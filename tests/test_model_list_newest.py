"""S5 (Salih 2026-09-30): the model selector shows only the newest version per
model name, with a window-only "show older models" toggle. DISPLAY ONLY.

A. The pure rule (jarvis.model_retirement.newest_per_name): Salih's two live
   tables exactly, every bypass, date snapshots, specialties, alias keepers,
   the selected model, and Codex's declared upgrades first.
B. The window: the lists, the toggle (off at every launch, never written to
   ui-preferences.json), the "new version" hint and its one explicit click.
C. Nothing else moved: the cache / provider lines stay complete and only the
   bridge and the rule know the rule.

No test calls a CLI, a model or the network, or opens the live DB.
"""

from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QPointF

from jarvis.model_retirement import codex_retirement, newest_per_name
from tests.test_projects_ui import ProjectsUiCase
from tests.test_projects_ui_6e import WindowCase as SixEWindowCase, settle
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import UiTestCase, _APP, pump

EFFORTS = ["low", "medium", "high"]


def ids(rows) -> list[str]:
    return [r["id"] for r in rows]


def rows(*model_ids: str) -> list[dict]:
    return [{"id": i, "efforts": EFFORTS, "default_effort": "medium", "specialty": ""} for i in model_ids]


def alias(row_id: str, resolved) -> dict:
    return {"id": row_id, "name": row_id.title(), "resolved": resolved, "efforts": EFFORTS}


CODEX_LIVE = ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
              "gpt-daybreak-blue-latest")
CLAUDE_LIVE = [alias("default", "claude-opus-5-5"), alias("opus", "claude-opus-5-5"), alias("sonnet", "claude-sonnet-5-5"),
               alias("haiku", "claude-haiku-4-5-20251001"),
               *[{"id": i, "name": i, "efforts": EFFORTS} for i in (
                   "claude-fable-5-1", "claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-opus-4-8",
                   "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")]]


