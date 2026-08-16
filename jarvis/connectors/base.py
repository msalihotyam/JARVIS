"""Shared connector interface for every JARVIS capability."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace

from jarvis.contracts import (
    ActionScope,
    Availability,
    CostEstimate,
    OutboundDataPreview,
    PermissionRequest,
    RiskLevel,
    SessionState,
    SideEffect,
    SideEffectProfile,
    Task,
    ToolResult,
    VerificationRecord,
    VerificationStatus,
    future_time,
)


@dataclass(frozen=True, slots=True)
class ConnectorManifest:
    name: str
    capabilities: tuple[str, ...]
    description: str = ""
    priority: int = 0
    default_risk: RiskLevel = RiskLevel.LOW
    default_side_effect: SideEffectProfile = SideEffectProfile()
    timeout_seconds: int = 120
    is_paid: bool = False


class BaseConnector(ABC):
    manifest: ConnectorManifest

    def can_handle(self, task: Task) -> bool:
        return task.capability in self.manifest.capabilities

    def suitability(self, task: Task) -> float:
        return 1.0 if self.can_handle(task) else 0.0

    def availability(self) -> Availability:
        return Availability(True)

    def estimate_cost(self, task: Task) -> CostEstimate:
        estimate = task.estimate
        if self.manifest.is_paid and not estimate.is_paid:
            return replace(estimate, is_paid=True)
        return estimate

    def side_effects(self, task: Task) -> SideEffectProfile:
        task_effect = task.side_effect
        if task_effect != SideEffectProfile() or self.manifest.default_side_effect.effect is SideEffect.NONE:
            return task_effect
        return self.manifest.default_side_effect

    def outbound_preview(self, task: Task) -> OutboundDataPreview | None:
        return None

    def permission_request(
        self,
        task: Task,
        session_id: str,
        approval_ttl_minutes: int = 30,
        *,
        scope_override: ActionScope | None = None,
        reason: str | None = None,
    ) -> PermissionRequest:
        risk = max(task.risk_level, self.manifest.default_risk)
        effect = self.side_effects(task)
        base_scope = scope_override or task.scope
        scope = ActionScope(
            action=base_scope.action,
            target=base_scope.target,
            connector=self.manifest.name,
            destination=base_scope.destination,
            data_classification=base_scope.data_classification,
        ).normalized()
        return PermissionRequest(
            task_id=task.id,
            session_id=session_id,
            scope=scope,
            risk_level=risk,
            side_effect=effect,
            reason=reason or f"Connector '{self.manifest.name}' will execute the scoped action.",
            outbound_preview=self.outbound_preview(task),
            expires_at=future_time(approval_ttl_minutes),
        )

    @abstractmethod
    def execute(self, task: Task, session: SessionState) -> ToolResult:
        """Execute one task and return a structured, unverified result."""

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        return VerificationRecord(
            status=VerificationStatus.INCONCLUSIVE,
            method="connector_provided_no_verifier",
            evidence=(),
        )

    def cancel(self, task_id: str) -> bool:
        return False
