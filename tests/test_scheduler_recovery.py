"""Scheduler phase 2B2a: same-session recovery of an ALREADY-BOUND generation.

Covers the fenced recovery acquisition and its three load-bearing corrections -
the observed fencing epoch as an explicit CAS input, the approval-resolution
crash window, and the audit record that commits inside the mutation's own
transaction - plus the orchestration that re-proves everything before handing
the SAME session back to Core.

THE CRASH SHAPES ARE REAL. Nothing here plants an attempt state by hand where a
worker can be made to die for real instead: the worker is killed at the exact
storage write that produces each shape, through a real ``AgentCore``, real
connectors and a real schema-10 store, so PREPARED, IN_FLIGHT, FINISHED and the
parked approval are the states the machinery actually leaves behind. What that
buys is the only assertion that matters for a recovery suite - the number of
times a connector was reached, counted across the crash AND the resume.

THE PHASE BOUNDARY IS ITSELF UNDER TEST. Recovery takes back a coordination
lease and nothing else. It creates no session, replaces no chain, mints and
consumes no grant, answers no approval, mutates no execution attempt, resolves
no unknown outcome and hands back no retry permission. An attempt still in
doubt is where it STOPS: a dead owner's in-flight attempt stays exactly as it
is, unrecovered and unresolved, which is the accepted phase-2B2b blocker rather
than a gap this phase may close.

Every instant is EXPLICIT and every clock is injected. Nothing sleeps, nothing
polls, and no test performs a live model, auth or network call.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import json
import sqlite3
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from jarvis.contracts import (
    AgentRun,
    AttemptState,
    CLAIMABLE_JOB_RUN_STATUSES,
    ChainStatus,
    JobError,
    JobRunLease,
    JobRunStatus,
    LoopStage,
    PermissionGrant,
    REACQUIRABLE_JOB_RUN_STATUSES,
    RunStatus,
    SessionState,
    TaskStatus,
    utc_now,
)
from jarvis.dispatch import (
    DISPATCH_BLOCKED,
    DISPATCH_PARKED,
    DISPATCH_REFUSED,
    DISPATCH_SETTLED,
    _bound_state,
    dispatch_job_run,
    recover_job_run,
)
from jarvis.memory import SQLiteMemoryStore

from tests.test_scheduler_dispatch import (  # noqa: F401
    LEASE,
    WORKER_A,
    WORKER_B,
    DispatchTestCase,
    ScriptedClock,
    ScriptedCore,
    setUpModule,
    tearDownModule,
)
from tests.test_storage_abstraction import InMemoryStore

WORKER_C = "worker-c"
ACTOR = "operator:dora"
WHY = "the worker host was rebooted"


class WorkerDied(RuntimeError):
    """A process that stopped where a real one can stop, mid-write."""


class RecoveryTestCase(DispatchTestCase):
    """A real bound generation whose worker is gone, and the clocks to prove it."""

    def setUp(self) -> None:
        super().setUp()
        # ``at`` claims and binds; the lease runs five minutes from there.
        self.expiry = self.at + LEASE
        self.just_live = self.expiry - timedelta(microseconds=1)
        self.after = self.at + timedelta(minutes=10)
        self._alive: dict[str, object] = {}

    # -- fixtures ---------------------------------------------------------

    def bound(self, *, steps: int = 1, worker: str = WORKER_A):
        """One generation durably RUNNING and bound, with nothing run yet.

        This is the crash-before-anything shape, produced the way it really
        occurs: storage commits the binding and the RUNNING transition BEFORE
        Core is entered, so a process that dies there leaves exactly this.
        """
        run, lease = self.claimed(steps=steps, worker=worker)
        dispatch = self.memory.bind_job_run_dispatch(
            run.id, worker, lease.fencing_epoch, now=self.at
        )
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(lease.fencing_epoch, stored.fencing_epoch)
        return stored, dispatch

    def kill_at(self, method: str) -> None:
        """Let the real machinery run until it makes this write, then die."""
        self._alive.setdefault(method, getattr(self.memory, method))
        self.addCleanup(self.revive, method)

        def died(*_args, **_kwargs):
            raise WorkerDied(method)

        setattr(self.memory, method, died)

    def revive(self, method: str) -> None:
        """Put the killed write back, so the recovering worker is a real one."""
        original = self._alive.pop(method, None)
        if original is not None:
            setattr(self.memory, method, original)

    def crashed_run(self, method: str, *, steps: int = 1):
        """A generation whose worker really died at ``method``.

        The dispatch does its own binding, exactly as a first run does, and the
        death happens inside Core - so the generation is left durably RUNNING
        and bound by the real path rather than by a fixture.
        """
        run, lease = self.claimed(steps=steps)
        self.kill_at(method)
        outcome = dispatch_job_run(
            self.core,
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.at + timedelta(minutes=1),
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict, outcome.reason)
        self.assertEqual("run_chain_raised", outcome.reason)
        self.revive(method)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertIsNotNone(stored.session_id)
        return stored, stored

    def reacquire(self, run_id, worker, epoch, *, now=None, lease=LEASE, **over):
        fields = dict(actor_label=ACTOR, reason=WHY)
        fields.update(over)
        return self.memory.reacquire_job_run(
            run_id, worker, epoch, now=now or self.after, lease=lease, **fields
        )

    def recover(self, run_id, worker, epoch, *, core=None, clock=None, **over):
        fields = dict(actor_label=ACTOR, reason=WHY)
        fields.update(over)
        return recover_job_run(
            core or self.core,
            run_id,
            worker,
            epoch,
            clock=clock or (lambda: self.after),
            **fields,
        )

    # -- observations -----------------------------------------------------

    def attempt_states(self) -> list[str]:
        with contextlib.closing(self.raw()) as connection:
            return [
                row["state"]
                for row in connection.execute(
                    "SELECT state FROM execution_attempts ORDER BY rowid"
                )
            ]

    def attempt_rows(self) -> list[sqlite3.Row]:
        with contextlib.closing(self.raw()) as connection:
            return list(
                connection.execute(
                    "SELECT id, task_id, session_id, connector, fingerprint, "
                    "state FROM execution_attempts ORDER BY rowid"
                )
            )

    def events(self, session_id: str | None = None) -> list[dict]:
        with contextlib.closing(self.raw()) as connection:
            rows = list(
                connection.execute(
                    "SELECT session_id, task_id, event_type, payload_json, "
                    "created_at FROM event_log WHERE event_type = "
                    "'job_run_reacquired' ORDER BY id"
                )
            )
        return [
            {
                "session_id": row["session_id"],
                "task_id": row["task_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "payload_json": row["payload_json"],
                "created_at": row["created_at"],
            }
            for row in rows
            if session_id is None or row["session_id"] == session_id
        ]

    def chain_of(self, session_id: str):
        session = self.memory.load_session(session_id)
        self.assertIsNotNone(session)
        return session.chain

    def parked(self):
        """A REAL park: the egress step really asked for a person."""
        run, lease = self.claimed(steps=2)
        outcome = dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.at
        )
        self.assertEqual(DISPATCH_PARKED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, stored.status)
        self.assertIsNone(stored.owner_token)
        chain = self.chain_of(stored.session_id)
        task_id = chain.step_task_ids[chain.cursor]
        request = self.memory.load_pending_approval(task_id, stored.session_id)
        self.assertIsNotNone(request)
        # The egress connector has NOT been reached; that is what is waiting.
        self.assertEqual(0, len(self.planner.calls))
        self.assertEqual(1, len(self.reader.calls))
        return stored, stored, task_id, request


# --------------------------------------------------------------------------
# F1 - THE FENCED RECOVERY ACQUISITION
# --------------------------------------------------------------------------


class RecoveryAcquisitionTests(RecoveryTestCase):
    """Who may take a bound generation back, and on exactly what proof."""

    def test_an_expired_running_generation_is_recovered_at_the_next_epoch(
        self,
    ) -> None:
        run, dispatch = self.bound()
        before = self.sessions()
        lease = self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        self.assertIsInstance(lease, JobRunLease)
        self.assertEqual(run.fencing_epoch + 1, lease.fencing_epoch)
        self.assertEqual(WORKER_B, lease.owner_token)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(WORKER_B, stored.owner_token)
        self.assertEqual(run.fencing_epoch + 1, stored.fencing_epoch)
        self.assertEqual((self.after + LEASE).isoformat(), stored.lease_expires_at_utc)
        # THE BINDING IS BYTE-IDENTICAL. Not merely present: the same session
        # and the same chain, and no second session anywhere in the database.
        self.assertEqual(dispatch.session_id, stored.session_id)
        self.assertEqual(dispatch.chain_id, stored.chain_id)
        self.assertEqual(before, self.sessions())
        self.assertEqual(1, self.sessions())

    def test_the_claim_instant_becomes_this_acquisition(self) -> None:
        # ``claimed_at`` means the most recent successful ownership
        # acquisition, and a recovery is one. The previous instant survives in
        # the audit record rather than in the row.
        run, _dispatch = self.bound()
        self.assertEqual(self.at.isoformat(), run.claimed_at)
        self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(self.after.isoformat(), self.reload(run.id).claimed_at)
        self.assertEqual(
            self.at.isoformat(), self.events()[0]["payload"]["previous_claimed_at"]
        )

    def test_a_renewal_still_moves_neither_the_epoch_nor_the_claim_instant(
        self,
    ) -> None:
        run, _dispatch = self.bound()
        lease = self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        renewed = self.memory.renew_job_run_lease(
            run.id,
            WORKER_B,
            lease.fencing_epoch,
            now=self.after + timedelta(minutes=1),
            lease=LEASE,
        )
        self.assertEqual(lease.fencing_epoch, renewed.fencing_epoch)
        stored = self.reload(run.id)
        self.assertEqual(self.after.isoformat(), stored.claimed_at)
        self.assertEqual(lease.fencing_epoch, stored.fencing_epoch)

    def test_the_instant_of_expiry_is_already_expired(self) -> None:
        # The house's one half-open definition, asked at the exact boundary.
        run, _dispatch = self.bound()
        lease = self.reacquire(run.id, WORKER_B, run.fencing_epoch, now=self.expiry)
        self.assertEqual(run.fencing_epoch + 1, lease.fencing_epoch)

    def test_a_lease_one_microsecond_from_expiry_is_still_live(self) -> None:
        run, _dispatch = self.bound()
        with self.assertRaisesRegex(JobError, "live owner"):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch, now=self.just_live)
        stored = self.reload(run.id)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(run.fencing_epoch, stored.fencing_epoch)
        self.assertEqual([], self.events())

    def test_a_stale_observer_cannot_recover_after_a_later_owner_expired(
        self,
    ) -> None:
        # F1, THE RACE ITSELF. A observes epoch N. B recovers to N+1 and then
        # lets its own lease expire. A's lease check would now PASS - the lease
        # in front of it really is expired - and only the observed epoch
        # refuses it. This is why an expiry can never be the fence.
        run, _dispatch = self.bound()
        observed_by_a = run.fencing_epoch
        b = self.reacquire(run.id, WORKER_B, observed_by_a)
        self.assertEqual(observed_by_a + 1, b.fencing_epoch)
        much_later = self.after + timedelta(minutes=10)
        # B's lease really is expired by now, so nothing but the epoch is left.
        self.assertFalse(self.reload(run.id).lease_is_live(much_later))
        with self.assertRaisesRegex(JobError, "not recoverable"):
            self.reacquire(run.id, WORKER_C, observed_by_a, now=much_later)
        stored = self.reload(run.id)
        self.assertEqual(WORKER_B, stored.owner_token)
        self.assertEqual(observed_by_a + 1, stored.fencing_epoch)
        # And exactly one recovery is on the record: B's.
        self.assertEqual(1, len(self.events()))

    def test_a_future_epoch_is_refused(self) -> None:
        run, _dispatch = self.bound()
        with self.assertRaisesRegex(JobError, "not recoverable"):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch + 1)
        self.assertEqual(WORKER_A, self.reload(run.id).owner_token)
        self.assertEqual([], self.events())

    def test_a_malformed_epoch_is_refused_exactly_as_elsewhere(self) -> None:
        run, _dispatch = self.bound()
        for bad in (True, False, "1", 1.0, None):
            with self.subTest(epoch=bad):
                with self.assertRaisesRegex(JobError, "exact integer"):
                    self.reacquire(run.id, WORKER_B, bad)
        self.assertEqual([], self.events())

    def test_a_malformed_owner_token_is_refused(self) -> None:
        run, _dispatch = self.bound()
        for bad in ("", "   ", None):
            with self.subTest(token=bad):
                with self.assertRaises(JobError):
                    self.reacquire(run.id, bad, run.fencing_epoch)

    def test_an_unbound_generation_is_not_recoverable(self) -> None:
        # The generic claim path's business, and refused here even though a
        # CLAIMED generation is unsettled and its lease has expired.
        run, _lease = self.claimed()
        with self.assertRaises(JobError):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(JobRunStatus.CLAIMED, self.reload(run.id).status)

    def test_a_running_generation_with_no_binding_is_refused(self) -> None:
        # Unreachable through the API - binding and RUNNING are one
        # transaction - so it is planted, because "unreachable" is exactly the
        # state a fail-closed guard exists for.
        run, _dispatch = self.bound()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE job_runs SET session_id = NULL, chain_id = NULL WHERE id = ?",
                (run.id,),
            )
            connection.commit()
        with self.assertRaisesRegex(JobError, "not bound"):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual([], self.events())

    def test_every_other_source_status_is_refused(self) -> None:
        # UNKNOWN_OUTCOME is the one that matters most: recovering it would be
        # a timer deciding what only evidence can decide.
        for status in JobRunStatus:
            if status in REACQUIRABLE_JOB_RUN_STATUSES:
                continue
            with self.subTest(status=status):
                run, _dispatch = self.bound()
                with contextlib.closing(self.raw()) as connection:
                    connection.execute(
                        "UPDATE job_runs SET status = ?, settled_at = ? WHERE id = ?",
                        (
                            status.value,
                            utc_now() if status not in {
                                JobRunStatus.PENDING,
                                JobRunStatus.CLAIMED,
                                JobRunStatus.UNKNOWN_OUTCOME,
                            } else None,
                            run.id,
                        ),
                    )
                    connection.commit()
                with self.assertRaises(JobError):
                    self.reacquire(run.id, WORKER_B, run.fencing_epoch)
                self.assertEqual([], self.events())

    def test_the_generic_claim_path_still_cannot_reach_a_bound_generation(
        self,
    ) -> None:
        # The whole reason recovery is a separate boundary. Nothing about this
        # phase widened the claim path, and its own set says so.
        run, _dispatch = self.bound()
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.memory.claim_job_run(run.id, WORKER_B, now=self.after, lease=LEASE)
        self.assertEqual(
            {JobRunStatus.PENDING, JobRunStatus.CLAIMED}, CLAIMABLE_JOB_RUN_STATUSES
        )
        self.assertNotIn(JobRunStatus.RUNNING, CLAIMABLE_JOB_RUN_STATUSES)
        self.assertNotIn(JobRunStatus.AWAITING_APPROVAL, CLAIMABLE_JOB_RUN_STATUSES)

    def test_the_displaced_owner_can_no_longer_move_the_generation(self) -> None:
        # THE ZOMBIE. A's Core call may still be running and may still write
        # H2 truth - that is Core's business and stays untouched - but A can no
        # longer state this generation's lifecycle, because its proof names an
        # epoch that has been superseded.
        run, _dispatch = self.bound()
        self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        for label, call in (
            (
                "settle",
                lambda: self.memory.settle_job_run(
                    run.id,
                    JobRunStatus.COMPLETED,
                    WORKER_A,
                    run.fencing_epoch,
                    now=self.after,
                ),
            ),
            (
                "park",
                lambda: self.memory.await_job_run_approval(
                    run.id, WORKER_A, run.fencing_epoch, now=self.after
                ),
            ),
            (
                "unknown",
                lambda: self.memory.mark_job_run_unknown(
                    run.id, WORKER_A, run.fencing_epoch, now=self.after
                ),
            ),
            (
                "renew",
                lambda: self.memory.renew_job_run_lease(
                    run.id, WORKER_A, run.fencing_epoch, now=self.after, lease=LEASE
                ),
            ),
            (
                "recover again",
                lambda: self.reacquire(run.id, WORKER_A, run.fencing_epoch),
            ),
        ):
            with self.subTest(call=label):
                with self.assertRaises(JobError):
                    call()
        stored = self.reload(run.id)
        self.assertEqual(WORKER_B, stored.owner_token)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)

    def test_two_concurrent_recoverers_produce_exactly_one_winner(self) -> None:
        run, _dispatch = self.bound()
        results: list[object] = []
        barrier = threading.Barrier(2)

        def take(worker: str) -> None:
            store = SQLiteMemoryStore(self.database)
            barrier.wait()
            try:
                results.append(
                    store.reacquire_job_run(
                        run.id,
                        worker,
                        run.fencing_epoch,
                        now=self.after,
                        lease=LEASE,
                        actor_label=ACTOR,
                        reason=WHY,
                    )
                )
            except JobError as exc:
                results.append(exc)

        threads = [
            threading.Thread(target=take, args=(worker,))
            for worker in (WORKER_B, WORKER_C)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [r for r in results if isinstance(r, JobRunLease)]
        self.assertEqual(1, len(winners))
        self.assertEqual(1, len([r for r in results if isinstance(r, JobError)]))
        stored = self.reload(run.id)
        self.assertEqual(winners[0].owner_token, stored.owner_token)
        self.assertEqual(run.fencing_epoch + 1, stored.fencing_epoch)
        # ONE mutation, ONE record. A loser writes neither.
        self.assertEqual(1, len(self.events()))

    def test_recovery_survives_reopening_the_database(self) -> None:
        run, dispatch = self.bound()
        lease = self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        reopened = SQLiteMemoryStore(self.database)
        stored = reopened.load_job_run(run.id)
        self.assertEqual(WORKER_B, stored.owner_token)
        self.assertEqual(lease.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(dispatch.session_id, stored.session_id)
        self.assertEqual(dispatch.chain_id, stored.chain_id)
        events = [
            event
            for event in reopened.list_events(dispatch.session_id)
            if event["event_type"] == "job_run_reacquired"
        ]
        self.assertEqual(1, len(events))


# --------------------------------------------------------------------------
# F2 - THE APPROVAL RESOLUTION CRASH WINDOW
# --------------------------------------------------------------------------


class RecoveryApprovalTests(RecoveryTestCase):
    """A parked generation, and the exact window Core leaves behind."""

    def resolve(self, request, *, approved=True, expires_at=None):
        """Commit exactly the transaction Core commits, and then stop.

        This IS the crash window. ``resolve_approval`` is one transaction and
        the task checkpoint that follows it is another, so a process that dies
        between them leaves the request resolved, the grant durable, and the
        task still saying AWAITING_APPROVAL. Nothing is faked here: it is the
        real storage call with the real grant Core would have built.
        """
        grant = PermissionGrant.for_request(request, approved=approved)
        if expires_at is not None:
            grant = replace(grant, expires_at=expires_at)
        self.assertTrue(self.memory.resolve_approval(request, grant, "approved"))
        return grant

    def test_a_still_pending_approval_refuses_recovery(self) -> None:
        run, _dispatch, task_id, _request = self.parked()
        with self.assertRaisesRegex(JobError, "pending"):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, stored.status)
        self.assertEqual([], self.events())
        # The person's question is untouched: still pending, still theirs.
        self.assertIsNotNone(
            self.memory.load_pending_approval(task_id, stored.session_id)
        )

    def test_the_resolved_crash_window_is_recoverable(self) -> None:
        # THE F2 SHAPE. Resolution committed, the worker died before the task
        # checkpoint, so the task STILL says AWAITING_APPROVAL. That is not
        # malformed, and recovery must not require the task to have moved.
        run, dispatch, task_id, request = self.parked()
        self.resolve(request)
        self.assertIs(
            TaskStatus.AWAITING_APPROVAL, self.memory.load_task(task_id).status
        )
        self.assertIsNone(
            self.memory.load_pending_approval(task_id, dispatch.session_id)
        )
        lease = self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        stored = self.reload(run.id)
        # Coordination returns to RUNNING; the binding is untouched.
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(run.fencing_epoch + 1, lease.fencing_epoch)
        self.assertEqual(dispatch.session_id, stored.session_id)
        self.assertEqual(dispatch.chain_id, stored.chain_id)
        self.assertEqual(1, self.sessions())
        # The audit names the task the wait was proved against.
        self.assertEqual(task_id, self.events()[0]["payload"]["cursor_task_id"])
        self.assertEqual(
            "awaiting_approval", self.events()[0]["payload"]["previous_status"]
        )

    def test_a_recovered_approval_re_enters_the_same_session_and_re_prompts(
        self,
    ) -> None:
        # WHAT ACTUALLY HAPPENS, and why it is safe. The grant really is
        # valid - the assertion below proves storage would hand it back - but
        # the base permission gate is satisfied only by a grant PASSED INTO the
        # resume that consumes it, and a plain ``run_chain`` re-entry carries
        # none. So Core asks again, on the SAME session, and the ordinary 2B1
        # reflection parks the generation on the new request.
        #
        # That is the accepted outcome rather than a shortfall: being asked
        # twice costs a person one click, and the alternative - teaching the
        # scheduler to hand Core a grant it did not receive from a human -
        # would be exactly the approval authority this phase must not have.
        # Nothing here changes Core to make the reuse seamless.
        run, dispatch, task_id, request = self.parked()
        self.resolve(request)
        self.assertTrue(
            self.memory.has_approved_grant(
                dispatch.session_id, task_id, request.fingerprint
            ),
            "the grant is valid; the re-prompt is Core's decision, not a defect",
        )
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_PARKED, outcome.verdict, outcome.reason)
        self.assertEqual(dispatch.session_id, outcome.session_id)
        # THE SAME SESSION, never a second one.
        self.assertEqual(1, self.sessions())
        self.assertEqual(dispatch.session_id, self.reload(run.id).session_id)
        # ZERO EGRESS REPLAY, which is the whole safety claim of this path.
        self.assertEqual(0, len(self.planner.calls))
        self.assertEqual(1, len(self.reader.calls))
        # And it is parked on the NEW question, proved by the 2B1 exact-request
        # rule rather than by anything this phase added.
        reissued = self.memory.load_pending_approval(task_id, dispatch.session_id)
        self.assertIsNotNone(reissued)
        self.assertNotEqual(request.id, reissued.id)
        self.assertEqual(
            JobRunStatus.AWAITING_APPROVAL, self.reload(run.id).status
        )

    def test_a_recovered_generation_can_be_answered_and_finished(self) -> None:
        # THE ROUND TRIP. The re-prompt above is answered the way any approval
        # is - through Core's own resume, with a grant a person produced - and
        # the generation finishes. The egress connector is reached EXACTLY ONCE
        # in the whole life of this occurrence, across a park, a recovery, a
        # second park and the answer.
        run, dispatch, task_id, request = self.parked()
        self.resolve(request)
        self.recover(run.id, WORKER_B, run.fencing_epoch)
        second = self.memory.load_pending_approval(task_id, dispatch.session_id)
        self.assertIsNotNone(second)
        # Core's own approval path, unchanged and unassisted by the scheduler.
        answered = self.core.resume(
            dispatch.session_id, task_id, PermissionGrant.for_request(second)
        )
        self.assertIsNot(RunStatus.AWAITING_APPROVAL, answered.status)
        self.assertEqual(1, len(self.planner.calls))
        self.assertIs(TaskStatus.COMPLETED, self.memory.load_task(task_id).status)
        self.assertEqual(1, self.sessions())

    def test_an_expired_grant_is_re_gated_by_core_and_parks_again(self) -> None:
        # The scheduler read no grant and formed no opinion. Core found the
        # recorded grant's window closed, asked a person again, and the
        # ordinary 2B1 approval reflection parked the generation on the NEW
        # request. Being asked twice is a correct outcome.
        run, dispatch, task_id, request = self.parked()
        self.resolve(request, expires_at="2020-01-01T00:00:00+00:00")
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_PARKED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, stored.status)
        reissued = self.memory.load_pending_approval(task_id, dispatch.session_id)
        self.assertIsNotNone(reissued)
        self.assertNotEqual(request.id, reissued.id)
        # ZERO EGRESS. An expired grant authorized nothing.
        self.assertEqual(0, len(self.planner.calls))

    def test_a_rejected_approval_follows_the_canonical_cancellation(self) -> None:
        run, dispatch, task_id, request = self.parked()
        denial = PermissionGrant.for_request(request, approved=False)
        session = self.memory.load_session(dispatch.session_id)
        task = self.memory.load_task(task_id)
        # Exactly what Core writes on a denial: the terminal task and the
        # resolution are ONE transaction, so the task is finalized by the same
        # call rather than by a later one.
        task.status = TaskStatus.REJECTED
        task.stage = LoopStage.FAILED
        self.assertTrue(
            self.memory.reject_task(request, denial, session, task, {"code": "denied"})
        )
        self.assertIs(TaskStatus.REJECTED, self.memory.load_task(task_id).status)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.CANCELLED, outcome.run.status)
        self.assertIs(ChainStatus.CANCELLED, self.chain_of(dispatch.session_id).status)
        self.assertEqual(0, len(self.planner.calls))

    def test_absence_of_a_pending_request_is_never_read_as_approval(self) -> None:
        # The request is CLOSED without any grant at all - neither approved nor
        # rejected. Recovery of the coordination lease still proceeds, because
        # it is only a lease; and Core, asked the actual question, refuses to
        # execute and gates again. If the scheduler had read "no pending
        # request" as "approved", the egress connector would have run here.
        run, dispatch, task_id, request = self.parked()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE pending_approvals SET status = 'expired', resolved_at = ? "
                "WHERE id = ?",
                (utc_now(), request.id),
            )
            connection.commit()
        self.assertEqual(0, self.table_count("permission_grants"))
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_PARKED, outcome.verdict, outcome.reason)
        self.assertEqual(0, len(self.planner.calls))
        self.assertEqual(
            JobRunStatus.AWAITING_APPROVAL, self.reload(run.id).status
        )

    def test_a_foreign_cursor_task_fails_closed(self) -> None:
        # The chain's current step names a task that belongs to another
        # session. The wait rule cannot be asked honestly, so nothing moves.
        run, dispatch, task_id, _request = self.parked()
        other = SessionState(objective="somebody else's work")
        self.memory.save_session(other)
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?", (other.id, task_id)
            )
            connection.commit()
        with self.assertRaisesRegex(JobError, "not this session's task"):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(
            JobRunStatus.AWAITING_APPROVAL, self.reload(run.id).status
        )
        self.assertEqual([], self.events())

    def test_an_unreadable_chain_cursor_fails_closed(self) -> None:
        run, dispatch, _task_id, _request = self.parked()
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (dispatch.session_id,)
            ).fetchone()
            payload = json.loads(row["state_json"])
            payload["chain"]["step_task_ids"] = ["", ""]
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(payload), dispatch.session_id),
            )
            connection.commit()
        with self.assertRaises(JobError):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual([], self.events())

    def test_the_recovery_boundary_reads_no_grant_authority(self) -> None:
        # SOURCE GUARD. The one approval question the store may ask is whether
        # a request is still pending. Grants, resolutions and rejections are
        # Core's alone, and this proves the recovery path does not name them.
        source = inspect.getsource(SQLiteMemoryStore.reacquire_job_run)
        for forbidden in (
            "permission_grants",
            "resolve_approval",
            "reject_task",
            "has_approved_grant",
            "execution_attempts",
            "mark_execution_unknown",
            "conclude_execution",
            "INSERT INTO sessions",
            "INSERT INTO tasks",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        # It never writes either binding column, in any statement.
        for assignment in ("session_id =", "chain_id ="):
            self.assertNotIn(f"SET {assignment}", source)


# --------------------------------------------------------------------------
# F3 - THE TRANSACTIONAL AUDIT RECORD
# --------------------------------------------------------------------------


class RecoveryAuditTests(RecoveryTestCase):
    """The record and the mutation are one write, or there is neither."""

    def test_one_success_writes_exactly_one_complete_event(self) -> None:
        run, dispatch = self.bound()
        lease = self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        events = self.events()
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual(dispatch.session_id, event["session_id"])
        self.assertIsNone(event["task_id"])
        self.assertEqual(self.after.isoformat(), event["created_at"])
        payload = event["payload"]
        self.assertEqual(
            {
                "run_id": run.id,
                "job_id": run.job_id,
                "generation": run.generation,
                "chain_id": dispatch.chain_id,
                "previous_status": "running",
                "resulting_status": "running",
                "previous_fencing_epoch": run.fencing_epoch,
                "fencing_epoch": lease.fencing_epoch,
                "previous_claimed_at": self.at.isoformat(),
                "claimed_at": self.after.isoformat(),
                "lease_expires_at_utc": (self.after + LEASE).isoformat(),
                "previous_owner_present": True,
                "actor_label": ACTOR,
                "actor_authenticated": False,
                "reason": WHY,
                "cursor_task_id": None,
                "recovered_at": self.after.isoformat(),
            },
            payload,
        )

    def test_no_owner_token_ever_reaches_the_record(self) -> None:
        run, _dispatch = self.bound()
        self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        raw = self.events()[0]["payload_json"]
        self.assertNotIn(WORKER_A, raw)
        self.assertNotIn(WORKER_B, raw)
        # Only the operational fact survives.
        self.assertTrue(self.events()[0]["payload"]["previous_owner_present"])

    def test_a_generation_whose_owner_had_already_gone_says_so(self) -> None:
        # A parked generation carries no lease at all, so there was nobody to
        # displace - a materially different fact from taking one over.
        run, lease = self.claimed(steps=2)
        dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.at
        )
        parked = self.reload(run.id)
        self.assertIsNone(parked.owner_token)
        chain = self.chain_of(parked.session_id)
        request = self.memory.load_pending_approval(
            chain.step_task_ids[chain.cursor], parked.session_id
        )
        self.memory.resolve_approval(
            request, PermissionGrant.for_request(request), "approved"
        )
        self.reacquire(run.id, WORKER_B, parked.fencing_epoch)
        self.assertFalse(self.events()[0]["payload"]["previous_owner_present"])

    def test_a_failing_audit_insert_rolls_the_recovery_back(self) -> None:
        # F3, THE POINT OF IT. If the record cannot be written the ownership
        # move must not survive, because an authority change nobody can
        # reconstruct is worse than a refused one.
        run, _dispatch = self.bound()
        # The descriptor itself, so the restore puts back a staticmethod rather
        # than a plain function - otherwise every later test in the process
        # would be running against a subtly different store.
        original = SQLiteMemoryStore.__dict__["_insert_job_run_event"]

        def refuse(*_args, **_kwargs):
            raise sqlite3.IntegrityError("the audit row could not be written")

        SQLiteMemoryStore._insert_job_run_event = staticmethod(refuse)
        self.addCleanup(
            setattr, SQLiteMemoryStore, "_insert_job_run_event", original
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        stored = self.reload(run.id)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(run.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(self.at.isoformat(), stored.claimed_at)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual([], self.events())
        # And the generation is still genuinely recoverable afterwards.
        SQLiteMemoryStore._insert_job_run_event = original
        lease = self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(run.fencing_epoch + 1, lease.fencing_epoch)
        self.assertEqual(1, len(self.events()))

    def test_a_refused_recovery_writes_no_record_at_all(self) -> None:
        run, _dispatch = self.bound()
        for label, call in (
            ("live owner", lambda: self.reacquire(
                run.id, WORKER_B, run.fencing_epoch, now=self.just_live)),
            ("stale epoch", lambda: self.reacquire(
                run.id, WORKER_B, run.fencing_epoch - 1)),
            ("bad epoch", lambda: self.reacquire(run.id, WORKER_B, True)),
            ("blank actor", lambda: self.reacquire(
                run.id, WORKER_B, run.fencing_epoch, actor_label="  ")),
            ("blank reason", lambda: self.reacquire(
                run.id, WORKER_B, run.fencing_epoch, reason="")),
            ("unknown run", lambda: self.reacquire(
                "no-such-run", WORKER_B, run.fencing_epoch)),
        ):
            with self.subTest(refusal=label):
                with self.assertRaises(JobError):
                    call()
                self.assertEqual([], self.events())

    def test_recovery_metadata_is_bounded_and_redacted(self) -> None:
        # Persisted operator prose goes through the same canonical pipeline as
        # every other stored diagnostic: bounded, flattened, and stripped of
        # anything that looks like a credential BEFORE it becomes durable.
        run, _dispatch = self.bound()
        self.reacquire(
            run.id,
            WORKER_B,
            run.fencing_epoch,
            actor_label="operator:" + "d" * 400,
            reason="restarting with token sk-ant-api03-SECRETSECRETSECRETSECRET",
        )
        payload = self.events()[0]["payload"]
        self.assertLessEqual(len(payload["actor_label"]), 120)
        self.assertLessEqual(len(payload["reason"]), 500)
        self.assertNotIn("SECRETSECRETSECRETSECRET", payload["reason"])
        # And it is never dressed up as an identity.
        self.assertFalse(payload["actor_authenticated"])

    def test_there_is_no_public_event_writing_operation(self) -> None:
        # The audit writer stays private, so no caller can record a success
        # without the mutation that earned it - or after it.
        self.assertTrue(
            hasattr(SQLiteMemoryStore, "_insert_job_run_event"),
            "the private writer is missing",
        )
        for absent in ("record_job_run_event", "write_job_run_event", "log_job_run"):
            self.assertFalse(hasattr(SQLiteMemoryStore, absent), absent)
        self.assertFalse(hasattr(InMemoryStore, "record_job_run_event"))


# --------------------------------------------------------------------------
# RESUME - WHAT THE H2 JOURNAL ALLOWS
# --------------------------------------------------------------------------


class RecoveryResumeTests(RecoveryTestCase):
    """The orchestration: re-prove everything, then hand back the SAME session."""

    def test_a_generation_that_never_started_resumes_and_completes(self) -> None:
        run, dispatch = self.bound()
        self.assertEqual([], self.attempt_states())
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.COMPLETED, outcome.run.status)
        self.assertEqual(dispatch.session_id, outcome.session_id)
        self.assertEqual(dispatch.chain_id, outcome.chain_id)
        self.assertEqual(1, len(self.reader.calls))
        # SAME SESSION. Not a second one, then or now.
        self.assertEqual(1, self.sessions())
        self.assertEqual(dispatch.session_id, self.reload(run.id).session_id)

    def test_a_prepared_attempt_resumes_and_reaches_the_connector_once(self) -> None:
        # The worker died between opening the attempt and beginning it, so the
        # connector boundary was never crossed. Core reclaims the PREPARED row
        # through its own CAS and the call happens exactly once IN TOTAL.
        run, dispatch = self.crashed_run("begin_execution")
        self.assertEqual([AttemptState.PREPARED.value], self.attempt_states())
        self.assertEqual(0, len(self.reader.calls))
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.COMPLETED, outcome.run.status)
        self.assertEqual(1, len(self.reader.calls))
        self.assertEqual([AttemptState.FINISHED.value], self.attempt_states())
        self.assertEqual(1, self.sessions())

    def test_a_finished_attempt_resumes_with_no_second_connector_call(self) -> None:
        # The connector really ran and the attempt really concluded; only the
        # chain had not advanced. Core advances from the persisted result, so
        # the recovery costs ZERO further calls.
        run, dispatch = self.crashed_run("advance_chain_step")
        self.assertEqual([AttemptState.FINISHED.value], self.attempt_states())
        self.assertEqual(1, len(self.reader.calls))
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.COMPLETED, outcome.run.status)
        self.assertEqual(1, len(self.reader.calls))
        self.assertIs(ChainStatus.COMPLETED, self.chain_of(dispatch.session_id).status)

    def in_flight(self):
        """A dead owner's IN_FLIGHT attempt: the connector was reached and
        nobody ever established what came back."""
        run, lease = self.claimed()
        self.kill_at("conclude_execution")
        self.kill_at("mark_execution_unknown")
        dispatch_job_run(
            self.core,
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.at + timedelta(minutes=1),
        )
        self.revive("conclude_execution")
        self.revive("mark_execution_unknown")
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.attempt_states())
        self.assertEqual(1, len(self.reader.calls))
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        return stored, stored

    def test_an_in_flight_attempt_blocks_before_core_is_entered(self) -> None:
        # THE DEFERRED BLOCKER, held exactly where it is. The coordination
        # lease may be recovered - somebody has to be able to see this - but
        # nothing resumes, nothing retries, and the attempt is left untouched
        # for the human gate a later phase builds.
        run, dispatch = self.in_flight()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("execution_not_established", outcome.reason)
        # CORE WAS NEVER ENTERED, and the connector was never reached again.
        self.assertEqual([], scripted.calls)
        self.assertEqual(1, len(self.reader.calls))
        # The attempt is exactly as the dead owner left it.
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.attempt_states())
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)
        self.assertEqual(dispatch.session_id, stored.session_id)

    def test_an_unknown_outcome_attempt_blocks_before_core_is_entered(self) -> None:
        # The owner itself recorded that nobody could establish what happened -
        # the one production caller of that transition. Recovery reads it and
        # stops; it does not resolve, abandon or retry it.
        run, dispatch = self.in_flight()
        attempt = self.attempt_rows()[0]
        self.memory.mark_execution_unknown(attempt["id"])
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.attempt_states())
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("execution_not_established", outcome.reason)
        self.assertEqual([], scripted.calls)
        self.assertEqual(1, len(self.reader.calls))
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.attempt_states())
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_an_older_in_doubt_attempt_is_not_hidden_by_a_newer_clean_one(
        self,
    ) -> None:
        # THE COMPLETE HISTORY, not the newest row. There is no
        # one-attempt-per-task rule, so an unresolved in-doubt attempt can sit
        # UNDER a later clean one; reading only the top would resume over a
        # connector call nobody ever resolved.
        run, dispatch = self.crashed_run("advance_chain_step")
        finished = self.attempt_rows()[0]
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                """
                INSERT INTO execution_attempts
                    (id, session_id, task_id, connector, fingerprint, state,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "older-attempt",
                    finished["session_id"],
                    finished["task_id"],
                    finished["connector"],
                    finished["fingerprint"],
                    AttemptState.UNKNOWN_OUTCOME.value,
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.commit()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("execution_not_established", outcome.reason)
        self.assertEqual([], scripted.calls)

    def test_a_replaced_chain_blocks_after_acquisition(self) -> None:
        # Ownership was recovered - that part is honest - and then every proof
        # under it failed. The generation stays RUNNING and owned, and Core is
        # never entered.
        run, dispatch = self.bound()
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (dispatch.session_id,)
            ).fetchone()
            payload = json.loads(row["state_json"])
            payload["chain"]["id"] = "some-other-chain"
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(payload), dispatch.session_id),
            )
            connection.commit()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("bound_state_mismatch", outcome.reason)
        self.assertEqual([], scripted.calls)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(WORKER_B, stored.owner_token)

    def test_a_drifted_declaration_blocks_after_acquisition(self) -> None:
        run, dispatch = self.bound()
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (dispatch.session_id,)
            ).fetchone()
            payload = json.loads(row["state_json"])
            payload["chain"]["steps"][0]["objective"] = "something else entirely"
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(payload), dispatch.session_id),
            )
            connection.commit()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("bound_state_mismatch", outcome.reason)
        self.assertEqual([], scripted.calls)

    def test_a_foreign_routed_task_blocks_before_core(self) -> None:
        run, dispatch = self.crashed_run("advance_chain_step")
        task_id = self.task_ids(dispatch.session_id)[0]
        other = SessionState(objective="somebody else's work")
        self.memory.save_session(other)
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?", (other.id, task_id)
            )
            connection.commit()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("routing_unprovable", outcome.reason)
        self.assertEqual([], scripted.calls)

    def test_a_lease_that_dies_while_core_works_writes_nothing(self) -> None:
        # The recovery lease is not special: a reflection still needs a LIVE
        # one, proved against an instant sampled after Core returned.
        run, dispatch = self.bound()
        clock = ScriptedClock(
            self.after, self.after, self.after + timedelta(hours=1)
        )
        scripted = ScriptedCore(
            self.memory, (AgentRun(RunStatus.COMPLETED, None, None), "completed")
        )
        outcome = self.recover(
            run.id, WORKER_B, run.fencing_epoch, core=scripted, clock=clock
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("lease_expired", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertFalse(stored.settled)

    def test_the_post_run_instant_is_sampled_after_core_returns(self) -> None:
        run, _dispatch = self.bound()
        clock = ScriptedClock(self.after, self.after, self.after)
        scripted = ScriptedCore(
            self.memory,
            (AgentRun(RunStatus.COMPLETED, None, None), "completed"),
            clock=clock,
        )
        self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted, clock=clock)
        self.assertIn(f"run_chain:{scripted.calls[0]}", clock.events)
        self.assertGreater(
            len(clock.events) - 1 - clock.events[::-1].index("clock:" + self.after.isoformat()),
            clock.events.index(f"run_chain:{scripted.calls[0]}"),
            "the settling instant was not sampled after run_chain returned",
        )

    def test_the_post_run_instant_is_sampled_after_core_raises(self) -> None:
        run, _dispatch = self.bound()
        clock = ScriptedClock(self.after, self.after, self.after)
        scripted = ScriptedCore(
            self.memory, None, clock=clock, raises=WorkerDied("again")
        )
        outcome = self.recover(
            run.id, WORKER_B, run.fencing_epoch, core=scripted, clock=clock
        )
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("run_chain_raised", outcome.reason)
        # The contract is about ORDER and has no exception-shaped hole in it.
        self.assertGreater(
            len(clock.events) - 1,
            clock.events.index(f"run_chain:{scripted.calls[0]}"),
        )
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(WORKER_B, stored.owner_token)

    def test_a_refused_acquisition_never_enters_core(self) -> None:
        run, _dispatch = self.bound()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(
            run.id,
            WORKER_B,
            run.fencing_epoch,
            core=scripted,
            clock=lambda: self.just_live,
        )
        self.assertEqual(DISPATCH_REFUSED, outcome.verdict)
        self.assertIn("not_recoverable", outcome.reason)
        self.assertEqual([], scripted.calls)
        self.assertEqual(WORKER_A, self.reload(run.id).owner_token)

    def test_the_reflection_after_recovery_is_fenced_on_the_new_epoch(self) -> None:
        # The settlement really happened under the recovered ownership, and the
        # displaced owner could not have produced it.
        run, _dispatch = self.bound()
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(run.fencing_epoch + 1, stored.fencing_epoch)
        self.assertTrue(stored.settled)
        with self.assertRaises(JobError):
            self.memory.settle_job_run(
                run.id,
                JobRunStatus.FAILED,
                WORKER_A,
                run.fencing_epoch,
                now=self.after,
            )


# --------------------------------------------------------------------------
# PARITY AND AUTHORITY
# --------------------------------------------------------------------------


class ForeignSessionIdentityTests(RecoveryTestCase):
    """A persisted session document must BE the session it was loaded for.

    Storage answers "the session bound to this generation" by row key, and the
    answer is a DOCUMENT that carries its own identity. Nothing forces the two
    to agree once a row has been written by anything but the ordinary path, and
    Core works from the decoded object - so a document sitting under key S1 but
    claiming to be S2 makes every later write land in S2 while the generation
    still says S1. Three things have to name one session before any of this is
    trustworthy: the generation's binding, the row it was found under, and the
    identity the document itself asserts.
    """

    def forge_document(self, session_id: str, **document) -> None:
        """Rewrite a session's persisted DOCUMENT, leaving its row key alone.

        Deliberately raw SQL: no API can produce this state, which is exactly
        why the guard exists - the question is what the boundary does when
        storage is corrupt, not whether the ordinary path can corrupt it.
        """
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            payload = json.loads(row["state_json"])
            payload.update(document)
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(payload), session_id),
            )
            connection.commit()

    def forge_project_column(self, session_id: str, project_id: str) -> None:
        """Move only the AUTHORITATIVE column, leaving the document's own claim."""
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE sessions SET project_id = ? WHERE id = ?",
                (project_id, session_id),
            )
            connection.commit()

    def second_session(self) -> SessionState:
        """A REAL second session, so the forged identity names something that
        exists - otherwise the foreign key would refuse the writes and the
        test would prove nothing about the guard."""
        other = SessionState(objective="a second real session", project_id="proj-a")
        self.memory.save_session(other)
        return other

    def test_a_forged_document_identity_cannot_reach_core(self) -> None:
        # THE ATTACK. The generation stays bound to S1 and the row is still
        # found under S1, but the document inside it says S2. Core works from
        # the decoded object, so without this guard the step task would be
        # materialized and the connector reached under a session this
        # generation was never bound to - while the generation still reads as
        # bound to S1, leaving the executed work invisible from the JobRun.
        run, dispatch = self.bound()
        s1 = dispatch.session_id
        s2 = self.second_session()
        self.forge_document(s1, id=s2.id)

        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)

        # Refused by the FIRST defence, inside the recovery transaction, so the
        # ownership never moved either. ``_bound_state`` would have refused it
        # a second time; that guard is proved separately below so neither can
        # be masking the other.
        self.assertEqual(DISPATCH_REFUSED, outcome.verdict, outcome.reason)
        self.assertIn("the persisted session is not the bound session", outcome.reason)
        self.assertEqual([], self.events())
        self.assertEqual(run.fencing_epoch, self.reload(run.id).fencing_epoch)
        # NOTHING RAN. No connector, and no task anywhere - least of all one
        # under the foreign session.
        self.assertEqual(0, len(self.reader.calls))
        self.assertEqual(0, len(self.planner.calls))
        self.assertEqual(0, self.table_count("tasks"))
        self.assertEqual([], self.task_ids(s2.id))
        self.assertEqual([], self.task_ids(s1))
        # The binding is untouched and no third session was invented.
        stored = self.reload(run.id)
        self.assertEqual(s1, stored.session_id)
        self.assertEqual(dispatch.chain_id, stored.chain_id)
        self.assertEqual(2, self.sessions())

    def test_the_shared_binding_proof_refuses_a_forged_identity_directly(
        self,
    ) -> None:
        # THE SECOND DEFENCE, asked on its own so neither guard can be masking
        # the other. ``_bound_state`` is the proof BOTH phase entry points make
        # immediately before Core, and it is the last thing standing between a
        # forged document and ``run_chain`` - including on the 2B1 first
        # dispatch, whose binding transaction it re-proves the same way.
        run, dispatch = self.bound()
        s1 = dispatch.session_id
        s2 = self.second_session()
        stored = self.reload(run.id)
        # Honest first: the helper accepts exactly what it should.
        self.assertIsNotNone(_bound_state(self.memory, stored))
        self.forge_document(s1, id=s2.id)
        self.assertIsNone(_bound_state(self.memory, stored))
        # And it never went looking for the identity the document named.
        self.assertEqual(s1, self.reload(run.id).session_id)
        self.assertEqual(0, self.table_count("tasks"))

    def test_the_binding_proof_refuses_a_session_of_the_wrong_type(self) -> None:
        # A store that answered with something that merely looks like a session
        # would otherwise be trusted for every identity below it.
        run, dispatch = self.bound()
        stored = self.reload(run.id)

        class NotASession:
            id = dispatch.session_id
            project_id = "proj-a"
            chain = None

        class Liar:
            def load_session(self, _session_id):
                return NotASession()

        self.assertIsNone(_bound_state(Liar(), stored))

    def test_a_forged_document_identity_refuses_reacquisition_itself(self) -> None:
        # THE FIRST DEFENCE, proved on its own. A parked generation resolves
        # its chain cursor INSIDE the recovery transaction, so the identity
        # disagreement is caught before any ownership moves: no epoch change,
        # no lease, and no success audit for an acquisition that never
        # happened.
        run, dispatch, task_id, request = self.parked()
        s1 = dispatch.session_id
        s2 = self.second_session()
        self.memory.resolve_approval(
            request, PermissionGrant.for_request(request), "approved"
        )
        self.forge_document(s1, id=s2.id)

        with self.assertRaisesRegex(JobError, "is not the bound session"):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)

        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, stored.status)
        self.assertEqual(run.fencing_epoch, stored.fencing_epoch)
        self.assertIsNone(stored.owner_token)
        self.assertEqual(s1, stored.session_id)
        self.assertEqual([], self.events())
        self.assertEqual(0, len(self.planner.calls))

    def test_a_forged_document_identity_refuses_a_running_reacquisition(
        self,
    ) -> None:
        # And the same refusal for a RUNNING source, which resolves no cursor:
        # the identity proof is not a side effect of the approval rule, it is
        # its own check on every recovery.
        run, dispatch = self.bound()
        s2 = self.second_session()
        self.forge_document(dispatch.session_id, id=s2.id)

        with self.assertRaisesRegex(JobError, "is not the bound session"):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)

        stored = self.reload(run.id)
        self.assertEqual(run.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(dispatch.session_id, stored.session_id)
        self.assertEqual([], self.events())

    def test_an_unreadable_session_document_refuses_recovery(self) -> None:
        run, dispatch = self.bound()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                ("{not json at all", dispatch.session_id),
            )
            connection.commit()
        with self.assertRaisesRegex(JobError, "unreadable"):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual([], self.events())
        self.assertEqual(WORKER_A, self.reload(run.id).owner_token)

    def test_a_project_column_that_disagrees_with_the_document_refuses(
        self,
    ) -> None:
        # THE ADJACENT IDENTITY. The document still claims the project the
        # generation was scheduled under - so the post-acquisition declaration
        # proof would accept it - while the AUTHORITATIVE column, which is what
        # every project-scoped query answers from, says another. Row identity
        # and document identity must not be allowed to disagree.
        from jarvis.contracts import ProjectRecord

        self.memory.save_project(ProjectRecord(id="proj-b", name="Project B"))
        run, dispatch = self.bound()
        self.forge_project_column(dispatch.session_id, "proj-b")

        with self.assertRaisesRegex(JobError, "project"):
            self.reacquire(run.id, WORKER_B, run.fencing_epoch)

        stored = self.reload(run.id)
        self.assertEqual(run.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual([], self.events())
        self.assertEqual(0, len(self.reader.calls))

    def test_an_honest_session_still_recovers(self) -> None:
        # THE CONTROL. The new proof refuses corruption and nothing else: an
        # ordinary bound session, whose row key, document identity and project
        # column all agree, recovers and completes exactly as before.
        run, dispatch = self.bound()
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.COMPLETED, outcome.run.status)
        self.assertEqual(1, len(self.reader.calls))
        self.assertEqual(1, len(self.events()))


