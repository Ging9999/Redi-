"""Unit tests for the settings.json installer merge logic (gap #9).

The merge must be pure, idempotent, preserve existing content, and never
create duplicate hook entries.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hook"))
import install  # noqa: E402


CMD = "python3 /path/to/coordinator_hook.py"
ENV = {"COORD_URL": "http://x", "COORD_TOKEN": "t", "COORD_MODE": "warn", "COORD_TIMEOUT": "0.5"}


class MergeTest(unittest.TestCase):
    def test_installs_all_four_events(self):
        merged, changes = install.merge_settings({}, CMD, 5, ENV)
        for event, _ in install.HOOK_EVENTS:
            self.assertIn(event, merged["hooks"])
        self.assertTrue(any("PreToolUse" in c for c in changes))

    def test_matcher_applied_only_where_expected(self):
        merged, _ = install.merge_settings({}, CMD, 5, ENV)
        pre = merged["hooks"]["PreToolUse"][0]
        self.assertEqual(pre["matcher"], "Edit|Write")
        stop = merged["hooks"]["Stop"][0]
        self.assertNotIn("matcher", stop)

    def test_env_block_merged(self):
        merged, _ = install.merge_settings({}, CMD, 5, ENV)
        self.assertEqual(merged["env"]["COORD_URL"], "http://x")
        self.assertEqual(merged["env"]["COORD_TOKEN"], "t")

    def test_none_env_values_skipped(self):
        env = dict(ENV)
        env["COORD_TOKEN"] = None
        merged, _ = install.merge_settings({}, CMD, 5, env)
        self.assertNotIn("COORD_TOKEN", merged.get("env", {}))

    def test_idempotent(self):
        once, _ = install.merge_settings({}, CMD, 5, ENV)
        twice, changes = install.merge_settings(once, CMD, 5, ENV)
        self.assertEqual(once, twice)
        self.assertEqual(changes, [])

    def test_preserves_existing_hooks_and_keys(self):
        existing = {
            "model": "claude-opus",
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "other.sh"}]}
                ]
            },
        }
        merged, _ = install.merge_settings(existing, CMD, 5, ENV)
        # Original key preserved.
        self.assertEqual(merged["model"], "claude-opus")
        # Original Bash hook preserved, ours appended alongside.
        commands = [
            h["command"]
            for g in merged["hooks"]["PreToolUse"]
            for h in g["hooks"]
        ]
        self.assertIn("other.sh", commands)
        self.assertIn(CMD, commands)

    def test_does_not_mutate_input(self):
        original = {"env": {"KEEP": "1"}}
        install.merge_settings(original, CMD, 5, ENV)
        self.assertEqual(original, {"env": {"KEEP": "1"}})


if __name__ == "__main__":
    unittest.main()
