import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lesson_coverage as LC  # noqa: E402

RULES = [
    {"rule": "ZZ10", "class": "enforced_in_code", "kaynak": "R.md:10"},
    {"rule": "ZZ2", "class": "enforced_in_code", "kaynak": "R.md:2"},
    {"rule": "ZZ1", "class": "every_round", "kaynak": "R.md:1"},
    {"rule": "ZZ11", "class": "topic", "kaynak": "R.md:11"},
    {"rule": "ZZ3", "class": "every_round", "kaynak": "R.md:3"},
    {"rule": "ZZ4", "class": "historical", "kaynak": "R.md:4"},
    {"rule": "ZZ5", "class": "dropped_dora", "kaynak": "R.md:5"},
]


class LessonCoverageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "tests").mkdir()
        # ZZ2 named as a word; ZZ10 only inside a longer token, so it is not guarded.
        (self.root / "tests" / "test_x.py").write_text("# guards ZZ2\nX = 'ZZ100'\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("add", "tests/test_x.py")
        self.rules = self.root / "rules.jsonl"
        lines = [json.dumps(r) for r in RULES] + ["{not json", "", '{"rule": "ZZ9"}']
        self.rules.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.cands = self.root / "cands.jsonl"
        cands = [{"dogrulama_durumu": "aday"}] * 3 + [{"dogrulama_durumu": "dogrulandi"}, {}]
        self.cands.write_text("\n".join(json.dumps(c) for c in cands) + "\n", encoding="utf-8")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def guarded(self):
        _, out, _ = self.run_cli("--json")
        return json.loads(out)["guarded"]

    def test_invalid_utf8_line_counted_unreadable(self):
        good = json.dumps(RULES[1]).encode()
        self.rules.write_bytes(good + b"\n" + b'{"rule": "\xff\xfe", "class": "topic"}\n' + b"\xff\xfe\n" + good + b"\n")
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        rep = json.loads(out)
        self.assertEqual(rep["unreadable"]["rules"], [2, 3])
        self.assertEqual(rep["classes"], {"enforced_in_code": 2})

    def test_untracked_binary_and_ignored_files_do_not_guard(self):
        cache = self.root / "tests" / "__pycache__"
        cache.mkdir()
        (cache / "old.cpython-312.pyc").write_bytes(b"\x00\x01ZZ10\x00")
        (self.root / "tests" / "test_untracked.py").write_text("# ZZ10\n", encoding="utf-8")
        (self.root / "tests" / "notes.txt").write_text("ZZ10\n", encoding="utf-8")
        self.git("add", "tests/notes.txt")
        self.assertEqual(self.guarded(), ["ZZ2"])
        # tracked binary .py is skipped by -I; a tracked text .py in a subfolder counts
        (self.root / "tests" / "bin.py").write_bytes(b"\x00ZZ10\x00")
        self.git("add", "-f", "tests/bin.py")
        self.assertEqual(self.guarded(), ["ZZ2"])
        (self.root / "tests" / "sub").mkdir()
        (self.root / "tests" / "sub" / "test_y.py").write_text("# ZZ10\n", encoding="utf-8")
        self.git("add", "tests/sub/test_y.py")
        self.assertEqual(self.guarded(), ["ZZ2", "ZZ10"])

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--rules", str(self.rules), "--candidates", str(self.cands), "--root", str(self.root), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = LC.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_json_shape_counts_and_coverage(self):
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        rep = json.loads(out)
        self.assertEqual(set(rep), {"classes", "live", "enforced_in_code", "coverage_pct", "guarded",
                                    "enforced_no_test", "live_not_in_code", "candidates", "unreadable"})
        self.assertEqual(rep["classes"], {"dropped_dora": 1, "enforced_in_code": 2, "every_round": 2,
                                          "historical": 1, "topic": 1})
        self.assertEqual(rep["live"], 5)  # historical and dropped_dora excluded
        self.assertEqual(rep["coverage_pct"], 40.0)
        self.assertEqual(rep["guarded"], ["ZZ2"])
        self.assertEqual(rep["enforced_no_test"], ["ZZ10"])
        self.assertEqual([r["rule"] for r in rep["live_not_in_code"]], ["ZZ1", "ZZ3", "ZZ11"])
        self.assertEqual(rep["unreadable"], {"rules": [8, 10], "candidates": []})
        self.assertEqual(rep["candidates"], {"total": 5, "dogrulama_durumu":
                                             {"(none)": 1, "aday": 3, "dogrulandi": 1}})

    def test_text_output(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("coverage: 2/5 = 40.0% enforced_in_code", out)
        self.assertIn("enforced, no test names it: ZZ10", out)
        self.assertIn("unreadable rules: 2 (lines 8, 10)", out)
        self.assertNotIn("ZZ4", out)
        self.assertNotIn("ZZ5", out)

    def test_percent_rounds_to_one_decimal(self):
        rows = [{"rule": "A1", "class": "enforced_in_code"}] * 2 + [{"rule": "A2", "class": "topic"}]
        self.rules.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        _, out, _ = self.run_cli("--json")
        self.assertEqual(json.loads(out)["coverage_pct"], 66.7)

    def test_natural_sort(self):
        self.assertEqual(sorted(["ZZ10", "ZZ2", "ZY1", "ZZ1"], key=LC.natural_key), ["ZY1", "ZZ1", "ZZ2", "ZZ10"])

    def test_missing_file_exits_2(self):
        self.cands = self.root / "absent.jsonl"
        code, out, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("absent.jsonl", err)

    def test_nothing_written(self):
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        self.run_cli()
        self.run_cli("--json")
        self.assertEqual(sorted(p.relative_to(self.root) for p in self.root.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
