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

        snap = self.states()
        notes = lr.tick(self.root, self.ports(situation=situation))
        self.assertIn("A-1: handed back (switch off)", notes)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.live(), frozenset())  # the conveyor may dispatch it now
        self.assertEqual(self.states(), snap)  # F3: still pending, never marked started
        conv = self.root / "build" / "conveyor"
        conv.mkdir(parents=True)
        (conv / "lanes.json").write_text('{"A-1": {}}', encoding="utf-8")  # the conveyor took it
        self.on()
        lr.tick(self.root, self.ports())
        self.assertEqual(self.ran, [])  # N6 never runs what the conveyor has: no double run

    def test_int_flag_or_int_name_with_t1_tier_is_not_taken(self):
        # N6-WIRE2FIX F2: an INT entry stays with the conveyor even if its tier says T1.
        self.on()
        self.backlog(n6_deps.Item("A-1", is_int=True), n6_deps.Item("int-5"))
        snap = self.states()
        lr.tick(self.root, self.ports(tier=lambda s: "T1"))
        self.assertEqual(self.ran, [])
        self.assertEqual(self.states(), snap)
        self.assertFalse((self.n6 / "lease.json").exists())

    def test_lease_lock_held_marks_nothing(self):
        # F4/F5: the conveyor holding the lease lock (mid-dispatch) means no acquire, no mark.
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        snap = self.states()
        with n6_lease._locked(self.n6 / "lease.json") as got:
            self.assertTrue(got)
            notes = lr.tick(self.root, self.ports())
        self.assertIn("lease held: nothing started", notes)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.states(), snap)

    def test_conveyor_record_or_unreadable_record_is_not_taken(self):
        # F5: lanes.json key, an inbox/started file, or an unreadable lanes.json.
        conv = self.root / "build" / "conveyor"
        started = conv / "inbox" / "started"
        started.mkdir(parents=True)
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        snap = self.states()
        for setup in (lambda: (conv / "lanes.json").write_text('{"A-1": {}}', encoding="utf-8"),
                      lambda: (started / "20261112-120000-A-1.md").write_text("", encoding="utf-8"),
                      lambda: (conv / "lanes.json").write_text("{", encoding="utf-8")):
            for p in [conv / "lanes.json", *started.iterdir()]:
                p.unlink(missing_ok=True)
            setup()
            lr.tick(self.root, self.ports())
            self.assertEqual(self.ran, [])
            self.assertEqual(self.states(), snap)

    def test_lease_lost_before_mark_hands_back(self):
        # F6: the lease is re-checked with a fresh clock right before the mark.
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        snap = self.states()
        calls = iter([1000.0, 1000.0, 2000.0, 2000.0])
        notes = lr.tick(self.root, self.ports(now=lambda: next(calls)))
        self.assertIn("A-1: handed back (lease lost)", notes)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.states(), snap)

    def test_overflowing_ttl_or_inf_clock_starts_nothing(self):
        # F7: now + ttl_s must be finite before any acquire.
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        for kw in (dict(ttl_s=10 ** 400), dict(ttl_s=float("inf")),
                   dict(now=lambda: float("inf")), dict(ttl_s=-5.0)):
            lr.tick(self.root, self.ports(**kw))
        self.assertEqual(self.ran, [])
        self.assertFalse((self.n6 / "lease.json").exists())

    def test_acquire_refuses_non_finite_expiry(self):
        p = self.n6 / "lease.json"
        nan, inf = float("nan"), float("inf")
        for ttl, now in ((nan, 1.0), (inf, 1.0), (10 ** 400, 1.0), (0.0, 1.0), (-1.0, 1.0),
                         (600.0, nan), (600.0, inf), (1e308, 1e308)):
            self.assertIsNone(n6_lease.acquire(p, "o", "A-1", ttl, now), (ttl, now))
        self.assertFalse(p.exists())

    def test_runner_exception_releases_and_hands_back_to_pending(self):
        # N6-WIRE2FIX2: a raising runner never strands the slice started; it runs again
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        self.status = RuntimeError("lane crashed")
        notes = lr.tick(self.root, self.ports())
        self.assertIn("A-1: handed back (RuntimeError)", notes)
        self.assertEqual(self.live(), frozenset())
        self.assertIn('"state": "pending"', self.states())
        self.status = "PASS"
        lr.tick(self.root, self.ports())
        self.assertEqual(self.ran, ["A-1", "A-1"])
        self.assertIn('"state": "done"', self.states())

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
        calls = iter([self.clock] * 3)  # t0, the fresh acquire clock, the pre-mark re-check

        def now():
            return next(calls)  # StopIteration once the slice has run

        notes = lr.tick(self.root, self.ports(now=now))
        self.assertEqual(self.ran, ["A-1"])
        self.assertIn("A-1: PASS", notes)
        self.assertEqual(self.live(), frozenset())

    def test_page_failure_is_a_note_not_a_crash(self):
        self.on()

        def page(path):
            raise OSError("pager down")

        self.assertEqual(lr.tick(self.root, self.ports(page=page)), ["page failed: OSError"])


