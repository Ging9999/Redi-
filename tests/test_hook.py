"""Unit tests for the hook client's pure helpers (spec sections 2, 10).

These cover the parts that break most often: remote-URL normalization and
file-path normalization across POSIX/Windows separators and clone locations.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import redi.hook as hook  # noqa: E402


class RemoteNormalizationTest(unittest.TestCase):
    def test_scp_and_https_collapse_to_same_key(self):
        a = hook.normalize_remote("git@github.com:acme/api.git")
        b = hook.normalize_remote("https://github.com/acme/api")
        self.assertEqual(a, b)
        self.assertEqual(a, "github.com/acme/api")

    def test_trailing_git_stripped(self):
        self.assertEqual(
            hook.normalize_remote("https://github.com/acme/api.git"),
            "github.com/acme/api",
        )

    def test_case_insensitive(self):
        self.assertEqual(
            hook.normalize_remote("https://GitHub.com/Acme/API"),
            "github.com/acme/api",
        )

    def test_credentials_stripped(self):
        self.assertEqual(
            hook.normalize_remote("https://user:token@github.com/acme/api.git"),
            "github.com/acme/api",
        )

    def test_ssh_protocol_form(self):
        self.assertEqual(
            hook.normalize_remote("ssh://git@github.com/acme/api.git"),
            "github.com/acme/api",
        )


class FilePathNormalizationTest(unittest.TestCase):
    def test_posix_relative(self):
        rel = hook.normalize_file_path(
            "/home/sam/code/api/src/auth/middleware.ts", "/home/sam/code/api"
        )
        self.assertEqual(rel, "src/auth/middleware.ts")

    def test_different_clone_locations_same_rel(self):
        # Same repo cloned to different paths on each machine -> same claim key.
        a = hook.normalize_file_path(
            "/home/sam/code/api/src/a.ts", "/home/sam/code/api"
        )
        b = hook.normalize_file_path(
            "/Users/priya/dev/api/src/a.ts", "/Users/priya/dev/api"
        )
        self.assertEqual(a, b)

    def test_windows_separators_normalize(self):
        rel = hook.normalize_file_path(
            r"C:\Users\priya\api\src\a.ts", r"C:\Users\priya\api"
        )
        self.assertEqual(rel, "src/a.ts")

    def test_windows_and_posix_same_claim(self):
        posix = hook.normalize_file_path("/home/sam/api/src/a.ts", "/home/sam/api")
        win = hook.normalize_file_path(r"C:\dev\api\src\a.ts", r"C:\dev\api")
        self.assertEqual(posix, win)

    def test_path_outside_repo_rejected(self):
        rel = hook.normalize_file_path("/etc/passwd", "/home/sam/api")
        self.assertIsNone(rel)

    def test_empty_inputs_rejected(self):
        self.assertIsNone(hook.normalize_file_path("", "/home/sam/api"))
        self.assertIsNone(hook.normalize_file_path("/home/sam/api/a.ts", ""))


class ModeNormalizationTest(unittest.TestCase):
    def test_valid_modes_passthrough(self):
        self.assertEqual(hook._normalize_mode("warn"), "warn")
        self.assertEqual(hook._normalize_mode("ASK"), "ask")
        self.assertEqual(hook._normalize_mode(" block "), "block")

    def test_invalid_mode_falls_back_to_warn(self):
        self.assertEqual(hook._normalize_mode("nonsense"), "warn")
        self.assertEqual(hook._normalize_mode(""), "warn")


class MessageFormattingTest(unittest.TestCase):
    def test_message_includes_intent_branch_and_age(self):
        msg = hook._format_conflict_message(
            [
                {
                    "display_name": "Sam",
                    "branch": "feat/rate-limit",
                    "intent": "add per-IP rate limiting to the auth middleware",
                    "age_seconds": 240,
                }
            ]
        )
        self.assertIn("Sam", msg)
        self.assertIn("feat/rate-limit", msg)
        self.assertIn("per-IP rate limiting", msg)
        self.assertIn("4 minutes ago", msg)

    def test_humanize_age(self):
        self.assertEqual(hook._humanize_age(3), "just now")
        self.assertEqual(hook._humanize_age(45), "45s ago")
        self.assertEqual(hook._humanize_age(60), "1 minute ago")
        self.assertEqual(hook._humanize_age(240), "4 minutes ago")
        self.assertEqual(hook._humanize_age(3600), "1 hour ago")


if __name__ == "__main__":
    unittest.main()
