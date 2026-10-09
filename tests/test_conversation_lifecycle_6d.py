"""V2 Projects Gate 6D-b: conversation archive, unarchive, the archived
sections and the permanent delete (Salih decisions 2026-09-30), plus the two
window fixes that ride with it (GAP A drop, 6C re-open with state).

A. Store: archive / unarchive round trip; refusals under an archived project
   and with open work; every delete refusal writes nothing; the delete under
   an archived project; the tombstone; after a delete every reader hides it
   and every writer refuses.
B. Legacy: a deleted legacy import is never imported again; a changed digest
   is (documented behaviour).
C. Atomicity: an injected failure after the UPDATE rolls back; secure_delete
   is restored.
D. Schema: 10, migrations 1..10, conversations columns unchanged, exactly one
   new compatibility object.
E. Core / app: a deleted id is refused before the model; F2 and F1 still
   fail closed; GAP B is the accepted, documented behaviour (no Core change).
F. Authority: writers only in memory.py, single app.py callers, no CLI or
   intent path.
G. Window: the open-conversation transition in every branch, the archived
   sections, the dialog text, Vazgeç, the busy guard, "Önce arşivleyin".
H. GAP A: a late reply for a deleted or corrupt conversation is dropped.
I. 6C: after an attach the earlier messages stay on screen and in history.
J. Turkish catalog.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from jarvis.app import conversation_delete_result
from jarvis.contracts import ChainState, ErrorCode, RunStatus, SessionState, TaskStatus, chain_steps_from_declaration
from jarvis.conversation import ConversationState, ConversationStore, append_turn, encode_state
from jarvis.intent import IntentStatus
from jarvis.memory import COMPATIBILITY_SCHEMA_OBJECTS, SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.settings import Settings
from tests.helpers import make_task, safe_cleanup, settled, wait_until
from tests.test_chain_cancel_cli import DECLARATION
from tests.test_intent import IntentTestCase, ScriptedModel, chat_proposal
from tests.test_projects_ui import ProjectsUiCase
from tests.test_session_owner_binding import OwnerBindingTests
from tests.test_ui import UiTestCase, _APP, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

WATCHED = ("projects", "conversations", "conversation_sessions", "conversation_moves", "conversation_legacy_imports",
           "conversation_deletions", "sessions", "tasks", "pending_approvals", "event_log", "decision_log",
           "loop_checkpoints", "execution_attempts", "project_memory_items")
KEPT = ("sessions", "tasks", "pending_approvals", "event_log", "decision_log", "loop_checkpoints", "execution_attempts")
CHAT = json.dumps({"kind": "chat", "reply": "tamam"})


def snapshot(database: Path) -> dict[str, list]:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return {table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in WATCHED}


def row(database: Path, conversation_id: str) -> tuple:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return connection.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()


def say(memory: SQLiteMemoryStore, conversation_id: str, text: str) -> None:
    memory.update_conversation_state(conversation_id, lambda s: append_turn(
        s, role="user", kind="user", text=text, message=text, key=text)[0])


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.root = Path(temp.name)
        self.database = self.root / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.p = self.memory.create_project("P")
        self.conversation = self.memory.create_conversation(title="Tatil")
        say(self.memory, self.conversation.id, "ilk mesaj")
        say(self.memory, self.conversation.id, "ikinci mesaj")

    def linked_session(self, conversation_id: str | None = None) -> SessionState:
        session = SessionState(objective="window session")
        self.memory.save_session(session)
        self.memory.link_session_conversation(session.id, conversation_id or self.conversation.id)
        return session

    def delete(self, conversation_id: str | None = None, **overrides):
        conversation_id = conversation_id or self.conversation.id
        preview = self.memory.conversation_delete_preview(conversation_id)
        arguments = {"confirm": conversation_id, "expected_next_seq": preview["expected_next_seq"],
                     "expected_title": preview["title"], **overrides}
        return self.memory.delete_conversation(conversation_id, **arguments)

    def assertRefusedNothingWritten(self, attempt, message: str, error=ValueError) -> None:
        before = snapshot(self.database)
        with self.assertRaises(error) as refused:
            attempt()
        self.assertIn(message, str(refused.exception))
        self.assertEqual(before, snapshot(self.database))


class ArchiveStoreTests(StoreCase):
    def test_a_archive_unarchive_round_trip_leaves_the_state_unchanged(self) -> None:
        before = row(self.database, self.conversation.id)
        archived = self.memory.archive_conversation(self.conversation.id)
        self.assertIsNotNone(archived.archived_at)
        self.assertNotIn(self.conversation.id, [c.id for c in self.memory.list_conversations()])
        self.assertIn(self.conversation.id, [c.id for c in self.memory.list_conversations(include_archived=True)])
        self.assertEqual(archived.archived_at, self.memory.archive_conversation(self.conversation.id).archived_at)
        self.assertIsNone(self.memory.unarchive_conversation(self.conversation.id).archived_at)
        self.assertEqual(before, row(self.database, self.conversation.id))          # every column, as it was
        self.assertRefusedNothingWritten(lambda: self.memory.unarchive_conversation(self.conversation.id),
                                         "not archived")

    def test_a_archive_and_unarchive_are_refused_under_an_archived_project(self) -> None:
        owned = self.memory.create_conversation(self.p.id)
        archived = self.memory.create_conversation(self.p.id)
        self.memory.archive_conversation(archived.id)
        self.memory.set_project_active(self.p.id, False)
        self.assertRefusedNothingWritten(lambda: self.memory.archive_conversation(owned.id), "archived project")
        self.assertRefusedNothingWritten(lambda: self.memory.unarchive_conversation(archived.id),
                                         "unarchive the project first")
        self.memory.set_project_active(self.p.id, True)
        self.assertIsNone(self.memory.unarchive_conversation(archived.id).archived_at)

    def test_a_archive_is_refused_with_open_work(self) -> None:
        session = self.linked_session()
        task = make_task()
        self.memory.save_task(session.id, task)
        self.assertRefusedNothingWritten(lambda: self.memory.archive_conversation(self.conversation.id),
                                         "task in this conversation")
        task.status = TaskStatus.COMPLETED
        self.memory.save_task(session.id, task)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO pending_approvals (id, task_id, session_id, request_json, status, created_at,"
                               " expires_at) VALUES ('r1', ?, ?, '{}', 'pending', 'a', 'b')", (task.id, session.id))
            connection.commit()
        self.assertRefusedNothingWritten(lambda: self.memory.archive_conversation(self.conversation.id),
                                         "approval in this conversation")
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE pending_approvals SET status = 'rejected'")
            connection.commit()
        chained = self.linked_session()
        chain = ChainState.declared(chain_steps_from_declaration([dict(s) for s in DECLARATION[:2]]))
        self.assertEqual("declared", self.memory.declare_chain_if_absent(chained.id, chain))
        self.assertRefusedNothingWritten(lambda: self.memory.archive_conversation(self.conversation.id),
                                         "chain in this conversation")
        other = self.memory.create_conversation()
        broken = self.linked_session(other.id)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE sessions SET state_json = 'not json' WHERE id = ?", (broken.id,))
            connection.commit()
        self.assertRefusedNothingWritten(lambda: self.memory.archive_conversation(other.id), "cannot be archived")


class DeleteStoreTests(StoreCase):
    def test_a_every_delete_refusal_writes_nothing(self) -> None:
        # Not archived, of an ACTIVE project: archive first. (Gate 6F F7: a Global one needs no archive -
        # test_a_f7_a_global_conversation_is_deleted_without_an_archive.)
        owned = self.memory.create_conversation(self.p.id, title="Proje")
        for conversation_id in (owned.id,):
            with self.subTest(conversation=conversation_id):
                self.assertIn("Archive this conversation first",
                              self.memory.conversation_delete_preview(conversation_id)["blocked"])
                self.assertRefusedNothingWritten(lambda: self.delete(conversation_id), "Archive this conversation first")
        # Open work in a linked session of an archived conversation.
        session = self.linked_session()
        self.memory.archive_conversation(self.conversation.id)
        task = make_task()
        self.memory.save_task(session.id, task)
        self.assertRefusedNothingWritten(lambda: self.delete(), "task in this conversation")
        task.status = TaskStatus.CANCELLED
        self.memory.save_task(session.id, task)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO pending_approvals (id, task_id, session_id, request_json, status, created_at,"
                               " expires_at) VALUES ('r1', ?, ?, '{}', 'pending', 'a', 'b')", (task.id, session.id))
            connection.commit()
        self.assertRefusedNothingWritten(lambda: self.delete(), "approval in this conversation")
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE pending_approvals SET status = 'rejected'")
            connection.commit()
        chain = ChainState.declared(chain_steps_from_declaration([dict(s) for s in DECLARATION[:2]]))
        self.assertEqual("declared", self.memory.declare_chain_if_absent(session.id, chain))
        self.assertRefusedNothingWritten(lambda: self.delete(), "chain in this conversation")
        # An unreadable linked session fails closed.
        other = self.memory.create_conversation()
        broken = self.linked_session(other.id)
        self.memory.archive_conversation(other.id)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE sessions SET state_json = 'not json' WHERE id = ?", (broken.id,))
            connection.commit()
        self.assertRefusedNothingWritten(lambda: self.delete(other.id), "cannot be deleted")
        # The preview binds the confirm: next_seq AND title.
        clean = self.memory.create_conversation(title="Bağlı")
        say(self.memory, clean.id, "bir")
        self.memory.archive_conversation(clean.id)
        preview = self.memory.conversation_delete_preview(clean.id)
        self.memory.unarchive_conversation(clean.id)
        say(self.memory, clean.id, "iki")
        self.memory.archive_conversation(clean.id)
        self.assertRefusedNothingWritten(lambda: self.memory.delete_conversation(
            clean.id, confirm=clean.id, expected_next_seq=preview["expected_next_seq"], expected_title="Bağlı"),
            "changed after the preview; preview again")
        preview = self.memory.conversation_delete_preview(clean.id)
        self.memory.unarchive_conversation(clean.id)
        self.memory.set_conversation_title(clean.id, "Yeni ad")
        self.memory.archive_conversation(clean.id)
        self.assertRefusedNothingWritten(lambda: self.memory.delete_conversation(
            clean.id, confirm=clean.id, expected_next_seq=preview["expected_next_seq"], expected_title=preview["title"]),
            "changed after the preview; preview again")
        # The confirmation must be the id itself, and the binding values well-typed.
        self.assertRefusedNothingWritten(lambda: self.delete(clean.id, confirm="Yeni ad"), "confirmation equal")
        self.assertRefusedNothingWritten(lambda: self.delete(clean.id, expected_next_seq="3"), "integer")
        # Already deleted.
        self.delete(clean.id)
        self.assertRefusedNothingWritten(lambda: self.memory.delete_conversation(
            clean.id, confirm=clean.id, expected_next_seq=1, expected_title=""), "Unknown conversation")

    def test_a_delete_under_an_archived_project_and_the_tombstone(self) -> None:
        # A Global conversation moved to P (a conversation_moves row), with a
        # finished session and task, then P archived: deletable at once.
        moved = self.memory.create_conversation(title="Taşınan")
        say(self.memory, moved.id, "gizli ayrıntı")
        self.memory.attach_global_conversation_to_project(moved.id, expected_next_seq=2, project_id=self.p.id)
        session = self.linked_session(moved.id)
        task = make_task()
        task.status = TaskStatus.COMPLETED
        self.memory.save_task(session.id, task)
        self.memory.set_project_active(self.p.id, False)
        preview = self.memory.conversation_delete_preview(moved.id)
        self.assertEqual(("", None, 1, "Taşınan"), (preview["blocked"], preview["archived_at"], preview["turns"],
                                                    preview["title"]))
        self.assertEqual({"project_id": self.p.id, "name": "P", "active": False}, preview["owner"])
        before = snapshot(self.database)
        created = row(self.database, moved.id)
        deleted = self.delete(moved.id)
        after = snapshot(self.database)
        empty_json, empty_integrity = encode_state(ConversationState())
        tombstone = dict(zip(("id", "project_id", "title", "state_json", "integrity", "origin", "created_at",
                              "updated_at", "archived_at"), row(self.database, moved.id)))
        self.assertEqual({"id": moved.id, "project_id": self.p.id, "title": "", "state_json": empty_json,
                          "integrity": empty_integrity, "origin": "jarvis", "created_at": created[6],
                          "updated_at": deleted["deleted_at"], "archived_at": deleted["deleted_at"]}, tombstone)
        self.assertNotIn("gizli ayrıntı", json.dumps(after["conversations"], ensure_ascii=False))
        self.assertEqual([(moved.id, deleted["deleted_at"])], after["conversation_deletions"])
        for table in ("conversation_sessions", "conversation_moves", "conversation_legacy_imports", *KEPT):
            self.assertEqual(before[table], after[table], table)
        # A Global conversation archived first keeps its archive time.
        self.memory.set_project_active(self.p.id, True)
        archived = self.memory.archive_conversation(self.conversation.id)
        self.delete()
        self.assertEqual(archived.archived_at, row(self.database, self.conversation.id)[8])

    def test_a_after_a_delete_every_reader_hides_it_and_every_writer_refuses(self) -> None:
        session = self.linked_session()
        self.memory.archive_conversation(self.conversation.id)
        self.delete()
        cid = self.conversation.id
        for listing in (self.memory.list_conversations(), self.memory.list_conversations(include_archived=True),
                        self.memory.list_conversations(project_id=None, include_archived=True)):
            self.assertNotIn(cid, [c.id for c in listing])
        self.assertIsNone(self.memory.load_conversation(cid))
        self.assertTrue(self.memory.is_conversation_deleted(cid))
        self.assertEqual(cid, self.memory.conversation_for_session(session.id))          # the link is kept
        before = snapshot(self.database)
        for name, attempt, error in (
            ("load state", lambda: self.memory.load_conversation_state(cid), KeyError),
            ("update", lambda: say(self.memory, cid, "geç"), KeyError),
            ("title", lambda: self.memory.set_conversation_title(cid, "x"), KeyError),
            ("archive", lambda: self.memory.archive_conversation(cid), KeyError),
            ("unarchive", lambda: self.memory.unarchive_conversation(cid), KeyError),
            ("link", lambda: self.memory.link_session_conversation(session.id, cid), KeyError),
            ("attach", lambda: self.memory.attach_global_conversation_to_project(
                cid, expected_next_seq=1, project_id=self.p.id), ValueError),
            ("preview", lambda: self.memory.conversation_delete_preview(cid), ValueError),
            ("delete", lambda: self.memory.delete_conversation(cid, confirm=cid, expected_next_seq=1, expected_title=""),
             ValueError),
        ):
            with self.subTest(name), self.assertRaises(error):
                attempt()
        self.assertEqual(before, snapshot(self.database))

    def test_a_f7_a_global_conversation_is_deleted_without_an_archive(self) -> None:
        """Gate 6F F7 (Salih 2026-10-05): a Global conversation is deleted in one step; the open-work
        refusal, the preview binding and the tombstone are unchanged; a project's still needs its archive."""
        cid = self.conversation.id
        preview = self.memory.conversation_delete_preview(cid)
        self.assertEqual(("", None, 2, None), (preview["blocked"], preview["archived_at"], preview["turns"], preview["owner"]))
        session = self.linked_session()
        task = make_task()
        self.memory.save_task(session.id, task)
        self.assertIn("task in this conversation", self.memory.conversation_delete_preview(cid)["blocked"])
        self.assertRefusedNothingWritten(lambda: self.delete(), "task in this conversation")
        task.status = TaskStatus.CANCELLED
        self.memory.save_task(session.id, task)
        held = self.memory.conversation_delete_preview(cid)
        say(self.memory, cid, "üçüncü mesaj")
        self.assertRefusedNothingWritten(lambda: self.memory.delete_conversation(
            cid, confirm=cid, expected_next_seq=held["expected_next_seq"], expected_title=held["title"]),
            "changed after the preview; preview again")
        self.assertRefusedNothingWritten(lambda: self.delete(cid, confirm="Tatil"), "confirmation equal")
        deleted = self.delete()
        self.assertTrue(self.memory.is_conversation_deleted(cid))
        self.assertEqual(deleted["deleted_at"], row(self.database, cid)[8])            # archived_at set by the delete
        self.assertEqual([(cid, deleted["deleted_at"])], snapshot(self.database)["conversation_deletions"])
        self.assertEqual(cid, self.memory.conversation_for_session(session.id))         # the link is kept
        owned = self.memory.create_conversation(self.p.id, title="Proje")
        self.assertIn("Archive this conversation first", self.memory.conversation_delete_preview(owned.id)["blocked"])
        self.assertRefusedNothingWritten(lambda: self.delete(owned.id), "Archive this conversation first")

    def test_a_the_app_results_report_refusals(self) -> None:
        settings = Settings(memory_database=self.database)
        # Gate 6F F7: an ACTIVE project's conversation (a Global one needs no archive any more).
        owned = self.memory.create_conversation(self.p.id, title="Tatil")
        say(self.memory, owned.id, "ilk mesaj")
        say(self.memory, owned.id, "ikinci mesaj")
        refused = conversation_delete_result(self.memory, settings, owned.id, confirm=owned.id,
                                             expected_next_seq=3, expected_title="Tatil")
        self.assertEqual("invalid_task", refused.payload["error"]["code"])
        self.assertIn("Archive this conversation first", refused.payload["error"]["message"])
        json.dumps(refused.payload)