class RuleTests(unittest.TestCase):
    def test_a_salihs_codex_table(self) -> None:
        shown, hidden = newest_per_name(rows(*CODEX_LIVE), provider="codex")
        self.assertEqual(["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-terra", "gpt-daybreak-blue-latest"], ids(shown))
        self.assertEqual({"gpt-6-sol": "gpt-6.1-sol", "gpt-5.6-sol": "gpt-6.1-sol", "gpt-5.6-luna": "gpt-6-luna"}, hidden)

    def test_a_salihs_claude_table_and_alias_keepers(self) -> None:
        shown, hidden = newest_per_name(CLAUDE_LIVE, provider="claude")
        self.assertEqual(["default", "opus", "sonnet", "haiku", "claude-fable-5-1"], ids(shown))
        self.assertEqual({"claude-sonnet-5": "sonnet", "claude-opus-5": "opus", "claude-fable-5": "claude-fable-5-1",
                          "claude-opus-4-8": "opus", "claude-opus-4-7": "opus", "claude-opus-4-6": "opus",
                          "claude-sonnet-4-6": "sonnet"}, hidden)

    def test_a_bypass_ids_always_stay_and_never_hide_anything(self) -> None:
        stable = rows("claude-opus-5-5", "claude-opus-4-7")
        extra = ["claude-opus-9-preview", "claude-opus-9[1m]", "claude-opus-9-latest", "claude-opus", "claude-3-5-opus-20241022",
                 "claude-opus-9-5-20250101-x", "opus[1m]", "default"]
        shown, hidden = newest_per_name(stable + rows(*extra), provider="claude")
        self.assertEqual(["claude-opus-5-5"] + extra, ids(shown))                      # only 4-7 is hidden, by 5-5
        self.assertEqual({"claude-opus-4-7": "claude-opus-5-5"}, hidden)
        shown, hidden = newest_per_name(rows("gpt-6-sol", "gpt-5.6-sol", "gpt-9-sol-preview", "gpt-9-sol[1m]", "gpt-9-latest",
                                             "gpt-sol", "gpt-5.6-sol-2"), provider="codex")
        self.assertEqual({"gpt-5.6-sol": "gpt-6-sol"}, hidden)
        self.assertEqual(["gpt-6-sol", "gpt-9-sol-preview", "gpt-9-sol[1m]", "gpt-9-latest", "gpt-sol", "gpt-5.6-sol-2"], ids(shown))
        # An unknown provider, and garbage rows, hide nothing.
        self.assertEqual((rows("a", "b"), {}), newest_per_name(rows("a", "b"), provider="google"))
        shown, hidden = newest_per_name([None, {"id": 3}, {"name": "x"}, *rows("gpt-6-sol")], provider="codex")
        self.assertEqual((["gpt-6-sol"], {}), (ids(shown), hidden))

    def test_a_a_date_is_a_snapshot_never_a_version(self) -> None:
        shown, hidden = newest_per_name(rows("claude-haiku-4-5-20251001", "claude-haiku-4-5-20260101", "claude-haiku-4-5",
                                             "claude-haiku-4-20250101", "claude-haiku-4"), provider="claude")
        self.assertEqual(["claude-haiku-4-5-20251001", "claude-haiku-4-5-20260101", "claude-haiku-4-5"], ids(shown))
        self.assertEqual({"claude-haiku-4-20250101": "claude-haiku-4-5-20251001", "claude-haiku-4": "claude-haiku-4-5-20251001"}, hidden)
        # Missing minor = 0.
        shown, hidden = newest_per_name(rows("claude-opus-5", "claude-opus-4-9"), provider="claude")
        self.assertEqual(({"claude-opus-4-9": "claude-opus-5"}, ["claude-opus-5"]), (hidden, ids(shown)))

    def test_a_specialty_names_are_separate_names(self) -> None:
        shown, hidden = newest_per_name(rows("gpt-5.1-codex-mini", "gpt-5.1-codex", "gpt-6-mini", "gpt-5-codex", "gpt-5-codex-mini",
                                             "gpt-6-sol", "gpt-6.1-luna"), provider="codex")
        self.assertEqual({"gpt-5-codex": "gpt-5.1-codex", "gpt-5-codex-mini": "gpt-5.1-codex-mini"}, hidden)
        self.assertEqual(["gpt-5.1-codex-mini", "gpt-5.1-codex", "gpt-6-mini", "gpt-6-sol", "gpt-6.1-luna"], ids(shown))

    def test_a_an_alias_without_a_readable_resolved_hides_nothing(self) -> None:
        full = rows("claude-sonnet-4-6", "claude-sonnet-5")
        for resolved in (None, "", "sonnet", "claude-sonnet-x", "claude-sonnet-9[1m]", "claude-3-5-sonnet-20241022", 5):
            with self.subTest(resolved=resolved):
                shown, hidden = newest_per_name([alias("sonnet", resolved), *full], provider="claude")
                self.assertEqual({"claude-sonnet-4-6": "claude-sonnet-5"}, hidden)        # only the full ids speak
        no_key = {"id": "sonnet"}                                                        # no "resolved" at all
        self.assertEqual({"claude-sonnet-4-6": "claude-sonnet-5"}, newest_per_name([no_key, *full], provider="claude")[1])
        # An alias resolving LOWER or EQUAL never hides a full id, and equal keeps the full id as keeper.
        self.assertEqual({}, newest_per_name([alias("sonnet", "claude-sonnet-4-6"), *full[1:]], provider="claude")[1])
        shown, hidden = newest_per_name([alias("opus", "claude-opus-5-5"), *rows("claude-opus-5-5", "claude-opus-4-7")],
                                        provider="claude")
        self.assertEqual({"claude-opus-4-7": "claude-opus-5-5"}, hidden)
        # A different name key is never touched by another alias.
        self.assertEqual({}, newest_per_name([alias("opus", "claude-opus-5-5"), *rows("claude-sonnet-4-6")], provider="claude")[1])

    def test_a_the_selected_model_is_never_hidden_and_its_keeper_is_named(self) -> None:
        shown, hidden = newest_per_name(CLAUDE_LIVE, provider="claude", selected="claude-opus-4-7")
        self.assertIn("claude-opus-4-7", ids(shown))
        self.assertEqual("opus", hidden["claude-opus-4-7"])
        self.assertEqual(7, len(hidden))
        shown, hidden = newest_per_name(rows(*CODEX_LIVE), provider="codex", selected="gpt-5.6-sol")
        self.assertEqual(["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-daybreak-blue-latest"], ids(shown))
        self.assertEqual("gpt-6.1-sol", hidden["gpt-5.6-sol"])

    def test_a_codex_declared_upgrades_run_first(self) -> None:
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        models = rows("gpt-6-sol", "gpt-5.6-sol", "gpt-5.5-sol", "gpt-5.6-luna", "gpt-6-luna")
        upgrades = {"gpt-5.5-sol": {"model": "gpt-5.6-sol", "retirement_at": "", "specialty": ""}}
        offered, retired = codex_retirement(models, upgrades, now)
        self.assertEqual({"gpt-5.5-sol": "gpt-5.6-sol"}, retired)                         # the declared upgrade, unchanged
        shown, hidden = newest_per_name(offered, provider="codex")
        self.assertEqual(["gpt-6-sol", "gpt-6-luna"], ids(shown))                          # then the newest per name
        self.assertEqual({"gpt-5.6-sol": "gpt-6-sol", "gpt-5.6-luna": "gpt-6-luna"}, hidden)
        self.assertNotIn("gpt-5.5-sol", hidden)                                            # already gone before the rule
        # The rule never mutates its input.
        before = [dict(m) for m in offered]
        newest_per_name(offered, provider="codex")
        self.assertEqual(before, offered)


