"""H1 - approval / egress attestation hardening.

Adversarial coverage for the invariant that a permission grant authorizes the
exact external data, gate state, connector-owned side effect, destination and
local-resource boundary that execution will actually use. Persisted lifecycle
stage alone never proves a permission or cost gate was satisfied.

No test here performs a live model, auth, network or MCP call: the auth status
is the known subscription fixture, ``shutil.which`` is fixed, ``subprocess.run``
fails if touched and the model process is the recorded ``FakeProcess``. Several
tests assert the model process count stays exactly zero.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis.connectors import claude_code
from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.connectors.local_files import LocalFileReadConnector
from jarvis.contracts import (
    ActionScope,
    AttestationError,
    BillingClass,
    CostEstimate,
    DataClassification,
    EgressAttestation,
    ErrorCode,
    LoopStage,
    MonetaryState,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    RetrievedContext,
    RetrievedMemoryItem,
    RetrievedMemoryKind,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    SideEffectProfile,
    Task,
    ToolResult,
    UsageRecord,
    is_expired,
    merge_side_effects,
    redact_credentials,
)
from jarvis.core import AgentCore
from jarvis.destinations import (
    CLAUDE_CODE_DESTINATION,
    DestinationGrant,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, installed_cli, make_task, open_app, open_core, safe_cleanup
from tests.test_claude_connector import (
    OAUTH_MAX_AUTH_STATUS,
    FakeProcess,
    patch_oauth_environment,
)


#: The shipped Claude rows, written out here rather than imported, so a
#: fixture that composes the real connector still DECLARES its authority.
CLAUDE_TUPLES = frozenset(
    {
        DestinationGrant("claude-code-plan", capability, CLAUDE_CODE_DESTINATION)
        for capability in ("code_analysis", "code_planning")
    }
)


SUCCESS_CLI = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Plan ready",
        "total_cost_usd": 0,
        "usage": {"input_tokens": 12, "output_tokens": 5},
        "modelUsage": {"claude-test": {}},
    }
)


class AttestationContractTests(unittest.TestCase):
    """The typed attestation and its digest/fingerprint binding."""

    def scope(self, **overrides) -> ActionScope:
        base = {
            "action": "analyze",
            "target": "resource-a",
            "connector": "claude-code-plan",
            "destination": "anthropic/claude-code",
            "data_classification": DataClassification.INTERNAL,
        }
        base.update(overrides)
        return ActionScope(**base).normalized()

    def attestation(self, **overrides) -> EgressAttestation:
        base = dict(
            scope=self.scope(),
            risk_level=RiskLevel.MEDIUM,
            side_effect=SideEffectProfile(
                effect=SideEffect.EXTERNAL_EGRESS, always_requires_approval=True
            ),
            outbound_digest="a" * 64,
            billing_gate="subscription_included|0.000000000|0|0.150000000",
            cost_gate="",
            working_directory="c:/work",
            allowed_roots=("c:/work",),
        )
        base.update(overrides)
        return EgressAttestation(**base)

    def test_digest_is_deterministic_and_round_trips(self) -> None:
        attestation = self.attestation()
        self.assertEqual(attestation.digest, self.attestation().digest)
        restored = EgressAttestation.from_dict(attestation.to_dict())
        self.assertEqual(attestation, restored)
        self.assertEqual(attestation.digest, restored.digest)

    def test_each_bound_field_changes_the_digest(self) -> None:
        baseline = self.attestation().digest
        mutations = (
            {"outbound_digest": "b" * 64},
            {"billing_gate": "metered_monetary|0.250000000|1|0.150000000"},
            {"cost_gate": "override_monthly_cost_cap|20.5|20|0"},
            {"working_directory": "c:/elsewhere"},
            {"allowed_roots": ("c:/work", "c:/extra")},
            {"risk_level": RiskLevel.HIGH},
            {"scope": self.scope(target="resource-b")},
            {"scope": self.scope(destination="anthropic/other-endpoint")},
            {
                "side_effect": SideEffectProfile(
                    effect=SideEffect.EXTERNAL_EGRESS, reversible=False,
                    always_requires_approval=True,
                )
            },
        )
        digests = {baseline}
        for mutation in mutations:
            with self.subTest(mutation=tuple(mutation)):
                digest = self.attestation(**mutation).digest
                self.assertNotIn(digest, digests)
                digests.add(digest)

    def test_missing_attestation_yields_a_distinct_fingerprint(self) -> None:
        from jarvis.contracts import PermissionRequest

        common = dict(
            task_id="task-1",
            session_id="session-1",
            scope=self.scope(),
            risk_level=RiskLevel.MEDIUM,
            side_effect=SideEffectProfile(
                effect=SideEffect.EXTERNAL_EGRESS, always_requires_approval=True
            ),
            reason="test",
        )
        without = PermissionRequest(**common)
        with_attestation = PermissionRequest(**common, attestation=self.attestation())
        self.assertNotEqual(without.fingerprint, with_attestation.fingerprint)
        self.assertNotEqual(without.id, with_attestation.id)


class ConnectorAttestationMutationTests(unittest.TestCase):
    """Every externally meaningful mutation invalidates the old attestation."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        (self.root / "sub").mkdir()
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )
        for target, attribute in (
            (claude_code.subprocess, "Popen"),
            (claude_code.subprocess, "run"),
            (claude_code, "resolve_trusted_executable"),
        ):
            patcher = mock.patch.object(
                target, attribute, side_effect=AssertionError(f"{attribute}")
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self, **overrides) -> Task:
        task = Task(
            objective="Analyze the fixture without changing files",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )
        for key, value in overrides.items():
            setattr(task, key, value)
        # A cost estimate is present in the loop before an attestation is
        # built; use the connector's own subscription estimate as the floor.
        task.estimate = CostEstimate(
            billing_class=self.connector.manifest.default_billing_class
        )
        return task

    def fingerprint(self, task: Task) -> str:
        with patch_oauth_environment(), mock.patch.object(
            ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
        ):
            # The estimate the loop would bind before asking for approval.
            task.estimate = self.connector.estimate_cost(task)
            return self.connector.permission_request(task, "session-1").fingerprint

    def test_each_meaningful_mutation_changes_the_fingerprint(self) -> None:
        base_task = self.task()
        baseline = self.fingerprint(base_task)

        retrieved = RetrievedContext(
            items=(
                RetrievedMemoryItem(
                    kind=RetrievedMemoryKind.DECISION,
                    record_id="r1",
                    source_task_id="t1",
                    summary="eligible earlier decision",
                    rationale="because",
                    next_step=None,
                    classification=DataClassification.INTERNAL,
                    created_at="2026-08-18T00:00:00+00:00",
                    relevance_score=3,
                    truncated=False,
                ),
            )
        )
        mutations = {
            "objective": dict(objective="A completely different objective"),
            "capability": dict(capability="code_analysis"),
            "visible_assumption": dict(visible_assumption="Assume option B only"),
            "context": dict(
                context={"working_directory": str(self.root), "inputs": {"x": 1}}
            ),
            "retrieved_memory": dict(retrieved_context=retrieved),
            "scope_action": dict(
                scope=replace(base_task.scope, action="summarize")
            ),
            "target": dict(scope=replace(base_task.scope, target=str(self.root / "sub"))),
            "working_directory": dict(
                context={"working_directory": str(self.root / "sub")}
            ),
        }
        seen = {baseline}
        for label, overrides in mutations.items():
            with self.subTest(mutation=label):
                fingerprint = self.fingerprint(self.task(**overrides))
                self.assertNotIn(
                    fingerprint, seen, f"{label} did not change the attestation"
                )
                seen.add(fingerprint)

    def test_billing_and_ceiling_changes_change_the_fingerprint(self) -> None:
        task = self.task()
        baseline = self.fingerprint(task)
        # A different resolved resource ceiling is a different connector and a
        # different attestation, even for the same task.
        other_connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.20,
        )
        with patch_oauth_environment(), mock.patch.object(
            ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
        ):
            task.estimate = other_connector.estimate_cost(task)
            changed = other_connector.permission_request(task, "session-1").fingerprint
        self.assertNotEqual(baseline, changed)

    def test_side_effect_strengthening_changes_the_fingerprint(self) -> None:
        task = self.task()
        baseline = self.fingerprint(task)
        task.side_effect = SideEffectProfile(
            effect=SideEffect.EXTERNAL_EGRESS,
            reversible=False,
            always_requires_approval=True,
        )
        self.assertNotEqual(baseline, self.fingerprint(task))


