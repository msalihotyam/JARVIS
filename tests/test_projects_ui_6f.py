"""V2 Projects Gate 6F: the left sidebar.

Slice F1 - read-only Bridge additions for the sidebar:

B1. Every item of ``scopeConversations`` carries ``updated_at`` - the store's
    own timestamp, unchanged in format - so the sidebar can group Bugün / Dün /
    Daha eski.
B2. Every item of ``projectRecords`` carries ``conversation_count`` and
    ``archived_count``, cut from ONE ``list_conversations`` call per refresh
    (never a query per project) and recounted whenever the conversation lists
    are: create, archive, unarchive, attach and permanent delete.

Presentation only: extra keys on existing properties; the store is read, never
written, and no property, slot or signal is new.

Slice F2a - the sidebar shell (L2, Q3-Q5, Q7, Q11; L5): one panel on the home
page only, from the clock card's line to above the lower HUD; the 2x2 tiles
and the clock strip at its foot, inside the window with no clipping and no
overlap at 1100x700, 1280x760 and 1920x1080 in both themes; a hidden tile
reflows the grid; the peek opens beside the sidebar level with its tile; the
right column keeps its place.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import sqlite3
import unittest
from unittest import mock

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtTest import QTest

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



SIZES = ((1100, 700), (1280, 760), (1920, 1080))
TILES = ("Tasks", "Focus", "Plan", "Memory")


class SidebarShellCase(ProjectsUiCase):
    def box(self, root, name) -> QRectF:
        return self.rect(self.item(root, name))

    def inside(self, outer: QRectF, inner: QRectF, what: str) -> None:
        self.assertGreaterEqual(inner.left(), outer.left() - 0.5, what)
        self.assertGreaterEqual(inner.top(), outer.top() - 0.5, what)
        self.assertLessEqual(inner.right(), outer.right() + 0.5, what)
        self.assertLessEqual(inner.bottom(), outer.bottom() + 0.5, what)

    def apart(self, a: QRectF, b: QRectF, what: str) -> None:
        overlap = a.intersected(b)
        self.assertFalse(overlap.width() > 0.5 and overlap.height() > 0.5, what)

    def press(self, root, name) -> None:
        item = self.item(root, name)
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())
        pump(350)


class SidebarShellTests(SidebarShellCase):
    def test_h_sidebar_tiles_and_strip_fit_at_every_size_in_both_themes(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for theme in ("space", "dark"):
            bridge.setTheme(theme)
            for width, height in SIZES:
                root.resize(width, height); pump(700)
                with self.subTest(theme=theme, size=(width, height)):
                    window = QRectF(0, 0, width, height)
                    side = self.box(root, "sidebar")
                    self.inside(window, side, "sidebar")
                    # Theme.sidebarWidth: 340 at 1920, 256 below.
                    self.assertAlmostEqual(max(256, min(340, round(width * 0.1771))), side.width(), delta=0.5)
                    for other in ("canonicalWorkspace", "canonicalComposer", "panelColumn"):
                        self.apart(side, self.box(root, other), other)
                    strip = self.box(root, "leftContextCard")
                    self.inside(side, strip, "clock strip")
                    tiles = [self.box(root, "rail" + key) for key in TILES]
                    for key, tile in zip(TILES, tiles):
                        self.inside(side, tile, key)
                        self.assertLessEqual(tile.bottom(), strip.top(), key)
                        # One line that never clips: each text inside its tile, the line not elided.
                        for part in ("railTitle", "railCount", "railLine"):
                            text = self.item(root, part + key)
                            if not text.isVisible():
                                continue
                            pos = text.mapToItem(self.item(root, "rail" + key), QPointF(0, 0))
                            self.assertGreaterEqual(pos.x(), 0, part + key)
                            self.assertLessEqual(pos.x() + text.property("contentWidth"), tile.width() + 0.5, part + key)
                            self.assertLessEqual(pos.y() + text.property("contentHeight"), tile.height() + 0.5, part + key)
                            self.assertEqual(1, text.property("lineCount"), part + key)
                        line = self.item(root, "railLine" + key)
                        self.assertFalse(line.isVisible() and line.property("truncated"), key)   # a status tile's line gives way
                        self.assertFalse(self.item(root, "railTitle" + key).property("truncated"), key)
                    for i, a in enumerate(tiles):
                        for b in tiles[i + 1:]:
                            self.apart(a, b, "tiles")
                    for part in ("contextClock", "contextDate", "contextTimeZone", "contextTemperature",
                                 "contextWeatherCondition", "contextPlace"):
                        text = self.item(root, part)
                        if text.isVisible():
                            ink = self.rect(text)
                            ink.setWidth(text.property("contentWidth"))
                            self.inside(strip, ink, part)
                            self.assertEqual(1, text.property("lineCount"), part)
        self.assertEqual(0, self.execute_calls())

    def test_i_home_only_from_the_clock_line_and_the_right_column_keeps_its_place(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        # The right column at c49e856 (L5): (x, y, width) per size.
        column = {(1100, 700): (858, 150, 221), (1280, 760): (998, 150, 257), (1920, 1080): (1497, 205, 386)}
        for width, height in SIZES:
            root.resize(width, height); pump(700)
            with self.subTest(size=(width, height)):
                stack = self.box(root, "panelColumn")
                self.assertEqual(column[(width, height)], (round(stack.x()), round(stack.y()), round(stack.width())))
                side = self.box(root, "sidebar")
                # Where the clock card started (Q7: the TopBar keeps its height); while the
                # scope group takes its own row on a narrow window (until F4), below that row.
                scope = self.box(root, "projectScope")
                clock_line = max(84, round(height * 0.1980) + 76 - 12 - 144)
                self.assertGreaterEqual(side.top(), clock_line - 0.5)
                for name in ("brandWordmark", "brandTagline", "settingsOrb", "projectScope", "jarvisCore", "commandStrip"):
                    self.apart(side, self.box(root, name), name)
                if scope.top() < 40:                      # inline beside the wordmark: nothing below it
                    self.assertAlmostEqual(clock_line, side.top(), delta=0.5)
                self.assertAlmostEqual(height - 64, side.bottom(), delta=0.5)
        # Home only (Q11): every other page keeps its own width.
        for page in ("history", "projects", "tasks"):
            bridge.setPage(page); pump(500)
            self.assertFalse(self.item(root, "sidebar").isVisible(), page)
        bridge.setPage("home"); pump(500)
        self.assertTrue(self.item(root, "sidebar").isVisible())
        self.assertEqual(0, self.execute_calls())

    def test_j_a_hidden_tile_reflows_the_grid_at_every_size(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for width, height in SIZES:
            root.resize(width, height); pump(600)
            tile, gap = (86, 8) if width >= 1600 else (74, 6)
            with self.subTest(size=(width, height)):
                nav_full = self.box(root, "sidebarNav").height()
                self.assertAlmostEqual(2 * tile + gap, self.box(root, "summaryRail").height(), delta=0.5)
                bridge.setCardVisible("rail", "plan", False); pump(300)      # 3: two, then one across
                grid = self.box(root, "summaryRail")
                self.assertAlmostEqual(grid.width(), self.box(root, "railMemory").width(), delta=0.5)
                self.assertAlmostEqual(2 * tile + gap, grid.height(), delta=0.5)
                bridge.setCardVisible("rail", "focus", False); pump(300)     # 2: one row
                self.assertAlmostEqual(tile, self.box(root, "summaryRail").height(), delta=0.5)
                self.assertAlmostEqual(self.box(root, "railTasks").top(), self.box(root, "railMemory").top(), delta=0.5)
                self.assertGreater(self.box(root, "sidebarNav").height(), nav_full)   # the navigation takes the room
                bridge.setCardVisible("rail", "tasks", False); pump(300)     # 1: across
                self.assertAlmostEqual(self.box(root, "summaryRail").width(), self.box(root, "railMemory").width(), delta=0.5)
                bridge.setCardVisible("rail", "memory", False); pump(300)    # none: no grid
                self.assertFalse(self.item(root, "summaryRail").isVisible())
                for key in ("tasks", "focus", "plan", "memory"):
                    bridge.setCardVisible("rail", key, True)
                pump(300)
                self.assertEqual(TILES, tuple(c["id"].capitalize() for c in bridge.railCards if c["visible"] and c["id"] != "updates"))
        self.assertEqual(0, self.execute_calls())

    def test_k_the_peek_opens_beside_its_tile_inside_the_window(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for width, height in SIZES:
            root.resize(width, height); pump(600)
            for key in ("Tasks", "Focus", "Plan"):
                with self.subTest(size=(width, height), tile=key):
                    self.press(root, "rail" + key)
                    self.assertEqual((True, key), (root.property("peekOpen"), root.property("peekKey")))
                    peek, side, tile = self.box(root, "summaryPeek"), self.box(root, "sidebar"), self.box(root, "rail" + key)
                    self.inside(QRectF(0, 0, width, height), peek, "peek")
                    self.assertAlmostEqual(side.right() + 12, peek.left(), delta=0.5)
                    self.assertLessEqual(peek.top(), tile.center().y())
                    self.assertGreaterEqual(peek.bottom(), tile.center().y())
                    self.press(root, "rail" + key)
                    self.assertFalse(root.property("peekOpen"))
            # Memory is read, not pressed: no peek.
            self.press(root, "railMemory")
            self.assertFalse(root.property("peekOpen"))
        self.assertEqual(0, self.execute_calls())

    def test_l_edit_mode_keeps_a_left_section_for_the_four_tiles(self) -> None:
        bridge = self.bridge()
        bridge.setCardVisible("rail", "updates", True)        # a saved layout that shows Updates: still no tile (Q4)
        root = self.window(bridge)
        pump(500)
        self.assertEqual([], self.all_items(root, "railUpdates"))
        bridge.setEditMode(True); pump(300)
        for key in TILES:
            self.assertTrue(self.item(root, "railMenu" + key).isVisible(), key)
            # The menu sits on the number's line: the title keeps its room.
            self.assertFalse(self.item(root, "railTitle" + key).property("truncated"), key)
        self.press(root, "railMenuFocus")
        menus = self.item(root, "layoutMenus")
        self.assertEqual(("card", "rail", "focus"), tuple(menus.property(k) for k in ("mode", "region", "card")))
        self.press(root, "menuHide")
        self.assertEqual([], [i for i in self.all_items(root, "railFocus") if i.isVisible()])
        self.press(root, "layoutCards")
        # Only the tiles are offered on the left; Updates is not one.
        self.assertTrue(self.item(root, "addToLayout_focus").isVisible())
        self.assertEqual([], self.all_items(root, "addToLayout_updates"))
        self.press(root, "addToLayout_focus")
        self.assertTrue(self.item(root, "railFocus").isVisible())
        bridge.setEditMode(False); pump(300)
        self.assertEqual(0, self.execute_calls())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
