"""Scheduler phase 2B2b2: minimum-safe cancellation of a scheduled generation.

Cancellation here is ONE STICKY BIT and an order of operations. It records that
a person asked for an occurrence to stop; it never claims a connector stopped,
never claims an external effect did not happen, and never turns an unresolved
execution into a clean ending.

WHAT THE TESTS ARE PROTECTING is the boundary between "no future work" and "no
past work". A cancellation that arrives before anything crossed the connector
boundary must leave the connector count at zero; one that arrives after must
leave the effect, its result and its money exactly as they stand. The two are
separated by a single compare-and-set inside the execution claim transaction,
and most of what follows exists to prove that seam holds from both sides.

THE RACES ARE REAL. Where a race decides the answer, it is driven by committing
the competing transaction from inside the other one's window - a store wrapper
that cancels between the binding and the claim, a barrier between two threads -
rather than by asserting what the code looks like.

Every instant is EXPLICIT and every clock is injected. Nothing sleeps, nothing
polls, and no test performs a live model, auth or network call.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import unittest
from datetime import timedelta

from jarvis.contracts import (
    AttemptRecovery,
    AttemptState,
    ChainStatus,
    ErrorCode,
    ExecutionClaimOutcome,
    JobError,
    JobRunStatus,
    PermissionGrant,
    PermissionRequest,
    TaskStatus,
)
from jarvis.dispatch import (
    DISPATCH_BLOCKED,
    DISPATCH_CANCEL_INTENT,
    DISPATCH_REFUSED,
    DISPATCH_SETTLED,
    DISPATCH_UNKNOWN,
    cancel_job_run,
    dispatch_job_run,
)
from jarvis.memory import SQLiteMemoryStore

from tests.test_scheduler_dispatch import (  # noqa: F401
    LEASE,
    WORKER_A,
    WORKER_B,
    ScriptedCore,
    setUpModule,
    tearDownModule,
)
from tests.test_scheduler_recovery import ACTOR, WHY, WORKER_C, RecoveryTestCase


class CancelTestCase(RecoveryTestCase):
    """A real generation, and the operator asking it to stop."""

    def cancel(self, run_id, *, worker=WORKER_B, store=None, **over):
        fields = dict(actor_label=ACTOR, reason=WHY)
        fields.update(over)
        return cancel_job_run(
            store or self.memory,
            run_id,
            worker,
            clock=lambda: self.after,
            **fields,
        )

    def request(self, run_id, *, now=None, store=None, **over):
        fields = dict(actor_label=ACTOR, reason=WHY)
        fields.update(over)
        return (store or self.memory).request_job_run_cancel(
            run_id, now=now or self.after, **fields
        )

    def flagged(self, run_id) -> bool:
        return self.reload(run_id).cancel_requested

    def audit(self, event_type: str) -> list[dict]:
        with contextlib.closing(self.raw()) as connection:
            rows = list(
                connection.execute(
                    "SELECT session_id, task_id, payload_json FROM event_log "
                    "WHERE event_type = ? ORDER BY id",
                    (event_type,),
                )
            )
        return [
            {
                "session_id": row["session_id"],
                "task_id": row["task_id"],
                "payload": json.loads(row["payload_json"]),
                "payload_json": row["payload_json"],
            }
            for row in rows
        ]

    def approval_status(self, task_id: str) -> str | None:
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT status FROM pending_approvals WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return None if row is None else row["status"]

    def attempt_rows(self) -> list[sqlite3.Row]:
        """Every attempt row, INCLUDING the stamp the orphan guard reads."""
        with contextlib.closing(self.raw()) as connection:
            return list(
                connection.execute(
                    "SELECT id, task_id, session_id, connector, fingerprint, "
                    "state, created_at, updated_at FROM execution_attempts "
                    "ORDER BY rowid"
                )
            )

    def in_flight(self):
        """A dead owner's IN_FLIGHT attempt, produced by a real death."""
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
        return self.reload(run.id)


# --------------------------------------------------------------------------
# F3 - IMMEDIATE CANCELLATION NEEDS THE COMPLETE UNBOUND SHAPE
# --------------------------------------------------------------------------


