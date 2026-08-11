#!/usr/bin/env python3
"""Install / uninstall Redi's hooks in a Claude Code settings.json.

Safely merges the five lifecycle hooks (and an optional COORD_* env block) into
an existing settings file, preserving everything already there and never
duplicating an entry. Idempotent.

**Commands are PATH-resolved** (spec B2): the hooks are written as
``redi hook pre-tool-use`` — no absolute path — so a *committed*, project-level
``settings.json`` works on every teammate's machine (and on any OS). On a machine
where `redi` isn't installed, command-not-found is a non-blocking hook error, so
the edit still proceeds (fail-open). This is the single most important packaging
correctness property, and it has a dedicated test.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

# (event name, kebab CLI verb, matcher or None)
HOOK_EVENTS = [
    ("SessionStart", "session-start", None),
    ("UserPromptSubmit", "user-prompt-submit", None),
    ("PreToolUse", "pre-tool-use", "Edit|Write"),
    ("PostToolUse", "post-tool-use", "Edit|Write"),
    ("Stop", "stop", None),
]

DEFAULT_PREFIX = "redi hook"

# Substrings that identify a hook entry as ours, for idempotency + uninstall.
_REDI_MARKERS = ("redi hook", "redi.hook", "coordinator_hook.py")


def hook_command(kebab: str, prefix: str = DEFAULT_PREFIX) -> str:
    return f"{prefix} {kebab}"


def _is_redi_command(command: str) -> bool:
    return any(m in (command or "") for m in _REDI_MARKERS)


def _group_has_command(group: dict, command: str) -> bool:
    return any(
        isinstance(h, dict) and h.get("command") == command
        for h in group.get("hooks", [])
    )


def merge_settings(settings: dict, prefix: str, timeout: int, env: dict) -> tuple[dict, list[str]]:
    """Return (new_settings, changes). Pure — does not touch disk."""
    result = copy.deepcopy(settings)
    changes: list[str] = []

    env_block = result.setdefault("env", {})
    for k, v in env.items():
        if v is None:
            continue
        if env_block.get(k) != v:
            env_block[k] = v
            changes.append(f"env.{k} = {v}")
    if not env_block:  # don't leave an empty env dict we created
        result.pop("env", None)

    hooks = result.setdefault("hooks", {})
    for event, kebab, matcher in HOOK_EVENTS:
        command = hook_command(kebab, prefix)
        groups = hooks.setdefault(event, [])
        if any(_group_has_command(g, command) for g in groups if isinstance(g, dict)):
            continue
        group = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
        changes.append(f"hooks.{event} += {command}"
                       + (f"  (matcher: {matcher})" if matcher else ""))

    return result, changes


def uninstall_settings(settings: dict) -> tuple[dict, list[str]]:
    """Remove only Redi's hook entries + COORD_* env keys. Idempotent (B4)."""
    result = copy.deepcopy(settings)
    changes: list[str] = []

    hooks = result.get("hooks") or {}
    for event in list(hooks.keys()):
        groups = hooks.get(event) or []
        new_groups = []
        for g in groups:
            if not isinstance(g, dict):
                new_groups.append(g)
                continue
            kept = [h for h in g.get("hooks", [])
                    if not (isinstance(h, dict) and _is_redi_command(h.get("command", "")))]
            removed = len(g.get("hooks", [])) - len(kept)
            if removed:
                changes.append(f"hooks.{event} -= {removed} Redi entr"
                               + ("y" if removed == 1 else "ies"))
            if kept:
                g = dict(g)
                g["hooks"] = kept
                new_groups.append(g)
            # drop groups whose only hooks were ours
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)

    env_block = result.get("env") or {}
    for k in list(env_block.keys()):
        if k.startswith("COORD_"):
            del env_block[k]
            changes.append(f"env.{k} removed")
    if env_block:
        result["env"] = env_block
    else:
        result.pop("env", None)

    return result, changes


# --- file helpers ------------------------------------------------------------


def load_settings(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {path} is not valid JSON ({exc}). Refusing to touch it.")
    if not isinstance(data, dict):
        raise SystemExit(f"error: {path} is not a JSON object. Refusing to touch it.")
    return data


def write_settings(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def settings_path(scope: str = "project", explicit: str | None = None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    if scope == "user":
        return os.path.expanduser("~/.claude/settings.json")
    return os.path.abspath(os.path.join(".claude", "settings.json"))


def install(path: str, env: dict, prefix: str = DEFAULT_PREFIX,
            hook_timeout: int = 5, dry_run: bool = False) -> list[str]:
    merged, changes = merge_settings(load_settings(path), prefix, hook_timeout, env)
    if changes and not dry_run:
        write_settings(path, merged)
    return changes


def uninstall(path: str, dry_run: bool = False) -> list[str]:
    result, changes = uninstall_settings(load_settings(path))
    if changes and not dry_run:
        write_settings(path, result)
    return changes


# --- CLI (also reachable as `python3 -m redi.install`) -----------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Install Redi hooks into settings.json")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--project", action="store_true", help="./.claude/settings.json (default)")
    scope.add_argument("--user", action="store_true", help="~/.claude/settings.json")
    ap.add_argument("--settings", help="explicit settings.json path")
    ap.add_argument("--url", help="COORD_URL to write into the env block")
    ap.add_argument("--token", help="COORD_TOKEN (prefer credentials; avoid committing)")
    ap.add_argument("--mode", choices=["warn", "ask", "block"], help="COORD_MODE")
    ap.add_argument("--timeout", help="COORD_TIMEOUT seconds")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX,
                    help='hook command prefix (default "redi hook")')
    ap.add_argument("--hook-timeout", type=int, default=5)
    ap.add_argument("--no-env", action="store_true",
                    help="write hooks only (recommended for a committed project file)")
    ap.add_argument("--uninstall", action="store_true", help="remove Redi hooks instead")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    scope_name = "user" if args.user else "project"
    path = settings_path(scope_name, args.settings)

    if args.uninstall:
        result, changes = uninstall_settings(load_settings(path))
        _report(path, changes, result if args.dry_run else None)
        if changes and not args.dry_run:
            write_settings(path, result)
            print(f"\nRemoved Redi hooks from {path}")
        return 0

    env = {} if args.no_env else {
        "COORD_URL": args.url, "COORD_TOKEN": args.token,
        "COORD_MODE": args.mode, "COORD_TIMEOUT": args.timeout,
    }
    merged, changes = merge_settings(load_settings(path), args.prefix, args.hook_timeout, env)
    _report(path, changes, merged if args.dry_run else None)
    if changes and not args.dry_run:
        write_settings(path, merged)
        print(f"\nWrote {path}")
    return 0


def _report(path: str, changes: list[str], preview: dict | None) -> None:
    if not changes:
        print(f"Nothing to do — {path}")
        return
    print(f"Target: {path}\nChanges:")
    for c in changes:
        print(f"  + {c}")
    if preview is not None:
        print("\n--- merged (dry run, not written) ---")
        print(json.dumps(preview, indent=2))


if __name__ == "__main__":
    sys.exit(main())
