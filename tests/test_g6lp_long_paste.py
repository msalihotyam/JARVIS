"""Gate 6G G6LP: long paste (build/6g/PLAN_DRAFT.txt "LONG PASTE").

* LP1 A very long paste does not fill the draft: it becomes a pasted-text card
      above the bar ("PASTED", its first lines faint, a pencil). Several long
      pastes are several cards. Typing never does this, only a paste.
* LP2 The pencil puts the text back into the draft at the caret; the card goes.
* LP3 One threshold: >= 2,000 characters OR >= 30 lines; shorter is as before.
* LP4 On send it travels as message-level context (the attachment path, apart
      from the typed request); the sent message shows the same card, which
      opens a read-only preview. Clipboard files and images still attach first.

No model, network or live DB: the scripted model and the bridge's clipboard seam.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import tests.test_ui as ui
from tests.test_ui import _APP, pump  # noqa: F401 - _APP: the application
from tests.helpers import settled

from PySide6.QtCore import QMetaObject, QMimeData, QObject, QUrl, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

LONG = "Uzun metin satırı, yapıştırıldı.\n" * 80          # 2,640 characters, 80 lines


class LongPasteCase(ui.SurfaceCase):
    def clipboard(self, bridge, text="", files=()):
        """The same content on the real clipboard (an editor's text paste) and
        behind the bridge's seam (the attachment decision)."""
        def mime():
            data = QMimeData()
            if text:
                data.setText(text)
            if files:
                data.setUrls([QUrl.fromLocalFile(str(path)) for path in files])
            return data
        QGuiApplication.clipboard().setMimeData(mime())
        self.addCleanup(QGuiApplication.clipboard().clear)
        data = mime()
        bridge.clipboard_data = lambda: data

    def paste(self, root, bridge, text="", files=()):
        """Paste through the composer's own edit menu - the mouse route."""
        self.clipboard(bridge, text, files)
        menu = root.findChild(QObject, "composerEditMenu")
        QMetaObject.invokeMethod(menu, "popup")
        pump(200)
        QMetaObject.invokeMethod(menu.findChild(QObject, "editPaste"), "triggered")
        QMetaObject.invokeMethod(menu, "close")
        pump(250)

    def pasted(self, bridge):
        return [a for a in bridge.globalAttachments if a.get("pasted")]

    def shown(self, root, name):
        return [i for i in self.all_items(root) if i.objectName() == name and i.isVisible()]


class ThresholdTests(LongPasteCase):
    def test_characters_and_lines_each_cross_the_threshold_at_exactly_the_boundary(self) -> None:
        cases = [("x" * 1999, False), ("x" * 2000, True),
                 ("\n".join(["satır"] * 29), False), ("\n".join(["satır"] * 30), True),
                 ("\n".join(["satır"] * 29) + "\n", False)]       # a final line break is not a 30th line
        for text, card in cases:
            with self.subTest(chars=len(text), lines=text.count("\n") + 1, card=card):
                bridge = self.bridge()
                root = self.window(bridge)
                editor = self.item(root, "composerInput")
                editor.forceActiveFocus()
                self.paste(root, bridge, text)
                if card:
                    self.assertEqual(("", 1), (editor.property("text"), len(self.pasted(bridge))))
                    self.assertEqual("pasted-text.txt", self.pasted(bridge)[0]["name"])
                else:
                    self.assertEqual((text, []), (editor.property("text"), bridge.globalAttachments))

    def test_typing_never_becomes_a_card(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        editor = self.item(root, "composerInput")
        editor.forceActiveFocus()
        pump(50)
        for _ in range(2100):
            QTest.keyClick(root, "y")
        for _ in range(31):
            QTest.keyClick(root, Qt.Key_Return, Qt.ShiftModifier)
        pump(100)
        self.assertEqual(2100 + 31, len(editor.property("text")))
        self.assertEqual([], bridge.globalAttachments)

    def test_a_provider_without_text_files_keeps_the_old_paste(self) -> None:
        from unittest.mock import patch
        import jarvis.ui.bridge as bridge_module
        bridge = self.bridge()
        root = self.window(bridge)
        editor = self.item(root, "composerInput")
        editor.forceActiveFocus()
        with patch.dict(bridge_module.ATTACHMENT_SUPPORT, {"claude": {"image": True, "text": False}}):
            self.paste(root, bridge, LONG)
        self.assertEqual((LONG, []), (editor.property("text"), bridge.globalAttachments))


class CardTests(LongPasteCase):
    def test_several_long_pastes_are_several_cards_above_the_bar(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        editor = self.item(root, "composerInput")
        editor.forceActiveFocus()
        self.paste(root, bridge, LONG)
        self.paste(root, bridge, "İkinci yapıştırma.\n" * 40)
        first, second = self.pasted(bridge)
        self.assertEqual("", editor.property("text"))
        for item, head in ((first, "Uzun metin satırı"), (second, "İkinci yapıştırma.")):
            self.assertEqual(1, len(self.shown(root, "pasteBody_" + item["id"])))
            self.assertEqual("PASTED", self.item(root, "pasteLabel_" + item["id"]).property("text"))
            self.assertTrue(self.item(root, "pasteExcerpt_" + item["id"]).property("text").startswith(head))
            self.assertEqual(1, len(self.shown(root, "pasteEdit_" + item["id"])))
        bar = self.item(root, "composerBar")
        strip = self.item(root, "composerContext")
        self.assertLessEqual(strip.y() + strip.height(), bar.y())

    def test_the_pencil_puts_the_text_back_at_the_caret_and_the_card_goes(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        editor = self.item(root, "composerInput")
        editor.setProperty("text", "Başla SON")
        editor.forceActiveFocus()
        editor.setProperty("cursorPosition", 6)
        raw = "  girintili\n" + LONG                        # exactly as pasted, leading spaces included
        self.paste(root, bridge, raw)
        (item,) = self.pasted(bridge)
        self.assertEqual("Başla SON", editor.property("text"))
        self.click(root, "pasteEdit_" + item["id"])
        settled(300, lambda: (
            self.assertEqual("Başla " + raw + "SON", editor.property("text")),
            self.assertEqual([], bridge.globalAttachments),
            self.assertEqual([], self.shown(root, "pasteBody_" + item["id"])),
        ))


class SendTests(LongPasteCase):
    def test_the_paste_travels_as_context_apart_from_the_request_and_the_sent_card_previews_read_only(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        editor = self.item(root, "composerInput")
        editor.forceActiveFocus()
        self.paste(root, bridge, LONG)
        (item,) = self.pasted(bridge)
        editor.setProperty("text", "Bunu özetle.")
        pump(80)
        self.click(root, "composerSend")
        settled(400, lambda: (
            self.assertEqual(1, len(self.model.calls)),
        ))
        call = self.model.calls[-1]
        (sent,) = call.context["global_attachments"]
        self.assertEqual(("file", "pasted-text.txt"), (sent["kind"], sent["name"]))
        self.assertTrue(sent["file_text"].startswith("Uzun metin satırı, yapıştırıldı."))
        self.assertIn("Bunu özetle.", call.objective)
        self.assertNotIn("yapıştırıldı", call.objective)
        self.assertEqual([], bridge.globalAttachments)
        # The sent message: the same card, no pencil; a click opens the read-only preview.
        settled(300, lambda: (
            self.assertEqual(1, len(self.shown(root, "pasteBody_" + item["id"]))),
            self.assertEqual([], self.shown(root, "pasteEdit_" + item["id"])),
        ))
        self.click(root, "attachmentOpen_" + item["id"])
        preview = self.item(root, "attachmentPreview")
        text = self.item(root, "attachmentPreviewText")
        settled(300, lambda: (
            self.assertTrue(preview.property("open")),
            self.assertTrue(text.property("readOnly")),
            self.assertTrue(text.property("text").startswith("Uzun metin satırı, yapıştırıldı.")),
        ))


class OrderTests(LongPasteCase):
    def test_clipboard_files_still_attach_first(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="jarvis-lp-"))
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "notes.md").write_text("# not", encoding="utf-8")
        bridge = self.bridge()
        root = self.window(bridge)
        editor = self.item(root, "composerInput")
        editor.forceActiveFocus()
        self.paste(root, bridge, LONG, files=[folder / "notes.md"])
        self.assertEqual(("", ["notes.md"], []),
                         (editor.property("text"), [a["name"] for a in bridge.globalAttachments], self.pasted(bridge)))
