# Hosting a Redi server

Redi is an internal team tool: one small, single-process HTTP service holding a
claims registry. It has no database server, no message queue, and no
dependencies — the entire state is one SQLite file.

## Docker Compose (recommended)

[`docker-compose.yml`](../docker-compose.yml) at the repo root is the primary
path:

```console
$ docker compose up -d
$ docker compose logs redi        # prints the `redi join ...` string
```

- The image is pulled from GHCR (`ghcr.io/ging9999/redi:1.2`) or built locally
  with `docker compose up --build`.
- A token is generated on first run and persisted to `./redi-data/token`; the
  join string is printed on **every** startup.
- Set `COORD_EXTERNAL_HOST` to the address teammates reach you at, so the
  printed join string is pasteable as-is (otherwise it uses the container
  hostname).

### Server environment variables

| Var | Default | Meaning |
|---|---|---|
| `COORD_HOST` | `0.0.0.0` (image) | Bind address. |
| `COORD_PORT` | `8787` | Port. |
| `COORD_DB` | `/data/redi.db` (image) | SQLite path, or `:memory:`. |
| `COORD_DATA_DIR` | dir of `COORD_DB` | Where the auto-generated `token` lives. |
| `COORD_TOKEN` | *(unset)* | Set to pin a token; otherwise auto-generated. |
| `COORD_TTL_SECONDS` | `900` | Claim TTL; also the refresh debounce (TTL/3). |
| `COORD_QUIET_SECONDS` | `60` | Solo-session backoff window. |
| `COORD_EXTERNAL_HOST` | container hostname | Host advertised in the join string. |

## The container image (GHCR)

`.github/workflows/release.yml` builds a **multi-arch** image (`amd64` +
`arm64`, so a Raspberry Pi or an ARM VPS works) on any `v*` tag and pushes
`ghcr.io/<owner>/redi:<version>` and `:latest`. It also attaches a single-file
`redi.pyz` to the GitHub release for clients without `uv`/`pipx`.

## TLS — pick one (the server speaks plain HTTP)

Redi sends a bearer token, so tokens over plain HTTP on the open internet is not
something to do casually. Three paths, most-recommended first. The client
**warns** on a plain-`http://` join to a public (non-loopback, non-private)
host.

### 1. Tailscale / private network (recommended)

Redi is an internal tool that doesn't need public exposure. Put the host on your
tailnet and share the join string with its Tailscale name:

```
redi join redi://<token>@redi-host.tailnet.ts.net:8787
```

No certificates, no reverse proxy — the tailnet is already encrypted, and the
`100.64.0.0/10` / `*.ts.net` addresses are treated as private (no warning).

### 2. Caddy reverse proxy (automatic certs)

If you must expose it publicly, front it with Caddy for automatic Let's Encrypt
certificates. A whole `Caddyfile`:

```
redi.example.com {
    reverse_proxy localhost:8787
}
```

Then teammates join with `https://`:

```
redi join https://<token>@redi.example.com
```

### 3. Direct exposure (not recommended)

Binding `0.0.0.0` straight to a public IP over HTTP works but ships tokens in the
clear. The client prints a warning for this case. Don't, unless it's a
throwaway.

## Backup and restore

State is one SQLite file (`redi.db`) plus the `token` file, both under the data
volume. To back up, copy `./redi-data` while the server runs (WAL mode makes a
hot copy safe enough for coordination metadata) or stop and copy for a clean
snapshot. Losing the DB only drops live claims, which expire within the TTL
anyway; losing the `token` file rotates the token (everyone re-runs `redi join`).

## Upgrades

```console
$ docker compose pull && docker compose up -d
```

The data volume carries the DB and token across versions. Clients keep working;
`redi doctor` and `redi join` warn on a **major**-version mismatch between client
and server (see the compatibility note below). SQLite columns added in newer
versions are migrated in place on startup.

## Version compatibility

`/healthz` reports the server version. The promise, defined now while there's one
version: **same major version = compatible.** A minor/patch skew between client
and server is fine (endpoints are additive). A major mismatch prints a warning
rather than failing, so an out-of-date client degrades loudly, not silently.
