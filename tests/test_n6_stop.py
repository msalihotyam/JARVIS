"""N6STOP1: the kit section 5 stop rule evaluator (pure; no clock, file or network)."""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone

from jarvis import n6_stop
from jarvis.n6_kpi import KpiRow
from jarvis.n6_stop import CONTINUE, PAUSE, STOP, Event, StopInputError, Verdict, evaluate

AT = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def row(slice_id: str = "S1", source: str = "n6", *, passed: bool = True, defects: int = 0,
        security: int = 0) -> KpiRow:
    return KpiRow(slice_id, source, passed, defects, 0, 1000, 60.0, security, AT)


def run(rows: object = (), events: object = (), usage: object = 50.0,
        share: object = 5.0) -> Verdict:
    return evaluate(rows=rows, events=events, usage_pct=usage, n6_share_pct=share)  # type: ignore[arg-type]


def results(*spec: str) -> list[Event]:
    """"n6:blocked conveyor:pass ..." -> result events, oldest first."""
    return [Event(kind, source) for source, kind in (s.split(":") for s in spec)]


class StopTests(unittest.TestCase):
    def test_security_incident_stops(self) -> None:
        self.assertEqual(run(rows=[row(security=1)]), Verdict(STOP, n6_stop.SECURITY_INCIDENT))
        self.assertEqual(run(events=[Event("security_incident", "n6")]).reason, n6_stop.SECURITY_INCIDENT)

    def test_false_pass_stops(self) -> None:
        self.assertEqual(run(rows=[row(defects=1)]), Verdict(STOP, n6_stop.FALSE_PASS))
        self.assertEqual(run(events=[Event("false_pass", "n6")]), Verdict(STOP, n6_stop.FALSE_PASS))

    def test_two_manager_stops(self) -> None:
        self.assertEqual(run(events=[Event("two_manager", "n6")]), Verdict(STOP, n6_stop.TWO_MANAGER))

    def test_acceptance_hash_mismatch_stops(self) -> None:
        self.assertEqual(run(events=[Event("acceptance_hash_mismatch", "conveyor")]),
                         Verdict(STOP, n6_stop.ACCEPTANCE_HASH_MISMATCH))

    def test_stop_outranks_pause(self) -> None:
        stop = run(events=[Event("two_manager", "n6"), *results("n6:blocked", "n6:blocked", "n6:blocked")],
                   usage=99.0, share=50.0)
        self.assertEqual(stop, Verdict(STOP, n6_stop.TWO_MANAGER))

    def test_stop_outranks_missing_input(self) -> None:
        self.assertEqual(run(rows=[row(security=1)], events=None, usage=None, share=None),
                         Verdict(STOP, n6_stop.SECURITY_INCIDENT))


class PauseTests(unittest.TestCase):
    def test_quota_at_line_or_inside_reserve_pauses(self) -> None:
        self.assertEqual(run(usage=94.9).decision, CONTINUE)
        for usage in (95, 95.0, 97.5, 100):
            self.assertEqual(run(usage=usage), Verdict(PAUSE, n6_stop.QUOTA_LINE), usage)

    def test_three_consecutive_blocked_pauses(self) -> None:
        self.assertEqual(run(events=results("n6:blocked", "n6:blocked", "n6:blocked")),
                         Verdict(PAUSE, n6_stop.BLOCKED_RUN))

    def test_conveyor_result_does_not_reset_streak(self) -> None:
        self.assertEqual(run(events=results("n6:blocked", "conveyor:pass", "n6:blocked", "n6:blocked")),
                         Verdict(PAUSE, n6_stop.BLOCKED_RUN))

    def test_share_cap_pauses(self) -> None:
        self.assertEqual(run(share=14.9).decision, CONTINUE)
        self.assertEqual(run(share=15), Verdict(PAUSE, n6_stop.SHARE_CAP))

    def test_otherwise_continue(self) -> None:
        self.assertEqual(run(rows=[row(), row(source="conveyor", passed=False)],
                             events=results("n6:pass", "n6:blocked")), Verdict(CONTINUE, n6_stop.OK))
        self.assertEqual(run(rows=[], events=[]), Verdict(CONTINUE, n6_stop.OK))

    def test_thresholds_are_constants_not_arguments(self) -> None:
        self.assertEqual(set(inspect.signature(evaluate).parameters),
                         {"rows", "events", "usage_pct", "n6_share_pct"})
        self.assertEqual((n6_stop.QUOTA_LINE_PCT, n6_stop.RESERVE_PCT, n6_stop.BLOCKED_STREAK,
                          n6_stop.N6_SHARE_CAP_PCT), (95, 5, 3, 15))
        with self.assertRaises(TypeError):
            evaluate(rows=(), events=(), usage_pct=50.0, n6_share_pct=5.0,  # type: ignore[call-arg]
                     quota_line=100)


