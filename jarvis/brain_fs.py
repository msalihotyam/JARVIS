"""Brain read-only file tools backend (BRAINFS1).

The brain sees the repo read-only through these functions; this code is the
boundary, not CLI settings. Rule: validate the raw request (suspicious form =
refuse, before any file system call), resolve, then require the real path to
stay inside the real root and to hit no refused name. Every public call
returns a value or a Refusal; no exception escapes. Refusal texts are ASCII
constants and never echo the request.

Out of scope (stated): a secret hidden in a file with an allowed name and no
recognised header (a SQL text dump, a raw key pasted into a .txt). The name
list plus the SQLite/PEM header sniff is the boundary.

Swap between resolve and open (TOCTOU): every opened file or directory handle
is checked again by its final path (GetFinalPathNameByHandleW on Windows,
/proc/self/fd on Linux), which must be the resolved path inside the root and
hit no refused name; else refuse. Directory entries are then read from that
same handle (never by path again), so a junction swapped in after the check
is not followed. On Windows the handles do not share delete, so the opened
path cannot be renamed while it is read.

Nothing in the running app imports this module yet.
"""

import fnmatch
import os
import stat
import time
import unicodedata
from typing import NamedTuple

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _K32.CreateFileW.restype = wintypes.HANDLE
    _K32.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    _K32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _K32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _K32.CloseHandle.restype = wintypes.BOOL
    _K32.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _K32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

# One constant: each entry is a run of consecutive folded segments (glob per
# segment), matched at any depth (worktrees under .claude have their own data/).
REFUSED_NAMES = (
    ("data",),
    (".git",),
    (".env*",),
    ("*.key",),
    ("*.pem",),
    ("*.pfx",),
    ("*.db",),
    ("*.sqlite*",),
    ("*-wal",),
    ("*-shm",),
    ("*-journal",),
    ("talk", "state"),
)

MAX_PATH_CHARS = 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_LINES = 2000
MAX_TEXT_CHARS = 256 * 1024
MAX_ENTRIES = 1000
MAX_PATTERN_CHARS = 200
MAX_SEARCH_FILES = 2000
MAX_MATCHES = 200
MAX_SEARCH_SECONDS = 5.0
MAX_LINE_CHARS = 300

_DEVICES = frozenset(
    ["con", "prn", "aux", "nul", "conin$", "conout$", "clock$"]
    + [prefix + digit for prefix in ("com", "lpt") for digit in "0123456789¹²³"]
)
# ~ (8.3 short names, home), % and $ (environment forms), : (drives, streams).
_BAD_CHARS = frozenset('\\/:<>"|?*~%$')
_BAD_CATEGORIES = frozenset(("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp", "Zs"))
_IGNORABLE = frozenset(
    [0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x2D7F, 0x3164, 0xFFA0, 0xFFFC, 0x1D159]
    + list(range(0x180B, 0x1810)) + list(range(0x200B, 0x2010)) + list(range(0x202A, 0x202F))
    + list(range(0x2060, 0x2070)) + list(range(0xFE00, 0xFE10)) + [0xFEFF] + list(range(0xFFF0, 0xFFF9))
    + list(range(0x1BCA0, 0x1BCA4)) + list(range(0x1D173, 0x1D17B)) + list(range(0xE0000, 0xE1000))
)
_SQLITE_HEADER = b"SQLite format 3\x00"
_PEM_HEADER = b"-----BEGIN"


class Refusal(NamedTuple):
    code: str
    reason: str


class FileText(NamedTuple):
    text: str
    offset: int
    lines: int
    total_lines: int
    truncated: bool


class Entry(NamedTuple):
    name: str
    kind: str  # "dir" | "file"


class Listing(NamedTuple):
    entries: tuple
    truncated: bool


class Match(NamedTuple):
    path: str
    line: int
    text: str


class SearchResult(NamedTuple):
    matches: tuple
    files_scanned: int
    stopped: str  # "" | "files" | "matches" | "time" | "entries" (a directory was cut at MAX_ENTRIES)


class _Refuse(Exception):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.refusal = Refusal(code, reason)


