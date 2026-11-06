# pyright: strict
"""N6-02: per-slice KPI rows for the E8 handover measures, one additive table.

One row per (slice, source) holds the five E8 measures (pass, post-accept defects, Salih
interventions, quota, finish time) plus security incidents; ``source`` says whether the
conveyor or N6 decided the slice, so both are compared on the same rows (R33). The table is
created with CREATE TABLE IF NOT EXISTS beside the existing ones: no ALTER, SCHEMA_VERSION
stays 10, a schema-10 reader never touches it. A row with any missing measure is refused
(R32: a missing reading is reported missing, never as zero), by the dataclass and again by
the table's NOT NULL / CHECK constraints. Nothing here imports live data, reads the wall
clock (``recorded_at`` is injected) or opens ``data/jarvis.db`` on its own.
"""

from __future__ import annotations

import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SOURCES = ("conveyor", "n6")
_SLICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS n6_kpi_row (
    slice_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('conveyor', 'n6')),
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    post_accept_defects INTEGER NOT NULL CHECK (post_accept_defects >= 0),
    salih_interventions INTEGER NOT NULL CHECK (salih_interventions >= 0),
    quota_tokens INTEGER NOT NULL CHECK (quota_tokens >= 0),
    finish_seconds REAL NOT NULL CHECK (finish_seconds >= 0),
    security_incidents INTEGER NOT NULL CHECK (security_incidents >= 0),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (slice_id, source)
)
"""
_COLUMNS = (
    "slice_id", "source", "passed", "post_accept_defects", "salih_interventions",
    "quota_tokens", "finish_seconds", "security_incidents", "recorded_at",
)


class KpiError(ValueError):
    """A KPI row that is incomplete, out of range or already recorded."""


def _count(name: str, value: object) -> int:
    # bool is an int subclass: True must not pass as a count of 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KpiError(f"{name} must be a non-negative integer, got {value!r}")
    return value


@dataclass(frozen=True)
class KpiRow:
    slice_id: str
    source: str
    passed: bool
    post_accept_defects: int
    salih_interventions: int
    quota_tokens: int
    finish_seconds: float
    security_incidents: int
    recorded_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.slice_id, str) or not _SLICE_ID.fullmatch(self.slice_id):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise KpiError(f"slice_id is missing or malformed: {self.slice_id!r}")
        if self.source not in SOURCES:
            raise KpiError(f"source must be one of {SOURCES}, got {self.source!r}")
        if not isinstance(self.passed, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise KpiError(f"passed must be a bool, got {self.passed!r}")
        for name in ("post_accept_defects", "salih_interventions", "quota_tokens",
                     "security_incidents"):
            _count(name, getattr(self, name))
        seconds: object = self.finish_seconds
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))  # pyright: ignore[reportUnnecessaryIsInstance]
                or not math.isfinite(seconds) or seconds < 0):
            raise KpiError(f"finish_seconds must be a finite number >= 0, got {seconds!r}")
        if (not isinstance(self.recorded_at, datetime)  # pyright: ignore[reportUnnecessaryIsInstance]
                or self.recorded_at.tzinfo is None):
            raise KpiError("recorded_at must be a timezone-aware datetime")


def ensure_table(connection: sqlite3.Connection) -> None:
    """Idempotent and additive: only creates ``n6_kpi_row`` when it is absent."""
    connection.execute(_TABLE_SQL)


def record(database_path: str | Path, row: KpiRow) -> None:
    """Insert one row; a second row for the same (slice, source) is refused, not overwritten."""
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_table(connection)
        try:
            with connection:
                connection.execute(
                    f"INSERT INTO n6_kpi_row ({', '.join(_COLUMNS)}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (row.slice_id, row.source, int(row.passed), row.post_accept_defects,
                     row.salih_interventions, row.quota_tokens, float(row.finish_seconds),
                     row.security_incidents, row.recorded_at.isoformat()),
                )
        except sqlite3.IntegrityError as error:
            raise KpiError(f"KPI row refused for {row.slice_id}/{row.source}: {error}") from error


def rows(database_path: str | Path, *, source: str | None = None) -> tuple[KpiRow, ...]:
    """All rows (optionally one source) in slice order; [] when the table does not exist yet."""
    with closing(sqlite3.connect(database_path)) as connection:
        found = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'n6_kpi_row'"
        ).fetchone()
        if found is None:
            return ()
        query = f"SELECT {', '.join(_COLUMNS)} FROM n6_kpi_row"
        args: tuple[str, ...] = ()
        if source is not None:
            query += " WHERE source = ?"
            args = (source,)
        fetched = connection.execute(query + " ORDER BY slice_id, source", args).fetchall()
    return tuple(
        KpiRow(str(r[0]), str(r[1]), bool(r[2]), int(r[3]), int(r[4]), int(r[5]), float(r[6]),
               int(r[7]), datetime.fromisoformat(str(r[8])))
        for r in fetched  # pyright: ignore[reportAny]
    )
