"""M12 S2B: the owner-local live stop.

Stopping live external work needs an external call, and an external call
cannot be made atomic with the storage decision authorizing it. S2A proved a
durable claim makes ACQUISITION unique without making the SIGNAL safe: a
second actor's permission can go stale between the two.

S2B dissolves that rather than guarding it. The only actor that ever signals
is the invocation that won ``begin_execution`` and is inside ``execute`` right
now, holding the connector object. "May I still signal?" is answered by that
connector's own live handle at the moment of signalling - not by a storage
read that could have aged.

These tests are about AUTHORITY, not state transitions: who may signal, which
object receives it, how many times, and what the answer is allowed to mean.
"""

from __future__ import annotations

import contextlib
import inspect
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    AttemptState,
    BillingClass,
    ErrorCode,
    ExecutionClaimOutcome,
    LoopStage,
    MonetaryState,
    OutcomeStatus,
    PermissionGrant,
    ReservationState,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    SideEffectProfile,
    Task,
    TaskStatus,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
)
from jarvis.destinations import (
    CLAUDE_CODE_DESTINATION,
    DestinationGrant,
)
from jarvis.core import AgentCore, _LiveStopWatcher
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, make_task, open_core, safe_cleanup


def _executable_source(obj) -> str:
    """The code of ``obj``, with docstrings and comments removed.

    A source-level assertion about what something may not DO must not read
    the prose explaining what it may not do. Scanning the raw text would
    make every forbidden word its own false positive.
    """
    import ast

    tree = ast.parse(inspect.getsource(obj).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


#: Tight enough that a watcher poll lands promptly inside a test, without any
#: test depending on wall-clock ordering: every race below is pinned with an
#: Event, and this only decides how soon the watcher notices.
FAST_POLL = 0.005

#: CIFLAKE: a hang guard, never a bound - each wait it caps ends the moment its Event is set.
#: An approved resume commits several SQLite transactions before execute; on a CI disk ~70x
#: slower than a desk that took longer than the old ten seconds, with nothing wrong.
HANG_GUARD = 120


def _open_in_flight_attempt(memory, database, session_id: str, task_id: str) -> str:
    """An authoritative IN_FLIGHT attempt, the way a live owner leaves one."""
    from jarvis.contracts import ExecutionAttempt

    attempt = memory.open_execution_attempt(
        ExecutionAttempt(
            session_id=session_id,
            task_id=task_id,
            connector="blocking",
            fingerprint="f" * 64,
        )
    )
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE execution_attempts SET state = ? WHERE id = ?",
            (AttemptState.IN_FLIGHT.value, attempt.id),
        )
        connection.commit()
    return attempt.id


class _BlockingConnector(BaseConnector):
    """A connector whose execute blocks until something releases it.

    Models the shape S2B exists for: the owner thread is parked inside a call
    it cannot observe from, and a live operation exists only in this object.
    """

    def __init__(self, name: str = "blocking") -> None:
        self.manifest = ConnectorManifest(
            name=name,
            capabilities=("echo",),
            priority=10,
            default_risk=RiskLevel.LOW,
            default_side_effect=SideEffectProfile(
                effect=SideEffect.NONE, reversible=True
            ),
            default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
        )
        #: Released by cancel(), or by a test that wants completion first.
        self.release = threading.Event()
        #: Set the moment execute is entered, so a test can pin "the call is
        #: live now" without sleeping.
        self.executing = threading.Event()
        #: Set when cancel() has been entered, for the same reason.
        self.signalled = threading.Event()
        self.cancel_calls: list[str] = []
        self.execute_calls = 0
        #: What cancel() does. Overridden per test.
        self.cancel_returns = True
        self.cancel_raises: Exception | None = None
        #: A connector that has not yet registered a live handle.
        self.handle_ready = True
        self.raise_on_execute: Exception | None = None
        self.outcome = OutcomeStatus.SUCCESS
        #: How long execute stays live without a release (HANG_GUARD: only a release ends it).
        self.release_ceiling = 10

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        self.execute_calls += 1
        self.executing.set()
        self.release.wait(timeout=self.release_ceiling)
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        return ToolResult(
            outcome=self.outcome,
            summary="blocking connector finished",
            usage=UsageRecord(
                connector=self.manifest.name,
                billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
                monetary_state=MonetaryState.NOT_APPLICABLE,
            ),
        )

    def canonical_outbound(self, task: Task) -> bytes:
        # A connector declaring egress must bind exact bytes, or the
        # attestation fails closed before any approval is even offered.
        return f"outbound:{task.id}".encode()

    def verify(self, task: Task, result: ToolResult, session: SessionState):
        return VerificationRecord(
            status=VerificationStatus.PASSED,
            method="blocking_connector_self_check",
            evidence=("the blocking call returned",),
        )

    def cancel(self, task_id: str) -> bool:
        self.cancel_calls.append(task_id)
        self.signalled.set()
        # Unblock execute the way a terminated subprocess would: the call
        # returns, and the owner's normal post-execute path takes over.
        self.release.set()
        if self.cancel_raises is not None:
            raise self.cancel_raises
        if not self.handle_ready:
            # The pre-handle window: nothing live to signal yet.
            return False
        return self.cancel_returns


class _ExplodingRegistry(ToolRegistry):
    """A registry that fails loudly if the watcher ever looks a connector up."""

    def __init__(self) -> None:
        super().__init__()
        self.lookups: list[str] = []
        self.armed = False

    def get(self, name: str):
        self.lookups.append(name)
        if self.armed:
            raise AssertionError(
                "the stop watcher performed a registry lookup instead of "
                f"using its captured connector instance (asked for {name!r})"
            )
        return super().get(name)