class SideEffectMergeTests(unittest.TestCase):
    """The connector manifest is the non-weakenable lower bound."""

    EGRESS = SideEffectProfile(
        effect=SideEffect.EXTERNAL_EGRESS, reversible=False, always_requires_approval=True
    )

    def test_external_egress_cannot_become_local_read(self) -> None:
        # A task cannot weaken a connector's external egress into a local
        # read: the conflicting profile fails closed rather than downgrading.
        with self.assertRaises(AttestationError):
            merge_side_effects(
                self.EGRESS, SideEffectProfile(effect=SideEffect.LOCAL_READ)
            )

    def test_mandatory_approval_cannot_become_non_mandatory(self) -> None:
        merged = merge_side_effects(
            self.EGRESS,
            SideEffectProfile(effect=SideEffect.NONE, always_requires_approval=False),
        )
        self.assertTrue(merged.mandatory)
        self.assertTrue(merged.always_requires_approval)

    def test_irreversible_cannot_become_reversible(self) -> None:
        merged = merge_side_effects(
            self.EGRESS, SideEffectProfile(effect=SideEffect.NONE, reversible=True)
        )
        self.assertFalse(merged.reversible)

    def test_task_may_strengthen_a_local_effect(self) -> None:
        merged = merge_side_effects(
            SideEffectProfile(effect=SideEffect.LOCAL_READ),
            SideEffectProfile(effect=SideEffect.LOCAL_WRITE, reversible=False),
        )
        self.assertIs(SideEffect.LOCAL_WRITE, merged.effect)
        self.assertFalse(merged.reversible)

    def test_task_may_not_downgrade_a_local_effect(self) -> None:
        with self.assertRaises(AttestationError):
            merge_side_effects(
                SideEffectProfile(effect=SideEffect.LOCAL_WRITE),
                SideEffectProfile(effect=SideEffect.LOCAL_READ),
            )

    def test_conflicting_effects_fail_closed(self) -> None:
        with self.assertRaises(AttestationError):
            merge_side_effects(
                self.EGRESS, SideEffectProfile(effect=SideEffect.BILLING)
            )

    def test_none_manifest_takes_the_task_effect(self) -> None:
        merged = merge_side_effects(
            SideEffectProfile(),
            SideEffectProfile(effect=SideEffect.LOCAL_WRITE, reversible=False),
        )
        self.assertIs(SideEffect.LOCAL_WRITE, merged.effect)

    def test_connector_side_effects_survive_a_task_downgrade_attempt(self) -> None:
        # An EXTERNAL_EGRESS + mandatory connector cannot be demoted to
        # LOCAL_READ + non-mandatory by task/persisted state: the attempt is
        # refused, never silently weakened.
        connector = _EgressConnector()
        task = make_task(effect=SideEffect.LOCAL_READ)
        task.side_effect = SideEffectProfile(
            effect=SideEffect.LOCAL_READ, reversible=True, always_requires_approval=False
        )
        with self.assertRaises(AttestationError):
            connector.side_effects(task)

    def test_connector_egress_survives_a_matching_task_effect(self) -> None:
        # The same egress effect from both sides keeps the connector floor:
        # a task cannot make it reversible or non-mandatory.
        connector = _EgressConnector()
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.side_effect = SideEffectProfile(
            effect=SideEffect.EXTERNAL_EGRESS,
            reversible=True,
            always_requires_approval=False,
        )
        effective = connector.side_effects(task)
        self.assertIs(SideEffect.EXTERNAL_EGRESS, effective.effect)
        self.assertTrue(effective.mandatory)
        self.assertFalse(effective.reversible)

    def test_permission_mode_matrix_still_forces_approval_for_egress(self) -> None:
        connector = _EgressConnector()
        for mode in PermissionMode:
            with self.subTest(mode=mode):
                temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                self.addCleanup(temp.cleanup)
                memory = SQLiteMemoryStore(Path(temp.name) / "jarvis.db")
                registry = ToolRegistry()
                registry.register(connector)
                core = open_core(
                    registry,
                    memory,
                    Settings(memory_database=memory.database_path, permission_mode=mode),
                    destination_policy=frozenset(
                        {
                            DestinationGrant(
                                connector.manifest.name,
                                DUMMY_CAPABILITY,
                                CLAUDE_CODE_DESTINATION,
                            )
                        }
                    ),
                )
                # The TASK's own effect stays NONE - the manifest floor is
                # what this proves - but the scope must still name the
                # destination Core registered, because the effective effect
                # this connector forces is an external egress.
                run = core.run(make_task(destination=CLAUDE_CODE_DESTINATION))
                self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)


