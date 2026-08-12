# MVP Spec: Cross-Machine Agent Edit Coordination

A coordination layer so Claude Code agents running on different developers' machines
know when another agent is already working on the same file — and can adapt instead
of colliding at merge time.

Hand this to a coding agent as a planning brief. Sections marked **DECIDE** are open
questions the agent should resolve and justify before building.

---

## 1. Goal

When Agent A (Sam's laptop) is mid-edit on `src/auth/middleware.ts`, and Agent B
(Priya's laptop) is about to edit the same file, Agent B should be told:

> Another agent is editing this file. Sam's session is on branch `feat/rate-limit`,
> working on: "add per-IP rate limiting to the auth middleware." Started 4 minutes ago.

Agent B then decides for itself: wait, work elsewhere, coordinate, or proceed anyway.

**The differentiator is intent, not presence.** Prior art (GitLive, GitKraken Team
View, FASTDash) only ever signalled "someone is here." Agents can state *what* they're
doing in natural language and *read* another agent's statement. Build for that.

### Non-goals for v1

- Line- or symbol-level granularity (file-level only)
- Semantic conflict detection
- Human-facing editor extension (agents only)
- Multi-tenant auth (single shared token is fine)
- Any actual merge or conflict *resolution*

---

## 2. Architecture

Three pieces:

1. **Coordination server** — small HTTP service, single process, holding a claims
   registry. Stateless-ish; a single SQLite file or in-memory store with periodic
   flush is sufficient for v1.
2. **Hook client** — a script (or direct HTTP hook) registered in each developer's
   `.claude/settings.json`, wired to Claude Code lifecycle events.
3. **Activity endpoint** — a JSON endpoint showing all live claims for a repo. Useful
   for debugging and for a later dashboard. No UI required in v1.

### Room key

Claims are scoped by **normalized git remote URL**, not by local path.

- Get it with `git remote get-url origin`, then normalize: strip protocol, strip
  trailing `.git`, lowercase. `git@github.com:acme/api.git` and
  `https://github.com/acme/api` must produce the same key.
- Branch is stored as *metadata*, not part of the key. Two agents on different
  branches touching the same file is exactly the case worth flagging.

### File path normalization

**This is the most common way this class of tool breaks.** The hook receives an
absolute path, which differs per machine. Convert to repo-relative before sending:

```
git rev-parse --show-toplevel   # then strip that prefix from tool_input.file_path
```

Normalize separators. Reject anything outside the repo root.

### Agent identity

- `session_id` — provided in the hook's stdin JSON
- `machine_id` — hostname, or a UUID persisted in `~/.config/<tool>/id`
- `display_name` — `git config user.name`

Identity is `(machine_id, session_id)`. A single developer may run several concurrent
sessions; treat each as a distinct agent.

---

## 3. Hook wiring

Claude Code hooks are shell commands (or HTTP endpoints) fired at fixed lifecycle
events, registered in `.claude/settings.json`. Relevant events:

| Event | Matcher | Purpose |
|---|---|---|
| `UserPromptSubmit` | — | Capture the prompt as session intent; POST to server |
| `PreToolUse` | `Edit\|Write` | Check for conflicting claims before the edit runs |
| `PostToolUse` | `Edit\|Write` | Register/refresh the claim on that file |
| `Stop` | — | Release all claims for the session |

### Key mechanics the agent must get right

- **`PreToolUse` is the only event that can stop the tool call.** Exit code 2 blocks
  it and feeds stderr back to Claude as the reason. Alternatively, exit 0 and print
  JSON with `hookSpecificOutput.permissionDecision` set to `allow` / `deny` / `ask`.
- **JSON decisions are only parsed on exit 0.** A script that prints a decision JSON
  and *then* exits 2 has its JSON discarded — stderr becomes the reason instead.
- **Matchers are case-sensitive.** `edit` never matches `Edit`.
- **HTTP hooks** are an option: `{"type": "http", "url": "..."}` POSTs the event JSON
  to your endpoint. Note that non-2xx responses, connection failures, and timeouts are
  *non-blocking* errors — execution continues. To block, return 2xx with
  `decision: "block"` in the body. Decide whether to use HTTP hooks directly or a thin
  local script that calls the server.
- **Set a short timeout.** Every file edit now blocks on a network round trip. Budget
  under ~500ms and fail open on timeout. A coordination tool that stalls the agent
  will be uninstalled within a day.

### Intent capture

Simplest v1: the `UserPromptSubmit` hook ships the user's prompt to the server as the
session's intent string. Zero extra work for the developer, and it's usually a decent
description of what the agent is about to do.

**DECIDE:** whether to additionally expose an MCP tool (e.g. `declare_intent`) letting
the agent write a more precise, self-authored statement of what it's changing and why.
This is likely better but costs the agent a tool call.

---

## 4. Data model

```
Claim
  repo_key        string
  file_path       string   -- repo-relative
  session_id      string
  machine_id      string
  display_name    string
  branch          string
  intent          string   -- from session
  created_at      timestamp
  expires_at      timestamp
```

Primary key: `(repo_key, file_path, machine_id, session_id)`.

---

## 5. API

- `POST /claims/check` → `{repo_key, file_path, session_id, machine_id}`
  Returns `{conflicts: [Claim]}`, excluding the caller's own claims. Must be fast.
- `POST /claims` → register or refresh a claim, extending `expires_at`
- `POST /sessions/{session_id}/intent` → `{intent}`
- `POST /sessions/{session_id}/release` → drop all claims for the session
- `GET /repos/{repo_key}/activity` → all live claims, for debugging

Auth: single bearer token in an env var. Nothing fancier in v1.

---

## 6. Claim lifecycle

- Claims carry a **TTL** — start with 15 minutes.
- `PostToolUse` refreshes on every edit, so an active agent keeps its claim alive.
- `Stop` releases explicitly.
- Expired claims are swept lazily on read. A crashed session must never hold a file
  hostage indefinitely.
- Use server-side timestamps throughout. Do not trust client clocks.

---

## 7. Response policy

**Default to warn, not block.** On a conflict, exit 2 with a stderr message describing
who else is on the file, their branch, their intent, and how long ago they started —
then let the agent decide. Hard locking will deadlock a team's agents fast.

Blocking mode should exist behind a config flag, off by default.

**DECIDE:** whether "warn" is better implemented as an exit-2 block with an explanatory
message (the agent can then retry deliberately) or as `permissionDecision: "ask"`
(escalates to the human). Consider which produces less friction in a real session.

---

## 8. Known gap to document

Agents don't only edit through `Edit`/`Write`. They also run `sed`, `python`, codemods,
and formatters through `Bash`. Those bypass your matcher entirely.

Do **not** try to solve this in v1. Document it as a known limitation. Note that
matching `Bash` and parsing commands for file paths is a possible v2 direction, with
the caveat that it's a large surface area and easy to get wrong.

---

## 9. Milestones

1. **Server skeleton** — claims registry, all five endpoints, TTL sweep, in-memory
   store. Testable with `curl` alone.
2. **Hook client** — path normalization, repo key derivation, identity, the four hook
   handlers. Test by piping sample JSON to the script and checking `echo $?`.
3. **Single-machine two-session test** — two Claude Code sessions in separate clones
   on one machine, editing the same file. This should work before touching networking.
4. **Two-machine test** — server on a VPS or tunnel, two developers, real repo.
5. **Intent quality pass** — is the message the second agent receives actually useful
   enough to change its behaviour? This is the real success criterion, and it's a
   judgement call, not a test assertion.

---

## 10. Test scenarios

- Two agents, same file, same branch → conflict reported
- Two agents, same file, different branches → conflict reported (this is the point)
- Two agents, different files → no conflict, no added latency
- Same agent re-editing its own file → no self-conflict
- Session killed with `SIGKILL` → claim expires, file becomes available
- Server unreachable → hook fails open, edits proceed, agent is not blocked
- Server slow (3s) → hook times out and fails open
- Same repo cloned to different local paths on each machine → keys still match
- Windows and POSIX path separators → normalize to the same claim

---

## 11. Success criterion

Not "does it detect conflicts" — that's easy. The question is whether an agent that
receives the warning **behaves differently and better** than one that doesn't. If the
second agent just retries and overwrites, the intent messaging isn't carrying enough
signal and that's the thing to iterate on, not the plumbing.
