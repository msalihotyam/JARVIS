"""Brain read-only file tools backend (BRAINFS1).

The brain sees the repo read-only through these functions; this code is the
boundary, not CLI settings. Rule: validate the raw request (suspicious form =
refuse, before any file system call), resolve, then require the real path to
stay inside the real root and to hit no refused name. Every public call
returns a value or a Refusal; no exception escapes. Refusal texts are ASCII
constants and never echo the request.

Out of scope (stated): a secret hidden in a file with an allowed name and no
recognised header (a SQL text dump, a raw key pasted into a .txt). The name
list plus the SQLite/PEM header sniff is the boundary. Between resolve and
open a link could be swapped in (TOCTOU); the brain cannot write, so it cannot
plant one itself.

Nothing in the running app imports this module yet.
"""

import fnmatch
import os
import stat
import time
import unicodedata
from typing import NamedTuple

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
    stopped: str  # "" | "files" | "matches" | "time"


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
    """Read-only view of one root. realpath / is_link / clock are injectable ports."""

    def __init__(self, root, *, realpath=None, is_link=None, clock=None):
        self._realpath = realpath or os.path.realpath
        self._is_link = is_link or _is_link
        self._clock = clock or time.monotonic
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
        if os.path.normcase(real) != os.path.normcase(candidate):
            raise _Refuse("link", "path resolves to another place")
        return Resolved(real, "/".join(rel_segs))

    def _read_text(self, real: str) -> str:
        with open(real, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise _Refuse("not_file", "path is not a regular file")
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
            with os.scandir(target.path) as scan:
                for entry in scan:
                    if not _name_ok(parent, entry.name) or self._is_link(entry.path):
                        continue
                    kind = "dir" if entry.is_dir(follow_symlinks=False) else "file"
                    entries.append(Entry(entry.name, kind))
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
        matches, scanned = [], 0
        stack = [start.rel.split("/") if start.rel else []]
        while stack:
            segs = stack.pop()
            parent = [_fold(s) for s in segs]
            try:
                with os.scandir(os.path.join(self._root, *segs)) as scan:
                    names = sorted((e.name, e.is_dir(follow_symlinks=False)) for e in scan)
            except OSError:
                continue
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
        return SearchResult(tuple(matches), scanned, "")


def resolve(root, requested):
    """Module-level form of BrainFiles(root).resolve(requested)."""
    return BrainFiles(root).resolve(requested)
