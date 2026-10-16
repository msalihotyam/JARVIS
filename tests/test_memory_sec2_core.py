"""SEC2CORE: Core wiring of the V2.1 security base part 2 - budgeted
injection (I18), lifetime on capture (I19), memory-off chats (I21) and
reply sources with the "no relevant memory" line (I22)."""

import json
import unittest
from unittest import mock

from jarvis import core as core_module
from jarvis.contracts import (
    MODEL_ANSWER_RESULT_KEY,
    ActionScope,
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
    OutcomeStatus,
    RetrievedContext,
    RetrievedMemoryKind,
    SessionState,
    Task,
    ToolResult,
    build_model_answer,
)
from jarvis.conversation import append_turn
from jarvis.memory import MemoryRecordApproval, SQLiteMemoryStore
from jarvis.memory_lifetime import Lifetime
from jarvis.memory_retrieval import MEMORY_INJECT_TOKEN_BUDGET
from jarvis.registry import ToolRegistry
from tests.helpers import DummyConnector, isolated_settings, open_core

SESSION_END = MemoryRecordOrigin.CAPTURE_SESSION_END
FILLER = " ".join(["the release checklist keeps every reviewer informed about staging"] * 6)
TURNS = (
    "We keep the staging server warm for now while the new cluster is being built by the infra team.",
    "The billing service always writes its audit log to the shared volume before any nightly backup job.",
    "Please remember both of these points, they matter for how the team operates this quarter.",
)
TRANSIENT = "We keep the staging server warm for now while the new cluster is built."
LASTING = "The billing service writes its audit log to the shared volume before backups."


class FakeModel:
    name = "fake-capture"
    provider = "fake"

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def complete(self, task, session):
        self.calls += 1
        return ToolResult(outcome=OutcomeStatus.SUCCESS, summary="ok",
                          data={MODEL_ANSWER_RESULT_KEY: build_model_answer(self.reply)})


def block_tokens(context):
    return core_module._MEMORY_BLOCK_HEADER_TOKENS + sum(
        core_module._memory_line_tokens(item) for item in context.items
    )


