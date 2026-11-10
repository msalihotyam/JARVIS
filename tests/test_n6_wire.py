"""N6-WIRE1: one shadow run of the driver, supervisor and planner over the N6 modules.

Fakes only: git, CI (checks) and the phone sink; a frozen clock; every state file lives
in a TemporaryDirectory (never build/conveyor/, never data/jarvis.db)."""
import json
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis import n6_breaker, n6_deps, n6_notify
from jarvis.work_driver import N6, drive
from jarvis.work_ledger import MAX_REVISIONS, EventKind, RunState, StepState, WorkRun
from jarvis.work_planner import n6_next
from jarvis.work_supervisor import n6_record_finish, supervise
from tests.test_work_driver import FAIL, RUN, T0, FakePorts, gates, result, run_drive
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

    # N6-WIRE1FIX2 NEG (F1): every refcheck refusal or raise is an N6 hold, never a revision.
    def test_neg_refcheck_refusal_or_error_holds_without_revision(self):
        def boom(*a):
            raise RuntimeError("boom")

        class MovingGit(FakeGit):  # the ref moves between read_evidence and accept
            n = 0

            def rev_parse(self, rev):
                self.n += 1
                return ("d" if self.n <= 2 else "f") * 40

        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("str")

        def unprintable(*a):
            raise Unprintable()

        good = "RUN_ID: r1\nPICKS:\n  N6-77 " + "a" * 40 + " -> " + "b" * 40 + "\n"
        cases = {
            "unprintable_error": unprintable,
            "refused_bad_picks": lambda s: (FakeGit(), "ci/int-1", lambda: "RUN_ID: r1\nPICKS: N6-77 abc\n", "r1"),
            "int_check_raises": boom,
            "int_check_raises_permission": lambda s: (_ for _ in ()).throw(PermissionError("denied")),
            "read_report_raises": lambda s: (FakeGit(), "ci/int-1", boom, "r1"),
            "read_evidence_bad_ref": lambda s: (FakeGit(), 7, lambda: good, "r1"),
            "git_raises": lambda s: (type("G", (), {"rev_parse": boom, "patch_id": boom})(), "ci/int-1",
                                     lambda: good, "r1"),
            "accept_ref_moved": lambda s: (MovingGit(), "ci/int-1", lambda: good, "r1"),
            "malformed_spec": lambda s: ("not", "a", "spec"),
            "not_a_tuple": lambda s: 42,
        }
        for name, check in cases.items():
            with self.subTest(name):
                ports, step = self.drive(self.n6(int_check=check), [result(), result(), result()])
                self.assertEqual(ports.count("start"), 1)  # no worker revision
                self.assertEqual((step.revisions, step.attempt), (0, 1))
                self.assertIs(step.state, StepState.ACCEPTING)
                self.assertEqual(ports.count("apply"), 0)
                self.assertEqual(self.gate_decision(ports, "refcheck"),
                                 {"action": "wait", "rule_id": "n6.refcheck", "constraint": "none"})

    # N6-WIRE1FIX3 NEG (F1): a resumed refcheck hold re-holds (valid root cause present) at
    # revisions 0 and MAX_REVISIONS: no worker revision, no FAILED_AFTER_CAP until accepted.
    def test_neg_resumed_refcheck_hold_never_revises_or_caps(self):
        refuse = lambda s: (FakeGit(), "ci/int-1", lambda: "RUN_ID: r1\nPICKS: N6-77 abc\n", "r1")  # noqa: E731
        good = "RUN_ID: r1\nPICKS:\n  N6-77 " + "a" * 40 + " -> " + "b" * 40 + "\n"

        class PassGit(FakeGit):  # full shas resolve to themselves, the INT ref to d*40: refcheck passes
            def rev_parse(self, rev):
                return rev if len(rev) == 40 else "d" * 40

        for revisions in (0, MAX_REVISIONS):
            with self.subTest(revisions=revisions):
                ports, step = self.drive(self.n6(int_check=refuse), [result(), result(), result()])
                step = replace(step, attempt=revisions + 1)  # the hold landed on this attempt
                for e in ports.log:
                    if e[0] == "event" and e[1] is EventKind.STEP_STATE and e[2] == step.key:
                        e[3]["attempt"] = step.attempt
                seen, held = len(ports.states("a")), gates(ports).count("refcheck")
                for n6 in (ports.n6, ports.n6, None):  # None: no N6 cannot accept, still holds
                    ports.n6 = n6
                    step = self.redrive(ports, step)
                    held += 1
                    self.assertIs(step.state, StepState.ACCEPTING)
                    self.assertEqual(step.revisions, revisions)
                    self.assertEqual(gates(ports)[-1], "refcheck")
                    self.assertEqual(gates(ports).count("refcheck"), held)
                    self.assertEqual(self.gate_decision(ports, "refcheck"),
                                     {"action": "wait", "rule_id": "n6.refcheck", "constraint": "none"})
                self.assertEqual(ports.states("a")[seen:], [])  # no RUNNING, no FAILED_AFTER_CAP
                self.assertEqual((ports.count("start"), ports.count("apply")), (1, 0))
                ports.n6 = self.n6(int_check=lambda s: (PassGit(), "ci/int-1", lambda: good, "r1"))
                step = self.redrive(ports, step)  # the evidence is accepted: the step proceeds
                self.assertIs(step.state, StepState.ACCEPTED)
                self.assertEqual(step.revisions, revisions)
                self.assertEqual((ports.count("start"), ports.count("apply")), (1, 1))
                self.assertEqual(ports.states("a")[seen:], [StepState.ACCEPTED.value])

    def redrive(self, ports, step):
        _, (step,) = drive(WorkRun(RUN, RunState.BUILDING, T0, T0), (step,), ports)
        return step

    def test_no_int_check_still_accepts(self):
        for check in (lambda s: None, None):
            ports, step = self.drive(self.n6(int_check=check))
            self.assertIs(step.state, StepState.ACCEPTED)
            self.assertIn("refcheck", gates(ports))

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

    # N6-WIRE1FIX4: a persisted refcheck hold and the non-accept outcomes a resume can meet.
    KINDS = ("review_major", "review_raises", "checks_raise", "regress_fails", "int_check_none",
             "int_check_returns_none", "int_check_raises", "no_n6", "findings_unreadable", "refused",
             "hold_row_gate_corrupt")
    WAIT = {"action": "wait", "rule_id": "n6.refcheck", "constraint": "none"}

    def accepting(self):
        class PassGit(FakeGit):  # full shas resolve to themselves, the INT ref to d*40: refcheck passes
            def rev_parse(self, rev):
                return rev if len(rev) == 40 else "d" * 40

        good = "RUN_ID: r1\nPICKS:\n  N6-77 " + "a" * 40 + " -> " + "b" * 40 + "\n"
        return self.n6(int_check=lambda s: (PassGit(), "ci/int-1", lambda: good, "r1"))

    def held(self, revisions):
        self.setUp()  # a fresh state directory per held step
        refuse = self.n6(int_check=lambda s: (FakeGit(), "ci/int-1", lambda: "RUN_ID: r1\nPICKS: N6-77 abc\n", "r1"))
        ports, step = self.drive(refuse, [result(), result(), result()])
        step = replace(step, attempt=revisions + 1)  # the hold landed on this attempt
        for e in ports.log:
            if e[0] == "event" and e[1] is EventKind.STEP_STATE and e[2] == step.key:
                e[3]["attempt"] = step.attempt
        return ports, step, len(ports.states("a")), len(ports.log)

    def spoil(self, ports, kind):
        """Make the next resume meet one non-accept outcome; returns the undo."""
        def boom(*a):
            raise OSError(kind)

        scripts = {"review_major": ("review", [[{"severity": "major", "detail": "flaky"}]]),
                   "review_raises": ("review", [RuntimeError("review down")]),
                   "checks_raise": ("run_checks", [OSError("checks down")]),
                   "regress_fails": ("regress", [FAIL])}
        swaps = {"int_check_none": lambda: self.n6(int_check=None),
                 "int_check_returns_none": lambda: self.n6(int_check=lambda s: None),
                 "int_check_raises": lambda: self.n6(int_check=boom),
                 "no_n6": lambda: None}
        if kind in scripts:
            port, items = scripts[kind]
            old, ports.script[port] = ports.script[port], items
            return lambda: ports.script.__setitem__(port, old)
        if kind in swaps:
            old, ports.n6 = ports.n6, swaps[kind]()
            return lambda: setattr(ports, "n6", old)
        if kind == "findings_unreadable":
            ports.gate_findings = boom
            return lambda: delattr(ports, "gate_findings")
        if kind == "hold_row_gate_corrupt":  # the hold row is still known by its decision
            rows = [e[3] for e in ports.log if e[0] == "event" and e[3].get("gate") == "refcheck"]
            for r in rows:
                r["gate"] = "Refcheck"
            return lambda: [r.__setitem__("gate", "refcheck") for r in rows]
        return lambda: None  # "refused": refcheck just refuses again

    def assert_still_held(self, ports, step, revisions, seen, mark):
        self.assertIs(step.state, StepState.ACCEPTING)
        self.assertEqual(step.revisions, revisions)
        self.assertEqual(ports.states("a")[seen:], [])  # no RUNNING, no FAILED_AFTER_CAP
        self.assertEqual((ports.count("start"), ports.count("apply")), (1, 0))
        rows = [e[3] for e in ports.log[mark:] if e[0] == "event" and "gate" in e[3]]
        self.assertEqual((rows[-1]["gate"], rows[-1].get("decision")), ("refcheck", self.WAIT))
        # no revisable gate row replaces the hold: every failing row is a wait
        self.assertEqual([r["gate"] for r in rows if r.get("passed") is False and r.get("decision") != self.WAIT], [])

    def assert_accepted_once(self, ports, step, revisions, seen):
        ports.n6 = self.accepting()
        step = self.redrive(ports, step)  # the evidence is accepted: the step proceeds, once
        self.assertIs(step.state, StepState.ACCEPTED)
        self.assertEqual((step.revisions, ports.count("start"), ports.count("apply")), (revisions, 1, 1))
        self.assertEqual(ports.states("a")[seen:], [StepState.ACCEPTED.value])

    # N6-WIRE1FIX4 NEG (F1-F3): every non-accept outcome on a resumed hold re-holds, the NEXT
    # resume still holds, and only accepted evidence lets the step proceed.
    def test_neg_resumed_hold_survives_every_non_accept_outcome(self):
        for revisions in (0, MAX_REVISIONS):
            for kind in self.KINDS:
                with self.subTest(revisions=revisions, kind=kind):
                    ports, step, seen, mark = self.held(revisions)
                    undo = self.spoil(ports, kind)
                    step = self.redrive(ports, step)
                    self.assert_still_held(ports, step, revisions, seen, mark)
                    undo()
                    step = self.redrive(ports, step)  # the NEXT resume: the hold is still the finding
                    self.assert_still_held(ports, step, revisions, seen, mark)
                    self.assert_accepted_once(ports, step, revisions, seen)

    # N6-WIRE1FIX4 property: N resumes with randomised gate / refcheck outcomes (fixed seed, no
    # clock) keep the invariant on every step.
    def test_property_random_resumes_keep_the_hold(self):
        rng = random.Random(64)
        for revisions in (0, MAX_REVISIONS):
            ports, step, seen, mark = self.held(revisions)
            for n in range(25):
                kinds = [rng.choice(self.KINDS[4:])] + ([rng.choice(self.KINDS[:4])] if rng.random() < 0.5 else [])
                with self.subTest(revisions=revisions, n=n, kinds=kinds):
                    undos = [self.spoil(ports, k) for k in kinds]
                    step = self.redrive(ports, step)
                    self.assert_still_held(ports, step, revisions, seen, mark)
                    for undo in reversed(undos):
                        undo()
            self.assert_accepted_once(ports, step, revisions, seen)

    # N6-WIRE1FIX4 NEG (F4): a mark_started error after an earlier mark keeps that start.
    def test_neg_partial_mark_error_keeps_the_marked_start(self):
        deps, verify, real, calls = self.dir / "deps.json", (lambda sha: None), n6_deps.mark_started, []
        n6_deps.enqueue(deps, [n6_deps.Item("N6-81"), n6_deps.Item("N6-82"), n6_deps.Item("N6-83")])

        def flaky(path, sid, check):
            calls.append(sid)
            if len(calls) == 2:
                raise OSError("disk")
            return real(path, sid, check)

        with mock.patch.object(n6_deps, "mark_started", side_effect=OSError("disk")):
            got = n6_next(self.dir, verify)  # nothing marked yet: the fail-closed shape
        self.assertEqual((got.start, got.held_all), ((), True))
        with mock.patch.object(n6_deps, "mark_started", side_effect=flaky):
            got = n6_next(self.dir, verify)
        self.assertEqual(([s for s, _ in got.start], got.held_all), (["N6-81"], False))
        self.assertEqual([s for s, _ in got.held], ["N6-82", "N6-83"])
        self.assertTrue(got.attention[-1].startswith("n6.deps: "))
        self.assertIn("started before it: N6-81", got.attention[-1])
        self.assertEqual([s for s, _ in n6_next(self.dir, verify).start], ["N6-82", "N6-83"])  # none lost


if __name__ == "__main__":
    unittest.main()
