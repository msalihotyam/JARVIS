"""V2.1 SEC: hard purge - the one DELETE path, hash-only audit, permanent tombstone (SECPURGE).

Every database is a throwaway in TEMP; nothing opens the operator's database.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
    utc_now,
)
from jarvis.memory import (
    MEMORY_RECORDS_FTS5_AVAILABLE,
    MemoryTombstoned,
    PurgeConfirmation,
    SQLiteMemoryStore,
)
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from tests.helpers import safe_cleanup

S = MemoryRecordStatus
K = MemoryRecordKind
USER = MemoryRecordActor.USER
MODEL = MemoryRecordActor.MODEL
TEXT = "the zorbaquilt pipeline deploys nightly"


def mint(record_ids) -> PurgeConfirmation:
    """Test stand-in for Core's mint (SECAPPUI mints in production)."""
    return PurgeConfirmation(frozenset(record_ids), utc_now(), uuid4().hex)


class PurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.database = self.root / "memory.db"
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id

    def insert(self, text=TEXT, *, subject="deploy", project=None, supersedes=None,
               origin=MemoryRecordOrigin.CAPTURE_SESSION_END, guarded=True) -> str:
        record = MemoryRecord(
            kind=K.FACT, text=text, subject_key=subject,
            scope=MemoryRecordScope(project or self.alpha),
            origin=origin, actor=MODEL, confidence=0.8,
            classification=DataClassification.INTERNAL, supersedes=supersedes,
        )
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (), K.FACT,
                            content_hash(text)) if guarded else None
        return self.store.insert_record(record, guard).record.record_id

    def rows(self, sql: str, args=()) -> list[tuple]:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def record_tables(self) -> list[str]:
        return [name for (name,) in self.rows(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND (name LIKE 'memory_record%' OR name LIKE 'memory_purge%' OR name LIKE 'memory_owned%')")]

    def mentions(self, needle: str) -> list[str]:
        """Every table (record*, FTS, audit, artifacts) holding ``needle`` in any column."""
        found = []
        for table in self.record_tables():
            for row in self.rows(f'SELECT * FROM "{table}"'):  # nosec B608 - names from sqlite_master
                if any(needle in str(value) for value in row if value is not None):
                    found.append(table)
        return found

    def fts_match(self, word: str) -> list[tuple]:
        return self.rows("SELECT record_id FROM memory_records_fts WHERE memory_records_fts MATCH ?",
                         (f'"{word}"',))

    def test_purge_leaves_no_row_text_or_id_anywhere(self) -> None:
        record_id = self.insert()
        keep = self.insert("an unrelated fact stays", subject="other")
        if MEMORY_RECORDS_FTS5_AVAILABLE:
            self.assertEqual(len(self.fts_match("zorbaquilt")), 1)
        result = self.store.purge_records([record_id], actor=USER, confirmation=mint([record_id]))
        self.assertEqual(result["record_ids"], (record_id,))
        self.assertEqual(result["still_held_by"], ())
        self.assertEqual(self.mentions(record_id), [])
        self.assertEqual(self.mentions("zorbaquilt"), [])
        if MEMORY_RECORDS_FTS5_AVAILABLE:
            self.assertEqual(self.fts_match("zorbaquilt"), [])
        self.assertEqual(self.store.search_records("zorbaquilt", MemoryRecordScope(self.alpha)), [])
        self.assertIsNone(self.store.current_status(record_id))
        self.assertIs(self.store.current_status(keep), S.APPROVED)
        # secure_delete + FTS optimize + WAL truncate: the text is not in the files either.
        for path in (self.database, self.database.with_name(self.database.name + "-wal")):
            if path.exists():
                self.assertNotIn(b"zorbaquilt", path.read_bytes(), path.name)

    def test_audit_keeps_purge_id_hash_and_time_only(self) -> None:
        record_id = self.insert()
        result = self.store.purge_records({record_id}, actor=USER, confirmation=mint([record_id]))
        columns = [row[1] for row in self.rows("PRAGMA table_info(memory_purge_audit)")]
        self.assertEqual(columns, ["purge_id", "content_hash", "purged_at"])
        audit = self.rows("SELECT purge_id, content_hash FROM memory_purge_audit")
        self.assertEqual(audit, [(result["purge_id"], content_hash(TEXT))])
        self.assertNotIn(record_id, result["purge_id"])

    def test_permanent_tombstone_blocks_recapture_in_scope(self) -> None:
        record_id = self.insert()
        self.store.purge_records([record_id], actor=USER, confirmation=mint([record_id]))
        self.assertEqual(
            self.rows("SELECT content_hash, project_id, scope_kind, reason, expires_at FROM memory_tombstones"),
            [(content_hash(TEXT), self.alpha, "project", "purged", None)],
        )
        self.assertTrue(self.store.is_tombstoned(content_hash(TEXT), self.alpha))
        with self.assertRaises(MemoryTombstoned):
            self.insert()
        self.insert(project=self.beta)  # another project's scope is not blocked

    def test_refused_without_user_and_exact_confirmation(self) -> None:
        record_id = self.insert()
        other = self.insert("a second fact", subject="second")
        refusals = [
            dict(actor=MODEL, confirmation=mint([record_id])),
            dict(actor=MemoryRecordActor.TOOL, confirmation=mint([record_id])),
            dict(actor=USER, confirmation=None),
            dict(actor=USER, confirmation=mint([record_id, other])),
            dict(actor=USER, confirmation=mint([other])),
            dict(actor=USER, confirmation=replace(mint([record_id]), nonce="")),
            dict(actor=USER, confirmation=replace(mint([record_id]), minted_at="yesterday")),
            dict(actor=USER, confirmation=replace(mint([record_id]), record_ids=[record_id])),
        ]
        for kwargs in refusals:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.purge_records([record_id], **kwargs)
        with self.assertRaises(ValueError):
            self.store.purge_records(["missing-id"], actor=USER, confirmation=mint(["missing-id"]))
        with self.assertRaises(ValueError):
            self.store.purge_records([], actor=USER, confirmation=mint([]))
        self.assertIs(self.store.current_status(record_id), S.APPROVED)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_tombstones"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_purge_audit"), [(0,)])

    def test_superseded_chain_is_removed_in_both_directions(self) -> None:
        first = self.insert("deploys run at nine")
        second = self.insert("deploys run at ten", supersedes=first)
        self.store.supersede(first, second, MODEL)
        third = self.insert("deploys run at eleven", supersedes=second)
        self.store.supersede(second, third, MODEL)
        pending = self.insert("deploys run at noon", supersedes=third, guarded=False)
        keep = self.insert("an unrelated fact stays", subject="other")
        chain = tuple(sorted([first, second, third, pending]))
        self.assertEqual(self.store.purge_preview([second]), {"record_ids": chain, "still_held_by": ()})
        with self.assertRaises(ValueError):  # the chain goes only once the user confirmed all of it
            self.store.purge_records([second], actor=USER, confirmation=mint([second]))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_records"), [(5,)])
        result = self.store.purge_records(chain, actor=USER, confirmation=mint(chain))
        self.assertEqual(result["record_ids"], chain)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_record_supersessions"), [(0,)])
        for record_id in (first, second, third, pending):
            self.assertEqual(self.mentions(record_id), [])
        self.assertEqual(self.rows("SELECT record_id FROM memory_records"), [(keep,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_tombstones WHERE reason = 'purged'"), [(4,)])

    def test_consolidation_derived_record_is_removed(self) -> None:
        source = self.insert()
        derived = self.insert("a consolidated summary", subject="summary",
                              origin=MemoryRecordOrigin.CONSOLIDATION, guarded=False)
        self.store.record_event(derived, S.REJECTED, USER, f"merged from {source}")
        bystander = self.insert("a consolidated other", subject="other",
                                origin=MemoryRecordOrigin.CONSOLIDATION, guarded=False)
        self.store.record_event(bystander, S.REJECTED, USER, f"merged from {source}x")
        purge_set = self.store.purge_preview([source])["record_ids"]
        self.assertEqual(purge_set, tuple(sorted([source, derived])))
        result = self.store.purge_records(purge_set, actor=USER, confirmation=mint(purge_set))
        self.assertEqual(result["record_ids"], purge_set)
        self.assertIsNone(self.store.current_status(derived))
        self.assertIs(self.store.current_status(bystander), S.REJECTED)

    def test_linked_files_deleted_backups_and_foreign_paths_kept(self) -> None:
        record_id = self.insert()
        keep = self.insert("an unrelated fact stays", subject="other")
        export = self.root / "exports" / "record.json"
        export.parent.mkdir()
        export.write_text(TEXT, encoding="utf-8")
        backup = self.root / "memory.backup.db"
        backup.write_bytes(b"whole db copy")
        outside_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, outside_dir)
        outside = Path(outside_dir.name) / "elsewhere.json"
        outside.write_text("not ours", encoding="utf-8")
        other_file = self.root / "other.json"
        other_file.write_text("other record", encoding="utf-8")
        connection = sqlite3.connect(self.database)
        try:
            connection.executemany(
                "INSERT INTO memory_owned_artifacts (path, kind, record_id, created_at) VALUES (?, ?, ?, ?)",
                [(str(export), "export", record_id, utc_now()),
                 (str(backup), "backup", None, utc_now()),
                 (str(outside), "export", record_id, utc_now()),
                 (str(self.database), "export", record_id, utc_now()),
                 (str(self.root / "gone.json"), "export", record_id, utc_now()),
                 (str(other_file), "export", keep, utc_now())],
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.store.purge_preview([record_id])["still_held_by"], (str(backup),))
        result = self.store.purge_records([record_id], actor=USER, confirmation=mint([record_id]))
        self.assertFalse(export.exists())
        self.assertTrue(backup.exists())
        self.assertTrue(outside.exists())
        self.assertTrue(self.database.exists())
        self.assertTrue(other_file.exists())
        self.assertEqual(sorted(result["still_held_by"]),
                         sorted([str(backup), str(outside), str(self.database)]))
        # Rows go only with their file; a still-held linked file keeps its row.
        self.assertEqual(sorted(self.rows("SELECT path, record_id FROM memory_owned_artifacts")),
                         sorted([(str(backup), None), (str(other_file), keep),
                                 (str(outside), record_id), (str(self.database), record_id)]))

    def test_wal_held_by_a_reader_is_reported(self) -> None:
        record_id = self.insert()
        reader = sqlite3.connect(self.database)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM memory_records").fetchone()
        result = self.store.purge_records([record_id], actor=USER, confirmation=mint([record_id]))
        self.assertEqual(result["still_held_by"], (str(self.database) + "-wal",))


if __name__ == "__main__":
    unittest.main()
