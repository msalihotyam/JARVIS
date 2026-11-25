"""Conversation log rotation: one append-only hash chain across numbered segments.

Pure module: bytes and names in, frozen values out. No I/O, no network, no
model calls; reading and writing files is the caller's job, and nothing in the
running app imports this module yet. Public functions never raise: they return
a Refusal (bad input) or a Break (the chain is broken), failing closed.

Segment k is named talk-<k>.jsonl, k >= 1, zero-padded to 4 digits (wider past
9999). Records use talk_log's line form {"hash": ..., "record": ...} sealed
from the previous record's hash; the chain runs on across segments. Segment 1
has no header; segment k >= 2 starts with exactly one header line naming the
previous segment, its last record hash and its record count. An old segment is
never rewritten: rolling only produces the next segment's name and header.
Record ids run 1, 2, 3, ... across all segments, as talk_log assigns them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

PREFIX = "talk-"
SUFFIX = ".jsonl"
WIDTH = 4
MAX_DIGITS = 9
LIMIT_MAX = 2**63 - 1
LIMIT_KEYS = frozenset({"max_bytes", "max_entries"})
GENESIS = "0" * 64
# [0-9], not \d: \d and int() also accept full-width and other Unicode digits.
_NAME = re.compile(r"talk-([0-9]{4,9})\.jsonl")

# Fixed ASCII texts: they never echo caller data.
REFUSALS = {
    "NAMES_INVALID": "names must be a list or tuple of strings",
    "NAME_INVALID": "a segment name is not a canonical talk-NNNN.jsonl name",
    "NAME_DUPLICATE": "two segment names are equal after case folding",
    "SEGMENT_GAP": "segment numbers have a gap",
    "SEGMENT_DUPLICATE": "segment number appears twice",
    "NAME_SPACE_FULL": "no segment number left",
    "LIMITS_INVALID": "limits must map max_bytes and max_entries to integers above zero",
    "COUNT_INVALID": "size and entries must be integers at or above zero",
    "SEGMENTS_INVALID": "segments must be a list or tuple of (name, bytes) pairs",
    "LINE_TORN": "segment does not end with a complete line",
    "HEADER_MISSING": "segment does not start with a header line",
    "HEADER_MISMATCH": "header does not match the previous segment",
    "TAMPER": "header line found where a record belongs",
    "RECORD_INVALID": "record line is not a sealed record",
    "HASH_MISMATCH": "record hash or encoding does not continue the chain",
    "ID_OUT_OF_ORDER": "record id does not follow the previous id",
    "SEGMENT_EMPTY": "a segment before the last holds no records",
    "INPUT_TOO_DEEP": "input is nested too deeply",
    "INPUT_INVALID": "input could not be read",
}


@dataclass(frozen=True)
class Refusal:
    code: str
    text: str


@dataclass(frozen=True)
class Break:
    code: str
    text: str
    segment: str  # name of the segment where the break is seen ("" if not a str)
    line: int  # 1-based line in that segment; 0 = the segment as a whole


@dataclass(frozen=True)
class Ok:
    segments: int
    entries: int
    last_id: int
    last_name: str  # "" when there are no segments
    last_hash: str
    last_count: int  # records in the last segment


@dataclass(frozen=True)
class Next:
    name: str
    header: bytes  # b"" for the first segment


class _Bad(Exception):
    def __init__(self, code, segment="", line=0):
        super().__init__(code)
        self.code, self.segment, self.line = code, segment, line


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


def _encode(value):
    # Same as talk_log._encode / _seal (a test pins the two together).
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _seal(record, previous):
    digest = hashlib.sha256(previous.encode("ascii") + _encode(record)).hexdigest()
    return _encode({"hash": digest, "record": record}) + b"\n", digest


def make_name(number):
    return f"{PREFIX}{number:0{WIDTH}d}{SUFFIX}"


def _number(name):
    match = _NAME.fullmatch(name) if type(name) is str else None
    if match is None:
        return None
    number = int(match.group(1))
    # Canonical width only: talk-00001 would alias talk-0001.
    return number if number >= 1 and make_name(number) == name else None


def _numbered(names):
    """((number, name), ...) sorted by number; raises _Bad."""
    exact, folded = set(), set()
    for name in names:
        if type(name) is not str:
            raise _Bad("NAME_INVALID")
        if name in exact:
            raise _Bad("SEGMENT_DUPLICATE", name)
        if name.casefold() in folded:
            raise _Bad("NAME_DUPLICATE", name)
        exact.add(name)
        folded.add(name.casefold())
    pairs = []
    for name in names:
        number = _number(name)
        if number is None:
            raise _Bad("NAME_INVALID", name)
        pairs.append((number, name))
    pairs.sort(key=lambda pair: pair[0])
    for expected, (number, name) in enumerate(pairs, 1):
        if number != expected:
            raise _Bad("SEGMENT_GAP" if number > expected else "SEGMENT_DUPLICATE", name)
    return tuple(pairs)


def _names(names):
    if type(names) not in (list, tuple):
        raise _Bad("NAMES_INVALID")
    return _numbered(tuple(names))


def _break(code, segment, line):
    return Break(code, REFUSALS[code], segment, line)


def _fits(value, low):
    return type(value) is int and low <= value <= LIMIT_MAX


def _header(prev_name, prev_count, prev_hash):
    return _encode({"segment": {"count": prev_count, "hash": prev_hash,
                                "prev": prev_name}}) + b"\n"


def _shape(line):
    """(kind, envelope): kind is "header", "record" or None (unreadable)."""
    try:
        envelope = json.loads(line)
    except (ValueError, RecursionError):
        return None, None
    if type(envelope) is dict and "segment" in envelope:
        return "header", envelope
    if type(envelope) is dict and envelope.keys() == {"hash", "record"}:
        return "record", envelope
    return None, None


@_guarded
def order(names):
    """Names in reading order: by segment number, not text order."""
    try:
        return tuple(name for _, name in _names(names))
    except _Bad as bad:
        return _refuse(bad.code)


@_guarded
def next_name(existing):
    """Zero-padded name of the segment after the existing ones."""
    try:
        number = len(_names(existing)) + 1
    except _Bad as bad:
        return _refuse(bad.code)
    if len(str(number)) > MAX_DIGITS:
        return _refuse("NAME_SPACE_FULL")
    return make_name(number)


@_guarded
def should_roll(size, entries, limits):
    """True when the current segment (size bytes, entries records) is full."""
    if type(limits) is not dict:
        return _refuse("LIMITS_INVALID")
    items = tuple(limits.items())
    if len(items) != len(LIMIT_KEYS) or any(
        type(key) is not str or key not in LIMIT_KEYS or not _fits(value, 1)
        for key, value in items
    ):
        return _refuse("LIMITS_INVALID")
    if not (_fits(size, 0) and _fits(entries, 0)):
        return _refuse("COUNT_INVALID")
    bounds = dict(items)
    # Never roll an empty segment: a header alone may exceed a tiny max_bytes.
    return entries > 0 and (size >= bounds["max_bytes"] or entries >= bounds["max_entries"])


@_guarded
def verify(segments):
    """Ok for an unbroken chain over (name, bytes) pairs, else the first Break."""
    if type(segments) not in (list, tuple):
        return _refuse("SEGMENTS_INVALID")
    items = tuple(segments)
    if any(type(item) is not tuple or len(item) != 2 or type(item[1]) is not bytes
           for item in items):
        return _refuse("SEGMENTS_INVALID")
    try:
        pairs = _numbered(tuple(name for name, _ in items))
    except _Bad as bad:
        return _break(bad.code, bad.segment, 0)
    contents = dict(items)
    previous, last_id, total = GENESIS, 0, 0
    prev_name, prev_count = "", 0
    for number, name in pairs:
        if number > 1 and prev_count == 0:
            return _break("SEGMENT_EMPTY", prev_name, 0)
        content = contents[name]
        if content and not content.endswith(b"\n"):
            return _break("LINE_TORN", name, content.count(b"\n") + 1)
        lines = [line + b"\n" for line in content.split(b"\n")[:-1]]
        start = 0
        if number > 1:
            if not lines or _shape(lines[0])[0] != "header":
                return _break("HEADER_MISSING", name, 1)
            if lines[0] != _header(prev_name, prev_count, previous):
                return _break("HEADER_MISMATCH", name, 1)
            start = 1
        count = 0
        for index in range(start, len(lines)):
            line = lines[index]
            kind, envelope = _shape(line)
            if kind == "header":
                return _break("TAMPER", name, index + 1)
            if kind is None:
                return _break("RECORD_INVALID", name, index + 1)
            record = envelope["record"]
            try:
                expected, digest = _seal(record, previous)
            except (TypeError, ValueError, RecursionError):
                return _break("RECORD_INVALID", name, index + 1)
            if line != expected:
                return _break("HASH_MISMATCH", name, index + 1)
            if type(record) is not dict or type(record.get("id")) is not int \
                    or record["id"] != last_id + 1:
                return _break("ID_OUT_OF_ORDER", name, index + 1)
            last_id, previous, count = last_id + 1, digest, count + 1
        total += count
        prev_name, prev_count = name, count
    return Ok(len(pairs), total, last_id, prev_name, previous, prev_count)


@_guarded
def roll(segments):
    """Next(name, header) for a new segment after verified ones; old ones stay as they are."""
    result = verify(segments)
    if type(result) is not Ok:
        return result
    if result.segments == 0:
        return Next(make_name(1), b"")
    if result.last_count == 0:
        return _refuse("SEGMENT_EMPTY")
    if len(str(result.segments + 1)) > MAX_DIGITS:
        return _refuse("NAME_SPACE_FULL")
    return Next(make_name(result.segments + 1),
                _header(result.last_name, result.last_count, result.last_hash))
