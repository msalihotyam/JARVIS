"""V2.1a M4: the chat header's "all projects' memory" switch (per conversation,
off by default). It shows the app's confirmed answer, a click only asks through
bridge.setGlobalMemoryEnabled, and when on a warning line says other projects'
memory is used read-only and that the conversation can no longer be moved into
a project (I7). Main ('space') and dark themes; the Command preset's header has
no switch. A throwaway TEMP database and a scripted model: no live call, never
data\\jarvis.db."""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtCore import QPointF

from tests.helpers import wait_until
from tests import test_ui
from tests.test_ui import UiTestCase, pump


class GlobalMemoryToggleTests(UiTestCase):
    # Not imported by name: a TestCase in this module's namespace would run here too.
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def open(self):
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertTrue(bridge.conversationId)
        return bridge, root

    def test_visible_default_off_and_no_warning(self) -> None:
        bridge, root = self.open()
        switch = self.item(root, "chatHeaderGlobalMemory")
        self.assertTrue(switch.isVisible())
        self.assertIs(False, bridge.globalMemoryEnabled)
        self.assertIs(False, switch.property("on"))
        self.assertFalse(self.item(root, "chatHeaderGlobalMemoryWarning").isVisible())
        # Told before the click: turning it on is one-way for attaching (I7).
        self.assertIn("never be moved into a project", self.item(root, "chatHeaderGlobalMemoryTip").property("text"))

    def test_click_calls_the_slot_and_the_warning_shows_when_on(self) -> None:
        bridge, root = self.open()
        conversation = bridge.conversationId
        backend = bridge.backend
        with mock.patch.object(backend, "set_global_memory_toggle", wraps=backend.set_global_memory_toggle) as setter:
            self.click(root, "chatHeaderGlobalMemory", until=lambda: bridge.globalMemoryEnabled)
        setter.assert_called_once_with(conversation, True)
        self.assertTrue(self.app_says_enabled(conversation))
        self.assertTrue(wait_until(lambda: self.item(root, "chatHeaderGlobalMemoryWarning").isVisible(), 500))
        self.assertEqual("Other projects' memory is in use, read-only",
                         self.item(root, "chatHeaderGlobalMemoryWarningText").property("text"))
        self.assertEqual("This conversation can no longer be moved into a project",
                         self.item(root, "chatHeaderGlobalMemoryConsequence").property("text"))
        switch = self.item(root, "chatHeaderGlobalMemory")
        self.assertIs(True, switch.property("on"))
        # Laid out: painted, left of the switch, inside the header (a positioner bug once left it 0 x 0).
        warning, header = self.item(root, "chatHeaderGlobalMemoryWarning"), self.item(root, "chatHeader")
        self.assertGreater(warning.width(), 0)
        self.assertGreater(warning.height(), 0)
        right = warning.mapToScene(QPointF(warning.width(), 0)).x()
        self.assertLessEqual(right, switch.mapToScene(QPointF(0, 0)).x() + 0.5)
        self.assertGreaterEqual(warning.mapToScene(QPointF(0, 0)).x(), header.mapToScene(QPointF(0, 0)).x() - 0.5)

    def test_a_refusal_leaves_it_off(self) -> None:
        bridge, root = self.open()
        refused = {"ok": False, "error": "Memory store unavailable."}
        with mock.patch.object(bridge.backend, "set_global_memory_toggle", return_value=refused) as setter:
            self.click(root, "chatHeaderGlobalMemory", until=lambda: setter.called)
        pump(100)
        self.assertIs(False, bridge.globalMemoryEnabled)
        self.assertIs(False, self.item(root, "chatHeaderGlobalMemory").property("on"))
        self.assertFalse(self.item(root, "chatHeaderGlobalMemoryWarning").isVisible())

    def test_space_and_dark_themes_and_not_in_the_command_preset(self) -> None:
        bridge, root = self.open()
        bridge.setGlobalMemoryEnabled(True)
        self.assertTrue(wait_until(lambda: bridge.globalMemoryEnabled, 500))
        for theme in ("space", "dark"):
            bridge.setTheme(theme)
            pump(50)
            self.assertEqual(theme, bridge.theme)
            self.assertTrue(self.item(root, "chatHeaderGlobalMemory").isVisible(), theme)
            self.assertTrue(self.item(root, "chatHeaderGlobalMemoryWarning").isVisible(), theme)
        header = self.item(root, "chatHeader")
        header.setProperty("scopeMenu", True)                 # the Command preset's header
        pump(50)
        self.assertFalse(self.item(root, "chatHeaderGlobalMemory").isVisible())
        self.assertFalse(self.item(root, "chatHeaderGlobalMemoryWarning").isVisible())
        header.setProperty("scopeMenu", False)

    def app_says_enabled(self, conversation_id: str) -> bool:
        payload = self.factory(self.store.load().settings).global_memory_toggle_payload(conversation_id)
        return payload.get("ok") is True and payload.get("enabled") is True


if __name__ == "__main__":
    unittest.main()
