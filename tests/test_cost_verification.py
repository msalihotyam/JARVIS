from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jarvis.contracts import (
    ErrorCode,
    LoopStage,
    PermissionGrant,
    PermissionMode,
    RunStatus,
    TaskStatus,
    UsageRecord,
)
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task


class CostAndVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def core(self, connector: DummyConnector, settings: Settings | None = None) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        return AgentCore(
            registry,
            self.memory,
            settings or Settings(memory_database=self.memory.database_path),
        )

    def test_hard_cap_blocks_until_explicit_override(self) -> None:
        self.memory.append_usage(UsageRecord(actual_cost_usd=19.90, is_paid=True))
        connector = DummyConnector(estimate_max=0.20)
        core = self.core(connector)
        first = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, first.status)
        self.assertEqual("override_monthly_cost_cap", first.permission_request.scope.action)
        self.assertEqual(0, connector.execute_calls)

        approved = core.resume(
            first.session.id,
            first.task.id,
            PermissionGrant.for_request(first.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, approved.status)
        self.assertEqual(1, connector.execute_calls)

    def test_paid_api_disabled_requires_task_specific_approval(self) -> None:
        connector = DummyConnector(estimate_max=0.10, is_paid=True)
        core = self.core(connector)
        first = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_APPROVAL, first.status)
        self.assertEqual("enable_paid_api_for_task", first.permission_request.scope.action)

        approved = core.resume(
            first.session.id,
            first.task.id,
            PermissionGrant.for_request(first.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, approved.status)

    def test_paid_api_can_be_enabled_only_with_approval_id(self) -> None:
        settings = Settings(memory_database=self.memory.database_path)
        with self.assertRaises(ValueError):
            Settings(
                memory_database=self.memory.database_path,
                paid_api_enabled=True,
            )
        enabled = settings.enable_paid_api("user-decision-123")
        self.assertTrue(enabled.paid_api_enabled)
        self.assertEqual("user-decision-123", enabled.paid_api_approval_id)

    def test_actual_usage_is_persisted_and_aggregated(self) -> None:
        connector = DummyConnector(actual_cost=0.07)
        run = self.core(connector).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertAlmostEqual(0.07, self.memory.monthly_actual_cost())
        records = self.memory.usage_records()
        self.assertEqual(1, len(records))
        self.assertEqual(run.task.id, records[0].task_id)

    def test_connector_failure_usage_is_not_lost(self) -> None:
        from jarvis.contracts import OutcomeStatus

        connector = DummyConnector(actual_cost=0.05, outcome=OutcomeStatus.ERROR)
        run = self.core(connector).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertAlmostEqual(0.05, self.memory.monthly_actual_cost())

    def test_verification_failure_prevents_completion_and_decision(self) -> None:
        connector = DummyConnector(verification_passes=False)
        run = self.core(connector).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.VERIFICATION_FAILED, run.error.code)
        self.assertEqual([], self.memory.list_decisions(run.session.id))

    def test_cancellation_is_persisted(self) -> None:
        connector = DummyConnector()
        core = self.core(connector)
        session = core.start_session()
        task = make_task()
        task.selected_connector = connector.manifest.name
        task.stage = LoopStage.EXECUTE
        task.status = TaskStatus.RUNNING
        self.memory.save_task(session.id, task)

        cancelled = core.cancel(session.id, task.id)
        self.assertEqual(RunStatus.CANCELLED, cancelled.status)
        self.assertEqual(1, connector.cancel_calls)
        restored = self.memory.load_task(task.id)
        self.assertEqual(TaskStatus.CANCELLED, restored.status)


if __name__ == "__main__":
    unittest.main()
