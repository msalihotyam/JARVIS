"""H3 - billing/ledger integrity.

Every test uses fake connectors, a temporary SQLite store and controlled
threading events. No test starts a live connector, auth probe, model
subprocess, MCP or network call; the Claude-facing cases additionally patch
``subprocess``/``shutil.which`` to fail if they are touched at all.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import threading
import unittest
from enum import Enum
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from jarvis.app import EXIT_POLICY, CommandResult, build_envelope
from jarvis.connectors import claude_code
from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    MAX_NANO_USD,
    MAX_SQLITE_INTEGER,
    MONTHLY_HARD_LIMIT_NANO_USD,
    AttemptState,
    BillingClaim,
    BillingClass,
    BillingIntegrityError,
    BillingReconciliation,
    CostEstimate,
    ErrorCode,
    ExecutionAttempt,
    ExecutionClaimOutcome,
    LoopStage,
    MonetaryState,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    ReconciliationOutcome,
    ReservationState,
    RunStatus,
    SessionState,
    SideEffect,
    Task,
    TaskStatus,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
    billing_class_from_raw,
    canonical_usd,
    format_usd,
    ledger_month,
    monetary_state_from_raw,
    nano_usd,
    persistable_count,
    require_billing_class,
    require_monetary_state,
    reservation_state_from_raw,
    usd_from_nano,
    validate_cost_estimate,
    validated_measured_cost,
    validated_metered_maximum,
    validated_resource_ceiling,
)
from jarvis.core import AgentCore
from jarvis.memory import (
    SCHEMA_VERSION,
    SQLiteMemoryStore,
    ledger_timestamp,
    validated_ledger_month,
)
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task, safe_cleanup


def _past_month() -> str:
    stamp = datetime.now(timezone.utc).replace(day=1) - timedelta(days=45)
    return stamp.isoformat()


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class BarrierConnector(DummyConnector):
    """Metered external-egress fake that can park inside ``execute``."""

    def __init__(
        self,
        *,
        name: str = "barrier",
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(name=name, side_effect=SideEffect.EXTERNAL_EGRESS, **kwargs)
        self._started = started
        self._release = release

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        if self._started is not None:
            self._started.set()
        if self._release is not None:
            self._release.wait(timeout=60)
        return super().execute(task, session)

    def cancel(self, task_id: str) -> bool:
        # A parked fake execution cannot be force-stopped; the executing owner
        # finalizes the cancellation on return (H2-F).
        self.cancel_calls += 1
        return False


class ReportingConnector(BaseConnector):
    """Metered fake whose reported usage is fully controlled by the test."""

    def __init__(
        self,
        *,
        maximum_usd: float = 0.10,
        route: BillingClass = BillingClass.METERED_MONETARY,
        reported_class: BillingClass | None = None,
        monetary_state: MonetaryState = MonetaryState.MEASURED,
        monetary_cost_usd: float | None = 0.05,
        reference_cost_usd: float | None = None,
        usage_id: str | None = None,
        recorded_at: str | None = None,
        resource_ceiling_usd: float | None = None,
        name: str = "reporting",
        reported_connector: str | None = None,
        reported_evidence: str | None = None,
        reported_call_count: int = 1,
        input_tokens: object = 0,
        output_tokens: object = 0,
    ) -> None:
        self.manifest = ConnectorManifest(
            name=name, capabilities=("echo",), priority=10, default_billing_class=route
        )
        self.route = route
        self.maximum_usd = maximum_usd
        self.reported_class = reported_class or route
        self.monetary_state = monetary_state
        self.monetary_cost_usd = monetary_cost_usd
        self.reference_cost_usd = reference_cost_usd
        self.usage_id = usage_id
        self.recorded_at = recorded_at
        self.resource_ceiling_usd = resource_ceiling_usd
        self.reported_connector = reported_connector
        self.reported_evidence = reported_evidence
        self.reported_call_count = reported_call_count
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.execute_calls = 0

    def estimate_cost(self, task: Task) -> CostEstimate:
        return CostEstimate(
            provider="test-provider",
            model="test-model",
            minimum_usd=0.0,
            maximum_usd=(
                self.maximum_usd
                if self.route is BillingClass.METERED_MONETARY
                else 0.0
            ),
            confidence="test",
            billing_class=self.route,
            billing_evidence="reporting_connector_fixed_route",
            resource_ceiling_usd=self.resource_ceiling_usd,
        )

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        self.execute_calls += 1
        usage = UsageRecord(
            provider="test-provider",
            model="test-model",
            # Deliberately connector-controlled: none of these may become
            # authoritative ledger data.
            connector=self.reported_connector or self.manifest.name,
            billing_class=self.reported_class,
            monetary_state=self.monetary_state,
            monetary_cost_usd=self.monetary_cost_usd,
            reference_cost_usd=self.reference_cost_usd,
            billing_evidence=self.reported_evidence or "",
            call_count=self.reported_call_count,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )
        if self.usage_id is not None:
            usage.id = self.usage_id
        if self.recorded_at is not None:
            usage.recorded_at = self.recorded_at
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=task.objective,
            data={"next_step": "continue testing"},
            usage=usage,
        )

    def verify(self, task, result, session) -> VerificationRecord:
        return VerificationRecord(
            status=VerificationStatus.PASSED,
            method="reporting_independent_check",
            evidence=("deterministic-test-evidence",),
        )


class BrokenEstimateConnector(DummyConnector):
    """A metered connector whose pre-execution estimate is malformed."""

    def __init__(
        self,
        *,
        maximum_usd: object = 0.10,
        minimum_usd: object = 0.0,
        resource_ceiling_usd: object = None,
        name: str = "broken-estimate",
    ) -> None:
        super().__init__(name=name, billing_class=BillingClass.METERED_MONETARY)
        self._maximum = maximum_usd
        self._minimum = minimum_usd
        self._ceiling = resource_ceiling_usd

    def estimate_cost(self, task: Task) -> CostEstimate:
        return CostEstimate(
            provider="test-provider",
            minimum_usd=self._minimum,
            maximum_usd=self._maximum,
            confidence="test",
            billing_class=BillingClass.METERED_MONETARY,
            billing_evidence="broken_estimate",
            resource_ceiling_usd=self._ceiling,
        )


class PausedClaimStore(SQLiteMemoryStore):
    """Parks one owner between reading the ledger and claiming the attempt."""

    def __init__(self, path, at_claim: threading.Event, release: threading.Event):
        super().__init__(path)
        self._at_claim = at_claim
        self._release = release

    def open_execution_attempt(self, attempt: ExecutionAttempt) -> ExecutionAttempt:
        opened = super().open_execution_attempt(attempt)
        self._at_claim.set()
        self._release.wait(timeout=60)
        return opened


# --------------------------------------------------------------------------
# Shared setup
# --------------------------------------------------------------------------


class BillingIntegrityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)

    def store(self) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(self.database)

    def settings(self, **overrides) -> Settings:
        return Settings(
            memory_database=self.database,
            permission_mode=PermissionMode.SESSION,
            **overrides,
        )

    def core(self, connector, memory=None, **overrides) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        return AgentCore(
            registry, memory or self.memory, self.settings(**overrides)
        )

    def seed_spend(self, amount: float, memory=None) -> None:
        (memory or self.memory).append_usage(
            UsageRecord(
                billing_class=BillingClass.METERED_MONETARY,
                monetary_state=MonetaryState.MEASURED,
                monetary_cost_usd=amount,
            )
        )

    def reservations(self, task_id: str) -> list[dict]:
        connection = sqlite3.connect(self.database)
        try:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM attempt_billing WHERE task_id = ?", (task_id,)
                )
            ]
        finally:
            connection.close()

    def consumed_grants(self, task_id: str) -> list[str]:
        connection = sqlite3.connect(self.database)
        try:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT consumed_by_attempt FROM permission_grants "
                    "WHERE task_id = ? AND consumed_by_attempt IS NOT NULL",
                    (task_id,),
                )
            ]
        finally:
            connection.close()

    def events(self, session_id: str) -> list[str]:
        return [item["event_type"] for item in self.memory.list_events(session_id)]


# --------------------------------------------------------------------------
# 1-3: canonical monetary validation
# --------------------------------------------------------------------------


class CanonicalMonetaryValidationTests(unittest.TestCase):
    REJECTED = (
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.01,
        -1,
        True,
        False,
        None,
        "abc",
        object(),
        [0.1],
    )

    def test_canonical_usd_rejects_every_unusable_value(self) -> None:
        for value in self.REJECTED:
            with self.subTest(value=repr(value)):
                with self.assertRaises(BillingIntegrityError):
                    canonical_usd(value)

    def test_canonical_usd_is_exact_and_nine_decimal_places(self) -> None:
        self.assertEqual(Decimal("0.100000000"), canonical_usd(0.1))
        self.assertEqual(Decimal("19.900000000"), canonical_usd("19.90"))
        self.assertEqual("0.100000000", format_usd(0.1))
        # Exact, not binary float: 19.90 + 0.20 is exactly 20.10 in nano-USD.
        self.assertEqual(20_100_000_000, nano_usd("19.90") + nano_usd("0.20"))
        self.assertEqual(Decimal("20.100000000"), usd_from_nano(20_100_000_000))

    def test_amount_outside_the_persistable_range_is_refused(self) -> None:
        for value in ("1e30", "1e999999999", str(MAX_NANO_USD)):
            with self.subTest(value=value):
                with self.assertRaises(BillingIntegrityError):
                    canonical_usd(value)

    def test_metered_maximum_must_be_finite_and_positive(self) -> None:
        for value in (float("nan"), float("inf"), -0.10, 0.0, 0):
            with self.subTest(value=value):
                with self.assertRaises(BillingIntegrityError):
                    validated_metered_maximum(value)
        self.assertEqual(Decimal("0.100000000"), validated_metered_maximum(0.10))

    def test_minimum_may_not_exceed_the_maximum(self) -> None:
        maximum = validated_metered_maximum(0.10)
        with self.assertRaises(BillingIntegrityError):
            validate_cost_estimate(
                CostEstimate(
                    minimum_usd=0.20,
                    maximum_usd=0.10,
                    billing_class=BillingClass.METERED_MONETARY,
                )
            )
        with self.assertRaises(BillingIntegrityError):
            validate_cost_estimate(
                CostEstimate(
                    minimum_usd=-0.01,
                    maximum_usd=0.10,
                    billing_class=BillingClass.METERED_MONETARY,
                )
            )
        self.assertEqual(Decimal("0.010000000"), canonical_usd(0.01))
        self.assertLessEqual(canonical_usd(0.01), maximum)

    def test_resource_ceiling_must_be_finite_and_positive(self) -> None:
        for value in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(BillingIntegrityError):
                    validated_resource_ceiling(value)
        self.assertEqual(Decimal("0.250000000"), validated_resource_ceiling(0.25))

    def test_measured_actual_may_be_zero_but_never_invalid(self) -> None:
        self.assertEqual(Decimal("0E-9"), validated_measured_cost(0.0))
        for value in (-0.01, float("nan"), float("inf"), None, "x"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(BillingIntegrityError):
                    validated_measured_cost(value)

    def test_malformed_raw_classification_values_fail_closed(self) -> None:
        for decoder in (
            billing_class_from_raw,
            monetary_state_from_raw,
            reservation_state_from_raw,
        ):
            for value in (None, 3, "not_a_real_value", ""):
                with self.subTest(decoder=decoder.__name__, value=repr(value)):
                    with self.assertRaises(BillingIntegrityError):
                        decoder(value)
        self.assertIs(
            BillingClass.METERED_MONETARY, billing_class_from_raw("metered_monetary")
        )
        self.assertIs(MonetaryState.MEASURED, monetary_state_from_raw("measured"))
        self.assertIs(ReservationState.ACTIVE, reservation_state_from_raw("active"))

    def test_billing_integrity_error_code_is_the_only_new_public_code(self) -> None:
        self.assertEqual(
            "billing_integrity_failed", ErrorCode.BILLING_INTEGRITY_FAILED.value
        )
        self.assertIs(
            ErrorCode.BILLING_INTEGRITY_FAILED, BillingIntegrityError.code
        )


class SettingsValidationTests(unittest.TestCase):
    def test_non_finite_settings_are_rejected(self) -> None:
        for field in (
            "monthly_target_usd",
            "monthly_hard_limit_usd",
            "claude_call_budget_usd",
        ):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        Settings(**{field: value})

    def test_hard_limit_is_still_fixed_at_twenty_usd(self) -> None:
        self.assertEqual(20.0, Settings().monthly_hard_limit_usd)
        with self.assertRaises(ValueError):
            Settings(monthly_hard_limit_usd=25.0)
        with self.assertRaises(ValueError):
            Settings(monthly_target_usd=-1.0)


class EstimateValidationBeforeExecutionTests(BillingIntegrityTestCase):
    def test_invalid_metered_maxima_fail_before_execution(self) -> None:
        for value in (float("nan"), float("inf"), -0.10, 0.0):
            with self.subTest(maximum=value):
                connector = BrokenEstimateConnector(maximum_usd=value)
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(
                    ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code
                )
                self.assertEqual(0, connector.execute_calls)

    def test_inverted_bounds_and_invalid_ceilings_fail_closed(self) -> None:
        cases = (
            {"minimum_usd": 0.50, "maximum_usd": 0.10},
            {"minimum_usd": -0.01, "maximum_usd": 0.10},
            {"maximum_usd": 0.10, "resource_ceiling_usd": 0.0},
            {"maximum_usd": 0.10, "resource_ceiling_usd": -1.0},
            {"maximum_usd": 0.10, "resource_ceiling_usd": float("nan")},
        )
        for case in cases:
            with self.subTest(**case):
                connector = BrokenEstimateConnector(**case)
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(
                    ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code
                )
                self.assertEqual(0, connector.execute_calls)


# --------------------------------------------------------------------------
# 4-8: concurrent cap enforcement, reservations and exact overrides
# --------------------------------------------------------------------------


class ConcurrentCapEnforcementTests(BillingIntegrityTestCase):
    """Two coordinated 0.10 USD executions over 19.90 USD of measured spend."""

    def _approve(self, core, run):
        return core.resume(
            run.session.id,
            run.task.id,
            PermissionGrant.for_request(run.permission_request),
        )

    def test_two_concurrent_metered_executions_cannot_both_execute(self) -> None:
        self.seed_spend(19.90)
        started = threading.Event()
        release = threading.Event()
        first = BarrierConnector(
            name="barrier",
            started=started,
            release=release,
            estimate_max=0.10,
            actual_cost=0.05,
        )
        second = BarrierConnector(name="barrier", estimate_max=0.10, actual_cost=0.05)

        owner_memory = self.store()
        owner = self.core(first, owner_memory)
        session = owner.start_session("concurrent cap")
        waiting_a = owner.run(make_task(effect=SideEffect.EXTERNAL_EGRESS), session.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting_a.status)

        outcome: dict[str, object] = {}

        def run_owner() -> None:
            outcome["run"] = self._approve(owner, waiting_a)

        thread = threading.Thread(target=run_owner)
        thread.start()
        try:
            # Barrier, not a sleep: the first owner is inside the connector
            # boundary with its reservation already durable.
            self.assertTrue(started.wait(timeout=60))
            attempt_a = self.memory.latest_execution_attempt(waiting_a.task.id)
            self.assertIs(
                AttemptState.IN_FLIGHT,
                self.memory.latest_execution_attempt(waiting_a.task.id).state,
            )
            reserved = self.reservations(waiting_a.task.id)
            self.assertEqual(1, len(reserved))
            self.assertEqual(100_000_000, reserved[0]["reserved_nano_usd"])
            self.assertEqual(ReservationState.ACTIVE.value, reserved[0]["state"])
            snapshot = self.store().ledger_snapshot()
            self.assertEqual(Decimal("20.000000000"), snapshot.total_usd)
            self.assertTrue(snapshot.has_unresolved_liability)

            # The contender observes the reserved exposure and stops at the
            # monetary gate; it never reaches the connector boundary.
            contender = self.core(second, self.store())
            waiting_b = contender.run(
                make_task(effect=SideEffect.EXTERNAL_EGRESS), session.id
            )
            self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting_b.status)
            gated = self._approve(contender, waiting_b)
            self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, gated.status)
            self.assertEqual(
                "override_monthly_cost_cap", gated.permission_request.scope.action
            )
            self.assertEqual(0, second.execute_calls)
            # 5: the loser consumed no grant and created no reservation.
            self.assertEqual([], self.consumed_grants(gated.task.id))
            self.assertEqual([], self.reservations(gated.task.id))
            # 6: it is back at cost preflight with an exact fresh request.
            self.assertIs(LoopStage.COST_PREFLIGHT, gated.task.stage)
            self.assertIn(
                f"override_monthly_cost_cap|{format_usd(Decimal('20.10'))}",
                gated.permission_request.attestation.cost_gate,
            )
            stale_grant = PermissionGrant.for_request(gated.permission_request)
        finally:
            release.set()
            thread.join(timeout=60)

        self.assertEqual(RunStatus.COMPLETED, outcome["run"].status)
        self.assertEqual(1, first.execute_calls)
        self.assertEqual(
            ReservationState.RECONCILED.value,
            self.reservations(waiting_a.task.id)[0]["state"],
        )
        self.assertEqual(attempt_a.id, self.consumed_grants(waiting_a.task.id)[0])
        # Measured spend replaced the reservation exactly once.
        settled = self.store().ledger_snapshot()
        self.assertEqual(Decimal("19.950000000"), settled.measured_usd)
        self.assertEqual(Decimal("0E-9"), settled.reserved_usd)

        # 7: the override approved for the stale 20.10 projection cannot
        # authorize the changed 20.05 projection.
        contender = self.core(second, self.store())
        refused = contender.resume(session.id, gated.task.id, stale_grant)
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, refused.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, refused.error.code)
        self.assertEqual(0, second.execute_calls)
        self.assertIn(
            f"override_monthly_cost_cap|{format_usd(Decimal('20.05'))}",
            refused.permission_request.attestation.cost_gate,
        )

        # 8: the exact fresh override claims and executes once.
        done = contender.resume(
            session.id,
            gated.task.id,
            PermissionGrant.for_request(refused.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, done.status)
        self.assertEqual(1, second.execute_calls)
        self.assertEqual(Decimal("20.000000000"), self.store().ledger_snapshot().measured_usd)

    def test_claim_time_recheck_writes_nothing_and_returns_to_preflight(self) -> None:
        # The contender reads the ledger, another execution settles, and only
        # then does the contender try to claim: the claim itself refuses.
        self.seed_spend(19.85)
        at_claim = threading.Event()
        release = threading.Event()
        contender_connector = BarrierConnector(
            name="barrier", estimate_max=0.10, actual_cost=0.05
        )
        paused = PausedClaimStore(self.database, at_claim, release)
        contender = self.core(contender_connector, paused)
        session = contender.start_session("claim recheck")
        waiting = contender.run(
            make_task(effect=SideEffect.EXTERNAL_EGRESS), session.id
        )
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
        grant = PermissionGrant.for_request(waiting.permission_request)

        outcome: dict[str, object] = {}

        def run_contender() -> None:
            outcome["run"] = contender.resume(session.id, waiting.task.id, grant)

        thread = threading.Thread(target=run_contender)
        thread.start()
        try:
            self.assertTrue(at_claim.wait(timeout=60))
            # The ledger moves after the contender read it: its approved
            # projection (19.95) is no longer the real one (20.05).
            self.seed_spend(0.10, self.store())
        finally:
            release.set()
            thread.join(timeout=60)

        run = outcome["run"]
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, run.status)
        self.assertIs(LoopStage.COST_PREFLIGHT, run.task.stage)
        self.assertEqual(0, contender_connector.execute_calls)
        self.assertEqual([], self.consumed_grants(waiting.task.id))
        self.assertEqual([], self.reservations(waiting.task.id))
        self.assertIn("billing_recheck_required", self.events(session.id))

    def test_claim_refuses_a_stale_lower_projection_override(self) -> None:
        # A cost-override grant issued against a lower projection can never
        # satisfy the claim's re-derived higher projection.
        self.seed_spend(19.90)
        connector = BarrierConnector(
            name="barrier", estimate_max=0.20, actual_cost=0.05
        )
        core = self.core(connector, self.store())
        session = core.start_session("stale override")
        waiting = core.run(make_task(effect=SideEffect.EXTERNAL_EGRESS), session.id)
        gated = core.resume(
            session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request),
        )
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, gated.status)
        stale = PermissionGrant.for_request(gated.permission_request)
        # The projection rises before the override is applied.
        self.seed_spend(0.05, self.store())
        refused = core.resume(session.id, gated.task.id, stale)
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, refused.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, refused.error.code)
        self.assertEqual(0, connector.execute_calls)
        self.assertEqual([], self.reservations(gated.task.id))

    def test_storage_claim_refuses_an_exposure_that_moved(self) -> None:
        memory = self.store()
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        core = self.core(connector, memory)
        session = core.start_session("storage claim")
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        memory.save_task(session.id, task)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        stale_claim = BillingClaim(
            ledger_month=ledger_month(),
            expected_exposure_nano_usd=nano_usd("5.00"),
            reservation_nano_usd=nano_usd("0.10"),
            limit_nano_usd=nano_usd("20.00"),
            billing_class=BillingClass.METERED_MONETARY,
            gate_state=task.estimate.gate_state,
        )
        self.assertIs(
            ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED,
            memory.begin_execution(attempt.id, session.id, task.id, [], stale_claim),
        )
        self.assertIs(
            AttemptState.PREPARED, memory.latest_execution_attempt(task.id).state
        )
        self.assertEqual([], self.reservations(task.id))

        over_limit = BillingClaim(
            ledger_month=ledger_month(),
            expected_exposure_nano_usd=0,
            reservation_nano_usd=nano_usd("25.00"),
            limit_nano_usd=nano_usd("20.00"),
            billing_class=BillingClass.METERED_MONETARY,
            gate_state=task.estimate.gate_state,
        )
        self.assertIs(
            ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED,
            memory.begin_execution(attempt.id, session.id, task.id, [], over_limit),
        )
        self.assertEqual([], self.reservations(task.id))

    def test_a_malformed_persisted_row_invalidates_the_ledger(self) -> None:
        self.seed_spend(1.00)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE usage_ledger SET monetary_cost_usd = -5.0"
            )
            connection.commit()
        finally:
            connection.close()
        snapshot = self.store().ledger_snapshot()
        self.assertFalse(snapshot.valid)
        self.assertEqual("malformed_usage_row", snapshot.reason)
        # It never subtracts from spend, and it never silently allows paid work.
        with self.assertRaises(BillingIntegrityError):
            self.store().monthly_monetary_cost()
        connector = DummyConnector(name="metered", estimate_max=0.10)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code)
        self.assertEqual(0, connector.execute_calls)


# --------------------------------------------------------------------------
# 9: crash / restart exposure
# --------------------------------------------------------------------------


class RestartExposureTests(BillingIntegrityTestCase):
    def _claimed_task(self, memory) -> tuple[SessionState, Task, str]:
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        core = self.core(connector, memory)
        session = core.start_session("restart exposure")
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        memory.save_task(session.id, task)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        claim = BillingClaim(
            ledger_month=ledger_month(),
            expected_exposure_nano_usd=memory.ledger_snapshot().total_nano_usd,
            reservation_nano_usd=nano_usd("0.10"),
            limit_nano_usd=nano_usd("20.00"),
            billing_class=BillingClass.METERED_MONETARY,
            gate_state=task.estimate.gate_state,
        )
        self.assertIs(
            ExecutionClaimOutcome.CLAIMED,
            memory.begin_execution(attempt.id, session.id, task.id, [], claim),
        )
        return session, task, attempt.id

    def test_in_flight_restart_retains_exposure_and_never_replays(self) -> None:
        memory = self.store()
        session, task, _ = self._claimed_task(memory)
        fresh_connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        run = self.core(fresh_connector, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, run.error.code)
        self.assertEqual(0, fresh_connector.execute_calls)
        snapshot = self.store().ledger_snapshot()
        self.assertEqual(Decimal("0.100000000"), snapshot.reserved_usd)
        self.assertTrue(snapshot.has_unresolved_liability)
        self.assertEqual(
            ReservationState.ACTIVE.value, self.reservations(task.id)[0]["state"]
        )

    def test_unknown_outcome_restart_retains_exposure_and_never_replays(self) -> None:
        memory = self.store()
        session, task, attempt_id = self._claimed_task(memory)
        memory.mark_execution_unknown(attempt_id)
        self.assertIs(
            AttemptState.UNKNOWN_OUTCOME,
            memory.latest_execution_attempt(task.id).state,
        )
        fresh_connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        run = self.core(fresh_connector, self.store()).resume(session.id, task.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, run.error.code)
        self.assertEqual(0, fresh_connector.execute_calls)
        self.assertEqual(
            Decimal("0.100000000"), self.store().ledger_snapshot().reserved_usd
        )
        # No automatic release: the reservation is still an ACTIVE liability.
        self.assertEqual(
            ReservationState.ACTIVE.value, self.reservations(task.id)[0]["state"]
        )

    def test_retained_exposure_blocks_a_later_metered_projection(self) -> None:
        self.seed_spend(19.85)
        memory = self.store()
        self._claimed_task(memory)
        later = DummyConnector(name="metered", estimate_max=0.10)
        run = self.core(later, self.store()).run(make_task())
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, run.status)
        self.assertEqual(0, later.execute_calls)


# --------------------------------------------------------------------------
# 10-14: authoritative reconciliation
# --------------------------------------------------------------------------


class ReconciliationTests(BillingIntegrityTestCase):
    def test_valid_metered_usage_reconciles_exactly_once(self) -> None:
        connector = ReportingConnector(maximum_usd=0.20, monetary_cost_usd=0.05)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        records = self.store().usage_records()
        self.assertEqual(1, len(records))
        self.assertAlmostEqual(0.05, records[0].monetary_cost_usd)
        self.assertEqual(run.task.id, records[0].task_id)
        snapshot = self.store().ledger_snapshot()
        self.assertEqual(Decimal("0.050000000"), snapshot.measured_usd)
        self.assertEqual(Decimal("0E-9"), snapshot.reserved_usd)
        self.assertFalse(snapshot.has_unresolved_liability)
        reservation = self.reservations(run.task.id)[0]
        self.assertEqual(ReservationState.RECONCILED.value, reservation["state"])
        self.assertEqual(50_000_000, reservation["actual_nano_usd"])
        # The persisted row carries the storage-owned identity, not the
        # connector's, and the result envelope reports the same row.
        self.assertEqual(reservation["usage_id"], records[0].id)
        self.assertEqual(reservation["usage_id"], run.result.usage.id)

    def test_unknown_metered_outcome_leaves_unresolved_exposure(self) -> None:
        connector = ReportingConnector(
            maximum_usd=0.20,
            monetary_state=MonetaryState.UNKNOWN,
            monetary_cost_usd=None,
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code)
        self.assertEqual(1, connector.execute_calls)
        self.assertEqual([], self.store().usage_records())
        snapshot = self.store().ledger_snapshot()
        self.assertEqual(Decimal("0E-9"), snapshot.measured_usd)
        self.assertEqual(Decimal("0.200000000"), snapshot.reserved_usd)
        self.assertTrue(snapshot.has_unresolved_liability)
        self.assertEqual(
            ReservationState.UNRESOLVED.value,
            self.reservations(run.task.id)[0]["state"],
        )
        events = self.events(run.session.id)
        self.assertIn("task_failed", events)
        self.assertNotIn("task_completed", events)
        self.assertNotIn("verification_passed", events)
        self.assertEqual([], self.store().list_decisions(run.session.id))

    def test_invalid_actual_costs_never_reach_the_measured_ledger(self) -> None:
        for value in (-1.0, float("nan"), float("inf")):
            with self.subTest(actual=value):
                store = self.store()
                connector = ReportingConnector(
                    maximum_usd=0.20, monetary_cost_usd=value
                )
                run = self.core(connector, store).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(
                    ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code
                )
                snapshot = self.store().ledger_snapshot()
                # Never negative, never reduced, never silently ignored.
                self.assertEqual(Decimal("0E-9"), snapshot.measured_usd)
                self.assertGreater(snapshot.reserved_usd, Decimal(0))
                self.assertEqual(
                    ReservationState.UNRESOLVED.value,
                    self.reservations(run.task.id)[0]["state"],
                )

    def test_actual_above_the_approved_maximum_is_recorded_then_fails(self) -> None:
        connector = ReportingConnector(maximum_usd=0.01, monetary_cost_usd=0.50)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code)
        records = self.store().usage_records()
        self.assertEqual(1, len(records))
        # Recorded in full - never clamped to the approved maximum.
        self.assertAlmostEqual(0.50, records[0].monetary_cost_usd)
        self.assertEqual(
            Decimal("0.500000000"), self.store().ledger_snapshot().measured_usd
        )
        self.assertEqual(
            ReservationState.RECONCILED.value,
            self.reservations(run.task.id)[0]["state"],
        )
        events = self.events(run.session.id)
        self.assertNotIn("task_completed", events)
        self.assertEqual([], self.store().list_decisions(run.session.id))

        # A later metered task observes the resulting exposure.
        later = DummyConnector(name="later", estimate_max=0.10)
        self.seed_spend(19.60, self.store())
        follow_up = self.core(later, self.store()).run(make_task())
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, follow_up.status)

    def test_connector_downgrade_cannot_erase_a_metered_route(self) -> None:
        for reported, state in (
            (BillingClass.SUBSCRIPTION_INCLUDED, MonetaryState.NOT_APPLICABLE),
            (BillingClass.NO_ADDITIONAL_CHARGE, MonetaryState.NOT_APPLICABLE),
            (BillingClass.UNKNOWN, MonetaryState.UNKNOWN),
        ):
            with self.subTest(reported=reported.value):
                store = self.store()
                connector = ReportingConnector(
                    route=BillingClass.METERED_MONETARY,
                    reported_class=reported,
                    monetary_state=state,
                    monetary_cost_usd=None,
                    reference_cost_usd=0.42,
                    maximum_usd=0.20,
                )
                run = self.core(connector, store).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(
                    ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code
                )
                self.assertEqual([], self.store().usage_records())
                reservation = self.reservations(run.task.id)[0]
                # The authoritative route survived the connector's claim.
                self.assertEqual(
                    BillingClass.METERED_MONETARY.value,
                    reservation["billing_class"],
                )
                self.assertEqual(
                    ReservationState.UNRESOLVED.value, reservation["state"]
                )
                self.assertEqual(200_000_000, reservation["reserved_nano_usd"])
                snapshot = self.store().ledger_snapshot()
                self.assertTrue(snapshot.has_unresolved_liability)
                # Unresolved exposure only ever accumulates; measured spend
                # never gains a fabricated amount from the downgrade.
                self.assertEqual(Decimal("0E-9"), snapshot.measured_usd)

    def test_subscription_route_records_zero_money_with_reference_cost(self) -> None:
        connector = ReportingConnector(
            route=BillingClass.SUBSCRIPTION_INCLUDED,
            monetary_state=MonetaryState.NOT_APPLICABLE,
            monetary_cost_usd=None,
            reference_cost_usd=0.42,
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        records = self.store().usage_records()
        self.assertEqual(1, len(records))
        self.assertIsNone(records[0].monetary_cost_usd)
        self.assertIs(MonetaryState.NOT_APPLICABLE, records[0].monetary_state)
        self.assertAlmostEqual(0.42, records[0].reference_cost_usd)
        self.assertEqual(
            Decimal("0E-9"), self.store().ledger_snapshot().measured_usd
        )
        # A zero-valued reservation still binds route and ledger identity.
        reservation = self.reservations(run.task.id)[0]
        self.assertEqual(0, reservation["reserved_nano_usd"])
        self.assertEqual(ReservationState.RECONCILED.value, reservation["state"])
        self.assertEqual(
            BillingClass.SUBSCRIPTION_INCLUDED.value, reservation["billing_class"]
        )

    def test_malformed_reference_cost_is_dropped_not_recorded(self) -> None:
        for value in (-1.0, float("nan"), float("inf")):
            with self.subTest(reference=value):
                connector = ReportingConnector(
                    route=BillingClass.SUBSCRIPTION_INCLUDED,
                    monetary_state=MonetaryState.NOT_APPLICABLE,
                    monetary_cost_usd=None,
                    reference_cost_usd=value,
                )
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.COMPLETED, run.status)
                record = [
                    item
                    for item in self.store().usage_records()
                    if item.task_id == run.task.id
                ][0]
                self.assertIsNone(record.reference_cost_usd)
                self.assertIsNone(record.monetary_cost_usd)


# --------------------------------------------------------------------------
# 15-17: ledger identity, time authority and idempotency
# --------------------------------------------------------------------------


class LedgerIdentityTests(BillingIntegrityTestCase):
    def test_connector_usage_id_cannot_overwrite_a_ledger_record(self) -> None:
        existing = UsageRecord(
            id="fixed-usage-id",
            billing_class=BillingClass.METERED_MONETARY,
            monetary_state=MonetaryState.MEASURED,
            monetary_cost_usd=5.00,
        )
        self.memory.append_usage(existing)
        connector = ReportingConnector(
            maximum_usd=0.20, monetary_cost_usd=0.05, usage_id="fixed-usage-id"
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        records = {item.id: item for item in self.store().usage_records()}
        # The prior row is untouched; the new row got a storage-owned id.
        self.assertAlmostEqual(5.00, records["fixed-usage-id"].monetary_cost_usd)
        self.assertEqual(2, len(records))
        self.assertNotEqual("fixed-usage-id", run.result.usage.id)
        self.assertEqual(
            Decimal("5.050000000"), self.store().ledger_snapshot().measured_usd
        )

    def test_compatibility_insert_refuses_a_conflicting_duplicate_id(self) -> None:
        first = UsageRecord(
            id="dup",
            billing_class=BillingClass.METERED_MONETARY,
            monetary_state=MonetaryState.MEASURED,
            monetary_cost_usd=1.00,
        )
        self.memory.append_usage(first)
        # The identical record is idempotent.
        self.memory.append_usage(first)
        self.assertEqual(1, len(self.memory.usage_records()))
        conflicting = UsageRecord(
            id="dup",
            billing_class=BillingClass.METERED_MONETARY,
            monetary_state=MonetaryState.MEASURED,
            monetary_cost_usd=9.99,
        )
        with self.assertRaises(BillingIntegrityError):
            self.memory.append_usage(conflicting)
        self.assertAlmostEqual(1.00, self.memory.monthly_monetary_cost())

    def test_compatibility_insert_rejects_invalid_money(self) -> None:
        for value in (-1.0, float("nan"), float("inf")):
            with self.subTest(amount=value):
                with self.assertRaises(BillingIntegrityError):
                    self.memory.append_usage(
                        UsageRecord(
                            billing_class=BillingClass.METERED_MONETARY,
                            monetary_state=MonetaryState.MEASURED,
                            monetary_cost_usd=value,
                        )
                    )
        with self.assertRaises(BillingIntegrityError):
            self.memory.append_usage(
                UsageRecord(
                    billing_class=BillingClass.METERED_MONETARY,
                    monetary_state=MonetaryState.MEASURED,
                    monetary_cost_usd=None,
                )
            )
        self.assertEqual([], self.memory.usage_records())

    def test_connector_recorded_at_cannot_backdate_spend(self) -> None:
        connector = ReportingConnector(
            maximum_usd=0.20, monetary_cost_usd=0.05, recorded_at=_past_month()
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        record = self.store().usage_records()[0]
        self.assertEqual(ledger_month(), record.recorded_at[:7])
        self.assertEqual(
            Decimal("0.050000000"), self.store().ledger_snapshot().measured_usd
        )

    def _reconciliation(self, attempt_id, session_id, task_id, **overrides):
        payload = {
            "attempt_id": attempt_id,
            "session_id": session_id,
            "task_id": task_id,
            "connector": "metered",
            "billing_class": BillingClass.METERED_MONETARY,
            "monetary_state": MonetaryState.MEASURED,
            "actual_nano_usd": nano_usd("0.05"),
            "estimated_cost_usd": 0.10,
        }
        payload.update(overrides)
        return BillingReconciliation(**payload)

    def test_identical_replay_is_idempotent_and_conflict_fails_closed(self) -> None:
        memory = self.store()
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        core = self.core(connector, memory)
        session = core.start_session("idempotent reconcile")
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        memory.save_task(session.id, task)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        claim = BillingClaim(
            ledger_month=ledger_month(),
            expected_exposure_nano_usd=0,
            reservation_nano_usd=nano_usd("0.10"),
            limit_nano_usd=nano_usd("20.00"),
            billing_class=BillingClass.METERED_MONETARY,
            gate_state=task.estimate.gate_state,
        )
        self.assertIs(
            ExecutionClaimOutcome.CLAIMED,
            memory.begin_execution(attempt.id, session.id, task.id, [], claim),
        )

        first = memory.reconcile_usage(
            self._reconciliation(attempt.id, session.id, task.id)
        )
        self.assertIs(ReconciliationOutcome.RECONCILED, first.outcome)
        replay = memory.reconcile_usage(
            self._reconciliation(attempt.id, session.id, task.id)
        )
        self.assertIs(ReconciliationOutcome.IDEMPOTENT, replay.outcome)
        self.assertEqual(first.usage.id, replay.usage.id)
        self.assertEqual(1, len(memory.usage_records()))
        self.assertEqual(Decimal("0.050000000"), memory.ledger_snapshot().measured_usd)

        conflicting = memory.reconcile_usage(
            self._reconciliation(
                attempt.id, session.id, task.id, actual_nano_usd=nano_usd("9.99")
            )
        )
        self.assertIs(ReconciliationOutcome.CONFLICT, conflicting.outcome)
        self.assertEqual(1, len(memory.usage_records()))
        self.assertEqual(Decimal("0.050000000"), memory.ledger_snapshot().measured_usd)

        # A route the claim was not made on is refused outright.
        mismatched = memory.reconcile_usage(
            self._reconciliation(
                attempt.id,
                session.id,
                task.id,
                billing_class=BillingClass.SUBSCRIPTION_INCLUDED,
                monetary_state=MonetaryState.NOT_APPLICABLE,
                actual_nano_usd=None,
            )
        )
        self.assertIs(ReconciliationOutcome.CONFLICT, mismatched.outcome)
        self.assertEqual("route_mismatch", mismatched.reason)

    def test_reconciliation_without_a_reservation_is_missing(self) -> None:
        outcome = self.memory.reconcile_usage(
            self._reconciliation("no-such-attempt", "s", "t")
        )
        self.assertIs(ReconciliationOutcome.MISSING, outcome.outcome)
        self.assertEqual([], self.memory.usage_records())


# --------------------------------------------------------------------------
# 18-19: Claude route, reference cost and the resource ceiling
# --------------------------------------------------------------------------


class ClaudeTelemetryTests(unittest.TestCase):
    """No live CLI, auth probe, model process or network call is possible."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        for target, attribute in (
            (claude_code.subprocess, "Popen"),
            (claude_code.subprocess, "run"),
            (claude_code.shutil, "which"),
        ):
            patcher = mock.patch.object(
                target,
                attribute,
                side_effect=AssertionError(f"{attribute} must not be called"),
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.connector = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.05,
        )
        self.task = make_task(target=self.root)

    def _usage(self, payload, billing_class):
        return self.connector._usage(self.task, payload, billing_class, "evidence", 1)

    def test_subscription_usage_is_zero_money_with_optional_reference(self) -> None:
        usage = self._usage(
            {"total_cost_usd": 0.42, "usage": {"input_tokens": 10, "output_tokens": 5}},
            BillingClass.SUBSCRIPTION_INCLUDED,
        )
        self.assertIs(BillingClass.SUBSCRIPTION_INCLUDED, usage.billing_class)
        self.assertIs(MonetaryState.NOT_APPLICABLE, usage.monetary_state)
        self.assertIsNone(usage.monetary_cost_usd)
        self.assertAlmostEqual(0.42, usage.reference_cost_usd)
        self.assertEqual(0.0, usage.budget_amount_usd)
        self.assertFalse(usage.counts_toward_monetary_budget)

    def test_malformed_cost_telemetry_becomes_no_reference_cost(self) -> None:
        for value in ("not-a-number", -1.0, float("nan"), float("inf"), {}, [], True):
            with self.subTest(total_cost_usd=repr(value)):
                usage = self._usage(
                    {"total_cost_usd": value}, BillingClass.SUBSCRIPTION_INCLUDED
                )
                self.assertIsNone(usage.reference_cost_usd)
                self.assertIsNone(usage.monetary_cost_usd)
                self.assertIs(
                    BillingClass.SUBSCRIPTION_INCLUDED, usage.billing_class
                )

    def test_malformed_token_telemetry_uses_the_legacy_zero(self) -> None:
        for payload in (
            {"usage": {"input_tokens": "x", "output_tokens": None}},
            {"usage": {"input_tokens": -5, "output_tokens": 1.5}},
            {"usage": {"input_tokens": float("nan"), "output_tokens": float("inf")}},
            {"usage": "not-a-mapping"},
            {},
        ):
            with self.subTest(payload=repr(payload)):
                usage = self._usage(payload, BillingClass.SUBSCRIPTION_INCLUDED)
                self.assertEqual(0, usage.input_tokens)
                self.assertEqual(0, usage.output_tokens)

    def test_resource_ceiling_stays_outside_monetary_accounting(self) -> None:
        estimate = CostEstimate(
            billing_class=BillingClass.SUBSCRIPTION_INCLUDED,
            maximum_usd=0.0,
            resource_ceiling_usd=0.05,
        )
        validate_cost_estimate(estimate)
        self.assertEqual(0.0, estimate.monetary_maximum_usd)
        self.assertFalse(estimate.is_paid)
        metered = CostEstimate(
            billing_class=BillingClass.METERED_MONETARY,
            maximum_usd=0.10,
            resource_ceiling_usd=5.00,
        )
        validate_cost_estimate(metered)
        # The projection is the maximum, never the containment ceiling.
        self.assertEqual(0.10, metered.monetary_maximum_usd)
        self.assertIn(format_usd(0.10), metered.gate_state)
        self.assertIn(format_usd(5.00), metered.gate_state)


