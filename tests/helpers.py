from __future__ import annotations

from pathlib import Path

from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    ActionScope,
    Availability,
    CostEstimate,
    DataClassification,
    ErrorCode,
    OutcomeStatus,
    RiskLevel,
    SessionState,
    SideEffect,
    SideEffectProfile,
    StructuredError,
    Task,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
)


class DummyConnector(BaseConnector):
    def __init__(
        self,
        *,
        name: str = "dummy",
        risk: RiskLevel = RiskLevel.LOW,
        side_effect: SideEffect = SideEffect.NONE,
        reversible: bool = True,
        always_requires_approval: bool = False,
        priority: int = 10,
        suitability: float = 1.0,
        available: bool = True,
        estimate_max: float = 0.0,
        is_paid: bool = False,
        actual_cost: float = 0.0,
        verification_passes: bool = True,
        outcome: OutcomeStatus = OutcomeStatus.SUCCESS,
    ) -> None:
        self.manifest = ConnectorManifest(
            name=name,
            capabilities=("echo",),
            priority=priority,
            default_risk=risk,
            default_side_effect=SideEffectProfile(
                effect=side_effect,
                reversible=reversible,
                always_requires_approval=always_requires_approval,
            ),
            is_paid=is_paid,
        )
        self._suitability = suitability
        self._available = available
        self._estimate_max = estimate_max
        self._is_paid = is_paid
        self.actual_cost = actual_cost
        self.verification_passes = verification_passes
        self.outcome = outcome
        self.execute_calls = 0
        self.cancel_calls = 0

    def availability(self) -> Availability:
        return Availability(self._available, "disabled for test" if not self._available else "")

    def suitability(self, task: Task) -> float:
        return self._suitability

    def estimate_cost(self, task: Task) -> CostEstimate:
        return CostEstimate(
            provider="test-provider",
            model="test-model",
            minimum_usd=0.0,
            maximum_usd=self._estimate_max,
            confidence="test",
            is_paid=self._is_paid,
        )

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        self.execute_calls += 1
        usage = UsageRecord(
            provider="test-provider",
            model="test-model",
            connector=self.manifest.name,
            actual_cost_usd=self.actual_cost,
            is_paid=self.actual_cost > 0,
        )
        if self.outcome is OutcomeStatus.ERROR:
            return ToolResult(
                outcome=OutcomeStatus.ERROR,
                summary="dummy failure",
                error=StructuredError(ErrorCode.CONNECTOR_FAILED, "dummy failure"),
                usage=usage,
            )
        if self.outcome is OutcomeStatus.CANCELLED:
            return ToolResult(
                outcome=OutcomeStatus.CANCELLED,
                summary="dummy cancelled",
                error=StructuredError(ErrorCode.CANCELLED, "dummy cancelled"),
                usage=usage,
            )
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=task.objective,
            data={"next_step": "continue testing"},
            usage=usage,
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        if self.verification_passes and result.ok:
            return VerificationRecord(
                status=VerificationStatus.PASSED,
                method="dummy_independent_check",
                evidence=("deterministic-test-evidence",),
            )
        return VerificationRecord(
            status=VerificationStatus.FAILED,
            method="dummy_independent_check",
            evidence=("verification-failed",),
        )

    def cancel(self, task_id: str) -> bool:
        self.cancel_calls += 1
        return True


def make_task(
    *,
    target: str | Path = "resource-a",
    risk: RiskLevel = RiskLevel.LOW,
    effect: SideEffect = SideEffect.NONE,
    reversible: bool = True,
    always_requires_approval: bool = False,
    classification: DataClassification = DataClassification.INTERNAL,
) -> Task:
    return Task(
        objective="echo this",
        capability="echo",
        scope=ActionScope(
            action="read",
            target=str(target),
            destination="test-destination" if effect is SideEffect.EXTERNAL_EGRESS else "",
            data_classification=classification,
        ),
        risk_level=risk,
        side_effect=SideEffectProfile(
            effect=effect,
            reversible=reversible,
            always_requires_approval=always_requires_approval,
        ),
    )
