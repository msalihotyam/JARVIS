"""Gate 6G G6W3 (W1/W3): the project chat's work panel, first version.

A. Bridge: {} in a plain chat; in a project chat only stored facts - the
   instruction preview and its length, the active files, the linked folder's
   NAME (never its path), the capabilities this chat's results recorded.
B. Window: hidden in a plain chat, shown in a project chat; each section only
   with data; the folder shows its name only; collapse persists (a sibling
   preferences file, the main document's key set unchanged).
C. Nothing of the panel reaches the model; the panel only reads.

Temp folders and the test's own DB only; no live model, auth or network call.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

from PySide6.QtCore import QPointF

from jarvis.project_files import read_project_file
from jarvis.ui.i18n import localization, tx
from jarvis.ui.preferences import UiPreferences, canonical_preferences
from tests import test_g6pj2_project_page as pj2
from tests.helpers import settled
from tests.test_project_folders_6h import long_temp
from tests.test_ui import _APP, UiTestCase, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

QML = Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "qml" / "components" / "WorkPanel.qml"
INSTRUCTION = "Mutfak bütçesi 50 bin TL.\n\nKaynak göster."


@unittest.skipUnless(os.name == "nt", "the 6H folder link is Windows only")
class Case(pj2.Case):
    def setUp(self) -> None:
        super().setUp()
        self.outside = long_temp(self)
        store = self.db()
        self.project = store.create_project("Evim tadilatı")
        self.folder = self.outside / "Gizli Klasör G6W3"
        self.folder.mkdir()

    def fill(self, bridge) -> None:
        store = self.db()
        store.save_explicit_user_project_instruction(self.project.id, INSTRUCTION, "internal")
        for name in ("teklif.txt", "olcu.txt", "eski.txt"):
            (self.outside / name).write_text(f"{name}\n", encoding="utf-8")
            row = store.save_explicit_user_project_file(self.project.id, read_project_file(self.outside / name), "internal")
            if name == "eski.txt":
                store.set_project_file_active(self.project.id, row["id"], False)       # retired: not in use
        self.assertTrue(bridge.linkProjectFolder(self.project.id, str(self.folder)))

    def result(self, bridge, task: str, capability: str) -> None:
        bridge._say("result", "Bulundu.", task_id=task, run_status="completed", capability=capability, destination="")


class BridgeTests(Case):
    def test_a_plain_chat_has_no_panel(self) -> None:
        bridge = self.bridge()
        self.assertTrue(bridge.newConversation())
        self.assertEqual({}, bridge.workPanel)

    def test_a_an_empty_project_chat_says_nothing_it_does_not_have(self) -> None:
        bridge = self.bridge()
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        self.assertEqual({"instruction": "", "instruction_chars": 0, "files": [], "folder": ""}, bridge.workPanel)
        self.assertEqual([], bridge.workPanelCapabilities)

    def test_a_stored_facts_and_the_folder_name_only(self) -> None:
        bridge = self.bridge()
        self.fill(bridge)
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        self.assertEqual({"instruction": "Mutfak bütçesi 50 bin TL. Kaynak göster.", "instruction_chars": len(INSTRUCTION),
                          "files": ["teklif.txt", "olcu.txt"], "folder": "Gizli Klasör G6W3"}, bridge.workPanel)
        self.assertNotIn(str(self.outside).casefold(), repr(bridge.workPanel).casefold())
        # A cleared instruction is no instruction; an unlinked folder is none.
        self.db().save_explicit_user_project_instruction(self.project.id, "", "internal")
        self.assertTrue(bridge.unlinkProjectFolder(self.project.id))
        self.assertEqual(("", 0, ""), tuple(bridge.workPanel[k] for k in ("instruction", "instruction_chars", "folder")))

    def test_a_capabilities_are_the_recorded_results_of_this_chat(self) -> None:
        bridge = self.bridge()
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        self.result(bridge, "t-1", "web_research")
        self.result(bridge, "t-2", "file_read")
        self.result(bridge, "t-3", "web_research")
        self.assertEqual(["web_research", "file_read"], bridge.workPanelCapabilities)
        first = bridge.conversationId
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        self.assertEqual([], bridge.workPanelCapabilities)                  # another chat: its own record
        self.assertTrue(bridge.openConversation(first))
        self.assertEqual(["web_research", "file_read"], bridge.workPanelCapabilities)   # read back from the store

    def test_a_collapse_persists_in_its_own_file(self) -> None:
        bridge = self.bridge()
        self.assertFalse(bridge.workPanelCollapsed)
        bridge.setTheme("dark")
        bridge.setWorkPanelCollapsed(True)
        self.assertTrue(bridge.workPanelCollapsed)
        self.assertTrue(UiPreferences(self.prefs_path).work_panel_collapsed)
        main = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.assertEqual(set(canonical_preferences()), set(main))           # rollback reads the main document whole
        bridge.setWorkPanelCollapsed(False)
        self.assertFalse(UiPreferences(self.prefs_path).work_panel_collapsed)
        self.prefs_path.with_suffix(".work-panel.json").write_text('{"collapsed": "yes"}', encoding="utf-8")
        self.assertFalse(UiPreferences(self.prefs_path).work_panel_collapsed)   # malformed: open


class WindowTests(Case):
    def stage(self, fill: bool):
        bridge = self.bridge()
        if fill:
            self.fill(bridge)
        root = self.window(bridge)
        return bridge, root

    def shown(self, root) -> list[str]:
        names = ("workPanelInstruction", "workPanelFiles", "workPanelFolder", "workPanelCapabilities", "workPanelNothing")
        return [n for n in names if self.visible(root, n)]

    def test_b_hidden_in_a_plain_chat_shown_in_a_project_chat(self) -> None:
        bridge, root = self.stage(fill=True)
        self.assertTrue(bridge.newConversation())
        pump(200)
        self.assertEqual([], self.visible(root, "workPanel"))
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        pump(200)
        self.assertEqual(1, len(self.visible(root, "workPanel")))
        self.assertTrue(bridge.newConversation())
        pump(200)
        self.assertEqual([], self.visible(root, "workPanel"))

    def test_b_each_section_only_with_data(self) -> None:
        bridge, root = self.stage(fill=False)
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        pump(200)
        self.assertEqual(["workPanelNothing"], self.shown(root))
        self.fill(bridge)
        pump(200)
        self.assertEqual(["workPanelInstruction", "workPanelFiles", "workPanelFolder"], self.shown(root))
        self.assertEqual(f"{len(INSTRUCTION)}/2000", self.under(self.item(root, "workPanelInstruction"), "workPanelCount")[0].property("text"))
        self.assertEqual(["teklif.txt", "olcu.txt"], [i.property("text") for i in self.visible(root, "workPanelFile")])
        self.assertEqual("Gizli Klasör G6W3", self.item(root, "workPanelFolderName").property("text"))
        texts, todo = [], [self.item(root, "workPanel")]
        while todo:
            node = todo.pop()
            texts.append(str(node.property("text") or ""))
            todo.extend(node.childItems())
        texts = " ".join(texts)
        self.assertIn("Gizli Klasör G6W3", texts)
        self.assertNotIn(str(self.outside).casefold(), texts.casefold())    # the path is never in the panel
        self.result(bridge, "t-1", "web_research")
        pump(200)
        self.assertEqual(["workPanelInstruction", "workPanelFiles", "workPanelFolder", "workPanelCapabilities"], self.shown(root))
        line = self.visible(root, "workPanelCapability")
        self.assertEqual(["web_research"], [i.property("text") for i in line])
        panel = self.item(root, "workPanel")                                # the last line fits inside the panel
        self.assertLessEqual(line[0].mapToItem(panel, QPointF(0, line[0].height())).y(), panel.height())

    def test_b_collapse_from_its_button_and_it_stays(self) -> None:
        bridge, root = self.stage(fill=True)
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        pump(200)
        feed = self.item(root, "conversationFeed")
        narrow = feed.width()
        self.click(root, "workPanelToggle", then=lambda: self.assertTrue(bridge.workPanelCollapsed))
        settled(600, lambda: self.assertEqual(40, self.item(root, "workPanel").width()))
        self.assertEqual([], self.shown(root))
        self.assertGreater(feed.width(), narrow)                            # the stream takes the room
        self.assertTrue(UiPreferences(self.prefs_path).work_panel_collapsed)
        self.click(root, "workPanelToggle", then=lambda: self.assertFalse(bridge.workPanelCollapsed))
        settled(600, lambda: self.assertEqual(["workPanelInstruction", "workPanelFiles", "workPanelFolder"], self.shown(root)))


class ModelPayloadTests(Case):
    def test_c_the_panel_only_reads(self) -> None:
        bridge = self.bridge(json.dumps({"kind": "chat", "reply": "Tamam."}))
        self.fill(bridge)
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        self.assertEqual("Gizli Klasör G6W3", bridge.workPanel["folder"])
        bridge.setWorkPanelCollapsed(True)
        self.assertTrue(bridge.submitText("Mutfak için bir plan yaz"))
        settled(600, lambda: self.assertEqual(1, len(self.model.calls)))
        sent = repr(self.model.calls).casefold()
        self.assertIn("mutfak için bir plan yaz", sent)
        self.assertNotIn("gizli klasör g6w3", sent)
        # The panel's only bridge call is its own collapse preference.
        self.assertEqual({"setWorkPanelCollapsed"}, set(re.findall(r"bridge\.(\w+)\s*\(", QML.read_text(encoding="utf-8"))))


class CatalogTests(UiTestCase):
    def test_d_new_strings_are_turkish(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual(("BU SOHBETTE", "Bağlı klasör"), (tx("WorkPanel", "IN THIS CHAT"), tx("WorkPanel", "Linked folder")))
        for source in ("Project instruction", "Project files", "Used in this chat", "Hide this panel",
                       "Show what this chat works with",
                       "This project has no instruction, active files or linked folder yet, and this chat has used no capability."):
            with self.subTest(source):
                self.assertNotEqual(source, tx("WorkPanel", source))


if __name__ == "__main__":
    unittest.main()
