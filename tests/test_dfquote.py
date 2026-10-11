"""DFQUOTE (dogfood, Salih 2026-10-12, D-2 item 3): one text selection at a time.

* B  Starting a selection in one message - or in the composer - clears the
     selection in every other message: at most one message holds a selection,
     so Ctrl+C copies only the current one and "Quote" at the selection still
     works on the one that is left. In en-US and tr-TR.

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


class OneSelectionTests(QuotingCase):
    language = "en-US"

    def window(self, bridge):
        root = super().window(bridge)
        if self.language != "en-US":
            from jarvis.ui.i18n import localization
            self.addCleanup(lambda: localization(_APP).apply("en-US"))
            bridge.setLanguage(self.language)
            pump(200)
        return root

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

    def select_in(self, edit, start, end):
        QMetaObject.invokeMethod(edit, "select", Q_ARG(int, start), Q_ARG(int, end))
        pump(80)

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



if __name__ == "__main__":
    unittest.main()
