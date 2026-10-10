"""Gate 6G Sidebar v2, slice 2 (build/6g/PLAN_DRAFT.txt "SIDEBAR v2" and its revisions).

L4   Geri / İleri (navigation history, disabled when not possible) and the two hide
     toggles in one row INSIDE the sidebar above the list; hidden, the same four
     buttons float top-left of the content row and hovering them shows the sidebar.
V5   A "grow" (the chat widens) / B "keep" (the space stays empty), remembered.
L1   Left column, conversation and right column share ONE top line under the brand
     and the controls; the title sits at the panel top, the first message below the core.
K1   The clock row stays in the sidebar; the tiles toggle is a small button below the
     left column in the footer line, the slogan right of it.
V6   Tiles mode: half of the DEFAULT left width, the lists hide, the tiles stack.
E1'  Edit mode: drag the inner edges to NARROW the columns (default = maximum: left
     340 / right 386 at 1920; minimum 240 / 300); remembered.
E2   One edit-mode entry (the Settings header, left of its X); a pill ends it.

Window preferences only (<prefs>.sidebar-layout.json, fail-closed). No test performs a
live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtTest import QTest

from jarvis.ui.preferences import UiPreferences, canonical_sidebar_layout
from tests.helpers import settled, wait_until
from tests.test_g6sb1_sidebar import SidebarV2Case
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump


class LayoutCase(SidebarV2Case):
    def layout_file(self) -> Path:
        return self.prefs_path.with_suffix(".sidebar-layout.json")

    def stored(self) -> dict:
        return json.loads(self.layout_file().read_text(encoding="utf-8"))

    def at(self, root, width: int, height: int) -> None:
        root.showNormal()
        root.resize(width, height)
        wait_until(lambda: (root.width(), root.height()) == (width, height), 3000)
        pump(150)

    def centre(self, item) -> QPoint:
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()

    def hit(self, root, item, until=None) -> None:
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, self.centre(item))
        if until is not None:
            self.assertTrue(wait_until(until, 2000))
        else:
            pump(200)

    def drag(self, root, item, dx: int) -> None:
        start = self.centre(item)
        QTest.mousePress(root, Qt.LeftButton, Qt.NoModifier, start)
        for n in range(1, 11):
            QTest.mouseMove(root, QPoint(start.x() + round(dx * n / 10), start.y()))
            pump(15)
        QTest.mouseRelease(root, Qt.LeftButton, Qt.NoModifier, QPoint(start.x() + dx, start.y()))
        pump(150)

    def evaluate(self, item, expression: str):
        value, undefined = QQmlExpression(QQmlEngine.contextForObject(item), item, expression).evaluate()
        self.assertFalse(undefined, expression)
        return value

    def visible(self, root, name) -> bool:
        return any(i.isVisible() for i in self.all_items(root, name))


class HistoryTests(LayoutCase):
    def test_a_back_and_forward_walk_the_views_and_are_disabled_when_not_possible(self) -> None:
        self.data()
        bridge, root = self.open_window()
        back, forward = self.item(root, "navBack"), self.item(root, "navForward")
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        pump(100)
        self.assertTrue(bridge.openConversation(self.plain[1].id))
        settled(300, lambda: (
            self.assertTrue(back.property("enabled")),
            self.assertFalse(forward.property("enabled")),
        ))
        self.hit(root, back, until=lambda: bridge.conversationId == self.plain[0].id)
        settled(300, lambda: self.assertTrue(forward.property("enabled")))
        self.hit(root, forward, until=lambda: bridge.conversationId == self.plain[1].id)
        settled(300, lambda: self.assertFalse(forward.property("enabled")))
        # A page is a view too: back from the Projects page returns home, to the same conversation.
        bridge.showProject(self.tez.id)
        pump(150)
        self.assertEqual("projects", bridge.page)
        self.evaluate(self.item(root, "panelColumn").parentItem(), "history.go(-1), true")
        settled(300, lambda: (
            self.assertEqual("home", bridge.page),
            self.assertEqual(self.plain[1].id, bridge.conversationId),
        ))
        # Going back to the very first view: Geri is disabled there.
        for _ in range(10):
            if not back.property("enabled"):
                break
            self.hit(root, back)
        self.assertFalse(back.property("enabled"))
        self.assertTrue(forward.property("enabled"))
        self.assertEqual(0, self.execute_calls())


class HideModeTests(LayoutCase):
    def test_b_both_hide_modes_the_hover_overlay_and_the_remembered_state(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.at(root, 1920, 1080)
        docked = self.rect(self.item(root, "canonicalWorkspace"))
        # A "grow": the sidebar hides, the four buttons float at its corner, the chat widens left under them.
        self.hit(root, self.item(root, "sidebarHide_grow"), until=lambda: not self.item(root, "sidebar").isVisible())
        settled(400, lambda: (
            self.assertTrue(self.item(root, "sidebarFloatBar").isVisible()),
            self.assertEqual("grow", bridge.sidebarLayout["hide_mode"]),
            self.assertEqual("grow", self.stored()["hide_mode"]),
        ))
        bar, work = self.rect(self.item(root, "sidebarFloatBar")), self.rect(self.item(root, "canonicalWorkspace"))
        self.assertAlmostEqual(10, bar.left(), delta=0.5)
        self.assertAlmostEqual(104, bar.top(), delta=0.5)
        self.assertAlmostEqual(10, work.left(), delta=1)
        self.assertGreaterEqual(work.top(), bar.bottom())
        self.assertGreater(work.width(), docked.width() + 300)
        # The click itself does not open the overlay; leaving and hovering the row does.
        QTest.mouseMove(root, QPoint(960, 700))
        pump(450)
        self.assertFalse(self.item(root, "sidebar").isVisible())
        QTest.mouseMove(root, self.centre(self.item(root, "sidebarFloatBar")))
        self.assertTrue(wait_until(lambda: self.item(root, "sidebar").isVisible(), 1000))
        side = self.rect(self.item(root, "sidebar"))
        self.assertEqual((10.0, 104.0), (side.left(), side.top()))
        # Still there while the pointer is on it; gone after the grace once it left.
        QTest.mouseMove(root, QPoint(round(side.center().x()), round(side.center().y())))
        pump(500)
        self.assertTrue(self.item(root, "sidebar").isVisible())
        QTest.mouseMove(root, QPoint(960, 700))
        self.assertTrue(wait_until(lambda: not self.item(root, "sidebar").isVisible(), 1500))
        # B "keep": the space stays empty (the work surface keeps its docked place).
        self.hit(root, [i for i in self.all_items(root, "sidebarHide_keep") if i.isVisible()][0],
                   until=lambda: bridge.sidebarLayout["hide_mode"] == "keep")
        QTest.mouseMove(root, QPoint(960, 700))
        settled(400, lambda: (
            self.assertAlmostEqual(docked.left(), self.rect(self.item(root, "canonicalWorkspace")).left(), delta=1),
            self.assertAlmostEqual(104, self.rect(self.item(root, "canonicalWorkspace")).top(), delta=1),
        ))
        # Remembered: a new window over the same preferences opens hidden in "keep".
        self.assertEqual("keep", UiPreferences(self.prefs_path).sidebar_layout["hide_mode"])
        # Clicking the active toggle docks the sidebar again.
        QTest.mouseMove(root, QPoint(960, 700))
        pump(450)
        keep = [i for i in self.all_items(root, "sidebarHide_keep") if i.isVisible()][0]
        self.hit(root, keep, until=lambda: bridge.sidebarLayout["hide_mode"] == "")
        settled(400, lambda: (
            self.assertTrue(self.item(root, "sidebar").isVisible()),
            self.assertFalse(self.item(root, "sidebarFloatBar").isVisible()),
        ))
        self.assertEqual(0, self.execute_calls())


class TopLineTests(LayoutCase):
    def test_c_one_shared_top_line_at_1920_and_1280_without_overlap(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        for width, height in ((1920, 1080), (1280, 720)):
            self.at(root, width, height)
            with self.subTest(size=(width, height)):
                side, work = self.rect(self.item(root, "sidebar")), self.rect(self.item(root, "canonicalWorkspace"))
                column = self.rect(self.item(root, "panelColumn"))
                self.assertEqual([104.0] * 3, [round(side.top(), 1), round(work.top(), 1), round(column.top(), 1)])
                # Under the brand and the controls; nothing overlaps.
                for name in ("settingsOrb", "brandWordmark", "brandTagline", "commandStrip", "killSwitchLabel"):
                    self.assertLessEqual(self.rect(self.item(root, name)).bottom(), 104, name)
                self.assertLessEqual(side.right(), work.left())
                self.assertLessEqual(work.right(), column.left())
                self.assertLessEqual(column.right(), width)
                # At 1920 the title sits at the panel top, left of the core's ring, with room for the
                # progress band under it; smaller, it degrades to under the core. The first message
                # always starts below the core art and the header.
                header, core = self.rect(self.item(root, "chatHeader")), self.rect(self.item(root, "jarvisCore"))
                feed = self.rect(self.item(root, "conversationFeed"))
                if width == 1920:
                    self.assertAlmostEqual(work.top() + 8, header.top(), delta=0.5)
                    self.assertLessEqual(header.right(), core.center().x() - core.width() * 0.37)
                    self.assertGreaterEqual(feed.top(), header.bottom() + 50)
                else:
                    self.assertGreaterEqual(header.top(), core.bottom())
                self.assertGreaterEqual(feed.top(), core.bottom())
                self.assertGreaterEqual(feed.top(), header.bottom())
        self.assertEqual(0, self.execute_calls())


class FooterAndTilesTests(LayoutCase):
    def test_d_the_clock_row_stays_and_the_tiles_toggle_sits_below_the_left_column(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.at(root, 1920, 1080)
        side, clock = self.rect(self.item(root, "sidebar")), self.rect(self.item(root, "leftContextCard"))
        toggle = self.rect(self.item(root, "sidebarTilesToggle"))
        self.assertTrue(self.item(root, "leftContextCard").isVisible())
        self.assertGreater(clock.top(), side.bottom() - 60)
        self.assertLessEqual(clock.bottom(), side.bottom())
        self.assertAlmostEqual(side.left(), toggle.left(), delta=0.5)
        self.assertGreaterEqual(toggle.top(), side.bottom())
        self.assertLessEqual(toggle.bottom(), 1080)
        slogan = [i for i in self.descendants(root.contentItem()) if i.property("text") == "INTELLIGENCE AMPLIFIES POTENTIAL"]
        self.assertEqual(1, len(slogan))
        self.assertGreater(self.rect(slogan[0]).left(), toggle.right())

    def test_e_tiles_mode_is_half_the_default_width_with_the_tiles_stacked(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.at(root, 1920, 1080)
        bridge.setSidebarWidths(260, 386)
        settled(300, lambda: self.assertEqual(260, round(self.rect(self.item(root, "sidebar")).width())))
        self.hit(root, self.item(root, "sidebarTilesToggle"), until=lambda: bridge.sidebarLayout["tiles"])
        settled(400, lambda: (
            self.assertEqual(170, round(self.rect(self.item(root, "sidebar")).width())),     # 340 / 2, not 260 / 2
            self.assertFalse(self.item(root, "sidebarNav").isVisible()),
            self.assertTrue(self.item(root, "leftContextCard").isVisible()),
        ))
        tiles = [self.rect(self.item(root, "rail" + key)) for key in ("Tasks", "Focus", "Plan", "Memory")]
        rail = self.rect(self.item(root, "summaryRail"))
        self.assertEqual({rail.left()}, {t.left() for t in tiles})
        self.assertEqual({rail.width()}, {t.width() for t in tiles})
        self.assertEqual(sorted(t.top() for t in tiles), [t.top() for t in tiles])
        self.assertGreater(rail.height(), 4 * 86)                                             # they fill the height
        self.assertTrue(self.stored()["tiles"])
        self.hit(root, self.item(root, "sidebarTilesToggle"), until=lambda: not bridge.sidebarLayout["tiles"])
        settled(400, lambda: (
            self.assertEqual(260, round(self.rect(self.item(root, "sidebar")).width())),
            self.assertTrue(self.item(root, "sidebarNav").isVisible()),
        ))


class EditModeTests(LayoutCase):
    def test_f_the_inner_edges_only_narrow_clamped_and_remembered(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.at(root, 1920, 1080)
        self.assertEqual(340, round(self.rect(self.item(root, "sidebar")).width()))
        self.assertEqual(386, round(self.rect(self.item(root, "panelColumn")).width()))
        self.assertFalse(self.visible(root, "leftWidthGrip"))
        bridge.setEditMode(True)
        pump(200)
        self.drag(root, self.item(root, "leftWidthGrip"), 60)                   # wider than the default: stays 340
        self.assertEqual(340, round(self.rect(self.item(root, "sidebar")).width()))
        self.drag(root, self.item(root, "leftWidthGrip"), -60)
        self.assertEqual(280, round(self.rect(self.item(root, "sidebar")).width()))
        self.drag(root, self.item(root, "leftWidthGrip"), -300)                 # clamped at the minimum
        self.assertEqual(240, round(self.rect(self.item(root, "sidebar")).width()))
        self.drag(root, self.item(root, "rightWidthGrip"), 50)
        settled(500, lambda: self.assertEqual(336, round(self.rect(self.item(root, "panelColumn")).width())))
        self.drag(root, self.item(root, "rightWidthGrip"), 300)
        settled(500, lambda: self.assertEqual(300, round(self.rect(self.item(root, "panelColumn")).width())))
        self.drag(root, self.item(root, "rightWidthGrip"), -400)                # back to the default, never past it
        settled(500, lambda: self.assertEqual(386, round(self.rect(self.item(root, "panelColumn")).width())))
        self.assertEqual((240, 386), (self.stored()["left"], self.stored()["right"]))
        self.assertEqual(0.236, bridge.columnWidth)                              # the main document is untouched
        # Scaled at another size by the existing rules (default 256 there: 240 / 340 of it).
        self.at(root, 1280, 720)
        self.assertEqual(round(256 * 240 / 340), round(self.rect(self.item(root, "sidebar")).width()))
        self.assertEqual(0, self.execute_calls())

    def test_g_one_edit_mode_entry_in_settings_and_a_pill_ends_it(self) -> None:
        self.data()
        bridge, root = self.open_window()
        self.at(root, 1920, 1080)
        triggers = self.all_items(root, "editLayoutTrigger")
        self.assertEqual(1, len(triggers))
        self.assertFalse(triggers[0].isVisible())                               # not in the top bar any more
        texts = [i.property("text") for i in self.descendants(root.contentItem())]
        self.assertNotIn("Edit panels", texts)
        self.hit(root, self.item(root, "settingsOrb"), until=lambda: triggers[0].isVisible())
        pump(300)
        trigger, close = self.rect(triggers[0]), self.rect(self.item(root, "settingsSurfaceClose"))
        self.assertLess(trigger.right(), close.left())
        self.assertLessEqual(close.left() - trigger.right(), 12)
        self.assertAlmostEqual(close.center().y(), trigger.center().y(), delta=1)
        self.assertFalse(self.visible(root, "editModePill"))
        self.hit(root, triggers[0], until=lambda: bridge.editMode)
        settled(400, lambda: (
            self.assertFalse(root.property("settingsOpen")),
            self.assertTrue(self.item(root, "editModePill").isVisible()),
        ))
        pill, word = self.rect(self.item(root, "editModePill")), self.rect(self.item(root, "brandWordmark"))
        self.assertGreater(pill.left(), word.right())
        self.assertLess(pill.top(), 104)
        self.hit(root, self.item(root, "editModeDone"), until=lambda: not bridge.editMode)
        settled(300, lambda: self.assertFalse(self.item(root, "editModePill").isVisible()))


class PinV9Tests(LayoutCase):
    def test_h_a_drop_among_the_shown_pins_keeps_the_hidden_ones_in_place(self) -> None:
        self.data()
        bridge, root = self.open_window()
        for conversation in (self.plain[0], self.tezc[0], self.plain[1]):
            self.assertTrue(bridge.pinConversation(conversation.id))
        # Pins, newest on top: Düz 1, Tez 0, Düz 0. Archiving Tez hides its pin (V9 strict), it stays stored.
        self.db().set_project_active(self.tez.id, False)
        bridge.refreshProjects()
        settled(300, lambda: self.assertEqual(["Düz 1", "Düz 0"],
                                              [i.property("title") for i in self.shown(root, "sidebarPinnedConversation")]))
        self.assertEqual(3, len(bridge.pinnedConversations))
        side = self.item(root, "sidebar")
        # "Düz 0" to the top of the shown pins: index 0 of the whole list.
        self.assertEqual(0, self.evaluate(side, f"sidebar.pinIndex('{self.plain[0].id}', 0)"))
        # "Düz 1" below "Düz 0": right after it in the whole list (the hidden pin keeps its place).
        self.assertEqual(2, self.evaluate(side, f"sidebar.pinIndex('{self.plain[1].id}', 1)"))


class LayoutPrefsTests(unittest.TestCase):
    def test_i_the_layout_file_is_fail_closed_and_never_touches_the_main_document(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ui-preferences.json"
            prefs = UiPreferences(path)
            prefs.save()
            main = path.read_text(encoding="utf-8")
            side = path.with_suffix(".sidebar-layout.json")
            self.assertEqual(canonical_sidebar_layout(), prefs.sidebar_layout)
            self.assertTrue(prefs.set_sidebar_layout({"hide_mode": "grow", "left": 260}))
            self.assertEqual(main, path.read_text(encoding="utf-8"))
            self.assertEqual({"version": 1, "hide_mode": "grow", "tiles": False, "left": 260, "right": 386},
                             json.loads(side.read_text(encoding="utf-8")))
            # A refused change writes nothing.
            for bad in ({"hide_mode": "gone"}, {"left": 239}, {"left": 341}, {"right": 299}, {"right": 387},
                        {"tiles": 1}, {"left": 260.5}, {"left": True}):
                self.assertFalse(prefs.set_sidebar_layout(bad), bad)
            self.assertEqual("grow", UiPreferences(path).sidebar_layout["hide_mode"])
            # Missing, unreadable, or any part out of range: the defaults, and the file is not rewritten.
            for text in ("{", "[]", json.dumps({**canonical_sidebar_layout(), "right": 500}),
                         json.dumps({**canonical_sidebar_layout(), "extra": 1}),
                         json.dumps({**canonical_sidebar_layout(), "version": 2})):
                side.write_text(text, encoding="utf-8")
                self.assertEqual(canonical_sidebar_layout(), UiPreferences(path).sidebar_layout, text)
                self.assertEqual(text, side.read_text(encoding="utf-8"))
            side.unlink()
            self.assertEqual(canonical_sidebar_layout(), UiPreferences(path).sidebar_layout)
            self.assertEqual(main, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
