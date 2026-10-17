"""V2.2 SKGATE: the end-to-end skill gate, through the production factory.

Every app here is composed by ``create_app`` (``tests.helpers.open_app``), the
path that wires the skill store into Core; nothing attaches it by hand. Fakes
only (``DummyConnector``, an isolated temp database, no model, connector or
network call); ``data/jarvis.db`` is only listed (size, mtime), never opened.

Pins: flag off -> nothing (S4); teach = run -> offer -> the user's approval
saves a project skill (S5, D8, a4); a similar task is announced and every
mandatory step still asks approval in strict/session/smart_auto (S1, S2);
decline and denial are no use (D6, D16); adapt = new skill with parent
provenance (S6); 3 failures -> stale, never auto-used (S12); a corrected run's
version draft -> v2, rollback -> v1 (S6); delete hides it from list and match
(D4); a secret in a draft is refused (S7); another project never sees it (S8,
D15); cancel and skills-off stop a delegated run (SKCOREFIX); a run with an
external-read step leaves no lesson (a2); no UPDATE/DELETE on skill tables (S9).
"""

from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis import intent
from jarvis.contracts import (
    ActionScope,
    ChainStatus,
    DataClassification,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    RunStatus,
    Task,
    TaskStatus,
    utc_now,
)
from jarvis.destinations import CLAUDE_CODE_DESTINATION, DestinationGrant
from jarvis.memory import SCHEMA_VERSION
from jarvis.settings import PROJECT_ROOT, ConfigStore
from jarvis.skill_lifecycle import active_skill, adapt
from jarvis.skill_store import SkillStore
from jarvis.skill_types import SkillEventKind, SkillOrigin
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, isolated_settings, open_app, safe_cleanup

LIVE_DB = PROJECT_ROOT / "data" / "jarvis.db"
TEACH = "Haftalik rapor dosyasini oku: notes/week.md"
AGAIN = "Haftalik rapor dosyasini oku: notes/next.md"
OFFER = "Bunu beceri olarak kaydedeyim mi?"
SECRET = "ghp_" + "A1b2C3d4E5" * 4
POLICY = frozenset({DestinationGrant("dummy", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION)})
SKILL_EVENTS = {"skill_announced", "skill_stale_announced", "skill_declined", "skill_needs_inputs",
                "skill_run_finished", "skill_use_requested", "skill_draft_offered",
                "skill_experience_captured"}
USED = {SkillEventKind.USED_SUCCESS, SkillEventKind.USED_FAILURE}
SKILL_TABLE_WRITE = re.compile(
    r"\b(?:UPDATE|DELETE\s+FROM|REPLACE\s+INTO|INSERT\s+OR\s+REPLACE\s+INTO)\s+[\"'`]?skill", re.I
)


def live_db_listing() -> tuple[int, int] | None:
    """Size and mtime of the operator's database from a directory listing."""
    try:
        listed = LIVE_DB.stat()
    except FileNotFoundError:
        return None
    return (listed.st_size, listed.st_mtime_ns)


class SkillGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.live = live_db_listing()
        patcher = mock.patch.dict(
            intent.NL_CAPABILITIES, {DUMMY_CAPABILITY: intent.CapabilityHint(None, "read", "echo")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = isolated_settings(
            self, permission_mode=PermissionMode.SMART_AUTO, skills_enabled=True
        )
        self.connector = DummyConnector(always_requires_approval=True)
        self.app = self.compose()
        self.alpha = self.app.memory.create_project("Alpha").id
        self.beta = self.app.memory.create_project("Beta").id

    def tearDown(self) -> None:
        self.assertEqual(live_db_listing(), self.live, "data/jarvis.db changed")

    # -- fixtures -------------------------------------------------------------

    def compose(self, connector: DummyConnector | None = None, **changes):
        """The production factory over this test's database."""
        app = open_app(
            destination_policy=POLICY,
            settings=replace(self.settings, **changes),
            connectors=(connector or self.connector,),
        )
        self.assertIsInstance(app.core.skill_store, SkillStore)  # wired, not attached by hand
        self.assertIs(app.skill_store, app.core.skill_store)
        return app

    @property
    def store(self) -> SkillStore:
        return self.app.skill_store

    def task(self, objective: str, project_id: str | None) -> Task:
        return Task(
            objective=objective,
            capability=DUMMY_CAPABILITY,
            scope=ActionScope(
                action="read", target="resource-a", data_classification=DataClassification.INTERNAL
            ),
            project_id=project_id,
        )

    def feed(self, session_id: str) -> set[str]:
        return {e["event_type"] for e in self.store.list_events(session_id)}

    def payloads(self, session_id: str, kind: str) -> list[dict]:
        return [e["payload"] for e in self.store.list_events(session_id) if e["event_type"] == kind]

    def kinds(self, skill_id: str) -> list[SkillEventKind]:
        return [e.kind for e in self.store.history(skill_id, self.alpha).events]

    def listed(self, project_id: str | None) -> list[dict]:
        return self.app.skills_list(project_id)["skills"]

    @staticmethod
    def grant(app, run, approved: bool = True):
        request = run.permission_request
        return app.core.resume(
            run.session.id, request.task_id, PermissionGrant.for_request(request, approved=approved)
        )

    def offer(self, objective: str = TEACH):
        """A plain run, its step approved, ends with an offer and no skill."""
        run = self.app.core.run(self.task(objective, self.alpha), request=objective)
        self.assertEqual(run.status, RunStatus.AWAITING_APPROVAL, run.error)
        self.assertEqual(self.grant(self.app, run).status, RunStatus.COMPLETED)
        [offered] = self.payloads(run.session.id, "skill_draft_offered")
        self.assertEqual(offered["message"], OFFER)
        self.assertEqual(self.listed(self.alpha), [])  # S5: nothing saved yet
        [card] = self.app.drafts_pending(self.alpha)["drafts"]
        self.assertEqual(offered["draft_ids"], [card["draft_id"]])
        return run, card

    def teach(self) -> str:
        run, card = self.offer()
        # a4: the card carries the whole record that will be saved, unclipped.
        [pending] = self.store.pending_drafts(self.alpha)
        self.assertEqual(card, pending.to_dict())
        self.assertEqual(card["scope"], {"kind": "project", "project_id": self.alpha})  # D8
        self.assertEqual(card["provenance"]["origin"], SkillOrigin.TAUGHT_FROM_RUN.value)
        self.assertEqual(card["provenance"]["source_run_id"], run.task.id)
        [step] = card["steps"]
        [given] = card["inputs"]
        self.assertIn("{" + given["name"] + "}", step["action"])
        self.assertEqual(step["capability"], DUMMY_CAPABILITY)
        saved = self.app.draft_approve(card["draft_id"], project_id=self.alpha)
        self.assertEqual((True, 1), (saved["ok"], saved["version"]), saved)
        [skill] = self.listed(self.alpha)
        self.assertEqual((skill["skill_id"], skill["status"]), (saved["skill_id"], "active"))
        self.assertEqual(skill["steps"], card["steps"])
        return saved["skill_id"]

    def use(self, app, objective: str = AGAIN, project_id: str | None = "alpha"):
        project_id = self.alpha if project_id == "alpha" else project_id
        run = app.core.run(self.task(objective, project_id), request=objective)
        self.assertTrue(run.session.objective.startswith("skill-run:"), run.error)
        anchor = run.session.objective.removeprefix("skill-run:")
        return run, anchor, self.store.task_session_id(anchor)

    def assertPlain(self, run) -> None:
        self.assertFalse(run.session.objective.startswith("skill-run:"))
        self.assertFalse(self.feed(run.session.id) & SKILL_EVENTS)
        self.assertNotIn("skill_run", run.task.preferences)

    # -- S4, teach, S1/S2 -------------------------------------------------------

    def test_flag_off_nothing_then_teach_and_use_through_the_normal_approval(self) -> None:
        off = self.compose(skills_enabled=False)
        run = off.core.run(self.task(TEACH, self.alpha), request=TEACH)
        self.assertEqual(self.grant(off, run).status, RunStatus.COMPLETED)
        self.assertPlain(run)
        self.assertEqual(self.store.pending_drafts(self.alpha), ())
        self.assertEqual(self.store.experiences(self.alpha), ())
        self.assertTrue(off.drafts_pending(self.alpha)["skills_disabled"])
        # Enable through the app's switch (the ConfigStore path).
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        config = ConfigStore(Path(temp.name) / "config.json")
        self.assertEqual(off.skills_set_enabled(True, config), {"ok": True, "skills_enabled": True})
        self.app = self.compose(skills_enabled=config.load().settings.skills_enabled)
        skill_id = self.teach()
        # S2: every mandatory step stops at approval, with no grant, in each mode.
        for mode in (PermissionMode.STRICT, PermissionMode.SESSION, PermissionMode.SMART_AUTO):
            with self.subTest(mode=mode):
                app = self.compose(permission_mode=mode)
                calls = self.connector.execute_calls
                run, anchor, owner = self.use(app)
                self.assertEqual(run.status, RunStatus.AWAITING_APPROVAL, run.error)
                self.assertTrue(run.permission_request.side_effect.mandatory)
                self.assertEqual(self.connector.execute_calls, calls)
                [said] = self.payloads(owner, "skill_announced")
                self.assertEqual(said["message"], f'Bu iş için "{said["name"]}" becerisini kullanıyorum')
                self.assertEqual(said["name"], self.listed(self.alpha)[0]["name"])
                self.assertEqual((said["skill_id"], said["origin"]), (skill_id, "taught_from_run"))
                if mode is not PermissionMode.SMART_AUTO:
                    # SKCOREFIX: cancelling the task stops the step and the chain; no use.
                    self.assertEqual(app.cancel(owner, anchor).payload["run_status"], "cancelled")
                    self.assertIs(self.store.load_session(run.session.id).chain.status, ChainStatus.CANCELLED)
                    self.assertEqual(self.connector.execute_calls, calls)
                    self.assertFalse(set(self.kinds(skill_id)) & USED)
                    continue
                self.assertEqual(self.grant(app, run).status, RunStatus.COMPLETED)
                app.core.run_chain(run.session.id)
                self.assertIs(self.store.load_task(anchor).status, TaskStatus.COMPLETED)
                self.assertEqual(self.connector.execute_calls, calls + 1)
                self.assertEqual(self.kinds(skill_id).count(SkillEventKind.USED_SUCCESS), 1)
                [skill] = self.listed(self.alpha)
                self.assertEqual(skill["health"]["success_count"], 1)

    def test_turning_skills_off_stops_a_delegated_run_and_new_matches(self) -> None:
        skill_id = self.teach()
        run, anchor, owner = self.use(self.app)
        # The app's switch turns skills off in this running app's Core at once.
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        off = self.app
        switched = off.skills_set_enabled(False, ConfigStore(Path(temp.name) / "config.json"))
        self.assertEqual(switched, {"ok": True, "skills_enabled": False})
        self.assertFalse(off.core.settings.skills_enabled)
        # The step in flight keeps its own approval: no grant, no execution.
        again, _ = off.core.run_chain(run.session.id)
        self.assertEqual(again.status, RunStatus.AWAITING_APPROVAL, again.error)
        calls = self.connector.execute_calls
        self.assertEqual(self.grant(off, run).status, RunStatus.COMPLETED)
        off.core.run_chain(run.session.id)
        self.assertEqual(self.connector.execute_calls, calls + 1)
        self.assertFalse(set(self.kinds(skill_id)) & USED)  # nothing recorded with the flag off
        self.assertPlain(off.core.run(self.task(AGAIN, self.alpha), request=AGAIN))

    # -- D6 / D16 ---------------------------------------------------------------

    def test_declines_and_denials_are_no_use_and_teach_nothing(self) -> None:
        skill_id = self.teach()
        before = self.listed(self.alpha)[0]["health"]
        for _ in range(3):
            run, anchor, owner = self.use(self.app)
            declined = self.app.skill_decline(owner, anchor)
            self.assertEqual(declined.payload["run_status"], "awaiting_approval", declined.payload)
            self.assertIn("skill_declined", self.feed(owner))
            self.assertIs(self.store.load_session(run.session.id).chain.status, ChainStatus.CANCELLED)
            # The user's own task runs as usual and is not offered as a skill.
            own = self.app.core.resume(owner, anchor)
            self.assertEqual(self.grant(self.app, own).status, RunStatus.COMPLETED)
            self.assertEqual(self.store.pending_drafts(self.alpha), ())
        run, _, _ = self.use(self.app)
        self.assertEqual(self.grant(self.app, run, approved=False).status, RunStatus.REJECTED)
        self.app.core.run_chain(run.session.id)
        [skill] = self.listed(self.alpha)
        self.assertEqual((skill["status"], skill["health"]), ("active", before))
        self.assertFalse(set(self.kinds(skill_id)) & USED)

    # -- S6 -----------------------------------------------------------------------

    def test_adapt_makes_a_new_skill_with_parent_provenance(self) -> None:
        skill_id = self.teach()
        history = self.store.history(skill_id, self.alpha)
        original = active_skill(history).to_dict()
        child = adapt(
            active_skill(history), {"name": "Aylik rapor dosyasini oku"}, utc_now(), history=history
        )
        self.store.add_version(child, actor="user")
        self.assertNotEqual(child.skill_id, skill_id)
        self.assertIs(child.provenance.origin, SkillOrigin.ADAPTED)
        self.assertEqual((child.provenance.parent_skill_id, child.provenance.parent_version), (skill_id, 1))
        after = self.store.history(skill_id, self.alpha)
        self.assertEqual([v.to_dict() for v in after.versions], [original])
        self.assertEqual({s["skill_id"] for s in self.listed(self.alpha)}, {skill_id, child.skill_id})

    def test_a_corrected_run_proposes_v2_and_rollback_returns_to_v1(self) -> None:
        skill_id = self.teach()
        run, anchor, owner = self.use(self.app)
        self.grant(self.app, run)
        self.app.core.run_chain(run.session.id)
        learned = self.app.core.correct_run(owner, anchor, "Once yedek dosyayi kontrol et")
        # a2: a run with any step not known to read no external content keeps no lesson.
        self.assertEqual((learned["experience_ids"], learned["experience_skipped"]), ([], "external_content"))
        self.assertEqual(self.store.experiences(self.alpha), ())
        [card] = self.app.drafts_pending(self.alpha)["drafts"]
        self.assertEqual(learned["draft_ids"], [card["draft_id"]])
        self.assertEqual(card["target_skill_id"], skill_id)
        self.assertEqual(card["provenance"]["origin"], SkillOrigin.VERSION_AFTER_FAILURE.value)
        self.assertEqual(card["provenance"]["parent_version"], 1)
        self.assertEqual(self.store.history(skill_id, self.alpha).latest_version, 1)  # S5
        saved = self.app.draft_approve(card["draft_id"], project_id=self.alpha)
        self.assertEqual((saved["skill_id"], saved["version"]), (skill_id, 2), saved)
        got = self.app.skill_get(skill_id, project_id=self.alpha)["skill"]
        self.assertEqual((got["active_version"], got["versions"]), (2, [1, 2]))
        self.assertTrue(self.app.skill_rollback(skill_id, 1, project_id=self.alpha)["ok"])
        got = self.app.skill_get(skill_id, project_id=self.alpha)["skill"]
        self.assertEqual((got["active_version"], got["versions"]), (1, [1, 2]))

    # -- S12 ----------------------------------------------------------------------

    def test_three_failures_make_the_skill_stale_and_it_is_not_used(self) -> None:
        skill_id = self.teach()
        failing = DummyConnector(outcome=OutcomeStatus.ERROR)
        app = self.compose(connector=failing)
        for _ in range(3):
            run, anchor, _ = self.use(app)
            self.assertIs(self.store.load_session(run.session.id).chain.status, ChainStatus.FAILED)
        self.assertEqual(self.kinds(skill_id).count(SkillEventKind.USED_FAILURE), 3)
        self.assertEqual(self.listed(self.alpha)[0]["status"], "stale")
        run = app.core.run(self.task(AGAIN, self.alpha), request=AGAIN)
        self.assertEqual(run.status, RunStatus.AWAITING_CLARIFICATION, run.error)
        self.assertIn("skill_stale_announced", self.feed(run.session.id))
        self.assertNotIn("skill_announced", self.feed(run.session.id))
        self.assertEqual(failing.execute_calls, 3)

    # -- D4 -----------------------------------------------------------------------

    def test_delete_removes_the_skill_from_list_and_match(self) -> None:
        skill_id = self.teach()
        self.assertTrue(self.app.skill_delete(skill_id, project_id=self.alpha)["ok"])
        self.assertEqual(self.listed(self.alpha), [])
        run = self.app.core.run(self.task(AGAIN, self.alpha), request=AGAIN)
        self.assertEqual(run.status, RunStatus.AWAITING_APPROVAL, run.error)
        self.assertPlain(run)

    # -- S7 -----------------------------------------------------------------------

    def test_a_secret_in_a_draft_is_refused(self) -> None:
        _, card = self.offer()
        step = dict(card["steps"][0], action=card["steps"][0]["action"] + " token " + SECRET)
        refused = self.app.draft_approve(
            card["draft_id"], edits=dict(card, steps=[step]), project_id=self.alpha
        )
        self.assertFalse(refused["ok"])
        self.assertIn("skill filter", refused["error"])
        self.assertNotIn(SECRET, refused["error"])
        self.assertEqual(self.listed(self.alpha), [])
        # A run that carried a secret is never offered at all.
        run = self.app.core.run(
            self.task(AGAIN + " password=Hunter2-Secret-Value", self.beta),
            request=AGAIN + " password=Hunter2-Secret-Value",
        )
        self.assertEqual(self.grant(self.app, run).status, RunStatus.COMPLETED)
        self.assertEqual(self.payloads(run.session.id, "skill_draft_offered"), [])
        self.assertEqual(self.store.pending_drafts(self.beta), ())

    # -- S8 / D15 -----------------------------------------------------------------

    def test_another_project_never_sees_the_skill(self) -> None:
        _, card = self.offer()
        self.assertEqual(self.app.drafts_pending(self.beta)["drafts"], [])
        self.assertFalse(self.app.draft_approve(card["draft_id"], project_id=self.beta)["ok"])
        skill_id = self.app.draft_approve(card["draft_id"], project_id=self.alpha)["skill_id"]
        for project_id in (self.beta, None):
            with self.subTest(project=project_id):
                self.assertEqual(self.listed(project_id), [])
                self.assertFalse(self.app.skill_get(skill_id, project_id=project_id)["ok"])
                self.assertFalse(self.app.skill_delete(skill_id, project_id=project_id)["ok"])
                run = self.app.core.run(self.task(AGAIN, project_id), request=AGAIN)
                self.assertPlain(run)
                self.assertEqual(self.store.experiences(project_id), ())
        self.assertEqual(self.listed(self.alpha)[0]["status"], "active")

    # -- S9 -----------------------------------------------------------------------

    def test_no_update_or_delete_on_skill_tables(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 10)
        sources = sorted((PROJECT_ROOT / "jarvis").rglob("*.py"))
        self.assertTrue(sources)
        hits = [
            f"{path.name}: {match.group(0)}"
            for path in sources
            for match in SKILL_TABLE_WRITE.finditer(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
