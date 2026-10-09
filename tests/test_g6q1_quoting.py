"""Gate 6G G6Q1: Quoting v2 - the card flow (roadmap §3.7, board 13 "Alıntılama").

* Q1  Selecting text offers ONE small "Quote" button at the selection; pressing
      it takes the quote and opens the quote popup there.
* Q2' The popup: the quoted words faint on top, one comment line, paperclip and
      add; Enter adds, Esc closes (a quote the popup started leaves with it);
      a pasted image belongs to that quote only, its thumbnails under the line.
* Q11 A half-selected word is taken whole.
* Q3' / Q10 One card per quote with its letter and kind ("comment" / "quote");
      the cards wrap and the card area grows UPWARD; 2+ quotes: the answer mode.
* Q9  A card (or a letter in the message) opens the detail: the whole quote,
      edit, delete.
* Q5  The source message marks each draft quote: a dotted underline and its
      letter, gone when the quote is removed or sent.
* Q8  The sent message shows each quote (letter, faint excerpt that folds,
      comment, its files), then the common message.

The model-facing groups are unchanged: these tests read them through the same
bridge API the existing quote-group tests use. No model, network or live DB.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import tests.test_ui as ui
from tests.test_ui import _APP, pump, tiny_png  # noqa: F401 - _APP: the application
from tests.helpers import settled
from tests.test_intent import proposal

from PySide6.QtCore import QMetaObject, QObject, QPointF, Q_ARG, Q_RETURN_ARG, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

REPLY = "Sol tarafında coding performansı ciddi şekilde yükselmiş; OpenAI'ın kendi ölçümlerinde pahalı modellere yaklaşıyor."


class QuotingCase(ui.SurfaceCase):
    def window(self, bridge):
        root = super().window(bridge)
        # The popup and the two detail dialogs. PySide invalidates a child wrapper
        # when the parent wrapper it was found through is collected, so the
        # parents are held for the test's life. Their contents are found in the
        # visual tree.
        self.owners = (self.feed(root), self.item(root, "composerContext"))
        self.popups = {"popup": self.owners[0].findChild(QObject, "quotePopup"),
                       "feedDetail": self.owners[0].findChild(QObject, "quoteDetail"),
                       "detail": self.owners[1].findChild(QObject, "quoteDetail")}
        self.root = root
        return root

    def part(self, key, name):
        """The shown item of that name: only an open popup's contents are visible."""
        shown = [i for i in self.all_items(self.root) if i.objectName() == name and i.isVisible()]
        self.assertEqual(1, len(shown), name)
        return shown[0]

    def conversation(self, bridge, reply=REPLY):
        bridge._say("user", "Bu grafiği yorumla.")
        bridge._say("jarvis", reply, chat=True)
        pump(300)
        return bridge.feed[-1]["id"]

    def reply(self, root):
        return [i for i in self.all_items(root) if i.objectName() == "feedMessage_jarvis"][0]

    def press(self, root, obj) -> None:
        """A real click at the centre of any item (popup and dialog contents included)."""
        centre = obj.mapToScene(QPointF(obj.width() / 2, obj.height() / 2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, centre)
        pump(260)

    def popup(self, root):
        return self.popups["popup"]

    def select(self, root, start, end):
        reply = self.reply(root)
        QMetaObject.invokeMethod(reply, "select", Q_ARG(int, start), Q_ARG(int, end))
        pump(80)
        return reply

    def quote_by_button(self, root, start, end):
        self.select(root, start, end)
        self.assertTrue(self.item(root, "quoteTip").property("shown"))
        self.click(root, "quoteTipButton")
        return self.popup(root)

    def type(self, root, text):
        for char in text:
            QTest.keyClick(root, char)
            pump(15)

    def detail(self, root):
        return self.popups["detail"]


class SelectionPopupTests(QuotingCase):
    def test_the_quote_button_opens_the_popup_and_enter_adds_the_comment(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        entry = self.conversation(bridge)
        start = REPLY.index("ciddi")
        popup = self.quote_by_button(root, start, start + len("ciddi şekilde yükselmiş"))
        # Q1: the quote is taken at once; the popup opens at the selection with its preview.
        self.assertEqual([("partial", "ciddi şekilde yükselmiş", entry)],
                         [(g["reference"]["selection"], g["reference"]["text"], g["reference"]["source_id"]) for g in bridge.contextGroups])
        self.assertTrue(popup.property("opened"))
        self.assertTrue(popup.property("fresh"))
        self.assertEqual("ciddi şekilde yükselmiş", self.part("popup", "quotePopupExcerpt").property("text"))
        self.assertFalse(self.item(root, "quoteTip").property("shown"))
        field = self.part("popup", "quoteComment")
        self.assertTrue(field.property("activeFocus"))
        self.type(root, "Bunu olc")
        QTest.keyClick(root, Qt.Key_Return)
        pump(200)
        # Enter adds: the comment is the group's note; the card now reads as a comment.
        gid = bridge.contextGroups[0]["id"]
        self.assertFalse(popup.property("visible"))
        self.assertEqual("Bunu olc", bridge.contextGroups[0]["note"])
        self.assertEqual("comment", self.item(root, "contextGroupKind_" + gid).property("kind"))
        self.assertEqual("Bunu olc", self.item(root, "contextGroupNote_" + gid).property("text"))
        self.assertEqual("", self.item(root, "composerInput").property("text"))     # nothing written into the draft
        self.assertEqual(([], False), (self.model.calls, bridge.approvalPending))   # nothing sent or authorized

    def test_adding_without_a_comment_keeps_a_plain_quote(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.conversation(bridge)
        popup = self.quote_by_button(root, 0, 3)
        self.press(root, self.part("popup", "quoteAdd"))
        gid = bridge.contextGroups[0]["id"]
        self.assertFalse(popup.property("visible"))
        self.assertEqual(("Sol", ""), (bridge.contextGroups[0]["reference"]["text"], bridge.contextGroups[0]["note"]))
        self.assertEqual("quote", self.item(root, "contextGroupKind_" + gid).property("kind"))
        self.assertTrue(self.item(root, "contextGroupText_" + gid).isVisible())

    def test_esc_closes_and_a_quote_the_popup_started_leaves_with_it(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.conversation(bridge)
        popup = self.quote_by_button(root, 0, 3)
        self.assertEqual(1, len(bridge.contextGroups))
        QTest.keyClick(root, Qt.Key_Escape)
        settled(300, lambda: (
            self.assertFalse(popup.property("visible")),
            self.assertEqual([], bridge.contextGroups),
        ))

    def test_a_pasted_image_belongs_to_that_quote_only_and_shows_under_the_line(self) -> None:
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QImage
        bridge = self.bridge()
        root = self.window(bridge)
        self.conversation(bridge)
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(0xFF2255CC)
        picture = QMimeData()
        picture.setImageData(image)
        bridge.clipboard_data = lambda: picture
        popup = self.quote_by_button(root, 0, 3)
        gid = bridge.contextGroups[0]["id"]
        QTest.keyClick(root, Qt.Key_V, Qt.ControlModifier)
        pump(200)
        self.assertEqual((["pasted-image.png"], []), ([f["name"] for f in bridge.groupAttachments(gid)], bridge.globalAttachments))
        self.assertEqual("", self.part("popup", "quoteComment").property("text"))     # attached, not pasted as text
        self.assertTrue(self.part("popup", "quoteEditorFiles").isVisible())
        self.assertTrue(self.part("popup", "quoteAttach").isVisible())                 # the paperclip: a picked file
        QTest.keyClick(root, Qt.Key_Return)
        pump(200)
        self.assertFalse(popup.property("visible"))
        self.assertTrue(self.item(root, "contextGroupFiles_" + gid).isVisible())


class WordSnapTests(QuotingCase):
    def test_a_half_selected_word_is_taken_whole(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.conversation(bridge)
        start = REPLY.index("performansı") + 4          # "orman..." through "cid"
        end = REPLY.index("ciddi") + 3
        self.quote_by_button(root, start, end)
        self.assertEqual(["performansı ciddi"], [g["reference"]["text"] for g in bridge.contextGroups])

    def test_an_apostrophe_suffix_belongs_to_its_word_and_whole_words_stay_as_they_are(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.conversation(bridge)
        start = REPLY.index("OpenAI")
        self.quote_by_button(root, start, start + 4)        # "Open" of "OpenAI'ın"
        QTest.keyClick(root, Qt.Key_Return)
        pump(150)
        start = REPLY.index("kendi")
        self.quote_by_button(root, start, start + len("kendi"))
        self.assertEqual(["OpenAI'ın", "kendi"], [g["reference"]["text"] for g in bridge.contextGroups])


class CardTests(QuotingCase):
    def test_one_card_per_quote_with_its_kind_and_the_mode_toggle_from_two(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        entry = self.conversation(bridge)
        bridge.quoteSelection(entry, "coding performansı")
        pump(200)
        self.assertFalse(self.item(root, "responseModeRow").isVisible())     # one quote: no answer mode
        bridge.quoteSelection(entry, "pahalı modellere")
        a, b = (g["id"] for g in bridge.contextGroups)
        bridge.setGroupNote(a, "Kıyasla")
        bridge.groupsChanged.emit()
        pump(200)
        cards = sorted((i for i in self.all_items(root) if i.objectName().startswith("contextGroup_") and i.isVisible()),
                       key=lambda c: (c.mapToScene(QPointF()).y(), c.mapToScene(QPointF()).x()))
        self.assertEqual(["contextGroup_" + a, "contextGroup_" + b], [c.objectName() for c in cards])
        self.assertEqual(("comment", "quote"), (self.item(root, "contextGroupKind_" + a).property("kind"),
                                                self.item(root, "contextGroupKind_" + b).property("kind")))
        self.assertFalse(self.item(root, "contextGroupText_" + a).isVisible())   # a comment card shows the comment
        self.assertTrue(self.item(root, "contextGroupText_" + b).isVisible())    # a plain quote shows its words
        self.assertTrue(self.item(root, "responseModeRow").isVisible())
        self.click(root, "responseMode_separate")
        self.assertEqual("separate", bridge.responseMode)
        self.click(root, "contextGroupRemove_" + a)
        self.assertEqual([b], [g["id"] for g in bridge.contextGroups])

    def test_cards_wrap_and_the_card_area_grows_upward(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        entry = self.conversation(bridge)
        words = REPLY.replace(";", "").replace(".", "").split()
        bridge.quoteSelection(entry, words[0])
        pump(200)
        bar = self.item(root, "composerInput")
        first = self.item(root, "contextGroup_" + bridge.contextGroups[0]["id"])
        bar_y, first_y = bar.mapToScene(QPointF(0, 0)).y(), first.mapToScene(QPointF(0, 0)).y()
        for word in words[1:9]:
            bridge.quoteSelection(entry, word)
        pump(300)
        cards = [self.item(root, "contextGroup_" + g["id"]) for g in bridge.contextGroups]
        rows = sorted({round(c.mapToScene(QPointF(0, 0)).y()) for c in cards})
        self.assertGreater(len(rows), 1)                                            # wrapped, no sideways scroll
        self.assertEqual(1, len({round(c.mapToScene(QPointF(0, 0)).x()) for c in cards[::len(cards)]}))
        self.assertAlmostEqual(bar_y, bar.mapToScene(QPointF(0, 0)).y(), delta=1)   # the input stays where it is
        self.assertLess(first.mapToScene(QPointF(0, 0)).y(), first_y - 20)          # the cards grew upward


class DetailTests(QuotingCase):
    def test_a_card_opens_the_detail_where_the_comment_is_edited_and_the_quote_deleted(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        entry = self.conversation(bridge)
        bridge.quoteSelection(entry, "ciddi şekilde yükselmiş")
        gid = bridge.contextGroups[0]["id"]
        pump(200)
        detail = self.detail(root)
        self.click(root, "contextGroupOpen_" + gid)
        self.assertTrue(detail.property("opened"))
        self.assertEqual(("Quote", "ciddi şekilde yükselmiş"),
                         (detail.property("title"), self.part("detail", "quoteDetailText").property("text")))
        self.press(root, self.part("detail", "quoteDetailEdit"))
        self.assertTrue(detail.property("editing"))
        editor = self.part("detail", "quoteComment")
        self.assertTrue(editor.property("activeFocus"))
        self.type(root, "Kaynak ne?")
        self.assertEqual("", bridge.contextGroups[0]["note"])                       # nothing stored before Save
        self.press(root, self.part("detail", "quoteDetailSave"))
        self.assertEqual(("Kaynak ne?", False, "Comment"),
                         (bridge.contextGroups[0]["note"], detail.property("editing"), detail.property("title")))
        self.assertEqual("comment", self.item(root, "contextGroupKind_" + gid).property("kind"))
        self.press(root, self.part("detail", "quoteDetailDelete"))
        settled(300, lambda: (
            self.assertEqual([], bridge.contextGroups),
            self.assertFalse(detail.property("visible")),
        ))


class ReviewFixTests(QuotingCase):
    def test_the_detail_reads_a_comment_typed_on_the_card_and_edit_keeps_it(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        entry = self.conversation(bridge)
        bridge.quoteSelection(entry, "pahalı modellere")
        gid = bridge.contextGroups[0]["id"]
        pump(200)
        detail = self.detail(root)
        self.click(root, "contextGroupOpen_" + gid)
        QTest.keyClick(root, Qt.Key_Escape)
        pump(200)
        self.click(root, "contextGroupNoteToggle_" + gid)
        self.type(root, "yerinde")
        self.click(root, "contextGroupLabel_" + gid)               # the card, away from the field being typed in
        self.assertEqual(("Comment", "yerinde"), (detail.property("title"), self.part("detail", "quoteDetailComment").property("text")))
        self.press(root, self.part("detail", "quoteDetailEdit"))
        self.assertEqual("yerinde", self.part("detail", "quoteComment").property("text"))
        self.press(root, self.part("detail", "quoteDetailSave"))
        self.assertEqual("yerinde", bridge.contextGroups[0]["note"])

    def test_the_same_words_twice_mark_the_occurrence_that_was_quoted(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        reply_text = "Önbellek bozuk; sonra önbellek yeniden kuruldu."
        self.conversation(bridge, reply=reply_text)
        start = reply_text.index("önbellek")                  # the SECOND one
        self.quote_by_button(root, start, start + len("önbellek"))
        QTest.keyClick(root, Qt.Key_Return)
        pump(200)
        reply = self.reply(root)
        rect = QMetaObject.invokeMethod(reply, "positionToRectangle", Q_RETURN_ARG("QRectF"), Q_ARG(int, start + len("önbellek")))
        self.assertAlmostEqual(rect.x() + 2, self.item(root, "quoteMarker_A").mapToItem(reply, QPointF()).x(), delta=2)
        # Quoting the same range again opens that quote, not a new one.
        popup = self.quote_by_button(root, start, start + len("önbellek"))
        self.assertEqual((1, False), (len(bridge.contextGroups), popup.property("fresh")))

    def test_the_popup_closes_when_its_quote_leaves_the_draft(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.conversation(bridge)
        popup = self.quote_by_button(root, 0, 3)
        self.assertTrue(popup.property("visible"))
        bridge.removeGroup(bridge.contextGroups[0]["id"])
        settled(300, lambda: (
            self.assertFalse(popup.property("visible")),
            self.assertEqual([], bridge.contextGroups),
        ))


class MarkerTests(QuotingCase):
    def test_the_source_marks_each_draft_quote_until_it_is_removed(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        entry = self.conversation(bridge)
        bridge.quoteSelection(entry, "ciddi şekilde yükselmiş")
        bridge.quoteSelection(entry, "pahalı modellere")
        pump(300)
        reply = self.reply(root)
        marker = self.item(root, "quoteMarker_A")
        lines = [i for i in self.all_items(root) if i.objectName() == "quoteUnderline" and i.parentItem().objectName() == "quoteMark_A"]
        self.assertGreaterEqual(len(lines), 1)
        self.assertTrue(self.item(root, "quoteMarker_B").isVisible())
        # The letter stands at the end of its quoted words.
        end = REPLY.index("yükselmiş") + len("yükselmiş")
        rect = QMetaObject.invokeMethod(reply, "positionToRectangle", Q_RETURN_ARG("QRectF"), Q_ARG(int, end))
        self.assertAlmostEqual(rect.x() + 3, marker.mapToItem(reply, QPointF()).x(), delta=2)
        # Its letter opens that quote's detail.
        self.click(root, "quoteMarker_A")
        detail = self.popups["feedDetail"]
        self.assertTrue(detail.property("opened"))
        self.assertEqual(bridge.contextGroups[0]["id"], detail.property("groupId"))
        QTest.keyClick(root, Qt.Key_Escape)
        pump(200)
        bridge.removeGroup(bridge.contextGroups[0]["id"])
        settled(300, lambda: (
            self.assertEqual([], [i for i in self.all_items(root) if i.objectName() == "quoteMark_B"]),
            self.assertEqual(1, len([i for i in self.all_items(root) if i.objectName() == "quoteMark_A"])),   # B is A now
        ))

    def test_sending_clears_the_marks(self) -> None:
        bridge = self.bridge(proposal(kind="chat", reply="Tamam."))
        root = self.window(bridge)
        entry = self.conversation(bridge)
        bridge.quoteMessage(entry)
        pump(200)
        self.assertEqual(1, len([i for i in self.all_items(root) if i.objectName() == "quoteMark_A"]))
        bridge.submitText("Bunu değerlendir.")
        settled(400, lambda: (
            self.assertEqual([], [i for i in self.all_items(root) if i.objectName().startswith("quoteMark_")]),
        ))


class SentMessageTests(QuotingCase):
    def test_the_sent_message_shows_each_quote_then_the_common_message(self) -> None:
        bridge = self.bridge(proposal(kind="chat", reply="Tamam."))
        root = self.window(bridge)
        long_reply = " ".join(["Uzun bir alıntı satırı burada sürüyor."] * 30)
        entry = self.conversation(bridge, reply=long_reply)
        folder = Path(tempfile.mkdtemp(prefix="jarvis-q8-"))
        self.addCleanup(shutil.rmtree, folder, True)
        tiny_png(folder / "shot.png")
        bridge.quoteMessage(entry)
        bridge.quoteSelection(entry, "Uzun bir")
        a, b = (g["id"] for g in bridge.contextGroups)
        bridge.setGroupNote(a, "Özetle")
        self.assertTrue(bridge.attachFileToGroup(a, (folder / "shot.png").as_uri()))
        bridge.submitText("İkisini birlikte değerlendir.")
        pump(400)
        quotes = [i for i in self.all_items(root) if i.objectName() == "sentQuotes" and i.isVisible()]
        self.assertEqual(1, len(quotes))
        block = quotes[0]
        message = [i for i in block.parentItem().childItems() if i.objectName() == "feedMessage_user"][0]
        self.assertGreater(message.y(), block.y() + block.height())                 # the common message comes last
        self.assertEqual(("A", "B"), (self.item(root, "sentQuoteLabel_0").property("text"), self.item(root, "sentQuoteLabel_1").property("text")))
        self.assertEqual(("Özetle", False), (self.item(root, "sentQuoteComment_0").property("text"), self.item(root, "sentQuoteComment_1").isVisible()))
        self.assertTrue(self.item(root, "sentQuoteFiles_0").isVisible())             # its files sit with it
        self.assertFalse([i for i in self.all_items(root) if i.objectName() == "sentAttachments" and i.isVisible()])
        # A long quote folds; "Show more" opens it, "Show less" folds it again.
        text, more = self.item(root, "sentQuoteText_0"), self.item(root, "sentQuoteMore_0")
        folded = text.height()
        self.assertTrue(more.isVisible())
        self.assertFalse(self.item(root, "sentQuoteMore_1").isVisible())             # a short one does not
        self.click(root, "sentQuoteMore_0")
        self.assertGreater(text.height(), folded * 2)
        self.click(root, "sentQuoteMore_0")
        self.assertAlmostEqual(folded, text.height(), delta=1)
        # Unfolded stays unfolded when the feed changes (every row is rebuilt then).
        self.click(root, "sentQuoteMore_0")
        bridge._say("jarvis", "Bir yanıt daha.", chat=True)
        pump(300)
        self.assertGreater(self.item(root, "sentQuoteText_0").height(), folded * 2)
        # The model-facing turn is the same lettered groups as before.
        context = self.model.calls[-1].context
        self.assertEqual([("A", "Özetle", ["shot.png"]), ("B", None, [])],
                         [(g["label"], g.get("note"), [f["name"] for f in g["attachments"]]) for g in context["context_groups"]])


if __name__ == "__main__":
    unittest.main()
