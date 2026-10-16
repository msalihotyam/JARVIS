"""SKUIB (V2.2 b1/b4/b5/b7/b8 + SKBRIDGE leftovers): the Skills page's run, lessons and
"not offered" sections, the draft card's save wording, the composer's /beceri hint, the
feed's lessons-used line and the settings skills switch.

The page and card load as pure views on a bare engine with fake data (signals logged);
the bridge half runs the real app over the test's own TEMP database (UiTestCase) - none
opens data\\jarvis.db, calls a model or touches the network. Space and dark themes only;
the command theme is frozen."""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from PySide6.QtCore import QMetaObject, QObject, QPointF, Qt, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtTest import QTest

from jarvis.app import JarvisApp
from jarvis.ui.app import QML_DIR
from jarvis.ui.i18n import localization
from tests.helpers import wait_until
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests import test_ui
from tests.test_ui import _APP, pump

NOTICE = "SKILL_REFUSED_NO_MATCH Yakın adlar: Haftalik rapor, Aylik rapor"

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
    property var log: []
    function note(entry) { log = log.concat([entry]) }
    property var lessonDetail: ({})
    SkillsPage {
        objectName: "page"; width: 720; height: 1080
        skillsEnabled: true; unreadable: 2; notice: "%NOTICE%"
        skills: [
            { id: "sk-report", name: "Weekly report", scope: "global", project_name: "", version: 1, versions: [1],
              status: "active", uses: 1, last_success: "", failures: 0, steps: ["Open {path}"], inputs: [], checks: [],
              origin: "taught_from_run", parent_name: "", parent_version: 0 },
            { id: "sk-mail", name: "Sort mail", scope: "global", project_name: "", version: 1, versions: [1],
              status: "disabled", uses: 0, last_success: "", failures: 0, steps: ["Read"], inputs: [], checks: [],
              origin: "taught_from_run", parent_name: "", parent_version: 0 }
        ]
        lessons: [ { experience_id: "ex-1", text: "Use the shared folder", state: "active" },
                   { experience_id: "ex-2", text: "Ask before mailing", state: "disabled" } ]
        lessonDetail: win.lessonDetail
        suppressions: [ { key: "report", scope: "task" }, { key: "", scope: "global" } ]
        onEditRequested: function (id) { win.note(["edit", id]) }
        onRunRequested: function (id) { win.note(["run", id]) }
        onLessonViewRequested: function (id) { win.note(["lessonView", id]) }
        onLessonDisableRequested: function (id) { win.note(["lessonDisable", id]) }
        onLessonDeleteRequested: function (id) { win.note(["lessonDelete", id]) }
        onUnsuppressRequested: function (key, scope) { win.note(["unsuppress", key, scope]) }
    }
    Column {
        x: 740; width: 460; spacing: 10
        SkillDraftCard { objectName: "versionCard"; width: parent.width; proposal: true; version: 2
                         draftName: "Weekly report"; targetName: "Weekly report" }
        SkillDraftCard { objectName: "adaptCard"; width: parent.width; draftName: "Quarterly report"
                         adaptedFrom: "Monthly report" }
        SkillDraftCard { objectName: "plainCard"; width: parent.width; draftName: "Plain" }
    }
}
""".replace("%NOTICE%", NOTICE)


def items(window):
    found, stack = [], [window.contentItem()]
    while stack:
        node = stack.pop()
        found.append(node)
        stack.extend(node.childItems())
    return found


class SkillsPageViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = QQmlEngine()
        self.engine.addImportPath(str(QML_DIR))
        self.engine.rootContext().setContextProperty("i18n", localization(_APP))
        component = QQmlComponent(self.engine)
        component.setData(HARNESS.encode("utf-8"), QUrl.fromLocalFile(str(QML_DIR / "skills_uib_harness.qml")))
        self.window = component.create()
        self.assertIsNotNone(self.window, component.errorString())
        self.component = component
        self.addCleanup(self.engine.deleteLater)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)
        pump(150)

    def log(self) -> list:
        return [list(e) for e in self.window.property("log").toVariant()]

    def all_shown(self, name: str) -> list:
        return sorted((i for i in items(self.window) if i.objectName() == name and i.isVisible()),
                      key=lambda i: i.mapToScene(QPointF()).y())

    def shown(self, name: str):
        found = self.all_shown(name)
        self.assertEqual(1, len(found), name)
        return found[0]

    def tap(self, item) -> None:
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier,
                         item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())
        pump(60)

    def label(self, card: str) -> str:
        root = next(i for i in items(self.window) if i.objectName() == card)
        stack = list(root.childItems())
        while stack:
            node = stack.pop()
            if node.objectName() == "skillDraftSave":
                return node.property("label")
            stack.extend(node.childItems())
        self.fail(card)

    def test_run_view_and_undo_emit_the_right_args_in_both_themes(self) -> None:
        for theme in ("space", "dark"):
            with self.subTest(theme):
                self.window.setProperty("themeName", theme)
                pump(500)
                self.assertEqual(theme, self.window.property("activeTheme"))
                self.window.setProperty("log", [])
                self.tap(self.shown("skillRun"))             # the selected, active skill
                self.tap(self.all_shown("lessonView")[0])
                undo = self.all_shown("suppressionUndo")
                self.assertEqual(2, len(undo))
                for button in undo:
                    self.tap(button)
                self.assertEqual([["run", "sk-report"], ["lessonView", "ex-1"],
                                  ["unsuppress", "report", "task"], ["unsuppress", "", "global"]], self.log())
                self.assertEqual(["report", "All skill offers"], [k.property("text") for k in self.all_shown("suppressionKey")])
                self.assertFalse(self.window.grabWindow().isNull())

    def body(self) -> list:
        return [t.property("text") for t in items(self.window) if t.isVisible() and isinstance(t.property("text"), str)]

    def confirm(self, dialog, name: str) -> None:
        self.assertTrue(wait_until(lambda: dialog.property("opened"), 1000))
        button = self.shown(name)
        self.assertTrue(wait_until(lambda: button.property("enabled"), 1500))   # a danger guard arms first
        self.tap(button)
        self.assertTrue(wait_until(lambda: not dialog.property("visible"), 1000))

    def test_disable_and_delete_go_through_their_final_confirm(self) -> None:
        page = self.shown("page")
        disable = page.findChild(QObject, "lessonDisableDialog")
        delete = page.findChild(QObject, "lessonDeleteDialog")
        self.assertEqual(1, len(self.all_shown("lessonDisable")))   # an off lesson has no disable control
        self.tap(self.shown("lessonDisable"))
        self.assertEqual([], self.log())
        self.assertIn("JARVIS stops using this lesson. This cannot be undone in this version.", self.body())
        self.confirm(disable, "lessonDisableConfirm")
        self.assertEqual([["lessonDisable", "ex-1"]], self.log())
        self.tap(self.all_shown("lessonDelete")[1])
        self.assertIn("The lesson is deleted for good and cannot be brought back.", self.body())
        self.confirm(delete, "lessonDeleteCancel")
        self.assertEqual([["lessonDisable", "ex-1"]], self.log())
        self.tap(self.all_shown("lessonDelete")[1])
        self.confirm(delete, "lessonDeleteConfirm")
        self.assertEqual([["lessonDisable", "ex-1"], ["lessonDelete", "ex-2"]], self.log())

    def test_notice_unreadable_states_and_detail_render_verbatim(self) -> None:
        self.assertEqual(NOTICE, self.shown("skillsNotice").property("text"))
        self.assertEqual("2 records could not be read", self.shown("skillsUnreadable").property("text"))
        self.assertEqual(["Use the shared folder", "Ask before mailing"], [t.property("text") for t in self.all_shown("lessonText")])
        self.assertEqual(2, len(self.all_shown("lessonState")))
        self.assertEqual([], self.all_shown("lessonDetail"))
        self.window.setProperty("lessonDetail", {"experience_id": "ex-1", "text": "Use the shared folder"})
        pump(60)
        self.assertTrue(self.shown("lessonDetail").isVisible())

    def test_the_edit_control_is_unreachable(self) -> None:
        self.assertEqual([], [i for i in items(self.window) if i.objectName() == "skillEdit" and i.isVisible()])
        self.assertNotIn("edit", [e[0] for e in self.log()])

    def test_draft_save_wording_names_the_target_or_the_origin(self) -> None:
        self.assertEqual("Save as a new version of Weekly report", self.label("versionCard"))
        self.assertEqual("Save as an adaptation of Monthly report", self.label("adaptCard"))
        self.assertEqual("Save", self.label("plainCard"))


class SkillsBridgeUibTests(test_ui.UiTestCase):
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def setUp(self) -> None:
        super().setUp()
        self.store.mutate(lambda s: replace(s, skills_enabled=True))
        self.app = self.factory(self.store.load().settings)

    def visible(self, root, name: str) -> list:
        # Delegates hang off the visual tree only, so walk childItems().
        found, todo = [], [root.contentItem()]
        while todo:
            node = todo.pop()
            if node.objectName() == name and node.isVisible():
                found.append(node)
            todo.extend(node.childItems())
        return found

    def test_composer_hint_shows_only_for_the_skill_prefix(self) -> None:
        ui = self.bridge()
        root = self.window(ui)
        field = self.item(root, "composerInput")
        for text, shown in (("", False), ("merhaba /beceri", False), ("/beceri rapor", True),
                            ("/skill report", True), ("/bec", False)):
            with self.subTest(text):
                field.setProperty("text", text)
                pump(30)
                self.assertEqual(shown, bool(self.visible(root, "composerSkillHint")))

    def test_lessons_used_shows_only_above_zero(self) -> None:
        ui = self.bridge()
        ui.refreshConfig()
        self.assertTrue(ui.skillsEnabled)
        root = self.window(ui)
        ui._say("skill", "Haftalik rapor", event="skill_run_finished", lessons_used=0)
        pump(300)
        self.assertEqual([], self.visible(root, "feedSkillLessonsUsed"))
        ui._say("skill", "Aylik rapor", event="skill_run_finished", lessons_used=2)
        pump(300)
        self.assertEqual(["2 lessons used"], [i.property("text") for i in self.visible(root, "feedSkillLessonsUsed")])

    def test_settings_switch_turns_skills_off(self) -> None:
        ui = self.bridge()
        root = self.window(ui)
        QMetaObject.invokeMethod(root, "toggleSettings")
        pump(400)
        self.assertTrue(self.visible(root, "skillsSwitchHint"))
        self.click(root, "skillsSwitch_off", until=lambda: not ui.skillsEnabled)
        self.assertIs(False, self.store.load().settings.skills_enabled)

    def test_run_goes_to_the_app_and_its_refusal_is_shown_verbatim(self) -> None:
        ui = self.bridge()
        with mock.patch.object(JarvisApp, "run_skill", autospec=True, side_effect=JarvisApp.run_skill) as spy:
            ui.runSkill("sk-missing")
            self.assertTrue(wait_until(lambda: spy.called, 3000))
        self.assertEqual(("sk-missing", None), spy.call_args.args[1:])

    def test_undo_refusal_is_the_app_error_verbatim(self) -> None:
        ui = self.bridge()
        expected = self.app.unsuppress("never", "task")
        self.assertFalse(expected["ok"])
        ui.unsuppress("never", "task")
        self.assertTrue(wait_until(lambda: ui.skillNotice != "", 3000))
        error = expected["error"]
        self.assertEqual(str(error["message"] if isinstance(error, dict) else error), ui.skillNotice)

    def test_lesson_and_undo_slots_send_the_right_args(self) -> None:
        ui = self.bridge()
        answers = {"list_lessons": {"ok": True, "lessons": [{"experience_id": "ex-1", "text": "t", "state": "active"}]},
                   "suppressions": {"ok": True, "suppressions": []}}
        fake = mock.Mock(side_effect=lambda method, *args: answers.get(method, {"ok": True}))
        with mock.patch.object(ui.backend, "skill_action", fake):
            ui.refreshLessons()
            self.assertTrue(wait_until(lambda: len(ui.lessons) == 1, 3000))
            ui.disableLesson("ex-1")
            ui.deleteLesson("ex-1")
            ui.unsuppress("", "global")
            ui.unsuppress("report", "task")
            pump(100)
        calls = [c.args for c in fake.call_args_list]
        self.assertIn(("list_lessons", None), calls)
        self.assertIn(("disable_lesson", "ex-1", None), calls)
        self.assertIn(("delete_lesson", "ex-1", None), calls)
        self.assertIn(("unsuppress", None, "global"), calls)
        self.assertIn(("unsuppress", "report", "task"), calls)
        self.assertNotIn("approve", " ".join(str(c[0]) for c in calls))


if __name__ == "__main__":
    unittest.main()
