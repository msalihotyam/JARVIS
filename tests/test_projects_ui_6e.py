"""V2 Projects Gate 6E, slice C1: copy, plurals and catalogs (Salih 2026-10-03).

A. English counts: 0 / 1 / n for messages (delete dialog), turns (attach
   dialog), tasks (History sessions) and the Bridge's activity notes; the
   Turkish catalog has one form ("1 mesaj", "1 görev").
B. One untitled name: "New Chat" | "Yeni Sohbet" in every list, menu and
   dialog; the actions stay sentence case ("Yeni sohbet").
C. One word: no "konuşma" in any Turkish string of the 6A-6D surfaces, the
   Composer, the command palette or the Bridge; the headings read SOHBETLER,
   ARŞİVLENMİŞ SOHBETLER and ARŞİVLENMİŞ PROJELER.

Strings only: no behaviour, signal or layout is under test here.
No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import re
import unittest

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from jarvis.conversation import append_turn
from jarvis.ui.i18n import CATALOG_DIR, load_catalog, localization, render, tx
from tests.test_projects_ui import ProjectsUiCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import _APP, UiTestCase, pump

SURFACES = ("TopBar", "HistoryPage", "ProjectsPage", "AttachDialog", "ConversationDeleteDialog", "Composer",
            "CommandPalette", "Bridge")


def say(memory, conversation_id: str, text: str) -> None:
    memory.update_conversation_state(conversation_id, lambda s: append_turn(
        s, role="user", kind="user", text=text, message=text, key=text)[0])


class WindowCase(ProjectsUiCase):
    def press(self, root, item) -> None:
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        pump(260)

    def visible(self, root, name) -> list:
        return [i for i in self.all_items(root, name) if i.isVisible()]

    def tr(self, bridge) -> None:
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        bridge.setLanguage("tr-TR"); pump(300)


class CountTests(WindowCase):
    def test_a_delete_dialog_counts_messages_in_english(self) -> None:
        store = self.db()
        ids = {}
        for turns in (0, 1, 2):
            conversation = store.create_conversation()
            for n in range(turns):
                say(store, conversation.id, f"m{turns}{n}")
            store.archive_conversation(conversation.id)
            ids[turns] = conversation.id
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("history"); pump(400)
        order = {r["id"]: i for i, r in enumerate(bridge.archivedConversations)}
        for turns, expected in ((0, "0 messages"), (1, "1 message"), (2, "2 messages")):
            self.press(root, self.visible(root, "historyDelete")[order[ids[turns]]])
            identity = self.visible(root, "deleteIdentity")[0].property("text")
            self.assertTrue(identity.startswith(f"«New Chat» · {expected} · created "), identity)
            self.press(root, self.visible(root, "deleteCancel")[0])

    def test_b_attach_dialog_counts_turns_in_english_and_messages_in_turkish(self) -> None:
        store = self.db()
        store.create_project("Evim")
        bridge = self.bridge()
        root = self.window(bridge)
        conversation_id = bridge.conversationId
        for turns, expected in ((0, "0 turns"), (1, "1 turn"), (2, "2 turns")):
            if turns:
                say(store, conversation_id, f"t{turns}")
            self.attach_identity_is(root, f'"New Chat" · {expected} · created ')
        self.tr(bridge)
        self.attach_identity_is(root, '"Yeni Sohbet" · 2 mesaj · oluşturuldu ')
        say(store, conversation_id, "t3")                    # 3 messages: the same single Turkish form
        self.attach_identity_is(root, '"Yeni Sohbet" · 3 mesaj · oluşturuldu ')

    def attach_identity_is(self, root, prefix: str) -> None:
        self.click(root, "projectSelector"); pump(200)
        self.click(root, "projectSelectorAttach"); pump(300)
        item = [i for i in self.all_items(root, "attachProject") if i.property("label") == "Evim"][0]
        self.press(root, item)
        identity = self.item(root, "attachIdentity").property("text")
        self.assertTrue(identity.startswith(prefix), identity)
        self.press(root, self.visible(root, "attachCancel")[0])

    def test_c_history_sessions_count_tasks(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._sessions = [{"session_id": f"s{n}", "title": f"Oturum {n}", "updated_at": 1_700_000_000.0 + n,
                             "task_count": n, "active_task_id": "", "readable": True} for n in (0, 1, 2)]
        bridge.dataChanged.emit()
        bridge.setPage("history"); pump(400)
        metas = lambda: sorted(t.split("  ·  ")[1] for t in self.texts(root, "historySessionMeta"))  # noqa: E731
        self.assertEqual(["0 tasks", "1 task", "2 tasks"], metas())
        self.tr(bridge)
        self.assertEqual(["0 görev", "1 görev", "2 görev"], metas())

    def test_d_the_activity_notes_count_turns(self) -> None:
        store = self.db()
        bridge = self.bridge()
        notes = lambda: [n["text"] for n in bridge.activity]  # noqa: E731
        for turns, expected in ((0, "(0 turns)"), (1, "(1 turn)"), (2, "(2 turns)")):
            other = store.create_conversation()
            for n in range(turns):
                say(store, other.id, f"a{turns}{n}")
            self.assertTrue(bridge.openConversation(other.id))
            self.assertIn(f"Opened a conversation {expected}", notes())
        self.assertFalse([n for n in notes() if "(1 turns)" in n or "(0 turn)" in n])
        # The Turkish catalog has one form, and the singular source is its own entry.
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        localization(_APP).apply("tr-TR")
        self.assertEqual(["Bir sohbet açıldı (0 mesaj)", "Bir sohbet açıldı (1 mesaj)", "Bir sohbet açıldı (2 mesaj)"],
                         [render(("Bridge", "Opened a conversation (1 turn)" if n == 1 else "Opened a conversation (%1 turns)", (n,)))
                          for n in (0, 1, 2)])
        self.assertEqual(["Önceki sohbet sürdürülüyor (1 mesaj)", "Önceki sohbet sürdürülüyor (4 mesaj)"],
                         [render(("Bridge", "Continuing the previous conversation (1 turn)" if n == 1
                                  else "Continuing the previous conversation (%1 turns)", (n,))) for n in (1, 4)])

    def test_e_continuing_the_previous_conversation_counts_turns(self) -> None:
        store = self.db()
        previous = store.create_conversation()
        say(store, previous.id, "tek")
        one = self.bridge()
        self.assertIn("Continuing the previous conversation (1 turn)", [n["text"] for n in one.activity])
        say(store, previous.id, "iki")
        two = self.bridge()
        self.assertIn("Continuing the previous conversation (2 turns)", [n["text"] for n in two.activity])


class UntitledNameTests(WindowCase):
    def test_f_one_untitled_name_in_every_list_and_dialog(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        loose = store.create_conversation()
        store.archive_conversation(loose.id)
        inside, archived = store.create_conversation(project.id), store.create_conversation(project.id)
        store.archive_conversation(archived.id)
        bridge = self.bridge()
        root = self.window(bridge)

        def names() -> list:
            bridge.refreshProjects(); bridge._refresh_conversations(); bridge.showProject(project.id); pump(300)
            return [r["title"] for r in (*bridge.archivedConversations, *bridge.projectConversations,
                                         *bridge.projectArchivedConversations)]
        self.assertEqual(["New Chat"] * 3, names())
        self.assertEqual("New Chat", self.item(root, "conversationSwitcherText").property("text"))
        self.tr(bridge)
        self.assertEqual(["Yeni Sohbet"] * 3, names())
        bridge.setPage("home"); pump(300)
        self.assertEqual("Yeni Sohbet", self.item(root, "conversationSwitcherText").property("text"))
        self.assertEqual(inside.id, bridge.projectConversations[0]["id"])
        self.assertEqual("Yeni Sohbet", tx("AttachDialog", "New Chat"))
        self.assertEqual("Yeni Sohbet", tx("ConversationDeleteDialog", "New Chat"))

    def test_g_the_actions_stay_sentence_case(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("+ Yeni sohbet", tx("TopBar", "+ New conversation"))
        self.assertEqual("Yeni sohbet", tx("HistoryPage", "New conversation"))
        self.assertEqual("Yeni sohbet", tx("CommandPalette", "New conversation"))
        self.assertEqual("Evim içinde yeni sohbet", tx("ProjectsPage", "New conversation in %1").replace("%1", "Evim"))

    def test_h_the_archived_headings_name_what_they_hold(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        other = store.create_project("Açık")
        kept = store.create_conversation(other.id, title="Eski")
        store.archive_conversation(kept.id)
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.archiveProject(project.id); pump(200)
        self.tr(bridge)
        bridge.showProject(other.id); bridge.setPage("projects"); pump(400)
        heading = lambda name: self.item(root, name).property("text")  # noqa: E731
        self.assertEqual("ARŞİVLENMİŞ PROJELER", heading("projectsArchivedHeading"))
        self.assertEqual("ARŞİVLENMİŞ SOHBETLER", heading("projectArchivedConversationsHeading"))
        bridge.setPage("history"); pump(400)
        self.assertEqual("ARŞİVLENMİŞ SOHBETLER", heading("historyArchivedHeading"))


class CatalogTests(UiTestCase):
    def test_i_no_konusma_in_the_turkish_catalog_of_these_surfaces(self) -> None:
        table = load_catalog(CATALOG_DIR / "jarvis_tr-TR.ts")
        found = {key: value for key, value in table.items() if key[0] in SURFACES and re.search("konuşma|KONUŞMA", value, re.I)}
        self.assertEqual({}, found)

    def test_j_the_copy_rules(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("SOHBETLER", tx("HistoryPage", "CONVERSATIONS"))
        self.assertEqual("SOHBETLER", tx("ProjectsPage", "CONVERSATIONS"))
        self.assertEqual("SOHBETLER", tx("TopBar", "CONVERSATIONS"))
        self.assertEqual("ARŞİVLENMİŞ SOHBETLER", tx("ProjectsPage", "ARCHIVED CONVERSATIONS"))
        self.assertEqual("ARŞİVLENMİŞ PROJELER", tx("ProjectsPage", "ARCHIVED PROJECTS"))
        self.assertEqual("Bu projede henüz sohbet yok.", tx("ProjectsPage", "No conversation in this project yet."))
        self.assertEqual("Sohbetlerini görmek için bir proje seçin.", tx("ProjectsPage", "Choose a project to see its conversations."))
        self.assertEqual("Salt okunur: bu sohbet arşivlenmiş %1 projesine ait. Devam etmek için projeyi arşivden çıkarın.",
                         tx("Composer", "Read-only: this conversation belongs to the archived project %1. Unarchive the project to continue it."))
        self.assertEqual("Yeni sohbet", tx("Bridge", "New conversation"))      # the activity note
        self.assertEqual("Yeni Sohbet", tx("Bridge", "New Chat"))              # the untitled NAME: its own source
        for source, expected in (("1 message", "1 mesaj"), ("%1 messages", "%1 mesaj")):
            self.assertEqual(expected, tx("ConversationDeleteDialog", source))
        for source, expected in (("1 turn", "1 mesaj"), ("%1 turns", "%1 mesaj")):
            self.assertEqual(expected, tx("AttachDialog", source))
        self.assertEqual("1 görev", tx("HistoryPage", "1 task"))
        self.assertEqual("%1 görev", tx("HistoryPage", "%1 tasks"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
