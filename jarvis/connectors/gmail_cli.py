"""The Gmail connector: minimum usable-v1 reads, one draft, one send.

It sequences ``jarvis.gmail`` and ``jarvis.gmail_cli`` and adds nothing of
its own; the sequence is the safety property:

1. rebuild the typed resource from the task's own validated Gmail section
   and re-judge it - a persisted task is state, never a promise;
2. build the canonical request frame from that resource and the identity
   CORE established, so the bytes an approval binds - for a write, the exact
   RFC 2822 message JARVIS built - are the bytes this operation is about;
3. re-derive the closed argument vector from the frame, every row naming the
   bound account in its request path;
4. run the one child process;
5. for a read, bind the bounded untrusted content to the frame that asked
   for it and hand back a snapshot; for a write, hand back the identifiers
   the API acknowledged - or let an unanswered write escape as an unknown
   outcome rather than claim it did not happen.

WHAT IT CANNOT DO. It cannot reach another mailbox: the only account it can
name is the one inside the validated resource, which must equal Core's own
binding and which Core has matched against an exact ``DestinationGrant``. It
cannot widen a message: recipients, subject and body are typed fields inside
the approved frame, so model-provided text is content and never a recipient.
It cannot archive, delete, label, reply, monitor or process attachments:
those names do not exist in the vocabulary, so no task can ask for them. It
cannot read a credential: nothing here has one, and the child environment
has every Google configuration family stripped from it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any

from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    EXTERNAL_READ_RESULT_KEY,
    AttestationError,
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
from jarvis.gmail import (
    EXTERNAL_READ_MAX_CHARS,
    GMAIL_BILLING_CLASS,
    GMAIL_CONNECTOR,
    GMAIL_ENABLED_CAPABILITIES,
    GMAIL_MONETARY_STATE,
    GMAIL_WRITE_CAPABILITIES,
    GmailExecutionUnavailable,
    GmailIdentityBinding,
    GmailOutcomeUnknown,
    GmailRequestFrame,
    GmailWriteOutcome,
    build_gmail_read_snapshot,
    build_gmail_request_frame,
    gmail_external_read_binding_refusal,
    gmail_identity_binding_refusal,
    gmail_operation_plan,
    gmail_read_content,
    gmail_resource_from_dict,
    gmail_write_outcome,
)
from jarvis.gmail_cli import (
    GWS_WRITE_TIMEOUT_SECONDS,
    GmailCommandExited,
    GmailCommandFailed,
    GmailCommandTimedOut,
    observed_gmail_identity,
    run_operation_plan,
)

_BILLING_EVIDENCE = "gmail_cli:no_metered_call"

_READ_PREVIEW = (
    "The mailbox identity and the typed resource fields will be sent to the "
    "Gmail API through the local Google Workspace CLI session."
)

#: The one sentence a successful write reports. It is the durable evidence
#: the verifier reads back, so it is a fixed grammar rather than prose.
_ACKNOWLEDGEMENT = re.compile(
    r"\AGmail (draft|send) acknowledged: message id=([A-Za-z0-9_-]+) "
    r"thread id=([A-Za-z0-9_-]+)(?: draft id=([A-Za-z0-9_-]+))?\Z"
)


def _acknowledgement(outcome: GmailWriteOutcome) -> str:
    text = (
        f"Gmail {outcome.operation} acknowledged: message id={outcome.message_id} "
        f"thread id={outcome.thread_id}"
    )
    if outcome.draft_id:
        text += f" draft id={outcome.draft_id}"
    return text


class GmailCliConnector(BaseConnector):
    """Bounded Gmail reads, one draft and one approved send, through the
    authenticated local Google Workspace CLI."""

    manifest = ConnectorManifest(
        name=GMAIL_CONNECTOR,
        capabilities=GMAIL_ENABLED_CAPABILITIES,
        description=(
            "Bounded listing, search and reading of one mailbox's threads and "
            "messages, creating one draft, and sending one message behind "
            "exact approval, through the authenticated Google Workspace CLI."
        ),
        priority=50,
        default_risk=RiskLevel.MEDIUM,
        # EXTERNAL_EGRESS with mandatory approval for every operation: a read
        # is still an outbound call naming a mailbox. A write is strengthened
        # to irreversible in ``side_effects`` below.
        default_side_effect=SideEffectProfile(
            effect=SideEffect.EXTERNAL_EGRESS,
            reversible=True,
            always_requires_approval=True,
        ),
        timeout_seconds=GWS_WRITE_TIMEOUT_SECONDS,
        default_billing_class=GMAIL_BILLING_CLASS,
        requires_paid_api=False,
    )

    def __init__(
        self,
        binding: GmailIdentityBinding,
        *,
        identity_observer: Any = observed_gmail_identity,
        plan_runner: Any = run_operation_plan,
    ) -> None:
        """The expected identity is handed IN, exactly like Core's copy."""
        reason = gmail_identity_binding_refusal(binding)
        if reason is not None:
            raise ValueError(f"the Gmail identity binding is unusable: {reason}")
        self.binding = binding
        self._observe = identity_observer
        self._run = plan_runner

    # -- billing --------------------------------------------------------------

    def estimate_cost(self, task: Task) -> CostEstimate:
        return CostEstimate(
            provider="gmail",
            model=None,
            minimum_usd=0.0,
            maximum_usd=0.0,
            confidence=_BILLING_EVIDENCE,
            billing_class=GMAIL_BILLING_CLASS,
            billing_evidence=_BILLING_EVIDENCE,
            resource_ceiling_usd=None,
            requires_paid_api=False,
        )

    # -- effects --------------------------------------------------------------

    def side_effects(self, task: Task) -> SideEffectProfile:
        """A draft or a send cannot be undone by this product: irreversible.

        Strengthened, never weakened: the manifest floor (egress, mandatory
        approval) is kept and only ``reversible`` is lowered, for the write
        capabilities only.
        """
        profile = super().side_effects(task)
        if task.capability in GMAIL_WRITE_CAPABILITIES:
            profile = replace(profile, reversible=False)
        return profile

    # -- identity -------------------------------------------------------------

    def observed_gmail_identity(self) -> tuple[str, str]:
        """The account the DEDICATED session acts as. AN OBSERVATION: the
        seam proves the credential route under the bound config directory
        and then the effective account, by name; Core compares the answer
        against its OWN binding."""
        return self._observe(self.binding.principal, self.binding.config_dir)

    # -- attestation ----------------------------------------------------------

    def _frame(self, task: Task) -> GmailRequestFrame:
        """The canonical request frame for this task, or fail closed.

        Built from the task's own validated Gmail section and the identity
        Core established, and nothing else. Change the account, the kind, an
        id, one recipient, one character of the subject or body, or the
        expected principal and the digest changes.
        """
        resource = gmail_resource_from_dict(task.gmail)
        return build_gmail_request_frame(
            capability=task.capability,
            destination=resource.destination,
            resource=resource,
            binding=self.binding,
            connector=self.manifest.name,
        )

    def canonical_outbound(self, task: Task) -> bytes:
        """The exact frame bytes a grant attests. For a write they cover the
        exact message bytes through ``body_sha256``."""
        return self._frame(task).canonical_bytes()

    def outbound_preview(self, task: Task) -> OutboundDataPreview:
        frame = self._frame(task)
        resource = frame.resource
        if frame.capability in GMAIL_WRITE_CAPABILITIES:
            verb = "SEND MAIL" if frame.operation == "send" else "Create a draft"
            summary = (
                f"{verb} as {resource.account} to {', '.join(resource.recipients)} "
                f"(subject: {resource.subject[:80]}); the exact message bytes are "
                "bound by this approval."
            )[:400]
        else:
            summary = _READ_PREVIEW
        return OutboundDataPreview(
            destination=frame.destination,
            data_classification=task.scope.data_classification,
            field_names=tuple(sorted(resource.to_dict())),
            approximate_bytes=len(frame.canonical_bytes()),
            redacted_items=0,
            summary=summary,
        )

    def resource_boundary(self, task: Task) -> tuple[str, tuple[str, ...]]:
        """No local boundary: the child runs outside every directory of the
        operator's and reads no local path."""
        return ("", ())

    # -- execution ------------------------------------------------------------

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        """One operation, one refusal, or - for a write only - one escaping
        unknown outcome. Never a partial or assumed answer."""
        try:
            frame = self._frame(task)
        except (GmailExecutionUnavailable, AttestationError, ValueError) as exc:
            return self._refusal(task, ErrorCode.INVALID_TASK, str(exc))
        if frame.capability not in GMAIL_ENABLED_CAPABILITIES:
            return self._refusal(
                task, ErrorCode.EGRESS_REFUSED, "the capability is not an enabled Gmail operation"
            )
        try:
            plan = gmail_operation_plan(frame)
        except GmailExecutionUnavailable as exc:
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, str(exc))
        if frame.capability in GMAIL_WRITE_CAPABILITIES:
            return self._write(task, session, frame, plan)
        return self._read(task, session, frame, plan)

    def _read(self, task: Task, session: SessionState, frame: GmailRequestFrame, plan: Any) -> ToolResult:
        try:
            output = self._run(plan, config_dir=self.binding.config_dir)
        except GmailCommandFailed as exc:
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, str(exc))
        except Exception:  # noqa: BLE001 - an unclassified failure is a refusal
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, "the Gmail read did not complete")
        try:
            content = gmail_read_content(output.stdout, frame=frame)
        except GmailExecutionUnavailable as exc:
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, str(exc))
        truncated = bool(output.truncated)
        if len(content) > EXTERNAL_READ_MAX_CHARS:
            content = content[:EXTERNAL_READ_MAX_CHARS]
            truncated = True
        try:
            snapshot = build_gmail_read_snapshot(
                source_task_id=task.id,
                frame=frame,
                content=content,
                captured_at=utc_now(),
                truncated=truncated,
            )
        except Exception:  # noqa: BLE001 - AttestationError and anything else
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, "the response could not be attested")
        reason = gmail_external_read_binding_refusal(snapshot, frame=frame)
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

    def _write(self, task: Task, session: SessionState, frame: GmailRequestFrame, plan: Any) -> ToolResult:
        """One draft or one send.

        A refusal here means nothing left the machine: the plan or the
        credential route was refused, the CLI could not be started, or the
        local dry run refused the request. Once the child has started,
        NOTHING short of a readable acknowledgement is proof of no effect: a
        nonzero exit, a timeout, an unclassified failure and an
        acknowledgement that cannot be read all ESCAPE as
        ``GmailOutcomeUnknown`` so Core marks the attempt in doubt.
        """
        try:
            output = self._run(plan, config_dir=self.binding.config_dir)
        except GmailOutcomeUnknown:
            raise
        except (GmailCommandExited, GmailCommandTimedOut) as exc:
            # The seam already maps these for a write; kept here so no
            # started-child failure can ever read as "nothing happened".
            raise GmailOutcomeUnknown(
                f"the Gmail {frame.operation} started and did not succeed: {exc}"
            ) from exc
        except GmailCommandFailed as exc:
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, str(exc))
        except Exception as exc:  # noqa: BLE001 - after the process may have started
            raise GmailOutcomeUnknown(
                f"the Gmail {frame.operation} did not complete and its outcome is unknown"
            ) from exc
        outcome = gmail_write_outcome(output.stdout, frame=frame)
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=_acknowledgement(outcome),
            data={"gmail_outcome": outcome.to_dict()},
            usage=self._usage(task, session_id=session.id, call_count=1),
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        """Verify a read against the snapshot Core bound to the task, and a
        write against the acknowledgement the API returned.

        A read's evidence is metadata only - digest, size, resource identity.
        A write's only evidence is the API's own acknowledgement: the
        identifiers it returned for THIS operation, read back from the
        bounded summary that survives the result boundary.
        """
        try:
            frame = self._frame(task)
        except (GmailExecutionUnavailable, AttestationError, ValueError):
            return self._verification(False, "the task no longer names a valid Gmail request")
        if frame.capability in GMAIL_WRITE_CAPABILITIES:
            match = _ACKNOWLEDGEMENT.fullmatch(result.summary or "")
            if (
                result.outcome is not OutcomeStatus.SUCCESS
                or match is None
                or match.group(1) != frame.operation
            ):
                return self._verification(False, "the API acknowledged no such operation")
            return self._verification(
                True,
                f"operation={frame.operation}",
                f"destination={frame.destination}",
                f"message_id={match.group(2)}",
                f"thread_id={match.group(3)}",
            )
        stored = task.external_read
        if not isinstance(stored, dict) or not stored:
            return self._verification(False, "no bounded external-read snapshot on the task")
        content = stored.get("content", "")
        if type(content) is not str:
            return self._verification(False, "the stored external-read content is not text")
        encoded = content.encode("utf-8")
        if (
            stored.get("sha256") != hashlib.sha256(encoded).hexdigest()
            or stored.get("byte_size") != len(encoded)
            or stored.get("destination") != frame.destination
            or stored.get("resource_id") != frame.resource.identity
        ):
            return self._verification(False, "the stored snapshot does not describe this request")
        return self._verification(
            True,
            f"destination={frame.destination}",
            f"resource_kind={frame.resource.kind.value}",
            f"byte_size={stored.get('byte_size')}",
            f"sha256={stored.get('sha256')}",
            f"truncated={'true' if stored.get('truncated') else 'false'}",
        )

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _verification(passed: bool, *evidence: str) -> VerificationRecord:
        return VerificationRecord(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            method="gmail_operation_integrity",
            evidence=tuple(evidence),
        )

    def _usage(
        self, task: Task, *, session_id: str | None = None, call_count: int = 0
    ) -> UsageRecord:
        return UsageRecord(
            provider="gmail",
            model=None,
            connector=self.manifest.name,
            session_id=session_id,
            task_id=task.id,
            call_count=call_count,
            billing_class=GMAIL_BILLING_CLASS,
            monetary_state=GMAIL_MONETARY_STATE,
            monetary_cost_usd=None,
            billing_evidence=_BILLING_EVIDENCE,
        )

    def _refusal(self, task: Task, code: ErrorCode, message: str) -> ToolResult:
        """One shape for every refusal: nothing happened, nothing to retry."""
        return ToolResult(
            outcome=OutcomeStatus.ERROR,
            summary=message,
            error=StructuredError(code, message, {}, False),
            usage=self._usage(task),
        )


__all__ = ["GmailCliConnector"]
