# Redi

Redi tells a Claude Code agent when another agent — on a **different machine** —
is already editing a file, and **what** it's doing, so it can adapt instead of
colliding at merge time. One person hosts a small server; everyone else joins
with a single command.

```
Another agent is editing this file.
  Sam (branch feat/rate-limit), started 4 minutes ago
    working on: "add per-IP rate limiting to the auth middleware"

This is advisory — Redi does not lock files. Wait, edit elsewhere, or proceed.
```

---

## Join a team's Redi (once per machine)

Someone hands you a join string. Two commands:

```console
$ uv tool install redi-coordinator          # or: pipx install redi-coordinator
$ redi join redi://Fk3n9xQ2@redi.acme.internal:8787

✓ Connected to redi.acme.internal (server 1.2)
✓ Credentials saved to ~/.config/redi/credentials
✓ Hooks installed in ~/.claude/settings.json

Redi is active. Run `redi status` to see live claims.
```

That's it — you're coordinating. `redi join` does the connectivity check, auth
check, credential write, and hook install as one atomic step, and rolls all of
it back if anything fails. If something looks off later, `redi doctor` tells you
exactly what and how to fix it.

No `uv`/`pipx`? Grab `redi.pyz` from the [releases](https://github.com/Ging9999/Redi-/releases)
and run it with any Python 3.9+: `python3 redi.pyz join redi://…`.

## Host a Redi for your team (once)

```console
$ docker compose up -d
$ docker compose logs redi

Redi 1.2 running on :8787
Data: /data/redi.db

Share this with your team:

    redi join redi://Fk3n9xQ2@redi.acme.internal:8787
```

The server **generates a token on first run**, persists it to `./redi-data`, and
prints the join command — no secret to invent, nothing to configure. Set
`COORD_EXTERNAL_HOST` to the address your team reaches you at so the printed
string is pasteable as-is. Need it again for a new teammate? `docker compose
logs redi`, or run `redi print-join` on the host.

Just want to try it on one machine first, no Docker, no VPS?

```console
$ redi serve --local          # zero-config localhost, open mode
```

**Hosting details** — TLS/Tailscale, GHCR image, backup, upgrades — are in
[`docs/HOSTING.md`](docs/HOSTING.md). **Something not working?**
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) (start with `redi doctor`).

---

## Why intent, not presence

Prior art (GitLive, GitKraken Team View, FASTDash) only ever signalled "someone
is here." Redi's difference is that each agent states in natural language what
it's changing, and another agent reads that statement and decides for itself.
The message an agent receives is the entire product; everything else is
plumbing to deliver it cheaply and reliably. The full design and feature
inventory is in [`FEATURES.md`](FEATURES.md).

Two properties make it safe to leave installed:

- **Fail-open, always.** Server down, slow, unreachable, or `redi` not installed
  at all → the edit just proceeds. Redi never blocks work on its own failure.
- **The hot path is cheap.** Every `Edit`/`Write` runs a hook, so it must be.
  `PreToolUse` is p50 ~32ms and on a solo repo makes no network call at all. See
  [`docs/PERF.md`](docs/PERF.md).

---

## The `redi` command

| Command | What it does |
|---|---|
| `redi join <url>` | Connect + save credentials + install hooks (atomic, rolls back on failure). |
| `redi status` | Live claims for the current repo: who, file, branch, intent, age. |
| `redi doctor` | Diagnose the setup — the first thing to run when "nothing happens." |
| `redi serve [--local]` | Run a coordination server (`--local` = zero-config localhost). |
| `redi print-join` | Print this server's join string (host side). |
| `redi uninstall` | Remove Redi's hooks (keeps credentials). |
| `redi hook <event>` | Internal — invoked by Claude Code, not by you. |

## Shared config vs secrets (split by secrecy)

- **`.redi.toml`, committed** at the repo root — server URL, mode, timeouts.
  Team settings, no secrets. A teammate who clones and runs `redi join` with no
  URL picks it up from here. See [`examples/.redi.toml`](examples/.redi.toml).
- **`~/.config/redi/credentials`, never committed** — tokens keyed by server
  host, mode `0600`. Written by `redi join`.

**Adoption tip:** commit `.claude/settings.json` at the project level. Because
the hooks are PATH-resolved (`redi hook pre-tool-use`, no absolute paths), a
committed config works for everyone on clone — and degrades to a silent no-op on
a machine where Redi isn't installed. Redi only prevents collisions if the whole
team runs it, so this is the single biggest adoption lever.

---

## Develop

```console
$ make test          # 115 tests, stdlib only, no deps
$ make bench         # measure the PreToolUse hot path
$ make serve-local   # a localhost server for two-session testing
$ make pyz           # build dist/redi.pyz
```

Layout: the `redi/` package (`server`, `store`, `hook`, `cli`, `install`,
`creds`); `tests/`; `docs/` (specs, PERF, HOSTING, TROUBLESHOOTING);
`examples/`. Built to the specs in `docs/agentcoordinationmvp.md` (v1),
`docs/rediv1_1efficiencyux.md` (v1.1), and `docs/rediv1_2distribution.md` (v1.2).
MIT licensed.
