"""PM27EMIT: done / interrupted / awaiting_acceptance go through NotificationCore
into a real SQLite ledger under TEMP (never data/jarvis.db); the clock is frozen."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from jarvis import n6_boot, work_driver
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.notify import NotificationCore
from jarvis.registry import ToolRegistry
from jarvis.work_ledger import (
    EventKind, RunSize, RunState, StepState, WorkEvent, WorkStep, event_payload_json, step_key,
)
from jarvis.work_plan import Plan, PlanBudget, PlanCheck, PlanSlice
from tests.test_core_project_drive import NOW, ROOT


class StorePorts:
    """The driver ports the emit path touches, writing to the real store."""

    def __init__(self, memory, run_id):
        self.memory, self.run_id = memory, run_id

    def clock(self):
        return NOW + timedelta(hours=1)

    def _seq(self):
        return self.memory.work_events(self.run_id)[-1].seq + 1

    def append_event(self, kind, key, payload, idempotency_key=None):
        event = WorkEvent(self.run_id, self._seq(), kind, self.clock(), key, event_payload_json(payload))
        self.memory.append_work_event(event, idempotency_key=idempotency_key)

    def notify(self, point, dedupe_key, key):
        notifier = NotificationCore(self.memory.append_work_event, self.memory.work_events)
        return notifier.notify(self.run_id, point, dedupe_key, seq=self._seq(), at=self.clock(), step_key=key)

    def boot_scan(self):  # an empty, unheld report, as tests.test_work_driver.FakePorts
        return n6_boot.Report("w", "", None, (), Path("n6.lease"), 0.0)

    def boot_record(self, item):
        return None

    def boot_resume(self, item):
        return None


class NotifyEmitTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "emit.sqlite"
        self.memory = SQLiteMemoryStore(self.path)
        self.core = AgentCore(ToolRegistry(), self.memory)
        self.plan = Plan(
            goal="repair source", project_root=ROOT,
            slices=(PlanSlice("s1", "repair a", ("src/a.py",), ("c1",)),),
            targets=("src/a.py",), connector_classes=(), operation_classes=(),
            checks=(PlanCheck("c1", ("python", "-m", "unittest", "tests.test_a"), 60, 4096, ()),),
            budgets=PlanBudget(10, 5, 3, 7200, 50), child_count=1, expires_at=4102444800, new_dependencies=(),
        )

    def _create(self, run_id, boot):
        self.core.create_project_run(
            run_id, self.plan, project_root=ROOT, jarvis_repo=False, size=RunSize.LARGE, boot_id=boot, at=NOW,
        )
        self.core.advance_project_run(run_id, RunState.AWAITING_APPROVAL, boot_id=boot, at=NOW)

    def _notes(self, run_id, point, memory=None):
        events = (memory or self.memory).work_events(run_id)
        return [json.loads(e.payload_json)["dedupe_key"] for e in events
                if e.kind is EventKind(point) and json.loads(e.payload_json).get("point") == point]

    def _outbox(self, memory=None):
        return len((memory or self.memory).notify_outbox_rows())

    def test_a2_n1_n3_interrupted_once_per_run_per_boot(self):
        self._create("r", "old")
        self._create("q", "old")
        before = self._outbox()
        self.assertEqual(sorted(self.core.recover_project_runs("b1", now=NOW + timedelta(minutes=1))), ["q", "r"])
        self.assertEqual(self._notes("r", "interrupted"), ["boot:b1"])  # A2
        self.assertEqual(self._notes("q", "interrupted"), ["boot:b1"])
        self.assertEqual(self._outbox(), before + 2)
        # N1: a restart on the same boot id replays nothing
        memory = SQLiteMemoryStore(self.path)
        AgentCore(ToolRegistry(), memory).recover_project_runs("b1", now=NOW + timedelta(minutes=2))
        self.assertEqual(self._notes("r", "interrupted", memory), ["boot:b1"])
        self.assertEqual(self._outbox(memory), before + 2)
        # N3: r resumes under b1 and is interrupted again by b2; q stays interrupted from b1
        self.core.advance_project_run("r", RunState.AWAITING_APPROVAL, boot_id="b1", at=NOW + timedelta(minutes=3))
        self.assertEqual(self.core.recover_project_runs("b2", now=NOW + timedelta(minutes=4)), ["r"])
        self.assertEqual(self._notes("r", "interrupted"), ["boot:b1", "boot:b2"])
        self.assertEqual(self._notes("q", "interrupted"), ["boot:b1"])
        self.assertEqual(self._outbox(), before + 3)

    def test_a1_done_once_and_n2_failed_never(self):
        self._create("r", "b")
        self._create("f", "b")
        before = self._outbox()
        run = replace(self.memory.latest_work_run("r"), state=RunState.INTEGRATING, resume_state=None)
        ports = StorePorts(self.memory, "r")
        self.assertIs(work_driver._run_to(run, RunState.DONE, ports).state, RunState.DONE)
        self.assertEqual(self._notes("r", "done"), ["done"])  # A1
        self.assertEqual(self._outbox(), before + 1)
        self.assertFalse(ports.notify("done", "done", None))  # a replay collapses
        failed = replace(self.memory.latest_work_run("f"), state=RunState.BUILDING, resume_state=None)
        self.assertIs(work_driver._run_to(failed, RunState.FAILED, StorePorts(self.memory, "f")).state, RunState.FAILED)
        self.assertEqual(self._notes("f", "done"), [])  # N2
        self.assertEqual(self._outbox(), before + 1)

    def test_a3_manual_check_acceptance_notifies_once(self):
        for run_id, manual in (("m", True), ("a", False)):
            self._create(run_id, "b")
            run = replace(self.memory.latest_work_run(run_id), state=RunState.BUILDING, resume_state=None)
            steps = (WorkStep(run_id, step_key(run_id, 1, "s1"), 1, "s1", StepState.ACCEPTED, NOW),)
            moved, _ = work_driver.drive(run, steps, StorePorts(self.memory, run_id), manual_checks=manual)
            self.assertIs(moved.state, RunState.ACCEPTING)
        self.assertEqual(self._notes("m", "awaiting_acceptance"), ["acceptance"])  # A3
        self.assertEqual(self._notes("a", "awaiting_acceptance"), [])


if __name__ == "__main__":
    unittest.main()
