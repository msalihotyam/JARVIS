# pyright: strict
"""N6-16 (G15, R13/R15, O32/O56): one action per watchdog verdict.

Verdicts: ``work_controls.watchdog_verdict`` gives ``continue`` or ``timeout``; the
caller classifies network/API errors as ``outage``. Each maps to exactly one action:

- ``outage``  -> HOLD: the run is PAUSED, not stuck. No nudge, no redispatch, never
  counted against the retry cap or the breaker. The stall clock restarts afterwards.
- ``timeout`` -> NUDGE at most MAX_NUDGES times, NUDGE_GAP_S apart (the conveyor's
  MAX_NUDGES 2 / NUDGE_GAP_S 900; a nudge is counted when decided, even if it fails),
  WAIT inside the gap, then DROP to attention after the cap (and its gap) or
  DROP_AFTER_S of stall. DROP is the only action that counts as a failure.
- ``continue`` -> CONTINUE: progress; a hold clears and the run may resume.
- anything else, a malformed state file or a clock earlier than the state -> HOLD
  plus attention (fail closed); the state is left untouched.

State is one small JSON file per run whose path the caller passes in, written via a
temp file + ``os.replace``. Single writer (the lease holder). This module reads no
clock (``now`` is epoch seconds), kills nothing and makes no live call.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

MAX_NUDGES = 2
NUDGE_GAP_S = 900.0
DROP_AFTER_S = 1800.0
VERSION = 1

CONTINUE, HOLD, NUDGE, WAIT, DROP = "continue", "hold", "nudge", "wait", "drop"


@dataclass(frozen=True)
class State:
    nudges: int = 0
    last_nudge_at: float | None = None
    stall_since: float | None = None
    outage_since: float | None = None
    dropped: bool = False


@dataclass(frozen=True)
class Decision:
    action: str
    attention: bool = False
    counts_failure: bool = False  # True only for DROP; an outage never counts


_FAIL_CLOSED = Decision(HOLD, attention=True)


def _time(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _valid(state: object) -> bool:
    if not isinstance(state, State):
        return False
    n = state.nudges
    stamps = (state.last_nudge_at, state.stall_since, state.outage_since)
    return (type(n) is int and 0 <= n <= MAX_NUDGES and type(state.dropped) is bool
            and all(t is None or _time(t) for t in stamps)
            and (n == 0) == (state.last_nudge_at is None))


def decide(state: State | None, verdict: object, now: float) -> tuple[Decision, State | None]:
    """Pure: the action for ``verdict`` at ``now`` and the state to store next."""
    if state is None or not _valid(state) or not _time(now):
        return _FAIL_CLOSED, state
    stamps = (state.last_nudge_at, state.stall_since, state.outage_since)
    if any(t is not None and now < t for t in stamps):
        return _FAIL_CLOSED, state  # clock went backwards: no guess
    if verdict == "continue":
        return Decision(CONTINUE), replace(state, stall_since=None, outage_since=None,
                                           dropped=False)
    if verdict == "outage":
        since = state.outage_since if state.outage_since is not None else now
        return Decision(HOLD), replace(state, outage_since=since, stall_since=None)
    if verdict != "timeout":
        return _FAIL_CLOSED, state
    stalled = replace(state, outage_since=None,
                      stall_since=state.stall_since if state.stall_since is not None else now)
    since = cast(float, stalled.stall_since)
    gap_ok = state.last_nudge_at is None or now - state.last_nudge_at >= NUDGE_GAP_S
    if now - since >= DROP_AFTER_S or (state.nudges >= MAX_NUDGES and gap_ok):
        first = not state.dropped  # page and count a drop once, not on every later tick
        return Decision(DROP, attention=first, counts_failure=first), replace(stalled, dropped=True)
    if state.nudges < MAX_NUDGES and gap_ok:
        return Decision(NUDGE), replace(stalled, nudges=state.nudges + 1, last_nudge_at=now)
    return Decision(WAIT), stalled


def load(path: Path | str) -> State | None:
    """Absent file -> a fresh State. Unreadable or malformed -> None (fail closed)."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None if os.path.lexists(path) else State()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = cast(object, json.loads(raw))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fields = cast(dict[str, object], data)
    if fields.pop("version", None) != VERSION or set(fields) != set(State.__dataclass_fields__):
        return None
    state = State(**fields)  # pyright: ignore[reportArgumentType]  # checked by _valid
    return state if _valid(state) else None


def save(path: Path | str, state: State) -> None:
    if not _valid(state):
        raise ValueError("invalid outage state")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, **asdict(state)}, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def step(path: Path | str, verdict: object, now: float) -> Decision:
    """load + decide + save. A malformed file is never overwritten (left for the owner)."""
    state = load(path)
    decision, new = decide(state, verdict, now)
    if state is not None and new is not None and new != state:
        save(path, new)
    return decision
