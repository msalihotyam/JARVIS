"""One optional Windows hotkey for the talk toggle.

Use one HotKey on the message thread. The caller forwards the ID from
WM_HOTKEY on that thread and closes the registration before the thread exits.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Literal

_HOTKEY_ID = 0x4A56
_NO_REPEAT = 0x4000
_MODIFIERS = {"ALT": 0x1, "CTRL": 0x2, "SHIFT": 0x4, "WIN": 0x8}
_KEYS = {
    **{key: ord(key) for key in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "ESC": 0x1B, "ESCAPE": 0x1B, "SPACE": 0x20,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28, "INSERT": 0x2D, "DELETE": 0x2E,
}

Register = Callable[[int, int, int], bool]
Unregister = Callable[[int], bool]
State = Literal["off", "active", "taken", "invalid"]


@dataclass(frozen=True)
class KeySpec:
    vk: int | None
    modifiers: int


@dataclass(frozen=True)
class Refusal:
    reason: str


@dataclass(frozen=True)
class Status:
    state: State
    message: str


_OFF = Status("off", "Konuşma kısayolu kapalı.")
_ACTIVE = Status("active", "Konuşma kısayolu etkin.")
_TAKEN = Status("taken", "Bu kısayolu başka bir uygulama kullanıyor.")
_INVALID = Refusal("Geçersiz konuşma kısayolu.")


def parse(setting: object) -> KeySpec | Refusal:
    """Parse one letter, digit or named key and up to four distinct modifiers."""
    if type(setting) is not str or len(setting) > 64 or not setting.isascii() or not setting.isprintable():
        return _INVALID
    if not setting.strip():
        return KeySpec(None, 0)

    vk = None
    modifiers = 0
    for token in setting.upper().split("+"):
        name = token.strip()
        modifier = _MODIFIERS.get(name)
        if modifier is not None:
            if modifiers & modifier:
                return _INVALID
            modifiers |= modifier
        elif vk is None and name in _KEYS:
            vk = _KEYS[name]
        else:
            return _INVALID
    return _INVALID if vk is None else KeySpec(vk, modifiers)


class HotKey:
    def __init__(self, register: Register, unregister: Unregister, talk_toggle: Callable[[str], object]) -> None:
        self._register = register
        self._unregister = unregister
        self._talk_toggle = talk_toggle
        self._registered: KeySpec | None = None
        self._status = _OFF

    @property
    def state(self) -> State:
        return self._status.state

    @property
    def message(self) -> str:
        return self._status.message

    def apply(self, setting: object = "") -> Status:
        spec = parse(setting)
        if spec == self._registered:
            self._status = _ACTIVE
            return self._status

        if self._registered is not None:
            try:
                removed = bool(self._unregister(_HOTKEY_ID))
            except Exception:
                removed = False
            if not removed:
                self._status = Status("invalid", "Önceki konuşma kısayolu kaldırılamadı.")
                return self._status
            self._registered = None

        if isinstance(spec, Refusal):
            self._status = Status("invalid", spec.reason)
        elif spec.vk is None:
            self._status = _OFF
        else:
            try:
                registered = bool(self._register(_HOTKEY_ID, spec.modifiers | _NO_REPEAT, spec.vk))
            except Exception:
                self._status = Status("invalid", "Konuşma kısayolu kaydedilemedi.")
                return self._status
            if registered:
                self._registered = spec
            self._status = _ACTIVE if registered else _TAKEN
        return self._status

    def close(self) -> Status:
        return self.apply()

    def on_message(self, ident: object) -> bool:
        if self.state != "active" or type(ident) is not int or ident != _HOTKEY_ID:
            return False
        try:
            self._talk_toggle("hotkey")
        except Exception:
            return False
        return True


def default_os() -> tuple[Register, Unregister] | Refusal:
    if sys.platform != "win32":
        return Refusal("Genel konuşma kısayolu yalnızca Windows'ta kullanılabilir.")
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32")
        register = user32.RegisterHotKey
        register.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint)
        register.restype = ctypes.c_int
        unregister = user32.UnregisterHotKey
        unregister.argtypes = (ctypes.c_void_p, ctypes.c_int)
        unregister.restype = ctypes.c_int
    except Exception:
        return Refusal("Windows kısayol işlevleri başlatılamadı.")

    def register_on_thread(ident: int, flags: int, vk: int) -> bool:
        return bool(register(None, ident, flags, vk))

    def unregister_from_thread(ident: int) -> bool:
        return bool(unregister(None, ident))

    return register_on_thread, unregister_from_thread
