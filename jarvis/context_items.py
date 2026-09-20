"""Context items: what a person explicitly shows the model with ONE request.

A context item is a quoted piece of the conversation (a reference) or a file
the person picked themselves (an attachment). They are organised as CONTEXT
GROUPS: each quote starts a group that owns its own note and its own files,
and files that belong to no quote are the message's global attachments.
Every part is owned by a stable id, never by its position. All of it is DATA
attached to the current turn:

* They carry ZERO authority. Imperative words inside a quote or a file are
  not instructions; only the person's own new message and Core's rules decide
  what may happen. Nothing here can grant a connector, a file, a browser or an
  approval.
* An attachment is the exact item the person selected and nothing else. There
  is no path the model can steer, no directory walk and no filesystem tool:
  the bytes are snapshotted here, at attach time, from a path the person
  chose, and only that snapshot travels.
* Everything is bounded. Text is capped per item and in total, so a large
  quote or file cannot quietly take over the request, and later conversation
  history keeps only a short provenance line - never the content again.

The module is pure Python: no Qt, no provider, no I/O beyond reading the file
the person selected and writing its snapshot.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Any

#: One draft may carry this many items, each this much text, and this much in total.
MAX_ITEMS = 12
ITEM_TEXT_MAX_CHARS = 2_000
ITEMS_TOTAL_MAX_CHARS = 8_000
NOTE_MAX_CHARS = 400
#: What later conversation history keeps of an item: a name, never the content.
HISTORY_ITEM_MAX_CHARS = 60
HISTORY_MAX_ITEMS = 3

#: How the answer is shaped when a draft carries groups: "together" lets the
#: model reason across groups (provenance kept); "separate" answers each group
#: as its own mini-request, with no group's material reaching another.
RESPONSE_MODES = ("together", "separate")
DEFAULT_RESPONSE_MODE = "together"

#: Text-like files JARVIS reads itself, so no provider needs file access.
TEXT_MEDIA_TYPES: dict[str, str] = {
    ".txt": "text/plain", ".md": "text/markdown", ".json": "application/json",
    ".yaml": "text/yaml", ".yml": "text/yaml", ".toml": "text/plain", ".ini": "text/plain",
    ".cfg": "text/plain", ".csv": "text/csv", ".log": "text/plain", ".xml": "text/xml",
    ".html": "text/html", ".css": "text/css", ".py": "text/x-python", ".js": "text/javascript",
    ".ts": "text/plain", ".tsx": "text/plain", ".jsx": "text/plain", ".qml": "text/plain",
    ".c": "text/x-c", ".h": "text/x-c", ".cpp": "text/x-c", ".hpp": "text/x-c", ".cs": "text/plain",
    ".java": "text/x-java", ".go": "text/plain", ".rs": "text/plain", ".rb": "text/plain",
    ".php": "text/plain", ".sh": "text/x-sh", ".ps1": "text/plain", ".bat": "text/plain",
    ".sql": "text/plain", ".ts.map": "text/plain",
}
#: Images the probed provider routes really accept as explicit input.
IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}
#: Formats JARVIS deliberately does NOT claim to read in this build.
UNSUPPORTED_SUFFIXES = frozenset({
    ".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".zip", ".rar",
    ".7z", ".exe", ".dll", ".msi", ".mp3", ".mp4", ".mov", ".avi", ".gif", ".bmp", ".tiff", ".svg",
})

ATTACHMENT_MAX_BYTES = 5_000_000
TEXT_ATTACHMENT_MAX_CHARS = 20_000


class AttachmentRefused(Exception):
    """One attachment could not be taken, with the reason to show the person.

    ``source`` is a message name the interface translates; ``args`` are its
    arguments (a file name, a size). Nothing is attached when this is raised.
    """

    def __init__(self, source: str, *args: Any) -> None:
        super().__init__(source)
        self.source = source
        self.args_tuple = tuple(args)


def _clean(text: Any, limit: int) -> str:
    """Bounded plain text: control characters dropped, paragraph separators normalized."""
    if type(text) is not str:
        return ""
    value = text.replace(" ", "\n").replace(" ", "\n")
    value = "".join(char for char in value if char >= " " or char in "\n\t")
    return value.strip()[:limit]


def file_kind(name: str) -> str:
    """``"text"``, ``"image"`` or ``""`` for a file name JARVIS does not read."""
    suffix = Path(str(name)).suffix.lower()
    if suffix in IMAGE_MEDIA_TYPES:
        return "image"
    if suffix in TEXT_MEDIA_TYPES:
        return "text"
    return ""


def safe_name(name: Any) -> str:
    """The display name of an attachment: its file name, and only that.

    A directory route, a drive letter, a control character or a right-to-left
    override can never reach the interface or the provider through a name.
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = "".join(char for char in text if char.isprintable() and char not in "‪‫‬‭‮")
    text = re.split(r"[\\/]", text)[-1].strip()
    return text[:120] or "file"


