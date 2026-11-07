# pyright: strict
"""N6-05: the usage reader. Turns the conveyor's usage sources into the N6-04 quota input.

Pure: every function takes text and an injected ``now`` epoch; nothing here opens a file, the
network or the wall clock (the caller reads the source text). Three source formats, mirroring
``scripts/conveyor.py`` ``usage_pct`` / ``write_status``:

* ledger  - one ``build/ledger/slices.jsonl`` row: ``seven_day.end`` (else ``.start``) at ``end``
* live    - ``build/lanes/<family>.json``: ``seven_pct`` at ``updated_at``
* status  - ``build/conveyor/status.json``: ``usage_pct`` at ``updated_at`` + ``pace.window_start``

Anything malformed, out of range, from the future or stale is UNKNOWN (``None``), never zero:
the quota function pauses on unknown. ``resolve`` then applies the conveyor's jump guards inside
the current quota window and returns ``(pct or None, window_start or None)`` for ``quota_verdict``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import cast

from jarvis.work_controls import ISTANBUL, quota_window_end

# Mirrors of scripts/conveyor.py (a jarvis module cannot import scripts/); keep in step.
USAGE_FRESH_S = 7 * 86400  # CONVUSAGE: ledger / live readings older than this are ignored
STATUS_FRESH_S = 600  # STATUS_STALE_S: a status.json older than this is stale
RESET_DROP = 40  # a jump of this many points between two readings is a spike (rise) or reset (drop)
SPIKE_WINDOW_S = 6 * 3600  # a RESET_DROP rise within this of the last accepted value needs confirming
SPIKE_CONFIRM_S = 1800  # ... by another source, or by the same source again this much later


@dataclass(frozen=True)
class Reading:
    pct: float
    ts: float
    source: str
    window_start: str | None = None


@dataclass(frozen=True)
class Usage:
    """The quota function's input; ``pct`` None means unknown (it pauses)."""

    pct: float | None
    window_start: str | None
    source: str | None


UNKNOWN = Usage(None, None, None)


def _ts(value: object) -> float | None:
    """Epoch of an ISO UTC time such as 2026-10-09T10:24:35.107Z (conveyor ``utc_ts``)."""
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        parsed = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
        return parsed.replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _pct(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not isfinite(value) or not 0 <= value <= 100:
        return None
    return float(value)


def _fresh(ts: float | None, now: float, limit: int) -> bool:
    return ts is not None and 0 <= now - ts <= limit


def _dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(k): v for k, v in cast("dict[object, object]", value).items()}


def _obj(text: str) -> dict[str, object] | None:
    try:
        data: object = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return _dict(data)


def parse_ledger(text: str, *, now: float) -> Reading | None:
    """Newest slice-ledger row with a usable weekly %; stale or none -> None."""
    for line in reversed(text.splitlines()):
        row = _obj(line)
        if row is None:
            continue
        week = _dict(row.get("seven_day"))
        if week is None:
            continue
        # conveyor: end, or start only when end is None; a bad end is not repaired from start
        pct = _pct(week.get("start" if week.get("end") is None else "end"))
        ts = _ts(row.get("end"))
        if pct is None or ts is None:
            continue
        # the newest usable row decides: an older row never stands in for a stale one
        return Reading(pct, ts, "ledger") if _fresh(ts, now, USAGE_FRESH_S) else None
    return None


def parse_live(text: str, *, family: str, now: float) -> Reading | None:
    d = _obj(text)
    if d is None:
        return None
    pct, ts = _pct(d.get("seven_pct")), _ts(d.get("updated_at"))
    if pct is None or not _fresh(ts, now, USAGE_FRESH_S) or ts is None:
        return None
    return Reading(pct, ts, f"live:{family}")


def parse_status(text: str, *, now: float) -> Reading | None:
    d = _obj(text)
    if d is None:
        return None
    pct, ts = _pct(d.get("usage_pct")), _ts(d.get("updated_at"))
    if pct is None or not _fresh(ts, now, STATUS_FRESH_S) or ts is None:
        return None
    ws = (_dict(d.get("pace")) or {}).get("window_start")
    return Reading(pct, ts, "status", ws if isinstance(ws, str) else None)


def _window(window_start: str | None) -> tuple[float, float] | None:
    """(start, end) epochs of the quota window, or None when unreadable."""
    if not isinstance(window_start, str):
        return None
    try:
        parsed = datetime.fromisoformat(window_start)
        start = (parsed if parsed.tzinfo else parsed.replace(tzinfo=ISTANBUL)).timestamp()
        end = quota_window_end(window_start)
    except (ValueError, OverflowError, OSError):
        return None
    return (start, end) if start <= end else None


def resolve(
    readings: Iterable[Reading | None], *, now: float, window_start: str | None = None,
    last_ok: tuple[float, float, float, str] | None = None,
    spike: tuple[str, float] | None = None,
) -> Usage:
    """Pick the current reading for the quota window.

    ``window_start`` is pace.json's (falls back to a status reading's). ``last_ok`` is the
    conveyor's pager ``usage_ok`` ``(pct, ts, seen_at, src)``, ``spike`` its ``usage_spike``
    ``(src, ts)``. Readings from before the window, a window that has already ended, a
    RESET_DROP+ fall inside the window (weekly % never drops) and an unconfirmed RESET_DROP+
    rise are not current: the first two and the fall give unknown, the rise holds ``last_ok``.
    """
    cands = [r for r in readings if r is not None]
    ws = window_start or next((r.window_start for r in cands if r.window_start), None)
    win = _window(ws)
    if win is None or not win[0] <= now < win[1]:
        return UNKNOWN
    cands = [r for r in cands if win[0] <= r.ts <= now]
    if not cands:
        return UNKNOWN
    top = max(cands, key=lambda r: (r.ts, r.pct))
    ok = last_ok if last_ok is not None and win[0] <= last_ok[1] <= now else None
    if ok is not None:
        if ok[0] - top.pct >= RESET_DROP:
            return UNKNOWN  # a reset inside the window is not a reading of it
        if top.pct - ok[0] >= RESET_DROP and now - ok[2] < SPIKE_WINDOW_S:
            confirmed = any(
                r.source != top.source and r.pct - ok[0] >= RESET_DROP and r.ts > ok[1]
                for r in cands) or (
                spike is not None and spike[0] == top.source and top.ts - spike[1] >= SPIKE_CONFIRM_S)
            if not confirmed:
                return Usage(ok[0], ws, ok[3])
    return Usage(top.pct, ws, top.source)
