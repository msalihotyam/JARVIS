"""V2 Projects Gate 6B: instruction editor, file panel and the top-bar
project selector, through the real bridge, the real app paths and the real
QML.

A. Instruction: save / clear / show round-trip; the store's cap and
   classification refusals shown, nothing written; redacted stored text
   shown and said; archived -> read-only and a forced save refused.
B. Files: add / retire / restore / purge; counters from the store listing;
   purge only on retired rows (active project), on active and retired rows
   (archived project); the typed name gates the call and the ROW's id is the
   store's confirmation; residue text; refusals shown as returned.
C. Selector: owner / Global, the menu, where a choice goes, "+ New
   conversation" in the current scope, geometry, the busy guard.
D. Authority: the selected project changes no approval decision.
E. Turkish catalog.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

from PySide6.QtCore import QUrl

from jarvis.memory import SQLiteMemoryStore
from tests.test_intent import task_proposal
from tests.test_projects_ui import ProjectsUiCase
from tests.helpers import settled
from tests.test_ui import UiTestCase, _APP, ambient_on, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401


class InstructionTests(ProjectsUiCase):
    def open(self, name: str = "Evim", archived: bool = False):
        store = self.db()
        project = store.create_project(name)
        bridge = self.bridge()
        root = self.window(bridge)
        if archived:
            store.set_project_active(project.id, False)
        bridge.showProject(project.id)
        pump(300)
        self.click(root, "projectInstructionRow")              # G6PJ2 (6G P3): was the instruction tab; a row opens its dialog
        pump(200)
        return store, project, bridge, root

    def versions(self, store: SQLiteMemoryStore, project_id: str) -> int:
        return len(store.list_project_instruction_history(project_id))

    def test_a_save_clear_and_show_round_trip(self) -> None:
        store, project, bridge, root = self.open()
        editor, save = self.item(root, "instructionEditor"), self.item(root, "instructionSave")
        self.assertEqual("", editor.property("text"))
        self.assertFalse(save.isEnabled())                               # no preselected classification
        self.assertIn("PUBLIC or INTERNAL", self.item(root, "instructionReason").property("text"))
        self.assertIn("never grants", self.item(root, "instructionPrecedence").property("text"))
        editor.setProperty("text", "Yanıtları kısa tut.")
        pump(60)
        self.assertEqual("19 / 2000", self.item(root, "instructionCounter").property("text"))
        self.click(root, "instructionClass_internal", then=lambda: (
            self.assertTrue(save.isEnabled()),
        ))
        self.click(root, "instructionSave")
        current = store.load_project_instruction(project.id)
        self.assertEqual(("Yanıtları kısa tut.", "internal"), (current.text, current.classification.value))
        self.assertEqual("", bridge.projectInstructionNotice)
        self.click(root, "instructionClear")
        current = store.load_project_instruction(project.id)
        self.assertEqual(("", "internal"), (current.text, current.classification.value))
        self.assertEqual("", editor.property("text"))                   # the STORED text is shown
        # A fresh window shows the stored version, classification prefilled.
        store.save_explicit_user_project_instruction(project.id, "Kaynak göster.", "public")
        bridge.showProject("")
        bridge.showProject(project.id)
        settled(200, lambda: (
            self.assertEqual("Kaynak göster.", editor.property("text")),
            self.assertTrue(save.isEnabled()),
            self.assertEqual(0, self.execute_calls()),
        ))

    def test_a_refusals_write_nothing_and_are_shown(self) -> None:
        store, project, bridge, root = self.open()
        editor = self.item(root, "instructionEditor")
        editor.setProperty("text", "x" * 2001)
        pump(60)
        self.assertEqual("2001 / 2000", self.item(root, "instructionCounter").property("text"))
        self.click(root, "instructionClass_public")
        self.click(root, "instructionSave", then=lambda: (
            self.assertIn("longer than 2000", self.item(root, "instructionNotice").property("text")),
            self.assertEqual("x" * 2001, editor.property("text")),           # what was typed stays
        ))
        bridge.saveProjectInstruction(project.id, "kısa", "")
        pump(60)
        self.assertIn("classification is required", bridge.projectInstructionNotice)
        bridge.saveProjectInstruction(project.id, "kısa", "secret")
        pump(60)
        self.assertIn("only be PUBLIC or INTERNAL", bridge.projectInstructionNotice)
        self.assertEqual(0, self.versions(store, project.id))

    def test_a_redacted_stored_text_is_shown_and_said(self) -> None:
        store, project, bridge, root = self.open()
        typed = "Notlar C:\\Users\\salih\\gizli\\plan.txt dosyasında."
        bridge.saveProjectInstruction(project.id, typed, "internal")
        pump(100)
        stored = store.load_project_instruction(project.id).text
        self.assertNotEqual(typed, stored)
        self.assertNotIn("salih", stored)
        self.assertEqual(stored, self.item(root, "instructionEditor").property("text"))
        self.assertIn("redacted", self.item(root, "instructionNotice").property("text"))

    def test_a_archived_is_read_only_and_the_store_still_refuses(self) -> None:
        store, project, bridge, root = self.open(archived=True)
        self.assertTrue(self.item(root, "instructionEditor").property("readOnly"))
        self.assertFalse(self.item(root, "instructionSave").isEnabled())
        self.assertFalse(self.item(root, "instructionClear").isEnabled())
        self.assertIn("read-only", self.item(root, "instructionReason").property("text"))
        bridge.saveProjectInstruction(project.id, "zorla", "internal")
        pump(60)
        self.assertIn("inactive project", bridge.projectInstructionNotice)
        self.assertEqual(0, self.versions(store, project.id))


class FileTests(ProjectsUiCase):
    def file(self, folder: str, name: str, text: str) -> str:
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return QUrl.fromLocalFile(str(path)).toString()

    def open(self):
        store = self.db()
        project = store.create_project("Evim")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.showProject(project.id)
        pump(300)
        self.click(root, "projectFilesRow")                    # G6PJ2 (6G P3): was the files tab; a row opens its dialog
        pump(200)
        return store, project, bridge, root

    def counters(self, root) -> tuple[str, str, str]:
        return tuple(self.item(root, name).property("text") for name in ("fileCountActive", "fileCountRetained", "fileCountChars"))

    def expected(self, store, project_id) -> tuple[str, str, str]:
        rows = store.list_project_files(project_id)
        active = sum(r["lifecycle"] == "active" for r in rows)
        chars = sum(r["char_count"] for r in rows)
        return (f"Active {active}/20", f"Retained {len(rows)}/40", f"Characters {chars:,}/2,000,000")

    def rows(self, root) -> dict[str, dict]:
        """Visible file rows by id: which buttons each one shows."""
        shown = {}
        for row in self.all_items(root, "fileRow"):
            if not row.isVisible():
                continue
            file = row.property("file").toVariant() if hasattr(row.property("file"), "toVariant") else row.property("file")
            buttons = {name: [b for b in self.all_items(root, name) if b.parentItem().parentItem() is row][0]
                       for name in ("fileRetire", "fileRestore", "filePurge")}
            shown[file["id"]] = {name: (b.isVisible(), b.isEnabled()) for name, b in buttons.items()}
        return shown

    def spy(self, bridge) -> list:
        calls, real = [], bridge.backend.project_file

        def record(action, project_id, **options):
            calls.append((action, options))
            return real(action, project_id, **options)
        bridge.backend.project_file = record
        return calls

    @ambient_on          # THIZ: it reads a colour
    def test_b_add_retire_restore_purge_and_counters(self) -> None:
        store, project, bridge, root = self.open()
        self.assertEqual(("Active 0/20", "Retained 0/40", "Characters 0/2,000,000"), self.counters(root))
        self.assertFalse(self.item(root, "fileAdd").isEnabled())         # classification first
        bridge.addProjectFile(project.id, self.file("a", "notes.md", "alpha " * 100), "")
        pump(60)
        self.assertIn("classification is required", bridge.projectFileNotice)
        self.assertEqual([], store.list_project_files(project.id))
        self.click(root, "fileClass_internal")
        self.assertTrue(self.item(root, "fileAdd").isEnabled())
        for folder, text in (("a", "alpha " * 100), ("b", "beta " * 50)):
            bridge.addProjectFile(project.id, self.file(folder, "notes.md", text), "internal")
            pump(60)
            self.assertEqual("", bridge.projectFileNotice)
            self.assertEqual(self.expected(store, project.id), self.counters(root))
        first, second = (r["id"] for r in store.list_project_files(project.id))
        self.assertEqual(["notes.md", "notes.md"], self.texts(root, "fileRowName"))
        meta = self.texts(root, "fileRowMeta")[0]
        self.assertIn("INTERNAL", meta)
        self.assertIn("600 bytes", meta)
        self.assertIn("active", meta)
        # Active project: no Purge on an active row.
        self.assertEqual({first: {"fileRetire": (True, True), "fileRestore": (False, True), "filePurge": (False, True)},
                          second: {"fileRetire": (True, True), "fileRestore": (False, True), "filePurge": (False, True)}},
                         self.rows(root))
        bridge.retireProjectFile(project.id, first)
        bridge.retireProjectFile(project.id, second)
        pump(60)
        self.assertEqual(self.expected(store, project.id), self.counters(root))
        self.assertEqual(("Active 0/20", "Retained 2/40"), self.counters(root)[:2])
        self.assertTrue(self.rows(root)[first]["filePurge"][0])
        bridge.restoreProjectFile(project.id, first)
        pump(60)
        self.assertEqual(self.expected(store, project.id), self.counters(root))
        self.assertEqual({"fileRetire": (True, True), "fileRestore": (False, True), "filePurge": (False, True)},
                         self.rows(root)[first])
        # Purge: a wrong name calls nothing; the right name purges THAT row only.
        calls = self.spy(bridge)
        self.assertFalse(bridge.purgeProjectFile(project.id, second, "notes"))
        self.assertFalse(bridge.purgeProjectFile(project.id, first, "notes.md"))      # active row: not offered
        self.assertEqual([], [c for c in calls if c[0] == "purge"])
        self.assertTrue(bridge.purgeProjectFile(project.id, second, "notes.md"))
        pump(60)
        self.assertEqual([("purge", {"file_id": second, "confirm": second})], [c for c in calls if c[0] == "purge"])
        lifecycles = {r["id"]: r["lifecycle"] for r in store.list_project_files(project.id, include_purged=True)}
        self.assertEqual({first: "active", second: "purged"}, lifecycles)
        self.assertEqual(self.expected(store, project.id), self.counters(root))
        self.assertEqual(0, self.execute_calls())

    def test_b_purge_dialog_names_the_row_says_the_residue_and_needs_the_name(self) -> None:
        store, project, bridge, root = self.open()
        bridge.addProjectFile(project.id, self.file("a", "rapor.txt", "gizli veri " * 30), "sensitive")
        pump(60)
        file_id = store.list_project_files(project.id)[0]["id"]
        bridge.retireProjectFile(project.id, file_id)
        pump(100)
        self.click(root, "filePurge")
        settled(200, lambda: (
            self.assertEqual("rapor.txt", self.item(root, "purgeIdentity").property("text")),
            self.assertIn("SENSITIVE", self.item(root, "purgeIdentityMeta").property("text")),
        ))
        residue = self.item(root, "purgeResidue").property("text")
        for part in ("does NOT reach", "provider", "approval previews", "conversation history", "backups", "WAL"):
            self.assertIn(part, residue)
        confirm = self.item(root, "purgeConfirm")
        self.assertFalse(confirm.isEnabled())
        self.item(root, "purgeConfirmName").setProperty("text", "rapor")
        pump(60)
        self.assertFalse(confirm.isEnabled())
        self.item(root, "purgeConfirmName").setProperty("text", "rapor.txt")
        pump(60)
        self.assertTrue(confirm.isEnabled())
        self.click(root, "purgeConfirm")
        self.assertEqual("purged", store.list_project_files(project.id, include_purged=True)[0]["lifecycle"])

    def test_b_archived_project_purges_active_and_retired_rows_only(self) -> None:
        store, project, bridge, root = self.open()
        self.click(root, "fileClass_public")
        for folder in ("a", "b"):
            bridge.addProjectFile(project.id, self.file(folder, f"{folder}.md", folder * 40), "public")
            pump(60)
        first, second = (r["id"] for r in store.list_project_files(project.id))
        bridge.retireProjectFile(project.id, second)
        pump(60)
        bridge.archiveProject(project.id)
        settled(200, lambda: (
            self.assertFalse(self.item(root, "fileAdd").isEnabled()),
            self.assertIn("Purge stays available", self.item(root, "fileReason").property("text")),
            self.assertEqual({first: {"fileRetire": (True, False), "fileRestore": (False, False), "filePurge": (True, True)},
                              second: {"fileRetire": (False, False), "fileRestore": (True, False), "filePurge": (True, True)}},
                             self.rows(root)),
        ))
        bridge.retireProjectFile(project.id, first)
        pump(60)
        self.assertIn("archived", bridge.projectFileNotice)                 # the store's refusal, as returned
        self.assertTrue(bridge.purgeProjectFile(project.id, first, "a.md"))
        pump(60)
        self.assertEqual("purged", {r["id"]: r["lifecycle"] for r in store.list_project_files(
            project.id, include_purged=True)}[first])
        self.assertEqual(self.expected(store, project.id), self.counters(root))

    def test_b_quota_refusal_is_shown_as_returned(self) -> None:
        store, project, bridge, root = self.open()
        for index in range(21):
            bridge.addProjectFile(project.id, self.file(str(index), "n.txt", f"file {index}"), "internal")
            pump(20)
        self.assertEqual(20, len(store.list_project_files(project.id)))
        self.assertEqual("A project can have at most 20 active files.", self.item(root, "fileNotice").property("text"))
        self.assertEqual("Active 20/20", self.counters(root)[0])


class SelectorTests(ProjectsUiCase):
    # Gate 6F: the selector's picks are the sidebar's rows (L4: the same calls).
    def menu_labels(self, root) -> list[str]:
        return [item.property("title") for item in self.all_items(root, "sidebarProject") if item.isVisible()]

    def row(self, root, title):
        return [i for i in self.all_items(root, "sidebarProject") if i.isVisible() and i.property("title") == title][0]

    def owner(self, root) -> str:
        return self.item(root, "chatHeaderScope").property("fullName")

    def test_c_selector_names_the_owner_and_lists_active_projects(self) -> None:
        store = self.db()
        home, thesis, old = store.create_project("Evim"), store.create_project("Tez"), store.create_project("Eski")
        store.set_project_active(old.id, False)
        owned = store.create_conversation(home.id)
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual("Global", self.owner(root))
        self.assertFalse(self.item(root, "chatHeaderScope").isVisible())        # Sidebar v2 V7: no scope line here
        bridge.openConversation(owned.id)
        pump(300)
        self.assertEqual("Evim", self.owner(root))
        self.assertTrue(self.item(root, "chatHeaderScope").isVisible())
        self.assertEqual(["Evim", "Tez"], sorted(self.menu_labels(root)))       # no archived project
        # Sidebar v2 V1: no "Genel" row; the fixed plain list holds the no-project conversations instead.
        self.assertEqual([], self.all_items(root, "sidebarGlobal"))
        self.assertEqual(sorted(c.id for c in store.list_conversations(project_id=None)),
                         sorted(i.property("itemId") for i in self.all_items(root, "sidebarConversation") if i.isVisible()))
        self.assertTrue(self.item(root, "sidebarProjectsLink").isVisible())     # "All projects" (Q10)
        self.assertEqual("All projects", self.item(root, "sidebarProjectsLink").property("tip"))
        self.click(root, "sidebarProjectsLink")
        settled(300, lambda: (
            self.assertEqual(("projects", {}), (bridge.page, bridge.openProject)),
            self.assertIsNotNone(thesis),
        ))

    def test_c_choosing_opens_the_newest_live_conversation_or_creates_one(self) -> None:
        store = self.db()
        home, thesis = store.create_project("Evim"), store.create_project("Tez")
        older = store.create_conversation(home.id)
        newest = store.create_conversation(home.id)
        store.archive_conversation(newest.id)
        bridge = self.bridge()
        root = self.window(bridge)
        global_open = bridge.conversationId
        # Sidebar v2 T1: a project row's click folds / unfolds its conversations and opens nothing.
        self.click_item(root, self.row(root, "Evim"), then=lambda: (
            self.assertEqual(global_open, bridge.conversationId),
            self.assertEqual("home", bridge.page),
            self.assertIn(home.id, bridge.sidebarTree["closed"]),
        ))
        self.click_item(root, self.row(root, "Evim"), then=lambda: (
            self.assertNotIn(home.id, bridge.sidebarTree["closed"]),
        ))
        # The 6B rule itself (openProjectScope, still the scope pick of the slot) is unchanged.
        self.assertTrue(bridge.openProjectScope(home.id))
        settled(200, lambda: (
            self.assertEqual(older.id, bridge.conversationId),                   # archived newest skipped
            self.assertEqual("home", bridge.page),
        ))
        before = len(store.list_conversations(project_id=thesis.id))
        self.assertTrue(bridge.openProjectScope(thesis.id))
        pump(200)
        self.assertEqual(before + 1, len(store.list_conversations(project_id=thesis.id)))
        self.assertEqual(thesis.id, store.load_conversation(bridge.conversationId).project_id)
        # Sidebar v2 V1: no "Genel" row; the no-project conversation is picked in the plain list.
        self.assertEqual([], self.all_items(root, "sidebarGlobal"))
        self.click_item(root, [i for i in self.all_items(root, "sidebarConversation")
                               if i.isVisible() and i.property("itemId") == global_open][0])
        self.assertEqual(global_open, bridge.conversationId)
        self.assertIsNone(store.load_conversation(bridge.conversationId).project_id)
        # An archived or unknown project is never opened through the selector.
        store.set_project_active(thesis.id, False)
        for project_id in (thesis.id, "p-" + "0" * 32):
            self.assertFalse(bridge.openProjectScope(project_id))
        self.assertEqual(global_open, bridge.conversationId)
        # The busy guard holds.
        bridge._busy = True
        self.assertFalse(bridge.openProjectScope(home.id))
        bridge._busy = False

    def click_item(self, root, item, then=None) -> None:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtTest import QTest
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        if then is not None:        # THIZ: the caller's assertions; one that holds at once keeps the old wait
            settled(260, then)
        else:
            pump(260)

    def test_c_new_conversation_in_the_current_scope(self) -> None:
        store = self.db()
        home = store.create_project("Evim")
        owned = store.create_conversation(home.id)
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.openConversation(owned.id)
        pump(200)
        # Sidebar v2 N1: no scope "+"; "Yeni sohbet" starts in the open conversation's project when writable.
        self.assertEqual([], self.all_items(root, "sidebarNewConversation"))
        self.click(root, "sidebarNewChat")
        self.assertNotEqual(owned.id, bridge.conversationId)
        self.assertEqual(home.id, store.load_conversation(bridge.conversationId).project_id)
        self.assertTrue(bridge.openProjectScope(""))
        pump(200)
        before = bridge.conversationId
        self.click(root, "sidebarNewChat")
        self.assertNotEqual(before, bridge.conversationId)
        self.assertIsNone(store.load_conversation(bridge.conversationId).project_id)
        # Archived owner: the scope's own new conversation is still refused, nothing is created there;
        # "Yeni sohbet" stays available and starts a no-project one (N1: else plain).
        bridge.openConversation(owned.id)
        pump(200)
        store.set_project_active(home.id, False)
        bridge.refreshProjects()
        pump(200)
        self.assertEqual([], self.all_items(root, "sidebarNewConversation"))
        button = self.item(root, "sidebarNewChat")
        self.assertTrue(button.property("available"))
        count = len(store.list_conversations(include_archived=True))
        self.assertFalse(bridge.newConversationInScope())
        self.assertEqual((owned.id, count), (bridge.conversationId, len(store.list_conversations(include_archived=True))))
        self.assertEqual("Evim · archived", self.owner(root))
        owned_count = len(store.list_conversations(project_id=home.id, include_archived=True))
        self.click(root, "sidebarNewChat")
        self.assertEqual(count + 1, len(store.list_conversations(include_archived=True)))
        self.assertIsNone(store.load_conversation(bridge.conversationId).project_id)
        self.assertEqual(owned_count, len(store.list_conversations(project_id=home.id, include_archived=True)))

class AuthorityTests(ProjectsUiCase):
    def test_d_the_selected_project_changes_no_approval_decision(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        bridge = self.bridge(task_proposal("code_analysis", objective="Bu projeyi incele."),
                             task_proposal("code_analysis", objective="Bu projeyi incele."))
        decisions = []
        for scope in ("", project.id):
            self.assertTrue(bridge.openProjectScope(scope))
            pump(100)
            bridge.submitText("Bu projeyi incele.")
            settled(100, lambda: (
                self.assertTrue(bridge.approvalPending),
            ))
            ticket = bridge._ticket
            request = store.load_pending_approval(ticket.task_id, ticket.session_id)
            decisions.append((request.scope, request.risk_level, request.side_effect, request.reason))
            bridge.deny()
            pump(100)
        self.assertEqual(decisions[0], decisions[1])
        self.assertEqual(0, self.execute_calls())


class CatalogTests(UiTestCase):
    def test_e_new_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Tüm projeler", tx("Sidebar", "All projects"))
        # Sidebar v2: "New conversation in %1" left with the scope "+" (its source comes back);
        # the project menu's item and the "Yeni sohbet" row replace it.
        self.assertEqual("New conversation in Evim", tx("Sidebar", "New conversation in %1").replace("%1", "Evim"))
        self.assertEqual("Bu projede yeni sohbet", tx("Sidebar", "New conversation in this project"))
        self.assertEqual("Yeni sohbet", tx("Sidebar", "New chat"))
        self.assertEqual("Kalıcı sil", tx("ProjectsPage", "Purge"))
