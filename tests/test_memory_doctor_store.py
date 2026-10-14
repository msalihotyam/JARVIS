"""V2.1b Memory Doctor store side: snapshot, FTS rebuild, proof-gated repairs.

Every database is a throwaway in TEMP; nothing opens the operator's database.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4

from jarvis import memory
from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
)
from jarvis.memory import MemoryRecordApproval, SQLiteMemoryStore
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from jarvis.memory_doctor import FindingCode, RepairAction, RepairProposal, diagnose
from jarvis.memory_filter import classify_candidate_text
from tests.helpers import safe_cleanup

S = MemoryRecordStatus
K = MemoryRecordKind
C = FindingCode
NEEDS_FTS = unittest.skipUnless(
    memory.MEMORY_RECORDS_FTS5_AVAILABLE, "SQLite build has no FTS5"
)


def days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class DoctorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "memory.db"
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id

    def add(self, text, *, kind=K.FACT, subject="tooling", project=None,
            conversation=None, **extra) -> str:
        record = MemoryRecord(
            kind=kind, text=text, subject_key=subject,
            scope=MemoryRecordScope(project or self.alpha, conversation),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.MODEL, confidence=0.8,
            classification=DataClassification.INTERNAL, **extra,
        )
        # A guard-less capture FACT/LESSON is now PENDING (SECCAPTURE): stand
        # in for Core's clean user_stated guard.
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (), kind,
                            content_hash(text)) if kind in (MemoryRecordKind.FACT, MemoryRecordKind.LESSON) else None
        return self.store.insert_record(record, guard).record.record_id

    def proof(self, record_id, project=None) -> MemoryRecordApproval:
        return MemoryRecordApproval(project or self.alpha, record_id, "doctor-repair")

    def sql(self, statement, args=()) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(statement, args)

    def findings(self, project=None, is_storable=None) -> set[tuple[str, tuple[str, ...]]]:
        snapshot = self.store.doctor_snapshot(project)
        storable = is_storable or (lambda text: classify_candidate_text(text).allowed)
        return {(f.code, f.record_ids) for f in diagnose(snapshot, is_storable=storable)}

    def test_seeded_snapshot_gives_expected_findings(self) -> None:
        self.add("the build uses uv for locking", subject="build")
        orphan = self.add("this chat prefers short answers", subject="style",
                          conversation=str(uuid4()))
        stale = self.add("ci runs on windows", subject="ci", created_at=days_ago(200))
        first = self.add("lint with ruff", subject="lint", created_at=days_ago(2))
        second = self.add("lint with flake8", subject="lint", created_at=days_ago(1))
        pending = self.add("adopt sqlite wal", kind=K.DECISION, subject="db",
                           created_at=days_ago(20))
        flagged = self.add("ship the beta on friday", subject="release")
        self.add("beta uses poetry", project=self.beta, subject="build")
        self.store.save_explicit_user_memory_item(self.alpha, "fact", "internal", "ship weekly")

        expected = {
            (C.ORPHAN_SCOPE, (orphan,)),
            (C.STALE, (stale,)),
            (C.CONFLICT_UNRESOLVED, tuple(sorted((first, second)))),
            (C.PENDING_TOO_OLD, (pending,)),
            (C.FILTER_VIOLATION, (flagged,)),
        }
        refuse_flagged = lambda text: "friday" not in text  # noqa: E731
        self.assertEqual(self.findings(self.alpha, refuse_flagged), expected)
        self.assertEqual(self.findings(None, refuse_flagged), expected)

        snapshot = self.store.doctor_snapshot(self.alpha)
        self.assertEqual({s.record.scope.project_id for s in snapshot.records}, {self.alpha})
        self.assertEqual(len(snapshot.legacy_items), 1)
        self.assertEqual(self.store.current_status(pending), S.PENDING)

    @NEEDS_FTS
    def test_rebuild_fts_fixes_drift(self) -> None:
        kept = self.add("the build uses uv for locking")
        dropped = self.add("tests run with unittest", subject="testing")
        self.add("beta uses poetry", project=self.beta)
        self.sql("INSERT INTO memory_records_fts (record_id, text, subject_key) "
                 "VALUES ('ghost', 'ghost', 'ghost')")
        self.assertIn((C.FTS_DRIFT, ()), self.findings())
        self.assertEqual(self.store.rebuild_fts(), 3)
        self.assertNotIn((C.FTS_DRIFT, ()), self.findings())

        self.sql("DELETE FROM memory_records_fts WHERE record_id = ?", (dropped,))
        self.assertIn((C.FTS_DRIFT, ()), self.findings())
        self.assertIn((C.FTS_DRIFT, ()), self.findings(self.alpha))
        self.assertNotIn((C.FTS_DRIFT, ()), self.findings(self.beta))

        self.assertEqual(self.store.rebuild_fts(), 3)

        self.assertNotIn((C.FTS_DRIFT, ()), self.findings())
        self.assertNotIn((C.FTS_DRIFT, ()), self.findings(self.alpha))
        scope = MemoryRecordScope(self.alpha, None)
        found = {view.record.record_id for view, _ in self.store.search_records("unittest", scope)}
        self.assertEqual(found, {dropped})
        self.assertNotIn(kept, found)

    def test_without_fts_rebuild_is_noop_and_no_drift(self) -> None:
        self.add("the build uses uv for locking")
        with mock.patch.object(memory, "memory_records_fts_available", return_value=False):
            self.assertIsNone(self.store.rebuild_fts())
            self.assertNotIn((C.FTS_DRIFT, ()), self.findings())

    def test_repair_without_proof_is_refused(self) -> None:
        rid = self.add("the build uses uv for locking")
        retract = RepairProposal(RepairAction.RETRACT, (rid,))
        for proof in (None, "user", (self.alpha, rid)):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                self.store.apply_doctor_repair(retract, proof)
        self.assertEqual(self.store.current_status(rid), S.APPROVED)

    def test_repair_refuses_wrong_proof_action_or_shape(self) -> None:
        rid = self.add("the build uses uv for locking")
        other = self.add("tests run with unittest", subject="testing")
        beta = self.add("beta uses poetry", project=self.beta)
        cases = [
            (RepairProposal(RepairAction.RETRACT, (rid,)), self.proof(other)),
            (RepairProposal(RepairAction.RETRACT, (beta,)), self.proof(beta)),
            (RepairProposal(RepairAction.REBUILD_FTS, ()), self.proof(rid)),
            (RepairProposal(RepairAction.EXPIRE, (rid, other)), self.proof(rid)),
            (RepairProposal("retract", (rid,)), self.proof(rid)),
            (RepairProposal(RepairAction.SUPERSEDE, (other, rid)), self.proof(rid)),
        ]
        for proposal, proof in cases:
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                self.store.apply_doctor_repair(proposal, proof)
        for record_id in (rid, other, beta):
            self.assertEqual(self.store.current_status(record_id), S.APPROVED)

    def test_proven_repairs_append_events(self) -> None:
        approved = self.add("the build uses uv for locking")
        pending = self.add("adopt sqlite wal", kind=K.DECISION, subject="db")
        stale = self.add("adopt wal mode", kind=K.DECISION, subject="wal")
        old = self.add("lint with flake8", subject="lint")
        new = self.add("lint with ruff", subject="lint", supersedes=old)
        P, A = RepairProposal, RepairAction
        self.assertEqual(self.store.apply_doctor_repair(P(A.RETRACT, (approved,)), self.proof(approved)), S.RETRACTED)
        self.assertEqual(self.store.apply_doctor_repair(P(A.RETRACT, (pending,)), self.proof(pending)), S.REJECTED)
        self.assertEqual(self.store.apply_doctor_repair(P(A.EXPIRE, (stale,)), self.proof(stale)), S.EXPIRED)
        self.assertEqual(self.store.apply_doctor_repair(P(A.SUPERSEDE, (new, old)), self.proof(old)), S.SUPERSEDED)
        self.assertEqual(self.store.current_status(new), S.APPROVED)
        with self.assertRaises(ValueError):  # terminal records never move again
            self.store.apply_doctor_repair(P(A.EXPIRE, (approved,)), self.proof(approved))


if __name__ == "__main__":
    unittest.main()
