"""TESTINFRA: scripts/test_parallel.py's unit timeout, UI lock line, median shard planning,
load-sensitive count, setUpClass skip naming and the DB tripwire. Every test uses its own temp
folder; the real build/locks and data/ are never touched."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
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