class CeilingIsNotSpendTests(BillingIntegrityTestCase):
    def test_a_large_ceiling_never_reserves_or_gates_money(self) -> None:
        self.seed_spend(19.95)
        connector = ReportingConnector(
            maximum_usd=0.01, monetary_cost_usd=0.01, resource_ceiling_usd=100.0
        )
        run = self.core(connector, self.store()).run(make_task())
        # 19.95 + 0.01 stays under the cap even though the ceiling is 100 USD.
        self.assertEqual(RunStatus.COMPLETED, run.status)
        reservation = self.reservations(run.task.id)[0]
        self.assertEqual(10_000_000, reservation["reserved_nano_usd"])
        self.assertEqual(
            Decimal("19.960000000"), self.store().ledger_snapshot().measured_usd
        )


# --------------------------------------------------------------------------
# 20: schema 4 -> 5 migration
# --------------------------------------------------------------------------


class SchemaMigrationTests(unittest.TestCase):
    """A schema-4 database upgrades to 5 additively, preserving H2 history."""

    V4_SCHEMA = """
    CREATE TABLE schema_version (version INTEGER NOT NULL);
    CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    CREATE TABLE sessions (id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, state_json TEXT NOT NULL,
        status TEXT NOT NULL, stage TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE permission_grants (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL, task_id TEXT NOT NULL,
        session_id TEXT NOT NULL, fingerprint TEXT NOT NULL, scope_fingerprint TEXT NOT NULL,
        grant_json TEXT NOT NULL, approved INTEGER NOT NULL, remember_for_session INTEGER NOT NULL,
        expires_at TEXT NOT NULL, created_at TEXT NOT NULL, consumed_by_attempt TEXT
    );
    CREATE TABLE execution_attempts (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
        connector TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE usage_ledger (
        id TEXT PRIMARY KEY, session_id TEXT, task_id TEXT, provider TEXT, model TEXT,
        connector TEXT, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        estimated_cost_usd REAL NOT NULL, actual_cost_usd REAL NOT NULL, is_paid INTEGER NOT NULL,
        billing_class TEXT, monetary_state TEXT, monetary_cost_usd REAL, reference_cost_usd REAL,
        call_count INTEGER, billing_evidence TEXT, recorded_at TEXT NOT NULL
    );
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "v4.db"
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(self.V4_SCHEMA)
            connection.execute("INSERT INTO schema_version (version) VALUES (4)")
            connection.execute(
                "INSERT INTO sessions (id, state_json, updated_at) "
                "VALUES ('s1', '{}', 't')"
            )
            connection.execute(
                "INSERT INTO tasks (id, session_id, state_json, status, stage, updated_at) "
                "VALUES ('t1', 's1', '{}', 'completed', 'completed', 't')"
            )
            connection.execute(
                "INSERT INTO permission_grants (id, request_id, task_id, session_id, "
                "fingerprint, scope_fingerprint, grant_json, approved, remember_for_session, "
                "expires_at, created_at, consumed_by_attempt) VALUES "
                "('g1','r1','t1','s1','fp','sfp','{}',1,0,'2999-01-01T00:00:00+00:00','t','a1')"
            )
            connection.execute(
                "INSERT INTO execution_attempts (id, session_id, task_id, connector, "
                "fingerprint, state, created_at, updated_at) VALUES "
                "('a1','s1','t1','c','fp','finished','t','t')"
            )
            connection.execute(
                "INSERT INTO usage_ledger (id, session_id, task_id, input_tokens, "
                "output_tokens, estimated_cost_usd, actual_cost_usd, is_paid, "
                "billing_class, monetary_state, monetary_cost_usd, recorded_at) VALUES "
                "('u1','s1','t1',1,2,0.0,0.42,1,'metered_monetary','measured',0.42,?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            connection.commit()
        finally:
            connection.close()

    def _rows(self, sql: str):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_v4_upgrades_to_v5_additively(self) -> None:
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(5, SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, store.schema_version())
        tables = {
            row[0]
            for row in self._rows("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("attempt_billing", tables)
        columns = {
            row[1]
            for row in self._rows("PRAGMA table_info(attempt_billing)")
        }
        self.assertEqual(
            {
                "attempt_id",
                "usage_id",
                "session_id",
                "task_id",
                "ledger_month",
                "billing_class",
                "reserved_nano_usd",
                "state",
                "actual_nano_usd",
                "created_at",
                "updated_at",
            },
            columns,
        )
        # Every schema-4 row survives untouched.
        self.assertEqual([("s1",)], self._rows("SELECT id FROM sessions"))
        self.assertEqual([("t1", "completed")], self._rows("SELECT id, status FROM tasks"))
        self.assertEqual(
            [("g1", "a1")],
            self._rows("SELECT id, consumed_by_attempt FROM permission_grants"),
        )
        self.assertEqual(
            [("a1", "finished")], self._rows("SELECT id, state FROM execution_attempts")
        )
        self.assertEqual([("u1", 0.42)], self._rows("SELECT id, monetary_cost_usd FROM usage_ledger"))
        self.assertAlmostEqual(0.42, store.monthly_monetary_cost())
        # No reservations exist yet, so exposure equals measured spend.
        snapshot = store.ledger_snapshot()
        self.assertTrue(snapshot.valid)
        self.assertEqual(snapshot.measured_usd, snapshot.total_usd)

    def test_migration_is_idempotent(self) -> None:
        SQLiteMemoryStore(self.database)
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(SCHEMA_VERSION, store.schema_version())
        self.assertEqual([("u1", 0.42)], self._rows("SELECT id, monetary_cost_usd FROM usage_ledger"))
        # Recorded exactly once, even after re-opening the database.
        self.assertEqual(
            [(5,)],
            self._rows("SELECT version FROM schema_migrations ORDER BY version"),
        )


# --------------------------------------------------------------------------
# Preserved H1/H2 invariants under the new claim path
# --------------------------------------------------------------------------


class PreservedInvariantTests(BillingIntegrityTestCase):
    def test_cancellation_still_wins_while_usage_stays_accounted(self) -> None:
        started = threading.Event()
        release = threading.Event()
        connector = BarrierConnector(
            name="barrier",
            started=started,
            release=release,
            estimate_max=0.20,
            actual_cost=0.05,
        )
        owner = self.core(connector, self.store())
        session = owner.start_session("cancel race")
        waiting = owner.run(make_task(effect=SideEffect.EXTERNAL_EGRESS), session.id)
        grant = PermissionGrant.for_request(waiting.permission_request)
        outcome: dict[str, object] = {}

        def run_owner() -> None:
            outcome["run"] = owner.resume(session.id, waiting.task.id, grant)

        thread = threading.Thread(target=run_owner)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=60))
            # The same fake contract: a parked execution cannot be force-stopped.
            canceller = self.core(
                BarrierConnector(name="barrier", estimate_max=0.20), self.store()
            )
            pending = canceller.cancel(session.id, waiting.task.id)
            self.assertEqual(RunStatus.CANCELLING, pending.status)
        finally:
            release.set()
            thread.join(timeout=60)

        run = outcome["run"]
        self.assertEqual(RunStatus.CANCELLED, run.status)
        self.assertIs(
            TaskStatus.CANCELLED, self.store().load_task(waiting.task.id).status
        )
        # The money the call really incurred is still durably accounted.
        self.assertEqual(
            Decimal("0.050000000"), self.store().ledger_snapshot().measured_usd
        )
        self.assertEqual(
            ReservationState.RECONCILED.value,
            self.reservations(waiting.task.id)[0]["state"],
        )
        self.assertEqual([], self.store().list_decisions(session.id))

    def test_connector_failure_reconciles_and_never_completes(self) -> None:
        connector = DummyConnector(
            name="metered",
            estimate_max=0.20,
            actual_cost=0.05,
            outcome=OutcomeStatus.ERROR,
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.CONNECTOR_FAILED, run.error.code)
        self.assertEqual(
            Decimal("0.050000000"), self.store().ledger_snapshot().measured_usd
        )
        self.assertNotIn("task_completed", self.events(run.session.id))

    def test_unclassified_route_still_fails_before_any_call(self) -> None:
        connector = ReportingConnector(route=BillingClass.UNKNOWN)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_ROUTE_UNKNOWN, run.error.code)
        self.assertEqual(0, connector.execute_calls)
        self.assertEqual([], self.reservations(run.task.id))

    def test_error_result_carries_no_raw_connector_telemetry(self) -> None:
        connector = ReportingConnector(
            maximum_usd=0.20,
            monetary_state=MonetaryState.UNKNOWN,
            monetary_cost_usd=None,
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code)
        reason = run.error.details["reason"]
        self.assertLessEqual(len(reason), 120)
        self.assertNotIn("token", reason.lower())
        self.assertNotIn("secret", reason.lower())


# --------------------------------------------------------------------------
# P1: the ledger month is fixed at claim time and never migrates
# --------------------------------------------------------------------------


class MonthRolloverReconciliationTests(BillingIntegrityTestCase):
    """A metered attempt claimed in month A stays accounted in month A.

    The UTC calendar is injected, never slept on: ``jarvis.memory.ledger_month``
    and ``jarvis.memory.utc_now`` are the only clocks the storage layer reads,
    so patching them advances the month deterministically.
    """

    MONTH_A = "2026-07"
    MONTH_B = "2026-08"

    @contextlib.contextmanager
    def clock(self, month: str, day: int = 15):
        """Pin the storage layer's UTC clock to one instant of ``month``."""
        stamp = f"{month}-{day:02d}T12:00:00+00:00"
        with mock.patch("jarvis.memory.ledger_month", return_value=month), mock.patch(
            "jarvis.memory.utc_now", return_value=stamp
        ):
            yield stamp

    def stage_task(self, memory, session, maximum_usd: float) -> Task:
        connector = DummyConnector(
            name="metered",
            side_effect=SideEffect.EXTERNAL_EGRESS,
            estimate_max=maximum_usd,
        )
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        memory.save_task(session.id, task)
        return task

    def claim(self, memory, session, month: str, reservation_usd: str, day: int = 15):
        """Claim one attempt whose reservation is fixed to ``month``."""
        task = self.stage_task(memory, session, float(reservation_usd))
        with self.clock(month, day):
            attempt = memory.open_execution_attempt(
                ExecutionAttempt(session.id, task.id, "metered", "fp")
            )
            claim = BillingClaim(
                ledger_month=month,
                expected_exposure_nano_usd=memory.ledger_snapshot(
                    month
                ).total_nano_usd,
                reservation_nano_usd=nano_usd(reservation_usd),
                limit_nano_usd=nano_usd("20.00"),
                billing_class=BillingClass.METERED_MONETARY,
                gate_state=task.estimate.gate_state,
            )
            outcome = memory.begin_execution(
                attempt.id, session.id, task.id, [], claim
            )
        self.assertIs(ExecutionClaimOutcome.CLAIMED, outcome)
        return task, attempt

    def measured(self, attempt, session, task, actual_usd: str):
        return BillingReconciliation(
            attempt_id=attempt.id,
            session_id=session.id,
            task_id=task.id,
            connector="metered",
            billing_class=BillingClass.METERED_MONETARY,
            monetary_state=MonetaryState.MEASURED,
            actual_nano_usd=nano_usd(actual_usd),
            estimated_cost_usd=0.10,
        )

    def session_for(self, memory) -> SessionState:
        session = SessionState(objective="month rollover")
        memory.save_session(session)
        return session

    def test_in_flight_across_the_boundary_reconciles_into_the_claim_month(
        self,
    ) -> None:
        memory = self.store()
        session = self.session_for(memory)

        # 1. A metered attempt is claimed and reserves cost in month A.
        task_a, attempt_a = self.claim(memory, session, self.MONTH_A, "0.10")
        opened = memory.ledger_snapshot(self.MONTH_A)
        self.assertEqual(nano_usd("0.10"), opened.reserved_nano_usd)
        self.assertEqual(0, opened.measured_nano_usd)
        self.assertTrue(opened.has_unresolved_liability)

        # 2/3. The execution stays in flight while the clock rolls into month
        # B. The month-A reservation is not month-B exposure.
        empty_b = memory.ledger_snapshot(self.MONTH_B)
        self.assertEqual(0, empty_b.total_nano_usd)
        self.assertFalse(empty_b.has_unresolved_liability)

        # 4. New, valid month-B exposure is admitted against the month-B cap.
        task_b, attempt_b = self.claim(memory, session, self.MONTH_B, "0.25", day=3)
        before_b = memory.ledger_snapshot(self.MONTH_B)
        self.assertEqual(nano_usd("0.25"), before_b.reserved_nano_usd)
        self.assertEqual(0, before_b.measured_nano_usd)

        # 5. The month-A attempt reconciles with a valid measured actual while
        # the wall clock already reads month B.
        with self.clock(self.MONTH_B, day=9):
            result = memory.reconcile_usage(
                self.measured(attempt_a, session, task_a, "0.07")
            )
        self.assertIs(ReconciliationOutcome.RECONCILED, result.outcome)

        # The actual usage stays accounted in month A.
        self.assertEqual(self.MONTH_A, result.usage.recorded_at[:7])
        settled_a = memory.ledger_snapshot(self.MONTH_A)
        self.assertEqual(nano_usd("0.07"), settled_a.measured_nano_usd)
        # The month-A reservation was replaced, not doubled and not stranded.
        self.assertEqual(0, settled_a.reserved_nano_usd)
        self.assertEqual(nano_usd("0.07"), settled_a.total_nano_usd)
        self.assertFalse(settled_a.has_unresolved_liability)

        # No actual cost for that attempt appears in month B, and month-B
        # exposure is untouched by the late reconciliation.
        settled_b = memory.ledger_snapshot(self.MONTH_B)
        self.assertEqual(0, settled_b.measured_nano_usd)
        self.assertEqual(before_b.reserved_nano_usd, settled_b.reserved_nano_usd)
        self.assertEqual(before_b.total_nano_usd, settled_b.total_nano_usd)

        # Usage identity stays attempt-bound; exactly one row, no duplicate spend.
        rows = memory.usage_records()
        self.assertEqual(1, len(rows))
        record = memory.attempt_billing_record(attempt_a.id)
        self.assertEqual(record["usage_id"], rows[0].id)
        self.assertEqual(task_a.id, rows[0].task_id)
        self.assertEqual(self.MONTH_A, record["ledger_month"])
        self.assertEqual(ReservationState.RECONCILED.value, record["state"])
        self.assertEqual(nano_usd("0.07"), record["actual_nano_usd"])
        # The month-B attempt is still an open month-B liability.
        other = memory.attempt_billing_record(attempt_b.id)
        self.assertEqual(self.MONTH_B, other["ledger_month"])
        self.assertEqual(ReservationState.ACTIVE.value, other["state"])

        # Replaying the same attempt from month B stays idempotent and cannot
        # move, duplicate or re-date the spend.
        with self.clock(self.MONTH_B, day=20):
            replay = memory.reconcile_usage(
                self.measured(attempt_a, session, task_a, "0.07")
            )
        self.assertIs(ReconciliationOutcome.IDEMPOTENT, replay.outcome)
        self.assertEqual(self.MONTH_A, replay.usage.recorded_at[:7])
        self.assertEqual(1, len(memory.usage_records()))
        self.assertEqual(
            nano_usd("0.07"), memory.ledger_snapshot(self.MONTH_A).measured_nano_usd
        )
        self.assertEqual(
            before_b.total_nano_usd, memory.ledger_snapshot(self.MONTH_B).total_nano_usd
        )

    def test_under_and_over_estimate_rollovers_stay_in_the_claim_month(self) -> None:
        memory = self.store()
        session = self.session_for(memory)
        # Reserved 0.10, actual 0.02 (under) and reserved 0.01, actual 0.50
        # (over the approved maximum - storage records it in full; the loop
        # then fails the task, which is covered separately).
        under_task, under_attempt = self.claim(memory, session, self.MONTH_A, "0.10")
        over_task, over_attempt = self.claim(
            memory, session, self.MONTH_A, "0.01", day=20
        )
        self.assertEqual(
            nano_usd("0.11"), memory.ledger_snapshot(self.MONTH_A).reserved_nano_usd
        )

        with self.clock(self.MONTH_B, day=2):
            self.assertIs(
                ReconciliationOutcome.RECONCILED,
                memory.reconcile_usage(
                    self.measured(under_attempt, session, under_task, "0.02")
                ).outcome,
            )
            self.assertIs(
                ReconciliationOutcome.RECONCILED,
                memory.reconcile_usage(
                    self.measured(over_attempt, session, over_task, "0.50")
                ).outcome,
            )

        settled_a = memory.ledger_snapshot(self.MONTH_A)
        # Both actuals land in month A; the over-estimate is recorded in full.
        self.assertEqual(nano_usd("0.52"), settled_a.measured_nano_usd)
        self.assertEqual(0, settled_a.reserved_nano_usd)
        self.assertEqual(nano_usd("0.52"), settled_a.total_nano_usd)
        settled_b = memory.ledger_snapshot(self.MONTH_B)
        self.assertEqual(0, settled_b.total_nano_usd)
        self.assertEqual(
            {self.MONTH_A},
            {row.recorded_at[:7] for row in memory.usage_records()},
        )
        self.assertEqual(2, len(memory.usage_records()))

    def test_a_late_reconciliation_cannot_admit_extra_month_b_exposure(self) -> None:
        # The month-B cap is enforced against month-B exposure only, and the
        # late month-A settlement never inflates or relieves it.
        memory = self.store()
        session = self.session_for(memory)
        task_a, attempt_a = self.claim(memory, session, self.MONTH_A, "0.10")
        with self.clock(self.MONTH_B, day=1):
            memory.append_usage(
                UsageRecord(
                    billing_class=BillingClass.METERED_MONETARY,
                    monetary_state=MonetaryState.MEASURED,
                    monetary_cost_usd=19.95,
                    recorded_at=f"{self.MONTH_B}-01T00:00:00+00:00",
                )
            )
        before_b = memory.ledger_snapshot(self.MONTH_B)
        self.assertEqual(nano_usd("19.95"), before_b.total_nano_usd)

        with self.clock(self.MONTH_B, day=5):
            self.assertIs(
                ReconciliationOutcome.RECONCILED,
                memory.reconcile_usage(
                    self.measured(attempt_a, session, task_a, "0.07")
                ).outcome,
            )
        # Month B is exactly what it was; month A absorbed the settlement.
        self.assertEqual(
            before_b.total_nano_usd, memory.ledger_snapshot(self.MONTH_B).total_nano_usd
        )
        self.assertEqual(
            nano_usd("0.07"), memory.ledger_snapshot(self.MONTH_A).total_nano_usd
        )

    def test_no_connector_or_completion_timestamp_can_choose_the_month(self) -> None:
        # Structural: the authoritative reconciliation contract carries no
        # timestamp and no month at all, so a connector cannot supply one.
        fields = set(BillingReconciliation.__dataclass_fields__)
        self.assertNotIn("recorded_at", fields)
        self.assertNotIn("ledger_month", fields)
        # And the derivation itself refuses anything outside the claim month.
        self.assertEqual(
            "2026-07-15T12:00:00+00:00",
            ledger_timestamp("2026-07", "2026-07-15T12:00:00+00:00"),
        )
        for outside in ("2026-08-03T09:00:00+00:00", "1999-01-01T00:00:00+00:00", None, 7):
            with self.subTest(claimed_at=repr(outside)):
                self.assertEqual(
                    "2026-07-01T00:00:00+00:00", ledger_timestamp("2026-07", outside)
                )

    def test_malformed_ledger_months_fail_closed(self) -> None:
        for value in (None, "", "x", "2026-13", "2026-00", "2026-1", "2026-07-01", 202607):
            with self.subTest(month=repr(value)):
                with self.assertRaises(BillingIntegrityError):
                    validated_ledger_month(value)
                with self.assertRaises(BillingIntegrityError):
                    ledger_timestamp(value, "2026-07-15T12:00:00+00:00")
        # A malformed month never returns a valid-looking empty position.
        snapshot = self.store().ledger_snapshot("not-a-month")
        self.assertFalse(snapshot.valid)
        self.assertEqual("malformed_ledger_month", snapshot.reason)
        self.assertEqual(nano_usd("0.00"), snapshot.measured_nano_usd)


