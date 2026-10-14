"""MEMSUPERSEDE: the user's approval of a newer DECISION/PREFERENCE
supersedes the one older approved record on the same subject (Salih
2026-10-14, ARCHITECTURE manual gate step 5); undo brings the older back.

Every database is a throwaway in TEMP; nothing opens the operator's database.
"""

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
from jarvis.memory import MemoryRecordApproval, SQLiteMemoryStore
from jarvis.memory_consolidation import plan_consolidation
from tests.helpers import safe_cleanup

S = MemoryRecordStatus
K = MemoryRecordKind
NOW = "2026-10-20T00:00:00+00:00"


class ApproveSupersedeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.store = SQLiteMemoryStore(Path(temp.name) / "memory.db")
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id

    def insert(self, text, *, day, kind=K.DECISION, subject="deploy-day", project=None,
               conversation=None, origin=MemoryRecordOrigin.CAPTURE_SESSION_END, at=None) -> str:
        project = project or self.alpha
        record = MemoryRecord(
            kind=kind, text=text, subject_key=subject,
            scope=MemoryRecordScope(project, conversation),
            origin=origin, actor=MemoryRecordActor.MODEL, confidence=0.8,
            classification=DataClassification.INTERNAL,
            created_at=at or f"2026-10-{day:02d}T00:00:00+00:00",
        )
        return self.store.insert_record(record).record.record_id

    def approve(self, record_id, project=None) -> None:
        project = project or self.alpha
        self.store.approve_record(record_id, MemoryRecordApproval(project, record_id))

    def approved(self, text, **kw) -> str:
        record_id = self.insert(text, **kw)
        self.approve(record_id, kw.get("project"))
        return record_id

    def status(self, record_id):
        return self.store.current_status(record_id)

    def test_approve_supersedes_the_older_decision_and_preference(self) -> None:
        for kind in (K.DECISION, K.PREFERENCE):
            old = self.approved(f"old {kind.value}", day=1, kind=kind, subject=kind.value)
            new = self.approved(f"new {kind.value}", day=2, kind=kind, subject=kind.value)
            self.assertIs(S.SUPERSEDED, self.status(old), kind)
            self.assertIs(S.APPROVED, self.status(new), kind)
        live = {view.record.text for view in self.store.list_records(
            MemoryRecordScope(self.alpha, None), statuses=(S.APPROVED,))}
        self.assertEqual({"new decision", "new preference"}, live)

    def test_pending_newer_record_supersedes_nothing(self) -> None:
        old = self.approved("Fridays", day=1)
        self.insert("Thursdays", day=2)
        self.assertIs(S.APPROVED, self.status(old))

    def test_other_subject_scope_kind_or_project_is_untouched(self) -> None:
        old = self.approved("Fridays", day=1)
        conversation = self.store.create_conversation(self.alpha).id
        self.approved("other subject", day=2, subject="release-notes")
        self.approved("conversation scope", day=3, conversation=conversation)
        self.approved("a rule", day=4, kind=K.RULE)
        self.approved("other project", day=5, project=self.beta)
        self.assertIs(S.APPROVED, self.status(old))

    def test_a_different_lifetime_never_supersedes(self) -> None:
        # A1: a SESSION ("for now") approval must not hide the PERMANENT
        # project record from other conversations, nor the reverse.
        for kind in (K.DECISION, K.PREFERENCE):
            permanent = self.approved(f"Deploy on Fridays {kind.value}", day=1, kind=kind, subject=kind.value)
            session = self.approved(f"For now deploy on Thursdays {kind.value}", day=2, kind=kind,
                                    subject=kind.value)
            self.assertEqual(("permanent", "session"),
                             (self.store.record_lifetime(permanent).value, self.store.record_lifetime(session).value))
            self.assertIs(S.APPROVED, self.status(permanent), kind)
            later = self.approved(f"Deploy on Mondays {kind.value}", day=3, kind=kind, subject=kind.value)
            self.assertIs(S.APPROVED, self.status(session), kind)
            # Same lifetime still supersedes (one PERMANENT match).
            self.assertIs(S.SUPERSEDED, self.status(permanent), kind)
            self.assertIs(S.APPROVED, self.status(later), kind)

    def test_rule_and_explicit_user_older_records_are_never_superseded(self) -> None:
        rule = self.approved("rule one", day=1, kind=K.RULE)
        self.approved("rule two", day=2, kind=K.RULE)
        self.assertIs(S.APPROVED, self.status(rule))
        explicit = self.store.insert_explicit_user_record(
            self.alpha, "decision", "internal", "said by the user", "explicit").record.record_id
        self.approve(explicit)
        self.approved("captured later", day=2, subject="explicit", at="2999-01-01T00:00:00+00:00")
        self.assertIs(S.APPROVED, self.status(explicit))

    def test_several_or_no_older_matches_supersede_nothing(self) -> None:
        first = self.approved("Fridays", day=1)
        # Approving an OLDER record than the approved one is not "newer".
        older = self.insert("Mondays", day=1)
        second = self.insert("Tuesdays", day=2)
        self.approve(second)
        self.assertIs(S.SUPERSEDED, self.status(first))
        self.approve(older)
        self.assertIs(S.APPROVED, self.status(second))
        # Two approved older records: no automatic choice.
        third = self.approved("Thursdays", day=3)
        self.assertEqual({S.APPROVED}, {self.status(i) for i in (older, second, third)})

    def test_undo_restores_the_older_record(self) -> None:
        old = self.approved("Fridays", day=1)
        new = self.approved("Thursdays", day=2)
        self.store.undo(new, MemoryRecordActor.USER)
        self.assertIs(S.RETRACTED, self.status(new))
        self.assertIs(S.APPROVED, self.status(old))
        # The restored record is undoable again; nothing is resurrected twice.
        self.store.undo(old, MemoryRecordActor.USER)
        self.assertIs(S.RETRACTED, self.status(old))

    def test_model_import_and_consolidation_paths_never_supersede(self) -> None:
        old = self.approved("Fridays", day=1)
        imported = self.insert("Thursdays", day=2, origin=MemoryRecordOrigin.IMPORT)
        self.assertIs(S.PENDING, self.status(imported))
        newer = self.approved("Wednesdays", day=3)
        self.store.undo(newer, MemoryRecordActor.USER)
        views = self.store.list_records(MemoryRecordScope(self.alpha, None))
        plan = plan_consolidation([(v.record, v.status) for v in views], now=NOW)
        self.assertEqual((), plan.supersessions)
        self.store.consolidate_scope(self.alpha, NOW)
        self.assertIs(S.APPROVED, self.status(old))
        self.assertIs(S.PENDING, self.status(imported))


if __name__ == "__main__":
    unittest.main()
