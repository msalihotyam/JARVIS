"""Gate 6G U2 / U3 part A (G6U1A): the usage ring and the usage popover as
self-contained components (placement is part B).

* U2: the ring's arc and centre text show the 5-hour window's USED %; one
  colour whatever the value; nothing reported -> an empty ring and "–"; the
  halo turns only with ambient effects on; a mouse click opens / closes the
  popover; a HoverTip.
* U3: "Plan usage limits · <provider>", one row per REPORTED window (used %,
  reset as a human time, bar); a window not reported is not drawn, none
  reported says so. No context-window block (V2.1).

No test opens the live DB or makes a live call.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from tests.test_ui import _APP, pump  # noqa: F401 - the offscreen platform and the application

import shiboken6  # noqa: E402
from PySide6.QtCore import QObject, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jarvis.ui import i18n  # noqa: E402
from jarvis.ui.typography import FONT_DIR  # noqa: E402
from jarvis.ui.wording import human_date  # noqa: E402

COMPONENTS = FONT_DIR.parent / "components"
SHEET = b"""
import QtQuick
import QtQuick.Window
import "../theme"
Window {
    width: 800; height: 600; visible: true; color: "#000000"
    function setAmbient(on) { Theme.ambient = on }
    UsageRing { x: 700; y: 540 }
}
"""


def descendants(item):
    for child in item.childItems():
        yield child
        yield from descendants(child)


class UsageCase(unittest.TestCase):
    ambient = False

    def setUp(self) -> None:
        self.loc = i18n.Localization(_APP)
        self.addCleanup(self.loc.apply, "en-US")
        engine = QQmlEngine()
        self.addCleanup(engine.deleteLater)
        engine.rootContext().setContextProperty("i18n", self.loc)
        self.loc.attach(engine)
        # Held for the whole test: a collected component took its window with it.
        self.component = component = QQmlComponent(engine)
        component.setData(SHEET, QUrl.fromLocalFile(str(COMPONENTS / "UsageSheetTest.qml")))
        self.window = component.create()
        self.assertIsNotNone(self.window, component.errorString())
        self.addCleanup(lambda: shiboken6.isValid(self.window) and self.window.hide())
        self.window.setAmbient(self.ambient)
        self.ring = self.named("usageRing")
        self.pop = self.ring.findChild(QObject, "usagePopover")
        self.assertIsNotNone(self.pop)
        pump(50)

    def named(self, name, root=None):
        found = [i for i in descendants(root or self.window.contentItem()) if i.objectName() == name]
        self.assertEqual(1, len(found), name)
        return found[0]

    def rows_shown(self):
        body = self.pop.property("contentItem")
        return {i.objectName() for i in descendants(body) if i.objectName().startswith("usageRow_") and i.isVisible()}

    def text_in(self, row_name, name):
        row = self.named(row_name, self.pop.property("contentItem"))
        return self.named(name, row)

    def open_popover(self):
        self.pop.open()
        pump(30)

    def click_ring(self):
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier,
                         self.ring.mapToScene(QPointF(self.ring.width() / 2, self.ring.height() / 2)).toPoint())
        pump(30)


class RingTests(UsageCase):
    def test_arc_and_centre_text_for_0_37_100_and_nothing_reported(self) -> None:
        text = self.named("usageRingText")
        arc = self.ring.findChild(QObject, "usageArc")
        tones = set()
        for used, sweep, shown in ((0, 0, "0%"), (37, 133.2, "37%"), (100, 360, "100%")):
            with self.subTest(used=used):
                self.ring.setProperty("rows", [{"window": "five_hour", "remaining": 100 - used, "resets_at": 0}])
                self.assertEqual(used, self.ring.property("usedPct"))
                self.assertAlmostEqual(sweep, self.ring.property("sweep"), places=3)
                self.assertEqual(shown, text.property("text"))
                tones.add((self.ring.property("tone").name(), text.property("color").name()))
                if used:
                    tones.add((arc.property("strokeColor").name(), text.property("color").name()))
        self.assertEqual(1, len(tones))                          # one colour whatever the value
        for rows in ([], [{"window": "weekly", "remaining": 40, "resets_at": 0}]):
            with self.subTest(rows=rows):
                self.ring.setProperty("rows", rows)
                self.assertIsNone(self.ring.property("usedPct"))
                self.assertEqual(0, self.ring.property("sweep"))
                self.assertEqual("–", text.property("text"))
        self.ring.setProperty("usedPct", 37)                   # a plain input wins over the rows
        self.assertEqual("37%", text.property("text"))

    def test_the_halo_stands_still_with_ambient_off(self) -> None:
        halo = self.named("usageHalo")
        self.assertFalse(self.ring.property("haloTurning"))
        before = halo.rotation()
        pump(200)
        self.assertEqual(before, halo.rotation())

    def test_a_click_opens_and_a_second_click_closes_the_popover_and_the_tip_names_it(self) -> None:
        tip = [c for c in self.ring.childItems() if "HoverTip" in c.metaObject().className()]
        self.assertEqual(1, len(tip))
        self.assertEqual("Usage not reported · details", tip[0].property("text"))
        self.ring.setProperty("rows", [{"window": "five_hour", "remaining": 72, "resets_at": 0}])
        self.assertEqual("5-hour usage 28% · details", tip[0].property("text"))
        self.click_ring()
        self.assertTrue(self.pop.property("opened"))
        self.click_ring()
        self.assertFalse(self.pop.property("visible"))
        self.click_ring()
        self.assertTrue(self.pop.property("opened"))
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier, QPointF(100, 100).toPoint())   # outside
        pump(30)
        self.assertFalse(self.pop.property("visible"))


class HaloMotionTests(UsageCase):
    ambient = True

    def test_the_halo_turns_with_ambient_on(self) -> None:
        self.assertTrue(self.ring.property("haloTurning"))
        self.window.setAmbient(False)
        self.assertFalse(self.ring.property("haloTurning"))


class PopoverTests(UsageCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.pop.setProperty("now", self.now.timestamp() * 1000)
        self.ring.setProperty("provider", "Anthropic")
        self.five = int((self.now + timedelta(hours=2, minutes=30)).timestamp())
        self.week = int((self.now + timedelta(days=3, hours=-1)).timestamp())
        self.both = [{"window": "five_hour", "remaining": 72, "resets_at": self.five},
                     {"window": "weekly", "remaining": 41, "resets_at": self.week}]

    def test_both_one_and_no_windows_reported(self) -> None:
        self.ring.setProperty("rows", self.both)
        self.open_popover()
        self.assertEqual("Plan usage limits · Anthropic", self.named("usagePopoverTitle", self.pop.property("contentItem")).property("text"))
        self.assertEqual({"usageRow_five_hour", "usageRow_weekly"}, self.rows_shown())
        self.assertEqual("28%", self.text_in("usageRow_five_hour", "usageRowUsed").property("text"))
        self.assertEqual("59%", self.text_in("usageRow_weekly", "usageRowUsed").property("text"))
        self.assertEqual("5-hour session", self.text_in("usageRow_five_hour", "usageRowTitle").property("text"))
        self.assertEqual("Weekly", self.text_in("usageRow_weekly", "usageRowTitle").property("text"))
        bar = self.text_in("usageRow_weekly", "usageRowBar")
        self.assertAlmostEqual(0.59, bar.width() / bar.parentItem().width(), places=3)
        none = self.named("usageNone", self.pop.property("contentItem"))
        self.assertFalse(none.isVisible())
        # One window reported: the other is hidden, not a zero.
        self.ring.setProperty("rows", self.both[1:])
        pump(10)
        self.assertEqual({"usageRow_weekly"}, self.rows_shown())
        # A row without a reported figure is hidden too.
        self.ring.setProperty("rows", [{"window": "five_hour", "resets_at": self.five}, self.both[1]])
        pump(10)
        self.assertEqual({"usageRow_weekly"}, self.rows_shown())
        self.ring.setProperty("rows", [])
        pump(10)
        self.assertEqual(set(), self.rows_shown())
        self.assertTrue(none.isVisible())
        self.assertEqual("This provider reported no usage limits.", none.property("text"))
        body = [i.property("text") for i in descendants(self.pop.property("contentItem")) if i.property("text")]
        self.assertFalse([t for t in body if "%" in t])          # no estimated figure anywhere

    def test_reset_times_read_as_human_times_and_a_missing_reset_is_hidden(self) -> None:
        self.ring.setProperty("rows", self.both)
        self.open_popover()
        self.assertEqual("Resets today 12:30", self.text_in("usageRow_five_hour", "usageRowReset").property("text"))
        self.assertEqual("Resets " + human_date(self.week, self.now, "en_US", "time"),
                         self.text_in("usageRow_weekly", "usageRowReset").property("text"))
        self.ring.setProperty("rows", [dict(self.both[0], resets_at=0)])
        pump(10)
        self.assertFalse(self.text_in("usageRow_five_hour", "usageRowReset").isVisible())

    def test_tr_tr_strings(self) -> None:
        self.loc.apply("tr-TR")
        self.ring.setProperty("rows", self.both)
        self.open_popover()
        body = self.pop.property("contentItem")
        self.assertEqual("Plan kullanım limitleri · Anthropic", self.named("usagePopoverTitle", body).property("text"))
        self.assertEqual("5 saatlik oturum", self.text_in("usageRow_five_hour", "usageRowTitle").property("text"))
        self.assertEqual("Haftalık", self.text_in("usageRow_weekly", "usageRowTitle").property("text"))
        self.assertEqual("%28", self.text_in("usageRow_five_hour", "usageRowUsed").property("text"))
        self.assertEqual("Sıfırlanma: bugün 12:30", self.text_in("usageRow_five_hour", "usageRowReset").property("text"))
        self.assertEqual("%28", self.named("usageRingText").property("text"))
        tip = [c for c in self.ring.childItems() if "HoverTip" in c.metaObject().className()][0]
        self.assertEqual("5 saatlik kullanım %28 · ayrıntılar", tip.property("text"))
        self.assertEqual("Sağlayıcının bildirdiği değerler; bildirilmeyen değer tahmin edilmez.",
                         self.named("usageFootnote", body).property("text"))
        self.ring.setProperty("rows", [])
        pump(10)
        self.assertEqual("Bu sağlayıcı kullanım limiti bildirmedi.", self.named("usageNone", body).property("text"))
        self.assertEqual("Kullanım bildirilmedi · ayrıntılar", tip.property("text"))


if __name__ == "__main__":
    unittest.main()
