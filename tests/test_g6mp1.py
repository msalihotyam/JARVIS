"""Gate 6G G6MP1 ("ModelPanel A", Salih 2026-10-10): the unreachable Model
panel is retired and its information lives in the usage ring's popover, which
takes the shape of Claude's own plan-usage panel.

* Header "Plan usage limits · <plan>" - the plan only when the provider reported it.
* One row per REPORTED window: "Session limit" (5-hour), "Weekly · all models",
  "Weekly · <model>" (only when reported); the reset as "in 2 h 41 min" under a
  day, else the weekday and time; the used % and a bar filled to exactly that
  share, in the one accent colour. A window not reported is not drawn.
* "Details": route, model state, last-call model, report time, extra paid usage /
  credits, Web Research, last-call tokens - each only when reported.
* No ModelPanel left: no file, no Main / TopBar wiring, nothing in the window.

No test opens the live DB or makes a live model, auth or network call.
"""
from __future__ import annotations

import time
import unittest

from tests.test_g6u1_usage import UsageCase, descendants
from tests import test_ui
from tests.test_ui import UiTestCase, pump, visual_items

from PySide6.QtCore import QDateTime, QLocale, QObject  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402

from jarvis.ui.typography import FONT_DIR  # noqa: E402

QML = FONT_DIR.parent
ACCENT = "#20c7f6"


def weekday(epoch: int, locale: str) -> str:
    return QLocale(locale).toString(QDateTime.fromSecsSinceEpoch(epoch), "ddd HH:mm")


class PopoverCase(UsageCase):
    def setUp(self) -> None:
        super().setUp()
        now = int(time.time())
        self.five = now + 2 * 3600 + 30 * 60 + 30          # 2 h 30 min (and a margin for the open)
        self.soon = now + 45 * 60 + 30
        self.week = now + 3 * 86400
        self.rows = [{"window": "five_hour", "remaining": 80, "resets_at": self.five},
                     {"window": "weekly", "remaining": 52, "resets_at": self.week},
                     {"window": "weekly_opus", "remaining": 100, "resets_at": self.week, "model": "Opus"}]

    def show(self, limits) -> None:
        self.ring.setProperty("limits", limits)
        self.open_popover()

    def body(self):
        return self.pop.property("contentItem")

    def details_shown(self) -> dict:
        return {i.objectName(): i.property("value") for i in descendants(self.body())
                if i.objectName().startswith("usageDetail") and i.objectName() not in ("usageDetails", "usageDetailValue") and i.isVisible()}


class RowTests(PopoverCase):
    def test_a_row_per_reported_window_and_none_for_an_unreported_one(self) -> None:
        self.show({"rows": self.rows})
        self.assertEqual({"usageRow_five_hour", "usageRow_weekly", "usageRow_weekly_opus"}, self.rows_shown())
        self.assertEqual("Session limit", self.text_in("usageRow_five_hour", "usageRowTitle").property("text"))
        self.assertEqual("Weekly · all models", self.text_in("usageRow_weekly", "usageRowTitle").property("text"))
        self.assertEqual("Weekly · Opus", self.text_in("usageRow_weekly_opus", "usageRowTitle").property("text"))
        self.assertEqual(["20%", "48%", "0%"], [self.text_in(r, "usageRowUsed").property("text")
                                                for r in ("usageRow_five_hour", "usageRow_weekly", "usageRow_weekly_opus")])
        # No per-model week reported: no such row (never a 0).
        self.ring.setProperty("limits", {"rows": self.rows[:2]})
        pump(10)
        self.assertEqual({"usageRow_five_hour", "usageRow_weekly"}, self.rows_shown())
        # Only the week reported: the session row is not drawn.
        self.ring.setProperty("limits", {"rows": self.rows[1:2]})
        pump(10)
        self.assertEqual({"usageRow_weekly"}, self.rows_shown())
        # A row with no figure is not drawn either.
        self.ring.setProperty("limits", {"rows": [{"window": "five_hour", "resets_at": self.five}]})
        pump(10)
        self.assertEqual(set(), self.rows_shown())
        self.assertTrue(self.named("usageNone", self.body()).isVisible())

    def test_the_bar_is_exactly_the_used_share_in_the_one_accent(self) -> None:
        rows = [dict(self.rows[0], remaining=63), dict(self.rows[1], remaining=8), self.rows[2]]
        self.show({"rows": rows})
        for name, share in (("usageRow_five_hour", 0.37), ("usageRow_weekly", 0.92), ("usageRow_weekly_opus", 0.0)):
            with self.subTest(row=name):
                bar = self.text_in(name, "usageRowBar")
                self.assertAlmostEqual(share, bar.width() / bar.parentItem().width(), places=3)
                self.assertEqual(ACCENT, QColor(bar.property("color")).name())      # 92 % used is not amber or red

    def test_reset_reads_as_a_countdown_under_a_day_and_a_weekday_later(self) -> None:
        self.show({"rows": [self.rows[0], dict(self.rows[1], resets_at=self.soon), self.rows[2]]})
        self.assertEqual("Resets in 2 h 30 min", self.text_in("usageRow_five_hour", "usageRowReset").property("text"))
        self.assertEqual("Resets in 45 min", self.text_in("usageRow_weekly", "usageRowReset").property("text"))
        self.assertEqual("Resets " + weekday(self.week, "en_US"), self.text_in("usageRow_weekly_opus", "usageRowReset").property("text"))
        # No reset reported: no reset text.
        self.ring.setProperty("limits", {"rows": [dict(self.rows[0], resets_at=0)]})
        pump(10)
        self.assertFalse(self.text_in("usageRow_five_hour", "usageRowReset").isVisible())

    def test_tr_tr_reads_like_the_reference(self) -> None:
        self.loc.apply("tr-TR")
        self.show({"plan": "Max (5x)", "rows": self.rows})
        self.assertEqual("Plan kullanım sınırları · Max (5x)", self.named("usagePopoverTitle", self.body()).property("text"))
        self.assertEqual("Oturum sınırı", self.text_in("usageRow_five_hour", "usageRowTitle").property("text"))
        self.assertEqual("Haftalık · tüm modeller", self.text_in("usageRow_weekly", "usageRowTitle").property("text"))
        self.assertEqual("Haftalık · Opus", self.text_in("usageRow_weekly_opus", "usageRowTitle").property("text"))
        self.assertEqual("2 sa 30 dk sonra sıfırlanır", self.text_in("usageRow_five_hour", "usageRowReset").property("text"))
        self.assertEqual("Sıfırlanma: " + weekday(self.week, "tr_TR"), self.text_in("usageRow_weekly", "usageRowReset").property("text"))
        self.assertEqual("%20", self.text_in("usageRow_five_hour", "usageRowUsed").property("text"))
        # Turkish day abbreviations: a Thursday reads "Per".
        thursday = QDateTime.fromString("2026-10-15T22:00:00", "yyyy-MM-ddTHH:mm:ss")
        self.assertEqual("Per 22:00", QLocale("tr_TR").toString(thursday, "ddd HH:mm"))


