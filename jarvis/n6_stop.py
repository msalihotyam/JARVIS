# pyright: strict
"""N6STOP1: the kit section 5 stop rule as a pure evaluator.

``evaluate`` reads KPI rows (``jarvis.n6_kpi.KpiRow``), ordered events and two quota readings
and returns STOP, PAUSE or CONTINUE with one reason code. It acts on nothing: no file, network,
clock, breaker or conveyor call; the caller decides what a verdict does.

STOP (hand every lane to the conveyor, tell Salih): one security incident, one N6 false PASS,
a two-manager event, an acceptance-set hash mismatch. PAUSE (conveyor continues): weekly quota
at the line or inside the reserve, BLOCKED_STREAK consecutive N6 BLOCKED, N6's weekly quota
share at the cap. STOP outranks PAUSE. A missing input (``None``) or an unknown reading pauses,
never continues; a malformed input raises and gives no verdict at all. Thresholds are module
constants on purpose: a caller cannot loosen them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from jarvis.n6_kpi import SOURCES, KpiRow

QUOTA_LINE_PCT = 95  # kit 5: "95 until X1 is decided"
RESERVE_PCT = 5  # N6-04 reserve: pause once usage is inside the last 5%
BLOCKED_STREAK = 3  # consecutive N6 BLOCKED results (est.)
N6_SHARE_CAP_PCT = 15  # O63: N6's share of the weekly quota

STOP, PAUSE, CONTINUE = "STOP", "PAUSE", "CONTINUE"

# Reason codes, in priority order within each decision.
SECURITY_INCIDENT = "security_incident"
FALSE_PASS = "false_pass"
TWO_MANAGER = "two_manager"
ACCEPTANCE_HASH_MISMATCH = "acceptance_hash_mismatch"
INPUT_MISSING = "input_missing"
QUOTA_UNKNOWN = "quota_unknown"
QUOTA_LINE = "quota_line"
SHARE_UNKNOWN = "n6_share_unknown"
SHARE_CAP = "n6_share_cap"
BLOCKED_RUN = "blocked_streak"
OK = "ok"

RESULT_KINDS = frozenset({"pass", "blocked"})
STOP_KINDS = (SECURITY_INCIDENT, FALSE_PASS, TWO_MANAGER, ACCEPTANCE_HASH_MISMATCH)
KINDS = frozenset(STOP_KINDS) | RESULT_KINDS


class StopInputError(ValueError):
    """A malformed input: no verdict is given."""


@dataclass(frozen=True)
class Event:
    """One stop-rule event; ``source`` is whose run it is about ("n6" or "conveyor").

    So a conveyor check that finds an N6 PASS wrong is ``Event("false_pass", "n6")``.
    """

    kind: str
    source: str

    def __post_init__(self) -> None:
        for field, allowed in (("kind", KINDS), ("source", SOURCES)):
            value: object = getattr(self, field)
            # Exact str: a subclass can lie in __eq__/__ne__/__hash__ (or __repr__) and turn a STOP into CONTINUE.
            if type(value) is not str:
                raise StopInputError(f"event {field} must be a str")  # fixed text: a metaclass __name__ can raise
            if value not in allowed:
                raise StopInputError(f"unknown event {field} {value!r}"[:120])


@dataclass(frozen=True)
class Verdict:
    decision: str
    reason: str


# The fields evaluate reads, with their exact types; n6_kpi checks with isinstance, which a lying subclass passes.
_ROW_FIELDS = (("source", str), ("passed", bool), ("post_accept_defects", int), ("security_incidents", int))
_EVENT_FIELDS = (("kind", str), ("source", str))


def _pct(name: str, value: object) -> float | None:
    if value is None:
        return None
    # Exact int/float (bool excluded too): a subclass can lie in its comparisons. Identity, never
    # `in`/==: a metaclass __eq__ -> True passes a tuple membership test. Bounds before
    # isfinite: math.isfinite(10**400) raises OverflowError.
    exact = type(value) is int or type(value) is float
    if not exact or not 0 <= value <= 100 or not math.isfinite(value):  # pyright: ignore[reportOperatorIssue, reportArgumentType]
        # Fixed text: never read the foreign type's metadata (a metaclass __name__ can raise).
        raise StopInputError(f"{name} must be a number 0..100 or None")
    return float(value)  # pyright: ignore[reportArgumentType]


def _snapshot(name: str, value: object, kind: type,
              fields: tuple[tuple[str, type], ...]) -> tuple[tuple[object, ...], list[tuple[object, ...]]] | None:
    """The items and plain copies of their read fields. Runs no foreign code at all.

    evaluate snapshots rows AND events before any item's check runs, so no check (KpiRow.__post_init__
    reads recorded_at.tzinfo) can rewrite any item, earlier or later, before its copy is taken.
    """
    if value is None:
        return None
    # Exact list/tuple (identity, as in _pct): a Sequence subclass's __iter__ is foreign code,
    # bytearray/memoryview are not item lists.
    if type(value) is not list and type(value) is not tuple:
        raise StopInputError(f"{name} must be a list, a tuple or None")
    items = tuple(value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    copies: list[tuple[object, ...]] = []
    for item in items:  # pyright: ignore[reportUnknownVariableType]
        if type(item) is not kind:  # pyright: ignore[reportUnknownArgumentType]
            raise StopInputError(f"malformed {name} item")
        # Not getattr: an instance-dict key that is a str subclass with a colliding hash runs its __eq__.
        attrs: object = vars(item)  # pyright: ignore[reportUnknownArgumentType]
        if type(attrs) is not dict or any(type(k) is not str for k in attrs):  # pyright: ignore[reportUnknownVariableType]
            raise StopInputError(f"malformed {name} item")
        copy = tuple(attrs.get(f) for f, _ in fields)  # pyright: ignore[reportUnknownMemberType]
        if any(type(v) is not t for v, (_, t) in zip(copy, fields)):
            raise StopInputError(f"malformed {name} item: a field has the wrong type")
        copies.append(copy)
    return items, copies


def _check(name: str, snapshot: tuple[tuple[object, ...], list[tuple[object, ...]]] | None, kind: type) -> None:
    """Re-run the dataclass checks: a frozen instance can still be forged with object.__setattr__."""
    try:
        for item in snapshot[0] if snapshot else ():
            # Through the class: an instance attribute can shadow __post_init__ with a no-op.
            kind.__post_init__(item)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    except StopInputError:
        raise
    except Exception as error:  # KpiError, or anything foreign code raised: no verdict either way
        # Fixed text: the error's type is foreign and may raise on __name__.
        raise StopInputError(f"malformed {name} item: its check raised") from error


def evaluate(
    *, rows: Sequence[KpiRow] | None, events: Sequence[Event] | None,
    usage_pct: float | None, n6_share_pct: float | None,
) -> Verdict:
    """The stop-rule verdict. ``None`` = missing / unreadable / unknown. ``events`` is oldest first."""
    # Every item of both lists is copied before any check runs foreign code; the checks come after.
    row_snap = _snapshot("rows", rows, KpiRow, _ROW_FIELDS)
    event_snap = _snapshot("events", events, Event, _EVENT_FIELDS)
    _check("rows", row_snap, KpiRow)
    _check("events", event_snap, Event)
    checked_rows = row_snap[1] if row_snap else None
    checked_events = event_snap[1] if event_snap else None
    usage = _pct("usage_pct", usage_pct)
    share = _pct("n6_share_pct", n6_share_pct)
    kpi = [(str(s), bool(p), int(d), int(i)) for s, p, d, i in checked_rows or ()]  # pyright: ignore[reportArgumentType]
    seen = [(str(k), str(s)) for k, s in checked_events or ()]
    # The copies were taken before __post_init__ ran foreign code that could reset the item: check them too.
    if any(s not in SOURCES or d < 0 or i < 0 for s, _, d, i in kpi) or any(
            k not in KINDS or s not in SOURCES for k, s in seen):
        raise StopInputError("malformed rows or events item")

    hits: set[str] = set()
    for source, passed, defects, security in kpi:
        if security > 0:  # any source: stopping N6 is the safe side
            hits.add(SECURITY_INCIDENT)
        # Mirrors scripts/n6_e8_report.py false_pass: an N6 PASS later shown wrong.
        if source == "n6" and passed and defects > 0:
            hits.add(FALSE_PASS)
    for kind, source in seen:
        if kind in STOP_KINDS and not (kind == FALSE_PASS and source != "n6"):
            hits.add(kind)
    for reason in STOP_KINDS:
        if reason in hits:
            return Verdict(STOP, reason)

    if checked_rows is None or checked_events is None:
        return Verdict(PAUSE, INPUT_MISSING)
    if usage is None:
        return Verdict(PAUSE, QUOTA_UNKNOWN)
    if usage >= min(QUOTA_LINE_PCT, 100 - RESERVE_PCT):
        return Verdict(PAUSE, QUOTA_LINE)
    streak = 0
    for kind, source in seen:
        if source == "n6" and kind in RESULT_KINDS:  # conveyor results neither count nor reset
            streak = streak + 1 if kind == "blocked" else 0
    if streak >= BLOCKED_STREAK:
        return Verdict(PAUSE, BLOCKED_RUN)
    if share is None:
        return Verdict(PAUSE, SHARE_UNKNOWN)
    if share >= N6_SHARE_CAP_PCT:
        return Verdict(PAUSE, SHARE_CAP)
    return Verdict(CONTINUE, OK)
