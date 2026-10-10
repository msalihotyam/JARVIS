"""G6MENUFIX: a menu is placed by its real size, and placed again when its size changes while open.

ConversationMenu.openFor() changed which rows show and called MenuSurface.popupAt() in the same
call; the Column lays out only at the next polish, so popupAt clamped with the LAST open's height.
A Global row shows the Attach row (one row taller): at 1100x700 its menu ran past the window's
bottom (INT14-AFX). These open the menu near the window bottom and let the polish run, so the
stale height shows without load.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtQml import QQmlEngine, QQmlExpression

from tests.helpers import settled
from tests.test_projects_ui_6f import F6Case, settle
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import ambient_on, pump

WINDOW = QRectF(0, 0, 1100, 700)


class MenuPlaceTests(F6Case):
    def setup_window(self):
        store = self.db()
        tez = store.create_project("Tez")
        self.owned = store.create_conversation(tez.id, title="Tez 0")
        plain = {c.id: c for c in (store.create_conversation(title=f"Genel {n}") for n in range(3))}
        bridge = self.bridge()
        for cid in plain:
            self.assertTrue(bridge.pinConversation(cid))
        self.pins = [plain[p["id"]] for p in bridge.pinnedConversations]          # SABİTLENDİ order
        root = self.window(bridge)
        self.resize_still(root, 1100, 700, 900)
        return root

    def run_js(self, root, code: str) -> None:
        side = self.item(root, "sidebar")
        _, undefined = QQmlExpression(QQmlEngine.contextForObject(side), side, code + "; true").evaluate()
        self.assertFalse(undefined, code)

    def open_for(self, root, conversation, plain: bool) -> None:
        """A sidebar row's menu (the row's own arguments), opened at the window's bottom left."""
        self.run_js(root, f"sidebar.conversationMenu.openFor('{conversation.id}', '{conversation.title}', sidebar,"
                          f" sidebar.mapFromItem(null, 40, {WINDOW.height() - 12}), null, null, false, {str(plain).lower()})")

    def opened(self, root):
        menu = self.popup_named(root, "conversationSwitcherContext")
        self.assertTrue(settle(lambda: bool(menu.property("opened")) and menu.property("opacity") == 1.0))
        return menu

    def assert_inside(self, root, menu, what: str, shown: str) -> None:
        settled(200, lambda: (
            self.assertTrue(self.item(root, shown).isVisible(), what),
            self.assertEqual(menu.property("restY"), menu.property("y"), what),
            self.inside(WINDOW.adjusted(7.5, 7.5, -7.5, -7.5), self.rect(menu.property("background")), what),
        ))

    def test_a_menu_is_placed_by_its_real_height_at_once(self) -> None:
        root = self.setup_window()
        menu = self.popup_named(root, "conversationSwitcherContext")
        self.open_for(root, self.pins[0], True)                  # the first pin: "Move down" only - one row shorter
        self.opened(root)
        self.assertFalse(self.item(root, "conversationPinUpItem").isVisible())
        self.close_menu(root)
        self.open_for(root, self.pins[1], True)                  # a middle pin: "Move up" and "Move down"
        first = (menu.property("implicitHeight"), menu.property("restY"))   # before any polish
        self.opened(root)
        pump(100)                                                 # the polish has run
        self.assert_inside(root, menu, "middle pin menu", "conversationPinUpItem")
        self.assertTrue(self.item(root, "conversationAttachItem").isVisible())
        self.assertEqual(first, (menu.property("implicitHeight"), menu.property("restY")))
        self.assertEqual(0, self.execute_calls())

    def test_a_height_change_while_open_places_it_again(self) -> None:
        root = self.setup_window()
        self.open_for(root, self.owned, False)
        menu = self.opened(root)
        before = menu.property("restY")
        # An archived owner while open: two reasons show, the delete reason goes - one reason taller.
        self.run_js(root, "sidebar.conversationMenu.rowOwnerArchived = true")
        self.assert_inside(root, menu, "grown while open", "conversationArchiveReason")
        self.assertLess(menu.property("restY"), before)
        self.assertEqual(0, self.execute_calls())

    @ambient_on
    def test_a_height_change_during_the_entrance_ends_at_the_new_place(self) -> None:
        root = self.setup_window()
        menu = self.popup_named(root, "conversationSwitcherContext")
        self.open_for(root, self.owned, False)
        before = menu.property("restY")
        self.run_js(root, "sidebar.conversationMenu.rowOwnerArchived = true")   # before menuIn has ended
        self.assertFalse(menu.property("opened"))
        self.opened(root)
        self.assert_inside(root, menu, "grown during menuIn", "conversationArchiveReason")
        self.assertLess(menu.property("restY"), before)
        self.assertEqual(0, self.execute_calls())
