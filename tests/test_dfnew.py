"""DFNEW dogfood fix (Salih 2026-10-12, D-2 item 4): "Yeni sohbet" is a screen.

1. "Yeni sohbet" (the sidebar row, the project page's entry) shows the New
   chat screen: no row anywhere and, once the owner has its blank
   conversation, nothing new stored however often it is pressed.
2. Deleting the open conversation lands on that screen and stores nothing new.
3. The first message makes exactly one row, owned by the picker's project.
4. A restart (a fresh bridge on the same database) shows no empty row; old
   empty conversations are hidden, never deleted.
5. The store's read-side filter: untitled, made by JARVIS, never a turn.

The test's own temporary database only; no live model, auth or network call.
"""

from __future__ import annotations

import json

from jarvis.conversation import append_turn
from tests.helpers import settled
from tests.test_projects_ui import ProjectsUiCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

CHAT = json.dumps({"kind": "chat", "reply": "Tamam."})


def say(store, conversation_id: str, text: str) -> None:
    store.update_conversation_state(conversation_id, lambda s: append_turn(
        s, role="user", kind="user", text=text, message=text, key=text)[0])


class NewChatCase(ProjectsUiCase):
    def listed(self, bridge) -> list[str]:
        """Every conversation id any list of the window shows."""
        side = [row["id"] for rows in bridge.sidebarConversations.values() for row in rows]
        return [*(r["id"] for r in (*bridge.conversations, *bridge.archivedConversations, *bridge.scopeConversations,
                                    *bridge.projectConversations, *bridge.projectArchivedConversations,
                                    *bridge.pinnedConversations)), *side]

    def stored(self, store, **owner) -> list[str]:
        """Every stored, not deleted conversation (``project_id=`` narrows to one owner)."""
        return [r.id for r in store.list_conversations(include_archived=True, **owner)]

    def rows(self, root) -> list:
        return [i.property("title") for name in ("sidebarConversation", "sidebarProjectConversation")
                for i in self.all_items(root, name) if i.isVisible()]


class NewChatScreenTests(NewChatCase):
    def test_a_new_chat_adds_no_row_and_presses_never_pile_up(self) -> None:
        store = self.db()
        evim = store.create_project("Evim")
        bridge = self.bridge()
        root = self.window(bridge)
        start = bridge.conversationId                                  # no conversation yet: the New chat screen
        self.assertEqual(([start], []), (self.stored(store), self.listed(bridge)))
        for _ in range(4):
            self.click(root, "sidebarNewChat")
            self.assertEqual(([start], start, "home"), (self.stored(store), bridge.conversationId, bridge.page))
        self.assertEqual(([], []), (self.listed(bridge), self.rows(root)))
        # The project page's entry: that project's New chat screen - one blank one, then reused.
        project_chats = set()
        for _ in range(3):
            bridge.showProject(evim.id)
            pump(100)
            self.assertTrue(bridge.startNewChat())
            project_chats.add(bridge.conversationId)
            self.assertEqual((evim.id, "home"), (store.load_conversation(bridge.conversationId).project_id, bridge.page))
        self.assertEqual(1, len(project_chats))
        self.assertEqual(sorted([start, *project_chats]), sorted(self.stored(store)))
        bridge.showProject(evim.id)
        pump(200)
        self.assertEqual([], self.listed(bridge))
        self.assertEqual({"Evim": (0, 0)}, {r["name"]: (r["conversation_count"], r["archived_count"])
                                           for r in bridge.projectRecords})
        # Switching away and back changes nothing either.
        say(store, start, "eski")                                      # now a real conversation of its own
        bridge._refresh_conversations()
        bridge.setPage("home")                                         # off the project page (its entry is evim's)
        chats = set()
        for _ in range(3):
            self.assertTrue(bridge.openConversation(start))
            self.assertTrue(bridge.startNewChat())
            chats.add(bridge.conversationId)
        self.assertEqual(1, len(chats))                                # one blank Global one, reused
        self.assertEqual({start}, set(self.listed(bridge)))
        self.assertEqual(2, len(self.stored(store, project_id=None)))
        self.assertEqual(0, self.execute_calls())

    def test_b_the_first_message_makes_exactly_one_row_of_the_picked_project(self) -> None:
        store = self.db()
        evim = store.create_project("Evim")
        bridge = self.bridge(CHAT)
        root = self.window(bridge)
        self.assertTrue(bridge.startNewChat())
        self.assertTrue(bridge.setNewChatProject(evim.id))
        chat = bridge.conversationId
        self.assertEqual([], self.listed(bridge))
        self.assertTrue(bridge.submitText("Mutfak için bir plan yaz"))
        settled(600, lambda: self.assertEqual(1, len(self.model.calls)))
        bridge.waitIdle()
        pump(100)
        self.assertEqual(chat, bridge.conversationId)
        record = store.load_conversation(chat)
        self.assertEqual(evim.id, record.project_id)
        self.assertTrue(record.title)
        self.assertEqual([chat], [row["id"] for row in bridge.sidebarConversations.get(evim.id, [])])
        self.assertEqual([], bridge.sidebarConversations.get("", []))
        self.assertEqual({chat}, set(self.listed(bridge)))
        self.assertEqual([chat], self.stored(store, project_id=evim.id))     # exactly one, the picker's owner
        settled(300, lambda: self.assertIn(record.title, self.rows(root)))

    def test_b2_a_first_message_that_gives_no_title_still_makes_its_row(self) -> None:
        # Review MAJOR: "Merhaba" names nothing (task_title is ""); the row appears all the same.
        store = self.db()
        bridge = self.bridge(CHAT)
        chat = bridge.conversationId
        self.assertEqual([], self.listed(bridge))
        self.assertTrue(bridge.submitText("Merhaba"))
        settled(600, lambda: self.assertEqual(1, len(self.model.calls)))
        bridge.waitIdle()
        self.assertEqual("", store.load_conversation(chat).title)
        self.assertEqual([chat], [row["id"] for row in bridge.sidebarConversations.get("", [])])
        self.assertIn(chat, [row["id"] for row in bridge.conversations])

    def test_b3_a_message_on_screen_is_never_wiped_by_a_new_chat(self) -> None:
        # Review NIT: the first turn failed to save, so the open one is still blank in the store:
        # "Yeni sohbet" never reopens it (that would clear the sent message off the screen).
        bridge = self.bridge()
        chat = bridge.conversationId
        bridge._feed.append({"kind": "user", "msg": "kaydedilemedi", "ts": 0.0, "id": "m-x"})
        self.assertTrue(bridge.startNewChat())
        self.assertNotEqual(chat, bridge.conversationId)


