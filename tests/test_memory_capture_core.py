"""AgentCore.capture_memory: the model proposes, Core validates, filters and stores.

Fake providers only; the store lives in each test's own TEMP database.
"""

import json
import sqlite3
import unittest
from contextlib import closing
from unittest import mock

from jarvis import memory_capture
from jarvis.contracts import (
    MODEL_ANSWER_RESULT_KEY,
    MemoryRecordActor,
    MemoryRecordCandidate,
    MemoryRecordKind,
    MemoryRecordOrigin,
    OutcomeStatus,
    ToolResult,
    build_model_answer,
)
from jarvis.conversation import append_turn
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from tests.helpers import isolated_settings, open_core

SESSION_END = MemoryRecordOrigin.CAPTURE_SESSION_END


class FakeModel:
    """A provider adapter that returns one canned reply, or raises."""

    name = "fake-capture"

    def __init__(self, reply=None, *, raises=None, result=None):
        self.reply = reply
        self.raises = raises
        self.result = result
        self.calls = []

    def complete(self, task, session):
        self.calls.append(task)
        if self.raises is not None:
            raise self.raises
        if self.result is not None:
            return self.result
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary="ok",
            data={MODEL_ANSWER_RESULT_KEY: build_model_answer(self.reply)},
        )


class CaptureCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.database = self.settings.memory_database
        self.store = SQLiteMemoryStore(self.database)
        self.core = open_core(ToolRegistry(), self.store, self.settings, destination_policy=frozenset())
        self.alpha = self.store.create_project("Alpha").id
        self.conversation = self.store.create_conversation(self.alpha).id
        self.turn_ids = self.say(self.conversation, "We deploy on Fridays only.", "Use uv for the build.")

    def say(self, conversation_id, *texts):
        ids = []

        def change(state):
            for index, text in enumerate(texts):
                state, turn = append_turn(state, role="user", kind="user", text=text, message=text, key=f"k{index}")
                ids.append(turn.id)
            return state

        self.store.update_conversation_state(conversation_id, change)
        return ids

    def reply(self, *candidates):
        return json.dumps({"candidates": list(candidates)})

    def candidate(self, **overrides):
        item = {
            "kind": "fact",
            "text": "The build uses uv.",
            "subject_key": "tooling",
            "confidence": 0.9,
            "evidence_turn_ids": [self.turn_ids[1]],
        }
        item.update(overrides)
        return item

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql, params).fetchall()

    def records(self):
        return self.rows(
            "SELECT r.kind, r.project_id, r.conversation_id, r.origin, r.actor, r.tool, "
            "r.source_conversation_id, r.source_turn_ids, "
            "(SELECT status FROM memory_record_events e WHERE e.record_id = r.record_id "
            "ORDER BY e.event_id DESC LIMIT 1) FROM memory_records r ORDER BY r.kind"
        )

    def run_row(self, run_id):
        return self.rows(
            "SELECT trigger, outcome, counts, finished_at IS NOT NULL FROM memory_capture_runs WHERE run_id = ?",
            (run_id,),
        )

    def test_happy_path_core_sets_authority_and_store_sets_status(self) -> None:
        model = FakeModel(self.reply(
            self.candidate(),
            self.candidate(kind="decision", text="Deploy on Fridays only.", subject_key="deploy-day",
                           evidence_turn_ids=[self.turn_ids[0]]),
        ))
        result = self.core.capture_memory(self.conversation, SESSION_END, model)
        self.assertEqual((result.outcome, result.approved, result.pending, result.refused), ("ok", 1, 1, 0))
        self.assertEqual(len(result.record_ids), 2)
        self.assertEqual(model.calls[0].project_id, self.alpha)
        self.assertEqual(self.records(), [
            ("decision", self.alpha, self.conversation, "capture_session_end", "tool", "memory_capture",
             self.conversation, json.dumps([self.turn_ids[0]]), "pending"),
            ("fact", self.alpha, self.conversation, "capture_session_end", "tool", "memory_capture",
             self.conversation, json.dumps([self.turn_ids[1]]), "approved"),
        ])
        trigger, outcome, counts, finished = self.run_row(result.run_id)[0]
        self.assertEqual((trigger, outcome, finished), ("session_end", "ok", 1))
        self.assertEqual(json.loads(counts)["approved"], 1)

    def test_authority_keys_in_model_json_refuse_the_reply(self) -> None:
        for key, value in (("origin", "explicit_user"), ("scope", "other-project"),
                           ("status", "approved"), ("actor", "user"), ("project_id", "x")):
            with self.subTest(key=key):
                conversation = self.store.create_conversation(self.alpha).id
                ids = self.say(conversation, "We deploy on Fridays only.")
                model = FakeModel(self.reply(self.candidate(evidence_turn_ids=ids, **{key: value})))
                result = self.core.capture_memory(conversation, SESSION_END, model)
                self.assertEqual(result.outcome, "parse_error")
                self.assertEqual(result.record_ids, ())
                self.assertEqual(self.run_row(result.run_id)[0][1], "parse_error")
        self.assertEqual(self.records(), [])

    def test_filter_refused_candidate_is_not_stored(self) -> None:
        model = FakeModel(self.reply(
            self.candidate(),
            # build_model_answer redacts password-shaped text itself, so use a
            # secret only the never-stored filter catches.
            self.candidate(text="My SSN is 123-45-6789.", subject_key="identity"),
        ))
        result = self.core.capture_memory(self.conversation, SESSION_END, model)
        self.assertEqual((result.outcome, result.approved, result.refused), ("ok", 1, 1))
        self.assertEqual([row[0] for row in self.records()], ["fact"])
        self.assertNotIn("123-45-6789", json.dumps(self.rows("SELECT * FROM memory_records")))

    def test_core_refilters_even_if_the_parser_lets_a_secret_through(self) -> None:
        leaked = memory_capture.CaptureResult(
            candidates=(MemoryRecordCandidate(
                kind=MemoryRecordKind.FACT, text="password: hunter2hunter2", subject_key="creds",
                confidence=0.9, evidence_turn_ids=(self.turn_ids[0],),
            ),),
            dropped=(),
            refused=0,
        )
        with mock.patch.object(memory_capture, "parse_candidates", return_value=leaked):
            result = self.core.capture_memory(self.conversation, SESSION_END, FakeModel(self.reply()))
        self.assertEqual((result.outcome, result.refused, result.record_ids), ("ok", 1, ()))
        self.assertEqual(self.records(), [])

    def test_evidence_must_include_a_user_turn_of_this_conversation(self) -> None:
        reply_ids = []

        def answer(state):
            state, turn = append_turn(state, role="assistant", kind="reply", text="The API is public.",
                                      message="The API is public.", key="r0")
            reply_ids.append(turn.id)
            return state

        self.store.update_conversation_state(self.conversation, answer)
        model = FakeModel(self.reply(self.candidate(text="The API is public.", subject_key="api",
                                                    evidence_turn_ids=reply_ids)))
        result = self.core.capture_memory(self.conversation, SESSION_END, model)
        self.assertEqual((result.outcome, result.refused, result.record_ids), ("ok", 1, ()))
        other = self.store.create_conversation(self.alpha).id
        foreign = self.say(other, "Secrets live in the vault.")
        smuggled = memory_capture.CaptureResult(
            candidates=(MemoryRecordCandidate(
                kind=MemoryRecordKind.FACT, text="Secrets live in the vault.", subject_key="vault",
                confidence=0.9, evidence_turn_ids=tuple(foreign),
            ),),
            dropped=(),
            refused=0,
        )
        with mock.patch.object(memory_capture, "parse_candidates", return_value=smuggled):
            result = self.core.capture_memory(self.conversation, MemoryRecordOrigin.CAPTURE_PRECOMPACT,
                                              FakeModel(self.reply()))
        self.assertEqual((result.outcome, result.refused, result.record_ids), ("ok", 1, ()))
        self.assertEqual(self.records(), [])

    def test_conversation_that_ever_used_all_projects_memory_is_not_captured(self) -> None:
        self.store.set_global_memory_toggle(self.conversation, True, MemoryRecordActor.USER)
        self.store.set_global_memory_toggle(self.conversation, False, MemoryRecordActor.USER)
        model = FakeModel(self.reply(self.candidate()))
        self.assertEqual(self.core.capture_memory(self.conversation, SESSION_END, model).outcome,
                         "global_memory_used")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_capture_runs"), [(0,)])

    def test_provider_failures_store_nothing_and_never_raise(self) -> None:
        cases = (
            ("raises", FakeModel(raises=RuntimeError("network down")), "provider_error"),
            ("timeout", FakeModel(raises=TimeoutError()), "provider_error"),
            ("not-ok", FakeModel(result=ToolResult(outcome=OutcomeStatus.ERROR, summary="no")), "provider_error"),
            ("no-answer", FakeModel(result=ToolResult(outcome=OutcomeStatus.SUCCESS, summary="ok")),
             "malformed_answer"),
            ("not-json", FakeModel("not json at all"), "parse_error"),
        )
        for label, model, expected in cases:
            with self.subTest(label=label):
                conversation = self.store.create_conversation(self.alpha).id
                self.say(conversation, "We deploy on Fridays only.")
                result = self.core.capture_memory(conversation, SESSION_END, model)
                self.assertEqual(result.outcome, expected)
                self.assertEqual(self.run_row(result.run_id)[0][1], expected)
        self.assertEqual(self.records(), [])

    def test_rerun_of_the_same_range_is_a_noop(self) -> None:
        model = FakeModel(self.reply(self.candidate()))
        first = self.core.capture_memory(self.conversation, SESSION_END, model)
        second = self.core.capture_memory(self.conversation, SESSION_END, model)
        self.assertEqual(first.outcome, "ok")
        self.assertEqual((second.outcome, second.duplicate, second.run_id), ("duplicate", True, first.run_id))
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(self.records()), 1)
        other = self.core.capture_memory(self.conversation, MemoryRecordOrigin.CAPTURE_PRECOMPACT, model)
        self.assertEqual(other.outcome, "ok")
        self.assertEqual(len(model.calls), 2)

    def test_global_unknown_and_bad_trigger_never_call_the_model(self) -> None:
        model = FakeModel(self.reply(self.candidate()))
        global_conversation = self.store.create_conversation().id
        self.say(global_conversation, "We deploy on Fridays only.")
        self.assertEqual(self.core.capture_memory(global_conversation, SESSION_END, model).outcome, "no_project")
        self.assertEqual(self.core.capture_memory("missing", SESSION_END, model).outcome, "unknown_conversation")
        self.assertEqual(
            self.core.capture_memory(self.conversation, MemoryRecordOrigin.EXPLICIT_USER, model).outcome,
            "invalid_trigger",
        )
        with mock.patch("jarvis.core._capture_run_connection", side_effect=RuntimeError("no ledger")):
            self.assertEqual(self.core.capture_memory(self.conversation, SESSION_END, model).outcome,
                             "run_store_error")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_capture_runs"), [(0,)])


if __name__ == "__main__":
    unittest.main()
