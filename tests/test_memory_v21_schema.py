"""V2.1 M1: the typed-memory tables arrive through the compatibility layer only.

SCHEMA_VERSION stays 10, nothing is ALTERed, a database the pre-change code
built gains the tables idempotently, and the pre-change code still opens the
result. The FTS5 index is created fail-closed. Every database here is a
throwaway in TEMP; nothing opens the operator's database.
"""

from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import memory
from jarvis.contracts import ProjectRecord
from jarvis.memory import (
    COMPATIBILITY_SCHEMA_OBJECTS,
    CONVERSATION_GLOBAL_MEMORY_TOGGLES,
    MEMORY_RECORDS_FTS,
    MEMORY_V21_SCHEMA_OBJECTS,
    SCHEMA_VERSION,
    SQLiteMemoryStore,
    memory_records_fts_available,
)
from tests.helpers import safe_cleanup

REPO = Path(__file__).resolve().parents[1]
#: The last commit before the V2.1 tables (this slice's base).
PRE_CHANGE_COMMIT = "df463a5"

V21_TABLES = {
    "memory_records",
    "memory_record_events",
    "memory_record_supersessions",
    CONVERSATION_GLOBAL_MEMORY_TOGGLES,
    "memory_capture_runs",
}


def objects(database: Path) -> dict[str, str]:
    connection = sqlite3.connect(database)
    try:
        return dict(connection.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall())
    finally:
        connection.close()


