"""G6H3POL: Gate 6H follow-ups. D-6: a project folder refusal reaches the
window as a stable English reason sentence and is shown in the interface
language. F-2: K3 "Choose an existing folder…" with a refused folder decides
nothing (the accepted pick ends REJECTED: tests/test_project_folder_k3.py)."""

from __future__ import annotations

import ast
import inspect
import unittest

from PySide6.QtCore import Q_ARG, QMetaObject

from jarvis import project_folders
from jarvis.ui.bridge import Bridge
from jarvis.ui.i18n import localization, tx
from tests import test_project_folder_k3 as k3
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import _APP, pump

#: Programming errors the window never sends: shown as returned.
DEVELOPER_ONLY = ("on_desktop must be a bool", "Unknown known folder: ")
#: The store's refusals on the same link path (memory.link_explicit_user_project_folder).
STORE_REFUSALS = ("The project already has a linked folder; unlink it first.",
                  "That folder, or one inside or around it, is linked to another project.",
                  "The created folder could not be proven to be the approved one.")


def raised(module) -> list[str]:
    """The text of every ``raise ValueError(...)`` (an f-string's leading part)."""
    found = []
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                and getattr(node.exc.func, "id", "") == "ValueError" and node.exc.args):
            text = node.exc.args[0]
            if isinstance(text, ast.JoinedStr):
                text = text.values[0]
            found.append(text.value)
    return found


def words(message: str) -> str | None:
    return Bridge._FOLDER_REASONS.get(message) or next(
        (said for prefix, said in Bridge._FOLDER_REASON_PREFIXES if message.startswith(prefix)), None)


class ReasonTests(unittest.TestCase):
    def test_d6_every_folder_refusal_has_words_in_both_languages(self) -> None:
        messages = [m for m in raised(project_folders) if m not in DEVELOPER_ONLY] + list(STORE_REFUSALS)
        self.assertGreater(len(messages), 25)
        self.assertNotIn("bulunamadı", " ".join(messages))
        localization(_APP).apply("tr-TR")
        self.addCleanup(localization(_APP).apply, "en-US")
        for message in messages:
            with self.subTest(message):
                said = words(message)
                self.assertIsNotNone(said)
                self.assertNotEqual(said, tx("Bridge", said))          # a Turkish line exists


@unittest.skipUnless(k3.h6.WINDOWS, "Windows folder creation")
class PickExistingTests(k3.h1b.Case):
    factory, pending, card = k3.CardTests.factory, k3.CardTests.pending, k3.CardTests.card

    def test_f2_a_refused_pick_decides_nothing(self) -> None:
        bridge = self.pending()
        old = bridge._ticket
        root, card = self.card(bridge)
        taken = self.folder("Başkasının")
        self.assertTrue(bridge.linkProjectFolder(self.other.id, str(taken)))
        for picked, said in ((self.documents / "Yok", "The folder does not exist."),
                             # The store's own refusal, asked before the request is rejected.
                             (taken, "That folder, or a folder inside or around it, is linked to another project.")):
            with self.subTest(said):
                QMetaObject.invokeMethod(card, "bindPicker")
                QMetaObject.invokeMethod(card, "linkPicked", Q_ARG("QVariant", str(picked)))
                pump(30)
                store = self.db()
                self.assertEqual(said, bridge.projectFolderNotice)
                self.assertIsNone(store.load_project_folder(self.project.id))
                self.assertEqual("awaiting_approval", store.load_task(old.task_id).status.value)   # the card stays
                self.assertEqual((old.request_id, True), (bridge._ticket.request_id, bridge.approvalPending))
        self.assertEqual(["Başkasının"], self.made())


if __name__ == "__main__":
    unittest.main()
