"""Validate and store a conversation attachment without opening its contents as a document."""

import io
import ntpath
import os
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_TEXT_KINDS = {"txt", "md", "csv", "json"}
_OFFICE_PARTS = {"xlsx": "xl/workbook.xml", "docx": "word/document.xml"}
_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"%PDF-": "pdf"}
_ALLOWED_KINDS = _TEXT_KINDS | _OFFICE_PARTS.keys() | set(_MAGIC.values()) | {"webp"}


class AttachmentRefused(ValueError):
    """A refusal with a stable, machine-readable reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AttachmentRecord:
    sha256: str
    bytes: int
    kind: str
    display_name: str
    relative_path: str


def _office_kind(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [info.orig_filename for info in archive.infolist()]
    except (zipfile.BadZipFile, UnicodeError) as exc:
        raise AttachmentRefused("invalid_content") from exc

    matches = [kind for kind, part in _OFFICE_PARTS.items() if part in names]
    if names.count("[Content_Types].xml") != 1 or len(matches) != 1:
        raise AttachmentRefused("invalid_content")
    kind = matches[0]
    if names.count(_OFFICE_PARTS[kind]) != 1:
        raise AttachmentRefused("invalid_content")
    return kind


def _check_content(content: bytes, extension: str) -> None:
    kind = next((kind for magic, kind in _MAGIC.items() if content.startswith(magic)), None)
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        kind = "webp"
    elif content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        kind = _office_kind(content)

    if kind is not None or extension not in _TEXT_KINDS:
        if extension != kind:
            raise AttachmentRefused("kind_mismatch")
        return

    if b"\0" in content:
        raise AttachmentRefused("invalid_content")
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AttachmentRefused("invalid_content") from exc


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _store(root: Path, destination: Path, content: bytes) -> None:
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if (not stat.S_ISREG(existing.st_mode) or _is_reparse_point(existing)
                or existing.st_size != len(content) or destination.read_bytes() != content):
            raise AttachmentRefused("storage_conflict")
        return

    temp_path = root / f".attach-{uuid4().hex}.tmp"
    temp = temp_path.open("xb")
    try:
        with temp:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def intake_attachment(
    root: str | os.PathLike[str],
    filename: str,
    content: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> AttachmentRecord:
    """Accept uploaded bytes into an existing, caller-controlled storage directory."""
    if not content:
        raise AttachmentRefused("empty")
    if len(content) > max_bytes:
        raise AttachmentRefused("too_large")

    name = ntpath.basename(filename)
    stem, suffix = ntpath.splitext(name)
    extension = suffix[1:].lower()
    if extension not in _ALLOWED_KINDS:
        raise AttachmentRefused("unsupported_extension")
    _check_content(content, extension)

    clean_stem = "".join(char for char in stem if unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    display_name = (clean_stem.strip() or "attachment")[:120 - len(suffix)] + suffix

    root = Path(root)
    try:
        info = root.lstat()
    except (OSError, ValueError) as exc:
        raise AttachmentRefused("invalid_root") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
        raise AttachmentRefused("invalid_root")

    digest = sha256(content).hexdigest()
    relative_path = f"{digest}.{extension}"
    try:
        _store(root, root / relative_path, content)
    except OSError as exc:
        raise AttachmentRefused("storage_error") from exc
    return AttachmentRecord(digest, len(content), extension, display_name, relative_path)
