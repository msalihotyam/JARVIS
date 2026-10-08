from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from jarvis.app import create_app
from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    ActionScope,
    Availability,
    BillingClass,
    CostEstimate,
    DataClassification,
    ErrorCode,
    MonetaryState,
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
from jarvis.core import AgentCore
from jarvis.destinations import CLAUDE_CODE_DESTINATION
from jarvis.registry import ToolRegistry


def isolated_settings(test, **fields):
    """Code-default ``Settings`` except for a temporary ``memory_database``,
    cleaned up with ``test``. For tests that exercise the production
    composition: the code default database is the operator's live one."""
    from jarvis.settings import Settings

    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    test.addCleanup(safe_cleanup, temp)
    return Settings(memory_database=Path(temp.name) / "jarvis.db", **fields)


def safe_cleanup(temp: tempfile.TemporaryDirectory) -> None:
    """Remove a test temp directory, tolerating Windows delete-pending files.

    SQLite leaves ``-wal``/``-shm`` sidecars that Windows can still hold as
    delete-pending when the directory is removed. ``ignore_cleanup_errors``
    does not cover this: CPython's cleanup handler calls ``_resetperms``
    (``os.chmod``) on the offending path *before* honouring the flag, and
    ``chmod`` on a delete-pending file raises ``WinError 5``. That escapes as a
    test error even though nothing is wrong with the test. Test temp
    directories live under the OS temp dir and are reclaimed there, so
    swallowing the failure is safe.
    """
    try:
        temp.cleanup()
    except OSError:
        pass


#: The capability every ``DummyConnector`` serves. Named here so a fixture can
#: write its authorization row without reading it back off the manifest.
DUMMY_CAPABILITY = "echo"


def open_core(
    registry: ToolRegistry,
    memory,
    settings=None,
    verifier=None,
    *,
    destination_policy,
) -> AgentCore:
    """``AgentCore`` for a fixture build, under an EXPLICIT tuple policy.

    ``destination_policy`` is REQUIRED and is never derived. Nothing here
    reads the registry, a manifest, a capability list, ``can_handle`` or a
    task's requested destination to manufacture a row: a fixture that
    registers a connector without writing that connector's exact
    ``DestinationGrant`` gets a build that refuses it, exactly as production
    would. Omitting the argument is a ``TypeError``, not a permissive
    default, so a new fixture cannot become accidentally authorized.

    Pass ``CONNECTOR_DESTINATION_POLICY`` only when the test really is
    composing the shipped production connectors.
    """
    return AgentCore(
        registry,
        memory,
        settings,
        verifier,
        destination_policy=frozenset(destination_policy),
    )