# --------------------------------------------------------------------------
# P1-A: connector-reported billing data is observation, never authority
# --------------------------------------------------------------------------


class AuthoritativeRouteReconciliationTests(BillingIntegrityTestCase):
    """The execution-time route decides; connector telemetry only observes."""

    #: (authoritative route, reported class, reported state, reported money)
    MISMATCHES = (
        # A metered route may never be settled as unbilled usage.
        (
            BillingClass.METERED_MONETARY,
            BillingClass.SUBSCRIPTION_INCLUDED,
            MonetaryState.MEASURED,
            0.05,
        ),
        (
            BillingClass.METERED_MONETARY,
            BillingClass.NO_ADDITIONAL_CHARGE,
            MonetaryState.NOT_APPLICABLE,
            None,
        ),
        (
            BillingClass.METERED_MONETARY,
            BillingClass.UNKNOWN,
            MonetaryState.UNKNOWN,
            None,
        ),
        # A metered route may not be satisfied by a reference figure alone.
        (
            BillingClass.METERED_MONETARY,
            BillingClass.METERED_MONETARY,
            MonetaryState.MEASURED,
            None,
        ),
        (
            BillingClass.METERED_MONETARY,
            BillingClass.METERED_MONETARY,
            MonetaryState.NOT_APPLICABLE,
            0.05,
        ),
        (
            BillingClass.METERED_MONETARY,
            BillingClass.METERED_MONETARY,
            MonetaryState.UNKNOWN,
            0.05,
        ),
        # An unbilled route may never be upgraded into monetary spend.
        (
            BillingClass.SUBSCRIPTION_INCLUDED,
            BillingClass.METERED_MONETARY,
            MonetaryState.MEASURED,
            0.05,
        ),
        (
            BillingClass.SUBSCRIPTION_INCLUDED,
            BillingClass.SUBSCRIPTION_INCLUDED,
            MonetaryState.MEASURED,
            0.05,
        ),
        (
            BillingClass.SUBSCRIPTION_INCLUDED,
            BillingClass.SUBSCRIPTION_INCLUDED,
            MonetaryState.NOT_APPLICABLE,
            0.05,
        ),
        (
            BillingClass.SUBSCRIPTION_INCLUDED,
            BillingClass.SUBSCRIPTION_INCLUDED,
            MonetaryState.UNKNOWN,
            None,
        ),
        (
            BillingClass.NO_ADDITIONAL_CHARGE,
            BillingClass.METERED_MONETARY,
            MonetaryState.MEASURED,
            0.05,
        ),
        (
            BillingClass.NO_ADDITIONAL_CHARGE,
            BillingClass.NO_ADDITIONAL_CHARGE,
            MonetaryState.MEASURED,
            0.05,
        ),
    )

    def test_every_class_state_money_mismatch_fails_closed(self) -> None:
        for route, reported, state, money in self.MISMATCHES:
            with self.subTest(
                route=route.value,
                reported=reported.value,
                state=state.value,
                money=money,
            ):
                store = self.store()
                connector = ReportingConnector(
                    route=route,
                    reported_class=reported,
                    monetary_state=state,
                    monetary_cost_usd=money,
                    maximum_usd=0.20,
                )
                run = self.core(connector, store).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(
                    ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code
                )
                # Never VERIFY/PRESENT, never a decision or completion event.
                events = self.events(run.session.id)
                self.assertNotIn("task_completed", events)
                self.assertNotIn("verification_passed", events)
                self.assertEqual([], self.store().list_decisions(run.session.id))
                # No fabricated monetary row; the reservation is retained.
                reservation = self.reservations(run.task.id)[0]
                self.assertEqual(route.value, reservation["billing_class"])
                self.assertEqual(
                    ReservationState.UNRESOLVED.value, reservation["state"]
                )
                self.assertEqual(
                    [],
                    [
                        item
                        for item in self.store().usage_records()
                        if item.task_id == run.task.id
                    ],
                )

    def test_valid_class_state_money_combinations_still_settle(self) -> None:
        cases = (
            (BillingClass.METERED_MONETARY, MonetaryState.MEASURED, 0.05, None),
            (BillingClass.METERED_MONETARY, MonetaryState.MEASURED, 0.0, None),
            (
                BillingClass.SUBSCRIPTION_INCLUDED,
                MonetaryState.NOT_APPLICABLE,
                None,
                0.42,
            ),
            (
                BillingClass.NO_ADDITIONAL_CHARGE,
                MonetaryState.NOT_APPLICABLE,
                None,
                None,
            ),
        )
        for route, state, money, reference in cases:
            with self.subTest(route=route.value, money=money):
                store = self.store()
                connector = ReportingConnector(
                    route=route,
                    monetary_state=state,
                    monetary_cost_usd=money,
                    reference_cost_usd=reference,
                    maximum_usd=0.20,
                )
                run = self.core(connector, store).run(make_task())
                self.assertEqual(RunStatus.COMPLETED, run.status)
                record = [
                    item
                    for item in self.store().usage_records()
                    if item.task_id == run.task.id
                ][0]
                self.assertIs(route, record.billing_class)
                self.assertIs(state, record.monetary_state)

    def test_connector_identity_evidence_and_call_count_are_not_authoritative(
        self,
    ) -> None:
        connector = ReportingConnector(
            name="selected-connector",
            maximum_usd=0.20,
            monetary_cost_usd=0.05,
            reported_connector="attacker-connector",
            reported_evidence="attacker_supplied_evidence",
            reported_call_count=999,
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        record = self.store().usage_records()[0]
        # Identity comes from the selected connector manifest.
        self.assertEqual("selected-connector", record.connector)
        # Evidence comes from the execution-time estimate.
        self.assertEqual("reporting_connector_fixed_route", record.billing_evidence)
        # One claimed execution is exactly one external call.
        self.assertEqual(1, record.call_count)
        self.assertEqual(record.connector, run.result.usage.connector)
        self.assertEqual(1, run.result.usage.call_count)

    def test_unknown_route_never_reconciles(self) -> None:
        # An UNKNOWN route is refused before execution, so no monetary
        # execution can ever be settled on it.
        connector = ReportingConnector(route=BillingClass.UNKNOWN)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_ROUTE_UNKNOWN, run.error.code)
        self.assertEqual(0, connector.execute_calls)
        # And directly at the storage boundary it cannot be claimed either.
        with self.assertRaises(BillingIntegrityError):
            self.store().begin_execution(
                "a",
                "s",
                "t",
                [],
                BillingClaim(
                    ledger_month=ledger_month(),
                    expected_exposure_nano_usd=0,
                    reservation_nano_usd=0,
                    limit_nano_usd=MONTHLY_HARD_LIMIT_NANO_USD,
                    billing_class=BillingClass.UNKNOWN,
                    gate_state="gate",
                ),
            )


