"""Gate 6G R3U - "mark as unread" and the top bar bell, on top of R3's green "finished while you
were elsewhere" (Salih 2026-10-10; idea: stablyai/orca "unread state + bell").

U1  A sidebar row's menu offers "Okunmadı olarak işaretle": the row goes back to R3's green
    (dot + glow) until it is opened again. Never offered on the open conversation.
U2  The top bar bell counts the unseen-finished and the needs-you conversations, each once
    (amber wins), never the open one. A click opens a K4 menu, newest first (title, project,
    state in words); an entry opens its conversation, which clears its green. Empty: no badge
    and "Bekleyen bir şey yok".
U3  Real state only (R3's sources); the marks are bridge memory (no schema, no preference file).
K2  No motion of its own; with ambient effects off the menu opens at once.

Runs are scripted Core / model answers; nothing reaches a model, a network or the live DB.
"""

from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import Qt

from jarvis.ui.bridge import Bridge
from jarvis.ui.preferences import UiPreferences
from tests.test_g6r3_status_dots import ANALYSE, StatusDotCase
from tests.test_intent import task_proposal
from tests.test_motion_6g import QML_DIR
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

MARK = "conversationMarkUnreadItem"


class BellCase(StatusDotCase):
    def bell(self, root):
        return self.item(root, "unreadBell")

    def badge(self, root):
        return self.child(self.bell(root), "unreadBellBadge")

    def open_bell(self, root):
        menu = self.popup_named(root, "unreadBellMenu")
        button = self.child(self.bell(root), "unreadBellButton")
        self.tap(root, button, until=lambda: bool(menu.property("opened")))
        self.assertTrue(bool(menu.property("opened")))
        return menu

    def entries(self, root) -> list:
        return [(e.property("label"), e.property("detail"), e.property("shortcut"))
                for e in self.all_items(root, "unreadBellEntry") if e.isVisible()]

    def mark_by_mouse(self, root, name, title) -> None:
        self.choose(root, self.row(root, name, title), MARK, label="Mark as unread")


class MarkUnreadTests(BellCase):
    def test_a_mark_unread_is_green_until_opened_and_never_offered_on_the_open_one(self) -> None:
        bridge, root = self.open_dots()
        a, b = self.plain[0].id, self.plain[1].id
        self.assertTrue(bridge.openConversation(b))
        pump(100)
        self.assertEqual("", self.status(root, "sidebarConversation", "Düz 0"))
        self.mark_by_mouse(root, "sidebarConversation", "Düz 0")
        self.assertEqual([a], bridge.finishedUnseenIds)
        self.assertEqual("finished", self.status(root, "sidebarConversation", "Düz 0"))
        self.assertTrue(self.glowing(root, "sidebarConversation", "Düz 0"))
        # Already green: the item is not offered again; the open row's menu never offers it.
        menu = self.menu(root, self.row(root, "sidebarConversation", "Düz 0"))
        self.assertFalse(self.item(root, MARK).isVisible())
        menu.close()
        pump(100)
        menu = self.menu(root, self.row(root, "sidebarConversation", "Düz 1"))
        self.assertFalse(self.item(root, MARK).isVisible())
        menu.close()
        pump(100)
        menu = self.menu(root, self.item(root, "chatHeaderMenu"), Qt.LeftButton)
        self.assertFalse(self.item(root, MARK).isVisible())
        menu.close()
        pump(100)
        # The bridge refuses the open one, an unknown and an archived one.
        archived = [c.id for c in self.db().list_conversations(include_archived=True) if c.archived_at is not None][0]
        self.assertEqual([False, False, False, False],
                         [bridge.markConversationUnread(x) for x in (b, "", "nope", archived)])
        # Opened by a click: green and glow clear, and stay cleared after leaving it.
        self.tap(root, self.row(root, "sidebarConversation", "Düz 0"), until=lambda: bridge.conversationId == a)
        self.assertEqual(([], ""), (bridge.finishedUnseenIds, self.status(root, "sidebarConversation", "Düz 0")))
        self.assertFalse(self.glowing(root, "sidebarConversation", "Düz 0"))
        self.assertTrue(bridge.openConversation(b))
        self.assertEqual([], bridge.finishedUnseenIds)

    def test_b_pinned_and_project_rows_too_and_nothing_survives_a_restart(self) -> None:
        bridge, root = self.open_dots()
        self.assertTrue(bridge.pinConversation(self.yday.id))
        pump(100)
        self.mark_by_mouse(root, "sidebarPinnedConversation", "Düz dün")
        self.mark_by_mouse(root, "sidebarProjectConversation", "Tez 0")
        self.assertEqual(sorted([self.yday.id, self.tezc[0].id]), bridge.finishedUnseenIds)
        self.assertEqual("finished", self.status(root, "sidebarProjectConversation", "Tez 0"))
        saved = [p.name for p in self.prefs_path.parent.glob(self.prefs_path.stem + "*")
                 if p.is_file() and self.tezc[0].id in p.read_text(encoding="utf-8")]
        self.assertEqual([], saved)
        again = Bridge(self.backend, UiPreferences(self.prefs_path), sync=True)
        again.refreshConfig()
        self.assertEqual(([], []), (again.finishedUnseenIds, again.unreadBell))


