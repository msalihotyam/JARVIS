from __future__ import annotations

import os
import re
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.contracts import (
    ActionScope,
    ErrorCode,
    LoopStage,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    RiskLevel,
    RunStatus,
    SideEffect,
    SideEffectProfile,
)
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task


class PermissionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def core(self, connector: DummyConnector, mode: PermissionMode) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        return AgentCore(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path, permission_mode=mode),
        )

    def test_strict_requires_approval_even_for_low_reversible(self) -> None:
        connector = DummyConnector()
        run = self.core(connector, PermissionMode.STRICT).run(make_task())
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertEqual(0, connector.execute_calls)

    def test_session_auto_runs_low_reversible(self) -> None:
        connector = DummyConnector()
        run = self.core(connector, PermissionMode.SESSION).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)

    def test_session_requires_medium_then_remembers_exact_scope(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        core = self.core(connector, PermissionMode.SESSION)
        session = core.start_session("permission test")
        first = core.run(make_task(risk=RiskLevel.MEDIUM), session.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, first.status)
        grant = PermissionGrant.for_request(
            first.permission_request, remember_for_session=True
        )
        approved = core.resume(session.id, first.task.id, grant)
        self.assertEqual(RunStatus.COMPLETED, approved.status)

        repeated = core.run(make_task(risk=RiskLevel.MEDIUM), session.id)
        self.assertEqual(RunStatus.COMPLETED, repeated.status)
        self.assertEqual(2, connector.execute_calls)

    def test_remembered_permission_does_not_cover_changed_target(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        core = self.core(connector, PermissionMode.SESSION)
        session = core.start_session()
        first = core.run(make_task(target="resource-a", risk=RiskLevel.MEDIUM), session.id)
        approved = core.resume(
            session.id,
            first.task.id,
            PermissionGrant.for_request(first.permission_request, remember_for_session=True),
        )
        self.assertEqual(RunStatus.COMPLETED, approved.status)

        changed = core.run(
            make_task(target="resource-b", risk=RiskLevel.MEDIUM), session.id
        )
        self.assertEqual(RunStatus.AWAITING_APPROVAL, changed.status)
        self.assertEqual(1, connector.execute_calls)

    def test_smart_auto_only_runs_low_reversible(self) -> None:
        cases = (
            (RiskLevel.LOW, True, RunStatus.COMPLETED),
            (RiskLevel.MEDIUM, True, RunStatus.AWAITING_APPROVAL),
            (RiskLevel.LOW, False, RunStatus.AWAITING_APPROVAL),
        )
        for index, (risk, reversible, expected) in enumerate(cases):
            with self.subTest(risk=risk, reversible=reversible):
                connector = DummyConnector(name=f"dummy-{index}", risk=risk)
                memory = SQLiteMemoryStore(Path(self.temp.name) / f"case-{index}.db")
                registry = ToolRegistry()
                registry.register(connector)
                core = AgentCore(
                    registry,
                    memory,
                    Settings(
                        memory_database=memory.database_path,
                        permission_mode=PermissionMode.SMART_AUTO,
                    ),
                )
                run = core.run(make_task(risk=risk, reversible=reversible))
                self.assertEqual(expected, run.status)

    def test_full_permission_mode_and_risk_matrix(self) -> None:
        expected = {
            PermissionMode.STRICT: {
                RiskLevel.LOW: RunStatus.AWAITING_APPROVAL,
                RiskLevel.MEDIUM: RunStatus.AWAITING_APPROVAL,
                RiskLevel.HIGH: RunStatus.AWAITING_APPROVAL,
                RiskLevel.CRITICAL: RunStatus.AWAITING_APPROVAL,
            },
            PermissionMode.SESSION: {
                RiskLevel.LOW: RunStatus.COMPLETED,
                RiskLevel.MEDIUM: RunStatus.AWAITING_APPROVAL,
                RiskLevel.HIGH: RunStatus.AWAITING_APPROVAL,
                RiskLevel.CRITICAL: RunStatus.AWAITING_APPROVAL,
            },
            PermissionMode.SMART_AUTO: {
                RiskLevel.LOW: RunStatus.COMPLETED,
                RiskLevel.MEDIUM: RunStatus.AWAITING_APPROVAL,
                RiskLevel.HIGH: RunStatus.AWAITING_APPROVAL,
                RiskLevel.CRITICAL: RunStatus.AWAITING_APPROVAL,
            },
        }
        for mode in PermissionMode:
            for risk in RiskLevel:
                with self.subTest(mode=mode, risk=risk):
                    database = Path(self.temp.name) / f"matrix-{mode.value}-{risk.name}.db"
                    memory = SQLiteMemoryStore(database)
                    connector = DummyConnector(name=f"matrix-{mode.value}-{risk.name}", risk=risk)
                    registry = ToolRegistry()
                    registry.register(connector)
                    core = AgentCore(
                        registry,
                        memory,
                        Settings(memory_database=database, permission_mode=mode),
                    )
                    run = core.run(make_task(risk=risk))
                    self.assertEqual(expected[mode][risk], run.status)

    def test_mandatory_side_effects_require_approval_in_every_mode(self) -> None:
        effects = (
            SideEffect.EXTERNAL_EGRESS,
            SideEffect.BILLING,
            SideEffect.DESTRUCTIVE,
            SideEffect.PUSH_DEPLOY_PUBLISH,
            SideEffect.FINANCIAL_OR_FORM_SUBMIT,
        )
        for mode in PermissionMode:
            for index, effect in enumerate(effects):
                with self.subTest(mode=mode, effect=effect):
                    memory = SQLiteMemoryStore(
                        Path(self.temp.name) / f"mandatory-{mode.value}-{index}.db"
                    )
                    connector = DummyConnector(
                        name=f"mandatory-{mode.value}-{index}", side_effect=effect
                    )
                    registry = ToolRegistry()
                    registry.register(connector)
                    core = AgentCore(
                        registry,
                        memory,
                        Settings(memory_database=memory.database_path, permission_mode=mode),
                    )
                    run = core.run(make_task(effect=effect))
                    self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
                    self.assertEqual(0, connector.execute_calls)

    def test_stale_grant_does_not_execute(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        core = self.core(connector, PermissionMode.SESSION)
        first = core.run(make_task(risk=RiskLevel.MEDIUM))
        stale = replace(
            PermissionGrant.for_request(first.permission_request),
            expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )
        resumed = core.resume(first.session.id, first.task.id, stale)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, resumed.status)
        self.assertEqual(ErrorCode.STALE_GRANT, resumed.error.code)
        self.assertEqual(0, connector.execute_calls)

    def test_cross_session_grant_does_not_execute(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        core = self.core(connector, PermissionMode.SESSION)
        first = core.run(make_task(risk=RiskLevel.MEDIUM))
        other_session = core.start_session()
        invalid = replace(
            PermissionGrant.for_request(first.permission_request),
            session_id=other_session.id,
        )
        resumed = core.resume(first.session.id, first.task.id, invalid)
        self.assertEqual(ErrorCode.INVALID_GRANT, resumed.error.code)
        self.assertEqual(0, connector.execute_calls)

    def test_rejected_grant_never_executes(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        core = self.core(connector, PermissionMode.SESSION)
        first = core.run(make_task(risk=RiskLevel.MEDIUM))
        rejected = PermissionGrant.for_request(first.permission_request, approved=False)
        resumed = core.resume(first.session.id, first.task.id, rejected)
        self.assertEqual(RunStatus.REJECTED, resumed.status)
        self.assertEqual(0, connector.execute_calls)

    def test_new_task_cannot_start_mid_loop_and_skip_approval(self) -> None:
        worst_case = dict(
            risk=RiskLevel.CRITICAL,
            effect=SideEffect.EXTERNAL_EGRESS,
            reversible=False,
            always_requires_approval=True,
        )
        connector = DummyConnector(
            risk=RiskLevel.CRITICAL,
            side_effect=SideEffect.EXTERNAL_EGRESS,
            reversible=False,
            always_requires_approval=True,
            estimate_max=999.0,
            is_paid=True,
        )
        core = self.core(connector, PermissionMode.SESSION)

        pre_staged = make_task(**worst_case)
        pre_staged.stage = LoopStage.EXECUTE
        pre_staged.selected_connector = connector.manifest.name
        run = core.run(pre_staged)

        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertEqual(0, connector.execute_calls)
        self.assertIsNone(self.memory.load_task(pre_staged.id))
        self.assertEqual(0.0, self.memory.monthly_monetary_cost())
        self.assertEqual([], self.memory.list_decisions(run.session.id))

        # The same worst-case task still reaches the approval gate normally.
        allowed = core.run(make_task(**worst_case))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, allowed.status)
        self.assertEqual(0, connector.execute_calls)

    def test_pending_approval_survives_core_restart(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        core = self.core(connector, PermissionMode.SESSION)
        first = core.run(make_task(risk=RiskLevel.MEDIUM))
        self.assertIsNotNone(
            self.memory.load_pending_approval(first.task.id, first.session.id)
        )

        restarted = self.core(connector, PermissionMode.SESSION)
        resumed = restarted.resume(
            first.session.id,
            first.task.id,
            PermissionGrant.for_request(first.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, resumed.status)


class ScopeNormalizationTests(unittest.TestCase):
    """A scope field is normalized by what it means, not by its punctuation."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    @staticmethod
    def request_for(destination: str) -> PermissionRequest:
        return PermissionRequest(
            task_id="task-1",
            session_id="session-1",
            scope=ActionScope(
                action="analyze",
                target="resource-a",
                connector="claude-code-plan",
                destination=destination,
            ),
            risk_level=RiskLevel.MEDIUM,
            side_effect=SideEffectProfile(effect=SideEffect.EXTERNAL_EGRESS),
            reason="normalization test",
        )

    def test_symbolic_destination_is_not_expanded_to_a_path(self) -> None:
        scope = ActionScope(action="analyze", destination="anthropic/claude-code")
        normalized = scope.normalized()
        self.assertEqual("anthropic/claude-code", normalized.destination)
        self.assertEqual("anthropic/claude-code", scope.to_dict()["destination"])
        cwd = os.path.normcase(str(Path.cwd())).replace("\\", "/")
        self.assertNotIn(cwd, normalized.destination)
        self.assertIsNone(re.match(r"^[A-Za-z]:", normalized.destination))

    def test_symbolic_action_and_connector_stay_symbolic(self) -> None:
        normalized = ActionScope(
            action="  Plan/Analyze  ",
            connector="Claude-Code-Plan",
        ).normalized()
        self.assertEqual("plan/analyze", normalized.action)
        self.assertEqual("claude-code-plan", normalized.connector)

    def test_symbolic_destination_fingerprint_is_working_directory_independent(
        self,
    ) -> None:
        before = self.request_for("anthropic/claude-code").scope_fingerprint
        elsewhere = self.root / "çalışma-ü"
        elsewhere.mkdir()
        original = Path.cwd()
        os.chdir(elsewhere)
        try:
            inside = self.request_for("anthropic/claude-code")
            self.assertEqual("anthropic/claude-code", inside.scope.normalized().destination)
            self.assertEqual(before, inside.scope_fingerprint)
        finally:
            os.chdir(original)
        self.assertEqual(before, self.request_for("anthropic/claude-code").scope_fingerprint)

    def test_changing_a_symbolic_destination_changes_the_fingerprint(self) -> None:
        self.assertNotEqual(
            self.request_for("anthropic/claude-code").scope_fingerprint,
            self.request_for("anthropic/other-endpoint").scope_fingerprint,
        )

    def test_filesystem_target_keeps_canonical_path_normalization(self) -> None:
        (self.root / "sub").mkdir()
        mixed = f"{self.root}\\sub\\..\\sub\\file.txt"
        forward = f"{str(self.root).replace(chr(92), '/')}/sub/file.txt"
        normalized = ActionScope(action="read", target=mixed).normalized().target
        self.assertEqual(
            normalized, ActionScope(action="read", target=forward).normalized().target
        )
        self.assertNotIn("\\", normalized)
        self.assertTrue(normalized.endswith("/sub/file.txt"))
        expected_root = os.path.normcase(str(self.root)).replace("\\", "/")
        self.assertTrue(normalized.startswith(expected_root))
        if os.name == "nt":
            self.assertRegex(normalized, r"^[a-z]:/")

    def test_relative_filesystem_target_still_resolves_against_the_cwd(self) -> None:
        original = Path.cwd()
        os.chdir(self.root)
        try:
            normalized = ActionScope(action="read", target="sub/file.txt").normalized()
        finally:
            os.chdir(original)
        expected = os.path.normcase(str(self.root / "sub" / "file.txt")).replace(
            "\\", "/"
        )
        self.assertEqual(expected, normalized.target)

    def test_url_normalization_is_unchanged(self) -> None:
        normalized = ActionScope(
            action="post",
            target="HTTPS://API.Example.COM/v1/Send?x=1#fragment",
            destination="HTTPS://API.Example.COM/v1/Send?x=1#fragment",
        ).normalized()
        expected = "https://api.example.com/v1/Send?x=1"
        self.assertEqual(expected, normalized.target)
        self.assertEqual(expected, normalized.destination)


if __name__ == "__main__":
    unittest.main()
