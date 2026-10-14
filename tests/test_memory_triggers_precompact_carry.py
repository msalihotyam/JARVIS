"""MEMGATEFIX GAP 2: a PreCompact batch Core skipped (too short, reply-only)
rides in front of the next PreCompact / SessionEnd batch, so the folded turns
are captured once there is enough to go on - bounded, and never twice.

Real Core and store (each test's own TEMP database), a fake capture model.
"""

import json
import sqlite3
import time
import unittest
from contextlib import closing
from types import SimpleNamespace

from jarvis.app import MemoryTriggers
from jarvis.contracts import (
    MODEL_ANSWER_RESULT_KEY,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
    OutcomeStatus,
    ToolResult,
    build_model_answer,
)
from jarvis.conversation import RAW_TURNS_MAX, PersistedConversation, register_precompact_hook
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from tests.helpers import isolated_settings, open_app

PRECOMPACT = MemoryRecordOrigin.CAPTURE_PRECOMPACT
PAD = " Keep the deploy checklist short and readable for the whole team please." * 3


class CaptureModel:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.prompts: list[str] = []

    def complete(self, task, session):
        self.prompts.append(task.objective)
        reply = self.replies.pop(0) if self.replies else json.dumps({"candidates": []})
        return ToolResult(outcome=OutcomeStatus.SUCCESS, summary="ok",
                          data={MODEL_ANSWER_RESULT_KEY: build_model_answer(reply)})


class PrecompactCarryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.database = self.settings.memory_database
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.app = open_app(destination_policy=(), settings=self.settings, memory=self.store,
                            registry=ToolRegistry())
        self.model = self.app.intent_model = CaptureModel()
        self.app.memory_triggers = MemoryTriggers()
        self.addCleanup(self.app.memory_triggers.shutdown, self.app, (), 5.0)
        self.addCleanup(register_precompact_hook, self.store, None)
        # The hook fires inside the store's write: schedule once append()
        # committed, so Core always reads the turn that caused the fold.
        self.folded = []
        register_precompact_hook(self.store, lambda cid, turns: self.folded.append(turns))
        self.cid = self.store.create_conversation(self.alpha).id
        self.conversation = PersistedConversation(self.store, self.cid, raw_turns_max=2)

    def say(self, role, text):
        kind = "user" if role == "user" else "reply"
        turn = self.conversation.append(role=role, kind=kind, text=text, message=text)
        self.assertIsNotNone(turn, self.conversation.diagnostic)
        while self.folded:
            self.app.memory_triggers.schedule(self.app, self.cid, PRECOMPACT, self.folded.pop(0))
        self.settle()
        return turn

    def settle(self) -> None:
        triggers = self.app.memory_triggers
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with triggers._lock:
                if not triggers._threads and not triggers._jobs:
                    return
            time.sleep(0.01)
        self.fail("capture worker did not settle")

    def runs(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT trigger, outcome FROM memory_capture_runs WHERE conversation_id = ? "
                "ORDER BY started_at, rowid", (self.cid,)).fetchall()

    def test_folded_turns_too_short_alone_are_captured_once_the_session_grows(self) -> None:
        said = self.say("user", "We use uv.")
        self.say("assistant", "Noted.")
        self.say("user", "Ok.")  # folds "We use uv.": the session is too short
        self.say("assistant", "Sure.")  # folds "Noted.": still too short
        self.model.replies.append(json.dumps({"candidates": [
            {"kind": "fact", "text": "We use uv.", "subject_key": "build-tool", "confidence": 0.9,
             "evidence_turn_ids": [said.id]}]}))
        self.say("user", "Now the long part." + PAD)  # folds "Ok.": enough now
        self.assertEqual([("precompact", "skipped:too_short")] * 2 + [("precompact", "ok")], self.runs())
        self.assertIn("We use uv.", self.model.prompts[0])
        facts = {view.record.text: view.status for view in self.store.list_records(MemoryRecordScope(self.alpha))}
        self.assertEqual({"We use uv.": MemoryRecordStatus.APPROVED}, facts)
        # Never twice: SessionEnd sees only the raw remainder.
        self.app.conversation_left(self.cid)
        self.settle()
        self.assertEqual(("session_end", "ok"), self.runs()[-1])
        self.assertEqual(2, len(self.model.prompts))
        self.assertNotIn("We use uv.", self.model.prompts[-1])

    def test_a_reply_only_fold_rides_into_session_end_once(self) -> None:
        self.say("user", "First long message." + PAD)
        self.say("assistant", "REPLY-ONE")
        self.say("user", "Second long message." + PAD)  # folds the first: ok
        self.say("assistant", "REPLY-TWO")  # folds REPLY-ONE alone: skipped:empty
        self.app.conversation_left(self.cid)
        self.settle()
        self.assertEqual([("precompact", "ok"), ("precompact", "skipped:empty"), ("session_end", "ok")],
                         self.runs())
        self.assertIn("REPLY-ONE", self.model.prompts[-1])
        self.app.conversation_left(self.cid)  # nothing new: no second run
        self.settle()
        self.assertEqual(3, len(self.runs()))

    def test_a_duplicate_session_end_keeps_the_carry(self) -> None:
        # B7: leave, return, leave with no new turn repeats the skipped
        # SessionEnd ("duplicate"); the folded turn must keep riding.
        said = self.say("user", "We use uv.")
        self.say("assistant", "Noted.")
        self.say("user", "Ok.")  # folds "We use uv.": too short
        for _ in range(2):
            self.app.conversation_left(self.cid)
            self.settle()
            self.assertEqual([said.id], [turn.id for turn in self.app.memory_triggers._carry[self.cid]])
        self.say("user", "Now the long part." + PAD)  # folds "Noted.": enough now
        self.assertEqual([("precompact", "skipped:too_short"), ("session_end", "skipped:too_short"),
                          ("precompact", "ok")], self.runs())
        self.assertEqual(1, len(self.model.prompts))
        self.assertIn("We use uv.", self.model.prompts[0])


