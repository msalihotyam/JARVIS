"""6G F9 (Salih 2026-10-05, mockup v16): the chat header and the conversation menu.

H1 "Projeye bağla" is not in the chat header any more: it is a ConversationMenu
   item (header "…", a row's "⋯" / right-click), Global conversations only, the
   same 6C dialog (preview + explicit confirm, one-way).
H2 a project chat's scope line sits ABOVE the title, faint (secondary), and is a
   link to that project's page (bridge.showProject); a no-project chat has no
   line, so nothing there is a link.
H3 the menu after the reference: a K1 icon per item, separators, the
   destructive "Kalıcı sil…" last in danger.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from PySide6.QtCore import QMetaObject, Qt
from PySide6.QtGui import QColor

from tests.helpers import wait_until
from tests.test_projects_ui_6f import F6Case, attach_dialog, settle
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

QML = Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "qml" / "components"
ORDER = ["conversationPinItem", "conversationRenameItem", "conversationAttachItem",
         "conversationArchiveItem", "conversationDeleteItem"]


class HeaderMenuCase(F6Case):
    def scoped(self):
        store = self.db()
        self.tez = store.create_project("Tez")
        self.genel = [store.create_conversation(title=f"Genel {n}") for n in range(2)]
        self.tezc = store.create_conversation(self.tez.id, title="Tez 0")
        return store

    def y(self, item) -> float:
        return self.rect(item).top()


class H1AttachEntryTests(HeaderMenuCase):
    def test_h1_the_header_has_no_attach_button(self) -> None:
        self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertTrue(bridge.canAttach)                                   # a writable Global one is open
        self.assertEqual([], self.all_items(root, "chatHeaderAttach"))
        self.assertNotIn("Attach to a project", (QML / "ChatHeader.qml").read_text(encoding="utf-8"))
        self.assertEqual(0, self.execute_calls())

    def test_h1_the_menu_offers_it_for_global_and_not_for_a_project(self) -> None:
        store = self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        # The open Global conversation, from the header "…".
        self.menu(root, self.item(root, "chatHeaderMenu"), Qt.LeftButton)
        item = self.item(root, "conversationAttachItem")
        self.assertEqual((True, True, "Attach to a project…"), (item.isVisible(), item.isEnabled(), item.property("label")))
        self.close_menu(root)
        # A project conversation's row: no entry (6C is Global → project only).
        self.menu(root, self.row(root, "sidebarProjectConversation", "Tez 0"))
        self.assertFalse(self.item(root, "conversationAttachItem").isVisible())
        self.close_menu(root)
        # A project conversation open, from its header "…": no entry either.
        self.assertTrue(bridge.openConversation(self.tezc.id))
        self.menu(root, self.item(root, "chatHeaderMenu"), Qt.LeftButton)
        self.assertFalse(self.item(root, "conversationAttachItem").isVisible())
        self.close_menu(root)
        # Another Global conversation's row: the entry opens THAT one, then the same dialog previews it.
        other = self.genel[0]
        self.choose(root, self.row(root, "sidebarConversation", "Genel 0"), "conversationAttachItem",
                    label="Attach to a project…")
        dialog = attach_dialog(root)
        self.assertTrue(settle(lambda: bool(dialog.property("visible"))))
        self.assertEqual(other.id, bridge.conversationId)
        choice = [i for i in self.all_items(root, "attachProject") if i.property("label") == "Tez"][0]
        self.tap(root, choice, then=lambda: (
            self.assertEqual("Target: Tez", self.item(root, "attachTarget").property("text")),
            self.assertIn("Genel 0", self.item(root, "attachIdentity").property("text")),
        ))
        self.assertIsNone(store.load_conversation(other.id).project_id)    # nothing before the explicit confirm
        self.tap(root, self.item(root, "attachConfirm"))
        self.assertEqual(self.tez.id, store.load_conversation(other.id).project_id)
        self.assertEqual(0, self.execute_calls())

    def test_h1_a_busy_run_disables_it_with_its_reason(self) -> None:
        self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._busy = True
        bridge.stateChanged.emit()
        menu = self.item(root, "conversationMenu")
        wait_until(lambda: not menu.property("attachAllowed"), 400)
        self.assertEqual((True, False), (menu.property("attachShown"), menu.property("attachAllowed")))
        self.assertEqual("Wait for the current run to finish.", menu.property("attachReason"))
        bridge._busy = False
        bridge.stateChanged.emit()


class H2ScopeLinkTests(HeaderMenuCase):
    def test_h2_a_project_label_opens_its_page_and_genel_has_no_link(self) -> None:
        self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        header = self.item(root, "chatHeader")
        # Genel: no line, and the link's handler does nothing there.
        self.assertFalse(self.item(root, "chatHeaderScope").isVisible())
        QMetaObject.invokeMethod(header, "pickScope")
        pump(100)
        self.assertEqual("home", bridge.page)
        # A project chat: a click on its name opens that project's page.
        self.assertTrue(bridge.openConversation(self.tezc.id))
        pump(200)
        scope = self.item(root, "chatHeaderScope")
        self.assertTrue(scope.isVisible())
        self.tap(root, scope, until=lambda: bridge.page == "projects")
        self.assertEqual(("projects", self.tez.id), (bridge.page, bridge.openProject.get("id")))
        self.assertEqual(0, self.execute_calls())

    def test_h2_the_label_is_faint_small_and_above_the_title(self) -> None:
        self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        self.assertTrue(bridge.openConversation(self.tezc.id))
        pump(200)
        name, title = self.item(root, "chatHeaderScopeName"), self.item(root, "chatHeaderTitle")
        scope = self.item(root, "chatHeaderScope")
        self.assertLessEqual(self.rect(scope).bottom(), self.rect(title).top() + 0.5)        # higher: above the title
        self.assertLess(name.property("font").pixelSize(), title.property("font").pixelSize())
        self.assertLess(QColor(name.property("color")).lightness(), QColor(title.property("color")).lightness())
        # Secondary: Theme.textMuted (as the menu's own caption line), on no chip background.
        self.menu(root, self.item(root, "chatHeaderMenu"), Qt.LeftButton)
        muted = QColor(self.item(root, "conversationContextHeader").property("color"))
        self.close_menu(root)
        self.assertEqual(muted, QColor(name.property("color")))
        self.assertEqual(0, QColor(scope.property("color")).alpha())
        # Its tip says where it leads (the one HoverTip).
        self.assertEqual("Open the project page", self.item(root, "chatHeaderScopeTip").property("text"))


class H3MenuStyleTests(HeaderMenuCase):
    def test_h3_order_icons_separators_and_delete_last_in_danger(self) -> None:
        self.scoped()
        bridge = self.bridge()
        root = self.window(bridge)
        self.menu(root, self.item(root, "chatHeaderMenu"), Qt.LeftButton)
        rows = [self.item(root, name) for name in ORDER]
        self.assertTrue(all(r.isVisible() for r in rows), [r.objectName() for r in rows if not r.isVisible()])
        ys = [self.y(r) for r in rows]
        self.assertEqual(sorted(ys), ys)                                            # pin, rename, attach | archive | delete
        self.assertEqual(["pin", "pencil", "folder", "archive", "trash"], [r.property("icon") for r in rows])
        dividers = sorted(self.y(d) for d in self.shown(root, "conversationMenuDivider"))
        self.assertEqual(2, len(dividers))
        self.assertTrue(ys[2] < dividers[0] < ys[3] < dividers[1] < ys[4], (ys, dividers))
        # "Kalıcı sil…" last of every row, destructive, in the danger ink (its trash icon's tint).
        menu = self.popup_named(root, "conversationSwitcherContext")
        every = [i for i in self.descendants(menu.property("contentItem")) if i.property("menuRow") is True and i.isVisible()]
        self.assertEqual(rows[-1], max(every, key=self.y))
        delete = rows[-1]
        self.assertTrue(delete.property("destructive") and delete.isEnabled())
        icon = next(i for i in self.descendants(delete) if i.property("name") == "trash")
        self.assertEqual(QColor(icon.property("tint")), QColor(delete.property("ink")))
        self.assertNotEqual(QColor(rows[0].property("ink")), QColor(delete.property("ink")))
        self.close_menu(root)

    def test_h3_the_reference_items_left_out_are_absent(self) -> None:
        source = (QML / "ConversationMenu.qml").read_text(encoding="utf-8")
        for gone in ("Change project", "Remove from project", "Open in new window", "Mark as unread"):
            self.assertNotIn(gone, source)

    def test_h3_the_new_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        from tests.test_ui import _APP
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Projeye bağla…", tx("ConversationMenu", "Attach to a project…"))
        self.assertEqual("Proje sayfasını aç", tx("ChatHeader", "Open the project page"))


if __name__ == "__main__":
    unittest.main()
