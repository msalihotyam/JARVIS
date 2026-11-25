"""Per-view reply mode (Sesli | Yazili) read from the shared talk log.

Audio plays only where the request came from, so each view keeps its own mode.
Pure decisions over entries the caller passes in; no I/O, grants nothing.
"""

from dataclasses import dataclass

VIEWS = ("pc", "telegram")
SESLI = "sesli"
YAZILI = "yazili"
DEFAULT_MODE = YAZILI
_LABELS = {"Sesli cevap": SESLI, "Yazılı cevap": YAZILI}


def _is(value, expected):
    return type(value) is str and value == expected


@dataclass(frozen=True)
class Refusal:
    reason: str  # ASCII


@dataclass(frozen=True)
class ModeState:
    mode: str
    ignored: int  # mode-change entries skipped as invalid


def telegram_keyboard():
    """Persistent Telegram reply keyboard with the two mode buttons, as plain data."""
    return {"keyboard": [[{"text": label} for label in _LABELS]],
            "is_persistent": True, "resize_keyboard": True}


def parse_button(text):
    """Exact label -> mode-change candidate ("sesli"/"yazili"); anything else -> None."""
    # type() is str: a str subclass could override __eq__/__hash__.
    if type(text) is str and text in _LABELS:
        return _LABELS[text]
    return None


def change(view, mode):
    """A mode change as a talk-log record, or a Refusal."""
    if type(view) is not str or view not in VIEWS:
        return Refusal("unknown view")
    if type(mode) is not str or mode not in (SESLI, YAZILI):
        return Refusal("unknown mode")
    return {"kind": "system", "who": "salih", "view": view, "mode": mode}


def current_mode(entries, view):
    """Latest valid mode change for view by sequence (entry id), else the default."""
    if type(view) is not str or view not in VIEWS:
        return Refusal("unknown view")
    try:
        best, ignored = None, 0
        for entry in entries:
            if type(entry) is not dict or not _is(entry.get("kind"), "system") or "mode" not in entry:
                continue
            seq, entry_view, mode = entry.get("id"), entry.get("view"), entry.get("mode")
            # A log id is a positive 64-bit row id; anything else is not a sequence.
            if (type(seq) is not int or not 0 < seq < 2**63 or not _is(entry.get("who"), "salih")
                    or type(entry_view) is not str or entry_view not in VIEWS
                    or type(mode) is not str or mode not in (SESLI, YAZILI)):
                ignored += 1
                continue
            # Sequence decides, never the timestamp; >= lets a later duplicate id win.
            if entry_view == view and (best is None or seq >= best[0]):
                best = (seq, mode)
        return ModeState(best[1] if best else DEFAULT_MODE, ignored)
    except Exception:  # RecursionError included; nothing escapes
        return Refusal("entries unreadable")


def wants_voice(entry, entries):
    """True only when the mode of the entry's own view (its source) is sesli."""
    try:
        view = entry.get("source") if type(entry) is dict else None
        if type(view) is not str:
            return False
        state = current_mode(entries, view)
        return type(state) is ModeState and state.mode == SESLI
    except Exception:
        return False
