"""Claim store for the agent coordination server.

Backed by SQLite (a file path, or ``:memory:`` for tests). All timestamps are
server-side epoch seconds — client clocks are never trusted (spec section 6).

Tables:

``sessions``
    The latest self-reported intent for a session, with ``updated_at`` so we can
    report how stale that intent is (spec B6).

``claims``
    One row per (repo, file, agent) claim. PK ``(repo_key, file_path,
    machine_id, session_id)``.

``contributors``
    Who has reported claims on a repo and when, so a team can see coverage —
    who is actually running Redi (spec B2). Survives claim expiry.

``overrides``
    A record that an agent deliberately overrode someone's claim, with a reason,
    surfaced to the overridden agent on its next check (spec B5).

Expired claims/overrides are swept lazily on read and by a background timer.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional


# Claim TTL. Refreshed as an agent keeps editing (spec section 6).
DEFAULT_TTL_SECONDS = 15 * 60

# How long a solo session may skip the network before re-checking (spec A3).
DEFAULT_QUIET_SECONDS = 60

# How long an override note lingers for the overridden agent to see (spec B5).
DEFAULT_OVERRIDE_TTL_SECONDS = 15 * 60

# Intent is echoed to other machines; cap it (row size + secret hygiene).
MAX_INTENT_CHARS = 2000


def _now() -> float:
    return time.time()


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
    # When the intent was last (re)stated — for staleness hedging (spec B6).
    intent_updated_at: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        now = _now()
        d["age_seconds"] = max(0, int(now - self.created_at))
        d["expires_in_seconds"] = max(0, int(self.expires_at - now))
        # How old the intent statement is (not the claim). A confidently wrong,
        # 40-minute-old intent is worse than none, so the reader can hedge.
        base = self.intent_updated_at or self.created_at
        d["intent_age_seconds"] = max(0, int(now - base))
        return d


class ClaimStore:
    """Thread-safe SQLite-backed claim registry (single-process server)."""

    def __init__(
        self,
        db_path: str = ":memory:",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        quiet_seconds: int = DEFAULT_QUIET_SECONDS,
    ):
        self.ttl_seconds = ttl_seconds
        self.quiet_seconds = quiet_seconds
        self._is_memory = db_path == ":memory:"
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            # A6: WAL + relaxed sync. This is coordination metadata, not
            # financial records — losing the last few claims on a hard crash is
            # fine since they'd expire anyway. WAL is a no-op on :memory:.
            if not self._is_memory:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id   TEXT PRIMARY KEY,
                    intent       TEXT NOT NULL DEFAULT '',
                    updated_at   REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS claims (
                    repo_key          TEXT NOT NULL,
                    file_path         TEXT NOT NULL,
                    session_id        TEXT NOT NULL,
                    machine_id        TEXT NOT NULL,
                    display_name      TEXT NOT NULL DEFAULT '',
                    branch            TEXT NOT NULL DEFAULT '',
                    intent            TEXT NOT NULL DEFAULT '',
                    intent_updated_at REAL NOT NULL DEFAULT 0,
                    created_at        REAL NOT NULL,
                    expires_at        REAL NOT NULL,
                    PRIMARY KEY (repo_key, file_path, machine_id, session_id)
                );

                CREATE TABLE IF NOT EXISTS contributors (
                    repo_key     TEXT NOT NULL,
                    machine_id   TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    last_seen    REAL NOT NULL,
                    PRIMARY KEY (repo_key, machine_id, display_name)
                );

                CREATE TABLE IF NOT EXISTS overrides (
                    repo_key       TEXT NOT NULL,
                    file_path      TEXT NOT NULL,
                    target_session TEXT NOT NULL,
                    overrider_name TEXT NOT NULL DEFAULT '',
                    reason         TEXT NOT NULL DEFAULT '',
                    created_at     REAL NOT NULL,
                    expires_at     REAL NOT NULL,
                    PRIMARY KEY (repo_key, file_path, target_session)
                );

                -- A6: cover every hot query path with an index.
                CREATE INDEX IF NOT EXISTS idx_claims_lookup
                    ON claims (repo_key, file_path);
                CREATE INDEX IF NOT EXISTS idx_claims_agent
                    ON claims (machine_id, session_id);
                CREATE INDEX IF NOT EXISTS idx_claims_session
                    ON claims (session_id);
                CREATE INDEX IF NOT EXISTS idx_claims_expiry
                    ON claims (expires_at);
                CREATE INDEX IF NOT EXISTS idx_overrides_target
                    ON overrides (repo_key, file_path, target_session);
                """
            )
            self._migrate_columns()
            self._conn.commit()

    def _migrate_columns(self) -> None:
        """Add columns introduced after v1.0 to a pre-existing claims table."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(claims)")}
        if "intent_updated_at" not in cols:
            self._conn.execute(
                "ALTER TABLE claims ADD COLUMN intent_updated_at REAL NOT NULL DEFAULT 0"
            )

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
            self._conn.execute(
                "UPDATE claims SET intent = ?, intent_updated_at = ? WHERE session_id = ?",
                (intent, now, session_id),
            )
            self._conn.commit()

    def _get_intent(self, session_id: str):
        row = self._conn.execute(
            "SELECT intent, updated_at FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return "", 0.0
        return row["intent"], row["updated_at"]

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
        with self._lock:
            return self._register_locked(
                repo_key, file_path, session_id, machine_id,
                display_name, branch, intent, ttl_seconds,
            )

    def _register_locked(
        self, repo_key, file_path, session_id, machine_id,
        display_name, branch, intent, ttl_seconds,
    ) -> Claim:
        now = _now()
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        expires_at = now + ttl

        session_intent, session_intent_at = self._get_intent(session_id)
        if intent is not None:
            resolved_intent = _clip_intent(intent)
            intent_updated_at = now
        else:
            resolved_intent = session_intent
            intent_updated_at = session_intent_at or now

        existing = self._conn.execute(
            """
            SELECT created_at FROM claims
            WHERE repo_key = ? AND file_path = ? AND machine_id = ? AND session_id = ?
            """,
            (repo_key, file_path, machine_id, session_id),
        ).fetchone()
        created_at = existing["created_at"] if existing else now

        self._conn.execute(
            """
            INSERT INTO claims (
                repo_key, file_path, session_id, machine_id,
                display_name, branch, intent, intent_updated_at, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_key, file_path, machine_id, session_id) DO UPDATE SET
                display_name      = excluded.display_name,
                branch            = excluded.branch,
                intent            = excluded.intent,
                intent_updated_at = excluded.intent_updated_at,
                expires_at        = excluded.expires_at
            """,
            (
                repo_key, file_path, session_id, machine_id,
                display_name, branch, resolved_intent, intent_updated_at,
                created_at, expires_at,
            ),
        )
        self._record_contributor(repo_key, machine_id, display_name, now)
        self._conn.commit()

        return Claim(
            repo_key=repo_key, file_path=file_path, session_id=session_id,
            machine_id=machine_id, display_name=display_name, branch=branch,
            intent=resolved_intent, created_at=created_at, expires_at=expires_at,
            intent_updated_at=intent_updated_at,
        )

    def _conflicts_locked(self, repo_key, file_path, session_id, machine_id) -> list[Claim]:
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

    def check(self, repo_key, file_path, session_id, machine_id) -> list[Claim]:
        """Live conflicting claims on a file, excluding the caller's own."""
        self._sweep_expired()
        with self._lock:
            return self._conflicts_locked(repo_key, file_path, session_id, machine_id)

    def acquire(
        self,
        repo_key: str,
        file_path: str,
        session_id: str,
        machine_id: str,
        display_name: str = "",
        branch: str = "",
        intent: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        quiet_seconds: Optional[int] = None,
    ) -> dict:
        """Atomic check-and-stake (spec A2), plus quiet backoff (A3) and any
        override notes addressed to the caller (B5) — one transaction, one round
        trip on the hot path.

        Returns ``{conflicts, claim, quiet_until, overrides}``.
        """
        self._sweep_expired()
        with self._lock:
            conflicts = self._conflicts_locked(repo_key, file_path, session_id, machine_id)
            claim = self._register_locked(
                repo_key, file_path, session_id, machine_id,
                display_name, branch, intent, ttl_seconds,
            )
            # A3: is the caller the only active session on this repo? (Its own
            # claim now exists, so exclude it.)
            others = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM (
                    SELECT DISTINCT machine_id, session_id FROM claims
                    WHERE repo_key = ? AND NOT (machine_id = ? AND session_id = ?)
                )
                """,
                (repo_key, machine_id, session_id),
            ).fetchone()["n"]
            qwin = quiet_seconds if quiet_seconds is not None else self.quiet_seconds
            quiet_until = (_now() + qwin) if others == 0 else None

            overrides = self._consume_overrides_locked(repo_key, session_id)

        return {
            "conflicts": [c.to_dict() for c in conflicts],
            "claim": claim.to_dict(),
            "quiet_until": quiet_until,
            "overrides": overrides,
        }

    def release(self, session_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM claims WHERE session_id = ?", (session_id,)
            )
            self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            self._conn.commit()
            return cur.rowcount

    def activity(self, repo_key: str) -> list[Claim]:
        self._sweep_expired()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM claims WHERE repo_key = ? ORDER BY created_at DESC",
                (repo_key,),
            ).fetchall()
        return [self._row_to_claim(r) for r in rows]

    # -- contributors (B2) ----------------------------------------------------

    def _record_contributor(self, repo_key, machine_id, display_name, now) -> None:
        if not display_name:
            return
        self._conn.execute(
            """
            INSERT INTO contributors (repo_key, machine_id, display_name, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(repo_key, machine_id, display_name) DO UPDATE SET
                last_seen = excluded.last_seen
            """,
            (repo_key, machine_id, display_name, now),
        )

    def contributors(self, repo_key: str, since_seconds: float) -> list[dict]:
        """Distinct display_names that have reported on a repo within the window."""
        cutoff = _now() - since_seconds
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT display_name, machine_id, MAX(last_seen) AS last_seen
                FROM contributors
                WHERE repo_key = ? AND last_seen >= ?
                GROUP BY display_name
                ORDER BY last_seen DESC
                """,
                (repo_key, cutoff),
            ).fetchall()
        now = _now()
        return [
            {
                "display_name": r["display_name"],
                "machine_id": r["machine_id"],
                "last_seen_seconds_ago": max(0, int(now - r["last_seen"])),
            }
            for r in rows
        ]

    # -- overrides (B5) -------------------------------------------------------

    def record_override(
        self,
        repo_key: str,
        file_path: str,
        target_session: str,
        overrider_name: str = "",
        reason: str = "",
        ttl_seconds: int = DEFAULT_OVERRIDE_TTL_SECONDS,
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO overrides (
                    repo_key, file_path, target_session, overrider_name, reason,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_key, file_path, target_session) DO UPDATE SET
                    overrider_name = excluded.overrider_name,
                    reason         = excluded.reason,
                    created_at     = excluded.created_at,
                    expires_at     = excluded.expires_at
                """,
                (repo_key, file_path, target_session, overrider_name,
                 _clip_intent(reason), now, now + ttl_seconds),
            )
            self._conn.commit()

    def _consume_overrides_locked(self, repo_key, session_id) -> list[dict]:
        """Return + delete override notes addressed to this session (once each)."""
        now = _now()
        rows = self._conn.execute(
            """
            SELECT file_path, overrider_name, reason, created_at FROM overrides
            WHERE repo_key = ? AND target_session = ? AND expires_at > ?
            """,
            (repo_key, session_id, now),
        ).fetchall()
        if rows:
            self._conn.execute(
                "DELETE FROM overrides WHERE repo_key = ? AND target_session = ?",
                (repo_key, session_id),
            )
        return [
            {
                "file_path": r["file_path"],
                "overrider_name": r["overrider_name"],
                "reason": r["reason"],
                "age_seconds": max(0, int(now - r["created_at"])),
            }
            for r in rows
        ]

    # -- housekeeping ---------------------------------------------------------

    def sweep(self) -> int:
        return self._sweep_expired()

    def _sweep_expired(self) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute("DELETE FROM claims WHERE expires_at <= ?", (now,))
            self._conn.execute("DELETE FROM overrides WHERE expires_at <= ?", (now,))
            self._conn.commit()
            return cur.rowcount

    @staticmethod
    def _row_to_claim(row: sqlite3.Row) -> Claim:
        keys = row.keys()
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
            intent_updated_at=(row["intent_updated_at"] if "intent_updated_at" in keys else 0.0) or 0.0,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
