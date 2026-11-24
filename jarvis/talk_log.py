"""Append-only local conversation log shared by the PC and Telegram views."""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import re

if os.name == "nt":
    import msvcrt
else:
    import fcntl


MAX_RECORD_BYTES = 64 * 1024
_VIEWS = ("pc", "telegram")
_STATES = ("pending", "delivered", "failed")
_ENTRY_FIELDS = {
    "id", "ts", "source", "who", "kind", "text", "attachments",
    "focus", "reply_to", "delivery",
}
_KINDS = ("text", "voice", "image", "file", "transcript", "approval_card")
_GENESIS = "0" * 64


class TalkLogError(ValueError):
    """A record is invalid or the log cannot be safely extended."""


def _encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _seal(record, previous):
    digest = sha256(previous.encode("ascii") + _encode(record)).hexdigest()
    return _encode({"hash": digest, "record": record}) + b"\n", digest


@contextmanager
def _locked(file):
    # Windows permits a byte-range lock past EOF, including on an empty log.
    if os.name == "nt":
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(file, fcntl.LOCK_EX)
    try:
        yield
    finally:
        if os.name == "nt":
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(file, fcntl.LOCK_UN)


class TalkLog:
    """Replay committed records from an injected local path.

    Entry IDs increase in file order, sharing a sequence with delivery records.
    An unterminated final line is ignored by readers and blocks further writes.
    """

    def __init__(self, path, attachments_root, *, clock=None):
        path = Path(path)
        self._validate_log_path(path)
        self.path = path.resolve()
        self._validate_log_path(self.path)
        self.attachments_root = Path(attachments_root).resolve()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    @staticmethod
    def _validate_log_path(path):
        if ":" in path.name or path.name.casefold().rstrip(" .") == "jarvis.db":
            raise TalkLogError("Database and alternate stream paths are invalid for a talk log")

    def read(self):
        """Return entries in order with their latest per-view delivery states."""
        try:
            file = self.path.open("rb")
        except FileNotFoundError:
            return []
        with file, _locked(file):
            entries, _, _, _ = self._replay(file)
        return list(entries.values())

    def append(
        self, *, source, who, kind, text, attachments=(), focus=None,
        reply_to=None, id=None,
    ):
        """Append an entry and return its stored fields."""
        now = self._clock()
        if now.utcoffset() is None:
            raise TalkLogError("The clock must return a timezone-aware datetime")
        entry = {
            "id": id,
            "ts": now.astimezone(timezone.utc).isoformat(),
            "source": source,
            "who": who,
            "kind": kind,
            "text": text,
            "attachments": list(attachments),
            "focus": focus,
            "reply_to": reply_to,
            "delivery": dict.fromkeys(_VIEWS, "pending"),
        }
        self._append_record({"entry": entry})
        return entry

    def set_delivery(self, entry_id, view, state):
        """Append a delivery update for one view of an existing entry."""
        self._append_record({"delivery": {"entry_id": entry_id, "view": view, "state": state}})

    def _append_record(self, record):
        # Append mode creates the file without truncation and directs writes
        # to EOF. Keep the lock through the flush and sync.
        with self.path.open("a+b") as file, _locked(file):
            file.seek(0)
            entries, last_id, previous, torn = self._replay(file)
            if torn:
                raise TalkLogError("Cannot append after an unterminated record")
            record["id"] = last_id + 1
            entry = record.get("entry")
            if entry is not None and entry["id"] is None:
                entry["id"] = record["id"]
            try:
                line, _ = _seal(record, previous)
            except (TypeError, ValueError, RecursionError) as exc:
                raise TalkLogError("Record is not JSON-serializable") from exc
            if len(line) > MAX_RECORD_BYTES:
                raise TalkLogError("Record exceeds byte limit")
            self._apply(record, entries, last_id + 1)
            if file.write(line) != len(line):
                raise OSError("Incomplete talk log write")
            file.flush()
            os.fsync(file.fileno())

    def _replay(self, file):
        entries = {}
        last_id = 0
        previous = _GENESIS
        while line := file.readline(MAX_RECORD_BYTES + 1):
            if len(line) > MAX_RECORD_BYTES:
                raise TalkLogError(f"Record {last_id + 1} exceeds byte limit")
            if not line.endswith(b"\n"):
                return entries, last_id, previous, True
            try:
                envelope = json.loads(line)
                record = envelope["record"]
                expected_line, digest = _seal(record, previous)
            except (KeyError, TypeError, ValueError, RecursionError) as exc:
                raise TalkLogError(f"Invalid record {last_id + 1}") from exc
            if line != expected_line:
                raise TalkLogError(f"Hash or encoding mismatch at record {last_id + 1}")
            last_id += 1
            self._apply(record, entries, last_id)
            previous = digest
        return entries, last_id, previous, False

    def _apply(self, record, entries, expected_id):
        if (
            not isinstance(record, dict)
            or type(record.get("id")) is not int
            or record["id"] != expected_id
        ):
            raise TalkLogError("Record IDs must follow file order")
        if record.keys() == {"id", "entry"}:
            entry = record["entry"]
            self._validate_entry(entry, entries, expected_id)
            entries[expected_id] = entry
            return
        if record.keys() != {"id", "delivery"}:
            raise TalkLogError("Unknown record fields")
        delivery = record["delivery"]
        if not isinstance(delivery, dict) or delivery.keys() != {"entry_id", "view", "state"}:
            raise TalkLogError("Invalid delivery fields")
        entry_id, view, state = delivery["entry_id"], delivery["view"], delivery["state"]
        if type(entry_id) is not int or entry_id not in entries:
            raise TalkLogError("Delivery must refer to an existing entry")
        if view not in _VIEWS or state not in _STATES:
            raise TalkLogError("Invalid delivery view or state")
        entries[entry_id]["delivery"][view] = state

    def _validate_entry(self, entry, entries, expected_id):
        if not isinstance(entry, dict) or entry.keys() != _ENTRY_FIELDS:
            raise TalkLogError("Unknown or missing entry fields")
        if type(entry["id"]) is not int or entry["id"] != expected_id:
            raise TalkLogError("Entry ID must match the next record ID")
        if entry["source"] not in _VIEWS or entry["who"] not in ("salih", "jarvis", "system"):
            raise TalkLogError("Invalid source or author")
        if entry["kind"] not in _KINDS or not isinstance(entry["text"], str):
            raise TalkLogError("Invalid entry kind or text")
        try:
            timestamp = datetime.fromisoformat(entry["ts"])
        except (TypeError, ValueError) as exc:
            raise TalkLogError("Invalid timestamp") from exc
        if timestamp.utcoffset() is None:
            raise TalkLogError("Timestamp must include a timezone")
        if entry["focus"] is not None and not isinstance(entry["focus"], str):
            raise TalkLogError("Focus must be a string or null")
        reply = entry["reply_to"]
        if reply is not None and (type(reply) is not int or reply not in entries):
            raise TalkLogError("Reply must refer to an earlier entry")
        if entry["delivery"] != dict.fromkeys(_VIEWS, "pending"):
            raise TalkLogError("New entries must have pending delivery to both views")
        if not isinstance(entry["attachments"], list):
            raise TalkLogError("Attachments must be a list")
        for attachment in entry["attachments"]:
            self._validate_attachment(attachment)

    def _validate_attachment(self, attachment):
        if not isinstance(attachment, dict) or attachment.keys() != {"sha256", "path"}:
            raise TalkLogError("Invalid attachment fields")
        digest, path = attachment["sha256"], attachment["path"]
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-fA-F]{64}", digest):
            raise TalkLogError("Invalid attachment SHA-256")
        if (
            not isinstance(path, str)
            or any(char in path for char in ("\\", ":", "\0"))
            or any(not part or part.endswith((".", " ")) for part in path.split("/"))
            or PureWindowsPath(path).anchor
        ):
            raise TalkLogError("Attachment path must be relative to its root")
        try:
            target = (self.attachments_root / path).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise TalkLogError("Cannot resolve attachment path") from exc
        if target == self.attachments_root or not target.is_relative_to(self.attachments_root):
            raise TalkLogError("Attachment path escapes its root")
