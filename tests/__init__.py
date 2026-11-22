"""JARVIS core tests.

THE LIVE DATABASE IS OFF LIMITS. ``Settings.memory_database`` defaults to
``<repo>/data/jarvis.db`` - which, in this canonical checkout, IS the
operator's live database. A test that isolates ``config.json`` but lets the
default through would open it, and every open writes (WAL, provenance
repair, the additive compatibility layer). So opening that exact path from a
test process fails loudly instead. Production code is untouched: this guard
exists only while the ``tests`` package is imported.

CHILD PROCESSES TOO. The guard lives in the process that imported ``tests``;
a ``python -c`` child does not inherit it. So a child that starts the app or
opens a store imports ``tests`` FIRST (before any ``jarvis`` import) AND names
a temporary ``memory_database``. The one exception is the clean-launch check
in ``test_ui``, which must import only what ``jarvis.ui.app`` imports; it
names its temporary database explicitly.

THE SAME SUITE ON EVERY MACHINE. Two facts of the developer's machine used to
leak into the results:

* The temporary directory. Production canonicalises its allowed roots
  (``resolve()``) but takes a caller-named path LEXICALLY (no-follow, by
  design), so a fixture that names its own temporary root by a non-canonical
  spelling - an 8.3 short name such as ``C:\\Users\\RUNNER~1``, as a GitHub
  runner's TEMP is - is refused as outside that same root. Tests therefore get
  the canonical temporary directory, whatever TEMP spells it as.
* The Claude CLI. Many connector tests mock ``Popen`` but let the connector
  locate ``claude`` through trusted-executable provenance on PATH - i.e. the
  developer's own installation. A stand-in ``claude`` is put FIRST on PATH, in
  its own temporary directory: provenance resolves a real file there exactly
  as it would an installed CLI, and no test can ever start the real one.
"""

import atexit
import builtins
import functools
import io
import os
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePath

from jarvis import memory as _memory
from jarvis.settings import PROJECT_ROOT as _PROJECT_ROOT

_CANONICAL_TEMP = str(Path(tempfile.gettempdir()).resolve())
tempfile.tempdir = _CANONICAL_TEMP
for _key in ("TEMP", "TMP", "TMPDIR"):
    if _key in os.environ:
        os.environ[_key] = _CANONICAL_TEMP

_STAND_IN_CLI = Path(tempfile.mkdtemp(prefix="jarvis-test-cli-"))
atexit.register(shutil.rmtree, _STAND_IN_CLI, True)
(_STAND_IN_CLI / ("claude.exe" if os.name == "nt" else "claude")).write_bytes(b"stand-in for the installed Claude CLI")
if os.name != "nt":
    (_STAND_IN_CLI / "claude").chmod(0o755)
os.environ["PATH"] = os.pathsep.join((str(_STAND_IN_CLI), os.environ.get("PATH", "")))

#: The canonical code-default database path - never a test's database.
LIVE_DATABASE = (_PROJECT_ROOT / "data" / "jarvis.db").resolve()

_open_store = _memory.SQLiteMemoryStore.__init__


def _refuse_live_database(self, database_path) -> None:  # type: ignore[no-untyped-def]
    if Path(database_path).resolve() == LIVE_DATABASE:
        raise AssertionError(
            f"a test tried to open the live database {LIVE_DATABASE}; give it a temporary one"
        )
    _open_store(self, database_path)


if getattr(_memory.SQLiteMemoryStore.__init__, "__name__", "") != _refuse_live_database.__name__:
    _memory.SQLiteMemoryStore.__init__ = _refuse_live_database

# POLICYGUARD1 (A5, one guard for every test module): ``sqlite3.connect`` and ``open`` refuse the
# live database path too, not only ``SQLiteMemoryStore``: any code a test reaches, any store,
# any direct read. POLICYGUARD1FIX3: decided by IDENTITY, never by spelling (Win32 reaches one file
# through endless spellings: \\?\, \??\, admin shares, ::$DATA, trailing dots, 8.3 names, links).
# A target is live when (a) it is the same file as an existing live file or companion, or (b) its
# folder is the same folder as data/ and its Win32 name starts with "jarvis.db", or (c) any step
# raises. A temporary copy named ``jarvis.db`` elsewhere is fine. ``LIVE_DATABASE`` is read at call
# time so tests can point it at a temporary folder. A "file:" string is judged as a URI AND as a
# plain path; a dangling link, a custom opener/factory and a changing __fspath__ are refused or pinned.
# shortcut: ``os.open``, ``ATTACH`` and ``VACUUM INTO`` are not covered; wrap them if a test needs them.
_COMPANIONS = ("", "-wal", "-shm", "-journal")


def _live_name(name: str) -> bool:
    # Win32 drops a :stream suffix and trailing dots/spaces; NTFS folds case. upper() also maps ı/ſ to
    # I/S, which NTFS does not fold: refused anyway (fail closed).
    name = name.split(":")[0].rstrip(". ")
    return name.casefold().startswith("jarvis.db") or name.upper().startswith("JARVIS.DB")


def _exact(target):  # type: ignore[no-untyped-def]
    # The exact str/bytes a call opens: a subclass cannot answer == / in differently.
    raw = os.fspath(target)
    return str.__str__(raw) if isinstance(raw, str) else bytes(raw)


