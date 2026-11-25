"""Inbound item -> conversation-log entry candidate, exactly once (TALKIN1).

One parsed Telegram record or PC submission becomes one entry candidate keyed
by (source, source id), or DUPLICATE when that key was already seen. The
candidate is built only from the fields read here; any other inbound field
(who, kind, trusted, focus, needs_you, approved, ...) is never copied. "who" is
always "salih", set by code. The Telegram allowlist verdict is the wire
layer's; this module only requires it to say "allowed".

Pure and deterministic: no I/O, no network, stdlib only, no other talk_* or
telegram_* import; no exception escapes. Refusal texts are fixed ASCII.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

WHO = "salih"
SOURCES = ("pc", "telegram")
ALLOWED = "allowed"
MAX_TEXT_CHARS = 4096
MAX_ATTACHMENTS = 10
MAX_NAME_CHARS = 255

# Inbound item type (read from "type"; an inbound "kind" is ignored) -> entry kind.
ITEM_KINDS = {
    "text": "text",
    "voice": "voice",
    "image": "image",
    "file": "file",
    "button": "approval_tap",
}
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_CARD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
# Unicode Default_Ignorable_Code_Point ranges (DerivedCoreProperties); any other Cf char is flagged too.
_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_NAME_BAD = frozenset('/\\:*?"<>|\u2215\u2044\u29f8\u2216\ufe68')
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"{p}{n}" for p in ("COM", "LPT") for n in "0123456789\u00b9\u00b2\u00b3"}
)
_SHORT_NAME = re.compile(r"~[0-9]")

REASONS = {
    "bad_item": "item must be a mapping",
    "bad_seen": "seen keys must be a set",
    "bad_source": "source must be pc or telegram",
    "bad_source_id": "source message id is missing or not a plain id",
    "not_allowed": "telegram item lacks the allowed verdict",
    "bad_type": "item type is missing or not allowed",
    "bad_card_id": "button tap needs a plain card id",
    "bad_time": "received time must be an ISO time with offset",
    "bad_text": "text must be a valid string",
    "too_long": "text exceeds the length cap",
    "empty": "text is empty and there is no attachment",
    "bad_attachments": "attachments must be a short list of records",
    "bad_sha256": "attachment lacks a valid sha256",
    "bad_name": "attachment name is missing or path-like",
    "internal": "item could not be converted",
}


@dataclass(frozen=True)
class Refusal:
    reason: str

    @property
    def message(self) -> str:
        return REASONS.get(self.reason, REASONS["internal"])


@dataclass(frozen=True)
class Duplicate:
    key: tuple[str, str]


@dataclass(frozen=True)
class Candidate:
    key: tuple[str, str]  # idempotency key (source, source id)
    source: str
    who: str
    kind: str
    text: str
    attachments: tuple[tuple[str, str], ...]  # (sha256, name)
    received_at: str
    card_id: str | None
    flags: tuple[str, ...]  # "bidi" / "control" / "zero_width": kept as data, flagged


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def to_candidate(item: object, seen: object) -> Candidate | Duplicate | Refusal:
    try:
        return _convert(item, seen)
    except _Refused as exc:
        return Refusal(exc.reason)
    except RecursionError:
        return Refusal("internal")
    except Exception:  # fail closed: nothing escapes
        return Refusal("internal")


def _convert(item: object, seen: object) -> Candidate | Duplicate:
    if not isinstance(item, Mapping):
        raise _Refused("bad_item")
    if type(seen) not in (set, frozenset):
        raise _Refused("bad_seen")
    source = item.get("source")
    if type(source) is not str or source not in SOURCES:
        raise _Refused("bad_source")
    source_id = _source_id(item.get("source_id"))
    verdict = item.get("allowlist") if source == "telegram" else ALLOWED  # read once
    if not (type(verdict) is str and verdict == ALLOWED):
        raise _Refused("not_allowed")
    item_type = item.get("type")
    if type(item_type) is not str or item_type not in ITEM_KINDS:
        raise _Refused("bad_type")
    kind = ITEM_KINDS[item_type]
    received_at = _time(item.get("received_at"))
    if kind == "approval_tap":  # carries only the opaque card id
        card_id = item.get("card_id")
        if type(card_id) is not str or not _CARD_ID.fullmatch(card_id):
            raise _Refused("bad_card_id")
        text, flags, attachments = "", (), ()
    else:
        card_id = None
        text, flags = _text(item.get("text", ""))
        attachments = _attachments(item.get("attachments", ()))
        if not text.strip() and not attachments:
            raise _Refused("empty")
    key = (source, source_id)
    if key in seen:
        return Duplicate(key)
    return Candidate(key, source, WHO, kind, text, attachments, received_at, card_id, flags)


def _source_id(value: object) -> str:
    if type(value) is int and value >= 0:
        value = str(value)
    if type(value) is not str or not _SOURCE_ID.fullmatch(value):
        raise _Refused("bad_source_id")
    return value


def _time(value: object) -> str:
    if type(value) is not str or len(value) > 40 or not value.isascii():
        raise _Refused("bad_time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _Refused("bad_time") from None
    if parsed.utcoffset() is None:
        raise _Refused("bad_time")
    return parsed.isoformat()


def _text(value: object) -> tuple[str, tuple[str, ...]]:
    if type(value) is not str:
        raise _Refused("bad_text")
    if len(value) > MAX_TEXT_CHARS * 4:  # cheap bound before normalising
        raise _Refused("too_long")
    value = value.replace("\x00", "")
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        raise _Refused("bad_text")
    value = unicodedata.normalize("NFC", value)
    if len(value) > MAX_TEXT_CHARS:  # refused, never cut
        raise _Refused("too_long")
    flags = set()
    for ch in value:
        if ch in _BIDI:
            flags.add("bidi")
        elif _invisible(ch):
            flags.add("zero_width")
        elif ch not in "\n\t" and unicodedata.category(ch) == "Cc":
            flags.add("control")
    return value, tuple(sorted(flags))


def _invisible(ch: str) -> bool:
    cp = ord(ch)
    return unicodedata.category(ch) == "Cf" or any(lo <= cp <= hi for lo, hi in _IGNORABLE)


def _attachments(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) not in (list, tuple) or len(value) > MAX_ATTACHMENTS:
        raise _Refused("bad_attachments")
    out = []
    for record in value:
        if not isinstance(record, Mapping):
            raise _Refused("bad_attachments")
        digest = record.get("sha256")
        if type(digest) is not str or not _SHA256.fullmatch(digest):
            raise _Refused("bad_sha256")
        out.append((digest, _name(record.get("name"))))
    return tuple(out)


def _name(value: object) -> str:
    if type(value) is not str or not 0 < len(value) <= MAX_NAME_CHARS:
        raise _Refused("bad_name")
    folded = unicodedata.normalize("NFKC", value)  # fullwidth solidus -> "/"
    for ch in folded:
        if ch in _NAME_BAD or (unicodedata.category(ch)[0] in "CZ" and ch != " "):
            raise _Refused("bad_name")
    stem = folded.split(".", 1)[0].rstrip(" ").upper()
    if folded.startswith(".") or folded.endswith((".", " ")) or stem in _RESERVED or _SHORT_NAME.search(folded):
        raise _Refused("bad_name")
    return value
