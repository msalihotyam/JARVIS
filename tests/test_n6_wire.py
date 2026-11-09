"""N6-WIRE1: one shadow run of the driver, supervisor and planner over the N6 modules.

Fakes only: git, CI (checks) and the phone sink; a frozen clock; every state file lives
in a TemporaryDirectory (never build/conveyor/, never data/jarvis.db)."""
import json
import tempfile
import unittest
from pathlib import Path

from jarvis import n6_breaker, n6_deps, n6_notify
from jarvis.work_driver import N6
from jarvis.work_ledger import StepState
from jarvis.work_planner import n6_next
from jarvis.work_supervisor import n6_record_finish, supervise
from tests.test_work_driver import FakePorts, gates, result, run_drive
from tests.test_work_supervisor import _sit

MAIN = "c" * 40
HEADER = f"@jarvis lane=N6-77 model=opus effort=high base={MAIN}"
NOW = 1_800_000_000.0


class FakeGit:
    def rev_parse(self, rev):
        return "d" * 40

    def patch_id(self, sha):
        return "e" * 40


class ShadowRunTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.owned = {}

    def n6(self, **kw):
        sl = {"id": "N6-77", "header": HEADER, "files": ["a.py"], "depends_on": []}
        return N6(self.dir, lambda: NOW, lambda step: (sl, MAIN, self.owned, []), **kw)

    def drive(self, n6, script=None):
        ports = FakePorts({"a": script or [result()]})
        ports.n6 = n6
        _, steps = run_drive(ports)
        return ports, steps["a"]

    def pending(self):
        f = self.dir / "notify.json"
        return json.loads(f.read_text("utf-8")).get("pending", []) if f.exists() else []

    def test_shadow_pass_accepts(self):
        ports, step = self.drive(self.n6(watchdog=lambda s, r: "continue"))
        self.assertIs(step.state, StepState.ACCEPTED)
        self.assertEqual(ports.points(), [])  # every notify went through n6_notify

    def test_preflight_block_never_starts(self):
        self.owned = {"N6-99": ["a.py"]}
        ports, step = self.drive(self.n6())
        self.assertNotIn(("start",), ports.log)
        self.assertIn("preflight", gates(ports))
        self.assertEqual(ports.count("apply"), 0)

    def test_bad_state_dir_holds(self):
        ports, _ = self.drive(N6(self.dir / "missing", lambda: NOW, lambda s: ({}, MAIN, {}, [])))
        self.assertNotIn(("start",), ports.log)
        self.assertIn("preflight", gates(ports))

    def test_outage_holds_and_is_not_counted(self):
        ports, step = self.drive(self.n6(watchdog=lambda s, r: "outage"))
        self.assertIs(step.state, StepState.INTERRUPTED)
        self.assertEqual(step.attempt, 1)
        self.assertIn("outage", gates(ports))
        self.assertEqual(ports.count("apply"), 0)
        self.assertFalse(n6_record_finish(self.dir, "N6-77", "BLOCKED", NOW, NOW, outage=True))
        self.assertFalse((self.dir / "breaker.json").exists())

    def test_orchestrator_only_event_never_pages(self):
        self.owned = {"N6-99": ["a.py"]}  # the hold notifies slice_failed_after_cap
        self.drive(self.n6())
        self.assertIs(n6_notify.classify("slice_failed_after_cap"), n6_notify.Route.ORCHESTRATOR)
        self.assertEqual(self.pending(), [])  # nothing queued for the phone sink

    def test_int_refused_on_bad_picks(self):
        report = "RUN_ID: r1\nPICKS: N6-77 abc\n"
        n6 = self.n6(int_check=lambda s: (FakeGit(), "ci/int-1", lambda: report, "r1"))
        ports, step = self.drive(n6)
        self.assertIsNot(step.state, StepState.ACCEPTED)
        self.assertEqual(ports.count("apply"), 0)

    def test_breaker_holds_only_the_open_root(self):
        for i in range(n6_breaker.BREAKER_BAD):
            self.assertTrue(n6_record_finish(self.dir, "N6-77", "BLOCKED", NOW + i, NOW + i))
        held = supervise(_sit(), n6_dir=self.dir, slice_id="N6-77")
        self.assertEqual((held.action, held.rule_id), ("wait", "n6.breaker"))
        free = supervise(_sit(), n6_dir=self.dir, slice_id="N6-78")
        self.assertEqual(free, supervise(_sit()))
        (self.dir / "breaker.json").write_text("{not json", "utf-8")
        self.assertEqual(supervise(_sit(), n6_dir=self.dir, slice_id="N6-78").rule_id, "n6.breaker")

    # N6-WIRE1FIX NEG: every wiring hold is a non-worker decision with constraint "none" (H6).
    def assertHeld(self, d, gate):
        self.assertEqual((d.action, d.rule_id, d.constraint), ("wait", f"n6.{gate}", "none"))
        self.assertEqual((d.groups, d.options), ((), ()))

    def gate_decision(self, ports, gate):
        got = [e[3]["decision"] for e in ports.log
               if e[0] == "event" and isinstance(e[3], dict) and e[3].get("gate") == gate]
        self.assertTrue(got, gate)
        return got[-1]

    def test_neg_breaker_open_corrupt_or_no_slice_is_constraint_none(self):
        self.assertEqual(supervise(_sit()).constraint, "any")  # the decision being held lets a worker start
        for i in range(n6_breaker.BREAKER_BAD):
            n6_record_finish(self.dir, "N6-77", "BLOCKED", NOW + i, NOW + i)
        self.assertHeld(supervise(_sit(), n6_dir=self.dir, slice_id="N6-77"), "breaker")
        class BadRepr(str):
            def __repr__(self):
                raise RuntimeError("repr")
        for sid in (None, "", "   ", 77, BadRepr("N6-77")):
            self.assertHeld(supervise(_sit(), n6_dir=self.dir, slice_id=sid), "breaker")
        (self.dir / "breaker.json").write_text("{not json", "utf-8")
        self.assertHeld(supervise(_sit(), n6_dir=self.dir, slice_id="N6-78"), "breaker")
        (self.dir / "breaker.json").write_bytes(b"\xff\xfe\x00")
        self.assertHeld(supervise(_sit(), n6_dir=self.dir, slice_id="N6-78"), "breaker")

    def test_neg_preflight_and_outage_hold_are_constraint_none(self):
        self.owned = {"N6-99": ["a.py"]}
        ports, _ = self.drive(self.n6())
        self.assertEqual(self.gate_decision(ports, "preflight"),
                         {"action": "wait", "rule_id": "n6.preflight", "constraint": "none"})
        self.owned = {}
        ports, _ = self.drive(self.n6(watchdog=lambda s, r: "outage"))
        self.assertEqual(self.gate_decision(ports, "outage"),
                         {"action": "wait", "rule_id": "n6.outage", "constraint": "none"})

    def test_neg_deps_error_holds_everything(self):
        (self.dir / "deps.json").write_text("{not json", "utf-8")
        got = n6_next(self.dir, lambda sha: None)
        self.assertEqual((got.start, got.held_all), ((), True))
        got = n6_next(None, lambda sha: None)  # the wiring's own error path
        self.assertEqual((got.start, got.held_all), ((), True))
        self.assertTrue(got.attention[0].startswith("n6.deps: "))

    def test_dependent_waits_for_verified_push(self):
        deps = self.dir / "deps.json"
        n6_deps.enqueue(deps, [n6_deps.Item("INT-1", is_int=True), n6_deps.Item("N6-80", ("INT-1",))])
        pushed = {}
        verify = lambda sha: pushed.get(sha)  # noqa: E731
        self.assertEqual([s for s, _ in n6_next(self.dir, verify).start], ["INT-1"])
        n6_deps.record(deps, "INT-1", "done", "f" * 40)
        self.assertEqual(n6_next(self.dir, verify).start, ())  # done but not pushed
        n6_deps.record_push(deps, "INT-1")
        self.assertEqual(n6_next(self.dir, verify).start, ())  # pushed but not verified
        pushed["f" * 40] = MAIN
        self.assertEqual([s for s, _ in n6_next(self.dir, verify).start], ["N6-80"])
        self.assertTrue(n6_next(self.dir / "nope", verify).held_all or not n6_next(self.dir / "nope", verify).start)


if __name__ == "__main__":
    unittest.main()
