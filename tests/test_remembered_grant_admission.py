"""A remembered grant is admitted only where the loop would reuse one.

Pre-existing since c7c9af9: Core admitted a ``remember_for_session`` grant on
a request that is not rememberable, the permission gate counted it, and the
execution claim - which consumes only one-shot grants - was LOST on every
resume (EXECUTION_IN_PROGRESS forever, nothing executed). Now:

A/B. Core refuses such a grant (INVALID_GRANT, nothing written); a following
     one-shot grant executes exactly once.
C.   A rememberable request in SESSION mode is remembered and reused.
D.   A legitimately remembered grant, then a switch to STRICT before
     execution: back to approval, never a stuck claim.
E.   The app and Core reuse predicates are one and the same.
F.   An existing out-of-policy remember row authorizes nothing, and a task
     already stuck under the old code recovers through a fresh approval.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import itertools
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis.contracts import (
    ErrorCode,
    PermissionGrant,
    PermissionMode,
    RiskLevel,
    RunStatus,
)
from jarvis.destinations import CLAUDE_CODE_DESTINATION, DestinationGrant
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, make_task, open_app, open_core, safe_cleanup


class GrantCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.database = Path(temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)

    def policy(self, connector: DummyConnector) -> frozenset:
        return frozenset({DestinationGrant(connector.manifest.name, DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION)})

    def core(self, mode: PermissionMode, connector: DummyConnector):
        registry = ToolRegistry()
        registry.register(connector)
        settings = Settings(memory_database=self.database, permission_mode=mode)
        return open_core(registry, self.memory, settings, destination_policy=self.policy(connector))

    def count(self, table: str) -> int:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def waiting(self, core, task, session_id=None):
        run = core.run(task, session_id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, run.error)
        return run

    def assertRefusedRemember(self, core, run) -> None:
        refused = core.resume(run.session.id, run.task.id,
                              PermissionGrant.for_request(run.permission_request, remember_for_session=True))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, refused.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, refused.error.code)
        self.assertEqual(0, self.count("permission_grants"))
        self.assertEqual(0, self.count("execution_attempts"))
        self.assertEqual("awaiting_approval", self.memory.load_task(run.task.id).status.value)

    def assertOneShotExecutesOnce(self, core, connector, run) -> None:
        pending = self.memory.load_pending_approval(run.task.id, run.session.id)
        done = core.resume(run.session.id, run.task.id, PermissionGrant.for_request(pending))
        self.assertEqual(RunStatus.COMPLETED, done.status, done.error)
        self.assertEqual(1, connector.execute_calls)


class AdmissionTests(GrantCase):
    def test_a_mandatory_request_refuses_a_remember_grant(self) -> None:
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(PermissionMode.SESSION, connector)
        run = self.waiting(core, make_task(always_requires_approval=True))
        self.assertTrue(run.permission_request.side_effect.mandatory)
        self.assertRefusedRemember(core, run)
        self.assertOneShotExecutesOnce(core, connector, run)

    def test_b_a_strict_mode_request_refuses_a_remember_grant(self) -> None:
        connector = DummyConnector()
        core = self.core(PermissionMode.STRICT, connector)
        run = self.waiting(core, make_task())
        self.assertFalse(run.permission_request.side_effect.mandatory)
        self.assertRefusedRemember(core, run)
        self.assertOneShotExecutesOnce(core, connector, run)

    def test_c_a_rememberable_request_is_remembered_and_reused(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        core = self.core(PermissionMode.SESSION, connector)
        run = self.waiting(core, make_task(risk=RiskLevel.MEDIUM))
        done = core.resume(run.session.id, run.task.id,
                           PermissionGrant.for_request(run.permission_request, remember_for_session=True))
        self.assertEqual(RunStatus.COMPLETED, done.status, done.error)
        again = core.run(make_task(risk=RiskLevel.MEDIUM), run.session.id)
        self.assertEqual(RunStatus.COMPLETED, again.status, again.error)          # reused, not asked again
        self.assertEqual(2, connector.execute_calls)
        self.assertEqual(1, self.count("permission_grants"))


class ModeSwitchTests(GrantCase):
    def test_d_remembered_then_strict_goes_back_to_approval(self) -> None:
        connector = DummyConnector(risk=RiskLevel.MEDIUM)
        session_core = self.core(PermissionMode.SESSION, connector)
        run = self.waiting(session_core, make_task(risk=RiskLevel.MEDIUM))
        # The remembered grant is admitted and stored, then the worker dies
        # before the attempt is opened.
        with mock.patch.object(self.memory, "open_execution_attempt", side_effect=RuntimeError("worker died")):
            with self.assertRaises(RuntimeError):
                session_core.resume(run.session.id, run.task.id,
                                    PermissionGrant.for_request(run.permission_request, remember_for_session=True))
        self.assertEqual(1, self.count("permission_grants"))
        self.assertEqual(0, connector.execute_calls)
        strict_core = self.core(PermissionMode.STRICT, connector)
        resumed = strict_core.resume(run.session.id, run.task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, resumed.status, resumed.error)
        self.assertNotEqual(ErrorCode.EXECUTION_IN_PROGRESS, getattr(resumed.error, "code", None))
        self.assertEqual(0, connector.execute_calls)
        self.assertOneShotExecutesOnce(strict_core, connector, run)


class PredicateAgreementTests(GrantCase):
    def test_e_app_and_core_share_one_predicate(self) -> None:
        connector = DummyConnector()
        for mode in PermissionMode:
            app = open_app(settings=Settings(memory_database=self.database, permission_mode=mode),
                           memory=self.memory, connectors=(connector,), destination_policy=self.policy(connector))
            base = connector.permission_request(make_task(), "s-matrix")
            for risk, mandatory, reversible in itertools.product(RiskLevel, (False, True), (False, True)):
                request = replace(
                    base,
                    risk_level=risk,
                    side_effect=replace(base.side_effect, always_requires_approval=mandatory, reversible=reversible),
                )
                expected = (mode is PermissionMode.SESSION and not request.side_effect.mandatory
                            and risk < RiskLevel.HIGH and reversible)
                with self.subTest(mode=mode.value, risk=risk.name, mandatory=mandatory, reversible=reversible):
                    self.assertEqual(expected, app.core._remembered_permission_allowed(request))
                    self.assertEqual(expected, app._remembrance_allowed(request))


class ExistingRowTests(GrantCase):
    def remembered_row(self, run) -> None:
        """What the old admission wrote: a remember=1 grant on a mandatory request."""
        grant = PermissionGrant.for_request(run.permission_request, remember_for_session=True)
        self.assertTrue(self.memory.resolve_approval(run.permission_request, grant, "approved"))
        self.assertEqual(1, self.count("permission_grants"))

    def test_f_an_out_of_policy_remember_row_authorizes_nothing(self) -> None:
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(PermissionMode.SESSION, connector)
        run = self.waiting(core, make_task(always_requires_approval=True))
        self.remembered_row(run)
        resumed = core.resume(run.session.id, run.task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, resumed.status, resumed.error)
        self.assertEqual(0, connector.execute_calls)
        self.assertEqual(0, self.count("execution_attempts"))
        # Nor does it cover a second task of the same scope in the session.
        second = core.run(make_task(always_requires_approval=True), run.session.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, second.status)
        self.assertOneShotExecutesOnce(core, connector, run)

    def test_f_a_task_stuck_under_the_old_code_recovers_through_a_fresh_approval(self) -> None:
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(PermissionMode.SESSION, connector)
        run = self.waiting(core, make_task(always_requires_approval=True))

        # Reproduce the OLD flow exactly: the grant is persisted as remember=1
        # (old admission) and the gate counts any approved row (old gate).
        def old_gate(session_id, task_id, fingerprint):
            with contextlib.closing(sqlite3.connect(self.database)) as connection:
                return connection.execute(
                    "SELECT 1 FROM permission_grants WHERE session_id = ? AND task_id = ? AND fingerprint = ? "
                    "AND approved = 1", (session_id, task_id, fingerprint)).fetchone() is not None

        resolve = self.memory.resolve_approval
        with mock.patch.object(self.memory, "has_approved_grant", side_effect=old_gate), \
                mock.patch.object(self.memory, "resolve_approval",
                                  side_effect=lambda r, g, d: resolve(r, replace(g, remember_for_session=True), d)):
            stuck = core.resume(run.session.id, run.task.id, PermissionGrant.for_request(run.permission_request))
        self.assertEqual(ErrorCode.EXECUTION_IN_PROGRESS, stuck.error.code)       # the old stuck shape
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual([("prepared",)], connection.execute("SELECT state FROM execution_attempts").fetchall())
        recovered = core.resume(run.session.id, run.task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, recovered.status, recovered.error)
        self.assertEqual(0, connector.execute_calls)
        self.assertOneShotExecutesOnce(core, connector, run)
        self.assertEqual(1, self.count("execution_attempts"))                     # the PREPARED row, claimed once
