# Redi

Redi is a coordination layer for Claude Code agents. When an agent is about to
edit a file, Redi checks whether another agent — possibly on a different
developer's machine — is already working on that same file, and if so, tells the
first agent who is there, what branch they are on, and what they are trying to
do. The agent then decides for itself whether to wait, edit something else, or
proceed. Redi never locks a file; it is advisory.

It is a small HTTP server plus a client that hooks into Claude Code's lifecycle
events. Both are a single Python package with no third-party dependencies.

---

## How it works

### The model

There are two moving parts:

1. A **coordination server** that one person on the team runs. It holds a
   registry of *claims*. A claim is a record that says "session X on machine Y is
   editing file F in repo R, on branch B, and here is what it is doing." Claims
   expire on a timer, so a crashed agent never holds a file forever.

2. A **hook client** that runs on each developer's machine. Claude Code fires
   hooks at fixed points in a session (a prompt is submitted, a file is about to
   be edited, a file was edited, the session ended). The client answers those
   events by talking to the server.

The unit of coordination is a file, scoped to a repository. Two agents editing
the same file are a conflict worth reporting even if they are on different
branches — that is precisely the case that causes a painful merge later.

### What happens on an edit

When an agent is about to run `Edit` or `Write`, Claude Code calls the client's
`PreToolUse` hook. The client sends one request to the server that does two
things at once: it checks for other live claims on that file, and it records the
agent's own claim. The response comes back with any conflicts.

- No conflict: the edit proceeds. The claim is now staked, so if another agent
  checks the same file a moment later, it will see this one.
- Conflict: the client writes a message to standard error describing the other
  agent, and exits with a status that tells Claude Code to hand that message
  back to the agent. The agent reads it and decides what to do. In the default
  mode this blocks that one edit attempt but lets the agent retry deliberately;
  other modes can escalate to the human or act as a soft lock.

The message is the whole point of the tool. It reads like this:

```
Another agent is editing this file.
  Sam (branch feat/rate-limit), started 4 minutes ago
    working on: "add per-IP rate limiting to the auth middleware"

This is advisory - Redi does not lock files. Wait, edit elsewhere, or proceed.
```

The intent line ("working on: ...") is what distinguishes Redi from older tools
that only ever signalled that someone was present. The agent's own prompt is
captured on `UserPromptSubmit` and used as that intent; it is re-captured on
every prompt so it stays current, and the message hedges the wording when the
intent is more than about twenty minutes old.

### Identity and scope

- A repository is identified by its normalized git remote URL, so the same repo
  cloned to different paths on different machines resolves to the same key.
  `git@github.com:acme/api.git` and `https://github.com/acme/api` both become
  `github.com/acme/api`.
- A file is identified by its path relative to the repo root, with separators
  normalized, so the same file is the same claim on every machine and OS.
- An agent is identified by (machine id, session id). One developer running two
  concurrent sessions counts as two agents.

### Fail-open

Every network call has a short timeout, and any failure — server down, slow,
unreachable, or the `redi` command not installed at all — results in the edit
proceeding. Redi will never block a developer's work because of its own failure.
The cost of this is that a broken setup is silent, which is why `redi doctor`
exists to make a misconfiguration visible.

### The hot path

Because every edit runs a hook, the hook has to be cheap. Session-stable values
(the repo key, root, machine id, display name, and configuration) are resolved
once when the session starts and cached on disk, so the per-edit path does not
shell out to git and imports only what it needs. On a repository where only one
session is active, the server tells the client it can skip checking for a short
window, so most edits make no network request at all. The result is a
`PreToolUse` hook of roughly 32 ms at the median. Details and measurements are in
[docs/PERF.md](docs/PERF.md).

---

## Running it

### Host a server (once per team)

```
docker compose up -d
docker compose logs redi
```

On first run the server generates a token, saves it next to its database, and
prints a join string on every startup:

```
Redi 1.2 running on :8787
Data: /data/redi.db

Share this with your team:

    redi join redi://Fk3n9xQ2@redi.acme.internal:8787
```

Set `COORD_EXTERNAL_HOST` to the address teammates reach you at so the printed
string is pasteable as-is. To see it again later, run `docker compose logs redi`
or `redi print-join` on the host. Hosting details — TLS, the container image,
backup, upgrades — are in [docs/HOSTING.md](docs/HOSTING.md).

To try it on one machine with no server to host, run a throwaway local server:

```
redi serve --local
```

### Join a server (once per machine)

```
uv tool install redi-coordinator      # or: pipx install redi-coordinator
redi join redi://Fk3n9xQ2@redi.acme.internal:8787
```

`redi join` performs a connectivity check, an auth check, and a version check,
then saves the credentials and installs the hooks. It does all of this as one
operation and rolls everything back if any step fails, so a failed join leaves
no partial state. It prints what it did:

