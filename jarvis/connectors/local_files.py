"""Bounded local file connectors: read (M3) and single-file write (M5).

JARVIS reads the bytes itself. No external model is ever granted filesystem
access, so everything a model can later see about a local file is a bounded,
redacted excerpt that JARVIS produced, persisted and attested. That is the
whole reason this connector exists rather than a ``Read`` tool grant on the
Claude Code subprocess: a tool grant would let the model pull bytes that no
approval ever attested, which is exactly the guarantee H1 exists to hold.

M5 adds the mirror image. JARVIS writes the bytes itself, one file at a
time, and the approval binds the exact bytes through the same canonical
outbound digest an egress uses. The model is granted no filesystem write
tool for the same reason it is granted no read tool: a grant would let it
produce bytes no approval ever attested.

M10 adds the one remaining local mutation: replacing the whole content of a
file that already exists. It is DIRECT only - the replacement bytes come
from the caller, never from a model answer - and it is bound to a completed
whole-file read, so the bytes being destroyed are ones the plan was actually
made against. The mutation happens in place, through the descriptor that
proved the pre-state, because for an EDIT the object already exists and can
therefore be HELD; no name is re-resolved at the syscall, so there is no
window to race. The price is that an edit is not crash-atomic, and that is
said plainly rather than dressed up.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Iterable, NamedTuple
from uuid import uuid4

from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    LOCAL_READ_CAPABILITY,
    LOCAL_READ_MAX_BYTES,
    LOCAL_READ_REGION_SCAN_MAX_BYTES,
    LOCAL_READ_RESULT_KEY,
    LOCAL_READ_TASK_REFERENCE_KEY,
    LOCAL_WRITE_AMBIGUOUS,
    LOCAL_WRITE_CAPABILITY,
    LOCAL_WRITE_DID_NOT_LAND,
    LOCAL_WRITE_FRAME_HEADER,
    LOCAL_WRITE_LANDED,
    LOCAL_WRITE_MAX_BYTES,
    LOCAL_WRITE_RESULT_KEY,
    LOCAL_WRITE_TEMP_PREFIX,
    AttestationError,
    Availability,
    BillingClass,
    CostEstimate,
    DataClassification,
    ErrorCode,
    LocalReadKind,
    LocalReadSnapshot,
    LocalWriteRecord,
    MonetaryState,
    OutboundDataPreview,
    OutcomeStatus,
    RiskLevel,
    SessionState,
    SideEffect,
    SideEffectProfile,
    StructuredError,
    Task,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
    bound_local_text,
    canonical_path,
    excerpt_line_span,
    local_read_denial_reason,
    local_read_path_refusal,
    local_read_region_refusal,
    local_read_snapshot_refusal,
    local_write_content_refusal,
    local_write_mode_unsupported,
    local_write_record_refusal,
    utc_now,
)

#: Every filesystem read this connector performs. ``O_BINARY`` matters on
#: Windows only; ``O_NOFOLLOW`` is added per-open where the platform has it.
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0)

#: Whether the platform can open a name RELATIVE TO AN OPEN DIRECTORY HANDLE.
#: Where it can (POSIX), the whole chain below the allowed root is walked
#: handle by handle with ``O_NOFOLLOW``, so no pathname is ever re-traversed
#: after it was validated and nothing outside the root can be opened at all -
#: not even transiently. Windows has no ``dir_fd`` support, so it takes the
#: handle-authoritative route in ``_open_in_root`` instead.
_DIRECTORY_HANDLE_TRAVERSAL = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)

#: NTFS junctions and symlinks are both reparse points, and a junction needs
#: no privilege to create - so ``S_ISLNK`` alone is not the Windows question.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)

#: Extended-length path prefixes ``GetFinalPathNameByHandleW`` returns.
_UNC_PREFIX = "\\\\?\\UNC\\"
_EXTENDED_PREFIX = "\\\\?\\"

#: Refusal texts. Each one names the policy rule that fired and the
#: root-relative path at most - never file content, never an absolute path.
_NO_TARGET = "A local read task must name exactly one file target."
_OUTSIDE_ROOTS = "Read target is outside the allowed roots."
_NOT_REGULAR = "Read target is not an existing regular file."
_TARGET_MOVED = (
    "Read target changed between authorization and open; refusing to read a "
    "different file under a stale authorization."
)
_TOO_LARGE = (
    f"Read target exceeds the {LOCAL_READ_MAX_BYTES}-byte local read ceiling; "
    "it is refused rather than silently truncated."
)
_NOT_TEXT = "Read target is not strict UTF-8 text."
#: M9. The read window itself is unusable - an INVALID durable identity, or a
#: head read carrying region parameters. Terminal, like every other input
#: refusal on this path: no fresh approval can repair it.
#: M9. Fixed streaming chunk. Bounds peak memory during a region read to one
#: chunk plus the captured region, independently of how large the file is.
_REGION_CHUNK_BYTES = 64 * 1024
_REGION_IDENTITY = "Local read window is not usable:"
#: M9. The load-bearing refusal of this milestone. A region that has not
#: closed by the capture ceiling is REFUSED, never returned as a partial
#: region, because a partial capture would make ``truncated`` ambiguous: it
#: would then mean either "the view shows less than the capture" or "the
#: filesystem request itself was cut", and every consumer inference built on
#: it - including "fewer lines than requested means EOF" - would be unsound.
#: Redaction can shrink a 64 KiB partial capture below the excerpt cap, so
#: such a fragment could otherwise arrive with ``truncated`` FALSE.
_REGION_NOT_CLOSED = (
    f"The requested region did not close within the {LOCAL_READ_MAX_BYTES}-byte "
    "capture ceiling; request fewer lines. It is refused rather than returned "
    "as a partial region."
)
_REGION_SCAN_EXCEEDED = (
    "Locating the first line of the requested region would read past the "
    f"{LOCAL_READ_REGION_SCAN_MAX_BYTES}-byte scan ceiling."
)
_REGION_EMPTY = "The requested region contains no source line."


def _region_past_eof(total_lines: int) -> str:
    """Refusal naming the file's real line count, so a caller paging through
    a file learns where it ends without the reader ever computing a total on
    the success path."""
    return (
        f"Read target has {total_lines} line(s); the requested region starts "
        "past the end of the file."
    )
#: A hard link is a second NAME for one file, not a link to be resolved, so
#: nothing in the containment chain can see it: ``resolve`` has nothing to
#: dereference, it is not a reparse point, and ``GetFinalPathNameByHandleW``
#: answers with the in-root name it was opened by. An in-root name for a file
#: that also lives outside the roots therefore passes every check while the
#: bytes come from outside. Multi-linked regular files are refused instead.
_MULTIPLY_LINKED = (
    "Read target has more than one filesystem name; a multiply-linked file "
    "cannot be proven to live inside the allowed roots and is refused."
)
#: P1-4/P1-5. The path is a resource IDENTIFIER, so it is refused rather than
#: rewritten: a redacted pseudo-path would make the approved identity and the
#: displayed identity diverge while still asserting a trustworthy read.
_UNREPRESENTABLE_PATH = "Read target path cannot be represented safely:"


def _final_path_of_handle(handle: int) -> Path | None:
    """The authoritative path of the OBJECT behind a raw Windows HANDLE.

    ``GetFinalPathNameByHandleW`` answers from the handle, with every reparse
    point already resolved, so a junction or symlink introduced anywhere in
    the chain between validation and open shows up here as a path outside the
    allowed roots. ``None`` means the question could not be answered, which
    every caller treats exactly like an answer outside the roots: no bytes
    are read or written either way.
    """
    try:
        import ctypes
    except ImportError:  # pragma: no cover - non-Windows platforms
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        final_path = kernel32.GetFinalPathNameByHandleW
        final_path.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        final_path.restype = ctypes.c_uint32
        # 32767 is the documented maximum extended-length path, so a single
        # call is always enough and a truncated answer is never accepted.
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_path(ctypes.c_void_p(handle), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            return None
        text = buffer.value
    except Exception:  # noqa: BLE001 - any failure to prove containment
        return None
    # The extended-length prefixes are spelling, not location: strip them so
    # the result compares against the roots like every other resolved path.
    if text.startswith(_UNC_PREFIX):
        text = "\\\\" + text[len(_UNC_PREFIX):]
    elif text.startswith(_EXTENDED_PREFIX):
        text = text[len(_EXTENDED_PREFIX):]
    if not text:
        return None
    return Path(text)


def _final_path_from_handle(descriptor: int) -> Path | None:
    """The same answer, for a Python descriptor rather than a raw HANDLE.

    Windows has no ``dir_fd``, so containment cannot be established by walking
    directory handles; it is established after the fact instead, by asking the
    kernel what the opened object actually is.
    """
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - non-Windows platforms
        return None
    try:
        handle = msvcrt.get_osfhandle(descriptor)
    except Exception:  # noqa: BLE001 - a descriptor with no Windows handle
        return None
    return _final_path_of_handle(handle)


#: Linux exposes every open descriptor as a magic symlink, which makes
#: ``/proc/self/fd/N`` a source that resolves to the OBJECT behind ``N`` and
#: cannot be substituted by anyone who can write the directory. That is the
#: whole reason it is used below.
_PROC_FD = Path("/proc/self/fd")
_PROC_FD_INSTALL = os.name == "posix" and _PROC_FD.is_dir()

#: ``NtSetInformationFile`` class for a rename, and the NT status a
#: no-clobber create or rename answers with when the name is already taken.
_FILE_RENAME_INFORMATION = 10
_STATUS_OBJECT_NAME_COLLISION = 0xC0000035

#: Opening the parent DIRECTORY object itself. Traverse/list access is all the
#: object manager needs to resolve a name beneath it, and every share mode is
#: granted so holding this handle does not lock the directory against anyone.
_DIRECTORY_ACCESS = 0x0001 | 0x0020 | 0x00100000  # LIST | TRAVERSE | SYNCHRONIZE
_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004  # READ | WRITE | DELETE
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # the only way to open a directory

#: The temporary's own handle. ``DELETE`` is what lets the file be renamed
#: through its own handle later, which is the whole P1-A guarantee, and
#: ``os.open`` cannot express it. ``FILE_READ_ATTRIBUTES`` is spelled out
#: because ``os.fstat`` needs it and the NT entry point, unlike
#: ``CreateFileW``, does not add it implicitly.
_TEMPORARY_ACCESS = (
    0x40000000 | 0x00010000 | 0x00100000 | 0x0080
)  # GENERIC_WRITE | DELETE | SYNCHRONIZE | FILE_READ_ATTRIBUTES
#: The temporary's own SHARE mode: nothing is shared at all (P1-B).
#:
#: Access decides what THIS handle may do; share mode decides what anyone
#: else may still obtain against the same OBJECT while it is open. Granting
#: ``FILE_SHARE_WRITE`` here would let a second opener take ``GENERIC_WRITE``
#: on the very object holding the approved bytes, between the fsync and the
#: install, and the installed file would then be theirs rather than the ones
#: that were attested and approved. Identity checks cannot answer that: the
#: object is the right object, only its contents changed. So the exact-byte
#: guarantee is carried by the OS sharing contract, which has no window,
#: instead of by a check with a window after it.
#:
#: ``FILE_SHARE_DELETE`` is not granted either, and is verifiably not needed:
#: the install renames the file through ITS OWN handle, and
#: ``NtSetInformationFile``/``FileRenameInformation`` consults the source
#: handle's access (``DELETE``, granted above), never its share mode. Read
#: sharing is not needed by anything on this route either, so it is not
#: granted: the temporary is unobservable for its whole authoritative life.
_TEMPORARY_SHARE = 0  # deny READ, WRITE and DELETE sharing
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_CREATE = 2  # the ``O_EXCL`` half: collide rather than open or truncate
_CREATE_OPTIONS = 0x20 | 0x40  # SYNCHRONOUS_IO_NONALERT | NON_DIRECTORY_FILE
_OBJ_CASE_INSENSITIVE = 0x40

#: M10. Open an object that must ALREADY EXIST. Never ``FILE_OPEN_IF`` and
#: never ``FILE_OVERWRITE_IF``: an overwrite that finds nothing there is a
#: refusal, not a create, and the disposition is where that is enforced
#: rather than checked.
_FILE_OPEN = 1

#: M10. The existing leaf's own handle. Read to digest the pre-state, write
#: to replace it, attributes so ``os.fstat`` can re-ask the regular-file and
#: link-count questions of the OPENED object, and ``SYNCHRONIZE`` because the
#: create options request synchronous I/O.
_LEAF_ACCESS = (
    0x0001 | 0x0002 | 0x0080 | 0x00100000
)  # FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE

#: M10. The leaf's SHARE mode, and it IS the authority argument for the whole
#: milestone.
#:
#: A descriptor pins the OBJECT. That is enough to know which bytes are being
#: replaced, and it is NOT enough to know where they are: on a platform with
#: no sharing contract, the approved directory entry can be renamed - even
#: out of the allowed roots - between the digest and the write, and the bytes
#: then land at a path no approval ever named.
#:
#: Denying ``FILE_SHARE_WRITE`` and ``FILE_SHARE_DELETE`` closes exactly that.
#: Rename and delete both require an open with ``DELETE`` access, and such an
#: open is refused while any existing handle omits ``FILE_SHARE_DELETE``;
#: writing requires ``FILE_WRITE_DATA``, refused while any existing handle
#: omits ``FILE_SHARE_WRITE``; and the name cannot be reused because the
#: object occupying it cannot be removed. So the object AND its entry are
#: pinned for the whole verify -> rewrite interval, by the OS rather than by
#: a check with a window after it - the same argument ``_TEMPORARY_SHARE``
#: already makes for the create route's temporary.
#:
#: The check is bidirectional, which is the other half of the guarantee: our
#: own open FAILS if a conflicting handle already exists, so "our open
#: succeeded" is itself the proof that none did.
_LEAF_SHARE = 0x00000001  # FILE_SHARE_READ only

#: NT statuses the leaf open can answer with and that are policy, not I/O.
_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_STATUS_SHARING_VIOLATION = 0xC0000043

#: M10. The only volume shape whose share-mode guarantee this milestone has
#: actually verified. Everything else - network redirectors, user-mode
#: filesystems, removable media, ReFS, anything unidentifiable - is refused
#: rather than assumed to enforce sharing faithfully.
_DRIVE_FIXED = 3
_ELIGIBLE_FILESYSTEM = "NTFS"


def _windows_native_api():
    """The NT primitives the Windows write route needs, or ``None``.

    Both the create and the rename have to be expressed RELATIVE TO AN OPEN
    DIRECTORY HANDLE. That is what binds the destination to the directory
    OBJECT this connector already verified instead of to a pathname the
    kernel re-resolves at the syscall - and a pathname re-check cannot
    substitute for it, because any check has a window after it.

    The Win32 surface cannot express the relative form at all:
    ``SetFileInformationByHandle`` refuses a non-NULL ``RootDirectory`` with
    ``ERROR_INVALID_PARAMETER``, and ``CreateFileW`` takes no root argument.
    The NT entry points underneath it do, so they are used directly.

    Resolved once, at import, so a platform that cannot offer the primitives
    is known before any task runs rather than discovered mid-write.
    """
    try:
        import ctypes
        import ctypes.wintypes as wintypes
    except ImportError:  # pragma: no cover - non-Windows platforms
        return None

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_void_p),
        ]

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_ushort),
            ("MaximumLength", ctypes.c_ushort),
            ("Buffer", ctypes.c_wchar_p),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_ulong),
            ("RootDirectory", ctypes.c_void_p),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", ctypes.c_ulong),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        open_directory = kernel32.CreateFileW
        open_directory.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        open_directory.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = wintypes.BOOL
        create_file = ntdll.NtCreateFile
        create_file.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_ulong,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock),
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        create_file.restype = ctypes.c_long
        set_information = ntdll.NtSetInformationFile
        set_information.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_IoStatusBlock),
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
        ]
        set_information.restype = ctypes.c_long
    except (AttributeError, OSError):  # pragma: no cover - not Windows
        return None
    return {
        "open_directory": open_directory,
        "close_handle": close_handle,
        "create_file": create_file,
        "set_information": set_information,
        "IoStatusBlock": _IoStatusBlock,
        "UnicodeString": _UnicodeString,
        "ObjectAttributes": _ObjectAttributes,
    }


_WINDOWS_NATIVE = _windows_native_api()
_WINDOWS_HANDLE_INSTALL = _WINDOWS_NATIVE is not None

#: M10. Whether this platform can pin the approved DIRECTORY ENTRY - not just
#: the object behind it - for the whole verify -> rewrite interval.
#:
#: A POSIX descriptor refers to the inode. ``rename`` and ``unlink`` act on
#: directory entries and have no effect on any open descriptor, so a write
#: through a held fd reaches the approved object wherever its name has since
#: been moved - including outside the allowed roots, on the same filesystem.
#: JARVIS write authority is (object AND path): the H1 frame binds the path,
#: ``_authority_of`` compares it, and containment and the secret-file deny
#: list are statements about names. Mutating the approved inode at a path the
#: approval never named is therefore an authority violation, not a
#: concurrency residual - and POSIX offers no primitive that closes it.
#: ``fstatat`` comparison is a check with a window after it, advisory locks
#: are ignorable by design, mandatory locking was removed from Linux, leases
#: break on open rather than on rename, and ``renameat2`` has no
#: destination-inode precondition.
#:
#: So overwrite is enabled only where the OS offers a sharing contract that
#: pins the name, and refused everywhere else. Reads and creates are
#: unaffected on every platform: a read takes bytes FROM the approved object,
#: so where it is later named changes nothing about what was disclosed, and a
#: create installs a new object whose destination absence the kernel enforces.
_ENTRY_PINNED_OVERWRITE = _WINDOWS_HANDLE_INSTALL


def _windows_open_directory(path: Path) -> int | None:
    """A HANDLE to the directory at ``path``, or ``None``.

    Reparse points are deliberately FOLLOWED here rather than opened as
    themselves: the caller proves what it got by asking the handle for its
    final path, and a followed junction answers with the directory it
    actually landed in. A reparse point opened as itself would answer with
    its own name and prove nothing.
    """
    import ctypes

    handle = _WINDOWS_NATIVE["open_directory"](
        str(path),
        _DIRECTORY_ACCESS,
        _SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle in (ctypes.c_void_p(-1).value, 0, None):
        return None
    return handle


def _windows_close_handle(handle: int) -> None:
    import ctypes

    _WINDOWS_NATIVE["close_handle"](ctypes.c_void_p(handle))


def _windows_create_in_parent(parent_handle: int, leaf: str) -> int:
    """``FILE_CREATE`` a regular file at ``leaf`` BENEATH ``parent_handle``.

    ``RootDirectory`` is the verified parent handle and the name is the bare
    leaf, so the object manager resolves it against that directory OBJECT and
    no pathname is walked. ``FILE_CREATE`` is the ``O_EXCL`` half: the create
    itself is the uniqueness proof, not a preceding existence check.

    ``_TEMPORARY_SHARE`` shares nothing, so from the instant the object
    exists until the install renames it, no second handle can obtain write
    access to it and the bytes under this descriptor stay the approved ones.
    """
    import ctypes
    import msvcrt

    api = _WINDOWS_NATIVE
    name = api["UnicodeString"](len(leaf) * 2, len(leaf) * 2, leaf)
    attributes = api["ObjectAttributes"](
        ctypes.sizeof(api["ObjectAttributes"]),
        parent_handle,
        ctypes.pointer(name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    handle = ctypes.c_void_p()
    status = (
        api["create_file"](
            ctypes.byref(handle),
            _TEMPORARY_ACCESS,
            ctypes.byref(attributes),
            ctypes.byref(api["IoStatusBlock"]()),
            None,
            _FILE_ATTRIBUTE_NORMAL,
            _TEMPORARY_SHARE,
            _FILE_CREATE,
            _CREATE_OPTIONS,
            None,
            0,
        )
        & 0xFFFFFFFF
    )
    if status == _STATUS_OBJECT_NAME_COLLISION:
        raise FileExistsError(errno.EEXIST, "temporary already exists")
    if status:
        raise OSError(errno.EIO, f"temporary could not be created (0x{status:08X})")
    return msvcrt.open_osfhandle(handle.value, os.O_BINARY | os.O_WRONLY)


def _windows_volume_facts(path: Path) -> tuple[int, str] | None:
    """``(drive type, filesystem name)`` for the volume ``path`` sits on.

    ``None`` means the question could not be answered, which every caller
    treats exactly like an ineligible answer: an overwrite is refused rather
    than performed on a volume whose sharing behaviour is unknown.
    """
    try:
        import ctypes
    except ImportError:  # pragma: no cover - non-Windows platforms
        return None
    try:
        anchor = str(Path(path).anchor)
        if not anchor:
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        drive_type = int(kernel32.GetDriveTypeW(ctypes.c_wchar_p(anchor)))
        filesystem = ctypes.create_unicode_buffer(64)
        answered = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(anchor),
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        )
        if not answered:
            return None
        return drive_type, filesystem.value
    except Exception:  # noqa: BLE001 - any failure to identify the volume
        return None


def _windows_handle_filesystem(descriptor: int) -> str | None:
    """The filesystem name reported BY THE OPEN OBJECT, not by a pathname.

    The approval-time check asks a path; this asks the handle that is about
    to be rewritten, so a volume swapped underneath the name after approval
    answers here as itself. ``None`` is treated as ineligible.
    """
    try:
        import ctypes
        import msvcrt
    except ImportError:  # pragma: no cover - non-Windows platforms
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        filesystem = ctypes.create_unicode_buffer(64)
        answered = kernel32.GetVolumeInformationByHandleW(
            ctypes.c_void_p(msvcrt.get_osfhandle(descriptor)),
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        )
        if not answered:
            return None
        return filesystem.value
    except Exception:  # noqa: BLE001 - any failure to identify the volume
        return None


def _volume_refusal(path: Path) -> str | None:
    """Why this volume may not host an in-place overwrite, or ``None``.

    The REMOTE question is answered lexically, and first. Asking Windows
    about a UNC anchor is itself network activity, so a target that is going
    to be refused anyway must never be able to make this connector reach out
    to decide it. Only a local-looking anchor is ever handed to the volume
    APIs.

    Callers must run the no-follow root/parent/reparse-chain policy BEFORE
    calling this: a volume query aimed by a chain this connector has not yet
    accepted would be inspecting a destination it never agreed to look at.
    """
    # A leading separator means a UNC share or a driveless rooted path.
    # Neither can be a local fixed volume, and asking Windows about the
    # first one is the very network activity this avoids.
    anchor = str(Path(path).anchor)
    if not anchor or anchor.startswith("\\"):
        return _WRITE_VOLUME_INELIGIBLE
    facts = _windows_volume_facts(path)
    if facts is None:
        return _WRITE_VOLUME_UNKNOWN
    drive_type, filesystem = facts
    if drive_type != _DRIVE_FIXED or filesystem.upper() != _ELIGIBLE_FILESYSTEM:
        return _WRITE_VOLUME_INELIGIBLE
    return None


def _windows_open_in_parent(parent_handle: int, leaf: str) -> int:
    """``FILE_OPEN`` the EXISTING ``leaf`` BENEATH ``parent_handle``.

    The sibling of ``_windows_create_in_parent``, and it differs in exactly
    the three ways that matter: the disposition opens rather than creates, the
    access is read AND write because this object is about to be rewritten,
    and the share mode denies write and delete so the object and its name are
    both pinned (see ``_LEAF_SHARE``).

    ``RootDirectory`` is the verified parent handle and the name is the bare
    leaf, so the object manager resolves it against that directory OBJECT and
    no pathname is walked - the same property the create route relies on.

    A sharing violation is a REFUSAL, not an I/O fault: something else holds
    the file with conflicting access, so it cannot be pinned for the whole
    replacement and the write fails closed.
    """
    import ctypes
    import msvcrt

    api = _WINDOWS_NATIVE
    name = api["UnicodeString"](len(leaf) * 2, len(leaf) * 2, leaf)
    attributes = api["ObjectAttributes"](
        ctypes.sizeof(api["ObjectAttributes"]),
        parent_handle,
        ctypes.pointer(name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    handle = ctypes.c_void_p()
    status = (
        api["create_file"](
            ctypes.byref(handle),
            _LEAF_ACCESS,
            ctypes.byref(attributes),
            ctypes.byref(api["IoStatusBlock"]()),
            None,
            _FILE_ATTRIBUTE_NORMAL,
            _LEAF_SHARE,
            _FILE_OPEN,
            _CREATE_OPTIONS,
            None,
            0,
        )
        & 0xFFFFFFFF
    )
    if status == _STATUS_SHARING_VIOLATION:
        raise _WriteRefused(_WRITE_TARGET_IN_USE)
    if status in (_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND):
        raise _TargetMoved()
    if status:
        raise OSError(errno.EIO, f"target could not be opened (0x{status:08X})")
    return msvcrt.open_osfhandle(handle.value, os.O_BINARY | os.O_RDWR)


def _windows_install_by_handle(descriptor: int, parent_handle: int, leaf: str) -> None:
    """Rename the OBJECT behind ``descriptor`` to ``leaf`` under the parent.

    Both ends of the mutation are objects, not names.

    The SOURCE is the open descriptor, so there is no source pathname for
    anyone to substitute between the last check and the syscall: the thing
    that moves is the thing this connector created and wrote, by construction
    rather than by inspection.

    The DESTINATION root is the already-verified parent HANDLE and only the
    relative leaf is supplied, so a parent component replaced or repointed
    with a junction after validation cannot move where the file lands - the
    object manager resolves ``leaf`` against the directory object this
    connector proved, and never against a path that could have changed
    meaning since.

    ``ReplaceIfExists`` is FALSE, so the destination's absence is enforced by
    the kernel too: a file that appeared after approval makes the rename fail
    with ``STATUS_OBJECT_NAME_COLLISION`` instead of being overwritten.
    """
    import ctypes
    import msvcrt

    api = _WINDOWS_NATIVE

    class _RenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("_padding", ctypes.c_ubyte * 7),
            ("RootDirectory", ctypes.c_void_p),
            ("FileNameLength", ctypes.c_ulong),
            ("FileName", ctypes.c_wchar * (len(leaf) + 1)),
        ]

    info = _RenameInfo()
    info.ReplaceIfExists = 0
    info.RootDirectory = parent_handle
    info.FileNameLength = len(leaf) * 2
    info.FileName = leaf
    status = (
        api["set_information"](
            ctypes.c_void_p(msvcrt.get_osfhandle(descriptor)),
            ctypes.byref(api["IoStatusBlock"]()),
            ctypes.byref(info),
            ctypes.sizeof(info),
            _FILE_RENAME_INFORMATION,
        )
        & 0xFFFFFFFF
    )
    if not status:
        return
    if status == _STATUS_OBJECT_NAME_COLLISION:
        raise _WriteRefused(_WRITE_STATE_CHANGED)
    raise OSError(errno.EIO, f"handle-sourced install failed (0x{status:08X})")


class _TargetMoved(Exception):
    """The opened descriptor is not the file that was authorized."""


class _ChainMissing(_TargetMoved):
    """A component of the chain does not exist, or is not a directory.

    P2. Raised by the no-follow scan so the ORDINARY "there is nothing there"
    answer stays distinguishable from "something was re-pointed", without
    anyone re-probing the path to find out which. A subclass of
    ``_TargetMoved`` so every caller that only cares about "refused" keeps
    catching it unchanged.
    """


class _LeafReparse(_TargetMoved):
    """The LAST component the scan was asked to clear is a reparse point.

    Distinguished from a reparse point higher up so a caller can say which
    thing it refused - the endpoint it named, or the chain leading to it -
    without following either to find out. Also a ``_TargetMoved``, so a
    caller that does not care keeps catching it unchanged.
    """


class _NotRegularTarget(Exception):
    """The accepted chain ends at something that is not a regular file."""


class _TargetTooLarge(Exception):
    """The file exceeds the physical read ceiling; never truncated."""


class _MultiplyLinked(Exception):
    """The opened file has more than one name, so containment cannot hold."""


class _RegionNotClosed(Exception):
    """The requested region did not close within the capture ceiling."""


class _RegionScanExceeded(Exception):
    """Reaching the region's first line would pass the scan ceiling."""


