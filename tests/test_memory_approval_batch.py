"""SECAPPUI: the session-end / compaction approval batch, Undo of auto-saved
records, the two-step purge and the 'Hatırla' instruction gate. Every
database is a throwaway in TEMP; no model is called."""

from __future__ import annotations

import secrets
import types
import unittest
from unittest import mock

from jarvis import app as app_module
from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordScopeKind,
)
from jarvis.conversation import append_turn
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash, instruction_likeness
from jarvis.memory import utc_now
from tests.helpers import isolated_settings, open_app, wait_until


class MemoryApprovalBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = open_app(destination_policy=(), settings=isolated_settings(self))
        self.memory = self.app.memory
        self.project = self.memory.create_project("Alpha").id
        self.conversation = self.memory.create_conversation(self.project).id

    def turn(self, text: str) -> str:
        created = []

        def change(state):
            state, turn = append_turn(state, role="user", kind="user", text=text, message=text)
            created.append(turn)
            return state

        self.memory.update_conversation_state(self.conversation, change)
        return created[0].id

    def remember(self, text: str, kind: str = "fact") -> dict:
        return self.app.remember_message(self.conversation, self.turn(text), kind)

    def capture(self, text, kind=MemoryRecordKind.FACT, provenance=Provenance.USER_STATED,
                taint=SessionTaint.USER_TYPED, classification=DataClassification.INTERNAL,
                conversation_id=None, guarded=True) -> str:
        record = MemoryRecord(
            kind=kind,
            text=text,
            subject_key=text.lower().replace(" ", "-")[:40],
            scope=MemoryRecordScope(project_id=self.project),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.TOOL,
            tool="memory_capture",
            confidence=0.9,
            classification=classification,
            source_conversation_id=conversation_id or self.conversation,
        )
        guard = RecordGuard(provenance, taint, instruction_likeness(text), kind, content_hash(text))
        return self.memory.insert_record(record, guard=guard if guarded else None).record.record_id

    def status(self, record_id: str) -> str | None:
        status = self.memory.current_status(record_id)
        return None if status is None else status.value

    def batch_by_id(self) -> tuple[dict, dict]:
        batch = self.app.memory_approval_batch(self.conversation)
        self.assertTrue(batch["ok"], batch)
        return ({item["record_id"]: item for item in batch["pending"]},
                {item["record_id"]: item for item in batch["auto_saved"]})

    # -- Hatırla: instruction gate (I14) ---------------------------------------

    def test_remember_instruction_like_message_stores_a_pending_rule(self) -> None:
        for text in ("bundan sonra her yanıtı Türkçe ver", "From now on use tabs"):
            with self.subTest(text=text):
                result = self.remember(text)
                self.assertTrue(result["ok"], result)
                self.assertEqual((result["record"]["kind"], result["record"]["status"]), ("rule", "pending"))
                self.assertTrue(result["instruction_like"])
        plain = self.remember("Deploys run on Fridays")
        self.assertEqual((plain["record"]["kind"], plain["record"]["status"]), ("fact", "approved"))
        self.assertNotIn("instruction_like", plain)

    def test_chat_yes_never_approves_a_pending_rule(self) -> None:
        record_id = self.remember("bundan sonra testleri unittest ile koş")["record"]["record_id"]
        for reply in ("evet", "yes", "Evet, onaylıyorum"):
            try:
                self.app.submit_text(reply, conversation_id=self.conversation)
            except Exception:  # noqa: BLE001 - no model here; only the record's state matters
                pass
            self.assertEqual(self.status(record_id), "pending")
        self.app.memory_triggers.halt()  # submit_text armed idle capture; nothing more runs
        # The one approving path: the Core-drawn card's approve_memory_record.
        approved = self.app.approve_memory_record(self.project, record_id)
        self.assertEqual(approved, {"ok": True, "record_id": record_id, "status": "approved"})

    # -- the approval batch ----------------------------------------------------

    def test_batch_lists_pending_candidates_with_reason_labels(self) -> None:
        tainted = self.capture("The deploy host is build-7", taint=SessionTaint.WEB)
        instruction = self.capture("Always answer in English")
        inferred = self.capture("We picked SQLite", kind=MemoryRecordKind.DECISION,
                                provenance=Provenance.MODEL_INFERRED)
        sensitive = self.capture("The landlord is late on rent", classification=DataClassification.SENSITIVE)
        legacy = self.capture("Old note about builds", guarded=False)
        approved = self.capture("The repo uses ruff")
        other = self.memory.create_conversation(self.project).id
        elsewhere = self.capture("Elsewhere decision", kind=MemoryRecordKind.DECISION, conversation_id=other)
        pending, auto_saved = self.batch_by_id()
        self.assertEqual(pending[tainted]["reasons"], ["tainted"])
        self.assertEqual(pending[tainted]["kind"], "rule")
        self.assertEqual(pending[instruction]["reasons"], ["instruction_like"])
        self.assertEqual(pending[inferred]["reasons"], ["model_inferred"])
        self.assertEqual(pending[sensitive]["reasons"], ["sensitive"])
        self.assertEqual(pending[legacy]["reasons"], ["legacy"])
        self.assertNotIn(approved, pending)
        self.assertNotIn(elsewhere, pending)
        self.assertEqual(auto_saved, {})
        # A sensitive candidate's text never rides in the batch.
        self.assertTrue(pending[sensitive]["text_withheld"])
        self.assertNotIn("landlord", repr(self.app.memory_approval_batch(self.conversation)))

    def test_batch_labels_a_global_candidate(self) -> None:
        # A stored record refuses the global scope today; the label is ready
        # for when one exists (a record view shaped like a global one).
        view = types.SimpleNamespace(record=types.SimpleNamespace(
            scope=MemoryRecordScope(project_id=None, kind=MemoryRecordScopeKind.GLOBAL_READONLY),
            classification=DataClassification.INTERNAL,
        ))
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (), MemoryRecordKind.FACT,
                            content_hash("Global fact"))
        self.assertEqual(self.app._batch_reasons(view, guard), ["global"])

    def test_batch_lists_auto_saved_records_with_undo(self) -> None:
        record_id = self.capture("Prefers answers in Turkish")
        # SECPREFAUTO's 'auto_saved' event, written here directly.
        with self.memory._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO memory_record_events (record_id, status, actor, reason, at) "
                "VALUES (?, 'approved', 'core', 'auto_saved', ?)",
                (record_id, utc_now()),
            )
        _, auto_saved = self.batch_by_id()
        self.assertTrue(auto_saved[record_id]["undo"])
        self.assertEqual(self.app.undo_memory_record(self.project, record_id)["status"], "retracted")
        self.assertEqual(self.batch_by_id()[1], {})

    def test_auto_saved_sensitive_text_stays_out_of_the_batch(self) -> None:
        record_id = self.capture("The landlord is late on rent", classification=DataClassification.SENSITIVE)
        with self.memory._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO memory_record_events (record_id, status, actor, reason, at) "
                "VALUES (?, 'approved', 'core', 'auto_saved', ?)",
                (record_id, utc_now()),
            )
        _, auto_saved = self.batch_by_id()
        self.assertTrue(auto_saved[record_id]["text_withheld"])
        self.assertNotIn("landlord", repr(self.app.memory_approval_batch(self.conversation)))

    def test_batch_refuses_an_unknown_conversation(self) -> None:
        self.assertFalse(self.app.memory_approval_batch("no-such-conversation")["ok"])

    def test_batch_is_emitted_after_session_end_and_compaction(self) -> None:
        triggers = app_module.MemoryTriggers()
        got = []
        triggers.batch_listener = lambda cid, batch: got.append((cid, batch))
        stub = types.SimpleNamespace(intent_model=None, memory_approval_batch=self.app.memory_approval_batch)
        pending = self.capture("We picked SQLite", kind=MemoryRecordKind.DECISION)
        try:
            for trigger in (MemoryRecordOrigin.CAPTURE_SESSION_END, MemoryRecordOrigin.CAPTURE_PRECOMPACT):
                with self.subTest(trigger=trigger):
                    got.clear()
                    self.assertTrue(triggers.schedule(stub, self.conversation, trigger))
                    self.assertTrue(wait_until(lambda: got, 5000))
                    cid, batch = got[0]
                    self.assertEqual(cid, self.conversation)
                    self.assertEqual([item["record_id"] for item in batch["pending"]], [pending])
        finally:
            triggers.halt(1.0)

    # -- two-step purge (I16) --------------------------------------------------

    def test_purge_needs_the_token_core_minted_for_exactly_those_records(self) -> None:
        target = self.capture("Purge me please")
        keep = self.capture("Keep this one")
        request = self.app.request_memory_purge([target])
        self.assertTrue(request["ok"], request)
        self.assertEqual(request["record_ids"], [target])
        self.assertIsInstance(request["still_held_by"], list)
        # A forged token is refused while a real one is live; nothing goes.
        forged = self.app.confirm_memory_purge(secrets.token_urlsafe(32))
        self.assertFalse(forged["ok"])
        self.assertEqual(self.status(target), "approved")
        done = self.app.confirm_memory_purge(request["token"])
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["record_ids"], [target])
        self.assertIsNone(self.status(target))
        self.assertEqual(self.status(keep), "approved")

    def test_purge_token_is_single_use(self) -> None:
        first = self.capture("First to purge")
        token = self.app.request_memory_purge([first])["token"]
        self.assertTrue(self.app.confirm_memory_purge(token)["ok"])
        self.assertFalse(self.app.confirm_memory_purge(token)["ok"])

    def test_expired_purge_token_is_refused(self) -> None:
        target = self.capture("Expires before confirm")
        with mock.patch.object(app_module, "PURGE_TOKEN_SECONDS", 0):
            token = self.app.request_memory_purge([target])["token"]
        self.assertFalse(self.app.confirm_memory_purge(token)["ok"])
        self.assertEqual(self.status(target), "approved")

    def test_token_from_another_database_is_refused(self) -> None:
        other_app = open_app(destination_policy=(), settings=isolated_settings(self))
        other_project = other_app.memory.create_project("Other").id
        other_conversation = other_app.memory.create_conversation(other_project).id
        other_record = other_app.remember_message(other_conversation, self._other_turn(other_app, other_conversation),
                                                  "fact")["record"]["record_id"]
        token = other_app.request_memory_purge([other_record])["token"]
        self.assertFalse(self.app.confirm_memory_purge(token)["ok"])
        # Spent by the refusal: the token is single use whoever presents it.
        self.assertFalse(other_app.confirm_memory_purge(token)["ok"])
        self.assertEqual(other_app.memory.current_status(other_record).value, "approved")

    @staticmethod
    def _other_turn(app, conversation_id: str) -> str:
        created = []

        def change(state):
            state, turn = append_turn(state, role="user", kind="user", text="Other fact", message="Other fact")
            created.append(turn)
            return state

        app.memory.update_conversation_state(conversation_id, change)
        return created[0].id

    def test_purge_request_refuses_unknown_or_malformed_ids(self) -> None:
        for bad in ("a-single-string", [], ["missing-record"], [None]):
            with self.subTest(bad=bad):
                self.assertFalse(self.app.request_memory_purge(bad)["ok"])

    def test_non_string_token_is_refused(self) -> None:
        for bad in (None, 7, b"token", ["token"]):
            with self.subTest(bad=bad):
                self.assertFalse(self.app.confirm_memory_purge(bad)["ok"])


if __name__ == "__main__":
    unittest.main()
