"""V2.1a M3 "tüm projelerin hafızası": the per-conversation, default-off,
append-only all-projects memory toggle; the attach refusal once it was ever
on; read-only global list/search with source-project provenance.

Every database is a throwaway in TEMP; nothing opens the operator's database.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
from jarvis.memory import CONVERSATION_GLOBAL_MEMORY_TOGGLES, SQLiteMemoryStore
from jarvis.memory_filter import classify_subject_key
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from tests.helpers import safe_cleanup

USER = MemoryRecordActor.USER


class GlobalToggleTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.database = Path(temp.name) / "memory.db"
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id
        self.cid = self.store.create_conversation().id

    def sql(self, statement: str, params=()) -> list:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(statement, params).fetchall()
            connection.commit()
        return rows

    def rows(self) -> list:
        return self.sql(f"SELECT conversation_id, enabled, actor FROM {CONVERSATION_GLOBAL_MEMORY_TOGGLES} ORDER BY rowid")  # nosec B608 - constant table name

    def blocked(self, cid: str | None = None) -> str:
        return self.store.conversation_attach_preview(cid or self.cid, project_id=self.alpha)["blocked"]

    def add(self, text: str, project: str, conversation: str | None = None,
            kind: MemoryRecordKind = MemoryRecordKind.FACT) -> str:
        record = MemoryRecord(
            kind=kind, text=text, subject_key="tooling",
            scope=MemoryRecordScope(project, conversation),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.MODEL, confidence=0.8,
            classification=DataClassification.INTERNAL,
        )
        # A guard-less capture FACT/LESSON is now PENDING (SECCAPTURE): stand
        # in for Core's clean user_stated guard.
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (), kind,
                            content_hash(text)) if kind in (MemoryRecordKind.FACT, MemoryRecordKind.LESSON) else None
        return self.store.insert_record(record, guard).record.record_id  # a FACT starts APPROVED

    def global_scope(self, cid: str | None = None) -> MemoryRecordScope:
        return MemoryRecordScope(None, cid or self.cid, MemoryRecordScopeKind.GLOBAL_READONLY)

    # -- toggle -----------------------------------------------------------

    def test_default_off_latest_row_wins_and_rows_are_append_only(self) -> None:
        self.assertFalse(self.store.global_memory_enabled(self.cid))
        self.store.set_global_memory_toggle(self.cid, True, USER)
        self.assertTrue(self.store.global_memory_enabled(self.cid))
        self.store.set_global_memory_toggle(self.cid, False, USER)
        self.assertFalse(self.store.global_memory_enabled(self.cid))
        self.assertEqual([(self.cid, 1, "user"), (self.cid, 0, "user")], self.rows())

    def test_only_the_user_may_toggle_and_inputs_are_validated(self) -> None:
        for actor in (MemoryRecordActor.MODEL, MemoryRecordActor.TOOL, "user"):
            with self.assertRaises(ValueError):
                self.store.set_global_memory_toggle(self.cid, True, actor)
        for enabled in (1, "yes", None):
            with self.assertRaises(ValueError):
                self.store.set_global_memory_toggle(self.cid, enabled, USER)
        for cid in ("c-" + "0" * 32, "not-a-conversation"):
            with self.assertRaises(ValueError):
                self.store.set_global_memory_toggle(cid, True, USER)
        self.assertEqual([], self.rows())

    def test_unreadable_table_reads_as_off(self) -> None:
        self.store.set_global_memory_toggle(self.cid, True, USER)
        self.sql(f"DROP TABLE {CONVERSATION_GLOBAL_MEMORY_TOGGLES}")
        self.sql(f"CREATE TABLE {CONVERSATION_GLOBAL_MEMORY_TOGGLES} (something_else TEXT)")
        self.assertFalse(self.store.global_memory_enabled(self.cid))

    # -- attach rule ------------------------------------------------------

    def test_on_then_off_still_refuses_attach(self) -> None:
        self.store.set_global_memory_toggle(self.cid, True, USER)
        self.store.set_global_memory_toggle(self.cid, False, USER)
        self.assertIn("all projects memory", self.blocked())
        with self.assertRaises(ValueError):
            self.store.attach_global_conversation_to_project(
                self.cid, project_id=self.alpha,
                expected_next_seq=self.store.load_conversation_state(self.cid).next_seq,
            )

    def test_never_on_is_attachable(self) -> None:
        self.store.set_global_memory_toggle(self.cid, False, USER)
        self.store.set_global_memory_toggle(self.cid, False, USER)
        self.assertEqual("", self.blocked())
        self.store.attach_global_conversation_to_project(
            self.cid, project_id=self.alpha,
            expected_next_seq=self.store.load_conversation_state(self.cid).next_seq,
        )
        self.assertEqual(self.alpha, self.store.load_conversation(self.cid).project_id)

    def test_unreadable_table_refuses_attach(self) -> None:
        self.sql(f"DROP TABLE {CONVERSATION_GLOBAL_MEMORY_TOGGLES}")
        self.sql(f"CREATE TABLE {CONVERSATION_GLOBAL_MEMORY_TOGGLES} (something_else TEXT)")
        self.assertIn("cannot be read", self.blocked())

    # -- global reads -----------------------------------------------------

    def test_global_reads_need_the_toggle_on(self) -> None:
        self.add("alpha uses ruff", self.alpha)
        off = "all projects memory is off"
        with self.assertRaisesRegex(ValueError, off):
            self.store.list_records(self.global_scope())
        with self.assertRaisesRegex(ValueError, off):
            self.store.search_records("ruff", self.global_scope())
        self.store.set_global_memory_toggle(self.cid, True, USER)
        self.store.set_global_memory_toggle(self.cid, False, USER)
        with self.assertRaisesRegex(ValueError, off):
            self.store.list_records(self.global_scope())
        with self.assertRaisesRegex(ValueError, off):
            self.store.search_records("ruff", self.global_scope())
        with self.assertRaisesRegex(ValueError, "conversation id must be"):  # no conversation named
            self.store.list_records(MemoryRecordScope(None, kind=MemoryRecordScopeKind.GLOBAL_READONLY))
        # A toggle row whose conversation does not exist reads nothing.
        ghost = "c-" + "2" * 32
        self.sql(f"INSERT INTO {CONVERSATION_GLOBAL_MEMORY_TOGGLES} (conversation_id, enabled, actor, at) "  # nosec B608 - constant table name
                 "VALUES (?, 1, 'user', '2026-10-13T00:00:00+00:00')", (ghost,))
        with self.assertRaisesRegex(ValueError, "needs an existing conversation"):
            self.store.list_records(self.global_scope(ghost))

    def test_global_reads_span_projects_with_provenance_and_stay_read_only(self) -> None:
        mine = self.add("alpha uses ruff", self.alpha)
        theirs = self.add("beta uses ruff too", self.beta)
        other_conversation = self.add("beta private ruff", self.beta, "c-" + "1" * 32)
        pending = self.add("beta pending ruff", self.beta, kind=MemoryRecordKind.DECISION)
        self.assertEqual("pending", self.sql(
            "SELECT status FROM memory_record_events WHERE record_id = ? ORDER BY rowid DESC LIMIT 1", (pending,))[0][0])
        self.store.set_global_memory_toggle(self.cid, True, USER)
        before = self.sql("SELECT COUNT(*) FROM memory_records UNION ALL SELECT COUNT(*) FROM memory_record_events")
        listed = {v.record.record_id: v.record.scope.project_id for v in self.store.list_records(self.global_scope())}
        found = {v.record.record_id: v.record.scope.project_id
                 for v, _ in self.store.search_records("ruff", self.global_scope())}
        expected = {mine: self.alpha, theirs: self.beta}
        self.assertEqual(expected, listed)
        self.assertEqual(expected, found)
        self.assertNotIn(other_conversation, listed)
        self.assertNotIn(pending, listed)  # only APPROVED crosses projects
        self.assertEqual([], self.store.list_records(self.global_scope(), statuses=[MemoryRecordStatus.PENDING]))
        self.assertEqual(before, self.sql("SELECT COUNT(*) FROM memory_records UNION ALL SELECT COUNT(*) FROM memory_record_events"))
        # The project scope is unchanged: Alpha still sees only Alpha.
        self.assertEqual([mine], [v.record.record_id for v in self.store.list_records(MemoryRecordScope(self.alpha))])

    def global_ids(self, query: str = "ruff", limit: int = 20) -> tuple[set, set]:
        listed = {v.record.record_id for v in self.store.list_records(self.global_scope())}
        found = {v.record.record_id for v, _ in self.store.search_records(query, self.global_scope(), limit=limit)}
        return listed, found

    @staticmethod
    def forced_secret(record_id: str):
        """Row-level SECRET fixture: the schema refuses classification='secret'
        and MemoryRecord refuses SECRET, so the row is turned SECRET as it is
        read back, past both walls, to reach the global read filter."""
        original = SQLiteMemoryStore._record_view_from_row

        def view_from_row(row):
            view = original(row)
            if view is not None and view.record.record_id == record_id:
                object.__setattr__(view.record, "classification", DataClassification.SECRET)
            return view

        return mock.patch.object(SQLiteMemoryStore, "_record_view_from_row", staticmethod(view_from_row))

    def test_global_reads_never_return_secret_or_filter_refused_rows(self) -> None:
        clean = self.add("beta uses ruff", self.beta)
        sensitive = self.add("beta ruff sensitive row", self.beta)
        secret = self.add("beta ruff secret row", self.beta)
        leaked = self.add("beta ruff tampered row", self.beta)
        self.sql("UPDATE memory_records SET classification = 'sensitive' WHERE record_id = ?", (sensitive,))
        # A credential that reached the table past the write-time checks.
        self.sql("UPDATE memory_records SET text = 'ruff password: hunter2xyz' WHERE record_id = ?", (leaked,))
        self.store.set_global_memory_toggle(self.cid, True, USER)
        with self.forced_secret(secret):
            listed, found = self.global_ids()
            # The project scope still reads it: only the global read bans SECRET.
            in_project = {v.record.record_id for v in self.store.list_records(MemoryRecordScope(self.beta))}
        self.assertEqual({clean, sensitive}, listed)
        self.assertEqual({clean, sensitive}, found)
        self.assertIn(secret, in_project)

    def test_search_pages_past_refused_rows_and_never_under_delivers(self) -> None:
        refused = [self.add(f"beta ruff tampered row {n}", self.beta) for n in range(6)]
        for record_id in refused:
            self.sql("UPDATE memory_records SET text = 'ruff password: hunter2xyz' WHERE record_id = ?", (record_id,))
        clean = {self.add(f"beta uses ruff in the long build pipeline {n}", self.beta) for n in range(2)}
        self.store.set_global_memory_toggle(self.cid, True, USER)
        for fts in (True, False):
            with self.subTest(fts=fts), mock.patch("jarvis.memory.memory_records_fts_available", return_value=fts), \
                    mock.patch("jarvis.memory._RECORD_LIST_MAX_LIMIT", 3):  # page size 3: 8 rows span three pages
                for limit in (1, 2, 3):
                    found = [v.record.record_id for v, _ in
                             self.store.search_records("ruff", self.global_scope(), limit=limit)]
                    self.assertEqual(min(limit, 2), len(found))
                    self.assertTrue(set(found) <= clean)
                project = [v.record.record_id for v, _ in self.store.search_records("ruff", MemoryRecordScope(self.beta), limit=2)]
                self.assertEqual(clean, set(project))

    def test_reads_recheck_the_subject_key_against_normalized_secret_shapes(self) -> None:
        # The key normalizer folds case and separators, hiding these shapes
        # from the text filter; the read filter must still refuse each.
        shapes = {
            "separator": "ghp-abcdefghijklmnopqrstuvwxyz0123456789",
            "case": "akiaiosfodnn7example",
            "space": "bearer-eyjhbgcioijiuzi1nij9-eyjzdwiioiixin0-abcdefghijk",
            "plain": "password-hunter2xyz",
            "underscore-pat": "github-pat-11abcdefg0123456789-abcdefghijklmnopqrstuvwxyz",
        }
        legit = self.add("beta ruff legit key", self.beta)
        self.sql("UPDATE memory_records SET subject_key = 'password-policy' WHERE record_id = ?", (legit,))
        tampered = {}
        for name, key in shapes.items():
            tampered[name] = self.add(f"beta ruff key {name}", self.beta)
            self.sql("UPDATE memory_records SET subject_key = ? WHERE record_id = ?", (key, tampered[name]))
        self.store.set_global_memory_toggle(self.cid, True, USER)
        listed, found = self.global_ids()
        in_project = {v.record.record_id for v in self.store.list_records(MemoryRecordScope(self.beta))}
        for name, record_id in tampered.items():
            with self.subTest(name):
                self.assertNotIn(record_id, listed)
                self.assertNotIn(record_id, found)
                self.assertNotIn(record_id, in_project)
        self.assertEqual({legit}, listed)
        self.assertEqual({legit}, found)


class SubjectKeyFilterTests(unittest.TestCase):
    def test_folded_secret_shapes_are_refused_and_plain_keys_pass(self) -> None:
        refused = ["ghp-abcdefghijklmnopqrstuvwxyz0123456789", "akiaiosfodnn7example",
                   "password-hunter2xyz", "bearer-eyjhbgcioijiuzi1nij9-eyjzdwiioiixin0-abcdefghijk",
                   "tooling-password-hunter2xyz"]
        allowed = ["tooling", "python-version", "password-policy", "api-key-rotation-policy",
                   "secret-management", "deploy-token-rotation", "aws-region", "openai-api-key"]
        for key in refused:
            with self.subTest(refused=key):
                self.assertFalse(classify_subject_key(key).allowed)
        for key in allowed:
            with self.subTest(allowed=key):
                self.assertTrue(classify_subject_key(key).allowed)
        self.assertFalse(classify_subject_key(None).allowed)  # fail-closed on a non-string


if __name__ == "__main__":
    unittest.main()
