#!/usr/bin/env python3
"""`redi` — command-line companion to the coordination hook.

    redi doctor    diagnose a setup: hooks wired? server reachable? auth ok?
                   git remote? machine id? who else is here?
    redi status    show live claims for the current repo (who/file/branch/
                   intent/age) so the human can see what the agents see.

Fail-open is correct at *runtime* but terrible during *setup*: a wrong token, a
typo'd URL, or an unregistered hook all produce a Redi that silently does
nothing, and a developer can believe they're covered for a week (spec B1). This
command makes that failure visible.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Reuse the hook's resolution logic (config, git, identity, HTTP).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hook"))
import coordinator_hook as hook  # noqa: E402


# --- pretty output -----------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def ok(msg: str) -> None:
    print(f"  {_c('32', 'OK')}   {msg}")


def warn(msg: str) -> None:
    print(f"  {_c('33', 'WARN')} {msg}")


def bad(msg: str, fix: str = "") -> None:
    print(f"  {_c('31', 'FAIL')} {msg}")
    if fix:
        print(f"       ↳ {fix}")


def section(title: str) -> None:
    print(f"\n{_c('1', title)}")


# --- settings.json resolution ------------------------------------------------

SETTINGS_CANDIDATES = [
    (".claude/settings.local.json", "project-local"),
    (".claude/settings.json", "project"),
    (os.path.expanduser("~/.claude/settings.json"), "user"),
]


def _hooks_have_redi(settings: dict) -> set:
    """Return the set of event names wired to coordinator_hook.py."""
    found = set()
    for event, groups in (settings.get("hooks") or {}).items():
        for g in groups if isinstance(groups, list) else []:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                if "coordinator_hook.py" in (h.get("command") or ""):
                    found.add(event)
    return found


def _resolve_settings(cwd: str):
    """Walk the precedence order; return (path, scope, events) for each file that
    wires Redi, plus whichever wins."""
    results = []
    for rel, scope in SETTINGS_CANDIDATES:
        path = rel if os.path.isabs(rel) else os.path.join(cwd, rel)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        events = _hooks_have_redi(data)
        if events:
            results.append((path, scope, events))
    return results


# --- server calls ------------------------------------------------------------


def _healthz(cfg):
    t0 = time.perf_counter()
    resp = hook._request("GET", f"{cfg['url']}/healthz", None, None, cfg["timeout"])
    return resp, (time.perf_counter() - t0) * 1000


def _activity(cfg, repo_key):
    url = f"{cfg['url']}/repos/{hook._q(repo_key)}/activity"
    return hook._request("GET", url, None, cfg["token"], cfg["timeout"])


# --- commands ----------------------------------------------------------------


def cmd_doctor(args) -> int:
    cwd = os.getcwd()
    print(_c("1", "redi doctor") + f"  ({cwd})")

    root, branch = hook.root_and_branch(cwd)
    cfg = hook.resolve_config(root or "")

    section("Hook registration")
    wired = _resolve_settings(cwd)
    required = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
    if not wired:
        bad("no settings.json wires coordinator_hook.py",
            "run:  python3 hook/install.py   (project)  or  --user")
    else:
        winner = wired[0]
        ok(f"hooks found in {winner[1]} settings: {winner[0]}")
        missing = required - winner[2]
        if missing:
            warn(f"missing events: {', '.join(sorted(missing))} "
                 f"(SessionStart is needed for the fast path)")
        if len(wired) > 1:
            others = ", ".join(f"{w[1]}" for w in wired[1:])
            warn(f"also wired in: {others} (project-local/project win over user)")

    section("Configuration")
    print(f"  URL     {cfg['url']}")
    print(f"  mode    {cfg['mode']}")
    print(f"  timeout {cfg['timeout']}s")
    print(f"  token   {'set' if cfg['token'] else '(none — open mode)'}")
    if root and os.path.exists(os.path.join(root, ".redi.toml")):
        ok(f".redi.toml found at {root}")

    section("Git / identity")
    repo_key = hook.repo_key_for(cwd)
    if not repo_key:
        bad("no git 'origin' remote — Redi scopes claims by remote URL",
            "add one:  git remote add origin <url>   (or cd into a cloned repo)")
    else:
        ok(f"repo_key = {repo_key}")
        print(f"       branch = {branch or '(unknown)'}")
    name = hook.git_display_name(cwd)
    (ok if name else warn)(
        f"display_name = {name!r}" if name else
        "git user.name is unset — you'll show as your machine id to others")
    mid = hook.machine_id()
    id_file = hook._env("COORD_ID_FILE") or os.path.join(
        os.path.expanduser("~"), ".config", "agent-coordinator", "id")
    if os.path.exists(id_file):
        ok(f"machine_id = {mid}  (persisted at {id_file})")
    else:
        warn(f"machine_id = {mid}  (not persisted — check {id_file} perms)")

    section("Server")
    health, ms = _healthz(cfg)
    if not health:
        bad(f"cannot reach {cfg['url']}",
            "is the server running?  make serve   (or fix COORD_URL)")
        return 1
    ok(f"reachable in {ms:.0f} ms — version {health.get('version', '?')}")

    if repo_key:
        act = _activity(cfg, repo_key)
        if act is None:
            bad("authorized request failed (activity endpoint)",
                "token mismatch? ensure COORD_TOKEN matches the server's")
        else:
            ok("auth accepted")
            claims = act.get("claims", [])
            sessions = {(c["machine_id"], c["session_id"]) for c in claims}
            print(f"       {len(claims)} live claim(s), "
                  f"{len(sessions)} session(s) on this repo")
            contribs = act.get("contributors", [])
            if contribs:
                names = ", ".join(sorted({c["display_name"] for c in contribs}))
                print(f"       recent contributors reporting: {names}")

    print()
    return 0


def cmd_status(args) -> int:
    cwd = os.getcwd()
    root, _ = hook.root_and_branch(cwd)
    cfg = hook.resolve_config(root or "")
    repo_key = hook.repo_key_for(cwd)
    if not repo_key:
        print("not a git repo with an 'origin' remote — nothing to show", file=sys.stderr)
        return 1

    act = _activity(cfg, repo_key)
    if act is None:
        print(f"could not reach {cfg['url']} (or auth failed)", file=sys.stderr)
        return 1

    claims = act.get("claims", [])
    print(_c("1", f"Live claims on {repo_key}") + f"  ({len(claims)})")
    if not claims:
        print("  (none — no agent is currently editing a file here)")
    for c in sorted(claims, key=lambda c: c.get("age_seconds", 0)):
        who = c.get("display_name") or c.get("machine_id") or "unknown"
        age = hook._humanize_age(c.get("age_seconds") or 0)
        line = f"  {_c('1', c['file_path'])}  —  {who}  (branch {c.get('branch') or '?'}, {age})"
        print(line)
        intent = (c.get("intent") or "").strip()
        if intent:
            stale = (c.get("intent_age_seconds") or 0) > hook.STALE_INTENT_SECONDS
            label = "started from" if stale else "working on"
            print(f"        {label}: {intent}")

    contribs = act.get("contributors", [])
    if contribs:
        print()
        print(_c("1", "Reporting contributors (last 7d):"))
        for c in contribs:
            ago = hook._humanize_age(c.get("last_seen_seconds_ago") or 0)
            print(f"  {c['display_name']}  (last seen {ago})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="redi", description="Redi coordination CLI")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="diagnose the local setup")
    sub.add_parser("status", help="show live claims for the current repo")
    args = ap.parse_args()
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "status":
        return cmd_status(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
