"""Shared connector interface for every JARVIS capability."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace

from jarvis.contracts import (
    ActionScope,
    AttestationError,
    Availability,
    BillingClass,
    CostEstimate,
    EgressAttestation,
    ErrorCode,
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
    merge_side_effects,
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
    #: Fail-closed by default: a connector that never declares or derives its
    #: billing route stays UNKNOWN and is refused before execution. A local or
    #: free connector must say NO_ADDITIONAL_CHARGE explicitly; connectors with
    #: more than one possible route must override estimate_cost().
    default_billing_class: BillingClass = BillingClass.UNKNOWN
    #: Whether this connector needs the separate ``paid_api_enabled`` switch.
    requires_paid_api: bool = False


class BaseConnector(ABC):
    manifest: ConnectorManifest

    def can_handle(self, task: Task) -> bool:
        return task.capability in self.manifest.capabilities

    def suitability(self, task: Task) -> float:
        return 1.0 if self.can_handle(task) else 0.0

    def availability(self) -> Availability:
        return Availability(True)

    def estimate_cost(self, task: Task) -> CostEstimate:
        # The connector, never the task, is the source of billing truth: a
        # persisted task must not be able to carry its own "this is free"
        # classification into execution.
        return replace(
            task.estimate,
            billing_class=self.manifest.default_billing_class,
            billing_evidence="connector_manifest_default_route",
            requires_paid_api=(
                task.estimate.requires_paid_api or self.manifest.requires_paid_api
            ),
        )

    def side_effects(self, task: Task) -> SideEffectProfile:
        """Effective profile; the manifest is the non-weakenable lower bound.

        Task or persisted state may strengthen it, never remove the
        connector-owned egress status, mandatory approval or irreversibility.
        Conflicting effects raise ``AttestationError`` and fail closed.
        """
        return merge_side_effects(self.manifest.default_side_effect, task.side_effect)

    def outbound_preview(self, task: Task) -> OutboundDataPreview | None:
        return None

    def canonical_outbound(self, task: Task) -> bytes:
        """The exact bytes this connector sends externally; none by default.

        The default is only valid for a connector that performs no external
        egress. A connector whose effective side effect is EXTERNAL_EGRESS
        must override this (and ``external_tool_grants`` if it grants tools);
        otherwise ``egress_attestation`` fails closed rather than letting an
        empty-bytes digest masquerade as a real attestation.
        """
        return b""

    def external_tool_grants(self, task: Task) -> tuple[str, ...]:
        """External tools the launched process is granted; none by default.

        Every grant widens what the external model can receive beyond the
        attested outbound bytes, so the set is bound into the attestation.
        """
        return ()

    def resource_boundary(self, task: Task) -> tuple[str, tuple[str, ...]]:
        """Canonical working directory and allowed roots an external process
        can inspect. A purely local connector exposes nothing."""
        return "", ()

    def egress_attestation(
        self,
        task: Task,
        scope: ActionScope,
        risk: RiskLevel,
        effect: SideEffectProfile,
    ) -> EgressAttestation:
        """Attest exactly what a grant would authorize for this task now.

        Built from the same inputs execution will use; ``task.estimate`` must
        be the connector-derived estimate AgentCore refreshes before every
        gate, so a persisted estimate can never be attested. A connector that
        declares external egress but never implemented a canonical outbound
        contract fails closed here - an empty default digest is not a valid
        attestation for something that sends data out.
        """
        if (
            effect.effect is SideEffect.EXTERNAL_EGRESS
            and type(self).canonical_outbound is BaseConnector.canonical_outbound
        ):
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED,
                f"Connector '{self.manifest.name}' declares external egress but "
                "does not implement a canonical outbound attestation.",
            )
        working_directory, allowed_roots = self.resource_boundary(task)
        return EgressAttestation(
            scope=scope,
            risk_level=risk,
            side_effect=effect,
            outbound_digest=hashlib.sha256(self.canonical_outbound(task)).hexdigest(),
            billing_gate=task.estimate.gate_state,
            working_directory=working_directory,
            allowed_roots=allowed_roots,
            tool_grants=tuple(self.external_tool_grants(task)),
        )

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
            attestation=self.egress_attestation(task, scope, risk, effect),
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
