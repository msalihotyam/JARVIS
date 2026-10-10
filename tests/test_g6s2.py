"""Gate 6G S2: freeze key skins.

* Default skin 1 is today's PowerControl: same size, caption, glyph state.
* The skin is a persisted UI preference (1-4) that never touches execution.
* Every skin maps running / freeze_requested / frozen from bridge.executionState
  only - a click never moves the lever - and Resume still asks first.
* Ambient off: the lever jumps to its place, nothing animates.
"""

from __future__ import annotations

import json
import unittest

from tests import test_ui
from tests.test_ui import UiTestCase, pump  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import QPointF  # noqa: E402

from jarvis.ui.preferences import FREEZE_KEYS, UiPreferences, canonical_preferences, validate_preferences  # noqa: E402

UP, HALF, DOWN = 0, 0.4, 1


class FreezeKeyPreferenceTests(unittest.TestCase):
    def test_default_is_the_classic_key_and_bad_values_are_refused(self) -> None:
        self.assertEqual((1, 2, 3, 4), FREEZE_KEYS)
        self.assertEqual(1, canonical_preferences()["freeze_key"])
        for bad in (0, 5, "2", True, 2.0, None):
            with self.subTest(bad=bad):
                self.assertEqual(canonical_preferences(), validate_preferences({**canonical_preferences(), "freeze_key": bad}))
        self.assertEqual(4, validate_preferences({**canonical_preferences(), "freeze_key": 4})["freeze_key"])

    def test_a_document_from_before_the_skins_keeps_every_choice(self) -> None:
        legacy = {**canonical_preferences(), "theme": "dark", "status_indicator": "hud"}
        del legacy["freeze_key"]
        self.assertEqual({**canonical_preferences(), "theme": "dark", "status_indicator": "hud"}, validate_preferences(legacy))
        del legacy["status_indicator"]          # older still: before the status lamp too
        self.assertEqual({**canonical_preferences(), "theme": "dark"}, validate_preferences(legacy))