class WindowCase(ProjectsUiCase):
    def lines(self, claude=CLAUDE_LIVE, codex=CODEX_LIVE) -> list[dict]:
        return [{"id": "claude", "vendor": "Anthropic", "name": "Claude", "status": "available", "research": True,
                 "models": [dict(m) for m in claude]},
                {"id": "codex", "vendor": "OpenAI", "name": "Codex", "status": "available", "plan": "plus", "research": False,
                 "models": [{"id": m, "name": m, "efforts": EFFORTS, "default_effort": "medium"} for m in codex], "retired": {}}]

    def open_panel(self, bridge, provider: str = "claude"):
        self.fake_providers(bridge, self.lines())
        bridge.setChatProvider(provider)
        root = self.window(bridge)
        self.click(root, "modelTrigger"); pump(400)
        return root

    def every(self, root):
        found, stack = [], [root.contentItem()]
        while stack:
            node = stack.pop()
            found.append(node)
            stack.extend(node.childItems())
        return found

    def listed(self, root, prefix: str) -> list[str]:
        """The rows the panel builds (in catalog order)."""
        named = [(i.mapToScene(QPointF(0, 0)).y(), str(i.objectName())[len(prefix):]) for i in self.every(root) if str(i.objectName()).startswith(prefix)]
        return [name for _, name in sorted(named)]

    def shown(self, root, name: str):
        """The visible one: the Claude and Codex blocks each own a hint and a toggle."""
        found = [i for i in self.every(root) if str(i.objectName()) == name and i.isVisible()]
        self.assertTrue(found, name)
        return found[0]

    def invoke(self, root, name: str) -> None:
        from PySide6.QtCore import QMetaObject
        QMetaObject.invokeMethod(self.shown(root, name), "chosen")
        pump(200)