class _EgressConnector(DummyConnector):
    """A connector whose manifest owns a mandatory external-egress effect."""

    def __init__(self, **kwargs) -> None:
        super().__init__(name="egress", **kwargs)
        self.manifest = replace(
            self.manifest,
            default_side_effect=SideEffectProfile(
                effect=SideEffect.EXTERNAL_EGRESS,
                reversible=False,
                always_requires_approval=True,
            ),
        )


class CredentialRedactionTests(unittest.TestCase):
    """Labelled and shape-based credentials never leave through any field."""

    POSITIVE = (
        "api_key: SECRETVALUE",
        "api-key=SECRETVALUE",
        "openai_api_key=SECRETVALUE",
        "password=SECRETVALUE",
        "database_password: SECRETVALUE",
        "access_token=SECRETVALUE",
        "github-token: SECRETVALUE",
        "client_secret=SECRETVALUE",
        "session_cookie=SECRETVALUE",
        "Client_Secret = SECRETVALUE",
        "AUTHORIZATION: SECRETVALUE",
        "leaked sk-abcdefghijklmnop now",
        "Bearer abcdefghijklmnop rest",
        "jwt aaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc end",
    )
    NEGATIVE = (
        "open tokenizer.py",
        "see secret_handling.md",
        "Review token telemetry",
        "an ordinary sentence about a secret meeting and a token gift",
        "mytoken=value",
        "passwordless=value",
    )

    def test_positive_cases_are_redacted(self) -> None:
        for text in self.POSITIVE:
            with self.subTest(text=text):
                redacted, count = redact_credentials(text)
                self.assertGreaterEqual(count, 1)
                self.assertIn("[REDACTED]", redacted)
                self.assertNotIn("SECRETVALUE", redacted)
                self.assertNotIn("abcdefghijklmnop", redacted)

    def test_negative_cases_are_left_alone(self) -> None:
        for text in self.NEGATIVE:
            with self.subTest(text=text):
                redacted, count = redact_credentials(text)
                self.assertEqual(text, redacted)
                self.assertEqual(0, count)

    def test_labelled_credentials_are_redacted_in_every_outbound_field(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        connector = ClaudeCodeConnector(
            allowed_roots=(root,), default_working_directory=root, max_budget_usd=0.15
        )
        task = Task(
            objective="objective with database_password=LEAKONE inside",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
            context={"note": "openai_api_key=LEAKTWO"},
            visible_assumption="reuse github-token: LEAKTHREE",
        )
        with mock.patch.object(claude_code, "resolve_trusted_executable", side_effect=AssertionError):
            payload, count = connector._sanitized_payload(task)
        serialized = json.dumps(payload)
        for leak in ("LEAKONE", "LEAKTWO", "LEAKTHREE"):
            self.assertNotIn(leak, serialized)
        self.assertGreaterEqual(count, 3)


class SecretExternalExecutionTests(unittest.TestCase):
    """A SECRET task must never reach the external Claude process."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.memory = SQLiteMemoryStore(self.root / "jarvis.db")
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)
        auth = mock.patch.object(
            ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
        )
        auth.start()
        self.addCleanup(auth.stop)
        self.popen = mock.patch.object(
            claude_code.subprocess,
            "Popen",
            side_effect=AssertionError("no model process for a SECRET task"),
        )
        self.popen_mock = self.popen.start()
        self.addCleanup(self.popen.stop)

    def secret_task(self) -> Task:
        return Task(
            objective="classified objective",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.SECRET,
            ),
            context={"working_directory": str(self.root)},
        )

    def test_connector_execute_refuses_before_any_process(self) -> None:
        result = self.connector.execute(self.secret_task(), None)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, result.error.code)
        self.assertEqual(0, result.usage.call_count)
        self.popen_mock.assert_not_called()

    def test_connector_cannot_attest_a_secret_task(self) -> None:
        with self.assertRaises(AttestationError) as caught:
            self.connector.permission_request(self.secret_task(), "session-1")
        self.assertEqual(ErrorCode.EGRESS_REFUSED, caught.exception.code)

    def test_agent_loop_fails_a_secret_task_closed_with_zero_processes(self) -> None:
        registry = ToolRegistry()
        registry.register(self.connector)
        core = open_core(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path),
            destination_policy=CLAUDE_TUPLES,
        )
        run = core.run(self.secret_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.popen_mock.assert_not_called()

    def test_fabricated_secret_execute_task_never_spawns_a_process(self) -> None:
        registry = ToolRegistry()
        registry.register(self.connector)
        core = open_core(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path),
            destination_policy=CLAUDE_TUPLES,
        )
        session = core.start_session("secret")
        task = self.secret_task()
        task.selected_connector = self.connector.manifest.name
        task.stage = LoopStage.EXECUTE
        task.preferences["billing_preflight_fingerprint"] = "forged"
        self.memory.save_task(session.id, task)
        run = core.resume(session.id, task.id)
        self.assertNotEqual(RunStatus.COMPLETED, run.status)
        self.popen_mock.assert_not_called()


def _symlinks_supported() -> bool:  # pragma: no cover - environment probe
    probe = Path(tempfile.gettempdir()) / "jarvis-symlink-probe"
    try:
        target = probe.with_suffix(".target")
        target.mkdir(exist_ok=True)
        if probe.exists() or probe.is_symlink():
            probe.unlink()
        probe.symlink_to(target, target_is_directory=True)
        probe.unlink()
        return True
    except (OSError, NotImplementedError):
        return False


class ResourceBoundaryTests(unittest.TestCase):
    """The approved local-resource boundary is bound and re-checked."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        (self.root / "inside").mkdir()
        self.outside = self.root.parent
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )
        for target, attribute in (
            (claude_code.subprocess, "Popen"),
            (claude_code.subprocess, "run"),
        ):
            patcher = mock.patch.object(
                target, attribute, side_effect=AssertionError(attribute)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self, target: str | None = None, working_directory: str | None = None):
        return Task(
            objective="Analyze",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=target if target is not None else str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
            context=(
                {"working_directory": working_directory}
                if working_directory is not None
                else {}
            ),
        )

    def test_boundary_is_canonical_and_bound(self) -> None:
        working, roots = self.connector.resource_boundary(self.task())
        self.assertEqual(1, len(roots))
        self.assertNotIn("\\", working)
        self.assertTrue(working.startswith(roots[0]) or working == roots[0])

    def test_working_directory_outside_roots_aborts(self) -> None:
        with self.assertRaises(AttestationError) as caught:
            self.connector.resource_boundary(
                self.task(working_directory=str(self.outside))
            )
        self.assertEqual(ErrorCode.EGRESS_REFUSED, caught.exception.code)

    def test_target_outside_roots_aborts(self) -> None:
        with self.assertRaises(AttestationError):
            self.connector.resource_boundary(
                self.task(target=str(self.outside / "secrets.txt"))
            )

    def test_dotdot_traversal_out_of_roots_aborts(self) -> None:
        escape = str(self.root / "inside" / ".." / ".." / "escape.txt")
        with self.assertRaises(AttestationError):
            self.connector.resource_boundary(self.task(target=escape))

    def test_dotdot_traversal_inside_roots_is_allowed(self) -> None:
        stays = str(self.root / "inside" / ".." / "inside")
        working, _ = self.connector.resource_boundary(self.task(target=stays))
        self.assertTrue(working)

    def test_changed_allowed_root_changes_the_attestation(self) -> None:
        # permission_request never probes billing; a fixed estimate is enough.
        # The target stays inside both roots so the only difference is the
        # connector's allowed roots and resolved working directory.
        inside = str(self.root / "inside")
        task = self.task(target=inside)
        task.estimate = CostEstimate()
        with mock.patch.object(claude_code, "resolve_trusted_executable", side_effect=AssertionError):
            approved = self.connector.permission_request(task, "session-1")
            moved = ClaudeCodeConnector(
                allowed_roots=(self.root / "inside",),
                default_working_directory=self.root / "inside",
                max_budget_usd=0.15,
            )
            task.context = {"working_directory": inside}
            task.estimate = CostEstimate()
            refreshed = moved.permission_request(task, "session-1")
        self.assertNotEqual(approved.fingerprint, refreshed.fingerprint)

    @unittest.skipUnless(os.name != "nt" or _symlinks_supported(), "symlinks")
    def test_symlink_escape_fails_closed(self) -> None:
        link = self.root / "inside" / "escape-link"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - privilege
            self.skipTest("symlink creation not permitted")
        with self.assertRaises(AttestationError):
            self.connector.resource_boundary(
                self.task(working_directory=str(link))
            )


