"""Installer merge/uninstall tests: PATH-resolved commands (B2), idempotency,
non-destructiveness, and clean removal (B4)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from redi import install  # noqa: E402


ENV = {"COORD_URL": "http://x", "COORD_MODE": "warn"}


class MergeTest(unittest.TestCase):
    def test_installs_all_events_path_resolved(self):
        merged, changes = install.merge_settings({}, install.DEFAULT_PREFIX, 5, ENV)
        for event, kebab, _ in install.HOOK_EVENTS:
            self.assertIn(event, merged["hooks"])
            cmd = merged["hooks"][event][0]["hooks"][0]["command"]
            self.assertEqual(cmd, f"redi hook {kebab}")

    def test_commands_contain_no_absolute_path(self):
        # The whole point of B2: a committed settings.json must be portable.
        merged, _ = install.merge_settings({}, install.DEFAULT_PREFIX, 5, {})
        for event, _, _ in install.HOOK_EVENTS:
            cmd = merged["hooks"][event][0]["hooks"][0]["command"]
            self.assertNotIn("/", cmd.replace("redi hook", ""))
            self.assertFalse(cmd.startswith("/"))

    def test_matcher_only_on_tool_events(self):
        merged, _ = install.merge_settings({}, install.DEFAULT_PREFIX, 5, {})
        self.assertEqual(merged["hooks"]["PreToolUse"][0]["matcher"], "Edit|Write")
        self.assertNotIn("matcher", merged["hooks"]["Stop"][0])

    def test_idempotent(self):
        once, _ = install.merge_settings({}, install.DEFAULT_PREFIX, 5, ENV)
        twice, changes = install.merge_settings(once, install.DEFAULT_PREFIX, 5, ENV)
        self.assertEqual(once, twice)
        self.assertEqual(changes, [])

    def test_no_env_writes_hooks_only(self):
        merged, _ = install.merge_settings({}, install.DEFAULT_PREFIX, 5, {})
        self.assertNotIn("env", merged)

    def test_preserves_existing(self):
        existing = {
            "model": "claude-opus",
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "other.sh"}]}]},
        }
        merged, _ = install.merge_settings(existing, install.DEFAULT_PREFIX, 5, ENV)
        self.assertEqual(merged["model"], "claude-opus")
        cmds = [h["command"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertIn("other.sh", cmds)
        self.assertIn("redi hook pre-tool-use", cmds)


class UninstallTest(unittest.TestCase):
    def test_removes_only_redi(self):
        merged, _ = install.merge_settings(
            {"model": "x", "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"command": "keep.sh"}]}]}},
            install.DEFAULT_PREFIX, 5, ENV)
        cleaned, changes = install.uninstall_settings(merged)
        self.assertTrue(changes)
        self.assertEqual(cleaned["model"], "x")
        # The unrelated Bash hook survives.
        cmds = [h["command"] for g in cleaned["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertIn("keep.sh", cmds)
        self.assertNotIn("redi hook pre-tool-use", cmds)
        # COORD_* env removed.
        self.assertNotIn("env", cleaned)

    def test_uninstall_idempotent(self):
        merged, _ = install.merge_settings({}, install.DEFAULT_PREFIX, 5, ENV)
        once, _ = install.uninstall_settings(merged)
        twice, changes = install.uninstall_settings(once)
        self.assertEqual(changes, [])

    def test_uninstall_restores_empty(self):
        merged, _ = install.merge_settings({}, install.DEFAULT_PREFIX, 5, {})
        cleaned, _ = install.uninstall_settings(merged)
        self.assertEqual(cleaned, {})

    def test_removes_legacy_absolute_path_hooks(self):
        legacy = {"hooks": {"PreToolUse": [
            {"matcher": "Edit|Write",
             "hooks": [{"command": "python3 /abs/coordinator_hook.py"}]}]}}
        cleaned, changes = install.uninstall_settings(legacy)
        self.assertTrue(changes)
        self.assertNotIn("hooks", cleaned)


if __name__ == "__main__":
    unittest.main()
