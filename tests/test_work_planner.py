"""PM11: the planner call after a confirmed read-back, driven by fake models."""

import json
import os
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jarvis import clarify, work_planner
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.work_controls import Usage
from jarvis.work_ledger import EventKind, RunState
from jarvis.work_plan import Plan
from tests.test_work_clarify_dialog import CONV, IDEA, REST, ROUND1, T0, FakeModel, round2
from tests.test_work_plan import example


class FakePlanner:
    """Scripted plan replies; records every context."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, context):
        self.calls.append(context)
        return self.replies.pop(0)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "x.db"
        self.memory = SQLiteMemoryStore(self.db)
        self.core = AgentCore(ToolRegistry(), self.memory)
        self.turn = 0
        self.root = str(self.dir / "proj")
        self.part1 = {"goal": "tarif saklamak", "users": "ben", "platform": "Windows",
                      "target_location": self.root}

    def plan(self, **changes):
        return json.dumps({**example(), "project_root": self.root, **changes})

    def say(self, text, model, planner=None, *, boot="boot-1"):
        self.turn += 1
        return self.core.clarify_turn(
            CONV, text, turn=self.turn, boot_id=boot, at=T0 + timedelta(minutes=self.turn),
            propose=model, plan_model=planner,
        )

    def confirm(self, planner):
        model = FakeModel(questions=[ROUND1, round2(self.part1)], answers=[self.part1, REST])
        self.say(IDEA, model)
        self.say("evet", model)
        self.say("tarif saklamak, ben, Windows", model)
        self.assertTrue(self.say("geri kalanlar", model).startswith("Anladığım kadarıyla:"))
        run_id = self.memory.work_runs_open()[0].run_id
        return run_id, self.say("evet", model, planner)

    def last_payload(self, run_id):
        events = [e for e in self.memory.work_events(run_id) if e.kind is EventKind.RUN_STATE]
        return json.loads(events[-1].payload_json)

    def test_valid_plan_is_validated_classified_hashed_and_recorded(self):
        planner = FakePlanner(self.plan())
        run_id, reply = self.confirm(planner)
        self.assertEqual(reply, "Plan hazır (küçük); onaylanan özete bağlı olarak inşa aşamasına geçti.")
        run = self.memory.latest_work_run(run_id)
        self.assertIs(run.state, RunState.BUILDING)
        self.assertEqual(run.size.value, "small")
        payload = self.last_payload(run_id)
        self.assertEqual(payload["plan_hash"], Plan.from_json(self.plan()).plan_hash)
        self.assertEqual(payload["plan"], json.loads(self.plan()))
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(payload["to"], "building")
        self.assertIn("brief_hash", payload)
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(planner.calls[0]["refusals"], [])

    def test_invalid_then_valid_reasks_once(self):
        planner = FakePlanner("{not json", self.plan())
        run_id, _ = self.confirm(planner)
        self.assertEqual(len(planner.calls), 2)
        self.assertEqual(planner.calls[1]["attempt"], 2)
        self.assertEqual(planner.calls[1]["refusals"][0]["code"], "schema")
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.BUILDING)
        self.assertEqual(self.last_payload(run_id)["attempts"], 2)

    def test_three_invalid_plans_need_input_then_cancel(self):
        planner = FakePlanner("[]", "{}", self.plan(targets=["*.py"]))
        run_id, reply = self.confirm(planner)
        self.assertTrue(reply.startswith("Geçerli bir proje planı oluşturulamadı"))
        self.assertEqual(len(planner.calls), 3)
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.NEEDS_INPUT)
        self.assertEqual(self.last_payload(run_id)["refusals"][0]["code"], "glob")
        self.assertEqual(self.say("iptal", FakeModel()), "Peki, projeyi iptal ettim.")
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.CANCELLED)

    def test_oversized_plan_is_reasked(self):
        big = {**example()["budgets"], "worker_sessions": 11}
        planner = FakePlanner(self.plan(budgets=big), self.plan())
        run_id, _ = self.confirm(planner)
        self.assertIn("oversized", [r["code"] for r in planner.calls[1]["refusals"]])
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.BUILDING)

    def brief(self):
        return clarify.fill_brief(clarify.ProjectBrief(), {**self.part1, **REST})

    def test_byte_cap_and_glob_targets_are_refused(self):
        root, facts = work_planner.brief_facts(self.brief(), large_forced=False)
        huge = "x" * (work_planner.MAX_PLAN_BYTES + 1)
        result = work_planner.plan_project(self.brief(), root, facts, FakePlanner(huge, huge, huge))
        self.assertEqual({r.code for r in result.refusals}, {"oversized"})
        for char in "*?[]{}!":
            planner = FakePlanner(*[self.plan(targets=[f"a{char}.py"])] * 3)
            result = work_planner.plan_project(self.brief(), root, facts, planner)
            self.assertIsInstance(result, work_planner.PlanFailed, char)
            self.assertIn("glob", [r.code for r in result.refusals], char)
            self.assertEqual(len(planner.calls), 3)

    def test_model_cannot_claim_jarvis_repo_false(self):
        claimed = json.dumps({**json.loads(self.plan()), "jarvis_repo": False})
        planner = FakePlanner(claimed, self.plan())
        with mock.patch.object(work_planner, "JARVIS_ROOT", self.dir):
            run_id, reply = self.confirm(planner)
        self.assertEqual(reply, "Plan hazır (büyük); onayınız bekleniyor.")
        self.assertEqual(planner.calls[1]["refusals"][0]["code"], "schema")
        run = self.memory.latest_work_run(run_id)
        self.assertIs(run.state, RunState.AWAITING_APPROVAL)
        self.assertEqual(run.size.value, "large")
        self.assertTrue(self.last_payload(run_id)["facts"]["jarvis_repo"])
        self.assertTrue(self.last_payload(run_id)["jarvis_repo"])

    def test_root_above_the_jarvis_checkout_is_jarvis_repo(self):
        with mock.patch.object(work_planner, "JARVIS_ROOT", Path(self.root) / "JARVIS"):
            _, facts = work_planner.brief_facts(self.brief(), large_forced=False)
        self.assertTrue(facts["jarvis_repo"])
        _, facts = work_planner.brief_facts(self.brief(), large_forced=False)
        self.assertFalse(facts["jarvis_repo"])

    def test_chat_planned_run_resumes_after_restart(self):
        run_id, _ = self.confirm(FakePlanner(self.plan()))
        core = AgentCore(ToolRegistry(), SQLiteMemoryStore(self.db))
        self.assertEqual(core.recover_project_runs("boot-2", now=T0 + timedelta(hours=1)), [run_id])
        resumed = core.resume_project_run(
            run_id, boot_id="boot-2", now=T0 + timedelta(hours=1, minutes=1),
            granted_targets={"a.py"}, lease=timedelta(minutes=10), usage=Usage(),
        )
        self.assertIs(resumed.state, RunState.BUILDING)

    def test_nothing_executes(self):
        boom = AssertionError("executed")
        with mock.patch.object(subprocess, "run", side_effect=boom), \
                mock.patch.object(subprocess, "Popen", side_effect=boom), \
                mock.patch.object(os, "system", side_effect=boom):
            run_id, _ = self.confirm(FakePlanner(self.plan()))
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.BUILDING)
        self.assertFalse(Path(self.root).exists())

    def test_restart_mid_planning_is_interrupted_without_resume(self):
        def crash(context):
            raise SystemExit("power cut")

        with self.assertRaises(SystemExit):
            self.confirm(crash)
        run_id = self.memory.work_runs_open()[0].run_id
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.PLANNING)
        core = AgentCore(ToolRegistry(), SQLiteMemoryStore(self.db))
        self.assertEqual(core.recover_project_runs("boot-2", now=T0 + timedelta(hours=1)), [run_id])
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.INTERRUPTED)
        planner = FakePlanner(self.plan())
        self.core = core
        self.assertIsNone(self.say("evet", FakeModel(), planner, boot="boot-2"))
        self.assertEqual(planner.calls, [])
        self.assertIs(self.memory.latest_work_run(run_id).state, RunState.INTERRUPTED)


if __name__ == "__main__":
    unittest.main()
