"""Untrusted history export parser for V2.1b import (pure, no side effects).

``parse_export`` turns a ChatGPT ``conversations.json``, a Claude export JSON or
a plain Markdown/text transcript into an ``ImportBatch`` of ``ImportedItem``s.
Every field is DATA from an untrusted source: nothing here interprets, executes
or routes on the text, and the role is clamped to a fixed vocabulary. The
pipeline after this (dedup -> scope -> extract -> approval -> consolidation)
lives elsewhere; this module never writes anything.

Fail-closed: protected paths, links, over-limit input and malformed or unknown
formats raise a typed ``ImportRefused`` subclass; nothing is truncated or
partially returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Same root as jarvis.settings.PROJECT_ROOT (not imported: that module pulls in
# the connectors and this one stays pure).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ITEMS = 50_000
MAX_ITEM_CHARS = 100_000

FORMAT_CHATGPT = "chatgpt"
FORMAT_CLAUDE = "claude"
FORMAT_TEXT = "text"
FORMAT_HINTS = ("auto", FORMAT_CHATGPT, FORMAT_CLAUDE, FORMAT_TEXT)

ROLES = ("user", "assistant", "system", "tool", "unknown")
_ROLE_ALIASES = {
    "user": "user", "human": "user", "you": "user",
    "assistant": "assistant", "ai": "assistant", "claude": "assistant",
    "chatgpt": "assistant", "gpt": "assistant",
    "system": "system", "tool": "tool",
}
_SECRET_SUFFIXES = (
    ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".pem", ".key",
    ".crt", ".cer", ".p12", ".pfx", ".kdbx",
)
# Possessive quantifiers: no backtracking, so a long run of spaces stays linear.
_TRANSCRIPT_MARKER = re.compile(
    r"^\s*+(?:#{1,6}\s++)?\**+\s*+(user|human|you|assistant|ai|claude|chatgpt|gpt|system|tool)"
    r"\s*+\**+\s*+(?::\**+\s*+(.*)|\s*$)",
    re.IGNORECASE,
)


class ImportRefused(ValueError):
    """Base of every refusal; callers catch this one."""


class ProtectedPathError(ImportRefused):
    """The path is protected (operator data, secrets) or is a link."""


class ImportTooLargeError(ImportRefused):
    """File size, item count or one item's text is over the hard limit."""


class MalformedExportError(ImportRefused):
    """Not decodable / not the structure its format requires."""


class UnknownFormatError(ImportRefused):
    """The format hint is unknown or the content matches no known format."""


@dataclass(frozen=True, slots=True)
class ImportedItem:
    source_format: str
    source_conversation_title: str
    role: str
    text: str
    created_at: str | None
    content_hash: str


@dataclass(frozen=True, slots=True)
class ImportBatch:
    items: tuple[ImportedItem, ...]
    warnings: tuple[str, ...]


def parse_export(
    path_or_bytes: str | os.PathLike[str] | bytes,
    format_hint: str = "auto",
    *,
    max_bytes: int = MAX_FILE_BYTES,
    max_items: int = MAX_ITEMS,
    max_item_chars: int = MAX_ITEM_CHARS,
    protected_roots: Iterable[str | os.PathLike[str]] = (),
) -> ImportBatch:
    """Parse one export. Limits can only be tightened, never raised;
    ``protected_roots`` adds to the built-in protected set."""
    if format_hint not in FORMAT_HINTS:
        raise UnknownFormatError(f"unknown format hint {format_hint!r}")
    limits = _Limits(
        min(max_bytes, MAX_FILE_BYTES),
        min(max_items, MAX_ITEMS),
        min(max_item_chars, MAX_ITEM_CHARS),
    )
    default_title = ""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        raw = bytes(path_or_bytes)
        if len(raw) > limits.max_bytes:
            raise ImportTooLargeError(f"input is over {limits.max_bytes} bytes")
    elif isinstance(path_or_bytes, (str, os.PathLike)):
        raw = _read_path(Path(path_or_bytes), limits.max_bytes, protected_roots)
        default_title = Path(path_or_bytes).stem
    else:
        raise TypeError("parse_export takes a path or bytes")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MalformedExportError("export is not valid UTF-8") from exc

    builder = _Builder(limits)
    if format_hint == FORMAT_TEXT or (
        format_hint == "auto" and not text.lstrip().startswith(("[", "{"))
    ):
        _parse_transcript(text, default_title, builder)
    else:
        try:
            document = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise MalformedExportError("export is not valid JSON") from exc
        conversations = [document] if isinstance(document, dict) else document
        if not isinstance(conversations, list):
            raise MalformedExportError("export JSON is not a list of conversations")
        detected = format_hint if format_hint != "auto" else _detect(conversations)
        if detected == FORMAT_CHATGPT:
            _parse_chatgpt(conversations, builder)
        else:
            _parse_claude(conversations, builder)
    return ImportBatch(tuple(builder.items), tuple(builder.warnings))


