"""N6-14 (B4): cross-run backlog and the INT push wait. Temp dirs only; no git,
no network, no clock, never data/jarvis.db (sqlite3.connect is patched to fail)."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import n6_deps, work_planner
from jarvis.n6_deps import Item

CAND = "a" * 40
MAIN = "b" * 40


class N6DepsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "n6" / "backlog.json"
        patcher = mock.patch("sqlite3.connect", side_effect=AssertionError("no DB"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _int_with_dependent(self, pushed=True):
        n6_deps.enqueue(self.path, [Item("INT1", is_int=True), Item("D1", ("INT1",))])
        self.assertTrue(n6_deps.mark_started(self.path, "INT1", self._verify(None)))
        n6_deps.record(self.path, "INT1", "done", CAND)
        if pushed:
            n6_deps.record_push(self.path, "INT1")

    def _verify(self, answer):
        return mock.Mock(return_value=answer)

    # --- NEG: the INT116 flaw and every unknown hold -------------------------

    def test_neg_finished_int_not_pushed_holds(self):
        self._int_with_dependent(pushed=False)
        verify = self._verify(MAIN)
        pick = n6_deps.pick(self.path, verify)
        self.assertEqual(pick.start, ())
        self.assertEqual(pick.held, (("D1", "INT INT1 is not pushed"),))
        verify.assert_not_called()

    def test_neg_unverified_main_sha_does_not_start(self):
        self._int_with_dependent()
        for bad in (True, "yes", CAND[:7], MAIN.upper(), MAIN + "\n", 1, b"b" * 40):
            with self.subTest(answer=bad):
                pick = n6_deps.pick(self.path, self._verify(bad))
                self.assertEqual(pick.start, ())
                self.assertEqual([h[0] for h in pick.held], ["D1"])
                self.assertIn("malformed", pick.attention[0])

    def test_neg_push_reported_but_not_on_remote_holds(self):
        self._int_with_dependent()
        pick = n6_deps.pick(self.path, self._verify(None))
        self.assertEqual(pick.start, ())
        self.assertEqual(pick.held, (("D1", "INT INT1 is not verified on the remote main"),))
        self.assertEqual(pick.attention, ())

    def test_neg_remote_unreadable_holds(self):
        self._int_with_dependent()
        for exc in (OSError("offline"), TimeoutError(), RuntimeError("git")):
            with self.subTest(exc=type(exc).__name__):
                pick = n6_deps.pick(self.path, mock.Mock(side_effect=exc))
                self.assertEqual(pick.start, ())
                self.assertEqual([h[0] for h in pick.held], ["D1"])
                self.assertIn("remote unreadable", pick.attention[0])
                self.assertFalse(pick.held_all)

    def test_neg_blocked_unknown_and_running_dependencies_hold(self):
        n6_deps.enqueue(self.path, [Item("A"), Item("B"), Item("X", ("A",)),
                                    Item("Y", ("B",)), Item("Z", ("GHOST",))])
        n6_deps.record(self.path, "A", "blocked")
        self.assertTrue(n6_deps.mark_started(self.path, "B", self._verify(None)))
        pick = n6_deps.pick(self.path, self._verify(MAIN))
        self.assertEqual(pick.start, ())
        self.assertEqual(dict(pick.held), {"X": "dependency A is blocked",
                                           "Y": "dependency B is started",
                                           "Z": "dependency GHOST is unknown"})
        with self.assertRaises(ValueError):  # BLOCKED never turns done (R04)
            n6_deps.record(self.path, "A", "done")

    def test_neg_int_verified_at_different_main_shas_holds(self):
        n6_deps.enqueue(self.path, [Item("I1", is_int=True), Item("I2", is_int=True),
                                    Item("D", ("I1", "I2"))])
        n6_deps.record(self.path, "I1", "done", CAND)
        n6_deps.record(self.path, "I2", "done", "c" * 40)
        n6_deps.record_push(self.path, "I1")
        n6_deps.record_push(self.path, "I2")
        pick = n6_deps.pick(self.path, lambda sha: MAIN if sha == CAND else "d" * 40)
        self.assertEqual(pick.start, ())
        self.assertIn("different main SHAs", pick.held[0][1])

    def test_neg_corrupt_state_file_holds_all(self):
        self._int_with_dependent()
        good = self.path.read_text(encoding="utf-8")
        variants = {
            "empty": b"", "truncated": good[:-3].encode(), "list": b"[]",
            "no backlog": b'{"version": 1}', "version true": good.replace(
                '"version": 1', '"version": true').encode(),
            "version 2": good.replace('"version": 1', '"version": 2').encode(),
            "missing key": good.replace(', "pushed": false', "").encode(),
            "bad state": good.replace('"pending"', '"running"').encode(),
            "list state": good.replace('"pending"', "[]").encode(),
            "short sha": good.replace(CAND, CAND[:39]).encode(),
            "pushed not done": good.replace('"pushed": false', '"pushed": true').encode(),
            "duplicate": good.replace('"D1"', '"INT1"').encode(),
            "not utf-8": b"\xff\xfe\x00{",
            "deep": b"[" * 100000 + b"]" * 100000,
            # N6-14FIX F2: json keeps the last duplicate key, which would drop a dependency
            "duplicate deps key": good.replace(
                '"deps": ["INT1"]', '"deps": ["INT1"], "deps": []').encode(),
            "duplicate backlog key": good.replace(
                '{"version": 1, ', '{"version": 1, "backlog": [], ').encode(),
            "duplicate version key": good.replace(
                '"version": 1', '"version": 1, "version": 1').encode(),
        }
        for name, raw in variants.items():  # every variant really differs from the good file
            self.assertNotEqual(raw, good.encode(), name)
        for name, raw in variants.items():
            with self.subTest(variant=name):
                self.path.write_bytes(raw)
                pick = n6_deps.pick(self.path, self._verify(MAIN))
                self.assertTrue(pick.held_all)
                self.assertEqual((pick.start, pick.held), ((), ()))
                self.assertIn("backlog held", pick.attention[0])
                with self.assertRaises(n6_deps.Unreadable):
                    n6_deps.enqueue(self.path, [Item("NEW")])
                with self.assertRaises(n6_deps.Unreadable):
                    n6_deps.mark_started(self.path, "D1", self._verify(MAIN))
                self.assertEqual(self.path.read_bytes(), raw)  # never overwritten

    def _assert_start_refused(self, verify, reason):
        before = self.path.read_bytes()
        self.assertFalse(n6_deps.mark_started(self.path, "D1", verify))
        self.assertEqual(self.path.read_bytes(), before)  # no state change
        pick = n6_deps.pick(self.path, verify)
        self.assertEqual(pick.start, ())
        self.assertEqual(len(pick.held), 1)
        self.assertEqual(pick.held[0][0], "D1")
        self.assertIn(reason, pick.held[0][1])  # the hold reason is kept

    def test_neg_mark_started_refuses_unpushed_int_dependent(self):
        self._int_with_dependent(pushed=False)
        verify = self._verify(MAIN)
        self._assert_start_refused(verify, "is not pushed")
        verify.assert_not_called()

    def test_neg_mark_started_refuses_unverified_int_dependent(self):
        self._int_with_dependent()
        for answer in (None, True, CAND[:7], MAIN.upper(), MAIN + "\n"):
            with self.subTest(answer=answer):
                self._assert_start_refused(self._verify(answer), "not verified")

    def test_neg_mark_started_refuses_when_remote_unreadable(self):
        self._int_with_dependent()
        for exc in (OSError("net"), RuntimeError("x"), TimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                self._assert_start_refused(mock.Mock(side_effect=exc), "not verified")

    def test_neg_mark_started_refuses_other_holds(self):
        n6_deps.enqueue(self.path, [Item("A"), Item("BL"), Item("I1", is_int=True),
                                    Item("I2", is_int=True)])
        n6_deps.record(self.path, "BL", "blocked")
        for i, (dep, sha) in enumerate((("I1", CAND), ("I2", "c" * 40))):
            self.assertTrue(n6_deps.mark_started(self.path, dep, self._verify(None)), i)
            n6_deps.record(self.path, dep, "done", sha)
            n6_deps.record_push(self.path, dep)
        cases = {("A",): "is pending", ("BL",): "is blocked", ("GHOST",): "is unknown",
                 ("I1", "I2"): "different main SHAs"}
        for deps, reason in cases.items():
            with self.subTest(deps=deps):
                sid = "D-" + "-".join(deps)
                n6_deps.enqueue(self.path, [Item(sid, deps)])
                before = self.path.read_bytes()
                verify = mock.Mock(side_effect=lambda sha: MAIN if sha == CAND else "d" * 40)
                self.assertFalse(n6_deps.mark_started(self.path, sid, verify))
                self.assertEqual(self.path.read_bytes(), before)
                held = dict(n6_deps.pick(self.path, verify).held)
                self.assertIn(reason, held[sid])

    def test_ok_mark_started_verified_dependent_starts_once(self):
        self._int_with_dependent()
        verify = self._verify(MAIN)
        self.assertTrue(n6_deps.mark_started(self.path, "D1", verify))
        verify.assert_called_once_with(CAND)
        self.assertFalse(n6_deps.mark_started(self.path, "D1", verify))
        self.assertNotIn("D1", dict(n6_deps.pick(self.path, verify).held))

    def test_neg_malformed_input_raises_and_writes_nothing(self):
        n6_deps.enqueue(self.path, [Item("A")])
        before = self.path.read_bytes()
        bad_items = ([Item("B"), Item("A")], [Item(" ")], [Item("C", ("C",))],
                     [Item("C", ["A"])], [Item("C", ("",))], [Item("C", is_int=1)],
                     [Item("B"), Item("B")])
        for items in bad_items:
            with self.subTest(items=items), self.assertRaises(ValueError):
                n6_deps.enqueue(self.path, items)
        self.assertEqual(self.path.read_bytes(), before)
        n6_deps.enqueue(self.path, [Item("I", is_int=True)])
        with self.assertRaises(ValueError):  # an INT is done only with its candidate SHA
            n6_deps.record(self.path, "I", "done")
        with self.assertRaises(ValueError):
            n6_deps.record(self.path, "I", "done", CAND.upper())
        with self.assertRaises(ValueError):
            n6_deps.record_push(self.path, "I")  # not done yet
        with self.assertRaises(ValueError):
            n6_deps.record(self.path, "GHOST", "blocked")

    # --- restart, release, once ----------------------------------------------

    def test_restart_keeps_backlog_order(self):
        n6_deps.enqueue(self.path, [Item("c"), Item("a")])
        n6_deps.enqueue(self.path, [Item("b", ("c",))])
        n6_deps.record(self.path, "c", "done")
        pick = n6_deps.pick(Path(str(self.path)), self._verify(None))  # a new process
        self.assertEqual(pick.start, (("a", None), ("b", None)))
        self.assertEqual(sorted(os.listdir(self.path.parent)), ["backlog.json"])  # no .tmp left

    def test_ok_verified_push_releases_dependent_on_verified_base(self):
        self._int_with_dependent()
        verify = self._verify(MAIN)
        pick = n6_deps.pick(self.path, verify)
        self.assertEqual(pick.start, (("D1", MAIN),))
        self.assertEqual((pick.held, pick.attention, pick.held_all), ((), (), False))
        verify.assert_called_once_with(CAND)

    def test_two_passes_start_a_slice_once(self):
        self._int_with_dependent()
        self.assertTrue(n6_deps.mark_started(self.path, "D1", self._verify(MAIN)))
        self.assertFalse(n6_deps.mark_started(self.path, "D1", self._verify(MAIN)))
        self.assertEqual(n6_deps.pick(self.path, self._verify(MAIN)).start, ())

    def test_g13_dependency_pushed_late(self):
        self._int_with_dependent(pushed=False)
        self.assertEqual(n6_deps.pick(self.path, self._verify(MAIN)).start, ())
        n6_deps.record_push(self.path, "INT1")
        self.assertEqual(n6_deps.pick(self.path, self._verify(None)).start, ())
        self.assertEqual(n6_deps.pick(self.path, self._verify(MAIN)).start, (("D1", MAIN),))

    def test_work_planner_seeds_backlog_in_plan_order(self):
        plan = SimpleNamespace(slices=[SimpleNamespace(slice_id=s) for s in ("s2", "s1")])
        work_planner.seed_backlog(self.path, SimpleNamespace(plan=plan))
        pick = n6_deps.pick(self.path, self._verify(None))
        self.assertEqual(pick.start, (("s2", None), ("s1", None)))


if __name__ == "__main__":
    unittest.main()
