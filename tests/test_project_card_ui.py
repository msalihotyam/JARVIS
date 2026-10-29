"""PM29: the project run card - a read-only mirror of the newest project run
whose actions are Core calls that Core re-checks."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QUrl

from jarvis.core import AgentCore
from jarvis.registry import ToolRegistry
from jarvis.ui.bridge import _PROJECT_CARD_LABELS, _RUN_STATE_LABELS
from jarvis.work_ledger import (
    RUN_TRANSITIONS, WAIT_RUN_STATES, EventKind, RunSize, RunState, WorkEvent, WorkRun, advance_run, event_payload_json,
)
from jarvis.work_plan import Plan, PlanBudget, PlanCheck, PlanSlice
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import _APP, UiTestCase, find, pump

T0 = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


class FakeStore:
    """Just the ledger reads the card makes; like the real store,
    work_runs_recent() keeps terminal runs."""

    def __init__(self, run: WorkRun, events=()):
        self.run, self.events = run, list(events)

    def work_runs_recent(self, limit):
        return [self.run][:limit]

    def work_events(self, run_id):
        return self.events if run_id == self.run.run_id else []


class BrokenStore:
    def work_runs_recent(self, limit):
        raise sqlite3.OperationalError("database is locked")


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
        if size is RunSize.LARGE:
            self.core.advance_project_run(run_id, RunState.AWAITING_APPROVAL, boot_id="b", at=T0)
        else:
            # PM12: small/medium build only on the planner's read-back approval
            # row; Core has no API for it, so write it as the planner does.
            run = self.memory.latest_work_run(run_id)
            record = {"run_id": run_id, "plan_hash": self.plan.plan_hash, "approver": "readback",
                      "brief_hash": "brief", "approved_at": int(T0.timestamp()),
                      "expires_at": int(T0.timestamp()) + 3600}
            self.memory.append_work_run(advance_run(run, RunState.BUILDING, at=T0),
                                        event_kind=EventKind.RUN_STATE, event_payload={
                "from": run.state.value, "to": RunState.BUILDING.value, "boot_id": "b",
                "plan": self.plan.to_dict(), "plan_hash": self.plan.plan_hash, "project_root": self.target,
                "jarvis_repo": False, "brief_hash": "brief", "approval": record,
            }, prior=run)
        self.ui.refreshProjectCard()

    def event(self, seq: int, run_id: str = "r", **payload) -> WorkEvent:
        return WorkEvent(run_id, seq, EventKind.RUN_STATE, T0, step_key=f"s{seq}",
                         payload_json=event_payload_json(payload))

    def card_from(self, store, ui=None) -> dict:
        ui = ui or self.ui
        with mock.patch.object(ui, "_conversation_db", return_value=store):
            ui.refreshProjectCard()
        return ui.projectCard

    def test_every_run_state_maps_to_a_label_and_flags(self):
        self.assertEqual(set(RunState), set(_RUN_STATE_LABELS))
        payload = event_payload_json({"project_root": self.target, "plan_hash": "h", "checks": "3/3 geçti",
                              "diff_summary": "2 dosya", "brief": {"definition_of_done": "python app.py"}})
        for size in (RunSize.LARGE, RunSize.SMALL):
            for state in RunState:
                with self.subTest(state=state, size=size):
                    resume = RunState.BUILDING if state in WAIT_RUN_STATES else None
                    run = WorkRun("r", state, T0, T0, size=size, resume_state=resume)
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
                    self.assertEqual(RunState.PAUSED in RUN_TRANSITIONS[state], card["canPause"])
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
        card = find(root.contentItem(), "projectRunCard")
        self.assertIsNotNone(card)
        self.assertFalse(find(card, "projectApprove").property("enabled"))
        self.assertFalse(find(card, "projectReject").property("enabled"))
        self.assertTrue(find(card, "projectCancel").property("enabled"))
        self.assertTrue(self.ui.cancelProjectRun())
        pump(50)
        self.assertFalse(find(card, "projectCancel").property("enabled"))

    def test_terminal_run_stays_on_the_card_live_and_after_restart(self):
        store = FakeStore(WorkRun("r", RunState.BUILDING, T0, T0, size=RunSize.SMALL),
                          [self.event(1, project_root=self.target)])
        self.assertEqual("building", self.card_from(store)["state"])
        for state in (RunState.DONE, RunState.FAILED, RunState.CANCELLED):
            with self.subTest(state=state):
                store.run = WorkRun("r", state, T0, T0, size=RunSize.SMALL)
                self.assertEqual(state.value, self.card_from(store)["state"])
                # restart: a new bridge on the same preferences reads the id back
                self.assertEqual(state.value, self.card_from(store, self.bridge())["state"])

    def test_real_ledger_cancel_outside_the_card_keeps_the_run(self):
        self.run_at("small", RunSize.SMALL)
        self.core.advance_project_run("small", RunState.CANCELLED, boot_id="b", at=T0)
        self.assertEqual([], self.memory.work_runs_open())
        self.ui.refreshProjectCard()
        self.assertEqual(("small", "cancelled"), (self.ui.projectCard["runId"], self.ui.projectCard["state"]))
        restarted = self.bridge()
        restarted.refreshProjectCard()
        self.assertEqual(("small", "cancelled"), (restarted.projectCard["runId"], restarted.projectCard["state"]))

    def test_stale_pm29fix_sidecar_is_ignored(self):
        store = FakeStore(WorkRun("new", RunState.DONE, T0, T0), [self.event(1, run_id="new")])
        sidecar = self.ui.prefs.path.with_suffix(".project-card.json")
        for raw in (json.dumps({"version": 1, "run_id": "old"}), "{not json"):
            with self.subTest(raw=raw):
                sidecar.write_text(raw, encoding="utf-8")
                self.assertEqual("new", self.card_from(store, self.bridge())["runId"])

    def test_run_between_polls_or_while_closed_is_shown(self):
        # nothing seen live: the run started and ended before the first poll
        self.assertEqual({}, self.card_from(self.memory))
        for run_id in ("quick", "closed"):      # a second run replaces the first
            with self.subTest(run_id=run_id):
                self.core.create_project_run(run_id, self.plan, project_root=self.target, jarvis_repo=False,
                                             size=RunSize.LARGE, boot_id="b", at=T0)
                self.core.advance_project_run(run_id, RunState.CANCELLED, boot_id="b", at=T0)
                self.assertEqual([], self.memory.work_runs_open())
                self.ui.refreshProjectCard()
                self.assertEqual((run_id, "cancelled"), (self.ui.projectCard["runId"], self.ui.projectCard["state"]))
                restarted = self.bridge()       # app was closed: a fresh bridge, no sidecar
                restarted.refreshProjectCard()
                self.assertEqual(run_id, restarted.projectCard["runId"])

    def test_newest_write_wins_over_an_older_open_run(self):
        self.run_at("open", RunSize.SMALL)
        self.run_at("ended", RunSize.SMALL)
        self.core.advance_project_run("ended", RunState.CANCELLED, boot_id="b", at=T0)
        self.ui.refreshProjectCard()
        # an older run left open (e.g. INTERRUPTED after a restart) must not hide the newer one
        self.assertEqual(("ended", "cancelled"), (self.ui.projectCard["runId"], self.ui.projectCard["state"]))
        self.core.advance_project_run("open", RunState.CANCELLED, boot_id="b", at=T0)
        self.ui.refreshProjectCard()
        self.assertEqual("open", self.ui.projectCard["runId"])     # its cancel is the newest change

    def test_open_runs_with_equal_time_follow_write_order(self):
        self.run_at("z", RunSize.SMALL)
        self.run_at("a", RunSize.SMALL)                            # same `at`, written last
        self.ui.refreshProjectCard()
        self.assertEqual(("a", "building"), (self.ui.projectCard["runId"], self.ui.projectCard["state"]))

    def test_work_runs_recent_orders_by_last_change(self):
        self.assertEqual([], self.memory.work_runs_recent(5))
        for run_id in ("a", "b", "c"):
            self.core.create_project_run(run_id, self.plan, project_root=self.target, jarvis_repo=False,
                                         size=RunSize.LARGE, boot_id="b", at=T0)
        self.core.advance_project_run("a", RunState.CANCELLED, boot_id="b", at=T0)
        self.assertEqual(["a", "c", "b"], [r.run_id for r in self.memory.work_runs_recent(5)])
        self.assertEqual(["a"], [r.run_id for r in self.memory.work_runs_recent(1)])
        self.assertEqual(RunState.CANCELLED, RunState(self.memory.work_runs_recent(1)[0].state))
        for bad in (0, -1, True, 1.5, "1", None):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                self.memory.work_runs_recent(bad)

    def test_pause_follows_the_ledger_transitions(self):
        for state, allowed in ((RunState.NEEDS_INPUT, True), (RunState.BLOCKED_BUDGET, True),
                               (RunState.BUILDING, True), (RunState.PAUSED, False), (RunState.DONE, False)):
            with self.subTest(state=state):
                resume = RunState.BUILDING if state in WAIT_RUN_STATES else None
                run = WorkRun("r", state, T0, T0, size=RunSize.SMALL, resume_state=resume)
                self.assertEqual(allowed, self.card_from(FakeStore(run))["canPause"])

    def test_any_core_exception_is_a_refusal(self):
        self.run_at("small", RunSize.SMALL)
        for error in (sqlite3.OperationalError("locked"), OSError("disk"), RuntimeError("boom"), KeyError("x")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(self.core, "advance_project_run", side_effect=error):
                    self.assertFalse(self.ui.pauseProjectRun())
                    self.assertFalse(self.ui.cancelProjectRun())
                self.assertEqual(_PROJECT_CARD_LABELS["refused"], self.ui.projectCard["notice"])
                self.assertEqual(RunState.BUILDING, RunState(self.memory.latest_work_run("small").state))
                self.assertEqual("building", self.ui.projectCard["state"])

    def test_refusal_leaves_the_ledger_unchanged(self):
        self.run_at("small", RunSize.SMALL)
        before = self.memory.work_events("small")
        self.assertFalse(self.ui.approveProjectPlan())
        self.assertFalse(self.ui.rejectProjectPlan())
        self.assertEqual(before, self.memory.work_events("small"))
        self.assertEqual("building", self.ui.projectCard["state"])

    def test_size_refusal_outside_building(self):
        self.core.create_project_run("med", self.plan, project_root=self.target, jarvis_repo=False,
                                     size=RunSize.MEDIUM, boot_id="b", at=T0)
        self.ui.refreshProjectCard()
        state = self.ui.projectCard["state"]
        self.assertNotEqual("building", state)
        self.assertFalse(self.ui.projectCard["canApprove"])
        self.assertFalse(self.ui.approveProjectPlan())
        self.assertEqual(state, RunState(self.memory.latest_work_run("med").state).value)

    def test_show_result_opens_the_bound_root_only_if_it_is_a_folder(self):
        other = Path(self.target) / "other"
        other.mkdir()
        store = FakeStore(WorkRun("r", RunState.DONE, T0, T0),
                          [self.event(1, project_root=self.target), self.event(2, project_root=str(other))])
        self.assertTrue(self.card_from(store)["canShowResult"])
        with mock.patch.object(self.ui, "_conversation_db", return_value=store), \
                mock.patch("jarvis.ui.bridge.QDesktopServices") as desktop:
            desktop.openUrl.return_value = True
            self.assertTrue(self.ui.showProjectResult())
        desktop.openUrl.assert_called_once_with(QUrl.fromLocalFile(self.target))
        for root in (str(Path(self.target) / "gone"), "relative/dir", 7, ""):
            with self.subTest(root=root):
                self.ui._project_events = {}
                store = FakeStore(WorkRun("r", RunState.DONE, T0, T0), [self.event(1, project_root=root)])
                # polling never touches the filesystem; the click checks is_dir()
                with mock.patch.object(Path, "is_dir", side_effect=AssertionError("fs call on poll")):
                    self.assertEqual(isinstance(root, str) and Path(root).is_absolute(),
                                     self.card_from(store)["canShowResult"])
                with mock.patch.object(self.ui, "_conversation_db", return_value=store), \
                        mock.patch("jarvis.ui.bridge.QDesktopServices") as desktop:
                    self.assertFalse(self.ui.showProjectResult())
                desktop.openUrl.assert_not_called()

    def test_replan_rebinds_the_root_and_open_failure_is_false(self):
        other = Path(self.target) / "replanned"
        other.mkdir()
        store = FakeStore(WorkRun("r", RunState.DONE, T0, T0),
                          [self.event(1, project_root=str(Path(self.target) / "gone")),
                           self.event(2, project_root=str(other), plan={})])
        self.assertEqual(str(other), self.card_from(store)["projectRoot"])
        with mock.patch.object(self.ui, "_conversation_db", return_value=store), \
                mock.patch("jarvis.ui.bridge.QDesktopServices") as desktop:
            desktop.openUrl.side_effect = RuntimeError("no handler")
            self.assertFalse(self.ui.showProjectResult())
        desktop.openUrl.assert_called_once_with(QUrl.fromLocalFile(str(other)))

    def test_ended_run_is_not_refetched_and_notice_follows_the_run(self):
        store = FakeStore(WorkRun("r", RunState.DONE, T0, T0), [self.event(1, checks="3/3")])
        self.card_from(store)
        self.ui._project_card_notice = _PROJECT_CARD_LABELS["refused"]
        with mock.patch.object(store, "work_events", side_effect=AssertionError("refetched")):
            self.assertEqual(("3/3", _PROJECT_CARD_LABELS["refused"]),
                             (self.card_from(store)["checks"], self.ui.projectCard["notice"]))
        other = FakeStore(WorkRun("x", RunState.BUILDING, T0, T0), [self.event(1, run_id="x")])
        self.assertEqual(("x", ""), (self.card_from(other)["runId"], self.ui.projectCard["notice"]))

    def test_failed_first_read_of_an_ended_run_is_retried(self):
        store = FakeStore(WorkRun("r", RunState.DONE, T0, T0),
                          [self.event(1, checks="3/3", plan={}, project_root=self.target)])
        with mock.patch.object(store, "work_events", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual({}, self.card_from(store))
        card = self.card_from(store)
        self.assertEqual(("3/3", self.target, True), (card["checks"], card["projectRoot"], card["canShowResult"]))

    def test_polling_parses_only_new_events(self):
        store = FakeStore(WorkRun("r", RunState.BUILDING, T0, T0), [self.event(1, checks="1/3")])
        events = store.events
        self.assertEqual("1/3", self.card_from(store)["checks"])
        events[0] = self.event(1, checks="rewritten")       # same seq: not parsed again
        self.assertEqual("1/3", self.card_from(store)["checks"])
        events.append(self.event(2, checks="3/3"))
        self.assertEqual(("3/3", "s2"), (self.card_from(store)["checks"], self.ui.projectCard["currentSlice"]))
        other = FakeStore(WorkRun("x", RunState.BUILDING, T0, T0), [self.event(1, run_id="x")])
        self.assertEqual("", self.card_from(other)["checks"])   # another run starts clean

    def test_refresh_survives_store_errors(self):
        self.run_at("small", RunSize.SMALL)
        self.assertEqual({}, self.card_from(BrokenStore()))
        self.ui.refreshProjectCard()
        self.assertEqual("small", self.ui.projectCard["runId"])

    def test_card_notice_is_not_the_projects_notice(self):
        self.run_at("small", RunSize.SMALL)
        projects = self.ui.projectNotice
        self.assertFalse(self.ui.approveProjectPlan())
        self.assertEqual(projects, self.ui.projectNotice)
        self.assertEqual(_PROJECT_CARD_LABELS["refused"], self.ui.projectCard["notice"])


if __name__ == "__main__":
    unittest.main()
