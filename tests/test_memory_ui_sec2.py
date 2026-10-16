"""SEC2UIQML: the chat header's temporary-chat (memory-off) switch and the
used-memory list under a reply (I21, I22). SEC2BRIDGE gave the bridge
conversationMemoryOff + setConversationMemoryOff (it follows the app's answer)
and a reply's memory fields from the app's reply_memory_sources; the list is
plain text, apart from the web sources, and absent fields show nothing. A
throwaway TEMP database and a scripted model: no live call, never
data\\jarvis.db."""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtCore import QPointF, Qt
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtTest import QTest

from tests import test_ui
from tests.helpers import wait_until
from tests.test_ui import UiTestCase, pump

CARRIER = "![x](https://a.invalid)"


class MemoryUiSec2Tests(UiTestCase):
    # Not imported by name: a TestCase in this module's namespace would run here too.
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def shown(self, root, name):
        found, stack = [], [root.contentItem()]
        while stack:
            node = stack.pop()
            stack.extend(node.childItems())
            if node.objectName() == name and node.isVisible():
                found.append(node)
        return found

    def reply(self, **fields):
        bridge = self.bridge()
        root = self.window(bridge)
        # SEC2BRIDGE: a stored reply's memory fields are then set from the app's
        # reply_memory_sources; here the app answers with the same fields.
        answer = {"usedMemory": [], "memoryNoneRelevant": False, **fields}
        with mock.patch.object(self.backend, "reply_memory_sources", lambda cid, turn: answer):
            bridge._say("jarvis", "Bir yanıt.", chat=True, **fields)
            pump(200)
        return root

    def expand(self, root):
        toggle = self.shown(root, "feedUsedMemoryToggle")[0]
        center = toggle.mapToScene(QPointF(toggle.width() / 2, toggle.height() / 2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        wait_until(lambda: self.shown(root, "feedUsedMemory_0"), 500)

    def test_absent_fields_render_nothing(self) -> None:
        root = self.reply()
        self.assertTrue(self.shown(root, "feedMessage_jarvis"))
        self.assertEqual([], self.shown(root, "feedUsedMemory"))
        self.assertEqual([], self.shown(root, "feedUsedMemoryNone"))

    def test_malformed_fields_render_nothing(self) -> None:
        root = self.reply(usedMemory="not a list", memoryNoneRelevant="yes")
        self.assertEqual([], self.shown(root, "feedUsedMemory"))

    def test_two_records_collapsed_then_two_lines_with_rev(self) -> None:
        records = [{"record_id": "r1", "kind": "rule", "label": "Kısa yanıt ver", "revision": 1},
                   {"record_id": "r2", "kind": "fact", "label": "Proje adı JARVIS", "revision": 3}]
        root = self.reply(usedMemory=records, sources=[{"title": "Sayfa", "url": "https://example.invalid/a"}])
        self.assertEqual(1, len(self.shown(root, "feedUsedMemory")))
        self.assertEqual("Memory used (2)", self.shown(root, "feedUsedMemoryCount")[0].property("text"))
        self.assertEqual([], self.shown(root, "feedUsedMemory_0"))           # collapsed
        self.assertEqual([], self.shown(root, "feedUsedMemoryNone"))
        # Apart from the web sources: those keep their own block, above it.
        sources, used = self.shown(root, "feedSources")[0], self.shown(root, "feedUsedMemory")[0]
        self.assertGreaterEqual(used.mapToScene(QPointF(0, 0)).y(), sources.mapToScene(QPointF(0, sources.height())).y())
        self.expand(root)
        lines = [self.shown(root, f"feedUsedMemory_{i}")[0].property("text") for i in range(2)]
        self.assertEqual([], self.shown(root, "feedUsedMemory_2"))
        self.assertEqual(["rule  ·  Kısa yanıt ver  ·  rev 1", "fact  ·  Proje adı JARVIS  ·  rev 3"], lines)

    def test_none_flag_renders_the_line(self) -> None:
        root = self.reply(memoryNoneRelevant=True)
        none = self.shown(root, "feedUsedMemoryNone")
        self.assertEqual(1, len(none))
        self.assertEqual("No relevant memory", none[0].property("text"))
        self.assertEqual([], self.shown(root, "feedUsedMemoryToggle"))

    def test_a_label_renders_as_plain_text(self) -> None:
        root = self.reply(usedMemory=[{"record_id": "r1", "kind": "fact", "label": CARRIER, "revision": 2}])
        self.expand(root)
        line = self.shown(root, "feedUsedMemory_0")[0]
        # The enum has no Python converter: read it in the delegate's own QML context.
        shown_as = QQmlExpression(QQmlEngine.contextForObject(line), line, "textFormat").evaluate()[0]
        self.assertEqual(Qt.TextFormat.PlainText.value, shown_as)
        self.assertEqual(f"fact  ·  {CARRIER}  ·  rev 2", line.property("text"))

    def test_the_switch_shows_off_until_the_app_says_otherwise(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertTrue(bridge.conversationId)
        self.assertTrue(self.item(root, "chatHeaderGlobalMemory").isVisible())
        switch = self.item(root, "chatHeaderTemporaryChat")
        self.assertTrue(switch.isVisible())
        self.assertIs(True, switch.property("available"))
        self.assertIs(False, switch.property("on"))
        self.assertFalse(self.item(root, "chatHeaderMemoryOffBadge").isVisible())

    def test_with_the_slot_a_click_asks_and_the_badge_shows(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        switch = self.item(root, "chatHeaderTemporaryChat")
        self.assertTrue(switch.isVisible())
        self.assertIs(False, switch.property("on"))
        self.assertFalse(self.item(root, "chatHeaderMemoryOffBadge").isVisible())
        with mock.patch.object(self.backend, "set_conversation_memory_off",
                               wraps=self.backend.set_conversation_memory_off) as asked:
            self.click(root, "chatHeaderTemporaryChat", until=lambda: bridge.conversationMemoryOff)
        asked.assert_called_once_with(bridge.conversationId, True)
        self.assertIs(True, self.backend.conversation_memory_off(bridge.conversationId)["conversation_memory_off"])
        self.assertTrue(wait_until(lambda: self.item(root, "chatHeaderMemoryOffBadge").isVisible(), 500))
        self.assertEqual("No memory", self.item(root, "chatHeaderMemoryOffBadge").property("text"))


if __name__ == "__main__":
    unittest.main()
