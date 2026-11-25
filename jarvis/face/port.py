"""FACEPORT1: the data contract between the PC window and the conversation service.

Pure: entries and status are plain mappings, the result is frozen data. No Qt, no talk_*,
no I/O. Nothing in the running app imports this module; it grants nothing. Builtins only,
because the FACESHELL1 static test caps what any jarvis/face module may import.
"""
from __future__ import annotations

RING_STATES = ("idle", "listening", "understanding", "thinking", "speaking", "needs_you")
MODES = ("sesli", "yazili")
WINDOW_ROWS = {"ring": 0, "small": 30, "large": 200}
CHOICES = ("approve", "reject")
BUTTONS = (("approve", "Onayla"), ("reject", "Reddet"))
WHO = {"salih": "Salih", "jarvis": "JARVIS", "system": "Sistem"}
KINDS = ("text", "voice", "image", "file", "transcript", "approval_card")
CHIP_KINDS = ("txt", "md", "csv", "json", "xlsx", "docx", "png", "jpg", "pdf", "webp")
UNSUPPORTED = "desteklenmeyen"
MAX_SEND_CHARS = 4000
MAX_TEXT_CHARS = 4000
MAX_LABEL_CHARS = 128
MAX_PATH_CHARS = 32767
MAX_SIZE = 2**53
COMMANDS = {
    "send_text": ("text",),
    "talk_start": (),
    "talk_stop": (),
    "attach_file": ("path",),
    "attach_screenshot": (),
    "set_mode": ("mode",),
    "card_tap": ("card_id", "choice"),
    "resize": ("size",),
}
# same invisible set as talk_card; category C* covers bidi, zero-width and control chars
_INVISIBLE = frozenset(
    [0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x17B4, 0x17B5]
    + list(range(0x180B, 0x1810))
    + list(range(0xFE00, 0xFE10))
    + list(range(0xE0100, 0xE01F0))
)


class _Record:
    """Frozen value record: fields are __slots__, later fields may have _defaults."""

    __slots__ = ()
    _defaults: dict = {}

    def __init__(self, *args, **kwargs):
        values = {**self._defaults, **dict(zip(self.__slots__, args)), **kwargs}
        if len(args) > len(self.__slots__) or set(values) != set(self.__slots__):
            raise TypeError(f"{type(self).__name__} takes fields {self.__slots__}")
        for name in self.__slots__:
            object.__setattr__(self, name, values[name])

    def __setattr__(self, name, value):
        raise AttributeError(f"{type(self).__name__} is frozen")

    def __delattr__(self, name):
        raise AttributeError(f"{type(self).__name__} is frozen")

    def _key(self):
        return tuple(getattr(self, name) for name in self.__slots__)

    def __eq__(self, other):
        return type(other) is type(self) and other._key() == self._key()

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return f"{type(self).__name__}{self._key()!r}"


class Refusal(_Record):
    __slots__ = ("reason",)


class Chip(_Record):
    __slots__ = ("name", "size", "kind")


class Row(_Record):
    __slots__ = ("entry_id", "kind", "who", "label", "text", "time", "chips", "card_id", "buttons")
    _defaults = {"chips": (), "card_id": None, "buttons": ()}


class ViewModel(_Record):
    __slots__ = ("rows", "ring", "focus_label", "mode", "window", "open_cards")
    _defaults = {"open_cards": frozenset()}


class Command(_Record):
    __slots__ = ("name", "text", "path", "mode", "card_id", "choice", "size")
    _defaults = dict.fromkeys(("text", "path", "mode", "card_id", "choice", "size"))


def clean(value, limit=MAX_TEXT_CHARS, newlines=True):
    """Display copy of value: no control/format/bidi/invisible char, at most limit chars."""
    if type(value) is not str:
        return ""
    out = []
    for ch in value[: limit * 4]:
        if (newlines and ch in "\n\t") or (ch.isprintable() and ord(ch) not in _INVISIBLE):
            out.append(ch)
        elif ch.isspace():
            out.append(" ")  # odd separators (NBSP, line separator) still split words
    return "".join(out)[:limit]


def _time(ts):
    """HH:MM as written in an ISO timestamp (no zone conversion), else ""."""
    if type(ts) is not str or not 16 <= len(ts) <= 64 or ts[10] not in "T " or ts[13] != ":":
        return ""
    hh, mm = ts[11:13], ts[14:16]
    ok = (hh + mm).isascii() and (hh + mm).isdigit() and int(hh) < 24 and int(mm) < 60
    return f"{hh}:{mm}" if ok else ""


def _known(value, choices):
    """Exact str in choices: a str subclass with its own __eq__ never matches."""
    return type(value) is str and value in choices


def _plain(mapping):
    """Exact dict with exact-str keys: a key subclass could alias a field by hash and __eq__."""
    return type(mapping) is dict and all(type(k) is str for k in mapping)