class FreezeKeyWindowTests(UiTestCase):
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def find(self, root, name):
        def walk(node):
            if node.objectName() == name:
                return node
            for child in node.childItems():
                found = walk(child)
                if found is not None:
                    return found
            return None
        return walk(root.contentItem())

    def shown(self, root, control):
        """The lever position a skin draws, read from the skin itself."""
        skin = control.property("skin")
        if skin == 1:
            return DOWN if control.property("engaged") else UP
        if skin == 2:
            return self.item(root, "freezeKeyRedesign").property("position")
        return self.item(root, "freezeKeySwitch").property("shown")

    def test_default_skin_is_the_classic_key_unchanged(self) -> None:
        bridge = self.bridge()
        self.assertEqual(1, bridge.freezeKeySkin)
        root = self.window(bridge)
        control, label = self.item(root, "killSwitchControl"), self.item(root, "killSwitchLabel")
        self.assertEqual(1, control.property("skin"))
        self.assertEqual((50, 50), (control.width(), control.height()))       # Theme.stripKill, as before
        self.assertTrue(self.item(root, "freezeKeyClassic").isVisible())
        self.assertFalse(self.item(root, "freezeKeyRedesign").isVisible())
        self.assertIsNone(self.find(root, "freezeKeySwitch"))                   # skins 3-4 not even loaded
        self.assertTrue(label.isVisible())
        self.assertEqual("FREEZE", label.property("text"))

    def test_the_skin_persists_from_the_settings_drawer_and_never_touches_execution(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.click(root, "settingsOrb")
        self.click(root, "freezeKey_4")
        self.assertEqual(4, bridge.freezeKeySkin)
        self.assertEqual(4, json.loads(self.prefs_path.read_text(encoding="utf-8"))["freeze_key"])
        self.assertEqual(4, UiPreferences(self.prefs_path).data["freeze_key"])
        self.assertEqual(4, self.item(root, "killSwitchControl").property("skin"))
        self.assertTrue(self.store.load().settings.execution_enabled)
        self.assertEqual("running", bridge.executionState)
        bridge.setFreezeKeySkin(9)               # not a skin: ignored
        self.assertEqual(4, bridge.freezeKeySkin)
        bridge.setFreezeKeySkin(1)
        self.assertEqual(1, UiPreferences(self.prefs_path).data["freeze_key"])
        self.assertEqual(0, self.execute_calls())

    def test_each_skin_maps_the_three_execution_states(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        control, label = self.item(root, "killSwitchControl"), self.item(root, "killSwitchLabel")
        for skin in FREEZE_KEYS:
            with self.subTest(skin=skin):
                bridge.setFreezeKeySkin(skin)
                pump(50)
                self.assertEqual(skin == 1, label.isVisible())               # the caption is skin 1's alone
                seen = [self.shown(root, control)]
                # An operation still on its way to its safe point: the gate is closed, not yet frozen.
                bridge._busy = True
                bridge.freezeJarvis()
                pump(50)
                self.assertEqual("freeze_requested", bridge.executionState)
                seen.append(self.shown(root, control))
                bridge._busy = False
                bridge.stateChanged.emit()
                pump(50)
                self.assertEqual("frozen", bridge.executionState)
                seen.append(self.shown(root, control))
                bridge.resumeJarvis()
                pump(50)
                seen.append(self.shown(root, control))
                # Skin 1 keeps today's two looks (latched only when frozen); the others show the way between.
                expected = [UP, UP, DOWN, UP] if skin == 1 else [UP, HALF, DOWN, UP]
                self.assertEqual(expected, [round(v, 3) for v in seen])

    def test_a_click_never_moves_the_lever_and_resume_still_asks(self) -> None:
        bridge = self.bridge()
        bridge.setFreezeKeySkin(3)
        root = self.window(bridge)
        switch = self.item(root, "freezeKeySwitch")
        bridge.freezeJarvis()
        pump(50)
        self.assertEqual(DOWN, switch.property("shown"))
        confirm = self.item(root, "resumeConfirm")
        self.click(root, "killSwitchControl", then=lambda: (
            self.assertTrue(confirm.property("open")),
            self.assertEqual("frozen", bridge.executionState),
            self.assertEqual(DOWN, switch.property("shown")),                  # the question resumes nothing
        ))
        self.click(root, "keepFrozen")
        self.assertEqual(DOWN, switch.property("shown"))
        self.click(root, "killSwitchControl")
        self.click(root, "resumeJarvis")
        self.assertEqual("running", bridge.executionState)
        self.assertEqual(UP, switch.property("shown"))
        # A freeze the store does not confirm: the lever goes half way, never down.
        self.backend.set_config = lambda key, raw: {"command": "config-set", "error": {"code": "config_io_failed", "message": "store is busy"}}
        self.click(root, "killSwitchControl")
        self.assertEqual("unconfirmed", bridge.executionState)
        self.assertEqual(HALF, round(switch.property("shown"), 3))

    def test_ambient_off_the_lever_jumps_without_motion(self) -> None:
        bridge = self.bridge()
        bridge.setFreezeKeySkin(4)
        root = self.window(bridge)
        switch = self.item(root, "freezeKeySwitch")
        bridge.freezeJarvis()
        self.assertEqual(DOWN, switch.property("shown"))                       # no pump: there was no animation
        bridge.resumeJarvis()
        self.assertEqual(UP, switch.property("shown"))

    def test_tall_skins_fit_the_strip_beside_the_lamp(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        lamp, control = self.item(root, "statusOrb"), self.item(root, "killSwitchControl")
        for skin, size in ((2, 44), (3, 84), (4, 112)):
            with self.subTest(skin=skin):
                bridge.setFreezeKeySkin(skin)
                pump(100)
                self.assertEqual(size, control.height())
                top = control.mapToScene(QPointF(0, 0))
                self.assertGreaterEqual(top.y(), 6 - 0.5)                        # never cut by the window edge
                self.assertLessEqual(lamp.mapToScene(QPointF(lamp.width(), 0)).x(), top.x() + 1)
                self.assertLessEqual(control.mapToScene(QPointF(control.width(), 0)).x(), 1920)


class FreezeKeyMotionTests(UiTestCase):
    ambient = True      # its subject is the lever's motion

    def test_ambient_on_the_lever_moves_and_lands_on_the_state(self) -> None:
        bridge = self.bridge()
        bridge.setFreezeKeySkin(4)
        root = test_ui.PresentationTests.window(self, bridge)
        switch = test_ui.PresentationTests.item(self, root, "freezeKeySwitch")
        bridge.freezeJarvis()
        self.assertEqual(UP, switch.property("shown"))                         # it starts from where it was
        pump(800)
        self.assertEqual(DOWN, switch.property("shown"))


if __name__ == "__main__":
    unittest.main()