class LegacyTests(StoreCase):
    def test_b_a_deleted_legacy_import_is_never_imported_again(self) -> None:
        legacy = self.root / "conversation.json"
        store = ConversationStore(legacy)
        store.append(role="user", kind="user", text="V2 öncesi", message="V2 öncesi", key="legacy-0")
        imported = self.memory.import_legacy_conversation(legacy)
        self.memory.archive_conversation(imported.id)
        self.assertEqual("legacy-import", self.memory.conversation_delete_preview(imported.id)["origin"])
        self.delete(imported.id)
        # "Restart": a fresh store imports the same file again - nothing new.
        self.assertIsNone(SQLiteMemoryStore(self.database).import_legacy_conversation(legacy))
        self.assertEqual([self.conversation.id], [c.id for c in self.memory.list_conversations(include_archived=True)])
        # DOCUMENTED: a changed file (a new digest - e.g. a rollback release
        # wrote to it) is one more recovered conversation, with every turn it holds.
        store.append(role="user", kind="user", text="eski sürümde", message="eski sürümde", key="legacy-1")
        recovered = SQLiteMemoryStore(self.database).import_legacy_conversation(legacy)
        self.assertIsNotNone(recovered)
        self.assertEqual(["V2 öncesi", "eski sürümde"],
                         [t.message for t in self.memory.load_conversation_state(recovered.id).turns])


