"""Gate 6G placement (G6TB1B): the mechanical status lamp in the top bar, the
brand-only top card (U1) and the usage ring under every composer (U2/U3).

* S1: "mechanical" is a status-indicator skin and the DEFAULT; a saved choice
  wins and the older skins stay selectable. Its slot is 120 px (the orbs keep
  theirs), its top 4 px under the window edge, overlapping the right column's
  top by design (~20 px) and nothing else.
* K3: running & idle -> ready, running & busy -> running, freeze requested ->
  freezing, frozen -> frozen; unconfirmed (and refused / unknown) stands still
  like frozen - never "ready".
* U1: the top card names the provider's brand only: no model, no usage, no action.
* U2/U3: the usage ring sits right of the Model and Effort chips in each
  composer and shows only the provider-reported rows (G6MP1: bridge.usageLimits).

No test opens the live DB or makes a live call.
"""
from __future__ import annotations

import json
import unittest

from tests import test_ui as ui
from tests.test_ui import pump

from PySide6.QtCore import QObject, QPointF  # noqa: E402

from jarvis.ui.preferences import STATUS_INDICATORS, UiPreferences, canonical_preferences, validate_preferences  # noqa: E402


def box(item):
    p0 = item.mapToScene(QPointF(0, 0))
    p1 = item.mapToScene(QPointF(item.width(), item.height()))
    return p0.x(), p0.y(), p1.x(), p1.y()


def find(node, name):
    if node.objectName() == name:
        return node
    for child in node.childItems():
        found = find(child, name)
        if found is not None:
            return found
    return None


def descendants(node):
    for child in node.childItems():
        yield child
        yield from descendants(child)


class PlacementCase(ui.UiTestCase):
    item = ui.PresentationTests.item
    window = ui.PresentationTests.window
    click = ui.PresentationTests.click
    providers = ui.ModelCardTests.providers
    CLAUDE_MODELS = ui.ModelCardTests.CLAUDE_MODELS


