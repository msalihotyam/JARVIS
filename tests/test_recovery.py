"""M12 S2A - operator recovery for an attempt whose outcome is UNKNOWN.

What this file pins:

    an authoritatively UNKNOWN_OUTCOME attempt -> ONE explicit operator
    decision -> a terminally FAILED task, an attempt that stays UNKNOWN, and
    a monetary reservation that stays exposed

plus everything that must NOT happen: no connector call, no replay, no return
to a claimable state, no fabricated success, no consumed grant, no new
reservation, no released exposure, and no chain write.

It also pins the cancellation correction: an UNKNOWN_OUTCOME attempt may not
be relabelled CANCELLED, because "cancelled" would assert that nothing
happened when nobody knows that.

No live model, connector, auth or network call is ever made.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from jarvis.app import (
    EXIT_INVALID_INPUT,
    EXIT_NOT_FOUND,
    EXIT_OK,
    task_resolve_result,
)
from jarvis.cli import main as cli_main
from jarvis.contracts import (
    ActionScope,
    AttemptRecovery,
    AttemptRecoveryOutcome,
    AttemptState,
    ErrorCode,
    ExecutionAttempt,
    LoopStage,
    ProgressIssueKind,
    ReservationState,
    RunStatus,
    SessionState,
    Task,
    TaskStatus,
)
from jarvis.core import AgentCore
from jarvis.memory import SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import safe_cleanup

#: A reserved amount large enough to be unmistakable in the ledger.
RESERVED_NANO_USD = 1_500_000_000


class _RecordingConnector:
    """A connector that records calls. Recovery must never reach it."""

    def __init__(self, name: str = "local") -> None:
        self.manifest = type(
            "M", (), {"name": name, "capabilities": ("file_read",)}
        )()
        self.calls: list[str] = []

    def cancel(self, task_id: str) -> bool:
        self.calls.append(f"cancel:{task_id}")
        return False

    def execute(self, task, session):  # pragma: no cover - must never run
        raise AssertionError("recovery reached the connector boundary")


class RecoveryTestCase(unittest.TestCase):
    """A real SQLite store and real rows. No model, no network."""

    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self.root)
        self.database = Path(self.root.name) / "jarvis.db"
        self.settings = Settings(memory_database=self.database)
        self.memory = SQLiteMemoryStore(self.database)
        self.connector = _RecordingConnector()
        self.registry = ToolRegistry()
        self.core = AgentCore(
            memory=self.memory, registry=self.registry, settings=self.settings
        )
        self.session = SessionState(objective="recovery")
        self.memory.save_session(self.session)

    # -- fixtures ------------------------------------------------------

    def make_task(self, **fields: object) -> Task:
        task = Task(
            objective="do the thing",
            capability="file_read",
            scope=ActionScope(action="read", target="notes.txt"),
        )
        task.status = TaskStatus.RUNNING
        task.stage = LoopStage.EXECUTE
        for key, value in fields.items():
            setattr(task, key, value)
        self.memory.save_task(self.session.id, task)
        return task

    def open_attempt(self, task_id: str, state: AttemptState) -> str:
        """An attempt row in an EXACT state, the way a crash leaves one."""
        attempt = self.memory.open_execution_attempt(
            ExecutionAttempt(
                session_id=self.session.id,
                task_id=task_id,
                connector="local",
                fingerprint="f" * 64,
            )
        )
        if state is not AttemptState.PREPARED:
            self.set_attempt_state(attempt.id, state)
        return attempt.id

    def set_attempt_state(self, attempt_id: str, state: AttemptState) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE execution_attempts SET state = ? WHERE id = ?",
                (state.value, attempt_id),
            )
            connection.commit()

    def reserve(
        self,
        attempt_id: str,
        task_id: str,
        state: ReservationState = ReservationState.ACTIVE,
        actual: int | None = None,
    ) -> None:
        """An attempt-bound reservation, planted the way H3 creates one."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO attempt_billing (attempt_id, usage_id, session_id, "
                "task_id, ledger_month, billing_class, reserved_nano_usd, state, "
                "actual_nano_usd, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    f"usage-{attempt_id}",
                    self.session.id,
                    task_id,
                    "2026-08",
                    "metered_monetary",
                    RESERVED_NANO_USD,
                    state.value,
                    actual,
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-01T00:00:00+00:00",
                ),
            )
            connection.commit()

    def in_doubt_task(self) -> tuple[Task, str]:
        """The state S2A exists for: UNKNOWN_OUTCOME with money at stake."""
        task = self.make_task()
        attempt_id = self.open_attempt(task.id, AttemptState.UNKNOWN_OUTCOME)
        self.reserve(attempt_id, task.id)
        return task, attempt_id

    # -- probes --------------------------------------------------------

    def abandon(self, task_id: str, attempt_id: str, **over: object):
        return self.memory.abandon_unknown_attempt(
            over.get("session_id", self.session.id),
            task_id,
            attempt_id,
            over.get("recovery", AttemptRecovery.ABANDONED),
        )

    def attempt_state(self, task_id: str) -> AttemptState | None:
        latest = self.memory.latest_execution_attempt(task_id)
        return None if latest is None else latest.state

    def reservation_state(self, attempt_id: str) -> str | None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state FROM attempt_billing WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def reserved_amount(self, attempt_id: str) -> int | None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT reserved_nano_usd FROM attempt_billing WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return None if row is None else int(row[0])

    def consumed_grants(self) -> int:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM permission_grants "
                "WHERE consumed_by_attempt IS NOT NULL"
            ).fetchone()[0]

    def database_content(self) -> dict[str, list[tuple]]:
        """EVERY row of EVERY table. The strongest available no-write proof."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            tables = sorted(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            )
            return {
                table: connection.execute(
                    f"SELECT * FROM {table}"  # fixed table names only
                ).fetchall()
                for table in tables
            }

    def events(self, task_id: str) -> list[str]:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT event_type FROM event_log WHERE task_id = ? ORDER BY id",
                    (task_id,),
                ).fetchall()
            ]

    def assertUntouched(self, before: dict, label: str = "") -> None:
        self.assertEqual(self.database_content(), before, f"{label}: storage changed")


class AbandonSemanticsTests(RecoveryTestCase):
    """What abandoning an unknown outcome actually does."""

    def test_the_task_becomes_terminally_failed(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.FAILED)
        self.assertIs(stored.stage, LoopStage.FAILED)
        kinds = [issue.kind for issue in stored.progress.open_issues]
        self.assertIn(ProgressIssueKind.FAILURE, kinds)
        failure = next(
            issue
            for issue in stored.progress.open_issues
            if issue.kind is ProgressIssueKind.FAILURE
        )
        self.assertIs(failure.error_code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        self.assertFalse(failure.retryable, "recovery advertised a retry")

    def test_the_attempt_stays_unknown_outcome_forever(self) -> None:
        """The durable record that something may have happened out there."""
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        self.assertIs(self.attempt_state(task.id), AttemptState.UNKNOWN_OUTCOME)
        # Never returned to a claimable state.
        self.assertIsNot(self.attempt_state(task.id), AttemptState.PREPARED)
        self.assertIsNot(self.attempt_state(task.id), AttemptState.FINISHED)

    def test_no_success_is_ever_fabricated(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIsNot(stored.status, TaskStatus.COMPLETED)
        self.assertIsNot(stored.stage, LoopStage.COMPLETED)

    def test_the_session_stops_pointing_at_the_task_as_active(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.session.active_task_id = task.id
        self.memory.save_session(self.session)
        self.abandon(task.id, attempt_id)
        stored = self.memory.load_session(self.session.id)
        assert stored is not None
        self.assertIsNone(stored.active_task_id)
        self.assertEqual(stored.latest_task_id, task.id)

    def test_a_bounded_recovery_event_is_recorded(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        self.assertIn("execution_abandoned", self.events(task.id))
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM event_log WHERE task_id = ? "
                    "AND event_type = 'execution_abandoned'",
                    (task.id,),
                ).fetchone()[0]
            )
        self.assertEqual(payload["recovery"], AttemptRecovery.ABANDONED.value)
        self.assertEqual(payload["attempt_id"], attempt_id)
        self.assertEqual(
            payload["attempt_state"], AttemptState.UNKNOWN_OUTCOME.value
        )
        self.assertEqual(
            payload["error_code"], ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value
        )
        # The task's own words never travel into the recovery record.
        self.assertNotIn("do the thing", json.dumps(payload))

    def test_no_grant_is_consumed_and_no_reservation_is_created(self) -> None:
        task, attempt_id = self.in_doubt_task()
        before_grants = self.consumed_grants()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            before_rows = connection.execute(
                "SELECT COUNT(*) FROM attempt_billing"
            ).fetchone()[0]
        self.abandon(task.id, attempt_id)
        self.assertEqual(self.consumed_grants(), before_grants)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM attempt_billing"
                ).fetchone()[0],
                before_rows,
            )

    def test_the_connector_is_never_reached(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.registry.register(self.connector)
        self.abandon(task.id, attempt_id)
        self.assertEqual(self.connector.calls, [])

    def test_recovery_is_structurally_incapable_of_executing(self) -> None:
        """By source: no connector, and no write to the attempt's own state.

        The second half is the stronger claim. The primitive never issues an
        UPDATE against ``execution_attempts`` at all, so there is no statement
        it could get wrong: the attempt cannot be moved to a claimable state,
        cannot be marked finished, and cannot be touched by accident.
        """
        source = inspect.getsource(SQLiteMemoryStore.abandon_unknown_attempt)
        body = source.split('"""', 2)[-1]
        for forbidden in ("registry", "connector.", "self.registry"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)
        self.assertNotIn(
            "UPDATE execution_attempts",
            body,
            "recovery writes the attempt's own state",
        )
        self.assertNotIn("AttemptState.PREPARED", body)
        self.assertNotIn("AttemptState.FINISHED", body)


class ReplayAfterRecoveryTests(RecoveryTestCase):
    """An abandoned attempt can never run again."""

    def test_resume_reports_terminal_and_never_executes(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.registry.register(self.connector)
        self.abandon(task.id, attempt_id)
        for _ in range(3):
            run = self.core.resume(self.session.id, task.id)
            self.assertIs(run.status, RunStatus.FAILED)
        self.assertEqual(self.connector.calls, [])
        self.assertIs(self.attempt_state(task.id), AttemptState.UNKNOWN_OUTCOME)

    def test_a_fresh_core_and_store_still_cannot_replay(self) -> None:
        """Restart changes nothing: the refusal is durable, not in memory."""
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        fresh_store = SQLiteMemoryStore(self.database)
        fresh_registry = ToolRegistry()
        fresh_connector = _RecordingConnector()
        fresh_registry.register(fresh_connector)
        fresh_core = AgentCore(
            memory=fresh_store, registry=fresh_registry, settings=self.settings
        )
        run = fresh_core.resume(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.FAILED)
        self.assertEqual(fresh_connector.calls, [])

    def test_unknown_outcome_remains_non_replayable_without_recovery(
        self,
    ) -> None:
        """The pre-existing H2 block is untouched by S2A."""
        task, attempt_id = self.in_doubt_task()
        self.registry.register(self.connector)
        run = self.core.resume(self.session.id, task.id)
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.ACTIVE.value
        )
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        self.assertEqual(self.connector.calls, [])
        # Still NON-terminal: recovery is the operator's decision, not a
        # side effect of looking at the task.
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.RUNNING)