# --------------------------------------------------------------------------
# P1-B: the whole monthly accounting dataset is validated, fail-closed
# --------------------------------------------------------------------------


class LedgerFullValidationTests(BillingIntegrityTestCase):
    """Raw-SQL corruption must invalidate the month, never vanish from it."""

    def settled_run(self):
        """One completed metered execution: usage row plus reconciled record."""
        connector = ReportingConnector(maximum_usd=0.20, monetary_cost_usd=0.05)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        return run

    def corrupt(self, sql: str, parameters: tuple = ()) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    def assert_invalid(self, reason: str) -> None:
        snapshot = self.store().ledger_snapshot()
        self.assertFalse(snapshot.valid, "the corrupted month must not be valid")
        self.assertEqual(reason, snapshot.reason)
        # The malformed data is never silently excluded from exposure: no
        # reduced-but-valid position is ever reported.
        self.assertEqual(0, snapshot.measured_nano_usd)
        self.assertEqual(0, snapshot.reserved_nano_usd)
        with self.assertRaises(BillingIntegrityError):
            self.store().monthly_monetary_cost()
        # And no metered execution may proceed against it.
        connector = DummyConnector(name="later", estimate_max=0.10)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code)
        self.assertEqual(0, connector.execute_calls)

    def test_tampered_billing_class_invalidates_the_month(self) -> None:
        # The exact finding: a 19 USD row relabelled to an unknown class must
        # not disappear from enforcement.
        self.seed_spend(19.00)
        self.assertEqual(
            nano_usd("19.00"), self.store().ledger_snapshot().measured_nano_usd
        )
        self.corrupt("UPDATE usage_ledger SET billing_class = 'tampered'")
        self.assert_invalid("malformed_usage_row")

    def test_malformed_monetary_state_invalidates_the_month(self) -> None:
        self.seed_spend(19.00)
        self.corrupt("UPDATE usage_ledger SET monetary_state = 'sort-of-measured'")
        self.assert_invalid("malformed_usage_row")

    def test_measured_money_on_a_non_metered_route_invalidates(self) -> None:
        self.seed_spend(1.00)
        self.corrupt(
            "UPDATE usage_ledger SET billing_class = ?, monetary_state = ?",
            (
                BillingClass.SUBSCRIPTION_INCLUDED.value,
                MonetaryState.NOT_APPLICABLE.value,
            ),
        )
        self.assert_invalid("malformed_usage_row")

    def test_a_metered_ledger_row_is_valid_only_when_measured(self) -> None:
        # A recorded metered row is settled money or it is corruption. It is
        # never a valid zero-cost monetary history entry, so an UNKNOWN or
        # NOT_APPLICABLE metered row must invalidate the month rather than be
        # counted as nothing.
        cases = (
            ("METERED + UNKNOWN + NULL", MonetaryState.UNKNOWN, None),
            (
                "METERED + NOT_APPLICABLE + NULL",
                MonetaryState.NOT_APPLICABLE,
                None,
            ),
            ("METERED + MEASURED + NULL", MonetaryState.MEASURED, None),
        )
        for label, state, amount in cases:
            with self.subTest(case=label):
                self.setUp()
                self.seed_spend(19.00)
                self.assertEqual(
                    nano_usd("19.00"),
                    self.store().ledger_snapshot().measured_nano_usd,
                )
                self.corrupt(
                    "UPDATE usage_ledger SET monetary_state = ?, "
                    "monetary_cost_usd = ?",
                    (state.value, amount),
                )
                self.assert_invalid("malformed_usage_row")

    def test_an_unresolved_reservation_is_not_a_reconciled_usage_row(self) -> None:
        # An unresolved reservation keeps its own semantics: it is exposure
        # without a usage row, and that stays perfectly valid.
        connector = ReportingConnector(
            maximum_usd=0.20,
            monetary_state=MonetaryState.UNKNOWN,
            monetary_cost_usd=None,
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code)
        reservation = self.reservations(run.task.id)[0]
        self.assertEqual(ReservationState.UNRESOLVED.value, reservation["state"])
        snapshot = self.store().ledger_snapshot()
        self.assertTrue(snapshot.valid)
        self.assertEqual(0, snapshot.measured_nano_usd)
        self.assertEqual(nano_usd("0.20"), snapshot.reserved_nano_usd)
        self.assertEqual(nano_usd("0.20"), snapshot.unresolved_nano_usd)
        self.assertTrue(snapshot.has_unresolved_liability)

    def test_negative_and_non_representable_amounts_invalidate(self) -> None:
        for statement in (
            "UPDATE usage_ledger SET monetary_cost_usd = -5.0",
            "UPDATE usage_ledger SET monetary_cost_usd = 9e999",
            "UPDATE usage_ledger SET monetary_cost_usd = 1e30",
        ):
            with self.subTest(statement=statement):
                self.setUp()
                self.seed_spend(1.00)
                self.corrupt(statement)
                self.assert_invalid("malformed_usage_row")

    def test_unknown_route_row_carrying_money_invalidates(self) -> None:
        self.seed_spend(1.00)
        self.corrupt(
            "UPDATE usage_ledger SET billing_class = ?, monetary_state = ?",
            (BillingClass.UNKNOWN.value, MonetaryState.UNKNOWN.value),
        )
        self.assert_invalid("malformed_usage_row")

    def test_malformed_integer_telemetry_invalidates(self) -> None:
        self.seed_spend(1.00)
        self.corrupt("UPDATE usage_ledger SET input_tokens = -3")
        self.assert_invalid("malformed_usage_row")

    def test_reconciled_reservation_without_its_usage_row_invalidates(self) -> None:
        run = self.settled_run()
        self.corrupt("DELETE FROM usage_ledger WHERE task_id = ?", (run.task.id,))
        self.assert_invalid("reconciled_without_usage_row")

    def test_reconciled_amount_conflict_invalidates(self) -> None:
        run = self.settled_run()
        self.corrupt(
            "UPDATE attempt_billing SET actual_nano_usd = ? WHERE task_id = ?",
            (nano_usd("9.99"), run.task.id),
        )
        self.assert_invalid("reconciled_amount_conflict")

    def test_reconciled_route_conflict_invalidates(self) -> None:
        run = self.settled_run()
        self.corrupt(
            "UPDATE attempt_billing SET billing_class = ? WHERE task_id = ?",
            (BillingClass.SUBSCRIPTION_INCLUDED.value, run.task.id),
        )
        # A subscription reservation must reserve exactly zero, so the
        # impossible route relationship is caught first.
        self.assert_invalid("impossible_reservation_route")

    def test_reconciled_without_an_actual_amount_invalidates(self) -> None:
        run = self.settled_run()
        self.corrupt(
            "UPDATE attempt_billing SET actual_nano_usd = NULL WHERE task_id = ?",
            (run.task.id,),
        )
        self.assert_invalid("reconciled_without_actual")

    def test_unsettled_reservation_carrying_an_actual_invalidates(self) -> None:
        run = self.settled_run()
        self.corrupt(
            "UPDATE attempt_billing SET state = ? WHERE task_id = ?",
            (ReservationState.ACTIVE.value, run.task.id),
        )
        self.assert_invalid("unsettled_reservation_conflict")

    def test_impossible_reservation_amounts_invalidate(self) -> None:
        for statement, parameters, reason in (
            (
                "UPDATE attempt_billing SET reserved_nano_usd = 0",
                (),
                "metered_reservation_without_amount",
            ),
            (
                "UPDATE attempt_billing SET reserved_nano_usd = -1",
                (),
                "malformed_reservation_row",
            ),
            (
                "UPDATE attempt_billing SET billing_class = ?",
                (BillingClass.UNKNOWN.value,),
                "impossible_reservation_route",
            ),
            (
                "UPDATE attempt_billing SET state = 'half-done'",
                (),
                "malformed_reservation_row",
            ),
        ):
            with self.subTest(reason=reason):
                self.setUp()
                self.settled_run()
                self.corrupt(statement, parameters)
                self.assert_invalid(reason)

    def test_a_detached_usage_identity_invalidates(self) -> None:
        # SQLite permits NULL in a non-INTEGER primary key; such a row would
        # otherwise be silently detached from its reservation.
        run = self.settled_run()
        self.corrupt("UPDATE usage_ledger SET id = NULL WHERE task_id = ?", (run.task.id,))
        self.assert_invalid("malformed_usage_identity")

    def test_reconciled_route_conflict_between_reservation_and_usage(self) -> None:
        # The reservation still says metered; its authoritative usage row was
        # rewritten as an unbilled record.
        run = self.settled_run()
        self.corrupt(
            "UPDATE usage_ledger SET billing_class = ?, monetary_state = ?, "
            "monetary_cost_usd = NULL WHERE task_id = ?",
            (
                BillingClass.NO_ADDITIONAL_CHARGE.value,
                MonetaryState.NOT_APPLICABLE.value,
                run.task.id,
            ),
        )
        self.assert_invalid("reconciled_route_conflict")

    def test_a_clean_month_stays_valid(self) -> None:
        run = self.settled_run()
        snapshot = self.store().ledger_snapshot()
        self.assertTrue(snapshot.valid)
        self.assertEqual("", snapshot.reason)
        self.assertEqual(nano_usd("0.05"), snapshot.measured_nano_usd)
        self.assertEqual(0, snapshot.reserved_nano_usd)
        self.assertEqual(run.task.id, self.store().usage_records()[0].task_id)


