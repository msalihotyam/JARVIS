"""Proposal-inbox record validator (TALKINBOX1).

The read-only brain may write exactly one thing: proposal-inbox records
(Salih, 2026-11-25 00:02). A record is data for the executor; it never names
where or how it is applied, and nothing here ever marks a record approved.

Pure: validate_record(bytes) returns an InboxRecord or a Refusal. No I/O, no
network, deterministic; no exception escapes. Refusal texts are fixed ASCII.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

MAX_RECORD_BYTES = 16 * 1024
MAX_DEPTH = 2  # top object + one list (cited_turns); anything deeper is refused
MAX_TEXT_CHARS = 4000
MAX_TURNS = 16
MAX_TURN_ID = 64

CLASSES = ("ordinary", "authority")
_TURN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,%d}" % (MAX_TURN_ID - 1))  # no leading "-"
_JSON_WS = " \t\r\n"
_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")

# kind -> (required fields, optional fields); anything else is refused.
KIND_FIELDS = {
    "decision": (frozenset({"kind", "class", "cited_turns", "summary"}), frozenset({"rationale"})),
    "deferred": (frozenset({"kind", "class", "cited_turns", "summary"}), frozenset({"reason"})),
    "question": (frozenset({"kind", "question"}), frozenset({"context", "cited_turns"})),
    "glossary_candidate": (frozenset({"kind", "term", "definition"}), frozenset({"cited_turns"})),
    "spec_proposal": (frozenset({"kind", "title", "body"}), frozenset({"cited_turns"})),
}
# Field names that would let a record say where/how it applies or claim trust.
_FORBIDDEN_TOKENS = (
    "path", "file", "target", "command", "cmd", "exec", "script", "key",
    "backlog", "allowlist", "approv", "appl", "trust", "author", "sign",
)
_FORBIDDEN_EXACT = frozenset({"by"})

REASONS = {
    "wrong_type": "input must be bytes",
    "too_large": "record exceeds the size cap",
    "bom": "byte order mark is not allowed",
    "bad_encoding": "record is not valid UTF-8",
    "too_deep": "record nesting exceeds the depth limit",
    "bad_json": "record is not valid JSON",
    "duplicate_key": "record repeats a key",
    "trailing_data": "data follows the record object",
    "not_object": "record must be a JSON object",
    "bad_kind": "kind is missing or not allowed",
    "forbidden_field": "field names a target, command or trust claim",
    "unknown_field": "field is not allowed for this kind",
    "missing_field": "a required field is missing",
    "class_missing": "class is required for this kind",
    "bad_class": "class must be ordinary or authority",
    "turns_missing": "at least one cited Salih turn id is required",
    "bad_turn_id": "cited turn id is not a plain id",
    "bad_text": "field must be plain text within the length cap",
    "internal": "record could not be validated",
}


@dataclass(frozen=True)
class Refusal:
    reason: str

    @property
    def message(self) -> str:
        return REASONS.get(self.reason, REASONS["internal"])


@dataclass(frozen=True)
class InboxRecord:
    kind: str
    record_class: str | None
    needs_card: bool
    cited_turns: tuple[str, ...]
    text: tuple[tuple[str, str], ...]  # sorted (field, value) pairs


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_record(data: object) -> InboxRecord | Refusal:
    try:
        return _validate(data)
    except _Refused as exc:
        return Refusal(exc.reason)
    except RecursionError:
        return Refusal("too_deep")
    except Exception:  # fail closed: nothing escapes
        return Refusal("internal")


def _validate(data: object) -> InboxRecord:
    if type(data) is not bytes:
        raise _Refused("wrong_type")
    if len(data) > MAX_RECORD_BYTES:  # before any decoding or parsing
        raise _Refused("too_large")
    if data.startswith(_BOMS):
        raise _Refused("bom")
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise _Refused("bad_encoding") from None
    if _too_deep(text):
        raise _Refused("too_deep")
    obj = _parse(text)
    if type(obj) is not dict:
        raise _Refused("not_object")
    return _check(obj)


def _too_deep(text: str) -> bool:
    depth, in_str, esc = 0, False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def _pairs(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _Refused("duplicate_key")
        out[key] = value
    return out


def _no_constant(_name: str) -> None:
    raise _Refused("bad_json")


def _parse(text: str) -> object:
    body = text.strip(_JSON_WS)
    decoder = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_no_constant)
    try:
        obj, end = decoder.raw_decode(body)
    except ValueError:
        raise _Refused("bad_json") from None
    if end != len(body):
        raise _Refused("trailing_data")
    return obj


def _forbidden(name: str) -> bool:
    folded = name.casefold()
    norm = "".join(ch for ch in folded if ch.isalnum())
    words = set(re.split(r"[^0-9a-z]+", folded))  # "by: salih", "signed_by" -> word "by"
    return norm in _FORBIDDEN_EXACT or bool(words & _FORBIDDEN_EXACT) or any(tok in norm for tok in _FORBIDDEN_TOKENS)


def _check(obj: dict) -> InboxRecord:
    kind = obj.get("kind")
    if type(kind) is not str or kind not in KIND_FIELDS:
        raise _Refused("bad_kind")
    required, optional = KIND_FIELDS[kind]
    for name in obj:
        if _forbidden(name):
            raise _Refused("forbidden_field")
    if any(name not in required and name not in optional for name in obj):
        raise _Refused("unknown_field")
    needs_class = "class" in required
    if needs_class and "class" not in obj:
        raise _Refused("class_missing")
    if needs_class and "cited_turns" not in obj:
        raise _Refused("turns_missing")
    if not required <= obj.keys():
        raise _Refused("missing_field")
    record_class = None
    if needs_class:
        record_class = obj["class"]
        if type(record_class) is not str or record_class not in CLASSES:
            raise _Refused("bad_class")
    turns = _turns(obj["cited_turns"]) if "cited_turns" in obj else ()
    text = []
    for name in sorted(obj):
        if name in ("kind", "class", "cited_turns"):
            continue
        text.append((name, _text(obj[name])))
    return InboxRecord(
        kind=kind,
        record_class=record_class,
        needs_card=record_class == "authority",
        cited_turns=turns,
        text=tuple(text),
    )


def _turns(value: object) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise _Refused("turns_missing")
    if len(value) > MAX_TURNS or len(set(map(repr, value))) != len(value):
        raise _Refused("bad_turn_id")
    for item in value:
        if type(item) is not str or not item.isascii() or not _TURN_ID.fullmatch(item):
            raise _Refused("bad_turn_id")
    return tuple(value)


def _text(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value) > MAX_TEXT_CHARS:
        raise _Refused("bad_text")
    for ch in value:
        if ch in "\n\t":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            raise _Refused("bad_text")
    return value
