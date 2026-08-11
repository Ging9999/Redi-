#!/usr/bin/env python3
"""Claude Code hook client for the Redi coordination server.

One script handles every lifecycle event; it dispatches on ``hook_event_name``:

    SessionStart      -> resolve session-stable values once, cache them
    UserPromptSubmit  -> POST the prompt as the session's (re)stated intent
    PreToolUse        -> one atomic acquire: check + stake; warn/ask/block/force
    PostToolUse       -> refresh the claim (debounced)
    Stop              -> release all claims for the session

Design commitments:

* **Fail open, always.** Server down, slow, bad git state, corrupt cache — the
  edit proceeds. Every network call has a short timeout; any unexpected
  exception exits 0.

* **The hot path is the product** (spec A0). ``PreToolUse``/``PostToolUse`` must
  be cheap. So: session-stable values (repo key, root, machine id, display name,
  config) are resolved once at ``SessionStart`` and cached to disk — the hot path
  never shells out to git (branch is re-checked lazily, >60s). Heavy imports
  (``urllib``, ``subprocess``, ``uuid``) are pulled in only on the path that
  needs them, and the quiet-backoff path makes no network call and imports none.

Configuration precedence: environment variable > ``.redi.toml`` at the repo root
> default. See README / ``hook/config.example.json``.

    COORD_URL, COORD_TOKEN, COORD_MODE (warn|ask|block), COORD_TIMEOUT,
    COORD_ENABLED, COORD_ID_FILE, COORD_CACHE_DIR, COORD_TTL_SECONDS,
    COORD_SUPPRESS_SECONDS, COORD_FORCE, COORD_FORCE_REASON
"""

from __future__ import annotations

# Only cheap stdlib at module scope. Everything expensive (urllib -> ssl/http/
# email, subprocess, uuid, pathlib) is imported lazily inside the function that
# needs it, so the quiet hot path pays for almost none of it (spec A1).
import json
import os
import sys

DEFAULT_URL = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT = 0.5
DEFAULT_TTL_SECONDS = 900
DEFAULT_SUPPRESS_SECONDS = 600     # B4: repeat-warning suppression window
STALE_INTENT_SECONDS = 1200        # B6: intent older than this is hedged
BRANCH_MAX_AGE_SECONDS = 60        # A1: re-resolve branch at most this often
VALID_MODES = ("warn", "ask", "block")


# --- small env helpers -------------------------------------------------------


def _env(name: str):
    v = os.environ.get(name)
    return v if v not in (None, "") else None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _normalize_mode(raw) -> str:
    mode = (raw or "").strip().lower()
    if mode not in VALID_MODES:
        if raw:
            sys.stderr.write(
                f"[redi] unknown COORD_MODE '{raw}', falling back to 'warn'\n"
            )
        return "warn"
    return mode


# --- git / identity (lazy subprocess; only SessionStart / bootstrap) ---------


def _run_git(args, cwd):
    import subprocess  # lazy
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=2.0
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def normalize_remote(url: str) -> str:
    """Normalize a git remote URL into a stable room key.

    ``git@github.com:acme/api.git`` and ``https://github.com/acme/api`` collapse
    to the same key: strip protocol/credentials, strip trailing ``.git``,
    lowercase.
    """
    u = url.strip()
    if u.startswith("git@") or ("@" in u.split("/")[0] and ":" in u and "://" not in u):
        u = u.split("@", 1)[1].replace(":", "/", 1)
    else:
        if "://" in u:
            u = u.split("://", 1)[1]
        if "@" in u.split("/")[0]:
            u = u.split("@", 1)[1]
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return u.strip("/").lower()


def repo_key_for(cwd: str):
    remote = _run_git(["remote", "get-url", "origin"], cwd)
    return normalize_remote(remote) if remote else None


