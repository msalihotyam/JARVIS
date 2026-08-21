"""Adversarial tests for the provider-neutral billing/cost policy.

No test in this module starts a real external model call. The local
``claude auth status --json`` probe is mocked; the forbidden ``claude -p``
model process is asserted to never start.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.connectors.claude_code import (
    PROHIBITED_BILLING_ENV_VARS,
    ClaudeCodeConnector,
)
from jarvis.contracts import (
    ActionScope,
    BillingClass,
    CostEstimate,
    DataClassification,
    ErrorCode,
    LoopStage,
    MonetaryState,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    RunStatus,
    SessionState,
    Task,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
    utc_now,
)
from jarvis.core import AgentCore
from jarvis.memory import SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task, safe_cleanup
from tests.test_claude_connector import (
    FakeProcess,
    OAUTH_MAX_AUTH_STATUS,
    patch_oauth_environment,
)


SMOKE_TEST_CEILING_USD = 0.10
FAKE_API_KEY = "sk-test-must-never-leak-0123456789"


def _passing_verification() -> VerificationRecord:
    return VerificationRecord(
        status=VerificationStatus.PASSED,
        method="mock_independent_check",
        evidence=("deterministic-test-evidence",),
    )


class UnclassifiedConnector(BaseConnector):
    """A connector whose author forgot to declare a billing route."""

    def __init__(self, name: str = "unclassified") -> None:
        self.manifest = ConnectorManifest(
            name=name, capabilities=("echo",), priority=10
        )
        self.execute_calls = 0

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        self.execute_calls += 1
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=task.objective,
            data={"next_step": "should never happen"},
            usage=UsageRecord(connector=self.manifest.name),
        )

    def verify(self, task, result, session) -> VerificationRecord:
        return _passing_verification()


class SwitchingRouteConnector(BaseConnector):
    """Deterministic connector whose billing route changes once.

    The first ``switch_after`` classifications are subscription-included; every
    later one is metered. Nothing here is provider-specific.
    """

    def __init__(
        self,
        switch_after: int,
        maximum_usd: float = 0.10,
        requires_paid_api: bool = False,
        measured_cost_usd: float = 0.05,
    ) -> None:
        self.manifest = ConnectorManifest(
            name="switching",
            capabilities=("echo",),
            priority=10,
            default_billing_class=BillingClass.SUBSCRIPTION_INCLUDED,
        )
        self.switch_after = switch_after
        self.maximum_usd = maximum_usd
        self.requires_paid_api = requires_paid_api
        self.measured_cost_usd = measured_cost_usd
        self.estimate_calls = 0
        self.execute_calls = 0

    def estimate_cost(self, task: Task) -> CostEstimate:
        self.estimate_calls += 1
        if self.estimate_calls <= self.switch_after:
            return CostEstimate(
                provider="mock",
                confidence="test",
                billing_class=BillingClass.SUBSCRIPTION_INCLUDED,
                billing_evidence="mock:subscription",
                resource_ceiling_usd=SMOKE_TEST_CEILING_USD,
            )
        return CostEstimate(
            provider="mock",
            maximum_usd=self.maximum_usd,
            confidence="test",
            billing_class=BillingClass.METERED_MONETARY,
            billing_evidence="mock:metered",
            requires_paid_api=self.requires_paid_api,
            resource_ceiling_usd=SMOKE_TEST_CEILING_USD,
        )

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        self.execute_calls += 1
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=task.objective,
            data={"next_step": "continue testing"},
            usage=UsageRecord(
                connector=self.manifest.name,
                billing_class=BillingClass.METERED_MONETARY,
                monetary_state=MonetaryState.MEASURED,
                monetary_cost_usd=self.measured_cost_usd,
                call_count=1,
            ),
        )

    def verify(self, task, result, session) -> VerificationRecord:
        return _passing_verification()


def cli_payload(total_cost_usd: float | None = None) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Read-only plan ready",
        "usage": {"input_tokens": 120, "output_tokens": 45},
        "modelUsage": {"claude-test": {}},
    }
    if total_cost_usd is not None:
        payload["total_cost_usd"] = total_cost_usd
    return json.dumps(payload)


class BillingPolicyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.memory = SQLiteMemoryStore(self.root / "jarvis.db")
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=SMOKE_TEST_CEILING_USD,
        )
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)

    def core(self, *connectors) -> AgentCore:
        registry = ToolRegistry()
        for connector in connectors or (self.connector,):
            registry.register(connector)
        return AgentCore(
            registry,
            self.memory,
            Settings(
                memory_database=self.memory.database_path,
                permission_mode=PermissionMode.SESSION,
            ),
        )

    def claude_task(self) -> Task:
        return Task(
            objective="Summarize the module layout without changing files",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )

    def approve(self, core: AgentCore, run):
        """Grant the pending approval and continue the same task."""
        return core.resume(
            run.session.id,
            run.task.id,
            PermissionGrant.for_request(run.permission_request),
        )

    @staticmethod
    def model_calls(popen) -> list:
        return [call for call in popen.call_args_list if "-p" in call.args[0]]


class MaxSubscriptionBillingTests(BillingPolicyTestCase):
    """Test A - Claude Max/OAuth while the provider reports a USD value."""

    @patch.object(
        ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
    )
    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_reported_usd_never_touches_the_monetary_ledger(
        self, popen, auth_status
    ) -> None:
        popen.return_value = FakeProcess(cli_payload(total_cost_usd=0.0431))
        core = self.core()

        first = core.run(self.claude_task())
        # External egress approval is still mandatory for subscription usage.
        self.assertEqual(RunStatus.AWAITING_APPROVAL, first.status)
        self.assertEqual(
            "anthropic/claude-code", first.permission_request.scope.destination
        )
        self.assertIsNotNone(first.permission_request.outbound_preview)
        self.assertEqual(0, len(self.model_calls(popen)))

        run = self.approve(core, first)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, len(self.model_calls(popen)))

        self.assertEqual(
            BillingClass.SUBSCRIPTION_INCLUDED, run.task.estimate.billing_class
        )
        self.assertFalse(run.task.estimate.requires_paid_api)
        self.assertEqual(
            "claude_auth_status:oauth_subscription",
            run.task.estimate.billing_evidence,
        )

        usage = self.memory.usage_records()[0]
        self.assertEqual(BillingClass.SUBSCRIPTION_INCLUDED, usage.billing_class)
        self.assertEqual(MonetaryState.NOT_APPLICABLE, usage.monetary_state)
        self.assertIsNone(usage.monetary_cost_usd)
        # The provider figure survives as reference telemetry only.
        self.assertAlmostEqual(0.0431, usage.reference_cost_usd)
        self.assertEqual(120, usage.input_tokens)
        self.assertEqual(45, usage.output_tokens)
        self.assertEqual(1, usage.call_count)
        self.assertEqual("anthropic", usage.provider)
        self.assertEqual(0.0, self.memory.monthly_monetary_cost())

    @patch.object(
        ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
    )
    def test_route_is_classified_before_execution(self, auth_status) -> None:
        estimate = self.connector.estimate_cost(self.claude_task())
        self.assertEqual(BillingClass.SUBSCRIPTION_INCLUDED, estimate.billing_class)
        self.assertEqual(0.0, estimate.monetary_maximum_usd)


class ApprovedPayloadIntegrityTests(BillingPolicyTestCase):
    """What the user approved is byte-for-byte what the CLI receives."""

    @staticmethod
    def outbound_json(prompt: str) -> dict:
        return json.loads(prompt.split("\n", 1)[1])

    @patch.object(
        ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
    )
    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_executed_prompt_equals_the_approved_prompt(self, popen, auth_status) -> None:
        popen.return_value = FakeProcess(cli_payload(total_cost_usd=0.0))
        core = self.core()

        first = core.run(self.claude_task())
        self.assertEqual(RunStatus.AWAITING_APPROVAL, first.status)
        approved_prompt, _ = self.connector._build_prompt(first.task)
        approved_payload, _ = self.connector._sanitized_payload(first.task)
        self.assertNotIn("billing_preflight_fingerprint", first.task.preferences)

        run = self.approve(core, first)
        self.assertEqual(RunStatus.COMPLETED, run.status)

        model_calls = self.model_calls(popen)
        self.assertEqual(1, len(model_calls))
        command = model_calls[0].args[0]
        executed_prompt = command[command.index("-p") + 1]

        # The exact bytes the user approved are the exact bytes that were sent.
        self.assertEqual(approved_prompt, executed_prompt)
        executed_payload = self.outbound_json(executed_prompt)
        self.assertEqual(approved_payload, executed_payload)

        for key in (
            "billing_preflight_fingerprint",
            "billing_repreflight_attempts",
            "paid_api_task_approval",
            "cost_cap_override",
        ):
            with self.subTest(preference=key):
                self.assertNotIn(key, executed_payload["preferences"])
                self.assertNotIn(key, executed_prompt)

        # The internal marker is still persisted locally for execution safety.
        persisted = self.memory.load_task(run.task.id)
        self.assertIn("billing_preflight_fingerprint", persisted.preferences)
        self.assertEqual(0.0, self.memory.monthly_monetary_cost())

    @patch.object(
        ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
    )
    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_user_clarification_reaches_the_approved_payload(
        self, popen, auth_status
    ) -> None:
        popen.return_value = FakeProcess(cli_payload(total_cost_usd=0.0))
        clarification = "Use option B and inspect only the approved fixture."
        core = self.core()

        task = self.claude_task()
        task.preferences = {
            "needs_clarification": True,
            "clarification_question": "Which option should the plan assume?",
        }
        asked = core.run(task)
        self.assertEqual(RunStatus.AWAITING_CLARIFICATION, asked.status)
        self.assertEqual(0, len(self.model_calls(popen)))

        answered = core.provide_clarification(
            asked.session.id, asked.task.id, clarification
        )
        self.assertEqual(RunStatus.AWAITING_APPROVAL, answered.status)
        self.assertEqual(clarification, answered.task.visible_assumption)
        self.assertEqual(0, len(self.model_calls(popen)))

        approved_payload, _ = self.connector._sanitized_payload(answered.task)
        approved_prompt, _ = self.connector._build_prompt(answered.task)
        self.assertEqual(clarification, approved_payload["visible_assumption"])
        self.assertNotIn("clarification", approved_payload["preferences"])
        self.assertIn(
            "visible_assumption",
            answered.permission_request.outbound_preview.field_names,
        )

        run = core.resume(
            answered.session.id,
            answered.task.id,
            PermissionGrant.for_request(
                answered.permission_request, remember_for_session=False
            ),
        )
        self.assertEqual(RunStatus.COMPLETED, run.status)

        model_calls = self.model_calls(popen)
        self.assertEqual(1, len(model_calls))
        command = model_calls[0].args[0]
        executed_prompt = command[command.index("-p") + 1]

        self.assertEqual(approved_prompt, executed_prompt)
        executed_payload = self.outbound_json(executed_prompt)
        self.assertEqual(clarification, executed_payload["visible_assumption"])
        for key in (
            "billing_preflight_fingerprint",
            "billing_repreflight_attempts",
            "paid_api_task_approval",
            "cost_cap_override",
            "needs_clarification",
            "clarification_question",
            "clarification",
            "assumption",
        ):
            with self.subTest(preference=key):
                self.assertNotIn(key, executed_payload["preferences"])
                self.assertNotIn(f'"{key}"', executed_prompt)

        persisted = self.memory.load_task(run.task.id)
        self.assertIn("billing_preflight_fingerprint", persisted.preferences)
        self.assertEqual(clarification, persisted.preferences["clarification"])
        self.assertEqual(0.0, self.memory.monthly_monetary_cost())


class ApiKeyRouteTests(BillingPolicyTestCase):
    """Test B - a metered credential in the environment blocks execution."""

    @patch.object(
        ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
    )
    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_api_key_blocks_before_any_model_process(self, popen, auth_status) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": FAKE_API_KEY}):
            billing_class, evidence = self.connector.billing_route()
            self.assertEqual(BillingClass.UNKNOWN, billing_class)
            self.assertEqual(
                "claude_env:prohibited_metered_credential_present", evidence
            )

            core = self.core()
            run = core.run(self.claude_task())

        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_ROUTE_UNKNOWN, run.error.code)
        popen.assert_not_called()
        self.assertEqual(0, len(self.model_calls(popen)))

        # The credential value is never surfaced or persisted anywhere.
        serialized = json.dumps(
            [
                run.error.to_dict(),
                run.task.to_dict(),
                [record.to_dict() for record in self.memory.usage_records()],
                self.memory.list_events(run.session.id),
            ],
            default=str,
        )
        self.assertNotIn(FAKE_API_KEY, serialized)
        self.assertEqual(0.0, self.memory.monthly_monetary_cost())

    @patch.object(
        ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
    )
    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_connector_execute_refuses_directly(self, popen, auth_status) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": FAKE_API_KEY}):
            result = self.connector.execute(self.claude_task(), SessionState())
        self.assertEqual(ErrorCode.BILLING_ROUTE_UNKNOWN, result.error.code)
        self.assertEqual(0, result.usage.call_count)
        popen.assert_not_called()

    def test_model_subprocess_environment_is_sanitized(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": FAKE_API_KEY}):
            environment = self.connector._sanitized_environment()
        self.assertNotIn("ANTHROPIC_API_KEY", environment)


class UnknownRouteTests(BillingPolicyTestCase):
    """Test C - an unestablished billing route fails closed."""

    @patch("jarvis.connectors.claude_code.subprocess.run")
    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_failed_auth_probe_stops_before_execution(self, popen, run_probe) -> None:
        # Permitted local probe: present, but unusable.
        run_probe.return_value = type(
            "Completed", (), {"returncode": 1, "stdout": "", "stderr": ""}
        )()
        core = self.core()
        run = core.run(self.claude_task())

        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_ROUTE_UNKNOWN, run.error.code)
        self.assertEqual(
            "claude_auth_status:unavailable", run.error.details["billing_evidence"]
        )
        run_probe.assert_called()
        popen.assert_not_called()
        self.assertEqual(0, len(self.model_calls(popen)))

    @patch("jarvis.connectors.claude_code.subprocess.run")
    def test_unknown_monetary_state_is_not_a_zero(self, run_probe) -> None:
        run_probe.return_value = type(
            "Completed", (), {"returncode": 0, "stdout": "{not json", "stderr": ""}
        )()
        core = self.core()
        run = core.run(self.claude_task())

        self.assertEqual(RunStatus.FAILED, run.status)
        usage = run.result.usage
        self.assertEqual(BillingClass.UNKNOWN, usage.billing_class)
        self.assertEqual(MonetaryState.UNKNOWN, usage.monetary_state)
        self.assertIsNone(usage.monetary_cost_usd)
        self.assertFalse(usage.counts_toward_monetary_budget)

    @patch("jarvis.connectors.claude_code.subprocess.run")
    def test_ambiguous_auth_evidence_is_unknown(self, run_probe) -> None:
        for status, expected in (
            ({"loggedIn": False}, "claude_auth_status:not_logged_in"),
            (
                {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"},
                "claude_auth_status:unsupported_auth_route",
            ),
            (
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                },
                "claude_auth_status:no_subscription_evidence",
            ),
        ):
            with self.subTest(status=expected):
                run_probe.return_value = type(
                    "Completed",
                    (),
                    {"returncode": 0, "stdout": json.dumps(status), "stderr": ""},
                )()
                billing_class, evidence = self.connector.billing_route()
                self.assertEqual(BillingClass.UNKNOWN, billing_class)
                self.assertEqual(expected, evidence)


class ResourceCeilingTests(BillingPolicyTestCase):
    """Test D - max_budget_usd is a CLI safety ceiling, not projected spend."""

    @patch.object(
        ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
    )
    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_ceiling_does_not_consume_the_monthly_budget(
        self, popen, auth_status
    ) -> None:
        popen.return_value = FakeProcess(cli_payload(total_cost_usd=0.0))
        # Almost the entire monthly budget is already spent on metered usage.
        self.memory.append_usage(
            UsageRecord(
                billing_class=BillingClass.METERED_MONETARY,
                monetary_state=MonetaryState.MEASURED,
                monetary_cost_usd=19.95,
            )
        )
        core = self.core()
        run = self.approve(core, core.run(self.claude_task()))

        # 19.95 + 0.10 would exceed 20 USD if the ceiling were money.
        self.assertEqual(RunStatus.COMPLETED, run.status)
        estimate = run.task.estimate
        self.assertEqual(SMOKE_TEST_CEILING_USD, estimate.resource_ceiling_usd)
        self.assertEqual(0.0, estimate.maximum_usd)
        self.assertEqual(0.0, estimate.monetary_maximum_usd)
        self.assertAlmostEqual(19.95, self.memory.monthly_monetary_cost())

    def test_ceiling_is_still_passed_to_the_cli(self) -> None:
        command = self.connector._command("safe prompt")
        self.assertIn("--max-budget-usd", command)
        self.assertIn(f"{SMOKE_TEST_CEILING_USD:.4f}", command)


class MeteredRouteTests(BillingPolicyTestCase):
    """Test E - genuinely metered usage keeps the full monetary policy."""

    def test_paid_api_switch_still_gates_a_metered_route(self) -> None:
        connector = DummyConnector(
            name="metered", estimate_max=0.10, is_paid=True, actual_cost=0.05
        )
        core = self.core(connector)

        paid_gate = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_APPROVAL, paid_gate.status)
        self.assertEqual(
            "enable_paid_api_for_task", paid_gate.permission_request.scope.action
        )
        self.assertEqual(0, connector.execute_calls)

        run = self.approve(core, paid_gate)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)
        self.assertAlmostEqual(0.05, self.memory.monthly_monetary_cost())

    def test_hard_limit_still_blocks_a_metered_projection(self) -> None:
        self.memory.append_usage(
            UsageRecord(
                billing_class=BillingClass.METERED_MONETARY,
                monetary_state=MonetaryState.MEASURED,
                monetary_cost_usd=19.90,
            )
        )
        connector = DummyConnector(name="metered", estimate_max=0.20, actual_cost=0.15)
        core = self.core(connector)

        cap_gate = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, cap_gate.status)
        self.assertEqual(
            "override_monthly_cost_cap", cap_gate.permission_request.scope.action
        )
        self.assertEqual(0, connector.execute_calls)

        run = self.approve(core, cap_gate)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)
        self.assertAlmostEqual(20.05, self.memory.monthly_monetary_cost())

    def test_metered_usage_is_recorded_as_measured_money(self) -> None:
        connector = DummyConnector(
            name="metered", estimate_max=0.10, actual_cost=0.02
        )
        run = self.core(connector).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        usage = self.memory.usage_records()[0]
        self.assertEqual(BillingClass.METERED_MONETARY, usage.billing_class)
        self.assertEqual(MonetaryState.MEASURED, usage.monetary_state)
        self.assertAlmostEqual(0.02, usage.monetary_cost_usd)
        self.assertAlmostEqual(0.02, self.memory.monthly_monetary_cost())


class PersistedResumeBillingTests(BillingPolicyTestCase):
    """A persisted late-stage task may not bypass billing classification."""

    def persisted_at(self, connector, stage: LoopStage):
        core = self.core(connector)
        session = core.start_session()
        task = make_task()
        task.selected_connector = connector.manifest.name
        task.stage = stage
        self.memory.save_task(session.id, task)
        return core, session, task

    def assert_failed_closed(self, run, connector) -> None:
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_ROUTE_UNKNOWN, run.error.code)
        self.assertEqual(0, connector.execute_calls)
        self.assertEqual([], self.memory.usage_records())
        self.assertEqual(0.0, self.memory.monthly_monetary_cost())

    def test_resume_at_execute_cannot_bypass_billing_preflight(self) -> None:
        connector = UnclassifiedConnector()
        core, session, task = self.persisted_at(connector, LoopStage.EXECUTE)
        self.assert_failed_closed(core.resume(session.id, task.id), connector)

    def test_resume_at_tool_selection_cannot_bypass_billing_preflight(self) -> None:
        connector = UnclassifiedConnector()
        core, session, task = self.persisted_at(connector, LoopStage.TOOL_SELECTION)
        self.assert_failed_closed(core.resume(session.id, task.id), connector)

    def test_resume_at_execute_ignores_a_stale_persisted_estimate(self) -> None:
        """A friendly-looking stored estimate does not authorize execution."""
        connector = UnclassifiedConnector()
        core = self.core(connector)
        session = core.start_session()
        task = make_task()
        task.selected_connector = connector.manifest.name
        task.stage = LoopStage.EXECUTE
        # Forged: a free classification that never passed cost preflight.
        task.estimate = CostEstimate(
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            billing_evidence="stale_persisted_value",
        )
        self.memory.save_task(session.id, task)
        self.assert_failed_closed(core.resume(session.id, task.id), connector)

    def test_route_change_after_preflight_reenters_monetary_approval(self) -> None:
        # Subscription for select/risk/preflight, metered from the execute gate.
        connector = SwitchingRouteConnector(switch_after=3)
        self.memory.append_usage(
            UsageRecord(
                billing_class=BillingClass.METERED_MONETARY,
                monetary_state=MonetaryState.MEASURED,
                monetary_cost_usd=19.95,
            )
        )
        core = self.core(connector)

        blocked = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, blocked.status)
        self.assertEqual(
            "override_monthly_cost_cap", blocked.permission_request.scope.action
        )
        self.assertEqual(0, connector.execute_calls)
        events = {event["event_type"] for event in self.memory.list_events(blocked.session.id)}
        self.assertIn("billing_preflight_required", events)

        run = self.approve(core, blocked)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)
        self.assertEqual(BillingClass.METERED_MONETARY, run.task.estimate.billing_class)
        self.assertAlmostEqual(20.00, self.memory.monthly_monetary_cost())


class UnclassifiedConnectorTests(BillingPolicyTestCase):
    """A connector that declares nothing stays UNKNOWN and fails closed."""

    def test_missing_billing_class_defaults_to_unknown(self) -> None:
        connector = UnclassifiedConnector()
        self.assertEqual(
            BillingClass.UNKNOWN, connector.manifest.default_billing_class
        )
        estimate = connector.estimate_cost(make_task())
        self.assertEqual(BillingClass.UNKNOWN, estimate.billing_class)

    def test_agent_core_refuses_an_unclassified_connector(self) -> None:
        connector = UnclassifiedConnector()
        run = self.core(connector).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_ROUTE_UNKNOWN, run.error.code)
        self.assertEqual(0, connector.execute_calls)
        self.assertEqual(0.0, self.memory.monthly_monetary_cost())


class SequentialMonetaryGateTests(BillingPolicyTestCase):
    """Paid-API approval must not skip the monthly hard-cap gate."""

    def test_paid_api_then_hard_cap_then_execution(self) -> None:
        self.memory.append_usage(
            UsageRecord(
                billing_class=BillingClass.METERED_MONETARY,
                monetary_state=MonetaryState.MEASURED,
                monetary_cost_usd=19.95,
            )
        )
        connector = DummyConnector(
            name="metered", estimate_max=0.10, is_paid=True, actual_cost=0.10
        )
        core = self.core(connector)

        paid_gate = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_APPROVAL, paid_gate.status)
        self.assertEqual(
            "enable_paid_api_for_task", paid_gate.permission_request.scope.action
        )
        self.assertEqual(0, connector.execute_calls)

        cap_gate = self.approve(core, paid_gate)
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, cap_gate.status)
        self.assertEqual(
            "override_monthly_cost_cap", cap_gate.permission_request.scope.action
        )
        self.assertEqual(0, connector.execute_calls)
        # A second, distinct request: one grant never satisfies another.
        self.assertNotEqual(
            paid_gate.permission_request.id, cap_gate.permission_request.id
        )

        run = self.approve(core, cap_gate)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)
        self.assertAlmostEqual(20.05, self.memory.monthly_monetary_cost())

        grants = self.recorded_grants()
        self.assertEqual(2, len(grants))
        self.assertEqual(
            {paid_gate.permission_request.id, cap_gate.permission_request.id},
            {grant["request_id"] for grant in grants},
        )
        for grant in grants:
            self.assertEqual(run.task.id, grant["task_id"])
            self.assertEqual(run.session.id, grant["session_id"])
            self.assertEqual(1, grant["approved"])

    def recorded_grants(self) -> list[dict]:
        connection = sqlite3.connect(self.memory.database_path)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT request_id, task_id, session_id, approved, fingerprint "
                "FROM permission_grants ORDER BY created_at"
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]


class ProhibitedEnvironmentPresenceTests(BillingPolicyTestCase):
    """Presence blocks, empty or not; the value is never inspected."""

    @patch.object(ClaudeCodeConnector, "_auth_status")
    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_empty_api_key_still_blocks(self, popen, auth_status) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.assertIn("ANTHROPIC_API_KEY", self.connector.prohibited_billing_env_vars())
            billing_class, evidence = self.connector.billing_route()
            self.assertEqual(BillingClass.UNKNOWN, billing_class)
            self.assertEqual(
                "claude_env:prohibited_metered_credential_present", evidence
            )

            core = self.core()
            run = core.run(self.claude_task())

        # The prohibited variable is detected before the auth probe runs.
        auth_status.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.BILLING_ROUTE_UNKNOWN, run.error.code)

        serialized = json.dumps(
            [
                run.error.to_dict(),
                run.task.to_dict(),
                [record.to_dict() for record in self.memory.usage_records()],
                self.memory.list_events(run.session.id),
            ],
            default=str,
        )
        self.assertNotIn("ANTHROPIC_API_KEY", serialized)
        self.assertEqual(0.0, self.memory.monthly_monetary_cost())

    def test_every_prohibited_variable_blocks_when_empty(self) -> None:
        for name in PROHIBITED_BILLING_ENV_VARS:
            with self.subTest(variable=name), patch.dict("os.environ", {name: ""}):
                self.assertEqual(
                    (name,), self.connector.prohibited_billing_env_vars()
                )
                self.assertEqual(
                    BillingClass.UNKNOWN, self.connector.billing_route()[0]
                )

    def test_detection_and_sanitization_ignore_case(self) -> None:
        with patch.dict("os.environ", {"anthropic_api_key": FAKE_API_KEY}):
            self.assertIn(
                "ANTHROPIC_API_KEY", self.connector.prohibited_billing_env_vars()
            )
            environment = self.connector._sanitized_environment()
        self.assertEqual(
            [], [key for key in environment if key.upper() == "ANTHROPIC_API_KEY"]
        )


LEGACY_V2_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE usage_ledger (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    task_id TEXT,
    provider TEXT,
    model TEXT,
    connector TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    actual_cost_usd REAL NOT NULL,
    is_paid INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


class UsageLedgerMigrationTests(unittest.TestCase):
    """Schema 2 databases must upgrade additively and keep their history."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(LEGACY_V2_SCHEMA)
            connection.execute("INSERT INTO schema_version (version) VALUES (2)")
            connection.executemany(
                """
                INSERT INTO usage_ledger
                    (id, session_id, task_id, provider, model, connector,
                     input_tokens, output_tokens, estimated_cost_usd,
                     actual_cost_usd, is_paid, recorded_at)
                VALUES (?, NULL, NULL, 'legacy', 'legacy-model', 'legacy-connector',
                        ?, ?, 0.0, ?, ?, ?)
                """,
                [
                    ("paid-row", 10, 5, 0.42, 1, utc_now()),
                    ("free-row", 7, 3, 0.0, 0, utc_now()),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def test_legacy_database_upgrades_without_data_loss(self) -> None:
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(SCHEMA_VERSION, store.schema_version())

        records = {record.id: record for record in store.usage_records()}
        self.assertEqual({"paid-row", "free-row"}, set(records))
        self.assertEqual(10, records["paid-row"].input_tokens)
        self.assertEqual(3, records["free-row"].output_tokens)

        # Unambiguous legacy spend keeps counting against the hard limit.
        paid = records["paid-row"]
        self.assertEqual(BillingClass.METERED_MONETARY, paid.billing_class)
        self.assertEqual(MonetaryState.MEASURED, paid.monetary_state)
        self.assertAlmostEqual(0.42, paid.monetary_cost_usd)
        self.assertAlmostEqual(0.42, store.monthly_monetary_cost())

        # An ambiguous legacy zero is neither a measured zero nor subscription
        # usage; it stays unknown and contributes nothing.
        free = records["free-row"]
        self.assertEqual(BillingClass.UNKNOWN, free.billing_class)
        self.assertEqual(MonetaryState.UNKNOWN, free.monetary_state)
        self.assertIsNone(free.monetary_cost_usd)

    def test_migration_is_idempotent_and_keeps_working(self) -> None:
        SQLiteMemoryStore(self.database)
        store = SQLiteMemoryStore(self.database)
        self.assertEqual(SCHEMA_VERSION, store.schema_version())
        store.append_usage(
            UsageRecord(
                billing_class=BillingClass.SUBSCRIPTION_INCLUDED,
                monetary_state=MonetaryState.NOT_APPLICABLE,
                reference_cost_usd=0.05,
            )
        )
        self.assertAlmostEqual(0.42, store.monthly_monetary_cost())
        self.assertEqual(3, len(store.usage_records()))


class LegacyRecordCompatibilityTests(unittest.TestCase):
    """Persisted pre-migration payloads must not become measured zeros."""

    def test_legacy_usage_zero_stays_unknown(self) -> None:
        record = UsageRecord.from_dict(
            {"provider": "anthropic", "actual_cost_usd": 0.0, "is_paid": False}
        )
        self.assertEqual(BillingClass.UNKNOWN, record.billing_class)
        self.assertEqual(MonetaryState.UNKNOWN, record.monetary_state)
        self.assertIsNone(record.monetary_cost_usd)

    def test_legacy_usage_spend_is_preserved(self) -> None:
        record = UsageRecord.from_dict({"actual_cost_usd": 1.25, "is_paid": True})
        self.assertEqual(BillingClass.METERED_MONETARY, record.billing_class)
        self.assertEqual(MonetaryState.MEASURED, record.monetary_state)
        self.assertAlmostEqual(1.25, record.monetary_cost_usd)
        self.assertTrue(record.counts_toward_monetary_budget)


if __name__ == "__main__":
    unittest.main()
