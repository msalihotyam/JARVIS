"""SECCAPFIX F3: consolidation leaves out tombstoned steps, never stalls on them."""

from __future__ import annotations

import tempfile
import unittest
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
from jarvis.memory import ConsolidationPlanRejected, SQLiteMemoryStore
from jarvis.memory_consolidation import ConsolidationEvent, ConsolidationPlan, Supersession
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from tests.helpers import safe_cleanup

S = MemoryRecordStatus
NOW = "2026-10-20T00:00:00+00:00"


class ConsolidationTombstoneTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.store = SQLiteMemoryStore(Path(temp.name) / "memory.db")
        self.alpha = self.store.create_project("Alpha").id

    def insert(self, text, day, subject="tooling", expires_at=None):
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (),
                            MemoryRecordKind.FACT, content_hash(text))
        return self.store.insert_record(MemoryRecord(
            kind=MemoryRecordKind.FACT, text=text, subject_key=subject,
            scope=MemoryRecordScope(self.alpha, None),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.MODEL, confidence=0.8,
            classification=DataClassification.INTERNAL,
            created_at=f"2026-10-{day:02d}T00:00:00+00:00", expires_at=expires_at,
        ), guard=guard).record.record_id

    def tombstone(self, text):
        # An undone record tombstones its text; the twin carries the same content.
        self.store.undo(self.insert(text, 9, subject="twin"), MemoryRecordActor.USER)

    def test_tombstoned_survivor_step_is_skipped_and_the_rest_applies(self) -> None:
        old, new = self.insert("the build uses pip", 1), self.insert("the build uses uv", 2)
        a, b = self.insert("we deploy on monday", 1, "deploy"), self.insert("we deploy on friday", 2, "deploy")
        gone = self.insert("the cache lives in tmp", 1, "cache", expires_at="2026-10-05T00:00:00+00:00")
        self.tombstone("the build uses uv")
        result = self.store.consolidate_scope(self.alpha, NOW)
        self.assertEqual(result, {"expired": 1, "superseded": 1, "merged": 0, "conflicts": 0,
                                  "skipped_tombstoned": 1})
        self.assertIs(self.store.current_status(old), S.APPROVED)
        self.assertIs(self.store.current_status(new), S.APPROVED)
        self.assertIs(self.store.current_status(a), S.SUPERSEDED)
        self.assertIs(self.store.current_status(gone), S.EXPIRED)
        # Idempotent: the left-out step is left out again, nothing else to do.
        self.assertEqual(self.store.consolidate_scope(self.alpha, NOW)["skipped_tombstoned"], 1)
        self.assertIs(self.store.current_status(b), S.APPROVED)

    def test_tombstoned_loser_is_still_retired(self) -> None:
        # Retiring tombstoned content only tightens: that step stays (as before).
        old, new = self.insert("the build uses pip", 1), self.insert("the build uses uv", 2)
        self.tombstone("the build uses pip")
        result = self.store.consolidate_scope(self.alpha, NOW)
        self.assertEqual((result["superseded"], result["skipped_tombstoned"]), (1, 0))
        self.assertIs(self.store.current_status(old), S.SUPERSEDED)
        self.assertIs(self.store.current_status(new), S.APPROVED)

    def test_duplicate_merge_onto_tombstoned_survivor_is_skipped(self) -> None:
        loser, kept = self.insert("the build uses uv", 1), self.insert("The build uses  UV", 2)
        self.tombstone("The build uses  UV")
        result = self.store.consolidate_scope(self.alpha, NOW)
        self.assertEqual((result["superseded"], result["merged"], result["skipped_tombstoned"]), (0, 0, 1))
        self.assertIs(self.store.current_status(loser), S.APPROVED)
        self.assertIs(self.store.current_status(kept), S.APPROVED)

    def test_apply_still_rejects_a_tombstoned_survivor_whole_plan(self) -> None:
        old, new = self.insert("the build uses pip", 1), self.insert("the build uses uv", 2)
        a, b = self.insert("we deploy on monday", 1, "deploy"), self.insert("we deploy on friday", 2, "deploy")
        self.tombstone("the build uses uv")
        plan = ConsolidationPlan(
            events=tuple(ConsolidationEvent(i, S.SUPERSEDED, "newer") for i in (old, a)),
            supersessions=(Supersession(old, new, "newer"), Supersession(a, b, "newer")),
        )
        with self.assertRaisesRegex(ConsolidationPlanRejected, "tombstoned"):
            self.store.apply_consolidation_plan(plan)
        self.assertIs(self.store.current_status(a), S.APPROVED)


if __name__ == "__main__":
    unittest.main()