class LiveStopTestCase(unittest.TestCase):
    """A real store, real threads, a real blocking connector. No mocks of
    storage authority."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.settings = Settings(memory_database=self.database)
        self.connector = _BlockingConnector()
        self.registry = _ExplodingRegistry()
        self.registry.register(self.connector)
        self.core = open_core(
            self.registry,
            self.memory,
            self.settings,
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        self.connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )
        self.poll = mock.patch.object(
            __import__("jarvis.core", fromlist=["core"]),
            "_LIVE_STOP_POLL_SECONDS",
            FAST_POLL,
        )
        self.poll.start()
        self.addCleanup(self.poll.stop)

    # -- fixtures ------------------------------------------------------

    def run_until_executing(self) -> tuple[threading.Thread, list]:
        """Start a run on its own thread and wait until execute is entered."""
        runs: list = []

        def go() -> None:
            runs.append(self.core.run(make_task()))

        thread = threading.Thread(target=go, name="jarvis-owner")
        thread.start()
        self.assertTrue(
            self.connector.executing.wait(timeout=HANG_GUARD), "execute never started"
        )
        return thread, runs

    def running_task_id(self) -> str:
        """The one task the owner thread is executing, by authoritative row."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT task_id FROM execution_attempts "
                "WHERE state = ? ORDER BY rowid DESC LIMIT 1",
                (AttemptState.IN_FLIGHT.value,),
            ).fetchone()
        self.assertIsNotNone(row, "no IN_FLIGHT attempt to cancel")
        return row[0]

    def session_of(self, task_id: str) -> str:
        owner = self.memory.task_session_id(task_id)
        assert owner is not None
        return owner

    def request_cancel(self, task_id: str) -> None:
        self.memory.cancel_task(self.session_of(task_id), task_id)

    def finish(self, thread: threading.Thread, runs: list):
        self.connector.release.set()
        thread.join(timeout=HANG_GUARD)
        self.assertFalse(thread.is_alive(), "the owner thread never finished")
        self.assertTrue(runs, "the owner produced no run")
        return runs[0]

    def attempt_state(self, task_id: str) -> AttemptState | None:
        attempt = self.memory.latest_execution_attempt(task_id)
        return None if attempt is None else attempt.state

    def reservation_state(self, task_id: str) -> str | None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT b.state FROM attempt_billing b WHERE b.task_id = ?",
                (task_id,),
            ).fetchone()
        return None if row is None else row[0]

    def events(self, task_id: str) -> list[str]:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT event_type FROM event_log WHERE task_id = ? "
                    "ORDER BY id",
                    (task_id,),
                )
            ]

    def database_content(self) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return {
                table: list(connection.execute(f"SELECT * FROM {table}"))
                for table in (
                    "tasks", "sessions", "execution_attempts", "attempt_billing",
                    "event_log", "loop_checkpoints", "usage_ledger",
                    "permission_grants", "decision_log",
                )
            }


class LiveOwnerStopTests(LiveStopTestCase):
    """1, 10. The owner signals its own live call, and finalizes it itself."""

    def test_1_a_live_cancellation_reaches_the_executing_instance(self) -> None:
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.request_cancel(task_id)
        self.assertTrue(
            self.connector.signalled.wait(timeout=10),
            "the owner watcher never signalled its own live operation",
        )
        run = self.finish(thread, runs)
        # Exactly the object that was executing, named by the exact task.
        self.assertEqual(self.connector.cancel_calls, [task_id])
        # ...and the reviewed finalization, not something the watcher wrote.
        self.assertIs(run.status, RunStatus.CANCELLED)
        stored = self.memory.load_task(task_id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCELLED)
        self.assertIs(self.attempt_state(task_id), AttemptState.FINISHED)

    def test_1_the_watcher_writes_no_terminal_state_itself(self) -> None:
        """By source: the watcher has no lifecycle vocabulary at all."""
        body = _executable_source(_LiveStopWatcher)
        for forbidden in (
            "conclude_execution", "mark_execution_unknown", "cancel_task",
            "save_task", "save_session", "reconcile", "reservation",
            "abandon_unknown_attempt", "chain", "result_snapshot",
            "TaskStatus", "AttemptState",
        ):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, body)
        # The ONE storage call it is allowed to make.
        self.assertIn("execution_cancellation_requested", body)

    def test_1_a_pending_cancellation_beats_a_returned_failure(self) -> None:
        """The post-execute reload alone decides this one.

        A connector that returns an ERROR while a cancellation is already
        persisted must finalize as CANCELLED, not FAILED: the reload happens
        before the failure branch, so the operator's request wins over a
        result that arrived after they asked for it. conclude_execution's own
        H2-F guard never sees this case, because the failure path returns
        first.
        """
        self.connector.outcome = OutcomeStatus.ERROR
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.request_cancel(task_id)
        self.assertTrue(self.connector.signalled.wait(timeout=10))
        run = self.finish(thread, runs)
        self.assertIs(run.status, RunStatus.CANCELLED, "the failure won")
        self.assertIsNot(run.status, RunStatus.FAILED)
        stored = self.memory.load_task(task_id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCELLED)

    def test_10_the_owner_stays_the_finalizer_after_a_signal(self) -> None:
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.request_cancel(task_id)
        self.assertTrue(self.connector.signalled.wait(timeout=10))
        # Nothing about the stop is persisted while the call is still live.
        self.assertIsNot(
            self.attempt_state(task_id), AttemptState.FINISHED
        )
        run = self.finish(thread, runs)
        self.assertIs(run.status, RunStatus.CANCELLED)
        # The conclusion came from conclude_execution, not from the watcher.
        self.assertIn("execution_cancelled", self.events(task_id))



