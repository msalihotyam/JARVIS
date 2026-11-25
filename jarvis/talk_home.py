"""Home folder of the conversation service and its single-instance lock (TALKHOME1).

The caller names the root (real: <repo>/talk; tests: a temporary folder).
Everything this module creates or opens lives under that root: log,
attachments, inbox and state folders, and state/talk.lock. The root is
checked lexically before any filesystem call, so a root under data/ or
~/.jarvis is refused without ever being resolved or opened; the module never
expands "~" or reads the environment.

Stdlib only; nothing in the running app imports it yet. Every public function
returns a Refusal instead of raising (fail closed). Ports are injected:
  stat   lstat-like callable (default os.lstat), used to spot links/junctions
  alive  callable(pid) -> bool; only a literal False means the holder is dead
"""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import secrets
import stat as stat_mod
from contextlib import contextmanager
from dataclasses import dataclass

if os.name == "nt":
    import msvcrt
else:
    import fcntl

FOLDERS = ("log", "attachments", "inbox", "state")
LOCK_NAME = "talk.lock"
GUARD_NAME = LOCK_NAME + ".guard"
MAX_LOCK_BYTES = 512
MAX_ROOT_CHARS = 200
INTERRUPTED = "previous run interrupted"

_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions are not S_ISLNK
_BOOT_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_TOKEN = re.compile(r"[0-9a-f]{32}")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{p}{n}" for p in ("COM", "LPT") for n in "0123456789\u00b9\u00b2\u00b3"
}
_PROTECTED_PARTS = {"data", ".jarvis"}
_BAD_CHARS = set('<>"|?*:~/\\')  # inside one part: no separator, stream, 8.3 or wildcard
_KEYS = {"version", "boot_id", "pid", "since", "token"}

# Fixed ASCII texts; only HELD appends the holder, whose fields are validated
# to a tight ASCII charset before they are echoed.
REFUSALS = {
    "ROOT_INVALID": "root must be an absolute local folder path without traversal or special names",
    "ROOT_PROTECTED": "root must not be inside data or .jarvis",
    "ROOT_NOT_FOLDER": "root exists but is not a plain folder",
    "ROOT_PARENT_MISSING": "the parent folder of root does not exist",
    "FOLDER_NOT_FOLDER": "a home folder exists but is not a plain folder",
    "LINK_REFUSED": "a home path is a link or junction",
    "BOOT_ID_INVALID": "boot id must be 1-64 chars of letters, digits, dot, dash or underscore",
    "PID_INVALID": "pid must be an integer above zero",
    "NOW_INVALID": "now must be a finite number at or above zero",
    "ALIVE_INVALID": "alive must be a callable liveness check",
    "TOKEN_INVALID": "token is missing or malformed",
    "HELD": "lock held by",
    "UNKNOWN_HOLDER": "lock file is unreadable; unknown holder, operator reset required",
    "NO_LOCK": "no lock is held",
    "WRONG_TOKEN": "token does not match the lock holder",
    "RACE_LOST": "another instance took the lock first",
    "LOCK_BUSY": "lock file is in use by another process",
    "IO_ERROR": "home folder could not be read or written",
    "INPUT_TOO_DEEP": "input is nested too deeply",
    "INPUT_INVALID": "input could not be read",
}


@dataclass(frozen=True)
class Refusal:
    code: str
    text: str


@dataclass(frozen=True)
class Layout:
    root: str
    log: str
    attachments: str
    inbox: str
    state: str


@dataclass(frozen=True)
class Lease:
    token: str
    lock_path: str
    note: str  # INTERRUPTED after a stale takeover, else ""


class _Refused(Exception):
    def __init__(self, code, extra=""):
        super().__init__(code)
        self.refusal = Refusal(code, REFUSALS[code] + extra)


