"""MEMGATEFIX GAP 1: a captured record is project-wide (conversation_id NULL),
remembers its source conversation, and the app's Hafıza page, approve and
undo and a sibling conversation all see it; project B never does.

Fake provider only; the store lives in each test's own TEMP database.
"""

import json
import unittest
from unittest import mock

from jarvis import memory_capture
from jarvis.contracts import MemoryRecordOrigin, MemoryRecordScope, MemoryRecordStatus
from jarvis.conversation import append_turn
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from tests.helpers import isolated_settings, open_app
from tests.test_memory_capture_core import FakeModel

FACT = "The build uses uv."
DECISION = "Deploy on Fridays only."


class CaptureProjectScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        # The skip policy is not under test here (test_memory_capture_guard).
        skip = mock.patch.object(memory_capture, "extraction_skip_reason", return_value=None)
        skip.start()
        self.addCleanup(skip.stop)
        self.settings = isolated_settings(self)
        self.store = SQLiteMemoryStore(self.settings.memory_database)
        self.app = open_app(destination_policy=(), settings=self.settings, memory=self.store,
                            registry=ToolRegistry())
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id
        self.cid = self.store.create_conversation(self.alpha).id
        ids = []

        def say(state):
            for index, text in enumerate((DECISION, FACT)):
                state, turn = append_turn(state, role="user", kind="user", text=text, message=text, key=f"k{index}")
                ids.append(turn.id)
            return state

        self.store.update_conversation_state(self.cid, say)
        model = FakeModel(json.dumps({"candidates": [
            {"kind": "fact", "text": FACT, "subject_key": "tooling", "confidence": 0.9,
             "evidence_turn_ids": [ids[1]]},
            {"kind": "decision", "text": DECISION, "subject_key": "deploy-day", "confidence": 0.9,
             "evidence_turn_ids": [ids[0]]},
        ]}))
        result = self.app.core.capture_memory(self.cid, MemoryRecordOrigin.CAPTURE_SESSION_END, model)
        self.assertEqual(("ok", 1, 1), (result.outcome, result.approved, result.pending))

    def listed(self, project_id):
        payload = self.app.memory_records_payload(project_id)
        self.assertTrue(payload["ok"], payload)
        return {record["text"]: record for record in payload["records"]}

    def test_records_are_project_wide_and_keep_their_source(self) -> None:
        views = self.store.list_records(MemoryRecordScope(self.alpha))
        self.assertEqual({FACT, DECISION}, {view.record.text for view in views})
        for view in views:
            self.assertIsNone(view.record.scope.conversation_id)
            self.assertEqual(self.cid, view.record.source_conversation_id)

    def test_hafiza_page_lists_and_approve_undo_work_pending_until_approved(self) -> None:
        listed = self.listed(self.alpha)
        self.assertEqual(("approved", "pending"), (listed[FACT]["status"], listed[DECISION]["status"]))
        sibling = MemoryRecordScope(self.alpha, self.store.create_conversation(self.alpha).id)
        approved_only = (MemoryRecordStatus.APPROVED,)
        self.assertEqual({FACT}, {v.record.text for v in self.store.list_records(sibling, approved_only)})
        approved = self.app.approve_memory_record(self.alpha, listed[DECISION]["record_id"])
        self.assertTrue(approved["ok"], approved)
        self.assertEqual({FACT, DECISION}, {v.record.text for v in self.store.list_records(sibling, approved_only)})
        undone = self.app.undo_memory_record(self.alpha, listed[FACT]["record_id"])
        self.assertEqual((True, "retracted"), (undone["ok"], undone["status"]), undone)

    def test_project_b_never_sees_or_touches_them(self) -> None:
        self.assertEqual({}, self.listed(self.beta))
        record_id = self.listed(self.alpha)[DECISION]["record_id"]
        self.assertFalse(self.app.approve_memory_record(self.beta, record_id)["ok"])
        self.assertFalse(self.app.undo_memory_record(self.beta, record_id)["ok"])
        self.assertEqual("pending", self.listed(self.alpha)[DECISION]["status"])
        other = MemoryRecordScope(self.beta, self.store.create_conversation(self.beta).id)
        self.assertEqual([], self.store.list_records(other))


if __name__ == "__main__":
    unittest.main()
