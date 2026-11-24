"""Brain session rotation: when to replace the session and what to hand over.

Pure module (design 6): plain mappings in, frozen values out. No I/O, no
network, no model calls; nothing in the running app imports it yet. Every
public function returns a Refusal instead of raising (fail closed).

Turn mapping fields:
  id             int >= 0, strictly increasing across the list
  role           str
  text           str
  topic          str or None (optional)
  pending        bool (optional) - entry still waiting for the brain's reply
  approval_open  bool (optional) - an open approval card; always handed over
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

KEEP = "KEEP"
ROTATE = "ROTATE"
DEFER = "DEFER"

DEFAULT_THRESHOLD = 0.7
MAX_TURNS = 50

# Fixed ASCII texts: refusals never echo caller data, so they print under any
# narrow stdout encoding.
REFUSALS = {
    "LIMIT_INVALID": "limit must be a finite number above zero",
    "THRESHOLD_INVALID": "threshold must be a finite number in (0, 1]",
    "TURNS_INVALID": "turns must be a list or tuple of mappings",
    "TURN_FIELD_INVALID": "a turn has a missing or wrongly typed field",
    "TURN_DUPLICATE_ID": "two turns share the same id",
    "TURN_OUT_OF_ORDER": "turn ids are not in increasing order",
    "STYLE_INVALID": "style reference must be a string",
    "SUMMARIES_INVALID": "summaries must map topic strings to summary strings",
    "SUMMARY_MISSING": "a topic touched by a turn has no summary",
    "BUDGET_INVALID": "budget must be an integer above zero",
    "BUDGET_TOO_SMALL": "style, summaries and open approval cards exceed the budget",
    "INPUT_TOO_DEEP": "input is nested too deeply",
    "INPUT_INVALID": "input could not be read",
}


@dataclass(frozen=True)
class Refusal:
    code: str
    text: str


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str


@dataclass(frozen=True)
class Bundle:
    style: str
    summaries: tuple  # ((topic, summary), ...) sorted by topic
    turns: tuple  # turn dicts in id order
    chars: int
    dropped: tuple  # ids of turns trimmed for the budget, oldest first


def _refuse(code):
    return Refusal(code, REFUSALS[code])


def _guarded(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RecursionError:
            return _refuse("INPUT_TOO_DEEP")
        except Exception:  # fail closed: no exception escapes a public call
            return _refuse("INPUT_INVALID")

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _turns(turns):
    """Normalized tuple of turn dicts, or a Refusal."""
    if not isinstance(turns, (list, tuple)):
        return _refuse("TURNS_INVALID")
    out = []
    for turn in turns:
        if not isinstance(turn, Mapping):
            return _refuse("TURNS_INVALID")
        tid, role, text = turn.get("id"), turn.get("role"), turn.get("text")
        topic = turn.get("topic")
        pending = turn.get("pending", False)
        card = turn.get("approval_open", False)
        if (
            not isinstance(tid, int) or isinstance(tid, bool) or tid < 0
            or not isinstance(role, str) or not isinstance(text, str)
            or not (topic is None or isinstance(topic, str))
            or not isinstance(pending, bool) or not isinstance(card, bool)
        ):
            return _refuse("TURN_FIELD_INVALID")
        out.append({"id": tid, "role": role, "text": text, "topic": topic,
                    "pending": pending, "approval_open": card})
    ids = [t["id"] for t in out]
    if len(set(ids)) != len(ids):
        return _refuse("TURN_DUPLICATE_ID")
    if ids != sorted(ids):
        return _refuse("TURN_OUT_OF_ORDER")
    return tuple(out)


@_guarded
def decide(used, limit, threshold=DEFAULT_THRESHOLD, turns=()):
    """KEEP | ROTATE | DEFER for the brain session, or a Refusal.

    A pending entry always defers (never rotate mid-turn). A bad usage reading
    rotates: it is never read as room to keep going.
    """
    if not _number(limit) or limit <= 0:
        return _refuse("LIMIT_INVALID")
    if not _number(threshold) or not 0 < threshold <= 1:
        return _refuse("THRESHOLD_INVALID")
    norm = _turns(turns)
    if isinstance(norm, Refusal):
        return norm
    if any(t["pending"] for t in norm):
        return Decision(DEFER, "TURN_PENDING")
    if not _number(used) or used < 0:
        return Decision(ROTATE, "USED_INVALID")
    if used > limit:
        return Decision(ROTATE, "USED_OVER_LIMIT")
    if used / limit >= threshold:
        return Decision(ROTATE, "AT_THRESHOLD")
    return Decision(KEEP, "BELOW_THRESHOLD")


@_guarded
def build_bundle(style, summaries, turns, budget):
    """Handoff bundle for the next brain session, or a Refusal.

    Size in characters = len(style) + len(topic) + len(summary) per summary +
    len(text) per turn. Only summaries of topics some turn touches (any turn,
    also outside the window) are handed over; untouched ones are left out and
    not counted. Style, those summaries and every open approval card are
    never cut (over budget -> refused); of the last MAX_TURNS turns the oldest
    are trimmed first until the bundle fits.
    """
    if not isinstance(style, str):
        return _refuse("STYLE_INVALID")
    if not isinstance(summaries, Mapping):
        return _refuse("SUMMARIES_INVALID")
    pairs = list(summaries.items())
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in pairs):
        return _refuse("SUMMARIES_INVALID")
    known = dict(pairs)  # one view of summaries: a Mapping's keys() may disagree with items()
    if len(known) != len(pairs):
        return _refuse("SUMMARIES_INVALID")
    pairs.sort()
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        return _refuse("BUDGET_INVALID")
    norm = _turns(turns)
    if isinstance(norm, Refusal):
        return norm
    touched = {t["topic"] for t in norm if t["topic"] is not None}
    if not touched <= known.keys():
        return _refuse("SUMMARY_MISSING")
    pairs = [(k, v) for k, v in pairs if k in touched]  # one summary per touched topic

    window = norm[-MAX_TURNS:]
    window_ids = {t["id"] for t in window}
    kept = [t for t in norm if t["id"] in window_ids or t["approval_open"]]
    size = len(style) + sum(len(k) + len(v) for k, v in pairs)
    size += sum(len(t["text"]) for t in kept if t["approval_open"])
    if size > budget:
        return _refuse("BUDGET_TOO_SMALL")
    size += sum(len(t["text"]) for t in kept if not t["approval_open"])
    dropped = []
    for t in kept:  # id order, so the oldest go first
        if size <= budget:
            break
        if not t["approval_open"]:
            dropped.append(t["id"])
            size -= len(t["text"])
    gone = set(dropped)
    final = tuple(dict(t) for t in kept if t["id"] not in gone)
    return Bundle(style, tuple(pairs), final, size, tuple(dropped))
