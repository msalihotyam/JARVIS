"""Scheduler phase 2A: due generation + claim / lease / fencing coordination.

Covers the additive SQLite 8 -> 9 migration, bounded due-generation
materialization with missed-run coalescing, atomic claim, the bounded lease
window, lease renewal, monotonic fencing, safe expiry/reclaim, voluntary
release, crash/restart safety and the revision binding a generation keeps for
life.

THE PHASE BOUNDARY IS ITSELF UNDER TEST. A valid lease is coordination, never
execution authority: at the end of this phase there is still no production
path that materializes an AgentCore task from a generation, creates an
approval, consumes a grant, opens an execution attempt, reserves money,
reaches a connector, reads a condition target, starts a background loop or
retries an unknown outcome. ``AuthorityBoundaryTests`` proves that from the
shipped source and from the durable tables rather than from this paragraph.

Every instant in this suite is EXPLICIT and every database is a disposable
local temporary file. Nothing sleeps, nothing polls, and no test performs a
live model, auth or network call.

TIMEZONE DATABASE. The calendar coalescing cases need real DST rules, and a
bare Windows CPython has no ``tzdata``. The phase-1A fixture already solves
this exactly once and is reused here through the phase-1B suite, exactly as
that suite reuses it.
"""

from __future__ import annotations

import ast
import copy
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from jarvis.contracts import (
    CLAIMABLE_JOB_RUN_STATUSES,
    MAX_JOB_LEASE,
    MIN_JOB_LEASE,
    SETTLED_JOB_RUN_STATUSES,
    UNSETTLED_JOB_RUN_STATUSES,
    JobError,
    JobKind,
    JobRun,
    JobRunLease,
    JobRunStatus,
    JobSpec,
    ProjectRecord,
    canonical_utc_timestamp,
)
from jarvis.memory import SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.schedules import Daily, Interval, OneTime, Weekday, Weekly
from tests.helpers import safe_cleanup

# The phase-1B fixtures, reused verbatim rather than copied: a second
# handwritten legacy schema or a second job factory would be a second set of
# facts to keep in agreement with the reviewed ones.
from tests.test_scheduler_jobs import (  # noqa: F401
    DIGEST,
    NOW,
    _V7_ROWS,
    _V7_SCHEMA,
    interval_schedule,
    one_time_schedule,
    raw,
    rewrite_run,
    settle,
    setUpModule,
    spec,
    step,
    tearDownModule,
)


#: The approved lease used by most of this suite. Comfortably inside the
#: window at both ends, so a test that fails is failing about coordination
#: rather than about the bound.
LEASE = timedelta(minutes=5)

#: An opaque worker identity. The store never inspects one, which is exactly
#: why a projection must never print it.
WORKER_A = "worker-a"
WORKER_B = "worker-b"

#: The durable scheduler storage surface as of phase 2A. No production module
#: may call any of it: there is still no dispatcher, no watcher and no
#: background worker in this build, so the only callers are tests.
SCHEDULER_SURFACE = frozenset(
    {
        "create_job",
        "edit_job",
        "set_job_enabled",
        "archive_job",
        "record_job_run",
        "materialize_due_job_run",
        "claim_job_run",
        "renew_job_run_lease",
        "release_job_run",
        "settle_job_run",
        "mark_job_run_unknown",
        "await_job_run_approval",
        "load_job",
        "load_job_run",
        "list_jobs",
        "list_job_runs",
        "job_summary",
    }
)

#: The scheduler tables of a REAL schema-8 database, written by hand rather
#: than generated from the code under test: a fixture the current build
#: produced could not prove that the current build leaves it alone. This is
#: the schema-8 shape exactly - the phase-2A ``lease_expires_at_utc`` column
#: is deliberately absent, because its arrival is what is being tested.
_V8_SCHEDULER_SCHEMA = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    next_due_at_utc TEXT,
    enabled INTEGER NOT NULL,
    archived_at TEXT,
    last_run_id TEXT,
    next_generation INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (kind IN ('one_time', 'recurring', 'condition_watch')),
    CHECK (definition_version >= 1),
    CHECK (revision >= 1),
    CHECK (next_generation >= 1),
    CHECK (enabled IN (0, 1)),
    CHECK (archived_at IS NULL OR enabled = 0),
    CHECK (next_due_at_utc IS NULL OR (enabled = 1 AND archived_at IS NULL)),
    FOREIGN KEY(project_id) REFERENCES projects(id));
CREATE INDEX idx_jobs_due ON jobs(enabled, next_due_at_utc);
CREATE TABLE job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    nominal_due_at_utc TEXT NOT NULL,
    project_id TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT,
    task_id TEXT,
    chain_id TEXT,
    attempt_id TEXT,
    owner_token TEXT,
    fencing_epoch INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    blocking_code TEXT,
    condition_result TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    settled_at TEXT,
    CHECK (job_revision >= 1),
    CHECK (generation >= 1),
    CHECK (fencing_epoch >= 0),
    CHECK (cancel_requested IN (0, 1)),
    CHECK (
        (settled_at IS NULL AND status IN (
            'pending', 'claimed', 'awaiting_approval', 'running',
            'unknown_outcome'))
        OR
        (settled_at IS NOT NULL AND status IN (
            'completed', 'failed', 'cancelled', 'skipped'))),
    UNIQUE (job_id, generation),
    UNIQUE (job_id, job_revision, nominal_due_at_utc),
    FOREIGN KEY(job_id) REFERENCES jobs(id));
CREATE UNIQUE INDEX idx_job_runs_unsettled
    ON job_runs(job_id) WHERE settled_at IS NULL;
"""


def table_names(database: Path) -> set[str]:
    connection = raw(database)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()


def run_columns(database: Path) -> list[str]:
    connection = raw(database)
    try:
        return [row[1] for row in connection.execute("PRAGMA table_info(job_runs)")]
    finally:
        connection.close()


def table_count(database: Path, table: str) -> int:
    connection = raw(database)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


class ClaimTestCase(unittest.TestCase):
    """A fresh schema-9 store with one active project to hang jobs on."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.store = SQLiteMemoryStore(self.database)
        self.store.save_project(ProjectRecord(id="proj-a", name="Project A"))

    def create(self, **kwargs):
        return self.store.create_job(spec(**kwargs), now=NOW)

    def due_run(self, at: datetime | None = None, **kwargs) -> JobRun:
        """One materialized generation of a fresh hourly job."""
        record = self.create(**kwargs)
        run = self.store.materialize_due_job_run(
            record.id, record.revision, now=at or NOW + timedelta(hours=1)
        )
        self.assertIsNotNone(run)
        self.job = record
        return run

    def force_status(self, run_id: str, status: JobRunStatus) -> None:
        """Put a generation into a coordination state phase 2A cannot reach.

        ``RUNNING``, ``AWAITING_APPROVAL`` and ``UNKNOWN_OUTCOME`` are states
        only a later phase will write. They still have to be REFUSED now, and
        the only honest way to test that today is to place the durable row in
        one directly, exactly as a future build - or a crash mid-transition -
        would leave it.
        """
        rewrite_run(self.store, run_id, status=status.value)


