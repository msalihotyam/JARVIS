"""N6-05 usage reader: fixtures are inline, no file, network, wall clock or data/jarvis.db."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from jarvis import n6_usage as u
from jarvis.work_controls import quota_verdict

WS = "2026-11-05T22:00:00+03:00"  # window 2026-11-05T19:00Z .. 2026-11-12T19:00Z
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc).timestamp()


def iso(offset_s: float) -> str:
    return datetime.fromtimestamp(NOW + offset_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ledger_row(pct: object, end: object, key: str = "end") -> str:
    return json.dumps({"slice": "X", "end": end, "seven_day": {key: pct}})


class UsageReaderTests(unittest.TestCase):
    # ---- negatives first
    def test_malformed_input_is_unknown_never_zero(self) -> None:
        for text in ("", "not json", "[]", "{", "null", '{"seven_pct": "12"}', '{"usage_pct": true}'):
            self.assertIsNone(u.parse_ledger(text, now=NOW), text)
            self.assertIsNone(u.parse_live(text, family="a", now=NOW), text)
            self.assertIsNone(u.parse_status(text, now=NOW), text)
        for bad in (None, True, "40", -1, 101, float("nan"), float("inf"), [40]):
            self.assertIsNone(u.parse_live(json.dumps({"seven_pct": bad, "updated_at": iso(-60)}),
                                           family="a", now=NOW), bad)
            self.assertIsNone(u.parse_ledger(ledger_row(bad, iso(-60), "end"), now=NOW), bad)
        self.assertIsNone(u.parse_live(json.dumps({"seven_pct": 5, "updated_at": "garbage"}), family="a", now=NOW))
        # unknown stays unknown all the way into the quota function: it pauses
        self.assertIsNone(u.resolve([None, None], now=NOW, window_start=WS).pct)
        self.assertIsNone(u.resolve([], now=NOW, window_start=WS).pct)

    def test_stale_or_future_reading_is_unknown(self) -> None:
        old = iso(-u.USAGE_FRESH_S - 1)
        self.assertIsNone(u.parse_ledger(ledger_row(10, old), now=NOW))
        self.assertIsNone(u.parse_live(json.dumps({"seven_pct": 10, "updated_at": old}), family="a", now=NOW))
        self.assertIsNone(u.parse_status(json.dumps({"usage_pct": 10, "updated_at": iso(-u.STATUS_FRESH_S - 1)}), now=NOW))
        self.assertIsNone(u.parse_live(json.dumps({"seven_pct": 10, "updated_at": iso(+3600)}), family="a", now=NOW))
        # a stale newest ledger row is not replaced by an older row
        self.assertIsNone(u.parse_ledger(ledger_row(10, iso(-86400 * 8)) + "\n" + ledger_row(5, iso(-86400 * 9)), now=NOW))

    def test_reading_from_before_the_window_is_not_current(self) -> None:
        before = u.Reading(80.0, NOW - 4 * 86400, "ledger")  # 2026-11-03, previous window
        self.assertIsNone(u.resolve([before], now=NOW, window_start=WS).pct)

    def test_unreadable_or_ended_window_is_unknown(self) -> None:
        r = u.Reading(10.0, NOW - 60, "ledger")
        for ws in (None, "", "garbage", 7):
            self.assertIsNone(u.resolve([r], now=NOW, window_start=ws).pct, ws)  # type: ignore[arg-type]
        self.assertIsNone(u.resolve([r], now=NOW + 6 * 86400, window_start=WS).pct)  # window over

    def test_reset_drop_inside_the_window_is_not_current(self) -> None:
        r = u.Reading(5.0, NOW - 60, "live:a")
        ok = (60.0, NOW - 3600, NOW - 3600, "live:a")
        self.assertIsNone(u.resolve([r], now=NOW, window_start=WS, last_ok=ok).pct)

    def test_unconfirmed_jump_up_is_held_back(self) -> None:
        r = u.Reading(90.0, NOW - 60, "live:a")
        ok = (10.0, NOW - 3600, NOW - 3600, "ledger")
        got = u.resolve([r], now=NOW, window_start=WS, last_ok=ok)
        self.assertEqual((got.pct, got.source), (10.0, "ledger"))
        # confirmed by another source, or by the same source a later poll
        other = u.Reading(88.0, NOW - 30, "live:b")
        self.assertEqual(u.resolve([r, other], now=NOW, window_start=WS, last_ok=ok).pct, 88.0)  # newest wins
        self.assertEqual(u.resolve([r], now=NOW, window_start=WS, last_ok=ok,
                                   spike=("live:a", r.ts - u.SPIKE_CONFIRM_S)).pct, 90.0)
        # a jump after the spike window is taken
        late = (10.0, NOW - 3600, NOW - u.SPIKE_WINDOW_S - 1, "ledger")
        self.assertEqual(u.resolve([r], now=NOW, window_start=WS, last_ok=late).pct, 90.0)

    def test_tests_never_open_the_live_db(self) -> None:
        import builtins
        real, seen = builtins.open, []

        def spy(file, *a, **k):  # type: ignore[no-untyped-def]
            seen.append(str(file))
            return real(file, *a, **k)

        builtins.open = spy  # type: ignore[assignment]
        try:
            u.resolve([u.parse_live(json.dumps({"seven_pct": 3, "updated_at": iso(-5)}), family="a", now=NOW)],
                      now=NOW, window_start=WS)
        finally:
            builtins.open = real
        self.assertFalse([p for p in seen if "jarvis.db" in p], seen)

    # ---- the three formats
    def test_ledger_row_parses_end_then_start_newest_wins(self) -> None:
        text = ledger_row(10, iso(-7200)) + "\ngarbage\n" + ledger_row(33.5, iso(-60)) + "\n"
        r = u.parse_ledger(text, now=NOW)
        assert r is not None
        self.assertEqual((r.pct, r.source, r.ts), (33.5, "ledger", NOW - 60))
        row = json.dumps({"end": iso(-60), "seven_day": {"start": 21, "end": None}})
        self.assertEqual(getattr(u.parse_ledger(row, now=NOW), "pct", None), 21.0)

    def test_live_lane_file_parses(self) -> None:
        r = u.parse_live(json.dumps({"seven_pct": 42, "updated_at": iso(-120), "other": 1}), family="claude", now=NOW)
        assert r is not None
        self.assertEqual((r.pct, r.source, r.ts), (42.0, "live:claude", NOW - 120))

    def test_status_json_parses_with_window(self) -> None:
        doc = {"usage_pct": 55, "updated_at": iso(-30), "pace": {"window_start": WS, "mode": "x"}}
        r = u.parse_status(json.dumps(doc), now=NOW)
        assert r is not None
        self.assertEqual((r.pct, r.source, r.window_start), (55.0, "status", WS))

    def test_resolve_feeds_the_quota_function(self) -> None:
        led = u.parse_ledger(ledger_row(30, iso(-3600)), now=NOW)
        live = u.parse_live(json.dumps({"seven_pct": 35, "updated_at": iso(-60)}), family="a", now=NOW)
        got = u.resolve([led, live], now=NOW, window_start=WS)
        self.assertEqual((got.pct, got.window_start, got.source), (35.0, WS, "live:a"))
        self.assertEqual(quota_verdict(now=NOW, usage_pct=got.pct, exhausted=False, keep_pct=20,
                                       keep_window=WS, window_start=WS), "continue")
        # nothing fresh: unknown pauses, never continues
        none = u.resolve([None], now=NOW, window_start=WS)
        self.assertEqual(quota_verdict(now=NOW, usage_pct=none.pct, exhausted=False, keep_pct=20,
                                       keep_window=WS, window_start=WS), "pause")


if __name__ == "__main__":
    unittest.main()
