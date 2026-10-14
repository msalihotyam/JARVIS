"""V2.1a M5: consolidation plans applied to the store through status events.

Every database is a throwaway in TEMP; nothing opens the operator's database.
"""

from __future__ import annotations

import sqlite3
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
from jarvis.memory import (
    ConsolidationPlanRejected,
    MemoryRecordApproval,
    SQLiteMemoryStore,
)
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from jarvis.memory_consolidation import (
    ConsolidationEvent,
    ConsolidationPlan,
    Supersession,
)
from tests.helpers import safe_cleanup

S = MemoryRecordStatus
K = MemoryRecordKind
NOW = "2030-01-01T00:00:00+00:00"
PAST = "2029-01-01T00:00:00+00:00"
ZERO = {"expired": 0, "superseded": 0, "merged": 0, "conflicts": 0, "skipped_tombstoned": 0}


class ConsolidationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "memory.db"
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id

    def insert(self, kind=K.FACT, text="the build uses uv", *, day=1, project=None,
               subject="tooling", approve=False, **extra) -> str:
        project = project or self.alpha
        record = MemoryRecord(
            kind=kind, text=text, subject_key=subject,
            scope=MemoryRecordScope(project, None),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.MODEL, confidence=0.8,
            classification=DataClassification.INTERNAL,
            created_at=f"2026-10-{day:02d}T00:00:00+00:00", **extra,
        )
        # A guard-less capture FACT/LESSON is now PENDING (SECCAPTURE): stand
        # in for Core's clean user_stated guard.
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (), kind,
                            content_hash(text)) if kind in (K.FACT, K.LESSON) else None
        record_id = self.store.insert_record(record, guard).record.record_id
        if approve:
            self.store.approve_record(record_id, MemoryRecordApproval(project, record_id))
        return record_id

    def counts(self) -> tuple[int, int]:
        connection = sqlite3.connect(self.database)
        try:
            return (
                connection.execute("SELECT COUNT(*) FROM memory_record_events").fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM memory_record_supersessions"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    def newest_event(self, record_id: str) -> tuple[str, str]:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT status, actor FROM memory_record_events WHERE record_id = ? "
                "ORDER BY event_id DESC LIMIT 1", (record_id,),
            ).fetchone()
        finally:
            connection.close()

    def assert_refused(self, plan, *, actor=None) -> None:
        before = self.counts()
        with self.assertRaises(ConsolidationPlanRejected):
            if actor is None:
                self.store.apply_consolidation_plan(plan)
            else:
                self.store.apply_consolidation_plan(plan, actor=actor)
        self.assertEqual(self.counts(), before)

    def test_expired_fact_gets_expired_event_as_consolidation(self) -> None:
        gone = self.insert(expires_at=PAST)
        result = self.store.consolidate_scope(self.alpha, NOW)
        self.assertEqual(result, {**ZERO, "expired": 1})
        self.assertEqual(self.newest_event(gone), ("expired", "consolidation"))

    def test_duplicate_fact_superseded_with_row_and_second_call_is_noop(self) -> None:
        old = self.insert(day=1)
        new = self.insert(day=2, text="  The build uses   UV ")
        first = self.store.consolidate_scope(self.alpha, NOW)
        self.assertEqual(first, {**ZERO, "superseded": 1, "merged": 1})
        self.assertIs(self.store.current_status(old), S.SUPERSEDED)
        self.assertIs(self.store.current_status(new), S.APPROVED)
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT new_record_id, actor FROM memory_record_supersessions "
                "WHERE old_record_id = ?", (old,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (new, "consolidation"))
        after_first = self.counts()
        self.assertEqual(self.store.consolidate_scope(self.alpha, NOW), ZERO)
        self.assertEqual(self.counts(), after_first)

    def test_approved_rule_pair_is_untouched(self) -> None:
        old = self.insert(K.RULE, "never push main", day=1, approve=True)
        new = self.insert(K.RULE, "push main freely", day=2, approve=True)
        before = self.counts()
        for _ in range(2):
            self.assertEqual(
                self.store.consolidate_scope(self.alpha, NOW), {**ZERO, "conflicts": 1}
            )
        self.assertEqual(self.counts(), before)
        self.assertIs(self.store.current_status(old), S.APPROVED)
        self.assertIs(self.store.current_status(new), S.APPROVED)

    def test_handcrafted_plan_cannot_touch_approved_rule(self) -> None:
        old = self.insert(K.RULE, "never push main", day=1, approve=True)
        new = self.insert(K.RULE, "push main freely", day=2, approve=True)
        self.assert_refused(ConsolidationPlan(
            events=(ConsolidationEvent(old, S.SUPERSEDED, "newer"),),
            supersessions=(Supersession(old, new, "newer"),),
        ))
        self.assert_refused(ConsolidationPlan(
            events=(ConsolidationEvent(old, S.EXPIRED, "expired"),),
        ))
        self.assertIs(self.store.current_status(old), S.APPROVED)

    def test_one_invalid_step_rolls_back_the_whole_plan(self) -> None:
        stale = self.insert(subject="a", expires_at=PAST)
        old = self.insert(subject="b", day=1)
        new = self.insert(subject="b", day=2, text="the build uses pip")
        retracted = self.insert(subject="c")
        self.store.undo(retracted, MemoryRecordActor.USER)
        valid_events = (
            ConsolidationEvent(stale, S.EXPIRED, "expired"),
            ConsolidationEvent(old, S.SUPERSEDED, "newer"),
        )
        valid_supersessions = (Supersession(old, new, "newer"),)
        for bad in (retracted, "rec_" + "0" * 32, "not a record id"):
            with self.subTest(bad=bad):
                self.assert_refused(ConsolidationPlan(
                    events=(*valid_events, ConsolidationEvent(bad, S.EXPIRED, "expired")),
                    supersessions=valid_supersessions,
                ))
        self.assertIs(self.store.current_status(stale), S.APPROVED)
        self.assertIs(self.store.current_status(old), S.APPROVED)
        self.assertIs(self.store.current_status(retracted), S.RETRACTED)

    def test_explicit_user_record_is_not_superseded(self) -> None:
        user = self.store.insert_explicit_user_record(
            self.alpha, "fact", "internal", "the build uses uv", "tooling"
        ).record.record_id
        newer = self.insert(day=28, text="the build uses pip")
        self.assert_refused(ConsolidationPlan(
            events=(ConsolidationEvent(user, S.SUPERSEDED, "newer"),),
            supersessions=(Supersession(user, newer, "newer"),),
        ))
        self.assertIs(self.store.current_status(user), S.APPROVED)

    def test_cross_project_plan_is_refused_and_beta_unaffected(self) -> None:
        alpha_old = self.insert(subject="x", day=1)
        beta_new = self.insert(subject="x", day=2, project=self.beta)
        beta_gone = self.insert(project=self.beta, expires_at=PAST)
        self.assert_refused(ConsolidationPlan(
            events=(
                ConsolidationEvent(alpha_old, S.SUPERSEDED, "newer"),
                ConsolidationEvent(beta_gone, S.EXPIRED, "expired"),
            ),
            supersessions=(Supersession(alpha_old, beta_new, "newer"),),
        ))
        self.assertEqual(self.store.consolidate_scope(self.alpha, NOW), ZERO)
        self.assertIs(self.store.current_status(beta_gone), S.APPROVED)

    def test_bad_actor_and_malformed_plans_are_refused(self) -> None:
        gone = self.insert(expires_at=PAST)
        event = ConsolidationEvent(gone, S.EXPIRED, "expired")
        self.assert_refused(ConsolidationPlan(events=(event,)), actor="user")
        for plan in (
            None,
            {"events": (event,)},
            ConsolidationPlan(events=[event]),
            ConsolidationPlan(events=(event, event)),
            ConsolidationPlan(events=(ConsolidationEvent(gone, "expired", "expired"),)),
            ConsolidationPlan(events=(ConsolidationEvent([gone], S.EXPIRED, "expired"),)),
            ConsolidationPlan(supersessions=(Supersession(gone, ["x"], "newer"),)),
            ConsolidationPlan(events=(ConsolidationEvent(gone, S.RETRACTED, "x"),)),
            ConsolidationPlan(events=(ConsolidationEvent(gone, S.SUPERSEDED, "x"),)),
        ):
            with self.subTest(plan=plan):
                self.assert_refused(plan)
        self.assertIs(self.store.current_status(gone), S.APPROVED)

    def test_pending_duplicate_of_approved_rule_fails_closed(self) -> None:
        # The planner proposes PENDING -> SUPERSEDED, which the store's
        # transition table refuses; nothing is written (see report).
        approved = self.insert(K.RULE, "never push main", day=1, approve=True)
        pending = self.insert(K.RULE, "never push main", day=2)
        before = self.counts()
        with self.assertRaises(ConsolidationPlanRejected):
            self.store.consolidate_scope(self.alpha, NOW)
        self.assertEqual(self.counts(), before)
        self.assertIs(self.store.current_status(approved), S.APPROVED)
        self.assertIs(self.store.current_status(pending), S.PENDING)


if __name__ == "__main__":
    unittest.main()