# --------------------------------------------------------------------------
# P1-C: the claim is mandatory, typed, and the $20 cap is storage-owned
# --------------------------------------------------------------------------


class ClaimBoundaryEnforcementTests(BillingIntegrityTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        self.core_ = self.core(self.connector)
        self.session = self.core_.start_session("claim boundary")

    def staged(self):
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = self.connector.manifest.name
        task.estimate = self.connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        self.memory.save_task(self.session.id, task)
        attempt = self.memory.open_execution_attempt(
            ExecutionAttempt(
                self.session.id, task.id, self.connector.manifest.name, "fp"
            )
        )
        return task, attempt

    def claim(self, **overrides) -> BillingClaim:
        payload = {
            "ledger_month": ledger_month(),
            "expected_exposure_nano_usd": 0,
            "reservation_nano_usd": nano_usd("0.10"),
            "limit_nano_usd": MONTHLY_HARD_LIMIT_NANO_USD,
            "billing_class": BillingClass.METERED_MONETARY,
            "gate_state": "gate",
        }
        payload.update(overrides)
        return BillingClaim(**payload)

    def test_a_missing_claim_cannot_claim_anything(self) -> None:
        # Billing is never inferred from absence: there is no implicit route
        # and no synthetic fallback claim.
        task, attempt = self.staged()
        with self.assertRaises(TypeError):
            self.memory.begin_execution(attempt.id, self.session.id, task.id, [])
        with self.assertRaises(BillingIntegrityError):
            self.memory.begin_execution(
                attempt.id, self.session.id, task.id, [], None
            )
        # Nothing was claimed, consumed, reserved or executed.
        self.assertIs(
            AttemptState.PREPARED,
            self.memory.latest_execution_attempt(task.id).state,
        )
        self.assertIsNone(self.memory.attempt_billing_record(attempt.id))
        self.assertEqual([], self.consumed_grants(task.id))
        self.assertEqual([], self.reservations(task.id))
        self.assertEqual(0, self.connector.execute_calls)
        self.assertEqual([], self.memory.usage_records())

    def test_a_missing_claim_never_consumes_an_approved_grant(self) -> None:
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = self.connector.manifest.name
        task.estimate = self.connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        self.memory.save_task(self.session.id, task)
        request = self.connector.permission_request(task, self.session.id)
        self.memory.save_pending_approval(request)
        self.memory.resolve_approval(
            request, PermissionGrant.for_request(request), "approved"
        )
        attempt = self.memory.open_execution_attempt(
            ExecutionAttempt(
                self.session.id, task.id, self.connector.manifest.name, "fp"
            )
        )
        with self.assertRaises(BillingIntegrityError):
            self.memory.begin_execution(
                attempt.id, self.session.id, task.id, [request.fingerprint], None
            )
        self.assertEqual([], self.consumed_grants(task.id))
        self.assertEqual([], self.reservations(task.id))
        self.assertIs(
            AttemptState.PREPARED,
            self.memory.latest_execution_attempt(task.id).state,
        )
        self.assertEqual(0, self.connector.execute_calls)

    def test_explicit_zero_monetary_routes_still_claim(self) -> None:
        for route in (
            BillingClass.NO_ADDITIONAL_CHARGE,
            BillingClass.SUBSCRIPTION_INCLUDED,
        ):
            with self.subTest(route=route.value):
                task, attempt = self.staged()
                self.assertIs(
                    ExecutionClaimOutcome.CLAIMED,
                    self.memory.begin_execution(
                        attempt.id,
                        self.session.id,
                        task.id,
                        [],
                        self.claim(billing_class=route, reservation_nano_usd=0),
                    ),
                )
                record = self.memory.attempt_billing_record(attempt.id)
                self.assertEqual(0, record["reserved_nano_usd"])
                self.assertEqual(route.value, record["billing_class"])
                self.assertEqual(ReservationState.ACTIVE.value, record["state"])
                # A zero-monetary route adds no exposure.
                self.assertEqual(0, self.memory.ledger_snapshot().total_nano_usd)
                # And a metered result can never be settled through it.
                outcome = self.memory.reconcile_usage(
                    BillingReconciliation(
                        attempt_id=attempt.id,
                        session_id=self.session.id,
                        task_id=task.id,
                        connector="metered",
                        billing_class=BillingClass.METERED_MONETARY,
                        monetary_state=MonetaryState.MEASURED,
                        actual_nano_usd=nano_usd("5.00"),
                    )
                )
                self.assertIs(ReconciliationOutcome.CONFLICT, outcome.outcome)
                self.assertEqual("route_mismatch", outcome.reason)
                self.assertEqual([], self.memory.usage_records())

    def test_an_explicit_metered_claim_still_reserves_and_caps(self) -> None:
        task, attempt = self.staged()
        self.assertIs(
            ExecutionClaimOutcome.CLAIMED,
            self.memory.begin_execution(
                attempt.id, self.session.id, task.id, [], self.claim()
            ),
        )
        record = self.memory.attempt_billing_record(attempt.id)
        self.assertEqual(nano_usd("0.10"), record["reserved_nano_usd"])
        self.assertEqual(
            BillingClass.METERED_MONETARY.value, record["billing_class"]
        )
        snapshot = self.memory.ledger_snapshot()
        self.assertEqual(nano_usd("0.10"), snapshot.reserved_nano_usd)
        self.assertTrue(snapshot.has_unresolved_liability)
        # The next metered claim sees that exposure and is capped against it.
        other_task, other_attempt = self.staged()
        self.assertIs(
            ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED,
            self.memory.begin_execution(
                other_attempt.id,
                self.session.id,
                other_task.id,
                [],
                self.claim(reservation_nano_usd=nano_usd("25.00")),
            ),
        )
        self.assertIsNone(self.memory.attempt_billing_record(other_attempt.id))

    def test_non_billingclaim_values_are_refused(self) -> None:
        for bogus in (
            {"ledger_month": ledger_month()},
            "override_monthly_cost_cap",
            5,
            [ledger_month()],
            object(),
        ):
            with self.subTest(kind=type(bogus).__name__):
                task, attempt = self.staged()
                with self.assertRaises(BillingIntegrityError):
                    self.memory.begin_execution(
                        attempt.id, self.session.id, task.id, [], bogus
                    )
                self.assertIsNone(self.memory.attempt_billing_record(attempt.id))
                self.assertIs(
                    AttemptState.PREPARED,
                    self.memory.latest_execution_attempt(task.id).state,
                )

    def test_malformed_claim_fields_are_refused(self) -> None:
        cases = (
            ("negative reservation", {"reservation_nano_usd": -5}),
            ("float reservation", {"reservation_nano_usd": 1.5}),
            ("bool reservation", {"reservation_nano_usd": True}),
            ("oversized reservation", {"reservation_nano_usd": MAX_NANO_USD + 1}),
            ("zero metered reservation", {"reservation_nano_usd": 0}),
            (
                "non-metered non-zero reservation",
                {
                    "billing_class": BillingClass.NO_ADDITIONAL_CHARGE,
                    "reservation_nano_usd": 5,
                },
            ),
            ("unknown route", {"billing_class": BillingClass.UNKNOWN}),
            ("raw string route", {"billing_class": "metered_monetary"}),
            ("malformed month", {"ledger_month": "not-a-month"}),
            ("float exposure", {"expected_exposure_nano_usd": 1.5}),
            ("negative exposure", {"expected_exposure_nano_usd": -1}),
            ("empty gate state", {"gate_state": ""}),
            ("non-string gate state", {"gate_state": 7}),
            ("non-string override", {"cost_override_fingerprint": 7}),
        )
        for label, overrides in cases:
            with self.subTest(case=label):
                task, attempt = self.staged()
                with self.assertRaises(BillingIntegrityError):
                    self.memory.begin_execution(
                        attempt.id,
                        self.session.id,
                        task.id,
                        [],
                        self.claim(**overrides),
                    )
                self.assertIsNone(self.memory.attempt_billing_record(attempt.id))
                self.assertIs(
                    AttemptState.PREPARED,
                    self.memory.latest_execution_attempt(task.id).state,
                )

    def test_caller_supplied_hard_limit_is_never_trusted(self) -> None:
        # The confirmed finding: 25 USD against a caller-declared 100 USD cap.
        task, attempt = self.staged()
        with self.assertRaises(BillingIntegrityError):
            self.memory.begin_execution(
                attempt.id,
                self.session.id,
                task.id,
                [],
                self.claim(
                    reservation_nano_usd=nano_usd("25.00"),
                    limit_nano_usd=nano_usd("100.00"),
                ),
            )
        self.assertIsNone(self.memory.attempt_billing_record(attempt.id))
        # With the canonical cap declared, the same reservation is refused as a
        # billing recheck instead of quietly claiming.
        self.assertIs(
            ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED,
            self.memory.begin_execution(
                attempt.id,
                self.session.id,
                task.id,
                [],
                self.claim(reservation_nano_usd=nano_usd("25.00")),
            ),
        )
        self.assertIsNone(self.memory.attempt_billing_record(attempt.id))
        self.assertIs(
            AttemptState.PREPARED,
            self.memory.latest_execution_attempt(task.id).state,
        )
        self.assertEqual(0, self.connector.execute_calls)

    def test_only_the_exact_cost_override_request_authorizes_the_claim(self) -> None:
        # A perfectly valid *base permission* grant is not a cost override.
        self.seed_spend(19.90)
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.20
        )
        core = self.core(connector, self.store())
        session = core.start_session("wrong gate")
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        self.memory.save_task(session.id, task)
        base = connector.permission_request(task, session.id)
        self.memory.save_pending_approval(base)
        self.memory.resolve_approval(
            base, PermissionGrant.for_request(base), "approved"
        )
        attempt = self.memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        outcome = self.memory.begin_execution(
            attempt.id,
            session.id,
            task.id,
            [base.fingerprint],
            BillingClaim(
                ledger_month=ledger_month(),
                expected_exposure_nano_usd=nano_usd("19.90"),
                reservation_nano_usd=nano_usd("0.20"),
                limit_nano_usd=MONTHLY_HARD_LIMIT_NANO_USD,
                billing_class=BillingClass.METERED_MONETARY,
                gate_state=task.estimate.gate_state,
                # The fingerprint of a real, approved, unexpired grant - for a
                # different action entirely.
                cost_override_fingerprint=base.fingerprint,
            ),
        )
        self.assertIs(ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED, outcome)
        self.assertIsNone(self.memory.attempt_billing_record(attempt.id))
        self.assertEqual([], self.consumed_grants(task.id))
        self.assertIs(
            AttemptState.PREPARED, self.memory.latest_execution_attempt(task.id).state
        )
        self.assertEqual(0, connector.execute_calls)

    def test_a_stale_projection_override_is_refused_at_the_boundary(self) -> None:
        # A genuine cost-override request, but attested for a lower projection.
        self.seed_spend(19.90)
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.20
        )
        core = self.core(connector, self.store())
        waiting = core.run(
            make_task(effect=SideEffect.EXTERNAL_EGRESS),
            core.start_session("stale gate").id,
        )
        approved = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request),
        )
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, approved.status)
        override = approved.permission_request
        self.memory.resolve_approval(
            override, PermissionGrant.for_request(override), "approved"
        )
        attempt = self.memory.open_execution_attempt(
            ExecutionAttempt(
                approved.session.id,
                approved.task.id,
                connector.manifest.name,
                "fp",
            )
        )
        # Claim a *different* projection than the one the override attested.
        outcome = self.memory.begin_execution(
            attempt.id,
            approved.session.id,
            approved.task.id,
            [override.fingerprint],
            BillingClaim(
                ledger_month=ledger_month(),
                expected_exposure_nano_usd=nano_usd("19.90"),
                reservation_nano_usd=nano_usd("5.00"),
                limit_nano_usd=MONTHLY_HARD_LIMIT_NANO_USD,
                billing_class=BillingClass.METERED_MONETARY,
                gate_state=approved.task.estimate.gate_state,
                cost_override_fingerprint=override.fingerprint,
            ),
        )
        self.assertIs(ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED, outcome)
        self.assertIsNone(self.memory.attempt_billing_record(attempt.id))
        self.assertEqual([], self.consumed_grants(approved.task.id))
        self.assertEqual(0, connector.execute_calls)


