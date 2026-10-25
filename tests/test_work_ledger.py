from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from jarvis.work_ledger import (
    ACTIVE_RUN_STATES,
    MAX_REVISIONS,
    WAIT_RUN_STATES,
    EventKind,
    IllegalTransition,
    RunSize,
    RunState,
    StepState,
    WorkEvent,
    WorkLedgerError,
    WorkRun,
    WorkStep,
    advance_run,
    advance_step,
    event_payload_json,
    idempotency_key,
    input_blocked_keys,
    run_should_pause,
    run_waits_for_input,
    runnable_steps,
    step_key,
)

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)

R = RunState
S = StepState

_ENDS = {"paused", "interrupted", "failed", "cancelled"}
_WAIT_EXITS = {
    "clarifying",
    "planning",
    "awaiting_approval",
    "building",
    "accepting",
    "integrating",
    "interrupted",
    "failed",
    "cancelled",
}

# Written out independently of the module's table.
EXPECTED_RUN = {
    "clarifying": _ENDS | {"planning", "needs_input", "blocked:budget"},
    "planning": _ENDS
    | {"awaiting_approval", "building", "needs_input", "blocked:budget"},
    "awaiting_approval": _ENDS | {"building", "planning"},
    "building": _ENDS | {"accepting", "needs_input", "blocked:budget"},
    "accepting": _ENDS
    | {"integrating", "building", "needs_input", "blocked:budget"},
    "integrating": _ENDS | {"done", "building", "blocked:budget"},
    "needs_input": _WAIT_EXITS | {"paused"},
    "blocked:budget": _WAIT_EXITS | {"paused"},
    "paused": _WAIT_EXITS,
    "interrupted": _WAIT_EXITS - {"interrupted"},
    "done": set(),
    "failed": set(),
    "cancelled": set(),
}

EXPECTED_STEP = {
    "pending": {"running", "needs_input", "cancelled"},
    "running": {
        "accepting",
        "needs_input",
        "blocked_by_gate",
        "timeout",
        "interrupted",
        "cancelled",
    },
    "accepting": {
        "accepted",
        "running",
        "failed_after_cap",
        "cancelled",
    },
    "blocked_by_gate": {"running", "failed_after_cap", "cancelled"},
    "timeout": {"running", "failed_after_cap", "cancelled"},
    "needs_input": {"pending", "cancelled"},
    "interrupted": {"pending", "cancelled"},
    "accepted": set(),
    "failed_after_cap": set(),
    "cancelled": set(),
}


def run_in(state, *, size=RunSize.LARGE, resume=None):
    if state in WAIT_RUN_STATES and resume is None:
        resume = R.BUILDING
    return WorkRun("run-1", state, T0, T0, size=size, resume_state=resume)


def step(ordinal=1, slug="api", state=S.PENDING, *, deps=(), attempt=1, run_id="run-1"):
    return WorkStep(
        run_id,
        step_key(run_id, ordinal, slug),
        ordinal,
        slug,
        state,
        T0,
        depends_on=tuple(deps),
        attempt=attempt,
    )


class RunTransitionTests(unittest.TestCase):
    def test_table_matches_expected_for_every_pair(self) -> None:
        for src in R:
            for dst in R:
                legal = dst.value in EXPECTED_RUN[src.value]
                with self.subTest(src=src.value, dst=dst.value, legal=legal):
                    resume = dst if src in WAIT_RUN_STATES and dst in ACTIVE_RUN_STATES else None
                    size = RunSize.SMALL if dst is R.BUILDING else RunSize.LARGE
                    run = run_in(src, size=size, resume=resume)
                    if legal:
                        moved = advance_run(run, dst, at=T1)
                        self.assertIs(moved.state, dst)
                        self.assertEqual(moved.updated_at, T1)
                    else:
                        with self.assertRaises(IllegalTransition):
                            advance_run(run, dst, at=T1)

    def test_planning_exit_depends_on_size_d9(self) -> None:
        for size in (RunSize.SMALL, RunSize.MEDIUM):
            run = run_in(R.PLANNING, size=size)
            self.assertIs(advance_run(run, R.BUILDING, at=T1).state, R.BUILDING)
            with self.assertRaises(IllegalTransition):
                advance_run(run, R.AWAITING_APPROVAL, at=T1)
        large = run_in(R.PLANNING, size=RunSize.LARGE)
        with self.assertRaises(IllegalTransition):
            advance_run(large, R.BUILDING, at=T1)
        unknown = run_in(R.PLANNING, size=None)
        for dst in (R.BUILDING, R.AWAITING_APPROVAL):
            with self.assertRaises(IllegalTransition):
                advance_run(unknown, dst, at=T1)

    def test_wait_remembers_and_only_resumes_to_origin(self) -> None:
        run = advance_run(run_in(R.ACCEPTING), R.BLOCKED_BUDGET, at=T1)
        self.assertIs(run.resume_state, R.ACCEPTING)
        crashed = advance_run(run, R.INTERRUPTED, at=T1)
        self.assertIs(crashed.resume_state, R.ACCEPTING)
        with self.assertRaises(IllegalTransition):
            advance_run(crashed, R.BUILDING, at=T1)
        back = advance_run(crashed, R.ACCEPTING, at=T1)
        self.assertIs(back.state, R.ACCEPTING)
        self.assertIsNone(back.resume_state)
        cancelled = advance_run(crashed, R.CANCELLED, at=T1)
        self.assertIsNone(cancelled.resume_state)

    def test_time_must_not_go_backwards(self) -> None:
        run = replace(run_in(R.CLARIFYING), updated_at=T1)
        with self.assertRaises(WorkLedgerError):
            advance_run(run, R.PLANNING, at=T0)
        with self.assertRaises(WorkLedgerError):
            advance_run(run, R.PLANNING, at=datetime(2026, 10, 25, 13, 0))

    def test_run_validation(self) -> None:
        with self.assertRaises(WorkLedgerError):
            WorkRun("run-1", R.PAUSED, T0, T0)
        with self.assertRaises(WorkLedgerError):
            WorkRun("run-1", R.PAUSED, T0, T0, resume_state=R.DONE)
        with self.assertRaises(WorkLedgerError):
            WorkRun("run-1", R.BUILDING, T0, T0, resume_state=R.BUILDING)
        with self.assertRaises(WorkLedgerError):
            WorkRun("bad id", R.BUILDING, T0, T0)
        with self.assertRaises(WorkLedgerError):
            WorkRun("run-1", R.BUILDING, T1, T0)
        with self.assertRaises(WorkLedgerError):
            WorkRun("run-1", "building", T0, T0)


