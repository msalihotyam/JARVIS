"""The Windows Desktop connector: minimum usable-v1 reads and bounded input.

It sequences ``jarvis.desktop`` and ``jarvis.desktop_win32`` and adds nothing
of its own, exactly like the browser connector:

1. rebuild the typed resource from the task's own validated desktop section
   and re-judge it - a persisted task is state, never a promise;
2. build the canonical request frame from it, so the bytes an approval
   binds - the application, the capability, the window, the element ref,
   the inspection digest, the exact text or key - are the bytes this
   operation is about;
3. run the one operation, whose seam re-verifies the window and element
   against live Windows state and refuses before dispatch on any drift;
4. for a read, bind the bounded untrusted content to the frame; for an
   interaction, report what Windows reported - or let a dispatched input
   whose effect is not established escape as an unknown outcome.
"""

from __future__ import annotations

import hashlib
import re
import threading
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
from jarvis.desktop import (
    DESKTOP_BILLING_CLASS,
    DESKTOP_CONNECTOR,
    DESKTOP_ENABLED_CAPABILITIES,
    DESKTOP_INTERACT_CAPABILITIES,
    DESKTOP_MONETARY_STATE,
    DesktopExecutionUnavailable,
    DesktopOutcomeUnknown,
    DesktopRequestFrame,
    build_desktop_read_snapshot,
    build_desktop_request_frame,
    desktop_outcome,
    desktop_resource_from_dict,
)
from jarvis.desktop_win32 import DESKTOP_TIMEOUT_SECONDS, run_desktop_operation
from jarvis.github import EXTERNAL_READ_MAX_CHARS

_BILLING_EVIDENCE = "desktop_uia:no_metered_call"
_COMPLETED = re.compile(r"\ADesktop (\w+) completed: (\S.*)\Z")


