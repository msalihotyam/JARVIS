"""V2 Projects Gate 6H H1/H1b: the path safety of a project folder LINK.

A linked folder is CONTEXT, never authority (Salih K2). This module decides
whether a path the local user picked may be stored as a project's folder,
proposes where a folder WOULD go and (H1b, K1) creates exactly that folder
at the user's click. It adds no allowed root, no working directory and no
model-visible field. The one filesystem write is ``create_folder``: a new
directory (and, once, ``Documents\\JARVIS Projeleri``) created RELATIVE to a
held, proven parent handle. Nothing here deletes, moves, renames or changes
the attributes of anything, also not after a failure (D4).

D3: every directory is judged through a HELD handle opened as itself (a
reparse point is not followed): its attributes and its final path come from
the handle, and a child is only ever looked up or created relative to it.
The held handles share READ | WRITE but never DELETE, so a held directory
cannot be renamed or deleted while JARVIS holds it. Residual (Salih
2026-10-10, accepted): after the last judgement a folder can still be moved
or turned into an online-only OneDrive placeholder, the same as after the
user links an existing folder; a later UI shows such a folder as missing.

Every helper takes the JARVIS-protected roots explicitly (``protected``);
none reads Settings for them. The fixed system locations (Windows, Program
Files, ProgramData, the profile root) are refused by this module itself.
"""

from __future__ import annotations

import contextlib
import ntpath
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from jarvis.contracts import LOCAL_READ_PATH_MAX_CHARS, canonical_path

#: The user's own pick (H1) and a folder JARVIS created at the user's click
#: on its exact shown path (H1b, Salih Q5).
LINK_ORIGINS = ("picked", "created")
LINK_STATES = ("linked", "not linked", "unknown")
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
_DIRECTORY = 0x10
# D3 handles. Held: list/traverse (a child is resolved beneath it), read
# attributes (the attribute query), synchronize; shared READ | WRITE only.
_HELD_ACCESS = 0x0001 | 0x0020 | 0x0080 | 0x00100000
_HELD_SHARE = 0x00000001 | 0x00000002
_PROBE_ACCESS = 0x0080 | 0x00100000              # FILE_READ_ATTRIBUTES | SYNCHRONIZE
_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_OPEN_AS_ITSELF = 0x02000000 | 0x00200000        # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_TAG_INFO = 9
_UNVERIFIED = "amaçlanan yol; konumu doğrulanamadı"
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


@dataclass
class FolderCreation:
    """``create_folder``'s context: the approved path, the held handles as
    ``(handle, expected path)`` and, once the context has exited, ``created``:
    each directory JARVIS created, at its handle-observed location."""

    path: str
    handles: tuple[tuple[int, str], ...] = ()
    created: tuple[str, ...] = ()


class FolderCreateError(Exception):
    """A failure AFTER JARVIS created at least one directory (D4). Nothing is
    removed: ``created`` names each directory as its own handle located it
    (or its intended path, labelled as unverified). ``link_state`` is read
    back by ``create_and_link`` ("linked" / "not linked" / "unknown")."""

    def __init__(self, created: Iterable[str], link_state: str | None = None) -> None:
        self.created, self.link_state = tuple(created), link_state
        super().__init__(f"{len(self.created)} folder(s) created and kept; link state: {link_state}")


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


def same_folder(first: str, second: str) -> bool:
    """The same folder (canonical keys)."""
    return _key(first) == _key(second)


def _check_component(component: str) -> None:
    """One path component: the component half of rule a."""
    if any(char in _FORBIDDEN_CHARS for char in component):
        raise ValueError("A folder name may not contain < > : \" | ? *.")
    if component[-1] in ". ":
        raise ValueError("A folder name may not end with a dot or a space.")
    if _reserved(component):
        raise ValueError("A folder name may not be a Windows device name.")
    if _SHORT_NAME.search(component):
        raise ValueError("A folder path may not use a short (8.3) name.")


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
        _check_component(component)
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


def _close(handle: int) -> None:
    from jarvis.connectors.local_files import _windows_close_handle

    _windows_close_handle(handle)


