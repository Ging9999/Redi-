"""Tests for v1.1 store features: atomic acquire (A2), quiet backoff (A3),
SQLite index use (A6), coverage/contributors (B2), overrides (B5), intent
staleness (B6)."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
from store import ClaimStore  # noqa: E402


class AcquireTest(unittest.TestCase):
    def setUp(self):
        self.store = ClaimStore(db_path=":memory:", ttl_seconds=900, quiet_seconds=60)

    def tearDown(self):
        self.store.close()

    def test_acquire_stakes_and_reports_no_conflict(self):
        r = self.store.acquire("repo", "f.ts", "s1", "m1", branch="main")
        self.assertEqual(r["conflicts"], [])
        self.assertEqual(r["claim"]["file_path"], "f.ts")

    def test_acquire_is_atomic_second_sees_first(self):
        self.store.acquire("repo", "f.ts", "s1", "m1")
        r2 = self.store.acquire("repo", "f.ts", "s2", "m2")
        self.assertEqual(len(r2["conflicts"]), 1)
        self.assertEqual(r2["conflicts"][0]["session_id"], "s1")

    def test_solo_session_gets_quiet_until(self):
        r = self.store.acquire("repo", "f.ts", "s1", "m1")
        self.assertIsNotNone(r["quiet_until"])
        self.assertGreater(r["quiet_until"], time.time())

    def test_second_session_revokes_quiet(self):
        self.store.acquire("repo", "a.ts", "s1", "m1")
        r2 = self.store.acquire("repo", "b.ts", "s2", "m2")
        # s2 is not solo — s1 is active on the repo.
        self.assertIsNone(r2["quiet_until"])
        # And a fresh acquire by s1 now also sees it's not solo.
        r1b = self.store.acquire("repo", "a.ts", "s1", "m1")
        self.assertIsNone(r1b["quiet_until"])


class IntentStalenessTest(unittest.TestCase):
    def setUp(self):
        self.store = ClaimStore(db_path=":memory:")

    def tearDown(self):
        self.store.close()

    def test_intent_age_seconds_present(self):
        self.store.set_intent("s1", "do the thing")
        r = self.store.acquire("repo", "f.ts", "s2", "m2")  # someone else checks
        self.store.acquire("repo", "f.ts", "s1", "m1")
        r2 = self.store.acquire("repo", "f.ts", "s3", "m3")
        conflict = next(c for c in r2["conflicts"] if c["session_id"] == "s1")
        self.assertIn("intent_age_seconds", conflict)
        self.assertEqual(conflict["intent"], "do the thing")

    def test_reintent_resets_age(self):
        self.store.set_intent("s1", "old")
        self.store.acquire("repo", "f.ts", "s1", "m1")
        time.sleep(0.02)
        self.store.set_intent("s1", "new and current")
        claims = self.store.activity("repo")
        c = claims[0].to_dict()
        self.assertEqual(c["intent"], "new and current")
        self.assertLess(c["intent_age_seconds"], 5)


class ContributorsTest(unittest.TestCase):
    def setUp(self):
        self.store = ClaimStore(db_path=":memory:")

    def tearDown(self):
        self.store.close()

    def test_contributors_recorded_and_listed(self):
        self.store.acquire("repo", "f.ts", "s1", "m1", display_name="Sam")
        self.store.acquire("repo", "g.ts", "s2", "m2", display_name="Priya")
        names = {c["display_name"] for c in self.store.contributors("repo", 86400)}
        self.assertEqual(names, {"Sam", "Priya"})

    def test_contributor_outside_window_excluded(self):
        self.store.acquire("repo", "f.ts", "s1", "m1", display_name="Sam")
        # Zero-second window excludes everything (last_seen is in the past).
        time.sleep(0.02)
        self.assertEqual(self.store.contributors("repo", 0), [])

    def test_anonymous_not_recorded(self):
        self.store.acquire("repo", "f.ts", "s1", "m1", display_name="")
        self.assertEqual(self.store.contributors("repo", 86400), [])


class OverrideTest(unittest.TestCase):
    def setUp(self):
        self.store = ClaimStore(db_path=":memory:")

    def tearDown(self):
        self.store.close()

    def test_override_surfaced_to_target_once(self):
        # s1 holds a claim; s2 overrides it with a reason.
        self.store.acquire("repo", "f.ts", "s1", "m1")
        self.store.record_override("repo", "f.ts", target_session="s1",
                                   overrider_name="Priya", reason="hotfix must ship")
        # s1's next acquire surfaces the override.
        r = self.store.acquire("repo", "f.ts", "s1", "m1")
        self.assertEqual(len(r["overrides"]), 1)
        self.assertEqual(r["overrides"][0]["overrider_name"], "Priya")
        self.assertIn("hotfix", r["overrides"][0]["reason"])
        # Consumed — not surfaced again.
        r2 = self.store.acquire("repo", "f.ts", "s1", "m1")
        self.assertEqual(r2["overrides"], [])

    def test_override_not_shown_to_others(self):
        self.store.record_override("repo", "f.ts", target_session="s1", reason="x")
        r = self.store.acquire("repo", "f.ts", "s9", "m9")
        self.assertEqual(r["overrides"], [])


class QueryPlanTest(unittest.TestCase):
    """A6: every hot query path must hit an index, not full-scan."""

    def setUp(self):
        self.store = ClaimStore(db_path=":memory:")
        # Populate so the planner has a real table.
        for i in range(50):
            self.store.acquire("repo", f"f{i}.ts", f"s{i}", f"m{i}", display_name="X")

    def tearDown(self):
        self.store.close()

    def _plan(self, sql, params):
        rows = self.store._conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return " ".join(r["detail"] for r in rows)

    def test_conflict_lookup_uses_index(self):
        plan = self._plan(
            "SELECT * FROM claims WHERE repo_key=? AND file_path=? "
            "AND NOT (machine_id=? AND session_id=?)",
            ("repo", "f1.ts", "m", "s"),
        )
        self.assertIn("idx_claims_lookup", plan)

    def test_release_uses_index(self):
        plan = self._plan("DELETE FROM claims WHERE session_id=?", ("s1",))
        self.assertIn("idx_claims_session", plan)

    def test_sweep_uses_expiry_index(self):
        plan = self._plan("DELETE FROM claims WHERE expires_at<=?", (0,))
        self.assertIn("idx_claims_expiry", plan)

    def test_repo_scan_uses_index(self):
        plan = self._plan(
            "SELECT DISTINCT machine_id, session_id FROM claims WHERE repo_key=?",
            ("repo",),
        )
        # Any index on the repo_key prefix is fine (the PK autoindex covers it);
        # what matters is it SEARCHes an index rather than SCANning the table.
        self.assertIn("USING", plan)
        self.assertIn("INDEX", plan)
        self.assertNotIn("SCAN claims\b", plan)


if __name__ == "__main__":
    unittest.main()
