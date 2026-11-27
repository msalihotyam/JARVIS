"""FACECONF1: the service's own settings file, parsed strictly.

* The file is <root>\\talk\\state\\service.json; load(path) opens nothing else and only reads it.
* A missing file gives the defaults; an unreadable, oversized, non-UTF-8 or malformed file is a Refusal.
* Fields: net.port, net.remote_bind (an IPv4 literal in 100.64.0.0/10, or "" = remote off),
  net.blob_cap_bytes, telegram.enabled. Loopback is not a field: the service always binds it.
* Refusals name the field, never the value. The file never holds secrets: a key that looks like one is
  refused as "secret_in_config".
"""
from __future__ import annotations

import ipaddress
import json
import os
import unicodedata
from dataclasses import dataclass

RELATIVE = ("talk", "state", "service.json")
MAX_FILE_BYTES = 64 * 1024
MAX_PATH = 260
PORT_MIN, PORT_MAX = 1024, 65535
MB = 1024 * 1024
BLOB_CAP_MAX = 100 * MB
_REMOTE = ipaddress.IPv4Network("100.64.0.0/10")
_SECRET_WORDS = ("token", "key", "secret", "passw", "credential")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{p}{n}" for p in ("COM", "LPT") for n in "0123456789"}
_PROTECTED = {".claude", ".github", ".git", ".jarvis", "data"}  # the settings file never lives under these
_BAD_CHARS = set('<>"|?*/:~')
# field -> expected type; nested dicts are sections
_SCHEMA = {"net": {"port": int, "remote_bind": str, "blob_cap_bytes": int}, "telegram": {"enabled": bool}}


@dataclass(frozen=True)
class Refusal:
    reason: str
    field: str = ""


@dataclass(frozen=True)
class ServiceConfig:
    port: int = 48117
    remote_bind: str = ""  # "" = remote off
    blob_cap_bytes: int = 25 * MB
    telegram_enabled: bool = False


def _valid_part(part: str) -> bool:
    if not part or part in (".", "..") or part[-1] in ". " or not (part.isascii() and part.isprintable()):
        return False
    if any(c in _BAD_CHARS for c in part) or part.casefold() in _PROTECTED:
        return False
    return part.split(".")[0].rstrip(" ").upper() not in _RESERVED


def _check_path(path: object) -> str | None:
    """A plain local drive path X:\\...\\talk\\state\\service.json, else None."""
    try:
        path = os.fspath(path)
    except TypeError:
        return None
    if type(path) is not str or not 3 < len(path) <= MAX_PATH:
        return None
    if not (path[0].isascii() and path[0].isalpha() and path[1:3] == ":\\"):
        return None  # relative, drive-relative, UNC, \\?\ and \\.\ prefixes all fail here
    parts = path[3:].split("\\")
    if tuple(parts[-3:]) != RELATIVE or not all(_valid_part(p) for p in parts):
        return None
    return path


def _name(key: str) -> str:
    """The key as a field name when it is a short plain identifier; otherwise a placeholder."""
    return key if key.isascii() and key.isidentifier() and len(key) <= 32 else "?"


def _secret_like(key: str) -> bool:
    letters = "".join(c for c in unicodedata.normalize("NFKC", key).casefold() if c.isalpha())
    return any(word in letters for word in _SECRET_WORDS)


def _find_secret(tree) -> Refusal | None:
    stack = [(tree, "")]
    while stack:
        node, prefix = stack.pop()
        if type(node) is tuple:  # a JSON object, kept as its (key, value) pairs
            for key, value in node:
                if _secret_like(key):
                    return Refusal("secret_in_config", prefix + _name(key))
                stack.append((value, prefix + _name(key) + "."))
        elif type(node) is list:
            stack.extend((item, prefix) for item in node)
    return None


def _check_value(field: str, kind: type, value):
    if type(value) is not kind:  # bool is not int, float is not int
        return Refusal("wrong_type", field)
    if field == "net.port" and not PORT_MIN <= value <= PORT_MAX:
        return Refusal("bad_value", field)
    if field == "net.blob_cap_bytes" and not 1 <= value <= BLOB_CAP_MAX:
        return Refusal("bad_value", field)
    if field == "net.remote_bind" and value:
        try:
            address = ipaddress.IPv4Address(value)
        except ValueError:
            return Refusal("bad_value", field)
        if str(address) != value or address not in _REMOTE:
            return Refusal("bad_value", field)
    return None


def _fields(node, schema: dict, prefix: str, out: dict) -> Refusal | None:
    if type(node) is not tuple:
        return Refusal("wrong_type", prefix.rstrip(".") or "(root)")
    seen = set()
    for key, value in node:
        field = prefix + _name(key)
        if key in seen:
            return Refusal("duplicate_key", field)
        seen.add(key)
        if key not in schema:
            return Refusal("unknown_key", field)
        if type(schema[key]) is dict:
            refusal = _fields(value, schema[key], field + ".", out)
        else:
            refusal = _check_value(field, schema[key], value)
            out[field] = value
        if refusal:
            return refusal
    return None


def _reject_constant(name: str) -> None:
    raise ValueError("non-finite number")


def parse(raw: bytes) -> ServiceConfig | Refusal:
    if len(raw) > MAX_FILE_BYTES:
        return Refusal("too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Refusal("not_utf8")
    try:  # objects stay (key, value) tuples so duplicates and nesting are checked with their field names
        tree = json.loads(text, object_pairs_hook=tuple, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return Refusal("malformed")
    out: dict = {}
    refusal = _find_secret(tree) or _fields(tree, _SCHEMA, "", out)
    if refusal:
        return refusal
    base = ServiceConfig()
    return ServiceConfig(port=out.get("net.port", base.port),
                         remote_bind=out.get("net.remote_bind", base.remote_bind),
                         blob_cap_bytes=out.get("net.blob_cap_bytes", base.blob_cap_bytes),
                         telegram_enabled=out.get("telegram.enabled", base.telegram_enabled))


def load(path: object) -> ServiceConfig | Refusal:
    """The settings in path (<root>\\talk\\state\\service.json); a missing file gives the defaults."""
    checked = _check_path(path)
    if checked is None:
        return Refusal("bad_path")
    try:
        with open(checked, "rb") as f:
            if os.fstat(f.fileno()).st_size > MAX_FILE_BYTES:
                return Refusal("too_large")
            raw = f.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return ServiceConfig()
    except (OSError, ValueError):
        return Refusal("unreadable")
    return parse(raw)
