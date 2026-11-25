"""CIOSSHADOW1: the OS shadow compare tool, its static check and list, and the gate's isolation."""
from __future__ import annotations

import collections
import contextlib
import fnmatch
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shadow = _load("ci_os_shadow_under_test", ROOT / "scripts" / "ci_os_shadow.py")
WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
MODS = ["tests.test_a", "tests.test_b"]


def report(ids, *, shard=None, green=True, failures=0, errors=0, wall=10.0, durations=None):
    return {"shard": shard, "green": green, "wall": wall, "units": [], "dispatched": ids, "ran": len(ids),
            "skipped": 0, "skips": [], "failures": failures, "errors": errors, "accounted": True,
            "rule_d": [], "discovered": len(ids),
            "durations": durations if durations is not None else [[1.0, f"x ({i})"] for i in ids]}


IDS = ["tests.test_a.A.test_1", "tests.test_b.B.test_1"]


def write_run(base: Path, name: str, *, windows=None, linux=None, win_summary="", linux_summary="") -> Path:
    run = base / name
    if windows is not None:
        (run / "ci-shard-1").mkdir(parents=True)
        (run / "ci-shard-1" / "shard-report.json").write_text(json.dumps(windows), encoding="utf-8")
        (run / "ci-shard-1" / "shard-summary.txt").write_text(win_summary, encoding="utf-8")
    if linux is not None:
        (run / "ci-os-shadow").mkdir(parents=True)
        (run / "ci-os-shadow" / "os-shadow-report.json").write_text(json.dumps(linux), encoding="utf-8")
        (run / "ci-os-shadow" / "os-shadow-summary.txt").write_text(linux_summary, encoding="utf-8")
    run.mkdir(parents=True, exist_ok=True)
    return run