def turns(first, last):
    return tuple(SimpleNamespace(seq=seq) for seq in range(first, last + 1))


class WithCarryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.triggers = MemoryTriggers()

    def seqs(self, batch):
        return [turn.seq for turn in batch]

    def test_bounded_by_cores_cap_and_the_new_turns_are_never_cut(self) -> None:
        self.triggers._carry["c"] = turns(1, RAW_TURNS_MAX)
        batch, new = self.triggers._with_carry("c", turns(RAW_TURNS_MAX + 1, RAW_TURNS_MAX + 10))
        self.assertEqual(RAW_TURNS_MAX, len(batch))
        self.assertEqual(list(range(11, RAW_TURNS_MAX + 11)), self.seqs(batch))
        self.assertEqual(new, batch[-10:])

    def test_a_reoffered_batch_is_not_new_and_a_gap_drops_the_carry(self) -> None:
        self.triggers._carry["c"] = turns(1, 3)
        self.assertEqual((), self.triggers._with_carry("c", turns(2, 3))[1])
        self.assertEqual([1, 2, 3, 4], self.seqs(self.triggers._with_carry("c", turns(3, 4))[0]))
        self.assertEqual([6, 7], self.seqs(self.triggers._with_carry("c", turns(6, 7))[0]))

    def test_only_a_run_that_wrote_nothing_keeps_it_riding(self) -> None:
        for outcome, kept in (("skipped:too_short", True), ("skipped:empty", True), ("provider_error", True),
                              ("run_store_error", True), ("ok", False), ("skipped:temporary", False),
                              ("duplicate", False), ("aborted", False), ("internal_error", False)):
            self.triggers._carry_on("c", turns(1, 2), SimpleNamespace(outcome=outcome))
            self.assertEqual(kept, "c" in self.triggers._carry, outcome)
        # B7: a duplicate keeps a live carry (it repeats a skipped run).
        self.triggers._carry["c"] = turns(1, 2)
        self.triggers._carry_on("c", turns(1, 2), SimpleNamespace(outcome="duplicate"))
        self.assertEqual([1, 2], self.seqs(self.triggers._carry["c"]))


if __name__ == "__main__":
    unittest.main()
