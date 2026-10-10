"""V2 Projects Gate 6H H1: the path safety of a project folder LINK.

A linked folder is CONTEXT, never authority (Salih K2). This module only
decides whether a path the local user picked may be stored as a project's
folder, and proposes where a folder WOULD go. It adds no allowed root, no
working directory and no model-visible field, and it never mutates the
filesystem: no mkdir, no file, no delete, move, rename or attribute change.
Folder creation is G6H1b/G6H3.

Every helper takes the JARVIS-protected roots explicitly (``protected``);
none reads Settings for them. The fixed system locations (Windows, Program
Files, ProgramData, the profile root) are refused by this module itself.
"""

from __future__ import annotations

import ntpath
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from jarvis.contracts import LOCAL_READ_PATH_MAX_CHARS, canonical_path

#: The only origin H1 writes ("created" is reserved for G6H1b).
LINK_ORIGINS = ("picked",)
LINK_ID = re.compile(r"pfl-[0-9a-f]{32}")

DEFAULT_FOLDER_NAME = "Proje"
DEFAULT_PARENT_NAME = "JARVIS Projeleri"
FOLDER_NAME_MAX = 80
COLLISION_MAX = 99

_KNOWN_FOLDERS = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
}
_DRIVE_FIXED = 3
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_FORBIDDEN_CHARS = frozenset('<>:"|?*')
_DRIVE_ABSOLUTE = re.compile(r"[A-Za-z]:\\")
_SHORT_NAME = re.compile(r"~\d")
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{kind}{digit}" for kind in ("com", "lpt") for digit in "0123456789\u00b9\u00b2\u00b3"}
)


@dataclass(frozen=True)
class ProjectFolderLink:
    """One stored link. The path is for the LOCAL USER only."""

    id: str
    project_id: str
    path: str
    origin: str
    linked_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "origin": self.origin, "linked_at": self.linked_at}


def _unsafe_char(char: str) -> bool:
    return unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") or char in _BIDI


def _reserved(component: str) -> bool:
    return component.split(".")[0].rstrip(" ").casefold() in _RESERVED_STEMS


def _key(path: str | Path) -> str:
    return canonical_path(path).rstrip("/")


def _at_or_under(key: str, root: str) -> bool:
    return key == root or key.startswith(root + "/")


def folders_overlap(first: str, second: str) -> bool:
    """The same folder, or one inside the other (canonical keys)."""
    a, b = _key(first), _key(second)
    return _at_or_under(a, b) or _at_or_under(b, a)


