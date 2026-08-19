from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.contracts import (
    LINEAR_LOOP_STAGES,
    ErrorCode,
    LoopStage,
    PermissionMode,
    RunStatus,
)
from jarvis.core import AgentCore
from jarvis.memory import SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task


class RegistryAndPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def core(self, *connectors: DummyConnector) -> AgentCore:
        registry = ToolRegistry()
        for connector in connectors:
            registry.register(connector)
        return AgentCore(
            registry,
            self.memory,
            Settings(
                memory_database=self.memory.database_path,
                permission_mode=PermissionMode.SESSION,
            ),
        )

    def test_ambiguous_connectors_fail_instead_of_first_match(self) -> None:
        run = self.core(
            DummyConnector(name="a"), DummyConnector(name="b")
        ).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.CONNECTOR_AMBIGUOUS, run.error.code)

    def test_unavailable_connector_is_reported(self) -> None:
        run = self.core(DummyConnector(available=False)).run(make_task())
        self.assertEqual(ErrorCode.CONNECTOR_UNAVAILABLE, run.error.code)

    def test_priority_then_cost_selects_deterministically(self) -> None:
        high = DummyConnector(name="high", priority=20, estimate_max=1.0)
        low = DummyConnector(name="low", priority=10, estimate_max=0.0)
        run = self.core(high, low).run(make_task())
        self.assertEqual("high", run.task.selected_connector)
        self.assertEqual(1, high.execute_calls)
        self.assertEqual(0, low.execute_calls)

    def test_lower_cost_breaks_equal_priority_and_suitability(self) -> None:
        expensive = DummyConnector(name="expensive", estimate_max=1.0)
        cheap = DummyConnector(name="cheap", estimate_max=0.0)
        run = self.core(expensive, cheap).run(make_task())
        self.assertEqual("cheap", run.task.selected_connector)

    def test_unknown_session_never_creates_a_new_session(self) -> None:
        core = self.core(DummyConnector())
        run = core.run(make_task(), "missing-session")
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.UNKNOWN_SESSION, run.error.code)
        self.assertIsNone(self.memory.load_session("missing-session"))

    def test_completed_task_persists_all_loop_information(self) -> None:
        run = self.core(DummyConnector()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        restored = self.memory.load_task(run.task.id)
        self.assertEqual(LoopStage.COMPLETED, restored.stage)
        self.assertIsNotNone(restored.result_snapshot)
        # Progress and the navigation pointer survive a real SQLite restart
        # through the existing JSON columns only.
        reopened = SQLiteMemoryStore(self.memory.database_path)
        after_restart = reopened.load_task(run.task.id)
        self.assertEqual(LINEAR_LOOP_STAGES, after_restart.progress.completed_stages)
        self.assertEqual((), after_restart.progress.open_issues)
        self.assertEqual((), after_restart.progress.artifacts)
        self.assertEqual(
            run.task.progress.to_dict(), after_restart.progress.to_dict()
        )
        session = reopened.load_session(run.session.id)
        self.assertIsNone(session.active_task_id)
        self.assertEqual(run.task.id, session.latest_task_id)
        events = {event["event_type"] for event in self.memory.list_events(run.session.id)}
        self.assertTrue(
            {
                "intent_understood",
                "context_retrieved",
                "assumption_visible",
                "plan_created",
                "permission_policy_passed",
                "cost_preflight_passed",
                "tool_selected",
                "execution_completed",
                "verification_passed",
                "result_presented",
                "task_completed",
            }.issubset(events)
        )
        decisions = self.memory.list_decisions(run.session.id)
        self.assertEqual(1, len(decisions))
        self.assertEqual(run.task.id, decisions[0].task_id)

    def test_schema_has_version_foreign_keys_and_wal(self) -> None:
        # Progress enrichment added no SQL column, table or migration.
        self.assertEqual(3, SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, self.memory.schema_version())
        connection = sqlite3.connect(self.memory.database_path)
        try:
            event_fks = connection.execute("PRAGMA foreign_key_list(event_log)").fetchall()
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
        self.assertGreaterEqual(len(event_fks), 1)
        self.assertEqual("wal", journal_mode.lower())

    def test_legacy_database_is_migrated_additively(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()
        migrated = SQLiteMemoryStore(legacy_path)
        self.assertEqual(SCHEMA_VERSION, migrated.schema_version())
        check = sqlite3.connect(legacy_path)
        try:
            tables = {
                row[0]
                for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            check.close()
        self.assertIn("tasks", tables)
        self.assertIn("schema_migrations", tables)


if __name__ == "__main__":
    unittest.main()
