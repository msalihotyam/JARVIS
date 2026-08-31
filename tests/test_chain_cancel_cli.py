"""M12 S3: the chain-cancel command surface.

The authority already existed - ``cancel_chain`` has decided since M11 what a
chain cancellation may do, and every materialize and advance already refuses
on a chain that is not ACTIVE. What was missing was a way to ask for it.

So these tests are almost entirely about what the SURFACE does not do: it
decides nothing, writes nothing itself, terminalizes no running task, starts
no successor, touches no money and builds no connector. The one thing it may
do is hand two ids to storage and report the verdict that comes back.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.app import chain_cancel_result
from jarvis.cli import main as cli_main
from jarvis.contracts import (
    CHAIN_NO_PREDECESSOR,
    LOCAL_READ_CAPABILITY,
    LOCAL_WRITE_CAPABILITY,
    ActionScope,
    AttemptState,
    ChainBinding,
    ChainStatus,
    ExecutionAttempt,
    LoopStage,
    SessionState,
    Task,
    TaskStatus,
    chain_steps_from_declaration,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import Settings
from tests.helpers import safe_cleanup

EXIT_OK = 0
EXIT_INVALID_INPUT = 64
EXIT_NOT_FOUND = 66

#: The canonical three-step declaration the M11 suite already uses. Reused
#: verbatim rather than re-invented: the grammar is strict, and a chain the
#: real suite would not accept would make these tests about the wrong thing.
DECLARATION = [
    {
        "objective": "Read the project notes.",
        "capability": LOCAL_READ_CAPABILITY,
        "action": "read",
        "target": "notes.txt",
        "destination": "",
        "write_mode": "",
        "consumes": CHAIN_NO_PREDECESSOR,
    },
    {
        "objective": "Summarize the notes in three bullet points.",
        "capability": "code_analysis",
        "action": "analyze",
        "target": "",
        "destination": "anthropic/claude-code",
        "write_mode": "",
        "consumes": 0,
    },
    {
        "objective": "Save the summary as a new file.",
        "capability": LOCAL_WRITE_CAPABILITY,
        "action": "write",
        "target": "summary.md",
        "destination": "",
        "write_mode": "create",
        "consumes": 1,
    },
]


class ChainCancelTestCase(unittest.TestCase):
    """A real store and a real declared chain. No core, no registry."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.settings = Settings(memory_database=self.database)
        self.session = SessionState(objective="chain cancel")
        self.memory.save_session(self.session)
        self.declare()

    # -- fixtures ------------------------------------------------------

    def declare(self, count: int = 3) -> None:
        from jarvis.contracts import ChainState

        steps = chain_steps_from_declaration([dict(s) for s in DECLARATION[:count]])
        chain = ChainState.declared(steps)
        assert chain is not None
        self.assertEqual(
            self.memory.declare_chain_if_absent(self.session.id, chain), "declared"
        )

    def chain_now(self, session_id: str | None = None):
        stored = self.memory.load_session(session_id or self.session.id)
        assert stored is not None
        return stored.chain

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(argv, settings=self.settings)
        return code, buffer.getvalue()

    def cancel_argv(self, session_id: str | None = None) -> list[str]:
        return [
            "chain",
            "cancel",
            "--session-id",
            session_id or self.session.id,
        ]

    def cancel(self, session_id: str | None = None):
        return chain_cancel_result(
            self.memory, self.settings, session_id or self.session.id
        )

    def database_content(self) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return {
                table: list(connection.execute(f"SELECT * FROM {table}"))
                for table in (
                    "sessions", "tasks", "execution_attempts", "attempt_billing",
                    "event_log", "loop_checkpoints", "usage_ledger",
                    "permission_grants", "decision_log",
                )
            }

    def assertUntouched(self, before: dict, label: str = "") -> None:
        self.assertEqual(before, self.database_content(), label)

    def step_task(self, index: int, **fields: object) -> Task:
        """A live chain step, created through the REAL materialization path.

        Bound into ``step_task_ids`` and pointed at by the session, because a
        fixture that only writes a loose task would leave the chain looking
        like nothing had started - and then a command that wrongly reached
        for the running task would have nothing to reach for.
        """
        chain = self.chain_now()
        assert chain is not None
        # Derived through the real builder: materialization refuses a task
        # that does not match its declared step, so a hand-written one would
        # only prove the fixture wrong.
        session = self.memory.load_session(self.session.id)
        assert session is not None
        task, reason = self.core().build_chain_step_task(session, chain, index)
        self.assertIsNone(reason, f"derivation refused: {reason}")
        assert task is not None
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain.id, chain.digest, index, task
        )
        self.assertEqual(verdict, "created", "the fixture did not materialize")
        stored = self.memory.load_task(task_id)
        assert stored is not None
        stored.status = TaskStatus.RUNNING
        stored.stage = LoopStage.EXECUTE
        for key, value in fields.items():
            setattr(stored, key, value)
        self.memory.save_task(self.session.id, stored)
        session = self.memory.load_session(self.session.id)
        assert session is not None
        session.active_task_id = task_id
        session.latest_task_id = task_id
        self.memory.save_session(session)
        return stored

    def core(self):
        """An AgentCore used ONLY to derive fixture tasks, never to run one."""
        from jarvis.core import AgentCore
        from jarvis.registry import ToolRegistry

        return AgentCore(
            memory=self.memory, registry=ToolRegistry(), settings=self.settings
        )

    def in_flight_attempt(self, task_id: str) -> str:
        attempt = self.memory.open_execution_attempt(
            ExecutionAttempt(
                session_id=self.session.id,
                task_id=task_id,
                connector="local",
                fingerprint="f" * 64,
            )
        )
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE execution_attempts SET state = ? WHERE id = ?",
                (AttemptState.IN_FLIGHT.value, attempt.id),
            )
            connection.commit()
        return attempt.id

    def attempt_state(self, task_id: str) -> AttemptState | None:
        attempt = self.memory.latest_execution_attempt(task_id)
        return None if attempt is None else attempt.state