# --------------------------------------------------------------------------
# P1-D: losing the attempt is LOST, never a billing recheck
# --------------------------------------------------------------------------


class ClaimOwnershipOrderingTests(BillingIntegrityTestCase):
    def staged(self, memory, session, connector):
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        memory.save_task(session.id, task)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        return task, attempt

    def claim(self, **overrides) -> BillingClaim:
        payload = {
            "ledger_month": ledger_month(),
            "expected_exposure_nano_usd": 0,
            "reservation_nano_usd": nano_usd("0.10"),
            "limit_nano_usd": MONTHLY_HARD_LIMIT_NANO_USD,
            "billing_class": BillingClass.METERED_MONETARY,
            "gate_state": "gate",
        }
        payload.update(overrides)
        return BillingClaim(**payload)

    def test_same_attempt_claimed_twice_is_claimed_then_lost(self) -> None:
        memory = self.store()
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        session = self.core(connector, memory).start_session("twice")
        task, attempt = self.staged(memory, session, connector)
        self.assertIs(
            ExecutionClaimOutcome.CLAIMED,
            memory.begin_execution(attempt.id, session.id, task.id, [], self.claim()),
        )
        reservations = self.reservations(task.id)
        self.assertEqual(1, len(reservations))
        # The second contender simply lost ownership. It is NOT a billing
        # change, so it must not be told to re-gate.
        self.assertIs(
            ExecutionClaimOutcome.LOST,
            memory.begin_execution(
                attempt.id,
                session.id,
                task.id,
                [],
                self.claim(expected_exposure_nano_usd=nano_usd("9.99")),
            ),
        )
        # No extra reservation and no state change.
        self.assertEqual(reservations, self.reservations(task.id))
        self.assertIs(
            AttemptState.IN_FLIGHT, memory.latest_execution_attempt(task.id).state
        )

    def test_a_concluded_or_in_doubt_attempt_is_lost_not_rechecked(self) -> None:
        memory = self.store()
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        session = self.core(connector, memory).start_session("in doubt")
        task, attempt = self.staged(memory, session, connector)
        memory.begin_execution(attempt.id, session.id, task.id, [], self.claim())
        memory.mark_execution_unknown(attempt.id)
        self.assertIs(
            ExecutionClaimOutcome.LOST,
            memory.begin_execution(
                attempt.id,
                session.id,
                task.id,
                [],
                self.claim(expected_exposure_nano_usd=nano_usd("9.99")),
            ),
        )

    def test_a_foreign_session_or_task_is_lost(self) -> None:
        memory = self.store()
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        session = self.core(connector, memory).start_session("foreign")
        task, attempt = self.staged(memory, session, connector)
        for label, args in (
            ("unknown attempt", ("no-such-attempt", session.id, task.id)),
            ("foreign session", (attempt.id, "other-session", task.id)),
        ):
            with self.subTest(case=label):
                self.assertIs(
                    ExecutionClaimOutcome.LOST,
                    memory.begin_execution(*args, [], self.claim()),
                )
        self.assertEqual([], self.reservations(task.id))
        self.assertIs(
            AttemptState.PREPARED, memory.latest_execution_attempt(task.id).state
        )

    def test_a_still_claimable_attempt_still_reports_a_billing_recheck(self) -> None:
        memory = self.store()
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        session = self.core(connector, memory).start_session("recheck")
        task, attempt = self.staged(memory, session, connector)
        self.assertIs(
            ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED,
            memory.begin_execution(
                attempt.id,
                session.id,
                task.id,
                [],
                self.claim(expected_exposure_nano_usd=nano_usd("9.99")),
            ),
        )
        self.assertIs(
            AttemptState.PREPARED, memory.latest_execution_attempt(task.id).state
        )

    def test_concurrent_contenders_have_exactly_one_winner(self) -> None:
        memory = self.store()
        connector = DummyConnector(
            name="metered", side_effect=SideEffect.EXTERNAL_EGRESS, estimate_max=0.10
        )
        session = self.core(connector, memory).start_session("concurrent claim")
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        memory.save_task(session.id, task)
        request = connector.permission_request(task, session.id)
        memory.save_pending_approval(request)
        memory.resolve_approval(
            request, PermissionGrant.for_request(request), "approved"
        )
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, connector.manifest.name, "fp")
        )
        ready = threading.Barrier(2)
        outcomes: list[ExecutionClaimOutcome] = []
        lock = threading.Lock()

        def contend() -> None:
            store = self.store()
            ready.wait(timeout=60)
            outcome = store.begin_execution(
                attempt.id, session.id, task.id, [request.fingerprint], self.claim()
            )
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=contend) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(
            [ExecutionClaimOutcome.CLAIMED, ExecutionClaimOutcome.LOST],
            sorted(outcomes, key=lambda item: item.value),
        )
        # The loser consumed no grant beyond the winner's single consumption,
        # created no extra reservation and left no fresh approval behind.
        self.assertEqual(1, len(self.consumed_grants(task.id)))
        self.assertEqual(1, len(self.reservations(task.id)))
        self.assertIsNone(memory.load_pending_approval(task.id, session.id))
        self.assertNotIn("billing_recheck_required", self.events(session.id))