GREEN_W = report(IDS, shard=[1, 1], wall=100.0)
GREEN_L = report(IDS, wall=40.0)
FAIL_B = "FAIL: test_1 (tests.test_b.B.test_1)\n"


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ciosshadow1-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def runs(self, n, **kw):
        start = len(list(self.tmp.iterdir()))
        return [shadow.read_run(write_run(self.tmp, f"r{start + i:03}", **kw), MODS) for i in range(n)]

    def test_same_in_n_runs_is_safe_with_ratio_and_modelled_gate(self):
        runs = self.runs(10, windows=GREEN_W, linux=GREEN_L)
        rows = shadow.compare(runs, MODS, 10)
        self.assertTrue(all(r["safe"] for r in rows.values()))
        self.assertEqual(rows["tests.test_a"]["ratio"], 1.0)
        self.assertEqual(shadow.modelled_gate(runs, {"tests.test_a", "tests.test_b"}), (100.0, 98.0))

    def test_negative_1_pass_on_linux_fail_on_windows_differs_never_safe(self):
        red_w = report(IDS, shard=[1, 1], green=False, failures=1)
        for kw in ({"windows": red_w, "linux": GREEN_L, "win_summary": FAIL_B},
                   {"windows": GREEN_W, "linux": report(IDS, green=False, failures=1), "linux_summary": FAIL_B}):
            with self.subTest(kw=sorted(kw)):
                rows = shadow.compare(self.runs(10, **kw), MODS, 10)
                self.assertEqual(rows["tests.test_b"]["verdicts"], ["differs"] * 10)
                self.assertFalse(rows["tests.test_b"]["safe"])
                self.assertTrue(rows["tests.test_a"]["safe"])
                self.setUp()

    def test_failing_on_both_sides_is_same_but_never_safe(self):
        red_w = report(IDS, shard=[1, 1], green=False, failures=1)
        rows = shadow.compare(self.runs(10, windows=red_w, linux=report(IDS, green=False, failures=1),
                                        win_summary=FAIL_B, linux_summary=FAIL_B), MODS, 10)
        self.assertEqual(rows["tests.test_b"]["verdicts"][-1], "same")
        self.assertFalse(rows["tests.test_b"]["safe"])

    def test_negative_2_missing_on_one_side_never_safe(self):
        for kw in ({"windows": GREEN_W, "linux": report(IDS[:1])}, {"windows": GREEN_W}, {"linux": GREEN_L}):
            with self.subTest(kw=sorted(kw)):
                rows = shadow.compare(self.runs(10, **kw), MODS, 10)
                self.assertEqual(rows["tests.test_b"]["verdicts"][-1], "missing")
                self.assertFalse(rows["tests.test_b"]["safe"])
                self.setUp()

    def test_unaccountable_red_side_is_unknown_never_safe(self):
        # red with a failure the (truncated) summary does not name: no module can be called passing
        red = report(IDS, green=False, failures=1)
        rows = shadow.compare(self.runs(10, windows=GREEN_W, linux=red, linux_summary=""), MODS, 10)
        self.assertEqual({r["verdicts"][-1] for r in rows.values()}, {"unknown"})
        self.assertFalse(any(r["safe"] for r in rows.values()))
        broken = shadow.read_run(write_run(self.tmp, "bad", windows=GREEN_W), MODS)
        (self.tmp / "bad" / "ci-shard-1" / "shard-report.json").write_text("{not json", encoding="utf-8")
        broken = shadow.read_run(self.tmp / "bad", MODS)
        self.assertEqual(broken["windows"]["results"]["tests.test_a"][0], "unknown")

    def test_negative_6_fewer_than_n_runs_not_enough_and_no_safe_set(self):
        rows = shadow.compare(self.runs(9, windows=GREEN_W, linux=GREEN_L), MODS, 10)
        self.assertTrue(all(r["safe"] is None for r in rows.values()))
        dirs = [str(write_run(self.tmp, f"c{i}", windows=GREEN_W, linux=GREEN_L)) for i in range(3)]
        out = io.StringIO()
        with mock.patch.object(shadow, "read_list", return_value=MODS), contextlib.redirect_stdout(out):
            self.assertEqual(shadow.main(["compare", *dirs]), 0)
        self.assertIn("not enough runs: 3 of 10; no safe set", out.getvalue())
        self.assertNotIn("safe to move", out.getvalue())

    def test_only_the_last_n_runs_count(self):
        red_l = report(IDS, green=False, failures=1)
        old = self.runs(1, windows=GREEN_W, linux=red_l, linux_summary=FAIL_B)
        rows = shadow.compare(old + self.runs(3, windows=GREEN_W, linux=GREEN_L), MODS, 3)
        self.assertTrue(rows["tests.test_b"]["safe"])
        rows = shadow.compare(self.runs(3, windows=GREEN_W, linux=GREEN_L) + old, MODS, 3)
        self.assertFalse(rows["tests.test_b"]["safe"])

    def test_absent_shard_other_test_ids_and_a_repeated_run_dir_are_never_safe(self):
        # shard 1 of 2 only: the module's classes in shard 2 are unseen, so the Windows side is unknown
        rows = shadow.compare(self.runs(10, windows=report(IDS, shard=[1, 2]), linux=GREEN_L), MODS, 10)
        self.assertEqual({r["verdicts"][-1] for r in rows.values()}, {"unknown"})
        # both green, but Linux ran a test Windows did not
        rows = shadow.compare(self.runs(10, windows=GREEN_W, linux=report(IDS + ["tests.test_b.B.test_2"])),
                              MODS, 10)
        self.assertEqual(rows["tests.test_b"]["verdicts"], ["differs"] * 10)
        self.assertFalse(rows["tests.test_b"]["safe"])
        self.assertTrue(rows["tests.test_a"]["safe"])
        run = str(write_run(self.tmp, "once", windows=GREEN_W, linux=GREEN_L))
        with mock.patch.object(shadow, "read_list", return_value=MODS), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(shadow.main(["compare", run, run, "--runs-needed", "2"]), 0)