def snapshot_directory(root: str | Path) -> Path:
    """The JARVIS-owned folder this session's attachment snapshots live in."""
    folder = Path(root)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def discard_snapshots(root: str | Path) -> None:
    """Remove the snapshot folder and everything JARVIS copied into it."""
    shutil.rmtree(Path(root), ignore_errors=True)


def _store(data: bytes, suffix: str, folder: Path) -> tuple[Path, str]:
    digest = hashlib.sha256(data).hexdigest()
    path = snapshot_directory(folder) / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return path, digest


def attach_file(path: str | Path, folder: str | Path, item_id: str) -> dict[str, Any]:
    """One file the person selected, snapshotted and described. Raises ``AttachmentRefused``.

    Only an ordinary, readable file of a supported kind is taken: a directory,
    a device, a pipe, an unreadable path or a format this build cannot read is
    refused with a reason, and nothing is copied. The bytes are copied at once
    so the draft cannot change under the person between attaching and sending.
    """
    try:
        source = Path(str(path)).expanduser()
        resolved = source.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise AttachmentRefused("%1 could not be read.", safe_name(path)) from exc
    name = safe_name(resolved.name)
    try:
        info = os.stat(resolved)
    except OSError as exc:
        raise AttachmentRefused("%1 could not be read.", name) from exc
    if resolved.is_dir():
        raise AttachmentRefused("%1 is a folder. Choose a file.", name)
    if not os.path.isfile(resolved):
        raise AttachmentRefused("%1 is not an ordinary file.", name)
    kind = file_kind(name)
    if not kind:
        suffix = resolved.suffix.lower() or "?"
        raise AttachmentRefused("JARVIS cannot read %1 files in this build, so %2 was not attached.", suffix, name)
    if info.st_size > ATTACHMENT_MAX_BYTES:
        raise AttachmentRefused("%1 is larger than %2 MB.", name, ATTACHMENT_MAX_BYTES // 1_000_000)
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise AttachmentRefused("%1 could not be read.", name) from exc
    return _describe(data, name, kind, folder, item_id)


def attach_bytes(data: bytes, name: str, folder: str | Path, item_id: str) -> dict[str, Any]:
    """An image the person pasted: the same rules as a selected file."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise AttachmentRefused("The clipboard held no image.")
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise AttachmentRefused("%1 is larger than %2 MB.", safe_name(name), ATTACHMENT_MAX_BYTES // 1_000_000)
    clean = safe_name(name)
    kind = file_kind(clean)
    if not kind:
        raise AttachmentRefused("JARVIS cannot read %1 files in this build, so %2 was not attached.",
                                Path(clean).suffix.lower() or "?", clean)
    return _describe(bytes(data), clean, kind, folder, item_id)


def _describe(data: bytes, name: str, kind: str, folder: str | Path, item_id: str) -> dict[str, Any]:
    suffix = Path(name).suffix.lower()
    path, digest = _store(data, suffix, Path(folder))
    item: dict[str, Any] = {
        "id": str(item_id), "type": "attachment", "kind": kind, "name": name,
        "media_type": (IMAGE_MEDIA_TYPES if kind == "image" else TEXT_MEDIA_TYPES)[suffix],
        "bytes": len(data), "sha256": digest, "path": str(path),
        # Where this attachment lives: the current chat, for now. The field is
        # the seam for later scopes (a project's files, a library) - the record
        # already carries the stable identity those need, so no second store.
        "scope": "chat", "attached_at": int(time.time()),
    }
    if kind == "text":
        text = data.decode("utf-8", errors="replace")
        item["text"] = _clean(text, TEXT_ATTACHMENT_MAX_CHARS)
        item["truncated"] = len(text) > TEXT_ATTACHMENT_MAX_CHARS
    return item


def reference(item_id: str, *, source_role: str, source_id: str, selection: str,
              text: str, source_time: str = "") -> dict[str, Any]:
    """One quoted piece of the conversation, as the interface captured it."""
    return {
        "id": str(item_id), "type": "reference", "kind": "quote",
        "source_role": "assistant" if source_role != "user" else "user",
        "source_id": str(source_id)[:64],
        "source_time": str(source_time)[:32],
        "selection": "partial" if selection == "partial" else "full",
        "text": _clean(text, ITEM_TEXT_MAX_CHARS),
    }


def group(group_id: str, primary: dict[str, Any]) -> dict[str, Any]:
    """A context group: ONE quoted reference, the note for it, and the files
    the person attached to THAT reference. Every part is owned by the group id."""
    return {"id": str(group_id), "reference": primary, "note": "", "attachments": []}


def same_reference(first: Any, second: Any) -> bool:
    """Two references that quote exactly the same thing from the same message."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    keys = ("source_id", "source_role", "selection", "text")
    return all(first.get(key) == second.get(key) for key in keys)


def group_label(index: int) -> str:
    """The letter a group is shown and answered under: A, B, ... Z, AA, AB ..."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


class _Budget:
    """The one text budget a whole turn's context shares."""

    def __init__(self) -> None:
        self.items = 0
        self.chars = 0

    def take(self, entry: dict[str, Any]) -> bool:
        size = sum(len(str(value)) for value in entry.values() if not isinstance(value, (list, dict)))
        if self.items >= MAX_ITEMS or (self.chars + size > ITEMS_TOTAL_MAX_CHARS and self.items):
            return False
        self.items += 1
        self.chars += size
        return True


def _attachment_entry(raw: Any, images: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("id") or raw.get("kind") not in ("text", "image"):
        return None
    entry: dict[str, Any] = {"id": str(raw["id"]), "kind": "file" if raw["kind"] == "text" else "image",
                             "name": safe_name(raw.get("name")), "media_type": str(raw.get("media_type") or "")[:64]}
    if raw["kind"] == "text":
        entry["file_text"] = _clean(raw.get("text"), ITEM_TEXT_MAX_CHARS)
        if raw.get("truncated") or len(_clean(raw.get("text"), TEXT_ATTACHMENT_MAX_CHARS)) > ITEM_TEXT_MAX_CHARS:
            entry["truncated"] = True
    else:
        # Images travel as separate explicit inputs; this number says WHICH one
        # this is, so the model never has to guess an image's owner.
        images.append(entry["id"])
        entry["image_number"] = len(images)
    return entry


def context_window(groups: Any, attachments: Any = ()) -> dict[str, Any]:
    """The bounded, well-typed context of ONE turn, in the person's order.

    ``{"groups": [{label, id, reference, note, attachments}], "global_attachments": [...]}``
    Each group keeps its own reference, note and files; nothing is flattened
    into one list and nothing is re-owned. Text is capped per item and the
    whole turn shares one budget, so what a turn carries is bounded however
    much was attached. JARVIS's local snapshot paths are never part of it.
    """
    budget, images = _Budget(), []
    window: dict[str, Any] = {"groups": [], "global_attachments": []}
    for raw in groups if isinstance(groups, (list, tuple)) else []:
        primary = raw.get("reference") if isinstance(raw, dict) else None
        if not isinstance(primary, dict) or not raw.get("id"):
            continue
        quoted = _clean(primary.get("text"), ITEM_TEXT_MAX_CHARS)
        if not quoted:
            continue
        entry: dict[str, Any] = {
            "label": group_label(len(window["groups"])), "id": str(raw["id"]),
            "reference": {"source_role": "user" if primary.get("source_role") == "user" else "assistant",
                          "selection": "partial" if primary.get("selection") == "partial" else "full",
                          "quoted_text": quoted},
            "attachments": [],
        }
        note = _clean(raw.get("note"), NOTE_MAX_CHARS)
        if note:
            entry["note"] = note
        if not budget.take({"q": quoted, "n": note}):
            break
        for attachment in raw.get("attachments") or []:
            item = _attachment_entry(attachment, images)
            if item is not None and budget.take(item):
                entry["attachments"].append(item)
            elif item is not None and item["kind"] == "image":
                images.pop()
        window["groups"].append(entry)
    for attachment in attachments if isinstance(attachments, (list, tuple)) else []:
        item = _attachment_entry(attachment, images)
        if item is not None and budget.take(item):
            window["global_attachments"].append(item)
        elif item is not None and item["kind"] == "image":
            images.pop()
    return window


def image_files(groups: Any, attachments: Any = ()) -> list[tuple[str, str, str]]:
    """``(id, media_type, snapshot path)`` for every image the turn carries, in
    exactly the order ``context_window`` numbered them (groups first, then the
    message's own attachments). The adapters attach these files and nothing else:
    each path is JARVIS's own snapshot, never a path any model asked for."""
    window = context_window(groups, attachments)
    wanted = [a["id"] for g in window["groups"] for a in g["attachments"] if a["kind"] == "image"]
    wanted += [a["id"] for a in window["global_attachments"] if a["kind"] == "image"]
    paths: dict[str, tuple[str, str, str]] = {}
    for raw in list(attachments or []) + [a for g in (groups or []) if isinstance(g, dict) for a in g.get("attachments") or []]:
        if isinstance(raw, dict) and raw.get("kind") == "image" and type(raw.get("path")) is str:
            paths[str(raw.get("id"))] = (str(raw.get("id")), str(raw.get("media_type") or ""), raw["path"])
    return [paths[item] for item in wanted if item in paths]


def response_mode(value: Any) -> str:
    return value if value in RESPONSE_MODES else DEFAULT_RESPONSE_MODE


def history_text(text: str, groups: Any, attachments: Any = ()) -> str:
    """One past turn as later history should remember it: the words, plus a
    SHORT provenance line per group - never the content again.

    A quote, a note, a file or an image is sent in full exactly once, with the
    turn that carried it. Afterwards a follow-up still knows WHICH quote had
    which files, and nothing is duplicated into later requests.
    """
    window = context_window(groups, attachments)
    lines = []
    for entry in window["groups"][:HISTORY_MAX_ITEMS]:
        parts = ['quote "%s"' % entry["reference"]["quoted_text"][:HISTORY_ITEM_MAX_CHARS]]
        parts += ["%s %s" % (a["kind"], a["name"][:HISTORY_ITEM_MAX_CHARS]) for a in entry["attachments"][:HISTORY_MAX_ITEMS]]
        if len(entry["attachments"]) > HISTORY_MAX_ITEMS:
            parts.append("+%d more" % (len(entry["attachments"]) - HISTORY_MAX_ITEMS))
        lines.append("[Group %s: %s]" % (entry["label"], "; ".join(parts)))
    if len(window["groups"]) > HISTORY_MAX_ITEMS:
        lines.append("[+%d more groups]" % (len(window["groups"]) - HISTORY_MAX_ITEMS))
    extra = window["global_attachments"]
    if extra:
        names = ["%s %s" % (a["kind"], a["name"][:HISTORY_ITEM_MAX_CHARS]) for a in extra[:HISTORY_MAX_ITEMS]]
        if len(extra) > HISTORY_MAX_ITEMS:
            names.append("+%d more" % (len(extra) - HISTORY_MAX_ITEMS))
        lines.append("[attached: %s]" % "; ".join(names))
    return "%s\n%s" % (text, "\n".join(lines)) if lines else str(text)


def result_history(text: str, capability: Any = "", destination: Any = "") -> str:
    """One delivered TASK RESULT as later history should remember it.

    The result's own words, once, with a SHORT marker saying what produced
    them. The words are what a follow-up needs - "what did you find in that
    result?" is answerable only from the result itself - while the marker
    keeps them from being read back as something JARVIS said in its own voice,
    and keeps the capability/destination provenance out of the content.
    """
    what = " - ".join(part for part in (str(capability or ""), str(destination or "")) if part)
    return "[Task result%s]\n%s" % (" - " + what if what else "", text)


__all__ = [
    "ATTACHMENT_MAX_BYTES",
    "AttachmentRefused",
    "DEFAULT_RESPONSE_MODE",
    "IMAGE_MEDIA_TYPES",
    "ITEMS_TOTAL_MAX_CHARS",
    "ITEM_TEXT_MAX_CHARS",
    "MAX_ITEMS",
    "NOTE_MAX_CHARS",
    "RESPONSE_MODES",
    "TEXT_ATTACHMENT_MAX_CHARS",
    "TEXT_MEDIA_TYPES",
    "UNSUPPORTED_SUFFIXES",
    "attach_bytes",
    "attach_file",
    "context_window",
    "discard_snapshots",
    "file_kind",
    "group",
    "group_label",
    "history_text",
    "image_files",
    "reference",
    "result_history",
    "response_mode",
    "safe_name",
    "same_reference",
    "snapshot_directory",
]
