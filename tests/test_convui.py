"""CONVUI: scripts/test_parallel.py runs the UI units of one run at a time on this machine
(build/locks/ui.json). Every test uses its own temp lock folder and fake units (run_unit is
replaced), so no test module runs and the real build/locks is never touched."""

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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "test_parallel", Path(__file__).resolve().parents[1] / "scripts" / "test_parallel.py")
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)

ARGS = argparse.Namespace(write_durations="", report="", durations=0, python=sys.executable)


def unit(name: str, ui: bool):
    return tp.Unit(name, [""], [f"tests.{name}.T.test"], 1.0, ui)


class UiLockTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jarvis-convui-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.events = []

    def lock(self, name: str):
        lock = tp.UiLock(self.dir, name)
        lock.poll = 0.05
        return lock

    def text(self, name: str = "ui.json") -> str:
        return (self.dir / name).read_text(encoding="utf-8")

    def start(self, target) -> threading.Thread:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def test_second_run_ui_starts_after_first_releases_and_its_plain_part_runs_meanwhile(self):
        go_first, first_ui_started, plain_done = threading.Event(), threading.Event(), threading.Event()

        def fake(python, u, env, durations):
            self.events.append(("start", u.module, time.monotonic()))
            if u.module == "first_ui":
                first_ui_started.set()
                go_first.wait(10)
            if u.module == "second_plain":
                plain_done.set()
            self.events.append(("end", u.module, time.monotonic()))
            return u.module

        out = io.StringIO()
        with mock.patch.object(tp, "run_unit", fake), mock.patch.object(tp, "worker_env", lambda i, b: {}), \
                contextlib.redirect_stdout(out):
            first = self.start(lambda: tp.execute(ARGS, [unit("first_ui", True)], 1, 1, self.lock("first")))
            self.assertTrue(first_ui_started.wait(10))
            second = self.start(lambda: tp.execute(
                ARGS, [unit("second_ui", True), unit("second_plain", False)], 1, 1, self.lock("second")))
            self.assertTrue(plain_done.wait(10))
            time.sleep(0.3)
            self.assertNotIn("second_ui", [m for _, m, _ in self.events])
            go_first.set()
            first.join(10)
            second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        at = {(kind, m): t for kind, m, t in self.events}
        self.assertGreaterEqual(at[("start", "second_ui")], at[("end", "first_ui")])
        self.assertIn("waiting for the UI test lock held by ", out.getvalue())
        self.assertIn(f"/{os.getpid()} since ", out.getvalue())
        self.assertEqual(self.text(), "")

    def test_a_live_holder_blocks_and_a_dead_holder_is_taken_over(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(holder.kill)
        (self.dir / "ui.json").write_text(json.dumps({"lane": "other", "pid": holder.pid, "started_at": "t"}), encoding="utf-8")
        self.assertTrue(tp.pid_alive(holder.pid))
        lock = self.lock("mine")
        waiter = self.start(lock.acquire)
        waiter.join(0.5)
        self.assertTrue(waiter.is_alive())
        self.assertFalse(lock.held)
        holder.kill()
        holder.wait(10)
        self.assertFalse(tp.pid_alive(holder.pid))
        waiter.join(10)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(lock.held)
        self.assertEqual(self.text(), lock.record)
        lock.release()
        self.assertEqual(self.text(), "")

    def test_ci_or_no_ui_lock_or_no_ui_units_take_no_lock(self):
        ui, plain = [unit("a_ui", True)], [unit("a_plain", False)]
        with mock.patch.object(tp, "lock_dir", lambda: self.dir):
            with mock.patch.dict(os.environ, {"CI": "true"}):
                self.assertIsNone(tp.ui_lock_for(argparse.Namespace(no_ui_lock=False), ui))
            env = {k: v for k, v in os.environ.items() if k != "CI"}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertIsNone(tp.ui_lock_for(argparse.Namespace(no_ui_lock=True), ui))
                self.assertIsNone(tp.ui_lock_for(argparse.Namespace(no_ui_lock=False), plain))
                lock = tp.ui_lock_for(argparse.Namespace(no_ui_lock=False), ui)
        self.assertIsInstance(lock, tp.UiLock)
        self.assertEqual(lock.path, self.dir / "ui.json")

    def test_an_error_inside_a_ui_run_releases_the_lock(self):
        def fake(python, u, env, durations):
            raise RuntimeError("worker broke")

        lock = self.lock("broken")
        with mock.patch.object(tp, "run_unit", fake), mock.patch.object(tp, "worker_env", lambda i, b: {}):
            with self.assertRaises(RuntimeError):
                tp.execute(ARGS, [unit("b_ui", True), unit("b_ui2", True)], 1, 1, lock)
        self.assertFalse(lock.held)
        self.assertEqual(self.text(), "")
        after = self.lock("after")
        taker = self.start(after.acquire)
        taker.join(5)
        self.assertTrue(after.held)

    def test_another_sessions_full_suite_holds_the_ui_lock_but_our_own_does_not(self):
        now = datetime.now(timezone.utc).isoformat()
        record = {"lane": "DFX", "session": "other-session", "started_at": now, "command": "x", "heartbeat": now}
        (self.dir / "suite.json").write_text(json.dumps(record), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "my-session"}):
            lock = self.lock("mine")
            own = self.lock("own")
        self.assertIn("DFX (full suite) since ", lock._suite_holder())
        waiter = self.start(lock.acquire)
        waiter.join(0.5)
        self.assertTrue(waiter.is_alive())
        (self.dir / "suite.json").write_text(json.dumps(dict(record, session="my-session")), encoding="utf-8")
        self.assertIsNone(own._suite_holder())
        waiter.join(10)
        self.assertTrue(lock.held)
        lock.release()
        stale = "2020-01-01T00:00:00Z"
        (self.dir / "suite.json").write_text(json.dumps(dict(record, heartbeat=stale)), encoding="utf-8")
        self.assertIsNone(lock._suite_holder())


if __name__ == "__main__":
    unittest.main()
