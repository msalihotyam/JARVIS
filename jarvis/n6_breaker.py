# pyright: strict
"""N6-17 (G16/R16): a cross-run breaker that trips to hold and resets only by an explicit action.

The conveyor's autopilot breaker without the conveyor: pure functions over a ``State``
plus one small JSON file whose path the caller passes in. Bad finishes count one per
root id (auto-fix chain suffixes share the root); ``BREAKER_BAD`` distinct roots within
``BREAKER_WINDOW_S`` open it. While open, every new non-INT slice is held. It closes
only on ``reset`` (an operator action at or after the opening) or a GOOD finish, after
the opening, of a root that is not one of the breaker's bad roots and not a CDX/DRY
review lane. BLOCKED from a CDX/DRY lane never counts.

A malformed or unreadable state file is UNREADABLE, which counts as open; finishes do
not rewrite it. Writes go through a temp file + ``os.replace`` under an OS byte lock on
``<path>.lock`` (the n6_lease pattern). Time is always an argument; no clock is read.
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
INT_RE = re.compile(r"(?i)^int(?![a-z])")
CDX_DRY_RE = re.compile(r"(CDX|DRY)")  # used with .match: a CDX/DRY prefix
# Deviation: conveyor's r"(?i)(-\d+)?-AFX$|-R$|-S$" strips a slice's own number (N6-05-AFX -> N6),
# merging distinct slices (fail open). Here an -<i> iteration before -AFX is stripped only when
# what remains still has a numbered root (N6-05-2-AFX) or is a numbered INT lane (INT5-2-AFX).
CHAIN_SUFFIX_RE = re.compile(r"(?i)-(AFX|R|S)$")
_ITER_RE = re.compile(r"-\d+$")
BREAKER_BAD = 3
BREAKER_WINDOW_S = 7200


class _Unreadable(enum.Enum):
    OPEN = "unreadable"


UNREADABLE = _Unreadable.OPEN  # load() of a state that cannot be trusted: counts as open


@dataclass(frozen=True)
class State:
    bad: tuple[tuple[str, float], ...] = ()  # (lane name, finished ts), one per root
    since: float | None = None  # opening time; None while closed
    roots: tuple[str, ...] = ()  # root ids that opened it or failed while open
    reason: str = ""


def root_id(name: str) -> str:
    """The chain root: X-AFX, X-2-AFX, X-R, X-S (and chains of them) all map to X."""
    while (m := CHAIN_SUFFIX_RE.search(name)) and m.start() > 0:
        name = name[:m.start()]
        if m.group(1).upper() == "AFX" and (it := _ITER_RE.search(name)) and it.start() > 0:
            base = root_id(name[:it.start()])
            if _ITER_RE.search(base) or (INT_RE.match(base) and any(c.isdigit() for c in base)):
                name = name[:it.start()]
    return name.upper()


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:  # an int too large for a float
        return None
    return f if math.isfinite(f) else None


def _parse(data: object) -> State | None:
    if not isinstance(data, dict):
        return None
    d = cast("dict[str, object]", data)
    raw_bad, brk = d.get("bad"), d.get("breaker")
    if not isinstance(raw_bad, list) or "breaker" not in d:
        return None
    bad: list[tuple[str, float]] = []
    for item in cast("list[object]", raw_bad):
        if not isinstance(item, list):
            return None
        pair = cast("list[object]", item)
        fin = _num(pair[1]) if len(pair) == 2 else None
        if fin is None or not isinstance(pair[0], str) or not pair[0].strip():
            return None
        bad.append((pair[0], fin))
    if brk is None:
        return State(tuple(bad))
    if not isinstance(brk, dict):
        return None
    b = cast("dict[str, object]", brk)
    since, roots, reason = _num(b.get("since")), b.get("roots"), b.get("reason")
    if since is None or not isinstance(reason, str) or not isinstance(roots, list):
        return None
    rl = cast("list[object]", roots)
    if not all(isinstance(r, str) for r in rl):
        return None
    return State(tuple(bad), since, tuple(cast("list[str]", rl)), reason)


def load(path: Path | str) -> State | _Unreadable:
    """The breaker state; a fresh closed State when there is no file; UNREADABLE otherwise."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return State()
    except (OSError, UnicodeError):
        return UNREADABLE
    try:
        data = cast(object, json.loads(raw))
    except (ValueError, RecursionError):  # RecursionError: absurdly deep nesting
        return UNREADABLE
    state = _parse(data)
    return UNREADABLE if state is None else state


def is_open(state: State | _Unreadable) -> bool:
    return not isinstance(state, State) or state.since is not None


def holds(state: State | _Unreadable, slice_id: str) -> bool:
    """True when a new dispatch of ``slice_id`` must wait: the breaker is open and it is not INT."""
    return is_open(state) and not INT_RE.match(slice_id.strip())


def apply_finish(state: State, name: str, status: str, fin: float, now: float) -> State:
    """The state after one lane finish (pure). Non-terminal statuses only age the window."""
    if not name.strip() or _num(fin) is None or _num(now) is None:
        raise ValueError(f"bad finish: name={name!r} fin={fin!r} now={now!r}")
    st, root = status.strip().upper(), root_id(name)
    bad, since, roots, reason = list(state.bad), state.since, state.roots, state.reason
    if st in BAD and not (st == "BLOCKED" and CDX_DRY_RE.match(name)):
        bad = [b for b in bad if root_id(b[0]) != root] + [(name, float(fin))]
        if since is not None and root not in roots:
            roots = (*roots, root)
    elif st in GOOD and since is not None and fin > since and root not in roots \
            and not CDX_DRY_RE.match(root):
        since, roots, reason = None, (), ""
        bad = [b for b in bad if b[1] > fin]
    bad = [b for b in bad if now - b[1] < BREAKER_WINDOW_S]
    if since is None and len(bad) >= BREAKER_BAD:
        since, roots = float(now), tuple(sorted({root_id(b[0]) for b in bad}))
        reason = f"{len(bad)} bad roots within {BREAKER_WINDOW_S}s"
    return State(tuple(bad), since, roots, reason)


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
    brk = None if state.since is None else \
        {"reason": state.reason, "since": state.since, "roots": list(state.roots)}
    data = {"bad": [list(b) for b in state.bad], "breaker": brk}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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
    """Operator reset record: closes the breaker when ``ts`` is at or after its opening and
    clears the bad list. A reset older than the opening is ignored (False). On an unreadable
    file the reset is the repair: it writes a fresh closed state."""
    if _num(ts) is None:
        raise ValueError(f"bad reset ts: {ts!r}")
    path = Path(path)
    with _locked(path) as got:
        if not got:
            return False
        state = load(path)
        if isinstance(state, State) and (state.since is None or ts < state.since):
            return False
        try:
            _write(path, State())
        except (OSError, ValueError):
            return False
        return True