class DeleteOpenTests(NewChatCase):
    def test_c_deleting_the_open_one_lands_on_the_new_chat_screen_and_stores_nothing(self) -> None:
        store = self.db()
        bridge = self.bridge()
        root = self.window(bridge)
        blank = bridge.conversationId                                 # Global's blank one (startup)
        for n in range(3):
            doomed = store.create_conversation(title=f"Silinecek {n}")
            say(store, doomed.id, "bir")
            bridge._refresh_conversations()
            self.assertTrue(bridge.openConversation(doomed.id))
            before = set(self.stored(store))
            bridge.previewDeleteConversation(doomed.id)
            pump(60)
            self.assertTrue(bridge.confirmDeleteConversation(doomed.id))
            settled(300, lambda: self.assertEqual(blank, bridge.conversationId))
            self.assertEqual(before - {doomed.id}, set(self.stored(store)))       # nothing new was stored
            self.assertEqual(("home", [], []), (bridge.page, bridge.feed, self.listed(bridge)))
        settled(300, lambda: self.assertTrue(self.item(root, "conversationWelcome").isVisible()))
        self.assertEqual([], self.rows(root))


class RestartTests(NewChatCase):
    def test_d_a_restart_shows_no_empty_row_and_old_empty_ones_stay_stored(self) -> None:
        store = self.db()
        evim = store.create_project("Evim")
        old = [store.create_conversation().id for _ in range(3)] + [store.create_conversation(evim.id).id]
        kept = store.create_conversation(title="Dolu")
        say(store, kept.id, "bir")
        for _ in range(3):                                             # three restarts, a New chat in each owner
            bridge = self.bridge()
            self.assertTrue(bridge.startNewChat())
            self.assertTrue(bridge.newProjectConversation(evim.id))
            self.assertEqual({kept.id}, set(self.listed(bridge)))
            self.assertEqual(sorted([*old, kept.id]), sorted(self.stored(store)))   # nothing added, nothing deleted
        root = self.window(self.bridge())
        self.assertEqual(["Dolu"], self.rows(root))


class BlankFilterTests(NewChatCase):
    def test_e_blank_is_untitled_jarvis_made_and_never_written(self) -> None:
        store = self.db()
        evim = store.create_project("Evim")
        blank = store.create_conversation()
        owned_blank = store.create_conversation(evim.id)
        titled = store.create_conversation(title="Adlı")
        spoken = store.create_conversation()
        say(store, spoken.id, "selam")
        gone = store.create_conversation(title="Gidecek")
        say(store, gone.id, "x")
        store.archive_conversation(gone.id)
        preview = store.conversation_delete_preview(gone.id)
        store.delete_conversation(gone.id, confirm=gone.id, expected_next_seq=preview["expected_next_seq"],
                                  expected_title=preview["title"])
        archived = store.create_conversation()          # review MINOR: an archived empty one stays in History
        store.archive_conversation(archived.id)
        self.assertEqual({blank.id, owned_blank.id}, set(store.blank_conversation_ids()))
        # Read only: every row is still there, unchanged.
        self.assertEqual({blank.id, owned_blank.id, titled.id, spoken.id, archived.id},
                         {r.id for r in store.list_conversations(include_archived=True)})
        bridge = self.bridge()
        self.assertIn(archived.id, [r["id"] for r in bridge.archivedConversations])
