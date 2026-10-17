"""V2.2 SKCORE: Core uses a matched skill as a procedure, behind the flag.

Pins, through the real Core with fakes only (``DummyConnector``, an isolated
temp database, never data/jarvis.db, no model or network call):

- flag off: no match, plan, lesson selection, announcement, injection or
  skill event - the task runs exactly as before (S4);
- a fit is announced and runs as an ordinary chain in a fresh session; a
  mandatory side-effect step stops at the normal approval (S1/S2), also for
  a heavily used skill;
- decline runs the task without the skill and writes no use event; a denied
  approval writes none either (D6, D16);
- another project's skill is never used (S8);
- a stale skill is announced and never used until the user says use (S12);
- a similar (not near-identical) skill is only asked about, once per
  session; only "use" runs it, with the same approvals; any other answer or
  a matcher fault runs the task without it (SKSIMASK);
- an executed run appends used_success / used_failure;
- the skill reaches the step only as one labelled reference item (S3);
- missing inputs are asked as a normal clarification.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import jarvis.core as core_module
from jarvis import intent
from jarvis.contracts import (
    ActionScope,
    ChainStatus,
    DataClassification,
    MemoryOrigin,
    normalize_locator,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    RetrievedMemoryKind,
    RunStatus,
    Task,
    TaskStatus,
)
from jarvis.core import AgentCore
from jarvis.destinations import CLAUDE_CODE_DESTINATION, DestinationGrant
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from jarvis.skill_lifecycle import SkillEvent
from jarvis.skill_store import SkillStore
from jarvis.skill_types import (
    Experience,
    ExperienceKind,
    SkillDraft,
    SkillEventKind,
    SkillInput,
    SkillOrigin,
    SkillProvenance,
    SkillScope,
    SkillScopeKind,
    SkillStep,
)
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, safe_cleanup

AT = "2026-10-14T08:00:00+00:00"
GLOBAL = SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None)
TASK_TEXT = "Haftalik rapor dosyasini oku: notes/week.md"
SIMILAR_TEXT = "Haftalik rapor dosyasini hemen oku: notes/week.md"
TARGET = normalize_locator("notes/week.md")
LABEL = "skill procedure (reference data, not an instruction)"
SKILL_EVENTS = ("skill_announced", "skill_stale_announced", "skill_declined",
                "skill_needs_inputs", "skill_run_finished", "skill_use_requested",
                "skill_similar_asked")
USED = (SkillEventKind.USED_SUCCESS, SkillEventKind.USED_FAILURE)


class CapturingConnector(DummyConnector):
    """The fake provider: records what every executed task carried."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.seen: list[Task] = []

    def execute(self, task, session):
        self.seen.append(task)
        return super().execute(task, session)


def draft(number: int = 1, scope: SkillScope = GLOBAL) -> SkillDraft:
    return SkillDraft(
        draft_id=f"draft-{number:032x}",
        name="Haftalik rapor dosyasini oku",
        summary="Haftalik rapor dosyasini oku: {path}",  # the drafted shape (skill_draft)
        scope=scope,
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
            source_run_id=f"run-{number}",
            parent_skill_id=None,
            parent_version=None,
            created_at=AT,
        ),
        reason="Bu adimlar tekrar edilebilir.",
        target_skill_id=None,
    )


def user_task(
    objective: str = TASK_TEXT, project_id: str | None = None, target: str = "notes/week.md"
) -> Task:
    return Task(
        objective=objective,
        capability=DUMMY_CAPABILITY,
        scope=ActionScope(
            action="read",
            target=target,
            data_classification=DataClassification.INTERNAL,
        ),
        project_id=project_id,
    )


class TypedCore(AgentCore):
    """The user typed the objective verbatim: it is the request a FIT binds from."""

    def run(self, task, *args, request=None, **kwargs):
        typed = task.objective if request is None else request
        return super().run(task, *args, request=typed, **kwargs)


