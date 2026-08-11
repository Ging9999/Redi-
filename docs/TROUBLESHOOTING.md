# Troubleshooting

**Run `redi doctor` first.** Because Redi fails open, most problems present as
silence — nothing errors, nothing coordinates. `doctor` turns that silence into
a specific, named cause. Every entry below points back to it.

---

## "It's installed but nothing happens"

By far the most common report, because fail-open hides every misconfiguration.
Work down what `redi doctor` shows:

**Hooks not registered.** `doctor` says "no settings.json wires Redi hooks."
- You ran `uv tool install` but not `redi join`. Run
  `redi join redi://<token>@<host>:<port>`.
- Or a committed project `settings.json` exists but you cloned before it was
  added — `git pull`, then re-open the session (hooks load at SessionStart).

**`redi` not on PATH.** The hooks are `redi hook …`; if the shell can't find
`redi`, Claude Code reports a non-blocking hook error and edits proceed silently.
- `which redi` — nothing? Re-install: `uv tool install redi-coordinator` (and
  ensure `uv`'s tool bin dir is on PATH), or use the zipapp.
- This is intentional fail-open: a teammate without Redi is never blocked. But
  it means *you* aren't reporting claims. `redi doctor` flags it.

**Server unreachable.** `doctor` says "cannot reach <url>."
- Wrong URL: check `redi doctor`'s Configuration block. Fix with a fresh
  `redi join`, or correct `COORD_URL` / `.redi.toml`.
- Server down: on the host, `docker compose ps` / `docker compose up -d`.

**Token rejected.** `doctor` says "authorized request failed."
- The host rotated the token (e.g. lost the `token` file). Ask for a new join
  string and re-run `redi join`.

**No git remote.** `doctor` says "no git 'origin' remote."
- Redi scopes claims by remote URL. `git remote add origin <url>`, or work in a
  cloned repo.

---

## "My agent feels slower"

The hot path is p50 ~32ms and makes no network call on a solo repo (see
[`PERF.md`](PERF.md)). If it regressed:
- A slow/unreachable server should *not* slow edits (fail-open times out at
  `COORD_TIMEOUT`, default 0.5s). If it does, lower `COORD_TIMEOUT`.
- `SessionStart` not wired means the hot path re-derives git state each edit.
  `redi doctor` warns when SessionStart is missing; re-run `redi join`.

---

## "I get warned about the same file over and over"

You shouldn't — a repeat `(file, other-session)` warning is suppressed for
`COORD_SUPPRESS_SECONDS` (default 10 min), and only re-warns if the other
agent's intent changes. If you're seeing it every edit, the other agent's intent
string is churning (each new prompt re-states it). That's genuinely new info, so
it's working as designed.

---

## "I must edit a file another agent has claimed"

In `block` mode, set `COORD_FORCE=1` (and `COORD_FORCE_REASON="why"`) for that
run. Redi proceeds and leaves the other agent a note it sees on its next check —
better than a silent overwrite.

---

## "A teammate isn't showing up in `redi status`"

Redi is worthless at partial adoption. `redi status` lists *reporting*
contributors; `SessionStart` also notes once when a repo has recent git authors
who aren't reporting. If someone's missing, they haven't run `redi join` (or
`redi` isn't on their PATH — see above). Commit `.claude/settings.json` at the
project level so clone-and-go covers them.

---

## "`redi join` failed"

It's atomic: on any failure it installs nothing and leaves no partial state.
- "cannot reach <url>" — wrong host/port, or server down.
- "token was rejected" — wrong or rotated token; get a fresh `redi print-join`.
- A plain-`http://` warning to a public host is a *warning*, not a failure — the
  join still succeeds. Prefer Tailscale or TLS (see [`HOSTING.md`](HOSTING.md)).

---

## Removing Redi

```console
$ redi uninstall            # removes only Redi's hook entries; keeps credentials
```

It's idempotent and leaves the rest of `settings.json` intact. Delete
`~/.config/redi/credentials` to remove the stored tokens too.
