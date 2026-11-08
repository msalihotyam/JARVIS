# pyright: strict
"""N6-17 (G16/R16): a cross-run breaker that trips to hold and resets only by an explicit action.

The conveyor's autopilot breaker without the conveyor: pure functions over a ``State``
plus one small JSON file whose path the caller passes in. The breaker is per root:
every BAD finish is kept, and ``BREAKER_BAD`` BAD finishes of the same root (its
-AFX/-R/-S/-<i>-AFX iterations included, see ``root_ids``) within ``BREAKER_WINDOW_S``
open that root. While a root is open, new dispatches of that root (INT roots included)
are held; unrelated roots are never held. An open root closes only by ``reset`` (an
operator record at or after its opening) or a GOOD finish of that same root after the
opening that is not a CDX/DRY review lane. BLOCKED from a CDX/DRY lane never counts.

A malformed or unreadable state file is UNREADABLE, which counts as open for every root;
finishes do not rewrite it. A missing state file while the ``<path>.open`` marker exists
(written whenever any root is open) is UNREADABLE too. Writes go through a temp file +
``os.replace`` under an OS byte lock on ``<path>.lock`` (the n6_lease pattern). Time is
always an argument; no clock is read.
"""

from __future__ import annotations

import contextlib
import enum
import json
import math
import os
import re
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

# Mirrors scripts/conveyor.py (autopilot breaker); copied, not imported: jarvis/ never imports scripts/.
GOOD = frozenset({"PASS", "COMPLETE", "DONE", "GREEN", "OK"})
BAD = frozenset({"BLOCKED", "STOP", "FAIL", "FAILED"})
CDX_DRY_RE = re.compile(r"(CDX|DRY)")  # used with .match: a CDX/DRY prefix
# conveyor's r"(?i)(-\d+)?-AFX$|-R$|-S$", split in two: the -<i> iteration before -AFX is an
# unpadded positive int (conveyor emits f"{name}-{i}-AFX", i from 1), so a zero-padded slice
# number (N6-05-AFX) never strips to N6. Where the shape is ambiguous (A1-2-AFX) the name
# belongs to both candidate roots (A1-2 and A1): it counts toward and is held by either
# (A1-2/3/4-AFX alone open A1: the conveyor's split lanes of A1 are named exactly so).
CHAIN_SUFFIX_RE = re.compile(r"(?i)-(AFX|R|S)$")
_ITER_RE = re.compile(r"-[1-9]\d*$")
BREAKER_BAD = 3
BREAKER_WINDOW_S = 7200


class _Unreadable(enum.Enum):
    OPEN = "unreadable"


UNREADABLE = _Unreadable.OPEN  # load() of a state that cannot be trusted: counts as open


@dataclass(frozen=True)
class State:
    bad: tuple[tuple[str, float], ...] = ()  # every BAD finish in the window: (lane name, fin)
    opened: tuple[tuple[str, float, str], ...] = ()  # open roots: (root, since, reason)
    cleared: tuple[tuple[str, float], ...] = ()  # (root, ts): its BAD finishes up to ts no longer count


def root_id(name: str) -> str:
    """The literal root: chain suffixes (-AFX/-R/-S) stripped, no iteration stripped."""
    name = name.strip()
    while (m := CHAIN_SUFFIX_RE.search(name)) and m.start() > 0:
        name = name[:m.start()]
    return name.upper()


def root_ids(name: str) -> frozenset[str]:
    """Every root ``name`` may be an iteration of: X-AFX, X-<i>-AFX, X-R, X-S and chains -> X."""
    # iterative with a seen set: every candidate is a prefix of name, so a deep chain neither
    # recurses nor blows up (review H3)
    out: set[str] = set()
    todo, seen = [name.strip().upper()], set[str]()
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        m = CHAIN_SUFFIX_RE.search(n)
        if not m or m.start() == 0:
            out.add(n)
            continue
        base = n[:m.start()]
        todo.append(base)
        if m.group(1) == "AFX" and (it := _ITER_RE.search(base)) and it.start() > 0:
            todo.append(base[:it.start()])
    return frozenset(out)


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:  # an int too large for a float
        return None
    return f if math.isfinite(f) else None


def _rows(v: object, width: int) -> list[tuple[str, float, str]] | None:
    if not isinstance(v, list):
        return None
    out: list[tuple[str, float, str]] = []
    for item in cast("list[object]", v):
        row = cast("list[object]", item) if isinstance(item, list) else []
        if len(row) != width:
            return None
        key, ts, why = row[0], _num(row[1]), row[2] if width == 3 else ""
        if not isinstance(key, str) or not key.strip() or ts is None or not isinstance(why, str):
            return None
        out.append((key, ts, why))
    return out


def _parse(data: object) -> State | None:
    if not isinstance(data, dict):
        return None
    d = cast("dict[str, object]", data)
    bad, opened, cleared = _rows(d.get("bad"), 2), _rows(d.get("open"), 3), _rows(d.get("cleared"), 2)
    if bad is None or opened is None or cleared is None:
        return None
    # root keys are normalized by root_id, so a hand-edited "x" or "x-afx" still holds X (review H2)
    return State(tuple((n, f) for n, f, _ in bad), tuple((root_id(r), t, w) for r, t, w in opened),
                 tuple((root_id(r), t) for r, t, _ in cleared))