class AtomicityTests(StoreCase):
    def test_c_a_failure_after_the_update_rolls_back_and_secure_delete_is_restored(self) -> None:
        self.memory.archive_conversation(self.conversation.id)
        before = snapshot(self.database)
        seen: list[int] = []
        original = self.memory._connect

        class Tracked:
            """A real connection that reports secure_delete as it is closed."""

            def __init__(self) -> None:
                self._real = original()

            def __getattr__(self, name):
                return getattr(self._real, name)

            def close(self) -> None:
                seen.append(int(self._real.execute("PRAGMA secure_delete").fetchone()[0]))
                self._real.close()

        with contextlib.closing(original()) as probe:
            baseline = int(probe.execute("PRAGMA secure_delete").fetchone()[0])
        preview = self.memory.conversation_delete_preview(self.conversation.id)
        arguments = {"confirm": self.conversation.id, "expected_next_seq": preview["expected_next_seq"],
                     "expected_title": preview["title"]}
        with mock.patch.object(self.memory, "_connect", side_effect=Tracked), \
                mock.patch.object(SQLiteMemoryStore, "_record_conversation_deletion",
                                  side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.memory.delete_conversation(self.conversation.id, **arguments)
        self.assertEqual(before, snapshot(self.database))                        # the UPDATE is rolled back too
        self.assertEqual([baseline], seen)
        self.assertEqual(2, len(self.memory.load_conversation_state(self.conversation.id).turns))
        seen.clear()
        with mock.patch.object(self.memory, "_connect", side_effect=Tracked):
            self.memory.delete_conversation(self.conversation.id, **arguments)
        self.assertEqual([baseline], seen)
        self.assertTrue(self.memory.is_conversation_deleted(self.conversation.id))


class SchemaTests(StoreCase):
    BEFORE_6DB = {"conversations", "idx_conversations_owner", "conversation_sessions", "conversation_legacy_imports",
                  "conversation_moves", "project_instructions", "idx_project_instructions_current", "project_files",
                  "idx_project_files_active_sha", "project_file_chunks"}

    def rows(self, sql: str) -> list:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql).fetchall()

    def test_d_schema_ten_and_exactly_one_new_compatibility_object(self) -> None:
        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual([(10,)], self.rows("SELECT version FROM schema_version"))
        self.assertEqual(list(range(1, 11)), [r[0] for r in self.rows("SELECT version FROM schema_migrations ORDER BY version")])
        self.assertEqual({"conversation_deletions"}, COMPATIBILITY_SCHEMA_OBJECTS - self.BEFORE_6DB)
        self.assertLessEqual(self.BEFORE_6DB, COMPATIBILITY_SCHEMA_OBJECTS)
        self.assertEqual(["id", "project_id", "title", "state_json", "integrity", "origin", "created_at", "updated_at",
                          "archived_at"], [r[1] for r in self.rows("PRAGMA table_info(conversations)")])   # never ALTERed
        self.assertEqual(["conversation_id", "deleted_at"],
                         [r[1] for r in self.rows("PRAGMA table_info(conversation_deletions)")])
        self.assertEqual({"conversations"}, {r[2] for r in self.rows("PRAGMA foreign_key_list(conversation_deletions)")})


