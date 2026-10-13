"""V2.1a M4 app API: record payloads, user approvals, 'Hatırla', the global
toggle and context usage. Every database is a throwaway in TEMP; no model is
called (the only "model" is a bare stub object)."""

from __future__ import annotations

import unittest
from unittest import mock

from jarvis.conversation import append_turn
from jarvis.memory import MemoryRecordApproval
from tests.helpers import isolated_settings, open_app


class MemoryAppApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = open_app(destination_policy=(), settings=isolated_settings(self))
        self.memory = self.app.memory
        self.project = self.memory.create_project("Alpha").id
        self.other = self.memory.create_project("Beta").id
        self.conversation = self.memory.create_conversation(self.project).id

    def turn(self, conversation_id: str, role: str, kind: str, text: str) -> str:
        created = []

        def change(state):
            state, turn = append_turn(state, role=role, kind=kind, text=text, message=text if role == "user" else "")
            created.append(turn)
            return state

        self.memory.update_conversation_state(conversation_id, change)
        return created[0].id

    def remember(self, text: str, kind: str = "fact", conversation_id: str | None = None) -> dict:
        conversation_id = conversation_id or self.conversation
        return self.app.remember_message(conversation_id, self.turn(conversation_id, "user", "user", text), kind)

    def status(self, record_id: str) -> str:
        return self.memory.current_status(record_id).value

    # -- Hatırla ---------------------------------------------------------------

    def test_remember_writes_an_approved_explicit_user_fact(self) -> None:
        result = self.remember("Deploys run on Fridays")
        self.assertTrue(result["ok"])
        record = result["record"]
        self.assertEqual(
            (record["kind"], record["status"], record["origin"], record["actor"], record["project_id"]),
            ("fact", "approved", "explicit_user", "user", self.project),
        )
        self.assertEqual(record["text"], "Deploys run on Fridays")

    def test_remember_an_assistant_reply(self) -> None:
        message_id = self.turn(self.conversation, "assistant", "reply", "The build uses Python 3.13")
        result = self.app.remember_message(self.conversation, message_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["record"]["text"], "The build uses Python 3.13")

    def test_remember_a_decision_stays_pending_until_the_user_approves(self) -> None:
        result = self.remember("We ship weekly", kind="decision")
        record_id = result["record"]["record_id"]
        self.assertEqual(result["record"]["status"], "pending")
        approved = self.app.approve_memory_record(self.project, record_id)
        self.assertEqual(approved, {"ok": True, "record_id": record_id, "status": "approved"})

    def test_remember_refuses_filtered_text_without_echoing_it(self) -> None:
        result = self.remember("my password is hunter2")
        self.assertFalse(result["ok"])
        self.assertIn("refused", result)
        self.assertNotIn("hunter2", repr(result))
        self.assertEqual(self.app.memory_records_payload(self.project)["records"], [])

    def test_remember_derives_the_subject_key_from_redacted_text(self) -> None:
        result = self.remember(r"C:\Users\someone\Documents\salary.xlsx is the budget")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("salary", result["record"]["subject_key"])
        self.assertNotIn("someone", result["record"]["subject_key"])

    def test_remember_refuses_tool_results_unknown_ids_and_global_conversations(self) -> None:
        result_id = self.turn(self.conversation, "assistant", "result", "connector output")
        self.assertFalse(self.app.remember_message(self.conversation, result_id)["ok"])
        self.assertFalse(self.app.remember_message(self.conversation, "t999-missing")["ok"])
        self.assertFalse(self.app.remember_message("no-such-conversation", "t0")["ok"])
        global_conversation = self.memory.create_conversation(None).id
        message_id = self.turn(global_conversation, "user", "user", "Deploys run on Fridays")
        self.assertFalse(self.app.remember_message(global_conversation, message_id)["ok"])
        self.assertFalse(self.remember("Bad kind", kind="opinion")["ok"])

    # -- payloads --------------------------------------------------------------

    def test_records_payload_filters_and_stays_in_its_project(self) -> None:
        fact = self.remember("Deploys run on Fridays")["record"]["record_id"]
        decision = self.remember("We ship weekly", kind="decision")["record"]["record_id"]
        beta = self.memory.create_conversation(self.other).id
        self.remember("Beta secret plan", conversation_id=beta)

        ids = lambda payload: {record["record_id"] for record in payload["records"]}  # noqa: E731
        self.assertEqual(ids(self.app.memory_records_payload(self.project)), {fact, decision})
        self.assertEqual(ids(self.app.memory_records_payload(self.project, {"statuses": ["pending"]})), {decision})
        self.assertEqual(ids(self.app.memory_records_payload(self.project, {"kinds": ["fact"]})), {fact})
        self.assertEqual(ids(self.app.memory_records_payload(self.project, {"query": "Fridays"})), {fact})
        self.assertEqual(ids(self.app.memory_records_payload(self.project, {"query": "Beta"})), set())

    def test_records_payload_refuses_bad_input(self) -> None:
        for project_id, filters in (
            ("no-such-project", None),
            (self.project, {"statuses": ["bogus"]}),
            (self.project, {"kinds": ["opinion"]}),
            (self.project, {"limit": 0}),
            (self.project, {"query": 7}),
            (self.project, ["not", "a", "mapping"]),
        ):
            with self.subTest(project_id=project_id, filters=filters):
                self.assertFalse(self.app.memory_records_payload(project_id, filters)["ok"])

    # -- approve / reject / undo -------------------------------------------------

    def test_approve_builds_the_explicit_user_proof_at_the_boundary(self) -> None:
        record_id = self.remember("We ship weekly", kind="decision")["record"]["record_id"]
        with mock.patch.object(self.memory, "approve_record", wraps=self.memory.approve_record) as approve:
            self.assertTrue(self.app.approve_memory_record(self.project, record_id)["ok"])
        (called_id, proof), _ = approve.call_args
        self.assertEqual(called_id, record_id)
        self.assertIs(type(proof), MemoryRecordApproval)
        self.assertEqual((proof.project_id, proof.record_id), (self.project, record_id))
        self.assertEqual(self.status(record_id), "approved")

    def test_approve_without_a_real_proof_refuses(self) -> None:
        record_id = self.remember("We ship weekly", kind="decision")["record"]["record_id"]
        with mock.patch("jarvis.app.MemoryRecordApproval", lambda **fields: dict(fields)):
            self.assertFalse(self.app.approve_memory_record(self.project, record_id)["ok"])
        self.assertEqual(self.status(record_id), "pending")

    def test_reject_and_undo(self) -> None:
        decision = self.remember("We ship weekly", kind="decision")["record"]["record_id"]
        self.assertEqual(self.app.reject_memory_record(self.project, decision)["status"], "rejected")
        self.assertFalse(self.app.approve_memory_record(self.project, decision)["ok"])
        fact = self.remember("Deploys run on Fridays")["record"]["record_id"]
        self.assertEqual(self.app.undo_memory_record(self.project, fact)["status"], "retracted")
        self.assertFalse(self.app.undo_memory_record(self.project, fact)["ok"])

    def test_actions_refuse_unknown_and_other_project_records(self) -> None:
        beta = self.memory.create_conversation(self.other).id
        beta_decision = self.remember("Beta plan", kind="decision", conversation_id=beta)["record"]["record_id"]
        beta_fact = self.remember("Beta fact", conversation_id=beta)["record"]["record_id"]
        for action, record_id in (
            (self.app.approve_memory_record, beta_decision),
            (self.app.reject_memory_record, beta_decision),
            (self.app.undo_memory_record, beta_fact),
            (self.app.approve_memory_record, "no-such-record"),
            (self.app.reject_memory_record, "no-such-record"),
            (self.app.undo_memory_record, "no-such-record"),
        ):
            with self.subTest(action=action.__name__, record_id=record_id):
                self.assertFalse(action(self.project, record_id)["ok"])
        self.assertEqual((self.status(beta_decision), self.status(beta_fact)), ("pending", "approved"))
        self.assertFalse(self.app.approve_memory_record("no-such-project", beta_decision)["ok"])

    # -- global toggle and context usage ---------------------------------------

    def test_global_toggle_round_trip_and_refusals(self) -> None:
        self.assertEqual(self.app.global_memory_toggle_payload(self.conversation)["enabled"], False)
        self.assertTrue(self.app.set_global_memory_toggle(self.conversation, True)["enabled"])
        self.assertTrue(self.app.global_memory_toggle_payload(self.conversation)["enabled"])
        self.assertFalse(self.app.set_global_memory_toggle(self.conversation, False)["enabled"])
        self.assertFalse(self.app.set_global_memory_toggle(self.conversation, "yes")["ok"])
        self.assertFalse(self.app.set_global_memory_toggle("no-such-conversation", True)["ok"])

    def test_context_usage_is_honest_without_a_model(self) -> None:
        self.turn(self.conversation, "user", "user", "Hello there")
        usage = self.app.context_usage_payload(self.conversation)
        self.assertTrue(usage["ok"])
        self.assertEqual(usage["label_key"], "bilinmiyor")
        self.app.intent_model = object()  # a stub: no model is ever called
        usage = self.app.context_usage_payload(self.conversation)
        self.assertEqual(usage["label_key"], "normal")
        self.assertGreater(usage["max_tokens"], usage["used_tokens"])
        self.assertFalse(self.app.context_usage_payload("no-such-conversation")["ok"])


if __name__ == "__main__":
    unittest.main()
