"""V2.2 S4 SKFEED: the skill announce chip and save-as-skill cards in the feed.

Fake feed items (the integration lane's skill events) drive the real window;
every button reaches the right bridge slot and app method over the test's own
TEMP database. No click here approves a task step: that stays the ApprovalCard.
"""

from __future__ import annotations

from dataclasses import replace
from unittest import mock

from jarvis.app import JarvisApp
from jarvis.skill_types import SkillOrigin, SkillScope, SkillScopeKind
from tests.helpers import wait_until
from tests.test_intent import task_proposal
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_skill_store import TK, draft
from tests import test_ui
from tests.test_ui import pump, tap

DRAFT = "draft-{:032x}".format
SPIED = ("draft_approve", "draft_reject", "suppress", "skill_decline", "skill_use", "approve")


class SkillsFeedTests(test_ui.UiTestCase):
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def setUp(self) -> None:
        super().setUp()
        self.store.mutate(lambda s: replace(s, skills_enabled=True))
        self.app = self.factory(self.store.load().settings)
        self.skills = self.app.skill_store
        self.project = self.app.memory.create_project("Alpha").id
        patches = {name: mock.patch.object(JarvisApp, name, autospec=True, side_effect=getattr(JarvisApp, name))
                   for name in SPIED}
        self.calls = {name: patch.start() for name, patch in patches.items()}
        for patch in patches.values():
            self.addCleanup(patch.stop)

    def open(self, *answers):
        ui = self.bridge(*answers)
        ui.refreshConfig()
        self.assertTrue(ui.skillsEnabled)
        return ui, self.window(ui)

    def visible(self, root, name) -> bool:
        return self.item(root, name).isVisible()

    def announce(self, ui, event="skill_announced"):
        # The skill's row as SKBRIDGE reads it (an adapted skill names its parent),
        # then Core's announce payload with the anchor session and task.
        ui._skills = [{"id": "sk-1", "project_id": "", "name": "Haftalik rapor", "version": 2, "versions": [1, 2],
                       "parent_name": "Rapor", "parent_version": 1, "steps": [], "inputs": [], "checks": []}]
        ui.skillsChanged.emit()
        ui._say("skill", "Haftalik rapor becerisi", event=event, session_id="s-1", task_id="t-1",
                skill_id="sk-1", name="Haftalik rapor", version=2, origin="adapted", revalidate=False)
        pump(300)

    def offer(self, ui, *ids):
        ui.refreshSkills()
        if ui.skillsEnabled:
            self.assertTrue(wait_until(lambda: len(ui.pendingDrafts) >= len(ids), 2000))
        ui._say("skill", "Beceri olarak kaydedilsin mi?", event="skill_draft_offered", run_id="run-1",
                task_kind=TK, draft_ids=list(ids))
        pump(300)

    def test_announce_chip_declines_through_the_bridge_in_both_themes(self) -> None:
        ui, root = self.open()
        self.announce(ui)
        for theme in ("space", "dark"):
            with self.subTest(theme):
                ui.setTheme(theme)
                pump(200)
                self.assertEqual(theme, root.property("themeName"))
                self.assertTrue(self.visible(root, "feedSkillChip"))
                self.assertIn("Haftalik rapor", self.item(root, "skillAnnounceText").property("text"))
                self.assertTrue(self.visible(root, "skillAnnounceProvenance"))
                self.assertFalse(self.visible(root, "skillAnnounceUse"))
        with mock.patch.object(ui, "_open_owner_of", return_value=True):
            self.click(root, "skillAnnounceDecline", until=lambda: self.calls["skill_decline"].called)
        self.assertEqual(("s-1", "t-1"), self.calls["skill_decline"].call_args.args[1:3])
        self.assertFalse(self.calls["approve"].called)

    def test_stale_chip_skip_declines_and_use_never_approves(self) -> None:
        ui, root = self.open()
        self.announce(ui, "skill_stale_announced")
        self.assertFalse(self.visible(root, "skillAnnounceDecline"))
        self.click(root, "skillAnnounceUse")       # the bridge has no use slot yet: nothing runs
        self.assertFalse(self.calls["skill_use"].called)
        with mock.patch.object(ui, "_open_owner_of", return_value=True):
            self.click(root, "skillAnnounceSkip", until=lambda: self.calls["skill_decline"].called)
        self.assertEqual(("s-1", "t-1"), self.calls["skill_decline"].call_args.args[1:3])
        self.assertFalse(self.calls["approve"].called)

    def test_one_card_per_draft_and_each_button_reaches_its_slot(self) -> None:
        scope = SkillScope(kind=SkillScopeKind.PROJECT, project_id=self.project)
        self.skills.add_draft(draft(1, scope))
        self.skills.add_draft(draft(2))
        self.skills.add_draft(draft(3))           # another task's draft: not this offer's card
        ui, root = self.open()
        self.offer(ui, DRAFT(1), DRAFT(2))
        for theme in ("space", "dark"):
            with self.subTest(theme):
                ui.setTheme(theme)
                pump(200)
                self.assertTrue(self.visible(root, "feedSkillDraft_" + DRAFT(1)))
                self.assertTrue(self.visible(root, "feedSkillDraft_" + DRAFT(2)))
        self.assertNotIn(DRAFT(3), {c.objectName() for c in self.cards(root)})
        self.assertIn("run-1", self.item(root, "feedSkillDraftSource").property("text"))
        self.assertTrue(self.item(root, "skillDraftStep").property("text").startswith("1. "))

        self.press(root, DRAFT(1), "skillDraftSave", lambda: self.calls["draft_approve"].called)
        self.assertEqual((DRAFT(1), "same", None, self.project), self.calls["draft_approve"].call_args.args[1:])
        self.assertTrue(wait_until(lambda: DRAFT(1) not in {c.objectName()[15:] for c in self.cards(root)}, 2000))
        self.press(root, DRAFT(2), "skillDraftSuppress", lambda: self.calls["suppress"].called)
        self.assertEqual((TK, "task"), self.calls["suppress"].call_args.args[1:3])
        self.press(root, DRAFT(2), "skillDraftEdit", lambda: ui.page == "skills")
        ui.setPage("home")
        pump(300)
        self.press(root, DRAFT(2), "skillDraftReject", lambda: self.calls["draft_reject"].called)
        self.assertEqual(DRAFT(2), self.calls["draft_reject"].call_args.args[1])
        self.assertTrue(wait_until(lambda: not self.cards(root), 2000))
        self.assertFalse(self.calls["approve"].called)

    def test_a_version_proposal_shows_its_number_and_diff_to_the_active_skill(self) -> None:
        self.skills.add_draft(draft(5))
        skill = self.skills.approve_draft(DRAFT(5), project_id=None)
        note = "Adım 1 son çalıştırmada başarısız oldu; önce girdilerini ve önkoşullarını doğrula."
        base = draft(6)
        self.skills.add_draft(replace(
            base, name=skill.name, checks=(note,), target_skill_id=skill.skill_id,
            provenance=replace(base.provenance, origin=SkillOrigin.VERSION_AFTER_FAILURE,
                               parent_skill_id=skill.skill_id, parent_version=skill.version)))
        ui, root = self.open()
        ui.refreshSkills()
        self.assertTrue(wait_until(lambda: ui.skills and ui.pendingDrafts, 2000))
        # The bridge row does not carry the draft's target yet (integration lane).
        ui._pending_drafts = [{**row, "target_skill_id": skill.skill_id} for row in ui._pending_drafts]
        ui.skillsChanged.emit()
        ui._say("skill", "Beceri olarak kaydedilsin mi?", event="skill_draft_offered", run_id="run-6",
                task_kind=TK, draft_ids=[DRAFT(6)])
        pump(300)
        for theme in ("space", "dark"):
            with self.subTest(theme):
                ui.setTheme(theme)
                pump(200)
                self.assertTrue(self.visible(root, "feedSkillDraft_" + DRAFT(6)))
                self.assertEqual('Version 2 proposal for "%s"' % skill.name,
                                 self.item(root, "skillDraftQuestion").property("text"))
                self.assertEqual("+ " + note, self.item(root, "feedSkillDraftDiff").property("text"))
                self.assertEqual("· " + note, self.item(root, "skillDraftCheck").property("text"))
        self.press(root, DRAFT(6), "skillDraftSave", lambda: self.calls["draft_approve"].called)
        self.assertEqual((DRAFT(6), "global", None, None), self.calls["draft_approve"].call_args.args[1:])
        self.assertFalse(self.calls["approve"].called)

    def press(self, root, draft_id, name, until) -> None:
        """Click one button of one draft's card (the cards' order is the bridge's)."""
        card = next(c for c in self.cards(root) if c.objectName() == "feedSkillDraft_" + draft_id)
        todo = [card]
        while todo:
            node = todo.pop()
            if node.objectName() == name:
                break
            todo += node.childItems()
        self.assertEqual(name, node.objectName())
        tap(root, node)
        self.assertTrue(wait_until(until, 2000), name)
        pump(300)

    def cards(self, root) -> list:
        found, todo = [], [root.contentItem()]
        while todo:
            node = todo.pop()
            if node.objectName().startswith("feedSkillDraft_") and node.isVisible():
                found.append(node)
            todo += node.childItems()
        return found

    def test_flag_off_shows_neither_chip_nor_card(self) -> None:
        self.skills.add_draft(draft(1))
        self.store.mutate(lambda s: replace(s, skills_enabled=False))
        ui = self.bridge()
        ui.refreshConfig()
        self.assertFalse(ui.skillsEnabled)
        root = self.window(ui)
        self.announce(ui)
        self.offer(ui, DRAFT(1))
        # Even with a draft row at hand, the flag alone hides the card.
        ui._pending_drafts = [{"id": DRAFT(1), "project_id": "", "name": "Taslak", "project_name": "", "reason": "",
                               "origin": "taught_from_run", "parent_version": 0, "steps": ["a"], "inputs": [], "checks": []}]
        ui.skillsChanged.emit()
        pump(300)
        self.assertEqual([], self.cards(root))
        found, todo = [], [root.contentItem()]
        while todo:
            node = todo.pop()
            found += [node] if node.objectName() == "feedSkillChip" else []
            todo += node.childItems()
        self.assertEqual([], found)

    def test_a_skill_step_approval_is_the_normal_approval_card(self) -> None:
        ui, root = self.open(task_proposal("code_analysis", objective="Bu projeyi incele."))
        self.announce(ui)
        ui.submitText("Bu projeyi incele.")
        self.assertTrue(wait_until(lambda: ui.approvalPending, 2000))
        self.assertTrue(wait_until(lambda: self.visible(root, "safetyCard_approval"), 900))
        self.assertTrue(self.visible(root, "feedSkillChip"))
        self.assertFalse(self.calls["approve"].called)
        self.assertTrue(ui.approvalPending)