class CoreAppTests(IntentTestCase):
    def deleted(self, owner: str | None = None) -> str:
        conversation = self.memory.create_conversation(owner)
        self.memory.archive_conversation(conversation.id)
        preview = self.memory.conversation_delete_preview(conversation.id)
        self.memory.delete_conversation(conversation.id, confirm=conversation.id,
                                        expected_next_seq=preview["expected_next_seq"], expected_title="")
        return conversation.id

    def test_e_a_deleted_id_is_refused_before_the_model(self) -> None:
        model = ScriptedModel(chat_proposal("never"))
        payload = self.build(model).submit_text("merhaba", conversation_id=self.deleted()).payload
        self.assertNothingHappened(payload, IntentStatus.INVALID_INPUT)
        self.assertIn("Unknown conversation", payload["intent"]["message"])
        self.assertEqual([], model.calls)

    def test_e_f2_refuses_a_session_of_a_deleted_conversation(self) -> None:
        session = SessionState(objective="window session")
        self.memory.save_session(session)
        conversation = self.memory.create_conversation()
        self.memory.link_session_conversation(session.id, conversation.id)
        self.memory.archive_conversation(conversation.id)
        preview = self.memory.conversation_delete_preview(conversation.id)
        self.memory.delete_conversation(conversation.id, confirm=conversation.id,
                                        expected_next_seq=preview["expected_next_seq"], expected_title="")
        other = self.memory.create_conversation()
        model = ScriptedModel(chat_proposal("never"))
        payload = self.build(model).submit_text("merhaba", session.id, conversation_id=other.id).payload
        self.assertNothingHappened(payload, IntentStatus.INVALID_INPUT)
        self.assertIn("another project scope", payload["intent"]["message"])
        self.assertEqual([], model.calls)


