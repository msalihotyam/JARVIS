"""V2.1 SEC: guard rows and scope-aware content-hash tombstones (SECSTORE).

Every database is a throwaway in TEMP; nothing opens the operator's database.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
)
from jarvis.memory import MemoryRecordRefused, MemoryTombstoned, SQLiteMemoryStore
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from tests.helpers import safe_cleanup

S = MemoryRecordStatus
K = MemoryRecordKind
MODEL = MemoryRecordActor.MODEL
TEXT = "the release branch is cut on fridays"


class GuardStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "memory.db"
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id

    def record(self, kind=K.FACT, text=TEXT, project=None) -> MemoryRecord:
        return MemoryRecord(
            kind=kind, text=text, subject_key="release",
            scope=MemoryRecordScope(project or self.alpha),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END, actor=MODEL,
            confidence=0.8, classification=DataClassification.INTERNAL,
        )

    def guard(self, kind=K.FACT, text=TEXT, *, provenance=Provenance.USER_STATED,
              taint=SessionTaint.USER_TYPED) -> RecordGuard:
        return RecordGuard(provenance, taint, (), kind, content_hash(text))

    def rows(self, sql: str) -> list[tuple]:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def counts(self) -> tuple[int, ...]:
        return tuple(
            self.rows(f"SELECT COUNT(*) FROM {table}")[0][0]  # nosec B608 - test constants
            for table in ("memory_records", "memory_record_events", "memory_record_guard")
        )

    def test_web_tainted_fact_ends_pending_rule(self) -> None:
        # Without a guard this FACT would auto-approve; the store re-decides.
        self.assertIs(self.store.insert_record(self.record(text="other fact")).status, S.APPROVED)
        view = self.store.insert_record(self.record(), self.guard(taint=SessionTaint.WEB))
        self.assertIs(view.status, S.PENDING)
        self.assertIs(view.record.kind, K.RULE)
        record_id = view.record.record_id
        self.assertIs(self.store.current_status(record_id), S.PENDING)
        stored = self.store.record_guard(record_id)
        self.assertIs(stored.session_taint, SessionTaint.WEB)
        self.assertIs(stored.proposed_kind, K.FACT)
        self.assertEqual(content_hash(TEXT), stored.content_hash)

    def test_store_rederives_instruction_hits(self) -> None:
        view = self.store.insert_record(
            self.record(text="from now on deploy on fridays"),
            self.guard(text="from now on deploy on fridays"),
        )
        self.assertEqual((S.PENDING, K.RULE), (view.status, view.record.kind))
        self.assertIn("en_from_now_on", self.store.record_guard(view.record.record_id).instruction_like)

    def test_refused_admission_writes_nothing(self) -> None:
        before = self.counts()
        with self.assertRaises(MemoryRecordRefused) as caught:
            self.store.insert_record(self.record(), self.guard(provenance=Provenance.MODEL_INFERRED))
        self.assertEqual("model_inferred_fact", caught.exception.category)
        with self.assertRaises(ValueError):
            self.store.insert_record(self.record(), self.guard(kind=K.LESSON))
        imported = replace(self.record(), origin=MemoryRecordOrigin.IMPORT)
        with self.assertRaises(ValueError):
            self.store.insert_record(imported, self.guard())
        self.assertEqual(before, self.counts())

    def test_admission_expiry_caps_a_lesson(self) -> None:
        view = self.store.insert_record(self.record(K.LESSON), self.guard(K.LESSON))
        self.assertIs(view.status, S.APPROVED)
        stored = view.record
        self.assertEqual(
            timedelta(days=90),
            datetime.fromisoformat(stored.expires_at) - datetime.fromisoformat(stored.created_at),
        )

    def test_reject_tombstone_is_scope_aware(self) -> None:
        first = self.store.insert_record(self.record(K.DECISION), self.guard(K.DECISION))
        self.assertIs(first.status, S.PENDING)
        self.store.record_event(first.record.record_id, S.REJECTED, MODEL, "no")
        digest = content_hash(TEXT)
        self.assertTrue(self.store.is_tombstoned(digest, self.alpha))
        self.assertFalse(self.store.is_tombstoned(digest, self.beta))
        self.assertFalse(self.store.is_tombstoned(digest, None))
        before = self.counts()
        with self.assertRaises(MemoryTombstoned):
            self.store.insert_record(self.record(K.DECISION), self.guard(K.DECISION))
        self.assertEqual(before, self.counts())
        # The explicit user path ignores tombstones; another project is untouched.
        self.store.insert_explicit_user_record(self.alpha, "decision", "internal", TEXT, "release")
        self.store.insert_record(self.record(K.DECISION, project=self.beta), self.guard(K.DECISION))
        # Guard-less writes are today's path (SECCAPTURE gates them).
        self.store.insert_record(self.record(K.DECISION))
        ((stored_hash, project, scope, reason, created, expires),) = self.rows(
            "SELECT content_hash, project_id, scope_kind, reason, created_at, expires_at "
            "FROM memory_tombstones"
        )
        self.assertEqual((digest, self.alpha, "project", "rejected"), (stored_hash, project, scope, reason))
        self.assertEqual(
            timedelta(days=180),
            datetime.fromisoformat(expires) - datetime.fromisoformat(created),
        )
        self.assertNotIn(TEXT, repr(self.rows("SELECT * FROM memory_tombstones")))

    def test_undo_tombstone_lasts_a_year(self) -> None:
        view = self.store.insert_record(self.record(), self.guard())
        self.assertIs(view.status, S.APPROVED)
        self.store.undo(view.record.record_id, MODEL)
        ((reason, created, expires),) = self.rows(
            "SELECT reason, created_at, expires_at FROM memory_tombstones"
        )
        self.assertEqual("retracted", reason)
        self.assertEqual(
            timedelta(days=365),
            datetime.fromisoformat(expires) - datetime.fromisoformat(created),
        )
        with self.assertRaises(MemoryTombstoned):
            self.store.insert_record(self.record(), self.guard())

    def test_expired_ignored_and_global_applies_everywhere(self) -> None:
        now = datetime.now(timezone.utc)
        digest, other = content_hash(TEXT), content_hash("x")
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.executemany(
                    "INSERT INTO memory_tombstones (content_hash, project_id, scope_kind, "
                    "reason, created_at, expires_at) VALUES (?, ?, ?, 'rejected', ?, ?)",
                    [
                        (digest, self.alpha, "project", now.isoformat(),
                         (now - timedelta(days=1)).isoformat()),
                        (other, None, "global", now.isoformat(), None),
                    ],
                )
        finally:
            connection.close()
        self.assertFalse(self.store.is_tombstoned(digest, self.alpha))
        self.store.insert_record(self.record(), self.guard())
        for project in (self.alpha, self.beta, None):
            with self.subTest(project=project):
                self.assertTrue(self.store.is_tombstoned(other, project))

    def test_record_guard_legacy_and_unknown(self) -> None:
        view = self.store.insert_record(self.record(K.LESSON))
        legacy = self.store.record_guard(view.record.record_id)
        self.assertEqual(
            (Provenance.LEGACY, SessionTaint.LEGACY, (), K.LESSON, content_hash(TEXT)),
            (legacy.provenance, legacy.session_taint, legacy.instruction_like,
             legacy.proposed_kind, legacy.content_hash),
        )
        self.assertEqual(0, self.counts()[2])
        self.assertIsNone(self.store.record_guard("mr-" + "0" * 32))


if __name__ == "__main__":
    unittest.main()
