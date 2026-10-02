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

import unittest
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QPointF

from jarvis.model_retirement import codex_retirement, newest_per_name
from tests.test_projects_ui import ProjectsUiCase
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
