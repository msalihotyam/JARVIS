"""V2.2 S4 SKBRIDGE: the Skills page wired to the bridge and the sidebar.

Every slot runs the real app skill API over the test's own TEMP database
(UiTestCase: a TEMP config and DB, scripted model, stub connectors); none
opens data\\jarvis.db, calls a model or touches the network.
"""

from __future__ import annotations

from dataclasses import replace
from unittest import mock

from jarvis.app import JarvisApp
from jarvis.skill_types import SkillScope, SkillScopeKind
from tests.helpers import wait_until
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_skill_store import TK, draft
from tests import test_ui
from tests.test_ui import pump

DRAFT = "draft-{:032x}".format


class SkillsBridgeTests(test_ui.UiTestCase):
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def setUp(self) -> None:
        super().setUp()
        self.store.mutate(lambda s: replace(s, skills_enabled=True))
        self.app = self.factory(self.store.load().settings)   # the same TEMP database, for setup and checks
        self.skills = self.app.skill_store
        self.project = self.app.memory.create_project("Alpha").id

    def approved(self, number: int, project: str | None = None) -> str:
        scope = SkillScope(kind=SkillScopeKind.PROJECT, project_id=project) if project else None
        self.skills.add_draft(draft(number) if scope is None else draft(number, scope))
        result = self.app.draft_approve(DRAFT(number), project_id=project)
        self.assertTrue(result["ok"], result)
        return result["skill_id"]

    def row(self, ui, skill_id: str) -> dict:
        return next(s for s in ui.skills if s["id"] == skill_id)

    def errors(self, ui) -> list[str]:
        return [entry["text"] for entry in ui.feed if entry["kind"] == "error"]

    def test_refresh_fills_the_model_from_every_scope(self) -> None:
        general = self.approved(1)
        local = self.approved(2, self.project)
        self.skills.add_draft(draft(3, SkillScope(kind=SkillScopeKind.PROJECT, project_id=self.project)))
        ui = self.bridge()
        ui.refreshConfig()
        self.assertTrue(ui.skillsEnabled)
        ui.refreshSkills()
        self.assertEqual({general, local}, {s["id"] for s in ui.skills})
        self.assertEqual(("global", "", ""), tuple(self.row(ui, general)[k] for k in ("scope", "project_id", "project_name")))
        self.assertEqual(("project", self.project, "Alpha"),
                         tuple(self.row(ui, local)[k] for k in ("scope", "project_id", "project_name")))
        shown = self.row(ui, general)
        self.assertEqual((1, [1], "active", 0, "taught_from_run"),
                         (shown["version"], shown["versions"], shown["status"], shown["uses"], shown["origin"]))
        self.assertEqual(["Haftalik rapor dosyasini oku: {path}"], shown["steps"])
        self.assertEqual(["path"], shown["inputs"])
        self.assertEqual([(DRAFT(3), self.project, "Alpha")],
                         [(d["id"], d["project_id"], d["project_name"]) for d in ui.pendingDrafts])
        self.assertEqual([], self.errors(ui))

    def test_every_slot_reaches_the_app_method(self) -> None:
        general = self.approved(1)
        local = self.approved(2, self.project)
        self.skills.add_draft(draft(3))
        self.skills.add_draft(draft(4))
        ui = self.bridge()
        ui.refreshSkills()
        names = ("skill_edit", "skill_set_enabled", "skill_delete", "skill_rollback",
                 "draft_approve", "draft_reject", "suppress", "skill_decline")
        spies = {name: mock.patch.object(JarvisApp, name, autospec=True, side_effect=getattr(JarvisApp, name))
                 for name in names}
        calls = {name: patch.start() for name, patch in spies.items()}
        for patch in spies.values():
            self.addCleanup(patch.stop)

        form = self.app.skill_get(local, None, self.project)["skill"]["edit_draft"]
        ui.skillEdit(local, dict(form, summary="Haftalik rapor dosyasini oku ve ozetle"))
        self.assertEqual((2, [1, 2]), (self.row(ui, local)["version"], self.row(ui, local)["versions"]))
        ui.skillRollback(local, 1)
        self.assertEqual(1, self.row(ui, local)["version"])
        ui.skillSetEnabled(general, False)
        self.assertEqual("disabled", self.row(ui, general)["status"])
        ui.draftApprove(DRAFT(3), "same")
        ui.draftReject(DRAFT(4))
        self.assertEqual([], ui.pendingDrafts)
        self.assertEqual(3, len(ui.skills))
        ui.skillDelete(general)
        self.assertNotIn(general, {s["id"] for s in ui.skills})
        ui.suppressTaskKind(TK, "task")
        self.assertTrue(self.skills.is_suppressed(TK))
        self.assertEqual([], self.errors(ui))
        ui.declineSkill("no-such-session", "no-such-task")     # Core's own refusal is shown, never swallowed
        wait_until(lambda: calls["skill_decline"].called, 2000)
        self.assertTrue(self.errors(ui))

        # Each action went back with the project its row was read in (S8).
        self.assertEqual(self.project, calls["skill_edit"].call_args.args[3])
        self.assertEqual((local, 1, self.project), calls["skill_rollback"].call_args.args[1:])
        self.assertEqual((general, False, None), calls["skill_set_enabled"].call_args.args[1:])
        self.assertEqual((general, None), calls["skill_delete"].call_args.args[1:])
        self.assertEqual((DRAFT(3), "same", None, None), calls["draft_approve"].call_args.args[1:])
        self.assertEqual((DRAFT(4), None), calls["draft_reject"].call_args.args[1:])
        self.assertEqual((TK, "task"), calls["suppress"].call_args.args[1:])
        self.assertEqual(("no-such-session", "no-such-task"), calls["skill_decline"].call_args.args[1:])

    def test_a_refusal_is_visible_and_an_unknown_row_goes_with_no_project(self) -> None:
        local = self.approved(2, self.project)
        ui = self.bridge()
        with mock.patch.object(JarvisApp, "skill_delete", autospec=True,
                               side_effect=JarvisApp.skill_delete) as spy:
            ui.skillDelete(local)          # not refreshed yet: no row, so no project - the app refuses (S8)
        self.assertEqual((local, None), spy.call_args.args[1:])
        self.assertTrue(self.errors(ui))
        self.assertEqual([local], [s["id"] for s in ui.skills])

    def test_flag_off_is_the_off_note_state_and_keeps_saved_skills(self) -> None:
        general = self.approved(1)
        self.skills.add_draft(draft(3))
        ui = self.bridge()
        ui.enableSkills(False)
        self.assertIs(False, self.store.load().settings.skills_enabled)
        self.assertFalse(ui.skillsEnabled)
        self.assertEqual([general], [s["id"] for s in ui.skills])     # clean-up stays possible (S4)
        self.assertEqual([], ui.pendingDrafts)                        # no offers while off
        self.assertEqual([], self.errors(ui))                         # off is the page's note, not an error
        with mock.patch.object(JarvisApp, "skill_rollback", autospec=True,
                               side_effect=JarvisApp.skill_rollback) as spy:
            ui.skillRollback(general, 1)                              # the app refuses while off, not the bridge
        self.assertEqual((general, 1, None), spy.call_args.args[1:])
        self.assertIs(True, self.factory(self.store.load().settings).skill_rollback(general, 1)["skills_disabled"])
        self.assertEqual([], self.errors(ui))                         # the off-note already says it
        ui.enableSkills(True)
        self.assertTrue(ui.skillsEnabled)
        self.assertEqual([DRAFT(3)], [d["id"] for d in ui.pendingDrafts])

    def test_sidebar_entry_opens_the_page_in_both_themes(self) -> None:
        general = self.approved(1)
        self.store.mutate(lambda s: replace(s, skills_enabled=False))
        ui = self.bridge()
        root = self.window(ui)
        for theme in ("dark", "space"):
            with self.subTest(theme=theme):
                ui.setTheme(theme)
                ui.setPage("home")
                pump(50)
                self.assertTrue(self.item(root, "sidebarSkills").isVisible())   # shown with the flag off too
                self.click(root, "sidebarSkills", until=lambda: root.property("loadedPage") == "skills")
                self.assertEqual("skills", ui.page)
                self.assertEqual("home", ui.prefs.data["page"])     # never persisted: an older build reads home
                self.assertTrue(self.item(root, "sidebarSkills").property("current"))
                self.assertTrue(self.item(root, "skillsOff").isVisible())
                self.assertEqual([general], [s["id"] for s in ui.skills])
        ui.enableSkills(True)
        pump(50)
        self.assertFalse(self.item(root, "skillsOff").isVisible())
        self.click(root, "skillToggle", until=lambda: self.row(ui, general)["status"] == "disabled")
        self.assertEqual("disabled", self.row(ui, general)["status"])


if __name__ == "__main__":
    import unittest
    unittest.main()
