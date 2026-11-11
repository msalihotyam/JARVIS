# pyright: strict
"""N6-08 (M8): a single-owner lease with a monotonically increasing fencing epoch.

The condition_watch idea (owner + fencing epoch; a write under a stale epoch is
refused) without its database: one JSON file whose path the caller passes in
(production: ``build/n6/lease.json``, outside ``build/conveyor/``). Writes go
through a temp file + ``os.replace`` under an OS byte lock on ``<path>.lock``
(the conveyor's CONVLOCK2 approach). Readers take no lock and create nothing.

A malformed or unreadable lease file is HELD_ALL (fail closed), never "no lease".
Time is always the ``now`` argument (epoch seconds); this module reads no clock.
"""

from __future__ import annotations

import contextlib
import enum
import json
import math
import os
import tempfile
from collections.abc import Callable, Generator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast


class _Held(enum.Enum):
    ALL = "held_all"


HELD_ALL = _Held.ALL  # live_slices of an unreadable lease: every slice counts as held


class Fenced(Exception):
    """Refused: another owner or a newer epoch holds the lease."""


@dataclass(frozen=True)
class Lease:
    owner: str
    slice_id: str
    epoch: int
    expires: float


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
    """True while this process holds the lease's OS lock; False if another one does."""
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


def _valid(data: object) -> dict[str, object] | None:
    if not isinstance(data, dict):
        return None
    state = cast("dict[str, object]", data)
    epoch, expires, applied = state.get("epoch"), state.get("expires"), state.get("applied")
    if type(epoch) is not int or epoch < 0:
        return None
    if not isinstance(state.get("owner"), str) or not isinstance(state.get("slice_id"), str):
        return None
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return None
    try:
        if not math.isfinite(expires):
            return None
    except OverflowError:  # N6-08FIX F5: an int too large for a float (1 followed by 400 zeros)
        return None
    if not isinstance(applied, list):
        return None
    for item in cast("list[object]", applied):
        if not isinstance(item, list):
            return None
        pair = cast("list[object]", item)
        if len(pair) != 2 or type(pair[0]) is not int or not isinstance(pair[1], str):
            return None
    return state


def _read(path: Path) -> dict[str, object] | None | _Held:
    """The lease state; None when there is no file; HELD_ALL when it cannot be trusted."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return HELD_ALL
    try:
        data = cast(object, json.loads(raw))
    except (ValueError, RecursionError):  # RecursionError: absurdly deep nesting
        return HELD_ALL
    state = _valid(data)
    return HELD_ALL if state is None else state


def _lease(state: dict[str, object]) -> Lease:
    return Lease(cast(str, state["owner"]), cast(str, state["slice_id"]),
                 cast(int, state["epoch"]), float(cast(float, state["expires"])))


def _current(state: dict[str, object] | None | _Held, lease: Lease, now: float) -> bool:
    """The lease is still the file's live lease: same epoch, owner and slice, unexpired."""
    if not isinstance(state, dict):
        return False
    held = _lease(state)
    return held.epoch == lease.epoch and held.owner == lease.owner \
        and held.slice_id == lease.slice_id and held.expires > now


def _write(path: Path, lease: Lease, applied: list[list[object]]) -> None:
    state = {"epoch": lease.epoch, "owner": lease.owner, "slice_id": lease.slice_id,
             "expires": lease.expires, "applied": applied}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _applied(state: dict[str, object], epoch: int) -> list[list[object]]:
    # Older epochs can never write again, so only this epoch's events are kept.
    return [p for p in cast("list[list[object]]", state["applied"]) if p[0] == epoch]


