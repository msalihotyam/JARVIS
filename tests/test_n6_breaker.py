"""N6-17 (G16/R16): per-root cross-run breaker. Negative cases first; time is always injected."""

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


def roots(st: nb.State) -> list[str]:
    return [r for r, _, _ in st.opened]


THREE_BAD = [("N6-01", "BLOCKED", T0), ("N6-02", "FAIL", T0 + 60), ("N6-03", "STOP", T0 + 120)]
SAME_ROOT = [("N6-05", "FAIL", T0), ("N6-05-AFX", "FAIL", T0 + 1), ("N6-05-2-AFX", "FAIL", T0 + 2)]


class N6BreakerTest(unittest.TestCase):
    # ---- F1 NEG: every BAD finish counts per root ----
    def test_neg_same_root_three_bad_in_window_opens(self) -> None:
        st = run(SAME_ROOT)  # the CDX-N6-17 F1 probe
        self.assertEqual(roots(st), ["N6-05"])
        self.assertEqual(st.opened[0][1], T0 + 2)
        self.assertEqual(len(st.bad), 3)
        for v in (["N6-05", "N6-05-AFX", "n6-05-2-afx", "N6-05-S-R", "N6-05-R-AFX"],
                  ["G16", "G16-AFX", "G16-2-AFX"], ["A1", "A1-2-AFX", "A1-S"]):
            with self.subTest(v=v):
                self.assertTrue(nb.is_open(run([(n, "FAIL", T0 + i) for i, n in enumerate(v)])))

    def test_neg_lowercase_padded_status_and_cdx_dry_stop_fail_count(self) -> None:
        self.assertEqual(roots(run([("a1", " failed ", T0), ("A1-AFX", "blocked", T0 + 1),
                                    ("A1-R", "Stop", T0 + 2)])), ["A1"])
        self.assertEqual(roots(run([("CDX-1", "STOP", T0), ("CDX-1-AFX", "FAIL", T0 + 1),
                                    ("CDX-1-R", "FAILED", T0 + 2)])), ["CDX-1"])

    def test_neg_distinct_roots_do_not_open(self) -> None:
        st = run(THREE_BAD)
        self.assertFalse(nb.is_open(st))
        self.assertEqual(len(st.bad), 3)
        # zero-padded slice numbers are never iterations: distinct slices never merge
        st = run([("N6-05-AFX", "FAIL", T0), ("N6-02-AFX", "FAIL", T0 + 1), ("N6-03-AFX", "FAIL", T0 + 2)])
        self.assertFalse(nb.is_open(st))
        # AFX lanes of different unpadded slices never pool into a phantom root (review D1)
        for v, other in ((["N6-11-AFX", "N6-12-AFX", "N6-13-AFX"], "N6-14-AFX"),
                         (["A1-2-AFX", "A1-3-AFX", "A1-4-AFX"], "A1-5-AFX")):
            with self.subTest(v=v):
                st = run([(n, "FAIL", T0 + i) for i, n in enumerate(v)])
                self.assertFalse(nb.is_open(st))
                self.assertFalse(nb.holds(st, other))
        # the real chain still opens: the root itself failed too
        self.assertEqual(roots(run([(n, "FAIL", T0 + i) for i, n in enumerate(
            ["N6", "N6-1-AFX", "N6-2-AFX"])])), ["N6"])

    def test_neg_same_root_spread_over_two_hours_does_not_open(self) -> None:
        st = run([("N6-05", "FAIL", T0), ("N6-05-AFX", "FAIL", T0 + H + 1), ("N6-05-R", "FAIL", T0 + 2 * H + 1)])
        self.assertFalse(nb.is_open(st))
        self.assertEqual(len(st.bad), 2)
        edge = [("N6-05", "FAIL", T0), ("N6-05", "FAIL", T0 + 1)]
        self.assertFalse(nb.is_open(run([*edge, ("N6-05", "FAIL", T0 + 7200)])))  # 7200 s is out
        self.assertTrue(nb.is_open(run([*edge, ("N6-05", "FAIL", T0 + 7199)])))

    def test_neg_cdx_dry_blocked_is_excluded(self) -> None:
        st = run([("CDX-1", "BLOCKED", T0), ("CDX-1-AFX", "BLOCKED", T0 + 1), ("CDX-1-R", "BLOCKED", T0 + 2),
                  ("DRY-2", "BLOCKED", T0 + 3)])
        self.assertEqual(st, nb.State())
        # a CDX in the middle of the name is not a review lane: it counts
        st = run([("N6-CDX", "BLOCKED", T0), ("N6-CDX-AFX", "BLOCKED", T0 + 1), ("N6-CDX-R", "BLOCKED", T0 + 2)])
        self.assertEqual(roots(st), ["N6-CDX"])

    def test_neg_unknown_status_and_bad_inputs(self) -> None:
        self.assertEqual(run([("A1", "CI_PENDING", T0), ("A1", "", T0), ("A1", "RUNNING", T0)]), nb.State())
        for name, fin in (("", T0), ("  ", T0), ("A", float("nan")), ("A", float("inf"))):
            with self.assertRaises(ValueError):
                nb.apply_finish(nb.State(), name, "FAIL", fin, T0)

    # ---- F4: every AFX/-S/-R iteration groups with its root, for any id shape ----
    def test_root_ids_table(self) -> None:
        table = {"N6-05": {"N6-05"}, "N6-05-AFX": {"N6-05"}, "n6-05-2-afx": {"N6-05-2", "N6-05"},
                 "N6-05-S-R": {"N6-05"}, "N6-05-R-AFX": {"N6-05"}, "N6-05-S-R-2-AFX": {"N6-05-S-R-2", "N6-05"},
                 "A1-2-AFX": {"A1-2", "A1"}, "G16-2-AFX": {"G16-2", "G16"}, "INT5-2-AFX": {"INT5-2", "INT5"},
                 "INT-7-AFX": {"INT-7", "INT"}, "N6-02-AFX": {"N6-02"}, "N6-10-AFX": {"N6-10", "N6"},
                 "-AFX": {"-AFX"}, " n6-05-afx ": {"N6-05"}, "N6-17FIX": {"N6-17FIX"}, "X-AFX-AFX": {"X"},
                 "A1-0-AFX": {"A1-0"}, "A1-S-3-AFX": {"A1-S-3", "A1"}}
        for name, want in table.items():
            with self.subTest(name=name):
                self.assertEqual(nb.root_ids(name), want)
                self.assertIn(nb.root_id(name), nb.root_ids(name))
        self.assertEqual(nb.root_id("N6-05-2-AFX"), "N6-05-2")

    # ---- F2 NEG: hold only the open root(s), INT included; never unrelated roots ----
    def test_neg_hold_only_open_roots(self) -> None:
        st = run([("INT-7", "FAIL", T0), ("INT-7-AFX", "FAIL", T0 + 1), ("INT-7-R", "FAIL", T0 + 2)])
        self.assertEqual(roots(st), ["INT-7"])
        for held in ("INT-7", "int-7-afx", "INT-7-S", "INT-7-3-AFX"):
            self.assertTrue(nb.holds(st, held), held)
        for free in ("INT-8", "INT", "N6-05", "INT-70"):
            self.assertFalse(nb.holds(st, free), free)
        st = run(SAME_ROOT)
        self.assertTrue(nb.holds(st, "N6-05-R"))
        for free in ("N6-06", "N6", "N6-050", "INT-7"):
            self.assertFalse(nb.holds(st, free), free)
        self.assertFalse(nb.holds(nb.State(), "N6-05"))
        self.assertTrue(nb.holds(nb.UNREADABLE, "N6-05"))

    # ---- F3 NEG: only a reset record or that root's GOOD finish closes it ----
    def test_neg_nothing_else_closes(self) -> None:
        st = run(SAME_ROOT)
        later = run([("N6-05-R", "RUNNING", T0 + 9 * H), ("N6-05", "FAIL", T0 + 10 * H)], state=st)
        self.assertEqual(roots(later), ["N6-05"])  # time passing, unknown status, more bad
        for name, fin in (("N6-06", T0 + H), ("A1", T0 + H), ("Z", T0 + H), ("N6-05", T0 + 2),
                          ("CDX-7", T0 + H), ("DRY-7", T0 + H), ("N6-05-2-AFX", T0 + H)):
            with self.subTest(name=name, fin=fin):
                self.assertTrue(nb.holds(nb.apply_finish(st, name, "PASS", fin, T0 + H), "N6-05"))

    def test_good_of_that_root_closes_only_it(self) -> None:
        st = run([*SAME_ROOT, ("A1", "FAIL", T0 + 3), ("A1-AFX", "FAIL", T0 + 4), ("A1-R", "FAIL", T0 + 5)])
        self.assertEqual(roots(st), ["A1", "N6-05"])
        st = nb.apply_finish(st, "a1-afx", " pass ", T0 + 10, T0 + 10)
        self.assertEqual(roots(st), ["N6-05"])
        st = nb.apply_finish(st, "N6-05-AFX", "OK", T0 + 11, T0 + 11)
        self.assertFalse(nb.is_open(st))
        # the close clears the old count: one new bad does not reopen, three do
        self.assertFalse(nb.is_open(run([("N6-05", "FAIL", T0 + 12)], state=st)))
        self.assertTrue(nb.is_open(run([(n, "FAIL", T0 + 12 + i) for i, n in enumerate(
            ["N6-05", "N6-05-R", "N6-05-S"])], state=st)))

    # ---- NEG unreadable state is open, never rewritten ----
    def test_neg_unreadable_state_is_open_and_untouched(self) -> None:
        cases = [b"", b"{", b"\xff\xfe", b"[]", b'{"bad": []}', b'{"bad": [], "breaker": null}',
                 b'{"bad": [["A", true]], "open": [], "cleared": []}',
                 b'{"bad": [["A", NaN]], "open": [], "cleared": []}',
                 b'{"bad": [["A", 1' + b"0" * 400 + b']], "open": [], "cleared": []}',
                 b'{"bad": [["", 1]], "open": [], "cleared": []}',
                 b'{"bad": [], "open": [["A", 1]], "cleared": []}',
                 b'{"bad": [], "open": [["A", 1, 2]], "cleared": []}', b"[" * 100_000]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "breaker.json"
            for raw in cases:
                with self.subTest(raw=raw[:40]):
                    p.write_bytes(raw)
                    self.assertIs(nb.load(p), nb.UNREADABLE)
                    self.assertTrue(nb.holds(nb.load(p), "Z"))
                    self.assertFalse(nb.record_finish(p, "A", "PASS", T0, T0))
                    self.assertEqual(p.read_bytes(), raw)

    # ---- F5 NEG: a lost state file while the open marker exists stays open ----
    def test_neg_missing_state_with_marker_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "breaker.json"
            for n, s, f in SAME_ROOT:
                self.assertTrue(nb.record_finish(p, n, s, f, f))
            self.assertTrue(Path(f"{p}.open").exists())
            p.unlink()
            self.assertIs(nb.load(p), nb.UNREADABLE)
            self.assertTrue(nb.holds(nb.load(p), "Z"))
            self.assertFalse(nb.record_finish(p, "N6-05", "PASS", T0 + 9, T0 + 9))
            self.assertFalse(p.exists())
            self.assertTrue(nb.reset(p, T0))  # the reset is the repair
            self.assertEqual(nb.load(p), nb.State())
            self.assertFalse(Path(f"{p}.open").exists())

    def test_neg_unnormalized_open_key_holds(self) -> None:
        # a hand-edited lowercase / padded root key still holds that root (review D3)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "breaker.json"
            p.write_text('{"bad": [], "open": [[" x ", 1, "r"]], "cleared": [["y", 1]]}', encoding="utf-8")
            st = nb.load(p)
            assert isinstance(st, nb.State)
            self.assertTrue(nb.holds(st, "X"))
            self.assertTrue(nb.holds(st, "x-afx"))
            self.assertFalse(nb.holds(st, "Y"))
            self.assertEqual(st.cleared, (("Y", 1.0),))

    def test_file_round_trip_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "breaker.json"
            self.assertEqual(nb.load(p), nb.State())
            self.assertFalse(nb.reset(p, T0))  # nothing open: nothing to close
            fins = [("N6-05", "FAIL", T0), ("N6-05-AFX", "FAIL", T0 + 60), ("N6-05-R", "STOP", T0 + 120),
                    ("A1", "FAIL", T0 + 200), ("A1-AFX", "FAIL", T0 + 201), ("A1-S", "BLOCKED", T0 + 202)]
            for n, s, f in fins:
                self.assertTrue(nb.record_finish(p, n, s, f, f))
            self.assertEqual(nb.load(p), run(fins))
            self.assertEqual(roots(run(fins)), ["A1", "N6-05"])
            self.assertFalse(nb.reset(p, T0 + 119))  # older than every opening: ignored
            self.assertTrue(nb.reset(p, T0 + 120))
            st = nb.load(p)
            assert isinstance(st, nb.State)
            self.assertEqual(roots(st), ["A1"])
            self.assertFalse(nb.holds(st, "N6-05"))
            self.assertTrue(nb.reset(p, T0 + 300))
            self.assertEqual(nb.load(p), nb.State())
            self.assertFalse(Path(f"{p}.open").exists())
            self.assertFalse(nb.reset(p, T0 + 400))
            p.write_text("{", encoding="utf-8")
            self.assertTrue(nb.reset(p, T0))
            self.assertEqual(nb.load(p), nb.State())
            self.assertEqual(sorted(x.name for x in p.parent.iterdir()), ["breaker.json", "breaker.json.lock"])
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"bad": [], "open": [], "cleared": []})
            with self.assertRaises(ValueError):
                nb.reset(p, float("nan"))

    def test_neg_never_opens_jarvis_db(self) -> None:
        with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db opened")), \
                tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.json"
            for n, s, f in SAME_ROOT:
                nb.record_finish(p, n, s, f, f)
            self.assertTrue(nb.holds(nb.load(p), "N6-05"))
            self.assertTrue(nb.reset(p, T0 + 5))
        self.assertFalse(hasattr(nb, "sqlite3"))


if __name__ == "__main__":
    unittest.main()