class _NotTextBytes(Exception):
    """A NUL byte was seen while scanning or capturing; not text."""


class _RegionPastEof(Exception):
    """The file has fewer lines than the requested region's first line."""

    def __init__(self, total_lines: int) -> None:
        super().__init__(total_lines)
        self.total_lines = total_lines


class _RootBoundLocalConnector(BaseConnector):
    """Everything both local capabilities need to stay inside the roots.

    Extracted from the M3 read connector unchanged so there is exactly ONE
    containment implementation. A second, independently written copy for the
    write path would be the weaker of the two the moment either drifted, and
    containment is precisely the property that must not have two answers.
    """

    #: Provider-neutral evidence label for the unbilled local route. Each
    #: capability names its own so the ledger can tell them apart.
    _billing_evidence = "local:no_external_call"

    def __init__(
        self,
        allowed_roots: Iterable[str | Path] | None = None,
        default_working_directory: str | Path | None = None,
    ) -> None:
        """Bind the authority set, or refuse to exist (P2-6).

        Omitting ``allowed_roots`` entirely is the documented default and
        means "the configured startup/project root". Passing an EXPLICITLY
        empty iterable is not a synonym for that - it says "no roots at all" -
        and quietly turning it into the current working directory would widen
        an empty authority set into a real one, at construction time, before
        any approval or execution could refuse it. An explicitly empty set is
        therefore an invalid configuration and raises here.

        A root is a real ``str``/``Path`` naming a real absolute location.
        ``Path("")`` resolves to the current directory, so a blank entry is
        the same authority expansion by another spelling and is refused too.
        """
        default_root = Path(default_working_directory or Path.cwd()).resolve()
        if allowed_roots is None:
            roots: tuple[str | Path, ...] = (default_root,)
        else:
            # A generator is consumed exactly once, here, so "yields nothing"
            # is observable rather than silently re-read as empty later.
            roots = tuple(allowed_roots)
            if not roots:
                raise ValueError(
                    "allowed_roots was explicitly empty; omit it to use the "
                    "default root, or name at least one real root"
                )
        resolved: list[Path] = []
        for root in roots:
            if not isinstance(root, (str, Path)):
                raise TypeError(f"allowed root is not a path: {type(root).__name__}")
            if not str(root).strip():
                raise ValueError("allowed root is blank")
            resolved.append(Path(root).expanduser().resolve())
        self.allowed_roots = tuple(resolved)
        # The identity of each root AS AN OBJECT, captured now - at the moment
        # the authority set is established - rather than re-derived from the
        # root's pathname at read time. Every containment decision downstream
        # is ultimately anchored here, so anchoring it to a pathname would
        # make the whole chain only as strong as that pathname: re-point the
        # root itself between validation and open and a "contained" read is
        # contained in the wrong directory. A root that does not exist gets no
        # identity, and a read under it therefore fails closed.
        self._root_identity: dict[Path, tuple[int, int]] = {}
        for root in self.allowed_roots:
            try:
                info = os.stat(root)
            except OSError:
                continue
            self._root_identity[root] = (info.st_dev, info.st_ino)
        self.default_working_directory = default_root

    def availability(self) -> Availability:
        return Availability(True)

    def _reject_reparse_chain(
        self, root: Path, parts: tuple[str, ...]
    ) -> os.stat_result:
        """Refuse if the root, or any component below it, is not what it was.

        The root is checked FIRST and by identity, not merely by shape: a
        chain that starts somewhere other than the authorized root directory
        is outside the roots however clean its components look.

        P2. This is the ONLY thing allowed to touch the filesystem before the
        chain is accepted, so it is also the only place the facts a caller
        needs about the chain may come from. It therefore RETURNS the
        ``lstat`` of the last component it accepted (the root's own when
        ``parts`` is empty). A caller that wants to know "is the parent a
        directory" or "is the leaf a regular file" reads that answer here,
        from a directory ENTRY the scan already had to fetch, instead of
        asking ``is_dir``/``stat``/``resolve`` - every one of which
        DEREFERENCES, and would therefore follow a junction planted below the
        root before this scan had refused it.

        Each component is lstat'ed and cleared BEFORE the next one is named,
        so a reparse point is always seen as itself rather than traversed.
        """
        identity = self._root_identity.get(root)
        if identity is None:
            raise _TargetMoved()
        try:
            root_info = os.lstat(root)
        except OSError as exc:
            raise _TargetMoved() from exc
        if (root_info.st_dev, root_info.st_ino) != identity:
            raise _TargetMoved()
        info = root_info
        current = root
        last = len(parts) - 1
        for index, name in enumerate(parts):
            current = current / name
            try:
                info = os.lstat(current)
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise _ChainMissing() from exc
            except OSError as exc:
                raise _TargetMoved() from exc
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
            ):
                raise _LeafReparse() if index == last else _TargetMoved()
        return info

    def _containing_root(self, path: Path) -> Path | None:
        """The deepest allowed root containing ``path``; the chain starts there."""
        best: Path | None = None
        for root in self.allowed_roots:
            if root in path.parents and (
                best is None or len(root.parts) > len(best.parts)
            ):
                best = root
        return best

    @staticmethod
    def _resolved_target(task: Task) -> Path | None:
        """The canonical target, or ``None`` when the platform refuses it.

        P2. Canonicalization here is LEXICAL and only lexical:
        ``os.path.abspath`` anchors a relative name to the working directory
        and collapses ``.``/``..`` as string operations - on Windows through
        ``GetFullPathNameW``, which never touches the filesystem. ``resolve``
        cannot be used: it dereferences the chain (on Windows by opening it
        and asking ``GetFinalPathNameByHandleW``), so a junction planted below
        an allowed root would be FOLLOWED here, before the no-follow
        root-identity/reparse-chain policy has accepted a single component.
        That is exactly the inspection this milestone orders after the chain
        check, and it must not happen implicitly in the canonical name.

        Refusing to dereference is also strictly tighter: a reparse point in
        the chain now reaches ``_reject_reparse_chain`` as itself and is
        refused, where ``resolve`` would have laundered an in-root junction
        into a clean-looking path the policy never saw. Nothing is weakened by
        keeping the typed spelling either - containment, the secret-file
        policy and the stored root-relative name are all re-asked of the path
        the OPEN HANDLE proves, never of this one.

        ``abspath`` raises for a target the OS cannot name at all - an
        embedded NUL - and a connector that raises out of ``execute`` or
        ``resource_boundary`` turns a policy question into an exception at a
        stage boundary. ``None`` routes it to the ordinary no-target refusal
        instead. ``AgentCore`` refuses such a target earlier and more
        precisely; this keeps the connector correct when it is driven
        directly.
        """
        target = str(task.scope.target or "").strip()
        if not target:
            return None
        try:
            return Path(os.path.abspath(Path(target).expanduser()))
        except (ValueError, OSError):
            return None

    def _relative_path(self, resolved: Path) -> str:
        """Root-relative POSIX path; no absolute path ever leaves this class.

        The longest matching root wins, so nested roots produce the shortest
        stable name. A path that matches no root cannot reach here - the
        containment check refused it - but the fallback is the bare file name
        rather than anything absolute.
        """
        best = ""
        for root in self.allowed_roots:
            if resolved == root or root in resolved.parents:
                candidate = resolved.relative_to(root).as_posix()
                if not best or len(candidate) < len(best):
                    best = candidate
        return best or resolved.name

    def _working_directory(self, context: dict[str, Any]) -> Path:
        """The effective working directory, proven without following a link.

        P2. ``context["working_directory"]`` is CALLER-CONTROLLED, so it gets
        the same order every other caller-named path gets: lexical grammar,
        lexical containment, then the no-follow component scan - and only a
        path all three accepted is allowed to become the working directory.

        ``resolve()`` used to run first. It dereferences (on Windows by
        opening the chain and asking ``GetFinalPathNameByHandleW``), so a
        junction supplied here was FOLLOWED before any resource policy had
        looked at it - including one aimed outside the allowed roots, whose
        destination the containment check would then have been asked about
        instead of the name the caller actually gave. ``abspath`` collapses
        ``.``/``..`` as a string and never touches the filesystem, so a
        ``..`` route out of the roots is still refused lexically.

        ``is_dir()`` is gone from the caller's path for the same reason: it
        follows. "This is a real directory" now comes from the scan's own
        no-follow ``lstat`` of the final component, which has already refused
        a reparse point sitting there - so a working-directory junction is
        refused AS ITSELF, including one aimed outside the roots, and where it
        pointed is never learned.

        The configured default keeps the checks it always had. It is not
        caller input: it was fixed at construction and its identity is already
        bound in ``_root_identity``, so re-deciding it per task would be a
        redesign of the trusted startup root rather than a fix.
        """
        requested = context.get("working_directory")
        if not requested:
            # The configured startup root, established and identity-bound at
            # construction. Trusted input, not caller input, so it keeps
            # exactly the checks it always had and gains no new ones.
            path = self.default_working_directory
            if not path.is_dir():
                raise ValueError("Working directory does not exist.")
            if not self._within_allowed_roots(path):
                raise ValueError("Working directory is outside the allowed roots.")
            return path
        try:
            path = Path(os.path.abspath(Path(requested).expanduser()))
        except (ValueError, OSError) as exc:
            raise ValueError("Working directory does not exist.") from exc
        if not self._within_allowed_roots(path):
            raise ValueError("Working directory is outside the allowed roots.")
        # ``_containing_root`` answers "strictly below"; a path that IS an
        # allowed root is its own chain with no components under it.
        root = self._containing_root(path) or path
        try:
            info = self._reject_reparse_chain(root, path.relative_to(root).parts)
        except _TargetMoved as exc:
            raise ValueError("Working directory does not exist.") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Working directory does not exist.")
        return path

    def _within_allowed_roots(self, path: Path) -> bool:
        """Containment over resolved paths; ``Path`` equality is case-aware
        per platform, so a Windows casing variant cannot escape."""
        return any(path == root or root in path.parents for root in self.allowed_roots)

    def _usage(self, task: Task, session: SessionState) -> UsageRecord:
        return UsageRecord(
            provider="local",
            model=None,
            connector=self.manifest.name,
            session_id=session.id,
            task_id=task.id,
            call_count=1,
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            # An unbilled route: the state is known, and reporting any
            # monetary amount here would fail H3 reconciliation closed.
            monetary_state=MonetaryState.NOT_APPLICABLE,
            monetary_cost_usd=None,
            billing_evidence=self._billing_evidence,
        )

    def _refusal(
        self,
        task: Task,
        message: str,
        *,
        code: ErrorCode = ErrorCode.INVALID_TASK,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult(
            outcome=OutcomeStatus.ERROR,
            summary=message,
            error=StructuredError(code, message, details or {}, retryable),
            usage=UsageRecord(
                provider="local",
                connector=self.manifest.name,
                task_id=task.id,
                call_count=0,
                billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
                monetary_state=MonetaryState.NOT_APPLICABLE,
                billing_evidence=self._billing_evidence,
            ),
        )


# --------------------------------------------------------------------------
# M5. Bounded single-file local write
# --------------------------------------------------------------------------

#: Creating the same-directory temporary. ``O_EXCL`` makes the create itself
#: the uniqueness proof rather than a prior existence check, so no window
#: exists between "the name was free" and "the name is mine".
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)

