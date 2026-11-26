"""FACEKEY1: where a remote window keeps its device key and server address on the client PC.

* The files live in one fixed folder under the user's LOCALAPPDATA; any other location is refused.
* The key is stored only as a DPAPI blob for the current Windows user; the protect functions are
  injected, so tests use fakes. Nothing is ever written unprotected.
* The server address is plain JSON beside it: an IPv4 literal in 127.0.0.0/8 or 100.64.0.0/10 and a
  port in 1024-65535, validated on save and on load.
* Every call returns a value or a Refusal and never raises; the key never appears in a repr or a
  Refusal reason.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import tempfile
from dataclasses import dataclass

APP_FOLDER = ("JARVIS", "face")  # fixed: the caller names only the LOCALAPPDATA root
KEY_FILE = "device.key"
SERVER_FILE = "server.json"
KEY_MIN, KEY_MAX = 16, 512
KEY_BLOB_CAP = 16 * 1024  # a DPAPI blob of a 512-char key is well under this
SERVER_CAP = 4096
PORT_MIN, PORT_MAX = 1024, 65535
_TOKEN = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_NETWORKS = (ipaddress.IPv4Network("127.0.0.0/8"), ipaddress.IPv4Network("100.64.0.0/10"))
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{p}{n}" for p in ("COM", "LPT") for n in "0123456789\u00b9\u00b2\u00b3"}
_BAD_CHARS = set('<>"|?*/:~') | set("\u2215\u2044\uff0f\uff3c\u29f5\u29f8\u29f9\ufe68\uff1a\u2236")


@dataclass(frozen=True)
class Refusal:
    reason: str


class DeviceKey:
    """The loaded key; repr and str never show it, and it cannot be copied by pickle."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        return self._value

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("DeviceKey is immutable")

    def __repr__(self) -> str:
        return "DeviceKey(<hidden>)"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("DeviceKey cannot be pickled")


def _valid_key(key: object) -> bool:
    return type(key) is str and KEY_MIN <= len(key) <= KEY_MAX and all(c in _TOKEN for c in key)


def _valid_part(part: str) -> bool:
    if not part or part in (".", "..") or part[-1] in ". " or not part.isprintable():
        return False
    if any(c in _BAD_CHARS or c == "\\" for c in part):
        return False
    return part.split(".")[0].rstrip(" ").upper() not in _RESERVED


def _check_root(root: object) -> str | None:
    """The root as given if it is a plain local drive path (X:\\a\\b), else None."""
    if type(root) is not str or not 3 < len(root) <= 240:
        return None
    if not (root[0].isascii() and root[0].isalpha() and root[1:3] == ":\\"):
        return None  # relative, drive-relative, UNC, \\?\ and \\.\ prefixes all fail here
    if not all(_valid_part(p) for p in root[3:].split("\\")):
        return None
    return root


def _check_server(ip: object, port: object) -> tuple[str, int] | None:
    if type(ip) is not str or type(port) is not int or not PORT_MIN <= port <= PORT_MAX:
        return None
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    if str(address) != ip or not any(address in net for net in _NETWORKS):
        return None
    return ip, port


def _bounded_read(path: str, cap: int) -> bytes | None:
    """At most cap bytes, or None; the size is checked before anything is read."""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size > cap:
                return None
            data = f.read(cap + 1)
    except (OSError, ValueError):
        return None
    return data if len(data) <= cap else None


def _no_duplicates(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name: str) -> None:
    raise ValueError("non-finite number")


