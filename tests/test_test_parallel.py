"""TESTSPEED1: scripts/test_parallel.py fills UI processes first when memory or the pool cuts the process
count, so the UI set no longer sets the suite's critical path; ``--ui-split share`` keeps the old cut.
The plan is replayed on scripts/test_durations.json (recorded seconds, no clock); no test module runs."""
import argparse
import importlib.util
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_parallel.py"
_SPEC = importlib.util.spec_from_file_location("test_parallel", SCRIPT)
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)

MEMORY_GB = 6.5  # this machine's observed cut: 6 slots, 12 CPUs


def real_units() -> list:
    """The suite's units as discovery would give them, rebuilt from the recorded durations file."""
    data = json.loads(tp.DURATIONS.read_text(encoding="utf-8"))
    modules: dict = {}
    for cls in data["classes"]:
        _, module, name = cls.split(".", 2)
        tests = [t for t in data["tests"] if t.rsplit(".", 1)[0] == cls] or [cls + ".test"]
        modules.setdefault(module, {"split": True, "classes": {}})["classes"][name] = tests
    return tp.build_units({"modules": modules}, tp.ui_modules(sorted(modules)), tp.load_durations(),
                          tp.load_durations(key="tests"))


def old_worker_counts(cpus, free_gb, workers=0, ui_workers=0):
    """The pre-TESTSPEED1 formula, verbatim."""
    w, ui = min(cpus - 1, 6), min(4, cpus // 3)
    if free_gb is not None:
        slots = int(free_gb // tp.WORKER_MEM_GB)
        ui = min(ui, slots * ui // max(1, ui + w))
        w = min(w, slots - ui)
    return max(1, workers or w), max(1, ui_workers or ui)


class PlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.units = real_units()
        cls.old = tp.worker_counts(12, MEMORY_GB)  # (workers, ui)
        cls.new = tp.worker_counts(12, MEMORY_GB, ui_first=True)

    def plan(self, counts):
        workers, ui_workers = counts
        return tp.plan_processes(self.units, ui_workers, workers)

    def test_counts_on_this_machine(self):
        self.assertEqual(self.old, (4, 2))
        self.assertEqual(self.new, (2, 4))  # same 6 processes, UI first

    def test_every_unit_is_assigned_exactly_once(self):
        self.assertGreater(sum(u.ui for u in self.units), 0)
        want = sorted((u.label, tuple(u.ids)) for u in self.units)
        for counts in (self.old, self.new, (1, 1), (5, 1)):
            planned = [u for run in self.plan(counts).values() for u in run]
            self.assertEqual(len(planned), len(self.units), counts)
            self.assertEqual(sorted((u.label, tuple(u.ids)) for u in planned), want, counts)

    def test_planned_ui_wall_is_at_most_60_percent_of_the_old(self):
        old, new = tp.ui_wall(self.plan(self.old)), tp.ui_wall(self.plan(self.new))
        self.assertGreater(old, 0)
        self.assertLessEqual(new, 0.6 * old, (new, old))

    def test_a_ui_unit_never_lands_on_a_plain_worker(self):
        for counts in (self.old, self.new, (6, 1)):
            for name, run in self.plan(counts).items():
                if name.startswith("plain"):
                    self.assertFalse([u.label for u in run if u.ui], (counts, name))

    def test_execute_runs_ui_units_only_on_ui_processes_each_once(self):
        workers, ui_workers = self.new
        seen: list = []
        fake = lambda python, unit, env, durations: seen.append((unit, int(env["W"]))) or tp.Result(unit, [], 0, "", 0.0)  # noqa: E731
        args = argparse.Namespace(write_durations="", report="", durations=0, python=sys.executable)
        with mock.patch.object(tp, "run_unit", fake), \
                mock.patch.object(tp, "worker_env", lambda index, bases: {"W": str(index)}):
            results, _, _, bases = tp.execute(args, self.units, ui_workers, workers)
        for base in bases:
            shutil.rmtree(base, ignore_errors=True)
        self.assertEqual(sorted(r.unit.label for r in results), sorted(u.label for u in self.units))
        self.assertEqual(len(seen), len(self.units))
        self.assertFalse([u.label for u, index in seen if u.ui and index > ui_workers])


class RollbackTest(unittest.TestCase):
    def test_the_default_split_is_the_old_formula(self):
        for cpus in range(1, 17):
            for free in [None] + [n / 2 for n in range(41)]:
                self.assertEqual(tp.worker_counts(cpus, free), old_worker_counts(cpus, free), (cpus, free))
                self.assertEqual(tp.worker_counts(cpus, free, 5, 3), old_worker_counts(cpus, free, 5, 3))

    def test_the_old_counts_reproduce_the_old_plan(self):
        workers, ui_workers = old_worker_counts(12, MEMORY_GB)
        plan = tp.plan_processes(real_units(), ui_workers, workers)
        self.assertEqual(list(plan), ["ui1", "ui2", "plain3", "plain4", "plain5", "plain6"])

    def test_ui_first_never_more_processes_and_at_least_one_each(self):
        for free in range(0, 41):
            old, new = old_worker_counts(12, free / 2), tp.worker_counts(12, free / 2, ui_first=True)
            self.assertLessEqual(sum(new), max(sum(old), 2), free)
            self.assertGreaterEqual(min(new), 1, free)
        self.assertEqual(tp.worker_counts(12, None, ui_first=True), (6, 4))  # uncut: the CPU defaults

    def pool_split(self, got, ui_first):
        pool = mock.Mock()
        pool.acquire.return_value = got
        with mock.patch.object(tp, "lock_dir", lambda: Path("unused")), mock.patch.dict(os.environ, {"CI": ""}), \
                mock.patch.object(tp, "pool_budget", lambda folder: 8), \
                mock.patch.object(tp, "TestPool", lambda folder, budget: pool):
            return tp.pool_for(4, 6, ui_first)[1:]

    def test_pool_cut_share_is_the_old_split_and_first_fills_ui(self):
        for got in range(1, 10):
            ui = max(1, got * 4 // 10)
            self.assertEqual(self.pool_split(got, False), (ui, got - ui), got)
        self.assertEqual(self.pool_split(6, True), (4, 2))
        self.assertEqual(self.pool_split(1, True), (1, 0))
        self.assertEqual(self.pool_split(10, True), (4, 6))  # not cut: unchanged

    def test_ci_keeps_the_requested_counts(self):
        with mock.patch.dict(os.environ, {"CI": "true"}):
            self.assertEqual(tp.pool_for(4, 6, True), (None, 4, 6))


if __name__ == "__main__":
    unittest.main()