```
Connected to redi.acme.internal (server 1.2)
Credentials saved to ~/.config/redi/credentials
Hooks installed in ~/.claude/settings.json
```

If you cannot use `uv` or `pipx`, download `redi.pyz` from the releases and run
it with any Python 3.9 or newer: `python3 redi.pyz join redi://...`.

---

## Configuration

Configuration is split by whether it is secret.

- **`.redi.toml`, committed to the repo root** — non-secret team settings:
  server URL, mode, timeouts. A teammate who clones the repo and runs `redi
  join` with no URL picks these up. See [examples/.redi.toml](examples/.redi.toml).
- **`~/.config/redi/credentials`, never committed** — the token, stored with
  file mode 0600, keyed by server host. Written by `redi join`.

Environment variables override both. The full list is in
[examples/config.reference.json](examples/config.reference.json); the ones you
are most likely to touch:

| Variable | Meaning |
|---|---|
| `COORD_URL` | Server base URL. |
| `COORD_TOKEN` | Bearer token (normally comes from the credentials file). |
| `COORD_MODE` | `warn` (default), `ask`, or `block`. |
| `COORD_TIMEOUT` | Per-request timeout in seconds (default 0.5). |
| `COORD_FORCE` | Set to 1 to proceed past a `block` and record an override. |

### Committing the hook configuration

The hooks are written as PATH-resolved commands (`redi hook pre-tool-use`), with
no absolute paths, so a `.claude/settings.json` committed at the project level
works for everyone on clone, and on any operating system. On a machine where
`redi` is not installed, the command is simply not found, which is a
non-blocking hook error, so edits proceed as normal. Committing the project
settings is the most effective way to get a whole team covered, and Redi is only
useful when the whole team is covered.

---

## The hook lifecycle

The client is invoked as `redi hook <event>` for each of these:

| Event | What the client does |
|---|---|
| `SessionStart` | Resolve and cache the session-stable values; enable the fast path. |
| `UserPromptSubmit` | Send the prompt to the server as this session's current intent. |
| `PreToolUse` (Edit, Write) | Check for conflicts and stake the claim in one request; warn, ask, block, or proceed. |
| `PostToolUse` (Edit, Write) | Refresh the claim, at most once per third of the TTL. |
| `Stop` | Release all of the session's claims and drop its cache. |

Only `PreToolUse` can stop an edit, and only in the modes that are meant to.

---

## The server

A single process, state held in one SQLite file. Endpoints:

| Method and path | Purpose |
|---|---|
| `POST /claims/acquire` | Check conflicts and stake a claim in one transaction. |
| `POST /claims/check` | Check conflicts without staking. |
| `POST /claims` | Register or refresh a claim. |
| `POST /claims/override` | Record an override note for another session. |
| `POST /sessions/{id}/intent` | Set a session's intent. |
| `POST /sessions/{id}/release` | Release a session's claims. |
| `GET /repos/{key}/activity` | List live claims and recent contributors. |
| `GET /healthz` | Liveness and version; no auth. |

Authentication is a single bearer token, compared in constant time. All
timestamps are the server's own; client clocks are never trusted. Claims carry
their age, time to expiry, and how stale the stated intent is.

---

## Command reference

| Command | Purpose |
|---|---|
| `redi join <url>` | Connect, save credentials, install hooks; atomic, rolls back on failure. |
| `redi status` | Show live claims for the current repo. |
| `redi doctor` | Diagnose the local setup; run this first when nothing seems to happen. |
| `redi serve [--local]` | Run a server; `--local` is a zero-config localhost server. |
| `redi print-join` | Print this server's join string. |
| `redi uninstall` | Remove Redi's hooks; leaves credentials in place. |
| `redi hook <event>` | Internal; invoked by Claude Code. |

When something is installed but nothing appears to be happening — the most common
report, because Redi fails open — run `redi doctor`. It names the cause: hooks
not registered, `redi` not on the path, server unreachable, token rejected, or no
git remote. See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

---

## Development

```
make test          # run the test suite (stdlib only, no dependencies)
make bench         # measure the PreToolUse hot path
make serve-local   # a localhost server for testing with two sessions
make pyz           # build the single-file dist/redi.pyz
```

The package lives in `redi/`: `server` and `store` (the coordination server),
`hook` (the client), `cli` (the `redi` command), `install` (the settings.json
merge), and `creds` (tokens, join URLs, credentials). Tests are in `tests/`.

The full feature inventory is in [FEATURES.md](FEATURES.md). The design was
built to three specs in `docs/`: the original MVP, the v1.1 efficiency and
usability pass, and the v1.2 distribution and hosting pass. MIT licensed.
