"""V2.2 S3 SKAPPAPI: the skill app API over SkillStore.

Every test opens its own TEMP database (isolated_settings); none opens
data\\jarvis.db. No model, connector or network call.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.app import config_show_result
from jarvis.contracts import AgentRun, RunStatus
from jarvis.settings import ConfigStore
from jarvis.skill_match import MatchKind, match_skills
from jarvis.skill_types import SkillScope, SkillScopeKind
from tests.helpers import isolated_settings, make_task, open_app, safe_cleanup
from tests.test_skill_store import SCOPE_A, TK, draft

TASK = "Haftalik rapor dosyasini oku"


class SkillAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self, skills_enabled=True)
        self.app = open_app(destination_policy=(), settings=self.settings)
        self.store = self.app.skill_store

    def app_with(self, **fields):
        return open_app(destination_policy=(), settings=dataclasses.replace(self.settings, **fields))

    def approved(self, number: int = 1, scope: SkillScope | None = None, project_id: str | None = None) -> str:
        self.store.add_draft(draft(number) if scope is None else draft(number, scope))
        result = self.app.draft_approve(f"draft-{number:032x}", project_id=project_id)
        self.assertTrue(result["ok"], result)
        return result["skill_id"]

    def actors(self) -> set[str]:
        with sqlite3.connect(self.settings.memory_database) as connection:
            rows = connection.execute("SELECT actor FROM skill_events").fetchall()
            rows += connection.execute("SELECT actor FROM skill_suppressions").fetchall()
        return {row[0] for row in rows}

    def test_approve_lists_the_skill_and_a_second_approve_is_refused(self) -> None:
        self.store.add_draft(draft(1))
        pending = self.app.drafts_pending()
        self.assertEqual(["draft-" + f"{1:032x}"], [item["draft_id"] for item in pending["drafts"]])
        first = self.app.draft_approve(f"draft-{1:032x}")
        self.assertEqual((True, 1), (first["ok"], first["version"]))
        second = self.app.draft_approve(f"draft-{1:032x}")
        self.assertFalse(second["ok"])
        self.assertIn("not pending", second["error"])
        self.assertEqual([], self.app.drafts_pending()["drafts"])
        listed = self.app.skills_list()["skills"]
        self.assertEqual([(first["skill_id"], 1, "active")], [(s["skill_id"], s["version"], s["status"]) for s in listed])
        got = self.app.skill_get(first["skill_id"])["skill"]
        self.assertEqual((1, [1], TASK), (got["active_version"], got["versions"], got["name"]))
        self.assertEqual({"user"}, self.actors())

    def test_a_rejected_draft_cannot_be_approved(self) -> None:
        self.store.add_draft(draft(1))
        self.assertTrue(self.app.draft_reject(f"draft-{1:032x}")["ok"])
        approve = self.app.draft_approve(f"draft-{1:032x}")
        self.assertFalse(approve["ok"])
        self.assertEqual([], self.app.skills_list()["skills"])
        self.assertFalse(self.app.draft_reject(f"draft-{1:032x}")["ok"])
        self.assertFalse(self.app.draft_approve(123)["ok"])
        self.assertFalse(self.app.draft_approve(f"draft-{2:032x}", scope="proj-b")["ok"])

    def test_edit_saves_the_next_version_and_refuses_secrets_authority_and_stale_forms(self) -> None:
        skill_id = self.approved()
        form = self.app.skill_get(skill_id)["skill"]["edit_draft"]
        edited = dict(form, summary="Haftalik rapor dosyasini oku ve ozetle")
        saved = self.app.skill_edit(skill_id, edited)
        self.assertEqual((True, 2), (saved["ok"], saved["version"]))
        got = self.app.skill_get(skill_id)["skill"]
        self.assertEqual((2, [1, 2], "user_edit"), (got["active_version"], got["versions"], got["provenance"]["origin"]))
        # The form of version 1 names the old latest version: refused, not overwritten.
        self.assertFalse(self.app.skill_edit(skill_id, dict(form, summary="Eski form"))["ok"])
        fresh = self.app.skill_get(skill_id)["skill"]["edit_draft"]
        token = "ghp_" + "A1b2C3d4E5" * 4
        secret = self.app.skill_edit(skill_id, dict(fresh, summary=f"Rapor icin token {token} kullan"))
        self.assertFalse(secret["ok"])
        self.assertIn("skill filter", secret["error"])
        self.assertNotIn(token, secret["error"])
        authority = self.app.skill_edit(skill_id, dict(fresh, approved=True))
        self.assertFalse(authority["ok"])
        self.assertFalse(self.app.skill_edit(skill_id, dict(fresh))["ok"])  # a no-op edit saves nothing
        self.assertEqual([1, 2], self.app.skill_get(skill_id)["skill"]["versions"])
        self.assertEqual({"user"}, self.actors())

    def test_delete_hides_the_skill_from_list_get_and_match(self) -> None:
        skill_id = self.approved()
        self.assertIsNotNone(match_skills(TASK, self.store.list_skills(None), None).best)
        deleted = self.app.skill_delete(skill_id)
        self.assertEqual((True, "deleted"), (deleted["ok"], deleted["event"]))
        self.assertEqual([], self.app.skills_list()["skills"])
        self.assertFalse(self.app.skill_get(skill_id)["ok"])
        match = match_skills(TASK, self.store.list_skills(None), None)
        self.assertEqual((MatchKind.NONE, None), (match.kind, match.best))
        for again in (self.app.skill_delete(skill_id), self.app.skill_set_enabled(skill_id, True),
                      self.app.skill_rollback(skill_id, 1)):
            self.assertFalse(again["ok"])

    def test_rollback_moves_the_active_version_and_keeps_every_version(self) -> None:
        skill_id = self.approved()
        form = self.app.skill_get(skill_id)["skill"]["edit_draft"]
        self.assertTrue(self.app.skill_edit(skill_id, dict(form, summary="Ikinci surum"))["ok"])
        rolled = self.app.skill_rollback(skill_id, 1)
        self.assertEqual((True, "rolled_back", 1), (rolled["ok"], rolled["event"], rolled["version"]))
        got = self.app.skill_get(skill_id)["skill"]
        self.assertEqual((1, [1, 2], TASK), (got["active_version"], got["versions"], got["summary"]))
        self.assertEqual("Ikinci surum", self.app.skill_get(skill_id, version=2)["skill"]["summary"])
        self.assertFalse(self.app.skill_rollback(skill_id, 1)["ok"])  # already active
        self.assertFalse(self.app.skill_rollback(skill_id, 9)["ok"])
        self.assertFalse(self.app.skill_get(skill_id, version=True)["ok"])

    def test_disable_and_enable(self) -> None:
        skill_id = self.approved()
        self.assertTrue(self.app.skill_set_enabled(skill_id, False)["ok"])
        self.assertEqual("disabled", self.app.skills_list()["skills"][0]["status"])
        self.assertTrue(self.app.skill_set_enabled(skill_id, True)["ok"])
        self.assertEqual("active", self.app.skills_list()["skills"][0]["status"])
        self.assertFalse(self.app.skill_set_enabled(skill_id, "yes")["ok"])
        self.assertFalse(self.app.skill_set_enabled("sk-unknown", False)["ok"])

    def test_flag_off_allows_only_clean_up(self) -> None:
        skill_id = self.approved()
        self.store.add_draft(draft(2))
        off = self.app_with(skills_enabled=False)
        refused = (
            off.drafts_pending(),
            off.draft_approve(f"draft-{2:032x}"),
            off.draft_reject(f"draft-{2:032x}"),
            off.skill_edit(skill_id, self.app.skill_get(skill_id)["skill"]["edit_draft"]),
            off.skill_rollback(skill_id, 1),
            off.skill_set_enabled(skill_id, True),
            off.suppress(TK),
            off.unsuppress(TK),
        )
        for result in refused:
            self.assertEqual({"ok": False, "error": "skills disabled", "skills_disabled": True}, result)
        self.assertEqual(1, len(off.skills_list()["skills"]))
        self.assertTrue(off.skill_get(skill_id)["ok"])
        self.assertTrue(off.skill_set_enabled(skill_id, False)["ok"])
        self.assertTrue(off.skill_delete(skill_id)["ok"])
        self.assertEqual([], off.skills_list()["skills"])
        self.assertEqual(1, len(self.app.drafts_pending()["drafts"]))  # the draft is untouched
        use = off.skill_use("no-session", "no-task")
        self.assertEqual("skills disabled", use.payload["error"]["message"])

    def test_project_scope_never_leaks(self) -> None:
        self.store.add_draft(draft(1, SCOPE_A))
        self.assertEqual([], self.app.drafts_pending()["drafts"])
        self.assertEqual([], self.app.drafts_pending("proj-b")["drafts"])
        self.assertFalse(self.app.draft_approve(f"draft-{1:032x}", project_id="proj-b")["ok"])
        self.assertFalse(self.app.draft_approve(f"draft-{1:032x}")["ok"])
        skill_id = self.app.draft_approve(f"draft-{1:032x}", project_id="proj-a")["skill_id"]
        self.assertEqual(1, len(self.app.skills_list("proj-a")["skills"]))
        for project_id in (None, "proj-b"):
            self.assertEqual([], self.app.skills_list(project_id)["skills"])
            self.assertFalse(self.app.skill_get(skill_id, project_id=project_id)["ok"])
            self.assertFalse(self.app.skill_delete(skill_id, project_id=project_id)["ok"])
            self.assertFalse(self.app.skill_set_enabled(skill_id, False, project_id=project_id)["ok"])
        self.assertFalse(self.app.skills_list("Bad Project")["ok"])

    def test_a_project_draft_may_be_approved_as_global(self) -> None:
        self.store.add_draft(draft(1, SCOPE_A))
        result = self.app.draft_approve(f"draft-{1:032x}", scope="global", project_id="proj-a")
        self.assertTrue(result["ok"], result)
        got = self.app.skill_get(result["skill_id"], project_id="proj-b")["skill"]
        self.assertEqual(SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None).to_dict(), got["scope"])

    def test_suppress_and_unsuppress(self) -> None:
        self.assertEqual({"ok": True, "key": TK, "suppressed": True}, self.app.suppress(TK))
        self.assertTrue(self.store.is_suppressed(TK))
        self.assertTrue(self.app.unsuppress(TK)["ok"])
        self.assertFalse(self.store.is_suppressed(TK))
        self.assertTrue(self.app.suppress(None, "global")["ok"])
        self.assertTrue(self.store.is_suppressed("global"))
        self.assertFalse(self.app.suppress("not-a-kind")["ok"])
        self.assertFalse(self.app.suppress(TK, "everything")["ok"])
        self.assertEqual({"user"}, self.actors())

    def test_skills_switch_goes_through_the_config_store(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        store = ConfigStore(Path(temp.name) / "config.json")
        self.assertEqual({"ok": True, "skills_enabled": True}, self.app.skills_set_enabled(True, store))
        self.assertIs(True, store.load().settings.skills_enabled)
        self.assertIs(True, config_show_result(store, store.load()).payload["skills_enabled"])
        self.assertEqual({"ok": True, "skills_enabled": False}, self.app.skills_set_enabled(False, store))
        self.assertIs(False, store.load().settings.skills_enabled)
        self.assertEqual("skills disabled", self.app.drafts_pending()["error"])  # off at once
        self.assertFalse(self.app.skills_set_enabled(1, store)["ok"])

    def test_decline_and_use_forward_to_core_for_an_owned_task(self) -> None:
        run = self.app.core.run(make_task())
        session_id, task_id = run.session.id, run.task.id
        calls = []

        class Core:
            def decline_skill(self, *ids):
                calls.append(("decline", ids))
                return AgentRun(status=RunStatus.CANCELLED, session=run.session, task=run.task)

            def use_skill(self, *ids):
                calls.append(("use", ids))
                return AgentRun(status=RunStatus.CANCELLED, session=run.session, task=run.task)

        if not hasattr(self.app.core, "decline_skill"):  # this base: Core has no skill hooks yet
            self.assertIsNotNone(self.app.skill_decline(session_id, task_id).payload["error"])
        self.app.core = Core()
        self.assertEqual("skill-decline", self.app.skill_decline(session_id, task_id).payload["command"])
        self.assertEqual("skill-use", self.app.skill_use(session_id, task_id).payload["command"])
        self.assertEqual([("decline", (session_id, task_id)), ("use", (session_id, task_id))], calls)
        self.assertIsNotNone(self.app.skill_decline("unknown", task_id).payload["error"])
        self.assertIsNotNone(self.app.skill_use(session_id, "unknown").payload["error"])
        off = self.app_with(skills_enabled=False)
        off.core = Core()
        off.skill_decline(session_id, task_id)  # stopping a skill is clean-up: works with the flag off
        self.assertEqual(("decline", (session_id, task_id)), calls[-1])
        self.assertEqual(3, len(calls))


if __name__ == "__main__":
    unittest.main()
