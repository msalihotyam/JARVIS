"""N6-02: KPI rows for the E8 measures, an additive table inside a schema-10 database."""

import dataclasses
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from jarvis import memory, n6_kpi
from jarvis.memory import SQLiteMemoryStore

ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = (ROOT / "data" / "jarvis.db").resolve()
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
MEASURES = ("post_accept_defects", "salih_interventions", "quota_tokens", "finish_seconds",
            "security_incidents")


def _row(**over):
    base = dict(slice_id="N6-02", source="n6", passed=True, post_accept_defects=0,
                salih_interventions=1, quota_tokens=1_260_000, finish_seconds=1800.5,
                security_incidents=0, recorded_at=T0)
    base.update(over)
    return n6_kpi.KpiRow(**base)


def _schema(path):
    with sqlite3.connect(path) as connection:
        return dict(connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall())


class N6KpiTests(unittest.TestCase):
    def setUp(self):
        real_connect = sqlite3.connect
        self.opened = []

        def spy(database, *args, **kwargs):
            self.opened.append(str(database))
            return real_connect(database, *args, **kwargs)

        patch = mock.patch("sqlite3.connect", spy)
        patch.start()
        self.addCleanup(patch.stop)
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEMP"), ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "kpi.db"

    def tearDown(self):
        # NEG: no test in this module ever touches the live database.
        for opened in self.opened:
            self.assertNotEqual(Path(opened).resolve(), LIVE_DB)

    def test_round_trip_of_one_row(self):
        n6_kpi.record(self.db, _row())
        self.assertEqual(n6_kpi.rows(self.db), (_row(),))

    def test_conveyor_and_n6_rows_for_one_slice_sit_side_by_side(self):
        n6_kpi.record(self.db, _row(source="conveyor", passed=False))
        n6_kpi.record(self.db, _row(source="n6"))
        self.assertEqual([r.source for r in n6_kpi.rows(self.db)], ["conveyor", "n6"])
        self.assertEqual([r.passed for r in n6_kpi.rows(self.db, source="n6")], [True])

    def test_rows_without_the_table_is_empty_and_creates_nothing(self):
        SQLiteMemoryStore(self.db)
        before = _schema(self.db)
        self.assertEqual(n6_kpi.rows(self.db), ())
        self.assertEqual(_schema(self.db), before)

    def test_schema_10_reader_opens_the_database_unchanged(self):
        SQLiteMemoryStore(self.db)
        before = _schema(self.db)
        n6_kpi.record(self.db, _row())
        after = _schema(self.db)
        self.assertEqual(set(after) - set(before), {"n6_kpi_row"})
        self.assertEqual({k: after[k] for k in before}, before)  # no ALTER of any table
        SQLiteMemoryStore(self.db)  # a schema-10 reader still opens it ...
        self.assertEqual(memory.SCHEMA_VERSION, 10)
        with sqlite3.connect(self.db) as connection:  # ... and the version row is untouched
            self.assertEqual(connection.execute(
                "SELECT version FROM schema_version ORDER BY rowid DESC").fetchone()[0], 10)
        self.assertEqual(n6_kpi.rows(self.db), (_row(),))
        self.assertEqual({k: _schema(self.db)[k] for k in before}, before)

    def test_ensure_table_is_idempotent(self):
        with sqlite3.connect(self.db) as connection:
            n6_kpi.ensure_table(connection)
            n6_kpi.ensure_table(connection)

    def test_neg_a_missing_measure_is_refused_not_filled_with_zero(self):
        for name in MEASURES + ("passed",):
            with self.subTest(missing=name):
                kwargs = dataclasses.asdict(_row())
                del kwargs[name]
                with self.assertRaises(TypeError):
                    n6_kpi.KpiRow(**kwargs)
                with self.assertRaises(n6_kpi.KpiError):
                    _row(**{name: None})
        self.assertEqual(n6_kpi.rows(self.db), ())

    def test_neg_the_table_itself_refuses_a_null_measure(self):
        n6_kpi.record(self.db, _row(slice_id="N6-03"))
        columns = ", ".join(n6_kpi._COLUMNS)
        for index, name in enumerate(n6_kpi._COLUMNS[2:], start=2):
            with self.subTest(null=name):
                values = [x for x in ("X-1", "n6", 1, 0, 0, 5, 1.0, 0, "t")]
                values[index] = None
                with sqlite3.connect(self.db) as connection:
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            f"INSERT INTO n6_kpi_row ({columns}) VALUES (?,?,?,?,?,?,?,?,?)",
                            values)
        self.assertEqual(len(n6_kpi.rows(self.db)), 1)

    def test_neg_out_of_range_or_mistyped_measures_are_refused(self):
        bad = [("post_accept_defects", -1), ("salih_interventions", 1.5),
               ("quota_tokens", "9"), ("quota_tokens", True), ("security_incidents", -2),
               ("finish_seconds", -0.1), ("finish_seconds", float("nan")),
               ("finish_seconds", float("inf")), ("finish_seconds", "soon"),
               ("passed", 1), ("passed", "yes"), ("source", "orchestrator"), ("source", ""),
               ("slice_id", ""), ("slice_id", "../x"), ("slice_id", None),
               ("recorded_at", datetime(2026, 11, 7)), ("recorded_at", "2026-11-07")]
        for name, value in bad:
            with self.subTest(name=name, value=value):
                with self.assertRaises(n6_kpi.KpiError):
                    _row(**{name: value})
        self.assertFalse(self.db.exists())

    def test_neg_a_duplicate_slice_and_source_is_refused_not_overwritten(self):
        n6_kpi.record(self.db, _row())
        with self.assertRaises(n6_kpi.KpiError):
            n6_kpi.record(self.db, _row(passed=False, quota_tokens=1))
        self.assertEqual(n6_kpi.rows(self.db), (_row(),))

    def test_zero_is_a_real_reading_when_given(self):
        n6_kpi.record(self.db, _row(quota_tokens=0, finish_seconds=0, salih_interventions=0))
        stored = n6_kpi.rows(self.db)[0]
        self.assertEqual((stored.quota_tokens, stored.finish_seconds), (0, 0.0))


if __name__ == "__main__":
    unittest.main()
