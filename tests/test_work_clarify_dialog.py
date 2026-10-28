"""PM07: the clarify dialog in chat, driven by a fake model proposer."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis import clarify
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.work_ledger import EventKind, RunState, WorkLedgerError

T0 = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
CONV = "conv-1"
IDEA = "Bana bir yemek tarifi uygulaması yap"

ROUND1 = [
    {"slot": "goal", "text": "Uygulamanın asıl amacı ne?"},
    {"slot": "users", "text": "Kimler kullanacak?"},
    {"slot": "platform", "text": "Hangi platformda çalışacak?"},
]
PART1 = {"goal": "tarif saklamak", "users": "ben", "platform": "Windows", "target_location": "C:/tarif"}
REST = {
    "definition_of_done": "tarif eklenip aranabiliyor",
    "acceptance_examples": [{"text": "tarif ekle", "check": "test_add"}],
    "constraints": "yok", "out_of_scope": "bulut", "data_sensitivity": "düşük",
    "outgoing_or_money": False,
}


class FakeModel:
    """Scripted proposals per kind; records every call."""

    def __init__(self, **script):
        self.script = {kind: list(items) for kind, items in script.items()}
        self.calls = []

    def __call__(self, kind, context):
        self.calls.append((kind, json.loads(json.dumps(context))))
        return self.script[kind].pop(0)


def round2(brief_values):
    filled = clarify.fill_brief(clarify.ProjectBrief(), brief_values)
    return [{"slot": slot, "text": f"{slot}?"} for slot in clarify.empty_slots(filled)]


class ClarifyDialogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.memory = SQLiteMemoryStore(self.dir / "x.db")
        self.core = AgentCore(ToolRegistry(), self.memory)
        self.turn = 0

    def say(self, text, model, *, turn=None, boot="boot-1"):
        if turn is None:
            self.turn += 1
            turn = self.turn
        return self.core.clarify_turn(
            CONV, text, turn=turn, boot_id=boot, at=T0 + timedelta(minutes=turn), propose=model,
        )

    def runs(self):
        return self.memory.work_runs_open()

    def kinds(self, run_id):
        return [event.kind for event in self.memory.work_events(run_id)]

    def walk_to_readback(self, model):
        self.assertEqual(self.say(IDEA, model), clarify.CONFIRM_PROMPT)
        self.assertEqual(self.runs(), [])
        reply = self.say("evet", model)
        self.assertIn("1. Uygulamanın asıl amacı ne?\n2. Kimler kullanacak?", reply)
        self.assertEqual([run.state for run in self.runs()], [RunState.CLARIFYING])
        reply = self.say("tarif saklamak, ben, Windows", model)
        self.assertTrue(reply.startswith("Birkaç sorum daha var:\n1. "))
        reply = self.say("geri kalanlar", model)
        self.assertTrue(reply.startswith("Anladığım kadarıyla:"))
        return self.runs()[0]

    def model(self, **extra):
        script = {
            "questions": [ROUND1, round2(PART1)], "answers": [PART1, REST],
        }
        script.update(extra)
        return FakeModel(**script)

    def test_full_walk_reaches_planning(self):
        model = self.model()
        run = self.walk_to_readback(model)
        self.assertEqual(self.say("evet", model), "Onaylandı; proje planlama aşamasına geçti.")
        latest = self.memory.latest_work_run(run.run_id)
        self.assertIs(latest.state, RunState.PLANNING)
        kinds = self.kinds(run.run_id)
        self.assertEqual(kinds.count(EventKind.QUESTIONS_READY), 2)
        self.assertEqual(kinds.count(EventKind.READBACK_READY), 1)
        # Follow-ups asked only the slots still empty after the first answers.
        second = model.calls[2]
        self.assertEqual(second[0], "questions")
        self.assertEqual(second[1]["round"], 2)
        self.assertNotIn("goal", second[1]["empty"])

    def test_ordinary_chat_creates_no_run(self):
        model = FakeModel()
        self.assertIsNone(self.say("Bugün hava nasıl?", model))
        self.assertIsNone(self.say("evet", model))
        self.assertEqual(self.runs(), [])
        self.assertEqual(model.calls, [])

    def test_no_to_confirm_creates_no_run(self):
        model = FakeModel()
        self.assertEqual(self.say(IDEA, model), clarify.CONFIRM_PROMPT)
        self.assertEqual(self.say("hayır", model), "Peki, projeyi başlatmıyorum.")
        self.assertIsNone(self.say("evet", model))
        self.assertEqual(self.runs(), [])

    def test_same_turn_confirmation_fails(self):
        model = self.model()
        run = self.walk_to_readback(model)
        reply = self.say("evet", model, turn=self.turn)
        self.assertTrue(reply.startswith("Onay, özetten sonraki ayrı bir mesajla gelmeli"))
        self.assertIs(self.memory.latest_work_run(run.run_id).state, RunState.CLARIFYING)

    def test_yes_inside_answers_turn_does_not_confirm(self):
        model = self.model(answers=[PART1, {**REST, "constraints": "evet"}])
        run = self.walk_to_readback(model)
        self.assertIs(self.memory.latest_work_run(run.run_id).state, RunState.CLARIFYING)

    def test_correction_returns_to_questions(self):
        model = self.model(
            correction=[{"platform": "Linux"}], answers=[PART1, REST, {"platform": "Linux"}],
        )
        model.script["questions"].append([{"slot": "platform", "text": "Platform hangisi?"}])
        run = self.walk_to_readback(model)
        reply = self.say("platform yanlış", model)
        self.assertEqual(reply, "Birkaç sorum daha var:\n1. Platform hangisi?")
        state = self.core._clarify_active(CONV, "boot-1")[1]
        self.assertIsNone(state.readback)
        self.assertEqual(clarify.empty_slots(state.brief), ("platform",))
        self.assertIs(self.memory.latest_work_run(run.run_id).state, RunState.CLARIFYING)
        reply = self.say("Linux", model)
        self.assertTrue(reply.startswith("Anladığım kadarıyla:"))
        self.assertIn("Linux", reply)

    def test_rejected_readback_cannot_be_confirmed_after_failed_questions(self):
        # The scripted model has no further questions: it raises IndexError.
        model = self.model(correction=[{"platform": "Linux"}])
        run = self.walk_to_readback(model)
        self.assertTrue(self.say("platform yanlış", model).startswith("Sorular hazırlanamadı"))
        self.assertIsNone(self.core._clarify_active(CONV, "boot-1")[1].readback)
        self.assertTrue(self.say("evet", model).startswith("Yanıtınızı işleyemedim"))
        self.assertIs(self.memory.latest_work_run(run.run_id).state, RunState.CLARIFYING)

    def assert_readback_reissued(self, reply, run):
        self.assertTrue(reply.startswith("Düzeltmeyi anlayamadım.\nAnladığım kadarıyla:"))
        state = self.core._clarify_active(CONV, "boot-1")[1]
        self.assertEqual(state.readback.shown_turn, self.turn)
        # A yes in the very turn that re-issued the read-back cannot confirm it.
        reply = self.say("evet", model=FakeModel(), turn=self.turn)
        self.assertTrue(reply.startswith("Onay, özetten sonraki ayrı bir mesajla gelmeli"))
        self.assertIs(self.memory.latest_work_run(run.run_id).state, RunState.CLARIFYING)
        self.assertEqual(self.say("evet", FakeModel()), "Onaylandı; proje planlama aşamasına geçti.")
        self.assertIs(self.memory.latest_work_run(run.run_id).state, RunState.PLANNING)

    def test_failed_correction_then_yes_needs_reissued_readback(self):
        # A-C9: the scripted model has no correction: it raises IndexError.
        model = self.model()
        run = self.walk_to_readback(model)
        self.assert_readback_reissued(self.say("platform yanlış", model), run)

    def test_invalid_correction_then_yes_needs_reissued_readback(self):
        model = self.model(correction=[{"secret": "x"}])
        run = self.walk_to_readback(model)
        self.assert_readback_reissued(self.say("şunu değiştir", model), run)

    def test_empty_correction_then_yes_needs_reissued_readback(self):
        model = self.model(correction=[{}])
        run = self.walk_to_readback(model)
        self.assert_readback_reissued(self.say("bir şey değişsin", model), run)

    def test_notify_seq_conflict_keeps_reply_and_state_consistent(self):
        # A-C9: every notification write loses its seq; the saved state and
        # the reply still agree and the re-issued read-back stays confirmable.
        model = self.model()
        run = self.walk_to_readback(model)
        before = self.kinds(run.run_id)
        with mock.patch.object(
            self.memory, "append_work_event", side_effect=WorkLedgerError("seq taken"),
        ) as write:
            reply = self.say("platform yanlış", model)
        self.assertEqual(write.call_count, 2)
        # Only the re-issued state was saved; no notification got through.
        self.assertEqual(self.kinds(run.run_id), before + [EventKind.RUN_STATE])
        self.assert_readback_reissued(reply, run)

    def drive_to_needs_input(self):
        partial = [{"goal": "x"}, {"users": "ben"}, {"platform": "Windows"}]
        model = FakeModel(
            questions=[ROUND1, round2(partial[0]), round2({**partial[0], **partial[1]})],
            answers=partial,
        )
        self.say(IDEA, model)
        self.say("evet", model)
        self.say("x", model)
        self.say("ben", model)
        reply = self.say("Windows", model)
        self.assertTrue(reply.startswith("Soru sınırına ulaşıldı"))
        run = self.runs()[0]
        self.assertIs(run.state, RunState.NEEDS_INPUT)
        return run

    def test_needs_input_run_cancels_from_chat(self):
        run = self.drive_to_needs_input()
        self.assertEqual(self.say("iptal", FakeModel()), "Peki, projeyi iptal ettim.")
        self.assertIs(self.memory.latest_work_run(run.run_id).state, RunState.CANCELLED)
        self.assertEqual(self.runs(), [])

    def test_needs_input_run_other_text_is_ordinary_chat(self):
        run = self.drive_to_needs_input()
        before = self.memory.work_events(run.run_id)
        model = FakeModel()
        self.assertIsNone(self.say("evet", model))
        self.assertIsNone(self.say("tarif saklamak", model))
        self.assertEqual(model.calls, [])
        self.assertEqual(self.memory.work_events(run.run_id), before)
        # Another boot never cancels it either.
        self.assertIsNone(self.say("iptal", model, boot="boot-2"))
        self.assertIs(self.memory.latest_work_run(run.run_id).state, RunState.NEEDS_INPUT)

    def test_other_boot_never_continues_run_before_sweep(self):
        model = self.model()
        self.say(IDEA, model)
        self.say("evet", model)
        run_id = self.runs()[0].run_id
        before = self.memory.work_events(run_id)
        self.assertIsNone(self.say("tarif saklamak", model, boot="boot-2"))
        self.assertEqual(self.memory.work_events(run_id), before)
        self.assertEqual(self.core.recover_project_runs("boot-2", now=T0 + timedelta(hours=1)), [run_id])

    def test_invalid_model_questions_create_no_run(self):
        model = FakeModel(questions=[[{"slot": "nope", "text": "?"}]])
        self.say(IDEA, model)
        reply = self.say("evet", model)
        self.assertTrue(reply.startswith("Sorular hazırlanamadı; proje başlatılmadı"))
        self.assertEqual(self.runs(), [])

    def test_invalid_model_answers_leave_state_unchanged(self):
        model = self.model(answers=[{"secret": "x"}])
        self.say(IDEA, model)
        self.say("evet", model)
        before = self.memory.work_events(self.runs()[0].run_id)
        self.assertTrue(self.say("cevap", model).startswith("Yanıtınızı işleyemedim"))
        self.assertEqual(self.memory.work_events(self.runs()[0].run_id), before)

    def test_nothing_starts_before_confirmed_readback(self):
        model = self.model()
        with mock.patch.object(AgentCore, "run") as run_worker, \
                mock.patch.object(AgentCore, "create_project_run") as create_plan:
            run = self.walk_to_readback(model)
            self.say("evet", model, turn=self.turn)
            run_worker.assert_not_called()
            create_plan.assert_not_called()
        self.assertEqual({kind for kind, _ in model.calls}, {"questions", "answers"})
        allowed = {EventKind.RUN_STATE, EventKind.QUESTIONS_READY, EventKind.READBACK_READY}
        self.assertTrue(set(self.kinds(run.run_id)) <= allowed)
        self.assertEqual({p.name for p in self.dir.iterdir()} - {"x.db", "x.db-wal", "x.db-shm"}, set())

    def test_restart_mid_clarify_stays_interrupted(self):
        model = self.model()
        self.say(IDEA, model)
        self.say("evet", model)
        run_id = self.runs()[0].run_id
        self.assertEqual(self.core.recover_project_runs("boot-1", now=T0 + timedelta(hours=1)), [])
        restarted = AgentCore(ToolRegistry(), self.memory)
        swept = restarted.recover_project_runs("boot-2", now=T0 + timedelta(hours=1))
        self.assertEqual(swept, [run_id])
        self.core = restarted
        self.assertIsNone(self.say("tarif saklamak", model, boot="boot-2"))
        self.assertEqual(self.say(IDEA, model, boot="boot-2"), clarify.CONFIRM_PROMPT)
        latest = self.memory.latest_work_run(run_id)
        self.assertIs(latest.state, RunState.INTERRUPTED)
        self.assertIs(latest.resume_state, RunState.CLARIFYING)


if __name__ == "__main__":
    unittest.main()
