# pyright: strict
"""N6 G28: read-only presence signal and power proposal (R10), shadow only.

Mirrors the conveyor's CONV6 presence model (scripts/conveyor.py sense_presence)
without acting on anything:

- Inputs are injected: idle seconds (the GetLastInputInfo reader is a later
  slice), the pace.json mapping, the clock as epoch seconds, and the prior state.
- idle >= away_after_min*60 -> raw away; idle < PRESENT_IDLE_S -> raw present;
  in between -> raw = current state (dead band, clears any pending change).
- A change counts only once the new raw state has held PRESENCE_HOLD_S: one
  switch per presence change, no flapping inside the hold or the dead band.
- Unknown or malformed input (idle, clock, pace, state) -> typed "unknown" with
  no lane target; presence is kept and a pending change dropped (a hold never
  spans an unseen read). The conveyor instead counts an unreadable idle as
  present; N6 never guesses. A pace.json the conveyor's load_pace rejects is
  unknown here (the conveyor then paces from its defaults).
- Lane target = pace max_lanes, else lanes_<presence> (as the conveyor's pace).
- Power: only a PowerProposal record (plan from POWER_PLANS) that always
  needs approval. Nothing here switches a plan, sets a priority, reads the OS,
  writes a file, or reads the wall clock. Not wired into the conveyor.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

PRESENT, AWAY, UNKNOWN = "present", "away", "unknown"
STATES = (PRESENT, AWAY)
# Copied from scripts/conveyor.py (not importable, must not be wired); a test pins them.
PRESENT_IDLE_S = 60
PRESENCE_HOLD_S = 120
MAX_LANES_CAP = 8
PACE_DEFAULTS: dict[str, object] = {
    "lanes_present": 3, "lanes_away": 5, "away_after_min": 10, "power_switch": False}
POWER_PLANS = {PRESENT: "JarvisWatch", AWAY: "JarvisFocus"}  # the allowlist
KEEP_MAX = 30
POSITIVE_KEYS = ("days", "reset_alert_pct", "full_burn_limit_pct", "min_free_mem_gb",
                 "max_commit_pct", "blocked_nudge_min")
VERDICTS = ("agree", "disagree", "unknown")


def _number(value: object) -> float | None:
    """A finite number >= 0, else None (bool, str, NaN, inf, negatives and huge ints fail)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        v = float(value)
    except OverflowError:  # an int past float range
        return None
    return v if math.isfinite(v) and v >= 0 else None


def _lanes(value: object, low: int) -> int | None:
    """A pace lane key as the conveyor reads it: number, int(), clamped to low..cap."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            (isinstance(value, float) and not math.isfinite(value)):
        return None
    return min(MAX_LANES_CAP, max(low, int(value)))


def _words(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(a, str) and a for a in cast("list[object]", value))


def _int_in(p: dict[str, object], key: str, top: int) -> bool:
    v = p.get(key, 0)
    return not isinstance(v, bool) and isinstance(v, int) and 0 <= v <= top


def _conveyor_rejects(p: dict[str, object]) -> bool:
    """load_pace's checks on the other keys: on any, it drops the whole file for its defaults."""
    cv, cmd = p.get("claude_version"), p.get("power_switch_cmd", [])
    if cv is not None and (not isinstance(cv, str) or not cv.strip() or cv != cv.strip()):
        return True
    for k in POSITIVE_KEYS:
        v = p.get(k, 1)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            return True
    if not all(_int_in(p, k, MAX_LANES_CAP) for k in ("codex_max", "jules_max")) or \
            not _int_in(p, "salih_keep_pct", KEEP_MAX) or \
            not isinstance(p.get("salih_keep_window", ""), str) or \
            not _words(cmd) or (p["power_switch"] and not cmd) or \
            not _words(p.get("power_switch_query_cmd", ["q"])) or \
            p.get("power_switch_query_cmd") == [] or \
            not _words(p.get("power_switch_skip_plans", [])) or \
            not isinstance(p.get("autofix_fable", False), bool):
        return True
    mode = p.get("mode", "full_burn")
    if mode == "paced":
        try:
            datetime.fromisoformat(cast(str, p.get("window_start"))).timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            return True
    return mode not in ("full_burn", "paced")