def root_and_branch(cwd: str):
    """Repo root and branch in a single git call (fallback for commit-less repos)."""
    out = _run_git(["rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD"], cwd)
    if out:
        lines = out.splitlines()
        root = lines[0].strip() if lines else None
        branch = lines[1].strip() if len(lines) > 1 else ""
        return root, branch
    root = _run_git(["rev-parse", "--show-toplevel"], cwd)
    return (root or None), ""


def resolve_branch(cwd: str) -> str:
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or ""


def git_display_name(cwd: str) -> str:
    return _run_git(["config", "user.name"], cwd) or ""


def machine_id() -> str:
    """A stable per-machine id: a UUID persisted under COORD_ID_FILE.

    Only computed at SessionStart / bootstrap; the hot path reads it from cache.
    """
    import socket  # lazy
    id_file = _env("COORD_ID_FILE") or os.path.join(
        os.path.expanduser("~"), ".config", "agent-coordinator", "id"
    )
    try:
        if os.path.exists(id_file):
            with open(id_file, "r", encoding="utf-8") as fh:
                val = fh.read().strip()
                if val:
                    return val
        import uuid  # lazy
        os.makedirs(os.path.dirname(id_file), exist_ok=True)
        new_id = uuid.uuid4().hex
        with open(id_file, "w", encoding="utf-8") as fh:
            fh.write(new_id)
        return new_id
    except OSError:
        return socket.gethostname() or "unknown-machine"


def normalize_file_path(abs_path: str, root: str):
    """Convert an absolute tool path to a repo-relative, POSIX-separated path.

    The single most important correctness step: every machine's clone lives
    somewhere different, so reduce the path to one relative to the repo root, so
    the same file yields the same claim everywhere. Handles POSIX/Windows
    separators and rejects anything outside the repo root. Pure string ops — no
    ``pathlib`` import on the hot path.
    """
    if not abs_path or not root:
        return None
    ap = abs_path.replace("\\", "/").rstrip("/")
    rp = root.replace("\\", "/").rstrip("/")
    if ap == rp:
        return None
    prefix = rp + "/"
    if ap.startswith(prefix):
        rel = ap[len(prefix):]
    elif ap.lower().startswith(prefix.lower()):
        # Windows drive-letter case differences; separators already normalized.
        rel = ap[len(prefix):]
    else:
        return None
    if not rel or rel.startswith("..") or "/../" in rel:
        return None
    return rel


# --- config (.redi.toml + env, spec B7) --------------------------------------


def _parse_toml(text: str) -> dict:
    """Tiny TOML reader for the handful of keys we support. Uses stdlib tomllib
    when available (3.11+), else a minimal ``key = value`` fallback."""
    try:
        import tomllib  # 3.11+
        data = tomllib.loads(text)
        # Accept either top-level keys or a [redi] table.
        flat = dict(data)
        if isinstance(data.get("redi"), dict):
            flat.update(data["redi"])
        return flat
    except Exception:  # noqa: BLE001 — fall back to the minimal parser
        pass
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v.replace(".", "", 1).isdigit():
            v = float(v) if "." in v else int(v)
        out[k] = v
    return out


