"""Gate 6G Sidebar v2, slice 1 (list structure; build/6g/PLAN_DRAFT.txt "SIDEBAR v2"
and its revisions; roadmap 3.7 "Kenar çubuğu v2").

V1/L3  The plain (no-project) conversations are ONE fixed list by day (Bugün / Dün /
       Daha eski, F6 collapse rules), with no "Genel" row and no SOHBETLER heading;
       it does not change with the open conversation.
V3/V10 SABİTLENENLER is one shared pin list; the lead glyph says the kind (folder =
       project conversation, speech bubble = plain); no project label.
N1     "Yeni sohbet" is a full row above PROJELER (a muted "+"); it opens a new,
       empty conversation.
L2     The word PROJELER links to the Projects page; the rest of the row collapses
       the tree (chevron, count while collapsed, remembered).
T1-T4  Project rows are a tree: a click toggles that project's conversations
       (default open, remembered per project) and never opens the project page;
       right-click = "Proje sayfasını aç", "Bu projede yeni sohbet", "Sabitle",
       "Arşivle"; no hover "⋯"; semibold names; the count while collapsed; the
       tree and the list each scroll on their own.
P1     A label-less bar over the plain conversations hides / shows them ("n sohbet"
       while hidden, remembered; the open one stays).
V9     Archived projects and conversations are not in the sidebar.
V7     A no-project conversation's header has no scope line.

Window preferences only: the store is never written by a collapse. No test performs a
live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from tests.helpers import settled, wait_until
from tests.test_projects_ui_6f import F6Case, dump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump


class SidebarV2Case(F6Case):
    def data(self):
        """Plain: 2 today, 1 yesterday, 1 earlier, 1 archived. Tez: 2 live + 1 archived. Kod: 1. Eski: archived project."""
        store = self.db()
        now = self.now = datetime.now().astimezone()                        # THIZ2: once per test
        self.tez = store.create_project("Tez")
        self.kod = store.create_project("Kod")
        self.eski = store.create_project("Eski")
        self.tezc = [store.create_conversation(self.tez.id, title=f"Tez {n}") for n in range(2)]
        store.archive_conversation(store.create_conversation(self.tez.id, title="Tez arşiv").id)
        self.kodc = store.create_conversation(self.kod.id, title="Kod 0")
        store.create_conversation(self.eski.id, title="Eski proje sohbeti")
        store.set_project_active(self.eski.id, False)
        self.older = store.create_conversation(title="Düz eski")
        self.stamp(self.older.id, now - timedelta(days=5))
        self.yday = store.create_conversation(title="Düz dün")
        self.stamp(self.yday.id, now - timedelta(days=1))
        store.archive_conversation(store.create_conversation(title="Düz arşiv").id)
        self.plain = [store.create_conversation(title=f"Düz {n}") for n in range(2)]
        return store

    def open_window(self):
        bridge = self.bridge()
        root = self.window(bridge)
        self.fixed_today(root, self.now)
        pump(100)
        return bridge, root

    def tree_file(self):
        return self.prefs_path.with_suffix(".sidebar-tree.json")

    def projects(self, root) -> list:
        return [i.property("title") for i in self.shown(root, "sidebarProject")]

    def tree_titles(self, root) -> list:
        return [i.property("title") for i in self.shown(root, "sidebarProjectConversation")]

    def everything(self, root) -> list:
        names = ("sidebarProject", "sidebarProjectConversation", "sidebarPinnedConversation", "sidebarConversation")
        return [i.property("title") for n in names for i in self.shown(root, n)]


class FixedListTests(SidebarV2Case):
    def test_a_one_fixed_plain_list_by_day_without_genel_or_a_heading(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[1].id))
        settled(300, lambda: (
            self.assertEqual(["Today", "Yesterday", "Earlier"], self.texts(root, "sidebarGroupLabel")),
            self.assertEqual(["Düz 1", "Düz 0", "Düz dün"], self.titles(root)),      # Daha eski collapsed (F6)
            self.assertEqual(["· 1"], self.texts(root, "sidebarGroupCount")),
        ))
        # No Genel row, no SOHBETLER heading, no scope chip, no "+" button, no empty-scope line.
        for gone in ("sidebarGlobal", "sidebarScopeChip", "sidebarNewConversation", "sidebarNewProject",
                     "sidebarConversationEmpty", "sidebarArchive", "sidebarArchivedProjects"):
            self.assertEqual([], self.all_items(root, gone), gone)
        nav = self.item(root, "sidebarNav")
        texts = [t.property("text") for t in self.descendants(nav) if t.isVisible() and isinstance(t.property("text"), str)]
        self.assertNotIn("CONVERSATIONS", texts)
        self.assertNotIn("Global", texts)
        # A project conversation opened: the plain list stays exactly as it was.
        self.tap(root, self.row(root, "sidebarProjectConversation", "Tez 1"), then=lambda: (
            self.assertEqual(self.tezc[1].id, bridge.conversationId),
            self.assertEqual(["Düz 1", "Düz 0", "Düz dün"], self.titles(root)),
            self.assertEqual({"pinned": True, "today": True, "yesterday": True, "earlier": False}, bridge.sidebarGroups),
        ))
        # The date groups are one state, whatever is open (collapsing from a project chat holds for the list).
        self.tap(root, self.head(root, "earlier"), then=lambda: (
            self.assertIn("Düz eski", self.titles(root)),
        ))
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        settled(300, lambda: self.assertIn("Düz eski", self.titles(root)))
        # A plain row's menu keeps the one-step delete (F7); a project row's still asks for the archive first.
        self.menu(root, self.row(root, "sidebarConversation", "Düz dün"))
        self.assertTrue(self.item(root, "conversationDeleteItem").property("available"))
        self.close_menu(root)
        self.menu(root, self.row(root, "sidebarProjectConversation", "Tez 0"))
        self.assertFalse(self.item(root, "conversationDeleteItem").property("available"))
        self.close_menu(root)
        self.assertEqual({"global": {"today": True, "yesterday": True, "earlier": True}},
                         json.loads(self.sidebar_file().read_text(encoding="utf-8"))["groups"])
        self.assertEqual(0, self.execute_calls())


class PinnedListTests(SidebarV2Case):
    def test_b_one_shared_pin_list_with_the_kind_glyph_and_no_project_label(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.pinConversation(self.plain[0].id))
        self.assertTrue(bridge.pinConversation(self.tezc[0].id))
        settled(300, lambda: (
            self.assertEqual(["Tez 0", "Düz 0"], self.pinned(root)),
            self.assertEqual(["PINNED"], self.texts(root, "sidebarPinnedLabel")),
        ))
        leads = {r.property("title"): r.property("lead") for r in self.shown(root, "sidebarPinnedConversation")}
        self.assertEqual({"Tez 0": "folder", "Düz 0": "chat"}, leads)
        glyphs = [i.property("name") for i in self.shown(root, "sidebarRowLead")
                  if i.parentItem().objectName() == "sidebarPinnedConversation"]
        self.assertEqual(["folder", "chat"], glyphs)
        self.assertEqual([], self.all_items(root, "sidebarPinPlace"))               # no project label (V10)
        self.assertEqual([], self.all_items(root, "sidebarPinIcon"))                # the kind glyph replaced the pin
        # A pinned conversation is not repeated in its project's tree or its date group.
        self.assertNotIn("Tez 0", self.tree_titles(root))
        self.assertNotIn("Düz 0", self.titles(root))
        # The grip still drags on hover (F6 unchanged).
        row = self.pin_row(root, "Tez 0")
        self.hover(root, row)
        settled(200, lambda: self.assertTrue(self.child(row, "sidebarPinGripArea").isVisible()))
        self.assertEqual(0, self.execute_calls())


class NewChatRowTests(SidebarV2Case):
    def test_c_yeni_sohbet_is_a_full_row_above_projeler_and_opens_a_new_chat(self) -> None:
        store = self.data()
        bridge, root = self.open_window()
        row = self.item(root, "sidebarNewChat")
        nav = self.item(root, "sidebarNav")
        self.assertEqual("New chat", row.property("title"))
        self.assertAlmostEqual(nav.width(), row.width(), delta=0.5)                # a full row
        self.assertLess(self.rect(row).bottom(), self.rect(self.item(root, "sidebarProjectsLink")).top() + 0.5)
        glyph = self.child(row, "sidebarRowLead")
        self.assertEqual("plus", glyph.property("name"))
        self.assertTrue(row.property("muted"))
        self.assertEqual([], [c for c in row.childItems() if c.objectName() == "sidebarRowMenuButton" and c.isVisible()])
        # From a plain conversation: a new plain one, empty, opened.
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        before = {c.id for c in store.list_conversations(include_archived=True)}
        self.tap(root, row, then=lambda: self.assertNotIn(bridge.conversationId, before))
        made = store.load_conversation(bridge.conversationId)
        self.assertIsNone(made.project_id)
        self.assertEqual([], list(store.load_conversation_state(made.id).turns))
        # From a project conversation: in that project (the N1 default; the picker is a later slice).
        self.assertTrue(bridge.openConversation(self.kodc.id))
        self.tap(root, self.item(root, "sidebarNewChat"), then=lambda: self.assertNotEqual(self.kodc.id, bridge.conversationId))
        self.assertEqual(self.kod.id, store.load_conversation(bridge.conversationId).project_id)
        self.assertEqual(0, self.execute_calls())


class ProjectsHeadTests(SidebarV2Case):
    def test_d_the_word_projeler_opens_the_projects_page_and_the_rest_collapses_the_tree(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.assertEqual([], self.all_items(root, "sidebarNewProject"))            # no "+" on the row
        self.assertEqual(["Kod", "Tez"], self.projects(root))  # G6SB3 DRAG: a never-ordered project list is newest first
        self.assertFalse(self.item(root, "sidebarProjectsCount").isVisible())
        self.assertEqual(90, self.item(root, "sidebarProjectsChevron").property("rotation"))
        # The rest of the row collapses: the tree and "Tüm projeler" go, the count shows.
        self.tap(root, self.item(root, "sidebarProjectsToggle"), then=lambda: (
            self.assertFalse(bridge.sidebarTree["projects_open"]),
            self.assertEqual([], self.projects(root)),
            self.assertFalse(self.item(root, "sidebarProjectList").isVisible()),
            self.assertFalse(self.item(root, "sidebarAllProjects").isVisible()),
            self.assertEqual("2", self.item(root, "sidebarProjectsCount").property("text")),
            self.assertEqual(0, self.item(root, "sidebarProjectsChevron").property("rotation")),
            self.assertEqual("home", bridge.page),
        ))
        self.assertFalse(json.loads(self.tree_file().read_text(encoding="utf-8"))["projects_open"])
        # Remembered across a reopen.
        root.hide()
        bridge, root = self.open_window()
        self.assertEqual([], self.projects(root))
        self.tap(root, self.item(root, "sidebarProjectsToggle"), then=lambda: self.assertEqual(["Kod", "Tez"], self.projects(root)))  # G6SB3 DRAG: a never-ordered project list is newest first
        # The word is a link to the Projects page.
        self.tap(root, self.item(root, "sidebarProjectsLink"), then=lambda: (
            self.assertEqual("projects", bridge.page),
            self.assertTrue(bridge.sidebarTree["projects_open"]),
        ))
        self.assertEqual(0, self.execute_calls())


class TreeTests(SidebarV2Case):
    def test_e_a_project_row_toggles_its_conversations_remembered_per_project(self) -> None:
        self.data()
        self.stamp(self.tezc[0].id, self.now - timedelta(days=1))                 # no day heading in a tree: its day
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        db_before = dump(self.database)
        # Default: every project open, its live conversations newest first, flush left, times at right.
        settled(300, lambda: self.assertEqual(["Kod 0", "Tez 1", "Tez 0"], self.tree_titles(root)))  # G6SB3 DRAG: a never-ordered project list is newest first
        project = self.row(root, "sidebarProject", "Tez")
        conversation = self.row(root, "sidebarProjectConversation", "Tez 1")
        name = self.child(project, "sidebarProjectName")
        self.assertTrue(name.property("font").bold())                               # semibold (T4)
        self.assertAlmostEqual(name.x(), self.child(conversation, "sidebarProjectConversationTitle").x(), delta=0.5)
        self.assertEqual(["project"], [c.property("name") for c in project.childItems()
                                       if c.objectName() == "sidebarRowLead" and c.isVisible()])   # DFSIDE: the project icon
        self.assertEqual("", self.child(project, "sidebarProjectCount").property("text"))   # count only while collapsed
        self.assertRegex(self.child(conversation, "sidebarProjectConversationTime").property("text"), r"^\d\d:\d\d$")
        self.assertEqual("yesterday", self.child(self.row(root, "sidebarProjectConversation", "Tez 0"),
                                                 "sidebarProjectConversationTime").property("text"))
        # No hover "⋯" on a project row (F7); a conversation row has one.
        self.hover(root, project)
        settled(200, lambda: self.assertFalse(self.child(project, "sidebarRowMenuButton").isVisible()))
        self.hover(root, conversation)
        settled(200, lambda: self.assertTrue(self.child(conversation, "sidebarRowMenuButton").isVisible()))
        # A click collapses ITS list (count shown), stays on the page, opens nothing.
        self.tap(root, self.row(root, "sidebarProject", "Tez"), then=lambda: (
            self.assertEqual(["Kod 0"], self.tree_titles(root)),
            self.assertEqual("2", self.child(self.row(root, "sidebarProject", "Tez"), "sidebarProjectCount").property("text")),
            self.assertEqual(self.plain[0].id, bridge.conversationId),
            self.assertEqual("home", bridge.page),
            self.assertEqual([self.tez.id], bridge.sidebarTree["closed"]),
        ))
        # The open conversation stays in view in its collapsed project.
        self.assertTrue(bridge.openConversation(self.tezc[0].id))
        settled(300, lambda: self.assertEqual(["Kod 0", "Tez 0"], self.tree_titles(root)))  # G6SB3 DRAG: a never-ordered project list is newest first
        # Remembered per project across a reopen.
        root.hide()
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        settled(300, lambda: self.assertEqual(["Kod 0"], self.tree_titles(root)))
        self.tap(root, self.row(root, "sidebarProject", "Tez"), then=lambda: (
            self.assertEqual(["Kod 0", "Tez 1", "Tez 0"], self.tree_titles(root)),  # G6SB3 DRAG: a never-ordered project list is newest first
        ))
        self.assertEqual([], json.loads(self.tree_file().read_text(encoding="utf-8"))["closed"])
        self.assertEqual(db_before, dump(self.database))                           # window preferences only
        self.assertEqual(0, self.execute_calls())

    def test_f_the_tree_and_the_list_share_the_height_each_scrolling_on_its_own(self) -> None:
        store = self.db()
        self.now = datetime.now().astimezone()
        big = store.create_project("Büyük")
        for n in range(30):
            store.create_conversation(big.id, title=f"B {n}")
        for n in range(30):
            store.create_conversation(title=f"D {n}")
        bridge, root = self.open_window()
        tree, flist = self.item(root, "sidebarProjectList"), self.item(root, "sidebarConversationList")
        nav = self.item(root, "sidebarNav")
        self.assertGreater(tree.property("contentHeight"), tree.height())
        self.assertGreater(flist.property("contentHeight"), flist.height())
        self.assertLessEqual(self.rect(tree).bottom(), self.rect(flist).top() + 0.5)
        self.assertLessEqual(self.rect(flist).bottom(), self.rect(nav).bottom() + 0.5)
        step = self.item(root, "sidebar").property("step")
        self.assertAlmostEqual(tree.height(), flist.height(), delta=2 * step)          # about half each, whole rows
        # Each scrolls on its own.
        tree.setProperty("contentY", 200)
        pump(50)
        self.assertEqual(0, flist.property("contentY"))
        # Short content: the tree keeps only what it needs, the list takes the rest.
        root.hide()
        for c in store.list_conversations(project_id=big.id):
            store.archive_conversation(c.id)
        bridge, root = self.open_window()
        tree, flist = self.item(root, "sidebarProjectList"), self.item(root, "sidebarConversationList")
        self.assertEqual(tree.property("contentHeight"), tree.height())
        self.assertGreater(flist.height(), tree.height() * 3)


class ProjectMenuTests(SidebarV2Case):
    def project_menu(self, root, row):
        menu = self.popup_named(root, "sidebarProjectMenu")
        self.tap(root, row, Qt.RightButton, until=lambda: bool(menu.property("opened")))
        self.assertTrue(wait_until(lambda: bool(menu.property("opened")), 500))
        return menu

    def pick(self, root, menu, name) -> None:
        self.tap(root, self.item(root, name), until=lambda: not menu.property("visible"))

    def test_g_a_project_rows_right_click_menu(self) -> None:
        store = self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        pump(100)
        menu = self.project_menu(root, self.row(root, "sidebarProject", "Kod"))
        self.assertEqual(["Open project page", "New conversation in this project", "Pin", "Archive"],
                         [self.item(root, n).property("label") for n in ("sidebarProjectOpenItem", "sidebarProjectNewItem",
                                                                          "sidebarProjectPinItem", "sidebarProjectArchiveItem")])
        self.assertEqual("Kod", self.item(root, "sidebarProjectMenuHeader").property("text"))
        self.assertTrue(self.item(root, "sidebarProjectOpenItem").property("menuRow"))   # the K4 menu row
        self.assertTrue(self.tree_titles(root))                                     # a right-click toggles nothing
        self.assertEqual(["Kod 0", "Tez 1", "Tez 0"], self.tree_titles(root))  # G6SB3 DRAG: a never-ordered project list is newest first
        # Proje sayfasını aç.
        self.pick(root, menu, "sidebarProjectOpenItem")
        settled(300, lambda: (self.assertEqual("projects", bridge.page), self.assertEqual(self.kod.id, bridge.openProject["id"])))
        self.home(root, bridge, 500)
        # Bu projede yeni sohbet.
        menu = self.project_menu(root, self.row(root, "sidebarProject", "Kod"))
        self.pick(root, menu, "sidebarProjectNewItem")
        settled(300, lambda: (
            self.assertEqual(self.kod.id, store.load_conversation(bridge.conversationId).project_id),
            self.assertNotEqual(self.kodc.id, bridge.conversationId),
            self.assertEqual("home", bridge.page),
        ))
        # Sabitle: the project goes to the top of the tree; then Sabitlemeyi kaldır.
        menu = self.project_menu(root, self.row(root, "sidebarProject", "Kod"))
        self.pick(root, menu, "sidebarProjectPinItem")
        settled(300, lambda: (self.assertEqual(["Kod", "Tez"], self.projects(root)),
                              self.assertEqual([self.kod.id], bridge.sidebarTree["pinned_projects"])))
        menu = self.project_menu(root, self.row(root, "sidebarProject", "Kod"))
        self.assertEqual("Unpin", self.item(root, "sidebarProjectPinItem").property("label"))
        self.pick(root, menu, "sidebarProjectPinItem")
        settled(300, lambda: self.assertEqual(["Kod", "Tez"], self.projects(root)))  # G6SB3 DRAG: a never-ordered project list is newest first
        # Arşivle: through the app path; the archived project leaves the sidebar (V9).
        menu = self.project_menu(root, self.row(root, "sidebarProject", "Kod"))
        self.pick(root, menu, "sidebarProjectArchiveItem")
        self.assertTrue(wait_until(lambda: not store.load_project(self.kod.id).active, 2000))
        settled(300, lambda: (self.assertEqual(["Tez"], self.projects(root)), self.assertNotIn("Kod 0", self.everything(root))))
        self.assertEqual(0, self.execute_calls())

    def test_h_opening_the_project_menu_closes_a_row_menu_and_back(self) -> None:
        self.data()
        bridge, root = self.open_window()
        rowmenu = self.menu(root, self.row(root, "sidebarConversation", "Düz 1"))
        menu = self.project_menu(root, self.row(root, "sidebarProject", "Tez"))  # G6SB3 DRAG: Tez is the lower project now
        settled(300, lambda: (self.assertTrue(menu.property("visible")), self.assertFalse(rowmenu.property("visible"))))
        self.menu(root, self.row(root, "sidebarProjectConversation", "Kod 0"))       # above Tez: not under its menu
        settled(300, lambda: self.assertFalse(menu.property("visible")))
        self.close_menu(root)
        self.assertEqual(0, self.execute_calls())


class PlainBarTests(SidebarV2Case):
    def test_i_the_label_less_bar_hides_and_shows_the_plain_conversations(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[1].id))
        self.assertTrue(bridge.pinConversation(self.tezc[0].id))
        bar = self.item(root, "sidebarPlainBar")
        settled(300, lambda: self.assertTrue(bar.isVisible()))
        # Below SABİTLENENLER, above the date groups; no label of its own; the chevron only on hover.
        self.assertLess(self.rect(self.item(root, "sidebarPinned")).bottom(), self.rect(bar).top() + 0.5)
        self.assertLess(self.rect(bar).bottom(), self.rect(self.head(root, "today")).top() + 0.5)
        self.assertFalse(self.item(root, "sidebarPlainCount").isVisible())
        self.assertEqual(0, self.item(root, "sidebarPlainChevron").property("opacity"))
        self.hover(root, bar)
        settled(200, lambda: self.assertEqual(1, self.item(root, "sidebarPlainChevron").property("opacity")))
        # Hidden: only the open one stays; "n sohbet" (every plain conversation) shows.
        self.tap(root, bar, then=lambda: (
            self.assertFalse(bridge.sidebarTree["plain_open"]),
            self.assertEqual(["Düz 1"], self.titles(root)),
            self.assertEqual(["4 conversations"], self.texts(root, "sidebarPlainCount")),
            self.assertEqual(["Tez 0"], self.pinned(root)),                       # the pins stay
        ))
        self.assertFalse(json.loads(self.tree_file().read_text(encoding="utf-8"))["plain_open"])
        # Remembered across a reopen; a click shows them again.
        root.hide()
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.tezc[1].id))
        settled(300, lambda: self.assertEqual([], self.titles(root)))
        self.tap(root, self.item(root, "sidebarPlainBar"), then=lambda: (
            self.assertEqual(["Düz 1", "Düz 0", "Düz dün"], self.titles(root)),
        ))
        # The keyboard toggles it too.
        bar = self.item(root, "sidebarPlainBar")
        bar.forceActiveFocus()
        pump(100)
        QTest.keyClick(root, Qt.Key_Return)
        settled(300, lambda: self.assertFalse(bridge.sidebarTree["plain_open"]))
        self.assertEqual(0, self.execute_calls())


class ArchivedHiddenTests(SidebarV2Case):
    def test_j_archived_projects_and_conversations_are_not_in_the_sidebar(self) -> None:
        self.data()
        bridge, root = self.open_window()
        shown = self.everything(root)
        for gone in ("Eski", "Eski proje sohbeti", "Tez arşiv", "Düz arşiv"):
            self.assertNotIn(gone, shown)
        self.assertNotIn("Eski", self.projects(root))
        # "Tüm projeler" stays under the project list and opens the Projects page.
        link = self.item(root, "sidebarAllProjects")
        self.assertLess(self.rect(self.item(root, "sidebarProjectList")).bottom(), self.rect(link).top() + 0.5)
        self.tap(root, link, then=lambda: (self.assertEqual("projects", bridge.page), self.assertEqual({}, bridge.openProject)))
        self.assertEqual(0, self.execute_calls())


class HeaderTests(SidebarV2Case):
    def test_k_a_no_project_chat_header_has_no_scope_line(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        settled(300, lambda: (
            self.assertFalse(self.item(root, "chatHeaderScope").isVisible()),
            self.assertEqual(0, self.item(root, "chatHeaderTitle").x()),
            self.assertEqual("Düz 0", self.item(root, "chatHeaderTitle").property("text")),
        ))
        self.assertTrue(bridge.openConversation(self.tezc[0].id))
        settled(300, lambda: (
            self.assertTrue(self.item(root, "chatHeaderScope").isVisible()),
            self.assertEqual("Tez", self.item(root, "chatHeaderScopeName").property("text")),
            # Gate 6G F9 H2: the project's line sits ABOVE the title (was: beside it, title to its right).
            self.assertGreaterEqual(self.rect(self.item(root, "chatHeaderTitle")).top(), self.rect(self.item(root, "chatHeaderScope")).bottom()),
        ))
        self.assertEqual(0, self.execute_calls())


class TreePrefsTests(SidebarV2Case):
    def test_l_the_tree_file_loads_old_and_malformed_files_as_defaults(self) -> None:
        from jarvis.ui.preferences import UiPreferences, canonical_tree, validate_tree
        self.assertEqual(canonical_tree(), validate_tree({"version": 1, "projects_open": "no"}))
        self.assertEqual(canonical_tree(), validate_tree({**canonical_tree(), "closed": ["p-1", "p-1"]}))
        good = {**canonical_tree(), "projects_open": False, "closed": ["p-1"], "pinned_projects": ["p-2"]}
        self.assertEqual(good, validate_tree(json.loads(json.dumps(good))))
        prefs = UiPreferences(self.prefs_path)
        self.assertEqual(canonical_tree(), prefs.tree)                              # no file: defaults, nothing written
        self.assertFalse(prefs.tree_path.exists())
        prefs.tree_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(canonical_tree(), UiPreferences(self.prefs_path).tree)
        # The F6 sidebar file keeps its exact shape (its pins survive a rollback to F6).
        self.assertTrue(prefs.set_tree_member("closed", "p-1", True))
        self.assertFalse(prefs.set_tree_member("closed", "p-1", True))
        self.assertTrue(prefs.save_tree())
        self.assertFalse(prefs.sidebar_path.exists())
        self.assertEqual(["p-1"], UiPreferences(self.prefs_path).tree["closed"])


if __name__ == "__main__":  # pragma: no cover
    import unittest
    unittest.main()
