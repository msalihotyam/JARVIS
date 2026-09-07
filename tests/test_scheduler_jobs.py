"""Scheduler phase 1B: durable job persistence foundation.

Covers the canonical durable job definition and generation contracts, their
canonical persisted encodings of the phase-1A pure schedule/condition kernels,
the additive SQLite 7 -> 8 migration, revision compare-and-set, the
enable/disable/archive management semantics, occurrence identity, the
one-unsettled-generation invariant, bounded provider-neutral projections and
fail-closed corruption behaviour.

The phase boundary is itself under test. Claim / lease / fencing coordination
arrived in phase 2A and is covered by ``tests/test_scheduler_claims.py``;
what is still absent is a dispatcher, a background worker, AgentCore
materialization, approval creation and condition evaluation, and
``ExecutionBoundaryTests`` proves that from the shipped source rather than
from this paragraph.

No test performs a live model, auth or network call; every database is a
disposable local temporary file.

TIMEZONE DATABASE. The calendar cases need real DST rules, and a bare Windows
CPython has no ``tzdata``. The phase-1A schedule suite already solves this
exactly once, so its module fixture is imported and reused here rather than
copied: where a real database is present it is used, and only otherwise are
the two handwritten TZif zones installed for the duration of the run.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from jarvis.conditions import ConditionError, ConditionKind, LocalReadCondition
from jarvis.contracts import (
    JOB_DEFINITION_VERSION,
    JOB_LABEL_MAX_CHARS,
    JOB_SPEC_MAX_BYTES,
    SETTLED_JOB_RUN_STATUSES,
    UNSETTLED_JOB_RUN_STATUSES,
    VISIBILITY_MAX_LIMIT,
    ChainStep,
    ErrorCode,
    JobError,
    JobKind,
    JobRecord,
    JobRun,
    JobRunStatus,
    JobSpec,
    ProjectRecord,
    canonical_utc_timestamp,
    condition_from_dict,
    condition_to_dict,
    job_identifier_refusal,
    schedule_from_dict,
    schedule_to_dict,
    utc_now,
)
from jarvis.memory import SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.schedules import Daily, Interval, OneTime, ScheduleError, Weekday, Weekly
from tests.helpers import safe_cleanup

# The phase-1A timezone fixture, reused verbatim. Rebinding the module hooks
# is what makes ``unittest`` install and remove the fallback around THIS
# module's tests too; duplicating the TZif builder would mean two sets of
# handwritten DST rules to keep in agreement.
from tests.test_schedules import (  # noqa: F401
    BERLIN,
    NEW_YORK,
    setUpModule,
    tearDownModule,
)


#: A fixed planning instant. Every calculation in this suite is asked from an
#: EXPLICIT moment, so no assertion depends on when the suite runs.
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

#: A sha256 digest in the canonical form the condition kernel requires.
DIGEST = "a" * 64

#: The raw DDL of a REAL schema-7 database, written by hand rather than
#: generated from the code under test: a fixture built by the current build
#: could not prove that the current build leaves it alone.
_V7_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    project_id TEXT);
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL);
CREATE TABLE project_memory_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    classification TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'inferred',
    origin_detail TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE);
CREATE TABLE pending_approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE);
CREATE TABLE permission_grants (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    scope_fingerprint TEXT NOT NULL,
    grant_json TEXT NOT NULL,
    approved INTEGER NOT NULL,
    remember_for_session INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_by_attempt TEXT,
    FOREIGN KEY(request_id) REFERENCES pending_approvals(id),
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE);
CREATE TABLE execution_attempts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    connector TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE);
CREATE TABLE attempt_billing (
    attempt_id TEXT PRIMARY KEY,
    usage_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    ledger_month TEXT NOT NULL,
    billing_class TEXT NOT NULL,
    reserved_nano_usd INTEGER NOT NULL,
    state TEXT NOT NULL,
    actual_nano_usd INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(attempt_id) REFERENCES execution_attempts(id)
        ON DELETE CASCADE);
CREATE TABLE decision_log (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE);
CREATE TABLE event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    task_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE);
CREATE TABLE loop_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE);
CREATE TABLE usage_ledger (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    task_id TEXT,
    provider TEXT,
    model TEXT,
    connector TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    actual_cost_usd REAL NOT NULL,
    is_paid INTEGER NOT NULL,
    billing_class TEXT,
    monetary_state TEXT,
    monetary_cost_usd REAL,
    reference_cost_usd REAL,
    call_count INTEGER,
    billing_evidence TEXT,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL);
CREATE INDEX idx_pmi_project_active
    ON project_memory_items(project_id, active);
CREATE INDEX idx_pending_task
    ON pending_approvals(task_id, session_id, status);
CREATE INDEX idx_grant_scope
    ON permission_grants(session_id, scope_fingerprint, approved, expires_at);
CREATE INDEX idx_attempt_task ON execution_attempts(task_id);
CREATE INDEX idx_attempt_billing_month
    ON attempt_billing(ledger_month, state);
"""

#: Legacy rows across the authority tables that must survive an upgrade
#: byte-for-byte.
_V7_ROWS = """
INSERT INTO schema_version (version) VALUES (7);
INSERT INTO schema_migrations VALUES (6, 't6');
INSERT INTO schema_migrations VALUES (7, 't7');
INSERT INTO projects VALUES ('proj-old', 'Old Project', 1, 't0');
INSERT INTO sessions VALUES ('s-old', '{"id": "s-old"}', 'u0', 'proj-old');
INSERT INTO tasks VALUES
    ('t-old', 's-old', '{"id": "t-old"}', 'completed', 'persist', 'u0');
INSERT INTO project_memory_items VALUES
    ('m-old', 'proj-old', 'fact', 'a durable legacy fact', 'internal',
     'inferred', '', 1, NULL, 'c0', 'u0');
INSERT INTO pending_approvals VALUES
    ('a-old', 't-old', 's-old', '{}', 'approved', 'c0', 'e0', 'r0');
INSERT INTO permission_grants VALUES
    ('g-old', 'a-old', 't-old', 's-old', 'fp', 'sfp', '{}', 1, 0, 'e0',
     'c0', NULL);
INSERT INTO execution_attempts VALUES
    ('att-old', 's-old', 't-old', 'dummy', 'fp', 'finished', 'c0', 'u0');
INSERT INTO attempt_billing VALUES
    ('att-old', 'u-old', 's-old', 't-old', '2026-08',
     'no_additional_charge', 0, 'reconciled', 0, 'c0', 'u0');
INSERT INTO usage_ledger VALUES
    ('u-old', 's-old', 't-old', 'p', 'm', 'dummy', 1, 1, 0.0, 0.0, 0,
     'no_additional_charge', 'not_applicable', NULL, NULL, 1, 'ev', 'r0');
"""

#: Exactly the durable objects schemas 8 and 9 introduce. Anything else
#: appearing in ``sqlite_master`` after an upgrade would be an unrelated schema
#: change. Schema 9 adds a COLUMN rather than an object, so this set is
#: unchanged by it and the column is asserted separately.
_SCHEDULER_OBJECTS = frozenset(
    {"jobs", "job_runs", "idx_jobs_due", "idx_job_runs_unsettled"}
)


