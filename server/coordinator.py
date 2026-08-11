#!/usr/bin/env python3
"""Cross-machine agent edit coordination server.

A small single-process HTTP service holding a claims registry, so Claude Code
agents on different machines can see when another agent is already editing a
file — and, crucially, *what* that agent is doing (spec section 1).

Stdlib only. Run it, point the hook client at it, done.

Endpoints (spec section 5):

    POST /claims/check                 -> {conflicts: [Claim]}   (excludes caller)
    POST /claims                       -> {claim: Claim}          (register/refresh)
    POST /sessions/{session_id}/intent -> {ok: true}              (set intent)
    POST /sessions/{session_id}/release-> {released: N}           (drop claims)
    GET  /repos/{repo_key}/activity    -> {claims: [Claim]}       (debug/dashboard)
    GET  /healthz                      -> {ok: true}              (liveness)

Auth: a single bearer token (spec section 5/2 — no multi-tenant auth in v1).
Set ``COORD_TOKEN`` and clients must send ``Authorization: Bearer <token>``.
If ``COORD_TOKEN`` is unset the server runs open (development only) and says so.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

# Allow running as `python3 server/coordinator.py` or `python3 -m server.coordinator`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from store import DEFAULT_TTL_SECONDS, ClaimStore  # noqa: E402


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
        # Constant-ish comparison; tokens are short-lived shared secrets in v1.
        return header == expected

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
            self._send_json(200, {"ok": True})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        parts = [p for p in path.split("/") if p != ""]
        # GET /repos/{repo_key}/activity
        if len(parts) == 3 and parts[0] == "repos" and parts[2] == "activity":
            repo_key = unquote(parts[1])
            claims = STORE.activity(repo_key)
            self._send_json(200, {"claims": [c.to_dict() for c in claims]})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        data = self._read_json_body()
        if data is None:
            return  # error already sent

        parts = [p for p in path.split("/") if p != ""]

        # POST /claims/check
        if parts == ["claims", "check"]:
            self._handle_check(data)
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


def main() -> None:
    global STORE, AUTH_TOKEN

    host = os.environ.get("COORD_HOST", "127.0.0.1")
    port = int(os.environ.get("COORD_PORT", "8787"))
    db_path = os.environ.get("COORD_DB", "coordinator.db")
    ttl = int(os.environ.get("COORD_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))
    AUTH_TOKEN = os.environ.get("COORD_TOKEN") or None

    STORE = ClaimStore(db_path=db_path, ttl_seconds=ttl)

    server = build_server(host, port)
    auth_state = "token auth ENABLED" if AUTH_TOKEN else "OPEN (no COORD_TOKEN set — dev only)"
    sys.stderr.write(
        f"[coordinator] listening on http://{host}:{port}  db={db_path}  ttl={ttl}s  {auth_state}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[coordinator] shutting down\n")
    finally:
        server.shutdown()
        STORE.close()


if __name__ == "__main__":
    main()
