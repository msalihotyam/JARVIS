"""scripts/skill_history_count.py on synthetic temp databases (never data\\jarvis.db)."""

from __future__ import annotations

import contextlib
import io
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.contracts import DataClassification, SessionState, TaskStatus
from jarvis.memory import SQLiteMemoryStore
from jarvis.skill_draft import build_drafts
from jarvis.skill_store import SkillStore
from jarvis.skill_types import RunOutcome, RunStep, RunSummary
from tests.helpers import make_task, safe_cleanup

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "skill_history_count.py"
_spec = importlib.util.spec_from_file_location("skill_history_count", _SCRIPT)
shc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shc)

CANARY = "QXCANARY7731"


def _task(objective, capability, status, target="", classification=DataClassification.INTERNAL):
    task = make_task(target=target, classification=classification)
    task.objective, task.capability, task.status = objective, capability, status
    return task


class SkillHistoryCountTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.dir = Path(temp.name)
        self.db = self.dir / "jarvis.db"

    def build(self, *, skill: bool = False) -> None:
        memory = SQLiteMemoryStore(self.db)
        session = SessionState(objective=f"session {CANARY}")
        memory.save_session(session)
        done, failed = TaskStatus.COMPLETED, TaskStatus.FAILED
        read = f"read notes/a.md {CANARY}"
        for task in (
            _task(read, "file_read", done, "notes/a.md"),
            _task(f"hello {CANARY}", "chat", done),
            _task(read, "file_read", failed, "notes/a.md"),
            _task(read, "file_read", done, "notes/a.md", DataClassification.SENSITIVE),
            _task(f"later {CANARY}", "file_read", TaskStatus.RUNNING, "notes/a.md"),
        ):
            memory.save_task(session.id, task)
        if skill:
            store = SkillStore(self.db)
            run = RunSummary(
                run_id="seed-run",
                task_text=read,
                project_id=None,
                status=RunOutcome.COMPLETED,
                steps=(RunStep(action=read, capability="file_read", outcome=RunOutcome.COMPLETED, error=None),),
                user_correction=None,
                matched_skill=None,
            )
            draft = build_drafts(run)[0]
            store.add_draft(draft)
            store.approve_draft(draft.draft_id, project_id=None)
        connection = sqlite3.connect(self.db)
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.close()
        self.assertEqual(os.listdir(self.dir), ["jarvis.db"])

    def run_script(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = shc.main(["--db", str(self.db)])
        return code, out.getvalue(), err.getvalue()

    def counts(self) -> dict[str, int]:
        code, out, err = self.run_script()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(os.listdir(self.dir), ["jarvis.db"])  # nothing created next to it
        return {key: int(value) for key, value in (line.split(" ") for line in out.splitlines())}

    def assertCounts(self, counts: dict[str, int], expected: dict[str, int]) -> None:
        self.assertEqual({key: counts.get(key, 0) for key in expected}, expected)

    def test_counts_without_an_active_skill(self) -> None:
        self.build()
        self.assertCounts(self.counts(), {
            "sessions": 1, "tasks": 5, "rows_unreadable": 0, "finished": 4, "unfinished": 1,
            "status.completed": 3, "status.failed": 1, "standalone": 4, "chain_step": 0,
            # completed file read: offered, plannable, lesson refused by the a2 allow-list
            "learn.replayed": 2, "offer_reason.offer": 1, "offered": 1, "drafts_offered": 1,
            "drafts_plannable": 1, "drafts_unplannable": 0, "lesson_captured": 0,
            "lesson_skipped_external": 1,
            "offer_reason.chat_only": 1,  # the chat answer
            "learn.not_representable": 1,  # the sensitive task
            "match.none": 3, "match.not_eligible": 1, "match.fit": 0,
            "family.file.finished": 3, "family.file.offered": 1, "family.chat.finished": 1,
            "family.chat.offered": 0,
        })

    def test_counts_with_an_active_skill(self) -> None:
        self.build(skill=True)
        self.assertCounts(self.counts(), {
            "match.fit": 2, "match.similar": 1, "match.not_eligible": 1, "match.none": 0,
            # b8: a run identical to the active skill offers no duplicate draft
            "family.file.match.fit": 2, "family.chat.match.similar": 1, "offered": 0,
            "skill_rows_broken": 0,
        })

    def test_a_non_read_only_open_is_refused(self) -> None:
        self.build()
        uri = shc.read_only_uri(self.db)
        self.assertTrue(uri.startswith("file:") and uri.endswith("?mode=ro"))
        shc.connect(uri).close()
        missing = shc.read_only_uri(self.dir / "new.db").replace("mode=ro", "mode=rwc")
        for bad in (
            uri.replace("mode=ro", "mode=rw"),
            uri.replace("mode=ro", "mode=rwc"),
            uri + "&immutable=1",
            uri.replace("?mode=ro", ""),
            str(self.db),
            missing,
        ):
            with self.subTest(bad=bad[-24:]), self.assertRaises(ValueError):
                shc.connect(bad)
        with shc.connect(uri) as connection, self.assertRaises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE x (y)")
        self.assertEqual(os.listdir(self.dir), ["jarvis.db"])

    def test_a_wal_database_or_side_files_are_not_run(self) -> None:
        self.build()
        connection = sqlite3.connect(self.db)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.close()
        self.assertEqual(os.listdir(self.dir), ["jarvis.db"])
        code, out, err = self.run_script()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("WAL mode", err)
        self.assertEqual(os.listdir(self.dir), ["jarvis.db"])  # no -wal/-shm created

        (self.dir / "jarvis.db-wal").write_bytes(b"")
        code, out, err = self.run_script()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("side files present", err)

    def test_a_canary_in_every_text_column_never_reaches_the_output(self) -> None:
        self.build(skill=True)
        code, out, err = self.run_script()
        self.assertEqual(code, 0)
        self.assertNotIn(CANARY, out + err)
        # Now every TEXT column of every row carries it (append-only triggers and CHECKs off).
        connection = sqlite3.connect(self.db)
        connection.execute("PRAGMA ignore_check_constraints = ON")
        for (trigger,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            connection.execute(f'DROP TRIGGER "{trigger}"')
        seeded = 0
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        for (table,) in tables:
            for column in connection.execute(f'PRAGMA table_info("{table}")').fetchall():
                if "TEXT" in column[2].upper():
                    connection.execute(
                        f'UPDATE "{table}" SET "{column[1]}" = "{column[1]}" || ? '  # nosec B608 - temp DB's own schema names
                        f'WHERE "{column[1]}" IS NOT NULL',
                        (" " + CANARY,),
                    )
                    seeded += 1
        connection.commit()
        connection.close()
        self.assertGreater(seeded, 20)
        code, out, err = self.run_script()
        self.assertEqual(code, 0)
        self.assertNotIn(CANARY, out + err)
        self.assertEqual(self.counts()["rows_unreadable"], 6)  # every session and task row


if __name__ == "__main__":
    unittest.main()
