"""DFRESUME: after Stop cuts a text reply the composer key resumes it as ONE new request."""

from __future__ import annotations

from PySide6.QtCore import QMetaObject

from jarvis.ui.i18n import QT_TRANSLATE_NOOP, tx
from tests.test_dfreply import chat
from tests import test_ui
from tests.test_ui import StreamingModel, UiTestCase, pump

PREVIEWS = ("Kısmi", "Kısmi yanıt")
CONTINUE = QT_TRANSLATE_NOOP(
    "Bridge", "Continue your previous answer exactly where it stopped, without repeating what was already written.")


class ResumeTests(UiTestCase):
    item = test_ui.PresentationTests.item
    window = test_ui.PresentationTests.window

    def cut(self, *answers):
        """A bridge whose first reply is cut by Stop at "Kısmi yanıt"; later runs stream unless re-armed."""
        self.stop = {"on": True}
        holder = {}

        def during(text):
            if self.stop["on"] and text == "Kısmi yanıt":
                self.stop["on"] = False
                holder["stopped"] = holder["bridge"].cancelText()
        self.model = StreamingModel(previews=PREVIEWS, during=during)
        bridge = self.bridge(*answers)
        holder["bridge"] = bridge
        self.holder = holder
        self.assertTrue(bridge.submitText("Anlat."))
        return bridge

    def test_stop_on_text_offers_resume_for_the_kept_reply(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"))
        self.assertTrue(self.holder["stopped"])
        self.assertEqual(["user", "jarvis", "notice"], self.kinds(bridge))
        self.assertIs(True, bridge.feed[1]["interrupted"])
        self.assertTrue(bridge.canResume)

    def test_resume_sends_one_request_through_the_normal_path_carrying_the_partial(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"), chat("ve devamı."))
        self.assertTrue(bridge.resumeText())
        self.assertEqual(2, len(self.model.calls))
        conversation = self.model.calls[1].context["conversation"]
        self.assertTrue(any(turn["role"] == "assistant" and "Kısmi yanıt" in turn["text"] for turn in conversation))
        self.assertEqual(["user", "jarvis", "notice", "user", "jarvis"], self.kinds(bridge))
        self.assertEqual(tx("Bridge", CONTINUE), bridge.feed[3]["text"])
        self.assertEqual("ve devamı.", bridge.feed[4]["text"])
        self.assertIs(True, bridge.feed[4].get("continuation"))
        self.assertNotIn("continuation", bridge.feed[1])
        self.assertFalse(bridge.canResume)                                  # the continuation finished: nothing to resume

    def test_double_press_sends_once(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"), chat("ve devamı."), chat("fazla"))
        self.assertTrue(bridge.resumeText())
        self.assertFalse(bridge.resumeText())
        self.assertEqual(2, len(self.model.calls))

    def test_stop_resume_stop_keeps_cycling(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"), chat("Kısmi yanıt ikinci"), chat("son."))
        self.stop["on"] = True                                              # the continuation is cut again
        self.assertTrue(bridge.resumeText())
        self.assertTrue(self.holder["stopped"])
        self.assertTrue(bridge.canResume)
        self.assertIs(True, bridge.feed[-2]["interrupted"])
        self.assertTrue(bridge.resumeText())
        self.assertEqual(3, len(self.model.calls))
        self.assertEqual("son.", bridge.feed[-1]["text"])

    def test_a_stop_that_cuts_nothing_or_a_task_offers_no_resume(self) -> None:
        bridge = self.bridge()
        self.assertFalse(bridge.cancelText())                               # nothing running
        self.assertFalse(bridge.canResume)
        # A task run: cancelText answers False (the Composer falls back to cancelTask) and nothing is armed.
        holder = {}

        def during(text):
            if text == "Kısmi yanıt":
                holder["bridge"].backend.cancel_text = lambda request_id: False
                holder["stopped"] = holder["bridge"].cancelText()
        self.model = StreamingModel(previews=PREVIEWS, during=during)
        bridge = self.bridge(chat("Tam yanıt."))
        holder["bridge"] = bridge
        self.assertTrue(bridge.submitText("Anlat."))
        self.assertFalse(holder["stopped"])
        self.assertFalse(bridge.canResume)

    def test_resume_expires_on_typing(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"))
        bridge.setDraftText("   ")
        self.assertTrue(bridge.canResume)                                   # blank is not typing
        bridge.setDraftText("x")
        self.assertFalse(bridge.canResume)
        bridge.setDraftText("")
        self.assertFalse(bridge.canResume)

    def test_staged_context_makes_the_key_send_not_resume(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"))
        self.assertTrue(bridge.quoteMessage(bridge.feed[1]["id"]))
        self.assertFalse(bridge.canResume)                                  # the quote is the user's next message
        self.assertFalse(bridge.resumeText())
        self.assertEqual(1, len(self.model.calls))
        self.assertEqual(1, len(bridge._groups))                            # nothing sent, nothing cleared

    def test_resume_expires_on_sending_anything(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"), chat("Başka."))
        self.assertTrue(bridge.submitText("Başka bir şey."))
        self.assertFalse(bridge.canResume)
        self.assertFalse(bridge.resumeText())
        self.assertEqual(2, len(self.model.calls))

    def test_resume_expires_on_switching_conversation(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"))
        a = bridge.conversationId
        store = bridge._conversation_db()
        b = store.create_conversation().id
        self.assertTrue(bridge.openConversation(b))
        self.assertFalse(bridge.canResume)
        self.assertTrue(bridge.openConversation(a))
        self.assertFalse(bridge.canResume)                                  # coming back does not revive it

    def test_resume_expires_when_the_reply_is_no_longer_last(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"))
        bridge._say("jarvis", "Sonraki yanıt.")
        self.assertFalse(bridge.canResume)

    def test_resume_expires_on_restart(self) -> None:
        self.cut(chat("Kısmi yanıt ve devamı"))
        again = self.bridge()
        self.assertFalse(again.canResume)

    def test_the_key_resumes_only_while_the_composer_is_empty(self) -> None:
        bridge = self.cut(chat("Kısmi yanıt ve devamı"), chat("Yazılan."))
        root = self.window(bridge)
        send, glyph = self.item(root, "composerSend"), self.item(root, "composerResumeGlyph")
        self.assertEqual((True, True, True), (send.property("resuming"), send.property("enabled"), glyph.isVisible()))
        self.item(root, "composerInput").setProperty("text", "Yazılan")
        pump(50)
        self.assertEqual((False, False), (send.property("resuming"), glyph.isVisible()))
        self.assertFalse(bridge.canResume)                                  # typing expired it
        QMetaObject.invokeMethod(send, "act")                               # typed text wins: Send, not Resume
        pump(50)
        self.assertEqual(2, len(self.model.calls))
        self.assertEqual(["Yazılan"], [entry["text"] for entry in bridge.feed if entry["kind"] == "user"][1:])
