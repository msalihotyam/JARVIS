import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("pm32_compare", ROOT / "scripts" / "pm32_compare.py")
pm32 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm32)

VERDICT = "project mode pass rate >= single agent: "
TASKS = [f"PM32-{n:02d}" for n in range(1, 21)]


def record(task_id, arm, claimed=True, hidden="pass", quota=10, duration=60):
    return {"task_id": task_id, "arm": arm, "claimed_done": claimed,
            "hidden_check_result": hidden, "quota": quota, "duration": duration}


def full_runs(project_fail=0, single_fail=0):
    runs = []
    for i, task_id in enumerate(TASKS):
        runs.append(record(task_id, "project", hidden="fail" if i < project_fail else "pass"))
        runs.append(record(task_id, "single", hidden="fail" if i < single_fail else "pass"))
    return runs


class Pm32CompareTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / "v24" / "PM32.md"

    def run_scorer(self, runs, tasks=TASKS, runs_text=None):
        return self.run_raw(json.dumps(tasks).encode("utf-8"),
                            (runs_text or json.dumps({"runs": runs})).encode("utf-8"))

    def run_raw(self, tasks_bytes, runs_bytes):
        (self.dir / "tasks.json").write_bytes(tasks_bytes)
        (self.dir / "runs.json").write_bytes(runs_bytes)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = pm32.main(["--tasks", str(self.dir / "tasks.json"), "--runs",
                              str(self.dir / "runs.json"), "--out", str(self.out)])
        self.err = err.getvalue()
        return code

    def report(self, runs, tasks=TASKS):
        self.assertEqual(self.run_scorer(runs, tasks), 0, self.err[:80])
        return self.out.read_text(encoding="utf-8")

    def row(self, text, name):
        line = next(ln for ln in text.splitlines() if ln.startswith(f"| {name} |"))
        return [cell.strip() for cell in line.strip("|").split("|")][1:]

    # Acceptance 1 + 3: record format in, both arms side by side, verdict line out.
    def test_full_records_write_side_by_side_table_and_yes_verdict(self):
        text = self.report(full_runs(project_fail=2, single_fail=5))
        self.assertIn("| metric | project mode | single agent |", text)
        self.assertEqual(self.row(text, "pass rate (accepted / scored)"),
                         ["18/20 (90.0%)", "15/20 (75.0%)"])
        self.assertEqual(self.row(text, "quota per accepted task"), ["11.11", "13.33"])
        self.assertEqual(self.row(text, "duration total"), ["1200.0s", "1200.0s"])
        self.assertIn(VERDICT + "yes", text)

    def test_lower_project_pass_rate_gives_no(self):
        text = self.report(full_runs(project_fail=6, single_fail=5))
        self.assertIn(VERDICT + "no", text)

    def test_equal_pass_rates_give_yes(self):
        text = self.report(full_runs(project_fail=3, single_fail=3))
        self.assertIn(VERDICT + "yes", text)

    def test_default_out_is_build_v24_pm32(self):
        self.assertEqual(pm32.DEFAULT_OUT, ROOT / "build" / "v24" / "PM32.md")

    # Acceptance 2: false-done = claimed done with failing hidden checks.
    def test_false_done_counts_only_claimed_done_with_failing_checks(self):
        runs = full_runs()
        runs[0] = record(TASKS[0], "project", claimed=True, hidden="fail")
        runs[2] = record(TASKS[1], "project", claimed=False, hidden="fail")
        runs[4] = record(TASKS[2], "project", claimed=False, hidden="pass")
        text = self.report(runs)
        self.assertEqual(self.row(text, "false-done (claimed done, hidden checks fail)"), ["1", "0"])
        self.assertEqual(self.row(text, "false-done rate (false-done / claimed done)"),
                         ["1/18 (5.6%)", "0/20 (0.0%)"])
        self.assertEqual(self.row(text, "accepted (claimed done, hidden checks pass)"), ["17", "20"])

    # Acceptance 4: deterministic output for the same records.
    def test_same_records_in_any_order_give_identical_bytes(self):
        runs = full_runs(project_fail=1, single_fail=4)
        first = self.report(runs)
        second = self.report(list(reversed(runs)), tasks=list(reversed(TASKS)))
        self.assertEqual(first, second)

    # Negative 1: a missing run record is "missing", never pass or fail; verdict incomplete.
    def test_missing_run_record_is_missing_and_verdict_incomplete(self):
        runs = [r for r in full_runs() if not (r["task_id"] == TASKS[3] and r["arm"] == "single")]
        text = self.report(runs)
        self.assertEqual(self.row(text, "missing runs"), ["0", "1"])
        self.assertEqual(self.row(text, "scored runs"), ["20", "19"])
        self.assertEqual(self.row(text, "pass rate (accepted / scored)"),
                         ["20/20 (100.0%)", "19/19 (100.0%)"])
        self.assertEqual(self.row(text, "false-done (claimed done, hidden checks fail)"), ["0", "0"])
        self.assertIn("Missing (single agent): PM32-04", text)
        self.assertIn(VERDICT + "incomplete", text)

    def test_null_hidden_check_result_is_missing_not_fail(self):
        runs = full_runs()
        runs[0] = record(TASKS[0], "project", hidden=None)
        text = self.report(runs)
        self.assertEqual(self.row(text, "missing runs"), ["1", "0"])
        self.assertEqual(self.row(text, "false-done (claimed done, hidden checks fail)"), ["0", "0"])
        self.assertIn(VERDICT + "incomplete", text)

    # Negative 2: quota missing -> "missing", not 0.
    def test_missing_quota_shows_missing_not_zero(self):
        runs = full_runs()
        runs[1] = record(TASKS[0], "single", quota=None)
        text = self.report(runs)
        self.assertEqual(self.row(text, "quota total"), ["200.00", "missing"])
        self.assertEqual(self.row(text, "quota per accepted task"), ["10.00", "missing"])

    def test_missing_record_makes_quota_missing(self):
        runs = [r for r in full_runs() if not (r["task_id"] == TASKS[0] and r["arm"] == "project")]
        text = self.report(runs)
        self.assertEqual(self.row(text, "quota per accepted task"), ["missing", "10.00"])
        self.assertEqual(self.row(text, "duration total"), ["missing", "1200.0s"])

    # Negative 3: records for an unknown task id are refused.
    def test_unknown_task_id_is_refused_and_nothing_written(self):
        runs = full_runs() + [record("PM32-99", "project")]
        self.assertEqual(self.run_scorer(runs), 2)
        self.assertIn("unknown task id", self.err)
        self.assertFalse(self.out.exists())

    def test_bad_records_are_refused(self):
        # Keys are substrings of the expected refusal message.
        bad = {
            "unknown arm": record(TASKS[0], "codex"),
            "duplicate run record": record(TASKS[1], "project"),
            "claimed_done": record(TASKS[0], "project", claimed="yes"),
            "hidden_check_result": record(TASKS[0], "project", hidden="passed"),
            "bad quota": record(TASKS[0], "project", quota=-1),
            "bad duration": record(TASKS[0], "project", duration=True),
        }
        for name, extra in bad.items():
            with self.subTest(name):
                self.assertEqual(self.run_scorer(full_runs()[1:] + [extra]), 2)
                self.assertIn(name, self.err)
                self.assertFalse(self.out.exists())

    def test_nan_quota_and_bad_task_lists_are_refused(self):
        text = json.dumps({"runs": full_runs()}).replace('"quota": 10', '"quota": NaN', 1)
        self.assertEqual(self.run_scorer(None, runs_text=text), 2)
        for tasks in ([], TASKS + [TASKS[0]], TASKS + [""], TASKS + [7]):
            with self.subTest(tasks=tasks[-1:]):
                self.assertEqual(self.run_scorer(full_runs(), tasks=tasks), 2)
        self.assertFalse(self.out.exists())

    # PM32SCORE1FIX F1: ids are written raw into the report; anything but the allowlist is refused.
    def test_task_ids_that_could_forge_report_lines_are_refused(self):
        forged = "x\n" + VERDICT + "yes"
        bad_ids = [forged, "x\ry", "x\r\ny", "x\vy", "x\fy", "x\x1cy", "x\x1dy", "x\x1ey", "x\x85y",
                   "x\u2028y", "x\u2029y", "x\ty", "x\x00y", "x\x9by", "x\u200by", "x\u202ey",
                   "x\u00a0y", "\u0420M32-21", "a, b", VERDICT + "yes", "-x", ".x", " x", "x ",
                   "x" * 81, "PM32\uff0d21"]
        for task_id in bad_ids:
            with self.subTest(task_id=task_id[:40]):
                runs = full_runs() + [record(task_id, "project"), record(task_id, "single")]
                self.assertEqual(self.run_scorer(runs, tasks=TASKS + [task_id]), 2)
                self.assertIn("bad task id", self.err)
                self.assertNotIn("\n", self.err.rstrip("\n"))
                self.assertFalse(self.out.exists())

    def test_allowlisted_task_ids_are_accepted_with_one_verdict_line(self):
        tasks = TASKS + ["a.b_c-1", "Z" * 80]
        runs = full_runs() + [record(t, arm) for t in tasks[20:] for arm in ("project", "single")]
        text = self.report(runs, tasks=tasks)
        self.assertEqual([ln for ln in text.splitlines() if VERDICT in ln], [VERDICT + "yes"])

    # PM32SCORE1FIX F2: non-finite or overflowing quota/duration is refused, never "inf" or a traceback.
    def test_non_finite_or_overflowing_amounts_are_refused(self):
        base = json.dumps({"runs": full_runs()})
        cases = {
            "1e309 quota": base.replace('"quota": 10', '"quota": 1e309', 1),
            "-1e309 duration": base.replace('"duration": 60', '"duration": -1e309', 1),
            "401-digit quota": base.replace('"quota": 10', '"quota": 1' + "0" * 400, 1),
            "5000-digit duration": base.replace('"duration": 60', '"duration": 1' + "0" * 5000, 1),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(self.run_scorer(None, runs_text=text), 2)
                self.assertFalse(self.out.exists())

    def test_overflowing_totals_are_refused(self):
        for key in ("quota", "duration"):
            with self.subTest(key):
                runs = full_runs()
                for r in runs:
                    r[key] = 1e308
                self.assertEqual(self.run_scorer(runs), 2)
                self.assertIn("total overflows", self.err)
                self.assertFalse(self.out.exists())

    def test_large_finite_amounts_give_same_text_as_before(self):
        runs = full_runs()
        runs[0]["quota"] = 10 ** 20
        text = self.report(runs)
        self.assertEqual(self.row(text, "quota total"), [f"{10 ** 20 + 190:.2f}", "200.00"])

    # PM32SCORE1FIX F3: undecodable or pathological input files are refused with exit 2.
    def test_non_utf8_or_too_deep_input_is_refused(self):
        good_tasks = json.dumps(TASKS).encode("utf-8")
        good_runs = json.dumps({"runs": full_runs()}).encode("utf-8")
        cases = {
            "tasks 0xFF": (b'["\xff"]', good_runs),
            "runs 0xFF": (good_tasks, b'{"runs": ["\xff"]}'),
            "runs deep nesting": (good_tasks, b"[" * 200000 + b"]" * 200000),
        }
        for name, (tasks_bytes, runs_bytes) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.run_raw(tasks_bytes, runs_bytes), 2)
                self.assertIn("refused", self.err)
                self.assertFalse(self.out.exists())

    # Negative 4: fewer than 20 tasks per arm -> verdict incomplete.
    def test_fewer_than_twenty_tasks_gives_incomplete(self):
        tasks = TASKS[:19]
        runs = [r for r in full_runs(single_fail=5) if r["task_id"] in tasks]
        text = self.report(runs, tasks=tasks)
        self.assertEqual(self.row(text, "missing runs"), ["0", "0"])
        self.assertIn(VERDICT + "incomplete", text)


if __name__ == "__main__":
    unittest.main()
