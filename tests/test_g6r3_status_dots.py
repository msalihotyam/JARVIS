"""Gate 6G R3 - the sidebar's conversation status dots (build/6g/PLAN_DRAFT.txt D1 + D2,
mockup v16; amber from R2's needsYouIds).

D1  Every conversation row (SABİTLENENLER, the plain list, the project trees) starts with a
    6 px dot: idle unlit (a faint ring), RUNNING violet and blinking (the conversation the
    R1 run record works for), NEEDS YOU amber (R2: a parked approval / clarification waits
    there; amber wins over violet and green), FINISHED while another conversation was open
    green until that conversation is opened. The open one is never green.
D2  A green row also glows (green tint + slow pulse); dot and glow clear when it is opened.
K2  Ambient effects off: static colours, nothing blinks or pulses.
K3  The state is said in words (tip and accessible name): Çalışıyor / Sizi bekliyor / Bitti.

Real bridge state only; bridge memory only (a restart shows none). Runs are scripted Core /
model answers; nothing reaches a model, a network or the live DB.
"""

from __future__ import annotations

from jarvis.ui.bridge import Bridge
from jarvis.ui.preferences import UiPreferences
from tests.test_g6sb1_sidebar import SidebarV2Case
from tests.test_intent import proposal, task_proposal
from tests.test_motion_6g import QML_DIR
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

ANALYSE = "Bu projeyi incele."
CHAT = proposal(kind="chat", reply="Tamam")


class StatusDotCase(SidebarV2Case):
    def open_dots(self, *answers):
        self.data()
        bridge = self.bridge(*answers)
        root = self.window(bridge)
        self.fixed_today(root, self.now)
        pump(100)
        return bridge, root

    def status(self, root, name, title) -> str:
        """The row's dot state, read fresh (a list change rebuilds the rows)."""
        row = self.row(root, name, title)
        dot = self.child(row, "sidebarStatusDot")
        self.assertTrue(dot.isVisible())
        self.assertEqual(row.property("status"), dot.property("status"))
        return dot.property("status")

    def glowing(self, root, name, title) -> bool:
        return self.child(self.row(root, name, title), "sidebarRowGlow").isVisible()

    def run_away(self, bridge, root, name, title, target, away) -> list:
        """Open ``target``, send a message; inside the model call (the run in flight) read the
        row, open ``away``, read it again. Returns what was seen in flight."""
        seen = []

        def inside(_task) -> None:
            self.model.on_complete = None
            seen.append((list(bridge.runningIds), self.status(root, name, title)))
            self.assertTrue(bridge.openConversation(away))                 # R1: a recorded run lets it
            seen.append((list(bridge.runningIds), self.status(root, name, title), bridge.conversationId))
        self.assertTrue(bridge.openConversation(target))
        self.model.on_complete = inside
        self.assertTrue(bridge.submitText("Merhaba"))
        return seen


