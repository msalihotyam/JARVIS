"""V2.1a M3: task-targeted memory selection (pure; no database, no model)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordScopeKind,
    MemoryRecordStatus,
)
from jarvis.memory_retrieval import (
    STALE_AFTER,
    RecordWithStatus,
    TaskQuery,
    select_for_task,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
HOME = "proj-home"
OTHER = "proj-other"
CONV = "conv-1"
APPROVED = MemoryRecordStatus.APPROVED


def at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def record(
    record_id: str,
    *,
    kind: MemoryRecordKind = MemoryRecordKind.FACT,
    text: str = "the build uses python unittest",
    subject: str = "build",
    project: str = HOME,
    conversation: str | None = None,
    confidence: float = 1.0,
    created_days_ago: float = 1,
    verified_days_ago: float | None = None,
    expires_at: str | None = None,
    supersedes: str | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        kind=kind,
        text=text,
        subject_key=subject,
        scope=MemoryRecordScope(project, conversation),
        origin=MemoryRecordOrigin.EXPLICIT_USER,
        actor=MemoryRecordActor.USER,
        confidence=confidence,
        classification=DataClassification.INTERNAL,
        record_id=record_id,
        created_at=at(created_days_ago),
        last_verified_at=None if verified_days_ago is None else at(verified_days_ago),
        expires_at=expires_at,
        supersedes=supersedes,
    )


def select(
    items,
    *,
    text: str = "build",
    global_readonly: bool = False,
    scope: MemoryRecordScope | None = None,
    limit: int = 10,
    live_projects=frozenset({HOME, OTHER}),
    live_conversations=frozenset({CONV}),
):
    return select_for_task(
        [
            item if isinstance(item, RecordWithStatus) else RecordWithStatus(item, APPROVED)
            for item in items
        ],
        TaskQuery(text, global_readonly=global_readonly),
        scope or MemoryRecordScope(HOME, CONV),
        now=NOW,
        limit=limit,
        live_project_ids=live_projects,
        live_conversation_ids=live_conversations,
    )


def ids(selected) -> list[str]:
    return [s.record.record_id for s in selected]


class ScopeTests(unittest.TestCase):
    def test_cross_project_record_is_never_chosen_without_the_global_flag(self) -> None:
        selection = select([record("own"), record("foreign", project=OTHER)])
        self.assertEqual(ids(selection.chosen), ["own"])
        for bucket in (selection.stale, selection.conflicts, selection.orphans):
            self.assertNotIn("foreign", ids(bucket))

    def test_global_flag_admits_other_projects_labelled_with_their_source(self) -> None:
        selection = select(
            [record("own"), record("foreign", project=OTHER)], global_readonly=True
        )
        labels = {s.record.record_id: s.source_project_id for s in selection.chosen}
        self.assertEqual(labels, {"own": None, "foreign": OTHER})

    def test_global_readonly_scope_without_the_flag_chooses_nothing(self) -> None:
        scope = MemoryRecordScope(None, CONV, MemoryRecordScopeKind.GLOBAL_READONLY)
        self.assertEqual(select([record("own")], scope=scope).chosen, ())
        chosen = select([record("own")], scope=scope, global_readonly=True).chosen
        self.assertEqual([s.source_project_id for s in chosen], [HOME])

    def test_conversation_scoped_record_only_in_its_conversation(self) -> None:
        items = [record("mine", conversation=CONV), record("theirs", conversation="conv-2")]
        selection = select(items, live_conversations=frozenset({CONV, "conv-2"}))
        self.assertEqual(ids(selection.chosen), ["mine"])
        elsewhere = select(
            items,
            scope=MemoryRecordScope(HOME, "conv-3"),
            live_conversations=frozenset({CONV, "conv-2", "conv-3"}),
        )
        self.assertEqual(elsewhere.chosen, ())


class ValidityTests(unittest.TestCase):
    def test_only_approved_unexpired_records_are_chosen(self) -> None:
        items = [RecordWithStatus(record("ok"), APPROVED)] + [
            RecordWithStatus(record(status.value), status)
            for status in MemoryRecordStatus
            if status is not APPROVED
        ]
        items.append(
            RecordWithStatus(record("expired-at", expires_at=at(0.5)), APPROVED)
        )
        items.append(
            RecordWithStatus(record("future-expiry", expires_at=at(-5)), APPROVED)
        )
        selection = select(items)
        self.assertEqual(ids(selection.chosen), ["future-expiry", "ok"])

    def test_superseded_by_pointer_is_never_chosen(self) -> None:
        old = record("old", kind=MemoryRecordKind.DECISION, created_days_ago=5)
        new = record(
            "new", kind=MemoryRecordKind.DECISION, created_days_ago=10, supersedes="old"
        )
        selection = select([old, new])
        self.assertEqual(ids(selection.chosen), ["new"])
        self.assertEqual(selection.conflicts, ())

    def test_pointer_from_an_unapproved_record_does_not_supersede(self) -> None:
        old = record("old")
        pending = RecordWithStatus(
            record("new", supersedes="old"), MemoryRecordStatus.PENDING
        )
        self.assertEqual(ids(select([old, pending]).chosen), ["old"])

    def test_pointer_from_another_project_does_not_supersede(self) -> None:
        own = record("own")
        foreign = record("foreign", project=OTHER, supersedes="own")
        self.assertEqual(ids(select([own, foreign]).chosen), ["own"])

    def test_pointer_from_another_conversation_does_not_supersede(self) -> None:
        old = record("old", kind=MemoryRecordKind.DECISION)
        new = record(
            "new", kind=MemoryRecordKind.DECISION, conversation="conv-2", supersedes="old"
        )
        live = frozenset({CONV, "conv-2"})
        self.assertEqual(ids(select([old, new], live_conversations=live).chosen), ["old"])
        in_conv_2 = select(
            [old, new], scope=MemoryRecordScope(HOME, "conv-2"), live_conversations=live
        )
        self.assertEqual(ids(in_conv_2.chosen), ["new"])

    def test_pointer_needs_same_subject_and_a_fact_never_retires_a_rule(self) -> None:
        rule = record("rule", kind=MemoryRecordKind.RULE)
        other_subject = record(
            "x", kind=MemoryRecordKind.RULE, subject="deploy", supersedes="rule"
        )
        fact = record("fact", subject="build", supersedes="rule")
        selection = select([rule, other_subject, fact], text="")
        self.assertEqual(sorted(ids(selection.chosen)), ["fact", "rule", "x"])


class DecisionTests(unittest.TestCase):
    def test_newest_decision_wins_and_older_is_a_visible_conflict(self) -> None:
        older = record(
            "older", kind=MemoryRecordKind.DECISION, subject="DB engine",
            text="use postgres", created_days_ago=10, confidence=1.0,
        )
        newer = record(
            "newer", kind=MemoryRecordKind.RULE, subject="db-engine",
            text="use sqlite", created_days_ago=2, confidence=0.5,
        )
        selection = select([older, newer], text="")
        self.assertEqual(ids(selection.chosen), ["newer"])
        self.assertEqual(ids(selection.conflicts), ["older"])

    def test_facts_on_one_subject_do_not_conflict(self) -> None:
        selection = select([record("a"), record("b", created_days_ago=3)])
        self.assertEqual(sorted(ids(selection.chosen)), ["a", "b"])
        self.assertEqual(selection.conflicts, ())


class ReportTests(unittest.TestCase):
    def test_stale_is_reported_and_still_eligible(self) -> None:
        old_days = STALE_AFTER.days + 1
        selection = select(
            [record("stale", created_days_ago=old_days), record("fresh")]
        )
        self.assertEqual(ids(selection.stale), ["stale"])
        self.assertIn("stale", ids(selection.chosen))
        reverified = select(
            [record("stale", created_days_ago=old_days, verified_days_ago=1)]
        )
        self.assertEqual(reverified.stale, ())

    def test_orphans_are_reported_and_never_chosen(self) -> None:
        items = [
            record("gone-project", project=OTHER),
            record("gone-conversation", conversation=CONV),
            record("alive"),
        ]
        selection = select(
            items, global_readonly=True,
            live_projects=frozenset({HOME}), live_conversations=frozenset(),
        )
        self.assertEqual(ids(selection.chosen), ["alive"])
        self.assertEqual(ids(selection.orphans), ["gone-conversation", "gone-project"])


class ScoreTests(unittest.TestCase):
    def test_score_orders_by_relevance_freshness_confidence_then_record_id(self) -> None:
        items = [
            RecordWithStatus(record("fts-low"), APPROVED, relevance=0.2),
            RecordWithStatus(record("fts-high"), APPROVED, relevance=2.0),
            record("tie-b", text="build cache", confidence=0.5),
            record("tie-a", text="build cache", confidence=0.5),
            record("older", text="build cache", created_days_ago=200),
            record("unrelated", text="weather in izmir", subject="weather"),
        ]
        selection = select(items, text="build cache")
        self.assertEqual(
            ids(selection.chosen), ["fts-high", "tie-a", "tie-b", "older", "fts-low"]
        )
        self.assertGreater(selection.chosen[2].score, selection.chosen[3].score)

    def test_limit_keeps_the_top_records(self) -> None:
        items = [record(f"r{i}", confidence=i / 10) for i in range(1, 6)]
        self.assertEqual(ids(select(items, limit=2).chosen), ["r5", "r4"])
        self.assertEqual(select(items, limit=0).chosen, ())

    def test_bad_input_fails_closed(self) -> None:
        good = [RecordWithStatus(record("a"), APPROVED)]
        query = TaskQuery("build")
        scope = MemoryRecordScope(HOME)
        live = dict(live_project_ids=frozenset(), live_conversation_ids=frozenset())
        with self.assertRaises(ValueError):
            select_for_task(good, query, scope, now=NOW.replace(tzinfo=None), limit=1, **live)
        with self.assertRaises(ValueError):
            select_for_task(good, query, scope, now=NOW, limit=True, **live)
        with self.assertRaises(ValueError):
            select_for_task(good * 2, query, scope, now=NOW, limit=1, **live)
        with self.assertRaises(ValueError):
            select_for_task([record("a")], query, scope, now=NOW, limit=1, **live)
        with self.assertRaises(ValueError):
            RecordWithStatus(record("a"), APPROVED, relevance=float("nan"))
        with self.assertRaises(ValueError):
            TaskQuery("x", global_readonly=1)
        with self.assertRaises(ValueError):
            RecordWithStatus(record("a"), APPROVED, relevance=10**400)
        for bad_live in ("proj-home", None, frozenset({1})):
            with self.assertRaises(ValueError):
                select_for_task(
                    good, query, scope, now=NOW, limit=1,
                    live_project_ids=bad_live, live_conversation_ids=frozenset(),
                )


if __name__ == "__main__":
    unittest.main()
