"""V2.2 b5 SKFMTVER: every skill body names its record format, Unicode and Python.

The reader dispatches on body_format: format 2 (the envelope) and format 1
(bare to_dict, written before it) parse strictly; a newer or corrupted row is
never used, matched or offered, only listed as unreadable. Raw rows are
written with INSERT only, on each test's own TEMP database (never
data\\jarvis.db).
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

from jarvis.experience import select_for_task
from jarvis.memory import SCHEMA_VERSION
from jarvis.skill_match import match_skills
from jarvis.skill_store import BODY_FORMAT, NEEDS_NEWER, SkillStore, UnreadableRow
from jarvis.skill_types import SkillEventKind
from tests.helpers import safe_cleanup
from tests.test_skill_store import AT, draft, event, lesson

TASK = "Haftalik rapor dosyasini oku"
PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"


def bare(record) -> str:
    return json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)


def envelope(
    record_data, *, body_format=BODY_FORMAT, unicode=unicodedata.unidata_version, python=PYTHON
) -> str:
    return json.dumps(
        {"body_format": body_format, "unicode": unicode, "python": python, "record": record_data},
        ensure_ascii=False,
        sort_keys=True,
    )


class SkillFormatVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.path = Path(temp.name) / "skills.db"
        self.store = SkillStore(self.path)

    def raw(self, sql: str, params: tuple = ()) -> list:
        self.assertTrue(sql.startswith(("SELECT", "INSERT")))  # append-only, even in tests
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def approved(self, number: int):
        self.store.add_draft(draft(number))
        return self.store.approve_draft(f"draft-{number:032x}", project_id=None)

    def insert_skill(self, skill, body: str, body_format: int) -> None:
        self.raw(
            "INSERT INTO skills (skill_id, version, body, body_format, scope_kind, project_id, "
            "created_at) VALUES (?, ?, ?, ?, 'global', NULL, ?)",
            (skill.skill_id, skill.version, body, body_format, AT),
        )

    def insert_event(self, item, body: str, body_format: int) -> None:
        self.raw(
            "INSERT INTO skill_events (skill_id, kind, body, body_format, draft_id, actor, "
            "created_at) VALUES (?, ?, ?, ?, NULL, 'user', ?)",
            (item.skill_id, item.kind.value, body, body_format, AT),
        )

    def insert_draft(self, item, body: str, body_format: int) -> None:
        self.raw(
            "INSERT INTO skill_drafts (draft_id, body, body_format, scope_kind, project_id, "
            "created_at) VALUES (?, ?, ?, 'global', NULL, ?)",
            (item.draft_id, body, body_format, AT),
        )

    def insert_experience(self, item, body: str, body_format: int) -> None:
        self.raw(
            "INSERT INTO skill_experiences (experience_id, body, body_format, scope_kind, "
            "project_id, skill_id, created_at) VALUES (?, ?, ?, 'global', NULL, NULL, ?)",
            (item.experience_id, body, body_format, AT),
        )

    def unusable_rows(self, make_body, body_format: int, reason: str) -> None:
        """A whole skill, a draft and a lesson in rows no read may use."""
        kept = self.approved(1)
        self.store.add_experience(lesson(1))
        ghost = dataclasses.replace(kept, skill_id="sk-ghost")
        approved = event("sk-ghost", SkillEventKind.DRAFT_APPROVED, 1)
        self.insert_skill(ghost, make_body(ghost), body_format)
        self.insert_event(approved, make_body(approved), body_format)
        offered = draft(2)
        self.insert_draft(offered, make_body(offered), body_format)
        ghost_lesson = dataclasses.replace(lesson(2), text="Ghost lesson text.")
        self.insert_experience(ghost_lesson, make_body(ghost_lesson), body_format)

        listed = self.store.list_skills(None)
        self.assertEqual([kept], [item.skill for item in listed])
        self.assertIsNone(self.store.get_skill("sk-ghost", None))
        self.assertIsNone(self.store.history("sk-ghost", None))
        self.assertNotIn(
            "sk-ghost", [c.skill_id for c in match_skills(TASK, listed, None).candidates]
        )
        self.assertEqual((), self.store.pending_drafts(None))
        self.assertEqual((lesson(1),), self.store.experiences(None))
        self.assertEqual(
            (lesson(1),), select_for_task(TASK, self.store.experiences(None), None, None)
        )
        self.assertEqual(
            {
                UnreadableRow("skills", "sk-ghost:1", reason),
                UnreadableRow("skill_events", "2", reason),
                UnreadableRow("skill_drafts", offered.draft_id, reason),
                UnreadableRow("skill_experiences", "exp-2", reason),
            },
            set(self.store.unreadable_rows()),
        )
        self.assertGreaterEqual(self.store.broken_rows, 4)

    def test_round_trip_writes_the_envelope_on_every_table(self) -> None:
        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(2, BODY_FORMAT)
        skill = self.approved(1)
        self.store.add_draft(draft(2))
        self.store.add_experience(lesson(1))
        first = "SELECT body, body_format FROM {} ORDER BY rowid LIMIT 1"
        records = {
            "skills": (first.format("skills"), skill.to_dict()),
            "skill_events": (
                first.format("skill_events"),
                event(skill.skill_id, SkillEventKind.DRAFT_APPROVED, 1).to_dict(),
            ),
            "skill_drafts": (first.format("skill_drafts"), draft(1).to_dict()),
            "skill_experiences": (first.format("skill_experiences"), lesson(1).to_dict()),
        }
        for table, (query, record) in records.items():
            with self.subTest(table=table):
                (body, body_format), = self.raw(query)
                self.assertEqual(BODY_FORMAT, body_format)
                data = json.loads(body)
                self.assertEqual(
                    {"body_format": 2, "unicode": unicodedata.unidata_version,
                     "python": PYTHON, "record": data["record"]},
                    data,
                )
                if table != "skill_events":  # its stamp is the store's utc_now
                    self.assertEqual(record, data["record"])
        self.assertEqual(skill, self.store.get_skill(skill.skill_id, None))
        self.assertEqual((draft(2),), self.store.pending_drafts(None))
        self.assertEqual((lesson(1),), self.store.experiences(None))
        self.assertEqual((), self.store.unreadable_rows())
        self.assertEqual(0, self.store.broken_rows)

    def test_a_future_body_format_is_never_used_and_listed_as_needs_newer(self) -> None:
        def future(record) -> str:
            return envelope({**record.to_dict(), "added_later": True}, body_format=BODY_FORMAT + 1)

        self.unusable_rows(future, BODY_FORMAT + 1, NEEDS_NEWER)
        self.assertEqual(NEEDS_NEWER, "needs a newer JARVIS")

    def test_a_corrupted_body_is_never_used_and_listed_as_unreadable(self) -> None:
        self.unusable_rows(lambda record: "{", BODY_FORMAT, "unreadable")

    def test_an_unknown_field_or_bad_envelope_is_unreadable(self) -> None:
        for name, make_body in (
            ("extra record key", lambda r: envelope({**r.to_dict(), "added_later": 1})),
            ("bare body under format 2", bare),
            ("format mismatch", lambda r: envelope(r.to_dict(), body_format=1)),
            ("no unicode", lambda r: envelope(r.to_dict(), unicode=None)),
            ("python not a string", lambda r: envelope(r.to_dict(), python=3.13)),
            ("missing envelope key", lambda r: json.dumps({"body_format": 2, "record": r.to_dict()})),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
                self.path = Path(d) / "skills.db"
                self.store = SkillStore(self.path)
                self.unusable_rows(make_body, BODY_FORMAT, "unreadable")

    def test_an_envelope_under_format_1_is_unreadable(self) -> None:
        self.unusable_rows(lambda r: envelope(r.to_dict()), 1, "unreadable")

    def test_a_newer_tombstone_leaves_its_skill_out_whole(self) -> None:
        skill = self.approved(1)
        other = self.approved(2)
        deleted = event(skill.skill_id, SkillEventKind.DELETED)
        self.insert_event(deleted, envelope(deleted.to_dict(), body_format=3), 3)
        self.assertEqual([other], [item.skill for item in self.store.list_skills(None)])
        self.assertIsNone(self.store.get_skill(skill.skill_id, None))
        self.assertEqual(
            (UnreadableRow("skill_events", "3", NEEDS_NEWER),), self.store.unreadable_rows()
        )

    def test_a_legacy_lesson_with_non_canonical_tokens_stays_strict(self) -> None:
        data = lesson(2).to_dict() | {"task_signature": ["HAFTALIK", "rapor"]}
        self.insert_experience(lesson(2), json.dumps(data), 1)
        self.assertEqual((), self.store.experiences(None))
        self.assertEqual(
            (UnreadableRow("skill_experiences", "exp-2", "unreadable"),),
            self.store.unreadable_rows(),
        )

    def test_rows_written_before_the_envelope_still_read(self) -> None:
        kept = self.approved(1)
        legacy = dataclasses.replace(kept, skill_id="sk-legacy")
        approved = event("sk-legacy", SkillEventKind.DRAFT_APPROVED, 1)
        self.insert_skill(legacy, bare(legacy), 1)
        self.insert_event(approved, bare(approved), 1)
        self.insert_draft(draft(2), bare(draft(2)), 1)
        self.insert_experience(lesson(2), bare(lesson(2)), 1)
        self.assertEqual(
            {kept, legacy}, {item.skill for item in self.store.list_skills(None)}
        )
        self.assertEqual(legacy, self.store.get_skill("sk-legacy", None))
        self.assertEqual((draft(2),), self.store.pending_drafts(None))
        self.assertEqual((lesson(2),), self.store.experiences(None))
        self.assertEqual((), self.store.unreadable_rows())
        # A new event on a legacy skill is written in the current format.
        self.store.append_event(event("sk-legacy", SkillEventKind.DISABLED))
        self.assertEqual(
            [(2,), (1,), (2,)],
            self.raw("SELECT body_format FROM skill_events ORDER BY event_id"),
        )

    def test_another_unicode_database_is_refolded_in_memory_only(self) -> None:
        drifted = dataclasses.replace(lesson(1), task_signature=("haftalik", "rapor"))
        data = drifted.to_dict() | {"task_signature": ["HAFTALIK", "Rapor", "rapor"]}
        body = envelope(data, unicode="0.0.0")
        self.insert_experience(drifted, body, BODY_FORMAT)
        self.assertEqual((drifted,), self.store.experiences(None))
        self.assertEqual((drifted,), select_for_task(TASK, self.store.experiences(None), None, None))
        self.assertEqual([(body,)], self.raw("SELECT body FROM skill_experiences"))
        # Same tokens claimed under this very Unicode version: strict, unreadable.
        self.insert_experience(lesson(2), envelope(lesson(2).to_dict() | {
            "task_signature": ["HAFTALIK"]}), BODY_FORMAT)
        self.assertEqual((drifted,), self.store.experiences(None))
        self.assertEqual(
            (UnreadableRow("skill_experiences", "exp-2", "unreadable"),),
            self.store.unreadable_rows(),
        )


if __name__ == "__main__":
    unittest.main()
