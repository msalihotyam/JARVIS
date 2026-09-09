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

PHASE 2B2a ADDS RECOVERY, AND RECOVERY IS NOT A RETRY. ``recover_job_run``
takes coordination ownership of a generation that is ALREADY bound - a worker
died holding it, or it was parked for a person who has since been asked - and
hands Core back THE SAME session it was always bound to. It creates no session,
replaces no chain, and gives one occurrence no second anything. What makes that
safe is not this module: it is that Core's own re-entry is non-replaying, and
that this module refuses to enter Core at all while any execution attempt under
the chain is still in doubt. An in-flight or unknown attempt is where recovery
STOPS - the generation stays blocked for a person, exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from jarvis.contracts import (
    AttemptState,
    ChainStatus,
    ChainTaskMode,
    ErrorCode,
    HistoryFinding,
    JobError,
    JobKind,
    JobRun,
    JobRunStatus,
    RunStatus,
    SessionState,
    TaskStatus,
    chain_lifecycle_coherent,
    chain_task_refusal,
    execution_history_verdict,
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
#: A cancellation was recorded but the generation is not finished yet - a live
#: owner still has to conclude it, or a person still has to resolve an
#: uncertain execution. It is NOT a failure and NOT a settlement: the intent is
#: durable and somebody else's authority owns the rest.
DISPATCH_CANCEL_INTENT = "cancel_intent"

#: The default recovery lease. Long enough that an ordinary chain step is not
#: racing its own expiry, short enough that a worker which dies again does not
#: hold the generation for long. A caller states its own; this is only what it
#: gets for not stating one, and the storage boundary still refuses anything
#: outside the approved window.
DEFAULT_RECOVERY_LEASE = timedelta(minutes=5)


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


def _attempt_history(store: Any, routed: list) -> dict | None:
    """The COMPLETE attempt history of every routed task, or ``None``.

    Read through the store's own accessor, which returns every row for a task
    in creation order - never the newest one. An unreadable journal yields
    ``None``, and every caller treats that exactly as it treats doubt: as a
    refusal, because a history that cannot be read is not a clean one.
    """
    history: dict = {}
    for _index, task in routed:
        try:
            attempts = store.execution_attempts(task.id)
        except Exception:  # noqa: BLE001 - an unreadable journal is not a clearance
            return None
        if attempts is None:
            return None
        history[task.id] = attempts
    return history


def _execution_established(store: Any, session_id: str, routed: list) -> bool:
    """Whether every routed task's execution reality is known AND is its own.

    Phase 2B2b1 moved the reading itself into ``execution_history_verdict``,
    which storage also runs inside its own write transactions. This is the
    same question it always was - "is there anything in doubt anywhere under
    this chain, and is every row provably about the task it was filed under" -
    asked of the one shared definition instead of a second local copy.

    The cursor is deliberately NOT supplied. "Established" is a
    cursor-independent question: ANY attempt in doubt, wherever it sits,
    refuses, and passing no cursor is what makes every unresolved outcome read
    as ``NON_CURSOR_UNKNOWN`` rather than as the one reflectable shape. Only
    recovery asks the cursor-aware version, and only to reflect uncertainty -
    never to settle or resume on it.
    """
    history = _attempt_history(store, routed)
    if history is None:
        return False
    return execution_history_verdict(session_id, routed, None, history).established


def _lifecycle_coherent(chain: Any, routed: list) -> bool:
    """Whether the routed task states could really have produced this status.

    The rule itself is ``chain_lifecycle_coherent``, shared with storage so
    that settling a generation here and terminalizing one there cannot come to
    disagree about which chain shapes a canonical writer could have produced.
    """
    return chain_lifecycle_coherent(chain, routed)


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

    THE DOCUMENT MUST BE THE SESSION IT WAS ASKED FOR. Storage is asked for the
    session bound to this generation and answers with a DOCUMENT that carries
    its own identity; those are two different facts, and nothing outside the
    ordinary write path forces them to agree. Core then works from the DECODED
    object, so a document stored under this generation's session but claiming
    to be another one would send every task, attempt and connector call into a
    session this generation was never bound to - while the generation itself
    still read as bound here, leaving the executed work invisible from it.
    The decoded identity is therefore proved against the binding BEFORE the
    project, the chain, the declaration or any routing derived from it is
    trusted, and a disagreement is refused rather than followed: this never
    loads the identity the document names, never rewrites either identity, and
    never substitutes a session of its own.
    """
    if run.session_id is None or run.chain_id is None:
        return None
    session = store.load_session(run.session_id)
    if type(session) is not SessionState:
        return None
    if session.id != run.session_id:
        return None
    if session.chain is None:
        return None
    chain = session.chain
    if chain.id != run.chain_id:
        return None
    if session.project_id != run.spec.project_id:
        return None
    # The DECLARATION itself, not a digest of it: the chain must still be the
    # exact ordered steps the snapshot froze when the generation was recorded.
    # A ``CONDITION_WATCH`` was bound from its ``on_true`` declaration, never
    # from ``steps`` - a watch's ``steps`` is always empty - so this asks the
    # same question of the field that generation's kind actually declares.
    declared = (
        run.spec.on_true if run.spec.kind is JobKind.CONDITION_WATCH else run.spec.steps
    )
    if tuple(chain.steps) != tuple(declared):
        return None
    return session, chain


def _cancel_blocked(reason: str, run: JobRun | None) -> DispatchOutcome:
    """A cancellation that recorded its intent but could carry no further."""
    return _blocked(reason, run, run, None)


def _recovery_blocked(
    reason: str, run: JobRun | None, verdict: Any = None
) -> DispatchOutcome:
    """A blocked recovery, reported against the generation's OWN binding.

    A first dispatch reports the session and chain from the receipt storage
    handed it. A recovery has no receipt - it never created either identity -
    so the generation itself is the only honest source for them.
    """
    return _blocked(reason, run, run, verdict)


def _blocked(reason: str, run: JobRun | None, dispatch: Any, verdict: Any) -> DispatchOutcome:
    return DispatchOutcome(
        verdict=DISPATCH_BLOCKED,
        reason=reason,
        run=run,
        session_id=None if dispatch is None else dispatch.session_id,
        chain_id=None if dispatch is None else dispatch.chain_id,
        core_verdict=verdict,
    )


def _reflect_outcome(
    store: Any,
    run_id: str,
    owner_token: str,
    fencing_epoch: int,
    *,
    binding: Any,
    run: JobRun,
    chain: Any,
    agent_run: Any,
    core_verdict: Any,
    settled_at: datetime,
) -> DispatchOutcome:
    """Read authoritative storage and perform AT MOST ONE fenced mutation.

    The whole of what a generation's lifecycle may be moved to on the strength
    of a completed Core call, and deliberately ONE implementation of it. A
    first dispatch and a recovery differ in how they came to own the run and in
    nothing at all about how its outcome may be read, so a second copy of this
    would be a second place for the reading to drift - which is exactly the
    argument that gave the store one ``_fenced_transition`` instead of two.

    ``binding`` is whatever carries the session and chain identity this call
    ran under - the dispatch receipt on the first path, the recovered
    generation itself on the second. Every mutation below still proves the
    owner, the epoch and a live lease through the fenced storage boundary; this
    function holds no authority of its own and writes nothing directly.
    """
    def _reflect(call: Callable[[], JobRun], verdict: str, reason: str) -> DispatchOutcome:
        try:
            reflected = call()
        except JobError as exc:
            # The fenced storage boundary refused. It is the authority on that,
            # and its refusal leaves the generation exactly as it was.
            return _blocked(f"refused: {exc}", store.load_job_run(run_id), binding, core_verdict)
        return DispatchOutcome(
            verdict=verdict,
            reason=reason,
            run=reflected,
            session_id=binding.session_id,
            chain_id=binding.chain_id,
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
            return _blocked("approval_disagreement", run, binding, core_verdict)
        if _approval_task(store, run, chain, agent_run) is None:
            # Either no durable approval, or one that does not belong to this
            # chain's CURRENT step. Parking on either would block the job
            # behind an answer that cannot move it.
            return _blocked("approval_not_bound_to_step", run, binding, core_verdict)
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
        if not _terminal_evidence(store, run, chain):
            # Routing that does not resolve, a task that is not this chain's,
            # a lifecycle that disagrees with the claimed outcome, or an
            # attempt still in doubt. Missing evidence is never a clearance.
            return _blocked("terminal_evidence_missing", run, binding, core_verdict)
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
    return _blocked(f"unmapped: {core_verdict}", run, binding, core_verdict)


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
    return _run_bound_dispatch(core, run_id, owner_token, fencing_epoch, dispatch, clock)


def dispatch_condition_watch_true(
    core: Any,
    run_id: str,
    owner_token: str,
    fencing_epoch: int,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> DispatchOutcome:
    """Bind a condition watch's TRUE declaration, run once, and reflect.

    The phase-2C boundary, and the exact mirror of ``dispatch_job_run``: same
    caller contract (a live fenced lease on a CLAIMED generation), same
    binding-then-run-then-reflect order, same fail-closed tail - shared
    outright as ``_run_bound_dispatch``, so a first ordinary dispatch and a
    watch's TRUE dispatch cannot drift apart in anything but what they bind.

    The one difference is the binding source: ``bind_condition_true_dispatch``
    in place of ``bind_job_run_dispatch``, which binds ``JobSpec.on_true``
    instead of ``JobSpec.steps`` and persists ``condition_result = "true"`` in
    the same fenced transition.

    THIS FUNCTION EVALUATES NOTHING. It must be called only after some other
    caller has already asked the watch's predicate the question, over a
    hardened observation this module never takes, and found it TRUE. A caller
    that has not done that has no business calling this - binding is the
    entire authority a TRUE answer ever carries, and it is spent exactly once,
    because the underlying binding is immutable regardless of how many times
    it is asked for.
    """
    store = core.memory
    run = store.load_job_run(run_id)
    if run is None:
        return DispatchOutcome(DISPATCH_REFUSED, "unknown_generation")
    if run.spec.kind is not JobKind.CONDITION_WATCH:
        # The mirror refusal: an ordinary job's unconditional steps must never
        # be bound through the watch's path, exactly as a watch's conditional
        # declaration must never be bound through the ordinary one.
        return DispatchOutcome(DISPATCH_REFUSED, "not_a_condition_watch", run=run)

    try:
        dispatch = store.bind_condition_true_dispatch(
            run_id, owner_token, fencing_epoch, now=clock()
        )
    except JobError as exc:
        return DispatchOutcome(
            DISPATCH_REFUSED, f"not_bindable: {exc}", run=store.load_job_run(run_id)
        )
    return _run_bound_dispatch(core, run_id, owner_token, fencing_epoch, dispatch, clock)


def _run_bound_dispatch(
    core: Any,
    run_id: str,
    owner_token: str,
    fencing_epoch: int,
    dispatch: Any,
    clock: Callable[[], datetime],
) -> DispatchOutcome:
    """Everything after a FRESH binding commits: re-prove, run once, reflect.

    Shared by every caller that just bound a pristine session for the first
    time. An ordinary dispatch and a condition watch's TRUE dispatch differ
    only in what they bound and through which storage call; from the instant
    the binding is committed the safety property - and every step of proving
    it - is identical, so this is the ONE implementation of it.
    """
    store = core.memory

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

    return _reflect_outcome(
        store,
        run_id,
        owner_token,
        fencing_epoch,
        binding=dispatch,
        run=after,
        chain=chain,
        agent_run=agent_run,
        core_verdict=core_verdict,
        settled_at=settled_at,
    )


def recover_job_run(
    core: Any,
    run_id: str,
    owner_token: str,
    expected_fencing_epoch: int,
    *,
    lease: timedelta = DEFAULT_RECOVERY_LEASE,
    actor_label: str,
    reason: str,
    clock: Callable[[], datetime] = _utc_now,
) -> DispatchOutcome:
    """Recover ONE bound generation, re-prove everything, and only then resume.

    The phase-2B2a boundary. Where ``dispatch_job_run`` binds a fresh session
    and runs it once, this takes back a session that already exists - after a
    worker died holding it, or after it was parked for an approval a person has
    since been asked about - and continues THE SAME one.

    The ORDER is the safety property, so it is fixed:

    1. recover coordination ownership through the fenced storage boundary,
       which is where the observed-epoch fence, the binding immutability and
       the still-pending-approval wait rule are enforced, and which writes its
       own audit record inside its own transaction;
    2. re-prove the whole world under the NEW ownership: the generation is
       RUNNING and bound to the same session and chain, the caller is now the
       owner at the new epoch with a live lease, the chain is still the exact
       declaration the snapshot froze, and every routed task is still this
       session's own;
    3. ask the H2 journal what actually happened, over the COMPLETE history of
       every routed task, and stop here unless it is established;
    4. call ``AgentCore.run_chain`` on the BOUND session id - never a new one;
    5. sample a FRESH instant the moment it returns or raises;
    6. reflect through exactly the same fenced paths a first dispatch uses.

    OWNING IT DOES NOT AUTHORIZE RUNNING IT. Step 1 succeeding proves only that
    nobody else coordinates this occurrence; every proof in steps 2 and 3 still
    has to pass, and each one that does not leaves the generation RUNNING,
    owned and blocked - which is a strictly better place to be blocked than
    before, because now a person can see it and a later phase can act on it.

    WHAT STOPS A DUPLICATE EFFECT is step 3 plus Core. An attempt still
    ``IN_FLIGHT`` or ``UNKNOWN_OUTCOME`` means the connector boundary was
    crossed and nobody established what happened on the other side; this
    function will not enter Core at all in that state, and Core's own journal
    precheck refuses it independently. A crashed executor's in-flight attempt
    therefore stays exactly where it is - nothing here marks it, abandons it,
    resolves it or infers an outcome for it, and nothing here may: that gate
    belongs to a human authority a later phase builds.

    NOTHING HERE INTERPRETS AN APPROVAL. The storage boundary refused to
    recover a generation whose person is still being asked; past that point
    what the approval came to - a valid grant, an expired one, a rejection, or
    a fresh question - is Core's answer and only Core's, given when the session
    is re-entered. Being asked again is a correct outcome.
    """
    store = core.memory
    try:
        recovered = store.reacquire_job_run(
            run_id,
            owner_token,
            expected_fencing_epoch,
            now=clock(),
            lease=lease,
            actor_label=actor_label,
            reason=reason,
        )
    except JobError as exc:
        # The fenced boundary refused - a stale epoch, a live owner, an unbound
        # or unrecoverable state, a pending approval, unusable metadata. It is
        # the authority on all of that, and its refusal changed nothing.
        return DispatchOutcome(
            DISPATCH_REFUSED,
            f"not_recoverable: {exc}",
            run=store.load_job_run(run_id),
        )

    epoch = recovered.fencing_epoch

    # From here the generation is durably RUNNING under the NEW ownership, and
    # every exit leaves it that way unless a reflection is positively proved.
    run = store.load_job_run(run_id)
    if run is None or run.status is not JobRunStatus.RUNNING:
        return _recovery_blocked("recovered_state_unreadable", run)
    if run.session_id is None or run.chain_id is None:
        # Unreachable: the storage CAS required both. Proved again rather than
        # trusted, because everything below is about to be run on them.
        return _recovery_blocked("binding_missing", run)
    # PHASE 2C: a ``CONDITION_WATCH`` CAN be bound now - through
    # ``bind_condition_true_dispatch``, after its predicate was found TRUE -
    # so it is no longer refused here on sight. Nothing else about recovery
    # changes for it: ``_bound_state`` re-proves an ``on_true``-declared
    # binding by the same rule it always proved a ``steps``-declared one, and
    # every check below - lease, routing, complete H2 history, cancellation -
    # applies identically regardless of which declaration produced the chain.
    if (
        run.owner_token != owner_token
        or run.fencing_epoch != epoch
        or not run.lease_is_live(clock())
    ):
        return _recovery_blocked("lease_lost_before_resume", run)
    bound = _bound_state(store, run)
    if bound is None:
        # The session vanished, the chain was replaced, the project drifted, or
        # the declaration is no longer the one the snapshot froze.
        return _recovery_blocked("bound_state_mismatch", run)
    _session, chain = bound

    # (3) AUTHORITATIVE H2 REALITY, BEFORE CORE IS ENTERED. The complete
    # attempt history of every routed task, not the newest row: an unresolved
    # in-flight or unknown attempt can sit under a later clean one, and reading
    # only the top would resume over a connector call nobody ever resolved.
    routed = _routed_tasks(store, run, chain)
    if routed is None:
        return _recovery_blocked("routing_unprovable", run)
    history = _attempt_history(store, routed)
    if history is None:
        return _recovery_blocked("execution_not_established", run)
    verdict = execution_history_verdict(run.session_id, routed, chain.cursor, history)

    if verdict.finding is HistoryFinding.UNKNOWN_AT_CURSOR:
        # (3a) REFLECTION, NOT RESOLUTION, and never Core. A person has
        # already declared this attempt's outcome unestablishable - or its own
        # owner did, on a raise - and that is durable H2 truth this generation
        # is entitled to READ. Reflecting it moves the coordination state to
        # match what H2 already says; it decides nothing about the world,
        # touches no attempt, and gives nobody permission to run anything.
        #
        # Safe to write on a read taken a moment ago because UNKNOWN_OUTCOME
        # is a SINK: no production writer names it as a source, so nothing can
        # make this reading false between here and the fenced mutation - which
        # still proves this caller's own owner token, epoch and live lease.
        try:
            reflected = store.mark_job_run_unknown(
                run_id, owner_token, epoch, now=clock()
            )
        except JobError as exc:
            return _recovery_blocked(f"refused: {exc}", store.load_job_run(run_id))
        return DispatchOutcome(
            verdict=DISPATCH_UNKNOWN,
            reason="execution_unknown_at_cursor",
            run=reflected,
            session_id=run.session_id,
            chain_id=run.chain_id,
        )

    if not verdict.established:
        # IN_FLIGHT anywhere, an unresolved outcome under another step, more
        # than one attempt under one task, foreign or malformed identity, or a
        # completed task whose evidence is missing. ONE refusal for all of
        # them: every one means the same thing here - what really happened is
        # not established, so nothing may be resumed on top of it.
        return _recovery_blocked("execution_not_established", run)

    if run.cancel_requested:
        # (3b) ASKED TO STOP. Everything above proved this generation COULD be
        # resumed; this proves it must not be. Recovery reflects durable truth
        # and never decides policy, so it stops here rather than cancelling
        # anything itself - ``cancel_job_run`` owns that, through the
        # authorities that own the task and the chain.
        #
        # Deliberately AFTER the unknown reflection above: an uncertain
        # execution is durable H2 truth and outranks a request to stop, which
        # is only ever a wish about the future.
        return _recovery_blocked("cancel_requested", run)

    # (4) THE ONE CALL, on the session this generation has always been bound
    # to. No new session exists to pass, and none is created to pass.
    try:
        agent_run, core_verdict = core.run_chain(run.session_id)
    except Exception:  # noqa: BLE001 - an unclassified failure is not a verdict
        # THE FRESH INSTANT IS SAMPLED HERE TOO, for the same reason and with
        # the same absence of any write: an exception says the driver stopped,
        # never that the work did not happen.
        clock()
        return _recovery_blocked("run_chain_raised", store.load_job_run(run_id))

    # (5) FRESH NOW, sampled only now that Core has returned.
    settled_at = clock()

    after = store.load_job_run(run_id)
    if after is None or after.status is not JobRunStatus.RUNNING:
        return _recovery_blocked("generation_moved", after, core_verdict)
    if (
        after.session_id != run.session_id
        or after.chain_id != run.chain_id
        or after.owner_token != owner_token
        or after.fencing_epoch != epoch
    ):
        return _recovery_blocked("binding_or_owner_changed", after, core_verdict)
    if not after.lease_is_live(settled_at):
        # The recovery lease died while Core worked. A third worker may
        # legitimately own this generation by now, so this one writes NOTHING.
        return _recovery_blocked("lease_expired", after, core_verdict)
    rebound = _bound_state(store, after)
    if rebound is None:
        return _recovery_blocked("bound_state_mismatch", after, core_verdict)
    _session, chain = rebound

    # (6) The same reflection a first dispatch uses, under the new fencing.
    return _reflect_outcome(
        store,
        run_id,
        owner_token,
        epoch,
        binding=after,
        run=after,
        chain=chain,
        agent_run=agent_run,
        core_verdict=core_verdict,
        settled_at=settled_at,
    )


def cancel_job_run(
    store: Any,
    run_id: str,
    owner_token: str,
    *,
    lease: timedelta = DEFAULT_RECOVERY_LEASE,
    actor_label: str,
    reason: str,
    clock: Callable[[], datetime] = _utc_now,
) -> DispatchOutcome:
    """Ask for ONE generation to stop, and carry that as far as it safely goes.

    The phase-2B2b2 boundary, and deliberately an ORCHESTRATOR rather than an
    authority: every mutation below is performed by the component that already
    owns it. This function decides only the ORDER, and the order is the safety
    property:

    1. record the sticky intent (and settle immediately if the generation is
       provably nothing but a queue position);
    2. cancel the CHAIN, so no further step can be materialized or claimed -
       this is what makes a racing execution claim lose;
    3. cancel the current TASK through storage's own arbitration, which decides
       for itself whether that is final or merely requested;
    4. reflect whatever durable truth resulted onto the generation, through the
       same fenced settlement a first dispatch uses.

    IT TAKES NO STORE AUTHORITY IT DOES NOT ALREADY HAVE. It never writes a
    task status, never touches an execution attempt, never signals a connector
    and never settles a generation except through the fenced reflection, which
    refuses over any unresolved execution. A cancellation that arrives while
    something is IN_FLIGHT therefore ends as recorded intent, not as a lie
    about what happened.

    IDEMPOTENT AND RESTART-SAFE. Every step tolerates having already been done
    - an already-flagged run, an already-cancelled chain, an already-terminal
    task - so a process that dies anywhere is recovered by running the whole
    thing again. Nothing here is a point of no return except the operations
    that were already safe to repeat.
    """
    try:
        run = store.request_job_run_cancel(
            run_id, now=clock(), actor_label=actor_label, reason=reason
        )
    except JobError as exc:
        # Settled, in doubt, or unreadable. The storage boundary is the
        # authority on all of it and its refusal changed nothing.
        return DispatchOutcome(
            DISPATCH_REFUSED,
            f"not_cancellable: {exc}",
            run=store.load_job_run(run_id),
        )

    if run.settled:
        # The F3 case: provably a queue position, settled in that same
        # transaction. There is no session, no chain and no task to stop.
        return DispatchOutcome(
            DISPATCH_SETTLED,
            "cancelled_before_binding",
            run=run,
        )

    if run.session_id is None or run.chain_id is None:
        # Flagged but not bound: a CLAIMED generation whose holder still has a
        # live lease. The intent is durable and the claim CAS now refuses to
        # bind it, so it will be settled by the next request once the lease is
        # gone. Nothing further is safe or necessary here.
        return DispatchOutcome(
            DISPATCH_CANCEL_INTENT,
            "unbound_awaiting_release",
            run=run,
        )

    # (2) THE CHAIN, first: once it is not ACTIVE, every later execution claim
    # loses its chain authority check, so this is what actually stops work that
    # has not started. "terminal" and "conflict" mean somebody already did it.
    chain_verdict = store.cancel_chain(run.session_id, run.chain_id)
    if chain_verdict == "unknown":
        return _cancel_blocked("chain_unreadable", run)

    # (3) THE TASK, through storage's own cancellation arbitration. It alone
    # decides whether this is final or merely requested, and it refuses
    # outright when an outcome is in doubt.
    session = store.load_session(run.session_id)
    if session is None or session.chain is None:
        return _cancel_blocked("bound_state_mismatch", run)
    chain = session.chain
    cursor = chain.cursor
    if isinstance(cursor, int) and 0 <= cursor < len(chain.step_task_ids):
        cursor_task_id = chain.step_task_ids[cursor]
        if cursor_task_id:
            store.cancel_task(run.session_id, cursor_task_id)

    # (4) REFLECT. Take the coordination lease only now, so the reflection is
    # fenced exactly like every other settlement. A live owner still holding it
    # is the ordinary case for an in-flight execution: it will reflect its own
    # outcome, and this call leaves the intent durable.
    try:
        recovered = store.reacquire_job_run(
            run_id,
            owner_token,
            run.fencing_epoch,
            now=clock(),
            lease=lease,
            actor_label=actor_label,
            reason=reason,
        )
    except JobError:
        return DispatchOutcome(
            DISPATCH_CANCEL_INTENT,
            "intent_recorded",
            run=store.load_job_run(run_id),
            session_id=run.session_id,
            chain_id=run.chain_id,
        )

    epoch = recovered.fencing_epoch
    after = store.load_job_run(run_id)
    if after is None or after.status is not JobRunStatus.RUNNING:
        return _cancel_blocked("recovered_state_unreadable", after or run)
    if (
        after.owner_token != owner_token
        or after.fencing_epoch != epoch
        or not after.lease_is_live(clock())
    ):
        return _cancel_blocked("lease_lost_before_reflection", after)
    bound = _bound_state(store, after)
    if bound is None:
        return _cancel_blocked("bound_state_mismatch", after)
    _session, rebound = bound

    routed = _routed_tasks(store, after, rebound)
    if routed is None:
        return _cancel_blocked("routing_unprovable", after)
    history = _attempt_history(store, routed)
    if history is None:
        return _cancel_blocked("execution_not_established", after)
    verdict = execution_history_verdict(
        after.session_id, routed, rebound.cursor, history
    )
    if not verdict.established:
        # An execution is still in flight or unresolved. The generation stays
        # RUNNING with its intent recorded; settling CANCELLED over it would
        # claim that nothing happened, and only the 2B2b1 human gate can end an
        # uncertain execution.
        return DispatchOutcome(
            DISPATCH_CANCEL_INTENT,
            "execution_not_established",
            run=after,
            session_id=after.session_id,
            chain_id=after.chain_id,
        )

    # The SAME fenced reflection a first dispatch uses, with no Core call and
    # no agent run to read: the chain's own durable status is the evidence.
    return _reflect_outcome(
        store,
        run_id,
        owner_token,
        epoch,
        binding=after,
        run=after,
        chain=rebound,
        agent_run=None,
        core_verdict="cancel_requested",
        settled_at=clock(),
    )
