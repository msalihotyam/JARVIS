"""Focus label text for the PC title and the Telegram focus line (design 10).

Pure: plain mappings in, one-line text or a typed Refusal out. A focus is
{"project": str | None (optional), "topic": str}; other keys are ignored.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import unicodedata

NAME_CAP = 40
SEPARATOR = " · "
ARROW = "→ "
ELLIPSIS = "…"
# Lookalikes so a name never forms a slash command, URL or @mention.
_INERT = str.maketrans({
    "/": "∕", "\\": "∖", "@": "＠", ".": "․", ":": "꞉",
})
_DROP = ("Cf", "Cs", "Co", "Cn")      # bidi/zero-width controls, surrogates, unassigned
_SPACE = ("Cc", "Zs", "Zl", "Zp")     # newline, tab, NUL and other breaks -> one space


@dataclass(frozen=True)
class Refusal:
    reason: str


def _clean(name):
    if not isinstance(name, str):
        raise _Refused("name_not_str")
    out = []
    for ch in unicodedata.normalize("NFC", name):
        cat = unicodedata.category(ch)
        if cat in _SPACE:
            out.append(" ")
        elif cat not in _DROP:
            out.append(ch)
    cleaned = " ".join("".join(out).split())
    if not cleaned:
        raise _Refused("name_empty")
    return cleaned


def _display(cleaned):
    text = cleaned.translate(_INERT)
    if len(text) > NAME_CAP:
        text = text[:NAME_CAP - 1].rstrip() + ELLIPSIS
    return text


def _key(cleaned):
    # Turkish I/İ/ı/i fold together so a case-only change is never a focus change.
    folded = "".join("i" if ch in "Iİı" else ch for ch in cleaned)
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", folded).casefold())


class _Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _parts(focus):
    if not isinstance(focus, Mapping):
        raise _Refused("focus_not_mapping")
    if "topic" not in focus:
        raise _Refused("topic_missing")
    topic = _clean(focus["topic"])
    project = focus.get("project")
    return (None if project is None else _clean(project)), topic


def _label(project, topic):
    if project is None:
        return _display(topic)
    return _display(project) + SEPARATOR + _display(topic)


def _guarded(fn):
    def run(*args):
        try:
            return fn(*args)
        except _Refused as refused:
            return Refusal(refused.reason)
        except RecursionError:
            return Refusal("input_too_deep")
        except Exception:  # fail closed: a hostile mapping never escapes
            return Refusal("input_invalid")
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


@_guarded
def pc_title(focus):
    """PC window title: "project · topic", or the topic alone."""
    return _label(*_parts(focus))


@_guarded
def telegram_line(old, new):
    """"→ <label>" when the focus changed, None when it did not.

    old is None before the first focus.
    """
    project, topic = _parts(new)
    if old is not None:
        old_project, old_topic = _parts(old)
        same_project = (old_project is None and project is None) or (
            old_project is not None and project is not None
            and _key(old_project) == _key(project))
        if same_project and _key(old_topic) == _key(topic):
            return None
    return ARROW + _label(project, topic)
