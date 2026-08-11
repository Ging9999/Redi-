#!/usr/bin/env python3
"""Claude Code hook client for the agent coordination server.

One script handles all four lifecycle events (spec section 3). Claude Code
fires a hook by piping a JSON event on stdin; this script dispatches on the
``hook_event_name`` field:

    UserPromptSubmit  -> POST the prompt as the session's intent
    PreToolUse        -> check for conflicting claims; warn/ask/block on conflict
    PostToolUse       -> register/refresh a claim on the edited file
    Stop              -> release all claims for the session

Design commitments (spec sections 3 & 7):

* **Fail open, always.** Any error — server down, timeout, bad git state — must
  let the edit proceed. A coordination tool that stalls or breaks the agent
  gets uninstalled within a day. Every network call has a short timeout and
  every unexpected exception exits 0.

* **PreToolUse is the only gate.** On a conflict in the default ``warn`` mode we
  exit 2 with a rich human-readable reason on stderr. Claude Code feeds that
  stderr back to the agent, which then decides for itself: wait, work
  elsewhere, coordinate, or deliberately retry. That matches the spec's thesis
  that the *agent* adapts on intent, rather than escalating to the human.

Configuration is via environment variables (see ``hook/config.example.json``
for the settings.json wiring and README for the full list):

    COORD_URL       base URL of the server         (default http://127.0.0.1:8787)
    COORD_TOKEN     bearer token                   (optional; must match server)
    COORD_MODE      warn | ask | block             (default warn)
    COORD_TIMEOUT   per-request timeout, seconds    (default 0.5)
    COORD_ENABLED   set to 0/false to disable       (default enabled)
    COORD_ID_FILE   persisted machine id location   (default ~/.config/agent-coordinator/id)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import PurePosixPath, PureWindowsPath


# --- configuration -----------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


COORD_URL = os.environ.get("COORD_URL", "http://127.0.0.1:8787").rstrip("/")
COORD_TOKEN = os.environ.get("COORD_TOKEN") or None
COORD_MODE = os.environ.get("COORD_MODE", "warn").strip().lower()
COORD_TIMEOUT = float(os.environ.get("COORD_TIMEOUT", "0.5"))
COORD_ENABLED = _env_bool("COORD_ENABLED", True)
DEFAULT_ID_FILE = os.path.join(
    os.path.expanduser("~"), ".config", "agent-coordinator", "id"
)
COORD_ID_FILE = os.environ.get("COORD_ID_FILE", DEFAULT_ID_FILE)


# --- tiny HTTP client (stdlib only) ------------------------------------------


def _post(path: str, payload: dict) -> dict | None:
    """POST JSON, return parsed JSON, or None on any failure. Never raises."""
    return _request("POST", path, payload)


def _request(method: str, path: str, payload: dict | None) -> dict | None:
    url = f"{COORD_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if COORD_TOKEN:
        req.add_header("Authorization", f"Bearer {COORD_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=COORD_TIMEOUT) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError):
        # Server down, DNS failure, timeout, malformed response — fail open.
        return None


# --- git / identity helpers --------------------------------------------------


def _run_git(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def normalize_remote(url: str) -> str:
    """Normalize a git remote URL into a stable room key (spec section 2).

    ``git@github.com:acme/api.git`` and ``https://github.com/acme/api`` must
    collapse to the same key: strip protocol/credentials, strip a trailing
    ``.git``, lowercase.
    """
    u = url.strip()
    # scp-like syntax: git@host:owner/repo(.git)
    if u.startswith("git@") or ("@" in u.split("/")[0] and ":" in u and "://" not in u):
        # e.g. git@github.com:acme/api.git  ->  github.com/acme/api
        u = u.split("@", 1)[1]
        u = u.replace(":", "/", 1)
    else:
        # protocol://[user@]host/path
        if "://" in u:
            u = u.split("://", 1)[1]
        if "@" in u.split("/")[0]:
            u = u.split("@", 1)[1]
    if u.endswith(".git"):
        u = u[: -len(".git")]
    u = u.strip("/").lower()
    return u


def repo_key_for(cwd: str) -> str | None:
    remote = _run_git(["remote", "get-url", "origin"], cwd)
    if not remote:
        return None
    return normalize_remote(remote)


def repo_root(cwd: str) -> str | None:
    return _run_git(["rev-parse", "--show-toplevel"], cwd)


def current_branch(cwd: str) -> str:
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or ""


def display_name(cwd: str) -> str:
    return _run_git(["config", "user.name"], cwd) or ""


def machine_id() -> str:
    """A stable per-machine id: a UUID persisted under COORD_ID_FILE.

    Falls back to the hostname if the id file can't be written (read-only home,
    etc.) — still stable within a machine, just less collision-proof.
    """
    try:
        if os.path.exists(COORD_ID_FILE):
            with open(COORD_ID_FILE, "r", encoding="utf-8") as fh:
                val = fh.read().strip()
                if val:
                    return val
        os.makedirs(os.path.dirname(COORD_ID_FILE), exist_ok=True)
        new_id = uuid.uuid4().hex
        with open(COORD_ID_FILE, "w", encoding="utf-8") as fh:
            fh.write(new_id)
        return new_id
    except OSError:
        return socket.gethostname() or "unknown-machine"


def normalize_file_path(abs_path: str, root: str) -> str | None:
    """Convert an absolute tool path to a repo-relative, POSIX-separated path.

    This is the single most important correctness step (spec section 2): the
    hook receives a machine-specific absolute path, and every machine's clone
    lives somewhere different. We reduce it to a path relative to the repo root
    so the same file produces the same claim key everywhere.

    Handles both POSIX and Windows separators, and rejects anything that
    escapes the repo root (returns None).
    """
    if not abs_path or not root:
        return None

    # Accept either separator on input regardless of the host OS: a Windows
    # agent may send backslashes, a POSIX agent forward slashes.
    def _parts(p: str):
        p = p.replace("\\", "/")
        return PurePosixPath(p)

    ap = _parts(abs_path)
    rp = _parts(root)

    # Compare case-insensitively only for the drive/anchor on Windows-style
    # paths; keep file-name case as-is (POSIX is case-sensitive).
    try:
        rel = ap.relative_to(rp)
    except ValueError:
        # Path is not under the repo root — do not claim it.
        # Try a Windows-aware comparison as a fallback (drive letters differ
        # in case, separators already normalized above).
        try:
            wa = PureWindowsPath(abs_path)
            wr = PureWindowsPath(root)
            rel_w = wa.relative_to(wr)
            return PurePosixPath(*rel_w.parts).as_posix()
        except ValueError:
            return None

    rel_posix = rel.as_posix()
    # Guard against traversal that somehow survived (defensive).
    if rel_posix.startswith("..") or rel_posix == "":
        return None
    return rel_posix


# --- context assembly --------------------------------------------------------


class HookContext:
    """Everything needed to talk to the server for one hook invocation."""

    def __init__(self, event: dict):
        self.event = event
        self.session_id = event.get("session_id") or ""
        self.cwd = event.get("cwd") or os.getcwd()
        self.tool_name = event.get("tool_name") or ""
        self.tool_input = event.get("tool_input") or {}
        self.prompt = event.get("prompt") or ""

    def repo_context(self):
        """Return (repo_key, root, branch, display_name) or None if not a repo."""
        key = repo_key_for(self.cwd)
        root = repo_root(self.cwd)
        if not key or not root:
            return None
        return key, root, current_branch(self.cwd), display_name(self.cwd)

    def file_path(self):
        return self.tool_input.get("file_path") or self.tool_input.get("filePath")


# --- event handlers ----------------------------------------------------------
# Each returns a process exit code. Only PreToolUse ever returns non-zero.


def handle_user_prompt_submit(ctx: HookContext) -> int:
    """Ship the user's prompt to the server as the session's intent string."""
    if not ctx.session_id or not ctx.prompt:
        return 0
    _post(f"/sessions/{ctx.session_id}/intent", {"intent": ctx.prompt})
    return 0


def handle_post_tool_use(ctx: HookContext) -> int:
    """Register/refresh a claim on the file that was just edited."""
    rc = ctx.repo_context()
    fp = ctx.file_path()
    if not rc or not fp:
        return 0
    repo_key, root, branch, name = rc
    rel = normalize_file_path(fp, root)
    if not rel:
        return 0
    _post(
        "/claims",
        {
            "repo_key": repo_key,
            "file_path": rel,
            "session_id": ctx.session_id,
            "machine_id": machine_id(),
            "display_name": name,
            "branch": branch,
        },
    )
    return 0


def handle_stop(ctx: HookContext) -> int:
    """Release all of this session's claims."""
    if ctx.session_id:
        _post(f"/sessions/{ctx.session_id}/release", {})
    return 0