class KeyStore:
    """root is the LOCALAPPDATA folder; the files always live in root\\JARVIS\\face."""

    __slots__ = ("_root", "_protect", "_unprotect")

    def __init__(self, root: object, protect, unprotect) -> None:
        self._root = _check_root(root)  # None: every call refuses
        self._protect = protect
        self._unprotect = unprotect

    def __repr__(self) -> str:
        return f"KeyStore(root={self._root!r})"

    def _inside(self, path: str) -> bool:
        try:
            real_root = os.path.normcase(os.path.realpath(self._root, strict=True))
            real = os.path.normcase(os.path.realpath(path))
        except (OSError, ValueError):
            return False
        return real.startswith(real_root.rstrip("\\") + "\\")

    def _place(self, name: str, create: bool) -> str | Refusal:
        if self._root is None:
            return Refusal("location refused")
        folder = os.path.join(self._root, *APP_FOLDER)
        path = os.path.join(folder, name)
        try:
            if create:
                os.makedirs(folder, exist_ok=True)
        except (OSError, ValueError):
            return Refusal("folder unavailable")
        return path if self._inside(path) else Refusal("location refused")

    def _write(self, name: str, data: bytes) -> Refusal | None:
        path = self._place(name, create=True)
        if isinstance(path, Refusal):
            return path
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if not self._inside(path):
                raise OSError("moved outside the root")
            os.replace(tmp, path)
        except (OSError, ValueError):
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return Refusal("write failed")
        return None

    def save(self, key: object) -> Refusal | None:
        if not _valid_key(key):
            return Refusal("key refused")
        plain = key.encode("ascii")
        try:
            blob = self._protect(plain)
        except Exception:  # the protect error may carry the key; never pass it on
            return Refusal("protect failed")
        if type(blob) is not bytes or not blob or len(blob) > KEY_BLOB_CAP or plain in blob:
            return Refusal("protect failed")  # fail closed: never store the key unprotected
        return self._write(KEY_FILE, blob)

    def load(self) -> DeviceKey | Refusal:
        path = self._place(KEY_FILE, create=False)
        if isinstance(path, Refusal):
            return path
        blob = _bounded_read(path, KEY_BLOB_CAP)
        if not blob:
            return Refusal("no key")
        try:
            plain = self._unprotect(blob)
        except Exception:  # tampered blob, other user, or a fake that echoes the key
            return Refusal("key unreadable")
        if type(plain) is not bytes or not plain.isascii():
            return Refusal("key unreadable")
        key = plain.decode("ascii")
        return DeviceKey(key) if _valid_key(key) else Refusal("key unreadable")

    def save_address(self, ip: object, port: object) -> Refusal | None:
        server = _check_server(ip, port)
        if server is None:
            return Refusal("address refused")
        return self._write(SERVER_FILE, json.dumps({"ip": server[0], "port": server[1]}).encode("ascii"))

    def load_address(self) -> tuple[str, int] | Refusal:
        path = self._place(SERVER_FILE, create=False)
        if isinstance(path, Refusal):
            return path
        data = _bounded_read(path, SERVER_CAP)
        if not data:
            return Refusal("no address")
        try:
            obj = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates,
                             parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return Refusal("address unreadable")
        if type(obj) is not dict or set(obj) != {"ip", "port"}:
            return Refusal("address unreadable")
        server = _check_server(obj["ip"], obj["port"])
        return Refusal("address refused") if server is None else server

    def clear(self) -> Refusal | None:
        for name in (KEY_FILE, SERVER_FILE):
            path = self._place(name, create=False)
            if isinstance(path, Refusal):
                return path
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                return Refusal("clear failed")
        return None


def default(environ=None) -> KeyStore | Refusal:
    """The store in the real LOCALAPPDATA, protected with DPAPI for the current Windows user."""
    if sys.platform != "win32":
        return Refusal("DPAPI unavailable on this platform")
    base = (os.environ if environ is None else environ).get("LOCALAPPDATA")
    if type(base) is not str or not base:
        return Refusal("LOCALAPPDATA missing")
    if not (base[:1].isascii() and base[:1].isalpha() and base[1:3] == ":\\"):
        return Refusal("location refused")  # before realpath, so a UNC value is never touched
    try:
        base = os.path.realpath(base)  # expands 8.3 short names; a UNC value stays UNC and is refused
    except (OSError, ValueError):
        return Refusal("location refused")
    if _check_root(base) is None:
        return Refusal("location refused")
    import ctypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]

    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return Refusal("DPAPI unavailable")
    sig = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
           ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_Blob)]
    for fn in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
        fn.argtypes, fn.restype = sig, ctypes.c_int
    kernel32.LocalFree.argtypes, kernel32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p

    def call(fn, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _Blob()
        if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # UI_FORBIDDEN
            raise OSError("DPAPI call failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))

    return KeyStore(base, lambda data: call(crypt32.CryptProtectData, data),
                    lambda blob: call(crypt32.CryptUnprotectData, blob))
