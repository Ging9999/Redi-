"""Unit tests for the claim store (spec sections 4, 6, 10)."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
from store import ClaimStore  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = ClaimStore(db_path=":memory:", ttl_seconds=900)

    def tearDown(self):
        self.store.close()

    def _reg(self, session, machine, file="src/a.ts", branch="main", **kw):
        return self.store.register(
            repo_key="github.com/acme/api",
            file_path=file,
            session_id=session,
            machine_id=machine,
            branch=branch,
            **kw,
        )

    def test_same_file_same_branch_is_conflict(self):
        self._reg("s1", "m1", branch="main")
        conflicts = self.store.check("github.com/acme/api", "src/a.ts", "s2", "m2")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].session_id, "s1")

    def test_same_file_different_branch_is_conflict(self):
        # The whole point of the tool (spec section 10).
        self._reg("s1", "m1", branch="feat/rate-limit")
        conflicts = self.store.check("github.com/acme/api", "src/a.ts", "s2", "m2")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].branch, "feat/rate-limit")

    def test_different_files_no_conflict(self):
        self._reg("s1", "m1", file="src/a.ts")
        conflicts = self.store.check("github.com/acme/api", "src/b.ts", "s2", "m2")
        self.assertEqual(conflicts, [])

    def test_self_is_not_a_conflict(self):
        self._reg("s1", "m1")
        conflicts = self.store.check("github.com/acme/api", "src/a.ts", "s1", "m1")
        self.assertEqual(conflicts, [])

    def test_same_machine_different_session_is_conflict(self):
        # A developer running two concurrent sessions -> distinct agents.
        self._reg("s1", "m1")
        conflicts = self.store.check("github.com/acme/api", "src/a.ts", "s2", "m1")
        self.assertEqual(len(conflicts), 1)

    def test_expired_claim_is_swept(self):
        store = ClaimStore(db_path=":memory:", ttl_seconds=1)
        store.register("github.com/acme/api", "src/a.ts", "s1", "m1", ttl_seconds=-1)
        conflicts = store.check("github.com/acme/api", "src/a.ts", "s2", "m2")
        self.assertEqual(conflicts, [])
        store.close()

    def test_refresh_extends_expiry(self):
        c1 = self._reg("s1", "m1", ttl_seconds=10)
        time.sleep(0.01)
        c2 = self._reg("s1", "m1", ttl_seconds=20)
        self.assertGreater(c2.expires_at, c1.expires_at)
        # created_at is preserved across refresh.
        self.assertEqual(c1.created_at, c2.created_at)

    def test_release_drops_all_session_claims(self):
        self._reg("s1", "m1", file="src/a.ts")
        self._reg("s1", "m1", file="src/b.ts")
        removed = self.store.release("s1")
        self.assertEqual(removed, 2)
        self.assertEqual(self.store.activity("github.com/acme/api"), [])

    def test_intent_before_claim_is_applied(self):
        # UserPromptSubmit can arrive before the first edit.
        self.store.set_intent("s1", "add per-IP rate limiting")
        claim = self._reg("s1", "m1")
        self.assertEqual(claim.intent, "add per-IP rate limiting")

    def test_intent_update_propagates_to_existing_claims(self):
        self._reg("s1", "m1")
        self.store.set_intent("s1", "refactor the auth middleware")
        claims = self.store.activity("github.com/acme/api")
        self.assertEqual(claims[0].intent, "refactor the auth middleware")

    def test_explicit_intent_overrides_session_intent(self):
        self.store.set_intent("s1", "session-level")
        claim = self._reg("s1", "m1", intent="explicit-level")
        self.assertEqual(claim.intent, "explicit-level")

    def test_activity_lists_all_repo_claims(self):
        self._reg("s1", "m1", file="src/a.ts")
        self._reg("s2", "m2", file="src/b.ts")
        claims = self.store.activity("github.com/acme/api")
        self.assertEqual(len(claims), 2)

    def test_age_seconds_reported(self):
        claim = self._reg("s1", "m1")
        self.assertIn("age_seconds", claim.to_dict())
        self.assertGreaterEqual(claim.to_dict()["age_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
