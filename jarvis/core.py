"""Resumable, API-free JARVIS Agent Loop state machine."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from jarvis.connectors.base import BaseConnector
from jarvis.contracts import (
    COST_CAP_GATE,
    PAID_API_GATE,
    ActionScope,
    AgentRun,
    AttemptState,
    AttestationError,
    BillingClaim,
    BillingClass,
    BillingIntegrityError,
    BillingReconciliation,
    CostEstimate,
    DataClassification,
    DecisionRecord,
    ErrorCode,
    ExecutionAttempt,
    ExecutionClaimOutcome,
    LedgerSnapshot,
    LoopStage,
    MemoryRetrievalQuery,
    MemoryStore,
    MonetaryState,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    ProgressIssueKind,
    ResultVerifier,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    StructuredError,
    Task,
    TaskStatus,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    canonical_usd,
    format_usd,
    is_expired,
    nano_usd,
    require_billing_class,
    require_monetary_state,
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
        session = self.memory.load_session(session_id)
        task = self.memory.load_task(task_id)
        if session is None:
            return self._input_failure(
                task, ErrorCode.UNKNOWN_SESSION, f"Unknown session: {session_id}"
            )
        if task is None or self.memory.task_session_id(task_id) != session_id:
            return self._input_failure(
                task, ErrorCode.TASK_NOT_FOUND, f"Unknown task: {task_id}", session
            )
        # Fast path: a terminal task is reported as-is (the atomic op below
        # re-checks terminality authoritatively).
        terminal = self._terminal_run(session, task)
        if terminal is not None:
            return terminal
        # Best-effort connector cancellation signal, taken before the atomic
        # arbitration; its result only matters when an attempt is IN_FLIGHT.
        cancelled = False
        if task.selected_connector:
            try:
                cancelled = self.registry.get(task.selected_connector).cancel(task.id)
            except ConnectorNotFoundError:
                cancelled = False
        # Prepare the pending-cancellation view; the atomic op upgrades it to a
        # final CANCELLED only if no execution has been claimed (or the
        # connector confirmed the stop), all within one transaction (P1-B).
        task.cancellation_requested = True
        task.status = TaskStatus.CANCEL_REQUESTED
        session.latest_task_id = task.id
        clear_issues(task, *NON_FAILURE_ISSUE_KINDS)
        set_issue(task, ProgressIssueKind.CANCELLATION_PENDING)
        outcome = self.memory.cancel_task(session, task, connector_cancelled=cancelled)
        if outcome == "terminal":
            # Execution legitimately reached a terminal state first; never
            # overwrite it. Report the authoritative persisted state.
            fresh = self.memory.load_task(task.id)
            reported = self._terminal_run(session, fresh) if fresh is not None else None
            return reported if reported is not None else AgentRun(
                RunStatus.CANCELLED, session, fresh or task
            )
        if outcome == "cancelled":
            return AgentRun(RunStatus.CANCELLED, session, task)
        if outcome == "cancelling":
            return AgentRun(RunStatus.CANCELLING, session, task)
        return self._input_failure(
            task, ErrorCode.TASK_NOT_FOUND, f"Unknown task: {task_id}", session
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
                )
                retrieved = self.memory.retrieve_context(query)
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
                        {"question": task.preferences.get("clarification_question", "Clarification required")},
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
                    {"assumption": task.visible_assumption},
                )
                continue

            if task.stage is LoopStage.PLAN:
                # Always rebuilt from safe structural task state: a persisted
                # or tampered plan is never preserved while the task is still
                # at PLAN. A task resumed past this stage keeps its persisted
                # plan because PLAN is not re-entered.
                task.plan = build_task_plan(task)
                try:
                    selection = self.registry.select(task)
                except Exception as exc:
                    return self._selection_failure(session, task, exc)
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
                availability = connector.availability()
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
                external = current.side_effect.effect is SideEffect.EXTERNAL_EGRESS
                try:
                    result = connector.execute(task, session)
                except Exception as exc:  # noqa: BLE001 - connector boundary
                    if external:
                        # The external outcome is unknowable after a raise:
                        # mark the attempt non-replayable rather than inventing
                        # a clean failure that could be safely retried. The
                        # reservation stays active, so the money the call may
                        # already have spent keeps counting as exposure.
                        self.memory.mark_execution_unknown(attempt.id)
                        return self._execution_in_doubt(
                            session,
                            task,
                            self.memory.latest_execution_attempt(task.id),
                        )
                    result = ToolResult(
                        outcome=OutcomeStatus.ERROR,
                        summary=f"Connector failed: {exc}",
                        error=StructuredError(
                            ErrorCode.CONNECTOR_FAILED,
                            f"Connector failed: {exc}",
                            retryable=True,
                        ),
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
                verification = (
                    self.verifier.verify(task, result, session)
                    if self.verifier is not None
                    else connector.verify(task, result, session)
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
                    {"summary": result.summary, "next_step": session.next_step},
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
                decision = DecisionRecord(
                    session_id=session.id,
                    task_id=task.id,
                    summary=result.summary,
                    rationale=(
                        f"Connector={connector.manifest.name}; "
                        f"verification={result.verification.method}; "
                        f"assumption={task.visible_assumption}"
                    ),
                    next_step=session.next_step,
                )
                # All-or-nothing with lifecycle authority: if a cancellation
                # already won the terminal transition, no decision, usage
                # rewrite, terminal event or session overwrite is persisted and
                # the authoritative state is reported instead of a phantom
                # COMPLETED built on this stale task.
                if not self.memory.finalize_task(
                    session,
                    task,
                    decision,
                    result.usage,
                    "task_completed",
                    {"summary": result.summary, "next_step": session.next_step},
                ):
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
            return connector.permission_request(
                task,
                session.id,
                self.settings.approval_ttl_minutes,
                scope_override=scope_override,
                reason=reason,
            )
        except AttestationError as exc:
            return self._terminal_failure(
                session, task, StructuredError(exc.code, str(exc))
            )

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
        return AgentRun(
            RunStatus.FAILED,
            session,
            task,
            error=StructuredError(
                ErrorCode.EXECUTION_UNKNOWN_OUTCOME,
                "A prior execution attempt is in doubt and must not be replayed "
                "automatically; explicit recovery is required.",
                details=(
                    {"attempt_state": attempt.state.value} if attempt is not None else {}
                ),
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
        estimate = connector.estimate_cost(task)
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
            StructuredError(code, str(exc), retryable=code is ErrorCode.CONNECTOR_UNAVAILABLE),
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
        return {
            "outcome": result.outcome.value,
            "summary": result.summary,
            "data": result.data,
            "error": result.error.to_dict() if result.error else None,
            "usage": result.usage.to_dict(),
            "verification": result.verification.to_dict(),
        }

    @staticmethod
    def _result_from_dict(data: dict[str, Any] | None) -> ToolResult | None:
        if not data:
            return None
        return ToolResult(
            outcome=OutcomeStatus(data["outcome"]),
            summary=data["summary"],
            data=data.get("data", {}),
            error=StructuredError.from_dict(data.get("error")),
            usage=UsageRecord.from_dict(data.get("usage")),
            verification=VerificationRecord.from_dict(data.get("verification")),
        )