class CoreOwnerTests(unittest.TestCase):
    setUp = OwnerBindingTests.setUp
    task = OwnerBindingTests.task

    def test_e_f1_refuses_a_project_task_and_gap_b_is_the_accepted_behaviour(self) -> None:
        session = SessionState(objective="window session")
        self.memory.save_session(session)
        conversation = self.memory.create_conversation(self.p.id)
        self.memory.link_session_conversation(session.id, conversation.id)
        self.memory.archive_conversation(conversation.id)
        preview = self.memory.conversation_delete_preview(conversation.id)
        self.memory.delete_conversation(conversation.id, confirm=conversation.id,
                                        expected_next_seq=preview["expected_next_seq"], expected_title="")
        refused = self.core.run(self.task(self.p.id), session.id)
        self.assertEqual((RunStatus.FAILED, ErrorCode.INVALID_TASK), (refused.status, refused.error.code))
        self.assertIn("owner of the session's conversation", refused.error.message)
        # GAP B (Salih decision S3, ARCHITECTURE 4.22): a task naming NO project
        # on that session (`jarvis run --session-id`) is not refused by Core;
        # accepted for V2.0 - it writes to no conversation.
        unbound = self.core.run(self.task(None), session.id)
        self.assertEqual(RunStatus.COMPLETED, unbound.status, unbound.error)
        self.assertTrue(self.memory.is_conversation_deleted(conversation.id))
        with self.assertRaises(KeyError):
            self.memory.load_conversation_state(conversation.id)


class AuthorityScanTests(unittest.TestCase):
    RESULTS = {"archive_conversation": "conversation_archive_result",
               "unarchive_conversation": "conversation_unarchive_result",
               "conversation_delete_preview": "conversation_delete_preview_result",
               "delete_conversation": "conversation_delete_result"}
    WRITERS = ("archive_conversation", "unarchive_conversation", "delete_conversation")

    def test_f_writers_have_one_app_caller_and_no_other_path(self) -> None:
        root = Path(__file__).resolve().parents[1] / "jarvis"
        sources = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in root.rglob("*.py")}
        app = sources["app.py"]
        for writer in self.WRITERS:
            with self.subTest(writer=writer):
                # Only the store defines it and only app.py calls it (``.unarchive_`` is not ``.archive_``).
                mentions = sorted(n for n, s in sources.items() if f".{writer}(" in s.replace(f".un{writer}(", "")
                                  or f"def {writer}(" in s)
                self.assertEqual(["app.py", "memory.py"], mentions)
        for method, result in self.RESULTS.items():
            with self.subTest(result=result):
                self.assertEqual(1, app.count(f"memory.{method}("))
                self.assertLess(app.index(f"def {result}("), app.index(f"memory.{method}("))
                self.assertIn(f"{result}(", sources["ui/backend.py"])
                for name, text in sources.items():
                    if name not in ("app.py", "ui/backend.py", "memory.py"):             # memory.py: docstrings only
                        self.assertNotIn(result, text, name)
                self.assertNotIn(f"{result}(", sources["memory.py"])
        for sql in ("INSERT INTO conversation_deletions", "UPDATE conversations SET archived_at",
                    "UPDATE conversations SET title = ''"):
            self.assertEqual(["memory.py"], [n for n, s in sources.items() if sql in s], sql)
        # No CLI, no intent capability.
        for name in ("cli.py", "intent.py", "scheduler_runner.py", "dispatch.py"):
            for word in ("conversation_archive", "conversation_delete", "delete_conversation", "archive_conversation"):
                self.assertNotIn(word, sources[name], (name, word))


class WindowCase(ProjectsUiCase):
    def press(self, root, item, button=Qt.LeftButton, then=None) -> None:
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(root, button, Qt.NoModifier, center)
        if then is not None:        # THIZ: the caller's assertions; one that holds at once keeps the old wait
            settled(260, then)
        else:
            pump(260)

    def confirm_delete(self, root) -> None:
        """Gate 6G K2: the destructive confirm arms Theme.confirmGuardMs (300 ms) after the
        dialog opens; press it once armed (the old 260 ms wait fell inside the guard)."""
        confirm = self.visible(root, "deleteConfirm")[0]
        wait_until(confirm.isEnabled, 1000)
        self.press(root, confirm)

    def visible(self, root, name) -> list:
        return [i for i in self.all_items(root, name) if i.isVisible()]

    def context(self, root) -> None:
        """The open conversation's menu: the chat header's "…" (on the home page)."""
        self.home.setPage("home")
        pump(300)
        self.press(root, self.item(root, "chatHeaderMenu"))

    def window(self, bridge):
        self.home = bridge
        return super().window(bridge)

    def archive_open(self, root) -> None:
        self.context(root)
        self.press(root, self.item(root, "conversationArchiveItem"))

    def delete_open(self, root) -> None:
        self.context(root)
        self.press(root, self.item(root, "conversationDeleteItem"))
        self.confirm_delete(root)

    def gone(self, bridge, store, conversation_id: str) -> None:
        self.assertNotEqual(conversation_id, bridge.conversationId)
        self.assertTrue(store.is_conversation_deleted(conversation_id))
        listed = [r["id"] for r in (*bridge.conversations, *bridge.archivedConversations, *bridge.scopeConversations,
                                    *bridge.projectConversations, *bridge.projectArchivedConversations)]
        self.assertNotIn(conversation_id, listed)


