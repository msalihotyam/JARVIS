"""DFPAUSE: Stop pauses a running task at its next step boundary; Resume continues it.

Through the real Core with fakes only (``DummyConnector``, an isolated temp
database, never data/jarvis.db, no model or network call). Nothing in pause or
resume reads the clock, so no clock is injected.
"""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta

from PySide6.QtCore import QMetaObject

from jarvis.contracts import (
    ORPHAN_MIN_AGE, AttemptRecovery, AttemptState, ChainStatus, ErrorCode, PermissionGrant, RunStatus,
    SideEffect, TaskStatus,
)
from jarvis.lifecycle import is_terminal
from jarvis.memory import SQLiteMemoryStore
from jarvis.skill_store import SkillStore
from jarvis.skill_types import SkillStep
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, wait_until
from tests import test_skill_core as base
from tests import test_skill_core_cancel as cancel
from tests import test_ui
from tests.test_intent import StubConnector, task_proposal
from tests.test_skill_core import user_task
from tests.test_skill_core_cancel import HookConnector
from tests.test_ui import UiTestCase, pump


class _SimulatedCrash(BaseException):
    """The process dies right here: nothing below the raise runs or settles."""


class PauseCoreTests(unittest.TestCase):
    # SKCORE's fixtures, borrowed (not subclassed, so its tests run once).
    setUp = base.SkillCoreTests.setUp
    core = base.SkillCoreTests.core
    feed = base.SkillCoreTests.feed
    save_two_step_skill = cancel.SkillCancelTests.save_two_step_skill

    def status(self, task_id: str) -> TaskStatus:
        return self.store.load_task(task_id).status

    def paused_mid_step(self):
        """A plain task paused while its one step executes: (core, connector, run, seen)."""
        connector = HookConnector()
        core = self.core(connector)
        seen = []
        connector.hook = lambda session: seen.append(core.pause(session.id, session.latest_task_id))
        run = core.run(user_task())
        return core, connector, run, seen

    def paused_at_approval(self):
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run = core.run(user_task())
        self.assertEqual(run.status, RunStatus.AWAITING_APPROVAL, run.error)
        paused = core.pause(run.session.id, run.task.id)
        self.assertEqual(paused.status, RunStatus.AWAITING_CLARIFICATION, paused.error)
        self.assertIs(self.status(run.task.id), TaskStatus.PAUSED)
        return core, connector, run

    # -- boundary and resume ------------------------------------------------

    def test_pause_during_a_step_lets_it_finish_and_stops_at_the_boundary(self) -> None:
        _, connector, run, seen = self.paused_mid_step()
        self.assertEqual(seen[0].status, RunStatus.AWAITING_CLARIFICATION, seen[0].error)
        self.assertIs(seen[0].task.status, TaskStatus.PAUSE_REQUESTED)    # "duruyor: bu adım bitince"
        self.assertEqual(run.status, RunStatus.AWAITING_CLARIFICATION, run.error)
        self.assertIs(self.status(run.task.id), TaskStatus.PAUSED)
        self.assertEqual(connector.execute_calls, 1)                      # the step was not cut
        self.assertIn("task_paused", self.feed(run.session.id))

    def test_resume_runs_only_the_remaining_steps(self) -> None:
        core, connector, run, _ = self.paused_mid_step()
        done = core.resume_paused(run.session.id, run.task.id)
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        self.assertEqual(connector.execute_calls, 1)                      # the finished step never re-ran
        self.assertIs(self.status(run.task.id), TaskStatus.COMPLETED)

    def test_resume_asks_the_approval_again_and_never_reuses_the_old_one(self) -> None:
        core, connector, run = self.paused_at_approval()
        old = run.permission_request
        # The old approval cannot move a paused task.
        held = core.resume(run.session.id, run.task.id, PermissionGrant.for_request(old))
        self.assertEqual(held.status, RunStatus.AWAITING_CLARIFICATION, held.error)
        self.assertIs(self.status(run.task.id), TaskStatus.PAUSED)
        self.assertEqual(connector.execute_calls, 0)
        again = core.resume_paused(run.session.id, run.task.id)
        self.assertEqual(again.status, RunStatus.AWAITING_APPROVAL, again.error)
        self.assertIsNotNone(again.permission_request)
        self.assertEqual(connector.execute_calls, 0)                      # asked, not assumed
        fresh = again.permission_request
        done = core.resume(run.session.id, run.task.id, PermissionGrant.for_request(fresh))
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        self.assertEqual(connector.execute_calls, 1)

    def test_pause_resume_pause_cycles(self) -> None:
        core, connector, run = self.paused_at_approval()
        for _ in range(3):
            again = core.resume_paused(run.session.id, run.task.id)
            self.assertEqual(again.status, RunStatus.AWAITING_APPROVAL, again.error)
            paused = core.pause(run.session.id, run.task.id)
            self.assertEqual(paused.status, RunStatus.AWAITING_CLARIFICATION, paused.error)
            self.assertIs(self.status(run.task.id), TaskStatus.PAUSED)
        self.assertEqual(connector.execute_calls, 0)
        # A second pause of a paused task changes nothing.
        twice = core.pause(run.session.id, run.task.id)
        self.assertEqual(twice.status, RunStatus.FAILED)
        self.assertIs(self.status(run.task.id), TaskStatus.PAUSED)

    # -- cancel stays terminal ------------------------------------------------

    def test_cancel_of_a_paused_task_is_terminal_and_resume_is_refused(self) -> None:
        core, connector, run = self.paused_at_approval()
        cancelled = core.cancel(run.session.id, run.task.id)
        self.assertEqual(cancelled.status, RunStatus.CANCELLED, cancelled.error)
        self.assertIs(self.status(run.task.id), TaskStatus.CANCELLED)
        refused = core.resume_paused(run.session.id, run.task.id)
        self.assertEqual(refused.status, RunStatus.CANCELLED)
        self.assertIs(self.status(run.task.id), TaskStatus.CANCELLED)
        self.assertEqual(core.pause(run.session.id, run.task.id).status, RunStatus.CANCELLED)
        self.assertIs(self.status(run.task.id), TaskStatus.CANCELLED)
        self.assertEqual(connector.execute_calls, 0)

    # -- ownership ---------------------------------------------------------------

    def test_another_sessions_task_is_refused(self) -> None:
        core, connector, run = self.paused_at_approval()
        other = core.run(user_task()).session.id
        for verb in (core.resume_paused, core.pause):
            refused = verb(other, run.task.id)
            self.assertEqual(refused.status, RunStatus.FAILED)
            self.assertIs(refused.error.code, ErrorCode.TASK_NOT_FOUND)
            self.assertIs(self.status(run.task.id), TaskStatus.PAUSED)
        forged = core.resume_paused(run.session.id, "task-forged")
        self.assertIs(forged.error.code, ErrorCode.TASK_NOT_FOUND)
        unknown = core.pause("session-forged", run.task.id)
        self.assertIs(unknown.error.code, ErrorCode.UNKNOWN_SESSION)
        self.assertIs(self.status(run.task.id), TaskStatus.PAUSED)
        self.assertEqual(connector.execute_calls, 0)

    # -- restart and scheduler ----------------------------------------------------

    def test_restart_keeps_the_task_paused_and_resumable(self) -> None:
        _, _, run, _ = self.paused_mid_step()
        self.store = SkillStore(self.database)
        connector = DummyConnector()
        core = self.core(connector)
        self.assertIs(self.status(run.task.id), TaskStatus.PAUSED)
        done = core.resume_paused(run.session.id, run.task.id)
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        self.assertEqual(connector.execute_calls, 0)                      # the step ran before restart

    def test_scheduler_paths_never_resume_it(self) -> None:
        # A scheduled run reaches the task only through resume / approve /
        # clarify, which all route through _advance and report it as paused.
        core, connector, run = self.paused_at_approval()
        for attempt in (
            lambda: core.resume(run.session.id, run.task.id),
            lambda: core.resume(run.session.id, run.task.id, PermissionGrant.for_request(run.permission_request)),
        ):
            held = attempt()
            self.assertEqual(held.status, RunStatus.AWAITING_CLARIFICATION, held.error)
            self.assertIs(self.status(run.task.id), TaskStatus.PAUSED)
        self.assertEqual(connector.execute_calls, 0)

    # -- typed message while paused ------------------------------------------

    def test_a_new_request_while_paused_never_touches_the_paused_task(self) -> None:
        core, connector, run = self.paused_at_approval()
        before = self.store.load_task(run.task.id)
        other = core.run(user_task())
        self.assertNotEqual(other.task.id, run.task.id)
        after = self.store.load_task(run.task.id)
        self.assertIs(after.status, TaskStatus.PAUSED)
        self.assertEqual(before.preferences, after.preferences)
        self.assertEqual(core.resume_paused(run.session.id, run.task.id).status, RunStatus.AWAITING_APPROVAL)

    # -- skill-delegated (SKCOREFIX chain) ---------------------------------

    def test_a_skill_delegated_task_pauses_at_its_step_boundary(self) -> None:
        self.save_two_step_skill()
        connector = HookConnector()
        core = self.core(connector)
        seen = []

        def pause_the_anchor(session) -> None:
            # The step executes in the skill-run session, named for its anchor.
            anchor = session.objective.removeprefix("skill-run:")
            seen.append((session.id, core.pause(self.store.task_session_id(anchor), anchor)))

        connector.hook = pause_the_anchor
        run = core.run(user_task())
        runner, paused = seen[0]
        anchor = self.store.load_session(runner).objective.removeprefix("skill-run:")
        owner = self.store.task_session_id(anchor)
        # The owner's view: the anchor is paused, not the step the chain last ran.
        self.assertEqual((run.session.id, run.task.id), (owner, anchor))
        self.assertEqual(paused.status, RunStatus.AWAITING_CLARIFICATION, paused.error)
        self.assertEqual(connector.execute_calls, 1)                      # step 1 finished, step 2 never started
        chain = self.store.load_session(runner).chain
        self.assertFalse(chain.step_task_ids[1:] and chain.step_task_ids[1])
        self.assertIsNot(chain.status, ChainStatus.CANCELLED)
        self.assertIs(self.status(anchor), TaskStatus.PAUSED)
        self.assertEqual(run.status, RunStatus.AWAITING_CLARIFICATION, run.error)
        done = core.resume_paused(owner, anchor)
        self.assertEqual(connector.execute_calls, 2, done.error)          # only step 2 ran
        self.assertIs(self.status(anchor), TaskStatus.COMPLETED, done.error)

    # -- F2: orderings that leave pause_requested with nothing driving it ------

    def test_pause_before_the_claim_loses_it_and_the_task_still_resumes(self) -> None:
        connector = DummyConnector()
        core = self.core(connector)
        opened = core.memory.open_execution_attempt
        seen = []

        def pause_then_open(attempt):
            if not seen:
                seen.append(core.pause(attempt.session_id, attempt.task_id))
            return opened(attempt)

        core.memory.open_execution_attempt = pause_then_open
        run = core.run(user_task())
        core.memory.open_execution_attempt = opened
        self.assertIs(self.status(run.task.id), TaskStatus.PAUSE_REQUESTED)
        self.assertEqual(connector.execute_calls, 0)                      # the lost claim crossed nothing
        done = core.resume_paused(run.session.id, run.task.id)
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        self.assertEqual(connector.execute_calls, 1)

    def test_a_delegated_step_parked_at_its_gate_resumes_and_asks_again(self) -> None:
        self.save_two_step_skill()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run = core.run(user_task())
        self.assertEqual(run.status, RunStatus.AWAITING_APPROVAL, run.error)
        anchor = self.store.load_session(run.session.id).objective.removeprefix("skill-run:")
        owner = self.store.task_session_id(anchor)
        paused = core.pause(owner, anchor)
        self.assertEqual(paused.status, RunStatus.AWAITING_CLARIFICATION, paused.error)
        again = core.resume_paused(owner, anchor)
        self.assertEqual(again.status, RunStatus.AWAITING_APPROVAL, again.error)
        self.assertIsNot(self.status(anchor), TaskStatus.PAUSE_REQUESTED)
        self.assertEqual(connector.execute_calls, 0)

    def test_a_stale_pause_request_resumes_after_restart_only_with_no_step_in_flight(self) -> None:
        _, _, run, _ = self.paused_mid_step()
        task = self.store.load_task(run.task.id)
        task.status = TaskStatus.PAUSE_REQUESTED                          # a crash before the boundary settled it
        self.store.save_task(run.session.id, task)
        with closing(sqlite3.connect(self.database)) as db, db:
            state = db.execute("SELECT state FROM execution_attempts WHERE task_id = ?", (run.task.id,)).fetchone()[0]
            db.execute("UPDATE execution_attempts SET state = ? WHERE task_id = ?",
                       (AttemptState.IN_FLIGHT.value, run.task.id))
        self.store = SkillStore(self.database)
        connector = DummyConnector()
        core = self.core(connector)
        refused = core.resume_paused(run.session.id, run.task.id)
        self.assertEqual(refused.status, RunStatus.FAILED)
        self.assertIs(self.status(run.task.id), TaskStatus.PAUSE_REQUESTED)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE execution_attempts SET state = ? WHERE task_id = ?", (state, run.task.id))
        done = core.resume_paused(run.session.id, run.task.id)
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        self.assertEqual(connector.execute_calls, 0)

    # -- DFPAUSEFIX: M2 a pause racing the next bind, M3 a crash mid-step -------

    def save_skill(self, steps: int) -> None:
        made = replace(base.draft(), steps=tuple(
            SkillStep(index=i, action=cancel.STEP, capability=DUMMY_CAPABILITY, inputs=("path",), check=None)
            for i in range(1, steps + 1)
        ))
        self.store.add_draft(made)
        self.store.approve_draft(made.draft_id, project_id=None)

    def delegated(self, steps: int, *, index=None, after=False, crash=False, mid_step=False):
        """Run a ``steps``-step skill; pause its anchor once just before (or just
        after) step ``index`` (0-based) binds, or during step 1 - and maybe die
        right there. Returns (core, connector, runner, anchor, owner)."""
        self.save_skill(steps)
        connector = HookConnector()
        core = self.core(connector)
        bind = core.memory.materialize_chain_step
        runners, fired = [], []

        def pause(session_id: str) -> None:
            fired.append(session_id)
            anchor = self.store.load_session(session_id).objective.removeprefix("skill-run:")
            core.pause(self.store.task_session_id(anchor), anchor)
            if crash:
                raise _SimulatedCrash

        def pause_at_bind(session_id, chain_id, digest, step_index, task):
            hit = step_index == index and not fired
            runners.append(session_id)
            if hit and not after:
                pause(session_id)
            bound = bind(session_id, chain_id, digest, step_index, task)
            if hit and after:
                pause(session_id)
            return bound

        core.memory.materialize_chain_step = pause_at_bind
        if mid_step:
            connector.hook = lambda session: pause(session.id)
        try:
            core.run(user_task())
        except _SimulatedCrash:
            pass
        core.memory.materialize_chain_step = bind
        anchor = self.store.load_session(runners[0]).objective.removeprefix("skill-run:")
        return core, connector, runners[0], anchor, self.store.task_session_id(anchor)

    def restart(self, connector=None):
        self.store = SkillStore(self.database)
        connector = connector or DummyConnector()
        return self.core(connector), connector

    def assertPausedThenResumesOnce(self, core, connector, owner, anchor, steps, ran):
        """PAUSED with ``ran`` steps executed; Resume runs only the rest, and a
        second Resume changes nothing."""
        self.assertIs(self.status(anchor), TaskStatus.PAUSED)
        self.assertEqual(connector.execute_calls, ran)
        done = core.resume_paused(owner, anchor)
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        self.assertEqual(connector.execute_calls, steps)
        core.resume_paused(owner, anchor)
        self.assertIs(self.status(anchor), TaskStatus.COMPLETED)
        self.assertEqual(connector.execute_calls, steps)

    def test_m2_a_pause_just_before_the_next_bind_is_never_lost(self) -> None:
        core, connector, _, anchor, owner = self.delegated(2, index=1)
        # Codex probe: step 2 ran and the anchor COMPLETED. Now it stops.
        self.assertPausedThenResumesOnce(core, connector, owner, anchor, steps=2, ran=1)

    def test_m2_a_pause_just_after_the_next_bind_pauses_the_bound_step(self) -> None:
        core, connector, runner, anchor, owner = self.delegated(2, index=1, after=True)
        step = self.store.load_session(runner).chain.step_task_ids[1]
        self.assertIs(self.status(step), TaskStatus.PAUSED)
        self.assertPausedThenResumesOnce(core, connector, owner, anchor, steps=2, ran=1)

    def crashed_mid_step(self):
        """Codex M3: Stop, then the process dies while step 1 executes."""
        _, _, runner, anchor, owner = self.delegated(2, mid_step=True, crash=True)
        step = self.store.load_session(runner).chain.step_task_ids[0]
        core, connector = self.restart()
        self.assertIs(self.status(anchor), TaskStatus.PAUSE_REQUESTED)
        self.assertIs(self.status(step), TaskStatus.PAUSE_REQUESTED)
        return core, connector, runner, anchor, owner, step

    def recover(self, step: str) -> None:
        """The operator's orphan recovery of the step's in-doubt attempt."""
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM execution_attempts WHERE task_id = ?", (step,)).fetchone()
        aged = datetime.fromisoformat(row["updated_at"]) + ORPHAN_MIN_AGE + timedelta(minutes=1)
        self.store.declare_attempt_orphaned(
            row["session_id"], step, row["id"], now=aged, actor_label="test", reason="crash"
        )
        self.store.abandon_unknown_attempt(row["session_id"], step, row["id"], AttemptRecovery.ABANDONED)

    def test_m3_resume_after_a_crash_mid_step_refuses_without_changing_anything(self) -> None:
        core, connector, _, anchor, owner, step = self.crashed_mid_step()
        for _ in range(2):
            refused = core.resume_paused(owner, anchor)
            self.assertEqual(refused.status, RunStatus.FAILED)
            self.assertIs(self.status(anchor), TaskStatus.PAUSE_REQUESTED)
            self.assertIs(self.status(step), TaskStatus.PAUSE_REQUESTED)
        self.assertEqual(connector.execute_calls, 0)
        self.recover(step)
        ended = core.resume_paused(owner, anchor)
        self.assertTrue(is_terminal(self.store.load_task(anchor)), ended.error)
        self.assertEqual(connector.execute_calls, 0)                      # nothing restarted the doubtful step

    def test_m3_cancel_after_a_crash_mid_step_reaches_a_terminal_state(self) -> None:
        core, connector, runner, anchor, owner, step = self.crashed_mid_step()
        self.assertEqual(core.cancel(owner, anchor).status, RunStatus.CANCELLING)
        self.recover(step)
        self.assertEqual(core.cancel(owner, anchor).status, RunStatus.CANCELLED)
        self.assertIs(self.status(anchor), TaskStatus.CANCELLED)
        self.assertTrue(is_terminal(self.store.load_task(step)))
        self.assertIs(self.store.load_session(runner).chain.status, ChainStatus.CANCELLED)
        self.assertEqual(connector.execute_calls, 0)

    def test_f2_every_boundary_of_one_two_and_three_step_chains(self) -> None:
        cases = [(n, i, after) for n in (1, 2, 3) for i in range(n) for after in (False, True)]
        for steps, index, after in cases:                                 # 12 inputs
            with self.subTest(steps=steps, index=index, after=after):
                self.setUp()
                core, connector, _, anchor, owner = self.delegated(steps, index=index, after=after)
                self.assertPausedThenResumesOnce(core, connector, owner, anchor, steps, ran=index)
        for steps in (1, 2, 3):                                           # 3 inputs: during step 1
            with self.subTest(steps=steps, mid_step=True):
                self.setUp()
                core, connector, _, anchor, owner = self.delegated(steps, mid_step=True)
                self.assertPausedThenResumesOnce(core, connector, owner, anchor, steps, ran=1)

    def test_f2_a_crash_at_each_bind_phase_waits_for_the_user(self) -> None:
        for after in (False, True):                                       # 2 inputs
            with self.subTest(after=after):
                self.setUp()
                _, _, _, anchor, owner = self.delegated(2, index=1, after=after, crash=True)
                core, connector = self.restart()
                self.assertIs(self.status(anchor), TaskStatus.PAUSE_REQUESTED)   # no auto-resume
                self.assertEqual(connector.execute_calls, 0)
                done = core.resume_paused(owner, anchor)
                self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
                self.assertEqual(connector.execute_calls, 1)              # only step 2, once
                core.resume_paused(owner, anchor)
                self.assertIs(self.status(anchor), TaskStatus.COMPLETED)
                self.assertEqual(connector.execute_calls, 1)

    def test_f2_approval_wait_cancel_and_a_typed_message_while_delegated_is_paused(self) -> None:
        with self.subTest("pause during the approval wait"):              # 3 inputs
            self.setUp()
            self.save_skill(2)
            connector = DummyConnector(always_requires_approval=True)
            core = self.core(connector)
            run = core.run(user_task())
            anchor = self.store.load_session(run.session.id).objective.removeprefix("skill-run:")
            owner = self.store.task_session_id(anchor)
            self.assertEqual(core.pause(owner, anchor).status, RunStatus.AWAITING_CLARIFICATION)
            self.assertIs(self.status(anchor), TaskStatus.PAUSED)
            old = PermissionGrant.for_request(run.permission_request)
            self.assertNotEqual(core.resume(run.session.id, run.permission_request.task_id, old).status,
                                RunStatus.COMPLETED)
            again = core.resume_paused(owner, anchor)
            self.assertEqual(again.status, RunStatus.AWAITING_APPROVAL, again.error)
            core.resume_paused(owner, anchor)
            self.assertNotIn(self.status(anchor), (TaskStatus.PAUSED, TaskStatus.COMPLETED))
            self.assertEqual(connector.execute_calls, 0)
        with self.subTest("cancel after pause"):
            self.setUp()
            core, connector, runner, anchor, owner = self.delegated(2, index=1)
            self.assertEqual(core.cancel(owner, anchor).status, RunStatus.CANCELLED)
            self.assertIs(self.store.load_session(runner).chain.status, ChainStatus.CANCELLED)
            self.assertEqual(core.resume_paused(owner, anchor).status, RunStatus.CANCELLED)
            self.assertIs(self.status(anchor), TaskStatus.CANCELLED)
            self.assertEqual(connector.execute_calls, 1)
        with self.subTest("typed message while paused"):
            self.setUp()
            core, connector, _, anchor, owner = self.delegated(2, index=1)
            other = core.run(user_task())
            self.assertNotIn(anchor, (other.task.id, other.session.objective.removeprefix("skill-run:")))
            self.assertIs(self.status(anchor), TaskStatus.PAUSED)
            ran = connector.execute_calls
            done = core.resume_paused(owner, anchor)
            self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
            self.assertEqual(connector.execute_calls, ran + 1)            # only the paused run's step 2

    def test_f2_a_runner_step_pauses_and_resumes_as_its_anchor(self) -> None:
        self.save_skill(2)
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run = core.run(user_task())
        runner, step = run.session.id, run.permission_request.task_id
        anchor = self.store.load_session(runner).objective.removeprefix("skill-run:")
        core.pause(runner, step)
        self.assertIs(self.status(anchor), TaskStatus.PAUSED)
        self.assertIs(self.status(step), TaskStatus.PAUSED)
        self.assertEqual(core.run_chain(runner)[0].status, RunStatus.AWAITING_CLARIFICATION)
        chain = self.store.load_session(runner).chain
        self.assertFalse(chain.step_task_ids[1:] and chain.step_task_ids[1])  # no step 2 started
        again = core.resume_paused(runner, step)
        self.assertEqual(again.status, RunStatus.AWAITING_APPROVAL, again.error)   # gate re-asked
        self.assertNotIn(self.status(anchor), (TaskStatus.PAUSED, TaskStatus.PAUSE_REQUESTED))
        self.assertEqual(connector.execute_calls, 0)

    # -- DFPAUSEFIX2: a typed "skill-run:<id>" objective is not a runner -------

    def test_a_forged_runner_objective_reaches_no_other_sessions_task(self) -> None:
        core = self.core(DummyConnector(always_requires_approval=True))
        victim = core.run(user_task())
        a = victim.task.id
        for objective in ("skill-run:" + a, "skill-run:" + a.upper(), " skill-run:" + a):
            forged = core.run(user_task(objective=objective))
            s, t = forged.session.id, forged.task.id
            for verb, mine in (
                (core.pause, TaskStatus.PAUSED),
                (core.resume_paused, TaskStatus.AWAITING_APPROVAL),
                (core.cancel, TaskStatus.CANCELLED),
            ):
                before = self.store.load_task(a)
                verb(s, t)
                self.assertEqual(self.store.load_task(a), before, (objective, verb.__name__))
                self.assertIs(self.status(t), mine, (objective, verb.__name__))   # its own task only
        self.assertIs(self.status(a), TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(core.pause(victim.session.id, a).status, RunStatus.AWAITING_CLARIFICATION)
        self.assertEqual(core.resume_paused(victim.session.id, a).status, RunStatus.AWAITING_APPROVAL)

    def test_a_forged_runner_session_neither_reads_nor_settles_the_victims_pause(self) -> None:
        connector = HookConnector()
        core = self.core(connector)
        seen = []

        def forge(session) -> None:
            a = session.latest_task_id
            core.pause(session.id, a)
            forged = core.start_session("skill-run:" + a)
            seen.append((core._skill_anchor_paused(forged), self.status(a)))
            core._skill_anchor_settle_pause(forged)
            seen.append((None, self.status(a)))

        connector.hook = forge
        core.run(user_task())
        self.assertEqual(seen, [(False, TaskStatus.PAUSE_REQUESTED), (None, TaskStatus.PAUSE_REQUESTED)])

    def test_f2_resume_after_a_cancel_of_a_crashed_step_stays_cancelled(self) -> None:
        core, connector, _, anchor, owner, step = self.crashed_mid_step()
        self.assertEqual(core.cancel(owner, anchor).status, RunStatus.CANCELLING)
        self.recover(step)
        self.assertEqual(core.resume_paused(owner, anchor).status, RunStatus.CANCELLED)
        self.assertIs(self.status(anchor), TaskStatus.CANCELLED)
        self.assertEqual(connector.execute_calls, 0)


class PauseUiTests(UiTestCase):
    """Stop on a running task pauses it; the task card resumes or cancels it."""

    item = test_ui.PresentationTests.item
    window = test_ui.PresentationTests.window

    def stored(self, task_id: str) -> str:
        return SQLiteMemoryStore(self.database).load_task(task_id).status.value

    def paused(self):
        """A task paused by Stop while its one step executes: (bridge, task_id, seen)."""
        test, seen = self, []

        class PausesMidStep(StubConnector):
            def execute(self, task, session):
                composer_said_cancel = test.current.cancelText()
                seen.append((composer_said_cancel, test.current.pauseTask(), test.current.taskPausing))
                return super().execute(task, session)

        self.claude = PausesMidStep("claude-code-plan", ("code_analysis", "code_planning"),
                                    side_effect=SideEffect.EXTERNAL_EGRESS, always_requires_approval=True)
        bridge = self.current = self.bridge(task_proposal("code_analysis"))
        bridge.submitText("Bu projeyi incele.")
        self.assertTrue(bridge.approvalPending)
        task_id = bridge._ticket.task_id
        bridge.approve()
        return bridge, task_id, seen

    def test_stop_pauses_at_the_step_boundary_and_resume_finishes_it(self) -> None:
        bridge, task_id, seen = self.paused()
        self.assertEqual(seen, [(False, True, True)])      # no text reply to cut: pause, never cancel
        self.assertEqual(self.claude.execute_calls, 1)     # the step in flight finished
        self.assertEqual(self.stored(task_id), "paused")
        self.assertTrue(bridge.taskPaused)
        bridge.resumePausedTask()
        self.assertEqual(self.stored(task_id), "completed")
        self.assertEqual(self.claude.execute_calls, 1)     # the finished step never re-ran
        self.assertFalse(bridge.taskPaused)

    def test_pause_with_nothing_running_changes_nothing(self) -> None:
        bridge = self.bridge(task_proposal("code_analysis"))
        bridge.submitText("Bu projeyi incele.")
        task_id = bridge._ticket.task_id
        self.assertFalse(bridge.pauseTask())
        self.assertEqual(self.stored(task_id), "awaiting_approval")
        self.assertTrue(bridge.approvalPending)

    def test_card_and_composer_key_resume_typed_text_sends_and_card_cancels(self) -> None:
        bridge, task_id, _ = self.paused()
        root = self.window(bridge)
        card, send = self.item(root, "pausedTaskCard"), self.item(root, "composerSend")
        self.assertTrue(card.isVisible())
        self.assertTrue(send.property("resuming"))
        field = self.item(root, "composerInput")
        field.setProperty("text", "yeni bir istek")
        pump(50)
        self.assertFalse(send.property("resuming"))        # typed text is a new request: Send
        field.setProperty("text", "")
        pump(50)
        self.assertTrue(send.property("resuming"))
        QMetaObject.invokeMethod(send, "act")
        wait_until(lambda: self.stored(task_id) == "completed", 2000)
        self.assertEqual(self.claude.execute_calls, 1)

    def test_card_cancel_is_terminal(self) -> None:
        bridge, task_id, _ = self.paused()
        root = self.window(bridge)
        QMetaObject.invokeMethod(self.item(root, "pausedTaskCancel"), "trigger")
        wait_until(lambda: self.stored(task_id) == "cancelled", 2000)
        self.assertFalse(bridge.taskPaused)
        bridge.resumePausedTask()
        self.assertEqual(self.stored(task_id), "cancelled")
        self.assertEqual(self.claude.execute_calls, 1)


if __name__ == "__main__":
    unittest.main()
