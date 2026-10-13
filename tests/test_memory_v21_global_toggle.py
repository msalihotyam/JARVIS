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
        return self.store.insert_record(record).record.record_id  # a FACT starts APPROVED

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

    def test_global_reads_never_return_secret_or_filter_refused_rows(self) -> None:
        clean = self.add("beta uses ruff", self.beta)
        secret = self.add("beta ruff secret row", self.beta)
        leaked = self.add("beta ruff tampered row", self.beta)
        # The schema itself never stores a SECRET row (the read filter is a second wall).
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE memory_records SET classification = 'secret' WHERE record_id = ?", (secret,))
        self.sql("UPDATE memory_records SET classification = 'sensitive' WHERE record_id = ?", (secret,))
        # A credential that reached the table past the write-time checks.
        self.sql("UPDATE memory_records SET text = 'ruff password: hunter2xyz' WHERE record_id = ?", (leaked,))
        self.store.set_global_memory_toggle(self.cid, True, USER)
        self.assertEqual({clean, secret}, {v.record.record_id for v in self.store.list_records(self.global_scope())})
        self.assertEqual({clean, secret}, {v.record.record_id for v, _ in self.store.search_records("ruff", self.global_scope())})


if __name__ == "__main__":
    unittest.main()
