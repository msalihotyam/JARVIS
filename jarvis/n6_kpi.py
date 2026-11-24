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
ORIGINS = ("live", "sweep", "replay", "unknown")  # N6LIVE1: only "live" counts toward Gate B
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
# N6LIVE1: origin lives in its own additive table (no ALTER on n6_kpi_row, schema stays 10);
# a row recorded before this table existed has no origin row and reads "unknown".
_ORIGIN_SQL = """
CREATE TABLE IF NOT EXISTS n6_kpi_origin (
    slice_id TEXT NOT NULL,
    source TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('live', 'sweep', 'replay', 'unknown')),
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
    origin: str = "unknown"

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
        if self.origin not in ORIGINS:
            raise KpiError(f"origin must be one of {ORIGINS}, got {self.origin!r}")
        if (not isinstance(self.recorded_at, datetime)  # pyright: ignore[reportUnnecessaryIsInstance]
                or self.recorded_at.tzinfo is None):
            raise KpiError("recorded_at must be a timezone-aware datetime")


def ensure_table(connection: sqlite3.Connection) -> None:
    """Idempotent and additive: only creates ``n6_kpi_row`` / ``n6_kpi_origin`` when absent."""
    connection.execute(_TABLE_SQL)
    connection.execute(_ORIGIN_SQL)


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
                connection.execute("INSERT INTO n6_kpi_origin (slice_id, source, origin) VALUES (?,?,?)",
                                   (row.slice_id, row.source, row.origin))
        except sqlite3.IntegrityError as error:
            raise KpiError(f"KPI row refused for {row.slice_id}/{row.source}: {error}") from error


RECEIPT_ID = re.compile(r"[0-9a-f]{8}")  # the conveyor's lane id (lanes.json "id", dispatch "id")
# N6EVT1: one durable record per slice N6 ran end to end under LIMITED_ON (N6_KIT Phase 2
# exit evidence). Its own additive table (no ALTER, schema stays 10), created only on write.
_LIMITED_SQL = """
CREATE TABLE IF NOT EXISTS n6_limited_event (
    slice_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    PRIMARY KEY (slice_id, run_id)
)
"""


@dataclass(frozen=True)
class LimitedEvent:
    slice_id: str
    run_id: str
    started_at: datetime
    ended_at: datetime
    verdict: str
    receipt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.slice_id, str) or not _SLICE_ID.fullmatch(self.slice_id):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise KpiError(f"slice_id is missing or malformed: {self.slice_id!r}")
        if not isinstance(self.run_id, str) or not self.run_id:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise KpiError(f"run_id must be a non-empty string, got {self.run_id!r}")
        for at in (self.started_at, self.ended_at):
            if not isinstance(at, datetime) or at.tzinfo is None:  # pyright: ignore[reportUnnecessaryIsInstance]
                raise KpiError("started_at / ended_at must be timezone-aware datetimes")
        if self.started_at > self.ended_at:
            raise KpiError("started_at is after ended_at")
        if not isinstance(self.verdict, str) or not self.verdict or self.verdict != self.verdict.upper():  # pyright: ignore[reportUnnecessaryIsInstance]
            raise KpiError(f"verdict must be a non-empty upper-case string, got {self.verdict!r}")
        if not isinstance(self.receipt_id, str) or not RECEIPT_ID.fullmatch(self.receipt_id):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise KpiError(f"receipt_id is missing or malformed: {self.receipt_id!r}")


def record_limited(database_path: str | Path, event: LimitedEvent) -> None:
    """Insert one event; the same (slice, run) a second time is refused, never overwritten."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(_LIMITED_SQL)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO n6_limited_event (slice_id, run_id, started_at, ended_at, verdict,"
                    " receipt_id) VALUES (?,?,?,?,?,?)",
                    (event.slice_id, event.run_id, event.started_at.isoformat(),
                     event.ended_at.isoformat(), event.verdict, event.receipt_id))
        except sqlite3.IntegrityError as error:
            raise KpiError(f"n6_limited record refused for {event.slice_id}/{event.run_id}: {error}") from error


def limited_events(database_path: str | Path) -> tuple[LimitedEvent, ...]:
    """All n6_limited events in slice order; () when the table does not exist yet."""
    with closing(sqlite3.connect(database_path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                              " AND name = 'n6_limited_event'").fetchone() is None:
            return ()
        fetched = connection.execute(
            "SELECT slice_id, run_id, started_at, ended_at, verdict, receipt_id"
            " FROM n6_limited_event ORDER BY slice_id, run_id").fetchall()
    return tuple(
        LimitedEvent(str(r[0]), str(r[1]), datetime.fromisoformat(str(r[2])),
                     datetime.fromisoformat(str(r[3])), str(r[4]), str(r[5]))
        for r in fetched  # pyright: ignore[reportAny]
    )


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
        has_origin = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'n6_kpi_origin'"
        ).fetchone() is not None
        origins: dict[tuple[str, str], str] = {}
        if has_origin:
            origins = {(str(a), str(b)): str(c) for a, b, c in connection.execute(  # pyright: ignore[reportAny]
                "SELECT slice_id, source, origin FROM n6_kpi_origin")}
    return tuple(
        KpiRow(str(r[0]), str(r[1]), bool(r[2]), int(r[3]), int(r[4]), int(r[5]), float(r[6]),
               int(r[7]), datetime.fromisoformat(str(r[8])), origins.get((str(r[0]), str(r[1])), "unknown"))
        for r in fetched  # pyright: ignore[reportAny]
    )
