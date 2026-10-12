"""DFSCROLL dogfood fix (Salih 2026-10-12 01:34, look 02:30).

The jump-to-latest key above the composer shows whenever the feed is above its
end (more than ~two rows), in every state - idle, running, after a long
conversation was filled - and hides at the end. While JARVIS works in this
conversation it shows the live activity dots instead of the arrow; under the
pointer, and once the run ends, the arrow. Ambient off: the dots stand still.

Temp stores only: no test opens the live DB or makes a live model, auth or
network call. With JARVIS_SHOTS_DIR set, the frames and a contact sheet are
saved there (otherwise to a temp folder, so the still test never skips).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QMetaObject, QPointF, QRect, Q_ARG, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest

from tests import test_ui as ui
from tests.test_ui import pump


class JumpCase(ui.UiTestCase):
    item = ui.PresentationTests.item
    window = ui.PresentationTests.window

    def open(self):
        bridge = self.bridge()
        root = self.window(bridge)
        for i in range(30):                                               # a long conversation
            bridge._say("jarvis" if i % 2 else "user", f"Entry {i}: enough text to give the stream a real height at this width.")
        pump(800)
        return bridge, root, self.item(root, "conversationFeed"), self.item(root, "jumpToLatest")

    def gap(self, feed) -> float:
        return feed.property("contentHeight") - feed.property("viewportHeight") - feed.property("contentY")

    def scroll_up(self, feed, by: float = 400) -> None:
        end = feed.property("contentHeight") - feed.property("viewportHeight")
        QMetaObject.invokeMethod(feed, "scrollTo", Q_ARG("QVariant", float(end - by)))
        pump(300)

    def set_running(self, bridge, on: bool) -> None:
        bridge._busy = on                                                 # a run of this screen (no record)
        bridge._refresh_core_state()
        pump(300)

    def centre(self, jump):
        return jump.mapToScene(QPointF(jump.width() / 2, jump.height() / 2)).toPoint()

    def face(self, jump) -> tuple:
        arrow, dots = (next(c for c in jump.childItems() if c.objectName() == n) for n in ("jumpArrow", "jumpDots"))
        return round(arrow.opacity(), 2), round(dots.opacity(), 2)


class VisibilityTests(JumpCase):
    def test_idle_scrolled_up_shows_the_arrow_and_the_end_hides_it(self) -> None:
        bridge, root, feed, jump = self.open()
        self.assertEqual("idle", bridge.coreState)
        self.assertFalse(feed.property("jumpVisible"))                    # at the end
        self.scroll_up(feed)
        self.assertTrue(feed.property("jumpVisible"))
        self.assertTrue(jump.isVisible())
        QMetaObject.invokeMethod(feed, "scrollTo", Q_ARG("QVariant", float(1e9)))   # back to the end by hand
        pump(300)
        self.assertFalse(feed.property("jumpVisible"))
        self.assertFalse(jump.isVisible())

    def test_a_tiny_overscroll_does_not_show_it(self) -> None:
        _bridge, _root, feed, _jump = self.open()
        self.scroll_up(feed, 30)                                          # under two rows
        self.assertFalse(feed.property("followEnd"))
        self.assertFalse(feed.property("jumpVisible"))

    def test_running_at_the_end_follows_and_running_above_keeps_the_place(self) -> None:
        bridge, _root, feed, _jump = self.open()
        self.set_running(bridge, True)
        self.assertEqual("running", bridge.coreState)
        bridge._say("jarvis", "while at the end", chat=True)
        pump(700)
        self.assertAlmostEqual(0, self.gap(feed), delta=1)                # follows
        self.assertFalse(feed.property("jumpVisible"))
        self.scroll_up(feed)
        place = round(feed.property("contentY"))
        self.assertTrue(feed.property("jumpVisible"))
        bridge._say("jarvis", "while reading above", chat=True)
        pump(700)
        self.assertEqual(place, round(feed.property("contentY")))         # no auto-follow
        self.assertTrue(feed.property("jumpVisible"))

    def test_a_click_glides_to_the_end_and_hides_it(self) -> None:
        _bridge, root, feed, jump = self.open()
        self.scroll_up(feed)
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, self.centre(jump))
        pump(900)
        self.assertAlmostEqual(0, self.gap(feed), delta=1)
        self.assertFalse(feed.property("jumpVisible"))
        self.assertFalse(jump.isVisible())


class LookTests(JumpCase):
    ambient = True      # THIZ: its subject is the dots' rhythm and the crossfade

    def test_running_above_shows_the_dots_hover_the_arrow_and_the_end_of_the_run_the_arrow(self) -> None:
        bridge, root, feed, jump = self.open()
        self.scroll_up(feed)
        QTest.mouseMove(root, root.contentItem().mapToScene(QPointF(5, 5)).toPoint())
        self.assertEqual((1, 0), self.face(jump))                         # idle: the arrow
        self.set_running(bridge, True)
        self.assertEqual((0, 1), self.face(jump))                         # running: the dots
        self.assertTrue(jump.property("dotsAnimated"))
        QTest.mouseMove(root, self.centre(jump))
        pump(400)
        self.assertEqual((1, 0), self.face(jump))                         # hover: the arrow
        self.assertFalse(jump.property("dotsAnimated"))
        QTest.mouseMove(root, root.contentItem().mapToScene(QPointF(5, 5)).toPoint())
        pump(400)
        self.assertEqual((0, 1), self.face(jump))
        self.set_running(bridge, False)
        self.assertEqual((1, 0), self.face(jump))                         # the run ended: the arrow
        self.assertFalse(jump.property("dotsAnimated"))

    def test_with_ambient_off_the_dots_stand_still(self) -> None:
        bridge, _root, feed, jump = self.open()
        self.scroll_up(feed)
        bridge.setAmbientEffects(False)
        self.set_running(bridge, True)
        self.assertEqual((0, 1), self.face(jump))
        self.assertFalse(jump.property("dotsAnimated"))                   # the loop does not run

    def test_frames_of_the_arrow_the_dots_and_hover(self) -> None:
        folder = os.environ.get("JARVIS_SHOTS_DIR")
        if not folder:                                                    # still runs: the frames go to a temp folder
            temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            self.addCleanup(temp.cleanup)
            folder = temp.name
        Path(folder).mkdir(parents=True, exist_ok=True)
        bridge, root, feed, jump = self.open()
        self.scroll_up(feed)
        away = root.contentItem().mapToScene(QPointF(5, 5)).toPoint()
        QTest.mouseMove(root, away)
        corner = jump.mapToScene(QPointF(0, 0)).toPoint()
        area = QRect(corner.x() - 132, corner.y() - 60, 300, 120)
        frames = []
        for name, running, hover in (("arrow", False, False), ("dots", True, False), ("hover", True, True)):
            self.set_running(bridge, running)
            QTest.mouseMove(root, self.centre(jump) if hover else away)
            pump(400)
            frame = root.grabWindow().copy(area)
            frame.save(str(Path(folder) / f"dfscroll_{name}.png"))
            frames.append(frame)
        sheet = QImage(area.width() * 3 + 40, area.height() + 20, QImage.Format.Format_ARGB32)
        sheet.fill(QColor("#10161e"))
        painter = QPainter(sheet)
        for i, frame in enumerate(frames):
            painter.drawImage(10 + i * (area.width() + 10), 10, frame)
        painter.end()
        self.assertTrue(sheet.save(str(Path(folder) / "dfscroll_contact.png")))


if __name__ == "__main__":
    unittest.main()
