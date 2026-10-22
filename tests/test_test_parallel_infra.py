"""TESTINFRA: scripts/test_parallel.py's unit timeout, UI lock line, median shard planning,
load-sensitive count, setUpClass skip naming and the DB tripwire. Every test uses its own temp
folder; the real build/locks and data/ are never touched."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_parallel.py"
_SPEC = importlib.util.spec_from_file_location("test_parallel", SCRIPT)
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)

# A test that hangs and leaves a grandchild holding the output pipes: only a tree kill ends it.
HANG = """
import subprocess, sys, time, unittest
class Hang(unittest.TestCase):
    def test_hangs(self):
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        time.sleep(120)
"""


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jarvis-testinfra-"))
        self.addCleanup(shutil.rmtree, self.dir, True)


class TimeoutTests(Temp):
    def _local(self):
        env = {k: v for k, v in os.environ.items() if k != "CI"}
        return mock.patch.dict(os.environ, env, clear=True), mock.patch.object(tp, "SHARDED", False)

    def test_limits(self):
        unit = lambda s: tp.Unit("test_x", [""], [], s, False)  # noqa: E731
        env, sharded = self._local()
        with env, sharded:
            self.assertEqual(tp.unit_timeout(unit(None)), 600.0)
            self.assertEqual(tp.unit_timeout(unit(10.0)), 120.0)
            self.assertEqual(tp.unit_timeout(unit(100.0)), 400.0)

    def test_ci_limits(self):
        """INT57B: a 27 s unit gets 600 s under CI (the variable or --shard), 120 s locally."""
        unit = lambda s: tp.Unit("test_x", [""], [], s, False)  # noqa: E731
        env, sharded = self._local()
        with env, sharded:
            self.assertEqual(tp.unit_timeout(unit(27.0)), 120.0)
            with mock.patch.dict(os.environ, {"CI": "true"}):
                self.assertEqual(tp.unit_timeout(unit(27.0)), 600.0)
                self.assertEqual(tp.unit_timeout(unit(100.0)), 1000.0)
                self.assertEqual(tp.unit_timeout(unit(None)), 600.0)
            with mock.patch.object(tp, "SHARDED", True):
                self.assertEqual(tp.unit_timeout(unit(27.0)), 600.0)

    def test_a_hung_unit_is_killed_with_its_tree_and_named(self):
        (self.dir / "tests").mkdir()
        (self.dir / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.dir / "tests" / "test_hang.py").write_text(HANG, encoding="utf-8")
        unit = tp.Unit("test_hang", [""], ["tests.test_hang.Hang.test_hangs"], None, False)
        out = io.StringIO()
        with mock.patch.object(tp, "ROOT", self.dir), mock.patch.object(tp, "unit_timeout", lambda u: 3.0), \
                contextlib.redirect_stdout(out):
            result = tp.run_unit(sys.executable, unit, dict(os.environ, PYTHONUNBUFFERED="1"), None)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        line = "TIMEOUT: tests.test_hang after 3s; last test line: test_hangs (tests.test_hang.Hang.test_hangs)"
        self.assertIn(line, out.getvalue())
        self.assertIn(line, result.output)


class UiQueueTests(Temp):
    def lock(self, lane):
        with mock.patch.dict(os.environ, {"CONV_LANE": lane}):
            lock = tp.UiLock(self.dir, "test")
        self.addCleanup(lock.release)
        return lock

    def test_int_lanes_go_first_then_fifo(self):
        holder, plain, integration = self.lock("FOO"), self.lock("BAR"), self.lock("INT9")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(holder.try_acquire())
            self.assertFalse(plain.try_acquire())
            self.assertFalse(integration.try_acquire())
            holder.release()
            plain.told = None
            self.assertFalse(plain.try_acquire())
            self.assertTrue(integration.try_acquire())
            integration.release()
            self.assertTrue(plain.try_acquire())
        self.assertIn(f"queued behind INT9/{os.getpid()}", out.getvalue())
        plain.release()
        self.assertEqual(json.loads((self.dir / "ui-queue.json").read_text(encoding="utf-8")), [])

    def test_a_reused_pid_is_no_holder(self):
        stale = {"lane": "OLD", "pid": os.getpid(), "created": 1, "started_at": "x"}
        (self.dir / "ui.json").write_text(json.dumps(stale), encoding="utf-8")
        with mock.patch.object(tp, "_created", lambda pid: 42):
            lock = self.lock("FOO")
            self.assertTrue(lock.try_acquire())
            self.assertEqual((self.dir / "ui.json").read_text(encoding="utf-8"), lock.record)

    def test_a_gone_holder_is_taken_over(self):
        gone = {"lane": "FGT", "pid": 999991, "created": None, "started_at": "2026-10-18T14:56:00+00:00"}
        (self.dir / "ui.json").write_text(json.dumps(gone), encoding="utf-8")
        with mock.patch.object(tp, "pid_alive", lambda pid: pid != 999991):
            lock = self.lock("FOO")
            self.assertTrue(lock.try_acquire())
        self.assertEqual((self.dir / "ui.json").read_text(encoding="utf-8"), lock.record)

    def test_the_hold_is_stamped_when_taken_not_when_the_run_began(self):
        with mock.patch.object(tp, "_now", lambda: datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)):
            lock = self.lock("FOO")
        with mock.patch.object(tp, "_now", lambda: NOW):
            self.assertTrue(lock.try_acquire())
        rec = json.loads((self.dir / "ui.json").read_text(encoding="utf-8"))
        self.assertEqual((rec["started_at"], rec["heartbeat"]), ("2026-10-18T16:36:00+00:00",) * 2)
        self.assertEqual(json.dumps(rec), lock.record)

    def test_a_waiter_is_told_who_holds_the_lock_and_for_how_long(self):
        """TESTLOCK: GMAILMOVE waited 1h40m behind FGT without being told how long."""
        rec = {"lane": "FGT", "pid": os.getpid(), "created": None, "started_at": "2026-10-18T14:56:00+00:00"}
        cases = [("2026-10-18T14:56:00+00:00", " for 1h40m since 2026-10-18T14:56:00+00:00"),
                 ("2026-10-18T17:00:00+00:00", " since 2026-10-18T17:00:00+00:00"),  # ahead of the clock: no length
                 ("x", " since x"), ("", " since \n"), (None, " since None"), (5, " since 5"), ("missing", " since None")]
        lock = self.lock("GMAILMOVE")
        for started_at, tail in cases:
            rec.pop("started_at", None)
            if started_at != "missing":
                rec["started_at"] = started_at
            (self.dir / "ui.json").write_text(json.dumps(rec), encoding="utf-8")
            out, lock.told = io.StringIO(), None
            with mock.patch.object(tp, "_now", lambda: NOW), contextlib.redirect_stdout(out):
                self.assertFalse(lock.try_acquire())
            self.assertIn(f"waiting for the UI test lock held by FGT/{os.getpid()}{tail}", out.getvalue())

    def test_held_for(self):
        with mock.patch.object(tp, "_now", lambda: NOW):
            for started_at, want in (("2026-10-18T16:00:00Z", " for 36m"), ("2026-10-18T14:56:00", " for 1h40m"),
                                     ("2026-10-18T18:36:00+02:00", " for 0m"), ("2026-10-17T16:36:00Z", " for 24h00m"),
                                     ("2026-10-18T16:37:00+00:00", ""), ("9999-12-31T23:59:59", ""),
                                     ("", ""), (None, ""), (1.5, ""), (b"2026-10-18", ""), ({}, "")):
                self.assertEqual(tp._held_for(started_at), want, started_at)

    def test_a_full_suite_holder_is_told_for_how_long(self):
        suite = {"lane": "FGT", "session": "another-session", "started_at": "2026-10-18T15:36:00+00:00",
                 "command": "x", "heartbeat": "2026-10-18T16:35:00+00:00"}
        (self.dir / "suite.json").write_text(json.dumps(suite), encoding="utf-8")
        with mock.patch.object(tp, "_now", lambda: NOW):
            self.assertEqual(self.lock("FOO")._suite_holder(), "FGT (full suite) for 1h00m since 2026-10-18T15:36:00+00:00")


#: TESTLOCK: the frozen clock of the lock message tests (FGT's run had held the lock 1h40m by then).
NOW = datetime(2026, 10, 18, 16, 36, tzinfo=timezone.utc)
ARGS = argparse.Namespace(write_durations="", report="", durations=0, python=sys.executable)


def units(lane: str) -> list:
    return [tp.Unit(f"{lane}_{kind}{i}", [""], [f"tests.{lane}_{kind}{i}.T.test"], 1.0, kind == "ui")
            for kind in ("ui", "plain") for i in range(3)]


class LockScopeTests(Temp):
    """TESTLOCK: the machine-wide UI lock covers a run's UI units only; its non-UI units go first, without it."""

    def setUp(self):
        super().setUp()
        self.locks, self.seen, self.guard, self.plain_done = {}, [], threading.Lock(), threading.Event()
        for patch in (mock.patch.object(tp, "run_unit", self.fake), mock.patch.object(tp, "worker_env", lambda i, bases: {})):
            patch.start()
            self.addCleanup(patch.stop)

    def lock(self, lane):
        with mock.patch.dict(os.environ, {"CONV_LANE": lane}):
            lock = tp.UiLock(self.dir, "test")
        lock.poll = 0.01
        self.locks[lane] = lock
        self.addCleanup(lock.release)
        return lock

    def ui_json(self):
        path = self.dir / "ui.json"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def fake(self, python, unit, env, durations):
        lock = self.locks[unit.module.split("_")[0]]
        with self.guard:
            self.seen.append(("start", unit.module, lock.held, self.ui_json()))
        time.sleep(0.01)
        with self.guard:
            self.seen.append(("end", unit.module, lock.held, self.ui_json()))
            if sum(e[0] == "end" and "plain" in e[1] for e in self.seen) == 3:
                self.plain_done.set()
        return tp.Result(unit, [], 0, "", 0.0)

    def start(self, lane, ui_workers, workers):
        done = []
        thread = threading.Thread(target=lambda: done.append(tp.execute(ARGS, units(lane), ui_workers, workers, self.locks[lane])))
        thread.start()
        return thread, done

    def ui_events(self, lane):
        return [(i, e) for i, e in enumerate(self.seen) if e[1].startswith(f"{lane}_ui")]

    def test_non_ui_units_run_while_another_run_holds_the_lock(self):
        holder, run = self.lock("HOLD"), self.lock("A")
        self.assertTrue(holder.try_acquire())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            thread, done = self.start("A", 1, 1)
            self.assertTrue(self.plain_done.wait(10))
            time.sleep(0.1)  # time enough to start a UI unit under HOLD's lock, were that possible
            self.assertEqual(self.ui_events("A"), [])
            holder.release()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(done[0][0]), 6)
        plain = [e for e in self.seen if "plain" in e[1]]
        self.assertEqual(len(plain), 6)
        self.assertTrue(all(not held and text == holder.record for _, _, held, text in plain), plain)
        ui = self.ui_events("A")
        self.assertEqual(len(ui), 6)
        self.assertTrue(all(held and text == run.record for _, (_, _, held, text) in ui), ui)
        self.assertLess(max(i for i, e in enumerate(self.seen) if "plain" in e[1]), ui[0][0])
        self.assertIn(f"waiting for the UI test lock held by HOLD/{os.getpid()}", out.getvalue())
        self.assertFalse(run.held)
        self.assertEqual(self.ui_json(), "")  # given back as the last UI unit ended

    def test_two_runs_hold_the_lock_in_turn_for_their_ui_units_only(self):
        a, b = self.lock("A"), self.lock("B")
        with contextlib.redirect_stdout(io.StringIO()):
            runs = [self.start("A", 2, 1), self.start("B", 2, 1)]
            for thread, _ in runs:
                thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread, _ in runs))
        self.assertEqual([len(done[0][0]) for _, done in runs], [6, 6])
        for lane, lock in (("A", a), ("B", b)):
            ui = self.ui_events(lane)
            self.assertEqual(len(ui), 6)
            self.assertTrue(all(held and text == lock.record for _, (_, _, held, text) in ui), ui)
        (a_first, _), (a_last, _) = self.ui_events("A")[0], self.ui_events("A")[-1]
        (b_first, _), (b_last, _) = self.ui_events("B")[0], self.ui_events("B")[-1]
        self.assertTrue(a_last < b_first or b_last < a_first, self.seen)
        self.assertEqual(self.ui_json(), "")
        self.assertEqual(json.loads((self.dir / "ui-queue.json").read_text(encoding="utf-8")), [])