def _format_conflict_message(conflicts: list[dict]) -> str:
    """Human-readable warning the agent will read and act on (spec section 1)."""
    lines = []
    n = len(conflicts)
    header = (
        "Another agent is editing this file."
        if n == 1
        else f"{n} other agents are editing this file."
    )
    lines.append(header)
    for c in conflicts:
        who = c.get("display_name") or c.get("machine_id") or "unknown"
        branch = c.get("branch") or "(unknown branch)"
        intent = (c.get("intent") or "").strip()
        age = c.get("age_seconds")
        ago = _humanize_age(age) if age is not None else "recently"
        line = f"  - {who}'s session is on branch `{branch}`, started {ago}"
        if intent:
            line += f'\n    working on: "{intent}"'
        lines.append(line)
    lines.append(
        "\nDecide for yourself: wait, edit a different file, coordinate, or "
        "proceed anyway (retry the edit) if your change is independent."
    )
    return "\n".join(lines)


def _humanize_age(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago" if seconds > 5 else "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


def handle_pre_tool_use(ctx: HookContext) -> int:
    """Check for conflicts before the edit runs.

    Modes (spec section 7):
      warn  (default) : on conflict, exit 2 with the message on stderr. Blocks
                        this call and hands the agent the reason to decide on.
      ask             : exit 0 with permissionDecision "ask" — escalate to the
                        human operator instead of the agent.
      block           : exit 2 like warn, but the message tells the agent not to
                        proceed (a hard advisory lock).

    Any failure to reach the server fails open (exit 0): edits are never
    blocked by an unavailable coordination server.
    """
    rc = ctx.repo_context()
    fp = ctx.file_path()
    if not rc or not fp:
        return 0
    repo_key, root, _branch, _name = rc
    rel = normalize_file_path(fp, root)
    if not rel:
        return 0

    result = _post(
        "/claims/check",
        {
            "repo_key": repo_key,
            "file_path": rel,
            "session_id": ctx.session_id,
            "machine_id": machine_id(),
        },
    )
    # Server unreachable / slow / malformed -> fail open.
    if result is None:
        return 0
    conflicts = result.get("conflicts") or []
    if not conflicts:
        return 0

    message = _format_conflict_message(conflicts)

    if COORD_MODE == "ask":
        # Escalate to the human via a permission decision (parsed only on exit 0).
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": message,
            }
        }
        print(json.dumps(decision))
        return 0

    # warn (default) and block both surface the reason to the agent via stderr.
    # Exit 2 blocks this specific call and feeds stderr back to Claude; the JSON
    # decision path is NOT used here because a decision printed before exit 2 is
    # discarded (spec section 3).
    if COORD_MODE == "block":
        message += "\n\n[block mode] Do not edit this file until the other agent releases it."
    sys.stderr.write(message + "\n")
    return 2


HANDLERS = {
    "UserPromptSubmit": handle_user_prompt_submit,
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
    "Stop": handle_stop,
    # SubagentStop behaves like Stop for our purposes.
    "SubagentStop": handle_stop,
}


def main() -> int:
    if not COORD_ENABLED:
        return 0
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            return 0
    except (json.JSONDecodeError, ValueError):
        return 0

    event_name = event.get("hook_event_name") or ""
    handler = HANDLERS.get(event_name)
    if not handler:
        return 0

    try:
        return handler(HookContext(event))
    except Exception as exc:  # noqa: BLE001 — fail open on anything unexpected.
        sys.stderr.write(f"[coordinator-hook] non-fatal error (failing open): {exc}\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
