"""Gate 6H G6H1b (K1): "Create a folder" in the new-project popup (C11).

The popup shows the exact path Core proposes before the click; an empty name
offers no creation; an answer for older inputs is discarded; no folder choice
creates no directory but still the project; "Browse…" keeps G6H2's behaviour;
the click sends exactly the shown path with the id from the create's own
answer, once; after a refusal or a partial failure the popup ends for THAT
project with the reason and only "OK".

Desktop and Documents are temp folders (the shell lookup is patched); no test
opens the live DB or makes a live model, auth or network call.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QMetaObject, Qt
from PySide6.QtTest import QTest

from jarvis import project_folders
from jarvis.ui.i18n import localization, tx
from tests import test_g6h2_folder_ui as h2
from tests.helpers import settled
from tests.test_ui import _APP, UiTestCase, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

JP = project_folders.DEFAULT_PARENT_NAME


class Case(h2.Case):
    def setUp(self) -> None:
        super().setUp()
        self.desktop, self.documents = self.folder("Desktop"), self.folder("Documents")
        known = {project_folders._KNOWN_FOLDERS["desktop"]: str(self.desktop),
                 project_folders._KNOWN_FOLDERS["documents"]: str(self.documents)}
        patcher = mock.patch.object(project_folders, "_shell_known_folder", side_effect=known.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_popup(self, bridge):
        return h2.PopupTests.open_popup(self, bridge)

    def made(self) -> list[str]:
        return sorted(str(Path(d).relative_to(self.outside)) for d, _, _ in os.walk(self.outside)
                      if Path(d) not in (self.outside, self.desktop, self.documents))

    def choose_create(self, root, dialog, name: str) -> None:
        self.item(root, "newProjectName").setProperty("text", name)
        self.click(root, "newProjectFolderCreate", then=lambda: self.assertTrue(dialog.property("makeFolder")))

    def shown(self, root) -> str:
        return self.item(root, "newProjectFolderPath").property("text")

    def spy_create(self, bridge) -> list[tuple]:
        calls, real = [], bridge.backend.project_folder_create

        def spy(project_id, path, on_desktop):
            calls.append((project_id, path, on_desktop))
            return real(project_id, path, on_desktop)

        patcher = mock.patch.object(bridge.backend, "project_folder_create", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def named(self, bridge, name: str) -> list[dict]:
        return [r for r in bridge.projectRecords if r["name"] == name]


class CreateFolderPopupTests(Case):
    def test_c11_the_preview_shows_before_the_click_and_the_click_sends_it(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        calls = self.spy_create(bridge)
        self.choose_create(root, dialog, "Mutfak")
        documents_path = str(self.documents / JP / "Mutfak")
        settled(200, lambda: self.assertEqual(documents_path, self.shown(root)))
        self.assertIn("creates exactly this folder", self.item(root, "newProjectFolderNote").property("text"))
        self.assertTrue(self.item(root, "newProjectCreate").isEnabled())
        self.click(root, "newProjectOnDesktop", then=lambda: self.assertTrue(dialog.property("onDesktop")))
        shown = str(self.desktop / "Mutfak")
        settled(200, lambda: self.assertEqual(shown, self.shown(root)))
        self.assertEqual([], self.made())                                       # nothing exists before the click
        self.click(root, "newProjectCreate", then=lambda: self.assertFalse(dialog.property("visible")))
        created = self.named(bridge, "Mutfak")
        self.assertEqual(1, len(created))
        self.assertEqual([(created[0]["id"], shown, True)], calls)              # the shown path, that create's id
        self.assertEqual((created[0]["id"], shown), (bridge.openProject["id"], bridge.projectFolder["path"]))
        self.assertEqual("created", self.db().load_project_folder(created[0]["id"]).origin)
        self.assertEqual(["Desktop\\Mutfak"], self.made())

    def test_c11_an_empty_name_offers_no_creation(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        self.choose_create(root, dialog, "")
        self.assertIn("Type a name", self.shown(root))
        self.assertFalse(self.item(root, "newProjectCreate").isEnabled())
        self.assertFalse(dialog.property("previewReady"))

    def test_c11_an_answer_for_older_inputs_is_discarded(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        bridge.sync = False                                                     # answers come from the worker
        self.addCleanup(setattr, bridge, "sync", True)
        self.choose_create(root, dialog, "Eski")
        settled(400, lambda: self.assertEqual(str(self.documents / JP / "Eski"), self.shown(root)))
        old = dialog.property("previewGeneration")
        self.item(root, "newProjectName").setProperty("text", "Yeni")
        self.assertFalse(self.item(root, "newProjectCreate").isEnabled())       # pending: no creation
        bridge.projectFolderPreviewed.emit(old, str(self.documents / JP / "Eski"), "")   # late, for "Eski"
        pump(30)
        self.assertNotIn("Eski", self.shown(root))
        settled(400, lambda: self.assertEqual(str(self.documents / JP / "Yeni"), self.shown(root)))
        self.assertTrue(self.item(root, "newProjectCreate").isEnabled())
        bridge.projectFolderPreviewed.emit(old, "C:\\stale", "")
        pump(30)
        self.assertEqual(str(self.documents / JP / "Yeni"), self.shown(root))

    def test_c11_no_folder_choice_creates_the_project_and_no_directory(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        calls = self.spy_create(bridge)
        self.choose_create(root, dialog, "Kitap")
        self.click(root, "newProjectFolderClear", then=lambda: self.assertFalse(dialog.property("makeFolder")))
        self.assertIn("without one", self.shown(root))
        self.click(root, "newProjectCreate", then=lambda: self.assertFalse(dialog.property("visible")))
        self.assertEqual(1, len(self.named(bridge, "Kitap")))
        self.assertEqual(([], [], {}), (calls, self.made(), bridge.projectFolder))

    def test_c11_browse_keeps_the_g6h2_link(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        calls = self.spy_create(bridge)
        existing = self.folder("Var olan")
        picker = self.dialog(root, "newProjectFolderPicker")

        def pick() -> None:
            # The picker's own accept runs (it ends "Create a folder"); its native
            # selection cannot be set unopened, so the shown path is set as G6H2's tests do.
            QMetaObject.invokeMethod(picker, "accepted")
            dialog.setProperty("folder", bridge.folderPath(h2.picked(existing)))
            pump(30)

        self.choose_create(root, dialog, "Bahçe")
        pick()
        self.assertEqual((False, str(existing)), (dialog.property("makeFolder"), self.shown(root)))
        self.click(root, "newProjectFolderCreate", then=lambda: self.assertEqual("", dialog.property("folder")))
        pick()
        self.assertFalse(dialog.property("makeFolder"))
        self.click(root, "newProjectCreate", then=lambda: self.assertFalse(dialog.property("visible")))
        created = self.named(bridge, "Bahçe")[0]
        link = self.db().load_project_folder(created["id"])
        self.assertEqual((str(existing), "picked", []), (link.path, link.origin, calls))
        self.assertEqual(["Var olan"], self.made())

    def test_c11_a_double_click_submits_once(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        self.choose_create(root, dialog, "Tek")
        settled(200, lambda: self.assertTrue(dialog.property("previewReady")))
        creates, real = [], bridge.backend.project_create
        bridge.sync = False
        self.addCleanup(setattr, bridge, "sync", True)
        with mock.patch.object(bridge.backend, "project_create", side_effect=lambda n: creates.append(n) or real(n)):
            QMetaObject.invokeMethod(dialog, "create")
            QMetaObject.invokeMethod(dialog, "create")
            self.assertTrue(dialog.property("waiting"))
            self.assertFalse(self.item(root, "newProjectCreate").isEnabled())
            QTest.keyClick(root, Qt.Key_Escape)                                 # open until Core's answer
            self.assertTrue(dialog.property("visible"))
            settled(600, lambda: self.assertFalse(dialog.property("visible")))
        self.assertEqual(["Tek"], creates)
        self.assertEqual(1, len(self.named(bridge, "Tek")))
        self.assertEqual(["Documents\\" + JP, "Documents\\" + JP + "\\Tek"], self.made())

    def final_state(self, root, dialog, bridge, name: str) -> str:
        """The popup ended for that project: its reason, only "OK"."""
        self.assertTrue(dialog.property("visible"))
        self.assertNotEqual("", dialog.property("finalId"))
        self.assertEqual([], self.visible(root, "newProjectCancel"))
        self.assertEqual("OK", self.item(root, "newProjectCreate").property("label"))
        self.assertTrue(self.item(root, "newProjectCreate").isEnabled())
        self.assertTrue(self.item(root, "newProjectName").property("readOnly"))
        self.assertFalse(self.item(root, "newProjectFolderCreate").isEnabled())
        notice = self.item(root, "newProjectNotice").property("text")
        QTest.keyClick(root, Qt.Key_Escape)                                     # only "OK" ends it
        pump(30)
        self.assertTrue(dialog.property("visible"))
        self.click(root, "newProjectCreate", then=lambda: self.assertFalse(dialog.property("visible")))
        created = self.named(bridge, name)
        self.assertEqual(1, len(created))                                       # never a second project
        self.assertEqual((created[0]["id"], {}), (bridge.openProject["id"], bridge.projectFolder))
        self.assertIsNone(self.db().load_project_folder(created[0]["id"]))
        return notice

    def test_c11_a_refusal_ends_in_the_final_state(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        self.choose_create(root, dialog, "Banyo")
        self.click(root, "newProjectOnDesktop", then=lambda: self.assertTrue(dialog.property("onDesktop")))
        settled(200, lambda: self.assertTrue(dialog.property("previewReady")))
        (self.desktop / "Banyo").mkdir()                                        # someone took the name: the path changed
        self.click(root, "newProjectCreate", then=lambda: self.assertNotEqual("", dialog.property("finalId")))
        notice = self.final_state(root, dialog, bridge, "Banyo")
        self.assertIn("The project was created, but its folder was not", notice)
        self.assertIn("The folder path changed. Confirm the new path.", notice)
        self.assertEqual(["Desktop\\Banyo"], self.made())                       # only the other one's

    def test_c11_a_partial_failure_names_what_was_kept(self) -> None:
        bridge = self.bridge()
        root, dialog = self.open_popup(bridge)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TRIGGER fail_link BEFORE INSERT ON project_folder_links "
                               "BEGIN SELECT RAISE(ABORT, 'injected'); END")
            connection.commit()
        self.choose_create(root, dialog, "Salon")
        settled(200, lambda: self.assertTrue(dialog.property("previewReady")))
        self.click(root, "newProjectCreate", then=lambda: self.assertNotEqual("", dialog.property("finalId")))
        notice = self.final_state(root, dialog, bridge, "Salon")
        self.assertIn("no folder was deleted", notice)
        self.assertIn("Project link: not linked.", notice)
        for path in (self.documents / JP, self.documents / JP / "Salon"):
            self.assertIn(str(path), notice)
            self.assertTrue(path.is_dir())


class CatalogTests(UiTestCase):
    def test_c11_new_strings_are_turkish(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual(("Klasör oluştur", "Masaüstünde", "Tamam"),
                         (tx("NewProjectDialog", "Create a folder"), tx("NewProjectDialog", "On the Desktop"),
                          tx("NewProjectDialog", "OK")))
        self.assertIn("%1", tx("NewProjectDialog", "The project was created, but its folder was not: %1"))
        self.assertEqual("Proje oluşturulamadı.", tx("Bridge", "The project could not be created."))


if __name__ == "__main__":
    unittest.main()
