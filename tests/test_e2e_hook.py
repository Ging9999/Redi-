"""End-to-end: drive the hook script exactly as Claude Code would — pipe event
JSON to stdin, check the exit code — against a live server in a real git repo.

Covers the spec's section-10 scenarios that span hook + server together:
conflict reporting across branches, no-conflict on different files, self is not
a conflict, release on Stop, and fail-open when the server is unreachable.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import redi.server as coordinator  # noqa: E402
from redi.store import ClaimStore  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp, remote="git@github.com:acme/api.git"):
    _git(tmp, "init", "-q")
    _git(tmp, "remote", "add", "origin", remote)
    _git(tmp, "config", "user.name", "Test Dev")
    _git(tmp, "config", "user.email", "dev@example.com")
    fp = os.path.join(tmp, "src")
    os.makedirs(fp, exist_ok=True)
    target = os.path.join(fp, "a.ts")
    with open(target, "w") as fh:
        fh.write("// file\n")
    return target


class E2EHookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        coordinator.AUTH_TOKEN = None  # open mode for the test server
        cls.httpd = coordinator.build_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        # Fresh store per test — the handler reads the module global at request
        # time, so reassigning here fully isolates each test's claims.
        if coordinator.STORE is not None:
            coordinator.STORE.close()
        coordinator.STORE = ClaimStore(db_path=":memory:", ttl_seconds=900)

    def _run_hook(self, event, cwd, extra_env=None, url=None):
        env = dict(os.environ)
        env["COORD_URL"] = url or self.base_url
        env["COORD_TIMEOUT"] = "2.0"
        env["COORD_ENABLED"] = "1"
        # Isolate the session cache per test (each test has its own tmp repo).
        env["COORD_CACHE_DIR"] = os.path.join(cwd, ".redi-cache")
        env.pop("COORD_TOKEN", None)
        # Make the `redi` package importable from any cwd (as an installed
        # console script would be on PATH).
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        # Isolate machine id per subprocess so we can simulate two machines.
        if extra_env:
            env.update(extra_env)
        # Invoke exactly as the PATH-resolved hook does: `redi hook` reading the
        # event name from stdin's hook_event_name (event arg omitted here).
        proc = subprocess.run(
            [sys.executable, "-m", "redi", "hook"],
            input=json.dumps(event),
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc

    def _event(self, name, cwd, target=None, session="s", branch=None):
        ev = {"hook_event_name": name, "session_id": session, "cwd": cwd}
        if target:
            ev["tool_name"] = "Edit"
            ev["tool_input"] = {"file_path": target}
        return ev

    def test_full_flow_two_machines_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _make_repo(tmp)
            id_a = os.path.join(tmp, "id_a")
            id_b = os.path.join(tmp, "id_b")

            # Agent A submits intent, then edits (PostToolUse registers a claim).
            self._run_hook(
                self._event("UserPromptSubmit", tmp, session="A"),
                tmp,
                extra_env={"COORD_ID_FILE": id_a},
            )
            # Attach the prompt for intent.
            ev_intent = {"hook_event_name": "UserPromptSubmit", "session_id": "A",
                         "cwd": tmp, "prompt": "add per-IP rate limiting"}
            self._run_hook(ev_intent, tmp, extra_env={"COORD_ID_FILE": id_a})
            self._run_hook(
                self._event("PostToolUse", tmp, target=target, session="A"),
                tmp,
                extra_env={"COORD_ID_FILE": id_a},
            )

            # Agent B (different machine id) does PreToolUse on the same file.
            proc = self._run_hook(
                self._event("PreToolUse", tmp, target=target, session="B"),
                tmp,
                extra_env={"COORD_ID_FILE": id_b},
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("Another agent is editing this file", proc.stderr)
            self.assertIn("per-IP rate limiting", proc.stderr)

    def test_self_is_not_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _make_repo(tmp)
            idf = os.path.join(tmp, "id")
            self._run_hook(
                self._event("PostToolUse", tmp, target=target, session="A"),
                tmp, extra_env={"COORD_ID_FILE": idf},
            )
            # Same session + same machine re-editing -> no conflict.
            proc = self._run_hook(
                self._event("PreToolUse", tmp, target=target, session="A"),
                tmp, extra_env={"COORD_ID_FILE": idf},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_different_files_no_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            other = os.path.join(tmp, "src", "b.ts")
            with open(other, "w") as fh:
                fh.write("// b\n")
            a_target = os.path.join(tmp, "src", "a.ts")
            self._run_hook(
                self._event("PostToolUse", tmp, target=a_target, session="A"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_a")},
            )
            proc = self._run_hook(
                self._event("PreToolUse", tmp, target=other, session="B"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_b")},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_stop_releases_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _make_repo(tmp)
            self._run_hook(
                self._event("PostToolUse", tmp, target=target, session="A"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_a")},
            )
            self._run_hook(
                self._event("Stop", tmp, session="A"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_a")},
            )
            # After release, B sees no conflict.
            proc = self._run_hook(
                self._event("PreToolUse", tmp, target=target, session="B"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_b")},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_server_unreachable_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _make_repo(tmp)
            # Point at a closed port; edit must proceed (exit 0).
            proc = self._run_hook(
                self._event("PreToolUse", tmp, target=target, session="B"),
                tmp,
                extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_b")},
                url="http://127.0.0.1:1",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_path_outside_repo_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            proc = self._run_hook(
                self._event("PreToolUse", tmp, target="/etc/passwd", session="B"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_b")},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_pretooluse_stakes_claim_without_posttooluse(self):
        # The race-narrowing behaviour: A's PreToolUse (no conflict) stakes the
        # claim immediately, so B's PreToolUse sees it even though A never ran
        # PostToolUse (i.e. A hasn't finished — or even started — its edit).
        with tempfile.TemporaryDirectory() as tmp:
            target = _make_repo(tmp)
            a = self._run_hook(
                self._event("PreToolUse", tmp, target=target, session="A"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_a")},
            )
            self.assertEqual(a.returncode, 0, a.stderr)  # A saw no conflict, staked
            b = self._run_hook(
                self._event("PreToolUse", tmp, target=target, session="B"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_b")},
            )
            self.assertEqual(b.returncode, 2, b.stderr)  # B now sees A's stake

    def test_ask_mode_escalates_to_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _make_repo(tmp)
            self._run_hook(
                self._event("PostToolUse", tmp, target=target, session="A"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_a")},
            )
            proc = self._run_hook(
                self._event("PreToolUse", tmp, target=target, session="B"),
                tmp,
                extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_b"), "COORD_MODE": "ask"},
            )
            # ask mode: exit 0, decision JSON on stdout (parsed only on exit 0).
            self.assertEqual(proc.returncode, 0, proc.stderr)
            decision = json.loads(proc.stdout)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "ask"
            )

    def test_block_mode_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _make_repo(tmp)
            self._run_hook(
                self._event("PostToolUse", tmp, target=target, session="A"),
                tmp, extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_a")},
            )
            proc = self._run_hook(
                self._event("PreToolUse", tmp, target=target, session="B"),
                tmp,
                extra_env={"COORD_ID_FILE": os.path.join(tmp, "id_b"), "COORD_MODE": "block"},
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("[block mode]", proc.stderr)

    def test_slow_server_times_out_and_fails_open(self):
        # Spec section 10: "Server slow (3s) -> hook times out and fails open."
        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                time.sleep(3.0)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"conflicts": []}')

            def log_message(self, *a):
                pass

        slow = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        port = slow.server_address[1]
        t = threading.Thread(target=slow.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = _make_repo(tmp)
                start = time.time()
                proc = self._run_hook(
                    self._event("PreToolUse", tmp, target=target, session="B"),
                    tmp,
                    extra_env={
                        "COORD_ID_FILE": os.path.join(tmp, "id_b"),
                        "COORD_TIMEOUT": "0.4",  # well under the 3s server delay
                    },
                    url=f"http://127.0.0.1:{port}",
                )
                elapsed = time.time() - start
                self.assertEqual(proc.returncode, 0, proc.stderr)  # failed open
                self.assertLess(elapsed, 2.5, "hook should not wait for the slow server")
        finally:
            slow.shutdown()
            slow.server_close()


if __name__ == "__main__":
    unittest.main()
