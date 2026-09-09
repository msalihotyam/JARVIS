"""Scheduler phase 2C: minimum condition-watch evaluation.

Covers ``evaluate_condition_watch``'s whole boundary: FALSE and UNKNOWN
settling the generation SKIPPED with the result persisted and reaching
neither a session nor Core; TRUE binding ``JobSpec.on_true`` in the same
fenced transition that persists ``condition_result = "true"`` and then
running exactly once through the existing dispatch tail; stale fencing
refusing both the settle and the bind path; a restart before either write
re-evaluating safely; a restart after a TRUE bind reaching its existing
recovery path rather than a second session; a cancellation recorded before
evaluation losing the TRUE bind to the same CAS every other binder loses to;
and the observation itself never bypassing the hardened, root-bound reader
Core's own registry already holds.

Every instant is EXPLICIT and every clock is injected. Nothing sleeps,
nothing polls, and no test performs a live model, auth or network call.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from jarvis.conditions import ConditionKind, LocalReadCondition
from jarvis.connectors.local_files import LocalFileReadConnector
from jarvis.contracts import (
    ChainStep,
    JobError,
    JobKind,
    JobRunStatus,
    JobSpec,
    ProjectRecord,
)
from jarvis.destinations import CLAUDE_CODE_DESTINATION, DestinationGrant
from jarvis.dispatch import (
    DISPATCH_REFUSED,
    DISPATCH_SETTLED,
    dispatch_condition_watch_true,
    recover_job_run,
)
from jarvis.condition_watch import evaluate_condition_watch
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, open_core, safe_cleanup
from tests.test_scheduler_jobs import (  # noqa: F401
    DIGEST,
    NOW,
    interval_schedule,
    setUpModule,
    tearDownModule,
)

WORKER_A = "worker-a"
WORKER_B = "worker-b"
LEASE = timedelta(minutes=5)
ACTOR = "operator"
WHY = "test"


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class ConditionWatchTestCase(unittest.TestCase):
    """A real schema-10 store, a real registry, and a real bounded reader."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.database = self.root / "jarvis.db"
        self.settings = Settings(memory_database=self.database)
        self.memory = SQLiteMemoryStore(self.database)
        self.memory.save_project(ProjectRecord(id="proj-a", name="Project A"))

        # The SAME kind of connector production wires up, bound to a REAL
        # temp root - never a stub - so the observation genuinely exercises
        # containment, the secret-file policy and the no-follow open chain.
        self.reader = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        # An unrelated capability for ``on_true``, so the observation
        # connector and the chain-execution connector can never be confused
        # for one another.
        self.worker = DummyConnector(name="worker")
        self.registry = ToolRegistry()
        self.registry.register(self.reader)
        self.registry.register(self.worker)
        self.core = open_core(
            registry=self.registry,
            memory=self.memory,
            settings=self.settings,
            destination_policy=frozenset(
                {DestinationGrant("worker", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION)}
            ),
        )
        self.at = NOW + timedelta(hours=1)
        self.after = self.at + timedelta(minutes=1)

    # -- fixtures -----------------------------------------------------------

    def write_file(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def spec(
        self,
        *,
        condition_kind: ConditionKind = ConditionKind.LOCAL_READ_SHA256_EQUALS,
        reference: str = "notes.txt",
        expected_sha256: str = DIGEST,
        on_true: tuple[ChainStep, ...] | None = None,
    ) -> JobSpec:
        return JobSpec(
            project_id="proj-a",
            kind=JobKind.CONDITION_WATCH,
            label="watch it",
            schedule=interval_schedule(1),
            condition=LocalReadCondition(
                kind=condition_kind,
                reference=reference,
                expected_sha256=expected_sha256,
            ),
            on_true=on_true
            or (
                ChainStep(
                    objective="react to the change",
                    capability=DUMMY_CAPABILITY,
                    action="read",
                    target="resource-a",
                ),
            ),
        )

    def claimed(self, *, worker: str = WORKER_A, **spec_kwargs):
        record = self.memory.create_job(self.spec(**spec_kwargs), now=NOW)
        run = self.memory.materialize_due_job_run(
            record.id, record.revision, now=self.at
        )
        self.assertIsNotNone(run)
        lease = self.memory.claim_job_run(run.id, worker, now=self.at, lease=LEASE)
        return run, lease

    def reload(self, run_id: str):
        return self.memory.load_job_run(run_id)

    def raw(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def sessions(self) -> int:
        with contextlib.closing(self.raw()) as connection:
            return connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


# --------------------------------------------------------------------------
# FALSE and UNKNOWN - SKIPPED, no session, no Core
# --------------------------------------------------------------------------


class FalseAndUnknownTests(ConditionWatchTestCase):
    def test_false_settles_skipped_with_condition_result_and_no_core(self) -> None:
        # EQUALS against a digest that provably does not match the file's
        # real content: a conclusive, evidenced FALSE.
        self.write_file("notes.txt", b"the actual content")
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(b"something else entirely"),
        )
        outcome = evaluate_condition_watch(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.after
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual("condition_false", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.SKIPPED, stored.status)
        self.assertEqual("false", stored.condition_result)
        self.assertIsNone(stored.session_id)
        self.assertIsNone(stored.chain_id)
        self.assertTrue(stored.settled)
        self.assertIsNotNone(stored.settled_at)
        self.assertEqual(0, self.sessions())
        self.assertEqual(0, self.worker.execute_calls)

    def test_unknown_settles_skipped_with_condition_result_and_no_core(self) -> None:
        # The file simply does not exist yet: no observation, regardless of
        # which predicate kind was declared.
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_DIFFERS,
            reference="missing.txt",
        )
        outcome = evaluate_condition_watch(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.after
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual("condition_unknown", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.SKIPPED, stored.status)
        self.assertEqual("unknown", stored.condition_result)
        self.assertIsNone(stored.session_id)
        self.assertEqual(0, self.sessions())
        self.assertEqual(0, self.worker.execute_calls)

    def test_a_settled_generation_unblocks_the_next_one(self) -> None:
        # SKIPPED is a real settled status: the one-unsettled-generation rule
        # releases the job exactly as it does for COMPLETED or FAILED.
        self.write_file("notes.txt", b"content")
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(b"different content"),
        )
        evaluate_condition_watch(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.after
        )
        job = self.memory.load_job(run.job_id)
        self.assertIsNotNone(job.next_due_at_utc)


# --------------------------------------------------------------------------
# TRUE - exactly one bound execution, condition_result persisted
# --------------------------------------------------------------------------


class TrueBindingTests(ConditionWatchTestCase):
    def test_true_binds_and_runs_exactly_once_with_condition_result_true(
        self,
    ) -> None:
        content = b"the watched content"
        self.write_file("notes.txt", content)
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(content),
        )
        outcome = evaluate_condition_watch(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.after
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.COMPLETED, stored.status)
        self.assertEqual("true", stored.condition_result)
        self.assertIsNotNone(stored.session_id)
        self.assertIsNotNone(stored.chain_id)
        self.assertEqual(1, self.sessions())
        self.assertEqual(1, self.worker.execute_calls)

    def test_a_second_true_evaluation_cannot_bind_again(self) -> None:
        # The binding is spent exactly once, regardless of how many times the
        # predicate is later asked - the same CAS ``bind_job_run_dispatch``
        # enforces for an ordinary job.
        content = b"watched"
        self.write_file("notes.txt", content)
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(content),
        )
        evaluate_condition_watch(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.after
        )
        with self.assertRaises(JobError):
            self.memory.bind_condition_true_dispatch(
                run.id, WORKER_A, lease.fencing_epoch, now=self.after
            )
        self.assertEqual(1, self.sessions())

    def test_an_ordinary_job_cannot_bind_through_the_watch_path(self) -> None:
        record = self.memory.create_job(
            JobSpec(
                project_id="proj-a",
                kind=JobKind.RECURRING,
                label="ordinary",
                schedule=interval_schedule(1),
                steps=(
                    ChainStep(
                        objective="do it",
                        capability=DUMMY_CAPABILITY,
                        action="read",
                        target="resource-a",
                    ),
                ),
            ),
            now=NOW,
        )
        run = self.memory.materialize_due_job_run(
            record.id, record.revision, now=self.at
        )
        lease = self.memory.claim_job_run(run.id, WORKER_A, now=self.at, lease=LEASE)
        with self.assertRaisesRegex(JobError, "condition watch"):
            self.memory.bind_condition_true_dispatch(
                run.id, WORKER_A, lease.fencing_epoch, now=self.at
            )
        self.assertEqual(0, self.sessions())


