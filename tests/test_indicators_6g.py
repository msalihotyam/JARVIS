"""Gate 6G K3: honest indicators - they claim only what is true.

* The feed's "live" badge shows only while an approved execution runs or its
  newest event is under five minutes old; never as decoration.
* The workflow's "N/4" stage counter is hidden while there is no task.
* Core codes become words; an unknown code is named as such and kept exact,
  and the exact codes stay in the Status row's tip.
* Dates go through ONE formatter (``jarvis.ui.wording.human_date``, QML
  ``i18n.when``): today / yesterday / tomorrow, else "3 Oct" ("3 Eki"), decided
  by the caller's clock in its timezone - so midnight is checked with a fixed one.
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_ui import _APP, PresentationTests, UiTestCase, settled  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import Q_ARG, Q_RETURN_ARG, QMetaObject  # noqa: E402
from PySide6.QtQml import QQmlProperty  # noqa: E402

from jarvis.contracts import LoopStage, ProjectMemoryKind, TaskStatus  # noqa: E402
from jarvis.contracts import JobKind  # noqa: E402
from jarvis.ui.i18n import localization, tx  # noqa: E402
from jarvis.ui.wording import CODE_WORDS, code_word, human_date  # noqa: E402

ISTANBUL = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ISTANBUL)            # a fixed clock


class TurkishCase(unittest.TestCase):
    def turkish(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))


class HumanDateTests(TurkishCase):
    def test_the_day_word_follows_the_local_calendar_across_midnight(self) -> None:
        for value, word in (("2026-10-09T00:00:00+03:00", "today"), ("2026-10-09T23:59:59+03:00", "today"),
                            ("2026-10-08T23:59:59+03:00", "yesterday"), ("2026-10-08T00:00:00+03:00", "yesterday"),
                            ("2026-10-10T00:00:00+03:00", "tomorrow"), ("2026-10-07T23:59:59+03:00", "7 Oct"),
                            ("2026-10-11T00:00:00+03:00", "11 Oct")):
            with self.subTest(value=value):
                self.assertEqual(word, human_date(value, NOW))
        # The clock just after midnight: an hour ago is yesterday.
        self.assertEqual("yesterday", human_date("2026-10-08T23:30:00+03:00", NOW.replace(hour=0, minute=30)))

    def test_the_moment_is_read_in_the_clock_s_timezone(self) -> None:
        # 21:30 UTC on the 8th is 00:30 on the 9th in Istanbul: today there, not yesterday.
        self.assertEqual("today", human_date("2026-10-08T21:30:00+00:00", NOW))
        self.assertEqual("today", human_date("2026-10-08T21:30:00Z", NOW))
        self.assertEqual("yesterday", human_date("2026-10-08T20:59:59Z", NOW))
        self.assertEqual("today 00:30", human_date("2026-10-08T21:30:00Z", NOW, style="time"))
        # The same moment read by a UTC clock is the 8th.
        utc_now = NOW.astimezone(timezone.utc)
        self.assertEqual("yesterday", human_date("2026-10-08T21:30:00Z", utc_now))
        # Epoch seconds and a naive (already local) text mean the same moment.
        moment = datetime(2026, 10, 8, 21, 30, tzinfo=timezone.utc)
        self.assertEqual("today 00:30", human_date(moment.timestamp(), NOW, style="time"))
        self.assertEqual("today 00:30", human_date("2026-10-09T00:30:00", NOW, style="time"))

    def test_styles_and_years(self) -> None:
        self.assertEqual("today 14:05", human_date("2026-10-09T14:05:00+03:00", NOW, style="time"))
        self.assertEqual("14:05", human_date("2026-10-09T14:05:00+03:00", NOW, style="short"))
        self.assertEqual("yesterday", human_date("2026-10-08T14:05:00+03:00", NOW, style="short"))
        self.assertEqual("3 Oct 14:05", human_date("2026-10-03T14:05:00+03:00", NOW, style="time"))
        self.assertEqual("3 Oct 2025", human_date("2025-10-03T14:05:00+03:00", NOW))
        self.assertEqual("3 Jan 2027", human_date("2027-01-03T14:05:00+03:00", NOW))

    def test_an_unreadable_value_is_empty_never_invented(self) -> None:
        for value in (None, "", "   ", "not a date", "2026-13-40", True, 0, -5, float("inf")):
            with self.subTest(value=value):
                self.assertEqual("", human_date(value, NOW))

    def test_turkish(self) -> None:
        self.turkish()
        self.assertEqual("bugün 14:05", human_date("2026-10-09T14:05:00+03:00", NOW, "tr_TR", "time"))
        self.assertEqual("dün", human_date("2026-10-08T14:05:00+03:00", NOW, "tr_TR"))
        self.assertEqual("yarın", human_date("2026-10-10T14:05:00+03:00", NOW, "tr_TR"))
        self.assertEqual("3 Eki", human_date("2026-10-03T14:05:00+03:00", NOW, "tr_TR"))
        self.assertEqual("3 Eki 2025", human_date("2025-10-03T14:05:00+03:00", NOW, "tr_TR"))

    def test_the_qml_entry_point_uses_the_caller_s_clock(self) -> None:
        i18n = localization(_APP)
        now_ms = NOW.timestamp() * 1000
        local = datetime.fromtimestamp(NOW.timestamp()).astimezone()
        due = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        self.assertEqual("tomorrow 09:00", i18n.when(due.isoformat(), now_ms, "time"))
        self.assertEqual("", i18n.when("", now_ms, "day"))


class CodeWordTests(TurkishCase):
    def test_every_code_of_the_vocabularies_has_a_word(self) -> None:
        for kind, enum in (("stage", LoopStage), ("job_kind", JobKind), ("memory_kind", ProjectMemoryKind)):
            self.assertEqual({member.value for member in enum}, set(CODE_WORDS[kind]), kind)
            for member in enum:
                word = code_word(kind, member.value)
                self.assertNotIn(member.value, word, (kind, member))
                self.assertFalse(word.startswith("Unrecognized"), (kind, member))
        self.assertEqual("Recording", code_word("stage", "persist_decision_reason_next_step"))

    def test_an_unknown_code_is_named_and_kept_exact(self) -> None:
        self.assertEqual("Unrecognized (warp_drive)", code_word("stage", "warp_drive"))
        self.assertEqual("Unrecognized (plan)", code_word("no_such_vocabulary", "plan"))
        self.assertEqual("", code_word("stage", ""))
        self.turkish()
        self.assertEqual("Tanınmayan (warp_drive)", code_word("stage", "warp_drive"))
        self.assertEqual("Planlama", code_word("stage", "plan"))
        self.assertEqual(("dün", "bugün", "yarın"), tuple(tx("Wording", w) for w in ("yesterday", "today", "tomorrow")))


class WindowIndicatorTests(UiTestCase):
    item = PresentationTests.item
    window = PresentationTests.window

    def all_items(self, root):
        found, stack = [], [root.contentItem()]
        while stack:
            node = stack.pop()
            found.append(node)
            stack.extend(node.childItems())
        return found

    def feed_badge(self, root) -> str:
        panels = [i for i in self.all_items(root) if i.property("title") == "Live intelligence feed" and i.property("badge") is not None]
        self.assertEqual(1, len(panels))
        return panels[0].property("badge")

    def call(self, obj, method, *args):
        result = QMetaObject.invokeMethod(obj, method, Q_RETURN_ARG("QVariant"), *[Q_ARG("QVariant", a) for a in args])
        return result

    def test_the_live_badge_needs_a_running_execution_or_a_recent_event(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        column = self.item(root, "panelColumn")
        now = time.time()
        self.assertTrue(QQmlProperty(column, "nowSec").write(now))          # a fixed clock for the window
        # The start-up note is a real event of a moment ago: live.
        settled(200, lambda: self.assertEqual("live", self.feed_badge(root)))
        # Five minutes later with nothing new: no badge.
        for entry in bridge._activity:
            entry["ts"] = now - 301
        bridge.dataChanged.emit()
        settled(200, lambda: self.assertEqual("", self.feed_badge(root)))
        # Busy understanding a request is not an execution: still none.
        bridge._busy, bridge._executing = True, False
        bridge.stateChanged.emit()
        settled(200, lambda: self.assertEqual("", self.feed_badge(root)))
        # An approved execution running: live.
        bridge._executing = True
        bridge.stateChanged.emit()
        settled(200, lambda: self.assertEqual("live", self.feed_badge(root)))
        bridge._busy = False
        bridge.stateChanged.emit()
        settled(200, lambda: self.assertEqual("", self.feed_badge(root)))
        # A new event under five minutes old: live again.
        bridge._activity[0]["ts"] = now - 299
        bridge.dataChanged.emit()
        settled(200, lambda: self.assertEqual("live", self.feed_badge(root)))

    def test_the_stage_counter_hides_without_a_task_and_codes_read_as_words(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        status, tip = self.item(root, "workflowValue_Status"), self.item(root, "workflowTip_Status")
        # Every panel slot carries the header row; only the workflow panel's is shown.
        counters = [i for i in self.all_items(root) if i.objectName() == "workflowCounter"]
        def shown():
            return [c.property("text") for c in counters if c.isVisible()]
        self.assertEqual([], shown())
        self.assertEqual(("No active task", ""), (status.property("text"), tip.property("text")))
        bridge._task = {"task_id": "t-1", "status": "running", "stage": "persist_decision_reason_next_step"}
        bridge.stateChanged.emit()
        settled(200, lambda: (
            self.assertEqual(1, len(shown())),
            self.assertRegex(shown()[0], r"^[1-4]/4$"),
            self.assertEqual("running / Recording", status.property("text")),
            self.assertEqual("running · persist_decision_reason_next_step", tip.property("text")),
        ))
        bridge._task = {"task_id": "t-1", "status": "teleporting", "stage": "warp"}
        bridge.stateChanged.emit()
        settled(200, lambda: self.assertEqual("unrecognized (teleporting) / Unrecognized (warp)", status.property("text")))
        bridge._task = {}
        bridge.stateChanged.emit()
        settled(200, lambda: self.assertEqual([], shown()))

    def test_every_task_status_has_a_word_on_the_column_and_the_rail(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        # In Turkish, where no word can equal its English code by accident.
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        for name, unknown in (("panelColumn", "tanınmayan (zz)"), ("summaryRail", "Tanınmayan (zz)")):
            owner = self.item(root, name)
            # Every task status, and the run status "cancelling" the Outcome row falls back to.
            for status in [s.value for s in TaskStatus] + ["cancelling"]:
                with self.subTest(owner=name, status=status):
                    word = self.call(owner, "statusWord", status)
                    self.assertTrue(word)
                    self.assertNotIn(status, word)
                    self.assertFalse(word.lower().startswith("tanınmayan"), word)
            self.assertEqual(unknown, self.call(owner, "statusWord", "zz"))
            self.assertEqual("", self.call(owner, "statusWord", ""))

    def test_the_next_run_is_a_human_date(self) -> None:
        bridge = self.bridge()
        bridge.setPanelVisible("automations", True)
        root = self.window(bridge)
        column = self.item(root, "panelColumn")
        now = datetime.now().astimezone()
        self.assertTrue(QQmlProperty(column, "nowSec").write(now.timestamp()))
        due = (now + timedelta(days=1)).replace(hour=9, minute=5, second=0, microsecond=0)
        bridge._jobs = [{"id": "j-1", "label": "Rapor", "readable": True, "enabled": True,
                         "next_due_at_utc": due.astimezone(timezone.utc).isoformat()}]
        bridge.dataChanged.emit()
        settled(200, lambda: self.assertIn("next tomorrow 09:05",
                                           [i.property("text") for i in self.all_items(root) if i.objectName() == "workflowValue_Rapor"]))


if __name__ == "__main__":
    unittest.main()