class WindowTests(WindowCase):
    def test_b_claude_list_shows_the_newest_and_the_toggle_shows_everything(self) -> None:
        bridge = self.bridge()
        root = self.open_panel(bridge)
        self.assertEqual(["opus", "sonnet", "haiku", "claude-fable-5-1"], self.listed(root, "claudeModel_"))
        self.assertFalse(bridge.showOldModels)
        self.click_toggle(root)
        self.assertTrue(bridge.showOldModels)
        self.assertEqual(sorted(m["id"] for m in CLAUDE_LIVE if m["id"] != "default"), sorted(self.listed(root, "claudeModel_")))
        self.click_toggle(root)
        self.assertEqual(["opus", "sonnet", "haiku", "claude-fable-5-1"], self.listed(root, "claudeModel_"))

    def click_toggle(self, root) -> None:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtTest import QTest
        item = self.shown(root, "showOldModelsToggle")
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())
        pump(260)

    def test_b_codex_list_shows_the_newest(self) -> None:
        bridge = self.bridge()
        root = self.open_panel(bridge, "codex")
        self.assertEqual(["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-terra", "gpt-daybreak-blue-latest"],
                         self.listed(root, "codexModel_"))
        self.click_toggle(root)
        self.assertEqual(sorted(CODEX_LIVE), sorted(self.listed(root, "codexModel_")))

    def test_b_the_toggle_is_window_only_and_off_at_every_launch(self) -> None:
        bridge = self.bridge()
        root = self.open_panel(bridge)
        bridge.setChatProvider("claude")
        before = self.prefs_path.read_bytes() if self.prefs_path.exists() else None
        self.click_toggle(root)
        self.assertTrue(bridge.showOldModels)
        after = self.prefs_path.read_bytes() if self.prefs_path.exists() else None
        self.assertEqual(before, after)                                              # ui-preferences.json: byte-identical
        if after is not None:
            self.assertNotIn(b"old", after.lower())
        relaunched = self.bridge()
        self.assertFalse(relaunched.showOldModels)

    def test_b_the_lists_the_cache_and_routing_read_stay_complete(self) -> None:
        bridge = self.bridge()
        self.open_panel(bridge)
        claude = next(p for p in bridge.providers if p["id"] == "claude")
        codex = next(p for p in bridge.providers if p["id"] == "codex")
        self.assertEqual([m["id"] for m in CLAUDE_LIVE], ids(claude["models"]))
        self.assertEqual(list(CODEX_LIVE), ids(codex["models"]))
        self.assertEqual({"claude", "codex"}, set(bridge.modelLists))
        self.assertEqual(7, len(bridge.modelLists["claude"]["hidden"]))
        self.assertEqual(["gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-luna"], list(bridge.modelLists["codex"]["hidden"]))

    def test_b_a_selected_old_claude_model_stays_and_the_hint_selects_the_keeper_only_on_click(self) -> None:
        bridge = self.bridge()
        bridge.applyModel("claude-opus-4-7", "", "claude-opus-4-7"); pump(100)
        root = self.open_panel(bridge)
        self.assertEqual("claude-opus-4-7", self.store.load().settings.claude_model)
        self.assertIn("claude-opus-4-7", self.listed(root, "claudeModel_"))              # shown although older
        hint = self.shown(root, "modelNewerHint")
        self.assertTrue(hint.isVisible())
        self.assertIn("New version: ", self.shown(root, "modelNewerHintText").property("text"))
        self.assertIn("Opus", self.shown(root, "modelNewerHintText").property("text"))
        pump(300)
        self.assertEqual("claude-opus-4-7", self.store.load().settings.claude_model)    # never automatic
        self.invoke(root, "modelNewerHint")
        settings = self.store.load().settings
        self.assertEqual("claude-opus-5-5", settings.claude_model)
        chat = __import__("json").loads(self.prefs_path.read_text(encoding="utf-8"))["chat"]
        self.assertEqual(("opus", "claude-opus-5-5"), (chat["claude_lane"], chat["claude_lane_model"]))   # the existing lane path
        self.assertNotIn("claude-opus-4-7", self.listed(root, "claudeModel_"))
        self.assertFalse([i for i in self.every(root) if str(i.objectName()) == "modelNewerHint" and i.isVisible()])

    def test_b_a_selected_old_codex_model_stays_and_the_hint_selects_the_keeper_only_on_click(self) -> None:
        bridge = self.bridge()
        self.fake_providers(bridge, self.lines())
        bridge.setChatProvider("codex")
        bridge.setCodexModel("gpt-5.6-sol", "high")
        root = self.window(bridge)
        self.click(root, "modelTrigger"); pump(400)
        self.assertIn("gpt-5.6-sol", self.listed(root, "codexModel_"))
        self.assertIn("6.1 Sol", self.shown(root, "modelNewerHintText").property("text"))
        self.assertIn("New version: ", self.shown(root, "modelNewerHintText").property("text"))
        self.assertEqual("gpt-5.6-sol", bridge.codexModel)
        self.invoke(root, "modelNewerHint")
        self.assertEqual(("gpt-6.1-sol", "high"), (bridge.codexModel, bridge.codexEffort))
        self.assertNotIn("gpt-5.6-sol", self.listed(root, "codexModel_"))

    def test_b_no_hint_when_the_selection_is_current(self) -> None:
        bridge = self.bridge()
        root = self.open_panel(bridge)
        self.assertFalse([i for i in self.every(root) if str(i.objectName()) == "modelNewerHint" and i.isVisible()])
        bridge.applyModel("claude-opus-5-5", "", "opus"); pump(100)
        self.assertFalse([i for i in self.every(root) if str(i.objectName()) == "modelNewerHint" and i.isVisible()])