_WRITE_NO_TARGET = "A local write task must name exactly one file target."
_WRITE_OUTSIDE_ROOTS = "Write target is outside the allowed roots."
_WRITE_NO_PARENT = (
    "Write target's parent directory does not exist inside the allowed roots; "
    "this capability never creates directories."
)
_WRITE_NOT_REGULAR = "Write target exists and is not a regular file."
_WRITE_MULTIPLY_LINKED = (
    "Write target has more than one filesystem name; an atomic replace would "
    "silently detach the other name, so a multiply-linked file is refused."
)
_WRITE_PRE_STATE_TOO_LARGE = (
    f"Write target already exceeds the {LOCAL_WRITE_MAX_BYTES}-byte local "
    "write ceiling, so the state being replaced cannot be bound; refused "
    "rather than overwritten blind."
)
_WRITE_STATE_CHANGED = (
    "Write target's state changed between authorization and write; refusing "
    "to write under an authorization granted for a different prior state."
)
_WRITE_CHAIN_CHANGED = (
    "Write target's directory chain changed between authorization and write; "
    "refusing to write outside the approved chain."
)
_WRITE_UNREPRESENTABLE_PATH = "Write target path cannot be represented safely:"
_WRITE_NO_SAFE_CREATE = (
    "Write target's filesystem cannot create a name without risking an "
    "overwrite; refusing rather than falling back to an unconditional "
    "replace."
)
#: P1-A. Every install path here takes its SOURCE from an open handle. A
#: platform that cannot do that could only offer a pathname rename, which is
#: re-resolved by the kernel at the syscall and therefore substitutable after
#: any check this connector could make.
_WRITE_NO_HANDLE_INSTALL = (
    "Write target's platform cannot install a file from an open handle, so "
    "the object that would be installed cannot be bound to the object this "
    "connector wrote; refusing rather than performing a substitutable "
    "pathname rename."
)
#: P1-B. The approved record is the authority. Execution re-derives the world
#: and compares; it never adopts what it finds.
#: M10. The platform cannot pin the approved directory entry, so it cannot
#: prove the approved PATH still names the approved OBJECT at the instant of
#: mutation. See ``_ENTRY_PINNED_OVERWRITE``.
_WRITE_NO_ENTRY_PINNING = (
    "Write target's platform cannot pin the approved directory entry against "
    "rename or delete while the file is replaced, so it cannot prove the "
    "approved path still names the approved object at the moment of "
    "mutation; overwrite is refused rather than performed on an object that "
    "may have been moved out of the approved path."
)
_WRITE_VOLUME_UNKNOWN = (
    "Write target's volume could not be identified, so the sharing guarantee "
    "an overwrite depends on cannot be established; refused rather than "
    "assumed."
)
_WRITE_VOLUME_INELIGIBLE = (
    "Write target is not on a local fixed NTFS volume, the only volume shape "
    "whose entry-pinning guarantee this milestone verified; overwrite is "
    "refused rather than performed under an unverified guarantee."
)
_WRITE_TARGET_IN_USE = (
    "Write target is already held by another process with conflicting "
    "access, so it cannot be pinned for the whole replacement; refused."
)
#: M10. The create installer's entire guarantee is the destination's ABSENCE,
#: enforced by the kernel. Nothing but a create may ever reach it.
_WRITE_NOT_THE_CREATE_INSTALLER = (
    "Write mode is not 'create', so it must never reach the create installer "
    "whose guarantee is the destination's absence; refused."
)
#: M10. The read -> plan link. An overwrite destroys bytes, so the bytes it
#: destroys must be ones a completed read already put in front of the caller.
_WRITE_NO_PREIMAGE_REFERENCE = (
    "Overwrite must name the completed local read of this exact file in "
    "context.local_read_task_id; this task names none, and a snapshot that "
    "merely matches proves nothing about where it came from."
)
_WRITE_NO_PREIMAGE = (
    "Overwrite requires a completed local read of this exact file to bind "
    "the bytes being replaced; this task has none bound."
)
_WRITE_PREIMAGE_MALFORMED = (
    "The local read bound to this overwrite is not a structurally valid "
    "snapshot, so it cannot bind a preimage."
)
_WRITE_PREIMAGE_IS_REGION = (
    "The local read bound to this overwrite is a bounded REGION, whose digest "
    "describes only the captured lines; replacing the whole file needs a "
    "whole-file preimage."
)
_WRITE_PREIMAGE_OTHER_FILE = (
    "The local read bound to this overwrite describes a different file than "
    "the write target."
)
_WRITE_PREIMAGE_STALE = (
    "Write target changed since the local read this overwrite was planned "
    "against; refusing to replace bytes that were never reviewed."
)
_WRITE_NO_PREPARED_AUTHORITY = (
    "Write has no durable approved authority to execute under; refusing."
)
_WRITE_AUTHORITY_DIVERGED = (
    "Write target no longer matches the authority that was approved; "
    "refusing to execute under an authority granted for a different state."
)


