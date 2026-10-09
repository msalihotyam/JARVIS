"""Gate 6G K5: the conversation feed behaves honestly and calmly.

* The streaming caret shows only while text really arrives for the running
  request; a final, failed or stopped reply has none.
* Copy says "Copied" for a moment: a word, not a motion.
* One JarvisMark: the streaming turn and the finished reply wear the same mark
  at the same place.
* A new entry makes its entrance alone; the history above it does not re-fade.
* The receipt replaces the approval request with a crossfade; with ambient
  effects off, at once.
"""
from __future__ import annotations

import unittest

import tests.test_ui as ui
from tests.test_ui import _APP, StreamingModel, UiTestCase, ambient_on, find, pump  # noqa: F401 - _APP: the application
from tests.helpers import settled, wait_until
from tests.test_intent import proposal, task_proposal

from PySide6.QtCore import QMetaObject, QPointF  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlProperty  # noqa: E402


class FeedCase(UiTestCase):
    item = ui.PresentationTests.item
    window = ui.PresentationTests.window

    def streaming(self, *answers, previews=("Merhaba!", "Merhaba! Size nasıl"), error=None):
        self.model = StreamingModel(previews=previews, error=error)
        return self.bridge(*answers)

    @staticmethod
    def signature(node) -> list:
        """What an item draws, in tree order: type, size, colour and border of every part."""
        out = []

        def walk(item):
            color = item.property("color")
            rectangle = color is not None
            out.append((item.metaObject().className(), round(item.width(), 1), round(item.height(), 1),
                        QColor(color).name(QColor.HexArgb) if rectangle else "",
                        (QQmlProperty.read(item, "border.width"), QColor(QQmlProperty.read(item, "border.color")).name()) if rectangle else ()))
            for child in item.childItems():
                walk(child)
        walk(node)
        return out

    def rows(self, root) -> list:
        stack = self.item(root, "streamingReply").parentItem()
        return [child for child in stack.childItems() if child.objectName().startswith("feedRow_")]


