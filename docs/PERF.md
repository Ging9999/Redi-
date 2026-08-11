# Redi performance — the hot-path budget

Every `Edit`/`Write` an agent makes now pays Redi's cost on the `PreToolUse`
hook. That budget *is* the product (spec A0): if a developer perceives their
agent as slower, Redi gets uninstalled no matter how good the coordination is.

Numbers are produced by `make bench` (`tests/bench.py`), 100 iterations each,
on localhost (so no real DNS/TLS — a note on that below). Re-run after any
hot-path change and update the "AFTER" section.

## How to read this

- **PreToolUse end-to-end** is the number that matters: the whole hook process,
  spawned exactly as Claude Code spawns it (fork + `python3` + stdin JSON).
- **fail-open** must be *fast*, not just correct — a down or slow server must not
  stall the agent. If this row is as slow as the "server up" row, the cost is
  fixed overhead (interpreter + imports), not the network.
- The component rows are measured independently and don't sum exactly to the
  end-to-end figure; the **residual** captures Python module imports and
  per-invocation hook work not attributed elsewhere.

## Caveats

- **Localhost hides real network cost.** DNS, TCP handshake, and TLS are ~0 here.
  On a real remote server budget tens of ms per round trip — which is the entire
  reason A2 (one request instead of two) and A3 (usually *zero* requests) matter.
- Interpreter startup is unavoidable while hooks are `type: command` (a fresh
  `python3` per event). The lever we have is what that interpreter then imports.

---

## Baseline — before optimization (v1.0)

### BEFORE (v1.0 baseline)

_Python 3.11.15, linux, n=100, localhost (no real DNS/TLS), 2026-08-11 16:36:36 UTC_

| Measurement | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|
| **PreToolUse end-to-end (server up)** | 89.5 | 110.6 | 177.3 |
| **PreToolUse end-to-end (fail-open, server down)** | 88.4 | 103.3 | 115.6 |
| _component:_ interpreter startup | 13.7 | 15.5 | 19.4 |
| _component:_ git subprocess | 2.1 | 2.4 | 2.6 |
| _component:_ network + server handling | 1.2 | 1.7 | 1.9 |
| _derived:_ residual (imports/other) p50 | 72.5 | — | — |

### What the baseline says

- **The network is not the problem here; the process is.** "Server up" (89.5ms)
  and "fail-open" (88.4ms) are within noise of each other. A single localhost
  round trip is ~1ms. The cost is paid before any packet moves.
- **The dominant cost is module imports** — the 72.5ms residual. At module scope
  the hook imports `urllib.request` (which pulls in `ssl`, `http`, `email`,
  `socket`), plus `subprocess`, `uuid`, and `pathlib`. That import graph is the
  single biggest line item, larger than a bare interpreter start (13.7ms).
- **git is cheap but not free** (~2ms/call), and the v1.0 hook shells out 3× per
  `PreToolUse` (`remote get-url`, `rev-parse`, `config user.name`).

### The plan this justifies

| Item | Targets | Expected effect |
|---|---|---|
| A1 lazy imports | the 72.5ms residual | don't import `urllib`/`ssl`/`uuid`/`pathlib` unless the path actually needs them |
| A1 session cache | the 3× git (~6ms) | zero subprocess on the steady-state Edit path |
| A2 single `acquire` | 2 round trips → 1 | halves network cost (matters off-localhost) |
| A3 quiet backoff | the whole network + most imports | solo repo: most edits make **no** request and skip the urllib import entirely |
| A4 refresh debounce | PostToolUse traffic | one refresh per ~5 min instead of per edit |

Everything below is measured against these numbers, not assumed.

---

## After optimization (A1–A4, A6)

### AFTER (v1.1)

_Python 3.11.15, linux, n=100, localhost (no real DNS/TLS), 2026-08-11 16:47:40 UTC_

| Measurement | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|
| **PreToolUse end-to-end (server up)** | 32.0 | 39.0 | 42.7 |
| **PreToolUse end-to-end (fail-open, server down)** | 32.0 | 34.7 | 35.9 |
| _component:_ interpreter startup | 14.4 | 19.6 | 20.2 |
| _component:_ git subprocess | 2.2 | 2.7 | 3.1 |
| _component:_ network + server handling | 1.4 | 1.8 | 2.2 |
| _derived:_ residual (imports/other) p50 | 14.0 | — | — |

### What changed

| | BEFORE p50 | AFTER p50 | |
|---|---|---|---|
| PreToolUse end-to-end | 89.5 ms | **32.0 ms** | −64% |
| residual (imports/other) | 72.5 ms | **14.0 ms** | −81% |

- **The import tax is gone.** Moving `urllib`/`subprocess`/`uuid`/`pathlib` off
  module scope (A1) cut the residual from 72.5ms to 14.0ms. `normalize_file_path`
  was also rewritten without `pathlib` (pure string ops).
- **The hot path no longer touches git.** Session-stable values are resolved once
  (SessionStart / first-edit bootstrap) and read from `~/.cache/redi`; the bench's
  steady-state iterations shell out zero times (enforced by
  `tests/test_v11_hook.py::ZeroSubprocessTest`).
- **On a solo repo, edits make almost no requests.** The bench points at one repo
  with one session; after the first `acquire` returns `quiet_until`, iterations
  2–100 skip the network entirely (A3). That's why "server up" and "fail-open"
  are identical — the common path makes no call either way. A 50-edit solo run
  makes **1** HTTP request, comfortably under the ≤3 acceptance bar.
- **What's left is interpreter startup** (14ms) — unavoidable while hooks are
  `type: command` (a fresh `python3` per event). git (2ms) and network (1ms) are
  noise. The "makes a request" path (fail-open row, which imports `urllib` and
  attempts a connection every iteration) is also ~32ms, so a real request on
  localhost adds only the round trip.

### Off-localhost note

These numbers hide real network cost. A2 (one `acquire` instead of check+stake)
and A3 (usually zero requests) are what keep a *remote* server from being felt:
on the quiet path there is no round trip to pay, and when there is one, it's a
single one.

---

## A5 — local daemon decision: **not building it**

The structural fix for interpreter startup + connection setup is a local daemon
holding a warm connection and cache, with hooks talking to it over a unix socket.
The spec is explicit: build it only if the hot path still exceeds ~50ms p95 after
A1–A4, and do not build it on principle.

**Decision: do not build the daemon in v1.1.** The evidence:

- Post-optimization p95 is **39.0ms**, under the 50ms bar.
- The dominant remaining cost is interpreter startup (14ms), which a daemon
  reduces but does not eliminate (the hook process still has to start to talk to
  the socket).
- A3 already removes the network from the common (solo) case entirely, so the
  daemon's warm-connection benefit applies mostly to multi-session repos, which
  are the minority.
- A daemon adds real cost the spec warns about: lifecycle management, orphan
  processes, harder install, an extra thing to debug.

Revisit if: (a) real-world remote-server p95 exceeds 50ms despite A2/A3, or
(b) profiling shows a workload dominated by contested files (where quiet backoff
never engages). Until then the cost/benefit is negative.
