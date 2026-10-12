"""V2.1 (Salih 2026-10-11): the window powers down to the core on the minimize
control and powers up from it on every return (WindowPowerFx).

* Our minimize: the effect runs, then ``bridge.minimizeWindow`` - once, within the cap
  (DFWIN: the 520 ms power-down timeline, a safety Timer of the effect + 60 ms); a second
  click adds nothing.
* Ambient effects off: minimize at once, no overlay frame; a return shows the content at once.
* Every return from Minimized or hidden plays the power up; the content ends at exactly 1,
  the overlay ends invisible and is never enabled - also after a native minimize cut a
  power down short (that minimize never fires later).
* A stalled timeline (the window not drawn) still minimizes and still ends at full content.
* Every duration goes through Theme.dur (DFWIN: 520 / 600 ms, one documented K2 exception);
  the live content never travels.
"""
from __future__ import annotations

import re
import time
import unittest

from tests.test_ui import _APP, UiTestCase, find, pump, tap  # noqa: F401 - the offscreen platform and the application
from tests.helpers import wait_until
from tests.test_motion_6g import EXCEPTIONS, QML_DIR, durations, gated

from PySide6.QtCore import SIGNAL, QObject, Qt  # noqa: E402
from PySide6.QtTest import QSignalSpy  # noqa: E402

FX = QML_DIR / "components" / "WindowPowerFx.qml"
CAP = 0.58          # click -> minimize (DFWIN: the 520 ms power down + the 60 ms safety margin)
LATENCY = 0.15      # the event loop under load (the machine is not idle, rule D)


def items(node):
    yield node
    for child in node.childItems():
        yield from items(child)


class WindowPowerFxTests(UiTestCase):
    ambient = True      # THIZ: its subject is motion

    def window(self, ambient: bool = True):
        bridge = self.bridge()
        root = self.load(bridge)
        root.showNormal()
        root.resize(1920, 1080)
        pump(350)
        if not ambient:
            bridge.setAmbientEffects(False)
            pump(50)
        fx = find(root.contentItem(), "windowPowerFx")
        self.assertIsNotNone(fx)
        spy = QSignalSpy(fx, SIGNAL("minimizeNow()"))
        self.spy = spy          # alive for the test
        return bridge, root, fx, spy

    def minimize_control(self, root):
        found = [i for i in items(root.contentItem()) if i.property("icon") == "minimize" and i.isVisible()]
        self.assertEqual(1, len(found))
        return found[0]

    def restore(self, root):
        root.setWindowStates(root.windowStates() & ~Qt.WindowState.WindowMinimized)      # what the taskbar does

    def at_rest(self, fx, ceiling_ms: int = 1500) -> None:
        self.assertTrue(wait_until(lambda: fx.property("phase") == "", ceiling_ms))
        self.assertEqual(1.0, fx.property("contentOpacity"))
        self.assertFalse(fx.isVisible())
        self.assertFalse(fx.isEnabled())

    def test_a_the_minimize_control_powers_down_then_minimizes_once(self) -> None:
        _bridge, root, fx, spy = self.window()
        control = self.minimize_control(root)
        run = fx.findChild(QObject, "windowPowerRun")
        # The effect's state at the minimize call - its own clock, not the wall clock (a loaded
        # CI runner saw 0.195 s of wall time for the 220 ms Qt animation clock).
        at_call = []
        QObject.connect(fx, SIGNAL("minimizeNow()"), lambda: at_call.append(
            (fx.property("phase"), fx.property("t"), fx.property("contentOpacity"), fx.property("shrink"),
             run.property("running"))))
        started = time.monotonic()
        tap(root, control)
        self.assertEqual(("down", True, False, 1), (fx.property("phase"), fx.isVisible(), fx.isEnabled(), fx.property("plays")))
        self.assertEqual(0, spy.count())                                          # not before the effect
        tap(root, control)                                                        # a second click while it runs
        self.assertTrue(wait_until(lambda: spy.count() >= 1, 1000))
        elapsed = time.monotonic() - started
        # The effect ran first: the power-down timeline at its end, the room fully dark, the band shrunk into the core.
        self.assertEqual([("down", 1.0, 0.0, 1.0, False)], at_call)
        self.assertLessEqual(elapsed, CAP + LATENCY)
        self.assertEqual("Minimized", root.visibility().name)
        pump(700)
        self.assertEqual(1, spy.count())                                          # once, never again from the safety Timer
        self.assertEqual(1, fx.property("plays"))
        self.restore(root)
        self.assertEqual(("up", 2), (fx.property("phase"), fx.property("plays")))
        self.at_rest(fx)

    def test_b_ambient_off_minimizes_at_once_with_no_overlay_frame(self) -> None:
        _bridge, root, fx, spy = self.window(ambient=False)
        tap(root, self.minimize_control(root))
        self.assertEqual(1, spy.count())                                          # at once, no added delay
        self.assertEqual("Minimized", root.visibility().name)
        self.assertEqual(("", False, 0), (fx.property("phase"), fx.isVisible(), fx.property("plays")))
        self.restore(root)
        self.assertEqual(("", False, 0, 1.0), (fx.property("phase"), fx.isVisible(), fx.property("plays"),
                                               fx.property("contentOpacity")))  # the content at once
        pump(300)
        self.assertEqual((1, 0), (spy.count(), fx.property("plays")))

    def test_c_every_return_plays_the_power_up_and_ends_at_full_content(self) -> None:
        from jarvis.ui.tray import TrayController
        bridge, root, fx, spy = self.window()
        bridge.setTrayAvailable(True)
        tray = TrayController(bridge, root)
        # DFWIN2: the window's X (hide to tray) powers down first (one play), then hides.
        for name, away, back, downs in (("native minimize", lambda: bridge.minimizeWindow(root), lambda: self.restore(root), 0),
                                        ("hide to tray", root.close, tray.open, 1)):
            with self.subTest(way=name):
                plays = fx.property("plays")
                away()
                if downs:
                    self.assertTrue(wait_until(lambda: not root.isVisible(), 1000))
                    self.assertEqual("down", fx.property("phase"))                # held while hidden
                else:
                    pump(50)
                    self.assertFalse(fx.isVisible())                              # a native minimize: no power down
                self.assertEqual(plays + downs, fx.property("plays"))
                back()
                self.assertEqual(("up", plays + downs + 1), (fx.property("phase"), fx.property("plays")))
                self.assertTrue(fx.isVisible())
                self.assertLess(fx.property("contentOpacity"), 1.0)
                self.assertFalse(fx.isEnabled())
                self.at_rest(fx)
        self.assertEqual(0, spy.count())

    def test_d_a_return_that_cuts_a_power_down_short_ends_at_full_content(self) -> None:
        bridge, root, fx, spy = self.window()
        tap(root, self.minimize_control(root))
        pump(80)
        self.assertEqual("down", fx.property("phase"))
        bridge.minimizeWindow(root)                                               # Win+D / the taskbar mid-run
        self.restore(root)
        self.assertEqual("up", fx.property("phase"))
        self.at_rest(fx)
        pump(700)
        self.assertEqual(0, spy.count())                                          # the cut minimize never fires later
        self.assertNotEqual("Minimized", root.visibility().name)
        self.assertEqual(1.0, fx.property("contentOpacity"))

    def test_a_stalled_timeline_still_minimizes_and_still_ends_at_full_content(self) -> None:
        _bridge, root, fx, spy = self.window()
        run = fx.findChild(QObject, "windowPowerRun")
        self.assertIsNotNone(run)
        started = time.monotonic()
        tap(root, self.minimize_control(root))
        run.setProperty("running", False)                                        # the window not drawn: the timeline stalls
        self.assertTrue(wait_until(lambda: spy.count() >= 1, 1000))
        self.assertLessEqual(time.monotonic() - started, CAP + LATENCY)
        self.assertEqual("Minimized", root.visibility().name)
        self.restore(root)
        run.setProperty("running", False)
        self.assertLess(fx.property("contentOpacity"), 1.0)
        self.at_rest(fx)