class ReservationTests(RecoveryTestCase):
    """H3: abandoning an unknown outcome keeps the money exposed."""

    def test_an_active_reservation_becomes_unresolved(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.ACTIVE.value
        )
        self.abandon(task.id, attempt_id)
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.UNRESOLVED.value
        )

    def test_the_amount_is_never_zeroed_or_released(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        self.assertEqual(self.reserved_amount(attempt_id), RESERVED_NANO_USD)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            actual = connection.execute(
                "SELECT actual_nano_usd FROM attempt_billing WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
        self.assertIsNone(actual, "recovery settled an unknown outcome")

    def test_the_ledger_exposure_stays_conservative(self) -> None:
        """Before and after, the month still carries the same exposure."""
        task, attempt_id = self.in_doubt_task()
        before = self.memory.ledger_snapshot("2026-08")
        self.abandon(task.id, attempt_id)
        after = self.memory.ledger_snapshot("2026-08")
        self.assertEqual(after.reserved_nano_usd, before.reserved_nano_usd)
        self.assertGreaterEqual(after.reserved_nano_usd, RESERVED_NANO_USD)
        # ...and it is now visible as UNRESOLVED rather than merely reserved.
        self.assertEqual(after.unresolved_nano_usd, RESERVED_NANO_USD)
        self.assertEqual(after.total_nano_usd, before.total_nano_usd)

    def test_an_already_unresolved_reservation_is_left_alone(self) -> None:
        task = self.make_task()
        attempt_id = self.open_attempt(task.id, AttemptState.UNKNOWN_OUTCOME)
        self.reserve(attempt_id, task.id, ReservationState.UNRESOLVED)
        self.abandon(task.id, attempt_id)
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.UNRESOLVED.value
        )

    def test_a_reconciled_reservation_is_never_reopened(self) -> None:
        """THE H3 LAW: settled money is not un-settled by recovery."""
        task = self.make_task()
        attempt_id = self.open_attempt(task.id, AttemptState.UNKNOWN_OUTCOME)
        self.reserve(
            attempt_id, task.id, ReservationState.RECONCILED, actual=RESERVED_NANO_USD
        )
        self.abandon(task.id, attempt_id)
        self.assertEqual(
            self.reservation_state(attempt_id),
            ReservationState.RECONCILED.value,
            "recovery reopened a reconciled reservation",
        )

    def test_an_attempt_with_no_reservation_recovers_cleanly(self) -> None:
        task = self.make_task()
        attempt_id = self.open_attempt(task.id, AttemptState.UNKNOWN_OUTCOME)
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )
        self.assertIsNone(self.reservation_state(attempt_id))


class RefusalTests(RecoveryTestCase):
    """Every refusal writes NOTHING at all."""

    def test_only_unknown_outcome_is_recoverable(self) -> None:
        for state in (
            AttemptState.PREPARED,
            AttemptState.IN_FLIGHT,
            AttemptState.FINISHED,
        ):
            with self.subTest(attempt=state.value):
                task = self.make_task()
                attempt_id = self.open_attempt(task.id, state)
                self.reserve(attempt_id, task.id)
                before = self.database_content()
                self.assertIs(
                    self.abandon(task.id, attempt_id),
                    AttemptRecoveryOutcome.NOT_RECOVERABLE,
                    state.value,
                )
                self.assertUntouched(before, state.value)

    def test_a_missing_attempt_is_refused(self) -> None:
        task = self.make_task()
        before = self.database_content()
        self.assertIs(
            self.abandon(task.id, "no-such-attempt"),
            AttemptRecoveryOutcome.UNKNOWN,
        )
        self.assertUntouched(before)

    def test_a_foreign_session_is_refused_without_disclosure(self) -> None:
        task, attempt_id = self.in_doubt_task()
        stranger = SessionState(objective="elsewhere")
        self.memory.save_session(stranger)
        before = self.database_content()
        self.assertIs(
            self.abandon(task.id, attempt_id, session_id=stranger.id),
            AttemptRecoveryOutcome.UNKNOWN,
        )
        self.assertUntouched(before)

    def test_a_foreign_task_is_refused(self) -> None:
        task, attempt_id = self.in_doubt_task()
        other = self.make_task()
        before = self.database_content()
        self.assertIs(
            self.abandon(other.id, attempt_id), AttemptRecoveryOutcome.UNKNOWN
        )
        self.assertUntouched(before)
        # NON-VACUITY: the same attempt under its OWN task does recover, so
        # the refusal above is the pairing rule and not a blanket failure.
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )

    def test_a_terminal_task_is_refused(self) -> None:
        for status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.REJECTED,
        ):
            with self.subTest(status=status.value):
                task = self.make_task()
                attempt_id = self.open_attempt(
                    task.id, AttemptState.UNKNOWN_OUTCOME
                )
                stored = self.memory.load_task(task.id)
                assert stored is not None
                stored.status = status
                self.memory.save_task(self.session.id, stored)
                before = self.database_content()
                self.assertIs(
                    self.abandon(task.id, attempt_id),
                    AttemptRecoveryOutcome.TERMINAL,
                    status.value,
                )
                self.assertUntouched(before, status.value)

    def test_a_double_recovery_is_refused(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )
        after_first = self.database_content()
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.TERMINAL
        )
        self.assertUntouched(after_first, "second recovery")

    def test_a_malformed_recovery_value_is_refused(self) -> None:
        task, attempt_id = self.in_doubt_task()
        before = self.database_content()
        for bogus in ("abandoned", None, 1, object()):
            with self.subTest(recovery=type(bogus).__name__):
                self.assertIs(
                    self.abandon(task.id, attempt_id, recovery=bogus),
                    AttemptRecoveryOutcome.NOT_RECOVERABLE,
                )
        self.assertUntouched(before)

    def test_a_malformed_persisted_task_is_refused(self) -> None:
        task, attempt_id = self.in_doubt_task()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                ("{not json at all", task.id),
            )
            connection.commit()
        before = self.database_content()
        try:
            outcome = self.abandon(task.id, attempt_id)
        except Exception as exc:  # noqa: BLE001 - that would BE the defect
            self.fail(f"recovery raised {type(exc).__name__} on a malformed row")
        self.assertIs(outcome, AttemptRecoveryOutcome.UNKNOWN)
        self.assertUntouched(before)

    def test_a_malformed_persisted_session_is_refused(self) -> None:
        task, attempt_id = self.in_doubt_task()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                ("null", self.session.id),
            )
            connection.commit()
        before = self.database_content()
        try:
            outcome = self.abandon(task.id, attempt_id)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"recovery raised {type(exc).__name__} on a malformed row")
        self.assertIs(outcome, AttemptRecoveryOutcome.UNKNOWN)
        self.assertUntouched(before)

    def test_an_attempt_belonging_to_another_task_is_refused(self) -> None:
        """The attempt's own columns must agree with the caller's pair."""
        task, attempt_id = self.in_doubt_task()
        other = self.make_task()
        other_attempt = self.open_attempt(other.id, AttemptState.UNKNOWN_OUTCOME)
        before = self.database_content()
        # Right task, wrong attempt.
        self.assertIs(
            self.abandon(task.id, other_attempt), AttemptRecoveryOutcome.UNKNOWN
        )
        self.assertUntouched(before)
        # NON-VACUITY: each attempt recovers under its own task.
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )


class CancelCorrectionTests(RecoveryTestCase):
    """An UNKNOWN outcome may not be relabelled as a clean cancellation."""

    def test_cancel_refuses_to_call_an_unknown_outcome_cancelled(self) -> None:
        """THE S2A CORRECTION: 'cancelled' would assert nothing happened."""
        task, attempt_id = self.in_doubt_task()
        self.registry.register(self.connector)
        before = self.database_content()
        run = self.core.cancel(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        # Nothing was written: task, attempt, reservation and chain all stand.
        self.assertUntouched(before, "cancel on unknown outcome")
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIsNot(stored.status, TaskStatus.CANCELLED)
        self.assertIs(stored.status, TaskStatus.RUNNING)
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.ACTIVE.value
        )

    def test_repeated_cancel_stays_refused(self) -> None:
        task, attempt_id = self.in_doubt_task()
        for _ in range(3):
            run = self.core.cancel(self.session.id, task.id)
            self.assertIs(run.status, RunStatus.FAILED)
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.ACTIVE.value
        )
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.RUNNING)

    def test_the_operator_can_still_recover_after_a_refused_cancel(self) -> None:
        """The refusal is a signpost, not a dead end."""
        task, attempt_id = self.in_doubt_task()
        self.core.cancel(self.session.id, task.id)
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.FAILED)

    def test_prepared_cancellation_behaviour_is_unchanged(self) -> None:
        """POSITIVE CONTROL: an unclaimed attempt still cancels cleanly."""
        task = self.make_task()
        self.open_attempt(task.id, AttemptState.PREPARED)
        run = self.core.cancel(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.CANCELLED)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCELLED)

    def test_in_flight_cancellation_behaviour_is_unchanged(self) -> None:
        """POSITIVE CONTROL: a live claim still records pending intent."""
        task = self.make_task()
        self.open_attempt(task.id, AttemptState.IN_FLIGHT)
        run = self.core.cancel(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.CANCELLING)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCEL_REQUESTED)

    def test_pre_execution_cancellation_behaviour_is_unchanged(self) -> None:
        """POSITIVE CONTROL: no attempt at all still cancels cleanly."""
        task = self.make_task()
        run = self.core.cancel(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.CANCELLED)

    def test_the_refusal_is_taken_before_any_write(self) -> None:
        """By source: the guard precedes the branch that terminalizes."""
        source = inspect.getsource(SQLiteMemoryStore._cancel_task_atomically)
        guard = source.index('CancellationOutcome("in_doubt"')
        self.assertLess(
            guard,
            source.index("task.status = TaskStatus.CANCELLED"),
            "the unknown-outcome guard runs after a cancellation write",
        )


