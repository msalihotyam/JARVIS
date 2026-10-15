"""V2.2 research b8: a duplicate check at offer time (AWM abstract signature).

Through the real Core with fakes only (an isolated temp database, never
data/jarvis.db, no model or network call): a skill-less completed run whose
abstract step signature (capabilities + target kinds, words ignored) equals an
existing skill's in scope is offered as a PENDING next version of that skill;
the same family with another signature as an adaptation (a new skill with
parent provenance); a skill of another project changes nothing; a failed run
offers nothing (a5). Nothing here approves anything.
"""

from __future__ import annotations

import unittest

from jarvis.contracts import RunStatus
from jarvis.skill_draft import build_drafts, step_family, step_signature
from jarvis.skill_lifecycle import SkillEvent, SkillEventKind
from jarvis.skill_types import RunOutcome, RunStep, RunSummary, SkillOrigin
from tests.helpers import DUMMY_CAPABILITY, DummyConnector
from tests import test_skill_learn as base
from tests.test_skill_learn import AT, user_task


def summary(
    action: str,
    *,
    run_id: str = "run-b8",
    project_id: str | None = None,
    status: RunOutcome = RunOutcome.COMPLETED,
) -> RunSummary:
    return RunSummary(
        run_id=run_id,
        task_text=action,
        project_id=project_id,
        status=status,
        steps=(RunStep(action=action, capability=DUMMY_CAPABILITY, outcome=status, error=None),),
        user_correction=None,
        matched_skill=None,
    )


READ_WEEK = "Haftalik rapor dosyasini oku: notes/week.md"
OPEN_MONTH = "Aylik ozet dosyasini ac: docs/month.md"
FETCH_DATE = "Haftalik raporu indir: 2026-10-14"


