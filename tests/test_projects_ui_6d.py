"""V2 Projects Gate 6D-a: the top-bar conversation switcher and rename,
through the real bridge, the real app path and the real QML.

A. Switcher: the open conversation's name ("Yeni Sohbet" when it has none);
   the menu is exactly the CURRENT scope's non-archived conversations, newest
   first, the open one marked; choosing opens it; the busy guard. (Sidebar v2:
   the plain list is fixed; a project's conversations are its tree rows.)
B. Rename: right-click -> edit -> commit through conversation_rename_result;
   normalization first, then the store alone decides; empty -> no call; the
   archived project's conversation is disabled and a forced call refused.
C. Race safety: a manual name is never overwritten by the automatic one; an
   untitled conversation still gets its automatic name; busy disables the
   rename; an edit is bound to the conversation it started on.
D. Authority: one new production caller of set_conversation_title; a rename
   changes no approval decision and nothing else the person was shown.
E. Layout: retired with the TopBar scope group (Gate 6F L1); the header and
   sidebar layout is tests/test_projects_ui_6f.py.
F. Turkish catalog.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from jarvis.conversation import append_turn, task_title
from tests.test_intent import task_proposal
from tests.test_projects_ui import ProjectsUiCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.helpers import settled
from tests.test_ui import UiTestCase, _APP, pump

CHAT = json.dumps({"kind": "chat", "reply": "tamam"})


class SwitcherCase(ProjectsUiCase):
    def visible(self, root, name) -> list:
        return [i for i in self.all_items(root, name) if i.isVisible()]

    def press(self, root, item, button=Qt.LeftButton, then=None) -> None:
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(root, button, Qt.NoModifier, center)
        if then is not None:        # THIZ: the caller's assertions; one that holds at once keeps the old wait
            settled(260, then)
        else:
            pump(260)

    def editing(self, root) -> bool:
        """The edit popup is open (its field is on screen; a Popup itself is no item)."""
        return any(i.isVisible() for i in self.all_items(root, "conversationRenameField"))

    def text(self, root, name) -> str:
        return self.item(root, name).property("text")

    def menu(self, root) -> list[tuple]:
        """(label, open?) of the scope's conversation list (Gate 6F: the sidebar rows), in order."""
        return [(i.property("title"), i.property("current")) for i in self.visible(root, "sidebarConversation")]

    def close_menu(self, root) -> None:
        pass                                  # the sidebar list is always shown: nothing to close

    def open_actions(self, root) -> None:
        """The conversation menu of the OPEN conversation (Gate 6F: the chat header's "…")."""
        self.press(root, self.item(root, "chatHeaderMenu"))

    def start_edit(self, root) -> None:
        self.open_actions(root)
        self.press(root, self.item(root, "conversationRenameItem"))

    def commit(self, root, text: str) -> None:
        self.item(root, "conversationRenameField").setProperty("text", text)
        pump(60)
        self.press(root, self.item(root, "conversationRenameSave"))

    def spy(self, bridge) -> list:
        calls, real = [], bridge.backend.conversation_rename

        def record(conversation_id, title):
            calls.append((conversation_id, title))
            return real(conversation_id, title)
        bridge.backend.conversation_rename = record
        return calls