class DotTests(StatusDotCase):
    def test_a_running_then_finished_away_then_cleared_on_open_in_every_row_kind(self) -> None:
        bridge, root = self.open_dots(CHAT, CHAT, CHAT)
        self.assertTrue(bridge.pinConversation(self.yday.id))
        pump(100)
        away = self.plain[1]
        for name, title, target in (("sidebarConversation", "Düz 0", self.plain[0].id),
                                    ("sidebarPinnedConversation", "Düz dün", self.yday.id),
                                    ("sidebarProjectConversation", "Tez 0", self.tezc[0].id)):
            with self.subTest(row=name):
                self.assertEqual("", self.status(root, name, title))         # idle: unlit
                seen = self.run_away(bridge, root, name, title, target, away.id)
                self.assertEqual([([target], "running"), ([target], "running", away.id)], seen)
                # Finished while Düz 1 was open: green + glow on its row; the open row is idle.
                self.assertEqual(([], [target]), (bridge.runningIds, bridge.finishedUnseenIds))
                self.assertEqual("finished", self.status(root, name, title))
                self.assertTrue(self.glowing(root, name, title))
                self.assertEqual("", self.status(root, "sidebarConversation", "Düz 1"))
                self.assertFalse(self.glowing(root, "sidebarConversation", "Düz 1"))
                # A mouse click opens it: dot and glow clear, and stay cleared after leaving it.
                self.tap(root, self.row(root, name, title), until=lambda: bridge.conversationId == target)
                self.assertEqual(target, bridge.conversationId)
                self.assertEqual([], bridge.finishedUnseenIds)
                self.assertEqual("", self.status(root, name, title))
                self.assertFalse(self.glowing(root, name, title))
                self.assertTrue(bridge.openConversation(away.id))
                self.assertEqual(("", []), (self.status(root, name, title), bridge.finishedUnseenIds))

    def test_b_a_run_finished_on_the_open_conversation_is_never_green(self) -> None:
        bridge, root = self.open_dots(CHAT)
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        self.assertTrue(bridge.submitText("Merhaba"))
        self.assertEqual(([], []), (bridge.runningIds, bridge.finishedUnseenIds))
        self.assertEqual("", self.status(root, "sidebarConversation", "Düz 0"))

    def test_c_a_parked_approval_is_amber_over_green_and_violet_over_nothing_else(self) -> None:
        bridge, root = self.open_dots(task_proposal("code_analysis", objective=ANALYSE))
        a, b = self.plain[0].id, self.plain[1].id
        seen = self.run_away(bridge, root, "sidebarConversation", "Düz 0", a, b)
        self.assertEqual("running", seen[-1][1])
        # The run ended awaiting approval while B was open: parked (R2), so amber - not green.
        self.assertEqual([a], bridge.needsYouIds)
        self.assertEqual([a], bridge.finishedUnseenIds)
        self.assertEqual("needsYou", self.status(root, "sidebarConversation", "Düz 0"))
        self.assertFalse(self.glowing(root, "sidebarConversation", "Düz 0"))
        dot = self.child(self.row(root, "sidebarConversation", "Düz 0"), "sidebarStatusDot")
        self.assertEqual("Waiting for you", dot.property("words"))
        # Opened: green is gone, the approval still waits there (amber stays until decided).
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual(([], [a]), (bridge.finishedUnseenIds, bridge.needsYouIds))
        self.assertEqual("needsYou", self.status(root, "sidebarConversation", "Düz 0"))
        bridge.deny()
        self.assertEqual([], bridge.needsYouIds)
        self.assertEqual("", self.status(root, "sidebarConversation", "Düz 0"))
        # The precedence is amber, then violet, then green (the bridge never reports amber for
        # the conversation a run is answering: R2 OD5).
        source = (QML_DIR / "components" / "Sidebar.qml").read_text(encoding="utf-8")
        order = [source.index(f'>= 0 ? "{state}"') for state in ("needsYou", "running", "finished")]
        self.assertEqual(sorted(order), order)

    def test_d_reduced_motion_keeps_the_colours_and_never_animates(self) -> None:
        bridge, root = self.open_dots(CHAT, CHAT)
        a, b = self.plain[0].id, self.plain[1].id
        moving = {}

        def inside(_task) -> None:
            self.model.on_complete = None
            for ambient in (False, True):
                bridge.setAmbientEffects(ambient)
                pump(30)
                dot = self.child(self.row(root, "sidebarConversation", "Düz 0"), "sidebarStatusDot")
                fill = self.child(dot, "statusDotFill")
                moving[("running", ambient)] = (dot.property("animated"), dot.property("tone").name())
                if not ambient:
                    self.assertEqual(1.0, fill.property("opacity"))
            self.assertTrue(bridge.openConversation(b))
        self.assertTrue(bridge.openConversation(a))
        self.model.on_complete = inside
        self.assertTrue(bridge.submitText("Merhaba"))
        for ambient in (False, True):
            bridge.setAmbientEffects(ambient)
            pump(30)
            row = self.row(root, "sidebarConversation", "Düz 0")
            dot = self.child(row, "sidebarStatusDot")
            moving[("finished", ambient)] = (dot.property("animated"), dot.property("tone").name())
            if not ambient:
                self.assertEqual(1.0, dot.property("glow"))
                self.assertEqual(1.0, self.child(row, "sidebarRowGlow").property("opacity"))
        bridge.setAmbientEffects(False)
        violet, green = "#9b7bff", "#00d8ad"
        self.assertEqual({("running", False): (False, violet), ("running", True): (True, violet),
                          ("finished", False): (False, green), ("finished", True): (True, green)}, moving)

    def test_e_the_state_in_words_and_its_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        from tests.test_ui import _APP
        bridge, root = self.open_dots(CHAT)
        self.run_away(bridge, root, "sidebarConversation", "Düz 0", self.plain[0].id, self.plain[1].id)
        dot = self.child(self.row(root, "sidebarConversation", "Düz 0"), "sidebarStatusDot")
        self.assertEqual("Finished", dot.property("words"))
        tip = [c for c in self.descendants(dot) if c.objectName() == "statusDotTip"]
        self.assertEqual(["Finished"], [t.property("text") for t in tip])
        idle = self.child(self.row(root, "sidebarConversation", "Düz 1"), "sidebarStatusDot")
        self.assertEqual("", idle.property("words"))
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual(["Çalışıyor", "Sizi bekliyor", "Bitti"],
                         [tx("StatusDot", s) for s in ("Running", "Waiting for you", "Finished")])

    def test_f_nothing_survives_a_restart(self) -> None:
        bridge, root = self.open_dots(CHAT)
        self.run_away(bridge, root, "sidebarConversation", "Düz 0", self.plain[0].id, self.plain[1].id)
        self.assertEqual([self.plain[0].id], bridge.finishedUnseenIds)
        # No window preference file carries it (THIZ keeps this test's preferences in memory: none written).
        saved = [p.name for p in self.prefs_path.parent.glob(self.prefs_path.stem + "*")
                 if p.is_file() and self.plain[0].id in p.read_text(encoding="utf-8")]
        self.assertEqual([], saved)
        again = Bridge(self.backend, UiPreferences(self.prefs_path), sync=True)
        again.refreshConfig()
        self.assertEqual(([], [], []), (again.runningIds, again.finishedUnseenIds, again.needsYouIds))
        self.assertTrue(again.openConversation(self.plain[1].id))
        self.assertEqual([], again.finishedUnseenIds)


if __name__ == "__main__":
    import unittest
    unittest.main()
