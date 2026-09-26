"""JARVIS core tests.

THE LIVE DATABASE IS OFF LIMITS. ``Settings.memory_database`` defaults to
``<repo>/data/jarvis.db`` - which, in this canonical checkout, IS the
operator's live database. A test that isolates ``config.json`` but lets the
default through would open it, and every open writes (WAL, provenance
repair, the additive compatibility layer). So opening that exact path from a
test process fails loudly instead. Production code is untouched: this guard
exists only while the ``tests`` package is imported.
"""

from pathlib import Path

from jarvis import memory as _memory
from jarvis.settings import PROJECT_ROOT as _PROJECT_ROOT

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
