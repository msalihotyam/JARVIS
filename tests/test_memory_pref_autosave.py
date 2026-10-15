"""SECPREFAUTO: Q1 switch-on - capture auto-saves only the clean user-stated
project operational PREFERENCE, with an 'auto_saved' event for Undo; every
other Q1 row stays PENDING or refused."""

import inspect
import json
import sqlite3
import unittest
from contextlib import closing
from unittest import mock

from jarvis.contracts import (
    MODEL_ANSWER_RESULT_KEY,
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordScopeKind,
    MemoryRecordStatus,
    OutcomeStatus,
    RunStatus,
    ToolResult,
    build_model_answer,
)
from jarvis.conversation import append_turn
from jarvis.memory import SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.memory_guard import (
    Provenance,
    RecordGuard,
    SessionTaint,
    content_hash,
    decide_admission,
    verify_user_stated,
)
from jarvis.registry import ToolRegistry
from tests.helpers import DummyConnector, isolated_settings, make_task, open_core

SESSION_END = MemoryRecordOrigin.CAPTURE_SESSION_END
DEPLOY = ("We deploy on Fridays only, because the release team reviews every change "
          "on Thursday afternoon and needs a full day of buffer before shipping.")
PREF_TURN = "Please answer me in Turkish and keep every reply concise with markdown headings."
WALK_TURN = "I prefer short walks after lunch on quiet working days, it clears my head."
NEG_TURN = "Don't answer me in English, use Turkish and plain text in every reply."
PREF = "Answer me in Turkish and keep every reply concise."
INFERRED = "The person wants json output and verbose formatting for code reviews."
FLIPPED = "Answer me in English in every reply."
WALKS = "I prefer short walks after lunch."


class FakeModel:
    name = "fake-capture"
    provider = "anthropic"

    def __init__(self, reply):
        self.reply = reply
        self.model = "chat-model"

    def complete(self, task, session):
        return ToolResult(outcome=OutcomeStatus.SUCCESS, summary="ok",
                          data={MODEL_ANSWER_RESULT_KEY: build_model_answer(self.reply)})


class CapturingConnector(DummyConnector):
    def __init__(self) -> None:
        super().__init__()
        self.seen = []

    def execute(self, task, session):
        self.seen.append(task.retrieved_context)
        return super().execute(task, session)


class PreferenceAutosaveTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = isolated_settings(self)
        self.database = settings.memory_database
        self.store = SQLiteMemoryStore(self.database)
        self.connector = CapturingConnector()
        registry = ToolRegistry()
        registry.register(self.connector)
        self.core = open_core(registry, self.store, settings, destination_policy=frozenset())
        self.alpha = self.store.create_project("Alpha").id

    def session(self, results=(), turns=(DEPLOY, PREF_TURN, WALK_TURN)):
        conversation = self.store.create_conversation(self.alpha).id
        ids = []

        def change(state):
            for index, text in enumerate(turns):
                state, turn = append_turn(state, role="user", kind="user", text=text,
                                          message=text, key=f"u{index}")
                ids.append(turn.id)
            for index, capability in enumerate(results):
                state, _ = append_turn(state, role="assistant", kind="result", text="done",
                                       capability=capability, destination="(remote)",
                                       key=f"r{index}")
            return state

        self.store.update_conversation_state(conversation, change)
        return conversation, ids

    def capture(self, *texts, results=(), turns=(DEPLOY, PREF_TURN, WALK_TURN)):
        conversation, ids = self.session(results, turns)
        evidence = {PREF: ids[1], INFERRED: ids[1], WALKS: ids[2], FLIPPED: ids[-1]}
        model = FakeModel(json.dumps({"candidates": [
            {"kind": "preference", "text": text, "subject_key": "reply-style",
             "confidence": 0.9, "evidence_turn_ids": [evidence[text]]}
            for text in texts
        ]}))
        return conversation, self.core.capture_memory(conversation, SESSION_END, model)

    def rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT r.record_id, r.kind, r.text, (SELECT status FROM memory_record_events e "
                "WHERE e.record_id = r.record_id ORDER BY e.event_id DESC LIMIT 1), "
                "g.provenance FROM memory_records r JOIN memory_record_guard g "
                "ON g.record_id = r.record_id ORDER BY r.rowid"
            ).fetchall()

    def events(self, record_id, columns="status, reason"):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                f"SELECT {columns} FROM memory_record_events WHERE record_id = ? "  # nosec B608 - test constant
                "ORDER BY event_id", (record_id,)
            ).fetchall()

    def store_insert(self, guard_provenance, **flags):
        record = MemoryRecord(
            kind=MemoryRecordKind.PREFERENCE, text=PREF, subject_key="reply-style",
            scope=MemoryRecordScope(self.alpha), origin=SESSION_END,
            actor=MemoryRecordActor.MODEL, confidence=0.9,
            classification=DataClassification.INTERNAL,
        )
        guard = None if guard_provenance is None else RecordGuard(
            guard_provenance, SessionTaint.USER_TYPED, (), MemoryRecordKind.PREFERENCE,
            content_hash(PREF),
        )
        view = self.store.insert_record(record, guard, **flags)
        return self.events(view.record.record_id)

    def delivered(self, conversation):
        session = self.core.start_session("pref")
        self.store.link_session_conversation(session.id, conversation)
        task = make_task()
        task.objective = "answer in turkish concise json verbose formatting"
        task.project_id = self.alpha
        run = self.core.run(task, session.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        return {item.record_id for item in self.connector.seen[-1].items}

    def test_clean_user_stated_operational_preference_is_auto_saved_with_undo_event(self) -> None:
        # I14: no user approval is fabricated - approve_record is never reached.
        with mock.patch.object(self.store, "approve_record",
                               side_effect=AssertionError("approval fabricated")):
            _, result = self.capture(PREF)
        self.assertEqual((result.outcome, result.approved, result.pending), ("ok", 1, 0))
        [(record_id, kind, text, status, provenance)] = self.rows()
        self.assertEqual((kind, text, status, provenance),
                         ("preference", PREF, "approved", "user_stated"))
        self.assertEqual(self.events(record_id, "status, actor, reason"),
                         [("approved", "tool", "auto_saved")])
        # record_event still refuses APPROVED; the flag opens no second path.
        with self.assertRaises(ValueError):
            self.store.record_event(record_id, MemoryRecordStatus.APPROVED,
                                    MemoryRecordActor.MODEL, "auto_saved")
        self.assertEqual(self.events(record_id), [("approved", "auto_saved")])

    def test_model_inferred_preference_stays_pending_and_is_never_delivered(self) -> None:
        conversation, result = self.capture(PREF, INFERRED)
        self.assertEqual((result.outcome, result.approved, result.pending), ("ok", 1, 1))
        by_text = {row[2]: row for row in self.rows()}
        saved, inferred = by_text[PREF], by_text[INFERRED]
        self.assertEqual(inferred[1:], ("preference", INFERRED, "pending", "model_inferred"))
        self.assertEqual(self.events(inferred[0]), [("pending", "created")])
        delivered = self.delivered(conversation)
        self.assertIn(saved[0], delivered)
        self.assertNotIn(inferred[0], delivered)

    def test_tainted_session_gives_a_pending_rule(self) -> None:
        _, result = self.capture(PREF, results=("web_search",))
        self.assertEqual((result.outcome, result.approved, result.pending), ("ok", 0, 1))
        [(record_id, kind, _, status, _)] = self.rows()
        self.assertEqual((kind, status), ("rule", "pending"))
        self.assertEqual(self.events(record_id), [("pending", "created")])

    def test_global_scope_and_other_store_callers_stay_pending(self) -> None:
        admission = decide_admission(
            MemoryRecordKind.PREFERENCE, Provenance.USER_STATED, SessionTaint.USER_TYPED, (),
            MemoryRecordScopeKind.GLOBAL_READONLY, DataClassification.INTERNAL,
            operational=True, preference_autosave=True,
        )
        self.assertEqual((admission.final_kind, admission.status),
                         (MemoryRecordKind.PREFERENCE, MemoryRecordStatus.PENDING))
        # Every other insert_record caller (default False), a guardless insert
        # and a model-inferred guard never auto-save in the store itself.
        self.assertEqual(self.store_insert(Provenance.USER_STATED), [("pending", "created")])
        self.assertEqual(self.store_insert(None, preference_autosave=True),
                         [("pending", "created")])
        self.assertEqual(self.store_insert(Provenance.MODEL_INFERRED, preference_autosave=True),
                         [("pending", "created")])
        self.assertEqual(self.store_insert(Provenance.USER_STATED, preference_autosave=True),
                         [("approved", "auto_saved")])
        # INSERT-only path, schema unchanged.
        source = inspect.getsource(SQLiteMemoryStore._persist_record).upper()
        for verb in ("UPDATE ", "DELETE ", "ALTER "):
            self.assertNotIn(verb, source)
        self.assertEqual(SCHEMA_VERSION, 10)

    def test_a_dropped_user_negation_is_model_inferred_and_never_auto_saved(self) -> None:
        # R8 F1: the model leaves out the user's "Don't" and flips the preference.
        self.assertFalse(verify_user_stated(FLIPPED, NEG_TURN))
        self.assertFalse(verify_user_stated(
            "Use markdown headings in replies.",
            "Never use markdown headings in replies, plain text please."))
        self.assertTrue(verify_user_stated("Don't answer me in English.", NEG_TURN))
        _, result = self.capture(FLIPPED, turns=(DEPLOY, PREF_TURN, WALK_TURN, NEG_TURN))
        self.assertEqual((result.outcome, result.approved, result.pending), ("ok", 0, 1))
        [(record_id, kind, _, status, provenance)] = self.rows()
        self.assertEqual((kind, status, provenance), ("preference", "pending", "model_inferred"))
        self.assertEqual(self.events(record_id), [("pending", "created")])

    def test_non_operational_preference_is_refused(self) -> None:
        _, result = self.capture(WALKS)
        self.assertEqual((result.outcome, result.refused, result.record_ids), ("ok", 1, ()))
        self.assertEqual(result.counts()["reasons"], {"preference_not_operational": 1})
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