class ChainEffectTests(RecoveryTestCase):
    """Recovery never writes the chain; ordinary chain authority observes it."""

    def declare_chain(self) -> object:
        from jarvis.contracts import ChainState, chain_steps_from_declaration

        chain = ChainState.declared(
            chain_steps_from_declaration(
                [
                    {
                        "objective": "Read the project notes.",
                        "capability": "file_read",
                        "action": "read",
                        "target": "notes.txt",
                        "consumes": -1,
                    }
                ]
            )
        )
        assert chain is not None
        self.assertEqual(
            self.memory.declare_chain_if_absent(self.session.id, chain), "declared"
        )
        stored = self.memory.load_session(self.session.id)
        assert stored is not None and stored.chain is not None
        return stored.chain

    def test_recovery_does_not_touch_the_chain(self) -> None:
        chain = self.declare_chain()
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        stored = self.memory.load_session(self.session.id)
        assert stored is not None and stored.chain is not None
        self.assertEqual(stored.chain.status, chain.status)
        self.assertEqual(stored.chain.cursor, chain.cursor)
        self.assertEqual(stored.chain.step_task_ids, chain.step_task_ids)

    def test_the_primitive_names_no_chain_authority(self) -> None:
        """By source: no chain write can hide in the recovery transaction."""
        source = inspect.getsource(SQLiteMemoryStore.abandon_unknown_attempt)
        for forbidden in ("chain_authority", "session.chain", "ChainStatus"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_existing_chain_authority_observes_the_failed_task(self) -> None:
        """The chain terminalizes through its OWN rule, on its next pass."""
        chain = self.declare_chain()
        task = Task(
            objective="Read the project notes.",
            capability="file_read",
            scope=ActionScope(action="read", target="notes.txt"),
        )
        from jarvis.contracts import ChainBinding, DataClassification

        task.scope = ActionScope(
            action="read",
            target="notes.txt",
            data_classification=DataClassification.INTERNAL,
        )
        task.chain_binding = ChainBinding(
            chain_id=chain.id, chain_digest=chain.digest, step_index=0
        )
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain.id, chain.digest, 0, task
        )
        self.assertEqual(verdict, "created")
        stored = self.memory.load_task(task_id)
        assert stored is not None
        stored.status = TaskStatus.RUNNING
        stored.stage = LoopStage.EXECUTE
        self.memory.save_task(self.session.id, stored)
        attempt_id = self.open_attempt(task_id, AttemptState.UNKNOWN_OUTCOME)
        self.assertIs(
            self.abandon(task_id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )
        # The chain is still ACTIVE right after recovery...
        after = self.memory.load_session(self.session.id)
        assert after is not None and after.chain is not None
        from jarvis.contracts import ChainStatus

        self.assertIs(after.chain.status, ChainStatus.ACTIVE)
        # ...and terminalizes itself the ordinary way.
        self.assertEqual(
            self.memory.advance_chain_step(self.session.id, chain.id, chain.digest, 0),
            "failed",
        )
        final = self.memory.load_session(self.session.id)
        assert final is not None and final.chain is not None
        self.assertIs(final.chain.status, ChainStatus.FAILED)


class ConcurrencyTests(RecoveryTestCase):
    """Separate stores over one real database, as separate processes have."""

    def other_store(self) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(self.database)

    def test_r1_two_simultaneous_abandons_produce_one_transition(self) -> None:
        for attempt in range(4):
            with self.subTest(round=attempt):
                task, attempt_id = self.in_doubt_task()
                barrier = threading.Barrier(2)
                outcomes: list[AttemptRecoveryOutcome] = []
                guard = threading.Lock()

                def race(
                    gate: threading.Barrier = barrier,
                    task_id: str = task.id,
                    claim: str = attempt_id,
                    lock: threading.Lock = guard,
                    sink: list = outcomes,
                ) -> None:
                    store = self.other_store()
                    gate.wait(timeout=10)
                    verdict = store.abandon_unknown_attempt(
                        self.session.id, task_id, claim, AttemptRecovery.ABANDONED
                    )
                    with lock:
                        sink.append(verdict)

                threads = [threading.Thread(target=race) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(len(outcomes), 2)
                self.assertEqual(
                    outcomes.count(AttemptRecoveryOutcome.RECOVERED),
                    1,
                    "two recoveries both claimed the transition",
                )
                stored = self.memory.load_task(task.id)
                assert stored is not None
                self.assertIs(stored.status, TaskStatus.FAILED)
                self.assertIs(
                    self.attempt_state(task.id), AttemptState.UNKNOWN_OUTCOME
                )

    def test_r2_an_abandoned_attempt_cannot_be_concluded(self) -> None:
        """UNKNOWN_OUTCOME never legitimately concludes; prove no resurrection."""
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        store = self.other_store()
        stored = store.load_task(task.id)
        session = store.load_session(self.session.id)
        assert stored is not None and session is not None
        stored.status = TaskStatus.COMPLETED
        stored.stage = LoopStage.COMPLETED
        outcome = store.conclude_execution(
            session, stored, attempt_id, "execution_completed", {}
        )
        self.assertEqual(outcome, "lost", "an abandoned attempt concluded")
        final = self.memory.load_task(task.id)
        assert final is not None
        self.assertIs(final.status, TaskStatus.FAILED)
        self.assertIs(self.attempt_state(task.id), AttemptState.UNKNOWN_OUTCOME)

    def test_r3_abandon_versus_cancel_never_yields_a_cancelled_lie(self) -> None:
        for attempt in range(4):
            with self.subTest(round=attempt):
                task, attempt_id = self.in_doubt_task()
                barrier = threading.Barrier(2)
                results: list[str] = []
                guard = threading.Lock()

                def recover(
                    gate: threading.Barrier = barrier,
                    task_id: str = task.id,
                    claim: str = attempt_id,
                    lock: threading.Lock = guard,
                    sink: list = results,
                ) -> None:
                    store = self.other_store()
                    gate.wait(timeout=10)
                    verdict = store.abandon_unknown_attempt(
                        self.session.id, task_id, claim, AttemptRecovery.ABANDONED
                    )
                    with lock:
                        sink.append(f"abandon:{verdict.value}")

                def canceller(
                    gate: threading.Barrier = barrier,
                    task_id: str = task.id,
                    lock: threading.Lock = guard,
                    sink: list = results,
                ) -> None:
                    store = self.other_store()
                    core = AgentCore(
                        memory=store,
                        registry=ToolRegistry(),
                        settings=self.settings,
                    )
                    gate.wait(timeout=10)
                    run = core.cancel(self.session.id, task_id)
                    with lock:
                        sink.append(f"cancel:{run.status.value}")

                threads = [
                    threading.Thread(target=recover),
                    threading.Thread(target=canceller),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                    self.assertFalse(thread.is_alive())
                stored = self.memory.load_task(task.id)
                assert stored is not None
                self.assertIsNot(
                    stored.status,
                    TaskStatus.CANCELLED,
                    f"a CANCELLED lie survived: {results}",
                )
                self.assertIn(
                    stored.status, (TaskStatus.FAILED, TaskStatus.RUNNING)
                )

    def test_r4_abandon_versus_run_chain_never_writes_the_chain(self) -> None:
        chain_case = ChainEffectTests("test_recovery_does_not_touch_the_chain")
        chain_case.setUp()
        try:
            chain = chain_case.declare_chain()
            task, attempt_id = chain_case.in_doubt_task()
            before = chain_case.memory.load_session(chain_case.session.id)
            assert before is not None and before.chain is not None
            barrier = threading.Barrier(2)

            def recover() -> None:
                store = SQLiteMemoryStore(chain_case.database)
                barrier.wait(timeout=10)
                store.abandon_unknown_attempt(
                    chain_case.session.id,
                    task.id,
                    attempt_id,
                    AttemptRecovery.ABANDONED,
                )

            def drive() -> None:
                store = SQLiteMemoryStore(chain_case.database)
                core = AgentCore(
                    memory=store,
                    registry=ToolRegistry(),
                    settings=chain_case.settings,
                )
                barrier.wait(timeout=10)
                core.run_chain(chain_case.session.id)

            threads = [
                threading.Thread(target=recover),
                threading.Thread(target=drive),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive())
            after = chain_case.memory.load_session(chain_case.session.id)
            assert after is not None and after.chain is not None
            self.assertEqual(after.chain.id, chain.id)
            # Recovery wrote no chain; only the driver's own authority could,
            # and this task is not the chain's step task.
            self.assertEqual(after.chain.cursor, before.chain.cursor)
        finally:
            chain_case.doCleanups()

    def test_r5_a_stale_repeated_request_is_deterministically_refused(
        self,
    ) -> None:
        task, attempt_id = self.in_doubt_task()
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )
        settled = self.database_content()
        for _ in range(4):
            store = self.other_store()
            self.assertIs(
                store.abandon_unknown_attempt(
                    self.session.id, task.id, attempt_id, AttemptRecovery.ABANDONED
                ),
                AttemptRecoveryOutcome.TERMINAL,
            )
        self.assertUntouched(settled, "stale repeats")


class CommandSurfaceTests(RecoveryTestCase):
    """The operator verb, its bounded facts, and its exit codes."""

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(argv, settings=self.settings)
        return code, buffer.getvalue()

    def resolve_argv(self, task_id: str, attempt_id: str) -> list[str]:
        return [
            "task",
            "resolve",
            "--session-id",
            self.session.id,
            "--task-id",
            task_id,
            "--attempt-id",
            attempt_id,
            "--resolution",
            "abandoned",
        ]

    def test_the_response_states_the_bounded_facts(self) -> None:
        task, attempt_id = self.in_doubt_task()
        result = task_resolve_result(
            self.memory, self.settings, self.session.id, task.id, attempt_id,
            "abandoned",
        )
        self.assertEqual(result.exit_code, EXIT_OK)
        payload = result.payload
        self.assertEqual(payload["outcome"], AttemptRecoveryOutcome.RECOVERED.value)
        self.assertEqual(
            payload["attempt_state"], AttemptState.UNKNOWN_OUTCOME.value
        )
        self.assertIs(payload["connector_replay"], False)
        self.assertEqual(payload["task_status"], TaskStatus.FAILED.value)
        self.assertEqual(
            payload["error_code"], ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value
        )
        self.assertIs(payload["monetary_exposure_released"], False)
        self.assertIs(payload["new_reservation_created"], False)
        self.assertIs(payload["reconciled_reservation_reopened"], False)
        self.assertIs(payload["chain_modified"], False)
        # F3. The response reports only UNIVERSAL invariants, never this
        # attempt's row. "reservation: unresolved" was false whenever the
        # attempt had none, or had a RECONCILED one recovery left alone; and
        # naming the ACTIVE -> UNRESOLVED transition was false for exactly the
        # same three cases. Neither may reappear in any shape.
        self.assertNotIn("reservation", payload)
        self.assertNotIn("active_reservation_moved_to_unresolved", payload)

    def test_no_task_content_reaches_the_response(self) -> None:
        task, attempt_id = self.in_doubt_task()
        _code, output = self.run_cli(self.resolve_argv(task.id, attempt_id))
        self.assertNotIn("do the thing", output)
        self.assertNotIn("notes.txt", output)

    def test_the_cli_resolves_and_reports_exit_zero(self) -> None:
        task, attempt_id = self.in_doubt_task()
        code, output = self.run_cli(self.resolve_argv(task.id, attempt_id))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(output)["command"], "task-resolve")
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.FAILED)

    def test_the_attempt_id_is_mandatory(self) -> None:
        task, _attempt_id = self.in_doubt_task()
        code, output = self.run_cli(
            [
                "task", "resolve", "--session-id", self.session.id,
                "--task-id", task.id, "--resolution", "abandoned",
            ]
        )
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertEqual(json.loads(output)["error"]["code"], "invalid_task")

    def test_the_resolution_vocabulary_is_closed(self) -> None:
        task, attempt_id = self.in_doubt_task()
        for bogus in ("retry", "succeeded", "release", "completed"):
            with self.subTest(resolution=bogus):
                argv = self.resolve_argv(task.id, attempt_id)
                argv[-1] = bogus
                code, output = self.run_cli(argv)
                self.assertEqual(code, EXIT_INVALID_INPUT, bogus)
                self.assertEqual(
                    json.loads(output)["error"]["code"], "invalid_task", bogus
                )
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.RUNNING)

    def test_refusals_carry_the_intended_exit_codes(self) -> None:
        task = self.make_task()
        prepared = self.open_attempt(task.id, AttemptState.PREPARED)
        code, output = self.run_cli(self.resolve_argv(task.id, prepared))
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertEqual(json.loads(output)["error"]["code"], "invalid_task")
        code, output = self.run_cli(self.resolve_argv(task.id, "no-such-attempt"))
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertEqual(json.loads(output)["error"]["code"], "task_not_found")

    def test_a_refusal_never_names_the_owning_session(self) -> None:
        task, attempt_id = self.in_doubt_task()
        stranger = SessionState(objective="elsewhere")
        self.memory.save_session(stranger)
        code, output = self.run_cli(
            [
                "task", "resolve", "--session-id", stranger.id,
                "--task-id", task.id, "--attempt-id", attempt_id,
                "--resolution", "abandoned",
            ]
        )
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertNotIn(self.session.id, output)

    def test_the_command_builds_no_application(self) -> None:
        """Recovery must be structurally incapable of executing anything."""
        task, attempt_id = self.in_doubt_task()

        def forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("recovery built an application")

        buffer = io.StringIO()
        with (
            mock.patch("jarvis.cli.build_default_app", forbidden),
            contextlib.redirect_stdout(buffer),
        ):
            code = cli_main(self.resolve_argv(task.id, attempt_id), settings=self.settings)
        self.assertEqual(code, EXIT_OK)

    def test_the_bare_task_verb_is_refused(self) -> None:
        code, output = self.run_cli(["task"])
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertEqual(json.loads(output)["error"]["code"], "invalid_task")