class PoolShareTests(Temp):
    """TESTLOCK: a run takes at most ceil(budget / active runs) tokens, and gives one back as each worker ends."""

    OTHER = 999991

    def setUp(self):
        super().setUp()
        self.me = str(os.getpid())
        patch = mock.patch.object(tp, "pid_alive", lambda pid: pid == os.getpid() or pid >= self.OTHER)
        patch.start()
        self.addCleanup(patch.stop)

    def others(self, *tokens):
        (self.dir / "pool.json").write_text(json.dumps({str(self.OTHER + i): t for i, t in enumerate(tokens)}), encoding="utf-8")

    def held(self):
        return json.loads((self.dir / "pool.json").read_text(encoding="utf-8"))

    def test_fair_share(self):
        for others, budget, want, got in (((), 8, 12, 8), ((), 8, 3, 3), ((2,), 8, 8, 4), ((1, 1, 1), 8, 8, 2),
                                          ((7,), 8, 8, 1), ((1,), 3, 3, 2), ((2,), 9, 9, 5), ((8,), 8, 8, 0)):
            self.others(*others)
            self.assertEqual(tp.TestPool(self.dir, budget)._take(want), got, (others, budget, want))
            mine = {self.me: got} if got else {}
            self.assertEqual(self.held(), {**{str(self.OTHER + i): t for i, t in enumerate(others)}, **mine})

    def test_a_gone_runs_tokens_are_reclaimed(self):
        self.others(6)
        with mock.patch.object(tp, "pid_alive", lambda pid: pid == os.getpid()):
            self.assertEqual(tp.TestPool(self.dir, 8)._take(8), 8)
        self.assertEqual(self.held(), {self.me: 8})

    def test_tokens_go_back_as_workers_end_the_last_at_release(self):
        self.others(2)
        pool = tp.TestPool(self.dir, 8)
        self.assertEqual(pool.acquire(3), 3)
        pool.give_back()
        self.assertEqual((pool.held, self.held()), (2, {str(self.OTHER): 2, self.me: 2}))
        pool.give_back()
        pool.give_back()
        self.assertEqual((pool.held, self.held()), (1, {str(self.OTHER): 2, self.me: 1}))
        pool.release()
        self.assertEqual(self.held(), {str(self.OTHER): 2})

    def test_execute_gives_back_all_but_one(self):
        pool = tp.TestPool(self.dir, 8)
        self.assertEqual(pool.acquire(3), 3)
        fake = lambda python, unit, env, durations: tp.Result(unit, [], 0, "", 0.0)  # noqa: E731
        with mock.patch.object(tp, "run_unit", fake), mock.patch.object(tp, "worker_env", lambda i, bases: {}):
            results = tp.execute(ARGS, units("A"), 1, 2, pool=pool)[0]
        self.assertEqual(len(results), 6)
        self.assertEqual((pool.held, self.held()), (1, {self.me: 1}))
        pool.release()

    def test_pool_for_asks_for_a_fair_share(self):
        self.others(2)
        with mock.patch.object(tp, "lock_dir", lambda: self.dir), mock.patch.object(tp, "pool_budget", lambda folder: 8), \
                mock.patch.dict(os.environ, {"CI": ""}):
            pool, ui_workers, workers = tp.pool_for(4, 6)
        self.assertEqual((pool.held, ui_workers, workers), (4, 1, 3))
        pool.release()


