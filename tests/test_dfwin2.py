"""DFWIN2 (Salih 2026-10-12: "kucelip buyurken efekt yaptik ama kapanisina ve simgelerden
geri acilisina yapmadik, yapalim"): the DFWIN power-down also plays on the window's close
and on a confirmed quit; the power-up on every return (WindowPowerFx).

* X (any close that hides JARVIS to the tray): the power-down, then the hide exactly as
  before - once; a second close while it runs adds nothing; a close outranks a pending
  minimize.
* Without a tray the close is the quit: the power-down, then the window closes.
* A confirmed quit (the tray's Quit with work active): the power-down after the
  confirmation, then the orderly quit - once, within the effect + 60 ms.
* A stalled timeline still hides (the safety Timer).
* Ambient off: close, hide and quit at once, no frame.
* Tray Open after the hide plays the power-up and ends clean.
"""
from __future__ import annotations

import time

from tests.test_ui import _APP, UiTestCase, find, pump, tap  # noqa: F401 - the offscreen platform and the application
from tests.helpers import wait_until
from tests.test_v21win import CAP, LATENCY

from PySide6.QtCore import SIGNAL, QObject  # noqa: E402


class WindowPowerCloseTests(UiTestCase):
    ambient = True      # THIZ: its subject is motion

    def window(self, ambient: bool = True, tray: bool = True):
        bridge = self.bridge()
        root = self.load(bridge)
        root.showNormal()
        root.resize(1920, 1080)
        pump(350)
        if not ambient:
            bridge.setAmbientEffects(False)
            pump(50)
        bridge.setTrayAvailable(tray)
        fx = find(root.contentItem(), "windowPowerFx")
        self.assertIsNotNone(fx)
        self.hidden, self.quits, self.minimized = [], [], []
        bridge.hiddenToTray.connect(lambda: self.hidden.append((fx.property("phase"), fx.property("t"), fx.property("shrink"))))
        bridge.quitRequested.connect(lambda: self.quits.append((fx.property("phase"), fx.property("t"), fx.property("shrink"))))
        QObject.connect(fx, SIGNAL("minimizeNow()"), lambda: self.minimized.append(True))
        return bridge, root, fx

    def assert_clean(self, root, fx) -> None:
        self.assertTrue(wait_until(lambda: fx.property("phase") == "", 1500))
        self.assertEqual((1.0, False, False), (fx.property("contentOpacity"), fx.isVisible(), fx.isEnabled()))
        self.assertFalse(fx.findChild(QObject, "windowPowerStage").property("active"))
        self.assertIsNone(find(root.contentItem(), "windowPowerSnapshot"))
        self.assertEqual("", fx.property("finish"))
        self.assertFalse(fx.property("pending"))

    def test_x_powers_down_then_hides_to_the_tray_once_and_tray_open_powers_up_clean(self) -> None:
        from jarvis.ui.tray import TrayController
        _bridge, root, fx = self.window()
        started = time.monotonic()
        root.close()                                                    # the X (and Alt+F4)
        self.assertEqual(("down", 1, True), (fx.property("phase"), fx.property("plays"), root.isVisible()))
        self.assertEqual([], self.hidden)                               # not before the effect
        root.close()                                                    # a second close while it runs
        self.assertTrue(wait_until(lambda: not root.isVisible(), 1000))
        self.assertLessEqual(time.monotonic() - started, CAP + LATENCY)
        self.assertEqual([("down", 1.0, 1.0)], self.hidden)             # the band shrunk into the core, then the hide
        pump(700)
        self.assertEqual((1, 1, []), (len(self.hidden), fx.property("plays"), self.quits))
        self.assertEqual("down", fx.property("phase"))                  # held while hidden
        TrayController(_bridge, root).open()
        self.assertTrue(root.isVisible())
        self.assertEqual(("up", 2), (fx.property("phase"), fx.property("plays")))
        self.assert_clean(root, fx)
        self.assertEqual([], self.minimized)

    def test_a_close_outranks_a_pending_minimize(self) -> None:
        _bridge, root, fx = self.window()
        fx.powerDown()                                                  # the minimize control
        root.close()
        self.assertEqual(1, fx.property("plays"))
        self.assertTrue(wait_until(lambda: not root.isVisible(), 1000))
        self.assertEqual(([], 1), (self.minimized, len(self.hidden)))
        self.assertNotEqual("Minimized", root.visibility().name)

    def test_without_a_tray_the_close_powers_down_then_closes(self) -> None:
        _bridge, root, fx = self.window(tray=False)
        root.close()
        self.assertTrue(root.isVisible())
        self.assertEqual("down", fx.property("phase"))
        self.assertTrue(wait_until(lambda: not root.isVisible(), 1000))
        self.assertEqual(([], 1), (self.hidden, fx.property("plays")))  # closed, not hidden to a tray

    def test_a_confirmed_quit_powers_down_after_the_confirmation_then_quits_once(self) -> None:
        from jarvis.ui.tray import TrayController
        bridge, root, fx = self.window()
        bridge._busy = True                                             # work active: Quit asks first
        TrayController(bridge, root).quit()
        pump(200)
        self.assertEqual(("", 0, []), (fx.property("phase"), fx.property("plays"), self.quits))  # the dialog is never delayed
        confirm = find(root.contentItem(), "confirmAccept")
        self.assertIsNotNone(confirm)
        started = time.monotonic()
        tap(root, confirm)                                              # the confirmation
        self.assertEqual(("down", []), (fx.property("phase"), self.quits))
        self.assertTrue(wait_until(lambda: bool(self.quits), 1000))
        self.assertLessEqual(time.monotonic() - started, CAP + LATENCY)
        self.assertEqual([("down", 1.0, 1.0)], self.quits)
        pump(700)
        self.assertEqual((1, True, "down"), (len(self.quits), bridge.quitting, fx.property("phase")))  # dark to the end
        bridge._busy = False

    def test_a_stalled_timeline_still_hides_to_the_tray(self) -> None:
        _bridge, root, fx = self.window()
        started = time.monotonic()
        root.close()
        fx.findChild(QObject, "windowPowerRun").setProperty("running", False)   # the window not drawn
        self.assertTrue(wait_until(lambda: not root.isVisible(), 1000))
        self.assertLessEqual(time.monotonic() - started, CAP + LATENCY)
        self.assertEqual(1, len(self.hidden))

    def test_ambient_off_closes_hides_and_quits_at_once(self) -> None:
        bridge, root, fx = self.window(ambient=False)
        root.close()
        self.assertFalse(root.isVisible())                              # at once, no frame
        self.assertEqual((1, "", 0), (len(self.hidden), fx.property("phase"), fx.property("plays")))
        root.show()
        fx.powerOff("quit")
        self.assertEqual((1, "", 0), (len(self.quits), fx.property("phase"), fx.property("plays")))
        bridge.setTrayAvailable(False)
        root.close()
        self.assertFalse(root.isVisible())
        self.assertEqual((1, 0), (len(self.hidden), fx.property("plays")))


class MainRoutingTests(UiTestCase):
    def test_main_routes_the_close_and_the_confirmed_quit_through_the_effect(self) -> None:
        from tests.test_motion_6g import QML_DIR
        main = (QML_DIR / "Main.qml").read_text(encoding="utf-8")
        self.assertIn("if (powerFx.holdClose()) { close.accepted = false; return }", main)
        self.assertIn('function() { powerFx.powerOff("quit") }', main)
        self.assertIn("onQuitNow: bridge.confirmQuit()", main)
        self.assertEqual(1, main.count("bridge.confirmQuit()"))
