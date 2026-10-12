"""DFWIN (Salih 2026-10-12, dogfood item 9): the window power-down / power-up v2 -
a CRT collapse of a frozen snapshot into the core, and the core igniting and unfolding
over the live content (WindowPowerFx).

* The power-down collapses a snapshot (a ShaderEffectSource, ``live: false``) of the
  window, never the live items; the snapshot is released after every run.
* The end of a power-up is clean: nothing of the effect is left - no stage (the visuals'
  Loader is inactive), no snapshot, no bloom layer, the overlay invisible and never
  enabled, the content at exactly 1 and the live content untouched (no transform, no
  layer, opacity 1).
"""
from __future__ import annotations

from tests.test_ui import _APP, UiTestCase, find, pump, tap  # noqa: F401 - the offscreen platform and the application
from tests.helpers import wait_until
from tests.test_v21win import items

from PySide6.QtCore import SIGNAL, QObject, Qt  # noqa: E402
from PySide6.QtTest import QSignalSpy  # noqa: E402


class WindowPowerV2Tests(UiTestCase):
    ambient = True      # THIZ: its subject is motion

    def window(self):
        bridge = self.bridge()
        root = self.load(bridge)
        root.showNormal()
        root.resize(1920, 1080)
        pump(350)
        fx = find(root.contentItem(), "windowPowerFx")
        self.assertIsNotNone(fx)
        self.spy = QSignalSpy(fx, SIGNAL("minimizeNow()"))
        self.live = self.live_state(root, fx)
        return bridge, root, fx

    def live_state(self, root, fx):
        """The window's own items (everything but the effect): geometry, scale, turn, opacity."""
        return [(c.metaObject().className(), c.x(), c.y(), c.width(), c.height(), c.scale(), c.rotation(), c.opacity())
                for c in root.contentItem().childItems() if c is not fx]

    def stage(self, fx):
        loader = fx.findChild(QObject, "windowPowerStage")
        self.assertIsNotNone(loader)
        return loader

    def assert_clean(self, root, fx) -> None:
        self.assertEqual(("", 1.0, False, False), (fx.property("phase"), fx.property("contentOpacity"),
                                                    fx.isVisible(), fx.isEnabled()))
        loader = self.stage(fx)
        self.assertEqual((False, None), (loader.property("active"), loader.property("item")))
        self.assertFalse(fx.property("snapshotHeld"))
        self.assertIsNone(find(root.contentItem(), "windowPowerSnapshot"))   # released
        self.assertIsNone(find(root.contentItem(), "windowPowerBloom"))      # no effect layer left
        # The live content was never touched: the window at full opacity, no scale / turn / layer on
        # it, its own items exactly as before the run.
        content = root.contentItem()
        self.assertEqual((1.0, 1.0, 0.0), (content.opacity(), content.scale(), content.rotation()))
        self.assertEqual(self.live, self.live_state(root, fx))
        self.assertTrue(all(state[5:7] == (1.0, 0.0) for state in self.live))

    def restore(self, root):
        root.setWindowStates(root.windowStates() & ~Qt.WindowState.WindowMinimized)

    def test_the_power_down_collapses_a_frozen_snapshot_of_the_window_and_releases_it(self) -> None:
        _bridge, root, fx = self.window()
        self.assertFalse(fx.property("snapshots"))            # the software scene graph never snapshots (cost)
        fx.setProperty("snapshots", True)                      # the GPU path, as in the packaged app
        control = [i for i in items(root.contentItem()) if i.property("icon") == "minimize" and i.isVisible()][0]
        tap(root, control)
        snapshot = find(root.contentItem(), "windowPowerSnapshot")
        self.assertIsNotNone(snapshot)
        self.assertEqual((False, True, root.contentItem()),
                         (snapshot.property("live"), snapshot.property("recursive"), snapshot.property("sourceItem")))
        run = fx.findChild(QObject, "windowPowerRun")
        self.assertTrue(run.property("running"))                               # the timeline starts at the click
        self.assertTrue(wait_until(lambda: fx.property("grabbed"), 500))      # the visuals once the snapshot is drawn
        self.assertTrue(wait_until(lambda: self.spy.count() == 1, 1500))
        self.assertEqual("Minimized", root.visibility().name)
        # Held while minimized: the dark room and the ember, the snapshot already gone.
        self.assertEqual(("down", 0.0, False), (fx.property("phase"), fx.property("contentOpacity"),
                                                fx.property("snapshotHeld")))
        self.assertIsNone(find(root.contentItem(), "windowPowerSnapshot"))
        self.restore(root)
        self.assertEqual("up", fx.property("phase"))
        self.assertIsNone(find(root.contentItem(), "windowPowerSnapshot"))   # a power up never snapshots
        self.assertTrue(wait_until(lambda: fx.property("phase") == "", 1500))
        self.assert_clean(root, fx)

    def test_the_end_of_every_power_up_is_clean(self) -> None:
        bridge, root, fx = self.window()
        self.assert_clean(root, fx)                                           # before any run
        for run in range(2):
            with self.subTest(run=run):
                bridge.minimizeWindow(root)                                   # a native minimize
                pump(50)
                self.restore(root)
                self.assertEqual("up", fx.property("phase"))
                self.assertIsNotNone(self.stage(fx).property("item"))
                self.assertTrue(wait_until(lambda: fx.property("phase") == "", 1500))
                self.assert_clean(root, fx)
        self.assertEqual(0, self.spy.count())

    def test_the_power_up_ignites_the_core_then_unfolds_the_content(self) -> None:
        _bridge, root, fx = self.window()
        fx.setProperty("phase", "up")
        expect = {0: (0.0, 0.0, 0.25), 200: (None, 0.0, None), 300: (1.0, None, None), 600: (1.0, 1.0, 0.0)}
        for ms, (extend, unfold, flare) in expect.items():
            fx.setProperty("t", ms / 600)
            with self.subTest(ms=ms):
                if extend is not None:
                    self.assertAlmostEqual(extend, fx.property("extend"))
                if unfold is not None:
                    self.assertAlmostEqual(unfold, fx.property("unfold"))
                if flare is not None:
                    self.assertAlmostEqual(flare, fx.property("flare"))
        fx.setProperty("t", 200 / 600)
        self.assertTrue(0 < fx.property("extend") < 1)                       # the line on its way out
        fx.setProperty("t", 0)
        fx.setProperty("phase", "")
        pump(50)
        self.assert_clean(root, fx)
