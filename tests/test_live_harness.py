from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests_live import harness
from tests_live.harness import Adapter, Entry, Ledger, Tracker, VerificationError

TAG = "clitest-202610281200-abcd"


class FakeStore:
    """In-memory records keyed by id, with an optional delete that fails."""

    def __init__(self, records: dict[int, dict[str, Any]], *, sticky: bool = False):
        self.records = records
        self.sticky = sticky
        self.deleted: list[int] = []

    def adapter(self) -> Adapter:
        def delete(entry: Entry) -> None:
            self.deleted.append(entry.id)
            if not self.sticky:
                self.records.pop(entry.id, None)

        return Adapter(lambda entry: self.records.get(entry.id), delete)


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = Ledger(Path(directory.name) / "ledger.json")

    def tracker(self, store: FakeStore) -> Tracker:
        return Tracker(TAG, {("freshsales", "deals"): store.adapter()}, self.ledger)

    def test_only_marked_records_of_the_run_are_deleted_and_verified(self) -> None:
        store = FakeStore(
            {
                1: {"tags": [TAG]},
                2: {"name": "[CLI-TEST] deal"},
                3: {"name": "Real customer deal", "tags": ["other"]},
            }
        )
        tracker = self.tracker(store)
        for record_id in (1, 2, 3):
            tracker.register("freshsales", "deals", record_id)
        with self.assertRaisesRegex(VerificationError, "freshsales:deals:3"):
            tracker.cleanup()
        self.assertEqual(store.deleted, [2, 1])
        self.assertIn(3, store.records)
        statuses = {e.id: e.status for e in self.ledger.load()}
        self.assertEqual(statuses, {1: "deleted", 2: "deleted", 3: "failed"})

    def test_record_still_readable_after_delete_fails_verification(self) -> None:
        tracker = self.tracker(FakeStore({1: {"tags": [TAG]}}, sticky=True))
        entry = tracker.register("freshsales", "deals", 1)
        with self.assertRaisesRegex(VerificationError, "still readable"):
            tracker.delete(entry)

    def test_already_gone_records_and_repeat_cleanup(self) -> None:
        store = FakeStore({})
        tracker = self.tracker(store)
        tracker.register("freshsales", "deals", 9)
        self.assertEqual(tracker.cleanup(), {"freshsales:deals:9": "already gone"})
        self.assertEqual(tracker.cleanup(), {"freshsales:deals:9": "deleted"})
        self.assertEqual(store.deleted, [])

    def test_per_entity_cap_and_foreign_entries(self) -> None:
        tracker = self.tracker(FakeStore({}))
        for record_id in range(harness.MAX_PER_ENTITY):
            tracker.register("freshsales", "deals", record_id + 1)
        with self.assertRaisesRegex(VerificationError, "more than"):
            tracker.register("freshsales", "deals", 99)
        foreign = Entry("freshsales", "deals", 5, "clitest-other")
        with self.assertRaisesRegex(VerificationError, "not from this run"):
            tracker.delete(foreign)

    def test_ledger_round_trip_and_marker_rules(self) -> None:
        self.assertEqual(self.ledger.load(), [])
        entry = Entry("freshsales", "notes", 4, TAG, parent="deals:1")
        self.ledger.upsert(entry)
        entry.status = "deleted"
        self.ledger.upsert(entry)
        self.assertEqual(self.ledger.load(), [entry])
        self.assertTrue(harness.carries_marker({"subject": "[CLI-TEST] x"}, TAG))
        self.assertTrue(harness.carries_marker({"description": "[CLI-TEST] n"}, TAG))
        self.assertFalse(harness.carries_marker({"name": "x [CLI-TEST]"}, TAG))
        self.assertFalse(harness.carries_marker({"tags": None, "name": 3}, TAG))

    def test_base_url_is_required_from_the_environment(self) -> None:
        with mock.patch.dict("os.environ", {"X_BASE_URL": "https://x.example/api"}):
            self.assertEqual(harness.base_url("X_BASE_URL"), "https://x.example/api")
        with (
            mock.patch.dict("os.environ", {"X_BASE_URL": ""}),
            self.assertRaisesRegex(RuntimeError, "set X_BASE_URL"),
        ):
            harness.base_url("X_BASE_URL")

    def test_live_flag_and_run_tag(self) -> None:
        with mock.patch.dict("os.environ", {harness.LIVE_FLAG: "1"}):
            self.assertTrue(harness.live_enabled())
        with mock.patch.dict("os.environ", {harness.LIVE_FLAG: "yes"}):
            self.assertFalse(harness.live_enabled())
        tag = harness.new_run_tag()
        self.assertRegex(tag, r"^clitest-\d{12}-[0-9a-f]{4}$")
        self.assertLessEqual(len(tag), 32)
        with mock.patch.dict("os.environ", {"FRESHWORKS_LIVE_LEDGER": "/x/l.json"}):
            self.assertEqual(harness.ledger_path(), Path("/x/l.json"))


if __name__ == "__main__":
    unittest.main()
