"""TGCLIENT1: the only code that talks to Telegram (Bot API over HTTPS).

Sending, polling and file download with the ADR K1/K4 limits built in: a fixed
method allowlist, one host (https://api.telegram.org, port 443), no proxy, no
redirect, certificate checks on, no retry. The bot token comes from a getter at
call time, is never kept and never appears in a result, repr or log record;
every error text is redacted. Every public call returns Ok, Refused or Failed;
no exception escapes. Nothing in the running app imports this module yet.
"""

from __future__ import annotations

import json
import logging
import math
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from jarvis.telegram_files import FILE_MIMES, VOICE_MIMES, DownloadPlan

API_HOST = "api.telegram.org"
ALLOWED_METHODS = frozenset(
    {"getUpdates", "sendMessage", "sendVoice", "sendPhoto", "sendDocument", "getFile"}
)
SEND_METHODS = frozenset({"sendMessage", "sendVoice", "sendPhoto", "sendDocument"})
RESULT_TYPES = {"getUpdates": list, "getFile": dict, **{m: dict for m in SEND_METHODS}}
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Bot API getFile limit
DEFAULT_MAX_RETRY_AFTER = 300
MAX_FILE_PATH = 256
MAX_ISSUED = 64
MAX_DETAIL = 200
CHUNK = 64 * 1024

_LOG = logging.getLogger(__name__)
_TOKEN_RE = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{30,64}", re.ASCII)
# Telegram file paths look like "voice/file_12.oga": ASCII segments, a dot only in the last
# one (so "evil.com/x" is refused).
_FILE_PATH_RE = re.compile(r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9]+)?", re.ASCII)
# Any word holding a URL part ("://", "//", "user@host", the API host) or a token-shaped run.
# Run on input already cut to MAX_DETAIL * 4 chars, so backtracking stays bounded.
_URL_RE = re.compile(
    r"\S*(?://|@|api\.telegram\.org)\S*"
    r"|(?<![0-9])[0-9]{3,}:[A-Za-z0-9_-]{20,}",
    re.ASCII | re.IGNORECASE,
)


@dataclass(frozen=True)
class Ok:
    value: object


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class Failed:
    kind: str  # timeout | http_status | bad_response | too_large | network
    status: int | None = None
    retry_after: int | None = None
    detail: str = ""


class _TooLarge(Exception):
    pass


class _BadResponse(Exception):
    pass


def redact(text: object, token: str | None = None) -> str:
    """ASCII, single-line, bounded text with the token and every URL removed."""
    try:
        out = str(text)
        if token:
            out = out.replace(token, "[token]").replace(token.split(":", 1)[-1], "[token]")
        out = _URL_RE.sub("[url]", out[: MAX_DETAIL * 4])
        return "".join(c if " " <= c <= "~" else "?" for c in out)[:MAX_DETAIL]
    except Exception:
        return "[unprintable]"


