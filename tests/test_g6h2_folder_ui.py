"""Gate 6H G6H2: the project folder link in the window (P1 popup, P3 Klasör).

A. Bridge: link / show / unlink round trip through the app's audited
   project_folder_result only; unlink leaves the folder and its contents as
   they were; the picked URL becomes the exact path shown and sent.
B. Refusals (reparse point, protected root, overlap, invalid) write nothing
   and surface as plain words (Turkish in tr-TR).
C. P3: no link is said honestly ("No folder linked"); Browse -> exact path ->
   Link; Unlink says the folder is not deleted; archived: no change control.
D. P1: a picked folder links to the new project; a refused one leaves the
   project created without a folder, says why, and lets it go on without one.
E. The linked path never reaches the model.

Temp folders only (never under the test's DB folder, which JARVIS protects);
no test opens the live DB or makes a live model, auth or network call.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from PySide6.QtCore import QUrl

from jarvis.ui.i18n import localization, tx
from tests import test_g6pj2_project_page as pj2
from tests.helpers import settled
from tests.test_project_folders_6h import junction, long_temp
from tests.test_ui import _APP, UiTestCase, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

WINDOWS = os.name == "nt"


def picked(path: Path) -> str:
    """What a QML FolderDialog hands over: a file URL."""
    return QUrl.fromLocalFile(str(path)).toString()


@unittest.skipUnless(WINDOWS, "Windows path rules")
class Case(pj2.Case):
    def setUp(self) -> None:
        super().setUp()
        self.outside = long_temp(self)                                       # not the protected DB folder
        store = self.db()
        self.project = store.create_project("Evim tadilatı")
        self.other = store.create_project("Tez")

    def folder(self, name: str) -> Path:
        path = self.outside / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def links(self) -> list:
        store = self.db()
        return [(p.name, getattr(store.load_project_folder(p.id), "path", None)) for p in (self.project, self.other)]


class BridgeTests(Case):
    def test_a_link_show_unlink_round_trip(self) -> None:
        folder = self.folder("Mutfak")
        (folder / "teklif.txt").write_text("fiyat\n", encoding="utf-8")
        bridge = self.bridge()
        path = bridge.folderPath(picked(folder))
        self.assertEqual(str(folder), path)                                  # the exact path, Windows form
        bridge.showProject(self.project.id)
        self.assertEqual({}, bridge.projectFolder)
        self.assertTrue(bridge.linkProjectFolder(self.project.id, path))
        self.assertEqual(("", path), (bridge.projectFolderNotice, bridge.projectFolder["path"]))
        self.assertEqual([("Evim tadilatı", path), ("Tez", None)], self.links())
        bridge.showProject(self.other.id)
        self.assertEqual({}, bridge.projectFolder)                           # each project shows its own
        bridge.showProject(self.project.id)
        self.assertEqual(path, bridge.projectFolder["path"])
        self.assertTrue(bridge.unlinkProjectFolder(self.project.id))
        self.assertEqual(({}, ""), (bridge.projectFolder, bridge.projectFolderNotice))
        self.assertEqual("fiyat\n", (folder / "teklif.txt").read_text(encoding="utf-8"))   # never deleted
        self.assertEqual(["teklif.txt"], os.listdir(folder))
        self.assertTrue(bridge.linkProjectFolder(self.project.id, path))     # relink

    def test_b_refusals_write_nothing_and_say_why(self) -> None:
        bridge = self.bridge()
        bridge.showProject(self.project.id)
        real = self.folder("Gerçek")
        link = self.outside / "Bağlantı"
        junction(real, link)
        protected = self.root / "veri"                                       # beside the test's DB
        protected.mkdir()
        self.assertTrue(bridge.linkProjectFolder(self.other.id, str(self.folder("Tez klasörü"))))
        cases = (
            (str(link), "This folder is a link or junction"),
            (str(self.folder("Gerçek") / ".." / "Gerçek"), "not in plain form"),
            (str(protected), "JARVIS's own data"),
            (str(self.outside / "Tez klasörü" / "alt"), "is linked to another project"),
            ("göreli\\klasör", "Pick a folder on a local drive"),
            (str(self.outside / "yok"), "The folder does not exist."),
        )
        (self.outside / "Tez klasörü" / "alt").mkdir()
        before = self.links()
        for path, said in cases:
            with self.subTest(path):
                self.assertFalse(bridge.linkProjectFolder(self.project.id, path))
                self.assertIn(said, bridge.projectFolderNotice)
                self.assertEqual(({}, before), (bridge.projectFolder, self.links()))
        self.assertTrue(bridge.linkProjectFolder(self.project.id, str(real)))
        self.assertEqual("", bridge.projectFolderNotice)                      # a success clears the reason
        self.assertFalse(bridge.linkProjectFolder(self.project.id, str(self.folder("İkinci"))))
        self.assertIn("already has a linked folder", bridge.projectFolderNotice)
        # An unknown backend message is one generic line, never shown raw (D-6).
        self.assertEqual("The folder step did not complete as expected. Check the project's folder before trying again.",
                         bridge._folder_reason("Something new."))


class PageTests(Case):
    def stage(self):
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.showProject(self.project.id)
        pump(300)
        return bridge, root

    def test_c_no_link_is_honest_then_browse_link_and_unlink(self) -> None:
        bridge, root = self.stage()
        folder = self.folder("Banyo")
        self.assertEqual("No folder linked", self.item(root, "projectFolderRowBody").property("text"))
        dialog = self.dialog(root, "projectFolderDialog")
        self.click(root, "projectFolderRow", then=lambda: self.assertTrue(dialog.property("opened")))
        self.assertEqual("folder", self.item(root, "projectsPage").property("section"))
        self.assertTrue(self.item(root, "projectFolderBrowse").isVisible())
        self.assertFalse(self.item(root, "projectFolderLink").isEnabled())    # nothing picked yet
        self.assertEqual([], self.visible(root, "projectFolderUnlink"))
        # The picker's answer: its exact path shows before Link.
        self.item(root, "projectDetail").setProperty("pickedFolder", bridge.folderPath(picked(folder)))
        pump(30)
        self.assertEqual(("Selected folder", str(folder)), (self.item(root, "folderPathLabel").property("text"),
                                                           self.item(root, "folderPath").property("text")))
        self.assertEqual([("Evim tadilatı", None), ("Tez", None)], self.links())
        self.click(root, "projectFolderLink", then=lambda: self.assertEqual(str(folder), bridge.projectFolder.get("path")))
        self.assertEqual("Linked folder", self.item(root, "folderPathLabel").property("text"))
        self.assertTrue(self.item(root, "folderUnlinkNote").isVisible())
        self.assertIn("the folder is not deleted", self.item(root, "folderUnlinkNote").property("text"))
        self.assertEqual([], self.visible(root, "projectFolderBrowse") + self.visible(root, "projectFolderLink"))
        self.click(root, "projectFolderUnlink", then=lambda: self.assertEqual({}, bridge.projectFolder))
        self.assertTrue(folder.is_dir())
        self.click(root, "folderDialogClose", then=lambda: self.assertFalse(dialog.property("visible")))
        self.assertEqual("No folder linked", self.item(root, "projectFolderRowBody").property("text"))
        # Opening the folder in Explorer is not offered (not in G6H2).
        self.assertEqual([], self.all_items(root, "projectFolderOpen"))

    def test_c_a_refusal_shows_in_the_dialog(self) -> None:
        bridge, root = self.stage()
        self.click(root, "projectFolderRow")
        self.item(root, "projectDetail").setProperty("pickedFolder", str(self.root))   # the DB's own folder
        pump(30)
        self.click(root, "projectFolderLink", then=lambda: self.assertTrue(self.item(root, "folderNotice").isVisible()))
        self.assertIn("JARVIS's own data", self.item(root, "folderNotice").property("text"))
        self.assertEqual({}, bridge.projectFolder)

    def test_c_archived_shows_the_link_and_offers_no_change(self) -> None:
        bridge, root = self.stage()
        folder = self.folder("Arşiv")
        self.assertTrue(bridge.linkProjectFolder(self.project.id, str(folder)))
        bridge.archiveProject(self.project.id)
        settled(300, lambda: self.assertFalse(bridge.openProject["active"]))
        self.assertEqual(str(folder), self.item(root, "projectFolderRowBody").property("text"))
        self.click(root, "projectFolderRow", then=lambda: self.assertTrue(self.dialog(root, "projectFolderDialog").property("opened")))
        self.assertEqual([], [n for n in ("projectFolderBrowse", "projectFolderLink", "projectFolderUnlink") if self.visible(root, n)])
        self.assertTrue(self.item(root, "folderReason").isVisible())
        # An archived project without a link has no folder row at all.
        bridge.showProject(self.other.id)
        bridge.archiveProject(self.other.id)
        settled(300, lambda: self.assertFalse(bridge.openProject["active"]))
        self.assertEqual([], self.visible(root, "projectFolderRow"))


class PopupTests(Case):
    def open_popup(self, bridge):
        root = self.window(bridge)
        bridge.showProject("")
        pump(300)
        self.click(root, "projectsNew", then=lambda: self.assertTrue(self.dialog(root, "newProjectDialog").property("opened")))
        return root, self.dialog(root, "newProjectDialog")

    def test_d_a_picked_folder_links_to_the_new_project(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        folder = self.folder("Mutfak")
        self.assertIn("without one", self.item(root, "newProjectFolderPath").property("text"))
        self.item(root, "newProjectName").setProperty("text", "Mutfak")
        dialog.setProperty("folder", bridge.folderPath(picked(folder)))
        pump(30)
        self.assertEqual(str(folder), self.item(root, "newProjectFolderPath").property("text"))   # shown before the click
        self.assertTrue(self.item(root, "newProjectFolderNote").isVisible())
        self.click(root, "newProjectCreate", then=lambda: self.assertFalse(dialog.property("visible")))
        created = next(r for r in bridge.projectRecords if r["name"] == "Mutfak")
        self.assertEqual((created["id"], str(folder)), (bridge.openProject["id"], bridge.projectFolder["path"]))

    def test_d_a_refused_folder_keeps_the_project_and_says_why(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        real = self.folder("Gerçek")
        link = self.outside / "Bağlantı"
        junction(real, link)
        self.item(root, "newProjectName").setProperty("text", "Bahçe")
        dialog.setProperty("folder", str(link))
        pump(30)
        self.click(root, "newProjectCreate", then=lambda: self.assertNotEqual("", dialog.property("createdId")))
        self.assertTrue(dialog.property("visible"))
        notice = self.item(root, "newProjectNotice").property("text")
        self.assertIn("The project was created, but the folder was not linked", notice)
        self.assertIn("link or junction", notice)
        created = next(r for r in bridge.projectRecords if r["name"] == "Bahçe")
        self.assertIsNone(self.db().load_project_folder(created["id"]))
        self.assertEqual(("Link folder", "Continue without a folder"),
                         (self.item(root, "newProjectCreate").property("label"), self.item(root, "newProjectCancel").property("label")))
        self.assertTrue(self.item(root, "newProjectName").property("readOnly"))
        # Another folder, then the project opens with it.
        dialog.setProperty("folder", str(real))
        pump(30)
        self.click(root, "newProjectCreate", then=lambda: self.assertFalse(dialog.property("visible")))
        self.assertEqual((created["id"], str(real)), (bridge.openProject["id"], bridge.projectFolder["path"]))
        self.assertEqual(1, sum(r["name"] == "Bahçe" for r in bridge.projectRecords))     # never a second project

    def test_d_continue_without_a_folder(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        self.item(root, "newProjectName").setProperty("text", "Kitap")
        dialog.setProperty("folder", str(self.root))                         # the DB's own folder
        pump(30)
        self.click(root, "newProjectCreate", then=lambda: self.assertNotEqual("", dialog.property("createdId")))
        self.click(root, "newProjectCancel", then=lambda: self.assertFalse(dialog.property("visible")))
        created = next(r for r in bridge.projectRecords if r["name"] == "Kitap")
        self.assertEqual((created["id"], {}), (bridge.openProject["id"], bridge.projectFolder))


class ModelPayloadTests(Case):
    def test_e_the_linked_path_never_reaches_the_model(self) -> None:
        folder = self.folder("Gizli Klasör G6H2")
        bridge = self.bridge(json.dumps({"kind": "chat", "reply": "Tamam."}))
        self.assertTrue(bridge.linkProjectFolder(self.project.id, str(folder)))
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        self.assertTrue(bridge.submitText("Mutfak için bir plan yaz"))
        settled(600, lambda: self.assertEqual(1, len(self.model.calls)))
        sent = repr(self.model.calls).casefold()
        self.assertIn("mutfak için bir plan yaz", sent)                      # the dump does carry the payload
        for secret in (str(folder), str(folder).replace("\\", "\\\\"), str(folder).replace("\\", "/"), folder.name):
            self.assertNotIn(secret.casefold(), sent)


class CatalogTests(UiTestCase):
    def test_f_new_strings_are_turkish(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual(("Bağlı klasör yok", "Klasöre bağla", "Göz at…"),
                         (tx("ProjectPage", "No folder linked"), tx("ProjectPage", "Link a folder"), tx("ProjectPage", "Browse…")))
        self.assertIn("klasör silinmez", tx("ProjectPage", "Unlinking only ends the link to this project: the folder is not deleted and nothing in it changes."))
        self.assertEqual(("Klasöre bağla", "Göz at…"), (tx("NewProjectDialog", "Link a folder"), tx("NewProjectDialog", "Browse…")))
        self.assertNotEqual("", tx("Bridge", "This project already has a linked folder. Unlink it first."))
        for source in ("A whole drive cannot be a project folder.", "The folder does not exist.",
                       "That folder, or a folder inside or around it, is linked to another project."):
            with self.subTest(source):
                self.assertNotEqual(source, tx("Bridge", source))


if __name__ == "__main__":
    unittest.main()
