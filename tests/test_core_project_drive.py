"""PM26B: project slices run as 4.27 chain steps through core.resume().

drive and build_ports are faked at the module boundary; no worker, model,
check process or network call runs, and the DB lives under TEMP.
"""

import inspect
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis.contracts import MemoryStore, RunStatus, TaskStatus
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.work_controls import Budget, Usage
from jarvis.work_ledger import EventKind, RunSize, RunState, StepState, advance_run
from jarvis.work_plan import Plan, PlanBudget, PlanCheck, PlanSlice
from tests.test_work_driver import FAIL, FakePorts, result

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
        self.drive_now = NOW + timedelta(minutes=2)
        self.core.project_drive = lambda run_id, step: {
            "boot_id": "new", "now": self.drive_now, "lease": LEASE,
            "granted_targets": frozenset(self.granted), "usage": Usage(),
            "large_budget": Budget(10, 7200, 50), "worker": self.worker,
        }
        self.build = mock.patch("jarvis.work_ports.build_ports", return_value=mock.Mock()).start()
        mock.patch("jarvis.work_driver.drive", side_effect=self.fake_drive).start()
        self.addCleanup(mock.patch.stopall)

    def fake_drive(self, run, steps, ports, *, only):
        self.drive_calls.append(only)
        self.worker.start(only)
        if self.outcome is StepState.ACCEPTED:
            self.applied.append(only)
        return run, tuple(
            replace(s, state=self.outcome, updated_at=NOW + timedelta(minutes=3)) if s.key == only else s
            for s in steps
        )

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
        # PM26W1FIX3: a gate block is a revision, so the task waits (not FAILED) with the evidence kept;
        # a check failed past the cap saves FAILED (RealDriverTests.test_f1_failed_after_cap_...).
        first, _ = self.ready()
        self.outcome = StepState.BLOCKED_BY_GATE
        result = self.resume(first)
        self.assertIs(result.status, RunStatus.FAILED)
        self.assertEqual(self.applied, [])
        self.assertIn("ended blocked_by_gate (run building)", result.result.summary)
        self.assertIsNot(self.memory.load_task(first.id).status, TaskStatus.FAILED)
        last = [step for step in self.memory.work_steps("r") if step.ordinal == 1][-1]
        self.assertIs(last.state, StepState.BLOCKED_BY_GATE)

    # PM26W1FIX3 F2: a slice with no target is refused before any run, lease or task state changes.
    def test_f2_empty_slice_targets_refused_before_state_changes(self):
        self.plan = replace(
            self.plan,
            slices=(PlanSlice("s1", "repair a", (), ("c1",)), PlanSlice("s2", "repair b", ("src/b.py",), ("c1",))),
        )
        first, _ = self.ready()
        before_run, before_steps = self.memory.latest_work_run("r"), self.memory.work_steps("r")
        result = self.resume(first)
        self.assert_refused(result, "no_targets")
        self.assertEqual(self.drive_calls, [])
        self.assertEqual(self.memory.latest_work_run("r"), before_run)
        self.assertEqual(self.memory.work_steps("r"), before_steps)
        self.assertIsNot(self.memory.load_task(first.id).status, TaskStatus.FAILED)

    def test_f2_step_ordinal_past_the_plan_refused_before_state_changes(self):
        first, _ = self.ready()
        real = self.core._project_run_payloads

        def no_slices(run_id):
            head, rest = real(run_id)
            return {**head, "plan": {**head["plan"], "slices": head["plan"]["slices"][:0]}}, rest

        before_run, before_steps = self.memory.latest_work_run("r"), self.memory.work_steps("r")
        with mock.patch.object(self.core, "_project_run_payloads", no_slices):
            result = self.resume(first)
        self.assert_refused(result, "no_slice")
        self.assertEqual(self.drive_calls, [])
        self.assertEqual(self.memory.latest_work_run("r"), before_run)
        self.assertEqual(self.memory.work_steps("r"), before_steps)
        self.assertIsNot(self.memory.load_task(first.id).status, TaskStatus.FAILED)

    # PM26W1FIX2 F1: only terminal outcomes save the chain task FAILED.
    def test_f1_movable_step_outcomes_keep_the_task_resumable(self):
        for state in (StepState.PENDING, StepState.ACCEPTING, StepState.NEEDS_INPUT,
                      StepState.BLOCKED_BY_GATE, StepState.TIMEOUT):
            with self.subTest(state=state):
                self.setUp()
                first, _ = self.ready()
                self.outcome = state
                result = self.resume(first)
                self.assertIs(result.status, RunStatus.FAILED)
                self.assertIn("waiting:", result.result.summary)
                self.assertIsNot(self.memory.load_task(first.id).status, TaskStatus.FAILED)
                self.outcome = StepState.ACCEPTED
                self.assertIs(self.resume(first).status, RunStatus.COMPLETED)
                self.assertEqual(len(self.drive_calls), 2)
                self.assertIs(self.memory.load_task(first.id).status, TaskStatus.COMPLETED)

    def test_f1_terminal_step_outcomes_save_the_task_failed(self):
        # FAILED_AFTER_CAP needs a reached revision cap: RealDriverTests drives it for real.
        first, _ = self.ready()
        self.outcome = StepState.CANCELLED
        self.assertIs(self.resume(first).status, RunStatus.FAILED)
        saved = self.memory.load_task(first.id)
        self.assertIs(saved.status, TaskStatus.FAILED)
        self.assertIn("cancelled", str(saved.result_snapshot))

    def test_f1_unmapped_outcome_fails_closed_with_reason(self):
        first, _ = self.ready()
        self.outcome = StepState.RUNNING
        self.assertIs(self.resume(first).status, RunStatus.FAILED)
        saved = self.memory.load_task(first.id)
        self.assertIs(saved.status, TaskStatus.FAILED)
        self.assertIn("running", str(saved.result_snapshot))

    # PM26W1FIX2 F2: every permitted target of the slice is leased; the targets map is the slice's.
    def test_f2_multi_target_slice_leases_every_target_and_sees_only_its_own(self):
        self.plan = replace(
            self.plan,
            slices=(PlanSlice("s1", "repair a", ("src/a.py", "lib/c.py", "src/a.py"), ("c1",)),
                    PlanSlice("s2", "repair b", ("src/b.py",), ("c1",))),
            targets=("src/a.py", "src/b.py", "lib/c.py"),
        )
        self.granted = {"src/", "lib/"}
        first, _ = self.ready()
        self.assertIs(self.resume(first).status, RunStatus.COMPLETED)
        kwargs = self.build.call_args.kwargs
        self.assertEqual(sorted(kwargs["lease_target"]), ["lib/c.py", "src/a.py"])
        self.assertEqual(kwargs["targets"], {"src/a.py": "file", "lib/c.py": "file"})


