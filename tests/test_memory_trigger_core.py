"""V2.1a M2b: Core capture from given turns and abort of open capture runs.

Fake providers only; every store lives in the test's own TEMP database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace

from jarvis.app import MemoryTriggers
from jarvis.contracts import (
    MODEL_ANSWER_RESULT_KEY,
    MemoryRecordOrigin,
    OutcomeStatus,
    ToolResult,
    build_model_answer,
)
from jarvis.conversation import (
    RAW_TURNS_MAX,
    TURN_MAX_CHARS,
    PersistedConversation,
    append_turn,
    register_precompact_hook,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from tests.helpers import isolated_settings, open_app, open_core

PRECOMPACT = MemoryRecordOrigin.CAPTURE_PRECOMPACT
SESSION_END = MemoryRecordOrigin.CAPTURE_SESSION_END
RECORD_TABLES = ("memory_records", "memory_record_events", "memory_record_supersessions")


class FakeModel:
    """Proposes one fact citing the first transcript turn; may block on a gate."""

    name = "fake-capture"

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.calls: list[str] = []
        self.started = threading.Event()

    def complete(self, task, session):
        self.calls.append(task.objective)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        transcript = task.objective.split("<transcript>\n", 1)[1].split("\n</transcript>", 1)[0]
        first = json.loads(transcript)[0]["id"]
        reply = json.dumps({"candidates": [{
            "kind": "fact", "text": "The build uses uv.", "subject_key": "tooling",
            "confidence": 0.9, "evidence_turn_ids": [first]}]})
        return ToolResult(outcome=OutcomeStatus.SUCCESS, summary="ok",
                          data={MODEL_ANSWER_RESULT_KEY: build_model_answer(reply)})


class _Base(unittest.TestCase):
    def rows(self, sql, params=()):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql, params).fetchall()

    def runs(self):
        return self.rows("SELECT run_id, conversation_id, trigger, outcome, finished_at IS NOT NULL "
                         "FROM memory_capture_runs ORDER BY started_at, run_id")

    def record_counts(self):
        return tuple(self.rows(f"SELECT COUNT(*) FROM {table}")[0][0] for table in RECORD_TABLES)  # nosec B608 - constant names

    def say(self, conversation_id, *texts, raw_turns_max=RAW_TURNS_MAX):
        doomed: list = []

        def change(state):
            for text in texts:
                state, _ = append_turn(state, role="user", kind="user", text=text, message=text,
                                       key=f"k{state.next_seq}", raw_turns_max=raw_turns_max,
                                       on_compact=doomed.extend)
            return state

        self.store.update_conversation_state(conversation_id, change)
        return tuple(doomed)


class CaptureFromTurnsTests(_Base):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.database = self.settings.memory_database
        self.store = SQLiteMemoryStore(self.database)
        self.core = open_core(ToolRegistry(), self.store, self.settings, destination_policy=frozenset())
        self.alpha = self.store.create_project("Alpha").id
        self.cid = self.store.create_conversation(self.alpha).id
        self.say(self.cid, "We deploy on Fridays only.", "Use uv for the build.", "Tests run nightly.")
        self.turns = self.store.load_conversation_state(self.cid).turns

    def test_captures_exactly_the_given_turns_and_a_reoffer_dedupes(self) -> None:
        model = FakeModel()
        given = self.turns[1:]
        first = self.core.capture_memory(self.cid, SESSION_END, model, turns=given)
        self.assertEqual((first.outcome, first.approved), ("ok", 1))
        sent = json.loads(model.calls[0].split("<transcript>\n", 1)[1].split("\n</transcript>", 1)[0])
        self.assertEqual([t["id"] for t in sent], [t.id for t in given])
        self.assertEqual(self.rows("SELECT source_turn_ids FROM memory_records"), [(json.dumps([given[0].id]),)])
        again = self.core.capture_memory(self.cid, SESSION_END, model, turns=tuple(given))
        self.assertEqual((again.run_id, again.outcome, again.duplicate), (first.run_id, "duplicate", True))
        self.assertEqual(len(model.calls), 1)
        # The same range named by turns= or by the stored history is one run.
        whole = self.core.capture_memory(self.cid, PRECOMPACT, model, turns=self.turns)
        stored = self.core.capture_memory(self.cid, PRECOMPACT, model)
        self.assertEqual((stored.run_id, stored.outcome), (whole.run_id, "duplicate"))

    def test_turns_none_keeps_the_stored_history(self) -> None:
        model = FakeModel()
        result = self.core.capture_memory(self.cid, SESSION_END, model)
        sent = json.loads(model.calls[0].split("<transcript>\n", 1)[1].split("\n</transcript>", 1)[0])
        self.assertEqual((result.outcome, [t["id"] for t in sent]), ("ok", [t.id for t in self.turns]))

    def test_invalid_foreign_or_oversized_turns_are_refused_with_no_run_row(self) -> None:
        other = self.store.create_conversation(self.alpha).id
        self.say(other, "Foreign words.")
        foreign = self.store.load_conversation_state(other).turns
        t1, t2, t3 = self.turns
        # Genuine stored turns, refused only for their count.
        many = self.store.create_conversation(self.alpha).id
        self.say(many, *(f"Line {i}." for i in range(RAW_TURNS_MAX + 1)), raw_turns_max=RAW_TURNS_MAX + 1)
        too_many = self.store.load_conversation_state(many).turns
        self.assertEqual(len(too_many), RAW_TURNS_MAX + 1)
        model = FakeModel()
        self.assertEqual(self.core.capture_memory(many, SESSION_END, model, turns=too_many).outcome,
                         "invalid_turns")
        cases = {
            "empty": (),
            "list": [t1],
            "not a turn": ({"id": t1.id, "seq": 1, "role": "user", "kind": "user", "text": "x"},),
            "edited text": (replace(t1, text="Deploy any day."),),
            "promoted role": (replace(t3, role="assistant", kind="reply"),),
            "foreign": foreign,
            "gap": (t1, t3),
            "out of order": (t2, t1),
            "unknown seq": (replace(t3, seq=9, id="t9-x"),),
            "id of another seq": (replace(t1, id=t2.id),),
        }
        for name, turns in cases.items():
            with self.subTest(name):
                result = self.core.capture_memory(self.cid, SESSION_END, model, turns=turns)
                self.assertEqual((result.run_id, result.outcome), ("", "invalid_turns"))
        self.assertEqual((model.calls, self.runs(), self.record_counts()), ([], [], (0, 0, 0)))

    def test_compacted_batch_is_accepted_only_as_checkpointed(self) -> None:
        cid = self.store.create_conversation(self.alpha).id
        long_text = "We deploy on Fridays only. " + "a" * 400

        def change(state):
            doomed = []
            for role, kind, text in (("user", "user", long_text), ("assistant", "reply", "Noted."),
                                     ("user", "user", "Use uv."), ("user", "user", "Third.")):
                state, _ = append_turn(state, role=role, kind=kind, text=text,
                                       message=text if role == "user" else "", key=f"k{state.next_seq}",
                                       raw_turns_max=2, on_compact=doomed.extend)
            captured[:] = doomed
            return state

        captured: list = []
        self.store.update_conversation_state(cid, change)
        doomed = tuple(captured)
        self.assertEqual([t.kind for t in doomed], ["user", "reply"])
        state = self.store.load_conversation_state(cid)
        self.assertEqual(len(state.compacted), 2)
        self.assertNotIn(doomed[0].id, [t.id for t in state.turns])
        user, reply = doomed
        model = FakeModel()
        cases = {
            "edited text": (replace(user, text="We deploy on Mondays only."),),
            # Same digest (only the opening is checkpointed); refused by the clip cap.
            "oversized text": (replace(user, text=long_text + "a" * TURN_MAX_CHARS),),
            "reply relabelled as user": (user, replace(reply, role="user", kind="user")),
            "capability": (replace(user, capability="web_search"),),
            "id of another seq": (replace(user, id=reply.id),),
            "id suffix": (replace(user, id=f"t{user.seq}-forged"),),
        }
        for name, turns in cases.items():
            with self.subTest(name):
                refused = self.core.capture_memory(cid, PRECOMPACT, model, turns=turns)
                self.assertEqual(refused.outcome, "invalid_turns")
        self.assertEqual((model.calls, self.runs()), ([], []))
        result = self.core.capture_memory(cid, PRECOMPACT, model, turns=doomed)
        self.assertEqual((result.outcome, result.approved), ("ok", 1))
        self.assertEqual(self.rows("SELECT trigger, outcome FROM memory_capture_runs"), [("precompact", "ok")])


class AbortCaptureRunsTests(_Base):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.database = self.settings.memory_database
        self.store = SQLiteMemoryStore(self.database)
        self.core = open_core(ToolRegistry(), self.store, self.settings, destination_policy=frozenset())
        alpha = self.store.create_project("Alpha").id
        self.cid = self.store.create_conversation(alpha).id
        self.say(self.cid, "Use uv for the build.")
        self.assertEqual(self.core.capture_memory(self.cid, SESSION_END, FakeModel()).outcome, "ok")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany(
                "INSERT INTO memory_capture_runs (run_id, conversation_id, trigger, started_at) VALUES (?, ?, ?, ?)",
                [("open-a", self.cid, "precompact", "2026-01-01T00:00:00Z"),
                 ("open-a2", self.cid, "session_end", "2026-01-01T00:00:01Z"),
                 ("open-b", "conv-b", "precompact", "2026-01-01T00:00:02Z")])

    def test_only_open_runs_of_that_conversation_are_aborted(self) -> None:
        before = {row[0]: row[3:] for row in self.runs()}
        counts = self.record_counts()
        self.assertEqual(counts[0], 1)
        self.assertEqual(self.core.abort_capture_runs(self.cid), 2)
        after = {row[0]: row[3:] for row in self.runs()}
        self.assertEqual((after["open-a"], after["open-a2"]), (("aborted", 1), ("aborted", 1)))
        self.assertEqual(after["open-b"], (None, 0))
        finished = [run for run in before if run.startswith("cap-")]
        self.assertEqual([after[run] for run in finished], [("ok", 1)])
        self.assertEqual(self.record_counts(), counts)
        self.assertEqual(self.core.abort_capture_runs(self.cid), 0)
        self.assertEqual(self.core.abort_capture_runs("conv-none"), 0)
        with self.assertRaises(ValueError):
            self.core.abort_capture_runs("")


class EndToEndTests(_Base):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.database = self.settings.memory_database
        self.app = open_app(settings=self.settings, destination_policy=())
        self.store = self.app.memory
        self.addCleanup(register_precompact_hook, self.store, None)
        self.app.memory_triggers = MemoryTriggers()
        self.addCleanup(self.app.memory_triggers.shutdown, self.app, (), 5.0)
        self.cid = self.store.create_conversation(self.store.create_project("Alpha").id).id

    def settle(self) -> None:
        triggers = self.app.memory_triggers
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with triggers._lock:
                if not triggers._threads and not triggers._jobs:
                    return
            time.sleep(0.01)
        self.fail("capture worker did not settle")

    def test_precompact_and_session_end_reach_core(self) -> None:
        self.app.intent_model = FakeModel()
        self.app.watch_conversations(self.store)
        conversation = PersistedConversation(self.store, self.cid, raw_turns_max=2)
        for text in ("We deploy on Fridays only.", "Use uv.", "Tests run nightly."):
            self.assertIsNotNone(conversation.append(role="user", kind="user", text=text, message=text))
        self.settle()
        self.assertEqual([r[2:] for r in self.runs()], [("precompact", "ok", 1)])
        self.assertTrue(self.app.conversation_left(self.cid))
        self.settle()
        self.assertEqual(sorted(r[2:] for r in self.runs()),
                         [("precompact", "ok", 1), ("session_end", "ok", 1)])
        self.assertEqual(self.record_counts()[0], 2)

    def test_shutdown_aborts_the_open_run(self) -> None:
        gate = threading.Event()
        model = self.app.intent_model = FakeModel(gate)
        self.addCleanup(gate.set)
        PersistedConversation(self.store, self.cid).append(role="user", kind="user", text="Use uv.", message="Use uv.")
        self.assertTrue(self.app.conversation_left(self.cid))
        self.assertTrue(model.started.wait(5))
        self.assertEqual(self.app.shutdown_memory_capture((), 0.05), (self.cid,))
        self.assertEqual([r[2:] for r in self.runs()], [("session_end", "aborted", 1)])
        gate.set()
        self.settle()
        # The late finish does not overwrite the abort.
        self.assertEqual([r[2:] for r in self.runs()], [("session_end", "aborted", 1)])


if __name__ == "__main__":
    unittest.main()
