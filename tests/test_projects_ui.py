"""V2 Projects Gate 6A: Projects UX - navigation, conversations, archive
view and the approval display, through the real bridge and the real QML.

A. Nav: Projects from the menu and Ctrl+6 (window-only page); History keeps
   the Global conversations.
B. List: active + archived sections; create / rename / archive / unarchive
   through the app paths; Core refusals shown as returned; names, never ids.
C. Detail: only the project's conversations; new conversation with that
   owner; no move control; archived / unknown owner refused by the store.
D. Selector (the 6A badge, Gate 6B): name or Global, opens its menu; no
   overlap in the top bar.
E. Archived: banner + disabled composer + jobs (bounded label honest); a
   forced submit is still refused by Core.
F. Memory page shows names.
G. Approval card: instruction and excerpts complete, inert plain text.
H. Turkish catalog for the new strings.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtTest import QTest

from jarvis.app import job_create_result
from jarvis.intent import NL_CAPABILITIES
from jarvis.memory import SQLiteMemoryStore
from jarvis.ui.present import _compact, approval_card
from tests import test_ui
from tests.helpers import settled
from tests.test_ui import UiTestCase, _APP, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

INSTRUCTION = "Yanıtları kısa tut.\n" + "i" * 1978 + "\nSON"          # 2,000 characters
EXCERPT = "Deploy notes.\n" + "e" * 3981 + "\nEND"                   # 4,000 characters
HOSTILE = ("<b>bold</b> <i>x</i> <a href=\"http://evil.example\">click</a> [md](http://evil.example) "
           "‮RTL override‬ ⁦isolate⁩ &amp; &lt;tag&gt; " + "W" * 600)


class ProjectsUiCase(UiTestCase):
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def db(self) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(self.database)

    def all_items(self, root, name) -> list:
        found = []

        def walk(node):
            if node.objectName() == name:
                found.append(node)
            for child in node.childItems():
                walk(child)
        walk(root.contentItem())
        return found

    def rect(self, item) -> QRectF:
        top_left = item.mapToScene(QPointF(0, 0))
        return QRectF(top_left.x(), top_left.y(), item.width(), item.height())

    def steady(self, root, name) -> None:
        """Wait until ``name`` stops moving: the home work surface reflows (animates its
        width) after the window opens, and the chat header moves with it (Gate 6F)."""
        last = None
        for _ in range(60):
            now = self.rect(self.item(root, name))
            if now == last:
                return
            last = now
            pump(40)

    def texts(self, root, name) -> list[str]:
        return [item.property("text") for item in self.all_items(root, name) if item.isVisible()]


class NavigationTests(ProjectsUiCase):
    def test_a_projects_from_the_menu_and_ctrl_6_history_unchanged(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        owned = store.create_conversation(project.id)
        bridge = self.bridge()
        root = self.window(bridge)
        self.click(root, "navigationTrigger")
        self.click(root, "navigate_projects", then=lambda: (
            self.assertEqual(("projects", "projects"), (bridge.page, root.property("loadedPage"))),
        ))
        bridge.setPage("home")
        pump(300)
        QTest.keyClick(root, Qt.Key_6, Qt.ControlModifier)
        pump(400)
        self.assertEqual("projects", root.property("loadedPage"))
        # Window-only: a release that does not know the page never reads it.
        self.assertNotEqual("projects", json.loads(self.prefs_path.read_text(encoding="utf-8"))["page"])
        QTest.keyClick(root, Qt.Key_Escape)
        settled(400, lambda: (
            self.assertEqual("home", bridge.page),
        ))
        QTest.keyClick(root, Qt.Key_5, Qt.ControlModifier)
        settled(400, lambda: (
            self.assertEqual("history", root.property("loadedPage")),
            self.assertNotIn(owned.id, [row["id"] for row in bridge.conversations]),      # History: Global only
            self.assertEqual(0, self.execute_calls()),
        ))


class ListTests(ProjectsUiCase):
    def test_b_lifecycle_through_the_app_paths_and_refusals_shown(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("projects")
        pump(400)
        bridge.createProject("Evim")
        pump(100)
        bridge.createProject("Tez")
        pump(100)
        records = {r["name"]: r for r in bridge.projectRecords}
        self.assertEqual({"Evim", "Tez"}, set(records))
        self.assertTrue(all(r["id"].startswith("p-") for r in records.values()))
        self.assertEqual("", bridge.projectNotice)
        for refused, message in ((lambda: bridge.createProject("   "), "non-blank"),
                                 (lambda: bridge.createProject("evim"), "already exists"),
                                 (lambda: bridge.createProject("x" * 121), "longer than"),
                                 (lambda: bridge.renameProject("p-" + "0" * 32, "Yeni"), "Unknown project")):
            refused()
            pump(60)
            self.assertIn(message, bridge.projectNotice)
        self.assertEqual(2, len(bridge.projectRecords))
        bridge.renameProject(records["Tez"]["id"], "Tez 2025")
        pump(60)
        bridge.archiveProject(records["Tez"]["id"])
        settled(200, lambda: (
            self.assertEqual({"Evim": True, "Tez 2025": False}, {r["name"]: r["active"] for r in bridge.projectRecords}),
            self.assertTrue(self.item(root, "projectsArchivedHeading").isVisible()),
        ))
        names = self.texts(root, "projectRowName")
        self.assertEqual(["Evim", "Tez 2025"], names)                           # active first, then archived
        self.assertFalse(any(text.startswith("p-") for text in names))
        self.assertEqual(1, len([u for u in self.all_items(root, "projectUnarchive") if u.isVisible()]))
        bridge.unarchiveProject(records["Tez"]["id"])
        settled(200, lambda: (
            self.assertTrue(all(r["active"] for r in bridge.projectRecords)),
            self.assertFalse(self.item(root, "projectsArchivedHeading").isVisible()),
        ))


class DetailTests(ProjectsUiCase):
    def test_c_detail_lists_its_own_conversations_and_creates_owned_ones(self) -> None:
        store = self.db()
        a, b = store.create_project("A"), store.create_project("B")
        a1 = store.create_conversation(a.id)
        store.create_conversation(b.id)
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.showProject(a.id)
        settled(400, lambda: (
            self.assertEqual([a1.id], [row["id"] for row in bridge.projectConversations]),
            self.assertEqual("A", self.item(root, "projectDetailName").property("text")),
        ))
        before = len(store.list_conversations())
        self.assertTrue(bridge.newProjectConversation(a.id))
        self.assertEqual(before + 1, len(store.list_conversations()))
        self.assertEqual(a.id, store.load_conversation(bridge.conversationId).project_id)
        bridge.showProject(a.id)
        settled(300, lambda: (
            self.assertEqual(2, len(bridge.projectConversations)),
            # No move control here: the only owner change is the Gate 6C
            # Global -> project attach, offered in the top-bar selector alone.
            self.assertEqual([], [n for n in ("projectMove", "conversationMove", "moveConversation")
                                  if self.all_items(root, n)]),
        ))

    def test_c_the_store_path_refuses_an_archived_or_unknown_owner(self) -> None:
        store = self.db()
        archived = store.create_project("Arşiv")
        store.set_project_active(archived.id, False)
        bridge = self.bridge()
        opened = bridge.conversationId
        before = len(store.list_conversations())
        for project_id in (archived.id, "p-" + "0" * 32, "not-a-project"):
            with self.subTest(project=project_id):
                self.assertFalse(bridge.newProjectConversation(project_id))
                self.assertTrue(bridge.projectNotice)
        self.assertEqual(before, len(store.list_conversations()))
        self.assertEqual(opened, bridge.conversationId)


class BadgeTests(ProjectsUiCase):
    # Gate 6B (Salih decision 2026-09-30): the 6A badge became the project
    # selector in the same slot; Gate 6F: the owner is named by the chat
    # header's scope chip and the sidebar marks it; the sidebar lists the
    # projects directly (the selector rules are in tests/test_projects_ui_6b.py).
    def test_d_selector_names_the_owner_and_opens_its_menu(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        owned = store.create_conversation(project.id)
        bridge = self.bridge()
        root = self.window(bridge)
        owner = lambda: self.item(root, "chatHeaderScope").property("fullName")  # noqa: E731
        selected = lambda: [i.property("title") for i in self.all_items(root, "sidebarProject") if i.property("current")]  # noqa: E731
        self.assertEqual("Global", owner())
        self.assertTrue(self.item(root, "sidebarGlobal").property("current"))
        bridge.openConversation(owned.id)
        pump(300)
        self.assertEqual("Evim", owner())
        self.assertEqual(["Evim"], selected())
        self.assertTrue(self.item(root, "sidebarProjectsLink").isVisible())            # "All projects" (Q10)
        self.assertEqual(["Evim"], [i.property("title") for i in self.all_items(root, "sidebarProject") if i.isVisible()])
        self.assertEqual("home", bridge.page)
        bridge.newConversation()
        pump(300)
        self.assertEqual("Global", owner())
        self.assertEqual([], selected())


class ArchivedTests(ProjectsUiCase):
    def job(self, project_id: str, label: str) -> None:
        at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
        document = {"project_id": project_id, "kind": "one_time", "label": label,
                    "schedule": {"kind": "one_time", "at": at},
                    "steps": [{"objective": "read notes", "capability": "code_analysis",
                               "action": NL_CAPABILITIES["code_analysis"].action, "target": "",
                               "destination": "anthropic/claude-code"}],
                    "condition": None, "on_true": []}
        settings = self.store.load().settings
        result = job_create_result(self.db(), settings, json.dumps(document))
        self.assertEqual(0, result.exit_code, result.payload)

    def test_e_banner_disabled_composer_jobs_and_core_still_refuses(self) -> None:
        store = self.db()
        project = store.create_project("Tez")
        owned = store.create_conversation(project.id)
        bridge = self.bridge(json.dumps({"kind": "chat", "reply": "never"}))
        self.job(project.id, "Tez hatırlatma")
        store.set_project_active(project.id, False)
        root = self.window(bridge)
        bridge.openConversation(owned.id)
        settled(400, lambda: (
            self.assertTrue(bridge.conversationReadOnly),
            self.assertTrue(self.item(root, "readOnlyBanner").isVisible()),
            self.assertIn("Tez", self.item(root, "readOnlyReason").property("text")),
            self.assertFalse(self.item(root, "composerInput").isEnabled()),
            self.assertEqual("Tez · archived", self.item(root, "chatHeaderScope").property("fullName")),
        ))
        # A submit forced through the bridge still meets Core's refusal.
        bridge.submitText("zorla gönder")
        settled(200, lambda: (
            self.assertEqual([], self.model.calls),
        ))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        self.assertIn("read-only", "\n".join(e["text"] for e in bridge.activity + bridge.feed).lower())
        bridge.showProject(project.id)
        settled(400, lambda: (
            self.assertTrue(self.item(root, "projectArchivedBanner").isVisible()),
            self.assertEqual(["Tez hatırlatma"], [job["label"] for job in bridge.projectJobs]),
            self.assertFalse(bridge.projectJobsBounded),
            self.assertEqual("SCHEDULED JOBS", self.item(root, "projectJobsHeading").property("text")),
            self.assertFalse(self.item(root, "projectNewConversation").isEnabled()),
        ))

    def test_e_a_bounded_job_listing_never_claims_all(self) -> None:
        store = self.db()
        other, project = store.create_project("Other"), store.create_project("P")
        for index in range(20):
            self.job(other.id, f"other {index}")
        self.job(project.id, "hidden by the bound")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.showProject(project.id)
        settled(400, lambda: (
            self.assertTrue(bridge.projectJobsBounded),
            self.assertEqual([], bridge.projectJobs),                    # the store's first 20 omit it
            self.assertIn("first 20", self.item(root, "projectJobsHeading").property("text")),
        ))


class MemoryNamesTests(ProjectsUiCase):
    def test_f_memory_page_shows_names(self) -> None:
        project = self.db().create_project("Evim")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("memory")
        pump(400)
        bridge.refreshMemory("", False)
        pump(200)
        labels = [item.property("label") for item in self.all_items(root, "memoryProject")]
        self.assertEqual(["Evim"], labels)
        self.assertNotIn(project.id, labels)


class ApprovalDisplayTests(ProjectsUiCase):
    pending = test_ui.AuthorityTests.pending

    def payload(self, instruction: str, excerpt: str) -> dict:
        block = json.dumps({"notice": "n", "excerpts": [{"file_id": "pf-" + "a" * 32, "name": "notes.md",
                            "sha256": "b" * 64, "chunk_index": 2, "classification": "internal", "text": excerpt}]},
                           ensure_ascii=False, sort_keys=True)
        return {"approval": {"request_id": "r", "fingerprint": "f" * 64, "scope": {"action": "analyze", "connector": "c"},
                             "side_effect": {}, "outbound_preview": {"summary": "s", "field_names": ["objective"],
                                                                     "approximate_bytes": 9, "project_instruction": instruction,
                                                                     "project_files": block},
                             "attestation": {"outbound_digest": "d" * 64}},
                "task": {"id": "t"}, "session": {"id": "s"}}

    def test_g_presenter_keeps_everything_and_the_rest_unchanged(self) -> None:
        card = approval_card(self.payload(INSTRUCTION, EXCERPT))
        self.assertEqual(INSTRUCTION, card["project_instruction"])
        self.assertEqual([{"name": "notes.md", "chunk_index": 2, "classification": "internal", "text": EXCERPT}],
                         card["project_files"])
        self.assertEqual("f" * 64, card["fingerprint"])
        self.assertNotIn("truncated", card["project_instruction"] + card["project_files"][0]["text"])
        rest = {"summary": "s", "field_names": ["objective"], "approximate_bytes": 9}
        self.assertEqual(_compact(rest, 2000), card["outbound_preview"])            # the rest: as before
        self.assertEqual(_compact({"outbound_digest": "d" * 64}, 2000), card["attestation"])

    def show(self, bridge, root, instruction, excerpt) -> None:
        shown = approval_card(self.payload(instruction, excerpt))
        shown.update({k: bridge._approval[k] for k in ("request_id", "fingerprint", "task_id", "session_id")})
        bridge._approval = shown
        bridge.approvalChanged.emit()
        pump(400)

    def test_g_both_surfaces_render_complete_inert_plain_text(self) -> None:
        bridge = self.pending()
        root = self.window(bridge)
        for theme in ("space", "command"):
            for instruction, excerpt in ((INSTRUCTION, EXCERPT), (HOSTILE, HOSTILE + "\n" + EXCERPT[:100])):
                with self.subTest(theme=theme, hostile=instruction is HOSTILE):
                    bridge.setTheme(theme)
                    pump(400)
                    self.show(bridge, root, instruction, excerpt)
                    shown_instruction = [i for i in self.all_items(root, "approvalInstruction") if i.isVisible()]
                    shown_excerpt = [i for i in self.all_items(root, "approvalExcerpt") if i.isVisible()]
                    self.assertTrue(shown_instruction and shown_excerpt)
                    for edit, expected in ((shown_instruction[0], instruction), (shown_excerpt[0], excerpt)):
                        self.assertEqual(expected, edit.property("text"))              # every character, no marker
                        # PlainText: every tag, link and control character counts as a
                        # character (rich text would drop the markup from the length).
                        self.assertEqual(len(expected), edit.property("length"))
                        self.assertTrue(edit.property("readOnly"))
                        self.assertEqual("", edit.property("hoveredLink") or "")
        self.assertEqual(0, self.execute_calls())


class CatalogTests(UiTestCase):
    def test_h_new_strings_are_turkish(self) -> None:
        from jarvis.ui.i18n import localization, tx
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Projeler", tx("ProjectsPage", "Projects"))
        self.assertEqual("Genel", tx("Bridge", "Global"))
        self.assertEqual("Salt okunur: proje arşivlendi", tx("Composer", "Read-only: the project is archived"))