def record(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "record_id": "r1", "kind": "fact", "text": "uses uv", "subject_key": "tooling",
        "project_id": "alpha", "conversation_id": None, "scope_kind": "project",
        "origin": "explicit_user", "actor": "user", "tool": None, "confidence": 0.9,
        "classification": "internal", "created_at": "2026-10-12T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.database = self.root / "jarvis.db"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        return connection


class FreshDatabaseTests(Case):
    def test_every_new_object_exists_and_the_version_stays_ten(self) -> None:
        store = SQLiteMemoryStore(self.database)
        names = set(objects(self.database))
        self.assertLessEqual(MEMORY_V21_SCHEMA_OBJECTS, COMPATIBILITY_SCHEMA_OBJECTS)
        self.assertLessEqual(V21_TABLES, MEMORY_V21_SCHEMA_OBJECTS)
        self.assertLessEqual(MEMORY_V21_SCHEMA_OBJECTS, names)
        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(10, store.schema_version())
        connection = self.connect()
        # The version lives in schema_version; PRAGMA user_version was never used and stays untouched.
        self.assertEqual(0, connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual([(10,)], connection.execute("SELECT version FROM schema_version").fetchall())
        for table in V21_TABLES:
            with self.subTest(table=table):
                self.assertEqual(0, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # nosec B608 - test constants

    def test_the_fts_index_exists_where_sqlite_has_fts5(self) -> None:
        if not memory.MEMORY_RECORDS_FTS5_AVAILABLE:
            self.skipTest("this SQLite has no FTS5")
        SQLiteMemoryStore(self.database)
        self.assertIn(MEMORY_RECORDS_FTS, MEMORY_V21_SCHEMA_OBJECTS)
        self.assertTrue(memory_records_fts_available(self.connect()))

    def test_the_constraints_refuse_bad_records(self) -> None:
        store = SQLiteMemoryStore(self.database)
        store.save_project(ProjectRecord(id="alpha", name="Alpha"))
        connection = self.connect()

        def insert(row: dict[str, object]) -> None:
            columns = ", ".join(row)
            connection.execute(f"INSERT INTO memory_records ({columns}) VALUES ({', '.join('?' * len(row))})",  # nosec B608 - test constants
                               tuple(row.values()))

        insert(record())
        for bad in ({"kind": "guess"}, {"origin": "model_whim"}, {"actor": "admin"}, {"confidence": 1.5},
                    {"confidence": -0.1}, {"text": None}, {"text": ""}, {"classification": "secret"},
                    {"scope_kind": "global_readonly"}):
            with self.subTest(bad=bad), self.assertRaises(sqlite3.IntegrityError):
                insert(record(record_id="bad", **bad))
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO memory_record_events (record_id, status, actor, at) VALUES ('r1', 'live', 'user', 'a')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO memory_record_supersessions (old_record_id, new_record_id, actor, at)"
                               " VALUES ('r1', 'r1', 'user', 'a')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(f"INSERT INTO {CONVERSATION_GLOBAL_MEMORY_TOGGLES} VALUES ('c', 2, 'user', 'a')")  # nosec B608 - test constant
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO memory_capture_runs (run_id, conversation_id, trigger, started_at)"
                               " VALUES ('x', 'c', 'whenever', 'a')")
        # A record's status is its latest event; old_record_id is unique.
        connection.execute("INSERT INTO memory_record_events (record_id, status, actor, at) VALUES ('r1', 'pending', 'model', 'a')")
        connection.execute("INSERT INTO memory_record_events (record_id, status, actor, at) VALUES ('r1', 'approved', 'user', 'b')")
        self.assertEqual("approved", connection.execute(
            "SELECT status FROM memory_record_events WHERE record_id = 'r1' ORDER BY event_id DESC LIMIT 1").fetchone()[0])
        insert(record(record_id="r2"))
        insert(record(record_id="r3"))
        connection.execute("INSERT INTO memory_record_supersessions (old_record_id, new_record_id, actor, at) VALUES ('r1', 'r2', 'user', 'a')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO memory_record_supersessions (old_record_id, new_record_id, actor, at) VALUES ('r1', 'r3', 'user', 'a')")


class FtsFailClosedTests(Case):
    def test_no_fts5_means_no_index_a_false_flag_and_an_open_store(self) -> None:
        with mock.patch.object(memory, "MEMORY_RECORDS_FTS5_AVAILABLE", False):
            SQLiteMemoryStore(self.database)
            self.assertFalse(memory_records_fts_available(self.connect()))
        names = set(objects(self.database))
        self.assertNotIn(MEMORY_RECORDS_FTS, names)
        self.assertLessEqual(V21_TABLES, names)

    def test_a_failing_create_rolls_back_to_its_savepoint_only(self) -> None:
        with mock.patch.object(memory, "_MEMORY_RECORDS_FTS_SQL",
                               f"CREATE VIRTUAL TABLE {MEMORY_RECORDS_FTS} USING no_such_module(text)"), \
                self.assertLogs("jarvis.memory", "WARNING"):
            store = SQLiteMemoryStore(self.database)
        store.save_project(ProjectRecord(id="alpha", name="Alpha"))  # the open committed and works
        names = set(objects(self.database))
        self.assertFalse({n for n in names if n.startswith(MEMORY_RECORDS_FTS)})
        self.assertLessEqual(V21_TABLES, names)
        self.assertFalse(memory_records_fts_available(self.connect()))
        SQLiteMemoryStore(self.database)  # a later open with FTS5 builds it
        if memory.MEMORY_RECORDS_FTS5_AVAILABLE:
            self.assertTrue(memory_records_fts_available(self.connect()))

    def test_the_probe_is_false_when_sqlite_refuses(self) -> None:
        with mock.patch.object(memory.sqlite3, "connect", side_effect=sqlite3.OperationalError("no fts5")):
            self.assertFalse(memory._sqlite_has_fts5())

    def test_an_unreadable_schema_reports_false(self) -> None:
        broken = mock.Mock()
        broken.execute.side_effect = sqlite3.DatabaseError("malformed")
        self.assertFalse(memory_records_fts_available(broken))


class PreChangeDatabaseTests(Case):
    """The pre-change code from a throwaway extraction of its commit."""

    def setUp(self) -> None:
        super().setUp()
        known = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{PRE_CHANGE_COMMIT}^{{commit}}"],
                               capture_output=True)
        if known.returncode != 0:
            self.skipTest("the pre-change commit is not in this clone")
        archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", PRE_CHANGE_COMMIT, "jarvis"],
                                 capture_output=True, check=True).stdout
        self.old_tree = self.root / "pre-change"
        self.old_tree.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(self.old_tree, filter="data")

    def old_open(self) -> str:
        script = ("import sys, jarvis.memory as m\n"
                  "assert m.__file__.startswith(sys.argv[2]), m.__file__\n"
                  "print(m.SQLiteMemoryStore(sys.argv[1]).schema_version())\n")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        done = subprocess.run([sys.executable, "-c", script, str(self.database), str(self.old_tree)],
                              cwd=self.old_tree, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout.strip().splitlines()[-1]

    def test_a_pre_change_database_gains_the_tables_idempotently_and_still_opens_in_the_old_code(self) -> None:
        self.assertEqual("10", self.old_open())
        before = objects(self.database)
        self.assertFalse(MEMORY_V21_SCHEMA_OBJECTS & set(before))
        SQLiteMemoryStore(self.database)
        after = objects(self.database)
        self.assertEqual(MEMORY_V21_SCHEMA_OBJECTS, set(after) - set(before))
        for name, sql in before.items():
            self.assertEqual(sql, after[name], name)
        SQLiteMemoryStore(self.database)
        self.assertEqual(after, objects(self.database))  # a second open is a no-op
        # Rollback: the schema-10 pre-change code opens the V2.1 database.
        self.assertEqual("10", self.old_open())
        self.assertEqual(after, objects(self.database))


if __name__ == "__main__":
    unittest.main()
