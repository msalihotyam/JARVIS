"""PM12: the approval gate between a validated plan and building."""

import dataclasses
import json
import unittest
from datetime import timedelta
from pathlib import Path

from jarvis.contracts import WorkApproval
from jarvis.core import AgentCore, ResumeRefused
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.work_controls import Budget, Usage
from jarvis.work_ledger import EventKind, RunSize, RunState, WorkLedgerError, advance_run
from jarvis.work_plan import Plan, PlanSlice
from tests import test_work_planner as planner_tests
from tests.test_work_resume import GRANTED, LEASE, T0, WorkResumeTests

DAY = timedelta(days=1)


class ApprovalGateTests(unittest.TestCase):
    setUp = WorkResumeTests.setUp

    def awaiting(self, run_id="r1", boot_id="old"):
        self.core.create_project_run(
            run_id, self.plan, project_root=self.root, jarvis_repo=False,
            size=RunSize.LARGE, boot_id=boot_id, at=T0,
        )
        return self.core.advance_project_run(run_id, RunState.AWAITING_APPROVAL, boot_id=boot_id, at=T0)

    def approve(self, run_id="r1", plan_hash=None, **overrides):
        kwargs = dict(approver="operator", boot_id="old", at=T0 + timedelta(minutes=1), expires_at=T0 + DAY)
        kwargs.update(overrides)
        return self.core.approve_project_plan(run_id, plan_hash or self.plan.plan_hash, **kwargs)

    def resume(self, core=None, **overrides):
        kwargs = dict(boot_id="new", now=T0 + timedelta(minutes=5), granted_targets=GRANTED,
                      lease=LEASE, usage=Usage(), large_budget=Budget(10, 7200, 50))
        kwargs.update(overrides)
        return (core or self.core).resume_project_run("r1", **kwargs)

    def other_plan(self):
        return dataclasses.replace(
            self.plan, goal="fix a differently",
            slices=(PlanSlice("s1", "fix a differently", ("src/a.py",), ("c1",)),),
        )

    def payloads(self, run_id="r1"):
        return [json.loads(e.payload_json) for e in self.memory.work_events(run_id)]

    def test_large_needs_an_operator_approval(self):
        self.awaiting()
        with self.assertRaisesRegex(WorkLedgerError, "approval refused: no approval"):
            self.core.advance_project_run("r1", RunState.BUILDING, boot_id="old", at=T0)
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.AWAITING_APPROVAL)
        self.assertIs(self.approve().state, RunState.BUILDING)
        record = self.payloads()[-1]["approval"]
        self.assertEqual(record, {
            "run_id": "r1", "plan_hash": self.plan.plan_hash, "approver": "operator",
            "approved_at": int((T0 + timedelta(minutes=1)).timestamp()), "expires_at": int((T0 + DAY).timestamp()),
        })

    def test_small_without_a_readback_cannot_build(self):
        self.core.create_project_run(
            "r1", self.plan, project_root=self.root, jarvis_repo=False,
            size=RunSize.SMALL, boot_id="old", at=T0,
        )
        with self.assertRaisesRegex(WorkLedgerError, "no approval"):
            self.core.advance_project_run("r1", RunState.BUILDING, boot_id="old", at=T0)
        with self.assertRaisesRegex(WorkLedgerError, "awaiting approval"):
            self.approve()
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.PLANNING)

    def test_hash_mismatch_is_refused_and_stays_awaiting(self):
        self.awaiting()
        events = self.memory.work_events("r1")
        for wrong in ("0" * 64, self.other_plan().plan_hash):
            with self.assertRaisesRegex(WorkLedgerError, "another run or plan"):
                self.approve(plan_hash=wrong)
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.AWAITING_APPROVAL)
        self.assertEqual(self.memory.work_events("r1"), events)

    def test_replanned_run_needs_an_approval_of_the_new_plan(self):
        self.awaiting()
        other = self.other_plan()
        self.core.advance_project_run("r1", RunState.PLANNING, boot_id="old", at=T0, extra={
            "plan": other.to_dict(), "plan_hash": other.plan_hash,
        })
        self.core.advance_project_run("r1", RunState.AWAITING_APPROVAL, boot_id="old", at=T0)
        with self.assertRaisesRegex(WorkLedgerError, "another run or plan"):
            self.approve()
        self.assertIs(self.approve(plan_hash=other.plan_hash).state, RunState.BUILDING)
        # Resume revalidates and leases the plan that was approved, not the first one.
        self.assertEqual(self.core._project_run_payloads("r1")[0]["plan_hash"], other.plan_hash)

    def test_model_or_unknown_approver_is_refused(self):
        self.awaiting()
        for approver in ("model", "assistant", "readback", "", "Operator"):
            with self.subTest(approver=approver):
                with self.assertRaisesRegex(WorkLedgerError, "approver not allowed"):
                    self.approve(approver=approver)
                with self.assertRaisesRegex(WorkLedgerError, "approver not allowed"):
                    self.core.reject_project_plan("r1", approver=approver, boot_id="old", at=T0)
        forged = {"run_id": "r1", "plan_hash": self.plan.plan_hash, "approver": "operator",
                  "approved_at": 0, "expires_at": 4102444800}
        with self.assertRaisesRegex(WorkLedgerError, "only by approve_project_plan"):
            self.core.advance_project_run("r1", RunState.PAUSED, boot_id="old", at=T0, extra={"approval": forged})
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.AWAITING_APPROVAL)

    def test_an_approval_of_another_run_does_not_unlock(self):
        self.awaiting("r1")
        self.awaiting("r2")
        self.approve("r2")
        with self.assertRaisesRegex(WorkLedgerError, "no approval"):
            self.core.advance_project_run("r1", RunState.BUILDING, boot_id="old", at=T0 + timedelta(minutes=2))

    def test_forged_records_in_the_log_are_refused(self):
        hash_ = self.plan.plan_hash
        good = {"run_id": "r1", "plan_hash": hash_, "approver": "operator",
                "approved_at": int(T0.timestamp()), "expires_at": int((T0 + DAY).timestamp())}
        cases = {
            "another run or plan": [{**good, "run_id": "r2"}, {**good, "plan_hash": "0" * 64}],
            "approver not allowed": [{**good, "approver": "model"}],
            "read-back does not bind": [{**good, "approver": "readback", "brief_hash": "b"}],
            "malformed approval": [{**good, "approved_at": True}, {**good, "expires_at": good["approved_at"]}],
            "expired or not yet valid": [{**good, "approved_at": int((T0 + DAY).timestamp()),
                                          "expires_at": int((T0 + 2 * DAY).timestamp())}],
        }
        n = 0
        for reason, records in cases.items():
            for record in records:
                n += 1
                run_id = f"f{n}"
                with self.subTest(reason=reason, record=record):
                    run = self.awaiting(run_id)
                    down = advance_run(run, RunState.INTERRUPTED, at=T0)
                    self.memory.append_work_run(down, event_kind=EventKind.RUN_STATE, event_payload={
                        "from": "awaiting_approval", "to": "interrupted", "approval": {**record, "run_id": (
                            record["run_id"] if record["run_id"] != "r1" else run_id)},
                    }, prior=run)
                    self.core.advance_project_run(run_id, RunState.AWAITING_APPROVAL, boot_id="new", at=T0)
                    with self.assertRaisesRegex(WorkLedgerError, reason):
                        self.core.advance_project_run(run_id, RunState.BUILDING, boot_id="new", at=T0)

    def test_expiry_is_honoured(self):
        self.awaiting()
        with self.assertRaisesRegex(WorkLedgerError, "malformed approval"):
            self.approve(expires_at=T0)
        self.assertIs(self.approve(expires_at=T0 + timedelta(minutes=3)).state, RunState.BUILDING)
        self.assertEqual(self.core.recover_project_runs("new", now=T0 + timedelta(minutes=4)), ["r1"])
        refused = self.resume()
        self.assertIsInstance(refused, ResumeRefused)
        self.assertEqual((refused.code, refused.detail), ("approval", "the approval is expired or not yet valid"))
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.INTERRUPTED)

    def test_plan_change_after_approval_is_refused(self):
        other = self.other_plan()
        # Resume revalidates the latest binding before the gate, so a rebound
        # plan is refused there already; either way the run never builds.
        for plan_hash, detail in ((other.plan_hash, "root_mismatch"),
                                  (self.plan.plan_hash, "the plan no longer matches its bound hash")):
            with self.subTest(detail=detail):
                self.setUp()
                self.awaiting()
                self.approve()
                self.core.advance_project_run("r1", RunState.INTERRUPTED, boot_id="old", at=T0 + timedelta(minutes=2),
                                              extra={"plan": other.to_dict(), "plan_hash": plan_hash})
                refused = self.resume()
                self.assertEqual((refused.code, refused.detail), ("plan", detail))
                self.assertIs(self.memory.latest_work_run("r1").state, RunState.INTERRUPTED)

    def test_reject_ends_cancelled(self):
        self.awaiting()
        run = self.core.reject_project_plan("r1", approver="operator", boot_id="old", at=T0, reason="no")
        self.assertIs(run.state, RunState.CANCELLED)
        payload = self.payloads()[-1]
        self.assertEqual((payload["rejected"], payload["approver"], payload["reason"]), (True, "operator", "no"))
        with self.assertRaisesRegex(WorkLedgerError, "awaiting approval"):
            self.approve()
        with self.assertRaisesRegex(WorkLedgerError, "awaiting approval"):
            self.core.reject_project_plan("r1", approver="operator", boot_id="old", at=T0)
        with self.assertRaises(WorkLedgerError):
            self.core.advance_project_run("r1", RunState.BUILDING, boot_id="old", at=T0)

    def test_restart_keeps_approvals_and_interrupted_rules(self):
        self.awaiting()
        self.approve()
        core = AgentCore(ToolRegistry(), SQLiteMemoryStore(Path(self.root) / "x.db"))
        self.assertEqual(core.recover_project_runs("new", now=T0 + timedelta(minutes=2)), ["r1"])
        self.assertIs(self.memory.latest_work_run("r1").state, RunState.INTERRUPTED)
        self.assertIs(self.resume(core).state, RunState.BUILDING)

    def test_restart_while_awaiting_resumes_to_awaiting(self):
        self.awaiting()
        self.assertEqual(self.core.recover_project_runs("new", now=T0 + timedelta(minutes=2)), ["r1"])
        with self.assertRaisesRegex(WorkLedgerError, "awaiting approval"):
            self.approve(boot_id="new", at=T0 + timedelta(minutes=3))
        self.assertIs(self.resume().state, RunState.AWAITING_APPROVAL)
        self.assertIs(self.approve(boot_id="new", at=T0 + timedelta(minutes=6)).state, RunState.BUILDING)


