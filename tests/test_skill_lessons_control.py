"""V2.2 b1 SKLESSONS: the lesson control surface and the lesson-use record.

Through the real Core and the app API with fakes only (``DummyConnector``,
an isolated temp database, never data/jarvis.db, no model or network call):

- a disabled or deleted lesson is never selected for a task again;
- a deleted lesson is not listed and its text never comes back from the API;
- every injection records the lesson ids (ids only) and the finished run
  carries ``lessons_used``;
- another project never sees or controls a project's lessons;
- the two extra tables are append-only, created on open, schema stays 10.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from jarvis.memory import COMPATIBILITY_SCHEMA_OBJECTS, SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.skill_store import LESSON_SCHEMA_OBJECTS, SkillStore
from jarvis.skill_types import Experience, ExperienceKind, SkillScope
from tests.helpers import isolated_settings, open_app, safe_cleanup
from tests import test_skill_core
from tests.test_skill_core import GLOBAL, CapturingConnector, user_task
from tests.test_skill_store import AT, SCOPE_A

TEXTS = {
    1: "Once dosyanin yerini kontrol et.",
    2: "Rapor basligini koru.",
    3: "Eski haftayi okuma.",
    **{
        number: f"Ders {number}: " + "rapor satirlarini tek tek ve dikkatle oku sonra ozetle " * 2
        for number in (4, 5, 6)
    },
}


def lesson(number: int, scope: SkillScope = GLOBAL, skill_id: str | None = None) -> Experience:
    return Experience(
        experience_id=f"exp-{number}",
        kind=ExperienceKind.AVOID,
        skill_id=skill_id,
        task_signature=("haftalik", "rapor"),
        text=TEXTS[number],
        scope=scope,
        source_run_id=f"run-{number}",
        created_at=AT,
    )


def raw(database: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql).fetchall()


class LessonCoreTests(unittest.TestCase):
    # The SKCORE harness, borrowed without collecting its tests here.
    setUp = test_skill_core.SkillCoreTests.setUp
    core = test_skill_core.SkillCoreTests.core
    save_skill = test_skill_core.SkillCoreTests.save_skill

    def run_skill(self) -> tuple[str, dict, str]:
        """Run the skill task to its end: (step summary, finished payload, anchor id)."""
        connector = CapturingConnector()
        core = self.core(connector)
        run = core.run(user_task())
        core.run_chain(run.session.id)
        anchor = run.session.objective.removeprefix("skill-run:")
        events = self.store.list_events(self.store.task_session_id(anchor))
        [finished] = [e["payload"] for e in events if e["event_type"] == "skill_run_finished"]
        self.assertEqual(finished["outcome"], "completed")
        [item] = [
            i for task in connector.seen for i in task.retrieved_context.items
            if i.source_task_id.startswith("skill-reference:")
        ]
        return item.summary, finished, anchor

    def test_injected_ids_are_recorded_and_lessons_used_is_counted(self) -> None:
        skill_id = self.save_skill()
        for number in (1, 2):
            self.store.add_experience(lesson(number, skill_id=skill_id))
        summary, finished, anchor = self.run_skill()
        self.assertIn(TEXTS[1], summary)
        self.assertIn(TEXTS[2], summary)
        self.assertEqual(finished["lessons_used"], 2)
        uses = raw(self.database, "SELECT run_id, experience_id FROM skill_experience_uses")
        self.assertEqual(sorted(uses), [(anchor, "exp-1"), (anchor, "exp-2")])
        # Ids only: no column could hold lesson text.
        columns = {row[1] for row in raw(self.database, "PRAGMA table_info(skill_experience_uses)")}
        self.assertEqual(columns, {"use_id", "run_id", "task_id", "experience_id", "created_at"})

    def test_disabled_and_deleted_lessons_are_never_selected(self) -> None:
        skill_id = self.save_skill()
        for number in (1, 2, 3):
            self.store.add_experience(lesson(number, skill_id=skill_id))
        self.store.set_lesson_state("exp-1", "disabled", None)
        self.store.set_lesson_state("exp-2", "deleted", None)
        summary, finished, _ = self.run_skill()
        self.assertNotIn(TEXTS[1], summary)
        self.assertNotIn(TEXTS[2], summary)
        self.assertIn(TEXTS[3], summary)
        self.assertEqual(finished["lessons_used"], 1)
        self.assertEqual(raw(self.database, "SELECT experience_id FROM skill_experience_uses"), [("exp-3",)])

    def test_a_lesson_that_does_not_fit_whole_is_neither_given_nor_counted(self) -> None:
        skill_id = self.save_skill()
        for number in (4, 5, 6):
            self.store.add_experience(lesson(number, skill_id=skill_id))
        summary, finished, _ = self.run_skill()
        given = [number for number in (4, 5, 6) if TEXTS[number].strip() in summary]
        self.assertTrue(1 <= len(given) < 3, summary)
        self.assertFalse(summary.endswith(chr(0x2026)))  # nothing cut mid-lesson
        self.assertEqual(finished["lessons_used"], len(given))
        self.assertEqual(
            sorted(raw(self.database, "SELECT experience_id FROM skill_experience_uses")),
            [(f"exp-{number}",) for number in given],
        )

    def test_a_run_without_lessons_records_none(self) -> None:
        self.save_skill()
        _, finished, _ = self.run_skill()
        self.assertEqual(finished["lessons_used"], 0)
        self.assertEqual(raw(self.database, "SELECT * FROM skill_experience_uses"), [])

    def test_a_failed_use_record_gives_no_lessons(self) -> None:
        skill_id = self.save_skill()
        self.store.add_experience(lesson(1, skill_id=skill_id))
        connector = CapturingConnector()
        with mock.patch.object(
            SkillStore, "record_lesson_uses", side_effect=sqlite3.OperationalError
        ) as record:
            self.core(connector).run(user_task())
        record.assert_called()  # the lesson was selected, then withheld
        self.assertEqual(len(connector.seen), 1)
        for item in connector.seen[0].retrieved_context.items:
            self.assertNotIn(TEXTS[1], item.summary)


class LessonAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self, skills_enabled=True)
        self.app = open_app(destination_policy=(), settings=self.settings)
        self.store = self.app.skill_store
        self.database = self.settings.memory_database

    def listed(self, project_id: str | None = None) -> list[tuple[str, str]]:
        result = self.app.list_lessons(project_id)
        self.assertTrue(result["ok"], result)
        return [(item["experience_id"], item["state"]) for item in result["lessons"]]

    def test_list_view_disable_delete(self) -> None:
        for number in (1, 2):
            self.store.add_experience(lesson(number))
        self.assertEqual(self.listed(), [("exp-1", "active"), ("exp-2", "active")])
        viewed = self.app.lesson("exp-1")
        self.assertTrue(viewed["ok"], viewed)
        self.assertEqual(viewed["lesson"]["text"], TEXTS[1])
        self.assertEqual(viewed["lesson"]["state"], "active")

        self.assertTrue(self.app.disable_lesson("exp-1")["ok"])
        self.assertEqual(self.listed(), [("exp-1", "disabled"), ("exp-2", "active")])
        self.assertEqual([e.experience_id for e in self.store.experiences(None)], ["exp-2"])

        before = raw(self.database, "SELECT * FROM skill_experiences ORDER BY experience_id")
        self.assertTrue(self.app.delete_lesson("exp-1")["ok"])
        self.assertTrue(self.app.delete_lesson("exp-2")["ok"])
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.store.experiences(None), ())
        # Append-only: the lesson rows are untouched, the state is three event rows.
        self.assertEqual(before, raw(self.database, "SELECT * FROM skill_experiences ORDER BY experience_id"))
        self.assertEqual(
            raw(self.database, "SELECT experience_id, kind, actor FROM skill_experience_events ORDER BY event_id"),
            [("exp-1", "disabled", "user"), ("exp-1", "deleted", "user"), ("exp-2", "deleted", "user")],
        )
        # A tombstone is final and the deleted text never comes back.
        answers = [
            self.app.list_lessons(),
            self.app.lesson("exp-1"),
            self.app.disable_lesson("exp-1"),
            self.app.delete_lesson("exp-1"),
            self.app.lesson("exp-2"),
        ]
        for answer in answers[1:]:
            self.assertFalse(answer["ok"])
            self.assertEqual(answer["error"], "no such lesson")
        dumped = json.dumps(answers, ensure_ascii=False)
        self.assertNotIn(TEXTS[1], dumped)
        self.assertNotIn(TEXTS[2], dumped)
        with self.assertRaises(ValueError):
            self.store.set_lesson_state("exp-1", "enabled", None)

    def test_a_disabled_lesson_can_be_enabled_again_in_the_store(self) -> None:
        self.store.add_experience(lesson(1))
        self.store.set_lesson_state("exp-1", "disabled", None)
        self.store.set_lesson_state("exp-1", "enabled", None)
        self.assertEqual(self.listed(), [("exp-1", "active")])

    def test_another_project_never_sees_or_controls_a_projects_lesson(self) -> None:
        self.store.add_experience(lesson(1, SCOPE_A))
        self.assertEqual(self.listed("proj-a"), [("exp-1", "active")])
        for project_id in ("proj-b", None):
            self.assertEqual(self.listed(project_id), [])
            for call in (self.app.lesson, self.app.disable_lesson, self.app.delete_lesson):
                answer = call("exp-1", project_id)
                self.assertEqual((answer["ok"], answer["error"]), (False, "no such lesson"))
                self.assertNotIn(TEXTS[1], json.dumps(answer, ensure_ascii=False))
        self.assertEqual(raw(self.database, "SELECT * FROM skill_experience_events"), [])
        self.assertEqual(len(self.store.experiences("proj-a")), 1)

    def test_bad_ids_and_states_are_refused(self) -> None:
        self.store.add_experience(lesson(1))
        for bad in ("exp-9", 1, None, ""):
            for call in (self.app.lesson, self.app.disable_lesson, self.app.delete_lesson):
                self.assertFalse(call(bad)["ok"])
        self.assertFalse(self.app.list_lessons("../x")["ok"])
        with self.assertRaises(ValueError):
            self.store.set_lesson_state("exp-1", "purged", None)
        with self.assertRaises(ValueError):
            self.store.set_lesson_state("exp-1", "disabled", None, actor="model")
        self.assertEqual(raw(self.database, "SELECT * FROM skill_experience_events"), [])

    def test_the_surface_works_with_skills_off(self) -> None:
        app = open_app(destination_policy=(), settings=isolated_settings(self, skills_enabled=False))
        app.skill_store.add_experience(lesson(1))
        self.assertEqual([item["experience_id"] for item in app.list_lessons()["lessons"]], ["exp-1"])
        self.assertTrue(app.disable_lesson("exp-1")["ok"])
        self.assertTrue(app.delete_lesson("exp-1")["ok"])
        self.assertEqual(app.list_lessons()["lessons"], [])


class LessonSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.database = Path(temp.name) / "jarvis.db"

    def objects(self) -> set[str]:
        return {row[0] for row in raw(self.database, "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}

    def test_skill_store_open_adds_exactly_the_lesson_objects(self) -> None:
        SQLiteMemoryStore(self.database)
        before = self.objects()
        self.assertFalse(LESSON_SCHEMA_OBJECTS & before)
        self.assertFalse(LESSON_SCHEMA_OBJECTS & COMPATIBILITY_SCHEMA_OBJECTS)
        SkillStore(self.database)
        SkillStore(self.database)  # idempotent
        self.assertEqual(self.objects() - before, LESSON_SCHEMA_OBJECTS)
        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertEqual(raw(self.database, "SELECT MAX(version) FROM schema_version"), [(10,)])


if __name__ == "__main__":
    unittest.main()