class ShownAttemptIdFlowTests(RecoveryTestCase):
    """P2-1. The id the operator READS is the id recovery ACCEPTS.

    ``task resolve`` has always required the exact attempt id, which is the
    right shape - a blind "recover whatever is latest" is not expressible -
    but the id was not rendered anywhere, so the shipped authority could only
    be reached by opening SQLite. These pin the closed loop: show it, pass it
    back, and let the existing S2A authority do exactly what it always did.

    Knowing an id grants nothing. Every refusal below is the recovery path
    re-proving the attempt against the authoritative rows, not the display
    surface withholding something.
    """

    def run_cli(self, argv: list[str]) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(argv, settings=self.settings)
        return code, json.loads(buffer.getvalue())

    def shown_attempt_id(self, task_id: str, session_id: str | None = None) -> str:
        """The id exactly as an operator reads it out of ``jarvis show``."""
        code, payload = self.run_cli(
            ["show", "--session-id", session_id or self.session.id,
             "--task-id", task_id]
        )
        self.assertEqual(code, EXIT_OK)
        return payload["attempt"]["id"]

    def resolve(self, task_id: str, attempt_id: str, session_id: str | None = None):
        return self.run_cli(
            ["task", "resolve", "--session-id", session_id or self.session.id,
             "--task-id", task_id, "--attempt-id", attempt_id,
             "--resolution", "abandoned"]
        )

    def test_the_shown_id_resolves_the_attempt_it_names(self) -> None:
        """THE flow, with no database access anywhere in this test."""
        task, attempt_id = self.in_doubt_task()
        shown = self.shown_attempt_id(task.id)
        self.assertEqual(shown, attempt_id)
        code, payload = self.resolve(task.id, shown)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            payload["outcome"], AttemptRecoveryOutcome.RECOVERED.value
        )
        # Unchanged S2A semantics: the attempt KEEPS its unknown state and
        # the task becomes terminally FAILED. Recovery is not a retry.
        self.assertEqual(
            payload["attempt_state"], AttemptState.UNKNOWN_OUTCOME.value
        )
        self.assertEqual(payload["task_status"], TaskStatus.FAILED.value)
        self.assertIs(payload["connector_replay"], False)
        self.assertEqual(self.connector.calls, [])

    def test_reading_the_id_changes_nothing_before_the_decision(self) -> None:
        task, attempt_id = self.in_doubt_task()
        before = self.database_content()
        for _ in range(3):
            self.assertEqual(self.shown_attempt_id(task.id), attempt_id)
        self.assertEqual(before, self.database_content(), "show wrote a row")

    def test_a_wrong_attempt_id_still_refuses(self) -> None:
        task, _ = self.in_doubt_task()
        self.shown_attempt_id(task.id)
        before = self.database_content()
        code, _ = self.resolve(
            task.id, "00000000-0000-0000-0000-000000000000"
        )
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertEqual(before, self.database_content(), "a refusal wrote")
        self.assertEqual(self.connector.calls, [])

    def test_another_tasks_shown_id_cannot_resolve_this_task(self) -> None:
        """An id is a handle, never an authority over a different task."""
        mine, _ = self.in_doubt_task()
        theirs, _ = self.in_doubt_task()
        foreign = self.shown_attempt_id(theirs.id)
        before = self.database_content()
        code, _ = self.resolve(mine.id, foreign)
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertEqual(before, self.database_content())

    def test_a_foreign_session_can_neither_read_nor_use_the_id(self) -> None:
        task, attempt_id = self.in_doubt_task()
        stranger = SessionState(objective="stranger")
        self.memory.save_session(stranger)
        code, payload = self.run_cli(
            ["show", "--session-id", stranger.id, "--task-id", task.id]
        )
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertNotIn(attempt_id, json.dumps(payload))
        before = self.database_content()
        code, _ = self.resolve(task.id, attempt_id, session_id=stranger.id)
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertEqual(before, self.database_content())

    def test_an_in_flight_attempt_is_shown_but_still_cannot_be_abandoned(self) -> None:
        """Visibility is not permission, and there is no IN_FLIGHT recovery."""
        task = self.make_task()
        attempt_id = self.open_attempt(task.id, AttemptState.IN_FLIGHT)
        self.assertEqual(self.shown_attempt_id(task.id), attempt_id)
        before = self.database_content()
        code, payload = self.resolve(task.id, attempt_id)
        self.assertEqual(code, EXIT_INVALID_INPUT)
        # A refusal renders the ordinary error envelope, not a recovery
        # outcome: nothing was recovered, so there is no outcome to report.
        self.assertEqual(payload["error"]["code"], ErrorCode.INVALID_TASK.value)
        self.assertIn("UNKNOWN", payload["error"]["message"])
        self.assertEqual(before, self.database_content(), "IN_FLIGHT was written")
        self.assertEqual(self.connector.calls, [])
        latest = self.memory.latest_execution_attempt(task.id)
        assert latest is not None
        self.assertIs(latest.state, AttemptState.IN_FLIGHT)

    def test_a_prepared_attempt_is_shown_but_still_cannot_be_abandoned(self) -> None:
        task = self.make_task()
        attempt_id = self.open_attempt(task.id, AttemptState.PREPARED)
        self.assertEqual(self.shown_attempt_id(task.id), attempt_id)
        before = self.database_content()
        code, _ = self.resolve(task.id, attempt_id)
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertEqual(before, self.database_content())


class ScopeTests(RecoveryTestCase):
    """S2A adds exactly one recovery, and no more."""

    def test_no_generic_in_flight_recovery_exists(self) -> None:
        """The S2A lock: IN_FLIGHT has no operator exit yet, by design."""
        self.assertFalse(
            hasattr(SQLiteMemoryStore, "declare_attempt_unknown"),
            "S2B's primitive leaked into S2A",
        )
        task = self.make_task()
        attempt_id = self.open_attempt(task.id, AttemptState.IN_FLIGHT)
        self.assertIs(
            self.abandon(task.id, attempt_id),
            AttemptRecoveryOutcome.NOT_RECOVERABLE,
        )

    def test_no_lease_or_ownership_mechanism_was_added(self) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(execution_attempts)"
                ).fetchall()
            }
        self.assertEqual(
            columns,
            {
                "id", "session_id", "task_id", "connector", "fingerprint",
                "state", "created_at", "updated_at",
            },
            "a lease/ownership column appeared",
        )

    def test_the_recovery_vocabulary_stays_closed(self) -> None:
        self.assertEqual(
            [recovery.value for recovery in AttemptRecovery], ["abandoned"]
        )
        for forbidden in ("retry", "succeeded", "success", "release", "completed"):
            with self.subTest(word=forbidden):
                self.assertNotIn(
                    forbidden, [r.value for r in AttemptRecovery]
                )

    def test_the_schema_is_untouched(self) -> None:
        """S2A adds no storage schema of its own: 7 in, 7 out (Memory V2
        owns the current SQLite schema version; this milestone adds none)."""
        self.assertEqual(self.memory.schema_version(), SCHEMA_VERSION)
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        self.assertEqual(self.memory.schema_version(), SCHEMA_VERSION)

    def test_no_cancellation_claim_authority_is_persisted(self) -> None:
        """CLOSEOUT: the schema-7 claim mechanism is gone, not dormant.

        It existed only to authorize a live connector signal. S2A no longer
        signals, so keeping an unused authority table would be complexity that
        looks like a safety property and enforces nothing.
        """
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        for gone in ("cancellation_claims", "leases", "heartbeats", "orphans"):
            with self.subTest(table=gone):
                self.assertNotIn(gone, tables)
        source = inspect.getsource(SQLiteMemoryStore)
        for word in ("cancellation_claims", "acquire_cancellation_claim"):
            with self.subTest(symbol=word):
                self.assertNotIn(word, source)


# --------------------------------------------------------------------------
# S2A four-finding correction. The connector boundary, row authority, a
# durable diagnostic, and an operator report that only claims what it proved.
# --------------------------------------------------------------------------


#: Persisted documents that are valid JSON but not a description of the row
#: they sit in. Every one used to be able to reach a write.
_UNUSABLE_DOCUMENTS = {
    "empty object": "{}",
    "null": "null",
    "list": "[]",
    "number": "123",
    "string": '"text"',
    "malformed json": "{not json",
}


