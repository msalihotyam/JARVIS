"""V2.1a M3b: Core's public typed-memory API, keyed by the conversation.

``AgentCore.typed_memory_for`` resolves the scope itself: the conversation's
global toggle counts only for a stored conversation of that project. The
store lives in each test's own TEMP database (never data\\jarvis.db).
"""

import sqlite3
import unittest
from unittest import mock

from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    RetrievedContext,
    RetrievedMemoryKind,
    SessionState,
)
from jarvis.memory import MemoryRecordApproval, SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from tests.helpers import isolated_settings, make_task, open_core

READ_ONLY = "read-only from project Beta (all projects memory)"


class TypedMemoryForTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = isolated_settings(self)
        self.store = SQLiteMemoryStore(settings.memory_database)
        self.core = open_core(ToolRegistry(), self.store, settings, destination_policy=frozenset())
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id
        self.conversation = self.store.create_conversation(self.alpha).id
        self.own = self.record(self.alpha, MemoryRecordKind.FACT, "deploy uses uv")
        self.other = self.record(self.beta, MemoryRecordKind.FACT, "deploy needs a VPN")

    def record(self, project, kind, text):
        view = self.store.insert_record(
            MemoryRecord(
                kind=kind,
                text=text,
                subject_key=text.split()[0] + "-" + kind.value,
                scope=MemoryRecordScope(project),
                origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
                actor=MemoryRecordActor.MODEL,
                confidence=0.8,
                classification=DataClassification.INTERNAL,
            )
        )
        record_id = view.record.record_id
        self.store.approve_record(record_id, MemoryRecordApproval(project, record_id))
        return record_id

    def toggle(self, conversation):
        self.store.set_global_memory_toggle(conversation, True, MemoryRecordActor.USER)

    def delivered(self, project_id, conversation_id):
        task = make_task()
        task.objective = "deploy checklist"
        return {
            i.record_id: i
            for i in self.core.typed_memory_for(project_id, conversation_id, task).items
        }

    def test_unlinked_project_conversation_honours_its_own_toggle(self) -> None:
        # No window session is linked to this conversation: Core reads the
        # toggle from the conversation id itself.
        self.assertEqual({self.own}, set(self.delivered(self.alpha, self.conversation)))
        own_rule = self.record(self.alpha, MemoryRecordKind.RULE, "deploy needs two reviewers")
        other_rule = self.record(self.beta, MemoryRecordKind.RULE, "deploy never on Mondays")
        self.toggle(self.conversation)
        items = self.delivered(self.alpha, self.conversation)
        self.assertEqual({self.own, self.other, own_rule, other_rule}, set(items))
        self.assertIn(READ_ONLY, items[self.other].rationale)
        self.assertNotIn("read-only", items[self.own].rationale)
        # RULE stays Core's re-proof: another project's rule is data only.
        self.assertEqual(RetrievedMemoryKind.RULE, items[own_rule].kind)
        self.assertNotEqual(RetrievedMemoryKind.RULE, items[other_rule].kind)
        self.assertIn(READ_ONLY, items[other_rule].rationale)

    def test_unknown_foreign_or_malformed_conversation_fails_closed(self) -> None:
        foreign = self.store.create_conversation(self.beta).id
        sibling = self.store.create_conversation(self.alpha).id
        self.toggle(self.conversation)
        self.toggle(foreign)
        for conversation_id in ("conversation-unknown", foreign, sibling, "", None, 42, b"x", ["x"]):
            with self.subTest(conversation_id=conversation_id):
                self.assertEqual({self.own}, set(self.delivered(self.alpha, conversation_id)))

    def test_project_less_or_malformed_project_gets_nothing(self) -> None:
        self.toggle(self.conversation)
        for project_id in (None, "", 42, ["x"]):
            with self.subTest(project_id=project_id):
                self.assertEqual({}, self.delivered(project_id, self.conversation))

    def test_failed_conversation_lookup_delivers_nothing(self) -> None:
        # B4: the memory-off (I21) and SESSION checks need the conversation;
        # a lookup that raises must not fall back to project-wide delivery.
        self.store.set_conversation_memory_off(self.conversation, True, MemoryRecordActor.USER)
        with mock.patch.object(self.store, "load_conversation", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual({}, self.delivered(self.alpha, self.conversation))
        self.assertEqual({}, self.delivered(self.alpha, self.conversation))  # memory off
        self.assertEqual({self.own}, set(self.delivered(self.alpha, None)))

    def test_linked_session_scope_matches_the_run_path(self) -> None:
        self.toggle(self.conversation)
        session = SessionState(objective="window session", project_id=self.alpha)
        self.store.save_session(session)
        self.store.link_session_conversation(session.id, self.conversation)
        task = make_task()
        task.objective = "deploy checklist"
        public = self.core.typed_memory_for(self.alpha, self.conversation, task)
        self.assertEqual({self.own, self.other}, {i.record_id for i in public.items})
        self.assertEqual(self.core._with_typed_memory(session, task, RetrievedContext()), public)


if __name__ == "__main__":
    unittest.main()
