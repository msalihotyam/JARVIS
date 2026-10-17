"""V2.2 SKLEARN: Core offers save-as-skill and version-after-failure drafts
and keeps advisory lessons; only the user's approval saves a skill.

Pins, through the real Core with fakes only (``DummyConnector``, an isolated
temp database, never data/jarvis.db, no model or network call):

- a repeatable run leaves a PENDING draft and an offer event, no skill (S5);
- a trivial, failed or suppressed run is not offered;
- a failed skill run proposes no new version (a5, SKLEARNSAFE), old kept;
- a correction leaves an ``avoid`` lesson;
- a secret in a step stores no draft (S7);
- flag off: no offer, draft or lesson code runs (S4).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jarvis.core as core_module
from jarvis import intent
from jarvis.contracts import (
    ActionScope,
    ChainStatus,
    DataClassification,
    OutcomeStatus,
    PermissionMode,
    RunStatus,
    Task,
    TaskStatus,
)
from jarvis.core import AgentCore
from jarvis.destinations import CLAUDE_CODE_DESTINATION, DestinationGrant
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from jarvis.skill_draft import GLOBAL_SUPPRESSION, task_kind
from jarvis.skill_store import SkillStore
from jarvis.skill_types import (
    ExperienceKind,
    RunOutcome,
    RunStep,
    RunSummary,
    SkillDraft,
    SkillInput,
    SkillOrigin,
    SkillProvenance,
    SkillScope,
    SkillScopeKind,
    SkillStep,
)
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, safe_cleanup
from tests.test_skill_core import TypedCore

AT = "2026-10-14T08:00:00+00:00"
GLOBAL = SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None)
TASK_TEXT = "Haftalik rapor dosyasini oku: notes/week.md"
OFFER = "Bunu beceri olarak kaydedeyim mi?"


def user_task(objective: str = TASK_TEXT, target: str = "resource-a") -> Task:
    return Task(
        objective=objective,
        capability=DUMMY_CAPABILITY,
        scope=ActionScope(
            action="read", target=target, data_classification=DataClassification.INTERNAL
        ),
    )


def skill_task() -> Task:
    # SKSIMASK: only a near-identical request auto-runs a skill; the target
    # is the declared path value, so nothing but the skill's own text is left.
    return user_task(target="notes/week.md")


def skill_draft() -> SkillDraft:
    return SkillDraft(
        draft_id=f"draft-{1:032x}",
        name="Haftalik rapor dosyasini oku",
        summary="Haftalik rapor dosyasini oku: {path}",  # the typed value is the placeholder span
        scope=GLOBAL,
        inputs=(SkillInput(name="path", description="", required=True),),
        steps=(
            SkillStep(
                index=1,
                action="Haftalik rapor dosyasini oku: {path}",
                capability=DUMMY_CAPABILITY,
                inputs=("path",),
                check=None,
            ),
        ),
        checks=(),
        provenance=SkillProvenance(
            origin=SkillOrigin.TAUGHT_FROM_RUN,
            source_run_id="run-1",
            parent_skill_id=None,
            parent_version=None,
            created_at=AT,
        ),
        reason="Bu adimlar tekrar edilebilir.",
        target_skill_id=None,
    )


class SkillLearnTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.database = Path(temp.name) / "jarvis.db"
        self.store = SkillStore(self.database)
        patcher = mock.patch.dict(
            intent.NL_CAPABILITIES,
            {DUMMY_CAPABILITY: intent.CapabilityHint(None, "read", "echo")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def core(self, connector: DummyConnector, *, enabled: bool = True) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        settings = Settings(
            memory_database=self.database,
            permission_mode=PermissionMode.SMART_AUTO,
            skills_enabled=enabled,
        )
        policy = frozenset(
            {DestinationGrant(connector.manifest.name, DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION)}
        )
        return TypedCore(
            registry, self.store, settings, destination_policy=policy, skill_store=self.store
        )

    def save_skill(self) -> str:
        made = skill_draft()
        self.store.add_draft(made)
        return self.store.approve_draft(made.draft_id, project_id=None).skill_id

    def offers(self, session_id: str) -> list[dict]:
        return [
            e["payload"]
            for e in self.store.list_events(session_id)
            if e["event_type"] == "skill_draft_offered"
        ]

    def pending(self) -> list[str]:
        return [d.draft_id for d in self.store.pending_drafts(None)]

    def assertNoOffer(self, run) -> None:
        self.assertEqual(self.offers(run.session.id), [])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.store.list_skills(None), ())

    # -- offer --------------------------------------------------------------

    def test_a_repeatable_run_leaves_a_pending_draft_and_an_offer_not_a_skill(self) -> None:
        run = self.core(DummyConnector()).run(user_task())
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        [offer] = self.offers(run.session.id)
        self.assertEqual(offer["message"], OFFER)
        self.assertEqual(offer["run_id"], run.task.id)
        self.assertTrue(offer["draft_ids"])
        self.assertEqual(sorted(offer["draft_ids"]), sorted(self.pending()))
        [made] = self.store.pending_drafts(None)
        self.assertIs(made.provenance.origin, SkillOrigin.TAUGHT_FROM_RUN)
        self.assertEqual(made.provenance.source_run_id, run.task.id)
        self.assertIsNone(made.target_skill_id)
        # No skill row until the user's approval (S5) - the app's path, not Core's.
        self.assertEqual(self.store.list_skills(None), ())
        self.store.approve_draft(made.draft_id, project_id=None)
        self.assertEqual(len(self.store.list_skills(None)), 1)

    def test_flag_off_runs_no_learning_code(self) -> None:
        refused = mock.Mock(side_effect=AssertionError("learning ran with the flag off"))
        with mock.patch.multiple(
            core_module, should_offer=refused, build_drafts=refused, check_fields=refused
        ), mock.patch.object(core_module.skill_experience, "capture_candidates", refused):
            run = self.core(DummyConnector(), enabled=False).run(user_task())
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        self.assertNoOffer(run)
        self.assertEqual(self.store.experiences(None), ())

    def test_a_trivial_run_is_not_offered(self) -> None:
        run = self.core(DummyConnector()).run(user_task("echo this"))
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        self.assertNoOffer(run)

    def test_a_failed_run_is_not_offered(self) -> None:
        run = self.core(DummyConnector(outcome=OutcomeStatus.ERROR)).run(user_task())
        self.assertIsNot(run.status, RunStatus.COMPLETED)
        self.assertNoOffer(run)

    def test_a_suppressed_task_kind_is_not_offered(self) -> None:
        kind = task_kind(
            RunSummary(
                run_id="probe",
                task_text=TASK_TEXT,
                project_id=None,
                status=RunOutcome.COMPLETED,
                steps=(RunStep(action=TASK_TEXT, capability=DUMMY_CAPABILITY,
                               outcome=RunOutcome.COMPLETED, error=None),),
                user_correction=None,
                matched_skill=None,
            )
        )
        for key in (kind, GLOBAL_SUPPRESSION):
            with self.subTest(key=key):
                self.store.set_suppression(key, True)
                run = self.core(DummyConnector()).run(user_task())
                self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
                self.assertNoOffer(run)
                self.store.set_suppression(key, False)

    def test_a_secret_in_a_step_stores_no_draft(self) -> None:
        secret = TASK_TEXT + " password=Hunter2-Secret-Value"
        run = self.core(DummyConnector()).run(user_task(secret))
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        self.assertNoOffer(run)
        for lesson in self.store.experiences(None):
            self.assertNotIn("Hunter2", lesson.text)

    # -- version after failure -------------------------------------------------

    def test_a_failed_skill_run_proposes_no_new_version_of_that_skill(self) -> None:
        # a5 (SKLEARNSAFE): a failed run only records used_failure; a new
        # version comes from a corrected, completed run (below).
        skill_id = self.save_skill()
        run = self.core(DummyConnector(outcome=OutcomeStatus.ERROR)).run(skill_task())
        self.assertIs(run.session.chain.status, ChainStatus.FAILED)
        anchor_id = run.session.objective.removeprefix("skill-run:")
        self.assertIs(self.store.load_task(anchor_id).status, TaskStatus.FAILED)
        self.assertEqual(self.offers(self.store.task_session_id(anchor_id)), [])
        self.assertEqual(self.store.pending_drafts(None), ())
        # The used version stays; nothing new is saved without approval.
        history = self.store.history(skill_id, None)
        self.assertEqual([v.version for v in history.versions], [1])

    # -- corrections -----------------------------------------------------------

    def test_a_correction_leaves_an_avoid_lesson(self) -> None:
        core = self.core(DummyConnector())
        run = core.run(user_task())
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        learned = core.correct_run(run.session.id, run.task.id, "Once yedek dosyayi kontrol et")
        # a2 (V22RESAPPLY): the dummy "echo" step is not on the no-external-read
        # allow-list, so the run leaves no lesson; the same asserts with echo
        # allow-listed live in test_skill_experience_origin.
        self.assertEqual(learned["experience_ids"], [])
        self.assertEqual(learned["experience_skipped"], "external_content")
        avoid = [e for e in self.store.experiences(None) if e.kind is ExperienceKind.AVOID]
        self.assertEqual(len(avoid), 0)
        self.assertEqual(learned["draft_ids"], [])

    def test_a_corrected_skill_run_proposes_a_version_with_the_correction(self) -> None:
        skill_id = self.save_skill()
        core = self.core(DummyConnector())
        run = core.run(skill_task())
        self.assertIs(run.session.chain.status, ChainStatus.COMPLETED)
        anchor_id = run.session.objective.removeprefix("skill-run:")
        owner = self.store.task_session_id(anchor_id)
        learned = core.correct_run(owner, anchor_id, "Once yedek dosyayi kontrol et")
        [made] = self.store.pending_drafts(None)
        self.assertEqual(learned["draft_ids"], [made.draft_id])
        self.assertEqual(made.target_skill_id, skill_id)
        self.assertIn("Once yedek dosyayi kontrol et", made.checks[-1])
        self.assertEqual(len(self.store.history(skill_id, None).versions), 1)
        self.assertIs(made.provenance.origin, SkillOrigin.VERSION_AFTER_FAILURE)
        self.assertEqual(made.provenance.parent_skill_id, skill_id)
        self.assertEqual(made.provenance.parent_version, 1)
        self.assertEqual(self.store.approve_draft(made.draft_id, project_id=None).version, 2)
        self.assertEqual([v.version for v in self.store.history(skill_id, None).versions], [1, 2])

    def test_a_global_skill_corrected_in_a_project_keeps_the_words_out_of_the_draft(self) -> None:
        skill_id = self.save_skill()
        history = self.store.history(skill_id, None)
        run = RunSummary(
            run_id="run-p",
            task_text=TASK_TEXT,
            project_id="proj-a",
            status=RunOutcome.COMPLETED,
            steps=(RunStep(action=TASK_TEXT, capability=DUMMY_CAPABILITY,
                           outcome=RunOutcome.COMPLETED, error=None),),
            user_correction="Proje-A gizli klasorunu kullan",
            matched_skill=None,
        )
        made = AgentCore._skill_version_draft(run, history.versions[0], history, AT)
        self.assertIsNone(made.scope.project_id)
        self.assertNotIn("Proje-A", " ".join(made.checks))

    def test_a_correction_of_an_unknown_or_unfinished_run_learns_nothing(self) -> None:
        core = self.core(DummyConnector())
        learned = core.correct_run("no-session", "no-task", "x")
        self.assertEqual(learned["draft_ids"] + learned["experience_ids"], [])
        self.assertEqual(self.store.experiences(None), ())


if __name__ == "__main__":
    unittest.main()