class _PreState(NamedTuple):
    """What the destination was before the write, as metadata only.

    Absent is ``(False, "", 0)``; present carries the digest and size of the
    bytes about to be replaced. The bytes themselves are hashed and dropped -
    M5 keeps no pre-image, which is exactly why it promises no rollback.
    """

    present: bool
    sha256: str
    byte_size: int


class _WriteIntent(NamedTuple):
    """Everything one approved write is authorized to do."""

    resolved: Path
    root: Path
    parent_parts: tuple[str, ...]
    leaf: str
    relative_path: str
    mode: str
    pre: _PreState
    content: bytes
    intended_sha256: str


class _WriteRefused(Exception):
    """A write policy rule refused; carries the user-facing reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _preimage_binding_refusal(
    task: Task, relative_path: str, pre: _PreState
) -> str | None:
    """Why this overwrite is not bound to a reviewed preimage, or ``None``.

    M10's read -> plan link, and the reason it exists: the connector observes
    the destination's current digest for itself, so without this the approval
    would bind whatever the file happens to be at approval time - including a
    version nobody ever looked at. Requiring a completed WHOLE-FILE read of
    the same path, whose digest equals the observed one, makes "the file I am
    about to replace is still the version the plan was made against" a proof
    rather than a hope. The remaining links - plan to approval, approval to
    execution, execution to mutation - are the frame, the seven-field
    authority comparison and the re-digest through the pinned descriptor.

    A REGION snapshot is refused rather than accepted at a discount: its
    digest covers the captured lines only, and a whole-file replacement needs
    a whole-file preimage. The ``0/0`` head sentinel is the exact
    discriminator, and ``local_read_snapshot_refusal`` has already proved a
    non-zero span against the excerpt it describes.

    The excerpt is deliberately NOT compared with anything. It is
    newline-normalized, credential- and path-redacted and character-capped,
    so it is a display artefact, not the file; the caller here supplies the
    complete postimage explicitly, so nothing is being reconstructed from a
    partial view and the excerpt's fidelity is irrelevant to this operation.

    What this function can and cannot prove. It compares a VALUE - does this
    snapshot describe the bytes on disk right now - and that is all a
    connector is able to establish: it has no store and no registry, so it
    cannot tell a snapshot a real ``file_read`` produced from one somebody
    wrote onto the task. So it also demands the explicit reference, which is
    the thing ``AgentCore`` can and does verify (``_overwrite_preimage_failure``:
    same session, completed, a real ``file_read``, a producer whose manifest
    declares the capability, HEAD not REGION, and the bound snapshot still
    exactly that source's own). Requiring the reference here means a
    connector driven directly - by a test, a script, or anything else that
    bypasses the loop - cannot be handed an injected snapshot and told it is
    authority to destroy a file.
    """
    reference = task.context.get(LOCAL_READ_TASK_REFERENCE_KEY)
    if not isinstance(reference, str) or not reference:
        return _WRITE_NO_PREIMAGE_REFERENCE
    snapshot = task.local_read
    if not snapshot.present:
        return _WRITE_NO_PREIMAGE
    # The ONE canonical structural contract, the same function the load
    # boundary, the envelope and the egress boundary ask.
    if local_read_snapshot_refusal(snapshot) is not None:
        return _WRITE_PREIMAGE_MALFORMED
    if snapshot.start_line != 0 or snapshot.end_line != 0:
        return _WRITE_PREIMAGE_IS_REGION
    if snapshot.relative_path != relative_path:
        return _WRITE_PREIMAGE_OTHER_FILE
    if snapshot.sha256 != pre.sha256 or snapshot.byte_size != pre.byte_size:
        return _WRITE_PREIMAGE_STALE
    return None


def protected_paths(roots: Iterable[str | Path]) -> tuple[Path, ...]:
    """Each protected folder as spelled (lexically absolute) AND as resolved,
    so an 8.3, SUBST or junction spelling of it cannot hide it."""
    roots = tuple(roots)
    spelled = tuple(Path(os.path.abspath(root)) for root in roots)
    return spelled + tuple(Path(root).resolve() for root in roots)


class LocalFileWriteConnector(_RootBoundLocalConnector):
    """Write exactly one in-root UTF-8 text file, once approved.

    TWO routes, and their guarantees are deliberately NOT the same:

    CREATE    the destination must be ABSENT and is never opened for writing.
              The bytes go to a fresh same-directory temporary and become
              visible through one install from that temporary's own handle,
              which the kernel refuses if the name is taken. Atomic: the only
              two states any observer or any crash can see are "not there"
              and the whole new file.
    OVERWRITE the destination must be PRESENT with the attested digest, and it
              IS opened for writing. The leaf is held so that the object and
              its directory entry are both pinned, the pre-state is digested
              through that same descriptor, and the file is truncated and
              rewritten in place. NOT atomic: a crash between the truncate and
              the fsync can leave content that is neither the pre-state nor
              the intended bytes, and no pre-image is kept, so nothing can put
              the previous content back. Windows on a local fixed NTFS volume
              only; every other platform refuses it before any approval.

    Both routes are ``reversible=False`` and both require explicit approval in
    every permission mode. Neither promises rollback.
    """

    _billing_evidence = "local_write:no_external_call"

    def __init__(self, *args: Any, protected_roots: Iterable[str | Path] = (), **kwargs: Any) -> None:
        """``protected_roots``: JARVIS's own folders (its data directory). No
        write lands in or below one, even inside an allowed root (V2.2 b6)."""
        super().__init__(*args, **kwargs)
        self.protected_roots = protected_paths(protected_roots)

    def _within_allowed_roots(self, path: Path) -> bool:
        """Every containment check of this connector - the typed name and each
        handle-proven path - routes through here, so the protected folders
        are refused by all of them."""
        return super()._within_allowed_roots(path) and not any(
            path == protected or protected in path.parents for protected in self.protected_roots
        )

    manifest = ConnectorManifest(
        name="local-file-write",
        capabilities=(LOCAL_WRITE_CAPABILITY,),
        description=(
            "Bounded write of one local UTF-8 text file inside the allowed "
            "roots: an atomic create, or an in-place whole-file overwrite "
            "bound to a completed read."
        ),
        priority=60,
        # Not LOW. A local write mutates state the user cares about and this
        # milestone keeps no pre-image, so the honest risk is not a read's.
        # SMART_AUTO's stated contract - only LOW and reversible runs
        # unattended - then excludes it on its face as well as by the
        # mandatory-approval rule.
        default_risk=RiskLevel.MEDIUM,
        default_side_effect=SideEffectProfile(
            effect=SideEffect.LOCAL_WRITE,
            # Both flags are the literal truth, not caution. Nothing in M5
            # can put the previous bytes back, and LOCAL_WRITE is in
            # MANDATORY_APPROVAL_EFFECTS, so every mode asks and no
            # remembered SESSION grant can ever cover a write.
            reversible=False,
            always_requires_approval=True,
        ),
        timeout_seconds=30,
        default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
        requires_paid_api=False,
    )

    # -- availability / billing ---------------------------------------------

    def estimate_cost(self, task: Task) -> CostEstimate:
        """Connector-owned, never inherited from a persisted task estimate."""
        return CostEstimate(
            provider="local",
            model=None,
            minimum_usd=0.0,
            maximum_usd=0.0,
            confidence="local_filesystem_write",
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            billing_evidence=self._billing_evidence,
            resource_ceiling_usd=None,
            requires_paid_api=False,
        )

    # -- authority ----------------------------------------------------------

    def resource_boundary(self, task: Task) -> tuple[str, tuple[str, ...]]:
        """The canonical local boundary this write is authorized against.

        Called when the permission request is built AND again immediately
        before execution, so a target that leaves the roots, becomes a denied
        secret file or loses its parent directory after approval fails closed
        instead of executing under stale authority.
        """
        try:
            working_directory = self._working_directory(task.context)
        except ValueError as exc:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, str(exc)) from exc
        self._target_policy(task)
        return (
            canonical_path(working_directory),
            tuple(canonical_path(root) for root in self.allowed_roots),
        )

    def canonical_outbound(self, task: Task) -> bytes:
        """The exact effect this approval binds (H1).

        Deliberately more than the content. An approval binding only the
        bytes would authorize writing them over ANY prior state; one binding
        only the path would authorize writing ANY bytes there. The frame
        therefore carries the destination, the declared mode, the pre-state
        the authorization was granted against and the intended digest and
        size, followed by the exact bytes verbatim.

        ``ensure_ascii=True`` guarantees the header JSON contains no literal
        newline, so "the payload is everything after the newline following
        the header" is unambiguous, and the declared ``intended_byte_size``
        makes a truncated payload detectable rather than merely different.

        The digest of these bytes becomes ``outbound_digest``, which flows
        into the attestation digest and the permission fingerprint: change
        the path, the mode, the prior state or one byte of content and the
        approval is no longer valid for what is about to happen.
        """
        intent = self._intent(task)
        header = json.dumps(
            {
                "path": intent.relative_path,
                "mode": intent.mode,
                "pre_present": intent.pre.present,
                "pre_sha256": intent.pre.sha256,
                "pre_byte_size": intent.pre.byte_size,
                "intended_sha256": intent.intended_sha256,
                "intended_byte_size": len(intent.content),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return LOCAL_WRITE_FRAME_HEADER + b"\n" + header + b"\n" + intent.content

    def outbound_preview(self, task: Task) -> OutboundDataPreview | None:
        """What the approval screen shows: metadata, never the bytes.

        The user is approving a change to a named file, and the digest is
        what makes "these exact bytes" checkable. Putting the content here
        would persist it into a pending-approval row for no added authority -
        the fingerprint already binds it.
        """
        try:
            intent = self._intent(task)
        except AttestationError:
            return None
        if intent.mode == "create":
            change = f"create {len(intent.content)} bytes"
            field_names = ("path", "mode", "byte_size", "sha256")
        else:
            # M10. What is being approved is a REPLACEMENT, so the bytes about
            # to be destroyed are named as precisely as the ones replacing
            # them - a byte count alone would not let the user tell WHICH
            # version they are agreeing to lose. Only this branch is new: the
            # create strings and field names are byte-identical, so every
            # existing create fingerprint is preserved by construction rather
            # than by careful matching.
            change = (
                f"overwrite {len(intent.content)} bytes "
                f"(was {intent.pre.byte_size}, sha256 {intent.pre.sha256})"
            )
            field_names = (
                "path",
                "mode",
                "byte_size",
                "sha256",
                "pre_byte_size",
                "pre_sha256",
            )
        return OutboundDataPreview(
            # The SCOPE's destination, not a local pseudo-destination. The
            # core refuses a request whose preview names a different
            # destination than the scope it authorizes, and that consistency
            # check is worth more than a prettier label. The destination the
            # user actually needs - the file - is in the summary below, and
            # the summary is bound into ``display_digest`` and therefore into
            # the approval fingerprint, so it cannot be swapped after the
            # fact any more than the destination could.
            destination=task.scope.destination,
            data_classification=task.scope.data_classification,
            field_names=field_names,
            approximate_bytes=len(intent.content),
            redacted_items=0,
            summary=f"local write {intent.relative_path}: {change}",
        )

    # -- execution -----------------------------------------------------------

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        """Derive the authorized intent, then perform the route it names.

        Every refusal below happens before a single byte is written, on both
        routes. What differs is the mutation, and the two guarantees must not
        be read as one.

        CREATE is atomic and unchanged from M5. The destination is never
        opened for writing at all: the bytes go to a fresh same-directory
        temporary and become visible only through one install sourced from
        that temporary's own handle, whose destination absence the kernel
        enforces. No observer and no crash sees a half-written file.

        OVERWRITE is NOT atomic, and that is stated rather than implied. The
        destination IS opened for writing, held so that both the object and
        its directory entry are pinned, re-digested through that same
        descriptor, then truncated and rewritten in place. A crash between the
        truncate and the fsync can leave the file in a state that is neither
        the attested pre-state nor the intended bytes, and the previous bytes
        are gone - nothing keeps them, which is the same reason this
        capability has never promised rollback. What IS guaranteed: the object
        mutated is exactly the approved object, its name never disappears, no
        other process can write to, rename or delete it for the whole
        interval, the bytes are fsync'd before success is reported, the damage
        is bounded to one approved file, and the outcome stays decidable
        afterwards through ``diagnose_write`` - which H2 never replays.
        """
        # P1-B. The DURABLE APPROVED RECORD is the authority, and execution
        # is only allowed to agree with it. Without this, a destination that
        # changed between approval and execution would simply be re-observed
        # here and become a new, self-granted pre-state.
        approved = task.local_write
        if (
            local_write_record_refusal(approved) is not None
            or approved.status != "prepared"
        ):
            return self._refusal(task, _WRITE_NO_PREPARED_AUTHORITY)
        try:
            intent = self._intent(task)
        except AttestationError as exc:
            return self._refusal(task, str(exc))
        if self._authority_of(intent) != self._authority_of_record(approved):
            return self._refusal(
                task, _WRITE_AUTHORITY_DIVERGED, record=self._record(intent, "refused")
            )
        # Belt and braces: the pre-state carried forward is taken from the
        # APPROVED RECORD, not from what execution just observed. They are
        # proven equal above, so this changes no value - it removes the
        # possibility that a future edit quietly makes the observed state the
        # one that gets used.
        intent = intent._replace(
            pre=_PreState(
                approved.pre_present, approved.pre_sha256, approved.pre_byte_size
            )
        )
        try:
            # M10. Two modes, two entirely separate mutations, and the split is
            # explicit rather than parameterised: a create INSTALLS a new
            # object whose destination absence the kernel enforces, an
            # overwrite REWRITES an object it holds. Neither route can perform
            # the other's operation even if the dispatch were wrong, because
            # each re-asks its own exact mode first.
            if intent.mode == "create":
                self._atomic_write(intent)
            else:
                self._inplace_overwrite(intent)
        except _WriteRefused as exc:
            return self._refusal(
                task, exc.reason, record=self._record(intent, "refused")
            )
        except _TargetMoved:
            return self._refusal(
                task, _WRITE_CHAIN_CHANGED, record=self._record(intent, "refused")
            )
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=(
                f"Wrote {intent.relative_path}: {len(intent.content)} bytes "
                f"({intent.mode})."
            ),
            data={LOCAL_WRITE_RESULT_KEY: self._record(intent, "written")},
            usage=self._usage(task, session),
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        """Independently re-read the destination and compare digests.

        Not a self-report: the file is reopened through the same containment
        chain and hashed again. A third party that changed it between the
        replace and this check fails verification honestly rather than being
        smoothed over - the write did happen, but what is on disk is no
        longer what was authorized.
        """
        record = task.local_write
        if local_write_record_refusal(record) is not None or record.status != "written":
            return VerificationRecord(
                status=VerificationStatus.FAILED,
                method="local_write_destination_integrity",
                evidence=("no valid completed local-write record on the task",),
            )
        try:
            observed = self._observe(task)
        except (AttestationError, _WriteRefused, _TargetMoved, OSError):
            return VerificationRecord(
                status=VerificationStatus.FAILED,
                method="local_write_destination_integrity",
                evidence=(f"path={record.relative_path}", "destination unreadable"),
            )
        if (
            not observed.present
            or observed.sha256 != record.intended_sha256
            or observed.byte_size != record.intended_byte_size
        ):
            return VerificationRecord(
                status=VerificationStatus.FAILED,
                method="local_write_destination_integrity",
                evidence=(
                    f"path={record.relative_path}",
                    f"expected_sha256={record.intended_sha256}",
                    f"observed_sha256={observed.sha256}",
                ),
            )
        return VerificationRecord(
            status=VerificationStatus.PASSED,
            method="local_write_destination_integrity",
            evidence=(
                f"path={record.relative_path}",
                f"mode={record.mode}",
                f"byte_size={record.intended_byte_size}",
                f"sha256={record.intended_sha256}",
            ),
        )

    # -- recovery ------------------------------------------------------------

    def pre_execution_record(self, task: Task) -> LocalWriteRecord | None:
        """The authorized write as durable metadata, BEFORE anything runs.

        Derived from the very same ``_intent`` that produces the canonical
        outbound frame the approval fingerprint binds, so this is the
        ATTESTED authority rendered as metadata - not a connector's
        self-report about what it did afterwards.

        It exists because the case that most needs a diagnosis is the one
        that produces no result at all: if ``execute`` raises, H2 marks the
        attempt non-replayable and there is nothing to compare the
        destination against unless the two attested states were already
        durable before the boundary was crossed.

        ``None`` when the intent cannot be derived at all; the execution that
        follows would refuse for the same reason, so there is no outcome to
        be in doubt about.
        """
        try:
            return self._record(self._intent(task), "prepared")
        except (AttestationError, _WriteRefused, _TargetMoved):
            return None

    def diagnose_write(self, task: Task) -> str:
        """What the destination says happened, for an attempt in doubt.

        Unlike an external egress, a local write is DECIDABLE after the fact:
        both attested states are digests and the file answers for itself.
        This only reports - it never replays, never repairs, never advances
        the task, and never deletes anything.
        """
        record = task.local_write
        try:
            observed = self._observe(task)
        except (AttestationError, _WriteRefused, _TargetMoved, OSError):
            return LOCAL_WRITE_AMBIGUOUS
        if local_write_record_refusal(record) is not None:
            # Nothing valid was ever bound, so the intent is unknown and the
            # only honest answer is that the destination cannot be explained.
            return LOCAL_WRITE_AMBIGUOUS
        if (
            observed.present
            and observed.sha256 == record.intended_sha256
            and observed.byte_size == record.intended_byte_size
        ):
            return LOCAL_WRITE_LANDED
        if observed.present == record.pre_present and (
            not observed.present
            or (
                observed.sha256 == record.pre_sha256
                and observed.byte_size == record.pre_byte_size
            )
        ):
            return LOCAL_WRITE_DID_NOT_LAND
        return LOCAL_WRITE_AMBIGUOUS

    def orphan_temporaries(self, task: Task) -> tuple[str, ...]:
        """Same-directory temporaries left behind, reported and never removed.

        Deleting a file is a DESTRUCTIVE authority this capability does not
        hold, so an orphan is surfaced for the user rather than cleaned up on
        their behalf.
        """
        try:
            root, parent_parts, _ = self._chain(self._target_policy(task))
        except (AttestationError, _WriteRefused, _TargetMoved):
            return ()
        try:
            return tuple(
                sorted(
                    entry.name
                    for entry in root.joinpath(*parent_parts).iterdir()
                    if entry.name.startswith(LOCAL_WRITE_TEMP_PREFIX)
                )
            )
        except OSError:
            return ()

    # -- internals -----------------------------------------------------------

    def _target_policy(self, task: Task) -> Path:
        """Everything decidable about the destination NAME, before any I/O.

        Same order and the same shared predicates the read path uses, so the
        two capabilities cannot drift apart on what a legal target is.
        """
        resolved = self._resolved_target(task)
        if resolved is None:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, _WRITE_NO_TARGET)
        if not self._within_allowed_roots(resolved):
            raise AttestationError(ErrorCode.EGRESS_REFUSED, _WRITE_OUTSIDE_ROOTS)
        reason = local_read_denial_reason(resolved)
        if reason is not None:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED,
                f"Write target is refused by the secret-file policy: {reason}.",
            )
        reason = local_read_path_refusal(self._relative_path(resolved))
        if reason is not None:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED, f"{_WRITE_UNREPRESENTABLE_PATH} {reason}."
            )
        return resolved

    def _chain(self, resolved: Path) -> tuple[Path, tuple[str, ...], str]:
        """(root, parent components, leaf) with the chain proven unchanged."""
        root = self._containing_root(resolved)
        if root is None:
            raise _WriteRefused(_WRITE_OUTSIDE_ROOTS)
        parts = resolved.relative_to(root).parts
        if not parts:
            raise _WriteRefused(_WRITE_NO_TARGET)
        parent_parts = parts[:-1]
        # P2. Root identity plus a no-follow scan of every component down to
        # the parent, FIRST and with nothing before it. Deliberately run on
        # BOTH routes: redundant where directory handles pin the chain, and
        # the only pre-open defence where they do not.
        #
        # ``is_dir()`` used to run ahead of this, to name the right rule for a
        # missing parent. It follows links, so a junction planted at a parent
        # was traversed here - an inspection aimed by a path this scan had not
        # accepted yet - before being refused below. The scan itself now
        # supplies that fact: it ends on the parent's own no-follow ``lstat``,
        # which is the same directory ENTRY ``is_dir`` would have answered
        # from, minus the dereference.
        try:
            parent = self._reject_reparse_chain(root, parent_parts)
        except _ChainMissing as exc:
            # An ordinary "this capability creates no directories", not a
            # chain that moved underneath us.
            raise _WriteRefused(_WRITE_NO_PARENT) from exc
        if not stat.S_ISDIR(parent.st_mode):
            raise _WriteRefused(_WRITE_NO_PARENT)
        return root, parent_parts, parts[-1]

    def _intent(self, task: Task) -> _WriteIntent:
        """The complete authorized write, or an ``AttestationError``.

        Derived identically at approval time and at execution time, which is
        what makes the fingerprint stable across approve -> resume while
        still invalidating it the moment the destination's prior state
        changes.
        """
        resolved = self._target_policy(task)
        # The SUPPORTED-mode question, not just the grammar one: an
        # unsupported mode is refused here, before an approval for it can
        # ever be requested, so nothing downstream has to remember that it
        # would fail closed later.
        reason = local_write_mode_unsupported(task.write_mode)
        if reason is not None:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED, f"Write refused: {reason}."
            )
        # M10. The PLATFORM question, asked in the same place and for the same
        # reason as the policy one: at the top of the single function every
        # authority-building entry point calls. ``canonical_outbound``,
        # ``outbound_preview`` and ``pre_execution_record`` all derive from
        # here, so a host that cannot pin the approved directory entry can
        # never produce a frame, a preview, a fingerprint, a pending approval
        # or an execution attempt for an overwrite.
        #
        # The VOLUME question is deliberately NOT asked here (P2). It touches
        # the filesystem, and nothing may touch the target's filesystem until
        # the no-follow root/parent/reparse-chain policy has accepted the
        # path - otherwise a junction introduced below the root could aim that
        # inspection at a destination this connector has not yet agreed to
        # look at. This check costs no I/O at all, so it keeps its place.
        if task.write_mode == "overwrite" and not _ENTRY_PINNED_OVERWRITE:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED, _WRITE_NO_ENTRY_PINNING
            )
        reason = local_write_content_refusal(task.write_content)
        if reason is not None:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED, f"Write refused: {reason}."
            )
        content = task.write_content.encode("utf-8")
        # P2. The chain policy FIRST, on its own: root identity plus a
        # no-follow scan of every component below it. Nothing that inspects
        # the target's volume or opens the target itself runs before this
        # returns.
        try:
            root, parent_parts, leaf = self._chain(resolved)
        except _WriteRefused as exc:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, exc.reason) from exc
        except _TargetMoved as exc:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED, _WRITE_CHAIN_CHANGED
            ) from exc
        # Only now. The chain has been accepted, so this inspection can no
        # longer be aimed by a reparse point the policy would have refused.
        if task.write_mode == "overwrite":
            reason = _volume_refusal(resolved)
            if reason is not None:
                raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
        try:
            pre = self._pre_state(None, root.joinpath(*parent_parts), leaf)
        except _WriteRefused as exc:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, exc.reason) from exc
        except _TargetMoved as exc:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED, _WRITE_CHAIN_CHANGED
            ) from exc
        # Mode and prior state are ONE authority. A "create" whose target
        # already exists is not silently upgraded to an overwrite, and an
        # "overwrite" of something absent is not silently downgraded to a
        # create: both are refused, because the user approved one of them.
        if (task.write_mode == "create") is pre.present:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, _WRITE_STATE_CHANGED)
        relative_path = self._relative_path(resolved)
        # M10. An overwrite destroys bytes, so it must be bound to a completed
        # WHOLE-FILE read of this exact file whose digest is the one observed
        # here. A create binds nothing of the sort and is untouched: there is
        # no prior state to have reviewed.
        if task.write_mode == "overwrite":
            reason = _preimage_binding_refusal(task, relative_path, pre)
            if reason is not None:
                raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
        return _WriteIntent(
            resolved=resolved,
            root=root,
            parent_parts=parent_parts,
            leaf=leaf,
            relative_path=relative_path,
            mode=task.write_mode,
            pre=pre,
            content=content,
            intended_sha256=hashlib.sha256(content).hexdigest(),
        )

    def _observe(self, task: Task) -> _PreState:
        """The destination's current state, through the full chain."""
        resolved = self._target_policy(task)
        root, parent_parts, leaf = self._chain(resolved)
        return self._pre_state(None, root.joinpath(*parent_parts), leaf)

    def _pre_state(self, parent_fd: int | None, parent: Path, leaf: str) -> _PreState:
        """What is at ``leaf`` now, refusing anything that cannot be replaced.

        Never follows a link: a symlink or reparse point sitting at the
        destination would make the atomic replace land somewhere the approval
        never named, so it is refused rather than resolved.
        """
        try:
            if parent_fd is None:
                info = os.lstat(parent / leaf)
            else:
                info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _PreState(False, "", 0)
        except OSError as exc:
            raise _TargetMoved() from exc
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise _WriteRefused(_WRITE_NOT_REGULAR)
        if not stat.S_ISREG(info.st_mode):
            raise _WriteRefused(_WRITE_NOT_REGULAR)
        # M4's hard-link policy, kept and now doubly justified: an atomic
        # replace does not write THROUGH the other name, it detaches it, so a
        # second name would silently stop tracking this file.
        if info.st_nlink > 1:
            raise _WriteRefused(_WRITE_MULTIPLY_LINKED)
        return _PreState(True, *self._digest(parent_fd, parent, leaf))

    def _digest(
        self, parent_fd: int | None, parent: Path, leaf: str
    ) -> tuple[str, int]:
        """Digest the bytes about to be replaced, then drop them.

        They are held only long enough to hash - never persisted, never
        summarized, never put in a diagnostic. That is precisely why M5
        offers no rollback: nothing keeps them.
        """
        if parent_fd is None:
            descriptor = os.open(parent / leaf, _READ_FLAGS)
        else:
            descriptor = os.open(leaf, _READ_FLAGS | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            if parent_fd is None:
                opened = _final_path_from_handle(descriptor)
                if opened is not None and (
                    not self._within_allowed_roots(opened)
                    or local_read_denial_reason(opened) is not None
                ):
                    raise _TargetMoved()
            raw = os.read(descriptor, LOCAL_WRITE_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > LOCAL_WRITE_MAX_BYTES:
            raise _WriteRefused(_WRITE_PRE_STATE_TOO_LARGE)
        return hashlib.sha256(raw).hexdigest(), len(raw)

    def _open_parent_handle(self, root: Path, parent_parts: tuple[str, ...]) -> int:
        """Walk root -> parent one no-follow, handle-relative step at a time.

        ``O_NOFOLLOW`` is load-bearing here in a way it is not for a read. A
        read binds the opened file's ``(st_dev, st_ino)`` against a prior
        stat, so a swapped component is caught even if it were followed. A
        create has NO prior inode to compare against, so refusing to traverse
        a component that became a link is the only thing standing between the
        approved directory and one substituted for it.
        """
        identity = self._root_identity.get(root)
        if identity is None:
            raise _TargetMoved()
        directory_flags = _READ_FLAGS | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(root, directory_flags)
        try:
            opened_root = os.fstat(parent)
            if (opened_root.st_dev, opened_root.st_ino) != identity:
                raise _TargetMoved()
            for name in parent_parts:
                child = os.open(name, directory_flags, dir_fd=parent)
                os.close(parent)
                parent = child
            return parent
        except OSError as exc:
            os.close(parent)
            # ELOOP is a component that became a symlink, ENOTDIR one that
            # stopped being a directory, ENOENT one that vanished: all three
            # are the race, not an I/O fault.
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                raise _TargetMoved() from exc
            raise
        except BaseException:
            os.close(parent)
            raise

    def _atomic_write(self, intent: _WriteIntent) -> None:
        """Write a fresh object, then install it FROM ITS OWN HANDLE.

        The destination is opened for writing at no point. The bytes go to a
        fresh same-directory temporary and become visible in one step, so the
        only two states any observer or any crash can see are "not there" and
        the whole new file.

        What changed after review, and why it matters: the install no longer
        names its source. A rename or link that takes a source PATHNAME
        re-resolves that name inside the kernel, so proving the name pointed
        at the right object a moment earlier proves nothing about what gets
        installed - it is a check with a window after it. Here the source is
        the open descriptor itself, which cannot be substituted by anyone,
        and the destination's absence is enforced by the syscall rather than
        checked by this code.

        The earlier pre-state check below is kept only as a cheap early-out:
        it avoids writing 64 KiB that the install would refuse anyway. It
        authorizes nothing.
        """
        # M10. EXACT, and not "is this mode enabled": this installer's whole
        # guarantee is that the destination is ABSENT and that the kernel
        # enforces it. Widening the policy tuple must never be able to route an
        # overwrite in here, so the guard names the one mode it serves.
        if intent.mode != "create":
            raise _WriteRefused(_WRITE_NOT_THE_CREATE_INSTALLER)
        parent = intent.root.joinpath(*intent.parent_parts)
        parent_fd = (
            self._open_parent_handle(intent.root, intent.parent_parts)
            if _DIRECTORY_HANDLE_TRAVERSAL
            else None
        )
        # Exactly one of the two is ever held: POSIX pins the parent as a
        # directory descriptor, Windows as a directory HANDLE. They are the
        # same thing - the authoritative parent OBJECT - and everything below
        # names the leaf relative to it rather than by a path that could stop
        # meaning what it meant when it was validated.
        parent_handle = (
            self._open_windows_parent(parent)
            if parent_fd is None and _WINDOWS_HANDLE_INSTALL
            else None
        )
        try:
            if self._pre_state(parent_fd, parent, intent.leaf) != intent.pre:
                raise _WriteRefused(_WRITE_STATE_CHANGED)
            temporary = f"{LOCAL_WRITE_TEMP_PREFIX}{uuid4().hex}.tmp"
            descriptor = self._create_temporary(
                parent_fd, parent_handle, temporary
            )
            try:
                offset = 0
                while offset < len(intent.content):
                    offset += os.write(descriptor, intent.content[offset:])
                os.fsync(descriptor)
                written = os.fstat(descriptor)
                self._install_from_handle(
                    descriptor, parent_fd, parent_handle, intent.leaf
                )
            finally:
                os.close(descriptor)
            self._drop_temporary(
                parent_fd, parent, temporary, (written.st_dev, written.st_ino)
            )
            if parent_fd is not None:
                os.fsync(parent_fd)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
            if parent_handle is not None:
                _windows_close_handle(parent_handle)

    def _inplace_overwrite(self, intent: _WriteIntent) -> None:
        """Rewrite the approved object IN PLACE, through a handle that pins it.

        M5 deferred overwrite because no RENAME primitive expresses "replace
        this destination if and only if it is still the approved object". That
        finding stands and is not weakened here. What changed is the question:
        an edit, unlike a create, acts on an object that ALREADY EXISTS, so
        the object can be HELD rather than named. The leaf is opened beneath
        the parent handle this connector already proved, the pre-state is
        digested through that same descriptor, and the replacement is written
        through it too - there is no pathname anywhere between the proof and
        the mutation, so there is no window for anyone to substitute
        something. That is the same structural argument the create route makes
        for its handle-sourced install, applied to an object that was already
        there.

        The share mode carries the other half. A descriptor alone pins the
        OBJECT and not its NAME; ``_LEAF_SHARE`` denies write and delete
        sharing, so for the whole verify -> rewrite interval nobody can modify
        the object, rename it, delete it, or put a different file at the leaf.
        Both halves of JARVIS write authority - which object, and at which
        path - therefore hold at the instant of mutation rather than at the
        instant of a preceding check.

        WHAT THIS ROUTE DOES NOT DO, and must never be made to do: there is no
        temporary file, no rename, no ``ReplaceIfExists``, and no check
        followed by an unconditional replace. Introducing any of them would
        reintroduce exactly the race M5 removed.

        WHAT IT COSTS, stated plainly because the record must be honest: the
        destination IS opened for writing and IS truncated, so this is NOT
        crash-atomic. A crash, power loss or hard kill between the truncate
        and the fsync leaves content that is neither the preimage nor the
        intended bytes, and the preimage is gone - nothing keeps it, which is
        the same reason M5 promises no rollback. The damage is bounded to at
        most ``LOCAL_WRITE_MAX_BYTES`` of one named, approved file, it is
        DECIDABLE afterwards by ``diagnose_write``, and H2 never replays an
        attempt in doubt.

        Deliberately preserved, and a second independent reason for choosing
        in place over temp-and-replace: the file keeps its identity,
        permissions, ACLs and timestamps' owner. Installing a fresh object
        over a tracked source file would silently reset its mode bits and its
        inode, and there is no bounded way to copy that metadata back
        faithfully.
        """
        # Re-asked here and not merely dispatched: even a wrong dispatch must
        # not be able to perform the other route's operation.
        if intent.mode != "overwrite":  # pragma: no cover - dispatch guards it
            raise _WriteRefused(_WRITE_NOT_THE_CREATE_INSTALLER)
        if not _ENTRY_PINNED_OVERWRITE:
            raise _WriteRefused(_WRITE_NO_ENTRY_PINNING)
        parent = intent.root.joinpath(*intent.parent_parts)
        parent_handle = self._open_windows_parent(parent)
        try:
            descriptor = _windows_open_in_parent(parent_handle, intent.leaf)
            try:
                self._verify_pinned_target(descriptor, intent)
                # Digested through the SAME descriptor that is about to be
                # rewritten, so the bytes proved here are the bytes replaced.
                # One byte past the ceiling is read for the same reason the
                # head reader reads it: to tell "exactly at" from "over".
                raw = os.read(descriptor, LOCAL_WRITE_MAX_BYTES + 1)
                if len(raw) > LOCAL_WRITE_MAX_BYTES:
                    raise _WriteRefused(_WRITE_PRE_STATE_TOO_LARGE)
                # Against the APPROVED record's pre-state, which ``execute``
                # has already proved equal to the intent's. The observation is
                # compared with the authority; it never becomes one.
                if (
                    hashlib.sha256(raw).hexdigest() != intent.pre.sha256
                    or len(raw) != intent.pre.byte_size
                ):
                    raise _WriteRefused(_WRITE_STATE_CHANGED)
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.ftruncate(descriptor, 0)
                offset = 0
                while offset < len(intent.content):
                    offset += os.write(descriptor, intent.content[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            _windows_close_handle(parent_handle)

    def _verify_pinned_target(self, descriptor: int, intent: _WriteIntent) -> None:
        """Re-ask every question of the OPENED object, before a byte moves.

        The pre-state check at approval time asked a NAME. This asks the
        object the mutation will actually reach, which is the only version of
        the question that can still be true at the syscall.
        """
        actual = os.fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode) or (
            getattr(actual, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise _WriteRefused(_WRITE_NOT_REGULAR)
        # Kept from M4/M5, with the reason restated for an edit: an in-place
        # rewrite does not detach a second name, it writes THROUGH it, so the
        # objection here is containment rather than detachment. Containment is
        # a statement about names, and a file with two names is inside the
        # allowed roots by only one of them.
        if actual.st_nlink > 1:
            raise _WriteRefused(_WRITE_MULTIPLY_LINKED)
        # The authority is (object AND path), so the path is re-derived from
        # the OBJECT's own answer rather than from the pathname that was
        # validated earlier.
        opened = _final_path_from_handle(descriptor)
        if (
            opened is None
            or opened != intent.resolved
            or not self._within_allowed_roots(opened)
            or local_read_denial_reason(opened) is not None
            or self._relative_path(opened) != intent.relative_path
        ):
            raise _TargetMoved()
        # The volume, asked of the HANDLE and then of its authoritative path.
        # The approval-time check asked a pathname; a volume swapped underneath
        # that name afterwards answers here as itself.
        filesystem = _windows_handle_filesystem(descriptor)
        if filesystem is None or filesystem.upper() != _ELIGIBLE_FILESYSTEM:
            raise _WriteRefused(_WRITE_VOLUME_INELIGIBLE)
        reason = _volume_refusal(opened)
        if reason is not None:
            raise _WriteRefused(reason)

    def _install_from_handle(
        self,
        descriptor: int,
        parent_fd: int | None,
        parent_handle: int | None,
        leaf: str,
    ) -> None:
        """Install the object behind ``descriptor`` at ``leaf``, or refuse.

        Two platform routes, one guarantee, and the guarantee has two halves:
        the installed object is the one this connector wrote, and it lands
        beneath the parent DIRECTORY OBJECT this connector verified. Neither
        end is a pathname the kernel re-resolves at the syscall, and an
        existing destination is refused BY THE KERNEL rather than by a
        preceding check.

        * Windows renames the file through its own handle, with the verified
          parent handle as the NT ``RootDirectory`` and the bare leaf as the
          name (``NtSetInformationFile`` / ``FileRenameInformation``,
          ``ReplaceIfExists`` FALSE). The rename also consumes the temporary
          name, so nothing is left behind.
        * Linux links ``/proc/self/fd/N`` - a magic symlink that always
          resolves to the object behind the descriptor - into the destination
          directory through the pinned parent descriptor. ``link`` refuses an
          existing name with ``EEXIST``.

        Anywhere else this refuses. The only remaining option would be a
        pathname rename, which is exactly the substitutable operation this
        method exists to avoid, so it fails closed instead.
        """
        if parent_handle is not None:
            _windows_install_by_handle(descriptor, parent_handle, leaf)
            return
        if _PROC_FD_INSTALL and parent_fd is not None:
            try:
                os.link(
                    str(_PROC_FD / str(descriptor)),
                    leaf,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=True,
                )
            except FileExistsError as exc:
                raise _WriteRefused(_WRITE_STATE_CHANGED) from exc
            return
        raise _WriteRefused(_WRITE_NO_HANDLE_INSTALL)

    def _drop_temporary(
        self,
        parent_fd: int | None,
        parent: Path,
        temporary: str,
        identity: tuple[int, int],
    ) -> None:
        """Remove our own temporary name, and ONLY if it is still ours.

        On the Windows route the rename already consumed the name, so there
        is nothing to do. On the link route the object now has two names and
        one of them must go, or the destination would carry a link count this
        connector's own policy refuses to ever write again.

        The identity check is not about safety of the destination - that is
        already installed - but about not deleting somebody else's file: if
        the temporary name was substituted while we held the descriptor, the
        thing under that name is theirs, and it is left alone and reported as
        an orphan instead.
        """
        try:
            if parent_fd is None:
                info = os.lstat(parent / temporary)
            else:
                info = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            return
        if (info.st_dev, info.st_ino) != identity:
            return
        try:
            if parent_fd is None:
                os.unlink(parent / temporary)
            else:
                os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            return

    def _open_windows_parent(self, parent: Path) -> int:
        """A HANDLE to the parent DIRECTORY OBJECT, proven to be the right one.

        Windows has no ``dir_fd``, so the chain cannot be walked handle by
        handle the way ``_open_parent_handle`` walks it. The parent is opened
        by name ONCE and then proven from the handle: the kernel answers with
        the directory it actually landed in, so a junction anywhere in the
        chain shows up here as a different directory and is refused before
        anything is created.

        After this moment the pathname stops mattering. Every filesystem
        operation the mutation performs - the exclusive create and the
        no-clobber install - is relative to THIS handle, so a component
        replaced or repointed afterwards can no longer move where the bytes
        land. That is the difference between this and a final pathname
        re-check, which would only move the race window rather than close it.
        """
        handle = _windows_open_directory(parent)
        if handle is None:
            raise _TargetMoved()
        try:
            opened = _final_path_of_handle(handle)
            if opened is None or opened != parent:
                raise _TargetMoved()
            if not self._within_allowed_roots(opened):
                raise _TargetMoved()
        except BaseException:
            _windows_close_handle(handle)
            raise
        return handle

    def _create_temporary(
        self, parent_fd: int | None, parent_handle: int | None, temporary: str
    ) -> int:
        """A fresh, exclusive temporary created BENEATH THE PINNED PARENT.

        Same directory because an install is only atomic within one
        filesystem. Exclusive because the object must be ours alone from the
        moment it exists. Neither route names the parent by pathname: POSIX
        passes the pinned directory descriptor as ``dir_fd``, Windows passes
        the pinned directory HANDLE as the NT ``RootDirectory``. On Windows
        the temporary's own handle also carries ``DELETE`` access, which is
        what lets the file be renamed through that handle later.

        Neither handle means no safe create and no handle-sourced install
        either, so it refuses here rather than falling back to a pathname.
        The parent's PATHNAME is not a parameter of this method, nor of the
        install below: there is nothing left for either one to re-resolve.
        """
        if parent_fd is not None:
            return os.open(
                temporary, _WRITE_FLAGS | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd
            )
        if parent_handle is not None:
            return _windows_create_in_parent(parent_handle, temporary)
        raise _WriteRefused(_WRITE_NO_HANDLE_INSTALL)

    @staticmethod
    def _authority_of(intent: _WriteIntent) -> tuple:
        """The seven fields that ARE the authority, as a comparable tuple."""
        return (
            intent.relative_path,
            intent.mode,
            intent.pre.present,
            intent.pre.sha256,
            intent.pre.byte_size,
            intent.intended_sha256,
            len(intent.content),
        )

    @staticmethod
    def _authority_of_record(record: LocalWriteRecord) -> tuple:
        """The same seven fields, read off the durable approved record.

        ``status`` and ``written_at`` are deliberately excluded: they say
        when and how far the operation got, not what it was allowed to do.
        """
        return (
            record.relative_path,
            record.mode,
            record.pre_present,
            record.pre_sha256,
            record.pre_byte_size,
            record.intended_sha256,
            record.intended_byte_size,
        )

    def _record(self, intent: _WriteIntent, status: str) -> LocalWriteRecord:
        return LocalWriteRecord(
            relative_path=intent.relative_path,
            mode=intent.mode,
            pre_present=intent.pre.present,
            pre_sha256=intent.pre.sha256,
            pre_byte_size=intent.pre.byte_size,
            intended_sha256=intent.intended_sha256,
            intended_byte_size=len(intent.content),
            status=status,
            written_at=utc_now(),
        )

    def _refusal(
        self,
        task: Task,
        message: str,
        *,
        code: ErrorCode = ErrorCode.INVALID_TASK,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        record: LocalWriteRecord | None = None,
    ) -> ToolResult:
        """A refusal, optionally carrying what was authorized and refused.

        The record describes the AUTHORIZED intent, not the state that caused
        the refusal, so the envelope shows what the user approved and what
        did not happen - never anything read off the destination during the
        refusal itself.
        """
        result = super()._refusal(
            task, message, code=code, retryable=retryable, details=details
        )
        if record is not None and local_write_record_refusal(record) is None:
            result.data = {LOCAL_WRITE_RESULT_KEY: record}
        return result


__all__ = [
    "LOCAL_READ_RESULT_KEY",
    "LOCAL_WRITE_RESULT_KEY",
    "LocalFileReadConnector",
    "LocalFileWriteConnector",
]


class LocalFileReadConnector(_RootBoundLocalConnector):
    """Read exactly one in-root text file into a bounded typed snapshot."""

    _billing_evidence = "local_read:no_external_call"

    manifest = ConnectorManifest(
        name="local-file-read",
        capabilities=(LOCAL_READ_CAPABILITY,),
        description=(
            "Bounded, redacted read of one local UTF-8 text file inside the "
            "allowed roots."
        ),
        priority=60,
        default_risk=RiskLevel.LOW,
        # LOCAL_READ is deliberately NOT a mandatory-approval effect: this
        # connector is the first real consumer of the existing authority
        # model rather than a special case bolted onto it. STRICT still asks;
        # SESSION and SMART_AUTO auto-approve because the effect is LOW and
        # reversible. ``_LOCAL_EFFECT_LADDER`` still lets a task strengthen
        # the profile, and a conflicting pair still fails closed.
        default_side_effect=SideEffectProfile(
            effect=SideEffect.LOCAL_READ,
            reversible=True,
            always_requires_approval=False,
        ),
        timeout_seconds=30,
        # Explicit, not inherited: the manifest default is UNKNOWN and would
        # be refused before execution. A local read spends nothing.
        default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
        requires_paid_api=False,
    )

    # -- availability / billing ---------------------------------------------

    def estimate_cost(self, task: Task) -> CostEstimate:
        """Connector-owned, never inherited from a persisted task estimate."""
        return CostEstimate(
            provider="local",
            model=None,
            minimum_usd=0.0,
            maximum_usd=0.0,
            confidence="local_filesystem_read",
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            billing_evidence="local_read:no_external_call",
            # ``None``, not ``0.0``: there is no provider-side resource
            # ceiling to report, and the canonical monetary validator refuses
            # a non-positive ceiling rather than treating it as "no ceiling".
            resource_ceiling_usd=None,
            requires_paid_api=False,
        )

    # -- resource boundary ---------------------------------------------------

    def resource_boundary(self, task: Task) -> tuple[str, tuple[str, ...]]:
        """The canonical local boundary this read is authorized against.

        Called when the permission request is built AND again immediately
        before execution, so a target that leaves the roots or becomes a
        denied secret file after approval fails closed instead of executing
        under stale authority.

        The resolved target itself is bound separately and more tightly: it
        is ``ActionScope.target``, which ``AgentCore`` canonicalizes through
        ``canonical_path`` for every LEXICAL spelling of a local-read target
        before the scope is read at all (P1-2) - bare relative names included
        - and the scope is part of the attestation and therefore of the grant
        fingerprint.

        P2. That canonicalization does NOT resolve symlinks or junctions, and
        this capability never reads through one: a link in the target chain is
        refused by the no-follow scan in ``_open_in_root`` as itself, so where
        it pointed is never learned. A repoint after approval is therefore
        refused at that boundary rather than by changing the fingerprint.

        The stored ROOT-RELATIVE representation is checked here too, against
        the same single grammar the snapshot contract uses, so a
        credential-bearing or otherwise unrepresentable path name fails
        closed BEFORE any approval request, preview or attestation exists
        (P1-4).
        """
        try:
            working_directory = self._working_directory(task.context)
        except ValueError as exc:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, str(exc)) from exc
        resolved = self._resolved_target(task)
        if resolved is None:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, _NO_TARGET)
        if not self._within_allowed_roots(resolved):
            raise AttestationError(ErrorCode.EGRESS_REFUSED, _OUTSIDE_ROOTS)
        reason = local_read_denial_reason(resolved)
        if reason is not None:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED,
                f"Read target is refused by the secret-file policy: {reason}.",
            )
        reason = local_read_path_refusal(self._relative_path(resolved))
        if reason is not None:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED, f"{_UNREPRESENTABLE_PATH} {reason}."
            )
        return (
            canonical_path(working_directory),
            tuple(canonical_path(root) for root in self.allowed_roots),
        )

    # -- execution -----------------------------------------------------------

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        """The mandatory order, re-checked independently of attestation.

        canonical resolve -> allowed-root containment -> secret deny ->
        representable root-relative name -> exact resolved regular file ->
        open the file through a chain that cannot be re-pointed and prove the
        OPENED object is still in root -> physical ceiling -> binary refusal
        -> bounded representation.

        Every refusal happens before the bytes are read, except the two that
        are *about* the bytes (size, encoding); a denied file is therefore
        never held in memory at all, so its content cannot reach a
        diagnostic, a checkpoint or an event even in principle.
        """
        resolved = self._resolved_target(task)
        if resolved is None:
            return self._refusal(task, _NO_TARGET)
        if not self._within_allowed_roots(resolved):
            return self._refusal(task, _OUTSIDE_ROOTS)
        reason = local_read_denial_reason(resolved)
        if reason is not None:
            return self._refusal(
                task, f"Read target is refused by the secret-file policy: {reason}."
            )
        reason = local_read_path_refusal(self._relative_path(resolved))
        if reason is not None:
            return self._refusal(task, f"{_UNREPRESENTABLE_PATH} {reason}.")
        # P2. There is deliberately NO full-target ``lstat`` here. Naming
        # ``root/a/b/notes.txt`` in one call makes the OS traverse ``a`` and
        # ``b`` to answer, so a junction at either one was followed before the
        # no-follow chain scan had accepted a single component. The identity
        # this used to produce is now taken inside ``_open_in_root``, from the
        # leaf of a chain the scan has already cleared component by component.

        # M9. THREE-WAY and explicit. Written as an if/elif/else with INVALID
        # refused FIRST on purpose: a two-way ``if REGION ... else head``
        # would turn a corrupted region identity into a SUCCESSFUL head read
        # carrying region metadata - the first lines of the file returned
        # under a label claiming lines 1900 onward. The shared contract also
        # refuses a HEAD that carries a window, so neither kind can borrow
        # the other's parameters.
        kind = task.local_read_kind
        reason = local_read_region_refusal(
            kind, task.read_start_line, task.read_max_lines
        )
        if reason is not None:
            return self._refusal(task, f"{_REGION_IDENTITY} {reason}.")

        try:
            if kind is LocalReadKind.REGION:
                raw, opened = self._read_region_file(
                    resolved, task.read_start_line, task.read_max_lines
                )
            else:
                raw, opened = self._read_exact_file(resolved)
        except _NotRegularTarget:
            return self._refusal(task, _NOT_REGULAR)
        except _TargetMoved:
            return self._refusal(task, _TARGET_MOVED)
        except _MultiplyLinked:
            return self._refusal(task, _MULTIPLY_LINKED)
        except _TargetTooLarge:
            return self._refusal(task, _TOO_LARGE)
        except _RegionNotClosed:
            return self._refusal(task, _REGION_NOT_CLOSED)
        except _RegionScanExceeded:
            return self._refusal(task, _REGION_SCAN_EXCEEDED)
        except _RegionPastEof as exc:
            return self._refusal(task, _region_past_eof(exc.total_lines))
        except _NotTextBytes:
            return self._refusal(task, _NOT_TEXT)
        except OSError as exc:
            # A genuine I/O fault, not a policy refusal: retryable, and the
            # message is the structured-error contract's own bounded text.
            return self._refusal(
                task,
                "Read target could not be opened.",
                code=ErrorCode.CONNECTOR_FAILED,
                retryable=True,
                details={"errno": exc.errno if isinstance(exc.errno, int) else 0},
            )

        if b"\x00" in raw:
            return self._refusal(task, _NOT_TEXT)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self._refusal(task, _NOT_TEXT)

        excerpt, truncated = bound_local_text(text)
        # M9. The span describes the BOUNDED EXCERPT, not the raw capture, so
        # it is derived AFTER bounding. That is what makes it honest for
        # every consumer: whatever reaches M3, M8 or a model payload is
        # exactly the span these two numbers name. A line the character cap
        # cut in half still counts, because the reader can see part of it.
        if kind is LocalReadKind.REGION:
            span = excerpt_line_span(excerpt, truncated)
            if span < 1:  # pragma: no cover - an empty region already refused
                return self._refusal(task, _REGION_EMPTY)
            start_line = task.read_start_line
            end_line = start_line + span - 1
        else:
            start_line = 0
            end_line = 0
        # From the path the OPEN HANDLE proved, never from the pathname that
        # was merely validated, so what is persisted names the file that was
        # actually read.
        relative_path = self._relative_path(opened)
        reason = local_read_path_refusal(relative_path)
        if reason is not None:  # pragma: no cover - the pre-read check fired
            return self._refusal(task, f"{_UNREPRESENTABLE_PATH} {reason}.")
        snapshot = LocalReadSnapshot(
            relative_path=relative_path,
            byte_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            excerpt=excerpt,
            truncated=truncated,
            # Deliberately the most restrictive value this connector could
            # possibly claim. AgentCore rebinds it from the reading task's own
            # trusted scope classification, so nothing a connector puts here
            # can establish, raise or lower authority.
            classification=DataClassification.SECRET,
            read_at=utc_now(),
            start_line=start_line,
            end_line=end_line,
        )
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=(
                f"Read {relative_path}: {len(raw)} bytes, "
                f"{len(excerpt)}-character bounded excerpt"
                f"{f' (lines {start_line}-{end_line})' if start_line else ''}"
                f"{' (truncated)' if truncated else ''}."
            ),
            data={LOCAL_READ_RESULT_KEY: snapshot},
            usage=self._usage(task, session),
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        """Verify against the snapshot AgentCore actually bound to the task.

        Not against ``result.data``: that mapping is reduced at the execution
        boundary, so the only durable evidence is the typed task field - which
        is precisely the state a later Claude task would egress. Evidence is
        metadata only; no excerpt text is ever quoted into a verification
        record.
        """
        snapshot = task.local_read
        # The same single structural contract every other boundary uses, so
        # VERIFY cannot pass a snapshot the egress boundaries would refuse.
        if local_read_snapshot_refusal(snapshot) is not None:
            return VerificationRecord(
                status=VerificationStatus.FAILED,
                method="local_read_snapshot_integrity",
                evidence=("no valid bounded local-read snapshot on the task",),
            )
        return VerificationRecord(
            status=VerificationStatus.PASSED,
            method="local_read_snapshot_integrity",
            evidence=(
                f"path={snapshot.relative_path}",
                f"byte_size={snapshot.byte_size}",
                f"sha256={snapshot.sha256}",
                f"truncated={'true' if snapshot.truncated else 'false'}",
            )
            + (
                (f"lines={snapshot.start_line}-{snapshot.end_line}",)
                if snapshot.start_line
                else ()
            ),
        )

    # -- internals -----------------------------------------------------------

    def _read_exact_file(self, resolved: Path) -> tuple[bytes, Path]:
        """Read the file, and prove the bytes came from inside an allowed root.

        Returns the bytes and the AUTHORITATIVE path of the object that was
        actually opened - not the pathname that was validated earlier.

        The final-file replacement check is retained: the fstat of the opened
        descriptor must be the same ``(st_dev, st_ino)`` the pre-open stat
        saw, so a name swapped in that window is refused rather than read
        under an authorization granted for a different file. P2: that
        pre-open stat is no longer taken here from the whole target name -
        ``_open_in_root`` hands it back, taken from the leaf of a chain its
        no-follow scan had already accepted. What that check
        alone cannot prove is that the file was reached through the APPROVED
        DIRECTORY CHAIN - an attacker who re-points an intermediate directory
        before the stat gets a matching device/inode pair for a file outside
        the root - so ``_open_in_root`` establishes the chain independently.

        A hard link defeats all of that by not being a link at all: it is a
        second NAME for the same file, so there is nothing to dereference, no
        reparse point to detect, and the authoritative final path is the
        in-root name it was opened by. Containment is a statement about
        names, and a file with two names is only ever inside the roots by one
        of them. The deliberate tradeoff, taken here for M3: multi-linked
        regular files are REFUSED so strict allowed-root isolation holds
        without exception. Legitimately hardlinked files - git worktrees,
        dedup and package caches - become unreadable, which is an accepted
        compatibility cost for now rather than a permanent decision.
        """
        descriptor, opened, expected = self._open_in_root(resolved)
        with os.fdopen(descriptor, "rb") as handle:
            actual = os.fstat(handle.fileno())
            if not stat.S_ISREG(actual.st_mode) or (
                actual.st_dev,
                actual.st_ino,
            ) != (expected.st_dev, expected.st_ino):
                raise _TargetMoved()
            # Asked of the OPENED regular file only, and before a single byte
            # is read, so no outside content is ever held in memory. Nothing
            # about directory handling changes: a directory legitimately
            # carries several links on some platforms and never reaches here,
            # because the S_ISREG check above already refused it.
            if actual.st_nlink > 1:
                raise _MultiplyLinked()
            # One byte past the ceiling is read on purpose: it is the only way
            # to distinguish "exactly at the ceiling" from "over it" without
            # trusting a stat size that may be stale.
            raw = handle.read(LOCAL_READ_MAX_BYTES + 1)
        if len(raw) > LOCAL_READ_MAX_BYTES:
            raise _TargetTooLarge()
        return raw, opened

    def _read_region_file(
        self,
        resolved: Path,
        start_line: int,
        max_lines: int,
    ) -> tuple[bytes, Path]:
        """Read ONE bounded line region, over the same proven descriptor.

        ``_read_exact_file`` is deliberately left untouched: a head read is
        the same single bounded ``read`` it always was. This is a second,
        separate path that reuses every proof that one relies on - the
        descriptor comes from ``_open_in_root``, and the dev/inode rebinding,
        the regular-file check and the hard-link refusal are re-asked here on
        the OPENED file before a byte is kept. None of those proofs is tied
        to how long the handle stays open; they are anchored to the
        descriptor, which is what makes a streaming read safe.

        Two phases, and the split is the whole design:

        SEEK    read chunks bounded by what is LEFT of the scan ceiling,
                count raw newline bytes, refuse a NUL anywhere in the prefix
                actually traversed, and DISCARD every byte. Nothing is
                decoded here, so locating line 200 000 of a large file costs
                no memory and no decoding.
        CAPTURE from the byte after the (start_line - 1)-th newline, RETAIN
                raw bytes until the region CLOSES - either the max_lines-th
                newline is consumed, or the file ends. Reading stops there;
                the rest of the file is never touched.

        The region succeeds ONLY if it closes at or under the capture
        ceiling. One byte past the ceiling is accepted into the buffer purely
        to PROVE the region is still open - the same trick, for the same
        reason, as the head reader's ``read(LOCAL_READ_MAX_BYTES + 1)``. That
        sentinel byte can never reach content, ``byte_size`` or ``sha256``,
        because reaching it is a refusal.

        Both phases are bounded at the DESCRIPTOR and not only in the
        counters: the handle is unbuffered, and every read asks for at most
        what is left of its phase's ceiling. So the PHYSICAL bytes this
        function pulls off the file are at most
        LOCAL_READ_REGION_SCAN_MAX_BYTES while seeking and at most
        LOCAL_READ_MAX_BYTES + 1 while capturing.

        Nothing is decoded until the region has closed, which is what makes
        the strict UTF-8 rule sound: a successful region ends at a newline or
        at EOF, and both are code-point boundaries in a valid UTF-8 file, so
        an arbitrary ceiling byte is never handed to the decoder.
        """
        descriptor, opened, expected = self._open_in_root(resolved)
        closed: str | None = None
        captured = bytearray()
        # Unbuffered ON PURPOSE, and only here (P2-2). A buffered reader
        # prefetches whole blocks, so a logical one-byte read for the
        # sentinel pulls kilobytes off the descriptor: the accounting below
        # would sit inside its ceilings while the FILE DESCRIPTOR had already
        # read past them. With ``buffering=0`` one ``read(n)`` is one bounded
        # descriptor read, which is what makes both bounds true of the
        # descriptor rather than of a counter - SEEK fetches at most
        # LOCAL_READ_REGION_SCAN_MAX_BYTES, CAPTURE at most
        # LOCAL_READ_MAX_BYTES + 1. The head reader is untouched: it issues a
        # single bounded read and has never had a prefetch to bound.
        with os.fdopen(descriptor, "rb", buffering=0) as handle:
            actual = os.fstat(handle.fileno())
            if not stat.S_ISREG(actual.st_mode) or (
                actual.st_dev,
                actual.st_ino,
            ) != (expected.st_dev, expected.st_ino):
                raise _TargetMoved()
            if actual.st_nlink > 1:
                raise _MultiplyLinked()

            scanned = 0
            newlines = 0
            last_byte: int | None = None
            pending = b""
            while newlines < start_line - 1:
                # Bounded BEFORE the read, never audited after it (P2-1):
                # asking for a whole chunk and only then noticing the ceiling
                # was passed has already pulled those bytes off the
                # descriptor, so the declared scan bound would describe a
                # counter rather than the file.
                remaining = LOCAL_READ_REGION_SCAN_MAX_BYTES - scanned
                if remaining <= 0:
                    raise _RegionScanExceeded()
                chunk = handle.read(min(_REGION_CHUNK_BYTES, remaining))
                if not chunk:
                    # The canonical line rule: no lines in an empty file,
                    # otherwise one per newline plus a final unterminated one.
                    total = (
                        newlines
                        if (scanned == 0 or last_byte == 0x0A)
                        else newlines + 1
                    )
                    raise _RegionPastEof(total)
                scanned += len(chunk)
                last_byte = chunk[-1]
                cursor = 0
                while newlines < start_line - 1:
                    index = chunk.find(b"\n", cursor)
                    if index < 0:
                        cursor = len(chunk)
                        break
                    newlines += 1
                    cursor = index + 1
                # The scanned prefix is what was actually TRAVERSED to reach
                # the boundary (P2-3) - ``cursor``, not the whole chunk.
                # Bytes past the boundary are the first bytes of the region,
                # not part of the seek, and CAPTURE judges them; checking
                # them here would let a NUL OUTSIDE the requested region
                # refuse a read it has nothing to do with.
                if b"\x00" in chunk[:cursor]:
                    raise _NotTextBytes()
                if newlines == start_line - 1:
                    pending = chunk[cursor:]

            ceiling = LOCAL_READ_MAX_BYTES + 1
            region_lines = 0
            while closed is None:
                if not pending:
                    room = ceiling - len(captured)
                    if room <= 0:
                        break
                    chunk = handle.read(min(_REGION_CHUNK_BYTES, room))
                    if not chunk:
                        closed = "eof"
                        break
                    pending = chunk
                room = ceiling - len(captured)
                take = pending[:room]
                pending = pending[len(take) :]
                cursor = 0
                consumed: int | None = None
                while region_lines < max_lines:
                    index = take.find(b"\n", cursor)
                    if index < 0:
                        break
                    region_lines += 1
                    cursor = index + 1
                    if region_lines == max_lines:
                        consumed = cursor
                        closed = "lines"
                        break
                # The region's own bytes and nothing past its closing
                # newline (P2-3). Once the region has CLOSED, the rest of the
                # fetched chunk is not part of this read at all, so it can
                # neither be captured nor refuse the capture; while the
                # region is still open every byte taken IS region content and
                # is checked.
                window = take if consumed is None else take[:consumed]
                if b"\x00" in window:
                    raise _NotTextBytes()
                captured.extend(window)
                if consumed is None and len(captured) >= ceiling:
                    break

        if closed is None:
            raise _RegionNotClosed()
        raw = bytes(captured)
        if len(raw) > LOCAL_READ_MAX_BYTES:
            raise _RegionNotClosed()
        if not raw:
            # The seek found start_line - 1 newlines and nothing followed, so
            # the requested first line does not exist.
            raise _RegionPastEof(start_line - 1)
        return raw, opened

    def _open_in_root(self, resolved: Path) -> tuple[int, Path, os.stat_result]:
        """Open ``resolved`` so that containment holds AT OPEN TIME (P1-1).

        A pre-open ``resolve()`` proves a pathname, not an object: any parent
        component can be replaced by a symlink or an NTFS junction in the
        window between validation and open, and the opened file then never
        traversed the approved chain at all. Neither ``O_NOFOLLOW`` (final
        component only, and absent on Windows) nor the device/inode binding
        catches that, because after such a swap both see the same post-swap
        file and agree with each other.

        Two platform-appropriate routes, one guarantee:

        * where the platform can open a name relative to an open DIRECTORY
          HANDLE, the chain below the root is walked handle by handle with
          ``O_NOFOLLOW``. Each step is pinned to the directory object the
          previous step opened, so no component can be re-pointed underneath
          it, and a swapped component fails the open outright. Nothing
          outside the root is opened, not even transiently.
        * on Windows there is no ``dir_fd``, so the chain is first walked with
          no-follow ``lstat`` and refused if ANY component below the root is a
          reparse point (a junction needs no privilege, which is exactly why
          ``S_ISLNK`` is not the question), and the opened handle is then
          asked for its own authoritative final path via
          ``GetFinalPathNameByHandleW``. That answer comes from the OBJECT,
          not from a pathname, so a chain swapped after the scan still lands
          outside the roots, is refused, and yields zero bytes.

        The returned path is the authoritative one on both routes, and the
        returned ``stat_result`` is the leaf's, taken from the accepted chain.

        P2. Nothing above this scan may touch the target. ``execute`` used to
        ``lstat`` the WHOLE target name first, to learn "is it a regular
        file" and to bind the identity the post-open ``fstat`` is compared
        against - but naming ``root/a/b/leaf`` in one call makes the OS
        traverse ``a`` and ``b``, so a junction at either was followed before
        this scan refused it. Both facts are taken here instead, from the
        leaf's own no-follow ``lstat``, which the scan had to fetch anyway.
        The refusals are unchanged: a missing component, a leaf that is a
        directory, and a leaf that is a symlink or reparse point are all
        still "not an existing regular file", and the link is still never
        followed to learn where it went.
        """
        root = self._containing_root(resolved)
        if root is None:
            raise _TargetMoved()
        parts = resolved.relative_to(root).parts
        if not parts:
            raise _TargetMoved()
        # Deliberately run on BOTH routes: redundant where directory handles
        # already pin the chain, and the only PRE-open defence where they do
        # not. It is also the one seam a coordinated-race test can drive.
        try:
            expected = self._reject_reparse_chain(root, parts)
        except (_ChainMissing, _LeafReparse) as exc:
            # The target itself is absent, unreachable, or a link. All three
            # are "not an existing regular file" - and a link is refused AS
            # ITSELF here, so where it pointed is never learned.
            raise _NotRegularTarget() from exc
        if not stat.S_ISREG(expected.st_mode):
            raise _NotRegularTarget()
        if _DIRECTORY_HANDLE_TRAVERSAL:
            # Proven by construction: every step below the bound root was
            # opened no-follow relative to the previous handle.
            descriptor, opened = self._open_by_directory_handles(root, parts), resolved
        else:
            descriptor = os.open(resolved, _READ_FLAGS)
            opened = _final_path_from_handle(descriptor)
        # Containment and the file policy are re-asked of the AUTHORITATIVE
        # path on BOTH routes, so neither one can be the weaker of the two.
        try:
            if opened is None:
                raise _TargetMoved()
            if not self._within_allowed_roots(opened):
                raise _TargetMoved()
            if local_read_denial_reason(opened) is not None:
                raise _TargetMoved()
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, opened, expected

    def _open_by_directory_handles(self, root: Path, parts: tuple[str, ...]) -> int:
        """Walk root -> file one no-follow step at a time, handle to handle.

        The walk is only as trustworthy as the directory it starts from, and
        that first ``os.open`` is the one step still taken BY PATHNAME. So the
        opened root is bound to the authorized root object before a single
        component below it is traversed: ``O_NOFOLLOW`` refuses a root path
        whose final component became a symlink, and the ``fstat`` of the
        handle must be the ``(st_dev, st_ino)`` recorded when the authority
        set was created. Only then does every subsequent no-follow,
        handle-relative step actually prove that ``root / parts`` is the file
        that was opened.
        """
        identity = self._root_identity.get(root)
        if identity is None:
            raise _TargetMoved()
        directory_flags = _READ_FLAGS | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(root, directory_flags)
        try:
            opened_root = os.fstat(parent)
            if (opened_root.st_dev, opened_root.st_ino) != identity:
                raise _TargetMoved()
            for name in parts[:-1]:
                child = os.open(name, directory_flags, dir_fd=parent)
                os.close(parent)
                parent = child
            return os.open(parts[-1], _READ_FLAGS | os.O_NOFOLLOW, dir_fd=parent)
        except OSError as exc:
            # ELOOP is a component that became a symlink, ENOTDIR one that
            # stopped being a directory, ENOENT one that vanished: all three
            # are the race, not an I/O fault, and read as "the target moved".
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                raise _TargetMoved() from exc
            raise
        finally:
            os.close(parent)