def content_hash(text: str) -> str:
    """sha256 of the normalized text (NFC, whitespace collapsed)."""
    normalized = " ".join(unicodedata.normalize("NFC", text).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_protected_path(
    path: str | os.PathLike[str], extra_roots: Iterable[str | os.PathLike[str]] = ()
) -> bool:
    candidate = Path(os.path.abspath(path))
    # \\?\ and UNC spellings dodge the root comparison (and a share is a network read).
    if str(candidate).startswith(("\\\\", "//")):
        return True
    if any(part.lower().startswith(".env") for part in candidate.parts):
        return True
    if candidate.name.lower().endswith(_SECRET_SUFFIXES):
        return True
    roots = [_PROJECT_ROOT / "data", Path.home() / ".jarvis", *map(Path, extra_roots)]
    return any(
        _within(candidate, Path(os.path.abspath(root))) or _within(candidate, Path(root).resolve())
        for root in roots
    )


def _within(candidate: Path, root: Path) -> bool:
    child = os.path.normcase(str(candidate))
    parent = os.path.normcase(str(root))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives
        return False


def _read_path(
    path: Path, max_bytes: int, extra_roots: Iterable[str | os.PathLike[str]]
) -> bytes:
    extra_roots = tuple(extra_roots)
    # Checked on the path as given and on its resolved target, before any read,
    # so a link or a junction cannot reach a protected file.
    if is_protected_path(path, extra_roots):
        raise ProtectedPathError("refusing to read a protected path")
    if path.is_symlink() or path.is_junction():
        raise ProtectedPathError("refusing to follow a link")
    resolved = path.resolve()
    if is_protected_path(resolved, extra_roots):
        raise ProtectedPathError("refusing to read a protected path")
    if not resolved.is_file():
        raise MalformedExportError("export path is not a regular file")
    if resolved.stat().st_size > max_bytes:
        raise ImportTooLargeError(f"file is over {max_bytes} bytes")
    with open(resolved, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ImportTooLargeError(f"file is over {max_bytes} bytes")
    return raw


@dataclass(frozen=True, slots=True)
class _Limits:
    max_bytes: int
    max_items: int
    max_item_chars: int


class _Builder:
    def __init__(self, limits: _Limits) -> None:
        self.limits = limits
        self.items: list[ImportedItem] = []
        self.warnings: list[str] = []

    def add(self, fmt: str, title: str, role: str, text: str, created_at: str | None) -> None:
        text = unicodedata.normalize("NFC", text.replace("\r\n", "\n")).strip()
        if not text:
            return
        if len(text) > self.limits.max_item_chars or len(title) > self.limits.max_item_chars:
            raise ImportTooLargeError(
                f"item {len(self.items) + 1} is over {self.limits.max_item_chars} characters"
            )
        try:  # a lone surrogate from a JSON escape cannot be hashed or stored
            text.encode("utf-8")
            title.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise MalformedExportError(f"item {len(self.items) + 1} is not valid Unicode") from exc
        if len(self.items) >= self.limits.max_items:
            raise ImportTooLargeError(f"export has more than {self.limits.max_items} items")
        self.items.append(
            ImportedItem(fmt, title, role, text, created_at, content_hash(text))
        )


def _role(value: object) -> str:
    if isinstance(value, str):
        return _ROLE_ALIASES.get(value.strip().lower(), "unknown")
    return "unknown"


def _title(value: object) -> str:
    return unicodedata.normalize("NFC", value).strip() if isinstance(value, str) else ""


def _timestamp(value: object) -> str | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            moment = datetime.fromtimestamp(value, tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            moment = datetime.fromisoformat(value.strip())
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        else:
            return None
        return moment.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _detect(conversations: list) -> str:
    for conversation in conversations:
        if isinstance(conversation, dict):
            if "mapping" in conversation:
                return FORMAT_CHATGPT
            if "chat_messages" in conversation:
                return FORMAT_CLAUDE
    raise UnknownFormatError("JSON matches neither the ChatGPT nor the Claude export")


def _conversation(conversation: object, index: int, key: str, kind: type) -> object:
    if not isinstance(conversation, dict) or not isinstance(conversation.get(key), kind):
        raise MalformedExportError(f"conversation {index}: missing or invalid {key!r}")
    return conversation[key]


def _parse_chatgpt(conversations: list, builder: _Builder) -> None:
    for index, conversation in enumerate(conversations, 1):
        mapping = _conversation(conversation, index, "mapping", dict)
        title = _title(conversation.get("title"))
        for node in _chatgpt_path(mapping, conversation.get("current_node")):
            message = node.get("message") if isinstance(node, dict) else None
            if not isinstance(message, dict):
                continue
            author = message.get("author")
            content = message.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            texts = [part for part in parts if isinstance(part, str)]
            if len(texts) != len(parts):
                builder.warnings.append(f"conversation {index}: skipped non-text content")
            builder.add(
                FORMAT_CHATGPT, title,
                _role(author.get("role") if isinstance(author, dict) else None),
                "\n".join(texts), _timestamp(message.get("create_time")),
            )


def _chatgpt_path(mapping: dict, current: object) -> list:
    """The active branch (current_node up to the root), oldest first; without
    a usable current_node every node, by message time."""
    if isinstance(current, str) and current in mapping:
        path, seen = [], set()
        while isinstance(current, str) and current in mapping and current not in seen:
            seen.add(current)
            path.append(mapping[current])
            node = mapping[current]
            current = node.get("parent") if isinstance(node, dict) else None
        return path[::-1]

    def when(node: object) -> float:
        message = node.get("message") if isinstance(node, dict) else None
        stamp = message.get("create_time") if isinstance(message, dict) else None
        return stamp if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else 0.0

    return sorted(mapping.values(), key=when)


def _parse_claude(conversations: list, builder: _Builder) -> None:
    for index, conversation in enumerate(conversations, 1):
        messages = _conversation(conversation, index, "chat_messages", list)
        title = _title(conversation.get("name"))
        for message in messages:
            if not isinstance(message, dict):
                builder.warnings.append(f"conversation {index}: skipped a non-object message")
                continue
            text = message.get("text")
            if not isinstance(text, str) or not text.strip():
                blocks = message.get("content")
                text = "\n".join(
                    block["text"] for block in blocks if isinstance(block, dict)
                    and block.get("type") == "text" and isinstance(block.get("text"), str)
                ) if isinstance(blocks, list) else ""
            builder.add(
                FORMAT_CLAUDE, title, _role(message.get("sender")), text,
                _timestamp(message.get("created_at")),
            )


def _parse_transcript(text: str, title: str, builder: _Builder) -> None:
    """``User: ...`` / ``**Assistant:**`` / ``## User`` start a turn; text
    with no marker at all is one ``unknown`` item."""
    role, lines = "unknown", []
    for line in text.splitlines():
        marker = _TRANSCRIPT_MARKER.match(line)
        if marker:
            builder.add(FORMAT_TEXT, title, role, "\n".join(lines), None)
            role, lines = _role(marker.group(1)), [marker.group(2) or ""]
        else:
            lines.append(line)
    builder.add(FORMAT_TEXT, title, role, "\n".join(lines), None)
