"""Scheduler phase 2D: the minimum FOREGROUND runner.

One object, driven by a person in a terminal, that asks the durable catalogue
what is due, walks that bounded list ONCE per sweep, and hands each occurrence
to the existing phase-2B/2C boundary that already owns it. It is a caller of
authorities, never an authority: every mutation below happens inside
``jarvis.memory``, ``jarvis.dispatch`` or ``jarvis.condition_watch``, under the
same owner token, fencing epoch and live lease those boundaries have always
demanded.

WHAT THIS MODULE IS NOT. There is no background service, no daemon, no worker
pool, no thread, no queue and no distributed scheduling. It owns exactly one
loop, on the caller's own stack, that sweeps and then sleeps for a fixed
interval. Nothing here evaluates a predicate, reaches a connector, opens or
concludes an execution attempt, creates or answers an approval, mints a grant
or authorizes spending.

A SWEEP IS BOUNDED BY CONSTRUCTION. The job catalogue read is bounded by the
store, at most one generation per job is unsettled at a time by the
one-unsettled-generation rule, and each generation is touched at most once per
sweep. A machine that was offline for a year therefore costs exactly what a
machine offline for a minute costs - the coalescing lives in
``materialize_due_job_run`` and this module does not second-guess it.

UNCERTAINTY IS NEVER RETRY PERMISSION. ``UNKNOWN_OUTCOME`` is skipped and
stays skipped, forever, by every sweep: it is the durable record that nobody
could establish what happened, and a loop that came back to it would be a
timer deciding what only evidence can. A generation left ``blocked`` by a
dispatch is likewise not re-dispatched - it stays RUNNING and a person
resolves it. The only thing this module repeats is the SWEEP, and the backoff
between sweeps is a fixed, bounded coordination interval rather than a
per-generation retry.

THE KILL SWITCH IS CHECKED BEFORE NEW CLAIMS. ``execution_enabled`` false
means no occurrence is materialized and no coordination ownership is taken -
the runner stops creating new work rather than creating it and failing later.
Recovering an ALREADY-bound generation is deliberately not gated here: it is
not new work, and Core's own execution barrier is the authority that refuses
to execute while the switch is off.

CTRL+C IS SAFE BECAUSE NOTHING IS CAUGHT UNDER IT. ``KeyboardInterrupt`` is
handled at the loop boundary only. Arriving mid-dispatch it propagates out of
this module untouched, leaving the generation exactly as the fenced boundary
last committed it - RUNNING and unsettled, which is the honest state - because
the one thing a shutdown must never do is write an ending nobody proved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from jarvis.condition_watch import evaluate_condition_watch
from jarvis.contracts import JobError, JobKind, JobRunStatus
from jarvis.dispatch import cancel_job_run, dispatch_job_run, recover_job_run

#: How many jobs one sweep will look at. The store bounds its own listing too;
#: this is the runner's own ceiling on how much work a single foreground pass
#: may take before the operator gets control back.
DEFAULT_JOB_BUDGET = 50

#: The fixed wait between sweeps. It is a COORDINATION interval - how often
#: this process asks the catalogue what changed - and never a retry timer for
#: anything that failed inside a sweep.
DEFAULT_POLL_INTERVAL = timedelta(seconds=5)

#: The lease a fresh claim takes. Inside the approved window, and long enough
#: that an ordinary chain step is not racing its own expiry.
DEFAULT_CLAIM_LEASE = timedelta(minutes=5)

#: What one sweep did to one generation. Reports, not verdicts: the verdict is
#: whatever the boundary that owns the mutation returned.
ACTION_DISPATCHED = "dispatched"
ACTION_EVALUATED = "condition_evaluated"
ACTION_RECOVERED = "recovered"
ACTION_CANCELLED = "cancellation_carried"
ACTION_PARKED = "parked"
ACTION_SKIPPED = "skipped"


def _utc_now() -> datetime:
    """The default clock, isolated so a test can replace exactly this."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class RunnerAction:
    """One generation, one sweep, one report. It carries no authority.

    ``verdict`` is the ``DispatchOutcome`` verdict when a dispatch boundary was
    actually entered, and ``None`` when the runner decided on its own not to
    touch the generation at all.
    """

    job_id: str
    run_id: str | None
    action: str
    reason: str
    verdict: str | None = None


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What ONE bounded pass over the catalogue did.

    ``claims_suspended`` is the kill switch as it was read at the start of the
    sweep, stated rather than implied: an operator reading a sweep that did
    nothing must be able to tell "nothing was due" from "execution is off".
    """

    started_at_utc: str
    jobs_considered: int
    claims_suspended: bool
    actions: tuple[RunnerAction, ...] = ()


class SchedulerRunner:
    """Sweep due and unsettled scheduled work, in the foreground, sequentially.

    Holds a single ``AgentCore``: the store it reads is ``core.memory`` and the
    execution it can never perform itself is ``core.run_chain``, reached only
    through the phase-2B/2C boundaries. One occurrence is finished before the
    next is looked at - there is no concurrency here to reason about.
    """

    def __init__(
        self,
        core: Any,
        *,
        job_budget: int = DEFAULT_JOB_BUDGET,
        lease: timedelta = DEFAULT_CLAIM_LEASE,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        actor_label: str = "scheduler-runner",
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.core = core
        self.job_budget = job_budget
        self.lease = lease
        self.poll_interval = poll_interval
        self.actor_label = actor_label
        self._clock = clock
        self._sleep = sleep

    # -- the loop ---------------------------------------------------------

    def run_once(self) -> SweepReport:
        """ONE bounded pass: what is due, what is stuck, nothing else.

        Every job in the bounded catalogue listing is considered exactly once,
        in the store's own total order, and each contributes at most one
        action. The kill switch is read ONCE, at the top, so a single sweep
        cannot half-observe an operator flipping it mid-pass.
        """
        now = self._clock()
        # The live gate too, so a frozen installation materializes and claims nothing.
        claims_suspended = not self.core.execution_allowed()
        jobs = self.core.memory.list_jobs(self.job_budget)
        actions: list[RunnerAction] = []
        for job in jobs:
            actions.extend(self._sweep_job(job, now, claims_suspended))
        return SweepReport(
            started_at_utc=now.astimezone(timezone.utc).isoformat(),
            jobs_considered=len(jobs),
            claims_suspended=claims_suspended,
            actions=tuple(actions),
        )

    def run_until_stopped(self) -> tuple[SweepReport, ...]:
        """Sweep, wait, repeat, until the operator stops it with Ctrl+C.

        The wait is unconditional and fixed. A sweep that DID something is not
        rewarded with an immediate next sweep, because "something happened"
        includes outcomes like ``blocked`` that must never accelerate into a
        spin - the interval is coordination pacing, not backoff from failure.

        ``KeyboardInterrupt`` is caught HERE and nowhere else. One raised
        inside a dispatch travels through this module without being handled,
        so a shutdown can never turn an unfinished execution into a written
        outcome.
        """
        reports: list[SweepReport] = []
        try:
            while True:
                reports.append(self.run_once())
                self._sleep(self.poll_interval.total_seconds())
        except KeyboardInterrupt:
            # A clean foreground stop. Nothing is written, nothing is rolled
            # back, and whatever the last sweep committed stands on its own.
            pass
        return tuple(reports)

    # -- one job ----------------------------------------------------------

    def _sweep_job(
        self, job: Any, now: datetime, claims_suspended: bool
    ) -> list[RunnerAction]:
        """Materialize this job's due occurrence if any, then act on its run."""
        if not job.readable or not job.enabled or job.archived:
            # A definition that cannot be rebuilt, one an operator switched
            # off, or one they archived. None of the three is the runner's
            # business - including any generation still open underneath, which
            # stays exactly where it is for a person to look at.
            return []

        run = None
        if not claims_suspended:
            # THE KILL SWITCH, before anything is created. A materialized
            # occurrence is durable state that would sit unsettled and block
            # its job, so it is not created while execution is off either.
            try:
                run = self.core.memory.materialize_due_job_run(
                    job.id, job.revision, now=now
                )
            except JobError as exc:
                # An unusable schedule or a definition that moved past the
                # revision we just read. Reported, never guessed at.
                return [
                    RunnerAction(job.id, None, ACTION_SKIPPED, f"not_due_able: {exc}")
                ]

        if run is None:
            # Nothing new was due (or claims are suspended); the job's current
            # generation, if it has one, may still need attention.
            if job.last_run_id is None:
                return []
            run = self.core.memory.load_job_run(job.last_run_id)
        if run is None or run.settled:
            return []
        return [self._sweep_run(job.id, run, now, claims_suspended)]

    def _sweep_run(
        self, job_id: str, run: Any, now: datetime, claims_suspended: bool
    ) -> RunnerAction:
        """Route ONE unsettled generation to the boundary that owns its state."""
        status = run.status
        if status is JobRunStatus.AWAITING_APPROVAL:
            # PARKED, and left parked. A person was asked something and has
            # not answered; no sweep answers it, expires it, resumes past it
            # or takes its lease. The job stays blocked behind the question,
            # which is what a human gate is for.
            return RunnerAction(job_id, run.id, ACTION_PARKED, "awaiting_approval")
        if status is JobRunStatus.UNKNOWN_OUTCOME:
            # NEVER retried and never redispatched. The durable record says
            # nobody could establish what happened; only the operator gates
            # ('jarvis task orphan', 'jarvis job resolve') end it.
            return RunnerAction(job_id, run.id, ACTION_SKIPPED, "unknown_outcome")
        if status is JobRunStatus.RUNNING:
            if run.lease_is_live(now):
                # Somebody is coordinating it right now. An expiry is the only
                # evidence this runner accepts that they are gone.
                return RunnerAction(job_id, run.id, ACTION_SKIPPED, "lease_live")
            return self._recover(job_id, run)
        if status in (JobRunStatus.PENDING, JobRunStatus.CLAIMED):
            if claims_suspended:
                return RunnerAction(job_id, run.id, ACTION_SKIPPED, "execution_disabled")
            return self._claim_and_run(job_id, run, now)
        # Unreachable while the run is unsettled and the vocabulary is closed;
        # skipped rather than assumed if that ever changes.
        return RunnerAction(job_id, run.id, ACTION_SKIPPED, f"not_actionable: {status.value}")

    # -- the two entries into an existing boundary -------------------------

    def _claim_and_run(self, job_id: str, run: Any, now: datetime) -> RunnerAction:
        """PENDING, or CLAIMED with a dead lease: claim it, then run it once.

        ``claim_job_run`` is the sole authority on whether either is takeable -
        it re-reads the row inside its own immediate transaction, refuses a
        live lease, refuses a recorded cancellation and fences the winner - so
        nothing is pre-judged here and a lost race is an ordinary answer.
        """
        owner_token = f"scheduler-{uuid4()}"
        try:
            lease = self.core.memory.claim_job_run(
                run.id, owner_token, now=now, lease=self.lease
            )
        except JobError as exc:
            return RunnerAction(job_id, run.id, ACTION_SKIPPED, f"not_claimable: {exc}")

        if run.spec.kind is JobKind.CONDITION_WATCH:
            # Phase 2C owns the predicate: observation through Core's own
            # root-bound reader, evaluation outside every transaction, then
            # SKIPPED or a fenced on_true bind. The runner reads none of it.
            outcome = evaluate_condition_watch(
                self.core, run.id, owner_token, lease.fencing_epoch, clock=self._clock
            )
            return RunnerAction(
                job_id, run.id, ACTION_EVALUATED, outcome.reason, outcome.verdict
            )
        outcome = dispatch_job_run(
            self.core, run.id, owner_token, lease.fencing_epoch, clock=self._clock
        )
        return RunnerAction(
            job_id, run.id, ACTION_DISPATCHED, outcome.reason, outcome.verdict
        )

    def _recover(self, job_id: str, run: Any) -> RunnerAction:
        """A bound generation whose coordinator is gone. Resume, or stop it.

        The order matters: a recorded cancellation is checked FIRST, because
        ``recover_job_run`` deliberately refuses to resume a flagged
        generation and would leave it blocked forever. Carrying the intent is
        phase 2B2b2's orchestration, and it settles nothing an execution has
        not already established.
        """
        owner_token = f"scheduler-{uuid4()}"
        if run.cancel_requested:
            outcome = cancel_job_run(
                self.core.memory,
                run.id,
                owner_token,
                actor_label=self.actor_label,
                reason="carrying a recorded cancellation for an expired lease",
                clock=self._clock,
            )
            return RunnerAction(
                job_id, run.id, ACTION_CANCELLED, outcome.reason, outcome.verdict
            )
        outcome = recover_job_run(
            self.core,
            run.id,
            owner_token,
            run.fencing_epoch,
            actor_label=self.actor_label,
            reason="the previous coordinator lease expired",
            clock=self._clock,
        )
        return RunnerAction(
            job_id, run.id, ACTION_RECOVERED, outcome.reason, outcome.verdict
        )
