"""N6-07: E8 side-by-side report over the hash-pinned M9 acceptance set (negative-first)."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from jarvis import n6_kpi

ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = (ROOT / "data" / "jarvis.db").resolve()
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
_SPEC = importlib.util.spec_from_file_location("n6_e8_report", ROOT / "scripts" / "n6_e8_report.py")
e8 = importlib.util.module_from_spec(_SPEC)
sys.modules["n6_e8_report"] = e8
_SPEC.loader.exec_module(e8)

GOOD_SET = {
    "version": 1,
    "limits": {"false_pass": 0, "security_incidents": 0, "rollback_drill_required": True},
    "measures": dict(e8.MEASURES),
    "slice_ids": ["S1", "S2"],
}


def _row(slice_id, source, **over):
    base = dict(slice_id=slice_id, source=source, passed=True, post_accept_defects=0,
                salih_interventions=1, quota_tokens=1_000_000, finish_seconds=1800.0,
                security_incidents=0, recorded_at=T0)
    base.update(over)
    return n6_kpi.KpiRow(**base)


def _pairs(n6_over=None, conveyor_over=None, slices=("S1", "S2")):
    out = []
    for s in slices:
        out.append(_row(s, "conveyor", **(conveyor_over or {})))
        out.append(_row(s, "n6", **(n6_over or {})))
    return out


class N6E8ReportTests(unittest.TestCase):
    def setUp(self):
        self.opened = []
        real_connect = sqlite3.connect

        def spy(database, *args, **kwargs):
            self.opened.append(str(database))
            return real_connect(database, *args, **kwargs)

        patch = mock.patch("sqlite3.connect", spy)
        patch.start()
        self.addCleanup(patch.stop)
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEMP"), ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.set_path = self._write_set(GOOD_SET)

    def tearDown(self):
        # NEG: no test in this module ever opens the live database.
        for opened in self.opened:
            self.assertNotEqual(Path(opened).resolve(), LIVE_DB)

    def _write_set(self, doc, *, raw=None, pin=None):
        data = raw if raw is not None else json.dumps(doc, separators=(",", ":")).encode()
        path = self.dir / e8.SET_NAME
        path.write_bytes(data)
        digest = pin or hashlib.sha256(data).hexdigest()
        (self.dir / e8.SUMS_NAME).write_text(f"{digest}  {e8.SET_NAME}\n", encoding="ascii")
        return path

    def _verdict(self, rows):
        code, text = e8.report(rows, self.set_path)
        return code, text, text.splitlines()

    def assertNoAdvance(self, rows, reason):
        code, text, lines = self._verdict(rows)
        self.assertEqual(code, 1, text[-300:])
        self.assertEqual(lines[-1], e8.NO_ADVANCE)
        self.assertNotIn(e8.ADVANCE, lines)
        self.assertTrue(any(line.startswith("REASON:") and reason in line for line in lines),
                        text[-300:])

    # NEG: one false PASS fails the report even when every other measure is better.
    def test_one_false_pass_fails_regardless_of_other_measures(self):
        rows = _pairs(n6_over=dict(salih_interventions=0, quota_tokens=10, finish_seconds=1.0),
                      conveyor_over=dict(passed=False, post_accept_defects=5))
        rows[1] = _row("S1", "n6", post_accept_defects=1, salih_interventions=0, quota_tokens=10,
                       finish_seconds=1.0)
        self.assertNoAdvance(rows, "false PASS 1: S1")

    def test_one_worse_measure_does_not_advance(self):
        cases = {
            "pass_rate": dict(passed=False, quota_tokens=0),
            "post_accept_defects": dict(passed=False, post_accept_defects=1),
            "salih_interventions": dict(salih_interventions=2),
            "quota_tokens_per_accepted": dict(quota_tokens=1_000_001),
            "finish_seconds": dict(finish_seconds=1800.5),
        }
        for name, over in cases.items():
            with self.subTest(name):
                rows = _pairs()
                rows[1] = _row("S1", "n6", **over)
                self.assertNoAdvance(rows, f"{name} is worse")

    def test_n6_without_any_accepted_slice_never_ties(self):
        rows = _pairs(n6_over=dict(passed=False), conveyor_over=dict(passed=False))
        self.assertNoAdvance(rows, "quota_tokens_per_accepted is worse")

    def test_security_incident_does_not_advance(self):
        rows = _pairs(conveyor_over=dict(security_incidents=3))
        rows[3] = _row("S2", "n6", security_incidents=1)
        self.assertNoAdvance(rows, "n6 security incidents 1")

    # NEG: a set whose bytes no longer match SHA256SUMS is refused.
    def test_edited_set_is_refused_on_hash_mismatch(self):
        self.set_path.write_bytes(self.set_path.read_bytes().replace(b'"S2"', b'"S3"'))
        code, text, lines = self._verdict(_pairs())
        self.assertEqual(code, 2)
        self.assertTrue(lines[0].startswith("REFUSED: hash mismatch"), text)
        self.assertEqual(lines[-1], e8.NO_ADVANCE)

    # NEG: a malformed set is refused even when its hash is pinned correctly.
    def test_malformed_set_is_refused(self):
        def variant(**over):
            return json.dumps({**GOOD_SET, **over}).encode()

        cases = {
            "bad json": b"{not json",
            "not an object": b"[]",
            "version true": variant(version=True),
            "version float": variant(version=1.0),
            "version 2": variant(version=2),
            "extra key": variant(extra=1),
            "missing key": json.dumps({k: v for k, v in GOOD_SET.items() if k != "limits"}).encode(),
            "false_pass 1": variant(limits={**GOOD_SET["limits"], "false_pass": 1}),
            "security false": variant(limits={**GOOD_SET["limits"], "security_incidents": False}),
            "drill 1": variant(limits={**GOOD_SET["limits"], "rollback_drill_required": 1}),
            "extra limit": variant(limits={**GOOD_SET["limits"], "x": 0}),
            "wrong direction": variant(measures={**e8.MEASURES, "pass_rate": "lower"}),
            "missing measure": variant(measures={"pass_rate": "higher"}),
            "duplicate ids": variant(slice_ids=["S1", "S1"]),
            "empty id": variant(slice_ids=[""]),
            "id not str": variant(slice_ids=[1]),
            "ids not list": variant(slice_ids="S1"),
            "not utf8": b"\xff\xfe",
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self._write_set(None, raw=raw)
                code, text, lines = self._verdict(_pairs())
                self.assertEqual(code, 2, text)
                self.assertTrue(lines[0].startswith("REFUSED: malformed set"), text)
                self.assertEqual(lines[-1], e8.NO_ADVANCE)

    def test_bad_sums_file_is_refused(self):
        sums = self.dir / e8.SUMS_NAME
        good = sums.read_text(encoding="ascii")
        digest = good.split()[0]
        for name, text in {"empty": "", "upper hex": good.upper(), "short": digest[:63] + "  x",
                           "other name": f"{digest}  other.json\n",
                           "two lines": good + good, "missing": None}.items():
            with self.subTest(name):
                if text is None:
                    sums.unlink()
                else:
                    sums.write_text(text, encoding="ascii")
                code, out, lines = self._verdict(_pairs())
                self.assertEqual(code, 2, out)
                self.assertTrue(lines[0].startswith("REFUSED:"), out)

    def test_duplicate_row_is_refused(self):
        code, text, _ = self._verdict(_pairs() + [_row("S1", "n6")])
        self.assertEqual(code, 2, text)

    # NEG: rows outside the set and unpaired set slices are never counted.
    def test_unpaired_and_outside_rows_are_not_counted(self):
        rows = _pairs() + [_row("X9", "n6", post_accept_defects=9, security_incidents=4,
                                salih_interventions=50)]
        code, text, lines = self._verdict(rows)
        self.assertEqual((code, lines[-1]), (0, e8.ADVANCE), text)
        self.assertIn("OUTSIDE SET (not counted): X9/n6", lines)
        self.assertIn("FALSE PASS (n6): 0", lines)
        # A set slice with only one side is listed and blocks; its row is not counted.
        rows = _pairs() + [_row("S3", "n6", post_accept_defects=7)]
        self._write_set({**GOOD_SET, "slice_ids": ["S1", "S2", "S3"]})
        self.assertNoAdvance(rows, "set slice S3 lacks an n6 or conveyor row")
        _, text, lines = self._verdict(rows)
        self.assertIn("UNPAIRED (not counted): S3", lines)
        self.assertIn("FALSE PASS (n6): 0", lines)

    def test_empty_set_does_not_advance(self):
        self._write_set({**GOOD_SET, "slice_ids": []})
        self.assertNoAdvance(_pairs(), "no paired slices")

    def test_all_measures_equal_or_better_advances(self):
        better = dict(salih_interventions=0, quota_tokens=5, finish_seconds=60.0)
        for name, n6_over in {"equal": {}, "better": better}.items():
            with self.subTest(name):
                code, text, lines = self._verdict(_pairs(n6_over=n6_over))
                self.assertEqual(code, 0, text)
                self.assertEqual(lines[-1], e8.ADVANCE)
                self.assertFalse(any(line.startswith("REASON:") for line in lines), text)
                self.assertTrue(lines[0].startswith("PINNED SHA256: "), text)

    def test_shipped_set_verifies_and_prints_its_pinned_hash(self):
        shipped = ROOT / "dev" / "n6" / "acceptance" / e8.SET_NAME
        pinned = (shipped.parent / e8.SUMS_NAME).read_text(encoding="ascii").split()[0]
        self.assertEqual(hashlib.sha256(shipped.read_bytes()).hexdigest(), pinned)
        self.assertEqual(e8.load_set(shipped), (pinned, ()))
        _, text = e8.report([], shipped)
        self.assertIn(f"PINNED SHA256: {pinned}", text.splitlines())

    def test_cli_reads_an_explicit_db_and_refuses_a_missing_one(self):
        db = self.dir / "kpi.db"
        for row in _pairs():
            n6_kpi.record(db, row)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = e8.main(["--db", str(db), "--set", str(self.set_path)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().splitlines()[-1], e8.ADVANCE)
        self.assertTrue(self.opened)
        missing = self.dir / "missing.db"
        with contextlib.redirect_stdout(io.StringIO()):
            code = e8.main(["--db", str(missing), "--set", str(self.set_path)])
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            e8.main(["--set", str(self.set_path)])  # --db is required, never defaulted


if __name__ == "__main__":
    unittest.main()
