"""Pure decision for "Telefona gönder": one PC attachment to Salih's Telegram chat.

ADR K1 (Salih 2026-11-25): an attachment goes to the phone only when Salih asks
for that one attachment from the PC view. Nothing here does I/O or grants
anything; the caller runs the existing send path with the returned action and
stores the returned log record (its request_id marks the request as used).
Every bad input becomes a typed refusal with an ASCII reason code.
"""

import re
from collections.abc import Mapping
from datetime import datetime

OWNER = "salih"
SOURCE = "pc_view"
COMMAND = "send_to_phone"
CHANNEL_OFF_NOTE = "kanal kapalı"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_BULK = {"all", "hepsi", "tumu", "tümü", "*"}
# talk_attach stores flat "<sha256>.<kind>" under the attachments root; anything else is outside it.
_PATH = re.compile(r"([0-9a-f]{64})\.([a-z0-9]{1,8})")
_KIND = re.compile(r"[a-z0-9]{1,8}")
_NAME_MAX = 120
_CAP_LIMIT = 50 * 1024 * 1024  # Telegram Bot API upload limit
_CHAT_LIMIT = 2**53  # Telegram chat ids fit in 52 bits
_LISTS = (frozenset, set, tuple, list)
_FATAL = (KeyboardInterrupt, SystemExit)  # the operator stopping the process is not a refusal
_REASONS = frozenset({
    "bad_config", "channel_off", "bad_request", "not_from_pc_view", "not_from_owner", "bad_command",
    "bad_time", "one_attachment_only", "bad_attachment_id", "bad_used", "request_used", "not_in_log",
    "bad_attachment", "already_on_phone", "kind_not_allowed", "too_large", "path_outside_root",
    "kind_mismatch", "bad_redact", "internal_error"})


class _Refused(Exception):
    """Internal refusal; decide() trusts only its exact type and a reason from _REASONS."""


def _reason(refused):
    # A hostile Mapping can raise a forged or uninitialised _Refused: read only the args descriptor.
    args = refused.args if type(refused) is _Refused else ()
    reason = args[0] if len(args) == 1 else None
    return reason if type(reason) is str and reason in _REASONS else "internal_error"


def _guard(call):
    """Run caller-object code; anything it raises, even a forged _Refused, is internal_error."""
    try:
        return call()
    except _FATAL:
        raise
    except BaseException:
        raise _Refused("internal_error") from None


def _get(mapping, key):
    return _guard(lambda: mapping.get(key))  # the .get lookup itself is caller code too


def _mapping(value):
    return _guard(lambda: isinstance(value, Mapping))


# Exact builtin types only: subclasses can override ==, hash, ordering and str methods.
def _int(value):
    return type(value) is int


def _text(value):
    return type(value) is str


def _is(value, expected):
    return type(value) is str and value == expected


def _list(value):
    # By identity: `type(v) in _LISTS` calls the caller's metaclass __eq__.
    return any(type(value) is kind for kind in _LISTS)


def _strs(value):
    """A builtin collection of exact strs as a frozenset, else None."""
    if not _list(value):
        return None
    items = _guard(lambda: tuple(value))
    return frozenset(items) if all(type(item) is str for item in items) else None


def _channel(config):
    """telegram_enabled, read once and first: a closed channel always answers with its note."""
    if not _mapping(config):
        raise _Refused("bad_config")
    enabled = _get(config, "telegram_enabled")
    if type(enabled) is not bool:
        raise _Refused("bad_config")
    return enabled


def _config(config):
    chat_id, kinds, cap = _get(config, "chat_id"), _strs(_get(config, "kinds")), _get(config, "max_bytes")
    if not _int(chat_id) or not 0 < chat_id < _CHAT_LIMIT or not _int(cap) or not 0 < cap <= _CAP_LIMIT:
        raise _Refused("bad_config")
    if kinds is None or not all(_KIND.fullmatch(kind) for kind in kinds):
        raise _Refused("bad_config")
    return chat_id, kinds, cap


