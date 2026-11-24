"""N6SENSE1: read-only RAM, commit and disk sensors feed the Situation hold.

Readers are injected; no clock, no network, no real process is touched.
"""

import math
import re
import unittest
from pathlib import Path
from unittest import mock

from jarvis import n6_sensors, work_supervisor
from jarvis.n6_sensors import Reading, margin_ok, memory_situation, sense
from jarvis.work_supervisor import Policy, Situation, supervise

ROOT = Path(__file__).resolve().parents[1]
THEN = Policy(90.0, 3.0, (2, 8), False, 3.0, 1.5)
GOOD = (16.0, 40.0)


def _boom():
    raise OSError("unreadable")


def _decide(reading, held=False, ok_for_s=0):
    return supervise(Situation.from_dict({
        "operator_pause": False, "policy": THEN,
        "memory": memory_situation(reading, held, ok_for_s),
        "quota": {"used_pct": 10, "line_pct": 10, "hour": 12}}))


class Port:
    """Process port stand-in: records every call; kill/terminate must never run."""

    def __init__(self, ram, disk):
        self.calls, self._ram, self._disk = [], ram, disk

    def read_ram(self):
        self.calls.append("read_ram")
        return self._ram() if callable(self._ram) else self._ram

    def read_disk(self):
        self.calls.append("read_disk")
        return self._disk() if callable(self._disk) else self._disk

    def kill(self, *a):
        self.calls.append("kill")

    def terminate(self, *a):
        self.calls.append("terminate")


class SensorReadingTests(unittest.TestCase):
    def test_reading_is_frozen_and_carries_three_values(self):
        r = sense(ram=lambda: GOOD, disk=lambda: 50.0)
        self.assertEqual(r, Reading(16.0, 40.0, 50.0))
        with self.assertRaises(Exception):
            r.free_ram_gb = 1.0

    def test_raising_or_unknown_ram_is_none(self):
        for ram in (_boom, lambda: None, lambda: (1.0,), lambda: "x"):
            r = sense(ram=ram, disk=lambda: 50.0)
            self.assertEqual((r.free_ram_gb, r.commit_pct), (None, None), ram)

    def test_unreadable_disk_is_none(self):
        for disk in (_boom, lambda: None):
            self.assertIsNone(sense(ram=lambda: GOOD, disk=disk).free_disk_gb)

    def test_bad_values_are_unknown(self):
        for bad in (-1, -0.0001, float("nan"), float("inf"), float("-inf"), True, False, "5"):
            r = sense(ram=lambda: (bad, bad), disk=lambda: bad)
            self.assertEqual(r, Reading(None, None, None), bad)
        self.assertIsNone(sense(ram=lambda: (8.0, 100.5), disk=lambda: 50).commit_pct)

    def test_default_readers_are_stdlib_and_unknown_off_windows(self):
        with mock.patch.object(n6_sensors.sys, "platform", "linux"):
            self.assertIsNone(n6_sensors.read_ram())
        with mock.patch.object(n6_sensors.shutil, "disk_usage",
                               return_value=mock.Mock(free=12e9)) as du:
            self.assertEqual(n6_sensors.read_disk(), 12.0)
        du.assert_called_once_with(n6_sensors.ROOT)

    def test_memory_situation_rejects_non_reading(self):
        with self.assertRaises(ValueError):
            memory_situation({"free_ram_gb": 8}, False)


