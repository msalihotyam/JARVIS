"""PM24 fake-worker matrix: no process, database, network or wall clock."""

import unittest
from datetime import datetime, timedelta, timezone

from jarvis.work_checks import CheckResult
from jarvis.work_driver import drive
from jarvis.work_ledger import (
    EventKind,
    RunState,
    StepState,
    WorkRun,
    WorkStep,
    advance_run,
    idempotency_key,
    step_key,
)
from jarvis.work_regress import RegressionVerdict
from jarvis.workers.base import Worker, WorkerResult

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN = "run-1"
CHECK = CheckResult(("python", "-m", "unittest"), "python", 0, 1.0, "", "", False, False)
HUNG = CheckResult(("python", "-m", "unittest"), "python", None, 9.0, "", "", False, True)
PASS = RegressionVerdict(True, frozenset(), ())
FAIL = RegressionVerdict(False, frozenset({"tests.t.T.test_x"}), ())
TREE = "ab" * 20
KEY_A = step_key(RUN, 1, "a")
INTENT = idempotency_key(RUN, KEY_A, 1, "apply:" + TREE)


def result(status="completed", summary="done, tests pass"):
    return WorkerResult(status, summary, ("a.py",), ())


def _next(seq):
    item = seq.pop(0) if len(seq) > 1 else seq[0]
    if isinstance(item, Exception):
        raise item
    return item


class FakeWorker(Worker):
    def __init__(self, log, script):
        self.log, self.script, self.tasks = log, list(script), []

    def start(self, task):
        self.tasks.append(task)
        self.log.append(("start",))
        return f"w{len(self.tasks)}"

    def status(self, run_id):
        return "done"

    def stop(self, run_id):
        self.log.append(("stop", run_id))

    def collect(self, run_id):
        return _next(self.script)


class FakePorts:
    DEFAULTS = {
        "stage_create": ["stage"], "budget_exhausted": [False], "verify_cwd": [None],
        "link_rescan": [[]], "scope_check": [[]], "run_checks": [[CHECK]],
        "regress": [PASS], "review": [[]], "evidence": ["receipt"], "apply": ["ckpt"],
        "tree_hash": [TREE], "apply_intent": [None], "applied": [False], "undo": [None],
    }

    def __init__(self, workers, **script):
        self.log, self.now, self.keys = [], T0, []
        self.script = {k: list(script.get(k, v)) for k, v in self.DEFAULTS.items()}
        self.workers = {slug: FakeWorker(self.log, s) for slug, s in workers.items()}

    def __getattr__(self, name):
        if name not in self.__dict__.get("script", {}):
            raise AttributeError(name)

        def port(*args):
            self.log.append((name,))
            return _next(self.script[name])
        return port

    def clock(self):
        self.now += timedelta(seconds=1)
        return self.now

    def append_event(self, kind, key, payload, idempotency_key=None):
        if idempotency_key is not None:
            if idempotency_key in self.keys:
                raise ValueError("idempotency key reused")
            self.keys.append(idempotency_key)
        self.log.append(("event", kind, key, payload))

    def notify(self, point, dedupe_key, key):
        self.log.append(("notify", point, dedupe_key))

    def lease_claim(self, step):
        self.log.append(("claim", step.slug))

    def lease_release(self, step):
        self.log.append(("release", step.slug))

    def route(self, step):
        return step.slug

    def worker(self, name):
        return self.workers[name]

    def task(self, step, stage, findings):
        return {"slug": step.slug, "findings": findings}

    def needs_input(self, worker_result):
        return worker_result.summary == "question?"

    def count(self, name):
        return sum(1 for entry in self.log if entry == (name,))

    def points(self):
        return [e[1] for e in self.log if e[0] == "notify"]

    def states(self, slug):
        key = step_key(RUN, sorted(self.workers).index(slug) + 1, slug)
        return [e[3]["state"] for e in self.log
                if e[0] == "event" and e[1] is EventKind.STEP_STATE and e[2] == key]