class ConservativeCancelTests(RecoveryTestCase):
    """S2A CLOSEOUT. Cancellation never crosses the connector boundary.

    Stopping live work means an external call that cannot be made atomic with
    the storage decision authorizing it - no transaction may span it, the
    execution owner must never block on it, and S2A admits no lease, heartbeat
    or reclaim to bound it. A durable claim made acquisition unique and left
    the signal itself detached, which is uniqueness rather than safety. So S2A
    fails closed and records honest intent instead. Live IN_FLIGHT
    connector-boundary coordination is deferred to S2B.
    """

    def signalling_task(self, state: AttemptState) -> tuple[Task, str]:
        """A task whose connector WOULD be signalled, if anything signalled."""
        task = self.make_task(selected_connector="local")
        attempt_id = self.open_attempt(task.id, state)
        if self.connector.manifest.name not in self.registry.list_names():
            self.registry.register(self.connector)
        self.connector.calls.clear()
        return task, attempt_id

    # -- 1. authoritative IN_FLIGHT --------------------------------------

    def test_an_in_flight_cancel_never_reaches_the_connector(self) -> None:
        """1. THE CLOSEOUT: zero calls, and an honest unconfirmed state."""
        task, _attempt_id = self.signalling_task(AttemptState.IN_FLIGHT)
        run = self.core.cancel(self.session.id, task.id)
        self.assertEqual(
            self.connector.calls, [], "cancel crossed the connector boundary"
        )
        # The existing honest representation, not a new lifecycle state.
        self.assertIs(run.status, RunStatus.CANCELLING)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCEL_REQUESTED)
        self.assertIsNot(stored.status, TaskStatus.CANCELLED)
        self.assertTrue(stored.cancellation_requested)
        # The attempt stays under execution-owner authority, untouched.
        self.assertIs(self.attempt_state(task.id), AttemptState.IN_FLIGHT)

    def test_repeated_in_flight_cancels_never_reach_the_connector(self) -> None:
        task, _attempt_id = self.signalling_task(AttemptState.IN_FLIGHT)
        for _ in range(3):
            run = self.core.cancel(self.session.id, task.id)
            self.assertIs(run.status, RunStatus.CANCELLING)
        self.assertEqual(self.connector.calls, [])
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIsNot(stored.status, TaskStatus.CANCELLED)

    def test_the_execution_owner_still_concludes_the_attempt(self) -> None:
        """A cancel request blocks nothing: the owner remains authoritative."""
        task, attempt_id = self.signalling_task(AttemptState.IN_FLIGHT)
        self.core.cancel(self.session.id, task.id)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        stored.status = TaskStatus.CANCELLED
        stored.stage = LoopStage.CANCELLED
        self.assertEqual(
            self.memory.conclude_execution(
                self.session,
                stored,
                attempt_id,
                "execution_cancelled",
                {},
                finalizing_cancellation=True,
            ),
            "finished",
        )
        self.assertIs(self.attempt_state(task.id), AttemptState.FINISHED)

    def test_no_connector_is_reachable_from_cancel_at_all(self) -> None:
        """By source: the registry is not consulted anywhere in cancel."""
        source = inspect.getsource(AgentCore.cancel)
        body = source.split('"""')[-1] if '"""' in source else source
        for forbidden in ("self.registry", ".cancel(task.id)", "selected_connector"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, body)

    # -- 4. the claimless confirmed-cancel defence ------------------------

    def test_a_caller_cannot_fabricate_a_confirmed_in_flight_cancel(self) -> None:
        """4. FINDING 2: a caller's word is not evidence about the world.

        The storage API used to take ``connector_cancelled``, and passing True
        terminalized a running task as CANCELLED. Nothing could check it, and
        after the closeout nothing can even legitimately hold it, so the
        parameter is gone rather than ignored - an ignored parameter is an
        invitation to believe it still means something.
        """
        task, _attempt_id = self.signalling_task(AttemptState.IN_FLIGHT)
        with self.assertRaises(TypeError):
            self.memory.cancel_task(
                self.session.id, task.id, connector_cancelled=True
            )
        # Nor can it hand in objects positionally and have them select a
        # row: the parameters are ids, and an object is not one.
        untouched = self.database_content()
        with self.assertRaises((TypeError, sqlite3.ProgrammingError)):
            self.memory.cancel_task(
                self.session, self.memory.load_task(task.id)
            )
        self.assertUntouched(untouched, "objects passed as ids")
        # ...and the honest call refuses to terminalize it either way.
        before = self.database_content()
        outcome = self.memory.cancel_task(self.session.id, task.id)
        self.assertEqual(
            outcome.verdict, "cancelling", "IN_FLIGHT was terminalized"
        )
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCEL_REQUESTED)
        self.assertIsNot(stored.status, TaskStatus.CANCELLED)
        # The attempt is untouched and no unauthorized row appeared.
        self.assertIs(self.attempt_state(task.id), AttemptState.IN_FLIGHT)
        after = self.database_content()
        self.assertEqual(
            len(after["execution_attempts"]), len(before["execution_attempts"])
        )
        self.assertEqual(before["execution_attempts"], after["execution_attempts"])
        self.assertEqual(before["attempt_billing"], after["attempt_billing"])

    def test_no_confirmation_parameter_survives_anywhere(self) -> None:
        """By source and signature: there is no way to assert a stop."""
        signature = inspect.signature(SQLiteMemoryStore.cancel_task)
        self.assertEqual(
            list(signature.parameters), ["self", "session_id", "task_id"],
            "a caller can still hand in state instead of naming a row",
        )
        # The audit event keeps the field, and it is now a fact rather than a
        # report: no connector was cancelled, because none was asked.
        body = inspect.getsource(SQLiteMemoryStore._cancel_task_atomically)
        self.assertIn('{"connector_cancelled": False}', body)
        self.assertNotIn('"connector_cancelled": connector_cancelled', body)
        self.assertNotIn("connector_cancelled=True", body)

    # -- 2, 3. races with execution authority -----------------------------

    def test_a_cancel_racing_the_unknown_transition_never_signals(self) -> None:
        """2. Either ordering: zero connector calls, no false cancellation."""
        for order in ("cancel first", "unknown first"):
            with self.subTest(order=order):
                task, attempt_id = self.signalling_task(AttemptState.IN_FLIGHT)
                if order == "unknown first":
                    self.memory.mark_execution_unknown(attempt_id)
                    run = self.core.cancel(self.session.id, task.id)
                    self.assertIs(run.status, RunStatus.FAILED)
                    assert run.error is not None
                    self.assertIs(
                        run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME
                    )
                else:
                    run = self.core.cancel(self.session.id, task.id)
                    self.assertIs(run.status, RunStatus.CANCELLING)
                    # The UNKNOWN transition still proceeds normally.
                    self.memory.mark_execution_unknown(attempt_id)
                self.assertEqual(self.connector.calls, [], order)
                self.assertIs(
                    self.attempt_state(task.id), AttemptState.UNKNOWN_OUTCOME
                )
                stored = self.memory.load_task(task.id)
                assert stored is not None
                self.assertIsNot(stored.status, TaskStatus.CANCELLED)

    def test_a_concurrent_cancel_and_unknown_transition_never_signal(self) -> None:
        """2. Under real threads, both orderings, repeatedly."""
        for attempt_number in range(8):
            with self.subTest(attempt=attempt_number):
                task, attempt_id = self.signalling_task(AttemptState.IN_FLIGHT)
                barrier = threading.Barrier(2)

                def canceller(
                    task_id: str = task.id, gate: threading.Barrier = barrier
                ) -> None:
                    gate.wait(timeout=5)
                    self.core.cancel(self.session.id, task_id)

                def unknowner(
                    aid: str = attempt_id, gate: threading.Barrier = barrier
                ) -> None:
                    gate.wait(timeout=5)
                    self.memory.mark_execution_unknown(aid)

                threads = [
                    threading.Thread(target=canceller),
                    threading.Thread(target=unknowner),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertEqual(self.connector.calls, [])
                self.assertIs(
                    self.attempt_state(task.id), AttemptState.UNKNOWN_OUTCOME
                )
                stored = self.memory.load_task(task.id)
                assert stored is not None
                self.assertIsNot(stored.status, TaskStatus.CANCELLED)

    def test_a_cancel_racing_conclude_execution_never_signals(self) -> None:
        """3. Conclusion authority stays with the owner; no stale signal."""
        for order in ("cancel first", "conclude first"):
            with self.subTest(order=order):
                task, attempt_id = self.signalling_task(AttemptState.IN_FLIGHT)
                stored = self.memory.load_task(task.id)
                assert stored is not None
                stored.status = TaskStatus.COMPLETED
                stored.stage = LoopStage.COMPLETED
                if order == "conclude first":
                    self.assertEqual(
                        self.memory.conclude_execution(
                            self.session, stored, attempt_id,
                            "execution_finished", {},
                        ),
                        "finished",
                    )
                    run = self.core.cancel(self.session.id, task.id)
                    # A completed task is reported as it is, never rewritten.
                    self.assertIsNot(run.status, RunStatus.CANCELLED)
                    self.assertIs(
                        self.memory.load_task(task.id).status,
                        TaskStatus.COMPLETED,
                    )
                else:
                    self.core.cancel(self.session.id, task.id)
                    # The owner still concludes; the cancellation guard
                    # decides which write wins, not a connector.
                    self.assertIn(
                        self.memory.conclude_execution(
                            self.session, stored, attempt_id,
                            "execution_finished", {},
                        ),
                        ("finished", "cancelled"),
                    )
                self.assertEqual(self.connector.calls, [], order)

    # -- 5, 6, 7. the paths that are still allowed to be final ------------

    def test_a_no_attempt_cancel_still_terminalizes(self) -> None:
        """5. Nothing was ever claimed, so CANCELLED is the honest answer."""
        task = self.make_task(selected_connector="local")
        if self.connector.manifest.name not in self.registry.list_names():
            self.registry.register(self.connector)
        self.connector.calls.clear()
        run = self.core.cancel(self.session.id, task.id)
        self.assertEqual(self.connector.calls, [])
        self.assertIs(run.status, RunStatus.CANCELLED)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCELLED)
        self.assertIs(stored.stage, LoopStage.CANCELLED)

    def test_a_prepared_cancel_still_terminalizes(self) -> None:
        """6. A prepared attempt never reached a connector either."""
        task, _attempt_id = self.signalling_task(AttemptState.PREPARED)
        run = self.core.cancel(self.session.id, task.id)
        self.assertEqual(self.connector.calls, [])
        self.assertIs(run.status, RunStatus.CANCELLED)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCELLED)

    def test_a_finished_attempt_cancel_still_terminalizes(self) -> None:
        task, _attempt_id = self.signalling_task(AttemptState.FINISHED)
        run = self.core.cancel(self.session.id, task.id)
        self.assertEqual(self.connector.calls, [])
        self.assertIs(run.status, RunStatus.CANCELLED)

    def test_an_unknown_outcome_cancel_signals_nothing_and_writes_nothing(
        self,
    ) -> None:
        """7. Still in doubt, still recovery-required, still no CANCELLED."""
        task, attempt_id = self.signalling_task(AttemptState.UNKNOWN_OUTCOME)
        self.reserve(attempt_id, task.id)
        before = self.database_content()
        run = self.core.cancel(self.session.id, task.id)
        self.assertEqual(self.connector.calls, [])
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        self.assertUntouched(before, "unknown-outcome cancel")
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.RUNNING)
        self.assertIsNot(stored.status, TaskStatus.CANCELLED)
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.ACTIVE.value
        )

    def test_cancel_versus_abandon_never_signals_after_recovery(self) -> None:
        """Recovery wins first: the task stays FAILED, nothing is signalled."""
        task, attempt_id = self.signalling_task(AttemptState.UNKNOWN_OUTCOME)
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )
        before = self.database_content()
        run = self.core.cancel(self.session.id, task.id)
        self.assertEqual(self.connector.calls, [])
        self.assertUntouched(before, "cancel after recovery")
        self.assertIs(run.status, RunStatus.FAILED)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.FAILED)

    def test_a_stale_view_cannot_finalize_a_cancellation(self) -> None:
        """A caller holding a pre-transition task object still loses.

        The finalizing write re-reads the attempt for itself, so a decision
        taken against a task that has since gone in doubt is refused rather
        than committed.
        """
        task, attempt_id = self.signalling_task(AttemptState.IN_FLIGHT)
        self.memory.mark_execution_unknown(attempt_id)
        before = self.database_content()
        outcome = self.memory.cancel_task(self.session.id, task.id)
        self.assertEqual(
            outcome.verdict, "in_doubt", "a stale view finalized a cancel"
        )
        self.assertUntouched(before, "stale-view finalization")

    # -- the S2A / S2B boundary, stated where it is enforced ---------------

    def test_the_deferred_capability_is_documented_where_it_is_enforced(
        self,
    ) -> None:
        """The gap is deliberate, and the code says so at the gap."""
        source = inspect.getsource(AgentCore.cancel)
        self.assertIn("S2B", source)
        self.assertIn("NO CONNECTOR IS SIGNALLED", source)

    def test_no_deferred_s2b_machinery_was_smuggled_in(self) -> None:
        """None of the S2B design space is implemented here.

        S2B is about the EXECUTION boundary: an attempt that crossed into a
        connector and whose outcome is in doubt. Scheduler phase 2A added a
        bounded lease, but it leases a job GENERATION - a place in a queue -
        and never an execution attempt, so the two must not be confused. The
        needles below therefore name attempt machinery specifically, and
        ``test_no_execution_attempt_is_ever_leased_or_reclaimed`` proves the
        separation from the durable schema rather than from a substring.
        """
        for module in (SQLiteMemoryStore, AgentCore):
            source = inspect.getsource(module)
            # Named machinery, not prose: "orphan" appears in unrelated
            # pre-existing docstrings, and banning the word would make this
            # test about vocabulary instead of about what exists.
            for forbidden in (
                "def heartbeat",
                "heartbeat_at",
                "attempt_lease",
                "declare_attempt_unknown",
                "def _detect_orphan",
                "orphan_recovery",
                "auto_reclaim",
                "runner_owner",
                "cancellation_claims",
                "acquire_cancellation_claim",
            ):
                with self.subTest(module=module.__name__, symbol=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_no_execution_attempt_is_ever_leased_or_reclaimed(self) -> None:
        """The one lease this build has belongs to a generation, not an attempt.

        Proved from the durable schema, so a future edit that gave attempts an
        expiry - the exact S2B capability that is deferred - fails here even
        if it invented a brand-new column name for it.
        """
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            attempt_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(execution_attempts)")
            }
            run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(job_runs)")
            }
        for column in attempt_columns:
            for banned in ("lease", "heartbeat", "reclaim", "expires"):
                self.assertNotIn(banned, column, f"execution_attempts.{column}")
        # And the scheduler side really does own the only one.
        self.assertIn("lease_expires_at_utc", run_columns)
        # An in-doubt attempt is still non-replayable: nothing times it out.
        task, attempt_id = self.signalling_task(AttemptState.IN_FLIGHT)
        self.memory.mark_execution_unknown(attempt_id)
        before = self.database_content()
        self.assertEqual(
            AttemptState.UNKNOWN_OUTCOME,
            self.memory.latest_execution_attempt(task.id).state,
        )
        self.assertUntouched(before, "reading an in-doubt attempt")