class WatcherEligibilityTests(LiveStopTestCase):
    """2, 14. A watcher belongs to one CLAIMED invocation and nothing else."""

    def test_2_no_watcher_exists_without_a_claimed_execution(self) -> None:
        """A refused claim never reaches the connector, so never watches."""
        real_begin = SQLiteMemoryStore.begin_execution
        watchers: list = []
        real_start = _LiveStopWatcher.start

        def counting_start(watcher):
            watchers.append(watcher)
            return real_start(watcher)

        def refuse(self_store, *args, **kwargs):
            return ExecutionClaimOutcome.LOST

        with mock.patch.object(
            SQLiteMemoryStore, "begin_execution", refuse
        ), mock.patch.object(_LiveStopWatcher, "start", counting_start):
            run = self.core.run(make_task())
        self.assertEqual(watchers, [], "a watcher started without a claim")
        self.assertEqual(self.connector.execute_calls, 0)
        self.assertEqual(self.connector.cancel_calls, [])
        self.assertIsNot(run.status, RunStatus.COMPLETED)
        # NON-VACUITY: the same run claims and watches when begin_execution
        # is left alone.
        self.assertIs(real_begin, SQLiteMemoryStore.begin_execution)
        with mock.patch.object(_LiveStopWatcher, "start", counting_start):
            self.connector.release.set()
            self.core.run(make_task())
        self.assertEqual(len(watchers), 1, "a claimed execution did not watch")

    def test_2_the_watcher_is_created_only_after_the_claim_succeeds(self) -> None:
        """By source: creation follows the CLAIMED gate, precedes execute."""
        source = inspect.getsource(AgentCore._advance_loop)
        claimed = source.index("if outcome is not ExecutionClaimOutcome.CLAIMED:")
        created = source.index("stop_watcher = _LiveStopWatcher(")
        executed = source.index("result = connector.execute(task, session)")
        self.assertLess(claimed, created, "the watcher precedes the claim gate")
        self.assertLess(created, executed, "the watcher starts after execute")

    def test_14_a_restart_never_watches_or_signals_old_in_flight_work(
        self,
    ) -> None:
        """14. The dead owner's handle died with it. Nobody inherits it."""
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.request_cancel(task_id)
        self.assertTrue(self.connector.signalled.wait(timeout=10))
        self.finish(thread, runs)
        # Now forge the state a dead owner leaves: an attempt still IN_FLIGHT
        # and a cancellation still requested, with nobody alive to resolve it.
        stored = self.memory.load_task(task_id)
        assert stored is not None
        stored.status = TaskStatus.CANCEL_REQUESTED
        stored.stage = LoopStage.EXECUTE
        stored.cancellation_requested = True
        self.memory.save_task(self.session_of(task_id), stored)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE execution_attempts SET state = ? WHERE task_id = ?",
                (AttemptState.IN_FLIGHT.value, task_id),
            )
            connection.commit()
        fresh_connector = _BlockingConnector()
        fresh_registry = ToolRegistry()
        fresh_registry.register(fresh_connector)
        fresh_core = open_core(
            fresh_registry,
            SQLiteMemoryStore(self.database),
            self.settings,
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        fresh_connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )
        watchers: list = []
        real_start = _LiveStopWatcher.start
        with mock.patch.object(
            _LiveStopWatcher, "start",
            lambda w: (watchers.append(w), real_start(w))[1],
        ):
            run = fresh_core.resume(self.session_of(task_id), task_id)
        self.assertEqual(watchers, [], "a restart started a watcher")
        self.assertEqual(fresh_connector.cancel_calls, [])
        self.assertEqual(fresh_connector.execute_calls, 0, "a restart replayed")
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        self.assertIs(self.attempt_state(task_id), AttemptState.IN_FLIGHT)


class ExactInstanceTests(LiveStopTestCase):
    """3. The captured object, never a lookup and never a sibling."""

    def test_3_the_watcher_never_consults_the_registry(self) -> None:
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        # From here, ANY registry lookup is the watcher's and must explode.
        self.registry.armed = True
        self.request_cancel(task_id)
        self.assertTrue(self.connector.signalled.wait(timeout=10))
        self.registry.armed = False
        self.finish(thread, runs)
        self.assertEqual(self.connector.cancel_calls, [task_id])

    def test_3_a_sibling_connector_is_never_signalled(self) -> None:
        sibling = _BlockingConnector(name="sibling")
        # Lower priority so selection still picks the connector under test:
        # this test is about which INSTANCE gets signalled, not about routing.
        sibling.manifest = replace(sibling.manifest, priority=1)
        self.registry.register(sibling)
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.request_cancel(task_id)
        self.assertTrue(self.connector.signalled.wait(timeout=10))
        self.finish(thread, runs)
        self.assertEqual(sibling.cancel_calls, [], "a sibling was signalled")
        self.assertEqual(self.connector.cancel_calls, [task_id])

    def test_3_the_captured_instance_is_the_executing_one(self) -> None:
        """By source: the watcher is handed the connector, not a name."""
        source = inspect.getsource(AgentCore._advance_loop)
        creation = source[
            source.index("stop_watcher = _LiveStopWatcher(") :
        ][:200]
        self.assertIn("connector", creation)
        self.assertNotIn("registry", creation)
        self.assertNotIn("selected_connector", creation)
        watcher_source = _executable_source(_LiveStopWatcher)
        for forbidden in (
            "registry", "selected_connector", "manifest.name", ".get(",
        ):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, watcher_source)


class AtMostOnceTests(LiveStopTestCase):
    """4, 5, 6, 7. One attempt, consumed for every outcome."""

    def test_4_repeated_cancels_produce_exactly_one_signal(self) -> None:
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        for _ in range(5):
            self.request_cancel(task_id)
        self.assertTrue(self.connector.signalled.wait(timeout=10))
        self.finish(thread, runs)
        self.assertEqual(len(self.connector.cancel_calls), 1)

    def consume_and_assert_single(self, task_id: str) -> None:
        """Poll well past the first signal; the count must not move."""
        watcher = _LiveStopWatcher(
            self.memory, self.session_of(task_id), task_id, self.connector
        )
        for _ in range(5):
            watcher._signal_once()
        self.assertEqual(len(self.connector.cancel_calls), 1)
        self.assertTrue(watcher.attempted)

    def test_5_a_true_answer_consumes_the_attempt(self) -> None:
        task_id = self._parked_task()
        self.connector.cancel_returns = True
        self.consume_and_assert_single(task_id)
        # ...and decides no lifecycle by itself.
        self.assertIsNone(self.memory.load_task(task_id).result_snapshot)

    def test_6_a_false_answer_consumes_the_attempt_and_is_not_retried(
        self,
    ) -> None:
        task_id = self._parked_task()
        self.connector.cancel_returns = False
        self.consume_and_assert_single(task_id)

    def test_7_a_raising_cancel_consumes_the_attempt_and_is_contained(
        self,
    ) -> None:
        task_id = self._parked_task()
        self.connector.cancel_raises = RuntimeError("connector is on fire")
        watcher = _LiveStopWatcher(
            self.memory, self.session_of(task_id), task_id, self.connector
        )
        try:
            for _ in range(3):
                watcher._signal_once()
        except Exception as exc:  # noqa: BLE001 - proving it does not raise
            self.fail(f"a raising cancel escaped the watcher: {exc!r}")
        self.assertEqual(len(self.connector.cancel_calls), 1)
        self.assertTrue(watcher.attempted)

    def test_7_a_raising_cancel_does_not_become_the_execution_result(
        self,
    ) -> None:
        self.connector.cancel_raises = RuntimeError("connector is on fire")
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.request_cancel(task_id)
        self.assertTrue(self.connector.signalled.wait(timeout=10))
        run = self.finish(thread, runs)
        self.assertIs(run.status, RunStatus.CANCELLED)
        if run.error is not None:
            self.assertNotIn("on fire", run.error.message)

    def test_the_attempt_is_consumed_before_the_call_not_after(self) -> None:
        """By source: a raise must not leave a second attempt available."""
        source = inspect.getsource(_LiveStopWatcher._signal_once)
        self.assertLess(
            source.index("self._attempted = True"),
            source.index("self._connector.cancel("),
            "the one-shot flag is set after the call, so a raise re-arms it",
        )

    def _parked_task(self) -> str:
        """A CANCEL_REQUESTED task with no live call, for direct probes.

        The request only reaches CANCEL_REQUESTED for an authoritative
        IN_FLIGHT attempt - anything else cancels outright - so the attempt
        is real, and only the owning process is missing.
        """
        session = SessionState(objective="parked")
        self.memory.save_session(session)
        task = Task(objective="o", capability="echo", scope=make_task().scope)
        task.status = TaskStatus.RUNNING
        task.stage = LoopStage.EXECUTE
        self.memory.save_task(session.id, task)
        _open_in_flight_attempt(self.memory, self.database, session.id, task.id)
        self.memory.cancel_task(session.id, task.id)
        assert (
            self.memory.load_task(task.id).status is TaskStatus.CANCEL_REQUESTED
        )
        return task.id