class ActiveChainCancelTests(ChainCancelTestCase):
    """1, 9. The chain stops; nothing else moves."""

    def test_1_an_active_chain_is_cancelled_through_existing_authority(
        self,
    ) -> None:
        result = self.cancel()
        self.assertEqual(result.exit_code, EXIT_OK)
        chain = self.chain_now()
        assert chain is not None
        self.assertIs(chain.status, ChainStatus.CANCELLED)
        self.assertEqual(result.payload["chain_status"], "cancelled")
        self.assertIs(result.payload["chain_cancelled"], True)

    def test_1_the_surface_delegates_rather_than_deciding(self) -> None:
        """The verdict comes from storage; this layer only asks."""
        seen: list[tuple[str, str]] = []
        real = SQLiteMemoryStore.cancel_chain

        def traced(store, session_id, chain_id):
            seen.append((session_id, chain_id))
            return real(store, session_id, chain_id)

        chain = self.chain_now()
        assert chain is not None
        with mock.patch.object(SQLiteMemoryStore, "cancel_chain", traced):
            self.cancel()
        self.assertEqual(seen, [(self.session.id, chain.id)])

    def test_1_the_surface_writes_nothing_itself(self) -> None:
        """By source: no direct chain or task write exists here."""
        body = inspect.getsource(chain_cancel_result).split('"""', 2)[-1]
        for forbidden in (
            "save_session(", "save_task(", "_upsert", "session.chain =",
            "cancel_task(", "conclude_execution(", "mark_execution_unknown(",
            "registry.", "AgentCore(", "connector.", "reservation(",
            "reconcile_", "consume_grant",
        ):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, body)
        # The ONE authority it is allowed to invoke.
        self.assertIn("memory.cancel_chain(", body)

    def test_9_no_successor_can_be_materialized_afterwards(self) -> None:
        """9. Suppression is the existing precondition, not a new write."""
        chain = self.chain_now()
        assert chain is not None
        self.assertEqual(self.cancel().exit_code, EXIT_OK)
        task = Task(
            objective="the successor",
            capability="file_read",
            scope=ActionScope(action="read", target="notes.txt"),
            chain_binding=ChainBinding(chain.id, chain.digest, 0),
        )
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain.id, chain.digest, 0, task
        )
        self.assertEqual(verdict, "inactive")
        self.assertEqual(task_id, "")
        self.assertIsNone(self.memory.load_task(task.id))
        # ...and no advance either.
        self.assertEqual(
            self.memory.advance_chain_step(
                self.session.id, chain.id, chain.digest, 0
            ),
            "inactive",
        )

    def test_9_the_cursor_and_bound_ids_are_preserved(self) -> None:
        """Cancellation stops what has not started; it undoes nothing."""
        before = self.chain_now()
        assert before is not None
        self.cancel()
        after = self.chain_now()
        assert after is not None
        self.assertEqual(after.cursor, before.cursor)
        self.assertEqual(after.step_task_ids, before.step_task_ids)
        self.assertEqual(after.id, before.id)
        self.assertEqual(after.digest, before.digest)