class NegativeTests(unittest.TestCase):
    def test_missing_input_pauses_never_continues(self) -> None:
        self.assertEqual(run(rows=None), Verdict(PAUSE, n6_stop.INPUT_MISSING))
        self.assertEqual(run(events=None), Verdict(PAUSE, n6_stop.INPUT_MISSING))
        self.assertEqual(run(share=None), Verdict(PAUSE, n6_stop.SHARE_UNKNOWN))

    def test_unknown_quota_pauses(self) -> None:
        self.assertEqual(run(usage=None), Verdict(PAUSE, n6_stop.QUOTA_UNKNOWN))

    def test_blocked_pass_blocked_continues(self) -> None:
        self.assertEqual(run(events=results("n6:blocked", "n6:blocked", "n6:pass", "n6:blocked")),
                         Verdict(CONTINUE, n6_stop.OK))
        # below the streak, and a later N6 PASS ends a streak (only the current run counts)
        self.assertEqual(run(events=results("n6:pass", "n6:blocked", "n6:blocked")), Verdict(CONTINUE, n6_stop.OK))
        self.assertEqual(run(events=results("n6:blocked", "n6:blocked", "n6:blocked", "n6:pass")),
                         Verdict(CONTINUE, n6_stop.OK))

    def test_conveyor_false_pass_does_not_stop_n6(self) -> None:
        self.assertEqual(run(rows=[row(source="conveyor", defects=2)], events=[Event("false_pass", "conveyor")]),
                         Verdict(CONTINUE, n6_stop.OK))

    def test_malformed_row_raises_no_verdict(self) -> None:
        good = row()
        forged = row()
        object.__setattr__(forged, "post_accept_defects", -1)
        bad_event = Event("pass", "n6")
        object.__setattr__(bad_event, "kind", "maybe")
        unhashable = Event("pass", "n6")
        object.__setattr__(unhashable, "kind", ["pass"])
        for rows, events in (([{"slice_id": "S1"}], []), ([good, ("S1", "n6")], []), ([forged], []),
                             ("S1", []), ([good], ["pass"]), ([good], [bad_event]), ([good], [unhashable]),
                             # malformed beats a STOP elsewhere: no verdict at all
                             ([row(security=1), {"x": 1}], []), ([], [Event("two_manager", "n6"), None])):
            with self.subTest(rows=rows, events=events), self.assertRaises(StopInputError):
                run(rows=rows, events=events)

    def test_malformed_reading_raises(self) -> None:
        for value in (True, "95", float("nan"), float("inf"), -1, 101):
            with self.subTest(value=value), self.assertRaises(StopInputError):
                run(usage=value)
            with self.subTest(share=value), self.assertRaises(StopInputError):
                run(share=value)

    def test_unknown_event_kind_or_source_refused(self) -> None:
        with self.assertRaises(StopInputError):
            Event("paused", "n6")
        with self.assertRaises(StopInputError):
            Event("blocked", "codex")


if __name__ == "__main__":
    unittest.main()
