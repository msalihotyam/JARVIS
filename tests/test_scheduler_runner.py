"""Scheduler phase 2D: the minimum foreground runner.

Covers ``SchedulerRunner`` as a DRIVER of the existing boundaries and nothing
more: a due job materialized, claimed, dispatched and settled through the real
Core; the kill switch stopping new occurrences and new claims before anything
is created or owned; an expired RUNNING lease reaching ``recover_job_run`` and
a live one being left alone; a parked generation never resumed, answered or
claimed; ``UNKNOWN_OUTCOME`` never retried by any number of sweeps; a recorded
cancellation carried through ``cancel_job_run`` rather than resumed; a
condition watch reaching phase 2C's evaluation; Ctrl+C stopping the loop
cleanly at the boundary and, arriving mid-dispatch, propagating without
writing an outcome; and the ``jarvis scheduler run`` command.

Every instant is EXPLICIT, every clock and every sleep is injected. Nothing
sleeps for real, nothing runs in the background, and no test performs a live
model, auth or network call.
"""

from __future__ import annotations

from dataclasses import replace

from jarvis.app import EXIT_INVALID_INPUT, EXIT_OK, create_app
from jarvis.cli import main
from jarvis.conditions import ConditionKind
from jarvis.contracts import JobRunStatus
from jarvis.dispatch import DISPATCH_SETTLED
from jarvis.scheduler_runner import (
    ACTION_CANCELLED,
    ACTION_DISPATCHED,
    ACTION_EVALUATED,
    ACTION_PARKED,
    ACTION_RECOVERED,
    ACTION_SKIPPED,
    DEFAULT_POLL_INTERVAL,
    SchedulerRunner,
)
from tests.test_condition_watch import ConditionWatchTestCase, sha256_of
from tests.test_scheduler_jobs import NOW  # noqa: F401
from tests.test_scheduler_recovery import (  # noqa: F401
    ACTOR,
    WHY,
    WORKER_A,
    RecoveryTestCase,
    setUpModule,
    tearDownModule,
)
from tests.test_task_chain import _DRIVER_POLICY


class RunnerTestCase(RecoveryTestCase):
    """The recovery harness: a real store, a real Core, real local connectors."""

    def runner(self, *, at=None, sleep=None) -> SchedulerRunner:
        return SchedulerRunner(
            self.core,
            clock=lambda: at or self.after,
            sleep=sleep or (lambda _seconds: None),
        )

    def only_action(self, report):
        self.assertEqual(1, len(report.actions), report)
        return report.actions[0]


# --------------------------------------------------------------------------
# DUE WORK - materialize, claim, dispatch, settle
# --------------------------------------------------------------------------


class DueWorkTests(RunnerTestCase):
    def test_a_due_job_is_claimed_dispatched_and_settled_through_core(self) -> None:
        record = self.memory.create_job(self.spec(steps=1), now=NOW)
        report = self.runner(at=self.at).run_once()

        self.assertFalse(report.claims_suspended)
        self.assertEqual(1, report.jobs_considered)
        action = self.only_action(report)
        self.assertEqual(ACTION_DISPATCHED, action.action)
        self.assertEqual(DISPATCH_SETTLED, action.verdict, action.reason)
        self.assertEqual("chain_completed", action.reason)
        self.assertEqual(record.id, action.job_id)
        stored = self.reload(action.run_id)
        self.assertEqual(JobRunStatus.COMPLETED, stored.status)
        self.assertTrue(stored.settled)
        self.assertTrue(stored.owner_token.startswith("scheduler-"))
        # Core reached the connector exactly once; the runner never did.
        self.assertEqual(1, len(self.reader.calls))

    def test_a_second_sweep_does_not_redispatch_a_settled_generation(self) -> None:
        self.memory.create_job(self.spec(steps=1), now=NOW)
        runner = self.runner(at=self.at)
        runner.run_once()
        second = runner.run_once()
        self.assertEqual((), second.actions)
        self.assertEqual(1, len(self.reader.calls))

    def test_kill_switch_creates_no_occurrence_and_takes_no_claim(self) -> None:
        record = self.memory.create_job(self.spec(steps=1), now=NOW)
        self.core.settings = replace(self.settings, execution_enabled=False)
        report = self.runner(at=self.at).run_once()

        self.assertTrue(report.claims_suspended)
        self.assertEqual((), report.actions)
        # Nothing durable was created while execution was off.
        self.assertIsNone(self.memory.load_job(record.id).last_run_id)
        self.assertEqual(0, len(self.reader.calls))

    def test_kill_switch_leaves_an_existing_pending_generation_unclaimed(self) -> None:
        record = self.memory.create_job(self.spec(steps=1), now=NOW)
        run = self.memory.materialize_due_job_run(record.id, record.revision, now=self.at)
        self.core.settings = replace(self.settings, execution_enabled=False)
        action = self.only_action(self.runner(at=self.at).run_once())

        self.assertEqual(ACTION_SKIPPED, action.action)
        self.assertEqual("execution_disabled", action.reason)
        self.assertIsNone(action.verdict)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.PENDING, stored.status)
        self.assertIsNone(stored.owner_token)
        self.assertEqual(0, len(self.reader.calls))

    def test_a_disabled_job_is_not_touched(self) -> None:
        record = self.memory.create_job(self.spec(steps=1), now=NOW)
        self.memory.set_job_enabled(record.id, record.revision, False, now=NOW)
        report = self.runner(at=self.at).run_once()
        self.assertEqual((), report.actions)
        self.assertEqual(0, len(self.reader.calls))


