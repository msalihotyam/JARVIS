"""PM05: project-run lifecycle, boot recovery and explicit crash resume."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis.core import AgentCore, ResumeRefused
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.work_controls import Budget, Usage
from jarvis.work_ledger import EventKind, RunSize, RunState, WorkEvent, WorkLedgerError, WorkRun
from jarvis.work_plan import Plan, PlanBudget, PlanCheck, PlanSlice

T0 = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=10)
TARGETS = ("src/a.py", "docs/")
GRANTED = frozenset({"src/", "docs/"})


def fold(target):
    return target.replace("\\", "/").upper().casefold()


class WorkResumeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.memory = SQLiteMemoryStore(Path(tmp.name) / "x.db")
        self.core = AgentCore(ToolRegistry(), self.memory)
        self.plan = Plan(
            goal="fix a", project_root=self.root,
            slices=(PlanSlice("s1", "fix a", ("src/a.py",), ("c1",)),),
            targets=TARGETS, connector_classes=(), operation_classes=(),
            checks=(PlanCheck("c1", ("python", "-m", "unittest", "tests.test_x"), 60, 4096, ()),),
            budgets=PlanBudget(10, 5, 3, 7200, 50), child_count=1,
            expires_at=4102444800, new_dependencies=(),
        )

    def killed_run(self):
        """A run building under boot "old" whose process then died."""
        self.core.create_project_run(
            "r1", self.plan, project_root=self.root, jarvis_repo=False,
            size=RunSize.LARGE, boot_id="old", at=T0,
        )
        self.core.advance_project_run("r1", RunState.AWAITING_APPROVAL, boot_id="old", at=T0)
        self.core.approve_project_plan(
            "r1", self.plan.plan_hash, approver="operator", boot_id="old", at=T0,
            expires_at=T0 + timedelta(days=1),
        )
        for target in TARGETS:
            self.memory.claim_work_lease(target, "r1", "old", now=T0, lease=LEASE)

    def resume(self, **overrides):
        kwargs = dict(
            boot_id="new", now=T0 + timedelta(minutes=2), granted_targets=GRANTED,
            lease=LEASE, usage=Usage(), large_budget=Budget(10, 7200, 50),
        )
        kwargs.update(overrides)
        return self.core.resume_project_run("r1", **kwargs)

    def test_killed_run_boots_interrupted_with_leases_released(self):
        self.killed_run()
        now = T0 + timedelta(minutes=1)
        self.assertEqual(self.core.recover_project_runs("new", now=now), ["r1"])
        run = self.memory.latest_work_run("r1")
        self.assertIs(run.state, RunState.INTERRUPTED)
        self.assertIs(run.resume_state, RunState.BUILDING)
        self.assertIs(self.memory.work_events("r1")[-1].kind, EventKind.INTERRUPTED)
        for target in TARGETS:
            row = self.memory.work_leases(fold(target))[-1]
            self.assertEqual(row["boot_id"], "old")
            self.assertEqual(row["claimed_at"], row["expires_at"])

    def test_boot_never_resumes(self):
        self.killed_run()
        self.core.recover_project_runs("new", now=T0 + timedelta(minutes=1))
        events = len(self.memory.work_events("r1"))
        self.assertEqual(self.core.recover_project_runs("new", now=T0 + timedelta(minutes=2)), [])
        self.assertEqual(self.core.recover_project_runs("newer", now=T0 + timedelta(minutes=3)), [])
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.INTERRUPTED)
        self.assertEqual(len(self.memory.work_events("r1")), events)

    def test_same_boot_run_is_not_interrupted(self):
        self.killed_run()
        self.assertEqual(self.core.recover_project_runs("old", now=T0), [])
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.BUILDING)

    def test_resume_after_scope_narrowed_is_refused(self):
        self.killed_run()
        self.core.recover_project_runs("new", now=T0 + timedelta(minutes=1))
        leases = [len(self.memory.work_leases(fold(t))) for t in TARGETS]
        result = self.resume(granted_targets=frozenset({"src/"}))
        self.assertIsInstance(result, ResumeRefused)
        self.assertEqual(result.code, "scope_narrowed")
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.INTERRUPTED)
        self.assertEqual([len(self.memory.work_leases(fold(t))) for t in TARGETS], leases)

    def test_resume_refused_when_execution_frozen(self):
        self.killed_run()
        self.core.recover_project_runs("new", now=T0 + timedelta(minutes=1))
        self.core.execution_gate = lambda: False
        self.assertEqual(self.resume().code, "policy")
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.INTERRUPTED)

    def test_resume_refused_when_budget_spent_releases_leases(self):
        self.killed_run()
        now = T0 + timedelta(minutes=2)
        self.core.recover_project_runs("new", now=T0 + timedelta(minutes=1))
        result = self.resume(usage=Usage(model_calls=50))
        self.assertEqual(result.code, "budget")
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.INTERRUPTED)
        for target in TARGETS:
            row = self.memory.work_leases(fold(target))[-1]
            self.assertEqual((row["boot_id"], row["claimed_at"]), ("new", row["expires_at"]))
        self.assertIs(self.resume(now=now).state, RunState.BUILDING)  # leases were freed

    def test_resume_refused_while_another_run_holds_a_target(self):
        self.killed_run()
        now = T0 + timedelta(minutes=1)
        self.core.recover_project_runs("new", now=now)
        self.memory.claim_work_lease("docs/", "r2", "new", now=now, lease=LEASE)
        self.assertEqual(self.resume().code, "lease")
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.INTERRUPTED)
        row = self.memory.work_leases(fold("src/a.py"))[-1]
        self.assertEqual(row["claimed_at"], row["expires_at"])  # partial claim undone

    def test_valid_resume_continues_in_saved_phase(self):
        self.killed_run()
        self.core.recover_project_runs("new", now=T0 + timedelta(minutes=1))
        run = self.resume()
        self.assertIs(run.state, RunState.BUILDING)
        self.assertIsNone(run.resume_state)
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.BUILDING)
        for target in TARGETS:
            row = self.memory.work_leases(fold(target))[-1]
            self.assertEqual((row["run_id"], row["boot_id"]), ("r1", "new"))
            self.assertGreater(row["expires_at"], row["claimed_at"])
        self.assertEqual(self.resume().code, "not_interrupted")

    def test_events_are_append_only(self):
        self.killed_run()
        self.core.recover_project_runs("new", now=T0 + timedelta(minutes=1))
        self.resume()
        events = self.memory.work_events("r1")
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(len(events), 5)
        with self.assertRaises(WorkLedgerError):
            self.memory.append_work_event(WorkEvent(
                run_id="r1", seq=1, kind=EventKind.RUN_STATE, at=T0,
                payload_json='{"to":"done"}',
            ))
        self.assertEqual(self.memory.work_events("r1"), events)

    def test_create_refuses_invalid_plan_and_duplicate_run(self):
        with self.assertRaises(WorkLedgerError):
            self.core.create_project_run(
                "r1", self.plan, project_root=self.root, jarvis_repo=True,
                size=RunSize.SMALL, boot_id="old", at=T0,
            )
        self.assertIsNone(self.memory.latest_work_run("r1"))
        self.killed_run()
        with self.assertRaises(WorkLedgerError):
            self.core.create_project_run(
                "r1", self.plan, project_root=self.root, jarvis_repo=False,
                size=RunSize.SMALL, boot_id="old", at=T0,
            )

    def test_run_snapshot_and_event_are_one_transaction(self):
        run = WorkRun(run_id="r2", state=RunState.PLANNING, created_at=T0, updated_at=T0, size=RunSize.SMALL)
        self.assertEqual(self.memory.append_work_run(run, event_kind=EventKind.RUN_STATE, event_payload={"to": "planning"}), 1)
        self.assertEqual(self.memory.work_events("r2")[0].at, T0)
        later = WorkRun(run_id="r2", state=RunState.BUILDING, created_at=T0, updated_at=T0 + LEASE, size=RunSize.SMALL)
        with mock.patch("jarvis.memory.WorkEvent", side_effect=WorkLedgerError("boom")):
            with self.assertRaises(WorkLedgerError):
                self.memory.append_work_run(later, event_kind=EventKind.RUN_STATE)
        with self.assertRaises(WorkLedgerError):
            self.memory.append_work_run(later, event_payload={"to": "building"})
        with self.assertRaises(WorkLedgerError):
            self.memory.append_work_run(later, event_kind=EventKind.RUN_STATE, event_payload={"x": float("nan")})
        self.assertEqual(self.memory.latest_work_run("r2"), run)
        self.assertEqual(len(self.memory.work_events("r2")), 1)
        self.assertEqual(self.memory.append_work_run(later, event_kind=EventKind.RUN_STATE), 2)

    def test_writers_from_one_base_cannot_fork_the_run(self):
        self.killed_run()
        base = self.memory.latest_work_run("r1")
        self.core.advance_project_run("r1", RunState.INTERRUPTED, boot_id="new", at=T0)
        events = self.memory.work_events("r1")
        with mock.patch.object(self.memory, "latest_work_run", return_value=base):
            with self.assertRaisesRegex(WorkLedgerError, "concurrently"):
                self.core.advance_project_run("r1", RunState.CANCELLED, boot_id="new", at=T0)
        with mock.patch.object(self.memory, "latest_work_run", return_value=None):
            with self.assertRaisesRegex(WorkLedgerError, "concurrently"):
                self.core.create_project_run(
                    "r1", self.plan, project_root=self.root, jarvis_repo=False,
                    size=RunSize.SMALL, boot_id="new", at=T0,
                )
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.INTERRUPTED)
        self.assertEqual(self.memory.work_events("r1"), events)

    def test_open_runs_skip_terminal_ones(self):
        self.killed_run()
        done = WorkRun(run_id="r0", state=RunState.DONE, created_at=T0, updated_at=T0, size=RunSize.SMALL)
        self.assertIsNone(self.memory.append_work_run(done))
        self.assertEqual([run.run_id for run in self.memory.work_runs_open()], ["r1"])
        self.assertEqual(self.core.recover_project_runs("new", now=T0), ["r1"])
        self.assertIs(self.memory.work_runs_open()[0].state, RunState.INTERRUPTED)


if __name__ == "__main__":
    unittest.main()
