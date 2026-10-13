"""Memory Doctor lesson freshness and contradiction findings (pure, no database)."""

from __future__ import annotations

import unittest

from jarvis.contracts import MemoryRecordKind, MemoryRecordScope, MemoryRecordStatus
from jarvis.memory_doctor import (
    LESSON_FRESHNESS_DAYS,
    FindingCode,
    RepairAction,
    RepairProposal,
    Severity,
)
from tests.test_memory_doctor import _run, _snapshot, _state

LESSON = MemoryRecordKind.LESSON
FRESH = "2026-10-01T00:00:00+00:00"
NEWER = "2026-10-10T00:00:00+00:00"
STALE = "2026-07-01T00:00:00+00:00"  # 104 days before NOW
EDGE = "2026-07-15T00:00:00+00:00"  # exactly 90 days before NOW


def _lesson(record_id, **overrides):
    values = dict(kind=LESSON, text="Run the suite once.", subject_key="test discipline", created_at=FRESH)
    values.update(overrides)
    return _state(record_id, **values)


def _of(code, *states):
    return [f for f in _run(_snapshot(*states)) if f.code is code]


class LessonStaleTests(unittest.TestCase):
    def test_window_is_ninety_days(self):
        self.assertEqual(LESSON_FRESHNESS_DAYS, 90)

    def test_old_unverified_lesson_is_flagged_with_expire_proposal(self):
        [finding] = _of(FindingCode.LESSON_STALE, _lesson("l1", created_at=STALE))
        self.assertEqual(finding.record_ids, ("l1",))
        self.assertIs(finding.severity, Severity.INFO)
        self.assertEqual(finding.summary_key, "memory_doctor.lesson_stale")
        self.assertEqual(finding.repair, RepairProposal(RepairAction.EXPIRE, ("l1",)))

    def test_fresh_lesson_is_not_flagged(self):
        self.assertEqual(_of(FindingCode.LESSON_STALE, _lesson("l1")), [])

    def test_window_boundary_is_not_flagged(self):
        self.assertEqual(_of(FindingCode.LESSON_STALE, _lesson("l1", created_at=EDGE)), [])

    def test_recent_verification_wins_over_old_creation(self):
        state = _lesson("l1", created_at=STALE, last_verified_at=FRESH)
        self.assertEqual(_of(FindingCode.LESSON_STALE, state), [])

    def test_old_verification_is_flagged(self):
        state = _lesson("l1", created_at=STALE, last_verified_at=STALE)
        self.assertEqual(len(_of(FindingCode.LESSON_STALE, state)), 1)

    def test_only_approved_lessons_are_checked(self):
        states = (
            _lesson("l1", created_at=STALE, status=MemoryRecordStatus.PENDING),
            _lesson("l2", created_at=STALE, status=MemoryRecordStatus.EXPIRED),
            _lesson("d1", created_at=STALE, kind=MemoryRecordKind.DECISION),
        )
        self.assertEqual(_of(FindingCode.LESSON_STALE, *states), [])

    def test_already_expired_lesson_is_left_to_stale(self):
        state = _lesson("l1", created_at=STALE, expires_at=FRESH)
        self.assertEqual(_of(FindingCode.LESSON_STALE, state), [])


class LessonContradictedTests(unittest.TestCase):
    def test_newer_decision_rule_or_lesson_with_other_text_contradicts(self):
        for kind in (MemoryRecordKind.DECISION, MemoryRecordKind.RULE, LESSON):
            with self.subTest(kind=kind):
                newer = _lesson("n1", kind=kind, text="Run it twice.", created_at=NEWER)
                [finding] = _of(FindingCode.LESSON_CONTRADICTED, _lesson("l1"), newer)
                self.assertEqual(finding.record_ids, ("l1", "n1"))
                self.assertIs(finding.severity, Severity.WARNING)
                self.assertEqual(finding.summary_key, "memory_doctor.lesson_contradicted")
                self.assertEqual(finding.repair, RepairProposal(RepairAction.EXPIRE, ("l1",)))

    def test_folded_equal_text_is_consistent(self):
        newer = _lesson("n1", kind=MemoryRecordKind.RULE, text="  run THE suite\nonce. ", created_at=NEWER)
        self.assertEqual(_of(FindingCode.LESSON_CONTRADICTED, _lesson("l1"), newer), [])

    def test_older_or_unapproved_or_fact_records_do_not_contradict(self):
        states = (
            _lesson("l1"),
            _lesson("o1", kind=MemoryRecordKind.DECISION, text="Other.", created_at=STALE),
            _lesson("p1", kind=MemoryRecordKind.RULE, text="Other.", created_at=NEWER,
                    status=MemoryRecordStatus.PENDING),
            _lesson("f1", kind=MemoryRecordKind.FACT, text="Other.", created_at=NEWER),
            _lesson("s1", kind=MemoryRecordKind.RULE, text="Other.", created_at=NEWER, subject_key="other"),
        )
        self.assertEqual(_of(FindingCode.LESSON_CONTRADICTED, *states), [])

    def test_same_subject_in_another_project_is_not_flagged(self):
        newer = _lesson("n1", kind=MemoryRecordKind.DECISION, text="Run it twice.", created_at=NEWER,
                        scope=MemoryRecordScope(project_id="other"))
        self.assertEqual(_of(FindingCode.LESSON_CONTRADICTED, _lesson("l1"), newer), [])

    def test_already_expired_records_are_left_to_stale(self):
        expired_rule = _lesson("n1", kind=MemoryRecordKind.RULE, text="Run it twice.", created_at=NEWER,
                               expires_at="2026-10-11T00:00:00+00:00")
        self.assertEqual(_of(FindingCode.LESSON_CONTRADICTED, _lesson("l1"), expired_rule), [])
        rule = _lesson("n1", kind=MemoryRecordKind.RULE, text="Run it twice.", created_at=NEWER)
        expired_lesson = _lesson("l1", expires_at=NEWER)
        self.assertEqual(_of(FindingCode.LESSON_CONTRADICTED, expired_lesson, rule), [])

    def test_only_the_older_lesson_is_proposed_for_expiry(self):
        newer = _lesson("l2", text="Run it twice.", created_at=NEWER)
        [finding] = _of(FindingCode.LESSON_CONTRADICTED, _lesson("l1"), newer)
        self.assertEqual(finding.repair.record_ids, ("l1",))


class DeterminismTests(unittest.TestCase):
    def test_same_snapshot_same_findings_in_code_order(self):
        states = (
            _lesson("l1", created_at=STALE),
            _lesson("n1", kind=MemoryRecordKind.RULE, text="Run it twice.", created_at=NEWER),
        )
        snapshot = _snapshot(*states)
        first = _run(snapshot)
        self.assertEqual(first, _run(snapshot))
        codes = [f.code for f in first]
        self.assertLess(codes.index(FindingCode.LESSON_STALE), codes.index(FindingCode.LESSON_CONTRADICTED))


if __name__ == "__main__":
    unittest.main()