def _open_held(path: str) -> int | None:
    """D3: the directory at ``path`` opened by name AS ITSELF (its own reparse
    point is not followed; one in an ancestor shows in the final path)."""
    import ctypes

    from jarvis.connectors.local_files import _WINDOWS_NATIVE

    handle = _WINDOWS_NATIVE["open_directory"](
        path, _HELD_ACCESS, _HELD_SHARE, None, _OPEN_EXISTING, _OPEN_AS_ITSELF, None
    )
    return None if handle in (ctypes.c_void_p(-1).value, 0, None) else handle


def _nt_open(parent: int, leaf: str, access: int, share: int, disposition: int, options: int) -> tuple[int, int | None]:
    """``NtCreateFile`` of the bare ``leaf`` RELATIVE to the held ``parent``
    (``RootDirectory``): no pathname is walked. ``(status, handle or None)``."""
    import ctypes

    from jarvis.connectors.local_files import _FILE_ATTRIBUTE_NORMAL, _OBJ_CASE_INSENSITIVE, _WINDOWS_NATIVE

    api = _WINDOWS_NATIVE
    size = len(leaf.encode("utf-16-le"))                   # UTF-16 code units, not characters
    name = api["UnicodeString"](size, size, leaf)
    attributes = api["ObjectAttributes"](
        ctypes.sizeof(api["ObjectAttributes"]), parent, ctypes.pointer(name), _OBJ_CASE_INSENSITIVE, None, None
    )
    handle = ctypes.c_void_p()
    status = api["create_file"](
        ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(api["IoStatusBlock"]()),
        None, _FILE_ATTRIBUTE_NORMAL, share, disposition, options, None, 0,
    ) & 0xFFFFFFFF
    return status, (handle.value if status == 0 and handle.value else None)


def _attribute_tag(handle: int) -> tuple[int, int] | None:
    """``(attributes, reparse tag)`` of the object behind ``handle``, or None."""
    import ctypes

    class _Info(ctypes.Structure):
        _fields_ = [("FileAttributes", ctypes.c_uint32), ("ReparseTag", ctypes.c_uint32)]

    try:
        query = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
        query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        query.restype = ctypes.c_int
        info = _Info()
        if not query(ctypes.c_void_p(handle), _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)):
            return None
    except Exception:  # noqa: BLE001 - an unanswered question refuses
        return None
    return info.FileAttributes, info.ReparseTag


def _final_path(handle: int) -> str | None:
    """Where the HANDLE's object is, every reparse point resolved (or None)."""
    from jarvis.connectors.local_files import _final_path_of_handle

    final = _final_path_of_handle(handle)
    return None if final is None else str(final)


def _judge(handle: int, expected: str) -> str:
    """D3: what the held handle IS - a plain directory (no reparse attribute,
    no tag) whose final path is ``expected`` - never what a name points at
    now. Returns the proven path."""
    found = _attribute_tag(handle)
    if found is None:
        raise ValueError("The folder could not be inspected.")
    attributes, tag = found
    if attributes & _REPARSE_POINT or tag:
        raise ValueError("A folder path may not pass through a link or junction.")
    if not attributes & _DIRECTORY:
        raise ValueError("The path is not a folder.")
    final = _final_path(handle)
    if final is None or not same_folder(final, expected):
        raise ValueError("The folder could not be proven to be the named one.")
    return final


def _hold_folder(path: Any, protected: tuple) -> tuple[int, str]:
    """Rules a-e on an existing folder; the HELD handle and the proven path.
    The caller closes the handle."""
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
    handle = _open_held(path)                                           # e
    if handle is None:
        raise ValueError("The folder could not be proven to be the named one.")
    try:
        proven = _judge(handle, path)
        _check_location(proven, _check_grammar(proven), protected)
    except BaseException:
        _close(handle)
        raise
    return handle, proven


def validate_folder(path: Any, *, protected: Iterable[str | Path]) -> str:
    """The PROVEN path of an existing local folder the user may link, or a
    ``ValueError`` at the first rule that fails (order a-e)."""
    handle, proven = _hold_folder(path, tuple(protected))
    _close(handle)
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


def _known_path(kind: str) -> str:
    """The shell's answer for the real Desktop or Documents folder (no
    filesystem access). Never a ``Path.home()``, ``USERPROFILE`` or registry
    guess; any failure refuses."""
    if kind not in _KNOWN_FOLDERS:
        raise ValueError(f"Unknown known folder: {kind}")
    try:
        found = _shell_known_folder(_KNOWN_FOLDERS[kind])
    except Exception:  # noqa: BLE001 - any failure refuses, no fallback
        found = None
    if not found:
        raise ValueError("Masaüstü/Belgeler klasörü bulunamadı.")
    return found


