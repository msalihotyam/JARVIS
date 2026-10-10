"""Gate 6G Projects UX slice 1 (G6PJ1B): P1 the new-project popup, P2 the
Projects page card grid, and the bridge's read-only ``instruction_line`` key.

A. Bridge: every projectRecords entry carries the first non-blank line of its
   current stored instruction ("" when none or cleared).
B. Popup: name required; instruction cap (x / 2000) and PUBLIC / INTERNAL
   asked only when there is text, nothing preselected.
C. Create path: createProject, then the instruction for exactly that new id;
   a refusal shows Core's notice and saves nothing (fail closed).
D. Grid: tabs (active / archived), name search, both sorts (last activity
   default, name), card fields, the card menu per state (never a delete).
E. V9: an archived project's conversations stay reachable from the Arşiv tab.
F. Layout: 1920 and 1280 grids without overlap.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from PySide6.QtCore import QObject, QPointF, QRectF, Qt
from PySide6.QtTest import QTest

from jarvis.memory import SQLiteMemoryStore
from tests import test_ui
from tests.helpers import settled
from tests.test_ui import UiTestCase, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401


class Case(UiTestCase):
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def db(self) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(self.database)

    def all_items(self, root, name) -> list:
        found = []

        def walk(node):
            if node.objectName() == name:
                found.append(node)
            for child in node.childItems():
                walk(child)
        walk(root.contentItem())
        return found

    def visible(self, root, name) -> list:
        return [i for i in self.all_items(root, name) if i.isVisible()]

    def rect(self, item) -> QRectF:
        top_left = item.mapToScene(QPointF(0, 0))
        return QRectF(top_left.x(), top_left.y(), item.width(), item.height())

    def card_names(self, root) -> list[str]:
        cards = sorted(self.visible(root, "projectCard"), key=lambda c: (round(self.rect(c).y()), self.rect(c).x()))
        return [self.under(c, "projectCardName")[0].property("text") for c in cards]

    def under(self, node, name) -> list:
        found = []

        def walk(n):
            if n.objectName() == name:
                found.append(n)
            for child in n.childItems():
                walk(child)
        walk(node)
        return found

    def stamp(self, project_id: str, created: str) -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE projects SET created_at = ? WHERE id = ?", (created, project_id))

    def touch(self, conversation_id: str, updated: str) -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (updated, conversation_id))

    def popup(self, root, name):
        found = root.findChild(QObject, name)                                   # a Popup is no visual item
        self.assertIsNotNone(found, name)
        return found

    def page(self, bridge):
        root = self.window(bridge)
        bridge.showProject("")
        pump(300)
        return root

    def open_dialog(self, root):
        self.click(root, "projectsNew", then=lambda: self.assertTrue(self.popup(root, "newProjectDialog").property("opened")))
        return self.popup(root, "newProjectDialog")


class BridgeTests(Case):
    def test_a_instruction_line_is_the_first_non_blank_line_of_the_current_instruction(self) -> None:
        store = self.db()
        with_text, without, cleared = store.create_project("A"), store.create_project("B"), store.create_project("C")
        bridge = self.bridge()
        bridge.saveProjectInstruction(with_text.id, "\n   \n  Yanıtları kısa tut.  \nikinci satır", "internal")
        bridge.saveProjectInstruction(cleared.id, "Eski talimat", "public")
        bridge.saveProjectInstruction(cleared.id, "", "public")
        bridge.refreshProjects()
        lines = {r["name"]: r["instruction_line"] for r in bridge.projectRecords}
        self.assertEqual({"A": "Yanıtları kısa tut.", "B": "", "C": ""}, lines)
        self.assertEqual("", bridge.projectInstructionNotice)
        self.assertIsNotNone(without)


class PopupTests(Case):
    def test_b_name_required_instruction_cap_and_classification_only_with_text(self) -> None:
        bridge = self.bridge()
        root = self.page(bridge)
        self.open_dialog(root)
        create = self.item(root, "newProjectCreate")
        name = self.item(root, "newProjectName")
        instruction = self.item(root, "newProjectInstruction")
        self.assertFalse(create.isEnabled())                                    # no name
        name.setProperty("text", "   ")
        pump(30)
        self.assertFalse(create.isEnabled())                                    # blank is no name
        name.setProperty("text", "Evim")
        pump(30)
        self.assertTrue(create.isEnabled())                                     # a folder is never needed
        self.assertFalse(self.item(root, "newProjectClassRow").isVisible())
        instruction.setProperty("text", "Kısa tut.")
        pump(30)
        self.assertTrue(self.item(root, "newProjectClassRow").isVisible())
        self.assertFalse(create.isEnabled())                                    # nothing preselected (6B)
        self.assertEqual("1 / 2000".replace("1", str(len("Kısa tut."))), self.item(root, "newProjectCounter").property("text"))
        self.click(root, "newProjectClass_internal")
        self.assertTrue(create.isEnabled())
        instruction.setProperty("text", "x" * 2001)
        pump(30)
        self.assertFalse(create.isEnabled())
        self.assertEqual("2001 / 2000", self.item(root, "newProjectCounter").property("text"))
        self.assertTrue(self.item(root, "newProjectReason").isVisible())
        instruction.setProperty("text", "x" * 2000)
        pump(30)
        self.assertTrue(create.isEnabled())
        self.assertEqual([], [r for r in bridge.projectRecords])               # nothing created by typing


class CreateTests(Case):
    def fill(self, root, name: str, text: str = "", classification: str = "") -> None:
        self.item(root, "newProjectName").setProperty("text", name)
        self.item(root, "newProjectInstruction").setProperty("text", text)
        pump(30)
        if classification:
            self.click(root, "newProjectClass_" + classification)

    def test_c_create_then_the_instruction_for_that_new_id_and_the_project_opens(self) -> None:
        bridge = self.bridge()
        root = self.page(bridge)
        dialog = self.open_dialog(root)
        self.fill(root, "  Mutfak  ", "\nYenileme planı.\nBütçe 50k", "internal")
        self.click(root, "newProjectCreate", then=lambda: self.assertFalse(dialog.property("visible")))
        record = next(r for r in bridge.projectRecords if r["name"] == "Mutfak")
        self.assertEqual("Yenileme planı.", record["instruction_line"])
        current = self.db().load_project_instruction(record["id"])
        self.assertEqual(("Yenileme planı.\nBütçe 50k", "internal"), (current.text, current.classification.value))   # as stored
        self.assertEqual(record["id"], bridge.openProject["id"])               # the new project opens
        # Without an instruction: created, nothing saved.
        self.click(root, "projectsBack")
        self.open_dialog(root)
        self.fill(root, "Tez")
        self.click(root, "newProjectCreate")
        tez = next(r for r in bridge.projectRecords if r["name"] == "Tez")
        self.assertIsNone(self.db().load_project_instruction(tez["id"]))

    def test_c_a_refused_create_shows_the_notice_and_saves_nothing(self) -> None:
        store = self.db()
        existing = store.create_project("Evim")
        bridge = self.bridge()
        root = self.page(bridge)
        dialog = self.open_dialog(root)
        self.fill(root, "evim", "Bu talimat hiçbir yere yazılmamalı.", "public")
        self.click(root, "newProjectCreate")
        self.assertTrue(dialog.property("visible"))
        self.assertIn("already exists", self.item(root, "newProjectNotice").property("text"))
        self.assertEqual(["Evim"], [r["name"] for r in bridge.projectRecords])
        self.assertIsNone(self.db().load_project_instruction(existing.id))      # fail closed
        self.assertEqual("", bridge.openProject.get("id", ""))
        self.click(root, "newProjectCancel", then=lambda: self.assertFalse(dialog.property("visible")))

    def test_c_the_sidebar_plus_opens_the_popup(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("projects")
        pump(300)
        page = self.item(root, "projectsPage")
        page.metaObject().invokeMethod(page, "focusCreate")
        settled(200, lambda: self.assertTrue(self.popup(root, "newProjectDialog").property("opened")))


class GridTests(Case):
    def setUp(self) -> None:
        super().setUp()
        store = self.db()
        self.evim, self.tez, self.arsiv = store.create_project("Evim"), store.create_project("Tez 2025"), store.create_project("Eski iş")
        self.stamp(self.evim.id, "2026-01-01T10:00:00+00:00")
        self.stamp(self.tez.id, "2026-03-01T10:00:00+00:00")
        conversation = store.create_conversation(self.evim.id)
        self.touch(conversation.id, "2026-05-01T10:00:00+00:00")                 # Evim's last activity is newer
        self.archived_conversation = store.create_conversation(self.arsiv.id)    # created while the project was live
        store.set_project_active(self.arsiv.id, False)

    def ready(self):
        bridge = self.bridge()
        root = self.page(bridge)
        return bridge, root

    def test_d_tabs_search_and_both_sorts(self) -> None:
        bridge, root = self.ready()
        page = self.item(root, "projectsPage")
        self.assertEqual(["Evim", "Tez 2025"], self.card_names(root))           # last activity first (default)
        self.click(root, "projectsSort_name", then=lambda: self.assertEqual(["Evim", "Tez 2025"], self.card_names(root)))
        self.stamp(self.tez.id, "2026-09-01T10:00:00+00:00")
        bridge.refreshProjects()
        pump(100)
        self.assertEqual(["Evim", "Tez 2025"], self.card_names(root))           # by name, whatever the activity
        self.click(root, "projectsSort_activity", then=lambda: self.assertEqual(["Tez 2025", "Evim"], self.card_names(root)))
        self.item(root, "projectsSearch").setProperty("text", "tez")
        settled(100, lambda: self.assertEqual(["Tez 2025"], self.card_names(root)))
        self.item(root, "projectsSearch").setProperty("text", "yok böyle")
        settled(100, lambda: (
            self.assertEqual([], self.card_names(root)),
            self.assertEqual("No project matches your search.", self.item(root, "projectsEmpty").property("text")),
        ))
        self.item(root, "projectsSearch").setProperty("text", "")
        self.click(root, "projectsTab_archived", then=lambda: self.assertEqual(["Eski iş"], self.card_names(root)))
        self.assertEqual("archived", page.property("tab"))
        self.click(root, "projectsTab_mine", then=lambda: self.assertEqual(["Tez 2025", "Evim"], self.card_names(root)))

    def test_d_card_fields(self) -> None:
        bridge, root = self.ready()
        bridge.saveProjectInstruction(self.evim.id, "Ev işleri.\nikinci", "internal")
        bridge.showProject("")
        pump(200)
        page = self.item(root, "projectsPage")
        cards = {self.under(c, "projectCardName")[0].property("text"): c for c in self.visible(root, "projectCard")}
        evim, tez = cards["Evim"], cards["Tez 2025"]
        self.assertEqual("Ev işleri.", self.under(evim, "projectCardInstruction")[0].property("text"))
        self.assertEqual("No instruction", self.under(tez, "projectCardInstruction")[0].property("text"))
        self.assertEqual(page.property("cards").toVariant()[0]["at"], "2026-05-01T10:00:00+00:00")
        activity = self.under(evim, "projectCardActivity")[0].property("text")
        self.assertTrue(activity and not activity.startswith("2026-"))         # K3: a human date, never the raw stamp
        self.assertFalse(self.under(evim, "projectCardArchived")[0].isVisible())
        self.click(root, "projectsTab_archived")
        archived = self.visible(root, "projectCard")[0]
        self.assertTrue(self.under(archived, "projectCardArchived")[0].isVisible())
        self.assertFalse(any(t.startswith("p-") for t in self.card_names(root)))

    def menu_labels(self, root) -> list[str]:
        self.assertTrue(self.popup(root, "projectCardMenuSurface").property("visible"))
        return [r.property("label") for r in self.all_items(root, "projectMenuEdit") + self.all_items(root, "projectMenuArchive")
                + self.all_items(root, "projectMenuUnarchive") if r.isVisible()]

    def test_d_card_menu_per_state_never_a_delete(self) -> None:
        bridge, root = self.ready()
        tez = next(c for c in self.visible(root, "projectCard") if self.under(c, "projectCardName")[0].property("text") == "Tez 2025")
        button = self.under(tez, "projectCardMenu")[0]
        center = button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        pump(200)
        self.assertEqual(["Edit", "Archive"], self.menu_labels(root))
        self.click(root, "projectMenuArchive", then=lambda: self.assertFalse(
            next(r for r in bridge.projectRecords if r["id"] == self.tez.id)["active"]))
        self.click(root, "projectsTab_archived")
        archived = next(c for c in self.visible(root, "projectCard") if self.under(c, "projectCardName")[0].property("text") == "Tez 2025")
        button = self.under(archived, "projectCardMenu")[0]
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, button.mapToScene(QPointF(15, 15)).toPoint())
        pump(200)
        self.assertEqual(["Unarchive"], self.menu_labels(root))                 # projects have no delete path
        self.click(root, "projectMenuUnarchive", then=lambda: self.assertTrue(
            next(r for r in bridge.projectRecords if r["id"] == self.tez.id)["active"]))
        # Edit opens the project on its instruction.
        self.click(root, "projectsTab_mine")
        tez = next(c for c in self.visible(root, "projectCard") if self.under(c, "projectCardName")[0].property("text") == "Tez 2025")
        button = self.under(tez, "projectCardMenu")[0]
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, button.mapToScene(QPointF(15, 15)).toPoint())
        pump(200)
        self.click(root, "projectMenuEdit", then=lambda: (
            self.assertEqual(self.tez.id, bridge.openProject["id"]),
            self.assertTrue(self.item(root, "instructionPanel").isVisible()),
        ))

    def test_e_archived_projects_conversations_stay_reachable_from_the_archive_tab(self) -> None:
        kept = self.archived_conversation
        bridge, root = self.ready()
        self.click(root, "projectsTab_archived")
        card = self.visible(root, "projectCard")[0]
        center = card.mapToScene(QPointF(card.width() / 2, card.height() - 20)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        settled(300, lambda: (
            self.assertEqual(self.arsiv.id, bridge.openProject["id"]),
            self.assertTrue(self.item(root, "projectArchivedBanner").isVisible()),
            self.assertEqual(1, len(self.visible(root, "projectConversationRow"))),
        ))
        self.click(root, "projectConversationRow", then=lambda: self.assertEqual(kept.id, bridge.conversationId))


class LayoutTests(Case):
    def test_f_1920_and_1280_grids_without_overlap(self) -> None:
        store = self.db()
        for index in range(7):
            store.create_project(f"Proje {index} " + "uzun ad " * index)
        bridge = self.bridge()
        root = self.page(bridge)
        for width, height, columns in ((1920, 1080, 3), (1280, 760, 2)):
            root.resize(width, height)
            pump(600)
            grid = self.item(root, "projectGrid")
            area = self.rect(grid)
            cards = self.visible(root, "projectCard")
            self.assertGreaterEqual(len(cards), 2, (width, height))
            rects = [self.rect(c) for c in cards]
            for i, a in enumerate(rects):
                self.assertGreaterEqual(a.left(), area.left() - 0.5, (width, i))
                self.assertLessEqual(a.right(), area.right() + 0.5, (width, i))
                for b in rects[i + 1:]:
                    self.assertFalse(a.intersects(b), (width, a, b))
            self.assertGreaterEqual(grid.property("columns"), columns, (width, height))
            toolbar = [self.rect(self.item(root, n)) for n in ("projectsTabs", "projectsSearch", "projectsSort", "projectsNew")]
            for i, a in enumerate(toolbar):
                for b in toolbar[i + 1:]:
                    self.assertFalse(a.intersects(b), (width, a, b))
            self.assertLessEqual(toolbar[-1].right(), area.right() + 0.5)