def step(objective: str = "summarize the log", target: str = "notes.txt") -> ChainStep:
    """One inert declared step; the same grammar a chain step uses."""
    return ChainStep(
        objective=objective, capability="echo", action="read", target=target
    )


def one_time_schedule(at: datetime | None = None) -> OneTime:
    return OneTime(at=at or datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc))


def interval_schedule(hours: int = 1) -> Interval:
    return Interval(anchor=NOW, every=timedelta(hours=hours))


def spec(
    *,
    kind: JobKind = JobKind.RECURRING,
    project_id: str = "proj-a",
    label: str = "hourly digest",
    schedule=None,
    steps: tuple[ChainStep, ...] | None = None,
    condition: LocalReadCondition | None = None,
    on_true: tuple[ChainStep, ...] = (),
) -> JobSpec:
    if schedule is None:
        schedule = (
            one_time_schedule() if kind is JobKind.ONE_TIME else interval_schedule()
        )
    if kind is JobKind.CONDITION_WATCH:
        return JobSpec(
            project_id=project_id,
            kind=kind,
            label=label,
            schedule=schedule,
            condition=condition
            or LocalReadCondition(
                kind=ConditionKind.LOCAL_READ_SHA256_DIFFERS,
                reference="notes.txt",
                expected_sha256=DIGEST,
            ),
            on_true=on_true or (step(),),
        )
    return JobSpec(
        project_id=project_id,
        kind=kind,
        label=label,
        schedule=schedule,
        steps=steps if steps is not None else (step(),),
    )


def raw(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    return connection


def job_row(store: SQLiteMemoryStore, job_id: str) -> dict:
    """One persisted job row as a plain mapping, bypassing every loader."""
    connection = raw(store.database_path)
    try:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row)
    finally:
        connection.close()


def rewrite_job(
    store: SQLiteMemoryStore, job_id: str, *, bypass_checks: bool = False, **columns
) -> None:
    """Tamper with one persisted job row exactly as an editor could.

    ``bypass_checks`` disables SQL CHECK enforcement for the write only, which
    is how a row that the schema itself forbids - the state a hand-edited or
    downgraded database can still hold - reaches the loader under test.
    """
    connection = raw(store.database_path)
    try:
        if bypass_checks:
            connection.execute("PRAGMA ignore_check_constraints = ON")
        assignments = ", ".join(f"{name} = ?" for name in columns)
        connection.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?",
            (*columns.values(), job_id),
        )
        connection.commit()
    finally:
        connection.close()


def rewrite_run(
    store: SQLiteMemoryStore, run_id: str, *, bypass_checks: bool = False, **columns
) -> None:
    connection = raw(store.database_path)
    try:
        if bypass_checks:
            connection.execute("PRAGMA ignore_check_constraints = ON")
        assignments = ", ".join(f"{name} = ?" for name in columns)
        connection.execute(
            f"UPDATE job_runs SET {assignments} WHERE id = ?",
            (*columns.values(), run_id),
        )
        connection.commit()
    finally:
        connection.close()


class JobStoreTestCase(unittest.TestCase):
    """A fresh schema-8 store with one active project to hang jobs on."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.store = SQLiteMemoryStore(self.database)
        self.store.save_project(ProjectRecord(id="proj-a", name="Project A"))

    def create(self, **kwargs) -> JobRecord:
        return self.store.create_job(spec(**kwargs), now=NOW)


# --------------------------------------------------------------------------
# SCHEMA
# --------------------------------------------------------------------------


class SchemaMigrationTests(unittest.TestCase):
    """The additive 7 -> 8 -> 9 upgrade, on empty and populated databases.

    A schema-7 fixture now crosses TWO additive migrations in one open, so
    these also prove that the chain composes: the tables schema 8 adds and the
    lease column schema 9 adds appear together, and nothing else does.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"

    def build_v7(self, *, populated: bool) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(_V7_SCHEMA)
            if populated:
                connection.executescript(_V7_ROWS)
            else:
                connection.execute("INSERT INTO schema_version VALUES (7)")
            connection.commit()
        finally:
            connection.close()

    def schema_objects(self) -> dict[str, str]:
        connection = sqlite3.connect(self.database)
        try:
            return {
                name: sql
                for name, sql in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
                ).fetchall()
            }
        finally:
            connection.close()

    def test_empty_schema_seven_database_upgrades(self) -> None:
        self.build_v7(populated=False)
        before = self.schema_objects()
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(9, SCHEMA_VERSION)
        self.assertEqual(9, store.schema_version())
        after = self.schema_objects()
        self.assertEqual(_SCHEDULER_OBJECTS, set(after) - set(before))
        # Every pre-existing object is byte-identical: the upgrade adds, and
        # touches nothing that was already there.
        for name, sql in before.items():
            self.assertEqual(sql, after[name], name)

    def test_populated_schema_seven_database_upgrades_without_data_loss(self) -> None:
        self.build_v7(populated=True)
        before = self.schema_objects()
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(9, store.schema_version())
        after = self.schema_objects()
        self.assertEqual(_SCHEDULER_OBJECTS, set(after) - set(before))
        connection = raw(self.database)
        try:
            # Legacy rows across every authority table survive untouched.
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
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE {column} = ?", (identifier,)
                ).fetchone()
                self.assertIsNotNone(row, table)
            grant = connection.execute(
                "SELECT approved, consumed_by_attempt FROM permission_grants "
                "WHERE id = 'g-old'"
            ).fetchone()
            self.assertEqual(1, grant["approved"])
            self.assertIsNone(grant["consumed_by_attempt"])
            billing = connection.execute(
                "SELECT state, reserved_nano_usd FROM attempt_billing "
                "WHERE attempt_id = 'att-old'"
            ).fetchone()
            self.assertEqual("reconciled", billing["state"])
            self.assertEqual(0, billing["reserved_nano_usd"])
            memory = connection.execute(
                "SELECT kind, text, origin, origin_detail FROM "
                "project_memory_items WHERE id = 'm-old'"
            ).fetchone()
            self.assertEqual("fact", memory["kind"])
            self.assertEqual("a durable legacy fact", memory["text"])
            self.assertEqual("inferred", memory["origin"])
            self.assertEqual("", memory["origin_detail"])
            # No authority is invented: the upgrade creates EMPTY tables.
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            )
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0]
            )
            self.assertEqual(
                [6, 7, 8, 9],
                [
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ],
            )
        finally:
            connection.close()

    def test_reopening_after_upgrade_is_idempotent(self) -> None:
        self.build_v7(populated=True)
        SQLiteMemoryStore(self.database)
        first = self.schema_objects()
        again = SQLiteMemoryStore(self.database)
        self.assertEqual(9, again.schema_version())
        self.assertEqual(first, self.schema_objects())
        connection = raw(self.database)
        try:
            self.assertEqual(
                [6, 7, 8, 9],
                [
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ],
            )
        finally:
            connection.close()

    def test_an_already_current_schema_database_opens_unchanged(self) -> None:
        store = SQLiteMemoryStore(self.database)
        store.save_project(ProjectRecord(id="proj-a", name="A"))
        record = store.create_job(spec(), now=NOW)
        objects = self.schema_objects()
        reopened = SQLiteMemoryStore(self.database)
        self.assertEqual(9, reopened.schema_version())
        self.assertEqual(objects, self.schema_objects())
        self.assertEqual(record, reopened.load_job(record.id))

    def test_output_schema_version_is_unchanged(self) -> None:
        # The SQLite storage schema advances to 9 and NOTHING else does: the
        # user-facing output envelope contract stays at 6, and the durable job
        # definition contract is its own separate surface at 1. Three policy
        # surfaces, none of them an alias of another.
        from jarvis.app import OUTPUT_SCHEMA_VERSION

        self.assertEqual(9, SCHEMA_VERSION)
        self.assertEqual(6, OUTPUT_SCHEMA_VERSION)
        self.assertEqual(1, JOB_DEFINITION_VERSION)