# --------------------------------------------------------------------------
# Stale fencing cannot settle or bind
# --------------------------------------------------------------------------


class StaleFencingTests(ConditionWatchTestCase):
    def test_stale_fencing_cannot_settle_the_false_or_unknown_path(self) -> None:
        self.write_file("notes.txt", b"content")
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(b"not the content"),
        )
        with self.assertRaises(JobError):
            self.memory.settle_job_run(
                run.id,
                JobRunStatus.SKIPPED,
                WORKER_A,
                lease.fencing_epoch + 1,
                now=self.after,
                condition_result="false",
            )
        with self.assertRaises(JobError):
            self.memory.settle_job_run(
                run.id,
                JobRunStatus.SKIPPED,
                WORKER_B,
                lease.fencing_epoch,
                now=self.after,
                condition_result="false",
            )
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.CLAIMED, stored.status)
        self.assertFalse(stored.settled)

    def test_stale_fencing_cannot_bind_the_true_path(self) -> None:
        content = b"content"
        self.write_file("notes.txt", content)
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(content),
        )
        with self.assertRaises(JobError):
            self.memory.bind_condition_true_dispatch(
                run.id, WORKER_A, lease.fencing_epoch + 1, now=self.after
            )
        with self.assertRaises(JobError):
            self.memory.bind_condition_true_dispatch(
                run.id, WORKER_B, lease.fencing_epoch, now=self.after
            )
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.CLAIMED, stored.status)
        self.assertIsNone(stored.session_id)
        self.assertEqual(0, self.sessions())
        self.assertEqual(0, self.worker.execute_calls)

    def test_stale_fencing_via_the_orchestrator_refuses_cleanly(self) -> None:
        content = b"content"
        self.write_file("notes.txt", content)
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(content),
        )
        outcome = evaluate_condition_watch(
            self.core, run.id, WORKER_A, lease.fencing_epoch + 1, clock=lambda: self.after
        )
        self.assertEqual(DISPATCH_REFUSED, outcome.verdict)
        self.assertEqual(0, self.worker.execute_calls)
        self.assertEqual(0, self.sessions())
        self.assertEqual(JobRunStatus.CLAIMED, self.reload(run.id).status)