def _chip(attachment):
    if not _plain(attachment):
        return Chip("dosya", None, "file")
    raw = attachment.get("name")
    raw = raw if type(raw) is str else attachment.get("path")
    name = clean(raw, MAX_LABEL_CHARS * 4, newlines=False)
    name = name.replace("\\", "/").replace(":", "/").rsplit("/", 1)[-1].strip(" .")
    name = name[-MAX_LABEL_CHARS:] or "dosya"
    size = attachment.get("size")
    size = size if type(size) is int and 0 <= size <= MAX_SIZE else None
    kind = attachment.get("kind")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    kind = kind if _known(kind, CHIP_KINDS) else ext if ext in CHIP_KINDS else "file"
    return Chip(name, size, kind)


def _card_id(value):
    ok = (
        type(value) is str and value.startswith("card-") and len(value) <= MAX_LABEL_CHARS
        and value.isascii() and value.isprintable() and " " not in value
    )
    return value if ok else None


def _row(entry):
    entry_id = entry.get("id")
    entry_id = entry_id if type(entry_id) in (int, str) else None
    kind, who = entry.get("kind"), entry.get("who")
    time = _time(entry.get("ts"))
    if not (_known(kind, KINDS) and _known(who, WHO)):
        kind = who = None
    card_id = _card_id(entry.get("card_id")) if kind == "approval_card" else None
    if kind is None or (kind == "approval_card" and card_id is None):
        return Row(entry_id, "unsupported", WHO["system"], "", UNSUPPORTED, time)
    label = "Telefon" if who == "salih" and _known(entry.get("source"), ("telegram",)) else ""
    if kind == "transcript":
        label = "Anladığım:"
    attachments = entry.get("attachments")
    chips = tuple(_chip(a) for a in attachments) if type(attachments) in (list, tuple) else ()
    is_open = card_id is not None and _known(entry.get("state", "PENDING"), ("PENDING",))
    return Row(
        entry_id, kind, WHO[who], label, clean(entry.get("text")), time, chips,
        card_id, BUTTONS if is_open else (),
    )


def _safe_row(entry):
    try:
        if _plain(entry):
            return _row(entry)
    except Exception:  # nosec B110 - a hostile entry becomes a neutral row, never dropped
        pass
    return Row(None, "unsupported", WHO["system"], "", UNSUPPORTED, "")


def present(entries, status, focus, mode, window):
    """View model of the window's last N entries, or a Refusal. Never raises."""
    try:
        if type(entries) not in (list, tuple):  # a subclass could slice to more than N
            return Refusal("entries must be a list")
        if not _plain(status) or not _known(status.get("ring"), RING_STATES):
            return Refusal("unknown ring state")
        if focus is not None and type(focus) is not str:
            return Refusal("focus must be a string or none")
        if type(mode) is not str or mode not in MODES:
            return Refusal("unknown reply mode")
        if type(window) is not str or window not in WINDOW_ROWS:
            return Refusal("unknown window size")
        count = WINDOW_ROWS[window]
        rows = tuple(_safe_row(e) for e in entries[-count:]) if count else ()
        open_cards = frozenset(r.card_id for r in rows if r.buttons)
        return ViewModel(
            rows, status["ring"], clean(focus, MAX_LABEL_CHARS, newlines=False), mode, window,
            open_cards,
        )
    except Exception:
        return Refusal("view model could not be built")


def _command(command, view):
    if not _plain(command):  # a subclass or an aliasing key could hide or forge fields
        return Refusal("command must be a mapping")
    name = command.get("name")
    if type(name) is not str or name not in COMMANDS:
        return Refusal("unknown command")
    fields = COMMANDS[name]
    if set(command) != {"name", *fields}:
        return Refusal("unexpected or missing command fields")
    args = {f: command[f] for f in fields}
    if any(type(v) is not str for v in args.values()):
        return Refusal("command fields must be strings")
    if name == "send_text" and not (args["text"].strip() and len(args["text"]) <= MAX_SEND_CHARS):
        return Refusal("text must be non-empty and within the cap")
    if name == "attach_file" and not (args["path"] and len(args["path"]) <= MAX_PATH_CHARS):
        return Refusal("path must be non-empty and within the cap")
    if name == "set_mode" and args["mode"] not in MODES:
        return Refusal("unknown reply mode")
    if name == "resize" and args["size"] not in WINDOW_ROWS:
        return Refusal("unknown window size")
    if name == "card_tap":
        if args["choice"] not in CHOICES:
            return Refusal("unknown card choice")
        cards = view.open_cards if type(view) is ViewModel else None
        plain = type(cards) is frozenset and all(type(c) is str for c in cards)
        if not plain or args["card_id"] not in cards:
            return Refusal("card is not open in the view")
    return Command(name, **args)


def validate(command, view=None):
    """Typed Command for one window command, or a Refusal. Never raises."""
    try:
        return _command(command, view)
    except Exception:
        return Refusal("command could not be validated")


class FakeService:
    """UI-test stand-in: records commands, replays scripted view models in order."""

    def __init__(self, views=()):
        self.views = list(views)
        self.current = None
        self.commands = []
        self.refused = []

    def view(self):
        """Next scripted view model; the last one repeats; None when nothing is scripted."""
        if self.views:
            self.current = self.views.pop(0) if len(self.views) > 1 else self.views[0]
        return self.current

    def send(self, command):
        """Validate against the last replayed view; record the Command or the Refusal."""
        result = validate(command, self.current)
        (self.refused if isinstance(result, Refusal) else self.commands).append(result)
        return result