def open_app(
    *,
    destination_policy,
    settings=None,
    memory=None,
    registry: ToolRegistry | None = None,
    connectors=(),
    verifier=None,
):
    """``create_app`` for a fixture build, under an EXPLICIT tuple policy.

    Same contract as ``open_core``: the policy is declared by the test at the
    call site and is never reconstructed from what happens to be registered.
    """
    return create_app(
        settings=settings,
        memory=memory,
        registry=registry,
        connectors=connectors,
        verifier=verifier,
        destination_policy=frozenset(destination_policy),
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
        billing_class: BillingClass | None = None,
        verification_passes: bool = True,
        outcome: OutcomeStatus = OutcomeStatus.SUCCESS,
    ) -> None:
        # A connector that projects or reports real USD is metered by
        # definition; anything else defaults to a free local route.
        self._billing_class = billing_class or (
            BillingClass.METERED_MONETARY
            if is_paid or estimate_max > 0 or actual_cost > 0
            else BillingClass.NO_ADDITIONAL_CHARGE
        )
        self._requires_paid_api = is_paid
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
            default_billing_class=self._billing_class,
            requires_paid_api=is_paid,
        )
        self._suitability = suitability
        self._available = available
        self._estimate_max = estimate_max
        self.actual_cost = actual_cost
        self.verification_passes = verification_passes
        self.outcome = outcome
        self.execute_calls = 0
        self.cancel_calls = 0

    def canonical_outbound(self, task: Task) -> bytes:
        # A valid egress connector must implement this; a deterministic view
        # of the task's outbound-relevant state stands in for real bytes so a
        # DummyConnector declaring EXTERNAL_EGRESS is a proper attester.
        return json.dumps(
            {
                "objective": task.objective,
                "scope": task.scope.to_dict(),
                "assumption": task.visible_assumption,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")

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
            billing_class=self._billing_class,
            billing_evidence="dummy_connector_fixed_route",
            requires_paid_api=self._requires_paid_api,
        )

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        self.execute_calls += 1
        metered = self._billing_class is BillingClass.METERED_MONETARY
        usage = UsageRecord(
            provider="test-provider",
            model="test-model",
            connector=self.manifest.name,
            billing_class=self._billing_class,
            monetary_state=(
                MonetaryState.MEASURED if metered else MonetaryState.NOT_APPLICABLE
            ),
            monetary_cost_usd=self.actual_cost if metered else None,
            reference_cost_usd=None if metered else (self.actual_cost or None),
            call_count=1,
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
    destination: str | None = None,
) -> Task:
    return Task(
        objective="echo this",
        capability="echo",
        scope=ActionScope(
            action="read",
            target=str(target),
            # An egress task must name a destination Core actually owns; the
            # policy registry is the authority and no fake string passes it.
            # ``destination`` overrides for a task whose own effect is not
            # egress but whose connector's manifest makes it one.
            destination=(
                destination
                if destination is not None
                else (
                    CLAUDE_CODE_DESTINATION
                    if effect is SideEffect.EXTERNAL_EGRESS
                    else ""
                )
            ),
            data_classification=classification,
        ),
        risk_level=risk,
        side_effect=SideEffectProfile(
            effect=effect,
            reversible=reversible,
            always_requires_approval=always_requires_approval,
        ),
    )


def installed_cli(case, stem: str = "claude"):
    """A stand-in CLI that satisfies trusted-executable provenance (DB-001).

    A real file in its own temporary directory OUTSIDE the case's workspace,
    resolved through the production resolver so the returned value carries a
    genuine identity and survives the re-validation every spawn performs. The
    fixture cannot live under the connector's allowed roots any more: that is
    precisely the location the fix refuses.
    """
    from jarvis.trusted_exec import resolve_trusted_executable

    folder = Path(tempfile.mkdtemp(prefix="jarvis-installed-cli-"))
    case.addCleanup(shutil.rmtree, folder, True)
    binary = folder / (f"{stem}.exe" if os.name == "nt" else stem)
    binary.write_bytes(b"stand-in for the installed CLI")
    if os.name != "nt":
        binary.chmod(0o755)
    resolved = resolve_trusted_executable(stem, configured=str(binary))
    assert resolved is not None, "the stand-in CLI did not resolve"
    return resolved


def wait_until(condition, ceiling_ms: int, poll_ms: int = 10) -> bool:
    """Turn the Qt event loop (``tests.test_ui.pump``) until ``condition()`` holds; at most
    ``ceiling_ms``. S0b: it replaces a fixed wait, whose length it keeps as the ceiling, so a
    run is never shorter than the condition and never longer than before. Returns
    ``condition()`` - the caller's own assertion follows. Not for absence checks ("nothing
    happened"): those keep their fixed waits."""
    import time

    from tests.test_ui import pump

    deadline = time.monotonic() + ceiling_ms / 1000
    while True:
        pump(0)
        if condition():
            return True
        if time.monotonic() >= deadline:
            return bool(condition())
        pump(poll_ms)


#: THIZ: a condition wait's ceiling is at least this long (the F-ee2 rule): it ends as soon as the
#: state is there, so only a test that is failing anyway waits it out.
CHECK_FLOOR_MS = 15000


def settled(fixed_ms: int, check, poll_ms: int = 20) -> None:
    """THIZ: in place of ``pump(fixed_ms)`` before the assertions ``check`` makes (a callable that
    raises AssertionError until they hold). A ``check`` that already holds before the loop turns
    awaits nothing but time - a negative check ("nothing happens for N ms") or a state that was
    already there - so it keeps the full fixed wait and is then made again. Otherwise the loop
    turns until it holds, at most max(fixed_ms, CHECK_FLOOR_MS); out of time, its own assertion
    fails (loud). The assertions are the same either way; only the wait before them changes.
    Not for a check whose time IS the subject (a timer's tick, a note that leaves within N ms,
    exactly one sweep in N ms): those keep ``pump(fixed_ms)``. A negative part of a check that
    first fails is made at the moment the whole holds, not N ms later."""
    import time

    from tests.test_ui import pump

    try:
        check()
    except AssertionError:
        pass
    else:
        pump(fixed_ms)
        check()
        return
    deadline = time.monotonic() + max(fixed_ms, CHECK_FLOOR_MS) / 1000
    while True:
        pump(poll_ms)
        try:
            check()
            return
        except AssertionError:
            if time.monotonic() >= deadline:
                raise


def wait_still(sample, ceiling_ms: int, poll_ms: int = 50, quiet: int = 3) -> bool:
    """``wait_until`` for motion: until ``sample()`` (e.g. some items' scene rectangles) is the
    same ``quiet`` times in a row, ``poll_ms`` apart - an animated reflow has ended; at most
    ``ceiling_ms``."""
    seen: list = []

    def still() -> bool:
        seen.append(sample())
        return len(seen) >= quiet and all(s == seen[-1] for s in seen[-quiet:])

    return wait_until(still, ceiling_ms, poll_ms)
