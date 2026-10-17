"""DFHOVER: the meta line under a message (time, telemetry, actions) waits for the person.

* At rest it is hidden - for every kind of entry and for the streaming turn.
* It fades in while the pointer is over that message, while the message has
  keyboard focus or a selection, or while an action is confirming.
* Opacity only: the bubbles do not move when it appears.
* A screen reader still hears the time.
"""
from __future__ import annotations

import unittest

from tests.test_g6k5_feed import FeedCase
from tests.test_ui import _APP, ambient_on, find, pump  # noqa: F401 - _APP: the application
from tests.helpers import wait_until

from PySide6.QtCore import QMetaObject, QPointF  # noqa: E402
from PySide6.QtGui import QAccessible  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

KINDS = ("user", "jarvis", "result", "error")


class MetaLineTests(FeedCase):
    def feed(self):
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._say("user", "Bu projeyi incele.")
        bridge._say("jarvis", "İnceledim.", chat=True, telemetry={"elapsed_ms": 4800, "first_ms": 4200, "tokens": 14500})
        bridge._say("result", "Görev sonucu.", task_id="t-1", run_status="completed", capability="code_analysis")
        bridge._say("error", "Bağlantı kurulamadı.")
        pump(60)
        self.rest(root)
        return bridge, root

    def rest(self, root) -> None:
        QTest.mouseMove(root, root.contentItem().mapToScene(QPointF(5, 5)).toPoint())
        pump(20)

    def row(self, root, kind):
        return next(r for r in self.rows(root) if r.objectName() == "feedRow_" + kind)

    def shown(self, root) -> dict:
        return {kind: find(self.row(root, kind), "feedMeta").property("opacity") for kind in KINDS}

    def geometry(self, root) -> list:
        out = []
        for kind in KINDS:
            row, card = self.row(root, kind), self.item(root, "feedCard_" + kind)
            corner = card.mapToScene(QPointF(0, 0))
            out.append((kind, row.height(), round(corner.x(), 1), round(corner.y(), 1), card.width(), card.height()))
        return out

    def hover(self, root, item) -> None:
        QTest.mouseMove(root, item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())

    def only(self, kind) -> dict:
        return {k: 1.0 if k == kind else 0.0 for k in KINDS}

    def test_hidden_at_rest_for_every_kind(self) -> None:
        _, root = self.feed()
        self.assertEqual(self.only(None), self.shown(root))
        for index in (0, 1):                      # the person's message and the chat reply carry actions
            self.assertEqual(0.0, self.item(root, "messageActions_%d" % index).property("opacity"))

    def check_hover(self) -> None:
        _, root = self.feed()
        for kind in KINDS:
            self.hover(root, self.item(root, "feedCard_" + kind))
            self.assertTrue(wait_until(lambda: self.shown(root) == self.only(kind), 600), (kind, self.shown(root)))
        self.hover(root, self.item(root, "feedCard_jarvis"))
        self.assertTrue(wait_until(lambda: self.item(root, "messageActions_1").property("opacity") == 1.0, 600))
        self.rest(root)
        self.assertTrue(wait_until(lambda: self.shown(root) == self.only(None), 600))

    def test_hover_shows_only_that_message_line(self) -> None:
        self.check_hover()

    @ambient_on
    def test_hover_fades_with_ambient_effects_on(self) -> None:
        self.check_hover()

    def test_keyboard_focus_shows_the_line(self) -> None:
        _, root = self.feed()
        root.requestActivate()
        for kind in KINDS:
            message = self.item(root, "feedMessage_" + kind)
            message.forceActiveFocus()
            self.assertTrue(wait_until(lambda: message.hasActiveFocus(), 600), kind)
            self.assertTrue(message.property("activeFocusOnTab"), kind)
            self.assertEqual(self.only(kind), self.shown(root))

    def test_a_selection_shows_the_line(self) -> None:
        _, root = self.feed()
        message = self.item(root, "feedMessage_error")
        QMetaObject.invokeMethod(message, "selectAll")
        pump(0)
        self.assertEqual(self.only("error"), self.shown(root))
        QMetaObject.invokeMethod(message, "deselect")
        pump(0)
        self.assertEqual(self.only(None), self.shown(root))

    def test_a_confirming_action_keeps_the_line(self) -> None:
        _, root = self.feed()
        QMetaObject.invokeMethod(self.item(root, "messageCopy_1"), "trigger")
        pump(0)
        self.assertEqual(self.only("jarvis"), self.shown(root))
        self.assertEqual(1.0, self.item(root, "messageActions_1").property("opacity"))
        self.assertEqual(0.0, self.item(root, "messageActions_0").property("opacity"))

    def test_the_bubbles_do_not_move_when_the_line_appears(self) -> None:
        _, root = self.feed()
        at_rest = self.geometry(root)
        self.hover(root, self.item(root, "feedCard_jarvis"))
        self.assertTrue(wait_until(lambda: self.shown(root)["jarvis"] == 1.0, 600))
        self.assertEqual(at_rest, self.geometry(root))
        QMetaObject.invokeMethod(self.item(root, "feedMessage_user"), "selectAll")
        QMetaObject.invokeMethod(self.item(root, "feedMessage_error"), "selectAll")
        pump(0)
        self.assertEqual(at_rest, self.geometry(root))

    def test_a_screen_reader_hears_the_time_while_the_line_is_hidden(self) -> None:
        _, root = self.feed()
        for kind in KINDS:
            stamp = find(self.row(root, kind), "feedStamp").property("text")
            message = self.item(root, "feedMessage_" + kind)
            description = QAccessible.queryAccessibleInterface(message).text(QAccessible.Text.Description)
            self.assertTrue(stamp and description.startswith(stamp), (kind, stamp, description))
        telemetry = find(self.row(root, "jarvis"), "feedTelemetry").property("text")
        description = QAccessible.queryAccessibleInterface(self.item(root, "feedMessage_jarvis")).text(QAccessible.Text.Description)
        self.assertIn(telemetry, description)


