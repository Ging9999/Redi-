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
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hook", "coordinator_hook.py")
sys.path.insert(0, os.path.join(ROOT, "server"))

import coordinator  # noqa: E402
from store import ClaimStore  # noqa: E402


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
        env.pop("COORD_TOKEN", None)
        # Isolate machine id per subprocess so we can simulate two machines.
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, HOOK],
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


if __name__ == "__main__":
    unittest.main()
