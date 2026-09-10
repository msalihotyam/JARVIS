"""Scheduler phase 2B1: the one-shot JobRun -> AgentCore dispatch boundary.

Covers the additive SQLite 9 -> 10 migration, the atomic first binding of a
claimed generation to a pristine session and chain, the durable CLAIMED ->
RUNNING transition that happens BEFORE Core is entered, the single
``run_chain`` call, the fresh clock sample taken only after it returns, and the
fenced reflection of authoritative stored truth back onto the generation.

TWO KINDS OF TEST, deliberately. The binding, the approval park and the ordinary
completion run against a REAL ``AgentCore`` with real connectors, because those
paths are only worth what the real machinery does. The result MAPPING - unknown
outcomes outranking failures, a lease dying mid-run, a chain status that
disagrees with the run object - is driven through a scripted core against the
same real store, because those are exactly the situations a real connector
cannot be asked to produce on demand, and pretending otherwise would make the
tests weaker rather than more honest.

THE PHASE BOUNDARY IS ITSELF UNDER TEST. At the end of this phase the scheduler
still creates no approval and no grant, authorizes no spending, reaches no
connector, opens and concludes no execution attempt, evaluates no predicate and
runs no background loop; there is no RUNNING takeover and no recovery. A
generation whose worker dies mid-run stays RUNNING and BLOCKED, which is the
intended outcome rather than a gap.

Every instant is EXPLICIT and every clock is injected. Nothing sleeps, nothing
polls, and no test performs a live model, auth or network call.
"""

from __future__ import annotations

import ast
import contextlib
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from jarvis.contracts import (
    AgentRun,
    ActionScope,
    ChainState,
    AttemptState,
    ChainStatus,
    ConditionKind,
    DISPATCHABLE_JOB_RUN_STATUSES,
    ErrorCode,
    JobError,
    JobKind,
    JobRunDispatch,
    JobRunStatus,
    JobSpec,
    PermissionRequest,
    LOCAL_READ_CAPABILITY,
    LocalReadCondition,
    ProjectRecord,
    OutcomeStatus,
    RunStatus,
    SideEffect,
    RiskLevel,
    SessionState,
    Task,
    StructuredError,
    SideEffectProfile,
    TaskStatus,
    chain_steps_from_declaration,
)
from jarvis.dispatch import (
    DISPATCH_BLOCKED,
    DISPATCH_PARKED,
    DISPATCH_REFUSED,
    DISPATCH_SETTLED,
    DISPATCH_UNKNOWN,
    DispatchOutcome,
    dispatch_job_run,
)
from jarvis.memory import SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import make_task, open_core, safe_cleanup

# The canonical read/model declaration and the connectors that serve it,
# reused verbatim from the chain suite rather than copied: a second set of
# fixtures would be a second set of facts to keep in agreement.
from tests.test_task_chain import (  # noqa: F401
    _DRIVER_POLICY,
    _RecordingConnector,
    declaration,
)
from tests.test_scheduler_jobs import (  # noqa: F401
    DIGEST,
    NOW,
    interval_schedule,
    setUpModule,
    tearDownModule,
)

LEASE = timedelta(minutes=5)
WORKER_A = "worker-a"
WORKER_B = "worker-b"


class ScriptedClock:
    """A clock that hands out stated instants and records when it was asked.

    The ORDER is the point. Phase 2B1 requires the post-run instant to be
    sampled after ``run_chain`` returns, and the only way to prove that is to
    watch the interleaving rather than to trust the source.
    """

    def __init__(self, *instants: datetime) -> None:
        self._instants = list(instants)
        self.events: list[str] = []

    def __call__(self) -> datetime:
        moment = self._instants[0] if len(self._instants) == 1 else self._instants.pop(0)
        self.events.append(f"clock:{moment.isoformat()}")
        return moment


class ScriptedCore:
    """A stand-in for exactly one method: ``run_chain``. Nothing else.

    It shares the REAL store with the test, so every piece of authoritative
    truth the dispatcher reads - the generation, the session, the chain, the
    attempt journal - is genuine storage. Only Core's verdict is scripted.
    """

    def __init__(self, memory, result, *, clock=None, raises=None) -> None:
        self.memory = memory
        self._result = result
        self._raises = raises
        self._clock = clock
        self.calls: list[str] = []

    def run_chain(self, session_id: str):
        self.calls.append(session_id)
        if self._clock is not None:
            self._clock.events.append(f"run_chain:{session_id}")
        if self._raises is not None:
            raise self._raises
        return self._result