class RowAuthorityTests(RecoveryTestCase):
    """F2. A decoded document is not a description of the row it sits in."""

    def write_task_document(self, task_id: str, document: object) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (document if isinstance(document, str) else json.dumps(document),
                 task_id),
            )
            connection.commit()

    def write_session_document(self, session_id: str, document: object) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (document if isinstance(document, str) else json.dumps(document),
                 session_id),
            )
            connection.commit()

    def task_document(self, task_id: str) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return json.loads(
                connection.execute(
                    "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()[0]
            )

    def session_document(self, session_id: str) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return json.loads(
                connection.execute(
                    "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()[0]
            )

    def test_a_forged_task_identity_never_inserts_a_row(self) -> None:
        """THE F2 REPRODUCTION: a forged id used to create a NEW FAILED task."""
        task, attempt_id = self.in_doubt_task()
        document = self.task_document(task.id)
        document["id"] = "FORGED-TASK-ID"
        self.write_task_document(task.id, document)
        before = self.database_content()
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.UNKNOWN
        )
        self.assertUntouched(before, "forged task id")
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE id = 'FORGED-TASK-ID'"
                ).fetchone()[0],
                0,
                "a forged task row was inserted",
            )

    def test_a_forged_session_identity_never_mutates_another_session(
        self,
    ) -> None:
        """THE SHARPEST F2 CASE: recovery used to write a FOREIGN session."""
        victim = SessionState(objective="a different session")
        victim.next_step = "VICTIM-NEXT-STEP"
        self.memory.save_session(victim)
        task, attempt_id = self.in_doubt_task()
        document = self.session_document(self.session.id)
        document["id"] = victim.id
        document["next_step"] = "HIJACKED"
        self.write_session_document(self.session.id, document)
        before = self.database_content()
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.UNKNOWN
        )
        self.assertUntouched(before, "forged session id")
        stored = self.memory.load_session(victim.id)
        assert stored is not None
        self.assertEqual(stored.next_step, "VICTIM-NEXT-STEP")

    def test_no_unusable_task_document_reaches_a_write(self) -> None:
        for label, payload in _UNUSABLE_DOCUMENTS.items():
            with self.subTest(document=label):
                task, attempt_id = self.in_doubt_task()
                self.write_task_document(task.id, payload)
                before = self.database_content()
                try:
                    outcome = self.abandon(task.id, attempt_id)
                except Exception as exc:  # noqa: BLE001 - that would BE the bug
                    self.fail(f"{label}: recovery raised {type(exc).__name__}")
                self.assertIs(outcome, AttemptRecoveryOutcome.UNKNOWN, label)
                self.assertUntouched(before, label)

    def test_no_unusable_session_document_reaches_a_write(self) -> None:
        for label, payload in _UNUSABLE_DOCUMENTS.items():
            with self.subTest(document=label):
                task, attempt_id = self.in_doubt_task()
                self.write_session_document(self.session.id, payload)
                before = self.database_content()
                try:
                    outcome = self.abandon(task.id, attempt_id)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{label}: recovery raised {type(exc).__name__}")
                self.assertIs(outcome, AttemptRecoveryOutcome.UNKNOWN, label)
                self.assertUntouched(before, label)
                # Restore for the next round.
                self.write_session_document(
                    self.session.id, json.loads(json.dumps(
                        SessionState(objective="recovery", id=self.session.id).to_dict()
                    ))
                )

    def test_a_forged_lifecycle_never_rides_into_the_write(self) -> None:
        for field, value in (
            ("status", TaskStatus.COMPLETED.value),
            ("stage", LoopStage.COMPLETED.value),
        ):
            with self.subTest(field=field):
                task, attempt_id = self.in_doubt_task()
                document = self.task_document(task.id)
                document[field] = value
                self.write_task_document(task.id, document)
                before = self.database_content()
                self.assertIs(
                    self.abandon(task.id, attempt_id),
                    AttemptRecoveryOutcome.UNKNOWN,
                    field,
                )
                self.assertUntouched(before, field)

    def test_an_incomplete_task_document_is_refused(self) -> None:
        task, attempt_id = self.in_doubt_task()
        document = self.task_document(task.id)
        del document["capability"]
        self.write_task_document(task.id, document)
        before = self.database_content()
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.UNKNOWN
        )
        self.assertUntouched(before)

    def test_a_task_owned_by_another_session_is_refused(self) -> None:
        """Attempt ownership is not task ownership; both are proven."""
        task, attempt_id = self.in_doubt_task()
        stranger = SessionState(objective="elsewhere")
        self.memory.save_session(stranger)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?",
                (stranger.id, task.id),
            )
            connection.commit()
        before = self.database_content()
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.UNKNOWN
        )
        self.assertUntouched(before)

    def test_recovery_rebinds_rather_than_trusting_the_document(self) -> None:
        """By source: the recovery-specific validators, not S1 summaries."""
        source = inspect.getsource(SQLiteMemoryStore.abandon_unknown_attempt)
        self.assertIn("self._authoritative_task_row(", source)
        self.assertIn("self._authoritative_session_row(", source)
        for forbidden in ("task_summary(", "session_summary(", "_stored_task("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_a_canonical_row_still_recovers(self) -> None:
        """POSITIVE CONTROL: the strict rebinding refuses nothing legitimate."""
        task, attempt_id = self.in_doubt_task()
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )


