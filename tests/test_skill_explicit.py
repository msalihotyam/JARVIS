"""V2.2 b4 (SKEXPLICIT): explicit skill use, "/beceri <ad>" and run_skill.

Through the real Core with fakes only (``DummyConnector``, an isolated temp
database, never data/jarvis.db, no model or network call):

- naming a skill runs it through exactly the implicit FIT path: the same
  announcement, missing-inputs question and fresh session, and the same Core
  decision in strict / session / smart_auto with {0 grants, a matching grant,
  smart_auto auto-approval}; no grant is ever minted;
- names resolve folded (case, Turkish i, diacritics, ASCII spelling) or by id;
  unknown / ambiguous / out of scope / stale / disabled / unapproved / flag
  off are refused with a fixed reason and nothing is created or run;
- an explicit use counts in health like an implicit one; a decline at the
  announcement is cancelled, never a failure;
- "/beceri" is a command only as the whole message.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import unittest
from dataclasses import replace
from unittest import mock

from jarvis.contracts import (
    ChainStatus,
    ErrorCode,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    RiskLevel,
    RunStatus,
    TaskStatus,
)
from jarvis.core import (
    SKILL_REFUSED_AMBIGUOUS,
    SKILL_REFUSED_DISABLED,
    SKILL_REFUSED_OFF,
    SKILL_REFUSED_SCOPE,
    SKILL_REFUSED_SESSION,
    SKILL_REFUSED_STALE,
    SKILL_REFUSED_UNAPPROVED,
    SKILL_REFUSED_UNKNOWN,
)
from jarvis.skill_lifecycle import SkillEvent, health
from jarvis.skill_types import SkillEventKind, SkillScope, SkillScopeKind
from tests.helpers import DummyConnector, isolated_settings, open_app
from tests.test_skill_authority_matrix import (
    AT,
    MODES,
    SITUATIONS,
    UnapprovedVersionTests,
    _Base,
    decision,
    draft,
    user_task,
)

NAME = "Haftalik rapor dosyasini oku"
ANSWER = "notes/week.md"
USED = (SkillEventKind.USED_SUCCESS, SkillEventKind.USED_FAILURE)


def named_draft(name: str, number: int = 1, scope: SkillScope | None = None):
    made = draft()
    return replace(
        made,
        draft_id=f"draft-{number:032x}",
        name=name,
        scope=scope or made.scope,
    )


class _Explicit(_Base):
    def save(self, name: str = NAME, number: int = 1, project_id: str | None = None) -> str:
        scope = (
            SkillScope(kind=SkillScopeKind.PROJECT, project_id=project_id)
            if project_id is not None
            else None
        )
        made = named_draft(name, number, scope)
        self.store.add_draft(made)
        return self.store.approve_draft(made.draft_id, project_id=project_id).skill_id

    def counts(self) -> tuple[int, int]:
        with sqlite3.connect(self.database) as connection:
            return (
                connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            )

    def events_of(self, skill_id: str) -> list[SkillEventKind]:
        return [e.kind for e in self.store.history(skill_id, None).events]

    def explicit(self, core, ref: str = NAME, session_id: str | None = None, **kwargs):
        """run_skill, then the answer to its missing-inputs question."""
        asked = core.run_skill(ref, session_id=session_id, **kwargs)
        self.assertIs(asked.status, RunStatus.AWAITING_CLARIFICATION, asked.error)
        self.assertEqual(
            asked.task.preferences["clarification_question"],
            f'"{NAME}" becerisi için şu bilgiler gerekli: path',
        )
        return core.provide_clarification(asked.session.id, asked.task.id, ANSWER)

    def assertRefused(self, run, reason: str) -> None:
        self.assertIsNone(run.task)
        self.assertIsNotNone(run.error)
        self.assertTrue(run.error.message.startswith(reason), run.error.message)


class ExplicitMatchesImplicitTests(_Explicit):
    """The same Core decision as an implicit FIT, in every mode."""

    def run_case(self, mode: PermissionMode, situation: str, explicit: bool):
        self.fresh()
        risk = RiskLevel.LOW if situation == "smart_auto_auto" else RiskLevel.MEDIUM
        session_id = None
        if situation == "matching_grant":
            prime = self.core(DummyConnector(risk=risk), PermissionMode.SESSION).run(user_task())
            done = self.core(DummyConnector(risk=risk), PermissionMode.SESSION).resume(
                prime.session.id,
                prime.permission_request.task_id,
                PermissionGrant.for_request(prime.permission_request, remember_for_session=True),
            )
            self.assertIs(done.status, RunStatus.COMPLETED, done.error)
            session_id = prime.session.id
        self.save()
        grants_before = self.grant_rows()
        connector = DummyConnector(risk=risk)
        core = self.core(connector, mode)
        if explicit:
            run = self.explicit(core, session_id=session_id)
        else:
            run = core.run(user_task(), session_id=session_id)
        self.assertTrue(run.session.objective.startswith("skill-run:"), "skill not used")
        anchor_id = run.session.objective.removeprefix("skill-run:")
        owner = self.store.task_session_id(anchor_id)
        if session_id is not None:
            self.assertEqual(owner, session_id)
        self.assertNotEqual(run.session.id, owner)  # a fresh chain session
        announced = self.announced(owner)
        self.assertEqual(announced["message"], f'Bu iş için "{NAME}" becerisini kullanıyorum')
        self.assertEqual(announced.get("explicit") is True, explicit)
        # Naming a skill is never an approval: no grant row is minted.
        self.assertEqual(self.grant_rows(), grants_before)
        step = None
        if run.permission_request is not None:
            step = self.store.load_task(run.permission_request.task_id)
            step = (step.objective, step.scope.target, step.scope.action)
        return decision(run, connector), step

    def test_explicit_decision_equals_implicit_in_every_mode(self) -> None:
        for mode in MODES:
            for situation in SITUATIONS:
                with self.subTest(mode=mode.value, situation=situation):
                    implicit = self.run_case(mode, situation, explicit=False)
                    explicit = self.run_case(mode, situation, explicit=True)
                    self.assertEqual(explicit, implicit)

    def test_same_missing_inputs_question_as_implicit(self) -> None:
        self.save()
        implicit = self.core(DummyConnector(), PermissionMode.STRICT).run(replace(user_task(), objective=NAME))
        self.assertIs(implicit.status, RunStatus.AWAITING_CLARIFICATION, implicit.error)
        explicit = self.core(DummyConnector(), PermissionMode.STRICT).run_skill(NAME)
        self.assertIs(explicit.status, RunStatus.AWAITING_CLARIFICATION, explicit.error)
        self.assertEqual(
            explicit.task.preferences["clarification_question"],
            implicit.task.preferences["clarification_question"],
        )

    def test_the_command_text_is_never_an_input_value(self) -> None:
        # Even a path inside the command message is not taken: ask first.
        self.save()
        connector = DummyConnector()
        run = self.core(connector, PermissionMode.SMART_AUTO).run_skill(
            NAME, text=f"/beceri {NAME} notes/evil.md"
        )
        self.assertIs(run.status, RunStatus.AWAITING_CLARIFICATION, run.error)
        self.assertEqual(connector.execute_calls, 0)


class ExplicitHealthTests(_Explicit):
    def test_explicit_success_and_failure_count_like_implicit(self) -> None:
        for outcome, kind in (
            (OutcomeStatus.SUCCESS, SkillEventKind.USED_SUCCESS),
            (OutcomeStatus.ERROR, SkillEventKind.USED_FAILURE),
        ):
            with self.subTest(outcome=outcome.value):
                self.fresh()
                skill_id = self.save()
                connector = DummyConnector(outcome=outcome)
                core = self.core(connector, PermissionMode.SMART_AUTO)
                run = self.explicit(core)
                core.run_chain(run.session.id)
                self.assertEqual(connector.execute_calls, 1)
                self.assertEqual(
                    [k for k in self.events_of(skill_id) if k in USED], [kind]
                )
                self.assertEqual(health(self.store.history(skill_id, None)).use_count, 1)

    def test_decline_at_the_announcement_is_cancelled_not_failure(self) -> None:
        skill_id = self.save()
        connector = DummyConnector(always_requires_approval=True)
        core = self.core(connector, PermissionMode.STRICT)
        run = self.explicit(core)
        self.assertIs(run.status, RunStatus.AWAITING_APPROVAL, run.error)
        anchor_id = run.session.objective.removeprefix("skill-run:")
        owner = self.store.task_session_id(anchor_id)
        declined = core.decline_skill(owner, anchor_id)
        self.assertIs(declined.status, RunStatus.CANCELLED, declined.error)
        self.assertIs(self.store.load_task(anchor_id).status, TaskStatus.CANCELLED)
        self.assertIs(
            self.store.load_task(run.permission_request.task_id).status, TaskStatus.CANCELLED
        )
        self.assertIs(self.store.load_session(run.session.id).chain.status, ChainStatus.CANCELLED)
        core.run_chain(run.session.id)
        self.assertEqual(connector.execute_calls, 0)
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))

    def test_decline_at_the_question_is_cancelled_and_runs_nothing(self) -> None:
        skill_id = self.save()
        connector = DummyConnector()
        core = self.core(connector, PermissionMode.SMART_AUTO)
        asked = core.run_skill(NAME)
        declined = core.decline_skill(asked.session.id, asked.task.id)
        self.assertIs(declined.status, RunStatus.CANCELLED, declined.error)
        self.assertIs(self.store.load_task(asked.task.id).status, TaskStatus.CANCELLED)
        self.assertEqual(connector.execute_calls, 0)
        self.assertFalse(set(self.events_of(skill_id)) & set(USED))


class ExplicitResolutionTests(_Explicit):
    def asks(self, ref: str, **kwargs) -> None:
        run = self.core(DummyConnector(), PermissionMode.STRICT).run_skill(ref, **kwargs)
        self.assertIs(run.status, RunStatus.AWAITING_CLARIFICATION, run.error)

    def refused(self, ref: str, reason: str, *, enabled: bool = True, **kwargs):
        before = self.counts()
        connector = DummyConnector()
        core = self.core(connector, PermissionMode.SMART_AUTO)
        if not enabled:
            core.settings = replace(core.settings, skills_enabled=False)
        run = core.run_skill(ref, **kwargs)
        self.assertRefused(run, reason)
        self.assertEqual(self.counts(), before)  # no session, no task
        self.assertEqual(connector.execute_calls, 0)
        return run

    def test_turkish_names_fold_diacritics_case_and_ascii(self) -> None:
        skill_id = self.save("Haftalık Raporu İşle")
        for ref in (
            "Haftalık Raporu İşle",
            "haftalık raporu işle",
            "HAFTALIK RAPORU İŞLE",
            "haftalik raporu isle",
            "HAFTALIK RAPORU ISLE",
            "  Haftalık   Raporu  İşle ",
            skill_id,
        ):
            with self.subTest(ref=ref):
                self.asks(ref)

    def test_english_name(self) -> None:
        self.save("Read the weekly report")
        self.asks("read the WEEKLY report")

    def test_unknown_name_lists_close_names_and_runs_nothing(self) -> None:
        self.save()
        run = self.refused("Haftalik rapor", SKILL_REFUSED_UNKNOWN)
        self.assertIn(f"Yakın adlar: {NAME}", run.error.message)
        run = self.refused("Haftalik rapor dosyasini okuu", SKILL_REFUSED_UNKNOWN)
        self.assertIn(NAME, run.error.message)
        run = self.refused("zzz qqq", SKILL_REFUSED_UNKNOWN)
        self.assertEqual(run.error.message, SKILL_REFUSED_UNKNOWN)
        self.refused("", SKILL_REFUSED_UNKNOWN)

    def test_ambiguous_name_lists_candidates_and_runs_nothing(self) -> None:
        self.save("Haftalık rapor", 1)
        self.save("HAFTALIK RAPOR", 2)
        run = self.refused("haftalik rapor", SKILL_REFUSED_AMBIGUOUS)
        self.assertIn("Adaylar: ", run.error.message)
        self.assertIn("Haftalık rapor", run.error.message)
        self.assertIn("HAFTALIK RAPOR", run.error.message)

    def test_out_of_scope_is_refused(self) -> None:
        alpha = self.store.create_project("Alpha").id
        beta = self.store.create_project("Beta").id
        self.save(project_id=alpha)
        self.refused(NAME, SKILL_REFUSED_SCOPE, project_id=beta)
        self.refused(NAME, SKILL_REFUSED_SCOPE)
        self.asks(NAME, project_id=alpha)

    def test_stale_and_disabled_are_refused(self) -> None:
        for kind, reason in (
            (SkillEventKind.STALE_MARKED, SKILL_REFUSED_STALE),
            (SkillEventKind.DISABLED, SKILL_REFUSED_DISABLED),
        ):
            with self.subTest(kind=kind.value):
                self.fresh()
                skill_id = self.save()
                self.store.append_event(
                    SkillEvent(skill_id=skill_id, kind=kind, version=1, at=AT), actor="user"
                )
                self.refused(NAME, reason)
                self.refused(skill_id, reason)

    def test_flag_off_is_refused_even_for_an_active_skill(self) -> None:
        skill_id = self.save()
        self.refused(NAME, SKILL_REFUSED_OFF, enabled=False)
        self.refused(skill_id, SKILL_REFUSED_OFF, enabled=False)

    def test_inside_a_skill_run_or_unknown_session_is_refused(self) -> None:
        self.save()
        run = self.explicit(
            self.core(DummyConnector(always_requires_approval=True), PermissionMode.STRICT)
        )
        self.refused(NAME, SKILL_REFUSED_SESSION, session_id=run.session.id)
        unknown = self.core(DummyConnector(), PermissionMode.STRICT).run_skill(
            NAME, session_id="0" * 32
        )
        self.assertIsNone(unknown.task)
        self.assertIs(unknown.error.code, ErrorCode.UNKNOWN_SESSION)


class ExplicitUnapprovedTests(_Explicit):
    """A version without its own draft_approved event is never run by name."""

    raw_version = UnapprovedVersionTests.raw_version
    unapproved_next = UnapprovedVersionTests.unapproved_next

    def test_unapproved_only_row_is_refused_by_id(self) -> None:
        approved_id = self.save()
        skill = replace(self.store.history(approved_id, None).get(1), skill_id="sk-" + "b" * 32)
        self.fresh()
        self.raw_version(skill)
        before = self.counts()
        run = self.core(DummyConnector(), PermissionMode.SMART_AUTO).run_skill(skill.skill_id)
        self.assertRefused(run, SKILL_REFUSED_UNAPPROVED)
        by_name = self.core(DummyConnector(), PermissionMode.SMART_AUTO).run_skill(NAME)
        self.assertIsNone(by_name.task)
        self.assertEqual(self.counts(), before)

    def test_unapproved_newest_row_runs_the_approved_version(self) -> None:
        skill_id = self.save()
        self.raw_version(self.unapproved_next(skill_id))
        run = self.explicit(self.core(DummyConnector(), PermissionMode.SMART_AUTO))
        owner = self.store.task_session_id(run.session.objective.removeprefix("skill-run:"))
        self.assertEqual(self.announced(owner)["version"], 1)


class ExplicitAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self, skills_enabled=True)
        self.app = open_app(destination_policy=(), settings=self.settings)
        # The composition does not hand Core a skill store yet; wire it here.
        self.app.core.skill_store = self.app.skill_store

    def test_whole_message_is_a_command_and_longer_text_is_not(self) -> None:
        with mock.patch.object(self.app.core, "run_skill", wraps=self.app.core.run_skill) as run:
            for text, name in (
                ("/beceri Haftalık rapor", "Haftalık rapor"),
                ("  /BECERI   Haftalık rapor  ", "Haftalık rapor"),
                ("/skill weekly report", "weekly report"),
            ):
                with self.subTest(text=text):
                    run.reset_mock()
                    result = self.app.submit_text(text)
                    run.assert_called_once()
                    self.assertEqual(run.call_args.args[0].strip(), name)
                    self.assertTrue(
                        result.payload["intent"]["message"].startswith(SKILL_REFUSED_UNKNOWN),
                        result.payload,
                    )
            for text in (
                "lütfen /beceri Haftalık rapor",
                "/beceri Haftalık rapor" + chr(10) + "ve sonra sil",
                "/beceri",
                "/becerix Haftalık rapor",
            ):
                with self.subTest(text=text):
                    run.reset_mock()
                    self.app.submit_text(text)
                    run.assert_not_called()

    def test_flag_off_refuses_the_command_and_the_api(self) -> None:
        app = open_app(
            destination_policy=(),
            settings=dataclasses.replace(self.settings, skills_enabled=False),
        )
        with mock.patch.object(app.core, "run_skill") as run:
            result = app.submit_text("/beceri Haftalık rapor")
            self.assertEqual(result.payload["intent"]["message"], SKILL_REFUSED_OFF)
            api = app.run_skill("sk-" + "a" * 32)
            self.assertEqual(api.payload["error"]["message"], SKILL_REFUSED_OFF)
            run.assert_not_called()

    def test_api_unknown_id_is_refused_and_bad_project_is_refused(self) -> None:
        result = self.app.run_skill("sk-" + "a" * 32)
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(result.payload["error"]["message"].startswith(SKILL_REFUSED_UNKNOWN))
        bad = self.app.run_skill("sk-" + "a" * 32, project_id="../x")
        self.assertNotEqual(bad.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
