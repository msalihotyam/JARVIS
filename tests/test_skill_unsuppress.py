"""V2.2 b7 SKUNSUPPRESS: "never suggest again" is undone by an append-only off row.

Every test opens its own TEMP database (isolated_settings); none opens
data\\jarvis.db. No model, connector or network call.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import unittest
from unittest import mock

from jarvis import intent
from jarvis.contracts import PermissionMode, RunStatus
from jarvis.core import AgentCore
from jarvis.destinations import CLAUDE_CODE_DESTINATION, DestinationGrant
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from jarvis.skill_draft import GLOBAL_SUPPRESSION, task_kind
from jarvis.skill_types import RunOutcome, RunStep, RunSummary
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, isolated_settings, open_app
from tests.test_skill_learn import TASK_TEXT, user_task
from tests.test_skill_store import SCOPE_A, draft

OTHER = "tk-" + "b" * 32


class SkillUnsuppressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self, skills_enabled=True)
        self.app = open_app(destination_policy=(), settings=self.settings)
        self.store = self.app.skill_store
        patcher = mock.patch.dict(
            intent.NL_CAPABILITIES,
            {DUMMY_CAPABILITY: intent.CapabilityHint(None, "read", "echo")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def offered(self) -> str | None:
        """Run the repeatable task once; the task kind if it was offered."""
        connector = DummyConnector()
        registry = ToolRegistry()
        registry.register(connector)
        settings = Settings(
            memory_database=self.settings.memory_database,
            permission_mode=PermissionMode.SMART_AUTO,
            skills_enabled=True,
        )
        policy = frozenset({DestinationGrant(connector.manifest.name, DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION)})
        run = AgentCore(registry, self.store, settings, destination_policy=policy, skill_store=self.store).run(
            user_task()
        )
        self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
        offers = [e for e in self.store.list_events(run.session.id) if e["event_type"] == "skill_draft_offered"]
        return run.task.id if offers else None

    def kind(self) -> str:
        """The task kind of the repeatable run (as test_skill_learn probes it)."""
        step = RunStep(action=TASK_TEXT, capability=DUMMY_CAPABILITY, outcome=RunOutcome.COMPLETED, error=None)
        return task_kind(
            RunSummary(
                run_id="probe", task_text=TASK_TEXT, project_id=None, status=RunOutcome.COMPLETED,
                steps=(step,), user_correction=None, matched_skill=None,
            )
        )

    def rows(self) -> list[tuple[str, int]]:
        with sqlite3.connect(self.settings.memory_database) as connection:
            return connection.execute(
                "SELECT suppression_key, suppressed FROM skill_suppressions ORDER BY suppression_id"
            ).fetchall()

    def test_undo_a_task_kind_suppression_offers_again(self) -> None:
        kind = self.kind()
        self.assertTrue(self.app.suppress(kind)["ok"])
        self.assertIsNone(self.offered())
        self.assertEqual(
            [(kind, "task")], [(s["key"], s["scope"]) for s in self.app.suppressions()["suppressions"]]
        )
        self.assertEqual(
            {"ok": True, "key": kind, "suppressed": False, "changed": True}, self.app.unsuppress(kind)
        )
        self.assertEqual([], self.app.suppressions()["suppressions"])
        self.assertIsNotNone(self.offered())
        self.assertEqual([(kind, 1), (kind, 0)], self.rows())

    def test_undo_the_global_suppression_offers_again(self) -> None:
        self.assertTrue(self.app.suppress(None, "global")["ok"])
        self.assertIsNone(self.offered())
        self.assertEqual([GLOBAL_SUPPRESSION], [s["key"] for s in self.app.suppressions("global")["suppressions"]])
        self.assertEqual([], self.app.suppressions("task")["suppressions"])
        self.assertTrue(self.app.unsuppress(None, "global")["changed"])
        self.assertIsNotNone(self.offered())

    def test_global_and_task_kind_are_undone_one_at_a_time(self) -> None:
        kind = self.kind()
        self.app.suppress(kind)
        self.app.suppress(None, "global")
        self.assertEqual(
            [(kind, "task"), (GLOBAL_SUPPRESSION, "global")],
            [(s["key"], s["scope"]) for s in self.app.suppressions()["suppressions"]],
        )
        self.assertTrue(self.app.unsuppress(kind)["changed"])
        self.assertIsNone(self.offered())  # global still on
        self.assertTrue(self.app.unsuppress(None, "global")["changed"])
        self.assertIsNotNone(self.offered())

    def test_double_undo_is_idempotent_and_appends_nothing(self) -> None:
        self.app.suppress(OTHER)
        self.assertTrue(self.app.unsuppress(OTHER)["changed"])
        again = self.app.unsuppress(OTHER)
        self.assertEqual({"ok": True, "key": OTHER, "suppressed": False, "changed": False}, again)
        self.assertEqual([(OTHER, 1), (OTHER, 0)], self.rows())
        self.assertFalse(self.store.is_suppressed(OTHER))

    def test_resuppress_after_undo_wins_again(self) -> None:
        self.app.suppress(OTHER)
        self.app.unsuppress(OTHER)
        self.app.suppress(OTHER)
        self.assertTrue(self.store.is_suppressed(OTHER))
        self.assertEqual([OTHER], [s["key"] for s in self.app.suppressions()["suppressions"]])

    def test_another_key_and_another_projects_drafts_are_unaffected(self) -> None:
        # Suppression keys are project-independent (task kind or global); an
        # undo touches only its own key and never another project's records.
        self.store.add_draft(draft(7, SCOPE_A))
        self.app.suppress(OTHER)
        self.app.suppress(None, "global")
        self.app.unsuppress(OTHER)
        self.assertTrue(self.store.is_suppressed(GLOBAL_SUPPRESSION))
        self.assertEqual(
            [f"draft-{7:032x}"], [d.draft_id for d in self.store.pending_drafts("proj-a")]
        )

    def test_an_unknown_or_malformed_key_is_refused(self) -> None:
        for args in ((OTHER,), (None, "global"), ("not-a-kind",), (OTHER, "everything"), (123,)):
            with self.subTest(args=args):
                result = self.app.unsuppress(*args)
                self.assertFalse(result["ok"])
        self.assertIn("no such suppression", self.app.unsuppress(OTHER)["error"])
        self.assertEqual([], self.rows())
        self.assertFalse(self.app.suppressions("everything")["ok"])

    def test_listing_works_with_the_flag_off_but_undo_does_not(self) -> None:
        self.app.suppress(OTHER)
        off = open_app(destination_policy=(), settings=dataclasses.replace(self.settings, skills_enabled=False))
        self.assertEqual([OTHER], [s["key"] for s in off.suppressions()["suppressions"]])
        self.assertFalse(off.unsuppress(OTHER)["ok"])
        self.assertTrue(self.store.is_suppressed(OTHER))


if __name__ == "__main__":
    unittest.main()