class TransitionTests(WindowCase):
    """N2 (Salih 2026-10-05, replacing the Gate 6D-b "newest of the scope, on its list" rule):
    the open conversation archived or deleted -> the New chat screen: a new, empty
    conversation of its scope when writable, else Global, on the chat page."""

    def new_chat(self, bridge, store, before, project_id=None) -> str:
        now = bridge.conversationId
        self.assertNotIn(now, before)
        record = store.load_conversation(now)
        self.assertEqual((project_id, None, ""), (record.project_id, record.archived_at, record.title))
        self.assertEqual(0, len(store.load_conversation_state(now).turns))
        self.assertEqual(([], "home", False), (bridge.feed, bridge.page, bridge.conversationReadOnly))
        return now

    def test_g_writable_scope_with_another_conversation(self) -> None:
        store = self.db()
        bridge = self.bridge()
        root = self.window(bridge)
        older = bridge.conversationId
        store.set_conversation_title(older, "Eski")
        project = store.create_project("Evim")
        first, second = store.create_conversation(project.id, title="Bir"), store.create_conversation(project.id)
        newer = store.create_conversation(title="Yeni")
        say(store, newer.id, "silinecek")
        # Archive of the open Global conversation: a new Global one, not the newest other one.
        bridge.openConversation(newer.id)
        pump(200)
        self.archive_open(root)
        self.assertIsNotNone(store.load_conversation(newer.id).archived_at)
        fresh = self.new_chat(bridge, store, (older, newer.id))
        self.assertIn(newer.id, [r["id"] for r in bridge.archivedConversations])
        settled(300, lambda: (
            self.assertEqual("home", root.property("loadedPage")),
            self.assertTrue(self.item(root, "conversationWelcome").isVisible()),
        ))
        # Archive of an open project conversation: a new one of that project, on the chat page.
        bridge.openConversation(second.id)
        pump(200)
        self.archive_open(root)
        self.new_chat(bridge, store, (first.id, second.id, fresh), project.id)
        self.assertIsNotNone(store.load_conversation(second.id).archived_at)
        bridge.showProject(project.id)
        self.assertEqual([second.id], [r["id"] for r in bridge.projectArchivedConversations])
        # Delete of the OPEN archived Global conversation (opened read-only): a new Global one.
        bridge.openConversation(newer.id)
        settled(200, lambda: (
            self.assertTrue(bridge.conversationReadOnly),
        ))
        self.delete_open(root)
        self.gone(bridge, store, newer.id)
        self.new_chat(bridge, store, (older, newer.id, fresh))
        self.assertEqual(0, self.execute_calls())

    def test_g_archived_project_falls_back_to_a_new_global_chat(self) -> None:
        store = self.db()
        bridge = self.bridge()
        root = self.window(bridge)
        global_id = bridge.conversationId
        project = store.create_project("Tez")
        owned = store.create_conversation(project.id, title="Tez sohbeti")
        store.create_conversation(project.id, title="Başka")                   # never chosen: the project is archived
        store.set_project_active(project.id, False)
        bridge.openConversation(owned.id)
        pump(200)
        self.context(root)
        self.assertFalse(self.item(root, "conversationArchiveItem").isEnabled())    # already read-only
        self.assertTrue(self.item(root, "conversationDeleteItem").isEnabled())       # directly under an archived project
        self.press(root, self.item(root, "conversationDeleteItem"))
        self.confirm_delete(root)
        self.gone(bridge, store, owned.id)
        self.new_chat(bridge, store, (global_id, owned.id))                          # never in the archived project

    def test_g_no_global_conversation_opens_a_new_one(self) -> None:
        store = self.db()
        bridge = self.bridge()
        root = self.window(bridge)
        startup = bridge.conversationId
        project = store.create_project("Evim")
        only = store.create_conversation(project.id, title="Tek")
        store.archive_conversation(startup)
        preview = store.conversation_delete_preview(startup)
        store.delete_conversation(startup, confirm=startup, expected_next_seq=preview["expected_next_seq"],
                                  expected_title=preview["title"])
        # Archive: the project has no other conversation and no Global one exists - a new one of the project.
        bridge.openConversation(only.id)
        pump(200)
        self.archive_open(root)
        created = self.new_chat(bridge, store, (startup, only.id), project.id)
        # Delete under the project archived meanwhile: a new Global one.
        store.set_project_active(project.id, False)
        bridge.openConversation(only.id)
        pump(200)
        self.delete_open(root)
        self.gone(bridge, store, only.id)
        self.new_chat(bridge, store, (startup, only.id, created))


class ArchivedSectionTests(WindowCase):
    def test_g_history_section_open_read_only_and_unarchive(self) -> None:
        store = self.db()
        archived = store.create_conversation(title="Arşivde")
        say(store, archived.id, "okunur")
        store.archive_conversation(archived.id)
        bridge = self.bridge()
        root = self.window(bridge)
        kept = bridge.conversationId                                                # startup: a new Global one
        bridge.setPage("history")
        pump(400)
        rows = self.visible(root, "historyArchivedRow")
        self.assertEqual(1, len(rows))
        self.assertEqual([archived.id], [r["id"] for r in bridge.archivedConversations])
        self.assertNotIn(archived.id, [r["id"] for r in bridge.conversations])
        # Opening it: read-only, its own banner and its own name.
        self.assertTrue(bridge.openConversation(archived.id))
        settled(200, lambda: (
            self.assertEqual((True, True, "Arşivde"), (bridge.conversationReadOnly, bridge.conversationArchived,
                                                        bridge.conversationTitle)),
        ))
        bridge.setPage("home")
        settled(300, lambda: (
            self.assertIn("this conversation is archived", self.item(root, "readOnlyReason").property("text")),
            self.assertTrue(self.item(root, "sidebarNewConversation").property("allowed")),   # a Global one may still start
        ))
        # Unarchive from History: back in the lists, writable again.
        bridge.setPage("history")
        pump(300)
        self.press(root, self.visible(root, "historyUnarchive")[0])
        self.assertIsNone(store.load_conversation(archived.id).archived_at)
        self.assertIn(archived.id, [r["id"] for r in bridge.conversations])
        self.assertEqual([], bridge.archivedConversations)
        self.assertFalse(bridge.conversationReadOnly)
        self.assertNotEqual(kept, archived.id)

    def test_g_project_section_and_the_archived_project_rules(self) -> None:
        store = self.db()
        bridge = self.bridge()
        root = self.window(bridge)
        project = store.create_project("Evim")
        live = store.create_conversation(project.id, title="Canlı")
        archived = store.create_conversation(project.id, title="Arşivli")
        store.archive_conversation(archived.id)
        bridge.showProject(project.id)
        pump(400)
        self.assertEqual([live.id], [r["id"] for r in bridge.projectConversations])
        self.assertEqual([archived.id], [r["id"] for r in bridge.projectArchivedConversations])
        self.assertFalse(self.visible(root, "projectConversationDelete"))                 # active project: archive first
        self.assertTrue(self.visible(root, "projectConversationUnarchive")[0].isEnabled())
        # Archived project: delete offered on every row, unarchive disabled with its reason.
        store.set_project_active(project.id, False)
        bridge.refreshProjects()
        pump(300)
        self.assertEqual(1, len(self.visible(root, "projectConversationDelete")))
        self.assertFalse(self.visible(root, "projectConversationUnarchive")[0].isEnabled())
        self.assertEqual("Unarchive the project first",
                         self.item(root, "projectConversationUnarchiveReason").property("text"))
        self.assertFalse(bridge.unarchiveConversation(archived.id))                       # a forced call meets the store
        self.assertIn("unarchive the project first", bridge.conversationNotice)
        self.press(root, self.visible(root, "projectArchivedConversationDelete")[0])
        self.confirm_delete(root)
        self.assertTrue(store.is_conversation_deleted(archived.id))
        self.assertEqual([], bridge.projectArchivedConversations)


