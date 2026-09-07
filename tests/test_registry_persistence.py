from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.contracts import (
    LINEAR_LOOP_STAGES,
    DataClassification,
    ErrorCode,
    LocalReadSnapshot,
    LoopStage,
    PermissionMode,
    RunStatus,
    SessionState,
)
from jarvis.core import AgentCore
from jarvis.memory import SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task, safe_cleanup


class RegistryAndPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
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
        # H2 added the execution-attempt journal at schema 4; H3 added the
        # attempt-bound execution-billing/reservation journal at schema 5;
        # Project Memory M1 added projects/project_memory_items at schema 6;
        # Memory V2 typed provenance at 7; Scheduler phase 1B the durable
        # jobs/job_runs foundation at 8.
        self.assertEqual(9, SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, self.memory.schema_version())
        connection = sqlite3.connect(self.memory.database_path)
        try:
            event_fks = connection.execute("PRAGMA foreign_key_list(event_log)").fetchall()
            billing_fks = connection.execute(
                "PRAGMA foreign_key_list(attempt_billing)"
            ).fetchall()
            billing_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(attempt_billing)")
            }
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
        self.assertGreaterEqual(len(event_fks), 1)
        # The reservation is bound to its execution attempt and carries the
        # ledger month that fixes which monthly cap receives the spend.
        self.assertGreaterEqual(len(billing_fks), 1)
        self.assertIn("execution_attempts", {row[2] for row in billing_fks})
        self.assertIn("ledger_month", billing_columns)
        self.assertIn("reserved_nano_usd", billing_columns)
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
        self.assertIn("execution_attempts", tables)
        self.assertIn("attempt_billing", tables)

    def test_m3_adds_no_schema_migration(self) -> None:
        # M3 stores the local-read snapshot inside the existing opaque task
        # JSON blob, so the database contract is unchanged. If this number
        # moves, a migration was added that M3 explicitly ruled out.
        self.assertEqual(9, SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, self.memory.schema_version())

    def test_local_read_snapshot_survives_a_task_round_trip(self) -> None:
        # The snapshot is persisted task state - not memory, not a decision -
        # because the approved outbound payload must be reproducible
        # byte-identically after a restart (H1).
        session = SessionState(objective="round trip")
        self.memory.save_session(session)
        task = make_task()
        task.local_read = LocalReadSnapshot(
            relative_path="docs/notes.txt",
            byte_size=11,
            sha256="a" * 64,
            excerpt="alpha beta\n",
            truncated=False,
            classification=DataClassification.INTERNAL,
            read_at="2026-01-01T00:00:00+00:00",
        )
        self.memory.save_task(session.id, task)
        reloaded = self.memory.load_task(task.id)
        self.assertEqual(task.local_read, reloaded.local_read)

    def _reload_with_local_read(self, **overrides):
        session = SessionState(objective="tampered")
        self.memory.save_session(session)
        task = make_task()
        self.memory.save_task(session.id, task)
        snapshot = {
            "relative_path": "notes.txt",
            "byte_size": 10,
            "sha256": "b" * 64,
            "excerpt": "z" * 9_000,
            "truncated": False,
            "classification": "public",
            "read_at": "2026-01-01T00:00:00+00:00",
        }
        snapshot.update(overrides)
        connection = sqlite3.connect(self.memory.database_path)
        try:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task.id,)
            ).fetchone()
            document = json.loads(row[0])
            document["local_read"] = snapshot
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task.id),
            )
            connection.commit()
        finally:
            connection.close()
        return self.memory.load_task(task.id)

    def test_a_tampered_persisted_snapshot_reloads_fail_closed(self) -> None:
        """An unreadable classification discards the WHOLE snapshot.

        A SECRET fallback would hand back a structurally valid, PRESENT
        snapshot instead - one that satisfies the contract, renders in the
        envelope and answers ``present`` to every gate that asks that first.
        Malformed persisted state must not survive as a usable object
        wearing a safe label.
        """
        reloaded = self._reload_with_local_read(
            classification="not-a-classification"
        )
        self.assertFalse(reloaded.local_read.present)
        self.assertEqual("", reloaded.local_read.relative_path)
        self.assertEqual("", reloaded.local_read.excerpt)

    def test_an_unreadable_classification_type_reloads_fail_closed(self) -> None:
        for value in (None, 7, True, ["public"], {"value": "public"}):
            with self.subTest(value=value):
                reloaded = self._reload_with_local_read(classification=value)
                self.assertFalse(reloaded.local_read.present)

    def test_a_valid_classification_still_rebounds_an_oversized_excerpt(
        self,
    ) -> None:
        """The one legal repair survives, and only for a readable row."""
        reloaded = self._reload_with_local_read(classification="public")
        self.assertTrue(reloaded.local_read.present)
        self.assertIs(DataClassification.PUBLIC, reloaded.local_read.classification)
        self.assertEqual(4_000, len(reloaded.local_read.excerpt))
        self.assertTrue(reloaded.local_read.truncated)

    def test_a_valid_secret_classification_is_preserved(self) -> None:
        reloaded = self._reload_with_local_read(
            classification="secret", excerpt="bounded\n"
        )
        self.assertTrue(reloaded.local_read.present)
        self.assertIs(DataClassification.SECRET, reloaded.local_read.classification)


if __name__ == "__main__":
    unittest.main()
