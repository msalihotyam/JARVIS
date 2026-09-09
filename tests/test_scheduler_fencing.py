"""Scheduler phase 2B0: the fencing boundary around CONCLUDING a generation.

Phase 2A fenced every mutation that moves a lease. It left one hole:
``settle_job_run`` closed a generation with no proof of ownership at all, so a
worker whose lease had expired - and whose successor was, by then, legitimately
working the same occurrence - could write an ending over it and unblock the
next generation on evidence nobody held. This suite is about that hole being
shut, and about the two narrow unsettled transitions a future dispatch phase
needs being shut the same way from the day they exist.

WHAT IS DELIBERATELY ABSENT IS ALSO UNDER TEST. There is still no dispatcher,
no AgentCore materialization, no connector call, no approval, no grant, no
reservation and no background loop, and ``UNKNOWN_OUTCOME`` still has no way
out: resolving one is a human authority gate that this build does not have and
must not grow by accident. ``BoundaryTests`` proves that from the shipped
source and from the durable tables rather than from this paragraph.

Every instant is EXPLICIT. Nothing sleeps, nothing polls, and no test performs
a live model, auth or network call.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import unittest
from datetime import timedelta
from pathlib import Path

from jarvis.contracts import (
    AttemptState,
    APPROVAL_PENDABLE_JOB_RUN_STATUSES,
    CLAIMABLE_JOB_RUN_STATUSES,
    SETTLEABLE_JOB_RUN_STATUSES,
    SETTLED_JOB_RUN_STATUSES,
    UNKNOWNABLE_JOB_RUN_STATUSES,
    UNSETTLED_JOB_RUN_STATUSES,
    JobError,
    JobRunStatus,
)
from tests.test_scheduler_claims import (  # noqa: F401
    LEASE,
    NOW,
    WORKER_A,
    WORKER_B,
    ClaimTestCase,
    setUpModule,
    table_count,
    tearDownModule,
)


class FenceTestCase(ClaimTestCase):
    """One due generation, claimed by ``WORKER_A`` under a live lease."""

    def setUp(self) -> None:
        super().setUp()
        self.at = NOW + timedelta(hours=1)
        self.run = self.due_run(self.at)
        self.lease = self.store.claim_job_run(
            self.run.id, WORKER_A, now=self.at, lease=LEASE
        )
        #: Comfortably inside the lease, and comfortably past its end. Both
        #: are stated, never slept for.
        self.during = self.at + timedelta(minutes=1)
        self.after = self.at + LEASE + timedelta(minutes=1)

    def running(self) -> None:
        """Put the claimed generation into the state a dispatch would."""
        self.force_status(self.run.id, JobRunStatus.RUNNING)

    def status(self) -> JobRunStatus:
        return self.store.load_job_run(self.run.id).status


# --------------------------------------------------------------------------
# SETTLEMENT IS FENCED
# --------------------------------------------------------------------------


class SettlementFenceTests(FenceTestCase):
    """Only the CURRENT owner, holding a LIVE lease, may state an outcome."""

    def settle(self, status, owner, epoch, now, **fields):
        return self.store.settle_job_run(
            self.run.id, status, owner, epoch, now=now, **fields
        )

    def test_the_current_owner_with_a_live_lease_settles(self) -> None:
        settled = self.settle(
            JobRunStatus.COMPLETED, WORKER_A, self.lease.fencing_epoch, self.during
        )
        self.assertTrue(settled.settled)
        self.assertEqual(JobRunStatus.COMPLETED, settled.status)
        # The SETTLEMENT INSTANT is the caller's, not a wall clock: an outcome
        # a test cannot pin down is an outcome nobody can audit.
        self.assertEqual(self.during.isoformat(), settled.settled_at)
        self.assertEqual(settled, self.store.load_job_run(self.run.id))

    def test_the_wrong_owner_token_cannot_settle(self) -> None:
        with self.assertRaisesRegex(JobError, "does not hold this lease"):
            self.settle(
                JobRunStatus.COMPLETED,
                WORKER_B,
                self.lease.fencing_epoch,
                self.during,
            )
        self.assertEqual(JobRunStatus.CLAIMED, self.status())

    def test_a_stale_fencing_epoch_cannot_settle(self) -> None:
        # The token is exactly right. The epoch is one behind, which is the
        # entire reason an epoch exists: a displaced worker still remembers
        # its token.
        for epoch in (self.lease.fencing_epoch - 1, self.lease.fencing_epoch + 1):
            with self.assertRaisesRegex(JobError, "does not hold this lease"):
                self.settle(JobRunStatus.COMPLETED, WORKER_A, epoch, self.during)
        self.assertEqual(JobRunStatus.CLAIMED, self.status())

    def test_an_expired_owner_cannot_settle(self) -> None:
        with self.assertRaisesRegex(JobError, "lease has expired"):
            self.settle(
                JobRunStatus.COMPLETED,
                WORKER_A,
                self.lease.fencing_epoch,
                self.after,
            )
        self.assertEqual(JobRunStatus.CLAIMED, self.status())

    def test_a_released_owner_cannot_settle(self) -> None:
        self.store.release_job_run(
            self.run.id, WORKER_A, self.lease.fencing_epoch, now=self.during
        )
        with self.assertRaisesRegex(JobError, "carries no lease"):
            self.settle(
                JobRunStatus.COMPLETED,
                WORKER_A,
                self.lease.fencing_epoch,
                self.during,
            )
        self.assertEqual(JobRunStatus.PENDING, self.status())

    def test_a_displaced_owner_cannot_settle_and_its_successor_can(self) -> None:
        # THE SCENARIO THE FENCE EXISTS FOR, end to end. A claims epoch 1; A's
        # lease expires; B reclaims with epoch 2; A - which never noticed -
        # tries to write the outcome it believes it owns.
        self.assertEqual(1, self.lease.fencing_epoch)
        taken = self.store.claim_job_run(
            self.run.id, WORKER_B, now=self.after, lease=LEASE
        )
        self.assertEqual(2, taken.fencing_epoch)
        for owner, epoch in (
            (WORKER_A, 1),  # the token and epoch A actually held
            (WORKER_A, 2),  # A guessing its successor's epoch
            (WORKER_B, 1),  # B's token under the superseded epoch
        ):
            with self.assertRaises(JobError):
                self.settle(JobRunStatus.COMPLETED, owner, epoch, self.after)
        self.assertEqual(JobRunStatus.CLAIMED, self.status())
        self.assertIsNone(self.store.load_job_run(self.run.id).settled_at)
        # And the worker that really does own it still can.
        settled = self.settle(
            JobRunStatus.FAILED, WORKER_B, taken.fencing_epoch, self.after
        )
        self.assertTrue(settled.settled)
        self.assertEqual(WORKER_B, self.store.load_job_run(self.run.id).owner_token)

    def test_a_settled_generation_cannot_be_settled_twice(self) -> None:
        self.settle(
            JobRunStatus.COMPLETED, WORKER_A, self.lease.fencing_epoch, self.during
        )
        for status in (JobRunStatus.FAILED, JobRunStatus.CANCELLED):
            with self.assertRaisesRegex(JobError, "already settled"):
                self.settle(
                    status, WORKER_A, self.lease.fencing_epoch, self.during
                )
        stored = self.store.load_job_run(self.run.id)
        self.assertEqual(JobRunStatus.COMPLETED, stored.status)
        self.assertEqual(self.during.isoformat(), stored.settled_at)

    def test_an_unowned_generation_cannot_be_settled_by_anyone(self) -> None:
        # A PENDING generation has no lease, so there is no proof to present -
        # which is the intended consequence of removing the unfenced path, not
        # an oversight. Closing an occurrence is a worker's statement about
        # work it owned.
        fresh = self.due_run(self.at, label="second job")
        self.assertEqual(JobRunStatus.PENDING, fresh.status)
        for owner, epoch in ((WORKER_A, 0), (WORKER_A, 1), ("", 0)):
            with self.assertRaises(JobError):
                self.store.settle_job_run(
                    fresh.id, JobRunStatus.CANCELLED, owner, epoch, now=self.during
                )
        self.assertIsNone(self.store.load_job_run(fresh.id).settled_at)

    def test_the_status_vocabulary_is_still_enforced(self) -> None:
        for bad in (JobRunStatus.RUNNING, JobRunStatus.PENDING):
            with self.assertRaisesRegex(JobError, "settled status"):
                self.settle(bad, WORKER_A, self.lease.fencing_epoch, self.during)
        for bad in ("completed", None, 1, True):
            with self.assertRaisesRegex(JobError, "exact JobRunStatus"):
                self.settle(bad, WORKER_A, self.lease.fencing_epoch, self.during)

    def test_the_instant_is_explicit_and_never_a_machine_clock(self) -> None:
        for bad in (None, "2026-09-06T13:00:00+00:00", self.during.replace(tzinfo=None)):
            with self.assertRaisesRegex(JobError, "aware datetime"):
                self.settle(
                    JobRunStatus.COMPLETED, WORKER_A, self.lease.fencing_epoch, bad
                )
        self.assertEqual(JobRunStatus.CLAIMED, self.status())

    def test_only_a_narrow_source_state_may_be_settled(self) -> None:
        # AWAITING_APPROVAL and UNKNOWN_OUTCOME are unsettled states a worker
        # must NOT be able to write an ending over: one belongs to a human's
        # answer, the other is the record that nobody could establish what
        # happened. Both keep their live lease here on purpose - the refusal
        # has to come from the state, not from an absent lease.
        for blocked in (JobRunStatus.AWAITING_APPROVAL, JobRunStatus.UNKNOWN_OUTCOME):
            self.force_status(self.run.id, blocked)
            with self.assertRaisesRegex(JobError, "not settleable"):
                self.settle(
                    JobRunStatus.COMPLETED,
                    WORKER_A,
                    self.lease.fencing_epoch,
                    self.during,
                )
            self.assertEqual(blocked, self.status())

    def test_a_running_generation_settles_under_its_own_lease(self) -> None:
        self.running()
        settled = self.settle(
            JobRunStatus.COMPLETED, WORKER_A, self.lease.fencing_epoch, self.during
        )
        self.assertTrue(settled.settled)

    def test_the_settleable_set_is_exactly_the_owned_working_states(self) -> None:
        self.assertEqual(
            frozenset({JobRunStatus.CLAIMED, JobRunStatus.RUNNING}),
            SETTLEABLE_JOB_RUN_STATUSES,
        )
        self.assertTrue(SETTLEABLE_JOB_RUN_STATUSES <= UNSETTLED_JOB_RUN_STATUSES)
        for unsafe in (
            JobRunStatus.PENDING,
            JobRunStatus.AWAITING_APPROVAL,
            JobRunStatus.UNKNOWN_OUTCOME,
        ):
            self.assertNotIn(unsafe, SETTLEABLE_JOB_RUN_STATUSES)


# --------------------------------------------------------------------------
# UNKNOWN OUTCOME
# --------------------------------------------------------------------------


class UnknownOutcomeTests(FenceTestCase):
    """An unestablishable outcome is recorded, fenced, and stays blocked."""

    def mark(self, owner, epoch, now):
        return self.store.mark_job_run_unknown(self.run.id, owner, epoch, now=now)

    def test_the_current_owner_of_a_running_generation_marks_it_unknown(self) -> None:
        self.running()
        marked = self.mark(WORKER_A, self.lease.fencing_epoch, self.during)
        self.assertEqual(JobRunStatus.UNKNOWN_OUTCOME, marked.status)
        # The lease is dropped - coordination ownership of a run only a human
        # can resolve means nothing - but the EVIDENCE of who was running it
        # is kept: the epoch does not roll back and the claim instant stays.
        self.assertIsNone(marked.owner_token)
        self.assertIsNone(marked.lease_expires_at_utc)
        self.assertEqual(self.lease.fencing_epoch, marked.fencing_epoch)
        self.assertEqual(self.at.isoformat(), marked.claimed_at)
        self.assertEqual(marked, self.store.load_job_run(self.run.id))

    def test_a_stale_worker_cannot_mark_a_generation_unknown(self) -> None:
        self.running()
        taken_after = self.after
        for owner, epoch, when in (
            (WORKER_B, self.lease.fencing_epoch, self.during),  # wrong token
            (WORKER_A, self.lease.fencing_epoch - 1, self.during),  # stale epoch
            (WORKER_A, self.lease.fencing_epoch, taken_after),  # expired lease
        ):
            with self.assertRaises(JobError):
                self.mark(owner, epoch, when)
        self.assertEqual(JobRunStatus.RUNNING, self.status())

    def test_an_unknown_outcome_is_unsettled_and_stays_unsettled(self) -> None:
        self.running()
        marked = self.mark(WORKER_A, self.lease.fencing_epoch, self.during)
        self.assertFalse(marked.settled)
        self.assertIsNone(marked.settled_at)
        self.assertIn(JobRunStatus.UNKNOWN_OUTCOME, UNSETTLED_JOB_RUN_STATUSES)
        self.assertNotIn(JobRunStatus.UNKNOWN_OUTCOME, SETTLED_JOB_RUN_STATUSES)
        # And the durable row agrees with the rebuilt object.
        self.assertIsNone(self.store.load_job_run(self.run.id).settled_at)

    def test_an_unknown_outcome_blocks_every_later_generation(self) -> None:
        self.running()
        self.mark(WORKER_A, self.lease.fencing_epoch, self.during)
        current = self.store.load_job(self.job.id)
        # Not now, not next hour, not in a decade. The job is stopped until a
        # human resolves it, and no elapsed time is a resolution.
        for later in (
            self.during,
            self.at + timedelta(hours=2),
            self.at + timedelta(days=3650),
        ):
            self.assertIsNone(
                self.store.materialize_due_job_run(
                    self.job.id, current.revision, now=later
                )
            )
        with self.assertRaisesRegex(JobError, "already open"):
            self.store.record_job_run(self.job.id, current.next_due_at_utc)
        self.assertEqual(1, table_count(self.database, "job_runs"))

    def test_an_unknown_outcome_is_never_reclaimed_retried_or_settled(self) -> None:
        self.running()
        self.mark(WORKER_A, self.lease.fencing_epoch, self.during)
        self.assertNotIn(JobRunStatus.UNKNOWN_OUTCOME, CLAIMABLE_JOB_RUN_STATUSES)
        long_after = self.at + timedelta(days=365)
        # No worker takes it over, however long it has sat there.
        for worker, when in ((WORKER_A, self.during), (WORKER_B, long_after)):
            with self.assertRaisesRegex(JobError, "not claimable"):
                self.store.claim_job_run(self.run.id, worker, now=when, lease=LEASE)
        # No settlement closes it, with or without a proof - the lease it
        # would need was cleared, and the state is not settleable either way.
        for owner, epoch in ((WORKER_A, self.lease.fencing_epoch), (WORKER_B, 2)):
            with self.assertRaises(JobError):
                self.store.settle_job_run(
                    self.run.id,
                    JobRunStatus.COMPLETED,
                    owner,
                    epoch,
                    now=self.during,
                )
        # And it cannot be re-marked, released or renewed either.
        for call in (
            lambda: self.mark(WORKER_A, self.lease.fencing_epoch, self.during),
            lambda: self.store.release_job_run(
                self.run.id, WORKER_A, self.lease.fencing_epoch, now=self.during
            ),
            lambda: self.store.renew_job_run_lease(
                self.run.id,
                WORKER_A,
                self.lease.fencing_epoch,
                now=self.during,
                lease=LEASE,
            ),
        ):
            with self.assertRaises(JobError):
                call()
        self.assertEqual(JobRunStatus.UNKNOWN_OUTCOME, self.status())

    def test_uncertainty_needs_an_attempt_to_be_uncertain_about(self) -> None:
        # A CLAIMED generation has started NO external action. Recording doubt
        # about it would manufacture the one condition that blocks a job
        # forever, out of a state that proves there is nothing to doubt.
        self.assertEqual(JobRunStatus.CLAIMED, self.status())
        with self.assertRaisesRegex(JobError, "no attempt whose outcome"):
            self.mark(WORKER_A, self.lease.fencing_epoch, self.during)
        self.assertEqual(JobRunStatus.CLAIMED, self.status())
        self.assertEqual(
            frozenset({JobRunStatus.RUNNING}), UNKNOWNABLE_JOB_RUN_STATUSES
        )


# --------------------------------------------------------------------------
# AWAITING APPROVAL
# --------------------------------------------------------------------------


class AwaitApprovalTests(FenceTestCase):
    """Parking for a human answer moves ONE state and clears ONE lease.

    The source is ``RUNNING``, not ``CLAIMED``. An approval requirement is
    something an attempt DISCOVERS - the chain runs, reaches a step that needs
    a person, and returns awaiting-approval - so by the time this transition is
    reached the generation is already executing. Every test here starts from a
    RUNNING generation for that reason.
    """

    APPROVAL_TABLES = ("pending_approvals", "permission_grants", "decision_log")

    def setUp(self) -> None:
        super().setUp()
        self.running()

    def park(self, owner, epoch, now):
        return self.store.await_job_run_approval(self.run.id, owner, epoch, now=now)

    def test_the_current_owner_parks_a_running_generation(self) -> None:
        parked = self.park(WORKER_A, self.lease.fencing_epoch, self.during)
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, parked.status)
        self.assertFalse(parked.settled)
        self.assertIsNone(parked.settled_at)
        self.assertEqual(parked, self.store.load_job_run(self.run.id))
        self.assertEqual(
            frozenset({JobRunStatus.RUNNING}), APPROVAL_PENDABLE_JOB_RUN_STATUSES
        )

    def test_a_claimed_generation_cannot_be_parked(self) -> None:
        # A CLAIMED generation is bound to no attempt, so there is nothing yet
        # identified to approve. Parking one would write an unsettled,
        # unclaimable row that blocks the job - and every later generation of
        # it - behind an approval request that names nothing.
        fresh = self.due_run(self.at, label="claimed only")
        held = self.store.claim_job_run(fresh.id, WORKER_A, now=self.at, lease=LEASE)
        self.assertEqual(
            JobRunStatus.CLAIMED, self.store.load_job_run(fresh.id).status
        )
        with self.assertRaisesRegex(JobError, "not awaiting-approval"):
            self.store.await_job_run_approval(
                fresh.id, WORKER_A, held.fencing_epoch, now=self.during
            )
        self.assertEqual(
            JobRunStatus.CLAIMED, self.store.load_job_run(fresh.id).status
        )
        self.assertNotIn(
            JobRunStatus.CLAIMED, APPROVAL_PENDABLE_JOB_RUN_STATUSES
        )

    def test_a_stale_owner_cannot_park_a_generation(self) -> None:
        with self.assertRaisesRegex(JobError, "does not hold this lease"):
            self.park(WORKER_B, self.lease.fencing_epoch, self.during)
        self.assertEqual(JobRunStatus.RUNNING, self.status())

    def test_a_stale_fencing_epoch_cannot_park_a_generation(self) -> None:
        for epoch in (self.lease.fencing_epoch - 1, self.lease.fencing_epoch + 1):
            with self.assertRaisesRegex(JobError, "does not hold this lease"):
                self.park(WORKER_A, epoch, self.during)
        self.assertEqual(JobRunStatus.RUNNING, self.status())

    def test_an_expired_lease_cannot_park_a_generation(self) -> None:
        # THE CASE A REAL 2B1 CALLER MUST NOT HIT. A chain that outlived its
        # lease is refused here rather than allowed to park a run another
        # worker may already own - which is why the caller must read ``now``
        # after its work returns, not before it dispatched.
        with self.assertRaisesRegex(JobError, "lease has expired"):
            self.park(WORKER_A, self.lease.fencing_epoch, self.after)
        self.assertEqual(JobRunStatus.RUNNING, self.status())
        # And a displaced owner stays refused after a successor takes over.
        self.force_status(self.run.id, JobRunStatus.CLAIMED)
        taken = self.store.claim_job_run(
            self.run.id, WORKER_B, now=self.after, lease=LEASE
        )
        self.force_status(self.run.id, JobRunStatus.RUNNING)
        with self.assertRaises(JobError):
            self.park(WORKER_A, self.lease.fencing_epoch, self.after)
        self.assertEqual(JobRunStatus.RUNNING, self.status())
        # The worker that really owns it can.
        parked = self.park(WORKER_B, taken.fencing_epoch, self.after)
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, parked.status)

    def test_parking_clears_the_lease_and_keeps_the_fencing_evidence(self) -> None:
        parked = self.park(WORKER_A, self.lease.fencing_epoch, self.during)
        self.assertIsNone(parked.owner_token)
        self.assertIsNone(parked.lease_expires_at_utc)
        # THE EPOCH IS NOT ROLLED BACK, so the proof this call just consumed
        # can never match again - the parking worker cannot un-park its own
        # generation with the same token and epoch.
        self.assertEqual(self.lease.fencing_epoch, parked.fencing_epoch)
        self.assertEqual(self.at.isoformat(), parked.claimed_at)
        stored = self.store.load_job_run(self.run.id)
        self.assertIsNone(stored.owner_token)
        self.assertIsNone(stored.lease_expires_at_utc)
        self.assertEqual(self.lease.fencing_epoch, stored.fencing_epoch)
        self.assertEqual(self.at.isoformat(), stored.claimed_at)
        # The consumed proof is dead: re-presenting it parks nothing.
        with self.assertRaisesRegex(JobError, "carries no lease"):
            self.park(WORKER_A, self.lease.fencing_epoch, self.during)
        # The cleared lease is durable, not merely absent from the projection.
        summary = self.store.list_job_runs(self.job.id, 10)[0]
        self.assertFalse(summary.claimed)
        self.assertIsNone(summary.lease_expires_at_utc)
        self.assertEqual(self.lease.fencing_epoch, summary.fencing_epoch)

    def test_parking_creates_no_approval_grant_or_decision(self) -> None:
        before = {t: table_count(self.database, t) for t in self.APPROVAL_TABLES}
        self.park(WORKER_A, self.lease.fencing_epoch, self.during)
        for table in self.APPROVAL_TABLES:
            self.assertEqual(0, before[table], table)
            self.assertEqual(0, table_count(self.database, table), table)
        # Nor does the generation acquire any materialization association: an
        # approval would be FOR something, and this phase binds nothing.
        stored = self.store.load_job_run(self.run.id)
        for absent in (
            stored.session_id,
            stored.task_id,
            stored.chain_id,
            stored.attempt_id,
        ):
            self.assertIsNone(absent)

    def test_a_parked_generation_is_blocked_for_workers_and_for_the_job(self) -> None:
        self.park(WORKER_A, self.lease.fencing_epoch, self.during)
        self.assertNotIn(JobRunStatus.AWAITING_APPROVAL, CLAIMABLE_JOB_RUN_STATUSES)
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.store.claim_job_run(
                self.run.id, WORKER_B, now=self.after, lease=LEASE
            )
        current = self.store.load_job(self.job.id)
        self.assertIsNone(
            self.store.materialize_due_job_run(
                self.job.id, current.revision, now=self.at + timedelta(days=365)
            )
        )

    def test_a_parked_generation_cannot_be_settled_or_marked_unknown(self) -> None:
        self.park(WORKER_A, self.lease.fencing_epoch, self.during)
        for call in (
            lambda: self.store.settle_job_run(
                self.run.id,
                JobRunStatus.COMPLETED,
                WORKER_A,
                self.lease.fencing_epoch,
                now=self.during,
            ),
            lambda: self.store.mark_job_run_unknown(
                self.run.id, WORKER_A, self.lease.fencing_epoch, now=self.during
            ),
        ):
            with self.assertRaises(JobError):
                call()
        self.assertEqual(JobRunStatus.AWAITING_APPROVAL, self.status())
        self.assertIsNone(self.store.load_job_run(self.run.id).settled_at)


# --------------------------------------------------------------------------
# THE BOUNDARY
# --------------------------------------------------------------------------


class BoundaryTests(FenceTestCase):
    """What phase 2B0 still does NOT have, proved from source and storage."""

    #: Every table a real execution would have to touch. None of them may gain
    #: a row from anything in this phase.
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

    def production_modules(self) -> list[Path]:
        root = Path(__file__).resolve().parent.parent / "jarvis"
        return sorted(root.rglob("*.py"))

    def test_the_new_transitions_create_no_authority_row_of_any_kind(self) -> None:
        parked = self.due_run(self.at, label="parking job")
        parking = self.store.claim_job_run(
            parked.id, WORKER_A, now=self.at, lease=LEASE
        )
        self.force_status(parked.id, JobRunStatus.RUNNING)
        self.store.await_job_run_approval(
            parked.id, WORKER_A, parking.fencing_epoch, now=self.during
        )
        self.running()
        self.store.mark_job_run_unknown(
            self.run.id, WORKER_A, self.lease.fencing_epoch, now=self.during
        )
        for table in self.AUTHORITY_TABLES:
            self.assertEqual(0, table_count(self.database, table), table)
        # Two jobs, one generation each. That is the ONLY durable evidence.
        self.assertEqual(2, table_count(self.database, "jobs"))
        self.assertEqual(2, table_count(self.database, "job_runs"))

    def test_no_forced_or_retrying_operation_exists_anywhere(self) -> None:
        # Human resolution of UNKNOWN_OUTCOME arrived in phase 2B2b1, with the
        # explicit design this guard always demanded of it - so
        # ``resolve_unknown`` left the list, exactly as ``recover_job_run`` did
        # in 2B2a, and for the same reason. What stays forbidden is everything
        # that would unblock a job WITHOUT that design: a settle that forces an
        # outcome, a clear that erases the doubt, a retry that replays it.
        #
        # The distinction the list protects is unchanged. The resolution that
        # exists ends a GENERATION whose attempt was explicitly declared
        # orphaned and explicitly abandoned; it never touches the attempt,
        # never says what happened, and never makes anything runnable again.
        for module in self.production_modules():
            source = module.read_text(encoding="utf-8")
            for absent in (
                "resolve_job_run",
                "force_settle",
                "force_resolve",
                "clear_unknown_outcome",
                "retry_job_run",
                "retry_attempt",
            ):
                self.assertNotIn(absent, source, f"{module.name}: {absent}")
        from jarvis.contracts import REACQUIRABLE_JOB_RUN_STATUSES
        from jarvis.memory import SQLiteMemoryStore

        for absent in ("resolve_job_run", "retry_job_run", "abandon_job_run"):
            self.assertFalse(hasattr(SQLiteMemoryStore, absent), absent)
        # An unknown-outcome generation is still not recoverable by anyone, and
        # the resolution added in 2B2b1 does not change that: it SETTLES the
        # generation rather than returning it to anybody's ownership.
        self.assertNotIn(
            JobRunStatus.UNKNOWN_OUTCOME, REACQUIRABLE_JOB_RUN_STATUSES
        )

    def test_no_production_writer_ever_leaves_unknown_outcome(self) -> None:
        # THE SINK PROPERTY, on which the whole 2B2b1 read-then-fenced-write
        # reasoning rests: once an attempt is UNKNOWN_OUTCOME, NO writer may
        # move it anywhere else. That is what lets the scheduler read the
        # history and then perform a fenced JobRun mutation without the reading
        # going stale underneath it.
        #
        # Every attempt-state writer in the production store is found and its
        # compare-and-set SOURCE state is read out of the parameters bound to
        # it. The source is what the statement is allowed to overwrite, so the
        # property is exactly "no writer names UNKNOWN_OUTCOME as a source".
        import re

        from jarvis.memory import SQLiteMemoryStore

        source = pathlib.Path(inspect.getfile(SQLiteMemoryStore)).read_text(
            encoding="utf-8"
        )
        # Flattened so that implicit string concatenation across lines - which
        # every one of these statements uses - cannot hide a writer from the
        # scan. A regex over the raw text found only one of the four.
        flat = " ".join(re.sub(r"[\"']", " ", source).split())
        writers = list(re.finditer(r"UPDATE execution_attempts", flat))
        self.assertEqual(
            4,
            len(writers),
            "an attempt-state writer was added or removed; the sink property "
            "must be re-proved for it deliberately",
        )
        sources = []
        for writer in writers:
            window = flat[writer.start() : writer.start() + 280]
            self.assertIn("state = ?", window)
            states = re.findall(r"AttemptState\.([A-Z_]+)", window)
            self.assertGreaterEqual(len(states), 2, window)
            sources.append(states[-1])
        self.assertNotIn(
            AttemptState.UNKNOWN_OUTCOME.name,
            sources,
            "a writer accepts UNKNOWN_OUTCOME as a source state",
        )
        # And the four are exactly the transitions the design accounts for.
        self.assertEqual(
            {AttemptState.PREPARED.name, AttemptState.IN_FLIGHT.name},
            set(sources),
        )

    def test_an_unknown_outcome_generation_is_still_recoverable_by_nobody(
        self,
    ) -> None:
        # The deferred gate, asked of the real store. A generation whose
        # external outcome nobody could establish is refused by BOTH ownership
        # paths - the generic claim and the phase-2B2a recovery - so no timer,
        # worker or lease expiry resolves it. A person does, through an
        # authority gate that still does not exist.
        self.running()
        self.store.mark_job_run_unknown(
            self.run.id, WORKER_A, self.lease.fencing_epoch, now=self.during
        )
        stored = self.store.load_job_run(self.run.id)
        self.assertEqual(JobRunStatus.UNKNOWN_OUTCOME, stored.status)
        with self.assertRaises(JobError):
            self.store.claim_job_run(
                self.run.id, WORKER_B, now=self.after, lease=LEASE
            )
        with self.assertRaises(JobError):
            self.store.reacquire_job_run(
                self.run.id,
                WORKER_B,
                stored.fencing_epoch,
                now=self.after,
                lease=LEASE,
                actor_label="operator",
                reason="trying to unstick it",
            )
        self.assertEqual(
            JobRunStatus.UNKNOWN_OUTCOME, self.store.load_job_run(self.run.id).status
        )

    def test_no_autonomous_dispatch_path_reaches_a_generation(self) -> None:
        # Phase 2B1 added ONE callable bridge, ``dispatch.py``. What stays
        # absent is anything that would drive it by itself, and any second
        # execution path around it.
        for module in self.production_modules():
            source = module.read_text(encoding="utf-8")
            for absent in (
                "run_job_run",
                "execute_job_run",
                "job_run_to_task",
                "materialize_job_run_task",
            ):
                self.assertNotIn(absent, source, f"{module.name}: {absent}")
            if module.name != "dispatch.py":
                self.assertNotIn("dispatch_job_run", source, module.name)
        names = {module.name for module in self.production_modules()}
        for absent in ("dispatcher.py", "scheduler_dispatch.py", "worker.py", "daemon.py"):
            self.assertNotIn(absent, names)
        # And nothing wires the EXECUTION bridge into the app or the CLI.
        # ``cli.py`` has its own unrelated ``_dispatch_*`` command helpers, so
        # the check names the entry points, not the word.
        #
        # Phase 2B2b2 gave the operator surface a reason to import the module
        # at all - cancelling a generation is an operator act - so the module
        # name left this list. What did NOT change is the property it was
        # protecting: the app and the CLI may ask for a generation to STOP, and
        # they still have no way to make one RUN. Only the non-executing entry
        # points may be named there.
        for module in self.production_modules():
            if module.name in ("app.py", "cli.py"):
                source = module.read_text(encoding="utf-8")
                for absent in ("dispatch_job_run", "recover_job_run"):
                    self.assertNotIn(absent, source, f"{module.name}: {absent}")

    def test_only_the_one_shot_bridge_calls_the_fenced_operations(self) -> None:
        # ``memory.py`` defines them, ``contracts.py`` declares them on the
        # provider-independent protocol, and from phase 2B1 ``dispatch.py``
        # calls them - it is the ONE production caller, and it is a callable
        # boundary rather than anything that runs by itself. Nothing else may
        # even mention them.
        allowed = {"memory.py", "contracts.py", "dispatch.py"}
        for module in self.production_modules():
            if module.name in allowed:
                continue
            source = module.read_text(encoding="utf-8")
            for name in (
                "settle_job_run",
                "mark_job_run_unknown",
                "await_job_run_approval",
                "bind_job_run_dispatch",
            ):
                self.assertNotIn(name, source, f"{module.name} mentions {name}")

    def test_the_one_shot_bridge_holds_no_execution_authority(self) -> None:
        # The permission dispatch.py gains is narrow and is proved narrow: it
        # may ask Core to run and then READ what Core wrote, and it may not
        # reach past Core into anything that authorizes an effect.
        source = (
            Path(__file__).resolve().parent.parent / "jarvis" / "dispatch.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        for forbidden in (
            "execute",
            "open_execution_attempt",
            "begin_execution",
            "conclude_execution",
            "mark_execution_unknown",
            "finalize_task",
            "resolve_approval",
            "record_permission_grant",
            "reserve",
            "record_usage",
            "evaluate",
            "materialize_chain_step",
            "advance_chain_step",
            "declare_chain_if_absent",
            "save_session",
            "save_task",
        ):
            self.assertNotIn(forbidden, called, f"dispatch.py calls {forbidden}")
        # The ONLY Core entry point it uses.
        self.assertIn("run_chain", called)
        for absent in ("threading", "Thread", "time.sleep", "asyncio", "schedule("):
            self.assertNotIn(absent, source, f"dispatch.py reaches {absent}")

    def test_no_background_loop_thread_or_service_was_added(self) -> None:
        for module in self.production_modules():
            source = module.read_text(encoding="utf-8")
            for absent in (
                "def run_scheduler",
                "def scheduler_loop",
                "def poll_job_runs",
                "auto_settle",
                "auto_recover",
            ):
                self.assertNotIn(absent, source, f"{module.name}: {absent}")

    def test_the_schema_versions_did_not_move(self) -> None:
        from jarvis.app import OUTPUT_SCHEMA_VERSION
        from jarvis.memory import SCHEMA_VERSION

        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(6, OUTPUT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