class DialogTests(WindowCase):
    def test_g_dialog_text_exactly_and_the_legacy_line_only_for_legacy(self) -> None:
        from jarvis.ui.i18n import localization
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        store = self.db()
        legacy = self.root / "conversation.json"
        legacy_store = ConversationStore(legacy)
        legacy_store.append(role="user", kind="user", text="eski", message="eski", key="legacy-0")
        imported = store.import_legacy_conversation(legacy)
        store.archive_conversation(imported.id)
        named = store.create_conversation(title="Tatil planı")
        say(store, named.id, "bir")
        say(store, named.id, "iki")
        store.archive_conversation(named.id)
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setLanguage("tr-TR")
        pump(300)
        bridge.setPage("history")
        pump(400)
        texts = lambda name: self.visible(root, name)[0].property("text")           # noqa: E731
        by_id = {r["id"]: i for i, r in enumerate(bridge.archivedConversations)}
        self.press(root, self.visible(root, "historyDelete")[by_id[named.id]], then=lambda: (
            self.assertEqual("Bu sohbet kalıcı olarak silinsin mi?", texts("deleteTitle")),
        ))
        identity = texts("deleteIdentity")
        self.assertTrue(identity.startswith("«Tatil planı» · 2 mesaj · oluşturma "), identity)
        self.assertIn(" · son değişiklik ", identity)
        self.assertTrue(identity.endswith(" · Genel"), identity)
        self.assertEqual("SİLİNECEK: bu sohbetin mesajları ve başlığı (sohbetin içinde tutulan görev başlıkları dahil) "
                         "JARVIS sohbet deposundan. Sohbet hiçbir listede görünmez ve açılamaz.", texts("deleteRemoved"))
        self.assertEqual("KALANLAR: bu sohbete bağlı oturum, görev, checkpoint, onay ve denetim kayıtları (görev "
                         "sonuçları Görevler listesinde kalır); sağlayıcılara zaten gönderilmiş içerik; veritabanının "
                         "WAL dosyası, boş sayfaları ve depolama katmanında kalabilecek kopyalar; yedekler.",
                         texts("deleteKept"))
        self.assertEqual([], self.visible(root, "deleteLegacy"))
        self.assertEqual("Bu işlem geri alınamaz.", texts("deleteIrreversible"))
        self.assertEqual(["Vazgeç", "Kalıcı sil"],
                         [self.visible(root, n)[0].property("label") for n in ("deleteCancel", "deleteConfirm")])
        # Vazgeç writes nothing.
        before = snapshot(self.database)
        self.press(root, self.visible(root, "deleteCancel")[0])
        self.assertEqual(before, snapshot(self.database))
        self.assertEqual([], self.visible(root, "deleteTitle"))
        # The legacy import: the untitled fallback and the legacy line.
        self.press(root, self.visible(root, "historyDelete")[by_id[imported.id]], then=lambda: (
            self.assertTrue(texts("deleteIdentity").startswith("«Yeni Sohbet» · 1 mesaj · ")),
            self.assertEqual("Ayrıca eski sürümün sohbet dosyası (~/.jarvis/conversation.json) yerinde kalır.",
                             texts("deleteLegacy")),
        ))
        self.confirm_delete(root)
        self.assertTrue(store.is_conversation_deleted(imported.id))
        self.assertTrue(legacy.exists())                                            # S4: the file stays
        self.assertIsNone(store.import_legacy_conversation(legacy))

    def test_g_a_changed_conversation_refreshes_the_preview_and_is_never_retried(self) -> None:
        store = self.db()
        conversation = store.create_conversation(title="Ad")
        store.archive_conversation(conversation.id)
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("history")
        pump(400)
        self.press(root, self.visible(root, "historyDelete")[0])
        store.unarchive_conversation(conversation.id)
        store.set_conversation_title(conversation.id, "Başka ad")
        store.archive_conversation(conversation.id)
        calls, real = [], bridge.backend.conversation_delete
        bridge.backend.conversation_delete = lambda *a, **k: calls.append(k) or real(*a, **k)
        before = snapshot(self.database)
        self.confirm_delete(root)
        self.assertEqual(before, snapshot(self.database))
        self.assertEqual(1, len(calls))                                             # one call, never retried
        self.assertIn("preview again", bridge.conversationNotice)
        self.assertEqual("Başka ad", bridge.deletePreview["title"])                # the refreshed preview
        self.confirm_delete(root)
        self.assertTrue(store.is_conversation_deleted(conversation.id))

    def test_g_busy_disables_every_item_and_delete_needs_an_archive_first(self) -> None:
        store = self.db()
        owned = store.create_conversation(store.create_project("Ev").id, title="Ev sohbeti")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.openConversation(owned.id)
        pump(200)                                 # F7: a project's (Global: one step)
        self.context(root)
        self.assertFalse(self.item(root, "conversationDeleteItem").isEnabled())
        self.assertEqual("Archive it first", self.item(root, "conversationDeleteReason").property("text"))
        self.assertTrue(self.item(root, "conversationArchiveItem").isEnabled())
        bridge._busy = True
        bridge.stateChanged.emit()
        pump(60)
        for name in ("conversationRenameItem", "conversationArchiveItem", "conversationDeleteItem"):
            self.assertFalse(self.item(root, name).isEnabled(), name)
        conversation_id = bridge.conversationId
        self.assertFalse(bridge.archiveConversation(conversation_id))
        store.archive_conversation(conversation_id)                                  # the store's own archive, directly
        before = snapshot(self.database)
        self.assertFalse(bridge.unarchiveConversation(conversation_id))
        bridge.previewDeleteConversation(conversation_id)
        self.assertFalse(bridge.confirmDeleteConversation(conversation_id))
        self.assertEqual(before, snapshot(self.database))                            # nothing while busy
        bridge._busy = False

    def test_g_archive_refusal_is_shown_and_the_conversation_stays_open(self) -> None:
        store = self.db()
        bridge = self.bridge()
        root = self.window(bridge)
        conversation_id = bridge.conversationId
        session = SessionState(objective="window session")
        store.save_session(session)
        store.link_session_conversation(session.id, conversation_id)
        store.save_task(session.id, make_task())
        self.archive_open(root)
        self.assertIn("not finished", bridge.conversationNotice)
        self.assertTrue(self.item(root, "conversationLifecycleNotice").isVisible())
        self.assertEqual(conversation_id, bridge.conversationId)
        self.assertIsNone(store.load_conversation(conversation_id).archived_at)


