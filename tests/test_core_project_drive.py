"""PM26B: project slices run as 4.27 chain steps through core.resume().

drive and build_ports are faked at the module boundary; no worker, model,
check process or network call runs, and the DB lives under TEMP.
"""

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis.contracts import RunStatus, TaskStatus
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.work_controls import Budget, Usage
from jarvis.work_ledger import EventKind, RunSize, RunState, StepState, advance_run
from jarvis.work_plan import Plan, PlanBudget, PlanCheck, PlanSlice

NOW = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
LEASE = timedelta(minutes=10)
ROOT = "C:/PM26W1-project"


class CoreProjectDriveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(temp.cleanup)
        self.memory = SQLiteMemoryStore(Path(temp.name) / "drive.sqlite")
        self.core = AgentCore(ToolRegistry(), self.memory)
        self.plan = Plan(
            goal="repair source", project_root=ROOT,
            slices=(PlanSlice("s1", "repair a", ("src/a.py",), ("c1",)),
                    PlanSlice("s2", "repair b", ("src/b.py",), ("c1",))),
            targets=("src/a.py", "src/b.py"), connector_classes=(), operation_classes=(),
            checks=(PlanCheck("c1", ("python", "-m", "unittest", "tests.test_a"), 60, 4096, ()),),
            budgets=PlanBudget(10, 5, 3, 7200, 50), child_count=1,
            expires_at=4102444800, new_dependencies=(),
        )
        self.session = self.core.start_session("project")
        self.worker = mock.Mock()
        self.applied = []
        self.outcome = StepState.ACCEPTED
        self.granted = {"src/"}
        self.drive_calls = []
        self.core.project_drive = lambda run_id, step: {
            "boot_id": "new", "now": NOW + timedelta(minutes=2), "lease": LEASE,
            "granted_targets": frozenset(self.granted), "usage": Usage(),
            "large_budget": Budget(10, 7200, 50), "worker": self.worker,
        }
        self.build = mock.patch("jarvis.work_ports.build_ports", return_value=mock.Mock()).start()
        mock.patch("jarvis.work_driver.drive", side_effect=self.fake_drive).start()
        self.addCleanup(mock.patch.stopall)

    def fake_drive(self, run, steps, ports):
        self.drive_calls.append([step.key for step in steps])
        current = steps[-1]
        self.worker.start(current.key)
        if self.outcome is StepState.ACCEPTED:
            self.applied.append(current.key)
        return run, (*steps[:-1], replace(current, state=self.outcome, updated_at=NOW + timedelta(minutes=3)))

    def ready(self, *, approve=True, recover=True):
        self.core.create_project_run(
            "r", self.plan, project_root=ROOT, jarvis_repo=False,
            size=RunSize.LARGE, boot_id="old", at=NOW,
        )
        self.core.advance_project_run("r", RunState.AWAITING_APPROVAL, boot_id="old", at=NOW)
        if approve:
            self.core.approve_project_plan(
                "r", self.plan.plan_hash, approver="operator", boot_id="old", at=NOW,
                expires_at=NOW + timedelta(days=1),
            )
            if recover:
                self.core.recover_project_runs("new", now=NOW + timedelta(minutes=1))
        return self.core.create_project_slice_tasks(self.session.id, "r", at=NOW)

    def resume(self, task):
        return self.core.resume(self.session.id, task.id)

    def assert_refused(self, result, code):
        self.assertIs(result.status, RunStatus.FAILED)
        self.assertIn(f"refused: {code}", result.result.summary)
        self.worker.start.assert_not_called()
        self.build.assert_not_called()
        self.assertEqual(self.applied, [])

    def test_a1_each_slice_is_one_chain_task_driven_once_per_resume(self):
        tasks = self.ready()
        self.assertEqual([task.preferences["project_step"] for task in tasks], [1, 2])
        for count, task in enumerate(tasks, start=1):
            self.assertIs(self.resume(task).status, RunStatus.COMPLETED)
            self.assertEqual(len(self.drive_calls), count)
        self.assertEqual(self.worker.start.call_count, 2)
        self.assertEqual({step.state for step in self.memory.work_steps("r")[-2:]}, {StepState.ACCEPTED})

    def test_a2_gates_rechecked_before_every_step(self):
        first, second = self.ready()
        self.assertIs(self.resume(first).status, RunStatus.COMPLETED)
        self.core.execution_gate = lambda: False
        self.assertIn("refused: policy", self.resume(second).result.summary)
        self.core.execution_gate = lambda: True
        self.granted = {"src/a.py"}
        self.assertIn("refused: scope_narrowed", self.resume(second).result.summary)
        self.assertEqual(self.worker.start.call_count, 1)
        self.assertEqual(self.build.call_count, 1)

    def test_a3_n5_applied_step_is_not_applied_again(self):
        first, _ = self.ready()
        self.resume(first)
        again = self.resume(first)
        self.assertIs(again.status, RunStatus.COMPLETED)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(len(self.drive_calls), 1)
        self.assertIs(self.memory.load_task(first.id).status, TaskStatus.COMPLETED)

    def test_a4_not_resumable_run_starts_no_worker(self):
        first, _ = self.ready(approve=False)
        self.assert_refused(self.resume(first), "not_resumable")

    def test_n1_frozen_policy(self):
        first, _ = self.ready()
        self.core.execution_gate = lambda: False
        self.assert_refused(self.resume(first), "policy")

    def test_n2_narrowed_scope(self):
        first, _ = self.ready()
        self.granted = {"other/"}
        self.assert_refused(self.resume(first), "scope_narrowed")

    def test_n3_plan_hash_mismatch(self):
        first, _ = self.ready(recover=False)
        run = self.memory.latest_work_run("r")
        self.memory.append_work_run(
            advance_run(run, RunState.INTERRUPTED, at=NOW), event_kind=EventKind.INTERRUPTED,
            event_payload={"from": "approved", "to": "interrupted", "boot_id": "new",
                           "plan": self.plan.to_dict(), "plan_hash": "f" * 64,
                           "project_root": ROOT, "jarvis_repo": False}, prior=run,
        )
        self.assert_refused(self.resume(first), "plan")

    def test_n4_failed_check_means_no_apply_and_failed_with_evidence(self):
        first, _ = self.ready()
        self.outcome = StepState.BLOCKED_BY_GATE
        result = self.resume(first)
        self.assertIs(result.status, RunStatus.FAILED)
        self.assertEqual(self.applied, [])
        self.assertIn("blocked", result.result.summary)
        saved = self.memory.load_task(first.id)
        self.assertIs(saved.status, TaskStatus.FAILED)
        self.assertIn("blocked", str(saved.result_snapshot))
        last = [step for step in self.memory.work_steps("r") if step.ordinal == 1][-1]
        self.assertIs(last.state, StepState.BLOCKED_BY_GATE)


if __name__ == "__main__":
    unittest.main()
