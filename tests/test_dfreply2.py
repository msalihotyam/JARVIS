"""DFREPLY2 (Salih 2026-10-12): a long reply shows without stalling the window.

``Typography.setLineHeight`` restyles a Markdown document inside ONE edit block, so
the document lays itself out once instead of after each of its thousands of block
and character formats. Proved here: the formats are exactly those the old
one-format-at-a-time pass set (the old pass is kept below, verbatim, as the
reference), the document reports one change instead of thousands, no undo step is
added to a feed message, and a 50,000-character document is restyled inside a bound.
The window-level bounds (show and stream) are in tests/test_dfreply.py LongMarkdownTests.

Bare TextEdits on a private engine: no bridge, model, network or database.
"""

from __future__ import annotations

import time
import unittest

from tests.test_ui import _APP, pump  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtGui import QTextBlockFormat, QTextCursor  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402

from jarvis.conversation import REPLY_MAX_CHARS  # noqa: E402
from jarvis.ui.typography import (  # noqa: E402
    FONT_DIR,
    LIST_ITEM_BOTTOM,
    Typography,
    _as_quick_text_document,
    load_fonts,
)

#: Theme.conversationLine / Theme.conversationParagraph, as FeedView passes them.
LINE, PARAGRAPH = 26.0, 12.0
#: The restyle of a 50,000-character document alone (the old pass: about 1.2 s here).
RESTYLE_BOUND = 0.5

