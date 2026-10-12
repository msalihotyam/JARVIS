"""DFSINGLE: one JARVIS at a time; a second launch brings the running one to the front.

Every test uses its own lock name, never the real per-user one, and no test
opens a store: the winner in the end-to-end test runs on throwaway state.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import tests  # noqa: F401 - the live-database guard
from jarvis.memory import SQLiteMemoryStore
from jarvis.ui import app as ui_app
from jarvis.ui import single_instance

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT)}
ENV.pop("TOMORROW_API_KEY", None)


def unique_name() -> str:
    return f"jarvis-test-{os.getpid()}-{uuid.uuid4().hex}"


#: Takes the lock, says WON or LOST, and keeps it until killed.
HOLD = r'''
import sys, time
from jarvis.ui import single_instance
start = float(sys.argv[2])
while time.time() < start:
    time.sleep(0.005)
print("WON" if single_instance.acquire(sys.argv[1]) else "LOST", flush=True)
time.sleep(120)
'''

#: The real ``main`` on throwaway state as the winner: minimized once it is up,
#: then it reports what the tray's ``open`` did when a later launch asked.
WINNER = r'''
import tests
import os, sys, tempfile, threading
watchdog = threading.Timer(90, os._exit, (3,))   # never hang the suite
watchdog.daemon = True
watchdog.start()
from dataclasses import replace
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QTimer
from jarvis.settings import ConfigStore
from jarvis.ui import app as ui_app, tray
tmp = Path(tempfile.mkdtemp(prefix="jarvis-dfsingle-test-"))
store = ConfigStore(tmp / "config.json"); store.mutate(lambda s: replace(s, memory_database=tmp / "jarvis.db"))
Backend, Prefs, engine_of = ui_app.Backend, ui_app.UiPreferences, ui_app.create_engine
ui_app.Backend = lambda: Backend(store)
Backend.provider_line = lambda self, pid: {"id": pid, "status": "not_installed", "models": []}
ui_app.UiPreferences = lambda: Prefs(tmp / "prefs.json")
say = lambda what: os.write(2, (what + "\n").encode())
held = []
def create_engine(bridge, *, icon=None):
    engine = engine_of(bridge, icon=icon)
    window = engine.rootObjects()[0]
    held.append(window)
    def ready():
        window.showMinimized()
        say("READY")
    QTimer.singleShot(800, ready)
    QTimer.singleShot(60000, QCoreApplication.quit)
    return engine
opened = tray.TrayController.open
def open(self):
    before = self.window.visibility()
    opened(self)
    say("OPEN %s -> %s" % (before.name, self.window.visibility().name))
    QTimer.singleShot(300, QCoreApplication.quit)
tray.TrayController.open = open
ui_app.create_engine = create_engine
raise SystemExit(ui_app.main([], instance=sys.argv[1]))
'''


def child(script: str, *args: str, **kwargs) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", script, *args], cwd=ROOT, env=ENV, text=True,
                            encoding="utf-8", errors="replace", **kwargs)


def lose(name: str, *argv, **kwargs) -> int:
    """``main`` while ``name`` is held: it must end before the UI or a store exists."""
    with mock.patch.object(single_instance, "NOTIFY_TIMEOUT_MS", 300), \
            mock.patch("PySide6.QtWidgets.QApplication") as application, \
            mock.patch.object(ui_app, "Backend", side_effect=AssertionError("Backend")) as backend, \
            mock.patch.object(ui_app, "UiPreferences") as prefs, \
            mock.patch.object(SQLiteMemoryStore, "__init__", side_effect=AssertionError("store")) as store:
        code = ui_app.main(*argv, **kwargs)
    for spy in (application, backend, prefs, store):
        spy.assert_not_called()
    return code


class LosingLaunchTests(unittest.TestCase):
    def test_a_second_launch_exits_0_without_building_anything(self) -> None:
        name = unique_name()
        held = single_instance.acquire(name)
        self.addCleanup(single_instance.release, held)
        self.assertIsNotNone(held)
        self.assertEqual(0, lose(name, [], instance=name))

    def test_the_launch_claims_the_per_user_name(self) -> None:
        name = unique_name()
        held = single_instance.acquire(name)
        self.addCleanup(single_instance.release, held)
        with mock.patch.object(single_instance, "default_name", return_value=name):
            self.assertEqual(0, lose(name))                # argv None: the process launch
        with mock.patch("os.getlogin", side_effect=["alice", "bob"]):
            alice, bob = single_instance.default_name(), single_instance.default_name()
        self.assertNotEqual(alice, bob)
        self.assertIn("alice", alice)

    def test_a_launch_takes_over_when_the_holder_is_gone_without_answering(self) -> None:
        """A JARVIS that was quitting (lock still held, no longer listening)."""
        name = unique_name()
        held = [single_instance.acquire(name)]
        taken = []
        acquire = single_instance.acquire

        def gone(name, timeout_ms=None):
            single_instance.release(held.pop())
            return False

        def take(name):
            taken.append(acquire(name))
            return taken[-1]
        self.addCleanup(lambda: [single_instance.release(h) for h in held + taken])
        with mock.patch.object(single_instance, "notify_running", side_effect=gone), \
                mock.patch.object(single_instance, "acquire", side_effect=take), \
                mock.patch.object(single_instance, "InstanceServer"), \
                mock.patch("PySide6.QtWidgets.QApplication"), mock.patch("PySide6.QtGui.QIcon"), \
                mock.patch.object(ui_app, "Backend"), mock.patch.object(ui_app, "UiPreferences"), \
                mock.patch.object(ui_app, "Bridge", side_effect=RuntimeError("stop")) as bridge, \
                self.assertRaises(RuntimeError):
            ui_app.main([], instance=name)
        bridge.assert_called_once()                        # it went on as the one JARVIS
        self.assertIsNotNone(taken[-1])

    def test_an_embedded_call_never_takes_the_real_lock(self) -> None:
        with mock.patch.object(single_instance, "acquire") as acquire, \
                mock.patch("PySide6.QtWidgets.QApplication"), mock.patch("PySide6.QtGui.QIcon"), \
                mock.patch.object(ui_app, "Backend"), mock.patch.object(ui_app, "UiPreferences"), \
                mock.patch.object(ui_app, "Bridge", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                ui_app.main([])
        acquire.assert_not_called()


class LockTests(unittest.TestCase):
    def test_a_killed_holder_never_blocks_the_next_start(self) -> None:
        name = unique_name()
        holder = child(HOLD, name, "0", stdout=subprocess.PIPE)
        self.addCleanup(holder.kill)
        self.assertEqual("WON", holder.stdout.readline().strip())
        self.assertIsNone(single_instance.acquire(name))
        holder.kill()
        holder.wait(30)
        holder.stdout.close()
        handle = single_instance.acquire(name)
        self.addCleanup(single_instance.release, handle)
        self.assertIsNotNone(handle)

    def test_simultaneous_starts_end_with_exactly_one_winner(self) -> None:
        name = unique_name()
        start = str(time.time() + 6)
        racers = [child(HOLD, name, start, stdout=subprocess.PIPE) for _ in range(6)]
        for racer in racers:
            self.addCleanup(racer.stdout.close)
            self.addCleanup(racer.wait, 30)
            self.addCleanup(racer.kill)
        said = sorted(racer.stdout.readline().strip() for racer in racers)
        self.assertEqual(["LOST"] * 5 + ["WON"], said)


class SecondLaunchShowsTheRunningWindowTests(unittest.TestCase):
    def test_the_running_jarvis_comes_back_as_the_tray_open_does(self) -> None:
        name = unique_name()
        winner = child(WINNER, name, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        self.addCleanup(winner.kill)
        log = []
        for line in winner.stderr:
            log.append(line)
            if line.startswith("READY"):
                break
        self.assertTrue(log and log[-1].startswith("READY"), "".join(log)[-2000:])
        self.assertEqual(0, lose(name, [], instance=name))
        log.append(winner.stderr.read())                   # to its end (it quits after open, or at 60 s)
        winner.wait(30)
        winner.stderr.close()
        self.assertEqual(0, winner.returncode, "".join(log)[-2000:])
        self.assertIn("OPEN Minimized -> FullScreen", "".join(log))


if __name__ == "__main__":
    unittest.main()