class ImmediateCancelTests(CancelTestCase):
    """When a generation is provably a queue position and nothing more."""

    def test_a_pristine_generation_is_cancelled_outright(self) -> None:
        run, _lease = self.claimed()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE job_runs SET status = 'pending', owner_token = NULL, "
                "lease_expires_at_utc = NULL WHERE id = ?",
                (run.id,),
            )
            connection.commit()
        cancelled = self.request(run.id)
        self.assertEqual(JobRunStatus.CANCELLED, cancelled.status)
        self.assertTrue(cancelled.settled)
        self.assertTrue(cancelled.cancel_requested)
        self.assertEqual(self.after.isoformat(), cancelled.settled_at)
        # NOTHING was created to cancel: no session, no task, no attempt.
        self.assertEqual(0, self.sessions())
        self.assertEqual(0, self.table_count("tasks"))
        self.assertEqual(0, self.table_count("execution_attempts"))
        self.assertEqual(0, len(self.reader.calls))

    def test_every_partial_binding_shape_refuses_immediate_settlement(self) -> None:
        # THE F3 RULE. "No session" alone is never read as "nothing ran": a
        # partially populated binding is corrupted or racing state, and the
        # honest answer is intent, never a settlement claiming nothing happened.
        for column, value in (
            ("session_id", "some-session"),
            ("chain_id", "some-chain"),
            ("task_id", "some-task"),
            ("attempt_id", "some-attempt"),
        ):
            with self.subTest(column=column):
                self.setUp()
                run, _lease = self.claimed()
                with contextlib.closing(self.raw()) as connection:
                    connection.execute(
                        "UPDATE job_runs SET status = 'pending', "
                        "owner_token = NULL, lease_expires_at_utc = NULL, "
                        f"{column} = ? WHERE id = ?",
                        (value, run.id),
                    )
                    connection.commit()
                flagged = self.request(run.id)
                self.assertTrue(flagged.cancel_requested)
                self.assertFalse(flagged.settled)
                self.assertEqual(JobRunStatus.PENDING, flagged.status)

    def test_a_live_lease_holder_blocks_immediate_settlement(self) -> None:
        # CLAIMED with a live lease: the holder could still be binding. Intent
        # only, and the bind it might attempt is now refused.
        run, lease = self.claimed()
        flagged = self.request(run.id, now=self.at)
        self.assertTrue(flagged.cancel_requested)
        self.assertFalse(flagged.settled)
        self.assertEqual(JobRunStatus.CLAIMED, flagged.status)
        with self.assertRaises(JobError):
            self.memory.bind_job_run_dispatch(
                run.id, WORKER_A, lease.fencing_epoch, now=self.at
            )
        self.assertEqual(0, self.sessions())

    def test_an_expired_claim_is_cancelled_outright(self) -> None:
        run, _lease = self.claimed()
        cancelled = self.request(run.id)
        self.assertEqual(JobRunStatus.CANCELLED, cancelled.status)
        self.assertTrue(cancelled.settled)
        self.assertEqual(0, self.sessions())


class CancelRequestTests(CancelTestCase):
    """The intent bit itself: what it refuses, and that it never comes back."""

    def test_the_flag_is_sticky_and_idempotent(self) -> None:
        run, _dispatch = self.bound()
        first = self.request(run.id)
        self.assertTrue(first.cancel_requested)
        self.assertEqual(1, len(self.audit("job_run_cancel_requested")))
        second = self.request(run.id)
        self.assertTrue(second.cancel_requested)
        # No second record for a request that changed nothing.
        self.assertEqual(1, len(self.audit("job_run_cancel_requested")))
        self.assertTrue(self.flagged(run.id))

    def test_a_settled_generation_is_refused(self) -> None:
        # A real run all the way to its own settlement; there is nothing left
        # to stop.
        run, lease = self.claimed()
        outcome = dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.at
        )
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        settled = self.reload(run.id)
        self.assertTrue(settled.settled)
        with self.assertRaises(JobError):
            self.request(run.id)
        self.assertFalse(self.flagged(run.id))

    def test_an_unknown_outcome_generation_is_refused(self) -> None:
        # UNCERTAINTY HAS ONE EXIT and it ends FAILED. Cancelling it would turn
        # "nobody knows" into "nothing happened".
        run = self.in_flight()
        row = self.attempt_rows()[0]
        self.memory.mark_execution_unknown(row["id"])
        self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(
            JobRunStatus.UNKNOWN_OUTCOME, self.reload(run.id).status
        )
        with self.assertRaisesRegex(JobError, "in doubt"):
            self.request(run.id)
        self.assertFalse(self.flagged(run.id))
        outcome = self.cancel(run.id)
        self.assertEqual(DISPATCH_REFUSED, outcome.verdict)
        self.assertEqual(
            JobRunStatus.UNKNOWN_OUTCOME, self.reload(run.id).status
        )

    def test_blank_attribution_is_refused(self) -> None:
        run, _dispatch = self.bound()
        for over in ({"actor_label": "  "}, {"reason": ""}):
            with self.subTest(over=over):
                with self.assertRaises(JobError):
                    self.request(run.id, **over)
        self.assertFalse(self.flagged(run.id))
        self.assertEqual([], self.audit("job_run_cancel_requested"))

    def test_the_record_states_what_it_is_not(self) -> None:
        run, dispatch = self.bound()
        self.request(run.id)
        events = self.audit("job_run_cancel_requested")
        self.assertEqual(1, len(events))
        self.assertEqual(dispatch.session_id, events[0]["session_id"])
        payload = events[0]["payload"]
        self.assertEqual(run.id, payload["run_id"])
        self.assertEqual(ACTOR, payload["actor_label"])
        self.assertFalse(payload["actor_authenticated"])
        self.assertFalse(payload["settled_immediately"])
        self.assertFalse(payload["connector_stopped"])
        self.assertFalse(payload["external_effect_established"])
        self.assertFalse(payload["retry_authorized"])
        self.assertNotIn(WORKER_A, events[0]["payload_json"])

    def test_two_concurrent_requesters_write_one_record(self) -> None:
        run, _dispatch = self.bound()
        results: list = []
        barrier = threading.Barrier(2)

        def ask(actor: str) -> None:
            store = SQLiteMemoryStore(self.database)
            barrier.wait()
            try:
                results.append(
                    store.request_job_run_cancel(
                        run.id, now=self.after, actor_label=actor, reason=WHY
                    )
                )
            except JobError as exc:  # pragma: no cover - defensive
                results.append(exc)

        threads = [
            threading.Thread(target=ask, args=(actor,))
            for actor in ("operator:one", "operator:two")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(not isinstance(r, JobError) for r in results))
        self.assertTrue(self.flagged(run.id))
        self.assertEqual(1, len(self.audit("job_run_cancel_requested")))


