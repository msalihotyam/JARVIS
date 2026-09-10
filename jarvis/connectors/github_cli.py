"""The GitHub read connector: Phase 1's foundation, executed exactly once.

WHAT IT ADDS is a body for a boundary that was already fully specified. The
destination grammar, the closed capability and resource vocabularies, the
request frame an approval binds, the argument vectors and the untrusted
snapshot type are all Phase 1's, unchanged; the process seam is
``jarvis.github_cli``'s. This class only sequences them, and the sequence is
the safety property:

1. rebuild the typed resource from the task's own validated GitHub section,
   and re-judge it - a persisted task is state, never a promise;
2. build the canonical request frame from that resource and the identity
   CORE established, so the bytes an approval binds are the bytes this read
   is about;
3. re-derive the closed argument vector from the frame;
4. re-observe the local session's identity and compare it against Core's
   binding AGAIN, immediately before the call - an approval granted minutes
   ago must not execute under an account that changed since;
5. run the one child process;
6. bind the returned content to the frame that asked for it, and hand back an
   UNTRUSTED snapshot.

WHAT IT CANNOT DO. It cannot write: the write capabilities have no argument
builder anywhere in this build, so a write frame produces no plan and this
manifest does not name one. It cannot widen a repository: the only repository
it can reach is the one inside the validated resource, which Core has already
matched against an exact ``DestinationGrant``. It cannot choose its own
identity: the expected principal is handed in at composition, and what this
class observes is only ever an input to a comparison. It cannot read a
credential: nothing here has one, and the child environment has the whole
token-bearing variable family stripped from it.
"""

from __future__ import annotations

import hashlib
from typing import Any

from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    EXTERNAL_READ_RESULT_KEY,
    CostEstimate,
    ErrorCode,
    OutboundDataPreview,
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
    utc_now,
)
from jarvis.github import (
    EXTERNAL_READ_MAX_CHARS,
    GITHUB_BILLING_CLASS,
    GITHUB_CONNECTOR,
    GITHUB_MONETARY_STATE,
    GITHUB_READ_CAPABILITIES,
    GITHUB_READ_OPERATION,
    GitHubExecutionUnavailable,
    GitHubIdentityBinding,
    GitHubRequestFrame,
    build_external_read_snapshot,
    external_read_binding_refusal,
    github_identity_binding_refusal,
    github_identity_refusal,
    github_operation_plan,
    github_resource_from_dict,
    github_resource_refusal,
)
from jarvis.github_cli import (
    GH_TIMEOUT_SECONDS,
    GitHubCommandFailed,
    observed_github_identity,
    run_operation_plan,
)

#: The one billing evidence string this connector reports, and the same one
#: its estimate claims: an authenticated CLI read is not a metered call.
_BILLING_EVIDENCE = "github_cli:no_metered_call"

#: What the preview tells a person is about to leave the machine. A read
#: sends the repository identity and the typed resource fields - never task
#: text, never file content, never project memory.
_PREVIEW_SUMMARY = (
    "The repository identity and the typed resource fields will be sent to "
    "the GitHub API through the local CLI session."
)


