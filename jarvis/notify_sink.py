"""Deliver project-mode outbox rows: one throttled toast, a bounded recent list.

Pure: no Qt, no database. ``fetch(after_id)`` returns outbox rows (dicts with
``outbox_id``, ``kind``, ``severity``) ordered by id, as
``SQLiteMemoryStore.notify_outbox_rows`` does. Display text never comes from
a row: it is looked up from the fixed Turkish texts by kind, so nothing a row
carries is ever rendered, let alone as markup.

The cursor lives in memory only: the first poll of a run marks every stored
row as seen (into the recent list) without a toast, so a restart never
replays earlier runs' toasts.
"""

from collections import deque
from collections.abc import Callable, Iterable
import time

from jarvis.notify import _POINTS

#: At most one toast per this many seconds; the rest collapse into one.
THROTTLE_S = 5.0
RECENT_MAX = 50

_RANK = {"low": 0, "normal": 1, "high": 2}
_PROGRESS = ("Proje ilerlemesi", "Proje üzerinde çalışılıyor.")
_UNKNOWN = ("Yeni bildirim", "Yeni bir proje bildirimi var.")


def display(kind: object) -> tuple[str, str]:
    """The fixed title and text for an outbox kind; never the kind itself."""
    if kind == "progress":
        return _PROGRESS
    if isinstance(kind, str) and kind in _POINTS:
        return _POINTS[kind][1:]
    return _UNKNOWN


class NotifySink:
    def __init__(self, fetch: Callable[[int], Iterable[dict]], clock: Callable[[], float] = time.monotonic):
        self._fetch = fetch
        self._clock = clock
        self.cursor: int | None = None
        self._recent: deque[dict] = deque(maxlen=RECENT_MAX)
        self._pending: list[dict] = []
        self._last_toast: float | None = None

    def poll(self) -> list[dict]:
        """New rows since the cursor, in id order, each at most once."""
        seeding = self.cursor is None
        last = self.cursor or 0
        fresh = []
        for row in sorted(self._fetch(last), key=_id):
            outbox_id = _id(row)
            if outbox_id <= last:                 # a duplicate, a replay or a malformed id
                continue
            last = outbox_id
            severity = row.get("severity")
            title, text = display(row.get("kind"))
            item = {"outbox_id": outbox_id, "importance": severity if severity in _RANK else "normal",
                    "title": title, "text": text}
            fresh.append(item)
            self._recent.append(item)
            if not seeding and _RANK[item["importance"]] >= _RANK["normal"]:
                self._pending.append(item)
        self.cursor = last
        return fresh

    def recent(self) -> list[dict]:
        """All levels, newest last, for an in-app list."""
        return list(self._recent)

    def toast(self) -> tuple[str, str, str] | None:
        """``(title, text, importance)`` to show now, or None (nothing / throttled)."""
        if not self._pending:
            return None
        now = self._clock()
        if self._last_toast is not None and now - self._last_toast < THROTTLE_S:
            return None
        pending, self._pending, self._last_toast = self._pending, [], now
        importance = max((p["importance"] for p in pending), key=_RANK.__getitem__)
        if len(pending) == 1:
            return pending[0]["title"], pending[0]["text"], importance
        return "JARVIS", f"{len(pending)} yeni bildirim", importance


def _id(row: dict) -> int:
    value = row.get("outbox_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1