class WindowPowerFxSourceTests(unittest.TestCase):
    def test_e_every_duration_goes_through_theme_dur_as_one_documented_k2_exception(self) -> None:
        text = FX.read_text(encoding="utf-8")
        found = list(durations(text))
        self.assertTrue(found)
        for _line, kind, expr in found:
            self.assertTrue(gated(expr), expr)
            inner = re.fullmatch(r'Theme\.dur\(fx\.phase === "up" \? (\d+) : (\d+)\)', expr.strip())
            self.assertIsNotNone(inner, expr)
            self.assertEqual((600, 520), (int(inner.group(1)), int(inner.group(2))), expr)
        # DFWIN (Salih 2026-10-12): richer and slower than K2's 120-220 ms - exactly one EXCEPTIONS row.
        self.assertEqual(1, [e[0] for e in EXCEPTIONS].count("components/WindowPowerFx.qml"))
        # One timeline per run; its segments: the room goes dark over 80-240 ms of a power down, the
        # content opens over 240-500 ms of a power up.
        self.assertIn('readonly property int span: phase === "up" ? 600 : 520', text)
        self.assertIn("readonly property real contentOpacity: down ? 1 - outCubic(seg(80, 240)) : up ? unfold : 1", text)
        self.assertIn("readonly property real unfold: up ? inOutCubic(seg(240, 500)) : 0", text)
        self.assertIn("function outCubic(x) { return 1 - Math.pow(1 - x, 3) }", text)
        safety = int(re.search(r"interval: fx\.span \+ (\d+)", text).group(1))
        self.assertEqual(60, safety)                                              # the effect + 60 ms
        self.assertEqual(520 + safety, round(CAP * 1000))
        # Never interactive, and the live content never travels: no Translate, no scale, no x/y animation of it.
        self.assertIn("enabled: false", text)
        self.assertNotRegex(text, r"Translate|scale:|property: \"[xy]\"|MouseArea|HoverHandler|TapHandler|focus:")

    def test_main_routes_the_minimize_control_through_the_effect(self) -> None:
        main = (QML_DIR / "Main.qml").read_text(encoding="utf-8")
        self.assertIn("onMinimizeRequested: powerFx.powerDown()", main)
        self.assertIn("onMinimizeNow: bridge.minimizeWindow(root)", main)
        self.assertEqual(1, main.count("bridge.minimizeWindow(root)"))


if __name__ == "__main__":
    unittest.main()