class WorkApprovalRecordTests(unittest.TestCase):
    def setUp(self):
        WorkResumeTests.setUp(self)
        self.envelope = self.plan.grant_envelope()

    def make(self, **changes):
        fields = dict(plan_hash=self.plan.plan_hash, envelope=self.envelope, approver="operator",
                      approved_at=100, expires_at=200)
        fields.update(changes)
        return WorkApproval(**fields)

    def test_record_binds_exactly_its_plan(self):
        approval = self.make()
        self.assertIs(approval.envelope.plan, self.plan)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            approval.plan_hash = "x"
        other = dataclasses.replace(self.plan, goal="other")
        for changes in ({"plan_hash": other.plan_hash}, {"plan_hash": None},
                        {"envelope": other.grant_envelope()}, {"envelope": self.envelope.to_dict()},
                        {"approver": ""}, {"approver": None}, {"approved_at": True},
                        {"expires_at": 200.0}, {"expires_at": 100}, {"expires_at": 99}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.make(**changes)

    def test_valid_window_and_plan_expiry(self):
        approval = self.make()
        self.assertEqual([approval.valid_at(t) for t in (99, 100, 199, 200)], [False, True, True, False])
        capped = self.make(expires_at=self.plan.expires_at + 10)
        self.assertTrue(capped.valid_at(self.plan.expires_at - 1))
        self.assertFalse(capped.valid_at(self.plan.expires_at))


class ChatApprovalTests(unittest.TestCase):
    setUp = planner_tests.PlannerTests.setUp
    plan = planner_tests.PlannerTests.plan
    say = planner_tests.PlannerTests.say
    confirm = planner_tests.PlannerTests.confirm
    last_payload = planner_tests.PlannerTests.last_payload

    def test_small_is_bound_to_the_confirmed_readback(self):
        run_id, _ = self.confirm(planner_tests.FakePlanner(self.plan()))
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.BUILDING)
        payload = self.last_payload(run_id)
        approval = payload["approval"]
        self.assertEqual((approval["run_id"], approval["plan_hash"], approval["approver"], approval["brief_hash"]),
                         (run_id, Plan.from_json(self.plan()).plan_hash, "readback", payload["brief_hash"]))
        self.assertEqual(approval["expires_at"], min(Plan.from_json(self.plan()).expires_at,
                                                     approval["approved_at"] + 86_400))

    def test_readback_expiry_is_core_capped_and_an_expired_plan_waits(self):
        far = json.dumps({**json.loads(self.plan()), "expires_at": 4102444800})
        run_id, _ = self.confirm(planner_tests.FakePlanner(far))
        approval = self.last_payload(run_id)["approval"]
        self.assertEqual(approval["expires_at"], approval["approved_at"] + 86_400)
        self.setUp()
        stale = json.dumps({**json.loads(self.plan()), "expires_at": 1})
        run_id, _ = self.confirm(planner_tests.FakePlanner(stale))
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.NEEDS_INPUT)
        payload = self.last_payload(run_id)
        self.assertNotIn("approval", payload)
        # expires_at before approved_at fails the record's own validation.
        self.assertEqual(payload["refusals"], [{"code": "approval", "detail": "malformed approval"}])

    def test_large_waits_for_approval_and_notifies(self):
        large = json.dumps({**json.loads(self.plan()), "connector_classes": ["local"]})
        run_id, reply = self.confirm(planner_tests.FakePlanner(large))
        self.assertEqual(reply, "Plan hazır (büyük); onayınız bekleniyor.")
        self.assertNotIn("approval", self.last_payload(run_id))
        points = [json.loads(e.payload_json).get("point") for e in self.memory.work_events(run_id)]
        self.assertIn("plan_ready", points)
        self.assertIn("approval_required", points)
        with self.assertRaisesRegex(WorkLedgerError, "no approval"):
            self.core.advance_project_run(run_id, RunState.BUILDING, boot_id="boot-1", at=T0 + timedelta(hours=1))
        run = self.core.approve_project_plan(
            run_id, Plan.from_json(large).plan_hash, approver="operator", boot_id="boot-1",
            at=T0 + timedelta(hours=1), expires_at=T0 + timedelta(hours=2),
        )
        self.assertIs(run.state, RunState.BUILDING)


if __name__ == "__main__":
    unittest.main()
