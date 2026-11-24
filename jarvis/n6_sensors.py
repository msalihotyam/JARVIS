"""N6 read-only host sensors: free RAM, commit charge and free disk (shadow).

Readers are injectable callables; the defaults use the stdlib only (ctypes
GlobalMemoryStatusEx on Windows, shutil.disk_usage). Off Windows the RAM
reading is unknown. Every bad value (raise, None, bool, NaN, inf, negative,
commit outside 0..100) becomes None, and memory_hold turns None into a wait.
This module reads; it never starts, stops or signals a process. Nothing in the
running app imports it yet.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from jarvis.work_supervisor import DEFAULT_POLICY, MEM_OK_S, _num, memory_hold

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Reading:
    free_ram_gb: float | None
    commit_pct: float | None
    free_disk_gb: float | None


def _gb(value) -> float | None:
    value = _num(value)
    return None if value is None or value < 0 else value


def _pct(value) -> float | None:
    value = _num(value)
    return None if value is None or not 0 <= value <= 100 else value


def read_ram():
    """(free physical GB, commit charge %) or None; same math as conveyor real_mem."""
    if sys.platform != "win32":
        return None
    import ctypes

    class MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                   [(n, ctypes.c_ulonglong) for n in (
                       "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                       "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]
    m = MemStatus(dwLength=ctypes.sizeof(MemStatus))
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    return m.ullAvailPhys / 1e9, 100 * (1 - m.ullAvailPageFile / m.ullTotalPageFile)


def read_disk(path=ROOT):
    return shutil.disk_usage(path).free / 1e9


def sense(ram=read_ram, disk=read_disk) -> Reading:
    """One reading; a reader that raises or returns junk gives None for its fields."""
    try:
        free, commit = ram()
    except Exception:   # unreadable, None (not a pair) or a raising reader: unknown
        free = commit = None
    try:
        disk_gb = disk()
    except Exception:
        disk_gb = None
    return Reading(_gb(free), _pct(commit), _gb(disk_gb))


def margin_ok(reading: Reading, policy=DEFAULT_POLICY) -> bool:
    """True only when RAM is at low + margin AND commit and disk are within limits.
    A False reading clears the release timer (see release_timer), so a commit or
    disk dip also restarts the full MEM_OK_S release wait."""
    if type(reading) is not Reading:
        raise ValueError("reading must be a Reading")
    return memory_hold(policy, reading.free_ram_gb, True, MEM_OK_S,
                       commit_pct=reading.commit_pct,
                       disk_free_gb=reading.free_disk_gb).action == "dispatch"


def release_timer(ok_since, reading: Reading, now, policy=DEFAULT_POLICY):
    """(ok_since, ok_for_s) after one reading taken at `now` (caller's clock).
    ok_since is the time of the FIRST margin_ok() reading since the last one that
    was not ok (None while not ok), so the unobserved gap before the first healthy
    sample never counts toward MEM_OK_S (same as conveyor mem_ok_since)."""
    t, start = _num(now), ok_since if ok_since is None else _num(ok_since)
    if t is None or (ok_since is not None and start is None):
        raise ValueError("now and ok_since must be finite numbers")
    if not margin_ok(reading, policy):
        return None, 0
    start = t if start is None else start
    return start, t - start   # a clock that went back gives < 0: still held


def memory_situation(reading: Reading, held: bool, ok_for_s=0) -> dict:
    """Situation.memory for supervise(): always carries all three readings.
    ok_for_s = now - first healthy sample since the last unhealthy one (release_timer);
    never the time since the last unhealthy sample."""
    if type(reading) is not Reading:
        raise ValueError("reading must be a Reading")
    return {"free_gb": reading.free_ram_gb, "held": held, "ok_for_s": ok_for_s,
            "commit_pct": reading.commit_pct, "disk_free_gb": reading.free_disk_gb}
