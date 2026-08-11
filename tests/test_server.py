"""Integration tests: run the real HTTP server and exercise every endpoint
(spec sections 5, 10). Uses only stdlib (threading + urllib)."""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "server")
sys.path.insert(0, SERVER_DIR)

import coordinator  # noqa: E402
from store import ClaimStore  # noqa: E402


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        coordinator.STORE = ClaimStore(db_path=":memory:", ttl_seconds=900)
        coordinator.AUTH_TOKEN = "test-token"
        cls.httpd = coordinator.build_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        coordinator.STORE.close()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _req(self, method, path, body=None, token="test-token"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._url(path), data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _claim_body(self, session, machine, file="src/a.ts", branch="main"):
        return {
            "repo_key": "github.com/acme/api",
            "file_path": file,
            "session_id": session,
            "machine_id": machine,
            "branch": branch,
        }

    # -- auth -----------------------------------------------------------------

    def test_healthz_needs_no_auth(self):
        status, body = self._req("GET", "/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_missing_token_rejected(self):
        status, _ = self._req("POST", "/claims", self._claim_body("s1", "m1"), token=None)
        self.assertEqual(status, 401)

    def test_wrong_token_rejected(self):
        status, _ = self._req("POST", "/claims", self._claim_body("s1", "m1"), token="nope")
        self.assertEqual(status, 401)

    # -- happy path -----------------------------------------------------------

    def test_register_then_check_conflict(self):
        self._req("POST", "/claims", self._claim_body("owner", "m1"))
        status, body = self._req(
            "POST", "/claims/check",
            {
                "repo_key": "github.com/acme/api",
                "file_path": "src/a.ts",
                "session_id": "other",
                "machine_id": "m2",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["conflicts"]), 1)
        self.assertEqual(body["conflicts"][0]["session_id"], "owner")

    def test_intent_flows_into_conflict(self):
        self._req("POST", "/sessions/intent-sess/intent", {"intent": "wire up rate limiting"})
        self._req("POST", "/claims", self._claim_body("intent-sess", "m9", file="src/rl.ts"))
        _, body = self._req(
            "POST", "/claims/check",
            {
                "repo_key": "github.com/acme/api",
                "file_path": "src/rl.ts",
                "session_id": "reader",
                "machine_id": "m10",
            },
        )
        self.assertEqual(body["conflicts"][0]["intent"], "wire up rate limiting")

    def test_release_clears_conflict(self):
        self._req("POST", "/claims", self._claim_body("rel-sess", "m3", file="src/rel.ts"))
        self._req("POST", "/sessions/rel-sess/release", {})
        _, body = self._req(
            "POST", "/claims/check",
            {
                "repo_key": "github.com/acme/api",
                "file_path": "src/rel.ts",
                "session_id": "x",
                "machine_id": "y",
            },
        )
        self.assertEqual(body["conflicts"], [])

    def test_activity_endpoint(self):
        self._req("POST", "/claims", self._claim_body("act-sess", "m4", file="src/act.ts"))
        status, body = self._req("GET", "/repos/github.com%2Facme%2Fapi/activity")
        self.assertEqual(status, 200)
        files = {c["file_path"] for c in body["claims"]}
        self.assertIn("src/act.ts", files)

    def test_missing_fields_400(self):
        status, body = self._req("POST", "/claims/check", {"repo_key": "x"})
        self.assertEqual(status, 400)
        self.assertIn("missing", body["error"])

    def test_unknown_route_404(self):
        status, _ = self._req("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
