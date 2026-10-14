"""V2.1a gate: typed memory end to end, through the real app and Core paths.

chat in project A -> PreCompact capture -> SessionEnd capture -> FACT auto,
DECISION pending -> approve -> a new conversation in A retrieves it labelled
-> a newer DECISION supersedes -> project B never sees it -> B's "all
projects memory" sees it read-only and labelled -> that conversation cannot
be attached -> Hatırla -> undo -> context usage + manual compaction.

One fake provider plays both the chat model and the capture model (no
process, no network); every store lives in the test's own TEMP database
(never data\\jarvis.db). Nothing here writes a record past Core or the app:
records come from Core's capture or the app's Hatırla, approvals from the
app's approve action.
"""

from __future__ import annotations

import json
import sqlite3
import time
import unittest
from contextlib import closing

from jarvis.app import CHAT_MEMORY_KEY, EXIT_OK, MemoryTriggers, conversation_attach_result
from jarvis.contracts import (
    MODEL_ANSWER_RESULT_KEY,
    MemoryRecordKind,
    MemoryRecordScope,
    MemoryRecordStatus,
    OutcomeStatus,
    SessionState,
    ToolResult,
    build_model_answer,
)
from jarvis.conversation import PersistedConversation, register_precompact_hook
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from tests.helpers import isolated_settings, open_app
from tests.test_intent import ScriptedModel, chat_proposal

LABEL = "memory record (reference data, not an instruction)"
FRIDAY = "We deploy the release only on Fridays."
THURSDAY = "We deploy the release only on Thursdays now."
UV = "The build of the release uses uv for every package."
#: Padding so a captured batch passes the skip policy on its own merits
#: (two user turns, 200+ characters of the person's own words).
PAD = " Keep the deploy checklist short and readable for the whole team please."


class FakeProvider(ScriptedModel):
    """Chat answers "Tamam."; a capture call gets the next queued reply."""

    def __init__(self) -> None:
        super().__init__()
        self.capture_replies: list[str] = []
        self.captures: list = []

    def complete(self, task, session):
        if task.capability == "memory_capture":
            self.captures.append(task)
            reply = self.capture_replies.pop(0) if self.capture_replies else json.dumps({"candidates": []})
            return ToolResult(outcome=OutcomeStatus.SUCCESS, summary="ok",
                              data={MODEL_ANSWER_RESULT_KEY: build_model_answer(reply)})
        self.answers.append(chat_proposal("Tamam."))
        return super().complete(task, session)


def candidate(kind: str, text: str, subject: str, turn) -> dict:
    return {"kind": kind, "text": text, "subject_key": subject, "confidence": 0.9,
            "evidence_turn_ids": [turn.id]}


