# pyright: strict
"""N6 importance filter in front of ``notify.NotificationCore`` (G18, matrix R22).

Every event is classified SALIH (pages the phone), DAILY (evening summary only)
or ORCHESTRATOR (never pages; the caller routes it to the attention list, O39).
Unknown kinds are ORCHESTRATOR, so nothing unexpected ever pages (fail closed).
An ask without a recommendation is refused (O21). At most ``ASKS_PER_DAY`` asks
are paged per Istanbul day; the rest stay pending for the next day's batch (O16).
No quiet hours (O71). One ``flush`` sends at most one phone message; each item
is first deduped through the NotificationCore ledger, so a replay never pages
twice. A message whose send raised is kept and resent once by the next flush.

State is a small JSON file whose path the caller passes in; writes are atomic
(temp file + ``os.replace``). A missing file is empty state; a malformed one
raises StateError and nothing is sent or overwritten. There is no lock: callers
serialize calls per state file. Time is always the ``at`` argument.
"""

from __future__ import annotations

import contextlib
import enum
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, astuple, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

from jarvis.notify import NotificationCore
from jarvis.work_controls import ISTANBUL

ASKS_PER_DAY = 4
MAX_CHARS = 1000

_SALIH_POINTS = {"ask": "needs_input", "job_finished": "done", "urgent": "interrupted"}
_DAILY = frozenset({"status", "progress", "slice_started", "slice_accepted"})
_LINK = re.compile(
    r"\S*://\S*|\bwww\.\S+|\b\d{1,3}(?:\.\d{1,3}){3}\b\S*|(?:[\w-]+\.)+[^\W\d_]{2,}\b(?:/\S*)?",
    re.IGNORECASE,
)
_DOTS = {0x3002: ".", 0xFF61: "."}
_KEYS = {"version", "pending", "daily", "ask_day", "asks", "outbox", "outbox_asks"}
_ITEM_KEYS = ("kind", "key", "text", "recommendation")


class Route(enum.Enum):
    SALIH = "salih"
    DAILY = "daily"
    ORCHESTRATOR = "orchestrator"


class StateError(ValueError):
    """The state file is unreadable or malformed; nothing is sent (fail closed)."""


class AskRefused(ValueError):
    """An ask to Salih must carry a recommendation (O21)."""


@dataclass(frozen=True)
class Event:
    kind: str
    key: str
    text: str
    recommendation: str = ""


@dataclass
class _State:
    pending: list[Event]
    daily: list[str]
    ask_day: str
    asks: int
    outbox: str
    outbox_asks: int = 0


def classify(kind: str) -> Route:
    if kind in _SALIH_POINTS:
        return Route.SALIH
    if kind in _DAILY:
        return Route.DAILY
    return Route.ORCHESTRATOR


def clean(text: str) -> str:
    """One line with no control/format characters and no link of any form."""
    text = unicodedata.normalize("NFKC", text).translate(_DOTS)
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in text)
    return " ".join(_LINK.sub(" ", text).split())


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _rec_ok(rec: str) -> bool:
    return 0 < len(clean(rec)) <= MAX_CHARS // 2


def _line(ev: Event) -> str:
    """One page line; the recommendation is never clipped away (O21)."""
    tail = " | Öneri: " + clean(ev.recommendation) if ev.kind == "ask" else ""
    return clean(_clip(clean(ev.text) or ev.kind, MAX_CHARS - len(tail)) + tail)


def _iso_day(day: str) -> bool:
    try:
        return date.fromisoformat(day).isoformat() == day
    except ValueError:
        return False


def _bad(why: str) -> StateError:
    return StateError(f"notify state malformed: {why}")


