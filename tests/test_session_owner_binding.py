"""A conversation-linked session binds only to its conversation's owner.

Independent Codex review finding (F1): Core compared a task's project only
with the session's own binding, never with the session's persistent
conversation link, so a non-chat entry could bind a session linked to a
Global (or other-owner) conversation to project P. And (F2) a chat request
into an archived conversation reached the model.

A. Global-linked session + task naming P: refused, unbound, nothing runs.
B. P-linked session + task naming Q: refused.
C. P-linked session + task naming P or no project: runs as today.
D. No link (CLI / scheduler sessions): unchanged.
E. Chat into an archived conversation: refused before the model call.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.contracts import DataClassification, ErrorCode, ProjectMemoryItem, ProjectMemoryKind, RunStatus, SessionState
from jarvis.destinations import CLAUDE_CODE_DESTINATION, DestinationGrant
from jarvis.intent import IntentStatus
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, make_task, open_core, safe_cleanup
from tests.test_intent import IntentTestCase, ScriptedModel, chat_proposal


class OwnerBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.database = Path(temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.connector = DummyConnector()
        registry = ToolRegistry()
        registry.register(self.connector)
        self.core = open_core(
            registry, self.memory, Settings(memory_database=self.database),
            destination_policy=frozenset({DestinationGrant(self.connector.manifest.name, DUMMY_CAPABILITY,
                                                           CLAUDE_CODE_DESTINATION)}),
        )
        self.p = self.memory.create_project("P")
        self.q = self.memory.create_project("Q")
        self.memory.save_memory_item(ProjectMemoryItem(project_id=self.p.id, kind=ProjectMemoryKind.FACT,
                                                       text="echo this fact of P", classification=DataClassification.INTERNAL))
        self.memory.save_explicit_user_project_instruction(self.p.id, "P instruction", "internal")

    def linked(self, owner: str | None) -> SessionState:
        session = SessionState(objective="window session")
        self.memory.save_session(session)
        self.memory.link_session_conversation(session.id, self.memory.create_conversation(owner).id)
        return session

    def task(self, project_id: str | None):
        task = make_task()
        task.project_id = project_id
        return task

    def tasks(self) -> int:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def assertRefused(self, session: SessionState, project_id: str) -> None:
        run = self.core.run(self.task(project_id), session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("owner of the session's conversation", run.error.message)
        self.assertEqual(session.project_id, self.memory.load_session(session.id).project_id)
        self.assertEqual((0, 0), (self.tasks(), self.connector.execute_calls))

    def test_a_global_linked_session_never_binds_a_project(self) -> None:
        session = self.linked(None)
        self.assertRefused(session, self.p.id)
        self.assertIsNone(self.memory.load_session(session.id).project_id)
        # A task naming no project still runs, Global: no project memory,
        # instruction or files reach it.
        run = self.core.run(self.task(None), session.id)
        self.assertEqual(RunStatus.COMPLETED, run.status, run.error)
        self.assertIsNone(self.memory.load_session(session.id).project_id)
        stored = self.memory.load_task(run.task.id)
        self.assertFalse(any("fact of P" in item.summary for item in stored.retrieved_context.items))
        self.assertIsNone(run.task.project_instruction)

    def test_b_p_linked_session_refuses_another_project(self) -> None:
        self.assertRefused(self.linked(self.p.id), self.q.id)

    def test_c_p_linked_session_runs_p_or_no_project_as_today(self) -> None:
        session = self.linked(self.p.id)
        first = self.core.run(self.task(self.p.id), session.id)
        self.assertEqual(RunStatus.COMPLETED, first.status, first.error)
        self.assertEqual(self.p.id, self.memory.load_session(session.id).project_id)
        self.assertEqual("P instruction", first.task.project_instruction)
        second = self.core.run(self.task(None), session.id)
        self.assertEqual(RunStatus.COMPLETED, second.status, second.error)
        self.assertEqual(2, self.connector.execute_calls)

    def test_d_an_unlinked_session_is_unchanged(self) -> None:
        run = self.core.run(self.task(self.p.id))                      # a fresh CLI-style session
        self.assertEqual(RunStatus.COMPLETED, run.status, run.error)
        self.assertEqual(self.p.id, self.memory.load_session(run.session.id).project_id)

    def test_an_unreadable_link_or_conversation_fails_closed(self) -> None:
        session = self.linked(self.p.id)
        with mock.patch.object(self.memory, "conversation_for_session", side_effect=RuntimeError("broken")):
            self.assertRefused(session, self.p.id)
        with mock.patch.object(self.memory, "load_conversation", return_value=None):
            self.assertRefused(session, self.p.id)


class ArchivedConversationTests(IntentTestCase):
    def test_e_chat_into_an_archived_conversation_is_refused_before_the_model(self) -> None:
        conversation = self.memory.create_conversation()
        self.memory.archive_conversation(conversation.id)
        model = ScriptedModel(chat_proposal("never"))
        payload = self.build(model).submit_text("merhaba", conversation_id=conversation.id).payload
        self.assertNothingHappened(payload, IntentStatus.INVALID_INPUT)
        self.assertIn("archived", payload["intent"]["message"])
        self.assertEqual([], model.calls)
