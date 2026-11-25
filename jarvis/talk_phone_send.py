"""Pure decision for "Telefona gönder": one PC attachment to Salih's Telegram chat.

ADR K1 (Salih 2026-11-25): an attachment goes to the phone only when Salih asks
for that one attachment from the PC view. Nothing here does I/O or grants
anything; the caller runs the existing send path with the returned action and
stores the returned log record (its request_id marks the request as used).
Every bad input becomes a typed refusal with an ASCII reason code.
"""

import re
from collections.abc import Collection, Mapping
from datetime import datetime

OWNER = "salih"
SOURCE = "pc_view"
COMMAND = "send_to_phone"
CHANNEL_OFF_NOTE = "kanal kapalı"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_BULK = {"all", "hepsi", "tumu", "tümü", "*"}
# talk_attach stores flat "<sha256>.<kind>" under the attachments root; anything else is outside it.
_PATH = re.compile(r"([0-9a-f]{64})\.([a-z0-9]{1,8})")
_NAME_MAX = 120
_CAP_LIMIT = 50 * 1024 * 1024  # Telegram Bot API upload limit
_CHAT_LIMIT = 2**53  # Telegram chat ids fit in 52 bits


class _Refused(Exception):
    def __init__(self, reason, note=None, pointless=False):
        super().__init__(reason)
        self.reason, self.note, self.pointless = reason, note, pointless


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _str(value, field):
    if not isinstance(value, str) or not value:
        raise _Refused("bad_" + field)
    return value


def _config(config):
    if not isinstance(config, Mapping):
        raise _Refused("bad_config")
    enabled, chat_id = config.get("telegram_enabled"), config.get("chat_id")
    kinds, cap = config.get("kinds"), config.get("max_bytes")
    if not isinstance(enabled, bool):
        raise _Refused("bad_config")
    if not enabled:
        raise _Refused("channel_off", note=CHANNEL_OFF_NOTE)
    if not _int(chat_id) or not 0 < chat_id < _CHAT_LIMIT or not _int(cap) or not 0 < cap <= _CAP_LIMIT:
        raise _Refused("bad_config")
    if not isinstance(kinds, (frozenset, set, tuple, list)) or not all(isinstance(k, str) for k in kinds):
        raise _Refused("bad_config")
    return chat_id, frozenset(kinds), cap


def _request(request):
    if not isinstance(request, Mapping):
        raise _Refused("bad_request")
    request_id = request.get("id")
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise _Refused("bad_request")
    if request.get("source") != SOURCE:
        raise _Refused("not_from_pc_view")
    if request.get("by") != OWNER:
        raise _Refused("not_from_owner")
    if request.get("command") != COMMAND:
        raise _Refused("bad_command")
    at = _str(request.get("at"), "time")
    if len(at) > 64 or not at.isascii():
        raise _Refused("bad_time")
    try:
        datetime.fromisoformat(at)
    except ValueError as exc:
        raise _Refused("bad_time") from exc
    target = request.get("attachment_id")
    if not isinstance(target, str):
        raise _Refused("one_attachment_only" if isinstance(target, (list, tuple, set, frozenset)) else "bad_attachment_id")
    if target.strip().casefold() in _BULK:
        raise _Refused("one_attachment_only")
    if not _ID.fullmatch(target):
        raise _Refused("bad_attachment_id")
    return request_id, target, at


def _unused(request_id, used):
    if isinstance(used, (str, bytes)) or not isinstance(used, Collection):
        raise _Refused("bad_used")
    if request_id in used:
        raise _Refused("request_used")


def _attachment(record, target, kinds, cap):
    if record is None:
        raise _Refused("not_in_log")
    if not isinstance(record, Mapping):
        raise _Refused("bad_attachment")
    if record.get("id") != target:
        raise _Refused("not_in_log")
    origin, on_phone = record.get("origin"), record.get("on_phone")
    if not isinstance(on_phone, bool) or origin not in ("pc", "telegram"):
        raise _Refused("bad_attachment")
    if origin == "telegram" or on_phone:
        raise _Refused("already_on_phone", pointless=True)
    kind, size, path = record.get("kind"), record.get("bytes"), record.get("path")
    if not isinstance(kind, str) or kind not in kinds:
        raise _Refused("kind_not_allowed")
    if not _int(size) or size < 1:
        raise _Refused("bad_attachment")
    if size > cap:
        raise _Refused("too_large")
    match = _PATH.fullmatch(path) if isinstance(path, str) else None
    if match is None:
        raise _Refused("path_outside_root")
    if match.group(2) != kind:
        raise _Refused("kind_mismatch")
    return kind, path, record.get("display_name")


def _file_name(name, kind, redact):
    """The shown name, or a neutral one when the filter flags it, refuses or fails."""
    neutral = "attachment." + kind
    if (not isinstance(name, str) or not name.isprintable() or len(name) > _NAME_MAX
            or not name.lower().endswith("." + kind)):
        return neutral, True
    try:
        result = redact(name)
        clean = getattr(result, "flagged", None) is False
    except Exception:  # a broken filter is treated as flagged, never as clean
        clean = False
    return (name, False) if clean else (neutral, True)


def decide(request, attachment, config, *, redact=None, used=None):
    """Return {"ok": True, "action", "log"} or {"ok": False, "reason", "note", "pointless", "log"}.

    request: the PC-view command; attachment: the log record for its id (None when absent);
    config: telegram_enabled, chat_id, kinds, max_bytes; redact: TALKREDACT2-shaped callable;
    used: request ids already consumed. Missing redact/used are refused, never defaulted open.
    """
    log = {"event": COMMAND, "by": None, "request_id": None, "attachment_id": None, "at": None}
    try:
        request_id, target, at = _request(request)
        log.update(by=OWNER, request_id=request_id, attachment_id=target, at=at)
        _unused(request_id, used)
        chat_id, kinds, cap = _config(config)
        kind, path, name = _attachment(attachment, target, kinds, cap)
        if not callable(redact):
            raise _Refused("bad_redact")
        file_name, renamed = _file_name(name, kind, redact)
    except _Refused as refused:
        log.update(decision="refused", reason=refused.reason)
        return {"ok": False, "reason": refused.reason, "note": refused.note,
                "pointless": refused.pointless, "log": log}
    except Exception:  # hostile Mapping subclasses, RecursionError: fail closed, never escape
        log.update(decision="refused", reason="internal_error")
        return {"ok": False, "reason": "internal_error", "note": None, "pointless": False, "log": log}
    log.update(decision="upload", reason=None)
    action = {"kind": "upload", "chat_id": chat_id, "attachment_id": target, "path": path,
              "file_name": file_name, "name_replaced": renamed}
    return {"ok": True, "action": action, "log": log}
