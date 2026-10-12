"""DFWIN2B (Salih 2026-10-12 01:41: the power effect on close and quit too): the tray's
Quit with no work active, the window on screen, plays the DFWIN2 power-down before the
quit - QApplication.quit never reaches QML onClosing, so TrayController.quit asks the
window's WindowPowerFx itself; its quitNow confirms the quit, once.

* Window on screen, no work: the power-down, then the quit once within the effect + 60 ms.
* Hidden to the tray or minimized (nothing on screen): the quit at once, no effect.
* Work active: unchanged - the dialog first, the effect after the accept, the quit once.
* No WindowPowerFx in the window: the quit at once.
* Ambient off: the quit at once, no frame.
"""
from __future__ import annotations

import time

from tests.test_ui import _APP, UiTestCase, find, pump, tap  # noqa: F401 - the offscreen platform and the application
from tests.helpers import wait_until
from tests import test_dfwin2   # the module, not its class: unittest would collect that class here again
from tests.test_v21win import CAP, LATENCY


class TrayQuitPowerDownTests(UiTestCase):
    ambient = True      # THIZ: its subject is motion
    window = test_dfwin2.WindowPowerCloseTests.window

    def test_tray_quit_with_the_window_on_screen_powers_down_then_quits_once(self) -> None:
        from jarvis.ui.tray import TrayController
        bridge, root, fx = self.window()
        started = time.monotonic()
        TrayController(bridge, root).quit()
        self.assertEqual(("down", 1, []), (fx.property("phase"), fx.property("plays"), self.quits))
        self.assertTrue(wait_until(lambda: bool(self.quits), 1000))
        self.assertLessEqual(time.monotonic() - started, CAP + LATENCY)
        self.assertEqual([("down", 1.0, 1.0)], self.quits)              # the band shrunk into the core, then the quit
        pump(700)
        self.assertEqual((1, True, "down", 1), (len(self.quits), bridge.quitting, fx.property("phase"), fx.property("plays")))

    def test_tray_quit_with_nothing_on_screen_quits_at_once(self) -> None:
        from jarvis.ui.tray import TrayController
        for away in ("hidden", "minimized"):
            with self.subTest(away=away):
                bridge, root, fx = self.window()
                root.hide() if away == "hidden" else root.showMinimized()
                pump(100)
                TrayController(bridge, root).quit()
                self.assertEqual((1, True, 0), (len(self.quits), bridge.quitting, fx.property("plays")))

    def test_tray_quit_with_work_active_keeps_the_confirmation_first(self) -> None:
        from jarvis.ui.tray import TrayController
        bridge, root, fx = self.window()
        bridge._busy = True
        TrayController(bridge, root).quit()
        pump(200)
        self.assertEqual(("", 0, []), (fx.property("phase"), fx.property("plays"), self.quits))  # the dialog, no effect
        tap(root, find(root.contentItem(), "confirmAccept"))
        self.assertEqual(("down", []), (fx.property("phase"), self.quits))
        self.assertTrue(wait_until(lambda: bool(self.quits), 1000))
        pump(700)
        self.assertEqual((1, 1), (len(self.quits), fx.property("plays")))
        bridge._busy = False

    def test_tray_quit_without_the_effect_in_the_window_quits_at_once(self) -> None:
        from jarvis.ui.tray import TrayController
        bridge, root, fx = self.window()
        fx.setObjectName("")
        try:
            TrayController(bridge, root).quit()
            self.assertEqual((1, 0), (len(self.quits), fx.property("plays")))
        finally:
            fx.setObjectName("windowPowerFx")

    def test_tray_quit_with_ambient_off_quits_at_once(self) -> None:
        from jarvis.ui.tray import TrayController
        bridge, root, fx = self.window(ambient=False)
        TrayController(bridge, root).quit()
        self.assertEqual((1, "", 0), (len(self.quits), fx.property("phase"), fx.property("plays")))
        fx.powerOff("quit")                                             # DFWIN2's own ambient-off path: also at once
        self.assertEqual((2, "", 0), (len(self.quits), fx.property("phase"), fx.property("plays")))
