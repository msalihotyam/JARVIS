"""V2.1 cleanup V21K3 (Salih 2026-10-11).

(a) G6H3POL residual: the person's REJECT of a K3 card whose proposal changed
    in between (they made the folder themselves, renamed or linked the
    project) ends REJECTED, not FAILED. Nothing is created, approve stays
    impossible, and the identity and fingerprint checks still run; an
    approve after the same change still FAILS.
(b) Gate 6G B-4: a malformed current folder link row reads as "link
    unreadable", never "folderless": no K3 offer or card, no create, a
    localized notice, and the row is never repaired.

Temp stores and folders only; fake models; no live DB, model or network call.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import unittest
from unittest import mock
from uuid import uuid4

from jarvis.connectors.project_folder import PROJECT_FOLDER_CAPABILITY, STALE_PROPOSAL
from jarvis.contracts import AttestationError, ErrorCode, PermissionGrant, future_time
from jarvis.memory import PROJECT_FOLDER_LINK_UNREADABLE
from jarvis.ui.i18n import localization
from tests import test_g6h2_folder_ui as h2
from tests import test_project_folder_k3 as k3
from tests import test_project_folders_6h as h6
from tests.test_intent import proposal
from tests.test_ui import _APP
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401


def corrupt_link(database, project_id: str) -> None:
    """A current link row this build could never have written (bad origin)."""
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("INSERT INTO project_folder_links VALUES (?, ?, ?, 'evil', ?, NULL)",
                           ("pfl-" + uuid4().hex, project_id, "D:\\work\\x", "2026-10-11T00:00:00Z"))
        connection.commit()


class StaleRejectTests(k3.K3Case):
    def changes(self):
        other = self.folder("elsewhere")
        return {
            "the person made the folder": lambda path: os.makedirs(path),
            "project renamed": lambda path: self.memory.rename_project(self.p.id, f"Gamma {self.p.id}"),
            "a link added": lambda path: self.app("link", self.p.id, path=other),
        }

    def pending(self, name: str):
        self.p = self.memory.create_project(f"Alpha {name}")
        payload, _ = self.ask(self.p)
        return payload, self.proposed(self.p)

    def test_a_reject_after_a_changed_proposal_ends_rejected(self) -> None:
        for name, change in self.changes().items():
            with self.subTest(name):
                payload, path = self.pending(name)
                session_id, task_id, _, _ = k3.ids(payload)
                change(path)
                before = self.everything()
                with self.removes_nothing():
                    done = self.k3.reject(*k3.ids(payload)).payload
                self.assertEqual("rejected", done["run_status"], done)
                self.assertEqual("rejected", self.memory.load_task(task_id).status.value)
                self.assertIsNone(self.memory.load_pending_approval(task_id, session_id))
                self.assertEqual(before, self.everything())                          # nothing created
                self.assertEqual("task_not_found", self.approve(payload)["error"]["code"])   # approve impossible
                self.assertEqual(before, self.everything())
        self.assertEqual(0, self.rows("SELECT COUNT(*) FROM permission_grants WHERE approved = 1")[0][0])

    def test_a_an_approve_after_the_same_change_still_fails(self) -> None:
        for name, change in self.changes().items():
            with self.subTest(name):
                payload, path = self.pending(name)
                change(path)
                before = self.everything()
                with self.removes_nothing():
                    done = self.approve(payload)
                self.assertEqual(("failed", STALE_PROPOSAL), (done["run_status"], done["error"]["message"]))
                self.assertEqual("failed", self.memory.load_task(payload["run"]["task"]["id"]).status.value)
                self.assertEqual(before, self.everything())

    def test_a_identity_and_fingerprint_are_still_checked_on_the_reject(self) -> None:
        payload, path = self.pending("checks")
        session_id, task_id, request_id, fingerprint = k3.ids(payload)
        os.makedirs(path)                                                                # stale now
        core = self.k3.core

        def rejection(**changed) -> PermissionGrant:
            fields = dict(request_id=request_id, task_id=task_id, session_id=session_id,
                          fingerprint=fingerprint, approved=False, expires_at=future_time(15))
            return PermissionGrant(**{**fields, **changed})

        for name, grant in (("other fingerprint", rejection(fingerprint="0" * 64)),
                            ("other request id", rejection(request_id="other"))):
            with self.subTest(name):
                core.resume(session_id, task_id, grant)
                self.assertEqual("awaiting_approval", self.memory.load_task(task_id).status.value)
                self.assertEqual(request_id, self.memory.load_pending_approval(task_id, session_id).id)
        # The registry / destination identity check runs on the stored request.
        refused = AttestationError(ErrorCode.EGRESS_REFUSED, "identity")
        with mock.patch.object(core, "_destination_policy_refusal", side_effect=refused) as checked:
            core.resume(session_id, task_id, rejection())
        self.assertTrue(checked.called)
        self.assertEqual("failed", self.memory.load_task(task_id).status.value)       # never REJECTED unchecked
        self.assertEqual(0, self.rows("SELECT COUNT(*) FROM permission_grants")[0][0])


class UnreadableLinkTests(k3.K3Case):
    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.memory.create_conversation(self.p.id)
        corrupt_link(self.database, self.p.id)
        self.links_before = self.rows("SELECT * FROM project_folder_links ORDER BY rowid")

    def tearDown(self) -> None:
        self.assertEqual(self.links_before, self.rows("SELECT * FROM project_folder_links ORDER BY rowid"))
        super().tearDown()                                                               # never repaired

    def test_b_never_folderless_so_nothing_is_offered_or_proposed(self) -> None:
        self.assertFalse(self.k3._folderless(self.p.id))
        self.model.answers.append(proposal(kind="chat", reply="tamam"))
        self.k3.submit_text("merhaba", conversation_id=self.conversation.id)
        names = [c["name"] for c in self.model.calls[-1].context["catalog"]["capabilities"]]
        self.assertNotIn(PROJECT_FOLDER_CAPABILITY, names)
        with self.assertRaisesRegex(ValueError, f"^{re.escape(PROJECT_FOLDER_LINK_UNREADABLE)}$"):
            self.proposed(self.p)

    def test_b_no_card_and_no_create(self) -> None:
        before = self.everything()
        with self.removes_nothing():
            payload, _ = self.ask(self.p, conversation=self.conversation)
        self.assertIsNone((payload.get("run") or {}).get("approval"))
        self.assertEqual(0, self.count("pending_approvals"))
        documents = h6.tree(self.folders)
        path = str(self.folders / "Documents" / k3.JP / self.p.name)
        created = self.app("create", self.p.id, path=path, on_desktop=False)
        self.assertNotEqual(0, created.exit_code)
        self.assertEqual(documents, h6.tree(self.folders))
        self.assertNotEqual(0, self.app("link", self.p.id, path=self.folder("var")).exit_code)
        self.assertEqual(before[1], self.everything()[1])

    def test_b_show_and_check_say_unreadable(self) -> None:
        for action, options in (("show", {}), ("check", {"path": self.folder("var")})):
            with self.subTest(action):
                shown = self.app(action, self.p.id, **options).payload
                self.assertIsNone(shown.get("folder"))
                self.assertEqual(PROJECT_FOLDER_LINK_UNREADABLE, shown["error"]["message"])


@unittest.skipUnless(h6.WINDOWS, "Windows folder links")
class NoticeTests(h2.Case):
    def test_b_the_project_page_says_unreadable_in_the_ui_language(self) -> None:
        corrupt_link(self.database, self.project.id)
        bridge = self.bridge()
        said = {"en-US": "A project folder link could not be read, so no folder is shown or offered. Nothing was changed.",
                "tr-TR": "Bir proje klasörü bağlantısı okunamadı; bu yüzden klasör gösterilmiyor ve önerilmiyor. "
                         "Hiçbir şey değiştirilmedi."}
        self.addCleanup(localization(_APP).apply, "en-US")
        for language, text in said.items():
            with self.subTest(language):
                localization(_APP).apply(language)
                bridge.showProject(self.project.id)
                self.assertEqual(({}, text), (bridge.projectFolder, bridge.projectFolderNotice))
        bridge.showProject(self.other.id)
        self.assertEqual(({}, ""), (bridge.projectFolder, bridge.projectFolderNotice))   # only that project


if __name__ == "__main__":
    unittest.main()
