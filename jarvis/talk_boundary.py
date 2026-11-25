"""PC konuşma çekirdeğinin tek giriş sınırı (the single entry boundary).

Each raw port (pc inbox, keyed log, brain, view, clock) is wrapped here once.
A wrapper hands back only frozen records, the OK / EXISTS / FAILED / ABSENT
marks, a Refused value or UNKNOWN, and it never raises. Checks are exact type
first (no subclasses, bool is not int), ints 0 <= n < 2**63, a finite clock
that never runs backwards, length caps and no NUL. Any exception or wrong shape
is UNKNOWN. A port that can hang must time out by itself; its timeout error is
then UNKNOWN like any other exception.

Raw port contracts:
  inbox.take(limit)     -> list of {"id": str, "kind": "text" | "retry", "text": str}
  inbox.ack(raw)        -> anything; raw is the item's "id" value as given, or the
                           item itself when it has none (match it by identity)
  log.append(key, line) -> "ok" | "exists" | "failed" | "unknown"
  log.get(key)          -> line | "absent" | "unknown"
  log.read()            -> list of lines; a line is {"key", "kind", "text", "time"}
  brain.ask(text)       -> str
  view.show(model)      -> None
  clock.now()           -> int or float seconds, finite, never decreasing
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass

INT_CAP = 2**63
TEXT_CAP = 4000
REPLY_CAP = 8000
LOG_CAP = 100_000
MAX_PC_CAP = 100
BUDGET_CAP = 10_000
ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}", re.ASCII)
KEY_RE = re.compile(r"(?:in:pc|reply):(?:~[0-9a-f]{32}|[A-Za-z0-9_.-]{1,128})", re.ASCII)
IN_KINDS = ("text", "retry", "refused")
REPLY_KINDS = ("reply", "failed", "refused")
REFUSED_TEXT = "Bu ileti işlenemedi: biçimi geçersiz."
FAILED_TEXT = "cevap üretilemedi"


class _Mark:
    """A result mark; compared by identity only."""

    __slots__ = ("name",)
    refused = False

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


OK, EXISTS, FAILED, ABSENT, UNKNOWN = (_Mark(n) for n in ("OK", "EXISTS", "FAILED", "ABSENT", "UNKNOWN"))


@dataclass(frozen=True)
class Refused:
    """A value the boundary would not pass on. Inbox items keep a key part and an ack handle."""

    reason: str
    item_id: str = ""
    handle: int = -1
    refused = True


@dataclass(frozen=True)
class PcItem:
    item_id: str
    kind: str
    text: str
    handle: int
    refused = False


@dataclass(frozen=True)
class LogLine:
    key: str
    kind: str
    text: str
    time: int
    refused = False


@dataclass(frozen=True)
class Answer:
    text: str
    refused = False


@dataclass(frozen=True)
class Tick:
    now: int  # milliseconds
    refused = False


@dataclass(frozen=True)
class Config:
    max_pc: int
    budget: int
    refused = False


def _int(v):
    return type(v) is int and 0 <= v < INT_CAP


def _text(v, cap):
    return type(v) is str and len(v) <= cap and "\x00" not in v


def _tag(raw):
    """A stable key part for an item with no usable id."""
    try:
        blob = json.dumps(raw, sort_keys=True, default=repr)
    except Exception:  # a 5000-digit int, a raising repr, mixed keys: one shared tag
        blob = "unencodable"
    return "~" + hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _item(raw, handle):
    if type(raw) is dict:
        rid = raw.get("id")
        if type(rid) is str and ID_RE.fullmatch(rid):
            kind, text = raw.get("kind"), raw.get("text")
            good = (type(kind) is str and kind in ("text", "retry") and _text(text, TEXT_CAP)
                    and text.strip() != "" and (kind == "text" or ID_RE.fullmatch(text)))
            return PcItem(rid, kind, text, handle) if good else Refused("bad_field", rid, handle)
    return Refused("bad_id", _tag(raw), handle)


def _ack_value(raw):
    try:
        return raw["id"] if type(raw) is dict and "id" in raw else raw
    except Exception:  # a key whose __eq__ raises: hand the item itself back
        return raw


def _line(raw):
    """A raw line as a LogLine, or None."""
    if type(raw) is not dict or len(raw) != 4:
        return None
    key, kind, text, time = (raw.get(k) for k in ("key", "kind", "text", "time"))
    if not (type(key) is str and KEY_RE.fullmatch(key) and type(kind) is str
            and kind in (IN_KINDS if key.startswith("in:") else REPLY_KINDS)
            and _text(text, REPLY_CAP) and _int(time)):
        return None
    return LogLine(key, kind, text, time)


_APPEND = {"ok": OK, "exists": EXISTS, "failed": FAILED, "unknown": UNKNOWN}


class PcInbox:
    def __init__(self, raw):
        self._raw, self._acks, self._next = raw, {}, 0

    def take(self, limit):
        """A tuple of PcItem / Refused (each with an ack handle), or UNKNOWN."""
        try:
            got = self._raw.take(limit)
            if type(got) is not list:
                return UNKNOWN
            self._acks, out = {}, []
            for raw in got[:limit]:
                handle, self._next = self._next, self._next + 1
                self._acks[handle] = _ack_value(raw)
                try:
                    out.append(_item(raw, handle))
                except Exception:  # hostile keys: refuse the item, never the queue
                    out.append(Refused("bad_id", _tag(raw), handle))
            return tuple(out)
        except Exception:
            return UNKNOWN

    def ack(self, handle):
        try:
            if type(handle) is not int or handle not in self._acks:
                return Refused("bad_handle")
            self._raw.ack(self._acks.pop(handle))
            return OK
        except Exception:
            return UNKNOWN


class KeyedLog:
    def __init__(self, raw):
        self._raw = raw

    def append(self, line):
        """OK | EXISTS | FAILED | UNKNOWN."""
        try:
            got = self._raw.append(line.key, {"key": line.key, "kind": line.kind,
                                              "text": line.text, "time": line.time})
            return _APPEND.get(got, UNKNOWN) if type(got) is str else UNKNOWN
        except Exception:
            return UNKNOWN

    def get(self, key):
        """The LogLine under key, ABSENT or UNKNOWN."""
        try:
            got = self._raw.get(key)
            if type(got) is str:
                return ABSENT if got == "absent" else UNKNOWN
            line = _line(got)
            return line if line is not None and line.key == key else UNKNOWN
        except Exception:
            return UNKNOWN

    def read(self):
        """A tuple of the well-formed lines, or UNKNOWN. Bad lines are left out:
        the core asks get(key) before it calls the brain."""
        try:
            got = self._raw.read()
            if type(got) is not list or len(got) > LOG_CAP:
                return UNKNOWN
            out = []
            for raw in got:
                try:
                    line = _line(raw)
                except Exception:
                    line = None
                if line is not None:
                    out.append(line)
            return tuple(out)
        except Exception:
            return UNKNOWN


class Brain:
    def __init__(self, raw):
        self._raw = raw

    def ask(self, text):
        """Answer, Refused (bad reply) or UNKNOWN."""
        try:
            got = self._raw.ask(text)
            if _text(got, REPLY_CAP) and got.strip() != "":
                return Answer(got)
            return Refused("bad_answer")
        except Exception:
            return UNKNOWN


class View:
    def __init__(self, raw):
        self._raw = raw

    def publish(self, model):
        try:
            self._raw.show(model)
            return OK
        except Exception:
            return UNKNOWN


class Clock:
    def __init__(self, raw):
        self._raw, self._last = raw, 0

    def now(self):
        """Tick(ms) or UNKNOWN; never earlier than the last tick."""
        try:
            got = self._raw.now()
            if type(got) is float and math.isfinite(got) and 0 <= got < INT_CAP / 1000:
                got = int(got * 1000)
            elif type(got) is int and 0 <= got < INT_CAP // 1000:
                got = got * 1000
            else:
                return UNKNOWN
            if got < self._last:
                return UNKNOWN
            self._last = got
            return Tick(got)
        except Exception:
            return UNKNOWN


@dataclass(frozen=True)
class Ports:
    inbox: PcInbox
    log: KeyedLog
    brain: Brain
    view: View
    clock: Clock


def wrap(inbox, log, brain, view, clock):
    return Ports(PcInbox(inbox), KeyedLog(log), Brain(brain), View(view), Clock(clock))


def config(max_pc, budget):
    """Config, or Refused for a wrong type or range. A step must fit the fixed
    calls (clock, read, take, view), one whole good turn and two calls per item."""
    if not (type(max_pc) is int and 1 <= max_pc <= MAX_PC_CAP):
        return Refused("bad_max_pc")
    if not (type(budget) is int and 2 * max_pc + 8 <= budget <= BUDGET_CAP):
        return Refused("bad_budget")
    return Config(max_pc, budget)