class DesktopUiaConnector(BaseConnector):
    """Bounded control of the configured Windows applications."""

    manifest = ConnectorManifest(
        name=DESKTOP_CONNECTOR,
        capabilities=DESKTOP_ENABLED_CAPABILITIES,
        description=(
            "Launching, listing, inspecting and focusing the windows of one "
            "authorized Windows application, and invoking or writing into "
            "elements an inspection printed, by calls bound to that element."
        ),
        priority=50,
        default_risk=RiskLevel.MEDIUM,
        # The browser's footing, for the browser's reason: what an application
        # does with a click or a keystroke can leave the machine, so every
        # operation needs an exact approval, and a raise after dispatch is an
        # unknown outcome rather than a clean, retryable failure.
        default_side_effect=SideEffectProfile(
            effect=SideEffect.EXTERNAL_EGRESS,
            reversible=True,
            always_requires_approval=True,
        ),
        timeout_seconds=DESKTOP_TIMEOUT_SECONDS,
        default_billing_class=DESKTOP_BILLING_CLASS,
        requires_paid_api=False,
    )

    def __init__(self, *, operation_runner: Any = run_desktop_operation) -> None:
        self._run = operation_runner
        self._cancel = threading.Event()
        self._live = False

    def estimate_cost(self, task: Task) -> CostEstimate:
        return CostEstimate(
            provider="desktop",
            model=None,
            minimum_usd=0.0,
            maximum_usd=0.0,
            confidence=_BILLING_EVIDENCE,
            billing_class=DESKTOP_BILLING_CLASS,
            billing_evidence=_BILLING_EVIDENCE,
            resource_ceiling_usd=None,
            requires_paid_api=False,
        )

    def side_effects(self, task: Task) -> SideEffectProfile:
        """Strengthened, never weakened: an interaction is irreversible."""
        profile = super().side_effects(task)
        if task.capability in DESKTOP_INTERACT_CAPABILITIES:
            profile = replace(profile, reversible=False)
        return profile

    def _frame(self, task: Task) -> DesktopRequestFrame:
        resource = desktop_resource_from_dict(task.desktop)
        return build_desktop_request_frame(
            capability=task.capability,
            destination=resource.destination,
            resource=resource,
            connector=self.manifest.name,
        )

    def canonical_outbound(self, task: Task) -> bytes:
        return self._frame(task).canonical_bytes()

    def outbound_preview(self, task: Task) -> OutboundDataPreview:
        frame = self._frame(task)
        resource = frame.resource
        summary = (
            f"{frame.capability} on {frame.app}"
            + (f" window {resource.window_id}" if resource.window_id else "")
            + (f" element {resource.ref}" if resource.ref else "")
            + (f" text: {resource.text[:80]}" if resource.text else "")
            + "; this approval binds that exact application, window and element state."
        )[:400]
        return OutboundDataPreview(
            destination=frame.destination,
            data_classification=task.scope.data_classification,
            field_names=tuple(sorted(resource.to_dict())),
            approximate_bytes=len(frame.canonical_bytes()),
            redacted_items=0,
            summary=summary,
        )

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        try:
            frame = self._frame(task)
        except (DesktopExecutionUnavailable, AttestationError, ValueError) as exc:
            return self._refusal(task, ErrorCode.INVALID_TASK, str(exc))
        self._cancel.clear()
        self._live = True
        try:
            if frame.capability in DESKTOP_INTERACT_CAPABILITIES:
                return self._interact(task, session, frame)
            return self._read(task, session, frame)
        finally:
            self._live = False

    def _read(self, task: Task, session: SessionState, frame: DesktopRequestFrame) -> ToolResult:
        try:
            output = self._run(frame, cancelled=self._cancel.is_set)
        except DesktopExecutionUnavailable as exc:
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, str(exc))
        except Exception:  # noqa: BLE001 - a read that failed changed nothing
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, "the desktop read did not complete")
        content = output.get("content") if isinstance(output, dict) else None
        if type(content) is not str:
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, "Windows returned no readable state")
        truncated = len(content) > EXTERNAL_READ_MAX_CHARS
        try:
            snapshot = build_desktop_read_snapshot(
                source_task_id=task.id,
                frame=frame,
                content=content[:EXTERNAL_READ_MAX_CHARS],
                captured_at=utc_now(),
                truncated=truncated,
            )
        except Exception:  # noqa: BLE001
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, "the response could not be attested")
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=f"Read {frame.resource.kind.value} of {frame.app}.",
            data={EXTERNAL_READ_RESULT_KEY: snapshot.to_dict()},
            usage=self._usage(task, session_id=session.id, call_count=1),
        )

    def _interact(self, task: Task, session: SessionState, frame: DesktopRequestFrame) -> ToolResult:
        """A refusal here is definite: nothing was dispatched, or the observed
        end state is known. Anything else after dispatch is an unknown
        outcome, raised so Core marks the attempt in doubt."""
        try:
            output = self._run(frame, cancelled=self._cancel.is_set)
        except DesktopOutcomeUnknown:
            raise
        except DesktopExecutionUnavailable as exc:
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, str(exc))
        except Exception as exc:  # noqa: BLE001 - after input may have left
            raise DesktopOutcomeUnknown(
                f"the {frame.capability} did not complete and its outcome is unknown"
            ) from exc
        outcome = desktop_outcome(output, frame=frame)
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=f"Desktop {outcome.capability} completed: {outcome.detail}",
            data={"desktop_outcome": outcome.to_dict()},
            usage=self._usage(task, session_id=session.id, call_count=1),
        )

    def verify(self, task: Task, result: ToolResult, session: SessionState) -> VerificationRecord:
        try:
            frame = self._frame(task)
        except (DesktopExecutionUnavailable, AttestationError, ValueError):
            return self._verification(False, "the task no longer names a valid desktop request")
        if frame.capability in DESKTOP_INTERACT_CAPABILITIES:
            match = _COMPLETED.fullmatch(result.summary or "")
            if result.outcome is not OutcomeStatus.SUCCESS or match is None or match.group(1) != frame.capability:
                return self._verification(False, "Windows reported no such operation")
            return self._verification(
                True, f"capability={frame.capability}", f"destination={frame.destination}", match.group(2)
            )
        stored = task.external_read
        content = stored.get("content") if isinstance(stored, dict) else None
        if type(content) is not str:
            return self._verification(False, "no bounded external-read snapshot on the task")
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
            f"sha256={stored.get('sha256')}",
        )

    def cancel(self, task_id: str) -> bool:
        """Stop THIS instance's live operation before it dispatches input.

        Advisory, as the base contract says: the seam checks the flag
        immediately before the one dispatching call, so a cancellation that
        arrives in time refuses the operation with nothing sent, and one that
        arrives later changes nothing - the dispatched input settles as
        whatever Windows reported, or as an unknown outcome.
        """
        if not self._live:
            return False
        self._cancel.set()
        return True

    @staticmethod
    def _verification(passed: bool, *evidence: str) -> VerificationRecord:
        return VerificationRecord(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            method="desktop_operation_integrity",
            evidence=tuple(evidence),
        )

    def _usage(self, task: Task, *, session_id: str | None = None, call_count: int = 0) -> UsageRecord:
        return UsageRecord(
            provider="desktop",
            model=None,
            connector=self.manifest.name,
            session_id=session_id,
            task_id=task.id,
            call_count=call_count,
            billing_class=DESKTOP_BILLING_CLASS,
            monetary_state=DESKTOP_MONETARY_STATE,
            monetary_cost_usd=None,
            billing_evidence=_BILLING_EVIDENCE,
        )

    def _refusal(self, task: Task, code: ErrorCode, message: str) -> ToolResult:
        return ToolResult(
            outcome=OutcomeStatus.ERROR,
            summary=message,
            error=StructuredError(code, message, {}, False),
            usage=self._usage(task),
        )


__all__ = ["DesktopUiaConnector"]