class RepeatedCancelTests(ChainCancelTestCase):
    """2, 6. Repeating it is safe, and finished work stays finished."""

    def test_2_a_second_cancel_is_refused_and_writes_nothing(self) -> None:
        self.assertEqual(self.cancel().exit_code, EXIT_OK)
        before = self.database_content()
        for _ in range(3):
            repeat = self.cancel()
            self.assertEqual(repeat.exit_code, EXIT_INVALID_INPUT)
            self.assertIsNotNone(repeat.payload["error"])
        self.assertUntouched(before, "repeated chain cancel")
        chain = self.chain_now()
        assert chain is not None
        self.assertIs(chain.status, ChainStatus.CANCELLED)

    def test_6_a_terminal_chain_is_never_rewritten(self) -> None:
        """A chain that is no longer ACTIVE is refused and left exactly as is.

        The status is forced through the persisted document, so an exact
        terminal value may or may not survive the chain grammar - which is
        not what this test is about. What must hold either way: the command
        refuses, writes nothing, and does not move the chain one step closer
        to ACTIVE.
        """
        for status in (
            ChainStatus.COMPLETED, ChainStatus.FAILED, ChainStatus.CANCELLED,
        ):
            with self.subTest(status=status.value):
                self.setUp()
                self.force_chain_status(status)
                was = self.chain_now()
                assert was is not None
                self.assertIsNot(was.status, ChainStatus.ACTIVE, status.value)
                before = self.database_content()
                result = self.cancel()
                self.assertEqual(result.exit_code, EXIT_INVALID_INPUT)
                self.assertUntouched(before, status.value)
                chain = self.chain_now()
                assert chain is not None
                self.assertIs(chain.status, was.status, "the chain moved")

    def force_chain_status(self, status: ChainStatus) -> None:
        stored = self.memory.load_session(self.session.id)
        assert stored is not None and stored.chain is not None
        from dataclasses import replace as _replace

        document = json.loads(
            json.dumps(
                _replace(stored, chain=_replace(stored.chain, status=status)).to_dict()
            )
        )
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(document), self.session.id),
            )
            connection.commit()


class RefusalTests(ChainCancelTestCase):
    """3, 4, 5. Missing, foreign and malformed authority all fail closed."""

    def test_3_a_missing_session_is_refused(self) -> None:
        before = self.database_content()
        result = self.cancel("no-such-session")
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)
        self.assertEqual(result.payload["error"]["code"], "unknown_session")
        self.assertUntouched(before, "missing session")

    def test_4_a_foreign_session_cannot_cancel_another_chain(self) -> None:
        stranger = SessionState(objective="elsewhere")
        self.memory.save_session(stranger)
        before = self.database_content()
        result = self.cancel(stranger.id)
        # The stranger owns no chain of its own, and cannot reach this one.
        self.assertEqual(result.exit_code, EXIT_INVALID_INPUT)
        self.assertUntouched(before, "foreign cancel")
        chain = self.chain_now()
        assert chain is not None
        self.assertIs(chain.status, ChainStatus.ACTIVE, "a foreign session won")

    def test_4_a_stranger_owning_its_own_chain_touches_only_that_one(
        self,
    ) -> None:
        stranger = SessionState(objective="elsewhere")
        self.memory.save_session(stranger)
        from jarvis.contracts import ChainState

        steps = chain_steps_from_declaration([dict(DECLARATION[0])])
        other = ChainState.declared(steps)
        assert other is not None
        self.assertEqual(
            self.memory.declare_chain_if_absent(stranger.id, other), "declared"
        )
        self.assertEqual(self.cancel(stranger.id).exit_code, EXIT_OK)
        mine = self.chain_now()
        assert mine is not None
        self.assertIs(mine.status, ChainStatus.ACTIVE, "the wrong chain was hit")
        theirs = self.chain_now(stranger.id)
        assert theirs is not None
        self.assertIs(theirs.status, ChainStatus.CANCELLED)

    def test_5_a_malformed_session_row_is_refused_without_raising(self) -> None:
        documents = {
            "undecodable": "{not json at all",
            "null": "null",
            "list": "[]",
            "string": '"a session, honestly"',
            "empty dict": "{}",
        }
        for label, raw in documents.items():
            with self.subTest(document=label):
                self.setUp()
                with contextlib.closing(sqlite3.connect(self.database)) as db:
                    db.execute(
                        "UPDATE sessions SET state_json = ? WHERE id = ?",
                        (raw, self.session.id),
                    )
                    db.commit()
                before = self.database_content()
                try:
                    result = self.cancel()
                except Exception as exc:  # noqa: BLE001 - proving it does not raise
                    self.fail(f"{label} raised {type(exc).__name__}")
                self.assertIn(
                    result.exit_code, (EXIT_NOT_FOUND, EXIT_INVALID_INPUT), label
                )
                self.assertIsNotNone(result.payload["error"], label)
                self.assertUntouched(before, f"malformed session {label}")

    def test_5_a_malformed_chain_is_refused_without_raising(self) -> None:
        stored = self.memory.load_session(self.session.id)
        assert stored is not None
        document = json.loads(json.dumps(stored.to_dict()))
        for label, forged in {
            "chain is a string": "not-a-chain",
            "chain is a list": [],
            "chain has no id": {"status": "active"},
        }.items():
            with self.subTest(chain=label):
                self.setUp()
                stored = self.memory.load_session(self.session.id)
                assert stored is not None
                document = json.loads(json.dumps(stored.to_dict()))
                document["chain"] = forged
                with contextlib.closing(sqlite3.connect(self.database)) as db:
                    db.execute(
                        "UPDATE sessions SET state_json = ? WHERE id = ?",
                        (json.dumps(document), self.session.id),
                    )
                    db.commit()
                before = self.database_content()
                try:
                    result = self.cancel()
                except Exception as exc:  # noqa: BLE001 - proving it does not raise
                    self.fail(f"{label} raised {type(exc).__name__}")
                self.assertIsNotNone(result.payload["error"], label)
                self.assertUntouched(before, f"malformed chain {label}")

    def test_5_no_traceback_or_path_reaches_the_output(self) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                ("{not json", self.session.id),
            )
            db.commit()
        code, output = self.run_cli(self.cancel_argv())
        self.assertNotEqual(code, EXIT_OK)
        for leak in ("Traceback", "JSONDecodeError", ".py", "jarvis\\", "jarvis/"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, output)