class SensorHoldTests(unittest.TestCase):
    def test_unknown_ram_waits_never_ok(self):
        for ram in (_boom, lambda: None, lambda: (float("nan"), 10.0)):
            for held, ok in ((False, 0), (True, 600)):
                d = _decide(sense(ram=ram, disk=lambda: 50.0), held, ok)
                self.assertEqual((d.action, d.rule_id), ("wait", "mem.unknown"))

    def test_unknown_commit_waits(self):
        d = _decide(sense(ram=lambda: (16.0, float("nan")), disk=lambda: 50.0))
        self.assertEqual((d.action, d.rule_id), ("wait", "mem.commit_unknown"))

    def test_low_ram_or_high_commit_waits(self):
        self.assertEqual(_decide(sense(ram=lambda: (2.9, 10.0), disk=lambda: 50.0)).rule_id,
                         "mem.hold")
        d = _decide(sense(ram=lambda: (64.0, 90.1), disk=lambda: 500.0))
        self.assertEqual((d.action, d.rule_id), ("wait", "mem.commit"))

    def test_disk_low_or_unreadable_holds(self):
        self.assertEqual(_decide(sense(ram=lambda: GOOD, disk=lambda: 9.5)).rule_id,
                         "disk.hold")
        self.assertEqual(_decide(sense(ram=lambda: GOOD, disk=_boom)).rule_id, "disk.unknown")

    def test_ram_just_over_minimum_after_hold_stays_held(self):
        r = sense(ram=lambda: (3.1, 10.0), disk=lambda: 50.0)
        for ok in (0, 300, 6000):
            self.assertEqual(_decide(r, True, ok).rule_id, "mem.hysteresis")
        r = sense(ram=lambda: (4.5, 10.0), disk=lambda: 50.0)
        self.assertEqual(_decide(r, True, 299).rule_id, "mem.hysteresis")
        self.assertEqual(_decide(r, True, 300).action, "dispatch")

    def test_healthy_reading_dispatches(self):
        self.assertEqual(_decide(sense(ram=lambda: GOOD, disk=lambda: 50.0)).action, "dispatch")

    def test_commit_dip_restarts_the_release_timer(self):
        # Ticks every 10 s (injected, no clock); ok_since restarts when margin_ok is False.
        ok_since, held, out = 0, True, []
        for t in range(0, 900, 10):
            commit = 95.0 if t < 300 else 85.0      # RAM stays at 8 GB throughout
            r = sense(ram=lambda: (8.0, commit), disk=lambda: 50.0)
            if not margin_ok(r, THEN):
                ok_since = t
            d = _decide(r, held, t - ok_since)
            held = d.action != "dispatch"
            out.append((t, d.rule_id if held else "dispatch"))
        self.assertTrue(all(rule == "mem.commit" for t, rule in out if t < 300))
        self.assertTrue(all(rule == "mem.hysteresis" for t, rule in out if 300 <= t < 590))
        self.assertEqual(dict(out)[590], "dispatch")
        with self.assertRaises(ValueError):
            margin_ok({"free_ram_gb": 8.0})

    def test_no_hold_path_kills_or_terminates(self):
        # sense() is handed only the read side of the port; the recorder proves
        # kill/terminate are never reached on any hold path.
        cases = ((_boom, 50.0), ((2.0, 10.0), 50.0), ((64.0, 95.0), 50.0),
                 (GOOD, 1.0), (GOOD, _boom), ((3.1, 10.0), 50.0))
        for ram, disk in cases:
            port = Port(ram, disk)
            d = _decide(sense(ram=port.read_ram, disk=port.read_disk), held=True)
            self.assertEqual(d.action, "wait", (ram, disk))
            self.assertEqual(port.calls, ["read_ram", "read_disk"])

    def test_module_has_no_process_control(self):
        src = (ROOT / "jarvis" / "n6_sensors.py").read_text(encoding="utf-8")
        for word in ("kill", "terminate", "TerminateProcess", "subprocess", "signal", "Popen"):
            self.assertNotIn(word, src.replace("never starts, stops or signals", ""), word)


class SensorShadowAndParityTests(unittest.TestCase):
    def test_nothing_in_the_app_imports_the_sensors(self):
        users = [p.relative_to(ROOT).as_posix() for p in (ROOT / "jarvis").rglob("*.py")
                 if p.name != "n6_sensors.py"
                 and "n6_sensors" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(users, [])

    def test_limits_match_conveyor_text(self):
        text = (ROOT / "scripts" / "conveyor.py").read_text(encoding="utf-8")
        for pattern, ours in ((r'"min_free_mem_gb":\s*([\d.]+)', work_supervisor.MEM_LOW_GB),
                              (r'"max_commit_pct":\s*([\d.]+)', work_supervisor.MAX_COMMIT_PCT),
                              (r"(?m)^MIN_FREE_GB = ([\d.]+)", work_supervisor.MIN_FREE_GB),
                              (r"(?m)^MEM_MARGIN_GB = ([\d.]+)", work_supervisor.MEM_MARGIN_GB)):
            found = re.findall(pattern, text)
            self.assertEqual(len(found), 1, pattern)
            self.assertEqual(float(found[0]), float(ours), pattern)
        found = re.findall(r"(?m)^MEM_OK_S = ([\d *]+?)\s*$", text)   # "5 * 60"
        self.assertEqual(len(found), 1)
        self.assertEqual(math.prod(int(n) for n in found[0].split("*")),
                         work_supervisor.MEM_OK_S)
        self.assertEqual(work_supervisor.DEFAULT_POLICY.mem_low_gb, work_supervisor.MEM_LOW_GB)
        self.assertEqual(work_supervisor.DEFAULT_POLICY.mem_margin_gb,
                         work_supervisor.MEM_MARGIN_GB)


if __name__ == "__main__":
    unittest.main()
