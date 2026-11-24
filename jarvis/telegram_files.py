"""Decide before any download whether a Telegram file or voice note is accepted.

Pure module: no I/O, no network. The descriptor's mime and name are hints only;
the caller must verify the content after download. The file id is opaque.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_VOICE_SECONDS = 300
MAX_FILE_ID_CHARS = 256
MAX_NAME_CHARS = 255
VERIFY_NOTE = "verify content after download"

# Local copy of TALKATTACH1's kind allowlist (talk_attach is deliberately not imported).
FILE_MIMES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "csv",
    "application/json": "json",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}
VOICE_MIMES = {"audio/ogg": "ogg", "audio/opus": "opus"}
_EXTENSION_KIND = {
    **{kind: kind for kind in (*FILE_MIMES.values(), *VOICE_MIMES.values())},
    "jpeg": "jpg",
    "oga": "ogg",
}
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{device}{digit}" for device in ("COM", "LPT") for digit in range(10)}
)
_NAME_FORBIDDEN = frozenset('/\\:~<>"|?*')


class Refusal(Enum):
    INVALID_DESCRIPTOR = "invalid_descriptor"
    INVALID_LIMIT = "invalid_limit"
    FILE_ID_INVALID = "file_id_invalid"
    SIZE_INVALID = "size_invalid"
    SIZE_OVER_CAP = "size_over_cap"
    MIME_NOT_ALLOWED = "mime_not_allowed"
    MIME_NAME_MISMATCH = "mime_name_mismatch"
    DURATION_INVALID = "duration_invalid"
    DURATION_OVER_CAP = "duration_over_cap"

    @property
    def text(self) -> str:
        return _REFUSAL_TEXTS[self]


_REFUSAL_TEXTS = {
    Refusal.INVALID_DESCRIPTOR: "File refused: the file description is not readable.",
    Refusal.INVALID_LIMIT: "File refused: the size or duration limit is not valid.",
    Refusal.FILE_ID_INVALID: "File refused: the file id is missing, too long or not printable.",
    Refusal.SIZE_INVALID: "File refused: the file size is missing or not a positive number.",
    Refusal.SIZE_OVER_CAP: "File refused: the file is larger than the allowed size.",
    Refusal.MIME_NOT_ALLOWED: "File refused: this file type is not accepted.",
    Refusal.MIME_NAME_MISMATCH: "File refused: the file name does not match its type.",
    Refusal.DURATION_INVALID: "Voice note refused: the duration is missing or not a number.",
    Refusal.DURATION_OVER_CAP: "Voice note refused: the voice note is longer than allowed.",
}


@dataclass(frozen=True, slots=True)
class DownloadPlan:
    file_id: str
    kind: str
    extension: str
    size: int
    voice: bool
    duration: int | None
    mime_hint: str
    name_hint: str | None
    verify: str = field(init=False, default=VERIFY_NOTE)
    trusted: Literal[False] = field(init=False, default=False)


def plan_download(
    descriptor: object,
    *,
    voice: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_voice_seconds: int = DEFAULT_MAX_VOICE_SECONDS,
) -> DownloadPlan | Refusal:
    """Return a download plan for an allowlisted file, or a typed refusal."""
    try:
        return _plan(descriptor, voice, max_bytes, max_voice_seconds)
    except Exception:  # Hostile mappings (RecursionError included) fail closed.
        return Refusal.INVALID_DESCRIPTOR


def _count(value: object, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _plan(descriptor: object, voice: object, max_bytes: object, max_seconds: object):
    if type(voice) is not bool or not _count(max_bytes, 1) or not _count(max_seconds, 1):
        return Refusal.INVALID_LIMIT
    if not isinstance(descriptor, Mapping):
        return Refusal.INVALID_DESCRIPTOR
    file_id = descriptor.get("file_id")
    if (type(file_id) is not str or not 0 < len(file_id) <= MAX_FILE_ID_CHARS
            or not file_id.isprintable()):
        return Refusal.FILE_ID_INVALID
    size = descriptor.get("file_size")
    if not _count(size, 1):
        return Refusal.SIZE_INVALID
    if size > max_bytes:
        return Refusal.SIZE_OVER_CAP
    duration = None
    if voice:
        duration = descriptor.get("duration")
        if not _count(duration, 0):
            return Refusal.DURATION_INVALID
        if duration > max_seconds:
            return Refusal.DURATION_OVER_CAP
    # Bot API voice notes are OGG/Opus; their mime_type field is optional.
    mime = descriptor.get("mime_type", "audio/ogg" if voice else None)
    mime = mime.split(";")[0].strip().lower() if type(mime) is str else None
    kind = (VOICE_MIMES if voice else FILE_MIMES).get(mime)
    if kind is None:
        return Refusal.MIME_NOT_ALLOWED
    name = _safe_name(descriptor.get("file_name"))
    if name is not None and "." in name:
        if _EXTENSION_KIND.get(name.rsplit(".", 1)[1].lower()) != kind:
            return Refusal.MIME_NAME_MISMATCH
    return DownloadPlan(file_id, kind, "." + kind, size, voice, duration, mime, name)


def _safe_name(value: object) -> str | None:
    """Keep a display-only name hint, or drop it; it never names the target."""
    if type(value) is not str or not 0 < len(value) <= MAX_NAME_CHARS:
        return None
    name = unicodedata.normalize("NFKC", value)
    if (not name.isprintable() or ".." in name or name[-1] in ". "
            or not _NAME_FORBIDDEN.isdisjoint(name)
            or name.split(".")[0].strip().upper() in _RESERVED_NAMES):
        return None
    return name