class ClaimFlow(unittest.TestCase):
    """N6-WIRE2FIX2 (O77 B): the production runner writes claim.json; the conveyor answers
    in claim_result.json; tick settles it. Every refusal returns the slice to pending."""

    setUp, on, backlog, ports, live, run_port = (
        LimitedRun.setUp, LimitedRun.on, LimitedRun.backlog, LimitedRun.ports, LimitedRun.live,
        LimitedRun.run_port)

    def claim_ports(self, **kw):
        return self.ports(run=lambda sid, lease: lr.write_claim(self.n6, lease), **kw)

    def state(self, sid="A-1"):
        data = json.loads((self.n6 / "deps.json").read_text(encoding="utf-8"))
        return {e["id"]: e["state"] for e in data["backlog"]}[sid]

    def claimed(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))
        notes = lr.tick(self.root, self.claim_ports())
        self.assertIn("A-1: claimed for the conveyor", notes)
        return json.loads((self.n6 / "claim.json").read_text(encoding="utf-8"))

    def answer(self, claim, state, reason="", sid="A-1"):
        (self.n6 / "claim_result.json").write_text(json.dumps(
            {"epoch": claim["epoch"], "slice_id": sid, "state": state, "reason": reason}),
            encoding="utf-8")

    def lanes(self, data):
        conv = self.root / "build" / "conveyor"
        conv.mkdir(parents=True, exist_ok=True)
        (conv / "lanes.json").write_text(json.dumps(data), encoding="utf-8")

    def test_claim_is_written_and_the_lease_kept(self):
        claim = self.claimed()
        self.assertEqual(claim["slice_id"], "A-1")
        self.assertEqual(self.state(), "started")
        self.assertEqual(self.live(), frozenset({"A-1"}))
        self.assertEqual(lr.tick(self.root, self.claim_ports()), ["A-1: claim pending"])

    def test_each_refusal_hands_back_to_pending(self):
        for reason in ("switch off", "int", "not started", "lease lost", "not T1",
                       "already running", "start_error: RuntimeError", "start_failed",
                       "route not claude", "overlap", "held: no header"):
            with self.subTest(reason=reason):
                self.setUp()
                claim = self.claimed()
                self.answer(claim, "refused", reason)
                notes = lr.tick(self.root, self.claim_ports())
                self.assertEqual(notes, [f"A-1: handed back ({reason})"])
                self.assertEqual(self.state(), "pending")
                self.assertEqual(self.live(), frozenset())
                self.assertFalse((self.n6 / "claim.json").exists())
                self.assertFalse((self.n6 / "claim_result.json").exists())

    def test_started_claim_records_the_conveyor_verdict_once(self):
        claim = self.claimed()
        self.answer(claim, "started")
        self.lanes({"A-1": {"status": "running"}})
        self.assertEqual(lr.tick(self.root, self.claim_ports()), ["A-1: running"])
        self.assertEqual(self.state(), "started")
        self.lanes({"A-1": {"finished": True, "status": "PASS"}})
        self.assertEqual(lr.tick(self.root, self.claim_ports()), ["A-1: PASS"])
        self.assertEqual(self.state(), "done")
        self.assertFalse((self.n6 / "claim.json").exists())

    def test_started_claim_whose_lanes_entry_is_gone_does_not_wedge(self):
        # N6-WIRE2FIX3 F3: finished, verdict unknown -> failure, claim dropped, one attention item
        claim = self.claimed()
        self.answer(claim, "started")
        self.lanes({"OTHER": {"status": "running"}})
        notes = lr.tick(self.root, self.claim_ports())
        self.assertEqual([n for n in notes if n.startswith("attention:")],
                         ["attention: A-1 finished unknown (lanes entry gone), recorded as failure"])
        self.assertIn("A-1: BLOCKED", notes)
        self.assertEqual(self.state(), "blocked")
        self.assertFalse((self.n6 / "claim.json").exists())
        self.assertFalse((self.n6 / "claim_result.json").exists())
        self.assertNotIn("A-1: running", lr.tick(self.root, self.claim_ports()))

    def test_an_older_claims_answer_is_no_answer(self):
        claim = self.claimed()
        self.answer({"epoch": claim["epoch"] - 1}, "refused", "x")
        self.assertEqual(lr.tick(self.root, self.claim_ports()), ["A-1: claim pending"])
        self.assertEqual(self.state(), "started")

    def test_switch_off_still_settles_an_open_claim(self):
        claim = self.claimed()
        (self.n6 / "LIMITED_ON").unlink()
        self.answer(claim, "refused", "switch off")
        self.assertEqual(lr.tick(self.root, self.claim_ports()), ["A-1: handed back (switch off)"])
        self.assertEqual(self.state(), "pending")

    def test_expired_unanswered_claim_is_handed_back(self):
        self.claimed()
        self.clock += 601.0
        self.assertEqual(lr.tick(self.root, self.claim_ports()),
                         ["A-1: handed back (claim expired)"])
        self.assertEqual(self.state(), "pending")
        self.assertFalse((self.n6 / "claim.json").exists())

    def test_expired_claim_the_conveyor_already_has_stays(self):
        self.claimed()
        self.lanes({"A-1": {"status": "running"}})
        self.clock += 601.0
        self.assertEqual(lr.tick(self.root, self.claim_ports()), ["A-1: claim pending"])
        self.assertEqual(self.state(), "started")

    def test_raising_claim_writer_never_strands_started(self):
        self.on()
        self.backlog(n6_deps.Item("A-1"))

        def boom(sid, lease):
            raise OSError("disk full")

        notes = lr.tick(self.root, self.ports(run=boom))
        self.assertIn("A-1: handed back (OSError)", notes)
        self.assertEqual(self.state(), "pending")
        self.assertEqual(self.live(), frozenset())

    def test_unreadable_claim_holds_and_changes_nothing(self):
        claim = self.claimed()
        (self.n6 / "claim.json").write_text("{not json", encoding="utf-8")
        self.answer(claim, "refused", "x")
        self.assertEqual(lr.tick(self.root, self.claim_ports()), ["n6 claim held: JSONDecodeError"])
        self.assertEqual(self.state(), "started")

    def test_main_is_a_no_op_when_off(self):
        old = lr.ROOT
        lr.ROOT = self.root
        self.addCleanup(setattr, lr, "ROOT", old)
        self.assertEqual(lr.main(), 0)
        self.assertEqual(sorted(p.name for p in self.n6.iterdir()), [])


