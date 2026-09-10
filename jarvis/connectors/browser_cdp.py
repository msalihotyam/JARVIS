"""The browser connector: minimum usable-v1 reads and bounded interactions.

It sequences ``jarvis.browser`` and ``jarvis.browser_cdp`` and adds nothing of
its own; the sequence is the safety property:

1. rebuild the typed resource from the task's own validated browser section
   and re-judge it - a persisted task is state, never a promise;
2. build the canonical request frame from that resource, so the bytes an
   approval binds - the origin, the capability, the exact URL, the exact
   element ref and the exact page-state digest - are the bytes this
   operation is about;
3. re-derive the closed method set from the frame, which the protocol
   connection then enforces at the socket;
4. run the one operation;
5. for a read, bind the bounded untrusted content to the frame that asked
   for it and hand back a snapshot; for an interaction, hand back where the
   browser landed - or let an unacknowledged interaction escape as an
   unknown outcome rather than claim it did not happen.

WHAT IT CANNOT DO. It cannot leave the bound origin: the destination is
``browser/<origin>``, Core matched it against an exact ``DestinationGrant``,
a URL outside it is not even expressible in the resource grammar, and an
interaction on a tab that has drifted outside it is refused. It cannot
invent a target: an element is named by a ``ref`` a previous reading printed
plus the digest of that reading, so a page that moved refuses rather than
being re-matched. It cannot run script, take a screenshot, read a cookie,
download or upload: those names do not exist in the vocabulary. It cannot
type a credential: a credential-shaped string is refused by the grammar and
a password field is refused at the element.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any

from jarvis.browser import (
    BROWSER_BILLING_CLASS,
    BROWSER_CONNECTOR,
    BROWSER_ENABLED_CAPABILITIES,
    BROWSER_INTERACT_CAPABILITIES,
    BROWSER_MONETARY_STATE,
    BrowserExecutionUnavailable,
    BrowserOriginBlocked,
    BrowserOutcome,
    BrowserOutcomeUnknown,
    BrowserRequestFrame,
    browser_external_read_binding_refusal,
    browser_operation_plan,
    browser_outcome,
    browser_resource_from_dict,
    build_browser_read_snapshot,
    build_browser_request_frame,
    url_in_origin,
)
from jarvis.browser_cdp import (
    BROWSER_COMMAND_TIMEOUT_SECONDS,
    BrowserCommandFailed,
    BrowserCommandStarted,
    run_operation_plan,
)
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
from jarvis.github import EXTERNAL_READ_MAX_CHARS

_BILLING_EVIDENCE = "browser_cdp:no_metered_call"

_READ_PREVIEW = (
    "The bound origin and the typed target will be sent to the local browser "
    "over its loopback debugging socket; the page's own content comes back as "
    "untrusted reference material."
)

#: The one sentence a successful interaction reports. Durable evidence the
#: verifier reads back, so it is a fixed grammar rather than prose.
_ACKNOWLEDGEMENT = re.compile(
    r"\ABrowser (\w+) acknowledged: tab=([0-9A-Fa-f]{32}) url=(\S*)\Z"
)


def _acknowledgement(outcome: BrowserOutcome) -> str:
    return (
        f"Browser {outcome.capability} acknowledged: tab={outcome.tab_id} "
        f"url={outcome.url}"
    )


class BrowserCdpConnector(BaseConnector):
    """Bounded browsing of one authorized origin through the local browser."""

    manifest = ConnectorManifest(
        name=BROWSER_CONNECTOR,
        capabilities=BROWSER_ENABLED_CAPABILITIES,
        description=(
            "Bounded navigation, reading, clicking, typing, waiting and tab "
            "handling inside one authorized web origin, through the local "
            "browser's loopback debugging socket."
        ),
        priority=50,
        default_risk=RiskLevel.MEDIUM,
        # EXTERNAL_EGRESS with mandatory approval for every operation: even a
        # page reading is an outbound act naming an origin, and it brings
        # untrusted content back. Interactions are strengthened to
        # irreversible in ``side_effects`` below.
        default_side_effect=SideEffectProfile(
            effect=SideEffect.EXTERNAL_EGRESS,
            reversible=True,
            always_requires_approval=True,
        ),
        timeout_seconds=BROWSER_COMMAND_TIMEOUT_SECONDS,
        default_billing_class=BROWSER_BILLING_CLASS,
        requires_paid_api=False,
    )

    def __init__(self, profile_dir: str, *, plan_runner: Any = run_operation_plan) -> None:
        """The dedicated profile location is handed IN, like Core's own state."""
        if type(profile_dir) is not str or not profile_dir:
            raise ValueError("the browser profile directory is unusable")
        self.profile_dir = profile_dir
        self._run = plan_runner
        self._connection: Any = None

    # -- billing --------------------------------------------------------------

    def estimate_cost(self, task: Task) -> CostEstimate:
        return CostEstimate(
            provider="browser",
            model=None,
            minimum_usd=0.0,
            maximum_usd=0.0,
            confidence=_BILLING_EVIDENCE,
            billing_class=BROWSER_BILLING_CLASS,
            billing_evidence=_BILLING_EVIDENCE,
            resource_ceiling_usd=None,
            requires_paid_api=False,
        )

    # -- effects --------------------------------------------------------------

    def side_effects(self, task: Task) -> SideEffectProfile:
        """An interaction cannot be undone by this product: irreversible.

        Strengthened, never weakened: the manifest floor (egress, mandatory
        approval) is kept and only ``reversible`` is lowered, for the
        interaction capabilities only.
        """
        profile = super().side_effects(task)
        if task.capability in BROWSER_INTERACT_CAPABILITIES:
            profile = replace(profile, reversible=False)
        return profile

    # -- attestation ----------------------------------------------------------

    def _frame(self, task: Task) -> BrowserRequestFrame:
        """The canonical request frame for this task, or fail closed.

        Built from the task's own validated browser section and nothing else.
        Change the origin, the capability, one character of the URL, the ref
        or the page digest and the digest changes.
        """
        resource = browser_resource_from_dict(task.browser)
        return build_browser_request_frame(
            capability=task.capability,
            destination=resource.destination,
            resource=resource,
            connector=self.manifest.name,
        )

    def canonical_outbound(self, task: Task) -> bytes:
        """The exact frame bytes a grant attests."""
        return self._frame(task).canonical_bytes()

    def outbound_preview(self, task: Task) -> OutboundDataPreview:
        frame = self._frame(task)
        resource = frame.resource
        if frame.capability in BROWSER_INTERACT_CAPABILITIES:
            summary = (
                f"{frame.capability} on {frame.destination}"
                + (f" -> {resource.url}" if resource.url else "")
                + (f" element {resource.ref}" if resource.ref else "")
                + (f" text: {resource.text[:80]}" if resource.text else "")
                + "; this approval binds that exact target and page state."
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
        """No local boundary: the browser reads no path of the operator's on
        JARVIS's behalf, and this connector opens no file."""
        return ("", ())

    # -- execution ------------------------------------------------------------

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        """One operation, one refusal, or - for an interaction only - one
        escaping unknown outcome. Never a partial or assumed answer."""
        try:
            frame = self._frame(task)
        except (BrowserExecutionUnavailable, AttestationError, ValueError) as exc:
            return self._refusal(task, ErrorCode.INVALID_TASK, str(exc))
        if frame.capability not in BROWSER_ENABLED_CAPABILITIES:
            return self._refusal(
                task,
                ErrorCode.EGRESS_REFUSED,
                "the capability is not an enabled browser operation",
            )
        try:
            plan = browser_operation_plan(frame)
        except BrowserExecutionUnavailable as exc:
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, str(exc))
        if frame.capability in BROWSER_INTERACT_CAPABILITIES:
            return self._interact(task, session, frame, plan)
        return self._read(task, session, frame, plan)

    def _read(self, task: Task, session: SessionState, frame: BrowserRequestFrame, plan: Any) -> ToolResult:
        try:
            output = self._run(
                plan,
                frame=frame,
                profile_dir=self.profile_dir,
                connection_sink=self._hold,
            )
        except BrowserCommandFailed as exc:
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, str(exc))
        except Exception:  # noqa: BLE001 - an unclassified failure is a refusal
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, "the browser read did not complete")
        content = output.payload.get("content")
        if type(content) is not str:
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, "the browser returned no readable page state")
        truncated = bool(output.truncated)
        if len(content) > EXTERNAL_READ_MAX_CHARS:
            content = content[:EXTERNAL_READ_MAX_CHARS]
            truncated = True
        try:
            snapshot = build_browser_read_snapshot(
                source_task_id=task.id,
                frame=frame,
                content=content,
                captured_at=utc_now(),
                truncated=truncated,
            )
        except Exception:  # noqa: BLE001 - AttestationError and anything else
            return self._refusal(task, ErrorCode.EGRESS_REFUSED, "the response could not be attested")
        reason = browser_external_read_binding_refusal(snapshot, frame=frame)
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

    def _interact(
        self, task: Task, session: SessionState, frame: BrowserRequestFrame, plan: Any
    ) -> ToolResult:
        """One click, one text entry, one navigation or one tab operation.

        A refusal here means nothing reached the page: the plan was refused,
        the browser could not be reached, the tab was ambiguous, the page had
        moved since it was read, the element could not be identified
        confidently, or it was a password field. Once the browser has been
        asked to ACT, NOTHING short of a readable acknowledgement is proof of
        no effect: a protocol error, a closed socket, a timeout and an
        unreadable answer all ESCAPE as ``BrowserOutcomeUnknown`` so Core
        marks the attempt in doubt.
        """
        try:
            output = self._run(
                plan,
                frame=frame,
                profile_dir=self.profile_dir,
                connection_sink=self._hold,
            )
        except BrowserOutcomeUnknown:
            raise
        except BrowserOriginBlocked as exc:
            # THE DESTINATION BOUNDARY, enforced rather than observed. The
            # approved origin was contacted, it answered with a redirect out
            # of that origin, and that request was refused before it was sent.
            # A definite outcome, so it is neither a "nothing happened"
            # refusal nor an unknown one - and never a success. Terminal:
            # repeating it would only contact the approved origin again to be
            # redirected to the same place.
            message = (
                f"Browser {frame.capability} refused: {exc}. The approval named "
                f"{frame.destination} and authorizes no other origin."
            )
            return ToolResult(
                outcome=OutcomeStatus.ERROR,
                summary=message,
                data={"browser_blocked_origin": exc.blocked},
                error=StructuredError(ErrorCode.EGRESS_REFUSED, message, {}, False),
                usage=self._usage(task, session_id=session.id, call_count=1),
            )
        except BrowserCommandStarted as exc:
            raise BrowserOutcomeUnknown(
                f"the browser {frame.capability} started and did not succeed: {exc}"
            ) from exc
        except BrowserCommandFailed as exc:
            return self._refusal(task, ErrorCode.CONNECTOR_FAILED, str(exc))
        except Exception as exc:  # noqa: BLE001 - after the browser may have acted
            raise BrowserOutcomeUnknown(
                f"the browser {frame.capability} did not complete and its outcome is unknown"
            ) from exc
        outcome = browser_outcome(output.payload, frame=frame)
        # WHERE IT LANDED. A navigation that ended outside the authorized
        # origin really happened, so it is reported as what it is - a
        # completed action with an unauthorized landing - and never as a
        # plain success. Terminal and non-retryable: repeating it would
        # repeat the navigation.
        if outcome.url and not url_in_origin(outcome.url, frame.origin):
            message = (
                f"{_acknowledgement(outcome)}; the browser left the authorized "
                f"origin {frame.origin}, so this action is reported as completed "
                "with an unauthorized landing and must not be repeated."
            )
            return ToolResult(
                outcome=OutcomeStatus.ERROR,
                summary=message,
                data={"browser_outcome": outcome.to_dict()},
                error=StructuredError(ErrorCode.EGRESS_REFUSED, message, {}, False),
                usage=self._usage(task, session_id=session.id, call_count=1),
            )
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=_acknowledgement(outcome),
            data={"browser_outcome": outcome.to_dict()},
            usage=self._usage(task, session_id=session.id, call_count=1),
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        """Verify a read against the snapshot Core bound to the task, and an
        interaction against the acknowledgement the browser returned."""
        try:
            frame = self._frame(task)
        except (BrowserExecutionUnavailable, AttestationError, ValueError):
            return self._verification(False, "the task no longer names a valid browser request")
        if frame.capability in BROWSER_INTERACT_CAPABILITIES:
            match = _ACKNOWLEDGEMENT.fullmatch(result.summary or "")
            if (
                result.outcome is not OutcomeStatus.SUCCESS
                or match is None
                or match.group(1) != frame.capability
            ):
                return self._verification(False, "the browser acknowledged no such operation")
            return self._verification(
                True,
                f"capability={frame.capability}",
                f"destination={frame.destination}",
                f"tab_id={match.group(2)}",
                f"url={match.group(3)}",
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

    # -- cancellation ---------------------------------------------------------

    def cancel(self, task_id: str) -> bool:
        """Close this instance's live protocol connection, if it has one.

        Advisory and owner-local, exactly as the base contract says. Closing
        the socket makes the in-flight command fail; whether that command had
        already reached the page is precisely what the execution path then
        refuses to guess - a cancelled interaction that had been dispatched
        still settles as an unknown outcome, never as "it did not happen".
        """
        connection = self._connection
        if connection is None:
            return False
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - an exception is no different from False
            return False
        return True

    def _hold(self, connection: Any) -> None:
        """Remember the live protocol connection while one operation runs."""
        self._connection = connection

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _verification(passed: bool, *evidence: str) -> VerificationRecord:
        return VerificationRecord(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            method="browser_operation_integrity",
            evidence=tuple(evidence),
        )

    def _usage(
        self, task: Task, *, session_id: str | None = None, call_count: int = 0
    ) -> UsageRecord:
        return UsageRecord(
            provider="browser",
            model=None,
            connector=self.manifest.name,
            session_id=session_id,
            task_id=task.id,
            call_count=call_count,
            billing_class=BROWSER_BILLING_CLASS,
            monetary_state=BROWSER_MONETARY_STATE,
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


__all__ = ["BrowserCdpConnector"]