# --------------------------------------------------------------------------
# CLAIM / BIND RACES
# --------------------------------------------------------------------------


class ClaimAndBindRaceTests(CancelTestCase):
    """A flagged generation acquires no new owner and no new session."""

    def test_a_flagged_generation_cannot_be_claimed(self) -> None:
        # An unsettled generation carrying the flag acquires no new owner. The
        # flag is planted rather than requested because an ordinary request
        # would settle this shape outright - that path is its own test.
        run, _lease = self.claimed()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE job_runs SET status = 'pending', owner_token = NULL, "
                "lease_expires_at_utc = NULL, cancel_requested = 1 WHERE id = ?",
                (run.id,),
            )
            connection.commit()
        with self.assertRaisesRegex(JobError, "not claimable"):
            self.memory.claim_job_run(run.id, WORKER_A, now=self.after, lease=LEASE)
        stored = self.reload(run.id)
        self.assertIsNone(stored.owner_token)
        self.assertEqual(JobRunStatus.PENDING, stored.status)

    def test_a_flagged_generation_cannot_be_bound(self) -> None:
        run, lease = self.claimed()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE job_runs SET cancel_requested = 1 WHERE id = ?", (run.id,)
            )
            connection.commit()
        with self.assertRaisesRegex(JobError, "asked to cancel"):
            self.memory.bind_job_run_dispatch(
                run.id, WORKER_A, lease.fencing_epoch, now=self.at
            )
        # No session, no chain, no RUNNING transition.
        self.assertEqual(0, self.sessions())
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.CLAIMED, stored.status)
        self.assertIsNone(stored.session_id)

    def test_a_flagged_generation_refuses_the_whole_dispatch(self) -> None:
        run, lease = self.claimed()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE job_runs SET cancel_requested = 1 WHERE id = ?", (run.id,)
            )
            connection.commit()
        scripted = ScriptedCore(self.memory, None)
        outcome = dispatch_job_run(
            scripted, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.at
        )
        self.assertEqual(DISPATCH_REFUSED, outcome.verdict)
        self.assertIn("not_bindable", outcome.reason)
        self.assertEqual([], scripted.calls)
        self.assertEqual(0, len(self.reader.calls))

    def test_binding_first_leaves_cancellation_as_intent(self) -> None:
        run, _dispatch = self.bound()
        flagged = self.request(run.id)
        self.assertTrue(flagged.cancel_requested)
        self.assertFalse(flagged.settled)
        self.assertEqual(JobRunStatus.RUNNING, flagged.status)


# --------------------------------------------------------------------------
# F1 - THE EXECUTION CLAIM ARBITRATES IN ITS OWN TRANSACTION
# --------------------------------------------------------------------------


