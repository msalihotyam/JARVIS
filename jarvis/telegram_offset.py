"""TGPOLL1: pure Telegram getUpdates offset bookkeeping.

From one getUpdates batch and the stored offset, return the updates to handle
exactly once (ascending update_id) and the next offset. No I/O: the caller
fetches the batch and writes the state string. Every bad input comes back as a
typed Refusal with an ASCII reason code; no exception escapes. A Refusal from
process_batch means: handle nothing and keep the stored offset.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

MAX_BATCH = 1000
MAX_UPDATE_ID = 2**63 - 1  # Telegram ids are signed 64-bit; 2**63 and above is refused
MAX_OFFSET = MAX_UPDATE_ID + 1
MAX_STATE_CHARS = 64
STATE_VERSION = 1


@dataclass(frozen=True)
class Refusal:
    reason: str

    def __str__(self) -> str:
        return "refused: " + self.reason


@dataclass(frozen=True)
class Handled:
    update_id: int
    update: Mapping
    late: bool


@dataclass(frozen=True)
class PollResult:
    updates: tuple[Handled, ...]
    next_offset: int
    refused: tuple[tuple[int, str], ...]  # (batch index, reason code)
    replayed: int
    duplicates: int


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _update_id(entry: object) -> int | str:
    """Return the valid id, or a reason code."""
    if not isinstance(entry, Mapping):
        return "update_not_mapping"
    if "update_id" not in entry:
        return "update_id_missing"
    uid = entry["update_id"]
    if not _is_int(uid):
        return "update_id_type"
    if uid < 0:
        return "update_id_negative"
    if uid > MAX_UPDATE_ID:
        return "update_id_too_large"
    return uid


def _is_late(entry: Mapping, now: int, max_age: int) -> bool:
    # The payload (message, edited_message, ...) is the one mapping value carrying an int "date".
    for value in entry.values():
        if isinstance(value, Mapping):
            date = value.get("date")
            if _is_int(date):
                return now - date > max_age
    return False


def process_batch(batch: object, offset: object, *, now: object, max_age: object) -> PollResult | Refusal:
    try:
        return _process(batch, offset, now, max_age)
    except (RecursionError, MemoryError):
        return Refusal("input_too_deep")
    except Exception:  # fail closed: a hostile mapping must not crash the poller
        return Refusal("input_unreadable")


def _process(batch: object, offset: object, now: object, max_age: object) -> PollResult | Refusal:
    if not _is_int(offset) or not 0 <= offset <= MAX_OFFSET:
        return Refusal("bad_offset")
    if not _is_int(now) or not _is_int(max_age) or max_age < 0:
        return Refusal("bad_clock")
    if not isinstance(batch, list):
        return Refusal("batch_not_list")
    if len(batch) > MAX_BATCH:
        return Refusal("batch_too_large")
    refused: list[tuple[int, str]] = []
    kept: dict[int, Mapping] = {}
    replayed = duplicates = 0
    for index, entry in enumerate(batch):
        try:
            uid = _update_id(entry)
        except (RecursionError, MemoryError):
            uid = "update_too_deep"
        except Exception:
            uid = "update_unreadable"
        if isinstance(uid, str):
            refused.append((index, uid))
        elif uid < offset:
            replayed += 1
        elif uid in kept:
            duplicates += 1
        else:
            kept[uid] = entry
    handled = []
    for uid in sorted(kept):
        try:
            late = _is_late(kept[uid], now, max_age)
        except Exception:  # unreadable date: still delivered, never silently lost
            late = False
        handled.append(Handled(uid, kept[uid], late))
    next_offset = max(offset, handled[-1].update_id + 1) if handled else offset
    return PollResult(tuple(handled), next_offset, tuple(refused), replayed, duplicates)


def dump_state(offset: int) -> str | Refusal:
    if not _is_int(offset) or not 0 <= offset <= MAX_OFFSET:
        return Refusal("bad_offset")
    return json.dumps({"offset": offset, "v": STATE_VERSION}, sort_keys=True, separators=(",", ":"))


def _no_duplicate_keys(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def load_state(text: object) -> int | Refusal:
    if not isinstance(text, str):
        return Refusal("state_type")
    if len(text) > MAX_STATE_CHARS:
        return Refusal("state_too_long")
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (RecursionError, MemoryError):
        return Refusal("state_too_deep")
    except (ValueError, TypeError):
        return Refusal("state_unparsable")
    if not isinstance(data, dict) or set(data) != {"offset", "v"}:
        return Refusal("state_fields")
    if not _is_int(data["v"]) or data["v"] != STATE_VERSION:
        return Refusal("state_version")
    offset = data["offset"]
    if not _is_int(offset) or not 0 <= offset <= MAX_OFFSET:
        return Refusal("bad_offset")
    return offset
