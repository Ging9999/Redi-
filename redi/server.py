#!/usr/bin/env python3
"""Cross-machine agent edit coordination server.

A small single-process HTTP service holding a claims registry, so Claude Code
agents on different machines can see when another agent is already editing a
file — and, crucially, *what* that agent is doing (spec section 1).

Stdlib only. Run it, point the hook client at it, done.

Endpoints:

    POST /claims/acquire               -> {conflicts, claim, quiet_until, overrides}
    POST /claims/check                 -> {conflicts: [Claim]}   (excludes caller)
    POST /claims                       -> {claim: Claim}          (register/refresh)
    POST /claims/override              -> {ok: true}              (record an override)
    POST /sessions/{session_id}/intent -> {ok: true}              (set intent)
    POST /sessions/{session_id}/release-> {released: N}           (drop claims)
    GET  /repos/{repo_key}/activity    -> {claims, contributors}  (debug/status; ETag)
    GET  /healthz                      -> {ok: true, version}     (liveness)

Auth: a single bearer token (spec section 5/2 — no multi-tenant auth in v1).
Set ``COORD_TOKEN`` and clients must send ``Authorization: Bearer <token>``.
If ``COORD_TOKEN`` is unset the server runs open (development only) and says so.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .store import DEFAULT_QUIET_SECONDS, DEFAULT_TTL_SECONDS, ClaimStore


VERSION = __version__

# Populated in main(); a module global so the handler class can reach it.
STORE: ClaimStore | None = None
AUTH_TOKEN: str | None = None
MAX_BODY_BYTES = 64 * 1024


class CoordinatorHandler(BaseHTTPRequestHandler):
    server_version = "AgentCoordinator/1.0"

    # -- helpers --------------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return None
        if not isinstance(data, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return None
        return data

    def _authorized(self) -> bool:
        if not AUTH_TOKEN:
            return True  # open mode (dev)
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {AUTH_TOKEN}"
        # Constant-time comparison so a wrong token can't be recovered by timing.
        return hmac.compare_digest(header, expected)

    def _require_fields(self, data: dict, fields: list[str]) -> bool:
        missing = [f for f in fields if not data.get(f)]
        if missing:
            self._send_json(400, {"error": f"missing fields: {', '.join(missing)}"})
            return False
        return True

    # -- routing --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"ok": True, "version": VERSION})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        parts = [p for p in path.split("/") if p != ""]
        # GET /repos/{repo_key}/activity?days=N
        if len(parts) == 3 and parts[0] == "repos" and parts[2] == "activity":
            self._handle_activity(unquote(parts[1]), urlparse(self.path).query)
            return

        self._send_json(404, {"error": "not found"})

    def _handle_activity(self, repo_key: str, query: str) -> None:
        days = 7
        try:
            q = parse_qs(query)
            if "days" in q:
                days = max(0, float(q["days"][0]))
        except (ValueError, KeyError):
            pass
        claims = STORE.activity(repo_key)
        contributors = STORE.contributors(repo_key, since_seconds=days * 86400)
        # A8: an ETag derived from claim identity + version (expires_at/intent),
        # NOT the age fields, so it stays stable until the claim set changes.
        sig = "|".join(
            f"{c.file_path}:{c.session_id}:{c.machine_id}:{c.expires_at}:{c.intent}"
            for c in claims
        )
        etag = '"' + hashlib.md5(sig.encode("utf-8")).hexdigest() + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        body = json.dumps({
            "claims": [c.to_dict() for c in claims],
            "contributors": contributors,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        data = self._read_json_body()
        if data is None:
            return  # error already sent

        parts = [p for p in path.split("/") if p != ""]

        # POST /claims/acquire
        if parts == ["claims", "acquire"]:
            self._handle_acquire(data)
            return
        # POST /claims/check
        if parts == ["claims", "check"]:
            self._handle_check(data)
            return
        # POST /claims/override
        if parts == ["claims", "override"]:
            self._handle_override(data)
            return
        # POST /claims
        if parts == ["claims"]:
            self._handle_register(data)
            return
        # POST /sessions/{session_id}/intent
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "intent":
            self._handle_intent(unquote(parts[1]), data)
            return
        # POST /sessions/{session_id}/release
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "release":
            self._handle_release(unquote(parts[1]))
            return

        self._send_json(404, {"error": "not found"})

    # -- handlers -------------------------------------------------------------

    def _handle_acquire(self, data: dict) -> None:
        if not self._require_fields(data, ["repo_key", "file_path", "session_id", "machine_id"]):
            return
        result = STORE.acquire(
            repo_key=data["repo_key"],
            file_path=data["file_path"],
            session_id=data["session_id"],
            machine_id=data["machine_id"],
            display_name=data.get("display_name", ""),
            branch=data.get("branch", ""),
            intent=data.get("intent"),
        )
        self._send_json(200, result)

    def _handle_override(self, data: dict) -> None:
        if not self._require_fields(
            data, ["repo_key", "file_path", "target_session"]
        ):
            return
        STORE.record_override(
            repo_key=data["repo_key"],
            file_path=data["file_path"],
            target_session=data["target_session"],
            overrider_name=data.get("overrider_name", ""),
            reason=data.get("reason", ""),
        )
        self._send_json(200, {"ok": True})

    def _handle_check(self, data: dict) -> None:
        if not self._require_fields(data, ["repo_key", "file_path", "session_id", "machine_id"]):
            return
        conflicts = STORE.check(
            repo_key=data["repo_key"],
            file_path=data["file_path"],
            session_id=data["session_id"],
            machine_id=data["machine_id"],
        )
        self._send_json(200, {"conflicts": [c.to_dict() for c in conflicts]})

    def _handle_register(self, data: dict) -> None:
        if not self._require_fields(data, ["repo_key", "file_path", "session_id", "machine_id"]):
            return
        claim = STORE.register(
            repo_key=data["repo_key"],
            file_path=data["file_path"],
            session_id=data["session_id"],
            machine_id=data["machine_id"],
            display_name=data.get("display_name", ""),
            branch=data.get("branch", ""),
            # Only override the stored session intent when explicitly provided.
            intent=data.get("intent"),
        )
        self._send_json(200, {"claim": claim.to_dict()})

    def _handle_intent(self, session_id: str, data: dict) -> None:
        if "intent" not in data:
            self._send_json(400, {"error": "missing field: intent"})
            return
        STORE.set_intent(session_id, str(data["intent"]))
        self._send_json(200, {"ok": True})

    def _handle_release(self, session_id: str) -> None:
        released = STORE.release(session_id)
        self._send_json(200, {"released": released})

    # Quieter, structured-ish logging to stderr.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[coordinator] %s - %s\n" % (self.address_string(), fmt % args))


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), CoordinatorHandler)


def _start_sweeper(interval_seconds: float, stop_event: threading.Event) -> threading.Thread:
    """Background TTL sweep so an idle repo's DB doesn't grow unbounded.

    Reads already sweep lazily (spec section 6); this just bounds growth when no
    reads are happening. Runs as a daemon and exits promptly on shutdown.
    """

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                if STORE is not None:
                    STORE.sweep()
            except Exception as exc:  # noqa: BLE001 — never let the sweeper die.
                sys.stderr.write(f"[coordinator] sweep error (ignored): {exc}\n")

    t = threading.Thread(target=_loop, name="ttl-sweeper", daemon=True)
    t.start()
    return t


def join_string() -> str:
    """The `redi join ...` command a host shares with the team (spec C3)."""
    from . import creds
    token, _ = creds.load_or_create_server_token()
    port = int(os.environ.get("COORD_PORT", "8787"))
    return "redi join " + creds.build_join_url(token, creds.external_host(), port)


def print_join(stream=None) -> None:
    (stream or sys.stdout).write(
        "\nShare this with your team:\n\n    " + join_string() + "\n\n"
    )


def serve(local: bool = False) -> None:
    """Run the coordination server. ``local`` = zero-config localhost, open mode
    (spec C5), for evaluating with two sessions on one machine."""
    global STORE, AUTH_TOKEN
    from . import creds

    if local:
        os.environ.setdefault("COORD_HOST", "127.0.0.1")
        os.environ.setdefault("COORD_DB", ":memory:")
        os.environ["COORD_TOKEN"] = ""  # open mode

    host = os.environ.get("COORD_HOST", "127.0.0.1")
    port = int(os.environ.get("COORD_PORT", "8787"))
    db_path = os.environ.get("COORD_DB", "coordinator.db")
    ttl = int(os.environ.get("COORD_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))
    quiet = int(os.environ.get("COORD_QUIET_SECONDS", str(DEFAULT_QUIET_SECONDS)))

    # C3: first-run token generation — no secret to invent, none to configure.
    if local:
        AUTH_TOKEN = None
        token_created = False
    else:
        AUTH_TOKEN, token_created = creds.load_or_create_server_token()

    STORE = ClaimStore(db_path=db_path, ttl_seconds=ttl, quiet_seconds=quiet)

    server = build_server(host, port)
    stop_event = threading.Event()
    _start_sweeper(max(30.0, ttl / 2.0), stop_event)

    if AUTH_TOKEN:
        auth_state = "token auth ENABLED" + (" (generated on first run)" if token_created else "")
    else:
        auth_state = "OPEN (no token — local/dev only)"
    sys.stdout.write(f"\nRedi {VERSION} running on :{port}\n")
    sys.stdout.write(f"Data: {db_path}\n")
    sys.stderr.write(
        f"[coordinator] v{VERSION} listening on http://{host}:{port}  "
        f"db={db_path}  ttl={ttl}s  {auth_state}\n"
    )
    # Print the join string on EVERY startup (spec C3) — the host needs it again
    # each time a new person joins and shouldn't have to hunt for it.
    if not local:
        print_join()
    else:
        sys.stdout.write("\nLocal mode (open, in-memory). Point a session at:\n"
                         f"    redi join redi://127.0.0.1:{port}\n\n")

    def _graceful(signum, _frame):
        sys.stderr.write(f"\n[coordinator] signal {signum}, shutting down\n")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _graceful)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[coordinator] shutting down\n")
    finally:
        stop_event.set()
        server.server_close()
        STORE.close()


# Backwards-compatible alias.
main = serve


if __name__ == "__main__":
    serve()