SHORT = "# Başlık\n\nBir paragraf, içinde `kod` ve **kalın** bir söz.\n"
MEDIUM_BLOCK = (
    "## Bölüm\n\n1. **Kalın** bir madde ve `kod` parçası.\n- Bir liste satırı, biraz daha uzun yazılmış.\n\n"
    "```python\nprint('merhaba')\n```\n\n"
    "# Birinci\n\n### Üçüncü düzey\n\n#### Dördüncü düzey\n\n"
    "Düz bir paragraf: *eğik*, **kalın**, `jarvis/ui` ve bir [bağlantı](https://example.invalid).\n"
    "Aynı paragrafın ikinci satırı, `iki` `kod` parçasıyla.\n\n"
    "- dış madde\n  - iç madde `x = 1`\n  - ikinci iç madde\n- son dış madde\n\n"
    "> Bir alıntı, içinde `kod`.\n\n"
    "| Ad | Değer |\n|----|-------|\n| a | `1` |\n| b | 2 |\n\n"
    "```\nçok satırlı\n  girintili kod\n```\n\n"
)
MEDIUM = MEDIUM_BLOCK * 3
#: Cut mid-construct on purpose, as a stream preview or a too-long reply is.
LONG = (MEDIUM_BLOCK * (REPLY_MAX_CHARS // len(MEDIUM_BLOCK) + 1))[:REPLY_MAX_CHARS]
#: The document LongMarkdownTests shows.
LONG_LIST = (("## Bölüm\n\n1. **Kalın** bir madde ve `kod` parçası.\n- Bir liste satırı, biraz daha uzun yazılmış.\n\n"
              "```python\nprint('merhaba')\n```\n\n") * 400)[:REPLY_MAX_CHARS]


def old_set_line_height(typography: Typography, document, line: float, paragraph: float) -> None:
    """The structured pass as it was at f84c808, verbatim: every format its own edit."""
    quick_document = _as_quick_text_document(document)
    text_document = quick_document.textDocument() if quick_document is not None else None
    if text_document is None:
        return
    block = text_document.firstBlock()
    while block.isValid():
        existing = block.blockFormat()
        level = existing.headingLevel()
        if level:
            typography._style_heading(block, level)
        elif existing.nonBreakableLines():
            typography._style_code(QTextCursor(block), block.length() - 1)  # without the separator
        else:
            updated = QTextBlockFormat(existing)
            updated.setLineHeight(float(line), QTextBlockFormat.LineHeightTypes.FixedHeight.value)
            updated.setBottomMargin(LIST_ITEM_BOTTOM if block.textList() is not None else float(paragraph))
            cursor = QTextCursor(block)
            cursor.setBlockFormat(updated)
        typography._style_code_spans(block)
        block = block.next()


def formats(text_document) -> list:
    """Every block's format and char format, and every fragment's span and char format."""
    found = []
    block = text_document.firstBlock()
    while block.isValid():
        fragments = []
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            iterator += 1
            fragments.append((fragment.position(), fragment.length(), fragment.charFormat()))
        found.append((block.position(), block.blockFormat(), block.charFormat(), fragments))
        block = block.next()
    return found


def layout(text_document) -> list:
    """What the layout made of it: the size, and every block's line count and box."""
    text_document.documentLayout().documentSize()
    found = [text_document.size()]
    block = text_document.firstBlock()
    while block.isValid():
        lines = block.layout()
        found.append((lines.lineCount(), lines.boundingRect(),
                      [lines.lineAt(n).height() for n in range(lines.lineCount())]))
        block = block.next()
    return found


class Restyle(unittest.TestCase):
    def setUp(self) -> None:
        self.typography = Typography(load_fonts())
        self.engine = QQmlEngine()
        self.addCleanup(self.engine.deleteLater)
        self.keep: list = []

    def edit(self, text: str, read_only: bool = True):
        """A Markdown TextEdit set up as a feed message is, holding ``text``."""
        component = QQmlComponent(self.engine)
        component.setData(f"""import QtQuick
TextEdit {{ width: 700; readOnly: {'true' if read_only else 'false'}; wrapMode: TextEdit.Wrap
           textFormat: TextEdit.MarkdownText; font.pixelSize: 16 }}""".encode(),
                          QUrl.fromLocalFile(str(FONT_DIR.parent / "dfreply2_probe.qml")))
        item = component.create()
        self.assertIsNotNone(item, component.errorString())
        self.keep += [component, item]
        self.addCleanup(item.deleteLater)
        item.setProperty("text", text)
        pump(0)
        document = item.property("textDocument")
        return document, _as_quick_text_document(document).textDocument()


class FormatEqualityTests(Restyle):
    """The batched pass sets exactly what the old one-format-at-a-time pass set."""

    def test_short_medium_and_50000_character_documents_get_identical_formats(self) -> None:
        for name, text in (("short", SHORT), ("medium", MEDIUM), ("long", LONG), ("long list", LONG_LIST)):
            with self.subTest(name):
                old_document, old_text = self.edit(text)
                new_document, new_text = self.edit(text)
                untouched = formats(new_text)
                old_set_line_height(self.typography, old_document, LINE, PARAGRAPH)
                self.typography.setLineHeight(new_document, LINE, PARAGRAPH, True)
                expected, got = formats(old_text), formats(new_text)
                self.assertNotEqual(untouched, expected)                     # the pass really restyles
                self.assertEqual(len(expected), len(got))
                for want, have in zip(expected, got):
                    self.assertEqual(want, have, f"{name}: block at {want[0]} differs")
                self.assertTrue(any(block[1].lineHeight() == LINE for block in got))
                pump(0)
                self.assertEqual(layout(old_text), layout(new_text))         # the same lines, the same heights

    def test_the_document_changes_once_instead_of_once_per_format(self) -> None:
        old_document, old_text = self.edit(LONG)
        new_document, new_text = self.edit(LONG)
        counts = {"old": 0, "new": 0}
        old_text.contentsChange.connect(lambda *_: counts.__setitem__("old", counts["old"] + 1))
        new_text.contentsChange.connect(lambda *_: counts.__setitem__("new", counts["new"] + 1))
        old_set_line_height(self.typography, old_document, LINE, PARAGRAPH)
        self.typography.setLineHeight(new_document, LINE, PARAGRAPH, True)
        self.assertGreater(counts["old"], 1000)                              # the stall DFREPLY measured
        self.assertEqual(1, counts["new"])


def undo_steps(text_document) -> int:
    """The undo steps a person would take, counted by taking them."""
    steps = 0
    while text_document.isUndoAvailable():
        text_document.undo()
        steps += 1
    return steps


class UndoTests(Restyle):
    """The restyle is display formatting: it never fills a Markdown message's undo stack.
    (A read-only TextEdit's document still records undo: the old pass left one command
    per format there, 112 for MEDIUM.)"""

    def test_a_read_only_message_gets_no_undo_step(self) -> None:
        document, text_document = self.edit(MEDIUM)
        self.assertTrue(text_document.isUndoRedoEnabled())                  # so the check below means something
        self.assertEqual(0, text_document.availableUndoSteps())
        for _ in range(2):                                                   # applied again, as a delegate does
            self.typography.setLineHeight(document, LINE, PARAGRAPH, True)
            self.assertEqual(0, text_document.availableUndoSteps())
            self.assertFalse(text_document.isUndoAvailable())
        self.assertTrue(text_document.isUndoRedoEnabled())                  # left as it was

    def test_a_document_with_history_keeps_it_and_gains_at_most_one_step(self) -> None:
        document, text_document = self.edit(MEDIUM, read_only=False)
        QTextCursor(text_document).insertText("Önce yazılan. ")
        self.typography.setLineHeight(document, LINE, PARAGRAPH, True)
        self.assertIn(undo_steps(text_document), (1, 2))                    # the typing, and at most the restyle
        self.assertTrue(text_document.toPlainText().startswith(MEDIUM.splitlines()[0].lstrip("# ")))


class RestylePerformanceTests(Restyle):
    def test_a_50000_character_document_is_restyled_inside_the_bound(self) -> None:
        for text in (LONG, LONG_LIST):
            document, _ = self.edit(text)
            started = time.monotonic()
            self.typography.setLineHeight(document, LINE, PARAGRAPH, True)
            took = time.monotonic() - started
            self.assertLess(took, RESTYLE_BOUND, f"restyling 50,000 characters took {took:.2f} s")


if __name__ == "__main__":
    unittest.main()