class HeaderAndDetailTests(PopoverCase):
    def test_the_plan_is_named_only_when_reported(self) -> None:
        self.show({"rows": self.rows})
        title = self.named("usagePopoverTitle", self.body())
        self.assertEqual("Plan usage limits", title.property("text"))
        self.ring.setProperty("limits", {"plan": "plus", "rows": self.rows})
        pump(10)
        self.assertEqual("Plan usage limits · Plus", title.property("text"))

    def test_details_show_only_what_was_reported(self) -> None:
        self.show({"rows": self.rows})
        self.assertFalse(self.named("usageDetails", self.body()).isVisible())
        self.assertEqual({}, self.details_shown())
        seen = int(time.time()) - 60
        self.ring.setProperty("limits", {
            "provider": "claude", "status": "available", "research": True, "overage": "off", "seen_at": seen, "rows": self.rows,
            "last_call": {"reported": {"model": "claude-haiku-4-5-20251001", "input_tokens": 3, "output_tokens": 41}, "calls": 1}})
        pump(10)
        self.assertTrue(self.named("usageDetails", self.body()).isVisible())
        shown = self.details_shown()
        self.assertEqual({"usageDetailRoute", "usageDetailModel", "usageDetailSeen", "usageDetailOverage",
                          "usageDetailResearch", "usageDetailTokens"}, set(shown))
        self.assertEqual("Subscription route verified", shown["usageDetailRoute"])
        self.assertEqual("claude-haiku-4-5-20251001", shown["usageDetailModel"])
        self.assertEqual("Off", shown["usageDetailOverage"])
        self.assertEqual("Available", shown["usageDetailResearch"])
        self.assertEqual("3 input · 41 output", shown["usageDetailTokens"])     # unreported token kinds are left out
        self.assertTrue(shown["usageDetailSeen"])
        # Codex: credits and the chosen model's state; a failure names its reason.
        self.ring.setProperty("limits", {"provider": "codex", "status": "not_logged_in", "credits": False, "research": False,
                                         "verified": {"state": "failed", "reason": "no plan", "model": "gpt-5.6-sol"}, "rows": [],
                                         "last_call": {"reported": {"input_tokens": 1200, "cached_input_tokens": 5,
                                                                    "output_tokens": 7, "reasoning_output_tokens": 2}, "calls": 3}})
        pump(10)
        shown = self.details_shown()
        self.assertEqual({"usageDetailRoute", "usageDetailModelState", "usageDetailCredits", "usageDetailResearch", "usageDetailTokens"}, set(shown))
        self.assertEqual("Not signed in - run: codex login", shown["usageDetailRoute"])
        self.assertEqual("gpt-5.6-sol · Failed this session: no plan", shown["usageDetailModelState"])   # the canonical id stays visible
        self.assertEqual(("None", "Unavailable"), (shown["usageDetailCredits"], shown["usageDetailResearch"]))
        self.assertEqual("1,200 input · 5 cached · 7 output · 2 reasoning", shown["usageDetailTokens"])
        tokens = [i for i in descendants(self.body()) if i.objectName() == "usageDetailTokens"][0]
        self.assertEqual("Last turn tokens (3 calls)", tokens.property("label"))

    def test_details_tr_tr(self) -> None:
        self.loc.apply("tr-TR")
        self.show({"provider": "claude", "status": "available", "overage": "in_use", "research": True, "rows": self.rows})
        self.assertEqual("Ayrıntılar", [i for i in descendants(self.named("usageDetails", self.body()))
                                       if i.property("text") == "Ayrıntılar"][0].property("text"))
        shown = self.details_shown()
        self.assertEqual(("Kullanımda", "Kullanılabilir"), (shown["usageDetailOverage"], shown["usageDetailResearch"]))