class ExecutionClaimArbitrationTests(CancelTestCase):
    """The seam between "no future work" and "no past work"."""

    def test_a_cancellation_between_binding_and_the_claim_stops_the_connector(
        self,
    ) -> None:
        # THE F1 BLOCKER, driven as a real race. The generation is bound and
        # Core is entered; the cancellation commits from INSIDE the window
        # after everything the dispatcher checked and before the execution
        # claim. Only an arbitration in the claim's own transaction can see it.
        run, lease = self.claimed()
        outer = self
        original = self.memory.begin_execution

        def cancelling_begin(*args, **kwargs):
            # The operator's request lands here, in the gap the dispatcher
            # cannot close from outside.
            if not outer.reload(run.id).cancel_requested:
                outer.request(run.id)
            return original(*args, **kwargs)

        self.memory.begin_execution = cancelling_begin
        self.addCleanup(setattr, self.memory, "begin_execution", original)

        dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.at
        )

        # THE CONNECTOR WAS NEVER REACHED.
        self.assertEqual(0, len(self.reader.calls))
        self.assertEqual([AttemptState.PREPARED.value], self.attempt_states())
        self.assertTrue(self.flagged(run.id))

    def test_the_claim_winning_first_leaves_execution_owning_reality(self) -> None:
        # The other side of the same seam: once IN_FLIGHT exists, cancellation
        # is advisory and the effect that happened stands.
        run, lease = self.claimed()
        outer = self
        original = self.memory.begin_execution

        def cancelling_after_claim(*args, **kwargs):
            outcome = original(*args, **kwargs)
            # The claim has committed; the request arrives a moment too late.
            if outcome is ExecutionClaimOutcome.CLAIMED:
                outer.request(run.id)
            return outcome

        self.memory.begin_execution = cancelling_after_claim
        self.addCleanup(setattr, self.memory, "begin_execution", original)

        dispatch_job_run(
            self.core, run.id, WORKER_A, lease.fencing_epoch, clock=lambda: self.at
        )

        # The connector really ran, and the record of it survives.
        self.assertEqual(1, len(self.reader.calls))
        self.assertTrue(self.flagged(run.id))
        self.assertEqual([AttemptState.FINISHED.value], self.attempt_states())

    def test_the_arbitration_refuses_a_flagged_generation_directly(self) -> None:
        run, _dispatch = self.bound()
        self.request(run.id)
        with contextlib.closing(self.raw()) as connection:
            self.assertTrue(
                SQLiteMemoryStore._scheduled_claim_refused(
                    connection, run.session_id
                )
            )

    def test_the_arbitration_refuses_a_settled_or_mismatched_binding(self) -> None:
        run, dispatch = self.bound()
        with contextlib.closing(self.raw()) as connection:
            # A settled generation starts nothing further.
            connection.execute(
                "UPDATE job_runs SET status = 'completed', settled_at = ? "
                "WHERE id = ?",
                (self.after.isoformat(), run.id),
            )
            connection.commit()
        with contextlib.closing(self.raw()) as connection:
            self.assertTrue(
                SQLiteMemoryStore._scheduled_claim_refused(
                    connection, dispatch.session_id
                )
            )
        # And a binding that names another chain than the session holds.
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE job_runs SET status = 'running', settled_at = NULL, "
                "chain_id = 'some-other-chain' WHERE id = ?",
                (run.id,),
            )
            connection.commit()
        with contextlib.closing(self.raw()) as connection:
            self.assertTrue(
                SQLiteMemoryStore._scheduled_claim_refused(
                    connection, dispatch.session_id
                )
            )

    def test_an_ordinary_bound_generation_is_not_refused(self) -> None:
        run, dispatch = self.bound()
        del run
        with contextlib.closing(self.raw()) as connection:
            self.assertFalse(
                SQLiteMemoryStore._scheduled_claim_refused(
                    connection, dispatch.session_id
                )
            )

    def test_interactive_execution_is_untouched(self) -> None:
        # A session with no generation takes exactly the path it always did.
        # This is the regression that keeps every hand-driven task working.
        from jarvis.contracts import SessionState

        session = SessionState(objective="a person's own work", project_id="proj-a")
        self.memory.save_session(session)
        with contextlib.closing(self.raw()) as connection:
            self.assertFalse(
                SQLiteMemoryStore._scheduled_claim_refused(connection, session.id)
            )


# --------------------------------------------------------------------------
# STATE-BY-STATE CANCELLATION
# --------------------------------------------------------------------------


