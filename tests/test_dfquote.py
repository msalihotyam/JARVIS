"""DFQUOTE (dogfood, Salih 2026-10-12, D-2 item 3): quoting in daily use.

* B  Starting a selection in one message - or in the composer - clears the
     selection in every other message: at most one message holds a selection,
     so Ctrl+C copies only the current one and "Quote" at the selection still
     works on the one that is left.
* A  Enter in a quote card's comment field keeps the comment and closes the
     edit; Esc puts back the comment from before and closes.
* C  A clarification question can be copied and quoted, whole or in part.

All in en-US and tr-TR.

No model, network or live DB: the feed is filled through the bridge directly.
"""
from __future__ import annotations

import unittest

from tests.test_ui import _APP, pump  # noqa: F401 - _APP: the application
from tests.test_g6q1_quoting import QuotingCase

from PySide6.QtCore import QMetaObject, Q_ARG, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

FIRST = "İlk cevap: grafik yükseliyor ve maliyet düşüyor."
SECOND = "İkinci cevap: ölçümler pahalı modellere yaklaşıyor."


class LanguageCase(QuotingCase):
    language = "en-US"

    def window(self, bridge):
        root = super().window(bridge)
        if self.language != "en-US":
            from jarvis.ui.i18n import localization
            self.addCleanup(lambda: localization(_APP).apply("en-US"))
            bridge.setLanguage(self.language)
            pump(200)
        return root

    def select_in(self, edit, start, end):
        QMetaObject.invokeMethod(edit, "select", Q_ARG(int, start), Q_ARG(int, end))
        pump(80)


