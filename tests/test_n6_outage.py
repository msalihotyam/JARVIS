"""N6-16: outage hold and the stall action with a nudge cap (negative-first)."""

import builtins
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import n6_outage as o
from jarvis.work_controls import watchdog_verdict

T0 = 1_000_000.0
GAP = o.NUDGE_GAP_S


class N6OutageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "run" / "outage.json"

    def run_seq(self, steps: list[tuple[str, float]]) -> list[o.Decision]:
        return [o.step(self.path, v, t) for v, t in steps]

    # --- negative first ---------------------------------------------------
    def test_neg_outage_never_counts_failure_or_spends_nudges(self) -> None:
        got = self.run_seq([("outage", T0 + i * 600) for i in range(20)])
        self.assertTrue(all(d == o.Decision(o.HOLD) for d in got))
        self.assertFalse(any(d.counts_failure or d.attention for d in got))
        state = o.load(self.path)
        self.assertEqual(state, o.State(outage_since=T0))
        assert state is not None
        self.assertEqual(state.nudges, 0)

    def test_neg_outage_time_is_not_stall_time(self) -> None:
        self.run_seq([("timeout", T0), ("outage", T0 + 100), ("outage", T0 + 5000)])
        d = o.step(self.path, "timeout", T0 + 5000 + GAP)
        self.assertEqual(d.action, o.NUDGE)  # stall clock restarted: no 30-min drop
        self.assertFalse(d.counts_failure)

    def test_neg_third_nudge_refused_then_attention(self) -> None:
        got = self.run_seq([("timeout", T0), ("timeout", T0 + GAP), ("timeout", T0 + GAP + 1)])
        self.assertEqual([d.action for d in got], [o.NUDGE, o.NUDGE, o.WAIT])
        third = o.step(self.path, "timeout", T0 + 2 * GAP)
        self.assertEqual(third, o.Decision(o.DROP, attention=True, counts_failure=True))
        state = o.load(self.path)
        assert state is not None
        self.assertEqual(state.nudges, o.MAX_NUDGES)
        again = o.step(self.path, "timeout", T0 + 3 * GAP)
        self.assertEqual(again, o.Decision(o.DROP))  # attention and failure count once

    def test_neg_cap_reached_after_reset_still_refuses_nudge(self) -> None:
        self.run_seq([("timeout", T0), ("timeout", T0 + GAP), ("continue", T0 + GAP + 1)])
        d = o.step(self.path, "timeout", T0 + 3 * GAP)
        self.assertEqual(d.action, o.DROP)  # the nudge count stays per run

    def test_neg_nudge_sooner_than_gap_refused(self) -> None:
        self.assertEqual(o.step(self.path, "timeout", T0).action, o.NUDGE)
        for t in (T0 + 1, T0 + GAP - 0.001):
            self.assertEqual(o.step(self.path, "timeout", t), o.Decision(o.WAIT))
        state = o.load(self.path)
        assert state is not None
        self.assertEqual(state.nudges, 1)

    def test_neg_drop_after_thirty_minutes_of_stall(self) -> None:
        o.step(self.path, "timeout", T0)
        d = o.step(self.path, "timeout", T0 + o.DROP_AFTER_S)
        self.assertEqual(d, o.Decision(o.DROP, attention=True, counts_failure=True))

    def test_neg_unknown_verdict_holds_with_attention(self) -> None:
        o.step(self.path, "timeout", T0)
        before = self.path.read_bytes()
        for v in ("", "TIMEOUT", "outage ", "stuck", None, 1, True, ["timeout"]):
            self.assertEqual(o.step(self.path, v, T0 + GAP), o.Decision(o.HOLD, attention=True))
        self.assertEqual(self.path.read_bytes(), before)

    def test_neg_malformed_state_fails_closed_and_is_kept(self) -> None:
        bad = [b"", b"{", b"\xff\xfe", b"[]", b'{"version": 1}',
               b'{"version": 2, "nudges": 0, "last_nudge_at": null, "stall_since": null,'
               b' "outage_since": null, "dropped": false}',
               b'{"version": 1, "nudges": 3, "last_nudge_at": 5, "stall_since": null,'
               b' "outage_since": null, "dropped": false}',
               b'{"version": 1, "nudges": true, "last_nudge_at": 5, "stall_since": null,'
               b' "outage_since": null, "dropped": false}',
               b'{"version": 1, "nudges": 0, "last_nudge_at": null, "stall_since": NaN,'
               b' "outage_since": null, "dropped": false}',
               b'{"version": 1, "nudges": 0, "last_nudge_at": null, "stall_since": null,'
               b' "outage_since": null, "dropped": 0, "x": 1}']
        self.path.parent.mkdir(parents=True)
        for raw in bad:
            self.path.write_bytes(raw)
            for v in ("continue", "outage", "timeout"):
                self.assertEqual(o.step(self.path, v, T0), o.Decision(o.HOLD, attention=True),
                                 raw[:80])
            self.assertEqual(self.path.read_bytes(), raw)

    def assert_holds_and_kept(self, raw: bytes, msg: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(raw)
        self.assertIsNone(o.load(self.path), msg)
        for v in ("continue", "outage", "timeout"):
            self.assertEqual(o.step(self.path, v, T0 + 10), o.Decision(o.HOLD, attention=True), msg)
        self.assertEqual(self.path.read_bytes(), raw, msg)

    def test_neg_every_numeric_field_every_bad_shape_holds(self) -> None:
        good = {"version": 1, "nudges": 1, "last_nudge_at": T0, "stall_since": T0,
                "outage_since": None, "dropped": False}
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(o.load(self.path), o.State(nudges=1, last_nudge_at=T0, stall_since=T0))
        bad = ["true", "false", '"5"', '"1e3"', "[]", "{}", "[1]", "NaN", "Infinity", "-Infinity",
               "1e400", "-1", "-1e-9", "1" + "0" * 400, "-1" + "0" * 400, "1" + "0" * 5000,
               "1e12", "1e300", "1.7976931348623157e308"]
        extra = {"version": ["null", "1.0", "0", "2"], "nudges": ["null", "1.0", "3", "0.5"]}
        for field in ("version", "nudges", "last_nudge_at", "stall_since", "outage_since"):
            for token in bad + extra.get(field, []):
                raw = json.dumps({**good, field: "@BAD@"}).replace('"@BAD@"', token).encode()
                with self.subTest(field=field, token=token[:20]):
                    self.assert_holds_and_kept(raw, (field, token[:20]))

    def test_neg_deeply_nested_state_holds(self) -> None:
        self.assert_holds_and_kept(b"[" * 100_000, "nested")

    def test_neg_bad_now_shapes_hold(self) -> None:
        class BadFloat(int):
            def __float__(self) -> float:
                raise RuntimeError("no")

        class BadMath(float):  # converts fine, but its own compare/subtract raise
            def _no(self, *_: object) -> bool:
                raise RuntimeError("no")
            __lt__ = __le__ = __gt__ = __ge__ = __sub__ = __rsub__ = _no  # type: ignore[assignment]

        o.step(self.path, "timeout", T0)
        self.assertEqual(o.step(self.path, "timeout", BadMath(T0 + GAP)).action, o.NUDGE)
        before = self.path.read_bytes()
        for now in (True, False, "5", None, [T0], float("-inf"), -0.5, 10 ** 400, -(10 ** 400),
                    1e12, 1e300, BadFloat(int(T0) + 5), BadMath(T0 - 1)):
            with self.subTest(now=repr(now)[:20]):
                for v in ("continue", "outage", "timeout"):
                    self.assertEqual(o.step(self.path, v, now),  # type: ignore[arg-type]
                                     o.Decision(o.HOLD, attention=True))
                self.assertEqual(self.path.read_bytes(), before)

    def test_neg_save_refuses_absurd_numbers(self) -> None:
        for s in (o.State(stall_since=10 ** 400), o.State(outage_since=1e300),  # type: ignore[arg-type]
                  o.State(nudges=True, last_nudge_at=T0), o.State(nudges=10 ** 400)):  # type: ignore[arg-type]
            with self.subTest(state=repr(s)[:60]), self.assertRaises(ValueError):
                o.save(self.path, s)
        self.assertFalse(self.path.exists())

    def test_neg_unreadable_state_fails_closed(self) -> None:
        self.path.mkdir(parents=True)  # a directory at the path
        self.assertIsNone(o.load(self.path))
        self.assertEqual(o.step(self.path, "continue", T0), o.Decision(o.HOLD, attention=True))

    def test_neg_bad_path_or_unwritable_holds(self) -> None:
        for p in ("a\0b", 5):
            self.assertIsNone(o.load(p))  # type: ignore[arg-type]
            self.assertEqual(o.step(p, "timeout", T0), o.Decision(o.HOLD, attention=True))  # type: ignore[arg-type]
        blocker = Path(self._tmp.name) / "file"
        blocker.write_bytes(b"x")  # a file where the state's parent directory should be
        self.assertEqual(o.step(blocker / "outage.json", "timeout", T0),
                         o.Decision(o.HOLD, attention=True))
        self.assertEqual(blocker.read_bytes(), b"x")

    def test_neg_clock_backwards_or_invalid_holds(self) -> None:
        o.step(self.path, "timeout", T0)
        before = self.path.read_bytes()
        for now in (T0 - 1, -1.0, float("nan"), float("inf")):
            self.assertEqual(o.step(self.path, "timeout", now), o.Decision(o.HOLD, attention=True))
        self.assertEqual(self.path.read_bytes(), before)

    def test_neg_save_refuses_invalid_state(self) -> None:
        with self.assertRaises(ValueError):
            o.save(self.path, o.State(nudges=3, last_nudge_at=T0))
        self.assertFalse(self.path.exists())

    def test_neg_never_opens_live_db(self) -> None:
        seen: list[str] = []
        real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

        def spy(real):  # type: ignore[no-untyped-def]
            def inner(file, *a, **k):  # type: ignore[no-untyped-def]
                if not isinstance(file, int):  # an int is an fd already opened (fdopen)
                    seen.append(os.fspath(file))
                return real(file, *a, **k)
            return inner

        with mock.patch("builtins.open", spy(real_open)), \
                mock.patch("io.open", spy(real_io_open)), \
                mock.patch("os.open", spy(real_os_open)):
            self.run_seq([("outage", T0), ("continue", T0 + 1), ("timeout", T0 + 2)])
        self.assertTrue(seen)
        self.assertFalse([p for p in seen if "jarvis.db" in p or not p.startswith(self._tmp.name)])
        src = Path(o.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", src)

    # --- positive ----------------------------------------------------------
    def test_outage_clears_resume_allowed(self) -> None:
        self.run_seq([("outage", T0), ("outage", T0 + 600)])
        self.assertEqual(o.step(self.path, "continue", T0 + 700), o.Decision(o.CONTINUE))
        self.assertEqual(o.load(self.path), o.State())

    def test_watchdog_verdicts_map_to_one_action(self) -> None:
        cont = watchdog_verdict(last_progress_at=T0, now=T0 + 10, threshold=60)
        stall = watchdog_verdict(last_progress_at=T0, now=T0 + 61, threshold=60)
        self.assertEqual(o.step(self.path, cont, T0 + 10).action, o.CONTINUE)
        self.assertEqual(o.step(self.path, stall, T0 + 61).action, o.NUDGE)

    def test_state_file_round_trip_is_atomic_json(self) -> None:
        o.step(self.path, "timeout", T0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"version": 1, "nudges": 1, "last_nudge_at": T0,
                                "stall_since": T0, "outage_since": None, "dropped": False})
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["outage.json"])

    def test_decide_is_pure(self) -> None:
        s = o.State()
        d, new = o.decide(s, "timeout", T0)
        self.assertEqual((d.action, s.nudges), (o.NUDGE, 0))
        self.assertEqual(new, o.State(nudges=1, last_nudge_at=T0, stall_since=T0))
        self.assertEqual(o.decide(None, "continue", T0), (o.Decision(o.HOLD, attention=True), None))
        for now in (0, int(T0), -0.0, o.MAX_EPOCH_S):  # the gate keeps every sane time
            self.assertEqual(o.decide(s, "timeout", now)[0].action, o.NUDGE, now)


if __name__ == "__main__":
    unittest.main()