class LiveTaskTests(ChainCancelTestCase):
    """7, 8. A running task is not this command's business."""

    def test_7_an_in_flight_task_is_never_terminalized(self) -> None:
        task = self.step_task(0)
        self.in_flight_attempt(task.id)
        self.assertEqual(self.cancel().exit_code, EXIT_OK)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        # The chain stopped; the running task did not.
        self.assertIs(stored.status, TaskStatus.RUNNING)
        self.assertIsNot(stored.status, TaskStatus.CANCELLED)
        self.assertIs(self.attempt_state(task.id), AttemptState.IN_FLIGHT)
        self.assertIs(self.chain_now().status, ChainStatus.CANCELLED)

    def test_7_no_connector_is_reachable_from_the_command(self) -> None:
        """12. Not even constructed: the command is routed local-only."""
        task = self.step_task(0)
        self.in_flight_attempt(task.id)

        def explode(settings):
            raise AssertionError("chain cancel built an application")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                self.cancel_argv(), settings=self.settings, app_factory=explode
            )
        output = buffer.getvalue()
        self.assertEqual(code, EXIT_OK, output)
        self.assertIs(self.attempt_state(task.id), AttemptState.IN_FLIGHT)

    def test_8_a_cancel_requested_task_stays_conservative(self) -> None:
        task = self.step_task(0)
        self.in_flight_attempt(task.id)
        # The ordinary task cancellation lands first, exactly as S2A leaves it.
        self.assertEqual(
            self.memory.cancel_task(self.session.id, task.id).verdict, "cancelling"
        )
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCEL_REQUESTED)
        for _ in range(2):
            self.cancel()
        after = self.memory.load_task(task.id)
        assert after is not None
        self.assertIs(after.status, TaskStatus.CANCEL_REQUESTED)
        self.assertIsNot(after.status, TaskStatus.CANCELLED)
        self.assertIs(self.attempt_state(task.id), AttemptState.IN_FLIGHT)

    def test_8_an_unknown_outcome_attempt_is_left_to_recovery(self) -> None:
        task = self.step_task(0)
        attempt_id = self.in_flight_attempt(task.id)
        self.memory.mark_execution_unknown(attempt_id)
        self.assertEqual(self.cancel().exit_code, EXIT_OK)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIsNot(stored.status, TaskStatus.CANCELLED)
        self.assertIsNot(stored.status, TaskStatus.FAILED)
        self.assertIs(self.attempt_state(task.id), AttemptState.UNKNOWN_OUTCOME)