class DurableFailureResultTests(RecoveryTestCase):
    """F3. A recovered task can say why it failed, after any restart."""

    def test_the_typed_failure_snapshot_is_persisted(self) -> None:
        """THE F3 REPRODUCTION: the snapshot used to stay None."""
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIsNotNone(stored.result_snapshot)
        snapshot = stored.result_snapshot
        assert snapshot is not None
        self.assertEqual(
            snapshot["error"]["code"], ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value
        )
        self.assertIs(snapshot["error"]["retryable"], False)
        self.assertEqual(snapshot["outcome"], "error")

    def test_a_stale_snapshot_is_replaced(self) -> None:
        task = self.make_task()
        stored = self.memory.load_task(task.id)
        assert stored is not None
        stored.result_snapshot = {
            "outcome": "success",
            "summary": "STALE-SUCCESS-CANARY",
        }
        self.memory.save_task(self.session.id, stored)
        attempt_id = self.open_attempt(task.id, AttemptState.UNKNOWN_OUTCOME)
        self.abandon(task.id, attempt_id)
        after = self.memory.load_task(task.id)
        assert after is not None and after.result_snapshot is not None
        self.assertNotIn(
            "STALE-SUCCESS-CANARY", json.dumps(after.result_snapshot)
        )
        self.assertEqual(after.result_snapshot["outcome"], "error")

    def test_an_immediate_reload_reconstructs_the_diagnostic(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        run = self.core.resume(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        self.assertFalse(run.error.retryable)

    def test_a_fresh_store_and_core_reconstruct_the_same_diagnostic(
        self,
    ) -> None:
        """Restart is the case this exists for."""
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        fresh_connector = _RecordingConnector()
        fresh_registry = ToolRegistry()
        fresh_registry.register(fresh_connector)
        fresh_core = AgentCore(
            memory=SQLiteMemoryStore(self.database),
            registry=fresh_registry,
            settings=self.settings,
        )
        run = fresh_core.resume(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        self.assertFalse(run.error.retryable)
        self.assertEqual(fresh_connector.calls, [], "restart replayed")

    def test_the_snapshot_carries_no_content(self) -> None:
        task, attempt_id = self.in_doubt_task()
        self.abandon(task.id, attempt_id)
        stored = self.memory.load_task(task.id)
        assert stored is not None and stored.result_snapshot is not None
        rendered = json.dumps(stored.result_snapshot)
        for canary in ("do the thing", "notes.txt"):
            with self.subTest(canary=canary):
                self.assertNotIn(canary, rendered)
        self.assertEqual(stored.result_snapshot["data"], {})

    def test_there_is_only_one_result_serializer(self) -> None:
        """By source: core delegates rather than restating the shape."""
        source = inspect.getsource(AgentCore._result_to_dict)
        self.assertIn("result_to_dict(result)", source)
        self.assertNotIn('"outcome": result.outcome.value', source)


class OperatorReportTests(RecoveryTestCase):
    """F4. The report claims only what recovery actually guarantees."""

    def resolve(self, task_id: str, attempt_id: str) -> dict:
        return task_resolve_result(
            self.memory, self.settings, self.session.id, task_id, attempt_id,
            "abandoned",
        ).payload

    def test_no_reservation_state_is_ever_claimed(self) -> None:
        """THE F4 REPRODUCTION: 'unresolved' was asserted unconditionally."""
        cases = {
            "active": (ReservationState.ACTIVE, None),
            "already unresolved": (ReservationState.UNRESOLVED, None),
            "reconciled": (ReservationState.RECONCILED, RESERVED_NANO_USD),
            "no reservation": (None, None),
        }
        for label, (state, actual) in cases.items():
            with self.subTest(reservation=label):
                task = self.make_task()
                attempt_id = self.open_attempt(
                    task.id, AttemptState.UNKNOWN_OUTCOME
                )
                if state is not None:
                    self.reserve(attempt_id, task.id, state, actual)
                payload = self.resolve(task.id, attempt_id)
                self.assertNotIn("reservation", payload, label)
                # F3. No field may name this attempt's reservation state OR a
                # transition of it. Three of these four cases had no ACTIVE
                # row to move, so an unconditional claim was false in all of
                # them; the fix is removal, not a conditional guess.
                self.assertNotIn(
                    "active_reservation_moved_to_unresolved", payload, label
                )
                for key, value in payload.items():
                    with self.subTest(reservation=label, key=key):
                        self.assertNotIn(
                            ReservationState.UNRESOLVED.value,
                            str(value).lower(),
                            f"{label}: {key} names a reservation state",
                        )
                # Only the invariants, which hold in every one of these cases.
                self.assertIs(payload["monetary_exposure_released"], False, label)
                self.assertIs(payload["new_reservation_created"], False, label)
                self.assertIs(
                    payload["reconciled_reservation_reopened"], False, label
                )

    def test_every_reported_invariant_matches_storage(self) -> None:
        """The invariants are true of the rows, not merely printed."""
        cases = {
            "active": (ReservationState.ACTIVE, None, "unresolved"),
            "already unresolved": (ReservationState.UNRESOLVED, None, "unresolved"),
            "reconciled": (
                ReservationState.RECONCILED, RESERVED_NANO_USD, "reconciled",
            ),
        }
        for label, (state, actual, expected) in cases.items():
            with self.subTest(reservation=label):
                task = self.make_task()
                attempt_id = self.open_attempt(
                    task.id, AttemptState.UNKNOWN_OUTCOME
                )
                self.reserve(attempt_id, task.id, state, actual)
                self.resolve(task.id, attempt_id)
                self.assertEqual(
                    self.reservation_state(attempt_id), expected, label
                )
                self.assertEqual(
                    self.reserved_amount(attempt_id), RESERVED_NANO_USD, label
                )

    def test_no_reservation_row_is_ever_created(self) -> None:
        task = self.make_task()
        attempt_id = self.open_attempt(task.id, AttemptState.UNKNOWN_OUTCOME)
        self.resolve(task.id, attempt_id)
        self.assertIsNone(self.reservation_state(attempt_id))

    def test_the_narrative_matches_the_invariants(self) -> None:
        task, attempt_id = self.in_doubt_task()
        payload = self.resolve(task.id, attempt_id)
        narrative = payload["next_step"]
        self.assertIn("No monetary exposure was released", narrative)
        self.assertIn("reconciled", narrative)


class RollbackTests(RecoveryTestCase):
    """The recovery transaction is all-or-nothing."""

    def test_a_failure_at_the_last_write_leaves_storage_unchanged(self) -> None:
        """Force a raise at the LATEST practical write point and compare.

        The reservation transition is the final statement in the
        transaction, so failing there proves the task write, the session
        write and the checkpoint/event writes all roll back with it.
        """
        task, attempt_id = self.in_doubt_task()
        before = self.database_content()
        real_write = SQLiteMemoryStore._write_checkpoint_and_event

        def explode(connection, session, task_arg, event_type, payload,
                    created_at=None):
            real_write(connection, session, task_arg, event_type, payload,
                       created_at)
            raise RuntimeError("forced failure after the last logical write")

        with mock.patch.object(
            SQLiteMemoryStore,
            "_write_checkpoint_and_event",
            staticmethod(explode),
        ), self.assertRaises(RuntimeError):
            self.abandon(task.id, attempt_id)
        self.assertUntouched(before, "rollback")
        # Every surface, named explicitly.
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.RUNNING)
        self.assertIsNone(stored.result_snapshot)
        self.assertIs(self.attempt_state(task.id), AttemptState.UNKNOWN_OUTCOME)
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.ACTIVE.value
        )
        self.assertNotIn("execution_abandoned", self.events(task.id))

    def test_recovery_still_succeeds_after_a_rolled_back_attempt(self) -> None:
        """POSITIVE CONTROL: the rollback left the row recoverable."""
        task, attempt_id = self.in_doubt_task()

        def explode(connection, session, task_arg, event_type, payload,
                    created_at=None):
            raise RuntimeError("forced")

        with mock.patch.object(
            SQLiteMemoryStore,
            "_write_checkpoint_and_event",
            staticmethod(explode),
        ), self.assertRaises(RuntimeError):
            self.abandon(task.id, attempt_id)
        self.assertIs(
            self.abandon(task.id, attempt_id), AttemptRecoveryOutcome.RECOVERED
        )
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.UNRESOLVED.value
        )

    def test_the_transaction_opens_no_nested_public_helper(self) -> None:
        """By source: every write uses a transaction-local primitive."""
        source = inspect.getsource(SQLiteMemoryStore.abandon_unknown_attempt)
        body = source.split('"""', 2)[-1]
        self.assertEqual(body.count("self._connection("), 1)
        for nested in ("self.save_task(", "self.save_session(",
                       "self.mark_reservation_unresolved("):
            with self.subTest(nested=nested):
                self.assertNotIn(nested, body)


# --------------------------------------------------------------------------
# Cancellation row/document authority. The requested id is the only selector,
# and a persisted document never gets to describe a row it is not.
# --------------------------------------------------------------------------


