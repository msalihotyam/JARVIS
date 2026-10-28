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
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


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
        # no declared action classes: those alone force large (CDXINT109 F2)
        return json.dumps({**example(), "project_root": self.root, "connector_classes": [],
                           "operation_classes": [], **changes})

    def checkout(self, name="JARVIS"):
        home = self.dir / name
        for sub in ("jarvis", "data", "projects"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        return home

    def overlap(self, home, root, *targets):
        """plan_project for root with JARVIS_ROOT = home; every reply targets targets."""
        root = str(root)
        reply = self.plan(project_root=root, targets=list(targets),
                          slices=[{**example()["slices"][0], "targets": list(targets)}])
        facts = dict(target_folders=[root], existing_repo=True, outgoing_actions=False,
                     money_actions=False, manual_checks=False, jarvis_repo=True)
        planner = FakePlanner(reply, reply, reply)
        with mock.patch.object(work_planner, "JARVIS_ROOT", home):
            return work_planner.plan_project(self.brief(), root, facts, planner), planner

    def assertRefused(self, outcome, code, calls=3):
        result, planner = outcome
        self.assertIsInstance(result, work_planner.PlanFailed)
        self.assertIn(code, [r.code for r in result.refusals])
        self.assertEqual(len(planner.calls), calls)

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
        self.assertEqual(payload["brief_hash"], clarify.brief_hash(self.brief()))
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

    def test_root_above_the_jarvis_checkout_is_refused_before_any_model_call(self):
        # V3, CDXINT109 F1: root=<parent>, "JARVIS/jarvis/memory.py"
        home = self.checkout()
        with mock.patch.object(work_planner, "JARVIS_ROOT", home):
            _, facts = work_planner.brief_facts(self.brief(), large_forced=False)
        self.assertFalse(facts["jarvis_repo"])  # self.root is a sibling
        self.part1["target_location"] = str(self.dir)
        with mock.patch.object(work_planner, "JARVIS_ROOT", home):
            _, facts = work_planner.brief_facts(self.brief(), large_forced=False)
        self.assertTrue(facts["jarvis_repo"])
        result, planner = self.overlap(home, self.dir, "JARVIS/jarvis/memory.py")
        self.assertEqual(result, work_planner.PlanFailed(
            (work_planner.Refusal("jarvis_ancestor", str(self.dir)),), 0))
        self.assertEqual(planner.calls, [])

    def test_root_at_the_checkout_sees_self_targets(self):  # V1
        home = self.checkout()
        self.assertRefused(self.overlap(home, home, "jarvis/memory.py"), "self_target")
        self.assertRefused(self.overlap(home, home, "data/x.txt"), "protected_path")

    def test_root_under_the_checkout_checks_targets_checkout_relative(self):  # V2, V8
        home = self.checkout()
        self.assertRefused(self.overlap(home, home / "jarvis", "memory.py"), "self_target")
        self.assertRefused(self.overlap(home, home / "jarvis", ".env"), "protected_path")
        self.assertRefused(self.overlap(home, home / "data", "x.txt"), "protected_path")
        # an ordinary file under a checkout subfolder still plans, as large
        result, _ = self.overlap(home, home / "projects", "a.py")
        self.assertIsInstance(result, work_planner.Planned)
        self.assertEqual(result.size, "large")

    def test_other_spellings_of_the_checkout_overlap(self):  # V4, V5
        home = self.checkout()
        for root in (str(home / "jarvis").upper(), str(home / "jarvis").lower(),
                     str(home / "jarvis") + ".", str(home / "jarvis") + " ",
                     str(home) + ". \\jarvis"):
            with self.subTest(root=root):
                self.assertRefused(self.overlap(home, root, "memory.py"), "self_target")

    def test_short_name_of_the_checkout_overlaps(self):  # V6
        home = self.checkout("JARVIS-checkout-long")
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.windll.kernel32.GetShortPathNameW(str(home / "jarvis"), buf, 1024)
        except (ImportError, AttributeError, OSError):
            self.skipTest("no GetShortPathNameW")
        if not size or "~" not in buf.value:
            self.skipTest("8.3 names disabled on this volume")
        self.assertRefused(self.overlap(home, buf.value, "memory.py"), "self_target")

    def test_links_into_the_checkout_overlap(self):  # V7
        home = self.checkout()
        made = []
        for name, source in (("junction", home / "jarvis"), ("symlink", home / "jarvis"),
                             ("junction-up", self.dir)):
            link = self.dir / "links" / name
            link.parent.mkdir(exist_ok=True)
            try:
                if name.startswith("junction"):
                    import _winapi
                    _winapi.CreateJunction(str(source), str(link))
                else:
                    os.symlink(source, link, target_is_directory=True)
            except (ImportError, AttributeError, OSError):
                continue
            # remove the link itself before the temp dir is cleaned up
            self.addCleanup(os.rmdir, link)
            made.append(name)
            with self.subTest(name=name):
                if name == "junction-up":  # a link to the parent is an ancestor
                    self.assertRefused(self.overlap(home, link, "JARVIS/jarvis/memory.py"),
                                       "jarvis_ancestor", calls=0)
                else:
                    self.assertRefused(self.overlap(home, link, "memory.py"), "self_target")
        if not made:
            self.skipTest("no junction or symlink support")

    def test_relative_root_is_refused_before_any_model_call(self):  # V9
        home = self.checkout()
        for root in ("proj", "\\proj", "C:proj", "\\\\localhost\\c$\\proj",
                     "\\\\?\\C:\\proj", "//server/share/proj"):
            with self.subTest(root=root):
                self.assertRefused(self.overlap(home, root, "a.py"), "root_unresolvable",
                                   calls=0)
                self.part1["target_location"] = root
                with mock.patch.object(work_planner.Path, "exists") as exists:
                    _, facts = work_planner.brief_facts(self.brief(), large_forced=False)
                exists.assert_not_called()  # a UNC root is never stat'ed
                self.assertTrue(facts["jarvis_repo"] and facts["existing_repo"])

    def test_action_classes_force_large_or_are_refused(self):  # F2
        root, facts = work_planner.brief_facts(self.brief(), large_forced=False)
        self.assertFalse(facts["jarvis_repo"] or facts["outgoing_actions"])
        declared = FakePlanner(self.plan(connector_classes=["local"],
                                         operation_classes=["stage_write", "check"]))
        result = work_planner.plan_project(self.brief(), root, facts, declared)
        self.assertEqual((result.size, result.reasons), ("large", ("declared action classes",)))
        for name in ("connector_classes", "operation_classes"):
            planner = FakePlanner(*[self.plan(**{name: ["network"]})] * 3)
            result = work_planner.plan_project(self.brief(), root, facts, planner)
            self.assertIn(work_planner.Refusal("unknown_class", "network"), result.refusals)
            self.assertEqual(len(planner.calls), 3)

    def test_model_errors_are_reasked_until_the_third_attempt(self):  # F3
        root, facts = work_planner.brief_facts(self.brief(), large_forced=False)
        planner = FakePlanner(RuntimeError("down"), TimeoutError("slow"), self.plan())
        result = work_planner.plan_project(self.brief(), root, facts, planner)
        self.assertIsInstance(result, work_planner.Planned)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(planner.calls[1]["refusals"], [{"code": "model_error", "detail": "RuntimeError"}])
        self.assertEqual(planner.calls[2]["refusals"], [{"code": "model_error", "detail": "TimeoutError"}])

    def test_encoded_glob_in_slice_targets_is_refused(self):  # F3
        root, facts = work_planner.brief_facts(self.brief(), large_forced=False)
        for target in ("a%2A.py", "a%3f.py", "a＊.py", "a［1］.py"):
            with self.subTest(target=target):
                reply = self.plan(slices=[{**example()["slices"][0], "targets": [target]}])
                planner = FakePlanner(reply, reply, reply)
                result = work_planner.plan_project(self.brief(), root, facts, planner)
                self.assertIn(work_planner.Refusal("glob", target), result.refusals)
                self.assertEqual(len(planner.calls), 3)

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
