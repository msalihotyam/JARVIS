"""DFSCROLL2 dogfood fix (Salih 2026-10-12 10:12, "dolu olsun").

The jump-to-latest key has a solid fill: in every theme (dark, space) and every
face (arrow, live dots, hover) its colour is opaque, and over a line of text
the pixels inside its circle are the key's own - the same as with the text
gone. The ring, the arrow and the dots are unchanged.

Temp stores only: no test opens the live DB or makes a live model, auth or
network call. With JARVIS_SHOTS_DIR set, the contact sheet is saved there
(otherwise to a temp folder, so the test never skips).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QMetaObject, QPointF, QRect, Q_ARG
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest

from tests.test_dfscroll import JumpCase
from tests.test_ui import pump

THEMES = ("dark", "space")
FACES = (("arrow", False, False), ("dots", True, False), ("hover", True, True))
LINE = "Entry {0}: a long line of reading text that runs across the whole width of the stream, " * 3


class SolidFillTests(JumpCase):
    def open(self):
        bridge = self.bridge()
        root = self.window(bridge)
        for i in range(30):
            bridge._say("jarvis", LINE.format(i))
        pump(800)
        return bridge, root, self.item(root, "conversationFeed"), self.item(root, "jumpToLatest")

    def show(self, bridge, root, jump, running: bool, hover: bool) -> None:
        self.set_running(bridge, running)
        QTest.mouseMove(root, self.centre(jump) if hover else root.contentItem().mapToScene(QPointF(5, 5)).toPoint())
        pump(300)
        self.assertEqual(running and not hover, jump.property("showDots"))   # the face really shows

    def disc(self, root, jump) -> list:
        """The pixels strictly inside the ring (radius 15 of 18), as RGB tuples."""
        image = root.grabWindow()
        scale = image.width() / root.width()
        centre = jump.mapToScene(QPointF(jump.width() / 2, jump.height() / 2))
        cx, cy, r = centre.x() * scale, centre.y() * scale, 15 * scale
        return [QColor(image.pixel(x, y)).getRgb()[:3]
                for y in range(int(cy - r), int(cy + r) + 1) for x in range(int(cx - r), int(cx + r) + 1)
                if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r * r]

    def over_text(self, root, feed, jump) -> None:
        """Scroll so that a line of text lies behind the key (proved with the key hidden)."""
        end = feed.property("contentHeight") - feed.property("viewportHeight")
        for by in range(300, 900, 9):
            QMetaObject.invokeMethod(feed, "scrollTo", Q_ARG("QVariant", float(end - by)))
            pump(60)
            if self.text_behind(root, jump) >= 20:
                return
        self.fail("no scroll position puts text behind the key")

    def text_behind(self, root, jump) -> int:
        """Bright text pixels inside the disc with the key hidden."""
        jump.setVisible(False)
        pump(60)
        bright = sum(1 for p in self.disc(root, jump) if min(p) > 150)
        jump.setVisible(True)
        pump(60)
        return bright

    def flick(self, feed):
        return next(c for c in feed.childItems() if c.property("followEnd") is not None)

    def test_the_fill_is_opaque_in_every_theme_and_face(self) -> None:
        bridge, root, feed, jump = self.open()
        self.scroll_up(feed)
        for theme in THEMES:
            bridge.setTheme(theme)
            pump(200)
            for face, running, hover in FACES:
                self.show(bridge, root, jump, running, hover)
                self.assertTrue(jump.isVisible(), (theme, face))
                self.assertEqual(1.0, jump.property("color").alphaF(), (theme, face))

    def test_over_text_the_disc_shows_only_the_key(self) -> None:
        bridge, root, feed, jump = self.open()
        self.over_text(root, feed, jump)
        flick = self.flick(feed)
        for theme in THEMES:
            bridge.setTheme(theme)
            pump(200)
            self.assertGreaterEqual(self.text_behind(root, jump), 20, theme)  # text really lies behind the key
            for face, running, hover in FACES:
                self.show(bridge, root, jump, running, hover)
                with_text = self.disc(root, jump)
                flick.setOpacity(0)                                          # the text gone
                pump(60)
                without = self.disc(root, jump)
                flick.setOpacity(1)
                pump(60)
                worst = max(max(abs(a - b) for a, b in zip(p, q)) for p, q in zip(with_text, without))
                self.assertLessEqual(worst, 2, (theme, face))

    def test_contact_sheet_over_text(self) -> None:
        folder = os.environ.get("JARVIS_SHOTS_DIR")
        if not folder:
            temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            self.addCleanup(temp.cleanup)
            folder = temp.name
        Path(folder).mkdir(parents=True, exist_ok=True)
        bridge, root, feed, jump = self.open()
        self.over_text(root, feed, jump)
        corner = jump.mapToScene(QPointF(0, 0)).toPoint()
        area = QRect(corner.x() - 132, corner.y() - 60, 300, 120)
        sheet = QImage(area.width() * 3 + 40, (area.height() + 30) * 2 + 10, QImage.Format.Format_ARGB32)
        sheet.fill(QColor("#10161e"))
        painter = QPainter(sheet)
        painter.setPen(QColor("#dde6ec"))
        for row, theme in enumerate(THEMES):
            bridge.setTheme(theme)
            pump(200)
            for col, (face, running, hover) in enumerate(FACES):
                self.show(bridge, root, jump, running, hover)
                x, y = 10 + col * (area.width() + 10), 10 + row * (area.height() + 30)
                painter.drawText(x, y + 12, f"{theme} / {face}")
                painter.drawImage(x, y + 20, root.grabWindow().copy(area))
        painter.end()
        self.assertTrue(sheet.save(str(Path(folder) / "contact.png")))


if __name__ == "__main__":
    unittest.main()