class CancellationAuthorityTests(RecoveryTestCase):
    """A cancellation is aimed by ID. Documents describe; they do not select."""

    def make_pair(self) -> tuple[Task, Task]:
        return self.make_task(), self.make_task()

    def forge_task(self, task_id: str, **fields: object) -> None:
        """Corrupt ONLY state_json; the authoritative columns stay put."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            raw = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0]
            document = json.loads(raw)
            document.update(fields)
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task_id),
            )
            connection.commit()

    def set_task_document(self, task_id: str, raw: str) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?", (raw, task_id)
            )
            connection.commit()

    def task_row(self, task_id: str) -> tuple[str, str, str] | None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT session_id, status, stage FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()

    def assertRefused(self, run, label: str = "") -> None:
        """A bounded, non-disclosing refusal - never a terminal verdict."""
        self.assertIs(run.status, RunStatus.FAILED, label)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.TASK_NOT_FOUND, label)
        for terminal in (
            RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.CANCELLING,
        ):
            self.assertIsNot(run.status, terminal, label)

    # -- 1. same-session wrong id -----------------------------------------

    def test_1_a_document_naming_another_task_cancels_nothing(self) -> None:
        """THE P1 REPRODUCTION: cancelling A used to cancel B instead."""
        first, second = self.make_pair()
        self.forge_task(first.id, id=second.id)
        before = self.database_content()
        run = self.core.cancel(self.session.id, first.id)
        self.assertRefused(run, "a forged id drove a cancellation")
        self.assertUntouched(before, "wrong-row cancellation")
        # Both rows, named explicitly: neither the requested task nor the one
        # the document pointed at moved.
        self.assertEqual(self.task_row(first.id)[1], TaskStatus.RUNNING.value)
        self.assertEqual(self.task_row(second.id)[1], TaskStatus.RUNNING.value)
        self.assertEqual(self.events(first.id), [])
        self.assertEqual(self.events(second.id), [])
        # ...and the run does not hand the caller the other task's identity.
        self.assertIsNot(getattr(run.task, "id", None), second.id)

    def test_1_the_requested_id_is_the_only_selector(self) -> None:
        """NON-VACUITY: the same pair cancels correctly once uncorrupted."""
        first, second = self.make_pair()
        run = self.core.cancel(self.session.id, first.id)
        self.assertIs(run.status, RunStatus.CANCELLED)
        self.assertEqual(run.task.id, first.id)
        self.assertEqual(self.task_row(second.id)[1], TaskStatus.RUNNING.value)

    # -- 2, 3. forged lifecycle -------------------------------------------

    def test_2_a_forged_status_is_refused_with_zero_writes(self) -> None:
        forged = {
            "terminal": TaskStatus.COMPLETED.value,
            "nonterminal": TaskStatus.VERIFYING.value,
            "cancelled": TaskStatus.CANCELLED.value,
        }
        for label, status in forged.items():
            with self.subTest(status=label):
                task = self.make_task()
                self.open_attempt(task.id, AttemptState.IN_FLIGHT)
                self.forge_task(task.id, status=status)
                before = self.database_content()
                run = self.core.cancel(self.session.id, task.id)
                self.assertRefused(run, label)
                self.assertUntouched(before, f"forged status {label}")
                self.assertEqual(
                    self.task_row(task.id)[1], TaskStatus.RUNNING.value, label
                )

    def test_3_a_forged_stage_is_refused_with_zero_writes(self) -> None:
        """THE DRIFT REPRODUCTION: a forged stage used to be persisted."""
        forged = {
            "nonterminal": LoopStage.PLAN.value,
            "terminal": LoopStage.COMPLETED.value,
            "cancelled": LoopStage.CANCELLED.value,
        }
        for label, stage in forged.items():
            with self.subTest(stage=label):
                task = self.make_task()
                self.open_attempt(task.id, AttemptState.IN_FLIGHT)
                self.forge_task(task.id, stage=stage)
                before = self.database_content()
                run = self.core.cancel(self.session.id, task.id)
                self.assertRefused(run, label)
                self.assertUntouched(before, f"forged stage {label}")
                self.assertEqual(
                    self.task_row(task.id)[2], LoopStage.EXECUTE.value, label
                )

    # -- 4. malformed persistence -----------------------------------------

    def test_4_a_malformed_task_document_is_refused_without_raising(self) -> None:
        documents = {
            "empty dict": "{}",
            "null": "null",
            "list": "[]",
            "string": '"a task, honestly"',
            "number": "7",
            "undecodable": "{not json at all",
            "incomplete dict": '{"id": "x", "objective": "o"}',
        }
        for label, raw in documents.items():
            with self.subTest(document=label):
                task = self.make_task()
                self.set_task_document(task.id, raw)
                before = self.database_content()
                try:
                    run = self.core.cancel(self.session.id, task.id)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{label} raised {type(exc).__name__}")
                self.assertRefused(run, label)
                self.assertUntouched(before, f"malformed task {label}")

    def test_4_a_malformed_session_document_is_refused(self) -> None:
        task = self.make_task()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                ("[]", self.session.id),
            )
            connection.commit()
        before = self.database_content()
        try:
            run = self.core.cancel(self.session.id, task.id)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"a malformed session raised {type(exc).__name__}")
        self.assertIs(run.status, RunStatus.FAILED)
        self.assertUntouched(before, "malformed session")

    # -- 5. forged session identity ----------------------------------------

    def test_5_a_forged_session_id_mutates_no_foreign_session(self) -> None:
        victim = SessionState(objective="VICTIM")
        self.memory.save_session(victim)
        task = self.make_task()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            raw = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?",
                (self.session.id,),
            ).fetchone()[0]
            document = json.loads(raw)
            document["id"] = victim.id
            document["next_step"] = "HIJACKED"
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(document), self.session.id),
            )
            connection.commit()
        before = self.database_content()
        run = self.core.cancel(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.FAILED)
        self.assertUntouched(before, "forged session id")
        stored = self.memory.load_session(victim.id)
        assert stored is not None
        self.assertNotEqual(stored.next_step, "HIJACKED")
        self.assertEqual(self.task_row(task.id)[1], TaskStatus.RUNNING.value)

    # -- 6. foreign ownership ---------------------------------------------

    def test_6_a_foreign_task_is_refused_without_disclosure(self) -> None:
        task = self.make_task()
        stranger = SessionState(objective="elsewhere")
        self.memory.save_session(stranger)
        before = self.database_content()
        run = self.core.cancel(stranger.id, task.id)
        self.assertRefused(run, "a foreign session cancelled a task")
        self.assertUntouched(before, "foreign cancellation")
        # The refusal is the SAME one an unknown id gets: it may not reveal
        # that the task exists, nor who owns it.
        missing = self.core.cancel(self.session.id, "no-such-task")
        self.assertIs(missing.status, run.status)
        assert missing.error is not None and run.error is not None
        self.assertIs(missing.error.code, run.error.code)
        self.assertNotIn(self.session.id, run.error.message)

    def test_6_an_unknown_session_is_refused(self) -> None:
        task = self.make_task()
        before = self.database_content()
        run = self.core.cancel("no-such-session", task.id)
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.UNKNOWN_SESSION)
        self.assertUntouched(before, "unknown session")

    # -- 7. ownership change race ------------------------------------------

    def test_7_an_owner_change_before_the_call_is_refused(self) -> None:
        """Ownership moved: the arbitration refuses and writes nothing."""
        task = self.make_task()
        new_owner = SessionState(objective="new owner")
        self.memory.save_session(new_owner)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?",
                (new_owner.id, task.id),
            )
            connection.commit()
        before = self.database_content()
        run = self.core.cancel(self.session.id, task.id)
        self.assertRefused(run, "a stale caller cancelled a moved task")
        self.assertUntouched(before, "cancel after an owner change")
        self.assertEqual(
            self.task_row(task.id)[0], new_owner.id, "the task was re-owned"
        )

    def test_7_a_guard_refusal_rolls_the_whole_write_back(self) -> None:
        """The last-moment loss: nothing partial survives it.

        The guarded task write is what fails closed when ownership moves under
        a transaction. Forcing it to lose proves the rest of the cancellation
        goes with it - no session pointer, no checkpoint, no event describing
        a cancellation that never reached the task.
        """
        task = self.make_task()
        before = self.database_content()

        def refuse(connection, session_id, task_arg, **kwargs):
            return False

        with mock.patch.object(
            SQLiteMemoryStore, "_upsert_task", staticmethod(refuse)
        ):
            run = self.core.cancel(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.FAILED, "a lost write reported success")
        self.assertUntouched(before, "rolled-back cancellation")
        self.assertEqual(self.task_row(task.id)[1], TaskStatus.RUNNING.value)
        self.assertEqual(self.events(task.id), [])
        stored = self.memory.load_session(self.session.id)
        assert stored is not None
        self.assertIsNot(stored.latest_task_id, task.id)
        # NON-VACUITY: the same cancellation succeeds once the guard allows it.
        self.assertIs(
            self.core.cancel(self.session.id, task.id).status, RunStatus.CANCELLED
        )

    def test_7_an_in_transaction_owner_change_loses_at_the_sql_guard(self) -> None:
        """The SQL owner guard alone: every other proof has already passed.

        Ownership is proved by the rebinding, and only THEN moves - inside the
        transaction, after arbitration, before the write. Nothing but the
        guard on the upsert can catch that, and it must fail the whole
        cancellation rather than write over the new owner.
        """
        task = self.make_task()
        new_owner = SessionState(objective="new owner")
        self.memory.save_session(new_owner)
        real_upsert_session = SQLiteMemoryStore._upsert_session

        def steal(connection, session, owner=new_owner.id, task_id=task.id):
            connection.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?", (owner, task_id)
            )
            return real_upsert_session(connection, session)

        before = self.database_content()
        with mock.patch.object(
            SQLiteMemoryStore, "_upsert_session", staticmethod(steal)
        ):
            run = self.core.cancel(self.session.id, task.id)
        self.assertIs(
            run.status, RunStatus.FAILED, "a stale write overwrote a new owner"
        )
        self.assertIsNot(run.status, RunStatus.CANCELLED)
        self.assertUntouched(before, "in-transaction owner change")
        self.assertEqual(self.task_row(task.id)[1], TaskStatus.RUNNING.value)
        self.assertEqual(self.events(task.id), [])

    # -- 8. positive controls ----------------------------------------------

    def test_8_canonical_rows_keep_their_reviewed_semantics(self) -> None:
        if self.connector.manifest.name not in self.registry.list_names():
            self.registry.register(self.connector)
        cases = {
            "no attempt": (None, RunStatus.CANCELLED, TaskStatus.CANCELLED),
            "prepared": (
                AttemptState.PREPARED, RunStatus.CANCELLED, TaskStatus.CANCELLED,
            ),
            "finished": (
                AttemptState.FINISHED, RunStatus.CANCELLED, TaskStatus.CANCELLED,
            ),
            "in flight": (
                AttemptState.IN_FLIGHT,
                RunStatus.CANCELLING,
                TaskStatus.CANCEL_REQUESTED,
            ),
        }
        for label, (state, run_status, task_status) in cases.items():
            with self.subTest(attempt=label):
                task = self.make_task(selected_connector="local")
                if state is not None:
                    self.open_attempt(task.id, state)
                self.connector.calls.clear()
                run = self.core.cancel(self.session.id, task.id)
                self.assertEqual(self.connector.calls, [], label)
                self.assertIs(run.status, run_status, label)
                self.assertEqual(run.task.id, task.id, label)
                stored = self.memory.load_task(task.id)
                assert stored is not None
                self.assertIs(stored.status, task_status, label)

    def test_8_unknown_outcome_keeps_its_reviewed_semantics(self) -> None:
        task = self.make_task(selected_connector="local")
        attempt_id = self.open_attempt(task.id, AttemptState.UNKNOWN_OUTCOME)
        self.reserve(attempt_id, task.id)
        if self.connector.manifest.name not in self.registry.list_names():
            self.registry.register(self.connector)
        self.connector.calls.clear()
        before = self.database_content()
        run = self.core.cancel(self.session.id, task.id)
        self.assertEqual(self.connector.calls, [])
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        self.assertUntouched(before, "unknown-outcome cancel")
        self.assertEqual(
            self.reservation_state(attempt_id), ReservationState.ACTIVE.value
        )

    def test_8_a_genuinely_terminal_task_is_reported_not_rewritten(self) -> None:
        task = self.make_task()
        stored = self.memory.load_task(task.id)
        assert stored is not None
        stored.status = TaskStatus.COMPLETED
        stored.stage = LoopStage.COMPLETED
        self.memory.save_task(self.session.id, stored)
        before = self.database_content()
        run = self.core.cancel(self.session.id, task.id)
        self.assertIs(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.task.id, task.id)
        self.assertUntouched(before, "terminal report")

    def test_8_repeated_in_flight_cancellation_stays_conservative(self) -> None:
        task = self.make_task()
        self.open_attempt(task.id, AttemptState.IN_FLIGHT)
        for _ in range(3):
            run = self.core.cancel(self.session.id, task.id)
            self.assertIs(run.status, RunStatus.CANCELLING)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCEL_REQUESTED)
        self.assertIs(self.attempt_state(task.id), AttemptState.IN_FLIGHT)

    # -- 9. the terminal fast path ------------------------------------------

    def test_9_a_forged_terminal_document_cannot_report_terminal(self) -> None:
        """THE FAST-PATH REPRODUCTION: core used to answer from the document."""
        for label, fields in {
            "completed": {
                "status": TaskStatus.COMPLETED.value,
                "stage": LoopStage.COMPLETED.value,
            },
            "failed": {
                "status": TaskStatus.FAILED.value,
                "stage": LoopStage.FAILED.value,
            },
            "cancelled": {
                "status": TaskStatus.CANCELLED.value,
                "stage": LoopStage.CANCELLED.value,
            },
        }.items():
            with self.subTest(forged=label):
                task = self.make_task()
                self.forge_task(task.id, **fields)
                before = self.database_content()
                run = self.core.cancel(self.session.id, task.id)
                self.assertRefused(run, label)
                self.assertUntouched(before, f"forged terminal {label}")
                self.assertEqual(
                    self.task_row(task.id)[1], TaskStatus.RUNNING.value, label
                )

    def test_9_core_holds_no_pre_storage_lifecycle_authority(self) -> None:
        """By source: nothing is judged before storage has rebound the rows."""
        source = inspect.getsource(AgentCore.cancel)
        body = source.split('"""', 2)[-1]
        arbitration = body.index("self.memory.cancel_task(")
        for premature in ("_terminal_run(", "load_task(", "is_terminal("):
            with self.subTest(call=premature):
                found = body.find(premature)
                self.assertTrue(
                    found == -1 or found > arbitration,
                    f"{premature} is consulted before storage decides",
                )

    def test_9_the_verdict_carries_the_rebound_rows(self) -> None:
        """The receipt is the rows, not what the caller brought."""
        task = self.make_task()
        outcome = self.memory.cancel_task(self.session.id, task.id)
        self.assertEqual(outcome.verdict, "cancelled")
        assert outcome.task is not None and outcome.session is not None
        self.assertEqual(outcome.task.id, task.id)
        self.assertEqual(outcome.session.id, self.session.id)
        self.assertIs(outcome.task.status, TaskStatus.CANCELLED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
