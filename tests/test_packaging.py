"""Distribution tests (v1.2): join URLs, credentials, version compat, the
PATH-resolved-hook fail-open property (B2), and the redi join flow with rollback."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import redi.server as server  # noqa: E402
from redi import cli, creds  # noqa: E402
from redi.store import ClaimStore  # noqa: E402


class JoinUrlTest(unittest.TestCase):
    def test_parse_full(self):
        info = creds.parse_join_url("redi://Fk3n9xQ2@redi.acme.internal:8787")
        self.assertEqual(info["token"], "Fk3n9xQ2")
        self.assertEqual(info["host"], "redi.acme.internal")
        self.assertEqual(info["port"], 8787)
        self.assertEqual(info["base_url"], "http://redi.acme.internal:8787")

    def test_parse_no_token(self):
        info = creds.parse_join_url("redi://127.0.0.1:8787")
        self.assertIsNone(info["token"])

    def test_parse_https_preserved(self):
        info = creds.parse_join_url("https://tok@redi.example.com")
        self.assertEqual(info["base_url"], "https://redi.example.com:8787")

    def test_roundtrip(self):
        url = creds.build_join_url("abc", "host.internal", 9000)
        self.assertEqual(creds.parse_join_url(url)["token"], "abc")

    def test_bad_url_raises(self):
        with self.assertRaises(ValueError):
            creds.parse_join_url("redi://@:")


class InsecureWarningTest(unittest.TestCase):
    def test_public_http_warns(self):
        self.assertTrue(creds.warn_if_insecure("http://redi.example.com:8787", "redi.example.com"))

    def test_loopback_ok(self):
        self.assertEqual(creds.warn_if_insecure("http://127.0.0.1:8787", "127.0.0.1"), "")

    def test_private_ip_ok(self):
        self.assertEqual(creds.warn_if_insecure("http://10.0.0.5:8787", "10.0.0.5"), "")

    def test_tailscale_ok(self):
        self.assertEqual(creds.warn_if_insecure("http://100.100.1.1:8787", "100.100.1.1"), "")

    def test_internal_hostname_ok(self):
        self.assertEqual(creds.warn_if_insecure("http://redi.internal:8787", "redi.internal"), "")

    def test_https_never_warns(self):
        self.assertEqual(creds.warn_if_insecure("https://redi.example.com", "redi.example.com"), "")


class VersionCompatTest(unittest.TestCase):
    def test_same_major_ok(self):
        from redi import __version__
        self.assertEqual(cli.check_version(__version__), "")

    def test_major_mismatch_warns(self):
        self.assertIn("mismatch", cli.check_version("99.0.0"))


class CredentialsFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["COORD_CONFIG_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("COORD_CONFIG_DIR", None)

    def test_save_get_remove(self):
        creds.save_credential("h1", "tok1", "http://h1:8787")
        self.assertEqual(creds.get_credential("h1")["token"], "tok1")
        self.assertTrue(creds.remove_credential("h1"))
        self.assertIsNone(creds.get_credential("h1"))

    def test_file_is_0600(self):
        creds.save_credential("h1", "tok1", "http://h1:8787")
        mode = os.stat(creds.credentials_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class ServerTokenTest(unittest.TestCase):
    def test_generate_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["COORD_DATA_DIR"] = tmp
            os.environ.pop("COORD_TOKEN", None)
            try:
                tok1, created1 = creds.load_or_create_server_token()
                tok2, created2 = creds.load_or_create_server_token()
                self.assertTrue(created1)
                self.assertFalse(created2)     # persisted, reused
                self.assertEqual(tok1, tok2)
                self.assertTrue(os.path.exists(os.path.join(tmp, "token")))
            finally:
                os.environ.pop("COORD_DATA_DIR", None)

    def test_env_token_wins(self):
        os.environ["COORD_TOKEN"] = "configured"
        try:
            tok, created = creds.load_or_create_server_token()
            self.assertEqual(tok, "configured")
            self.assertFalse(created)
        finally:
            os.environ.pop("COORD_TOKEN", None)


class MissingBinaryFailOpenTest(unittest.TestCase):
    """B2 acceptance: a committed `redi hook ...` command, on a machine where
    `redi` isn't installed, must NOT block the edit. Claude Code blocks a
    PreToolUse only on exit code 2; command-not-found is 127, so the edit
    proceeds — silently, as fail-open intends."""

    def test_command_not_found_is_not_a_block(self):
        env = dict(os.environ, PATH="")  # nothing on PATH -> `redi` not found
        proc = subprocess.run(
            ["/bin/sh", "-c", "redi hook pre-tool-use"],
            input="{}", capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(proc.returncode, 2)   # 2 would block the edit
        self.assertEqual(proc.returncode, 127)    # command-not-found


class JoinFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.AUTH_TOKEN = "secret-token"
        server.STORE = ClaimStore(db_path=":memory:")
        cls.httpd = server.build_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.settings = os.path.join(tempfile.mkdtemp(), "settings.json")
        os.environ["COORD_CONFIG_DIR"] = self.cfg
        for k in ("COORD_TOKEN", "COORD_URL"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.pop("COORD_CONFIG_DIR", None)

    def _join(self, url):
        args = argparse.Namespace(url=url, settings=self.settings)
        return cli.cmd_join(args)

    def test_join_success_writes_creds_and_hooks(self):
        rc = self._join(f"redi://secret-token@127.0.0.1:{self.port}")
        self.assertEqual(rc, 0)
        cred = creds.get_credential("127.0.0.1")
        self.assertEqual(cred["token"], "secret-token")
        with open(self.settings) as fh:
            data = json.load(fh)
        cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, "redi hook pre-tool-use")
        # Token must NOT be baked into settings.json.
        self.assertNotIn("secret-token", json.dumps(data))

    def test_bad_token_rolls_back(self):
        rc = self._join(f"redi://wrong@127.0.0.1:{self.port}")
        self.assertEqual(rc, 1)
        self.assertIsNone(creds.get_credential("127.0.0.1"))
        self.assertFalse(os.path.exists(self.settings))

    def test_unreachable_rolls_back(self):
        rc = self._join("redi://tok@127.0.0.1:1")   # closed port
        self.assertEqual(rc, 1)
        self.assertIsNone(creds.get_credential("127.0.0.1"))
        self.assertFalse(os.path.exists(self.settings))


if __name__ == "__main__":
    unittest.main()
