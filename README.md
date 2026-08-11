# Cross-Machine Agent Edit Coordination

A coordination layer so Claude Code agents running on different developers'
machines know when another agent is already working on the same file — and,
crucially, **what** that agent is doing — so they can adapt instead of colliding
at merge time.

The differentiator is **intent, not presence**. Prior art (GitLive, GitKraken
Team View, FASTDash) only ever signalled "someone is here." Here, each agent
states in natural language what it's changing, and another agent can read that
statement and decide for itself: wait, work elsewhere, coordinate, or proceed.

> Another agent is editing this file.
> Sam's session is on branch `feat/rate-limit`, started 4 minutes ago
> working on: "add per-IP rate limiting to the auth middleware."

This is an MVP built to the spec in `docs/agentcoordinationmvp.md`.

---

## Architecture

Three pieces (spec section 2), **Python 3 stdlib only — no dependencies**:

| Piece | File | Role |
|---|---|---|
| Coordination server | `server/coordinator.py` + `server/store.py` | Single-process HTTP service holding the claims registry, SQLite-backed. |
| Hook client | `hook/coordinator_hook.py` | One script wired into Claude Code lifecycle events on each machine. |
| Activity endpoint | `GET /repos/{repo_key}/activity` | JSON view of all live claims, for debugging / a later dashboard. |

Zero pip installs is a deliberate choice: a coordination tool has to be
installed on every developer's machine, so the friction of dependencies is a
real adoption cost.

### Room key — normalized git remote

Claims are scoped by the **normalized git remote URL**, not local path (spec
section 2). `git@github.com:acme/api.git` and `https://github.com/acme/api`
collapse to the same key `github.com/acme/api` (strip protocol + credentials,
strip trailing `.git`, lowercase). Branch is stored as *metadata*, not part of
the key — two agents on different branches touching the same file is exactly the
case worth flagging.

### File path normalization — the thing that usually breaks

The hook receives a machine-specific **absolute** path. Every clone lives
somewhere different, so before sending anything we reduce the path to
**repo-relative, POSIX-separated** using `git rev-parse --show-toplevel`.
Windows and POSIX separators normalize to the same claim, and any path that
escapes the repo root is rejected. This is the single most important
correctness step (spec section 2) and has dedicated tests in
`tests/test_hook.py`.

### Agent identity

Identity is `(machine_id, session_id)` (spec section 2):