class WatcherReadFailureTests(LiveStopTestCase):
    """8. A watcher that cannot read is a watcher that does nothing."""

    def test_8_a_transient_read_failure_is_contained_and_retried(self) -> None:
        calls = {"n": 0}
        real_read = SQLiteMemoryStore.execution_cancellation_requested

        def flaky(store, session_id, task_id):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise sqlite3.OperationalError("database is locked")
            return real_read(store, session_id, task_id)

        with mock.patch.object(
            SQLiteMemoryStore, "execution_cancellation_requested", flaky
        ):
            thread, runs = self.run_until_executing()
            task_id = self.running_task_id()
            self.request_cancel(task_id)
            self.assertTrue(
                self.connector.signalled.wait(timeout=10),
                "the watcher gave up after a transient read failure",
            )
            run = self.finish(thread, runs)
        self.assertGreater(calls["n"], 3, "the watcher stopped polling")
        self.assertIs(run.status, RunStatus.CANCELLED)

    def test_8_a_permanent_read_failure_cannot_fail_the_run(self) -> None:
        def always_raises(store, session_id, task_id):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(
            SQLiteMemoryStore, "execution_cancellation_requested", always_raises
        ):
            thread, runs = self.run_until_executing()
            run = self.finish(thread, runs)
        # No signal was possible, so the run is exactly the pre-S2B outcome.
        self.assertEqual(self.connector.cancel_calls, [])
        self.assertIs(run.status, RunStatus.COMPLETED)


