"""Gate 6G Sidebar v2, slice 3 (build/6g/PLAN_DRAFT.txt V10, DRAG, V4 + revisions).

V10   SABİTLENENLER rows: the lead glyph tells the kind (folder = project
      conversation, speech bubble = plain); hover and wait (~0.5 s) opens an info
      card to the RIGHT of the row with stored facts only (kind + project, title,
      the project's instruction line or the first message, the project's files,
      the last activity); an empty fact is left out; nothing is generated.
DRAG  Projects in the sidebar: manual order by a mouse drag with a drop line,
      kept in the window preferences (a sibling file, like the pins); a new
      project goes on top; pinned projects keep their place on top; project
      conversations keep no manual order.
V4    The New chat screen's project picker under the composer ("Proje seç ⌄":
      search, "Proje yok", the projects, "Yeni proje…" = the existing dialog).
      Default: the current project (its page or its conversation), else none.
      The first message fixes the owner; the 6C one-way bind is unchanged.

Window preferences and navigation only. No test makes a live model, auth or
network call or opens the live DB.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest

from tests.helpers import settled, wait_until
from tests.test_g6sb1_sidebar import SidebarV2Case
from tests.test_projects_ui_6f import dump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump, task_proposal


class G6SB3Case(SidebarV2Case):
    def popup_items(self, popup, name) -> list:
        found = [i for i in self.descendants(popup.property("contentItem")) if i.objectName() == name]
        return sorted(found, key=lambda i: i.mapToScene(QPointF(0, 0)).y())          # top to bottom

    def popup_text(self, popup, name) -> str:
        found = [i for i in self.popup_items(popup, name) if i.isVisible()]
        return found[0].property("text") if found else ""

    def add_files(self, bridge, project_id, names) -> None:
        folder = Path(tempfile.mkdtemp(prefix="jarvis-g6sb3-"))
        self.addCleanup(shutil.rmtree, folder, True)
        for name in names:
            (folder / name).write_text(f"{name}\n" + "satır\n" * 20, encoding="utf-8")
            bridge.backend.project_file("add", project_id, path=str(folder / name), classification="internal")

    def order_file(self) -> Path:
        return self.prefs_path.with_suffix(".sidebar-order.json")


class PinInfoCardTests(G6SB3Case):
    def facts(self):
        from jarvis.conversation import PersistedConversation
        store = self.data()
        store.save_explicit_user_project_instruction(self.tez.id, "\n  Tezi bitir.\nİkinci satır", "internal")
        PersistedConversation(store, self.plain[0].id).append(role="user", kind="user", text="Merhaba, ilk mesaj.\n[ek]",
                                                              message="Merhaba, ilk mesaj.")
        return store

    def test_a_icons_by_kind_and_the_card_opens_right_of_the_row_after_the_wait(self) -> None:
        self.facts()
        bridge = self.bridge()
        self.add_files(bridge, self.tez.id, ("a.md", "b.md"))
        bridge.refreshProjects()
        root = self.window(bridge)
        self.fixed_today(root, self.now)
        for c in (self.kodc, self.plain[0], self.tezc[0]):
            self.assertTrue(bridge.pinConversation(c.id))
        settled(300, lambda: self.assertEqual(["Tez 0", "Düz 0", "Kod 0"], self.pinned(root)))
        db_before = dump(self.database)
        # V10: the kind glyph, no project label, no tip of its own (the card says it all).
        self.assertEqual({"Tez 0": "folder", "Düz 0": "chat", "Kod 0": "folder"},
                         {r.property("title"): r.property("lead") for r in self.shown(root, "sidebarPinnedConversation")})
        row = self.pin_row(root, "Tez 0")
        self.assertFalse(self.child(row, "sidebarRowTip").property("active"))
        card = self.popup_named(root, "pinInfoCard")
        self.assertEqual(500, card.property("delay"))
        self.hover(root, row)
        hovered = time.monotonic()
        pump(200)
        self.assertFalse(card.property("visible"))                                   # not before the wait
        self.assertTrue(wait_until(lambda: bool(card.property("opened")), 3000))
        self.assertGreaterEqual(time.monotonic() - hovered, 0.45)
        # To the RIGHT of the row, its top at the row's top.
        surface = card.property("background")
        self.assertGreaterEqual(self.rect(surface).left(), self.rect(row).right())
        self.assertAlmostEqual(self.rect(surface).top(), self.rect(row).top(), delta=1)
        self.assertEqual("Project conversation · Tez", self.popup_text(card, "pinInfoKind"))
        self.assertEqual("Tez 0", self.popup_text(card, "pinInfoTitle"))
        self.assertEqual("Tezi bitir.", self.popup_text(card, "pinInfoSummaryText"))      # the instruction's first line
        self.assertEqual("2 files", self.popup_text(card, "pinInfoFileCount"))
        self.assertEqual("a.md, b.md", self.popup_text(card, "pinInfoFileNames"))
        self.assertRegex(self.popup_text(card, "pinInfoUpdated"), r"^Last activity: today \d\d:\d\d$")
        # The pointer leaves: it closes.
        QTest.mouseMove(root, QPoint(960, 600))
        self.assertTrue(wait_until(lambda: not card.property("visible"), 1000))
        # A plain conversation: its first message (the person's own words), no files.
        self.hover(root, self.pin_row(root, "Düz 0"))
        self.assertTrue(wait_until(lambda: bool(card.property("opened")), 3000))
        self.assertEqual("Conversation without a project", self.popup_text(card, "pinInfoKind"))
        self.assertEqual("Merhaba, ilk mesaj.", self.popup_text(card, "pinInfoSummaryText"))
        self.assertEqual("", self.popup_text(card, "pinInfoFileCount"))
        # A project with no instruction and no files: both left out, nothing made up.
        QTest.mouseMove(root, QPoint(960, 600))
        self.assertTrue(wait_until(lambda: not card.property("visible"), 1000))
        self.hover(root, self.pin_row(root, "Kod 0"))
        self.assertTrue(wait_until(lambda: bool(card.property("opened")), 3000))
        self.assertEqual("Project conversation · Kod", self.popup_text(card, "pinInfoKind"))
        self.assertEqual("", self.popup_text(card, "pinInfoSummaryText"))
        self.assertEqual("", self.popup_text(card, "pinInfoFileCount"))
        self.assertNotEqual("", self.popup_text(card, "pinInfoUpdated"))
        QTest.mouseMove(root, QPoint(960, 600))
        # Read only: not a listed pin -> nothing; the store unchanged; no call.
        self.assertEqual({}, bridge.pinInfo(self.plain[1].id))
        self.assertEqual(db_before, dump(self.database))
        self.assertEqual(0, self.execute_calls())


class ProjectDragTests(G6SB3Case):
    def drag_project(self, root, row, target_y: float, probe=None) -> None:
        """A real mouse drag of a project row: press, move in steps, release at scene y ``target_y``."""
        self.lists_at_rest(root)
        start = row.mapToScene(QPointF(row.width() / 2, row.height() / 2)).toPoint()
        self.hover(root, row)
        pump(80)
        QTest.mousePress(root, Qt.LeftButton, Qt.NoModifier, start)
        pump(40)
        for n in range(1, 7):
            QTest.mouseMove(root, QPoint(start.x(), round(start.y() + (target_y - start.y()) * n / 6)))
            pump(40)
        if probe is not None:
            probe()
        QTest.mouseRelease(root, Qt.LeftButton, Qt.NoModifier, QPoint(start.x(), round(target_y)))
        pump(300)

    def test_b_drag_reorders_with_a_drop_line_persisted_new_on_top_and_rollback_safe(self) -> None:
        from jarvis.ui.preferences import UiPreferences, canonical_tree, validate_preferences, validate_tree
        store = self.data()
        store.create_project("Not")
        bridge, root = self.open_window()
        db_before = dump(self.database)
        main_before = self.prefs_path.read_text(encoding="utf-8") if self.prefs_path.exists() else None
        # Never ordered: the newest project on top.
        self.assertEqual(["Not", "Kod", "Tez"], self.projects(root))
        line = self.item(root, "sidebarProjectDropLine")
        tez, top = self.row(root, "sidebarProject", "Tez"), self.row(root, "sidebarProject", "Not")
        seen = []
        self.drag_project(root, tez, top.mapToScene(QPointF(0, 1)).y(), probe=lambda: seen.append(
            (line.isVisible(), self.rect(line).top() - self.rect(top).top())))
        self.assertTrue(seen[0][0])                                                   # the drop line shows where
        self.assertLessEqual(abs(seen[0][1]), 3)
        settled(300, lambda: self.assertEqual(["Tez", "Not", "Kod"], self.projects(root)))
        self.assertFalse(line.isVisible())
        self.assertEqual(bridge.page, "home")
        self.assertEqual(["Tez 1", "Tez 0"], self.tree_titles(root)[:2])          # its conversations travel with it
        saved = json.loads(self.order_file().read_text(encoding="utf-8"))
        self.assertEqual(1, saved["version"])
        self.assertEqual([self.tez.id], saved["projects"][:1])
        # A press without a move still toggles the project (T1), never drags.
        self.tap(root, self.row(root, "sidebarProject", "Kod"), then=lambda: self.assertIn(self.kod.id, bridge.sidebarTree["closed"]))
        self.assertEqual(["Tez", "Not", "Kod"], self.projects(root))
        self.assertEqual(db_before, dump(self.database))                           # window preferences only
        # A new project goes on top.
        store.create_project("Yeni")
        bridge.refreshProjects()
        settled(300, lambda: self.assertEqual(["Yeni", "Tez", "Not", "Kod"], self.projects(root)))
        # A pinned project keeps its place on top (T2) and does not drag; a drop above it lands below it.
        bridge.setProjectPinned(self.kod.id, True)
        settled(300, lambda: self.assertEqual(["Kod", "Yeni", "Tez", "Not"], self.projects(root)))
        self.assertFalse(self.row(root, "sidebarProject", "Kod").property("draggable"))
        self.assertTrue(self.row(root, "sidebarProject", "Not").property("draggable"))
        kod = self.row(root, "sidebarProject", "Kod")
        self.drag_project(root, self.row(root, "sidebarProject", "Not"), kod.mapToScene(QPointF(0, 1)).y())
        settled(300, lambda: self.assertEqual(["Kod", "Not", "Yeni", "Tez"], self.projects(root)))
        # Remembered across a reopen.
        root.hide()
        bridge, root = self.open_window()
        self.assertEqual(["Kod", "Not", "Yeni", "Tez"], self.projects(root))
        # Rollback: the main preferences and the tree file keep their exact shapes (a schema-10
        # release reads both whole); a bad order file reads as "never ordered".
        main_after = self.prefs_path.read_text(encoding="utf-8") if self.prefs_path.exists() else None
        if main_after is not None:
            self.assertEqual(json.loads(main_after), validate_preferences(json.loads(main_after)))
        self.assertEqual(main_before, main_after)
        tree = json.loads(self.tree_file().read_text(encoding="utf-8"))
        self.assertEqual(set(canonical_tree()), set(tree))
        self.assertEqual(tree, validate_tree(tree))
        self.order_file().write_text('{"version": 1, "projects": ["x", "x"]}', encoding="utf-8")
        self.assertEqual([], UiPreferences(self.prefs_path).project_order)
        self.assertEqual(0, self.execute_calls())

    def test_c_move_project_refuses_unknown_and_self_targets(self) -> None:
        self.data()
        bridge, _root = self.open_window()
        self.assertFalse(bridge.moveProject("p-unknown", ""))
        self.assertFalse(bridge.moveProject(self.tez.id, self.tez.id))
        self.assertFalse(bridge.moveProject(self.tez.id, "p-unknown"))
        self.assertFalse(self.order_file().exists())
        self.assertTrue(bridge.moveProject(self.kod.id, ""))                       # to the end
        self.assertEqual([self.tez.id, self.kod.id], [i for i in bridge.sidebarProjectOrder if i != self.eski.id])


class PickerTests(G6SB3Case):
    def picker_menu(self, root):
        menu = self.popup_named(root, "projectPickerMenu")
        self.tap(root, self.item(root, "projectPickerChip"), until=lambda: bool(menu.property("opened")))
        self.assertTrue(wait_until(lambda: bool(menu.property("opened")), 1000))
        return menu

    def choices(self, menu) -> list:
        return [i.property("label") for i in self.popup_items(menu, "projectPickerItem") if i.isVisible()]

    def owner(self, store, bridge):
        return store.load_conversation(bridge.conversationId).project_id

    def test_d_the_picker_search_proje_yok_and_a_choice_before_the_first_message(self) -> None:
        store = self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        picker = self.item(root, "projectPicker")
        self.tap(root, self.item(root, "sidebarNewChat"), then=lambda: self.assertTrue(picker.isVisible()))
        first = bridge.conversationId
        self.assertIsNone(self.owner(store, bridge))
        self.assertEqual("Choose project", self.item(root, "projectPickerLabel").property("text"))
        # Under the composer.
        self.assertGreaterEqual(self.rect(picker).top(), self.rect(self.item(root, "canonicalComposer")).bottom() - 0.5)
        menu = self.picker_menu(root)
        none = self.popup_items(menu, "projectPickerNone")[0]
        self.assertEqual("No project", none.property("label"))
        self.assertTrue(none.property("checked"))
        self.assertEqual(["Kod", "Tez"], self.choices(menu))                         # active only (V9), sidebar order
        self.assertEqual("New project…", self.popup_items(menu, "projectPickerNew")[0].property("label"))
        search = self.popup_items(menu, "projectPickerSearch")[0]
        search.setProperty("text", "KO")
        settled(100, lambda: self.assertEqual(["Kod"], self.choices(menu)))
        search.setProperty("text", "zzz")
        settled(100, lambda: (self.assertEqual([], self.choices(menu)),
                              self.assertTrue(self.popup_items(menu, "projectPickerNoMatch")[0].isVisible())))
        search.setProperty("text", "")
        settled(100, lambda: self.assertEqual(["Kod", "Tez"], self.choices(menu)))
        # A project: the empty conversation is now that project's (a new one, made by the store).
        tez = [i for i in self.popup_items(menu, "projectPickerItem") if i.property("label") == "Tez"][0]
        self.tap(root, tez, until=lambda: not menu.property("visible"))
        settled(300, lambda: (self.assertEqual(self.tez.id, self.owner(store, bridge)),
                              self.assertEqual("Tez", self.item(root, "projectPickerLabel").property("text"))))
        made = bridge.conversationId
        self.assertEqual([], list(store.load_conversation_state(made).turns))
        # Proje yok again: back to the empty plain one (no second empty conversation).
        menu = self.picker_menu(root)
        self.tap(root, self.popup_items(menu, "projectPickerNone")[0], until=lambda: not menu.property("visible"))
        settled(300, lambda: self.assertEqual(first, bridge.conversationId))
        # And Tez again: its empty one is reused.
        self.assertTrue(bridge.setNewChatProject(self.tez.id))
        self.assertEqual(made, bridge.conversationId)
        # Refused: an archived or unknown project.
        self.assertFalse(bridge.setNewChatProject(self.eski.id))
        self.assertFalse(bridge.setNewChatProject("p-unknown"))
        self.assertEqual(made, bridge.conversationId)
        self.assertEqual(0, self.execute_calls())

    def test_e_the_default_is_the_current_project_page_or_conversation_else_none(self) -> None:
        store = self.data()
        bridge, root = self.open_window()
        # In a project conversation: that project.
        self.assertTrue(bridge.openConversation(self.kodc.id))
        self.tap(root, self.item(root, "sidebarNewChat"), then=lambda: self.assertNotEqual(self.kodc.id, bridge.conversationId))
        self.assertEqual(self.kod.id, self.owner(store, bridge))
        settled(200, lambda: self.assertEqual("Kod", self.item(root, "projectPickerLabel").property("text")))
        # On a project's page (the sidebar is the home page's; its page asks the same): that
        # project, and the New chat screen opens.
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        bridge.showProject(self.tez.id)
        pump(200)
        self.assertTrue(bridge.startNewChat())
        self.assertEqual(("home", self.tez.id), (bridge.page, self.owner(store, bridge)))
        # An archived project's page: none.
        bridge.showProject(self.eski.id)
        pump(200)
        self.assertTrue(bridge.startNewChat())
        self.assertIsNone(self.owner(store, bridge))
        # In a plain conversation, on the Projects list: none.
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        bridge.showProject("")
        pump(200)
        self.assertTrue(bridge.startNewChat())
        self.assertEqual("home", bridge.page)
        self.assertIsNone(self.owner(store, bridge))
        settled(200, lambda: self.assertEqual("Choose project", self.item(root, "projectPickerLabel").property("text")))
        self.assertEqual(0, self.execute_calls())

    def test_f_the_first_message_fixes_the_owner_and_6c_bind_is_unchanged(self) -> None:
        store = self.data()
        bridge = self.bridge(task_proposal("code_analysis"), task_proposal("code_analysis"))
        root = self.window(bridge)
        pump(100)
        self.assertTrue(bridge.startNewChat())
        self.assertTrue(bridge.setNewChatProject(self.tez.id))
        picker = self.item(root, "projectPicker")
        settled(200, lambda: self.assertTrue(picker.isVisible()))
        chat = bridge.conversationId
        bridge.submitText("Bu projeyi incele.")
        settled(400, lambda: self.assertTrue(bridge.approvalPending))
        bridge.deny()
        self.assertTrue(wait_until(lambda: not bridge.busy, 5000))
        bridge.waitIdle()
        settled(200, lambda: self.assertFalse(picker.isVisible()))                  # the picker is gone
        self.assertFalse(bridge.setNewChatProject(""))                               # the owner is fixed
        self.assertEqual((chat, self.tez.id), (bridge.conversationId, self.owner(store, bridge)))
        self.assertFalse(bridge.canAttach)                                           # a project's: no bind
        # A plain chat after its first message: the 6C one-way bind is offered as before.
        self.assertTrue(bridge.startNewChat())
        self.assertTrue(bridge.setNewChatProject(""))
        bridge.submitText("Bu projeyi incele.")
        settled(400, lambda: self.assertTrue(bridge.approvalPending))
        bridge.deny()
        self.assertTrue(wait_until(lambda: not bridge.busy, 5000))
        bridge.waitIdle()
        self.assertIsNone(self.owner(store, bridge))
        self.assertTrue(bridge.canAttach)
        self.assertEqual(0, self.execute_calls())

    def test_g_yeni_proje_opens_the_existing_new_project_dialog(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.startNewChat())
        pump(200)
        menu = self.picker_menu(root)
        # The New chat screen gives way to the Projects page, where the dialog opens.
        self.tap(root, self.popup_items(menu, "projectPickerNew")[0], until=lambda: bridge.page == "projects")
        dialog = self.popup_named(root, "newProjectDialog")
        self.assertTrue(wait_until(lambda: bool(dialog.property("visible")), 1000))
        self.assertEqual("projects", bridge.page)
        self.assertEqual(0, self.execute_calls())


if __name__ == "__main__":  # pragma: no cover
    import unittest
    unittest.main()