@dataclass(frozen=True)
class Pace:
    """The presence part of pace.json."""

    lanes_present: int
    lanes_away: int
    away_after_min: float
    max_lanes: int | None = None
    power_switch: bool = False


def read_pace(raw: object) -> Pace | None:
    """Pace from a pace.json mapping; None (= unknown) when the conveyor would reject it.

    A missing key takes the conveyor default; a present but malformed key -> None.
    """
    if not isinstance(raw, dict):
        return None
    p = {**PACE_DEFAULTS, **cast("dict[str, object]", raw)}
    away = _number(p["away_after_min"])
    present, gone = _lanes(p["lanes_present"], 1), _lanes(p["lanes_away"], 1)
    top = _lanes(p["max_lanes"], 1) if "max_lanes" in p else None
    if away is None or away <= 0 or present is None or gone is None or \
            ("max_lanes" in p and top is None) or not isinstance(p["power_switch"], bool) or \
            _conveyor_rejects(p):
        return None
    if "min_lanes" in p:  # the conveyor rejects the whole file on a bad min_lanes
        low = p["min_lanes"]
        floor = _lanes(low, 0 if low == 0 else 1)
        if floor is None or floor > (top if top is not None else MAX_LANES_CAP):
            return None
    return Pace(present, gone, away, top, p["power_switch"])


def lane_target(presence: str, pace: Pace | None) -> int | None:
    """The lane count pace gives for this presence; None when unknown."""
    if pace is None or presence not in STATES:
        return None
    return pace.max_lanes or (pace.lanes_present if presence == PRESENT else pace.lanes_away)


@dataclass(frozen=True)
class State:
    """Hysteresis state. The conveyor starts present and never persists it."""

    presence: str = PRESENT
    pending: str | None = None
    pending_since: float | None = None


def _valid(state: object) -> bool:
    if not isinstance(state, State) or state.presence not in STATES:
        return False
    if state.pending is None:
        return state.pending_since is None
    return state.pending in STATES and state.pending != state.presence and \
        _number(state.pending_since) is not None


@dataclass(frozen=True)
class Signal:
    """One read: presence present|away|unknown; changed only on the counted switch."""

    presence: str
    changed: bool = False
    was: str | None = None
    lane_target: int | None = None
    reason: str = ""  # why unknown: "state" | "pace" | "idle" | "clock"


def _unknown(state: State, reason: str) -> tuple[Signal, State]:
    """Presence kept; a pending change is dropped, so a hold never spans an unseen read."""
    return Signal(UNKNOWN, reason=reason), State(state.presence)


def sense(state: State, idle_s: object, pace: Pace | None, now: object) -> tuple[Signal, State]:
    """One presence read (the conveyor's sense_presence, minus every side effect)."""
    if not _valid(state):  # returned as is: stays unknown until the caller reseeds
        return Signal(UNKNOWN, reason="state"), state
    if pace is None:
        return _unknown(state, "pace")
    idle, t = _number(idle_s), _number(now)
    if idle is None:
        return _unknown(state, "idle")
    if t is None or (state.pending_since is not None and t < state.pending_since):
        return _unknown(state, "clock")
    if idle >= pace.away_after_min * 60:
        raw = AWAY
    elif idle < PRESENT_IDLE_S:
        raw = PRESENT
    else:
        raw = state.presence
    if raw == state.presence:
        return Signal(raw, lane_target=lane_target(raw, pace)), State(raw)
    if state.pending != raw:
        state = State(state.presence, raw, t)
    if t - cast(float, state.pending_since) < PRESENCE_HOLD_S:
        return Signal(state.presence, lane_target=lane_target(state.presence, pace)), state
    return Signal(raw, True, state.presence, lane_target(raw, pace)), State(raw)


