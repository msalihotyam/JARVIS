"""6G N2 (Salih 2026-10-05): the conversation open on screen archived or deleted
- from its own menu, a sidebar row's menu or anywhere else, incl. a change in
the store read on the window's next refresh - switches the view to the New chat
screen: a new, empty conversation of its scope (Global when that scope is not
writable) on the chat page. Archiving or deleting ANOTHER conversation does not
switch; opening an archived conversation read-only and unarchiving it behave as
before. The confirm dialog and its K2 input guard are unchanged.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import unittest

from PySide6.QtCore import Qt

from tests.helpers import settled, wait_until
from tests.test_projects_ui_6f import F6Case
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump


class OpenConversationRemovedTests(F6Case):
    def scoped(self):
        store = self.db()
        self.tez = store.create_project("Tez")
        self.genel = [store.create_conversation(title=f"Genel {n}") for n in range(3)]
        self.tezc = [store.create_conversation(self.tez.id, title=f"Tez {n}") for n in range(2)]
        return store

    def new_chat(self, root, bridge, store, before, project_id=None) -> str:
        """The New chat screen: a new, empty, writable conversation of ``project_id`` on the chat page."""
        now = bridge.conversationId
        self.assertNotIn(now, before)
        record = store.load_conversation(now)
        self.assertEqual((project_id, None, ""), (record.project_id, record.archived_at, record.title))
        self.assertEqual(0, len(store.load_conversation_state(now).turns))
        self.assertEqual(([], "home", False), (bridge.feed, bridge.page, bridge.conversationReadOnly))
        settled(400, lambda: (
            self.assertEqual("home", root.property("loadedPage")),
            self.assertTrue(self.item(root, "conversationWelcome").isVisible()),
            # Sidebar v2 V7: the scope (project) chip names a project chat's project; a no-project chat has none.
            self.assertEqual(project_id is not None, self.item(root, "chatHeaderScope").isVisible()),
        ))
        if project_id is not None:
            self.assertEqual(bridge.conversationProject["name"], self.item(root, "chatHeaderScope").property("fullName"))
        return now

    def confirm_delete(self, root) -> None:
        """The dialog and its K2 guard are unchanged: "Kalıcı sil" is pressed once armed."""
        confirm = [i for i in self.all_items(root, "deleteConfirm") if i.isVisible()][0]
        wait_until(confirm.isEnabled, 1000)
        self.tap(root, confirm)

    def test_n2_own_menu_archive_and_delete(self) -> None:
        store = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        open_id = bridge.conversationId
        self.assertEqual(self.genel[2].id, open_id)
        self.choose(root, self.item(root, "chatHeaderMenu"), "conversationArchiveItem", Qt.LeftButton, label="Archive")
        self.assertIsNotNone(store.load_conversation(open_id).archived_at)
        first = self.new_chat(root, bridge, store, [c.id for c in self.genel])
        # Delete (Global: one step) from the header "…" of the new chat.
        self.choose(root, self.item(root, "chatHeaderMenu"), "conversationDeleteItem", Qt.LeftButton)
        self.confirm_delete(root)
        self.assertIsNone(store.load_conversation(first))
        self.new_chat(root, bridge, store, [first, *(c.id for c in self.genel)])
        self.assertEqual(0, self.execute_calls())

    def test_n2_sidebar_archive_and_delete_of_the_open_one(self) -> None:
        store = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        # A project conversation, archived from its own sidebar row: a new chat in the same project.
        # Sidebar v2 T1: its row is in the project's tree (picked there), not in the plain list.
        self.tap(root, self.row(root, "sidebarProjectConversation", "Tez 0"))
        bridge.openConversation(self.tezc[0].id)
        wait_until(lambda: bridge.conversationId == self.tezc[0].id, 400)
        self.assertNotIn("Tez 0", self.titles(root))
        self.choose(root, self.row(root, "sidebarProjectConversation", "Tez 0"), "conversationArchiveItem", label="Archive")
        self.assertIsNotNone(store.load_conversation(self.tezc[0].id).archived_at)
        self.new_chat(root, bridge, store, [c.id for c in self.tezc], self.tez.id)
        # A Global conversation, deleted from its own sidebar row: a new Global chat.
        # Sidebar v2 V1: no "Genel" row; the plain list is always shown and its row is picked there.
        self.assertEqual([], self.all_items(root, "sidebarGlobal"))
        self.tap(root, self.row(root, "sidebarConversation", "Genel 1"))
        bridge.openConversation(self.genel[1].id)
        wait_until(lambda: bridge.conversationId == self.genel[1].id, 400)
        self.choose(root, self.row(root, "sidebarConversation", "Genel 1"), "conversationDeleteItem")
        self.confirm_delete(root)
        self.assertIsNone(store.load_conversation(self.genel[1].id))
        self.new_chat(root, bridge, store, [c.id for c in self.genel])

    def test_n2_another_conversation_never_switches(self) -> None:
        store = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        open_id = bridge.conversationId
        self.choose(root, self.row(root, "sidebarConversation", "Genel 0"), "conversationArchiveItem", label="Archive")
        self.assertIsNotNone(store.load_conversation(self.genel[0].id).archived_at)
        self.choose(root, self.row(root, "sidebarConversation", "Genel 1"), "conversationDeleteItem")
        self.confirm_delete(root)
        self.assertIsNone(store.load_conversation(self.genel[1].id))
        # ...and in the store, read on the next refresh.
        store.archive_conversation(self.tezc[0].id)
        bridge.refreshProjects()
        self.assertEqual((open_id, "home"), (bridge.conversationId, bridge.page))
        self.assertEqual(4, len(store.list_conversations(include_archived=True)))     # nothing new was created

    def test_n2_a_removal_in_the_store_is_left_on_the_next_refresh(self) -> None:
        store = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("history")
        # Archived elsewhere: the next read of the store (here the Projects page's Refresh) leaves it.
        open_id = bridge.conversationId
        store.archive_conversation(open_id)
        bridge.refreshProjects()
        first = self.new_chat(root, bridge, store, [c.id for c in self.genel])
        # Deleted elsewhere, while a project conversation is open: a new chat of that project.
        bridge.openConversation(self.tezc[1].id)
        store.archive_conversation(self.tezc[1].id)
        preview = store.conversation_delete_preview(self.tezc[1].id)
        store.delete_conversation(self.tezc[1].id, confirm=self.tezc[1].id,
                                  expected_next_seq=preview["expected_next_seq"], expected_title=preview["title"])
        bridge.refreshProjects()
        self.new_chat(root, bridge, store, [first, *(c.id for c in self.tezc)], self.tez.id)
        # An archived conversation opened read-only stays; deleted elsewhere, it is left too.
        self.assertTrue(bridge.openConversation(open_id))
        bridge.refreshProjects()
        self.assertEqual((open_id, True), (bridge.conversationId, bridge.conversationReadOnly))
        preview = store.conversation_delete_preview(open_id)
        store.delete_conversation(open_id, confirm=open_id, expected_next_seq=preview["expected_next_seq"],
                                  expected_title=preview["title"])
        bridge.refreshProjects()
        # DFNEW: Global's blank one (``first``) is reused, never a second one.
        self.assertEqual(first, self.new_chat(root, bridge, store, [open_id, *(c.id for c in self.genel)]))
        self.assertEqual(0, self.execute_calls())

    def test_n2_a_page_switch_or_history_refresh_rechecks_and_the_page_asked_for_wins(self) -> None:
        store = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        open_id = bridge.conversationId
        store.archive_conversation(open_id)
        bridge.setPage("history")
        self.assertNotIn(bridge.conversationId, [c.id for c in self.genel])
        self.assertEqual(("history", False), (bridge.page, bridge.conversationReadOnly))
        # The History refresh reads it too; Sohbet (home) is then the New chat screen.
        second = bridge.conversationId
        store.archive_conversation(second)
        bridge.refreshHistory()
        third = bridge.conversationId
        self.assertNotIn(third, (open_id, second))
        bridge.setPage("home")
        self.new_chat(root, bridge, store, [open_id, second, *(c.id for c in self.genel)])

    def test_n2_while_a_run_is_busy_it_is_left_when_the_run_ends(self) -> None:
        store = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        open_id = bridge.conversationId
        bridge._busy = True
        store.archive_conversation(open_id)
        bridge.refreshProjects()
        bridge.setPage("home")
        self.assertEqual(open_id, bridge.conversationId)                             # never mid-run
        bridge._run("task", lambda: {}, lambda result: None)                         # the run ends (sync)
        self.assertFalse(bridge.busy)
        self.new_chat(root, bridge, store, [c.id for c in self.genel])

    def test_n2_an_owner_archived_meanwhile_or_no_store_falls_back(self) -> None:
        from unittest import mock
        from jarvis.memory import SQLiteMemoryStore
        store = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertTrue(bridge.openConversation(self.tezc[0].id))
        # The project archived in the store, unseen by the window; then the conversation deleted.
        store.set_project_active(self.tez.id, False)
        preview = store.conversation_delete_preview(self.tezc[0].id)
        store.delete_conversation(self.tezc[0].id, confirm=self.tezc[0].id,
                                  expected_next_seq=preview["expected_next_seq"], expected_title=preview["title"])
        bridge.setPage("home")
        first = self.new_chat(root, bridge, store, [c.id for c in (*self.genel, *self.tezc)])   # Global
        # Nothing can be created: an unsaved view on the chat page, and the error said.
        store.archive_conversation(first)
        with mock.patch.object(SQLiteMemoryStore, "create_conversation", side_effect=OSError("disk")):
            bridge.setPage("home")
        self.assertEqual(("", "home"), (bridge.conversationId, bridge.page))
        self.assertIn("could not be created: OSError", bridge.activity[0]["text"])

    def test_n2_a_page_s_own_delete_dialog_on_the_open_one(self) -> None:
        """History's (and the Projects page's) dialog lives on the page the switch unloads:
        the dialog closes cleanly first, with no QML error."""
        from PySide6.QtCore import qInstallMessageHandler
        store = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        archived = self.genel[0].id
        store.archive_conversation(archived)
        bridge.refreshProjects()
        self.assertTrue(bridge.openConversation(archived))                           # read-only, from History
        bridge.setPage("history")
        wait_until(lambda: root.property("loadedPage") == "history", 1000)
        pump(400)
        messages = []
        previous = qInstallMessageHandler(lambda mode, context, text: messages.append(text))
        self.addCleanup(qInstallMessageHandler, previous)
        self.tap(root, [i for i in self.all_items(root, "historyDelete") if i.isVisible()][0])
        self.confirm_delete(root)
        self.assertIsNone(store.load_conversation(archived))
        self.new_chat(root, bridge, store, [c.id for c in self.genel])
        self.assertEqual([], [m for m in messages if "TypeError" in m or "ReferenceError" in m or "of null" in m
                              or "deleted" in m], messages)
        self.assertFalse([i for i in self.all_items(root, "deleteConfirm") if i.isVisible()])

    def test_n2_unarchive_keeps_the_open_conversation(self) -> None:
        store = self.scoped()
        bridge = self.bridge()
        self.window(bridge)
        archived = self.genel[0].id
        store.archive_conversation(archived)
        bridge.refreshProjects()
        self.assertTrue(bridge.openConversation(archived))
        self.assertTrue(bridge.conversationArchived)
        self.assertTrue(bridge.unarchiveConversation(archived))
        self.assertEqual((archived, False), (bridge.conversationId, bridge.conversationReadOnly))
        # Unarchived and open, it is live again: a later archive leaves it.
        store.archive_conversation(archived)
        bridge.refreshProjects()
        self.assertNotEqual(archived, bridge.conversationId)


if __name__ == "__main__":
    unittest.main()