class BoundCancellationTests(CancelTestCase):
    """What cancelling actually does to a generation that is under way."""

    def test_a_generation_that_never_started_is_cancelled_and_settled(self) -> None:
        run, dispatch = self.bound()
        outcome = self.cancel(run.id)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.CANCELLED, stored.status)
        self.assertTrue(stored.cancel_requested)
        self.assertIs(ChainStatus.CANCELLED, self.chain_of(dispatch.session_id).status)
        self.assertEqual(0, len(self.reader.calls))

    def test_a_prepared_attempt_is_cancelled_without_a_connector_call(self) -> None:
        run, _dispatch = self.crashed_run("begin_execution")
        self.assertEqual([AttemptState.PREPARED.value], self.attempt_states())
        self.assertEqual(0, len(self.reader.calls))
        outcome = self.cancel(run.id)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.CANCELLED, self.reload(run.id).status)
        # The connector was never reached, and the inert attempt is untouched.
        self.assertEqual(0, len(self.reader.calls))
        self.assertEqual([AttemptState.PREPARED.value], self.attempt_states())
        task = self.memory.load_task(self.task_ids(run.session_id)[0])
        self.assertIs(TaskStatus.CANCELLED, task.status)

    def test_a_late_execution_claim_cannot_win_after_cancellation(self) -> None:
        # The PREPARED attempt is still there and a stale dispatcher could
        # still try to claim it. The arbitration inside the claim transaction
        # is what refuses, before any billing question is even asked.
        run, _dispatch = self.crashed_run("begin_execution")
        self.cancel(run.id)
        with contextlib.closing(self.raw()) as connection:
            self.assertTrue(
                SQLiteMemoryStore._scheduled_claim_refused(
                    connection, run.session_id
                )
            )
        self.assertEqual(0, len(self.reader.calls))
        self.assertEqual([AttemptState.PREPARED.value], self.attempt_states())

    def test_a_finished_effect_survives_cancellation_of_future_work(self) -> None:
        # The effect happened. Cancelling stops what has not started and says
        # nothing at all about what has.
        run, dispatch = self.crashed_run("advance_chain_step")
        self.assertEqual([AttemptState.FINISHED.value], self.attempt_states())
        self.assertEqual(1, len(self.reader.calls))
        task_id = self.task_ids(dispatch.session_id)[0]
        before = self.memory.load_task(task_id)
        outcome = self.cancel(run.id)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.CANCELLED, self.reload(run.id).status)
        # EVERY TRACE OF THE EFFECT REMAINS.
        self.assertEqual([AttemptState.FINISHED.value], self.attempt_states())
        after = self.memory.load_task(task_id)
        self.assertIs(TaskStatus.COMPLETED, after.status)
        self.assertEqual(before.result_snapshot, after.result_snapshot)
        self.assertEqual(1, len(self.reader.calls))
        self.assertEqual(1, self.table_count("usage_ledger"))

    def test_an_orphaned_in_flight_execution_never_settles_cancelled(self) -> None:
        # THE LINE CANCELLATION MAY NOT CROSS. An execution crossed the
        # boundary and nobody knows what came back; no request to stop can
        # turn that into "nothing happened".
        run = self.in_flight()
        outcome = self.cancel(run.id)
        self.assertEqual(DISPATCH_CANCEL_INTENT, outcome.verdict, outcome.reason)
        self.assertEqual("execution_not_established", outcome.reason)
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertTrue(stored.cancel_requested)
        self.assertFalse(stored.settled)
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.attempt_states())
        # Asking again changes nothing: the flag is sticky and the generation
        # still cannot be settled over an unresolved execution.
        again = self.request(run.id)
        self.assertTrue(again.cancel_requested)
        self.assertFalse(again.settled)
        self.assertEqual(1, len(self.audit("job_run_cancel_requested")))

    def test_the_orphan_gate_still_ends_a_cancelled_in_flight_generation(
        self,
    ) -> None:
        run = self.in_flight()
        self.cancel(run.id)
        row = self.attempt_rows()[0]
        from jarvis.contracts import ORPHAN_MIN_AGE
        from datetime import datetime

        aged = datetime.fromisoformat(row["updated_at"]) + ORPHAN_MIN_AGE + timedelta(
            minutes=1
        )
        self.memory.declare_attempt_orphaned(
            row["session_id"],
            row["task_id"],
            row["id"],
            now=aged,
            actor_label=ACTOR,
            reason=WHY,
        )
        # Past the recovery lease the cancellation itself took, so this
        # recovery may legitimately reacquire the generation.
        later = self.after + LEASE + timedelta(minutes=1)
        self.recover(
            run.id,
            WORKER_C,
            self.reload(run.id).fencing_epoch,
            clock=lambda: later,
        )
        self.assertEqual(
            JobRunStatus.UNKNOWN_OUTCOME, self.reload(run.id).status
        )
        self.memory.abandon_unknown_attempt(
            row["session_id"], row["task_id"], row["id"], AttemptRecovery.ABANDONED
        )
        self.memory.cancel_chain(run.session_id, run.chain_id)
        resolved = self.memory.resolve_unknown_job_run(
            run.id, row["id"], now=self.after, actor_label=ACTOR, reason=WHY
        )
        # FAILED, never CANCELLED: the uncertainty is what ended it.
        self.assertEqual(JobRunStatus.FAILED, resolved.status)
        self.assertEqual(
            ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value, resolved.blocking_code
        )
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.attempt_states())

    def test_cancellation_is_idempotent_and_restart_safe(self) -> None:
        run, _dispatch = self.bound()
        first = self.cancel(run.id)
        self.assertEqual(DISPATCH_SETTLED, first.verdict, first.reason)
        # Running the whole orchestration again changes nothing.
        second = self.cancel(run.id)
        self.assertEqual(DISPATCH_REFUSED, second.verdict)
        self.assertEqual(JobRunStatus.CANCELLED, self.reload(run.id).status)
        self.assertEqual(1, len(self.audit("job_run_cancel_requested")))

    def test_a_crash_after_the_flag_converges_on_a_rerun(self) -> None:
        # Crash window A: the intent is durable, nothing else happened yet.
        run, dispatch = self.bound()
        self.request(run.id)
        self.assertIs(ChainStatus.ACTIVE, self.chain_of(dispatch.session_id).status)
        reopened = SQLiteMemoryStore(self.database)
        outcome = self.cancel(run.id, store=reopened)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.CANCELLED, self.reload(run.id).status)

    def test_a_crash_after_the_chain_converges_on_a_rerun(self) -> None:
        # Crash window B: chain cancelled, task not yet.
        run, dispatch = self.bound()
        self.request(run.id)
        self.memory.cancel_chain(dispatch.session_id, dispatch.chain_id)
        outcome = self.cancel(run.id)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.CANCELLED, self.reload(run.id).status)