class ClarificationLifecycleTests(unittest.TestCase):
    """provide_clarification is legal only while awaiting clarification."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def core(self, connector=None, mode=PermissionMode.SESSION) -> AgentCore:
        connector = connector or DummyConnector()
        registry = ToolRegistry()
        registry.register(connector)
        return open_core(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path, permission_mode=mode),
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )

    def waiting_for_clarification(self, core):
        task = make_task()
        task.preferences["needs_clarification"] = True
        run = core.run(task)
        self.assertEqual(RunStatus.AWAITING_CLARIFICATION, run.status)
        return run

    def assert_unchanged(self, core, run, before) -> None:
        result = core.provide_clarification(
            run.session.id, run.task.id, "late answer"
        )
        self.assertEqual(ErrorCode.INVALID_TASK, result.error.code)
        after = self.memory.load_task(run.task.id)
        self.assertEqual(before.status, after.status)
        self.assertEqual(before.stage, after.stage)
        self.assertEqual(before.preferences, after.preferences)

    def test_clarification_after_approval_request_is_rejected(self) -> None:
        core = self.core(DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS))
        run = core.run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assert_unchanged(core, run, self.memory.load_task(run.task.id))

    def test_clarification_after_approval_is_rejected(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        core = self.core(connector)
        run = core.run(make_task(risk=RiskLevel.MEDIUM))
        approved = core.resume(
            run.session.id,
            run.task.id,
            PermissionGrant.for_request(run.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, approved.status)
        self.assert_unchanged(core, run, self.memory.load_task(run.task.id))

    def test_clarification_after_terminal_states_is_rejected(self) -> None:
        for label, connector, act in (
            ("completed", DummyConnector(), None),
            (
                "rejected",
                DummyConnector(risk=RiskLevel.MEDIUM),
                "reject",
            ),
            ("failed", DummyConnector(verification_passes=False), None),
            ("cancelled", DummyConnector(risk=RiskLevel.MEDIUM), "cancel"),
        ):
            with self.subTest(state=label):
                self.setUp()
                core = self.core(connector)
                run = core.run(make_task(risk=connector.manifest.default_risk))
                if act == "reject":
                    run = core.resume(
                        run.session.id,
                        run.task.id,
                        PermissionGrant.for_request(
                            run.permission_request, approved=False
                        ),
                    )
                elif act == "cancel":
                    run = core.cancel(run.session.id, run.task.id)
                before = self.memory.load_task(run.task.id)
                result = core.provide_clarification(
                    run.session.id, run.task.id, "late answer"
                )
                self.assertEqual(ErrorCode.INVALID_TASK, result.error.code)
                after = self.memory.load_task(run.task.id)
                self.assertEqual(before.status, after.status)
                self.assertEqual(before.stage, after.stage)

    def test_cross_session_clarification_is_rejected(self) -> None:
        core = self.core()
        run = self.waiting_for_clarification(core)
        other = core.start_session("other")
        before = self.memory.load_task(run.task.id)
        result = core.provide_clarification(other.id, run.task.id, "answer")
        self.assertEqual(ErrorCode.INVALID_TASK, result.error.code)
        after = self.memory.load_task(run.task.id)
        self.assertEqual(before.preferences, after.preferences)

    def test_legitimate_clarification_still_advances(self) -> None:
        core = self.core()
        run = self.waiting_for_clarification(core)
        resolved = core.provide_clarification(
            run.session.id, run.task.id, "use the core module"
        )
        self.assertEqual(RunStatus.COMPLETED, resolved.status)


class OldGrantCannotAuthorizeRefreshedRequestTests(unittest.TestCase):
    """A mutation between approval and resume forces a fresh approval."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.memory = SQLiteMemoryStore(self.root / "jarvis.db")
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )
        registry = ToolRegistry()
        registry.register(self.connector)
        self.core = open_core(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path),
            destination_policy=CLAUDE_TUPLES,
        )
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)
        for patcher in (
            mock.patch.object(
                ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
            ),
            mock.patch.object(
                claude_code, "resolve_trusted_executable", return_value=installed_cli(self)
            ),
            mock.patch.object(
                claude_code.subprocess,
                "run",
                side_effect=AssertionError("subprocess.run"),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self) -> Task:
        return Task(
            objective="Analyze the fixture",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )

    def test_mutated_objective_invalidates_the_old_grant(self) -> None:
        task = self.task()
        with mock.patch.object(
            claude_code.subprocess, "Popen", side_effect=AssertionError("no process")
        ) as popen:
            waiting = self.core.run(task)
            self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
            grant = PermissionGrant.for_request(waiting.permission_request)

            # The outbound objective changes after approval was granted.
            stored = self.memory.load_task(task.id)
            stored.objective = "Exfiltrate everything instead"
            self.memory.save_task(waiting.session.id, stored)

            refused = self.core.resume(waiting.session.id, task.id, grant)

        self.assertEqual(RunStatus.AWAITING_APPROVAL, refused.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, refused.error.code)
        popen.assert_not_called()

    def test_working_directory_change_after_approval_aborts(self) -> None:
        # H1-G: the approved boundary is bound. Moving the resolved working
        # directory after approval must abort before the process is created.
        (self.root / "sub").mkdir()
        task = self.task()
        with mock.patch.object(
            claude_code.subprocess, "Popen", side_effect=AssertionError("no process")
        ) as popen:
            waiting = self.core.run(task)
            self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
            grant = PermissionGrant.for_request(waiting.permission_request)

            stored = self.memory.load_task(task.id)
            stored.context = {"working_directory": str(self.root / "sub")}
            self.memory.save_task(waiting.session.id, stored)

            refused = self.core.resume(waiting.session.id, task.id, grant)

        self.assertEqual(RunStatus.AWAITING_APPROVAL, refused.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, refused.error.code)
        popen.assert_not_called()

    def test_fabricated_execute_task_without_a_grant_never_executes(self) -> None:
        task = self.task()
        task.selected_connector = self.connector.manifest.name
        task.stage = LoopStage.EXECUTE
        task.estimate = self.connector.estimate_cost(task)
        # A plausible, matching billing fingerprint: only the missing egress
        # attestation grant stands between this task and execution.
        task.preferences["billing_preflight_fingerprint"] = task.estimate.gate_state
        session = self.core.start_session("fabricated")
        self.memory.save_task(session.id, task)
        with mock.patch.object(
            claude_code.subprocess, "Popen", side_effect=AssertionError("no process")
        ) as popen:
            run = self.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertEqual(0, task_execute_count(popen))


class ApprovedEgressEqualsExecutedEgressTests(unittest.TestCase):
    """task -> AWAITING_APPROVAL -> exact grant -> resume -> one execution.

    The approved attestation digest equals the executed outbound bytes digest.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.memory = SQLiteMemoryStore(self.root / "jarvis.db")
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )
        registry = ToolRegistry()
        registry.register(self.connector)
        self.core = open_core(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path),
            destination_policy=CLAUDE_TUPLES,
        )
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)
        for patcher in (
            mock.patch.object(
                ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
            ),
            mock.patch.object(
                claude_code, "resolve_trusted_executable", return_value=installed_cli(self)
            ),
            mock.patch.object(
                claude_code.subprocess,
                "run",
                side_effect=AssertionError("subprocess.run"),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_approved_digest_equals_executed_bytes(self) -> None:
        task = Task(
            objective="Summarize the module layout",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )
        waiting = self.core.run(task)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
        attestation = waiting.permission_request.attestation
        self.assertIsNotNone(attestation)

        approved_task = self.memory.load_task(task.id)
        import hashlib

        approved_bytes = self.connector.canonical_outbound(approved_task)
        self.assertEqual(
            attestation.outbound_digest, hashlib.sha256(approved_bytes).hexdigest()
        )

        grant = PermissionGrant.for_request(waiting.permission_request)
        with mock.patch.object(
            claude_code.subprocess, "Popen", return_value=FakeProcess(SUCCESS_CLI)
        ) as popen:
            resumed = self.core.resume(waiting.session.id, task.id, grant)

        self.assertEqual(RunStatus.COMPLETED, resumed.status)
        self.assertEqual(1, popen.call_count)
        command = popen.call_args.args[0]
        executed_prompt = command[command.index("-p") + 1]
        executed_digest = hashlib.sha256(executed_prompt.encode("utf-8")).hexdigest()
        # The exact bytes the grant attested are the exact bytes executed.
        self.assertEqual(attestation.outbound_digest, executed_digest)


class CostRequestReuseIntegrityTests(unittest.TestCase):
    """A persisted cost request is reused only when its full gate state still
    holds; a changed projected monthly spend invalidates it (H1-C)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def core(self, connector) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        return open_core(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path),
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )

    def append_metered(self, amount: float) -> None:
        self.memory.append_usage(
            UsageRecord(
                billing_class=BillingClass.METERED_MONETARY,
                monetary_state=MonetaryState.MEASURED,
                monetary_cost_usd=amount,
            )
        )

    def test_changed_projection_invalidates_a_persisted_cost_request(self) -> None:
        self.append_metered(19.90)
        connector = DummyConnector(name="metered", estimate_max=0.20, actual_cost=0.05)
        core = self.core(connector)

        waiting = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, waiting.status)
        self.assertEqual(
            "override_monthly_cost_cap", waiting.permission_request.scope.action
        )
        old_grant = PermissionGrant.for_request(waiting.permission_request)

        # The projected monthly spend changes before the override is applied.
        self.append_metered(0.05)

        refused = core.resume(waiting.session.id, waiting.task.id, old_grant)
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, refused.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, refused.error.code)
        self.assertEqual(0, connector.execute_calls)

        # A fresh grant against the current request clears the gate.
        fresh = core.resume(
            refused.session.id,
            refused.task.id,
            PermissionGrant.for_request(refused.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, fresh.status)
        self.assertEqual(1, connector.execute_calls)

    def test_scope_action_alone_does_not_authorize_reuse(self) -> None:
        # The persisted request and the current request share scope.action
        # but differ in projected spend; equality is by full fingerprint.
        self.append_metered(19.90)
        connector = DummyConnector(name="metered", estimate_max=0.20, actual_cost=0.05)
        core = self.core(connector)
        waiting = core.run(make_task())
        persisted = self.memory.load_pending_approval(
            waiting.task.id, waiting.session.id
        )
        self.append_metered(0.05)
        # Re-derive the current cost request the way the loop does.
        projected = self.memory.monthly_monetary_cost() + 0.20
        self.assertEqual(
            "override_monthly_cost_cap", persisted.scope.action
        )
        self.assertNotIn(
            f"{projected:.9f}", persisted.attestation.cost_gate
        )


def task_execute_count(popen) -> int:
    return len([call for call in popen.call_args_list if "-p" in call.args[0]])


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()


# --------------------------------------------------------------------------
# P1-A - approval covers the actual tool-mediated external egress
# --------------------------------------------------------------------------


class ToolManifestTests(unittest.TestCase):
    """The Claude execution path grants no file-reading tools, so the attested
    prompt bytes are everything the external model can receive."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )
        for target, attribute in (
            (claude_code.subprocess, "Popen"),
            (claude_code.subprocess, "run"),
            (claude_code, "resolve_trusted_executable"),
        ):
            patcher = mock.patch.object(
                target, attribute, side_effect=AssertionError(attribute)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self) -> Task:
        task = Task(
            objective="Analyze the fixture",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )
        task.estimate = CostEstimate()
        return task

    def test_no_broad_read_tools_are_granted(self) -> None:
        command = self.connector._command("prompt")
        tools = command[command.index("--tools") + 1]
        # Sibling/unlisted resources cannot pass a tool boundary that does not
        # exist; the CWD alone authorizes no file read to the external model.
        self.assertEqual("", tools)
        for tool in ("Read", "Glob", "Grep"):
            self.assertNotIn(tool, tools)
        self.assertEqual((), claude_code.EGRESS_TOOL_GRANTS)

    def test_attested_outbound_is_the_only_egress_channel(self) -> None:
        import hashlib

        task = self.task()
        request = self.connector.permission_request(task, "session-1")
        attestation = request.attestation
        self.assertEqual((), attestation.tool_grants)
        # Everything the model can receive is the attested prompt bytes.
        self.assertEqual(
            attestation.outbound_digest,
            hashlib.sha256(self.connector.canonical_outbound(task)).hexdigest(),
        )

    def test_changed_tool_manifest_after_approval_invalidates_it(self) -> None:
        task = self.task()
        approved = self.connector.permission_request(task, "session-1")
        # A connector that silently re-enables a file-reading tool produces a
        # different manifest, hence a different attestation and fingerprint.
        with mock.patch.object(claude_code, "EGRESS_TOOL_GRANTS", ("Read",)):
            widened = self.connector.permission_request(task, "session-1")
        self.assertNotEqual(approved.fingerprint, widened.fingerprint)
        self.assertEqual(("Read",), widened.attestation.tool_grants)

    def test_manifest_is_shown_in_a_safe_display(self) -> None:
        manifest = self.connector.permission_request(
            self.task(), "session-1"
        ).attestation.display_manifest()
        self.assertEqual([], manifest["tool_grants"])
        self.assertEqual("anthropic/claude-code", manifest["destination"])
        self.assertTrue(manifest["working_directory"])
        self.assertEqual(1, len(manifest["allowed_roots"]))
        # Only safe, bounded values - no raw prompt bytes.
        self.assertNotIn("JARVIS read-only planning request", json.dumps(manifest))


# --------------------------------------------------------------------------
# P1-B - expired request/grant must never reopen execution
# --------------------------------------------------------------------------


class ExpiredGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def core(self, connector) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        return open_core(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path),
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )

    def egress_connector(self):
        return DummyConnector(name="egress", side_effect=SideEffect.EXTERNAL_EGRESS)

    def persist_execute_task(self, connector):
        core = self.core(connector)
        session = core.start_session("expiry")
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.selected_connector = connector.manifest.name
        task.estimate = connector.estimate_cost(task)
        task.stage = LoopStage.EXECUTE
        task.preferences["billing_preflight_fingerprint"] = task.estimate.gate_state
        self.memory.save_task(session.id, task)
        return core, session, task

    def record_grant(self, session, task, connector, expires_at: str):
        request = connector.permission_request(task, session.id)
        self.memory.save_pending_approval(request)
        grant = replace(
            PermissionGrant.for_request(request), expires_at=expires_at
        )
        self.memory.resolve_approval(request, grant, "approved")
        return request

    def test_expired_recorded_grant_regates_at_execute_with_zero_execution(self) -> None:
        connector = self.egress_connector()
        core, session, task = self.persist_execute_task(connector)
        self.record_grant(session, task, connector, _past())

        run = core.resume(session.id, task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertEqual(0, connector.execute_calls)

    def test_valid_recorded_grant_executes_exactly_once(self) -> None:
        connector = self.egress_connector()
        core, session, task = self.persist_execute_task(connector)
        self.record_grant(session, task, connector, _future())

        run = core.resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)

    def test_expired_pending_with_unexpired_old_grant_reissues(self) -> None:
        connector = self.egress_connector()
        core = self.core(connector)
        waiting = core.run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
        original = waiting.permission_request
        old_grant = PermissionGrant.for_request(original)  # still unexpired

        # The persisted pending request expires before it is acted on.
        self.memory.save_pending_approval(replace(original, expires_at=_past()))

        refused = core.resume(waiting.session.id, waiting.task.id, old_grant)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, refused.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, refused.error.code)
        self.assertEqual(0, connector.execute_calls)
        # A fresh request with a new unique id replaced the expired one.
        reissued = self.memory.load_pending_approval(
            waiting.task.id, waiting.session.id
        )
        self.assertIsNotNone(reissued)
        self.assertNotEqual(original.id, reissued.id)
        self.assertFalse(is_expired(reissued.expires_at))

    def test_valid_exact_grant_still_works_once(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        core = self.core(connector)
        waiting = core.run(make_task(risk=RiskLevel.MEDIUM))
        run = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)

    def test_expired_cost_override_grant_returns_to_gate(self) -> None:
        self.memory.append_usage(
            UsageRecord(
                billing_class=BillingClass.METERED_MONETARY,
                monetary_state=MonetaryState.MEASURED,
                monetary_cost_usd=19.90,
            )
        )
        connector = DummyConnector(name="metered", estimate_max=0.20, actual_cost=0.05)
        core = self.core(connector)
        waiting = core.run(make_task())
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, waiting.status)
        expired = replace(
            PermissionGrant.for_request(waiting.permission_request),
            expires_at=_past(),
        )
        refused = core.resume(waiting.session.id, waiting.task.id, expired)
        self.assertEqual(RunStatus.AWAITING_COST_OVERRIDE, refused.status)
        self.assertEqual(ErrorCode.STALE_GRANT, refused.error.code)
        self.assertEqual(0, connector.execute_calls)


def is_expired_helper(timestamp: str) -> bool:
    from jarvis.contracts import is_expired

    return is_expired(timestamp)


class RequestIdentityTests(unittest.TestCase):
    """Issuance identity is distinct from the semantic fingerprint."""

    def test_two_issuances_share_fingerprint_but_not_id(self) -> None:
        connector = DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        task = make_task()
        # The same task rebuilt twice: identical semantic fingerprint, but a
        # fresh unique issuance id each time.
        first = connector.permission_request(task, "session-1")
        second = connector.permission_request(task, "session-1")
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.id, second.id)

    def test_old_grant_rejected_for_reissued_request_same_fingerprint(self) -> None:
        connector = DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        task = make_task()
        first = connector.permission_request(task, "session-1")
        second = connector.permission_request(task, "session-1")
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.id, second.id)
        error = AgentCore._validate_grant(
            second, PermissionGrant.for_request(first)
        )
        self.assertIsNotNone(error)
        self.assertEqual(ErrorCode.INVALID_GRANT, error.code)


# --------------------------------------------------------------------------
# P1-C - approval display is bound to the attestation
# --------------------------------------------------------------------------


class ApprovalDisplayBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )
        for target, attribute in (
            (claude_code.subprocess, "Popen"),
            (claude_code.subprocess, "run"),
            (claude_code, "resolve_trusted_executable"),
        ):
            patcher = mock.patch.object(
                target, attribute, side_effect=AssertionError(attribute)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self) -> PermissionRequest:
        task = Task(
            objective="Analyze the fixture",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )
        task.estimate = CostEstimate()
        return self.connector.permission_request(task, "session-1")

    def tamper(self, request: PermissionRequest, mutate) -> PermissionRequest:
        data = request.to_dict()
        mutate(data)
        return PermissionRequest.from_dict(data)

    def test_every_displayed_security_field_is_bound_to_the_fingerprint(self) -> None:
        request = self.request()
        baseline = request.fingerprint

        def set_reason(d):
            d["reason"] = "A totally different reason"

        def set_preview_summary(d):
            d["outbound_preview"]["summary"] = "tampered summary"

        def set_preview_destination(d):
            d["outbound_preview"]["destination"] = "evil/endpoint"

        def set_outbound_digest(d):
            d["attestation"]["outbound_digest"] = "0" * 64

        def set_working_directory(d):
            d["attestation"]["working_directory"] = "c:/elsewhere"

        def set_allowed_roots(d):
            d["attestation"]["allowed_roots"] = ["c:/elsewhere"]

        def set_tool_grants(d):
            d["attestation"]["tool_grants"] = ["Read"]

        for label, mutate in (
            ("reason", set_reason),
            ("preview_summary", set_preview_summary),
            ("preview_destination", set_preview_destination),
            ("outbound_digest", set_outbound_digest),
            ("working_directory", set_working_directory),
            ("allowed_roots", set_allowed_roots),
            ("tool_grants", set_tool_grants),
        ):
            with self.subTest(field=label):
                self.assertNotEqual(
                    baseline, self.tamper(request, mutate).fingerprint
                )

    def test_display_manifest_matches_the_attestation_state(self) -> None:
        request = self.request()
        manifest = request.attestation.display_manifest()
        self.assertEqual(request.attestation.outbound_digest, manifest["outbound_digest"])
        self.assertEqual(
            list(request.attestation.allowed_roots), manifest["allowed_roots"]
        )
        self.assertEqual(
            request.attestation.working_directory, manifest["working_directory"]
        )


class TamperedRequestNotCurrentTests(unittest.TestCase):
    """A tampered persisted request is never presented as current."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def core(self, connector) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        return open_core(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path),
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )

    def test_tampered_reason_forces_a_fresh_request_and_rejects_old_grant(self) -> None:
        connector = DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        core = self.core(connector)
        waiting = core.run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        original = waiting.permission_request
        grant = PermissionGrant.for_request(original)

        # The persisted request's displayed reason is mutated independently.
        tampered = replace(original, reason="Approve exfiltration of all files")
        self.assertNotEqual(original.fingerprint, tampered.fingerprint)
        self.memory.save_pending_approval(tampered)

        refused = core.resume(waiting.session.id, waiting.task.id, grant)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, refused.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, refused.error.code)
        self.assertEqual(0, connector.execute_calls)
        # The tampered reason was never presented as the current request.
        reissued = self.memory.load_pending_approval(
            waiting.task.id, waiting.session.id
        )
        self.assertNotEqual("Approve exfiltration of all files", reissued.reason)