def known_folder(kind: str, *, protected: Iterable[str | Path]) -> str:
    """The real Desktop or Documents folder (OneDrive-aware), validated."""
    return validate_folder(_known_path(kind), protected=protected)


def _open_child(parent: int, leaf: str) -> int | None:
    """D3: ``leaf`` looked up RELATIVE to the held ``parent`` and opened as
    itself (no-follow), held; ``None`` when it does not exist."""
    from jarvis.connectors.local_files import _FILE_OPEN, _STATUS_OBJECT_NAME_NOT_FOUND

    status, handle = _nt_open(parent, leaf, _HELD_ACCESS, _HELD_SHARE, _FILE_OPEN,
                              _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT)
    if handle is not None:
        return handle
    if status == _STATUS_OBJECT_NAME_NOT_FOUND:
        return None
    raise ValueError("The folder could not be inspected.")


def _taken(parent: int, leaf: str) -> bool:
    """Objective 1: is ``leaf`` beneath the held ``parent`` taken? A no-follow
    relative open: not found = free, opened (anything) = taken."""
    from jarvis.connectors.local_files import _FILE_OPEN, _STATUS_OBJECT_NAME_NOT_FOUND

    status, handle = _nt_open(parent, leaf, _PROBE_ACCESS, _SHARE_ALL, _FILE_OPEN,
                              _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT)
    if handle is not None:
        _close(handle)
        return True
    if status == _STATUS_OBJECT_NAME_NOT_FOUND:
        return False
    raise ValueError("A proposed folder could not be inspected.")


def _create_child(parent: int, leaf: str) -> int:
    """``FILE_CREATE`` a directory ``leaf`` beneath the held ``parent``: the
    create itself is the absence proof. The new directory's handle, held."""
    from jarvis.connectors.local_files import _FILE_CREATE, _STATUS_OBJECT_NAME_COLLISION

    status, handle = _nt_open(parent, leaf, _HELD_ACCESS, _HELD_SHARE, _FILE_CREATE,
                              _FILE_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT)
    if status == _STATUS_OBJECT_NAME_COLLISION:
        raise ValueError("A folder or file with that name already exists; nothing is reused.")
    if handle is None:
        raise ValueError(f"The folder could not be created (0x{status:08X}).")
    return handle


def propose_folder(project_name: Any, on_desktop: bool, *, protected: Iterable[str | Path]) -> str:
    """Where a new folder for this project WOULD go. Reads only; creates
    nothing. Desktop, or ``Documents\\JARVIS Projeleri`` by default. The known
    folder is held while ``JARVIS Projeleri`` and every candidate are looked
    up RELATIVE to a held handle (D3); no full-path probe beneath it."""
    if type(on_desktop) is not bool:
        raise ValueError("on_desktop must be a bool")
    protected = tuple(protected)
    leaf = sanitize_folder_name(project_name)
    with contextlib.ExitStack() as held:
        handle, parent = _hold_folder(_known_path("desktop" if on_desktop else "documents"), protected)
        held.callback(_close, handle)
        if not on_desktop:
            child = _open_child(handle, DEFAULT_PARENT_NAME)
            if child is None:
                # Nothing beneath a parent that does not exist yet is probed.
                return check_lexical(ntpath.join(parent, DEFAULT_PARENT_NAME, leaf), protected=protected)
            held.callback(_close, child)
            handle, parent = child, _judge(child, ntpath.join(parent, DEFAULT_PARENT_NAME))
        for number in range(1, COLLISION_MAX + 1):
            name = leaf if number == 1 else f"{leaf} ({number})"
            candidate = check_lexical(ntpath.join(parent, name), protected=protected)
            if not _taken(handle, name):
                return candidate
    raise ValueError(f"No free folder name up to ({COLLISION_MAX}).")


def _report(made: list[tuple[int, str]]) -> tuple[str, ...]:
    """2f: each created directory where its OWN handle says it is now; one
    that cannot be read is its intended path, labelled as such."""
    return tuple(_final_path(handle) or f"{intended} ({_UNVERIFIED})" for handle, intended in made)