# --------------------------------------------------------------------------
# JOB CREATION
# --------------------------------------------------------------------------


class JobCreationTests(JobStoreTestCase):
    def test_one_time_job_round_trips(self) -> None:
        at = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)
        record = self.create(kind=JobKind.ONE_TIME, schedule=one_time_schedule(at))
        self.assertEqual(JobKind.ONE_TIME, record.spec.kind)
        self.assertEqual(1, record.revision)
        self.assertEqual(1, record.next_generation)
        self.assertTrue(record.enabled)
        self.assertFalse(record.archived)
        self.assertEqual(at.isoformat(), record.next_due_at_utc)
        self.assertIsNone(job_identifier_refusal(record.id))
        loaded = self.store.load_job(record.id)
        self.assertEqual(record, loaded)
        self.assertEqual(at, loaded.spec.schedule.at)

    def test_a_one_time_schedule_already_past_yields_no_next_due(self) -> None:
        record = self.create(
            kind=JobKind.ONE_TIME,
            schedule=one_time_schedule(NOW - timedelta(days=1)),
        )
        self.assertIsNone(record.next_due_at_utc)
        self.assertTrue(record.schedulable)

    def test_interval_job_round_trips(self) -> None:
        record = self.create(schedule=interval_schedule(hours=6))
        loaded = self.store.load_job(record.id)
        self.assertEqual(timedelta(hours=6), loaded.spec.schedule.every)
        self.assertEqual(NOW, loaded.spec.schedule.anchor)
        self.assertEqual((NOW + timedelta(hours=6)).isoformat(), loaded.next_due_at_utc)
        self.assertEqual("", loaded.spec.timezone)

    def test_daily_job_persists_its_explicit_timezone(self) -> None:
        schedule = Daily(timezone=NEW_YORK, at=time(9, 30))
        record = self.create(schedule=schedule)
        loaded = self.store.load_job(record.id)
        self.assertEqual(NEW_YORK, loaded.spec.schedule.timezone)
        self.assertEqual(NEW_YORK, loaded.spec.timezone)
        self.assertEqual(time(9, 30), loaded.spec.schedule.at)
        # The stored instant is the one the NAMED zone produces, not one the
        # machine's own timezone would.
        self.assertEqual(
            "2026-09-06T13:30:00+00:00", loaded.next_due_at_utc
        )

    def test_weekly_job_round_trips_its_day_set(self) -> None:
        schedule = Weekly(
            timezone=BERLIN,
            at=time(7, 15),
            days=frozenset({Weekday.MONDAY, Weekday.FRIDAY}),
        )
        record = self.create(schedule=schedule)
        loaded = self.store.load_job(record.id)
        self.assertEqual(
            frozenset({Weekday.MONDAY, Weekday.FRIDAY}), loaded.spec.schedule.days
        )
        self.assertEqual(BERLIN, loaded.spec.timezone)
        # 2026-09-06 is a Sunday; the next declared day is Monday 07:15 CEST.
        self.assertEqual("2026-09-07T05:15:00+00:00", loaded.next_due_at_utc)

    def test_condition_watch_job_round_trips(self) -> None:
        record = self.create(kind=JobKind.CONDITION_WATCH)
        loaded = self.store.load_job(record.id)
        self.assertEqual(JobKind.CONDITION_WATCH, loaded.spec.kind)
        self.assertEqual(
            ConditionKind.LOCAL_READ_SHA256_DIFFERS, loaded.spec.condition.kind
        )
        self.assertEqual(DIGEST, loaded.spec.condition.expected_sha256)
        self.assertEqual((), loaded.spec.steps)
        self.assertEqual(1, len(loaded.spec.on_true))

    def test_an_invalid_schedule_is_refused(self) -> None:
        # Below the approved interval floor, and a naive instant: both fail at
        # the pure kernel, which storage never works around.
        with self.assertRaises(ScheduleError):
            Interval(anchor=NOW, every=timedelta(minutes=1))
        with self.assertRaises(ScheduleError):
            OneTime(at=datetime(2026, 9, 7, 3, 0))
        with self.assertRaises(JobError):
            spec(kind=JobKind.ONE_TIME, schedule=interval_schedule())
        with self.assertRaises(JobError):
            spec(kind=JobKind.RECURRING, schedule=one_time_schedule())
        with self.assertRaises(ScheduleError):
            schedule_from_dict({"kind": "cron", "at": "* * * * *"})

    def test_an_invalid_condition_is_refused(self) -> None:
        with self.assertRaises(ConditionError):
            LocalReadCondition(
                kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
                reference="notes.txt",
                expected_sha256="NOTADIGEST",
            )
        # A watch without a predicate, and a non-watch carrying one.
        with self.assertRaises(JobError):
            JobSpec(
                project_id="proj-a",
                kind=JobKind.CONDITION_WATCH,
                label="watch",
                schedule=interval_schedule(),
                on_true=(step(),),
            )
        with self.assertRaises(JobError):
            JobSpec(
                project_id="proj-a",
                kind=JobKind.RECURRING,
                label="not a watch",
                schedule=interval_schedule(),
                steps=(step(),),
                condition=LocalReadCondition(
                    kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
                    reference="notes.txt",
                    expected_sha256=DIGEST,
                ),
            )
        with self.assertRaises(ConditionError):
            condition_from_dict(
                {
                    "kind": "shell_exit_code",
                    "reference": "notes.txt",
                    "expected_sha256": DIGEST,
                }
            )

    def test_an_oversized_specification_is_refused_by_storage(self) -> None:
        # The declaration grammar's per-field ceilings bind long before the
        # whole-document one, so an oversized specification cannot be built
        # through the contract at all. The byte ceiling is the backstop for a
        # writer that did not come through it, and it is enforced by the table
        # rather than by application code.
        record = self.create()
        row = job_row(self.store, record.id)
        connection = raw(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE jobs SET spec_json = ? WHERE id = ?",
                    ("x" * (JOB_SPEC_MAX_BYTES + 1), record.id),
                )
        finally:
            connection.close()
        self.assertEqual(row, job_row(self.store, record.id))
        # A declaration field beyond its own grammar ceiling is refused too.
        with self.assertRaises(JobError):
            spec(steps=(step(objective="o" * 5000),))

    def test_unknown_keys_are_refused_by_every_parser(self) -> None:
        document = spec().to_dict()
        with self.assertRaises(JobError):
            JobSpec.from_dict({**document, "on_failure": []})
        with self.assertRaises(JobError):
            JobSpec.from_dict({k: v for k, v in document.items() if k != "label"})
        with self.assertRaises(ScheduleError):
            schedule_from_dict({**document["schedule"], "jitter_seconds": 5})
        with self.assertRaises(JobError):
            JobSpec.from_dict(
                {**document, "steps": [{**document["steps"][0], "task_id": "t-1"}]}
            )
        watch = spec(kind=JobKind.CONDITION_WATCH).to_dict()
        with self.assertRaises(ConditionError):
            condition_from_dict({**watch["condition"], "path": "/etc/passwd"})

    def test_creating_a_job_creates_no_task_approval_attempt_or_billing(self) -> None:
        self.create()
        self.create(kind=JobKind.ONE_TIME)
        connection = raw(self.database)
        try:
            for table in (
                "sessions",
                "tasks",
                "pending_approvals",
                "permission_grants",
                "execution_attempts",
                "attempt_billing",
                "usage_ledger",
                "decision_log",
                "event_log",
                "loop_checkpoints",
                "job_runs",
            ):
                self.assertEqual(
                    0,
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    table,
                )
            self.assertEqual(
                2, connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            )
        finally:
            connection.close()

    def test_a_job_requires_an_existing_active_project(self) -> None:
        with self.assertRaises(JobError):
            self.store.create_job(spec(project_id="proj-missing"), now=NOW)
        with self.assertRaises(JobError):
            self.store.create_job(spec(), now=NOW.replace(tzinfo=None))
        with self.assertRaises(JobError):
            self.store.create_job(object(), now=NOW)  # type: ignore[arg-type]

    def test_the_label_is_sanitized_and_bounded(self) -> None:
        record = self.create(label="report for api_key=HUNTER2 " + "x" * 400)
        self.assertNotIn("HUNTER2", record.spec.label)
        self.assertLessEqual(len(record.spec.label), JOB_LABEL_MAX_CHARS)
        with self.assertRaises(JobError):
            spec(label="   ")