def load_toml_config(root: str) -> dict:
    if not root:
        return {}
    path = os.path.join(root, ".redi.toml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return _parse_toml(fh.read())
    except (OSError, ValueError):
        return {}


def _host_of(url: str) -> str:
    try:
        from . import creds
        return creds.parse_join_url(url)["host"]
    except Exception:  # noqa: BLE001
        return ""


def _resolve_token(url: str, toml: dict):
    """Token precedence: env > .redi.toml (opt-in, see spec D) > saved
    credentials keyed by host. Credentials keep the secret out of the repo."""
    env_tok = _env("COORD_TOKEN")
    if env_tok:
        return env_tok
    if toml.get("token"):
        return toml["token"]
    host = _host_of(url)
    if host:
        try:
            from . import creds
            cred = creds.get_credential(host)
            if cred and cred.get("token"):
                return cred["token"]
        except Exception:  # noqa: BLE001
            pass
    return None


def resolve_config(root: str) -> dict:
    """Full config resolution (env > .redi.toml > saved credentials > default).
    Used at SessionStart and by the CLI; the hot path reads the cached result and
    only re-applies env overrides (see ``_hot_config``)."""
    toml = load_toml_config(root)

    def pick(env_name, toml_key, default):
        return _env(env_name) or toml.get(toml_key) or default

    url = str(pick("COORD_URL", "url", DEFAULT_URL)).rstrip("/")
    return {
        "url": url,
        "token": _resolve_token(url, toml),
        "mode": _normalize_mode(pick("COORD_MODE", "mode", "warn")),
        "timeout": float(pick("COORD_TIMEOUT", "timeout", DEFAULT_TIMEOUT)),
        "ttl_seconds": int(pick("COORD_TTL_SECONDS", "ttl_seconds", DEFAULT_TTL_SECONDS)),
    }


def _hot_config(cache: dict) -> dict:
    """Config for the hot path: cached values, with env still overriding."""
    cfg = dict(cache.get("config") or {})
    url = (_env("COORD_URL") or cfg.get("url") or DEFAULT_URL).rstrip("/")
    return {
        "url": url,
        "token": _env("COORD_TOKEN") or cfg.get("token") or None,
        "mode": _normalize_mode(_env("COORD_MODE") or cfg.get("mode") or "warn"),
        "timeout": float(_env("COORD_TIMEOUT") or cfg.get("timeout") or DEFAULT_TIMEOUT),
        "ttl_seconds": int(_env("COORD_TTL_SECONDS") or cfg.get("ttl_seconds") or DEFAULT_TTL_SECONDS),
    }


# --- session cache -----------------------------------------------------------


def _cache_dir() -> str:
    base = _env("COORD_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "redi"
    )
    return os.path.join(base, "sessions")


def _cache_path(session_id: str) -> str:
    safe = session_id.replace(os.sep, "_").replace("..", "_") or "unknown"
    return os.path.join(_cache_dir(), f"{safe}.json")


def load_cache(session_id: str) -> dict:
    try:
        with open(_cache_path(session_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(session_id: str, cache: dict) -> None:
    path = _cache_path(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # cache is an optimization; never fail the edit over it


def build_session_cache(session_id: str, cwd: str) -> dict:
    """Resolve every session-stable value once (the SessionStart / bootstrap job)."""
    import time
    repo_key = repo_key_for(cwd)
    root, branch = root_and_branch(cwd)
    return {
        "session_id": session_id,
        "cwd": cwd,
        "repo_key": repo_key,
        "root": root,
        "branch": branch,
        "branch_checked_at": time.time(),
        "display_name": git_display_name(cwd),
        "machine_id": machine_id(),
        "config": resolve_config(root or ""),
        "quiet_until": 0,
        "refresh": {},
        "warned": {},
    }


# --- HTTP (lazy urllib) ------------------------------------------------------


def _request(method, url, payload, token, timeout):
    import urllib.error  # lazy
    import urllib.request
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None  # fail open


def _post(cfg, path, payload):
    return _request("POST", f"{cfg['url']}{path}", payload, cfg["token"], cfg["timeout"])


# --- message formatting (B6 staleness + B8 the actual product) ---------------


def _humanize_age(seconds) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago" if seconds > 5 else "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


def _format_conflict_message(conflicts, mode: str = "warn") -> str:
    """The message the agent reads and acts on — the entire value proposition.

    Carries who + where, what they're doing (hedged if the intent is stale, B6),
    how long ago, and an explicit statement that this is advisory and how to
    proceed anyway (B8).
    """
    n = len(conflicts)
    lines = [
        "Another agent is editing this file."
        if n == 1
        else f"{n} other agents are editing this file."
    ]
    for c in conflicts:
        who = c.get("display_name") or c.get("machine_id") or "unknown"
        branch = c.get("branch") or "(unknown branch)"
        ago = _humanize_age(c.get("age_seconds") or 0)
        line = f"  - {who} (branch `{branch}`), started {ago}"
        intent = (c.get("intent") or "").strip()
        if intent:
            intent_age = c.get("intent_age_seconds")
            if intent_age is not None and intent_age > STALE_INTENT_SECONDS:
                line += (
                    f'\n    started from: "{intent}"'
                    f"  (stated {_humanize_age(intent_age)} — may be stale)"
                )
            else:
                line += f'\n    working on: "{intent}"'
        lines.append(line)
    lines.append(
        "\nThis is advisory — Redi does not lock files. Your options: wait for "
        "them to finish, edit a different file, coordinate, or proceed anyway by "
        "retrying the edit if your change is independent."
    )
    if mode == "block":
        lines.append(
            "[block mode] To proceed despite this, re-run with COORD_FORCE=1 "
            "(set COORD_FORCE_REASON to leave the other agent a note)."
        )
    return "\n".join(lines)


def _format_overrides(overrides) -> str:
    lines = []
    for o in overrides:
        who = o.get("overrider_name") or "another agent"
        f = o.get("file_path") or "a file"
        reason = (o.get("reason") or "").strip()
        msg = f"[redi] {who} overrode your claim on `{f}`"
        msg += f": {reason}" if reason else "."
        lines.append(msg)
    return "\n".join(lines)


# --- event context -----------------------------------------------------------


class Ctx:
    def __init__(self, event: dict):
        self.event = event
        self.session_id = event.get("session_id") or ""
        self.cwd = event.get("cwd") or os.getcwd()
        self.tool_input = event.get("tool_input") or {}
        self.prompt = event.get("prompt") or ""

    def file_path(self):
        return self.tool_input.get("file_path") or self.tool_input.get("filePath")


def _get_cache(ctx: Ctx) -> dict:
    """Load the session cache, bootstrapping it from git if SessionStart never
    ran (self-heal). Bootstrap is the only hot-path git use, and it happens at
    most once per session."""
    cache = load_cache(ctx.session_id)
    if cache.get("repo_key") and cache.get("root"):
        return cache
    cache = build_session_cache(ctx.session_id, ctx.cwd)
    save_cache(ctx.session_id, cache)
    return cache


def _maybe_refresh_branch(ctx: Ctx, cache: dict) -> None:
    """Branch is the one session value that can change mid-session (checkout).
    Re-resolve it lazily, only when the cached value is stale (spec A1)."""
    import time
    if time.time() - (cache.get("branch_checked_at") or 0) < BRANCH_MAX_AGE_SECONDS:
        return
    branch = resolve_branch(cache.get("cwd") or ctx.cwd)
    cache["branch"] = branch
    cache["branch_checked_at"] = time.time()


# --- handlers ----------------------------------------------------------------


def handle_session_start(ctx: Ctx) -> int:
    cache = build_session_cache(ctx.session_id, ctx.cwd)
    save_cache(ctx.session_id, cache)
    _coverage_nudge(ctx, cache)
    return 0


def _coverage_nudge(ctx: Ctx, cache: dict) -> None:
    """Note when this repo has had other recent git contributors who aren't
    reporting Redi claims — a partial rollout looks identical to a working one
    (spec B2). Best-effort, once per session, to stderr."""
    try:
        repo_key = cache.get("repo_key")
        root = cache.get("root")
        if not repo_key or not root:
            return
        authors = _run_git(
            ["log", "--since=14.days", "--format=%an"], root
        )
        if not authors:
            return
        git_authors = {a.strip() for a in authors.splitlines() if a.strip()}
        me = cache.get("display_name") or ""
        git_authors.discard(me)
        if not git_authors:
            return
        cfg = _hot_config(cache)
        resp = _request(
            "GET", f"{cfg['url']}/repos/{_q(repo_key)}/activity?days=14",
            None, cfg["token"], cfg["timeout"],
        )
        reporting = {me}
        if resp:
            reporting |= {c.get("display_name", "") for c in resp.get("contributors", [])}
        missing = sorted(git_authors - reporting)
        if missing:
            shown = ", ".join(missing[:3]) + (" …" if len(missing) > 3 else "")
            sys.stderr.write(
                f"[redi] {len(missing)} recent contributor(s) to this repo are not "
                f"reporting edits ({shown}). Redi only prevents collisions if "
                f"everyone runs it — consider committing .claude/settings.json.\n"
            )
    except Exception:  # noqa: BLE001 — a nudge must never disrupt startup
        pass


def _q(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def handle_user_prompt_submit(ctx: Ctx) -> int:
    """(Re)state the session's intent on every prompt, not just the first (B6)."""
    if not ctx.session_id or not ctx.prompt:
        return 0
    cache = load_cache(ctx.session_id) or {}
    cfg = _hot_config(cache)
    _post(cfg, f"/sessions/{_q(ctx.session_id)}/intent", {"intent": ctx.prompt})
    return 0


def handle_pre_tool_use(ctx: Ctx) -> int:
    import time
    fp = ctx.file_path()
    if not fp:
        return 0
    cache = _get_cache(ctx)
    repo_key, root = cache.get("repo_key"), cache.get("root")
    if not repo_key or not root:
        return 0
    rel = normalize_file_path(fp, root)
    if not rel:
        return 0

    # A3: solo-session backoff — inside the quiet window, make no network call
    # (and import no urllib). Worst case is up to one window of unwarned edits
    # when a teammate starts up; that tradeoff is documented.
    now = time.time()
    if (cache.get("quiet_until") or 0) > now:
        return 0

    _maybe_refresh_branch(ctx, cache)
    cfg = _hot_config(cache)

    # A2: one atomic request that checks conflicts and stakes our claim.
    result = _post(cfg, "/claims/acquire", {
        "repo_key": repo_key,
        "file_path": rel,
        "session_id": ctx.session_id,
        "machine_id": cache.get("machine_id", ""),
        "display_name": cache.get("display_name", ""),
        "branch": cache.get("branch", ""),
    })
    if result is None:
        return 0  # fail open; leave quiet/refresh untouched

    # We just staked at `rel`, so its refresh clock starts now (feeds A4).
    cache.setdefault("refresh", {})[rel] = now
    cache["quiet_until"] = result.get("quiet_until") or 0

    overrides = result.get("overrides") or []
    if overrides:
        sys.stderr.write(_format_overrides(overrides) + "\n")

    conflicts = result.get("conflicts") or []
    if not conflicts:
        save_cache(ctx.session_id, cache)
        return 0

    # B4: suppress a warning we already delivered for the same (file, session)
    # unless the other agent's intent has changed (genuinely new information).
    fresh = _unsuppressed(cache, rel, conflicts, now)

    # B5: explicit override path — proceed and record a note the other agent
    # sees on its next check, rather than silently overwriting.
    if _env_bool("COORD_FORCE", False):
        reason = _env("COORD_FORCE_REASON") or ""
        for c in conflicts:
            _post(cfg, "/claims/override", {
                "repo_key": repo_key, "file_path": rel,
                "target_session": c.get("session_id", ""),
                "overrider_name": cache.get("display_name", ""),
                "reason": reason,
            })
        sys.stderr.write(
            f"[redi] COORD_FORCE set — proceeding and notifying "
            f"{len(conflicts)} other agent(s).\n"
        )
        save_cache(ctx.session_id, cache)
        return 0

    if not fresh:
        save_cache(ctx.session_id, cache)  # all suppressed — proceed silently
        return 0

    _mark_warned(cache, rel, fresh, now)
    save_cache(ctx.session_id, cache)

    message = _format_conflict_message(fresh, cfg["mode"])
    if cfg["mode"] == "ask":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": message,
            }
        }))
        return 0
    sys.stderr.write(message + "\n")
    return 2


def _warn_key(rel: str, session: str) -> str:
    return f"{rel}\x00{session}"


def _unsuppressed(cache, rel, conflicts, now):
    warned = cache.get("warned") or {}
    suppress = float(_env("COORD_SUPPRESS_SECONDS") or DEFAULT_SUPPRESS_SECONDS)
    out = []
    for c in conflicts:
        key = _warn_key(rel, c.get("session_id", ""))
        prev = warned.get(key)
        if (
            prev
            and (now - prev.get("at", 0)) < suppress
            and prev.get("intent", None) == (c.get("intent") or "")
        ):
            continue  # already warned recently about this exact situation
        out.append(c)
    return out


def _mark_warned(cache, rel, conflicts, now):
    warned = cache.setdefault("warned", {})
    for c in conflicts:
        warned[_warn_key(rel, c.get("session_id", ""))] = {
            "at": now, "intent": (c.get("intent") or "")
        }


def handle_post_tool_use(ctx: Ctx) -> int:
    """Refresh the claim, debounced: TTL is minutes, edits are seconds, so most
    refreshes are wasted work (spec A4). Skip entirely while quiet (A3)."""
    import time
    fp = ctx.file_path()
    if not fp:
        return 0
    cache = _get_cache(ctx)
    repo_key, root = cache.get("repo_key"), cache.get("root")
    if not repo_key or not root:
        return 0
    rel = normalize_file_path(fp, root)
    if not rel:
        return 0

    now = time.time()
    if (cache.get("quiet_until") or 0) > now:
        return 0  # solo repo: claim stays fresh via periodic acquire

    cfg = _hot_config(cache)
    interval = cfg["ttl_seconds"] / 3.0
    last = (cache.get("refresh") or {}).get(rel, 0)
    if now - last < interval:
        return 0  # claim is still comfortably fresh — do nothing

    _maybe_refresh_branch(ctx, cache)
    _post(cfg, "/claims", {
        "repo_key": repo_key,
        "file_path": rel,
        "session_id": ctx.session_id,
        "machine_id": cache.get("machine_id", ""),
        "display_name": cache.get("display_name", ""),
        "branch": cache.get("branch", ""),
    })
    cache.setdefault("refresh", {})[rel] = now
    save_cache(ctx.session_id, cache)
    return 0


def handle_stop(ctx: Ctx) -> int:
    """Release all of this session's claims and drop its cache."""
    if not ctx.session_id:
        return 0
    cache = load_cache(ctx.session_id) or {}
    cfg = _hot_config(cache)
    _post(cfg, f"/sessions/{_q(ctx.session_id)}/release", {})
    try:
        os.remove(_cache_path(ctx.session_id))
    except OSError:
        pass
    return 0


HANDLERS = {
    "SessionStart": handle_session_start,
    "UserPromptSubmit": handle_user_prompt_submit,
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
    "Stop": handle_stop,
}

# Kebab aliases for the `redi hook <event>` CLI form (spec B2), so a committed
# settings.json can use PATH-resolved commands like `redi hook pre-tool-use`.
_KEBAB = {
    "session-start": "SessionStart",
    "user-prompt-submit": "UserPromptSubmit",
    "pre-tool-use": "PreToolUse",
    "post-tool-use": "PostToolUse",
    "stop": "Stop",
}


def run(event_name: str | None = None) -> int:
    """Read the event JSON from stdin and dispatch. ``event_name`` (from the
    ``redi hook <event>`` arg) wins; otherwise fall back to the JSON's
    ``hook_event_name``. Fails open on everything."""
    if not _env_bool("COORD_ENABLED", True):
        return 0
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            return 0
    except (json.JSONDecodeError, ValueError):
        return 0

    name = _KEBAB.get((event_name or "").strip().lower())
    name = name or event.get("hook_event_name") or ""
    handler = HANDLERS.get(name)
    if not handler:
        return 0
    try:
        return handler(Ctx(event))
    except Exception as exc:  # noqa: BLE001 — fail open on anything unexpected.
        sys.stderr.write(f"[redi] non-fatal error (failing open): {exc}\n")
        return 0


def main() -> int:
    # `python3 -m redi.hook [event]` — event optional, falls back to stdin.
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    sys.exit(main())
