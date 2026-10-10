"""V2 Projects Gate 6C "Projeye bağla": a Global conversation attached to a
project, one-way, through ONE audited explicit-user path.

A. Store: move to an existing project; a new project in the flow; the
   preview binding; every refusal writes nothing; open work and an
   unreadable linked session block; the reserved V2.1 table contract.
B. Authority: only app.py's result calls the writer; the same action's
   approval decision is identical before and after a move.
C. After the move: a fresh session binds to P and gets P's instruction and
   file excerpts; F1 still refuses Q; F2 still refuses an archived
   conversation; old tasks stay Global; no memory / approval / audit row
   changes.
D. An old Global-era session after the move: the OBSERVED behaviour.
E. Window: entry, preview lines, blocked state, "changed" refresh, confirm,
   busy guard, Turkish.
F. Rollback: schema 10, unchanged read paths.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.app import conversation_attach_preview_result, conversation_attach_result
from jarvis.contracts import ChainState, RunStatus, SessionState, TaskStatus, chain_steps_from_declaration
from jarvis.conversation import append_turn
from jarvis.memory import (
    COMPATIBILITY_SCHEMA_OBJECTS,
    CONVERSATION_GLOBAL_MEMORY_TOGGLES,
    SCHEMA_VERSION,
    SQLiteMemoryStore,
)
from jarvis.settings import Settings
from tests.helpers import make_task, safe_cleanup, settled
from tests.test_chain_cancel_cli import DECLARATION
from tests.test_intent import IntentTestCase, ScriptedModel, chat_proposal, task_proposal
from tests.test_project_files import OBJECTIVE, RELEVANT, CoreFileCase
from tests.test_projects_ui import ProjectsUiCase
from tests.test_session_owner_binding import OwnerBindingTests
from tests.test_ui import UiTestCase, _APP, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

WATCHED = ("projects", "conversations", "conversation_moves", "conversation_sessions", "sessions", "tasks",
           "pending_approvals", "event_log", "project_memory_items")


def snapshot(database: Path) -> dict[str, list]:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return {table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in WATCHED}


def say(memory: SQLiteMemoryStore, conversation_id: str, text: str) -> None:
    memory.update_conversation_state(conversation_id, lambda s: append_turn(
        s, role="user", kind="user", text=text, message=text, key=text)[0])


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.database = Path(temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.p, self.q = self.memory.create_project("P"), self.memory.create_project("Q")
        self.conversation = self.memory.create_conversation()
        say(self.memory, self.conversation.id, "ilk mesaj")
        say(self.memory, self.conversation.id, "ikinci mesaj")

    def seq(self, conversation_id: str | None = None) -> int:
        return self.memory.load_conversation_state(conversation_id or self.conversation.id).next_seq

    def attach(self, conversation_id: str | None = None, **target):
        conversation_id = conversation_id or self.conversation.id
        if "expected_next_seq" not in target:
            target["expected_next_seq"] = self.seq(conversation_id)
        return self.memory.attach_global_conversation_to_project(conversation_id, **target)

    def assertRefusedNothingWritten(self, attempt, message: str) -> None:
        before = snapshot(self.database)
        with self.assertRaises(ValueError) as refused:
            attempt()
        self.assertIn(message, str(refused.exception))
        self.assertEqual(before, snapshot(self.database))

    def linked_session(self, conversation_id: str | None = None) -> SessionState:
        session = SessionState(objective="window session")
        self.memory.save_session(session)
        self.memory.link_session_conversation(session.id, conversation_id or self.conversation.id)
        return session


class AttachStoreTests(StoreCase):
    def test_a_move_to_an_existing_active_project(self) -> None:
        state_before = self.memory.load_conversation_state(self.conversation.id)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            integrity = connection.execute("SELECT state_json, integrity FROM conversations WHERE id = ?",
                                           (self.conversation.id,)).fetchone()
        preview = self.memory.conversation_attach_preview(self.conversation.id, project_id=self.p.id)
        self.assertEqual((2, "", 3, {"project_id": self.p.id, "new": False, "name": "P"}),
                         (preview["turns"], preview["blocked"], preview["expected_next_seq"], preview["target"]))
        attached = self.attach(project_id=self.p.id, expected_next_seq=preview["expected_next_seq"])
        self.assertEqual((self.p.id, 3), (attached["target"]["project_id"], attached["boundary_seq"]))
        self.assertEqual(self.p.id, self.memory.load_conversation(self.conversation.id).project_id)
        self.assertNotIn(self.conversation.id, [c.id for c in self.memory.list_conversations(project_id=None)])
        self.assertIn(self.conversation.id, [c.id for c in self.memory.list_conversations(project_id=self.p.id)])
        self.assertEqual(state_before, self.memory.load_conversation_state(self.conversation.id))
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(integrity, connection.execute("SELECT state_json, integrity FROM conversations WHERE id = ?",
                                                           (self.conversation.id,)).fetchone())
            moves = connection.execute("SELECT conversation_id, project_id, boundary_seq FROM conversation_moves").fetchall()
        self.assertEqual([(self.conversation.id, self.p.id, 3)], moves)

    def test_a_new_project_in_the_flow_is_one_transaction(self) -> None:
        for name, message in (("   ", "non-blank"), ("p", "already exists"), ("x" * 121, "longer than")):
            with self.subTest(name=name):
                self.assertRefusedNothingWritten(lambda: self.attach(new_project_name=name), message)
        # Any other refusal after the name is fine leaves no project either.
        self.assertRefusedNothingWritten(lambda: self.attach(new_project_name="Tez", expected_next_seq=99),
                                         "preview again")
        attached = self.attach(new_project_name="  Tez  ")
        created = self.memory.load_project(attached["target"]["project_id"])
        self.assertEqual(("Tez", True), (created.name, created.active))
        self.assertTrue(created.id.startswith("p-"))
        self.assertEqual(created.id, self.memory.load_conversation(self.conversation.id).project_id)

    def test_a_the_preview_binds_the_confirm(self) -> None:
        preview = self.memory.conversation_attach_preview(self.conversation.id, project_id=self.p.id)
        say(self.memory, self.conversation.id, "önizlemeden sonra")
        self.assertRefusedNothingWritten(
            lambda: self.attach(project_id=self.p.id, expected_next_seq=preview["expected_next_seq"]),
            "changed after the preview; preview again")
        fresh = self.memory.conversation_attach_preview(self.conversation.id, project_id=self.p.id)
        self.assertEqual(preview["expected_next_seq"] + 1, fresh["expected_next_seq"])
        self.attach(project_id=self.p.id, expected_next_seq=fresh["expected_next_seq"])
        self.assertEqual(self.p.id, self.memory.load_conversation(self.conversation.id).project_id)

    def test_a_refusals_write_nothing(self) -> None:
        owned = self.memory.create_conversation(self.p.id)
        archived = self.memory.create_conversation()
        self.memory.archive_conversation(archived.id)
        old = self.memory.create_project("Old")
        self.memory.set_project_active(old.id, False)
        cases = [
            (lambda: self.attach(owned.id, project_id=self.q.id), "Only a Global conversation"),        # P -> Q
            (lambda: self.attach(archived.id, project_id=self.p.id), "archived"),
            (lambda: self.attach(project_id=old.id), "Project is archived"),
            (lambda: self.attach(project_id="p-" + "0" * 32), "Unknown project"),
            (lambda: self.attach(project_id=self.p.id, new_project_name="X"), "exactly one target"),
            (lambda: self.attach(), "exactly one target"),
            (lambda: self.attach("c-" + "0" * 32, project_id=self.p.id, expected_next_seq=1), "Unknown conversation"),
            (lambda: self.attach(project_id=self.p.id, expected_next_seq="3"), "integer"),
        ]
        for attempt, message in cases:
            with self.subTest(message=message):
                self.assertRefusedNothingWritten(attempt, message)
        # One-way, once: a repeat move and a move back are impossible.
        self.attach(project_id=self.p.id)
        for target in ({"project_id": self.q.id}, {"project_id": self.p.id}, {"new_project_name": "Back"}):
            with self.subTest(repeat=target):
                self.assertRefusedNothingWritten(lambda: self.attach(**target), "Only a Global conversation")
        for name in ("move_conversation", "set_conversation_project"):      # 6D-b's delete moves nothing
            self.assertFalse(hasattr(self.memory, name), name)

    def test_a_open_work_in_a_linked_session_blocks(self) -> None:
        # A non-terminal task.
        session = self.linked_session()
        task = make_task()
        self.memory.save_task(session.id, task)
        self.assertEqual(TaskStatus.PENDING, task.status)
        self.assertRefusedNothingWritten(lambda: self.attach(project_id=self.p.id), "task in this conversation")
        task.status = TaskStatus.COMPLETED
        self.memory.save_task(session.id, task)
        # A pending approval.
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO pending_approvals (id, task_id, session_id, request_json, status, created_at,"
                               " expires_at) VALUES ('r1', ?, ?, '{}', 'pending', 'a', 'b')", (task.id, session.id))
            connection.commit()
        self.assertRefusedNothingWritten(lambda: self.attach(project_id=self.p.id), "approval in this conversation")
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE pending_approvals SET status = 'rejected'")
            connection.commit()
        # A non-terminal chain.
        chained = self.linked_session()
        chain = ChainState.declared(chain_steps_from_declaration([dict(s) for s in DECLARATION[:2]]))
        self.assertEqual("declared", self.memory.declare_chain_if_absent(chained.id, chain))
        self.assertRefusedNothingWritten(lambda: self.attach(project_id=self.p.id), "chain in this conversation")
        # An unreadable linked session fails closed.
        other = self.memory.create_conversation()
        broken = self.linked_session(other.id)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE sessions SET state_json = 'not json' WHERE id = ?", (broken.id,))
            connection.commit()
        self.assertRefusedNothingWritten(lambda: self.attach(other.id, project_id=self.p.id), "cannot be read")
        # The preview says the same thing the writer enforces.
        self.assertIn("chain", self.memory.conversation_attach_preview(self.conversation.id, project_id=self.p.id)["blocked"])

    def test_a_the_reserved_v21_table_contract(self) -> None:
        self.assertEqual("conversation_global_memory_toggles", CONVERSATION_GLOBAL_MEMORY_TOGGLES)
        self.assertNotIn(CONVERSATION_GLOBAL_MEMORY_TOGGLES, COMPATIBILITY_SCHEMA_OBJECTS)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?",
                                                 (CONVERSATION_GLOBAL_MEMORY_TOGGLES,)).fetchone())  # 6C never creates it
            # A V2.1-shaped table with a row for this conversation: never movable.
            connection.execute(f"CREATE TABLE {CONVERSATION_GLOBAL_MEMORY_TOGGLES} (conversation_id TEXT NOT NULL, enabled_at TEXT)")
            connection.execute(f"INSERT INTO {CONVERSATION_GLOBAL_MEMORY_TOGGLES} VALUES (?, 'a')", (self.conversation.id,))
            connection.commit()
        self.assertRefusedNothingWritten(lambda: self.attach(project_id=self.p.id), "all projects memory")
        other = self.memory.create_conversation()
        self.assertEqual("", self.memory.conversation_attach_preview(other.id, project_id=self.p.id)["blocked"])
        # Present but unreadable (no conversation_id column): fail closed.
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(f"DROP TABLE {CONVERSATION_GLOBAL_MEMORY_TOGGLES}")
            connection.execute(f"CREATE TABLE {CONVERSATION_GLOBAL_MEMORY_TOGGLES} (something_else TEXT)")
            connection.commit()
        self.assertRefusedNothingWritten(lambda: self.attach(other.id, project_id=self.p.id), "cannot be read")

    def test_f_rollback_schema_10_and_unchanged_read_paths(self) -> None:
        self.assertEqual(10, SCHEMA_VERSION)
        self.assertIn("conversation_moves", COMPATIBILITY_SCHEMA_OBJECTS)
        self.attach(project_id=self.p.id)
        reopened = SQLiteMemoryStore(self.database)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual([(10,)], connection.execute("SELECT version FROM schema_version").fetchall())
        self.assertEqual(self.p.id, reopened.load_conversation(self.conversation.id).project_id)
        self.assertEqual(2, len(reopened.load_conversation_state(self.conversation.id).turns))
        self.assertEqual([self.conversation.id], [c.id for c in reopened.list_conversations(project_id=self.p.id)])


class AuthorityScanTests(unittest.TestCase):
    def test_b_the_writer_has_one_production_caller(self) -> None:
        root = Path(__file__).resolve().parents[1] / "jarvis"
        writer = "attach_global_conversation_to_project"
        mentions = sorted(str(p.relative_to(root)) for p in root.rglob("*.py") if writer in p.read_text(encoding="utf-8"))
        self.assertEqual(["app.py", "memory.py"], mentions)
        app = (root / "app.py").read_text(encoding="utf-8")
        self.assertEqual(1, app.count(f"memory.{writer}("))
        self.assertLess(app.index("def conversation_attach_result("), app.index(f"memory.{writer}("))
        # The window reaches it only through the app result.
        backend = (root / "ui" / "backend.py").read_text(encoding="utf-8")
        self.assertIn("conversation_attach_result(", backend)
        for path in root.rglob("*.py"):
            if path.name not in ("app.py", "memory.py", "backend.py"):
                self.assertNotIn("conversation_attach_result", path.read_text(encoding="utf-8"), path.name)


class MoveCoreTests(unittest.TestCase):
    setUp = OwnerBindingTests.setUp
    linked = OwnerBindingTests.linked
    task = OwnerBindingTests.task

    def move(self, conversation_id: str, project_id: str) -> None:
        preview = self.memory.conversation_attach_preview(conversation_id, project_id=project_id)
        result = conversation_attach_result(self.memory, Settings(memory_database=self.database), conversation_id,
                                            expected_next_seq=preview["expected_next_seq"], project_id=project_id)
        self.assertEqual(0, result.exit_code, result.payload)

    def old_global_conversation(self) -> tuple[SessionState, str, str]:
        old = self.linked(None)
        conversation_id = self.memory.conversation_for_session(old.id)
        run = self.core.run(self.task(None), old.id)
        self.assertEqual(RunStatus.COMPLETED, run.status, run.error)
        return old, conversation_id, run.task.id

    def test_c_after_the_move_a_fresh_session_binds_to_p(self) -> None:
        old, conversation_id, old_task = self.old_global_conversation()
        before = snapshot(self.database)
        self.move(conversation_id, self.p.id)
        after = snapshot(self.database)
        for table in ("project_memory_items", "pending_approvals", "event_log", "tasks", "sessions"):
            self.assertEqual(before[table], after[table], table)                   # nothing re-attributed
        fresh = SessionState(objective="window session")
        self.memory.save_session(fresh)
        self.memory.link_session_conversation(fresh.id, conversation_id)
        run = self.core.run(self.task(self.p.id), fresh.id)
        self.assertEqual(RunStatus.COMPLETED, run.status, run.error)
        self.assertEqual(self.p.id, self.memory.load_session(fresh.id).project_id)
        self.assertEqual("P instruction", run.task.project_instruction)
        # F1 still holds: the conversation's owner is P, so Q is refused.
        refused = self.core.run(self.task(self.q.id), fresh.id)
        self.assertEqual(RunStatus.FAILED, refused.status)
        self.assertIn("owner of the session's conversation", refused.error.message)
        # Before the move stays before the move.
        self.assertIsNone(self.memory.load_task(old_task).project_id)
        self.assertIsNone(self.memory.load_session(old.id).project_id)

    def test_d_an_old_global_era_session_after_the_move_observed(self) -> None:
        old, conversation_id, _ = self.old_global_conversation()
        self.move(conversation_id, self.p.id)
        # OBSERVED: a task naming no project runs unbound (Global-scoped: no
        # instruction), and a task naming P binds the old session to P.
        unbound = self.core.run(self.task(None), old.id)
        self.assertEqual(RunStatus.COMPLETED, unbound.status, unbound.error)
        self.assertIsNone(unbound.task.project_instruction)
        self.assertIsNone(self.memory.load_session(old.id).project_id)
        bound = self.core.run(self.task(self.p.id), old.id)
        self.assertEqual(RunStatus.COMPLETED, bound.status, bound.error)
        self.assertEqual(self.p.id, self.memory.load_session(old.id).project_id)
        self.assertEqual("P instruction", bound.task.project_instruction)


class MoveFileExcerptTests(CoreFileCase):
    def test_c_p_file_excerpts_reach_the_next_task_after_the_move(self) -> None:
        self.add("notes.md", RELEVANT)
        conversation = self.memory.create_conversation()
        self.memory.attach_global_conversation_to_project(conversation.id, expected_next_seq=1, project_id=self.p.id)
        session = SessionState(objective="window session")
        self.memory.save_session(session)
        self.memory.link_session_conversation(session.id, conversation.id)
        run = self.core.run(self.task(self.p.id, objective=OBJECTIVE), session.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, run.error)
        self.assertEqual(["notes.md"], [e["name"] for e in run.task.project_files_block["excerpts"]])


class MovedArchivedConversationTests(IntentTestCase):
    def test_c_f2_still_refuses_an_archived_moved_conversation(self) -> None:
        project = self.memory.create_project("P")
        conversation = self.memory.create_conversation()
        self.memory.attach_global_conversation_to_project(conversation.id, expected_next_seq=1, project_id=project.id)
        self.memory.archive_conversation(conversation.id)
        model = ScriptedModel(chat_proposal("never"))
        payload = self.build(model).submit_text("merhaba", conversation_id=conversation.id).payload
        self.assertNothingHappened(payload, __import__("jarvis.intent", fromlist=["IntentStatus"]).IntentStatus.INVALID_INPUT)
        self.assertIn("archived", payload["intent"]["message"])
        self.assertEqual([], model.calls)


class AttachWindowTests(ProjectsUiCase):
    def open_dialog(self, root) -> None:
        # Gate 6G F9 H1: the entry is the conversation menu's "Projeye bağla…" (header "…").
        self.steady(root, "chatHeaderMenu")
        self.click(root, "chatHeaderMenu")
        pump(300)
        self.click(root, "conversationAttachItem")
        pump(300)

    def offered(self, root) -> bool:
        """F9 H1: the open conversation's menu offers "Projeye bağla…", enabled."""
        menu = self.item(root, "conversationMenu")
        return bool(menu.property("attachShown") and menu.property("attachAllowed"))

    def choose(self, root, name: str) -> None:
        item = [i for i in self.all_items(root, "attachProject") if i.property("label") == name][0]
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtTest import QTest
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        pump(200)

    def text(self, root, name: str) -> str:
        return self.item(root, name).property("text")

    def test_e_entry_only_for_a_writable_global_conversation(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        owned = store.create_conversation(project.id)
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertTrue(bridge.canAttach)
        self.assertTrue(self.offered(root))                                         # Global only (L3; F9: the menu)
        bridge.openConversation(owned.id)
        settled(200, lambda: (
            self.assertFalse(bridge.canAttach),
            self.assertFalse(self.offered(root)),
        ))
        bridge.openProjectScope("")
        pump(200)
        archived = bridge.conversationId
        store.archive_conversation(archived)
        bridge.refreshProjects()
        # N2: the archived open conversation is left for a new, empty Global chat - writable, so attachable.
        self.assertNotEqual(archived, bridge.conversationId)
        settled(100, lambda: (
            self.assertTrue(bridge.canAttach),
            self.assertTrue(self.offered(root)),
        ))
        # The archived one, opened read-only, cannot attach.
        self.assertTrue(bridge.openConversation(archived))
        self.assertEqual((archived, True), (bridge.conversationId, bridge.conversationReadOnly))
        settled(200, lambda: (
            self.assertFalse(bridge.canAttach),
            self.assertFalse(self.offered(root)),
        ))

    def test_e_preview_changed_refresh_and_confirm(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        bridge = self.bridge()
        root = self.window(bridge)
        conversation_id = bridge.conversationId
        store.set_conversation_title(conversation_id, "Mutfak planı")
        say(store, conversation_id, "tezgah ölçüleri")
        self.open_dialog(root)
        self.choose(root, "Evim")
        identity = self.text(root, "attachIdentity")
        for part in ("Mutfak planı", "· 1 turn ·", "created"):
            self.assertIn(part, identity)
        self.assertEqual("Target: Evim", self.text(root, "attachTarget"))
        changes = self.text(root, "attachChanges")
        for part in ("owner becomes Evim, permanently", "leaves History", "instruction, file excerpts and memory",
                     "earlier messages together with Evim's context"):
            self.assertIn(part, changes)
        unchanged = self.text(root, "attachUnchanged")
        for part in ("kept as they were", "Past runs stay Global", "already sent to a provider", "No memory moves",
                     "not authority"):
            self.assertIn(part, unchanged)
        self.assertIn("never return to Global", self.text(root, "attachIrreversible"))
        self.assertTrue(self.item(root, "attachConfirm").isVisible())
        # The conversation changes after the preview: refused, refreshed, never retried.
        say(store, conversation_id, "önizlemeden sonra")
        before = snapshot(self.database)
        self.click(root, "attachConfirm")
        self.assertEqual(before, snapshot(self.database))
        self.assertIn("preview again", self.text(root, "attachNotice"))
        self.assertIn("2 turns", self.text(root, "attachIdentity"))              # the refreshed preview
        self.assertIsNone(store.load_conversation(conversation_id).project_id)
        # Confirming the fresh preview attaches it.
        self.click(root, "attachConfirm")
        self.assertEqual(project.id, store.load_conversation(conversation_id).project_id)
        self.assertEqual((conversation_id, ""), (bridge.conversationId, bridge._session_id))   # fresh session next
        self.assertEqual("Evim", self.item(root, "chatHeaderScope").property("fullName"))
        self.assertNotIn(conversation_id, [row["id"] for row in bridge.conversations])      # left History
        self.assertFalse(bridge.canAttach)
        self.assertEqual(0, self.execute_calls())

    def test_e_blocked_state_new_project_other_target_and_busy(self) -> None:
        store = self.db()
        home, thesis = store.create_project("Evim"), store.create_project("Tez")
        bridge = self.bridge()
        root = self.window(bridge)
        conversation_id = bridge.conversationId
        # Blocked: an unfinished task in a linked session -> reason, no button.
        session = SessionState(objective="window session")
        store.save_session(session)
        store.link_session_conversation(session.id, conversation_id)
        task = make_task()
        store.save_task(session.id, task)
        self.open_dialog(root)
        self.choose(root, "Evim")
        self.assertIn("not finished", self.text(root, "attachBlocked"))
        self.assertFalse(self.item(root, "attachConfirm").isVisible())
        self.assertFalse(bridge.confirmAttach(home.id, ""))
        task.status = TaskStatus.CANCELLED
        store.save_task(session.id, task)
        # A different target than previewed: refused, refreshed to the new choice, nothing written.
        bridge.previewAttach(home.id, "")
        pump(60)
        before = snapshot(self.database)
        self.assertFalse(bridge.confirmAttach(thesis.id, ""))
        self.assertEqual(before, snapshot(self.database))
        self.assertIn("choice changed", bridge.attachNotice)
        self.assertEqual(thesis.id, bridge.attachPreview["target"]["project_id"])
        # Busy guard.
        bridge._busy = True
        self.assertFalse(bridge.confirmAttach(thesis.id, ""))
        bridge._busy = False
        # A new project named in the dialog.
        self.item(root, "attachNewName").setProperty("text", "Mutfak")
        bridge.previewAttach("", "Mutfak")
        pump(60)
        self.assertEqual("Target: new project: Mutfak", self.text(root, "attachTarget"))
        self.assertTrue(bridge.confirmAttach("", "Mutfak"))
        pump(60)
        owner = store.load_conversation(conversation_id).project_id
        self.assertEqual("Mutfak", store.load_project(owner).name)

    def test_b_the_approval_decision_is_the_same_before_and_after_a_move(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        bridge = self.bridge(task_proposal("code_analysis", objective="Bu projeyi incele."),
                             task_proposal("code_analysis", objective="Bu projeyi incele."))
        decisions = []
        for moved in (False, True):
            if moved:
                bridge.previewAttach(project.id, "")
                pump(60)
                self.assertTrue(bridge.confirmAttach(project.id, ""), bridge.attachNotice)
            bridge.submitText("Bu projeyi incele.")
            settled(100, lambda: (
                self.assertTrue(bridge.approvalPending),
            ))
            ticket = bridge._ticket
            request = store.load_pending_approval(ticket.task_id, ticket.session_id)
            decisions.append((request.scope, request.risk_level, request.side_effect, request.reason))
            bridge.deny()
            pump(100)
        self.assertEqual(project.id, store.load_conversation(bridge.conversationId).project_id)
        self.assertEqual(decisions[0], decisions[1])
        self.assertEqual(0, self.execute_calls())


class AttachCatalogTests(UiTestCase):
    def test_e_new_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Projeye bağla", tx("AttachDialog", "Attach to project"))
        self.assertEqual("Projeye bağla…", tx("ConversationMenu", "Attach to a project…"))   # Gate 6G F9 H1: the menu's entry


class PreviewResultTests(StoreCase):
    def test_a_the_app_preview_result_reports_a_refusal_as_blocked(self) -> None:
        settings = Settings(memory_database=self.database)
        result = conversation_attach_preview_result(self.memory, settings, self.conversation.id, project_id=self.p.id)
        self.assertEqual(("", 3), (result.payload["preview"]["blocked"], result.payload["preview"]["expected_next_seq"]))
        bad = conversation_attach_preview_result(self.memory, settings, self.conversation.id)
        self.assertEqual("invalid_task", bad.payload["error"]["code"])
        json.dumps(result.payload)                                               # serializable as returned