def _is_link(path: str) -> bool:
    """Default stat port: symlink, junction or any reparse point."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # cannot tell -> treat as a link (fail closed)
    attrs = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _final_path(fd: int) -> str:
    """Default final-path port: the path the open handle really points at."""
    if os.name != "nt":
        # shortcut: /proc is Linux only; elsewhere the path never matches, so every
        # open refuses (fail closed); add F_GETPATH when macOS matters.
        return os.path.realpath(f"/proc/self/fd/{fd}")
    buf = ctypes.create_unicode_buffer(32768)
    size = _K32.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(fd), buf, len(buf), 0)
    if size == 0 or size >= len(buf):
        raise ctypes.WinError(ctypes.get_last_error())
    final = buf.value
    if final.startswith("\\\\?\\UNC\\"):
        return "\\\\" + final[8:]
    if final.startswith("\\\\?\\"):
        return final[4:]
    return final


def _open_dir(path: str) -> int:
    """Read-only fd on a directory (Windows: no delete sharing, so it is pinned)."""
    if os.name != "nt":
        return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    # FILE_LIST_DIRECTORY|FILE_READ_ATTRIBUTES, share read|write, OPEN_EXISTING, BACKUP_SEMANTICS.
    handle = _K32.CreateFileW(path, 0x81, 0x3, None, 3, 0x02000000, None)
    if handle is None or handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        _K32.CloseHandle(handle)
        raise


def _scan_fd(fd: int):
    """(name, is_dir) of the directory the fd holds, read from the handle itself;
    links and reparse points come as (name, None) so callers still count them
    toward the deadline, and must skip them (fail closed)."""
    if os.name != "nt":
        with os.scandir(fd) as scan:
            for entry in scan:
                yield entry.name, None if entry.is_symlink() else entry.is_dir(follow_symlinks=False)
        return
    handle, buf, info_class = msvcrt.get_osfhandle(fd), ctypes.create_string_buffer(65536), 11
    # FileIdBothDirectoryRestartInfo (11) first, then FileIdBothDirectoryInfo (10).
    while _K32.GetFileInformationByHandleEx(handle, info_class, buf, len(buf)):
        info_class, raw, offset = 10, buf.raw, 0
        while True:  # FILE_ID_BOTH_DIR_INFO: attrs @56, name bytes @60, name @104
            step = int.from_bytes(raw[offset:offset + 4], "little")
            attrs = int.from_bytes(raw[offset + 56:offset + 60], "little")
            size = int.from_bytes(raw[offset + 60:offset + 64], "little")
            name = raw[offset + 104:offset + 104 + size].decode("utf-16-le", "surrogatepass")
            if name not in (".", ".."):  # FILE_ATTRIBUTE_REPARSE_POINT (0x400) -> None
                yield name, None if attrs & 0x400 else bool(attrs & 0x10)
            if not step:
                break
            offset += step
    if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
        raise ctypes.WinError(ctypes.get_last_error())


def _fold(text: str) -> str:
    # upper() first so NTFS-style upcase pairs (dotless i -> I) fold together too.
    text = unicodedata.normalize("NFKC", text)
    return unicodedata.normalize("NFKC", text.upper().casefold())


def _refused_name(folded: list) -> bool:
    for rule in REFUSED_NAMES:
        width = len(rule)
        for start in range(len(folded) - width + 1):
            if all(fnmatch.fnmatchcase(folded[start + i], rule[i]) for i in range(width)):
                return True
    return False


def _bad_form(seg: str) -> str:
    """Reason when one segment (raw or folded) is a suspicious form, else ""."""
    if seg in ("", ".", ".."):
        return "empty or dot segment"
    if seg != seg.strip(" ") or seg.endswith("."):
        return "segment starts or ends in space or ends in dot"
    for ch in seg:
        if ch in _BAD_CHARS:
            return "segment has a separator, drive, stream, short name or variable character"
        if ord(ch) in _IGNORABLE or unicodedata.category(ch) in _BAD_CATEGORIES and ch != " ":
            return "segment has a control, bidi or zero-width character"
    if seg.split(".")[0].rstrip(" ") in _DEVICES:
        return "segment is a reserved device name"
    return ""


class Resolved(NamedTuple):
    path: str  # real absolute path, inside the real root
    rel: str  # root-relative, "/" separated ("" = the root itself)


def _int_arg(value, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _Refuse("bad_argument", "argument is not an int in the allowed range")
    return value


def _check_request(requested) -> list:
    """Raw segments of a root-relative request; refuses every suspicious form
    before any file system call. "" names the root itself; "/" is the only separator."""
    if not isinstance(requested, str):
        raise _Refuse("type", "path is not text")
    if len(requested) > MAX_PATH_CHARS:  # raw length, before NFKC can expand it
        raise _Refuse("too_long", "path is too long")
    if "\0" in requested:
        raise _Refuse("nul", "path contains NUL")
    if requested == "":
        return []
    if "\\" in requested:
        raise _Refuse("form", "backslash, UNC, device or mixed separator form")
    if requested.startswith("/"):
        raise _Refuse("form", "absolute path; paths are root-relative")
    if unicodedata.normalize("NFC", requested) != requested:
        raise _Refuse("form", "path is not NFC")
    segs = requested.split("/")
    folded = []
    for seg in segs:
        fold = _fold(seg)
        for form in (seg, fold):
            why = _bad_form(form)
            if why:
                raise _Refuse("form", why)
        folded.append(fold)
    if _refused_name(folded):
        raise _Refuse("refused_name", "path is in a refused area")
    return segs


def _name_ok(parent_folded: list, name: str) -> bool:
    """A directory entry the brain may see: clean form and not a refused name."""
    fold = _fold(name)
    if _bad_form(name) or _bad_form(fold):
        return False
    return not _refused_name(parent_folded + [fold])


class BrainFiles:
    """Read-only view of one root. realpath / is_link / clock / final_path (fd -> path
    of an open handle) are injectable ports."""

    def __init__(self, root, *, realpath=None, is_link=None, clock=None, final_path=None):
        self._realpath = realpath or os.path.realpath
        self._is_link = is_link or _is_link
        self._clock = clock or time.monotonic
        self._final_path = final_path or _final_path
        self._root = None
        self._root_refusal = None
        try:
            if not isinstance(root, str) or not root or len(root) > MAX_PATH_CHARS or "\0" in root:
                raise _Refuse("bad_root", "root is not a usable path")
            if not os.path.isabs(root):
                raise _Refuse("bad_root", "root is not absolute")
            real = self._realpath(root)
            if not isinstance(real, str) or not os.path.isdir(real):
                raise _Refuse("bad_root", "root is not a directory")
            self._root = real
        except _Refuse as exc:
            self._root_refusal = exc.refusal
        except Exception:  # noqa: BLE001 - the constructor never raises either
            self._root_refusal = Refusal("bad_root", "root could not be resolved")

    def _guard(self, func, *args):
        if self._root_refusal is not None:
            return self._root_refusal
        try:
            return func(*args)
        except _Refuse as exc:
            return exc.refusal
        except Exception:  # noqa: BLE001 - no exception escapes; refuse instead
            return Refusal("internal", "internal error")

    def _resolve(self, segs: list) -> Resolved:
        root = self._root
        candidate = os.path.join(root, *segs) if segs else root
        prefix = root
        for seg in segs:
            prefix = os.path.join(prefix, seg)
            if self._is_link(prefix):
                raise _Refuse("link", "path goes through a link or junction")
        real = self._realpath(candidate)
        if not isinstance(real, str):
            raise _Refuse("internal", "resolver returned no path")
        rel_segs = self._place(real)
        if os.path.normcase(real) != os.path.normcase(candidate):
            raise _Refuse("link", "path resolves to another place")
        return Resolved(real, "/".join(rel_segs))

    def _place(self, real: str) -> list:
        """Root-relative segments of a real path; refuses outside or refused areas."""
        root = self._root
        try:
            inside = os.path.commonpath([os.path.normcase(root), os.path.normcase(real)])
        except ValueError:  # another drive or a mix of absolute and relative
            raise _Refuse("outside", "real path is outside the root") from None
        if inside != os.path.normcase(root):
            raise _Refuse("outside", "real path is outside the root")
        rel = os.path.relpath(real, root)
        rel_segs = [] if rel == os.curdir else rel.split(os.sep)
        if _refused_name([_fold(s) for s in rel_segs]):
            raise _Refuse("refused_name", "real path is in a refused area")
        return rel_segs

    def _verify(self, fd: int, expected: str) -> None:
        """The open handle must still be the resolved path (closes the TOCTOU window)."""
        try:
            final = self._final_path(fd)
        except Exception:  # noqa: BLE001 - cannot verify -> refuse (fail closed)
            raise _Refuse("link", "opened path could not be verified") from None
        if not isinstance(final, str) or not final or "\0" in final:
            raise _Refuse("link", "opened path could not be verified")
        self._place(final)
        if os.path.normcase(final) != os.path.normcase(expected):
            raise _Refuse("link", "opened path is another place")

    def _read_text(self, real: str) -> str:
        # Nonblocking open: a POSIX FIFO without a writer must not hang before the S_ISREG check.
        # O_NOFOLLOW/O_NOCTTY: a final component swapped to a link or a tty is never opened with side effects.
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0))
        try:
            handle = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            self._verify(handle.fileno(), real)
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise _Refuse("not_file", "path is not a regular file")
            if info.st_nlink != 1:  # a hard link aliases a file outside or excluded under an allowed name
                raise _Refuse("link", "file has more than one link")
            if info.st_size > MAX_FILE_BYTES:
                raise _Refuse("too_large", "file is over the size cap")
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise _Refuse("too_large", "file is over the size cap")
        head = data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
        if data.startswith(_SQLITE_HEADER) or head.startswith(_PEM_HEADER):
            raise _Refuse("secret_header", "file starts with a database or key header")
        if b"\0" in data:
            raise _Refuse("binary", "file is binary")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise _Refuse("binary", "file is not UTF-8 text") from None

    def resolve(self, requested):
        """Resolved(real path, rel) or Refusal."""
        return self._guard(lambda: self._resolve(_check_request(requested)))

    def read_file(self, path, offset=0, limit=MAX_LINES):
        """FileText for lines [offset, offset+limit) or Refusal."""
        return self._guard(self._read_file, path, offset, limit)

    def _read_file(self, path, offset, limit) -> FileText:
        offset = _int_arg(offset, 0, 10**9)
        limit = _int_arg(limit, 1, MAX_LINES)
        target = self._resolve(_check_request(path))
        try:
            text = self._read_text(target.path)
        except OSError:
            raise _Refuse("io", "file could not be read") from None
        lines = text.splitlines(keepends=True)
        chunk = "".join(lines[offset:offset + limit])
        truncated = len(chunk) > MAX_TEXT_CHARS or offset + limit < len(lines)
        chunk = chunk[:MAX_TEXT_CHARS]
        return FileText(chunk, offset, min(limit, max(len(lines) - offset, 0)), len(lines), truncated)

    def list_dir(self, path=""):
        """Listing of allowed names, or Refusal (a refused folder is never an empty list)."""
        return self._guard(self._list_dir, path)

    def _list_dir(self, path) -> Listing:
        target = self._resolve(_check_request(path))
        parent = [_fold(s) for s in target.rel.split("/")] if target.rel else []
        entries = []
        try:
            fd = _open_dir(target.path)
            try:
                self._verify(fd, target.path)
                for name, is_dir in _scan_fd(fd):
                    if is_dir is None or not _name_ok(parent, name) or self._is_link(os.path.join(target.path, name)):
                        continue
                    entries.append(Entry(name, "dir" if is_dir else "file"))
            finally:
                os.close(fd)
        except NotADirectoryError:
            raise _Refuse("not_dir", "path is not a directory") from None
        except OSError:
            raise _Refuse("io", "directory could not be listed") from None
        entries.sort()
        return Listing(tuple(entries[:MAX_ENTRIES]), len(entries) > MAX_ENTRIES)

    def search(self, pattern, path=""):
        """Literal substring search over allowed text files; SearchResult or Refusal."""
        return self._guard(self._search, pattern, path)

    def _search(self, pattern, path) -> SearchResult:
        # Literal matcher (str.find is linear for long needles): no regex, so no
        # catastrophic backtracking; the clock budget bounds the walk as well.
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_CHARS:
            raise _Refuse("pattern", "pattern is not text of the allowed length")
        if "\0" in pattern or "\n" in pattern or "\r" in pattern:
            raise _Refuse("pattern", "pattern has NUL or a line break")
        start = self._resolve(_check_request(path))
        deadline = self._clock() + MAX_SEARCH_SECONDS
        matches, scanned, cut = [], 0, False
        stack = [start.rel.split("/") if start.rel else []]
        while stack:
            segs = stack.pop()
            parent = [_fold(s) for s in segs]
            dir_path = os.path.join(self._root, *segs)
            names = []
            try:
                fd = _open_dir(dir_path)
                try:
                    self._verify(fd, dir_path)
                    for entry in _scan_fd(fd):  # deadline and cap per entry: no full listing first
                        if self._clock() > deadline:
                            return SearchResult(tuple(matches), scanned, "time")
                        if entry[1] is None:  # link or reparse point: timed above, never followed
                            continue
                        if len(names) >= MAX_ENTRIES:
                            cut = True
                            break
                        names.append(entry)
                finally:
                    os.close(fd)
            except (_Refuse, OSError):
                continue  # swapped, refused or unreadable directories are skipped
            names.sort()
            subdirs = []
            for name, is_dir in names:
                if self._clock() > deadline:
                    return SearchResult(tuple(matches), scanned, "time")
                if not _name_ok(parent, name):
                    continue
                if is_dir:
                    if not self._is_link(os.path.join(self._root, *segs, name)):  # junctions too
                        subdirs.append(segs + [name])
                    continue
                if scanned >= MAX_SEARCH_FILES:
                    return SearchResult(tuple(matches), scanned, "files")
                scanned += 1
                try:
                    target = self._resolve(segs + [name])
                    text = self._read_text(target.path)
                except (_Refuse, OSError):
                    continue  # refused, link, binary or unreadable files are skipped
                for number, line in enumerate(text.splitlines(), 1):
                    if pattern in line:
                        matches.append(Match(target.rel, number, line[:MAX_LINE_CHARS]))
                        if len(matches) >= MAX_MATCHES:
                            return SearchResult(tuple(matches), scanned, "matches")
            stack.extend(reversed(subdirs))
        return SearchResult(tuple(matches), scanned, "entries" if cut else "")


def resolve(root, requested):
    """Module-level form of BrainFiles(root).resolve(requested)."""
    return BrainFiles(root).resolve(requested)