class RaceTests(LiveStopTestCase):
    """9, 11. Completion and the signal, pinned deterministically."""

    def test_9_completion_before_the_signal_is_arbitrated_by_storage(
        self,
    ) -> None:
        thread, runs = self.run_until_executing()
        # Completion happens FIRST; no cancellation is ever persisted, so the
        # watcher has nothing to observe and the run finishes honestly.
        self.connector.release.set()
        thread.join(timeout=20)
        run = runs[0]
        # A cancellation persisted before the owner's post-execute reload is
        # the S2A rule; here it landed after, so the run completed honestly.
        self.assertEqual(self.connector.cancel_calls, [])
        self.assertIs(run.status, RunStatus.COMPLETED)

    def test_9_a_cancellation_landing_in_the_conclude_window_still_wins(
        self,
    ) -> None:
        """The S2A arbitration, unchanged and still authoritative."""
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        real_reload = SQLiteMemoryStore.load_task
        landed = threading.Event()

        def cancel_then_load(store, wanted_id):
            if wanted_id == task_id and not landed.is_set():
                landed.set()
                store.cancel_task(self.session_of(task_id), task_id)
            return real_reload(store, wanted_id)

        with mock.patch.object(SQLiteMemoryStore, "load_task", cancel_then_load):
            self.connector.release.set()
            thread.join(timeout=20)
        run = runs[0]
        self.assertIs(run.status, RunStatus.CANCELLED)
        self.assertIs(self.memory.load_task(task_id).status, TaskStatus.CANCELLED)

    def test_11_a_simultaneous_signal_and_completion_finalize_once(self) -> None:
        """The exact race, pinned on a barrier, over many rounds.

        Each round gets its own store, connector and core rather than
        re-running setUp: re-entering a fixture while a previous round's owner
        thread may still be unwinding is how a race test becomes a flaky one.
        """
        for attempt_number in range(6):
            with self.subTest(attempt=attempt_number):
                self._one_simultaneous_round()

    def _one_simultaneous_round(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        database = Path(temp.name) / "jarvis.db"
        memory = SQLiteMemoryStore(database)
        connector = _BlockingConnector()
        registry = ToolRegistry()
        registry.register(connector)
        core = open_core(
            registry,
            memory,
            Settings(memory_database=database),
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )
        runs: list = []
        owner = threading.Thread(target=lambda: runs.append(core.run(make_task())))
        owner.start()
        self.assertTrue(connector.executing.wait(timeout=10))

        with contextlib.closing(sqlite3.connect(database)) as connection:
            task_id = connection.execute(
                "SELECT task_id FROM execution_attempts WHERE state = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (AttemptState.IN_FLIGHT.value,),
            ).fetchone()[0]
        session_id = memory.task_session_id(task_id)
        gate = threading.Barrier(2)

        def cancel_now() -> None:
            gate.wait(timeout=10)
            memory.cancel_task(session_id, task_id)

        def complete_now() -> None:
            gate.wait(timeout=10)
            connector.release.set()

        racers = [
            threading.Thread(target=cancel_now),
            threading.Thread(target=complete_now),
        ]
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join(timeout=15)
        connector.release.set()
        owner.join(timeout=20)
        self.assertFalse(owner.is_alive(), "the owner never finished")
        self.assertTrue(runs)
        run = runs[0]

        # At most one signal, and only ever to the executing instance.
        self.assertLessEqual(len(connector.cancel_calls), 1)
        for signalled in connector.cancel_calls:
            self.assertEqual(signalled, task_id)
        # Exactly one terminal outcome, and exactly one conclusion event.
        self.assertIn(run.status, (RunStatus.CANCELLED, RunStatus.COMPLETED))
        stored = memory.load_task(task_id)
        assert stored is not None
        self.assertIn(
            stored.status, (TaskStatus.CANCELLED, TaskStatus.COMPLETED)
        )
        attempt = memory.latest_execution_attempt(task_id)
        assert attempt is not None
        self.assertIs(attempt.state, AttemptState.FINISHED)
        with contextlib.closing(sqlite3.connect(database)) as connection:
            concluded = connection.execute(
                "SELECT COUNT(*) FROM event_log WHERE task_id = ? "
                "AND event_type IN ('execution_completed', "
                "'execution_cancelled')",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(concluded, 1, "the attempt finalized twice")


class PreHandleWindowTests(LiveStopTestCase):
    """12. The accepted limitation, stated as a test rather than a hope."""

    def test_12_a_signal_before_the_handle_exists_is_not_retried(self) -> None:
        self.connector.handle_ready = False
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.request_cancel(task_id)
        self.assertTrue(self.connector.signalled.wait(timeout=10))
        run = self.finish(thread, runs)
        # One attempt, spent on a connector that had nothing live to stop.
        self.assertEqual(len(self.connector.cancel_calls), 1)
        # Execution continued and the persisted request is still honoured by
        # the owner's existing post-execute arbitration.
        self.assertIs(run.status, RunStatus.CANCELLED)
        self.assertIs(self.memory.load_task(task_id).status, TaskStatus.CANCELLED)


class UnknownAfterStopTests(LiveStopTestCase):
    """13. An unknowable raise beats a stop attempt, every time."""

    def test_13_an_unknowable_raise_wins_over_the_stop_attempt(self) -> None:
        """A consumed stop attempt changes nothing about an unknown outcome.

        Driven through the real approval path, because an unknowable effect
        is exactly the kind that always requires one - and the point is that
        the stop attempt does not get to shortcut any of it.
        """
        egress = _BlockingConnector(name="egress")
        egress.manifest = replace(
            egress.manifest,
            default_side_effect=SideEffectProfile(
                effect=SideEffect.EXTERNAL_EGRESS, reversible=False
            ),
        )
        egress.raise_on_execute = RuntimeError("the network vanished")
        # The call stays live until the stop attempt releases it, never a timeout.
        egress.release_ceiling = HANG_GUARD
        registry = ToolRegistry()
        registry.register(egress)
        core = open_core(
            registry,
            self.memory,
            self.settings,
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        "egress",
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS, reversible=False)
        gated = core.run(task)
        self.assertIs(gated.status, RunStatus.AWAITING_APPROVAL)
        assert gated.permission_request is not None
        grant = PermissionGrant.for_request(gated.permission_request)

        runs: list = []
        thread = threading.Thread(
            target=lambda: runs.append(
                core.resume(gated.session.id, task.id, grant=grant)
            )
        )
        thread.start()
        executed = egress.executing.wait(timeout=HANG_GUARD)
        self.assertTrue(executed, f"execute never ran; the owner returned {runs!r}")
        # The operator asks to stop while the unknowable call is live.
        self.memory.cancel_task(gated.session.id, task.id)
        self.assertTrue(egress.signalled.wait(timeout=HANG_GUARD), "no stop attempt")
        thread.join(timeout=HANG_GUARD)
        self.assertTrue(runs, "the owner never returned")
        run = runs[0]

        # One stop attempt was made, and it decided nothing.
        self.assertEqual(len(egress.cancel_calls), 1)
        # The unknown outcome wins: no clean CANCELLED is fabricated.
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIsNot(stored.status, TaskStatus.CANCELLED)
        self.assertIs(self.attempt_state(task.id), AttemptState.UNKNOWN_OUTCOME)
        # H3 stays conservative: the exposure the call may have created stands.
        reservation = self.reservation_state(task.id)
        if reservation is not None:
            self.assertEqual(reservation, ReservationState.ACTIVE.value)


class BaseConnectorStopTests(LiveStopTestCase):
    """15. The default answer degrades to request-only, safely."""

    def test_15_a_connector_without_a_stop_degrades_to_request_only(
        self,
    ) -> None:
        class _NoStop(_BlockingConnector):
            def cancel(self, task_id: str) -> bool:
                self.cancel_calls.append(task_id)
                self.signalled.set()
                self.release.set()
                # The inherited contract: nothing to stop, and say so.
                return BaseConnector.cancel(self, task_id)

        plain = _NoStop(name="plain")
        registry = ToolRegistry()
        registry.register(plain)
        core = open_core(
            registry,
            self.memory,
            self.settings,
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        "plain",
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )
        runs: list = []
        thread = threading.Thread(target=lambda: runs.append(core.run(make_task())))
        thread.start()
        self.assertTrue(plain.executing.wait(timeout=10))
        task_id = self.running_task_id()
        self.memory.cancel_task(self.session_of(task_id), task_id)
        self.assertTrue(plain.signalled.wait(timeout=10))
        plain.release.set()
        thread.join(timeout=20)
        self.assertEqual(len(plain.cancel_calls), 1)
        self.assertIs(runs[0].status, RunStatus.CANCELLED)

    def test_15_the_base_default_is_false(self) -> None:
        class _Bare(BaseConnector):
            manifest = ConnectorManifest(name="bare", capabilities=())

            def execute(self, task, session):  # pragma: no cover
                raise AssertionError("not executed")

        self.assertIs(_Bare().cancel("any-task"), False)


class BillingChainTests(LiveStopTestCase):
    """16, 17. The watcher has no authority over money or chains."""

    def test_16_the_stop_attempt_changes_no_h3_state(self) -> None:
        task_id = self._reserved_task()
        before = self.reservation_state(task_id)
        watcher = _LiveStopWatcher(
            self.memory, self.session_of(task_id), task_id, self.connector
        )
        watcher._signal_once()
        self.assertEqual(self.reservation_state(task_id), before)
        self.assertEqual(before, ReservationState.ACTIVE.value)

    def test_16_a_full_stopped_run_leaves_billing_to_the_owner(self) -> None:
        """After a stopped run, billing is whatever EXECUTION produced.

        Deliberately asserted after the owner has finished rather than mid
        race: a snapshot taken while the owner is still unwinding proves
        nothing about who wrote what, and would only be racing the very
        thread that legitimately owns the ledger.
        """
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.request_cancel(task_id)
        self.assertTrue(self.connector.signalled.wait(timeout=10))
        run = self.finish(thread, runs)
        self.assertIs(run.status, RunStatus.CANCELLED)
        # A free route spends nothing, and the stop attempt added nothing.
        self.assertEqual(self.memory.monthly_monetary_cost(), 0.0)
        # No reservation was released or reopened by anyone.
        reservation = self.reservation_state(task_id)
        self.assertIn(
            reservation, (None, ReservationState.RECONCILED.value)
        )

    def test_17_the_watcher_writes_no_chain_state(self) -> None:
        body = _executable_source(_LiveStopWatcher).lower()
        for forbidden in ("chain", "cursor", "successor", "binding"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, body)

    def _reserved_task(self) -> str:
        session = SessionState(objective="reserved")
        self.memory.save_session(session)
        task = Task(objective="o", capability="echo", scope=make_task().scope)
        task.status = TaskStatus.RUNNING
        task.stage = LoopStage.EXECUTE
        self.memory.save_task(session.id, task)
        from jarvis.contracts import ExecutionAttempt

        attempt = self.memory.open_execution_attempt(
            ExecutionAttempt(
                session_id=session.id,
                task_id=task.id,
                connector="blocking",
                fingerprint="f" * 64,
            )
        )
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE execution_attempts SET state = ? WHERE id = ?",
                (AttemptState.IN_FLIGHT.value, attempt.id),
            )
            connection.execute(
                "INSERT INTO attempt_billing (attempt_id, usage_id, session_id, "
                "task_id, ledger_month, billing_class, reserved_nano_usd, state, "
                "actual_nano_usd, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt.id, f"u-{attempt.id}", session.id, task.id, "2026-08",
                    BillingClass.METERED_MONETARY.value, 500,
                    ReservationState.ACTIVE.value, None,
                    "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00",
                ),
            )
            connection.commit()
        self.memory.cancel_task(session.id, task.id)
        return task.id


