"""Describe what one allowlisted Telegram message carries besides plain text.

Pure module: no I/O, no network; nothing in the running app imports it. Every
descriptor is untrusted data, and forwarded or quoted words are never Salih's.
Unknown message fields are dropped; conflicting fields are refused, never guessed.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Literal


DEFAULT_MAX_PHOTO_BYTES = 20 * 1024 * 1024
MAX_CAPTION_CHARS = 1024  # Bot API caption limit
MAX_TEXT_CHARS = 4096  # Bot API message text limit
MAX_FILE_ID_CHARS = 256
MAX_HINT_CHARS = 255
MAX_ALBUM_KEY_CHARS = 64
MAX_PHOTO_SIZES = 16
_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-_")  # Bot API ids are base64url
FILE_KINDS = ("document", "voice", "audio")
UNSUPPORTED_TYPES = (
    "sticker", "location", "contact", "poll", "video", "video_note", "animation", "dice",
)
_MEDIA = ("photo", *FILE_KINDS, *UNSUPPORTED_TYPES)
_FORWARD = (
    "forward_origin", "forward_from", "forward_from_chat", "forward_sender_name",
    "forward_date", "is_automatic_forward",
)
_KNOWN = frozenset(
    {"message_id", "text", "caption", "media_group_id", "edit_date", "quote", *_MEDIA, *_FORWARD}
)


class Refusal(Enum):
    INVALID_MESSAGE = "invalid_message"
    INVALID_LIMIT = "invalid_limit"
    MESSAGE_ID_INVALID = "message_id_invalid"
    AMBIGUOUS = "ambiguous"
    PHOTO_INVALID = "photo_invalid"
    PHOTO_OVER_CAP = "photo_over_cap"
    FILE_INVALID = "file_invalid"
    FILE_ID_INVALID = "file_id_invalid"
    TEXT_INVALID = "text_invalid"
    CAPTION_OVER_CAP = "caption_over_cap"
    TEXT_OVER_CAP = "text_over_cap"
    ALBUM_KEY_INVALID = "album_key_invalid"
    EDIT_INVALID = "edit_invalid"

    @property
    def text(self) -> str:
        return _REFUSAL_TEXTS[self]


_REFUSAL_TEXTS = {
    Refusal.INVALID_MESSAGE: "Message refused: the message is not readable.",
    Refusal.INVALID_LIMIT: "Message refused: the photo size limit is not valid.",
    Refusal.MESSAGE_ID_INVALID: "Message refused: the message id is missing or not valid.",
    Refusal.AMBIGUOUS: "Message refused: it carries conflicting content.",
    Refusal.PHOTO_INVALID: "Photo refused: the photo sizes are missing or not valid.",
    Refusal.PHOTO_OVER_CAP: "Photo refused: every photo size is larger than allowed.",
    Refusal.FILE_INVALID: "File refused: the file description is not valid.",
    Refusal.FILE_ID_INVALID: "File refused: the file id is missing, too long or not plain.",
    Refusal.TEXT_INVALID: "Message refused: a caption or quoted text is not valid.",
    Refusal.CAPTION_OVER_CAP: "Message refused: the caption is longer than allowed.",
    Refusal.TEXT_OVER_CAP: "Message refused: the forwarded or quoted text is too long.",
    Refusal.ALBUM_KEY_INVALID: "Message refused: the album key is not valid.",
    Refusal.EDIT_INVALID: "Message refused: the edit date is not valid.",
}


@dataclass(frozen=True, slots=True)
class TgFile:
    """Local record; field names match the mapping TGFILE1 plan_download reads."""

    file_id: str
    file_size: int | None
    mime_type: str | None
    file_name: str | None
    duration: int | None

    def mapping(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)
                if getattr(self, f.name) is not None}


@dataclass(frozen=True, slots=True)
class Descriptor:
    kind: str  # photo | document | voice | audio | caption | forward | quote | edit | unsupported
    file: TgFile | None = None
    width: int | None = None
    height: int | None = None
    text: str | None = None
    type_name: str | None = None
    album_key: str | None = None
    edit_of: int | None = None
    not_salihs_words: bool = False
    trusted: Literal[False] = field(init=False, default=False)


class _Refused(Exception):
    def __init__(self, refusal: Refusal):
        super().__init__(refusal.value)
        self.refusal = refusal


def describe(
    message: object, *, max_photo_bytes: int = DEFAULT_MAX_PHOTO_BYTES
) -> tuple[Descriptor, ...] | Refusal:
    """Return the message's non-plain-text descriptors, or one typed refusal."""
    try:
        return _describe(message, max_photo_bytes)
    except _Refused as refused:
        return refused.refusal
    except Exception:  # Hostile mappings (RecursionError included) fail closed.
        return Refusal.INVALID_MESSAGE


# C0/C1 controls and lone surrogates; newline and tab are ordinary in captions.
_BAD_CHARS = frozenset(
    chr(i) for i in (*range(32), *range(0x7F, 0xA0), *range(0xD800, 0xE000))
) - {"\n", "\t"}


def _count(value: object, minimum: int) -> bool:
    return type(value) is int and minimum <= value <= 2**63 - 1  # Bot API ids/sizes are 64-bit