class CompleteHistoryCancellationTests(CancelTestCase):
    """A newer clean attempt may never hide an older unresolved one.

    ``cancel_task`` decides whether a task may be written TERMINAL. That is a
    question about EVERY execution the task ever had, not about whichever
    attempt sits on top of the stack: an older IN_FLIGHT or UNKNOWN_OUTCOME
    under a newer FINISHED one is still a connector call nobody concluded, and
    writing CANCELLED over it asserts "nothing happened" about something that
    did.

    Production writes one attempt row per task - ``open_execution_attempt``
    creates a row only when the task has none - so a multi-row history is
    corrupted or migrated state rather than an ordinary shape. It is planted
    directly here for exactly that reason: this rule is what the store falls
    back on when its one-row invariant has already failed, and a defence that
    only holds while nothing has gone wrong is not a defence.
    """

    def planted_history(self, *states):
        """One real task whose attempt history is exactly ``states``.

        The first attempt is the genuine one the run produced, rewritten to
        the requested state; later ones are inserted above it, so ``rowid``
        order really does make the uncertain attempt the OLDER row.
        """
        run, _dispatch = self.crashed_run("begin_execution")
        task_id = self.task_ids(run.session_id)[0]
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT id, session_id, task_id, connector, fingerprint, "
                "created_at, updated_at FROM execution_attempts"
            ).fetchone()
            connection.execute(
                "UPDATE execution_attempts SET state = ? WHERE id = ?",
                (states[0].value, row["id"]),
            )
            for index, state in enumerate(states[1:], start=1):
                connection.execute(
                    "INSERT INTO execution_attempts "
                    "(id, session_id, task_id, connector, fingerprint, state, "
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{row['id']}-{index}",
                        row["session_id"],
                        row["task_id"],
                        row["connector"],
                        row["fingerprint"],
                        state.value,
                        row["created_at"],
                        row["updated_at"],
                    ),
                )
            connection.commit()
        self.assertEqual([state.value for state in states], self.attempt_states())
        return run, task_id

    def test_an_older_in_flight_is_not_hidden_by_a_newer_finished(self) -> None:
        # THE BLOCKER. Top-of-stack says FINISHED, so a newest-row reader would
        # call this task cleanly cancellable. It is not: an execution below it
        # is still in flight and nobody concluded it.
        run, task_id = self.planted_history(
            AttemptState.IN_FLIGHT, AttemptState.FINISHED
        )
        outcome = self.memory.cancel_task(run.session_id, task_id)
        self.assertEqual("cancelling", outcome.verdict)
        stored = self.memory.load_task(task_id)
        self.assertIs(TaskStatus.CANCEL_REQUESTED, stored.status)
        self.assertIsNot(TaskStatus.CANCELLED, stored.status)
        self.assertTrue(stored.cancellation_requested)
        # The history itself is untouched: cancellation concludes nothing.
        self.assertEqual(
            [AttemptState.IN_FLIGHT.value, AttemptState.FINISHED.value],
            self.attempt_states(),
        )

    def test_an_older_unknown_outcome_is_not_hidden_by_a_newer_finished(
        self,
    ) -> None:
        run, task_id = self.planted_history(
            AttemptState.UNKNOWN_OUTCOME, AttemptState.FINISHED
        )
        before = self.memory.load_task(task_id).status
        outcome = self.memory.cancel_task(run.session_id, task_id)
        self.assertEqual("in_doubt", outcome.verdict)
        # REFUSED BEFORE ANY WRITE: the task is exactly as it was.
        stored = self.memory.load_task(task_id)
        self.assertIs(before, stored.status)
        self.assertFalse(stored.cancellation_requested)
        self.assertEqual(
            [AttemptState.UNKNOWN_OUTCOME.value, AttemptState.FINISHED.value],
            self.attempt_states(),
        )

    def test_uncertainty_outranks_a_live_attempt_wherever_it_sits(self) -> None:
        # Both kinds of unresolved history at once: UNKNOWN wins, because its
        # refusal writes nothing at all.
        run, task_id = self.planted_history(
            AttemptState.UNKNOWN_OUTCOME,
            AttemptState.IN_FLIGHT,
            AttemptState.FINISHED,
        )
        self.assertEqual(
            "in_doubt", self.memory.cancel_task(run.session_id, task_id).verdict
        )
        self.assertFalse(self.memory.load_task(task_id).cancellation_requested)

    def test_a_wholly_finished_history_still_cancels_terminally(self) -> None:
        # THE CONTROL. Nothing unresolved anywhere, so the existing semantics
        # are unchanged and the cancellation is final and honest.
        run, task_id = self.planted_history(
            AttemptState.FINISHED, AttemptState.FINISHED
        )
        outcome = self.memory.cancel_task(run.session_id, task_id)
        self.assertEqual("cancelled", outcome.verdict)
        self.assertIs(TaskStatus.CANCELLED, self.memory.load_task(task_id).status)

    def test_a_task_that_never_executed_still_cancels_terminally(self) -> None:
        # The ordinary shape: a real task with no attempt row at all.
        run, _dispatch = self.crashed_run("begin_execution")
        task_id = self.task_ids(run.session_id)[0]
        with contextlib.closing(self.raw()) as connection:
            connection.execute("DELETE FROM execution_attempts")
            connection.commit()
        self.assertEqual([], self.attempt_states())
        outcome = self.memory.cancel_task(run.session_id, task_id)
        self.assertEqual("cancelled", outcome.verdict)
        self.assertIs(TaskStatus.CANCELLED, self.memory.load_task(task_id).status)


