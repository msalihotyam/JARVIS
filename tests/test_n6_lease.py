"""N6-08: single-owner lease with a fencing epoch (temp dirs and explicit clocks only)."""

import dataclasses
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

    def test_neg_renew_never_shortens_the_expiry(self):
        # N6-HARD2 H2: a shorter, zero or negative ttl keeps the longer expiry already on file.
        lease = acquire(self.path, "mgr-a", "S1", 600, NOW)
        for ttl in (10, 0, -50):
            with self.subTest(ttl=ttl):
                got = renew(self.path, lease, ttl, NOW + 1)
                self.assertEqual(got.expires, NOW + 600)
                self.assertEqual(json.loads(self.path.read_text())["expires"], NOW + 600)
        self.assertEqual(renew(self.path, lease, 900, NOW + 1).expires, NOW + 901)

    def test_neg_renew_non_finite_ttl_refused_and_file_stays_readable(self):
        lease = acquire(self.path, "mgr-a", "S1", 60, NOW)
        for ttl in (float("inf"), float("nan"), float("-inf"), 10 ** 400):
            with self.subTest(ttl=ttl):
                self.assertIsNone(renew(self.path, lease, ttl, NOW + 1))
                self.assertEqual(live_slices(self.path, NOW + 1), frozenset({"S1"}))
                self.assertEqual(json.loads(self.path.read_text())["expires"], NOW + 60)

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

    # N6GAPLEASE: pins for the MUTPRUNE2 survivors (all clocks explicit, no waits).
    # Not asserted: the OS lock byte offset / byte count (msvcrt vs fcntl differ, no portable probe).
    def _write_state(self, **over):
        state = {"epoch": 1, "owner": "o", "slice_id": "S1", "expires": NOW + 10, "applied": []}
        state.update(over)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def test_lease_is_immutable(self):
        lease = acquire(self.path, "mgr-a", "S1", 60, NOW)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            lease.epoch = 9  # type: ignore[misc]

    def test_acquire_creates_missing_nested_dirs(self):
        deep = self.path.parent / "x" / "y" / "lease.json"
        self.assertIsNotNone(acquire(deep, "mgr-a", "S1", 60, NOW))
        self.assertTrue(deep.is_file())

    def test_lock_is_released_with_the_unlock_call(self):
        with mock.patch.object(n6_lease, "_os_lock", wraps=n6_lease._os_lock) as spy:
            with n6_lease._locked(self.path) as got:
                self.assertTrue(got)
        self.assertEqual(len(spy.call_args_list), 2)
        self.assertFalse(spy.call_args_list[0].kwargs.get("unlock", False))
        self.assertIs(spy.call_args_list[1].kwargs.get("unlock"), True)

    def test_lock_not_acquired_skips_unlock(self):
        with mock.patch.object(n6_lease, "_os_lock", side_effect=OSError("busy")) as spy:
            with n6_lease._locked(self.path) as got:
                self.assertFalse(got)
        self.assertEqual(spy.call_count, 1)

    def test_epoch_zero_is_a_valid_lease_file(self):
        self._write_state(epoch=0)
        self.assertEqual(live_slices(self.path, NOW), frozenset({"S1"}))
        self._write_state(epoch=-1)
        self.assertIs(live_slices(self.path, NOW), HELD_ALL)

    def test_epoch_zero_file_acquires_as_epoch_one_once_expired(self):
        self._write_state(epoch=0)
        lease = acquire(self.path, "mgr-b", "S1", 10, NOW + 10)
        self.assertEqual(lease.epoch, 1)

    def test_expiry_equal_to_now_is_expired(self):
        lease = acquire(self.path, "mgr-a", "S1", 10, NOW)
        self.assertTrue(n6_lease.check(self.path, lease, NOW + 9))
        self.assertFalse(n6_lease.check(self.path, lease, NOW + 10))
        self.assertIsNone(renew(self.path, lease, 10, NOW + 10))
        self.assertFalse(release(self.path, lease, NOW + 10))
        with self.assertRaises(Fenced):
            apply_event(self.path, lease, "ev", NOW + 10)
        self.assertEqual(live_slices(self.path, NOW + 10), frozenset())

    def test_acquire_at_the_exact_expiry_of_the_old_lease_succeeds(self):
        acquire(self.path, "mgr-a", "S1", 10, NOW)
        self.assertIsNone(acquire(self.path, "mgr-b", "S1", 10, NOW + 9))
        self.assertEqual(acquire(self.path, "mgr-b", "S1", 10, NOW + 10).epoch, 2)

    def test_acquire_refuses_zero_or_negative_ttl_and_writes_nothing(self):
        for ttl in (0, -1):
            with self.subTest(ttl=ttl):
                self.assertIsNone(acquire(self.path, "mgr-a", "S1", ttl, NOW))
        self.assertFalse(self.path.exists())

    def test_acquire_refuses_non_finite_clock_or_ttl(self):
        nan, inf = float("nan"), float("inf")
        for now, ttl in ((nan, 10), (NOW, nan), (inf, 10), (NOW, inf)):
            with self.subTest(now=now, ttl=ttl):
                self.assertIsNone(acquire(self.path, "mgr-a", "S1", ttl, now))
        self.assertFalse(self.path.exists())

    def test_fenced_runs_write_only_for_the_live_lease(self):
        lease = acquire(self.path, "mgr-a", "S1", 10, NOW)
        calls = []
        self.assertTrue(n6_lease.fenced(self.path, lease, NOW + 1, lambda: calls.append(1)))
        self.assertEqual(calls, [1])
        self.assertFalse(n6_lease.fenced(self.path, lease, NOW + 10, lambda: calls.append(2)))
        stale = n6_lease.Lease("mgr-a", "S1", lease.epoch + 1, lease.expires)
        self.assertFalse(n6_lease.fenced(self.path, stale, NOW + 1, lambda: calls.append(3)))
        self.assertEqual(calls, [1])

    def test_lock_held_elsewhere_refuses_every_lease_operation(self):
        lease = acquire(self.path, "mgr-a", "S1", 100, NOW)
        calls = []
        with n6_lease._locked(self.path) as got:
            self.assertTrue(got)
            self.assertIsNone(renew(self.path, lease, 100, NOW + 1))
            self.assertFalse(release(self.path, lease, NOW + 1))
            with self.assertRaises(Fenced):
                apply_event(self.path, lease, "ev", NOW + 1)
            self.assertFalse(n6_lease.fenced(self.path, lease, NOW + 1, lambda: calls.append(1)))
        self.assertEqual(calls, [])
        self.assertTrue(n6_lease.check(self.path, lease, NOW + 1))
        self.assertTrue(release(self.path, lease, NOW + 1))

    def test_apply_event_write_failure_surfaces_as_fenced(self):
        lease = acquire(self.path, "mgr-a", "S1", 100, NOW)
        for exc in (OSError("disk"), ValueError("digits")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(n6_lease, "_write", side_effect=exc):
                    with self.assertRaises(Fenced):
                        apply_event(self.path, lease, "ev-1", NOW + 1)
        self.assertEqual(json.loads(self.path.read_text())["applied"], [])
        self.assertTrue(apply_event(self.path, lease, "ev-1", NOW + 1))

    def test_neg_module_names_no_database(self):
        src = Path(n6_lease.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", src)
        self.assertNotIn("jarvis.db", src)


if __name__ == "__main__":
    unittest.main()
