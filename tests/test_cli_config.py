"""Tests for the CLI settings resolution (B1) and .redi.toml config (B7)."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "hook"))
sys.path.insert(0, os.path.join(HERE, "..", "cli"))
import coordinator_hook as hook  # noqa: E402
import redi as cli  # noqa: E402


class SettingsResolutionTest(unittest.TestCase):
    def test_detects_redi_hooks(self):
        settings = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Edit|Write", "hooks": [
                        {"type": "command", "command": "python3 /x/hook/coordinator_hook.py"}]}
                ],
                "Stop": [{"hooks": [{"command": "python3 /x/hook/coordinator_hook.py"}]}],
            }
        }
        self.assertEqual(cli._hooks_have_redi(settings), {"PreToolUse", "Stop"})

    def test_ignores_unrelated_hooks(self):
        settings = {"hooks": {"PreToolUse": [{"hooks": [{"command": "other.sh"}]}]}}
        self.assertEqual(cli._hooks_have_redi(settings), set())

    def test_precedence_project_local_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".claude"))
            wired = {"hooks": {"PreToolUse": [
                {"hooks": [{"command": "python3 coordinator_hook.py"}]}]}}
            for name in ("settings.json", "settings.local.json"):
                with open(os.path.join(tmp, ".claude", name), "w") as fh:
                    json.dump(wired, fh)
            results = cli._resolve_settings(tmp)
            # project-local is first in precedence order.
            self.assertEqual(results[0][1], "project-local")


class TomlConfigTest(unittest.TestCase):
    def test_parse_top_level(self):
        cfg = hook._parse_toml('url = "http://h:1"\nmode = "block"\ntimeout = 0.75\n')
        self.assertEqual(cfg["url"], "http://h:1")
        self.assertEqual(cfg["mode"], "block")
        self.assertEqual(cfg["timeout"], 0.75)

    def test_parse_redi_table(self):
        cfg = hook._parse_toml('[redi]\nurl = "http://t"\n')
        self.assertEqual(cfg["url"], "http://t")

    def test_resolve_reads_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".redi.toml"), "w") as fh:
                fh.write('url = "http://from-toml:9"\nmode = "ask"\n')
            for k in ("COORD_URL", "COORD_MODE"):
                os.environ.pop(k, None)
            cfg = hook.resolve_config(tmp)
            self.assertEqual(cfg["url"], "http://from-toml:9")
            self.assertEqual(cfg["mode"], "ask")

    def test_env_overrides_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".redi.toml"), "w") as fh:
                fh.write('url = "http://from-toml:9"\n')
            os.environ["COORD_URL"] = "http://from-env:1"
            try:
                cfg = hook.resolve_config(tmp)
                self.assertEqual(cfg["url"], "http://from-env:1")
            finally:
                os.environ.pop("COORD_URL", None)

    def test_no_toml_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            for k in ("COORD_URL", "COORD_MODE", "COORD_TIMEOUT"):
                os.environ.pop(k, None)
            cfg = hook.resolve_config(tmp)
            self.assertEqual(cfg["url"], hook.DEFAULT_URL)
            self.assertEqual(cfg["mode"], "warn")


if __name__ == "__main__":
    unittest.main()