# --------------------------------------------------------------------------
# SCHEMA 8 -> 9
# --------------------------------------------------------------------------


class Schema9MigrationTests(unittest.TestCase):
    """The additive 8 -> 9 upgrade: ONE nullable column and nothing else."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"

    def build_v8(self, *, populated: bool) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(_V7_SCHEMA)
            connection.executescript(_V8_SCHEDULER_SCHEMA)
            if populated:
                connection.executescript(_V7_ROWS)
                connection.execute("UPDATE schema_version SET version = 8")
                connection.execute("INSERT INTO schema_migrations VALUES (8, 't8')")
                self.seed_scheduler_rows(connection)
            else:
                connection.execute("INSERT INTO schema_version VALUES (8)")
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def seed_scheduler_rows(connection: sqlite3.Connection) -> None:
        """One legacy job and one open legacy generation, at the v8 shape."""
        payload = JobSpec(
            project_id="proj-old",
            kind=JobKind.RECURRING,
            label="legacy digest",
            schedule=Interval(anchor=NOW, every=timedelta(hours=1)),
            steps=(step(),),
        )
        stamp = NOW.isoformat()
        connection.execute(
            "INSERT INTO jobs VALUES (?, 'proj-old', 1, 1, 'recurring', ?, ?, ?,"
            " 1, NULL, ?, 2, ?, ?)",
            (
                "11111111-1111-4111-8111-111111111111",
                payload.encoded(),
                payload.digest,
                (NOW + timedelta(hours=2)).isoformat(),
                "22222222-2222-4222-8222-222222222222",
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO job_runs VALUES (?, ?, 1, 1, ?, 'proj-old', ?, ?,"
            " 'pending', NULL, NULL, NULL, NULL, NULL, 0, 0, NULL, NULL,"
            " ?, NULL, NULL)",
            (
                "22222222-2222-4222-8222-222222222222",
                "11111111-1111-4111-8111-111111111111",
                (NOW + timedelta(hours=1)).isoformat(),
                payload.encoded(),
                payload.digest,
                stamp,
            ),
        )

    def test_empty_schema_eight_database_upgrades(self) -> None:
        self.build_v8(populated=False)
        before = table_names(self.database)
        self.assertNotIn("lease_expires_at_utc", run_columns(self.database))
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(10, store.schema_version())
        # ONE column. No new table, no dropped table, no new index.
        self.assertEqual(before, table_names(self.database))
        self.assertIn("lease_expires_at_utc", run_columns(self.database))

    def test_populated_schema_eight_database_upgrades_without_data_loss(self) -> None:
        self.build_v8(populated=True)
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(10, store.schema_version())
        connection = raw(self.database)
        try:
            # Every legacy authority row survives untouched.
            for table, identifier, column in (
                ("sessions", "s-old", "id"),
                ("tasks", "t-old", "id"),
                ("project_memory_items", "m-old", "id"),
                ("pending_approvals", "a-old", "id"),
                ("permission_grants", "g-old", "id"),
                ("execution_attempts", "att-old", "id"),
                ("attempt_billing", "att-old", "attempt_id"),
                ("usage_ledger", "u-old", "id"),
            ):
                self.assertIsNotNone(
                    connection.execute(
                        f"SELECT * FROM {table} WHERE {column} = ?", (identifier,)
                    ).fetchone(),
                    table,
                )
            # And so do the existing scheduler rows, including the open one.
            job = connection.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual("recurring", job["kind"])
            self.assertEqual(1, job["revision"])
            self.assertEqual(2, job["next_generation"])
            legacy = connection.execute("SELECT * FROM job_runs").fetchone()
            self.assertEqual("pending", legacy["status"])
            self.assertEqual(0, legacy["fencing_epoch"])
            self.assertIsNone(legacy["settled_at"])
            # NO AUTHORITY IS INVENTED. The new column arrives NULL, which is
            # precisely "no lease": the upgrade hands no generation to any
            # worker and unblocks nothing.
            self.assertIsNone(legacy["lease_expires_at_utc"])
            self.assertEqual(
                [6, 7, 8, 9, 10],
                [
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ],
            )
        finally:
            connection.close()
        # The preserved generation is still usable, and claimable exactly on
        # the phase-2A rules rather than on anything the migration decided.
        run = store.load_job_run("22222222-2222-4222-8222-222222222222")
        self.assertIsNotNone(run)
        self.assertIsNone(run.lease_expires_at_utc)
        self.assertTrue(run.claimable_at(NOW + timedelta(hours=1)))

    def test_reopening_after_upgrade_is_idempotent(self) -> None:
        self.build_v8(populated=True)
        SQLiteMemoryStore(self.database)
        first_tables, first_columns = table_names(self.database), run_columns(
            self.database
        )
        again = SQLiteMemoryStore(self.database)
        self.assertEqual(10, again.schema_version())
        self.assertEqual(first_tables, table_names(self.database))
        # The column is added once, not once per open.
        self.assertEqual(first_columns, run_columns(self.database))
        self.assertEqual(1, run_columns(self.database).count("lease_expires_at_utc"))

    def test_an_already_schema_nine_database_opens_unchanged(self) -> None:
        store = SQLiteMemoryStore(self.database)
        store.save_project(ProjectRecord(id="proj-a", name="A"))
        record = store.create_job(spec(), now=NOW)
        run = store.materialize_due_job_run(
            record.id, record.revision, now=NOW + timedelta(hours=1)
        )
        lease = store.claim_job_run(
            run.id, WORKER_A, now=NOW + timedelta(hours=1), lease=LEASE
        )
        columns = run_columns(self.database)
        reopened = SQLiteMemoryStore(self.database)
        self.assertEqual(10, reopened.schema_version())
        self.assertEqual(columns, run_columns(self.database))
        reloaded = reopened.load_job_run(run.id)
        self.assertEqual(lease.fencing_epoch, reloaded.fencing_epoch)
        self.assertEqual(lease.lease_expires_at_utc, reloaded.lease_expires_at_utc)

    def test_a_fresh_database_and_a_migrated_one_hold_the_same_columns(self) -> None:
        # The reason the column is added by ALTER on BOTH paths. SQLite appends
        # an added column to the table, so declaring it inside the CREATE would
        # put it mid-table on a fresh database and last on a migrated one. This
        # asserts the job_runs COLUMN LIST and its order, not the stored
        # sqlite_master text: that text keeps the CREATE (comments included) of
        # whichever release built the table, and is expected to differ.
        self.build_v8(populated=False)
        SQLiteMemoryStore(self.database)
        other = Path(self.temp.name) / "fresh.db"
        SQLiteMemoryStore(other)
        self.assertEqual(run_columns(other), run_columns(self.database))

    def test_output_schema_version_is_unchanged(self) -> None:
        from jarvis.app import OUTPUT_SCHEMA_VERSION
        from jarvis.contracts import JOB_DEFINITION_VERSION

        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(6, OUTPUT_SCHEMA_VERSION)
        self.assertEqual(1, JOB_DEFINITION_VERSION)


# --------------------------------------------------------------------------
# DUE GENERATION
# --------------------------------------------------------------------------


class DueGenerationTests(ClaimTestCase):
    """Materialization is bounded, coalescing and revision-guarded."""

    def test_nothing_is_materialized_before_the_due_instant(self) -> None:
        record = self.create()
        self.assertEqual((NOW + timedelta(hours=1)).isoformat(),
                         record.next_due_at_utc)
        self.assertIsNone(
            self.store.materialize_due_job_run(record.id, record.revision, now=NOW)
        )
        self.assertEqual(0, table_count(self.database, "job_runs"))
        # The boundary itself IS due: half-open the other way from a lease.
        run = self.store.materialize_due_job_run(
            record.id, record.revision, now=NOW + timedelta(hours=1)
        )
        self.assertEqual(1, run.generation)

    def test_a_one_time_job_materializes_its_single_occurrence(self) -> None:
        at = NOW + timedelta(hours=3)
        record = self.create(kind=JobKind.ONE_TIME, schedule=OneTime(at=at))
        run = self.store.materialize_due_job_run(
            record.id, record.revision, now=at + timedelta(minutes=1)
        )
        self.assertEqual(at.isoformat(), run.nominal_due_at_utc)
        self.assertEqual(JobRunStatus.PENDING, run.status)
        # NOMINAL DUE IS THE OCCURRENCE, not the instant it was materialized.
        self.assertNotEqual(run.created_at, run.nominal_due_at_utc)
        self.assertIsNone(self.store.load_job(record.id).next_due_at_utc)

    def test_a_one_time_job_never_replays(self) -> None:
        at = NOW + timedelta(hours=3)
        record = self.create(kind=JobKind.ONE_TIME, schedule=OneTime(at=at))
        run = self.store.materialize_due_job_run(
            record.id, record.revision, now=at
        )
        settle(self.store, run.id, JobRunStatus.COMPLETED, now=at)
        # Not now, not tomorrow, not in a decade.
        for later in (at, at + timedelta(days=1), at + timedelta(days=3650)):
            current = self.store.load_job(record.id)
            self.assertIsNone(
                self.store.materialize_due_job_run(
                    record.id, current.revision, now=later
                )
            )
        self.assertEqual(1, table_count(self.database, "job_runs"))

    def test_interval_missed_runs_coalesce_to_the_latest_due_occurrence(self) -> None:
        record = self.create()
        # Two and a half hours late: the 13:00 and 14:00 occurrences were
        # missed and the 14:00 one is what materializes. The 13:00 one is NOT
        # replayed and NOT queued.
        run = self.store.materialize_due_job_run(
            record.id, record.revision, now=NOW + timedelta(hours=2, minutes=30)
        )
        self.assertEqual((NOW + timedelta(hours=2)).isoformat(),
                         run.nominal_due_at_utc)
        self.assertEqual(1, run.generation)
        # The anchor advances past the coalesced occurrence, never behind it.
        self.assertEqual(
            (NOW + timedelta(hours=3)).isoformat(),
            self.store.load_job(record.id).next_due_at_utc,
        )

    def test_daily_missed_runs_coalesce(self) -> None:
        record = self.create(
            schedule=Daily(timezone="Europe/Berlin", at=time(3, 0))
        )
        # 03:00 Berlin is 01:00 UTC in September (CEST).
        self.assertEqual(
            datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc).isoformat(),
            record.next_due_at_utc,
        )
        run = self.store.materialize_due_job_run(
            record.id,
            record.revision,
            now=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
        )
        # Four days of missed occurrences become ONE, at the latest.
        self.assertEqual(
            datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc).isoformat(),
            run.nominal_due_at_utc,
        )
        self.assertEqual(1, table_count(self.database, "job_runs"))

    def test_weekly_missed_runs_coalesce(self) -> None:
        record = self.create(
            schedule=Weekly(
                timezone="Europe/Berlin",
                at=time(3, 0),
                days=frozenset({Weekday.MONDAY}),
            )
        )
        self.assertEqual(
            datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc).isoformat(),
            record.next_due_at_utc,
        )
        run = self.store.materialize_due_job_run(
            record.id,
            record.revision,
            now=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
        )
        # Three missed Mondays become ONE, at the latest Monday.
        self.assertEqual(
            datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc).isoformat(),
            run.nominal_due_at_utc,
        )

    def test_a_year_of_downtime_creates_no_backlog(self) -> None:
        # THE bound. A five-minute interval offline for a year names roughly
        # 105 000 occurrences; exactly one generation exists afterwards, and
        # settling it does not release a queue of the other 104 999.
        record = self.create(schedule=Interval(anchor=NOW, every=timedelta(minutes=5)))
        year_later = NOW + timedelta(days=365)
        run = self.store.materialize_due_job_run(
            record.id, record.revision, now=year_later
        )
        self.assertEqual(1, run.generation)
        self.assertEqual(1, table_count(self.database, "job_runs"))
        settle(self.store, run.id, JobRunStatus.COMPLETED, now=year_later)
        # The anchor now points AFTER the coalesced occurrence, so the same
        # instant is no longer due and nothing further materializes.
        current = self.store.load_job(record.id)
        self.assertGreater(
            datetime.fromisoformat(current.next_due_at_utc), year_later
        )
        self.assertIsNone(
            self.store.materialize_due_job_run(
                record.id, current.revision, now=year_later
            )
        )
        self.assertEqual(1, table_count(self.database, "job_runs"))

    def test_a_condition_watch_gets_one_check_opportunity_and_no_evaluation(
        self,
    ) -> None:
        record = self.create(kind=JobKind.CONDITION_WATCH)
        run = self.store.materialize_due_job_run(
            record.id, record.revision, now=NOW + timedelta(hours=2, minutes=30)
        )
        self.assertEqual(JobKind.CONDITION_WATCH, run.spec.kind)
        # NOTHING was evaluated: no TRUE/FALSE/UNKNOWN, no notification, no
        # on_true action, no task. The declaration is carried, not performed.
        self.assertIsNone(run.condition_result)
        self.assertIsNone(run.task_id)
        self.assertEqual(0, table_count(self.database, "tasks"))
        self.assertEqual(1, table_count(self.database, "job_runs"))

    def test_a_disabled_job_materializes_nothing(self) -> None:
        record = self.create()
        disabled = self.store.set_job_enabled(
            record.id, record.revision, False, now=NOW
        )
        self.assertIsNone(disabled.next_due_at_utc)
        self.assertIsNone(
            self.store.materialize_due_job_run(
                record.id, disabled.revision, now=NOW + timedelta(days=7)
            )
        )
        self.assertEqual(0, table_count(self.database, "job_runs"))

    def test_an_archived_job_materializes_nothing(self) -> None:
        record = self.create()
        archived = self.store.archive_job(record.id, record.revision)
        self.assertIsNone(
            self.store.materialize_due_job_run(
                record.id, archived.revision, now=NOW + timedelta(days=7)
            )
        )
        self.assertEqual(0, table_count(self.database, "job_runs"))

    def test_a_stale_revision_race_is_refused_deterministically(self) -> None:
        record = self.create()
        self.store.edit_job(
            record.id, record.revision, spec(label="renamed"), now=NOW
        )
        with self.assertRaisesRegex(JobError, "stale job revision"):
            self.store.materialize_due_job_run(
                record.id, record.revision, now=NOW + timedelta(hours=2)
            )
        # Refused having written NOTHING.
        self.assertEqual(0, table_count(self.database, "job_runs"))
        with self.assertRaisesRegex(JobError, "no usable job"):
            self.store.materialize_due_job_run(str(uuid4()), 1, now=NOW)

    def test_the_one_unsettled_generation_invariant_stays_authoritative(self) -> None:
        record = self.create()
        first = self.store.materialize_due_job_run(
            record.id, record.revision, now=NOW + timedelta(hours=1)
        )
        current = self.store.load_job(record.id)
        # An open generation is not an error, it is simply nothing to do.
        self.assertIsNone(
            self.store.materialize_due_job_run(
                record.id, current.revision, now=NOW + timedelta(hours=5)
            )
        )
        self.assertEqual(1, table_count(self.database, "job_runs"))
        settle(self.store, first.id, JobRunStatus.COMPLETED, now=NOW)
        second = self.store.materialize_due_job_run(
            record.id, current.revision, now=NOW + timedelta(hours=5)
        )
        self.assertEqual(2, second.generation)
        self.assertEqual(
            (NOW + timedelta(hours=5)).isoformat(), second.nominal_due_at_utc
        )

    def test_the_snapshot_is_immutable_and_the_digest_deterministic(self) -> None:
        record = self.create()
        run = self.store.materialize_due_job_run(
            record.id, record.revision, now=NOW + timedelta(hours=1)
        )
        self.assertEqual(record.spec, run.spec)
        self.assertEqual(record.spec.digest, run.snapshot_digest)
        self.assertEqual(
            run.snapshot_digest, self.store.load_job_run(run.id).snapshot_digest
        )

    def test_an_explicit_now_is_required(self) -> None:
        record = self.create()
        for bad in (None, "2026-09-06T13:00:00+00:00", NOW.replace(tzinfo=None)):
            with self.assertRaises(JobError):
                self.store.materialize_due_job_run(
                    record.id, record.revision, now=bad
                )


# --------------------------------------------------------------------------
# CLAIM
# --------------------------------------------------------------------------


class ClaimTests(ClaimTestCase):
    """Atomic ownership: one winner, an exact token, a monotonic epoch."""

    def setUp(self) -> None:
        super().setUp()
        self.at = NOW + timedelta(hours=1)
        self.run = self.due_run(self.at)

    def test_the_first_claim_succeeds_and_records_exactly_what_was_asked(
        self,
    ) -> None:
        lease = self.store.claim_job_run(
            self.run.id, WORKER_A, now=self.at, lease=LEASE
        )
        self.assertIsInstance(lease, JobRunLease)
        self.assertEqual(self.run.id, lease.run_id)
        self.assertEqual(self.job.id, lease.job_id)
        self.assertEqual(self.run.job_revision, lease.job_revision)
        self.assertEqual(self.run.generation, lease.generation)
        # THE EXACT TOKEN, stored verbatim rather than normalized.
        self.assertEqual(WORKER_A, lease.owner_token)
        self.assertEqual(1, lease.fencing_epoch)
        # CANONICAL UTC EXPIRY, exactly now + the requested lease.
        self.assertEqual((self.at + LEASE).isoformat(), lease.lease_expires_at_utc)
        self.assertEqual(lease.lease_expires_at_utc,
                         canonical_utc_timestamp(lease.lease_expires_at_utc))
        self.assertEqual(self.at.isoformat(), lease.claimed_at)
        stored = self.store.load_job_run(self.run.id)
        self.assertEqual(JobRunStatus.CLAIMED, stored.status)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(lease.lease_expires_at_utc, stored.lease_expires_at_utc)
        self.assertIn(JobRunStatus.CLAIMED, UNSETTLED_JOB_RUN_STATUSES)

    def test_a_second_concurrent_claim_is_refused(self) -> None:
        first = self.store.claim_job_run(
            self.run.id, WORKER_A, now=self.at, lease=LEASE
        )
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.store.claim_job_run(self.run.id, WORKER_B, now=self.at, lease=LEASE)
        # Even the same token cannot acquire a second epoch on a live lease:
        # two workers must never both believe they own the run.
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.store.claim_job_run(self.run.id, WORKER_A, now=self.at, lease=LEASE)
        stored = self.store.load_job_run(self.run.id)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(first.fencing_epoch, stored.fencing_epoch)

    def test_the_fencing_epoch_is_monotonic_across_takeovers(self) -> None:
        epochs = []
        moment = self.at
        for owner in (WORKER_A, WORKER_B, "worker-c", WORKER_A):
            lease = self.store.claim_job_run(
                self.run.id, owner, now=moment, lease=LEASE
            )
            epochs.append(lease.fencing_epoch)
            moment = moment + LEASE + timedelta(seconds=1)
        self.assertEqual([1, 2, 3, 4], epochs)
        self.assertEqual(sorted(set(epochs)), epochs)

    def test_the_lease_window_is_bounded_and_refused_never_clamped(self) -> None:
        for bad in (
            timedelta(0),
            timedelta(seconds=-1),
            MIN_JOB_LEASE - timedelta(seconds=1),
            MAX_JOB_LEASE + timedelta(seconds=1),
            timedelta(days=365),
        ):
            with self.assertRaisesRegex(JobError, "outside the approved"):
                self.store.claim_job_run(
                    self.run.id, WORKER_A, now=self.at, lease=bad
                )
        for bad in (300, "5m", None, 300.0):
            with self.assertRaisesRegex(JobError, "not a timedelta"):
                self.store.claim_job_run(
                    self.run.id, WORKER_A, now=self.at, lease=bad
                )
        # Nothing was written by any refusal, so no run silently gained a
        # clamped lease it did not ask for.
        self.assertIsNone(self.store.load_job_run(self.run.id).lease_expires_at_utc)
        # Both ends of the window are themselves accepted.
        for good in (MIN_JOB_LEASE, MAX_JOB_LEASE):
            self.store.release_job_run(
                self.run.id,
                WORKER_A,
                self.store.claim_job_run(
                    self.run.id, WORKER_A, now=self.at, lease=good
                ).fencing_epoch,
                now=self.at,
            )

    def test_a_malformed_owner_token_or_instant_is_refused(self) -> None:
        for bad in ("", "  spaced  ", None, 7, "x" * 5000, "nul\x00byte"):
            with self.assertRaises(JobError):
                self.store.claim_job_run(
                    self.run.id, bad, now=self.at, lease=LEASE
                )
        with self.assertRaises(JobError):
            self.store.claim_job_run(
                self.run.id, WORKER_A, now=self.at.replace(tzinfo=None), lease=LEASE
            )
        with self.assertRaisesRegex(JobError, "no usable generation"):
            self.store.claim_job_run(str(uuid4()), WORKER_A, now=self.at, lease=LEASE)

    def test_a_settled_generation_cannot_be_claimed(self) -> None:
        settle(self.store, self.run.id, JobRunStatus.COMPLETED, now=self.at)
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.store.claim_job_run(self.run.id, WORKER_A, now=self.at, lease=LEASE)


# --------------------------------------------------------------------------
# RENEWAL
# --------------------------------------------------------------------------


class RenewalTests(ClaimTestCase):
    """A renewal proves current ownership and moves only the expiry."""

    def setUp(self) -> None:
        super().setUp()
        self.at = NOW + timedelta(hours=1)
        self.run = self.due_run(self.at)
        self.lease = self.store.claim_job_run(
            self.run.id, WORKER_A, now=self.at, lease=LEASE
        )

    def test_the_current_owner_renews_and_nothing_else_moves(self) -> None:
        later = self.at + timedelta(minutes=4)
        renewed = self.store.renew_job_run_lease(
            self.run.id, WORKER_A, self.lease.fencing_epoch, now=later, lease=LEASE
        )
        self.assertEqual((later + LEASE).isoformat(), renewed.lease_expires_at_utc)
        # THE EPOCH DOES NOT ADVANCE: it marks acquisition, and renewing is
        # not acquiring - an advancing epoch would fence out the renewer.
        self.assertEqual(self.lease.fencing_epoch, renewed.fencing_epoch)
        stored = self.store.load_job_run(self.run.id)
        self.assertEqual(self.run.generation, stored.generation)
        self.assertEqual(self.run.job_revision, stored.job_revision)
        self.assertEqual(self.run.spec, stored.spec)
        self.assertEqual(self.run.snapshot_digest, stored.snapshot_digest)
        self.assertEqual(self.run.nominal_due_at_utc, stored.nominal_due_at_utc)
        self.assertEqual(self.lease.claimed_at, stored.claimed_at)

    def test_a_wrong_owner_is_refused(self) -> None:
        with self.assertRaisesRegex(JobError, "does not hold this lease"):
            self.store.renew_job_run_lease(
                self.run.id, WORKER_B, self.lease.fencing_epoch,
                now=self.at, lease=LEASE,
            )
        self.assertEqual(
            self.lease.lease_expires_at_utc,
            self.store.load_job_run(self.run.id).lease_expires_at_utc,
        )

    def test_a_stale_epoch_is_refused_even_with_the_right_token(self) -> None:
        # THE POINT OF FENCING. A displaced owner still remembers its token.
        for epoch in (0, self.lease.fencing_epoch - 1, self.lease.fencing_epoch + 1):
            with self.assertRaisesRegex(JobError, "does not hold this lease"):
                self.store.renew_job_run_lease(
                    self.run.id, WORKER_A, epoch, now=self.at, lease=LEASE
                )
        for bad in (None, "1", True, 1.0):
            with self.assertRaises(JobError):
                self.store.renew_job_run_lease(
                    self.run.id, WORKER_A, bad, now=self.at, lease=LEASE
                )

    def test_an_expired_lease_cannot_be_renewed(self) -> None:
        # Deterministic and fail-closed: by the expiry instant another worker
        # may legitimately own the run, and no store can un-expire it.
        expiry = datetime.fromisoformat(self.lease.lease_expires_at_utc)
        for moment in (expiry, expiry + timedelta(seconds=1)):
            with self.assertRaisesRegex(JobError, "expired"):
                self.store.renew_job_run_lease(
                    self.run.id, WORKER_A, self.lease.fencing_epoch,
                    now=moment, lease=LEASE,
                )
        # One microsecond before it, the same call still succeeds.
        self.assertTrue(
            self.store.renew_job_run_lease(
                self.run.id, WORKER_A, self.lease.fencing_epoch,
                now=expiry - timedelta(microseconds=1), lease=LEASE,
            ).lease_expires_at_utc
        )

    def test_a_settled_generation_cannot_renew(self) -> None:
        self.store.settle_job_run(
            self.run.id,
            JobRunStatus.FAILED,
            WORKER_A,
            self.lease.fencing_epoch,
            now=self.at,
        )
        with self.assertRaisesRegex(JobError, "already settled"):
            self.store.renew_job_run_lease(
                self.run.id, WORKER_A, self.lease.fencing_epoch,
                now=self.at, lease=LEASE,
            )

    def test_an_unleased_generation_cannot_renew(self) -> None:
        other = self.store.create_job(spec(label="second"), now=NOW)
        fresh = self.store.materialize_due_job_run(
            other.id, other.revision, now=self.at
        )
        with self.assertRaisesRegex(JobError, "carries no lease"):
            self.store.renew_job_run_lease(
                fresh.id, WORKER_A, 0, now=self.at, lease=LEASE
            )

    def test_the_renewal_lease_window_is_bounded_too(self) -> None:
        with self.assertRaisesRegex(JobError, "outside the approved"):
            self.store.renew_job_run_lease(
                self.run.id, WORKER_A, self.lease.fencing_epoch,
                now=self.at, lease=MAX_JOB_LEASE + timedelta(seconds=1),
            )


# --------------------------------------------------------------------------
# EXPIRY / RECLAIM / RELEASE
# --------------------------------------------------------------------------


class ReclaimTests(ClaimTestCase):
    """Expiry unblocks coordination, and NOTHING that could be unsafe."""

    def setUp(self) -> None:
        super().setUp()
        self.at = NOW + timedelta(hours=1)
        self.run = self.due_run(self.at)
        self.lease = self.store.claim_job_run(
            self.run.id, WORKER_A, now=self.at, lease=LEASE
        )
        self.expired = self.at + LEASE

    def test_an_expired_lease_is_reclaimed_with_a_strictly_newer_epoch(self) -> None:
        taken = self.store.claim_job_run(
            self.run.id, WORKER_B, now=self.expired, lease=LEASE
        )
        self.assertEqual(WORKER_B, taken.owner_token)
        self.assertGreater(taken.fencing_epoch, self.lease.fencing_epoch)
        self.assertEqual(self.lease.fencing_epoch + 1, taken.fencing_epoch)
        self.assertEqual(self.expired.isoformat(), taken.claimed_at)

    def test_the_displaced_owner_is_fenced_out_of_everything(self) -> None:
        self.store.claim_job_run(self.run.id, WORKER_B, now=self.expired, lease=LEASE)
        for call in (
            lambda: self.store.renew_job_run_lease(
                self.run.id, WORKER_A, self.lease.fencing_epoch,
                now=self.expired, lease=LEASE,
            ),
            lambda: self.store.release_job_run(
                self.run.id, WORKER_A, self.lease.fencing_epoch, now=self.expired
            ),
        ):
            with self.assertRaises(JobError):
                call()
        # The successor still holds it, untouched.
        self.assertEqual(WORKER_B, self.store.load_job_run(self.run.id).owner_token)

    def test_an_unexpired_lease_cannot_be_stolen(self) -> None:
        just_before = self.expired - timedelta(microseconds=1)
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.store.claim_job_run(
                self.run.id, WORKER_B, now=just_before, lease=LEASE
            )
        # A renewal genuinely extends the protection.
        self.store.renew_job_run_lease(
            self.run.id, WORKER_A, self.lease.fencing_epoch,
            now=just_before, lease=LEASE,
        )
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.store.claim_job_run(
                self.run.id, WORKER_B, now=self.expired, lease=LEASE
            )

    def test_an_unknown_outcome_is_never_reclaimed(self) -> None:
        # THE SAFETY PROPERTY. "We could not find out what happened" is not an
        # ending, and a lease expiring is not evidence that the previous
        # owner's external action did not occur. Reclaiming would risk a
        # duplicate external effect, so it FAILS CLOSED forever.
        self.force_status(self.run.id, JobRunStatus.UNKNOWN_OUTCOME)
        self.assertIn(JobRunStatus.UNKNOWN_OUTCOME, UNSETTLED_JOB_RUN_STATUSES)
        self.assertNotIn(JobRunStatus.UNKNOWN_OUTCOME, CLAIMABLE_JOB_RUN_STATUSES)
        for moment in (self.expired, self.at + timedelta(days=3650)):
            with self.assertRaisesRegex(JobError, "not claimable"):
                self.store.claim_job_run(
                    self.run.id, WORKER_B, now=moment, lease=LEASE
                )
        # And it BLOCKS every later generation rather than being stepped over.
        current = self.store.load_job(self.job.id)
        self.assertIsNone(
            self.store.materialize_due_job_run(
                self.job.id, current.revision, now=self.at + timedelta(days=3650)
            )
        )
        self.assertEqual(1, table_count(self.database, "job_runs"))

    def test_a_state_with_an_uncertain_external_outcome_is_never_reclaimed(
        self,
    ) -> None:
        for status in (JobRunStatus.RUNNING, JobRunStatus.AWAITING_APPROVAL):
            self.force_status(self.run.id, status)
            self.assertNotIn(status, CLAIMABLE_JOB_RUN_STATUSES)
            with self.assertRaisesRegex(JobError, "not claimable"):
                self.store.claim_job_run(
                    self.run.id, WORKER_B,
                    now=self.at + timedelta(days=1), lease=LEASE,
                )
            # Nor released out from under whatever may be in flight.
            with self.assertRaisesRegex(JobError, "not releasable"):
                self.store.release_job_run(
                    self.run.id, WORKER_A, self.lease.fencing_epoch, now=self.at
                )

    def test_release_returns_the_run_without_rewriting_anything(self) -> None:
        released = self.store.release_job_run(
            self.run.id, WORKER_A, self.lease.fencing_epoch, now=self.at
        )
        self.assertEqual(JobRunStatus.PENDING, released.status)
        self.assertIsNone(released.owner_token)
        self.assertIsNone(released.lease_expires_at_utc)
        # RELEASING SETTLES NOTHING and cancels nothing.
        self.assertFalse(released.settled)
        self.assertIsNone(released.settled_at)
        self.assertEqual(self.run.generation, released.generation)
        # The epoch is NOT rolled back: it is the durable record that an
        # ownership happened, and rolling it back would let the released
        # owner's stale proof match again.
        self.assertEqual(self.lease.fencing_epoch, released.fencing_epoch)
        with self.assertRaises(JobError):
            self.store.renew_job_run_lease(
                self.run.id, WORKER_A, self.lease.fencing_epoch,
                now=self.at, lease=LEASE,
            )
        # And the next worker need not wait the lease out.
        taken = self.store.claim_job_run(
            self.run.id, WORKER_B, now=self.at, lease=LEASE
        )
        self.assertGreater(taken.fencing_epoch, self.lease.fencing_epoch)

    def test_release_requires_the_exact_owner_and_epoch(self) -> None:
        for owner, epoch in (
            (WORKER_B, self.lease.fencing_epoch),
            (WORKER_A, self.lease.fencing_epoch + 1),
            (WORKER_A, 0),
        ):
            with self.assertRaises(JobError):
                self.store.release_job_run(
                    self.run.id, owner, epoch, now=self.at
                )
        self.assertEqual(WORKER_A, self.store.load_job_run(self.run.id).owner_token)


# --------------------------------------------------------------------------
# CRASH / RESTART
# --------------------------------------------------------------------------


class RestartTests(ClaimTestCase):
    """Coordination lives in the database, never in a process."""

    def reopen(self) -> SQLiteMemoryStore:
        """A brand-new store object over the same file: a restarted process.

        No in-memory registry survives this, so anything that still holds
        afterwards is durable by construction.
        """
        return SQLiteMemoryStore(self.database)

    def test_an_unexpired_lease_survives_a_restart(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        lease = self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        after = self.reopen()
        stored = after.load_job_run(run.id)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(lease.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(lease.lease_expires_at_utc, stored.lease_expires_at_utc)
        self.assertTrue(stored.lease_is_live(at + timedelta(minutes=1)))
        # A restarted peer still cannot steal it.
        with self.assertRaisesRegex(JobError, "not claimable"):
            after.claim_job_run(run.id, WORKER_B, now=at, lease=LEASE)
        # And the original owner can still renew through the new handle.
        after.renew_job_run_lease(
            run.id, WORKER_A, lease.fencing_epoch,
            now=at + timedelta(minutes=1), lease=LEASE,
        )

    def test_an_expired_reclaimable_lease_survives_and_may_be_reclaimed(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        first = self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        after = self.reopen()
        expired = at + LEASE
        self.assertFalse(after.load_job_run(run.id).lease_is_live(expired))
        taken = after.claim_job_run(run.id, WORKER_B, now=expired, lease=LEASE)
        self.assertEqual(first.fencing_epoch + 1, taken.fencing_epoch)

    def test_the_fencing_epoch_never_regresses_across_restarts(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        epochs, moment, store = [], at, self.store
        for owner in (WORKER_A, WORKER_B, "worker-c"):
            epochs.append(
                store.claim_job_run(run.id, owner, now=moment, lease=LEASE)
                .fencing_epoch
            )
            store = self.reopen()
            moment = moment + LEASE + timedelta(seconds=1)
        self.assertEqual([1, 2, 3], epochs)
        self.assertEqual(3, self.reopen().load_job_run(run.id).fencing_epoch)

    def test_an_unsafe_outcome_stays_blocked_across_a_restart(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        self.force_status(run.id, JobRunStatus.UNKNOWN_OUTCOME)
        after = self.reopen()
        with self.assertRaisesRegex(JobError, "not claimable"):
            after.claim_job_run(
                run.id, WORKER_B, now=at + timedelta(days=365), lease=LEASE
            )

    def test_a_half_written_lease_row_never_loads(self) -> None:
        # A crash between the two halves of a lease, or a hand-edited
        # database. The owner and the expiry are ONE fact; a row carrying half
        # of it is refused, so it can never be claimed, renewed or reclaimed.
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        rewrite_run(self.store, run.id, lease_expires_at_utc=None)
        self.assertIsNone(self.reopen().load_job_run(run.id))
        with self.assertRaisesRegex(JobError, "no usable generation"):
            self.store.claim_job_run(
                run.id, WORKER_B, now=at + timedelta(days=1), lease=LEASE
            )
        rewrite_run(
            self.store,
            run.id,
            owner_token=None,
            lease_expires_at_utc=(at + LEASE).isoformat(),
        )
        self.assertIsNone(self.store.load_job_run(run.id))


# --------------------------------------------------------------------------
# REVISION / LIFECYCLE
# --------------------------------------------------------------------------


class RevisionBindingTests(ClaimTestCase):
    """A generation is bound to the revision that produced it, for life."""

    def test_a_later_edit_never_mutates_an_open_generation(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        original = run.spec
        lease = self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        edited = self.store.edit_job(
            self.job.id, self.job.revision, spec(label="renamed"), now=at
        )
        self.assertEqual(2, edited.revision)
        stored = self.store.load_job_run(run.id)
        self.assertEqual(original, stored.spec)
        self.assertEqual(original.digest, stored.snapshot_digest)
        self.assertEqual(1, stored.job_revision)
        # The lease travels with neither the revision nor the generation: it
        # is still the same ownership of the same occurrence.
        self.assertEqual(lease.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.store.renew_job_run_lease(
            run.id, WORKER_A, lease.fencing_epoch,
            now=at + timedelta(minutes=1), lease=LEASE,
        )

    def test_a_lease_is_never_transferred_to_another_generation(self) -> None:
        at = NOW + timedelta(hours=1)
        first = self.due_run(at)
        lease = self.store.claim_job_run(first.id, WORKER_A, now=at, lease=LEASE)
        self.store.settle_job_run(
            first.id, JobRunStatus.COMPLETED, WORKER_A, lease.fencing_epoch,
            now=at,
        )
        second = self.store.materialize_due_job_run(
            self.job.id,
            self.store.load_job(self.job.id).revision,
            now=at + timedelta(hours=1),
        )
        self.assertEqual(2, second.generation)
        # The new generation is born unowned, at epoch zero.
        self.assertIsNone(second.owner_token)
        self.assertIsNone(second.lease_expires_at_utc)
        self.assertEqual(0, second.fencing_epoch)
        with self.assertRaises(JobError):
            self.store.renew_job_run_lease(
                second.id, WORKER_A, lease.fencing_epoch,
                now=at + timedelta(hours=1), lease=LEASE,
            )

    def test_disabling_does_not_pretend_an_active_run_was_cancelled(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        lease = self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        disabled = self.store.set_job_enabled(
            self.job.id, self.job.revision, False, now=at
        )
        self.assertIsNone(disabled.next_due_at_utc)
        stored = self.store.load_job_run(run.id)
        # STILL CLAIMED, still unsettled, still owned. Storage cancelled
        # nothing and must not claim it did.
        self.assertEqual(JobRunStatus.CLAIMED, stored.status)
        self.assertFalse(stored.settled)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(lease.lease_expires_at_utc, stored.lease_expires_at_utc)
        self.store.renew_job_run_lease(
            run.id, WORKER_A, lease.fencing_epoch,
            now=at + timedelta(minutes=1), lease=LEASE,
        )
        # Future eligibility, however, is genuinely gone.
        self.assertIsNone(
            self.store.materialize_due_job_run(
                self.job.id, disabled.revision, now=at + timedelta(days=7)
            )
        )

    def test_archiving_preserves_every_trace_of_the_run(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        lease = self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        self.store.settle_job_run(
            run.id, JobRunStatus.COMPLETED, WORKER_A, lease.fencing_epoch,
            now=at,
        )
        current = self.store.load_job(self.job.id)
        self.store.archive_job(self.job.id, current.revision)
        stored = self.store.load_job_run(run.id)
        self.assertIsNotNone(stored)
        self.assertEqual(lease.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(lease.claimed_at, stored.claimed_at)
        self.assertEqual(1, len(self.store.list_job_runs(self.job.id, 10)))


# --------------------------------------------------------------------------
# SAFE PROJECTIONS
# --------------------------------------------------------------------------


class ProjectionTests(ClaimTestCase):
    """A listing shows coordination lifecycle, never a worker's identity."""

    def test_the_projection_carries_safe_lease_metadata_only(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        before = self.store.list_job_runs(self.job.id, 10)[0]
        self.assertFalse(before.claimed)
        self.assertEqual(0, before.fencing_epoch)
        self.assertIsNone(before.lease_expires_at_utc)
        lease = self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        after = self.store.list_job_runs(self.job.id, 10)[0]
        self.assertTrue(after.claimed)
        self.assertEqual(lease.fencing_epoch, after.fencing_epoch)
        self.assertEqual(lease.lease_expires_at_utc, after.lease_expires_at_utc)
        self.assertEqual("claimed", after.status)
        # THE OWNER TOKEN NEVER CROSSES. It is opaque caller-supplied text this
        # store never inspects, so it cannot be asserted non-secret.
        payload = repr(after) + repr(self.store.list_jobs(10))
        self.assertNotIn(WORKER_A, payload)
        self.assertNotIn("owner_token", payload)
        # Nor does the declaration, the target or the snapshot.
        self.assertNotIn("notes.txt", payload)
        self.assertNotIn("summarize the log", payload)

    def test_an_unreadable_run_is_reported_rather_than_guessed_at(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        rewrite_run(self.store, run.id, lease_expires_at_utc="not a timestamp")
        summary = self.store.list_job_runs(self.job.id, 10)[0]
        self.assertFalse(summary.readable)
        self.assertIsNone(summary.fencing_epoch)
        self.assertIsNone(summary.claimed)


# --------------------------------------------------------------------------
# AUTHORITY BOUNDARY
# --------------------------------------------------------------------------


class AuthorityBoundaryTests(ClaimTestCase):
    """A lease is coordination. It grants NOTHING, proved from the durable state."""

    #: Every table a claim would have to touch in order to have granted
    #: something. All of them must stay empty across a full 2A lifecycle.
    AUTHORITY_TABLES = (
        "tasks",
        "sessions",
        "pending_approvals",
        "permission_grants",
        "execution_attempts",
        "attempt_billing",
        "usage_ledger",
        "decision_log",
    )

    def full_lifecycle(self) -> None:
        """Materialize, claim, renew, reclaim, release and settle. Everything."""
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        lease = self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        self.store.renew_job_run_lease(
            run.id, WORKER_A, lease.fencing_epoch,
            now=at + timedelta(minutes=1), lease=LEASE,
        )
        taken = self.store.claim_job_run(
            run.id, WORKER_B, now=at + timedelta(minutes=30), lease=LEASE
        )
        self.store.release_job_run(
            run.id, WORKER_B, taken.fencing_epoch, now=at + timedelta(minutes=31)
        )
        final = self.store.claim_job_run(
            run.id, "worker-c", now=at + timedelta(minutes=31), lease=LEASE
        )
        self.store.settle_job_run(
            run.id, JobRunStatus.COMPLETED, "worker-c", final.fencing_epoch,
            now=at + timedelta(minutes=32),
        )

    def test_a_full_lifecycle_creates_no_authority_row_of_any_kind(self) -> None:
        self.full_lifecycle()
        for table in self.AUTHORITY_TABLES:
            self.assertEqual(0, table_count(self.database, table), table)
        # The job and its one generation are the ONLY durable evidence.
        self.assertEqual(1, table_count(self.database, "jobs"))
        self.assertEqual(1, table_count(self.database, "job_runs"))

    def test_a_claim_creates_no_task_approval_grant_attempt_or_billing(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        stored = self.store.load_job_run(run.id)
        # No AgentCore materialization: the associations stay empty.
        self.assertIsNone(stored.task_id)
        self.assertIsNone(stored.session_id)
        self.assertIsNone(stored.chain_id)
        # No execution attempt is opened and none is referenced.
        self.assertIsNone(stored.attempt_id)
        for table in self.AUTHORITY_TABLES:
            self.assertEqual(0, table_count(self.database, table), table)

    def test_no_condition_is_evaluated_and_no_target_is_read(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at, kind=JobKind.CONDITION_WATCH)
        self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        stored = self.store.load_job_run(run.id)
        self.assertIsNone(stored.condition_result)
        self.assertIsNone(stored.blocking_code)
        self.assertEqual(0, table_count(self.database, "tasks"))

    def test_no_background_thread_or_service_is_started(self) -> None:
        before = set(threading.enumerate())
        self.full_lifecycle()
        self.assertEqual(before, set(threading.enumerate()))

    def test_an_unknown_outcome_is_never_retried_automatically(self) -> None:
        at = NOW + timedelta(hours=1)
        run = self.due_run(at)
        self.store.claim_job_run(run.id, WORKER_A, now=at, lease=LEASE)
        self.force_status(run.id, JobRunStatus.UNKNOWN_OUTCOME)
        # Time passing changes nothing. There is no retry, no sweep, and no
        # instant at which the store decides on its own to try again.
        for moment in (
            at + LEASE,
            at + timedelta(days=1),
            at + timedelta(days=3650),
        ):
            current = self.store.load_job(self.job.id)
            self.assertIsNone(
                self.store.materialize_due_job_run(
                    self.job.id, current.revision, now=moment
                )
            )
            with self.assertRaises(JobError):
                self.store.claim_job_run(
                    run.id, WORKER_B, now=moment, lease=LEASE
                )
        self.assertEqual(
            JobRunStatus.UNKNOWN_OUTCOME, self.store.load_job_run(run.id).status
        )

    def test_the_claimable_set_is_the_narrowest_safe_one(self) -> None:
        self.assertEqual(
            frozenset({JobRunStatus.PENDING, JobRunStatus.CLAIMED}),
            CLAIMABLE_JOB_RUN_STATUSES,
        )
        # Every claimable state is unsettled, and every state whose external
        # outcome could be in doubt is excluded.
        self.assertTrue(CLAIMABLE_JOB_RUN_STATUSES <= UNSETTLED_JOB_RUN_STATUSES)
        self.assertEqual(
            frozenset(), CLAIMABLE_JOB_RUN_STATUSES & SETTLED_JOB_RUN_STATUSES
        )
        for unsafe in (
            JobRunStatus.RUNNING,
            JobRunStatus.AWAITING_APPROVAL,
            JobRunStatus.UNKNOWN_OUTCOME,
        ):
            self.assertNotIn(unsafe, CLAIMABLE_JOB_RUN_STATUSES)


class SourceBoundaryTests(unittest.TestCase):
    """The absent capabilities, proved from the shipped source."""

    def production_modules(self) -> list[Path]:
        root = Path(__file__).resolve().parent.parent / "jarvis"
        return sorted(root.rglob("*.py"))

    def test_no_production_module_calls_the_scheduler_storage_surface(self) -> None:
        # Phase 2B1 adds exactly ONE production caller: ``dispatch.py``, the
        # one-shot bridge. It is allowed to name the storage surface and is
        # separately proved - below and in the 2B1 suite - to reach no
        # approval, grant, billing, connector, attempt or condition authority.
        #
        # Phase 2C adds a SECOND, narrower one: ``condition_watch.py`` may
        # read a generation and settle an unbound watch - ``load_job_run`` and
        # ``settle_job_run`` only, with ``load_job`` allowed alongside
        # ``load_job_run`` purely because it is a literal substring of it, not
        # a second name this module calls. Every other scheduler storage
        # operation stays forbidden to it, same as to everything but
        # ``dispatch.py`` - binding still runs through that module alone.
        universal = {"memory.py", "contracts.py", "dispatch.py"}
        per_module_extra = {
            "condition_watch.py": {"load_job", "load_job_run", "settle_job_run"}
        }
        for module in self.production_modules():
            if module.name in universal:
                continue
            source = module.read_text(encoding="utf-8")
            extra = per_module_extra.get(module.name, frozenset())
            for name in SCHEDULER_SURFACE:
                if name in extra:
                    continue
                self.assertNotIn(name, source, f"{module.name} mentions {name}")

    def test_the_coordination_methods_reach_no_execution_authority(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent / "jarvis" / "memory.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        store = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SQLiteMemoryStore"
        )
        forbidden = (
            "execution_attempts",
            "attempt_billing",
            "pending_approvals",
            "permission_grants",
            "usage_ledger",
            "INSERT INTO tasks",
            "INSERT INTO sessions",
            "threading",
            "Thread",
            "registry",
            "connector",
        )
        covered = {
            "materialize_due_job_run",
            "claim_job_run",
            "renew_job_run_lease",
            "release_job_run",
            "_lease_expiry",
            "_owned_run",
            "_held_lease",
            "settle_job_run",
            "_fenced_transition",
            "mark_job_run_unknown",
            "await_job_run_approval",
            "_lease_result",
            "_insert_job_run",
        }
        seen = set()
        for node in store.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in covered:
                continue
            seen.add(node.name)
            # The CODE, without its prose. ``ast.unparse`` drops comments, and
            # the docstring is dropped explicitly: a method whose docstring
            # says it reaches no connector is the guard working, not failing.
            stripped = copy.deepcopy(node)
            if (
                stripped.body
                and isinstance(stripped.body[0], ast.Expr)
                and isinstance(stripped.body[0].value, ast.Constant)
                and isinstance(stripped.body[0].value.value, str)
            ):
                del stripped.body[0]
            body = ast.unparse(stripped)
            for needle in forbidden:
                self.assertNotIn(needle, body, f"{node.name} reaches {needle}")
        # The guard is worthless if it silently covers nothing.
        self.assertEqual(covered, seen)

    def test_no_autonomous_scheduler_machinery_exists(self) -> None:
        # A lease can expire. NOTHING in this build notices on its own: there
        # is no heartbeat, no timer-driven reclaim, no orphan sweep and no
        # due-job scanner. Every expiry judgement happens because a caller
        # asked, at an instant the caller supplied.
        for module in self.production_modules():
            source = module.read_text(encoding="utf-8")
            for forbidden in (
                "def heartbeat",
                "heartbeat_at",
                "auto_reclaim",
                "orphan_recovery",
                "def due_jobs",
                "def scan_due",
                "def run_scheduler",
            ):
                self.assertNotIn(forbidden, source, f"{module.name}: {forbidden}")

    def test_no_background_scheduler_module_exists(self) -> None:
        names = {module.name for module in self.production_modules()}
        for absent in ("dispatcher.py", "scheduler.py", "worker.py", "daemon.py"):
            self.assertNotIn(absent, names)

    def test_the_lease_bounds_are_explicit_and_sane(self) -> None:
        self.assertLess(timedelta(0), MIN_JOB_LEASE)
        self.assertLess(MIN_JOB_LEASE, MAX_JOB_LEASE)
        self.assertLessEqual(MAX_JOB_LEASE, timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