def run_drive(ports, *deps):
    """Steps are the sorted worker slugs; ``deps`` are (slug, on_slug) pairs."""
    slugs = sorted(ports.workers)
    keys = {slug: step_key(RUN, n, slug) for n, slug in enumerate(slugs, 1)}
    steps = [WorkStep(RUN, keys[slug], n, slug, StepState.PENDING, T0,
                      depends_on=tuple(keys[on] for s, on in deps if s == slug))
             for n, slug in enumerate(slugs, 1)]
    run, steps = drive(WorkRun(RUN, RunState.BUILDING, T0, T0), steps, ports)
    return run, {s.slug: s for s in steps}


def resume(ports, attempt=1):
    """Drive step ``a`` resumed in ACCEPTING, as after a crash mid-accept."""
    step = WorkStep(RUN, KEY_A, 1, "a", StepState.ACCEPTING, T0, attempt=attempt)
    run, (step,) = drive(WorkRun(RUN, RunState.BUILDING, T0, T0), (step,), ports)
    return run, step


def gates(ports):
    return [e[3]["gate"] for e in ports.log if e[0] == "event" and "gate" in e[3]]


class DriverMatrixTest(unittest.TestCase):
    def test_pass_events_and_notify_order(self):
        ports = FakePorts({"a": [result()]})
        run, steps = run_drive(ports)
        self.assertIs(run.state, RunState.ACCEPTING)
        self.assertIs(steps["a"].state, StepState.ACCEPTED)
        self.assertEqual(steps["a"].attempt, 1)
        flow = [e[1] if e[0] in ("claim", "release") else e[0] for e in ports.log
                if e[0] in ("claim", "release", "start", "apply")]
        self.assertEqual(flow, ["a", "start", "apply", "a"])
        self.assertEqual(ports.states("a"), ["running", "accepting", "accepted"])
        self.assertEqual(ports.points(), ["slice_started", "slice_accepted"])
        order = [e[0] if e[0] != "event" else e[3].get("gate", e[3].get("state")) for e in ports.log
                 if e[0] in ("verify_cwd", "start", "link_rescan", "scope_check", "run_checks", "regress",
                             "review", "evidence", "tree_hash", "notify", "apply", "release", "event")]
        self.assertEqual(order, ["running", "notify", "verify_cwd", "start", "link_rescan", "scope_check", "scope",
                                 "accepting", "run_checks", "checks", "regress", "regression", "review", "review",
                                 "evidence", "tree_hash", "apply_intent", "apply", "apply", "accepted", "notify",
                                 "release", "accepting"])
        self.assertEqual([e[1:] for e in ports.log if e[0] == "notify"],
                         [("slice_started", f"{KEY_A}:started:1"), ("slice_accepted", f"{KEY_A}:accepted")])

    def test_apply_intent_and_receipt_carry_idempotency_keys(self):
        ports = FakePorts({"a": [result()]})
        run_drive(ports)
        receipt = idempotency_key(RUN, KEY_A, 1, "applied:" + INTENT[:40])
        self.assertEqual(ports.keys, [INTENT, receipt])
        payloads = [e[3] for e in ports.log if e[0] == "event" and e[3].get("gate") in ("apply_intent", "apply")]
        self.assertEqual(payloads, [{"gate": "apply_intent", "idempotency_key": INTENT},
                                    {"gate": "apply", "passed": True, "idempotency_key": INTENT, "ref": "ckpt"}])

    def test_resume_with_intent_never_applies_twice(self):
        for receipted, applied in ((True, False), (False, True)):
            with self.subTest(receipted=receipted, applied=applied):
                ports = FakePorts({"a": [result()]}, apply_intent=[(INTENT, receipted)], applied=[applied])
                run, step = resume(ports)
                self.assertEqual((run.state, step.state, step.attempt),
                                 (RunState.ACCEPTING, StepState.ACCEPTED, 1))
                self.assertEqual(ports.count("apply") + ports.count("undo") + ports.count("start"), 0)
                self.assertEqual(ports.count("applied"), 0 if receipted else 1)
                self.assertEqual(ports.states("a"), ["accepted"])
                self.assertEqual(len(ports.keys), 0 if receipted else 1)
        ports = FakePorts({"a": [result()]}, apply_intent=[(INTENT, False)], applied=[False])
        run, step = resume(ports)
        self.assertEqual((step.state, step.attempt), (StepState.ACCEPTED, 2))
        self.assertEqual(ports.count("undo"), 1)
        self.assertEqual(ports.count("apply"), 1)
        self.assertLess(ports.log.index(("undo",)), ports.log.index(("start",)))
        self.assertEqual(ports.workers["a"].tasks[0]["findings"], ({"gate": "apply", "detail": "resume:not_applied"},))
        # The revision re-runs every gate before its own, fresh-keyed apply.
        self.assertEqual(gates(ports), ["apply", "scope", "checks", "regression", "review", "apply_intent", "apply"])
        self.assertEqual(ports.keys[0], idempotency_key(RUN, KEY_A, 2, "apply:" + TREE))
        ports = FakePorts({"a": [result()]}, apply_intent=[(INTENT, False)], applied=[False])
        run, step = resume(ports, attempt=6)
        self.assertEqual((step.state, ports.count("undo"), ports.count("start")), (StepState.FAILED_AFTER_CAP, 1, 0))

    def test_apply_error_undoes_then_counts_a_failed_attempt(self):
        ports = FakePorts({"a": [result()]}, apply=[RuntimeError("disk"), "ckpt"])
        run, steps = run_drive(ports)
        self.assertEqual((steps["a"].state, steps["a"].attempt), (StepState.ACCEPTED, 2))
        self.assertEqual(ports.count("undo"), 1)
        self.assertLess(ports.log.index(("undo",)), ports.log.index(("start",), ports.log.index(("undo",))))
        self.assertEqual(ports.workers["a"].tasks[1]["findings"], ({"gate": "apply", "detail": "gate_error:RuntimeError"},))
        ports = FakePorts({"a": [result()]}, apply=[RuntimeError("disk")], undo=[OSError("undo")])
        with self.assertRaises(OSError):
            run_drive(ports)
        self.assertEqual(ports.keys, [INTENT])
        self.assertEqual(ports.states("a"), ["running", "accepting"])
        self.assertEqual(ports.log[-1], ("release", "a"))

    def test_bad_tree_hash_never_applies(self):
        for tree in ("AB" * 20, "ab" * 10, TREE + "\n", None, RuntimeError("hash")):
            with self.subTest(tree=tree):
                ports = FakePorts({"a": [result()]}, tree_hash=[tree])
                run, steps = run_drive(ports)
                self.assertIs(steps["a"].state, StepState.FAILED_AFTER_CAP)
                self.assertEqual((ports.count("apply"), ports.keys), (0, []))
                self.assertEqual(ports.workers["a"].tasks[1]["findings"][0]["gate"], "apply")

    def test_collect_without_worker_result_is_a_gate_error(self):
        for bad in (None, "completed", {"status": "completed"}):
            with self.subTest(bad=bad):
                ports = FakePorts({"a": [bad]})
                run, steps = run_drive(ports)
                self.assertIs(steps["a"].state, StepState.FAILED_AFTER_CAP)
                self.assertEqual(ports.count("link_rescan") + ports.count("apply"), 0)
                self.assertEqual(ports.workers["a"].tasks[1]["findings"],
                                 ({"gate": "worker", "detail": "gate_error:TypeError"},))
                self.assertIn(("stop", "w1"), ports.log)

    def test_verify_cwd_failure_never_starts_the_worker(self):
        ports = FakePorts({"a": [result()]}, verify_cwd=[RuntimeError("cwd")])
        run, steps = run_drive(ports)
        self.assertIs(steps["a"].state, StepState.FAILED_AFTER_CAP)
        self.assertEqual((ports.workers["a"].tasks, ports.count("verify_cwd")), ([], 6))

    def test_resumed_step_at_cap_fails_without_a_worker(self):
        for state in (StepState.BLOCKED_BY_GATE, StepState.TIMEOUT, StepState.ACCEPTING):
            with self.subTest(state=state):
                ports = FakePorts({"a": [result()]})
                step = WorkStep(RUN, KEY_A, 1, "a", state, T0, attempt=6)
                run, (step,) = drive(WorkRun(RUN, RunState.BUILDING, T0, T0), (step,), ports)
                self.assertEqual((run.state, step.state), (RunState.PAUSED, StepState.FAILED_AFTER_CAP))
                self.assertEqual(ports.count("start") + ports.count("apply"), 0)

    def test_budget_none_fails_closed(self):
        ports = FakePorts({"a": [result()]}, budget_exhausted=[None])
        run, steps = run_drive(ports)
        self.assertIs(run.state, RunState.BLOCKED_BUDGET)
        self.assertEqual(ports.count("start"), 0)

    def test_apply_without_ref_is_undone_and_failed(self):
        ports = FakePorts({"a": [result(), result()]}, apply=[None, "ckpt"])
        run, steps = run_drive(ports)
        self.assertEqual((steps["a"].state, steps["a"].attempt), (StepState.ACCEPTED, 2))
        self.assertEqual((ports.count("apply"), ports.count("undo")), (2, 1))

    def test_resumed_run_with_three_failed_slices_pauses_at_once(self):
        keys = {s: step_key(RUN, n, s) for n, s in enumerate("abcd", 1)}
        steps = [WorkStep(RUN, keys[s], n, s, StepState.FAILED_AFTER_CAP, T0, attempt=6) for n, s in enumerate("abc", 1)]
        steps.append(WorkStep(RUN, keys["d"], 4, "d", StepState.PENDING, T0))
        ports = FakePorts({s: [result()] for s in "abcd"})
        run, _ = drive(WorkRun(RUN, RunState.BUILDING, T0, T0), steps, ports)
        self.assertIs(run.state, RunState.PAUSED)
        self.assertEqual(ports.log, [("event", EventKind.RUN_STATE, None, {"state": "paused"})])

    def test_escaping_exception_interrupts_running_step(self):
        ports = FakePorts({"a": [result()]})

        def boom(worker_result):
            raise KeyError("q")
        ports.needs_input = boom
        with self.assertRaises(KeyError):
            run_drive(ports)
        self.assertEqual(ports.states("a"), ["running", "interrupted"])
        self.assertEqual(ports.log[-1], ("release", "a"))

    def test_dedupe_keys_are_exact(self):
        ports = FakePorts({"a": [result()]}, regress=[FAIL])
        run_drive(ports)
        self.assertEqual([e[1:] for e in ports.log if e[0] == "notify"],
                         [("slice_started", f"{KEY_A}:started:1"), ("slice_failed_after_cap", f"{KEY_A}:failed_after_cap")])
        ports = FakePorts({"a": [result(summary="question?")]})
        run_drive(ports)
        self.assertEqual([e for e in ports.log if e[0] == "notify"][-1][1:], ("needs_input", f"{KEY_A}:needs_input:1"))
        ports = FakePorts({"a": [result()]}, regress=[FAIL], budget_exhausted=[False, True])
        run_drive(ports)
        self.assertEqual(ports.log[-1][1:], ("blocked:budget", f"budget:{KEY_A}:1"))

    def test_fail_then_pass_sends_findings_to_same_worker(self):
        ports = FakePorts({"a": [result()]}, regress=[FAIL, PASS])
        run, steps = run_drive(ports)
        self.assertIs(steps["a"].state, StepState.ACCEPTED)
        self.assertEqual(steps["a"].attempt, 2)
        tasks = ports.workers["a"].tasks
        self.assertEqual(tasks[0]["findings"], ())
        self.assertEqual(tasks[1]["findings"], ({"gate": "regression", "detail": "tests.t.T.test_x"},))
        self.assertEqual(ports.count("review"), 1)
        self.assertEqual(ports.count("apply"), 1)
        self.assertEqual(ports.points(), ["slice_started", "slice_accepted"])

    def test_cap_hit_fails_slice_and_others_continue(self):
        ports = FakePorts({"a": [result()], "b": [result()]}, regress=[FAIL] * 6 + [PASS])
        run, steps = run_drive(ports)
        self.assertIs(steps["a"].state, StepState.FAILED_AFTER_CAP)
        self.assertEqual(steps["a"].attempt, 6)
        self.assertEqual(len(ports.workers["a"].tasks), 6)
        self.assertIs(steps["b"].state, StepState.ACCEPTED)
        self.assertEqual(ports.count("apply"), 1)
        self.assertIn("slice_failed_after_cap", ports.points())
        self.assertIs(run.state, RunState.PAUSED)

    def test_three_failed_slices_pause_the_run(self):
        ports = FakePorts({s: [result()] for s in "abcd"}, regress=[FAIL])
        run, steps = run_drive(ports)
        self.assertIs(run.state, RunState.PAUSED)
        self.assertEqual([steps[s].state for s in "abc"], [StepState.FAILED_AFTER_CAP] * 3)
        self.assertIs(steps["d"].state, StepState.PENDING)
        self.assertNotIn(("claim", "d"), ports.log)
        self.assertEqual(ports.count("apply"), 0)

    def test_scope_gate_block_link_rescan_first(self):
        ports = FakePorts({"a": [result()]}, link_rescan=[["link:x"], [], []],
                          scope_check=[["out_of_scope:y"], []])
        run, steps = run_drive(ports)
        self.assertEqual(ports.states("a").count("blocked_by_gate"), 2)
        self.assertEqual(steps["a"].attempt, 3)
        self.assertEqual(ports.count("scope_check"), 2)
        self.assertEqual(ports.count("run_checks"), 1)
        self.assertEqual(ports.workers["a"].tasks[2]["findings"],
                         ({"gate": "scope", "detail": "out_of_scope:y"},))

    def test_check_timeout_is_a_revision(self):
        ports = FakePorts({"a": [result()]}, run_checks=[[HUNG], [CHECK]])
        run, steps = run_drive(ports)
        self.assertIs(steps["a"].state, StepState.ACCEPTED)
        self.assertEqual(steps["a"].attempt, 2)
        self.assertEqual(ports.count("regress"), 1)

    def test_review_major_and_unknown_block_minor_logged(self):
        ports = FakePorts({"a": [result()]}, review=[
            [{"severity": "MAJOR", "detail": "x"}], [{"severity": "weird"}],
            [{"severity": "MINOR", "detail": "nit"}]])
        run, steps = run_drive(ports)
        self.assertIs(steps["a"].state, StepState.ACCEPTED)
        self.assertEqual(steps["a"].attempt, 3)
        self.assertEqual(ports.workers["a"].tasks[1]["findings"], ({"gate": "review", "detail": "MAJOR:x"},))
        minor = [e[3] for e in ports.log if e[0] == "event" and e[3].get("gate") == "review_minor"]
        self.assertEqual(minor, [{"gate": "review_minor", "findings": ["MINOR:nit"]}])

    def test_worker_timeout_then_pass(self):
        ports = FakePorts({"a": [result("timeout"), result()]})
        run, steps = run_drive(ports)
        self.assertEqual(ports.states("a")[:2], ["running", "timeout"])
        self.assertEqual(steps["a"].attempt, 2)
        self.assertEqual(ports.count("link_rescan"), 1)

    def test_budget_exhausted_blocks_run(self):
        ports = FakePorts({"a": [result()]}, regress=[FAIL], budget_exhausted=[False, True])
        run, steps = run_drive(ports)
        self.assertIs(run.state, RunState.BLOCKED_BUDGET)
        # The ledger revises acceptance failures from ACCEPTING; resume continues there.
        self.assertIs(steps["a"].state, StepState.ACCEPTING)
        self.assertEqual(ports.points(), ["slice_started", "blocked:budget"])
        self.assertEqual(ports.log[-3][:2], ("release", "a"))
        ports.script.update(budget_exhausted=[False], regress=[PASS])
        resumed = advance_run(run, RunState.BUILDING, at=ports.clock())
        run, (step,) = drive(resumed, tuple(steps.values()), ports)
        self.assertIs(run.state, RunState.ACCEPTING)
        self.assertEqual((step.state, step.attempt), (StepState.ACCEPTED, 2))
        self.assertEqual(ports.workers["a"].tasks[1]["findings"],
                         ({"gate": "resume", "detail": "see_ledger_gate_events"},))
        self.assertEqual(ports.points().count("slice_started"), 1)
        fresh = FakePorts({"a": [result()]}, budget_exhausted=[True])
        run, steps = run_drive(fresh)
        self.assertIs(run.state, RunState.BLOCKED_BUDGET)
        self.assertIs(steps["a"].state, StepState.PENDING)
        self.assertEqual(fresh.workers["a"].tasks, [])

    def test_lying_worker_is_not_accepted(self):
        ports = FakePorts({"a": [result(summary="done, all tests pass")]}, run_checks=[[HUNG]])
        run, steps = run_drive(ports)
        self.assertIs(steps["a"].state, StepState.FAILED_AFTER_CAP)
        self.assertEqual(ports.count("apply"), 0)
        self.assertNotIn("slice_accepted", ports.points())

    def test_apply_never_runs_after_failed_gate(self):
        for port in ("link_rescan", "scope_check", "run_checks", "regress", "review", "evidence"):
            with self.subTest(port=port):
                ports = FakePorts({"a": [result()]}, **{port: [RuntimeError("boom")]})
                run, steps = run_drive(ports)
                self.assertIs(steps["a"].state, StepState.FAILED_AFTER_CAP)
                self.assertEqual(ports.count("apply"), 0)
                self.assertEqual(ports.log[-2], ("release", "a"))
        for script in ([result("failed")], [result("partial")], [RuntimeError("crash")]):
            with self.subTest(script=script):
                ports = FakePorts({"a": script})
                run, steps = run_drive(ports)
                self.assertIs(steps["a"].state, StepState.FAILED_AFTER_CAP)
                self.assertEqual(ports.count("link_rescan") + ports.count("apply"), 0)
        ports = FakePorts({"a": [RuntimeError("crash")]})
        run_drive(ports)
        self.assertIn(("stop", "w1"), ports.log)
        ports = FakePorts({"a": [result()]}, run_checks=[[]])
        run, steps = run_drive(ports)
        self.assertEqual((steps["a"].state, ports.count("regress")), (StepState.FAILED_AFTER_CAP, 0))
        self.assertIs(drive(WorkRun(RUN, RunState.BUILDING, T0, T0), (), ports)[0].state, RunState.PAUSED)
        ports = FakePorts({"a": [result()]}, apply=[RuntimeError("disk")])
        run, steps = run_drive(ports)
        self.assertIs(steps["a"].state, StepState.FAILED_AFTER_CAP)
        self.assertNotIn("slice_accepted", ports.points())

    def test_lease_released_on_exception(self):
        ports = FakePorts({"a": [result()]}, stage_create=[RuntimeError("no stage")])
        with self.assertRaises(RuntimeError):
            run_drive(ports)
        self.assertEqual([e for e in ports.log if e[0] in ("claim", "release")],
                         [("claim", "a"), ("release", "a")])
        self.assertEqual(ports.states("a"), [])

    def test_needs_input_blocks_only_dependents(self):
        ports = FakePorts({"a": [result(summary="question?")], "b": [result()], "c": [result()]})
        run, steps = run_drive(ports, ("b", "a"))
        self.assertIs(steps["a"].state, StepState.NEEDS_INPUT)
        self.assertIs(steps["b"].state, StepState.PENDING)
        self.assertIs(steps["c"].state, StepState.ACCEPTED)
        self.assertNotIn(("claim", "b"), ports.log)
        self.assertIs(run.state, RunState.NEEDS_INPUT)
        self.assertEqual(ports.points(), ["slice_started", "needs_input", "slice_started", "slice_accepted"])


if __name__ == "__main__":
    unittest.main()
