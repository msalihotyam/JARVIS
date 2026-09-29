"""Project files (V2 Projects Gate 5): explicit-user text files of a project.

A project file is untrusted DATA. It is read once, from a path the person
chose, under the same rules as a context attachment (``context_items``): an
ordinary readable file of a supported TEXT kind, bounded. Only its display
name, its digest and its normalized text (in chunks) are kept - never the
path and never the original bytes. Excerpts are selected deterministically
per Core task and travel, with provenance, as one attested block; they are
never an instruction and never authority.

Pure Python: no Qt, no provider, no I/O beyond reading the chosen file.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.context_items import (
    ATTACHMENT_MAX_BYTES,
    ITEM_TEXT_MAX_CHARS,
    TEXT_MEDIA_TYPES,
    UNSUPPORTED_SUFFIXES,
    safe_name,
)

#: Orchestrator decisions (Gate 5, 2026-09-29).
PROJECT_FILE_MAX_BYTES = ATTACHMENT_MAX_BYTES
PROJECT_FILE_MAX_CHARS = 200_000
PROJECT_FILE_ACTIVE_MAX = 20
PROJECT_FILE_RETAINED_MAX = 40
PROJECT_FILE_RETAINED_CHARS_MAX = 2_000_000
#: One stored chunk, the attached-text ceiling.
PROJECT_FILE_CHUNK_CHARS = ITEM_TEXT_MAX_CHARS
#: The per-task excerpt budget: separate from the 5-item / 4,000-char memory
#: quota; equal to the local-read excerpt ceiling.
PROJECT_FILE_EXCERPT_MAX_ITEMS = 3
PROJECT_FILE_EXCERPT_MAX_CHARS = 4_000

ACTIVE = "active"
RETIRED = "retired"
PURGED = "purged"
LIFECYCLES = (ACTIVE, RETIRED, PURGED)


class ProjectFileRefused(ValueError):
    """The file was not taken; nothing was stored."""


@dataclass(frozen=True, slots=True)
class ProjectFileContent:
    display_name: str
    sha256: str
    byte_size: int
    text: str


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_project_file(path: Any) -> ProjectFileContent:
    """Read ONE file the person chose, or raise ``ProjectFileRefused``.

    The ``context_items.attach_file`` rules: an ordinary readable file only
    (no directory, device or pipe), a supported TEXT kind, at most 5 MB;
    then valid UTF-8 and at most 200,000 characters after newline
    normalization. Over any cap it is refused, never truncated."""
    try:
        resolved = Path(str(path)).expanduser().resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ProjectFileRefused(f"{safe_name(path)} could not be read.") from exc
    name = safe_name(resolved.name)
    try:
        info = os.stat(resolved)
    except OSError as exc:
        raise ProjectFileRefused(f"{name} could not be read.") from exc
    if resolved.is_dir():
        raise ProjectFileRefused(f"{name} is a folder. Choose a file.")
    if not os.path.isfile(resolved):
        raise ProjectFileRefused(f"{name} is not an ordinary file.")
    suffix = resolved.suffix.lower()
    if suffix in UNSUPPORTED_SUFFIXES or suffix not in TEXT_MEDIA_TYPES:
        raise ProjectFileRefused(f"Only text files can be project files; {suffix or '?'} is not supported.")
    if info.st_size > PROJECT_FILE_MAX_BYTES:
        raise ProjectFileRefused(f"{name} is larger than {PROJECT_FILE_MAX_BYTES // 1_000_000} MB.")
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ProjectFileRefused(f"{name} could not be read.") from exc
    if len(data) > PROJECT_FILE_MAX_BYTES:
        raise ProjectFileRefused(f"{name} is larger than {PROJECT_FILE_MAX_BYTES // 1_000_000} MB.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectFileRefused(f"{name} is not valid UTF-8 text.") from exc
    text = normalize_newlines(text.removeprefix("﻿"))
    if len(text) > PROJECT_FILE_MAX_CHARS:
        raise ProjectFileRefused(f"{name} is longer than {PROJECT_FILE_MAX_CHARS} characters.")
    return ProjectFileContent(name, hashlib.sha256(data).hexdigest(), len(data), text)


def chunk_text(text: str) -> list[str]:
    """Fixed, deterministic chunks of at most ``PROJECT_FILE_CHUNK_CHARS``."""
    size = PROJECT_FILE_CHUNK_CHARS
    return [text[start:start + size] for start in range(0, len(text), size)]


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
