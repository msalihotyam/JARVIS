"""Gate 6G K4: one menu and one dialog style (app audit, Salih 2026-10-05).

* Every popup menu is a MenuSurface with MenuRow rows (no stock Menu / MenuItem):
  opaque Theme.menuSurface, Theme.popupRadius, rows Theme.menuRowHeight (36).
* Keys: focus enters the menu (its first available row), Up / Down move through
  its rows, Esc closes it and returns focus to the opener; a press outside closes
  it; a press on its trigger toggles it.
* Dialogs (JarvisDialog, ConfirmDialog, Resume) share the radius and the scrim
  (Theme.scrimOpacity); focus starts on the safe choice and returns to the opener.
  A destructive confirm keeps its motion-free input guard (K2 2adf1bb).
* M1: the header's "…" and a sidebar row's "…" / right-click open the SAME
  conversation menu content; only the clicked opener shows open.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import re
import time
import unittest
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest

from tests.helpers import wait_until
from tests.test_projects_ui_6f import F6Case, settle
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import ambient_on, pump

QML = Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "qml"
MENUS = ("components/ConversationMenu.qml", "components/ChatHeader.qml", "components/EditMenu.qml",
         "components/RenameMenu.qml", "components/ContextStrip.qml", "components/Composer.qml",
         "components/NavMenu.qml", "overlays/LayoutMenus.qml")


class MenuSourceTests(unittest.TestCase):
    def test_every_menu_is_the_one_surface_and_no_stock_menu_is_left(self) -> None:
        stock = re.compile(r"^\s*(Menu|MenuItem|MenuSeparator)\s*\{", re.M)
        popup = re.compile(r"(^|[\s:])Popup\s*\{", re.M)
        popups = set()
        for path in QML.rglob("*.qml"):
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(QML).as_posix()
            self.assertIsNone(stock.search(text), rel)
            if popup.search(text):
                popups.add(rel)
        # The surface itself, the dialog shell and the edit menu's "Copied" note - nothing else.
        self.assertEqual({"components/MenuSurface.qml", "components/JarvisDialog.qml", "components/EditMenu.qml"}, popups)
        for rel in MENUS:
            self.assertIn("MenuSurface", (QML / rel).read_text(encoding="utf-8"), rel)

    def test_dialogs_share_the_radius_and_the_scrim(self) -> None:
        for rel in ("components/JarvisDialog.qml", "overlays/ConfirmDialog.qml", "Main.qml", "components/MenuSurface.qml"):
            text = (QML / rel).read_text(encoding="utf-8")
            self.assertIn("Theme.popupRadius", text, rel)
        for rel in ("components/JarvisDialog.qml", "overlays/ConfirmDialog.qml", "Main.qml", "components/MenuSurface.qml"):
            self.assertIn("Theme.scrimOpacity", (QML / rel).read_text(encoding="utf-8"), rel)


class MenuCase(F6Case):
    def seed_two(self):
        store = self.db()
        self.g0 = store.create_conversation(title="Genel 0")
        self.g1 = store.create_conversation(title="Genel 1")
        return store

    def context(self, root):
        return self.popup_named(root, "conversationSwitcherContext")

    def is_open(self, popup) -> bool:
        return bool(popup.property("opened")) and popup.property("opacity") == 1.0

    def focus(self, root):
        return root.activeFocusItem()

    def rows_of(self, popup) -> list:
        return [i for i in self.descendants(popup.property("contentItem")) if i.property("menuRow") is True and i.isVisible()]

    def key(self, root, key, modifier=Qt.NoModifier) -> None:
        QTest.keyClick(root, key, modifier)
        pump(60)


class MenuStyleTests(MenuCase):
    def surface_ok(self, popup, name) -> None:
        body = popup.property("background")
        self.assertEqual(1.0, QColor(body.property("color")).alphaF(), name)          # opaque
        self.assertEqual(10, body.property("radius"), name)                            # Theme.popupRadius
        for row in self.rows_of(popup):
            if not row.property("detail"):
                self.assertEqual(36, row.height(), (name, row.objectName()))          # Theme.menuRowHeight

    def test_the_menus_are_opaque_one_radius_rows_36(self) -> None:
        self.seed_two()
        bridge = self.bridge()
        root = self.window(bridge)
        # The conversation menu (a row's), the nav menu, the edit menu and a composer chip's.
        self.menu(root, self.row(root, "sidebarConversation", "Genel 0"))
        self.assertGreaterEqual(len(self.rows_of(self.context(root))), 4)
        self.surface_ok(self.context(root), "conversation menu")
        self.close_menu(root)
        self.tap(root, self.item(root, "navigationTrigger"))
        nav = self.popup_named(root, "navMenu")
        self.assertTrue(settle(lambda: self.is_open(nav)))
        self.assertEqual(6, len(self.rows_of(nav)))
        self.assertEqual("Ctrl+2", self.item(root, "navigate_tasks").property("shortcut"))
        self.surface_ok(nav, "nav menu")
        self.key(root, Qt.Key_Escape)
        self.tap(root, self.item(root, "composerInput"), Qt.RightButton)
        edit = self.popup_named(root, "composerEditMenu")
        self.assertTrue(settle(lambda: self.is_open(edit)))
        self.surface_ok(edit, "edit menu")
        self.key(root, Qt.Key_Escape)
        self.tap(root, self.item(root, "permissionModeControl"))
        mode = self.popup_named(root, "permissionModeMenu")
        self.assertTrue(settle(lambda: self.is_open(mode)))
        self.surface_ok(mode, "permission mode menu")
        self.closed(mode, 400)
        self.assertEqual(0, self.execute_calls())


class MenuKeyTests(MenuCase):
    def test_focus_enters_arrows_move_and_esc_returns_it_to_the_opener_motion_free(self) -> None:
        self.keys()

    @ambient_on          # the menus' enter / exit motion must not move the focus
    def test_focus_enters_arrows_move_and_esc_returns_it_to_the_opener_with_motion(self) -> None:
        self.keys()

    def keys(self) -> None:
        self.seed_two()
        bridge = self.bridge()
        root = self.window(bridge)
        context = self.context(root)
        # The header's "…" from the keyboard.
        button = self.item(root, "chatHeaderMenu")
        button.forceActiveFocus()
        self.key(root, Qt.Key_Return)
        self.assertTrue(settle(lambda: self.is_open(context)))
        self.assertEqual("conversationPinItem", self.focus(root).objectName())            # the first available row
        self.key(root, Qt.Key_Down)
        self.assertEqual("conversationRenameItem", self.focus(root).objectName())         # Move up / down are hidden
        self.key(root, Qt.Key_Up)
        self.assertEqual("conversationPinItem", self.focus(root).objectName())
        self.key(root, Qt.Key_Up)                                                          # it wraps
        self.assertEqual("conversationDeleteItem", self.focus(root).objectName())
        self.key(root, Qt.Key_Escape)
        self.assertTrue(wait_until(lambda: not context.property("visible"), 400))
        self.assertTrue(button.hasActiveFocus())                                           # back on the opener
        # A sidebar row from the keyboard (the menu key): Esc returns to that row.
        row = self.row(root, "sidebarConversation", "Genel 0")
        self.into_view(root, row)
        row.forceActiveFocus()
        self.key(root, Qt.Key_Menu)
        self.assertTrue(settle(lambda: self.is_open(context)))
        self.assertEqual(self.g0.id, self.item(root, "conversationMenu").property("targetId"))
        self.assertEqual("conversationPinItem", self.focus(root).objectName())
        self.key(root, Qt.Key_Escape)
        self.assertTrue(wait_until(lambda: not context.property("visible"), 400))
        self.assertTrue(row.hasActiveFocus())
        self.assertEqual(self.g1.id, bridge.conversationId)                                 # nothing was opened
        # Enter on a focused row chooses it: Rename opens its editor, focus in the name; Esc goes back.
        button.forceActiveFocus()
        self.key(root, Qt.Key_Return)
        self.assertTrue(settle(lambda: self.is_open(context)))
        self.key(root, Qt.Key_Down)
        self.key(root, Qt.Key_Return)
        editor = self.popup_named(root, "conversationRenameEditor")
        self.assertTrue(settle(lambda: bool(editor.property("opened"))))
        self.assertEqual("conversationRenameField", self.focus(root).objectName())
        self.key(root, Qt.Key_Escape)
        self.assertTrue(wait_until(lambda: not editor.property("visible"), 400))
        self.assertTrue(button.hasActiveFocus())
        self.assertEqual("Genel 1", bridge.conversationTitle)                               # reverted
        # The nav menu (Ctrl+M): its first row, then back to the top bar's grid.
        self.key(root, Qt.Key_M, Qt.ControlModifier)
        nav = self.popup_named(root, "navMenu")
        self.assertTrue(settle(lambda: self.is_open(nav)))
        self.assertEqual("navigate_home", self.focus(root).objectName())
        self.key(root, Qt.Key_Escape)
        self.assertTrue(wait_until(lambda: not root.property("navOpen"), 400))
        self.assertTrue(self.item(root, "navigationTrigger").hasActiveFocus())
        self.assertEqual(0, self.execute_calls())

    def test_a_press_outside_closes_and_the_trigger_toggles(self) -> None:
        self.seed_two()
        bridge = self.bridge()
        root = self.window(bridge)
        context = self.context(root)
        self.menu(root, self.item(root, "chatHeaderMenu"), Qt.LeftButton)
        feed = self.item(root, "conversationFeed")
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, feed.mapToScene(feed.boundingRect().center()).toPoint())
        self.assertTrue(wait_until(lambda: not context.property("visible"), 400))
        # The "…" again while its menu is open: it closes and stays closed.
        self.menu(root, self.item(root, "chatHeaderMenu"), Qt.LeftButton)
        self.tap(root, self.item(root, "chatHeaderMenu"), until=lambda: not context.property("visible"))
        pump(200)
        self.assertFalse(context.property("visible"))
        # The nav grid the same.
        self.tap(root, self.item(root, "navigationTrigger"), until=lambda: root.property("navOpen"))
        self.tap(root, self.item(root, "navigationTrigger"), until=lambda: not root.property("navOpen"))
        pump(200)
        self.assertFalse(root.property("navOpen"))
        self.tap(root, self.item(root, "navigationTrigger"), until=lambda: root.property("navOpen"))
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(root.width() // 2, root.height() // 2))
        self.assertTrue(wait_until(lambda: not root.property("navOpen"), 400))
        self.assertEqual(0, self.execute_calls())


class SameMenuTests(MenuCase):
    def labels(self, root) -> list:
        return [(r.objectName(), r.property("label")) for r in self.rows_of(self.context(root))]

    def test_m1_the_header_and_the_open_rows_menu_are_the_same_and_only_the_clicked_one_opens(self) -> None:
        self.seed_two()
        bridge = self.bridge()
        root = self.window(bridge)
        menu = self.item(root, "conversationMenu")
        header = self.item(root, "chatHeaderMenu")
        own = self.row(root, "sidebarConversation", "Genel 1")                 # the open conversation's own row
        self.assertTrue(own.property("current"))
        self.menu(root, own)
        self.assertEqual(bridge.conversationId, menu.property("targetId"))
        self.assertTrue(own.property("menuHere"))
        self.assertFalse(header.property("open"))                                # F7 follow-up: the header stays unlit
        from_row = self.labels(root)
        # The header's "…" while the row's menu is open: that one closes, the header's opens - the same content.
        self.menu(root, header, Qt.LeftButton)
        self.assertTrue(header.property("open"))
        self.assertFalse(own.property("menuHere"))
        self.assertEqual(from_row, self.labels(root))
        self.assertEqual(1, len(root.findChildren(QObject, "conversationSwitcherContext")))
        # And back: the row's opens, the header's is no longer marked.
        self.menu(root, own)
        self.assertTrue(own.property("menuHere"))
        self.assertFalse(header.property("open"))
        self.close_menu(root)
        self.assertEqual(0, self.execute_calls())


class DialogStyleTests(MenuCase):
    def test_a_destructive_confirm_from_the_menu_keeps_its_guard_and_returns_focus(self) -> None:
        store = self.seed_two()
        bridge = self.bridge()
        root = self.window(bridge)
        header = self.item(root, "chatHeaderMenu")
        header.forceActiveFocus()
        self.key(root, Qt.Key_Return)
        self.assertTrue(settle(lambda: self.is_open(self.context(root))))
        self.key(root, Qt.Key_Up)                                                           # wraps to Delete permanently…
        self.assertEqual("conversationDeleteItem", self.focus(root).objectName())
        opened = time.monotonic()
        QTest.keyClick(root, Qt.Key_Return)
        dialog = next(d for d in root.findChildren(QObject, "conversationDeleteDialog") if d.property("visible"))
        confirm = next(i for i in self.all_items(root, "deleteConfirm") if i.isVisible())
        self.assertTrue(settle(lambda: bool(dialog.property("opened"))))
        self.assertEqual(10, dialog.property("background").property("radius"))             # Theme.popupRadius
        self.assertFalse(confirm.isEnabled())                                               # the K2 input guard
        self.assertTrue(settle(confirm.isEnabled))
        self.assertGreaterEqual(time.monotonic() - opened, 0.3 - 0.05)
        self.assertEqual("deleteCancel", self.focus(root).objectName())                     # the safe choice
        self.key(root, Qt.Key_Escape)
        self.assertTrue(wait_until(lambda: not dialog.property("visible"), 400))
        self.assertTrue(header.hasActiveFocus())                                            # back on the "…"
        self.assertFalse(store.is_conversation_deleted(self.g1.id))
        self.assertEqual(0, self.execute_calls())

    def test_the_confirm_dialog_focuses_its_safe_choice_and_returns_focus(self) -> None:
        self.seed_two()
        bridge = self.bridge()
        root = self.window(bridge)
        cancel = self.item(root, "confirmCancel")
        dialog = cancel.parentItem()
        while dialog is not None and dialog.property("armed") is None:
            dialog = dialog.parentItem()
        opener = self.item(root, "composerInput")
        opener.forceActiveFocus()
        pump(50)
        dialog.setProperty("open", True)
        self.assertTrue(settle(cancel.hasActiveFocus))                                      # the safe choice
        dim, panel = dialog.childItems()[0], next(c for c in dialog.childItems() if c.property("raised") is True)
        self.assertTrue(settle(lambda: abs(dim.opacity() - 0.56) < 1e-6))                   # Theme.scrimOpacity
        self.assertEqual(10, panel.property("radius"))                                      # Theme.popupRadius
        self.key(root, Qt.Key_Escape)
        self.assertFalse(dialog.property("open"))
        self.assertTrue(opener.hasActiveFocus())
        self.assertEqual(0, self.execute_calls())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
