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

from jarvis.n6_kpi import SOURCES, KpiError, KpiRow

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
        if not isinstance(self.kind, str) or self.kind not in KINDS:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StopInputError(f"unknown event kind {self.kind!r}")
        if not isinstance(self.source, str) or self.source not in SOURCES:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise StopInputError(f"event source must be one of {SOURCES}, got {self.source!r}")


@dataclass(frozen=True)
class Verdict:
    decision: str
    reason: str


def _pct(name: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= 100:
        raise StopInputError(f"{name} must be a number 0..100 or None, got {value!r}"[:120])
    return float(value)


def _items(name: str, value: object, kind: type) -> tuple[object, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StopInputError(f"{name} must be a sequence or None")
    items = tuple(value)  # pyright: ignore[reportUnknownArgumentType]
    for item in items:
        # Re-run the dataclass checks: a frozen instance can still be forged with object.__setattr__.
        if type(item) is not kind:
            raise StopInputError(f"malformed {name} item: {item!r}"[:120])
        item.__post_init__()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    return items


def evaluate(
    *, rows: Sequence[KpiRow] | None, events: Sequence[Event] | None,
    usage_pct: float | None, n6_share_pct: float | None,
) -> Verdict:
    """The stop-rule verdict. ``None`` = missing / unreadable / unknown. ``events`` is oldest first."""
    try:
        checked_rows = _items("rows", rows, KpiRow)
        checked_events = _items("events", events, Event)
    except KpiError as error:  # a forged row that fails its own checks
        raise StopInputError(str(error)[:120]) from error
    usage = _pct("usage_pct", usage_pct)
    share = _pct("n6_share_pct", n6_share_pct)
    kpi = [r for r in checked_rows or () if isinstance(r, KpiRow)]
    seen = [e for e in checked_events or () if isinstance(e, Event)]

    hits: set[str] = set()
    for r in kpi:
        if r.security_incidents > 0:  # any source: stopping N6 is the safe side
            hits.add(SECURITY_INCIDENT)
        # Mirrors scripts/n6_e8_report.py false_pass: an N6 PASS later shown wrong.
        if r.source == "n6" and r.passed and r.post_accept_defects > 0:
            hits.add(FALSE_PASS)
    for e in seen:
        if e.kind in STOP_KINDS and not (e.kind == FALSE_PASS and e.source != "n6"):
            hits.add(e.kind)
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
    for e in seen:
        if e.source == "n6" and e.kind in RESULT_KINDS:  # conveyor results neither count nor reset
            streak = streak + 1 if e.kind == "blocked" else 0
    if streak >= BLOCKED_STREAK:
        return Verdict(PAUSE, BLOCKED_RUN)
    if share is None:
        return Verdict(PAUSE, SHARE_UNKNOWN)
    if share >= N6_SHARE_CAP_PCT:
        return Verdict(PAUSE, SHARE_CAP)
    return Verdict(CONTINUE, OK)
