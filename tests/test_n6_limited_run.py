"""N6-WIRE2: the LIMITED-phase caller behind build/n6/LIMITED_ON (fakes only, injected clock)."""

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jarvis import n6_deps, n6_lease, n6_notify
from jarvis.notify import NotificationCore
from tests.test_n6_notify import Ledger
from tests.test_work_supervisor import _sit

_spec = importlib.util.spec_from_file_location(
    "n6_limited_run", Path(__file__).resolve().parents[1] / "scripts" / "n6_limited_run.py")
lr = importlib.util.module_from_spec(_spec)
sys.modules["n6_limited_run"] = lr  # dataclasses resolves annotations through sys.modules
_spec.loader.exec_module(lr)

TIERS = {"A-1": "T1", "B-2": "T2", "INT-3": "T1", "C-4": "T1"}


class LimitedRun(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.n6 = self.root / "build" / "n6"
        self.n6.mkdir(parents=True)
        self.clock = 1000.0
        self.ran: list[str] = []
        self.status = "PASS"

    def on(self):
        (self.n6 / "LIMITED_ON").write_text("", encoding="utf-8")

    def backlog(self, *items):
        n6_deps.enqueue(self.n6 / "deps.json", items)

    def run_port(self, sid, lease):
        self.ran.append(sid)
        if isinstance(self.status, BaseException):
            raise self.status
        return self.status

    def ports(self, **kw):
        base = dict(now=lambda: self.clock, verify=lambda sha: True, tier=TIERS.get,
                    situation=lambda sid: _sit(), run=self.run_port, ttl_s=600.0)
        return lr.Ports(**{**base, **kw})

    def live(self):
        return n6_lease.live_slices(self.n6 / "lease.json", self.clock)

    def states(self):
        data = json.loads((self.n6 / "deps.json").read_text(encoding="utf-8"))
        return json.dumps(data)

    def test_switch_off_means_zero_actions(self):
        self.backlog(n6_deps.Item("A-1"))
        before = sorted(p.name for p in self.n6.iterdir())
        snap = self.states()

        def boom(*a, **k):
            raise AssertionError("an N6 port was called with the switch off")

        notes = lr.tick(self.root, lr.Ports(now=boom, verify=boom, tier=boom, situation=boom,
                                            run=boom, ttl_s=600.0, page=boom))
        self.assertEqual(notes, [])
        self.assertEqual(sorted(p.name for p in self.n6.iterdir()), before)
        self.assertEqual(self.states(), snap)

    def test_conveyor_pause_also_means_off(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        (self.root / "build" / "conveyor").mkdir(parents=True)
        (self.root / "build" / "conveyor" / "PAUSE").write_text("", encoding="utf-8")
        self.assertEqual(lr.tick(self.root, self.ports()), [])
        self.assertEqual(self.ran, [])

    def test_runs_one_t1_slice_end_to_end_and_releases(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"), n6_deps.Item("C-4"))
        notes = lr.tick(self.root, self.ports())
        self.assertEqual(self.ran, ["A-1"])
        self.assertIn("A-1: PASS", notes)
        self.assertEqual(self.live(), frozenset())
        self.assertIn('"done"', self.states())
        self.assertTrue((self.n6 / "breaker.json").exists())

    def test_never_takes_t2_int_or_unknown_tier(self):
        self.on()
        self.backlog(n6_deps.Item("B-2"), n6_deps.Item("INT-3", is_int=True), n6_deps.Item("X-9"))
        snap = self.states()
        tier = lambda s: "INT" if s.startswith("INT") else TIERS.get(s)  # noqa: E731
        self.assertEqual(lr.tick(self.root, self.ports(tier=tier)), [])
        self.assertEqual(self.ran, [])
        self.assertEqual(self.states(), snap)  # untouched: still pending for the conveyor
        self.assertFalse((self.n6 / "lease.json").exists())

    def test_raising_tier_is_not_t1(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))

        def tier(s):
            raise OSError("tier unreadable")

        lr.tick(self.root, self.ports(tier=tier))
        self.assertEqual(self.ran, [])

    def test_switch_off_mid_tick_hands_back_without_running(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))

        def situation(sid):
            (self.n6 / "LIMITED_ON").unlink()
            return _sit()

        notes = lr.tick(self.root, self.ports(situation=situation))
        self.assertIn("A-1: handed back (switch off)", notes)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.live(), frozenset())  # the conveyor may dispatch it now
        self.on()
        lr.tick(self.root, self.ports())
        self.assertEqual(self.ran, [])  # N6 never re-picks it: no double run

    def test_runner_exception_releases_and_never_reruns(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        self.status = RuntimeError("lane crashed")
        notes = lr.tick(self.root, self.ports())
        self.assertIn("A-1: handed back (RuntimeError)", notes)
        self.assertEqual(self.live(), frozenset())
        self.status = "PASS"
        lr.tick(self.root, self.ports())
        self.assertEqual(self.ran, ["A-1"])

    def test_live_lease_or_unreadable_lease_starts_nothing(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        held = n6_lease.acquire(self.n6 / "lease.json", "conveyor", "Z-0", 600.0, self.clock)
        self.assertIsNotNone(held)
        self.assertEqual(lr.tick(self.root, self.ports()), ["lease held: nothing started"])
        (self.n6 / "lease.json").write_text("{", encoding="utf-8")
        self.assertEqual(lr.tick(self.root, self.ports()), ["lease held: nothing started"])
        self.assertEqual(self.ran, [])

    def test_n6_exception_never_escapes(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))

        def now():
            raise ValueError("clock gone")

        notes = lr.tick(self.root, self.ports(now=now))
        self.assertEqual(notes, ["n6 held: ValueError"])
        self.assertEqual(self.ran, [])

    def test_blocked_verdict_records_blocked(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        self.status = "BLOCKED"
        self.assertIn("A-1: BLOCKED", lr.tick(self.root, self.ports()))
        self.assertIn('"blocked"', self.states())

    def test_needs_input_and_failed_after_cap_page_the_phone(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        notify = self.n6 / "notify.json"
        ledger, sent, seq = Ledger(), [], iter(range(1, 100))
        core = NotificationCore(ledger.write, ledger.read)

        def run(sid, lease):  # the lane's work_driver submits these in N6 mode
            n6_notify.submit(notify, n6_notify.Event("needs_input", sid, "Bilgi gerekiyor"))
            n6_notify.submit(notify, n6_notify.Event("slice_failed_after_cap", sid, "Sınır"))
            return "BLOCKED"

        def page(path):
            return n6_notify.flush(path, core=core, run_id="n6", next_seq=lambda: next(seq),
                                   at=datetime(2026, 11, 11, 12, tzinfo=timezone.utc),
                                   send=sent.append)

        lr.tick(self.root, self.ports(run=run, page=page))
        self.assertEqual(len(sent), 1)
        self.assertIn("Bilgi gerekiyor", sent[0])
        self.assertIn("Sınır", sent[0])

    def test_any_eq_tier_nan_clock_and_bad_ttl_start_nothing(self):
        from unittest import mock
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        lr.tick(self.root, self.ports(tier=lambda s: mock.ANY))  # ANY == "T1" is True
        for kw in (dict(ttl_s=0.0), dict(ttl_s=float("nan")), dict(now=lambda: float("nan"))):
            lr.tick(self.root, self.ports(**kw))
        self.assertEqual(self.ran, [])
        self.assertFalse((self.n6 / "lease.json").exists())

    def test_clock_failing_after_run_still_releases(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        calls = iter([self.clock])

        def now():
            return next(calls)  # StopIteration after the first call

        notes = lr.tick(self.root, self.ports(now=now))
        self.assertEqual(self.ran, ["A-1"])
        self.assertIn("A-1: PASS", notes)
        self.assertEqual(self.live(), frozenset())

    def test_page_failure_is_a_note_not_a_crash(self):
        self.on()

        def page(path):
            raise OSError("pager down")

        self.assertEqual(lr.tick(self.root, self.ports(page=page)), ["page failed: OSError"])


if __name__ == "__main__":
    unittest.main()
