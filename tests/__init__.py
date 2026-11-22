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
from pathlib import Path

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
# any direct read. Keyed on the resolved real path (and its -wal/-shm/-journal files), so a
# temporary copy named ``jarvis.db`` is fine. shortcut: ``os.open`` and a hard link to the file
# are not covered; add them here if a test ever needs them.
_LIVE_PATHS = {os.path.normcase(str(LIVE_DATABASE) + suffix) for suffix in ("", "-wal", "-shm", "-journal")}


def _is_live(target) -> bool:  # type: ignore[no-untyped-def]
    if not isinstance(target, (str, bytes, os.PathLike)):
        return False  # a file descriptor is never the path
    target = os.fsdecode(os.fspath(target))
    # Any "file:" string counts as a URI, whatever flag or argument position carried it.
    uri = target[:5].lower() == "file:"
    try:
        if uri:
            target = urllib.request.url2pathname(urllib.parse.urlparse(target).path)
        if uri and "\0" in target:
            return True  # SQLite cuts the decoded URI path at an encoded NUL ("%00") and opens what is left
        return os.path.normcase(str(Path(target).resolve())) in _LIVE_PATHS
    except (OSError, ValueError):
        # A URI that cannot be resolved (an encoded NUL, ...) may still open the file: refuse it.
        # A plain path that cannot be read makes the wrapped call raise its own error.
        return uri


def _guarded(function):  # type: ignore[no-untyped-def]
    @functools.wraps(function)
    def guard(*args, **kwargs):  # type: ignore[no-untyped-def]
        target = args[0] if args else kwargs.get("database", kwargs.get("file"))
        if _is_live(target):
            raise AssertionError(f"a test tried to open the live database {LIVE_DATABASE}; give it a temporary one")
        return function(*args, **kwargs)

    guard.policy_guard = True  # type: ignore[attr-defined]
    return guard


if not getattr(sqlite3.connect, "policy_guard", False):
    sqlite3.connect = _guarded(sqlite3.connect)
if not getattr(io.open, "policy_guard", False):
    builtins.open = io.open = _guarded(io.open)  # one wrapper: builtins.open stays io.open

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
