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

Slice F2b - tile content (Salih, T1-T4): Tasks shows the running task, else
a waiting approval (amber; its click goes to the approval), else the last
task, else nothing; Focus what the model sees in the selected scope; Plan
the next scheduled job; Memory the status and this scope's records. Two
read-only Bridge keys: B3 on the newest ``sessions`` item (its latest task)
and B4 on ``conversationProject`` (instruction, files, memory records).

Slice F3 - the sidebar navigation (L2 a-e, L4, Q8-Q10): Genel, the active
projects and the selected scope's conversations grouped Bugün / Dün / Daha
eski, in whole rows with no clipping and no overlap with the tiles; a scope
pick is the 6B rule, a row pick the switcher pick, the round "+" a new
conversation in the selected scope; the counts follow every change; a
right-click on a row opens the ONE conversation menu (ConversationMenu.qml,
which the TopBar switcher also opens) for THAT row without opening it; the
links go where Q9 / Q10 say; every action has a mouse path.

Slice F4 - the chat header and the slimmed TopBar (L1, L3, Q2): the header
(scope chip > title; "Projeye bağla" for a Global conversation only; "…" =
the conversation menu on the open conversation) fits at every size in both
themes with long names eliding to their tips; the TopBar has no scope group
and nothing overlaps there; the Command preset's chip opens the scope and
conversation menu; the "Projeye bağla" dialog is found by its type.

Slice F6 - collapsible date groups, pins and their order (F6 A-C): a group
heading collapses its group (its count shown; Bugün + Dün open, Daha eski
collapsed; the open conversation stays in view; a switch opens its group;
per scope, across a reopen); SABİTLENDİ gathers the pins of every scope
above the date groups (not repeated there; pin, faint project, tip), at most
ten, in a manual order (a drag by the grip, Yukarı / Aşağı taşı; a new pin on
top); archive and delete unpin. Window preferences only, in UiPreferences'
sidebar file beside the main document: the store and the main document stay
byte-identical, unknown ids are pruned on read, older files load.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import unittest
from datetime import timezone
from unittest import mock

from PySide6.QtCore import QMetaObject, QObject, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtTest import QTest

from jarvis.conversation import append_turn
from jarvis.memory import SQLiteMemoryStore
from tests.helpers import settled, wait_still, wait_until
from tests.test_projects_ui import ProjectsUiCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import _APP, ambient_on, drawn_at_rest, pump


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
        self.assertTrue(bridge.openProjectScope(self.many.id))
        pump(100)
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
        self.assertTrue(bridge.newProjectConversation(self.one.id))
        pump(100)
        self.assertEqual((2, 0), self.counts(bridge)["Tek"])
        self.assertIn("projects", seen)
        self.assertTrue(bridge.newConversation())
        pump(100)
        self.assertEqual({"Boş": (0, 0), "Tek": (2, 0), "Çok": (3, 2), "Eski": (1, 0)}, self.counts(bridge))
        # archive / unarchive a project conversation
        victim = self.live[0].id
        self.assertTrue(bridge.archiveConversation(victim))
        pump(100)
        self.assertEqual((2, 3), self.counts(bridge)["Çok"])
        self.assertTrue(bridge.unarchiveConversation(victim))
        pump(100)
        self.assertEqual((3, 2), self.counts(bridge)["Çok"])
        # permanent delete of an archived one
        gone = next(r.id for r in store.list_conversations(project_id=self.many.id, include_archived=True) if r.archived_at)
        bridge.previewDeleteConversation(gone)
        pump(60)
        self.assertTrue(bridge.confirmDeleteConversation(gone))
        pump(100)
        self.assertEqual((3, 1), self.counts(bridge)["Çok"])

    def test_e_attach_moves_a_count_from_global_to_the_project(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        say(store, bridge.conversationId, "tezgah ölçüleri")
        bridge.previewAttach(self.empty.id, "")
        pump(60)
        self.assertTrue(bridge.confirmAttach(self.empty.id, ""), bridge.attachNotice)
        pump(100)
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
            bridge.openProjectScope(self.many.id)
            settled(100, lambda: (  # opening a scope refreshes once, too
                self.assertEqual(1, sum(1 for c in spy.call_args_list if c.kwargs == {"include_archived": True})),
            ))
            spy.reset_mock()
            bridge.projectRecords
            bridge.scopeConversations           # reading the properties reads no store
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
# F-ee2: the home frame's reflow (Main.qml) is at rest only when its own animated values
# reached their targets - the rail margin and the ONE column span - evaluated in Main's
# context with the product's own formulas, so the test never restates them.
REFLOW_AT_REST = ("content.anchors.leftMargin === (content.command ? Theme.margin : Theme.railWidth(root.width))"
                  " && Math.abs(content.columnSpan - (content.showColumn ? content.columnWidth + content.columnGap : 0)) < 0.5")
STILL_SPAN = 0.25          # s: the longest reflow (Theme.reflow / focusMs, 220 ms) plus one frame
SETTLE_FLOOR_MS = 15000    # a heavily loaded machine may take this long; the ceiling still fails loudly


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

    def press(self, root, name, until=None, then=None) -> None:
        item = self.item(root, name)
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())
        if until is not None:                       # S0b: the state the caller asserts next; the old wait is the ceiling
            wait_until(until, 350)
        elif then is not None:                      # THIZ: the caller's assertions; one that holds at once keeps the old wait
            settled(350, then)
        else:
            pump(350)

    def reflow_at_rest(self, root) -> bool:
        content = self.item(root, "panelColumn").parentItem()
        value, undefined = QQmlExpression(QQmlEngine.contextForObject(content), content, REFLOW_AT_REST).evaluate()
        self.assertFalse(undefined, REFLOW_AT_REST)
        return value is True

    def resize_still(self, root, width: int, height: int, ceiling_ms: int, every=None) -> bool:
        """Resize, then wait until the home frame (sidebar, header, composer, column) is still.
        F-ee2: still means the sampled geometry stayed the same for at least STILL_SPAN (the
        longest reflow plus a frame) AND the reflow's own animated values are at their
        targets - a few equal samples of a frame that stalled under load are not still. The
        ceiling is the caller's, at least SETTLE_FLOOR_MS, and running out of it fails the
        test. ``every`` (F-ee) runs its assertions on each sampled frame of the reflow."""
        root.resize(width, height)
        if every is None and not self.wants_ambient():     # THIZ: motion-free, nothing animates after the drawn frame
            drawn_at_rest(self, root, width, height, ceiling_ms)
            return True
        names = ("sidebar", "chatHeader", "canonicalComposer", "panelColumn")
        last = {"sample": None, "since": 0.0}

        def still() -> bool:
            if every is not None:
                every()
            now = time.monotonic()
            sample = (root.width(), root.height(), tuple(self.rect(i) for n in names for i in self.all_items(root, n)))
            if sample != last["sample"]:
                last["sample"], last["since"] = sample, now
                return False
            return now - last["since"] >= STILL_SPAN and self.reflow_at_rest(root)
        ceiling = max(ceiling_ms, SETTLE_FLOOR_MS)
        self.assertTrue(wait_until(still, ceiling, 20), f"the reflow to {width}x{height} did not settle in {ceiling} ms")
        return True

    def closed(self, popup, ceiling_ms: int) -> None:
        """Close a popup; wait until it is gone (its exit transition ended); the old wait is the ceiling."""
        QMetaObject.invokeMethod(popup, "close")
        wait_until(lambda: not popup.property("visible"), ceiling_ms)

    def home(self, root, bridge, ceiling_ms: int) -> None:
        bridge.setPage("home")
        wait_until(lambda: root.property("loadedPage") == "home", ceiling_ms)


class SidebarShellTests(SidebarShellCase):
    @ambient_on          # THIZ: the sidebar's one sweep with motion
    def test_h_sidebar_tiles_and_strip_fit_at_every_size_in_space(self) -> None:
        self.sweep_h(("space",))

    @ambient_on          # THIZ: the sidebar's one sweep with motion
    def test_h_sidebar_tiles_and_strip_fit_at_every_size_in_dark(self) -> None:
        self.sweep_h(("dark",))

    def sweep_h(self, themes) -> None:
        # THIZ: one theme per test method, so the parallel runner can spread them.
        bridge = self.bridge()
        root = self.window(bridge)
        for theme in themes:
            bridge.setTheme(theme)
            for width, height in SIZES:
                self.resize_still(root, width, height, 700)
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
            self.resize_still(root, width, height, 700)
            with self.subTest(size=(width, height)):
                stack = self.box(root, "panelColumn")
                self.assertEqual(column[(width, height)], (round(stack.x()), round(stack.y()), round(stack.width())))
                side = self.box(root, "sidebar")
                # Where the clock card started (Q7: the TopBar keeps its height): 84 / 84 / 134
                # (F4: the scope group left the bar, so nothing pushes it lower).
                clock_line = max(84, round(height * 0.1980) + 76 - 12 - 144)
                self.assertEqual({(1100, 700): 84, (1280, 760): 84, (1920, 1080): 134}[(width, height)], clock_line)
                self.assertAlmostEqual(clock_line, side.top(), delta=0.5)
                for name in ("brandWordmark", "brandTagline", "settingsOrb", "jarvisCore", "commandStrip"):
                    self.apart(side, self.box(root, name), name)
                self.assertAlmostEqual(height - 64, side.bottom(), delta=0.5)
        # Home only (Q11): every other page keeps its own width.
        for page in ("history", "projects", "tasks"):
            bridge.setPage(page)
            settled(500, lambda: (
                self.assertFalse(self.item(root, "sidebar").isVisible(), page),
            ))
        bridge.setPage("home")
        settled(500, lambda: (
            self.assertTrue(self.item(root, "sidebar").isVisible()),
            self.assertEqual(0, self.execute_calls()),
        ))

    def test_j_a_hidden_tile_reflows_the_grid_at_every_size(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for width, height in SIZES:
            self.resize_still(root, width, height, 600)
            tile, gap = (86, 8) if width >= 1600 else (74, 6)
            with self.subTest(size=(width, height)):
                nav_full = self.box(root, "sidebarNav").height()
                self.assertAlmostEqual(2 * tile + gap, self.box(root, "summaryRail").height(), delta=0.5)
                bridge.setCardVisible("rail", "plan", False)
                pump(300)      # 3: two, then one across
                grid = self.box(root, "summaryRail")
                self.assertAlmostEqual(grid.width(), self.box(root, "railMemory").width(), delta=0.5)
                self.assertAlmostEqual(2 * tile + gap, grid.height(), delta=0.5)
                bridge.setCardVisible("rail", "focus", False)
                settled(300, lambda: (  # 2: one row
                    self.assertAlmostEqual(tile, self.box(root, "summaryRail").height(), delta=0.5),
                    self.assertAlmostEqual(self.box(root, "railTasks").top(), self.box(root, "railMemory").top(), delta=0.5),
                    self.assertGreater(self.box(root, "sidebarNav").height(), nav_full),   # the navigation takes the room
                ))
                bridge.setCardVisible("rail", "tasks", False)
                settled(300, lambda: (  # 1: across
                    self.assertAlmostEqual(self.box(root, "summaryRail").width(), self.box(root, "railMemory").width(), delta=0.5),
                ))
                bridge.setCardVisible("rail", "memory", False)
                settled(300, lambda: (  # none: no grid
                    self.assertFalse(self.item(root, "summaryRail").isVisible()),
                ))
                for key in ("tasks", "focus", "plan", "memory"):
                    bridge.setCardVisible("rail", key, True)
                pump(300)
                self.assertEqual(TILES, tuple(c["id"].capitalize() for c in bridge.railCards if c["visible"] and c["id"] != "updates"))
        self.assertEqual(0, self.execute_calls())

    def test_k_the_peek_opens_beside_its_tile_inside_the_window(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for width, height in SIZES:
            self.resize_still(root, width, height, 600)
            for key in ("Tasks", "Focus", "Plan"):
                with self.subTest(size=(width, height), tile=key):
                    self.press(root, "rail" + key, then=lambda: (
                        self.assertEqual((True, key), (root.property("peekOpen"), root.property("peekKey"))),
                    ))
                    peek, side, tile = self.box(root, "summaryPeek"), self.box(root, "sidebar"), self.box(root, "rail" + key)
                    self.inside(QRectF(0, 0, width, height), peek, "peek")
                    self.assertAlmostEqual(side.right() + 12, peek.left(), delta=0.5)
                    self.assertLessEqual(peek.top(), tile.center().y())
                    self.assertGreaterEqual(peek.bottom(), tile.center().y())
                    self.press(root, "rail" + key, then=lambda: (
                        self.assertFalse(root.property("peekOpen")),
                    ))
            # Memory is read, not pressed: no peek.
            self.press(root, "railMemory", then=lambda: (
                self.assertFalse(root.property("peekOpen")),
            ))
        self.assertEqual(0, self.execute_calls())

    def test_l_edit_mode_keeps_a_left_section_for_the_four_tiles(self) -> None:
        bridge = self.bridge()
        bridge.setCardVisible("rail", "updates", True)        # a saved layout that shows Updates: still no tile (Q4)
        root = self.window(bridge)
        settled(500, lambda: (
            self.assertEqual([], self.all_items(root, "railUpdates")),
        ))
        bridge.setEditMode(True)
        pump(300)
        for key in TILES:
            self.assertTrue(self.item(root, "railMenu" + key).isVisible(), key)
            # The menu sits on the number's line: the title keeps its room.
            self.assertFalse(self.item(root, "railTitle" + key).property("truncated"), key)
        self.press(root, "railMenuFocus")
        menus = self.item(root, "layoutMenus")
        self.assertEqual(("card", "rail", "focus"), tuple(menus.property(k) for k in ("mode", "region", "card")))
        self.press(root, "menuHide", then=lambda: (
            self.assertEqual([], [i for i in self.all_items(root, "railFocus") if i.isVisible()]),
        ))
        self.press(root, "layoutCards", then=lambda: (
            # Only the tiles are offered on the left; Updates is not one.
            self.assertTrue(self.item(root, "addToLayout_focus").isVisible()),
            self.assertEqual([], self.all_items(root, "addToLayout_updates")),
        ))
        self.press(root, "addToLayout_focus", then=lambda: (
            self.assertTrue(self.item(root, "railFocus").isVisible()),
        ))
        bridge.setEditMode(False)
        settled(300, lambda: (
            self.assertEqual(0, self.execute_calls()),
        ))


class TileContentCase(SidebarShellCase):
    def text(self, root, name) -> str:
        return str(self.item(root, name).property("text"))

    def tile(self, root, key) -> tuple:
        """(value, line, tip) as the tile shows them."""
        return (self.text(root, "railCount" + key), self.text(root, "railLine" + key),
                str(self.item(root, "railTip" + key).property("text")))

    def scoped(self):
        """A project with an instruction, two active files (one more retired), three
        conversations and two memory records; Global with one conversation."""
        from jarvis.contracts import DataClassification, ProjectMemoryItem, ProjectMemoryKind
        store = self.db()
        project = store.create_project("Ev tadilatı")
        store.save_explicit_user_project_instruction(project.id, "Yanıtları Türkçe ver.", "internal")
        conversations = [store.create_conversation(project.id, title=f"Sohbet {n}") for n in range(3)]
        for n in range(2):
            store.save_memory_item(ProjectMemoryItem(project_id=project.id, kind=ProjectMemoryKind.FACT, text=f"Kayıt {n}",
                                                     classification=DataClassification.INTERNAL))
        global_one = store.create_conversation(title="Genel sohbet")
        return store, project, conversations, global_one

    def files(self, bridge, project_id, names) -> None:
        import tempfile
        from pathlib import Path
        folder = Path(tempfile.mkdtemp(prefix="jarvis-6f-files-"))
        for name in names:
            (folder / name).write_text(f"{name}\n" + "satır\n" * 20, encoding="utf-8")      # distinct: one active copy per content
            bridge.backend.project_file("add", project_id, path=str(folder / name), classification="internal")


class TileContentTests(TileContentCase):
    def test_m_tasks_shows_the_running_task_a_waiting_approval_the_last_task_or_nothing(self) -> None:
        import time
        from tests.test_ui import task_proposal
        bridge = self.bridge(task_proposal("code_analysis"))
        root = self.window(bridge)
        pump(400)
        # Nothing yet.
        self.assertEqual(("—", "No task yet"), self.tile(root, "Tasks")[:2])
        # A waiting approval: amber, and the click goes to the approval (no peek).
        bridge.submitText("Bu projeyi incele.")
        settled(400, lambda: (
            self.assertTrue(bridge.approvalPending),
        ))
        value, line, tip = self.tile(root, "Tasks")
        self.assertEqual(("1", "approval waiting"), (value, line))
        self.assertEqual(QColor("#ffb347").name(), QColor(self.item(root, "railCountTasks").property("color")).name())   # amber
        self.assertEqual(QColor("#ffb347").name(), QColor(self.item(root, "railLineTasks").property("color")).name())
        feed = self.item(root, "conversationFeed")
        feed.setProperty("followEnd", False)
        self.press(root, "railTasks", then=lambda: (
            self.assertFalse(root.property("peekOpen")),
            self.assertTrue(feed.property("followEnd")),                     # brought to the end, where the approval is
            self.assertTrue(self.item(root, "safetyCard_approval").isVisible()),
        ))
        # Approved and finished: the last task, its result, name and time.
        bridge.approve()
        settled(500, lambda: (
            self.assertFalse(bridge.approvalPending),
        ))
        value, line, tip = self.tile(root, "Tasks")
        status = bridge.task["status"]
        self.assertIn(status, ("completed", "failed", "rejected", "cancelled"))
        self.assertTrue(line.startswith(bridge.task["title"]), line)
        self.assertRegex(tip, r" · \d\d:\d\d$")
        # Running: its running time and its name.
        bridge._session_id = bridge._session_id or "s-1"
        bridge._busy = True
        bridge._task = {**bridge._task, "status": "running", "title": "Rapor özeti"}
        bridge._work_started_at = time.time() - 75
        bridge.stateChanged.emit()
        pump(1300)
        value, line, tip = self.tile(root, "Tasks")
        self.assertRegex(value, r"^1:1\d$")
        self.assertEqual("Rapor özeti", line)
        bridge._busy = False

    def test_n_tasks_after_a_restart_reads_the_newest_sessions_latest_task_once(self) -> None:
        bridge = self.bridge()
        calls = []
        bridge.backend.sessions = lambda: {"sessions": [
            {"session_id": "s-new", "updated_at": "2026-10-04T08:00:00+00:00", "readable": True, "latest_task_id": "t-9",
             "task_count": 2, "active_task_id": ""},
            {"session_id": "s-old", "updated_at": "2026-10-01T08:00:00+00:00", "readable": True, "latest_task_id": "t-1",
             "task_count": 1, "active_task_id": ""}]}

        def tasks(session_id):
            calls.append(session_id)
            return {"tasks": [{"task_id": "t-9", "session_id": session_id, "status": "failed",
                               "updated_at": "2026-10-04T08:00:00+00:00", "readable": True}]}
        bridge.backend.tasks = tasks
        bridge.refreshHistory()
        self.assertEqual(["s-new"], calls)                              # one listing, the newest session's
        newest = bridge.sessions[0]
        self.assertEqual(("failed", "2026-10-04T08:00:00+00:00"), (newest["latest_status"], newest["latest_updated_at"]))
        self.assertNotIn("latest_status", bridge.sessions[1])
        root = self.window(bridge)
        pump(400)
        value, line, tip = self.tile(root, "Tasks")
        self.assertEqual("Failed", value)
        self.assertTrue(tip.startswith("Failed · "), tip)

    def test_o_focus_says_what_the_model_sees_in_genel_and_in_a_project(self) -> None:
        store, project, conversations, global_one = self.scoped()
        bridge = self.bridge()
        self.files(bridge, project.id, ("a.md", "b.md", "c.md"))
        retired = store.list_project_files(project.id)[-1]["id"]
        bridge.backend.project_file("retire", project.id, file_id=retired)
        bridge.refreshProjects()
        root = self.window(bridge)
        bridge.openConversation(global_one.id)
        pump(400)
        self.assertEqual(("Global", "no project context"), self.tile(root, "Focus")[:2])
        bridge.openConversation(conversations[0].id)
        pump(400)
        facts = bridge.conversationProject
        self.assertEqual((True, 2), (facts["instruction"], facts["file_count"]))
        value, line, tip = self.tile(root, "Focus")
        self.assertEqual("Ev tadilatı", value)
        self.assertEqual("Ev tadilatı · instruction ✓ · 2 files · 3 conversations", tip)
        self.assertIn(line, ("instruction ✓ · 2 files · 3 conversations", "2 files · 3 conversations", "3 conversations"))
        for width, height in SIZES:
            self.resize_still(root, width, height, 500)
            with self.subTest(size=(width, height)):
                self.assertFalse(self.item(root, "railLineFocus").property("truncated"))

    def test_p_plan_shows_the_next_job_in_genel_and_in_a_project(self) -> None:
        from datetime import datetime, timedelta
        store, project, conversations, global_one = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        from PySide6.QtQml import QQmlProperty

        now = datetime.now().astimezone()                                       # TCLOCK: once per test
        # The rail's own slow clock (`now: Date.now()`) is pinned to it, so "tomorrow" cannot move under a midnight run.
        self.assertTrue(QQmlProperty(self.item(root, "summaryRail"), "now").write(now.timestamp() * 1000))
        tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        later = tomorrow + timedelta(days=3)
        for owner in (global_one.id, conversations[0].id):
            bridge.openConversation(owner)
            pump(300)
            with self.subTest(scope=owner):
                bridge._jobs = []
                bridge.dataChanged.emit()
                pump(200)
                self.assertEqual(("—", "No scheduled job"), self.tile(root, "Plan")[:2])
                bridge._jobs = [{"id": "j-2", "label": "Yedek", "enabled": True, "next_due_at_utc": later.isoformat()},
                                {"id": "j-1", "label": "Haftalık rapor", "enabled": True, "next_due_at_utc": tomorrow.isoformat()},
                                {"id": "j-3", "label": "Kapalı", "enabled": False, "next_due_at_utc": tomorrow.isoformat()}]
                bridge.dataChanged.emit()
                pump(200)
                value, line, tip = self.tile(root, "Plan")
                self.assertEqual("09:00", value)
                self.assertIn(line, ("Haftalık rapor · tomorrow", "Haftalık rapor"))
                self.assertEqual("Haftalık rapor · tomorrow 09:00", tip)

    def test_q_memory_shows_the_status_and_this_scopes_records(self) -> None:
        store, project, conversations, global_one = self.scoped()
        bridge = self.bridge()
        bridge.refreshConfig()
        root = self.window(bridge)
        bridge.openConversation(conversations[0].id)
        settled(400, lambda: (
            self.assertEqual(2, bridge.conversationProject["memory_count"]),
        ))
        value, line, tip = self.tile(root, "Memory")
        self.assertEqual(("Available", "2 records"), (value, line))
        bridge.openConversation(global_one.id)
        pump(400)
        value, line, tip = self.tile(root, "Memory")
        self.assertEqual("Available", value)
        self.assertRegex(line, r"^last record \d\d:\d\d$")
        self.assertFalse(self.item(root, "railMemory").property("activeFocusOnTab"))      # read, not pressed

    def test_r_reading_the_scope_facts_writes_nothing(self) -> None:
        store, project, conversations, global_one = self.scoped()
        bridge = self.bridge()
        bridge.openConversation(conversations[0].id)
        before = dump(self.database)
        for _ in range(2):
            bridge._refresh_conversations()
            bridge.conversationProject, bridge.sessions
        bridge.refreshHistory()
        self.assertEqual(before, dump(self.database))

    def test_s_the_new_tile_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        for source, turkish in (("1 approval waiting", "1 onay bekliyor"), ("No task yet", "Henüz görev yok"),
                                ("no project context", "proje bağlamı yok"), ("instruction ✓", "talimat ✓"),
                                ("%1 files", "%1 dosya"), ("%1 conversations", "%1 sohbet"), ("tomorrow", "yarın"),
                                ("No scheduled job", "Zamanlanmış iş yok"), ("%1 records", "%1 kayıt"),
                                ("last record %1", "son kayıt %1")):
            self.assertEqual(turkish, tx("SummaryRail", source))


LONG_PROJECT = "İstanbul Beşiktaş daire tadilatı, taşınma planlaması ve kira sözleşmesi takibi 2026"
LONG_TITLE = "Kat malikleri toplantısı için hazırlanacak belgeler, aidat hesapları ve tadilat izinleri"


class SidebarNavCase(SidebarShellCase, SidebarDataCase):
    def shown(self, root, name) -> list:
        return [i for i in self.all_items(root, name) if i.isVisible()]

    def tap(self, root, item, button=Qt.LeftButton, until=None, then=None) -> None:
        last = None
        for _ in range(60):                         # the work surface may still be reflowing (it animates its width)
            now = self.rect(item)
            if now == last:
                break
            last = now
            pump(40)
        QTest.mouseClick(root, button, Qt.NoModifier, item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())
        if until is not None:                       # S0b: the state the caller asserts next; the old wait is the ceiling
            wait_until(until, 350)
        elif then is not None:                      # THIZ: the caller's assertions; one that holds at once keeps the old wait
            settled(350, then)
        else:
            pump(350)

    def row(self, root, name, title):
        found = [i for i in self.shown(root, name) if i.property("title") == title]
        self.assertEqual(1, len(found), (name, title, [i.property("title") for i in self.shown(root, name)]))
        return found[0]

    def titles(self, root) -> list:
        return [i.property("title") for i in self.shown(root, "sidebarConversation")]

    def child(self, item, name):
        found = [c for c in item.childItems() if c.objectName() == name]
        return found[0] if found else None

    def link(self, root, name) -> str:
        return str(self.item(root, name).property("count"))

    def editing(self, root) -> bool:
        return any(i.isVisible() for i in self.all_items(root, "conversationRenameField"))

    def stamp(self, conversation_id: str, when) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                               (when.astimezone(timezone.utc).isoformat(), conversation_id))

    def fixed_today(self, root, now) -> None:
        """THIZ2: "today" from ``now``, computed once by the test, in the window's sidebar (its clock binding is
        replaced) and in the bridge's date groups (its ``datetime.now``), so a run across midnight groups the
        conversations as it began. Test side only."""
        from datetime import datetime

        from PySide6.QtCore import QDateTime
        from PySide6.QtQml import QQmlProperty

        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz)

        # A QQmlProperty write drops the `now: clock.now` binding (setProperty would be undone by the next tick).
        self.assertTrue(QQmlProperty(self.item(root, "sidebar"), "now").write(QDateTime.fromMSecsSinceEpoch(round(now.timestamp() * 1000))))
        patcher = mock.patch("jarvis.ui.bridge.datetime", Fixed)
        patcher.start()
        self.addCleanup(patcher.stop)