class CaretTests(FeedCase):
    def test_the_caret_shows_while_text_arrives_and_is_gone_once_the_answer_stands(self) -> None:
        final = "Merhaba! Size nasıl yardımcı olabilirim?"
        bridge = self.streaming(proposal(kind="chat", reply=final))
        root = self.window(bridge)
        caret = self.item(root, "streamCaret")
        text = self.item(root, "streamingReplyText")
        seen = []

        def look(_):
            pump(40)
            seen.append(caret.isVisible())
            # At the end of the growing text, inside it.
            self.assertLessEqual(caret.y() + caret.height(), text.height() + 1)
        self.model.during = look
        bridge.submitText("Merhaba")
        pump(200)
        self.assertEqual([True, True], seen)
        self.assertEqual(final, bridge.feed[-1]["text"])
        self.assertFalse(caret.isVisible())
        self.assertFalse(caret.property("visible"))

    def test_a_failed_reply_keeps_its_text_without_a_caret(self) -> None:
        from jarvis.contracts import ErrorCode, StructuredError
        bridge = self.streaming(previews=("Kısmi bir yanıt",), error=StructuredError(ErrorCode.CONNECTOR_FAILED, "failed"))
        root = self.window(bridge)
        caret = self.item(root, "streamCaret")
        seen = []
        self.model.during = lambda _: (pump(40), seen.append(caret.isVisible()))
        bridge.submitText("Merhaba")
        pump(200)
        self.assertEqual([True], seen)
        self.assertEqual(["user", "jarvis", "error"], [e["kind"] for e in bridge.feed])
        self.assertTrue(bridge.feed[1].get("interrupted"))
        self.assertFalse(caret.property("visible"))

    def test_a_stopped_run_shows_no_caret_even_while_its_text_is_still_on_screen(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        caret = self.item(root, "streamCaret")
        bridge._say("user", "Merhaba")
        bridge._busy, bridge._request_id = True, "live"
        bridge.stateChanged.emit()
        bridge._on_preview("live", "Yarım kalan yanıt")
        pump(40)
        self.assertTrue(caret.isVisible())
        bridge._busy = False                         # the run stopped; its text is not final yet
        bridge.stateChanged.emit()
        pump(40)
        self.assertEqual("Yarım kalan yanıt", bridge.streamText)
        self.assertFalse(caret.property("visible"))


class CopyConfirmationTests(FeedCase):
    def check_copy(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._say("jarvis", "Kopyalanacak yanıt.", chat=True)
        pump(60)
        copy = self.item(root, "messageCopy_0")
        actions = copy.parentItem()
        self.assertEqual(("Copy", 0.0), (copy.property("text"), actions.property("opacity")))
        center = copy.mapToScene(QPointF(copy.width() / 2, copy.height() / 2)).toPoint()
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        pump(0)
        self.assertEqual("Kopyalanacak yanıt.", QGuiApplication.clipboard().text())
        self.assertEqual(("Copied", True), (copy.property("text"), copy.property("confirming")))
        QTest.mouseMove(root, center + QPointF(0, 300).toPoint())    # the pointer leaves: it still says so
        self.assertTrue(wait_until(lambda: actions.property("opacity") == 1.0, 300))
        self.assertTrue(wait_until(lambda: copy.property("text") == "Copy", 2500))   # then the action returns
        self.assertFalse(copy.property("confirming"))

    def test_copy_confirms_briefly_motion_free(self) -> None:
        self.check_copy()

    @ambient_on
    def test_copy_confirms_briefly_with_ambient_effects_on(self) -> None:
        self.check_copy()

    def test_the_streaming_copy_confirms_too(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._say("user", "Merhaba")
        bridge._busy, bridge._request_id = True, "live"
        bridge._on_preview("live", "Akan yanıt")
        pump(40)
        copy = self.item(root, "streamCopy")
        QMetaObject.invokeMethod(copy, "trigger")
        self.assertEqual(("Akan yanıt", "Copied"), (QGuiApplication.clipboard().text(), copy.property("text")))
        self.assertEqual(1.0, copy.parentItem().property("opacity"))


class AvatarTests(FeedCase):
    def test_the_streaming_turn_and_the_final_reply_wear_one_mark_at_one_place(self) -> None:
        final = "Merhaba! Size nasıl yardımcı olabilirim?"
        bridge = self.streaming(proposal(kind="chat", reply=final))
        root = self.window(bridge)
        streaming = {}

        def look(_):
            pump(40)
            mark = self.item(root, "streamAvatar")
            streaming["sig"] = self.signature(mark)
            streaming["x"] = mark.mapToScene(QPointF(0, 0)).x()
        self.model.during = look
        bridge.submitText("Merhaba")
        pump(200)
        mark = self.item(root, "feedAvatar_jarvis")
        self.assertEqual(streaming["sig"], self.signature(mark))
        self.assertEqual(3, sum(1 for part in streaming["sig"] if part[1:3] == (1.5, 8.0)))    # the three radial cuts
        self.assertAlmostEqual(streaming["x"], mark.mapToScene(QPointF(0, 0)).x(), delta=0.5)


class EntranceTests(FeedCase):
    @ambient_on          # its subject is the entrance motion
    def test_only_the_new_entry_makes_an_entrance(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for i in range(3):
            bridge._say("jarvis" if i % 2 else "user", f"Entry {i}", chat=True)
        pump(400)
        bridge._say("jarvis", "The new one", chat=True)
        pump(16)
        rows = self.rows(root)
        self.assertEqual([False, False, False, True], [r.property("entered") for r in rows])
        self.assertEqual([1.0, 1.0, 1.0], [r.property("opacity") for r in rows[:3]])     # the history stays put
        self.assertLess(rows[3].property("opacity"), 1.0)                                  # the new entry fades in
        pump(400)
        self.assertEqual(1.0, rows[3].property("opacity"))

    def test_a_reopened_conversation_s_turns_and_a_streamed_reply_make_no_entrance(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._say("jarvis", "streamed", chat=True, streamed=True)
        bridge._say("jarvis", "restored", chat=True, restored=True)
        pump(40)
        self.assertEqual([False, False], [r.property("entered") for r in self.rows(root)])


class ApprovalReceiptTests(FeedCase):
    def pending(self, requests=1):
        bridge = self.bridge(*[task_proposal("code_analysis")] * requests)
        root = self.window(bridge)
        bridge.submitText("Bu projeyi incele.")
        self.assertTrue(wait_until(lambda: self.item(root, "safetyCard_approval").isVisible(), 900))
        pump(300)
        return bridge, root

    def reject(self, bridge):
        approval = bridge.safety["approval"]
        bridge.rejectRequest(approval["request_id"], approval["fingerprint"])
        self.assertEqual("rejected", bridge.safety["decision"]["state"])

    @ambient_on          # its subject is the crossfade
    def test_the_receipt_crossfades_over_the_request(self) -> None:
        bridge, root = self.pending()
        approval, receipt, slot = (self.item(root, n) for n in ("safetyCard_approval", "safetyCard_decision", "safetyDecisionSlot"))
        feed = self.item(root, "conversationFeed")
        tall = slot.height()
        # G6K4FIX2: the mid-crossfade state is read at its own animation frame, not after a fixed
        # pump that a loaded machine overshoots past the 160 ms crossfade.
        frames = []

        def frame():
            if not frames and 0.0 < approval.property("opacity") < 1.0:
                frames.append(dict(
                    shown=approval.isVisible() and receipt.isVisible(), opacity=approval.property("opacity"),
                    receipt_y=receipt.y(), approve=find(approval, "approvalApprove").property("enabled"),
                    deny=find(approval, "approvalDeny").property("enabled"),
                    heading=find(approval, "safetyHeading_approval").property("text"),
                    receipt_height=receipt.height(), slot_height=slot.height(), card=feed.property("approvalCardShown")))
        root.afterAnimating.connect(frame)
        try:
            self.reject(bridge)

            def ended():
                self.assertFalse(approval.isVisible())
                self.assertAlmostEqual(receipt.height(), slot.height(), delta=0.5)
            settled(400, ended)
        finally:
            root.afterAnimating.disconnect(frame)
        # Mid-crossfade: the request, as shown and with its buttons off, fades out under the receipt.
        self.assertTrue(frames, "no frame showed the crossfade")
        mid = frames[0]
        self.assertTrue(mid["shown"])
        self.assertLess(mid["opacity"], 1.0)
        self.assertEqual(0.0, mid["receipt_y"])
        self.assertFalse(mid["approve"])
        self.assertFalse(mid["deny"])
        self.assertEqual("Approval required", mid["heading"])
        self.assertLess(mid["receipt_height"], mid["slot_height"])
        self.assertLess(mid["slot_height"], tall + 1)
        self.assertFalse(mid["card"])

    @ambient_on          # it turns ambient effects off after a crossfade
    def test_a_request_after_ambient_effects_are_turned_off_is_shown_opaque(self) -> None:
        bridge, root = self.pending(requests=2)
        approval = self.item(root, "safetyCard_approval")
        self.reject(bridge)
        pump(400)                                    # the request faded out to opacity 0
        self.assertFalse(approval.isVisible())
        bridge.setAmbientEffects(False)
        bridge.submitText("Bu projeyi yine incele.")
        self.assertTrue(wait_until(lambda: approval.isVisible(), 900))
        pump(40)
        self.assertEqual(1.0, approval.property("opacity"))

    def test_with_ambient_effects_off_the_receipt_replaces_it_at_once(self) -> None:
        bridge, root = self.pending()
        approval, receipt, slot = (self.item(root, n) for n in ("safetyCard_approval", "safetyCard_decision", "safetyDecisionSlot"))
        self.reject(bridge)
        pump(0)
        self.assertFalse(approval.isVisible())
        self.assertTrue(receipt.isVisible())
        self.assertEqual(1.0, receipt.property("opacity"))
        self.assertAlmostEqual(receipt.height(), slot.height(), delta=0.5)


if __name__ == "__main__":
    unittest.main()
