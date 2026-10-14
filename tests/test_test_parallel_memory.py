"""CONVAUTO: scripts/test_parallel.py sizes its default workers by free memory (goal C) and every run on
this machine shares one budget of worker processes, build/locks/pool.json (goal D). Every pool test
uses its own temp folder, so the real build/locks is never touched; no test module runs."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_parallel.py"
_SPEC = importlib.util.spec_from_file_location("test_parallel", SCRIPT)
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)

# One child run: takes up to WANT tokens of BUDGET, notes the pool total it sees while holding them, gives them back.
CHILD = """
import importlib.util, json, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("tp", sys.argv[1])
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)
pool = tp.TestPool(Path(sys.argv[2]), int(sys.argv[3]))
pool.poll = 0.02
got = pool.acquire(int(sys.argv[4]))
seen = []
for _ in range(5):
    seen.append(tp._cas(Path(sys.argv[2]) / "pool.json", lambda text: (sum(json.loads(text or "{}").values()), None)))
    time.sleep(0.03)
pool.release()
print(json.dumps({"got": got, "seen": max(seen)}))
"""


class WorkerCountsTest(unittest.TestCase):
    def test_unknown_memory_keeps_the_cpu_defaults(self):
        self.assertEqual(tp.worker_counts(12, None), (6, 4))
        self.assertEqual(tp.worker_counts(2, None), (1, 1))

    def test_plenty_of_memory_keeps_the_cpu_defaults(self):
        self.assertEqual(tp.worker_counts(12, 64.0), (6, 4))

    def test_low_memory_shrinks_the_defaults_never_below_one(self):
        self.assertEqual(tp.WORKER_MEM_GB, 1.0)
        self.assertEqual(tp.worker_counts(12, 5.5), (3, 2))  # 5 processes in all, the UI share kept
        for free in (1.9, 0.5, 0.0):
            self.assertEqual(tp.worker_counts(12, free), (1, 1), free)
        totals = [sum(tp.worker_counts(12, free / 2)) for free in range(40, -1, -1)]
        self.assertEqual(totals, sorted(totals, reverse=True))  # less memory never more processes

    def test_explicit_flags_win(self):
        self.assertEqual(tp.worker_counts(12, 0.5, workers=6, ui_workers=3), (6, 3))
        self.assertEqual(tp.worker_counts(12, 0.5, workers=5), (5, 1))
        self.assertEqual(tp.worker_counts(12, 64.0, ui_workers=1), (6, 1))

    def test_free_memory_reads_or_is_unknown(self):
        free = tp.free_mem_gb()
        self.assertTrue(free is None or free > 0, free)


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jarvis-pool-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def other_run(self):
        """A live process that is not this one (another run's pid)."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def held(self):
        return json.loads((self.dir / "pool.json").read_text(encoding="utf-8"))

    def pool(self, budget):
        pool = tp.TestPool(self.dir, budget)
        pool.poll, pool.notice = 0.02, 0.0
        return pool

    def test_takes_what_is_free_and_gives_it_back(self):
        other = self.other_run()
        (self.dir / "pool.json").write_text(json.dumps({str(other.pid): 3}), encoding="utf-8")
        pool = self.pool(4)
        self.assertEqual(pool.acquire(10), 1)
        self.assertEqual(self.held(), {str(other.pid): 3, str(os.getpid()): 1})
        pool.release()
        self.assertEqual(self.held(), {str(other.pid): 3})

    def test_waits_in_the_foreground_until_a_token_is_free(self):
        other = self.other_run()
        (self.dir / "pool.json").write_text(json.dumps({str(other.pid): 2}), encoding="utf-8")
        pool, out, got = self.pool(2), io.StringIO(), []
        with contextlib.redirect_stdout(out):
            waiter = threading.Thread(target=lambda: got.append(pool.acquire(2)))
            waiter.start()
            time.sleep(0.3)
            self.assertEqual(got, [])  # none free: still waiting
            (self.dir / "pool.json").write_text(json.dumps({str(other.pid): 1}), encoding="utf-8")
            waiter.join(10)
        self.assertEqual(got, [1])
        self.assertIn("waiting for a free test worker token (machine budget 2, all held)", out.getvalue())
        pool.release()

    def test_a_killed_runs_tokens_come_back(self):
        other = self.other_run()
        (self.dir / "pool.json").write_text(json.dumps({str(other.pid): 3, "garbage": 1}), encoding="utf-8")
        pool = self.pool(3)
        got = []
        waiter = threading.Thread(target=lambda: got.append(pool.acquire(3)))
        with contextlib.redirect_stdout(io.StringIO()):
            waiter.start()
            time.sleep(0.2)
            self.assertEqual(got, [])
            other.kill()
            other.wait()
            waiter.join(10)
        self.assertEqual(got, [3])
        self.assertEqual(self.held(), {str(os.getpid()): 3})
        pool.release()

    def test_concurrent_runs_never_exceed_the_budget(self):
        procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(SCRIPT), str(self.dir), "3", "2"],
                                  stdout=subprocess.PIPE, text=True) for _ in range(4)]
        runs = [json.loads(p.communicate(timeout=60)[0].splitlines()[-1]) for p in procs]  # after any waiting line
        self.assertTrue(all(1 <= r["got"] <= 2 for r in runs), runs)
        self.assertLessEqual(max(r["seen"] for r in runs), 3, runs)
        self.assertEqual(self.held(), {})  # all given back

    def test_budget_one_still_finishes(self):
        pool = self.pool(1)
        with mock.patch.object(tp, "lock_dir", lambda: self.dir), mock.patch.dict(os.environ, {"CI": ""}), \
                mock.patch.object(tp, "TestPool", lambda folder, budget: pool):
            got, ui_workers, workers = tp.pool_for(4, 6)
        self.assertEqual((got, ui_workers, workers), (pool, 1, 0))
        units = [tp.Unit(n, [""], [f"tests.{n}.T.test"], 1.0, n.startswith("ui")) for n in ("ui_a", "b", "c")]
        fake = lambda python, unit, env, durations: tp.Result(unit, [], 0, "", 0.0)  # noqa: E731
        args = argparse.Namespace(write_durations="", report="", durations=0, python=sys.executable)
        with mock.patch.object(tp, "run_unit", fake):
            results, _, _, bases = tp.execute(args, units, ui_workers, workers)
        for base in bases:
            shutil.rmtree(base, ignore_errors=True)
        self.assertEqual(sorted(r.unit.module for r in results), ["b", "c", "ui_a"])
        pool.release()

    def test_budget_from_pace_json_only_when_valid(self):
        locks = self.dir / "build" / "locks"
        pace = self.dir / "build" / "conveyor" / "pace.json"
        self.assertEqual(tp.pool_budget(locks), tp.POOL_TOKENS)  # no pace.json
        pace.parent.mkdir(parents=True)
        for raw, want in (({"test_pool_tokens": 3}, 3), ({"test_pool_tokens": 0}, tp.POOL_TOKENS),
                          ({"test_pool_tokens": True}, tp.POOL_TOKENS), ({"test_pool_tokens": "4"}, tp.POOL_TOKENS),
                          ({"max_lanes": 6}, tp.POOL_TOKENS), ([], tp.POOL_TOKENS)):
            pace.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(tp.pool_budget(locks), want, raw)
        pace.write_text("{not json", encoding="utf-8")
        self.assertEqual(tp.pool_budget(locks), tp.POOL_TOKENS)

    def test_no_pool_under_ci(self):
        with mock.patch.dict(os.environ, {"CI": "true"}):
            self.assertEqual(tp.pool_for(4, 6), (None, 4, 6))


if __name__ == "__main__":
    unittest.main()
