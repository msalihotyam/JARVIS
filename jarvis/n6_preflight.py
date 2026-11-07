# pyright: strict
"""N6-06: sensors for the memory gate and the deterministic preflight port (O64, LANECTX P1).

Pure: inputs only. No git call, file read, network or clock; the caller reads the OS and the
conveyor state and passes plain values in.

* ``memory_input`` turns RAM / commit-charge / disk readings (GB, as numbers) into the mapping
  ``Situation.memory`` already takes (``free_gb`` / ``held`` / ``ok_for_s`` for
  ``work_supervisor.memory_hold``). It adds no second gate: the floor and the hysteresis stay in
  ``memory_hold``. The reading is the smallest of the three (the scarcest resource decides); a
  missing, negative, NaN, infinite, bool or non-numeric one makes it ``None``, which
  ``memory_hold`` answers with ``wait`` ("mem.unknown"), never dispatch.
* ``preflight`` blocks a slice before launch: wrong base sha, a granted file owned by another
  running lane, an unmet ``depends_on``, or a malformed slice (fail closed).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from math import isfinite
from typing import NamedTuple, cast

BASE_RE = re.compile(r"(?:^|\s)base=([0-9a-f]{40})(?:\s|$)")
SHA_RE = re.compile(r"[0-9a-f]{40}")


def _gb(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if isfinite(number) and number >= 0 else None


def memory_input(
    ram_free_gb: object,
    commit_free_gb: object,
    disk_free_gb: object,
    *,
    held: bool = False,
    ok_for_s: object = 0,
) -> dict[str, object]:
    """Build ``Situation.memory``. ``held`` is the caller's last ``decision.action == "wait"``."""
    readings = [_gb(ram_free_gb), _gb(commit_free_gb), _gb(disk_free_gb)]
    free = None if None in readings else min(cast("list[float]", readings))
    return {"free_gb": free, "held": held, "ok_for_s": ok_for_s}


class Verdict(NamedTuple):
    ok: bool
    reason: str


def _strs(value: object) -> list[str] | None:
    """A list/tuple of non-empty strings, else None."""
    if not isinstance(value, (list, tuple)):
        return None
    items = cast("list[object] | tuple[object, ...]", value)
    out: list[str] = []
    for item in items:
        if type(item) is not str or not item.strip():
            return None
        out.append(item.replace("\\", "/"))
    return out


def _overlap(a: str, b: str) -> bool:
    """The conveyor's rule: one path is a prefix of the other (a trailing * is a wildcard)."""
    a, b = a.rstrip("*"), b.rstrip("*")
    return a.startswith(b) or b.startswith(a)


def _blocked(reason: str) -> Verdict:
    return Verdict(False, reason)


def preflight(
    slice_: object,
    main_sha: object,
    owned_by_running_lanes: object,
    done_ids: object,
) -> Verdict:
    """ok | blocked(reason). ``slice_``: {"id", "header", "files", "depends_on"};
    ``owned_by_running_lanes``: {lane id: [files]}; ``done_ids``: ids already done."""
    if not isinstance(slice_, Mapping):
        return _blocked("malformed: slice is not a mapping")
    sl = cast("Mapping[object, object]", slice_)
    sid, header = sl.get("id"), sl.get("header")
    files, deps = _strs(sl.get("files")), _strs(sl.get("depends_on", []))
    if type(sid) is not str or not sid or type(header) is not str or files is None or deps is None:
        return _blocked("malformed: slice needs id, header, files and depends_on")
    if type(main_sha) is not str or not SHA_RE.fullmatch(main_sha):
        return _blocked("malformed: main_sha is not 40 hex")
    if not isinstance(owned_by_running_lanes, Mapping):
        return _blocked("malformed: owned_by_running_lanes is not a mapping")
    if not isinstance(done_ids, (list, tuple, set, frozenset)):
        return _blocked("malformed: done_ids is not a collection")
    try:
        done = set(cast("list[object]", done_ids))
    except TypeError:
        return _blocked("malformed: done_ids holds an unhashable item")
    m = BASE_RE.search(header.splitlines()[0] if header else "")
    if m is None:
        return _blocked("base: header has no base=<40hex>")
    if m.group(1) != main_sha:
        return _blocked(f"base: header {m.group(1)[:7]} != main {main_sha[:7]}")
    missing = [d for d in deps if d not in done]
    if missing:
        return _blocked("deps: unmet " + ", ".join(missing))
    owners = cast("Mapping[object, object]", owned_by_running_lanes)
    for lane, owned in owners.items():
        theirs = _strs(owned)
        if type(lane) is not str or theirs is None:
            return _blocked("malformed: owned_by_running_lanes entry")
        if lane == sid:
            continue
        for mine in files:
            hit = next((t for t in theirs if _overlap(mine, t)), None)
            if hit is not None:
                return _blocked(f"files: {mine} is owned by running lane {lane} ({hit})")
    return Verdict(True, "ok")
