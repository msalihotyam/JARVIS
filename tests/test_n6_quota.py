"""N6-04: the pure quota window. Every clock is an injected epoch, never the wall clock."""

import unittest
from datetime import datetime, timedelta, timezone

from jarvis.work_controls import ISTANBUL, quota_verdict, quota_window_end

THU = datetime(2026, 11, 5, 22, 0, tzinfo=ISTANBUL)  # a Thursday, the reset instant
WS = "2026-11-05T22:00:00+03:00"
NEXT_WS = "2026-11-12T22:00:00+03:00"
END = THU.timestamp() + 7 * 86400


def verdict(usage, *, now=THU.timestamp() + 3600, exhausted=False, keep=None,
            keep_window=WS, window_start=WS):
    return quota_verdict(now=now, usage_pct=usage, exhausted=exhausted, keep_pct=keep,
                         keep_window=keep_window, window_start=window_start)


class QuotaWindowTests(unittest.TestCase):
    # ---- negatives first ------------------------------------------------
    def test_unknown_usage_pauses(self):
        for bad in (None, float("nan"), float("inf"), True, False, "50", -1, [], {}):
            for keep in (None, 0, 5):
                with self.subTest(usage=bad, keep=keep):
                    self.assertEqual(verdict(bad, keep=keep), "pause")

    def test_budget_continues_only_on_explicit_false(self):
        for bad in (True, None, 0, 1, "", "False", "no", [], object()):
            with self.subTest(exhausted=bad):
                self.assertEqual(verdict(0, exhausted=bad), "pause")
        self.assertEqual(verdict(0, exhausted=False), "continue")

    def test_keep_from_the_previous_week_is_ignored(self):
        # keep written for last week's window: the full week is free again
        self.assertEqual(verdict(99, keep=1, keep_window=WS, window_start=NEXT_WS,
                                 now=END + 60), "continue")
        self.assertEqual(verdict(99, keep=1, keep_window=None), "continue")
        self.assertEqual(verdict(99, keep=1, keep_window="2026-10-29T22:00:00+03:00"),
                         "continue")
        # the same keep is honoured inside its own window
        self.assertEqual(verdict(99, keep=1), "pause")

    def test_keep_expires_at_the_thursday_boundary(self):
        # same window_start string, but the clock crossed Thursday 22:00
        self.assertEqual(verdict(99, keep=1, now=END - 1), "pause")
        self.assertEqual(verdict(99, keep=1, now=END), "continue")
        self.assertEqual(verdict(99, keep=1, now=END + 3600), "continue")

    def test_invalid_inputs_are_refused(self):
        for keep in (True, -1, 31, "1", 1.0):
            with self.subTest(keep=keep), self.assertRaises(ValueError):
                verdict(10, keep=keep)
        for ws in (None, "", "not a date", 5):
            with self.subTest(ws=ws), self.assertRaises(ValueError):
                verdict(10, window_start=ws)
        for now in (-1, float("nan"), True, "1"):
            with self.subTest(now=now), self.assertRaises(ValueError):
                verdict(10, now=now)

    # ---- the keep share -------------------------------------------------
    def test_keep_one_stops_at_99(self):
        self.assertEqual(verdict(98, keep=1), "continue")
        self.assertEqual(verdict(98.9, keep=1), "continue")
        self.assertEqual(verdict(99, keep=1), "pause")
        self.assertEqual(verdict(100, keep=1), "pause")

    def test_keep_zero_stops_at_100(self):
        self.assertEqual(verdict(99, keep=0), "continue")
        self.assertEqual(verdict(100, keep=0), "pause")

    def test_no_keep_is_a_free_week(self):
        self.assertEqual(verdict(0), "continue")
        self.assertEqual(verdict(99.5), "continue")
        self.assertEqual(verdict(100), "pause")
        self.assertEqual(verdict(150), "pause")

    def test_bigger_keep_lowers_the_line(self):
        self.assertEqual(verdict(69, keep=30), "continue")
        self.assertEqual(verdict(70, keep=30), "pause")

    # ---- the Thursday 22:00 Istanbul clock -------------------------------
    def test_window_end_is_the_next_thursday_2200(self):
        # Friday 00:07:42 (the pace.json shape, naive) -> Thursday 22:00 six days later
        self.assertEqual(quota_window_end("2026-11-06T00:07:42"),
                         datetime(2026, 11, 12, 22, 0, tzinfo=ISTANBUL).timestamp())
        self.assertEqual(quota_window_end(WS), END)  # exactly at the reset: a full week
        self.assertEqual(quota_window_end("2026-11-05T21:59:59+03:00"), THU.timestamp())

    def test_window_end_reads_other_offsets_as_istanbul_time(self):
        # 19:00 UTC is 22:00 Istanbul
        utc = datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc).isoformat()
        self.assertEqual(quota_window_end(utc), END)
        # 18:59:59 UTC is still before the reset
        self.assertEqual(quota_window_end("2026-11-05T18:59:59+00:00"), THU.timestamp())
        # Friday 00:30 Istanbul written in UTC (Thursday 21:30 UTC)
        self.assertEqual(quota_window_end("2026-11-05T21:30:00+00:00"), END)

    def test_no_dst_surprise_across_european_clock_changes(self):
        # Europe fell back on 2026-10-25 and Istanbul did not move: still 7 * 24 h
        start = datetime(2026, 10, 22, 22, 0, tzinfo=ISTANBUL)
        self.assertEqual(quota_window_end(start.isoformat()), start.timestamp() + 7 * 86400)
        # spring forward 2026-03-29 (Sunday) inside the week of Thursday 2026-03-26
        start = datetime(2026, 3, 26, 22, 0, tzinfo=ISTANBUL)
        self.assertEqual(quota_window_end(start.isoformat()), start.timestamp() + 7 * 86400)
        # the 22:00 wall time stays 22:00 +03:00 whatever the machine's own zone is
        end = datetime.fromtimestamp(quota_window_end(WS), ISTANBUL)
        self.assertEqual((end.weekday(), end.hour, end.minute), (3, 22, 0))
        self.assertEqual(end - timedelta(days=7), THU)

    def test_clock_across_the_boundary(self):
        # keep 1 at 99 %: held until the last second of the window, free from the reset
        self.assertEqual(verdict(99, keep=1, now=END - 0.001), "pause")
        self.assertEqual(verdict(99, keep=1, now=END), "continue")
        # the fresh window with its own (absent) keep is a free week
        self.assertEqual(verdict(50, now=END + 1, window_start=NEXT_WS, keep_window=None),
                         "continue")


if __name__ == "__main__":
    unittest.main()
