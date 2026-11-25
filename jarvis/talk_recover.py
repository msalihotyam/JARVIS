"""Restart recovery: what is unfinished, worked out from the log alone.

Pure module (ON specs-08, design 4): plain sequences in, a frozen value out.
No I/O, no network, no model calls; nothing in the running app imports it
yet. ``recover`` returns a Refusal instead of raising (fail closed).

Entry mapping fields (ids strictly increasing):
  id          int >= 0
  who         "salih" | "jarvis" | "system"
  kind        str ("approval_tap" is never queued for the brain)
  reply_to    int or None (optional) - a jarvis entry with it is a reply
  superseded  bool (optional) - a superseded entry is never queued
Delivery record fields (later record for one entry/view wins):
  entry_id    int, an id present in entries
  view        non-empty str
  state       one of STATES
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

STATES = ("pending", "sent", "failed", "gave_up", "unknown")
UNFINISHED_STATES = ("pending", "unknown")
WHO = ("salih", "jarvis", "system")
NEVER_QUEUED_KINDS = ("approval_tap",)
UNKNOWN = "unknown"
MAX_ITEMS = 100_000

# Fixed ASCII texts: refusals never echo caller data.
REFUSALS = {
    "bad_now": "now must be a finite number",
    "bad_threshold": "threshold must be a finite number >= 0",
    "bad_window": "window must be an int >= 1",
    "bad_entries": "entries must be a list of mappings",
    "bad_entry": "entry has an invalid field",
    "bad_id_order": "entry ids must be ints >= 0, strictly increasing, unique",
    "bad_deliveries": "deliveries must be a list of mappings",
    "bad_delivery": "delivery record has an invalid field",
    "unknown_delivery_entry": "delivery record names an entry not in the log",
    "too_many": "too many entries or delivery records",
    "bad_clock": "clock formatter did not return HH:MM",
    "bad_input": "input could not be read",
}


@dataclass(frozen=True)
class Refusal:
    code: str
    text: str


@dataclass(frozen=True)
class Recovery:
    brain_queue: tuple  # Salih entry ids with no reply, oldest first
    pending_deliveries: tuple  # (entry_id, view, state), state pending/unknown
    downtime: object  # seconds (float) or UNKNOWN
    note: object  # one line for the first reply, or None
    older_unanswered: int  # unanswered Salih entries outside the window
    older_undelivered: int  # pending/unknown deliveries outside the window
    duplicates: tuple  # later reply ids for an already answered entry
    orphans: tuple  # reply ids whose reply_to is not an earlier entry


class _Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _refuse(code):
    return Refusal(code, REFUSALS[code])


def _finite(value):
    """float(value) for a finite int/float (never bool), else None."""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _utc_hhmm(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%H:%M")


def _is_hhmm(text):
    return (
        type(text) is str and len(text) == 5 and text[2] == ":"
        and all(c in "0123456789" for c in text[:2] + text[3:])
    )


def _sequence(value, code):
    if not isinstance(value, (list, tuple)):
        raise _Refused(code)
    if len(value) > MAX_ITEMS:
        raise _Refused("too_many")
    return value


def _read_entries(entries):
    rows, previous = [], -1
    for raw in _sequence(entries, "bad_entries"):
        if not isinstance(raw, Mapping):
            raise _Refused("bad_entries")
        entry_id = raw.get("id")
        if type(entry_id) is not int or entry_id <= previous:
            raise _Refused("bad_id_order")
        previous = entry_id
        who, kind = raw.get("who"), raw.get("kind")
        reply_to, superseded = raw.get("reply_to"), raw.get("superseded", False)
        if (
            type(who) is not str or who not in WHO or type(kind) is not str
            or not (reply_to is None or type(reply_to) is int)
            or type(superseded) is not bool
        ):
            raise _Refused("bad_entry")
        rows.append((entry_id, who, kind, reply_to, superseded))
    return rows


def _read_deliveries(deliveries, known):
    latest = {}
    for raw in _sequence(deliveries, "bad_deliveries"):
        if not isinstance(raw, Mapping):
            raise _Refused("bad_deliveries")
        entry_id, view, state = raw.get("entry_id"), raw.get("view"), raw.get("state")
        if type(view) is not str or not view or type(state) is not str or state not in STATES:
            raise _Refused("bad_delivery")
        if type(entry_id) is not int:
            raise _Refused("bad_delivery")
        if entry_id not in known:
            raise _Refused("unknown_delivery_entry")
        latest[(entry_id, view)] = state
    return latest


def _recover(entries, deliveries, now, last_alive, threshold, window, hhmm):
    now_s = _finite(now)
    if now_s is None:
        raise _Refused("bad_now")
    limit = _finite(threshold)
    if limit is None or limit < 0:
        raise _Refused("bad_threshold")
    if type(window) is not int or window < 1:
        raise _Refused("bad_window")
    rows = _read_entries(entries)
    position = {row[0]: index for index, row in enumerate(rows)}
    latest = _read_deliveries(deliveries, position)
    first_in_window = max(0, len(rows) - window)

    answered, duplicates, orphans = set(), [], []
    for entry_id, who, _kind, reply_to, _sup in rows:
        if who != "jarvis" or reply_to is None:
            continue
        if reply_to not in position or reply_to >= entry_id:
            orphans.append(entry_id)
        elif reply_to in answered:
            duplicates.append(entry_id)
        else:
            answered.add(reply_to)

    queue, older_unanswered = [], 0
    for index, (entry_id, who, kind, _reply, superseded) in enumerate(rows):
        if who != "salih" or superseded or kind in NEVER_QUEUED_KINDS or entry_id in answered:
            continue
        if index >= first_in_window:
            queue.append(entry_id)
        else:
            older_unanswered += 1

    # DECISION: duplicate and orphan replies are never (re)delivered.
    withheld = set(duplicates) | set(orphans)
    pending, older_undelivered = [], 0
    for (entry_id, view), state in sorted(latest.items()):
        if state not in UNFINISHED_STATES or entry_id in withheld:
            continue
        if position[entry_id] >= first_in_window:
            pending.append((entry_id, view, state))
        else:
            older_undelivered += 1

    alive = _finite(last_alive)
    downtime = UNKNOWN if alive is None or alive > now_s else now_s - alive
    note = None
    if downtime != UNKNOWN and downtime > limit:
        stamp = hhmm(alive)
        if not _is_hhmm(stamp):
            raise _Refused("bad_clock")
        note = f"{stamp}'den beri kapalıydım"
    return Recovery(
        tuple(queue), tuple(pending), downtime, note, older_unanswered,
        older_undelivered, tuple(duplicates), tuple(orphans),
    )


def recover(entries, deliveries, now, last_alive, *, threshold, window, hhmm=_utc_hhmm):
    """Unfinished work after a restart; Recovery or Refusal, never raises."""
    try:
        return _recover(entries, deliveries, now, last_alive, threshold, window, hhmm)
    except _Refused as refused:
        return _refuse(refused.code)
    except Exception:  # RecursionError, hostile mappings, a raising hhmm
        return _refuse("bad_input")
