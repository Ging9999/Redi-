# Redi — full feature inventory

The [README](README.md) is the front door (what it does, the two flows). This is
the complete reference. Built to the specs in `docs/agentcoordinationmvp.md`
(v1), `docs/rediv1_1efficiencyux.md` (v1.1), `docs/rediv1_2distribution.md`
(v1.2). Python 3.9+, **stdlib only, zero dependencies**.

## Architecture

| Piece | Module | Role |
|---|---|---|
| Coordination server | `redi.server` + `redi.store` | Single-process HTTP service; claims registry in SQLite (WAL). |
| Hook client | `redi.hook` | Runs on each Claude Code lifecycle event (`redi hook <event>`). |
| CLI | `redi.cli` | `join` / `status` / `doctor` / `serve` / `uninstall` / `print-join`. |
| Credentials & join URLs | `redi.creds` | Token gen, `redi://` URLs, `~/.config/redi/credentials`, host classification. |
| Installer | `redi.install` | PATH-resolved hook merge/uninstall for `settings.json`. |

### Room key — normalized git remote

Claims are scoped by the normalized git remote URL, not local path.
`git@github.com:acme/api.git` and `https://github.com/acme/api` collapse to
`github.com/acme/api` (strip protocol + credentials, strip `.git`, lowercase).
Branch is metadata, not part of the key — two agents on different branches
touching the same file is exactly the case worth flagging.

### File-path normalization

The hook receives a machine-specific absolute path; every clone lives elsewhere.
It's reduced to a **repo-relative, POSIX-separated** path, so the same file
yields the same claim on every machine. POSIX and Windows separators normalize
identically; anything outside the repo root is rejected. Pure string ops — no
`pathlib` import on the hot path.

### Agent identity

`(machine_id, session_id)`: `session_id` from the hook's stdin JSON;
`machine_id` a UUID persisted at `~/.config/agent-coordinator/id` (hostname
fallback); `display_name` from `git config user.name`. Two sessions on one
machine are two agents.

## Hook events

| Event | Matcher | Action |
|---|---|---|
| `SessionStart` | — | Resolve session-stable values once → `~/.cache/redi`; enables the fast path. |
| `UserPromptSubmit` | — | POST the prompt as the session's intent, every prompt. |
| `PreToolUse` | `Edit\|Write` | One atomic `acquire`: check + stake; warn/ask/block/force. Skipped while quiet. |
| `PostToolUse` | `Edit\|Write` | Refresh the claim, debounced to once per TTL/3. |
| `Stop` | — | Release the session's claims; drop its cache. |

Commands are **PATH-resolved** (`redi hook pre-tool-use`) so a committed
`settings.json` is portable and degrades to a no-op where Redi isn't installed.

## Efficiency (the hot path is the product)

- **Zero subprocess on the steady-state edit path** — git state is cached at
  `SessionStart`; branch re-resolved lazily (>60s).
- **Lazy imports** — `urllib`/`subprocess`/`uuid`/`pathlib` load only where used.
- **One atomic request** — `POST /claims/acquire` checks and stakes together.
- **Solo-repo backoff** — a `quiet_until` window means most edits make no request
  at all (a 50-edit solo run makes ~1).
- **Refresh debounce** — `PostToolUse` refreshes at most once per TTL/3.
- **SQLite tuning** — WAL + `synchronous=NORMAL`; indexes on every hot query.

Result: `PreToolUse` p50 89ms → 32ms. Full numbers, and the (negative)
local-daemon decision, in [`docs/PERF.md`](docs/PERF.md).

## Coordination quality

- **Intent, not presence** — every claim carries a natural-language intent; the
  warning is the product.
- **Repeat-warning suppression** — a `(file, other-session)` warning is silenced
  for `COORD_SUPPRESS_SECONDS` (10 min) unless the intent changes.
- **Explicit override** — `COORD_FORCE=1` (+ `COORD_FORCE_REASON`) proceeds and
  records a note the other agent sees, instead of a silent overwrite.
- **Intent staleness** — intent re-captured every prompt; the message hedges
  anything older than ~20 min ("started from:" vs "working on:").
- **Coverage** — `activity` lists reporting contributors; `SessionStart` notes
  recent git authors who aren't reporting.

## Response modes (`COORD_MODE`)

- `warn` (default) — on conflict, exit 2 with the message on stderr; Claude Code
  feeds it back to the agent, which decides. Exit 2 is the only thing that blocks
  a `PreToolUse`.
- `ask` — exit 0 with `permissionDecision: "ask"`, escalating to the human.
- `block` — advisory lock; overridable with `COORD_FORCE`.

## API

| Method + path | Returns |
|---|---|
| `POST /claims/acquire` | `{conflicts, claim, quiet_until, overrides}` — the hot path. |
| `POST /claims/check` | `{conflicts}` — read-only. |
| `POST /claims` | `{claim}` — register/refresh. |
| `POST /claims/override` | `{ok}` — record an override note. |
| `POST /sessions/{id}/intent` | `{ok}` |
| `POST /sessions/{id}/release` | `{released}` |
| `GET /repos/{key}/activity?days=N` | `{claims, contributors}`; `If-None-Match`/`304`. |
| `GET /healthz` | `{ok, version}` — no auth. |

Auth is a single bearer token, compared in constant time. Claims carry
`age_seconds`, `expires_in_seconds`, `intent_age_seconds`. Server timestamps
only. Intent capped at 2000 chars.

## Claim lifecycle

15-minute TTL; refreshed (debounced) as an agent works; `created_at` preserved.
`Stop` releases. Expired claims swept lazily on read and by a background sweeper.

## Distribution

- **PyPI** `redi-coordinator`, console entry `redi` (`uv tool install` / `pipx`).
- **Single-file `redi.pyz`** (zipapp) attached to releases — any Python 3.9+.
- **Docker Compose** + multi-arch (`amd64`/`arm64`) GHCR image.
- **First-run token generation** persisted next to the DB; join string on every
  startup; `redi print-join`.
- **`redi join`** — atomic connect + auth + version check + credential write +
  hook install, with rollback.

## Credentials & config (split by secrecy)

- `.redi.toml` (committed) — URL, mode, timeouts. No secrets.
- `~/.config/redi/credentials` (0600, never committed) — tokens keyed by host.

### DECIDE — token in `.redi.toml` (spec D)

**Offered, but off by default and opt-in.** A token in a committed `.redi.toml`
makes onboarding a one-step clone-and-go with no join command — genuinely
compelling for teams that treat the token as low-value metadata. The cost:
**anyone with repo read access can read and write claims.** So Redi honors a
committed token **only** when the file also sets `allow_committed_token = true`;
without that explicit key a committed `token` is ignored. Recommend against as
the default; the argument for offering it is real, so it's available behind a
deliberate opt-in.

## Security & TLS

The server speaks plain HTTP; the client warns on a plain-`http://` join to a
public host. Recommended: Tailscale (private, no certs) or a Caddy reverse proxy
(automatic TLS). See [`docs/HOSTING.md`](docs/HOSTING.md).

## Known limitations

- **Bash-driven edits** (`sed`, codemods, formatters via `Bash`) bypass the
  `Edit|Write` matcher — not solved, by design (large, error-prone surface).
- **Residual simultaneous-start race** — `acquire` shrinks the window to one
  request but doesn't eliminate it; Redi is advisory, not a lock.
- **Batch turns** — no per-turn batch hook event exists to coalesce multi-file
  turns; A3/A4 already remove most of that traffic.
