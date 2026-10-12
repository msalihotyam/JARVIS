"""V2.1 consolidation planner (pure; no I/O, no database)."""

from __future__ import annotations

import dataclasses
import unittest

from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
)
from jarvis.memory_consolidation import (
    ConsolidationConflict,
    ConsolidationEvent,
    ConsolidationPlan,
    Merge,
    Supersession,
    plan_consolidation,
)

NOW = "2026-10-13T12:00:00+00:00"
APPROVED = MemoryRecordStatus.APPROVED
PENDING = MemoryRecordStatus.PENDING
SUPERSEDED = MemoryRecordStatus.SUPERSEDED
EXPIRED = MemoryRecordStatus.EXPIRED


def _record(record_id: str, hour: int, **overrides) -> MemoryRecord:
    values = dict(
        record_id=record_id,
        kind=MemoryRecordKind.FACT,
        text="The build uses Python 3.12.",
        subject_key="build python",
        scope=MemoryRecordScope(project_id=overrides.pop("project_id", "jarvis")),
        origin=MemoryRecordOrigin.EXPLICIT_USER,
        actor=MemoryRecordActor.USER,
        confidence=1.0,
        classification=DataClassification.INTERNAL,
        created_at=f"2026-10-{hour // 24 + 1:02d}T{hour % 24:02d}:00:00+00:00",
    )
    values.update(overrides)
    return MemoryRecord(**values)


def _apply(pairs, plan: ConsolidationPlan):
    """What the store does: append the planned status, delete nothing."""
    status = {event.record_id: event.status for event in plan.events}
    return [(record, status.get(record.record_id, current)) for record, current in pairs]


class DedupTests(unittest.TestCase):
    def test_duplicate_keeps_newest_and_supersedes_older(self) -> None:
        old = _record("old", 1)
        new = _record("new", 2, text="  the BUILD uses   python 3.12. ")
        plan = plan_consolidation([(old, APPROVED), (new, APPROVED)], now=NOW)
        self.assertEqual(plan.events, (ConsolidationEvent("old", SUPERSEDED, "duplicate"),))
        self.assertEqual(plan.supersessions, (Supersession("old", "new", "duplicate"),))
        self.assertEqual(plan.merges, (Merge("new", ("old",)),))
        self.assertEqual(plan.conflicts, ())

    def test_approved_original_outranks_newer_pending_duplicate(self) -> None:
        old = _record("old", 1)
        new = _record("new", 2)
        plan = plan_consolidation([(old, APPROVED), (new, PENDING)], now=NOW)
        self.assertEqual(plan.supersessions, (Supersession("new", "old", "duplicate"),))

    def test_pending_duplicate_never_supersedes_pending(self) -> None:
        user = _record("user", 1, kind=MemoryRecordKind.RULE)
        model = _record(
            "model", 2, kind=MemoryRecordKind.RULE,
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END, actor=MemoryRecordActor.MODEL,
        )
        plan = plan_consolidation([(user, PENDING), (model, PENDING)], now=NOW)
        self.assertEqual((plan.events, plan.supersessions, plan.merges), ((), (), ()))
        self.assertEqual(plan.conflicts, (ConsolidationConflict("model", "user", "duplicate"),))

    def test_approved_duplicate_rule_is_only_proposed(self) -> None:
        old = _record("old", 1, kind=MemoryRecordKind.RULE)
        new = _record("new", 2, kind=MemoryRecordKind.RULE)
        plan = plan_consolidation([(old, APPROVED), (new, APPROVED)], now=NOW)
        self.assertEqual((plan.events, plan.supersessions, plan.merges), ((), (), ()))
        self.assertEqual(plan.conflicts, (ConsolidationConflict("new", "old", "duplicate"),))


class SupersessionTests(unittest.TestCase):
    def test_newer_approved_fact_and_lesson_supersede_older(self) -> None:
        for kind in (MemoryRecordKind.FACT, MemoryRecordKind.LESSON):
            with self.subTest(kind=kind):
                old = _record("old", 1, kind=kind)
                new = _record("new", 2, kind=kind, text="The build uses Python 3.13.")
                plan = plan_consolidation([(new, APPROVED), (old, APPROVED)], now=NOW)
                self.assertEqual(plan.events, (ConsolidationEvent("old", SUPERSEDED, "newer"),))
                self.assertEqual(plan.supersessions, (Supersession("old", "new", "newer"),))
                self.assertEqual(plan.merges, ())

    def test_pending_fact_never_supersedes_approved(self) -> None:
        old = _record("old", 1)
        new = _record("new", 2, text="The build uses Python 3.13.", origin=MemoryRecordOrigin.IMPORT)
        plan = plan_consolidation([(old, APPROVED), (new, PENDING)], now=NOW)
        self.assertEqual(plan, ConsolidationPlan())

    def test_approved_rule_decision_preference_are_never_auto_superseded(self) -> None:
        for kind in (MemoryRecordKind.RULE, MemoryRecordKind.DECISION, MemoryRecordKind.PREFERENCE):
            with self.subTest(kind=kind):
                old = _record("old", 1, kind=kind)
                new = _record("new", 2, kind=kind, text="The build uses Python 3.13.")
                plan = plan_consolidation([(old, APPROVED), (new, APPROVED)], now=NOW)
                self.assertEqual((plan.events, plan.supersessions, plan.merges), ((), (), ()))
                self.assertEqual(plan.conflicts, (ConsolidationConflict("new", "old", "conflict"),))

    def test_groups_never_cross_project_conversation_or_classification(self) -> None:
        base = _record("base", 1)
        others = [
            _record("p2", 2, project_id="other"),
            _record("c2", 3, scope=MemoryRecordScope(project_id="jarvis", conversation_id="conv-1")),
            _record("s2", 4, classification=DataClassification.SENSITIVE),
            _record("k2", 5, subject_key="other subject"),
        ]
        pairs = [(base, APPROVED)] + [(record, APPROVED) for record in others]
        self.assertEqual(plan_consolidation(pairs, now=NOW), ConsolidationPlan())


