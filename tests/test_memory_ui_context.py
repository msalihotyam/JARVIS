"""MEMUICTX (V2.1a M4): the context usage block in the usage popover.

The block shows bridge.contextUsage as it is ("used / max" tokens, a bar at the
app's ratio, the turns left before auto-compaction), says unknown honestly
(K3) and asks bridge.compactConversation for the open conversation - never
while a reply streams. A fake bridge stands in: no app, no model.
"""

from __future__ import annotations

import unittest

from tests.test_ui import _APP, pump  # noqa: F401 - the offscreen platform and the application

import shiboken6  # noqa: E402
from PySide6.QtCore import Property, QObject, QPointF, Qt, QUrl, Signal, Slot  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jarvis.ui import i18n  # noqa: E402
from jarvis.ui.typography import FONT_DIR  # noqa: E402

COMPONENTS = FONT_DIR.parent / "components"
SHEET = b"""
import QtQuick
import QtQuick.Window
import "../theme"
Window {
    width: 800; height: 600; visible: true; color: "#000000"
    function setTheme(name) { Theme.name = name }
    UsageRing { x: 700; y: 540 }
}
"""
UNKNOWN = {"used": None, "max": None, "ratio": None, "turnsUntilAutoCompact": None, "state": "unknown"}


class FakeBridge(QObject):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._usage, self._busy, self._id, self.calls = dict(UNKNOWN), False, "c1", []

    def set(self, usage=None, busy=None, conversation_id=None) -> None:
        self._usage = usage if usage is not None else self._usage
        self._busy = busy if busy is not None else self._busy
        self._id = conversation_id if conversation_id is not None else self._id
        self.changed.emit()
        pump(10)

    @Property("QVariantMap", notify=changed)
    def contextUsage(self) -> dict:
        return dict(self._usage)

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=changed)
    def conversationId(self) -> str:
        return self._id

    @Slot(str)
    def compactConversation(self, conversation_id: str) -> None:
        self.calls.append(conversation_id)


def descendants(item):
    for child in item.childItems():
        yield child
        yield from descendants(child)


class ContextUsageBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loc = i18n.Localization(_APP)
        self.addCleanup(self.loc.apply, "en-US")
        engine = QQmlEngine()
        self.addCleanup(engine.deleteLater)
        self.bridge = FakeBridge()
        engine.rootContext().setContextProperty("i18n", self.loc)
        engine.rootContext().setContextProperty("bridge", self.bridge)
        self.loc.attach(engine)
        self.component = component = QQmlComponent(engine)
        component.setData(SHEET, QUrl.fromLocalFile(str(COMPONENTS / "ContextSheetTest.qml")))
        self.window = component.create()
        self.assertIsNotNone(self.window, component.errorString())
        self.addCleanup(lambda: shiboken6.isValid(self.window) and self.window.hide())
        self.addCleanup(self.window.setTheme, "space")
        ring = self.named("usageRing", self.window.contentItem())
        self.pop = ring.findChild(QObject, "usagePopover")
        self.pop.open()
        pump(30)
        self.block = self.named("contextUsageBlock", self.pop.property("contentItem"))

    def named(self, name, root=None):
        found = [i for i in descendants(root or self.block) if i.objectName() == name]
        self.assertEqual(1, len(found), name)
        return found[0]

    def text(self, name):
        return self.named(name).property("text")

    def click_compact(self):
        button = self.named("contextCompactButton")
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier,
                         button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint())
        pump(20)

    def test_values_are_bound_as_the_bridge_reports_them(self) -> None:
        self.bridge.set({"used": 12345, "max": 200000, "ratio": 0.25, "turnsUntilAutoCompact": 7, "state": "normal"})
        self.assertEqual("12,345 / 200,000 tokens", self.text("contextUsageFigure"))
        self.assertEqual("7 turns until auto-compaction", self.text("contextUsageTurns"))
        bar = self.named("contextUsageBar")
        self.assertTrue(self.named("contextUsageTrack").isVisible())
        self.assertAlmostEqual(0.25, bar.width() / bar.parentItem().width(), places=3)
        self.bridge.set({"used": 199000, "max": 200000, "ratio": 1.0, "turnsUntilAutoCompact": 1, "state": "full"})
        self.assertEqual("1 turn until auto-compaction", self.text("contextUsageTurns"))
        self.assertAlmostEqual(1.0, bar.width() / bar.parentItem().width(), places=3)

    def test_unknown_says_so_and_draws_no_bar(self) -> None:
        self.assertEqual("Unknown", self.text("contextUsageFigure"))
        self.assertFalse(self.named("contextUsageTrack").isVisible())
        self.assertFalse(self.named("contextUsageTurns").isVisible())
        # The window is unknown but the use is counted: the max is "unknown", never a guess.
        self.bridge.set({"used": 900, "max": None, "ratio": None, "turnsUntilAutoCompact": None, "state": "unknown"})
        self.assertEqual("900 tokens / limit unknown", self.text("contextUsageFigure"))
        self.assertFalse(self.named("contextUsageTrack").isVisible())

    def test_the_button_compacts_the_open_conversation_and_is_off_while_busy(self) -> None:
        button = self.named("contextCompactButton")
        self.assertEqual("Compact session", button.property("label"))
        self.click_compact()
        self.assertEqual(["c1"], self.bridge.calls)
        self.bridge.set(busy=True)
        self.assertFalse(button.property("enabled"))
        self.click_compact()
        self.assertEqual(["c1"], self.bridge.calls)
        self.bridge.set(busy=False, conversation_id="")
        self.assertFalse(button.property("enabled"))   # nothing open: nothing to compact
        self.bridge.set(conversation_id="c2")
        self.assertTrue(button.property("enabled"))
        self.click_compact()
        self.assertEqual(["c1", "c2"], self.bridge.calls)

    def test_tr_tr_strings_and_both_themes(self) -> None:
        self.loc.apply("tr-TR")
        pump(10)
        self.bridge.set({"used": 12345, "max": 200000, "ratio": 0.25, "turnsUntilAutoCompact": 7, "state": "normal"})
        self.assertEqual("12.345 / 200.000 token", self.text("contextUsageFigure"))
        self.assertEqual("Otomatik sıkıştırmaya 7 tur", self.text("contextUsageTurns"))
        self.assertEqual("Oturumu sıkıştır", self.named("contextCompactButton").property("label"))
        texts = {i.property("text") for i in descendants(self.block) if i.property("text")}
        self.assertIn("Bağlam kullanımı", texts)
        self.bridge.set({"used": 900, "max": None, "ratio": None, "turnsUntilAutoCompact": None, "state": "unknown"})
        self.assertEqual("900 token / sınır bilinmiyor", self.text("contextUsageFigure"))
        self.bridge.set(dict(UNKNOWN))
        self.assertEqual("Bilinmiyor", self.text("contextUsageFigure"))
        for theme in ("space", "dark"):
            self.window.setTheme(theme)
            pump(20)
            bar, figure = self.named("contextUsageBar"), self.named("contextUsageFigure")
            self.assertNotEqual(bar.property("color"), figure.property("color"))
            self.assertEqual("Bilinmiyor", figure.property("text"))


if __name__ == "__main__":
    unittest.main()