@contextlib.contextmanager
def create_folder(path: Any, *, protected: Iterable[str | Path]) -> Iterator[FolderCreation]:
    """K1: create EXACTLY ``path`` - ``<Desktop>\\<leaf>`` or ``<Documents>\\
    JARVIS Projeleri\\<leaf>`` (that parent created once when absent) -
    relative to held, proven handles (D3). Only ``create_and_link`` uses it.

    The handles stay held until the context exits; ``created`` is filled
    then, from the handles, before they close. A refusal before the first
    create is a ``ValueError`` with zero filesystem writes; any failure after
    it is a ``FolderCreateError`` naming what was created. Nothing created is
    ever removed (D4)."""
    protected = tuple(protected)
    check_lexical(path, protected=protected)                            # a
    parent, leaf = ntpath.split(path)
    _check_grammar(parent)
    _check_component(leaf)
    if os.name != "nt":
        raise ValueError("Project folders are supported on Windows only.")
    documents = ntpath.basename(parent).casefold() == DEFAULT_PARENT_NAME.casefold()   # b
    known = _known_path("documents" if documents else "desktop")
    if not same_folder(parent, ntpath.join(known, DEFAULT_PARENT_NAME) if documents else known):
        raise ValueError("A folder is created only on the Desktop or in Documents\\JARVIS Projeleri.")
    held: list[tuple[int, str]] = []                 # every handle this context holds
    made: list[tuple[int, str]] = []                 # recorded the moment NtCreateFile succeeds

    def keep(handle: int, expected: str, created: bool) -> int:
        held.append((handle, expected))
        if created:
            made.append((handle, expected))
        _judge(handle, expected)
        return handle

    def close_all() -> None:
        for handle, _ in reversed(held):
            with contextlib.suppress(Exception):
                _close(handle)

    try:
        handle, proven = _hold_folder(known, protected)                 # c
        keep(handle, proven, False)
        if documents:
            child = _open_child(handle, DEFAULT_PARENT_NAME)
            if child is not None:
                handle = keep(child, parent, False)
            else:                                                       # d
                handle = keep(_create_child(handle, DEFAULT_PARENT_NAME), parent, True)
        keep(_create_child(handle, leaf), path, True)                   # e
    except Exception as exc:
        if not made:
            close_all()
            if isinstance(exc, ValueError):
                raise
            raise ValueError("The folder could not be created.") from exc
        created = _report(made)
        close_all()
        raise FolderCreateError(created) from exc
    creation = FolderCreation(path, tuple(held))
    try:
        yield creation                                                  # f
    finally:
        creation.created = _report(made)
        close_all()


def _read_back(store: Any, project_id: str, path: str) -> str:
    """D4: whether the link exists, READ, never assumed."""
    try:
        link = store.load_project_folder(project_id)
    except Exception:  # noqa: BLE001 - an unreadable state is said as such
        return "unknown"
    if link is not None and link.origin == "created" and same_folder(link.path, path):
        return "linked"
    return "not linked"


def create_and_link(store: Any, project_id: str, path: str, *,
                    protected: Iterable[str | Path]) -> tuple[ProjectFolderLink, FolderCreation]:
    """THE one caller of the store's "created" branch (D2): the folder is
    created INSIDE the store's one transaction, after its eligibility,
    current-link and overlap checks and before the INSERT; every held handle
    is judged again just before it returns. The handles close after the store
    call returned or raised. A normal return means committed. A failure after
    any directory was created raises ``FolderCreateError`` with the created
    directories and the READ-BACK link state; one before re-raises as is."""
    protected = tuple(protected)
    holder: list[FolderCreation] = []
    failed: list[str] = []                           # created by a create_folder that then failed
    stack = contextlib.ExitStack()

    def create() -> str:
        try:
            creation = stack.enter_context(create_folder(path, protected=protected))
        except FolderCreateError as exc:
            failed.extend(exc.created)               # kept even if the rollback replaces this error
            raise
        holder.append(creation)
        for handle, expected in creation.handles:
            _judge(handle, expected)
        return creation.path

    try:
        with stack:
            link = store.link_explicit_user_project_folder(project_id, path, "created", create=create)
    except Exception as exc:
        created, error = tuple(failed) or (holder[0].created if holder else ()), exc
    else:
        return link, holder[0]
    if not created:
        raise error
    raise FolderCreateError(created, _read_back(store, project_id, path)) from error