class PlanningTests(unittest.TestCase):
    def test_an_unrecorded_class_counts_as_the_module_median(self):
        discovery = {"modules": {"test_m": {"split": True, "classes": {
            c: [f"tests.test_m.{c}.test_a"] for c in ("A", "B", "C")}}}}
        units = tp.build_units(discovery, set(), {"tests.test_m.A": 40.0, "tests.test_m.B": 50.0})
        self.assertEqual(len(units), 2)
        self.assertEqual(sum(u.seconds for u in units), 135.0)

    def test_a_module_with_no_record_stays_one_unknown_unit(self):
        discovery = {"modules": {"test_m": {"split": True, "classes": {"A": ["tests.test_m.A.test_a"]}}}}
        [unit] = tp.build_units(discovery, set(), {})
        self.assertIsNone(unit.seconds)


class LoadSensitiveTests(Temp):
    def run_with(self, text):
        state = self.dir / "STATE.md"
        if text is not None:
            state.write_text(text, encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(tp, "STATE", state), contextlib.redirect_stdout(out):
            return tp.load_sensitive(), out.getvalue()

    def test_count_and_source_are_printed(self):
        ids, out = self.run_with("- Load-sensitive list: test_a.K.test_x, test_b.K.test_y\n")
        self.assertEqual(len(ids), 2)
        self.assertIn("load-sensitive list: 2 ids from", out)
        self.assertNotIn("WARNING", out)

    def test_empty_or_missing_is_a_warning(self):
        self.assertIn("WARNING: load-sensitive list: 0 ids", self.run_with("- Load-sensitive list: none\n")[1])
        self.assertIn("WARNING: load-sensitive list:", self.run_with(None)[1])


class SkipTests(unittest.TestCase):
    def test_a_setupclass_skip_is_its_class(self):
        output = ("test_a (tests.test_m.Other.test_a) ... ok\n"
                  "setUpClass (tests.test_m.Gated) ... skipped 'no display'\n")
        self.assertEqual(tp._skips(output), [["tests.test_m.Gated", "no display"]])


class TripwireTests(Temp):
    def test_a_changed_db_file_is_seen(self):
        db = self.dir / "jarvis.db-wal"
        db.write_bytes(b"x")
        before = tp.db_snapshot(self.dir)
        self.assertEqual(before, tp.db_snapshot(self.dir))
        db.write_bytes(b"xy")
        self.assertNotEqual(before, tp.db_snapshot(self.dir))


if __name__ == "__main__":
    unittest.main()
