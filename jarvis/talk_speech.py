"""Speech seam (AUD0 F9): speech-to-text / text-to-speech interfaces, fake engines, voice steps.

No engine is installed here and no audio ships: engines and helpers are injected callables.
Pure and deterministic: no I/O, no network, no model calls; nothing in the running app
imports this module. Every public step returns {"ok": True, ...} or a typed refusal
{"ok": False, "reason": <ASCII code>}; no exception escapes.
"""

import hashlib
import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Protocol

AUDIO_KIND = "audio"
MAX_TEXT = 4000
MAX_HINT = 200
MAX_SECONDS = 3600.0
MAX_AUDIO_BYTES = 25 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"{p}{n}" for p in ("COM", "LPT") for n in range(10)]
)
_HINT_BOUNDARY = re.compile(r"[,;\s]+")
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


class SpeechToText(Protocol):
    def transcribe(self, audio_record, hint: str) -> tuple:
        """Return (text, seconds) for the audio record."""


class TextToSpeech(Protocol):
    def speak(self, text: str):
        """Return an audio record for the spoken text."""


def _refuse(reason, **extra):
    # Reasons are fixed ASCII codes; user or engine text is never echoed into a refusal.
    return {"ok": False, "reason": reason, **extra}


def _not_understood(flag):
    return _refuse("not_understood", flag=flag, fallback="text")


def _guard(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RecursionError:
            return _refuse("input_too_deep")
        except Exception:
            return _refuse("internal_error")
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def _field(record, name):
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _safe_relative(path):
    """Text-only check that path stays under the caller's root: no '..', absolute, UNC, ':' or '~digit'."""
    if not isinstance(path, str) or not path or len(path) > 260:
        return False
    for segment in path.split("/"):
        # The allowlist refuses '\\', ':', '~', leading '.', spaces and non-ASCII look-alikes.
        if not _SEGMENT.fullmatch(segment) or segment.endswith("."):
            return False
        if segment.split(".")[0].upper() in _RESERVED:
            return False
    return True


def check_audio_record(record):
    """None if record is a usable audio record, else an ASCII refusal code."""
    if _field(record, "kind") != AUDIO_KIND:
        return "record_wrong_kind"
    digest = _field(record, "sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        return "record_no_sha256"
    size = _field(record, "bytes")
    if type(size) is not int or not 0 < size <= MAX_AUDIO_BYTES:
        return "record_bad_size"
    if not _safe_relative(_field(record, "relative_path")):
        return "record_bad_path"
    return None


def cut_hint(hint):
    """Cut hint to MAX_HINT at a term boundary (comma, semicolon or space); '' if no whole term fits."""
    if not isinstance(hint, str):
        return ""
    hint = hint.strip()
    if len(hint) <= MAX_HINT:
        return hint
    head = hint[:MAX_HINT + 1]
    cut = None
    for match in _HINT_BOUNDARY.finditer(head):
        cut = match.start()
    return head[:cut].rstrip(",; \t\r\n") if cut else ""


def _clean_text(text):
    """None if text is a usable transcript, else a flag."""
    if not isinstance(text, str):
        return "not_text"
    if len(text) > MAX_TEXT:
        return "too_long"
    if not text.strip():
        return "empty"
    if any(unicodedata.category(ch) in _BAD_CATEGORIES and ch not in "\n\t" for ch in text):
        return "unsafe_text"
    return None


def _seconds_ok(seconds):
    # Range check first: math.isfinite overflows on huge ints; NaN and inf fail the range; -0.0 refused.
    return (type(seconds) in (int, float) and 0 <= seconds <= MAX_SECONDS
            and math.copysign(1, seconds) > 0)


@_guard
def voice_in(audio_record, stt, build_hint, hear):
    """Voice in: check the record, transcribe with a glossary hint, pass the text to hear.

    The record is checked before any collaborator runs. build_hint() takes no arguments;
    a failing build_hint means an empty hint. Returns {"ok": True, "text", "seconds",
    "heard"} or a "not_understood" refusal with a flag and fallback "text" (type instead).
    """
    problem = check_audio_record(audio_record)
    if problem:
        return _refuse(problem)
    if stt is None:
        return _not_understood("no_engine")
    try:
        hint = cut_hint(build_hint())
    except Exception:
        hint = ""
    try:
        result = stt.transcribe(audio_record, hint)
    except Exception:
        return _not_understood("engine_failed")
    if not isinstance(result, (tuple, list)) or len(result) != 2:
        return _not_understood("bad_result")
    text, seconds = result
    flag = _clean_text(text)
    if flag:
        return _not_understood(flag)
    if not _seconds_ok(seconds):
        return _not_understood("bad_seconds")
    try:
        heard = hear(text)
    except Exception:
        return _not_understood("hear_failed")
    return {"ok": True, "text": text, "seconds": float(seconds), "heard": heard}


def _text_only(text, flag):
    return {"ok": True, "audio": None, "text": text, "flag": flag}


@_guard
def voice_out(text, tts, prepare):
    """Voice out: prepare(text) gives the spoken form (None = as is), tts.speak makes audio.

    The text is always left for reading. No engine, a failing engine or a bad audio record
    gives text only, flagged. Returns {"ok": True, "audio", "text", "flag"} or a refusal.
    """
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        return _refuse("text_invalid")
    if not text.strip():
        return _refuse("text_empty")
    if tts is None:
        return _text_only(text, "no_engine")
    try:
        spoken = text if prepare is None else prepare(text)
    except Exception:
        return _text_only(text, "prepare_failed")
    flag = _clean_text(spoken)
    if flag:
        return _text_only(text, flag)
    try:
        audio = tts.speak(spoken)
        bad = check_audio_record(audio)  # inside the try: a hostile record mapping may raise
    except Exception:
        return _text_only(text, "engine_failed")
    if bad:
        return _text_only(text, "bad_audio")
    return {"ok": True, "audio": audio, "text": text, "flag": None}


def fake_audio_record(content):
    """Deterministic audio record for bytes content (fakes and tests only)."""
    digest = hashlib.sha256(content).hexdigest()
    return {"kind": AUDIO_KIND, "sha256": digest, "bytes": len(content),
            "relative_path": f"voice/{digest[:16]}.wav"}


class FakeSpeechToText:
    """Deterministic SpeechToText: returns (text, seconds) or raises error; records calls."""

    def __init__(self, text="merhaba", seconds=1.0, error=None):
        self.text, self.seconds, self.error = text, seconds, error
        self.calls = []

    def transcribe(self, audio_record, hint):
        self.calls.append((audio_record, hint))
        if self.error is not None:
            raise self.error
        return (self.text, self.seconds)


class FakeTextToSpeech:
    """Deterministic TextToSpeech: returns record, else a record of the UTF-8 text; or raises error."""

    def __init__(self, record=None, error=None):
        self.record, self.error = record, error
        self.calls = []

    def speak(self, text):
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        if self.record is not None:
            return self.record
        return fake_audio_record(text.encode("utf-8"))