class GapATests(WindowCase):
    REPLY = {"intent": {"status": "conversation", "message": "geç kalan yanıt"},
             "run": {"session": {"id": ""}}}

    def late(self, bridge, session_id: str, origin=None) -> list:
        handled = []
        if origin is not None:
            bridge._conversation_store, open_now = origin, bridge._conversation_store
        deliver = bridge._delivered(handled.append)
        if origin is not None:
            bridge._conversation_store = open_now
        payload = json.loads(json.dumps(self.REPLY))
        payload["run"]["session"]["id"] = session_id
        deliver(payload)
        return handled

    def linked(self, store, conversation_id: str) -> SessionState:
        session = SessionState(objective="window session")
        store.save_session(session)
        store.link_session_conversation(session.id, conversation_id)
        return session

    def test_h_a_late_reply_for_a_deleted_or_corrupt_conversation_is_dropped(self) -> None:
        store = self.db()
        bridge = self.bridge()
        self.window(bridge)
        doomed = store.create_conversation(title="Silinecek")
        session = self.linked(store, doomed.id)
        origin = bridge._load_conversation(store, doomed.id)                        # a request started in it
        store.archive_conversation(doomed.id)
        preview = store.conversation_delete_preview(doomed.id)
        store.delete_conversation(doomed.id, confirm=doomed.id, expected_next_seq=preview["expected_next_seq"],
                                  expected_title=preview["title"])
        corrupt = store.create_conversation(title="Bozuk")
        broken = self.linked(store, corrupt.id)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE conversations SET state_json = '{}' WHERE id = ?", (corrupt.id,))
            connection.commit()
        feed_before = list(bridge.feed)
        for session_id, captured in ((session.id, None), (session.id, origin), (broken.id, None)):
            with self.subTest(session=session_id, captured=captured is not None):
                before = snapshot(self.database)
                activity = len(bridge.activity)
                self.assertEqual([], self.late(bridge, session_id, captured))       # never the open conversation
                self.assertEqual(before, snapshot(self.database))                   # ZERO writes anywhere
                self.assertEqual(activity + 1, len(bridge.activity))
                self.assertIn("was dropped; it was saved nowhere", bridge.activity[0]["text"])
        self.assertEqual(feed_before, list(bridge.feed))
        # useSession on a deleted conversation's session opens nothing.
        open_now, session_now = bridge.conversationId, bridge._session_id
        bridge.useSession(session.id)
        settled(100, lambda: (
            self.assertEqual((open_now, session_now), (bridge.conversationId, bridge._session_id)),
        ))
        # Its session leaves History's list; the corrupt one's stays.
        bridge._set_sessions({"sessions": [{"session_id": session.id}, {"session_id": broken.id}]})
        self.assertEqual([broken.id], [r["session_id"] for r in bridge.sessions])
        self.assertEqual(0, self.execute_calls())


class AttachReopenTests(WindowCase):
    def test_i_after_an_attach_the_earlier_messages_stay_on_screen_and_in_history(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        bridge = self.bridge(CHAT)
        conversation_id = bridge.conversationId
        say(store, conversation_id, "tezgah ölçüleri")
        say(store, conversation_id, "dolap rengi")
        store_state = store.load_conversation_state(conversation_id)
        bridge.previewAttach(project.id, "")
        pump(60)
        self.assertTrue(bridge.confirmAttach(project.id, ""), bridge.attachNotice)
        self.assertEqual(conversation_id, bridge.conversationId)
        self.assertEqual(["tezgah ölçüleri", "dolap rengi"], [e["text"] for e in bridge.feed if e["kind"] == "user"])
        self.assertEqual(store_state.turns, bridge._conversation_state().turns)
        seen = {}
        real = bridge.backend.submit_text

        def spy(*args, **kwargs):
            seen["history"] = [t.message for t in kwargs["conversation"].turns]
            return real(*args, **kwargs)
        bridge.backend.submit_text = spy
        bridge.submitText("üçüncü")
        settled(200, lambda: (
            self.assertEqual(["tezgah ölçüleri", "dolap rengi"], seen["history"]),
        ))


class CatalogTests(UiTestCase):
    def test_j_new_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Arşivle", tx("ConversationMenu", "Archive"))
        self.assertEqual("Kalıcı sil…", tx("ConversationMenu", "Delete permanently…"))
        self.assertEqual("Önce arşivleyin", tx("ConversationMenu", "Archive it first"))
        self.assertEqual("Yeniden adlandır", tx("ConversationMenu", "Rename"))
        self.assertEqual("ARŞİVLENMİŞ SOHBETLER", tx("HistoryPage", "ARCHIVED CONVERSATIONS"))
        self.assertEqual("Arşivden çıkar", tx("HistoryPage", "Unarchive"))
        self.assertEqual("ARŞİVLENMİŞ PROJELER", tx("ProjectsPage", "ARCHIVED PROJECTS"))
        self.assertEqual("Önce projeyi arşivden çıkarın", tx("ProjectsPage", "Unarchive the project first"))
        self.assertEqual("Salt okunur: bu sohbet arşivlendi. Devam etmek için arşivden çıkarın.",
                         tx("Composer", "Read-only: this conversation is archived. Unarchive it to continue it."))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
