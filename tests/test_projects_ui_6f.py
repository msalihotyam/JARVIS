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

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import sqlite3
import unittest
from datetime import timezone
from unittest import mock

from PySide6.QtCore import QMetaObject, QObject, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF
from PySide6.QtTest import QTest

from jarvis.conversation import append_turn
from jarvis.memory import SQLiteMemoryStore
from tests.test_projects_ui import ProjectsUiCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import _APP, pump


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
        bridge.submitText("Bu projeyi incele."); pump(400)
        self.assertTrue(bridge.approvalPending)
        value, line, tip = self.tile(root, "Tasks")
        self.assertEqual(("1", "approval waiting"), (value, line))
        self.assertEqual(QColor("#ffb347").name(), QColor(self.item(root, "railCountTasks").property("color")).name())   # amber
        self.assertEqual(QColor("#ffb347").name(), QColor(self.item(root, "railLineTasks").property("color")).name())
        feed = self.item(root, "conversationFeed")
        feed.setProperty("followEnd", False)
        self.press(root, "railTasks")
        self.assertFalse(root.property("peekOpen"))
        self.assertTrue(feed.property("followEnd"))                     # brought to the end, where the approval is
        self.assertTrue(self.item(root, "safetyCard_approval").isVisible())
        # Approved and finished: the last task, its result, name and time.
        bridge.approve(); pump(500)
        self.assertFalse(bridge.approvalPending)
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
        bridge.stateChanged.emit(); pump(1300)
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
        bridge.openConversation(global_one.id); pump(400)
        self.assertEqual(("Global", "no project context"), self.tile(root, "Focus")[:2])
        bridge.openConversation(conversations[0].id); pump(400)
        facts = bridge.conversationProject
        self.assertEqual((True, 2), (facts["instruction"], facts["file_count"]))
        value, line, tip = self.tile(root, "Focus")
        self.assertEqual("Ev tadilatı", value)
        self.assertEqual("Ev tadilatı · instruction ✓ · 2 files · 3 conversations", tip)
        self.assertIn(line, ("instruction ✓ · 2 files · 3 conversations", "2 files · 3 conversations", "3 conversations"))
        for width, height in SIZES:
            root.resize(width, height); pump(500)
            with self.subTest(size=(width, height)):
                self.assertFalse(self.item(root, "railLineFocus").property("truncated"))

    def test_p_plan_shows_the_next_job_in_genel_and_in_a_project(self) -> None:
        from datetime import datetime, timedelta
        store, project, conversations, global_one = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).astimezone()
        later = tomorrow + timedelta(days=3)
        for owner in (global_one.id, conversations[0].id):
            bridge.openConversation(owner); pump(300)
            with self.subTest(scope=owner):
                bridge._jobs = []; bridge.dataChanged.emit(); pump(200)
                self.assertEqual(("—", "No scheduled job"), self.tile(root, "Plan")[:2])
                bridge._jobs = [{"id": "j-2", "label": "Yedek", "enabled": True, "next_due_at_utc": later.isoformat()},
                                {"id": "j-1", "label": "Haftalık rapor", "enabled": True, "next_due_at_utc": tomorrow.isoformat()},
                                {"id": "j-3", "label": "Kapalı", "enabled": False, "next_due_at_utc": tomorrow.isoformat()}]
                bridge.dataChanged.emit(); pump(200)
                value, line, tip = self.tile(root, "Plan")
                self.assertEqual("09:00", value)
                self.assertIn(line, ("Haftalık rapor · tomorrow", "Haftalık rapor"))
                self.assertEqual("Haftalık rapor · tomorrow 09:00", tip)

    def test_q_memory_shows_the_status_and_this_scopes_records(self) -> None:
        store, project, conversations, global_one = self.scoped()
        bridge = self.bridge()
        bridge.refreshConfig()
        root = self.window(bridge)
        bridge.openConversation(conversations[0].id); pump(400)
        self.assertEqual(2, bridge.conversationProject["memory_count"])
        value, line, tip = self.tile(root, "Memory")
        self.assertEqual(("Available", "2 records"), (value, line))
        bridge.openConversation(global_one.id); pump(400)
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

    def tap(self, root, item, button=Qt.LeftButton) -> None:
        QTest.mouseClick(root, button, Qt.NoModifier, item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())
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


