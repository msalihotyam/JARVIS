"""Resumable, API-free JARVIS Agent Loop state machine."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from jarvis.connectors.base import BaseConnector
from jarvis.contracts import (
    ActionScope,
    AgentRun,
    CostEstimate,
    DataClassification,
    DecisionRecord,
    ErrorCode,
    LoopStage,
    MemoryStore,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    ResultVerifier,
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
    is_expired,
)
from jarvis.registry import (
    AmbiguousConnectorError,
    ConnectorNotFoundError,
    ConnectorUnavailableError,
    ToolRegistry,
)
from jarvis.settings import Settings


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
        session.active_task_id = task.id
        self.memory.persist_checkpoint(session, task, "task_registered")
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
        task.preferences["clarification"] = clarification
        task.status = TaskStatus.PENDING
        task.stage = LoopStage.CLARIFY_OR_ASSUME
        self.memory.persist_checkpoint(session, task, "clarification_provided")
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
        task.cancellation_requested = True
        task.status = TaskStatus.CANCEL_REQUESTED
        cancelled = False
        if task.selected_connector:
            try:
                cancelled = self.registry.get(task.selected_connector).cancel(task.id)
            except ConnectorNotFoundError:
                cancelled = False
        if task.stage is not LoopStage.EXECUTE or cancelled:
            task.status = TaskStatus.CANCELLED
            task.stage = LoopStage.CANCELLED
            session.active_task_id = None
        self.memory.persist_checkpoint(
            session,
            task,
            "cancellation_requested",
            {"connector_cancelled": cancelled},
        )
        return AgentRun(RunStatus.CANCELLED, session, task)

    def _advance(
        self,
        session: SessionState,
        task: Task,
        grant: PermissionGrant | None,
    ) -> AgentRun:
        active_grant = grant
        while True:
            if task.cancellation_requested:
                task.status = TaskStatus.CANCELLED
                task.stage = LoopStage.CANCELLED
                session.active_task_id = None
                self.memory.persist_checkpoint(session, task, "task_cancelled")
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
                task.stage = LoopStage.CONTEXT_RETRIEVAL
                self.memory.persist_checkpoint(
                    session, task, "intent_understood", {"objective": task.objective}
                )
                continue

            if task.stage is LoopStage.CONTEXT_RETRIEVAL:
                decisions = self.memory.list_decisions(session.id)
                task.context["retrieved_decision_count"] = len(decisions)
                task.stage = LoopStage.CLARIFY_OR_ASSUME
                self.memory.persist_checkpoint(
                    session,
                    task,
                    "context_retrieved",
                    {"decision_count": len(decisions)},
                )
                continue

            if task.stage is LoopStage.CLARIFY_OR_ASSUME:
                needs_clarification = bool(task.preferences.get("needs_clarification"))
                clarification = str(task.preferences.get("clarification") or "").strip()
                if needs_clarification and not clarification:
                    task.status = TaskStatus.AWAITING_CLARIFICATION
                    self.memory.persist_checkpoint(
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
                task.stage = LoopStage.PLAN
                task.status = TaskStatus.PENDING
                self.memory.persist_checkpoint(
                    session,
                    task,
                    "assumption_visible",
                    {"assumption": task.visible_assumption},
                )
                continue

            if task.stage is LoopStage.PLAN:
                if not task.plan:
                    task.plan = [
                        "Validate intent and retrieved context",
                        "Apply permission and monthly cost policies",
                        "Select one deterministic available connector",
                        "Execute within the normalized scope",
                        "Verify with explicit method and evidence",
                        "Present and persist the decision, rationale, and next step",
                    ]
                try:
                    selection = self.registry.select(task)
                except Exception as exc:
                    return self._selection_failure(session, task, exc)
                task.selected_connector = selection.connector.manifest.name
                task.stage = LoopStage.RISK_PERMISSION
                self.memory.persist_checkpoint(
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
                request = self.memory.load_pending_approval(task.id, session.id)
                if request is None or is_expired(request.expires_at):
                    request = connector.permission_request(
                        task,
                        session.id,
                        self.settings.approval_ttl_minutes,
                    )
                if self._requires_approval(request):
                    if self._remembered_permission_allowed(request) and self.memory.has_remembered_scope_grant(
                        session.id, request.scope_fingerprint
                    ):
                        task.stage = LoopStage.COST_PREFLIGHT
                        self.memory.persist_checkpoint(
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
                task.stage = LoopStage.COST_PREFLIGHT
                task.status = TaskStatus.PENDING
                self.memory.persist_checkpoint(session, task, "permission_policy_passed")
                continue

            if task.stage is LoopStage.COST_PREFLIGHT:
                estimate = connector.estimate_cost(task)
                task.estimate = estimate
                current_spend = self.memory.monthly_actual_cost()
                projected = current_spend + estimate.maximum_usd
                request: PermissionRequest | None = None
                waiting_status = RunStatus.AWAITING_COST_OVERRIDE
                if estimate.is_paid and not self.settings.paid_api_enabled and not task.preferences.get(
                    "paid_api_task_approval"
                ):
                    request = self._cost_permission_request(
                        task,
                        session,
                        connector,
                        action="enable_paid_api_for_task",
                        reason="This task would use a paid API while paid APIs are disabled.",
                    )
                    waiting_status = RunStatus.AWAITING_APPROVAL
                elif (
                    estimate.maximum_usd > 0
                    and projected > self.settings.monthly_hard_limit_usd
                    and not task.preferences.get("cost_cap_override")
                ):
                    request = self._cost_permission_request(
                        task,
                        session,
                        connector,
                        action="override_monthly_cost_cap",
                        reason=(
                            f"Projected monthly cost {projected:.4f} USD exceeds the "
                            f"{self.settings.monthly_hard_limit_usd:.2f} USD hard cap."
                        ),
                    )
                if request is not None:
                    pending = self.memory.load_pending_approval(task.id, session.id)
                    if (
                        pending is not None
                        and not is_expired(pending.expires_at)
                        and pending.scope.action == request.scope.action
                    ):
                        request = pending
                    approval_run, consumed = self._handle_approval(
                        session, task, request, active_grant, waiting_status
                    )
                    if approval_run is not None:
                        return approval_run
                    if request.scope.action.endswith("enable_paid_api_for_task"):
                        task.preferences["paid_api_task_approval"] = request.id
                    else:
                        task.preferences["cost_cap_override"] = request.id
                    if consumed:
                        active_grant = None
                task.stage = LoopStage.TOOL_SELECTION
                self.memory.persist_checkpoint(
                    session,
                    task,
                    "cost_preflight_passed",
                    {
                        "current_month_usd": current_spend,
                        "estimated_max_usd": estimate.maximum_usd,
                        "projected_usd": projected,
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
                task.stage = LoopStage.EXECUTE
                self.memory.persist_checkpoint(
                    session,
                    task,
                    "tool_selected",
                    {"connector": connector.manifest.name},
                )
                continue

            if task.stage is LoopStage.EXECUTE:
                task.status = TaskStatus.RUNNING
                try:
                    result = connector.execute(task, session)
                except Exception as exc:
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
                            estimated_cost_usd=task.estimate.maximum_usd,
                        ),
                    )
                result.usage.session_id = session.id
                result.usage.task_id = task.id
                result.usage.connector = result.usage.connector or connector.manifest.name
                result.usage.estimated_cost_usd = max(
                    result.usage.estimated_cost_usd, task.estimate.maximum_usd
                )
                self.memory.append_usage(result.usage)
                task.result_snapshot = self._result_to_dict(result)
                if result.outcome is OutcomeStatus.CANCELLED:
                    task.status = TaskStatus.CANCELLED
                    task.stage = LoopStage.CANCELLED
                    session.active_task_id = None
                    self.memory.persist_checkpoint(session, task, "execution_cancelled")
                    return AgentRun(RunStatus.CANCELLED, session, task, result=result)
                if not result.ok:
                    return self._terminal_failure(
                        session,
                        task,
                        result.error
                        or StructuredError(ErrorCode.CONNECTOR_FAILED, result.summary),
                        result,
                    )
                task.status = TaskStatus.VERIFYING
                task.stage = LoopStage.VERIFY
                self.memory.persist_checkpoint(session, task, "execution_completed")
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
                task.stage = LoopStage.PRESENT
                self.memory.persist_checkpoint(
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
                session.next_step = str(
                    result.data.get("next_step") or "Review the verified result."
                )
                task.stage = LoopStage.PERSIST
                self.memory.persist_checkpoint(
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
                self.memory.finalize_task(
                    session,
                    task,
                    decision,
                    result.usage,
                    "task_completed",
                    {"summary": result.summary, "next_step": session.next_step},
                )
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
            self.memory.persist_checkpoint(
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
            self.memory.persist_checkpoint(
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
        status = "approved" if grant.approved else "rejected"
        self.memory.resolve_approval(request, grant, status)
        if not grant.approved:
            task.status = TaskStatus.REJECTED
            task.stage = LoopStage.FAILED
            session.active_task_id = None
            error = StructuredError(
                ErrorCode.APPROVAL_REJECTED, "The user rejected this action."
            )
            self.memory.persist_checkpoint(
                session, task, "approval_rejected", error.to_dict()
            )
            return (
                AgentRun(RunStatus.REJECTED, session, task, error=error),
                True,
            )
        task.status = TaskStatus.PENDING
        return None, True

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
    ) -> PermissionRequest:
        scope = ActionScope(
            action=action,
            target=f"monthly-budget:{self.settings.monthly_hard_limit_usd:.2f}-usd",
            connector=connector.manifest.name,
            destination=task.scope.destination,
            data_classification=DataClassification.INTERNAL,
        )
        billing_task = replace(
            task,
            risk_level=RiskLevel.HIGH,
            side_effect=SideEffectProfile(
                effect=SideEffect.BILLING,
                reversible=False,
                always_requires_approval=True,
            ),
        )
        return connector.permission_request(
            billing_task,
            session.id,
            self.settings.approval_ttl_minutes,
            scope_override=scope,
            reason=reason,
        )

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
        if result is None:
            result = ToolResult(
                outcome=OutcomeStatus.ERROR,
                summary=error.message,
                error=error,
                usage=UsageRecord(
                    session_id=session.id,
                    task_id=task.id,
                    connector=task.selected_connector,
                    estimated_cost_usd=task.estimate.maximum_usd,
                ),
            )
        task.result_snapshot = self._result_to_dict(result)
        self.memory.persist_checkpoint(
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
