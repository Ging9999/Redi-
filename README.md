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

Built to `docs/agentcoordinationmvp.md` (v1), then hardened for the hot path and
usability per `docs/rediv1_1efficiencyux.md` (v1.1). **The hot path is the
product**: every `Edit`/`Write` pays Redi's cost, so `PreToolUse` p50 was driven
from **89ms to 32ms** (see `docs/PERF.md`).

---

## Architecture

Four pieces, **Python 3 stdlib only — no dependencies**:

| Piece | File | Role |
|---|---|---|
| Coordination server | `server/coordinator.py` + `server/store.py` | Single-process HTTP service holding the claims registry, SQLite-backed (WAL). |
| Hook client | `hook/coordinator_hook.py` | One script wired into Claude Code lifecycle events on each machine. |
| CLI | `cli/redi.py` (`./redi`) | `redi doctor` (diagnose setup) and `redi status` (see live claims). |
| Activity endpoint | `GET /repos/{repo_key}/activity` | JSON view of live claims + reporting contributors; backs `redi status`. |

Zero pip installs is a deliberate choice: a coordination tool has to be
installed on every developer's machine, so the friction of dependencies is a
real adoption cost.

### The hot path (v1.1)

Every edit runs the `PreToolUse` hook, so it must be cheap. Session-stable values
(repo key, root, machine id, display name, config) are resolved **once** at
`SessionStart` and cached to `~/.cache/redi`; the steady-state edit path never
shells out to git and imports no `urllib`/`subprocess` unless it actually makes a
request. On a **solo repo** the server hands back a `quiet_until` window and the
hook skips the network entirely — a 50-edit run makes ~1 request. Full numbers
and the (negative) local-daemon decision are in `docs/PERF.md`.

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
| `COORD_TTL_SECONDS` | `900` | Claim TTL (15 min); also sets refresh debounce to TTL/3. |
| `COORD_QUIET_SECONDS` | `60` | Solo-session backoff window (spec A3). Lower = tighter coverage, more requests. |
| `COORD_TOKEN` | *(unset)* | Bearer token. Unset = open mode (dev only). |

### 2. Wire the hook into Claude Code

