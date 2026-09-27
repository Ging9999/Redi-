# Redi v1.2 — Distribution, Install & Hosting

Packaging spec. Goal: one person stands up a server in under five minutes, and every
teammate joins with a single command they can paste from Slack.

**Framing:** the code works. What's untested is the *join flow* — getting a URL and a
token from the host to five other people without a manual copy-paste ritual. This is
where team tools die, and it's a design problem, not an engineering one. Redi is also
worthless at partial adoption (B2), so friction in this flow costs more here than it
would for a solo tool.

---

## Part A — The two flows, as they should feel

Write these into the README first and build backwards from them. If an implementation
choice makes either flow longer, it's the wrong choice.

### Host (once per team)

```
$ docker compose up -d

Redi 1.2 running on :8787
Data: ./redi-data/redi.db

Share this with your team:

    redi join redi://Fk3n9xQ2@redi.acme.internal:8787
```

The server **generates a token on first run** if none is configured, persists it, and
prints the join command. No token generation step, no secret to invent, no docs to read
before it works.

### Team member (once per machine)

```
$ uv tool install redi-coordinator
$ redi join redi://Fk3n9xQ2@redi.acme.internal:8787

✓ Connected to redi.acme.internal (server 1.2)
✓ Credentials saved to ~/.config/redi/credentials
✓ Hooks installed in ~/.claude/settings.json

Redi is active. Run `redi status` to see live claims.
```

Two commands. `redi join` does connectivity check, auth check, credential write, and
hook install as one atomic operation — and rolls back all of it if any step fails.

**Acceptance:** a teammate who has never heard of Redi is coordinating within 60
seconds of being handed the join string, having read nothing.

---

## Part B — Client packaging

### B1. Publish to PyPI (P0)

Stdlib-only with no dependencies makes this nearly free. Package as
`redi-coordinator`, primary install `uv tool install` with `pipx install` documented
as the equivalent.

Console entry point `redi`, with subcommands:

```
redi join <url>       # connect + install hooks
redi status           # live claims for this repo
redi doctor           # diagnostics (B1 from v1.1)
redi serve            # run a server locally
redi hook <event>     # internal — invoked by Claude Code
redi uninstall        # remove hooks, keep credentials
```

Support Python 3.9+ to match existing CI.

### B2. `settings.json` must not contain absolute paths (P0 — blocking bug risk)

v1.1 recommends committing `.claude/settings.json` at the project level so teammates
are covered on clone. That directly conflicts with an installer that writes
`/Users/sam/.local/share/redi/coordinator_hook.py` into it — every other developer gets
a broken hook, and because hooks fail open, **it breaks silently**.

The installer must write a PATH-resolved command:

```json
{ "type": "command", "command": "redi hook pre-tool-use" }
```

Then verify the failure mode: on a machine where `redi` isn't installed,
command-not-found must be a non-blocking error that lets the edit proceed. Add an
explicit test for this — it's the single most likely way a committed config breaks
someone else's repo.

**Acceptance:** a committed `settings.json` works unmodified on macOS, Linux, and
Windows, and degrades to a no-op (not an error, not a block) where Redi isn't
installed.

### B3. Single-file zipapp fallback (P1)

Build a `redi.pyz` with `zipapp` and attach it to GitHub releases. Zero dependencies
means this actually works: download one file, run it with any Python 3.9+. Covers
people without `uv`/`pipx` and anyone who wants to read the whole thing before running
it.

### B4. `redi uninstall` (P1)

Must be as non-destructive as the installer: remove only Redi's hook entries from
`settings.json`, leave everything else untouched, and be idempotent. A tool that's hard
to remove doesn't get trialled.

---

## Part C — Server packaging

### C1. `docker-compose.yml` in the repo root (P0)

The primary hosting path. Drop-in, no arguments:

```yaml
services:
  redi:
    image: ghcr.io/<org>/redi:1.2
    ports: ["8787:8787"]
    volumes: ["./redi-data:/data"]
    restart: unless-stopped
```

Everything else defaults. Token auto-generated to `/data` on first run. Existing
healthcheck wired in.

### C2. Publish the image to GHCR (P0)

