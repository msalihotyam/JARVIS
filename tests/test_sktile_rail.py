"""SKTILE: Skills is a summary-rail tile, not a sidebar row.

The tile is hidden by default, four tiles show at most (a fifth is refused),
an older stored rail keeps its layout and gains Skills hidden at the end, and
the tile's click opens the Skills page. Every bridge runs over the test's own
TEMP config, DB and preferences file (UiTestCase); none opens data\\jarvis.db
or the real preferences file, calls a model or touches the network.
"""

from __future__ import annotations

import json
from dataclasses import replace

from jarvis.ui.preferences import RAIL_IDS, UiPreferences, canonical_preferences
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_skill_store import draft
from tests import test_ui
from tests.test_ui import pump

DRAFT = "draft-{:032x}".format


class SkillsTileTests(test_ui.UiTestCase):
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def shown(self, bridge) -> list[str]:
        return [c["id"] for c in bridge.railCards if c["visible"]]

    def test_skills_is_hidden_by_default_and_no_sidebar_row(self) -> None:
        bridge = self.bridge()
        self.assertIn("skills", RAIL_IDS)
        self.assertEqual(["tasks", "focus", "plan", "memory"], self.shown(bridge))
        root = self.window(bridge)
        self.assertIsNone(root.findChild(object, "sidebarSkills"))
        self.assertIsNone(root.findChild(object, "railSkills"))

    def test_older_rail_keeps_its_layout_and_gains_skills_hidden_at_the_end(self) -> None:
        older = canonical_preferences()
        older["rail"] = {"order": ["memory", "plan", "updates", "tasks", "focus"],
                         "visible": {"tasks": False, "updates": True, "focus": True, "plan": True, "memory": True}}
        older["theme"] = "dark"
        self.prefs_path.write_text(json.dumps(older), encoding="utf-8")
        prefs = UiPreferences(self.prefs_path)
        self.assertEqual(["memory", "plan", "updates", "tasks", "focus", "skills"], prefs.data["rail"]["order"])
        self.assertEqual({**older["rail"]["visible"], "skills": False}, prefs.data["rail"]["visible"])
        self.assertEqual("dark", prefs.data["theme"])

    def test_a_fifth_tile_is_refused_until_one_is_hidden(self) -> None:
        bridge = self.bridge()
        bridge.setCardVisible("rail", "plan", False)        # a saved file to compare against
        bridge.setCardVisible("rail", "plan", True)
        on_disk = self.prefs_path.read_bytes()
        self.assertFalse(bridge.setCardVisible("rail", "skills", True))
        self.assertFalse(bridge.prefs.data["rail"]["visible"]["skills"])
        self.assertEqual(on_disk, self.prefs_path.read_bytes())
        self.assertTrue(bridge.setCardVisible("rail", "updates", True))   # no tile: not counted
        self.assertTrue(bridge.setCardVisible("rail", "memory", False))
        self.assertTrue(bridge.setCardVisible("rail", "skills", True))
        self.assertEqual(["tasks", "updates", "focus", "plan", "skills"], self.shown(bridge))
        self.assertTrue(UiPreferences(self.prefs_path).data["rail"]["visible"]["skills"])

    def test_cards_inventory_says_why_a_fifth_tile_is_refused(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setEditMode(True)
        pump(250)
        self.click(root, "layoutCards")
        pump(250)
        self.assertFalse(self.item(root, "railCapNote").isVisible())
        self.click(root, "addToLayout_skills")
        pump(100)
        self.assertTrue(self.item(root, "railCapNote").isVisible())
        self.assertFalse(bridge.prefs.data["rail"]["visible"]["skills"])

    def test_the_tile_shows_real_counts_and_opens_the_skills_page(self) -> None:
        self.store.mutate(lambda s: replace(s, skills_enabled=True))
        app = self.factory(self.store.load().settings)
        app.skill_store.add_draft(draft(1))
        self.assertTrue(app.draft_approve(DRAFT(1))["ok"])
        app.skill_store.add_draft(draft(2))
        app.skill_store.add_draft(draft(3))
        bridge = self.bridge()
        bridge.refreshConfig()
        bridge.setCardVisible("rail", "memory", False)
        bridge.setCardVisible("rail", "skills", True)
        root = self.window(bridge)
        pump(250)
        self.assertTrue(bridge.skillsRead)                   # a shown tile read the skills itself
        self.assertEqual("1", self.item(root, "railCountSkills").property("text"))
        self.assertIn("2 drafts", self.item(root, "railLineSkills").property("text"))
        self.click(root, "railSkills")
        self.assertEqual("skills", bridge.page)