def _marker(path: Path) -> Path:
    return Path(f"{path}.open")


def load(path: Path | str) -> State | _Unreadable:
    """The breaker state; a fresh closed State when there is no file and no open marker;
    UNREADABLE otherwise (a lost file while a root was open stays open)."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return UNREADABLE if _marker(path).exists() else State()
    except (OSError, UnicodeError):
        return UNREADABLE
    try:
        data = cast(object, json.loads(raw))
    except (ValueError, RecursionError):  # RecursionError: absurdly deep nesting
        return UNREADABLE
    state = _parse(data)
    return UNREADABLE if state is None else state


def is_open(state: State | _Unreadable) -> bool:
    """True when any root is open (or the state is unreadable)."""
    return not isinstance(state, State) or bool(state.opened)


def holds(state: State | _Unreadable, slice_id: str) -> bool:
    """True when a new dispatch of ``slice_id`` must wait: one of its roots is open."""
    if not isinstance(state, State):
        return True
    return bool(root_ids(slice_id) & {r for r, _, _ in state.opened})


def apply_finish(state: State, name: str, status: str, fin: float, now: float) -> State:
    """The state after one lane finish (pure). Non-terminal statuses only age the window."""
    if not name.strip() or _num(fin) is None or _num(now) is None:
        raise ValueError(f"bad finish: name={name!r} fin={fin!r} now={now!r}")
    st = status.strip().upper()
    bad = list(state.bad)
    opened = {r: (since, why) for r, since, why in state.opened}
    cleared = dict(state.cleared)
    counted = st in BAD and not (st == "BLOCKED" and CDX_DRY_RE.match(name))
    if counted:
        bad.append((name, float(fin)))
    elif st in GOOD and not CDX_DRY_RE.match(name):
        r = root_id(name)
        if r in opened and fin > opened[r][0]:
            del opened[r]
            # capped at now: a future-dated GOOD must not mute that root's later BADs (review H1)
            cleared[r] = max(cleared.get(r, -math.inf), min(float(fin), float(now)))
    bad = [b for b in bad if now - b[1] < BREAKER_WINDOW_S]
    cleared = {r: t for r, t in cleared.items() if now - t < BREAKER_WINDOW_S}
    if counted:
        for r in sorted(root_ids(name) - opened.keys()):
            # every BAD counts toward every root it may belong to, so split lanes X-<i>-AFX
            # open X even when X's own BAD aged out or was reset (CLREV-N6-17FIX F1). A phantom
            # root pooled from AFX lanes of different slices (N6 from N6-11/12/13-AFX) fails
            # closed: it holds only N6 and N6-<i>-AFX names.
            n = sum(1 for b, f in bad if r in root_ids(b) and f > cleared.get(r, -math.inf))
            if n >= BREAKER_BAD:
                opened[r] = (float(now), f"{n} bad finishes of {r} within {BREAKER_WINDOW_S}s")
    return State(tuple(bad), tuple((r, s, w) for r, (s, w) in sorted(opened.items())),
                 tuple(sorted(cleared.items())))


def _os_lock(fd: int, unlock: bool = False) -> None:
    if os.name == "nt":
        import msvcrt
        _ = os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextlib.contextmanager
def _locked(path: Path) -> Generator[bool]:
    """True while this process holds the breaker's OS lock; False if another one does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _os_lock(fd)
            got = True
        except OSError:
            got = False
        try:
            yield got
        finally:
            if got:
                with contextlib.suppress(OSError):
                    _os_lock(fd, unlock=True)
    finally:
        os.close(fd)


def _write(path: Path, state: State) -> None:
    """Atomic write. The open marker is created before an open state lands and removed only
    after a fully closed state has landed, so a lost state file never reads as closed."""
    data = {"bad": [list(b) for b in state.bad], "open": [list(o) for o in state.opened],
            "cleared": [list(c) for c in state.cleared]}
    if state.opened:
        _marker(path).touch()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if not state.opened:
        _marker(path).unlink(missing_ok=True)


def record_finish(path: Path | str, name: str, status: str, fin: float, now: float) -> bool:
    """Apply one finish to the file. False (nothing written) when the state is unreadable,
    the lock is held elsewhere or the write fails: the caller retries; the file stays as is."""
    path = Path(path)
    with _locked(path) as got:
        state = load(path)
        if not got or not isinstance(state, State):
            return False
        new = apply_finish(state, name, status, fin, now)
        try:
            _write(path, new)
        except (OSError, ValueError):
            return False
        return True


def reset(path: Path | str, ts: float) -> bool:
    """Operator reset record: closes every root opened at or before ``ts`` and drops the BAD
    finishes up to ``ts``. False when it closes nothing (a reset older than every opening is
    ignored). On an unreadable state (or a lost file with the open marker) the reset is the
    repair: it writes a fresh closed state and removes the marker."""
    if _num(ts) is None:
        raise ValueError(f"bad reset ts: {ts!r}")
    path = Path(path)
    with _locked(path) as got:
        if not got:
            return False
        state = load(path)
        new = State()
        if isinstance(state, State):
            keep = tuple(o for o in state.opened if o[1] > ts)
            if len(keep) == len(state.opened):
                return False
            new = State(tuple(b for b in state.bad if b[1] > ts), keep, state.cleared)
        try:
            _write(path, new)
        except (OSError, ValueError):
            return False
        return True