class SkillOfferDedupTests(unittest.TestCase):
    # SKLEARN's fixture (isolated temp database, flag on), not its tests.
    setUp = base.SkillLearnTests.setUp
    core = base.SkillLearnTests.core
    save_skill = base.SkillLearnTests.save_skill
    offers = base.SkillLearnTests.offers

    def learn(self, run: RunSummary) -> list[str]:
        return self.core(DummyConnector())._skill_learn(run)["draft_ids"]

    def saved_from(self, run: RunSummary) -> str:
        """X: the run's own draft, approved by the user."""
        [draft_id] = self.learn(run)
        return self.store.approve_draft(draft_id, project_id=run.project_id).skill_id

    def test_identical_signature_proposes_a_pending_next_version_of_x(self) -> None:
        skill_id = self.saved_from(summary(READ_WEEK, run_id="run-x"))
        [draft_id] = self.learn(summary(OPEN_MONTH))
        [made] = self.store.pending_drafts(None)
        self.assertEqual(made.draft_id, draft_id)
        self.assertEqual(made.target_skill_id, skill_id)
        self.assertEqual(
            (made.provenance.origin, made.provenance.parent_skill_id,
             made.provenance.parent_version, made.provenance.source_run_id),
            (SkillOrigin.VERSION_AFTER_FAILURE, skill_id, 1, "run-b8"),
        )
        self.assertEqual(made.steps[0].action, "Aylik ozet dosyasini ac: {path}")
        # Pending, not saved: X is still only version 1, and no second skill.
        self.assertEqual([v.version for v in self.store.history(skill_id, None).versions], [1])
        self.assertEqual([s.skill.skill_id for s in self.store.list_skills(None)], [skill_id])
        # The same procedure again is X itself: no duplicate is offered.
        self.assertEqual(self.learn(summary("Haftalik rapor dosyasini oku: a/b.md", run_id="r2")), [])

    def test_a_stale_or_failing_x_still_gets_the_version_proposal(self) -> None:
        skill_id = self.saved_from(summary(READ_WEEK, run_id="run-x"))
        for kind in (SkillEventKind.USED_FAILURE, SkillEventKind.STALE_MARKED):
            self.store.append_event(SkillEvent(skill_id=skill_id, kind=kind, version=1, at=AT))
        self.learn(summary(OPEN_MONTH))
        [made] = self.store.pending_drafts(None)
        self.assertEqual(made.target_skill_id, skill_id)

    def test_same_family_other_signature_proposes_an_adaptation(self) -> None:
        skill_id = self.saved_from(summary(READ_WEEK, run_id="run-x"))
        [draft_id] = self.learn(summary(FETCH_DATE))
        [made] = self.store.pending_drafts(None)
        self.assertEqual(made.draft_id, draft_id)
        self.assertIsNone(made.target_skill_id)
        self.assertEqual(
            (made.provenance.origin, made.provenance.parent_skill_id, made.provenance.parent_version),
            (SkillOrigin.ADAPTED, skill_id, 1),
        )
        self.assertEqual(made.inputs[0].name, "date")
        # Approving it is the user's act and makes a NEW skill; X stays.
        new = self.store.approve_draft(draft_id, project_id=None)
        self.assertNotEqual(new.skill_id, skill_id)
        self.assertEqual([v.version for v in self.store.history(skill_id, None).versions], [1])

    def test_a_skill_of_another_project_leaves_the_run_independent(self) -> None:
        self.saved_from(summary(READ_WEEK, run_id="run-x", project_id="proj-a"))
        [draft_id] = self.learn(summary(OPEN_MONTH, project_id="proj-b"))
        [made] = self.store.pending_drafts("proj-b")
        self.assertEqual(made.draft_id, draft_id)
        self.assertIsNone(made.target_skill_id)
        self.assertIs(made.provenance.origin, SkillOrigin.TAUGHT_FROM_RUN)
        self.assertEqual(made.scope.project_id, "proj-b")

    def test_a_global_x_never_takes_a_project_runs_words_and_no_duplicate_is_offered(self) -> None:
        self.saved_from(summary(READ_WEEK, run_id="run-x"))
        self.assertEqual(self.learn(summary(OPEN_MONTH, project_id="proj-b")), [])
        self.assertEqual(self.store.pending_drafts("proj-b"), ())

    def test_a_words_only_difference_is_the_same_signature(self) -> None:
        [week] = build_drafts(summary(READ_WEEK), created_at=AT)
        [month] = build_drafts(summary(OPEN_MONTH), created_at=AT)
        [date] = build_drafts(summary(FETCH_DATE), created_at=AT)
        self.assertNotEqual(week.steps, month.steps)
        self.assertEqual(step_signature(week.steps), step_signature(month.steps))
        self.assertEqual(step_signature(week.steps), ((DUMMY_CAPABILITY, ("path",)),))
        self.assertNotEqual(step_signature(week.steps), step_signature(date.steps))
        self.assertEqual(step_family(week.steps), step_family(date.steps))

    def test_a_failed_run_offers_no_draft(self) -> None:
        self.saved_from(summary(READ_WEEK, run_id="run-x"))
        self.assertEqual(self.learn(summary(OPEN_MONTH, status=RunOutcome.FAILED)), [])
        self.assertEqual(self.store.pending_drafts(None), ())

    def test_a_declined_skill_run_is_offered_as_its_next_version(self) -> None:
        skill_id = self.saved_from(summary(READ_WEEK, run_id="run-x"))
        self.store.append_event(
            SkillEvent(skill_id=skill_id, kind=SkillEventKind.STALE_MARKED, version=1, at=AT)
        )
        core = self.core(DummyConnector())
        run = core.run(user_task(READ_WEEK + " hemen"))
        self.assertEqual(run.status, RunStatus.AWAITING_CLARIFICATION, run.error)
        done = core.decline_skill(run.session.id, run.task.id)
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        [offer] = self.offers(run.session.id)
        [made] = self.store.pending_drafts(None)
        self.assertEqual(offer["draft_ids"], [made.draft_id])
        self.assertEqual(made.target_skill_id, skill_id)


if __name__ == "__main__":
    unittest.main()
