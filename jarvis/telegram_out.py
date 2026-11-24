"""TGSEND1: network-free builder of Telegram Bot API request bodies.

Turns a mirror action (a plain mapping) into ``(method, body)`` for one of four
allowlisted Bot API methods. No token, no URL, no socket: the client that sends
the body owns the token and the upload. Every bad input becomes a ``Refusal``
with an ASCII reason code; no exception escapes ``build_request``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

ALLOWED_METHODS = frozenset({"sendMessage", "sendVoice", "sendPhoto", "sendDocument"})
KIND_METHOD = {
    "text": "sendMessage",
    "approval": "sendMessage",
    "voice": "sendVoice",
    "photo": "sendPhoto",
    "document": "sendDocument",
}
MEDIA_FIELD = {"sendVoice": "voice", "sendPhoto": "photo", "sendDocument": "document"}
MAX_TEXT = 4096
MAX_CALLBACK_BYTES = 64
MAX_PATH = 240
APPROVAL_BUTTON = "Open card"

_KEEP_CONTROLS = frozenset("\n\t")
_CALLBACK_RE = re.compile(r"[\x21-\x7e]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PATH_CHAR_RE = re.compile(r"[\x20-\x7e]+")
_SHORT_NAME_RE = re.compile(r"~[0-9]")
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
)
# Bot token shape (<bot id>:<35 char secret>), the Bot API host or any URL: never in output.
_LEAK_RE = re.compile(
    r"\d{5,}:[A-Za-z0-9_-]{30,}|api\.telegram\.org|[a-z][a-z0-9+.-]*://|www\.",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Refusal:
    reason: str

    def __str__(self) -> str:
        return f"refused: {self.reason}"


@dataclass(frozen=True)
class Request:
    method: str
    body: dict
    stripped: tuple[str, ...] = field(default=())


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def build_request(action: object, configured_chat_id: object) -> Request | Refusal:
    """Build one Bot API request from ``action`` for the configured chat only."""
    try:
        return _build(action, configured_chat_id)
    except _Refused as refused:
        return Refusal(refused.reason)
    except Exception:  # RecursionError and hostile mappings included: fail closed.
        return Refusal("invalid_input")


def _build(action: object, configured: object) -> Request:
    if not _is_int(configured):
        raise _Refused("bad_configured_chat_id")
    if not isinstance(action, Mapping):
        raise _Refused("action_not_mapping")
    chat_id = _required(action, "chat_id")
    if not _is_int(chat_id):
        raise _Refused("bad_chat_id_type")
    if chat_id != configured:
        raise _Refused("chat_id_mismatch")
    kind = _required(action, "kind")
    if not isinstance(kind, str) or kind not in KIND_METHOD:
        raise _Refused("unknown_kind")
    method = KIND_METHOD[kind]
    if "method" in action:
        asked = action["method"]
        if not isinstance(asked, str) or asked not in ALLOWED_METHODS:
            raise _Refused("method_not_allowed")
        if asked != method:
            raise _Refused("method_kind_mismatch")
    silent = action.get("silent", False)
    if not isinstance(silent, bool):
        raise _Refused("bad_silent_type")

    body: dict = {"chat_id": configured, "disable_notification": silent}
    stripped: tuple[str, ...] = ()
    if method == "sendMessage":
        text, stripped = _clean_text(_required(action, "text"))
        body["text"] = text
        body["link_preview_options"] = {"is_disabled": True}
        if kind == "approval":
            card_id = _card_id(_required(action, "card_id"))
            button = {"text": APPROVAL_BUTTON, "callback_data": card_id}
            body["reply_markup"] = {"inline_keyboard": [[button]]}
    else:
        body[MEDIA_FIELD[method]] = _file_record(_required(action, "file"))

    if method not in ALLOWED_METHODS:  # unreachable by construction; fail closed anyway
        raise _Refused("method_not_allowed")
    if _LEAK_RE.search(json.dumps(body, ensure_ascii=False)):
        raise _Refused("token_or_url_in_output")
    return Request(method, body, stripped)


def _is_int(value: object) -> bool:
    return type(value) is int  # bool and hostile int subclasses refused


def _required(action: Mapping, key: str) -> object:
    if key not in action:
        raise _Refused(f"missing_{key}")
    return action[key]


def _clean_text(value: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str):
        raise _Refused("bad_text_type")
    kept: list[str] = []
    removed: set[str] = set()
    for ch in value:
        # Cc = controls (NUL...), Cf = format (bidi overrides, zero-width, BOM).
        if ch not in _KEEP_CONTROLS and unicodedata.category(ch) in ("Cc", "Cf", "Cs"):
            removed.add(f"U+{ord(ch):04X}")
        else:
            kept.append(ch)
    text = "".join(kept)
    if not text.strip():
        raise _Refused("empty_text")
    # Telegram counts UTF-16 units; counting them is never below len(), so fail-closed.
    if len(text.encode("utf-16-le")) // 2 > MAX_TEXT:
        raise _Refused("text_too_long")
    return text, tuple(sorted(removed))


def _card_id(value: object) -> str:
    if not isinstance(value, str):
        raise _Refused("bad_card_id_type")
    if len(value.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise _Refused("card_id_too_long")
    if not _CALLBACK_RE.fullmatch(value):  # ASCII printable, no space, no bidi
        raise _Refused("bad_card_id")
    return value


def _file_record(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise _Refused("bad_file_record")
    sha = _required(value, "sha256")
    path = _required(value, "path")
    if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
        raise _Refused("bad_sha256")
    return {"sha256": sha, "path": _relative_path(path)}


def _relative_path(path: object) -> str:
    if not isinstance(path, str):
        raise _Refused("bad_path_type")
    if len(path) > MAX_PATH or not _PATH_CHAR_RE.fullmatch(path):
        raise _Refused("bad_path_chars")
    if ":" in path:  # drive letter or NTFS alternate data stream
        raise _Refused("path_colon")
    if path.startswith(("/", "\\")):  # absolute or UNC
        raise _Refused("path_absolute")
    parts = path.replace("\\", "/").split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise _Refused("path_traversal")
        if part != part.rstrip(" ."):  # Windows drops trailing dots/spaces: alias risk
            raise _Refused("path_trailing_dot_space")
        if _SHORT_NAME_RE.search(part):
            raise _Refused("path_short_name")
        if part.split(".")[0].rstrip(" ").upper() in _RESERVED:
            raise _Refused("path_reserved_name")
    return "/".join(parts)