class ScopeTests(unittest.TestCase):
    def test_c_only_the_bridge_and_the_rule_know_the_rule(self) -> None:
        root = Path(__file__).resolve().parents[1] / "jarvis"
        users = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py")
                       if "newest_per_name" in p.read_text(encoding="utf-8"))
        self.assertEqual(["model_retirement.py", "ui/bridge.py"], users)
        for name in ("model_catalog.py", "ui/backend.py", "intent.py", "codex.py", "core.py"):
            self.assertNotIn("newest_per_name", (root / name).read_text(encoding="utf-8"), name)


class CatalogTests(UiTestCase):
    def test_d_new_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Eski modelleri göster", tx("ModelPanel", "Show older models"))
        self.assertEqual("Yeni sürüm: %1", tx("ModelPanel", "New version: %1"))

    def test_e_the_effort_labels_are_turkish_and_nothing_else_moved(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual(["düşük", "orta", "yüksek"], [tx("ModelPanel", w) for w in ("low", "medium", "high")])
        self.assertEqual("Yeni sürüm: %1", tx("ModelPanel", "New version: %1"))      # the hint's own source is unchanged


# -- Gate 6E, slice C6: the model panel (D27, Salih Q8 2026-10-03) --

ALL_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE_FIVE = [{"id": "default", "name": "Default", "resolved": "claude-opus-5-5", "efforts": ALL_EFFORTS},
               {"id": "opus", "name": "Opus", "resolved": "claude-opus-5-5", "efforts": ALL_EFFORTS}]


class PolishTests(WindowCase):
    def stage(self, provider="claude", old=False, lines=None, size=None):
        bridge = self.bridge()
        if old:
            bridge.applyModel("claude-opus-4-7", "", "claude-opus-4-7"); pump(100)
        self.fake_providers(bridge, lines or self.lines())
        bridge.setChatProvider(provider)
        if provider == "codex":
            bridge.setCodexModel("gpt-6.1-sol", "medium")
        root = self.window(bridge)
        if size:
            root.resize(*size); pump(300)
        self.click(root, "modelTrigger"); pump(400)
        self.root = root
        return bridge, root

    def under(self, item, name) -> list:
        found, stack = [], [item]
        while stack:
            node = stack.pop()
            if str(node.objectName()) == name and node.isVisible():
                found.append(node)
            stack.extend(node.childItems())
        return found

    def center(self, item):
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()

    def hover(self, item) -> None:
        from PySide6.QtTest import QTest
        QTest.mouseMove(self.root, self.center(item)); pump(20)

    def press(self, item) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        QTest.mouseClick(self.root, Qt.LeftButton, Qt.NoModifier, self.center(item)); pump(260)

    def key(self, key) -> None:
        from PySide6.QtTest import QTest
        QTest.keyClick(self.root, key); pump(200)

    def tr(self, bridge) -> None:
        from jarvis.ui.i18n import localization
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        bridge.setLanguage("tr-TR"); pump(300)

    def label_of(self, segment) -> str:
        return next(c.property("text") for c in segment.childItems() if isinstance(c.property("text"), str))

    def test_f_the_hint_is_an_action_row_with_hover_focus_and_keys(self) -> None:
        from PySide6.QtCore import Qt
        bridge, root = self.stage(old=True)
        hint = self.shown(root, "modelNewerHint")
        text = self.shown(root, "modelNewerHintText")
        self.assertEqual("↑ New version: Opus 5.5", text.property("text"))
        self.assertTrue(hint.property("activeFocusOnTab"))
        # At rest it is text, not a box: nothing is filled. Hover lifts it (eased in).
        self.assertEqual(0.0, hint.property("color").alphaF())
        self.hover(hint)
        settle(lambda: abs(hint.property("color").alphaF() - 0.05) < 0.003)
        self.assertAlmostEqual(0.05, hint.property("color").alphaF(), places=2)
        # Focus: the ring (only then).
        self.assertFalse(self.under(hint, "modelRowFocusRing"))
        hint.forceActiveFocus(); pump(60)
        self.assertTrue(self.under(hint, "modelRowFocusRing"))
        # Return does what a click does: the keeper, and nothing before.
        self.assertEqual("claude-opus-4-7", self.store.load().settings.claude_model)
        self.key(Qt.Key_Return)
        self.assertEqual("claude-opus-5-5", self.store.load().settings.claude_model)
        # Space does the same, again from a fresh old selection.
        bridge.applyModel("claude-opus-4-7", "", "claude-opus-4-7"); pump(100)
        self.click(root, "modelTrigger"); pump(300)                   # close and reopen: the panel re-reads the selection
        self.click(root, "modelTrigger"); pump(400)
        hint = self.shown(root, "modelNewerHint")
        hint.forceActiveFocus(); pump(60)
        self.key(Qt.Key_Space)
        self.assertEqual("claude-opus-5-5", self.store.load().settings.claude_model)

    def test_g_the_toggle_has_hover_focus_and_space(self) -> None:
        from PySide6.QtCore import Qt
        bridge, root = self.stage()
        toggle = self.shown(root, "showOldModelsToggle")
        self.assertTrue(toggle.property("activeFocusOnTab"))
        self.assertEqual(0.0, toggle.property("color").alphaF())
        self.hover(toggle)
        settle(lambda: abs(toggle.property("color").alphaF() - 0.05) < 0.003)
        self.assertAlmostEqual(0.05, toggle.property("color").alphaF(), places=2)
        self.assertFalse(self.under(toggle, "modelRowFocusRing"))
        toggle.forceActiveFocus(); pump(60)
        self.assertTrue(self.under(toggle, "modelRowFocusRing"))
        self.assertFalse(bridge.showOldModels)
        self.key(Qt.Key_Space)
        self.assertTrue(bridge.showOldModels)
        self.key(Qt.Key_Space)
        self.assertFalse(bridge.showOldModels)
        self.key(Qt.Key_Return)                                   # Space only, like a check box
        self.assertFalse(bridge.showOldModels)

    def test_h_effort_labels_are_turkish_for_the_three_levels_and_the_values_never_change(self) -> None:
        lines = self.lines(claude=CLAUDE_FIVE)
        bridge, root = self.stage(lines=lines)
        labels = lambda prefix, values: [self.label_of(self.shown(root, prefix + v)) for v in values]  # noqa: E731
        self.assertEqual(["low", "medium", "high", "xhigh", "max"], labels("claudeEffort_", ALL_EFFORTS))     # English as today
        self.assertEqual("Default", self.label_of(self.shown(root, "claudeEffort_")))
        self.tr(bridge)
        self.assertEqual(["düşük", "orta", "yüksek", "xhigh", "max"], labels("claudeEffort_", ALL_EFFORTS))
        self.assertEqual("Varsayılan", self.label_of(self.shown(root, "claudeEffort_")))
        # The picked value is the stored one, in every language.
        for value in ("high", "low", "xhigh"):
            self.press(self.shown(root, "claudeEffort_" + value))
            self.assertEqual(value, self.store.load().settings.claude_effort)
            self.assertEqual(value, bridge.config["claude_effort"])

    def test_h_codex_effort_labels_and_values(self) -> None:
        bridge, root = self.stage("codex")
        self.tr(bridge)
        self.assertEqual(["düşük", "orta", "yüksek"], [self.label_of(self.shown(root, "codexEffort_" + v)) for v in EFFORTS])
        self.press(self.shown(root, "codexEffort_high"))
        self.assertEqual("high", bridge.codexEffort)
        self.press(self.shown(root, "codexEffort_low"))
        self.assertEqual("low", bridge.codexEffort)

    def test_i_the_panel_stays_inside_the_window_at_every_size(self) -> None:
        for size in ((1100, 700), (1280, 760), (1920, 1080)):
            with self.subTest(size=size):
                bridge, root = self.stage(size=size)
                surface = self.shown(root, "modelSurface")
                self.assertEqual(min(720, size[1] - surface.y() - 16), surface.height())
                self.assertLessEqual(surface.y() + surface.height(), size[1] - 16)
                self.assertLessEqual(surface.height(), 720)
                scroll = self.shown(root, "modelPanelScroll")
                self.assertLessEqual(scroll.mapToScene(QPointF(0, scroll.height())).y(), surface.y() + surface.height())

    def test_j_the_fade_shows_only_while_content_is_below(self) -> None:
        bridge, root = self.stage(size=(1100, 700))
        scroll = self.shown(root, "modelPanelScroll")
        fade = lambda: [f for f in self.under(root.contentItem(), "modelPanelFade") if f.property("opacity") > 0]  # noqa: E731
        self.assertEqual(24, self.under(root.contentItem(), "modelPanelFade")[0].height())
        self.assertGreater(scroll.property("contentHeight"), scroll.height())
        self.assertTrue(fade())                                                       # at the top, more below
        end = scroll.property("contentHeight") - scroll.height()
        scroll.setProperty("contentY", end)
        settle(lambda: not fade())
        self.assertFalse(fade())                                                      # at the end, nothing below
        scroll.setProperty("contentY", end / 2)
        settle(lambda: bool(fade()))
        self.assertTrue(fade())
        scroll.setProperty("contentHeight", scroll.height() - 10)                     # content that fits: no fade at all
        scroll.setProperty("contentY", 0)
        settle(lambda: not fade())
        self.assertFalse(fade())

    def test_k_motion_follows_the_tokens_and_ambient_off_is_instant(self) -> None:
        bridge, root = self.stage(old=True, size=(1100, 700))
        scroll = self.shown(root, "modelPanelScroll")
        fade = self.under(root.contentItem(), "modelPanelFade")[0]
        end = scroll.property("contentHeight") - scroll.height()
        scroll.setProperty("contentY", end)
        settle(lambda: not fade.property("visible"))
        samples = SixEWindowCase.motion(self, root, fade)
        mark, called = len(samples), time.monotonic()
        scroll.setProperty("contentY", 0)                                             # more below again: fades in over hoverMs
        SixEWindowCase.assert_motion_within_token_plus_one_frame(self, samples, called, mark, True, 110)
        mark, called = len(samples), time.monotonic()
        scroll.setProperty("contentY", end)
        SixEWindowCase.assert_motion_within_token_plus_one_frame(self, samples, called, mark, False, 110)
        # Hover lifts follow hoverMs too: the end state is held once reached.
        hint = self.shown(root, "modelNewerHint")
        self.hover(hint)
        settle(lambda: abs(hint.property("color").alphaF() - 0.05) < 0.003)
        # Ambient effects off: every one of them at once.
        bridge.setAmbientEffects(False); pump(100)
        toggle = self.shown(root, "showOldModelsToggle")
        self.assertEqual(0.0, toggle.property("color").alphaF())
        self.hover(toggle)
        self.assertAlmostEqual(0.05, toggle.property("color").alphaF(), places=2)    # one pump, no animation
        scroll.setProperty("contentY", 0); pump(10)
        self.assertEqual(1.0, fade.property("opacity"))
        scroll.setProperty("contentY", end); pump(10)
        self.assertEqual((0.0, False), (fade.property("opacity"), fade.property("visible")))
