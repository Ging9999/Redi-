"""Claim store for the agent coordination server.

Backed by SQLite (a file path, or ``:memory:`` for tests). All timestamps are
server-side epoch seconds — client clocks are never trusted (spec section 6).

Two tables:

``sessions``
    The latest self-reported intent for a session. Intent can arrive
    (``UserPromptSubmit``) before any file is claimed, so it lives on its own
    row and is copied onto claims as they are created.

``claims``
    One row per (repo, file, agent) claim. Primary key is
    ``(repo_key, file_path, machine_id, session_id)`` per spec section 4.

Expired claims are swept lazily on every read (spec section 6): a crashed
session must never hold a file hostage indefinitely.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional


# Default time-to-live for a claim, in seconds. Refreshed on every PostToolUse
# edit, so an actively working agent keeps its claim alive (spec section 6).
DEFAULT_TTL_SECONDS = 15 * 60

# Intent is captured from the user's prompt and echoed to every other machine,
# so cap it: bounds the row size and limits how much of a prompt (which may
# contain pasted secrets or noise) is stored and shared. Truncated with a marker.
MAX_INTENT_CHARS = 2000


def _clip_intent(intent: str) -> str:
    if intent is None:
        return ""
    intent = str(intent).strip()
    if len(intent) > MAX_INTENT_CHARS:
        return intent[: MAX_INTENT_CHARS - 1].rstrip() + "…"
    return intent


@dataclass
class Claim:
    repo_key: str
    file_path: str
    session_id: str
    machine_id: str
    display_name: str
    branch: str
    intent: str
    created_at: float
    expires_at: float

    def to_dict(self) -> dict:
        d = asdict(self)
        now = _now()
        # Surface a human-friendly "how long ago" so callers (and the agent
        # reading the warning) don't have to do clock math.
        d["age_seconds"] = max(0, int(now - self.created_at))
        # And how long until this claim lapses, so a reader can judge whether
        # it's worth waiting the other agent out.
        d["expires_in_seconds"] = max(0, int(self.expires_at - now))
        return d


def _now() -> float:
    return time.time()


class ClaimStore:
    """Thread-safe SQLite-backed claim registry.

    A single lock serialises writes; SQLite handles the persistence. This is
    deliberately modest — v1 targets a single-process server (spec section 2).
    """

    def __init__(self, db_path: str = ":memory:", ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        # check_same_thread=False lets the HTTP server's worker threads share
        # one connection; the lock below keeps access serialised.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id   TEXT PRIMARY KEY,
                    intent       TEXT NOT NULL DEFAULT '',
                    updated_at   REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS claims (
                    repo_key     TEXT NOT NULL,
                    file_path    TEXT NOT NULL,
                    session_id   TEXT NOT NULL,
                    machine_id   TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    branch       TEXT NOT NULL DEFAULT '',
                    intent       TEXT NOT NULL DEFAULT '',
                    created_at   REAL NOT NULL,
                    expires_at   REAL NOT NULL,
                    PRIMARY KEY (repo_key, file_path, machine_id, session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_claims_lookup
                    ON claims (repo_key, file_path);
                CREATE INDEX IF NOT EXISTS idx_claims_session
                    ON claims (session_id);
                """
            )
            self._conn.commit()

    # -- intent ---------------------------------------------------------------

    def set_intent(self, session_id: str, intent: str) -> None:
        """Record a session's latest intent and propagate it to live claims."""
        now = _now()
        intent = _clip_intent(intent)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (session_id, intent, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    intent = excluded.intent,
                    updated_at = excluded.updated_at
                """,
                (session_id, intent, now),
            )
            # Keep already-registered claims in sync with the newest intent.
            self._conn.execute(
                "UPDATE claims SET intent = ? WHERE session_id = ?",
                (intent, session_id),
            )
            self._conn.commit()

    def _get_intent(self, session_id: str) -> str:
        row = self._conn.execute(
            "SELECT intent FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row["intent"] if row else ""

    # -- claims ---------------------------------------------------------------

    def register(
        self,
        repo_key: str,
        file_path: str,
        session_id: str,
        machine_id: str,
        display_name: str = "",
        branch: str = "",
        intent: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> Claim:
        """Create or refresh a claim, extending ``expires_at`` from now."""
        now = _now()
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        expires_at = now + ttl
        with self._lock:
            # Fall back to the session's recorded intent when the caller does
            # not supply one explicitly (the common PostToolUse path).
            resolved_intent = (
                _clip_intent(intent) if intent is not None else self._get_intent(session_id)
            )

            existing = self._conn.execute(
                """
                SELECT created_at FROM claims
                WHERE repo_key = ? AND file_path = ?
                  AND machine_id = ? AND session_id = ?
                """,
                (repo_key, file_path, machine_id, session_id),
            ).fetchone()
            created_at = existing["created_at"] if existing else now

            self._conn.execute(
                """
                INSERT INTO claims (
                    repo_key, file_path, session_id, machine_id,
                    display_name, branch, intent, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_key, file_path, machine_id, session_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    branch       = excluded.branch,
                    intent       = excluded.intent,
                    expires_at   = excluded.expires_at
                """,
                (
                    repo_key, file_path, session_id, machine_id,
                    display_name, branch, resolved_intent, created_at, expires_at,
                ),
            )
            self._conn.commit()

        return Claim(
            repo_key=repo_key,
            file_path=file_path,
            session_id=session_id,
            machine_id=machine_id,
            display_name=display_name,
            branch=branch,
            intent=resolved_intent,
            created_at=created_at,
            expires_at=expires_at,
        )

    def check(
        self,
        repo_key: str,
        file_path: str,
        session_id: str,
        machine_id: str,
    ) -> list[Claim]:
        """Return live conflicting claims on a file, excluding the caller's own.

        A conflict is any *other* agent — different machine, or different
        session on the same machine — holding a live claim on the same file.
        """
        self._sweep_expired()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM claims
                WHERE repo_key = ? AND file_path = ?
                  AND NOT (machine_id = ? AND session_id = ?)
                ORDER BY created_at ASC
                """,
                (repo_key, file_path, machine_id, session_id),
            ).fetchall()
        return [self._row_to_claim(r) for r in rows]

    def release(self, session_id: str) -> int:
        """Drop all claims for a session. Returns the number removed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM claims WHERE session_id = ?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            self._conn.commit()
            return cur.rowcount

    def activity(self, repo_key: str) -> list[Claim]:
        """All live claims for a repo, newest first — for the activity view."""
        self._sweep_expired()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM claims WHERE repo_key = ? ORDER BY created_at DESC",
                (repo_key,),
            ).fetchall()
        return [self._row_to_claim(r) for r in rows]

    # -- housekeeping ---------------------------------------------------------

    def sweep(self) -> int:
        """Public sweep, for a background timer to bound DB growth on idle repos."""
        return self._sweep_expired()

    def _sweep_expired(self) -> int:
        """Delete claims whose TTL has lapsed. Called lazily on every read."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM claims WHERE expires_at <= ?", (now,)
            )
            self._conn.commit()
            return cur.rowcount

    @staticmethod
    def _row_to_claim(row: sqlite3.Row) -> Claim:
        return Claim(
            repo_key=row["repo_key"],
            file_path=row["file_path"],
            session_id=row["session_id"],
            machine_id=row["machine_id"],
            display_name=row["display_name"],
            branch=row["branch"],
            intent=row["intent"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