# --------------------------------------------------------------------------
# UNSETTLED WORK - recover, park, never retry
# --------------------------------------------------------------------------


class UnsettledWorkTests(RunnerTestCase):
    def test_an_expired_running_lease_is_recovered_through_recover_job_run(self) -> None:
        run, _dispatch = self.bound()
        action = self.only_action(self.runner().run_once())

        self.assertEqual(ACTION_RECOVERED, action.action)
        self.assertEqual(DISPATCH_SETTLED, action.verdict, action.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.COMPLETED, stored.status)
        # THE SAME session was resumed under a new fence, never a second one.
        self.assertEqual(run.session_id, stored.session_id)
        self.assertGreater(stored.fencing_epoch, run.fencing_epoch)
        self.assertEqual(1, len(self.reader.calls))

    def test_a_live_running_lease_is_left_to_its_owner(self) -> None:
        run, _dispatch = self.bound()
        action = self.only_action(self.runner(at=self.just_live).run_once())

        self.assertEqual(ACTION_SKIPPED, action.action)
        self.assertEqual("lease_live", action.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(run.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(0, len(self.reader.calls))

    def test_a_parked_generation_is_never_resumed_answered_or_claimed(self) -> None:
        run, _run, task_id, _request = self.parked()
        action = self.only_action(self.runner().run_once())

        self.assertEqual(ACTION_PARKED, action.action)
        self.assertEqual("awaiting_approval", action.reason)
        self.assertIsNone(action.verdict)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, stored.status)
        self.assertIsNone(stored.owner_token)
        self.assertIsNotNone(self.memory.load_pending_approval(task_id, run.session_id))
        # The egress step is still waiting for its person.
        self.assertEqual(0, len(self.planner.calls))

    def test_unknown_outcome_is_never_retried_by_any_sweep(self) -> None:
        run, _dispatch = self.bound()
        self.memory.mark_job_run_unknown(run.id, WORKER_A, run.fencing_epoch, now=self.at)
        runner = self.runner()
        for _ in range(3):
            action = self.only_action(runner.run_once())
            self.assertEqual(ACTION_SKIPPED, action.action)
            self.assertEqual("unknown_outcome", action.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.UNKNOWN_OUTCOME, stored.status)
        self.assertEqual(run.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(0, len(self.reader.calls))

    def test_a_recorded_cancellation_is_carried_not_resumed(self) -> None:
        run, _dispatch = self.bound()
        self.memory.request_job_run_cancel(
            run.id, now=self.at, actor_label=ACTOR, reason=WHY
        )
        action = self.only_action(self.runner().run_once())

        self.assertEqual(ACTION_CANCELLED, action.action)
        self.assertEqual(DISPATCH_SETTLED, action.verdict, action.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.CANCELLED, stored.status)
        self.assertTrue(stored.cancel_requested)
        # Nothing was executed on the way to stopping it.
        self.assertEqual(0, len(self.reader.calls))


# --------------------------------------------------------------------------
# CONDITION WATCH - phase 2C's evaluation, reached by the runner
# --------------------------------------------------------------------------


class ConditionWatchRunnerTests(ConditionWatchTestCase):
    def test_a_due_watch_is_evaluated_and_settled_skipped_on_false(self) -> None:
        self.write_file("notes.txt", b"the actual content")
        self.memory.create_job(
            self.spec(
                condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
                expected_sha256=sha256_of(b"something else"),
            ),
            now=NOW,
        )
        report = SchedulerRunner(self.core, clock=lambda: self.at).run_once()

        self.assertEqual(1, len(report.actions), report)
        action = report.actions[0]
        self.assertEqual(ACTION_EVALUATED, action.action)
        self.assertEqual(DISPATCH_SETTLED, action.verdict, action.reason)
        self.assertEqual("condition_false", action.reason)
        stored = self.reload(action.run_id)
        self.assertEqual(JobRunStatus.SKIPPED, stored.status)
        self.assertEqual("false", stored.condition_result)
        self.assertEqual(0, self.sessions())


# --------------------------------------------------------------------------
# SHUTDOWN - Ctrl+C at the boundary, and inside a dispatch
# --------------------------------------------------------------------------


class ShutdownTests(RunnerTestCase):
    def test_run_until_stopped_finishes_the_sweep_then_stops_on_interrupt(self) -> None:
        self.memory.create_job(self.spec(steps=1), now=NOW)
        waits: list[float] = []

        def interrupt(seconds: float) -> None:
            waits.append(seconds)
            raise KeyboardInterrupt

        reports = self.runner(at=self.at, sleep=interrupt).run_until_stopped()

        self.assertEqual(1, len(reports))
        self.assertEqual([DEFAULT_POLL_INTERVAL.total_seconds()], waits)
        action = self.only_action(reports[0])
        self.assertEqual(JobRunStatus.COMPLETED, self.reload(action.run_id).status)
        self.assertEqual(1, len(self.reader.calls))

    def test_an_interrupt_inside_a_dispatch_propagates_and_writes_no_outcome(self) -> None:
        record = self.memory.create_job(self.spec(steps=1), now=NOW)

        def die(_session_id):
            raise KeyboardInterrupt

        self.core.run_chain = die
        runner = self.runner(at=self.at)
        with self.assertRaises(KeyboardInterrupt):
            runner.run_once()

        # The generation stands exactly as the fenced boundary last committed
        # it: bound, RUNNING, unsettled. No ending was written for it.
        stored = self.reload(self.memory.load_job(record.id).last_run_id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertIsNotNone(stored.session_id)
        self.assertFalse(stored.settled)
        self.assertEqual(0, len(self.reader.calls))

    def test_run_until_stopped_swallows_only_the_loop_level_interrupt(self) -> None:
        self.memory.create_job(self.spec(steps=1), now=NOW)

        def die(_session_id):
            raise KeyboardInterrupt

        self.core.run_chain = die

        def never(_seconds: float) -> None:
            # The interrupt must arrive from INSIDE the sweep; reaching the
            # wait at all would mean the dispatch was refused instead.
            raise AssertionError("the sweep returned instead of being interrupted")

        reports = self.runner(at=self.at, sleep=never).run_until_stopped()
        # The interrupted sweep never produced a report, and nothing settled.
        self.assertEqual((), reports)


# --------------------------------------------------------------------------
# CLI - jarvis scheduler run
# --------------------------------------------------------------------------


class CommandTests(RunnerTestCase):
    def app(self):
        return create_app(
            settings=self.settings,
            memory=self.memory,
            registry=self.registry,
            destination_policy=_DRIVER_POLICY,
        )

    def test_scheduler_run_once_reports_the_sweep(self) -> None:
        self.memory.create_job(self.spec(steps=1), now=NOW)
        result = self.app().run_scheduler(once=True)

        self.assertEqual(EXIT_OK, result.exit_code)
        payload = result.payload
        self.assertEqual("scheduler-run", payload["command"])
        self.assertEqual("once", payload["mode"])
        self.assertEqual(1, payload["sweeps"])
        self.assertFalse(payload["claims_suspended"])
        self.assertEqual({ACTION_DISPATCHED: 1}, payload["counts"])
        self.assertEqual(DISPATCH_SETTLED, payload["actions"][0]["verdict"])
        self.assertIs(False, payload["retry_authorized"])

    def test_scheduler_run_once_through_the_cli(self) -> None:
        self.memory.create_job(self.spec(steps=1), now=NOW)
        app = self.app()
        code = main(
            ["scheduler", "run", "--once"],
            settings=self.settings,
            app_factory=lambda _settings: app,
        )
        self.assertEqual(EXIT_OK, code)
        self.assertEqual(1, len(self.reader.calls))

    def test_scheduler_without_run_is_refused(self) -> None:
        code = main(
            ["scheduler"], settings=self.settings, app_factory=lambda _s: self.app()
        )
        self.assertEqual(EXIT_INVALID_INPUT, code)
        self.assertEqual(0, len(self.reader.calls))