- `session_id` — from the hook's stdin JSON
- `machine_id` — a UUID persisted at `~/.config/agent-coordinator/id` (falls
  back to hostname if that can't be written)
- `display_name` — `git config user.name`

One developer may run several concurrent sessions; each is a distinct agent, so
a second session on the same machine editing the same file **is** flagged.

---

## Quick start

### 1. Run the server

```bash
export COORD_TOKEN="pick-a-shared-secret"      # optional; omit for open dev mode
python3 server/coordinator.py
# listening on http://127.0.0.1:8787  db=coordinator.db  ttl=900s  token auth ENABLED
```

Server configuration (all env vars):

| Var | Default | Meaning |
|---|---|---|
| `COORD_HOST` | `127.0.0.1` | Bind address (use `0.0.0.0` behind a tunnel/VPS). |
| `COORD_PORT` | `8787` | Port. |
| `COORD_DB` | `coordinator.db` | SQLite path, or `:memory:`. |
| `COORD_TTL_SECONDS` | `900` | Claim TTL (15 min). |
| `COORD_TOKEN` | *(unset)* | Bearer token. Unset = open mode (dev only). |

### 2. Wire the hook into Claude Code

Use the installer — it merges the four hooks and the `COORD_*` env block into
your `.claude/settings.json`, preserving anything already there, and is
idempotent:

```bash
# project-level (./.claude/settings.json), local server:
python3 hook/install.py

# user-level, shared server with a token:
python3 hook/install.py --user --url https://coord.example.com --token SECRET

# preview the merge without writing:
python3 hook/install.py --dry-run
```

Prefer to do it by hand? Copy the `env` and `hooks` blocks from
`examples/settings.json` and set the absolute path to `coordinator_hook.py`.
Hook environment variables are documented in `hook/config.example.json`.

**Matchers are case-sensitive** (spec section 3): `Edit|Write` matches the Edit
and Write tools; `edit` matches nothing.

### 3. Try it with curl

```bash
./tests/smoke.sh          # exercises every endpoint against a running server
```

### Run the server in Docker (for the two-machine test)

```bash
docker build -t agent-coordinator .
docker run -e COORD_TOKEN=secret -p 8787:8787 -v coord-data:/data agent-coordinator
```

The image is stdlib-only (no pip install), persists the SQLite store on the
`/data` volume, and has a built-in healthcheck.

---

## Hook wiring (spec section 3)

| Event | Matcher | What the hook does |
|---|---|---|
| `UserPromptSubmit` | — | POST the user's prompt as the session's intent. |
| `PreToolUse` | `Edit\|Write` | Check for conflicts **before** the edit; warn/ask/block — and, if clear, **stake the claim immediately** (see below). |
| `PostToolUse` | `Edit\|Write` | Register/refresh the claim on the edited file. |
| `Stop` | — | Release all claims for the session. |

**Staking at `PreToolUse` narrows the race.** A naive design only *checks* at
`PreToolUse` and doesn't record the claim until `PostToolUse` (after the edit) —
so two agents starting within the same moment both check-clear and then collide.
Instead, the instant an agent's check comes back clean, it stakes its claim,
before the edit runs. The next agent's check a moment later sees it.
`PostToolUse` then refreshes. This shrinks the collision window to the single
check round-trip; it doesn't eliminate it (see Known limitations).

Mechanics we deliberately got right:

- **`PreToolUse` is the only event that can stop a tool call.** On a conflict in
  the default `warn` mode the hook exits 2 with the message on stderr; Claude
  Code feeds that stderr back to the agent as the reason.
- **JSON decisions are only parsed on exit 0.** The `ask` path prints its
  decision JSON and exits 0; the `warn`/`block` paths never print a decision
  before exiting 2 (which would discard it).
- **Short timeout, fail open.** Every request has a `COORD_TIMEOUT` (default
  500ms) budget. Server down, slow, or unreachable → the hook exits 0 and the
  edit proceeds. A coordination tool that stalls the agent gets uninstalled
  within a day, so this is non-negotiable.

---

## API (spec section 5)

| Method + path | Body | Returns |
|---|---|---|
| `POST /claims/check` | `{repo_key, file_path, session_id, machine_id}` | `{conflicts: [Claim]}` — excludes the caller's own claims. |
| `POST /claims` | claim fields (+ optional `intent`) | `{claim: Claim}` — register or refresh, extending `expires_at`. |
| `POST /sessions/{session_id}/intent` | `{intent}` | `{ok: true}` |
| `POST /sessions/{session_id}/release` | — | `{released: N}` |
| `GET /repos/{repo_key}/activity` | — | `{claims: [Claim]}` |
| `GET /healthz` | — | `{ok: true}` (no auth) |

Auth is a single bearer token in `COORD_TOKEN` (spec section 5), compared in
constant time. Each `Claim` includes `age_seconds` and `expires_in_seconds` so
the reading agent can judge how long the other agent has been at it and how long
its claim will last, without doing clock math. `/healthz` reports the server
`version` and requires no auth (for load-balancer / container health checks).

## Claim lifecycle (spec section 6)

- Claims carry a **15-minute TTL**.
- `PostToolUse` refreshes on every edit, so an active agent keeps its claim
  alive; `created_at` is preserved across refreshes.
- `Stop` releases explicitly.
- Expired claims are **swept lazily on read**, and a background sweeper runs
  every ~TTL/2 so an idle repo's store doesn't grow unbounded either — a crashed
  session (SIGKILL) never holds a file hostage.
- All timestamps are **server-side**; client clocks are never trusted.
- Intent strings are length-capped server-side (2000 chars) so a giant prompt
  can't bloat rows or over-share what's echoed to other machines.

---

## Resolved design decisions

The spec left three points open (**DECIDE**). Here's how they were resolved and
why.

### DECIDE — self-authored intent via an MCP tool (section 3)

**Resolution: ship prompt-capture in v1; the plumbing already supports a
self-authored `declare_intent`, so adding it later is a small step.**

The `UserPromptSubmit` hook ships the user's prompt as the session's intent —
zero extra work for the developer, and usually a decent description of what the
agent is about to do. A dedicated MCP `declare_intent` tool would let the agent
write a more precise, self-authored statement, which is likely better, but it
costs a tool call and adds MCP-server plumbing to install.

Rather than pick one, the server exposes `POST /sessions/{id}/intent` as a
first-class endpoint that *anything* can call — the hook today, an MCP tool
tomorrow. So the value ("intent is a live, updatable string, denormalized onto
claims as they're created") is available now, and upgrading to self-authored
intent is a thin client on an endpoint that already exists. That's the right
MVP tradeoff: capture the cheap 80% now, leave a clean seam for the better 20%.

### DECIDE — warn as exit-2 block vs `permissionDecision: "ask"` (section 7)

**Resolution: default `warn` = exit 2 with a rich stderr message. `ask` and
`block` exist behind `COORD_MODE`, off by default.**

The core thesis of this tool is that the *agent* adapts based on another
agent's intent (spec sections 1 and 11 — the success criterion is that the
second agent "behaves differently and better"). Exit 2 blocks *this* tool call
and feeds the reason back to Claude, so the agent reads who else is on the file,
their branch, their intent, and how long ago they started, and then decides:
wait, work elsewhere, coordinate, or deliberately retry. That keeps the decision
with the agent.

`permissionDecision: "ask"` escalates to the **human** instead — which
contradicts the "agents only" goal (non-goal: no human-facing surface in v1)
and adds friction to every conflicting edit. It's genuinely useful for a cautious
team, so it's available via `COORD_MODE=ask`, but it's not the default.

`block` mode uses the same exit-2 mechanism as `warn` with a firmer message; it
is a soft advisory lock, still overridable by a deliberate retry, because hard
locking will deadlock a team's agents fast (spec section 7).

### DECIDE — HTTP hooks vs a thin local script (section 3)

**Resolution: a thin local script (`coordinator_hook.py`), not direct HTTP
hooks.**

A direct `{"type": "http"}` hook can't run `git` to derive the repo key, the
repo root, or the branch, and can't normalize the absolute file path to
repo-relative — the one step that matters most. All of that is local, per-machine
context that only a local process can gather. The script also owns the fail-open
timeout budget and the exit-code contract precisely. HTTP hooks remain a fine
option for a future zero-install mode once path normalization can be pushed
server-side, but for v1 the local script is the correct call.

---

## Known limitation: Bash-driven edits (spec section 8)

Agents don't only edit through `Edit`/`Write`. They also run `sed`, `python`,
codemods, and formatters through `Bash`, and those **bypass the `Edit|Write`
matcher entirely** — no claim is checked or registered for them.

This is **not solved in v1**, by design. Matching `Bash` and parsing arbitrary
commands for the file paths they touch is a large surface area that's easy to
get wrong (a mis-parse either misses a real edit or blocks an unrelated
command). It's noted here as a documented gap and a possible v2 direction.

### Residual simultaneous-start race

Staking the claim at `PreToolUse` (above) shrinks but does not eliminate the
window: two agents whose `PreToolUse` checks land in the same instant can both
see no claim before either has staked. This is inherent to an advisory,
check-then-act model without a lock, and acceptable for v1 — the tool warns and
lets agents adapt; it does not guarantee mutual exclusion (that's a non-goal).
A future version could close it with an atomic check-and-claim (a conditional
insert that returns the existing claim if one appears first).

---

## Testing

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

Coverage maps to the spec's section-10 scenarios:

| Scenario (spec section 10) | Test |
|---|---|
| Same file, same branch → conflict | `test_store`, `test_server` |
| Same file, different branches → conflict | `test_store.test_same_file_different_branch_is_conflict` |
| Different files → no conflict | `test_store`, `test_e2e_hook` |
| Same agent re-editing own file → no self-conflict | `test_e2e_hook.test_self_is_not_a_conflict` |
| Session SIGKILL → claim expires | `test_store.test_expired_claim_is_swept` |
| Server unreachable → fail open | `test_e2e_hook.test_server_unreachable_fails_open` |
| **Server slow (3s) → times out and fails open** | `test_e2e_hook.test_slow_server_times_out_and_fails_open` |
| Same repo cloned to different paths → keys match | `test_hook.test_different_clone_locations_same_rel` |
| Windows & POSIX separators → same claim | `test_hook.test_windows_and_posix_same_claim` |

Plus behavior tests for `PreToolUse` staking, `ask`/`block` modes, intent
length-capping, mode validation, and the settings.json installer merge. The
suite (58 tests) runs on CI (`.github/workflows/ci.yml`) across Python
3.9/3.11/3.12.

`test_e2e_hook.py` drives the hook script exactly as Claude Code would (event
JSON piped to stdin, exit code checked) against a live server in a real git
repo.

---

## Milestones status (spec section 9)

1. ✅ **Server skeleton** — claims registry, all endpoints, TTL sweep, SQLite/in-memory. Testable with `curl` (`tests/smoke.sh`).
2. ✅ **Hook client** — path normalization, repo-key derivation, identity, four handlers. Testable by piping sample JSON.
3. ✅ **Single-machine two-session test** — covered by `test_e2e_hook.py` (two sessions/machine-ids in one repo).
4. ⏭️ **Two-machine test** — `docker build` + `docker run` the server on a VPS/tunnel, `hook/install.py --user --url ...` on two machines. No code change needed.
5. ⏭️ **Intent quality pass** — the real success criterion (spec section 11): does the message make the second agent behave *better*? That's a judgement call to iterate on in live use, not a test assertion.

## Success criterion (spec section 11)

Detecting conflicts is the easy part. The real question is whether an agent that
receives the warning **behaves differently and better** than one that doesn't.
That's why intent is denormalized onto every claim and surfaced with branch and
age in the warning message — if the second agent just retries and overwrites,
the signal in that message is the thing to iterate on, not the plumbing.