# --------------------------------------------------------------------------
# REVISION
# --------------------------------------------------------------------------


class RevisionTests(JobStoreTestCase):
    def test_editing_increments_the_revision(self) -> None:
        record = self.create()
        edited = self.store.edit_job(
            record.id, record.revision, spec(label="renamed"), now=NOW
        )
        self.assertEqual(2, edited.revision)
        self.assertEqual("renamed", edited.spec.label)
        self.assertEqual(edited, self.store.load_job(record.id))
        self.assertNotEqual(record.spec.digest, edited.spec.digest)

    def test_a_stale_revision_is_refused_deterministically(self) -> None:
        record = self.create()
        self.store.edit_job(record.id, record.revision, spec(label="one"), now=NOW)
        for _ in range(2):
            with self.assertRaisesRegex(JobError, "stale job revision"):
                self.store.edit_job(
                    record.id, record.revision, spec(label="two"), now=NOW
                )
        self.assertEqual("one", self.store.load_job(record.id).spec.label)
        with self.assertRaises(JobError):
            self.store.edit_job(record.id, True, spec(), now=NOW)  # type: ignore[arg-type]
        with self.assertRaises(JobError):
            self.store.edit_job(str(uuid4()), 1, spec(), now=NOW)

    def test_an_old_generation_snapshot_stays_immutable(self) -> None:
        record = self.create()
        original = record.spec
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        self.store.edit_job(
            record.id,
            record.revision,
            spec(label="a different job entirely", schedule=interval_schedule(3)),
            now=NOW,
        )
        reloaded = self.store.load_job_run(run.id)
        self.assertEqual(original, reloaded.spec)
        self.assertEqual(original.digest, reloaded.snapshot_digest)
        self.assertEqual(1, reloaded.job_revision)
        self.assertEqual(2, self.store.load_job(record.id).revision)

    def test_a_scope_change_creates_a_revision(self) -> None:
        self.store.save_project(ProjectRecord(id="proj-b", name="Project B"))
        record = self.create()
        edited = self.store.edit_job(
            record.id, record.revision, spec(project_id="proj-b"), now=NOW
        )
        self.assertEqual(2, edited.revision)
        self.assertEqual("proj-b", edited.project_id)
        self.assertNotEqual(record.spec.digest, edited.spec.digest)
        self.assertEqual("proj-b", job_row(self.store, record.id)["project_id"])

    def test_a_schedule_change_creates_a_revision_and_moves_the_next_due(self) -> None:
        record = self.create(schedule=interval_schedule(1))
        edited = self.store.edit_job(
            record.id, record.revision, spec(schedule=interval_schedule(4)), now=NOW
        )
        self.assertEqual(2, edited.revision)
        self.assertEqual((NOW + timedelta(hours=4)).isoformat(), edited.next_due_at_utc)
        self.assertNotEqual(record.next_due_at_utc, edited.next_due_at_utc)

    def test_an_archived_job_cannot_be_edited(self) -> None:
        record = self.create()
        archived = self.store.archive_job(record.id, record.revision)
        with self.assertRaises(JobError):
            self.store.edit_job(record.id, archived.revision, spec(), now=NOW)


# --------------------------------------------------------------------------
# STATE
# --------------------------------------------------------------------------