class StreamMetaTests(FeedCase):
    def stream(self):
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._say("user", "Merhaba")
        bridge._busy, bridge._request_id = True, "live"
        bridge._on_preview("live", "Akan yanıt")
        pump(40)
        QTest.mouseMove(root, root.contentItem().mapToScene(QPointF(5, 5)).toPoint())
        pump(20)
        return root, self.item(root, "streamActions"), self.item(root, "streamingReplyText")

    def test_hidden_at_rest_then_shown_on_hover(self) -> None:
        root, actions, text = self.stream()
        self.assertTrue(actions.property("visible"))
        self.assertEqual(0.0, actions.property("opacity"))
        QTest.mouseMove(root, text.mapToScene(QPointF(text.width() / 2, text.height() / 2)).toPoint())
        self.assertTrue(wait_until(lambda: actions.property("opacity") == 1.0, 600))
        QTest.mouseMove(root, root.contentItem().mapToScene(QPointF(5, 5)).toPoint())
        self.assertTrue(wait_until(lambda: actions.property("opacity") == 0.0, 600))

    def test_focus_and_selection_show_it(self) -> None:
        root, actions, text = self.stream()
        root.requestActivate()
        text.forceActiveFocus()
        self.assertTrue(wait_until(lambda: actions.property("opacity") == 1.0, 600))
        self.assertTrue(text.property("activeFocusOnTab"))
        self.item(root, "feedMessage_user").forceActiveFocus()
        self.assertTrue(wait_until(lambda: actions.property("opacity") == 0.0, 600))
        QMetaObject.invokeMethod(text, "selectAll")
        pump(0)
        self.assertEqual(1.0, actions.property("opacity"))


if __name__ == "__main__":
    unittest.main()