class WatcherJoinTests(LiveStopTestCase):
    """18. The watcher never outlives the call it belongs to."""

    def test_18_the_watcher_is_joined_at_the_execute_boundary(self) -> None:
        captured: list[_LiveStopWatcher] = []
        real_start = _LiveStopWatcher.start

        def capture(watcher):
            captured.append(watcher)
            return real_start(watcher)

        with mock.patch.object(_LiveStopWatcher, "start", capture):
            thread, runs = self.run_until_executing()
            self.finish(thread, runs)
        self.assertEqual(len(captured), 1)
        self.assertFalse(
            captured[0].is_alive(), "the watcher outlived its execution"
        )

    def test_18_no_late_signal_reaches_the_connector(self) -> None:
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.finish(thread, runs)
        # The task is terminal and the watcher is gone; a cancellation now
        # must reach nothing at all.
        self.memory.cancel_task(self.session_of(task_id), task_id)
        self.assertEqual(self.connector.cancel_calls, [])

    def test_18_the_owner_joins_in_a_finally(self) -> None:
        """By source: a raising connector still joins its watcher."""
        source = inspect.getsource(AgentCore._advance_loop)
        executed = source.index("result = connector.execute(task, session)")
        joined = source.index("stop_watcher.stop()")
        self.assertLess(executed, joined)
        tail = source[executed:joined]
        self.assertIn("finally:", tail, "the join is not on the failure path")


class StorageReadContractTests(unittest.TestCase):
    """19. The narrow read answers from columns, and nothing else."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.session = SessionState(objective="reads")
        self.memory.save_session(self.session)

    def make_task(self, status: TaskStatus = TaskStatus.RUNNING) -> Task:
        task = Task(objective="o", capability="echo", scope=make_task().scope)
        task.status = status
        task.stage = LoopStage.EXECUTE
        self.memory.save_task(self.session.id, task)
        # An IN_FLIGHT attempt is what makes a cancellation land as
        # CANCEL_REQUESTED rather than terminalizing on the spot.
        _open_in_flight_attempt(
            self.memory, self.database, self.session.id, task.id
        )
        return task

    def test_19_exact_pair_and_cancel_requested_is_true(self) -> None:
        task = self.make_task()
        self.memory.cancel_task(self.session.id, task.id)
        self.assertIs(
            self.memory.execution_cancellation_requested(
                self.session.id, task.id
            ),
            True,
        )

    def test_19_a_foreign_session_reads_false(self) -> None:
        task = self.make_task()
        self.memory.cancel_task(self.session.id, task.id)
        stranger = SessionState(objective="elsewhere")
        self.memory.save_session(stranger)
        self.assertIs(
            self.memory.execution_cancellation_requested(stranger.id, task.id),
            False,
        )

    def test_19_every_other_status_reads_false(self) -> None:
        for status in TaskStatus:
            if status is TaskStatus.CANCEL_REQUESTED:
                continue
            with self.subTest(status=status.value):
                task = self.make_task()
                with contextlib.closing(sqlite3.connect(self.database)) as db:
                    db.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?",
                        (status.value, task.id),
                    )
                    db.commit()
                self.assertIs(
                    self.memory.execution_cancellation_requested(
                        self.session.id, task.id
                    ),
                    False,
                    status.value,
                )

    def test_19_a_missing_task_reads_false(self) -> None:
        self.assertIs(
            self.memory.execution_cancellation_requested(
                self.session.id, "no-such-task"
            ),
            False,
        )

    def test_19_a_corrupt_document_changes_nothing(self) -> None:
        """The columns are the answer; state_json is never opened."""
        task = self.make_task()
        self.memory.cancel_task(self.session.id, task.id)
        for raw in ("{not json", "null", "[]", '{"id": "somebody-else"}', ""):
            with self.subTest(document=raw):
                with contextlib.closing(sqlite3.connect(self.database)) as db:
                    db.execute(
                        "UPDATE tasks SET state_json = ? WHERE id = ?",
                        (raw, task.id),
                    )
                    db.commit()
                try:
                    answer = self.memory.execution_cancellation_requested(
                        self.session.id, task.id
                    )
                except Exception as exc:  # noqa: BLE001 - proving it does not raise
                    self.fail(f"a corrupt document raised {type(exc).__name__}")
                self.assertIs(answer, True, raw)

    def test_19_the_read_deserializes_nothing_and_writes_nothing(self) -> None:
        source = inspect.getsource(
            SQLiteMemoryStore.execution_cancellation_requested
        )
        body = source.split('"""', 2)[-1]
        for forbidden in (
            "state_json", "json.loads", "load_task", "from_dict",
            "task_summary", "INSERT", "UPDATE", "DELETE", "write=True",
        ):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, body)

    def test_19_repeated_reads_write_nothing(self) -> None:
        task = self.make_task()
        self.memory.cancel_task(self.session.id, task.id)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            before = {
                table: list(db.execute(f"SELECT * FROM {table}"))
                for table in ("tasks", "sessions", "event_log")
            }
        for _ in range(10):
            self.memory.execution_cancellation_requested(self.session.id, task.id)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            after = {
                table: list(db.execute(f"SELECT * FROM {table}"))
                for table in ("tasks", "sessions", "event_log")
            }
        self.assertEqual(before, after)