class StepTransitionTests(unittest.TestCase):
    def test_table_matches_expected_for_every_pair(self) -> None:
        for src in S:
            for dst in S:
                legal = dst.value in EXPECTED_STEP[src.value]
                capped = S.FAILED_AFTER_CAP in (src, dst)
                attempt = MAX_REVISIONS + 1 if capped else 1
                with self.subTest(src=src.value, dst=dst.value, legal=legal):
                    current = step(state=src, attempt=attempt)
                    if legal:
                        self.assertIs(advance_step(current, dst, at=T1).state, dst)
                    else:
                        with self.assertRaises(IllegalTransition):
                            advance_step(current, dst, at=T1)

    def test_revision_cap_is_five_d2(self) -> None:
        self.assertEqual(MAX_REVISIONS, 5)
        current = advance_step(step(), S.RUNNING, at=T0, attempt_link="h2-1")
        self.assertEqual(current.attempt, 1)
        failures = [S.ACCEPTING, S.TIMEOUT, S.BLOCKED_BY_GATE, S.ACCEPTING, S.ACCEPTING]
        for n, failure in enumerate(failures, start=2):
            if failure is S.ACCEPTING:
                with self.assertRaises(IllegalTransition):
                    advance_step(replace(current, state=failure), S.FAILED_AFTER_CAP, at=T1)
            current = advance_step(replace(current, state=failure), S.RUNNING, at=T1)
            self.assertEqual(current.attempt, n)
            self.assertIsNone(current.attempt_link)
        self.assertEqual(current.revisions, 5)
        rejected = advance_step(current, S.ACCEPTING, at=T1)
        with self.assertRaises(IllegalTransition):
            advance_step(rejected, S.RUNNING, at=T1)
        # No side door around the cap: acceptance cannot divert to a wait
        # state whose resume would start a fresh, uncounted attempt.
        for detour in (S.NEEDS_INPUT, S.INTERRUPTED):
            with self.assertRaises(IllegalTransition):
                advance_step(rejected, detour, at=T1)
        failed = advance_step(rejected, S.FAILED_AFTER_CAP, at=T1)
        self.assertIs(failed.state, S.FAILED_AFTER_CAP)
        with self.assertRaises(WorkLedgerError):
            step(state=S.FAILED_AFTER_CAP, attempt=MAX_REVISIONS)

    def test_resume_after_input_or_crash_is_not_a_revision(self) -> None:
        current = step(state=S.RUNNING, attempt=3)
        for wait in (S.NEEDS_INPUT, S.INTERRUPTED):
            waited = advance_step(current, wait, at=T1)
            again = advance_step(advance_step(waited, S.PENDING, at=T1), S.RUNNING, at=T1)
            self.assertEqual(again.attempt, 3)

    def test_attempt_beyond_cap_is_invalid(self) -> None:
        with self.assertRaises(WorkLedgerError):
            step(attempt=MAX_REVISIONS + 2)
        with self.assertRaises(WorkLedgerError):
            step(attempt=0)

    def test_step_validation(self) -> None:
        good = step()
        with self.assertRaises(WorkLedgerError):
            replace(good, key="s001-api-000000000000")
        with self.assertRaises(WorkLedgerError):
            replace(good, depends_on=(good.key,))
        with self.assertRaises(WorkLedgerError):
            replace(good, depends_on=["x"])
        with self.assertRaises(WorkLedgerError):
            replace(good, attempt_link="")