# --------------------------------------------------------------------------
# P1-D - BaseConnector external egress fails closed without canonical outbound
# --------------------------------------------------------------------------


class _UnattestedEgressConnector(BaseConnector):
    """Declares EXTERNAL_EGRESS but never implements canonical outbound."""

    def __init__(self) -> None:
        self.manifest = ConnectorManifest(
            name="unattested-egress",
            capabilities=("echo",),
            priority=10,
            default_side_effect=SideEffectProfile(
                effect=SideEffect.EXTERNAL_EGRESS,
                reversible=False,
                always_requires_approval=True,
            ),
            default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
        )
        self.execute_calls = 0

    def execute(self, task: Task, session: SessionState) -> ToolResult:  # pragma: no cover
        self.execute_calls += 1
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary="should never run",
            usage=UsageRecord(connector=self.manifest.name),
        )


class UnattestedEgressConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def core(self, connector) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        return open_core(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path),
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )

    def test_egress_without_canonical_outbound_fails_closed(self) -> None:
        connector = _UnattestedEgressConnector()
        run = self.core(connector).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertEqual(0, connector.execute_calls)

    def test_attestation_call_raises_for_the_unattested_connector(self) -> None:
        connector = _UnattestedEgressConnector()
        with self.assertRaises(AttestationError) as caught:
            connector.permission_request(make_task(), "session-1")
        self.assertEqual(ErrorCode.EGRESS_REFUSED, caught.exception.code)

    def test_implementing_egress_connector_still_follows_normal_flow(self) -> None:
        # DummyConnector implements canonical_outbound, so a declared egress
        # reaches the approval gate normally instead of failing closed.
        connector = DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        run = self.core(connector).run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertEqual(0, connector.execute_calls)


