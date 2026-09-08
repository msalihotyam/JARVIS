"""Scheduler phase 2B1: the ONE-SHOT bridge from a claimed generation to Core.

One callable, called by somebody who already holds a fenced lease, that binds a
generation to a fresh AgentCore session, hands that session to
``AgentCore.run_chain`` exactly once, and reflects what authoritative storage
says afterwards back into the generation's lifecycle.

WHAT THIS MODULE IS NOT. It owns no thread, no timer and no loop; it does not
poll, sleep, wake on a schedule or scan for due work. It evaluates no predicate,
reaches no connector, opens, advances or concludes no execution attempt, creates
and resolves no approval, mints no grant and authorizes no spending. Every one
of those belongs to ``AgentCore`` and to the H2 journal, unchanged, and this
module reaches them only by asking Core to run and then READING what Core wrote.

THE RESULT IS READ, NEVER ASSUMED. ``AgentRun.status`` is a report about the
last step Core touched; it is not the generation's outcome and is never mapped
straight onto one. What settles a generation is the DURABLE chain status plus
the absence of any attempt still in doubt, and an explicitly unknown execution
outcome outranks every other reading of the same run.

FAIL CLOSED IS THE DEFAULT. Every path that is not positively proven safe leaves
the generation RUNNING and unsettled: an unclassified exception, a binding that
no longer matches, missing or malformed authoritative state, a lease that
expired while Core was working, a chain waiting on a human for anything other
than an approval Core actually persisted. A blocked generation blocks its job,
which is the intended outcome - a stuck occurrence is a thing a person resolves,
not a thing a timer retries. Nothing here ever hands back retry permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from jarvis.contracts import (
    AttemptState,
    ChainStatus,
    ChainTaskMode,
    ErrorCode,
    JobError,
    JobKind,
    JobRun,
    JobRunStatus,
    RunStatus,
    TaskStatus,
    chain_task_refusal,
)

#: Attempt states that mean the connector boundary was crossed and nobody has
#: established what happened on the other side.
_IN_DOUBT_ATTEMPT_STATES = frozenset(
    {AttemptState.IN_FLIGHT, AttemptState.UNKNOWN_OUTCOME}
)

#: The verdicts this boundary returns. ``blocked`` is not a failure of the
#: dispatcher - it is the deliberate answer whenever the safe reflection cannot
#: be proved, and it always means "left RUNNING and unsettled, untouched".
DISPATCH_REFUSED = "refused"
DISPATCH_SETTLED = "settled"
DISPATCH_PARKED = "parked"
DISPATCH_UNKNOWN = "unknown"
DISPATCH_BLOCKED = "blocked"


def _utc_now() -> datetime:
    """The default clock, isolated so a test can replace exactly this."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What one dispatch did, as a report. It carries no authority.

    ``verdict`` says which of the five things happened; ``reason`` is a short
    machine-stable tag for why. ``run`` is the generation as storage held it
    when the dispatcher stopped touching it - so a ``blocked`` outcome carries
    the still-RUNNING row, which is exactly the evidence a human needs.
    """

    verdict: str
    reason: str
    run: JobRun | None = None
    session_id: str | None = None
    chain_id: str | None = None
    core_verdict: str | None = None

    @property
    def settled(self) -> bool:
        return self.verdict == DISPATCH_SETTLED


def _routed_tasks(store: Any, run: JobRun, chain: Any) -> list | None:
    """Every task this chain really routes, re-proved - or ``None``.

    ROUTING IS A CLAIM, NEVER A PROOF. ``step_task_ids`` says which task was
    created for which step so orchestration can find it again; it does not say
    the task exists, that it belongs to this session, or that it is still this
    chain's task. A chain carrying invented ids would otherwise let a
    generation settle on an execution history that never happened.

    Each named id is therefore checked against authoritative storage with the
    house's own ownership contract - the row must exist, must belong to the
    bound session, and must still satisfy ``chain_task_refusal``, which proves
    the binding, the CURRENT declaration digest, the step index, the objective,
    the capability, the scope and the project together. A single failure
    returns ``None`` and every caller treats that as "leave it RUNNING".

    Nothing here writes, and nothing repairs a row it cannot read.
    """
    routed: list = []
    for index, task_id in enumerate(chain.step_task_ids):
        if not task_id:
            continue
        task = store.load_task(task_id)
        if task is None:
            return None
        if store.task_session_id(task_id) != run.session_id:
            return None
        if chain_task_refusal(task, chain, index, mode=ChainTaskMode.STORED) is not None:
            return None
        routed.append((index, task))
    return routed


def _execution_established(store: Any, session_id: str, routed: list) -> bool:
    """Whether every routed task's execution reality is known AND is its own.

    Two independent things have to hold for an attempt row to count as
    evidence about a task, and both are checked here.

    IT MUST BELONG TO THIS EXECUTION CONTEXT. ``execution_attempts`` is keyed
    by task id, but the row itself carries its own ``task_id`` and
    ``session_id``, and those are the authoritative statement of what the
    attempt was for. A row whose identity names a different task or a
    different session is not this task's history, so reading it as "finished,
    nothing in doubt" would be clearing a task on somebody else's evidence.
    Malformed identity - anything that is not an exact non-empty string, or a
    state that is not an exact ``AttemptState`` - is refused for the same
    reason: it cannot be proved to be about this task at all.

    AND THE COMPLETE HISTORY MUST BE CLEAN. Not the newest row: there is no
    one-row-per-task rule, so an unresolved UNKNOWN_OUTCOME or IN_FLIGHT can
    sit under a later clean FINISHED, and reading only the top would report a
    settled effect for a connector call nobody ever resolved. Every attempt of
    every routed task is examined, and ANY in doubt blocks the generation.

    AND ZERO HISTORY IS NOT A FREE PASS. Whether no attempt is legitimate
    depends on what the task's own status says it did, and Core leaves exactly
    one authoritative statement about that: ``TaskStatus.COMPLETED`` is
    reachable through a single path, the PERSIST stage, which is reached only
    after EXECUTE - and EXECUTE durably opens an attempt BEFORE the connector
    boundary is crossed. A completed task therefore always left H2 evidence
    behind, so finding none means the evidence is missing or was misassigned to
    another task, not that none was ever required. That is refused.

    A task that ended FAILED, CANCELLED or REJECTED may legitimately have no
    attempt at all - it can stop at an approval, a gate or a cancellation long
    before any connector is reached - so no blanket "every task must have an
    attempt" rule is imposed. The rule is exactly as wide as what Core
    guarantees and no wider.

    Asked of the H2 journal, which owns the answer. Nothing here writes to it.
    """
    for _index, task in routed:
        try:
            attempts = store.execution_attempts(task.id)
        except Exception:  # noqa: BLE001 - an unreadable journal is not a clearance
            return False
        if attempts is None:
            return False
        if not attempts and task.status is TaskStatus.COMPLETED:
            # Completed means it executed, and executing means an attempt was
            # opened first. No rows is a visibility hole - a reassigned or lost
            # row - and absence of evidence is never evidence of absence.
            return False
        for attempt in attempts:
            state = getattr(attempt, "state", None)
            if type(state) is not AttemptState:
                return False
            owner_task = getattr(attempt, "task_id", None)
            owner_session = getattr(attempt, "session_id", None)
            if type(owner_task) is not str or not owner_task:
                return False
            if type(owner_session) is not str or not owner_session:
                return False
            if owner_task != task.id or owner_session != session_id:
                return False
            if state in _IN_DOUBT_ATTEMPT_STATES:
                return False
    return True


def _lifecycle_coherent(chain: Any, routed: list) -> bool:
    """Whether the routed task states could really have produced this status.

    MIRRORS ``advance_chain_step`` AND ``cancel_chain``, which are the only
    writers of a terminal chain status, rather than inventing a scheduler
    reading of its own. What each one requires:

    * the cursor only ever moves forward off a COMPLETED task, so every step
      BEFORE the cursor must be completed whatever the final status is;
    * ``COMPLETED`` is written only at the last step, off a COMPLETED task,
      with every step created - so all of that must hold;
    * ``FAILED`` is written only when the task AT THE CURSOR is FAILED. A
      chain reporting failure over a set of completed tasks did not come from
      that transition, and settling it would record a failure nothing
      produced;
    * ``CANCELLED`` has TWO legitimate producers - the cursor task being
      CANCELLED or REJECTED, and an operator ``cancel_chain`` that preserves
      the cursor and every binding exactly as they were. The second accepts a
      cursor task in ANY state, including one an executing owner finalizes
      afterwards, so no cursor-state rule is imposed here. Requiring every
      task to be cancelled would refuse the ordinary case of a cancellation
      that arrives after earlier steps legitimately completed.
    """
    by_index = dict(routed)
    cursor = chain.cursor
    if type(cursor) is not int:
        return False
    # Universal: the cursor advanced off completed steps, so they are completed.
    for index, task in routed:
        if index < cursor and task.status is not TaskStatus.COMPLETED:
            return False
    if chain.status is ChainStatus.COMPLETED:
        if cursor != len(chain.steps) - 1:
            return False
        if len(routed) != len(chain.steps):
            return False
        return all(task.status is TaskStatus.COMPLETED for _index, task in routed)
    if chain.status is ChainStatus.FAILED:
        task = by_index.get(cursor)
        return task is not None and task.status is TaskStatus.FAILED
    if chain.status is ChainStatus.CANCELLED:
        # Both canonical producers are admissible; the prior-step rule above is
        # the whole of what cancellation can be held to.
        return True
    return False


def _terminal_evidence(store: Any, run: JobRun, chain: Any) -> bool:
    """Whether this chain's claimed terminal outcome is safe to record.

    Three independent proofs, all required, and all read-only.

    ROUTING must resolve: every id the chain names is a real, session-owned,
    still-bound task. EXECUTION must be established: every attempt under those
    tasks is that task's own, and none of them is in doubt. And the task
    LIFECYCLES must be coherent with the specific status claimed, judged by the
    canonical transition that is the only thing able to write it.
    """
    routed = _routed_tasks(store, run, chain)
    if routed is None:
        return False
    if not _execution_established(store, run.session_id, routed):
        return False
    return _lifecycle_coherent(chain, routed)


def _approval_task(store: Any, run: JobRun, chain: Any, agent_run: Any) -> Any:
    """The task this park would really be waiting on, or ``None``.

    "There is a pending approval somewhere in this session" is not the
    question. A scheduled session can hold approvals for tasks that have
    nothing to do with the chain's current step, and parking a generation on
    one of those would block the job behind an answer that will never move it.

    So the chain of custody is proved end to end: the chain is still ACTIVE,
    the cursor names a routed task, Core handed back THAT task, the row exists,
    it belongs to the bound session, it is still this chain's task for this
    exact index, its own lifecycle says it is waiting for an approval, and the
    persisted request names that same task and session.
    """
    if chain.status is not ChainStatus.ACTIVE:
        return None
    index = chain.cursor
    if not isinstance(index, int) or not 0 <= index < len(chain.step_task_ids):
        return None
    task_id = chain.step_task_ids[index]
    if not task_id:
        return None
    # Core must have returned the very task the cursor routes to.
    if agent_run is None or agent_run.task is None or agent_run.task.id != task_id:
        return None
    task = store.load_task(task_id)
    if task is None:
        return None
    if store.task_session_id(task_id) != run.session_id:
        return None
    if chain_task_refusal(task, chain, index, mode=ChainTaskMode.STORED) is not None:
        return None
    if task.status is not TaskStatus.AWAITING_APPROVAL:
        return None
    # THE EXACT REQUEST, not merely one for this task. Two independent
    # identities have to agree between what Core handed back and what storage
    # holds, because they answer different questions.
    #
    # ``fingerprint`` is the contract's own canonical statement of what a
    # request MEANS - it binds the task, the scope fingerprint, the exact
    # egress attestation and the approval display, so any change to the
    # outbound digest, the gate state, the resource boundary, the tool grants,
    # the reason or the preview changes it. It is compared through the model's
    # own implementation rather than by re-listing its fields here, so a field
    # added to it later is covered without this module being edited.
    #
    # ``id`` is the ISSUANCE identity, minted per request and deliberately
    # never derived from the fingerprint, so a re-issue is a different request
    # even when it would authorize exactly the same thing. Neither identity
    # implies the other, so both are required.
    issued = getattr(agent_run, "permission_request", None)
    if issued is None:
        return None
    request = store.load_pending_approval(task_id, run.session_id)
    if request is None:
        return None
    for candidate in (request, issued):
        if candidate.task_id != task_id or candidate.session_id != run.session_id:
            return None
    if request.id != issued.id:
        return None
    if request.fingerprint != issued.fingerprint:
        return None
    return task


def _bound_state(store: Any, run: JobRun) -> tuple[Any, Any] | None:
    """The session and chain this generation is bound to, or ``None``.

    Every identity is re-proved against the generation's own immutable
    snapshot, because a session id alone proves nothing: the chain inside it
    could have been replaced, the session could have drifted to another
    project, or the row could simply be gone. A single mismatch returns
    ``None``, and every caller treats that as "leave it RUNNING".
    """
    if run.session_id is None or run.chain_id is None:
        return None
    session = store.load_session(run.session_id)
    if session is None or session.chain is None:
        return None
    chain = session.chain
    if chain.id != run.chain_id:
        return None
    if session.project_id != run.spec.project_id:
        return None
    # The DECLARATION itself, not a digest of it: the chain must still be the
    # exact ordered steps the snapshot froze when the generation was recorded.
    if tuple(chain.steps) != tuple(run.spec.steps):
        return None
    return session, chain


def _blocked(reason: str, run: JobRun | None, dispatch: Any, verdict: Any) -> DispatchOutcome:
    return DispatchOutcome(
        verdict=DISPATCH_BLOCKED,
        reason=reason,
        run=run,
        session_id=None if dispatch is None else dispatch.session_id,
        chain_id=None if dispatch is None else dispatch.chain_id,
        core_verdict=verdict,
    )


def dispatch_job_run(
    core: Any,
    run_id: str,
    owner_token: str,
    fencing_epoch: int,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> DispatchOutcome:
    """Bind, run once, and reflect. The whole phase-2B1 boundary.

    The caller must already hold a live fenced lease on a CLAIMED generation;
    this function never claims, never renews and never takes one over.

    The ORDER is the safety property, so it is fixed:

    1. refuse a condition watch, which this phase does not evaluate;
    2. bind a pristine session and chain and move CLAIMED -> RUNNING, in one
       storage transaction that commits before anything is executed;
    3. re-prove the committed binding against the immutable snapshot;
    4. call ``AgentCore.run_chain`` ONCE, outside every transaction;
    5. sample a FRESH instant the moment it returns or raises;
    6. re-read authoritative storage and reflect at most one fenced mutation.

    Step 5 is load-bearing and easy to get wrong. Core can take minutes; a
    timestamp captured before dispatch would judge the lease against an instant
    that has already passed and could settle a generation whose lease expired
    mid-run and whose successor may legitimately own it. The clock is sampled
    after, every time, including on the exception path.

    RUNNING IS DURABLE BEFORE CORE IS ENTERED. If the process dies during step 4
    the generation stays RUNNING, bound and blocked. It is not reclaimed, not
    resumed and not retried here - phase 2B2 owns recovery, and until then a
    person decides.
    """
    store = core.memory
    run = store.load_job_run(run_id)
    if run is None:
        return DispatchOutcome(DISPATCH_REFUSED, "unknown_generation")
    if run.spec.kind is JobKind.CONDITION_WATCH:
        # Refused before anything is bound. A watch declares what it would do
        # IF its predicate held; running that unconditionally is not a
        # degraded version of evaluating it, it is a different act.
        return DispatchOutcome(DISPATCH_REFUSED, "condition_watch", run=run)

    try:
        dispatch = store.bind_job_run_dispatch(
            run_id, owner_token, fencing_epoch, now=clock()
        )
    except JobError as exc:
        return DispatchOutcome(
            DISPATCH_REFUSED, f"not_bindable: {exc}", run=store.load_job_run(run_id)
        )

    # The binding is committed. From here the generation is durably RUNNING and
    # every exit leaves it that way unless a reflection is positively proved.
    bound_run = store.load_job_run(run_id)
    if bound_run is None or bound_run.status is not JobRunStatus.RUNNING:
        return _blocked("binding_unreadable", bound_run, dispatch, None)
    if (
        bound_run.session_id != dispatch.session_id
        or bound_run.chain_id != dispatch.chain_id
    ):
        return _blocked("binding_mismatch", bound_run, dispatch, None)
    if _bound_state(store, bound_run) is None:
        return _blocked("bound_state_mismatch", bound_run, dispatch, None)
    if (
        bound_run.owner_token != owner_token
        or bound_run.fencing_epoch != fencing_epoch
        or not bound_run.lease_is_live(clock())
    ):
        return _blocked("lease_lost_before_dispatch", bound_run, dispatch, None)

    # THE ONE CALL. Outside every transaction, and the only thing this module
    # asks Core to do. Core owns approval, billing, egress, the connector and
    # the attempt journal on the other side of it, exactly as it does for an
    # interactive run.
    try:
        agent_run, core_verdict = core.run_chain(dispatch.session_id)
    except Exception:  # noqa: BLE001 - an unclassified failure is not a verdict
        # THE FRESH INSTANT IS SAMPLED HERE TOO. The contract is about ORDER,
        # and it does not have an exception-shaped hole in it: whatever a
        # caller's clock does - advance, log, expire a lease - must happen
        # after the throw is observed on this path exactly as it does after a
        # normal return, so the two paths cannot drift apart.
        #
        # Nothing is written with it. An exception says the driver stopped,
        # never that the work did not happen, so there is no outcome that
        # could honestly be recorded: no settlement, no unknown, no retry
        # permission. The generation stays RUNNING and blocked.
        clock()
        return _blocked("run_chain_raised", store.load_job_run(run_id), dispatch, None)

    # FRESH NOW, sampled only now that Core has returned.
    settled_at = clock()

    after = store.load_job_run(run_id)
    if after is None or after.status is not JobRunStatus.RUNNING:
        return _blocked("generation_moved", after, dispatch, core_verdict)
    if (
        after.session_id != dispatch.session_id
        or after.chain_id != dispatch.chain_id
        or after.owner_token != owner_token
        or after.fencing_epoch != fencing_epoch
    ):
        return _blocked("binding_or_owner_changed", after, dispatch, core_verdict)
    if not after.lease_is_live(settled_at):
        # The lease died while Core worked. Another worker may legitimately own
        # this generation by now, so this one writes NOTHING.
        return _blocked("lease_expired", after, dispatch, core_verdict)
    bound = _bound_state(store, after)
    if bound is None:
        return _blocked("bound_state_mismatch", after, dispatch, core_verdict)
    _session, chain = bound

    def _reflect(call: Callable[[], JobRun], verdict: str, reason: str) -> DispatchOutcome:
        try:
            reflected = call()
        except JobError as exc:
            # The fenced storage boundary refused. It is the authority on that,
            # and its refusal leaves the generation exactly as it was.
            return _blocked(f"refused: {exc}", store.load_job_run(run_id), dispatch, core_verdict)
        return DispatchOutcome(
            verdict=verdict,
            reason=reason,
            run=reflected,
            session_id=dispatch.session_id,
            chain_id=dispatch.chain_id,
            core_verdict=core_verdict,
        )

    # (E) EXPLICIT UNKNOWN OUTRANKS EVERYTHING. Core says an execution attempt
    # is in doubt; that is a stronger statement than any chain status sitting
    # above it, and mapping it to FAILED would turn "we cannot know" into a
    # clean ending the next generation could start on top of.
    error = agent_run.error if agent_run is not None else None
    if error is not None and error.code == ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value:
        return _reflect(
            lambda: store.mark_job_run_unknown(
                run_id, owner_token, fencing_epoch, now=settled_at
            ),
            DISPATCH_UNKNOWN,
            "execution_unknown_outcome",
        )

    # (D) AWAITING APPROVAL. Three independent agreements required: Core's
    # verdict, the run it handed back, and an approval row Core has ALREADY
    # persisted. The scheduler creates none of it and resolves none of it.
    if core_verdict == "awaiting_approval":
        if agent_run is None or agent_run.status is not RunStatus.AWAITING_APPROVAL:
            return _blocked("approval_disagreement", after, dispatch, core_verdict)
        if _approval_task(store, after, chain, agent_run) is None:
            # Either no durable approval, or one that does not belong to this
            # chain's CURRENT step. Parking on either would block the job
            # behind an answer that cannot move it.
            return _blocked("approval_not_bound_to_step", after, dispatch, core_verdict)
        return _reflect(
            lambda: store.await_job_run_approval(
                run_id, owner_token, fencing_epoch, now=settled_at
            ),
            DISPATCH_PARKED,
            "awaiting_approval",
        )

    # (A/B/C) The DURABLE chain status, never the run object's status - and
    # never over an attempt still in doubt.
    if chain.status in (
        ChainStatus.COMPLETED,
        ChainStatus.FAILED,
        ChainStatus.CANCELLED,
    ):
        if not _terminal_evidence(store, after, chain):
            # Routing that does not resolve, a task that is not this chain's,
            # a lifecycle that disagrees with the claimed outcome, or an
            # attempt still in doubt. Missing evidence is never a clearance.
            return _blocked("terminal_evidence_missing", after, dispatch, core_verdict)
        outcome = {
            ChainStatus.COMPLETED: JobRunStatus.COMPLETED,
            ChainStatus.FAILED: JobRunStatus.FAILED,
            ChainStatus.CANCELLED: JobRunStatus.CANCELLED,
        }[chain.status]
        blocking = None
        if outcome is JobRunStatus.FAILED and error is not None:
            blocking = error.code
        return _reflect(
            lambda: store.settle_job_run(
                run_id,
                outcome,
                owner_token,
                fencing_epoch,
                now=settled_at,
                blocking_code=blocking,
            ),
            DISPATCH_SETTLED,
            f"chain_{chain.status.value}",
        )

    # Everything else - an ACTIVE chain that stopped, a clarification, a cost
    # override, a cancellation still in flight, INVALID, a conflict, a refusal -
    # is left RUNNING on purpose. None of them is an outcome, and inventing one
    # is how a scheduler manufactures a second attempt.
    return _blocked(f"unmapped: {core_verdict}", after, dispatch, core_verdict)