class KeyTests(unittest.TestCase):
    def test_step_key_is_stable_and_distinct(self) -> None:
        key = step_key("run-1", 2, "login-form")
        self.assertEqual(key, step_key("run-1", 2, "login-form"))
        self.assertRegex(key, r"^s002-login-form-[0-9a-f]{12}$")
        others = {
            step_key("run-2", 2, "login-form"),
            step_key("run-1", 3, "login-form"),
            step_key("run-1", 2, "login-forms"),
        }
        self.assertNotIn(key, others)
        self.assertEqual(len(others), 3)

    def test_step_key_golden_value(self) -> None:
        # Pinned so a change to the derivation is a visible contract change.
        self.assertEqual(step_key("run-1", 1, "api"), "s001-api-f819151f25af")

    def test_step_key_rejects_bad_input(self) -> None:
        for args in (
            ("run-1", 0, "api"),
            ("run-1", 1000, "api"),
            ("run-1", True, "api"),
            ("run-1", 1, "API"),
            ("run-1", 1, "-api"),
            ("run-1", 1, ""),
            ("", 1, "api"),
            ("run 1", 1, "api"),
        ):
            with self.subTest(args=args), self.assertRaises(WorkLedgerError):
                step_key(*args)

    def test_idempotency_key(self) -> None:
        key = step_key("run-1", 1, "api")
        first = idempotency_key("run-1", key, 1, "worker.dispatch")
        self.assertEqual(first, idempotency_key("run-1", key, 1, "worker.dispatch"))
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, idempotency_key("run-1", key, 2, "worker.dispatch"))
        self.assertNotEqual(first, idempotency_key("run-1", key, 1, "worker.accept"))
        self.assertNotEqual(first, idempotency_key("run-2", key, 1, "worker.dispatch"))
        for args in (
            ("run-1", key, 0, "a"),
            ("run-1", key, True, "a"),
            ("run-1", "", 1, "a"),
            ("run-1", key, 1, "Bad"),
        ):
            with self.subTest(args=args), self.assertRaises(WorkLedgerError):
                idempotency_key(*args)


class DependencyTests(unittest.TestCase):
    def test_needs_input_blocks_only_dependents_d8(self) -> None:
        a = step(1, "schema", S.NEEDS_INPUT)
        b = step(2, "api", deps=(a.key,))
        c = step(3, "ui", deps=(b.key,))
        d = step(4, "docs")
        steps = [c, d, b, a]
        self.assertEqual(input_blocked_keys(steps), {a.key, b.key, c.key})
        self.assertEqual(runnable_steps(steps), (d,))
        self.assertFalse(run_waits_for_input(steps))

    def test_run_waits_only_when_nothing_else_moves(self) -> None:
        a = step(1, "schema", S.NEEDS_INPUT)
        b = step(2, "api", deps=(a.key,))
        done = step(3, "docs", S.ACCEPTED)
        self.assertTrue(run_waits_for_input([a, b, done]))
        busy = step(4, "tests", S.RUNNING)
        self.assertFalse(run_waits_for_input([a, b, done, busy]))
        crashed = step(4, "tests", S.INTERRUPTED)
        self.assertFalse(run_waits_for_input([a, b, done, crashed]))
        self.assertFalse(run_waits_for_input([b, done]))

    def test_runnable_needs_accepted_dependencies(self) -> None:
        a = step(1, "schema", S.ACCEPTED)
        b = step(2, "api", deps=(a.key,))
        c = step(3, "ui", deps=(b.key,))
        ghost = step(4, "ghost", deps=("s999-missing-000000000000",))
        self.assertEqual(runnable_steps([ghost, c, b, a]), (b,))

    def test_pause_after_three_failed_steps_d2(self) -> None:
        failed = [step(n, f"s{n}", S.FAILED_AFTER_CAP, attempt=6) for n in (1, 2)]
        self.assertFalse(run_should_pause(failed))
        self.assertTrue(run_should_pause(failed + [step(3, "s3", S.FAILED_AFTER_CAP, attempt=6)]))


class EventTests(unittest.TestCase):
    def test_event_payload_is_canonical(self) -> None:
        payload = event_payload_json({"b": 1, "a": "ı"})
        self.assertEqual(payload, '{"a":"ı","b":1}')
        event = WorkEvent("run-1", 1, EventKind.PLAN_READY, T0, payload_json=payload)
        self.assertEqual(event.kind.value, "plan_ready")
        with self.assertRaises(WorkLedgerError):
            WorkEvent("run-1", 1, EventKind.PROGRESS, T0, payload_json='{"b":1, "a":2}')
        with self.assertRaises(WorkLedgerError):
            WorkEvent("run-1", 1, EventKind.PROGRESS, T0, payload_json="[]")
        with self.assertRaises(WorkLedgerError):
            event_payload_json({"x": float("nan")})
        with self.assertRaises(WorkLedgerError):
            event_payload_json({"x": object()})

    def test_event_validation(self) -> None:
        for kwargs in (
            {"seq": 0},
            {"seq": True},
            {"kind": "done"},
            {"at": datetime(2026, 10, 25)},
            {"step_key": ""},
        ):
            base = {"run_id": "run-1", "seq": 1, "kind": EventKind.DONE, "at": T0}
            base.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(WorkLedgerError):
                WorkEvent(**base)


if __name__ == "__main__":
    unittest.main()