class OneSelectionTests(LanguageCase):
    def feed_with_two_replies(self, bridge):
        bridge._say("user", "Bu grafiği yorumla.")
        bridge._say("jarvis", FIRST, chat=True)
        bridge._say("jarvis", SECOND, chat=True)
        pump(300)

    def replies(self, root):
        found = [i for i in self.all_items(root) if i.objectName() == "feedMessage_jarvis"]
        self.assertEqual(2, len(found))
        # Shown as Markdown: told apart by their first word.
        return sorted(found, key=lambda edit: "İkinci" in edit.property("text"))

    def selected(self, root):
        """Every message in the feed that holds a selection, by its selected text."""
        return [i.property("selectedText") for i in self.all_items(root)
                if i.objectName().startswith("feedMessage_") and i.property("selectedText")]

    def test_a_selection_in_a_second_message_clears_the_first(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.feed_with_two_replies(bridge)
        first, second = self.replies(root)
        self.select_in(first, 0, 3)
        self.assertEqual(["İlk"], self.selected(root))
        self.select_in(second, 0, 6)
        # Only the newest selection stands; the first message holds none.
        self.assertEqual("", first.property("selectedText"))
        self.assertEqual(["İkinci"], self.selected(root))
        # And back: the second clears when the first is selected again.
        self.select_in(first, 4, 9)
        self.assertEqual(["cevap"], self.selected(root))

    def test_ctrl_c_copies_only_the_current_selection(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.feed_with_two_replies(bridge)
        first, second = self.replies(root)
        self.select_in(first, 0, 3)
        second.forceActiveFocus()
        self.select_in(second, 0, 6)
        QTest.keyClick(root, Qt.Key_C, Qt.ControlModifier)
        pump(80)
        self.assertEqual("İkinci", QGuiApplication.clipboard().text())
        self.assertEqual(["İkinci"], self.selected(root))

    def test_a_selection_in_the_composer_clears_the_message_selection(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.feed_with_two_replies(bridge)
        first, _ = self.replies(root)
        self.select_in(first, 0, 3)
        self.assertEqual(["İlk"], self.selected(root))
        composer = self.item(root, "composerInput")
        composer.forceActiveFocus()
        self.type(root, "taslak")
        pump(60)
        self.assertEqual(["İlk"], self.selected(root))             # typing alone selects nothing
        QTest.keyClick(root, Qt.Key_A, Qt.ControlModifier)
        pump(80)
        self.assertEqual("taslak", composer.property("selectedText"))
        self.assertEqual([], self.selected(root))
        self.assertFalse(self.item(root, "quoteTip").property("shown"))

    def test_quote_at_the_selection_takes_the_one_selection_left(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.feed_with_two_replies(bridge)
        first, second = self.replies(root)
        self.select_in(first, 0, 3)
        self.select_in(second, 0, 6)
        self.assertTrue(self.item(root, "quoteTip").property("shown"))
        self.click(root, "quoteTipButton")
        pump(200)
        self.assertEqual([("partial", "İkinci", bridge.feed[-1]["id"])],
                         [(g["reference"]["selection"], g["reference"]["text"], g["reference"]["source_id"])
                          for g in bridge.contextGroups])
        self.assertEqual([], self.selected(root))


class OneSelectionTurkishTests(OneSelectionTests):
    language = "tr-TR"


QUESTION = "Hangi dosyayı kastediyorsun: rapor mu, sunum mu?"
LABELS = {"en-US": ("Copy", "Quote"), "tr-TR": ("Kopyala", "Alıntıla")}


class NoteKeysAndClarifyTests(LanguageCase):
    """A  The comment field on a composer quote card: Enter keeps what was typed
          and closes the edit; Esc puts back the comment from before and closes.
    C  A clarification question is quotable assistant text: Copy and Quote on
       its row, and a selection in it quotes like any General Chat reply."""

    def quoted_card(self, bridge, root):
        entry = self.conversation(bridge)
        bridge.quoteSelection(entry, "pahalı modellere")
        pump(200)
        return bridge.contextGroups[0]["id"]

    def edit_note(self, root, gid, typed):
        field = self.item(root, "contextGroupNote_" + gid)
        self.click(root, "contextGroupNoteToggle_" + gid)
        self.assertTrue(field.property("activeFocus"))
        QTest.keyClick(root, Qt.Key_A, Qt.ControlModifier)
        self.type(root, typed)
        return field

    def assert_closed(self, root, gid, field, note):
        self.assertFalse(field.property("activeFocus"))
        self.assertFalse(field.parentItem().property("open"))
        self.assertEqual(note, field.property("text"))

    def test_enter_keeps_the_comment_and_closes_the_edit(self) -> None:
        for key in (Qt.Key_Return, Qt.Key_Enter):
            with self.subTest(key=key):
                bridge = self.bridge()
                root = self.window(bridge)
                gid = self.quoted_card(bridge, root)
                field = self.edit_note(root, gid, "Bunu olc")
                QTest.keyClick(root, key)
                pump(120)
                self.assert_closed(root, gid, field, "Bunu olc")
                self.assertEqual("Bunu olc", bridge.contextGroups[0]["note"])
                self.assertEqual("comment", self.item(root, "contextGroupKind_" + gid).property("kind"))
                self.assertEqual(([], ""), (self.model.calls, self.item(root, "composerInput").property("text")))

    def test_escape_puts_back_the_comment_from_before_and_closes(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        gid = self.quoted_card(bridge, root)
        self.edit_note(root, gid, "Eski yorum")
        QTest.keyClick(root, Qt.Key_Return)
        pump(120)
        field = self.edit_note(root, gid, "yeni")
        self.assertEqual("yeni", bridge.contextGroups[0]["note"])      # typed straight to the group...
        QTest.keyClick(root, Qt.Key_Escape)
        pump(120)
        self.assert_closed(root, gid, field, "Eski yorum")            # ...and Esc takes it back
        self.assertEqual("Eski yorum", bridge.contextGroups[0]["note"])
        self.assertEqual([gid], [g["id"] for g in bridge.contextGroups])  # Esc closes the edit, not the quote

    def clarification(self, bridge, root):
        bridge._say("user", "Dosyayı özetle.")
        bridge._say("clarify", QUESTION)
        pump(300)
        return bridge.feed[-1]["id"]

    def test_a_clarification_offers_copy_and_quote(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.clarification(bridge, root)
        self.assertTrue(self.item(root, "messageActions_1").property("visible"))
        self.assertEqual(LABELS[self.language],
                         (self.item(root, "messageCopy_1").property("label"), self.item(root, "messageQuote_1").property("label")))
        QMetaObject.invokeMethod(self.item(root, "messageCopy_1"), "activated")
        pump(50)
        self.assertEqual(QUESTION, QGuiApplication.clipboard().text())

    def test_quoting_a_clarification_whole_and_in_part(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        entry = self.clarification(bridge, root)
        QMetaObject.invokeMethod(self.item(root, "messageQuote_1"), "activated")
        pump(200)
        self.assertTrue(bridge.quoteSelection(entry, "rapor mu"))
        self.assertEqual([("full", QUESTION, entry), ("partial", "rapor mu", entry)],
                         [(g["reference"]["selection"], g["reference"]["text"], g["reference"]["source_id"])
                          for g in bridge.contextGroups])
        # A selection in the question offers the floating Quote like a chat reply.
        question = next(i for i in self.all_items(root) if i.objectName() == "feedMessage_clarify")
        self.select_in(question, 0, 5)
        self.assertTrue(self.item(root, "quoteTip").property("shown"))
        self.click(root, "quoteTipButton")
        pump(200)
        self.assertEqual(("partial", "Hangi", entry),
                         tuple(bridge.contextGroups[-1]["reference"][k] for k in ("selection", "text", "source_id")))


class NoteKeysAndClarifyTurkishTests(NoteKeysAndClarifyTests):
    language = "tr-TR"



if __name__ == "__main__":
    unittest.main()
