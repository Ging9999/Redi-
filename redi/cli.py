#!/usr/bin/env python3
"""`redi` — the one command a teammate runs.

    redi join <url>     connect + save credentials + install hooks (atomic)
    redi status         live claims for this repo
    redi doctor         diagnose the local setup
    redi serve [--local] run a coordination server
    redi hook <event>   internal — invoked by Claude Code
    redi uninstall      remove hooks (keep credentials)
    redi print-join     print the join string for this server (host side)

Module scope stays light (argparse/json/os/sys) and the hot `hook` path is
dispatched before argparse, so `redi hook pre-tool-use` pays almost nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


# --- pretty output -----------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def ok(msg: str) -> None:
    print(f"  {_c('32', '✓')} {msg}")


def warn(msg: str) -> None:
    print(f"  {_c('33', '!')} {msg}")


def bad(msg: str, fix: str = "") -> None:
    print(f"  {_c('31', '✗')} {msg}")
    if fix:
        print(f"      ↳ {fix}")


def section(title: str) -> None:
    print(f"\n{_c('1', title)}")


# --- version compatibility (C6) ---------------------------------------------


def _major(v: str) -> str:
    return (v or "0").split(".", 1)[0]


def check_version(server_version: str) -> str:
    """Return a warning string on a major-version mismatch, else ''."""
    from . import __version__
    if not server_version:
        return ""
    if _major(server_version) != _major(__version__):
        return (f"version mismatch: client {__version__} vs server {server_version} "
                f"(different major versions may be incompatible)")
    return ""


# --- settings.json resolution (for doctor) -----------------------------------

SETTINGS_CANDIDATES = [
    (".claude/settings.local.json", "project-local"),
    (".claude/settings.json", "project"),
    (os.path.expanduser("~/.claude/settings.json"), "user"),
]


def _hooks_have_redi(settings: dict) -> set:
    found = set()
    for event, groups in (settings.get("hooks") or {}).items():
        for g in groups if isinstance(groups, list) else []:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                cmd = h.get("command") or ""
                if "redi hook" in cmd or "redi.hook" in cmd or "coordinator_hook.py" in cmd:
                    found.add(event)
    return found


def _resolve_settings(cwd: str):
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


def _healthz(base_url, timeout):
    import time
    from . import hook
    t0 = time.perf_counter()
    resp = hook._request("GET", f"{base_url}/healthz", None, None, timeout)
    return resp, (time.perf_counter() - t0) * 1000


def _auth_check(base_url, token, timeout):
    """A harmless authed GET: activity of a nonexistent repo. 200 => token ok,
    None => unreachable or 401."""
    from . import hook
    return hook._request(
        "GET", f"{base_url}/repos/{hook._q('__auth_check__')}/activity",
        None, token, timeout)


# --- commands ----------------------------------------------------------------


def cmd_join(args) -> int:
    from . import creds, hook, install

    try:
        info = creds.parse_join_url(args.url)
    except ValueError as exc:
        print(f"✗ bad join URL: {exc}", file=sys.stderr)
        return 2
    host, base_url, token = info["host"], info["base_url"], info["token"]
    timeout = float(hook._env("COORD_TIMEOUT") or 3.0)

    insecure = creds.warn_if_insecure(base_url, host)
    if insecure:
        print(_c("33", insecure), file=sys.stderr)

    # 1) connectivity + version (before writing anything).
    health, ms = _healthz(base_url, timeout)
    if health is None:
        print(f"✗ cannot reach {base_url} — installed nothing.", file=sys.stderr)
        print("  Check the URL/port and that the server is running.", file=sys.stderr)
        return 1
    vwarn = check_version(health.get("version", ""))

    # 2) auth.
    if _auth_check(base_url, token, timeout) is None:
        print(f"✗ connected to {host}, but the token was rejected — installed nothing.",
              file=sys.stderr)
        print("  Ask the host to re-send `redi print-join`.", file=sys.stderr)
        return 1

    # Snapshot for rollback.
    prior_cred = creds.get_credential(host)
    settings = install.settings_path("user", args.settings)
    prior_settings = None
    if os.path.exists(settings):
        with open(settings, encoding="utf-8") as fh:
            prior_settings = fh.read()

    try:
        if token:
            cred_path = creds.save_credential(host, token, base_url)
        else:
            cred_path = "(no token — open server)"
        # Hooks are PATH-resolved; URL goes in env, token stays in credentials.
        changes = install.install(settings, env={"COORD_URL": base_url},
                                  prefix=install.DEFAULT_PREFIX)
    except Exception as exc:  # noqa: BLE001 — roll back partial state
        _rollback(host, prior_cred, settings, prior_settings, creds)
        print(f"✗ join failed ({exc}); rolled back — no partial state left.",
              file=sys.stderr)
        return 1

    print(f"✓ Connected to {host} (server {health.get('version', '?')})")
    print(f"✓ Credentials saved to {cred_path}")
    if changes:
        print(f"✓ Hooks installed in {settings}")
    else:
        print(f"✓ Hooks already present in {settings}")
    if vwarn:
        print(_c("33", "! " + vwarn))
    print("\nRedi is active. Run `redi status` to see live claims.")
    return 0


def _rollback(host, prior_cred, settings_path, prior_settings, creds):
    try:
        if prior_cred is None:
            creds.remove_credential(host)
        else:
            creds.save_credential(host, prior_cred.get("token", ""),
                                  prior_cred.get("base_url", ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        if prior_settings is None:
            if os.path.exists(settings_path):
                os.remove(settings_path)
        else:
            with open(settings_path, "w", encoding="utf-8") as fh:
                fh.write(prior_settings)
    except Exception:  # noqa: BLE001
        pass


def cmd_uninstall(args) -> int:
    from . import install
    path = install.settings_path("user" if not args.project else "project", args.settings)
    changes = install.uninstall(path, dry_run=args.dry_run)
    if not changes:
        print(f"No Redi hooks found in {path}")
        return 0
    for c in changes:
        print(f"  - {c}")
    print(f"\n{'Would remove' if args.dry_run else 'Removed'} Redi hooks from {path}")
    print("Credentials were kept (~/.config/redi/credentials). "
          "Delete that file to fully remove.")
    return 0


def cmd_serve(args) -> int:
    from . import server
    server.serve(local=args.local)
    return 0


def cmd_print_join(args) -> int:
    from . import server
    server.print_join()
    return 0


def cmd_doctor(args) -> int:
    from . import creds, hook
    cwd = os.getcwd()
    print(_c("1", "redi doctor") + f"  ({cwd})")

    root, branch = hook.root_and_branch(cwd)
    cfg = hook.resolve_config(root or "")

    section("Hook registration")
    wired = _resolve_settings(cwd)
    required = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
    if not wired:
        bad("no settings.json wires Redi hooks",
            "run:  redi join redi://<token>@<host>:<port>")
    else:
        winner = wired[0]
        ok(f"hooks found in {winner[1]} settings: {winner[0]}")
        missing = required - winner[2]
        if missing:
            warn(f"missing events: {', '.join(sorted(missing))}")
        if len(wired) > 1:
            warn("also wired in: " + ", ".join(w[1] for w in wired[1:]))

    section("Configuration")
    print(f"  URL     {cfg['url']}")
    print(f"  mode    {cfg['mode']}")
    print(f"  token   {'from credentials/env' if cfg['token'] else '(none — open mode)'}")
    if root and os.path.exists(os.path.join(root, ".redi.toml")):
        ok(f".redi.toml found at {root}")

    section("Git / identity")
    repo_key = hook.repo_key_for(cwd)
    if not repo_key:
        bad("no git 'origin' remote — Redi scopes claims by remote URL",
            "add one:  git remote add origin <url>")
    else:
        ok(f"repo_key = {repo_key}   (branch {branch or '?'})")
    name = hook.git_display_name(cwd)
    (ok if name else warn)(f"display_name = {name!r}" if name else
                           "git user.name unset — you'll show as your machine id")
    mid = hook.machine_id()
    ok(f"machine_id = {mid}")

    section("Server")
    insecure = creds.warn_if_insecure(cfg["url"], hook._host_of(cfg["url"]))
    if insecure:
        warn(insecure)
    health, ms = _healthz(cfg["url"], cfg["timeout"])
    if not health:
        bad(f"cannot reach {cfg['url']}", "start one:  redi serve   (or fix COORD_URL)")
        return 1
    ok(f"reachable in {ms:.0f} ms — server {health.get('version', '?')}")
    vwarn = check_version(health.get("version", ""))
    if vwarn:
        warn(vwarn)
    if repo_key:
        act = _auth_check(cfg["url"], cfg["token"], cfg["timeout"])
        if act is None:
            bad("authorized request failed",
                "token mismatch — re-run `redi join` with the host's current URL")
        else:
            ok("auth accepted")
            real = hook._request("GET", f"{cfg['url']}/repos/{hook._q(repo_key)}/activity",
                                 None, cfg["token"], cfg["timeout"]) or {}
            claims = real.get("claims", [])
            sessions = {(c["machine_id"], c["session_id"]) for c in claims}
            print(f"       {len(claims)} live claim(s), {len(sessions)} session(s) here")
    print()
    return 0


def cmd_status(args) -> int:
    from . import hook
    cwd = os.getcwd()
    root, _ = hook.root_and_branch(cwd)
    cfg = hook.resolve_config(root or "")
    repo_key = hook.repo_key_for(cwd)
    if not repo_key:
        print("not a git repo with an 'origin' remote — nothing to show", file=sys.stderr)
        return 1
    act = hook._request("GET", f"{cfg['url']}/repos/{hook._q(repo_key)}/activity",
                        None, cfg["token"], cfg["timeout"])
    if act is None:
        print(f"could not reach {cfg['url']} (or auth failed) — try `redi doctor`",
              file=sys.stderr)
        return 1
    claims = act.get("claims", [])
    print(_c("1", f"Live claims on {repo_key}") + f"  ({len(claims)})")
    if not claims:
        print("  (none — no agent is currently editing a file here)")
    for c in sorted(claims, key=lambda c: c.get("age_seconds", 0)):
        who = c.get("display_name") or c.get("machine_id") or "unknown"
        age = hook._humanize_age(c.get("age_seconds") or 0)
        print(f"  {_c('1', c['file_path'])}  —  {who}  (branch {c.get('branch') or '?'}, {age})")
        intent = (c.get("intent") or "").strip()
        if intent:
            stale = (c.get("intent_age_seconds") or 0) > hook.STALE_INTENT_SECONDS
            print(f"        {'started from' if stale else 'working on'}: {intent}")
    contribs = act.get("contributors", [])
    if contribs:
        print("\n" + _c("1", "Reporting contributors (last 7d):"))
        for c in contribs:
            print(f"  {c['display_name']}  (last seen "
                  f"{hook._humanize_age(c.get('last_seen_seconds_ago') or 0)})")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Hot path: dispatch `redi hook <event>` before argparse to keep it cheap.
    if argv and argv[0] == "hook":
        from . import hook
        return hook.run(argv[1] if len(argv) > 1 else None)

    ap = argparse.ArgumentParser(prog="redi", description="Redi coordination CLI")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("join", help="connect + install hooks")
    p.add_argument("url", help="redi://<token>@<host>:<port>")
    p.add_argument("--settings", help="settings.json path (default ~/.claude/settings.json)")

    sub.add_parser("status", help="live claims for this repo")
    sub.add_parser("doctor", help="diagnose the local setup")

    p = sub.add_parser("serve", help="run a coordination server")
    p.add_argument("--local", action="store_true", help="zero-config localhost, open mode")

    sub.add_parser("print-join", help="print this server's join string")

    p = sub.add_parser("uninstall", help="remove Redi hooks (keeps credentials)")
    p.add_argument("--project", action="store_true", help="project settings instead of user")
    p.add_argument("--settings", help="explicit settings.json path")
    p.add_argument("--dry-run", action="store_true")

    # `redi hook <event>` also registered so `--help` lists it.
    p = sub.add_parser("hook", help="internal — invoked by Claude Code")
    p.add_argument("event", nargs="?")

    args = ap.parse_args(argv)
    dispatch = {
        "join": cmd_join, "status": cmd_status, "doctor": cmd_doctor,
        "serve": cmd_serve, "print-join": cmd_print_join, "uninstall": cmd_uninstall,
    }
    fn = dispatch.get(args.cmd)
    if not fn:
        ap.print_help()
        return 0
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