class RecoveryParityTests(unittest.TestCase):
    """The non-SQLite double refuses and permits exactly the same things."""

    NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        from jarvis.contracts import (
            ChainStep,
            JobKind,
            JobSpec,
            ProjectRecord,
        )
        from jarvis.schedules import Interval

        self.memory = InMemoryStore()
        self.memory.save_project(ProjectRecord(id="proj-a", name="Project A"))
        spec = JobSpec(
            project_id="proj-a",
            kind=JobKind.RECURRING,
            label="hourly digest",
            schedule=Interval(anchor=self.NOW, every=timedelta(hours=1)),
            steps=(
                ChainStep(
                    objective="summarize the log",
                    capability="echo",
                    action="read",
                    target="notes.txt",
                ),
            ),
        )
        record = self.memory.create_job(spec, now=self.NOW)
        self.at = self.NOW + timedelta(hours=1)
        run = self.memory.materialize_due_job_run(
            record.id, record.revision, now=self.at
        )
        lease = self.memory.claim_job_run(
            run.id, WORKER_A, now=self.at, lease=LEASE
        )
        self.dispatch = self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.at
        )
        self.run = self.memory.load_job_run(run.id)
        self.epoch = self.run.fencing_epoch
        self.expiry = self.at + LEASE
        self.after = self.at + timedelta(minutes=10)

    def reacquire(self, worker=WORKER_B, epoch=None, *, now=None, **over):
        fields = dict(actor_label=ACTOR, reason=WHY)
        fields.update(over)
        return self.memory.reacquire_job_run(
            self.run.id,
            worker,
            self.epoch if epoch is None else epoch,
            now=now or self.after,
            lease=LEASE,
            **fields,
        )

    def events(self):
        return [
            event
            for event in self.memory.list_events(self.dispatch.session_id)
            if event["event_type"] == "job_run_reacquired"
        ]

    def test_the_double_recovers_on_the_same_terms(self) -> None:
        lease = self.reacquire()
        self.assertEqual(self.epoch + 1, lease.fencing_epoch)
        stored = self.memory.load_job_run(self.run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(WORKER_B, stored.owner_token)
        self.assertEqual(self.dispatch.session_id, stored.session_id)
        self.assertEqual(self.dispatch.chain_id, stored.chain_id)
        self.assertEqual(self.after.isoformat(), stored.claimed_at)
        self.assertEqual(1, len(self.memory.sessions))
        events = self.events()
        self.assertEqual(1, len(events))
        self.assertIsNone(events[0]["task_id"])
        self.assertFalse(events[0]["payload"]["actor_authenticated"])
        self.assertNotIn(WORKER_B, json.dumps(events[0]["payload"]))

    def test_the_double_refuses_what_sqlite_refuses(self) -> None:
        for label, call in (
            ("live owner", lambda: self.reacquire(now=self.expiry - timedelta(microseconds=1))),
            ("stale epoch", lambda: self.reacquire(epoch=self.epoch - 1)),
            ("future epoch", lambda: self.reacquire(epoch=self.epoch + 1)),
            ("bool epoch", lambda: self.reacquire(epoch=True)),
            ("blank actor", lambda: self.reacquire(actor_label="")),
            ("blank reason", lambda: self.reacquire(reason="   ")),
            ("blank owner", lambda: self.reacquire(worker="")),
        ):
            with self.subTest(refusal=label):
                with self.assertRaises(JobError):
                    call()
                self.assertEqual([], self.events())
        stored = self.memory.load_job_run(self.run.id)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(self.epoch, stored.fencing_epoch)

    def test_the_double_treats_the_expiry_instant_as_expired(self) -> None:
        lease = self.reacquire(now=self.expiry)
        self.assertEqual(self.epoch + 1, lease.fencing_epoch)

    def test_the_double_refuses_a_stale_observer_after_a_later_epoch(self) -> None:
        self.reacquire(WORKER_B, self.epoch)
        with self.assertRaises(JobError):
            self.reacquire(
                WORKER_C, self.epoch, now=self.after + timedelta(minutes=30)
            )
        self.assertEqual(1, len(self.events()))

    def test_the_double_admits_exactly_one_concurrent_winner(self) -> None:
        results: list[object] = []
        barrier = threading.Barrier(2)

        def take(worker: str) -> None:
            barrier.wait()
            try:
                results.append(self.reacquire(worker, self.epoch))
            except JobError as exc:
                results.append(exc)

        threads = [
            threading.Thread(target=take, args=(worker,))
            for worker in (WORKER_B, WORKER_C)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len([r for r in results if isinstance(r, JobRunLease)]))
        self.assertEqual(1, len([r for r in results if isinstance(r, JobError)]))
        self.assertEqual(1, len(self.events()))

    def test_the_double_refuses_a_forged_session_document_identity(self) -> None:
        # PARITY FOR THE F1 BLOCKER. The dict key is this store's authoritative
        # row identity, so a value stored under S1 whose own ``id`` says S2 is
        # exactly the corrupted row SQLite refuses - and the double must not be
        # the weaker of the two.
        import dataclasses

        other = SessionState(objective="a second real session", project_id="proj-a")
        self.memory.save_session(other)
        forged = dataclasses.replace(
            self.memory.sessions[self.dispatch.session_id], id=other.id
        )
        self.memory.sessions[self.dispatch.session_id] = forged
        with self.assertRaisesRegex(JobError, "is not the bound session"):
            self.reacquire()
        stored = self.memory.load_job_run(self.run.id)
        self.assertEqual(WORKER_A, stored.owner_token)
        self.assertEqual(self.epoch, stored.fencing_epoch)
        self.assertEqual(self.dispatch.session_id, stored.session_id)
        self.assertEqual([], self.events())

    def test_the_double_refuses_a_disagreeing_project_record(self) -> None:
        # The authoritative project held BESIDE the document, mirroring the
        # SQLite ``sessions.project_id`` column: the two must not disagree.
        self.memory.session_projects[self.dispatch.session_id] = "proj-b"
        with self.assertRaisesRegex(JobError, "project record disagree"):
            self.reacquire()
        self.assertEqual(
            WORKER_A, self.memory.load_job_run(self.run.id).owner_token
        )
        self.assertEqual([], self.events())

    def test_the_double_refuses_a_missing_or_mistyped_session(self) -> None:
        self.memory.sessions[self.dispatch.session_id] = object()
        with self.assertRaisesRegex(JobError, "unreadable"):
            self.reacquire()
        self.assertEqual([], self.events())

    def test_the_double_refuses_an_unbound_or_wrongly_stated_generation(self) -> None:
        import dataclasses

        self.memory.job_runs[self.run.id] = dataclasses.replace(
            self.memory.job_runs[self.run.id], session_id=None, chain_id=None
        )
        with self.assertRaisesRegex(JobError, "not bound"):
            self.reacquire()
        self.assertEqual([], self.events())


class RecoveryAuthorityTests(unittest.TestCase):
    """What phase 2B2a did NOT add, proved from the source itself."""

    def source(self, obj) -> str:
        return inspect.getsource(obj)

    def test_recovery_holds_no_h2_or_approval_authority(self) -> None:
        import jarvis.dispatch as dispatch_module

        source = inspect.getsource(dispatch_module)
        tree = ast.parse(source)
        recover = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "recover_job_run"
        )
        called = {
            node.func.attr
            for node in ast.walk(recover)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for forbidden in (
            "open_execution_attempt",
            "begin_execution",
            "conclude_execution",
            "mark_execution_unknown",
            "abandon_unknown_attempt",
            "resolve_approval",
            "reject_task",
            "has_approved_grant",
            "save_pending_approval",
            "claim_job_run",
            "settle_job_run",
            "cancel_task",
            "cancel_chain",
            "save_session",
            "save_task",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, called)

    def test_the_reflection_is_shared_rather_than_copied(self) -> None:
        # A second copy of the outcome reading would be a second place for it
        # to drift, which is the same argument that gave storage exactly one
        # fenced transition helper.
        import jarvis.dispatch as dispatch_module

        tree = ast.parse(inspect.getsource(dispatch_module))
        for name in ("dispatch_job_run", "recover_job_run"):
            node = next(
                item
                for item in tree.body
                if isinstance(item, ast.FunctionDef) and item.name == name
            )
            calls = {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
            self.assertIn("_reflect_outcome", calls, name)

    def test_the_recoverable_set_is_disjoint_from_the_claimable_one(self) -> None:
        self.assertEqual(
            frozenset({JobRunStatus.RUNNING, JobRunStatus.AWAITING_APPROVAL}),
            REACQUIRABLE_JOB_RUN_STATUSES,
        )
        self.assertEqual(
            frozenset(), REACQUIRABLE_JOB_RUN_STATUSES & CLAIMABLE_JOB_RUN_STATUSES
        )
        self.assertNotIn(
            JobRunStatus.UNKNOWN_OUTCOME, REACQUIRABLE_JOB_RUN_STATUSES
        )

    def test_the_schema_did_not_move(self) -> None:
        from jarvis.app import OUTPUT_SCHEMA_VERSION
        from jarvis.memory import SCHEMA_VERSION

        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(6, OUTPUT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
