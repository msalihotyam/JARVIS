"""DFSWITCH (dogfood, Salih 2026-10-12): the freeze switch skins 3-4.

* Fit: the art has room at its edges (the toggle's top and the knife handle's
  rim were cut flush by the old crops) and the switch is inside its box at
  small / default / large window sizes in every presentation.
* The knife arcs only while its blades leave or seat in the jaws; the toggle
  never arcs; nothing is drawn at rest.
* Ambient off: the lever jumps, no arc, no spark, nothing animates.
* The Freeze command is sent on the click, before any effect frame.
"""

from __future__ import annotations

import unittest

from tests import test_ui
from tests.test_ui import UiTestCase, pump, window_ready  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jarvis.ui.typography import FONT_DIR  # noqa: E402

ASSETS = FONT_DIR.parent / "assets"
SIZES = ((1100, 700), (1920, 1080), (2560, 1440))


def scene_rect(item):
    top_left = item.mapToScene(QPointF(0, 0))
    return top_left.x(), top_left.y(), top_left.x() + item.width(), top_left.y() + item.height()


class FitTests(UiTestCase):
    item = test_ui.PresentationTests.item
    window = test_ui.PresentationTests.window

    def test_the_art_leaves_room_at_its_edges(self) -> None:
        for skin in ("toggle", "knife"):
            for layer in ("plate", "up", "down"):
                with self.subTest(skin=skin, layer=layer):
                    image = QImage(str(ASSETS / f"freeze-{skin}-{layer}.png"))
                    w, h = image.width(), image.height()
                    band = {"t": [(x, y) for y in range(4) for x in range(w)],
                            "b": [(x, h - 1 - y) for y in range(4) for x in range(w)],
                            "l": [(x, y) for x in range(4) for y in range(h)],
                            "r": [(w - 1 - x, y) for x in range(4) for y in range(h)]}
                    for side in "tlrb":
                        self.assertLessEqual(max(image.pixelColor(x, y).alpha() for x, y in band[side]), 8, side)

    def test_the_switch_is_inside_its_box_at_every_size_and_presentation(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for theme in ("space", "dark", "command"):
            bridge.setTheme(theme)
            for width, height in SIZES:
                root.resize(width, height)
                window_ready(self, root, width, height, 350)
                for skin in (3, 4):
                    with self.subTest(theme=theme, size=(width, height), skin=skin):
                        bridge.setFreezeKeySkin(skin)
                        pump(50)
                        control, switch = self.item(root, "killSwitchControl"), self.item(root, "freezeKeySwitch")
                        cx0, cy0, cx1, cy1 = scene_rect(control)
                        sx0, sy0, sx1, sy1 = scene_rect(switch)
                        self.assertTrue(cx0 - 0.5 <= sx0 and cy0 - 0.5 <= sy0 and sx1 <= cx1 + 0.5 and sy1 <= cy1 + 0.5,
                                        ((sx0, sy0, sx1, sy1), (cx0, cy0, cx1, cy1)))
                        self.assertTrue(0 <= sx0 and 0 <= sy0 and sx1 <= width and sy1 <= height, (sx0, sy0, sx1, sy1))


class EffectTests(UiTestCase):
    ambient = True      # its subject is the switches' motion and the knife's arcs
    item = test_ui.PresentationTests.item
    window = test_ui.PresentationTests.window

    def watch(self, arcs, ms: int) -> list[bool]:
        seen = []
        for _ in range(ms // 10):
            pump(10)
            seen.append(arcs.isVisible())
        return seen

    def test_the_knife_arcs_only_while_it_opens_or_closes(self) -> None:
        bridge = self.bridge()
        bridge.setFreezeKeySkin(4)
        root = self.window(bridge)
        switch, arcs = self.item(root, "freezeKeySwitch"), self.item(root, "freezeKnifeArcs")
        self.assertFalse(arcs.isVisible())                                     # nothing at rest
        for move in (bridge.freezeJarvis, bridge.resumeJarvis):                # open, then close
            with self.subTest(move=move.__name__):
                move()
                seen = self.watch(arcs, 900)
                self.assertTrue(any(seen), "no arc")
                self.assertFalse(seen[-1])                                     # gone after
                self.assertFalse(switch.property("arcing"))
        self.assertFalse(any(self.watch(arcs, 300)))                           # calm while idle

    def test_the_toggle_never_arcs(self) -> None:
        bridge = self.bridge()
        bridge.setFreezeKeySkin(3)
        root = self.window(bridge)
        arcs = self.item(root, "freezeKnifeArcs")
        bridge.freezeJarvis()
        seen = self.watch(arcs, 400)
        bridge.resumeJarvis()
        self.assertFalse(any(seen + self.watch(arcs, 400)))

    def test_the_command_is_sent_before_any_effect_frame(self) -> None:
        bridge = self.bridge()
        bridge.setFreezeKeySkin(4)
        root = self.window(bridge)
        switch, arcs = self.item(root, "freezeKeySwitch"), self.item(root, "freezeKnifeArcs")
        events = []
        arcs.painted.connect(lambda: events.append(("frame", switch.property("burst"))))
        send = self.backend.set_config

        def spy(key, raw):
            events.append(("command", switch.property("burst"), switch.property("shown")))
            return send(key, raw)
        self.backend.set_config = spy
        control = self.item(root, "killSwitchControl")
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, control.mapToScene(QPointF(control.width() / 2, control.height() / 2)).toPoint())
        self.assertEqual(("command", 0, 0), events[0])                         # sent on the click: no effect frame yet, the lever unmoved
        self.assertEqual("frozen", bridge.executionState)                      # the shown state at once, not after the effect
        self.assertTrue(any(self.watch(arcs, 500)))                            # the effect follows
        self.assertEqual(["command"], [e[0] for e in events if e[0] == "command"])
        self.assertIn("frame", [e[0] for e in events[1:]])


class AmbientOffTests(UiTestCase):
    item = test_ui.PresentationTests.item
    window = test_ui.PresentationTests.window

    def test_nothing_animates_with_ambient_off(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for skin in (3, 4):
            with self.subTest(skin=skin):
                bridge.setFreezeKeySkin(skin)
                pump(50)
                switch, arcs = self.item(root, "freezeKeySwitch"), self.item(root, "freezeKnifeArcs")
                for move, end in ((bridge.freezeJarvis, 1), (bridge.resumeJarvis, 0)):
                    move()
                    self.assertEqual(end, switch.property("shown"))            # no pump: it jumped
                    for _ in range(30):
                        pump(10)
                        self.assertEqual((end, 0, False, False),
                                         (switch.property("shown"), switch.property("burst"), switch.property("arcing"), arcs.isVisible()))


if __name__ == "__main__":
    unittest.main()
