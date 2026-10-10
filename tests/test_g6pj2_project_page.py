"""Gate 6G Projects UX slice 2 (G6PJ2): P2 project pins and P3 the project page.

A. Pins: the card menu's Pin / Unpin writes the sidebar's own project pins
   (the sibling tree file, newest pin first), the card shows a pin mark,
   pinned cards lead "Your projects" whatever the sort, and the main
   preferences document never gains a key (rollback: a schema-10 release
   reads it whole); a malformed tree file reads as no pins.
B. Page: the name header, the composer, SABİTLENEN (this project's pinned
   conversations) and SON SOHBETLER (the rest) from the real store.
C. New chat: the composer starts a conversation OWNED by this project, the
   typed text its first message; empty, it only opens a new one.
D. Dialogs: the right column's rows open the existing instruction editor
   and file panel as dialogs; closing one returns to the page.
E. Folder: the current 6H link shows; no link is said ("No folder linked");
   the row opens the folder dialog (G6H2).
F. Archived: the page is read only (composer, editor and add off; Unarchive).

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtTest import QTest

from jarvis.memory import SQLiteMemoryStore
from jarvis.ui.i18n import localization, tx
from jarvis.ui.preferences import UiPreferences, canonical_preferences
from tests import test_ui
from tests.helpers import settled
from tests.test_ui import _APP, UiTestCase, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

FOLDER = "C:\\Projeler\\Evim tadilatı"


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

    def under(self, node, name) -> list:
        found = []

        def walk(n):
            if n.objectName() == name:
                found.append(n)
            for child in n.childItems():
                walk(child)
        walk(node)
        return found

    def dialog(self, root, name):
        found = root.findChild(QObject, name)
        self.assertIsNotNone(found, name)
        return found

    def press(self, root, item) -> None:
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        pump(200)

    def card(self, root, name):
        return next(c for c in self.visible(root, "projectCard") if self.under(c, "projectCardName")[0].property("text") == name)

    def card_names(self, root) -> list[str]:
        cards = sorted(self.visible(root, "projectCard"), key=lambda c: (round(c.mapToScene(QPointF(0, 0)).y()),
                                                                        c.mapToScene(QPointF(0, 0)).x()))
        return [self.under(c, "projectCardName")[0].property("text") for c in cards]

    def card_menu(self, root, name) -> list[str]:
        self.press(root, self.under(self.card(root, name), "projectCardMenu")[0])
        self.assertTrue(self.dialog(root, "projectCardMenuSurface").property("visible"))
        return [r.property("label") for r in self.visible(root, "projectMenuPin")]

    def rows(self, root, name) -> list[str]:
        return [r.property("title") for r in self.visible(root, name)]


class PinTests(Case):
    def setUp(self) -> None:
        super().setUp()
        store = self.db()
        self.evim, self.tez, self.eski = store.create_project("Evim"), store.create_project("Tez"), store.create_project("Eski")
        store.set_project_active(self.eski.id, False)

    def test_a_pin_and_unpin_mark_order_and_persistence(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.showProject("")
        pump(300)
        self.click(root, "projectsSort_name")
        self.assertEqual(["Evim", "Tez"], self.card_names(root))
        self.assertEqual([False, False], [self.under(self.card(root, n), "projectCardPin")[0].isVisible() for n in ("Evim", "Tez")])
        self.assertEqual(["Pin"], self.card_menu(root, "Tez"))
        self.click(root, "projectMenuPin")
        self.assertEqual([self.tez.id], bridge.sidebarTree["pinned_projects"])
        self.assertEqual(["Tez", "Evim"], self.card_names(root))                     # pinned first, whatever the sort
        self.assertTrue(self.under(self.card(root, "Tez"), "projectCardPin")[0].isVisible())
        self.assertFalse(self.under(self.card(root, "Evim"), "projectCardPin")[0].isVisible())
        self.assertEqual(["Pin"], self.card_menu(root, "Evim"))
        self.click(root, "projectMenuPin")
        self.assertEqual(["Evim", "Tez"], self.card_names(root))                     # the newest pin on top (as the sidebar)
        # Persisted in the sidebar's tree file, read back by a new preferences object.
        self.assertEqual([self.evim.id, self.tez.id], UiPreferences(self.prefs_path).tree["pinned_projects"])
        self.assertEqual(["Unpin"], self.card_menu(root, "Evim"))
        self.click(root, "projectMenuPin")
        self.assertEqual(["Tez", "Evim"], self.card_names(root))
        self.assertFalse(self.under(self.card(root, "Evim"), "projectCardPin")[0].isVisible())
        self.assertEqual([self.tez.id], UiPreferences(self.prefs_path).tree["pinned_projects"])
        # The archive tab: no pin mark, no Pin row (an archived card offers Unarchive only).
        self.click(root, "projectsTab_archived")
        self.assertEqual(["Eski"], self.card_names(root))
        self.assertFalse(self.under(self.card(root, "Eski"), "projectCardPin")[0].isVisible())
        self.assertEqual([], self.card_menu(root, "Eski"))

    def test_a_rollback_the_main_document_never_gains_a_key(self) -> None:
        bridge = self.bridge()
        bridge.setTheme("dark")                                                   # the main document is written
        bridge.setProjectPinned(self.evim.id, True)
        main = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.assertEqual(set(canonical_preferences()), set(main))                 # a schema-10 release reads it whole
        self.assertNotIn("pinned_projects", json.dumps(main))
        tree = json.loads(self.prefs_path.with_suffix(".sidebar-tree.json").read_text(encoding="utf-8"))
        self.assertEqual([self.evim.id], tree["pinned_projects"])
        # A malformed tree file is no pins (and the theme survives).
        self.prefs_path.with_suffix(".sidebar-tree.json").write_text('{"pinned_projects": 5}', encoding="utf-8")
        again = UiPreferences(self.prefs_path)
        self.assertEqual([], again.tree["pinned_projects"])
        self.assertEqual("dark", again.data["theme"])


class PageTests(Case):
    def stage(self, reply: str = "Tamam."):
        store = self.db()
        self.project = store.create_project("Evim tadilatı")
        self.other = store.create_project("Tez")
        self.mutfak = store.create_conversation(self.project.id, title="Mutfak teklifleri")
        self.banyo = store.create_conversation(self.project.id, title="Banyo fayansı")
        self.elsewhere = store.create_conversation(self.other.id, title="Tez özeti")
        old = store.create_conversation(self.project.id, title="Eski notlar")
        store.archive_conversation(old.id)
        bridge = self.bridge(json.dumps({"kind": "chat", "reply": reply}))
        bridge.pinConversation(self.banyo.id)
        bridge.pinConversation(self.elsewhere.id)
        root = self.window(bridge)
        root.resize(1920, 1080)
        bridge.showProject(self.project.id)
        pump(400)
        return store, bridge, root

    def test_b_header_composer_pinned_and_recent_from_the_store(self) -> None:
        store, bridge, root = self.stage()
        self.assertEqual("Evim tadilatı", self.item(root, "projectDetailName").property("text"))
        self.assertTrue(self.item(root, "projectComposer").isVisible())
        self.assertEqual("New conversation in Evim tadilatı", self.item(root, "projectComposerInput").property("placeholderText"))
        self.assertTrue(self.item(root, "projectNewConversation").isEnabled())
        self.assertTrue(self.item(root, "projectPinnedHeading").isVisible())
        self.assertEqual(["Banyo fayansı"], self.rows(root, "projectPinnedRow"))       # only this project's pin
        self.assertEqual(["Mutfak teklifleri"], self.rows(root, "projectConversationRow"))
        self.assertEqual(["Eski notlar"], self.rows(root, "projectArchivedConversationRow"))
        self.assertEqual("No folder linked", self.item(root, "projectFolderRowBody").property("text"))   # G6H2: said, not hidden
        self.assertEqual([], [n for n in ("projectMemoryRow", "projectScheduledRow", "projectJobsHeading", "projectTabs")
                              if self.visible(root, n)])                            # nothing before its release
        # A pinned row opens its conversation.
        self.press(root, self.visible(root, "projectPinnedRow")[0])
        settled(200, lambda: self.assertEqual((self.banyo.id, "home"), (bridge.conversationId, bridge.page)))
        # Unpinned, it is a recent one again; no pin left, no heading.
        bridge.unpinConversation(self.banyo.id)
        bridge.showProject(self.project.id)
        pump(300)
        self.assertEqual([], self.visible(root, "projectPinnedHeading"))
        self.assertEqual(sorted(["Mutfak teklifleri", "Banyo fayansı"]), sorted(self.rows(root, "projectConversationRow")))

    def test_c_new_chat_from_the_page_lands_in_this_project(self) -> None:
        store, bridge, root = self.stage()
        before = {c.id for c in store.list_conversations(project_id=self.project.id)}
        self.item(root, "projectComposerInput").setProperty("text", "Fayans için üç teklif karşılaştır")
        pump(50)
        self.click(root, "projectNewConversation")
        settled(400, lambda: self.assertEqual("home", bridge.page))
        created = [c for c in store.list_conversations(project_id=self.project.id) if c.id not in before]
        self.assertEqual([bridge.conversationId], [c.id for c in created])
        self.assertEqual(self.project.id, store.load_conversation(bridge.conversationId).project_id)
        self.assertIn("Fayans için üç teklif karşılaştır", [e.get("text", e.get("msg")) for e in bridge.feed if e.get("kind") == "user"])
        self.assertEqual(1, len(self.model.calls))
        # Empty: a new conversation in this project opens, nothing is sent.
        bridge.showProject(self.project.id)
        pump(300)
        self.assertEqual("", self.item(root, "projectComposerInput").property("text"))
        self.click(root, "projectNewConversation")
        settled(300, lambda: self.assertEqual("home", bridge.page))
        self.assertEqual(self.project.id, store.load_conversation(bridge.conversationId).project_id)
        self.assertEqual(1, len(self.model.calls))

    def test_d_rows_open_the_existing_editor_and_file_panel(self) -> None:
        store, bridge, root = self.stage()
        bridge.saveProjectInstruction(self.project.id, "Teklifleri tablo halinde karşılaştır.", "internal")
        pump(100)
        self.assertEqual("Teklifleri tablo halinde karşılaştır.", self.item(root, "projectInstructionRowBody").property("text"))
        self.assertEqual("No active file", self.item(root, "projectFilesRowBody").property("text"))
        page = self.item(root, "projectsPage")
        instruction = self.dialog(root, "projectInstructionDialog")
        self.click(root, "projectInstructionRow", then=lambda: self.assertTrue(instruction.property("opened")))
        self.assertEqual("instruction", page.property("section"))
        self.assertEqual("Teklifleri tablo halinde karşılaştır.", self.item(root, "instructionEditor").property("text"))
        self.assertTrue(self.item(root, "instructionSave").isEnabled())               # the stored class is chosen
        self.click(root, "instructionDialogClose", then=lambda: self.assertFalse(instruction.property("visible")))
        self.assertEqual("conversations", page.property("section"))
        files = self.dialog(root, "projectFilesDialog")
        self.click(root, "projectFilesRow", then=lambda: self.assertTrue(files.property("opened")))
        self.assertEqual("files", page.property("section"))
        self.assertTrue(self.item(root, "fileCountActive").isVisible())
        self.assertFalse(self.item(root, "fileAdd").isEnabled())                      # classification first (6B)
        QTest.keyClick(root, Qt.Key_Escape)
        settled(200, lambda: self.assertFalse(files.property("visible")))
        self.assertEqual("conversations", page.property("section"))
        # The card menu's Edit opens the page on the instruction dialog.
        bridge.showProject("")
        pump(300)
        self.press(root, self.under(self.card(root, "Tez"), "projectCardMenu")[0])
        self.click(root, "projectMenuEdit", then=lambda: (
            self.assertEqual(self.other.id, bridge.openProject["id"]),
            self.assertTrue(instruction.property("opened")),
        ))
        # Leaving the page takes an open dialog with it: no scrim is left over the next page.
        bridge.setPage("home")
        settled(300, lambda: self.assertEqual([], self.visible(root, "jarvisDialogScrim")))

    def test_d_purge_hands_back_to_the_file_dialog(self) -> None:
        store, bridge, root = self.stage()
        path = self.root / "fixture-files" / "rapor.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("veri " * 30, encoding="utf-8")
        bridge.backend.project_file("add", self.project.id, path=str(path), classification="internal")
        file_id = store.list_project_files(self.project.id)[0]["id"]
        bridge.backend.project_file("retire", self.project.id, file_id=file_id)
        bridge.refreshProjects()
        self.click(root, "projectFilesRow")
        files, purge = self.dialog(root, "projectFilesDialog"), self.dialog(root, "purgeDialog")
        self.click(root, "filePurge", then=lambda: self.assertTrue(purge.property("opened")))
        self.assertFalse(files.property("visible"))                                 # one dialog, one scrim at a time
        self.assertEqual(1, len(self.visible(root, "jarvisDialogScrim")))
        self.click(root, "purgeCancel", then=lambda: self.assertTrue(files.property("opened")))
        self.assertEqual("files", self.item(root, "projectsPage").property("section"))
        self.assertEqual("retired", store.list_project_files(self.project.id)[0]["lifecycle"])

    def test_e_folder_row_shows_the_link_or_says_none_and_opens_its_dialog(self) -> None:
        store, bridge, root = self.stage()
        self.assertEqual({}, bridge.projectFolder)
        self.assertEqual("No folder linked", self.item(root, "projectFolderRowBody").property("text"))
        store.link_explicit_user_project_folder(self.project.id, FOLDER, "picked")
        bridge.refreshProjects()
        pump(100)
        self.assertEqual(FOLDER, bridge.projectFolder["path"])
        row = self.visible(root, "projectFolderRow")[0]
        self.assertEqual(FOLDER, self.item(root, "projectFolderRowBody").property("text"))
        self.assertTrue(row.property("activeFocusOnTab"))                            # G6H2: it opens the folder dialog
        self.press(root, row)
        self.assertEqual("folder", self.item(root, "projectsPage").property("section"))
        self.assertEqual([], [d for d in ("projectInstructionDialog", "projectFilesDialog") if self.dialog(root, d).property("visible")])
        self.assertTrue(self.dialog(root, "projectFolderDialog").property("visible"))
        # G6H2 (Salih 6H answers 2026-10-10): unlink is offered; changing in place and
        # opening in Explorer are not.
        self.assertEqual(1, len(self.visible(root, "projectFolderUnlink")))
        self.assertEqual([], [n for n in ("projectFolderChange", "projectFolderOpen") if self.all_items(root, n)])
        self.click(root, "folderDialogClose")
        # Another project shows its own (none); an unlinked one shows none.
        bridge.showProject(self.other.id)
        pump(300)
        self.assertEqual("No folder linked", self.item(root, "projectFolderRowBody").property("text"))
        store.unlink_project_folder(self.project.id)
        bridge.showProject(self.project.id)
        pump(300)
        self.assertEqual(({}, "No folder linked"), (bridge.projectFolder, self.item(root, "projectFolderRowBody").property("text")))

    def test_f_an_archived_project_page_is_read_only(self) -> None:
        store, bridge, root = self.stage()
        store.link_explicit_user_project_folder(self.project.id, FOLDER, "picked")
        bridge.archiveProject(self.project.id)
        settled(300, lambda: self.assertFalse(bridge.openProject["active"]))
        self.assertTrue(self.item(root, "projectArchivedBanner").isVisible())
        self.assertFalse(self.item(root, "projectComposerInput").isEnabled())
        self.assertFalse(self.item(root, "projectNewConversation").isEnabled())
        self.assertEqual("Unarchive the project first", self.item(root, "projectNewConversationTip").property("text"))
        self.assertEqual([], self.visible(root, "projectRename") + self.visible(root, "projectArchive"))
        self.assertEqual(1, len(self.visible(root, "projectDetailUnarchive")))
        self.assertEqual(FOLDER, self.item(root, "projectFolderRowBody").property("text"))   # still shown, read only
        before = len(store.list_conversations(project_id=self.project.id))
        self.press(root, self.item(root, "projectNewConversation"))
        self.assertEqual(before, len(store.list_conversations(project_id=self.project.id)))
        self.assertEqual("projects", bridge.page)
        self.click(root, "projectInstructionRow")
        self.assertTrue(self.item(root, "instructionEditor").property("readOnly"))
        self.assertFalse(self.item(root, "instructionSave").isEnabled())
        self.click(root, "instructionDialogClose")
        self.click(root, "projectFilesRow")
        self.assertFalse(self.item(root, "fileAdd").isEnabled())
        self.assertEqual(0, len(self.model.calls))


class CatalogTests(UiTestCase):
    def test_g_new_strings_are_turkish(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual(("Sabitle", "Sabitlemeyi kaldır"), (tx("ProjectsPage", "Pin"), tx("ProjectsPage", "Unpin")))
        self.assertEqual(("SABİTLENEN", "SON SOHBETLER"), (tx("ProjectPage", "PINNED"), tx("ProjectPage", "RECENT CONVERSATIONS")))
        self.assertEqual(("Talimatlar", "Dosyalar", "Klasör"),
                         (tx("ProjectPage", "Instructions"), tx("ProjectPage", "Files"), tx("ProjectPage", "Folder")))
        self.assertEqual("3 aktif dosya", tx("ProjectPage", "%1 active files", 3))