class LocalReadAttestationTests(unittest.TestCase):
    """M3 - the local reader is the first real consumer of H1's local
    resource boundary, and it must not become an egress path."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "notes.txt"
        self.target.write_text("alpha\n", encoding="utf-8")
        self.connector = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )

    def task(self, target: Path | str | None = None) -> Task:
        return Task(
            objective="read",
            capability="file_read",
            scope=ActionScope(
                action="read",
                target=str(self.target if target is None else target),
                data_classification=DataClassification.INTERNAL,
            ),
        )

    def test_the_attestation_binds_the_canonical_root_set(self) -> None:
        request = self.connector.permission_request(self.task(), "session-1")
        attestation = request.attestation
        self.assertIsNotNone(attestation)
        self.assertTrue(attestation.working_directory)
        self.assertEqual((attestation.working_directory,), attestation.allowed_roots)
        self.assertEqual((), attestation.tool_grants)
        self.assertIs(SideEffect.LOCAL_READ, attestation.side_effect.effect)

    def test_a_local_read_grants_no_external_tools_and_sends_no_bytes(self) -> None:
        # ``canonical_outbound`` is deliberately NOT overridden: the base
        # empty-bytes default is legitimate precisely because this connector
        # declares no external egress.
        self.assertEqual(b"", self.connector.canonical_outbound(self.task()))
        self.assertEqual((), self.connector.external_tool_grants(self.task()))

    def test_a_target_outside_the_roots_cannot_be_attested(self) -> None:
        outside = self.root.parent / "outside.txt"
        with self.assertRaises(AttestationError) as caught:
            self.connector.permission_request(self.task(outside), "session-1")
        self.assertEqual(ErrorCode.EGRESS_REFUSED, caught.exception.code)

    def test_a_denied_secret_file_can_never_be_attested(self) -> None:
        denied = self.root / ".env"
        denied.write_text("SECRET=1\n", encoding="utf-8")
        with self.assertRaises(AttestationError) as caught:
            self.connector.permission_request(self.task(denied), "session-1")
        self.assertEqual(ErrorCode.EGRESS_REFUSED, caught.exception.code)
        # The refusal names the policy rule, never the file content.
        self.assertNotIn("SECRET=1", str(caught.exception))

    def test_the_fingerprint_follows_the_symlink_resolved_target(self) -> None:
        # This is what makes a post-approval symlink repoint invalidate the
        # grant instead of silently reading a different file.
        other = self.root / "other.txt"
        other.write_text("other\n", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            os.symlink(self.target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlink creation not permitted")
        before = self.connector.permission_request(self.task(link), "s").fingerprint
        link.unlink()
        os.symlink(other, link)
        after = self.connector.permission_request(self.task(link), "s").fingerprint
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
