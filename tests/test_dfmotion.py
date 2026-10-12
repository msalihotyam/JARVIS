"""DFMOTION dogfood (Salih 2026-10-12 01:49): one subtle motion language inside K2.

"sol sutundaki gizleme/geri acma butonlarina da efekt koyalim ... bir sohbet ismine
tiklandiginda o sohbete gecerken de cok kucuk efekt olsun ... yeni sohbet butonuna
basinca da ... Gezin menusu ..."

A  The inventory: every surface that opens, closes, expands, collapses or swaps
   carries its transition, and each goes through Theme (tests/test_motion_6g.py
   keeps the gate: Theme.dur, 120-220 ms, no bounce, travel <= 4 px, no new
   exception). The no-effect list is untouched.
B  Live, ambient on: a menu (the Gezin menu) opens with fade + travel + a scale
   from its anchor corner; a sidebar section glides from its old height and an
   opening one's rows fade in; a conversation switch (a row, New chat) brings the
   feed and the header title in - and typing during it lands in the composer at
   once, for the new conversation.
C  Ambient off: none of it moves - every value is at rest at once.

With JARVIS_SHOTS_DIR set, frames for Salih go to <dir>/<surface>/frame_###.png
(+ <surface>.gif when Pillow imports); otherwise to a temp folder, so the frame
test never skips. Temp stores only: no live DB, model, auth or network call.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtTest import QTest

from jarvis.ui.typography import FONT_DIR
from tests import test_ui as ui
from tests.helpers import wait_until
from tests.test_g6sb1_sidebar import SidebarV2Case
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

QML_DIR = FONT_DIR.parent

# A: surface -> (file, the line that gives it its transition).
INVENTORY = {
    "every menu (Gezin, conversation, model, effort, project picker, usage, edit, quote, layout)":
        ("components/MenuSurface.qml",
         'NumberAnimation { property: "scale"; from: 1 - Theme.travel / Math.max(1, width, height); to: 1; '
         'duration: Theme.panelIn; easing.type: Theme.ease }'),
    "menu close: the quicker fade":
        ("components/MenuSurface.qml", 'NumberAnimation { property: "opacity"; to: 0; duration: Theme.panelOut; easing.type: Theme.ease }'),
    "sidebar section toggle (Sohbetler, Projeler, Sabitlenenler, date groups)":
        ("components/Sidebar.qml",
         'NumberAnimation { id: sectionMotion; target: sidebar; property: "motionT"; from: 0; to: 1; duration: Theme.panelIn'),
    "date group height": ("components/Sidebar.qml", "height: sidebar.moved(key, implicitHeight); clip: height !== implicitHeight"),
    "date group rows fade": ("components/Sidebar.qml", "appear: sidebar.revealed(group.key)"),
    "pinned height": ("components/Sidebar.qml", 'height: sidebar.moved("pinned", implicitHeight); clip: height !== implicitHeight'),
    "pinned rows fade": ("components/Sidebar.qml", 'appear: sidebar.revealed("pinned")'),
    "plain conversations (the P1 bar) fade": ("components/Sidebar.qml", 'opacity: sidebar.revealed("plain")'),
    "project tree height": ("components/Sidebar.qml", "height: sidebar.treeMoving ? sidebar.moved(sidebar.motionKey, sidebar.treeH) : sidebar.treeH"),
    "project tree fade (PROJELER)": ("components/Sidebar.qml", 'opacity: sidebar.revealed("projects")'),
    "a project's rows fade": ("components/Sidebar.qml", 'appear: isProject ? 1 : sidebar.revealed("project:" + modelData.project)'),
    "chevrons turn": ("components/Sidebar.qml", "Behavior on rotation { NumberAnimation { duration: Theme.panelIn; easing.type: Theme.ease } }"),
    "conversation switch, canonical": ("pages/CommandCenter.qml", "onShownConversationChanged: flowIn.restart()"),
    "conversation switch, Command preset": ("pages/CommandPreset.qml", "onShownConversationChanged: presetIn.restart()"),
    "header title": ("components/ChatHeader.qml", "onShownConversationChanged: titleIn.restart()"),
    "search results in the navigation's place": ("components/SidebarSearch.qml", "opacity: search.active ? 1 : 0"),
    "collapsed selector choices": ("components/Collapsed.qml", "opacity: field.open ? 1 : 0"),
    "collapsed selector height": ("components/Collapsed.qml", "Behavior on height { NumberAnimation { duration: Theme.hoverMs"),
    "pages (Komuta Merkezi / Gorevler / Otomasyonlar / Hafiza / Gecmis / Projeler)":
        ("Main.qml", 'PropertyAction { target: pageSlide; property: "y"; value: Theme.travel }'),
    "side panels (settings, context, summary peek)":
        ("components/ContextPanel.qml", "Behavior on opacity { NumberAnimation { duration: context.open ? Theme.panelIn : Theme.panelOut"),
    "work panel": ("components/WorkPanel.qml", "Behavior on width { NumberAnimation { duration: Theme.dur(180)"),
    "panel column slots": ("components/PanelColumn.qml", "transform: Translate { id: slide; x: Theme.travel }"),
    "non-destructive dialogs": ("components/JarvisDialog.qml", "enter: moves ? enterMotion : null"),
    "command palette": ("overlays/CommandPalette.qml", "Behavior on scale"),
    "attachment preview": ("overlays/AttachmentPreview.qml", "Behavior on scale"),
}
# The no-effect list (K2): DFMOTION adds nothing to them.
NO_EFFECT = ("components/ApprovalCard.qml", "overlays/ApprovalOverlay.qml", "components/AttentionStrip.qml",
             "components/ConversationDeleteDialog.qml", "overlays/ConfirmDialog.qml", "components/PowerControl.qml",
             "components/FreezeKeySwitch.qml", "components/SafetyCards.qml")


class InventoryTests(unittest.TestCase):
    def test_every_surface_has_its_transition(self) -> None:
        missing = [surface for surface, (name, line) in INVENTORY.items()
                   if line not in (QML_DIR / name).read_text(encoding="utf-8")]
        self.assertEqual([], missing)

    def test_the_no_effect_list_gets_nothing(self) -> None:
        for name in NO_EFFECT:
            self.assertNotIn("DFMOTION", (QML_DIR / name).read_text(encoding="utf-8"), name)
        self.assertIn("readonly property bool moves: Theme.ambient && !danger",
                      (QML_DIR / "components/JarvisDialog.qml").read_text(encoding="utf-8"))


class MotionCase(SidebarV2Case):
    def popup_item(self, root, surface):
        return ui.shown_named(root, surface).parentItem()            # the background's parent: the popup's item

    def group(self, root, key):
        heads = [h for h in self.shown(root, "sidebarGroupHead") if h.property("key") == key]
        self.assertEqual(1, len(heads), key)
        return heads[0], heads[0].parentItem()

    def flow(self, root):
        return ui.shown_named(root, "conversationFeed").parentItem()

    def title(self, root):
        return ui.shown_named(root, "chatHeaderTitle")

    def rows(self, root, name, title):
        return [r for r in self.shown(root, name) if r.property("title") == title]


class LiveMotionTests(MotionCase):
    ambient = True      # THIZ: its subject is motion

    def test_b1_the_gezin_menu_opens_with_fade_travel_and_scale_and_closes(self) -> None:
        self.data()
        _bridge, root = self.open_window()
        QTest.keyClick(root, Qt.Key_M, Qt.ControlModifier)
        menu = self.popup_item(root, "navSurface")
        self.assertLess(menu.scale(), 1)                                    # it starts a little smaller ...
        self.assertGreaterEqual(menu.scale(), 1 - 4 / max(menu.width(), menu.height()) - 1e-6)   # ... by <= Theme.travel
        self.assertLess(menu.opacity(), 1)
        self.assertTrue(wait_until(lambda: menu.scale() == 1 and menu.opacity() == 1, 1000))
        QTest.keyClick(root, Qt.Key_Escape)
        self.assertTrue(wait_until(lambda: not menu.isVisible(), 1000))

    def test_b2_a_date_group_glides_closed_and_open_and_its_rows_fade_in(self) -> None:
        self.data()
        _bridge, root = self.open_window()
        head, group = self.group(root, "today")
        before = group.height()
        self.assertEqual(2, len(self.rows(root, "sidebarConversation", "Düz 0") + self.rows(root, "sidebarConversation", "Düz 1")))
        side = ui.shown_named(root, "sidebar")
        ui.tap(root, head)                                                  # collapse
        head, group = self.group(root, "today")
        self.assertEqual(("today", before, False), (side.property("motionKey"), side.property("motionFrom"),
                                                    side.property("motionOpen")))
        self.assertLess(side.property("motionT"), 1)                        # mid-glide (the click took a frame or so)
        self.assertGreater(group.height(), group.implicitHeight() + 0.5)    # from its old height ...
        self.assertTrue(wait_until(lambda: abs(group.height() - group.implicitHeight()) < 0.5, 1000))
        self.assertLess(group.height(), before)                             # ... down to the collapsed one
        closed = group.height()
        ui.tap(root, head)                                                  # open again
        head, group = self.group(root, "today")
        self.assertEqual((closed, True), (side.property("motionFrom"), side.property("motionOpen")))
        self.assertLess(side.property("motionT"), 1)
        rows = self.rows(root, "sidebarConversation", "Düz 0")
        self.assertEqual(1, len(rows))
        self.assertLess(rows[0].opacity(), 1)                               # the opening rows fade in
        self.assertLess(group.height(), before - 0.5)                       # and the group grows
        self.assertTrue(wait_until(lambda: rows[0].opacity() == 1 and abs(group.height() - before) < 0.5, 1000))

    def test_b3_a_switch_brings_the_feed_and_title_in_and_typing_goes_on_at_once(self) -> None:
        store = self.data()
        from tests.test_dfnew import say
        say(store, self.plain[0].id, "bir")
        say(store, self.plain[1].id, "iki")
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        pump(400)
        flow, title = self.flow(root), self.title(root)
        self.assertEqual(1, flow.opacity())
        composer = ui.shown_named(root, "composerInput")
        composer.forceActiveFocus()
        for switch, target in ((lambda: ui.tap(root, self.rows(root, "sidebarConversation", "Düz 1")[0]), self.plain[1].id),
                               (lambda: ui.tap(root, ui.shown_named(root, "sidebarNewChat")), None)):
            with self.subTest(target=target or "new chat"):
                composer.setProperty("text", "")
                switch()
                if target:
                    self.assertEqual(target, bridge.conversationId)
                else:
                    self.assertTrue(bridge.newChatOpen)
                area = flow if target else ui.shown_named(root, "conversationWelcome").parentItem()
                self.assertLess(area.opacity(), 1)                          # mid-transition ...
                self.assertLess(title.opacity(), 1)
                started = time.monotonic()
                ui.tap(root, composer)                                      # the row took the focus: a click back
                for key in "abc":                                           # ... and the keys land at once
                    QTest.keyClick(root, key)
                self.assertEqual("abc", composer.property("text"))
                self.assertTrue(composer.hasActiveFocus())
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(wait_until(lambda: area.opacity() == 1 and title.opacity() == 1, 1000))


class AmbientOffTests(MotionCase):
    def test_c_ambient_off_nothing_moves(self) -> None:
        store = self.data()
        from tests.test_dfnew import say
        say(store, self.plain[1].id, "iki")
        bridge, root = self.open_window()
        QTest.keyClick(root, Qt.Key_M, Qt.ControlModifier)
        menu = self.popup_item(root, "navSurface")
        self.assertEqual((1, 1), (menu.scale(), menu.opacity()))
        QTest.keyClick(root, Qt.Key_Escape)
        pump(50)
        head, group = self.group(root, "today")
        ui.tap(root, head)
        pump(30)                                                            # one polish: the rebuilt group's size
        head, group = self.group(root, "today")
        self.assertEqual(group.implicitHeight(), group.height())
        ui.tap(root, head)
        pump(30)
        head, group = self.group(root, "today")
        self.assertEqual(group.implicitHeight(), group.height())
        self.assertEqual([1.0, 1.0], [r.opacity() for r in self.shown(root, "sidebarConversation")
                                      if r.property("title") in ("Düz 0", "Düz 1")])
        self.assertTrue(bridge.openConversation(self.plain[1].id))
        pump(30)
        self.assertEqual((1, 1), (self.flow(root).opacity(), self.title(root).opacity()))


class FrameTests(MotionCase):
    ambient = True      # THIZ: frames of motion

    def frames(self, root, folder, surface, area, act, count=14, step=16):
        out = Path(folder) / surface
        out.mkdir(parents=True, exist_ok=True)
        images = [root.grabWindow().copy(area)]
        act()
        for _ in range(count):
            images.append(root.grabWindow().copy(area))
            pump(step)
        for i, image in enumerate(images):
            self.assertTrue(image.save(str(out / f"frame_{i:03d}.png")))
        try:
            from PIL import Image
        except ImportError:                                                 # never pip install: frames only
            return
        pictures = [Image.open(out / f"frame_{i:03d}.png").convert("RGB") for i in range(len(images))]
        pictures[0].save(out / f"{surface}.gif", save_all=True, append_images=pictures[1:], duration=40, loop=0)

    def test_frames_for_salih(self) -> None:
        folder = os.environ.get("JARVIS_SHOTS_DIR")
        if not folder:                                                      # still runs: the frames go to a temp folder
            temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            self.addCleanup(temp.cleanup)
            folder = temp.name
        store = self.data()
        from tests.test_dfnew import say
        say(store, self.plain[0].id, "Merhaba JARVIS, bugün neler yapacağız?")
        say(store, self.plain[1].id, "Projeyi özetler misin?")
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        pump(400)
        side = ui.shown_named(root, "sidebar")
        corner = side.mapToScene(side.boundingRect().topLeft()).toPoint()
        left = QRect(corner.x(), corner.y(), int(side.width()), int(side.height()))
        whole = QRect(0, 0, int(root.width()), int(root.height()))
        self.frames(root, folder, "gezin_open", whole, lambda: QTest.keyClick(root, Qt.Key_M, Qt.ControlModifier))
        self.frames(root, folder, "gezin_close", whole, lambda: QTest.keyClick(root, Qt.Key_Escape))
        pump(300)
        self.frames(root, folder, "section_collapse", left, lambda: ui.tap(root, self.group(root, "today")[0]))
        pump(300)
        self.frames(root, folder, "section_expand", left, lambda: ui.tap(root, self.group(root, "today")[0]))
        pump(300)
        self.frames(root, folder, "conversation_switch", whole,
                    lambda: ui.tap(root, self.rows(root, "sidebarConversation", "Düz 1")[0]))
        pump(300)
        self.frames(root, folder, "new_chat", whole, lambda: ui.tap(root, ui.shown_named(root, "sidebarNewChat")))
        pump(300)
        self.frames(root, folder, "page_change", whole, lambda: bridge.setPage("tasks"))


if __name__ == "__main__":
    unittest.main()
