"""Core-owned canonical destination policy.

Two separate questions, deliberately kept apart:

VALIDITY       is this canonical destination well formed and known to Core at
               all? ``DESTINATION_REGISTRY`` answers it, and the answer never
               authorizes anything.
AUTHORIZATION  may THIS connector reach it with THIS capability? Only exact
               membership of the Core-owned
               (connector, capability, canonical destination) tuple answers
               it. A registered connector, a truthful ``can_handle`` and a
               globally registered destination are each necessary and none of
               them - nor all three together - is sufficient.

A destination that arrives with a task, from task JSON, a chain declaration, a
model answer or a connector, is a REQUEST. Core decides, before any approval
request, grant, execution attempt or connector call exists.

Nothing here performs a live model, auth, subprocess or network call. Every
run uses a ``DummyConnector``, which starts no process, and every fail-closed
case asserts the connector was never executed at all.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis.app import TaskInputError, decode_task_json
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.connectors.local_files import (
    LocalFileReadConnector,
    LocalFileWriteConnector,
)
from jarvis.contracts import (
    ActionScope,
    AttestationError,
    ChainState,
    ChainStep,
    DataClassification,
    ErrorCode,
    PermissionGrant,
    PermissionMode,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    SideEffectProfile,
    Task,
)
from jarvis.core import AgentCore
from jarvis.destinations import (
    CLAUDE_CODE_DESTINATION,
    CONNECTOR_DESTINATION_POLICY,
    DEFAULT_TASK_DESTINATION,
    DESTINATION_REGISTRY,
    DestinationGrant,
    DestinationKind,
    canonical_destination,
    connector_destination_refusal,
    destination_refusal,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests import helpers
from tests.helpers import (
    DummyConnector,
    make_task,
    open_app,
    open_core,
    safe_cleanup,
)


#: A destination that is syntactically perfect and that Core does not own.
FOREIGN = "https://elsewhere.example.com/v1"

#: Destination A is the one this build ships. Destination B is registered as
#: VALID only inside the tests that need two endpoints; it is never shipped,
#: and registering it authorizes nobody for it.
DEST_A = CLAUDE_CODE_DESTINATION
DEST_B = "anthropic/claude-code-two"

CAP_A = "capability_alpha"
CAP_B = "capability_beta"


def with_destination_b():
    """Make DEST_B a VALID destination. Authorization is a separate table."""
    return mock.patch.dict(
        DESTINATION_REGISTRY, {DEST_B: DestinationKind.PROVIDER_ENDPOINT}
    )


def task_json(destination: str) -> str:
    return json.dumps(
        {
            "objective": "o",
            "capability": "code_analysis",
            "scope": {"action": "read", "destination": destination},
        }
    )


class EgressConnector(DummyConnector):
    """A named egress connector serving exactly one named capability."""

    def __init__(self, name: str, capability: str, **kwargs) -> None:
        super().__init__(name=name, side_effect=SideEffect.EXTERNAL_EGRESS, **kwargs)
        self.manifest = replace(self.manifest, capabilities=(capability,))


def grants(*rows: tuple[str, str, str]) -> frozenset[DestinationGrant]:
    """Exactly the tuples a test build authorizes. No wildcard, no default."""
    return frozenset(DestinationGrant(*row) for row in rows)


class ValidityIsNotAuthorizationTests(unittest.TestCase):
    """The two policy functions, and the line between them."""

    def test_the_registered_claude_destination_is_valid(self) -> None:
        self.assertIsNone(destination_refusal(DEST_A))
        self.assertIs(
            DestinationKind.PROVIDER_ENDPOINT, DESTINATION_REGISTRY[DEST_A]
        )

    def test_an_unregistered_destination_is_refused(self) -> None:
        for value in (
            FOREIGN,
            DEST_B,
            "gmail:someone@example.com",
            "github.com/acme/repo",
            "anthropic/claude-cod",
            DEST_A + "/extra",
        ):
            with self.subTest(destination=value):
                self.assertIsNotNone(destination_refusal(value))

    def test_a_malformed_or_non_canonical_destination_is_refused(self) -> None:
        for value in (
            None,
            7,
            b"anthropic/claude-code",
            ["anthropic/claude-code"],
            "Anthropic/Claude-Code",  # not case-folded
            " anthropic/claude-code",  # not stripped
            "anthropic/claude-code ",
            "anthropic/claude\x00-code",
            "anthropic/  claude-code",  # inner whitespace not collapsed
        ):
            with self.subTest(destination=repr(value)):
                self.assertIsNotNone(destination_refusal(value))

    def test_canonicalization_is_deterministic_and_idempotent(self) -> None:
        for spelling in (
            "Anthropic/Claude-Code",
            "  anthropic/claude-code  ",
            "ANTHROPIC/CLAUDE-CODE",
        ):
            with self.subTest(spelling=spelling):
                canonical = canonical_destination(spelling)
                self.assertEqual(DEST_A, canonical)
                self.assertEqual(canonical, canonical_destination(canonical))
        self.assertIsNone(canonical_destination(None))

    def test_the_empty_destination_can_never_carry_an_egress(self) -> None:
        self.assertIsNone(destination_refusal("", egress=False))
        self.assertIsNotNone(destination_refusal("", egress=True))

    def test_validity_alone_never_authorizes(self) -> None:
        policy = grants(("alpha", CAP_A, DEST_A))
        # Valid AND authorized.
        self.assertIsNone(
            connector_destination_refusal(
                policy,
                connector="alpha",
                capability=CAP_A,
                destination=DEST_A,
                egress=True,
            )
        )
        # Valid, and every single field wrong in turn.
        for connector, capability in (
            ("beta", CAP_A),
            ("alpha", CAP_B),
            ("beta", CAP_B),
        ):
            with self.subTest(connector=connector, capability=capability):
                self.assertIsNotNone(
                    connector_destination_refusal(
                        policy,
                        connector=connector,
                        capability=capability,
                        destination=DEST_A,
                        egress=True,
                    )
                )

    def test_the_shipped_policy_has_no_wildcard_row(self) -> None:
        for row in CONNECTOR_DESTINATION_POLICY:
            with self.subTest(row=row):
                for field in (row.connector, row.capability, row.destination):
                    self.assertNotIn(field, ("", "*", "**", "?", ".*", "any"))
        for wildcard in ("*", "**", "?", ".*", "any"):
            with self.subTest(wildcard=wildcard):
                self.assertNotIn(wildcard, DESTINATION_REGISTRY)

    def test_the_shipped_policy_is_exactly_the_current_live_combinations(self) -> None:
        # Read off the live manifests, not a second copy of the table.
        live = {
            (kind.manifest.name, capability, DEST_A)
            for kind in (
                ClaudeCodeConnector,
                LocalFileReadConnector,
                LocalFileWriteConnector,
            )
            for capability in kind.manifest.capabilities
        }
        self.assertEqual(
            live,
            {(row.connector, row.capability, row.destination)
             for row in CONNECTOR_DESTINATION_POLICY},
        )


class RequestedVersusApprovedTests(unittest.TestCase):
    """Requested target and Core-approved canonical destination stay apart."""

    def test_input_may_confirm_the_core_destination_but_never_redirect(self) -> None:
        for spelling in ("anthropic/claude-code", "Anthropic/Claude-Code"):
            with self.subTest(spelling=spelling):
                task = decode_task_json(task_json(spelling))
                self.assertEqual(DEFAULT_TASK_DESTINATION, task.scope.destination)

    def test_an_unregistered_requested_destination_is_refused_at_input(self) -> None:
        for requested in (FOREIGN, DEST_B, "gmail:a@example.com"):
            with self.subTest(requested=requested):
                with self.assertRaises(TaskInputError):
                    decode_task_json(task_json(requested))

    def test_naming_no_destination_takes_the_build_default(self) -> None:
        for requested in ("", "   "):
            with self.subTest(requested=repr(requested)):
                self.assertEqual(
                    DEFAULT_TASK_DESTINATION,
                    decode_task_json(task_json(requested)).scope.destination,
                )

    def test_the_input_boundary_checks_validity_and_never_authorization(self) -> None:
        # It cannot authorize: no connector is chosen yet. Passing here is
        # only a statement that the destination exists; the gate still has to
        # find an authorizing tuple, which ``CoreGateTests`` proves it does.
        with self.assertRaises(TaskInputError):
            decode_task_json(task_json(DEST_B))
        with with_destination_b():
            self.assertEqual(
                DEST_B, decode_task_json(task_json(DEST_B)).scope.destination
            )


class CoreGateTests(unittest.TestCase):
    """The gate: fail closed before the connector is ever asked to execute."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def build(self, connectors, policy, mode=PermissionMode.SESSION) -> AgentCore:
        registry = ToolRegistry()
        for connector in connectors:
            registry.register(connector)
        return AgentCore(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path, permission_mode=mode),
            destination_policy=policy,
        )

    def task(self, capability: str, destination: str, **preferences) -> Task:
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS, destination=destination)
        task.capability = capability
        task.preferences.update(preferences)
        return task

    def assert_refused(self, run, *connectors) -> None:
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        for connector in connectors:
            self.assertEqual(0, connector.execute_calls)

    # -- 1/2/3/4. each connector reaches only its own authorized destination

    def test_each_connector_reaches_only_its_own_authorized_tuple(self) -> None:
        alpha = EgressConnector("alpha", CAP_A)
        beta = EgressConnector("beta", CAP_B)
        policy = grants(("alpha", CAP_A, DEST_A), ("beta", CAP_B, DEST_B))
        with with_destination_b():
            core = self.build((alpha, beta), policy)
            # 1. connector A + capability A + destination A -> authorized.
            self.assertEqual(
                RunStatus.AWAITING_APPROVAL,
                core.run(self.task(CAP_A, DEST_A)).status,
            )
            # 2. connector B + capability B + destination B -> authorized.
            self.assertEqual(
                RunStatus.AWAITING_APPROVAL,
                core.run(self.task(CAP_B, DEST_B)).status,
            )
            # 3. connector A, its own valid capability, the OTHER registered
            #    destination -> no tuple, refused.
            self.assert_refused(core.run(self.task(CAP_A, DEST_B)), alpha, beta)
            # 4. and the mirror image.
            self.assert_refused(core.run(self.task(CAP_B, DEST_A)), alpha, beta)

    # -- 8. global registration authorizes nobody -------------------------

    def test_registering_a_destination_globally_authorizes_no_connector(self) -> None:
        alpha = EgressConnector("alpha", CAP_A)
        policy = grants(("alpha", CAP_A, DEST_A))
        core = self.build((alpha,), policy)
        with with_destination_b():
            # DEST_B is now perfectly valid, and alpha is a perfectly
            # registered connector that can_handle this task. Still refused.
            self.assertIsNone(destination_refusal(DEST_B))
            self.assert_refused(core.run(self.task(CAP_A, DEST_B)), alpha)

    # -- 5. preferred_connector cannot bypass -----------------------------

    def test_preferred_connector_cannot_bypass_the_tuple(self) -> None:
        # Both serve the SAME capability, so selection is decided by the
        # preference rather than by the registry's ranking.
        alpha = EgressConnector("alpha", CAP_A)
        beta = EgressConnector("beta", CAP_A)
        policy = grants(("alpha", CAP_A, DEST_A))
        core = self.build((alpha, beta), policy)
        run = core.run(self.task(CAP_A, DEST_A, preferred_connector="beta"))
        self.assert_refused(run, alpha, beta)
        # The same task routed to the authorized connector still works.
        self.assertEqual(
            RunStatus.AWAITING_APPROVAL,
            core.run(self.task(CAP_A, DEST_A, preferred_connector="alpha")).status,
        )

    # -- 6/7. manifest widening and can_handle authorize nothing ----------

    def test_widening_the_manifest_cannot_create_authorization(self) -> None:
        alpha = EgressConnector("alpha", CAP_A)
        policy = grants(("alpha", CAP_A, DEST_A))
        core = self.build((alpha,), policy)
        # The connector claims a second capability AFTER registration, so
        # ``can_handle`` now answers True for it and the registry selects it.
        alpha.manifest = replace(alpha.manifest, capabilities=(CAP_A, CAP_B))
        widened = self.task(CAP_B, DEST_A)
        self.assertTrue(alpha.can_handle(widened))
        self.assert_refused(core.run(widened), alpha)

    def test_can_handle_returning_true_cannot_create_authorization(self) -> None:
        class AlwaysHandles(EgressConnector):
            def can_handle(self, task) -> bool:
                return True

        alpha = AlwaysHandles("alpha", CAP_A)
        core = self.build((alpha,), grants(("alpha", CAP_A, DEST_A)))
        task = self.task(CAP_B, DEST_A)
        self.assertTrue(alpha.can_handle(task))
        self.assert_refused(core.run(task), alpha)

    def test_a_connector_cannot_authorize_its_own_destination(self) -> None:
        class SelfAuthorizing(EgressConnector):
            """Declares a destination of its own in every channel it owns."""

            def permission_request(self, task, session_id, ttl=30, **kwargs):
                request = super().permission_request(task, session_id, ttl, **kwargs)
                scope = replace(request.scope, destination=DEST_B).normalized()
                return replace(
                    request,
                    scope=scope,
                    attestation=replace(request.attestation, scope=scope),
                )

        policy = grants(("alpha", CAP_A, DEST_A))
        with with_destination_b():
            # (a) the connector alone declares it.
            alpha = SelfAuthorizing("alpha", CAP_A)
            self.assert_refused(
                self.build((alpha,), policy).run(self.task(CAP_A, DEST_A)), alpha
            )
            # (b) task and connector AGREE on it: still no tuple, still refused.
            beta = SelfAuthorizing("beta", CAP_A)
            self.assert_refused(
                self.build((beta,), policy).run(self.task(CAP_A, DEST_B)), beta
            )

    # -- unregistered destination / connector / capability ----------------

    def test_an_unregistered_destination_fails_closed_before_execution(self) -> None:
        alpha = EgressConnector("alpha", CAP_A)
        core = self.build((alpha,), grants(("alpha", CAP_A, DEST_A)))
        run = core.run(self.task(CAP_A, FOREIGN))
        self.assert_refused(run, alpha)
        self.assertIsNone(
            self.memory.load_pending_approval(run.task.id, run.session.id)
        )

    def test_an_egress_naming_no_destination_fails_closed(self) -> None:
        alpha = EgressConnector("alpha", CAP_A)
        core = self.build((alpha,), grants(("alpha", CAP_A, DEST_A)))
        self.assert_refused(core.run(self.task(CAP_A, "")), alpha)

    def refuse_reason(self, core: AgentCore, connector, task: Task, scope=None) -> str:
        floor = SideEffectProfile(
            effect=SideEffect.EXTERNAL_EGRESS, always_requires_approval=True
        )
        with self.assertRaises(AttestationError) as caught:
            core._destination_policy_refusal(
                task, connector, scope or task.scope.normalized(), floor
            )
        self.assertEqual(ErrorCode.EGRESS_REFUSED, caught.exception.code)
        return str(caught.exception)

    def test_an_unregistered_connector_fails_closed(self) -> None:
        registered = EgressConnector("alpha", CAP_A)
        core = self.build((registered,), grants(("alpha", CAP_A, DEST_A)))
        # Same manifest NAME, different object: an impostor cannot inherit the
        # registered connector's standing, authorized tuple or not.
        impostor = EgressConnector("alpha", CAP_A)
        self.assertIn(
            "registered",
            self.refuse_reason(core, impostor, self.task(CAP_A, DEST_A)),
        )
        stranger = EgressConnector("stranger", CAP_A)
        self.assertIn(
            "registered",
            self.refuse_reason(core, stranger, self.task(CAP_A, DEST_A)),
        )

    def test_an_unserved_capability_fails_closed(self) -> None:
        alpha = EgressConnector("alpha", CAP_A)
        core = self.build((alpha,), grants(("alpha", CAP_A, DEST_A)))
        task = self.task(CAP_A, DEST_A)
        task.capability = "not_a_capability"
        self.assertIn("capability", self.refuse_reason(core, alpha, task))

    def test_a_malformed_canonical_destination_fails_closed(self) -> None:
        alpha = EgressConnector("alpha", CAP_A)
        core = self.build((alpha,), grants(("alpha", CAP_A, DEST_A)))
        task = self.task(CAP_A, DEST_A)
        for bad in ("Anthropic/Claude-Code", " anthropic/claude-code", FOREIGN, 7):
            with self.subTest(destination=repr(bad)):
                scope = ActionScope(
                    action="read",
                    target="resource-a",
                    connector="alpha",
                    destination="",
                    data_classification=DataClassification.INTERNAL,
                ).normalized()
                # Set AFTER normalization: this is the tampered/persisted
                # shape the gate must refuse rather than quietly repair.
                object.__setattr__(scope, "destination", bad)
                self.assertIsNotNone(self.refuse_reason(core, alpha, task, scope))