class SidebarNavTests(SidebarNavCase):
    def test_t_whole_rows_no_clip_no_overlap_and_long_names_elide_with_a_tip_in_space(self) -> None:
        self.sweep_t(("space",))

    def test_t_whole_rows_no_clip_no_overlap_and_long_names_elide_with_a_tip_in_dark(self) -> None:
        self.sweep_t(("dark",))

    def sweep_t(self, themes) -> None:
        # THIZ: one theme per test method, so the parallel runner can spread them.
        store = self.seed()
        extra = [store.create_project(f"Proje {n}") for n in range(3)]
        longp = store.create_project(LONG_PROJECT)
        for n in range(4):
            store.create_conversation(longp.id, title=f"Kısa {n}")
        owned = store.create_conversation(longp.id, title=LONG_TITLE)
        bridge = self.bridge()
        bridge.openConversation(owned.id)
        root = self.window(bridge)
        active = 3 + len(extra) + 1
        for theme in themes:
            bridge.setTheme(theme)
            for width, height in SIZES:
                self.resize_still(root, width, height, 700)
                with self.subTest(theme=theme, size=(width, height)):
                    side, nav = self.box(root, "sidebar"), self.box(root, "sidebarNav")
                    self.inside(side, nav, "nav")
                    tiles = [self.box(root, "rail" + key) for key in TILES]
                    plist, clist = self.box(root, "sidebarProjectList"), self.box(root, "sidebarConversationList")
                    for name in ("sidebarGlobal", "sidebarProjectsLink", "sidebarNewProject", "sidebarScopeChip",
                                 "sidebarNewConversation"):
                        box = self.box(root, name)
                        self.inside(nav, box, name)
                        for tile in tiles:
                            self.apart(box, tile, name)
                    for box, what in ((plist, "projects"), (clist, "conversations")):
                        self.inside(nav, box, what)
                        for tile in tiles:
                            self.apart(box, tile, what)
                    # Whole rows: a row the list shows is wholly inside it.
                    shown = {}
                    for name, box in (("sidebarProject", plist), ("sidebarConversation", clist)):
                        inside = 0
                        for item in self.shown(root, name):
                            r = self.rect(item)
                            cut = r.intersected(box)
                            if cut.height() > 0.5:
                                self.assertAlmostEqual(r.height(), cut.height(), delta=0.5, msg=name)
                                inside += 1
                        shown[name] = inside
                    # 5 at the 1920 class, 3 below.
                    self.assertEqual(min(5 if width >= 1600 else 3, active), shown["sidebarProject"])
                    self.assertGreaterEqual(shown["sidebarConversation"], 2)
                    # Names elide (never under 6 characters) and the tip has them whole; counts and times never elide.
                    for name, title_name, meta_name in (("sidebarGlobal", "sidebarGlobalName", "sidebarGlobalCount"),
                                                        ("sidebarProject", "sidebarProjectName", "sidebarProjectCount"),
                                                        ("sidebarConversation", "sidebarConversationTitle", "sidebarConversationTime")):
                        for row in self.shown(root, name):
                            title, meta = self.child(row, title_name), self.child(row, meta_name)
                            for text in (title, meta):
                                pos = text.mapToItem(row, QPointF(0, 0))
                                self.assertGreaterEqual(pos.x(), 0, name)
                                self.assertLessEqual(pos.x() + text.property("contentWidth"), row.width() + 0.5, name)
                                self.assertEqual(1, text.property("lineCount"), name)
                            self.assertFalse(meta.property("truncated"), name)
                            self.assertLessEqual(title.mapToItem(row, QPointF(title.width(), 0)).x(),
                                                 meta.mapToItem(row, QPointF(0, 0)).x(), name)
                            tip = self.child(row, "sidebarRowTip")
                            self.assertEqual(row.property("title"), tip.property("text"))
                            if title.property("truncated"):
                                six = QFontMetricsF(title.property("font")).horizontalAdvance(row.property("title")[:6] + "…")
                                self.assertGreaterEqual(title.width() + 0.5, six, name)
                                self.assertTrue(tip.property("active"), name)
                    self.assertTrue(self.row(root, "sidebarProject", LONG_PROJECT).property("current"))
                    self.assertTrue(self.child(self.row(root, "sidebarProject", LONG_PROJECT), "sidebarProjectName").property("truncated"))
                    self.assertTrue(self.child(self.row(root, "sidebarConversation", LONG_TITLE), "sidebarConversationTitle").property("truncated"))
                    chip, chip_name = self.box(root, "sidebarScopeChip"), self.item(root, "sidebarScopeName")
                    self.inside(chip, self.rect(chip_name), "chip name")
                    self.assertEqual(LONG_PROJECT, self.item(root, "sidebarScopeTip").property("text"))
                    self.apart(chip, self.box(root, "sidebarNewConversation"), "chip / +")
        self.assertEqual(0, self.execute_calls())

    def test_u_grouping_by_the_local_day_of_the_last_change(self) -> None:
        from datetime import datetime, timedelta
        store = self.db()
        now = datetime.now().astimezone()                                       # THIZ2: once per test
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cases = (("Bugün ilk", midnight + timedelta(seconds=30)),                # just after midnight: today
                 ("Dün son", midnight - timedelta(seconds=30)),                  # just before it: yesterday
                 ("Dün ilk", midnight - timedelta(days=1) + timedelta(seconds=30)),
                 ("Daha eski", midnight - timedelta(days=1, seconds=30)))        # the day before: earlier
        for title, when in cases:
            self.stamp(store.create_conversation(title=title).id, when)
        bridge = self.bridge()
        root = self.window(bridge)
        self.fixed_today(root, now)
        bridge._refresh_conversations()
        pump(300)
        # F6: Daha eski starts collapsed; its heading opens it.
        self.tap(root, next(h for h in self.shown(root, "sidebarGroupHead") if h.property("key") == "earlier"))
        groups = {}
        for row in self.shown(root, "sidebarConversation"):
            groups.setdefault(row.parentItem().property("key"), []).append(row.property("title"))
        self.assertEqual({"today": ["Bugün ilk"], "yesterday": ["Dün son", "Dün ilk"], "earlier": ["Daha eski"]}, groups)
        self.assertEqual(["Today", "Yesterday", "Earlier"], self.texts(root, "sidebarGroupLabel"))
        times = {row.property("title"): row.property("meta") for row in self.shown(root, "sidebarConversation")}
        self.assertEqual(("00:00", "23:59", "00:00"), (times["Bugün ilk"], times["Dün son"], times["Dün ilk"]))
        older = cases[3][1]
        self.assertEqual(f"{older.day} {older.strftime('%b')}", times["Daha eski"])
        # Empty groups are not shown.
        store.archive_conversation(next(r.id for r in store.list_conversations(project_id=None) if r.title == "Daha eski"))
        bridge._refresh_conversations()
        settled(300, lambda: (
            self.assertEqual(["Today", "Yesterday"], self.texts(root, "sidebarGroupLabel")),
        ))

    def test_v_a_scope_pick_is_the_6b_rule_and_a_row_pick_is_the_switcher_pick(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        # A project: its newest non-archived conversation.
        self.tap(root, self.row(root, "sidebarProject", "Çok"))
        newest = store.list_conversations(project_id=self.many.id)[0]
        self.assertEqual((newest.id, self.many.id), (bridge.conversationId, bridge.conversationProject["id"]))
        self.assertTrue(self.row(root, "sidebarProject", "Çok").property("current"))
        self.assertEqual(sorted(r.title for r in self.live), sorted(self.titles(root)))
        # An empty project: a new conversation in it.
        before = len(store.list_conversations(project_id=self.empty.id))
        self.tap(root, self.row(root, "sidebarProject", "Boş"))
        self.assertEqual((before + 1, self.empty.id), (len(store.list_conversations(project_id=self.empty.id)),
                                                       bridge.conversationProject["id"]))
        self.assertEqual(["New Chat"], self.titles(root))
        # Genel: the newest Global one.
        self.tap(root, self.item(root, "sidebarGlobal"))
        self.assertEqual(store.list_conversations(project_id=None)[0].id, bridge.conversationId)
        self.assertTrue(self.item(root, "sidebarGlobal").property("current"))
        # The archived project is not a row; openProjectScope refuses it anyway.
        self.assertNotIn("Eski", [i.property("title") for i in self.shown(root, "sidebarProject")])
        self.assertFalse(bridge.openProjectScope(self.old.id))
        # A row pick: that conversation opens and is marked (edge + fill); the switcher names it.
        other = self.row(root, "sidebarConversation", "Genel 0")
        self.assertFalse(other.property("current"))
        self.tap(root, other)
        picked = self.row(root, "sidebarConversation", "Genel 0")
        self.assertEqual(self.global_live[0].id, bridge.conversationId)
        self.assertTrue(picked.property("current"))
        self.assertEqual(1.0, self.child(picked, "sidebarRowEdge").property("opacity"))
        self.assertEqual(1, sum(1 for r in self.shown(root, "sidebarConversation") if r.property("current")))
        self.assertEqual("Genel 0", self.item(root, "chatHeaderTitle").property("text"))
        # The busy guard: no pick while a run is busy.
        bridge._busy = True
        try:
            self.tap(root, self.row(root, "sidebarConversation", "Genel 1"))
            self.tap(root, self.row(root, "sidebarProject", "Tek"), then=lambda: (
                self.assertEqual(self.global_live[0].id, bridge.conversationId),
            ))
        finally:
            bridge._busy = False
        self.assertEqual(0, self.execute_calls())

    def test_w_the_round_plus_opens_a_new_conversation_in_the_selected_scope(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        plus = self.item(root, "sidebarNewConversation")
        self.assertEqual("New conversation in Global", plus.property("tip"))
        count = len(store.list_conversations(project_id=None))
        self.tap(root, plus)
        self.assertEqual(count + 1, len(store.list_conversations(project_id=None)))
        self.assertTrue(bridge.conversationProject["global"])
        self.assertTrue(self.row(root, "sidebarConversation", "New Chat").property("current"))
        self.tap(root, self.row(root, "sidebarProject", "Tek"), then=lambda: (
            self.assertEqual("New conversation in Tek", plus.property("tip")),
        ))
        self.tap(root, plus)
        self.assertEqual((self.one.id, 2), (bridge.conversationProject["id"], len(store.list_conversations(project_id=self.one.id))))
        # Under an archived project: dimmed, the reason in its tip, and a click does nothing.
        bridge.openConversation(store.list_conversations(project_id=self.old.id)[0].id)
        settled(300, lambda: (
            self.assertFalse(plus.property("allowed")),
            self.assertAlmostEqual(0.38, plus.property("opacity"), places=2),
            self.assertEqual("The project is archived: unarchive it to start a new conversation.", plus.property("tip")),
        ))
        self.tap(root, plus)
        self.assertEqual(1, len(store.list_conversations(project_id=self.old.id)))
        self.assertTrue(self.item(root, "sidebarScopeChip").property("archived"))
        self.assertEqual("Eski · archived", self.item(root, "sidebarScopeTip").property("text"))
        # While busy: the same.
        self.tap(root, self.item(root, "sidebarGlobal"))
        bridge._busy = True
        try:
            bridge.stateChanged.emit()
            settled(100, lambda: (
                self.assertEqual("Wait for the current run to finish.", plus.property("tip")),
            ))
            self.tap(root, plus)
            self.assertEqual(count + 1, len(store.list_conversations(project_id=None)))
        finally:
            bridge._busy = False
        self.assertEqual(0, self.execute_calls())

    def test_x_the_counts_follow_archive_unarchive_attach_and_delete(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        count = lambda title: self.child(self.row(root, "sidebarProject", title), "sidebarProjectCount").property("text")  # noqa: E731
        genel = lambda: self.item(root, "sidebarGlobalCount").property("text")  # noqa: E731
        self.assertEqual(("–", "1", "3"), (count("Boş"), count("Tek"), count("Çok")))
        self.assertEqual(str(len(bridge.conversations)), genel())
        self.assertEqual("1", self.link(root, "sidebarArchivedProjects"))
        self.assertEqual("1", self.link(root, "sidebarArchive"))                       # Genel: History's archived
        self.tap(root, self.row(root, "sidebarProject", "Çok"), then=lambda: (
            self.assertEqual("2", self.link(root, "sidebarArchive")),
        ))
        # archive / unarchive
        victim = next(r for r in self.live if r.id != bridge.conversationId)
        self.assertTrue(bridge.archiveConversation(victim.id))
        pump(300)
        self.assertEqual(("2", "3"), (count("Çok"), self.link(root, "sidebarArchive")))
        self.assertNotIn(victim.title, self.titles(root))
        self.assertTrue(bridge.unarchiveConversation(victim.id))
        settled(300, lambda: (
            self.assertEqual(("3", "2"), (count("Çok"), self.link(root, "sidebarArchive"))),
        ))
        # permanent delete of an archived one
        gone = next(r.id for r in store.list_conversations(project_id=self.many.id, include_archived=True) if r.archived_at)
        bridge.previewDeleteConversation(gone)
        pump(60)
        self.assertTrue(bridge.confirmDeleteConversation(gone))
        pump(300)
        bridge.setPage("home")
        settled(400, lambda: (
            self.assertEqual("1", self.link(root, "sidebarArchive")),
        ))
        # attach: a Global conversation moves to Boş
        self.tap(root, self.item(root, "sidebarGlobal"))
        before = genel()
        say(store, bridge.conversationId, "tezgah ölçüleri")
        bridge.previewAttach(self.empty.id, "")
        pump(60)
        self.assertTrue(bridge.confirmAttach(self.empty.id, ""), bridge.attachNotice)
        pump(300)
        bridge.setPage("home")
        pump(400)
        self.assertEqual((str(int(before) - 1), "1"), (genel(), count("Boş")))
        # a project archived: one more in the archived link, its row gone
        bridge.archiveProject(self.one.id)
        pump(400)
        bridge.setPage("home")
        settled(400, lambda: (
            self.assertEqual("2", self.link(root, "sidebarArchivedProjects")),
            self.assertNotIn("Tek", [i.property("title") for i in self.shown(root, "sidebarProject")]),
            self.assertEqual(0, self.execute_calls()),
        ))

    def test_y_a_right_click_acts_on_that_row_and_does_not_open_it(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.tap(root, self.row(root, "sidebarProject", "Çok"))
        opened = bridge.conversationId
        target = next(r for r in self.live if r.id != opened)
        calls = []
        real = bridge.backend.conversation_rename
        bridge.backend.conversation_rename = lambda cid, title: (calls.append((cid, title)), real(cid, title))[1]
        self.tap(root, self.row(root, "sidebarConversation", target.title), Qt.RightButton)
        menu = self.item(root, "conversationMenu")
        self.assertTrue(menu.property("open"))
        self.assertEqual((target.id, opened), (menu.property("targetId"), bridge.conversationId))       # not opened
        self.assertEqual(target.title, self.item(root, "conversationContextHeader").property("text"))
        self.assertTrue(self.item(root, "conversationRenameItem").isEnabled())
        self.assertFalse(self.item(root, "conversationDeleteItem").isEnabled())
        self.assertEqual("Archive it first", self.item(root, "conversationDeleteReason").property("text"))
        # Rename: prefilled with THAT row's title; the commit renames that row only.
        self.tap(root, self.item(root, "conversationRenameItem"), then=lambda: (
            self.assertTrue(self.editing(root)),
            self.assertEqual(target.title, self.item(root, "conversationRenameField").property("text")),
        ))
        self.item(root, "conversationRenameField").setProperty("text", "Satırdan yeni ad")
        pump(60)
        self.tap(root, self.item(root, "conversationRenameSave"), then=lambda: (
            self.assertEqual([(target.id, "Satırdan yeni ad")], calls),
            self.assertFalse(self.editing(root)),
            self.assertEqual(opened, bridge.conversationId),
            self.assertIn("Satırdan yeni ad", self.titles(root)),
        ))
        # Archive from the row: that one leaves the list; the open one stays open.
        self.tap(root, self.row(root, "sidebarConversation", "Satırdan yeni ad"), Qt.RightButton)
        self.tap(root, self.item(root, "conversationArchiveItem"))
        self.assertIsNotNone(store.load_conversation(target.id).archived_at)
        self.assertEqual(opened, bridge.conversationId)
        self.assertNotIn("Satırdan yeni ad", self.titles(root))
        self.assertFalse(menu.property("open"))
        # Under an archived project: rename / archive disabled with their reasons; delete
        # offered for that row, and the dialog is for that row.
        store.set_project_active(self.old.id, True)                    # a second one, made before the archive
        second = store.create_conversation(self.old.id, title="Eski ikinci")
        store.set_project_active(self.old.id, False)
        bridge.refreshProjects()
        bridge.openConversation(store.list_conversations(project_id=self.old.id)[-1].id)
        settled(300, lambda: (
            self.assertNotEqual(second.id, bridge.conversationId),
        ))
        self.tap(root, self.row(root, "sidebarConversation", "Eski ikinci"), Qt.RightButton, then=lambda: (
            self.assertFalse(self.item(root, "conversationRenameItem").isEnabled()),
            self.assertIn("archived", self.item(root, "conversationRenameReason").property("text")),
            self.assertFalse(self.item(root, "conversationArchiveItem").isEnabled()),
            self.assertTrue(self.item(root, "conversationDeleteItem").isEnabled()),
        ))
        self.tap(root, self.item(root, "conversationDeleteItem"))
        dialog = root.findChild(QObject, "conversationDeleteDialog")
        self.assertTrue(dialog.property("visible"))
        self.assertEqual(second.id, dialog.property("targetId"))
        self.assertNotEqual(second.id, bridge.conversationId)
        QMetaObject.invokeMethod(dialog, "close")
        pump(400)
        self.assertIsNotNone(store.load_conversation(second.id))
        self.assertEqual(0, self.execute_calls())

    def test_z_the_header_menu_works_through_conversation_menu(self) -> None:
        from pathlib import Path
        qml = Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "qml" / "components"
        self.assertNotIn("conversationSwitcherContext", (qml / "TopBar.qml").read_text(encoding="utf-8"))
        self.assertIn("conversationSwitcherContext", (qml / "ConversationMenu.qml").read_text(encoding="utf-8"))
        store = self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual(1, len(self.all_items(root, "conversationMenu")))                # one menu, two openers
        self.tap(root, self.item(root, "chatHeaderMenu"))
        menu = self.item(root, "conversationMenu")
        self.assertEqual(("", True), (menu.property("rowId"), menu.property("open")))
        self.assertTrue(self.item(root, "chatHeaderMenu").property("open"))
        self.assertEqual(self.item(root, "chatHeaderTitle").property("text"),
                         self.item(root, "conversationContextHeader").property("text"))
        # Under "…".
        button, header = self.box(root, "chatHeaderMenu"), self.box(root, "conversationContextHeader")
        self.assertGreater(header.top(), button.bottom())
        self.tap(root, self.item(root, "conversationRenameItem"))
        self.item(root, "conversationRenameField").setProperty("text", "Üstten ad")
        pump(60)
        self.tap(root, self.item(root, "conversationRenameSave"))
        self.assertEqual("Üstten ad", store.load_conversation(bridge.conversationId).title)
        self.assertEqual("Üstten ad", self.item(root, "chatHeaderTitle").property("text"))
        self.assertIn("Üstten ad", self.titles(root))
        self.assertEqual(0, self.execute_calls())

    def test_aa_the_links_and_the_new_project_plus(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual("All projects", self.item(root, "sidebarProjectsLink").property("tip"))
        for name in ("sidebarProjectsLink", "sidebarArchivedProjects"):
            self.tap(root, self.item(root, name), then=lambda: (
                self.assertEqual(("projects", {}), (bridge.page, bridge.openProject), name),
            ))
            bridge.setPage("home")
            pump(400)
        # Q9: the Projects page with its create field focused.
        self.tap(root, self.item(root, "sidebarNewProject"), then=lambda: (
            self.assertEqual("projects", bridge.page),
            self.assertTrue(self.item(root, "projectNewName").property("activeFocus")),
        ))
        bridge.setPage("home")
        pump(400)
        # Arşiv (n): Genel -> History; a project -> its detail.
        self.tap(root, self.item(root, "sidebarArchive"), then=lambda: (
            self.assertEqual("history", bridge.page),
        ))
        bridge.setPage("home")
        pump(400)
        self.tap(root, self.row(root, "sidebarProject", "Çok"))
        self.tap(root, self.item(root, "sidebarArchive"), then=lambda: (
            self.assertEqual(("projects", self.many.id), (bridge.page, bridge.openProject.get("id"))),
        ))
        bridge.setPage("home")
        pump(400)
        # No archived conversation: no link.
        self.tap(root, self.row(root, "sidebarProject", "Tek"), then=lambda: (
            self.assertFalse(self.item(root, "sidebarArchive").isVisible()),
        ))
        # Below 760 px of height the archived-projects link folds into the heading as "(n)".
        self.resize_still(root, 1100, 700, 600)
        self.assertFalse(self.item(root, "sidebarArchivedProjects").isVisible())
        folded = self.item(root, "sidebarArchivedProjectsCount")
        self.assertEqual(("(1)", "Archived projects"), (folded.property("count"), folded.property("tip")))
        self.inside(self.box(root, "sidebarNav"), self.rect(folded), "folded link")
        self.apart(self.rect(folded), self.box(root, "sidebarNewProject"), "folded / +")
        self.tap(root, folded, then=lambda: (
            self.assertEqual("projects", bridge.page),
            self.assertEqual(0, self.execute_calls()),
        ))

    def test_bb_every_navigation_action_has_a_mouse_path(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        keys = []
        real = QTest.keyClick
        with mock.patch.object(QTest, "keyClick", side_effect=lambda *a, **k: (keys.append(a), real(*a, **k))):
            self.tap(root, self.row(root, "sidebarProject", "Çok"), then=lambda: (  # scope pick
                self.assertEqual(self.many.id, bridge.conversationProject["id"]),
            ))
            other = next(t for t in self.titles(root) if not self.row(root, "sidebarConversation", t).property("current"))
            self.tap(root, self.row(root, "sidebarConversation", other), then=lambda: (  # conversation pick
                self.assertEqual(other, bridge.conversationTitle),
            ))
            self.tap(root, self.item(root, "sidebarNewConversation"), then=lambda: (  # new conversation
                self.assertEqual(("", self.many.id), (bridge.conversationTitle, bridge.conversationProject["id"])),
            ))
            self.tap(root, self.row(root, "sidebarConversation", other), Qt.RightButton)                 # rename
            self.tap(root, self.item(root, "conversationRenameItem"))
            self.item(root, "conversationRenameField").setProperty("text", other + " 2")
            pump(60)
            self.tap(root, self.item(root, "conversationRenameSave"))
            self.tap(root, self.row(root, "sidebarConversation", other + " 2"), Qt.RightButton)          # archive
            self.tap(root, self.item(root, "conversationArchiveItem"), then=lambda: (
                self.assertNotIn(other + " 2", self.titles(root)),
            ))
            self.tap(root, self.item(root, "sidebarGlobal"), then=lambda: (  # Genel
                self.assertTrue(bridge.conversationProject["global"]),
            ))
            self.tap(root, self.item(root, "railTasks"), then=lambda: (  # tiles / peek
                self.assertTrue(root.property("peekOpen")),
            ))
            self.tap(root, self.item(root, "railTasks"))
            self.tap(root, self.item(root, "sidebarArchive"), then=lambda: (  # Arşiv
                self.assertEqual("history", bridge.page),
            ))
            bridge.setPage("home")
            pump(400)
            self.tap(root, self.item(root, "sidebarNewProject"), then=lambda: (  # new project
                self.assertEqual("projects", bridge.page),
            ))
        self.assertEqual([], keys)
        self.assertEqual(0, self.execute_calls())

    def test_cc_the_keyboard_reaches_the_rows_with_a_ring(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        genel = self.item(root, "sidebarGlobal")
        genel.forceActiveFocus()
        settled(200, lambda: (
            self.assertTrue(self.child(genel, "sidebarRowFocusRing").isVisible()),
        ))
        QTest.keyClick(root, Qt.Key_Tab)
        settled(200, lambda: (
            self.assertTrue(self.item(root, "sidebarProjectsLink").property("activeFocus")),
        ))
        row = self.row(root, "sidebarProject", "Çok")
        row.forceActiveFocus()
        pump(200)
        QTest.keyClick(root, Qt.Key_Return)
        settled(400, lambda: (
            self.assertEqual(self.many.id, bridge.conversationProject["id"]),
        ))
        target = next(r for r in self.shown(root, "sidebarConversation") if not r.property("current"))
        target.forceActiveFocus()
        pump(200)
        QTest.keyClick(root, Qt.Key_Menu)
        pump(400)
        menu = self.item(root, "conversationMenu")
        self.assertEqual((True, target.property("itemId")), (menu.property("open"), menu.property("targetId")))
        QTest.keyClick(root, Qt.Key_Escape)
        settled(400, lambda: (
            self.assertFalse(menu.property("open")),
        ))

    def test_dd_the_new_navigation_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        for source, turkish in (("Global", "Genel"), ("PROJECTS", "PROJELER"), ("CONVERSATIONS", "SOHBETLER"),
                                ("All projects", "Tüm projeler"), ("New project", "Yeni proje"),
                                ("Archived projects", "Arşivlenmiş projeler"), ("Today", "Bugün"), ("Yesterday", "Dün"),
                                ("Earlier", "Daha eski"), ("New Chat", "Yeni Sohbet"),
                                ("No conversation in this scope yet. Press + to start one.",
                                 "Bu kapsamda henüz sohbet yok. Başlatmak için + düğmesine basın.")):
            self.assertEqual(turkish, tx("Sidebar", source))
        self.assertEqual("Evim içinde yeni sohbet", tx("Sidebar", "New conversation in %1").replace("%1", "Evim"))
        self.assertEqual("Arşiv", _APP.translate("Sidebar", "Archive", "sidebar link"))     # the noun
        self.assertEqual("Arşivle", tx("ConversationMenu", "Archive"))                      # the verb, unchanged
        for source, turkish in (("Rename", "Yeniden adlandır"), ("Delete permanently…", "Kalıcı sil…"),
                                ("Archive it first", "Önce arşivleyin")):
            self.assertEqual(turkish, tx("ConversationMenu", source))


BAR = ("brandWordmark", "brandTagline", "settingsOrb", "jarvisCore", "commandStrip", "coreStatusGroup", "editLayoutTrigger")


def attach_dialog(root):
    """The "Projeye bağla" dialog by its type (the composer's file picker shares its objectName)."""
    found = [d for d in root.findChildren(QObject, "attachDialog") if d.metaObject().className().startswith("AttachDialog")]
    assert len(found) == 1, [d.metaObject().className() for d in root.findChildren(QObject, "attachDialog")]
    return found[0]


class ChatHeaderTests(SidebarNavCase):
    def clear(self, root, box, name, item) -> None:
        """``box`` is clear of ``item``; the core is its machine - the outer ring, 0.74 D
        (JarvisCore L5) - not its square box, whose corners the shell's fade band enters."""
        if name != "jarvisCore":
            self.apart(box, self.rect(item), name)
            return
        r = self.rect(item)
        cx, cy, radius = r.center().x(), r.center().y(), r.width() * 0.37
        nx, ny = min(max(cx, box.left()), box.right()), min(max(cy, box.top()), box.bottom())
        self.assertGreaterEqual(((nx - cx) ** 2 + (ny - cy) ** 2) ** 0.5, radius - 0.5, name)

    @ambient_on          # THIZ: it checks every mid-reflow frame
    def test_ee_the_header_fits_at_every_size_and_long_names_elide_in_space(self) -> None:
        self.sweep_ee(("space",))

    @ambient_on          # THIZ: it checks every mid-reflow frame
    def test_ee_the_header_fits_at_every_size_and_long_names_elide_in_dark(self) -> None:
        self.sweep_ee(("dark",))

    def sweep_ee(self, themes) -> None:
        # THIZ: one theme per test method, so the parallel runner can spread them.
        store = self.seed()
        longp = store.create_project(LONG_PROJECT)
        longest = (LONG_TITLE + ", " + LONG_TITLE.lower())[:120]                  # the store's limit; elides even at 1920
        owned = store.create_conversation(longp.id, title=longest)
        global_long = store.create_conversation(title=longest[:-8] + " (Genel)")
        bridge = self.bridge()
        root = self.window(bridge)

        def clear_mid_reflow() -> None:             # F-ee: no frame of a reflow puts the header over a neighbour
            header = self.box(root, "chatHeader")
            self.inside(QRectF(0, 0, root.width(), root.height()), header, "header in the window mid-reflow")
            for other in ("canonicalComposer", "sidebar", "panelColumn"):
                self.apart(header, self.box(root, other), other + " mid-reflow")
        # F-ee: the first step below is the largest shrink, a 4K window to the minimum.
        self.assertTrue(self.resize_still(root, 3840, 2160, 3000, every=clear_mid_reflow), "4K did not settle")
        for theme in themes:
            bridge.setTheme(theme)
            for key, conversation in (("project", owned), ("global", global_long)):
                bridge.openConversation(conversation.id)
                pump(300)
                for width, height in SIZES:
                    with self.subTest(theme=theme, owner=key, size=(width, height)):
                        # F-ee: the rail's reflow, then the column's, take ~1 s to go still offscreen
                        # (the old 600 ms ran out unnoticed); what follows measures the still frame.
                        self.assertTrue(self.resize_still(root, width, height, 3000, every=clear_mid_reflow),
                                        "the reflow did not settle")
                        header = self.box(root, "chatHeader")
                        self.inside(self.box(root, "canonicalWorkspace"), header, "header")
                        for other in ("canonicalComposer", "sidebar", "panelColumn"):
                            self.apart(header, self.box(root, other), other)
                        for name in BAR:
                            for item in self.shown(root, name):
                                if item.width() > 0:
                                    self.clear(root, header, name, item)
                        parts = ["chatHeaderScope", "chatHeaderTitle", "chatHeaderMenu"]
                        self.assertEqual(key == "global", self.item(root, "chatHeaderAttach").isVisible())
                        if key == "global":
                            parts.insert(2, "chatHeaderAttach")
                        boxes = [self.box(root, name) for name in parts]
                        for name, box in zip(parts, boxes):
                            self.inside(header, box, name)
                        for left, right in zip(boxes, boxes[1:]):
                            self.assertLessEqual(left.right(), right.left() + 0.5, (parts, left, right))
                        self.assertLessEqual(boxes[0].width(), round(header.width() * 0.32) + 0.5)
                        # Long names elide (never under 6 characters) and their tips have them whole.
                        name, title = self.item(root, "chatHeaderScopeName"), self.item(root, "chatHeaderTitle")
                        for text in (name, title):
                            self.assertEqual(1, text.property("lineCount"))
                            if text.property("truncated"):
                                six = QFontMetricsF(text.property("font")).horizontalAdvance(text.property("text")[:6] + "…")
                                self.assertGreaterEqual(text.width() + 0.5, six)
                        self.assertTrue(title.property("truncated"))
                        self.assertEqual(bridge.conversationTitle, self.item(root, "chatHeaderTitleTip").property("text"))
                        self.assertEqual(bridge.conversationProject["name"], self.item(root, "chatHeaderScopeTip").property("text"))
                        if key == "project":
                            self.assertTrue(name.property("truncated"))
                        # The stream starts below the header.
                        self.assertGreaterEqual(self.box(root, "conversationFeed").top(), header.bottom())
        self.assertEqual(0, self.execute_calls())

    def test_ff_attach_only_for_global_and_the_same_flow(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        button = self.item(root, "chatHeaderAttach")
        self.assertTrue((bridge.canAttach, True) == (True, button.isVisible()))
        self.tap(root, self.row(root, "sidebarProject", "Çok"), then=lambda: (
            self.assertFalse(button.isVisible()),                                            # a project's: no entry
        ))
        self.tap(root, self.item(root, "sidebarGlobal"))
        archived = next(r.id for r in store.list_conversations(project_id=None, include_archived=True) if r.archived_at)
        bridge.openConversation(archived)
        settled(300, lambda: (
            self.assertFalse(button.isVisible()),                                            # archived: no entry
        ))
        self.tap(root, self.item(root, "sidebarGlobal"))
        say(store, bridge.conversationId, "tezgah ölçüleri")
        moving = bridge.conversationId
        self.assertTrue(button.isVisible())
        self.tap(root, button)
        dialog = attach_dialog(root)
        self.assertTrue(dialog.property("visible"))
        item = [i for i in self.all_items(root, "attachProject") if i.property("label") == "Boş"][0]
        self.tap(root, item, then=lambda: (
            self.assertEqual("Target: Boş", self.item(root, "attachTarget").property("text")),
        ))
        self.tap(root, self.item(root, "attachConfirm"))
        self.assertEqual(self.empty.id, store.load_conversation(moving).project_id)
        self.assertEqual("Boş", self.item(root, "chatHeaderScope").property("fullName"))
        self.assertFalse(button.isVisible())
        self.assertEqual(0, self.execute_calls())

    def test_gg_the_header_menu_acts_on_the_open_one_and_a_row_menu_on_its_row(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.tap(root, self.row(root, "sidebarProject", "Çok"))
        menu = self.item(root, "conversationMenu")
        self.assertEqual(1, len(self.all_items(root, "conversationMenu")))              # one menu, two openers
        opened = bridge.conversationId
        self.tap(root, self.item(root, "chatHeaderMenu"), then=lambda: (
            self.assertEqual(("", opened, True), (menu.property("rowId"), menu.property("targetId"), menu.property("open"))),
            self.assertEqual(self.item(root, "chatHeaderTitle").property("text"),
                             self.item(root, "conversationContextHeader").property("text")),
        ))
        self.tap(root, self.item(root, "conversationRenameItem"))
        field = self.item(root, "conversationRenameField")
        self.assertEqual(bridge.conversationTitle, field.property("text"))
        # The editor sits under the title.
        title, editor = self.box(root, "chatHeaderTitle"), self.box(root, "conversationRenameTitle")
        self.assertGreater(editor.top(), title.bottom())
        self.assertLess(abs(editor.left() - title.left()), 40)
        field.setProperty("text", "Başlıktan ad")
        pump(60)
        self.tap(root, self.item(root, "conversationRenameSave"))
        self.assertEqual("Başlıktan ad", store.load_conversation(opened).title)
        self.assertEqual("Başlıktan ad", self.item(root, "chatHeaderTitle").property("text"))
        # A row's menu: that row, not the open one; the header's "…" is not marked open.
        other = next(r for r in self.shown(root, "sidebarConversation") if not r.property("current"))
        other_id = other.property("itemId")
        self.tap(root, other, Qt.RightButton, then=lambda: (
            self.assertEqual(other_id, menu.property("targetId")),
            self.assertFalse(self.item(root, "chatHeaderMenu").property("open")),
        ))
        self.tap(root, self.item(root, "conversationArchiveItem"))
        self.assertIsNotNone(store.load_conversation(other_id).archived_at)
        self.assertEqual(opened, bridge.conversationId)
        # The header's "…" archives the OPEN one: it leaves for the scope's newest live one.
        self.tap(root, self.item(root, "chatHeaderMenu"))
        self.tap(root, self.item(root, "conversationArchiveItem"))
        self.assertIsNotNone(store.load_conversation(opened).archived_at)
        self.assertNotEqual(opened, bridge.conversationId)
        self.assertEqual(0, self.execute_calls())

    def test_hh_the_top_bar_has_no_scope_group_and_nothing_overlaps_there_in_space(self) -> None:
        self.sweep_hh(("space",))

    def test_hh_the_top_bar_has_no_scope_group_and_nothing_overlaps_there_in_command(self) -> None:
        self.sweep_hh(("command",))

    def sweep_hh(self, themes) -> None:
        # THIZ: one theme per test method, so the parallel runner can spread them.
        from pathlib import Path
        qml = Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "qml" / "components" / "TopBar.qml"
        source = qml.read_text(encoding="utf-8")
        for gone in ("scopeGroup", "projectSelector", "conversationSwitcher", "newConversationButton", "ScopePill",
                     "Chevron", "scopeBottom", "AttachDialog"):
            self.assertNotIn(gone, source)
        store = self.seed()
        longp = store.create_project(LONG_PROJECT)
        owned = store.create_conversation(longp.id, title=LONG_TITLE)
        bridge = self.bridge()
        bridge.openConversation(owned.id)
        root = self.window(bridge)
        for gone in ("projectScope", "projectSelector", "conversationSwitcher", "newConversationButton"):
            self.assertEqual([], self.all_items(root, gone))
        for theme in themes:
            bridge.setTheme(theme)
            pump(400)
            for width, height in SIZES:
                self.resize_still(root, width, height, 600)
                with self.subTest(theme=theme, size=(width, height)):
                    window = QRectF(0, 0, width, height)
                    mine = [self.box(root, "chatHeader")] + ([self.box(root, "sidebar")] if theme != "command" else [])
                    for box in mine:
                        self.inside(window, box, "inside")
                        for name in BAR:
                            for item in self.shown(root, name):
                                if item.width() > 0:
                                    self.clear(root, box, name, item)
                    # The header progress (shown only during a run) starts after the
                    # sidebar or is below its own 240 px threshold (from 6E test_t).
                    hud = self.item(root, "hudProgress")
                    band = QRectF(hud.mapToScene(QPointF(0, 0)), hud.size())
                    if theme != "command" and hud.width() >= 240:
                        self.apart(band, self.box(root, "sidebar"), "progress")
        self.assertEqual(0, self.execute_calls())

    def test_ii_the_command_preset_chip_opens_the_scope_and_conversation_menu(self) -> None:
        store = self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setTheme("command")
        settled(500, lambda: (
            self.assertFalse(self.item(root, "sidebar").isVisible()),
        ))
        chip = self.item(root, "chatHeaderScope")
        # The canonical page's header may linger until its deferred deletion: the preset's menu is the shown one.
        scope_menu = lambda: next((m for m in root.findChildren(QObject, "chatHeaderScopeMenu") if m.property("visible")),  # noqa: E731
                                  root.findChildren(QObject, "chatHeaderScopeMenu")[-1])
        self.tap(root, chip)
        self.assertTrue(scope_menu().property("visible"))
        self.assertEqual(["Boş", "Tek", "Çok"], [i.property("label") for i in self.shown(root, "scopeMenuProject")])
        self.tap(root, [i for i in self.shown(root, "scopeMenuProject") if i.property("label") == "Çok"][0])
        self.assertFalse(scope_menu().property("visible"))
        self.assertEqual((store.list_conversations(project_id=self.many.id)[0].id, self.many.id),
                         (bridge.conversationId, bridge.conversationProject["id"]))           # the 6B rule
        self.tap(root, chip)
        rows = self.shown(root, "scopeMenuConversation")
        self.assertEqual(sorted(r.title for r in self.live), sorted(i.property("label") for i in rows))
        target = next(i for i in rows if not i.property("current"))
        label = target.property("label")
        self.tap(root, target, then=lambda: (
            self.assertEqual(label, bridge.conversationTitle),                                     # the switcher pick
        ))
        self.tap(root, chip)
        self.tap(root, self.item(root, "scopeMenuGlobal"), then=lambda: (
            self.assertTrue(bridge.conversationProject["global"]),
        ))
        self.tap(root, chip)
        self.tap(root, self.item(root, "scopeMenuAll"), then=lambda: (
            self.assertEqual(("projects", {}), (bridge.page, bridge.openProject)),
        ))
        bridge.setPage("home")
        pump(400)
        # "…" is the same conversation menu here.
        self.tap(root, self.item(root, "chatHeaderMenu"), then=lambda: (
            self.assertTrue(self.item(root, "conversationMenu").property("open")),
            self.assertEqual(0, self.execute_calls()),
        ))

    def test_jj_the_attach_dialog_lookup_is_unambiguous(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        named = root.findChildren(QObject, "attachDialog")
        kinds = sorted(d.metaObject().className().split("_")[0] for d in named)
        self.assertEqual(2, len(named), kinds)                                          # the picker and the dialog
        dialog = attach_dialog(root)
        self.assertIsNotNone(dialog.property("preferredWidth"))
        self.tap(root, self.item(root, "chatHeaderAttach"), then=lambda: (
            self.assertEqual([dialog], [d for d in named if d.property("visible")]),
            self.assertEqual(0, self.execute_calls()),
        ))

    def test_kk_the_header_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        for source, turkish in (("Attach to a project…", "Projeye bağla…"), ("Conversation actions", "Sohbet işlemleri"),
                                ("New Chat", "Yeni Sohbet"), ("ARCHIVED", "ARŞİV"), ("archived", "arşivlendi"),
                                ("PROJECTS", "PROJELER"), ("Global", "Genel"), ("All projects", "Tüm projeler"),
                                ("CONVERSATIONS", "SOHBETLER")):
            self.assertEqual(turkish, tx("ChatHeader", source))


# -- slice F5: the punch list P1-P3 and the acceptance sweep ------------------
# P1 tile tips follow G3 (elided text only; never over a neighbour's title);
# P2 every menu over content is fully opaque; P3 the selected project row is
# kept in view; the sweep: home page, header, menus and the peek at 3 sizes x
# 2 themes.

TIP_DELAY = 500                                   # Theme.tipDelay


def settle(condition, timeout: float = 5.0) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        pump(20)
    return bool(condition())


class PunchListCase(SidebarNavCase):
    def tips(self, root) -> list:
        return [i for i in self.all_items(root, "hoverTipText") if i.isVisible()]

    def hover(self, root, item) -> None:
        # THIZ: the point is read while ``item`` is the one asked for; a rail that rebuilds its
        # tiles during the pump below would leave a deleted item (base 072d0fe hit it under load).
        target = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseMove(root, QPoint(2, 2))
        pump(30)
        QTest.mouseMove(root, target)

    def running(self, bridge, name: str) -> None:
        import time
        bridge._session_id = bridge._session_id or "s-1"
        bridge._busy = True
        bridge._task = {**bridge._task, "status": "running", "title": name}
        bridge._work_started_at = time.time() - 75
        bridge.stateChanged.emit()


class TileTipTests(PunchListCase):
    def test_ll_p1_a_tile_tip_only_for_elided_text_and_never_over_a_neighbours_title(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for width, height in SIZES:
            root.resize(width, height)
            pump(700)
            with self.subTest(size=(width, height), state="nothing elided"):
                # The resting tiles say everything they have: hovering shows nothing, and the tips are off.
                for key in TILES:
                    self.assertFalse(self.item(root, "railTip" + key).property("active"), key)
                    self.hover(root, self.item(root, "rail" + key))
                    pump(TIP_DELAY + 250)
                    self.assertEqual([], self.tips(root), key)
        # A running task with a very long name: its line elides (or shortens) and the tip has it whole.
        long_name = "Kat malikleri toplantısı için hazırlanacak belgeler, aidat hesapları ve tadilat izinleri özeti"
        self.running(bridge, long_name)
        for width, height in SIZES:
            root.resize(width, height)
            pump(1300)
            with self.subTest(size=(width, height), state="elided"):
                tile = self.item(root, "railTasks")
                line = self.item(root, "railLineTasks")
                self.assertTrue(line.property("truncated") or line.property("text") != long_name, "the line must be cut")
                self.assertTrue(self.item(root, "railTipTasks").property("active"))
                self.hover(root, tile)
                self.assertTrue(settle(lambda: bool(self.tips(root))), "the tip never appeared")
                tip = self.tips(root)[0]
                self.assertIn(long_name, str(tip.property("text")))
                tip_box = self.rect(tip)
                self.inside(QRectF(0, 0, width, height), tip_box, "tip in the window")
                for key in TILES:
                    self.apart(tip_box, self.box(root, "rail" + key), "tip over tile " + key)
                    self.apart(tip_box, self.box(root, "railTitle" + key), "tip over the title of " + key)
                self.apart(tip_box, self.box(root, "sidebar"), "tip over the sidebar")
                QTest.mouseMove(root, QPoint(width // 2, 2))
                pump(300)
        # While its peek is open the tile's tip stays off.
        self.press(root, "railTasks", then=lambda: (
            self.assertTrue(root.property("peekOpen")),
            self.assertFalse(self.item(root, "railTipTasks").property("active")),
            self.assertEqual(0, self.execute_calls()),
        ))


class ProjectListTests(PunchListCase):
    def many(self, count: int = 9):
        store = self.db()
        self.projects = [store.create_project(f"Proje {n}") for n in range(count)]
        self.talks = [store.create_conversation(p.id, title=f"{p.name} sohbeti") for p in self.projects]
        return store

    def selected_in_view(self, root, index: int, what: str) -> None:
        pump(300)
        row = self.row(root, "sidebarProject", f"Proje {index}")
        self.assertTrue(row.property("current"), what)
        box, view = self.rect(row), self.rect(self.item(root, "sidebarProjectList"))
        self.assertGreaterEqual(box.top(), view.top() - 0.5, what)
        self.assertLessEqual(box.bottom(), view.bottom() + 0.5, what)       # the whole row, never a sliver

    def test_nn_p3_the_selected_project_row_stays_in_view(self) -> None:
        self.many()
        bridge = self.bridge()
        bridge.openConversation(self.talks[8].id)                            # open with the LAST project selected
        root = self.window(bridge)
        root.resize(1100, 700)
        settled(900, lambda: (
            self.assertTrue(self.item(root, "sidebarProjectList").property("contentHeight") > self.item(root, "sidebarProjectList").height()),
        ))
        self.selected_in_view(root, 8, "on open")
        for index in (3, 5, 8, 0, 4, 7, 1):                                  # a scope change, 4th and beyond
            with self.subTest(scope=index):
                self.assertTrue(bridge.openProjectScope(self.projects[index].id))
                pump(300)
                self.selected_in_view(root, index, f"scope {index}")
        # A resize changes how many rows fit: the selection is still in view, at both ends.
        bridge.openProjectScope(self.projects[6].id)
        pump(300)
        for width, height in ((1920, 1080), (1100, 700), (1280, 760), (1920, 1080), (1100, 700)):
            root.resize(width, height)
            pump(900)
            self.selected_in_view(root, 6, f"resize {width}x{height}")
        # The reader's own scrolling is left alone.
        plist = self.item(root, "sidebarProjectList")
        plist.setProperty("contentY", 0)
        settled(300, lambda: (
            self.assertEqual(0, plist.property("contentY")),
            self.assertEqual(0, self.execute_calls()),
        ))


class MenuOpacityTests(PunchListCase):
    def popup(self, root, name):
        # A lingering page's copy may exist until its deferred deletion: the shown one is the menu.
        found = root.findChildren(QObject, name)
        self.assertTrue(found, name)
        return next((m for m in found if m.property("visible")), found[-1])

    def is_open(self, popup) -> bool:
        return bool(popup.property("opened")) and popup.property("opacity") == 1.0

    def opaque(self, root, name: str, over: str) -> None:
        """The popup's own body is alpha 1, and what a sample of its padding shows is its body
        and nothing behind it: every sampled pixel is the menu surface."""
        popup = self.popup(root, name)
        self.assertTrue(settle(lambda: self.is_open(popup)), name + " never opened")
        body = popup.property("background")
        self.assertEqual(1.0, QColor(body.property("color")).alphaF(), name)
        want = QColor(6, 20, 36)
        image = root.grabWindow()
        origin = body.mapToScene(QPointF(0, 0))
        bad = []
        for dx in (3, int(body.width()) - 4):
            for dy in range(14, int(body.height()) - 14, 7):
                pixel = QColor(image.pixel(int(origin.x()) + dx, int(origin.y()) + dy))
                if max(abs(pixel.red() - want.red()), abs(pixel.green() - want.green()), abs(pixel.blue() - want.blue())) > 3:
                    bad.append((dx, dy, pixel.name()))
        self.assertEqual([], bad, f"{name} shows what is behind it ({over})")

    @ambient_on          # THIZ: it reads the painted menu pixels
    def test_oo_p2_every_menu_over_content_is_fully_opaque_in_space(self) -> None:
        self.sweep_oo(("space",))

    @ambient_on          # THIZ: it reads the painted menu pixels
    def test_oo_p2_every_menu_over_content_is_fully_opaque_in_dark(self) -> None:
        self.sweep_oo(("dark",))

    def sweep_oo(self, themes) -> None:
        # THIZ: one theme per test method, so the parallel runner can spread them.
        store = self.seed()
        for n in range(3):
            store.create_conversation(self.many.id, title=f"Çok ek {n}")
        bridge = self.bridge()
        root = self.window(bridge)
        for theme in themes:
            bridge.setTheme(theme)
            for width, height in SIZES[:2] + SIZES[2:]:
                self.resize_still(root, width, height, 700)
                bridge.openProjectScope(self.many.id)
                pump(400)
                with self.subTest(theme=theme, size=(width, height)):
                    self.tap(root, self.shown(root, "sidebarConversation")[1], Qt.RightButton,
                             until=lambda: self.is_open(self.popup(root, "conversationSwitcherContext")))
                    self.opaque(root, "conversationSwitcherContext", "the sidebar row menu")
                    self.tap(root, self.item(root, "conversationRenameItem"),
                             until=lambda: self.is_open(self.popup(root, "conversationRenameEditor")))
                    self.opaque(root, "conversationRenameEditor", "the rename editor")
                    self.closed(self.popup(root, "conversationRenameEditor"), 400)
                    self.tap(root, self.item(root, "chatHeaderMenu"),
                             until=lambda: self.is_open(self.popup(root, "conversationSwitcherContext")))
                    self.opaque(root, "conversationSwitcherContext", "the header menu")
                    self.closed(self.popup(root, "conversationSwitcherContext"), 400)
                    for chip, menu in (("permissionModeControl", "permissionModeMenu"), ("attachControl", "attachMenu")):
                        self.tap(root, self.item(root, chip), until=lambda: self.is_open(self.popup(root, menu)))
                        self.opaque(root, menu, "the composer")
                        self.closed(self.popup(root, menu), 400)
        # The Command preset's scope menu is the same material.
        bridge.setTheme("command")
        pump(500)
        bridge.openProjectScope(self.many.id)
        pump(400)
        self.tap(root, self.item(root, "chatHeaderScope"))
        self.opaque(root, "chatHeaderScopeMenu", "the command preset's scope menu")
        self.assertEqual(0, self.execute_calls())


class SweepCase(PunchListCase):
    """The acceptance sweep: home page, sidebar, header, menus and the peek, 3 sizes x 2 themes."""

    def descendants(self, item) -> list:
        found, stack = [], [item]
        while stack:
            node = stack.pop()
            found.append(node)
            stack.extend(node.childItems())
        return found

    def popup_named(self, root, name):
        found = root.findChildren(QObject, name)
        self.assertTrue(found, name)
        return next((m for m in found if m.property("visible")), found[-1])

    def has_tip(self, text) -> bool:
        """An elided Text is allowed only with its tip: an active HoverTip beside it or in an ancestor's own children."""
        node = text
        for _ in range(5):
            for child in node.childItems():
                if "HoverTip" in child.metaObject().className() and child.property("active") and child.property("text"):
                    return True
            node = node.parentItem()
            if node is None:
                return False
            for child in node.childItems():
                if "HoverTip" in child.metaObject().className() and child.property("active") and child.property("text"):
                    return True
        return False

    def no_clipped_text(self, root, region, what: str) -> None:
        """In ``region`` every visible Text is painted inside the window and inside its own box, and is
        cut (elided) only with its tip."""
        window = QRectF(0, 0, root.width(), root.height())
        for text in self.descendants(region):
            value = text.property("text")
            if not text.isVisible() or not isinstance(value, str) or not value:
                continue
            if "TextInput" in text.metaObject().className() or "TextField" in text.metaObject().className():
                continue                                                        # an editable field scrolls its text
            if text.width() <= 0 or text.property("contentWidth") is None or text.property("opacity") == 0:
                continue
            ink = self.rect(text)
            ink.setWidth(min(ink.width(), text.property("contentWidth")))
            self.inside(window, ink, f"{what}: {value[:30]!r} outside the window")
            if text.property("truncated"):
                self.assertTrue(self.has_tip(text), f"{what}: {value[:40]!r} is elided without a tip")
            else:                                                               # it must fit its own box
                self.assertLessEqual(text.property("contentWidth"), text.width() + 0.5,
                                     f"{what}: {value[:40]!r} paints wider than its box")

    def frame(self, root, what: str) -> None:
        """The big pieces of the home page do not overlap one another or the TopBar's parts."""
        names = ("sidebar", "chatHeader", "canonicalComposer", "panelColumn")
        boxes = {n: self.box(root, n) for n in names}
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.apart(boxes[a], boxes[b], f"{what}: {a} / {b}")
        for name in BAR:
            for item in self.shown(root, name):
                if item.width() > 0 and name != "jarvisCore":
                    for n in names[:3]:
                        self.apart(boxes[n], self.rect(item), f"{what}: {n} / {name}")


class SweepTests(SweepCase):
    def data(self):
        store = self.seed()
        longp = store.create_project(LONG_PROJECT)
        for n in range(3):
            store.create_project(f"Proje {n}")
        self.long_conv = store.create_conversation(longp.id, title=LONG_TITLE)
        store.create_conversation(longp.id, title="Kısa")
        return store

    def opened_menu(self, root, name: str):
        menu = self.popup_named(root, name)
        self.assertTrue(settle(lambda: bool(menu.property("opened")) and menu.property("opacity") == 1.0), name + " never opened")
        return menu

    @ambient_on          # THIZ: the home page's one sweep with motion
    def test_pp_the_home_page_states_have_no_clipped_text_and_no_overlap_in_space(self) -> None:
        self.sweep_pp(("space",))

    @ambient_on          # THIZ: the home page's one sweep with motion
    def test_pp_the_home_page_states_have_no_clipped_text_and_no_overlap_in_dark(self) -> None:
        self.sweep_pp(("dark",))

    def sweep_pp(self, themes) -> None:
        # THIZ: one theme per test method, so the parallel runner can spread them.
        self.data()
        bridge = self.bridge()
        bridge.openConversation(self.long_conv.id)
        root = self.window(bridge)
        for theme in themes:
            bridge.setTheme(theme)
            for width, height in SIZES:
                self.resize_still(root, width, height, 900)
                window = QRectF(0, 0, width, height)
                what = f"{theme} {width}x{height}"
                with self.subTest(state="rest", at=what):
                    self.frame(root, what)
                    self.no_clipped_text(root, self.item(root, "sidebar"), what + " sidebar")
                    self.no_clipped_text(root, self.item(root, "chatHeader"), what + " header")
                with self.subTest(state="row menu", at=what):
                    context = self.popup_named(root, "conversationSwitcherContext")
                    self.tap(root, self.shown(root, "sidebarConversation")[0], Qt.RightButton,
                             until=lambda: bool(context.property("opened")) and context.property("opacity") == 1.0)
                    menu = self.opened_menu(root, "conversationSwitcherContext")
                    body = self.rect(menu.property("background"))
                    self.inside(window, body, "row menu in the window")
                    self.assertGreaterEqual(body.left(), 7.5)                          # 8 px clear of the edges
                    self.assertLessEqual(body.right(), width - 7.5)
                    self.assertLessEqual(body.bottom(), height - 7.5)
                    self.no_clipped_text(root, menu.property("contentItem"), what + " row menu")
                    self.closed(menu, 400)
                with self.subTest(state="header menu", at=what):
                    self.tap(root, self.item(root, "chatHeaderMenu"),
                             until=lambda: bool(context.property("opened")) and context.property("opacity") == 1.0)
                    menu = self.opened_menu(root, "conversationSwitcherContext")
                    self.inside(window, self.rect(menu.property("background")), "header menu in the window")
                    self.no_clipped_text(root, menu.property("contentItem"), what + " header menu")
                    self.closed(menu, 400)
                with self.subTest(state="rename editor", at=what):
                    self.tap(root, self.item(root, "chatHeaderMenu"),
                             until=lambda: bool(context.property("opened")) and context.property("opacity") == 1.0)
                    named = self.popup_named(root, "conversationRenameEditor")
                    self.tap(root, self.item(root, "conversationRenameItem"),
                             until=lambda: bool(named.property("opened")) and named.property("opacity") == 1.0)
                    editor = self.opened_menu(root, "conversationRenameEditor")
                    self.inside(window, self.rect(editor.property("background")), "rename editor in the window")
                    self.no_clipped_text(root, editor.property("contentItem"), what + " rename editor")
                    self.closed(editor, 400)
                for key in ("Tasks", "Focus", "Plan"):
                    with self.subTest(state="peek " + key, at=what):
                        self.press(root, "rail" + key, until=lambda: root.property("peekOpen"))
                        self.assertTrue(root.property("peekOpen"))
                        peek = self.item(root, "summaryPeek")
                        # THIZ (the F-ee2 rule): measured once the peek and its texts are still for STILL_SPAN.
                        self.assertTrue(wait_still(lambda: [self.rect(i) for i in self.descendants(peek) if i.isVisible()],
                                                   SETTLE_FLOOR_MS, 50, 6), "the peek did not come to rest")
                        self.inside(window, self.rect(peek), "peek in the window")
                        self.apart(self.rect(peek), self.box(root, "sidebar"), "peek over the sidebar")
                        self.no_clipped_text(root, peek, what + " peek " + key)
                        self.press(root, "rail" + key, until=lambda: not root.property("peekOpen"))
                for chip, menu_name in (("permissionModeControl", "permissionModeMenu"), ("attachControl", "attachMenu")):
                    with self.subTest(state=menu_name, at=what):
                        chip_menu = self.popup_named(root, menu_name)
                        self.tap(root, self.item(root, chip),
                                 until=lambda: bool(chip_menu.property("opened")) and chip_menu.property("opacity") == 1.0)
                        menu = self.opened_menu(root, menu_name)
                        self.inside(window, self.rect(menu.property("background")), menu_name + " in the window")
                        self.no_clipped_text(root, menu.property("contentItem"), what + " " + menu_name)
                        self.closed(menu, 400)
        self.assertEqual(0, self.execute_calls())


class SweepPathTests(SweepCase):
    def test_qq_every_6f_action_has_a_mouse_path_at_every_size_in_space(self) -> None:
        self.sweep_qq(("space",))

    def test_qq_every_6f_action_has_a_mouse_path_at_every_size_in_dark(self) -> None:
        self.sweep_qq(("dark",))

    def sweep_qq(self, themes) -> None:
        # THIZ: one theme per test method, so the parallel runner can spread them.
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        keys = []
        real = QTest.keyClick
        with mock.patch.object(QTest, "keyClick", side_effect=lambda *a, **k: (keys.append(a), real(*a, **k))):
            for theme in themes:
                bridge.setTheme(theme)
                for width, height in SIZES:
                    self.resize_still(root, width, height, 900)
                    with self.subTest(theme=theme, size=(width, height)):
                        bridge.setPage("home")
                        bridge.openProjectScope("")
                        wait_until(lambda: root.property("loadedPage") == "home" and bridge.conversationProject["global"], 400)
                        self.tap(root, self.item(root, "sidebarGlobal"), until=lambda: bridge.conversationProject["global"])  # Genel
                        self.assertTrue(bridge.conversationProject["global"])
                        self.tap(root, self.row(root, "sidebarProject", "Çok"),
                                 until=lambda: bridge.conversationProject["id"] == self.many.id)               # a project
                        self.assertEqual(self.many.id, bridge.conversationProject["id"])
                        other = next(t for t in self.titles(root) if t.startswith("Çok ") and not self.row(root, "sidebarConversation", t).property("current"))
                        self.tap(root, self.scrolled_row(root, other), until=lambda: bridge.conversationTitle == other)  # a conversation
                        self.assertEqual(other, bridge.conversationTitle)
                        before = bridge.conversationId
                        self.tap(root, self.item(root, "sidebarNewConversation"), until=lambda: bridge.conversationId != before)  # the round +
                        self.assertNotEqual(before, bridge.conversationId)
                        self.tap(root, self.scrolled_row(root, other), Qt.RightButton)                       # its row menu
                        self.assertTrue(self.opened_menu_named(root, "conversationSwitcherContext"))
                        for item in ("conversationRenameItem", "conversationArchiveItem", "conversationDeleteItem"):
                            self.assertTrue(self.item(root, item).isVisible(), item)
                        self.closed(self.popup_named(root, "conversationSwitcherContext"), 400)
                        self.tap(root, self.item(root, "chatHeaderMenu"))                                    # the header "…"
                        self.assertTrue(self.opened_menu_named(root, "conversationSwitcherContext"))
                        self.closed(self.popup_named(root, "conversationSwitcherContext"), 400)
                        self.tap(root, self.item(root, "sidebarGlobal"), until=lambda: bridge.conversationProject["global"])
                        self.assertTrue(self.item(root, "chatHeaderAttach").isVisible())                     # Projeye bağla
                        self.tap(root, self.item(root, "chatHeaderAttach"))
                        dialog = attach_dialog(root)
                        self.assertTrue(settle(lambda: bool(dialog.property("visible"))))
                        self.closed(dialog, 400)
                        for key in ("Tasks", "Focus", "Plan"):                                               # the tiles' peeks
                            self.tap(root, self.item(root, "rail" + key),
                                     until=lambda: (root.property("peekOpen"), root.property("peekKey")) == (True, key))
                            self.assertEqual((True, key), (root.property("peekOpen"), root.property("peekKey")))
                            self.tap(root, self.item(root, "rail" + key), until=lambda: not root.property("peekOpen"))
                            self.assertFalse(root.property("peekOpen"))
                        listing = self.item(root, "sidebarConversationList")                                 # the link is the list's last
                        listing.setProperty("contentY", max(0, listing.property("contentHeight") - listing.height()))
                        pump(200)
                        self.tap(root, self.item(root, "sidebarArchive"), until=lambda: bridge.page == "history")  # Arşiv
                        self.assertEqual("history", bridge.page)
                        self.home(root, bridge, 400)
                        self.tap(root, self.item(root, "sidebarProjectsLink"), until=lambda: bridge.page == "projects")  # PROJELER
                        self.assertEqual(("projects", {}), (bridge.page, bridge.openProject))
                        self.home(root, bridge, 400)
                        self.tap(root, self.item(root, "sidebarNewProject"), until=lambda: bridge.page == "projects")  # new project
                        self.assertEqual("projects", bridge.page)
                        self.home(root, bridge, 400)
                        self.tap(root, self.item(root, "sidebarArchivedProjects") if self.shown(root, "sidebarArchivedProjects")
                                 else self.item(root, "sidebarArchivedProjectsCount"),
                                 until=lambda: bridge.page == "projects")                                    # archived projects
                        self.assertEqual("projects", bridge.page)
        self.assertEqual([], keys)                                                                           # no shortcut anywhere
        self.assertEqual(0, self.execute_calls())

    def scrolled_row(self, root, title: str):
        """The row, scrolled into the list's view as the reader's wheel would (the list keeps whole rows)."""
        row = self.row(root, "sidebarConversation", title)
        row.forceActiveFocus()
        pump(200)
        return row

    def opened_menu_named(self, root, name: str) -> bool:
        menu = self.popup_named(root, name)
        return settle(lambda: bool(menu.property("opened")) and menu.property("opacity") == 1.0)


class SweepDefectTests(SweepCase):
    def test_rr_the_menu_header_and_the_peek_elide_only_with_their_tips(self) -> None:
        store = self.seed()
        longp = store.create_project(LONG_PROJECT)
        long_conv = store.create_conversation(longp.id, title=LONG_TITLE)
        bridge = self.bridge()
        bridge.openConversation(long_conv.id)
        root = self.window(bridge)
        self.resize_still(root, 1100, 700, 900)
        # The conversation menu's header names the conversation it acts on: elided, it has its tip.
        self.tap(root, self.item(root, "chatHeaderMenu"))
        header = self.item(root, "conversationContextHeader")
        self.assertTrue(header.property("truncated"))
        tip = [c for c in header.childItems() if "HoverTip" in c.metaObject().className()][0]
        self.assertEqual((True, bridge.conversationTitle), (tip.property("active"), tip.property("text")))
        QMetaObject.invokeMethod(self.popup_named(root, "conversationSwitcherContext"), "close")
        pump(400)
        # The Focus peek names its projects, never their ids; every elided line in it has a tip.
        self.press(root, "railFocus")
        peek = self.item(root, "summaryPeek")
        shown = [t.property("text") for t in self.descendants(peek) if t.isVisible() and isinstance(t.property("text"), str)]
        import re
        self.assertFalse([t for t in shown if re.search("p-[0-9a-f]{16,}", t)], shown)
        first = next(p["name"] for p in bridge.projectRecords if p["id"] == bridge.projects[0])
        self.assertIn(f"Items in {first}", shown)
        self.assertIn(first, shown)
        self.no_clipped_text(root, peek, "Focus peek")
        self.assertEqual(0, self.execute_calls())


class NameFloorVarianceTests(SidebarNavCase):
    """G2 under metric variance (CI run 37212140938 painted 5): the six-character floor of
    the header's scope name and title and the sidebar's scope chip is measured as each
    name is laid out, so it holds with other interface faces too - here the bundled
    Inter, Manrope and Space Grotesk stand in for the wider metrics of another machine."""

    def painted(self, text_item) -> int:
        text = text_item.property("text")
        shown = QFontMetricsF(text_item.property("font")).elidedText(text, Qt.TextElideMode.ElideRight, text_item.width())
        return len(text) if shown == text else len(shown.rstrip("…"))

    def test_ll_the_six_character_floor_holds_with_another_interface_face_manrope(self) -> None:
        self.sweep_ll(("chat",))

    def test_ll_the_six_character_floor_holds_with_another_interface_face_inter(self) -> None:
        self.sweep_ll(("placeholder",))

    def test_ll_the_six_character_floor_holds_with_another_interface_face_space_grotesk(self) -> None:
        self.sweep_ll(("display",))

    def sweep_ll(self, keys) -> None:
        # THIZ: one face per test method, so the parallel runner can spread them.
        from jarvis.ui import typography
        base = typography.load_fonts()
        for family in (base[key] for key in keys):                                   # Manrope, Inter, Space Grotesk
            faces = {**base, "ui": family, "uiStrong": family, "uiStrongWeight": 600, "reading": family, "sans": family}
            with self.subTest(face=family), mock.patch.object(typography, "_families", faces):
                store = self.db()
                thesis = store.create_project(f"Tez 2025 okuma ve yazım çalışması {family}")
                archived = store.create_conversation(thesis.id, title="Okuma listesi taslağı ve kaynakça")
                store.set_project_active(thesis.id, False)
                longp = store.create_project(f"{LONG_PROJECT} {family}")
                live = store.create_conversation(longp.id, title=LONG_TITLE)
                bridge = self.bridge()
                root = self.window(bridge)
                self.assertEqual(family, self.item(root, "chatHeaderScopeName").property("font").family())
                for language in ("en-US", "tr-TR"):
                    if language == "tr-TR":
                        bridge.setLanguage("tr-TR")
                        pump(300)
                    for conversation in (archived, live):
                        bridge.openConversation(conversation.id)
                        bridge.setPage("home")
                        wait_until(lambda: bridge.conversationId == conversation.id and root.property("loadedPage") == "home", 300)
                        for width, height in SIZES:
                            self.resize_still(root, width, height, 400)
                            self.steady(root, "chatHeader")
                            for name in ("chatHeaderScopeName", "chatHeaderTitle", "sidebarScopeName"):
                                text = self.item(root, name)
                                self.assertGreaterEqual(self.painted(text), min(6, len(text.property("text"))),
                                                        (family, language, name, (width, height), text.property("text")))
                            # The pill stays (the chip's 32 % gave way to the floor, not the badge).
                            self.assertEqual(conversation is archived, self.item(root, "chatHeaderScopeArchived").isVisible())
                bridge.setLanguage("en-US")
                pump(200)
        self.assertEqual(0, self.execute_calls())



class F6Case(SweepCase):
    """Gate 6F F6: collapsible date groups, pins and their manual order (window preferences only)."""

    def into_view(self, root, item) -> None:
        """Scroll the sidebar's conversation list so ``item`` (one of its rows or headings) is wholly in view."""
        flick = self.item(root, "sidebarConversationList")
        content, node = flick.property("contentItem"), item
        while node is not None and node != content:
            node = node.parentItem()
        if node is None:
            return
        top = item.mapToItem(content, QPointF(0, 0)).y()
        y = target = flick.property("contentY")
        if top < y:
            target = top
        elif top + item.height() > y + flick.height():
            target = top + item.height() - flick.height()
        flick.setProperty("contentY", target)
        wait_until(lambda: abs(flick.property("contentY") - target) < 0.5, 150)       # S0b: at once when no scroll

    def tap(self, root, item, button=Qt.LeftButton, until=None, then=None) -> None:
        self.into_view(root, item)
        super().tap(root, item, button, until, then)

    def sidebar_file(self):
        return self.prefs_path.with_suffix(".sidebar.json")

    def head(self, root, key):
        if key == "pinned":
            return self.item(root, "sidebarPinnedHead")
        found = [h for h in self.shown(root, "sidebarGroupHead") if h.property("key") == key]
        self.assertEqual(1, len(found), key)
        return found[0]

    def chevron(self, head) -> float:
        return next(i for i in self.descendants(head) if i.objectName() == "sidebarGroupChevron").property("rotation")

    def pinned(self, root) -> list:
        return [i.property("title") for i in self.shown(root, "sidebarPinnedConversation")]

    def pin_row(self, root, title):
        return self.row(root, "sidebarPinnedConversation", title)

    def menu(self, root, opener, button=Qt.RightButton):
        menu = self.popup_named(root, "conversationSwitcherContext")
        opened = lambda: bool(menu.property("opened")) and menu.property("opacity") == 1.0  # noqa: E731
        self.tap(root, opener, button, until=opened)
        self.assertTrue(settle(lambda: bool(menu.property("opened")) and menu.property("opacity") == 1.0))
        return menu

    def choose(self, root, opener, item, button=Qt.RightButton, label=None) -> None:
        """Open the conversation menu on ``opener`` and click ``item`` (its label first, when given)."""
        menu = self.menu(root, opener, button)
        if label is not None:
            self.assertEqual(label, self.item(root, item).property("label"))
        self.tap(root, self.item(root, item), until=lambda: not menu.property("visible"))   # a chosen item closes it

    def close_menu(self, root) -> None:
        self.closed(self.popup_named(root, "conversationSwitcherContext"), 400)

    def drag(self, root, row, target_y: float, probe=None) -> None:
        """A real drag by the row's grip: press, move in steps, release at the scene y ``target_y``."""
        self.into_view(root, row)
        grip = self.child(row, "sidebarPinGripArea")
        start = grip.mapToScene(QPointF(grip.width() / 2, grip.height() / 2)).toPoint()
        self.hover(root, grip)
        pump(100)
        QTest.mousePress(root, Qt.LeftButton, Qt.NoModifier, start)
        pump(50)
        steps = 6
        for n in range(1, steps + 1):
            QTest.mouseMove(root, QPoint(start.x(), round(start.y() + (target_y - start.y()) * n / steps)))
            pump(40)
        if probe is not None:
            probe()
        QTest.mouseRelease(root, Qt.LeftButton, Qt.NoModifier, QPoint(start.x(), round(target_y)))
        pump(350)

    def gap_y(self, root, index: int) -> float:
        """The scene y of the gap above pinned row ``index`` (the one below the last row for len)."""
        rows = self.shown(root, "sidebarPinnedConversation")
        if index < len(rows):
            return rows[index].mapToScene(QPointF(0, 1)).y()
        return rows[-1].mapToScene(QPointF(0, rows[-1].height() - 1)).y()

    def painted(self, text_item) -> int:
        text = text_item.property("text")
        shown = QFontMetricsF(text_item.property("font")).elidedText(text, Qt.TextElideMode.ElideRight, text_item.width())
        return len(text) if shown == text else len(shown.rstrip("…"))


class CollapsibleGroupTests(F6Case):
    def dated(self):
        from datetime import datetime, timedelta
        store = self.db()
        now = self.now = datetime.now().astimezone()                         # THIZ2: once per test
        today = [store.create_conversation(title=f"Bugün {n}") for n in range(2)]
        yesterday = store.create_conversation(title="Dün 0")
        self.stamp(yesterday.id, now - timedelta(days=1))
        older = [store.create_conversation(title=f"Eski {n}") for n in range(3)]
        for n, c in enumerate(older):
            self.stamp(c.id, now - timedelta(days=5, minutes=n))
        return store, today, yesterday, older

    def test_ss_defaults_a_click_toggles_and_a_collapsed_heading_counts(self) -> None:
        store, today, yesterday, older = self.dated()
        bridge = self.bridge()
        root = self.window(bridge)
        self.fixed_today(root, self.now)
        self.assertEqual(today[1].id, bridge.conversationId)
        # Bugün + Dün open, Daha eski collapsed; SABİTLENDİ open (and hidden: nothing pinned).
        self.assertEqual({"pinned": True, "today": True, "yesterday": True, "earlier": False}, bridge.sidebarGroups)
        self.assertFalse(self.item(root, "sidebarPinned").isVisible())
        self.assertEqual(["Today", "Yesterday", "Earlier"], self.texts(root, "sidebarGroupLabel"))
        self.assertEqual(["· 3"], self.texts(root, "sidebarGroupCount"))
        self.assertEqual(["Bugün 1", "Bugün 0", "Dün 0"], self.titles(root))
        self.assertEqual((90, 90, 0), tuple(self.chevron(self.head(root, k)) for k in ("today", "yesterday", "earlier")))
        # A click opens it, another collapses it again.
        self.tap(root, self.head(root, "earlier"), then=lambda: (
            self.assertTrue(bridge.sidebarGroups["earlier"]),
            self.assertEqual(["Bugün 1", "Bugün 0", "Dün 0", "Eski 0", "Eski 1", "Eski 2"], self.titles(root)),
            self.assertEqual([], self.texts(root, "sidebarGroupCount")),
            self.assertEqual(90, self.chevron(self.head(root, "earlier"))),
        ))
        self.tap(root, self.head(root, "earlier"), then=lambda: (
            self.assertEqual(["Bugün 1", "Bugün 0", "Dün 0"], self.titles(root)),
        ))
        # The open conversation stays in view inside its collapsed group, marked; the heading counts all.
        self.tap(root, self.head(root, "today"), then=lambda: (
            self.assertEqual({"pinned": True, "today": False, "yesterday": True, "earlier": False}, bridge.sidebarGroups),
            self.assertEqual(["Bugün 1", "Dün 0"], self.titles(root)),
            self.assertTrue(self.row(root, "sidebarConversation", "Bugün 1").property("current")),
            self.assertEqual(["· 2", "· 3"], self.texts(root, "sidebarGroupCount")),
        ))
        # The keyboard toggles it too (Return on the focused heading).
        head = self.head(root, "today")
        head.forceActiveFocus()
        pump(100)
        QTest.keyClick(root, Qt.Key_Return)
        settled(300, lambda: (
            self.assertTrue(bridge.sidebarGroups["today"]),
            self.assertEqual(0, self.execute_calls()),
        ))

    def test_tt_a_switch_opens_its_group_and_the_state_is_per_scope_across_a_reopen(self) -> None:
        from datetime import datetime, timedelta
        store = self.db()
        now = datetime.now().astimezone()
        tez = store.create_project("Tez")
        g_old = store.create_conversation(title="Genel eski")
        self.stamp(g_old.id, now - timedelta(days=5))
        store.create_conversation(title="Genel yeni")
        t_old = store.create_conversation(tez.id, title="Tez eski")
        self.stamp(t_old.id, now - timedelta(days=5))
        store.create_conversation(tez.id, title="Tez yeni")
        bridge = self.bridge()
        root = self.window(bridge)
        self.fixed_today(root, now)                                             # THIZ2: once per test
        self.tap(root, self.head(root, "today"))                                # Genel: Bugün collapsed
        self.tap(root, self.row(root, "sidebarProject", "Tez"), then=lambda: (  # Tez: its own defaults
            self.assertEqual({"pinned": True, "today": True, "yesterday": True, "earlier": False}, bridge.sidebarGroups),
        ))
        self.tap(root, self.head(root, "earlier"), then=lambda: (  # Tez: Daha eski open
            self.assertIn("Tez eski", self.titles(root)),
        ))
        self.tap(root, self.item(root, "sidebarGlobal"))                        # back to Genel: as it was left
        self.assertEqual({"pinned": True, "today": False, "yesterday": True, "earlier": False}, bridge.sidebarGroups)
        self.assertNotIn("Genel eski", self.titles(root))
        # Switching to a conversation of a collapsed group opens that group.
        self.assertTrue(bridge.openConversation(g_old.id))
        settled(300, lambda: (
            self.assertTrue(bridge.sidebarGroups["earlier"]),
            self.assertTrue(self.row(root, "sidebarConversation", "Genel eski").property("current")),
            self.assertFalse(bridge.sidebarGroups["today"]),                          # the others keep their state
        ))
        # A reopened window has every scope's state.
        root.hide()
        bridge = self.bridge()
        root = self.window(bridge)
        self.fixed_today(root, now)
        self.assertTrue(bridge.conversationProject["global"])
        self.assertEqual({"pinned": True, "today": False, "yesterday": True, "earlier": True}, bridge.sidebarGroups)
        self.assertEqual(["Genel yeni", "Genel eski"], self.titles(root))             # the open one stays in view
        self.assertEqual(["· 1"], self.texts(root, "sidebarGroupCount"))
        self.tap(root, self.row(root, "sidebarProject", "Tez"), then=lambda: (
            self.assertEqual({"pinned": True, "today": True, "yesterday": True, "earlier": True}, bridge.sidebarGroups),
            self.assertEqual(["Tez yeni", "Tez eski"], self.titles(root)),
        ))
        saved = json.loads(self.sidebar_file().read_text(encoding="utf-8"))["groups"]
        self.assertEqual({"global": {"today": False, "yesterday": True, "earlier": True},
                          tez.id: {"today": True, "yesterday": True, "earlier": True}}, saved)
        self.assertEqual(0, self.execute_calls())


class PinTests(F6Case):
    def scoped(self):
        store = self.db()
        self.tez = store.create_project("Tez")
        self.genel = [store.create_conversation(title=f"Genel {n}") for n in range(3)]
        self.tezc = [store.create_conversation(self.tez.id, title=f"Tez {n}") for n in range(3)]
        return store

    def test_uu_pin_and_unpin_from_both_menus_across_scopes(self) -> None:
        self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual(self.genel[2].id, bridge.conversationId)
        # A sidebar row's menu: Sabitle - into SABİTLENDİ, out of its date group, the conversation unchanged.
        self.choose(root, self.row(root, "sidebarConversation", "Genel 0"), "conversationPinItem", label="Pin")
        self.assertEqual(["Genel 0"], self.pinned(root))
        self.assertTrue(self.item(root, "sidebarPinned").isVisible())
        self.assertNotIn("Genel 0", self.titles(root))
        self.assertEqual(["PINNED"], self.texts(root, "sidebarPinnedLabel"))
        row = self.pin_row(root, "Genel 0")
        self.assertFalse(self.child(row, "sidebarPinPlace").isVisible())               # Genel: no label
        self.assertTrue(self.child(row, "sidebarPinIcon").isVisible())
        tip = self.child(row, "sidebarRowTip")
        self.assertEqual(("Genel 0\nGlobal", True), (tip.property("text"), tip.property("active")))
        # The header "…": the open conversation; a new pin goes to the top.
        self.choose(root, self.item(root, "chatHeaderMenu"), "conversationPinItem", Qt.LeftButton, label="Pin")
        self.assertEqual(["Genel 2", "Genel 0"], self.pinned(root))
        self.assertTrue(self.pin_row(root, "Genel 2").property("current"))
        # Another scope: its pin joins the same list, labelled with its project.
        self.tap(root, self.row(root, "sidebarProject", "Tez"), then=lambda: (
            self.assertEqual(["Genel 2", "Genel 0"], self.pinned(root)),                      # every scope's pins
        ))
        self.choose(root, self.row(root, "sidebarConversation", "Tez 1"), "conversationPinItem", label="Pin")
        self.assertEqual(["Tez 1", "Genel 2", "Genel 0"], self.pinned(root))
        self.assertNotIn("Tez 1", self.titles(root))
        row = self.pin_row(root, "Tez 1")
        self.assertTrue(self.child(row, "sidebarPinPlace").isVisible())
        self.assertEqual(["Tez"], [t.property("text") for t in self.descendants(row) if t.objectName() == "sidebarPinPlaceName"])
        self.assertEqual("Tez 1\nIn the Tez project", self.child(row, "sidebarRowTip").property("text"))
        # A pin of another scope opens there: the scope pick and the conversation pick.
        self.tap(root, self.pin_row(root, "Genel 0"), then=lambda: (
            self.assertEqual((self.genel[0].id, True), (bridge.conversationId, bridge.conversationProject["global"])),
            self.assertTrue(self.item(root, "sidebarGlobal").property("current")),
            self.assertTrue(self.pin_row(root, "Genel 0").property("current")),
        ))
        self.tap(root, self.pin_row(root, "Tez 1"), then=lambda: (
            self.assertEqual((self.tezc[1].id, self.tez.id), (bridge.conversationId, bridge.conversationProject["id"])),
            self.assertTrue(self.row(root, "sidebarProject", "Tez").property("current")),
            self.assertEqual("Tez 1", self.item(root, "chatHeaderTitle").property("text")),
        ))
        # Sabitlemeyi kaldır: from a pinned row of another scope, and from the header.
        self.choose(root, self.pin_row(root, "Genel 2"), "conversationPinItem", label="Unpin")
        self.assertEqual(["Tez 1", "Genel 0"], self.pinned(root))
        self.choose(root, self.item(root, "chatHeaderMenu"), "conversationPinItem", Qt.LeftButton, label="Unpin")
        self.assertEqual(["Genel 0"], self.pinned(root))
        self.assertIn("Tez 1", self.titles(root))                                        # back in its date group
        self.tap(root, self.item(root, "sidebarGlobal"))
        self.assertIn("Genel 2", self.titles(root))
        self.assertEqual([self.genel[0].id], json.loads(self.sidebar_file().read_text(encoding="utf-8"))["pins"])
        self.assertEqual(0, self.execute_calls())

    def test_vv_ten_at_most_with_the_reason_and_archive_or_delete_unpins(self) -> None:
        store = self.db()
        convs = [store.create_conversation(title=f"Sohbet {n}") for n in range(11)]
        tez = store.create_project("Tez")
        owned = store.create_conversation(tez.id, title="Tez sohbeti")
        bridge = self.bridge()
        for c in convs[:9] + [owned]:
            self.assertTrue(bridge.pinConversation(c.id))
        root = self.window(bridge)
        self.assertEqual(10, len(bridge.pinnedConversations))
        # At the limit: Sabitle is disabled with its reason, and refused.
        self.menu(root, self.row(root, "sidebarConversation", "Sohbet 10"))
        pin = self.item(root, "conversationPinItem")
        self.assertEqual(("Pin", False), (pin.property("label"), pin.property("enabled")))
        reason = self.item(root, "conversationPinReason")
        self.assertTrue(reason.isVisible())
        self.assertEqual("At most 10 conversations can be pinned: unpin one first.", reason.property("text"))
        self.tap(root, pin, then=lambda: (
            self.assertEqual(10, len(bridge.pinnedConversations)),
        ))
        self.close_menu(root)
        self.assertFalse(bridge.pinConversation(convs[10].id))
        # A pinned one's menu still unpins at the limit.
        self.menu(root, self.pin_row(root, "Sohbet 3"))
        self.assertEqual(("Unpin", True), (self.item(root, "conversationPinItem").property("label"),
                                           self.item(root, "conversationPinItem").property("enabled")))
        self.assertFalse(self.item(root, "conversationPinReason").isVisible())
        # Archive (from its pinned row's menu): the pin goes; the conversation is archived, nothing else.
        self.tap(root, self.item(root, "conversationArchiveItem"))
        self.assertIsNotNone(store.load_conversation(convs[3].id).archived_at)
        self.assertNotIn("Sohbet 3", self.pinned(root))
        self.assertNotIn(convs[3].id, json.loads(self.sidebar_file().read_text(encoding="utf-8"))["pins"])
        # A pin under a project archived afterwards stays; its menu offers the delete (the project is archived).
        store.set_project_active(tez.id, False)
        bridge.refreshProjects()
        settled(300, lambda: (
            self.assertIn("Tez sohbeti", self.pinned(root)),
        ))
        self.menu(root, self.pin_row(root, "Tez sohbeti"))
        self.assertTrue(self.item(root, "conversationDeleteItem").property("enabled"))
        self.assertFalse(self.item(root, "conversationArchiveItem").property("enabled"))
        self.close_menu(root)
        # The permanent delete (its existing preview + confirm): the pin goes.
        bridge.previewDeleteConversation(owned.id)
        pump(60)
        self.assertTrue(bridge.confirmDeleteConversation(owned.id))
        pump(300)
        self.assertIsNone(store.load_conversation(owned.id))
        self.assertNotIn("Tez sohbeti", self.pinned(root))
        self.assertNotIn(owned.id, json.loads(self.sidebar_file().read_text(encoding="utf-8"))["pins"])
        # Below the limit again: Sabitle is back.
        self.menu(root, self.row(root, "sidebarConversation", "Sohbet 10"))
        self.assertTrue(self.item(root, "conversationPinItem").property("enabled"))
        self.assertFalse(self.item(root, "conversationPinReason").isVisible())
        self.close_menu(root)
        self.assertEqual(0, self.execute_calls())


class PinOrderTests(F6Case):
    def test_ww_drag_and_the_menu_reorder_a_new_pin_tops_and_the_order_persists(self) -> None:
        store = self.db()
        convs = {name: store.create_conversation(title=name) for name in ("A", "B", "C", "D")}
        bridge = self.bridge()
        root = self.window(bridge)
        for name in ("A", "B", "C"):
            self.choose(root, self.row(root, "sidebarConversation", name), "conversationPinItem")
        self.assertEqual(["C", "B", "A"], self.pinned(root))                             # each new pin on top
        self.choose(root, self.row(root, "sidebarConversation", "D"), "conversationPinItem")
        self.assertEqual(["D", "C", "B", "A"], self.pinned(root))
        # Hover shows the grip in place of the pin.
        row = self.pin_row(root, "D")
        self.hover(root, self.child(row, "sidebarPinGripArea"))
        settled(200, lambda: (
            self.assertTrue(self.child(row, "sidebarPinGrip").isVisible()),
            self.assertFalse(self.child(row, "sidebarPinIcon").isVisible()),
        ))
        # A real mouse drag: D into the gap above A; the drop line shows the gap while it moves.
        seen = {}

        def probe():
            line = self.item(root, "sidebarPinDropLine")
            seen.update(line=line.isVisible(), y=self.rect(line).center().y(), dragging=self.pin_row(root, "D").property("dragging"))
        target = self.gap_y(root, 3)
        self.drag(root, self.pin_row(root, "D"), target, probe)
        self.assertEqual((True, True), (seen["line"], seen["dragging"]))
        self.assertAlmostEqual(target, seen["y"], delta=4)
        self.assertEqual(["C", "B", "D", "A"], self.pinned(root))
        self.assertFalse(self.item(root, "sidebarPinDropLine").isVisible())
        # Down to the end, and up to the top.
        self.drag(root, self.pin_row(root, "C"), self.gap_y(root, 4))
        self.assertEqual(["B", "D", "A", "C"], self.pinned(root))
        self.drag(root, self.pin_row(root, "A"), self.gap_y(root, 0))
        self.assertEqual(["A", "B", "D", "C"], self.pinned(root))
        # A drop on its own place moves nothing; a press on the grip without a move is a click (it opens).
        self.drag(root, self.pin_row(root, "B"), self.gap_y(root, 1) + 8)
        self.assertEqual(["A", "B", "D", "C"], self.pinned(root))
        self.tap(root, self.child(self.pin_row(root, "B"), "sidebarPinGripArea"), then=lambda: (
            self.assertEqual(convs["B"].id, bridge.conversationId),
        ))
        # The menu: Yukarı / Aşağı taşı, only where it can move.
        self.menu(root, self.pin_row(root, "A"))
        self.assertEqual((False, True), (self.item(root, "conversationPinUpItem").isVisible(),
                                         self.item(root, "conversationPinDownItem").isVisible()))
        self.tap(root, self.item(root, "conversationPinDownItem"), then=lambda: (
            self.assertEqual(["B", "A", "D", "C"], self.pinned(root)),
        ))
        self.menu(root, self.pin_row(root, "C"))
        self.assertEqual((True, False), (self.item(root, "conversationPinUpItem").isVisible(),
                                         self.item(root, "conversationPinDownItem").isVisible()))
        self.tap(root, self.item(root, "conversationPinUpItem"), then=lambda: (
            self.assertEqual(["B", "A", "C", "D"], self.pinned(root)),
        ))
        self.menu(root, self.pin_row(root, "A"))
        self.assertEqual("Move up", self.item(root, "conversationPinUpItem").property("label"))
        self.assertEqual("Move down", self.item(root, "conversationPinDownItem").property("label"))
        self.close_menu(root)
        # A new pin goes to the top of the manual order.
        other = store.create_conversation(title="E")
        bridge._refresh_conversations()
        pump(300)
        self.choose(root, self.row(root, "sidebarConversation", "E"), "conversationPinItem")
        self.assertEqual(["E", "B", "A", "C", "D"], self.pinned(root))
        # The order persists across a reopen.
        root.hide()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual(["E", "B", "A", "C", "D"], self.pinned(root))
        self.assertEqual([other.id] + [convs[n].id for n in ("B", "A", "C", "D")],
                         json.loads(self.sidebar_file().read_text(encoding="utf-8"))["pins"])
        self.assertEqual(0, self.execute_calls())

    def test_xx_a_collapsed_pinned_section_keeps_the_open_pin_and_does_not_drag(self) -> None:
        store = self.db()
        for name in ("A", "B", "C"):
            store.create_conversation(title=name)
        bridge = self.bridge()
        for c in store.list_conversations(project_id=None):
            bridge.pinConversation(c.id)
        root = self.window(bridge)
        self.assertEqual(["A", "B", "C"], self.pinned(root))
        self.tap(root, self.head(root, "pinned"), then=lambda: (
            self.assertFalse(bridge.sidebarGroups["pinned"]),
            self.assertEqual(["C"], self.pinned(root)),                                       # the open one stays
            self.assertEqual(["· 3"], self.texts(root, "sidebarGroupCount")),
        ))
        self.drag(root, self.pin_row(root, "C"), self.gap_y(root, 0) - 40)
        self.assertEqual(["A", "B", "C"], [p["title"] for p in bridge.pinnedConversations])
        self.tap(root, self.head(root, "pinned"), then=lambda: (
            self.assertEqual(["A", "B", "C"], self.pinned(root)),
            self.assertEqual(0, self.execute_calls()),
        ))


    def test_ccc_a_drag_held_at_the_lists_edge_scrolls_it_to_every_gap(self) -> None:
        store = self.db()
        for n in range(6):
            store.create_conversation(title=f"Sabit {n}")
        bridge = self.bridge()
        for c in reversed(store.list_conversations(project_id=None)):
            bridge.pinConversation(c.id)
        root = self.window(bridge)
        self.resize_still(root, 1100, 700, 900)
        order = self.pinned(root)
        listing = self.box(root, "sidebarConversationList")
        flick = self.item(root, "sidebarConversationList")
        self.assertLess(flick.height(), 6 * 38)                                          # not all six in view
        row = self.pin_row(root, order[0])
        grip = self.child(row, "sidebarPinGripArea")
        start = grip.mapToScene(QPointF(grip.width() / 2, grip.height() / 2)).toPoint()
        QTest.mousePress(root, Qt.LeftButton, Qt.NoModifier, start)
        pump(50)
        edge = QPoint(start.x(), round(listing.bottom() - 6))
        for y in range(start.y(), edge.y() + 1, 12):
            QTest.mouseMove(root, QPoint(start.x(), y))
            pump(30)
        QTest.mouseMove(root, edge)
        self.assertTrue(settle(lambda: flick.property("contentY") >= flick.property("contentHeight") - flick.height() - 0.5))
        # Held at the bottom: the gap below the last pin is the drop target.
        bottom = self.gap_y(root, 6)
        QTest.mouseMove(root, QPoint(start.x(), round(min(bottom, listing.bottom() - 6))))
        pump(100)
        QTest.mouseRelease(root, Qt.LeftButton, Qt.NoModifier, QPoint(start.x(), round(min(bottom, listing.bottom() - 6))))
        settled(350, lambda: (
            self.assertEqual(order[1:] + order[:1], [p["title"] for p in bridge.pinnedConversations]),
            self.assertEqual(0, self.execute_calls()),
        ))


class SidebarPrefsStorageTests(F6Case):
    def test_yy_only_the_sidebar_file_is_written_pruned_on_read_and_old_files_load(self) -> None:
        from datetime import datetime

        from jarvis.ui.preferences import UiPreferences, canonical_preferences, validate_preferences
        now = datetime.now().astimezone()                                       # THIZ2: once per test
        store = self.db()
        tez = store.create_project("Tez")
        keep = [store.create_conversation(title=f"Genel {n}") for n in range(3)]
        owned = store.create_conversation(tez.id, title="Tez 0")
        bridge = self.bridge()
        bridge.setTheme("dark")
        bridge.setLanguage("en-US")                              # a main document on disk
        root = self.window(bridge)
        self.fixed_today(root, now)
        main_before = self.prefs_path.read_bytes()
        db_before = dump(self.database)
        # Pin, reorder, collapse - all by mouse.
        self.choose(root, self.row(root, "sidebarConversation", "Genel 0"), "conversationPinItem")
        self.choose(root, self.row(root, "sidebarConversation", "Genel 1"), "conversationPinItem")
        self.choose(root, self.pin_row(root, "Genel 1"), "conversationPinDownItem")
        self.tap(root, self.head(root, "today"))
        self.tap(root, self.head(root, "pinned"))
        self.tap(root, self.head(root, "pinned"))
        bridge.pinConversation(owned.id)
        pump(200)
        # The conversation store and the main preferences document are byte-identical.
        self.assertEqual(db_before, dump(self.database))
        self.assertEqual(main_before, self.prefs_path.read_bytes())
        doc = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.assertEqual(set(canonical_preferences()), set(doc))                         # the release's exact keys
        self.assertEqual(doc, validate_preferences(doc))                                 # accepted whole, never defaults
        self.assertEqual("dark", UiPreferences(self.prefs_path).data["theme"])
        # The sidebar file: its keys and shape.
        side = json.loads(self.sidebar_file().read_text(encoding="utf-8"))
        self.assertEqual({"version": 1, "pins": [owned.id, keep[0].id, keep[1].id], "pins_open": True,
                          "groups": {"global": {"today": False, "yesterday": True, "earlier": False}}}, side)
        # Unknown, archived and deleted ids (and an unknown scope) are pruned on read.
        archived = store.create_conversation(title="Arşivlenecek")
        store.archive_conversation(archived.id)
        side["pins"] = ["c-unknown", keep[2].id, archived.id] + side["pins"]
        side["groups"]["p-" + "0" * 32] = {"today": False, "yesterday": False, "earlier": True}
        self.sidebar_file().write_text(json.dumps(side), encoding="utf-8")
        root.hide()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual([keep[2].id, owned.id, keep[0].id, keep[1].id], [p["id"] for p in bridge.pinnedConversations])
        self.assertEqual(["Genel 2", "Tez 0", "Genel 0", "Genel 1"], self.pinned(root))
        self.assertEqual({"global"}, set(bridge.prefs.sidebar["groups"]))
        self.assertNotIn("c-unknown", bridge.prefs.sidebar["pins"])
        # A malformed sidebar file loads as the defaults, never an error.
        root.hide()
        self.sidebar_file().write_text('{"version": 1, "pins": "nope"}', encoding="utf-8")
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual(([], {"pinned": True, "today": True, "yesterday": True, "earlier": False}),
                         (bridge.pinnedConversations, bridge.sidebarGroups))
        self.assertEqual("dark", bridge.theme)
        # A preferences file from before F6 (no sidebar file at all) loads with every choice kept.
        root.hide()
        self.sidebar_file().unlink()
        old = canonical_preferences()
        old.update(theme="dark", language="tr-TR")
        self.prefs_path.write_text(json.dumps(old, indent=2), encoding="utf-8")
        bridge = self.bridge()
        self.assertEqual(("dark", "tr-TR", []), (bridge.theme, bridge.language, bridge.pinnedConversations))
        self.assertEqual({"pinned": True, "today": True, "yesterday": True, "earlier": False}, bridge.sidebarGroups)
        self.assertFalse(self.sidebar_file().exists())                                   # nothing written by reading
        bridge.setLanguage("en-US")
        self.assertEqual(0, self.execute_calls())


class PinFitTests(F6Case):
    def test_zz_ten_long_pins_fit_at_every_size_in_space(self) -> None:
        self.sweep_zz(("space",))

    def test_zz_ten_long_pins_fit_at_every_size_in_dark(self) -> None:
        self.sweep_zz(("dark",))

    def sweep_zz(self, themes) -> None:
        # THIZ: one theme per test method, so the parallel runner can spread them.
        store = self.db()
        longp = store.create_project(LONG_PROJECT)
        short = store.create_project("Ev")
        pins = [store.create_conversation(longp.id, title=f"{LONG_TITLE} {n}") for n in range(4)]
        pins += [store.create_conversation(short.id, title=f"Ev sohbeti {n}") for n in range(3)]
        pins += [store.create_conversation(title=f"{LONG_TITLE} genel {n}") for n in range(3)]
        for n in range(4):
            store.create_conversation(title=f"Liste {n}")
        bridge = self.bridge()
        for c in pins:
            self.assertTrue(bridge.pinConversation(c.id))
        root = self.window(bridge)
        for theme in themes:
            bridge.setTheme(theme)
            for width, height in SIZES:
                self.resize_still(root, width, height, 900)
                what = f"{theme} {width}x{height}"
                window = QRectF(0, 0, width, height)
                with self.subTest(at=what):
                    self.frame(root, what)
                    listing = self.box(root, "sidebarConversationList")
                    for tile in TILES:
                        self.apart(listing, self.box(root, "rail" + tile), what)
                    self.inside(self.box(root, "sidebarNav"), listing, what)
                    # Whole rows inside the list at rest; scrolled to its end, the last row is reachable whole.
                    flick = self.item(root, "sidebarConversationList")
                    end = max(0, flick.property("contentHeight") - flick.height())
                    flick.setProperty("contentY", end)
                    pump(200)
                    last = self.shown(root, "sidebarConversation")[-1]
                    self.inside(listing, self.rect(last), what + " last row")
                    self.no_clipped_text(root, last, what + " last row")
                    for contentY in (0,):
                        flick.setProperty("contentY", contentY)
                        pump(200)
                        whole = 0
                        for name in ("sidebarPinnedConversation", "sidebarConversation", "sidebarPinnedHead", "sidebarGroupHead"):
                            for item in self.shown(root, name):
                                r = self.rect(item)
                                cut = r.intersected(listing)
                                if cut.height() > 0.5:
                                    self.assertAlmostEqual(r.height(), cut.height(), delta=0.5, msg=(what, name, contentY))
                                    self.no_clipped_text(root, item, f"{what} {name}")
                                    whole += 1
                        self.assertGreaterEqual(whole, 2, what)
                    flick.setProperty("contentY", 0)
                    settled(200, lambda: (
                        self.assertEqual(10, len(self.shown(root, "sidebarPinnedConversation"))),
                    ))
                    # A pinned row: the title keeps six characters, the project label too, and they never touch.
                    for row in self.shown(root, "sidebarPinnedConversation"):
                        title = self.child(row, "sidebarPinnedTitle")
                        self.assertGreaterEqual(self.painted(title), 6, (what, row.property("title")))
                        place = self.child(row, "sidebarPinPlace")
                        if place.isVisible():
                            name = next(t for t in self.descendants(place) if t.objectName() == "sidebarPinPlaceName")
                            self.assertGreaterEqual(self.painted(name), min(6, len(name.property("text"))), what)
                            self.assertLessEqual(title.mapToScene(QPointF(title.width(), 0)).x(),
                                                 self.rect(place).left() - 4, what)
                            self.inside(self.rect(row), self.rect(place), what)
                        self.assertTrue(self.child(row, "sidebarRowTip").property("active"))
                    # The menu with its pin items, on a pinned row, inside the window and unclipped.
                    menu = self.menu(root, self.shown(root, "sidebarPinnedConversation")[1])
                    for name in ("conversationPinItem", "conversationPinUpItem", "conversationPinDownItem"):
                        self.assertTrue(self.item(root, name).isVisible(), name)
                    body = self.rect(menu.property("background"))
                    self.inside(window.adjusted(7.5, 7.5, -7.5, -7.5), body, what + " menu")
                    self.no_clipped_text(root, menu.property("contentItem"), what + " pin menu")
                    self.close_menu(root)
        self.assertEqual(0, self.execute_calls())

    def test_aaa_every_f6_action_has_a_mouse_path_at_every_size_in_space(self) -> None:
        self.sweep_aaa(("space",))

    def test_aaa_every_f6_action_has_a_mouse_path_at_every_size_in_dark(self) -> None:
        self.sweep_aaa(("dark",))

    def sweep_aaa(self, themes) -> None:
        # THIZ: one theme per test method, so the parallel runner can spread them.
        from datetime import datetime, timedelta
        store = self.db()
        tez = store.create_project("Tez")
        now = datetime.now().astimezone()                                       # THIZ2: once per test
        old = store.create_conversation(title="Eski")
        self.stamp(old.id, now - timedelta(days=5))
        for n in range(3):
            store.create_conversation(title=f"Genel {n}")
        store.create_conversation(tez.id, title="Tez 0")
        bridge = self.bridge()
        root = self.window(bridge)
        self.fixed_today(root, now)
        keys = []
        real = QTest.keyClick
        with mock.patch.object(QTest, "keyClick", side_effect=lambda *a, **k: (keys.append(a), real(*a, **k))):
            for theme in themes:
                bridge.setTheme(theme)
                for width, height in SIZES:
                    self.resize_still(root, width, height, 900)
                    with self.subTest(theme=theme, size=(width, height)):
                        self.tap(root, self.item(root, "sidebarGlobal"), until=lambda: bridge.conversationProject["global"])
                        self.tap(root, self.head(root, "earlier"), until=lambda: "Eski" in self.titles(root))   # expand
                        self.assertIn("Eski", self.titles(root))
                        self.tap(root, self.head(root, "earlier"), until=lambda: "Eski" not in self.titles(root))  # collapse
                        self.assertNotIn("Eski", self.titles(root))
                        self.choose(root, self.row(root, "sidebarConversation", "Genel 0"), "conversationPinItem")
                        self.choose(root, self.item(root, "chatHeaderMenu"), "conversationPinItem", Qt.LeftButton)
                        self.assertEqual(["Genel 2", "Genel 0"], self.pinned(root))
                        self.choose(root, self.pin_row(root, "Genel 2"), "conversationPinDownItem")      # menu move
                        self.assertEqual(["Genel 0", "Genel 2"], self.pinned(root))
                        self.drag(root, self.pin_row(root, "Genel 2"), self.gap_y(root, 0))               # drag
                        self.assertEqual(["Genel 2", "Genel 0"], self.pinned(root))
                        self.tap(root, self.row(root, "sidebarProject", "Tez"), until=lambda: bridge.conversationProject["name"] == "Tez")
                        self.tap(root, self.head(root, "today"), until=lambda: not bridge.sidebarGroups["today"])  # a project's group
                        self.tap(root, self.head(root, "today"), until=lambda: bridge.sidebarGroups["today"])
                        self.tap(root, self.pin_row(root, "Genel 0"), until=lambda: bridge.conversationTitle == "Genel 0")  # another scope's pin
                        self.assertEqual(bridge.conversationTitle, "Genel 0")
                        self.tap(root, self.head(root, "pinned"), until=lambda: not bridge.sidebarGroups["pinned"])
                        self.tap(root, self.head(root, "pinned"), until=lambda: bridge.sidebarGroups["pinned"])
                        for title in ("Genel 2", "Genel 0"):                                         # unpin
                            self.choose(root, self.pin_row(root, title), "conversationPinItem")
                        self.assertEqual([], self.pinned(root))
        self.assertEqual([], keys)                                                                    # no shortcut anywhere
        self.assertEqual(0, self.execute_calls())

    def test_bbb_the_f6_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        for source, turkish in (("Pin", "Sabitle"), ("Unpin", "Sabitlemeyi kaldır"), ("Move up", "Yukarı taşı"),
                                ("Move down", "Aşağı taşı"),
                                ("This conversation is archived: unarchive it to pin it.",
                                 "Bu sohbet arşivlendi: sabitlemek için arşivden çıkarın."),
                                ("At most %1 conversations can be pinned: unpin one first.",
                                 "En fazla %1 sohbet sabitlenebilir: önce birinin sabitlemesini kaldırın.")):
            self.assertEqual(turkish, tx("ConversationMenu", source))
        for source, turkish in (("PINNED", "SABİTLENDİ"), ("In the %1 project", "%1 projesinde"),
                                ("Expanded", "Açık"), ("Collapsed", "Kapalı")):
            self.assertEqual(turkish, tx("Sidebar", source))


class ProjectListOffsetTests(F6Case):
    """F7-3 (P-F6-1): after a resize across the 1600 px breakpoint the list was clamped with its
    old content height (compact rows) against its new height and kept that off-grid offset while
    no listed project was selected (Genel, an archived project): half a row at 1280 and 1100."""

    def offset(self, root) -> tuple:
        listing = self.item(root, "sidebarProjectList")
        step = self.shown(root, "sidebarProject")[0].height() + 2
        return listing.property("contentY"), step, max(0.0, listing.property("contentHeight") - listing.height())

    def test_ddd_the_project_list_opens_at_zero_and_stays_on_whole_rows_across_sizes(self) -> None:
        store = self.db()
        projects = [store.create_project(f"Proje {n:02d}") for n in range(10)]
        for project in projects:
            store.create_conversation(project.id, title=f"Sohbet {project.name}")
        old = store.create_project("Arşivli")
        store.create_conversation(old.id, title="Arşivli sohbet")
        store.set_project_active(old.id, False)
        bridge = self.bridge()
        bridge.openProjectScope("")
        pump(200)
        root = self.window(bridge)
        # Opened at every size: offset 0.
        for width, height in SIZES:
            root.resize(width, height)
            pump(700)
            self.assertEqual(0, self.offset(root)[0], (width, height))
        # Scrolled to the last project, then a scope that is not a row (Genel; an archived project),
        # then across the breakpoint and back: whole rows, never past the end.
        for leave in (lambda: self.tap(root, self.item(root, "sidebarGlobal")),
                      lambda: (bridge.openConversation(store.list_conversations(project_id=old.id)[0].id), pump(400))):
            root.resize(1100, 700)
            pump(700)
            bridge.openProjectScope(projects[-1].id)
            pump(500)                  # the list scrolls to show it
            self.assertGreater(self.offset(root)[0], 0)
            leave()
            for width, height in ((1920, 1080), (1280, 760), (1100, 700), (1920, 1080), (1100, 700)):
                root.resize(width, height)
                pump(700)
                y, step, end = self.offset(root)
                with self.subTest(size=(width, height), y=y, step=step):
                    self.assertAlmostEqual(0, y % step, delta=0.5)
                    self.assertLessEqual(y, end + 0.5)
                    top = self.box(root, "sidebarProjectList").top()
                    first = min(self.rect(r).top() for r in self.shown(root, "sidebarProject") if self.rect(r).bottom() > top + 0.5)
                    self.assertAlmostEqual(top, first, delta=0.5)             # the first row in view is whole
        self.assertEqual(0, self.execute_calls())


class OneStepDeleteTests(F6Case):
    """F7-1 (Salih 2026-10-05): a Global conversation's menu offers "Kalıcı sil…" at once - the same
    confirm dialog, the same Core path; a project's conversation still needs its archive."""

    def dialog_open(self, root) -> bool:
        return settle(lambda: any(i.isVisible() for i in self.all_items(root, "deleteConfirm")))

    def test_eee_a_global_conversation_is_deleted_in_one_step_through_the_same_dialog(self) -> None:
        store = self.db()
        g0, g1 = (store.create_conversation(title=f"Genel {n}") for n in range(2))
        tez = store.create_project("Tez")
        t0 = store.create_conversation(tez.id, title="Tez sohbeti")
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual(g1.id, bridge.conversationId)
        calls, real = [], bridge.backend.conversation_delete
        bridge.backend.conversation_delete = lambda *a, **k: calls.append((a, k)) or real(*a, **k)
        # A row's menu: Kalıcı sil… is offered; it opens the existing dialog, nothing is deleted yet.
        self.menu(root, self.row(root, "sidebarConversation", "Genel 0"))
        item = self.item(root, "conversationDeleteItem")
        self.assertEqual(("Delete permanently…", True), (item.property("label"), item.isEnabled()))
        self.assertFalse(self.item(root, "conversationDeleteReason").isVisible())
        self.tap(root, item)
        self.assertTrue(self.dialog_open(root))
        self.assertEqual((g0.id, "Genel 0", ""), (bridge.deletePreview["conversation_id"], bridge.deletePreview["title"],
                                                  bridge.deletePreview["blocked"]))
        self.assertFalse(store.is_conversation_deleted(g0.id))
        self.assertIsNone(store.load_conversation(g0.id).archived_at)                     # never archived first
        self.tap(root, next(i for i in self.all_items(root, "deleteConfirm") if i.isVisible()))
        self.assertTrue(store.is_conversation_deleted(g0.id))
        self.assertEqual(1, len(calls))
        self.assertEqual({"confirm": g0.id, "expected_next_seq": 1, "expected_title": "Genel 0"}, calls[0][1])
        self.assertEqual(g1.id, bridge.conversationId)                                    # the open one stays open
        self.assertNotIn("Genel 0", self.titles(root))
        # The header "…" on the open Global conversation: offered; Vazgeç writes nothing.
        self.menu(root, self.item(root, "chatHeaderMenu"), Qt.LeftButton)
        self.assertTrue(self.item(root, "conversationDeleteItem").isEnabled())
        self.tap(root, self.item(root, "conversationDeleteItem"))
        self.assertTrue(self.dialog_open(root))
        before = dump(self.database)
        self.tap(root, next(i for i in self.all_items(root, "deleteCancel") if i.isVisible()))
        self.assertEqual(before, dump(self.database))
        self.assertEqual(1, len(calls))
        # A pinned Global conversation seen from a project scope: offered too.
        bridge.pinConversation(g1.id)
        self.tap(root, self.row(root, "sidebarProject", "Tez"))
        self.menu(root, self.pin_row(root, "Genel 1"))
        self.assertTrue(self.item(root, "conversationDeleteItem").isEnabled())
        self.close_menu(root)
        # A project's conversation: unchanged - archive first, with its reason; Core refuses it too.
        self.menu(root, self.row(root, "sidebarConversation", "Tez sohbeti"))
        self.assertFalse(self.item(root, "conversationDeleteItem").isEnabled())
        self.assertEqual("Archive it first", self.item(root, "conversationDeleteReason").property("text"))
        self.close_menu(root)
        bridge.previewDeleteConversation(t0.id)
        pump(60)
        self.assertIn("Archive this conversation first", bridge.deletePreview["blocked"])
        self.assertFalse(bridge.confirmDeleteConversation(t0.id))
        self.assertFalse(store.is_conversation_deleted(t0.id))
        self.assertEqual(1, len(calls))
        self.assertEqual(0, self.execute_calls())


class RowMenuButtonTests(F6Case):
    """F7-2 (Salih 2026-10-05): a conversation row's "⋯" - on hover, keyboard focus or while its menu is
    open, in place of the time (or the pin's project); the same menu as a right-click, next to the row;
    the row is not opened. Conversation rows only."""

    def button(self, row):
        return self.child(row, "sidebarRowMenuButton")

    def away(self, root) -> None:
        QTest.mouseMove(root, QPoint(root.width() // 2, root.height() - 4))
        pump(200)

    @ambient_on          # THIZ: with ambient off its menu opens 100 px off the row after a resize (reported, not ours to fix)
    def test_fff_the_row_button_shows_on_hover_and_opens_that_rows_menu_without_opening_it(self) -> None:
        store = self.db()
        tez = store.create_project("Tez")
        pinned = store.create_conversation(tez.id, title="Tez sohbeti")
        a = store.create_conversation(title="Kat malikleri toplantısı için hazırlanacak belgeler ve aidatlar")
        b = store.create_conversation(title="Açık sohbet")
        bridge = self.bridge()
        bridge.pinConversation(pinned.id)
        root = self.window(bridge)
        self.assertEqual(b.id, bridge.conversationId)
        for width, height in SIZES:
            self.resize_still(root, width, height, 700)
            with self.subTest(size=(width, height)):
                self.away(root)
                row = self.row(root, "sidebarConversation", a.title)
                self.assertFalse(self.button(row).isVisible())
                self.assertEqual(1.0, self.child(row, "sidebarConversationTime").property("opacity"))
                # Hover: "⋯" in place of the time, inside the row, the title clear of it.
                self.into_view(root, row)
                self.hover(root, row)
                pump(200)
                button = self.button(row)
                self.assertTrue(button.isVisible())
                self.assertEqual(0.0, self.child(row, "sidebarConversationTime").property("opacity"))
                self.inside(self.rect(row), self.rect(button), "button in the row")
                title = self.child(row, "sidebarConversationTitle")
                self.assertLessEqual(self.rect(title).left() + title.width(), self.rect(button).left() - 4)
                self.assertGreaterEqual(self.painted(title), 6)
                self.assertEqual("Conversation actions", self.child(button, "sidebarRowMenuTip").property("text"))
                # Its click: that row's menu, next to the row; the open conversation does not change.
                self.tap(root, button)
                menu = self.popup_named(root, "conversationSwitcherContext")
                self.assertTrue(settle(lambda: bool(menu.property("opened"))))
                self.assertEqual((a.id, b.id), (self.item(root, "conversationMenu").property("targetId"), bridge.conversationId))
                self.assertEqual(a.title, self.item(root, "conversationContextHeader").property("text"))
                body, box = self.rect(menu.property("background")), self.rect(row)
                self.assertGreaterEqual(body.left(), box.right() - 0.5)
                self.assertAlmostEqual(box.top(), body.top(), delta=1 if body.bottom() < height - 8.5 else height)
                self.inside(QRectF(0, 0, width, height), body, "menu in the window")
                self.assertFalse(self.child(self.button(row), "sidebarRowMenuTip").property("active"))   # never over the menu
                self.away(root)
                self.assertTrue(self.button(row).isVisible())                              # its menu is open
                self.close_menu(root)
                self.assertFalse(self.button(row).isVisible())
                # Keyboard focus shows it too.
                row.forceActiveFocus()
                pump(200)
                self.assertTrue(self.button(row).isVisible())
                row.setProperty("focus", False)
                pump(100)
                self.assertFalse(self.button(row).isVisible())
                # A pinned row: in place of its project label.
                pin = self.pin_row(root, "Tez sohbeti")
                self.into_view(root, pin)
                self.hover(root, pin)
                pump(200)
                self.assertTrue(self.button(pin).isVisible())
                self.assertEqual(0.0, self.child(pin, "sidebarPinPlace").property("opacity"))
                self.tap(root, self.button(pin), until=lambda: bool(menu.property("opened")))   # THIZ: open before it closes
                self.assertEqual((pinned.id, b.id), (self.item(root, "conversationMenu").property("targetId"), bridge.conversationId))
                self.close_menu(root)
                # Project rows have none.
                self.hover(root, self.shown(root, "sidebarProject")[0])
                settled(200, lambda: (
                    self.assertEqual([], [i for i in self.descendants(self.shown(root, "sidebarProject")[0])
                                          if i.objectName() == "sidebarRowMenuButton" and i.isVisible()]),
                ))
        self.assertEqual(0, self.execute_calls())

    def test_ggg_the_row_button_string_is_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Sohbet işlemleri", tx("Sidebar", "Conversation actions"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