Use the installer — it merges the five hooks (`SessionStart`, `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `Stop`) and the `COORD_*` env block into your
`.claude/settings.json`, preserving anything already there, and is idempotent:

```bash
# project-level (./.claude/settings.json), local server:
python3 hook/install.py

# user-level, shared server with a token:
python3 hook/install.py --user --url https://coord.example.com --token SECRET

# preview the merge without writing:
python3 hook/install.py --dry-run
```

**Commit `.claude/settings.json` at the project level.** This is the single
biggest lever on adoption: a teammate who clones the repo is covered without
doing anything, and Redi only prevents collisions if *everyone* runs it (a
partial rollout looks identical to a working one). Shared, non-secret settings
(server URL, mode) can also live in a committed `.redi.toml` at the repo root
(see `examples/.redi.toml`); env vars override it. Keep the token in the
environment, not in a committed file.

Prefer to do it by hand? Copy the `env` and `hooks` blocks from
`examples/settings.json` and set the absolute path to `coordinator_hook.py`.
Hook environment variables are documented in `hook/config.example.json`.

**Matchers are case-sensitive** (spec section 3): `Edit|Write` matches the Edit
and Write tools; `edit` matches nothing.

### 3. Check it works — `redi doctor`

Fail-open is right at runtime but terrible during setup: a wrong token, a typo'd
URL, or an unregistered hook all produce a Redi that silently does nothing. Run
the doctor to make that visible:

```bash
./redi doctor     # hooks wired? server reachable? auth ok? git remote? who else is here?
./redi status     # live claims for this repo: who, file, branch, intent, age
```

```bash
./tests/smoke.sh          # or: exercise every endpoint against a running server with curl
```

### Run the server in Docker (for the two-machine test)

```bash
docker build -t agent-coordinator .
docker run -e COORD_TOKEN=secret -p 8787:8787 -v coord-data:/data agent-coordinator
```

The image is stdlib-only (no pip install), persists the SQLite store on the
`/data` volume, and has a built-in healthcheck.

---

## Hook wiring

| Event | Matcher | What the hook does |
|---|---|---|
| `SessionStart` | — | Resolve session-stable values (repo key, root, identity, config) once → cache. Enables the fast path. |
| `UserPromptSubmit` | — | POST the prompt as the session's intent — every prompt, so intent stays current (B6). |
| `PreToolUse` | `Edit\|Write` | One atomic `acquire`: check + stake; then warn/ask/block/force. Skipped entirely while quiet (A3). |
| `PostToolUse` | `Edit\|Write` | Refresh the claim, debounced to once per TTL/3 (A4). |
| `Stop` | — | Release all claims for the session and drop its cache. |

**`acquire` stakes atomically and narrows the race.** A naive design only
*checks* at `PreToolUse` and doesn't record the claim until `PostToolUse` (after
the edit), so two agents starting in the same moment both check-clear and
collide. Instead `POST /claims/acquire` checks conflicts and stakes the caller's
claim in **one transaction and one round trip** (spec A2). This shrinks the
collision window to a single request; it doesn't eliminate it (see Known
limitations).

**Solo-session backoff (A3).** When the server sees the caller is the only active
session on a repo, it returns a `quiet_until` timestamp; the hook caches it and
makes **no network call at all** until it lapses (default 60s, `COORD_QUIET_SECONDS`).
Tradeoff, documented: when a teammate starts up, the already-issued quiet window
can't be revoked, so there's up to one window of unwarned edits. Tighten the
window for teams that want it.

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

## API

| Method + path | Body | Returns |
|---|---|---|
| `POST /claims/acquire` | `{repo_key, file_path, session_id, machine_id, …}` | `{conflicts, claim, quiet_until, overrides}` — the hot path (A2/A3/B5). |
| `POST /claims/check` | `{repo_key, file_path, session_id, machine_id}` | `{conflicts: [Claim]}` — read-only, excludes caller. |
| `POST /claims` | claim fields (+ optional `intent`) | `{claim: Claim}` — register/refresh. |
| `POST /claims/override` | `{repo_key, file_path, target_session, overrider_name, reason}` | `{ok: true}` — record an override note (B5). |
| `POST /sessions/{session_id}/intent` | `{intent}` | `{ok: true}` |
| `POST /sessions/{session_id}/release` | — | `{released: N}` |
| `GET /repos/{repo_key}/activity?days=N` | — | `{claims, contributors}` — supports `If-None-Match`/`304` (A8). |
| `GET /healthz` | — | `{ok: true, version}` (no auth) |

Auth is a single bearer token in `COORD_TOKEN`, compared in constant time. Each
`Claim` carries `age_seconds`, `expires_in_seconds`, and `intent_age_seconds`
(so a reader can judge how stale the stated intent is — B6). `activity` also
lists **contributors** who reported in the last N days, so a team can see who is
actually running Redi (coverage, B2).

## Coordination quality (v1.1)

- **Repeat-warning suppression (B4).** Once an agent has been warned about a
  given `(file, other-session)`, that warning is suppressed for
  `COORD_SUPPRESS_SECONDS` (default 10 min) — unless the other agent's *intent*
  changes, which is genuinely new information. Two agents working the same file
  all afternoon get one warning, not one per edit.
- **Explicit override (B5).** In `block` mode an agent that genuinely must edit
  sets `COORD_FORCE=1` (with an optional `COORD_FORCE_REASON`). It proceeds and
  records a server-side override the other agent sees on its next check —
  "Priya overrode your claim on `auth.ts`: hotfix must ship" beats a silent
  overwrite.
- **Intent staleness (B6).** Intent captured at prompt time drifts; a
  confidently wrong 40-minute-old intent is worse than none. Intent is
  re-captured on every prompt, and the warning hedges anything older than ~20
  min ("started from: …" instead of "working on: …").
- **Coverage nudge (B2).** `SessionStart` notes once if the repo has had recent
  git contributors who aren't reporting Redi claims — surfacing a partial
  rollout instead of letting it masquerade as a working one.

## Claim lifecycle (spec section 6)

- Claims carry a **15-minute TTL**.
- `PostToolUse` refreshes, **debounced to once per TTL/3** (A4) — an active agent
  keeps its claim alive without a request per keystroke; `created_at` is
  preserved across refreshes.
- `Stop` releases explicitly and drops the session cache.
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

This is **not solved**, by design. Matching `Bash` and parsing arbitrary
commands for the file paths they touch is a large surface area that's easy to
get wrong (a mis-parse either misses a real edit or blocks an unrelated
command). It's noted here as a documented gap and a possible future direction.

### Batch turns (A7) — not built

An agent editing eight files in one turn makes up to eight `PreToolUse` calls.
Claude Code exposes no per-turn batch event to coalesce them, and A4 (refresh
debounce) plus A3 (quiet backoff) already remove most of the traffic, so this is
deferred rather than built. Revisit if a batch hook event appears.

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

Plus v1.1 behavior tests: atomic `acquire`, quiet backoff, `EXPLAIN QUERY PLAN`
index use, contributors, overrides, intent staleness (`test_v11_store.py`); the
zero-subprocess hot path, quiet-skip, refresh debounce, warning suppression, and
force/override (`test_v11_hook.py`); CLI settings resolution and `.redi.toml`
(`test_cli_config.py`). ~90 tests, on CI across Python 3.9/3.11/3.12.

`test_e2e_hook.py` drives the hook script exactly as Claude Code would (event
JSON piped to stdin, exit code checked) against a live server in a real git
repo. `tests/bench.py` (`make bench`) measures the hot path; see `docs/PERF.md`.

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
