"""V2 Projects Gate 7 phase A: end-to-end coverage, tests only.

A1. One journey on ONE temp store: project -> instruction -> file -> Global
    conversation -> attach (6C, through the app result) -> the next run
    carries the instruction and the file excerpt -> archive (read-only,
    submit refused, no model call) -> unarchive -> retire + purge.
A2. The Projects page archive / unarchive controls, clicked in the real QML.
A3. A moved conversation survives a restart (a NEW bridge on the same DB).
A5. The real ClaudeCodeConnector (Popen mocked) -> bridge -> approval card:
    the stored instruction and file excerpts reach the card exactly as
    attested and exactly as sent; no hand-built payload.

(A4, the rollback-release checks, lives in test_conversation_store.py.)

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from unittest import mock

from jarvis.app import (
    conversation_attach_preview_result,
    conversation_attach_result,
    create_app,
    project_file_result,
    project_instruction_result,
    project_lifecycle_result,
)
from jarvis.connectors import claude_code
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.contracts import (
    PROJECT_FILES_KEY,
    PROJECT_INSTRUCTION_KEY,
    RunStatus,
    SessionState,
)
from jarvis.conversation import append_turn
from jarvis.intent import IntentStatus
from jarvis.registry import ToolRegistry
from tests.helpers import installed_cli, settled
from tests.test_claude_connector import OAUTH_MAX_AUTH_STATUS, FakeProcess, patch_oauth_environment
from tests.test_egress_attestation import CLAUDE_TUPLES, SUCCESS_CLI
from tests.test_intent import ScriptedModel, StubConnector, chat_proposal, task_proposal
from tests.test_project_files import OBJECTIVE, RELEVANT, CoreFileCase
from tests.test_projects_ui import ProjectsUiCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

INSTRUCTION = "Yanıtları kısa tut, kaynak göster."


def say(memory, conversation_id: str, text: str) -> None:
    memory.update_conversation_state(conversation_id, lambda s: append_turn(
        s, role="user", kind="user", text=text, message=text, key=text)[0])


def table(database, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(database)) as connection:
        return [tuple(row) for row in connection.execute(sql).fetchall()]


def sent_excerpts(payload: dict) -> list[tuple]:
    return [(e["name"], e["chunk_index"], e["classification"], e["text"])
            for e in payload[PROJECT_FILES_KEY]["excerpts"]]


class JourneyTests(CoreFileCase):
    """A1: every step asserts the canonical state, on one store."""

    def linked_session(self, conversation_id: str) -> SessionState:
        session = SessionState(objective="window session")
        self.memory.save_session(session)
        self.memory.link_session_conversation(session.id, conversation_id)
        return session

    def waiting_in(self, session, task):
        run = self.core.run(task, session.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, run.error)
        return run

    def intent_app(self, model: ScriptedModel):
        return create_app(settings=self.settings, memory=self.memory, registry=ToolRegistry(),
                          connectors=[StubConnector("claude-code-plan", ("code_analysis", "code_planning"))],
                          destination_policy=CLAUDE_TUPLES, intent_model=model)

    def test_a1_project_instruction_file_attach_archive_unarchive_purge(self) -> None:
        memory, settings = self.memory, self.settings
        # 1. A project (the case's own P), empty.
        self.assertEqual(("P", True), (memory.load_project(self.p.id).name, memory.load_project(self.p.id).active))
        # 2. Instruction through the app result: one current version.
        shown = project_instruction_result(memory, settings, "set", self.p.id, INSTRUCTION, "internal")
        self.assertEqual(0, shown.exit_code, shown.payload)
        self.assertEqual((INSTRUCTION, "internal"), (memory.load_project_instruction(self.p.id).text,
                                                     memory.load_project_instruction(self.p.id).classification.value))
        self.assertEqual(1, len(memory.list_project_instruction_history(self.p.id)))
        # 3. A file through the app result: row, chunks, counters.
        added = project_file_result(memory, settings, "add", self.p.id, path=str(self.write("notes.md", RELEVANT)),
                                    classification="internal")
        self.assertEqual(0, added.exit_code, added.payload)
        file_id = added.payload["file"]["id"]
        self.assertEqual([("active", "notes.md")], [(f["lifecycle"], f["display_name"])
                                                    for f in memory.list_project_files(self.p.id)])
        self.assertEqual([0], [r[0] for r in table(memory.database_path, "SELECT chunk_index FROM project_file_chunks")])
        # 4. A Global conversation with turns; History lists it, the project does not.
        conversation = memory.create_conversation()
        say(memory, conversation.id, "ilk mesaj")
        say(memory, conversation.id, "ikinci mesaj")
        self.assertEqual([conversation.id], [c.id for c in memory.list_conversations(project_id=None)])
        self.assertEqual([], memory.list_conversations(project_id=self.p.id))
        # 5. Attach through the app result (preview, then the bound confirm).
        preview = conversation_attach_preview_result(memory, settings, conversation.id, project_id=self.p.id)
        self.assertEqual("", preview.payload["preview"]["blocked"])
        attached = conversation_attach_result(memory, settings, conversation.id, project_id=self.p.id,
                                              expected_next_seq=preview.payload["preview"]["expected_next_seq"])
        self.assertEqual(0, attached.exit_code, attached.payload)
        self.assertEqual(self.p.id, memory.load_conversation(conversation.id).project_id)
        self.assertEqual([conversation.id], [c.id for c in memory.list_conversations(project_id=self.p.id)])
        self.assertEqual([], memory.list_conversations(project_id=None))
        self.assertEqual([(conversation.id, self.p.id)],
                         table(memory.database_path, "SELECT conversation_id, project_id FROM conversation_moves"))
        self.assertEqual(["ilk mesaj", "ikinci mesaj"],
                         [t.message for t in memory.load_conversation_state(conversation.id).turns])
        # 6. The next run carries the instruction and the file excerpt, as sent.
        session = self.linked_session(conversation.id)
        run = self.waiting_in(session, self.task(self.p.id, objective=OBJECTIVE))
        self.assertEqual(INSTRUCTION, run.task.project_instruction)
        self.assertEqual(["notes.md"], [e["name"] for e in run.task.project_files_block["excerpts"]])
        done, prompt, calls = self.execute(run)
        self.assertEqual((RunStatus.COMPLETED, 1), (done.status, calls), done.error)
        payload = self.payload(prompt)
        self.assertEqual(INSTRUCTION, payload[PROJECT_INSTRUCTION_KEY]["text"])
        self.assertEqual(["notes.md"], [name for name, *_ in sent_excerpts(payload)])
        self.assertEqual(self.p.id, memory.load_session(session.id).project_id)
        # 7. Archive: the project is inactive, the conversation is read-only, a
        # submit is refused and no model is ever called; nothing is created.
        self.assertEqual(0, project_lifecycle_result(memory, settings, "archive", self.p.id).exit_code)
        self.assertFalse(memory.load_project(self.p.id).active)
        tasks_before = table(memory.database_path, "SELECT COUNT(*) FROM tasks")
        model = ScriptedModel(chat_proposal("never"))
        refused = self.intent_app(model).submit_text("merhaba", conversation_id=conversation.id).payload
        self.assertEqual(IntentStatus.INVALID_INPUT.value, refused["intent"]["status"], refused)
        self.assertIn("project is archived", refused["intent"]["message"])
        self.assertEqual([], model.calls)
        self.assertIsNone(refused["run"])
        self.assertEqual(tasks_before, table(memory.database_path, "SELECT COUNT(*) FROM tasks"))
        self.assertEqual(["ilk mesaj", "ikinci mesaj"],
                         [t.message for t in memory.load_conversation_state(conversation.id).turns])   # still readable
        with mock.patch.object(claude_code.subprocess, "Popen") as popen:
            blocked = self.core.run(self.task(self.p.id, objective=OBJECTIVE), session.id)
        self.assertEqual((RunStatus.FAILED, 0), (blocked.status, popen.call_count))
        # 8. Unarchive: writable again, same owner, same content.
        self.assertEqual(0, project_lifecycle_result(memory, settings, "unarchive", self.p.id).exit_code)
        self.assertTrue(memory.load_project(self.p.id).active)
        self.assertEqual(self.p.id, memory.load_conversation(conversation.id).project_id)
        self.assertEqual(INSTRUCTION, memory.load_project_instruction(self.p.id).text)
        model = ScriptedModel(chat_proposal("merhaba"))
        again = self.intent_app(model).submit_text("merhaba", conversation_id=conversation.id).payload
        self.assertNotEqual(IntentStatus.INVALID_INPUT.value, again["intent"]["status"], again)
        self.assertEqual(1, len(model.calls))
        # 9. Retire, then purge the file: tombstone, chunks gone, quota freed.
        retired = project_file_result(memory, settings, "retire", self.p.id, file_id=file_id)
        self.assertEqual("retired", retired.payload["file"]["lifecycle"])
        purged = project_file_result(memory, settings, "purge", self.p.id, file_id=file_id, confirm=file_id)
        self.assertEqual(0, purged.exit_code, purged.payload)
        self.assertEqual("purged", purged.payload["file"]["lifecycle"])
        self.assertEqual([], table(memory.database_path, "SELECT * FROM project_file_chunks"))
        tombstone = table(memory.database_path, "SELECT id, project_id, lifecycle, display_name, byte_size, char_count, "
                                                 "classification FROM project_files")
        self.assertEqual([(file_id, self.p.id, "purged", None, None, None, None)], tombstone)
        self.assertEqual([], memory.list_project_files(self.p.id))                       # retained/active/chars: 0
        self.assertEqual(["purged"], [f["lifecycle"] for f in memory.list_project_files(self.p.id, include_purged=True)])
        readded = project_file_result(memory, settings, "add", self.p.id, path=str(self.write("again.md", RELEVANT)),
                                      classification="internal")
        self.assertEqual(0, readded.exit_code, readded.payload)                        # the freed quota is usable


class ArchiveControlsTests(ProjectsUiCase):
    """A2: the real click path on the Projects page."""

    def visible(self, root, name) -> list:
        return [i for i in self.all_items(root, name) if i.isVisible()]

    def click_visible(self, root, name) -> None:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtTest import QTest
        (item,) = self.visible(root, name)
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier,
                         item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())
        pump(300)

    def selector_labels(self, root) -> list[str]:
        """The projects a scope pick offers (Gate 6F: the sidebar's rows)."""
        return sorted(i.property("title") for i in self.visible(root, "sidebarProject"))

    def test_a2_archive_and_unarchive_by_clicking(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        owned = store.create_conversation(project.id)
        say(store, owned.id, "merhaba")
        bridge = self.bridge(json.dumps({"kind": "chat", "reply": "never"}))
        root = self.window(bridge)
        bridge.openConversation(owned.id)
        pump(300)
        self.assertFalse(bridge.conversationReadOnly)
        self.assertTrue(self.item(root, "composerInput").isEnabled())
        self.assertEqual(["Evim"], self.selector_labels(root))
        # The detail's Archive button.
        bridge.showProject(project.id)
        bridge.setPage("projects")
        pump(400)
        self.assertEqual([], self.visible(root, "projectDetailUnarchive"))
        self.click_visible(root, "projectArchive")
        self.assertFalse(store.load_project(project.id).active)
        self.assertTrue(self.item(root, "projectArchivedBanner").isVisible())
        self.assertEqual([], self.visible(root, "projectArchive"))
        self.assertEqual(1, len(self.visible(root, "projectDetailUnarchive")))
        # G6PJ1B (6G P2): the list row's Unarchive moved to the card menu: asserted in the last block.
        self.assertFalse(self.item(root, "projectNewConversation").isEnabled())
        # The open conversation is read-only, its composer off, the selector hides it.
        bridge.setPage("home")
        pump(400)
        self.assertTrue(bridge.conversationReadOnly)
        self.assertTrue(self.item(root, "readOnlyBanner").isVisible())
        self.assertFalse(self.item(root, "composerInput").isEnabled())
        self.assertEqual("Evim · archived", self.item(root, "chatHeaderScope").property("fullName"))
        self.assertEqual([], self.selector_labels(root))
        bridge.submitText("zorla gönder")
        pump(200)
        self.assertEqual([], self.model.calls)
        self.assertEqual(0, table(self.database, "SELECT COUNT(*) FROM tasks")[0][0])
        # The detail's Unarchive button.
        bridge.setPage("projects")
        pump(400)
        self.click_visible(root, "projectDetailUnarchive")
        self.assertTrue(store.load_project(project.id).active)
        self.assertFalse(self.item(root, "projectArchivedBanner").isVisible())
        bridge.setPage("home")
        pump(400)
        self.assertFalse(bridge.conversationReadOnly)
        self.assertFalse(self.item(root, "readOnlyBanner").isVisible())
        self.assertTrue(self.item(root, "composerInput").isEnabled())
        self.assertEqual(["Evim"], self.selector_labels(root))
        # The card menu's Unarchive (G6PJ1B, 6G P2: was the list row's button), on the Arşiv tab.
        bridge.setPage("projects")
        pump(400)
        self.click_visible(root, "projectArchive")
        self.assertFalse(store.load_project(project.id).active)
        self.click_visible(root, "projectsBack")
        self.click_visible(root, "projectsTab_archived")
        self.click_visible(root, "projectCardMenu")
        self.assertEqual(1, len(self.visible(root, "projectMenuUnarchive")))
        self.click_visible(root, "projectMenuUnarchive")
        self.assertTrue(store.load_project(project.id).active)
        self.assertEqual([], self.visible(root, "projectMenuUnarchive"))
        self.assertEqual([], self.visible(root, "projectCard"))                       # the archive tab is empty
        self.assertTrue(self.item(root, "projectsEmpty").isVisible())
        self.assertEqual(0, self.execute_calls())


class RestartTests(ProjectsUiCase):
    """A3: a moved conversation after the application restarts."""

    def test_a3_a_new_bridge_on_the_same_db_sees_the_owner(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        first = self.bridge()
        conversation_id = first.conversationId
        say(store, conversation_id, "taşınacak sohbet")
        first.previewAttach(project.id, "")
        pump(60)
        self.assertTrue(first.confirmAttach(project.id, ""), first.attachNotice)
        # Restart: a brand-new backend and bridge on the same database.
        second = self.bridge(task_proposal("code_analysis", objective="Bu projeyi incele."))
        reopened = self.db()
        self.assertEqual(project.id, reopened.load_conversation(conversation_id).project_id)
        self.assertEqual([conversation_id], [c.id for c in reopened.list_conversations(project_id=project.id)])
        self.assertNotIn(conversation_id, [row["id"] for row in second.conversations])     # excluded from History
        self.assertFalse(any(row["id"] == conversation_id for row in second.conversations))
        root = self.window(second)
        self.assertTrue(second.openProjectScope(project.id))
        settled(300, lambda: (
            self.assertEqual(conversation_id, second.conversationId),                          # the moved conversation
            self.assertEqual("Evim", self.item(root, "chatHeaderScope").property("fullName")),
            self.assertEqual(["Evim"], [i.property("title") for i in self.all_items(root, "sidebarProject") if i.isVisible()]),
            self.assertFalse(second.canAttach),                                                  # never moves again
        ))
        # The next run is P-scoped.
        second.submitText("Bu projeyi incele.")
        settled(100, lambda: (
            self.assertTrue(second.approvalPending),
        ))
        ticket = second._ticket
        task = reopened.load_task(ticket.task_id)
        self.assertEqual(project.id, task.project_id)
        self.assertEqual(project.id, reopened.load_session(ticket.session_id).project_id)
        self.assertEqual(conversation_id, reopened.conversation_for_session(ticket.session_id))
        second.deny()
        settled(100, lambda: (
            self.assertEqual(0, self.execute_calls()),
        ))


class RealConnectorCardTests(ProjectsUiCase):
    """A5: the real ClaudeCodeConnector behind the bridge (only Popen mocked)."""

    def factory(self, settings):
        self.connector = ClaudeCodeConnector(allowed_roots=(self.root,), default_working_directory=self.root,
                                             max_budget_usd=0.15)
        self.claude = StubConnector("unused", ("code_analysis",))          # the base class's execute counter
        return create_app(
            settings=settings, memory=self.db(), registry=ToolRegistry(), connectors=[self.connector],
            destination_policy=CLAUDE_TUPLES, intent_model=self.model,
        )

    def setUp(self) -> None:
        super().setUp()
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)
        for patcher in (
            mock.patch.object(ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS),
            mock.patch.object(claude_code, "resolve_trusted_executable", return_value=installed_cli(self)),
            mock.patch.object(claude_code.subprocess, "run", side_effect=AssertionError("subprocess.run")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a5_instruction_and_excerpts_reach_the_card_as_attested(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        self.assertEqual(0, project_instruction_result(store, self.store.load().settings, "set", project.id,
                                                       INSTRUCTION, "internal").exit_code)
        path = self.root / "notes.md"
        path.write_text(RELEVANT, encoding="utf-8")
        self.assertEqual(0, project_file_result(store, self.store.load().settings, "add", project.id,
                                                path=str(path), classification="internal").exit_code)
        bridge = self.bridge(task_proposal("code_planning", objective=OBJECTIVE))
        root = self.window(bridge)
        self.assertTrue(bridge.openProjectScope(project.id))
        pump(300)
        bridge.submitText(OBJECTIVE)
        settled(300, lambda: (
            self.assertTrue(bridge.approvalPending, [e["text"] for e in bridge.activity + bridge.feed]),
        ))
        card = bridge.approval
        # The card: the stored instruction, complete, and the stored excerpt.
        self.assertEqual(INSTRUCTION, card["project_instruction"])
        self.assertEqual(1, len(card["project_files"]))
        excerpt = card["project_files"][0]
        self.assertEqual(("notes.md", 0, "internal", RELEVANT),
                         (excerpt["name"], excerpt["chunk_index"], excerpt["classification"], excerpt["text"]))
        # The real window renders exactly those characters.
        shown_instruction = [i for i in self.all_items(root, "approvalInstruction") if i.isVisible()]
        shown_excerpt = [i for i in self.all_items(root, "approvalExcerpt") if i.isVisible()]
        self.assertEqual(INSTRUCTION, shown_instruction[0].property("text"))
        self.assertEqual(RELEVANT, shown_excerpt[0].property("text"))
        # Approving sends exactly what the card showed.
        with mock.patch.object(claude_code.subprocess, "Popen", return_value=FakeProcess(SUCCESS_CLI)) as popen:
            bridge.approve()
            pump(300)
        self.assertEqual(1, popen.call_count)
        command = popen.call_args.args[0]
        prompt = command[command.index("-p") + 1]
        payload = json.loads(prompt[len(self.connector.prompt_prefix):])
        self.assertEqual(card["project_instruction"], payload[PROJECT_INSTRUCTION_KEY]["text"])
        self.assertEqual([(e["name"], e["chunk_index"], e["classification"], e["text"]) for e in card["project_files"]],
                         sent_excerpts(payload))