class StateTests(JobStoreTestCase):
    def test_disable_clears_scheduling_eligibility(self) -> None:
        record = self.create()
        disabled = self.store.set_job_enabled(record.id, record.revision, False, now=NOW)
        self.assertFalse(disabled.enabled)
        self.assertIsNone(disabled.next_due_at_utc)
        self.assertFalse(disabled.schedulable)
        # Disabling is not a claim that anything was cancelled: the revision,
        # the definition and the run history are all untouched.
        self.assertEqual(record.revision, disabled.revision)
        self.assertEqual(record.spec, disabled.spec)
        with self.assertRaises(JobError):
            self.store.record_job_run(record.id, record.next_due_at_utc)

    def test_enable_recalculates_a_fresh_scheduling_epoch(self) -> None:
        record = self.create(schedule=interval_schedule(1))
        disabled = self.store.set_job_enabled(record.id, record.revision, False, now=NOW)
        later = NOW + timedelta(hours=5, minutes=30)
        enabled = self.store.set_job_enabled(
            record.id, disabled.revision, True, now=later
        )
        self.assertTrue(enabled.enabled)
        # Forward from the moment of re-enabling; the occurrences named while
        # the job was off are not recovered.
        self.assertEqual((NOW + timedelta(hours=6)).isoformat(), enabled.next_due_at_utc)

    def test_enable_never_revives_a_settled_generation(self) -> None:
        record = self.create(schedule=interval_schedule(1))
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        settled = self.store.settle_job_run(run.id, JobRunStatus.CANCELLED)
        current = self.store.load_job(record.id)
        disabled = self.store.set_job_enabled(record.id, current.revision, False, now=NOW)
        enabled = self.store.set_job_enabled(
            record.id, disabled.revision, True, now=NOW
        )
        # The cancelled generation is still cancelled, and the monotonic
        # counter has moved past it: its number can never be reissued.
        self.assertTrue(self.store.load_job_run(run.id).settled)
        self.assertEqual(JobRunStatus.CANCELLED, self.store.load_job_run(run.id).status)
        self.assertEqual(2, enabled.next_generation)
        with self.assertRaises(JobError):
            self.store.record_job_run(record.id, settled.nominal_due_at_utc)

    def test_archive_disables_scheduling_and_preserves_history(self) -> None:
        record = self.create()
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        self.store.settle_job_run(run.id, JobRunStatus.COMPLETED)
        current = self.store.load_job(record.id)
        archived = self.store.archive_job(record.id, current.revision)
        self.assertTrue(archived.archived)
        self.assertFalse(archived.enabled)
        self.assertIsNone(archived.next_due_at_utc)
        self.assertIsNotNone(canonical_utc_timestamp(archived.archived_at))
        # Nothing is deleted: the job, its snapshot and its generations all
        # stay readable, and the definition itself is untouched.
        self.assertEqual(record.spec, archived.spec)
        self.assertIsNotNone(self.store.load_job(record.id))
        self.assertIsNotNone(self.store.load_job_run(run.id))
        self.assertEqual(1, len(self.store.list_job_runs(record.id, 10)))
        connection = raw(self.database)
        try:
            self.assertEqual(
                1, connection.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0]
            )
        finally:
            connection.close()
        with self.assertRaises(JobError):
            self.store.record_job_run(record.id, record.next_due_at_utc)
        with self.assertRaises(JobError):
            self.store.archive_job(record.id, archived.revision)
        with self.assertRaises(JobError):
            self.store.set_job_enabled(record.id, archived.revision, True, now=NOW)

    def test_an_impossible_enabled_archive_combination_is_refused(self) -> None:
        record = self.create()
        archived = self.store.archive_job(record.id, record.revision)
        # The contract refuses it...
        with self.assertRaises(JobError):
            JobRecord(spec=record.spec, enabled=True, archived_at=utc_now())
        with self.assertRaises(JobError):
            JobRecord(spec=record.spec, enabled=False, next_due_at_utc=utc_now())
        # ...and so does the table, independently of any Python check.
        connection = raw(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE jobs SET enabled = 1 WHERE id = ?", (archived.id,)
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE jobs SET next_due_at_utc = ? WHERE id = ?",
                    (utc_now(), archived.id),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE jobs SET revision = 0 WHERE id = ?", (archived.id,)
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE jobs SET kind = 'daily' WHERE id = ?", (archived.id,)
                )
        finally:
            connection.close()


# --------------------------------------------------------------------------
# RUNS / GENERATIONS
# --------------------------------------------------------------------------


