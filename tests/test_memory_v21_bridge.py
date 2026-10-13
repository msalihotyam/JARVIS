"""V2.1a M4 bridge slots: memory records, approve/reject/undo and 'Hatırla'.
Each slot runs the real app method over a throwaway TEMP database and emits
its answer; a refusal reaches the window as a visible error, never silence.
No model or connector is called."""

from __future__ import annotations

import unittest
from unittest import mock

from jarvis.conversation import append_turn
from tests.test_ui import UiTestCase


class MemoryBridgeTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ui = self.bridge()
        self.app = self.factory(self.store.load().settings)   # the same TEMP database, for setup and checks
        self.memory = self.app.memory
        self.project = self.memory.create_project("Alpha").id
        self.other = self.memory.create_project("Beta").id
        self.conversation = self.memory.create_conversation(self.project).id
        self.records: list[dict] = []
        self.actions: list[dict] = []
        self.ui.memoryRecordsDone.connect(self.records.append)
        self.ui.memoryRecordActionDone.connect(self.actions.append)

    def turn(self, text: str, conversation_id: str | None = None) -> str:
        created = []

        def change(state):
            state, turn = append_turn(state, role="user", kind="user", text=text, message=text)
            created.append(turn)
            return state

        self.memory.update_conversation_state(conversation_id or self.conversation, change)
        return created[0].id

    def remembered(self, text: str, kind: str = "fact", conversation_id: str | None = None) -> str:
        conversation_id = conversation_id or self.conversation
        return self.app.remember_message(conversation_id, self.turn(text, conversation_id), kind)["record"]["record_id"]

    def assertRefusedVisibly(self, answer: dict, feed_before: int) -> None:
        self.assertIs(answer["ok"], False)
        self.assertGreater(len(self.ui.feed), feed_before)   # this call said so, not an earlier one
        self.assertIsInstance(answer["error"], str)
        self.assertTrue(answer["error"])
        self.assertEqual(self.ui.feed[-1]["kind"], "error")

    # -- records -----------------------------------------------------------------

    def test_records_slot_lists_through_the_app_and_passes_the_filter(self) -> None:
        fact = self.remembered("Deploys run on Fridays")
        decision = self.remembered("We ship weekly", kind="decision")
        self.ui.memoryRecords(self.project, {})
        self.ui.memoryRecords(self.project, {"statuses": ["pending"]})
        everything, pending = self.records
        self.assertTrue(everything["ok"])
        self.assertEqual(everything["project_id"], self.project)
        self.assertEqual({r["record_id"] for r in everything["records"]}, {fact, decision})
        self.assertEqual([r["record_id"] for r in pending["records"]], [decision])
        self.assertEqual(pending["records"][0]["text"], "We ship weekly")

    def test_records_refusal_is_a_visible_error(self) -> None:
        for project_id, filters in (("no-such-project", {}), (self.project, {"statuses": ["bogus"]})):
            with self.subTest(project_id=project_id, filters=filters):
                before = len(self.ui.feed)
                self.ui.memoryRecords(project_id, filters)
                self.assertRefusedVisibly(self.records[-1], before)
                self.assertEqual(self.records[-1]["records"], [])

    # -- approve / reject / undo -------------------------------------------------

    def test_approve_reject_undo_call_the_app_and_emit(self) -> None:
        pending = self.remembered("We ship weekly", kind="decision")
        other = self.remembered("We release on Mondays", kind="decision")
        fact = self.remembered("Deploys run on Fridays")
        self.ui.approveMemoryRecord(self.project, pending)
        self.ui.rejectMemoryRecord(self.project, other)
        self.ui.undoMemoryRecord(self.project, fact)
        self.assertEqual(
            [(a["ok"], a["action"], a["record_id"], a["status"], a["project_id"]) for a in self.actions],
            [(True, "approve", pending, "approved", self.project), (True, "reject", other, "rejected", self.project),
             (True, "undo", fact, "retracted", self.project)],
        )
        self.assertEqual([self.memory.current_status(r).value for r in (pending, other, fact)],
                         ["approved", "rejected", "retracted"])

    def test_action_refusals_propagate_and_change_nothing(self) -> None:
        beta = self.memory.create_conversation(self.other).id
        beta_decision = self.remembered("Beta plan", kind="decision", conversation_id=beta)
        for slot, record_id in ((self.ui.approveMemoryRecord, beta_decision),
                                (self.ui.rejectMemoryRecord, beta_decision),
                                (self.ui.undoMemoryRecord, "no-such-record")):
            with self.subTest(slot=slot.__name__, record_id=record_id):
                before = len(self.ui.feed)
                slot(self.project, record_id)
                self.assertRefusedVisibly(self.actions[-1], before)
        self.assertEqual(self.memory.current_status(beta_decision).value, "pending")

    def test_a_worker_failure_is_a_visible_error(self) -> None:
        before = len(self.ui.feed)
        with mock.patch.object(self.ui.backend, "undo_memory_record", side_effect=RuntimeError("boom")):
            self.ui.undoMemoryRecord(self.project, "any")
        self.assertRefusedVisibly(self.actions[-1], before)
        self.assertIn("connector_failed", self.actions[-1]["error"])

    # -- Hatırla -----------------------------------------------------------------

    def test_remember_slot_stores_the_message_through_the_app(self) -> None:
        message_id = self.turn("Deploys run on Fridays")
        self.ui.rememberMessage(self.conversation, message_id)
        answer = self.actions[-1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["action"], answer["conversation_id"], answer["message_id"]),
                         ("remember", self.conversation, message_id))
        self.assertEqual((answer["record"]["text"], answer["record"]["origin"]),
                         ("Deploys run on Fridays", "explicit_user"))
        listed = self.app.memory_records_payload(self.project)["records"]
        self.assertEqual([r["record_id"] for r in listed], [answer["record"]["record_id"]])

    def test_remember_refusal_is_visible_and_does_not_echo_the_text(self) -> None:
        message_id = self.turn("my password is hunter2")
        before = len(self.ui.feed)
        self.ui.rememberMessage(self.conversation, message_id)
        self.assertRefusedVisibly(self.actions[-1], before)
        self.assertNotIn("hunter2", repr(self.actions[-1]))
        self.assertNotIn("hunter2", repr(self.ui.feed[-1]))
        before = len(self.ui.feed)
        self.ui.rememberMessage(self.conversation, "no-such-message")
        self.assertRefusedVisibly(self.actions[-1], before)
        self.assertEqual(self.app.memory_records_payload(self.project)["records"], [])


if __name__ == "__main__":
    unittest.main()