def _request(request):
    if not _mapping(request):
        raise _Refused("bad_request")
    request_id = _get(request, "id")
    if not _text(request_id) or not _ID.fullmatch(request_id):
        raise _Refused("bad_request")
    if not _is(_get(request, "source"), SOURCE):
        raise _Refused("not_from_pc_view")
    if not _is(_get(request, "by"), OWNER):
        raise _Refused("not_from_owner")
    if not _is(_get(request, "command"), COMMAND):
        raise _Refused("bad_command")
    at = _get(request, "at")
    if not _text(at) or not at or len(at) > 64 or not at.isascii():
        raise _Refused("bad_time")
    try:
        datetime.fromisoformat(at)
    except ValueError as exc:
        raise _Refused("bad_time") from exc
    target = _get(request, "attachment_id")
    if not _text(target):
        raise _Refused("one_attachment_only" if _list(target) else "bad_attachment_id")
    if target.strip().casefold() in _BULK:
        raise _Refused("one_attachment_only")
    if not _ID.fullmatch(target):
        raise _Refused("bad_attachment_id")
    return request_id, target, at


def _unused(request_id, used):
    used = _strs(used)
    if used is None:
        raise _Refused("bad_used")
    if request_id in used:
        raise _Refused("request_used")


def _attachment(record, target, kinds, cap):
    if record is None:
        raise _Refused("not_in_log")
    if not _mapping(record):
        raise _Refused("bad_attachment")
    if not _is(_get(record, "id"), target):
        raise _Refused("not_in_log")
    origin, on_phone = _get(record, "origin"), _get(record, "on_phone")
    if type(on_phone) is not bool or not _text(origin) or origin not in ("pc", "telegram"):
        raise _Refused("bad_attachment")
    if origin == "telegram" or on_phone:
        raise _Refused("already_on_phone")
    kind, size, path = _get(record, "kind"), _get(record, "bytes"), _get(record, "path")
    if not _text(kind) or kind not in kinds:
        raise _Refused("kind_not_allowed")
    if not _int(size) or size < 1:
        raise _Refused("bad_attachment")
    if size > cap:
        raise _Refused("too_large")
    match = _PATH.fullmatch(path) if _text(path) else None
    if match is None:
        raise _Refused("path_outside_root")
    if match.group(2) != kind:
        raise _Refused("kind_mismatch")
    return kind, path, _get(record, "display_name")


def _file_name(name, kind, redact):
    """The shown name, or a neutral one when the filter flags it, refuses or fails."""
    neutral = "attachment." + kind
    if (not _text(name) or not name.isprintable() or len(name) > _NAME_MAX
            or not name.lower().endswith("." + kind)):
        return neutral, True
    try:
        result = redact(name)
        clean = getattr(result, "flagged", None) is False
    except _FATAL:
        raise
    except BaseException:  # a broken filter is treated as flagged, never as clean
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
        enabled = _channel(config)
        try:
            request_id, target, at = _request(request)
        except _FATAL:
            raise
        except BaseException:
            if enabled:
                raise
            raise _Refused("channel_off") from None  # a closed channel wins over any bad request
        log.update(by=OWNER, request_id=request_id, attachment_id=target, at=at)
        if not enabled:
            raise _Refused("channel_off")
        _unused(request_id, used)
        chat_id, kinds, cap = _config(config)
        kind, path, name = _attachment(attachment, target, kinds, cap)
        if not callable(redact):
            raise _Refused("bad_redact")
        file_name, renamed = _file_name(name, kind, redact)
    except _FATAL:
        raise
    except BaseException as exc:  # hostile Mappings, RecursionError: never escape
        reason = _reason(exc)
        log.update(decision="refused", reason=reason)
        return {"ok": False, "reason": reason, "note": CHANNEL_OFF_NOTE if reason == "channel_off" else None,
                "pointless": reason == "already_on_phone", "log": log}
    log.update(decision="upload", reason=None)
    action = {"kind": "upload", "chat_id": chat_id, "attachment_id": target, "path": path,
              "file_name": file_name, "name_replaced": renamed}
    return {"ok": True, "action": action, "log": log}
