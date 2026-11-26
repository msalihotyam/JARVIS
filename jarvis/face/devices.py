"""FACEDEV1: remote-window device registry and one-time pairing codes.

Pure logic over one state file, <root>/talk/state/devices.json; no network. A device pairs
once with an 8-digit code shown on the local console and gets a 32-byte key back, once.
Only SHA-256 hashes of keys are stored; the live code's hash is kept in memory only.
A missing file means no devices; an unreadable or malformed file refuses every call.
Keys and codes never reach a log, a repr, an exception message or the file.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

CODE_TTL = 300
MAX_ATTEMPTS = 5
MAX_DEVICES = 4  # live (not revoked) devices
MAX_RECORDS = 16  # revoked records kept for list(); pair drops the oldest revoked past this
SEEN_EVERY = 60
MAX_NAME = 40
MAX_FILE_BYTES = 64 * 1024
MAX_TIME = 2**53
LOCK_WAIT = 2.0
LOCK_STALE = 30.0
VERSION = 1
FIELDS = {"id", "name", "key_hash", "created", "last_seen", "revoked"}
_ID = re.compile("[0-9a-f]{16}")
_HASH = re.compile("[0-9a-f]{64}")
_CODE = re.compile("[0-9]{8}")
_KEY = re.compile("[A-Z2-7]{52}")  # base32 of 32 bytes, padding stripped


@dataclass(frozen=True)
class Refusal:
    reason: str


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    created: int
    last_seen: int
    revoked: bool


@dataclass(frozen=True)
class Paired:
    device: Device
    key: str = field(repr=False)


@dataclass
class _Code:
    digest: str = field(repr=False)
    issued: float
    attempts: int = 0


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _now(now) -> float:
    if type(now) not in (int, float) or not 0 <= now < MAX_TIME:  # NaN and inf fail the range
        raise ValueError("clock value out of range")
    return now


def clean_name(name) -> str:
    """Printable label: no control/format/surrogate/private chars, whitespace collapsed."""
    if type(name) is not str:
        return ""
    kept = "".join(" " if ch.isspace() else ch for ch in name[: MAX_NAME * 4]
                   if ch.isspace() or unicodedata.category(ch)[0] != "C")
    # filter before NFC: dropping a format char between a letter and its mark would leave a non-NFC label
    return " ".join(unicodedata.normalize("NFC", kept).split())[:MAX_NAME].strip()


def _reject(_value):
    raise ValueError("not allowed")


def _pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _time_ok(value) -> bool:
    return type(value) is int and 0 <= value < MAX_TIME


def _valid(record) -> bool:
    return (type(record) is dict and set(record) == FIELDS
            and type(record["id"]) is str and _ID.fullmatch(record["id"]) is not None
            and type(record["key_hash"]) is str and _HASH.fullmatch(record["key_hash"]) is not None
            and type(record["name"]) is str and record["name"] != "" and clean_name(record["name"]) == record["name"]
            and _time_ok(record["created"]) and _time_ok(record["last_seen"])
            and type(record["revoked"]) is bool)


def _parse(raw: bytes):
    """The device list, or None when the bytes are not exactly a valid registry."""
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_float=_reject, parse_constant=_reject)
    except (ValueError, RecursionError, UnicodeDecodeError):
        return None
    if type(data) is not dict or set(data) != {"version", "devices"} or type(data["version"]) is not int \
            or data["version"] != VERSION or type(data["devices"]) is not list:
        return None
    records = data["devices"]
    if len(records) > MAX_RECORDS or not all(_valid(r) for r in records):
        return None
    if len({r["id"] for r in records}) != len(records) or len({r["key_hash"] for r in records}) != len(records):
        return None
    if sum(not r["revoked"] for r in records) > MAX_DEVICES:
        return None
    return records


def _device(record) -> Device:
    return Device(record["id"], record["name"], record["created"], record["last_seen"], record["revoked"])


class Registry:
    """Who may open a remote window. The file is re-read on every call, so a revoke elsewhere holds."""

    def __init__(self, root) -> None:
        self.path = Path(root) / "talk" / "state" / "devices.json"
        self._code: _Code | None = None
        self._mutex = threading.Lock()  # one pair/issue at a time, so parallel guesses cannot pass the attempt cap

    def __repr__(self) -> str:
        return f"Registry({str(self.path)!r})"

    @contextlib.contextmanager
    def _locked(self):
        """One writer at a time across processes, so a stale read-modify-write never undoes a revoke."""
        lock = self.path.with_name("devices.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except (FileExistsError, PermissionError):  # Windows reports a lock being deleted as PermissionError
                with contextlib.suppress(OSError):
                    # shortcut: a crashed writer's lock is broken after LOCK_STALE s and two breakers can race;
                    # use an OS file lock if writers ever multiply
                    if time.time() - lock.stat().st_mtime > LOCK_STALE:
                        lock.unlink()
                        continue
                if time.monotonic() > deadline:
                    raise TimeoutError("registry busy") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    def _load(self):
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                return None
            with open(self.path, "rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return []
        except OSError:
            return None
        return None if len(raw) > MAX_FILE_BYTES else _parse(raw)

    def _write(self, records) -> None:
        text = json.dumps({"version": VERSION, "devices": records}, ensure_ascii=True, allow_nan=False, indent=1)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".devices-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def issue_code(self, now) -> str:
        """A fresh 8-digit code; any older code dies. Only its hash stays, in memory."""
        now = _now(now)
        code = f"{secrets.randbelow(10**8):08d}"
        with self._mutex:
            self._code = _Code(_digest(code), now)
        return code

    def pair(self, code, name, now):
        """Paired(device, key) once, or Refusal. The attempt is counted before the comparison."""
        now = _now(now)
        with self._mutex, self._locked():
            return self._pair(code, name, now)

    def _pair(self, code, name, now):
        live = self._code
        if live is None:
            return Refusal("no_code")
        if not live.issued <= now < live.issued + CODE_TTL:  # a clock that went back counts as expired
            self._code = None
            return Refusal("expired")
        label = clean_name(name)
        if not label or clean_name(label) != label:  # never write a name the loader would reject
            return Refusal("bad_name")
        records = self._load()
        if records is None:
            return Refusal("registry_unreadable")
        if sum(not r["revoked"] for r in records) >= MAX_DEVICES:
            return Refusal("too_many_devices")
        live.attempts += 1
        ok = type(code) is str and _CODE.fullmatch(code) is not None \
            and hmac.compare_digest(_digest(code), live.digest)
        if not ok:
            if live.attempts >= MAX_ATTEMPTS:
                self._code = None
            return Refusal("wrong_code")
        self._code = None
        key = base64.b32encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
        stamp = int(now)
        record = {"id": secrets.token_hex(8), "name": label, "key_hash": _digest(key),
                  "created": stamp, "last_seen": stamp, "revoked": False}
        revoked = [r for r in records if r["revoked"]]
        drop = {id(r) for r in sorted(revoked, key=lambda r: r["created"])[:max(0, len(records) + 1 - MAX_RECORDS)]}
        self._write([r for r in records if id(r) not in drop] + [record])
        return Paired(_device(record), key)

    def check(self, key, now):
        """The device id for a valid key, or Refusal. Shape first, then a constant-time hash compare."""
        now = _now(now)
        if type(key) is not str or _KEY.fullmatch(key) is None:
            return Refusal("bad_key")
        records = self._load()
        if records is None:
            return Refusal("registry_unreadable")
        digest = _digest(key)
        found = None
        for record in records:  # no early exit: every hash is compared
            if hmac.compare_digest(record["key_hash"], digest):
                found = record
        if found is None:
            return Refusal("unknown_key")
        if found["revoked"]:
            return Refusal("revoked")
        if now - found["last_seen"] >= SEEN_EVERY:
            try:
                with self._locked():  # re-read under the lock: only last_seen changes, a fresh revoke stays
                    fresh = self._load() or []
                    for record in fresh:
                        if record["id"] == found["id"] and not record["revoked"]:
                            record["last_seen"] = int(now)
                            self._write(fresh)
            except OSError:
                pass  # last_seen is bookkeeping; a failed write must not lock a valid device out
        return found["id"]

    def revoke(self, device_id):
        with self._locked():
            records = self._load()
            if records is None:
                return Refusal("registry_unreadable")
            for record in records:
                if type(device_id) is str and record["id"] == device_id:
                    record["revoked"] = True
                    self._write(records)
                    return _device(record)
            return Refusal("unknown_device")

    def list(self):
        records = self._load()
        return Refusal("registry_unreadable") if records is None else tuple(_device(r) for r in records)


def main(argv=None, clock=time.time) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.face.devices")
    parser.add_argument("--root", required=True, help="folder that holds talk/state/devices.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("code", help="show a one-time pairing code on this console")
    sub.add_parser("list")
    sub.add_parser("revoke").add_argument("id")
    args = parser.parse_args(argv)
    registry = Registry(args.root)
    if args.command == "code":
        if not sys.stdout.isatty():  # the code goes to the local console only, never a pipe or a log file
            print("refused: the code is shown only on an interactive console", file=sys.stderr)
            return 2
        print(registry.issue_code(clock()))
        return 0
    result = registry.list() if args.command == "list" else registry.revoke(args.id)
    if isinstance(result, Refusal):
        print(f"refused: {result.reason}", file=sys.stderr)
        return 1
    for device in (result,) if isinstance(result, Device) else result:
        print(f"{device.id}  {device.name}  created={device.created}  last_seen={device.last_seen}"
              f"  {'revoked' if device.revoked else 'active'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