def _check(ok: object, refusal: Refusal) -> None:
    if not ok:
        raise _Refused(refusal)


def _describe(message: object, max_photo_bytes: object) -> tuple[Descriptor, ...]:
    _check(_count(max_photo_bytes, 1), Refusal.INVALID_LIMIT)
    _check(isinstance(message, Mapping), Refusal.INVALID_MESSAGE)
    msg = {key: message[key] for key in sorted(_KNOWN) if key in message}  # unknown dropped
    message_id = msg.get("message_id")
    _check(_count(message_id, 1), Refusal.MESSAGE_ID_INVALID)
    media = [key for key in _MEDIA if key in msg]
    if "animation" in media:  # Bot API also sets document beside animation (backward compat)
        media = [key for key in media if key != "document"]
    has_text, has_caption = "text" in msg, "caption" in msg
    conflicts = (len(media) > 1 or (has_text and (has_caption or bool(media)))
                 or (not media and (has_caption or "media_group_id" in msg)))
    _check(not conflicts, Refusal.AMBIGUOUS)
    forwarded = any(key in msg for key in _FORWARD)
    out = []
    if "edit_date" in msg:  # marker only: the caller decides what, if anything, it replaces
        _check(_count(msg["edit_date"], 1), Refusal.EDIT_INVALID)
        out.append(Descriptor("edit", edit_of=message_id))
    if forwarded:
        text = _text(msg["text"], MAX_TEXT_CHARS, Refusal.TEXT_OVER_CAP) if has_text else None
        out.append(Descriptor("forward", text=text, not_salihs_words=True))
    if "quote" in msg:
        quote = msg["quote"]
        _check(isinstance(quote, Mapping), Refusal.TEXT_INVALID)
        text = _text(quote.get("text"), MAX_TEXT_CHARS, Refusal.TEXT_OVER_CAP)
        out.append(Descriptor("quote", text=text, not_salihs_words=True))
    if media:
        album = msg.get("media_group_id")
        _check(album is None or (type(album) is str and 0 < len(album) <= MAX_ALBUM_KEY_CHARS
                                 and _TOKEN_CHARS.issuperset(album)), Refusal.ALBUM_KEY_INVALID)
        out.append(_media(media[0], msg[media[0]], max_photo_bytes, album, forwarded))
    if has_caption:
        text = _text(msg["caption"], MAX_CAPTION_CHARS, Refusal.CAPTION_OVER_CAP)
        out.append(Descriptor("caption", text=text, not_salihs_words=forwarded))
    return tuple(out)


def _text(value: object, cap: int, over: Refusal) -> str:
    _check(type(value) is str and value != "", Refusal.TEXT_INVALID)
    _check(len(value) <= cap, over)  # refused, never cut
    _check(_BAD_CHARS.isdisjoint(value), Refusal.TEXT_INVALID)
    return value


def _file_id(value: object) -> str:
    _check(type(value) is str and 0 < len(value) <= MAX_FILE_ID_CHARS and value.isprintable(),
           Refusal.FILE_ID_INVALID)
    return value


def _hint(value: object) -> str | None:
    ok = type(value) is str and 0 < len(value) <= MAX_HINT_CHARS and value.isprintable()
    return value if ok else None


def _media(kind: str, value: object, cap: int, album: str | None, forwarded: bool) -> Descriptor:
    if kind in UNSUPPORTED_TYPES:
        return Descriptor("unsupported", type_name=kind, album_key=album,
                          not_salihs_words=forwarded)
    if kind == "photo":
        record, width, height = _photo(value, cap)
        return Descriptor("photo", file=record, width=width, height=height, album_key=album,
                          not_salihs_words=forwarded)
    _check(isinstance(value, Mapping), Refusal.FILE_INVALID)
    file_id = _file_id(value.get("file_id"))
    size, duration = value.get("file_size"), None
    _check(size is None or _count(size, 1), Refusal.FILE_INVALID)
    if kind != "document":  # Bot API: voice and audio always carry a duration
        duration = value.get("duration")
        _check(_count(duration, 0), Refusal.FILE_INVALID)
    record = TgFile(file_id, size, _hint(value.get("mime_type")),
                    _hint(value.get("file_name")), duration)
    return Descriptor(kind, file=record, album_key=album, not_salihs_words=forwarded)


def _photo(sizes: object, cap: int) -> tuple[TgFile, int, int]:
    _check(type(sizes) in (list, tuple) and 0 < len(sizes) <= MAX_PHOTO_SIZES,
           Refusal.PHOTO_INVALID)
    checked = []
    for size in sizes:
        _check(isinstance(size, Mapping), Refusal.PHOTO_INVALID)
        nbytes, width, height = size.get("file_size"), size.get("width"), size.get("height")
        _check(_count(nbytes, 1) and _count(width, 1) and _count(height, 1),
               Refusal.PHOTO_INVALID)
        checked.append((nbytes, width * height, _file_id(size.get("file_id")), width, height))
    fitting = [entry for entry in checked if entry[0] <= cap]
    _check(fitting, Refusal.PHOTO_OVER_CAP)
    nbytes, _, file_id, width, height = max(fitting, key=lambda entry: entry[:2])
    # Bot API photos are always re-encoded JPEG.
    return TgFile(file_id, nbytes, "image/jpeg", None, None), width, height
