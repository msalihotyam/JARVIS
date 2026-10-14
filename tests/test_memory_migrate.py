"""SECMIGRATE: one-time, idempotent M1 legacy memory migration through Core.

Every database is an isolated TEMP one (isolated_settings); nothing opens
the operator's database and no model or connector is called.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from jarvis.app import EXIT_INVALID_INPUT, EXIT_OK
from jarvis.cli import main
from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    utc_now,
)
from jarvis.core import migrate_legacy_memory
from jarvis.memory import SQLiteMemoryStore
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from jarvis.registry import ToolRegistry
from tests.helpers import isolated_settings, open_core

SECRET = "deploy password is hunter2-Secret!"


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.db = Path(self.settings.memory_database)
        self.store = SQLiteMemoryStore(self.db)
        self.alpha = self.store.create_project("Alpha").id
        save = self.store.save_explicit_user_memory_item
        self.fact = save(self.alpha, "fact", "internal", "The build uses Python 3.13").id
        self.decision = save(self.alpha, "decision", "internal", "We chose SQLite for storage").id
        self.rule = save(self.alpha, "rule", "internal", "From now on always answer in Turkish").id
        self.thread = save(self.alpha, "thread", "internal", "Open question about packaging").id
        self.retired = save(self.alpha, "fact", "internal", "The old CI ran on Travis").id
        self.store.retire_memory_item(self.alpha, self.retired)
        # A pre-filter era row: the M1 writer would refuse it today.
        self.secret = str(uuid4())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO project_memory_items (id, project_id, kind, text, classification, "
                "origin, origin_detail, active, superseded_by, created_at, updated_at) "
                "VALUES (?, ?, 'fact', ?, 'internal', 'inferred', '', 1, NULL, ?, ?)",
                (self.secret, self.alpha, SECRET, utc_now(), utc_now()),
            )
        self.migrated_ids = {self.fact, self.decision, self.rule}

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.connect() as connection:
            return connection.execute(sql, args).fetchall()

    def legacy_rows(self) -> list[tuple]:
        return self.rows("SELECT * FROM project_memory_items ORDER BY id")

    def written(self) -> tuple[int, int, int, int]:
        return tuple(self.rows(f"SELECT COUNT(*) FROM {table}")[0][0] for table in (  # nosec B608 - constant names
            "memory_records", "memory_record_events", "memory_legacy_migration", "memory_owned_artifacts"))

    def backups(self) -> list[Path]:
        return sorted(self.db.parent.glob("*.pre-migrate-*"))

    def test_migrates_through_the_guard_as_pending_after_a_backup(self) -> None:
        before = self.legacy_rows()
        report = migrate_legacy_memory(self.store, self.alpha)
        self.assertEqual(report.outcome, "ok")
        self.assertEqual((report.migrated, report.skipped, report.failed, report.already_migrated), (3, 3, 0, 0))
        self.assertEqual(dict(report.skip_reasons), {
            self.thread: "unmapped_kind", self.retired: "inactive", self.secret: "filtered:credential"})
        # Legacy rows are only read.
        self.assertEqual(self.legacy_rows(), before)
        # The backup exists, is a full DB copy and is registered as a whole-DB backup.
        self.assertEqual(self.backups(), [Path(report.backup_path)])
        self.assertEqual(self.rows("SELECT path, kind, record_id FROM memory_owned_artifacts"),
                         [(report.backup_path, "backup", None)])
        with contextlib.closing(sqlite3.connect(report.backup_path)) as copy:
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM project_memory_items").fetchone()[0], 6)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 0)
        # Nothing is approved: one PENDING event per record, all legacy-guarded RULEs.
        marks = ",".join("?" * len(report.record_ids))
        self.assertEqual(sorted(self.rows(
            f"SELECT r.kind, r.origin, r.actor, r.tool, e.status, g.provenance, g.session_taint "  # nosec B608
            f"FROM memory_records r JOIN memory_record_events e ON e.record_id = r.record_id "
            f"JOIN memory_record_guard g ON g.record_id = r.record_id WHERE r.record_id IN ({marks})",
            report.record_ids)),
            [("rule", "legacy_m1", "tool", "memory_migrate", "pending", "legacy", "legacy")] * 3)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_record_events WHERE status != 'pending'"), [(0,)])
        ledger = dict(self.rows("SELECT legacy_id, COALESCE(record_id, skip_reason) FROM memory_legacy_migration"))
        self.assertEqual({lid for lid in ledger if ledger[lid] in report.record_ids}, self.migrated_ids)
        # The secret never reached a record, an index or the ledger.
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_records WHERE text LIKE '%hunter2%'"), [(0,)])
        self.assertEqual(ledger[self.secret], "filtered:credential")
        # Every legacy row is now mapped for the Doctor exit gate.
        self.assertEqual(self.store.doctor_snapshot(self.alpha).legacy_items, ())

    def test_second_run_creates_nothing(self) -> None:
        first = migrate_legacy_memory(self.store, None)
        self.assertEqual((first.outcome, first.migrated, first.skipped), ("ok", 3, 3))
        after_first = (self.written(), self.backups(), self.legacy_rows())
        second = migrate_legacy_memory(self.store, None)
        self.assertEqual(second.outcome, "ok")
        self.assertEqual((second.migrated, second.skipped, second.failed, second.already_migrated), (0, 0, 0, 6))
        self.assertIsNone(second.backup_path)
        self.assertEqual((self.written(), self.backups(), self.legacy_rows()), after_first)

    def test_no_backup_no_migration(self) -> None:
        before = (self.written(), self.legacy_rows())
        with mock.patch.object(self.store, "backup_before_migration", side_effect=OSError("disk full")):
            report = migrate_legacy_memory(self.store, self.alpha)
        self.assertEqual((report.outcome, report.migrated, report.record_ids), ("backup_failed", 0, ()))
        self.assertEqual((self.written(), self.legacy_rows()), before)

    def test_backup_that_does_not_verify_leaves_no_file(self) -> None:
        with mock.patch.object(SQLiteMemoryStore, "_verified_backup", side_effect=RuntimeError("bad copy")):
            report = migrate_legacy_memory(self.store, self.alpha)
        self.assertEqual(report.outcome, "backup_failed")
        self.assertEqual((self.written()[:3], self.backups()), ((0, 0, 0), []))

    def test_unregistered_backup_is_removed_and_nothing_migrated(self) -> None:
        with self.connect() as connection:
            connection.execute("CREATE TRIGGER no_artifacts BEFORE INSERT ON memory_owned_artifacts "
                               "BEGIN SELECT RAISE(ABORT, 'refused'); END")
        report = migrate_legacy_memory(self.store, self.alpha)
        self.assertEqual(report.outcome, "backup_failed")
        self.assertEqual((self.written(), self.backups()), ((0, 0, 0, 0), []))

    def test_archived_project_is_ledgered_once(self) -> None:
        beta = self.store.create_project("Beta").id
        archived = self.store.save_explicit_user_memory_item(beta, "fact", "internal", "Beta ships on Fridays").id
        self.store.set_project_active(beta, False)
        first = migrate_legacy_memory(self.store, beta)
        self.assertEqual((first.migrated, first.failed, dict(first.skip_reasons)),
                         (0, 0, {archived: "inactive_project"}))
        second = migrate_legacy_memory(self.store, beta)
        self.assertEqual((second.already_migrated, second.backup_path), (1, None))

    def test_dry_run_writes_nothing(self) -> None:
        before = (self.written(), self.legacy_rows(), sorted(self.db.parent.iterdir()))
        report = migrate_legacy_memory(self.store, self.alpha, dry_run=True)
        self.assertEqual((report.outcome, report.dry_run, report.migrated, report.skipped), ("ok", True, 3, 3))
        self.assertEqual((report.backup_path, report.record_ids), (None, ()))
        self.assertEqual((self.written(), self.legacy_rows(), sorted(self.db.parent.iterdir())), before)

    def test_tombstoned_text_is_skipped_with_reason(self) -> None:
        with mock.patch.object(SQLiteMemoryStore, "_tombstoned", return_value=True):
            report = migrate_legacy_memory(self.store, self.alpha)
        self.assertEqual(report.migrated, 0)
        self.assertEqual({lid: r for lid, r in report.skip_reasons if lid in self.migrated_ids},
                         dict.fromkeys(self.migrated_ids, "tombstoned"))

    def test_failed_write_is_not_ledgered_and_retries(self) -> None:
        with mock.patch.object(self.store, "insert_migrated_legacy_record", side_effect=sqlite3.OperationalError("locked")):
            report = migrate_legacy_memory(self.store, self.alpha)
        self.assertEqual((report.migrated, report.failed, report.skipped), (0, 3, 3))
        retry = migrate_legacy_memory(self.store, self.alpha)
        self.assertEqual((retry.migrated, retry.already_migrated, retry.failed), (3, 3, 0))

    def test_store_refuses_a_non_legacy_guard_or_a_second_ledger_row(self) -> None:
        record = MemoryRecord(
            kind=MemoryRecordKind.FACT, text="The build uses Python 3.13", subject_key="legacy-x",
            scope=MemoryRecordScope(self.alpha), origin=MemoryRecordOrigin.LEGACY_M1,
            actor=MemoryRecordActor.TOOL, tool="memory_migrate", confidence=0.5,
            classification=DataClassification.INTERNAL,
        )
        clean = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (), MemoryRecordKind.FACT,
                            content_hash(record.text))
        with self.assertRaises(ValueError):
            self.store.insert_migrated_legacy_record(self.fact, record, clean)
        self.assertEqual(self.written()[0], 0)
        report = migrate_legacy_memory(self.store, self.alpha)
        with self.assertRaises(ValueError):
            self.store.record_legacy_migration_skip(self.fact, "unmapped_kind")
        with self.assertRaises(ValueError):
            self.store.record_legacy_migration_skip(self.thread, "free text with spaces")
        self.assertEqual(self.written()[2], 6)
        self.assertEqual(len(report.record_ids), 3)

    def test_core_method_and_bad_project(self) -> None:
        core = open_core(ToolRegistry(), self.store, self.settings, destination_policy=frozenset())
        self.assertEqual(core.migrate_legacy_memory(self.alpha, dry_run=True).migrated, 3)
        self.assertEqual(migrate_legacy_memory(self.store, "../x").outcome, "invalid_project")

    def cli(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["memory", "migrate", *argv], settings=self.settings)
        return code, json.loads(out.getvalue())

    def test_cli_prints_counts_by_outcome(self) -> None:
        code, payload = self.cli("--dry-run")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["counts"], {"migrated": 3, "already_migrated": 0, "skipped": 3, "failed": 0,
                                             "reasons": {"unmapped_kind": 1, "inactive": 1, "filtered:credential": 1}})
        self.assertEqual(self.written()[2], 0)
        code, payload = self.cli("--project", self.alpha)
        self.assertEqual((code, payload["dry_run"], payload["counts"]["migrated"]), (EXIT_OK, False, 3))
        self.assertTrue(Path(payload["backup_path"]).is_relative_to(self.db.parent))
        code, payload = self.cli("--project", "../x")
        self.assertEqual(code, EXIT_INVALID_INPUT)


if __name__ == "__main__":
    unittest.main()