def _check_grammar(path: Any) -> list[str]:
    """Rule a: the RAW string, before any normalization. Its components."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("A folder path must be a non-blank string.")
    if len(path) > LOCAL_READ_PATH_MAX_CHARS:
        raise ValueError(f"A folder path is longer than {LOCAL_READ_PATH_MAX_CHARS} characters.")
    if any(_unsafe_char(char) for char in path):
        raise ValueError("A folder path may not contain control or bidi characters.")
    if not _DRIVE_ABSOLUTE.match(path) or "/" in path:
        raise ValueError("A folder path must be a local drive path like C:\\folder.")
    if ntpath.normpath(path) != path:
        raise ValueError("A folder path must already be in normal form.")
    components = [part for part in path[3:].split("\\") if part]
    for component in components:
        if any(char in _FORBIDDEN_CHARS for char in component):
            raise ValueError("A folder name may not contain < > : \" | ? *.")
        if component[-1] in ". ":
            raise ValueError("A folder name may not end with a dot or a space.")
        if _reserved(component):
            raise ValueError("A folder name may not be a Windows device name.")
        if _SHORT_NAME.search(component):
            raise ValueError("A folder path may not use a short (8.3) name.")
    return components


def _system_roots() -> list[str]:
    names = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
    return [_key(os.environ[name]) for name in names if os.environ.get(name)]


def _profile_roots() -> set[str]:
    roots = {_key(Path.home())}
    if os.environ.get("USERPROFILE"):
        roots.add(_key(os.environ["USERPROFILE"]))
    return roots


def _check_location(path: str, components: list[str], protected: Iterable[str | Path]) -> None:
    """Rule b: lexical location, on canonical keys."""
    if not components:
        raise ValueError("A drive root cannot be a project folder.")
    key = _key(path)
    if any(_at_or_under(profile, key) for profile in _profile_roots()):
        raise ValueError("The user profile folder, or a folder containing it, cannot be a project folder.")
    first = components[0].casefold()
    if (
        first in ("windows", "programdata")
        or first.startswith("program files")
        or any(_at_or_under(key, root) for root in _system_roots())
    ):
        raise ValueError("A Windows or program folder cannot be a project folder.")
    if any(_at_or_under(key, _key(root)) for root in protected):
        raise ValueError("A JARVIS data, settings, install or backup folder cannot be a project folder.")


def check_lexical(path: Any, *, protected: Iterable[str | Path]) -> str:
    """Rules a and b only: no filesystem access of any kind."""
    _check_location(path, _check_grammar(path), tuple(protected))
    return path


def _drive_type(anchor: str) -> int:
    import ctypes

    return int(ctypes.WinDLL("kernel32").GetDriveTypeW(ctypes.c_wchar_p(anchor)))


def _final_path(path: str) -> str | None:
    """Open the folder ONCE by name and ask the HANDLE what it is (D4)."""
    from jarvis.connectors.local_files import (
        _final_path_of_handle,
        _windows_close_handle,
        _windows_open_directory,
    )

    handle = _windows_open_directory(Path(path))
    if handle is None:
        return None
    try:
        final = _final_path_of_handle(handle)
    finally:
        _windows_close_handle(handle)
    return None if final is None else str(final)


def validate_folder(path: Any, *, protected: Iterable[str | Path]) -> str:
    """The PROVEN path of an existing local folder the user may link, or a
    ``ValueError`` at the first rule that fails (order a-e)."""
    protected = tuple(protected)
    components = _check_grammar(path)                                  # a
    _check_location(path, components, protected)                       # b
    if os.name != "nt":
        raise ValueError("Project folders are supported on Windows only.")
    try:                                                                # c
        fixed = _drive_type(path[:3]) == _DRIVE_FIXED
    except Exception:  # noqa: BLE001 - an unanswered question refuses
        fixed = False
    if not fixed:
        raise ValueError("A project folder must be on a local fixed drive.")
    current = path[:3]                                                  # d
    for index in range(len(components) + 1):
        if index:
            current = ntpath.join(current, components[index - 1])
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise ValueError("The folder does not exist.") from None
        except OSError:
            raise ValueError("The folder could not be inspected.") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise ValueError("A folder path may not pass through a link or junction.")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("The path is not a folder.")
    proven = _final_path(path)                                          # e
    if proven is None or _key(proven) != _key(path):
        raise ValueError("The folder could not be proven to be the named one.")
    _check_location(proven, _check_grammar(proven), protected)
    return proven


def sanitize_folder_name(name: Any) -> str:
    """A safe folder name from the STORED project name (never model text)."""
    text = unicodedata.normalize("NFC", name if isinstance(name, str) else "")
    text = "".join(char for char in text if not _unsafe_char(char))
    text = re.sub(r'[<>:"/\\|?*]', "-", text)
    text = re.sub(r"~(?=\d)", "-", text)
    text = " ".join(text.split()).rstrip(". ").lstrip()
    text = text[:FOLDER_NAME_MAX].rstrip(". ").lstrip()
    if text and _reserved(text):
        text = "_" + text
    return text or DEFAULT_FOLDER_NAME


def _shell_known_folder(folder_id: str) -> str | None:
    """``SHGetKnownFolderPath`` (flags 0); the buffer is ``CoTaskMemFree``d."""
    import ctypes
    import uuid

    class _Guid(ctypes.Structure):
        _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    guid = _Guid()
    ctypes.memmove(guid.bytes, uuid.UUID(folder_id).bytes_le, 16)
    buffer = ctypes.c_void_p()
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_Guid), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    try:
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buffer)) != 0:
            return None
        return ctypes.wstring_at(buffer.value) if buffer.value else None
    finally:
        if buffer.value:
            ctypes.WinDLL("ole32").CoTaskMemFree(buffer)


def known_folder(kind: str, *, protected: Iterable[str | Path]) -> str:
    """The real Desktop or Documents folder (OneDrive-aware), validated.
    Never a ``Path.home()``, ``USERPROFILE`` or registry guess."""
    if kind not in _KNOWN_FOLDERS:
        raise ValueError(f"Unknown known folder: {kind}")
    try:
        found = _shell_known_folder(_KNOWN_FOLDERS[kind])
    except Exception:  # noqa: BLE001 - any failure refuses, no fallback
        found = None
    if not found:
        raise ValueError("Masaüstü/Belgeler klasörü bulunamadı.")
    return validate_folder(found, protected=protected)


def _absent(path: str) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        raise ValueError("A proposed folder could not be inspected.") from None
    return False


def propose_folder(project_name: Any, on_desktop: bool, *, protected: Iterable[str | Path]) -> str:
    """Where a new folder for this project WOULD go. Reads only; creates
    nothing. Desktop, or ``Documents\\JARVIS Projeleri`` by default."""
    if type(on_desktop) is not bool:
        raise ValueError("on_desktop must be a bool")
    protected = tuple(protected)
    parent = known_folder("desktop" if on_desktop else "documents", protected=protected)
    leaf = sanitize_folder_name(project_name)
    if not on_desktop:
        parent = ntpath.join(parent, DEFAULT_PARENT_NAME)
        if _absent(parent):
            # Nothing beneath a parent that does not exist yet is probed.
            return check_lexical(ntpath.join(parent, leaf), protected=protected)
        parent = validate_folder(parent, protected=protected)
    for number in range(1, COLLISION_MAX + 1):
        name = leaf if number == 1 else f"{leaf} ({number})"
        candidate = check_lexical(ntpath.join(parent, name), protected=protected)
        if _absent(candidate):
            return candidate
    raise ValueError(f"No free folder name up to ({COLLISION_MAX}).")
