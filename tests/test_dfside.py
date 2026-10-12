"""DFSIDE dogfood fixes (Salih 2026-10-12 00:57, D-2 items 2 and 4; addendum 03:08).

A  One typed character searches: a one-character title is found at once (titles
   only - contents still from two characters, no conversation state is read);
   an empty query shows the normal list; a few hundred conversations answer
   within the debounce plus a small budget.
B  Every project row of the left column carries the drawn project icon; the
   conversation rows do not.
C  While the New chat screen is open (DFNEW's blank conversation) the sidebar's
   New chat row is the current one and no conversation row is; opening a
   conversation moves it there; deleting the open chat brings it back.

Temp stores only: no test opens the live DB or makes a live model, auth or
network call. With JARVIS_SHOTS_DIR set, the sidebar stills are saved there.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest

from jarvis import conversation_search as cs
from jarvis.memory import SQLiteMemoryStore
from tests.helpers import settled, wait_until
from tests.test_dfnew import say
from tests.test_g6sb1_sidebar import SidebarV2Case
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

ROWS = ("sidebarConversation", "sidebarProjectConversation", "sidebarPinnedConversation")


class OneCharacterStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.store = SQLiteMemoryStore(Path(folder.name) / "jarvis.db")

    def test_one_character_finds_the_one_character_title_and_reads_no_content(self) -> None:
        store = self.store
        k = store.create_conversation(title="K")
        other = store.create_conversation(title="Bahçe")
        say(store, other.id, "kapı kilidi")                                  # a content hit for "k" - not searched
        with mock.patch.object(store, "load_conversation_state", wraps=store.load_conversation_state) as read:
            found = cs.search(store, "k")
        self.assertEqual(([k.id], [], False), ([r["id"] for r in found["titles"]], found["contents"], found["more"]))
        self.assertEqual(0, read.call_count)
        # Two characters: contents as before.
        self.assertEqual([other.id], [r["id"] for r in cs.search(store, "ka")["contents"]])

    def test_a_few_hundred_conversations_answer_one_character_quickly(self) -> None:
        store = self.store
        for n in range(300):
            store.create_conversation(title=f"Not {n}")
        started = time.perf_counter()
        found = cs.search(store, "n")
        took = time.perf_counter() - started
        self.assertEqual((cs.RESULT_CAP, True, []), (len(found["titles"]), found["more"], found["contents"]))
        self.assertLess(took, 1.5, took)


class DfSideCase(SidebarV2Case):
    def current_rows(self, root) -> list:
        return [i.property("title") for n in ROWS for i in self.shown(root, n) if i.property("current")]

    def new_chat(self, root):
        return self.item(root, "sidebarNewChat")


class SidebarSearchTests(DfSideCase):
    def test_a_one_typed_character_shows_the_one_character_title(self) -> None:
        store = self.data()
        store.create_conversation(title="K")
        for n in range(300):                                                 # a long history
            store.create_conversation(title=f"Sohbet {n}")
        bridge, root = self.open_window()
        self.tap(root, self.item(root, "sidebarSearchEntry"), until=lambda: bool(self.shown(root, "sidebarSearchField")))
        # Open, nothing typed: the normal list.
        self.assertFalse(self.shown(root, "sidebarSearchResults"))
        self.assertTrue(self.shown(root, "sidebarConversation") and self.shown(root, "sidebarNewChat"))
        QTest.keyClick(root, "k")
        # Within the debounce (250 ms) plus a small budget.
        self.assertTrue(wait_until(lambda: bool(self.shown(root, "sidebarSearchTitleRow")), 250 + 1500))
        self.assertIn("K", [r.property("name") for r in self.shown(root, "sidebarSearchTitleRow")])
        self.assertEqual([], self.shown(root, "sidebarSearchContentRow"))
        self.assertFalse(self.shown(root, "sidebarConversation"))
        # Back to empty: the normal list again.
        self.item(root, "sidebarSearchField").setProperty("text", "")
        settled(300, lambda: (self.assertFalse(self.shown(root, "sidebarSearchResults")),
                              self.assertTrue(self.shown(root, "sidebarConversation"))))
        self.assertEqual(0, self.execute_calls())


class ProjectIconTests(DfSideCase):
    def test_b_every_project_row_has_the_project_icon_and_conversation_rows_do_not(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.pinConversation(self.tezc[0].id))             # a folder-led pin: the colour reference
        settled(300, lambda: self.assertEqual(["Tez 0"], self.pinned(root)))
        folder = self.child(self.pin_row(root, "Tez 0"), "sidebarRowLead")
        self.assertEqual("folder", folder.property("name"))
        projects = self.shown(root, "sidebarProject")
        self.assertEqual(["Kod", "Tez"], [r.property("title") for r in projects])
        for row in projects:
            leads = [c for c in row.childItems() if c.objectName() == "sidebarRowLead" and c.isVisible()]
            self.assertEqual(["project"], [c.property("name") for c in leads], row.property("title"))
            # The other row icons' size and colour rule (a project's folder-like accent tint).
            self.assertEqual((folder.property("size"), folder.property("tint")),
                             (leads[0].property("size"), leads[0].property("tint")))
            # In the dot column, left of the name, which stays flush with the tree's conversations.
            self.assertLess(leads[0].x() + leads[0].width(), self.child(row, "sidebarProjectName").x())
        for name in ("sidebarProjectConversation", "sidebarConversation"):
            rows = self.shown(root, name)
            self.assertTrue(rows, name)
            for row in rows:
                self.assertEqual([], [c for c in row.childItems() if c.objectName() == "sidebarRowLead" and c.isVisible()],
                                 row.property("title"))


class NewChatSelectedTests(DfSideCase):
    def test_c_the_new_chat_row_is_current_on_the_new_chat_screen_only(self) -> None:
        store = self.data()
        for c in (self.plain[0], self.tezc[0]):
            say(store, c.id, "bir")
        blank = store.create_conversation()                                  # left on the New chat screen last time
        bridge, root = self.open_window()
        self.assertTrue(bridge.pinConversation(self.plain[1].id))
        # App start: the New chat screen (the newest conversation is the blank one).
        self.assertEqual(blank.id, bridge.conversationId)
        settled(300, lambda: (self.assertTrue(bridge.newChatOpen),
                              self.assertTrue(self.new_chat(root).property("current")),
                              self.assertEqual([], self.current_rows(root))))
        self.assertTrue(self.shown(root, "sidebarPinnedConversation"))
        # Opening a conversation: its row is current, the button is not.
        self.tap(root, self.row(root, "sidebarConversation", "Düz 0"), then=lambda: (
            self.assertFalse(self.new_chat(root).property("current")),
            self.assertEqual(["Düz 0"], self.current_rows(root))))
        self.assertFalse(bridge.newChatOpen)
        # A pinned conversation, then the New chat click: the pin is not current any more.
        self.tap(root, self.pin_row(root, "Düz 1"), then=lambda: self.assertEqual(["Düz 1"], self.current_rows(root)))
        self.tap(root, self.new_chat(root), then=lambda: (
            self.assertTrue(self.new_chat(root).property("current")),
            self.assertEqual([], self.current_rows(root))))
        # A project conversation, then the New chat click.
        self.tap(root, self.row(root, "sidebarProjectConversation", "Tez 0"), then=lambda: (
            self.assertEqual(["Tez 0"], self.current_rows(root))))
        self.tap(root, self.new_chat(root), then=lambda: (
            self.assertTrue(self.new_chat(root).property("current")),
            self.assertEqual([], self.current_rows(root))))
        # The project's own New chat.
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        self.assertTrue(bridge.newProjectConversation(self.kod.id))
        settled(300, lambda: (self.assertTrue(self.new_chat(root).property("current")),
                              self.assertEqual([], self.current_rows(root))))
        self.assertEqual(0, self.execute_calls())

    def test_c2_deleting_the_open_chat_selects_the_new_chat_row_again(self) -> None:
        store = self.data()
        say(store, self.plain[0].id, "bir")
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        settled(300, lambda: (self.assertEqual(["Düz 0"], self.current_rows(root)),
                              self.assertFalse(self.new_chat(root).property("current"))))
        bridge.previewDeleteConversation(self.plain[0].id)
        pump(60)
        self.assertTrue(bridge.confirmDeleteConversation(self.plain[0].id))
        settled(300, lambda: (self.assertTrue(bridge.newChatOpen),
                              self.assertTrue(self.new_chat(root).property("current")),
                              self.assertEqual([], self.current_rows(root))))


class StillTests(DfSideCase):
    def test_s_sidebar_stills_in_space_and_dark(self) -> None:
        folder = os.environ.get("JARVIS_SHOTS_DIR")
        if not folder:
            self.skipTest("JARVIS_SHOTS_DIR not set")
        store = self.data()
        say(store, self.tezc[1].id, "bir")
        bridge, root = self.open_window()
        Path(folder).mkdir(parents=True, exist_ok=True)
        side = self.rect(self.item(root, "sidebar")).toRect()
        for theme in ("space", "dark"):
            bridge.setTheme(theme)
            self.assertTrue(bridge.startNewChat())
            pump(300)
            root.grabWindow().copy(side).save(str(Path(folder) / f"sidebar_newchat_{theme}.png"))
            self.assertTrue(bridge.openConversation(self.tezc[1].id))
            pump(300)
            root.grabWindow().copy(side).save(str(Path(folder) / f"sidebar_open_{theme}.png"))


if __name__ == "__main__":
    unittest.main()
