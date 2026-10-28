"""PM29: the project run card - a read-only mirror of the newest project run
whose actions are Core calls that Core re-checks."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from jarvis.core import AgentCore
from jarvis.registry import ToolRegistry
from jarvis.ui.bridge import _PROJECT_CARD_LABELS, _RUN_STATE_LABELS
from jarvis.work_ledger import EventKind, RunSize, RunState, WorkEvent, WorkRun
from jarvis.work_plan import Plan, PlanBudget, PlanCheck, PlanSlice
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import _APP, UiTestCase, find, pump

T0 = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


class FakeStore:
    """Just the three ledger reads the card makes."""

    def __init__(self, run: WorkRun, events=()):
        self.run, self.events = run, list(events)

    def work_runs_open(self):
        return [self.run]

    def latest_work_run(self, run_id):
        return self.run

    def work_events(self, run_id):
        return self.events


class FakeSink:
    def __init__(self, items):
        self.items = items

    def recent(self):
        return self.items


class ProjectCardTests(UiTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = tmp.name
        self.ui = self.bridge()
        self.memory = self.ui._conversation_db()
        self.core = AgentCore(ToolRegistry(), self.memory)
        self.ui.project_core = self.core
        self.ui.project_clock = lambda: T0 + timedelta(minutes=1)
        self.plan = Plan(
            goal="fix a", project_root=self.target,
            slices=(PlanSlice("s1", "fix a", ("src/a.py",), ("c1",)),),
            targets=("src/a.py",), connector_classes=(), operation_classes=(),
            checks=(PlanCheck("c1", ("python", "-m", "unittest", "tests.test_x"), 60, 4096, ()),),
            budgets=PlanBudget(10, 5, 3, 7200, 50), child_count=1,
            expires_at=4102444800, new_dependencies=(),
        )

    def run_at(self, run_id: str, size: RunSize) -> None:
        self.core.create_project_run(run_id, self.plan, project_root=self.target, jarvis_repo=False,
                                     size=size, boot_id="b", at=T0)
        nxt = RunState.AWAITING_APPROVAL if size is RunSize.LARGE else RunState.BUILDING
        self.core.advance_project_run(run_id, nxt, boot_id="b", at=T0)
        self.ui.refreshProjectCard()

    def test_every_run_state_maps_to_a_label_and_flags(self):
        self.assertEqual(set(RunState), set(_RUN_STATE_LABELS))
        payload = json.dumps({"project_root": self.target, "plan_hash": "h", "checks": "3/3 geçti",
                              "diff_summary": "2 dosya", "brief": {"definition_of_done": "python app.py"}})
        for size in (RunSize.LARGE, RunSize.SMALL):
            for state in RunState:
                with self.subTest(state=state, size=size):
                    run = WorkRun("r", state, T0, T0, size=size)
                    event = WorkEvent("r", 1, EventKind.RUN_STATE, T0, step_key="s1", payload_json=payload)
                    with mock.patch.object(self.ui, "_conversation_db", return_value=FakeStore(run, [event])):
                        card = self.ui._project_card_snapshot()
                    self.assertEqual(state.value, card["state"])
                    self.assertEqual(_RUN_STATE_LABELS[state], card["stateLabel"])
                    gate = size is RunSize.LARGE and state is RunState.AWAITING_APPROVAL
                    self.assertEqual(gate, card["canApprove"])
                    self.assertEqual(gate, card["canReject"])
                    self.assertEqual(state not in (RunState.DONE, RunState.FAILED, RunState.CANCELLED),
                                     card["canCancel"])
                    self.assertEqual(state is RunState.DONE, card["canShowResult"])
                    self.assertEqual(("s1", "3/3 geçti", "2 dosya", "python app.py"),
                                     (card["currentSlice"], card["checks"], card["diffSummary"], card["runHint"]))

    def test_large_run_approve_calls_core_with_the_run_id(self):
        self.run_at("big", RunSize.LARGE)
        self.assertEqual("big", self.ui.projectCard["runId"])
        self.assertTrue(self.ui.projectCard["canApprove"])
        with mock.patch.object(self.core, "approve_project_plan", wraps=self.core.approve_project_plan) as spy:
            self.assertTrue(self.ui.approveProjectPlan())
        self.assertEqual("big", spy.call_args.args[0])
        self.assertEqual(self.plan.plan_hash, spy.call_args.args[1])
        self.assertEqual(RunState.BUILDING, RunState(self.memory.latest_work_run("big").state))
        self.assertEqual("", self.ui.projectCard["notice"])

    def test_large_run_reject_calls_core_and_cancels(self):
        self.run_at("big", RunSize.LARGE)
        with mock.patch.object(self.core, "reject_project_plan", wraps=self.core.reject_project_plan) as spy:
            self.assertTrue(self.ui.rejectProjectPlan())
        self.assertEqual("big", spy.call_args.args[0])
        self.assertEqual(RunState.CANCELLED, RunState(self.memory.latest_work_run("big").state))
        self.assertEqual("cancelled", self.ui.projectCard["state"])
        self.assertFalse(self.ui.projectCard["canCancel"])

    def test_core_refuses_approve_and_reject_for_small_and_medium(self):
        for size in (RunSize.SMALL, RunSize.MEDIUM):
            with self.subTest(size=size):
                run_id = f"run-{size.value}"
                self.run_at(run_id, size)
                self.assertFalse(self.ui.projectCard["canApprove"])
                self.assertFalse(self.ui.projectCard["canReject"])
                self.assertFalse(self.ui.approveProjectPlan())
                self.assertEqual(_PROJECT_CARD_LABELS["refused"], self.ui.projectCard["notice"])
                self.assertFalse(self.ui.rejectProjectPlan())
                self.assertEqual(RunState.BUILDING, RunState(self.memory.latest_work_run(run_id).state))
                self.assertTrue(self.ui.cancelProjectRun())

    def test_pause_and_cancel_go_through_core(self):
        self.run_at("small", RunSize.SMALL)
        self.assertTrue(self.ui.pauseProjectRun())
        self.assertEqual("paused", self.ui.projectCard["state"])
        self.assertFalse(self.ui.projectCard["canPause"])
        self.assertTrue(self.ui.cancelProjectRun())
        self.assertFalse(self.ui.cancelProjectRun())      # Core refuses a terminal run
        self.assertEqual(_PROJECT_CARD_LABELS["refused"], self.ui.projectCard["notice"])
        self.assertFalse(self.ui.showProjectResult())

    def test_notifications_are_plain_text(self):
        sink = FakeSink([{"title": "<b>Proje</b>", "text": f"adım {i}"} for i in range(7)])
        with mock.patch.object(_APP, "_jarvis_notify", sink, create=True):
            self.run_at("small", RunSize.SMALL)
        notes = self.ui.projectCard["notifications"]
        self.assertEqual(5, len(notes))
        self.assertTrue(all(isinstance(note, str) for note in notes))
        self.assertEqual("<b>Proje</b> — adım 6", notes[-1])

    def test_feed_card_buttons_follow_core_flags(self):
        self.run_at("small", RunSize.SMALL)
        self.load(self.ui)
        root = self.engine.rootObjects()[0]
        pump(50)
        card = find(root, "projectRunCard")
        self.assertIsNotNone(card)
        self.assertFalse(find(card, "projectApprove").property("enabled"))
        self.assertFalse(find(card, "projectReject").property("enabled"))
        self.assertTrue(find(card, "projectCancel").property("enabled"))
        self.assertTrue(self.ui.cancelProjectRun())
        pump(50)
        self.assertFalse(find(card, "projectCancel").property("enabled"))


if __name__ == "__main__":
    unittest.main()
