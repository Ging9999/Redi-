#!/usr/bin/env python3
"""Hot-path benchmark for the Redi hook (spec A0).

Measures what every ``Edit``/``Write`` actually pays, because the hot-path
budget *is* the product: if a developer perceives their agent as slower, Redi
gets uninstalled regardless of how good the coordination is.

Reports, over N iterations (default 100):

  * p50 / p95 / p99 wall time of the ``PreToolUse`` hook, end to end
  * a component breakdown: interpreter startup, git subprocess, network +
    server handling, and the residual (imports / everything else)
  * the same end-to-end timing with the server unreachable (the fail-open path,
    which must be *fast*, not merely correct)

Run with ``make bench`` (writes docs/PERF.md) or ``python3 tests/bench.py``.
Pass ``--write docs/PERF.md`` to (re)generate the report, ``--label BEFORE`` to
tag the run.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hook", "coordinator_hook.py")
sys.path.insert(0, os.path.join(ROOT, "server"))

import coordinator  # noqa: E402
from store import ClaimStore  # noqa: E402


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _stats(values: list[float]) -> dict:
    return {
        "p50": _pct(values, 50) * 1000,
        "p95": _pct(values, 95) * 1000,
        "p99": _pct(values, 99) * 1000,
        "min": (min(values) if values else 0) * 1000,
        "n": len(values),
    }


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp):
    _git(tmp, "init", "-q")
    _git(tmp, "remote", "add", "origin", "git@github.com:acme/api.git")
    _git(tmp, "config", "user.name", "Bench Dev")
    _git(tmp, "config", "user.email", "bench@example.com")
    os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
    target = os.path.join(tmp, "src", "a.ts")
    with open(target, "w") as fh:
        fh.write("// bench\n")
    # A commit so branch resolution takes the common (fast) path.
    _git(tmp, "add", "-A")
    _git(tmp, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    return target


def _run_hook(event, cwd, env):
    proc = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(event),
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc


def _time_calls(fn, n):
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return times


def bench(n: int) -> dict:
    # Live server in-process.
    coordinator.AUTH_TOKEN = None
    coordinator.STORE = ClaimStore(db_path=":memory:", ttl_seconds=900)
    httpd = coordinator.build_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}"

    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        target = _make_repo(tmp)
        idf = os.path.join(tmp, "id")
        base_env = dict(os.environ)
        base_env.update({
            "COORD_URL": base_url,
            "COORD_TIMEOUT": "0.5",
            "COORD_ENABLED": "1",
            "COORD_ID_FILE": idf,
        })
        base_env.pop("COORD_TOKEN", None)
        pre_event = {
            "hook_event_name": "PreToolUse",
            "session_id": "bench",
            "cwd": tmp,
            "tool_name": "Edit",
            "tool_input": {"file_path": target},
        }

        # Warm up (creates id file, primes git).
        _run_hook(pre_event, tmp, base_env)

        # --- end-to-end PreToolUse (server reachable) ---
        results["e2e_reachable"] = _stats(
            _time_calls(lambda: _run_hook(pre_event, tmp, base_env), n)
        )

        # --- end-to-end PreToolUse (server unreachable: fail-open) ---
        down_env = dict(base_env)
        down_env["COORD_URL"] = "http://127.0.0.1:1"
        down_env["COORD_TIMEOUT"] = "0.5"
        results["e2e_failopen"] = _stats(
            _time_calls(lambda: _run_hook(pre_event, tmp, down_env), n)
        )

        # --- component: interpreter startup (import nothing) ---
        results["interpreter"] = _stats(
            _time_calls(
                lambda: subprocess.run([sys.executable, "-c", ""], capture_output=True), n
            )
        )

        # --- component: git subprocess (the combined rev-parse the hook runs) ---
        results["git"] = _stats(
            _time_calls(
                lambda: subprocess.run(
                    ["git", "rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD"],
                    cwd=tmp, capture_output=True, text=True,
                ),
                n,
            )
        )

        # --- component: one network round trip + server handling (warm client) ---
        payload = json.dumps({
            "repo_key": "github.com/acme/api", "file_path": "src/a.ts",
            "session_id": "bench", "machine_id": "m",
        }).encode()

        def _one_request():
            req = urllib.request.Request(
                f"{base_url}/claims/check", data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=1) as r:
                r.read()

        results["network_server"] = _stats(_time_calls(_one_request, n))

    httpd.shutdown()
    httpd.server_close()
    coordinator.STORE.close()

    # Residual = e2e - (interpreter + git + network). Rough, positive-clamped.
    e2e = results["e2e_reachable"]["p50"]
    residual = e2e - (
        results["interpreter"]["p50"]
        + results["git"]["p50"]
        + results["network_server"]["p50"]
    )
    results["residual_imports_p50"] = max(0.0, residual)
    results["_meta"] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "n": n,
        "localhost": True,
        "when": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }
    return results


def _fmt(stats: dict) -> str:
    return f"{stats['p50']:.1f} / {stats['p95']:.1f} / {stats['p99']:.1f}"


def render_markdown(results: dict, label: str) -> str:
    m = results["_meta"]
    lines = []
    lines.append(f"### {label}")
    lines.append("")
    lines.append(
        f"_Python {m['python']}, {m['platform']}, n={m['n']}, "
        f"localhost (no real DNS/TLS), {m['when']}_"
    )
    lines.append("")
    lines.append("| Measurement | p50 (ms) | p95 (ms) | p99 (ms) |")
    lines.append("|---|---|---|---|")
    lines.append(f"| **PreToolUse end-to-end (server up)** | "
                 f"{results['e2e_reachable']['p50']:.1f} | "
                 f"{results['e2e_reachable']['p95']:.1f} | "
                 f"{results['e2e_reachable']['p99']:.1f} |")
    lines.append(f"| **PreToolUse end-to-end (fail-open, server down)** | "
                 f"{results['e2e_failopen']['p50']:.1f} | "
                 f"{results['e2e_failopen']['p95']:.1f} | "
                 f"{results['e2e_failopen']['p99']:.1f} |")
    lines.append("| _component:_ interpreter startup | "
                 f"{results['interpreter']['p50']:.1f} | "
                 f"{results['interpreter']['p95']:.1f} | "
                 f"{results['interpreter']['p99']:.1f} |")
    lines.append("| _component:_ git subprocess | "
                 f"{results['git']['p50']:.1f} | "
                 f"{results['git']['p95']:.1f} | "
                 f"{results['git']['p99']:.1f} |")
    lines.append("| _component:_ network + server handling | "
                 f"{results['network_server']['p50']:.1f} | "
                 f"{results['network_server']['p95']:.1f} | "
                 f"{results['network_server']['p99']:.1f} |")
    lines.append(f"| _derived:_ residual (imports/other) p50 | "
                 f"{results['residual_imports_p50']:.1f} | — | — |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=100, help="iterations per measurement")
    ap.add_argument("--label", default="Run", help="label for this run")
    ap.add_argument("--write", help="append/update this markdown file with the results")
    ap.add_argument("--json", action="store_true", help="print raw JSON too")
    args = ap.parse_args()

    print(f"Running bench (n={args.n}); this shells out {args.n * 4}+ times, ~10-30s...",
          file=sys.stderr)
    results = bench(args.n)
    md = render_markdown(results, args.label)
    print(md)
    if args.json:
        print(json.dumps(results, indent=2))
    if args.write:
        with open(args.write, "a", encoding="utf-8") as fh:
            fh.write("\n" + md + "\n")
        print(f"\nappended to {args.write}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