class SidebarNavTests(SidebarNavCase):
    def test_t_whole_rows_no_clip_no_overlap_and_long_names_elide_with_a_tip(self) -> None:
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
        for theme in ("space", "dark"):
            bridge.setTheme(theme)
            for width, height in SIZES:
                root.resize(width, height); pump(700)
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
                    # 5 at the 1920 class, 3 below; fewer only while the conversations would keep
                    # less than two whole rows (1100x700 until the scope group leaves the bar, F4).
                    if (width, height) == (1100, 700):
                        self.assertIn(shown["sidebarProject"], (2, 3))
                    else:
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
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        cases = (("Bugün ilk", midnight + timedelta(seconds=30)),                # just after midnight: today
                 ("Dün son", midnight - timedelta(seconds=30)),                  # just before it: yesterday
                 ("Dün ilk", midnight - timedelta(days=1) + timedelta(seconds=30)),
                 ("Daha eski", midnight - timedelta(days=1, seconds=30)))        # the day before: earlier
        for title, when in cases:
            self.stamp(store.create_conversation(title=title).id, when)
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._refresh_conversations(); pump(300)
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
        bridge._refresh_conversations(); pump(300)
        self.assertEqual(["Today", "Yesterday"], self.texts(root, "sidebarGroupLabel"))

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
        self.assertEqual("Genel 0", self.item(root, "conversationSwitcherText").property("text"))
        # The busy guard: no pick while a run is busy.
        bridge._busy = True
        try:
            self.tap(root, self.row(root, "sidebarConversation", "Genel 1"))
            self.tap(root, self.row(root, "sidebarProject", "Tek"))
            self.assertEqual(self.global_live[0].id, bridge.conversationId)
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
        self.tap(root, self.row(root, "sidebarProject", "Tek"))
        self.assertEqual("New conversation in Tek", plus.property("tip"))
        self.tap(root, plus)
        self.assertEqual((self.one.id, 2), (bridge.conversationProject["id"], len(store.list_conversations(project_id=self.one.id))))
        # Under an archived project: dimmed, the reason in its tip, and a click does nothing.
        bridge.openConversation(store.list_conversations(project_id=self.old.id)[0].id); pump(300)
        self.assertFalse(plus.property("allowed"))
        self.assertAlmostEqual(0.38, plus.property("opacity"), places=2)
        self.assertEqual("The project is archived: unarchive it to start a new conversation.", plus.property("tip"))
        self.tap(root, plus)
        self.assertEqual(1, len(store.list_conversations(project_id=self.old.id)))
        self.assertTrue(self.item(root, "sidebarScopeChip").property("archived"))
        self.assertEqual("Eski · archived", self.item(root, "sidebarScopeTip").property("text"))
        # While busy: the same.
        self.tap(root, self.item(root, "sidebarGlobal"))
        bridge._busy = True
        try:
            bridge.stateChanged.emit(); pump(100)
            self.assertEqual("Wait for the current run to finish.", plus.property("tip"))
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
        self.tap(root, self.row(root, "sidebarProject", "Çok"))
        self.assertEqual("2", self.link(root, "sidebarArchive"))
        # archive / unarchive
        victim = next(r for r in self.live if r.id != bridge.conversationId)
        self.assertTrue(bridge.archiveConversation(victim.id)); pump(300)
        self.assertEqual(("2", "3"), (count("Çok"), self.link(root, "sidebarArchive")))
        self.assertNotIn(victim.title, self.titles(root))
        self.assertTrue(bridge.unarchiveConversation(victim.id)); pump(300)
        self.assertEqual(("3", "2"), (count("Çok"), self.link(root, "sidebarArchive")))
        # permanent delete of an archived one
        gone = next(r.id for r in store.list_conversations(project_id=self.many.id, include_archived=True) if r.archived_at)
        bridge.previewDeleteConversation(gone); pump(60)
        self.assertTrue(bridge.confirmDeleteConversation(gone)); pump(300)
        bridge.setPage("home"); pump(400)
        self.assertEqual("1", self.link(root, "sidebarArchive"))
        # attach: a Global conversation moves to Boş
        self.tap(root, self.item(root, "sidebarGlobal"))
        before = genel()
        say(store, bridge.conversationId, "tezgah ölçüleri")
        bridge.previewAttach(self.empty.id, ""); pump(60)
        self.assertTrue(bridge.confirmAttach(self.empty.id, ""), bridge.attachNotice); pump(300)
        bridge.setPage("home"); pump(400)
        self.assertEqual((str(int(before) - 1), "1"), (genel(), count("Boş")))
        # a project archived: one more in the archived link, its row gone
        bridge.archiveProject(self.one.id); pump(400)
        bridge.setPage("home"); pump(400)
        self.assertEqual("2", self.link(root, "sidebarArchivedProjects"))
        self.assertNotIn("Tek", [i.property("title") for i in self.shown(root, "sidebarProject")])
        self.assertEqual(0, self.execute_calls())

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
        self.tap(root, self.item(root, "conversationRenameItem"))
        self.assertTrue(self.editing(root))
        self.assertEqual(target.title, self.item(root, "conversationRenameField").property("text"))
        self.item(root, "conversationRenameField").setProperty("text", "Satırdan yeni ad"); pump(60)
        self.tap(root, self.item(root, "conversationRenameSave"))
        self.assertEqual([(target.id, "Satırdan yeni ad")], calls)
        self.assertFalse(self.editing(root))
        self.assertEqual(opened, bridge.conversationId)
        self.assertIn("Satırdan yeni ad", self.titles(root))
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
        bridge.openConversation(store.list_conversations(project_id=self.old.id)[-1].id); pump(300)
        self.assertNotEqual(second.id, bridge.conversationId)
        self.tap(root, self.row(root, "sidebarConversation", "Eski ikinci"), Qt.RightButton)
        self.assertFalse(self.item(root, "conversationRenameItem").isEnabled())
        self.assertIn("archived", self.item(root, "conversationRenameReason").property("text"))
        self.assertFalse(self.item(root, "conversationArchiveItem").isEnabled())
        self.assertTrue(self.item(root, "conversationDeleteItem").isEnabled())
        self.tap(root, self.item(root, "conversationDeleteItem"))
        dialog = root.findChild(QObject, "conversationDeleteDialog")
        self.assertTrue(dialog.property("visible"))
        self.assertEqual(second.id, dialog.property("targetId"))
        self.assertNotEqual(second.id, bridge.conversationId)
        QMetaObject.invokeMethod(dialog, "close"); pump(400)
        self.assertIsNotNone(store.load_conversation(second.id))
        self.assertEqual(0, self.execute_calls())

    def test_z_the_top_bar_menu_still_works_through_conversation_menu(self) -> None:
        from pathlib import Path
        qml = Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "qml" / "components"
        self.assertNotIn("conversationSwitcherContext", (qml / "TopBar.qml").read_text(encoding="utf-8"))
        self.assertIn("conversationSwitcherContext", (qml / "ConversationMenu.qml").read_text(encoding="utf-8"))
        store = self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual(1, len(self.all_items(root, "conversationMenu")))                # one menu, two openers
        self.tap(root, self.item(root, "conversationSwitcher"), Qt.RightButton)
        menu = self.item(root, "conversationMenu")
        self.assertEqual(("", True), (menu.property("rowId"), menu.property("open")))
        self.assertTrue(self.item(root, "conversationSwitcher").property("open"))
        self.assertEqual(self.item(root, "conversationSwitcherText").property("text"),
                         self.item(root, "conversationContextHeader").property("text"))
        # Under the switcher, as before.
        switcher, header = self.box(root, "conversationSwitcher"), self.box(root, "conversationContextHeader")
        self.assertGreater(header.top(), switcher.bottom())
        self.tap(root, self.item(root, "conversationRenameItem"))
        self.item(root, "conversationRenameField").setProperty("text", "Üstten ad"); pump(60)
        self.tap(root, self.item(root, "conversationRenameSave"))
        self.assertEqual("Üstten ad", store.load_conversation(bridge.conversationId).title)
        self.assertEqual("Üstten ad", self.item(root, "conversationSwitcherText").property("text"))
        self.assertIn("Üstten ad", self.titles(root))
        self.assertEqual(0, self.execute_calls())

    def test_aa_the_links_and_the_new_project_plus(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual("All projects", self.item(root, "sidebarProjectsLink").property("tip"))
        for name in ("sidebarProjectsLink", "sidebarArchivedProjects"):
            self.tap(root, self.item(root, name))
            self.assertEqual(("projects", {}), (bridge.page, bridge.openProject), name)
            bridge.setPage("home"); pump(400)
        # Q9: the Projects page with its create field focused.
        self.tap(root, self.item(root, "sidebarNewProject"))
        self.assertEqual("projects", bridge.page)
        self.assertTrue(self.item(root, "projectNewName").property("activeFocus"))
        bridge.setPage("home"); pump(400)
        # Arşiv (n): Genel -> History; a project -> its detail.
        self.tap(root, self.item(root, "sidebarArchive"))
        self.assertEqual("history", bridge.page)
        bridge.setPage("home"); pump(400)
        self.tap(root, self.row(root, "sidebarProject", "Çok"))
        self.tap(root, self.item(root, "sidebarArchive"))
        self.assertEqual(("projects", self.many.id), (bridge.page, bridge.openProject.get("id")))
        bridge.setPage("home"); pump(400)
        # No archived conversation: no link.
        self.tap(root, self.row(root, "sidebarProject", "Tek"))
        self.assertFalse(self.item(root, "sidebarArchive").isVisible())
        # Below 760 px of height the archived-projects link folds into the heading as "(n)".
        root.resize(1100, 700); pump(600)
        self.assertFalse(self.item(root, "sidebarArchivedProjects").isVisible())
        folded = self.item(root, "sidebarArchivedProjectsCount")
        self.assertEqual(("(1)", "Archived projects"), (folded.property("count"), folded.property("tip")))
        self.inside(self.box(root, "sidebarNav"), self.rect(folded), "folded link")
        self.apart(self.rect(folded), self.box(root, "sidebarNewProject"), "folded / +")
        self.tap(root, folded)
        self.assertEqual("projects", bridge.page)
        self.assertEqual(0, self.execute_calls())

    def test_bb_every_navigation_action_has_a_mouse_path(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        keys = []
        real = QTest.keyClick
        with mock.patch.object(QTest, "keyClick", side_effect=lambda *a, **k: (keys.append(a), real(*a, **k))):
            self.tap(root, self.row(root, "sidebarProject", "Çok"))                                      # scope pick
            self.assertEqual(self.many.id, bridge.conversationProject["id"])
            other = next(t for t in self.titles(root) if not self.row(root, "sidebarConversation", t).property("current"))
            self.tap(root, self.row(root, "sidebarConversation", other))                                 # conversation pick
            self.assertEqual(other, bridge.conversationTitle)
            self.tap(root, self.item(root, "sidebarNewConversation"))                                    # new conversation
            self.assertEqual(("", self.many.id), (bridge.conversationTitle, bridge.conversationProject["id"]))
            self.tap(root, self.row(root, "sidebarConversation", other), Qt.RightButton)                 # rename
            self.tap(root, self.item(root, "conversationRenameItem"))
            self.item(root, "conversationRenameField").setProperty("text", other + " 2"); pump(60)
            self.tap(root, self.item(root, "conversationRenameSave"))
            self.tap(root, self.row(root, "sidebarConversation", other + " 2"), Qt.RightButton)          # archive
            self.tap(root, self.item(root, "conversationArchiveItem"))
            self.assertNotIn(other + " 2", self.titles(root))
            self.tap(root, self.item(root, "sidebarGlobal"))                                             # Genel
            self.assertTrue(bridge.conversationProject["global"])
            self.tap(root, self.item(root, "railTasks"))                                                 # tiles / peek
            self.assertTrue(root.property("peekOpen"))
            self.tap(root, self.item(root, "railTasks"))
            self.tap(root, self.item(root, "sidebarArchive"))                                            # Arşiv
            self.assertEqual("history", bridge.page)
            bridge.setPage("home"); pump(400)
            self.tap(root, self.item(root, "sidebarNewProject"))                                         # new project
            self.assertEqual("projects", bridge.page)
        self.assertEqual([], keys)
        self.assertEqual(0, self.execute_calls())

    def test_cc_the_keyboard_reaches_the_rows_with_a_ring(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        genel = self.item(root, "sidebarGlobal")
        genel.forceActiveFocus(); pump(200)
        self.assertTrue(self.child(genel, "sidebarRowFocusRing").isVisible())
        QTest.keyClick(root, Qt.Key_Tab); pump(200)
        self.assertTrue(self.item(root, "sidebarProjectsLink").property("activeFocus"))
        row = self.row(root, "sidebarProject", "Çok")
        row.forceActiveFocus(); pump(200)
        QTest.keyClick(root, Qt.Key_Return); pump(400)
        self.assertEqual(self.many.id, bridge.conversationProject["id"])
        target = next(r for r in self.shown(root, "sidebarConversation") if not r.property("current"))
        target.forceActiveFocus(); pump(200)
        QTest.keyClick(root, Qt.Key_Menu); pump(400)
        menu = self.item(root, "conversationMenu")
        self.assertEqual((True, target.property("itemId")), (menu.property("open"), menu.property("targetId")))
        QTest.keyClick(root, Qt.Key_Escape); pump(400)
        self.assertFalse(menu.property("open"))

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