class LimitedInputs(unittest.TestCase):
    """N6-LIMIT1: the real situation and backlog ports, fail closed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.conv = self.root / "build" / "conveyor"
        self.conv.mkdir(parents=True)

    def status(self, **kw):
        data = {h: False for h in lr._HOLDS}
        data.update(breaker="closed", usage_pct=40.0, pace={"pace_line": 50.0}, memory={"free_gb": 8.0})
        data.update(kw)
        (self.conv / "status.json").write_text(json.dumps(data))

    def test_situation_clear_only_when_every_hold_is_false(self):
        self.status()
        self.assertFalse(lr._situation("A", self.root).operator_pause)
        for kw in [*({h: True} for h in lr._HOLDS), {"breaker": "open"}, {"usage_pct": None}]:
            with self.subTest(kw=kw):
                self.status(**kw)
                self.assertTrue(lr._situation("A", self.root).operator_pause)
        (self.conv / "status.json").write_text("{bad")
        self.assertTrue(lr._situation("A", self.root).operator_pause)
        (self.conv / "status.json").unlink()
        self.assertTrue(lr._situation("A", self.root).operator_pause)
        self.status()
        (self.conv / "PAUSE").write_text("")
        self.assertTrue(lr._situation("A", self.root).operator_pause)

    def test_sync_takes_only_marked_t1_once(self):
        deps = self.root / "build" / "n6" / "deps.json"
        deps.parent.mkdir(parents=True)
        (self.conv / "backlog.json").write_text(json.dumps({"slices": [
            {"id": "A", "tier": "T1", "n6": True}, {"id": "B", "tier": "T2", "n6": True},
            {"id": "INT-x", "tier": "T1", "n6": True}, {"id": "C", "tier": "T1"}]}))
        self.assertEqual(lr.sync_backlog(self.root, deps), ["A: queued for N6"])
        self.assertEqual(lr.sync_backlog(self.root, deps), [])
        self.assertEqual([e["id"] for e in json.loads(deps.read_text())["backlog"]], ["A"])


if __name__ == "__main__":
    unittest.main()