def _decide(text: str, plain: bool = False) -> bool:
    # plain: the path reading of a "file:" string. A path Win32 cannot resolve (invalid name, missing
    # folder) cannot be opened either, so only for that reading such an error means "not live".
    try:
        live = os.fspath(LIVE_DATABASE)
        # (a) the same file as an existing live file, whatever the spelling or link. The stream suffix
        # and trailing dots/spaces are dropped: a stream on a hard link is a stream on the live file.
        existing = []
        for suffix in _COMPANIONS:
            try:
                existing.append(os.stat(live + suffix))
            except FileNotFoundError:
                pass
        if existing:
            head, tail = os.path.split(os.path.abspath(text))
            try:
                found = os.stat(os.path.join(head, tail.split(":")[0].rstrip(". ")))
            except (FileNotFoundError, NotADirectoryError):
                found = None
            if found is not None and any(os.path.samestat(found, each) for each in existing):
                return True
        if os.path.lexists(text) and not os.path.exists(text):
            return True  # a dangling link: realpath can be led elsewhere than the OS will go
        # (b) a jarvis.db* name in the live folder. realpath also follows a link to a missing file.
        parent, name = os.path.split(os.path.realpath(text))
        if not _live_name(name):
            return False
        parent_stat = os.stat(parent)  # a missing or unreachable folder raises: live (c)
        try:
            live_folder = os.stat(os.path.dirname(live))
        except FileNotFoundError:
            return False  # no live folder: an existing folder cannot be it
        return os.path.samestat(parent_stat, live_folder)
    except OSError as error:
        unresolvable = isinstance(error, (FileNotFoundError, NotADirectoryError)) or getattr(error, "winerror", None) == 123
        if plain and unresolvable:
            return False
        raise


def _is_live(target) -> bool:  # type: ignore[no-untyped-def]
    if not isinstance(target, (str, bytes, os.PathLike)):
        return False  # a file descriptor is never the path
    try:
        text = os.fsdecode(_exact(target))
        if text[:5].lower() != "file:":
            return text not in ("", ":memory:") and _decide(text)  # SQLite's in-memory / temporary: no file
        # open() and a connect without uri=True take the same string as a plain relative path
        # ("file:/../data/jarvis.db" is data\jarvis.db): judge both readings.
        if _decide(text, plain=True):
            return True
        path = urllib.parse.urlparse(text).path
        if path == ":memory:":
            return False
        # url2pathname mangles a UNC authority ("//host/share"); SQLite opens it as is.
        text = urllib.parse.unquote(path) if path.startswith("//") else urllib.request.url2pathname(path)
        if "\0" in text:
            return True  # SQLite cuts the decoded URI path at an encoded NUL ("%00") and opens what is left
        return text not in ("", ":memory:") and _decide(text)
    except Exception:  # noqa: BLE001 - (c) anything that cannot be decided is refused (fail closed)
        return True


def _stable(target) -> bool:  # type: ignore[no-untyped-def]
    return type(target) in (str, bytes) or (
        isinstance(target, PurePath) and type(target).__fspath__ is PurePath.__fspath__
    )


def _guarded(function, hook: str, position: int):  # type: ignore[no-untyped-def]
    # hook: the argument (open's opener, connect's factory) that could open any file whatever the path.
    @functools.wraps(function)
    def guard(*args, **kwargs):  # type: ignore[no-untyped-def]
        extra = args[position] if len(args) > position else kwargs.get(hook)
        if extra is not None and extra is not sqlite3.Connection:
            raise AssertionError(f"a custom {hook} hides the opened file from the live-database guard; pass none")
        key = "database" if "database" in kwargs else "file"
        target = args[0] if args else kwargs.get(key)
        if isinstance(target, (str, bytes, os.PathLike)) and not _stable(target):
            target = _exact(target)  # __fspath__ runs once: the call opens exactly what was judged
            if args:
                args = (target, *args[1:])
            else:
                kwargs[key] = target
        if _is_live(target):
            raise AssertionError(f"a test tried to open the live database {LIVE_DATABASE}; give it a temporary one")
        return function(*args, **kwargs)

    guard.policy_guard = True  # type: ignore[attr-defined]
    return guard


if not getattr(sqlite3.connect, "policy_guard", False):
    sqlite3.connect = _guarded(sqlite3.connect, "factory", 5)
if not getattr(io.open, "policy_guard", False):
    builtins.open = io.open = _guarded(io.open, "opener", 7)  # one wrapper: builtins.open stays io.open

# The CLI moves the legacy Gmail directory out of the data directory at every
# start, before any store is opened. A test whose settings let the default
# through must never move the operator's live one: for the live data
# directory the move is a no-op. Bound here, before ``jarvis.cli`` imports it.
from jarvis import gmail_cli as _gmail_cli  # noqa: E402

_migrate_gmail = _gmail_cli.migrate_gmail_config_dir


def _never_move_live_gmail(legacy, current):  # type: ignore[no-untyped-def]
    if Path(legacy).resolve().parent == LIVE_DATABASE.parent:
        return None
    return _migrate_gmail(legacy, current)


if getattr(_gmail_cli.migrate_gmail_config_dir, "__name__", "") != _never_move_live_gmail.__name__:
    _gmail_cli.migrate_gmail_config_dir = _never_move_live_gmail