# --------------------------------------------------------------------------
# P1-E: exact runtime types and SQLite integer bounds
# --------------------------------------------------------------------------


class RuntimeTypeAndBoundsTests(BillingIntegrityTestCase):
    def test_raw_string_and_foreign_enum_values_fail_closed(self) -> None:
        for value in (
            "metered_monetary",
            "no_such_class",
            MonetaryState.MEASURED,
            None,
            1,
            True,
        ):
            with self.subTest(billing_class=repr(value)):
                with self.assertRaises(BillingIntegrityError):
                    require_billing_class(value)
        for value in ("measured", "no_such_state", BillingClass.UNKNOWN, None, 0, False):
            with self.subTest(monetary_state=repr(value)):
                with self.assertRaises(BillingIntegrityError):
                    require_monetary_state(value)

    def test_raw_string_enums_never_reach_persistence(self) -> None:
        record = UsageRecord(monetary_cost_usd=None)
        record.billing_class = "metered_monetary"
        record.monetary_state = "measured"
        # A structured billing-integrity failure, not an AttributeError.
        with self.assertRaises(BillingIntegrityError):
            self.memory.append_usage(record)
        self.assertEqual([], self.memory.usage_records())

    def test_oversized_and_malformed_counts_are_refused_by_the_compat_path(
        self,
    ) -> None:
        for value in (10**100, -1, MAX_SQLITE_INTEGER + 1, True):
            with self.subTest(input_tokens=repr(value)):
                record = UsageRecord(
                    billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
                    monetary_state=MonetaryState.NOT_APPLICABLE,
                )
                record.input_tokens = value
                with self.assertRaises(BillingIntegrityError):
                    self.memory.append_usage(record)
        self.assertEqual([], self.memory.usage_records())

    def test_connector_token_telemetry_is_normalized_not_raised(self) -> None:
        cases = (
            (10**100, 0),
            (MAX_SQLITE_INTEGER + 1, 0),
            (-5, 0),
            (3.5, 0),
            (float("nan"), 0),
            (float("inf"), 0),
            ("120", 0),
            (True, 0),
            (None, 0),
            (MAX_SQLITE_INTEGER, MAX_SQLITE_INTEGER),
            (120, 120),
        )
        for reported, expected in cases:
            with self.subTest(input_tokens=repr(reported)):
                store = self.store()
                connector = ReportingConnector(
                    maximum_usd=0.20,
                    monetary_cost_usd=0.05,
                    input_tokens=reported,
                    output_tokens=reported,
                )
                run = self.core(connector, store).run(make_task())
                # Billing policy decides the outcome, never a persistence
                # exception: the run completes and the count is normalized.
                self.assertEqual(RunStatus.COMPLETED, run.status)
                record = [
                    item
                    for item in self.store().usage_records()
                    if item.task_id == run.task.id
                ][0]
                self.assertEqual(expected, record.input_tokens)
                self.assertEqual(expected, record.output_tokens)
                self.assertAlmostEqual(0.05, record.monetary_cost_usd)

    def test_persistable_count_matrix(self) -> None:
        for value, expected in (
            (0, 0),
            (7, 7),
            (MAX_SQLITE_INTEGER, MAX_SQLITE_INTEGER),
            (MAX_SQLITE_INTEGER + 1, 0),
            (10**100, 0),
            (-1, 0),
            (2.0, 2),
            (2.5, 0),
            (float("nan"), 0),
            (float("inf"), 0),
            (True, 0),
            (False, 0),
            (None, 0),
            ("9", 0),
        ):
            with self.subTest(value=repr(value)):
                self.assertEqual(expected, persistable_count(value))


