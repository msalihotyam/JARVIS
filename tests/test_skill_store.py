"""V2.2 S2 SKSTORE: the skill store over the schema-10 extra tables.

Every test opens its own TEMP database; none opens data\\jarvis.db.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.memory import SCHEMA_VERSION, SKILL_SCHEMA_OBJECTS
from jarvis.skill_lifecycle import SkillEvent, next_version
from jarvis.skill_store import BODY_FORMAT, SkillStore, SkillStoreError
from jarvis.skill_types import (
    Experience,
    ExperienceKind,
    SkillDraft,
    SkillEventKind,
    SkillInput,
    SkillOrigin,
    SkillProvenance,
    SkillRecordError,
    SkillScope,
    SkillScopeKind,
    SkillStatus,
    SkillStep,
)
from tests.helpers import safe_cleanup

AT = "2026-10-14T00:00:00+00:00"
GLOBAL = SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None)
SCOPE_A = SkillScope(kind=SkillScopeKind.PROJECT, project_id="proj-a")
SCOPE_B = SkillScope(kind=SkillScopeKind.PROJECT, project_id="proj-b")
TK = "tk-" + "a" * 32


def draft(number: int = 1, scope: SkillScope = GLOBAL) -> SkillDraft:
    return SkillDraft(
        draft_id=f"draft-{number:032x}",
        name="Haftalik rapor dosyasini oku",
        summary="Haftalik rapor dosyasini oku",
        scope=scope,
        inputs=(SkillInput(name="path", description="", required=True),),
        steps=(
            SkillStep(
                index=1,
                action="Haftalik rapor dosyasini oku: {path}",
                capability="file_read",
                inputs=("path",),
                check=None,
            ),
        ),
        checks=(),
        provenance=SkillProvenance(
            origin=SkillOrigin.TAUGHT_FROM_RUN,
            source_run_id=f"run-{number}",
            parent_skill_id=None,
            parent_version=None,
            created_at=AT,
        ),
        reason="Bu adimlar tekrar edilebilir.",
        target_skill_id=None,
    )


def lesson(number: int = 1, scope: SkillScope = GLOBAL) -> Experience:
    return Experience(
        experience_id=f"exp-{number}",
        kind=ExperienceKind.AVOID,
        skill_id=None,
        task_signature=("haftalik", "rapor"),
        text="Once dosyanin yerini kontrol et.",
        scope=scope,
        source_run_id=f"run-{number}",
        created_at=AT,
    )


def event(skill_id: str, kind: SkillEventKind, version: int | None = None) -> SkillEvent:
    return SkillEvent(skill_id=skill_id, kind=kind, version=version, at=AT)


class SkillStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.path = Path(temp.name) / "skills.db"
        self.store = SkillStore(self.path)

    def raw(self, sql: str, params: tuple = ()) -> list:
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def approved(self, number: int = 1, scope: SkillScope = GLOBAL):
        self.store.add_draft(draft(number, scope))
        return self.store.approve_draft(f"draft-{number:032x}", project_id=scope.project_id)

    def test_schema_stays_10_and_the_compat_create_is_idempotent(self) -> None:
        self.assertEqual(10, SCHEMA_VERSION)
        self.approved()
        again = SkillStore(self.path)  # second open re-runs CREATE ... IF NOT EXISTS
        names = {row[0] for row in self.raw("SELECT name FROM sqlite_master")}
        self.assertLessEqual(SKILL_SCHEMA_OBJECTS, names)
        self.assertEqual([(10,)], self.raw("SELECT version FROM schema_version"))
        self.assertEqual(1, len(again.list_skills(None)))

    def test_round_trip_of_all_five_tables(self) -> None:
        offered = draft(1)
        self.assertTrue(self.store.add_draft(offered))
        self.assertFalse(self.store.add_draft(offered))  # idempotent on draft_id
        self.assertEqual((offered,), self.store.pending_drafts(None))
        skill = self.store.approve_draft(offered.draft_id, project_id=None)
        self.assertEqual((), self.store.pending_drafts(None))
        self.assertEqual(skill, self.store.get_skill(skill.skill_id, None))
        self.store.append_event(event(skill.skill_id, SkillEventKind.USED_SUCCESS, 1))
        (listed,) = self.store.list_skills(None)
        self.assertEqual((skill, SkillStatus.ACTIVE, 1), (listed.skill, listed.status, listed.health.use_count))
        kinds = [item.kind for item in self.store.history(skill.skill_id, None).events]
        self.assertEqual([SkillEventKind.DRAFT_APPROVED, SkillEventKind.USED_SUCCESS], kinds)
        self.assertFalse(self.store.is_suppressed(TK))
        self.store.set_suppression(TK, True)
        self.assertTrue(self.store.is_suppressed(TK))
        self.store.set_suppression(TK, False)
        self.assertFalse(self.store.is_suppressed(TK))
        self.assertEqual(2, len(self.raw("SELECT * FROM skill_suppressions")))  # append-only
        self.assertTrue(self.store.add_experience(lesson(1)))
        self.assertFalse(self.store.add_experience(lesson(1)))
        self.assertEqual((lesson(1),), self.store.experiences(None))

    def test_a_new_version_moves_the_pointer_and_rollback_is_an_event(self) -> None:
        first = self.approved()
        history = self.store.history(first.skill_id, None)
        second = next_version(
            first, {"summary": "Haftalik raporu oku ve ozetle"}, "user_edit", AT, history=history
        )
        self.store.add_version(second)
        self.assertEqual(second, self.store.get_skill(first.skill_id, None))
        self.assertEqual(first, self.store.get_skill(first.skill_id, None, version=1))
        with self.assertRaises(SkillRecordError):
            self.store.add_version(second)  # versions are immutable
        self.store.append_event(event(first.skill_id, SkillEventKind.ROLLED_BACK, 1))
        self.assertEqual(first, self.store.get_skill(first.skill_id, None))
        with self.assertRaises(SkillStoreError):
            self.store.add_version(second, actor="core")

    def test_approve_draft_refuses_a_non_pending_draft(self) -> None:
        with self.assertRaises(SkillStoreError):
            self.store.approve_draft(draft(9).draft_id, project_id=None)  # never offered
        skill = self.approved(1)
        with self.assertRaises(SkillStoreError):
            self.store.approve_draft(draft(1).draft_id, project_id=None)
        self.store.add_draft(draft(2))
        self.store.reject_draft(draft(2).draft_id, project_id=None)
        self.assertEqual((), self.store.pending_drafts(None))
        with self.assertRaises(SkillStoreError):
            self.store.approve_draft(draft(2).draft_id, project_id=None)
        with self.assertRaises(SkillStoreError):
            self.store.reject_draft(draft(2).draft_id, project_id=None)
        self.assertEqual([skill], [item.skill for item in self.store.list_skills(None)])

    def test_approve_draft_refuses_a_filtered_edit_and_a_non_user_actor(self) -> None:
        offered = draft(1)
        self.store.add_draft(offered)
        edited = offered.to_dict()
        edited["checks"] = ["password: hunter2"]
        with self.assertRaises(SkillRecordError) as caught:
            self.store.approve_draft(offered.draft_id, edits=edited, project_id=None)
        self.assertNotIn("hunter2", str(caught.exception))
        with self.assertRaises(SkillStoreError):
            self.store.approve_draft(offered.draft_id, actor="core", project_id=None)
        self.assertEqual((offered,), self.store.pending_drafts(None))
        self.assertEqual([], self.raw("SELECT * FROM skills"))
        with self.assertRaises(SkillStoreError):
            self.store.add_draft(dataclasses.replace(offered, draft_id="draft-" + "f" * 32, reason="password: hunter2"))
        with self.assertRaises(SkillStoreError):
            self.store.add_experience(dataclasses.replace(lesson(2), text="my password is hunter2"))

    def test_a_tombstoned_skill_is_invisible_and_final(self) -> None:
        skill = self.approved()
        self.store.append_event(event(skill.skill_id, SkillEventKind.DELETED))
        self.assertEqual((), self.store.list_skills(None))
        self.assertIsNone(self.store.get_skill(skill.skill_id, None))
        self.assertIsNone(self.store.get_skill(skill.skill_id, None, version=1))
        self.assertIsNone(self.store.history(skill.skill_id, None))
        with self.assertRaises(SkillStoreError):
            self.store.append_event(event(skill.skill_id, SkillEventKind.ENABLED))
        self.assertEqual(1, len(self.raw("SELECT * FROM skills")))  # tombstone, not DELETE

    def test_corrupt_rows_are_skipped_and_counted(self) -> None:
        skill = self.approved(1)
        other = self.approved(2)
        self.store.add_draft(draft(3))
        self.store.add_experience(lesson(1))
        self.raw("UPDATE skills SET body = '{' WHERE skill_id = ?", (skill.skill_id,))
        self.raw("UPDATE skill_drafts SET body_format = ?", (BODY_FORMAT + 1,))
        self.raw("UPDATE skill_experiences SET scope_kind = 'project', project_id = 'proj-a'")
        self.assertEqual([other], [item.skill for item in self.store.list_skills(None)])
        self.assertIsNone(self.store.get_skill(skill.skill_id, None))
        self.assertEqual((), self.store.pending_drafts(None))
        self.assertEqual((), self.store.experiences("proj-a"))
        self.assertEqual(4, self.store.broken_rows)
        with self.assertRaises(SkillStoreError):
            self.store.append_event(event(skill.skill_id, SkillEventKind.ENABLED))

    def test_a_broken_tombstone_never_resurrects_the_skill(self) -> None:
        skill = self.approved()
        self.store.append_event(event(skill.skill_id, SkillEventKind.DELETED))
        self.raw("UPDATE skill_events SET body = 'x' WHERE kind = 'deleted'")
        self.assertEqual((), self.store.list_skills(None))
        self.assertIsNone(self.store.get_skill(skill.skill_id, None))

    def test_project_scope_does_not_leak(self) -> None:
        local = self.approved(1, SCOPE_A)
        shared = self.approved(2)
        self.store.add_draft(draft(3, SCOPE_A))
        self.store.add_experience(lesson(1, SCOPE_A))
        listed = {"proj-a": [local, shared], "proj-b": [shared], None: [shared]}
        for project_id, expected in listed.items():
            with self.subTest(project_id=project_id):
                found = [item.skill for item in self.store.list_skills(project_id)]
                self.assertCountEqual(expected, found)
                self.assertEqual(project_id == "proj-a", self.store.get_skill(local.skill_id, project_id) is not None)
                self.assertEqual(project_id == "proj-a", self.store.history(local.skill_id, project_id) is not None)
                self.assertEqual(project_id == "proj-a", len(self.store.pending_drafts(project_id)) == 1)
                self.assertEqual(project_id == "proj-a", len(self.store.experiences(project_id)) == 1)
        with self.assertRaises(SkillRecordError):
            self.store.approve_draft(draft(3).draft_id, scope=SCOPE_B, project_id="proj-a")  # D15
        for other in ("proj-b", None):  # another project's draft is not decided from here
            with self.assertRaises(SkillStoreError):
                self.store.approve_draft(draft(3).draft_id, project_id=other)
            with self.assertRaises(SkillStoreError):
                self.store.reject_draft(draft(3).draft_id, project_id=other)
        promoted = self.store.approve_draft(draft(3).draft_id, scope=GLOBAL, project_id="proj-a")  # D8
        self.assertIsNotNone(self.store.get_skill(promoted.skill_id, "proj-b"))

    def test_no_event_kind_records_a_draft_decision_outside_approve_and_reject(self) -> None:
        skill = self.approved()
        for kind in (SkillEventKind.DRAFT_APPROVED, SkillEventKind.DRAFT_REJECTED):
            with self.assertRaises(SkillStoreError):
                self.store.append_event(event(skill.skill_id, kind, 1))
        with self.assertRaises(SkillStoreError):
            self.store.append_event(event("sk-unknown", SkillEventKind.ENABLED))
        for key in ("tk-short", "global ", "'; DROP TABLE skills; --"):
            with self.assertRaises(SkillStoreError):
                self.store.set_suppression(key, True)
        self.store.set_suppression("global", True)
        self.assertTrue(self.store.is_suppressed("global"))


if __name__ == "__main__":
    unittest.main()