# --------------------------------------------------------------------------
# Restart before binding: an unbound CLAIMED lease may expire and
# re-evaluate safely.
# --------------------------------------------------------------------------


class RestartBeforeBindTests(ConditionWatchTestCase):
    def test_restart_before_bind_reevaluates_safely_to_false(self) -> None:
        # The "crash" is simulated the same way every other phase in this
        # suite simulates one: the first owner's lease is simply never acted
        # on and is left to expire, exactly as a dead process would leave it.
        self.write_file("notes.txt", b"content")
        run, dead_lease = self.claimed(
            worker=WORKER_A,
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(b"different"),
        )
        expired = self.at + LEASE + timedelta(minutes=1)
        fresh_lease = self.memory.claim_job_run(
            run.id, WORKER_B, now=expired, lease=LEASE
        )
        self.assertNotEqual(dead_lease.fencing_epoch, fresh_lease.fencing_epoch)
        outcome = evaluate_condition_watch(
            self.core,
            run.id,
            WORKER_B,
            fresh_lease.fencing_epoch,
            clock=lambda: expired + timedelta(seconds=1),
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.SKIPPED, stored.status)
        self.assertEqual("false", stored.condition_result)
        # The dead owner's OWN attempt to act is now refused by the same
        # fencing proof stale settlement always was.
        with self.assertRaises(JobError):
            self.memory.settle_job_run(
                run.id,
                JobRunStatus.SKIPPED,
                WORKER_A,
                dead_lease.fencing_epoch,
                now=expired,
                condition_result="false",
            )

    def test_restart_before_bind_reevaluates_safely_to_true(self) -> None:
        content = b"content"
        self.write_file("notes.txt", content)
        run, dead_lease = self.claimed(
            worker=WORKER_A,
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(content),
        )
        expired = self.at + LEASE + timedelta(minutes=1)
        fresh_lease = self.memory.claim_job_run(
            run.id, WORKER_B, now=expired, lease=LEASE
        )
        outcome = evaluate_condition_watch(
            self.core,
            run.id,
            WORKER_B,
            fresh_lease.fencing_epoch,
            clock=lambda: expired + timedelta(seconds=1),
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.COMPLETED, stored.status)
        self.assertEqual("true", stored.condition_result)
        self.assertEqual(1, self.sessions())
        self.assertEqual(1, self.worker.execute_calls)
        with self.assertRaises(JobError):
            self.memory.bind_condition_true_dispatch(
                run.id, WORKER_A, dead_lease.fencing_epoch, now=expired
            )