class SwitcherTests(SwitcherCase):
    def test_a_title_fallback_menu_switching_and_busy_guard(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        first = store.create_conversation(title="Birinci")
        second = store.create_conversation()                                       # untitled
        store.update_conversation_state(second.id, lambda s: append_turn(          # DFNEW: a turnless one is unlisted
            s, role="user", kind="user", text="selam", message="selam")[0])
        third = store.create_conversation(title="Üçüncü")
        archived = store.create_conversation(title="Arşiv")
        store.archive_conversation(archived.id)
        owned = store.create_conversation(project.id, title="Proje sohbeti")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.openConversation(second.id)
        pump(200)
        self.assertEqual("New Chat", self.text(root, "chatHeaderTitle"))
        bridge.openConversation(first.id)
        pump(200)
        self.assertEqual("Birinci", self.text(root, "chatHeaderTitle"))
        # Global scope: exactly its non-archived conversations, newest first, the open one marked.
        expected = [c.id for c in store.list_conversations(project_id=None)]
        self.assertEqual({first.id, second.id, third.id}, set(expected))                 # no archived, no project one
        titles = {c.id: (c.title or "New Chat") for c in store.list_conversations(project_id=None)}
        self.assertEqual([(titles[i], i == first.id) for i in expected], self.menu(root))
        self.assertEqual([i for i in expected], [row["id"] for row in bridge.scopeConversations])
        items = self.visible(root, "sidebarConversation")
        target = [i for i in items if i.property("title") == "Üçüncü"][0]
        self.press(root, target)
        self.assertEqual(third.id, bridge.conversationId)
        self.assertEqual("Üçüncü", self.text(root, "chatHeaderTitle"))
        self.assertFalse([label for label, _ in self.menu(root) if label in ("Arşiv", "Proje sohbeti")])
        self.close_menu(root)
        # Project scope: only that project's conversations - Sidebar v2 V1/T1: in its tree rows,
        # the open one marked; the plain list stays fixed (it does not follow the project).
        self.assertTrue(bridge.openProjectScope(project.id))
        pump(300)
        self.assertEqual(owned.id, bridge.conversationId)
        self.assertEqual([("Proje sohbeti", True)], [(i.property("title"), i.property("current"))
                                                     for i in self.visible(root, "sidebarProjectConversation")])
        self.assertEqual([(c.title or "New Chat", False) for c in store.list_conversations(project_id=None)], self.menu(root))
        self.close_menu(root)
        # The busy guard: nothing opens while busy, and the choices are disabled.
        bridge.openProjectScope("")
        pump(200)
        bridge._busy = True
        bridge.stateChanged.emit()
        pump(60)
        self.assertTrue(self.visible(root, "sidebarConversation"))
        self.assertFalse(any(i.property("available") for i in self.visible(root, "sidebarConversation")))
        before = bridge.conversationId
        self.assertFalse(bridge.openConversation(first.id if before != first.id else second.id))
        self.assertEqual(before, bridge.conversationId)
        bridge._busy = False
        self.assertEqual(0, self.execute_calls())

    def test_a_the_full_title_is_in_the_tooltip_and_the_label_elides(self) -> None:
        store = self.db()
        long_title = "Çok uzun bir sohbet adı " * 4
        conversation = store.create_conversation(title=long_title.strip())
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.openConversation(conversation.id)
        pump(300)
        for size in ((1280, 760), (1100, 700)):
            root.resize(*size)
            pump(500)
            with self.subTest(size=size):
                self.assertEqual(long_title.strip(), self.text(root, "chatHeaderTitleTip"))
                header, label = self.item(root, "chatHeader"), self.item(root, "chatHeaderTitle")
                self.assertLessEqual(label.mapToItem(header, label.width(), 0).x(), header.width())
                self.assertLess(label.width(), label.property("implicitWidth"))
                row = [i for i in self.visible(root, "sidebarConversation") if i.property("current")][0]
                self.assertTrue([c for c in row.childItems() if c.objectName() == "sidebarConversationTitle"][0].property("truncated"))
                self.assertEqual(long_title.strip(), [c for c in row.childItems() if c.objectName() == "sidebarRowTip"][0].property("text"))


class RenameTests(SwitcherCase):
    def test_b_right_click_edit_commit_and_where_the_name_shows(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        owned = store.create_conversation(project.id, title="Eski ad")
        bridge = self.bridge()
        root = self.window(bridge)
        calls = self.spy(bridge)
        conversation = store.create_conversation(title="Global ad")
        bridge.openConversation(conversation.id)
        pump(200)
        # The conversation menu (the header's "…"): the one item, enabled, with no reason.
        self.open_actions(root)
        self.assertTrue(self.item(root, "conversationRenameItem").isEnabled())
        self.assertFalse(self.item(root, "conversationRenameReason").isVisible())
        self.assertEqual(1, len(self.visible(root, "conversationRenameItem")))
        self.press(root, self.item(root, "conversationRenameItem"))
        self.assertTrue(self.editing(root))
        self.assertEqual("Global ad", self.text(root, "conversationRenameField"))
        # Normalization first: newlines / tabs / control characters become single spaces.
        self.commit(root, "  Tatil\n\nplanı\t\x01 2025  ")
        self.assertEqual([(conversation.id, "Tatil planı 2025")], calls)
        self.assertEqual("Tatil planı 2025", store.load_conversation(conversation.id).title)
        self.assertFalse(self.editing(root))
        self.assertEqual("Tatil planı 2025", self.text(root, "chatHeaderTitle"))
        self.assertIn("Tatil planı 2025", [i.property("title") for i in self.visible(root, "sidebarConversation")])
        self.assertIn("Tatil planı 2025", [row["title"] for row in bridge.conversations])       # History
        # The project's conversation list shows a project conversation's new name.
        bridge.openConversation(owned.id)
        pump(200)
        self.start_edit(root)
        self.commit(root, "Yeni proje adı")
        bridge.showProject(project.id)
        settled(300, lambda: (
            self.assertEqual(["Yeni proje adı"], [row["title"] for row in bridge.projectConversations]),
            self.assertEqual(0, self.execute_calls()),
        ))

    def test_b_enter_commits_and_escape_reverts(self) -> None:
        store = self.db()
        conversation = store.create_conversation(title="Ad")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.openConversation(conversation.id)
        pump(200)
        calls = self.spy(bridge)
        self.start_edit(root)
        self.item(root, "conversationRenameField").setProperty("text", "Vazgeçildi")
        pump(60)
        QTest.keyClick(root, Qt.Key_Escape)
        pump(260)
        self.assertFalse(self.editing(root))
        self.assertEqual(([], "Ad"), (calls, store.load_conversation(conversation.id).title))
        self.start_edit(root)
        self.item(root, "conversationRenameField").setProperty("text", "Enter ile")
        pump(60)
        QTest.keyClick(root, Qt.Key_Return)
        pump(260)
        self.assertEqual("Enter ile", store.load_conversation(conversation.id).title)
        self.assertFalse(self.editing(root))
        # Cancel reverts too.
        self.start_edit(root)
        self.item(root, "conversationRenameField").setProperty("text", "İptal")
        pump(60)
        self.press(root, self.item(root, "conversationRenameCancel"))
        self.assertEqual("Enter ile", store.load_conversation(conversation.id).title)

    def test_b_empty_makes_no_call_and_too_long_is_the_stores_refusal(self) -> None:
        store = self.db()
        conversation = store.create_conversation(title="Ad")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.openConversation(conversation.id)
        pump(200)
        calls = self.spy(bridge)
        self.start_edit(root)
        self.commit(root, " \n\t\x07 ")                                              # empty once normalized
        self.assertEqual([], calls)
        self.assertEqual("Ad", store.load_conversation(conversation.id).title)
        self.assertTrue(self.item(root, "conversationRenameNotice").isVisible())
        self.assertTrue(self.editing(root))
        # 121 characters: the UI does not limit; the store refuses and says so.
        self.commit(root, "x" * 121)
        self.assertEqual([(conversation.id, "x" * 121)], calls)
        self.assertIn("at most 120", self.text(root, "conversationRenameNotice"))
        self.assertEqual("Ad", store.load_conversation(conversation.id).title)
        # Exactly 120 is accepted.
        self.commit(root, "y" * 120)
        self.assertEqual("y" * 120, store.load_conversation(conversation.id).title)
        self.assertFalse(self.editing(root))

    def test_b_an_archived_projects_conversation_is_disabled_and_the_store_refuses(self) -> None:
        store = self.db()
        project = store.create_project("Tez")
        owned = store.create_conversation(project.id, title="Tez sohbeti")
        store.set_project_active(project.id, False)
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.openConversation(owned.id)
        settled(300, lambda: (
            self.assertTrue(bridge.conversationReadOnly),
        ))
        self.open_actions(root)
        self.assertFalse(self.item(root, "conversationRenameItem").isEnabled())
        self.assertIn("archived", self.text(root, "conversationRenameReason"))
        self.assertTrue(self.item(root, "conversationRenameReason").isVisible())
        self.press(root, self.item(root, "conversationRenameItem"), then=lambda: (  # clicking a disabled item does nothing
            self.assertFalse(self.editing(root)),
        ))
        calls = self.spy(bridge)
        self.assertFalse(bridge.renameConversation(owned.id, "zorla"))             # a forced call meets the store
        self.assertEqual([(owned.id, "zorla")], calls)
        self.assertIn("archived", bridge.conversationRenameNotice)
        self.assertEqual("Tez sohbeti", store.load_conversation(owned.id).title)


class RaceTests(SwitcherCase):
    def test_c_a_manual_name_survives_the_first_message(self) -> None:
        store = self.db()
        conversation = store.create_conversation()
        bridge = self.bridge(CHAT)
        bridge.openConversation(conversation.id)
        pump(200)
        self.assertTrue(bridge.renameConversation(conversation.id, "Tatil planı"))
        bridge.submitText("Bu proje hakkında bir şey sor")
        pump(200)
        self.assertEqual("Tatil planı", store.load_conversation(conversation.id).title)
        self.assertEqual("Tatil planı", bridge.conversationTitle)

    def test_c_an_untitled_conversation_still_gets_its_automatic_name(self) -> None:
        store = self.db()
        conversation = store.create_conversation()
        bridge = self.bridge(CHAT, CHAT)
        bridge.openConversation(conversation.id)
        settled(200, lambda: (
            self.assertEqual("", bridge.conversationTitle),
        ))
        message = "Bu projenin yapısını özetle"
        bridge.submitText(message)
        pump(200)
        automatic = task_title(message)
        self.assertTrue(automatic)
        self.assertEqual(automatic, store.load_conversation(conversation.id).title)
        self.assertEqual(automatic, bridge.conversationTitle)
        # It keeps that name (the second message names nothing) until renamed.
        bridge.submitText("Şimdi başka bir konu")
        pump(200)
        self.assertEqual(automatic, store.load_conversation(conversation.id).title)
        self.assertTrue(bridge.renameConversation(conversation.id, "Benim adım"))
        bridge.submitText("Bir şey daha")
        pump(200)
        self.assertEqual("Benim adım", store.load_conversation(conversation.id).title)

    def test_c_busy_disables_the_rename_and_a_manual_rename_then_wins(self) -> None:
        store = self.db()
        conversation = store.create_conversation()
        bridge = self.bridge(CHAT)
        root = self.window(bridge)
        bridge.openConversation(conversation.id)
        pump(200)
        seen = {}
        real = bridge.backend.submit_text

        def during(*args, **kwargs):
            # Runs inside the submit: the conversation is busy right now.
            seen["busy"] = bridge.busy
            seen["allowed"] = self.item(root, "conversationMenu").property("renameAllowed")
            seen["commit"] = bridge.renameConversation(conversation.id, "Çok erken")
            seen["notice"] = bridge.conversationRenameNotice
            seen["title_during"] = store.load_conversation(conversation.id).title
            return real(*args, **kwargs)
        bridge.backend.submit_text = during
        message = "Bu projenin yapısını özetle"
        bridge.submitText(message)
        pump(200)
        self.assertEqual((True, False, False), (seen["busy"], seen["allowed"], seen["commit"]))
        self.assertIn("Wait", seen["notice"])
        self.assertEqual(task_title(message), seen["title_during"])                  # the automatic name came first
        self.assertFalse(bridge.busy)
        self.assertTrue(self.item(root, "conversationMenu").property("renameAllowed"))
        # After it clears, a manual rename wins and is never overwritten.
        self.assertTrue(bridge.renameConversation(conversation.id, "Elle verilen ad"))
        self.assertEqual("Elle verilen ad", store.load_conversation(conversation.id).title)
        self.assertEqual("Elle verilen ad", bridge.conversationTitle)

    def test_c_an_edit_is_bound_to_the_conversation_it_started_on(self) -> None:
        store = self.db()
        a = store.create_conversation(title="A adı")
        b = store.create_conversation(title="B adı")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.openConversation(a.id)
        pump(200)
        self.start_edit(root)
        self.item(root, "conversationRenameField").setProperty("text", "A yeni")
        pump(60)
        # The open conversation changes under the edit: it is cancelled.
        self.assertTrue(bridge.openConversation(b.id))
        pump(300)
        self.assertFalse(self.editing(root))
        self.assertEqual(("A adı", "B adı"), (store.load_conversation(a.id).title, store.load_conversation(b.id).title))
        # A commit that still carries A's id renames A only, never the open B.
        self.assertTrue(bridge.renameConversation(a.id, "A yeni"))
        self.assertEqual(("A yeni", "B adı"), (store.load_conversation(a.id).title, store.load_conversation(b.id).title))
        self.assertEqual(b.id, bridge.conversationId)
        self.assertEqual("B adı", self.text(root, "chatHeaderTitle"))
        # And the QML commit path uses the captured id, not the open one.
        self.start_edit(root)
        self.item(root, "conversationRenameField").setProperty("text", "B yeni")
        pump(60)
        self.assertEqual(b.id, self.item(root, "conversationMenu").property("editId"))
        self.press(root, self.item(root, "conversationRenameSave"))
        self.assertEqual(("A yeni", "B yeni"), (store.load_conversation(a.id).title, store.load_conversation(b.id).title))


class AuthorityTests(SwitcherCase):
    def test_d_one_new_production_caller_of_set_conversation_title(self) -> None:
        root = Path(__file__).resolve().parents[1] / "jarvis"
        callers = sorted(f"{p.relative_to(root).as_posix()}:{path_text.count('set_conversation_title(')}"
                         for p in root.rglob("*.py")
                         if (path_text := p.read_text(encoding="utf-8")).count("set_conversation_title(")
                         and p.name != "memory.py")
        self.assertEqual(["app.py:1", "ui/bridge.py:1"], callers)
        app = (root / "app.py").read_text(encoding="utf-8")
        self.assertLess(app.index("def conversation_rename_result("), app.index("memory.set_conversation_title("))
        bridge = (root / "ui" / "bridge.py").read_text(encoding="utf-8")
        self.assertLess(bridge.index("def _name_conversation("), bridge.index("set_conversation_title("))
        self.assertLess(bridge.index("set_conversation_title("), bridge.index("def _refresh_conversations("))
        backend = (root / "ui" / "backend.py").read_text(encoding="utf-8")
        self.assertIn("conversation_rename_result(", backend)
        self.assertNotIn("set_conversation_title", backend)
        for path in root.rglob("*.py"):
            if path.name not in ("app.py", "backend.py"):
                self.assertNotIn("conversation_rename_result", path.read_text(encoding="utf-8"), path.name)

    def test_d_renaming_changes_no_approval_decision_and_nothing_shown(self) -> None:
        store = self.db()
        bridge = self.bridge(task_proposal("code_analysis", objective="Bu projeyi incele."),
                             task_proposal("code_analysis", objective="Bu projeyi incele."))
        decisions, cards, turns = [], [], []
        for renamed in (False, True):
            if renamed:
                self.assertTrue(bridge.renameConversation(bridge.conversationId, "Bambaşka ad"))
            bridge.submitText("Bu projeyi incele.")
            settled(100, lambda: (
                self.assertTrue(bridge.approvalPending),
            ))
            ticket = bridge._ticket
            request = store.load_pending_approval(ticket.task_id, ticket.session_id)
            decisions.append((request.scope, request.risk_level, request.side_effect, request.reason))
            cards.append({k: v for k, v in bridge.approval.items() if k not in ("request_id", "fingerprint", "task_id", "session_id",
                                                                             "expires_at")})
            turns.append([t.message for t in store.load_conversation_state(bridge.conversationId).turns][:1])
            bridge.deny()
            pump(100)
        self.assertEqual(decisions[0], decisions[1])
        self.assertEqual(cards[0], cards[1])
        self.assertEqual(turns[0], turns[1])                                             # history turns untouched
        self.assertEqual("Bambaşka ad", store.load_conversation(bridge.conversationId).title)
        self.assertEqual(0, self.execute_calls())


class CatalogTests(UiTestCase):
    def test_f_new_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Yeni Sohbet", tx("ChatHeader", "New Chat"))
        self.assertEqual("Yeniden adlandır", tx("ConversationMenu", "Rename"))
        # Sidebar v2 L3: no SOHBETLER heading (the string left the catalog: its source comes back);
        # the list is headed by SABİTLENENLER and the day separators.
        self.assertEqual("CONVERSATIONS", tx("Sidebar", "CONVERSATIONS"))
        self.assertEqual("SABİTLENENLER", tx("Sidebar", "PINNED"))
        self.assertEqual("Bir ad yazın; eski ad korunur.", tx("Bridge", "Type a name; the old name is kept."))
