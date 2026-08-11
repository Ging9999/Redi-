#!/usr/bin/env python3
"""Install the coordination hook into a Claude Code settings.json.

Safely merges the four lifecycle hooks and the COORD_* env block into an
existing settings file, preserving everything already there and refusing to
create duplicate hook entries. Idempotent — run it twice, get the same result.

    # project-level (./.claude/settings.json), talking to a local server:
    python3 hook/install.py

    # user-level, pointing at a shared server with a token:
    python3 hook/install.py --user --url https://coord.example.com --token SECRET

    # preview without writing:
    python3 hook/install.py --dry-run
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys


HOOK_EVENTS = [
    # (event name, matcher or None)
    ("SessionStart", None),   # resolves the session cache — enables the fast path
    ("UserPromptSubmit", None),
    ("PreToolUse", "Edit|Write"),
    ("PostToolUse", "Edit|Write"),
    ("Stop", None),
]


def _command(python: str, hook_path: str) -> str:
    return f"{python} {hook_path}"


def _hook_entry(command: str, timeout: int) -> dict:
    return {"type": "command", "command": command, "timeout": timeout}


def _group_has_command(group: dict, command: str) -> bool:
    for h in group.get("hooks", []):
        if isinstance(h, dict) and h.get("command") == command:
            return True
    return False


def merge_settings(settings: dict, command: str, timeout: int, env: dict) -> tuple[dict, list[str]]:
    """Return (new_settings, list_of_changes). Pure — does not touch disk."""
    result = copy.deepcopy(settings)
    changes: list[str] = []

    # --- env block ---
    env_block = result.setdefault("env", {})
    for k, v in env.items():
        if v is None:
            continue
        if env_block.get(k) != v:
            env_block[k] = v
            changes.append(f"env.{k} = {v}")

    # --- hooks ---
    hooks = result.setdefault("hooks", {})
    for event, matcher in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        # Already installed anywhere in this event? Then skip (idempotent).
        if any(_group_has_command(g, command) for g in groups if isinstance(g, dict)):
            continue
        entry = _hook_entry(command, timeout)
        group = {"hooks": [entry]}
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
        changes.append(f"hooks.{event} += {command}"
                       + (f"  (matcher: {matcher})" if matcher else ""))

    return result, changes


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {path} is not valid JSON ({exc}). Refusing to overwrite it.")
    if not isinstance(data, dict):
        sys.exit(f"error: {path} does not contain a JSON object. Refusing to overwrite it.")
    return data


def main() -> int:
    default_hook = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinator_hook.py")

    ap = argparse.ArgumentParser(description="Install the coordination hook into settings.json")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--project", action="store_true", help="write ./.claude/settings.json (default)")
    scope.add_argument("--user", action="store_true", help="write ~/.claude/settings.json")
    ap.add_argument("--settings", help="explicit path to a settings.json (overrides scope)")
    ap.add_argument("--url", default="http://127.0.0.1:8787", help="COORD_URL")
    ap.add_argument("--token", help="COORD_TOKEN (shared bearer token)")
    ap.add_argument("--mode", default="warn", choices=["warn", "ask", "block"], help="COORD_MODE")
    ap.add_argument("--timeout", default="0.5", help="COORD_TIMEOUT seconds")
    ap.add_argument("--hook-timeout", type=int, default=5, help="Claude Code hook command timeout (s)")
    ap.add_argument("--python", default="python3", help="python interpreter used to run the hook")
    ap.add_argument("--hook-path", default=default_hook, help="path to coordinator_hook.py")
    ap.add_argument("--dry-run", action="store_true", help="print the merged result, write nothing")
    args = ap.parse_args()

    if args.settings:
        path = os.path.abspath(args.settings)
    elif args.user:
        path = os.path.expanduser("~/.claude/settings.json")
    else:
        path = os.path.abspath(os.path.join(".claude", "settings.json"))

    hook_path = os.path.abspath(args.hook_path)
    if not os.path.exists(hook_path):
        sys.stderr.write(f"warning: hook script not found at {hook_path}\n")

    command = _command(args.python, hook_path)
    env = {
        "COORD_URL": args.url,
        "COORD_TOKEN": args.token,   # None -> skipped
        "COORD_MODE": args.mode,
        "COORD_TIMEOUT": args.timeout,
    }

    existing = _load(path)
    merged, changes = merge_settings(existing, command, args.hook_timeout, env)

    if not changes:
        print(f"Nothing to do — hook already installed in {path}")
        return 0

    print(f"Target: {path}")
    print("Changes:")
    for c in changes:
        print(f"  + {c}")

    if args.dry_run:
        print("\n--- merged settings.json (dry run, not written) ---")
        print(json.dumps(merged, indent=2))
        return 0

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")
    print(f"\nWrote {path}")
    if not args.token:
        print("note: no --token set; the hook will talk to the server in open mode.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