class BellTests(BellCase):
    def test_c_the_count_is_unseen_plus_needs_you_once_amber_wins_never_the_open_one(self) -> None:
        bridge, root = self.open_dots(task_proposal("code_analysis", objective=ANALYSE))
        a, b = self.plain[0].id, self.plain[1].id
        self.assertFalse(self.badge(root).isVisible())                          # nothing there: no badge
        self.run_away(bridge, root, "sidebarConversation", "Düz 0", a, b)
        self.assertEqual(([a], [a]), (bridge.needsYouIds, bridge.finishedUnseenIds))
        self.assertTrue(bridge.markConversationUnread(self.kodc.id))
        pump(50)
        self.assertEqual({a: "needsYou", self.kodc.id: "finished"}, {e["id"]: e["state"] for e in bridge.unreadBell})
        self.assertEqual(2, len(bridge.unreadBell))                             # needs-you counted once
        badge = self.badge(root)
        self.assertTrue(badge.isVisible())
        self.assertEqual(2, self.bell(root).property("count"))
        self.assertTrue(self.bell(root).property("waiting"))                    # amber while one waits
        # Open: the open one is never counted, even while its approval still waits.
        self.assertTrue(bridge.openConversation(a))
        pump(50)
        self.assertEqual([a], bridge.needsYouIds)
        self.assertEqual([self.kodc.id], [e["id"] for e in bridge.unreadBell])
        self.assertEqual((1, False), (self.bell(root).property("count"), self.bell(root).property("waiting")))

    def test_d_newest_first_and_an_entry_click_opens_and_clears(self) -> None:
        bridge, root = self.open_dots()
        self.assertTrue(bridge.openConversation(self.plain[1].id))
        self.stamp(self.older.id, self.now - timedelta(days=5))
        self.stamp(self.tezc[1].id, self.now - timedelta(hours=1))
        for cid in (self.older.id, self.tezc[1].id):
            self.assertTrue(bridge.markConversationUnread(cid))
        bridge.refreshConfig()
        pump(100)
        self.assertEqual([self.tezc[1].id, self.older.id], [e["id"] for e in bridge.unreadBell])
        menu = self.open_bell(root)
        self.assertEqual([("Tez 1", "Tez", "Finished"), ("Düz eski", "Global", "Finished")], self.entries(root))
        entry = [e for e in self.all_items(root, "unreadBellEntry") if e.property("label") == "Tez 1"][0]
        self.tap(root, entry, until=lambda: bridge.conversationId == self.tezc[1].id)
        self.assertEqual(self.tezc[1].id, bridge.conversationId)
        self.assertFalse(menu.property("visible"))
        self.assertEqual([self.older.id], bridge.finishedUnseenIds)
        self.assertEqual(1, self.bell(root).property("count"))

    def test_e_empty_is_honest_and_reduced_motion_opens_at_once(self) -> None:
        bridge, root = self.open_dots()
        bridge.setAmbientEffects(False)
        pump(30)
        self.assertEqual((0, False), (self.bell(root).property("count"), self.badge(root).isVisible()))
        button = self.child(self.bell(root), "unreadBellButton")
        self.assertEqual("Nothing waiting", button.property("tip"))
        menu = self.open_bell(root)
        self.assertEqual(1.0, menu.property("opacity"))                         # K2: no transition
        self.assertEqual([], self.entries(root))
        self.assertTrue(self.item(root, "unreadBellEmpty").isVisible())
        source = (QML_DIR / "components" / "UnreadBell.qml").read_text(encoding="utf-8")
        self.assertNotIn("Animation", source)
        self.assertNotIn("Behavior", source)
        self.tap(root, button, until=lambda: not menu.property("visible"))     # the bell toggles it shut
        self.assertFalse(menu.property("visible"))

    def test_f_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        from tests.test_ui import _APP
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Okunmadı olarak işaretle", tx("ConversationMenu", "Mark as unread"))
        self.assertEqual(["Bekleyen bir şey yok", "Bekleyen: %1", "Seni bekliyor", "Bitti", "Genel"],
                         [tx("UnreadBell", s) for s in ("Nothing waiting", "Waiting: %1", "Waiting for you",
                                                        "Finished", "Global")])


if __name__ == "__main__":
    import unittest
    unittest.main()
