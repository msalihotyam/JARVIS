"""Local machine load for the System status card: CPU, memory and disk, read-only.

Standard library only. On Windows the figures come straight from the kernel
(``GetSystemTimes`` and ``GlobalMemoryStatusEx`` through ``ctypes``) and the
system drive's ``shutil.disk_usage``; each read costs microseconds. Nothing is
spawned, nothing is written, nothing leaves the machine, and no model or
provider is involved: this is a gauge, not a capability.

A figure that cannot be read is ``None`` and the sample says so; the interface
then shows nothing for it. There is no estimate and no placeholder.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from typing import Any

WINDOWS = sys.platform == "win32"


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def value(self) -> int:
        return (self.high << 32) | self.low


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32), ("memory_load", ctypes.c_uint32),
        ("total_phys", ctypes.c_uint64), ("avail_phys", ctypes.c_uint64),
        ("total_page", ctypes.c_uint64), ("avail_page", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64), ("avail_virtual", ctypes.c_uint64),
        ("avail_extended", ctypes.c_uint64),
    ]


def _system_times() -> tuple[int, int, int] | None:
    """(idle, kernel, user) in 100 ns ticks since boot, or None off Windows."""
    if not WINDOWS:
        return None
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    try:
        ok = ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    except (AttributeError, OSError):
        return None
    return (idle.value(), kernel.value(), user.value()) if ok else None


def memory_percent() -> int | None:
    """Physical memory in use, as the kernel reports it (0-100), or None."""
    if not WINDOWS:
        return None
    status = _MemoryStatus()
    status.length = ctypes.sizeof(_MemoryStatus)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None
    return int(status.memory_load) if ok else None


def disk_percent(path: str | None = None) -> int | None:
    """Space used on the system drive (0-100), or None when it cannot be read."""
    root = path or (os.environ.get("SystemDrive", "C:") + "\\" if WINDOWS else os.sep)
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return None
    return round(100 * usage.used / usage.total) if usage.total else None


class Sampler:
    """CPU load needs two readings; this keeps the previous one between ticks."""

    def __init__(self) -> None:
        self._last = _system_times()

    def cpu_percent(self) -> int | None:
        now = _system_times()
        if now is None or self._last is None:
            self._last = now
            return None
        idle = now[0] - self._last[0]
        busy = (now[1] - self._last[1]) + (now[2] - self._last[2])     # kernel time includes idle
        self._last = now
        if busy <= 0:
            return None
        return max(0, min(100, round(100 * (1 - idle / busy))))

    def sample(self) -> dict[str, Any]:
        """One reading. ``available`` is True only when every figure was read."""
        cpu, ram, disk = self.cpu_percent(), memory_percent(), disk_percent()
        return {"available": None not in (cpu, ram, disk), "cpu": cpu, "ram": ram, "disk": disk}


__all__ = ["Sampler", "disk_percent", "memory_percent"]
