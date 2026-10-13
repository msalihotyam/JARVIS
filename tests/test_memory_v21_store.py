"""V2.1a M1 typed record store: insert-only rows, status events, undo.

Every database is a throwaway in TEMP; nothing opens the operator's database.
"""

from __future__ import annotations

import re
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
    MemoryRecordScopeKind,
    MemoryRecordStatus,
)
from jarvis.memory import (
    MemoryRecordApproval,
    MemoryRecordRefused,
    SQLiteMemoryStore,
)
from tests.helpers import safe_cleanup

S = MemoryRecordStatus
K = MemoryRecordKind
MODEL = MemoryRecordActor.MODEL
REPO = Path(__file__).resolve().parents[1]


class RecordStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "memory.db"
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id

    def record(self, kind=K.FACT, text="the build uses uv", *, project=None,
               origin=MemoryRecordOrigin.CAPTURE_SESSION_END, actor=MODEL,
               subject="tooling", conversation=None, **extra) -> MemoryRecord:
        return MemoryRecord(
            kind=kind, text=text, subject_key=subject,
            scope=MemoryRecordScope(project or self.alpha, conversation),
            origin=origin, actor=actor, confidence=0.8,
            classification=DataClassification.INTERNAL, **extra,
        )

    def approve(self, record_id: str, project: str | None = None) -> None:
        self.store.approve_record(
            record_id, MemoryRecordApproval(project or self.alpha, record_id)
        )

    def counts(self) -> tuple[int, int]:
        connection = sqlite3.connect(self.database)
        try:
            return (
                connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM memory_record_events").fetchone()[0],
            )
        finally:
            connection.close()

    def test_initial_status_per_kind(self) -> None:
        expected = {K.FACT: S.APPROVED, K.LESSON: S.APPROVED, K.DECISION: S.PENDING,
                    K.RULE: S.PENDING, K.PREFERENCE: S.PENDING}
        for kind, status in expected.items():
            with self.subTest(kind=kind):
                view = self.store.insert_record(self.record(kind, subject=kind.value))
                self.assertIs(view.status, status)
                self.assertIs(self.store.current_status(view.record.record_id), status)

    def test_import_fact_stays_pending(self) -> None:
        view = self.store.insert_record(self.record(origin=MemoryRecordOrigin.IMPORT))
        self.assertIs(view.status, S.PENDING)

    def test_status_machine_table(self) -> None:
        # (initial kind, steps, expected final or the step index that is refused)
        def run_step(record_id: str, step: str) -> None:
            if step == "approve":
                self.approve(record_id)
            elif step == "undo":
                self.store.undo(record_id, MODEL)
            else:
                self.store.record_event(record_id, S(step), MODEL, "test")

        table = [
            (K.FACT, ["undo"], S.RETRACTED),
            (K.FACT, ["expired"], S.EXPIRED),
            (K.FACT, ["undo", "undo"], 1),
            (K.FACT, ["approve"], 0),
            (K.FACT, ["rejected"], 0),
            (K.RULE, ["approve"], S.APPROVED),
            (K.RULE, ["approve", "undo"], S.RETRACTED),
            (K.RULE, ["rejected"], S.REJECTED),
            (K.RULE, ["rejected", "approve"], 1),
            (K.RULE, ["undo"], 0),
            (K.RULE, ["approve", "approve"], 1),
            (K.PREFERENCE, ["expired", "undo"], 1),
            (K.DECISION, ["pending"], 0),
            (K.DECISION, ["approved"], 0),
            (K.DECISION, ["superseded"], 0),
        ]
        for index, (kind, steps, outcome) in enumerate(table):
            with self.subTest(row=index, kind=kind, steps=steps):
                view = self.store.insert_record(self.record(kind, subject=f"s{index}"))
                record_id = view.record.record_id
                before = self.store.current_status(record_id)
                for position, step in enumerate(steps):
                    if outcome == position:
                        _, events = self.counts()
                        with self.assertRaises(ValueError):
                            run_step(record_id, step)
                        self.assertEqual(self.counts()[1], events)
                        break
                    run_step(record_id, step)
                    before = self.store.current_status(record_id)
                else:
                    self.assertIs(self.store.current_status(record_id), outcome)
                    continue
                self.assertIs(self.store.current_status(record_id), before)

    def test_approve_without_valid_proof_is_refused(self) -> None:
        rule = self.store.insert_record(self.record(K.RULE)).record.record_id
        other = self.store.insert_record(self.record(K.RULE, subject="x")).record.record_id
        bad_proofs = [
            None,
            "user-approve",
            {"project_id": self.alpha, "record_id": rule},
            MemoryRecordApproval(self.alpha, other),
            MemoryRecordApproval(self.beta, rule),
        ]
        for proof in bad_proofs:
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                self.store.approve_record(rule, proof)  # type: ignore[arg-type]
        self.assertIs(self.store.current_status(rule), S.PENDING)

    def test_approve_refused_for_archived_project(self) -> None:
        rule = self.store.insert_record(self.record(K.RULE)).record.record_id
        self.store.set_project_active(self.alpha, False)
        with self.assertRaises(ValueError):
            self.approve(rule)
        self.assertIs(self.store.current_status(rule), S.PENDING)

    def test_insert_record_refuses_explicit_user_and_user_actor(self) -> None:
        refused = [
            self.record(origin=MemoryRecordOrigin.EXPLICIT_USER, actor=MemoryRecordActor.USER),
            self.record(actor=MemoryRecordActor.USER),
            self.record(origin=MemoryRecordOrigin.LEGACY_M1),
        ]
        for record in refused:
            with self.subTest(origin=record.origin, actor=record.actor):
                with self.assertRaises(ValueError):
                    self.store.insert_record(record)
        self.assertEqual(self.counts(), (0, 0))

    def test_explicit_user_boundary(self) -> None:
        fact = self.store.insert_explicit_user_record(
            self.alpha, "fact", "internal", "the build uses uv", "tooling")
        self.assertIs(fact.status, S.APPROVED)
        self.assertIs(fact.record.origin, MemoryRecordOrigin.EXPLICIT_USER)
        self.assertIs(fact.record.actor, MemoryRecordActor.USER)
        rule = self.store.insert_explicit_user_record(
            self.alpha, "rule", "internal", "always run the tests", "testing")
        self.assertIs(rule.status, S.PENDING)
        self.approve(rule.record.record_id)
        self.assertIs(self.store.current_status(rule.record.record_id), S.APPROVED)
        with self.assertRaises(ValueError):
            self.store.insert_explicit_user_record(
                self.alpha, "fact", "secret", "x", "y")

    def test_filter_refusal_writes_nothing(self) -> None:
        for insert in (
            lambda: self.store.insert_record(self.record(text="call me on 0532 123 45 67")),
            lambda: self.store.insert_explicit_user_record(
                self.alpha, "fact", "internal", "call me on 0532 123 45 67", "contact"),
        ):
            with self.assertRaises(MemoryRecordRefused) as caught:
                insert()
            self.assertEqual(caught.exception.category, "phone")
        self.assertEqual(self.counts(), (0, 0))

    def test_insert_refused_for_unknown_or_archived_project(self) -> None:
        self.store.set_project_active(self.beta, False)
        for project in (self.beta, "p-" + "0" * 32):
            with self.subTest(project=project), self.assertRaises(ValueError):
                self.store.insert_record(self.record(project=project))
        self.assertEqual(self.counts(), (0, 0))

    def test_duplicate_record_id_refused(self) -> None:
        view = self.store.insert_record(self.record())
        with self.assertRaises(ValueError):
            self.store.insert_record(self.record(record_id=view.record.record_id))
        self.assertEqual(self.counts(), (1, 1))

    def test_cross_project_isolation(self) -> None:
        mine = self.store.insert_record(self.record()).record.record_id
        theirs = self.store.insert_record(self.record(project=self.beta)).record.record_id
        self.store.save_explicit_user_memory_item(self.beta, "fact", "internal", "beta legacy")
        alpha_ids = [v.record.record_id for v in self.store.list_records(MemoryRecordScope(self.alpha))]
        beta_views = self.store.list_records(MemoryRecordScope(self.beta))
        self.assertEqual(alpha_ids, [mine])
        self.assertIn(theirs, [v.record.record_id for v in beta_views])
        self.assertTrue(all(v.record.scope.project_id == self.beta for v in beta_views))
        with self.assertRaises(ValueError):
            self.store.list_records(MemoryRecordScope(None, kind=MemoryRecordScopeKind.GLOBAL_READONLY))

    def test_conversation_scope_never_sees_another_conversation(self) -> None:
        wide = self.store.insert_record(self.record()).record.record_id
        own = self.store.insert_record(self.record(conversation="c-one", subject="a")).record.record_id
        self.store.insert_record(self.record(conversation="c-two", subject="b"))
        ids = {v.record.record_id for v in self.store.list_records(MemoryRecordScope(self.alpha, "c-one"))}
        self.assertEqual(ids, {wide, own})

    def test_list_filters_and_limit(self) -> None:
        fact = self.store.insert_record(self.record()).record.record_id
        self.store.insert_record(self.record(K.RULE, subject="r"))
        scope = MemoryRecordScope(self.alpha)
        approved = self.store.list_records(scope, statuses=[S.APPROVED])
        self.assertEqual([v.record.record_id for v in approved], [fact])
        self.assertEqual([v.record.kind for v in self.store.list_records(scope, kinds=[K.RULE])], [K.RULE])
        self.assertEqual(len(self.store.list_records(scope, limit=1)), 1)
        for bad in ({"limit": 0}, {"limit": 501}, {"limit": True}, {"statuses": ["approved"]},
                    {"kinds": ["fact"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.list_records(scope, **bad)

    def test_legacy_m1_items_read_only(self) -> None:
        kept = self.store.save_explicit_user_memory_item(self.alpha, "decision", "internal", "ship weekly")
        gone = self.store.save_explicit_user_memory_item(self.alpha, "rule", "internal", "no friday deploys")
        self.store.retire_memory_item(self.alpha, gone.id)
        self.store.save_explicit_user_memory_item(self.alpha, "thread", "internal", "open thread")
        views = {v.record.record_id: v for v in self.store.list_records(MemoryRecordScope(self.alpha))}
        self.assertEqual(set(views), {kept.id, gone.id})
        self.assertIs(views[kept.id].status, S.APPROVED)
        self.assertIs(views[kept.id].record.kind, K.DECISION)
        self.assertIs(views[kept.id].record.origin, MemoryRecordOrigin.LEGACY_M1)
        self.assertIs(views[gone.id].status, S.RETRACTED)
        self.assertEqual(self.counts(), (0, 0))  # nothing migrated

    def test_supersede(self) -> None:
        old = self.store.insert_record(self.record()).record.record_id
        new = self.store.insert_record(self.record(text="the build uses uv 0.11", supersedes=old)).record.record_id
        self.assertIs(self.store.supersede(old, new, MODEL, "newer"), S.SUPERSEDED)
        self.assertIs(self.store.current_status(old), S.SUPERSEDED)
        self.assertIs(self.store.current_status(new), S.APPROVED)
        with self.assertRaises(ValueError):
            self.store.supersede(old, new, MODEL)

    def test_supersede_refusals(self) -> None:
        old = self.store.insert_record(self.record()).record.record_id
        unrelated = self.store.insert_record(self.record(text="the build uses pip")).record.record_id
        other_subject = self.store.insert_record(
            self.record(text="b", subject="other", supersedes=old)).record.record_id
        for new in (unrelated, other_subject, old):
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.store.supersede(old, new, MODEL)
        rule = self.store.insert_record(self.record(K.RULE, subject="r")).record.record_id
        self.approve(rule)
        newer = self.store.insert_record(
            self.record(K.RULE, text="newer rule", subject="r", supersedes=rule)).record.record_id
        self.approve(newer)
        with self.assertRaises(ValueError):
            self.store.supersede(rule, newer, MODEL)
        with self.assertRaises(ValueError):
            self.store.supersede(rule, newer, MODEL, proof=MemoryRecordApproval(self.alpha, newer))
        self.assertIs(self.store.current_status(rule), S.APPROVED)
        self.store.supersede(rule, newer, MemoryRecordActor.USER,
                             proof=MemoryRecordApproval(self.alpha, rule))
        self.assertIs(self.store.current_status(rule), S.SUPERSEDED)

    def test_supersede_refuses_cross_conversation_and_unproven_explicit_user(self) -> None:
        wide = self.store.insert_record(self.record()).record.record_id
        narrow = self.store.insert_record(
            self.record(text="uv 0.11", conversation="c-one", supersedes=wide)).record.record_id
        with self.assertRaises(ValueError):
            self.store.supersede(wide, narrow, MODEL)
        self.assertIs(self.store.current_status(wide), S.APPROVED)
        user = self.store.insert_explicit_user_record(
            self.alpha, "fact", "internal", "deploys go through ci", "deploys").record.record_id
        model = self.store.insert_record(
            self.record(text="deploys go by hand", subject="deploys", supersedes=user)).record.record_id
        with self.assertRaises(ValueError):
            self.store.supersede(user, model, MODEL)
        self.assertIs(self.store.current_status(user), S.APPROVED)
        self.store.supersede(user, model, MemoryRecordActor.USER,
                             proof=MemoryRecordApproval(self.alpha, user))
        self.assertIs(self.store.current_status(user), S.SUPERSEDED)

    def test_record_id_cannot_shadow_legacy_m1_item(self) -> None:
        legacy = self.store.save_explicit_user_memory_item(self.alpha, "fact", "internal", "ship weekly")
        with self.assertRaises(ValueError):
            self.store.insert_record(self.record(record_id=legacy.id))
        self.assertEqual(self.counts(), (0, 0))

    def test_no_update_or_delete_on_record_tables(self) -> None:
        source = (REPO / "jarvis" / "memory.py").read_text(encoding="utf-8")
        pattern = re.compile(r"(UPDATE|DELETE\s+FROM|REPLACE\s+INTO|INSERT\s+OR\s+REPLACE\s+INTO)\s+memory_record",
                             re.IGNORECASE)
        self.assertEqual(pattern.findall(source), [])


if __name__ == "__main__":
    unittest.main()