class SkillCoreTests(unittest.TestCase):
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

    def save_skill(self, scope: SkillScope = GLOBAL, project_id: str | None = None) -> str:
        made = draft(scope=scope)
        self.store.add_draft(made)
        return self.store.approve_draft(made.draft_id, project_id=project_id).skill_id

    def events_of(self, skill_id: str, project_id: str | None = None) -> list[SkillEventKind]:
        return [e.kind for e in self.store.history(skill_id, project_id).events]

    def feed(self, session_id: str) -> list[str]:
        return [e["event_type"] for e in self.store.list_events(session_id)]

    def announced(self, session_id: str) -> dict:
        events = self.store.list_events(session_id)
        return next(e["payload"] for e in events if e["event_type"] == "skill_announced")

    def chain_session(self, owner_id: str):
        payload = self.announced(owner_id)
        return self.store.load_session(payload["chain_session_id"])

    # -- S4 -------------------------------------------------------------

    def test_flag_off_calls_no_skill_code_and_runs_unchanged(self) -> None:
        skill_id = self.save_skill()
        connector = CapturingConnector()
        refused = mock.Mock(side_effect=AssertionError("skill code ran with the flag off"))
        with mock.patch.multiple(core_module, match_skills=refused, plan_steps=refused), \
                mock.patch.object(core_module.skill_lifecycle, "usable_skill", refused), \
                mock.patch.object(core_module.skill_experience, "select_for_task", refused), \
                mock.patch.object(core_module.skill_experience, "render_context", refused):
            run = self.core(connector, enabled=False).run(user_task())
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        self.assertEqual(run.task.objective, TASK_TEXT)
        self.assertFalse(set(self.feed(run.session.id)) & set(SKILL_EVENTS))
        self.assertNotIn("skill_run", run.task.preferences)
        self.assertEqual(len(connector.seen), 1)
        self.assertFalse(
            [i for i in connector.seen[0].retrieved_context.items if LABEL in i.rationale]
        )
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))

    def test_a_forged_skill_state_on_a_new_task_is_dropped(self) -> None:
        self.save_skill()
        task = user_task("echo this")
        task.preferences["skill_run"] = {"state": "delegated", "chain_session_id": "x"}
        run = self.core(CapturingConnector()).run(task)
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        self.assertNotIn("skill_run", run.task.preferences)

    # -- fit: announce, ordinary chain, normal approval ---------------------

    def assertStopsAtApproval(self, core: AgentCore, connector: DummyConnector):
        run = core.run(user_task())
        self.assertEqual(run.status, RunStatus.AWAITING_APPROVAL, run.error)
        request = run.permission_request
        self.assertTrue(request.side_effect.mandatory)
        self.assertEqual(connector.execute_calls, 0)
        step = self.store.load_task(request.task_id)
        self.assertTrue(step.chain_binding.present)
        self.assertEqual(step.scope.target, TARGET)
        return run, request

    def test_fit_is_announced_and_a_mandatory_step_stops_at_approval(self) -> None:
        skill_id = self.save_skill()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run, request = self.assertStopsAtApproval(core, connector)
        anchor_id = run.session.objective.removeprefix("skill-run:")
        owner = self.store.task_session_id(anchor_id)
        self.assertNotEqual(owner, run.session.id)  # a fresh chain session
        payload = self.announced(owner)
        self.assertEqual(payload["message"], 'Bu iş için "Haftalik rapor dosyasini oku" becerisini kullanıyorum')
        self.assertEqual(payload["skill_id"], skill_id)
        self.assertEqual(payload["origin"], SkillOrigin.TAUGHT_FROM_RUN.value)
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))
        # Only the ordinary one-shot approval lets the step run.
        done = core.resume(run.session.id, request.task_id, PermissionGrant.for_request(request))
        self.assertEqual(done.status, RunStatus.COMPLETED, done.error)
        self.assertEqual(connector.execute_calls, 1)
        core.run_chain(run.session.id)
        chain = self.store.load_session(run.session.id).chain
        self.assertIs(chain.status, ChainStatus.COMPLETED)
        self.assertIs(self.store.load_task(anchor_id).status, TaskStatus.COMPLETED)
        self.assertIn("skill_run_finished", self.feed(owner))
        self.assertEqual(self.events_of(skill_id).count(SkillEventKind.USED_SUCCESS), 1)
        # Once only: driving the finished chain again records nothing more.
        core.run_chain(run.session.id)
        self.assertEqual(self.events_of(skill_id).count(SkillEventKind.USED_SUCCESS), 1)

    def test_a_heavily_used_skill_still_stops_at_approval(self) -> None:
        skill_id = self.save_skill()
        for _ in range(50):
            self.store.append_event(
                SkillEvent(skill_id=skill_id, kind=SkillEventKind.USED_SUCCESS, version=1, at=AT)
            )
        connector = DummyConnector(always_requires_approval=True)
        self.assertStopsAtApproval(self.core(connector), connector)

    # -- D6 / D16 -----------------------------------------------------------

    def test_decline_runs_the_task_without_the_skill_and_writes_no_event(self) -> None:
        skill_id = self.save_skill()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run, request = self.assertStopsAtApproval(core, connector)
        anchor_id = run.session.objective.split(":", 1)[1]
        owner = self.store.task_session_id(anchor_id)
        declined = core.decline_skill(owner, anchor_id)
        # The user's own task now runs as usual (its own approval included).
        self.assertEqual(declined.task.id, anchor_id)
        self.assertEqual(declined.status, RunStatus.AWAITING_APPROVAL, declined.error)
        self.assertIn("skill_declined", self.feed(owner))
        self.assertIs(self.store.load_task(request.task_id).status, TaskStatus.CANCELLED)
        self.assertIs(self.store.load_session(run.session.id).chain.status, ChainStatus.CANCELLED)
        core.run_chain(run.session.id)
        self.assertEqual(connector.execute_calls, 0)
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))
        # The step's task never saw a skill reference in the declined run.
        anchor = self.store.load_task(anchor_id)
        self.assertFalse([i for i in anchor.retrieved_context.items if LABEL in i.rationale])

    def test_a_denied_approval_is_not_a_use(self) -> None:
        skill_id = self.save_skill()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run, request = self.assertStopsAtApproval(core, connector)
        denied = core.resume(
            run.session.id, request.task_id, PermissionGrant.for_request(request, approved=False)
        )
        self.assertEqual(denied.status, RunStatus.REJECTED)
        core.run_chain(run.session.id)
        self.assertIs(self.store.load_session(run.session.id).chain.status, ChainStatus.CANCELLED)
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))

    # -- S8 -----------------------------------------------------------------

    def test_another_projects_skill_is_never_used(self) -> None:
        alpha = self.store.create_project("Alpha").id
        beta = self.store.create_project("Beta").id
        self.save_skill(SkillScope(kind=SkillScopeKind.PROJECT, project_id=alpha), alpha)
        connector = CapturingConnector()
        core = self.core(connector)
        for project_id in (beta, None):
            with self.subTest(project=project_id):
                run = core.run(user_task(project_id=project_id))
                self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
                self.assertEqual(run.task.objective, TASK_TEXT)
                self.assertFalse(set(self.feed(run.session.id)) & set(SKILL_EVENTS))
        # In its own project the same skill is used.
        run = core.run(user_task(project_id=alpha))
        self.assertTrue(run.session.objective.startswith("skill-run:"))
        self.assertEqual(run.session.project_id, alpha)

    def test_a_more_sensitive_task_never_becomes_a_skill_run(self) -> None:
        self.save_skill()
        core = self.core(CapturingConnector())
        for level in (DataClassification.SENSITIVE, DataClassification.SECRET):
            with self.subTest(level=level):
                task = user_task()
                task.scope = replace(task.scope, data_classification=level)
                run = core.run(task)
                self.assertFalse(run.session.objective.startswith("skill-run:"))
                self.assertFalse(set(self.feed(run.session.id)) & set(SKILL_EVENTS))

    # -- S12 ----------------------------------------------------------------

    def make_stale(self, skill_id: str) -> None:
        self.store.append_event(
            SkillEvent(skill_id=skill_id, kind=SkillEventKind.STALE_MARKED, version=1, at=AT)
        )

    def test_a_stale_skill_is_announced_and_never_used_automatically(self) -> None:
        skill_id = self.save_skill()
        self.make_stale(skill_id)
        connector = CapturingConnector()
        core = self.core(connector)
        run = core.run(user_task())
        self.assertEqual(run.status, RunStatus.AWAITING_CLARIFICATION, run.error)
        self.assertIn("eskimiş", run.task.preferences["clarification_question"])
        self.assertIn("skill_stale_announced", self.feed(run.session.id))
        self.assertNotIn("skill_announced", self.feed(run.session.id))
        self.assertEqual(connector.execute_calls, 0)
        # Only the user's "use" runs it: a normal revalidation run.
        used = core.use_skill(run.session.id, run.task.id)
        self.assertEqual(used.status, RunStatus.COMPLETED, used.error)
        self.assertTrue(used.session.objective.startswith("skill-run:"))
        kinds = self.events_of(skill_id)
        self.assertIn(SkillEventKind.USED_SUCCESS, kinds)
        self.assertIn(SkillEventKind.REVALIDATED, kinds)

    def test_skipping_a_stale_skill_runs_the_task_without_it(self) -> None:
        skill_id = self.save_skill()
        self.make_stale(skill_id)
        core = self.core(CapturingConnector())
        run = core.run(user_task())
        skipped = core.decline_skill(run.session.id, run.task.id)
        self.assertEqual(skipped.status, RunStatus.COMPLETED, skipped.error)
        self.assertEqual(skipped.task.id, run.task.id)
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))

    # -- SKSIMASK: a similar skill is only asked about ----------------------

    def ask_similar(self, core: AgentCore, connector: DummyConnector, session_id=None):
        executed = connector.execute_calls
        run = core.run(user_task(SIMILAR_TEXT), session_id)
        self.assertEqual(run.status, RunStatus.AWAITING_CLARIFICATION, run.error)
        self.assertEqual(
            run.task.preferences["clarification_question"],
            'Bu iş için "Haftalik rapor dosyasini oku" becerisi var. '
            "Kullanayım mı, becerisiz mi çalışayım?",
        )
        self.assertIn("skill_similar_asked", self.feed(run.session.id))
        self.assertNotIn("skill_announced", self.feed(run.session.id))
        self.assertFalse(run.session.objective.startswith("skill-run:"))
        self.assertEqual(connector.execute_calls, executed)
        return run

    def test_a_similar_skill_is_asked_once_and_use_keeps_every_approval(self) -> None:
        skill_id = self.save_skill()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector)
        run = self.ask_similar(core, connector)
        used = core.use_skill(run.session.id, run.task.id)
        self.assertTrue(used.session.objective.startswith("skill-run:"))
        self.assertEqual(used.status, RunStatus.AWAITING_APPROVAL, used.error)
        self.assertTrue(used.permission_request.side_effect.mandatory)
        self.assertEqual(connector.execute_calls, 0)
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))

    def test_without_the_skill_and_a_free_answer_run_the_task_without_it(self) -> None:
        skill_id = self.save_skill()
        connector = CapturingConnector()
        core = self.core(connector)
        run = self.ask_similar(core, connector)
        declined = core.decline_skill(run.session.id, run.task.id)
        self.assertEqual(declined.status, RunStatus.COMPLETED, declined.error)
        self.assertEqual(declined.task.id, run.task.id)
        self.assertFalse(declined.session.objective.startswith("skill-run:"))
        self.assertIn("skill_declined", self.feed(run.session.id))
        # An unclear answer (even "evet") is not "use": no skill, fail closed.
        asked = core.run(user_task(SIMILAR_TEXT))
        answered = core.provide_clarification(asked.session.id, asked.task.id, "evet")
        self.assertEqual(answered.status, RunStatus.COMPLETED, answered.error)
        self.assertFalse(answered.session.objective.startswith("skill-run:"))
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))
        self.assertEqual(len(connector.seen), 2)

    def test_a_declined_or_asked_skill_is_not_asked_again_in_the_session(self) -> None:
        self.save_skill()
        connector = CapturingConnector()
        core = self.core(connector)
        run = self.ask_similar(core, connector)
        core.decline_skill(run.session.id, run.task.id)
        again = core.run(user_task(SIMILAR_TEXT), run.session.id)
        self.assertEqual(again.status, RunStatus.COMPLETED, again.error)
        self.assertEqual(self.feed(run.session.id).count("skill_similar_asked"), 1)
        # Another session is asked again.
        self.ask_similar(core, connector)

    def test_a_matcher_fault_gives_no_skill_and_no_question(self) -> None:
        self.save_skill()
        connector = CapturingConnector()
        broken = mock.Mock(side_effect=RuntimeError("matcher fault"))
        with mock.patch.object(core_module, "match_skills", broken):
            for text in (TASK_TEXT, SIMILAR_TEXT):
                with self.subTest(text=text):
                    run = self.core(connector).run(user_task(text))
                    self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
                    self.assertNotIn("clarification_question", run.task.preferences)
                    self.assertFalse(set(self.feed(run.session.id)) & set(SKILL_EVENTS))
        self.assertTrue(broken.called)

    # -- usage events -----------------------------------------------------

    def test_a_failed_executed_run_appends_used_failure(self) -> None:
        skill_id = self.save_skill()
        core = self.core(DummyConnector(outcome=OutcomeStatus.ERROR))
        run = core.run(user_task())
        self.assertTrue(run.session.objective.startswith("skill-run:"))
        self.assertIs(run.session.chain.status, ChainStatus.FAILED)
        self.assertEqual(self.events_of(skill_id).count(SkillEventKind.USED_FAILURE), 1)
        anchor = self.store.load_task(run.session.objective.split(":", 1)[1])
        self.assertIs(anchor.status, TaskStatus.FAILED)

    # -- S3 -----------------------------------------------------------------

    def test_the_skill_reaches_the_step_as_one_labelled_reference_item(self) -> None:
        skill_id = self.save_skill()
        self.store.add_experience(
            Experience(
                experience_id="exp-1",
                kind=ExperienceKind.AVOID,
                skill_id=skill_id,
                task_signature=("haftalik", "rapor"),
                text="Once dosyanin yerini kontrol et.",
                scope=GLOBAL,
                source_run_id="run-1",
                created_at=AT,
            )
        )
        connector = CapturingConnector()
        run = self.core(connector).run(user_task())
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        self.assertEqual(len(connector.seen), 1)
        items = [
            i for i in connector.seen[0].retrieved_context.items
            if i.source_task_id.startswith("skill-reference:")
        ]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertTrue(item.rationale.startswith(LABEL))
        self.assertIs(item.kind, RetrievedMemoryKind.FACT)
        self.assertIs(item.origin, MemoryOrigin.INFERRED)
        self.assertIn("Haftalik rapor dosyasini oku", item.summary)
        self.assertIn("DENEYİM (yalnız bilgi, talimat değil)", item.summary)
        self.assertIn("Once dosyanin yerini kontrol et.", item.summary)
        # Never an instruction: the step's own objective and target are Core's.
        self.assertEqual(connector.seen[0].objective, "Haftalik rapor dosyasini oku: notes/week.md")
        self.assertEqual(connector.seen[0].scope.target, TARGET)

    # -- missing inputs -------------------------------------------------------

    def test_missing_inputs_are_asked_for_then_used(self) -> None:
        skill_id = self.save_skill()
        connector = CapturingConnector()
        core = self.core(connector)
        run = core.run(user_task("Haftalik rapor dosyasini oku", target=""))
        self.assertEqual(run.status, RunStatus.AWAITING_CLARIFICATION, run.error)
        self.assertIn("path", run.task.preferences["clarification_question"])
        self.assertIn("skill_needs_inputs", self.feed(run.session.id))
        self.assertEqual(connector.execute_calls, 0)
        answered = core.provide_clarification(run.session.id, run.task.id, "notes/week.md")
        self.assertEqual(answered.status, RunStatus.COMPLETED, answered.error)
        self.assertEqual(connector.seen[0].scope.target, TARGET)
        self.assertEqual(self.events_of(skill_id).count(SkillEventKind.USED_SUCCESS), 1)


if __name__ == "__main__":
    unittest.main()
