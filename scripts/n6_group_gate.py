# pyright: strict
"""N6GROUPGATE1: Phase 4 readiness check per behaviour group (N6_KIT.md "Phase 4: gradual takeover").

``check(shadow, e8, drills, taken_over)`` returns READY or NOT_READY with reasons for every group
in ``GROUPS`` order. A group is READY only when all of these hold:
- at least ``MIN_EVENTS`` distinct real shadow events (origin exactly "live"; fixture, rehearsal,
  replay, sweep or a missing origin never count);
- zero unsafe disagreements (n6 and conveyor decisions differ and ``safe`` is not True), counted
  over every row of the group whatever its origin;
- an E8 reading over at least ``E8_WINDOW`` slices with no measure worse than the conveyor;
- a passing R34 fallback drill receipt for the group;
- every earlier group has already taken over (R19R20 is last).

Input shapes (the shadow helpers n6_ci_watch / n6_cleanup / n6_intseq are not on main, so they
are defined here): a shadow row is {group, event_id, n6_decision, conveyor_decision, safe, origin};
an E8 reading is {"slices": int, "worse": [measure, ...]} per group; a drill receipt is
{group, passed}. An unknown group id anywhere raises ``GateError``; so does a malformed row or
a later group marked taken over before an earlier one; shadow, drills and taken_over must be lists
(of objects / of group ids), e8 an object, and both decisions non-empty strings. Read-only: stdlib only, no DB, no network,
nothing acts on the conveyor. CLI exit: 0 printed, 2 refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

GROUPS = ("R17R18", "R21", "R31", "R26", "R10", "R19R20")  # takeover order; R19R20 last
MIN_EVENTS = 5
E8_WINDOW = 10
REAL_ORIGIN = "live"
READY = "READY"
NOT_READY = "NOT_READY"
_ROW_KEYS = ("group", "event_id", "n6_decision", "conveyor_decision", "safe")


class GateError(ValueError):
    """Input the gate refuses to judge (unknown group, malformed row, inconsistent order)."""


def _group(value: object, where: str) -> str:
    if not isinstance(value, str) or value not in GROUPS:
        raise GateError(f"unknown group id in {where}: {value!r}")
    return value


def _e8_reasons(reading: object) -> list[str]:
    if reading is None:
        return ["no E8 reading"]
    if not isinstance(reading, Mapping):
        return ["E8 reading is malformed"]
    slices, worse = reading.get("slices"), reading.get("worse")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if type(slices) is not int or not isinstance(worse, (list, tuple)):
        return ["E8 reading is malformed"]
    reasons: list[str] = []
    if slices < E8_WINDOW:
        reasons.append(f"E8 window has {slices} slices, needs {E8_WINDOW}")
    if worse:
        reasons.append(f"E8 worse than conveyor: {' '.join(str(w) for w in worse)}")  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return reasons


def _list(value: object, where: str) -> Sequence[Any]:
    # a dict/str would iterate by key/char: {"R17R18": false} once read as "taken over"
    if not isinstance(value, (list, tuple)):
        raise GateError(f"{where} is not a list: {type(value).__name__}")
    return value  # pyright: ignore[reportUnknownVariableType]


def _mappings(value: object, where: str) -> list[Mapping[str, Any]]:
    items = _list(value, where)
    if not all(isinstance(item, Mapping) for item in items):
        raise GateError(f"{where} holds an entry that is not an object")
    return list(items)


def check(shadow: Sequence[Mapping[str, Any]], e8: Mapping[str, Any],
          drills: Sequence[Mapping[str, Any]],
          taken_over: Sequence[str] = ()) -> dict[str, tuple[str, list[str]]]:
    """{group: (READY | NOT_READY, reasons)} for every group, in takeover order."""
    done = {_group(g, "taken_over") for g in _list(taken_over, "taken_over")}
    for i, g in enumerate(GROUPS):
        if g in done and any(earlier not in done for earlier in GROUPS[:i]):
            raise GateError(f"{g} is marked taken over before an earlier group")
    if not isinstance(e8, Mapping):
        raise GateError(f"e8 is not an object: {type(e8).__name__}")
    for g in e8:
        _group(g, "e8")
    drilled: set[str] = set()
    failed_drill: set[str] = set()  # any failing receipt outweighs a pass (fail closed)
    for receipt in _mappings(drills, "drills"):
        g = _group(receipt.get("group"), "drill receipt")
        (drilled if receipt.get("passed") is True else failed_drill).add(g)
    drilled -= failed_drill
    real: dict[str, set[str]] = {g: set() for g in GROUPS}
    unsafe: dict[str, list[str]] = {g: [] for g in GROUPS}
    for row in _mappings(shadow, "shadow"):
        missing = [k for k in _ROW_KEYS if k not in row]
        if missing:
            raise GateError(f"shadow row lacks {' '.join(missing)}")
        g, event = _group(row["group"], "shadow row"), row["event_id"]
        if not isinstance(event, str) or not event.strip():
            raise GateError(f"shadow row event_id is missing or malformed: {event!r}")
        # str only: JSON true == 1 would hide a disagreement
        if any(not isinstance(row[k], str) or not row[k] for k in ("n6_decision", "conveyor_decision")):
            raise GateError(f"shadow row {event!r} has an empty or non-text decision")
        if row.get("origin") == REAL_ORIGIN:
            real[g].add(event)
        if row["n6_decision"] != row["conveyor_decision"] and row["safe"] is not True:
            unsafe[g].append(event)
    result: dict[str, tuple[str, list[str]]] = {}
    for i, g in enumerate(GROUPS):
        reasons = [f"earlier group {e} has not taken over" for e in GROUPS[:i] if e not in done]
        if len(real[g]) < MIN_EVENTS:
            reasons.append(f"{len(real[g])} real shadow events, needs {MIN_EVENTS}")
        if unsafe[g]:
            reasons.append(f"unsafe disagreement {len(unsafe[g])}: {' '.join(unsafe[g])}")
        reasons += _e8_reasons(e8.get(g))
        if g not in drilled:
            reasons.append("no passing R34 fallback drill receipt")
        result[g] = (NOT_READY if reasons else READY, reasons)
    return result


def render(result: Mapping[str, tuple[str, list[str]]]) -> str:
    lines: list[str] = []
    for g, (verdict, reasons) in result.items():
        lines.append(_escape(f"{g}: {verdict}"))
        lines += [_escape(f"  REASON: {r}") for r in reasons]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4 readiness per behaviour group (read-only)")
    parser.add_argument("--input", required=True, type=Path,
                        help='JSON {"shadow": [...], "e8": {...}, "drills": [...], "taken_over": [...]}')
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise GateError("input is not a JSON object")
        result = check(data.get("shadow", []), data.get("e8", {}),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
                       data.get("drills", []), data.get("taken_over", []))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    except (OSError, ValueError, AttributeError, TypeError, RecursionError) as error:
        print(_ascii(_escape(f"REFUSED: {error}")))
        return 2
    print(_ascii(render(result)), end="")
    return 0


def _escape(text: str) -> str:
    # one output line per call: an id holding newline, CR, U+2028, ESC... would forge a "<group>: READY" line;
    # the backslash is escaped too so a literal backslash-n id stays distinct from a real newline
    return "".join(c if c.isprintable() and c != "\\" else repr(c)[1:-1] for c in text)


def _ascii(text: str) -> str:
    # untrusted ids may hold lone surrogates or chars a cp1254 console cannot encode
    return text.encode("ascii", "backslashreplace").decode("ascii")


if __name__ == "__main__":
    sys.exit(main())
