"""PM02 ledger store: append-only add-on tables on schema 10."""

from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis import memory
from jarvis.memory import (
    COMPATIBILITY_SCHEMA_OBJECTS,
    SCHEMA_VERSION,
    WORK_SCHEMA_OBJECTS,
    SQLiteMemoryStore,
)
from jarvis.work_ledger import (
    EventKind,
    RunSize,
    RunState,
    StepState,
    WorkEvent,
    WorkLedgerError,
    WorkRun,
    WorkStep,
    event_payload_json,
    idempotency_key,
    step_key,
)

T = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
WORK_TABLES = ("work_run", "work_step", "work_event", "work_lease", "notify_outbox")


class WorkStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jarvis.db"
        self.store = SQLiteMemoryStore(self.path)

    def _tables(self) -> set[str]:
        connection = sqlite3.connect(self.path)
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                )
            }
        finally:
            connection.close()

    def test_schema_version_stays_10_and_objects_registered(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertTrue(set(WORK_TABLES) <= WORK_SCHEMA_OBJECTS)
        self.assertTrue(WORK_SCHEMA_OBJECTS <= COMPATIBILITY_SCHEMA_OBJECTS)
        self.assertTrue(WORK_SCHEMA_OBJECTS <= self._tables())

    def test_open_twice_is_idempotent(self) -> None:
        self.store.append_work_run(WorkRun("r1", RunState.CLARIFYING, T, T))
        SQLiteMemoryStore(self.path)
        SQLiteMemoryStore(self.path)
        self.assertTrue(WORK_SCHEMA_OBJECTS <= self._tables())
        self.assertEqual(self.store.latest_work_run("r1").state, RunState.CLARIFYING)

    def test_schema10_db_without_work_tables_still_opens(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            for table in reversed(WORK_TABLES):
                connection.execute(f"DROP TABLE {table}")
            connection.commit()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertFalse(set(WORK_TABLES) & self._tables())
        reopened = SQLiteMemoryStore(self.path)
        self.assertTrue(WORK_SCHEMA_OBJECTS <= self._tables())
        self.assertIsNone(reopened.latest_work_run("r1"))
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], version)
        finally:
            connection.close()

    def test_run_latest_snapshot_wins(self) -> None:
        self.assertIsNone(self.store.latest_work_run("r1"))
        self.store.append_work_run(WorkRun("r1", RunState.CLARIFYING, T, T))
        waiting = WorkRun(
            "r1",
            RunState.NEEDS_INPUT,
            T,
            T + timedelta(minutes=5),
            size=RunSize.MEDIUM,
            resume_state=RunState.BUILDING,
        )
        self.store.append_work_run(waiting)
        self.assertEqual(self.store.latest_work_run("r1"), waiting)
        connection = sqlite3.connect(self.path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM work_run").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 2)

    def test_steps_latest_per_key_in_ordinal_order(self) -> None:
        first = step_key("r1", 1, "scaffold")
        second = step_key("r1", 2, "tests")
        self.store.append_work_step(WorkStep("r1", second, 2, "tests", StepState.PENDING, T, depends_on=(first,)))
        self.store.append_work_step(WorkStep("r1", first, 1, "scaffold", StepState.PENDING, T))
        revised = WorkStep(
            "r1",
            first,
            1,
            "scaffold",
            StepState.RUNNING,
            T + timedelta(minutes=1),
            attempt=2,
            attempt_link="attempt://r1/1/2",
        )
        self.store.append_work_step(revised)
        steps = self.store.work_steps("r1")
        self.assertEqual([step.key for step in steps], [first, second])
        self.assertEqual(steps[0], revised)
        self.assertEqual(steps[1].depends_on, (first,))
        self.assertEqual(self.store.work_steps("other"), [])

    def test_model_output_event_round_trip_and_identical_repeat(self) -> None:
        key = step_key("r1", 1, "scaffold")
        event = WorkEvent(
            "r1",
            1,
            EventKind.MODEL_OUTPUT,
            T,
            step_key=key,
            payload_json=event_payload_json({"text": "merhaba", "tokens": 3}),
        )
        idem = idempotency_key("r1", key, 1, "model.reply")
        self.assertTrue(self.store.append_work_event(event, idempotency_key=idem))
        self.assertFalse(self.store.append_work_event(event, idempotency_key=idem))
        self.assertEqual(self.store.work_events("r1"), [event])
        self.assertEqual(self.store.notify_outbox_rows(), [])

    def test_conflicting_event_at_same_seq_fails_closed(self) -> None:
        self.store.append_work_event(WorkEvent("r1", 1, EventKind.PROGRESS, T))
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_event(
                WorkEvent("r1", 1, EventKind.DONE, T), severity="normal"
            )
        self.assertEqual([event.kind for event in self.store.work_events("r1")], [EventKind.PROGRESS])
        self.assertEqual(self.store.notify_outbox_rows(), [])

    def test_reused_idempotency_key_fails_closed(self) -> None:
        self.store.append_work_event(WorkEvent("r1", 1, EventKind.PROGRESS, T), idempotency_key="k")
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_event(WorkEvent("r1", 2, EventKind.PROGRESS, T), idempotency_key="k")
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_event(WorkEvent("r1", 3, EventKind.PROGRESS, T), idempotency_key="")
        self.assertEqual(len(self.store.work_events("r1")), 1)

    def test_bad_severity_rejected_before_write(self) -> None:
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_event(WorkEvent("r1", 1, EventKind.DONE, T), severity="urgent")
        self.assertEqual(self.store.work_events("r1"), [])

    def test_severity_writes_outbox_row_linked_to_event(self) -> None:
        self.store.append_work_event(WorkEvent("r1", 1, EventKind.PROGRESS, T), severity="low")
        self.store.append_work_event(
            WorkEvent("r1", 2, EventKind.PLAN_READY, T + timedelta(seconds=1)), severity="high"
        )
        rows = self.store.notify_outbox_rows()
        self.assertEqual(
            [(row["run_id"], row["event_seq"], row["kind"], row["severity"]) for row in rows],
            [("r1", 1, "progress", "low"), ("r1", 2, "plan_ready", "high")],
        )
        self.assertEqual(self.store.notify_outbox_rows(after_id=rows[0]["outbox_id"]), rows[1:])

    def test_identical_retry_with_severity_keeps_the_notification(self) -> None:
        event = WorkEvent("r1", 1, EventKind.DONE, T)
        self.store.append_work_event(event)
        self.assertFalse(self.store.append_work_event(event, severity="high"))
        self.assertFalse(self.store.append_work_event(event, severity="high"))
        rows = self.store.notify_outbox_rows()
        self.assertEqual([(row["event_seq"], row["severity"]) for row in rows], [(1, "high")])
        self.assertEqual(self.store.work_events("r1"), [event])

    def test_outbox_severity_check_constraint(self) -> None:
        self.store.append_work_event(WorkEvent("r1", 1, EventKind.DONE, T))
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO notify_outbox (run_id, event_seq, kind, severity, created_at)"
                    " VALUES ('r1', 1, 'done', 'urgent', ?)",
                    (T.isoformat(),),
                )
        finally:
            connection.close()

    def test_lease_append_and_read(self) -> None:
        self.store.append_work_lease(
            target="repo:jarvis", run_id="r1", boot_id="b1", claimed_at=T, expires_at=T + timedelta(minutes=10)
        )
        self.store.append_work_lease(
            target="repo:jarvis",
            run_id="r1",
            boot_id="b1",
            claimed_at=T + timedelta(minutes=5),
            expires_at=T + timedelta(minutes=15),
        )
        leases = self.store.work_leases("repo:jarvis")
        self.assertEqual([lease["expires_at"] for lease in leases], [T + timedelta(minutes=10), T + timedelta(minutes=15)])
        self.assertEqual(leases[0]["boot_id"], "b1")
        self.assertEqual(self.store.work_leases("repo:other"), [])
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_lease(target="repo:jarvis", run_id="r1", boot_id="b1", claimed_at=T, expires_at=T)
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_lease(
                target="", run_id="r1", boot_id="b1", claimed_at=T, expires_at=T + timedelta(minutes=1)
            )
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_lease(
                target="repo:jarvis",
                run_id="r1",
                boot_id="b1",
                claimed_at=T.replace(tzinfo=None),
                expires_at=T + timedelta(minutes=1),
            )
        self.assertEqual(len(self.store.work_leases("repo:jarvis")), 2)

    def test_non_dataclass_inputs_rejected(self) -> None:
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_run({"run_id": "r1"})  # type: ignore[arg-type]
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_step({"run_id": "r1"})  # type: ignore[arg-type]
        with self.assertRaises(WorkLedgerError):
            self.store.append_work_event({"run_id": "r1"})  # type: ignore[arg-type]

    def test_store_source_is_append_only_for_work_tables(self) -> None:
        source = Path(memory.__file__).read_text(encoding="utf-8")
        mutation = re.compile(r"\b(ALTER\s+TABLE|UPDATE|DELETE\s+FROM|DROP\s+TABLE)\s+(work_\w+|notify_outbox)\b", re.I)
        self.assertIsNone(mutation.search(source))


if __name__ == "__main__":
    unittest.main()