class JobRunTests(JobStoreTestCase):
    def test_recording_a_generation_advances_only_bookkeeping(self) -> None:
        record = self.create(schedule=interval_schedule(1))
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        self.assertEqual(1, run.generation)
        self.assertEqual(record.revision, run.job_revision)
        self.assertEqual(JobRunStatus.PENDING, run.status)
        self.assertFalse(run.settled)
        self.assertIsNone(run.settled_at)
        # Nothing was claimed, materialized or owned.
        self.assertIsNone(run.session_id)
        self.assertIsNone(run.task_id)
        self.assertIsNone(run.attempt_id)
        self.assertIsNone(run.owner_token)
        self.assertIsNone(run.claimed_at)
        self.assertEqual(0, run.fencing_epoch)
        self.assertFalse(run.cancel_requested)
        after = self.store.load_job(record.id)
        self.assertEqual(2, after.next_generation)
        self.assertEqual(run.id, after.last_run_id)
        self.assertEqual((NOW + timedelta(hours=2)).isoformat(), after.next_due_at_utc)

    def test_only_an_instant_the_schedule_names_can_be_recorded(self) -> None:
        record = self.create(schedule=interval_schedule(1))
        off_grid = (NOW + timedelta(minutes=90)).isoformat()
        with self.assertRaisesRegex(JobError, "does not name"):
            self.store.record_job_run(record.id, off_grid)
        with self.assertRaises(JobError):
            self.store.record_job_run(record.id, "2026-09-06 13:00:00")
        with self.assertRaises(JobError):
            self.store.record_job_run(str(uuid4()), record.next_due_at_utc)

    def test_a_duplicate_occurrence_is_refused(self) -> None:
        record = self.create(schedule=interval_schedule(1))
        first = self.store.record_job_run(record.id, record.next_due_at_utc)
        self.store.settle_job_run(first.id, JobRunStatus.COMPLETED)
        with self.assertRaisesRegex(JobError, "already been recorded"):
            self.store.record_job_run(record.id, first.nominal_due_at_utc)

    def test_occurrence_identity_is_enforced_by_storage(self) -> None:
        record = self.create(schedule=interval_schedule(1))
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        self.store.settle_job_run(run.id, JobRunStatus.COMPLETED)
        columns = (
            "id, job_id, job_revision, generation, nominal_due_at_utc, "
            "project_id, spec_json, snapshot_digest, status, fencing_epoch, "
            "cancel_requested, created_at, settled_at"
        )
        insert = (
            f"INSERT INTO job_runs ({columns}) "
            f"VALUES ({', '.join('?' * len(columns.split(', ')))})"
        )

        def row(generation: int, nominal: str) -> tuple:
            # Settled on purpose, so the ONE-unsettled index cannot be what
            # refuses these writes: the occurrence keys must do it alone.
            return (
                str(uuid4()),
                record.id,
                record.revision,
                generation,
                nominal,
                record.project_id,
                record.spec.encoded(),
                record.spec.digest,
                JobRunStatus.COMPLETED.value,
                0,
                0,
                utc_now(),
                utc_now(),
            )

        connection = raw(self.database)
        try:
            # The same (job, revision, nominal instant) cannot exist twice...
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(insert, row(50, run.nominal_due_at_utc))
            # ...and neither can the same (job, generation), so a settled
            # generation's number can never be reissued.
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    insert,
                    row(run.generation, (NOW + timedelta(hours=9)).isoformat()),
                )
        finally:
            connection.close()
        self.assertEqual(1, len(self.store.list_job_runs(record.id, 10)))

    def test_at_most_one_unsettled_generation_exists_per_job(self) -> None:
        record = self.create(schedule=interval_schedule(1))
        first = self.store.record_job_run(record.id, record.next_due_at_utc)
        current = self.store.load_job(record.id)
        with self.assertRaisesRegex(JobError, "already open"):
            self.store.record_job_run(record.id, current.next_due_at_utc)
        # The partial unique index holds the same line for a raw writer.
        connection = raw(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO job_runs
                        (id, job_id, job_revision, generation,
                         nominal_due_at_utc, project_id, spec_json,
                         snapshot_digest, status, fencing_epoch,
                         cancel_requested, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, ?)
                    """,
                    (
                        str(uuid4()),
                        record.id,
                        record.revision,
                        99,
                        current.next_due_at_utc,
                        record.project_id,
                        record.spec.encoded(),
                        record.spec.digest,
                        utc_now(),
                    ),
                )
        finally:
            connection.close()
        self.assertEqual([first.id], [r.id for r in self.store.list_job_runs(record.id, 10)])

    def test_settling_allows_the_next_generation(self) -> None:
        record = self.create(schedule=interval_schedule(1))
        first = self.store.record_job_run(record.id, record.next_due_at_utc)
        settled = self.store.settle_job_run(
            first.id,
            JobRunStatus.FAILED,
            blocking_code=ErrorCode.CONNECTOR_FAILED.value,
        )
        self.assertTrue(settled.settled)
        self.assertEqual(ErrorCode.CONNECTOR_FAILED.value, settled.blocking_code)
        self.assertIsNotNone(canonical_utc_timestamp(settled.settled_at))
        second = self.store.record_job_run(
            record.id, self.store.load_job(record.id).next_due_at_utc
        )
        self.assertEqual(2, second.generation)
        with self.assertRaisesRegex(JobError, "already settled"):
            self.store.settle_job_run(first.id, JobRunStatus.COMPLETED)
        with self.assertRaisesRegex(JobError, "settled status"):
            self.store.settle_job_run(second.id, JobRunStatus.RUNNING)
        with self.assertRaises(JobError):
            self.store.settle_job_run(second.id, "completed")  # type: ignore[arg-type]

    def test_a_condition_watch_generation_records_its_typed_outcome(self) -> None:
        record = self.create(kind=JobKind.CONDITION_WATCH)
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        settled = self.store.settle_job_run(
            run.id, JobRunStatus.SKIPPED, condition_result="unknown"
        )
        self.assertEqual("unknown", settled.condition_result)
        self.assertEqual("unknown", self.store.load_job_run(run.id).condition_result)
        with self.assertRaises(JobError):
            JobRun(
                job_id=record.id,
                job_revision=1,
                generation=1,
                nominal_due_at_utc=utc_now(),
                spec=record.spec,
                condition_result="probably",
            )

    def test_the_snapshot_digest_is_deterministic(self) -> None:
        record = self.create()
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        self.assertEqual(record.spec.digest, run.snapshot_digest)
        self.assertEqual(run.snapshot_digest, self.store.load_job_run(run.id).snapshot_digest)
        # Rebuilding the same definition independently produces the same
        # digest, and it covers only the definition - not the bookkeeping.
        self.assertEqual(spec().digest, record.spec.digest)
        self.assertEqual(
            record.spec.digest,
            JobSpec.from_dict(json.loads(record.spec.encoded())).digest,
        )
        self.assertNotEqual(spec().digest, spec(label="other").digest)

    def test_one_instant_has_exactly_one_durable_spelling(self) -> None:
        # The occurrence key is a string, so two renderings of the SAME moment
        # would be two rows the uniqueness index never sees collide. The
        # engine's own spelling is what gets stored, whatever the caller sent.
        record = self.create(schedule=interval_schedule(1))
        due = datetime.fromisoformat(record.next_due_at_utc)
        run = self.store.record_job_run(
            record.id, due.isoformat(timespec="microseconds")
        )
        self.assertEqual(record.next_due_at_utc, run.nominal_due_at_utc)
        self.store.settle_job_run(run.id, JobRunStatus.COMPLETED)
        with self.assertRaisesRegex(JobError, "already been recorded"):
            self.store.record_job_run(
                record.id, due.isoformat(timespec="microseconds")
            )
        # A non-UTC rendering of the same moment never reaches the occurrence
        # key at all: the canonical timestamp grammar refuses it first.
        with self.assertRaisesRegex(JobError, "canonical UTC"):
            self.store.record_job_run(
                record.id, due.astimezone(timezone(timedelta(hours=3))).isoformat()
            )

    def test_a_revision_inconsistent_generation_cannot_be_settled(self) -> None:
        record = self.create()
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        rewrite_run(self.store, run.id, job_revision=99)
        with self.assertRaises(JobError):
            self.store.settle_job_run(run.id, JobRunStatus.COMPLETED)
        connection = raw(self.database)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT settled_at FROM job_runs WHERE id = ?", (run.id,)
                ).fetchone()["settled_at"]
            )
        finally:
            connection.close()

    def test_a_generation_naming_an_impossible_revision_is_refused(self) -> None:
        record = self.create()
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        # A revision the job has never reached is inconsistent durable state.
        rewrite_run(self.store, run.id, job_revision=99)
        self.assertIsNone(self.store.load_job_run(run.id))
        self.assertFalse(self.store.list_job_runs(record.id, 10)[0].readable)
        # An OLDER revision is ordinary history and is kept.
        self.store.edit_job(record.id, record.revision, spec(label="v2"), now=NOW)
        rewrite_run(self.store, run.id, job_revision=1)
        self.assertEqual(1, self.store.load_job_run(run.id).job_revision)
        rewrite_run(self.store, run.id, job_revision=0, bypass_checks=True)
        self.assertIsNone(self.store.load_job_run(run.id))


# --------------------------------------------------------------------------
# VISIBILITY
# --------------------------------------------------------------------------


class VisibilityTests(JobStoreTestCase):
    def test_a_listing_is_bounded_by_the_store(self) -> None:
        for index in range(5):
            self.create(label=f"job {index}")
        self.assertEqual(2, len(self.store.list_jobs(2)))
        self.assertEqual(5, len(self.store.list_jobs(VISIBILITY_MAX_LIMIT)))
        # The ceiling is the store's, whoever calls and whatever they pass.
        self.assertLessEqual(
            len(self.store.list_jobs(VISIBILITY_MAX_LIMIT + 1_000)),
            VISIBILITY_MAX_LIMIT,
        )
        for bad in (0, -1, True, 1.0, "5", None):
            self.assertEqual([], self.store.list_jobs(bad))  # type: ignore[arg-type]
            self.assertEqual([], self.store.list_job_runs("j", bad))  # type: ignore[arg-type]

    def test_listings_have_a_stable_total_order(self) -> None:
        records = [self.create(label=f"job {index}") for index in range(4)]
        first = [summary.id for summary in self.store.list_jobs(10)]
        self.assertEqual(first, [summary.id for summary in self.store.list_jobs(10)])
        self.assertEqual(sorted({r.id for r in records}), sorted(first))
        # Toggling a job does not reshuffle the catalogue.
        self.store.set_job_enabled(records[0].id, records[0].revision, False, now=NOW)
        self.assertEqual(first, [summary.id for summary in self.store.list_jobs(10)])
        active = records[1]
        generations = []
        for _ in range(3):
            current = self.store.load_job(active.id)
            run = self.store.record_job_run(active.id, current.next_due_at_utc)
            self.store.settle_job_run(run.id, JobRunStatus.COMPLETED)
            generations.append(run.generation)
        listed = [summary.generation for summary in self.store.list_job_runs(active.id, 10)]
        self.assertEqual(sorted(generations, reverse=True), listed)

    def test_a_projection_exposes_no_prompt_target_or_secret(self) -> None:
        secret_objective = "email the api_key=HUNTER2 report to finance"
        record = self.create(
            label="nightly export",
            steps=(step(objective=secret_objective, target="C:/Users/me/secret.txt"),),
        )
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        payloads = [
            repr(self.store.job_summary(record.id)),
            repr(self.store.list_jobs(10)),
            repr(self.store.list_job_runs(record.id, 10)),
        ]
        for payload in payloads:
            self.assertNotIn("HUNTER2", payload)
            self.assertNotIn(secret_objective, payload)
            self.assertNotIn("secret.txt", payload)
            self.assertNotIn("C:/Users", payload)
        summary = self.store.job_summary(record.id)
        self.assertTrue(summary.readable)
        self.assertEqual("nightly export", summary.label)
        self.assertEqual("recurring", summary.kind)
        self.assertEqual("proj-a", summary.project_id)
        self.assertTrue(summary.enabled)
        self.assertFalse(summary.archived)
        self.assertEqual(1, summary.revision)
        self.assertEqual(run.id, summary.last_run_id)
        listed = self.store.list_job_runs(record.id, 10)[0]
        self.assertEqual(run.snapshot_digest, listed.snapshot_digest)
        self.assertEqual("pending", listed.status)
        self.assertFalse(listed.settled)
        self.assertIsNone(self.store.job_summary(str(uuid4())))

    def test_archived_jobs_leave_the_default_listing_but_stay_reachable(self) -> None:
        record = self.create()
        self.store.archive_job(record.id, record.revision)
        self.assertEqual([], self.store.list_jobs(10))
        self.assertEqual(
            [record.id], [s.id for s in self.store.list_jobs(10, include_archived=True)]
        )
        self.assertTrue(self.store.job_summary(record.id).archived)


# --------------------------------------------------------------------------
# CORRUPTION / FAIL-CLOSED
# --------------------------------------------------------------------------


class CorruptionTests(JobStoreTestCase):
    def assert_quarantined(self, record: JobRecord) -> None:
        """A corrupted job is unrunnable, still visible, and still refused."""
        self.assertIsNone(self.store.load_job(record.id))
        summary = self.store.job_summary(record.id)
        self.assertIsNotNone(summary)
        self.assertFalse(summary.readable)
        self.assertIsNone(summary.kind)
        self.assertIsNone(summary.label)
        self.assertIsNone(summary.enabled)
        self.assertIsNone(summary.next_due_at_utc)
        self.assertFalse(self.store.list_jobs(10, include_archived=True)[0].readable)
        # Nothing can act on it: it cannot be edited, toggled, archived, or
        # turned into a generation.
        for operation in (
            lambda: self.store.edit_job(record.id, record.revision, spec(), now=NOW),
            lambda: self.store.set_job_enabled(
                record.id, record.revision, False, now=NOW
            ),
            lambda: self.store.archive_job(record.id, record.revision),
            lambda: self.store.record_job_run(record.id, record.next_due_at_utc),
        ):
            with self.assertRaises(JobError):
                operation()

    def test_a_malformed_kind_never_loads(self) -> None:
        record = self.create()
        document = json.loads(record.spec.encoded())
        document["kind"] = "every_full_moon"
        rewrite_job(
            self.store,
            record.id,
            spec_json=json.dumps(document),
            bypass_checks=True,
        )
        self.assert_quarantined(record)

    def test_a_kind_disagreeing_with_its_column_never_loads(self) -> None:
        record = self.create()
        rewrite_job(self.store, record.id, kind="one_time")
        self.assert_quarantined(record)

    def test_a_malformed_identity_never_loads(self) -> None:
        record = self.create()
        rewrite_job(self.store, record.id, id="not-a-uuid")
        self.assertIsNone(self.store.load_job("not-a-uuid"))
        self.assertFalse(self.store.job_summary("not-a-uuid").readable)
        self.assertIsNotNone(job_identifier_refusal("not-a-uuid"))
        self.assertIsNotNone(job_identifier_refusal(str(uuid4()).upper()))
        self.assertIsNotNone(job_identifier_refusal("{%s}" % uuid4()))
        self.assertIsNone(job_identifier_refusal(str(uuid4())))

    def test_an_invalid_revision_or_generation_never_loads(self) -> None:
        record = self.create()
        rewrite_job(self.store, record.id, revision=0, bypass_checks=True)
        self.assert_quarantined(record)
        rewrite_job(self.store, record.id, revision=1, next_generation=-3,
                    bypass_checks=True)
        self.assertIsNone(self.store.load_job(record.id))

    def test_an_invalid_timezone_or_schedule_never_loads(self) -> None:
        record = self.create(schedule=Daily(timezone=NEW_YORK, at=time(6, 0)))
        document = json.loads(record.spec.encoded())
        document["schedule"]["timezone"] = "Mars/Olympus_Mons"
        rewrite_job(self.store, record.id, spec_json=json.dumps(document))
        self.assert_quarantined(record)
        document["schedule"] = {"kind": "daily", "timezone": NEW_YORK, "at": "25:99"}
        rewrite_job(self.store, record.id, spec_json=json.dumps(document))
        self.assertIsNone(self.store.load_job(record.id))
        rewrite_job(self.store, record.id, spec_json="{not json")
        self.assertIsNone(self.store.load_job(record.id))

    def test_a_specification_that_no_longer_matches_its_digest_never_loads(self) -> None:
        record = self.create()
        rewrite_job(self.store, record.id, definition_digest="0" * 64)
        self.assert_quarantined(record)

    def test_a_scope_disagreeing_with_its_column_never_loads(self) -> None:
        self.store.save_project(ProjectRecord(id="proj-b", name="B"))
        record = self.create()
        rewrite_job(self.store, record.id, project_id="proj-b")
        self.assert_quarantined(record)

    def test_an_impossible_settled_shape_never_loads(self) -> None:
        record = self.create()
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        # Settled instant without a settled status, and the reverse.
        rewrite_run(self.store, run.id, settled_at=utc_now(), bypass_checks=True)
        self.assertIsNone(self.store.load_job_run(run.id))
        self.assertFalse(self.store.list_job_runs(record.id, 10)[0].readable)
        rewrite_run(
            self.store,
            run.id,
            status="completed",
            settled_at=None,
            bypass_checks=True,
        )
        self.assertIsNone(self.store.load_job_run(run.id))
        rewrite_run(
            self.store,
            run.id,
            status="halfway",
            settled_at=None,
            bypass_checks=True,
        )
        self.assertIsNone(self.store.load_job_run(run.id))
        with self.assertRaises(JobError):
            self.store.settle_job_run(run.id, JobRunStatus.COMPLETED)

    def test_a_corrupt_row_can_never_become_eligible_or_runnable(self) -> None:
        record = self.create()
        # An enabled, due, otherwise perfect-looking row whose definition is
        # unreadable must not be schedulable by any path.
        rewrite_job(self.store, record.id, spec_json='{"steps": []}')
        self.assertIsNone(self.store.load_job(record.id))
        self.assert_quarantined(record)
        # And the row is still physically there - quarantined, not repaired.
        row = job_row(self.store, record.id)
        self.assertEqual('{"steps": []}', row["spec_json"])
        self.assertEqual(1, row["enabled"])

    def test_a_tampered_run_snapshot_never_loads(self) -> None:
        record = self.create()
        run = self.store.record_job_run(record.id, record.next_due_at_utc)
        document = json.loads(record.spec.encoded())
        document["steps"][0]["target"] = "C:/Windows/System32/config/SAM"
        rewrite_run(self.store, run.id, spec_json=json.dumps(document))
        self.assertIsNone(self.store.load_job_run(run.id))
        self.assertFalse(self.store.list_job_runs(record.id, 10)[0].readable)


# --------------------------------------------------------------------------
# PHASE BOUNDARY
# --------------------------------------------------------------------------


class ExecutionBoundaryTests(unittest.TestCase):
    """Phase 1B is storage and management ONLY, proved from the source."""

    #: The durable scheduler storage surface. No production module may call
    #: any of it: there is no dispatcher, no watcher and no background worker
    #: in this build, so the only callers are tests.
    SURFACE = frozenset(
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
            "load_job",
            "load_job_run",
            "list_jobs",
            "list_job_runs",
            "job_summary",
        }
    )

    def production_modules(self) -> list[Path]:
        root = Path(__file__).resolve().parent.parent / "jarvis"
        return sorted(root.rglob("*.py"))

    def test_no_production_module_calls_the_scheduler_storage_surface(self) -> None:
        # ``memory.py`` DEFINES the surface and ``contracts.py`` declares it on
        # the provider-independent protocol; nothing else may even mention it,
        # so no production path can wake on a due job, claim a generation,
        # materialize a task, create an approval or start a worker through it.
        allowed = {"memory.py", "contracts.py"}
        for module in self.production_modules():
            if module.name in allowed:
                continue
            source = module.read_text(encoding="utf-8")
            for name in self.SURFACE:
                self.assertNotIn(name, source, f"{module.name} mentions {name}")

    def test_the_storage_surface_reaches_no_execution_authority(self) -> None:
        # Inside memory.py, the scheduler methods must not touch the execution,
        # approval, billing or task-materialization surfaces at all.
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
        )
        for node in store.body:
            if not isinstance(node, ast.FunctionDef) or (
                node.name not in self.SURFACE
                and not node.name.startswith("_job")
                and node.name not in {"_next_due", "_names_occurrence", "_write_job"}
            ):
                continue
            body = ast.get_source_segment(source, node) or ""
            for needle in forbidden:
                self.assertNotIn(needle, body, f"{node.name} reaches {needle}")

    def test_no_autonomous_reclaim_or_heartbeat_machinery_exists(self) -> None:
        # Phase 2A MOVED this boundary rather than removing it. A bounded
        # lease, an atomic claim and a fencing epoch now exist and are
        # exercised by ``tests/test_scheduler_claims.py``. What still does not
        # exist is anything that acts on them BY ITSELF: no heartbeat, no
        # timer-driven reclaim, no orphan recovery sweep and no due-job
        # scanner. Every expiry judgement in this build happens because a
        # caller asked, at an instant the caller supplied.
        for module in self.production_modules():
            source = module.read_text(encoding="utf-8")
            for forbidden in (
                "def heartbeat",
                "heartbeat_at",
                "auto_reclaim",
                "orphan_recovery",
                "def due_jobs",
                "def scan_due",
            ):
                self.assertNotIn(forbidden, source, f"{module.name}: {forbidden}")

    def test_no_background_scheduler_module_exists(self) -> None:
        names = {module.name for module in self.production_modules()}
        for absent in ("dispatcher.py", "scheduler.py", "worker.py", "daemon.py"):
            self.assertNotIn(absent, names)

    def test_the_settled_vocabulary_partitions_the_run_statuses(self) -> None:
        self.assertEqual(
            frozenset(JobRunStatus),
            UNSETTLED_JOB_RUN_STATUSES | SETTLED_JOB_RUN_STATUSES,
        )
        self.assertEqual(
            frozenset(), UNSETTLED_JOB_RUN_STATUSES & SETTLED_JOB_RUN_STATUSES
        )
        # An outcome nobody could establish is NOT an ending.
        self.assertIn(JobRunStatus.UNKNOWN_OUTCOME, UNSETTLED_JOB_RUN_STATUSES)


# --------------------------------------------------------------------------
# ENCODING
# --------------------------------------------------------------------------


class EncodingTests(unittest.TestCase):
    """The persisted encodings reconstruct the pure kernels' typed objects."""

    def test_every_schedule_kind_round_trips(self) -> None:
        for schedule in (
            OneTime(at=NOW),
            Interval(anchor=NOW, every=timedelta(minutes=30)),
            Interval(anchor=NOW, every=timedelta(hours=2), end=NOW + timedelta(days=7)),
            Daily(timezone=NEW_YORK, at=time(9, 30)),
            Weekly(
                timezone=BERLIN,
                at=time(23, 59),
                days=frozenset({Weekday.SATURDAY, Weekday.SUNDAY}),
            ),
        ):
            with self.subTest(schedule=type(schedule).__name__):
                self.assertEqual(schedule, schedule_from_dict(schedule_to_dict(schedule)))

    def test_a_condition_round_trips(self) -> None:
        for kind in ConditionKind:
            condition = LocalReadCondition(
                kind=kind, reference="notes.txt", expected_sha256=DIGEST
            )
            self.assertEqual(condition, condition_from_dict(condition_to_dict(condition)))

    def test_a_naive_persisted_instant_is_refused(self) -> None:
        encoded = schedule_to_dict(OneTime(at=NOW))
        encoded["at"] = "2026-09-06T12:00:00"
        with self.assertRaises(ScheduleError):
            schedule_from_dict(encoded)

    def test_a_malformed_interval_is_refused(self) -> None:
        encoded = schedule_to_dict(Interval(anchor=NOW, every=timedelta(hours=1)))
        for broken in (0, -60, True, "3600", 60.0):
            with self.subTest(value=broken):
                with self.assertRaises(ScheduleError):
                    schedule_from_dict({**encoded, "every_seconds": broken})
        # Below the approved floor, refused by the kernel itself.
        with self.assertRaises(ScheduleError):
            schedule_from_dict({**encoded, "every_seconds": 60})

    def test_a_malformed_weekday_set_is_refused(self) -> None:
        encoded = schedule_to_dict(
            Weekly(timezone=BERLIN, at=time(8, 0), days=frozenset({Weekday.MONDAY}))
        )
        for broken in ([], [7], [-1], ["monday"], [True], "monday", None):
            with self.subTest(value=broken):
                with self.assertRaises(ScheduleError):
                    schedule_from_dict({**encoded, "days": broken})


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
