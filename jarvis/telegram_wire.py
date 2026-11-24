"""Offline Telegram update parsing and plain-text notification formatting."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import AbstractSet, Literal


MAX_UPDATE_BYTES = 256 * 1024
MAX_NOTIFICATION_CHARS = 2048  # At most 4096 UTF-16 code units.


class Refusal(Enum):
    TOO_LARGE = "too_large"
    INVALID_JSON = "invalid_json"
    INVALID_UPDATE = "invalid_update"
    CHAT_NOT_ALLOWED = "chat_not_allowed"


@dataclass(frozen=True, slots=True)
class InboundRecord:
    chat_id: int
    message_id: int
    date: int
    trusted: Literal[False] = field(init=False, default=False)


@dataclass(frozen=True, slots=True)
class TextMessage(InboundRecord):
    text: str


@dataclass(frozen=True, slots=True)
class VoiceNote(InboundRecord):
    file_id: str
    duration: int
    mime_type: str | None


@dataclass(frozen=True, slots=True)
class Photo(InboundRecord):
    file_id: str
    file_size: int | None
    mime_type: None
    caption: str | None


@dataclass(frozen=True, slots=True)
class Document(InboundRecord):
    file_id: str
    file_size: int | None
    mime_type: str | None
    caption: str | None


@dataclass(frozen=True, slots=True)
class ButtonCallback(InboundRecord):
    callback_id: str
    data: str


def is_allowed_chat(chat_id: int, allowed_chat_ids: AbstractSet[int]) -> bool:
    """Check the Telegram chat identifier against the caller's allowlist."""
    return type(chat_id) is int and chat_id != 0 and any(
        type(allowed_id) is int and chat_id == allowed_id for allowed_id in allowed_chat_ids
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON constant")


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("invalid integer")
    return value


def _string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("invalid string")
    value.encode("utf-8")  # Reject unpaired surrogates from JSON string escapes.
    return value


def _optional_string(source: dict, key: str, *, allow_empty: bool = False) -> str | None:
    return _string(source[key], allow_empty=allow_empty) if key in source else None


def _file_size(media: dict) -> int | None:
    return _integer(media["file_size"]) if "file_size" in media else None


def _photo_area(photo: dict) -> int:
    return _integer(photo.get("width"), 1) * _integer(photo.get("height"), 1)


def _parse_message(message: dict, envelope: tuple[int, int, int]) -> InboundRecord:
    fields = message.keys() & {"text", "voice", "photo", "document"}
    if len(fields) != 1:
        raise ValueError("expected one supported message payload")
    kind = fields.pop()
    if kind == "text":
        return TextMessage(*envelope, _string(message["text"]))
    if kind == "voice":
        voice = _object(message["voice"])
        return VoiceNote(
            *envelope, _string(voice.get("file_id")), _integer(voice.get("duration")),
            _optional_string(voice, "mime_type"),
        )
    caption = _optional_string(message, "caption", allow_empty=True)
    if kind == "photo":
        photos = message["photo"]
        if not isinstance(photos, list) or not photos:
            raise ValueError("expected photo sizes")
        photo = max(map(_object, photos), key=_photo_area)
        return Photo(*envelope, _string(photo.get("file_id")), _file_size(photo), None, caption)
    document = _object(message["document"])
    return Document(
        *envelope, _string(document.get("file_id")), _file_size(document),
        _optional_string(document, "mime_type"), caption,
    )


def parse_update(raw: bytes, allowed_chat_ids: AbstractSet[int]) -> InboundRecord | Refusal:
    """Decode a bounded UTF-8 Bot API update into an allowlisted record."""
    if len(raw) > MAX_UPDATE_BYTES:
        return Refusal.TOO_LARGE
    try:
        update = _object(json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        ))
    except (ValueError, RecursionError):
        return Refusal.INVALID_JSON

    try:
        fields = update.keys() & {"message", "callback_query"}
        if len(fields) != 1:
            return Refusal.INVALID_UPDATE
        callback = _object(update["callback_query"]) if "callback_query" in fields else None
        source = callback if callback is not None else update
        message = _object(source.get("message"))
        chat_id = _integer(_object(message.get("chat")).get("id"), -(2**52 - 1))
        if chat_id == 0 or chat_id >= 2**52:
            return Refusal.INVALID_UPDATE
        envelope = (chat_id, _integer(message.get("message_id")), _integer(message.get("date"), 1))
        if not is_allowed_chat(chat_id, allowed_chat_ids):
            return Refusal.CHAT_NOT_ALLOWED
        if callback is not None:
            return ButtonCallback(
                *envelope, _string(callback.get("id")), _string(callback.get("data")),
            )
        return _parse_message(message, envelope)
    except ValueError:
        return Refusal.INVALID_UPDATE


def _plain_words(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join("".join(
        character if character.isalnum() else " " for character in normalized
    ).split())


def format_notification(kind: str, *, title: str = "", body: str = "") -> str:
    """Render Core-supplied fields as text without URL or command punctuation."""
    lines = (_plain_words(value) for value in (kind, title, body))
    return "\n".join(filter(None, lines))[:MAX_NOTIFICATION_CHARS].rstrip()
