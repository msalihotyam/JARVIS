"""V2.2 SKCOREFIX: the user's stop controls stop a delegated skill run.

Through the real Core with fakes only (``DummyConnector``, an isolated temp
database, never data/jarvis.db, no model or network call):

- cancelling the task a skill runs for cancels the runner chain: the live
  step stops through its own cancel, no further step starts, and the task
  ends CANCELLED with skill_run_finished (outcome cancelled) - no use event;
- turning skills off while a run is delegated starts no further step; the
  step in flight still needs its own approval and settles normally; the run
  ends with outcome stopped - no use event;
- races fail closed: a cancel or a flag-off during a step's execution, and a
  cancelled task whose chain was never stopped, start no further step.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from jarvis.contracts import ChainStatus, PermissionGrant, RunStatus, TaskStatus
from jarvis.core import AgentCore
from jarvis.skill_types import SkillStep
from tests.helpers import DUMMY_CAPABILITY, DummyConnector
from tests import test_skill_core as base
from tests.test_skill_core import USED, draft, user_task

STEP = "Haftalik rapor dosyasini oku: {path}"


class HookConnector(DummyConnector):
    """The fake provider: runs ``hook(session)`` once, in its first execution."""

    hook = None

    def execute(self, task, session):
        hook, self.hook = self.hook, None
        if hook is not None:
            hook(session)
        return super().execute(task, session)


class SkillCancelTests(unittest.TestCase):
    # SKCORE's fixtures, borrowed (not subclassed, so its tests run once).
    setUp = base.SkillCoreTests.setUp
    core = base.SkillCoreTests.core
    events_of = base.SkillCoreTests.events_of
    feed = base.SkillCoreTests.feed

    def save_two_step_skill(self) -> str:
        made = draft()
        steps = tuple(
            SkillStep(index=i, action=STEP, capability=DUMMY_CAPABILITY, inputs=("path",), check=None)
            for i in (1, 2)
        )
        made = replace(made, steps=steps)
        self.store.add_draft(made)
        return self.store.approve_draft(made.draft_id, project_id=None).skill_id

    def start(self, core: AgentCore):
        """Run the user's task; return (run, runner id, anchor id, owner id)."""
        run = core.run(user_task())
        runner = run.session.id
        self.assertTrue(run.session.objective.startswith("skill-run:"), run.error)
        anchor = run.session.objective.removeprefix("skill-run:")
        return run, runner, anchor, self.store.task_session_id(anchor)

    def finished(self, owner: str) -> dict:
        events = self.store.list_events(owner)
        payloads = [e["payload"] for e in events if e["event_type"] == "skill_run_finished"]
        self.assertEqual(len(payloads), 1)
        return payloads[0]

    def assertEndedWithoutStep2(self, runner: str, anchor: str, owner: str, outcome: str):
        chain = self.store.load_session(runner).chain
        self.assertIs(chain.status, ChainStatus.CANCELLED)
        self.assertFalse(chain.step_task_ids[1:] and chain.step_task_ids[1])
        self.assertIs(self.store.load_task(anchor).status, TaskStatus.CANCELLED)
        self.assertEqual(self.finished(owner)["outcome"], outcome)

    # -- 1. cancelling the anchor -----------------------------------------

    def test_cancelling_the_task_stops_the_step_awaiting_approval_and_the_chain(self) -> None:
        skill_id = self.save_two_step_skill()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run, runner, anchor, owner = self.start(core)
        self.assertEqual(run.status, RunStatus.AWAITING_APPROVAL, run.error)
        request = run.permission_request
        cancelled = core.cancel(owner, anchor)
        self.assertEqual(cancelled.status, RunStatus.CANCELLED, cancelled.error)
        self.assertIs(self.store.load_task(request.task_id).status, TaskStatus.CANCELLED)
        self.assertEndedWithoutStep2(runner, anchor, owner, "cancelled")
        # The old approval cannot revive the step, and nothing advances.
        core.resume(runner, request.task_id, PermissionGrant.for_request(request))
        core.run_chain(runner)
        self.assertEqual(connector.execute_calls, 0)
        self.assertEndedWithoutStep2(runner, anchor, owner, "cancelled")
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))  # D16

    def test_cancelling_the_task_between_steps_starts_no_further_step(self) -> None:
        skill_id = self.save_two_step_skill()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run, runner, anchor, owner = self.start(core)
        request = run.permission_request
        done = core.resume(runner, request.task_id, PermissionGrant.for_request(request))
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        self.assertEqual(core.cancel(owner, anchor).status, RunStatus.CANCELLED)
        core.run_chain(runner)
        core.resume(owner, anchor)
        self.assertEqual(connector.execute_calls, 1)
        self.assertEndedWithoutStep2(runner, anchor, owner, "cancelled")
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))

    # -- 2. turning skills off ---------------------------------------------

    def test_turning_skills_off_lets_the_live_step_settle_and_stops_the_run(self) -> None:
        skill_id = self.save_two_step_skill()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run, runner, anchor, owner = self.start(core)
        request = run.permission_request
        core.settings = replace(core.settings, skills_enabled=False)
        # The step in flight keeps its own approval: no grant, no execution.
        again, _ = core.run_chain(runner)
        self.assertEqual(again.status, RunStatus.AWAITING_APPROVAL, again.error)
        self.assertEqual(connector.execute_calls, 0)
        done = core.resume(runner, request.task_id, PermissionGrant.for_request(request))
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        core.resume(owner, anchor)
        self.assertEqual(connector.execute_calls, 1)
        self.assertEndedWithoutStep2(runner, anchor, owner, "stopped")
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))
        # New runs are refused as today: the task runs without a skill.
        plain = core.run(user_task())
        self.assertNotIn("skill_announced", self.feed(plain.session.id))
        self.assertNotIn("skill_run", plain.task.preferences)

    # -- 3. races fail closed ----------------------------------------------

    def test_a_flag_off_during_a_step_starts_no_further_step(self) -> None:
        skill_id = self.save_two_step_skill()
        connector = HookConnector()
        core = self.core(connector)
        connector.hook = lambda _: setattr(
            core, "settings", replace(core.settings, skills_enabled=False)
        )
        _, runner, anchor, owner = self.start(core)
        self.assertEqual(connector.execute_calls, 1)
        self.assertEndedWithoutStep2(runner, anchor, owner, "stopped")
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))

    def test_a_cancel_during_a_step_starts_no_further_step(self) -> None:
        skill_id = self.save_two_step_skill()
        connector = HookConnector()
        core = self.core(connector)
        seen = []

        def cancel_the_anchor(session) -> None:
            # The step executes in the skill-run session, named for its anchor.
            anchor = session.objective.removeprefix("skill-run:")
            seen.append(core.cancel(self.store.task_session_id(anchor), anchor))

        connector.hook = cancel_the_anchor
        _, runner, anchor, owner = self.start(core)
        # The step was executing: the task is CANCELLING, not yet CANCELLED (S2A),
        # and the step's own owner concluded it before the task was closed.
        self.assertEqual(seen[0].status, RunStatus.CANCELLING, seen[0].error)
        step = self.store.load_session(runner).chain.step_task_ids[0]
        self.assertIn(
            self.store.load_task(step).status,
            (TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED),
        )
        self.assertEqual(connector.execute_calls, 1)
        self.assertEndedWithoutStep2(runner, anchor, owner, "cancelled")
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))

    def test_a_cancelled_task_whose_chain_was_not_stopped_starts_no_step(self) -> None:
        self.save_two_step_skill()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run, runner, anchor, owner = self.start(core)
        request = run.permission_request
        core.resume(runner, request.task_id, PermissionGrant.for_request(request))
        # Storage alone cancels the task (e.g. a crash before the chain stop).
        self.assertEqual(self.store.cancel_task(owner, anchor).verdict, "cancelled")
        core.run_chain(runner)
        self.assertEqual(connector.execute_calls, 1)
        chain = self.store.load_session(runner).chain
        self.assertIs(chain.status, ChainStatus.CANCELLED)
        self.assertFalse(chain.step_task_ids[1:] and chain.step_task_ids[1])


if __name__ == "__main__":
    unittest.main()
