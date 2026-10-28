"""PM07B: the clarify dialog wired into the real chat turn (``submit_text``).

The model is a ScriptedModel (no process, no live call); the store is the
IntentTestCase TEMP database, never ``data/jarvis.db``.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from jarvis import clarify
from jarvis.app import _CLARIFY_FAILED
from jarvis.intent import IntentStatus
from jarvis.work_ledger import RunState
from tests.test_intent import IntentTestCase, ScriptedModel, chat_proposal

T0 = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
IDEA = "Bana bir yemek tarifi uygulaması yap"
ROUND1 = [
    {"slot": "goal", "text": "Uygulamanın asıl amacı ne?"},
    {"slot": "users", "text": "Kimler kullanacak?"},
    {"slot": "platform", "text": "Hangi platformda çalışacak?"},
]


class ClarifyChatTests(IntentTestCase):
    def start(self, *answers):
        self.model = ScriptedModel(*answers)
        app = self.build(self.model)
        self.ticks = 0

        def clock():
            self.ticks += 1
            return T0 + timedelta(minutes=self.ticks)

        app.clarify_clock = clock
        self.conversation = self.memory.create_conversation()
        return app

    def say(self, text):
        payload = self.app.submit_text(text, conversation_id=self.conversation.id).payload
        self.assertIsNone(payload["run"])
        return payload["intent"]

    def runs(self):
        return self.memory.work_runs_open()

    def to_questions(self):
        self.assertEqual(self.say(IDEA)["reply"], clarify.CONFIRM_PROMPT)
        self.assertEqual(self.runs(), [])
        reply = self.say("evet")["reply"]
        self.assertIn("1. Uygulamanın asıl amacı ne?", reply)
        [run] = self.runs()
        self.assertIs(run.state, RunState.CLARIFYING)
        return run

    def test_project_idea_gets_the_confirm_prompt_from_the_chat_entry(self):
        self.start()
        intent = self.say(IDEA)
        self.assertEqual(IntentStatus.CONVERSATION.value, intent["status"])
        self.assertEqual(clarify.CONFIRM_PROMPT, intent["reply"])
        self.assertEqual([], self.model.calls)
        self.assertEqual([], self.runs())

    def test_yes_asks_questions_through_the_intent_model_path(self):
        self.start(json.dumps(ROUND1))
        self.to_questions()
        [call] = self.model.calls
        self.assertEqual("questions", json.loads(call.objective)["clarify"])

    def test_ordinary_chat_is_untouched(self):
        self.start(chat_proposal("Merhaba!"), chat_proposal("Tamam."))
        self.assertEqual("Merhaba!", self.say("merhaba")["reply"])
        # A bare cancel word with no dialog open is ordinary chat too.
        self.assertEqual("Tamam.", self.say("iptal")["reply"])
        self.assertEqual(2, len(self.model.calls))
        self.assertEqual([], self.runs())

    def test_cancel_ends_the_run_and_the_next_turn_is_chat(self):
        self.start(json.dumps(ROUND1), chat_proposal("Merhaba!"))
        run = self.to_questions()
        self.assertEqual("Peki, projeyi iptal ettim.", self.say("İptal!")["reply"])
        self.assertEqual([], self.runs())
        self.assertIs(RunState.CANCELLED, self.memory.latest_work_run(run.run_id).state)
        self.assertEqual("Merhaba!", self.say("merhaba")["reply"])
        self.assertEqual(2, len(self.model.calls))

    def test_cancel_at_the_confirm_prompt_starts_nothing(self):
        self.start(chat_proposal("Tamam."))
        self.say(IDEA)
        self.assertEqual("Peki, projeyi başlatmıyorum.", self.say("boş ver")["reply"])
        self.assertEqual("Tamam.", self.say("evet")["reply"])
        self.assertEqual([], self.runs())

    def test_bad_model_questions_create_no_run(self):
        self.start("bu json değil")
        self.say(IDEA)
        self.assertTrue(self.say("evet")["reply"].startswith("Sorular hazırlanamadı"))
        self.assertEqual([], self.runs())

    def test_clarify_failure_fails_closed_without_the_old_path(self):
        self.start(chat_proposal("never"))
        with mock.patch.object(self.app.core, "clarify_turn", side_effect=RuntimeError("boom")):
            intent = self.say(IDEA)
        self.assertEqual(_CLARIFY_FAILED, intent["reply"])
        self.assertNotIn("boom", json.dumps(intent))
        self.assertEqual([], self.model.calls)
        self.assertEqual([], self.runs())
        self.assertEqual(0, self.table_count("tasks"))

    def test_clarify_failure_mid_run_does_not_advance_it(self):
        self.start(json.dumps(ROUND1))
        run = self.to_questions()
        before = self.memory.work_events(run.run_id)
        with mock.patch.object(self.app.core, "clarify_turn", side_effect=RuntimeError):
            self.assertEqual(_CLARIFY_FAILED, self.say("tarif saklamak")["reply"])
        self.assertEqual(before, self.memory.work_events(run.run_id))
        self.assertEqual(1, len(self.model.calls))

    # One test per entry point: only the chat turn carries a conversation's text.

    def test_entry_submit_text_calls_clarify_turn(self):
        self.start()
        with mock.patch.object(self.app.core, "clarify_turn", return_value="x") as hook:
            self.assertEqual("x", self.say("merhaba")["reply"])
        [call] = hook.call_args_list
        self.assertEqual((self.conversation.id, "merhaba"), call.args)
        self.assertEqual(T0 + timedelta(minutes=1), call.kwargs["at"])

    def test_entry_unscoped_submit_text_skips_clarify(self):
        self.start(chat_proposal("Merhaba!"))
        with mock.patch.object(self.app.core, "clarify_turn") as hook:
            self.app.submit_text(IDEA)
        hook.assert_not_called()

    def test_entry_run_json_skips_clarify(self):
        self.start()
        with mock.patch.object(self.app.core, "clarify_turn") as hook:
            self.app.run(json.dumps({"objective": IDEA}))
        hook.assert_not_called()

    def test_entry_project_folder_switch_skips_clarify(self):
        self.start()
        with mock.patch.object(self.app.core, "clarify_turn") as hook:
            self.app.project_folder_switch("s", "t", "r", "f")
        hook.assert_not_called()


class ParseCancelTests(unittest.TestCase):
    """F1: one case per variant of the closed cancel set."""

    def test_case_and_dotted_i(self):
        for text in ("İPTAL", "IPTAL", "Vazgeç", "BOŞVER"):
            self.assertTrue(clarify.parse_cancel(text), text)

    def test_punctuation_and_spacing(self):
        for text in ("iptal!", " vazgeç. ", "boş  ver"):
            self.assertTrue(clarify.parse_cancel(text), text)
        for text in ("iptal et?", "cancel?", "iptal?!"):
            self.assertFalse(clarify.parse_cancel(text), text)

    def test_ascii_contractions(self):
        for text in ("vazgec", "bosver", "bos ver", "cancel"):
            self.assertTrue(clarify.parse_cancel(text), text)

    def test_fullwidth_folds_by_nfkc(self):
        self.assertTrue(clarify.parse_cancel("ｉｐｔａｌ"))

    def test_negation_and_suffixes_are_not_cancel(self):
        for text in ("iptal etme", "vazgeçmedim", "iptalden önce", "vazgeç ama sonra devam"):
            self.assertFalse(clarify.parse_cancel(text), text)

    def test_homoglyph_and_hidden_chars_are_not_cancel(self):
        for text in ("іптал", "іptal", "ip​tal"):
            self.assertFalse(clarify.parse_cancel(text), text)

    def test_empty_and_raw_values(self):
        for value in ("", "   ", "!!", None, 1, b"iptal"):
            self.assertFalse(clarify.parse_cancel(value), value)


if __name__ == "__main__":
    unittest.main()
