"""DFTRAY (Salih 2026-10-12): the hide-to-tray notice is one short line and leaves fast.

* The text: en "JARVIS is running in the background.", tr "JARVIS arka planda çalışıyor."
* A 2000 ms timeout hint, and the notice taken down by an empty balloon after 2.5 s
  (Windows 10/11 largely ignores the hint); the icon, its tooltip, menu and double-click
  keep working afterwards.
* Still once per run, never on every X.
"""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_ui import _APP  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QSystemTrayIcon  # noqa: E402

from jarvis.ui import tray  # noqa: E402
from jarvis.ui.i18n import localization  # noqa: E402


class FakeAction:
    def __init__(self, label="", slot=None) -> None:
        self.label, self.enabled, self.slot = label, True, slot

    def setText(self, label) -> None:
        self.label = label

    def text(self) -> str:
        return self.label

    def setEnabled(self, enabled) -> None:
        self.enabled = enabled


class FakeMenu:
    """QMenu's surface as install_tray uses it (the test application has no widgets)."""

    def __init__(self) -> None:
        self.items = []

    def addAction(self, label, slot) -> FakeAction:
        self.items.append(FakeAction(label, slot))
        return self.items[-1]

    def addSeparator(self) -> None:
        self.items.append(FakeAction())

    def actions(self) -> list:
        return self.items


class FakeTray(QObject):
    """QSystemTrayIcon's surface as install_tray uses it, recording every call."""

    activated = Signal(object)
    MessageIcon = QSystemTrayIcon.MessageIcon
    ActivationReason = QSystemTrayIcon.ActivationReason

    @staticmethod
    def isSystemTrayAvailable() -> bool:
        return True

    def __init__(self, icon, parent) -> None:
        super().__init__(parent)
        self.messages, self.visible, self.menu, self.tip = [], False, None, ""

    def setContextMenu(self, menu) -> None:
        self.menu = menu

    def contextMenu(self):
        return self.menu

    def setToolTip(self, text) -> None:
        self.tip = text

    def setIcon(self, icon) -> None:
        pass

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def isVisible(self) -> bool:
        return self.visible

    def showMessage(self, *args) -> None:
        self.messages.append(args)


class FakeBridge(QObject):
    attentionChanged = Signal()
    languageChanged = Signal()
    hiddenToTray = Signal()
    executionState, executionEnabled, quitWarning = "running", True, False
    safety = {"attention": []}

    def setTrayAvailable(self, available) -> None:
        self.trayAvailable = available


class HideNoticeTests(unittest.TestCase):
    def install(self):
        self.timers = []
        self.bridge, self.window = FakeBridge(), MagicMock()
        timer = SimpleNamespace(singleShot=lambda ms, context, fn: self.timers.append((ms, context, fn)))
        timers = patch.object(tray, "QTimer", timer)          # read when the window hides, so kept for the test
        timers.start()
        self.addCleanup(timers.stop)
        with patch("PySide6.QtWidgets.QSystemTrayIcon", FakeTray), patch("PySide6.QtWidgets.QMenu", FakeMenu):
            icon = tray.install_tray(_APP, self.bridge, self.window)
        self.addCleanup(icon.deleteLater)
        return icon

    def test_one_short_line_in_english_and_turkish(self) -> None:
        icon = self.install()
        self.bridge.hiddenToTray.emit()
        self.assertEqual(("JARVIS", "JARVIS is running in the background.", QSystemTrayIcon.MessageIcon.Information, 2000),
                         icon.messages[0])
        localization(_APP).apply("tr-TR")
        self.addCleanup(localization(_APP).apply, "en-US")
        icon = self.install()
        self.bridge.hiddenToTray.emit()
        self.assertEqual(("JARVIS", "JARVIS arka planda çalışıyor.", QSystemTrayIcon.MessageIcon.Information, 2000),
                         icon.messages[0])

    @unittest.skipUnless(sys.platform == "win32", "the empty-balloon removal is a Windows contract")
    def test_the_notice_is_taken_down_at_two_and_a_half_seconds_and_the_icon_keeps_working(self) -> None:
        icon = self.install()
        menu, tip = icon.contextMenu(), icon.tip
        self.bridge.hiddenToTray.emit()
        self.assertEqual(1, len(self.timers))
        ms, context, dismiss = self.timers[0]
        self.assertEqual((2500, icon), (ms, context))
        self.assertEqual(1, len(icon.messages))                                  # nothing taken down yet
        dismiss()
        self.assertEqual(("", "", QSystemTrayIcon.MessageIcon.NoIcon, 0), icon.messages[1])
        # The icon, its tooltip, its menu and double-click are untouched.
        self.assertTrue(icon.isVisible())
        self.assertIs(menu, icon.contextMenu())
        self.assertEqual(tip, icon.tip)
        self.assertEqual(["Open JARVIS", "Freeze JARVIS", "", "Quit JARVIS"], [a.text() for a in menu.actions()])
        icon.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
        self.window.requestActivate.assert_called_once_with()

    def test_still_once_per_run(self) -> None:
        icon = self.install()
        for _ in range(3):
            self.bridge.hiddenToTray.emit()
        self.assertEqual(1, len(icon.messages))
        self.assertEqual(1 if sys.platform == "win32" else 0, len(self.timers))


if __name__ == "__main__":
    unittest.main()
