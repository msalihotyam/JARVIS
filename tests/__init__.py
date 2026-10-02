"""JARVIS core tests.

THE LIVE DATABASE IS OFF LIMITS. ``Settings.memory_database`` defaults to
``<repo>/data/jarvis.db`` - which, in this canonical checkout, IS the
operator's live database. A test that isolates ``config.json`` but lets the
default through would open it, and every open writes (WAL, provenance
repair, the additive compatibility layer). So opening that exact path from a
test process fails loudly instead. Production code is untouched: this guard
exists only while the ``tests`` package is imported.

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
import os
import shutil
import tempfile
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