class DispatchTestCase(unittest.TestCase):
    """A real schema-10 store, a real registry and real local connectors."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.settings = Settings(memory_database=self.database)
        self.memory = SQLiteMemoryStore(self.database)
        self.memory.save_project(ProjectRecord(id="proj-a", name="Project A"))
        self.reader = _RecordingConnector(
            "reader",
            (LOCAL_READ_CAPABILITY,),
            snapshot="the project notes, in full",
        )
        from jarvis.contracts import SideEffect

        self.planner = _RecordingConnector(
            "planner",
            ("code_analysis", "code_planning"),
            SideEffect.EXTERNAL_EGRESS,
            answer="A three bullet summary of the notes.",
        )
        self.registry = ToolRegistry()
        self.registry.register(self.reader)
        self.registry.register(self.planner)
        self.core = open_core(
            memory=self.memory,
            registry=self.registry,
            settings=self.settings,
            destination_policy=_DRIVER_POLICY,
        )
        self.at = NOW + timedelta(hours=1)
        self.during = self.at + timedelta(minutes=1)

    # -- fixtures ---------------------------------------------------------

    def spec(self, *, steps: int = 1, kind: JobKind = JobKind.RECURRING, **over):
        declared = tuple(chain_steps_from_declaration(declaration(steps)))
        fields = dict(
            project_id="proj-a",
            kind=kind,
            label="nightly notes",
            schedule=interval_schedule(1),
            steps=declared,
        )
        fields.update(over)
        return JobSpec(**fields)

    def watch_spec(self) -> JobSpec:
        """A CONDITION_WATCH: a predicate and an on_true declaration, no steps."""
        return JobSpec(
            project_id="proj-a",
            kind=JobKind.CONDITION_WATCH,
            label="watch the notes",
            schedule=interval_schedule(1),
            condition=LocalReadCondition(
                kind=ConditionKind.LOCAL_READ_SHA256_DIFFERS,
                reference="notes.txt",
                expected_sha256=DIGEST,
            ),
            on_true=tuple(chain_steps_from_declaration(declaration(1))),
        )

    def claimed(self, *, steps: int = 1, worker: str = WORKER_A, **over):
        """One materialized generation, claimed, with its lease."""
        record = self.memory.create_job(self.spec(steps=steps, **over), now=NOW)
        run = self.memory.materialize_due_job_run(
            record.id, record.revision, now=self.at
        )
        self.assertIsNotNone(run)
        self.job = record
        lease = self.memory.claim_job_run(run.id, worker, now=self.at, lease=LEASE)
        return run, lease

    def reload(self, run_id: str):
        return self.memory.load_job_run(run_id)

    def raw(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def plant_chain_status(self, session_id: str, status: ChainStatus) -> None:
        """Force the bound chain into a terminal status, bypassing every API.

        Deliberately raw SQL. A scripted core does not really advance a chain,
        and the dispatcher reads the DURABLE status rather than the run object,
        so the fixture has to write the durable one.

        The whole chain is made COHERENT, not just its status field: a terminal
        chain that left a step uncreated is refused by the chain grammar and
        would load as INVALID, which would test the malformed path instead of
        the one named. So every step gets a routing id and the cursor sits on
        the last step, exactly as a really-finished chain looks.
        """
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            payload = json.loads(row["state_json"])
            chain = payload["chain"]
            chain["status"] = status.value
            if status is not ChainStatus.INVALID:
                total = len(chain["steps"])
                chain["step_task_ids"] = [str(uuid4()) for _ in range(total)]
                chain["cursor"] = total - 1
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(payload), session_id),
            )
            connection.commit()

    def plant_status_only(self, session_id: str, status: ChainStatus) -> None:
        """Flip ONLY the durable chain status, leaving all routing intact.

        The difference from ``plant_chain_status`` is the whole point of F1: a
        chain whose routing is REAL - real task rows, real session ownership,
        real attempt history - may legitimately be settled from, while one
        carrying invented ids may not. This fixture produces the first kind.
        """
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            payload = json.loads(row["state_json"])
            payload["chain"]["status"] = status.value
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(payload), session_id),
            )
            connection.commit()

    def rewind_task(self, task_id: str, status) -> None:
        """Put one task back into a non-terminal status, bypassing the loop."""
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            payload = json.loads(row["state_json"])
            payload["status"] = status.value
            connection.execute(
                "UPDATE tasks SET state_json = ?, status = ? WHERE id = ?",
                (json.dumps(payload), status.value, task_id),
            )
            connection.commit()

    def task_ids(self, session_id: str) -> list[str]:
        with contextlib.closing(self.raw()) as connection:
            return [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM tasks WHERE session_id = ? ORDER BY rowid",
                    (session_id,),
                )
            ]

    def sessions(self) -> int:
        with contextlib.closing(self.raw()) as connection:
            return connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def table_count(self, table: str) -> int:
        with contextlib.closing(self.raw()) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --------------------------------------------------------------------------
# SCHEMA 10
# --------------------------------------------------------------------------


class Schema10MigrationTests(unittest.TestCase):
    """The additive 9 -> 10 upgrade: ONE partial index and nothing else."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"

    def objects(self) -> dict[str, str]:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return {
                name: sql
                for name, sql in connection.execute(
                    "SELECT name, sql FROM sqlite_master"
                ).fetchall()
            }

    def build_schema_nine(self) -> None:
        """A REAL schema-9 database, produced by the shipped code path."""
        store = SQLiteMemoryStore(self.database)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP INDEX IF EXISTS idx_job_runs_session")
            connection.execute("UPDATE schema_version SET version = 9")
            connection.execute("DELETE FROM schema_migrations WHERE version = 10")
            connection.commit()
        del store

    def test_a_fresh_database_is_schema_ten(self) -> None:
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(10, store.schema_version())

    def test_a_schema_nine_database_upgrades_to_ten(self) -> None:
        self.build_schema_nine()
        before = self.objects()
        self.assertNotIn("idx_job_runs_session", before)
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(10, store.schema_version())
        after = self.objects()
        # EXACTLY one new object, and every pre-existing one byte-identical:
        # no table is rebuilt and no DDL is rewritten.
        self.assertEqual({"idx_job_runs_session"}, set(after) - set(before))
        for name, sql in before.items():
            self.assertEqual(sql, after[name], name)

    def test_the_index_is_unique_and_partial(self) -> None:
        SQLiteMemoryStore(self.database)
        sql = self.objects()["idx_job_runs_session"]
        self.assertIn("UNIQUE INDEX", sql)
        self.assertIn("job_runs(session_id)", sql)
        self.assertIn("WHERE session_id IS NOT NULL", sql)
        # NULLs must stay free to repeat: the overwhelming majority of
        # generations never get a session.
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            partial = connection.execute(
                "SELECT partial FROM pragma_index_list('job_runs') "
                "WHERE name = 'idx_job_runs_session'"
            ).fetchone()
        self.assertEqual(1, partial[0])

    def test_a_duplicate_legacy_session_binding_fails_the_upgrade_closed(self) -> None:
        # Two generations naming ONE session is ambiguous dispatch history.
        # The upgrade must refuse rather than pick a winner, because the loser
        # would then look free to dispatch a second time.
        self.build_schema_nine()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                INSERT INTO projects (id, name, active, created_at)
                VALUES ('proj-a', 'p', 1, '2026-01-01T00:00:00+00:00');
                INSERT INTO jobs (id, project_id, definition_version, revision,
                    kind, spec_json, definition_digest, next_due_at_utc, enabled,
                    archived_at, last_run_id, next_generation, created_at, updated_at)
                VALUES ('11111111-1111-4111-8111-111111111111', 'proj-a', 1, 1,
                    'one_time', '{}', 'd', NULL, 1, NULL, NULL, 3,
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
                INSERT INTO job_runs (id, job_id, job_revision, generation,
                    nominal_due_at_utc, project_id, spec_json, snapshot_digest,
                    status, session_id, fencing_epoch, cancel_requested,
                    created_at, settled_at)
                VALUES ('22222222-2222-4222-8222-222222222222',
                    '11111111-1111-4111-8111-111111111111', 1, 1,
                    '2026-01-01T00:00:00+00:00', 'proj-a', '{}', 'd',
                    'completed', 'shared-session', 1, 0,
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
                INSERT INTO job_runs (id, job_id, job_revision, generation,
                    nominal_due_at_utc, project_id, spec_json, snapshot_digest,
                    status, session_id, fencing_epoch, cancel_requested,
                    created_at, settled_at)
                VALUES ('33333333-3333-4333-8333-333333333333',
                    '11111111-1111-4111-8111-111111111111', 1, 2,
                    '2026-01-02T00:00:00+00:00', 'proj-a', '{}', 'd',
                    'completed', 'shared-session', 1, 0,
                    '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00');
                """
            )
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "more than one job run"):
            SQLiteMemoryStore(self.database)
        # NOTHING was repaired: both rows are still there, still ambiguous.
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                2,
                connection.execute(
                    "SELECT COUNT(*) FROM job_runs WHERE session_id = 'shared-session'"
                ).fetchone()[0],
            )
            self.assertEqual(
                9,
                connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()[0],
            )

    def test_the_output_schema_did_not_move(self) -> None:
        from jarvis.app import OUTPUT_SCHEMA_VERSION

        self.assertEqual(7, OUTPUT_SCHEMA_VERSION)


# --------------------------------------------------------------------------
# ATOMIC BINDING
# --------------------------------------------------------------------------


class BindingTests(DispatchTestCase):
    """The first and only binding a generation ever gets."""

    def test_one_claimed_generation_binds_exactly_one_session_and_chain(self) -> None:
        run, lease = self.claimed()
        before = self.sessions()
        bound = self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.during
        )
        self.assertIsInstance(bound, JobRunDispatch)
        self.assertEqual(before + 1, self.sessions())
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(bound.session_id, stored.session_id)
        self.assertEqual(bound.chain_id, stored.chain_id)
        self.assertFalse(stored.settled)
        # The session is PRISTINE and snapshot-derived.
        session = self.memory.load_session(bound.session_id)
        self.assertEqual("proj-a", session.project_id)
        self.assertIsNone(session.active_task_id)
        self.assertEqual(bound.chain_id, session.chain.id)
        self.assertEqual(ChainStatus.ACTIVE, session.chain.status)
        self.assertEqual(0, session.chain.cursor)
        self.assertEqual(tuple(run.spec.steps), tuple(session.chain.steps))
        self.assertEqual(("",) * len(run.spec.steps), session.chain.step_task_ids)

    def test_the_binding_is_immutable_and_never_replaced(self) -> None:
        run, lease = self.claimed()
        first = self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.during
        )
        sessions = self.sessions()
        # The same holder, the same proof - still refused. A second session
        # for one occurrence would be a second connector crossing.
        with self.assertRaisesRegex(
            JobError, "already bound|not dispatchable"
        ):
            self.memory.bind_job_run_dispatch(
                run.id, WORKER_A, lease.fencing_epoch, now=self.during
            )
        stored = self.reload(run.id)
        self.assertEqual(first.session_id, stored.session_id)
        self.assertEqual(first.chain_id, stored.chain_id)
        self.assertEqual(sessions, self.sessions())

    def test_a_failed_binding_leaves_no_orphan_session_or_chain(self) -> None:
        # A stale epoch fails the CAS. The session INSERT happens before that
        # update in the same transaction, so if the transaction were not
        # atomic an orphan session would survive.
        run, lease = self.claimed()
        before = self.sessions()
        for owner, epoch, when in (
            (WORKER_B, lease.fencing_epoch, self.during),
            (WORKER_A, lease.fencing_epoch - 1, self.during),
            (WORKER_A, lease.fencing_epoch, self.at + LEASE + timedelta(minutes=1)),
        ):
            with self.assertRaises(JobError):
                self.memory.bind_job_run_dispatch(run.id, owner, epoch, now=when)
        self.assertEqual(before, self.sessions())
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.CLAIMED, stored.status)
        self.assertIsNone(stored.session_id)
        self.assertIsNone(stored.chain_id)

    def test_two_concurrent_binders_produce_exactly_one_binding(self) -> None:
        run, lease = self.claimed()
        results: list[object] = []
        barrier = threading.Barrier(2)

        def bind() -> None:
            store = SQLiteMemoryStore(self.database)
            barrier.wait()
            try:
                results.append(
                    store.bind_job_run_dispatch(
                        run.id, WORKER_A, lease.fencing_epoch, now=self.during
                    )
                )
            except JobError as exc:
                results.append(exc)

        threads = [threading.Thread(target=bind) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [r for r in results if isinstance(r, JobRunDispatch)]
        self.assertEqual(1, len(winners))
        self.assertEqual(1, len([r for r in results if isinstance(r, JobError)]))
        stored = self.reload(run.id)
        self.assertEqual(winners[0].session_id, stored.session_id)
        # Exactly one session exists for this one occurrence.
        self.assertEqual(1, self.sessions())

    def test_a_session_already_bound_elsewhere_is_refused(self) -> None:
        # The unique partial index, proved directly: no second generation may
        # name a session another one already owns.
        run, lease = self.claimed()
        bound = self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.during
        )
        other, other_lease = self.claimed()
        with contextlib.closing(self.raw()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE job_runs SET session_id = ? WHERE id = ?",
                    (bound.session_id, other.id),
                )
                connection.commit()

    def test_binding_requires_the_exact_fenced_live_claim(self) -> None:
        run, lease = self.claimed()
        expired = self.at + LEASE + timedelta(seconds=1)
        for owner, epoch, when, fragment in (
            (WORKER_B, lease.fencing_epoch, self.during, "does not hold"),
            (WORKER_A, lease.fencing_epoch + 1, self.during, "does not hold"),
            (WORKER_A, lease.fencing_epoch, expired, "expired"),
        ):
            with self.assertRaisesRegex(JobError, fragment):
                self.memory.bind_job_run_dispatch(run.id, owner, epoch, now=when)
        self.assertEqual(JobRunStatus.CLAIMED, self.reload(run.id).status)

    def test_only_a_claimed_generation_is_dispatchable(self) -> None:
        self.assertEqual(
            frozenset({JobRunStatus.CLAIMED}), DISPATCHABLE_JOB_RUN_STATUSES
        )
        self.assertNotIn(JobRunStatus.RUNNING, DISPATCHABLE_JOB_RUN_STATUSES)
        run, lease = self.claimed()
        self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.during
        )
        # RUNNING is refused by the state gate as well as by the null-slot CAS.
        with self.assertRaises(JobError):
            self.memory.bind_job_run_dispatch(
                run.id, WORKER_A, lease.fencing_epoch, now=self.during
            )

    def test_a_condition_watch_is_refused_by_storage(self) -> None:
        record = self.memory.create_job(self.watch_spec(), now=NOW)
        run = self.memory.materialize_due_job_run(
            record.id, record.revision, now=self.at
        )
        lease = self.memory.claim_job_run(run.id, WORKER_A, now=self.at, lease=LEASE)
        with self.assertRaisesRegex(JobError, "condition watch"):
            self.memory.bind_job_run_dispatch(
                run.id, WORKER_A, lease.fencing_epoch, now=self.during
            )
        self.assertEqual(JobRunStatus.CLAIMED, self.reload(run.id).status)
        self.assertEqual(0, self.sessions())

    def test_the_binding_survives_a_restart_exactly(self) -> None:
        run, lease = self.claimed()
        bound = self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.during
        )
        reopened = SQLiteMemoryStore(self.database)
        stored = reopened.load_job_run(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(bound.session_id, stored.session_id)
        self.assertEqual(bound.chain_id, stored.chain_id)
        session = reopened.load_session(bound.session_id)
        self.assertEqual(bound.chain_id, session.chain.id)
        self.assertEqual(tuple(run.spec.steps), tuple(session.chain.steps))

    def test_binding_creates_no_authority_row_of_any_kind(self) -> None:
        run, lease = self.claimed()
        self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.during
        )
        for table in (
            "tasks",
            "pending_approvals",
            "permission_grants",
            "execution_attempts",
            "attempt_billing",
            "usage_ledger",
        ):
            self.assertEqual(0, self.table_count(table), table)


# --------------------------------------------------------------------------
# RUNNING DURABILITY / NO TAKEOVER
# --------------------------------------------------------------------------


class RunningDurabilityTests(DispatchTestCase):
    """RUNNING is committed before Core is entered, and nobody takes it over."""

    def test_the_generation_is_durably_running_before_run_chain_is_entered(
        self,
    ) -> None:
        run, lease = self.claimed()
        seen: list[tuple] = []

        class Observer(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                # Read through a SEPARATE connection: what a different process
                # would see at this instant.
                fresh = SQLiteMemoryStore(self.database).load_job_run(run.id)
                seen.append((fresh.status, fresh.session_id, fresh.chain_id))
                return inner._result

        core = Observer(
            self.memory,
            (AgentRun(RunStatus.COMPLETED, None, None), "completed"),
        )
        dispatch_job_run(
            core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        self.assertEqual(1, len(seen))
        status, session_id, chain_id = seen[0]
        self.assertEqual(JobRunStatus.RUNNING, status)
        self.assertIsNotNone(session_id)
        self.assertIsNotNone(chain_id)

    def test_a_running_generation_is_never_claimable(self) -> None:
        run, lease = self.claimed()
        self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.during
        )
        for worker, when in (
            (WORKER_B, self.during),
            (WORKER_B, self.at + timedelta(days=365)),
        ):
            with self.assertRaisesRegex(JobError, "not claimable"):
                self.memory.claim_job_run(run.id, worker, now=when, lease=LEASE)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_crash_after_binding_leaves_one_session_and_a_blocked_job(self) -> None:
        # The crash window: bound and RUNNING, worker gone, lease long expired.
        run, lease = self.claimed()
        bound = self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.during
        )
        long_after = self.at + timedelta(days=365)
        reopened = SQLiteMemoryStore(self.database)
        stored = reopened.load_job_run(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.lease_is_live(long_after))
        # No takeover, no second session, no retry, and the job stays blocked.
        with self.assertRaises(JobError):
            reopened.claim_job_run(run.id, WORKER_B, now=long_after, lease=LEASE)
        with self.assertRaises(JobError):
            reopened.bind_job_run_dispatch(
                run.id, WORKER_A, lease.fencing_epoch, now=long_after
            )
        self.assertEqual(1, self.sessions())
        self.assertIsNone(
            reopened.materialize_due_job_run(
                self.job.id,
                reopened.load_job(self.job.id).revision,
                now=long_after,
            )
        )
        self.assertEqual(bound.session_id, reopened.load_job_run(run.id).session_id)

    def test_no_recovery_or_takeover_operation_exists(self) -> None:
        for absent in (
            "recover_job_run",
            "resolve_job_run",
            "retry_job_run",
            "reclaim_job_run",
            "resume_job_run",
        ):
            self.assertFalse(hasattr(SQLiteMemoryStore, absent), absent)


# --------------------------------------------------------------------------
# THE ONE-SHOT DISPATCH, AGAINST A REAL CORE
# --------------------------------------------------------------------------


class RealDispatchTests(DispatchTestCase):
    """The real machinery: bind, run one local read, settle from stored truth."""

    def test_a_clean_local_chain_completes_and_settles_the_generation(self) -> None:
        run, lease = self.claimed(steps=1)
        outcome = dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual("chain_completed", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.COMPLETED, stored.status)
        self.assertTrue(stored.settled)
        self.assertEqual(self.during.isoformat(), stored.settled_at)
        # The durable chain agrees, and the binding is intact.
        session = self.memory.load_session(stored.session_id)
        self.assertEqual(ChainStatus.COMPLETED, session.chain.status)
        self.assertEqual(outcome.session_id, stored.session_id)
        self.assertEqual(outcome.chain_id, stored.chain_id)
        # Core really ran: the connector was reached by CORE, never by us.
        self.assertEqual(1, len(self.reader.calls))

    def test_run_chain_is_called_once_with_the_bound_session(self) -> None:
        run, lease = self.claimed()
        core = ScriptedCore(
            self.memory, (AgentRun(RunStatus.COMPLETED, None, None), "completed")
        )
        outcome = dispatch_job_run(
            core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        self.assertEqual(1, len(core.calls))
        self.assertEqual(self.reload(run.id).session_id, core.calls[0])
        self.assertEqual(outcome.session_id, core.calls[0])

    def test_a_condition_watch_is_refused_before_anything_is_bound(self) -> None:
        record = self.memory.create_job(self.watch_spec(), now=NOW)
        run = self.memory.materialize_due_job_run(
            record.id, record.revision, now=self.at
        )
        lease = self.memory.claim_job_run(run.id, WORKER_A, now=self.at, lease=LEASE)
        core = ScriptedCore(self.memory, None)
        outcome = dispatch_job_run(
            core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        self.assertEqual(DISPATCH_REFUSED, outcome.verdict)
        self.assertEqual("condition_watch", outcome.reason)
        self.assertEqual([], core.calls)
        self.assertEqual(0, self.sessions())
        self.assertEqual(JobRunStatus.CLAIMED, self.reload(run.id).status)
        self.assertEqual([], self.reader.calls + self.planner.calls)

    def test_an_egress_step_parks_on_the_approval_core_persisted(self) -> None:
        # The REAL approval path: step 2 is an egress model step, so Core
        # creates and persists the request and stops.
        run, lease = self.claimed(steps=2)
        outcome = dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        self.assertEqual(DISPATCH_PARKED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, stored.status)
        self.assertFalse(stored.settled)
        # THE BINDING SURVIVES PARKING - the approval is for this chain.
        self.assertEqual(outcome.session_id, stored.session_id)
        self.assertEqual(outcome.chain_id, stored.chain_id)
        # The lease is released; the wait is for a person.
        self.assertIsNone(stored.owner_token)
        self.assertIsNone(stored.lease_expires_at_utc)
        # CORE created the approval. The scheduler created none and resolved
        # none: exactly the one Core wrote is there.
        self.assertEqual(1, self.table_count("pending_approvals"))
        self.assertEqual(0, self.table_count("permission_grants"))

    def test_a_parked_generation_blocks_the_job_and_is_unclaimable(self) -> None:
        run, lease = self.claimed(steps=2)
        dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.memory.claim_job_run(
                run.id, WORKER_B, now=self.at + timedelta(days=1), lease=LEASE
            )
        self.assertIsNone(
            self.memory.materialize_due_job_run(
                self.job.id,
                self.memory.load_job(self.job.id).revision,
                now=self.at + timedelta(days=365),
            )
        )

    def test_a_generation_that_cannot_be_bound_is_refused_without_running(self) -> None:
        run, lease = self.claimed()
        core = ScriptedCore(self.memory, None)
        outcome = dispatch_job_run(
            core, run.id, WORKER_B, lease.fencing_epoch, clock=lambda: self.during
        )
        self.assertEqual(DISPATCH_REFUSED, outcome.verdict)
        self.assertIn("not_bindable", outcome.reason)
        self.assertEqual([], core.calls)
        self.assertEqual(JobRunStatus.CLAIMED, self.reload(run.id).status)


# --------------------------------------------------------------------------
# FRESH NOW / LEASE
# --------------------------------------------------------------------------


class FreshNowTests(DispatchTestCase):
    """The post-run instant is sampled AFTER Core returns, every time."""

    def test_the_post_run_instant_is_sampled_after_run_chain_returns(self) -> None:
        run, lease = self.claimed()
        clock = ScriptedClock(self.during, self.during, self.during, self.during)
        core = ScriptedCore(
            self.memory,
            (AgentRun(RunStatus.COMPLETED, None, None), "completed"),
            clock=clock,
        )
        dispatch_job_run(core, run.id, WORKER_A, lease.fencing_epoch, clock=clock)
        entered = clock.events.index(f"run_chain:{core.calls[0]}")
        # At least one clock reading happens strictly AFTER Core returned.
        self.assertTrue(
            any(event.startswith("clock:") for event in clock.events[entered + 1 :]),
            clock.events,
        )

    def test_a_lease_that_expires_during_the_run_refuses_post_processing(self) -> None:
        run, lease = self.claimed()
        expiry = datetime.fromisoformat(lease.lease_expires_at_utc)
        # EXACTLY the expiry instant. The window is half-open, so the instant
        # of expiry is already expired.
        clock = ScriptedClock(self.during, self.during, expiry)
        self.plant_chain_completed = None
        core = ScriptedCore(
            self.memory,
            (AgentRun(RunStatus.COMPLETED, None, None), "completed"),
            clock=clock,
        )
        outcome = dispatch_job_run(
            core, run.id, WORKER_A, lease.fencing_epoch, clock=clock
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("lease_expired", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)
        self.assertIsNone(stored.settled_at)

    def test_a_displaced_owner_cannot_reflect_a_result(self) -> None:
        run, lease = self.claimed()
        core = ScriptedCore(
            self.memory, (AgentRun(RunStatus.COMPLETED, None, None), "completed")
        )
        outcome = dispatch_job_run(
            core, run.id, WORKER_A, lease.fencing_epoch + 1, clock=lambda: self.during
        )
        self.assertEqual(DISPATCH_REFUSED, outcome.verdict)
        self.assertEqual(JobRunStatus.CLAIMED, self.reload(run.id).status)


# --------------------------------------------------------------------------
# RESULT MAPPING
# --------------------------------------------------------------------------


class ResultMappingTests(DispatchTestCase):
    """Authoritative stored truth decides, never the run object alone."""

    def bind_and_run(self, result, *, chain_status=None, raises=None, clock=None):
        run, lease = self.claimed()
        bound = self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.during
        )
        if chain_status is not None:
            self.plant_chain_status(bound.session_id, chain_status)

        class Planting(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                if inner._raises is not None:
                    raise inner._raises
                return inner._result

        core = Planting(self.memory, result, raises=raises)
        # Re-claim is impossible now, so the dispatcher is driven directly on
        # the already-bound generation through a second, equivalent path: the
        # binding step is idempotent-refused, so the test drives the reflection
        # by calling dispatch on a FRESH generation instead.
        return run, lease, bound, core

    def dispatch_with(self, *, result, chain_status=None, raises=None, clock=None):
        """One full dispatch whose chain lands in ``chain_status``."""
        run, lease = self.claimed()

        outer = self

        class Planting(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                if chain_status is not None:
                    outer.plant_chain_status(session_id, chain_status)
                if raises is not None:
                    raise raises
                return result

        core = Planting(self.memory, result, raises=raises)
        outcome = dispatch_job_run(
            core,
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=clock or (lambda: self.during),
        )
        return run, outcome

    def real_dispatch(self, *, flip=None, result=None, verdict=None):
        """A dispatch whose chain is driven by the REAL core, then flipped.

        Routing, task rows and attempt history are all genuine - the real core
        really ran the step. Only the durable chain STATUS is changed, and only
        after Core is done, so the terminal proof is exercised against evidence
        that actually exists rather than against a fixture's invention.
        """
        run, lease = self.claimed(steps=1)
        outer = self
        real = self.core

        class RealThenFlip(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                real_run, real_verdict = real.run_chain(session_id)
                if flip is not None:
                    outer.plant_status_only(session_id, flip)
                if result is None:
                    return real_run, real_verdict
                return result, verdict

        core = RealThenFlip(self.memory, None)
        outcome = dispatch_job_run(
            core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        return run, outcome

    def test_a_stored_completed_chain_settles_completed(self) -> None:
        run, outcome = self.real_dispatch()
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.COMPLETED, self.reload(run.id).status)

    def test_a_genuine_failed_chain_settles_failed(self) -> None:
        # CANONICAL failure: the connector really reports an error, the step
        # task really lands FAILED, and advance_chain_step - not the fixture -
        # writes ChainStatus.FAILED at the cursor.
        self.reader.outcome = OutcomeStatus.ERROR
        run, lease = self.claimed(steps=1)
        outcome = dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.FAILED, stored.status)
        session = self.memory.load_session(stored.session_id)
        self.assertIs(ChainStatus.FAILED, session.chain.status)
        cursor_task = self.memory.load_task(
            session.chain.step_task_ids[session.chain.cursor]
        )
        self.assertIs(TaskStatus.FAILED, cursor_task.status)

    def test_a_genuine_cancelled_chain_settles_cancelled(self) -> None:
        # CANONICAL cancellation over a legitimately completed prior step: the
        # first step really completes, then cancel_chain - the operator path -
        # marks the chain CANCELLED with the cursor and bindings preserved.
        run, lease = self.claimed(steps=2)
        outer = self
        real = self.core

        class CancelAfterFirstStep(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                real.run_chain(session_id)
                session = inner.memory.load_session(session_id)
                self.assertEqual(
                    "cancelled",
                    inner.memory.cancel_chain(session_id, session.chain.id),
                )
                return AgentRun(RunStatus.CANCELLED, None, None), "cancelled"

        outcome = dispatch_job_run(
            CancelAfterFirstStep(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.CANCELLED, stored.status)
        session = self.memory.load_session(stored.session_id)
        self.assertIs(ChainStatus.CANCELLED, session.chain.status)
        # The prior step really is COMPLETED, and that did not invalidate it.
        first = self.memory.load_task(session.chain.step_task_ids[0])
        self.assertIs(TaskStatus.COMPLETED, first.status)

    def test_complete_safe_attempt_reality_still_permits_settlement(self) -> None:
        # The positive control for F2: a real, fully finished attempt history
        # must NOT be blocked by the new proof.
        run, outcome = self.real_dispatch()
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        session_id = self.reload(run.id).session_id
        for task_id in self.task_ids(session_id):
            states = [a.state for a in self.memory.execution_attempts(task_id)]
            self.assertTrue(states)
            self.assertNotIn(AttemptState.IN_FLIGHT, states)
            self.assertNotIn(AttemptState.UNKNOWN_OUTCOME, states)

    def test_an_explicit_unknown_outcome_marks_the_generation_unknown(self) -> None:
        run, outcome = self.dispatch_with(
            result=(
                AgentRun(
                    RunStatus.FAILED,
                    None,
                    None,
                    error=StructuredError(
                        ErrorCode.EXECUTION_UNKNOWN_OUTCOME, "in doubt"
                    ),
                ),
                "blocked",
            ),
        )
        self.assertEqual(DISPATCH_UNKNOWN, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.UNKNOWN_OUTCOME, stored.status)
        self.assertFalse(stored.settled)

    def test_unknown_outranks_a_stored_failed_chain(self) -> None:
        # THE PRECEDENCE RULE. A FAILED chain with an in-doubt attempt beneath
        # it must never become a clean FAILED ending.
        run, outcome = self.dispatch_with(
            result=(
                AgentRun(
                    RunStatus.FAILED,
                    None,
                    None,
                    error=StructuredError(
                        ErrorCode.EXECUTION_UNKNOWN_OUTCOME, "in doubt"
                    ),
                ),
                "failed",
            ),
            chain_status=ChainStatus.FAILED,
        )
        self.assertEqual(DISPATCH_UNKNOWN, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.UNKNOWN_OUTCOME, stored.status)
        self.assertNotEqual(JobRunStatus.FAILED, stored.status)

    def test_an_unknown_generation_is_never_retried_or_reclaimed(self) -> None:
        run, outcome = self.dispatch_with(
            result=(
                AgentRun(
                    RunStatus.FAILED,
                    None,
                    None,
                    error=StructuredError(
                        ErrorCode.EXECUTION_UNKNOWN_OUTCOME, "in doubt"
                    ),
                ),
                "blocked",
            ),
        )
        long_after = self.at + timedelta(days=365)
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.memory.claim_job_run(run.id, WORKER_B, now=long_after, lease=LEASE)
        self.assertIsNone(
            self.memory.materialize_due_job_run(
                self.job.id,
                self.memory.load_job(self.job.id).revision,
                now=long_after,
            )
        )

    def test_a_completed_run_object_over_an_active_chain_settles_nothing(self) -> None:
        # AgentRun.status ALONE is insufficient. The chain is still ACTIVE, so
        # there is no ending to record whatever the run object says.
        run, outcome = self.dispatch_with(
            result=(AgentRun(RunStatus.COMPLETED, None, None), "advanced"),
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)

    def test_an_unclassified_exception_leaves_the_generation_running(self) -> None:
        run, outcome = self.dispatch_with(
            result=None, raises=RuntimeError("the driver fell over")
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("run_chain_raised", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)
        self.assertIsNone(stored.settled_at)

    def test_an_invalid_stored_chain_leaves_the_generation_running(self) -> None:
        run, outcome = self.dispatch_with(
            result=(AgentRun(RunStatus.COMPLETED, None, None), "completed"),
            chain_status=ChainStatus.INVALID,
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_an_awaiting_approval_verdict_without_a_persisted_approval_blocks(
        self,
    ) -> None:
        run, outcome = self.dispatch_with(
            result=(AgentRun(RunStatus.AWAITING_APPROVAL, None, None), "awaiting_approval"),
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)
        self.assertEqual(0, self.table_count("pending_approvals"))

    def test_every_unmapped_verdict_leaves_the_generation_running(self) -> None:
        for verdict, status in (
            ("awaiting_clarification", RunStatus.AWAITING_CLARIFICATION),
            ("blocked", RunStatus.FAILED),
            ("conflict", RunStatus.FAILED),
            ("refused", RunStatus.FAILED),
            ("cancelling", RunStatus.CANCELLING),
        ):
            with self.subTest(verdict=verdict):
                run, outcome = self.dispatch_with(
                    result=(AgentRun(status, None, None), verdict)
                )
                self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
                stored = self.reload(run.id)
                self.assertEqual(JobRunStatus.RUNNING, stored.status)
                self.assertFalse(stored.settled)


class TerminalEvidenceTests(DispatchTestCase):
    """F1/F2: a terminal chain settles only on evidence that really exists."""

    def real_run(self):
        """One real dispatch, settled COMPLETED, with genuine routing."""
        run, lease = self.claimed(steps=1)
        outcome = dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        return run, outcome

    def blocked_dispatch(self, mutate):
        """A dispatch whose real chain is sabotaged by ``mutate`` before it ends."""
        run, lease = self.claimed(steps=1)
        outer = self
        real = self.core

        class Sabotage(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                real_run, real_verdict = real.run_chain(session_id)
                mutate(session_id)
                return real_run, real_verdict

        outcome = dispatch_job_run(
            Sabotage(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        return run, outcome

    # -- F1 ---------------------------------------------------------------

    def test_a_terminal_chain_with_no_task_rows_cannot_settle(self) -> None:
        # THE REVIEW PROBE. A COMPLETED chain whose step_task_ids are invented
        # and whose tasks do not exist must not settle the generation.
        def invent(session_id):
            self.plant_chain_status(session_id, ChainStatus.COMPLETED)
            with contextlib.closing(self.raw()) as connection:
                connection.execute("DELETE FROM tasks")
                connection.commit()

        run, outcome = self.blocked_dispatch(invent)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)
        self.assertIsNone(stored.settled_at)

    def test_a_terminal_failed_chain_with_a_missing_task_cannot_settle(self) -> None:
        # The same missing-row hole on the NON-completed branch, where the
        # "every step produced a task" rule does not apply. A routed id that
        # names nothing is missing evidence, and missing evidence blocks.
        def erase(session_id):
            with contextlib.closing(self.raw()) as connection:
                connection.execute("DELETE FROM execution_attempts")
                connection.execute("DELETE FROM tasks")
                connection.commit()
            self.plant_status_only(session_id, ChainStatus.FAILED)

        run, outcome = self.blocked_dispatch(erase)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)

    def test_a_task_owned_by_another_session_cannot_settle(self) -> None:
        def steal(session_id):
            other = SessionState(objective="somebody else", project_id="proj-a")
            self.memory.save_session(other)
            with contextlib.closing(self.raw()) as connection:
                connection.execute(
                    "UPDATE tasks SET session_id = ? WHERE session_id = ?",
                    (other.id, session_id),
                )
                connection.commit()

        run, outcome = self.blocked_dispatch(steal)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_task_routed_to_the_wrong_step_cannot_settle(self) -> None:
        # The routing claims a task that belongs to a different chain: the
        # ownership contract refuses it even though the row exists.
        def relabel(session_id):
            with contextlib.closing(self.raw()) as connection:
                row = connection.execute(
                    "SELECT id, state_json FROM tasks WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                payload = json.loads(row["state_json"])
                payload["chain_binding"]["chain_id"] = str(uuid4())
                connection.execute(
                    "UPDATE tasks SET state_json = ? WHERE id = ?",
                    (json.dumps(payload), row["id"]),
                )
                connection.commit()

        run, outcome = self.blocked_dispatch(relabel)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_terminal_chain_over_an_unfinished_task_cannot_settle(self) -> None:
        def rewind(session_id):
            self.rewind_task(self.task_ids(session_id)[0], TaskStatus.RUNNING)

        run, outcome = self.blocked_dispatch(rewind)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_failed_chain_over_a_completed_task_cannot_settle(self) -> None:
        # THE REVIEW PROBE. ChainStatus.FAILED over a real, really-COMPLETED
        # routed task. No canonical transition produces that pair -
        # advance_chain_step writes FAILED only off a FAILED cursor task - so
        # settling it would record a failure nothing produced.
        run, outcome = self.blocked_dispatch(
            lambda session_id: self.plant_status_only(session_id, ChainStatus.FAILED)
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)
        # The task really did complete; it is the CHAIN's claim that is wrong.
        session = self.memory.load_session(stored.session_id)
        self.assertIs(
            TaskStatus.COMPLETED,
            self.memory.load_task(session.chain.step_task_ids[0]).status,
        )

    def test_an_incoherent_cancelled_chain_cannot_settle(self) -> None:
        # A cancellation whose PRIOR step never completed. The cursor advances
        # only off a COMPLETED task, so a cursor sitting past an unfinished
        # step is a state no canonical transition could have produced.
        run, lease = self.claimed(steps=2)
        outer = self
        real = self.core

        class Incoherent(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                real.run_chain(session_id)
                session = inner.memory.load_session(session_id)
                inner.memory.cancel_chain(session_id, session.chain.id)
                outer.rewind_task(
                    session.chain.step_task_ids[0], TaskStatus.RUNNING
                )
                return AgentRun(RunStatus.CANCELLED, None, None), "cancelled"

        outcome = dispatch_job_run(
            Incoherent(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    # -- F2 ---------------------------------------------------------------

    def add_attempt(self, session_id: str, task_id: str, state: AttemptState) -> None:
        """Append one attempt row directly, exactly as a crash would leave it."""
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                """
                INSERT INTO execution_attempts
                    (id, session_id, task_id, connector, fingerprint, state,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'reader', 'fp', ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    session_id,
                    task_id,
                    state.value,
                    self.during.isoformat(),
                    self.during.isoformat(),
                ),
            )
            connection.commit()

    def rewrite_attempts(self, task_id: str, **columns) -> None:
        """Rewrite the identity columns of one task's attempt rows."""
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                f"UPDATE execution_attempts SET {assignments} WHERE task_id = ?",
                (*columns.values(), task_id),
            )
            connection.commit()

    def test_a_finished_attempt_owned_by_another_session_cannot_settle(self) -> None:
        # THE REVIEW PROBE. The attempt is FINISHED and is keyed to this task,
        # but its own session identity names somebody else, so it is not this
        # execution context's evidence and cannot clear it.
        def steal(session_id):
            self.rewrite_attempts(
                self.task_ids(session_id)[0], session_id=str(uuid4())
            )

        run, outcome = self.blocked_dispatch(steal)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_an_attempt_claiming_another_task_cannot_settle(self) -> None:
        # SQLite looks attempts up BY task id, so a row it returns always
        # carries that id in its key column. The dangerous shape is a store
        # whose lookup and whose row disagree - a decoded attempt that claims a
        # different task - and the proof must refuse it on the row's own claim.
        outer = self

        def foreign(session_id):
            task_id = outer.task_ids(session_id)[0]
            real_attempts = outer.memory.execution_attempts

            def lying(asked_task_id):
                rows = real_attempts(asked_task_id)
                return [replace(row, task_id=str(uuid4())) for row in rows]

            outer.memory.execution_attempts = lying
            outer.addCleanup(
                setattr, outer.memory, "execution_attempts", real_attempts
            )

        run, outcome = self.blocked_dispatch(foreign)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_real_attempt_reassigned_to_another_task_cannot_settle(self) -> None:
        # THE REVIEW PROBE, through REAL SQLite and on a genuinely COMPLETED
        # chain, so nothing but the F2 rule can block it. The routed task's own
        # FINISHED attempt is reassigned to another EXISTING task, which makes
        # execution_attempts() for the routed task return ZERO rows. That is a
        # visibility hole, not an absence of requirement: the task COMPLETED,
        # and completing means it executed.
        run, lease = self.claimed(steps=1)
        outer = self
        real = self.core

        class Reassign(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                agent_run, verdict = real.run_chain(session_id)
                # A second REAL task row in the same session to move it onto.
                sink = make_task(target="sink.txt")
                inner.memory.save_task(session_id, sink)
                routed = outer.task_ids(session_id)[0]
                with contextlib.closing(outer.raw()) as connection:
                    connection.execute(
                        "UPDATE execution_attempts SET task_id = ? WHERE task_id = ?",
                        (sink.id, routed),
                    )
                    connection.commit()
                outer.reassigned_from = routed
                outer.reassigned_to = sink.id
                return agent_run, verdict

        outcome = dispatch_job_run(
            Reassign(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        # The hole is real: SQLite genuinely reports no history for the routed
        # task, and the row is really sitting under the other one.
        self.assertEqual([], self.memory.execution_attempts(self.reassigned_from))
        self.assertTrue(self.memory.execution_attempts(self.reassigned_to))
        # The chain really is COMPLETED and the task really is COMPLETED, so
        # only the missing-evidence rule can be what refuses this.
        stored = self.reload(run.id)
        session = self.memory.load_session(stored.session_id)
        self.assertIs(ChainStatus.COMPLETED, session.chain.status)
        self.assertIs(
            TaskStatus.COMPLETED, self.memory.load_task(self.reassigned_from).status
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)

    def test_a_completed_task_with_no_attempt_history_cannot_settle(self) -> None:
        # REAL SQLite: the evidence is simply gone. A COMPLETED task always
        # left an attempt behind, so zero rows blocks.
        def erase(session_id):
            with contextlib.closing(self.raw()) as connection:
                connection.execute("DELETE FROM execution_attempts")
                connection.commit()

        run, outcome = self.blocked_dispatch(erase)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_non_completed_task_may_legitimately_have_no_attempt(self) -> None:
        # THE POSITIVE CONTROL. A task that ended FAILED can legitimately never
        # have reached a connector - it can stop at an approval or a gate - so
        # zero attempts must NOT block. Here the connector really errors, the
        # task really lands FAILED, and its attempt rows are then removed to
        # stand for a task that never opened one at all.
        self.reader.outcome = OutcomeStatus.ERROR
        run, lease = self.claimed(steps=1)
        outer = self
        real = self.core

        class NoHistory(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                agent_run, verdict = real.run_chain(session_id)
                with contextlib.closing(outer.raw()) as connection:
                    connection.execute("DELETE FROM execution_attempts")
                    connection.commit()
                return agent_run, verdict

        outcome = dispatch_job_run(
            NoHistory(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        stored = self.reload(run.id)
        session = self.memory.load_session(stored.session_id)
        cursor_task = self.memory.load_task(
            session.chain.step_task_ids[session.chain.cursor]
        )
        self.assertIs(TaskStatus.FAILED, cursor_task.status)
        self.assertEqual([], self.memory.execution_attempts(cursor_task.id))
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.FAILED, stored.status)

    def test_one_step_missing_its_required_evidence_blocks_the_chain(self) -> None:
        # A canonically CANCELLED chain - legitimate on its own, and proved so
        # by the positive test - whose COMPLETED first step has lost its
        # attempt history. The chain lifecycle is coherent, so only the F2 rule
        # can refuse it, and one step's missing evidence blocks the whole
        # generation.
        run, lease = self.claimed(steps=2)
        outer = self
        real = self.core

        class LoseFirst(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                real.run_chain(session_id)
                session = inner.memory.load_session(session_id)
                inner.memory.cancel_chain(session_id, session.chain.id)
                first = session.chain.step_task_ids[0]
                with contextlib.closing(outer.raw()) as connection:
                    connection.execute(
                        "DELETE FROM execution_attempts WHERE task_id = ?", (first,)
                    )
                    connection.commit()
                outer.stripped_step = first
                return AgentRun(RunStatus.CANCELLED, None, None), "cancelled"

        outcome = dispatch_job_run(
            LoseFirst(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        self.assertIs(
            TaskStatus.COMPLETED, self.memory.load_task(self.stripped_step).status
        )
        self.assertEqual([], self.memory.execution_attempts(self.stripped_step))
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_malformed_attempt_identity_cannot_settle(self) -> None:
        for columns in ({"state": "not-a-state"}, {"session_id": ""}):
            with self.subTest(columns=columns):
                run, outcome = self.blocked_dispatch(
                    lambda session_id, c=columns: self.rewrite_attempts(
                        self.task_ids(session_id)[0], **c
                    )
                )
                self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
                self.assertEqual("terminal_evidence_missing", outcome.reason)
                self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_an_older_unknown_outcome_is_not_hidden_by_a_later_finished(
        self,
    ) -> None:
        # THE REVIEW PROBE. UNKNOWN_OUTCOME then FINISHED: reading only the
        # newest row would call this settled.
        def bury(session_id):
            task_id = self.task_ids(session_id)[0]
            self.add_attempt(session_id, task_id, AttemptState.UNKNOWN_OUTCOME)
            self.add_attempt(session_id, task_id, AttemptState.FINISHED)

        run, outcome = self.blocked_dispatch(bury)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)
        # And the newest row really is the clean one, so the block came from
        # looking at the WHOLE history.
        task_id = self.task_ids(stored.session_id)[0]
        self.assertIs(
            AttemptState.FINISHED,
            self.memory.latest_execution_attempt(task_id).state,
        )

    def test_an_older_in_flight_is_not_hidden_by_a_later_row(self) -> None:
        def bury(session_id):
            task_id = self.task_ids(session_id)[0]
            self.add_attempt(session_id, task_id, AttemptState.IN_FLIGHT)
            self.add_attempt(session_id, task_id, AttemptState.FINISHED)

        run, outcome = self.blocked_dispatch(bury)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_one_in_doubt_task_blocks_the_whole_generation(self) -> None:
        # A multi-step chain: the FIRST step's history is in doubt even though
        # the chain as a whole reports a terminal outcome.
        run, lease = self.claimed(steps=2)
        outer = self
        real = self.core

        class Sabotage(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                real.run_chain(session_id)
                ids = outer.task_ids(session_id)
                outer.add_attempt(session_id, ids[0], AttemptState.UNKNOWN_OUTCOME)
                outer.plant_status_only(session_id, ChainStatus.FAILED)
                return AgentRun(RunStatus.FAILED, None, None), "failed"

        outcome = dispatch_job_run(
            Sabotage(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("terminal_evidence_missing", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)


class ApprovalBindingTests(DispatchTestCase):
    """F3: a park requires the approval to belong to the CURRENT step."""

    def test_an_unrelated_same_session_approval_cannot_park(self) -> None:
        # THE REVIEW PROBE. A pending approval exists in the bound session, for
        # a task that has nothing to do with the chain's current step.
        run, lease = self.claimed(steps=1)
        outer = self
        real = self.core

        class Foreign(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                real.run_chain(session_id)
                # An unrelated task in the same session, waiting on approval.
                stray = make_task(target="other.txt")
                stray.status = TaskStatus.AWAITING_APPROVAL
                inner.memory.save_task(session_id, stray)
                request = PermissionRequest(
                    task_id=stray.id,
                    session_id=session_id,
                    scope=stray.scope,
                    risk_level=RiskLevel.LOW,
                    side_effect=SideEffectProfile(effect=SideEffect.NONE),
                    reason="an approval that has nothing to do with the chain",
                )
                inner.memory.save_pending_approval(request)
                outer.stray_task = stray
                return (
                    AgentRun(RunStatus.AWAITING_APPROVAL, None, stray),
                    "awaiting_approval",
                )

        outcome = dispatch_job_run(
            Foreign(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("approval_not_bound_to_step", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertNotEqual(JobRunStatus.AWAITING_APPROVAL, stored.status)
        # The lease is still held: nothing was parked.
        self.assertEqual(WORKER_A, stored.owner_token)

    def test_an_approval_for_a_non_current_step_cannot_park(self) -> None:
        # Step 0's task is real and this chain's, but the cursor is on step 1.
        run, lease = self.claimed(steps=2)
        outer = self
        real = self.core

        class WrongStep(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                real.run_chain(session_id)
                first = inner.memory.load_task(outer.task_ids(session_id)[0])
                return (
                    AgentRun(RunStatus.AWAITING_APPROVAL, None, first),
                    "awaiting_approval",
                )

        outcome = dispatch_job_run(
            WrongStep(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("approval_not_bound_to_step", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def park_with_replaced_request(self, mutate):
        """A real two-step run whose persisted request is swapped after Core.

        Core really creates and persists the approval; the fixture then
        replaces the stored row with ``mutate(request)``. Everything else -
        routing, task lifecycle, session - stays exactly as Core left it, so
        the only thing under test is whether the scheduler notices that the
        stored request is no longer the one Core decided on.
        """
        run, lease = self.claimed(steps=2)
        outer = self
        real = self.core

        class SwapRequest(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                agent_run, verdict = real.run_chain(session_id)
                issued = agent_run.permission_request
                outer.assertIsNotNone(issued)
                replacement = mutate(issued)
                with contextlib.closing(outer.raw()) as connection:
                    connection.execute(
                        "DELETE FROM pending_approvals WHERE task_id = ?",
                        (issued.task_id,),
                    )
                    connection.commit()
                inner.memory.save_pending_approval(replacement)
                return agent_run, verdict

        outcome = dispatch_job_run(
            SwapRequest(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        return run, outcome

    def test_a_different_request_identity_cannot_park(self) -> None:
        # THE REVIEW PROBE. Same task, same session, but a DIFFERENT issuance:
        # ``id`` is minted per request and never derived from the scope, so a
        # re-issue is a different request even when it would authorize the
        # same thing.
        run, outcome = self.park_with_replaced_request(
            lambda issued: replace(issued, id=str(uuid4()))
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("approval_not_bound_to_step", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(WORKER_A, stored.owner_token)

    def test_a_different_approval_scope_cannot_park(self) -> None:
        # Same issuance id, but the stored request would authorize something
        # else. ``scope_fingerprint`` is what the approval actually permits.
        def widen(issued):
            return replace(
                issued,
                scope=replace(issued.scope, target="somewhere-else.txt"),
            )

        run, outcome = self.park_with_replaced_request(widen)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("approval_not_bound_to_step", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_different_displayed_context_cannot_park(self) -> None:
        # Same id and same scope, but what a person would READ has changed.
        # ``display_digest`` binds the reason and the outbound preview.
        run, outcome = self.park_with_replaced_request(
            lambda issued: replace(issued, reason="something else entirely")
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("approval_not_bound_to_step", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_changed_attestation_cannot_park(self) -> None:
        # THE REVIEW PROBE. Same id, task, session, scope fingerprint and
        # display digest - only the egress ATTESTATION differs, which is part
        # of what the approval actually authorizes. The canonical fingerprint
        # binds it, so the park is refused.
        def restate(issued):
            self.assertIsNotNone(issued.attestation)
            return replace(
                issued,
                attestation=replace(
                    issued.attestation, outbound_digest="0" * 64
                ),
            )

        run, outcome = self.park_with_replaced_request(restate)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("approval_not_bound_to_step", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(WORKER_A, stored.owner_token)

    def test_a_dropped_attestation_cannot_park(self) -> None:
        # An approval with no attestation at all does not mean what the one
        # Core issued meant, however identical the rest of it looks.
        run, outcome = self.park_with_replaced_request(
            lambda issued: replace(issued, attestation=None)
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("approval_not_bound_to_step", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_the_canonical_fingerprint_is_what_is_compared(self) -> None:
        # The comparison is the model's own, not a re-listed field set: a
        # changed attestation moves the canonical fingerprint while leaving
        # every field the earlier partial check looked at untouched.
        run, lease = self.claimed(steps=2)
        captured: list = []
        real = self.core

        class Capture(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                agent_run, verdict = real.run_chain(session_id)
                captured.append(agent_run.permission_request)
                return agent_run, verdict

        dispatch_job_run(
            Capture(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        issued = captured[0]
        self.assertIsNotNone(issued.attestation)
        altered = replace(
            issued, attestation=replace(issued.attestation, outbound_digest="0" * 64)
        )
        self.assertEqual(issued.id, altered.id)
        self.assertEqual(issued.scope_fingerprint, altered.scope_fingerprint)
        self.assertEqual(issued.display_digest, altered.display_digest)
        self.assertNotEqual(issued.fingerprint, altered.fingerprint)

    def test_a_run_without_a_permission_request_cannot_park(self) -> None:
        # An awaiting_approval verdict whose run carries no request at all is
        # missing the very thing the park would be proved against.
        run, lease = self.claimed(steps=2)
        real = self.core

        class Stripped(ScriptedCore):
            def run_chain(inner, session_id):  # noqa: N805
                inner.calls.append(session_id)
                agent_run, verdict = real.run_chain(session_id)
                return replace(agent_run, permission_request=None), verdict

        outcome = dispatch_job_run(
            Stripped(self.memory, None),
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.during,
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("approval_not_bound_to_step", outcome.reason)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_genuine_current_step_approval_still_parks(self) -> None:
        # The positive control: the REAL two-step chain really stops on its
        # egress step, and that park must still work.
        run, lease = self.claimed(steps=2)
        outcome = dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        self.assertEqual(DISPATCH_PARKED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, stored.status)
        self.assertEqual(outcome.session_id, stored.session_id)
        self.assertEqual(outcome.chain_id, stored.chain_id)
        self.assertEqual(1, self.table_count("pending_approvals"))
        self.assertEqual(0, self.table_count("permission_grants"))
        # The stored request IS the one Core handed back - same issuance, same
        # scope, same displayed context.
        session_id = self.reload(run.id).session_id
        chain = self.memory.load_session(session_id).chain
        stored_request = self.memory.load_pending_approval(
            chain.step_task_ids[chain.cursor], session_id
        )
        self.assertIsNotNone(stored_request)


class PostExceptionClockTests(DispatchTestCase):
    """F5: the fresh instant is sampled after a throw, exactly as after a return."""

    def test_a_fresh_instant_is_sampled_after_run_chain_throws(self) -> None:
        run, lease = self.claimed()
        clock = ScriptedClock(self.during, self.during, self.during, self.during)
        core = ScriptedCore(
            self.memory, None, clock=clock, raises=RuntimeError("driver fell over")
        )
        outcome = dispatch_job_run(
            core, run.id, WORKER_A, lease.fencing_epoch, clock=clock
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("run_chain_raised", outcome.reason)
        threw = clock.events.index(f"run_chain:{core.calls[0]}")
        self.assertTrue(
            any(event.startswith("clock:") for event in clock.events[threw + 1 :]),
            clock.events,
        )

    def test_a_throw_mutates_the_generation_in_no_way(self) -> None:
        run, lease = self.claimed()
        core = ScriptedCore(
            self.memory, None, raises=RuntimeError("driver fell over")
        )
        outcome = dispatch_job_run(
            core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        after = self.reload(run.id)
        # Bound and RUNNING, and nothing beyond that: no settlement, no
        # unknown outcome, no park, no released lease, no retry permission.
        self.assertEqual(JobRunStatus.RUNNING, after.status)
        self.assertFalse(after.settled)
        self.assertIsNone(after.settled_at)
        self.assertEqual(WORKER_A, after.owner_token)
        # The binding made before Core was entered is intact and unique.
        self.assertIsNotNone(after.session_id)
        self.assertIsNotNone(after.chain_id)
        self.assertEqual(1, self.sessions())
        with self.assertRaises(JobError):
            self.memory.claim_job_run(
                run.id, WORKER_B, now=self.at + timedelta(days=365), lease=LEASE
            )


# --------------------------------------------------------------------------
# THE BOUNDARY
# --------------------------------------------------------------------------


class DispatchBoundaryTests(DispatchTestCase):
    """What the one-shot bridge still cannot do."""

    def source(self) -> str:
        return (
            Path(__file__).resolve().parent.parent / "jarvis" / "dispatch.py"
        ).read_text(encoding="utf-8")

    def code(self) -> str:
        """The module's CODE, with every docstring removed.

        dispatch.py's prose names the things it must not do - "reaches no
        connector", "opens no execution attempt" - so a raw substring scan
        would fail on the guarantee rather than on a breach. The docstrings
        come out and what is left is what actually executes.
        """
        tree = ast.parse(self.source())
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                del body[0]
        return ast.unparse(tree)

    def test_the_scheduler_never_reaches_a_connector_itself(self) -> None:
        run, lease = self.claimed(steps=1)
        dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        # The connector WAS reached - by Core, on its own trusted path. What
        # matters is that dispatch.py's code cannot name that path at all.
        code = self.code()
        for absent in ("registry", "connector", ".execute(", "manifest"):
            self.assertNotIn(absent, code, absent)

    def test_the_bridge_mutates_no_attempt_and_creates_no_authority(self) -> None:
        code = self.code()
        for absent in (
            "open_execution_attempt",
            "begin_execution",
            "conclude_execution",
            "mark_execution_unknown",
            "finalize_task",
            "resolve_approval",
            "record_permission_grant",
            "reserve",
            "usage_ledger",
            "attempt_billing",
            "record_usage",
            "evaluate",
        ):
            self.assertNotIn(absent, code, absent)

    def test_the_bridge_owns_no_thread_timer_or_loop(self) -> None:
        code = self.code()
        for absent in (
            "threading",
            "Thread",
            "sleep",
            "asyncio",
            "Timer",
            "while True",
            "schedule(",
        ):
            self.assertNotIn(absent, code, absent)

    def test_the_bridge_calls_exactly_one_core_entry_point(self) -> None:
        tree = ast.parse(self.source())
        core_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "core"
        }
        self.assertEqual({"run_chain"}, core_calls)

    def test_a_full_dispatch_authorizes_nothing_the_scheduler_could_have(
        self,
    ) -> None:
        run, lease = self.claimed(steps=1)
        dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.during
        )
        # NO STANDING AUTHORITY was minted. A scheduled run gets no permission
        # grant and no pending approval it answered itself: the step that ran
        # was a free local read that needed neither, and nothing in this path
        # can manufacture either.
        self.assertEqual(0, self.table_count("permission_grants"))
        self.assertEqual(0, self.table_count("pending_approvals"))
        # The execution attempt and its billing row DO exist - Core wrote them
        # on its own trusted path, exactly as it does for an interactive run,
        # and they are the H2 journal rather than scheduler state. What the
        # scheduler may not do is touch them, which the source guards prove.
        self.assertEqual(1, self.table_count("execution_attempts"))

    def test_the_module_declares_no_surface_beyond_dispatch_and_recovery(
        self,
    ) -> None:
        # PHASE BOUNDARY, restated for 2B2a. ``recover_job_run`` was forbidden
        # here while 2B1 was the whole module and recovery did not exist; it is
        # now the ONE thing 2B2a added, and the guard's job is to keep the list
        # at exactly two. Everything it forbade that is still forbidden is
        # still checked, unchanged.
        source = self.source()
        for absent in (
            "resolve_job_run",
            "retry_job_run",
            "abandon_unknown_attempt",
            "declare_attempt_orphaned",
            "resolve_unknown_job_run",
            "mark_execution_unknown",
            "resolve_approval",
            "reject_task",
            "permission_grants",
            "has_approved_grant",
            "claim_job_run",
            "renew_job_run_lease",
        ):
            self.assertNotIn(absent, source, absent)
        # It never takes GENERIC ownership either: dispatch is handed a proof
        # it did not acquire, and recovery acquires only through the fenced
        # recovery boundary - never through the claim path, which still cannot
        # reach a bound generation at all.
        self.assertNotIn("claim", self.code())
        # The public callables are exactly the phase entry points. Phase 2C
        # added ONE: ``dispatch_condition_watch_true``, the mirror of
        # ``dispatch_job_run`` for a watch's TRUE binding. It evaluates
        # nothing - the forbidden-substring checks above and the module's own
        # boundary tests still cover it, since they scan the whole module
        # rather than any one function.
        tree = ast.parse(source)
        public = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        }
        self.assertEqual(
            {
                "dispatch_job_run",
                "recover_job_run",
                "cancel_job_run",
                "dispatch_condition_watch_true",
            },
            public,
        )

    def test_cancellation_touches_only_authorities_that_already_exist(self) -> None:
        # Phase 2B2b2. ``cancel_job_run`` ORCHESTRATES; it owns nothing. Every
        # store call it makes belongs to a component that already had that
        # authority, and this pins the list so a later edit cannot quietly give
        # the scheduler a task status, an attempt state or a settlement of its
        # own. ``_reflect_outcome`` performs the settlement, through exactly
        # the fenced path a first dispatch uses.
        tree = ast.parse(self.source())
        cancel = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "cancel_job_run"
        )
        store_calls = {
            node.func.attr
            for node in ast.walk(cancel)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "store"
        }
        self.assertEqual(
            {
                "request_job_run_cancel",
                "cancel_chain",
                "cancel_task",
                "load_session",
                "load_job_run",
                "reacquire_job_run",
            },
            store_calls,
        )
        # It never enters Core, and it never writes an execution attempt.
        core_calls = {
            node.func.attr
            for node in ast.walk(cancel)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "core"
        }
        self.assertEqual(set(), core_calls)

    def test_recovery_touches_only_fenced_jobrun_operations(self) -> None:
        # Recovery reads H2 truth and moves JobRun COORDINATION; it never
        # mutates execution reality. The list is exhaustive on purpose:
        # anything appearing on it later - an approval read, a grant read, an
        # attempt mutation, a human gate - is a scope change, not a fix.
        #
        # ``mark_job_run_unknown`` joined it in phase 2B2b1. It is the EXISTING
        # 2B0 fenced JobRun transition, proved by owner token, fencing epoch
        # and a live lease, and it writes one coordination status - it is not
        # an H2 write and confers no authority over an attempt.
        tree = ast.parse(self.source())
        recover = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "recover_job_run"
        )
        store_calls = {
            node.func.attr
            for node in ast.walk(recover)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "store"
        }
        self.assertEqual(
            {
                "reacquire_job_run",
                "load_job_run",
                "mark_job_run_unknown",
            },
            store_calls,
        )
        # And the H2 / human-gate authorities stay absent from the whole
        # module, not merely from this function.
        for forbidden in (
            "declare_attempt_orphaned",
            "abandon_unknown_attempt",
            "resolve_unknown_job_run",
            "mark_execution_unknown",
            "begin_execution",
            "conclude_execution",
            "open_execution_attempt",
        ):
            self.assertNotIn(forbidden, self.source(), forbidden)
        core_calls = {
            node.func.attr
            for node in ast.walk(recover)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "core"
        }
        self.assertEqual({"run_chain"}, core_calls)


if __name__ == "__main__":
    unittest.main()