class AuthorityBarrierTests(LiveStopTestCase):
    """stop() is a barrier, not a request.

    A timed join proves nothing about authority. A watcher parked in a slow
    read can wake after the owner has finalized, and a connector call already
    in flight can still be crossing the boundary while the owner concludes.
    Both are closed by one lock: authority is revoked under it, and the
    external call is made while holding it.
    """

    def watcher_for(self, memory, connector) -> _LiveStopWatcher:
        watcher = _LiveStopWatcher(memory, "session", "task", connector)
        self.addCleanup(watcher.stop)
        return watcher

    # -- 1. a read that finishes after the barrier ------------------------

    def test_1_a_late_read_cannot_signal_after_revocation(self) -> None:
        """THE P1 REPRODUCTION: the parked read used to signal afterwards."""
        entered = threading.Event()
        release = threading.Event()

        class ParkedMemory:
            def execution_cancellation_requested(self, session_id, task_id):
                entered.set()
                release.wait(timeout=20)
                # It WOULD have said yes - and that is exactly the point.
                return True

        connector = _BlockingConnector()
        watcher = self.watcher_for(ParkedMemory(), connector)
        watcher.start()
        self.assertTrue(entered.wait(timeout=10), "the read never parked")

        # The owner reaches the barrier and finalizes while the read is stuck.
        watcher.stop()
        self.assertTrue(watcher.revoked, "authority was not revoked")
        self.assertEqual(connector.cancel_calls, [])

        # Now the read completes, answering True, long after the fact.
        release.set()
        # The watcher is given every chance to act on it.
        for _ in range(50):
            if connector.cancel_calls:
                break
            time.sleep(0.01)
        self.assertEqual(
            connector.cancel_calls, [], "a late read signalled after revocation"
        )

    def test_1_revocation_not_thread_death_is_what_refuses(self) -> None:
        """Proven without any thread at all: the flag alone refuses."""
        connector = _BlockingConnector()
        watcher = self.watcher_for(_ReadyMemory(), connector)
        watcher.stop()
        # No polling ever happened; the watcher is simply revoked. A direct
        # signal attempt - the exact thing a woken read would do - refuses.
        watcher._signal_once()
        self.assertEqual(connector.cancel_calls, [])
        self.assertFalse(watcher.attempted, "a revoked attempt was consumed")

    def test_1_revocation_is_permanent(self) -> None:
        connector = _BlockingConnector()
        watcher = self.watcher_for(_ReadyMemory(), connector)
        watcher.stop()
        for _ in range(3):
            watcher.stop()
            watcher._signal_once()
        self.assertTrue(watcher.revoked)
        self.assertEqual(connector.cancel_calls, [])

    # -- 2. a connector call still crossing the boundary -------------------

    def test_2_stop_waits_for_an_in_flight_connector_cancel(self) -> None:
        """THE P1 REPRODUCTION: the owner used to walk past a live cancel.

        Deliberately driven WITHOUT starting the watcher thread. If a thread
        existed, ``stop``'s join would block on it too, and the test could not
        tell a lock that waits from a join that happens to. With no thread to
        join, only the signal lock can hold the barrier - which is the
        property the brief requires, and the one a join timeout must never be
        standing in for.
        """
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        class SlowStop(_BlockingConnector):
            def cancel(self, task_id: str) -> bool:
                self.cancel_calls.append(task_id)
                entered.set()
                release.wait(timeout=20)
                finished.set()
                return True

        connector = SlowStop()
        watcher = self.watcher_for(_ReadyMemory(), connector)
        threading.Thread(target=watcher._signal_once).start()
        self.assertTrue(entered.wait(timeout=10), "cancel never entered")
        self.assertFalse(watcher.is_alive(), "the watcher thread was started")

        returned = threading.Event()
        threading.Thread(target=lambda: (watcher.stop(), returned.set())).start()
        # The owner is at the barrier. It must NOT be past it.
        self.assertFalse(
            returned.wait(timeout=1.0),
            "the owner crossed the barrier while cancel was still running",
        )
        self.assertFalse(finished.is_set())

        release.set()
        self.assertTrue(returned.wait(timeout=15), "the barrier never released")
        # The external call had finished before the barrier let the owner past.
        self.assertTrue(finished.is_set())
        self.assertTrue(watcher.revoked)

    def test_2_the_owner_writes_no_lifecycle_while_cancel_is_active(
        self,
    ) -> None:
        """End to end: no conclusion, no UNKNOWN, no terminal task write."""
        entered = threading.Event()
        release = threading.Event()
        connector = self.connector
        real_cancel = connector.cancel

        def slow_cancel(task_id: str) -> bool:
            entered.set()
            release.wait(timeout=20)
            return real_cancel(task_id)

        connector.cancel = slow_cancel  # type: ignore[method-assign]
        thread, runs = self.run_until_executing()
        task_id = self.running_task_id()
        self.request_cancel(task_id)
        self.assertTrue(entered.wait(timeout=10), "cancel never entered")
        # Let execute be ready to return; the barrier still holds the owner.
        connector.release.set()
        time.sleep(0.5)
        # Pre-execution loop events legitimately exist; what must NOT exist is
        # any post-execute conclusion while the stop call is still running.
        for concluded in (
            "execution_completed", "execution_cancelled", "execution_abandoned",
        ):
            with self.subTest(event=concluded):
                self.assertNotIn(
                    concluded, self.events(task_id), "lifecycle moved on"
                )
        stored = self.memory.load_task(task_id)
        assert stored is not None
        self.assertIsNot(stored.status, TaskStatus.CANCELLED)
        self.assertIs(self.attempt_state(task_id), AttemptState.IN_FLIGHT)
        # Only now may the owner proceed.
        release.set()
        run = self.finish(thread, runs)
        self.assertIs(run.status, RunStatus.CANCELLED)
        self.assertIn("execution_cancelled", self.events(task_id))

    def test_2_the_barrier_does_not_depend_on_the_join_bound(self) -> None:
        """By source: the wait is the lock, and the join is only cleanup."""
        body = _executable_source(_LiveStopWatcher.stop)
        lock_wait = body.index("self._signal_lock")
        joined = body.index("join")
        self.assertLess(
            lock_wait, joined, "the join happens before authority is revoked"
        )
        self.assertIn("self._revoked = True", body)
        # The external call is made while HOLDING the lock, which is what
        # makes acquiring it a wait for that call to finish.
        signal_body = _executable_source(_LiveStopWatcher._signal_once)
        self.assertLess(
            signal_body.index("self._signal_lock"),
            signal_body.index("self._connector.cancel("),
        )

    # -- 3. ordering at the execute boundary -------------------------------

    def test_3_unknown_is_persisted_only_after_the_barrier(self) -> None:
        """THE P1 REPRODUCTION: UNKNOWN used to be written first."""
        order: list[str] = []
        egress = _BlockingConnector(name="egress")
        egress.manifest = replace(
            egress.manifest,
            default_side_effect=SideEffectProfile(
                effect=SideEffect.EXTERNAL_EGRESS, reversible=False
            ),
        )
        egress.raise_on_execute = RuntimeError("the network vanished")
        registry = ToolRegistry()
        registry.register(egress)
        core = open_core(
            registry,
            self.memory,
            self.settings,
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        "egress",
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS, reversible=False)
        gated = core.run(task)
        self.assertIs(gated.status, RunStatus.AWAITING_APPROVAL)
        assert gated.permission_request is not None
        grant = PermissionGrant.for_request(gated.permission_request)

        real_stop = _LiveStopWatcher.stop
        real_unknown = SQLiteMemoryStore.mark_execution_unknown

        def traced_stop(watcher):
            order.append("barrier")
            return real_stop(watcher)

        def traced_unknown(store, attempt_id):
            order.append("unknown")
            return real_unknown(store, attempt_id)

        with mock.patch.object(
            _LiveStopWatcher, "stop", traced_stop
        ), mock.patch.object(
            SQLiteMemoryStore, "mark_execution_unknown", traced_unknown
        ):
            runs: list = []
            thread = threading.Thread(
                target=lambda: runs.append(
                    core.resume(gated.session.id, task.id, grant=grant)
                )
            )
            thread.start()
            self.assertTrue(egress.executing.wait(timeout=HANG_GUARD))
            egress.release.set()
            thread.join(timeout=HANG_GUARD)

        self.assertEqual(order[:2], ["barrier", "unknown"], f"order: {order}")
        run = runs[0]
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)

    def test_3_a_read_released_after_unknown_cannot_signal(self) -> None:
        """The same window, from the watcher's side."""
        entered = threading.Event()
        release = threading.Event()

        class ParkedMemory:
            def execution_cancellation_requested(self, session_id, task_id):
                entered.set()
                release.wait(timeout=20)
                return True

        connector = _BlockingConnector()
        watcher = self.watcher_for(ParkedMemory(), connector)
        watcher.start()
        self.assertTrue(entered.wait(timeout=10))
        # The owner barriers, then persists UNKNOWN for a real attempt.
        watcher.stop()
        session = SessionState(objective="unknown ordering")
        self.memory.save_session(session)
        task = Task(objective="o", capability="echo", scope=make_task().scope)
        task.status, task.stage = TaskStatus.RUNNING, LoopStage.EXECUTE
        self.memory.save_task(session.id, task)
        attempt_id = _open_in_flight_attempt(
            self.memory, self.database, session.id, task.id
        )
        self.memory.mark_execution_unknown(attempt_id)
        release.set()
        for _ in range(50):
            if connector.cancel_calls:
                break
            time.sleep(0.01)
        self.assertEqual(connector.cancel_calls, [])
        attempt = self.memory.latest_execution_attempt(task.id)
        assert attempt is not None
        self.assertIs(attempt.state, AttemptState.UNKNOWN_OUTCOME)

    def test_3_the_barrier_precedes_every_lifecycle_call(self) -> None:
        """By source: nothing is processed between execute and the barrier."""
        source = inspect.getsource(AgentCore._advance_loop)
        barrier = source.index("stop_watcher.stop()")
        for later in (
            "self.memory.mark_execution_unknown(attempt.id)",
            "self._reconcile_usage(",
            "self.memory.conclude_execution(",
            "self._bind_local_read(",
        ):
            with self.subTest(call=later):
                self.assertLess(
                    barrier,
                    source.index(later),
                    f"{later} runs before the watcher barrier",
                )
        # The post-execute reload specifically: the same call also appears in
        # the pre-execute claim-failure branch, which is not what this means.
        self.assertLess(
            barrier,
            source.rindex("fresh = self.memory.load_task(task.id)"),
            "the post-execute reload runs before the watcher barrier",
        )
        # ...and the barrier is on the failure path too.
        executed = source.index("result = connector.execute(task, session)")
        self.assertIn("finally:", source[executed:barrier])

    # -- 6. a raising cancel still finishes before the owner moves ---------

    def test_6_a_raising_cancel_is_awaited_before_lifecycle(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        left = threading.Event()

        class RaisingSlowStop(_BlockingConnector):
            def cancel(self, task_id: str) -> bool:
                self.cancel_calls.append(task_id)
                entered.set()
                release.wait(timeout=20)
                left.set()
                raise RuntimeError("connector is on fire")

        connector = RaisingSlowStop()
        watcher = self.watcher_for(_ReadyMemory(), connector)
        watcher.start()
        self.assertTrue(entered.wait(timeout=10))
        returned = threading.Event()
        threading.Thread(target=lambda: (watcher.stop(), returned.set())).start()
        self.assertFalse(returned.wait(timeout=1.0), "the owner did not wait")
        release.set()
        self.assertTrue(returned.wait(timeout=15))
        self.assertTrue(left.is_set(), "the barrier released before the raise")
        self.assertEqual(len(connector.cancel_calls), 1)
        self.assertIsInstance(watcher.last_stop_error, RuntimeError)


class _ReadyMemory:
    """Storage that always reports a cancellation, instantly."""

    def execution_cancellation_requested(self, session_id: str, task_id: str) -> bool:
        return True


class S2BBoundaryTests(unittest.TestCase):
    """The deferred design space stayed deferred."""

    def test_no_lease_heartbeat_or_orphan_machinery_appeared(self) -> None:
        """No S2B EXECUTION-ATTEMPT lease, heartbeat or orphan sweep exists.

        Scheduler phase 2A added a bounded lease, but it leases a job
        GENERATION rather than an execution attempt, so the needles below
        name attempt machinery specifically. The separation is proved from
        the durable schema in
        ``test_recovery.test_no_execution_attempt_is_ever_leased_or_reclaimed``.
        """
        for module in (SQLiteMemoryStore, AgentCore, _LiveStopWatcher):
            source = inspect.getsource(module)
            for forbidden in (
                "def heartbeat", "heartbeat_at", "attempt_lease",
                "declare_attempt_unknown", "orphan_recovery", "auto_reclaim",
                "runner_owner", "cancellation_claims",
            ):
                with self.subTest(module=module.__name__, symbol=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_ordinary_cancel_still_signals_no_connector(self) -> None:
        source = inspect.getsource(AgentCore.cancel)
        for forbidden in ("self.registry", "_LiveStopWatcher", "selected_connector"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