def state_from_events(events: Iterable[object]) -> State | None:
    """Seed from the conveyor's last "presence" event (present when none); None if malformed.

    Gap: the conveyor restarts at present without an event, so a seed of away can be stale.
    """
    presence = PRESENT
    for rec in events:
        if not isinstance(rec, dict):
            return None
        r = cast("dict[str, object]", rec)
        if r.get("event") != "presence":
            continue
        if r.get("state") not in STATES or r.get("was") not in STATES or r["state"] == r["was"]:
            return None
        presence = cast(str, r["state"])
    return State(presence)


@dataclass(frozen=True)
class PowerProposal:
    """A plan switch for a human to approve. Never executed; the plan is allowlisted."""

    presence: str
    plan: str
    would_switch: bool  # pace power_switch: the conveyor itself switches only when true
    needs_approval: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        if self.presence not in STATES or POWER_PLANS[self.presence] != self.plan:
            raise ValueError(f"plan {self.plan!r} not allowlisted for {self.presence!r}")


def propose_power(signal: Signal, pace: Pace | None) -> PowerProposal | None:
    """A proposal on a counted switch to present/away only; None otherwise or when unknown."""
    if pace is None or not signal.changed or signal.presence not in STATES:
        return None
    return PowerProposal(signal.presence, POWER_PLANS[signal.presence], pace.power_switch)


@dataclass(frozen=True)
class Comparison:
    """One shadow verdict: N6 vs the conveyor on one pass (tick)."""

    tick: int
    kind: str  # "presence" | "priority"
    verdict: str  # one of VERDICTS
    n6: str | None
    conveyor: str | None


def _transition(signal: Signal) -> str | None:
    return f"{signal.was}->{signal.presence}" if signal.changed else None


def shadow_compare(ticks: Sequence[tuple[Signal, Sequence[object]]]) -> tuple[Comparison, ...]:
    """Agree/disagree per pace pass: N6's signal vs the conveyor events of that same pass.

    presence: N6's counted switch vs the conveyor's "presence" event (or none on both).
    priority: each "priority" event's presence vs N6's presence. N6 unknown -> "unknown"
    (agree on "presence" when the conveyor also logged presence_unknown); a malformed
    event -> "unknown". Reads nothing and writes nothing.
    """
    out: list[Comparison] = []
    for i, (signal, events) in enumerate(ticks):
        if not all(isinstance(e, dict) and isinstance(cast("dict[str, object]", e).get("event"), str)
                   for e in events):
            out.append(Comparison(i, "presence", "unknown", signal.presence, None))
            continue
        recs = [cast("dict[str, object]", e) for e in events]
        kinds = [r["event"] for r in recs]
        moves = [r for r in recs if r["event"] == "presence"]
        if signal.presence == UNKNOWN:
            seen = "presence_unknown" if "presence_unknown" in kinds else None
            out.append(Comparison(i, "presence", "agree" if seen else "unknown", UNKNOWN, seen))
        elif len(moves) > 1 or any(m.get("state") not in STATES or m.get("was") not in STATES
                                   or m.get("state") == m.get("was") for m in moves):
            out.append(Comparison(i, "presence", "unknown", _transition(signal), None))
        else:
            conv = f"{moves[0].get('was')}->{moves[0].get('state')}" if moves else None
            mine = _transition(signal)
            out.append(Comparison(i, "presence", "agree" if mine == conv else "disagree", mine, conv))
        for r in recs:
            if r["event"] != "priority":
                continue
            want = r.get("presence")
            if signal.presence == UNKNOWN or want not in STATES:
                verdict = "unknown"
            else:
                verdict = "agree" if want == signal.presence else "disagree"
            out.append(Comparison(i, "priority", verdict, signal.presence,
                                  want if isinstance(want, str) else None))
    return tuple(out)
