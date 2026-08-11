"""In-process tests for v1.1 hook behaviour: zero-subprocess hot path (A1),
quiet backoff (A3), refresh debounce (A4), warning suppression (B4), and the
force/override path (B5)."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hook"))
import coordinator_hook as hook  # noqa: E402


class HookV11Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["COORD_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        for k in ("COORD_MODE", "COORD_FORCE", "COORD_FORCE_REASON",
                  "COORD_SUPPRESS_SECONDS", "COORD_URL", "COORD_TIMEOUT"):
            os.environ.pop(k, None)
        self.calls = []
        self._orig_request = hook._request
        hook._request = self._fake_request
        self.response = {"conflicts": [], "quiet_until": 0, "overrides": []}

    def tearDown(self):
        hook._request = self._orig_request
        os.environ.pop("COORD_CACHE_DIR", None)

    def _fake_request(self, method, url, payload, token, timeout):
        self.calls.append((method, url, payload))
        return dict(self.response)

    def _write_cache(self, session="s", **overrides):
        cache = {
            "session_id": session,
            "cwd": self.tmp,
            "repo_key": "github.com/acme/api",
            "root": self.tmp,
            "branch": "main",
            "branch_checked_at": time.time(),
            "display_name": "Tester",
            "machine_id": "m1",
            "config": {"url": "http://x", "mode": "warn", "timeout": 0.5,
                       "ttl_seconds": 900},
            "quiet_until": 0,
            "refresh": {},
            "warned": {},
        }
        cache.update(overrides)
        hook.save_cache(session, cache)
        return cache

    def _event(self, name, session="s", file="a.ts"):
        return hook.Ctx({
            "hook_event_name": name,
            "session_id": session,
            "cwd": self.tmp,
            "tool_input": {"file_path": os.path.join(self.tmp, file)},
        })

    def _conflict(self, session="other", intent="add rate limiting"):
        return {
            "session_id": session, "display_name": "Sam", "branch": "feat/x",
            "intent": intent, "age_seconds": 30, "intent_age_seconds": 30,
        }


class ZeroSubprocessTest(HookV11Base):
    """A1 acceptance: the steady-state Edit path makes no subprocess call."""

    def setUp(self):
        super().setUp()
        self._orig_run = subprocess.run
        subprocess.run = self._boom

    def tearDown(self):
        subprocess.run = self._orig_run
        super().tearDown()

    def _boom(self, *a, **k):
        raise AssertionError("subprocess.run must not be called on the hot path")

    def test_pretooluse_no_subprocess(self):
        self._write_cache(session="s")
        rc = hook.handle_pre_tool_use(self._event("PreToolUse", "s"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)  # exactly one request (acquire)
        self.assertTrue(self.calls[0][1].endswith("/claims/acquire"))

    def test_posttooluse_no_subprocess(self):
        self._write_cache(session="s", refresh={})
        rc = hook.handle_post_tool_use(self._event("PostToolUse", "s"))
        self.assertEqual(rc, 0)


class QuietBackoffTest(HookV11Base):
    """A3: inside the quiet window, no network call at all."""

    def test_pre_skips_network_when_quiet(self):
        self._write_cache(session="s", quiet_until=time.time() + 100)
        rc = hook.handle_pre_tool_use(self._event("PreToolUse", "s"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [])  # zero requests

    def test_post_skips_network_when_quiet(self):
        self._write_cache(session="s", quiet_until=time.time() + 100)
        hook.handle_post_tool_use(self._event("PostToolUse", "s"))
        self.assertEqual(self.calls, [])

    def test_solo_acquire_sets_quiet_and_next_edits_are_free(self):
        self._write_cache(session="s")
        self.response = {"conflicts": [], "quiet_until": time.time() + 100,
                         "overrides": []}
        hook.handle_pre_tool_use(self._event("PreToolUse", "s"))
        self.assertEqual(len(self.calls), 1)
        # Next 10 edits should make no further requests.
        for _ in range(10):
            hook.handle_pre_tool_use(self._event("PreToolUse", "s"))
        self.assertEqual(len(self.calls), 1)


class RefreshDebounceTest(HookV11Base):
    """A4: refresh at most once per TTL/3, not per edit."""

    def test_recent_refresh_is_skipped(self):
        self._write_cache(session="s", refresh={"a.ts": time.time()})
        hook.handle_post_tool_use(self._event("PostToolUse", "s"))
        self.assertEqual(self.calls, [])

    def test_stale_refresh_fires_once(self):
        # ttl 900 -> interval 300s; last refresh 400s ago -> should fire.
        self._write_cache(session="s", refresh={"a.ts": time.time() - 400})
        hook.handle_post_tool_use(self._event("PostToolUse", "s"))
        self.assertEqual(len(self.calls), 1)
        # Immediately after, a second edit is debounced.
        hook.handle_post_tool_use(self._event("PostToolUse", "s"))
        self.assertEqual(len(self.calls), 1)


class SuppressionTest(HookV11Base):
    """B4: 20 edits to a contested file => one warning."""

    def test_repeat_warning_suppressed(self):
        self._write_cache(session="s")
        self.response = {"conflicts": [self._conflict()], "quiet_until": None,
                         "overrides": []}
        codes = [hook.handle_pre_tool_use(self._event("PreToolUse", "s"))
                 for _ in range(20)]
        self.assertEqual(codes[0], 2)             # first warns
        self.assertTrue(all(c == 0 for c in codes[1:]))  # rest suppressed

    def test_intent_change_rewarns(self):
        self._write_cache(session="s")
        self.response = {"conflicts": [self._conflict(intent="v1")],
                         "quiet_until": None, "overrides": []}
        self.assertEqual(hook.handle_pre_tool_use(self._event("PreToolUse", "s")), 2)
        self.assertEqual(hook.handle_pre_tool_use(self._event("PreToolUse", "s")), 0)
        # Intent changes -> genuinely new info -> re-warn.
        self.response = {"conflicts": [self._conflict(intent="v2 different")],
                         "quiet_until": None, "overrides": []}
        self.assertEqual(hook.handle_pre_tool_use(self._event("PreToolUse", "s")), 2)


class ForceTest(HookV11Base):
    """B5: COORD_FORCE proceeds and records an override note."""

    def test_force_proceeds_and_records_override(self):
        self._write_cache(session="s")
        self.response = {"conflicts": [self._conflict(session="victim")],
                         "quiet_until": None, "overrides": []}
        os.environ["COORD_FORCE"] = "1"
        os.environ["COORD_FORCE_REASON"] = "prod hotfix"
        try:
            rc = hook.handle_pre_tool_use(self._event("PreToolUse", "s"))
        finally:
            os.environ.pop("COORD_FORCE", None)
            os.environ.pop("COORD_FORCE_REASON", None)
        self.assertEqual(rc, 0)  # proceeds
        overrides = [c for c in self.calls if c[1].endswith("/claims/override")]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0][2]["target_session"], "victim")
        self.assertEqual(overrides[0][2]["reason"], "prod hotfix")


if __name__ == "__main__":
    unittest.main()
