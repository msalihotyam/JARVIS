"""Resumable, API-free JARVIS Agent Loop state machine."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from decimal import Decimal
from typing import Any

from jarvis.connectors.base import BaseConnector
from jarvis.contracts import (
    CANONICAL_TARGET_CAPABILITIES,
    CHAIN_ENABLED_WRITE_MODES,
    CHAIN_NO_PREDECESSOR,
    MAX_CHAIN_STEPS,
    COST_CAP_GATE,
    DIAGNOSTIC_DETAIL_MAX_CHARS,
    LOCAL_READ_CAPABILITY,
    LOCAL_READ_CONTEXT_REFERENCE_KEY,
    LOCAL_READ_RESULT_KEY,
    LOCAL_READ_TASK_REFERENCE_KEY,
    LOCAL_WRITE_CAPABILITY,
    LOCAL_WRITE_RESULT_KEY,
    MAX_SQLITE_INTEGER,
    MODEL_ANSWER_RESULT_KEY,
    PAID_API_GATE,
    RETRIEVAL_MAX_CHARACTERS,
    RETRIEVAL_MAX_ITEMS,
    RETRIEVAL_NEXT_STEP_CAP,
    RETRIEVAL_RATIONALE_CAP,
    RETRIEVAL_SUMMARY_CAP,
    VERIFICATION_MAX_EVIDENCE,
    ActionScope,
    AgentRun,
    AttemptState,
    AttestationError,
    Availability,
    BillingClaim,
    BillingClass,
    BillingIntegrityError,
    BillingReconciliation,
    ChainBinding,
    ChainState,
    ChainStatus,
    ChainTaskMode,
    CostEstimate,
    DataClassification,
    DecisionRecord,
    EgressAttestation,
    ErrorCode,
    ExecutionAttempt,
    ExecutionClaimOutcome,
    LedgerSnapshot,
    LocalReadContextKind,
    LocalReadSnapshot,
    LocalWriteRecord,
    LoopStage,
    MemoryRetrievalQuery,
    MemoryStore,
    ModelAnswer,
    MonetaryState,
    OutboundDataPreview,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    ProgressIssueKind,
    ResultVerifier,
    RetrievedContext,
    RetrievedMemoryItem,
    RetrievedMemoryKind,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    SideEffectProfile,
    StructuredError,
    Task,
    TaskStatus,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
    WriteSource,
    canonical_path,
    canonical_usd,
    chain_state_refusal,
    chain_task_refusal,
    diagnostic_from_exception,
    format_usd,
    handoff_eligible,
    is_expired,
    local_read_context_refusal,
    local_read_reference_refusal,
    local_read_snapshot_refusal,
    local_write_content_refusal,
    local_write_identity_refusal,
    local_write_record_refusal,
    local_write_source_refusal,
    merge_side_effects,
    model_answer_refusal,
    nano_usd,
    normalize_destination,
    require_billing_class,
    result_to_dict,
    require_monetary_state,
    retrieval_eligible,
    sanitize_diagnostic_text,
    unusable_filesystem_target,
    usd_from_nano,
    validate_cost_estimate,
    validated_measured_cost,
    validated_metered_maximum,
)
from jarvis.lifecycle import is_terminal, terminal_run_status
from jarvis.planning import build_task_plan
from jarvis.progress import (
    GATE_ISSUE_KINDS,
    NON_FAILURE_ISSUE_KINDS,
    clear_issues,
    mark_stage_completed,
    normalize_session_next_step,
    set_issue,
    set_waiting_issue,
)
from jarvis.registry import (
    AmbiguousConnectorError,
    ConnectorNotFoundError,
    ConnectorUnavailableError,
    ToolRegistry,
)
from jarvis.settings import Settings


#: Task preference holding the billing fingerprint that cleared cost preflight.
#: A routing hint only: the proof that a gate was satisfied is always the
#: approved grant record bound to the current attestation, never this marker.
BILLING_PREFLIGHT_KEY = "billing_preflight_fingerprint"
BILLING_REPREFLIGHT_COUNT_KEY = "billing_repreflight_attempts"
#: Bound on gate <-> execute round trips caused by changing route/attestation.
BILLING_REPREFLIGHT_LIMIT = 3

#: Monetary gate actions come from ``jarvis.contracts`` so the loop that issues
#: the request and the storage boundary that re-verifies the persisted grant
#: compare exactly the same action name (imported above).

#: Bound on the non-secret reason tag carried by a billing-integrity failure.
BILLING_REASON_MAX_CHARS = 120


# --------------------------------------------------------------------------
# H4 - exact nested return validation
# --------------------------------------------------------------------------
#
# A stage boundary that only checks the outer object type is not a boundary:
# an exact ``RetrievedMemoryItem`` carrying ``kind="decision"`` instead of the
# ``RetrievedMemoryKind`` member, or an exact ``OutboundDataPreview`` carrying
# ``data_classification="internal"``, passes an outer check and then raises
# ``AttributeError`` on ``.value`` later - during checkpoint serialization,
# fingerprint construction or approval display, all *outside* the protected
# boundary. These helpers validate the authority-bearing nested fields against
# the exact types the contract declares, and raise so the caller's stage
# boundary turns it into deterministic terminal state.
#
# Nothing here coerces. ``str()``, ``int()``, ``bool()`` and ``Enum(value)``
# would turn tampered data into plausible active state, which is the failure
# these checks exist to prevent: a malformed value is refused, never repaired.


def _exact_type(value: Any, expected: type, name: str) -> Any:
    """Exactly ``expected``; a subclass or an equal-comparing value is not it.

    ``type() is`` rather than ``isinstance``: every classification and kind
    enum here derives from ``str``, so a raw ``"internal"`` compares equal to
    ``DataClassification.INTERNAL`` and satisfies ``isinstance(x, str)`` while
    having no ``.value`` at all.
    """
    if type(value) is not expected:
        raise TypeError(f"{name} is not an exact {expected.__name__}")
    return value


def _exact_text(value: Any, name: str, *, cap: int | None = None) -> str:
    """An actual string, within an existing hard ceiling when one applies."""
    if not isinstance(value, str):
        raise TypeError(f"{name} is not a string")
    if cap is not None and len(value) > cap:
        raise TypeError(f"{name} exceeds its ceiling")
    return value


def _exact_text_tuple(value: Any, name: str) -> tuple[str, ...]:
    """An exact tuple of actual strings; a list or a str is not a tuple."""
    _exact_type(value, tuple, name)
    for item in value:
        _exact_text(item, f"{name} entry")
    return value


def _exact_count(value: Any, name: str, *, cap: int = MAX_SQLITE_INTEGER) -> int:
    """A real non-negative integer inside the persistable range.

    ``True`` is not a count and neither is ``"7"``; the same signed-64-bit
    ceiling every persisted integer already obeys applies here, so an
    oversized value can never reach task JSON.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} is not an integer")
    if not 0 <= value <= cap:
        raise TypeError(f"{name} is outside the persistable range")
    return value


def _exact_flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} is not a boolean")
    return value


def _validated_scope(scope: Any, name: str) -> ActionScope:
    """Exactly an ``ActionScope`` whose every typed field is exact."""
    _exact_type(scope, ActionScope, name)
    for field_name in ("action", "target", "connector", "destination"):
        _exact_text(getattr(scope, field_name), f"{name} {field_name}")
    _exact_type(
        scope.data_classification, DataClassification, f"{name} classification"
    )
    return scope


def _validated_side_effect(profile: Any, name: str) -> SideEffectProfile:
    """Exactly a ``SideEffectProfile`` with an exact effect and real booleans."""
    _exact_type(profile, SideEffectProfile, name)
    _exact_type(profile.effect, SideEffect, f"{name} effect")
    _exact_flag(profile.reversible, f"{name} reversible")
    _exact_flag(profile.always_requires_approval, f"{name} approval flag")
    return profile


def _validated_attestation(attestation: Any) -> EgressAttestation:
    """Exactly the attestation contract, every bound field included.

    This is the object whose digest authorizes an external effect, so each
    field that feeds that digest - scope, risk, effect, outbound/gate digests,
    the resource boundary and the tool-grant manifest - must be exactly what
    the contract declares before any fingerprint is computed.
    """
    _exact_type(attestation, EgressAttestation, "attestation")
    _validated_scope(attestation.scope, "attestation scope")
    _exact_type(attestation.risk_level, RiskLevel, "attestation risk level")
    _validated_side_effect(attestation.side_effect, "attestation side effect")
    for field_name in (
        "outbound_digest",
        "billing_gate",
        "cost_gate",
        "working_directory",
    ):
        _exact_text(getattr(attestation, field_name), f"attestation {field_name}")
    _exact_text_tuple(attestation.allowed_roots, "attestation allowed roots")
    _exact_text_tuple(attestation.tool_grants, "attestation tool grants")
    return attestation


def _validated_outbound_preview(preview: Any) -> OutboundDataPreview:
    """Exactly the preview contract; this is what the user reads to approve."""
    _exact_type(preview, OutboundDataPreview, "outbound preview")
    _exact_text(preview.destination, "outbound preview destination")
    _exact_type(
        preview.data_classification,
        DataClassification,
        "outbound preview classification",
    )
    _exact_text_tuple(preview.field_names, "outbound preview field names")
    _exact_count(preview.approximate_bytes, "outbound preview byte count")
    _exact_count(preview.redacted_items, "outbound preview redacted count")
    _exact_text(preview.summary, "outbound preview summary")
    return preview


def _validated_retrieved_item(item: Any) -> RetrievedMemoryItem:
    """Exactly one retrieval item, every field validated by its own type.

    The per-field character ceilings are the same hard retrieval contract
    ``build_retrieved_context`` enforces, so an oversized item is refused
    rather than persisted into the task snapshot.
    """
    _exact_type(item, RetrievedMemoryItem, "retrieved item")
    _exact_type(item.kind, RetrievedMemoryKind, "retrieved item kind")
    _exact_type(
        item.classification, DataClassification, "retrieved item classification"
    )
    _exact_text(item.record_id, "retrieved item record id")
    _exact_text(item.source_task_id, "retrieved item source task id")
    _exact_text(item.created_at, "retrieved item created_at")
    _exact_text(item.summary, "retrieved item summary", cap=RETRIEVAL_SUMMARY_CAP)
    _exact_text(
        item.rationale, "retrieved item rationale", cap=RETRIEVAL_RATIONALE_CAP
    )
    if item.next_step is not None:
        _exact_text(
            item.next_step, "retrieved item next step", cap=RETRIEVAL_NEXT_STEP_CAP
        )
    _exact_count(item.relevance_score, "retrieved item relevance score")
    _exact_flag(item.truncated, "retrieved item truncated flag")
    return item


class _LifecycleLost(Exception):
    """Internal control flow: another writer won authoritative lifecycle.

    A guarded checkpoint that reports failure is a control-flow signal, not a
    harmless persistence miss: the local loop must stop immediately rather than
    keep advancing a stale task through later stages. ``run`` carries the
    authoritative result reloaded from storage.
    """

    def __init__(self, run: AgentRun) -> None:
        super().__init__("lifecycle authority lost to another writer")
        self.run = run


#: Effects whose outcome cannot be known once the connector raised, so the
#: attempt is marked non-replayable instead of becoming a clean, retryable
#: failure. EXTERNAL_EGRESS is the original member and its behaviour is
#: unchanged: the call may already have left the machine.
#:
#: M5 adds the two local effects for the same reason in a different medium. A
#: local write that raised may have completed its atomic replace, so treating
#: it as a clean failure would invite a retry over a file that already
#: changed. Deliberately NOT generalized to every irreversible connector:
#: irreversibility is about undo, while this set is about whether the outcome
#: is knowable at all.
_UNKNOWABLE_ON_RAISE_EFFECTS = frozenset(
    {
        SideEffect.EXTERNAL_EGRESS,
        SideEffect.LOCAL_WRITE,
        SideEffect.DESTRUCTIVE,
    }
)


#: How often the owner-local stop watcher asks storage whether a
#: cancellation has been requested, in seconds. A calibration knob, not a
#: correctness boundary: a longer interval only makes a stop less prompt,
#: never less safe, because the watcher's authority does not depend on how
#: fresh its read is - it can only ask its OWN live operation to stop.
#: Read inside the loop so a test can tighten it without touching behaviour.
_LIVE_STOP_POLL_SECONDS = 0.25

#: How long the owner waits for its watcher to finish after the connector
#: call returns. Sized above the storage busy timeout (5s) plus margin,
#: because the one thing the watcher can be blocked in is a short SELECT on a
#: busy database, which fails on its own within that bound. A watcher still
#: alive after this would be a thread leak rather than a slow poll, so tests
#: assert liveness rather than trusting the number.
_LIVE_STOP_JOIN_SECONDS = 10.0


class _LiveStopWatcher:
    """One claimed execution's advisory, owner-local stop attempt.

    S2B. Stopping live external work needs an external call, and an external
    call cannot be made atomic with the storage decision authorizing it. S2A
    proved that guarding it with a durable claim makes ACQUISITION unique
    without making the SIGNAL safe, because a second actor's permission can
    go stale between the two.

    This dissolves that instead of guarding it. The only actor that ever
    signals is the one that already won ``begin_execution`` and is holding
    the connector object mid-call, in the same process - so "may I still
    signal?" is answered by the connector's own live handle at the instant of
    signalling, under the connector's own lock, not by a storage read that
    could have gone stale. There is no second authority, because there is no
    second actor.

    What it may do: poll one bit of persisted intent, and ask THAT connector
    object to stop THAT task, at most once, ever.

    What it may never do: conclude an attempt, mark an outcome unknown, touch
    a reservation, a grant, an approval or a chain, persist anything at all
    about the stop, or retry. Its return value is ignored, and so is any
    exception it raises: a connector that says True has delivered a local
    signal, which is not evidence that provider-side work stopped, that money
    stopped, or that any effect did not happen. The execution owner's normal
    post-execute path remains the sole finalizer.
    """

    def __init__(
        self,
        memory: MemoryStore,
        session_id: str,
        task_id: str,
        connector: BaseConnector,
    ) -> None:
        self._memory = memory
        self._session_id = session_id
        self._task_id = task_id
        # THE EXACT OBJECT the owner is calling execute on. Never a registry
        # lookup, never task.selected_connector, never a fresh construction:
        # only this instance holds the live operation, and any other one
        # would be signalling somebody else's work or nothing at all.
        self._connector = connector
        self._stop = threading.Event()
        # THE signal-authority lock. It guards three things that must agree:
        # whether authority has been revoked, whether the one attempt is
        # spent, and the connector call itself. Holding it ACROSS the external
        # call is the point - it is what lets ``stop`` mean "no signal can
        # still be on its way", rather than "no signal has started yet".
        self._signal_lock = threading.Lock()
        self._revoked = False
        # One attempt per claimed execution, consumed BEFORE the call so that
        # a raising connector cannot leave a second attempt available.
        self._attempted = False
        self._thread = threading.Thread(
            target=self._watch, name="jarvis-live-stop", daemon=True
        )
        #: Contained failures, kept visible but never consulted by anything
        #: that decides an outcome.
        self.last_read_error: Exception | None = None
        self.last_stop_error: Exception | None = None

    @property
    def attempted(self) -> bool:
        """Whether the single stop attempt has been consumed."""
        return self._attempted

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """REVOKE stop authority, and return only once none can still be used.

        This is a barrier, not a request. Two things have to be true before
        the owner may touch a single piece of lifecycle state, and a timed
        join delivers neither of them:

        * a watcher parked in a slow storage read may wake up long after the
          owner has finalized, so authority is revoked UNDER the signal lock -
          its later ``_signal_once`` sees ``_revoked`` and refuses. The thread
          ending is not what makes this safe; the flag is.
        * a connector call already in flight must be FINISHED, not merely
          started, or the owner would be concluding an execution while an
          external stop is still crossing the boundary. Acquiring the lock
          waits for exactly that.

        So correctness rests on the lock, and nothing rests on the join. If a
        connector ignores its prompt-return contract badly enough, the owner
        waits here - fail-closed, and deliberately: the alternative is moving
        lifecycle forward with an uncontrolled external call still running. A
        process killed while waiting falls back to the existing crash and
        in-doubt semantics, which is a state S2A already handles honestly.
        """
        self._stop.set()
        with self._signal_lock:
            # Crossing this lock is the guarantee. Anything already inside it
            # has finished; anything arriving later finds authority gone.
            self._revoked = True
        # Cleanup only. A thread still parked in a read is now harmless, and
        # its liveness is never treated as authority - which is also why a
        # watcher that never started is stopped just as validly as one that
        # did: revocation above is the whole guarantee.
        if self._thread.ident is not None:
            self._thread.join(timeout=_LIVE_STOP_JOIN_SECONDS)

    @property
    def revoked(self) -> bool:
        """Whether stop authority has been revoked. Never re-granted."""
        return self._revoked

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _watch(self) -> None:
        while not self._stop.wait(_LIVE_STOP_POLL_SECONDS):
            if self._cancellation_requested():
                self._signal_once()
                return

    def _cancellation_requested(self) -> bool:
        """The one bit, or ``False`` when storage could not be asked.

        A failed poll is not a cancellation and not a reason to give up:
        storage may simply be busy, and the next poll asks again. A
        permanently failing read therefore means no stop is ever attempted -
        exactly the behaviour before this watcher existed.
        """
        try:
            return self._memory.execution_cancellation_requested(
                self._session_id, self._task_id
            )
        except Exception as exc:  # noqa: BLE001 - storage boundary
            self.last_read_error = exc
            return False

    def _signal_once(self) -> None:
        with self._signal_lock:
            if self._revoked:
                # The owner has moved on. A read that started before the
                # barrier and finished after it carries no authority with it.
                return
            if self._attempted:
                return
            # Consumed FIRST. If the call raises, the attempt is still spent:
            # a connector that failed to stop may equally have half-stopped,
            # and asking again would be a second uninvited crossing of the
            # same boundary on no better evidence.
            self._attempted = True
            try:
                self._connector.cancel(self._task_id)
            except Exception as exc:  # noqa: BLE001 - connector boundary
                # Advisory, so a failure is indistinguishable from a refusal
                # and neither is authority. Kept for inspection rather than
                # discarded silently, and read by nothing that decides
                # anything.
                self.last_stop_error = exc


