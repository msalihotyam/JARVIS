"""V2 Projects Gate 6F, slice F1: read-only Bridge additions for the sidebar.

B1. Every item of ``scopeConversations`` carries ``updated_at`` - the store's
    own timestamp, unchanged in format - so the sidebar can group Bugün / Dün /
    Daha eski.
B2. Every item of ``projectRecords`` carries ``conversation_count`` and
    ``archived_count``, cut from ONE ``list_conversations`` call per refresh
    (never a query per project) and recounted whenever the conversation lists
    are: create, archive, unarchive, attach and permanent delete.

Presentation only: extra keys on existing properties; the store is read, never
written, and no property, slot or signal is new. No test performs a live model,
auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import sqlite3
import unittest
from unittest import mock

from jarvis.conversation import append_turn
from jarvis.memory import SQLiteMemoryStore
from tests.test_projects_ui import ProjectsUiCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump


def say(memory, conversation_id: str, text: str) -> None:
    memory.update_conversation_state(conversation_id, lambda s: append_turn(
        s, role="user", kind="user", text=text, message=text, key=text)[0])


def dump(database) -> dict:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {t: connection.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}


class SidebarDataCase(ProjectsUiCase):
    def seed(self):
        """Global: 2 live + 1 archived. Empty: none. One: 1. Many: 3 live + 2 archived. Old: archived project with 1."""
        store = self.db()
        self.empty = store.create_project("Boş")
        self.one = store.create_project("Tek")
        self.many = store.create_project("Çok")
        self.old = store.create_project("Eski")
        store.create_conversation(self.one.id, title="Tek sohbet")
        self.live = [store.create_conversation(self.many.id, title=f"Çok {n}") for n in range(3)]
        for n in range(2):
            store.archive_conversation(store.create_conversation(self.many.id, title=f"Arşiv {n}").id)
        store.create_conversation(self.old.id, title="Eski sohbet")
        store.set_project_active(self.old.id, False)
        self.global_live = [store.create_conversation(title=f"Genel {n}") for n in range(2)]
        store.archive_conversation(store.create_conversation(title="Genel arşiv").id)
        return store

    def bridge(self, *answers, **kwargs):
        bridge = super().bridge(*answers, **kwargs)
        bridge.refreshProjects()                      # what the window does when it opens
        return bridge

    def counts(self, bridge) -> dict:
        return {r["name"]: (r["conversation_count"], r["archived_count"]) for r in bridge.projectRecords}


class UpdatedAtTests(SidebarDataCase):
    def test_a_every_scope_item_carries_the_stores_own_updated_at(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        expected = {r.id: r.updated_at for r in store.list_conversations(project_id=None)}
        shown = {r["id"]: r["updated_at"] for r in bridge.scopeConversations}
        self.assertEqual(expected, shown)
        self.assertTrue(shown and all(isinstance(v, str) and v for v in shown.values()))   # the store's string, as it is
        self.assertTrue(bridge.openProjectScope(self.many.id)); pump(100)
        expected = {r.id: r.updated_at for r in store.list_conversations(project_id=self.many.id)}
        shown = {r["id"]: r["updated_at"] for r in bridge.scopeConversations}
        self.assertEqual(expected, shown)
        self.assertEqual({r.id for r in self.live}, set(shown))                            # the archived two are not in it
        for row in bridge.scopeConversations:                                              # the existing keys keep their meaning
            self.assertEqual({"id", "title", "updated_at", "active"}, set(row))
        self.assertEqual(1, sum(r["active"] for r in bridge.scopeConversations))

    def test_b_the_timestamp_follows_a_new_message(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        before = {r["id"]: r["updated_at"] for r in bridge.scopeConversations}
        target = bridge.scopeConversations[-1]["id"]
        say(store, target, "yeni mesaj")
        bridge._refresh_conversations()
        after = {r["id"]: r["updated_at"] for r in bridge.scopeConversations}
        self.assertEqual(store.load_conversation(target).updated_at, after[target])
        self.assertNotEqual(before[target], after[target])


class CountTests(SidebarDataCase):
    def test_c_counts_for_none_one_many_and_archived(self) -> None:
        self.seed()
        bridge = self.bridge()
        self.assertEqual({"Boş": (0, 0), "Tek": (1, 0), "Çok": (3, 2), "Eski": (1, 0)}, self.counts(bridge))
        for record in bridge.projectRecords:                                                # the existing keys are still there
            self.assertLessEqual({"id", "name", "active", "created_at"}, set(record))
        self.assertEqual(2, len(bridge.conversations))                                      # Global: unchanged
        self.assertEqual(1, len(bridge.archivedConversations))

    def test_d_the_counts_follow_every_change_without_a_manual_refresh(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        seen = []
        bridge.projectsChanged.connect(lambda: seen.append("projects"))
        # create: a project's own new conversation, then Global's (no project moves)
        self.assertTrue(bridge.newProjectConversation(self.one.id)); pump(100)
        self.assertEqual((2, 0), self.counts(bridge)["Tek"])
        self.assertIn("projects", seen)
        self.assertTrue(bridge.newConversation()); pump(100)
        self.assertEqual({"Boş": (0, 0), "Tek": (2, 0), "Çok": (3, 2), "Eski": (1, 0)}, self.counts(bridge))
        # archive / unarchive a project conversation
        victim = self.live[0].id
        self.assertTrue(bridge.archiveConversation(victim)); pump(100)
        self.assertEqual((2, 3), self.counts(bridge)["Çok"])
        self.assertTrue(bridge.unarchiveConversation(victim)); pump(100)
        self.assertEqual((3, 2), self.counts(bridge)["Çok"])
        # permanent delete of an archived one
        gone = next(r.id for r in store.list_conversations(project_id=self.many.id, include_archived=True) if r.archived_at)
        bridge.previewDeleteConversation(gone); pump(60)
        self.assertTrue(bridge.confirmDeleteConversation(gone)); pump(100)
        self.assertEqual((3, 1), self.counts(bridge)["Çok"])

    def test_e_attach_moves_a_count_from_global_to_the_project(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        say(store, bridge.conversationId, "tezgah ölçüleri")
        bridge.previewAttach(self.empty.id, ""); pump(60)
        self.assertTrue(bridge.confirmAttach(self.empty.id, ""), bridge.attachNotice); pump(100)
        self.assertEqual((1, 0), self.counts(bridge)["Boş"])
        self.assertEqual(self.empty.id, bridge.conversationProject["id"])

    def test_f_one_list_call_per_refresh_not_one_per_project(self) -> None:
        self.seed()
        bridge = self.bridge()
        with mock.patch.object(SQLiteMemoryStore, "list_conversations", autospec=True,
                               side_effect=SQLiteMemoryStore.list_conversations) as spy:
            bridge._refresh_conversations()
            self.assertEqual(1, spy.call_count)                        # four projects, one read
            self.assertEqual({"include_archived": True}, spy.call_args.kwargs)
            spy.reset_mock()
            bridge.openProjectScope(self.many.id); pump(100)           # opening a scope refreshes once, too
            self.assertEqual(1, sum(1 for c in spy.call_args_list if c.kwargs == {"include_archived": True}))
            spy.reset_mock()
            bridge.projectRecords; bridge.scopeConversations           # reading the properties reads no store
            self.assertEqual(0, spy.call_count)

    def test_g_reading_is_read_only(self) -> None:
        self.seed()
        bridge = self.bridge()
        before = dump(self.database)
        for _ in range(3):
            bridge.projectRecords, bridge.scopeConversations, bridge.conversations, bridge.archivedConversations
        bridge._refresh_conversations()
        self.assertEqual(before, dump(self.database))
        from jarvis.memory import SCHEMA_VERSION
        self.assertEqual(10, SCHEMA_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
