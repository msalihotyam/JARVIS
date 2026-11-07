"""N6-08: single-owner lease with a fencing epoch (temp dirs and explicit clocks only)."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import n6_lease
from jarvis.n6_lease import HELD_ALL, Fenced, acquire, apply_event, live_slices, release, renew

NOW = 1_000_000.0


class N6LeaseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "n6" / "lease.json"
        # NEG: nothing in this module may open a database (data/jarvis.db least of all).
        patcher = mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db opened"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_neg_two_managers_exactly_one_wins(self):
        a = acquire(self.path, "mgr-a", "S1", 60, NOW)
        b = acquire(self.path, "mgr-b", "S1", 60, NOW + 1)
        self.assertIsNotNone(a)
        self.assertIsNone(b)
        self.assertEqual(a.epoch, 1)
        self.assertEqual(json.loads(self.path.read_text())["owner"], "mgr-a")

    def test_neg_lock_held_elsewhere_refuses_acquire(self):
        with n6_lease._locked(self.path) as got:
            self.assertTrue(got)
            self.assertIsNone(acquire(self.path, "mgr-b", "S1", 60, NOW))
        self.assertFalse(self.path.exists())

    def test_neg_stale_epoch_write_refused(self):
        old = acquire(self.path, "mgr-a", "S1", 10, NOW)
        new = acquire(self.path, "mgr-b", "S1", 10, NOW + 11)
        self.assertEqual(new.epoch, old.epoch + 1)
        self.assertIsNone(renew(self.path, old, 10, NOW + 12))
        self.assertFalse(release(self.path, old, NOW + 12))
        with self.assertRaises(Fenced):
            apply_event(self.path, old, "ev-1", NOW + 12)
        self.assertFalse(n6_lease.check(self.path, old, NOW + 12))
        self.assertTrue(n6_lease.check(self.path, new, NOW + 12))
        self.assertEqual(json.loads(self.path.read_text())["owner"], "mgr-b")

    def test_neg_event_replayed_twice_applies_once(self):
        lease = acquire(self.path, "mgr-a", "S1", 60, NOW)
        self.assertTrue(apply_event(self.path, lease, "ev-1", NOW + 1))
        self.assertFalse(apply_event(self.path, lease, "ev-1", NOW + 2))
        self.assertTrue(apply_event(self.path, lease, "ev-2", NOW + 3))
        lease = renew(self.path, lease, 60, NOW + 4)
        self.assertFalse(apply_event(self.path, lease, "ev-1", NOW + 5))
        self.assertEqual(json.loads(self.path.read_text())["applied"],
                         [[1, "ev-1"], [1, "ev-2"]])

    def test_neg_expired_lease_taken_with_higher_epoch(self):
        a = acquire(self.path, "mgr-a", "S1", 10, NOW)
        self.assertIsNone(acquire(self.path, "mgr-b", "S2", 10, NOW + 9))
        b = acquire(self.path, "mgr-b", "S2", 10, NOW + 10)
        self.assertEqual((b.owner, b.slice_id, b.epoch), ("mgr-b", "S2", a.epoch + 1))
        self.assertTrue(release(self.path, b, NOW + 11))
        c = acquire(self.path, "mgr-a", "S1", 10, NOW + 11)
        self.assertEqual(c.epoch, b.epoch + 1)
        self.assertEqual(live_slices(self.path, NOW + 12), frozenset({"S1"}))

    def test_neg_malformed_lease_file_is_held_all(self):
        bad = [b"{not json", b"[]", b"\xff\xfe",
               json.dumps({"epoch": True, "owner": "a", "slice_id": "S", "expires": 1.0,
                           "applied": []}).encode(),
               json.dumps({"epoch": 1, "owner": "a", "slice_id": "S", "applied": []}).encode(),
               b'{"epoch": 1, "owner": "a", "slice_id": "S", "expires": NaN, "applied": []}',
               json.dumps({"epoch": 1, "owner": "a", "slice_id": "S", "expires": 1.0,
                           "applied": [[1]]}).encode()]
        self.path.parent.mkdir(parents=True)
        for raw in bad:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.assertIs(live_slices(self.path, NOW), HELD_ALL)
                self.assertIsNone(acquire(self.path, "mgr-a", "S1", 60, NOW))
                self.assertEqual(self.path.read_bytes(), raw)

    def test_neg_apply_event_with_pre_renew_lease_keeps_renewed_expiry(self):
        # N6-08FIX F2: writes keep the file's state, never the caller's stale Lease copy.
        old = acquire(self.path, "mgr-a", "S1", 60, 0)
        self.assertEqual(renew(self.path, old, 120, 50).expires, 170)
        self.assertTrue(apply_event(self.path, old, "ev-1", 55))
        self.assertEqual(json.loads(self.path.read_text())["expires"], 170)
        self.assertIsNone(acquire(self.path, "mgr-b", "S2", 60, 70))
        self.assertEqual(live_slices(self.path, 70), frozenset({"S1"}))
        self.assertTrue(release(self.path, old, 80))
        self.assertEqual(json.loads(self.path.read_text())["expires"], 80)

    def test_neg_absurd_expires_numbers_are_held_all(self):
        # N6-08FIX F5: overflowing / non-finite / bool expires and deep nesting never raise.
        tmpl = '{"epoch": 1, "owner": "a", "slice_id": "S", "expires": %s, "applied": []}'
        bad = [tmpl % ("1" + "0" * 400), tmpl % ("-1" + "0" * 400), tmpl % "1e400",
               tmpl % "Infinity", tmpl % "-Infinity", tmpl % "NaN",
               tmpl % '"inf"', tmpl % '"nan"', tmpl % "true", tmpl % "false",
               tmpl % "null", "[" * 100000 + "]" * 100000]
        self.path.parent.mkdir(parents=True)
        for raw in bad:
            with self.subTest(raw=raw[:60]):
                self.path.write_text(raw, encoding="utf-8")
                self.assertIs(live_slices(self.path, NOW), HELD_ALL)
                self.assertIsNone(acquire(self.path, "mgr-a", "S1", 60, NOW))
        # epoch + 1 past the int digit limit: acquire refuses instead of raising
        self.path.write_text(tmpl.replace('"epoch": 1', '"epoch": ' + "9" * 4300) % "1.0",
                             encoding="utf-8")
        self.assertIsNone(acquire(self.path, "mgr-a", "S1", 60, NOW))

    def test_neg_unreadable_lease_file_is_held_all(self):
        self.path.mkdir(parents=True)  # a directory where the file should be
        self.assertIs(live_slices(self.path, NOW), HELD_ALL)

    def test_no_file_is_no_lease_and_reading_creates_nothing(self):
        self.assertEqual(live_slices(self.path, NOW), frozenset())
        self.assertFalse(self.path.parent.exists())
        lease = acquire(self.path, "mgr-a", "S1", 60, NOW)
        self.assertEqual(live_slices(self.path, NOW + 59), frozenset({"S1"}))
        self.assertEqual(live_slices(self.path, NOW + 60), frozenset())
        self.assertTrue(release(self.path, lease, NOW + 1))
        self.assertEqual(live_slices(self.path, NOW + 2), frozenset())
        leftovers = [p for p in os.listdir(self.path.parent) if p.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_neg_module_names_no_database(self):
        src = Path(n6_lease.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", src)
        self.assertNotIn("jarvis.db", src)


if __name__ == "__main__":
    unittest.main()
