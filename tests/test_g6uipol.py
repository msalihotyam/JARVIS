"""Gate 6G close follow-up G6UIPOL (build/6g/GATE6G_review.md; orchestrator 2026-10-11):
spec drifts restored, in the MAIN ("space") and the DARK ("dark") theme.

A-6  V1 removed "Genel" from the UI: a plain chat shows no scope label in the sidebar
     search results or the top bar bell; a project chat shows its project.
A-7  Q1: selecting text offers ONE floating "Alıntıla" button; the message actions keep
     the whole-message "Alıntıla" and carry no second "Quote selection".
D-8  K3, one date formatter: a plain sidebar row reads like a tree row (today its time,
     else its day: "yesterday" for both), and the usage popover's later reset is the
     same formatter's day and time ("tomorrow 12:00").

Screenshots: set JARVIS_SHOTS_DIR to save each themed state as a PNG.
Temp stores only: no test opens the live DB or makes a live model, auth or network call.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path

from PySide6.QtCore import QMetaObject, QPointF, Q_ARG, Qt
from PySide6.QtTest import QTest

from jarvis.ui.wording import human_date
from tests.helpers import wait_until
from tests import test_g6f8_search as f8          # the module: its tests are not collected here
from tests.test_g6r3u_unread_bell import BellCase
from tests.test_g6u1_usage import UsageCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

THEMES = ("space", "dark")
GONE = ("Global", "Genel")


def shot(root, name: str) -> None:
    folder = os.environ.get("JARVIS_SHOTS_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        root.grabWindow().save(str(Path(folder) / f"{name}.png"))


class PolishCase(BellCase):
    seed = f8.SidebarSearchTests.seed
    entry = f8.SidebarSearchTests.entry
    type = f8.SidebarSearchTests.type

    def theme(self, bridge, root, name: str) -> None:
        bridge.setTheme(name)
        pump(150)
        self.assertEqual(name, root.property("themeName"))

    def texts_under(self, item) -> list:
        return [t.property("text") for t in self.descendants(item)
                if t.isVisible() and isinstance(t.property("text"), str)]


class ScopeLabelTests(PolishCase):
    def test_a6_search_results_never_say_genel_in_either_theme(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        pump(100)
        self.tap(root, self.entry(root), until=lambda: bool(self.shown(root, "sidebarSearchField")))
        self.type(root, "ula")
        wait_until(lambda: bool(self.shown(root, "sidebarSearchTitleRow")), 2000)
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.theme(bridge, root, theme)
                titles, contents = self.shown(root, "sidebarSearchTitleRow"), self.shown(root, "sidebarSearchContentRow")
                self.assertEqual([("Sulama planı", "")], [(r.property("name"), r.property("scope")) for r in titles])
                self.assertEqual([("Notlar", "Bahçe")], [(r.property("name"), r.property("scope")) for r in contents])
                results = self.texts_under(self.shown(root, "sidebarSearchResults")[0])
                self.assertEqual([], [t for t in results if t in GONE], results)
                shot(root, f"a6_search_{theme}")

    def test_a6_the_bell_never_says_genel_in_either_theme(self) -> None:
        bridge, root = self.open_dots()
        self.assertTrue(bridge.openConversation(self.plain[1].id))
        for cid in (self.older.id, self.tezc[1].id):
            self.assertTrue(bridge.markConversationUnread(cid))
        bridge.refreshConfig()
        pump(100)
        menu = self.open_bell(root)
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.theme(bridge, root, theme)
                self.assertEqual({("Tez 1", "Tez"), ("Düz eski", "")}, {e[:2] for e in self.entries(root)})
                shown = self.texts_under(menu.property("contentItem"))
                self.assertIn("Düz eski", shown)
                self.assertEqual([], [t for t in shown if t in GONE], shown)
                shot(root, f"a6_bell_{theme}")


class QuoteSelectionTests(PolishCase):
    def test_a7_one_floating_button_and_the_whole_message_action_stays(self) -> None:
        self.data()
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._say("user", "soru")
        bridge._say("jarvis", "İlk paragraf: şöyle.\nİkinci satır burada.", chat=True)
        pump(300)
        everything = self.descendants(root.contentItem())
        self.assertEqual([], [i for i in everything if i.objectName().startswith("messageQuoteSelection")])
        whole = [i for i in everything if i.objectName().startswith("messageQuote_")]
        self.assertEqual(2, len(whole))                              # the person's turn and the chat reply
        self.assertEqual({"Quote"}, {i.property("label") for i in whole})
        tip = self.item(root, "quoteTip")
        reply = [i for i in everything if i.objectName() == "feedMessage_jarvis"][0]
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.theme(bridge, root, theme)
                QMetaObject.invokeMethod(reply, "select", Q_ARG(int, 14), Q_ARG(int, 20))
                wait_until(lambda: bool(tip.property("shown")), 1000)
                self.assertTrue(tip.property("shown"))
                floating = [i for i in self.descendants(root.contentItem()) if i.objectName() == "quoteTipButton"]
                self.assertEqual(1, len(floating))                   # ONE button at the selection
                shot(root, f"a7_quote_{theme}")
                QMetaObject.invokeMethod(reply, "deselect")
                wait_until(lambda: not tip.property("shown"), 1000)
                self.assertFalse(tip.property("shown"))
        # The floating button quotes the selection; the hover action quotes the whole message.
        QMetaObject.invokeMethod(reply, "select", Q_ARG(int, 14), Q_ARG(int, 20))
        wait_until(lambda: bool(tip.property("shown")), 1000)
        button = floating[0]
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier,
                         button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint())
        wait_until(lambda: len(bridge.contextGroups) == 1, 1000)
        self.assertEqual(["partial"], [g["reference"]["selection"] for g in bridge.contextGroups])
        QMetaObject.invokeMethod([i for i in whole if i.objectName() == "messageQuote_1"][0], "activated")
        wait_until(lambda: len(bridge.contextGroups) == 2, 1000)
        self.assertEqual(2, len(bridge.contextGroups))


class OneFormatterTests(PolishCase):
    def test_d8_a_plain_row_and_a_tree_row_follow_one_rule_in_either_theme(self) -> None:
        self.data()
        self.stamp(self.tezc[0].id, self.now - timedelta(days=1))   # a tree row from yesterday, like "Düz dün"
        bridge, root = self.open_window()
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.theme(bridge, root, theme)
                meta = {(n, t): self.row(root, n, t).property("meta")
                        for n, t in (("sidebarConversation", "Düz dün"), ("sidebarProjectConversation", "Tez 0"),
                                     ("sidebarConversation", "Düz 0"), ("sidebarProjectConversation", "Tez 1"))}
                yesterday = human_date(self.now - timedelta(days=1), self.now, "en_US", "short")
                self.assertEqual("yesterday", yesterday)
                self.assertEqual(yesterday, meta[("sidebarConversation", "Düz dün")])           # was its HH:mm
                self.assertEqual(yesterday, meta[("sidebarProjectConversation", "Tez 0")])
                today = meta[("sidebarConversation", "Düz 0")]
                self.assertRegex(today, r"^\d\d:\d\d$")                                     # today: its time
                self.assertRegex(meta[("sidebarProjectConversation", "Tez 1")], r"^\d\d:\d\d$")
                shot(root, f"d8_sidebar_{theme}")
        source = (Path(__file__).resolve().parents[1] / "jarvis/ui/qml/components/Sidebar.qml").read_text(encoding="utf-8")
        self.assertNotIn('"HH:mm"', source)
        self.assertEqual(2, len(re.findall(r'i18n\.when\([^)]*, (?:sidebar\.)?today, "short"\)', source)))   # both rows


class PopoverResetTests(UsageCase):
    def test_d8_a_later_reset_is_the_one_formatters_day_and_time(self) -> None:
        from datetime import datetime
        now = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        tomorrow = int((now + timedelta(days=1, hours=2)).timestamp())
        self.ring.setProperty("rows", [{"window": "weekly", "remaining": 41, "resets_at": tomorrow}])
        self.open_popover()                                  # opening reads the real clock (onAboutToShow)
        self.pop.setProperty("now", now.timestamp() * 1000)  # so the clock is pinned after it
        pump(10)
        self.assertEqual("Resets tomorrow 12:00", self.text_in("usageRow_weekly", "usageRowReset").property("text"))
        self.loc.apply("tr-TR")
        self.addCleanup(lambda: self.loc.apply("en-US"))
        self.pop.setProperty("now", now.timestamp() * 1000 + 1)
        pump(10)
        self.assertEqual("Sıfırlanma: yarın 12:00", self.text_in("usageRow_weekly", "usageRowReset").property("text"))
        source = (Path(__file__).resolve().parents[1] / "jarvis/ui/qml/components/UsagePopover.qml").read_text(encoding="utf-8")
        self.assertNotIn('"ddd HH:mm"', source)


if __name__ == "__main__":
    import unittest
    unittest.main()