def _load(path: Path) -> _State:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _State([], [], "", 0, "")
    except (OSError, UnicodeError) as exc:
        raise _bad(str(exc)) from exc
    try:
        data = cast(object, json.loads(raw))
    except ValueError as exc:
        raise _bad("not JSON") from exc
    if not isinstance(data, dict):
        raise _bad("not an object")
    d = cast("dict[str, object]", data)
    version, pending_raw, daily_raw = d.get("version"), d.get("pending"), d.get("daily")
    day, asks, outbox = d.get("ask_day"), d.get("asks"), d.get("outbox")
    oasks = d.get("outbox_asks")
    if set(d) != _KEYS or type(version) is not int or version != 1:
        raise _bad("keys or version")
    if not isinstance(pending_raw, list) or not isinstance(daily_raw, list):
        raise _bad("pending/daily")
    if not isinstance(day, str) or not isinstance(outbox, str):
        raise _bad("ask_day/outbox")
    if type(asks) is not int or type(oasks) is not int or not 0 <= oasks <= asks:
        raise _bad("asks")
    if (day and not _iso_day(day)) or (not day and asks):
        raise _bad("ask_day")
    lines_ok = all(x and x == clean(x) for x in outbox.split("\n")) if outbox else not oasks
    if len(outbox) > MAX_CHARS or not lines_ok:
        raise _bad("outbox")
    daily = cast("list[object]", daily_raw)
    if not all(isinstance(x, str) and x and x == clean(x) for x in daily):
        raise _bad("daily item")
    pending: list[Event] = []
    for item in cast("list[object]", pending_raw):
        if not isinstance(item, dict):
            raise _bad("pending item")
        it = cast("dict[str, object]", item)
        vals = [it.get(k) for k in _ITEM_KEYS]
        if set(it) != set(_ITEM_KEYS) or not all(isinstance(v, str) for v in vals):
            raise _bad("pending item fields")
        ev = Event(*cast("list[str]", vals))
        if classify(ev.kind) is not Route.SALIH or not _valid(ev):
            raise _bad(f"pending item {ev.kind!r}")
        pending.append(ev)
    return _State(pending, cast("list[str]", daily), day, asks, outbox, oasks)


def _save(path: Path, s: _State) -> None:
    data = {
        "version": 1, "pending": [asdict(e) for e in s.pending], "daily": s.daily,
        "ask_day": s.ask_day, "asks": s.asks, "outbox": s.outbox, "outbox_asks": s.outbox_asks,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _valid(ev: Event) -> bool:
    return bool(ev.key.strip()) and (ev.kind != "ask" or _rec_ok(ev.recommendation))


def submit(state_path: Path | str, event: Event) -> Route:
    """Classify and store one event; ORCHESTRATOR items are never stored or paged."""
    fields: tuple[object, ...] = astuple(event)
    if not all(isinstance(v, str) for v in fields):
        raise ValueError("event fields must be str")
    route = classify(event.kind)
    if route is Route.ORCHESTRATOR:
        return route
    if event.kind == "ask" and not _rec_ok(event.recommendation):
        raise AskRefused(f"ask {event.key!r} carries no usable recommendation")
    if not event.key.strip():
        raise ValueError("event key must not be empty")
    path = Path(state_path)
    s = _load(path)
    if route is Route.DAILY:
        if (line := clean(event.text)) and line not in s.daily:
            s.daily.append(line)  # ponytail: dedupe by text, store keys if distinct items collide
    elif all((p.kind, p.key) != (event.kind, event.key) for p in s.pending):
        s.pending.append(event)
    _save(path, s)
    return route


def flush(
    state_path: Path | str,
    *,
    core: NotificationCore,
    run_id: str,
    next_seq: Callable[[], int],
    at: datetime,
    send: Callable[[str], None],
) -> str | None:
    """Send at most one phone message holding every due SALIH item; return it."""
    if at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    path = Path(state_path)
    s = _load(path)
    day = at.astimezone(ISTANBUL).date().isoformat()
    if day > s.ask_day:  # a clock going backwards never resets the cap
        s.ask_day, s.asks = day, s.outbox_asks  # unsent asks count against the send day
    lines = [s.outbox] if s.outbox else []
    queue, s.pending = s.pending, []
    try:
        while queue:
            ev = queue[0]
            if ev.kind == "ask" and s.asks >= ASKS_PER_DAY:
                s.pending.append(queue.pop(0))
                continue
            line = _line(ev)
            if lines and len("\n".join([*lines, line])) > MAX_CHARS:
                break  # does not fit: waits for the next flush, never clipped away
            point = _SALIH_POINTS[ev.kind]
            if core.notify(run_id, point, f"{ev.kind}:{ev.key}", seq=next_seq(), at=at,
                           importance="high"):
                if ev.kind == "ask":
                    s.asks += 1
                    s.outbox_asks += 1
                lines.append(line)
            _ = queue.pop(0)
    finally:
        s.pending += queue
        s.outbox = "\n".join(lines)
        _save(path, s)
    if not s.outbox:
        return None
    text = s.outbox
    send(text)
    s.outbox, s.outbox_asks = "", 0
    _save(path, s)
    return text


def evening_summary(state_path: Path | str, deliver: Callable[[str], None]) -> str | None:
    """Hand the day's DAILY items to ``deliver`` as one text; clear them only after."""
    path = Path(state_path)
    s = _load(path)
    if not s.daily:
        return None
    text = _clip("\n".join(s.daily))
    deliver(text)
    s.daily = []
    _save(path, s)
    return text