class AgentCore:
    def __init__(
        self,
        registry: ToolRegistry,
        memory: MemoryStore,
        settings: Settings | None = None,
        verifier: ResultVerifier | None = None,
    ) -> None:
        self.registry = registry
        self.memory = memory
        self.settings = settings or Settings()
        self.verifier = verifier

    def start_session(self, objective: str = "") -> SessionState:
        session = SessionState(objective=objective)
        self.memory.save_session(session)
        return session

    def run(
        self,
        task: Task,
        session_id: str | None = None,
        grant: PermissionGrant | None = None,
    ) -> AgentRun:
        if session_id is None:
            session = self.start_session(task.objective)
        else:
            session = self.memory.load_session(session_id)
            if session is None:
                return self._input_failure(
                    task,
                    ErrorCode.UNKNOWN_SESSION,
                    f"Unknown session: {session_id}",
                )
        existing_session_id = self.memory.task_session_id(task.id)
        if existing_session_id is not None and existing_session_id != session.id:
            return self._input_failure(
                task,
                ErrorCode.INVALID_TASK,
                "Task belongs to a different session.",
                session,
            )
        if existing_session_id is None and task.stage is not LoopStage.UNDERSTAND:
            return self._input_failure(
                task,
                ErrorCode.INVALID_TASK,
                "A new task must start at the understand stage; "
                "use resume() to continue persisted work.",
                session,
            )
        if existing_session_id is not None:
            persisted = self.memory.load_task(task.id)
            if persisted is not None:
                task = persisted
                # A terminal task is reported, never re-registered or mutated.
                terminal = self._terminal_run(session, task)
                if terminal is not None:
                    return terminal
        binding_failure = self._bind_project(session, task)
        if binding_failure is not None:
            return binding_failure
        session.active_task_id = task.id
        session.latest_task_id = task.id
        try:
            self._checkpoint(session, task, "task_registered")
        except _LifecycleLost as lost:
            return lost.run
        return self._advance(session, task, grant)

    def resume(
        self,
        session_id: str,
        task_id: str,
        grant: PermissionGrant | None = None,
    ) -> AgentRun:
        session = self.memory.load_session(session_id)
        if session is None:
            return self._input_failure(
                None,
                ErrorCode.UNKNOWN_SESSION,
                f"Unknown session: {session_id}",
            )
        task = self.memory.load_task(task_id)
        if task is None:
            return self._input_failure(
                None,
                ErrorCode.TASK_NOT_FOUND,
                f"Unknown task: {task_id}",
                session,
            )
        if self.memory.task_session_id(task_id) != session_id:
            return self._input_failure(
                task,
                ErrorCode.INVALID_TASK,
                "Task does not belong to this session.",
                session,
            )
        # Navigation metadata only. Saved eagerly because a terminal-stage
        # resume returns without writing another checkpoint. Updating this
        # session pointer is not a task mutation and needs no connector.
        if session.latest_task_id != task.id:
            session.latest_task_id = task.id
            self.memory.save_session(session)
        # A terminal task is then reported without touching the connector or
        # mutating the task, so a completed/failed/cancelled/rejected task
        # cannot be reopened and stays readable even if its connector is gone.
        terminal = self._terminal_run(session, task)
        if terminal is not None:
            return terminal
        return self._advance(session, task, grant)

    def provide_clarification(
        self, session_id: str, task_id: str, clarification: str
    ) -> AgentRun:
        task = self.memory.load_task(task_id)
        session = self.memory.load_session(session_id)
        if session is None or task is None:
            return self._input_failure(
                task,
                ErrorCode.TASK_NOT_FOUND if task is None else ErrorCode.UNKNOWN_SESSION,
                "Session or task was not found.",
                session,
            )
        if self.memory.task_session_id(task_id) != session_id:
            return self._input_failure(
                task,
                ErrorCode.INVALID_TASK,
                "Task does not belong to this session.",
                session,
            )
        # Legal only while this exact task is actually waiting for the answer.
        # After an approval was requested or granted, execution began or the
        # task terminated, a clarification could change the outbound content
        # behind the user's approval; it is refused without any mutation.
        if (
            task.status is not TaskStatus.AWAITING_CLARIFICATION
            or task.stage is not LoopStage.CLARIFY_OR_ASSUME
        ):
            return self._input_failure(
                task,
                ErrorCode.INVALID_TASK,
                "Task is not awaiting clarification.",
                session,
            )
        task.preferences["clarification"] = clarification
        task.status = TaskStatus.PENDING
        task.stage = LoopStage.CLARIFY_OR_ASSUME
        session.latest_task_id = task.id
        try:
            self._checkpoint(session, task, "clarification_provided")
        except _LifecycleLost as lost:
            return lost.run
        return self._advance(session, task, None)

    def cancel(self, session_id: str, task_id: str) -> AgentRun:
        """Cancel by ID. Storage rebinds, decides, and hands back the rows.

        Nothing is loaded here first and nothing is judged here first. Core
        used to read the task, decide from the DOCUMENT whether it was already
        terminal, and only then ask storage - so a forged terminal document
        produced a terminal AgentRun that storage had never agreed to, and a
        forged id sent the whole cancellation at another task. Both were
        decisions taken on a caller-decoded object, which is exactly the
        authority core must not hold.

        S2A CLOSEOUT. NO CONNECTOR IS SIGNALLED, at all, from here.

        A cancellation that stops a live execution has to cross an external
        boundary, and that crossing cannot be made atomic with the storage
        decision that authorizes it: no transaction may be held across it, the
        execution owner must never be blocked waiting for it, and S2A admits no
        lease, heartbeat or automatic reclaim to bound it. A durable claim made
        ACQUISITION unique but left the signal itself detached - still able to
        go stale between the arbitration and the actual call - so it bought
        uniqueness, not safety.

        S2A therefore fails closed instead of pretending otherwise: the
        cancellation intent is recorded honestly and nothing is asked of any
        connector. Safe live IN_FLIGHT connector-boundary coordination is
        deferred to S2B. This is a deliberate capability gap, not an oversight,
        and the registry is not consulted below.
        """
        outcome = self.memory.cancel_task(session_id, task_id)
        if outcome.verdict == "unknown_session":
            return self._input_failure(
                None, ErrorCode.UNKNOWN_SESSION, f"Unknown session: {session_id}"
            )
        if outcome.verdict == "not_found":
            # One bounded refusal for a missing task, a foreign one, and a
            # persisted document that does not describe its own row.
            return self._input_failure(
                None, ErrorCode.TASK_NOT_FOUND, f"Unknown task: {task_id}"
            )
        # Everything below renders the REBOUND rows storage returned. Every
        # remaining verdict carries them; a receipt without them would mean
        # storage identified no task, which the two refusals above already
        # covered - so treat it as the same refusal rather than trusting it.
        session, task = outcome.session, outcome.task
        if session is None or task is None:
            return self._input_failure(
                None, ErrorCode.TASK_NOT_FOUND, f"Unknown task: {task_id}"
            )
        if outcome.verdict == "terminal":
            reported = self._terminal_run(session, task)
            return reported if reported is not None else AgentRun(
                RunStatus.CANCELLED, session, task
            )
        if outcome.verdict == "in_doubt":
            # No connector call, and no write: the attempt's outcome is
            # unknown, so no cancellation verdict would be truthful. The
            # operator is pointed at the explicit abandon recovery.
            return self._execution_in_doubt(
                session, task, self.memory.latest_execution_attempt(task_id)
            )
        if outcome.verdict == "lost":
            # The task changed owner between the read and the guarded write.
            # Nothing was written; reporting CANCELLED here would be the exact
            # lie the ownership guard exists to stop.
            return self._cancellation_not_authorized(session, task)
        if outcome.verdict == "cancelled":
            return AgentRun(RunStatus.CANCELLED, session, task)
        if outcome.verdict == "cancelling":
            return AgentRun(RunStatus.CANCELLING, session, task)
        return self._input_failure(
            task, ErrorCode.TASK_NOT_FOUND, f"Unknown task: {task_id}", session
        )

    def _cancellation_not_authorized(
        self, session: SessionState, task: Task
    ) -> AgentRun:
        """A cancellation that no longer holds authority over its task.

        Bounded and non-disclosing: it does not say which session owns the
        task now, because a refusal is not a channel for facts about somebody
        else's work. Nothing was written, so the honest report is that this
        cancellation did not happen - not a CANCELLED that would tell the
        operator the work stopped.
        """
        return AgentRun(
            RunStatus.FAILED,
            session,
            task,
            error=StructuredError(
                ErrorCode.TASK_NOT_FOUND,
                "This cancellation no longer holds authority over the task; "
                "nothing was changed. Re-read the task and try again.",
                retryable=True,
            ),
        )

    def _advance(
        self,
        session: SessionState,
        task: Task,
        grant: PermissionGrant | None,
    ) -> AgentRun:
        """Drive the loop, stopping at once if lifecycle authority is lost."""
        try:
            return self._advance_loop(session, task, grant)
        except _LifecycleLost as lost:
            return lost.run

    def _advance_loop(
        self,
        session: SessionState,
        task: Task,
        grant: PermissionGrant | None,
    ) -> AgentRun:
        # P1-2: bind a filesystem target to its canonical identity ONCE, at
        # the single entry point every path into the loop passes through
        # (``run`` and ``resume`` both reach the loop only through here), and
        # before anything reads the scope. Everything downstream - scope
        # authority, the permission and scope fingerprints, the egress
        # attestation's resource boundary and the connector's own execution -
        # then sees the same canonical string.
        canonical_failure = self._bind_canonical_local_target(session, task)
        if canonical_failure is not None:
            return canonical_failure
        active_grant = grant
        while True:
            # Terminal states are immutable; report and never re-enter the loop.
            terminal = self._terminal_run(session, task)
            if terminal is not None:
                return terminal

            # A durable in-doubt/finished execution attempt is authoritative
            # over the persisted stage: it is checked before the connector is
            # even looked up, so an in-doubt task is reportable and never
            # replayed, and a finished one advances without a second call.
            if task.stage is LoopStage.EXECUTE:
                journal = self._execution_journal_precheck(session, task)
                if isinstance(journal, AgentRun):
                    return journal
                if journal == "advanced":
                    continue

            # Cancellation before any external call started is safe: the
            # journal precheck above already blocked any IN_FLIGHT/UNKNOWN
            # attempt, so reaching here means no external call is in progress.
            if task.cancellation_requested:
                task.status = TaskStatus.CANCELLED
                task.stage = LoopStage.CANCELLED
                session.active_task_id = None
                clear_issues(task, *NON_FAILURE_ISSUE_KINDS)
                self._checkpoint(session, task, "task_cancelled")
                return AgentRun(RunStatus.CANCELLED, session, task)

            if task.stage is LoopStage.UNDERSTAND:
                if not task.objective.strip() or not task.capability.strip() or not task.scope.action.strip():
                    return self._terminal_failure(
                        session,
                        task,
                        StructuredError(
                            ErrorCode.INVALID_TASK,
                            "Objective, capability and scope.action are required.",
                        ),
                    )
                mark_stage_completed(task, LoopStage.UNDERSTAND)
                task.stage = LoopStage.CONTEXT_RETRIEVAL
                self._checkpoint(
                    session, task, "intent_understood", {"objective": task.objective}
                )
                continue

            if task.stage is LoopStage.CONTEXT_RETRIEVAL:
                # M7: the durable write-source identity is judged first. A
                # malformed or inconsistent identity is terminal here, so a
                # handoff task can never fall through to the direct-write
                # path merely because its persisted provenance was corrupted.
                identity_failure = self._write_identity_failure(session, task)
                if identity_failure is not None:
                    return identity_failure
                # M10: an overwrite must name the completed read of the file it
                # replaces, and must arrive carrying NO preimage of its own -
                # an injected snapshot never stands in for the resolution step.
                preimage_failure = self._overwrite_preimage_failure(session, task)
                if preimage_failure is not None:
                    return preimage_failure
                # M8: the durable read-context identity is judged next, before
                # either read resolver runs. A corrupted bundle is terminal
                # here, so a multi-file consumer can never fall through to the
                # no-context path merely because its provenance was damaged.
                read_context_failure = self._local_read_context_failure(
                    session, task
                )
                if read_context_failure is not None:
                    return read_context_failure
                # M3: an explicit reference to an earlier completed local read
                # is resolved here, before retrieval, so an unusable reference
                # fails the task closed rather than half-populating it. It is
                # a separate typed field: it never enters the retrieval
                # pipeline and never consumes the 5-item / 4000-character
                # retrieval quota.
                local_read_failure = self._bind_referenced_local_read(session, task)
                if local_read_failure is not None:
                    return local_read_failure
                # M8: the plural hop resolves and freezes the ordered bundle
                # here, before retrieval and three stages before the first
                # attestation, so no approval can ever bind live source
                # pointers instead of frozen excerpts. The two hops are
                # mutually exclusive by the identity check above.
                read_context_bind_failure = self._bind_referenced_local_reads(
                    session, task
                )
                if read_context_bind_failure is not None:
                    return read_context_bind_failure
                # M7: the explicit model answer handoff resolves and freezes
                # the write bytes here, before retrieval and three stages
                # before the first write attestation, so no approval can ever
                # bind a live source pointer instead of frozen bytes.
                handoff_failure = self._bind_referenced_model_answer(session, task)
                if handoff_failure is not None:
                    return handoff_failure
                # The query is built only from the task's own declared intent,
                # never from arbitrary context inputs, preferences or prior
                # retrieved content. Retrieval runs once; a task resumed past
                # this stage keeps the persisted snapshot.
                query = MemoryRetrievalQuery(
                    session_id=session.id,
                    query_text="\n".join(
                        (
                            task.objective,
                            task.capability,
                            task.scope.action,
                            task.scope.target,
                        )
                    ),
                    exclude_task_id=task.id,
                    # M1: the session's validated binding is the only project
                    # scope key; a project-less session retrieves exactly as
                    # before.
                    project_id=session.project_id,
                )
                try:
                    # H4-C: the call AND the validation of what it returned are
                    # one boundary, so a malformed return value is a boundary
                    # failure rather than a TypeError escaping into the loop.
                    retrieved = self._validated_retrieved_context(
                        self.memory.retrieve_context(query)
                    )
                except Exception as exc:  # noqa: BLE001 - stage boundary
                    # Retrieval is bounded, informational context. A storage or
                    # decoding fault here must not leave the task in an
                    # undefined state or leak raw diagnostics.
                    return self._stage_failure(
                        session,
                        task,
                        LoopStage.CONTEXT_RETRIEVAL,
                        ErrorCode.INVALID_TASK,
                        exc,
                    )
                task.retrieved_context = retrieved
                # Compatibility count: selected outbound-eligible items only.
                task.context["retrieved_decision_count"] = len(retrieved.items)
                mark_stage_completed(task, LoopStage.CONTEXT_RETRIEVAL)
                task.stage = LoopStage.CLARIFY_OR_ASSUME
                # Aggregates only: the checkpoint/audit trail never duplicates
                # the retrieved contents themselves.
                self._checkpoint(
                    session,
                    task,
                    "context_retrieved",
                    {
                        "decision_count": len(retrieved.items),
                        "candidate_count": retrieved.candidate_count,
                        "character_count": retrieved.character_count,
                        "truncated": retrieved.truncated,
                    },
                )
                continue

            if task.stage is LoopStage.CLARIFY_OR_ASSUME:
                needs_clarification = bool(task.preferences.get("needs_clarification"))
                clarification = str(task.preferences.get("clarification") or "").strip()
                if needs_clarification and not clarification:
                    task.status = TaskStatus.AWAITING_CLARIFICATION
                    set_waiting_issue(task)
                    self._checkpoint(
                        session,
                        task,
                        "clarification_required",
                        {
                            "question": sanitize_diagnostic_text(
                                task.preferences.get(
                                    "clarification_question",
                                    "Clarification required",
                                )
                            )
                        },
                    )
                    return AgentRun(RunStatus.AWAITING_CLARIFICATION, session, task)
                task.visible_assumption = (
                    clarification
                    or str(task.preferences.get("assumption") or "").strip()
                    or "Proceeding only within the explicit task scope."
                )
                clear_issues(task, ProgressIssueKind.CLARIFICATION_REQUIRED)
                mark_stage_completed(task, LoopStage.CLARIFY_OR_ASSUME)
                task.stage = LoopStage.PLAN
                task.status = TaskStatus.PENDING
                self._checkpoint(
                    session,
                    task,
                    "assumption_visible",
                    {"assumption": sanitize_diagnostic_text(task.visible_assumption)},
                )
                continue

            if task.stage is LoopStage.PLAN:
                # Always rebuilt from safe structural task state: a persisted
                # or tampered plan is never preserved while the task is still
                # at PLAN. A task resumed past this stage keeps its persisted
                # plan because PLAN is not re-entered.
                try:
                    task.plan = build_task_plan(task)
                except Exception as exc:  # noqa: BLE001 - stage boundary
                    return self._stage_failure(
                        session, task, LoopStage.PLAN, ErrorCode.INVALID_TASK, exc
                    )
                # M8: the consumer-connector policy is evaluated HERE, on
                # the exact frozen task ``registry.select`` is about to route,
                # and never on the pre-freeze one.
                consumer_failure = self._local_read_consumer_failure(session, task)
                if consumer_failure is not None:
                    return consumer_failure
                try:
                    selection = self.registry.select(task)
                except Exception as exc:  # noqa: BLE001 - stage boundary
                    return self._selection_failure(session, task, exc)
                # M8: and the connector selection actually returned is judged
                # again, before it is recorded. Enumeration and selection are
                # different code paths over the same registry; if they ever
                # disagree, the one that can execute must not be the weaker.
                selected_failure = self._local_read_selection_failure(
                    session, task, selection.connector
                )
                if selected_failure is not None:
                    return selected_failure
                task.selected_connector = selection.connector.manifest.name
                mark_stage_completed(task, LoopStage.PLAN)
                task.stage = LoopStage.RISK_PERMISSION
                self._checkpoint(
                    session,
                    task,
                    "plan_created",
                    {"steps": task.plan, "candidate_connector": task.selected_connector},
                )
                continue

            connector = self._connector_for_task(task)
            if isinstance(connector, AgentRun):
                return connector

            if task.stage is LoopStage.RISK_PERMISSION:
                # M7: no attestation, request or approval is ever built for a
                # write whose durable source identity is not usable.
                identity_failure = self._write_identity_failure(session, task)
                if identity_failure is not None:
                    return identity_failure
                # M10: nor for an overwrite whose preimage is no longer exactly
                # the referenced read's own snapshot. Past CONTEXT_RETRIEVAL
                # this is a consistency check and never a rebinding.
                preimage_failure = self._overwrite_preimage_failure(session, task)
                if preimage_failure is not None:
                    return preimage_failure
                # M8: nor for a task whose frozen read bundle is not usable.
                # Past CONTEXT_RETRIEVAL this demands one validated snapshot
                # per reference, so a bundle that was emptied or truncated
                # after the checkpoint can never be approved as if intact.
                read_context_failure = self._local_read_context_failure(
                    session, task
                )
                if read_context_failure is not None:
                    return read_context_failure
                # The billing route is established before the user is asked for
                # anything and before cost preflight, tool selection or
                # execution can be reached.
                billing_failure = self._billing_route_failure(session, task, connector)
                if billing_failure is not None:
                    return billing_failure
                # The request always describes the task as it is *now*; a
                # persisted pending request is reused only when it attests
                # exactly this state, otherwise it is superseded.
                current = self._attested_request(session, task, connector)
                if isinstance(current, AgentRun):
                    return current
                request = self._exact_pending_request(session, task, current)
                if self._requires_approval(request):
                    if self._remembered_permission_allowed(request) and self.memory.has_remembered_scope_grant(
                        session.id, request.scope_fingerprint
                    ):
                        clear_issues(task, *GATE_ISSUE_KINDS)
                        mark_stage_completed(task, LoopStage.RISK_PERMISSION)
                        task.stage = LoopStage.COST_PREFLIGHT
                        self._checkpoint(
                            session, task, "remembered_permission_applied"
                        )
                        continue
                    approval_run, consumed = self._handle_approval(
                        session,
                        task,
                        request,
                        active_grant,
                        RunStatus.AWAITING_APPROVAL,
                    )
                    if approval_run is not None:
                        return approval_run
                    if consumed:
                        active_grant = None
                clear_issues(task, *GATE_ISSUE_KINDS)
                mark_stage_completed(task, LoopStage.RISK_PERMISSION)
                task.stage = LoopStage.COST_PREFLIGHT
                task.status = TaskStatus.PENDING
                self._checkpoint(session, task, "permission_policy_passed")
                continue

            if task.stage is LoopStage.COST_PREFLIGHT:
                # Re-classified here rather than trusting the persisted value:
                # the route is a property of this call, not of the task.
                billing_failure = self._billing_route_failure(session, task, connector)
                if billing_failure is not None:
                    return billing_failure
                estimate = task.estimate
                # Only genuinely metered money is projected against the cap,
                # and the projection is exact integer nano-USD over the
                # authoritative snapshot - measured spend plus every still-open
                # reservation - never a bare float SUM of measured rows.
                exposure = self._ledger_exposure(session, task)
                if isinstance(exposure, AgentRun):
                    return exposure
                snapshot, monetary_maximum = exposure
                projected = usd_from_nano(snapshot.total_nano_usd + monetary_maximum)
                gates = self._monetary_gates(session, task, connector, projected)
                if isinstance(gates, AgentRun):
                    return gates
                # A gate is satisfied only by an approved grant bound to its
                # exact current attestation (gate, projection, limit, billing
                # state, egress digest); persisted markers prove nothing.
                open_gate = next(
                    (
                        (gate, current)
                        for gate, current in gates
                        if not self.memory.has_approved_grant(
                            session.id, task.id, current.fingerprint
                        )
                    ),
                    None,
                )
                if open_gate is not None:
                    gate, current = open_gate
                    request = self._exact_pending_request(session, task, current)
                    waiting_status = (
                        RunStatus.AWAITING_APPROVAL
                        if gate == PAID_API_GATE
                        else RunStatus.AWAITING_COST_OVERRIDE
                    )
                    approval_run, consumed = self._handle_approval(
                        session, task, request, active_grant, waiting_status
                    )
                    if approval_run is not None:
                        return approval_run
                    if consumed:
                        # One grant satisfies exactly one request.
                        active_grant = None
                    clear_issues(task, *GATE_ISSUE_KINDS)
                    # Re-enter preflight so every remaining monetary gate is
                    # evaluated. Each resolved gate now has its own grant, so
                    # this converges instead of looping.
                    self._checkpoint(
                        session,
                        task,
                        "monetary_gate_approved",
                        {"gate": gate, "request_id": request.id},
                    )
                    continue
                # No monetary gate remains: record the exact policy inputs that
                # were cleared so execution can prove it ran on this estimate.
                task.preferences[BILLING_PREFLIGHT_KEY] = estimate.gate_state
                clear_issues(task, *GATE_ISSUE_KINDS)
                mark_stage_completed(task, LoopStage.COST_PREFLIGHT)
                task.stage = LoopStage.TOOL_SELECTION
                self._checkpoint(
                    session,
                    task,
                    "cost_preflight_passed",
                    {
                        "ledger_month": snapshot.month,
                        "measured_month_usd": format_usd(snapshot.measured_usd),
                        "reserved_month_usd": format_usd(snapshot.reserved_usd),
                        "current_month_usd": format_usd(snapshot.total_usd),
                        "monetary_max_usd": format_usd(usd_from_nano(monetary_maximum)),
                        "projected_usd": format_usd(projected),
                        "billing_class": estimate.billing_class.value,
                        "billing_evidence": estimate.billing_evidence,
                        "resource_ceiling_usd": estimate.resource_ceiling_usd,
                    },
                )
                continue

            if task.stage is LoopStage.TOOL_SELECTION:
                try:
                    availability = self._validated_availability(
                        connector.availability()
                    )
                except Exception as exc:  # noqa: BLE001 - stage boundary
                    # A probe that raises - or returns something that is not an
                    # Availability - is an unavailable tool, never an excuse to
                    # continue toward the connector boundary.
                    return self._stage_failure(
                        session,
                        task,
                        LoopStage.TOOL_SELECTION,
                        ErrorCode.CONNECTOR_UNAVAILABLE,
                        exc,
                    )
                if not availability.available:
                    return self._terminal_failure(
                        session,
                        task,
                        StructuredError(
                            ErrorCode.CONNECTOR_UNAVAILABLE,
                            availability.reason or "Connector unavailable",
                            retryable=True,
                        ),
                    )
                mark_stage_completed(task, LoopStage.TOOL_SELECTION)
                task.stage = LoopStage.EXECUTE
                self._checkpoint(
                    session,
                    task,
                    "tool_selected",
                    {"connector": connector.manifest.name},
                )
                continue

            if task.stage is LoopStage.EXECUTE:
                # M7: the durable source identity is judged again here, before
                # any new authority, claim, attempt or side effect can be
                # created. A PREPARED attempt left by an earlier crash is
                # simply never claimed; H2 is not consulted or changed.
                identity_failure = self._write_identity_failure(session, task)
                if identity_failure is not None:
                    return identity_failure
                # M10: the same judgement for the overwrite preimage, and for
                # the same reason - it precedes open_execution_attempt and
                # begin_execution, so a tampered reference opens no attempt and
                # a stale grant can never reach the mutation.
                preimage_failure = self._overwrite_preimage_failure(session, task)
                if preimage_failure is not None:
                    return preimage_failure
                # M8: the same judgement for the frozen read bundle, and for
                # the same reason - it precedes open_execution_attempt and
                # begin_execution, so a damaged bundle opens no attempt and
                # never becomes a context-less external model request.
                read_context_failure = self._local_read_context_failure(
                    session, task
                )
                if read_context_failure is not None:
                    return read_context_failure
                # Last gate before the connector runs. A persisted task can be
                # resumed straight into this stage, so the billing route is
                # re-derived here and never taken from the stored estimate.
                billing_failure = self._billing_route_failure(session, task, connector)
                if billing_failure is not None:
                    return billing_failure
                if not self._billing_preflight_is_current(task):
                    repreflight = self._require_billing_preflight(session, task)
                    if repreflight is not None:
                        return repreflight
                    continue
                # H3: the exposure the claim will be arbitrated against. The
                # authoritative check happens again inside the claim
                # transaction; this snapshot only builds the exact projection
                # the gates and the claim are bound to.
                exposure = self._ledger_exposure(session, task)
                if isinstance(exposure, AgentRun):
                    return exposure
                snapshot, reservation = exposure
                projected = usd_from_nano(snapshot.total_nano_usd + reservation)
                # The persisted stage proves nothing. The attestation of what
                # will leave, to where, under which gates and from which local
                # boundary is recomputed here and must be backed by a consumed
                # grant (or be policy-exempt); otherwise the task goes back to
                # the gate it lacks and a fresh approval is required.
                open_stage, current = self._unsatisfied_gate(
                    session, task, connector, projected
                )
                if isinstance(open_stage, AgentRun):
                    return open_stage
                if open_stage is not None:
                    regate = self._require_regate(
                        session,
                        task,
                        stage=open_stage,
                        event_type="permission_attestation_required",
                        error_code=ErrorCode.STALE_GRANT,
                        message=(
                            "The approved attestation keeps diverging from the "
                            "execution-time state; refusing to run an external call."
                        ),
                        payload={"reason": "missing_or_stale_gate_attestation"},
                    )
                    if regate is not None:
                        return regate
                    continue

                # M5: make the attested write metadata durable BEFORE the
                # boundary, so an attempt that ends in doubt has something
                # authoritative to be diagnosed against. Fail closed: no
                # write boundary is crossed without it.
                unprepared = self._prepare_local_write(session, task, connector)
                if unprepared is not None:
                    return unprepared

                # H2: durably record the attempt (PREPARED) and atomically
                # claim single ownership (PREPARED -> IN_FLIGHT + one-shot grant
                # consumption) before crossing the connector boundary.
                attempt = self.memory.open_execution_attempt(
                    ExecutionAttempt(
                        session_id=session.id,
                        task_id=task.id,
                        connector=connector.manifest.name,
                        fingerprint=current.fingerprint,
                    )
                )
                if attempt.state is not AttemptState.PREPARED:
                    # Another owner raced ahead, or the attempt is in doubt.
                    return self._execution_in_doubt(session, task, attempt)
                one_shots, override = self._execution_one_shot_fingerprints(
                    session, task, connector, current, projected
                )
                # H3: the cap check and the reservation happen inside the same
                # transaction as the attempt CAS and the grant consumption, so
                # two concurrent metered tasks can never both clear the cap
                # against the same balance.
                claim = BillingClaim(
                    ledger_month=snapshot.month,
                    expected_exposure_nano_usd=snapshot.total_nano_usd,
                    reservation_nano_usd=reservation,
                    limit_nano_usd=self._limit_nano_usd(),
                    billing_class=task.estimate.billing_class,
                    gate_state=task.estimate.gate_state,
                    cost_override_fingerprint=override,
                )
                try:
                    outcome = self.memory.begin_execution(
                        attempt.id, session.id, task.id, one_shots, claim
                    )
                except BillingIntegrityError as exc:
                    # The storage boundary refused the claim structurally; it
                    # wrote nothing and the connector was never reached.
                    return self._billing_integrity_failure(session, task, str(exc))
                if outcome is ExecutionClaimOutcome.LEDGER_INVALID:
                    return self._billing_integrity_failure(
                        session, task, "ledger_state_invalid"
                    )
                if outcome is ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED:
                    # The ledger moved after preflight or approval: no attempt
                    # was claimed, no grant consumed, no reservation created and
                    # the connector boundary was not crossed. Go back to cost
                    # preflight, which re-derives the exact current projection
                    # and issues a fresh cost request for it.
                    regate = self._require_regate(
                        session,
                        task,
                        stage=LoopStage.COST_PREFLIGHT,
                        event_type="billing_recheck_required",
                        error_code=ErrorCode.BILLING_INTEGRITY_FAILED,
                        message=(
                            "The monthly monetary exposure keeps changing between "
                            "approval and execution; refusing to run a paid call."
                        ),
                        payload={"reason": "ledger_exposure_changed"},
                    )
                    if regate is not None:
                        return regate
                    continue
                if outcome is not ExecutionClaimOutcome.CLAIMED:
                    # The atomic claim failed: another owner won, an
                    # authoritative cancellation/terminal decision won first, or
                    # a required one-shot grant is gone. Reload and report the
                    # authoritative state; never execute here.
                    fresh = self.memory.load_task(task.id)
                    if fresh is not None:
                        terminal = self._terminal_run(session, fresh)
                        if terminal is not None:
                            return terminal
                    return AgentRun(
                        RunStatus.FAILED,
                        session,
                        fresh or task,
                        error=StructuredError(
                            ErrorCode.EXECUTION_IN_PROGRESS,
                            "The execution claim could not be acquired; "
                            "not executing.",
                            retryable=True,
                        ),
                    )

                # Durable IN_FLIGHT plus a durable reservation are committed;
                # cross the connector boundary.
                task.status = TaskStatus.RUNNING
                unknowable_on_raise = (
                    current.side_effect.effect in _UNKNOWABLE_ON_RAISE_EFFECTS
                )
                # S2B. The claim is won and the reservation is committed, so
                # THIS invocation is the execution owner and the only actor
                # entitled to ask its own live operation to stop. The watcher
                # exists for exactly the span of the blocking call below and
                # is joined before any lifecycle work resumes; nothing it does
                # is persisted, and nothing downstream reads its outcome.
                stop_watcher = _LiveStopWatcher(
                    self.memory, session.id, task.id, connector
                )
                stop_watcher.start()
                # The call, and NOTHING else. The exception is captured rather
                # than handled here: handling it would mean marking an attempt
                # UNKNOWN - a lifecycle write - while the watcher could still
                # be inside a connector stop. Everything waits for the barrier
                # in the finally below.
                execute_error: Exception | None = None
                try:
                    result = connector.execute(task, session)
                except Exception as exc:  # noqa: BLE001 - connector boundary
                    execute_error = exc
                finally:
                    # THE BARRIER. Authority is revoked and any in-flight
                    # connector stop has finished before one byte of lifecycle
                    # state is touched.
                    stop_watcher.stop()
                if execute_error is not None:
                    exc = execute_error
                    if unknowable_on_raise:
                        # The outcome is unknowable after a raise: mark the
                        # attempt non-replayable rather than inventing a clean
                        # failure that could be safely retried. The
                        # reservation stays active, so the money the call may
                        # already have spent keeps counting as exposure.
                        self.memory.mark_execution_unknown(attempt.id)
                        return self._execution_in_doubt(
                            session,
                            task,
                            self.memory.latest_execution_attempt(task.id),
                        )
                    # H4-E: the exception object never reaches storage. Its
                    # message is normalized into the safe diagnostic contract
                    # once and reused, so the summary and the error carry the
                    # same bounded, credential- and path-scrubbed text.
                    failure = diagnostic_from_exception(
                        ErrorCode.CONNECTOR_FAILED,
                        exc,
                        retryable=True,
                        stage=LoopStage.EXECUTE,
                    )
                    result = ToolResult(
                        outcome=OutcomeStatus.ERROR,
                        summary=failure.message,
                        error=failure,
                        usage=UsageRecord(
                            connector=connector.manifest.name,
                            billing_class=task.estimate.billing_class,
                            # An unbilled route cannot have spent money, so the
                            # monetary state is known even though the call
                            # failed. A metered route stays UNKNOWN and fails
                            # closed with its reservation retained.
                            monetary_state=(
                                MonetaryState.NOT_APPLICABLE
                                if task.estimate.billing_class
                                in (
                                    BillingClass.SUBSCRIPTION_INCLUDED,
                                    BillingClass.NO_ADDITIONAL_CHARGE,
                                )
                                else MonetaryState.UNKNOWN
                            ),
                            billing_evidence=task.estimate.billing_evidence,
                        ),
                    )
                # M3: the typed local-read snapshot is lifted off the result
                # HERE, before ``_sanitize_result`` reduces ``data`` to
                # ``next_step``. Deliberately non-raising, exactly like
                # ``_coerced_verification``: this runs after the connector
                # boundary and before H3 reconciliation, so a malformed
                # snapshot is discarded rather than raised. The connector's
                # own verifier then fails the task closed at VERIFY, with the
                # reservation already reconciled.
                self._bind_local_read(task, connector, result)
                # M5: the same lift, for the same reason and at the same
                # point - before ``_sanitize_result`` reduces ``data``.
                self._bind_local_write(task, connector, result)
                # M6: the third lift, same point, same reason. This one is
                # what stops an external model's answer from being reduced to
                # the H4 diagnostic summary before the user ever sees it.
                self._bind_model_answer(task, connector, result)
                # H4: connector-authored free-form text is bounded and
                # scrubbed before it can become persisted task, decision or
                # audit state. It never touches approval, billing, execution or
                # progress authority.
                self._sanitize_result(result)
                # H2-H/H3: usage is reconciled first, so a crash before the
                # atomic conclusion leaves the attempt IN_FLIGHT
                # (non-replayable) with the authoritative usage preserved. The
                # connector's own billing class, id and timestamp are never
                # ledger authority: the route bound into the claim is.
                reconciled = self._reconcile_usage(
                    session, task, connector, attempt.id, result
                )
                if isinstance(reconciled, AgentRun):
                    return reconciled
                task.result_snapshot = self._result_to_dict(result)

                # H2-F: reload authoritative cancellation before committing a
                # terminal COMPLETED/FAILED. A stale result never overwrites an
                # authoritative cancellation, while the usage recorded above
                # stays durably accounted.
                fresh = self.memory.load_task(task.id)
                cancelled = result.outcome is OutcomeStatus.CANCELLED or (
                    fresh is not None
                    and (
                        fresh.cancellation_requested
                        or fresh.status
                        in (TaskStatus.CANCEL_REQUESTED, TaskStatus.CANCELLED)
                    )
                )
                if cancelled:
                    return self._finalize_cancelled(session, task, attempt.id, result)
                overspend = self._overspend_failure(task, result)
                if overspend is not None:
                    # The full actual cost is already recorded and reconciled;
                    # only the task fails. Later metered work therefore observes
                    # the real exposure this call created.
                    return self._fail_execution(
                        session, task, attempt.id, result, overspend
                    )
                if not result.ok:
                    error = result.error or StructuredError(
                        ErrorCode.CONNECTOR_FAILED, result.summary
                    )
                    return self._fail_execution(
                        session, task, attempt.id, result, error
                    )
                mark_stage_completed(task, LoopStage.EXECUTE)
                task.status = TaskStatus.VERIFYING
                task.stage = LoopStage.VERIFY
                outcome = self.memory.conclude_execution(
                    session, task, attempt.id, "execution_completed", {}
                )
                if outcome == "cancelled":
                    return self._finalize_cancelled(session, task, attempt.id, result)
                if outcome == "lost":
                    return self._execution_in_doubt(
                        session, task, self.memory.latest_execution_attempt(task.id)
                    )
                continue

            if task.stage is LoopStage.VERIFY:
                result = self._result_from_dict(task.result_snapshot)
                if result is None:
                    return self._terminal_failure(
                        session,
                        task,
                        StructuredError(
                            ErrorCode.VERIFICATION_FAILED,
                            "Execution result is missing at verification checkpoint.",
                        ),
                    )
                try:
                    # H4-C: invoking the verifier and validating what it
                    # returned are ONE narrow boundary. Validation happens
                    # before any field-specific access, so a malformed return
                    # value (None, a dict, a raw-string status, malformed
                    # evidence) fails deterministically here instead of raising
                    # an AttributeError that would strand the task at VERIFY
                    # with a FINISHED attempt and invite a re-verify on restart.
                    verification = self._validated_verification(
                        self.verifier.verify(task, result, session)
                        if self.verifier is not None
                        else connector.verify(task, result, session)
                    )
                except Exception as exc:  # noqa: BLE001 - stage boundary
                    # H2/H3 already own the execution journal and the ledger by
                    # this point; a verifier fault only fails verification. The
                    # attempt stays FINISHED, the connector is never re-run and
                    # the reconciled usage/reservation is untouched.
                    return self._stage_failure(
                        session,
                        task,
                        LoopStage.VERIFY,
                        ErrorCode.VERIFICATION_FAILED,
                        exc,
                        result=result,
                    )
                result.verification = verification
                task.result_snapshot = self._result_to_dict(result)
                if not verification.passed:
                    return self._terminal_failure(
                        session,
                        task,
                        StructuredError(
                            ErrorCode.VERIFICATION_FAILED,
                            "Result could not be verified with explicit evidence.",
                            details=verification.to_dict(),
                        ),
                        result,
                    )
                mark_stage_completed(task, LoopStage.VERIFY)
                task.stage = LoopStage.PRESENT
                self._checkpoint(
                    session,
                    task,
                    "verification_passed",
                    verification.to_dict(),
                )
                continue

            if task.stage is LoopStage.PRESENT:
                result = self._result_from_dict(task.result_snapshot)
                if result is None:
                    return self._terminal_failure(
                        session,
                        task,
                        StructuredError(ErrorCode.CONNECTOR_FAILED, "Missing result"),
                    )
                # Only an actual string survives, bounded and credential-safe;
                # arbitrary connector data is never stringified into session
                # state.
                session.next_step = normalize_session_next_step(
                    result.data.get("next_step")
                )
                mark_stage_completed(task, LoopStage.PRESENT)
                task.stage = LoopStage.PERSIST
                self._checkpoint(
                    session,
                    task,
                    "result_presented",
                    {
                        "summary": sanitize_diagnostic_text(result.summary),
                        "next_step": session.next_step,
                    },
                )
                continue

            if task.stage is LoopStage.PERSIST:
                result = self._result_from_dict(task.result_snapshot)
                if result is None:
                    return self._terminal_failure(
                        session,
                        task,
                        StructuredError(ErrorCode.CONNECTOR_FAILED, "Missing result"),
                    )
                # PERSIST succeeded; COMPLETED is the terminal state, not an
                # executed work stage, and is never recorded as completed.
                mark_stage_completed(task, LoopStage.PERSIST)
                clear_issues(task, *ProgressIssueKind)
                task.status = TaskStatus.COMPLETED
                task.stage = LoopStage.COMPLETED
                session.active_task_id = None
                # M3: a local read records EVIDENCE, not a judgement. A
                # decision is trusted inherited history - later tasks in the
                # session retrieve it as context - so a read that merely
                # observed a file has nothing to contribute and finalizes
                # with no decision at all. Not a blanked or sentinel record:
                # the row is never written, so nothing can be retrieved from
                # it and nothing has to be cleaned up afterwards. Everything
                # else about finalization is unchanged, and every other
                # capability still writes its decision exactly as before.
                decision = (
                    None
                    # M5 joins M3 here for the same reason: a write is an
                    # ACTION that produced evidence, not a judgement later
                    # work should inherit as trusted history. The row is
                    # never written, so retrieval has nothing to filter and
                    # nothing to clean up.
                    if task.capability
                    in (LOCAL_READ_CAPABILITY, LOCAL_WRITE_CAPABILITY)
                    else DecisionRecord(
                        session_id=session.id,
                        task_id=task.id,
                        summary=result.summary,
                        # Bounded and scrubbed: a decision record is the
                        # record future work inherits as trusted history.
                        rationale=sanitize_diagnostic_text(
                            f"Connector={connector.manifest.name}; "
                            f"verification={result.verification.method}; "
                            f"assumption={task.visible_assumption}"
                        ),
                        next_step=session.next_step,
                    )
                )
                # All-or-nothing with lifecycle authority: if a cancellation
                # already won the terminal transition, no decision, usage
                # rewrite, terminal event or session overwrite is persisted and
                # the authoritative state is reported instead of a phantom
                # COMPLETED built on this stale task.
                try:
                    finalized = self.memory.finalize_task(
                        session,
                        task,
                        decision,
                        result.usage,
                        "task_completed",
                        {
                            "summary": sanitize_diagnostic_text(result.summary),
                            "next_step": session.next_step,
                        },
                    )
                except BillingIntegrityError as exc:
                    # A tampered/legacy snapshot whose restored usage cannot be
                    # written must not raise out of resume: it becomes the same
                    # deterministic terminal failure as any other stage fault,
                    # with no decision and no completion event.
                    return self._stage_failure(
                        session,
                        task,
                        LoopStage.PERSIST,
                        ErrorCode.BILLING_INTEGRITY_FAILED,
                        exc,
                        result=result,
                    )
                if not finalized:
                    return self._authoritative_run(session, task)
                return AgentRun(RunStatus.COMPLETED, session, task, result=result)

            if task.stage is LoopStage.COMPLETED:
                return AgentRun(
                    RunStatus.COMPLETED,
                    session,
                    task,
                    result=self._result_from_dict(task.result_snapshot),
                )
            if task.stage is LoopStage.CANCELLED:
                return AgentRun(RunStatus.CANCELLED, session, task)
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    ErrorCode.INVALID_TASK, f"Unsupported loop stage: {task.stage.value}"
                ),
            )

    # -- H3: monetary exposure, reservation and usage reconciliation --------

    def _limit_nano_usd(self) -> int:
        """The fixed monthly hard limit as exact integer nano-USD."""
        return nano_usd(self.settings.monthly_hard_limit_usd, "monthly_hard_limit_usd")

    def _reservation_nano_usd(self, task: Task) -> int:
        """Money this execution may add to the monthly ledger, in nano-USD.

        Only a genuinely metered route reserves anything; a subscription or
        no-additional-charge route reserves exactly zero and still gets its
        zero-valued attempt billing record for route/idempotency binding.
        """
        if task.estimate.billing_class is not BillingClass.METERED_MONETARY:
            return 0
        return nano_usd(validated_metered_maximum(task.estimate.maximum_usd))

    def _ledger_exposure(
        self, session: SessionState, task: Task
    ) -> tuple[LedgerSnapshot, int] | AgentRun:
        """The authoritative snapshot plus this task's reservation, or a
        terminal failure when either cannot be established."""
        try:
            reservation = self._reservation_nano_usd(task)
        except BillingIntegrityError as exc:
            return self._billing_integrity_failure(session, task, str(exc))
        snapshot = self.memory.ledger_snapshot()
        if not snapshot.valid:
            return self._billing_integrity_failure(
                session, task, f"ledger_invalid:{snapshot.reason}"
            )
        return snapshot, reservation

    def _billing_integrity_failure(
        self, session: SessionState, task: Task, reason: str
    ) -> AgentRun:
        """Fail a task closed on a monetary/ledger integrity violation.

        ``reason`` is a short non-secret rule tag; no connector telemetry,
        payload or credential material is ever carried into the audit trail.
        """
        return self._terminal_failure(
            session,
            task,
            StructuredError(
                ErrorCode.BILLING_INTEGRITY_FAILED,
                "Monetary integrity could not be established; refusing to "
                "record or continue this execution.",
                details={"reason": reason[:BILLING_REASON_MAX_CHARS]},
            ),
        )

    def _fail_execution(
        self,
        session: SessionState,
        task: Task,
        attempt_id: str,
        result: ToolResult,
        error: StructuredError,
    ) -> AgentRun:
        """Conclude a claimed attempt as a terminal task failure.

        Never reaches VERIFY/PRESENT, so no decision record and no
        ``task_completed`` event can be persisted for it.
        """
        task.status = TaskStatus.FAILED
        task.stage = LoopStage.FAILED
        session.active_task_id = None
        set_issue(task, ProgressIssueKind.FAILURE, error.code, error.retryable)
        outcome = self.memory.conclude_execution(
            session, task, attempt_id, "task_failed", error.to_dict()
        )
        if outcome == "cancelled":
            return self._finalize_cancelled(session, task, attempt_id, result)
        if outcome == "lost":
            return self._execution_in_doubt(
                session, task, self.memory.latest_execution_attempt(task.id)
            )
        return AgentRun(RunStatus.FAILED, session, task, result=result, error=error)

    def _overspend_failure(
        self, task: Task, result: ToolResult
    ) -> StructuredError | None:
        """Whether a valid measured actual exceeded the approved maximum.

        The full amount is already recorded and reconciled; the task still
        fails, so an execution that outran its approval can never present a
        completed decision.
        """
        if task.estimate.billing_class is not BillingClass.METERED_MONETARY:
            return None
        actual = result.usage.monetary_cost_usd
        if actual is None:
            return None
        try:
            measured = validated_measured_cost(actual)
            approved = validated_metered_maximum(task.estimate.maximum_usd)
        except BillingIntegrityError as exc:
            return StructuredError(
                ErrorCode.BILLING_INTEGRITY_FAILED,
                "The measured cost of this execution could not be validated.",
                details={"reason": str(exc)[:BILLING_REASON_MAX_CHARS]},
            )
        if measured <= approved:
            return None
        return StructuredError(
            ErrorCode.BILLING_INTEGRITY_FAILED,
            "The measured cost of this execution exceeded the approved "
            "maximum; the full amount was recorded and the task failed.",
            details={
                "reason": "actual_above_approved_maximum",
                "approved_maximum_usd": format_usd(approved),
                "measured_usd": format_usd(measured),
            },
        )

    def _reconcile_usage(
        self,
        session: SessionState,
        task: Task,
        connector: BaseConnector,
        attempt_id: str,
        result: ToolResult,
    ) -> AgentRun | None:
        """Bind the connector result to the attempt's authoritative reservation.

        The execution-time route bound into the claim is the authority; the
        connector's reported classification, monetary state, usage id and
        timestamp are telemetry only and can never downgrade, replace or
        backdate it. On any mismatch the reservation is retained as unresolved
        exposure and the task fails closed.
        """
        route = task.estimate.billing_class
        try:
            reconciliation = self._build_reconciliation(
                session, task, connector, attempt_id, result, route
            )
        except BillingIntegrityError as exc:
            return self._unresolved_billing_failure(
                session, task, attempt_id, result, str(exc)
            )
        outcome = self.memory.reconcile_usage(reconciliation)
        if not outcome.ok or outcome.usage is None:
            return self._unresolved_billing_failure(
                session,
                task,
                attempt_id,
                result,
                outcome.reason or outcome.outcome.value,
            )
        # The persisted authoritative record replaces whatever the connector
        # reported, so presentation, the result snapshot and the terminal
        # finalization all describe the row that actually entered the ledger.
        result.usage = outcome.usage
        return None

    @staticmethod
    def _build_reconciliation(
        session: SessionState,
        task: Task,
        connector: BaseConnector,
        attempt_id: str,
        result: ToolResult,
        route: BillingClass,
    ) -> BillingReconciliation:
        """Bind connector telemetry to the authoritative execution-time route.

        The connector is an *observer* here, never an authority. Its reported
        billing class may confirm the route but can never downgrade, upgrade or
        replace it; its class/state/money combination must satisfy the exact
        matrix for the authoritative route; and its connector name, call count
        and evidence string are discarded in favour of the selected connector
        manifest, the single claimed execution and the execution-time estimate.
        Anything else fails closed before a single ledger row is written.
        """
        usage = result.usage
        # Exact runtime enums: an externally constructed record carrying raw
        # strings fails as billing integrity, not as an attribute error.
        reported_class = require_billing_class(usage.billing_class)
        reported_state = require_monetary_state(usage.monetary_state)
        if reported_class is not BillingClass.UNKNOWN and reported_class is not route:
            # UNKNOWN means "the connector reported no route"; any other value
            # that disagrees with the attested execution route is a downgrade
            # or upgrade attempt and is refused.
            raise BillingIntegrityError("connector route contradicts the claim")
        if route is BillingClass.METERED_MONETARY:
            if reported_state is not MonetaryState.MEASURED:
                raise BillingIntegrityError("metered actual is not measured")
            if usage.monetary_cost_usd is None:
                # A provider "reference" figure can never stand in for a
                # measured metered cost.
                raise BillingIntegrityError("metered actual is missing an amount")
            actual_nano: int | None = nano_usd(
                validated_measured_cost(usage.monetary_cost_usd)
            )
            monetary_state = MonetaryState.MEASURED
            reference: float | None = None
        elif route in (
            BillingClass.SUBSCRIPTION_INCLUDED,
            BillingClass.NO_ADDITIONAL_CHARGE,
        ):
            if reported_state is not MonetaryState.NOT_APPLICABLE:
                raise BillingIntegrityError("unbilled route is not not_applicable")
            if usage.monetary_cost_usd is not None:
                raise BillingIntegrityError("unbilled route reported a monetary amount")
            actual_nano = 0
            monetary_state = MonetaryState.NOT_APPLICABLE
            # A provider-reported USD equivalent for unbilled usage is kept as
            # reference telemetry only; a malformed one is simply dropped.
            reference = None
            if usage.reference_cost_usd is not None:
                try:
                    reference = float(
                        canonical_usd(usage.reference_cost_usd, "reference_cost_usd")
                    )
                except BillingIntegrityError:
                    reference = None
        else:
            raise BillingIntegrityError("unknown route cannot be reconciled")
        return BillingReconciliation(
            attempt_id=attempt_id,
            session_id=session.id,
            task_id=task.id,
            # Authoritative identity and evidence: the selected connector
            # manifest and the execution-time estimate, never the strings the
            # connector put in its own telemetry.
            connector=connector.manifest.name,
            billing_class=route,
            monetary_state=monetary_state,
            actual_nano_usd=actual_nano,
            reference_cost_usd=reference,
            provider=usage.provider,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost_usd=float(
                canonical_usd(task.estimate.monetary_maximum_usd, "estimated_cost_usd")
            ),
            billing_evidence=task.estimate.billing_evidence,
        )

    def _unresolved_billing_failure(
        self,
        session: SessionState,
        task: Task,
        attempt_id: str,
        result: ToolResult,
        reason: str,
    ) -> AgentRun:
        """Retain the reservation and fail the task closed.

        The exposure stays part of the monthly position until valid
        reconciliation or explicit future recovery; H3 releases nothing
        automatically.
        """
        self.memory.mark_reservation_unresolved(attempt_id)
        error = StructuredError(
            ErrorCode.BILLING_INTEGRITY_FAILED,
            "The execution result could not be reconciled with its authoritative "
            "billing route; the reserved exposure was retained.",
            details={"reason": reason[:BILLING_REASON_MAX_CHARS]},
        )
        task.result_snapshot = self._result_to_dict(result)
        return self._fail_execution(session, task, attempt_id, result, error)

    # -- H4: persisted diagnostic hygiene ----------------------------------

    def _stage_failure(
        self,
        session: SessionState,
        task: Task,
        stage: LoopStage,
        code: ErrorCode,
        exc: BaseException,
        *,
        retryable: bool = False,
        result: ToolResult | None = None,
    ) -> AgentRun:
        """Normalize one stage-boundary exception into deterministic state.

        Narrow by construction: each call site wraps exactly one boundary call,
        so a lifecycle bug elsewhere still surfaces instead of being swallowed.
        The task reaches a terminal failure with a safe typed diagnostic, and
        because that state is terminal a restart reports it rather than
        retrying the same malformed input forever.

        H2 attempt authority is untouched: this helper is never used where an
        execution claim is in doubt, so ``UNKNOWN_OUTCOME`` keeps its own
        meaning and is never reinterpreted as an ordinary diagnostic failure.
        """
        return self._terminal_failure(
            session,
            task,
            diagnostic_from_exception(code, exc, retryable=retryable, stage=stage),
            result,
        )

    @staticmethod
    def _validated_retrieved_context(value: Any) -> RetrievedContext:
        """Require exactly the typed retrieval snapshot the contract declares.

        Validated *recursively* and before any field access, so a ``None``, a
        dict, a raw string, or an exact ``RetrievedMemoryItem`` whose ``kind``
        or ``classification`` is a raw string becomes a deterministic boundary
        failure here instead of an ``AttributeError`` raised later by
        ``to_dict``, when the task has already accepted the snapshot and the
        checkpoint is being serialized outside this boundary.

        The result/item/count/character ceilings are the same hard retrieval
        contract ``build_retrieved_context`` enforces: a snapshot claiming more
        items or more characters than retrieval can ever produce is tampered
        data, not a wider window.
        """
        if type(value) is not RetrievedContext:
            raise TypeError("retrieval did not return a RetrievedContext")
        _exact_type(value.items, tuple, "retrieved context items")
        if len(value.items) > RETRIEVAL_MAX_ITEMS:
            raise TypeError("retrieved context exceeds the hard result ceiling")
        for item in value.items:
            _validated_retrieved_item(item)
        _exact_count(value.candidate_count, "retrieved context candidate count")
        _exact_count(
            value.character_count,
            "retrieved context character count",
            cap=RETRIEVAL_MAX_CHARACTERS,
        )
        _exact_flag(value.truncated, "retrieved context truncated flag")
        return value

    @staticmethod
    def _validated_local_read(value: Any) -> LocalReadSnapshot:
        """Require exactly the typed local-read snapshot, before any field use.

        The same H4-C boundary as retrieval, for the same reason: this object
        crosses the connector boundary and is then serialized into task JSON
        outside the try block, so a malformed field must fail here rather than
        raise while a checkpoint is being written.

        P1-5: the structural contract itself is NOT restated here. There is
        exactly one - ``local_read_snapshot_refusal`` in ``jarvis.contracts`` -
        and the load boundary, this execution boundary, the public envelope
        and the Claude egress boundary all call that same function, so four
        subtly different validators cannot drift apart. This method only
        turns its refusal into the ``TypeError`` this stage boundary expects.

        ``classification`` is validated as an exact enum by that contract and
        is then *replaced* by the caller from trusted authority (the reading
        task's scope, or the SOURCE TASK's scope on the cross-task hop), so
        nothing a connector or a persisted row puts in it carries authority.
        """
        reason = local_read_snapshot_refusal(value)
        if reason is not None:
            raise TypeError(f"local read snapshot is invalid: {reason}")
        return value

    @staticmethod
    def _validated_model_answer(value: Any) -> ModelAnswer:
        """Require exactly the typed model answer, before any field use.

        The structural contract is NOT restated here. There is exactly one -
        ``model_answer_refusal`` in ``jarvis.contracts`` - and the load
        boundary, this execution boundary, the connector's verifier and the
        public envelope all call that same function, so four subtly different
        validators cannot drift apart. This method only turns its refusal into
        the ``TypeError`` the binding boundary expects.

        The exact-type check inside that contract is load-bearing rather than
        decorative: this object crosses the connector boundary and is then
        serialized into task JSON, so anything that merely resembles a
        ``ModelAnswer`` must fail here rather than while a checkpoint is
        being written.
        """
        reason = model_answer_refusal(value)
        if reason is not None:
            raise TypeError(f"model answer is invalid: {reason}")
        return value

    @staticmethod
    def _validated_availability(value: Any) -> Availability:
        """Require exactly the typed availability the connector contract declares."""
        if type(value) is not Availability:
            raise TypeError("availability probe did not return an Availability")
        if not isinstance(value.available, bool):
            raise TypeError("availability flag is not a boolean")
        if not isinstance(value.reason, str):
            raise TypeError("availability reason is not a string")
        return value

    @staticmethod
    def _validated_permission_request(value: Any) -> PermissionRequest:
        """Require exactly the typed permission request, recursively.

        Every enum, scope, preview and attestation field is checked before use,
        so a malformed request can never reach fingerprinting, be persisted as
        a pending approval row, be displayed for approval, or authorize
        execution. An outer type check alone is not enough: an exact
        ``OutboundDataPreview`` carrying ``data_classification="internal"``
        passes it and then raises ``AttributeError`` on ``.value`` during
        fingerprint or display serialization - outside this boundary, after a
        pending approval would already exist.
        """
        if type(value) is not PermissionRequest:
            raise TypeError("connector did not return a PermissionRequest")
        _exact_text(value.task_id, "permission request task id")
        _exact_text(value.session_id, "permission request session id")
        _validated_scope(value.scope, "permission request scope")
        _exact_type(value.risk_level, RiskLevel, "permission request risk level")
        _validated_side_effect(value.side_effect, "permission request side effect")
        _exact_text(value.reason, "permission request reason")
        if not isinstance(value.id, str) or not value.id:
            raise TypeError("permission request has no issuance identity")
        if not isinstance(value.expires_at, str) or not value.expires_at:
            raise TypeError("permission request has no expiry")
        if value.attestation is not None:
            _validated_attestation(value.attestation)
        if value.outbound_preview is not None:
            _validated_outbound_preview(value.outbound_preview)
        return value

    @staticmethod
    def _validated_verification(record: Any) -> VerificationRecord:
        """Validate a verifier return value, then bound and scrub it.

        Every structural check happens before any field-specific access, so a
        malformed value can never reach an attribute lookup: the object must be
        exactly a ``VerificationRecord``, its status exactly a
        ``VerificationStatus`` member (a raw string or a foreign enum is not
        equivalent), its method a string, and its evidence a sequence of
        strings. Anything else raises, and the caller's stage boundary turns
        that into a deterministic terminal verification failure.
        """
        if type(record) is not VerificationRecord:
            raise TypeError("verifier did not return a VerificationRecord")
        if type(record.status) is not VerificationStatus:
            raise TypeError("verification status is not a VerificationStatus")
        if not isinstance(record.method, str):
            raise TypeError("verification method is not a string")
        evidence = record.evidence
        if not isinstance(evidence, (tuple, list)):
            raise TypeError("verification evidence is not a sequence")
        if not all(isinstance(item, str) for item in evidence):
            raise TypeError("verification evidence is not a sequence of strings")
        return VerificationRecord(
            status=record.status,
            method=sanitize_diagnostic_text(record.method, DIAGNOSTIC_DETAIL_MAX_CHARS),
            evidence=tuple(
                sanitize_diagnostic_text(item, DIAGNOSTIC_DETAIL_MAX_CHARS)
                for item in evidence[:VERIFICATION_MAX_EVIDENCE]
            ),
        )

    @classmethod
    def _coerced_verification(cls, record: Any) -> VerificationRecord:
        """Total variant used where raising would strand lifecycle state.

        A connector may attach a verification record to its own ``ToolResult``;
        that is not the authoritative verification, which happens at VERIFY. A
        malformed one is discarded rather than raised, because this runs after
        the connector boundary was crossed and before H3 reconciliation, where
        an exception would leave the execution in doubt.
        """
        try:
            return cls._validated_verification(record)
        except Exception:  # noqa: BLE001 - discard, never strand the lifecycle
            return VerificationRecord()

    @classmethod
    def _sanitize_result(cls, result: ToolResult) -> None:
        """Bound and scrub connector-authored free-form result text in place.

        The summary becomes persisted task state, a decision record and audit
        events; ``data`` is reduced to the single key the loop consumes, so an
        arbitrary connector blob never becomes trusted history.
        """
        result.summary = sanitize_diagnostic_text(result.summary)
        raw = result.data if isinstance(result.data, dict) else {}
        result.data = {
            key: sanitize_diagnostic_text(raw[key])
            for key in ("next_step",)
            # Only an actual string survives; arbitrary connector data is never
            # stringified into persisted or session state.
            if isinstance(raw.get(key), str)
        }
        result.verification = cls._coerced_verification(result.verification)

    def _billing_preflight_is_current(self, task: Task) -> bool:
        """Whether this exact estimate is the one that cleared cost preflight."""
        recorded = task.preferences.get(BILLING_PREFLIGHT_KEY)
        return bool(recorded) and recorded == task.estimate.gate_state

    def _require_billing_preflight(
        self, session: SessionState, task: Task
    ) -> AgentRun | None:
        """Send a task back through monetary preflight, with a loop bound."""
        # Approvals were granted against the superseded policy inputs; the
        # gate attestations no longer match, so every applicable monetary
        # gate is re-evaluated from scratch.
        stale = task.preferences.pop(BILLING_PREFLIGHT_KEY, None)
        return self._require_regate(
            session,
            task,
            stage=LoopStage.COST_PREFLIGHT,
            event_type="billing_preflight_required",
            error_code=ErrorCode.BILLING_ROUTE_UNKNOWN,
            message=(
                "The billing route keeps changing between preflight and "
                "execution; refusing to run an external call."
            ),
            payload={
                "reason": "stale_or_missing_billing_preflight",
                "previous_fingerprint": stale,
                "current_fingerprint": task.estimate.gate_state,
            },
        )

    def _require_regate(
        self,
        session: SessionState,
        task: Task,
        *,
        stage: LoopStage,
        event_type: str,
        error_code: ErrorCode,
        message: str,
        payload: dict[str, Any],
    ) -> AgentRun | None:
        """Send a task from EXECUTE back to a gate stage, with a loop bound."""
        repeats = int(task.preferences.get(BILLING_REPREFLIGHT_COUNT_KEY, 0)) + 1
        if repeats > BILLING_REPREFLIGHT_LIMIT:
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    error_code, message, details={"regate_attempts": repeats}
                ),
            )
        task.preferences[BILLING_REPREFLIGHT_COUNT_KEY] = repeats
        task.stage = stage
        task.status = TaskStatus.PENDING
        self._checkpoint(
            session, task, event_type, {**payload, "attempt": repeats}
        )
        return None

    def _attested_request(
        self,
        session: SessionState,
        task: Task,
        connector: BaseConnector,
        *,
        scope_override: ActionScope | None = None,
        reason: str | None = None,
    ) -> PermissionRequest | AgentRun:
        """Build the connector's request/attestation for the task as it is now.

        A connector that cannot attest the egress (SECRET classification, a
        local boundary it cannot honour, a conflicting side-effect profile)
        fails the task closed before any approval request can exist.
        """
        try:
            if reason is None:
                # M7: a handoff write names its source and the exact digest of
                # the frozen bytes in the approval reason. Derived from the
                # persisted target fields only - never from the source task -
                # so RISK_PERMISSION and EXECUTE derive byte-identical text,
                # and bound into the fingerprint through ``display_digest``.
                reason = self._handoff_reason(
                    task, connector
                ) or self._local_read_context_reason(task, connector)
            return self._authoritative_request(
                task,
                session,
                connector,
                self._validated_permission_request(
                    connector.permission_request(
                        task,
                        session.id,
                        self.settings.approval_ttl_minutes,
                        scope_override=scope_override,
                        reason=reason,
                    )
                ),
                scope_override,
            )
        except AttestationError as exc:
            return self._terminal_failure(
                session, task, diagnostic_from_exception(exc.code, exc)
            )
        except Exception as exc:  # noqa: BLE001 - stage boundary
            # A connector that cannot build its attestation at all is refused
            # exactly like one that declines to attest.
            return self._stage_failure(
                session,
                task,
                LoopStage.RISK_PERMISSION,
                ErrorCode.EGRESS_REFUSED,
                exc,
            )

    def _authoritative_request(
        self,
        task: Task,
        session: SessionState,
        connector: BaseConnector,
        request: PermissionRequest,
        scope_override: ActionScope | None,
    ) -> PermissionRequest:
        """Require the request to *be* the current authoritative security state.

        Exact field types are not a security property. A connector whose
        manifest declares HIGH / EXTERNAL_EGRESS can return a perfectly typed
        request whose top-level ``risk_level`` is LOW and whose
        ``side_effect`` is a reversible NONE - every field valid, every
        relationship false - and ``_requires_approval`` would then read those
        weakened values, auto-approve under SMART_AUTO and execute an
        unapproved external egress. So the floor is *recomputed here* from the
        connector manifest and the task, and the returned request is never
        allowed to establish it.

        Strengthening stays legal (that is what ``merge_side_effects`` and the
        risk ``max`` already mean); weakening or contradicting is refused, and
        a malformed request is never silently repaired into a strong one. The
        refusal is an ``AttestationError``, so it becomes the same
        deterministic ``EGRESS_REFUSED`` terminal failure as a connector that
        declines to attest - before any pending approval, ``approval_requested``
        event, grant or execution attempt can exist.
        """
        manifest = connector.manifest
        # The authoritative floor. Deliberately built from the manifest and the
        # task, not from ``connector.side_effects()``/``permission_request()``:
        # a connector must not be able to lower its own floor by overriding the
        # method that reports it. A conflicting pair raises out of
        # ``merge_side_effects`` and fails closed exactly like any other
        # unattestable egress.
        floor = merge_side_effects(manifest.default_side_effect, task.side_effect)
        floor_risk = max(task.risk_level, manifest.default_risk)
        base_scope = scope_override or task.scope
        expected_scope = ActionScope(
            action=base_scope.action,
            target=base_scope.target,
            connector=manifest.name,
            destination=base_scope.destination,
            data_classification=base_scope.data_classification,
        ).normalized()

        def refuse(detail: str) -> None:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED,
                f"Permission request does not match the authoritative "
                f"security state: {detail}.",
            )

        # -- binding: this request is for this task in this session ----------
        if request.task_id != task.id or request.session_id != session.id:
            refuse("task/session binding")
        # -- scope: exactly the one the current state authorizes -------------
        # Covers connector, destination and data classification in one
        # comparison; a narrowed scope is not provably safe from free fields,
        # so equality is the fail-closed rule.
        if request.scope != expected_scope:
            refuse("scope")
        # -- floor: may be strengthened, never weakened ----------------------
        if request.risk_level < floor_risk:
            refuse("risk level below the connector/task floor")
        try:
            # Merging the floor into the request changes nothing exactly when
            # the request is already at or above it, so the canonical
            # non-weakenable policy is also the comparison. A pair that cannot
            # merge at all conflicts, which is a refusal too.
            strengthened = merge_side_effects(floor, request.side_effect)
        except AttestationError:
            strengthened = None
        if strengthened != request.side_effect:
            refuse("side effect below the connector/task floor")
        # -- attestation: attests exactly this request -----------------------
        attestation = request.attestation
        if attestation is None:
            refuse("missing egress attestation")
        if attestation.scope != request.scope:
            refuse("attestation scope")
        if attestation.risk_level != request.risk_level:
            refuse("attestation risk level")
        if attestation.side_effect != request.side_effect:
            refuse("attestation side effect")
        if (
            floor.effect is SideEffect.EXTERNAL_EGRESS
            and not attestation.outbound_digest
        ):
            # An external egress with nothing attested is not attestable; an
            # empty digest must never stand in for the bytes that leave.
            refuse("external egress without an outbound digest")
        # -- preview: describes the same egress the request authorizes -------
        preview = request.outbound_preview
        if preview is not None:
            if normalize_destination(preview.destination) != request.scope.destination:
                refuse("preview destination")
            if preview.data_classification is not task.scope.data_classification:
                refuse("preview data classification")
        return request

    def _exact_pending_request(
        self, session: SessionState, task: Task, current: PermissionRequest
    ) -> PermissionRequest:
        """The persisted pending request, only if it attests exactly the
        current state; anything stale, expired or mutated is superseded by a
        freshly issued ``current`` with a new unique request id.

        Reuse keys on the semantic fingerprint (scope, attestation, gate state
        and approval display), never on the issuance id: the persisted request
        keeps its stable id when reused, but an expired or diverged one is
        replaced by ``current``, which carries a brand-new id so an old grant
        cannot authorize the re-issued request.
        """
        pending = self.memory.load_pending_approval(task.id, session.id)
        if (
            pending is not None
            and not is_expired(pending.expires_at)
            and pending.fingerprint == current.fingerprint
        ):
            return pending
        return current

    def _monetary_gates(
        self,
        session: SessionState,
        task: Task,
        connector: BaseConnector,
        projected: Decimal,
    ) -> list[tuple[str, PermissionRequest]] | AgentRun:
        """The monetary gates that apply right now, each as an attested
        request binding the gate, projection, limit and switch state.

        ``projected`` is exact canonical USD, so the gate identity a grant is
        bound to is never a binary float artefact and a grant approved for a
        lower projection can never match a higher one.
        """
        estimate = task.estimate
        limit = canonical_usd(
            self.settings.monthly_hard_limit_usd, "monthly_hard_limit_usd"
        )
        applicable: list[tuple[str, str]] = []
        if estimate.requires_paid_api and not self.settings.paid_api_enabled:
            applicable.append(
                (
                    PAID_API_GATE,
                    "This task would use a paid API while paid APIs are disabled.",
                )
            )
        if estimate.billing_class is BillingClass.METERED_MONETARY and projected > limit:
            applicable.append(
                (
                    COST_CAP_GATE,
                    f"Projected monthly cost {projected:.4f} USD exceeds the "
                    f"{limit:.2f} USD hard cap.",
                )
            )
        gates: list[tuple[str, PermissionRequest]] = []
        for gate, reason in applicable:
            cost_gate = "|".join(
                (
                    gate,
                    format_usd(projected),
                    format_usd(limit),
                    str(int(self.settings.paid_api_enabled)),
                )
            )
            request = self._cost_permission_request(
                task, session, connector, action=gate, reason=reason, cost_gate=cost_gate
            )
            if isinstance(request, AgentRun):
                return request
            gates.append((gate, request))
        return gates

    def _permission_satisfied(
        self, session: SessionState, request: PermissionRequest
    ) -> bool:
        """Whether the exact current request is policy-exempt, covered by a
        remembered scope grant, or backed by an approved grant record."""
        if not self._requires_approval(request):
            return True
        if self._remembered_permission_allowed(
            request
        ) and self.memory.has_remembered_scope_grant(
            session.id, request.scope_fingerprint
        ):
            return True
        return self.memory.has_approved_grant(
            session.id, request.task_id, request.fingerprint
        )

    def _unsatisfied_gate(
        self,
        session: SessionState,
        task: Task,
        connector: BaseConnector,
        projected: Decimal,
    ) -> tuple[LoopStage | AgentRun | None, PermissionRequest | None]:
        """Recompute every gate attestation right before execution.

        Returns ``(open_stage, current_request)``: the first gate stage lacking
        a valid consumed grant (or a terminal run, or ``None`` when execution
        is authorized), together with the current attested egress request whose
        fingerprint binds the execution attempt. ``projected`` is the exact
        exposure the same claim will be arbitrated against.
        """
        current = self._attested_request(session, task, connector)
        if isinstance(current, AgentRun):
            return current, None
        if not self._permission_satisfied(session, current):
            return LoopStage.RISK_PERMISSION, current
        gates = self._monetary_gates(session, task, connector, projected)
        if isinstance(gates, AgentRun):
            return gates, current
        for _, request in gates:
            if not self.memory.has_approved_grant(
                session.id, task.id, request.fingerprint
            ):
                return LoopStage.COST_PREFLIGHT, current
        return None, current

    def _execution_grant_fingerprint(
        self, session: SessionState, current: PermissionRequest
    ) -> str | None:
        """The base-permission one-shot grant fingerprint that must be consumed
        for this execution, or None when authorization is policy-exempt or
        backed by a reusable (remembered) session grant."""
        if not self._requires_approval(current):
            return None
        if self._remembered_permission_allowed(
            current
        ) and self.memory.has_remembered_scope_grant(
            session.id, current.scope_fingerprint
        ):
            return None
        return current.fingerprint

    def _execution_one_shot_fingerprints(
        self,
        session: SessionState,
        task: Task,
        connector: BaseConnector,
        current: PermissionRequest,
        projected: Decimal,
    ) -> tuple[list[str], str]:
        """Every non-reusable one-shot grant that authorized this execution
        opportunity: the base permission plus each applicable monetary gate
        (paid-API, monthly-cost-cap). They are consumed atomically, all or
        nothing, so a task cannot execute with any authorizing gate unconsumed.

        Also returns the exact cost-override fingerprint (or ``""``), which the
        claim transaction re-verifies against the projection it recomputes.
        """
        fingerprints: list[str] = []
        override = ""
        base = self._execution_grant_fingerprint(session, current)
        if base is not None:
            fingerprints.append(base)
        gates = self._monetary_gates(session, task, connector, projected)
        if not isinstance(gates, AgentRun):
            for gate, request in gates:
                fingerprints.append(request.fingerprint)
                if gate == COST_CAP_GATE:
                    override = request.fingerprint
        return fingerprints, override

    def _finalize_cancelled(
        self,
        session: SessionState,
        task: Task,
        attempt_id: str,
        result: ToolResult,
    ) -> AgentRun:
        """Commit an authoritative cancellation over a returned execution: a
        stale success/failure never becomes COMPLETED/FAILED (H2-F)."""
        task.status = TaskStatus.CANCELLED
        task.stage = LoopStage.CANCELLED
        session.active_task_id = None
        clear_issues(task, *ProgressIssueKind)
        outcome = self.memory.conclude_execution(
            session,
            task,
            attempt_id,
            "execution_cancelled",
            {},
            finalizing_cancellation=True,
        )
        if outcome != "finished":
            return self._execution_in_doubt(
                session, task, self.memory.latest_execution_attempt(task.id)
            )
        return AgentRun(RunStatus.CANCELLED, session, task, result=result)

    def _terminal_run(self, session: SessionState, task: Task) -> AgentRun | None:
        """Report a terminal task as-is, without a connector or any mutation."""
        if not is_terminal(task):
            return None
        result = self._result_from_dict(task.result_snapshot)
        error = result.error if result is not None else None
        return AgentRun(
            terminal_run_status(task), session, task, result=result, error=error
        )

    def _execution_in_doubt(
        self,
        session: SessionState,
        task: Task,
        attempt: ExecutionAttempt | None,
    ) -> AgentRun:
        """A prior execution attempt cannot be safely replayed. Reports only;
        the task is left recoverable (never fabricated terminal)."""
        details: dict[str, Any] = (
            {"attempt_state": attempt.state.value} if attempt is not None else {}
        )
        # M5: a local write can say which of the two attested states the
        # destination is actually in, which turns "unknown" into an
        # actionable answer without replaying anything.
        diagnosis = self._local_write_diagnosis(task)
        if diagnosis is not None:
            details["local_write_diagnosis"] = diagnosis
        return AgentRun(
            RunStatus.FAILED,
            session,
            task,
            error=StructuredError(
                ErrorCode.EXECUTION_UNKNOWN_OUTCOME,
                "A prior execution attempt is in doubt and must not be replayed "
                "automatically; explicit recovery is required.",
                details=details,
            ),
        )

    def _execution_journal_precheck(
        self, session: SessionState, task: Task
    ) -> AgentRun | str | None:
        """Consult the durable attempt journal before the connector is used.

        Returns an ``AgentRun`` to block (in doubt), the sentinel ``"advanced"``
        when a FINISHED attempt let the task move past EXECUTE without a second
        call, or ``None`` (no attempt or a reclaimable PREPARED one) to proceed.
        """
        attempt = self.memory.latest_execution_attempt(task.id)
        if attempt is None or attempt.state is AttemptState.PREPARED:
            return None
        if attempt.state in (AttemptState.IN_FLIGHT, AttemptState.UNKNOWN_OUTCOME):
            # IN_FLIGHT after restart, or an unresolved outcome: never replay.
            return self._execution_in_doubt(session, task, attempt)
        # FINISHED: the connector already ran. Advance from the persisted
        # result instead of executing again.
        result = self._result_from_dict(task.result_snapshot)
        if result is None:
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    ErrorCode.EXECUTION_UNKNOWN_OUTCOME,
                    "A finished execution attempt has no persisted result to "
                    "recover; refusing to run it again.",
                ),
            )
        mark_stage_completed(task, LoopStage.EXECUTE)
        task.status = TaskStatus.VERIFYING
        task.stage = LoopStage.VERIFY
        self._checkpoint(
            session, task, "execution_completed_recovered", {}
        )
        return "advanced"

    def _billing_route_failure(
        self, session: SessionState, task: Task, connector: BaseConnector
    ) -> AgentRun | None:
        """Classify the billing route and fail closed when it is unknown.

        H3: the connector-derived monetary values are canonically validated in
        the same step, so a NaN, infinite, negative or zero metered maximum, an
        inverted minimum/maximum or a non-positive resource ceiling fails the
        task before any approval, preflight or external call can happen.
        """
        try:
            estimate = connector.estimate_cost(task)
        except Exception as exc:  # noqa: BLE001 - stage boundary
            # The route could not be established at all; fail closed exactly
            # like an UNKNOWN classification, before any approval or call.
            task.estimate = CostEstimate(billing_evidence="estimate_probe_failed")
            return self._stage_failure(
                session,
                task,
                LoopStage.COST_PREFLIGHT,
                ErrorCode.BILLING_ROUTE_UNKNOWN,
                exc,
            )
        try:
            validate_cost_estimate(estimate)
        except BillingIntegrityError as exc:
            # The malformed estimate is never adopted: a raw-string or foreign
            # billing class would otherwise reach persistence and presentation
            # and crash on enum-specific access. The task carries a fail-closed
            # UNKNOWN estimate instead and stops here.
            task.estimate = CostEstimate(
                billing_evidence="malformed_connector_estimate"
            )
            return self._billing_integrity_failure(session, task, str(exc))
        task.estimate = estimate
        if estimate.billing_class is not BillingClass.UNKNOWN:
            return None
        # Never assume a free route and never invent a zero cost.
        return self._terminal_failure(
            session,
            task,
            StructuredError(
                ErrorCode.BILLING_ROUTE_UNKNOWN,
                "The billing route could not be established; "
                "refusing to run an external call.",
                details={"billing_evidence": estimate.billing_evidence},
            ),
        )

    def _connector_for_task(self, task: Task) -> BaseConnector | AgentRun:
        try:
            return self.registry.get(task.selected_connector or "")
        except ConnectorNotFoundError as exc:
            return self._input_failure(
                task,
                ErrorCode.NO_CONNECTOR,
                str(exc),
            )

    def _requires_approval(self, request: PermissionRequest) -> bool:
        if request.side_effect.mandatory:
            return True
        if self.settings.permission_mode is PermissionMode.STRICT:
            return True
        if self.settings.permission_mode is PermissionMode.SMART_AUTO:
            return not (
                request.risk_level is RiskLevel.LOW
                and request.side_effect.reversible
            )
        if request.risk_level is RiskLevel.LOW and request.side_effect.reversible:
            return False
        return True

    def _remembered_permission_allowed(self, request: PermissionRequest) -> bool:
        return (
            self.settings.permission_mode is PermissionMode.SESSION
            and not request.side_effect.mandatory
            and request.risk_level < RiskLevel.HIGH
            and request.side_effect.reversible
        )

    def _handle_approval(
        self,
        session: SessionState,
        task: Task,
        request: PermissionRequest,
        grant: PermissionGrant | None,
        waiting_status: RunStatus,
    ) -> tuple[AgentRun | None, bool]:
        task.status = (
            TaskStatus.AWAITING_COST_OVERRIDE
            if waiting_status is RunStatus.AWAITING_COST_OVERRIDE
            else TaskStatus.AWAITING_APPROVAL
        )
        if grant is None:
            set_waiting_issue(task)
            # A terminal writer (rejection/cancellation) that already won
            # aborts here: the waiting state, its checkpoint, its audit event
            # and its pending request are all rolled back, so no approval
            # opportunity is reported that does not exist in persistence.
            self._checkpoint(
                session,
                task,
                "approval_requested",
                request.to_dict(),
                pending_request=request,
            )
            return (
                AgentRun(
                    waiting_status,
                    session,
                    task,
                    permission_request=request,
                ),
                False,
            )
        validation_error = self._validate_grant(request, grant)
        if validation_error is not None:
            # A concurrent contender may have resolved the original request
            # first (the pending is gone) and the winning resolution may
            # already be authoritative. Never overwrite a terminal or resolved
            # state with a stale waiting checkpoint: reload and report instead.
            fresh = self.memory.load_task(task.id)
            if fresh is not None:
                terminal = self._terminal_run(session, fresh)
                if terminal is not None:
                    return terminal, False
            if self.memory.load_pending_approval(task.id, session.id) is None:
                return self._resolution_lost(session, task), False
            # Still blocked on a genuine invalid grant against a live pending
            # request: the applicable blocker stays and the refused grant is
            # recorded as its own typed issue.
            set_waiting_issue(task)
            set_issue(
                task,
                ProgressIssueKind.INVALID_GRANT,
                validation_error.code,
                validation_error.retryable,
            )
            # A terminal writer that won while this grant was refused aborts
            # here; nothing was written, so nothing stale is reported.
            self._checkpoint(
                session,
                task,
                "invalid_grant_rejected",
                validation_error.to_dict(),
                pending_request=request,
            )
            return (
                AgentRun(
                    waiting_status,
                    session,
                    task,
                    permission_request=request,
                    error=validation_error,
                ),
                False,
            )
        if not grant.approved:
            # P1-A: resolution and terminal rejection are ONE transaction, so
            # no observable state has a rejected request with a resumable task.
            # A concurrent contender therefore cannot reissue or reopen it.
            task.status = TaskStatus.REJECTED
            task.stage = LoopStage.FAILED
            session.active_task_id = None
            clear_issues(task, *NON_FAILURE_ISSUE_KINDS)
            error = StructuredError(
                ErrorCode.APPROVAL_REJECTED, "The user rejected this action."
            )
            if not self.memory.reject_task(
                request, grant, session, task, error.to_dict()
            ):
                return self._resolution_lost(session, task), False
            return (
                AgentRun(RunStatus.REJECTED, session, task, error=error),
                True,
            )
        # Approve: atomic single-winner resolution. A loser makes no grant,
        # performs no execution, and reports the authoritative resolution.
        if not self.memory.resolve_approval(request, grant, "approved"):
            return self._resolution_lost(session, task), False
        task.status = TaskStatus.PENDING
        return None, True

    def _checkpoint(
        self,
        session: SessionState,
        task: Task,
        event_type: str,
        payload: dict[str, Any] | None = None,
        pending_request: PermissionRequest | None = None,
    ) -> None:
        """Persist one lifecycle checkpoint, or abort the local loop.

        Every lifecycle write in the loop goes through here. If storage reports
        that a terminal writer already won, nothing was persisted and
        ``_LifecycleLost`` stops this writer before it can emit any later
        stage, event, decision or finalization built on its stale task.
        """
        if not self.memory.persist_checkpoint(
            session, task, event_type, payload, pending_request
        ):
            raise _LifecycleLost(self._authoritative_run(session, task))

    def _authoritative_run(self, session: SessionState, task: Task) -> AgentRun:
        """Report the persisted truth after this writer lost lifecycle authority.

        The task and session are reloaded from storage, so the caller never
        returns its stale local view, and no ``permission_request`` is attached
        unless one genuinely exists in persistence.
        """
        fresh_session = self.memory.load_session(session.id) or session
        fresh = self.memory.load_task(task.id)
        if fresh is None:
            return self._input_failure(
                task, ErrorCode.TASK_NOT_FOUND, f"Unknown task: {task.id}", fresh_session
            )
        terminal = self._terminal_run(fresh_session, fresh)
        if terminal is not None:
            return terminal
        # Not terminal (a newer non-terminal writer won): report the persisted
        # state, with the real pending request when storage actually holds one.
        pending = self.memory.load_pending_approval(fresh.id, fresh_session.id)
        return AgentRun(
            RunStatus.FAILED,
            fresh_session,
            fresh,
            permission_request=pending,
            error=StructuredError(
                ErrorCode.STALE_GRANT,
                "This task was updated by another writer; the local view was "
                "discarded and no state was persisted.",
                retryable=True,
            ),
        )

    def _resolution_lost(self, session: SessionState, task: Task) -> AgentRun:
        """A concurrent contender already resolved this approval. Report the
        authoritative winning state; never execute or create a grant here."""
        fresh = self.memory.load_task(task.id)
        if fresh is not None:
            terminal = self._terminal_run(session, fresh)
            if terminal is not None:
                return terminal
        return AgentRun(
            RunStatus.FAILED,
            session,
            fresh or task,
            error=StructuredError(
                ErrorCode.STALE_GRANT,
                "The approval request was already resolved by another contender.",
                retryable=True,
            ),
        )

    @staticmethod
    def _validate_grant(
        request: PermissionRequest, grant: PermissionGrant
    ) -> StructuredError | None:
        if is_expired(request.expires_at) or is_expired(grant.expires_at):
            return StructuredError(
                ErrorCode.STALE_GRANT,
                "The approval request or grant has expired.",
                retryable=True,
            )
        expected = (
            request.id,
            request.task_id,
            request.session_id,
            request.fingerprint,
        )
        received = (
            grant.request_id,
            grant.task_id,
            grant.session_id,
            grant.fingerprint,
        )
        if received != expected:
            return StructuredError(
                ErrorCode.INVALID_GRANT,
                "Grant does not match request, task, session, or scope fingerprint.",
                details={"request_id": request.id},
            )
        return None

    def _cost_permission_request(
        self,
        task: Task,
        session: SessionState,
        connector: BaseConnector,
        *,
        action: str,
        reason: str,
        cost_gate: str,
    ) -> PermissionRequest | AgentRun:
        scope = ActionScope(
            action=action,
            target=f"monthly-budget:{self.settings.monthly_hard_limit_usd:.2f}-usd",
            connector=connector.manifest.name,
            destination=task.scope.destination,
            data_classification=DataClassification.INTERNAL,
        )
        # The connector attests the same egress the money would pay for; the
        # monetary gate view (HIGH, irreversible BILLING, always approved) and
        # the exact gate state are then bound on top by the loop.
        request = self._attested_request(
            session, task, connector, scope_override=scope, reason=reason
        )
        if isinstance(request, AgentRun):
            return request
        return request.as_monetary_gate(cost_gate)

    def _selection_failure(
        self, session: SessionState, task: Task, exc: Exception
    ) -> AgentRun:
        if isinstance(exc, AmbiguousConnectorError):
            code = ErrorCode.CONNECTOR_AMBIGUOUS
        elif isinstance(exc, ConnectorUnavailableError):
            code = ErrorCode.CONNECTOR_UNAVAILABLE
        else:
            code = ErrorCode.NO_CONNECTOR
        return self._terminal_failure(
            session,
            task,
            diagnostic_from_exception(
                code,
                exc,
                retryable=code is ErrorCode.CONNECTOR_UNAVAILABLE,
                stage=LoopStage.PLAN,
            ),
        )

    def _terminal_failure(
        self,
        session: SessionState,
        task: Task,
        error: StructuredError,
        result: ToolResult | None = None,
    ) -> AgentRun:
        task.status = TaskStatus.FAILED
        task.stage = LoopStage.FAILED
        session.active_task_id = None
        set_issue(task, ProgressIssueKind.FAILURE, error.code, error.retryable)
        if result is not None and result.error is None:
            # H4: the persisted snapshot must describe its own failure, so a
            # restart reports the same typed diagnostic instead of a terminal
            # task with no recorded reason.
            result.error = error
        if result is None:
            try:
                estimated = float(canonical_usd(task.estimate.monetary_maximum_usd))
            except BillingIntegrityError:
                # The failure may be the malformed estimate itself; a bad
                # amount must not ride along into telemetry or presentation.
                estimated = 0.0
            try:
                billing_class = require_billing_class(task.estimate.billing_class)
            except BillingIntegrityError:
                billing_class = BillingClass.UNKNOWN
            result = ToolResult(
                outcome=OutcomeStatus.ERROR,
                summary=error.message,
                error=error,
                usage=UsageRecord(
                    session_id=session.id,
                    task_id=task.id,
                    connector=task.selected_connector,
                    estimated_cost_usd=estimated,
                    billing_class=billing_class,
                    billing_evidence=task.estimate.billing_evidence,
                ),
            )
        task.result_snapshot = self._result_to_dict(result)
        self._checkpoint(
            session, task, "task_failed", error.to_dict()
        )
        return AgentRun(RunStatus.FAILED, session, task, result=result, error=error)

    def _bind_canonical_local_target(
        self, session: SessionState, task: Task
    ) -> AgentRun | None:
        """Canonicalize a local-read target through the FILESYSTEM path (P1-2).

        ``ActionScope.normalized`` is field-aware but shape-driven: it routes
        ``target`` through ``canonical_path`` only when the text already looks
        like a path. A bare relative name has no separator and no drive, so it
        fell through to ``normalize_symbol`` and stayed a *symbolic* string -
        which meant ``README.md`` and ``.\\README.md`` produced two different
        scope fingerprints for one file, and, worse, that the symbolic form
        was never re-resolved: a grant issued against ``readme.md`` stayed
        valid while the name was repointed at a different file, and execution
        then resolved the name live and read the new one.

        Binding the capability's target to ``canonical_path`` here removes
        both. Every LEXICAL spelling of one file - bare, ``.\\`` prefixed,
        dot/parent segments - converges on the same absolute canonical
        identity, so the approval fingerprint, the attested resource boundary
        and the bytes the connector opens are the same resource.

        P2. A symlink or junction is NOT one of those spellings and never
        converges with its destination. ``canonical_path`` is lexical and
        dereferences nothing, because following a link to decide what to call
        it would be an inspection of the filesystem aimed by a path no policy
        has accepted yet - and on Windows that inspection is an ``open``. So a
        link keeps its own canonical identity here and is REFUSED later, by
        the connector's no-follow chain scan, which sees the reparse point as
        itself rather than resolving through it. A repoint therefore no longer
        changes this fingerprint; it is refused at the open/policy boundary
        instead, which is the guarantee that actually mattered.

        Keyed on the capability, not on the connector: the canonical identity
        must exist before connector selection, since selection itself and the
        approval that follows are already reading the scope.

        The root-relative display name stays a completely separate value -
        ``LocalReadSnapshot.relative_path``, derived from the allowed root -
        so no absolute path travels to the user, the envelope or the model.

        ``None`` means proceed. A target no filesystem can ever name is
        refused HERE, as ordinary terminal state, because this is the first
        thing in the loop that touches it. That matters for more than
        tidiness: this method runs after ``run()`` has already registered the
        task and pointed the session at it, so a raise here escapes the loop
        entirely and leaves a registered task with no terminal state and a
        session still naming it active. An embedded NUL is exactly that case
        - POSIX rejects it inside ``resolve()`` while Windows accepts it
        there and rejects it later in ``lstat``/``open``, so the *platform*
        decided whether the product failed cleanly. It is refused explicitly
        instead, identically everywhere.
        """
        if task.capability not in CANONICAL_TARGET_CAPABILITIES:
            return None
        noun = "Read" if task.capability == LOCAL_READ_CAPABILITY else "Write"
        target = task.scope.target
        if type(target) is not str or not target.strip():
            # An empty or malformed target is refused downstream, with the
            # policy message that names the rule; it is not repaired here.
            return None
        unusable = unusable_filesystem_target(target)
        if unusable is not None:
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    ErrorCode.INVALID_TASK,
                    f"{noun} target is not a usable filesystem path: {unusable}.",
                ),
            )
        task.scope = replace(task.scope, target=canonical_path(target))
        return None

    def _bind_local_read(
        self, task: Task, connector: BaseConnector, result: ToolResult
    ) -> None:
        """Bind the connector's typed snapshot to the task, or bind nothing.

        Two independent gates before anything is accepted:

        * the *selected connector* must declare the local-read capability, so
          an unrelated connector cannot smuggle a snapshot onto a task through
          its result data; and
        * the object must pass ``_validated_local_read`` exactly.

        ``classification`` is then **replaced**, unconditionally, with the
        reading task's own scope classification.

        The exact guarantee, stated narrowly: *the classification the connector
        RETURNS is never read*. Whatever it puts in the snapshot it hands back -
        a lowered value, a raised one, a raw string, a foreign enum - is
        discarded here, so a task whose scope says SECRET produces a SECRET
        snapshot regardless.

        What this does NOT claim: a connector is handed the live ``Task`` in
        ``execute`` and ``verify`` and can mutate it directly afterwards. That
        exposure is pre-existing and completely general - the same connector can
        equally rewrite ``task.objective`` or ``task.scope`` - so it is a
        property of the connector boundary itself, not of local read, and it is
        not narrowed by this method. Downstream boundaries do not trust the
        persisted value either: the cross-task hop and the Claude egress
        boundary each re-validate the classification independently.
        """
        if LOCAL_READ_CAPABILITY not in connector.manifest.capabilities:
            return
        raw = result.data if isinstance(result.data, dict) else {}
        if LOCAL_READ_RESULT_KEY not in raw:
            return
        try:
            snapshot = self._validated_local_read(raw[LOCAL_READ_RESULT_KEY])
        except Exception:  # noqa: BLE001 - discard, never strand the lifecycle
            return
        task.local_read = replace(
            snapshot, classification=task.scope.data_classification
        )

    def _bind_local_write(
        self, task: Task, connector: BaseConnector, result: ToolResult
    ) -> None:
        """Bind the connector's typed write record to the task, or bind nothing.

        The same two gates as the read binding: the *selected connector* must
        declare the write capability, so an unrelated connector cannot smuggle
        a record onto a task through its result data, and the object must
        satisfy the one canonical structural contract exactly.

        Unlike the read snapshot there is no classification to rebind - the
        record carries no content of any kind, only digests, sizes and the
        root-relative name, so there is nothing in it a classification could
        be authority over.
        """
        if LOCAL_WRITE_CAPABILITY not in connector.manifest.capabilities:
            return
        raw = result.data if isinstance(result.data, dict) else {}
        record = raw.get(LOCAL_WRITE_RESULT_KEY)
        if type(record) is not LocalWriteRecord:
            return
        if local_write_record_refusal(record) is not None:
            return
        task.local_write = record

    def _bind_model_answer(
        self, task: Task, connector: BaseConnector, result: ToolResult
    ) -> None:
        """Bind the connector's typed model answer to the task, or bind nothing.

        Runs at the same point and for the same reason as the two bindings
        above: BEFORE ``_sanitize_result`` reduces ``data`` to ``next_step``.
        That ordering is the whole of M6 - after it, the answer has already
        been collapsed to a 240-character, path-redacted, single-line
        diagnostic string, which is right for a diagnostic and destroys a
        deliverable.

        Three gates, all mandatory:

        * the *selected connector's own MANIFEST* must declare
          ``EXTERNAL_EGRESS``. Deliberately the manifest and not the merged
          effect: a task may strengthen its effect upward (the local ladder
          relies on it), and that escalation must not make a purely local
          connector eligible to hand back "external model output". It is also
          deliberately the side effect rather than a capability name, so the
          provider-neutral core never learns Claude-specific vocabulary;
        * the object must be EXACTLY a ``ModelAnswer`` - a dict, a subclass or
          a foreign look-alike is not this contract; and
        * it must satisfy the one canonical structural contract.

        Non-raising, exactly like the two bindings above: this runs after the
        connector boundary and before H3 reconciliation, so a malformed
        candidate is discarded rather than raised, and the connector's own
        verifier then fails the task closed at VERIFY.

        The answer carries no authority of any kind, so unlike the read
        snapshot there is nothing here to rebind from trusted state: it is
        display data, and a task that ends up with none simply shows none.
        """
        if (
            connector.manifest.default_side_effect.effect
            is not SideEffect.EXTERNAL_EGRESS
        ):
            return
        raw = result.data if isinstance(result.data, dict) else {}
        if MODEL_ANSWER_RESULT_KEY not in raw:
            return
        try:
            answer = self._validated_model_answer(raw[MODEL_ANSWER_RESULT_KEY])
        except Exception:  # noqa: BLE001 - discard, never strand the lifecycle
            return
        task.model_answer = answer

    def _prepare_local_write(
        self, session: SessionState, task: Task, connector: BaseConnector
    ) -> AgentRun | None:
        """Persist the attested write metadata before the boundary is crossed.

        The binding that happens after ``execute`` returns covers only the
        outcomes that HAVE a result. The one that most needs explaining does
        not: when a write raises, H2 marks the attempt non-replayable and a
        diagnosis is worth having only if the two attested states - what the
        destination was, and what it was authorized to become - were already
        durable at that moment.

        The record is asked of the connector because the connector is what
        derived and attested the intent in the first place; it is then judged
        by the one canonical structural contract before it is bound, and it
        carries no authority of its own - nothing downstream reads it as
        permission, billing or lifecycle state. Resolved by duck typing so
        the core keeps knowing the capability contract and not a concrete
        connector.
        """
        if task.capability != LOCAL_WRITE_CAPABILITY:
            return None
        prepare = getattr(connector, "pre_execution_record", None)
        if not callable(prepare):
            return self._preparation_failure(session, task, "connector_cannot_prepare")
        try:
            record = prepare(task)
        except Exception as exc:  # noqa: BLE001 - connector boundary
            # Deliberately NOT ``diagnostic_from_exception``. That contract
            # bounds and scrubs connector-authored text, which is right for a
            # general failure, but here the connector was asked about bytes
            # the user is writing - so its message is the one place those
            # bytes could plausibly turn up. Only the exception's TYPE is
            # kept: a class name cannot carry content.
            return self._preparation_failure(
                session, task, "preparation_failed", type(exc).__name__
            )
        if type(record) is not LocalWriteRecord:
            return self._preparation_failure(session, task, "no_prepared_record")
        if local_write_record_refusal(record) is not None:
            return self._preparation_failure(session, task, "malformed_prepared_record")
        task.local_write = record
        # A real checkpoint, not an in-memory assignment: a raise inside
        # ``execute`` must leave this behind for the RELOADED task to carry.
        # A real checkpoint, not an in-memory assignment: a raise inside
        # ``execute`` must leave this behind for the RELOADED task to carry.
        # ``_checkpoint`` raises ``_LifecycleLost`` if storage refused, which
        # aborts this writer before any boundary is crossed - so a failed
        # persist cannot become a write either.
        self._checkpoint(
            session,
            task,
            "local_write_prepared",
            {"mode": record.mode, "pre_present": record.pre_present},
        )
        return None

    def _preparation_failure(
        self,
        session: SessionState,
        task: Task,
        reason: str,
        exception_type: str = "",
    ) -> AgentRun:
        """Refuse a write whose approved authority could not be made durable.

        Fail closed, not best effort. The prepared record is what makes an
        in-doubt write explainable, and a write that could still end up in
        doubt with nothing to explain it is exactly the outcome this
        capability must not produce. ``reason`` is a fixed, non-secret label
        and ``exception_type`` a bare class name; no connector-authored text
        and no part of the intended content can travel in either.
        """
        details: dict[str, Any] = {"reason": reason}
        if exception_type:
            details["exception_type"] = exception_type
        return self._terminal_failure(
            session,
            task,
            StructuredError(
                ErrorCode.EGRESS_REFUSED,
                "The approved local-write authority could not be established "
                "and persisted before execution; refusing to write.",
                details=details,
            ),
        )

    def _local_write_diagnosis(self, task: Task) -> str | None:
        """What a write in doubt actually did, if the connector can say.

        A local write is decidable after the fact, so the in-doubt report can
        carry more than "unknown". Resolved by duck typing rather than by
        importing a connector implementation: the core knows the capability
        contract, never a concrete connector.

        Purely informational. It never replays, never repairs and never
        advances the task - the attempt stays non-replayable either way.
        """
        if task.capability != LOCAL_WRITE_CAPABILITY:
            return None
        try:
            connector = self.registry.get(task.selected_connector or "")
        except Exception:  # noqa: BLE001 - a missing connector is not a fault here
            return None
        diagnose = getattr(connector, "diagnose_write", None)
        if not callable(diagnose):
            return None
        try:
            verdict = diagnose(task)
        except Exception:  # noqa: BLE001 - diagnosis must never strand recovery
            return None
        return verdict if type(verdict) is str else None

    def _bind_referenced_local_read(
        self, session: SessionState, task: Task
    ) -> AgentRun | None:
        """Carry a completed task's local-read snapshot onto this task.

        The only cross-task data hop in the product, and it is deliberately
        narrow. ``context.local_read_task_id`` is a *reference*, never
        content: every field is re-derived from the referenced task's own
        persisted state and re-validated here, so editing the reference can
        never widen what this task may see.

        Every one of these must hold, or the task fails closed:

        * the referenced task exists and belongs to **this** session;
        * it is not this task itself;
        * it COMPLETED (an in-flight, failed or cancelled read is not
          evidence);
        * it actually carries a structurally valid snapshot; and
        * the classification ladder allows this task's classification to see
          the SOURCE TASK's classification - both sides read from exact-typed
          ``scope.data_classification``, never from the persisted snapshot,
          and compared with the same ``retrieval_eligible`` matrix the
          retrieval pipeline and every egress boundary use, so SECRET is
          never eligible in either role.

        ``None`` means proceed; a task with no reference is untouched.
        """
        requested = task.context.get(LOCAL_READ_TASK_REFERENCE_KEY)
        if requested is None:
            return None

        def refuse(detail: str) -> AgentRun:
            # Terminal, like every other CONTEXT_RETRIEVAL boundary failure:
            # the task is persisted FAILED rather than left parked at a stage
            # it can never clear.
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    ErrorCode.INVALID_TASK,
                    f"Referenced local read is not usable: {detail}.",
                ),
            )

        if not isinstance(requested, str) or not requested:
            return refuse("the reference is not a task id")
        if requested == task.id:
            return refuse("a task cannot reference its own local read")
        if self.memory.task_session_id(requested) != session.id:
            return refuse("the referenced task belongs to another session")
        try:
            source = self.memory.load_task(requested)
        except Exception:  # noqa: BLE001 - storage boundary; fail closed
            source = None
        if source is None:
            return refuse("the referenced task could not be loaded")
        if source.status is not TaskStatus.COMPLETED:
            return refuse("the referenced task did not complete")
        snapshot = source.local_read
        if not snapshot.present:
            return refuse("the referenced task carries no local read")
        # P1-3, and this is the whole point of the hop: the SOURCE TASK's own
        # authoritative scope classification decides what this snapshot is.
        # The persisted ``snapshot.classification`` is NEVER read - a row
        # edited outside the product could say PUBLIC on a SECRET task's read,
        # and a raw string or a foreign ``str`` enum would satisfy both the
        # rank lookup and ``retrieval_eligible`` silently. Both sides of the
        # ladder are therefore taken from exact-typed task scope, and the
        # snapshot is REBUILT with the source authority before it is
        # validated or compared.
        source_authority = source.scope.data_classification
        if type(source_authority) is not DataClassification:
            return refuse("the referenced task classification is malformed")
        destination = task.scope.data_classification
        if type(destination) is not DataClassification:
            return refuse("this task classification is malformed")
        snapshot = replace(snapshot, classification=source_authority)
        try:
            snapshot = self._validated_local_read(snapshot)
        except Exception:  # noqa: BLE001 - boundary; fail closed
            return refuse("the referenced snapshot is malformed")
        if not retrieval_eligible(destination, source_authority):
            return refuse("the classification ladder does not allow it")
        task.local_read = snapshot
        return None

    def _write_identity_failure(
        self, session: SessionState, task: Task
    ) -> AgentRun | None:
        """Refuse a task whose durable write-source identity is not usable.

        The one central check of the M7 (kind, reference, content, stage)
        consistency, evaluated at every stage that builds write authority:
        the start of CONTEXT_RETRIEVAL, RISK_PERMISSION before the first
        attestation, and EXECUTE before any claim or side effect. It is what
        stops a persisted handoff from ever degrading into a direct write:
        ``INVALID`` is terminal, a handoff on a non-write capability is
        terminal, a handoff carrying content BEFORE its source was resolved
        is terminal (injected bytes are never frozen), and a handoff carrying
        no content AFTER resolution is terminal (blanked bytes are never a
        0-byte create). A direct write only has to carry no reference; its
        M5 content semantics are untouched.

        Terminal ``INVALID_TASK`` rather than a regate on purpose: none of
        these conditions can be repaired by a fresh approval, and at EXECUTE
        the check precedes ``open_execution_attempt``/``begin_execution``, so
        no attempt is opened, claimed or duplicated - a PREPARED attempt left
        by an earlier crash simply stays unclaimed, exactly as it does for
        every other pre-claim refusal. ``None`` means proceed.
        """
        reason = local_write_identity_refusal(
            task.write_source, task.write_source_task_id
        )
        if reason is None and task.write_source is WriteSource.MODEL_ANSWER:
            # M10. A handoff may CREATE and may never REPLACE. Nothing durable
            # proves the exact bounded read context reached the actual model
            # invocation that produced the answer, so an irreversible in-place
            # replacement is never authorized from model-generated bytes - for
            # every connector alike, not for a connector this product distrusts.
            # Checked FIRST and stage-independently, so the programmatic form is
            # refused at CONTEXT_RETRIEVAL, RISK_PERMISSION and EXECUTE, before
            # any connector selection, approval or execution attempt.
            if task.write_mode == "overwrite":
                reason = "a model answer handoff may not overwrite an existing file"
            elif task.capability != LOCAL_WRITE_CAPABILITY:
                reason = "a model answer handoff is only valid for a file_write task"
            elif task.stage is LoopStage.CONTEXT_RETRIEVAL:
                if task.write_content != "":
                    reason = "content is present before the source was resolved"
            elif task.write_content == "":
                reason = "the handoff carries no frozen content"
        if reason is None:
            return None
        return self._terminal_failure(
            session,
            task,
            StructuredError(
                ErrorCode.INVALID_TASK,
                f"Write source identity is not usable: {reason}.",
            ),
        )

    def _overwrite_preimage_failure(
        self, session: SessionState, task: Task
    ) -> AgentRun | None:
        """Refuse an overwrite whose preimage is not a GENUINE completed read.

        M10's read -> plan link, and the reason it cannot live in the
        connector: a matching snapshot on the task proves that SOME bytes were
        described, not that a real ``file_read`` ever produced them. A task
        row built programmatically, restored from a tampered store, or bound
        from an ordinary completed task carrying an injected snapshot would
        otherwise satisfy the connector's path/digest/size comparison and
        become authority to destroy a file. Structure matching is evidence
        about a value; provenance is evidence about where it came from, and
        only the second one can be checked here, where the store and the
        registry are.

        Every overwrite must therefore carry the explicit singular
        ``context.local_read_task_id``, and that reference must resolve to a
        source that is, exactly:

        * not this task itself, and in **this** session;
        * COMPLETED - an in-flight, failed or cancelled read is not evidence;
        * a ``file_read`` task, by its own durable capability;
        * produced by a connector whose MANIFEST declares ``file_read``, asked
          of the registry rather than of a name, so the check stays
          provider-neutral and a task that merely claims the capability is not
          enough;
        * carrying a structurally valid snapshot; and
        * a HEAD read - a REGION digest describes the captured lines only, so
          it can never bind a whole-file replacement.

        The stage half mirrors M7 and M8 exactly. At CONTEXT_RETRIEVAL the
        task must carry NO snapshot yet: an overwrite that arrives with
        ``local_read`` already populated is refused rather than trusted,
        because an injected snapshot must never stand in for the resolution
        step. Past that stage the task must carry EXACTLY the snapshot the
        binder would produce from the referenced source - rebuilt here the
        same way ``_bind_referenced_local_read`` builds it and compared whole,
        so neither side can be edited afterwards. That is a consistency check,
        never a rebinding: a source that changed after approval makes the
        comparison fail and the task refuse, and fresh source state never
        becomes new authority.

        Nothing here re-reads the FILE. The source TASK ROW is consulted, the
        filesystem is not, so the M8 freeze invariant is untouched.

        Terminal ``INVALID_TASK`` rather than a regate, like every other
        provenance gate: none of these can be repaired by a fresh approval,
        and at EXECUTE the check precedes ``open_execution_attempt`` /
        ``begin_execution``, so no attempt is opened, claimed or duplicated.
        ``None`` means proceed; every non-overwrite task is untouched.
        """
        if (
            task.capability != LOCAL_WRITE_CAPABILITY
            or task.write_mode != "overwrite"
        ):
            return None

        def refuse(detail: str) -> AgentRun:
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    ErrorCode.INVALID_TASK,
                    f"Overwrite preimage is not usable: {detail}.",
                ),
            )

        reference = task.context.get(LOCAL_READ_TASK_REFERENCE_KEY)
        if not isinstance(reference, str) or not reference:
            return refuse(
                "an overwrite must reference the completed local read of the "
                "file it replaces"
            )
        if reference == task.id:
            return refuse("a task cannot reference its own local read")
        binding_stage = task.stage is LoopStage.CONTEXT_RETRIEVAL
        if binding_stage and task.local_read.present:
            return refuse("a preimage is present before the reference was resolved")
        if not binding_stage and not task.local_read.present:
            return refuse("the reference resolved to no preimage")
        if self.memory.task_session_id(reference) != session.id:
            return refuse("the referenced read belongs to another session")
        try:
            source = self.memory.load_task(reference)
        except Exception:  # noqa: BLE001 - storage boundary; fail closed
            source = None
        if source is None:
            return refuse("the referenced read could not be loaded")
        if source.status is not TaskStatus.COMPLETED:
            return refuse("the referenced read did not complete")
        if source.capability != LOCAL_READ_CAPABILITY:
            return refuse("the referenced task is not a file read")
        producer_name = source.selected_connector
        if type(producer_name) is not str or not producer_name:
            return refuse("the referenced read names no producer")
        try:
            producer = self.registry.get(producer_name)
        except Exception:  # noqa: BLE001 - an unknown producer is a refusal
            producer = None
        manifest = getattr(producer, "manifest", None)
        capabilities = getattr(manifest, "capabilities", ())
        if LOCAL_READ_CAPABILITY not in capabilities:
            return refuse("the referenced read was not produced by a file reader")
        snapshot = source.local_read
        if not snapshot.present:
            return refuse("the referenced read carries no snapshot")
        source_authority = source.scope.data_classification
        if type(source_authority) is not DataClassification:
            return refuse("the referenced read classification is malformed")
        # Rebuilt exactly as ``_bind_referenced_local_read`` rebuilds it, so
        # the comparison below is against the value that hop would produce and
        # not against whatever the source row happens to say about itself.
        snapshot = replace(snapshot, classification=source_authority)
        try:
            snapshot = self._validated_local_read(snapshot)
        except Exception:  # noqa: BLE001 - boundary; fail closed
            return refuse("the referenced snapshot is malformed")
        if snapshot.start_line != 0 or snapshot.end_line != 0:
            return refuse(
                "the referenced read is a bounded region, which describes only "
                "the captured lines"
            )
        if not binding_stage and task.local_read != snapshot:
            return refuse(
                "the bound preimage is not the referenced read's own snapshot"
            )
        return None

    @staticmethod
    def _handoff_reason(task: Task, connector: BaseConnector) -> str | None:
        """The approval reason of a handoff write, from frozen target fields.

        Provenance and display only - it names the source task and the exact
        digest of the bytes the frame binds, so the user approving the write
        can compare it with the source task's own ``model_answer.sha256``.
        It is bound into the fingerprint through ``display_digest``, which is
        why a changed reference after approval forces a fresh approval, but
        it is never execution authority: the connector never reads it, and
        the source task is never consulted to build it. ``None`` for every
        non-handoff task, so M5 direct writes keep their default reason and
        their existing fingerprints.
        """
        if (
            task.write_source is not WriteSource.MODEL_ANSWER
            or task.capability != LOCAL_WRITE_CAPABILITY
        ):
            return None
        encoded = task.write_content.encode("utf-8")
        return (
            f"Connector '{connector.manifest.name}' will create {len(encoded)} "
            "bytes resolved from the model answer of task "
            f"{task.write_source_task_id} (sha256 "
            f"{hashlib.sha256(encoded).hexdigest()})."
        )

    def _local_read_context_failure(
        self, session: SessionState, task: Task
    ) -> AgentRun | None:
        """Refuse a task whose durable read-context identity is not usable.

        The one central check of the M8 (kind, references, frozen bundle,
        stage) consistency, evaluated at every stage that builds authority:
        the start of CONTEXT_RETRIEVAL, RISK_PERMISSION before the first
        attestation, and EXECUTE before any claim or side effect. It is what
        stops a persisted multi-file consumer from ever degrading into a
        context-LESS external model request.

        ``INVALID`` is terminal. So is a ``NONE`` that still carries
        references or excerpts, which is how a row whose kind key was
        deleted is caught: the kind defaults to ``NONE``, the leftover state
        contradicts it, and the task is refused instead of quietly running
        with no context. So is a plural reference list that reappeared inside
        ``task.context``, which the Claude connector serializes verbatim, and
        so is a task carrying the singular and plural references at once.

        The stage-dependent half mirrors M7 exactly: at CONTEXT_RETRIEVAL a
        REFERENCES task must carry NO frozen sources (injected excerpts are
        never frozen), and past it must carry exactly one validated snapshot
        per reference (an emptied or truncated bundle is never sent).

        Terminal ``INVALID_TASK`` rather than a regate on purpose: none of
        these can be repaired by a fresh approval, and at EXECUTE the check
        precedes ``open_execution_attempt``/``begin_execution``, so no
        attempt is opened, claimed or duplicated. ``None`` means proceed.
        """
        # F6. SHAPE BEFORE MEMBERSHIP, and before anything downstream reads a
        # key. A persisted ``"context": null`` rebuilds as ``None`` - nothing
        # coerces it - so the membership tests below would raise ``TypeError``
        # straight out of a direct ``resume``, past a lifecycle whose entire
        # job is to turn a bad row into deterministic terminal state. This
        # gate is the FIRST thing every stage that later touches ``context``
        # runs, so proving the shape here closes the retrieval counter, both
        # reference resolvers and the connector payload at once, and it
        # refuses through the ordinary bounded invalid-task path below rather
        # than inventing a second one.
        #
        # Exact type: a mapping SUBCLASS with a stateful ``__contains__`` or
        # ``get`` is not the declared type, and this contract is checked by
        # exact type everywhere else it is checked at all.
        reason: str | None = None
        if type(task.context) is not dict:
            reason = "task context is not an exact mapping"
        if reason is None:
            reason = local_read_context_refusal(
                task.local_read_context_kind,
                task.local_read_source_task_ids,
                task.local_read_sources,
            )
        if reason is None and LOCAL_READ_CONTEXT_REFERENCE_KEY in task.context:
            # The plural references are TYPED fields. A copy inside context
            # would travel verbatim into the outbound payload.
            reason = "the plural reference list must not live in task context"
        if (
            reason is None
            and task.local_read_context_kind is LocalReadContextKind.REFERENCES
        ):
            if LOCAL_READ_TASK_REFERENCE_KEY in task.context:
                reason = (
                    "a task carries both a singular and a plural local-read "
                    "reference"
                )
            elif task.stage is LoopStage.CONTEXT_RETRIEVAL:
                if task.local_read_sources:
                    reason = "frozen sources are present before they were resolved"
            elif len(task.local_read_sources) != len(
                task.local_read_source_task_ids
            ):
                reason = "the frozen bundle does not match its reference count"
        if reason is None:
            return None
        return self._terminal_failure(
            session,
            task,
            StructuredError(
                ErrorCode.INVALID_TASK,
                f"Local read context is not usable: {reason}.",
            ),
        )

    @staticmethod
    def _connector_egress_effect(connector: Any) -> Any:
        """The connector's OWN manifest effect, or ``None`` if unreadable.

        Deliberately the manifest and not the merged effect: a task may
        strengthen its effect upward, and that escalation must never make a
        purely local connector eligible to receive file excerpts.
        """
        return getattr(
            getattr(getattr(connector, "manifest", None), "default_side_effect", None),
            "effect",
            None,
        )

    def _local_read_consumer_failure(
        self, session: SessionState, task: Task
    ) -> AgentRun | None:
        """Refuse a frozen bundle whose capability any local connector serves.

        Evaluated at PLAN, on the EXACT task ``registry.select`` is about to
        route. That timing is the whole point. ``can_handle`` is a function of
        the whole task, so a connector may answer False while the bundle is
        empty and True once it is frozen; a policy asked at CONTEXT_RETRIEVAL
        would see only the first answer and selection would act on the second.
        Asking here means the state the policy judges and the state selection
        consumes are the same object.

        The rule is manifest-derived and provider-neutral: at least one
        registered connector must handle the task, and EVERY connector that
        handles it must declare ``EXTERNAL_EGRESS`` in its own manifest. So a
        bundle can never be routed to a connector that would ignore or
        mishandle it, and a preferred or higher-priority local connector
        cannot win the route, because it is refused before any selection
        happens at all.

        Uncertainty is a refusal, never an admission: if the registry cannot
        be enumerated, or a connector cannot be fetched or asked, the task
        fails closed rather than the unreadable candidate being skipped.
        ``None`` means proceed.
        """
        if task.local_read_context_kind is not LocalReadContextKind.REFERENCES:
            return None

        def refuse(detail: str) -> AgentRun:
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    ErrorCode.INVALID_TASK,
                    f"Local read context has no usable consumer: {detail}.",
                ),
            )

        try:
            names = tuple(self.registry.list_names())
        except Exception:  # noqa: BLE001 - registry boundary; fail closed
            return refuse("the connector registry could not be enumerated")
        capable = []
        for name in names:
            # A candidate that cannot be fetched or asked is NOT silently
            # skipped: skipping it would let an unreadable connector stay out
            # of the policy and still be selectable.
            try:
                candidate = self.registry.get(name)
                handles = bool(candidate.can_handle(task))
            except Exception:  # noqa: BLE001 - boundary; fail closed
                return refuse("a registered connector could not be inspected")
            if handles:
                capable.append(candidate)
        if not capable:
            return refuse("no registered connector can consume the bundle")
        for candidate in capable:
            if self._connector_egress_effect(candidate) is not SideEffect.EXTERNAL_EGRESS:
                return refuse(
                    "the capability is not served exclusively by external-model "
                    "connectors"
                )
        return None

    def _local_read_selection_failure(
        self, session: SessionState, task: Task, connector: Any
    ) -> AgentRun | None:
        """Judge the connector selection actually returned, before recording it.

        Defence in depth over ``_local_read_consumer_failure``: that method
        enumerates the registry, this one interrogates the single object that
        will really execute. The two are different code paths over the same
        registry - a preferred-connector route, a registry whose listing and
        lookup disagree, or a manifest read that differs between them - and
        if they ever diverge the object that can execute must not be the one
        that escaped judgement.

        Runs BEFORE ``task.selected_connector`` is assigned, so a refused
        connector is never recorded on the task, and long before any
        attestation, approval or execution attempt. ``None`` means proceed.
        """
        if task.local_read_context_kind is not LocalReadContextKind.REFERENCES:
            return None

        def refuse(detail: str) -> AgentRun:
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    ErrorCode.INVALID_TASK,
                    f"Local read context consumer is not usable: {detail}.",
                ),
            )

        try:
            handles = bool(connector.can_handle(task))
        except Exception:  # noqa: BLE001 - boundary; fail closed
            handles = False
        if not handles:
            return refuse("the selected connector cannot consume the frozen bundle")
        if self._connector_egress_effect(connector) is not SideEffect.EXTERNAL_EGRESS:
            return refuse("the selected connector is not an external-model connector")
        return None

    def _bind_referenced_local_reads(
        self, session: SessionState, task: Task
    ) -> AgentRun | None:
        """Resolve every reference and freeze ONE ordered bounded bundle.

        The third cross-task data hop, and the only one that is plural. Every
        reference is resolved exactly once, here, before retrieval and three
        stages before the first attestation; after the ``context_retrieved``
        checkpoint the frozen tuple is the only context input and no source
        task is ever read again.

        ALL OR NOTHING, and that is the whole point: every source is proven
        into a LOCAL list first, and ``task.local_read_sources`` is assigned
        exactly once, at the end, only if every single source passed. A
        bundle whose third source fails therefore leaves the task with an
        EMPTY tuple, and the refusal is terminal ``INVALID_TASK`` before any
        connector is selected, any approval is requested or any attempt is
        opened. No partial subset can exist, let alone be sent.

        The consumer gate is manifest-derived and provider-neutral: the
        capability must be served by at least one registered connector and by
        NOTHING but external-model connectors, so a bundle can never be
        attached to a task whose connector would silently ignore it. The core
        never learns a connector name or a capability name for this.

        ``task.local_read_sources`` is the ONLY field this method writes; the
        M3 singular ``task.local_read`` is deliberately left untouched.
        """
        if task.local_read_context_kind is not LocalReadContextKind.REFERENCES:
            return None

        def refuse(detail: str) -> AgentRun:
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    ErrorCode.INVALID_TASK,
                    f"Referenced local read context is not usable: {detail}.",
                ),
            )

        # NOTE: the consumer-connector policy is deliberately NOT evaluated
        # here. ``can_handle`` is a function of the WHOLE task, and at this
        # point the bundle has not been frozen yet, so a connector whose
        # ``can_handle`` depends on ``local_read_sources`` would answer False
        # now and True at PLAN - evading a gate asked here and then winning
        # ``registry.select``. The policy therefore runs at PLAN, against the
        # exact post-freeze task selection itself will see.
        destination = task.scope.data_classification
        if type(destination) is not DataClassification:
            return refuse("this task classification is malformed")

        validated: list[LocalReadSnapshot] = []
        for reference in task.local_read_source_task_ids:
            if local_read_reference_refusal(reference) is not None:
                return refuse("a source reference is malformed")
            if reference == task.id:
                return refuse("a task cannot reference its own local read")
            if self.memory.task_session_id(reference) != session.id:
                return refuse("a source task belongs to another session")
            try:
                source = self.memory.load_task(reference)
            except Exception:  # noqa: BLE001 - storage boundary; fail closed
                source = None
            if source is None:
                return refuse("a source task could not be loaded")
            if source.status is not TaskStatus.COMPLETED:
                return refuse("a source task did not complete")
            # The producer gate. The task's own declared capability AND the
            # connector that actually ran it are both required: a tampered
            # row can forge either field alone, and neither check subsumes
            # the other. Together they reject a transitively copied snapshot
            # - a model task that itself carries a local read is not a
            # local-read producer.
            if source.capability != LOCAL_READ_CAPABILITY:
                return refuse("a source task is not a local read")
            producer_name = source.selected_connector
            if type(producer_name) is not str or not producer_name:
                return refuse("a source task names no producer")
            try:
                producer = self.registry.get(producer_name)
            except Exception:  # noqa: BLE001 - an unknown producer is a refusal
                producer = None
            if producer is None:
                return refuse("a source producer is not a registered connector")
            capabilities = getattr(
                getattr(producer, "manifest", None), "capabilities", ()
            )
            if LOCAL_READ_CAPABILITY not in capabilities:
                return refuse("a source producer does not perform local reads")
            snapshot = source.local_read
            if not snapshot.present:
                return refuse("a source task carries no local read")
            # P1-3, exactly as the singular hop: the SOURCE TASK's own
            # authoritative scope classification decides what this snapshot
            # is. The persisted snapshot classification is NEVER read - a row
            # edited outside the product could say PUBLIC on a SENSITIVE
            # task's read, and a raw string or a foreign ``str`` enum would
            # satisfy both the rank lookup and ``retrieval_eligible``
            # silently. Both sides come from exact-typed task scope, and the
            # snapshot is REBUILT with the source authority before it is
            # validated or compared.
            source_authority = source.scope.data_classification
            if type(source_authority) is not DataClassification:
                return refuse("a source task classification is malformed")
            snapshot = replace(snapshot, classification=source_authority)
            try:
                snapshot = self._validated_local_read(snapshot)
            except Exception:  # noqa: BLE001 - boundary; fail closed
                return refuse("a source snapshot is malformed")
            if not retrieval_eligible(destination, source_authority):
                return refuse("the classification ladder does not allow it")
            validated.append(snapshot)

        frozen = tuple(validated)
        # The count and the combined character budget are judged by the one
        # canonical contract, on the completed tuple, before it is adopted.
        reason = local_read_context_refusal(
            task.local_read_context_kind, task.local_read_source_task_ids, frozen
        )
        if reason is not None:
            return refuse(reason)
        task.local_read_sources = frozen
        return None

    @staticmethod
    def _local_read_context_reason(
        task: Task, connector: BaseConnector
    ) -> str | None:
        """The approval reason of a multi-file read task, from frozen fields.

        Provenance and display only. It names how many excerpts will be sent,
        how many characters they carry, and the digest of the ORDERED source
        identity - never the raw task ids, which stay local. It is bound into
        the fingerprint through ``display_digest``, which is what makes a
        changed reference or a changed ORDER invalidate an existing approval
        even though the outbound bytes would be identical. The frozen content
        is separately bound through ``outbound_digest``, so repeating a
        content digest here would bind the same bytes twice.

        Derived from the persisted target fields only - no source task is
        consulted - so RISK_PERMISSION and EXECUTE derive byte-identical
        text. ``None`` for every other task, so existing reasons, display
        digests and fingerprints are unchanged.
        """
        if task.local_read_context_kind is not LocalReadContextKind.REFERENCES:
            return None
        sources = task.local_read_sources
        total = sum(len(snapshot.excerpt) for snapshot in sources)
        # A JSON array, so ORDER is significant; ``sort_keys`` is deliberately
        # not passed, and would not reorder a list if it were.
        identity = hashlib.sha256(
            json.dumps(
                list(task.local_read_source_task_ids),
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        return (
            f"Connector '{connector.manifest.name}' will send {len(sources)} "
            f"bounded local-read excerpt(s) ({total} characters; source-set "
            f"sha256 {identity})."
        )

    def _bind_referenced_model_answer(
        self, session: SessionState, task: Task
    ) -> AgentRun | None:
        """Resolve a handoff's source task and freeze its answer as the bytes.

        The second, equally narrow cross-task data hop after M3. The
        reference is resolved exactly once, here, before retrieval and three
        stages before the first write attestation; after the
        ``context_retrieved`` checkpoint the frozen ``write_content`` is the
        only input M5 ever consumes and the source is never read again.

        Every gate below runs BEFORE a single byte is copied, so a refused
        source leaves ``write_content`` empty, and the refusal is terminal
        ``INVALID_TASK`` before any connector is selected, any approval is
        requested or any attempt is opened. The producer gate is
        manifest-derived like ``_bind_model_answer``'s - an external-model
        connector is one whose OWN manifest declares ``EXTERNAL_EGRESS`` -
        so the provider-neutral core never learns a capability name. The
        classification ladder is ``handoff_eligible`` on the SOURCE TASK's
        exact-typed scope classification; the answer text is never consulted
        for classification or for anything else, and ``write_content`` is
        the ONLY field this method writes.
        """
        if task.write_source is WriteSource.DIRECT:
            return None

        def refuse(detail: str) -> AgentRun:
            return self._terminal_failure(
                session,
                task,
                StructuredError(
                    ErrorCode.INVALID_TASK,
                    f"Model answer handoff is not usable: {detail}.",
                ),
            )

        if task.write_source is not WriteSource.MODEL_ANSWER:
            return refuse("the write source identity is invalid")
        if task.capability != LOCAL_WRITE_CAPABILITY:
            return refuse("a model answer handoff is only valid for a file_write task")
        reference = task.write_source_task_id
        if local_write_source_refusal(reference) is not None:
            return refuse("the source reference is malformed")
        target = task.scope.target
        if type(target) is not str or not target.strip():
            return refuse("the handoff names no destination")
        if reference == task.id:
            return refuse("a task cannot hand off its own model answer")
        if self.memory.task_session_id(reference) != session.id:
            return refuse("the source task belongs to another session")
        try:
            source = self.memory.load_task(reference)
        except Exception:  # noqa: BLE001 - storage boundary; fail closed
            source = None
        if source is None:
            return refuse("the source task could not be loaded")
        if source.status is not TaskStatus.COMPLETED:
            return refuse("the source task did not complete")
        producer_name = source.selected_connector
        if type(producer_name) is not str or not producer_name:
            return refuse("the source task names no producer")
        try:
            producer = self.registry.get(producer_name)
        except Exception:  # noqa: BLE001 - an unknown producer is a refusal
            producer = None
        if producer is None:
            return refuse("the source producer is not a registered connector")
        manifest = getattr(producer, "manifest", None)
        effect = getattr(getattr(manifest, "default_side_effect", None), "effect", None)
        if effect is not SideEffect.EXTERNAL_EGRESS:
            return refuse("the source producer is not an external-model connector")
        answer = source.model_answer
        if type(answer) is not ModelAnswer or not answer.present:
            return refuse("the source task carries no model answer")
        if model_answer_refusal(answer) is not None:
            return refuse("the source model answer is malformed")
        if answer.truncated is not False:
            return refuse("the source model answer is truncated")
        content = answer.content
        if not content.strip():
            return refuse("the source model answer is blank")
        reason = local_write_content_refusal(content)
        if reason is not None:
            return refuse(f"the source model answer is not writable content: {reason}")
        source_classification = source.scope.data_classification
        target_classification = task.scope.data_classification
        if (
            type(source_classification) is not DataClassification
            or type(target_classification) is not DataClassification
        ):
            return refuse("classification is malformed")
        if not handoff_eligible(source_classification, target_classification):
            return refuse("the classification ladder does not allow the handoff")
        task.write_content = content
        return None

    def _bind_project(self, session: SessionState, task: Task) -> AgentRun | None:
        """Deterministic project binding (M1); ``None`` means proceed.

        A task without a project reference never changes the session's
        binding. A referenced project must already exist and be active -
        nothing here can auto-create one - and once a session is bound, a
        conflicting reference fails closed. Binding only scopes retrieval; it
        grants no authority of any kind.
        """
        requested = task.project_id
        if requested is None:
            return None
        if session.project_id is not None:
            if session.project_id != requested:
                return self._input_failure(
                    task,
                    ErrorCode.INVALID_TASK,
                    "Task project does not match the session's project.",
                    session,
                )
            return None
        try:
            project = self.memory.load_project(requested)
        except Exception:  # noqa: BLE001 - storage boundary; fail closed
            project = None
        if project is None or not project.active:
            return self._input_failure(
                task,
                ErrorCode.INVALID_TASK,
                f"Unknown or inactive project: {requested}",
                session,
            )
        session.project_id = requested
        return None

    # ------------------------------------------------------------------
    # M11 S3B. The sequential chain driver.
    #
    # It ORCHESTRATES and never executes. Every step is an ordinary
    # single-action task driven through ``resume`` - the same entry point a
    # standalone task uses - so H1 approval, H2 attempt arbitration, H3
    # reservation, connector selection, execution and result persistence are
    # the existing authorities, reached the existing way. There is no second
    # execution path here to keep in step with the first, because there is no
    # second execution path.
    # ------------------------------------------------------------------

    def run_chain(self, session_id: str) -> tuple[AgentRun, str]:
        """Drive the session's chain forward, one authoritative step at a time.

        Returns the ordinary ``AgentRun`` of the last step it touched and a
        verdict describing why it stopped.

        Every pass re-reads the session and chain from storage before deciding
        anything: an in-memory chain from the previous iteration is a
        SNAPSHOT, and acting on it would be exactly the stale-driver bug the
        S3A progress CAS exists to refuse. The loop is bounded by
        ``MAX_CHAIN_STEPS`` because a chain cannot be longer than that; it is
        a structural bound, not a retry budget.

        It stops at the first thing that is not a clean advance - an approval,
        a failure, a cancellation, an in-doubt attempt, or any authority
        conflict - and never starts a step it has not first proved.
        """
        session = self.memory.load_session(session_id)
        if session is None:
            return (
                self._input_failure(
                    None, ErrorCode.UNKNOWN_SESSION, f"Unknown session: {session_id}"
                ),
                "unknown",
            )
        if session.chain is None:
            return (
                self._input_failure(
                    None,
                    ErrorCode.INVALID_TASK,
                    "This session owns no chain.",
                    session,
                ),
                "unknown",
            )
        last: AgentRun | None = None
        verdict = "blocked"
        for _ in range(MAX_CHAIN_STEPS):
            session = self.memory.load_session(session_id)
            if session is None or session.chain is None:
                verdict = "conflict"
                break
            chain = session.chain
            if chain_state_refusal(chain) is not None:
                verdict = "conflict"
                break
            if chain.status is not ChainStatus.ACTIVE:
                verdict = chain.status.value
                break
            run, verdict = self._run_chain_step(session, chain, chain.cursor)
            if run is not None:
                last = run
            if verdict != "advanced":
                break
        final = self.memory.load_session(session_id)
        if last is None:
            last = AgentRun(RunStatus.COMPLETED, final, None)
        elif final is not None:
            # Report the chain as storage holds it NOW, not as it looked when
            # the step began.
            last = replace(last, session=final)
        return last, verdict

    def _run_chain_step(
        self, session: SessionState, chain: ChainState, index: int
    ) -> tuple[AgentRun | None, str]:
        """Prove, materialize, run and settle exactly one step."""
        task, reason = self.build_chain_step_task(session, chain, index)
        if task is None:
            return (
                self._input_failure(
                    None,
                    ErrorCode.INVALID_TASK,
                    f"Chain step {index} cannot be prepared: {reason}",
                    session,
                ),
                "refused",
            )
        outcome, task_id = self.memory.materialize_chain_step(
            session.id, chain.id, chain.digest, index, task
        )
        if outcome not in ("created", "observed") or not task_id:
            return (
                self._input_failure(
                    None,
                    ErrorCode.INVALID_TASK,
                    f"Chain step {index} could not be bound: {outcome}",
                    session,
                ),
                outcome if outcome else "conflict",
            )
        stored = self.memory.load_task(task_id)
        if stored is None:
            return (
                self._input_failure(
                    None,
                    ErrorCode.TASK_NOT_FOUND,
                    f"Chain step {index} names a task that cannot be loaded.",
                    session,
                ),
                "conflict",
            )
        # The bound row is re-proved HERE too, not only by storage: this is the
        # last point before an ordinary execution begins, and a task that lost
        # its chain authority must not be the one that runs.
        if (
            chain_task_refusal(stored, chain, index, mode=ChainTaskMode.STORED)
            is not None
        ):
            return (
                self._input_failure(
                    stored,
                    ErrorCode.INVALID_TASK,
                    f"Chain step {index} is not this chain's task.",
                    session,
                ),
                "conflict",
            )
        # Registry-side proof, which storage deliberately cannot make. S3A
        # proves the SHAPE of a persisted connector name; only the registry
        # knows whether that connector is a legitimate one for this task.
        refusal = self._chain_connector_refusal(stored)
        if refusal is not None:
            return (
                self._input_failure(
                    stored,
                    ErrorCode.INVALID_TASK,
                    f"Chain step {index} names an illegitimate connector: {refusal}",
                    session,
                ),
                "conflict",
            )
        # The ORDINARY entry point. Everything after this line - approval,
        # billing, the attempt journal, the connector call, verification,
        # persistence - is the existing single-task machinery, unchanged.
        run = self.resume(session.id, task_id)
        if run.status is RunStatus.AWAITING_APPROVAL:
            return run, "awaiting_approval"
        if run.status is RunStatus.AWAITING_CLARIFICATION:
            return run, "awaiting_clarification"
        # Advancement is decided by storage from the STORED task status, never
        # from what this run object happens to say.
        return run, self.memory.advance_chain_step(
            session.id, chain.id, chain.digest, index
        )

    def _chain_connector_refusal(self, task: Task) -> str | None:
        """Why this task's SELECTED connector is not legitimate, or ``None``.

        A stored ``selected_connector`` is a name, and a name proves nothing.
        The registry is the authority on which connector may serve a task, so
        the question is asked here rather than duplicated into storage: the
        connector must be registered, its own manifest must declare the task's
        capability, and it must accept the task.

        ``None`` is the answer when nothing is selected yet - that is the
        ordinary pre-PLAN state, and PLAN will select through the registry in
        the usual way.
        """
        name = task.selected_connector
        if name is None:
            return None
        if type(name) is not str or not name:
            return "the selected connector is malformed"
        try:
            connector = self.registry.get(name)
        except Exception:  # noqa: BLE001 - an unknown connector is a refusal
            connector = None
        if connector is None:
            return "the selected connector is not registered"
        manifest = getattr(connector, "manifest", None)
        capabilities = getattr(manifest, "capabilities", ())
        if task.capability not in capabilities:
            return "the selected connector does not declare this capability"
        try:
            handled = connector.can_handle(task)
        except Exception:  # noqa: BLE001 - connector boundary; fail closed
            handled = False
        if not handled:
            return "the selected connector cannot handle this task"
        return None

    # ------------------------------------------------------------------
    # M11. Trusted derivation of ONE ordinary task from ONE chain step.
    # ------------------------------------------------------------------

    def build_chain_step_task(
        self, session: SessionState, chain: ChainState, index: int
    ) -> tuple[Task | None, str | None]:
        """The ordinary task a chain step would become, or why it cannot.

        Derivation only: this builds a proposal and PROVES the facts that
        materializing it would rely on. It executes nothing, approves nothing
        and writes nothing - storage decides whether the proposal ever becomes
        a row, and every gate the resulting task must pass is unchanged.

        Every field comes from the FROZEN declaration or from trusted policy.
        The objective is copied verbatim, because it is the user's own words
        and nothing may rewrite it. The classification is set EXPLICITLY to
        INTERNAL - the same value the ordinary input boundary applies when the
        field is absent - and is never derived from the objective text, the
        target string, a predecessor's content or any model output. No caller
        task id, no model answer and no retrieved memory reaches any field.

        A predecessor is consumed only through an EXISTING sanctioned
        aperture, chosen by what the consumer is:

            file_read producer -> model consumer : M8 read-context references
            model producer     -> create consumer: M7 write-source reference

        There is no general "consume an arbitrary task result" channel, and
        the declaration's ``destination`` is NEVER producer authority. For a
        create step the producer is proven here the way M7 proves it - the
        predecessor id comes from the chain's own authoritative mapping, the
        task must be same-session and COMPLETED, and its SELECTED CONNECTOR's
        own manifest must declare EXTERNAL_EGRESS. That proof is repeated
        independently by the M7 resolver when the task actually runs; this is
        the earlier of the two, not a replacement for it.
        """

        def refuse(reason: str) -> tuple[None, str]:
            return None, reason

        if type(chain) is not ChainState or chain_state_refusal(chain) is not None:
            return refuse("the chain is not usable")
        if type(index) is not int or not 0 <= index < len(chain.steps):
            return refuse("the step index is outside the chain")
        step = chain.steps[index]
        if step.write_mode and step.write_mode not in CHAIN_ENABLED_WRITE_MODES:
            return refuse("a chain step may not declare this write mode")
        task = Task(
            objective=step.objective,
            capability=step.capability,
            scope=ActionScope(
                action=step.action,
                target=step.target,
                destination=step.destination,
                # Trusted policy, set explicitly and never derived.
                data_classification=DataClassification.INTERNAL,
            ),
            chain_binding=ChainBinding(
                chain_id=chain.id, chain_digest=chain.digest, step_index=index
            ),
        )
        if step.consumes == CHAIN_NO_PREDECESSOR:
            if step.capability == LOCAL_WRITE_CAPABILITY:
                return refuse("a chain write step consumes no producer")
            return task, None
        # The predecessor id comes from the chain's own mapping and from
        # nowhere else: not from the declaration, not from input, not from a
        # model answer.
        predecessor_id = chain.step_task_ids[step.consumes]
        if not predecessor_id:
            return refuse("the predecessor step has produced no task")
        if self.memory.task_session_id(predecessor_id) != session.id:
            return refuse("the predecessor task belongs to another session")
        try:
            source = self.memory.load_task(predecessor_id)
        except Exception:  # noqa: BLE001 - storage boundary; fail closed
            source = None
        if source is None:
            return refuse("the predecessor task could not be loaded")
        if source.status is not TaskStatus.COMPLETED:
            return refuse("the predecessor task did not complete")
        # The COMPLETE proof of the producer, not just its binding:
        # tampering with the producer's objective, scope or policy
        # invalidates it as a source of write bytes.
        if (
            chain_task_refusal(
                source, chain, step.consumes, mode=ChainTaskMode.STORED
            )
            is not None
        ):
            return refuse("the predecessor task is not this chain's step task")
        if step.capability == LOCAL_WRITE_CAPABILITY:
            reason = self._chain_producer_refusal(source)
            if reason is not None:
                return refuse(reason)
            task.write_mode = step.write_mode
            task.write_source = WriteSource.MODEL_ANSWER
            task.write_source_task_id = predecessor_id
            return task, None
        if source.capability != LOCAL_READ_CAPABILITY:
            return refuse("the predecessor task is not a local read")
        task.local_read_context_kind = LocalReadContextKind.REFERENCES
        task.local_read_source_task_ids = (predecessor_id,)
        return task, None

    def _chain_producer_refusal(self, source: Task) -> str | None:
        """Why this completed task cannot be a model-answer producer.

        The same question M7 asks, asked earlier. A declared destination is
        not an answer to it: what proves a producer is the connector that
        actually ran, and its own manifest.
        """
        producer_name = source.selected_connector
        if type(producer_name) is not str or not producer_name:
            return "the predecessor task names no producer"
        try:
            producer = self.registry.get(producer_name)
        except Exception:  # noqa: BLE001 - an unknown producer is a refusal
            producer = None
        if producer is None:
            return "the predecessor producer is not a registered connector"
        manifest = getattr(producer, "manifest", None)
        effect = getattr(getattr(manifest, "default_side_effect", None), "effect", None)
        if effect is not SideEffect.EXTERNAL_EGRESS:
            return "the predecessor producer is not an external-model connector"
        answer = source.model_answer
        if type(answer) is not ModelAnswer or not answer.present:
            return "the predecessor task carries no model answer"
        return None

    @staticmethod
    def _input_failure(
        task: Task | None,
        code: ErrorCode,
        message: str,
        session: SessionState | None = None,
    ) -> AgentRun:
        error = StructuredError(code, message)
        result = ToolResult(
            outcome=OutcomeStatus.ERROR,
            summary=message,
            error=error,
        )
        return AgentRun(RunStatus.FAILED, session, task, result=result, error=error)

    @staticmethod
    def _result_to_dict(result: ToolResult) -> dict[str, Any]:
        """THE canonical serialization, which now lives in ``jarvis.contracts``.

        F3. Storage-side recovery must persist the same terminal-failure
        snapshot this loop persists, so the shape has exactly one definition
        and this delegates to it rather than restating it.
        """
        return result_to_dict(result)

    @staticmethod
    def _result_from_dict(data: dict[str, Any] | None) -> ToolResult | None:
        """Restore a persisted result, or ``None`` when it is not restorable.

        H4-G: a malformed or adversarial snapshot - a missing/unknown outcome,
        a non-mapping body, malformed usage or verification - never becomes
        active state again. Returning ``None`` routes into the existing
        deterministic "result is missing" failure at whichever stage asked, so
        a reloaded bad row fails the same way every time instead of raising or
        retrying forever.
        """
        if not isinstance(data, dict) or not data:
            return None
        try:
            outcome = OutcomeStatus(data["outcome"])
        except (KeyError, TypeError, ValueError):
            return None
        raw_data = data.get("data")
        try:
            usage = UsageRecord.from_dict(data.get("usage"))
            verification = VerificationRecord.from_dict(data.get("verification"))
        except (KeyError, TypeError, ValueError):
            return None
        return ToolResult(
            outcome=outcome,
            summary=sanitize_diagnostic_text(data.get("summary", "")),
            data=raw_data if isinstance(raw_data, dict) else {},
            error=StructuredError.from_dict(data.get("error")),
            usage=usage,
            verification=verification,
        )