# --------------------------------------------------------------------------
# Restart after a TRUE bind: existing 2B recovery, no second session.
# --------------------------------------------------------------------------


class RestartAfterTrueBindTests(ConditionWatchTestCase):
    def test_restart_after_true_bind_uses_recovery_without_a_second_session(
        self,
    ) -> None:
        content = b"watched content"
        self.write_file("notes.txt", content)
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(content),
        )
        # THE EXACT CRASH WINDOW ``dispatch_job_run``'s own docstring names:
        # "RUNNING IS DURABLE BEFORE CORE IS ENTERED." The bind is committed
        # directly, exactly what evaluating TRUE would have produced, and
        # Core is never entered - simulating the process dying the instant
        # after the fenced transition lands.
        bound = self.memory.bind_condition_true_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.after
        )
        self.assertEqual(0, self.worker.execute_calls)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual("true", stored.condition_result)
        self.assertEqual(1, self.sessions())

        later = self.after + LEASE + timedelta(minutes=1)
        outcome = recover_job_run(
            self.core,
            run.id,
            WORKER_B,
            bound.fencing_epoch,
            lease=LEASE,
            actor_label=ACTOR,
            reason=WHY,
            clock=lambda: later,
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        recovered = self.reload(run.id)
        self.assertEqual(JobRunStatus.COMPLETED, recovered.status)
        # THE RECORD SURVIVES SETTLEMENT: the same true written at bind time,
        # never re-derived and never nulled out by the later settle.
        self.assertEqual("true", recovered.condition_result)
        self.assertEqual(bound.session_id, recovered.session_id)
        # EXACTLY ONE SESSION, ever - recovery resumed the one binding made,
        # it did not create a second.
        self.assertEqual(1, self.sessions())
        self.assertEqual(1, self.worker.execute_calls)

    def test_a_dead_watch_owner_is_still_blocked_not_retried(self) -> None:
        # Before recovery, the bound-but-crashed generation is exactly as
        # blocked as an ordinary crashed dispatch - nobody may claim it,
        # settle it or bind it again out from under the live coordination
        # record.
        content = b"x"
        self.write_file("notes.txt", content)
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(content),
        )
        self.memory.bind_condition_true_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.after
        )
        with self.assertRaises(JobError):
            self.memory.claim_job_run(
                run.id, WORKER_B, now=self.after, lease=LEASE
            )
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)


# --------------------------------------------------------------------------
# Cancellation before evaluation/bind prevents execution.
# --------------------------------------------------------------------------


class CancellationTests(ConditionWatchTestCase):
    def test_cancellation_before_evaluation_prevents_a_true_execution(self) -> None:
        # The condition is GENUINELY true - the file really does match - so
        # only the cancellation flag stands between this and a bound run.
        content = b"it really matches"
        self.write_file("notes.txt", content)
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(content),
        )
        self.memory.request_job_run_cancel(
            run.id, now=self.after, actor_label=ACTOR, reason=WHY
        )
        outcome = evaluate_condition_watch(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.after
        )
        self.assertEqual(DISPATCH_REFUSED, outcome.verdict)
        self.assertIn("not_bindable", outcome.reason)
        stored = self.reload(run.id)
        # Flagged and still unbound - the intent is durable, nothing executed.
        self.assertTrue(stored.cancel_requested)
        self.assertIsNone(stored.session_id)
        self.assertFalse(stored.settled)
        self.assertEqual(0, self.sessions())
        self.assertEqual(0, self.worker.execute_calls)

    def test_cancellation_before_evaluation_still_allows_the_honest_false(
        self,
    ) -> None:
        # A generation that would have evaluated FALSE anyway settles exactly
        # as it would without the flag: nothing ran, which is what the
        # cancellation wanted too.
        self.write_file("notes.txt", b"content")
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(b"not the content"),
        )
        self.memory.request_job_run_cancel(
            run.id, now=self.after, actor_label=ACTOR, reason=WHY
        )
        outcome = evaluate_condition_watch(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.after
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.SKIPPED, stored.status)
        self.assertEqual("false", stored.condition_result)
        self.assertTrue(stored.cancel_requested)
        self.assertEqual(0, self.sessions())


