"""Gate 6G K6: pages show only real content.

* Words: every task status, memory classification, lifecycle and origin code
  reads as a word on the pages (the exact code never shows bare).
* CommandPreset: no filler for features that do not exist; the load rings are
  drawn to their measured share, and say so when nothing can be read.
* Tasks / Memory / Automations / History: honest empty states and words.
* SummaryPeek: never covers the composer; the Focus count is the first active
  project's records in use; a row either goes somewhere or does not look like it.
* Greeting: follows the time of day by a clock a test fixes.
* Command palette: the choice stays visible, a query without matches says so,
  a click on the panel is not an outside click, closing returns the keyboard.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests.test_ui import _APP, PresentationTests, UiTestCase, pump, reflow_still, settled  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import QDateTime, QPointF, Qt  # noqa: E402
from PySide6.QtQml import QQmlProperty  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jarvis.contracts import DataClassification, MemoryLifecycle, MemoryOrigin, TaskStatus  # noqa: E402
from jarvis.memory import SQLiteMemoryStore  # noqa: E402
from jarvis.ui.i18n import localization  # noqa: E402
from jarvis.ui.wording import CODE_WORDS, code_word  # noqa: E402

TEXT_LIKE = (str,)


class WordTests(unittest.TestCase):
    def test_every_page_code_has_a_word_in_both_languages(self) -> None:
        for kind, enum in (("task_status", TaskStatus), ("memory_classification", DataClassification),
                           ("memory_lifecycle", MemoryLifecycle), ("memory_origin", MemoryOrigin)):
            self.assertEqual({m.value for m in enum}, set(CODE_WORDS[kind]), kind)
        for language in ("en-US", "tr-TR"):
            localization(_APP).apply(language)
            self.addCleanup(lambda: localization(_APP).apply("en-US"))
            for kind, enum in (("task_status", TaskStatus), ("memory_classification", DataClassification),
                               ("memory_lifecycle", MemoryLifecycle), ("memory_origin", MemoryOrigin)):
                for member in enum:
                    with self.subTest(language=language, kind=kind, code=member.value):
                        word = code_word(kind, member.value)
                        self.assertTrue(word)
                        self.assertNotIn(member.value, word)
        localization(_APP).apply("tr-TR")
        self.assertEqual("Onayınız bekleniyor", code_word("task_status", "awaiting_approval"))
        self.assertEqual("Sizin yazdığınız", code_word("memory_origin", "explicit_user"))


class K6Case(UiTestCase):
    item = PresentationTests.item
    click = PresentationTests.click
    window = PresentationTests.window

    def all_items(self, root):
        found, stack = [], [root.contentItem()]
        while stack:
            node = stack.pop()
            found.append(node)
            stack.extend(node.childItems())
        return found

    def named(self, root, name, visible=True):
        return [i for i in self.all_items(root) if i.objectName() == name and (not visible or i.isVisible())]

    def shown_texts(self, root):
        return [i.property("text") for i in self.all_items(root) if i.isVisible() and isinstance(i.property("text"), TEXT_LIKE)]

    def box(self, item):
        p = item.mapToScene(QPointF(0, 0))
        return p.x(), p.y(), p.x() + item.width(), p.y() + item.height()

    def page(self, bridge, root, name):
        bridge.setPage(name)
        settled(300, lambda: self.assertEqual(name, root.property("loadedPage")))


class GreetingTests(K6Case):
    def at(self, page, hour, minute=0):
        stamp = QDateTime(datetime(2026, 10, 9, hour, minute))
        self.assertTrue(QQmlProperty(page, "now").write(stamp))
        pump(20)

    def check(self, page, text_item) -> None:
        for hour, minute, word in ((9, 0, "Good morning"), (11, 59, "Good morning"), (12, 0, "Good afternoon"),
                                   (17, 59, "Good afternoon"), (18, 0, "Good evening"), (2, 0, "Good evening"),
                                   (4, 59, "Good evening"), (5, 0, "Good morning")):
            with self.subTest(at=(hour, minute)):
                self.at(page, hour, minute)
                self.assertTrue(text_item.property("text").startswith(word), (hour, minute, text_item.property("text")))
        # The clock the greeting reads is a running minute timer, not a value read once at load.
        timers = [c for c in page.children() if c.metaObject().className().startswith("QQmlTimer")]
        self.assertTrue([t for t in timers if t.property("interval") == 60000 and t.property("running")], timers)

    def test_the_home_greeting_follows_the_time_of_day(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.check(self.item(root, "canonicalWorkspace"), self.item(root, "welcomeGreeting"))

    def test_the_command_preset_greeting_follows_the_time_of_day(self) -> None:
        bridge = self.bridge()
        bridge.setTheme("command")
        root = self.window(bridge)
        self.check(self.item(root, "commandPreset"), self.item(root, "presetGreeting"))


class CommandPresetTests(K6Case):
    def test_no_filler_and_rings_drawn_to_their_reading(self) -> None:
        bridge = self.bridge()
        bridge.setTheme("command")
        root = self.window(bridge)
        texts = " | ".join(self.shown_texts(root) + [str(i.property("label")) for i in self.all_items(root) if i.isVisible()])
        for filler in ("Agent capacity", "Unavailable in this build", "N/A", "Coding agents", "Voice", "Calendar"):
            self.assertNotIn(filler, texts)
        quick = self.item(root, "presetQuickCommands")
        buttons = [i for i in self.all_items(root) if i.property("label") in ("Tasks", "History", "Automations", "Memory")
                   and i.property("compact") is True and quick.isAncestorOf(i)]
        self.assertEqual(4, len(buttons))
        self.assertTrue(all(b.property("enabled") for b in buttons))
        bridge._telemetry_timer.stop()          # the machine's own readings would replace the fixed ones
        bridge._telemetry = {"available": True, "cpu": 43, "ram": None, "disk": 89}
        bridge.telemetryChanged.emit()
        settled(200, lambda: (
            self.assertTrue(self.item(root, "presetGauges").isVisible()),
            self.assertAlmostEqual(0.43, self.item(root, "presetGauge_cpu").property("share"), places=3),
            self.assertTrue(self.item(root, "presetGauge_cpu").isVisible()),
            # An unread figure has no ring and no invented 0 %.
            self.assertFalse(self.item(root, "presetGauge_ram").isVisible()),
            self.assertFalse(self.item(root, "presetGaugeTrack_ram").isVisible()),
            self.assertTrue(self.item(root, "presetGaugeTrack_cpu").isVisible()),
            self.assertIn("—", self.shown_texts(root)),
            self.assertNotIn("0%", self.shown_texts(root)),
            self.assertFalse(self.item(root, "presetGaugesEmpty").isVisible()),
        ))
        bridge._telemetry = {"available": False}
        bridge.telemetryChanged.emit()
        settled(200, lambda: (
            self.assertFalse(self.item(root, "presetGauges").isVisible()),
            self.assertTrue(self.item(root, "presetGaugesEmpty").isVisible()),
        ))


class PageTests(K6Case):
    def test_tasks_page_empty_states_and_words(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.page(bridge, root, "tasks")
        self.assertEqual(1, len(self.named(root, "tasksSessionsEmpty")))
        self.assertEqual(1, len(self.named(root, "tasksTasksEmpty")))
        self.assertEqual(1, len(self.named(root, "tasksSnapshotEmpty")))
        self.click(root, "tasksSessionsEmptyAction", until=lambda: bridge.page == "home")
        self.assertEqual("home", bridge.page)
        self.page(bridge, root, "tasks")
        bridge._sessions = [{"session_id": "s-1", "title": "Rapor", "task_count": 1, "updated_at": "", "readable": False}]
        bridge._tasks = [{"task_id": "t-1", "title": "", "capability": "web_research", "status": "awaiting_approval",
                          "stage": "risk_and_permission", "selected_connector": ""}]
        bridge.dataChanged.emit()
        tasks_page = [i for i in self.all_items(root) if i.property("selectedSession") is not None][0]
        tasks_page.setProperty("selectedSession", "s-1")
        settled(200, lambda: (
            self.assertEqual([], self.named(root, "tasksSessionsEmpty")),
            self.assertEqual([], self.named(root, "tasksTasksEmpty")),
            # Words first; the exact capability name only beside them, as data.
            self.assertEqual(["Untitled task"], [i.property("text") for i in self.named(root, "tasksTaskTitle")]),
            self.assertEqual(["Awaiting your approval  ·  Checking permission"], [i.property("text") for i in self.named(root, "tasksTaskStatus")]),
            self.assertEqual(["web_research"], [i.property("text") for i in self.named(root, "tasksTaskSource")]),
            self.assertTrue(self.named(root, "tasksSessionMeta")[0].property("text").endswith("cannot be read")),
        ))
        self.assertNotIn("UNREADABLE", " ".join(self.shown_texts(root)))
        # While a session's tasks are on their way there is no "no tasks" verdict; it comes with the answer.
        bridge._tasks = []
        bridge.dataChanged.emit()
        settled(200, lambda: self.assertEqual(1, len(self.named(root, "tasksTasksEmpty"))))
        tasks_page.setProperty("loadingSession", "s-1")
        settled(200, lambda: self.assertEqual([], self.named(root, "tasksTasksEmpty")))
        bridge.dataChanged.emit()
        settled(200, lambda: self.assertEqual(1, len(self.named(root, "tasksTasksEmpty"))))
        self.assertIn("This session has no tasks.", self.shown_texts(root))
        self.assertEqual(0, self.execute_calls())

    def test_memory_page_words_and_empty_states(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.page(bridge, root, "memory")
        empty = self.item(root, "memoryEmpty")
        self.assertTrue(empty.isVisible())
        self.assertIn("No project yet, so there is no project memory.", self.shown_texts(root))
        self.assertNotIn("jarvis project create", " ".join(self.shown_texts(root)))
        self.click(root, "memoryEmptyAction", until=lambda: bridge.page == "projects")
        self.assertEqual("projects", bridge.page)
        store = SQLiteMemoryStore(self.database)
        project = store.create_project("Evim")
        bridge.setPage("memory")
        settled(300, lambda: (
            self.assertTrue(self.item(root, "memoryEmpty").isVisible()),
            self.assertIn("No memory records to show for this project.", self.shown_texts(root)),
            self.assertEqual([], self.named(root, "memoryEmptyAction")),
        ))
        store.save_explicit_user_memory_item(project.id, "fact", "internal", "tabs are 4 spaces")
        bridge.refreshMemory("", False)
        settled(300, lambda: (
            self.assertFalse(self.item(root, "memoryEmpty").isVisible()),
            self.assertEqual(["Internal"], [i.property("text") for i in self.named(root, "memoryClassification")]),
            self.assertEqual(["In use"], [i.property("text") for i in self.named(root, "memoryLifecycle")]),
            self.assertEqual(["Written by you"], [i.property("text") for i in self.named(root, "memoryOrigin")]),
        ))

    def test_automations_rows_read_as_words_and_dates(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.page(bridge, root, "automations")
        due = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=9, minute=5, second=0, microsecond=0)
        bridge._jobs = [{"id": "j-1", "label": "Rapor", "readable": True, "enabled": True, "archived": False, "revision": 1,
                         "kind": "recurring", "next_due_at_utc": due.astimezone(timezone.utc).isoformat()}]
        bridge.dataChanged.emit()
        settled(200, lambda: (
            self.assertEqual(["Recurring"], [i.property("text") for i in self.named(root, "automationsKind")]),
            self.assertEqual(["next: tomorrow 09:05"], [i.property("text") for i in self.named(root, "automationsNext")]),
        ))

    def test_history_page_empty_states_and_words(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.page(bridge, root, "history")
        bridge._activity = []
        bridge._sessions = [{"session_id": "s-1", "title": "Rapor", "task_count": 2, "updated_at": "", "readable": False}]
        bridge.dataChanged.emit()
        settled(200, lambda: (
            self.assertTrue(self.item(root, "historyActivityEmpty").isVisible()),
            self.assertTrue(self.named(root, "historySessionMeta")[0].property("text").endswith("cannot be read")),
        ))
        self.assertNotIn("UNREADABLE", " ".join(self.shown_texts(root)))
        bridge._note("Bir olay")
        settled(200, lambda: self.assertFalse(self.item(root, "historyActivityEmpty").isVisible()))


class PeekTests(K6Case):
    def sessions(self, bridge, count=7):
        bridge._sessions = [{"session_id": f"s-{n}", "title": f"Oturum {n}", "task_count": 1, "updated_at": "", "readable": True}
                            for n in range(count)]
        bridge._jobs = [{"id": f"j-{n}", "label": f"İş {n}", "readable": True, "enabled": True, "archived": False,
                         "revision": 1, "kind": "recurring", "next_due_at_utc": ""} for n in range(7)]
        bridge.dataChanged.emit()

    def open_peek(self, root, key):
        self.click(root, "rail" + key, until=lambda: root.property("peekOpen") and root.property("peekKey") == key)
        pump(100)
        return self.item(root, "summaryPeek")

    def close_peek(self, root):
        QTest.keyClick(root, Qt.Key_Escape)
        settled(300, lambda: self.assertFalse(root.property("peekOpen")))

    def test_the_peek_never_covers_the_composer(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.sessions(bridge)           # after start-up, which loads the real (empty) listings
        for width, height in ((1920, 1080), (1280, 760), (1100, 700)):
            root.resize(width, height)
            reflow_still(self, root, 600)
            for key in ("Tasks", "Focus", "Plan"):
                with self.subTest(size=(width, height), key=key):
                    peek = self.open_peek(root, key)
                    composer = self.box(self.item(root, "canonicalComposer"))
                    surface = self.box(peek)
                    self.assertLessEqual(surface[3], composer[1], (surface, composer))
                    self.assertGreaterEqual(surface[1], 0)
                    # Every row it lists is whole inside it.
                    for row in self.named(root, "summaryPeekRow"):
                        self.assertLessEqual(self.box(row)[3], surface[3] + 0.5)
                    self.close_peek(root)
        self.assertEqual(0, self.execute_calls())

    def test_the_focus_count_is_the_first_project_s_records_in_use(self) -> None:
        store = SQLiteMemoryStore(self.database)
        first, second = store.create_project("Birinci"), store.create_project("İkinci")
        items = [store.save_explicit_user_memory_item(first.id, "fact", "internal", f"kayıt {n}") for n in range(3)]
        store.retire_memory_item(first.id, items[0].id)
        for n in range(4):
            store.save_explicit_user_memory_item(second.id, "fact", "internal", f"öteki {n}")
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertEqual(first.id, bridge.projects[0])
        # The Memory page last showed the other project, retired records included.
        bridge.refreshMemory(second.id, True)
        self.assertEqual(4, len(bridge.memoryItems))
        self.open_peek(root, "Focus")
        values = [i.property("text") for i in self.named(root, "summaryPeekMetricValue")]
        self.assertEqual(["2", "2"], values)           # two active projects; two records in use in "Birinci"
        self.assertIn("Items in Birinci", self.shown_texts(root))
        self.assertIn("2 memory items", self.shown_texts(root))

    def test_rows_go_somewhere_or_do_not_look_like_it(self) -> None:
        store = SQLiteMemoryStore(self.database)
        project = store.create_project("Evim")
        bridge = self.bridge()
        root = self.window(bridge)
        self.sessions(bridge, 2)
        for key, page in (("Plan", "automations"), ("Tasks", "tasks"), ("Focus", "projects")):
            with self.subTest(key=key):
                bridge.setPage("home")
                settled(300, lambda: self.assertEqual("home", root.property("loadedPage")))
                self.open_peek(root, key)
                rows = self.named(root, "summaryPeekRow")
                self.assertTrue(rows)
                self.assertTrue(all(r.property("actionable") for r in rows))
                row = rows[0]
                hover = [c for c in row.childItems() if c.objectName() == "summaryPeekRowHover"][0]
                self.assertFalse(hover.isVisible())
                QTest.mouseMove(root, row.mapToScene(QPointF(row.width() / 2, row.height() / 2)).toPoint())
                settled(200, lambda: self.assertTrue(hover.isVisible()))
                QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, row.mapToScene(QPointF(row.width() / 2, row.height() / 2)).toPoint())
                settled(300, lambda: (
                    self.assertEqual(page, bridge.page),
                    self.assertFalse(root.property("peekOpen")),
                ))
        self.assertEqual(project.id, bridge.openProject.get("id"))
        # The live task has no place to go: it is not drawn as a button.
        bridge.setPage("home")
        bridge._session_id, bridge._task = "s-live", {"task_id": "t-1", "status": "running", "title": "Canlı"}
        bridge.stateChanged.emit()
        self.open_peek(root, "Tasks")
        live = [r for r in self.named(root, "summaryPeekRow") if not r.property("actionable")]
        self.assertEqual(1, len(live))
        self.assertEqual(0, self.execute_calls())


class PaletteTests(K6Case):
    def open_palette(self, root):
        QTest.keyClick(root, Qt.Key_K, Qt.ControlModifier)
        settled(100, lambda: self.assertTrue(root.property("paletteOpen")))

    def focus_name(self, root) -> str:
        item = root.activeFocusItem()
        return item.objectName() if item is not None else ""

    def test_escape_returns_the_keyboard_and_new_task_focuses_the_composer(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.item(root, "composerInput").forceActiveFocus()
        pump(20)
        self.assertEqual("composerInput", self.focus_name(root))
        self.open_palette(root)
        self.assertEqual("commandPaletteSearch", self.focus_name(root))
        QTest.keyClick(root, Qt.Key_Escape)
        settled(100, lambda: (
            self.assertFalse(root.property("paletteOpen")),
            self.assertEqual("composerInput", self.focus_name(root)),
        ))
        # "New task" from another page: home, and the keyboard in the composer.
        bridge.setPage("tasks")
        settled(300, lambda: self.assertEqual("tasks", root.property("loadedPage")))
        self.open_palette(root)
        self.item(root, "commandPaletteSearch").setProperty("text", "New task")
        QTest.keyClick(root, Qt.Key_Return)
        settled(300, lambda: (
            self.assertEqual("home", bridge.page),
            self.assertEqual("composerInput", self.focus_name(root)),
        ))

    def test_keyboard_choice_stays_visible_and_no_match_says_so(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.open_palette(root)
        palette_list = self.item(root, "commandPaletteList")
        self.assertEqual(0, palette_list.property("contentY"))
        for _ in range(15):
            QTest.keyClick(root, Qt.Key_Down)
        pump(50)
        # The 16th command is the choice and lies inside the visible list.
        top, height = palette_list.property("contentY"), palette_list.height()
        self.assertGreater(top, 0)
        self.assertLessEqual(15 * 36 + 36, top + height + 0.5)
        self.assertGreaterEqual(15 * 36, top - 0.5)
        QTest.keyClick(root, Qt.Key_Tab)
        QTest.keyClick(root, Qt.Key_Backtab)
        QTest.keyClick(root, Qt.Key_Backtab)
        pump(20)
        self.assertEqual("commandPaletteSearch", self.focus_name(root))
        palette = [i for i in self.all_items(root) if i.property("cursor") is not None and i.property("matches") is not None][0]
        self.assertEqual(14, palette.property("cursor"))
        # No match: said so; Enter and Down do nothing, the palette stays open.
        self.item(root, "commandPaletteSearch").setProperty("text", "zzzqqq")
        settled(100, lambda: self.assertTrue(self.item(root, "commandPaletteEmpty").isVisible()))
        self.assertIn('No command matches "zzzqqq".', self.shown_texts(root))
        QTest.keyClick(root, Qt.Key_Down)
        QTest.keyClick(root, Qt.Key_Return)
        pump(50)
        self.assertEqual(0, palette.property("cursor"))
        self.assertTrue(root.property("paletteOpen"))
        self.assertEqual("home", bridge.page)
        # A click on the panel (its padding) is not an outside click.
        surface = self.item(root, "commandPaletteSurface")
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, surface.mapToScene(QPointF(6, surface.height() - 6)).toPoint())
        pump(50)
        self.assertTrue(root.property("paletteOpen"))
        # Outside it is.
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, surface.mapToScene(QPointF(surface.width() / 2, surface.height() + 60)).toPoint())
        settled(100, lambda: self.assertFalse(root.property("paletteOpen")))
        self.assertEqual(0, self.execute_calls())


if __name__ == "__main__":
    unittest.main()