class ChainAndResumeTests(unittest.TestCase):
    """Chain steps and persisted tasks meet the same tuple, not a weaker one."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")
        self.connector = EgressConnector("alpha", CAP_A, risk=RiskLevel.MEDIUM)
        registry = ToolRegistry()
        registry.register(self.connector)
        self.registry = registry

    def core(self, policy) -> AgentCore:
        return AgentCore(
            self.registry,
            self.memory,
            Settings(
                memory_database=self.memory.database_path,
                permission_mode=PermissionMode.SESSION,
            ),
            destination_policy=policy,
        )

    def chain_step_task(self, core: AgentCore, destination: str) -> Task:
        session = SessionState(objective="chain")
        self.memory.save_session(session)
        chain = ChainState.declared(
            (
                ChainStep(
                    objective="analyse the notes",
                    capability=CAP_A,
                    action="analyze",
                    destination=destination,
                ),
            )
        )
        self.assertIsNotNone(chain)
        task, reason = core.build_chain_step_task(session, chain, 0)
        self.assertIsNone(reason)
        self.assertEqual(destination, task.scope.destination)
        return task, session

    # -- 9. chain steps ----------------------------------------------------

    def test_a_chain_step_is_bound_by_the_same_tuple(self) -> None:
        with with_destination_b():
            core = self.core(grants(("alpha", CAP_A, DEST_A)))
            # The declaration is free to NAME a valid destination; naming is
            # not authorization, and the step task meets the same gate.
            task, session = self.chain_step_task(core, DEST_B)
            run = core.run(task, session.id)
            self.assertEqual(RunStatus.FAILED, run.status)
            self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
            self.assertEqual(0, self.connector.execute_calls)
            # The authorized destination, declared the same way, is allowed.
            task, session = self.chain_step_task(core, DEST_A)
            self.assertEqual(
                RunStatus.AWAITING_APPROVAL, core.run(task, session.id).status
            )

    # -- 10. persisted / resumed / tampered --------------------------------

    def test_a_tampered_persisted_task_is_re_checked_before_execution(self) -> None:
        with with_destination_b():
            core = self.core(grants(("alpha", CAP_A, DEST_A)))
            task = make_task(effect=SideEffect.EXTERNAL_EGRESS, destination=DEST_A)
            task.capability = CAP_A
            first = core.run(task)
            self.assertEqual(RunStatus.AWAITING_APPROVAL, first.status)
            grant = PermissionGrant.for_request(first.permission_request)
            # The destination is swapped in STORAGE, to one that is valid but
            # that no tuple authorizes for this connector.
            stored = self.memory.load_task(first.task.id)
            stored.scope = replace(stored.scope, destination=DEST_B)
            self.memory.save_task(first.session.id, stored)
            resumed = core.resume(first.session.id, first.task.id, grant)
        self.assertEqual(RunStatus.FAILED, resumed.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, resumed.error.code)
        self.assertEqual(0, self.connector.execute_calls)

    # -- 11. destination swap between two AUTHORIZED tuples ----------------

    def test_a_swap_to_an_authorized_destination_still_rejects_the_old_grant(
        self,
    ) -> None:
        # Both tuples are authorized here, so the ONLY thing that can catch
        # the swap is the approval binding.
        with with_destination_b():
            core = self.core(
                grants(("alpha", CAP_A, DEST_A), ("alpha", CAP_A, DEST_B))
            )
            task = make_task(effect=SideEffect.EXTERNAL_EGRESS, destination=DEST_A)
            task.capability = CAP_A
            first = core.run(task)
            self.assertEqual(RunStatus.AWAITING_APPROVAL, first.status)
            grant = PermissionGrant.for_request(first.permission_request)
            stored = self.memory.load_task(first.task.id)
            stored.scope = replace(stored.scope, destination=DEST_B)
            self.memory.save_task(first.session.id, stored)
            resumed = core.resume(first.session.id, first.task.id, grant)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, resumed.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, resumed.error.code)
        self.assertNotEqual(
            first.permission_request.fingerprint,
            resumed.permission_request.fingerprint,
        )
        self.assertEqual(DEST_B, resumed.permission_request.scope.destination)
        self.assertEqual(0, self.connector.execute_calls)


class ApprovalBindingTests(unittest.TestCase):
    """An approval binds ONE Core-authorized canonical destination."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")
        self.connector = EgressConnector("alpha", CAP_A, risk=RiskLevel.MEDIUM)
        registry = ToolRegistry()
        registry.register(self.connector)
        # TWO authorized tuples, declared explicitly. Fingerprint separation
        # is proved between two destinations this build really permits.
        self.core = AgentCore(
            registry,
            self.memory,
            Settings(
                memory_database=self.memory.database_path,
                permission_mode=PermissionMode.SESSION,
            ),
            destination_policy=grants(
                ("alpha", CAP_A, DEST_A), ("alpha", CAP_A, DEST_B)
            ),
        )

    def run_for(self, destination: str, session_id: str | None = None):
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS, destination=destination)
        task.capability = CAP_A
        return self.core.run(task, session_id)

    def test_the_destination_is_bound_into_every_layer_of_the_approval(self) -> None:
        with with_destination_b():
            first = self.run_for(DEST_A)
            second = self.run_for(DEST_B, first.session.id)
        for run in (first, second):
            self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        a, b = first.permission_request, second.permission_request
        self.assertNotEqual(a.scope_fingerprint, b.scope_fingerprint)
        self.assertNotEqual(a.attestation.digest, b.attestation.digest)
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_a_grant_for_one_destination_cannot_authorize_another(self) -> None:
        with with_destination_b():
            first = self.run_for(DEST_A)
            other = self.run_for(DEST_B, first.session.id)
            stolen = replace(
                PermissionGrant.for_request(first.permission_request),
                task_id=other.task.id,
            )
            resumed = self.core.resume(first.session.id, other.task.id, stolen)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, resumed.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, resumed.error.code)
        self.assertEqual(0, self.connector.execute_calls)

    def test_an_authorized_tuple_still_executes_once_granted(self) -> None:
        first = self.run_for(DEST_A)
        granted = self.core.resume(
            first.session.id,
            first.task.id,
            PermissionGrant.for_request(first.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, granted.status)
        self.assertEqual(1, self.connector.execute_calls)


class TestCompositionIsFailClosedTests(unittest.TestCase):
    """The FIXTURE helpers must be as fail-closed as production is.

    A test helper that manufactures authorization rows from whatever happens
    to be registered would mask exactly the missing-policy bug this
    architecture exists to catch, so these prove that ``open_core`` and
    ``open_app`` derive nothing at all.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")
        self.connector = EgressConnector("helper-alpha", CAP_A)
        self.registry = ToolRegistry()
        self.registry.register(self.connector)

    def core(self, policy) -> AgentCore:
        return open_core(
            self.registry,
            self.memory,
            Settings(
                memory_database=self.memory.database_path,
                permission_mode=PermissionMode.SESSION,
            ),
            destination_policy=policy,
        )

    def task(self, capability: str = CAP_A, destination: str = DEST_A) -> Task:
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS, destination=destination)
        task.capability = capability
        return task

    # -- 1. registered, capable, valid destination, NO row -> refused ------

    def test_a_registered_connector_without_a_row_is_refused(self) -> None:
        task = self.task()
        # Everything a permissive helper would have keyed on is true here.
        self.assertIn("helper-alpha", self.registry.list_names())
        self.assertIn(CAP_A, self.connector.manifest.capabilities)
        self.assertTrue(self.connector.can_handle(task))
        self.assertIsNone(destination_refusal(DEST_A))

        run = self.core(frozenset()).run(task)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertIsNone(
            self.memory.load_pending_approval(run.task.id, run.session.id)
        )
        self.assertIsNone(self.memory.latest_execution_attempt(run.task.id))
        self.assertEqual(0, self.connector.execute_calls)

    # -- 2. the exact row, and only then --------------------------------

    def test_the_exact_row_enables_the_normal_approval_path(self) -> None:
        policy = frozenset(
            {
                DestinationGrant(
                    connector="helper-alpha", capability=CAP_A, destination=DEST_A
                )
            }
        )
        run = self.core(policy).run(self.task())
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertEqual(DEST_A, run.permission_request.scope.destination)
        self.assertEqual(0, self.connector.execute_calls)

    # -- 3. widening the manifest does not widen the fixture policy -------

    def test_widening_the_manifest_does_not_widen_the_fixture_policy(self) -> None:
        policy = frozenset(
            {DestinationGrant("helper-alpha", CAP_A, DEST_A)}
        )
        core = self.core(policy)
        self.connector.manifest = replace(
            self.connector.manifest, capabilities=(CAP_A, CAP_B)
        )
        widened = self.task(capability=CAP_B)
        self.assertTrue(self.connector.can_handle(widened))
        run = core.run(widened)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertEqual(0, self.connector.execute_calls)
        # The granted capability still works on the same build.
        self.assertEqual(
            RunStatus.AWAITING_APPROVAL, core.run(self.task()).status
        )

    # -- 4. a second valid destination is not auto-granted ----------------

    def test_registering_a_second_destination_grants_nothing(self) -> None:
        policy = frozenset({DestinationGrant("helper-alpha", CAP_A, DEST_A)})
        core = self.core(policy)
        with with_destination_b():
            self.assertIsNone(destination_refusal(DEST_B))
            run = core.run(self.task(destination=DEST_B))
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertEqual(0, self.connector.execute_calls)

    # -- 5. the helpers derive nothing ------------------------------------

    def test_the_helpers_hold_exactly_the_policy_they_were_given(self) -> None:
        for given in (
            frozenset(),
            frozenset({DestinationGrant("helper-alpha", CAP_A, DEST_A)}),
            frozenset({DestinationGrant("someone-else", CAP_B, DEST_A)}),
        ):
            with self.subTest(policy=sorted(map(str, given))):
                self.assertEqual(given, self.core(given).destination_policy)
                app = open_app(
                    settings=Settings(
                        memory_database=self.memory.database_path,
                        permission_mode=PermissionMode.SESSION,
                    ),
                    memory=self.memory,
                    registry=self.registry,
                    destination_policy=given,
                )
                self.assertEqual(given, app.core.destination_policy)

    def test_the_helpers_refuse_to_run_without_a_declared_policy(self) -> None:
        # Omission is a TypeError, never a permissive default: a new fixture
        # cannot become authorized by forgetting to say so.
        with self.assertRaises(TypeError):
            open_core(self.registry, self.memory, Settings())
        with self.assertRaises(TypeError):
            open_app(settings=Settings(), memory=self.memory, registry=self.registry)

    def test_no_helper_reconstructs_grants_from_manifests(self) -> None:
        # There is no derivation function left to call under any name.
        for banned in ("build_destination_policy", "derive_destination_policy"):
            with self.subTest(name=banned):
                self.assertFalse(hasattr(helpers, banned))
        source = Path(helpers.__file__).read_text(encoding="utf-8")
        # The two attributes a derivation would have to read.
        for reader in ("list_names(", ".manifest.capabilities"):
            with self.subTest(reader=reader):
                self.assertNotIn(reader, source)


class ClaudePathUnchangedTests(unittest.TestCase):
    """The shipped Claude path keeps its tuple, its destination and its bytes."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )

    def task(self) -> Task:
        return Task(
            objective="Analyze the fixture without changing files",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination=DEST_A,
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )

    def test_the_shipped_policy_authorizes_the_existing_claude_tuples(self) -> None:
        for capability in self.connector.manifest.capabilities:
            with self.subTest(capability=capability):
                self.assertIsNone(
                    connector_destination_refusal(
                        CONNECTOR_DESTINATION_POLICY,
                        connector=self.connector.manifest.name,
                        capability=capability,
                        destination=DEST_A,
                        egress=True,
                    )
                )

    def test_the_claude_destination_is_the_one_core_registered(self) -> None:
        self.assertEqual(
            DEST_A, self.connector.outbound_preview(self.task()).destination
        )
        self.assertIsNone(destination_refusal(DEST_A, egress=True))

    def test_the_canonical_outbound_bytes_are_the_prompt_and_nothing_else(self) -> None:
        task = self.task()
        prompt, _ = self.connector._build_prompt(task)
        self.assertEqual(
            prompt.encode("utf-8"), self.connector.canonical_outbound(task)
        )
        self.assertNotIn(b"destination policy", self.connector.canonical_outbound(task))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