# --------------------------------------------------------------------------
# The observation itself: hardened, never a bare read.
# --------------------------------------------------------------------------


class HardenedObservationTests(ConditionWatchTestCase):
    def test_a_reference_escaping_the_root_is_never_read(self) -> None:
        # Outside the connector's own allowed roots entirely, so the
        # containment check refuses it before a byte is read - even though a
        # file with matching content genuinely exists at that path.
        outside = self.temp.name  # the tempdir's own parent-adjacent sibling
        escaping = Path(outside).parent / "escaped-notes.txt"
        content = b"secret outside the root"
        try:
            escaping.write_bytes(content)
            run, lease = self.claimed(
                condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
                reference="../" + escaping.name,
                expected_sha256=sha256_of(content),
            )
            outcome = evaluate_condition_watch(
                self.core,
                run.id,
                WORKER_A,
                lease.fencing_epoch,
                clock=lambda: self.after,
            )
            self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
            stored = self.reload(run.id)
            # UNKNOWN, never TRUE: containment refused the read outright, and
            # a refusal is never allowed to read as a confirmed match.
            self.assertEqual("unknown", stored.condition_result)
            self.assertEqual(0, self.worker.execute_calls)
            self.assertEqual(0, self.sessions())
        finally:
            escaping.unlink(missing_ok=True)

    def test_a_successful_read_of_a_differently_spelled_reference_is_unknown(
        self,
    ) -> None:
        # The read genuinely SUCCEEDS - the file exists, is in root, and its
        # content really does match - but the identity the connector reports
        # back is not byte-identical to the declared reference, because a
        # leading ``./`` is not part of the root-relative spelling the
        # connector's own containment normalizes to. This is the identity
        # check earning its keep on a SUCCESSFUL read, not merely on a
        # refused one: evidence about a file reached by a different spelling
        # is still never treated as evidence about the declared reference.
        content = b"content"
        self.write_file("notes.txt", content)
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            reference="./notes.txt",
            expected_sha256=sha256_of(content),
        )
        outcome = evaluate_condition_watch(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.after
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual("unknown", stored.condition_result)
        self.assertEqual(0, self.sessions())

    def test_no_registered_reader_yields_unknown_not_a_crash(self) -> None:
        registry = ToolRegistry()
        registry.register(self.worker)
        core = open_core(
            registry=registry,
            memory=self.memory,
            settings=self.settings,
            destination_policy=frozenset(
                {DestinationGrant("worker", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION)}
            ),
        )
        self.write_file("notes.txt", b"content")
        run, lease = self.claimed(
            condition_kind=ConditionKind.LOCAL_READ_SHA256_EQUALS,
            expected_sha256=sha256_of(b"content"),
        )
        outcome = evaluate_condition_watch(
            core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.after
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual("unknown", self.reload(run.id).condition_result)

    def test_the_module_never_opens_a_file_itself(self) -> None:
        # The module's own code - docstrings stripped, so its prose about
        # what it does not do cannot satisfy the check in place of the code
        # actually not doing it - names no raw ``open`` call anywhere.
        source = (
            Path(__file__).resolve().parent.parent
            / "jarvis"
            / "condition_watch.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
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
        code = ast.unparse(tree)
        calls = {
            node.func.id
            for node in ast.walk(ast.parse(code))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("open", calls)
        for forbidden in ("os.open", "io.open", ".read_text(", ".read_bytes("):
            self.assertNotIn(forbidden, code, forbidden)


if __name__ == "__main__":
    unittest.main()