class ApprovalCancellationTests(CancelTestCase):
    """A question nobody can act on any more is superseded, never answered."""

    def approval_row(self, task_id: str):
        with contextlib.closing(self.raw()) as connection:
            return connection.execute(
                "SELECT id, request_json, status, resolved_at FROM "
                "pending_approvals WHERE task_id = ?",
                (task_id,),
            ).fetchone()

    def test_cancelling_a_parked_generation_supersedes_its_question(self) -> None:
        run, _dispatch, task_id, request = self.parked()
        self.assertEqual("pending", self.approval_status(task_id))
        before = self.approval_row(task_id)
        outcome = self.cancel(run.id)
        self.assertEqual(DISPATCH_SETTLED, outcome.verdict, outcome.reason)
        self.assertEqual(JobRunStatus.CANCELLED, self.reload(run.id).status)
        # SUPERSEDED, not denied - and in the store's EXISTING vocabulary,
        # the same word a newer request already writes over an older one.
        self.assertEqual("superseded", self.approval_status(task_id))
        after = self.approval_row(task_id)
        # THE REQUEST'S IDENTITY SURVIVES INTACT. Its id, its body and the
        # fingerprint derived from that body are byte-identical; only the
        # status moved, and ``resolved_at`` was stamped.
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(request.id, after["id"])
        self.assertEqual(before["request_json"], after["request_json"])
        self.assertEqual(
            request.scope_fingerprint,
            PermissionRequest.from_dict(
                json.loads(after["request_json"])
            ).scope_fingerprint,
        )
        self.assertIsNone(before["resolved_at"])
        self.assertIsNotNone(after["resolved_at"])
        # No decision was recorded anywhere: supersession is not a verdict.
        self.assertEqual(0, self.table_count("permission_grants"))
        self.assertIsNone(
            self.memory.load_pending_approval(task_id, run.session_id)
        )
        self.assertIs(TaskStatus.CANCELLED, self.memory.load_task(task_id).status)
        # The egress connector was never reached.
        self.assertEqual(0, len(self.planner.calls))

    def test_the_task_and_its_question_move_in_one_transaction(self) -> None:
        # There is NO observable state where the task is cancelled but its
        # question is still pending, or the reverse. Both writes live in the
        # same transaction, so a failure after the supersede discards both.
        run, _dispatch, task_id, _request = self.parked()

        def boom(*args, **kwargs):
            raise RuntimeError("checkpoint failed")

        original = SQLiteMemoryStore.__dict__["_write_checkpoint_and_event"]
        SQLiteMemoryStore._write_checkpoint_and_event = staticmethod(boom)
        try:
            with self.assertRaises(RuntimeError):
                self.memory.cancel_task(run.session_id, task_id)
        finally:
            SQLiteMemoryStore._write_checkpoint_and_event = original
        self.assertEqual("pending", self.approval_status(task_id))
        self.assertIsNot(TaskStatus.CANCELLED, self.memory.load_task(task_id).status)

    def test_an_approval_after_cancellation_cannot_execute(self) -> None:
        run, _dispatch, task_id, request = self.parked()
        self.cancel(run.id)
        # The approver arrives late: there is no pending request to resolve.
        self.assertFalse(
            self.memory.resolve_approval(
                request, PermissionGrant.for_request(request), "approved"
            )
        )
        self.assertEqual(0, len(self.planner.calls))
        self.assertIs(TaskStatus.CANCELLED, self.memory.load_task(task_id).status)

    def test_an_approval_that_wins_first_still_cannot_execute_after_cancel(
        self,
    ) -> None:
        # The approval resolves before the cancellation. The task returns to
        # PENDING, but the F1 arbitration and the chain check are what actually
        # stand between it and the connector.
        run, _dispatch, task_id, request = self.parked()
        self.assertTrue(
            self.memory.resolve_approval(
                request, PermissionGrant.for_request(request), "approved"
            )
        )
        outcome = self.cancel(run.id)
        self.assertIn(
            outcome.verdict, (DISPATCH_SETTLED, DISPATCH_CANCEL_INTENT), outcome.reason
        )
        # THE APPROVER'S OWN VERDICT SURVIVES. The supersede is a CAS on
        # 'pending', so a request that was already resolved is not rewritten:
        # the record still says a person approved it, and the grant they made
        # is still there to be audited.
        self.assertEqual("approved", self.approval_status(task_id))
        self.assertEqual(1, self.table_count("permission_grants"))
        with contextlib.closing(self.raw()) as connection:
            self.assertTrue(
                SQLiteMemoryStore._scheduled_claim_refused(
                    connection, run.session_id
                )
            )
        self.assertEqual(0, len(self.planner.calls))
        del task_id


