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

## After optimization

_Populated by `make bench LABEL="AFTER (v1.1)"` once A1–A4 land; see below._

<!-- AFTER-RESULTS -->

---

## A5 — local daemon decision

_Deferred pending the AFTER numbers. Decision recorded here once A1–A4 are
measured: build the unix-socket daemon only if the hot path still exceeds
~50ms p95 after those land. If A3 lands well, most edits make no network call
at all and the daemon is unnecessary._

<!-- A5-DECISION -->