class _ForeignRoute(Enum):
    """A different enum whose members mimic BillingClass values."""

    METERED_MONETARY = "metered_monetary"


class _DuckTypedRoute:
    """An object that merely quacks like an enum member."""

    value = "metered_monetary"

    def __eq__(self, other: object) -> bool:  # pragma: no cover - defensive
        return True

    def __hash__(self) -> int:  # pragma: no cover - defensive
        return hash("metered_monetary")


class MalformedEstimateConnector(DummyConnector):
    """Returns an estimate whose billing class is not a BillingClass member."""

    def __init__(self, route: object, *, name: str = "malformed-route") -> None:
        super().__init__(name=name, billing_class=BillingClass.METERED_MONETARY)
        self._route = route

    def estimate_cost(self, task: Task) -> CostEstimate:
        estimate = CostEstimate(
            provider="test-provider",
            minimum_usd=0.0,
            maximum_usd=0.10,
            confidence="test",
            billing_evidence="malformed_route",
        )
        # Frozen dataclass: bypass __init__ the way externally constructed or
        # deserialized runtime state could.
        object.__setattr__(estimate, "billing_class", self._route)
        return estimate


class NotAnEstimateConnector(DummyConnector):
    """Returns something that is not a CostEstimate at all."""

    def __init__(self, payload: object, *, name: str = "not-an-estimate") -> None:
        super().__init__(name=name, billing_class=BillingClass.NO_ADDITIONAL_CHARGE)
        self._payload = payload

    def estimate_cost(self, task: Task):
        return self._payload


class CostEstimateExactEnumTests(BillingIntegrityTestCase):
    """A malformed estimate route is a structured failure, never a crash."""

    MALFORMED_ROUTES = (
        "metered_monetary",
        "no_additional_charge",
        7,
        0,
        True,
        None,
        _ForeignRoute.METERED_MONETARY,
        MonetaryState.MEASURED,
        _DuckTypedRoute(),
    )

    def test_malformed_estimate_routes_fail_closed(self) -> None:
        for route in self.MALFORMED_ROUTES:
            with self.subTest(route=repr(route)):
                store = self.store()
                connector = MalformedEstimateConnector(route)
                run = self.core(connector, store).run(make_task())
                # Deterministic structured failure through the AgentCore flow,
                # not an AttributeError/TypeError escaping as an internal error.
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(
                    ErrorCode.BILLING_INTEGRITY_FAILED, run.error.code
                )
                self.assertEqual(0, connector.execute_calls)
                self.assertEqual([], self.reservations(run.task.id))
                self.assertEqual([], self.store().usage_records())
                self.assertEqual([], self.store().list_decisions(run.session.id))
                # No approval or execution opportunity was opened.
                self.assertIsNone(run.permission_request)
                self.assertIsNone(
                    self.store().load_pending_approval(
                        run.task.id, run.session.id
                    )
                )
                events = self.events(run.session.id)
                self.assertNotIn("approval_requested", events)
                self.assertNotIn("task_completed", events)
                # The malformed route was never adopted onto the task.
                self.assertIs(BillingClass.UNKNOWN, run.task.estimate.billing_class)

    def test_a_non_estimate_return_value_fails_closed(self) -> None:
        # A connector that does not even return a ``CostEstimate`` is caught at
        # the earlier connector-selection boundary, which is equally fail-closed
        # (structured error, nothing claimed, nothing executed). The monetary
        # boundary still refuses it independently - see
        # ``test_validate_cost_estimate_requires_the_exact_enum``.
        for payload in ({"billing_class": "metered_monetary"}, "free", 5, None):
            with self.subTest(payload=repr(payload)):
                store = self.store()
                connector = NotAnEstimateConnector(payload)
                run = self.core(connector, store).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertIn(
                    run.error.code,
                    (
                        ErrorCode.BILLING_INTEGRITY_FAILED,
                        ErrorCode.NO_CONNECTOR,
                    ),
                )
                self.assertEqual(0, connector.execute_calls)
                self.assertEqual([], self.reservations(run.task.id))
                self.assertEqual([], self.store().usage_records())

    def test_the_malformed_estimate_still_renders_a_clean_envelope(self) -> None:
        connector = MalformedEstimateConnector("metered_monetary")
        run = self.core(connector, self.store()).run(make_task())
        envelope = build_envelope("run", PermissionMode.SESSION, run)
        # Presentation never dereferences an unvalidated enum.
        self.assertEqual(BillingClass.UNKNOWN.value, envelope["billing"]["class"])
        self.assertEqual(
            ErrorCode.BILLING_INTEGRITY_FAILED.value, envelope["error"]["code"]
        )
        rendered = CommandResult(envelope, EXIT_POLICY).render()
        self.assertIn("billing_integrity_failed", rendered)

    def test_valid_enum_routes_are_unaffected(self) -> None:
        for route, status in (
            (BillingClass.NO_ADDITIONAL_CHARGE, RunStatus.COMPLETED),
            (BillingClass.SUBSCRIPTION_INCLUDED, RunStatus.COMPLETED),
            (BillingClass.METERED_MONETARY, RunStatus.COMPLETED),
        ):
            with self.subTest(route=route.value):
                store = self.store()
                connector = ReportingConnector(
                    route=route,
                    maximum_usd=0.10,
                    monetary_state=(
                        MonetaryState.MEASURED
                        if route is BillingClass.METERED_MONETARY
                        else MonetaryState.NOT_APPLICABLE
                    ),
                    monetary_cost_usd=(
                        0.05 if route is BillingClass.METERED_MONETARY else None
                    ),
                )
                run = self.core(connector, store).run(make_task())
                self.assertEqual(status, run.status)
                self.assertIs(route, run.task.estimate.billing_class)
                self.assertEqual(1, connector.execute_calls)

    def test_validate_cost_estimate_requires_the_exact_enum(self) -> None:
        for route in self.MALFORMED_ROUTES:
            with self.subTest(route=repr(route)):
                estimate = CostEstimate(maximum_usd=0.10)
                object.__setattr__(estimate, "billing_class", route)
                with self.assertRaises(BillingIntegrityError):
                    validate_cost_estimate(estimate)
        for payload in ({"a": 1}, "x", 5, None):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(BillingIntegrityError):
                    validate_cost_estimate(payload)
        # A well-formed estimate still validates.
        validate_cost_estimate(
            CostEstimate(
                maximum_usd=0.10, billing_class=BillingClass.METERED_MONETARY
            )
        )


if __name__ == "__main__":
    unittest.main()