class StaticCheckTests(unittest.TestCase):
    MARKED = ["import ctypes\nctypes.windll.user32", "import winreg", "import msvcrt", "import win32api",
              "import comtypes", "import uiautomation", "from PySide6 import QtCore", "from jarvis.ui import x",
              "run('powershell -NoProfile')", "script = 'tools/x.ps1'", "if sys.platform == 'win32': pass"]

    def setUp(self):
        self.tests = Path(tempfile.mkdtemp(prefix="ciosshadow1-tests-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tests, ignore_errors=True))
        (self.tests / "test_plain.py").write_text("import json\n", encoding="utf-8")
        for i, text in enumerate(self.MARKED):
            (self.tests / f"test_marked_{i}.py").write_text(text + "\n", encoding="utf-8")

    def write_list(self, *lines) -> Path:
        path = self.tests / "list.txt"
        path.write_text("# header\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8")
        return path

    def test_negative_4_each_marker_is_flagged_and_excluded(self):
        modules, flagged = shadow.scan(self.tests)
        self.assertEqual(modules, ["tests.test_plain"])
        self.assertEqual(sorted(flagged), sorted(f"tests.test_marked_{i}" for i in range(len(self.MARKED))))
        for i in range(len(self.MARKED)):
            with self.subTest(marker=self.MARKED[i]):
                with self.assertRaisesRegex(shadow.Refused, "flagged"):
                    shadow.read_list(self.write_list("tests.test_plain", f"tests.test_marked_{i}"), self.tests)

    def test_negative_5_duplicate_path_like_and_non_module_entries_refused(self):
        for bad, reason in (("tests.test_plain", "duplicate"), ("tests/test_plain.py", "not a tests.test_"),
                            ("tests\\test_plain", "not a tests.test_"), ("tests.test_plain.Class", "not a tests.test_"),
                            ("jarvis.config", "not a tests.test_"), ("tests.test_nope", "does not exist")):
            with self.subTest(bad=bad), self.assertRaisesRegex(shadow.Refused, reason):
                shadow.read_list(self.write_list("tests.test_plain", bad), self.tests)
        with self.assertRaisesRegex(shadow.Refused, "empty"):
            shadow.read_list(self.write_list(), self.tests)
        self.assertEqual(shadow.read_list(self.write_list("tests.test_plain"), self.tests), ["tests.test_plain"])

    def test_committed_list_passes_the_check(self):
        modules = shadow.read_list()
        self.assertGreater(len(modules), 0)
        self.assertNotIn("tests.test_ci_os_shadow", modules)  # it reads the workflow text, which names pwsh


class GateIsolationTests(unittest.TestCase):
    def job(self, name: str) -> str:
        match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", WORKFLOW, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_acceptance_the_workflow_only_adds_a_non_blocking_read_only_job(self):
        jobs = re.findall(r"^  ([\w-]+):\n", WORKFLOW.split("\njobs:\n", 1)[1], re.MULTILINE)
        self.assertEqual(jobs, ["shard", "aggregate", "types", "os-shadow"])
        shadow_job = self.job("os-shadow")
        self.assertIn("    continue-on-error: true\n", shadow_job)
        self.assertIn("    permissions:\n      contents: read\n", shadow_job)
        self.assertNotIn("secrets.", WORKFLOW)
        self.assertNotIn("needs:", shadow_job)
        self.assertIn("    needs: shard\n", self.job("aggregate"))
        self.assertIn("name: ci-os-shadow\n", shadow_job)
        self.assertIn("exit 0", shadow_job)
        uses = re.findall(r"uses: ([\w./-]+)@(\S+)", WORKFLOW)
        old = {u for u in re.findall(r"uses: ([\w./-]+)@(\S+)", WORKFLOW.split("\n  os-shadow:\n")[0])}
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", sha) for _, sha in uses))
        self.assertLessEqual(set(uses), old)

    def test_negative_3_red_linux_job_leaves_the_gate_verdict_unchanged(self):
        self.assertFalse(fnmatch.fnmatch("ci-os-shadow", "ci-shard-*"))  # aggregate + localci_shadow download pattern
        tp = _load("test_parallel_ciosshadow1", ROOT / "scripts" / "test_parallel.py")
        unit = SimpleNamespace(label="u1", weight=1.0, ui=False, ids=[IDS[0]])
        patches = {"discover": lambda _n: ["test_a"],
                   "discover_tests": lambda *_a, **_k: {"errors": [], "serial": [IDS[0]]},
                   "discovered_ids": lambda _d: collections.Counter([IDS[0]]),
                   "ui_modules": lambda _n: set(), "load_durations": lambda **_k: {},
                   "build_units": lambda *_a: [unit], "shard_plan": lambda _u, _s: [[unit]]}
        outputs = []
        for red_shadow in (False, True):
            with tempfile.TemporaryDirectory(prefix="ciosshadow1-agg-") as tmp:
                gate = report([IDS[0]], shard=[1, 1])
                gate["units"] = ["u1"]
                write_run(Path(tmp), "shards", windows=gate,
                          linux=report([IDS[0]], green=False, failures=1) if red_shadow else None)
                args = SimpleNamespace(aggregate=str(Path(tmp) / "shards"), shards=1, python=sys.executable,
                                       durations=30, write_durations=None)
                out = io.StringIO()
                with contextlib.ExitStack() as stack:
                    for name, fake in patches.items():
                        stack.enter_context(mock.patch.object(tp, name, fake))
                    stack.enter_context(contextlib.redirect_stdout(out))
                    outputs.append((tp.aggregate(args), out.getvalue()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1][0], 0)

    def test_negative_3_a_failing_shadow_check_run_is_not_a_required_check(self):
        from jarvis.n6_ci_watch import ci_verdict
        sha = "a" * 40
        runs = [{"name": n, "head_sha": sha, "status": "completed", "conclusion": c}
                for n, c in (("aggregate", "success"), ("types", "success"), ("os shadow (non-blocking)", "failure"))]
        verdict = ci_verdict(sha, required=["aggregate", "types"],
                             checks=json.dumps({"total_count": len(runs), "check_runs": runs}))
        self.assertEqual(verdict.verdict, "green")
        self.assertIn("name: os shadow (non-blocking)\n", self.job("os-shadow"))


if __name__ == "__main__":
    unittest.main()