class Sec2CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = isolated_settings(self)
        self.store = SQLiteMemoryStore(settings.memory_database)
        registry = ToolRegistry()
        registry.register(DummyConnector())
        self.core = open_core(registry, self.store, settings, destination_policy=frozenset())
        self.alpha = self.store.create_project("Alpha").id
        self.conversation = self.store.create_conversation(self.alpha).id

    def task(self, objective="deploy pipeline rollout"):
        return Task(objective=objective, capability="chat", scope=ActionScope(action="ask"))

    def approve(self, record_id):
        self.store.approve_record(record_id, MemoryRecordApproval(self.alpha, record_id))
        return record_id

    def remember(self, kind, text, subject):
        view = self.store.insert_explicit_user_record(self.alpha, kind, "internal", text, subject)
        if view.status is MemoryRecordStatus.PENDING:  # RULE/DECISION wait for approval
            self.approve(view.record.record_id)
        return view.record.record_id

    def off(self, conversation, off=True):
        self.store.set_conversation_memory_off(conversation, off, MemoryRecordActor.USER)

    def turn(self, conversation, text="answer"):
        ids = []

        def change(state):
            state, turn = append_turn(state, role="assistant", kind="reply", text=text,
                                      message=text, key=f"reply-{len(state.turns)}")
            ids.append(turn.id)
            return state

        self.store.update_conversation_state(conversation, change)
        return ids[0]

    # I18 -----------------------------------------------------------------

    def test_forty_long_records_stay_under_budget_rules_first(self) -> None:
        # The FACT matches three query words and outranks the RULEs (two
        # words); kind priority still puts every RULE first.
        self.remember("fact", f"deploy pipeline rollout {FILLER}", "fact-top")
        rules = {self.remember("rule", f"deploy rollout rule {n} {FILLER}", f"rule-{n}")
                 for n in range(4)}
        for n in range(35):
            self.remember("fact", f"deploy note {n} {FILLER}", f"fact-{n}")
        context = self.core.typed_memory_for(self.alpha, self.conversation, self.task())
        kinds = [item.kind for item in context.items]
        self.assertEqual(kinds[:4], [RetrievedMemoryKind.RULE] * 4)
        self.assertEqual({item.record_id for item in context.items[:4]}, rules)
        self.assertIn(RetrievedMemoryKind.FACT, kinds[4:])
        self.assertLessEqual(block_tokens(context), MEMORY_INJECT_TOKEN_BUDGET)

    def test_record_that_does_not_fit_is_dropped_whole(self) -> None:
        rule = self.remember("rule", f"deploy rollout rule {FILLER}", "rule-a")
        fact = self.remember("fact", f"deploy pipeline rollout {FILLER}", "fact-a")
        # Shrink the room through the reserved header: one long line fits.
        header = core_module._MEMORY_BLOCK_HEADER_TOKENS
        with mock.patch.object(
            core_module, "_MEMORY_BLOCK_HEADER_TOKENS", MEMORY_INJECT_TOKEN_BUDGET - 250
        ):
            context = self.core.typed_memory_for(self.alpha, self.conversation, self.task())
        self.assertEqual([item.record_id for item in context.items], [rule])
        self.assertTrue(context.truncated)
        self.assertNotIn(fact, {item.record_id for item in context.items})
        self.assertLessEqual(
            MEMORY_INJECT_TOKEN_BUDGET - 250 + sum(map(core_module._memory_line_tokens, context.items)),
            MEMORY_INJECT_TOKEN_BUDGET,
        )
        self.assertEqual(core_module._MEMORY_BLOCK_HEADER_TOKENS, header)

    # I21 -----------------------------------------------------------------

    def test_memory_off_conversation_makes_no_retrieval_call(self) -> None:
        self.remember("rule", "deploy rollout happens on fridays", "rule-a")
        self.off(self.conversation)
        with mock.patch.object(self.store, "search_records", wraps=self.store.search_records) as search, \
                mock.patch.object(self.store, "list_records", wraps=self.store.list_records) as listing:
            context = self.core.typed_memory_for(self.alpha, self.conversation, self.task())
            session = SessionState(objective="x", project_id=self.alpha)
            legacy = RetrievedContext(items=(), candidate_count=3)
            run_context = self.core._with_typed_memory(
                session, self.task(), legacy, conversation_id=self.conversation
            )
        search.assert_not_called()
        listing.assert_not_called()
        self.assertEqual(context, RetrievedContext())
        self.assertEqual(run_context, RetrievedContext())
        # Nothing kept for the reply either.
        self.assertEqual(
            self.core.record_reply_sources(self.conversation, self.turn(self.conversation), "x"),
            "unknown",
        )

    def capture_model(self, conversation, *texts):
        ids = []

        def change(state):
            for index, text in enumerate(TURNS):
                state, turn = append_turn(state, role="user", kind="user", text=text,
                                          message=text, key=f"u{index}")
                ids.append(turn.id)
            return state

        self.store.update_conversation_state(conversation, change)
        evidence = {TRANSIENT: ids[0], LASTING: ids[1]}
        return FakeModel(json.dumps({"candidates": [
            {"kind": "fact", "text": text, "subject_key": f"s{n}", "confidence": 0.9,
             "evidence_turn_ids": [evidence[text]]}
            for n, text in enumerate(texts)
        ]}))

    def test_memory_off_and_ever_off_conversations_are_never_captured(self) -> None:
        for flips in ((True,), (True, False)):
            conversation = self.store.create_conversation(self.alpha).id
            model = self.capture_model(conversation, LASTING)
            for off in flips:
                self.off(conversation, off)
            result = self.core.capture_memory(conversation, SESSION_END, model)
            self.assertEqual(result.outcome, "skipped:memory_off")
            self.assertEqual(model.calls, 0)
        self.assertEqual(
            self.store.list_records(MemoryRecordScope(self.alpha), limit=10), []
        )

    # I19 -----------------------------------------------------------------

    def test_capture_gives_every_candidate_its_lifetime(self) -> None:
        model = self.capture_model(self.conversation, TRANSIENT, LASTING)
        result = self.core.capture_memory(self.conversation, SESSION_END, model)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(result.record_ids), 2)
        lifetimes = [self.store.record_lifetime(rid) for rid in result.record_ids]
        self.assertEqual(lifetimes, [Lifetime.SESSION, Lifetime.PERMANENT])

    def test_session_record_reaches_only_its_own_conversation(self) -> None:
        view = self.store.insert_record(
            MemoryRecord(
                kind=MemoryRecordKind.FACT,
                text="deploy rollout freeze holds for now",
                subject_key="freeze",
                scope=MemoryRecordScope(project_id=self.alpha),
                origin=SESSION_END,
                actor=MemoryRecordActor.TOOL,
                tool="memory_capture",
                confidence=0.9,
                classification=DataClassification.INTERNAL,
                source_conversation_id=self.conversation,
            ),
            lifetime=Lifetime.SESSION,
        )
        self.approve(view.record.record_id)
        other = self.store.create_conversation(self.alpha).id
        own = self.core.typed_memory_for(self.alpha, self.conversation, self.task())
        foreign = self.core.typed_memory_for(self.alpha, other, self.task())
        self.assertIn(view.record.record_id, {item.record_id for item in own.items})
        self.assertNotIn(view.record.record_id, {item.record_id for item in foreign.items})

    # I22 -----------------------------------------------------------------

    def test_reply_sources_match_the_delivered_set(self) -> None:
        rule = self.remember("rule", "deploy rollout happens on fridays", "rule-a")
        fact = self.remember("fact", "deploy pipeline uses the blue cluster", "fact-a")
        task = self.task()
        context = self.core.typed_memory_for(self.alpha, self.conversation, task)
        delivered = [item.record_id for item in context.items]
        self.assertEqual(set(delivered), {rule, fact})
        turn = self.turn(self.conversation)
        self.assertEqual(self.core.record_reply_sources(self.conversation, turn, task.id), "used")
        sources = self.store.sources_for_turn(self.conversation, turn)
        self.assertEqual(
            sources,
            [(rid, MemoryRecordKind.RULE if rid == rule else MemoryRecordKind.FACT,
              self.store.record_revision(rid)) for rid in delivered],
        )
        # Used once; another conversation never takes the entry.
        self.assertEqual(self.core.record_reply_sources(self.conversation, turn, task.id), "unknown")
        again = self.task()
        self.core.typed_memory_for(self.alpha, self.conversation, again)
        other = self.store.create_conversation(self.alpha).id
        self.assertEqual(self.core.record_reply_sources(other, self.turn(other), again.id), "unknown")
        self.assertIsNone(self.store.sources_for_turn(other, turn))

    def test_none_line_only_when_memory_is_on_and_nothing_was_delivered(self) -> None:
        task = self.task()
        context = self.core.typed_memory_for(self.alpha, self.conversation, task)
        self.assertEqual([item.summary for item in context.items], [core_module._MEMORY_NONE_LINE])
        self.assertEqual(context.items[0].classification, DataClassification.PUBLIC)
        turn = self.turn(self.conversation)
        self.assertEqual(self.core.record_reply_sources(self.conversation, turn, task.id), "none")
        self.assertEqual(self.store.sources_for_turn(self.conversation, turn), [])
        # Delivered memory: no line.
        self.remember("rule", "deploy rollout happens on fridays", "rule-a")
        delivered = self.core.typed_memory_for(self.alpha, self.conversation, self.task())
        self.assertNotIn(core_module._MEMORY_NONE_LINE, [i.summary for i in delivered.items])
        # Memory-off: no line, nothing recorded.
        quiet = self.store.create_conversation(self.alpha).id
        self.off(quiet)
        task = self.task("unrelated weather question")
        self.assertEqual(self.core.typed_memory_for(self.alpha, quiet, task), RetrievedContext())
        reply = self.turn(quiet)
        self.assertEqual(self.core.record_reply_sources(quiet, reply, task.id), "unknown")
        self.assertIsNone(self.store.sources_for_turn(quiet, reply))
        # A failed retrieval is unknown, not "no memory": no line, nothing kept.
        task = self.task()
        with mock.patch.object(self.store, "search_records", side_effect=RuntimeError("disk")):
            failed = self.core.typed_memory_for(self.alpha, self.conversation, task)
        self.assertNotIn(core_module._MEMORY_NONE_LINE, [i.summary for i in failed.items])
        reply = self.turn(self.conversation)
        self.assertEqual(self.core.record_reply_sources(self.conversation, reply, task.id), "unknown")
        self.assertIsNone(self.store.sources_for_turn(self.conversation, reply))


if __name__ == "__main__":
    unittest.main()
