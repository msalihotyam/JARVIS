"""Ring status and heartbeat record (TALKSTATUS1, T1).

ring_state() turns activity flags into the one value the face ring shows,
needs_you() says whether Salih still owes an answer, and alive_record() /
parse_heartbeat() / last_alive() keep the short alive record a restart reads
(TALKRECOVER1).

Pure and deterministic; no I/O, no network, no jarvis import, no clock read
(callers pass `now` as integer epoch seconds). No exception escapes: bad
input returns a Refusal whose text is fixed ASCII and never echoes input.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# Highest precedence first; ring_state() returns the first raised flag.
RING_STATES = ("needs_you", "speaking", "thinking", "understanding", "listening", "idle")
_FLAGS = frozenset(RING_STATES[:-1])
WHO = frozenset({"salih", "jarvis", "system"})
UNKNOWN = "unknown"
MAX_TIME = 10**11 - 1  # 11 digits: year ~5138, keeps the record short
MAX_RECORD_CHARS = 96
_BOOT = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RECORD = re.compile(r"hb1 (0|[1-9][0-9]{0,10}) ([A-Za-z0-9_-]{1,64})")

REASONS = {
    "bad_flags": "flags must map known ring flags to bool",
    "bad_time": "time must be integer epoch seconds",
    "bad_boot": "boot id must be 1-64 letters, digits, _ or -",
    "bad_record": "heartbeat record is not valid",
    "bad_records": "records must be a list of strings",
    "bad_cards": "cards must be a list of approval card mappings",
    "bad_entries": "entries must be a list of log entry mappings",
    "bad_order": "entry ids must be unique",
    "internal": "status could not be computed",
}


@dataclass(frozen=True)
class Refusal:
    reason: str

    @property
    def message(self) -> str:
        return REASONS.get(self.reason, REASONS["internal"])


@dataclass(frozen=True)
class Heartbeat:
    at: int
    boot_id: str


class _Refuse(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _guard(fn, *args):
    try:
        return fn(*args)
    except _Refuse as stop:
        return Refusal(stop.reason)
    except Exception:  # RecursionError and hostile __eq__/__getitem__ included
        return Refusal("internal")


def _time(value) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TIME:
        raise _Refuse("bad_time")
    return value


def _boot(value) -> str:
    if type(value) is not str or not _BOOT.fullmatch(value):
        raise _Refuse("bad_boot")
    return value


def _items(value, reason) -> list:
    if type(value) not in (list, tuple):
        raise _Refuse(reason)
    return list(value)


def _snapshot(value, reason) -> dict:
    """Copy a mapping once with exact-str keys so later reads cannot change."""
    if not isinstance(value, Mapping):
        raise _Refuse(reason)
    out = {}
    for key, item in value.items():
        if type(key) is not str:
            raise _Refuse(reason)
        out[key] = item
    return out


def ring_state(flags: object) -> str | Refusal:
    """Return one of RING_STATES; contradictory flags are settled by precedence."""
    return _guard(_ring_state, flags)


def _ring_state(flags):
    raised = _snapshot(flags, "bad_flags")
    if not set(raised) <= _FLAGS or any(type(v) is not bool for v in raised.values()):
        raise _Refuse("bad_flags")
    return next((name for name in RING_STATES[:-1] if raised.get(name)), "idle")


def needs_you(cards: object, entries: object, now: object) -> bool | Refusal:
    """True while an approval card is open, or the latest needs_you JARVIS entry is unanswered.

    cards: mappings with "resolved" (bool) and optional "expires_at" (epoch
    seconds or None); a card is open when unresolved and now < expires_at.
    entries: mappings with "id" (int), "who" and optional "needs_you" (bool,
    JARVIS only). Order is the entry id, never list position or timestamp.
    """
    return _guard(_needs_you, cards, entries, now)


def _needs_you(cards, entries, now):
    now = _time(now)
    card_open = False
    for raw in _items(cards, "bad_cards"):
        card = _snapshot(raw, "bad_cards")
        resolved, expires = card.get("resolved"), card.get("expires_at")
        if type(resolved) is not bool or (expires is not None and type(expires) is not int):
            raise _Refuse("bad_cards")
        card_open = card_open or (not resolved and (expires is None or now < expires))
    asked = answered = -1
    seen = set()
    for raw in _items(entries, "bad_entries"):
        entry = _snapshot(raw, "bad_entries")
        eid, who, flag = entry.get("id"), entry.get("who"), entry.get("needs_you", False)
        if (type(eid) is not int or eid < 0 or type(who) is not str or who not in WHO
                or type(flag) is not bool or (flag and who != "jarvis")):
            raise _Refuse("bad_entries")
        if eid in seen:
            raise _Refuse("bad_order")
        seen.add(eid)
        if flag:
            asked = max(asked, eid)
        elif who == "salih":
            answered = max(answered, eid)
    return card_open or asked > answered


def alive_record(now: object, boot_id: object) -> str | Refusal:
    """Serialise one alive record: "hb1 <epoch seconds> <boot id>"."""
    return _guard(lambda: f"hb1 {_time(now)} {_boot(boot_id)}")


def parse_heartbeat(text: object) -> Heartbeat | Refusal:
    return _guard(_parse, text)


def _parse(text):
    if type(text) is not str or len(text) > MAX_RECORD_CHARS:
        raise _Refuse("bad_record")
    match = _RECORD.fullmatch(text)
    if not match:
        raise _Refuse("bad_record")
    return Heartbeat(int(match.group(1)), match.group(2))


def last_alive(records: object, now: object, boot_id: object) -> int | str | Refusal:
    """Epoch seconds of the last record, or UNKNOWN.

    Only the last record counts (file order is write order): if it is corrupt,
    later than now or from another boot id, the answer is UNKNOWN rather than
    an older record that would understate the gap.
    """
    return _guard(_last_alive, records, now, boot_id)


def _last_alive(records, now, boot_id):
    now, boot_id = _time(now), _boot(boot_id)
    rows = _items(records, "bad_records")
    if any(type(row) is not str for row in rows):
        raise _Refuse("bad_records")
    if not rows:
        return UNKNOWN
    beat = _parse_or_none(rows[-1])
    if beat is None or beat.boot_id != boot_id or beat.at > now:
        return UNKNOWN
    return beat.at


def _parse_or_none(text):
    beat = _guard(_parse, text)
    return beat if type(beat) is Heartbeat else None