def _guarded(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _Refused as refused:
            return refused.refusal
        except RecursionError:
            return Refusal("INPUT_TOO_DEEP", REFUSALS["INPUT_TOO_DEEP"])
        except OSError as err:
            if getattr(err, "winerror", None) in (32, 33):  # sharing/lock violation: someone has it open
                return Refusal("LOCK_BUSY", REFUSALS["LOCK_BUSY"])
            return Refusal("IO_ERROR", REFUSALS["IO_ERROR"])
        except Exception:  # fail closed: no exception escapes a public call
            return Refusal("INPUT_INVALID", REFUSALS["INPUT_INVALID"])

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _check_root(root):
    """Lexical only: no filesystem call happens before this passes."""
    if not isinstance(root, str) or not root or len(root) > MAX_ROOT_CHARS:
        raise _Refused("ROOT_INVALID")
    if not root.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in root):
        raise _Refused("ROOT_INVALID")
    if os.name == "nt":
        if not re.fullmatch(r"[A-Za-z]:\\.+", root):
            raise _Refused("ROOT_INVALID")  # also refuses UNC, \\?\, \\.\ and drive-relative
        parts = root[3:].split("\\")
    else:
        if not root.startswith("/") or root.startswith("//"):
            raise _Refused("ROOT_INVALID")
        parts = root[1:].split("/")
    protected = False
    for part in parts:
        if part in ("", ".", "..") or part[-1] in ". " or _BAD_CHARS & set(part):
            raise _Refused("ROOT_INVALID")
        if part.split(".")[0].rstrip(" ").upper() in _RESERVED:  # "NUL .txt" is NUL too
            raise _Refused("ROOT_INVALID")
        protected |= part.casefold() in _PROTECTED_PARTS
    if protected:
        raise _Refused("ROOT_PROTECTED")


def _plain_folder(path, stat, missing_code, file_code):
    """True if path is a plain folder, False if missing; refuse files and links."""
    try:
        st = stat(path)
    except FileNotFoundError:
        if missing_code:
            raise _Refused(missing_code) from None
        return False
    if stat_mod.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        raise _Refused("LINK_REFUSED")
    if not stat_mod.S_ISDIR(st.st_mode):
        raise _Refused(file_code)
    return True


def _layout(root):
    _check_root(root)
    return Layout(root, *(os.path.join(root, name) for name in FOLDERS))


@_guarded
def layout(root):
    """Paths of the home folders under root (no filesystem access)."""
    return _layout(root)


def _ensure(root, stat):
    home = _layout(root)
    if not _plain_folder(root, stat, None, "ROOT_NOT_FOLDER"):
        _plain_folder(os.path.dirname(root), stat, "ROOT_PARENT_MISSING", "ROOT_PARENT_MISSING")
        os.mkdir(root)  # one level only: nothing is created outside the root
        _plain_folder(root, stat, "IO_ERROR", "ROOT_NOT_FOLDER")
    for name in FOLDERS:
        path = getattr(home, name)
        if not _plain_folder(path, stat, None, "FOLDER_NOT_FOLDER"):
            os.mkdir(path)
            _plain_folder(path, stat, "IO_ERROR", "FOLDER_NOT_FOLDER")
    return home


@_guarded
def ensure(root, stat=os.lstat):
    """Create root (one level) and the home folders; refuse files and links."""
    return _ensure(root, stat)


def _pid_ok(pid):
    return isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2**32


def _now_ok(now):
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return False
    if isinstance(now, int):
        return 0 <= now < 2**53
    return math.isfinite(now) and math.copysign(1.0, now) > 0 and now < 2**53


def _no_dups(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name):
    raise ValueError(name)


def _parse(raw):
    """Validated lock record, or UNKNOWN_HOLDER (never guessed, never taken over)."""
    try:
        doc = json.loads(raw.decode("ascii"), object_pairs_hook=_no_dups, parse_constant=_no_constant)
        ok = (
            len(raw) <= MAX_LOCK_BYTES and type(doc) is dict and doc.keys() == _KEYS
            and type(doc["version"]) is int and doc["version"] == 1
            and isinstance(doc["boot_id"], str) and bool(_BOOT_ID.fullmatch(doc["boot_id"]))
            and _pid_ok(doc["pid"]) and _now_ok(doc["since"])
            and isinstance(doc["token"], str) and bool(_TOKEN.fullmatch(doc["token"]))
        )
    except (ValueError, TypeError, RecursionError):
        ok = False
    if not ok:
        raise _Refused("UNKNOWN_HOLDER")
    return doc


