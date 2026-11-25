"""FACECAP1: one screenshot, only on Salih's click, handed to attachment intake.

Not computer use: the brain and Telegram text never trigger it. A capture needs a
single-use click token that only the UI factory makes. Collaborators are injected;
this module writes nothing and keeps no image. Nothing in the running app imports it.
"""
from __future__ import annotations

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_AGE = 30.0  # seconds between the click and the capture

_KEY = object()  # only click_token() holds it, so only the factory builds a token
_ISSUED: dict = {}  # token -> creation time, kept here so a holder cannot reset it on the token
_USED: set = set()


class _ClickToken:
    __slots__ = ("_created", "__weakref__")

    def __init__(self, key: object, created: float) -> None:
        if key is not _KEY:
            raise TypeError("click tokens come from click_token() only")
        self._created = created

    def __reduce_ex__(self, protocol):  # no copy or pickle: a copy would be a second use
        raise TypeError("click tokens cannot be copied")


class CaptureResult:
    """ok=True carries the intake's record; ok=False carries an ASCII reason only."""

    __slots__ = ("ok", "reason", "record")

    def __init__(self, ok: bool, reason: str, record: object = None) -> None:
        for name, value in (("ok", ok), ("reason", reason), ("record", record)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: object) -> None:  # immutable: _INTERNAL_ERROR is shared
        raise AttributeError("CaptureResult is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("CaptureResult is immutable")

    def __repr__(self) -> str:
        return f"CaptureResult(ok={self.ok}, reason={self.reason!r})"


def _refuse(reason: str) -> CaptureResult:
    return CaptureResult(False, reason)


_INTERNAL_ERROR = CaptureResult(False, "internal_error")


def _finite(value: object) -> bool:
    return type(value) in (int, float) and value - value == 0  # rejects bool, NaN, inf


def click_token(now) -> _ClickToken | None:
    """The UI layer's factory: one token per click on "Ekran goruntusu al"; None on a bad clock."""
    try:
        created = now()
        if not _finite(created):
            return None
        token = _ClickToken(_KEY, created)
        _ISSUED[token] = created
        return token
    except Exception:  # RecursionError included
        return None


def capture(token, grab, intake, before, after, now, *,
            max_bytes: int = DEFAULT_MAX_BYTES, max_age: float = DEFAULT_MAX_AGE) -> CaptureResult:
    """One capture for one click: before, grab, after (always), then intake(png) -> record."""
    try:
        return _capture(token, grab, intake, before, after, now, max_bytes, max_age)
    except Exception:  # RecursionError included; nothing escapes
        return _INTERNAL_ERROR  # prebuilt: no call here, so a near-full stack cannot overflow again


def _capture(token, grab, intake, before, after, now, max_bytes, max_age) -> CaptureResult:
    if type(token) is not _ClickToken:
        return _refuse("no_token")
    if token in _USED:
        return _refuse("token_used")
    if token not in _ISSUED:
        return _refuse("no_token")
    created = _ISSUED.pop(token)
    _USED.add(token)  # burned before anything runs: a failed attempt is still its one use (UI thread only)
    if not all(callable(f) for f in (grab, intake, before, after, now)):
        return _refuse("bad_collaborator")
    if type(max_bytes) is not int or max_bytes <= 0 or not _finite(max_age) or max_age <= 0:
        return _refuse("bad_limit")
    clock = now()
    if not _finite(clock):
        return _refuse("bad_clock")
    try:
        age = clock - created
    except OverflowError:
        return _refuse("bad_clock")
    if not 0 <= age <= max_age:
        return _refuse("token_expired")

    failure, png = "", None
    try:
        before()
    except Exception:
        failure = "before_failed"
    else:
        try:
            png = grab()
        except Exception:
            failure = "grab_failed"
    finally:
        try:
            after()  # the window comes back whatever happened
        except Exception:
            failure = failure or "after_failed"
    if failure:
        return _refuse(failure)
    if type(png) is not bytes or not png:
        return _refuse("empty" if png == b"" else "not_png")
    if len(png) > max_bytes:
        return _refuse("too_large")
    if not png.startswith(PNG_MAGIC):
        return _refuse("not_png")
    try:
        record = intake(png)
    except Exception:
        return _refuse("intake_refused")
    if record is None:
        return _refuse("intake_refused")
    return CaptureResult(True, "captured", record)


def qt_grab_screen() -> bytes:
    """The real grabber, injected by the UI as grab=; tests use a fake and never call it."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QGuiApplication

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    QGuiApplication.primaryScreen().grabWindow(0).save(buffer, "PNG")
    return bytes(buffer.data())