Add a release workflow building multi-arch (`amd64`, `arm64` — plenty of people will
host on a Pi or an ARM VPS) and tagging `:1.2` and `:latest`.

### C3. First-run token generation and join-string printing (P0)

On startup with no configured token: generate a cryptographically random one, persist
it to `/data`, and print the join command with the resolved external host. This is what
makes "drop it in and run" true rather than aspirational.

Print the join string on *every* startup, not just the first — the host will need it
again when a new person joins, and they shouldn't have to go find where it's stored.

Also add `redi print-join` for retrieving it without a restart.

### C4. TLS — document, don't build (P0 docs, no code)

The server speaks plain HTTP. Tokens over plain HTTP on the open internet is not
something to ship without a clear statement. Three documented paths, in order of
recommendation:

1. **Tailscale / private network** — recommended default. Redi is an internal team
   tool that doesn't need public exposure; this removes the TLS problem rather than
   solving it, and needs no certificates.
2. **Caddy reverse proxy** — ship a four-line `Caddyfile`; automatic certs.
3. **Direct exposure** — document as not recommended, and have the client warn on a
   plain-`http://` join to a non-loopback, non-private address.

### C5. `redi serve --local` (P1)

Zero-config localhost server in open mode, for evaluating with two sessions on one
machine before asking anyone to host anything. The trial path shouldn't require Docker
or a VPS.

### C6. Version compatibility (P1)

`/healthz` already reports version. Have `redi join` and `redi doctor` compare it
against the client and warn clearly on a major mismatch. Define and document the
compatibility promise now, while there's exactly one version, rather than after it
breaks.

---

## Part D — Credentials and shared config

Split by secrecy, since v1.1 already introduced `.redi.toml`:

- **`.redi.toml`, committed** — server URL, mode, timeouts, quiet-window. Team
  settings, no secrets. A teammate who clones the repo and runs `redi join` with no
  arguments should pick the URL up from here and only need the token.
- **`~/.config/redi/credentials`, never committed** — tokens keyed by server host.
  Mode `0600`. Add the path to the repo's own `.gitignore` guidance in the docs.

**DECIDE:** whether to support a token-in-`.redi.toml` mode for teams who consider this
low-value metadata not worth secret-managing. It makes onboarding a one-step clone-and-go
with no join command at all, which is genuinely compelling. If offered, it must require
an explicit opt-in key and print a clear statement of what it exposes — anyone with repo
read access can read and write claims. Recommend against as the default; the argument
for offering it is real.

---

## Part E — Documentation

Restructure the README around the two Part A flows. Concretely:

- **Above the fold:** what it does in two sentences, then the member flow, then the
  host flow. Nothing else.
- **`docs/HOSTING.md`** — compose, GHCR, TLS options, backup (it's one SQLite file),
  upgrade path.
- **`docs/TROUBLESHOOTING.md`** — organized by symptom, starting with "it's installed
  but nothing happens," which given fail-open will be the most common report by a wide
  margin. Point at `redi doctor` first in every entry.
- Keep `FEATURES.md` for the full inventory, but don't make it the front door.

---

## Part F — Acceptance test matrix

Each row is a fresh environment, run end to end:

| Scenario | Expected |
|---|---|
| Host runs `docker compose up -d` on a clean VPS | Server up, token generated, join string printed |
| Member runs `uv tool install` + `redi join` | Coordinating, no other steps |
| Member on Windows | Same, paths normalized |
| Clone repo with committed `settings.json`, Redi not installed | Edits proceed normally, no errors surfaced to the agent |
| Clone repo with committed `settings.json`, Redi installed, no credentials | `doctor` names the exact fix |
| `redi join` against an unreachable host | Fails clearly, installs nothing, leaves no partial state |
| `redi uninstall` then inspect `settings.json` | Only Redi entries removed, file otherwise byte-identical |
| Server upgraded, client not | Warning, not a silent break |

---

## Suggested order

1. C1–C3 (compose, GHCR, first-run token) — the host flow is the gate on everyone else
2. B1–B2 (PyPI, PATH-resolved hooks) — B2 is a latent break in the current commit-the-config advice
3. `redi join` as one atomic transaction
4. C4 + Part E (TLS docs, README restructure)
5. Part F on real machines, not CI
6. B3, B4, C5, C6