def default_opener() -> urllib.request.OpenerDirector:
    """HTTPS only; no proxy (environment ignored), no redirect handler, certificates checked."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        urllib.request.HTTPDefaultErrorHandler(),  # every non-2xx, 3xx included, raises
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def _default_transport(request: urllib.request.Request, timeout: float) -> object:
    return default_opener().open(request, timeout=timeout)


def _describe(err: BaseException, token: str | None) -> str:
    try:
        text = f"{type(err).__name__}: {err}"
    except Exception:
        text = type(err).__name__
    return redact(text, token)


def _close(response: object) -> None:
    try:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    except Exception:  # nothing to recover; the answer is already decided
        pass


def _content_length(response: object) -> int | None:
    try:
        value = response.headers.get("Content-Length")
    except Exception:
        return None
    if type(value) is str and 0 < len(value) <= 20 and value.isascii() and value.isdigit():
        return int(value)
    return None


def _read_capped(response: object, limit: int, write: Callable | None) -> bytes | int:
    """Read at most limit + 1 bytes; raise _TooLarge past the limit."""
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(CHUNK, limit + 1 - total))
        if type(chunk) is not bytes:
            raise _BadResponse
        if not chunk:
            return total if write is not None else b"".join(parts)
        total += len(chunk)
        if total > limit:
            raise _TooLarge
        (write if write is not None else parts.append)(chunk)


def _no_duplicates(pairs: list) -> dict:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError("duplicate key")
    return out


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("float")
    return value


def _reject_constant(_name: str) -> object:
    raise ValueError("constant")


def _parse_json(raw: bytes) -> object:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_no_duplicates,
        parse_constant=_reject_constant,
        parse_float=_finite,
    )


def _encode(body: dict) -> bytes | None:
    if not all(type(key) is str for key in body):
        return None
    try:
        text = json.dumps(body, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    return text.encode("ascii")


def _valid_path(path: object) -> bool:
    return (type(path) is str and 0 < len(path) <= MAX_FILE_PATH
            and _FILE_PATH_RE.fullmatch(path) is not None)


def _outcome(result: object) -> str:
    if isinstance(result, Refused):
        return f"refused {result.reason}"
    if isinstance(result, Failed):
        return f"failed {result.kind} {result.status}"
    return "ok"


class TelegramClient:
    """Bot API client for one configured chat; it holds a token getter, never a token."""

    __slots__ = ("_token_getter", "_chat_id", "_transport", "_timeout", "_max_response",
                 "_max_download", "_max_retry_after", "_issued")

    def __init__(self, token_getter: Callable, chat_id: int, *, transport: Callable | None = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
                 max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
                 max_retry_after: int = DEFAULT_MAX_RETRY_AFTER) -> None:
        # No checks here so construction never raises; every call re-checks the config.
        self._token_getter = token_getter
        self._chat_id = chat_id
        self._transport = _default_transport if transport is None else transport
        self._timeout = timeout
        self._max_response = max_response_bytes
        self._max_download = max_download_bytes
        self._max_retry_after = max_retry_after
        self._issued: dict[str, int] = {}  # file path from getFile -> declared size

    def __repr__(self) -> str:
        return f"TelegramClient(host={API_HOST})"

    def call(self, method: object, body: object = None) -> Ok | Refused | Failed:
        label = method if type(method) is str and method in ALLOWED_METHODS else "call"
        return self._guard(label, lambda: self._call(method, body))

    def get_file(self, plan: object) -> Ok | Refused | Failed:
        return self._guard("getFile", lambda: self._get_file(plan))

    def download(self, file_path: object, sink: object) -> Ok | Refused | Failed:
        return self._guard("download", lambda: self._download(file_path, sink))

    def _guard(self, label: str, run: Callable) -> Ok | Refused | Failed:
        try:
            result = run()
        except Exception:  # RecursionError and hostile objects included: fail closed
            result = Refused("invalid_input")
        try:  # outcome only: never the body, the token or exc_info
            _LOG.info("telegram %s: %s", label, _outcome(result))
        except Exception:  # logging must not change the answer
            pass
        return result

    def _config_refusal(self) -> Refused | None:
        timeout = self._timeout
        if (type(timeout) not in (int, float) or timeout <= 0
                or (type(timeout) is float and not math.isfinite(timeout))):
            return Refused("bad_timeout")
        if not all(type(v) is int and v >= 1 for v in (self._max_response, self._max_download)):
            return Refused("bad_size_cap")
        if type(self._max_retry_after) is not int or self._max_retry_after < 0:
            return Refused("bad_retry_cap")
        if not callable(self._transport):
            return Refused("bad_transport")
        return None

    def _token(self) -> str | None:
        try:
            token = self._token_getter()
        except Exception:
            return None
        if type(token) is not str or _TOKEN_RE.fullmatch(token) is None:
            return None
        return token

    def _call(self, method: object, body: object) -> Ok | Refused | Failed:
        if type(method) is not str or method not in ALLOWED_METHODS:
            return Refused("method_not_allowed")
        if method == "getFile":
            return Refused("use_get_file")
        refusal = self._config_refusal()
        if refusal is not None:
            return refusal
        if body is not None and not isinstance(body, Mapping):
            return Refused("bad_body")
        body = {} if body is None else dict(body)  # one snapshot: checked and sent the same
        if method in SEND_METHODS:
            refusal = self._chat_refusal(body)
            if refusal is not None:
                return refusal
        payload = _encode(body)
        if payload is None:
            return Refused("bad_body")
        return self._api(method, payload)

    def _chat_refusal(self, body: dict) -> Refused | None:
        if type(self._chat_id) is not int:
            return Refused("bad_configured_chat_id")
        if "chat_id" not in body:
            return Refused("missing_chat_id")
        if type(body["chat_id"]) is not int:  # "123 ", "0123", True refused
            return Refused("bad_chat_id_type")
        if body["chat_id"] != self._chat_id:
            return Refused("chat_id_mismatch")
        return None

    def _api(self, method: str, payload: bytes) -> Ok | Refused | Failed:
        token = self._token()
        if token is None:
            return Refused("token_unavailable")
        request = urllib.request.Request(
            f"https://{API_HOST}/bot{token}/{method}", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        result = self._exchange(request, token, self._max_response, None)
        if not isinstance(result, Ok):
            return result
        try:
            envelope = _parse_json(result.value)
        except (ValueError, RecursionError):
            return Failed("bad_response")
        if (type(envelope) is not dict or envelope.get("ok") is not True
                or type(envelope.get("result")) is not RESULT_TYPES[method]):
            return Failed("bad_response")
        return Ok(envelope["result"])

    def _exchange(self, request: urllib.request.Request, token: str, limit: int,
                  write: Callable | None) -> Ok | Failed:
        """One transport call, no retry; a 3xx is a failure and its Location is never followed."""
        try:
            response = self._transport(request, self._timeout)
        except urllib.error.HTTPError as err:  # non-2xx from the default opener: read status
            response = err
        except TimeoutError:
            return Failed("timeout")
        except urllib.error.URLError as err:
            if isinstance(err.reason, TimeoutError):
                return Failed("timeout")
            return Failed("network", detail=_describe(err, token))
        except Exception as err:
            return Failed("network", detail=_describe(err, token))
        try:
            return self._answer(response, token, limit, write)
        except Exception:  # hostile response (a status property that raises): sent, so failed
            return Failed("bad_response")
        finally:
            _close(response)

    def _answer(self, response: object, token: str, limit: int,
                write: Callable | None) -> Ok | Failed:
        status = getattr(response, "status", None)
        if status is None:
            status = getattr(response, "code", None)
        if type(status) is not int:
            return Failed("bad_response")
        if status == 429:
            return Failed("http_status", 429, retry_after=self._retry_after(response))
        if status != 200:
            return Failed("http_status", status)
        declared = _content_length(response)
        if declared is not None and declared > limit:
            return Failed("too_large")
        try:
            return Ok(_read_capped(response, limit, write))
        except _TooLarge:
            return Failed("too_large")
        except _BadResponse:
            return Failed("bad_response")
        except TimeoutError:
            return Failed("timeout")
        except OSError as err:
            return Failed("network", detail=_describe(err, token))
        except Exception:
            return Failed("bad_response")

    def _retry_after(self, response: object) -> int:
        cap = self._max_retry_after
        try:
            wait = _parse_json(_read_capped(response, self._max_response, None))
            wait = wait["parameters"]["retry_after"]
        except Exception:  # missing or hostile: wait the full cap
            return cap
        return min(wait, cap) if type(wait) is int and wait >= 0 else cap

    def _get_file(self, plan: object) -> Ok | Refused | Failed:
        if (type(plan) is not DownloadPlan or type(plan.size) is not int or plan.size < 1
                or type(plan.file_id) is not str or type(plan.voice) is not bool
                or (VOICE_MIMES if plan.voice else FILE_MIMES).get(plan.mime_hint) != plan.kind
                or plan.extension != "." + plan.kind):  # a hand-built plan skips plan_download
            return Refused("bad_plan")
        refusal = self._config_refusal()
        if refusal is not None:
            return refusal
        if plan.size > self._max_download:
            return Refused("file_over_cap")
        payload = _encode({"file_id": plan.file_id})
        if payload is None:
            return Refused("bad_plan")
        result = self._api("getFile", payload)
        if not isinstance(result, Ok):
            return result
        path = result.value.get("file_path")
        if not _valid_path(path):
            return Refused("bad_file_path")
        size = result.value.get("file_size", plan.size)
        if type(size) is not int or not 0 < size <= plan.size:
            return Failed("bad_response")
        self._issued.pop(path, None)
        while len(self._issued) >= MAX_ISSUED:  # bounded: drop the oldest issued path
            del self._issued[next(iter(self._issued))]
        self._issued[path] = size
        return Ok(path)

    def _download(self, file_path: object, sink: object) -> Ok | Refused | Failed:
        """Stream an issued path into sink.write; on any failure call sink.discard()."""
        if not _valid_path(file_path):
            return Refused("bad_file_path")
        write = getattr(sink, "write", None)
        discard = getattr(sink, "discard", None)
        if not callable(write) or not callable(discard):
            return Refused("bad_sink")
        refusal = self._config_refusal()
        if refusal is not None:
            return refusal
        if file_path not in self._issued:
            return Refused("file_path_not_issued")
        token = self._token()
        if token is None:
            return Refused("token_unavailable")
        declared = self._issued.pop(file_path)  # single use
        request = urllib.request.Request(
            f"https://{API_HOST}/file/bot{token}/{file_path}", method="GET")
        result = self._exchange(request, token, min(declared, self._max_download), write)
        if isinstance(result, Ok) and result.value != declared:
            result = Failed("bad_response")  # shorter than getFile declared
        if not isinstance(result, Ok):
            try:
                discard()
            except Exception:  # the failure is already the answer
                pass
        return result
