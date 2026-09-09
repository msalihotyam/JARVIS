"""Scheduler phase 2C: minimum condition-watch evaluation.

The ONE thing phase 2B1's dispatch bridge explicitly refused to do - ask a
``CONDITION_WATCH`` generation's typed predicate a question, and act on the
answer. The two halves stay exactly where they already lived:

* ASKING is ``jarvis.conditions.evaluate`` - a pure function over a typed
  predicate and a typed observation, with no filesystem, no clock and no
  storage of its own. It is unchanged and untouched by this module.
* ACTING is ``jarvis.dispatch`` - the same fenced bind-run-reflect machinery
  an ordinary job already uses, extended there by exactly one new binding
  source (``dispatch_condition_watch_true``).

This module is the SEAM between them, and nothing more: it builds the one
observation a predicate needs, asks the question, and either settles the
generation itself (FALSE, UNKNOWN) or hands off to ``jarvis.dispatch`` for the
one case that runs anything (TRUE). It owns no thread, no timer and no loop,
exactly like ``jarvis.dispatch``; it does not poll, sleep or scan for due
work, and it evaluates a predicate only through the one pure kernel that is
allowed to.

THE OBSERVATION IS NEVER A BARE READ. A condition watch is contained by
PRECISELY the boundary Core's own interactive local read is - the same
registered ``LocalFileReadConnector`` instance, bound to the same allowed
roots, walked through the same no-follow root-identity chain - because this
module asks Core's OWN registry for it rather than constructing a boundary of
its own. A connector that is missing, misconfigured or not the trusted type
answers as NO OBSERVATION, never as a bare filesystem call of this module's
own.

EVALUATING IS NOT DECIDING. A ``TRUE`` found here is exactly what
``jarvis.conditions`` says it is: an input to the ONE fenced binding
``dispatch_condition_watch_true`` performs, never a standing authorization.
That binding still creates no approval, consumes no grant and reaches no
connector of its own; every gate real work must pass is still ahead of it,
proved by Core exactly as it is for an interactive run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from jarvis.conditions import (
    ConditionResult,
    FileObservation,
    LocalReadCondition,
    ObservationScope,
    ObservationStatus,
    evaluate,
)
from jarvis.connectors.local_files import LocalFileReadConnector
from jarvis.contracts import (
    LOCAL_READ_CAPABILITY,
    LOCAL_READ_RESULT_KEY,
    ActionScope,
    JobError,
    JobKind,
    JobRunStatus,
    LocalReadSnapshot,
    OutcomeStatus,
    SessionState,
    Task,
)
from jarvis.dispatch import (
    DISPATCH_REFUSED,
    DISPATCH_SETTLED,
    DispatchOutcome,
    dispatch_condition_watch_true,
)
from jarvis.registry import ConnectorNotFoundError

#: A throwaway observation task's declared work. Never persisted, never
#: routed by Core, never shown to an operator - it exists only to give the
#: connector's own ``execute()`` the shape it requires.
_OBSERVATION_OBJECTIVE = "condition watch observation"


def _utc_now() -> datetime:
    """The default clock, isolated so a test can replace exactly this."""
    return datetime.now(timezone.utc)


def _local_file_observation(
    core: Any, condition: LocalReadCondition
) -> FileObservation | None:
    """Observe ``condition.reference`` through Core's OWN registered reader.

    Never opens a file itself, and never constructs a boundary of its own.
    The exact ``LocalFileReadConnector`` instance Core's registry already
    holds - bound, at construction, to the ONE root Core itself trusts - is
    asked to read, so a condition watch is contained by precisely the
    boundary an interactive read is and cannot diverge from it by
    construction. Everything the connector does before it opens a byte -
    canonical resolve, allowed-root containment, the secret-file policy, the
    representable-name check, the no-follow open chain - runs exactly as it
    does for Core's own interactive read; nothing here weakens, skips or
    duplicates any of it.

    ``None`` means no observation exists: the connector is not registered, is
    not the trusted type, or the read was refused, missing or failed for any
    reason. ``evaluate`` already treats every one of those identically as
    UNKNOWN, so nothing here needs to tell them apart.

    THE OBSERVED REFERENCE IS ALWAYS ``snapshot.relative_path`` - the identity
    the OPEN HANDLE proved, never the pathname this function merely
    constructed - so a read that succeeded on a differently-spelled but
    in-root path reports its own true name. Nothing here re-checks that name
    against ``condition.reference``: ``evaluate`` already refuses an
    observation of a different reference as UNKNOWN, and duplicating that
    comparison here would be a second place for the same rule to drift.
    """
    try:
        connector = core.registry.get(LocalFileReadConnector.manifest.name)
    except ConnectorNotFoundError:
        return None
    # Exact type, not ``isinstance``: a same-named connector of a different
    # class is not a promise that it opens files the same way, and this
    # module trusts none of its authority to a name alone.
    if type(connector) is not LocalFileReadConnector:
        return None
    # A bare relative reference is root-relative BY CONVENTION - the same
    # convention ``LocalReadSnapshot.relative_path`` reports back in - so it
    # is anchored to the connector's OWN default root here, exactly as an
    # interactive local-read task's bare target is anchored by Core before
    # the connector ever sees it. Nothing about containment changes: the
    # connector re-derives and re-checks this path from nothing, regardless
    # of what is handed to it.
    target = str(connector.default_working_directory / condition.reference)
    task = Task(
        objective=_OBSERVATION_OBJECTIVE,
        capability=LOCAL_READ_CAPABILITY,
        scope=ActionScope(
            action="read", target=target, connector=connector.manifest.name
        ),
    )
    session = SessionState(objective=_OBSERVATION_OBJECTIVE)
    try:
        result = connector.execute(task, session)
    except Exception:  # noqa: BLE001 - an unclassified failure is not evidence
        return None
    if result.outcome is not OutcomeStatus.SUCCESS:
        return None
    snapshot = result.data.get(LOCAL_READ_RESULT_KEY)
    if type(snapshot) is not LocalReadSnapshot:
        return None
    return FileObservation(
        reference=snapshot.relative_path,
        status=ObservationStatus.OK,
        scope=ObservationScope.WHOLE_FILE,
        sha256=snapshot.sha256,
    )


def evaluate_condition_watch(
    core: Any,
    run_id: str,
    owner_token: str,
    fencing_epoch: int,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> DispatchOutcome:
    """Evaluate ONE claimed condition watch's predicate, then settle or bind.

    The caller must already hold a live fenced lease on a CLAIMED generation
    of kind ``CONDITION_WATCH`` - exactly the same caller contract every
    scheduler entry point in ``jarvis.dispatch`` states for an ordinary job,
    and this function never claims, renews or takes one over either.

    The ORDER is the safety property, so it is fixed:

    1. observe the declared reference through Core's own registered,
       root-bound local-file reader (never a boundary of this module's own,
       never a bare ``open()``);
    2. evaluate the typed predicate against that observation, PURELY, and
       entirely OUTSIDE any storage transaction - the observation above never
       opens a connection, and the fenced write below is a single later call;
    3. FALSE or UNKNOWN: fence-settle the generation SKIPPED with the result
       persisted, reaching neither a session nor Core;
    4. TRUE: hand off to ``dispatch_condition_watch_true``, which binds
       ``JobSpec.on_true`` and persists ``condition_result = "true"`` in the
       SAME fenced transition that moves ``CLAIMED -> RUNNING``, then runs and
       reflects through the identical tail an ordinary dispatch uses.

    RESTART SAFE BY CONSTRUCTION. Every step here is either a pure read (the
    observation, the evaluation) or an idempotent fenced write proved by
    owner token, fencing epoch and a live lease - the same proof every other
    scheduler mutation demands. A process that dies before either write
    leaves the generation exactly where a fresh claim, and a fresh call to
    this function, may find and re-evaluate it safely; nothing here is a
    point of no return except the CAS that already was one.

    A cancellation flagged before this call is spent is never bypassed: FALSE
    or UNKNOWN settle regardless, because a generation that never ran
    anything is exactly what a stop request wants, but a TRUE observation
    that races a cancellation loses the bind to the same CAS every other
    binder loses to.
    """
    store = core.memory
    run = store.load_job_run(run_id)
    if run is None:
        return DispatchOutcome(DISPATCH_REFUSED, "unknown_generation")
    if run.spec.kind is not JobKind.CONDITION_WATCH:
        return DispatchOutcome(DISPATCH_REFUSED, "not_a_condition_watch", run=run)

    observation = _local_file_observation(core, run.spec.condition)
    result = evaluate(run.spec.condition, observation)

    if result is ConditionResult.TRUE:
        return dispatch_condition_watch_true(
            core, run_id, owner_token, fencing_epoch, clock=clock
        )
    if result not in (ConditionResult.FALSE, ConditionResult.UNKNOWN):
        # Unreachable while ``ConditionResult`` stays three-valued; refused
        # rather than silently settled if that ever changes.
        raise JobError(f"condition result is not a recognized member: {result!r}")

    try:
        settled = store.settle_job_run(
            run_id,
            JobRunStatus.SKIPPED,
            owner_token,
            fencing_epoch,
            now=clock(),
            condition_result=result.value,
        )
    except JobError as exc:
        return DispatchOutcome(
            DISPATCH_REFUSED,
            f"not_settleable: {exc}",
            run=store.load_job_run(run_id),
        )
    return DispatchOutcome(
        DISPATCH_SETTLED,
        f"condition_{result.value}",
        run=settled,
    )