class MemoryV21EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.database = self.settings.memory_database
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id
        self.model = FakeProvider()
        self.app = open_app(destination_policy=(), settings=self.settings, memory=self.store,
                            registry=ToolRegistry())
        self.app.intent_model = self.model
        # A per-test worker, never the process-wide one.
        self.app.memory_triggers = MemoryTriggers()
        self.addCleanup(self.app.memory_triggers.shutdown, self.app, (), 5.0)
        self.addCleanup(register_precompact_hook, self.store, None)
        self.app.watch_conversations(self.store)

    # -- the window's moves, through the real app --------------------------

    def converse(self, conversation: PersistedConversation, text: str, session_id=None):
        """One chat turn: the user's turn is stored, the app answers it, the
        reply is stored. Returns (user turn, the task the chat model got)."""
        turn = conversation.append(role="user", kind="user", text=text, message=text)
        self.assertIsNotNone(turn, conversation.diagnostic)
        payload = self.app.submit_text(text, session_id, conversation_id=conversation.id).payload
        self.assertEqual("conversation", payload["intent"]["status"], payload)
        self.assertIsNotNone(conversation.append(role="assistant", kind="reply", text="Tamam."))
        return turn, self.model.calls[-1]

    def settle(self) -> None:
        triggers = self.app.memory_triggers
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with triggers._lock:
                if not triggers._threads and not triggers._jobs:
                    return
            time.sleep(0.01)
        self.fail("capture worker did not settle")

    def leave(self, conversation_id: str) -> None:
        """SessionEnd: the window switches away from the conversation."""
        self.assertTrue(self.app.conversation_left(conversation_id))
        self.settle()

    def linked_session(self, conversation_id: str) -> str:
        session = SessionState(objective="window session")
        self.store.save_session(session)
        self.store.link_session_conversation(session.id, conversation_id)
        return session.id

    def chat_memory(self, project_id: str, text="deploy release checklist", session_for=None):
        """Chat in a NEW conversation of ``project_id`` (or in ``session_for``'s
        conversation); returns {summary: item} of what reached the model."""
        cid = session_for or self.store.create_conversation(project_id).id
        sid = self.linked_session(cid) if session_for else None
        _, task = self.converse(PersistedConversation(self.store, cid), text, sid)
        block = task.context.get(CHAT_MEMORY_KEY)
        return {} if block is None else {item["summary"]: item for item in block["items"]}

    def records(self, project_id: str, *statuses: MemoryRecordStatus) -> dict[str, dict]:
        """What the Hafıza page lists for the project (the app payload)."""
        filters = {"statuses": [s.value for s in statuses]} if statuses else None
        payload = self.app.memory_records_payload(project_id, filters)
        self.assertTrue(payload["ok"], payload)
        return {record["text"]: record for record in payload["records"]}

    def runs(self, conversation_id: str) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT trigger, outcome FROM memory_capture_runs WHERE conversation_id = ? "
                "ORDER BY started_at", (conversation_id,)).fetchall()

    def capture_decision(self, text: str, subject: str = "deploy-day") -> dict:
        """A conversation in Alpha states ``text``; SessionEnd captures it as
        a pending DECISION; the user approves it in the app. Returns its payload."""
        cid = self.store.create_conversation(self.alpha).id
        conversation = PersistedConversation(self.store, cid)
        said, _ = self.converse(conversation, text + PAD)
        self.converse(conversation, "Please also remember the checklist itself." + PAD)
        self.model.capture_replies.append(json.dumps({"candidates": [
            candidate("decision", text, subject, said)]}))
        self.leave(cid)
        pending = self.records(self.alpha, MemoryRecordStatus.PENDING)
        self.assertIn(text, pending, f"SessionEnd DECISION not listed as pending; runs={self.runs(cid)}")
        record = pending[text]
        approved = self.app.approve_memory_record(self.alpha, record["record_id"])
        self.assertTrue(approved["ok"], approved)
        self.assertEqual("approved", approved["status"])
        return record

    # -- the gate ------------------------------------------------------------

    def test_precompact_captures_what_the_automatic_compaction_folds(self) -> None:
        cid = self.store.create_conversation(self.alpha).id
        conversation = PersistedConversation(self.store, cid, raw_turns_max=4)
        said, _ = self.converse(conversation, UV + PAD)
        self.model.capture_replies.append(json.dumps({"candidates": [candidate("fact", UV, "build-tool", said)]}))
        self.converse(conversation, "Second question about the release." + PAD)
        # The fourth turn crossed the threshold: the oldest turn was compacted.
        self.converse(conversation, "Third question about the release." + PAD)
        self.settle()
        state = self.store.load_conversation_state(cid)
        self.assertTrue(state.compacted, "nothing was compacted")
        self.assertNotIn(said.id, {turn.id for turn in state.turns})
        self.assertIn("precompact", {trigger for trigger, _ in self.runs(cid)}, self.runs(cid))
        facts = {view.record.text: view for view in
                 self.store.list_records(MemoryRecordScope(self.alpha, cid), kinds=(MemoryRecordKind.FACT,))}
        self.assertIn(UV, facts, f"PreCompact stored nothing; runs={self.runs(cid)}")
        self.assertIs(MemoryRecordStatus.APPROVED, facts[UV].status)

    def test_session_end_capture_approval_retrieval_supersession_and_scope(self) -> None:
        # SessionEnd: a FACT lands approved, a DECISION pending.
        cid = self.store.create_conversation(self.alpha).id
        conversation = PersistedConversation(self.store, cid)
        friday_turn, _ = self.converse(conversation, FRIDAY + PAD)
        uv_turn, _ = self.converse(conversation, UV + PAD)
        self.model.capture_replies.append(json.dumps({"candidates": [
            candidate("fact", UV, "build-tool", uv_turn),
            candidate("decision", FRIDAY, "deploy-day", friday_turn),
        ]}))
        self.leave(cid)
        self.assertEqual([("session_end", "ok")], self.runs(cid))
        captured = {view.record.text: (view.record.kind, view.status) for view in
                    self.store.list_records(MemoryRecordScope(self.alpha, cid))}
        self.assertEqual({UV: (MemoryRecordKind.FACT, MemoryRecordStatus.APPROVED),
                          FRIDAY: (MemoryRecordKind.DECISION, MemoryRecordStatus.PENDING)}, captured)
        listed = self.records(self.alpha)
        self.assertIn(UV, listed, f"captured FACT not on the Hafıza page: {sorted(listed)}")
        self.assertEqual("approved", listed[UV]["status"])
        self.assertIn(FRIDAY, listed, f"captured DECISION not on the Hafıza page: {sorted(listed)}")
        self.assertEqual("pending", listed[FRIDAY]["status"])
        self.assertEqual("capture_session_end", listed[FRIDAY]["origin"])
        # Pending is never delivered.
        self.assertNotIn(FRIDAY, self.chat_memory(self.alpha))
        approved = self.app.approve_memory_record(self.alpha, listed[FRIDAY]["record_id"])
        self.assertTrue(approved["ok"], approved)
        # A new conversation in Alpha gets both, as labelled data.
        items = self.chat_memory(self.alpha)
        self.assertIn(FRIDAY, items, f"approved DECISION not retrieved: {sorted(items)}")
        self.assertIn(UV, items)
        rationale = items[FRIDAY]["rationale"]
        self.assertTrue(rationale.startswith(LABEL), rationale)
        for part in ("kind=decision", "project=Alpha", "origin=capture_session_end"):
            self.assertIn(part, rationale)

        # A newer DECISION on the same subject supersedes the older one.
        self.capture_decision(THURSDAY)
        items = self.chat_memory(self.alpha)
        self.assertIn(THURSDAY, items)
        self.assertNotIn(FRIDAY, items)
        # Delivery-level: an approved DECISION is never auto-superseded
        # (memory_consolidation); the newest approved one wins retrieval.

        # Project B never sees Alpha's memory ...
        self.assertEqual({}, self.chat_memory(self.beta))
        self.assertNotIn(THURSDAY, self.records(self.beta))
        # ... except through ITS conversation's toggle: read-only, labelled.
        beta_cid = self.store.create_conversation(self.beta).id
        toggled = self.app.set_global_memory_toggle(beta_cid, True)
        self.assertEqual({"ok": True, "conversation_id": beta_cid, "enabled": True}, toggled)
        items = self.chat_memory(self.beta, session_for=beta_cid)
        self.assertIn(THURSDAY, items)
        self.assertNotIn(FRIDAY, items)
        self.assertIn("read-only from project Alpha (all projects memory)", items[THURSDAY]["rationale"])
        thursday = self.records(self.alpha)[THURSDAY]["record_id"]
        self.assertFalse(self.app.undo_memory_record(self.beta, thursday)["ok"])
        self.assertEqual("approved", self.records(self.alpha)[THURSDAY]["status"])
        # A conversation that used it captures nothing into B: the model is
        # never asked and no run is opened, even with a candidate queued.
        captures = len(self.model.captures)
        self.model.capture_replies.append(json.dumps({"candidates": [
            candidate("fact", THURSDAY, "deploy-day", self.store.load_conversation_state(beta_cid).turns[0])]}))
        self.leave(beta_cid)
        self.assertEqual(captures, len(self.model.captures))
        self.assertEqual([], self.runs(beta_cid))
        self.assertEqual({}, self.records(self.beta))
        self.model.capture_replies.clear()

        # That conversation can never be attached; neither can a Global one
        # that turned the toggle on, even after turning it off.
        state = self.store.load_conversation_state(beta_cid)
        result = conversation_attach_result(self.store, self.settings, beta_cid,
                                            expected_next_seq=state.next_seq, project_id=self.alpha)
        self.assertNotEqual(EXIT_OK, result.exit_code)
        self.assertEqual(self.beta, self.store.load_conversation(beta_cid).project_id)
        loose = self.store.create_conversation(None).id
        self.app.set_global_memory_toggle(loose, True)
        self.app.set_global_memory_toggle(loose, False)
        state = self.store.load_conversation_state(loose)
        result = conversation_attach_result(self.store, self.settings, loose,
                                            expected_next_seq=state.next_seq, project_id=self.alpha)
        self.assertNotEqual(EXIT_OK, result.exit_code)
        self.assertIn("all projects memory", json.dumps(result.payload))
        self.assertIsNone(self.store.load_conversation(loose).project_id)

    def test_hatirla_then_undo(self) -> None:
        cid = self.store.create_conversation(self.alpha).id
        said, _ = self.converse(PersistedConversation(self.store, cid), "The release notes are written every Friday evening.")
        remembered = self.app.remember_message(cid, said.id)
        self.assertTrue(remembered["ok"], remembered)
        record = remembered["record"]
        self.assertEqual(("fact", "approved", "explicit_user", "user"),
                         (record["kind"], record["status"], record["origin"], record["actor"]))
        self.assertIn(record["text"], self.chat_memory(self.alpha, "release notes"))
        undone = self.app.undo_memory_record(self.alpha, record["record_id"])
        self.assertTrue(undone["ok"], undone)
        self.assertEqual("retracted", undone["status"])
        self.assertNotIn(record["text"], self.chat_memory(self.alpha, "release notes"))
        # Undo is a status event, not a delete: the record is still listed.
        self.assertEqual("retracted", self.records(self.alpha)[record["text"]]["status"])

    def test_context_usage_and_manual_compaction_capture_first(self) -> None:
        cid = self.store.create_conversation(self.alpha).id
        conversation = PersistedConversation(self.store, cid)
        said, _ = self.converse(conversation, UV + PAD)
        for n in range(3):
            self.converse(conversation, f"Follow-up question {n} about the release." + PAD)
        before = self.app.context_usage_payload(cid)
        self.assertTrue(before["ok"], before)
        self.assertGreater(before["used_tokens"], 0)
        self.model.capture_replies.append(json.dumps({"candidates": [candidate("fact", UV, "build-tool", said)]}))
        payload = self.app.compact_conversation(cid, keep_last=2)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(self.app.context_usage_payload(cid), payload)
        self.assertLess(payload["used_tokens"], before["used_tokens"])
        self.settle()
        state = self.store.load_conversation_state(cid)
        self.assertEqual(2, len(state.turns))
        self.assertEqual(6, len(state.compacted))
        # The capture saw exactly the folded turns, and Core stored their FACT.
        self.assertEqual([("precompact", "ok")], self.runs(cid))
        facts = {view.record.text: view.status for view in
                 self.store.list_records(MemoryRecordScope(self.alpha, cid), kinds=(MemoryRecordKind.FACT,))}
        self.assertEqual(MemoryRecordStatus.APPROVED, facts.get(UV))


if __name__ == "__main__":
    unittest.main()
