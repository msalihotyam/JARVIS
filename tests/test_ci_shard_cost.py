"""CISHARDCOST1: scripts/ci_shard_cost.py models shard fixed cost from shard reports, offline."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_PATH = ROOT / "scripts" / "ci_shard_cost.py"
_SPEC = importlib.util.spec_from_file_location("ci_shard_cost", _PATH)
csc = importlib.util.module_from_spec(_SPEC)
_WRITE, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ under scripts/
try:
    _SPEC.loader.exec_module(csc)
finally:
    sys.dont_write_bytecode = _WRITE


def _report(i, n=8, wall=None, durations=((40.0, "a (t.A.a)"), (20.0, "b (t.A.b)")), **extra):
    doc = {"shard": [i, n], "wall": 100.0 + i if wall is None else wall,
           "durations": [list(r) for r in durations], "ran": len(durations)}
    doc.update(extra)
    return json.dumps(doc)


class ParseTests(unittest.TestCase):
    def refused(self, text):
        with self.assertRaises(csc.Refused):
            csc.parse(text)

    def test_bad_input_is_refused_not_raised(self):
        for text in ('{"wall": 1', '{"wall": 1, "wall": 2}', '{"wall": NaN}', '{"wall": Infinity}',
                     '{"wall": 1e999}', '{"wall": -1}', '{"wall": "12"}', '{"wall": true}', "[1, 2]",
                     "[" * 100000, '{"wall": 5, "durations": [[-1.0, "x"]]}',
                     '{"wall": 5, "durations": [["1", "x"]]}', '{"wall": 5, "durations": [[1.0]]}',
                     '{"shard": [3, 2], "wall": 5}'):
            with self.subTest(text=text[:40]):
                self.refused(text)

    def test_missing_fields_are_not_measured(self):
        self.assertEqual(csc.parse('{"durations": [[1.5, "x"]]}'), {"shards": None, "wall": None, "tests_sum": 1.5})
        self.assertIsNone(csc.parse('{"wall": 5, "durations": [], "ran": 3}')["tests_sum"])
        self.assertEqual(csc.parse('{"shard": null, "wall": 5, "durations": [], "ran": 0}'),
                         {"shards": 1, "wall": 5.0, "tests_sum": 0.0})


class ReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "in"
        self.dir.mkdir()
        self.out = Path(tmp.name) / "out" / "model.md"

    def write(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")

    def shards(self, count=8):
        for i in range(1, count + 1):
            self.write(f"s{i}.json", _report(i))

    def run_main(self, *extra):
        with mock.patch("sys.stdout"):
            self.assertEqual(csc.main([str(self.dir), "--out", str(self.out), *extra]), 0)
        return self.out.read_text(encoding="utf-8")

    def test_model_from_eight_shards(self):
        self.shards()
        text = self.run_main()
        self.assertIn("| s1.json | 8 | 101.0 | 60.0 | 41.0 | ok |", text)
        self.assertIn("fixed s: median 44.5, p90 48.0", text)
        self.assertIn("W = median(tests x shard count) = 480.0 s", text)
        for row in ("| 16 | 74.5 | 78.0 |", "| 24 | 64.5 | 68.0 |", "| 32 | 59.5 | 63.0 |"):
            self.assertIn(row, text)
        self.assertIn("gains under 5% from 40 shards", text)
        self.assertEqual(csc.DEFAULT_OUT, "build/reports/CISHARDCOST1-model.md")

    def test_negative_fixed_is_inconsistent_and_left_out(self):
        self.shards()
        self.write("s9.json", _report(1, wall=10.0))
        text = self.run_main()
        self.assertIn("| s9.json | 8 | 10.0 | 60.0 | -50.0 | inconsistent |", text)
        self.assertIn("usable: 8; refused: 0; inconsistent: 1", text)
        self.assertIn("fixed s: median 44.5, p90 48.0", text)
        self.assertIn("--concurrency P", text)

    def test_refused_files_are_counted(self):
        self.shards()
        for name, text in (("bad1.json", "{"), ("bad2.json", '{"wall": NaN}'), ("bad3.json", '{"a": 1, "a": 2}'),
                           ("bad4.json", '{"wall": -3}'), ("bad5.json", '{"wall": "x"}')):
            self.write(name, text)
        (self.dir / "bad6.json").write_bytes(b"\xff\xfe\x00")
        text = self.run_main()
        self.assertIn("usable: 8; refused: 6; inconsistent: 0", text)
        self.assertIn("fixed s: median 44.5", text)

    def test_fewer_than_five_usable_is_insufficient(self):
        self.shards(4)
        self.write("nm.json", '{"shard": [5, 8], "durations": [[1.0, "x"]]}')
        text = self.run_main()
        self.assertIn("insufficient data: 4 usable records", text)
        self.assertNotIn("gate(N)", text)
        self.assertIn("| nm.json | 8 | not measured | 1.0 | not measured | not measured |", text)
        self.assertIn("not measured: 1", text)

    def test_unsafe_names_and_links_are_not_followed(self):
        self.shards()
        self.write("x..json", _report(1, wall=10.0))
        self.write("link.json", _report(1, wall=10.0))
        self.write("away.json", _report(1, wall=10.0))
        outside = str(self.dir.parent / "elsewhere.json")
        real, islink = os.path.realpath, os.path.islink
        with mock.patch.object(csc.os.path, "islink", lambda p: str(p).endswith("link.json") or islink(p)), \
                mock.patch.object(csc.os.path, "realpath", lambda p: outside if str(p).endswith("away.json") else real(p)):
            text = self.run_main()
        self.assertIn("skipped (link, '..' or outside the folder): 3", text)
        self.assertIn("inconsistent: 0", text)
        self.assertNotIn("link.json", text.split("skipped")[1])

    def test_file_cap_is_stated(self):
        self.shards(7)
        text = self.run_main("--max-files", "5")
        self.assertIn("input: 5 of 7 JSON files read (cap --max-files 5; CAPPED, 2 not read)", text)
        self.assertEqual(csc.MAX_FILES, 5000)

    def test_output_is_deterministic_and_states_localci(self):
        self.shards()
        first = csc.build_report(self.dir)
        self.assertEqual(first, csc.build_report(self.dir))
        self.assertIn("localci", first)
        self.assertIn("P = 1;", first)

    def test_bad_arguments_fail_closed(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(csc.main([str(self.dir / "missing"), "--out", str(self.out)]), 2)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
