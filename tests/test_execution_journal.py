"""H2 - durable execution-attempt journal, atomic ownership and lifecycle.

Every test uses a fake connector and a temporary SQLite store. No test starts
a live connector, auth probe, model subprocess, MCP or network call. Crash and
restart are simulated with a fresh AgentCore over the same database file.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis.contracts import (
    AttemptState,
    BillingClass,
    ErrorCode,
    ExecutionAttempt,
    LoopStage,
    MonetaryState,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    RunStatus,
    SideEffect,
    StructuredError,
    Task,
    TaskStatus,
    ToolResult,
    UsageRecord,
)
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task, safe_cleanup


class _SimulatedCrash(BaseException):
    """Propagates past ``except Exception`` to mimic process death."""


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


class JournalConnector(DummyConnector):
    """Mandatory-egress fake that records what the durable journal shows at the
    connector boundary, and can block or crash on demand."""

    def __init__(
        self,
        *,
        store: SQLiteMemoryStore | None = None,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        verify_started: threading.Event | None = None,
        verify_release: threading.Event | None = None,
        crash: bool = False,
        fail: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(name="egress-journal", side_effect=SideEffect.EXTERNAL_EGRESS, **kwargs)
        self._store = store
        self._started = started
        self._release = release
        self._verify_started = verify_started
        self._verify_release = verify_release
        self._crash = crash
        self._fail = fail
        self.boundary_states: list[AttemptState | None] = []

    def verify(self, task, result, session):
        # Optional pause point: the owner is inside VERIFY, after execution
        # already reached FINISHED and before the verification checkpoint.
        if self._verify_started is not None:
            self._verify_started.set()
        if self._verify_release is not None:
            self._verify_release.wait(timeout=60)
        return super().verify(task, result, session)

    def execute(self, task: Task, session) -> ToolResult:
        if self._store is not None:
            attempt = self._store.latest_execution_attempt(task.id)
            self.boundary_states.append(attempt.state if attempt else None)
        self.execute_calls += 1
        if self._started is not None:
            self._started.set()
        if self._crash:
            raise _SimulatedCrash()
        if self._release is not None:
            self._release.wait(timeout=60)
        usage = UsageRecord(
            connector=self.manifest.name,
            billing_class=self._billing_class,
            monetary_state=MonetaryState.NOT_APPLICABLE,
            call_count=1,
        )
        if self._fail:
            return ToolResult(
                outcome=OutcomeStatus.ERROR,
                summary="journal connector failure",
                error=StructuredError(ErrorCode.CONNECTOR_FAILED, "journal failure"),
                usage=usage,
            )
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=task.objective,
            data={"next_step": "continue"},
            usage=usage,
        )

    def cancel(self, task_id: str) -> bool:
        # A fake blocking execution cannot be force-stopped; the executing
        # owner honours cancellation on return instead (H2-F).
        self.cancel_calls += 1
        return False


class ExecutionJournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"

    def store(self) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(self.database)

    def core(self, connector, memory=None) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        return AgentCore(
            registry,
            memory or self.store(),
            Settings(memory_database=self.database, permission_mode=PermissionMode.SESSION),
        )

    def egress_task(self, memory, connector, session_id: str) -> Task:
        """A task persisted at EXECUTE with a recorded one-shot egress grant."""
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        task.preferences["billing_preflight_fingerprint"] = task.estimate.gate_state
        memory.save_task(session_id, task)
        request = connector.permission_request(task, session_id)
        memory.save_pending_approval(request)
        memory.resolve_approval(request, PermissionGrant.for_request(request), "approved")
        return task

    def _grants(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT approved, consumed_by_attempt FROM permission_grants"
                )
            ]
        finally:
            connection.close()

    def _consumed_by(self) -> list[str]:
        return [
            grant["consumed_by_attempt"]
            for grant in self._grants()
            if grant["consumed_by_attempt"] is not None
        ]

    def _approved_grant_count(self) -> int:
        return sum(1 for grant in self._grants() if grant["approved"] == 1)

    def _pending_rows(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id, status FROM pending_approvals"
                )
            ]
        finally:
            connection.close()

    def _events(self) -> list[str]:
        connection = sqlite3.connect(self.database)
        try:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT event_type FROM event_log ORDER BY id"
                )
            ]
        finally:
            connection.close()

    def _checkpoint_stages(self) -> list[str]:
        connection = sqlite3.connect(self.database)
        try:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT stage FROM loop_checkpoints ORDER BY id"
                )
            ]
        finally:
            connection.close()

    def _decision_count(self) -> int:
        connection = sqlite3.connect(self.database)
        try:
            return int(
                connection.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
            )
        finally:
            connection.close()


class AttemptLifecycleTests(ExecutionJournalTestCase):
    def test_attempt_ids_are_unique_and_persisted(self) -> None:
        memory = self.store()
        session = self.core(JournalConnector()).start_session("ids")
        # Two distinct tasks each open one attempt with a unique id.
        ids = set()
        for _ in range(2):
            task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
            memory.save_task(session.id, task)
            attempt = memory.open_execution_attempt(
                ExecutionAttempt(session.id, task.id, "c", "fp")
            )
            ids.add(attempt.id)
            self.assertEqual(
                AttemptState.PREPARED, memory.latest_execution_attempt(task.id).state
            )
        self.assertEqual(2, len(ids))

    def test_prepared_then_in_flight_then_finished_one_execution(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("lifecycle")
        task = self.egress_task(memory, connector, session.id)

        run = core.resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)
        # The durable attempt was IN_FLIGHT when the connector boundary ran.
        self.assertEqual([AttemptState.IN_FLIGHT], connector.boundary_states)
        # Exactly one attempt, now FINISHED.
        attempts = memory.execution_attempts(task.id)
        self.assertEqual(1, len(attempts))
        self.assertEqual(AttemptState.FINISHED, attempts[0].state)

    def test_prepared_exists_before_the_connector_boundary(self) -> None:
        memory = self.store()
        session = self.core(JournalConnector()).start_session("prepared")
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        memory.save_task(session.id, task)
        # open persists PREPARED before begin/execute ever run.
        memory.open_execution_attempt(ExecutionAttempt(session.id, task.id, "c", "fp"))
        self.assertEqual(
            AttemptState.PREPARED, memory.latest_execution_attempt(task.id).state
        )


class RestartTests(ExecutionJournalTestCase):
    def _staged(self, connector, memory):
        session = self.core(connector, memory).start_session("restart")
        task = self.egress_task(memory, connector, session.id)
        return session, task

    def test_restart_from_finished_executes_zero_times(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        session, task = self._staged(connector, memory)
        # First run completes (one execution) and leaves a FINISHED attempt.
        self.assertEqual(RunStatus.COMPLETED, self.core(connector, memory).resume(session.id, task.id).status)
        self.assertEqual(1, connector.execute_calls)

        # A fresh core over the same database must not execute again.
        fresh = JournalConnector(store=memory)
        run = self.core(fresh, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(0, fresh.execute_calls)

    def test_restart_from_in_flight_executes_zero_times_and_reports_unknown(self) -> None:
        memory = self.store()
        session = self.core(JournalConnector(), memory).start_session("inflight")
        connector = JournalConnector()
        task = self.egress_task(memory, connector, session.id)
        # A durable IN_FLIGHT attempt with no live owner (as after a crash).
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        self.assertTrue(memory.begin_execution(attempt.id, session.id, task.id, []))

        fresh = JournalConnector()
        run = self.core(fresh, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, run.error.code)
        self.assertEqual(0, fresh.execute_calls)

    def test_restart_from_unknown_outcome_executes_zero_times(self) -> None:
        memory = self.store()
        session = self.core(JournalConnector(), memory).start_session("unknown")
        connector = JournalConnector()
        task = self.egress_task(memory, connector, session.id)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        memory.begin_execution(attempt.id, session.id, task.id, [])
        memory.mark_execution_unknown(attempt.id)
        self.assertEqual(
            AttemptState.UNKNOWN_OUTCOME, memory.latest_execution_attempt(task.id).state
        )

        fresh = JournalConnector()
        run = self.core(fresh, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, run.error.code)
        self.assertEqual(0, fresh.execute_calls)

    def test_restart_from_prepared_may_safely_proceed(self) -> None:
        # A PREPARED attempt means the connector boundary was never crossed, so
        # a fresh core may reclaim it and execute exactly once.
        memory = self.store()
        connector = JournalConnector(store=memory)
        session = self.core(connector, memory).start_session("prepared-restart")
        task = self.egress_task(memory, connector, session.id)
        memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        self.assertEqual(
            AttemptState.PREPARED, memory.latest_execution_attempt(task.id).state
        )

        run = self.core(connector, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)


class CrashWindowTests(ExecutionJournalTestCase):
    """A crash in any window must not cause an automatic second execution."""

    def _run_and_crash(self, connector, memory, patch_target=None):
        core = self.core(connector, memory)
        session = core.start_session("crash")
        task = self.egress_task(memory, connector, session.id)
        try:
            if patch_target is not None:
                name, replacement = patch_target
                original = getattr(memory, name)
                setattr(memory, name, replacement(original))
                try:
                    core.resume(session.id, task.id)
                finally:
                    setattr(memory, name, original)
            else:
                core.resume(session.id, task.id)
        except (_SimulatedCrash, _CrashInStore):
            pass
        return session, task

    def _resume_fresh(self, session, task):
        fresh = JournalConnector()
        run = self.core(fresh, self.store()).resume(session.id, task.id)
        return fresh, run

    def test_crash_after_entering_connector_boundary(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory, crash=True)
        session, task = self._run_and_crash(connector, memory)
        self.assertEqual(1, connector.execute_calls)
        self.assertEqual(
            AttemptState.IN_FLIGHT, memory.latest_execution_attempt(task.id).state
        )
        fresh, run = self._resume_fresh(session, task)
        self.assertEqual(0, fresh.execute_calls)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, run.error.code)

    def test_crash_before_connector_boundary(self) -> None:
        # begin_execution commits IN_FLIGHT, then the process dies before the
        # connector is called.
        memory = self.store()
        connector = JournalConnector(store=memory)

        def crashing_begin(original):
            def wrapper(*args, **kwargs):
                original(*args, **kwargs)  # IN_FLIGHT commits, then "crash"
                raise _CrashInStore()

            return wrapper

        session, task = self._run_and_crash(
            connector, memory, ("begin_execution", crashing_begin)
        )
        self.assertEqual(0, connector.execute_calls)
        self.assertEqual(
            AttemptState.IN_FLIGHT, memory.latest_execution_attempt(task.id).state
        )
        fresh, run = self._resume_fresh(session, task)
        self.assertEqual(0, fresh.execute_calls)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, run.error.code)

    def test_crash_after_return_before_usage_persistence(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)

        def crashing_append(original):
            def wrapper(*args, **kwargs):
                raise _CrashInStore()

            return wrapper

        session, task = self._run_and_crash(
            connector, memory, ("append_usage", crashing_append)
        )
        self.assertEqual(1, connector.execute_calls)
        self.assertEqual(
            AttemptState.IN_FLIGHT, memory.latest_execution_attempt(task.id).state
        )
        fresh, run = self._resume_fresh(session, task)
        self.assertEqual(0, fresh.execute_calls)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, run.error.code)

    def test_crash_after_usage_before_completed_checkpoint(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)

        def crashing_conclude(original):
            def wrapper(*args, **kwargs):
                raise _CrashInStore()

            return wrapper

        session, task = self._run_and_crash(
            connector, memory, ("conclude_execution", crashing_conclude)
        )
        self.assertEqual(1, connector.execute_calls)
        # Usage was recorded first (H2-H) and is not lost; the attempt stays
        # IN_FLIGHT and is non-replayable.
        self.assertEqual(1, len(memory.usage_records()))
        self.assertEqual(
            AttemptState.IN_FLIGHT, memory.latest_execution_attempt(task.id).state
        )
        fresh, run = self._resume_fresh(session, task)
        self.assertEqual(0, fresh.execute_calls)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, run.error.code)

    def test_crash_before_verification_recovers_without_reexecuting(self) -> None:
        # A successful execution that crashed before VERIFY leaves a FINISHED
        # attempt; restart advances from the persisted result, never re-runs.
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("pre-verify")
        task = self.egress_task(memory, connector, session.id)

        original_verify = connector.verify

        def crashing_verify(*args, **kwargs):
            raise _CrashInStore()

        connector.verify = crashing_verify
        try:
            core.resume(session.id, task.id)
        except _CrashInStore:
            pass
        finally:
            connector.verify = original_verify
        self.assertEqual(1, connector.execute_calls)
        self.assertEqual(
            AttemptState.FINISHED, memory.latest_execution_attempt(task.id).state
        )

        fresh = JournalConnector(store=memory)
        run = self.core(fresh, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(0, fresh.execute_calls)


class _CrashInStore(Exception):
    """A store-side failure used to simulate a crash mid-transaction."""


class ConcurrentResumeTests(ExecutionJournalTestCase):
    def test_two_concurrent_resumes_execute_exactly_once(self) -> None:
        started = threading.Event()
        release = threading.Event()
        # One shared connector instance so execute_calls counts total; two
        # separate stores + cores over the same database file arbitrate.
        connector = JournalConnector(started=started, release=release)
        setup_memory = self.store()
        session = self.core(connector, setup_memory).start_session("race")
        task = self.egress_task(setup_memory, connector, session.id)

        results: dict[str, object] = {}

        def worker(name: str) -> None:
            core = self.core(connector, self.store())
            results[name] = core.resume(session.id, task.id)

        winner = threading.Thread(target=worker, args=("a",))
        loser = threading.Thread(target=worker, args=("b",))
        winner.start()
        # Let the winner reach and block inside the connector boundary.
        self.assertTrue(started.wait(timeout=60))
        loser.start()
        loser.join(timeout=60)
        release.set()
        winner.join(timeout=60)

        self.assertEqual(1, connector.execute_calls)
        statuses = {name: run.status for name, run in results.items()}
        self.assertEqual(1, sum(1 for s in statuses.values() if s is RunStatus.COMPLETED))
        blocked = [run for run in results.values() if run.status is RunStatus.FAILED]
        self.assertEqual(1, len(blocked))
        self.assertIn(
            blocked[0].error.code,
            (ErrorCode.EXECUTION_IN_PROGRESS, ErrorCode.EXECUTION_UNKNOWN_OUTCOME),
        )
        # The one-shot grant was consumed by exactly one attempt.
        self.assertEqual(1, self._consumed_grant_count())

    def _consumed_grant_count(self) -> int:
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM permission_grants "
                "WHERE consumed_by_attempt IS NOT NULL"
            ).fetchone()
        finally:
            connection.close()
        return int(row[0])


class GrantConsumptionTests(ExecutionJournalTestCase):
    def test_one_shot_grant_is_consumed_exactly_once(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        session = self.core(connector, memory).start_session("one-shot")
        task = self.egress_task(memory, connector, session.id)
        request = connector.permission_request(task, session.id)

        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, request.fingerprint)
        )
        self.assertTrue(
            memory.begin_execution(attempt.id, session.id, task.id, [request.fingerprint])
        )
        # The same attempt is no longer PREPARED, so a repeat claim fails and
        # the grant is not consumed a second time.
        self.assertFalse(
            memory.begin_execution(attempt.id, session.id, task.id, [request.fingerprint])
        )
        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT consumed_by_attempt FROM permission_grants "
                "WHERE consumed_by_attempt IS NOT NULL"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([attempt.id], [row[0] for row in rows])

    def test_expired_grant_is_not_consumable(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        session = self.core(connector, memory).start_session("expired")
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        task.preferences["billing_preflight_fingerprint"] = task.estimate.gate_state
        memory.save_task(session.id, task)
        request = connector.permission_request(task, session.id)
        memory.save_pending_approval(request)
        expired = replace(PermissionGrant.for_request(request), expires_at=_past())
        memory.resolve_approval(request, expired, "approved")

        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, request.fingerprint)
        )
        self.assertFalse(
            memory.begin_execution(attempt.id, session.id, task.id, [request.fingerprint])
        )
        # And the loop refuses to execute on an expired grant.
        run = self.core(connector, self.store()).resume(session.id, task.id)
        self.assertEqual(0, connector.execute_calls)
        self.assertNotEqual(RunStatus.COMPLETED, run.status)

    def test_wrong_fingerprint_grant_is_not_consumable(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        session = self.core(connector, memory).start_session("wrong-fp")
        task = self.egress_task(memory, connector, session.id)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        self.assertFalse(
            memory.begin_execution(
                attempt.id, session.id, task.id, ["wrong-fingerprint"]
            )
        )

    def test_session_reusable_grant_is_not_consumed(self) -> None:
        # A remembered (session-reusable) grant is not a one-shot: the loop
        # claims with no one-shot fingerprint, so the grant stays unconsumed
        # and can authorize a later task.
        memory = self.store()
        connector = DummyConnector(name="reusable")
        session = self.core(connector, memory).start_session("reusable")
        task = make_task()
        memory.save_task(session.id, task)
        request = connector.permission_request(task, session.id)
        memory.save_pending_approval(request)
        memory.resolve_approval(
            request,
            PermissionGrant.for_request(request, remember_for_session=True),
            "approved",
        )
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, request.fingerprint)
        )
        # The loop passes one_shot=None for reusable grants; nothing is consumed.
        self.assertTrue(memory.begin_execution(attempt.id, session.id, task.id, []))
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM permission_grants WHERE consumed_by_attempt IS NOT NULL"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(0, int(row[0]))


class TerminalLifecycleTests(ExecutionJournalTestCase):
    def _completed(self):
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("terminal")
        task = self.egress_task(memory, connector, session.id)
        run = core.resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        return memory, session, task

    def test_resume_cannot_reopen_a_completed_task(self) -> None:
        memory, session, task = self._completed()
        fresh = JournalConnector(store=memory)
        run = self.core(fresh, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(0, fresh.execute_calls)

    def test_terminal_task_reportable_with_connector_absent(self) -> None:
        memory, session, task = self._completed()
        # A core whose registry does not contain the task's connector.
        core = AgentCore(
            ToolRegistry(),
            self.store(),
            Settings(memory_database=self.database, permission_mode=PermissionMode.SESSION),
        )
        run = core.resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertIsNone(run.error)

    def test_clarify_cannot_mutate_a_terminal_task(self) -> None:
        memory, session, task = self._completed()
        before = memory.load_task(task.id).to_dict()
        run = self.core(JournalConnector(), self.store()).provide_clarification(
            session.id, task.id, "late answer"
        )
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertEqual(before, memory.load_task(task.id).to_dict())

    def test_cancel_cannot_rewrite_a_terminal_task(self) -> None:
        memory, session, task = self._completed()
        run = self.core(JournalConnector(), self.store()).cancel(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertIs(TaskStatus.COMPLETED, memory.load_task(task.id).status)

    def test_rejected_task_stays_terminal(self) -> None:
        memory = self.store()
        connector = DummyConnector(name="egress", side_effect=SideEffect.EXTERNAL_EGRESS)
        core = self.core(connector, memory)
        waiting = core.run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        rejected = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request, approved=False),
        )
        self.assertEqual(RunStatus.REJECTED, rejected.status)
        # A resume cannot reopen it.
        again = core.resume(waiting.session.id, waiting.task.id)
        self.assertEqual(RunStatus.REJECTED, again.status)
        self.assertEqual(0, connector.execute_calls)


class CancellationRaceTests(ExecutionJournalTestCase):
    def test_stale_success_does_not_overwrite_authoritative_cancellation(self) -> None:
        started = threading.Event()
        release = threading.Event()
        connector = JournalConnector(started=started, release=release)
        memory = self.store()
        core = self.core(connector, memory)
        session = core.start_session("cancel-race")
        task = self.egress_task(memory, connector, session.id)

        outcome: dict[str, object] = {}

        def execute_worker() -> None:
            outcome["run"] = self.core(connector, self.store()).resume(session.id, task.id)

        runner = threading.Thread(target=execute_worker)
        runner.start()
        try:
            self.assertTrue(started.wait(timeout=60))
            # Execution is IN_FLIGHT; another owner requests cancellation.
            cancel_core = self.core(connector, self.store())
            cancel_run = cancel_core.cancel(session.id, task.id)
            # Not a final CANCELLED while the outcome is unresolved.
            self.assertEqual(RunStatus.CANCELLING, cancel_run.status)
        finally:
            release.set()
            runner.join(timeout=60)

        # The connector returned success, but authoritative cancellation wins:
        # the stale success never becomes COMPLETED.
        final = memory.load_task(task.id)
        self.assertIs(TaskStatus.CANCELLED, final.status)
        self.assertNotEqual(RunStatus.COMPLETED, outcome["run"].status)
        self.assertEqual(1, connector.execute_calls)

    def test_cancel_before_execution_is_final_and_blocks_execution(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("cancel-before")
        task = self.egress_task(memory, connector, session.id)
        # No attempt yet: cancellation is safe and final.
        cancelled = core.cancel(session.id, task.id)
        self.assertEqual(RunStatus.CANCELLED, cancelled.status)
        self.assertIs(TaskStatus.CANCELLED, memory.load_task(task.id).status)
        # A resume cannot execute the cancelled task.
        run = self.core(connector, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.CANCELLED, run.status)
        self.assertEqual(0, connector.execute_calls)


class ApproveRejectRaceTests(ExecutionJournalTestCase):
    """Approve and reject resolve a pending request with exactly one winner."""

    def _waiting(self, connector, memory):
        core = self.core(connector, memory)
        waiting = core.run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
        return core, waiting

    def test_reject_wins_then_stale_approve_loses(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core, waiting = self._waiting(connector, memory)
        request = waiting.permission_request

        rejected = self.core(JournalConnector(), self.store()).resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(request, approved=False),
        )
        self.assertEqual(RunStatus.REJECTED, rejected.status)

        approve = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(request, approved=True),
        )
        # The stale approve cannot execute or reopen the rejected task.
        self.assertEqual(RunStatus.REJECTED, approve.status)
        self.assertEqual(0, connector.execute_calls)
        self.assertIs(TaskStatus.REJECTED, memory.load_task(waiting.task.id).status)
        # No usable (approved) grant exists.
        self.assertEqual(0, self._approved_grant_count())

    def test_approve_wins_then_stale_reject_loses(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core, waiting = self._waiting(connector, memory)
        request = waiting.permission_request

        approved = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(request, approved=True),
        )
        self.assertEqual(RunStatus.COMPLETED, approved.status)
        self.assertEqual(1, connector.execute_calls)

        reject = self.core(JournalConnector(), self.store()).resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(request, approved=False),
        )
        # The stale reject cannot rewrite the completed task.
        self.assertEqual(RunStatus.COMPLETED, reject.status)
        self.assertIs(TaskStatus.COMPLETED, memory.load_task(waiting.task.id).status)
        self.assertEqual(1, connector.execute_calls)

    def test_concurrent_approve_and_reject_have_one_winner(self) -> None:
        memory = self.store()
        connector = JournalConnector()  # shared: counts total executions
        core, waiting = self._waiting(connector, memory)
        request = waiting.permission_request
        results: dict[str, object] = {}

        def approve() -> None:
            results["approve"] = self.core(connector, self.store()).resume(
                waiting.session.id,
                waiting.task.id,
                PermissionGrant.for_request(request, approved=True),
            )

        def reject() -> None:
            results["reject"] = self.core(connector, self.store()).resume(
                waiting.session.id,
                waiting.task.id,
                PermissionGrant.for_request(request, approved=False),
            )

        threads = [threading.Thread(target=approve), threading.Thread(target=reject)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        final = memory.load_task(waiting.task.id).status
        # Hard safety invariants (these fail if approve and reject both "win",
        # the original defect): at most one resolution wrote a grant, and the
        # connector executed at most once. A rare transient lock-contention
        # failure under heavy parallel load is tolerated as long as these hold.
        self.assertLessEqual(len(self._grants()), 1)
        self.assertLessEqual(connector.execute_calls, 1)
        # The definitive outcomes are internally consistent.
        if final is TaskStatus.COMPLETED:
            self.assertEqual(1, self._approved_grant_count())
            self.assertEqual(1, connector.execute_calls)
        if final is TaskStatus.REJECTED:
            self.assertEqual(0, self._approved_grant_count())
            self.assertEqual(0, connector.execute_calls)


class RejectAtomicityTests(ExecutionJournalTestCase):
    """P1-A: reject resolution and terminal rejection are one transaction."""

    def _waiting(self, connector, memory):
        core = self.core(connector, memory)
        waiting = core.run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
        return core, waiting

    def test_reject_leaves_no_resolved_resumable_state(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core, waiting = self._waiting(connector, memory)
        rejected = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request, approved=False),
        )
        self.assertEqual(RunStatus.REJECTED, rejected.status)
        # The request is resolved AND the task is terminal - atomically, so
        # there is no observable rejected-request + resumable-task state.
        self.assertIs(TaskStatus.REJECTED, memory.load_task(waiting.task.id).status)
        pending = self._pending_rows()
        self.assertEqual(1, len(pending))
        self.assertEqual("rejected", pending[0]["status"])

    def test_reject_wins_race_issues_no_fresh_request(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core, waiting = self._waiting(connector, memory)
        request = waiting.permission_request
        results: dict[str, object] = {}

        def reject() -> None:
            results["reject"] = self.core(connector, self.store()).resume(
                waiting.session.id,
                waiting.task.id,
                PermissionGrant.for_request(request, approved=False),
            )

        def plain_resume() -> None:
            # A plain resume must never reissue a fresh approval request.
            results["resume"] = self.core(JournalConnector(), self.store()).resume(
                waiting.session.id, waiting.task.id
            )

        threads = [threading.Thread(target=reject), threading.Thread(target=plain_resume)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        # Order-independent invariants. Either the reject won (task terminal
        # REJECTED and the resume could not reissue) or the resume reissued
        # first and the reject lost cleanly - but never both, and never a
        # rejected request sitting on a still-resumable task.
        final = memory.load_task(waiting.task.id).status
        self.assertEqual(0, connector.execute_calls)
        self.assertEqual(0, self._approved_grant_count())
        rejected_rows = [row for row in self._pending_rows() if row["status"] == "rejected"]
        pending_rows = [row for row in self._pending_rows() if row["status"] == "pending"]
        if final is TaskStatus.REJECTED:
            # Reject won: its request is resolved and nothing is left pending,
            # so no fresh approval opportunity survived the rejection.
            self.assertEqual(1, len(rejected_rows))
            self.assertEqual([], pending_rows)
        else:
            # Reject lost: it resolved nothing at all.
            self.assertEqual([], rejected_rows)
            self.assertIs(TaskStatus.AWAITING_APPROVAL, final)

    def test_crash_after_reject_restart_stays_terminal(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core, waiting = self._waiting(connector, memory)
        core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request, approved=False),
        )
        # A fresh core (as after a restart) observes terminal rejection: no
        # fresh approval, no execution.
        fresh = JournalConnector(store=memory)
        run = self.core(fresh, self.store()).resume(
            waiting.session.id, waiting.task.id
        )
        self.assertEqual(RunStatus.REJECTED, run.status)
        self.assertEqual(0, fresh.execute_calls)
        self.assertEqual(1, len(self._pending_rows()))

    def test_terminal_task_cannot_gain_a_fresh_pending_request(self) -> None:
        # Terminal immutability extends to approval issuance: a stale writer
        # cannot open a new approval opportunity on a rejected task, so no
        # orphan pending request survives the rejection.
        memory = self.store()
        connector = JournalConnector(store=memory)
        core, waiting = self._waiting(connector, memory)
        stale_request = waiting.permission_request
        core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(stale_request, approved=False),
        )
        self.assertIs(TaskStatus.REJECTED, memory.load_task(waiting.task.id).status)

        # A stale writer tries to persist a fresh pending approval anyway.
        memory.save_pending_approval(replace(stale_request, id="fresh-request-id"))
        self.assertEqual(
            [], [row for row in self._pending_rows() if row["status"] == "pending"]
        )
        self.assertIsNone(
            memory.load_pending_approval(waiting.task.id, waiting.session.id)
        )

    def test_stale_writer_cannot_demote_a_terminal_task(self) -> None:
        # Storage-level terminal immutability: a lifecycle write carrying an
        # older non-terminal task object cannot reopen a terminal row. This is
        # what stops a concurrent resume from resurrecting a rejected task.
        memory = self.store()
        connector = JournalConnector(store=memory)
        core, waiting = self._waiting(connector, memory)
        stale = memory.load_task(waiting.task.id)  # AWAITING_APPROVAL snapshot
        self.assertIs(TaskStatus.AWAITING_APPROVAL, stale.status)

        core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request, approved=False),
        )
        self.assertIs(TaskStatus.REJECTED, memory.load_task(waiting.task.id).status)

        # The stale writer replays its old snapshot through the loop's write
        # path; the terminal row must survive untouched.
        session = memory.load_session(waiting.session.id)
        memory.persist_checkpoint(session, stale, "stale_replay", {})
        self.assertIs(TaskStatus.REJECTED, memory.load_task(waiting.task.id).status)

    def test_rejected_task_is_immutable_to_every_command(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core, waiting = self._waiting(connector, memory)
        core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request, approved=False),
        )
        sid, tid = waiting.session.id, waiting.task.id
        self.assertEqual(RunStatus.REJECTED, core.resume(sid, tid).status)
        self.assertEqual(
            ErrorCode.INVALID_TASK,
            core.provide_clarification(sid, tid, "x").error.code,
        )
        self.assertEqual(RunStatus.REJECTED, core.cancel(sid, tid).status)
        self.assertIs(TaskStatus.REJECTED, memory.load_task(tid).status)
        self.assertEqual(0, connector.execute_calls)


class StaleResumeAfterRejectionTests(ExecutionJournalTestCase):
    """A stale resume must never report a phantom approval opportunity after a
    rejection already won lifecycle authority."""

    def test_stale_resume_reports_authoritative_rejection(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        rejecter = self.core(connector, memory)
        waiting = rejecter.run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
        request = waiting.permission_request

        # The concurrent resume captures the stale waiting state BEFORE the
        # rejection commits (the synchronization point of the race).
        stale_task = memory.load_task(waiting.task.id)
        self.assertIs(TaskStatus.AWAITING_APPROVAL, stale_task.status)

        # The rejection commits and takes authoritative lifecycle ownership.
        rejected = rejecter.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(request, approved=False),
        )
        self.assertEqual(RunStatus.REJECTED, rejected.status)

        events_before = self._events()
        checkpoints_before = self._checkpoint_stages()
        self.assertEqual("approval_rejected", events_before[-1])

        # The second core now continues from the state it captured earlier;
        # every later read observes the authoritative truth.
        stale_memory = self.store()
        stale_connector = JournalConnector(store=stale_memory)
        stale_core = self.core(stale_connector, stale_memory)
        real_load = stale_memory.load_task
        reads = {"count": 0}

        def staged_load(task_id: str):
            reads["count"] += 1
            return stale_task if reads["count"] == 1 else real_load(task_id)

        with mock.patch.object(stale_memory, "load_task", side_effect=staged_load):
            stale_run = stale_core.resume(waiting.session.id, waiting.task.id)

        # The stale writer reports the authoritative rejection, never its own
        # awaiting-approval view, and offers no approval opportunity.
        self.assertEqual(RunStatus.REJECTED, stale_run.status)
        self.assertIsNone(stale_run.permission_request)
        self.assertIs(TaskStatus.REJECTED, stale_run.task.status)
        self.assertGreaterEqual(reads["count"], 2, "authoritative state was reloaded")

        # Persistence is untouched by the stale writer.
        self.assertIs(TaskStatus.REJECTED, memory.load_task(waiting.task.id).status)
        self.assertIsNone(
            memory.load_pending_approval(waiting.task.id, waiting.session.id)
        )
        rows = self._pending_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("rejected", rows[0]["status"])
        self.assertEqual(request.id, rows[0]["id"])

        # No stale audit event and no stale awaiting-approval checkpoint.
        self.assertEqual(events_before, self._events())
        self.assertEqual(checkpoints_before, self._checkpoint_stages())
        self.assertEqual(0, connector.execute_calls)
        self.assertEqual(0, stale_connector.execute_calls)

    def test_normal_approval_flow_still_awaits_with_a_real_request(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        waiting = self.core(connector, memory).run(
            make_task(effect=SideEffect.EXTERNAL_EGRESS)
        )
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
        self.assertIsNotNone(waiting.permission_request)
        persisted = memory.load_pending_approval(waiting.task.id, waiting.session.id)
        self.assertIsNotNone(persisted)
        self.assertEqual(waiting.permission_request.id, persisted.id)
        self.assertIn("approval_requested", self._events())
        self.assertEqual(0, connector.execute_calls)


class BeginConcludeArbitrationTests(ExecutionJournalTestCase):
    """Window A/B: the claim and conclusion arbitrate cancellation atomically."""

    def test_begin_execution_rejects_a_cancelled_task(self) -> None:
        # Window A: cancellation persisted before the claim -> zero execution.
        memory = self.store()
        connector = JournalConnector(store=memory)
        session = self.core(connector, memory).start_session("begin-cancel")
        task = self.egress_task(memory, connector, session.id)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        stored = memory.load_task(task.id)
        stored.status = TaskStatus.CANCELLED
        stored.stage = LoopStage.CANCELLED
        memory.save_task(session.id, stored)

        self.assertFalse(memory.begin_execution(attempt.id, session.id, task.id, []))
        self.assertEqual(
            AttemptState.PREPARED, memory.latest_execution_attempt(task.id).state
        )

    def test_begin_execution_rejects_a_cancel_requested_task(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        session = self.core(connector, memory).start_session("begin-cancel-req")
        task = self.egress_task(memory, connector, session.id)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        stored = memory.load_task(task.id)
        stored.status = TaskStatus.CANCEL_REQUESTED
        memory.save_task(session.id, stored)
        self.assertFalse(memory.begin_execution(attempt.id, session.id, task.id, []))

    def test_conclude_refuses_to_overwrite_authoritative_cancellation(self) -> None:
        # Window B: cancellation persisted in the conclude window.
        memory = self.store()
        connector = JournalConnector(store=memory)
        session = self.core(connector, memory).start_session("conclude-cancel")
        task = self.egress_task(memory, connector, session.id)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        self.assertTrue(memory.begin_execution(attempt.id, session.id, task.id, []))
        stored = memory.load_task(task.id)
        stored.status = TaskStatus.CANCEL_REQUESTED
        memory.save_task(session.id, stored)

        task.status = TaskStatus.VERIFYING
        task.stage = LoopStage.VERIFY
        outcome = memory.conclude_execution(
            session, task, attempt.id, "execution_completed", {}
        )
        self.assertEqual("cancelled", outcome)
        # The attempt was not concluded; the stale success did not persist.
        self.assertEqual(
            AttemptState.IN_FLIGHT, memory.latest_execution_attempt(task.id).state
        )

    def _stale_outcome_test(self, *, fail: bool) -> None:
        started = threading.Event()
        release = threading.Event()
        connector = JournalConnector(started=started, release=release, fail=fail)
        memory = self.store()
        core = self.core(connector, memory)
        session = core.start_session("stale-outcome")
        task = self.egress_task(memory, connector, session.id)
        outcome: dict[str, object] = {}

        def worker() -> None:
            outcome["run"] = self.core(connector, self.store()).resume(
                session.id, task.id
            )

        runner = threading.Thread(target=worker)
        runner.start()
        try:
            self.assertTrue(started.wait(timeout=60))
            cancel_run = self.core(connector, self.store()).cancel(session.id, task.id)
            self.assertEqual(RunStatus.CANCELLING, cancel_run.status)
        finally:
            release.set()
            runner.join(timeout=60)

        self.assertIs(TaskStatus.CANCELLED, memory.load_task(task.id).status)
        self.assertNotEqual(RunStatus.COMPLETED, outcome["run"].status)
        self.assertEqual(1, connector.execute_calls)

    def test_stale_success_does_not_overwrite_cancellation(self) -> None:
        self._stale_outcome_test(fail=False)

    def test_stale_failure_does_not_overwrite_cancellation(self) -> None:
        self._stale_outcome_test(fail=True)

    def test_cancel_wins_before_claim_blocks_execution(self) -> None:
        # P1-B window A: cancel finalizes CANCELLED atomically; a later
        # execution claim loses and the connector is never invoked.
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("cancel-wins")
        task = self.egress_task(memory, connector, session.id)

        cancelled = core.cancel(session.id, task.id)
        self.assertEqual(RunStatus.CANCELLED, cancelled.status)
        self.assertIs(TaskStatus.CANCELLED, memory.load_task(task.id).status)
        # A stale execution claim (the resume's task was not terminal when it
        # loaded) loses at the atomic begin, and a fresh resume is blocked.
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        self.assertFalse(memory.begin_execution(attempt.id, session.id, task.id, []))
        run = self.core(connector, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.CANCELLED, run.status)
        self.assertEqual(0, connector.execute_calls)

    def test_claim_wins_before_cancel_is_not_final(self) -> None:
        # P1-B window B (store level): an IN_FLIGHT claim makes cancel report
        # a non-final CANCELLING with the intent recorded, never CANCELLED.
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("claim-wins")
        task = self.egress_task(memory, connector, session.id)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        self.assertTrue(memory.begin_execution(attempt.id, session.id, task.id, []))

        run = core.cancel(session.id, task.id)
        self.assertEqual(RunStatus.CANCELLING, run.status)
        self.assertIs(TaskStatus.CANCEL_REQUESTED, memory.load_task(task.id).status)

    def test_stale_cancel_after_completion_does_not_rewrite(self) -> None:
        # P1-B window C: a completed task is terminal; cancel observes it in
        # the atomic transaction and performs no rewrite.
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("cancel-after-complete")
        task = self.egress_task(memory, connector, session.id)
        self.assertEqual(RunStatus.COMPLETED, core.resume(session.id, task.id).status)

        run = self.core(connector, self.store()).cancel(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertIs(TaskStatus.COMPLETED, memory.load_task(task.id).status)

    def test_concurrent_cancels_are_deterministic(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("concurrent-cancel")
        task = self.egress_task(memory, connector, session.id)
        statuses: list[RunStatus] = []
        lock = threading.Lock()

        def cancel() -> None:
            run = self.core(connector, self.store()).cancel(session.id, task.id)
            with lock:
                statuses.append(run.status)

        threads = [threading.Thread(target=cancel) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        # No in-flight attempt: both cancels converge on a final CANCELLED
        # (one finalizes, the other observes the terminal state).
        self.assertIs(TaskStatus.CANCELLED, memory.load_task(task.id).status)
        self.assertTrue(all(s is RunStatus.CANCELLED for s in statuses))
        self.assertEqual(0, connector.execute_calls)

    def test_restart_after_cancel_stays_terminal(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("restart-cancel")
        task = self.egress_task(memory, connector, session.id)
        self.assertEqual(RunStatus.CANCELLED, core.cancel(session.id, task.id).status)

        fresh = JournalConnector(store=memory)
        run = self.core(fresh, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.CANCELLED, run.status)
        self.assertEqual(0, fresh.execute_calls)


class AllGrantConsumptionTests(ExecutionJournalTestCase):
    """Every one-shot gate that authorized execution is consumed atomically."""

    def _seed_spend(self, memory, amount: float) -> None:
        memory.append_usage(
            UsageRecord(
                billing_class=BillingClass.METERED_MONETARY,
                monetary_state=MonetaryState.MEASURED,
                monetary_cost_usd=amount,
            )
        )

    def test_cost_only_gate_grant_is_consumed(self) -> None:
        memory = self.store()
        self._seed_spend(memory, 19.90)
        connector = DummyConnector(name="metered", estimate_max=0.20, actual_cost=0.05)
        core = self.core(connector, memory)
        waiting = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, waiting.status)
        run = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)
        attempt = memory.latest_execution_attempt(run.task.id)
        self.assertEqual([attempt.id], self._consumed_by())

    def test_permission_and_cost_gates_both_consumed(self) -> None:
        memory = self.store()
        self._seed_spend(memory, 19.90)
        connector = DummyConnector(
            name="metered-egress",
            side_effect=SideEffect.EXTERNAL_EGRESS,
            estimate_max=0.20,
            actual_cost=0.05,
        )
        core = self.core(connector, memory)
        perm = core.run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, perm.status)
        cost = core.resume(
            perm.session.id,
            perm.task.id,
            PermissionGrant.for_request(perm.permission_request),
        )
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, cost.status)
        done = core.resume(
            cost.session.id,
            cost.task.id,
            PermissionGrant.for_request(cost.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, done.status)
        self.assertEqual(1, connector.execute_calls)
        attempt = memory.latest_execution_attempt(done.task.id)
        consumed = self._consumed_by()
        self.assertEqual(2, len(consumed))
        self.assertEqual({attempt.id}, set(consumed))

    def test_paid_api_and_cost_gates_both_consumed(self) -> None:
        memory = self.store()
        self._seed_spend(memory, 19.95)
        connector = DummyConnector(
            name="paid-metered", estimate_max=0.10, is_paid=True, actual_cost=0.05
        )
        core = self.core(connector, memory)
        paid = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_APPROVAL, paid.status)
        self.assertEqual(
            "enable_paid_api_for_task", paid.permission_request.scope.action
        )
        cost = core.resume(
            paid.session.id,
            paid.task.id,
            PermissionGrant.for_request(paid.permission_request),
        )
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, cost.status)
        done = core.resume(
            cost.session.id,
            cost.task.id,
            PermissionGrant.for_request(cost.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, done.status)
        attempt = memory.latest_execution_attempt(done.task.id)
        consumed = self._consumed_by()
        self.assertEqual(2, len(consumed))
        self.assertEqual({attempt.id}, set(consumed))

    def test_partial_grant_set_consumes_nothing(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        session = self.core(connector, memory).start_session("partial")
        task = self.egress_task(memory, connector, session.id)
        request = connector.permission_request(task, session.id)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, request.fingerprint)
        )
        # One valid base grant, one missing cost grant: all-or-nothing.
        self.assertFalse(
            memory.begin_execution(
                attempt.id, session.id, task.id, [request.fingerprint, "missing-cost"]
            )
        )
        self.assertEqual(
            AttemptState.PREPARED, memory.latest_execution_attempt(task.id).state
        )
        self.assertEqual([], self._consumed_by())

    def test_consumed_grant_cannot_authorize_a_second_attempt(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("restart-consume")
        task = self.egress_task(memory, connector, session.id)
        self.assertEqual(RunStatus.COMPLETED, core.resume(session.id, task.id).status)
        self.assertEqual(1, connector.execute_calls)
        self.assertEqual(1, len(self._consumed_by()))

        fresh = JournalConnector(store=memory)
        run = self.core(fresh, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(0, fresh.execute_calls)


class PostExecutionCancellationRaceTests(ExecutionJournalTestCase):
    """A stale VERIFY/finalize owner must never persist phantom completion.

    Execution has already happened exactly once and the attempt is FINISHED; a
    concurrent cancellation then wins authoritative lifecycle. The owner must
    stop, reload, and report the authoritative cancellation instead of writing
    a decision, a terminal event or a phantom COMPLETED result.
    """

    def _staged(self, connector, memory):
        session = self.core(connector, memory).start_session("post-exec-race")
        task = self.egress_task(memory, connector, session.id)
        return session, task

    def _run_owner(self, core, session_id, task_id, reached, release):
        outcome: dict[str, object] = {}

        def owner() -> None:
            outcome["run"] = core.resume(session_id, task_id)

        thread = threading.Thread(target=owner)
        thread.start()
        try:
            self.assertTrue(reached.wait(timeout=60), "owner never reached the pause")
            cancel_run = self.core(JournalConnector(), self.store()).cancel(
                session_id, task_id
            )
        finally:
            release.set()
            thread.join(timeout=60)
        return outcome.get("run"), cancel_run

    # -- Race 1: cancellation wins during VERIFY ---------------------------

    def test_cancellation_during_verify_reports_authoritative_cancellation(self) -> None:
        verify_started, verify_release = threading.Event(), threading.Event()
        setup_memory = self.store()
        connector = JournalConnector(
            verify_started=verify_started, verify_release=verify_release
        )
        session, task = self._staged(connector, setup_memory)
        owner_core = self.core(connector, self.store())

        owner_run, cancel_run = self._run_owner(
            owner_core, session.id, task.id, verify_started, verify_release
        )

        # Exactly one execution, and the attempt had already concluded.
        self.assertEqual(1, connector.execute_calls)
        self.assertEqual(
            AttemptState.FINISHED, setup_memory.latest_execution_attempt(task.id).state
        )
        # Both the canceller and the stale owner report the authoritative state.
        self.assertEqual(RunStatus.CANCELLED, cancel_run.status)
        self.assertEqual(RunStatus.CANCELLED, owner_run.status)
        self.assertIs(TaskStatus.CANCELLED, owner_run.task.status)
        self.assertIs(TaskStatus.CANCELLED, setup_memory.load_task(task.id).status)
        # No stale finalization or post-cancellation audit state.
        events = self._events()
        self.assertEqual(0, self._decision_count())
        self.assertEqual([], setup_memory.list_decisions(session.id))
        self.assertNotIn("task_completed", events)
        self.assertNotIn("verification_passed", events)
        self.assertNotIn("result_presented", events)
        self.assertIn("cancellation_requested", events)

    # -- Race 2: cancellation wins immediately before finalization ---------

    def test_cancellation_before_finalization_loses_no_state(self) -> None:
        setup_memory = self.store()
        connector = JournalConnector()
        session, task = self._staged(connector, setup_memory)

        owner_memory = self.store()
        owner_core = self.core(connector, owner_memory)
        reached, release = threading.Event(), threading.Event()
        real_finalize = owner_memory.finalize_task
        finalize_results: list[bool] = []

        def paused_finalize(*args, **kwargs):
            # Verification and presentation already persisted legitimately;
            # pause immediately before the finalization transaction.
            reached.set()
            release.wait(timeout=60)
            outcome = real_finalize(*args, **kwargs)
            finalize_results.append(outcome)
            return outcome

        with mock.patch.object(
            owner_memory, "finalize_task", side_effect=paused_finalize
        ):
            owner_run, cancel_run = self._run_owner(
                owner_core, session.id, task.id, reached, release
            )

        # finalize_task reported that this writer lost.
        self.assertEqual([False], finalize_results)
        self.assertEqual(RunStatus.CANCELLED, cancel_run.status)
        self.assertEqual(RunStatus.CANCELLED, owner_run.status)
        self.assertIs(TaskStatus.CANCELLED, setup_memory.load_task(task.id).status)
        self.assertEqual(1, connector.execute_calls)
        # No decision row, no terminal completion event.
        self.assertEqual(0, self._decision_count())
        self.assertNotIn("task_completed", self._events())
        # Verification did legitimately persist before the cancellation.
        self.assertIn("verification_passed", self._events())
        # The cancelled session state was not overwritten by the stale owner.
        self.assertIsNone(setup_memory.load_session(session.id).active_task_id)

    # -- Race 3: completion wins first ------------------------------------

    def test_completion_wins_first_and_stale_cancel_cannot_rewrite(self) -> None:
        memory = self.store()
        connector = JournalConnector(store=memory)
        core = self.core(connector, memory)
        session = core.start_session("completion-wins")
        task = self.egress_task(memory, connector, session.id)

        completed = core.resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, completed.status)

        stale_cancel = self.core(JournalConnector(), self.store()).cancel(
            session.id, task.id
        )
        self.assertEqual(RunStatus.COMPLETED, stale_cancel.status)
        self.assertIs(TaskStatus.COMPLETED, memory.load_task(task.id).status)
        # Exactly one decision and one terminal completion event.
        self.assertEqual(1, self._decision_count())
        self.assertEqual(1, len(memory.list_decisions(session.id)))
        self.assertEqual(1, self._events().count("task_completed"))
        self.assertEqual(1, connector.execute_calls)

    # -- Race 4: the terminal failure path ---------------------------------

    def test_cancellation_beats_a_stale_terminal_failure(self) -> None:
        verify_started, verify_release = threading.Event(), threading.Event()
        setup_memory = self.store()
        # Verification fails, so the owner would normally write a terminal
        # task_failed; a cancellation that won first must not be contradicted.
        connector = JournalConnector(
            verify_started=verify_started,
            verify_release=verify_release,
            verification_passes=False,
        )
        session, task = self._staged(connector, setup_memory)
        owner_core = self.core(connector, self.store())

        owner_run, cancel_run = self._run_owner(
            owner_core, session.id, task.id, verify_started, verify_release
        )

        self.assertEqual(1, connector.execute_calls)
        self.assertEqual(RunStatus.CANCELLED, cancel_run.status)
        self.assertEqual(RunStatus.CANCELLED, owner_run.status)
        self.assertIs(TaskStatus.CANCELLED, setup_memory.load_task(task.id).status)
        events = self._events()
        self.assertNotIn("task_failed", events)
        self.assertNotIn("task_completed", events)
        self.assertEqual(0, self._decision_count())


class ExecutionJournalMigrationTests(unittest.TestCase):
    """A schema-3 database upgrades to 4 additively without data loss."""

    V3_SCHEMA = """
    CREATE TABLE schema_version (version INTEGER NOT NULL);
    CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    CREATE TABLE sessions (id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, state_json TEXT NOT NULL,
        status TEXT NOT NULL, stage TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE permission_grants (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL, task_id TEXT NOT NULL,
        session_id TEXT NOT NULL, fingerprint TEXT NOT NULL, scope_fingerprint TEXT NOT NULL,
        grant_json TEXT NOT NULL, approved INTEGER NOT NULL, remember_for_session INTEGER NOT NULL,
        expires_at TEXT NOT NULL, created_at TEXT NOT NULL
    );
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(self.V3_SCHEMA)
            connection.execute("INSERT INTO schema_version (version) VALUES (3)")
            connection.execute(
                "INSERT INTO sessions (id, state_json, updated_at) VALUES ('s1', '{}', 't')"
            )
            connection.execute(
                "INSERT INTO permission_grants (id, request_id, task_id, session_id, "
                "fingerprint, scope_fingerprint, grant_json, approved, remember_for_session, "
                "expires_at, created_at) VALUES "
                "('g1','r1','t1','s1','fp','sfp','{}',1,0,'2999-01-01T00:00:00+00:00','t')"
            )
            connection.commit()
        finally:
            connection.close()

    def test_v3_upgrades_to_v4_additively(self) -> None:
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(4, store.schema_version())
        connection = sqlite3.connect(self.database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            grant_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(permission_grants)")
            }
            grant = connection.execute(
                "SELECT id, consumed_by_attempt FROM permission_grants WHERE id='g1'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIn("execution_attempts", tables)
        self.assertIn("consumed_by_attempt", grant_columns)
        # Existing grant preserved; the new column defaults to NULL (unconsumed).
        self.assertEqual("g1", grant[0])
        self.assertIsNone(grant[1])

    def test_migration_is_idempotent(self) -> None:
        SQLiteMemoryStore(self.database)
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(4, store.schema_version())


if __name__ == "__main__":
    unittest.main()
