"""V2.1a M4 (MEMUIPROJ): the project page's Memory row and its dialog.

The row says how many approved + pending records the project has; it opens
ProjectMemoryDialog, which lists them grouped by kind with provenance, and
whose Undo / Approve only ask the app through the bridge slots (Core is the
authority). Records are seeded through the real app over the TEMP database.
No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import json

from jarvis.conversation import append_turn
from jarvis.ui.i18n import localization, tx
from tests.helpers import settled
from tests.test_g6pj2_project_page import Case
from tests.test_ui import _APP, UiTestCase, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401


class ProjectMemoryTests(Case):
    def remember(self, app, conversation_id: str, text: str, kind: str) -> str:
        created = []

        def change(state):
            state, turn = append_turn(state, role="user", kind="user", text=text, message=text)
            created.append(turn)
            return state

        app.memory.update_conversation_state(conversation_id, change)
        return app.remember_message(conversation_id, created[0].id, kind)["record"]["record_id"]

    def test_row_counts_and_the_dialog_lists_undoes_and_approves(self) -> None:
        app = self.factory(self.store.load().settings)      # the same TEMP database
        project = app.memory.create_project("Evim tadilatı").id
        conversation = app.memory.create_conversation(project).id
        fact = self.remember(app, conversation, "Fayans Ege Seramik'ten", "fact")
        decision = self.remember(app, conversation, "Mutfak tezgahı granit olacak", "decision")
        bridge = self.bridge(json.dumps({"kind": "chat", "reply": "Tamam."}))
        root = self.window(bridge)
        bridge.showProject(project)
        pump(400)
        settled(300, lambda: self.assertEqual("2 records  ·  1 pending",
                                              self.item(root, "projectMemoryRowBody").property("text")))
        self.press(root, self.visible(root, "projectMemoryRow")[0])
        self.assertEqual("memory", self.item(root, "projectsPage").property("section"))
        self.assertTrue(self.dialog(root, "projectMemoryDialog").property("visible"))
        texts = [t.property("text") for t in self.visible(root, "projectMemoryText")]
        self.assertEqual(["Mutfak tezgahı granit olacak", "Fayans Ege Seramik'ten"], texts)   # grouped: decisions before facts
        self.assertEqual(["DECISIONS", "FACTS"], [t.property("text") for t in self.visible(root, "projectMemoryGroupTitle")])
        self.assertTrue(all(p.property("text").startswith("You  ·  ") for p in self.visible(root, "projectMemoryProvenance")))
        self.assertEqual(1, len(self.visible(root, "projectMemoryPending")))
        self.assertEqual([], self.visible(root, "projectMemoryStale") + self.visible(root, "projectMemoryConflict"))
        # Undo (the fact) and Approve (the pending decision) ask the app; it decides.
        self.press(root, self.visible(root, "projectMemoryUndo")[0])
        settled(300, lambda: self.assertEqual("retracted", app.memory.current_status(fact).value))
        settled(300, lambda: self.assertEqual(["Mutfak tezgahı granit olacak"],
                                              [t.property("text") for t in self.visible(root, "projectMemoryText")]))
        self.press(root, self.visible(root, "projectMemoryApprove")[0])
        settled(300, lambda: self.assertEqual("approved", app.memory.current_status(decision).value))
        settled(300, lambda: self.assertEqual("1 record", self.item(root, "projectMemoryRowBody").property("text")))
        self.assertEqual([], self.visible(root, "projectMemoryApprove") + self.visible(root, "projectMemoryUndo"))
        self.click(root, "projectMemoryClose")
        self.assertFalse(self.dialog(root, "projectMemoryDialog").property("visible"))
        self.assertEqual("conversations", self.item(root, "projectsPage").property("section"))


class CatalogTests(UiTestCase):
    def test_new_strings_are_turkish(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual(("Hafıza", "Yönet", "Henüz hafıza yok"),
                         (tx("ProjectPage", "Memory"), tx("ProjectPage", "Manage"), tx("ProjectPage", "No memory yet")))
        self.assertEqual("3 kayıt", tx("ProjectPage", "%1 records", 3))
        self.assertEqual(("Onayla", "Reddet", "Geri Al", "Bekliyor"),
                         tuple(tx("ProjectMemoryDialog", s) for s in ("Approve", "Reject", "Undo", "Pending")))
        self.assertEqual(("Kararlar", "Olgular"), (tx("ProjectMemoryDialog", "Decisions"), tx("ProjectMemoryDialog", "Facts")))