class AuthorityIsolationTests(ChainCancelTestCase):
    """10, 11, 12. Money, approvals and connectors are all out of reach."""

    def test_10_no_billing_state_is_created_or_changed(self) -> None:
        task = self.step_task(0)
        attempt_id = self.in_flight_attempt(task.id)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute(
                "INSERT INTO attempt_billing (attempt_id, usage_id, session_id, "
                "task_id, ledger_month, billing_class, reserved_nano_usd, state, "
                "actual_nano_usd, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id, f"u-{attempt_id}", self.session.id, task.id,
                    "2026-08", "metered_monetary", 500, "active", None,
                    "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00",
                ),
            )
            db.commit()
        before = self.database_content()
        self.assertEqual(self.cancel().exit_code, EXIT_OK)
        after = self.database_content()
        self.assertEqual(
            before["attempt_billing"], after["attempt_billing"], "H3 moved"
        )
        self.assertEqual(before["usage_ledger"], after["usage_ledger"])
        self.assertEqual(self.memory.monthly_monetary_cost(), 0.0)

    def test_11_no_approval_or_grant_is_changed(self) -> None:
        before = self.database_content()
        self.assertEqual(self.cancel().exit_code, EXIT_OK)
        after = self.database_content()
        self.assertEqual(before["permission_grants"], after["permission_grants"])
        self.assertEqual(before["decision_log"], after["decision_log"])

    def test_12_only_the_session_row_is_written(self) -> None:
        """The whole write is the chain status, in the session it belongs to."""
        before = self.database_content()
        self.assertEqual(self.cancel().exit_code, EXIT_OK)
        after = self.database_content()
        for table, rows in before.items():
            with self.subTest(table=table):
                if table == "sessions":
                    self.assertNotEqual(rows, after[table])
                else:
                    self.assertEqual(rows, after[table], f"{table} was written")


class CommandSurfaceTests(ChainCancelTestCase):
    """15. The CLI shape, and what it is allowed to say."""

    def test_the_command_runs_and_reports_exit_zero(self) -> None:
        code, output = self.run_cli(self.cancel_argv())
        self.assertEqual(code, EXIT_OK, output)
        payload = json.loads(output)
        self.assertEqual(payload["command"], "chain-cancel")
        self.assertIs(payload["chain_cancelled"], True)
        self.assertIs(self.chain_now().status, ChainStatus.CANCELLED)

    def test_the_session_id_is_mandatory(self) -> None:
        """No session id, no cancellation - and a bounded input exit code."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(
            io.StringIO()
        ):
            code = cli_main(["chain", "cancel"], settings=self.settings)
        self.assertEqual(code, EXIT_INVALID_INPUT)
        chain = self.chain_now()
        assert chain is not None
        self.assertIs(chain.status, ChainStatus.ACTIVE, "a chain was cancelled")

    def test_an_unknown_sub_command_is_refused(self) -> None:
        code, output = self.run_cli(["chain", "--session-id", self.session.id])
        self.assertNotEqual(code, EXIT_OK)
        self.assertIn("cancel", output)

    def test_15_no_declaration_content_reaches_the_output(self) -> None:
        """The steps are the operator's own input; a cancellation repeats
        none of it, and no task content appears either."""
        self.step_task(0)
        code, output = self.run_cli(self.cancel_argv())
        self.assertEqual(code, EXIT_OK)
        for leak in (
            "Read the project notes.", "Summarize the notes",
            "Save the summary", "notes.txt", "summary.md", "do the step",
        ):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, output)

    def test_15_the_response_states_only_bounded_invariants(self) -> None:
        result = self.cancel()
        payload = result.payload
        self.assertEqual(payload["session_id"], self.session.id)
        self.assertIs(payload["tasks_modified"], False)
        self.assertIs(payload["connector_called"], False)
        self.assertIs(payload["successor_started"], False)
        self.assertIs(payload["billing_modified"], False)
        # Every value is a bounded scalar; nothing carries a nested document.
        for key, value in payload.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, (str, bool, int))
                if isinstance(value, str):
                    self.assertLessEqual(len(value), 400)

    def test_the_command_is_routed_local_only(self) -> None:
        """By source: cancel rides the same inert path as declare and show."""
        from jarvis import cli

        source = inspect.getsource(cli.main)
        gate = source.index('command == "chain"')
        self.assertIn('chain_action", None) != "run"', source[gate:gate + 120])
        dispatch = inspect.getsource(cli._dispatch_chain_command)
        self.assertIn("chain_cancel_result(", dispatch)
        for forbidden in ("AgentCore", "ToolRegistry", "build_app", "connector"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, dispatch)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