def _read(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        return os.read(fd, MAX_LOCK_BYTES + 1)
    finally:
        os.close(fd)


def _is_link(st):
    return stat_mod.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE_POINT


def _load(path, stat):
    """Validated record; FileNotFoundError passes through to the caller."""
    st = stat(path)
    if _is_link(st):
        raise _Refused("LINK_REFUSED")
    if not stat_mod.S_ISREG(st.st_mode):
        raise _Refused("UNKNOWN_HOLDER")
    return _parse(_read(path))


@contextmanager
def _guard(state, stat):
    """Cross-process critical section around every look-then-change of the lock.

    A byte lock on state/talk.lock.guard: the OS drops it when the holding
    handle closes or its process dies, so the guard is never stale. Busy means
    another acquire/release/reset is mid-change: refuse, never wait.
    """
    path = os.path.join(state, GUARD_NAME)
    try:
        st = stat(path)
    except FileNotFoundError:
        pass
    else:
        if _is_link(st):
            raise _Refused("LINK_REFUSED")
        if not stat_mod.S_ISREG(st.st_mode):
            raise _Refused("IO_ERROR")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    except PermissionError:
        raise _Refused("IO_ERROR") from None  # read-only or denied guard: not busy, opens share
    try:
        try:
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # per handle: conflicts inside one process too
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # flock, not lockf: per open file
        except OSError:
            raise _Refused("LOCK_BUSY") from None
        try:
            yield
        finally:
            if os.name == "nt":
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass  # close below drops the byte lock anyway; never mask the result
    finally:
        os.close(fd)


def _create(path, record):
    """Atomic create: O_EXCL decides the single winner; a failed write leaves no lock."""
    data = json.dumps(record, sort_keys=True).encode("ascii")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    ok = False
    try:
        ok = os.write(fd, data) == len(data)
    finally:
        os.close(fd)
        if not ok:
            os.remove(path)
    if not ok:
        raise OSError("short write")


def _dead(alive, pid):
    try:
        return alive(pid) is False  # only a literal False; errors and odd answers mean alive
    except Exception:
        return False


@_guarded
def acquire(root, boot_id, pid, now, *, alive, stat=os.lstat):
    """Lease for the single instance, or a Refusal naming the holder.

    A lock from another boot id, or whose holder alive() reports dead, is stale
    and is taken over (Lease.note == INTERRUPTED). A lock that cannot be read
    is never taken over: UNKNOWN_HOLDER until operator_reset().
    """
    if not isinstance(boot_id, str) or not _BOOT_ID.fullmatch(boot_id):
        raise _Refused("BOOT_ID_INVALID")
    if not _pid_ok(pid):
        raise _Refused("PID_INVALID")
    if not _now_ok(now):
        raise _Refused("NOW_INVALID")
    if not callable(alive):
        raise _Refused("ALIVE_INVALID")
    home = _ensure(root, stat)
    path = os.path.join(home.state, LOCK_NAME)
    token = secrets.token_hex(16)
    record = {"version": 1, "boot_id": boot_id, "pid": pid, "since": now, "token": token}
    with _guard(home.state, stat):  # detection and replacement in one critical section
        try:
            _create(path, record)
            return Lease(token, path, "")
        except FileExistsError:
            pass
        try:
            held = _load(path, stat)
        except FileNotFoundError:
            raise _Refused("RACE_LOST") from None  # only a writer outside this module
        if held["boot_id"] == boot_id and not _dead(alive, held["pid"]):
            raise _Refused("HELD", f" pid {held['pid']} boot {held['boot_id']} since {held['since']}")
        new = path + ".new"  # write aside, then swap: a failed write keeps the stale lock and its note
        try:
            os.remove(new)  # leftover of a crashed swap; only guard holders touch it
        except FileNotFoundError:
            pass
        _create(new, record)
        os.replace(new, path)
        return Lease(token, path, INTERRUPTED)


def _lock_path(root, stat):
    home = _layout(root)
    _plain_folder(root, stat, "NO_LOCK", "ROOT_NOT_FOLDER")
    _plain_folder(home.state, stat, "NO_LOCK", "FOLDER_NOT_FOLDER")
    return os.path.join(home.state, LOCK_NAME)


@_guarded
def release(root, token, stat=os.lstat):
    """Remove the lock; only the holder's token works. True on success."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise _Refused("TOKEN_INVALID")
    path = _lock_path(root, stat)
    with _guard(os.path.dirname(path), stat):
        try:
            held = _load(path, stat)
        except FileNotFoundError:
            raise _Refused("NO_LOCK") from None
        if not hmac.compare_digest(held["token"], token):
            raise _Refused("WRONG_TOKEN")
        os.remove(path)
    return True


@_guarded
def operator_reset(root, stat=os.lstat):
    """Explicit operator call: drop whatever lock file is there. True if one was removed."""
    path = _lock_path(root, stat)
    with _guard(os.path.dirname(path), stat):
        try:
            st = stat(path)
        except FileNotFoundError:
            return False
        if _is_link(st):
            raise _Refused("LINK_REFUSED")
        os.remove(path)
    return True