class RealDriverTests(CoreProjectDriveTests):
    """PM26W1FIX: the real work_driver.drive behind a FakePorts, so run moves are exercised."""

    def setUp(self):
        super().setUp()
        mock.patch.stopall()
        self.ports = None
        self.scripts = {"s1": [result()], "s2": [result()]}
        self.port_kw = {}
        mock.patch("jarvis.work_ports.build_ports", side_effect=self.build_fake).start()

    def build_fake(self, **_):
        script = dict(self.port_kw)
        no_root_cause = script.pop("root_cause", True) is None
        unbooted = script.pop("unbooted", False)
        self.ports = FakePorts(self.scripts, **script)
        self.ports.now = self.drive_now
        if no_root_cause:
            self.ports.root_cause = None  # like production build_ports: no root_cause port
        if unbooted:
            self.ports.boot_scan = None  # drive acts on nothing: run and steps come back unchanged
        return self.ports

    def run_state(self):
        return self.memory.latest_work_run("r").state

    def step_states(self):
        latest = {}
        for step in self.memory.work_steps("r"):
            latest[step.ordinal] = step.state
        return [latest[n] for n in sorted(latest)]

    def test_two_slices_driven_in_turn_run_accepting_only_after_last(self):
        first, second = self.ready()
        self.assertIs(self.resume(first).status, RunStatus.COMPLETED)
        self.assertEqual([len(w.tasks) for w in self.ports.workers.values()], [1, 0])  # once per step
        self.assertIs(self.run_state(), RunState.BUILDING)
        self.assertEqual(self.step_states(), [StepState.ACCEPTED, StepState.PENDING])
        self.assertIs(self.resume(second).status, RunStatus.COMPLETED)
        self.assertEqual([len(w.tasks) for w in self.ports.workers.values()], [0, 1])
        self.assertIs(self.run_state(), RunState.ACCEPTING)
        self.assertEqual(self.step_states(), [StepState.ACCEPTED, StepState.ACCEPTED])

    def test_held_non_final_slice_still_pauses_the_run(self):
        # F2: a held slice (gate fail, no valid root cause) is an N6-13 attention item even
        # while slice 2 could move; the per-step guard must not hide that pause.
        first, second = self.ready()
        self.port_kw = {"regress": [FAIL], "root_cause": None}
        self.assertIs(self.resume(first).status, RunStatus.FAILED)
        self.assertIs(self.run_state(), RunState.PAUSED)
        self.assertEqual(len(self.ports.workers["s2"].tasks), 0)
        self.assertIn("refused: not_resumable", self.resume(second).result.summary)

    def test_last_slice_reached_out_of_order_still_waits_for_the_first(self):
        first, second = self.ready()
        self.assertIs(self.resume(second).status, RunStatus.COMPLETED)
        self.assertIs(self.run_state(), RunState.BUILDING)
        self.assertIs(self.resume(first).status, RunStatus.COMPLETED)
        self.assertIs(self.run_state(), RunState.ACCEPTING)

    # PM26W1FIX2 F1 with the real driver: the review's three probes.
    def test_f1_budget_keeps_the_task_resumable(self):
        first, _ = self.ready()
        self.port_kw = {"budget_exhausted": [True]}
        result = self.resume(first)
        self.assertIs(result.status, RunStatus.FAILED)
        self.assertIs(self.run_state(), RunState.BLOCKED_BUDGET)
        self.assertIsNot(self.memory.load_task(first.id).status, TaskStatus.FAILED)
        # Waiting on budget: a later resume is refused, never saved FAILED, and the task stays open.
        self.assertIn("refused: not_resumable", self.resume(first).result.summary)
        self.assertIsNot(self.memory.load_task(first.id).status, TaskStatus.FAILED)

    def test_f1_needs_input_keeps_the_task_resumable(self):
        first, _ = self.ready()
        self.scripts["s1"] = [result(summary="question?")]
        self.assertIs(self.resume(first).status, RunStatus.FAILED)
        self.assertIs(self.step_states()[0], StepState.NEEDS_INPUT)
        self.assertIsNot(self.memory.load_task(first.id).status, TaskStatus.FAILED)

    def test_f1_unbooted_drive_keeps_the_task_resumable(self):
        first, _ = self.ready()
        self.port_kw = {"unbooted": True}
        self.assertIs(self.resume(first).status, RunStatus.FAILED)
        self.assertIs(self.run_state(), RunState.BUILDING)
        self.assertEqual(self.step_states()[0], StepState.PENDING)
        self.assertIsNot(self.memory.load_task(first.id).status, TaskStatus.FAILED)
        self.port_kw = {}
        self.assertIs(self.resume(first).status, RunStatus.COMPLETED)

    # PM26W1FIX3 F1: a revisable step left by a budget block, run back to BUILDING, then an unbooted drive.
    def test_f1_revisable_step_under_unbooted_drive_keeps_the_task_resumable(self):
        cases = {
            StepState.BLOCKED_BY_GATE: ([result()], {"scope_check": [["out_of_scope:y"]]}),
            StepState.TIMEOUT: ([result("timeout"), result()], {}),
        }
        for state, (script, extra) in cases.items():
            with self.subTest(state=state):
                self.setUp()
                first, _ = self.ready()
                self.scripts["s1"] = script
                self.port_kw = {"budget_exhausted": [False, True], **extra}
                self.assertIs(self.resume(first).status, RunStatus.FAILED)
                self.assertIs(self.run_state(), RunState.BLOCKED_BUDGET)
                self.assertIs(self.step_states()[0], state)
                blocked_at = self.memory.latest_work_run("r").updated_at
                self.core.advance_project_run("r", RunState.BUILDING, boot_id="new", at=blocked_at)
                self.drive_now = blocked_at + timedelta(minutes=1)
                self.port_kw = {"unbooted": True}
                result_ = self.resume(first)
                self.assertIs(result_.status, RunStatus.FAILED)
                self.assertIn("waiting:", result_.result.summary)
                self.assertIs(self.run_state(), RunState.BUILDING)
                self.assertIs(self.step_states()[0], state)
                self.assertIsNot(self.memory.load_task(first.id).status, TaskStatus.FAILED)
                # Booted again, the kept task reaches the driver and the revision is attempted (not lost).
                self.scripts["s1"], self.port_kw = [result()], {"budget_exhausted": [False]}
                self.assertNotIn("refused", self.resume(first).result.summary)
                self.assertIn(("claim", "s1"), [entry[:2] for entry in self.ports.log])

    def test_f1_failed_after_cap_saves_the_task_failed(self):
        first, _ = self.ready()
        self.scripts["s1"] = [result()] * 4
        self.port_kw = {"regress": [FAIL] * 4}
        self.assertIs(self.resume(first).status, RunStatus.FAILED)
        self.assertIs(self.memory.load_task(first.id).status, TaskStatus.FAILED)

    # The fake-drive wiring tests do not apply to the real driver.
    test_a1_each_slice_is_one_chain_task_driven_once_per_resume = None
    test_a2_gates_rechecked_before_every_step = None
    test_a3_n5_applied_step_is_not_applied_again = None
    test_a4_not_resumable_run_starts_no_worker = None
    test_n1_frozen_policy = None
    test_n2_narrowed_scope = None
    test_n3_plan_hash_mismatch = None
    test_n4_failed_check_means_no_apply_and_failed_with_evidence = None
    test_f1_movable_step_outcomes_keep_the_task_resumable = None
    test_f1_terminal_step_outcomes_save_the_task_failed = None
    test_f1_unmapped_outcome_fails_closed_with_reason = None
    test_f2_multi_target_slice_leases_every_target_and_sees_only_its_own = None
    test_f2_empty_slice_targets_refused_before_state_changes = None


class WorkStepProtocolTests(unittest.TestCase):
    """INT-TRAIN34: AgentCore's work-step calls must be MemoryStore members."""

    def test_work_step_methods_are_declared_with_the_store_signatures(self) -> None:
        for name in ("append_work_step", "work_steps"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(MemoryStore, name), name)
                declared = inspect.signature(getattr(MemoryStore, name))
                concrete = inspect.signature(getattr(SQLiteMemoryStore, name))
                self.assertEqual(list(declared.parameters), list(concrete.parameters))


if __name__ == "__main__":
    unittest.main()
