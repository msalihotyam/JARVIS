"""SKUIQML (V2.2 S1): the skills page, the skill announce chip and the save-as-skill draft card
as pure views. They load on a bare engine with no ``bridge`` at all, take plain objects in and
give only signals out; every signal is recorded by the harness below. Both the main ("space")
and the dark theme are loaded; the command theme is frozen and out of scope."""
from __future__ import annotations

import unittest

from PySide6.QtCore import QObject, QPointF, Qt, QUrl
from PySide6.QtGui import QColor
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtTest import QTest

from jarvis.ui.app import QML_DIR
from jarvis.ui.i18n import localization
from tests.helpers import wait_until
from tests.test_ui import _APP, pump

VIEWS = ("pages/SkillsPage.qml", "components/SkillAnnounceChip.qml", "components/SkillDraftCard.qml")

HARNESS = """
import QtQuick
import QtQuick.Window
import "theme"
import "pages"
import "components"
Window {
    id: win
    width: 1700; height: 1100; visible: true
    property string themeName: "space"
    Binding { target: Theme; property: "name"; value: win.themeName }
    readonly property string activeTheme: Theme.name
    readonly property color ink: Theme.text
    property var log: []
    function note(entry) { log = log.concat([entry]) }
    property bool skillsOn: true
    property var skills: [
        { id: "sk-report", name: "Weekly report", scope: "project", project_name: "Evim", version: 3, versions: [1, 2, 3],
          status: "active", uses: 12, last_success: "", failures: 1, steps: ["Open {path}", "Sum the rows"],
          inputs: ["path"], checks: ["The total is shown"], origin: "adapted", parent_name: "Monthly report", parent_version: 2 },
        { id: "sk-mail", name: "Sort mail", scope: "global", project_name: "", version: 1, versions: [1],
          status: "disabled", uses: 0, last_success: "", failures: 0, steps: ["Read the inbox"], inputs: [], checks: [],
          origin: "taught_from_run", parent_name: "", parent_version: 0 },
        { id: "sk-old", name: "Old flow", scope: "global", project_name: "", version: 2, versions: [1, 2],
          status: "stale", uses: 4, last_success: "", failures: 3, steps: ["Step"], inputs: [], checks: [],
          origin: "version_after_failure", parent_name: "", parent_version: 0 }
    ]
    SkillsPage {
        objectName: "page"; width: 720; height: 900
        skills: win.skills; skillsEnabled: win.skillsOn
        onEditRequested: function (id) { win.note(["edit", id]) }
        onEnabledToggled: function (id, on) { win.note(["toggle", id, on]) }
        onDeleteRequested: function (id) { win.note(["delete", id]) }
        onRollbackRequested: function (id, version) { win.note(["rollback", id, version]) }
        onEnableSkillsRequested: win.note(["enable"])
    }
    Column {
        x: 740; width: 460; spacing: 10
        SkillAnnounceChip { objectName: "chip"; width: parent.width; skillName: "Weekly report"; version: 2
                            parentName: "Monthly report"; parentVersion: 1
                            onDeclined: win.note(["declined"]) }
        SkillAnnounceChip { objectName: "staleChip"; width: parent.width; skillName: "Old flow"; version: 2; stale: true
                            onUse: win.note(["use"]); onSkip: win.note(["skip"]) }
        // Several drafts from one task: several cards.
        Repeater {
            objectName: "drafts"
            model: [{ name: "Collect the invoices", project: "Evim" }, { name: "Mail the summary", project: "" }]
            SkillDraftCard {
                objectName: "draftCard" + index
                width: parent.width; draftName: modelData.name; projectName: modelData.project
                steps: ["Open {path}", "Copy the totals"]; inputs: ["path"]; checks: ["The sheet is saved"]
                onApprove: function (scope) { win.note(["approve", index, scope]) }
                onEdit: win.note(["draftEdit", index])
                onReject: win.note(["reject", index])
                onSuppress: win.note(["suppress", index])
            }
        }
    }
    SkillDraftCard { objectName: "proposalCard"; x: 1220; width: 460; proposal: true; version: 4; draftName: "Weekly report"
                     projectName: "Evim"; steps: ["Open {path}"]; onApprove: function (scope) { win.note(["proposal", scope]) } }
}
"""


def items(window):
    found, stack = [], [window.contentItem()]
    while stack:
        node = stack.pop()
        found.append(node)
        stack.extend(node.childItems())
    return found


class SkillsViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = QQmlEngine()
        self.engine.addImportPath(str(QML_DIR))
        self.i18n = localization(_APP)
        self.engine.rootContext().setContextProperty("i18n", self.i18n)
        component = QQmlComponent(self.engine)
        component.setData(HARNESS.encode("utf-8"), QUrl.fromLocalFile(str(QML_DIR / "skills_ui_harness.qml")))
        self.window = component.create()
        self.assertIsNotNone(self.window, component.errorString())
        self.component = component
        self.addCleanup(self.engine.deleteLater)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)
        pump(150)

    def log(self) -> list:
        return [list(e) for e in self.window.property("log").toVariant()]

    def shown(self, name: str):
        found = [i for i in items(self.window) if i.objectName() == name and i.isVisible()]
        self.assertEqual(1, len(found), name)
        return found[0]

    def all_shown(self, name: str) -> list:
        return [i for i in items(self.window) if i.objectName() == name and i.isVisible()]

    def tap(self, item) -> None:
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier,
                         item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())
        pump(30)

    def named(self, root, name: str):
        return next(i for i in [root, *self.below(root)] if i.objectName() == name)

    def below(self, node) -> list:
        found, stack = [], list(node.childItems())
        while stack:
            child = stack.pop()
            found.append(child)
            stack.extend(child.childItems())
        return found

    def test_views_are_pure(self) -> None:
        for view in VIEWS:
            with self.subTest(view):
                self.assertNotIn("bridge", (QML_DIR / view).read_text(encoding="utf-8"))

    def test_rows_render_in_both_themes(self) -> None:
        for theme in ("space", "dark"):
            with self.subTest(theme):
                self.window.setProperty("themeName", theme)
                pump(500)                                   # the theme's staged colour recalibration
                self.assertEqual(theme, self.window.property("activeTheme"))
                names = self.all_shown("skillRowName")
                self.assertEqual(["Weekly report", "Sort mail", "Old flow"],
                                 [n.property("text") for n in sorted(names, key=lambda n: n.mapToScene(QPointF()).y())])
                plain = next(n for n in names if n.property("text") == "Old flow")       # not selected, not off
                self.assertEqual(QColor(self.window.property("ink")).name(), QColor(plain.property("color")).name())
                self.assertEqual(["PROJECT", "GLOBAL", "GLOBAL"], sorted(
                    [b.childItems()[0].childItems()[1].property("text") for b in self.all_shown("listRowBadge")], reverse=True))
                statuses = {s.childItems()[0].childItems()[1].property("text") for s in self.all_shown("skillRowStatus")}
                self.assertEqual({"ACTIVE", "OFF", "STALE"}, statuses)
                usage = {u.property("text") for u in self.all_shown("skillRowUsage")}
                self.assertIn("12 uses · 1 failure", usage)
                self.assertIn("0 uses", usage)
                self.assertFalse(self.window.grabWindow().isNull())

    def test_detail_shows_steps_inputs_checks_and_provenance(self) -> None:
        self.assertEqual("Weekly report", self.shown("skillDetailName").property("text"))
        self.assertEqual("Adapted from the \"Monthly report\" skill, v2", self.shown("skillProvenance").property("text"))
        self.assertEqual(["1. Open {path}", "2. Sum the rows"], [s.property("text") for s in
                         sorted(self.all_shown("skillStep"), key=lambda s: s.mapToScene(QPointF()).y())])
        self.assertEqual(["· path"], [s.property("text") for s in self.all_shown("skillInput")])
        self.assertEqual(["· The total is shown"], [s.property("text") for s in self.all_shown("skillCheck")])
        self.assertFalse(self.all_shown("skillStaleNote"))
        old = next(r for r in self.all_shown("skillRow") if r.property("title") == "Old flow")
        self.tap(old)
        self.assertEqual("Old flow", self.shown("skillDetailName").property("text"))
        self.assertTrue(self.shown("skillStaleNote").property("text").startswith("This skill failed in recent runs."))
        self.assertEqual("New version after a failed run", self.shown("skillProvenance").property("text"))

    def test_actions_emit_their_signals_with_the_right_args(self) -> None:
        self.tap(self.shown("skillEdit"))
        self.tap(self.shown("skillToggle"))
        rollbacks = sorted(self.all_shown("skillRollback"), key=lambda b: b.mapToScene(QPointF()).y())
        self.assertEqual(2, len(rollbacks))                       # v1 and v2; the active v3 has none
        self.tap(rollbacks[0])
        self.tap(rollbacks[1])
        self.assertEqual([["edit", "sk-report"], ["toggle", "sk-report", False],
                          ["rollback", "sk-report", 1], ["rollback", "sk-report", 2]], self.log())
        self.tap(next(r for r in self.all_shown("skillRow") if r.property("title") == "Sort mail"))
        self.assertEqual("Turn on", self.shown("skillToggle").property("label"))
        self.tap(self.shown("skillToggle"))
        self.assertEqual(["toggle", "sk-mail", True], self.log()[-1])
        self.assertFalse(self.all_shown("skillRollback"))         # one version: nothing to return to

    def test_delete_asks_first(self) -> None:
        page = self.shown("page")                                  # its wrapper keeps the child's alive
        dialog = page.findChild(QObject, "skillDeleteDialog")
        self.tap(self.shown("skillDelete"))
        self.assertTrue(wait_until(lambda: dialog.property("opened"), 1000))
        self.assertEqual([], self.log())                          # asked, nothing deleted
        self.assertEqual("Delete the skill \"Weekly report\"?", self.shown("skillDeleteTitle").property("text"))
        self.tap(self.shown("skillDeleteCancel"))
        self.assertTrue(wait_until(lambda: not dialog.property("visible"), 1000))
        self.assertEqual([], self.log())
        self.tap(self.shown("skillDelete"))
        confirm = self.shown("skillDeleteConfirm")
        self.assertTrue(wait_until(lambda: confirm.property("enabled"), 1500))   # the danger guard arms first
        self.tap(confirm)
        self.assertTrue(wait_until(lambda: not dialog.property("visible"), 1000))
        self.assertEqual([["delete", "sk-report"]], self.log())

    def test_off_note_and_empty_state(self) -> None:
        self.assertFalse(self.all_shown("skillsOff"))
        self.window.setProperty("skillsOn", False)
        pump(30)
        self.tap(self.shown("skillsOffAction"))
        self.assertEqual([["enable"]], self.log())
        self.assertTrue(self.all_shown("skillRow"))               # saved skills stay listed while off
        self.assertFalse(self.all_shown("skillsEmpty"))
        self.window.setProperty("skills", [])
        pump(30)
        self.shown("skillsEmpty")
        self.assertFalse(self.all_shown("skillRow"))
        self.assertFalse(self.all_shown("skillDetailName"))

    def test_announce_chip(self) -> None:
        chip = self.shown("chip")
        self.assertEqual("Using the \"Weekly report\" skill for this (v2)", self.named(chip, "skillAnnounceText").property("text"))
        self.assertEqual("Adapted from the \"Monthly report\" skill, v1", self.named(chip, "skillAnnounceProvenance").property("text"))
        self.assertFalse(self.named(chip, "skillAnnounceUse").isVisible())
        self.tap(self.named(chip, "skillAnnounceDecline"))
        stale = self.shown("staleChip")
        self.assertEqual("The \"Old flow\" skill failed in recent runs - should I use it? (v2)",
                         self.named(stale, "skillAnnounceText").property("text"))
        self.assertFalse(self.named(stale, "skillAnnounceDecline").isVisible())
        self.tap(self.named(stale, "skillAnnounceUse"))
        self.tap(self.named(stale, "skillAnnounceSkip"))
        self.assertEqual([["declined"], ["use"], ["skip"]], self.log())

    def test_draft_cards_buttons_and_scope_picker(self) -> None:
        project, plain = self.shown("draftCard0"), self.shown("draftCard1")
        self.assertEqual("Save this as a skill?", self.named(project, "skillDraftQuestion").property("text"))
        self.assertEqual(["1. Open {path}", "2. Copy the totals"], [s.property("text") for s in sorted(
            [i for i in self.below(project) if i.objectName() == "skillDraftStep"], key=lambda s: s.mapToScene(QPointF()).y())])
        # D8: a project draft starts in its project; D15: it may become global, a global draft has no picker.
        self.assertEqual("project", project.property("chosenScope"))
        self.tap(self.named(project, "skillDraftSave"))
        self.tap(self.named(project, "skillDraftScope_global"))
        self.tap(self.named(project, "skillDraftSave"))
        self.tap(self.named(project, "skillDraftScope_project"))
        self.tap(self.named(project, "skillDraftEdit"))
        self.tap(self.named(project, "skillDraftReject"))
        self.tap(self.named(project, "skillDraftSuppress"))
        self.assertFalse(self.named(plain, "skillDraftScope").isVisible())
        self.assertTrue(self.named(plain, "skillDraftScopeGlobal").isVisible())
        plain.setProperty("scope", "project")                     # never into a project it does not come from
        self.tap(self.named(plain, "skillDraftSave"))
        self.assertEqual([["approve", 0, "project"], ["approve", 0, "global"], ["draftEdit", 0], ["reject", 0],
                          ["suppress", 0], ["approve", 1, "global"]], self.log())

    def test_version_proposal_variant(self) -> None:
        card = self.shown("proposalCard")
        self.assertEqual("Version 4 proposal for \"Weekly report\"", self.named(card, "skillDraftQuestion").property("text"))
        self.tap(self.named(card, "skillDraftSave"))
        self.assertEqual([["proposal", "project"]], self.log())

    def test_turkish_catalog(self) -> None:
        self.i18n.attach(self.engine)
        self.addCleanup(self.i18n.apply, "en-US")
        self.window.setProperty("skillsOn", False)
        self.i18n.apply("tr-TR")
        pump(50)
        self.assertEqual("Bu iş için \"Weekly report\" becerisini kullanıyorum (v2)",
                         self.named(self.shown("chip"), "skillAnnounceText").property("text"))
        self.assertEqual("Beceriler kapalı", self.shown("skillsOff").childItems()[0].property("text"))
        self.assertEqual("Bunu beceri olarak kaydedeyim mi?",
                         self.named(self.shown("draftCard0"), "skillDraftQuestion").property("text"))
        self.assertEqual("\"Weekly report\" için v4 önerisi",
                         self.named(self.shown("proposalCard"), "skillDraftQuestion").property("text"))


if __name__ == "__main__":
    unittest.main()