class GitHubCliConnector(BaseConnector):
    """Bounded, read-only GitHub access through the authenticated local CLI."""

    manifest = ConnectorManifest(
        name=GITHUB_CONNECTOR,
        capabilities=GITHUB_READ_CAPABILITIES,
        description=(
            "Bounded read of one GitHub repository's metadata, files, issues, "
            "pull requests, commits and code, through the authenticated "
            "GitHub CLI."
        ),
        priority=50,
        default_risk=RiskLevel.MEDIUM,
        # EXTERNAL_EGRESS with mandatory approval, exactly like the other
        # connector that leaves the machine. A read is still an outbound call
        # naming a repository, so it goes through the same gate rather than a
        # gentler one invented for it.
        default_side_effect=SideEffectProfile(
            effect=SideEffect.EXTERNAL_EGRESS,
            reversible=True,
            always_requires_approval=True,
        ),
        timeout_seconds=GH_TIMEOUT_SECONDS,
        # Explicit, not inherited: the GitHub API costs nothing per call
        # through an already-authenticated CLI. Rate limits are not money.
        default_billing_class=GITHUB_BILLING_CLASS,
        requires_paid_api=False,
    )

    def __init__(
        self,
        binding: GitHubIdentityBinding,
        *,
        identity_observer: Any = observed_github_identity,
        plan_runner: Any = run_operation_plan,
    ) -> None:
        """The expected identity is handed IN, exactly like Core's copy.

        A connector that could construct or edit its own expected identity
        would be attesting to itself. This one is given a binding it cannot
        reach around, validated here so a malformed one fails at composition
        rather than at the first read.
        """
        reason = github_identity_binding_refusal(binding)
        if reason is not None:
            raise ValueError(f"the GitHub identity binding is unusable: {reason}")
        self.binding = binding
        self._observe = identity_observer
        self._run = plan_runner

    # -- billing --------------------------------------------------------------

    def estimate_cost(self, task: Task) -> CostEstimate:
        """Connector-owned and free: an authenticated CLI read spends nothing."""
        return CostEstimate(
            provider="github",
            model=None,
            minimum_usd=0.0,
            maximum_usd=0.0,
            confidence=_BILLING_EVIDENCE,
            billing_class=GITHUB_BILLING_CLASS,
            billing_evidence=_BILLING_EVIDENCE,
            resource_ceiling_usd=None,
            requires_paid_api=False,
        )

    # -- identity -------------------------------------------------------------

    def observed_github_identity(self) -> tuple[str, str]:
        """The account the local session belongs to. AN OBSERVATION.

        Core calls this and compares the answer against its OWN binding. This
        class cannot influence that comparison, and it never reports what it
        would like to be true: an unreadable, ambiguous or non-credential-store
        session raises here, which Core reads as a refusal.
        """
        return self._observe()

    # -- attestation ----------------------------------------------------------

    def _frame(self, task: Task) -> GitHubRequestFrame:
        """The canonical request frame for this task, or fail closed.

        Everything an approval binds is built here from two sources and no
        others: the task's own validated GitHub section, and the identity Core
        established at composition. Change the repository, the resource kind,
        a number, a ref, one character of a path or the expected principal and
        the digest changes, so an approval cannot survive the change.
        """
        resource = github_resource_from_dict(task.github)
        reason = github_resource_refusal(resource)
        if reason is not None:  # pragma: no cover - from_dict already judged it
            raise GitHubExecutionUnavailable(reason)
        return GitHubRequestFrame(
            connector=self.manifest.name,
            capability=task.capability,
            destination=resource.destination,
            resource=resource,
            principal=self.binding.principal,
            credential_route=self.binding.credential_route,
            operation=GITHUB_READ_OPERATION,
        )

    def canonical_outbound(self, task: Task) -> bytes:
        """The exact frame bytes a grant attests. Never the response."""
        return self._frame(task).canonical_bytes()

    def outbound_preview(self, task: Task) -> OutboundDataPreview:
        frame = self._frame(task)
        return OutboundDataPreview(
            destination=frame.destination,
            data_classification=task.scope.data_classification,
            field_names=tuple(sorted(frame.resource.to_dict())),
            approximate_bytes=len(frame.canonical_bytes()),
            redacted_items=0,
            summary=_PREVIEW_SUMMARY,
        )

    def resource_boundary(self, task: Task) -> tuple[str, tuple[str, ...]]:
        """No local boundary at all: this connector reads no local path.

        Stated rather than inherited. The child process runs outside every
        repository on this machine and is handed no working directory of the
        operator's, so there is no local root for a grant to bind.
        """
        return ("", ())

    # -- execution ------------------------------------------------------------

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        """One read, or one refusal. Never a partial or assumed answer."""
        try:
            frame = self._frame(task)
        except (GitHubExecutionUnavailable, ValueError) as exc:
            return self._refusal(task, ErrorCode.INVALID_TASK, str(exc))
        if frame.capability not in GITHUB_READ_CAPABILITIES:
            return self._refusal(
                task,
                ErrorCode.EGRESS_REFUSED,
                "the capability is not a GitHub read capability",
            )
        try:
            plan = github_operation_plan(frame)
        except GitHubExecutionUnavailable as exc:
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, str(exc))

        # (4) THE IDENTITY, AGAIN, HERE. Core compared it before the approval
        # gate; between that moment and this one the local session can be
        # switched to another account, and an approval for one principal must
        # never execute as a different one.
        try:
            observed = self._observe()
        except Exception as exc:  # noqa: BLE001 - an unreportable identity refuses
            return self._refusal(
                task,
                ErrorCode.EGRESS_REFUSED,
                "the authenticated GitHub identity could not be observed",
            )
        if type(observed) is not tuple or len(observed) != 2:
            return self._refusal(
                task,
                ErrorCode.EGRESS_REFUSED,
                "the observed GitHub identity is not a (principal, route) pair",
            )
        reason = github_identity_refusal(
            self.binding,
            observed_principal=observed[0],
            observed_credential_route=observed[1],
        )
        if reason is not None:
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, reason)

        try:
            output = self._run(plan)
        except GitHubCommandFailed as exc:
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, str(exc))
        except Exception:  # noqa: BLE001 - an unclassified failure is a refusal
            return self._refusal(
                task, ErrorCode.CONNECTOR_FAILED, "the GitHub read did not complete"
            )

        content = output.stdout
        truncated = bool(output.truncated)
        if len(content) > EXTERNAL_READ_MAX_CHARS:
            content = content[:EXTERNAL_READ_MAX_CHARS]
            truncated = True
        try:
            snapshot = build_external_read_snapshot(
                source_task_id=task.id,
                frame=frame,
                content=content,
                captured_at=utc_now(),
                truncated=truncated,
            )
        except Exception as exc:  # noqa: BLE001 - AttestationError and anything else
            return self._refusal(
                task, ErrorCode.EGRESS_REFUSED, "the response could not be attested"
            )
        reason = external_read_binding_refusal(snapshot, frame=frame)
        if reason is not None:  # pragma: no cover - the builder derived it
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, reason)
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=(
                f"Read {frame.resource.kind.value} from {frame.destination}"
                f"{' (truncated)' if snapshot.truncated else ''}."
            ),
            data={EXTERNAL_READ_RESULT_KEY: snapshot.to_dict()},
            usage=self._usage(task, session_id=session.id, call_count=1),
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        """Verify against the snapshot AgentCore actually bound to the task.

        Not against ``result.data``: that mapping is reduced at the execution
        boundary, so the durable evidence is the task field - and that field
        is what Core bound only after re-proving that the content answers THIS
        authorized request. Evidence is metadata only: the digest, the size
        and the resource identity, never a line of the content itself.
        """
        stored = task.external_read
        if not isinstance(stored, dict) or not stored:
            return VerificationRecord(
                status=VerificationStatus.FAILED,
                method="github_external_read_integrity",
                evidence=("no bounded external-read snapshot on the task",),
            )
        try:
            frame = self._frame(task)
        except (GitHubExecutionUnavailable, ValueError):
            return VerificationRecord(
                status=VerificationStatus.FAILED,
                method="github_external_read_integrity",
                evidence=("the task no longer names a valid GitHub request",),
            )
        # The digest is re-computed from the stored content rather than read
        # off the record beside it, so a row whose content and digest disagree
        # fails here instead of being believed.
        content = stored.get("content", "")
        if type(content) is not str:
            return VerificationRecord(
                status=VerificationStatus.FAILED,
                method="github_external_read_integrity",
                evidence=("the stored external-read content is not text",),
            )
        encoded = content.encode("utf-8")
        if (
            stored.get("sha256") != hashlib.sha256(encoded).hexdigest()
            or stored.get("byte_size") != len(encoded)
            or stored.get("destination") != frame.destination
            or stored.get("resource_id") != frame.resource.identity
        ):
            return VerificationRecord(
                status=VerificationStatus.FAILED,
                method="github_external_read_integrity",
                evidence=("the stored snapshot does not describe this request",),
            )
        return VerificationRecord(
            status=VerificationStatus.PASSED,
            method="github_external_read_integrity",
            evidence=(
                f"destination={frame.destination}",
                f"resource_kind={frame.resource.kind.value}",
                f"byte_size={stored.get('byte_size')}",
                f"sha256={stored.get('sha256')}",
                f"truncated={'true' if stored.get('truncated') else 'false'}",
            ),
        )

    # -- helpers --------------------------------------------------------------

    def _usage(
        self, task: Task, *, session_id: str | None = None, call_count: int = 0
    ) -> UsageRecord:
        """The unbilled route, reported exactly as the claim classified it.

        ``monetary_cost_usd`` is ``None`` rather than ``0.0`` and the state is
        NOT_APPLICABLE: an authenticated CLI read through the GitHub API is
        not metered, and reporting any monetary amount for an unbilled route
        fails H3 reconciliation closed - correctly, because it would be a
        claim about money that nothing spent.
        """
        return UsageRecord(
            provider="github",
            model=None,
            connector=self.manifest.name,
            session_id=session_id,
            task_id=task.id,
            call_count=call_count,
            billing_class=GITHUB_BILLING_CLASS,
            monetary_state=GITHUB_MONETARY_STATE,
            monetary_cost_usd=None,
            billing_evidence=_BILLING_EVIDENCE,
        )

    def _refusal(self, task: Task, code: ErrorCode, message: str) -> ToolResult:
        """One shape for every refusal: nothing happened, nothing to retry.

        ``retryable`` is False throughout. Every failure above is a policy or
        identity refusal, an unusable request, or a call that did not complete
        - and none of those become true by being asked again.
        """
        return ToolResult(
            outcome=OutcomeStatus.ERROR,
            summary=message,
            error=StructuredError(code, message, {}, False),
            usage=self._usage(task),
        )


__all__ = ["GitHubCliConnector"]
