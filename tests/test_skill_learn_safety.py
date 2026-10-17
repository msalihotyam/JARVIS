"""V2.2 SKLEARNSAFE (research a5 + a6): undefined skill behaviours, fail closed.

Pins, through the real Core with fakes only (``DummyConnector``, an isolated
temp database, never data/jarvis.db, no model or network call):

- a5: a new-version draft comes ONLY from a run that ended ``completed`` and
  executed the version's steps; a failed run records ``used_failure`` and
  proposes nothing that changes the procedure;
- a6: a SIMILAR match never makes Core use or announce a skill; it is only
  asked about once (SKSIMASK), and any answer but "use" runs without it;
- a6: a draft that would not pass the use path's plan (``plan_steps`` and the
  one chain grammar: at most three independent catalog steps) is never
  stored or offered.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from jarvis.contracts import ChainStatus, OutcomeStatus, RunStatus
from jarvis.skill_match import MatchKind, SkillWithState, match_skills
from jarvis.skill_lifecycle import SkillEventKind, health
from jarvis.skill_types import RunOutcome, RunStep, RunSummary, SkillRef, SkillStatus
from tests.helpers import DUMMY_CAPABILITY, DummyConnector
from tests import test_skill_learn as base
from tests.test_skill_learn import skill_task, user_task

SIMILAR_TEXT = "Haftalik rapor hazirla"
SKILL_EVENTS = ("skill_announced", "skill_stale_announced", "skill_needs_inputs")


def step(action: str, capability: str = DUMMY_CAPABILITY, outcome=RunOutcome.COMPLETED) -> RunStep:
    return RunStep(action=action, capability=capability, outcome=outcome, error=None)


def summary(steps, *, status=RunOutcome.COMPLETED, correction=None) -> RunSummary:
    return RunSummary(
        run_id="run-safe",
        task_text="Haftalik rapor dosyasini oku: notes/week.md",
        project_id=None,
        status=status,
        steps=tuple(steps),
        user_correction=correction,
        matched_skill=None,
    )


class SkillLearnSafetyTests(unittest.TestCase):
    # SKLEARN's fixture (isolated temp database, flag on), not its tests.
    setUp = base.SkillLearnTests.setUp
    core = base.SkillLearnTests.core
    save_skill = base.SkillLearnTests.save_skill
    offers = base.SkillLearnTests.offers
    pending = base.SkillLearnTests.pending

    def learn(self, run: RunSummary, *, with_skill: bool) -> dict:
        core = self.core(DummyConnector())
        if not with_skill:
            return core._skill_learn(run)
        skill_id = self.save_skill()
        history = self.store.history(skill_id, None)
        # As Core summarises a skill run: the version used is named.
        run = replace(run, matched_skill=SkillRef(skill_id=skill_id, version=1))
        return core._skill_learn(
            run, skill=history.versions[0], history=history, health=health(history)
        )

    # -- a5: a new version only from completed, executed evidence -------------

    def test_a5_a_failed_skill_run_records_used_failure_and_proposes_no_version(self) -> None:
        skill_id = self.save_skill()
        run = self.core(DummyConnector(outcome=OutcomeStatus.ERROR)).run(skill_task())
        self.assertIs(run.session.chain.status, ChainStatus.FAILED)
        anchor_id = run.session.objective.removeprefix("skill-run:")
        self.assertEqual(self.offers(self.store.task_session_id(anchor_id)), [])
        self.assertEqual(self.pending(), [])
        history = self.store.history(skill_id, None)
        self.assertEqual([v.version for v in history.versions], [1])
        self.assertIn(SkillEventKind.USED_FAILURE, [e.kind for e in history.events])

    def test_a5_a_failed_run_with_a_correction_proposes_no_version(self) -> None:
        run = summary(
            [step("Haftalik rapor dosyasini oku: notes/week.md", outcome=RunOutcome.FAILED)],
            status=RunOutcome.FAILED,
            correction="Once yedek dosyayi kontrol et",
        )
        self.assertEqual(self.learn(run, with_skill=True)["draft_ids"], [])
        self.assertEqual(self.pending(), [])

    def test_a5_a_completed_run_with_a_step_that_did_not_execute_proposes_no_version(self) -> None:
        run = summary(
            [
                step("Haftalik rapor dosyasini oku: notes/week.md"),
                step("Haftalik rapor dosyasini oku: notes/week.md", outcome=RunOutcome.FAILED),
            ],
            correction="Once yedek dosyayi kontrol et",
        )
        self.assertEqual(self.learn(run, with_skill=True)["draft_ids"], [])
        self.assertEqual(self.pending(), [])

    def test_a5_a_completed_run_that_executed_other_steps_proposes_no_version(self) -> None:
        run = summary(
            [step("Haftalik rapor dosyasini oku: notes/week.md")] * 2,
            correction="Once yedek dosyayi kontrol et",
        )
        self.assertEqual(self.learn(run, with_skill=True)["draft_ids"], [])
        self.assertEqual(self.pending(), [])

    def test_a5_a_corrected_completed_run_of_the_skill_steps_still_proposes_a_version(self) -> None:
        run = summary(
            [step("Haftalik rapor dosyasini oku: notes/week.md")],
            correction="Once yedek dosyayi kontrol et",
        )
        [draft_id] = self.learn(run, with_skill=True)["draft_ids"]
        [made] = self.store.pending_drafts(None)
        self.assertEqual(made.draft_id, draft_id)
        self.assertEqual(made.steps, self.store.history(made.target_skill_id, None).versions[0].steps)

    # -- a6: SIMILAR never uses a skill ---------------------------------------

    def test_a6_a_similar_skill_is_never_used_or_announced_only_asked_about(self) -> None:
        skill_id = self.save_skill()
        task = user_task(SIMILAR_TEXT)
        history = self.store.history(skill_id, None)
        entry = SkillWithState(
            skill=history.versions[0], status=SkillStatus.ACTIVE, health=health(history)
        )
        text = "\n".join((task.objective, task.scope.target))
        self.assertIs(match_skills(text, (entry,), None).kind, MatchKind.SIMILAR)
        core = self.core(DummyConnector())
        asked = core.run(task)
        # SKSIMASK: one question, nothing used or announced yet.
        self.assertEqual(asked.status, RunStatus.AWAITING_CLARIFICATION, asked.error)
        self.assertTrue(asked.task.preferences.get("needs_clarification"))
        self.assertFalse(asked.session.objective.startswith("skill-run:"))
        self.assertIsNone(asked.session.chain)
        # An answer that is not "use" runs the task without the skill.
        run = core.provide_clarification(asked.session.id, asked.task.id, "bilmiyorum")
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        self.assertFalse(run.session.objective.startswith("skill-run:"))
        self.assertIsNone(run.session.chain)
        events = [e["event_type"] for e in self.store.list_events(run.session.id)]
        self.assertFalse(set(SKILL_EVENTS) & set(events), events)
        # A late "use" answer cannot turn the finished similar match into a use.
        core.use_skill(run.session.id, run.task.id)
        history = self.store.history(skill_id, None)
        self.assertEqual(
            [e.kind for e in history.events if e.kind.value.startswith("used_")], []
        )

    # -- a6: an unplannable draft is never offered -----------------------------

    def test_a6_a_draft_with_more_steps_than_one_chain_is_never_offered(self) -> None:
        run = summary([step(f"Haftalik rapor dosyasini oku: notes/week{n}.md") for n in range(4)])
        learned = self.learn(run, with_skill=False)
        self.assertEqual(learned["draft_ids"], [])
        self.assertEqual(self.pending(), [])

    def test_a6_a_draft_with_a_capability_outside_the_catalog_is_never_offered(self) -> None:
        run = summary(
            [step("Haftalik rapor dosyasini oku: notes/week.md", capability="not_served")] * 2
        )
        self.assertEqual(self.learn(run, with_skill=False)["draft_ids"], [])
        self.assertEqual(self.pending(), [])

    def test_a6_a_plannable_draft_is_still_offered(self) -> None:
        run = summary([step(f"Haftalik rapor dosyasini oku: notes/week{n}.md") for n in range(3)])
        learned = self.learn(run, with_skill=False)
        self.assertTrue(learned["draft_ids"])
        self.assertEqual(sorted(learned["draft_ids"]), sorted(self.pending()))


if __name__ == "__main__":
    unittest.main()