class FlaggedRecoveryTests(CancelTestCase):
    """Recovery reflects truth; it never treats a stop request as a start."""

    def test_a_flagged_generation_never_enters_core(self) -> None:
        run, _dispatch = self.bound()
        self.request(run.id)
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("cancel_requested", outcome.reason)
        self.assertEqual([], scripted.calls)
        self.assertEqual(0, len(self.reader.calls))
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_flagged_prepared_generation_never_enters_core(self) -> None:
        run, _dispatch = self.crashed_run("begin_execution")
        self.request(run.id)
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("cancel_requested", outcome.reason)
        self.assertEqual([], scripted.calls)
        self.assertEqual(0, len(self.reader.calls))

    def test_uncertainty_still_outranks_a_stop_request(self) -> None:
        # A durable unknown outcome is a fact; a cancellation is a wish about
        # the future. The fact is reflected first.
        run = self.in_flight()
        row = self.attempt_rows()[0]
        self.memory.mark_execution_unknown(row["id"])
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE job_runs SET cancel_requested = 1 WHERE id = ?", (run.id,)
            )
            connection.commit()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_UNKNOWN, outcome.verdict, outcome.reason)
        self.assertEqual([], scripted.calls)
        self.assertEqual(
            JobRunStatus.UNKNOWN_OUTCOME, self.reload(run.id).status
        )


if __name__ == "__main__":
    unittest.main()
