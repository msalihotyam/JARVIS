"""N6-17 (G16/R16): cross-run breaker. Negative cases first; time is always injected."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import n6_breaker as nb

T0 = 1_000_000.0
H = 3600.0


def run(finishes: list[tuple[str, str, float]], now: float | None = None,
        state: nb.State | None = None) -> nb.State:
    st = state or nb.State()
    for name, status, fin in finishes:
        st = nb.apply_finish(st, name, status, fin, fin if now is None else now)
    return st


THREE_BAD = [("N6-01", "BLOCKED", T0), ("N6-02", "FAIL", T0 + 60), ("N6-03", "STOP", T0 + 120)]


class N6BreakerTest(unittest.TestCase):
    # ---- NEG: what must open ----
    def test_neg_three_distinct_bad_roots_in_window_open(self) -> None:
        st = run(THREE_BAD)
        self.assertTrue(nb.is_open(st))
        self.assertEqual(st.roots, ("N6-01", "N6-02", "N6-03"))
        self.assertEqual(st.since, T0 + 120)

    def test_neg_lowercase_padded_bad_status_counts(self) -> None:
        st = run([("A1", " failed ", T0), ("A2", "blocked", T0 + 1), ("A3", "Stop", T0 + 2)])
        self.assertTrue(nb.is_open(st))

    def test_neg_cdx_dry_stop_and_fail_still_count(self) -> None:
        st = run([("CDX-1", "STOP", T0), ("DRY-2", "FAIL", T0 + 1), ("N6-03", "FAILED", T0 + 2)])
        self.assertTrue(nb.is_open(st))

    # ---- NEG: what must not open ----
    def test_neg_bad_spread_over_two_hours_does_not_open(self) -> None:
        st = run([("N6-01", "FAIL", T0), ("N6-02", "FAIL", T0 + H + 1), ("N6-03", "FAIL", T0 + 2 * H + 1)])
        self.assertFalse(nb.is_open(st))
        self.assertEqual(len(st.bad), 2)

    def test_neg_window_edge_exactly_7200_is_out(self) -> None:
        st = run([("N6-01", "FAIL", T0), ("N6-02", "FAIL", T0 + 1), ("N6-03", "FAIL", T0 + 7200)])
        self.assertFalse(nb.is_open(st))
        self.assertTrue(nb.is_open(run([("N6-01", "FAIL", T0), ("N6-02", "FAIL", T0 + 1),
                                        ("N6-03", "FAIL", T0 + 7199)])))

    def test_neg_same_root_and_chain_suffixes_count_once(self) -> None:
        names = ["N6-05", "N6-05-AFX", "n6-05-2-afx", "N6-05-S-R", "N6-05-R-AFX"]
        st = run([(n, "FAIL", T0 + i) for i, n in enumerate(names)])
        self.assertFalse(nb.is_open(st))
        self.assertEqual(len(st.bad), 1)
        self.assertEqual(nb.root_id("N6-05-S-R-2-AFX"), "N6-05")
        self.assertEqual(nb.root_id("-AFX"), "-AFX")
        # a slice's own number is never stripped: distinct slices never merge
        self.assertEqual([nb.root_id(n) for n in ("N6-05-AFX", "N6-02-AFX", "INT5-2-AFX", "INT-7-AFX")],
                         ["N6-05", "N6-02", "INT5", "INT-7"])

    def test_neg_cdx_dry_blocked_is_excluded(self) -> None:
        st = run([("CDX-1", "BLOCKED", T0), ("DRY-2", "BLOCKED", T0 + 1), ("CDX-3-AFX", "BLOCKED", T0 + 2)])
        self.assertFalse(nb.is_open(st))
        self.assertEqual(st.bad, ())
        # a CDX in the middle of the name is not a review lane: it counts
        st = run([("N6-CDX", "BLOCKED", T0), ("N6-DRY", "BLOCKED", T0 + 1), ("N6-03", "BLOCKED", T0 + 2)])
        self.assertTrue(nb.is_open(st))

    def test_neg_unknown_status_and_bad_inputs(self) -> None:
        st = run([("A1", "CI_PENDING", T0), ("A2", "", T0), ("A3", "RUNNING", T0)])
        self.assertEqual(st, nb.State())
        for name, fin in (("", T0), ("  ", T0), ("A", float("nan")), ("A", float("inf"))):
            with self.assertRaises(ValueError):
                nb.apply_finish(nb.State(), name, "FAIL", fin, T0)

    # ---- NEG: nothing resets except a reset record or a GOOD finish ----
    def test_neg_nothing_else_closes(self) -> None:
        st = run(THREE_BAD)
        later = run([("N6-09", "RUNNING", T0 + 9 * H), ("N6-10", "FAIL", T0 + 10 * H)], state=st)
        self.assertTrue(nb.is_open(later))  # time passing, unknown status, more bad
        self.assertIn("N6-10", later.roots)
        self.assertTrue(nb.is_open(nb.apply_finish(st, "N6-11", "PASS", T0 + 120, T0 + 999)))  # fin == since
        self.assertTrue(nb.is_open(nb.apply_finish(st, "N6-02-AFX", "PASS", T0 + H, T0 + H)))  # a bad root
        self.assertTrue(nb.is_open(nb.apply_finish(st, "CDX-7", "PASS", T0 + H, T0 + H)))  # review lane
        self.assertTrue(nb.is_open(nb.apply_finish(st, "DRY-7", "OK", T0 + H, T0 + H)))

    def test_good_new_root_after_opening_closes(self) -> None:
        st = nb.apply_finish(run(THREE_BAD), "N6-12", " pass ", T0 + 200, T0 + 200)
        self.assertFalse(nb.is_open(st))
        self.assertEqual(st.bad, ())

    def test_neg_hold_is_non_int_only(self) -> None:
        st = run(THREE_BAD)
        self.assertTrue(nb.holds(st, "N6-20"))
        self.assertTrue(nb.holds(st, "INTERNAL-1"))
        self.assertFalse(nb.holds(st, "INT-7"))
        self.assertFalse(nb.holds(st, "int7"))
        self.assertFalse(nb.holds(nb.State(), "N6-20"))
        self.assertTrue(nb.holds(nb.UNREADABLE, "N6-20"))

    # ---- file: unreadable = open, never rewritten by a finish; reset rules ----
    def test_neg_unreadable_state_is_open_and_untouched(self) -> None:
        bad_files = [b"", b"{", b"\xff\xfe", b"[]", b'{"bad": []}', b'{"bad": {}, "breaker": null}',
                     b'{"bad": [["A", true]], "breaker": null}', b'{"bad": [["A", NaN]], "breaker": null}',
                     b'{"bad": [["A", 1' + b"0" * 400 + b']], "breaker": null}',
                     b'{"bad": [["", 1]], "breaker": null}', b'{"bad": [], "breaker": {"since": 1}}',
                     b'{"bad": [], "breaker": {"since": 1, "roots": [1], "reason": ""}}',
                     b"[" * 100_000]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "breaker.json"
            for raw in bad_files:
                p.write_bytes(raw)
                self.assertIs(nb.load(p), nb.UNREADABLE, raw[:60])
                self.assertTrue(nb.holds(nb.load(p), "N6-01"))
                self.assertFalse(nb.record_finish(p, "N6-12", "PASS", T0, T0))
                self.assertEqual(p.read_bytes(), raw)

    def test_file_round_trip_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "breaker.json"
            self.assertEqual(nb.load(p), nb.State())
            self.assertFalse(nb.reset(p, T0))  # nothing open: no record needed
            for name, status, fin in THREE_BAD:
                self.assertTrue(nb.record_finish(p, name, status, fin, fin))
            self.assertTrue(nb.is_open(nb.load(p)))
            self.assertEqual(nb.load(p), run(THREE_BAD))
            self.assertFalse(nb.reset(p, T0 + 119))  # older than the opening: ignored
            self.assertTrue(nb.is_open(nb.load(p)))
            self.assertTrue(nb.reset(p, T0 + 120))  # reset == since closes
            self.assertEqual(nb.load(p), nb.State())
            p.write_text("{", encoding="utf-8")
            self.assertTrue(nb.reset(p, T0))  # operator reset repairs an unreadable file
            self.assertEqual(nb.load(p), nb.State())
            self.assertEqual(sorted(x.name for x in p.parent.iterdir()), ["breaker.json", "breaker.json.lock"])
            with self.assertRaises(ValueError):
                nb.reset(p, float("nan"))

    def test_neg_never_opens_jarvis_db(self) -> None:
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db opened")):
            p = Path(d) / "breaker.json"
            for name, status, fin in THREE_BAD:
                nb.record_finish(p, name, status, fin, fin)
            self.assertTrue(nb.holds(nb.load(p), "N6-04"))
            nb.reset(p, T0 + H)
            self.assertNotIn("jarvis.db", json.dumps([x.name for x in Path(d).rglob("*")]))
        self.assertFalse(hasattr(nb, "sqlite3"))


if __name__ == "__main__":
    unittest.main()