class StatusSkinTests(PlacementCase):
    def test_mechanical_is_the_default_a_saved_choice_wins_old_skins_stay(self) -> None:
        self.assertEqual(("orb", "hud", "mechanical"), STATUS_INDICATORS)
        self.assertEqual("mechanical", canonical_preferences()["status_indicator"])
        for skin in STATUS_INDICATORS:     # every skin survives validation as chosen
            self.assertEqual(skin, validate_preferences({**canonical_preferences(), "status_indicator": skin})["status_indicator"])
        bridge = self.bridge()
        self.assertEqual("mechanical", bridge.statusIndicator)
        root = self.window(bridge)
        self.assertTrue(self.item(root, "mechanicalLamp").isVisible())
        self.assertFalse(self.item(root, "statusOrb").isVisible())
        # The older skins stay selectable, by mouse, in Settings; the choice persists.
        self.click(root, "settingsOrb")
        self.click(root, "statusIndicator_hud")
        self.assertEqual("hud", bridge.statusIndicator)
        self.assertEqual("hud", json.loads(self.prefs_path.read_text(encoding="utf-8"))["status_indicator"])
        self.assertEqual("hud", UiPreferences(self.prefs_path).data["status_indicator"])
        orb = self.item(root, "statusOrb")
        self.assertTrue(orb.isVisible())
        self.assertEqual("hud", orb.property("skin"))
        self.assertIsNone(find(root.contentItem(), "mechanicalLamp"))
        self.click(root, "statusIndicator_orb")
        self.assertEqual("orb", UiPreferences(self.prefs_path).data["status_indicator"])
        self.click(root, "statusIndicator_mechanical")
        self.assertEqual("mechanical", UiPreferences(self.prefs_path).data["status_indicator"])
        self.assertTrue(self.item(root, "mechanicalLamp").isVisible())
        self.assertTrue(self.store.load().settings.execution_enabled)
        self.assertEqual(0, self.execute_calls())

    def test_slot_geometry_overlaps_only_the_right_column_top(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for width, height in ((1920, 1080), (1280, 760)):
            with self.subTest(size=f"{width}x{height}"):
                bridge.setStatusIndicator("mechanical")
                root.resize(width, height)
                pump(300)
                self.assertEqual(120, self.item(root, "statusSlot").width())
                lamp = box(self.item(root, "mechanicalLamp"))
                self.assertAlmostEqual(4, lamp[1], delta=0.5)
                self.assertAlmostEqual(120, lamp[3] - lamp[1], delta=0.5)
                # Beside the brand card and the freeze key, never over them, inside the window.
                card, kill = box(self.item(root, "brandCard")), box(self.item(root, "killSwitchControl"))
                self.assertLessEqual(card[2], lamp[0] + 0.5)
                self.assertLessEqual(lamp[2], kill[0] + 0.5)
                self.assertLessEqual(kill[2], width)
                # The designed overlap with the right column's top edge: about 20 px.
                column = box(self.item(root, "panelColumn"))
                self.assertLess(column[0], lamp[2])
                self.assertGreater(column[2], lamp[0])
                self.assertGreater(lamp[3] - column[1], 10)
                self.assertLessEqual(lamp[3] - column[1], 30)
                # The orbs keep their own instrument slot.
                bridge.setStatusIndicator("orb")
                pump(100)
                self.assertEqual(64, self.item(root, "statusSlot").width())
                orb, kill = box(self.item(root, "statusOrb")), box(self.item(root, "killSwitchControl"))
                self.assertLessEqual(orb[2], kill[0] + 1)
                self.assertLessEqual(card[2], orb[0] + 0.5)


class LampStateTests(PlacementCase):
    def lamp(self, root):
        pump(30)
        return self.item(root, "mechanicalLamp")

    def test_state_map_and_unconfirmed_stands_still(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual(("running", False), (bridge.executionState, bridge.busy))
        self.assertEqual("ready", self.lamp(root).property("executionState"))
        bridge._busy = True                       # a run is active
        bridge.stateChanged.emit()
        self.assertEqual("running", self.lamp(root).property("executionState"))
        bridge._execution_request = "freeze"      # a freeze on its way to a safe point
        bridge.stateChanged.emit()
        self.assertEqual("freeze_requested", bridge.executionState)
        self.assertEqual("freezing", self.lamp(root).property("executionState"))
        bridge._execution_request = None
        bridge._busy = False
        bridge.setExecutionEnabled(False)
        self.assertEqual("frozen", bridge.executionState)
        self.assertEqual("frozen", self.lamp(root).property("executionState"))
        bridge.setExecutionEnabled(True)
        self.assertEqual("ready", self.lamp(root).property("executionState"))
        # A freeze the store does not confirm: the lamp stands still (no drive), never "ready".
        self.backend.set_config = lambda key, raw: {"command": "config-set", "error": {"code": "config_io_failed", "message": "busy"}}
        bridge.freezeJarvis()
        self.assertEqual("unconfirmed", bridge.executionState)
        lamp = self.lamp(root)
        self.assertEqual("frozen", lamp.property("executionState"))
        self.assertEqual(0, lamp.property("target").toVariant()["s"])
        self.assertEqual(0, self.execute_calls())


class BrandCardTests(PlacementCase):
    def test_top_card_names_only_the_brand_and_does_nothing(self) -> None:
        bridge = self.bridge()
        self.providers(bridge)
        bridge._claude_limits = {"seen_at": 10, "windows": {"five_hour": {"utilization": 0.08, "resets_at": 1789560046},
                                                            "seven_day": {"utilization": 0.26, "resets_at": 1789814410}}}
        bridge.applyModel("claude-sonnet-5", "high")
        root = self.window(bridge)
        self.assertEqual(2, len(bridge.modelCard["quota"]))       # usage WAS reported ...
        card = self.item(root, "brandCard")
        self.assertEqual("ANTHROPIC", self.item(root, "brandName").property("text"))
        self.assertIsNotNone(self.item(root, "brandSocket"))
        texts = sorted(str(n.property("text")) for n in descendants(card) if n.property("text"))
        # ... and the card shows none of it, nor the model: the socket, the brand and its hover tip.
        self.assertEqual(["AI", "ANTHROPIC", "Selected provider"], texts)
        for gone in ("modelTrigger", "selectorModelText", "selectorMetadataText", "selectorQuota"):
            self.assertIsNone(find(root.contentItem(), gone), gone)
        # Not clickable: a click opens nothing.
        self.click(root, "brandCard")
        # G6MP1: the Model panel it once opened is retired - nothing opens, no panel exists.
        self.assertFalse(any(root.property(key) for key in ("settingsOpen", "navOpen")))
        self.assertIsNone(find(root.contentItem(), "modelSurface"))
        ring = find(self.item(root, "canonicalComposer"), "usageRing")
        pop = ring.findChild(QObject, "usagePopover")
        self.assertFalse(pop.property("visible"))
        bridge.setChatProvider("codex")
        pump(50)
        self.assertEqual("OPENAI", self.item(root, "brandName").property("text"))


class UsageRingPlacementTests(PlacementCase):
    def test_ring_sits_right_of_the_chips_in_each_composer(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for theme, composer in (("space", "canonicalComposer"), ("command", "commandComposer")):
            with self.subTest(composer=composer):
                bridge.setTheme(theme)
                pump(300)
                host = self.item(root, composer)
                ring, row, effort = find(host, "usageRing"), find(host, "composerChipRow"), find(host, "effortMenuChip")
                self.assertIsNotNone(ring)
                self.assertTrue(ring.isVisible())
                self.assertAlmostEqual(box(row)[2], box(ring)[2], delta=1)       # the row's right end
                self.assertLessEqual(box(effort)[2], box(ring)[0] + 0.5)          # right of Effort
                limits = ring.property("limits")
                self.assertEqual("claude", (limits.toVariant() if hasattr(limits, "toVariant") else limits)["provider"])   # G6MP1: bridge.usageLimits
                self.assertEqual("–", find(ring, "usageRingText").property("text"))   # nothing reported, nothing estimated

    def test_popover_rows_are_the_reported_quota_and_open_above(self) -> None:
        bridge = self.bridge()
        self.providers(bridge)
        bridge._claude_limits = {"seen_at": 10, "windows": {"five_hour": {"utilization": 0.08, "resets_at": 1789560046},
                                                            "seven_day": {"utilization": 0.26, "resets_at": 1789814410}}}
        bridge.applyModel("claude-sonnet-5", "high")
        root = self.window(bridge)
        ring = find(self.item(root, "canonicalComposer"), "usageRing")
        self.assertEqual(bridge.usageLimits["rows"], ring.property("rows"))     # G6MP1: every reported window
        self.assertEqual(bridge.modelCard["quota"], ring.property("rows"))      # (here the same two as the card)
        self.assertEqual("8%", find(ring, "usageRingText").property("text"))
        center = ring.mapToScene(QPointF(ring.width() / 2, ring.height() / 2)).toPoint()
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        pump(300)
        pop = ring.findChild(QObject, "usagePopover")
        self.assertTrue(pop.property("visible"))
        body = pop.property("contentItem")
        title = next(n for n in descendants(body) if n.objectName() == "usagePopoverTitle")
        self.assertEqual("Plan usage limits", title.property("text"))           # G6MP1: Claude reported no plan name
        used = {n.objectName(): next(t.property("text") for t in descendants(n) if t.objectName() == "usageRowUsed")
                for n in descendants(body) if n.objectName().startswith("usageRow_") and n.isVisible()}
        self.assertEqual({"usageRow_five_hour": "8%", "usageRow_weekly": "26%"}, used)
        self.assertLess(box(body)[3], box(ring)[1] + 1)                     # it opens above the ring
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        pump(300)
        self.assertFalse(pop.property("visible"))
        self.assertEqual(0, self.execute_calls())


if __name__ == "__main__":
    unittest.main()