class BridgeTests(UiTestCase):
    """bridge.usageLimits: every reported window, nothing estimated, no carry-over."""

    def test_claude_reports_every_window_and_only_its_own_details(self) -> None:
        bridge = self.bridge()
        self.fake_providers(bridge, [{"id": "claude", "vendor": "Anthropic", "name": "Claude", "status": "available",
                                      "research": True, "models": []}])
        self.assertEqual({"provider": "claude", "status": "available", "research": True, "rows": []}, bridge.usageLimits)
        bridge._claude_limits = {"seen_at": 10, "overage_status": "rejected", "using_overage": False, "windows": {
            "five_hour": {"utilization": 0.08, "resets_at": 1789560046},
            "seven_day": {"utilization": 0.26, "resets_at": 1789814410},
            "seven_day_opus": {"utilization": 0.61, "resets_at": 1789814410}}}
        bridge._chat_usage = {"provider": "openai", "reported": {"input_tokens": 9}}          # another provider's call
        limits = bridge.usageLimits
        self.assertEqual([("five_hour", 92, ""), ("weekly", 74, ""), ("weekly_opus", 39, "Opus")],
                         [(r["window"], r["remaining"], r.get("model", "")) for r in limits["rows"]])
        self.assertEqual(("off", 10), (limits["overage"], limits["seen_at"]))
        self.assertNotIn("last_call", limits)
        self.assertNotIn("plan", limits)                                   # Claude reports no plan name here
        bridge._chat_usage = {"provider": "anthropic", "call_count": 2, "reported": {"model": "claude-opus-5", "output_tokens": 4}}
        self.assertEqual({"reported": {"model": "claude-opus-5", "output_tokens": 4}, "calls": 2}, bridge.usageLimits["last_call"])
        bridge._claude_limits = {"seen_at": 11, "using_overage": True, "windows": {}}
        self.assertEqual(("in_use", []), (bridge.usageLimits["overage"], bridge.usageLimits["rows"]))

    def test_codex_reports_its_plan_credits_and_the_chosen_models_state(self) -> None:
        bridge = self.bridge()
        bridge.backend.codex_verified = {"gpt-5.6-luna": {"state": "usable"}}       # what a real call this session showed
        self.fake_providers(bridge, [{"id": "codex", "vendor": "OpenAI", "name": "Codex", "status": "available", "plan": "plus",
                                      "research": False, "quota": {"primary": {"used_percent": 4.0, "window_minutes": 300, "resets_at": 9},
                                                                   "has_credits": False, "checked_at": 5},
                                      "models": [{"id": "gpt-5.6-luna", "efforts": ["low"]}]}])
        bridge.setChatProvider("codex")
        bridge.setCodexModel("gpt-5.6-luna", "low")
        limits = bridge.usageLimits
        self.assertEqual(("codex", "plus", False, False, {"state": "usable", "model": "gpt-5.6-luna"}, 5),
                         (limits["provider"], limits["plan"], limits["credits"], limits["research"], limits["verified"], limits["seen_at"]))
        self.assertEqual([{"window": "five_hour", "remaining": 96, "resets_at": 9}], limits["rows"])
        self.assertNotIn("overage", limits)


class RetiredPanelTests(UiTestCase):
    window = test_ui.PresentationTests.window

    def test_no_model_panel_is_left_in_the_qml_tree(self) -> None:
        self.assertFalse((QML / "overlays" / "ModelPanel.qml").exists())
        main = (QML / "Main.qml").read_text(encoding="utf-8")
        for gone in ("ModelPanel", "toggleModel", "modelOpen", "onOpenModel", "modelPanel"):
            self.assertNotIn(gone, main)
        top = (QML / "components" / "TopBar.qml").read_text(encoding="utf-8")
        self.assertNotIn("signal openModel", top)
        root = self.window(self.bridge())
        names = {str(i.objectName()) for i in visual_items(root)}
        classes = {i.metaObject().className() for i in visual_items(root)}
        self.assertFalse(names & {"modelSurface", "modelPanelScroll", "providerField", "claudeModelField"})
        self.assertFalse([c for c in classes if "ModelPanel" in c])
        self.assertEqual(-1, root.metaObject().indexOfMethod("toggleModel()"))
        self.assertIsNone(root.findChild(QObject, "modelSurface"))
        self.assertEqual(0, self.execute_calls())


if __name__ == "__main__":
    unittest.main()
