"""Gate 6G composer slice 1 (G6CM1): C1, M2, M3.

* C1: "+" and the permission-mode chip sit in the row UNDER the composer bar,
  on the left ("+" first); the Model and Effort chips on the right of that row.
* M2: the send key lifts with a glow and a ring under the pointer; while JARVIS
  runs it is the stop key in the same spot (amber square + ring) and cancels the
  running call; a quiet "JARVIS is working…" line shows in the row. Nothing else.
* M3: the Model mini menu - the selected model on top, the models each CLI
  reported grouped by brand, one click switches model AND provider, a
  "Default" tag, a check on the selected; "More models" lists the older ones.
  The Effort popover: one stop per supported level, a draggable handle that
  snaps, a click jumps, the title follows live, "Recommended" under the
  provider's default level; written once, on release, as the panel writes it.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import unittest

from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtTest import QTest

from tests.helpers import wait_until
from tests.test_projects_ui_6f import F6Case
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE = {"id": "claude", "vendor": "Anthropic", "name": "Claude", "status": "available", "research": True, "models": [
    {"id": "default", "name": "Default", "resolved": "claude-opus-5", "description": "", "efforts": EFFORTS, "default_effort": "high"},
    {"id": "claude-opus-5", "name": "Opus 5", "description": "", "efforts": EFFORTS, "default_effort": "high"},
    {"id": "claude-opus-4-7", "name": "Opus 4.7", "description": "", "efforts": EFFORTS},
    {"id": "haiku", "name": "Haiku", "resolved": "claude-haiku-4-5-20251001", "description": "", "efforts": []}]}
CODEX = {"id": "codex", "vendor": "OpenAI", "name": "Codex", "status": "available", "plan": "plus", "research": False, "models": [
    {"id": "gpt-5.6-luna", "name": "GPT-5.6-Luna", "efforts": ["low", "medium"], "default_effort": "medium"},
    {"id": "gpt-5.5-luna", "name": "GPT-5.5-Luna", "efforts": ["low", "medium"], "default_effort": "low"}]}


class ComposerCase(F6Case):
    def open_window(self):
        bridge = self.bridge()
        self.fake_providers(bridge, [CLAUDE, CODEX])
        root = self.window(bridge)
        return bridge, root

    def centre(self, item) -> QPointF:
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2))

    def is_open(self, popup) -> bool:
        return bool(popup.property("opened")) and popup.property("opacity") == 1.0

    def open_menu(self, root, chip, name):
        menu = self.popup_named(root, name)
        self.tap(root, self.item(root, chip), until=lambda: self.is_open(menu))
        self.assertTrue(self.is_open(menu), name)
        return menu

    def row(self, popup, name):
        found = [i for i in self.descendants(popup.property("contentItem")) if i.objectName() == name and i.isVisible()]
        self.assertEqual(1, len(found), name)
        return found[0]


class RowLayoutTests(ComposerCase):
    def test_c1_attach_then_mode_on_the_left_under_the_bar_model_and_effort_on_the_right(self) -> None:
        bridge, root = self.open_window()
        composer = self.item(root, "canonicalComposer")
        row = self.rect(self.item(root, "composerChipRow"))
        bar = self.rect(self.item(root, "composerInput").parentItem().parentItem().parentItem())   # input -> flickable -> bar
        attach, mode = self.rect(self.item(root, "attachControl")), self.rect(self.item(root, "permissionModeControl"))
        model, effort = self.rect(self.item(root, "modelMenuChip")), self.rect(self.item(root, "effortMenuChip"))
        self.assertLessEqual(bar.bottom(), row.top() + 0.5)                       # the row is UNDER the bar
        self.assertAlmostEqual(self.rect(composer).bottom(), row.bottom(), delta=0.5)
        for chip in (attach, mode, model, effort):
            self.assertTrue(row.contains(chip.center()))
        self.assertLess(attach.right(), mode.left())                              # "+" first, then the mode
        self.assertLess(mode.right(), row.center().x())                           # left side
        self.assertLess(model.right(), effort.left() + 0.5)                       # Model, then Effort
        self.assertGreater(model.left(), row.center().x())                        # right side
        # 6G U2 (G6TB1B): the usage ring follows Effort and ends the row (it was Effort).
        ring = self.rect(self.item(root, "usageRing"))
        self.assertLessEqual(effort.right(), ring.left() + 0.5)
        self.assertTrue(row.contains(ring.center()))
        self.assertAlmostEqual(ring.right(), row.right(), delta=0.5)
        self.assertEqual(0, self.execute_calls())


class SendStopTests(ComposerCase):
    def test_m2_hover_lifts_the_key_and_a_run_turns_it_into_the_stop_key(self) -> None:
        bridge, root = self.open_window()
        send = self.item(root, "composerSend")
        working = self.item(root, "composerWorking")
        self.item(root, "composerInput").setProperty("text", "merhaba")
        pump(40)
        self.assertFalse(working.isVisible())
        self.assertFalse(self.item(root, "composerStopSquare").isVisible())
        glow = self.item(root, "composerSendGlow")
        QTest.mouseMove(root, self.centre(send).toPoint())
        self.assertTrue(wait_until(lambda: glow.property("opacity") == 1.0, 400))
        self.assertTrue(self.item(root, "composerSendRing").isVisible())
        # A run: the same key is the stop key and stops exactly the running call.
        cancelled = []
        bridge.backend.cancel_text = lambda rid: cancelled.append(rid) or True
        bridge._busy, bridge._request_id = True, "req-1"
        bridge.stateChanged.emit()
        pump(40)
        self.assertTrue(send.property("stopping"))
        self.assertTrue(send.property("enabled"))
        self.assertTrue(self.item(root, "composerStopSquare").isVisible())
        self.assertTrue(self.item(root, "composerStopRing").isVisible())
        self.assertTrue(working.isVisible())
        self.assertTrue(working.property("text"))
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, self.centre(send).toPoint())
        pump(40)
        self.assertEqual(["req-1"], cancelled)
        bridge._busy = False
        bridge.stateChanged.emit()
        pump(40)
        self.assertFalse(working.isVisible())
        self.assertFalse(send.property("stopping"))
        self.assertEqual(0, self.execute_calls())


class ModelMenuTests(ComposerCase):
    def test_m3_brands_default_tag_check_and_one_click_switches_model_and_provider(self) -> None:
        bridge, root = self.open_window()
        self.assertEqual("claude", bridge.chatProvider)
        menu = self.open_menu(root, "modelMenuChip", "modelMenu")
        texts = [i.property("text") for i in self.descendants(menu.property("contentItem")) if i.isVisible() and i.property("text")]
        self.assertIn("ANTHROPIC", texts)
        self.assertIn("OPENAI · CODEX", texts)
        self.assertTrue(self.row(menu, "modelMenuCurrent").property("current"))
        default = self.row(menu, "modelMenuRow_claude_default")
        self.assertTrue(default.property("shortcut"))                                  # the "Default" tag
        self.assertTrue(default.property("checked"))                                   # nothing configured: the CLI default
        self.assertFalse(self.row(menu, "modelMenuRow_claude_claude-opus-5").property("checked"))
        # The older versions are not in the main list.
        names = {i.objectName() for i in self.descendants(menu.property("contentItem")) if i.isVisible()}
        self.assertNotIn("modelMenuRow_claude_claude-opus-4-7", names)
        self.assertNotIn("modelMenuRow_codex_gpt-5.5-luna", names)
        # One click: Codex AND its model, with the model's own default effort.
        self.tap(root, self.row(menu, "modelMenuRow_codex_gpt-5.6-luna"), until=lambda: not menu.property("visible"))
        self.assertEqual(("codex", "gpt-5.6-luna", "medium"), (bridge.chatProvider, bridge.codexModel, bridge.codexEffort))
        self.assertFalse(menu.property("visible"))
        menu = self.open_menu(root, "modelMenuChip", "modelMenu")
        self.assertTrue(self.row(menu, "modelMenuRow_codex_gpt-5.6-luna").property("checked"))
        self.assertFalse(self.row(menu, "modelMenuRow_claude_default").property("checked"))
        # And back to Claude with one click.
        self.tap(root, self.row(menu, "modelMenuRow_claude_claude-opus-5"), until=lambda: not menu.property("visible"))
        self.assertEqual(("claude", "claude-opus-5"), (bridge.chatProvider, bridge.config["claude_model"]))
        self.assertEqual(0, self.execute_calls())

    def test_m3_more_models_lists_the_older_ones_by_brand_and_they_are_selectable(self) -> None:
        bridge, root = self.open_window()
        menu = self.open_menu(root, "modelMenuChip", "modelMenu")
        more = self.popup_named(root, "moreModelsMenu")
        self.tap(root, self.row(menu, "modelMenuMore"), until=lambda: self.is_open(more))
        self.assertTrue(self.is_open(more))
        self.assertTrue(menu.property("visible"))                                      # the menu stays beside it
        rows = sorted(i.objectName() for i in self.descendants(more.property("contentItem"))
                      if i.isVisible() and i.property("menuRow") is True)
        self.assertEqual(["modelMenuRow_claude_claude-opus-4-7", "modelMenuRow_codex_gpt-5.5-luna"], rows)
        self.tap(root, self.row(more, "modelMenuRow_codex_gpt-5.5-luna"), until=lambda: not menu.property("visible"))
        self.assertEqual(("codex", "gpt-5.5-luna", "low"), (bridge.chatProvider, bridge.codexModel, bridge.codexEffort))
        self.assertFalse(more.property("visible"))
        self.assertFalse(menu.property("visible"))
        self.assertEqual(0, self.execute_calls())


class EffortSliderTests(ComposerCase):
    def stops(self, track) -> list:
        n = track.property("count")
        return [track.mapToScene(QPointF((i + 0.5) * track.width() / n, track.height() / 2)).toPoint() for i in range(n)]

    def test_m3_one_stop_per_level_drag_snaps_click_jumps_written_on_release(self) -> None:
        bridge, root = self.open_window()
        bridge.applyModel("claude-opus-5", "", "claude-opus-5")
        pump(60)
        self.chip = self.item(root, "effortMenuChip")
        pop = self.open_menu(root, "effortMenuChip", "effortMenu")
        track = self.row(pop, "effortTrack")
        title = self.row(pop, "effortMenuTitle")
        stops = self.stops(track)
        self.assertEqual(5, len(stops))                                                # one per reported level
        recommended = self.row(pop, "effortRecommended")
        self.assertAlmostEqual(recommended.mapToScene(QPointF(recommended.width() / 2, 0)).x(), stops[2].x(), delta=1)   # under "high"
        # Drag from the first stop to the fourth: the title follows live, nothing is written until release.
        QTest.mousePress(root, Qt.LeftButton, Qt.NoModifier, stops[0])
        pump(20)
        QTest.mouseMove(root, stops[3])
        pump(40)
        self.assertEqual("xhigh", title.property("text"))
        self.assertEqual("", bridge.config.get("claude_effort") or "")
        QTest.mouseRelease(root, Qt.LeftButton, Qt.NoModifier, stops[3])
        self.assertTrue(wait_until(lambda: bridge.config.get("claude_effort") == "xhigh", 400))
        self.assertEqual("claude-opus-5", bridge.config["claude_model"])                # the model is kept
        # A click on a stop jumps there.
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, stops[4])
        self.assertTrue(wait_until(lambda: bridge.config.get("claude_effort") == "max", 400))
        self.assertTrue(pop.property("visible"))                                       # the popover stays open
        self.assertEqual(0, self.execute_calls())

    def test_m3_codex_levels_and_a_model_without_effort_has_no_chip(self) -> None:
        bridge, root = self.open_window()
        bridge.setChatProvider("codex")
        bridge.setCodexModel("gpt-5.6-luna", "medium")
        pump(60)
        self.chip = self.item(root, "effortMenuChip")
        pop = self.open_menu(root, "effortMenuChip", "effortMenu")
        stops = self.stops(self.row(pop, "effortTrack"))
        self.assertEqual(2, len(stops))
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, stops[0])
        self.assertTrue(wait_until(lambda: bridge.codexEffort == "low", 400))
        self.assertEqual(("codex", "gpt-5.6-luna"), (bridge.chatProvider, bridge.codexModel))
        QObject.setProperty(pop, "visible", False)
        bridge.setChatProvider("claude")
        bridge.applyModel("claude-haiku-4-5-20251001", "", "haiku")
        pump(60)
        self.assertFalse(self.chip.isVisible())                                        # Haiku reports no effort levels
        self.assertEqual(0, self.execute_calls())


if __name__ == "__main__":
    unittest.main()