def acquire(path: Path | str, owner: str, slice_id: str, ttl_s: float, now: float) -> Lease | None:
    """A new lease at epoch + 1, or None while a live lease, the lock or an unreadable file stands."""
    path = Path(path)
    try:  # N6-WIRE2FIX F7: a NaN/inf/overflowing expiry never reaches the file
        expires = float(now + ttl_s)
        if not (math.isfinite(float(now)) and math.isfinite(expires) and expires > now):
            return None
    except (OverflowError, TypeError, ValueError):
        return None
    with _locked(path) as got:
        if not got:
            return None
        state = _read(path)
        if isinstance(state, _Held) or (state is not None and _lease(state).expires > now):
            return None  # held, or unreadable: fail closed until an operator repairs the file
        epoch = 0 if state is None else _lease(state).epoch
        lease = Lease(owner, slice_id, epoch + 1, expires)
        try:
            _write(path, lease, [])
        except (OSError, ValueError):  # ValueError: an epoch past the int digit limit
            return None
        return lease


def renew(path: Path | str, lease: Lease, ttl_s: float, now: float) -> Lease | None:
    """The lease extended to max(current expiry, now + ttl_s); None for a stale epoch, an
    expired lease or a non-finite expiry. A renew never shortens the lease (N6-HARD2 H2)."""
    path = Path(path)
    with _locked(path) as got:
        state = _read(path)
        if not got or not isinstance(state, dict) or not _current(state, lease, now):
            return None
        held = _lease(state)  # N6-08FIX F2: the file's state, never the caller's copy
        try:
            new = float(now + ttl_s)
        except (OverflowError, TypeError):
            return None
        if not math.isfinite(new):
            return None
        renewed = replace(held, expires=max(held.expires, new))
        try:
            _write(path, renewed, _applied(state, renewed.epoch))
        except (OSError, ValueError):
            return None
        return renewed


def release(path: Path | str, lease: Lease, now: float) -> bool:
    """End the lease (the epoch stays, so the next owner gets epoch + 1); False if stale."""
    path = Path(path)
    with _locked(path) as got:
        state = _read(path)
        if not got or not isinstance(state, dict) or not _current(state, lease, now):
            return False
        try:
            held = _lease(state)
            _write(path, replace(held, expires=now), _applied(state, held.epoch))
        except (OSError, ValueError):
            return False
        return True


def check(path: Path | str, lease: Lease, now: float) -> bool:
    """True while ``lease`` is the file's live lease; a stale epoch is False."""
    return _current(_read(Path(path)), lease, now)


def fenced(path: Path | str, lease: Lease, now: float, write: Callable[[], object]) -> bool:
    """Run ``write`` while holding the lease's OS lock, only if ``lease`` is still the file's
    live lease (same slice, epoch and owner, unexpired). False and no write otherwise, also
    when another process holds the lock. Nothing can re-epoch the lease during ``write``."""
    path = Path(path)
    with _locked(path) as got:
        if not got or not _current(_read(path), lease, now):
            return False
        _ = write()
        return True


def apply_event(path: Path | str, lease: Lease, event_id: str, now: float) -> bool:
    """Record (epoch, event_id) once: True the first time, False on a replay.

    Raises Fenced when the lease's epoch is stale (or the lock is held elsewhere)."""
    path = Path(path)
    with _locked(path) as got:
        state = _read(path)
        if not got or not isinstance(state, dict) or not _current(state, lease, now):
            raise Fenced(f"stale epoch {lease.epoch} for {lease.slice_id}")
        held = _lease(state)  # N6-08FIX F2: the file's lease (renewed expiry, int epoch), never the caller's copy
        applied = _applied(state, held.epoch)
        if [held.epoch, event_id] in applied:
            return False
        try:
            _write(path, held, [*applied, [held.epoch, event_id]])
        except (OSError, ValueError) as e:
            raise Fenced(f"lease write failed: {e}") from e
        return True


def live_slices(path: Path | str, now: float) -> frozenset[str] | _Held:
    """Slice ids under a live lease; HELD_ALL when the file is malformed or unreadable."""
    state = _read(Path(path))
    if isinstance(state, _Held):
        return HELD_ALL
    if state is None or _lease(state).expires <= now:
        return frozenset()
    return frozenset({_lease(state).slice_id})
