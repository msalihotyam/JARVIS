"""Gate 6H H3 (Salih 2026-10-11, "A"): `jarvis approve` refuses a K3 request.

The folder path is shown only on the app's card, so the CLI never approves a
project_folder_create request: refused with the exact Turkish message,
non-zero, nothing approved, the request still pending, no grant, no folder;
`--remember-for-session` changes nothing. `jarvis reject` of it still works,
an ordinary approval still executes, and an unreadable request is refused.

Temp database and folders only; fake models; no live DB, model or network.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import unittest

from jarvis import cli
from jarvis.contracts import SideEffect
from tests import test_cli as tc
from tests import test_project_folders_6h as h6
from tests.test_project_folder_k3 import K3Case, ids
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

REFUSAL = "Bu klasör onayı yalnız JARVIS uygulamasındaki karttan verilebilir; klasörün yeri orada gösterilir."


class CliK3Tests(K3Case):
    def setUp(self) -> None:
        super().setUp()
        self.payload, _ = self.ask(self.p)
        self.session_id, self.task_id, self.request_id, self.fingerprint = ids(self.payload)
        self.assertEqual("awaiting_approval", self.payload["run"]["run_status"])

    def cli(self, verb: str, *extra: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main([verb, "--session-id", self.session_id, "--task-id", self.task_id,
                             "--request-id", self.request_id, "--fingerprint", self.fingerprint, *extra],
                            settings=self.settings, app_factory=lambda settings: self.k3)
        return code, json.loads(out.getvalue())

    def state(self):
        return (self.memory.load_task(self.task_id).status, self.count("permission_grants"),
                self.rows("SELECT id, status FROM pending_approvals ORDER BY rowid"),
                h6.dirs(self.folders), self.memory.load_project_folder(self.p.id))

    def test_approve_of_a_k3_request_is_refused_and_changes_nothing(self) -> None:
        for extra in ((), ("--remember-for-session",)):
            with self.subTest(extra=extra):
                before = self.state()
                code, payload = self.cli("approve", *extra)
                self.assertNotEqual(0, code)
                self.assertEqual(REFUSAL, payload["error"]["message"])
                self.assertEqual("approve", payload["command"])
                self.assertEqual(before, self.state())
                self.assertIsNotNone(self.memory.load_pending_approval(self.task_id, self.session_id))
                self.assertEqual(0, self.count("permission_grants"))
                self.assertIsNone(self.memory.load_project_folder(self.p.id))

    def test_reject_of_a_k3_request_still_rejects(self) -> None:
        folders = h6.dirs(self.folders)
        code, payload = self.cli("reject")
        self.assertEqual("rejected", payload["run_status"], payload)
        self.assertEqual("reject", payload["command"])
        self.assertIsNone(self.memory.load_pending_approval(self.task_id, self.session_id))
        self.assertEqual(folders, h6.dirs(self.folders))
        self.assertIsNone(self.memory.load_project_folder(self.p.id))

    def test_an_unreadable_request_is_refused_and_changes_nothing(self) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE pending_approvals SET request_json = '{not json' WHERE status = 'pending'")
        before = self.state()
        code, payload = self.cli("approve")
        self.assertNotEqual(0, code)
        self.assertEqual("task_not_found", payload["error"]["code"])
        self.assertNotEqual(REFUSAL, payload["error"]["message"])
        self.assertEqual(before, self.state())
        self.assertEqual([(1,)], self.rows("SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"))


class CliOrdinaryApproveTests(unittest.TestCase):
    """The regression: a non-K3 pending request is still approved by the CLI.
    Borrows test_cli's helpers without re-running its tests."""

    setUp = tc.CliTestCase.setUp
    factory_for = tc.CliTestCase.factory_for
    invoke = tc.CliTestCase.invoke
    task_file = tc.CliTestCase.task_file
    start_awaiting_approval = tc.CliTestCase.start_awaiting_approval

    def test_an_ordinary_approval_still_executes(self) -> None:
        connector = tc.FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS, preview=True)
        payload, session_id, task_id = self.start_awaiting_approval(connector)
        code, output, _ = self.invoke(
            ["approve", "--session-id", session_id, "--task-id", task_id,
             "--request-id", payload["approval"]["request_id"],
             "--fingerprint", payload["approval"]["fingerprint"]],
            connector,
        )
        self.assertEqual(0, code, output)
        self.assertEqual("completed", json.loads(output)["run_status"])
        self.assertEqual(1, connector.execute_calls)

    def test_no_pending_request_keeps_the_old_errors(self) -> None:
        payload, session_id, task_id = self.start_awaiting_approval(
            tc.FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS, preview=True))
        approval = ["--request-id", payload["approval"]["request_id"],
                    "--fingerprint", payload["approval"]["fingerprint"]]
        for session, task, error in (("no-such-session", task_id, "unknown_session"),
                                     (session_id, "no-such-task", "task_not_found")):
            with self.subTest(error=error):
                code, output, _ = self.invoke(["approve", "--session-id", session, "--task-id", task, *approval])
                self.assertEqual(error, json.loads(output)["error"]["code"])
                self.assertIn("Unknown", json.loads(output)["error"]["message"])


if __name__ == "__main__":
    unittest.main()
