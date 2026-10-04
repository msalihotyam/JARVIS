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

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import sqlite3
import unittest
from unittest import mock

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