class ExpiryTests(unittest.TestCase):
    def test_expired_strictly_before_now(self) -> None:
        gone = _record("gone", 1, expires_at="2026-10-13T11:59:59+00:00")
        edge = _record("edge", 1, subject_key="edge", expires_at=NOW)
        plan = plan_consolidation([(gone, PENDING), (edge, APPROVED)], now=NOW)
        self.assertEqual(plan.events, (ConsolidationEvent("gone", EXPIRED, "expired"),))
        self.assertEqual(plan.supersessions, ())

    def test_expired_record_takes_no_part_in_dedup(self) -> None:
        old = _record("old", 1)
        new = _record("new", 2, expires_at="2026-10-13T00:00:00+00:00")
        plan = plan_consolidation([(old, APPROVED), (new, APPROVED)], now=NOW)
        self.assertEqual(plan.events, (ConsolidationEvent("new", EXPIRED, "expired"),))
        self.assertEqual(plan.supersessions, ())


class IdempotenceTests(unittest.TestCase):
    def test_terminal_statuses_are_ignored(self) -> None:
        old = _record("old", 1, expires_at="2026-10-02T00:00:00+00:00")
        new = _record("new", 2)
        for status in (SUPERSEDED, EXPIRED, MemoryRecordStatus.REJECTED, MemoryRecordStatus.RETRACTED):
            with self.subTest(status=status):
                plan = plan_consolidation([(old, status), (new, APPROVED)], now=NOW)
                self.assertEqual(plan, ConsolidationPlan())

    def test_replanning_an_applied_plan_is_empty(self) -> None:
        rule_text = "Never push main."
        pairs = [
            (_record("f1", 1), APPROVED),
            (_record("f2", 2), PENDING),
            (_record("f3", 3, text="The build uses Python 3.13."), APPROVED),
            (_record("f4", 4, text="The build uses Python 3.14."), PENDING),
            (_record("r1", 1, kind=MemoryRecordKind.RULE, subject_key="push", text=rule_text), APPROVED),
            (_record("r2", 2, kind=MemoryRecordKind.RULE, subject_key="push", text=rule_text), APPROVED),
            (_record("r3", 3, kind=MemoryRecordKind.RULE, subject_key="push", text="Push freely."), PENDING),
            (_record("x1", 1, subject_key="old", expires_at="2026-10-05T00:00:00+00:00"), APPROVED),
        ]
        first = plan_consolidation(pairs, now=NOW)
        self.assertEqual(
            {(e.record_id, e.status) for e in first.events},
            {("f1", SUPERSEDED), ("f2", SUPERSEDED), ("x1", EXPIRED)},
        )
        self.assertEqual(
            {(s.record_id, s.superseded_by) for s in first.supersessions},
            {("f1", "f3"), ("f2", "f1")},
        )
        self.assertEqual(
            first.conflicts,
            (ConsolidationConflict("r2", "r1", "duplicate"), ConsolidationConflict("r3", "r2", "conflict")),
        )
        second = plan_consolidation(_apply(pairs, first), now=NOW)
        self.assertEqual((second.events, second.supersessions, second.merges), ((), (), ()))
        self.assertEqual(second.conflicts, first.conflicts)


class FailClosedTests(unittest.TestCase):
    def test_malformed_input_raises(self) -> None:
        record = _record("a", 1)
        bad_inputs = [
            [record],
            [(record,)],
            [(record, "approved")],
            [("a", APPROVED)],
            [(record, APPROVED), (_record("a", 2, text="other"), PENDING)],
        ]
        for pairs in bad_inputs:
            with self.subTest(pairs=pairs), self.assertRaises(ValueError):
                plan_consolidation(pairs, now=NOW)

    def test_bad_now_raises(self) -> None:
        for now in ("2026-10-13T12:00:00", "2026-10-13T15:00:00+03:00", "yesterday", None):
            with self.subTest(now=now), self.assertRaises(ValueError):
                plan_consolidation([], now=now)

    def test_plan_has_no_delete_concept(self) -> None:
        names = {f.name for cls in (ConsolidationPlan, ConsolidationEvent, Supersession, Merge) for f in dataclasses.fields(cls)}
        self.assertFalse({name for name in names if "delet" in name})
        statuses = {event.status for event in plan_consolidation(
            [(_record("a", 1), APPROVED), (_record("b", 2), APPROVED)], now=NOW
        ).events}
        self.assertLessEqual(statuses, {SUPERSEDED, EXPIRED})


if __name__ == "__main__":
    unittest.main()
