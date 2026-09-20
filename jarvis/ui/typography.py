"""The typefaces, and the one text-layout control QML lacks.

ONE interface face sets everything a person reads or scans - the conversation,
the composer, every label, row, caption and heading: the platform's own UI
face where it exists (Segoe UI Variable on Windows 11, Segoe UI on Windows
10), because it is hinted for exactly this rasterizer at 12-16 px, and the
bundled IBM Plex Sans anywhere else. It is resolved to a REAL regular and a
REAL semibold instance: on Segoe UI Variable a plain 600 request lands on the
Bold instance, so the semibold is its own named family there.

Space Grotesk (the official variable font) is the identity face only: the
wordmark, the greeting, the clock and the large instrument numerals. Both
bundled faces are SIL Open Font License 1.1 files shipped unmodified under
``qml/fonts`` with their license notices, registered once per process and
never downloaded.

A selectable ``TextEdit`` has no ``lineHeight`` property, and a QML font token
cannot reach inside a rendered Markdown document at all, so the conversation's
line height, paragraph rhythm, heading scale and code contrast are applied to
the text document here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import Property, QObject, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
# Imported here, not left to chance: PySide only hands a QML object to Python
# as its real wrapper type when that type's module is loaded. The normal launch
# (python -m jarvis.ui) never imports QtQuick, so without this a TextEdit's
# textDocument arrives as a plain QObject with no textDocument().
from PySide6.QtQuick import QQuickTextDocument

FONT_DIR = Path(__file__).resolve().parent / "qml" / "fonts"
FACES = {
    "sans": ("IBMPlexSans-Regular.ttf", "IBMPlexSans-Medium.ttf", "IBMPlexSans-SemiBold.ttf"),
    "display": ("SpaceGrotesk[wght].ttf",),
    # The conversation's own voice and the composer's invitation. Bundled, so a
    # packaged JARVIS never depends on what Windows happens to have installed.
    "chat": ("Manrope[wght].ttf",),
    "placeholder": ("Inter[opsz,wght].ttf",),
}
#: The conversation is read at Medium, and what names something in it at SemiBold:
#: real instances of the variable face, never a synthesised weight.
CHAT_WEIGHT = 500
CHAT_STRONG_WEIGHT = 600

#: THE THREE VOICES, and nothing else gets a font of its own:
#:
#: * ``ui`` (and its ``uiStrong`` semibold) - the interface face, for all text
#:   a person reads or scans. ``reading`` and ``sans`` are the same face.
#: * ``display`` - Space Grotesk, the identity: wordmark, greeting, clock and
#:   large numerals. Never running text, never small print.
#: * ``mono`` - a real fixed-pitch face for code and technical literals.
#:
#: The interface face, best first: (regular family, semibold family, the
#: weight to ask the semibold family for). Only a pair whose BOTH members are
#: installed is taken, so nothing silently falls back to another face.
UI_FACES = (
    # Windows 11, DirectWrite: the Text optical size, and its own semibold.
    ("Segoe UI Variable Text", "Segoe UI Variable Text Semibold", 400),
    # Windows 10, or a rasterizer that only sees the static files.
    ("Segoe UI", "Segoe UI", 600),
)

#: The reading scale INSIDE a rendered Markdown document, which no QML font
#: token can reach: a heading is a character format on a block, not a property
#: of the TextEdit. Clear over the 17 px body without becoming a banner, in the
#: same reading face (Space Grotesk Medium is a real instance of the variable
#: face, so nothing is synthesised).
HEADING_PX = {1: 21, 2: 19, 3: 17}
HEADING_MIN_PX = 16
#: Air around a heading, and the gap a list item keeps instead of the
#: paragraph gap - the paragraph rhythm of a reply.
HEADING_TOP = 16.0
HEADING_BOTTOM = 6.0
LIST_ITEM_BOTTOM = 4.0
#: Inline code inside a reply: the contrast that makes `jarvis/ui` read as a
#: token instead of as a differently-shaped word.
CODE_INK = "#9dd8ff"
CODE_BED = "#141e2e"
CODE_PX = 14
#: The mono face code is set in, best first. Qt's own "monospace" alias
#: resolves to whatever it finds, which is not always a fixed-pitch face.
MONO_FAMILIES = ("Cascadia Mono", "Consolas", "Courier New")

_families: dict[str, str] | None = None


def _as_quick_text_document(document: QObject | None) -> QQuickTextDocument | None:
    """The QQuickTextDocument behind a TextEdit's ``textDocument``, whatever
    wrapper PySide chose for it; None for anything else."""
    if document is None:
        return None
    if isinstance(document, QQuickTextDocument):
        return document
    if isinstance(document, QObject) and document.inherits("QQuickTextDocument"):
        return shiboken6.wrapInstance(shiboken6.getCppPointer(document)[0], QQuickTextDocument)
    return None


def load_fonts() -> dict[str, str]:
    """Register the bundled faces (once) and return the families to set text in.

    Requires a QGuiApplication. ``ui`` / ``uiStrong`` (with ``uiStrongWeight``)
    are the interface face - the first ``UI_FACES`` pair this rasterizer really
    has, else the bundled IBM Plex Sans at a real 600; ``reading`` and ``sans``
    are that same face. ``display`` is Space Grotesk; ``mono`` is the first real
    fixed-pitch face this machine has.
    """
    global _families
    if _families is None:
        found: dict[str, Any] = {}
        for role, files in FACES.items():
            for name in files:
                font_id = QFontDatabase.addApplicationFont(str(FONT_DIR / name))
                names = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
                if names:
                    found.setdefault(role, names[0])
        installed = set(QFontDatabase.families()) | set(found.values())
        bundled = found.get("sans", "")
        regular, strong, weight = next(((r, s, w) for r, s, w in UI_FACES if r in installed and s in installed),
                                       (bundled, bundled, 600))
        found.update({"ui": regular, "uiStrong": strong, "uiStrongWeight": weight,
                      "reading": regular, "sans": regular})
        for family in MONO_FAMILIES:
            if family in installed:
                found["mono"] = family
                break
        _families = found
    return dict(_families)


class Typography(QObject):
    """Exposed to QML as ``typography``: the loaded family names, and a fixed
    line height for a TextEdit's document."""

    def __init__(self, families: dict[str, str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._families = dict(families)

    @Property(str, constant=True)
    def ui(self) -> str:
        """The interface face: everything a person reads or scans."""
        return self._families.get("ui", "")

    @Property(str, constant=True)
    def uiStrong(self) -> str:
        """The interface face's real semibold family."""
        return self._families.get("uiStrong", "")

    @Property(int, constant=True)
    def uiStrongWeight(self) -> int:
        return int(self._families.get("uiStrongWeight", 600))

    @Property(str, constant=True)
    def sans(self) -> str:
        return self._families.get("sans", "")

    @Property(str, constant=True)
    def display(self) -> str:
        return self._families.get("display", "")

    @Property(str, constant=True)
    def chat(self) -> str:
        """The conversation's face (Manrope): messages, sources, reply metadata."""
        return self._families.get("chat", "")

    @Property(str, constant=True)
    def placeholder(self) -> str:
        """The composer placeholder's face (Inter) - the placeholder only."""
        return self._families.get("placeholder", "")

    @Property(str, constant=True)
    def reading(self) -> str:
        """What a person reads: the conversation, a result, their own message."""
        return self._families.get("reading", "")

    @Property(str, constant=True)
    def mono(self) -> str:
        return self._mono

    @property
    def _mono(self) -> str:
        return self._families.get("mono", "monospace")

    @Slot(QObject, float, float)
    @Slot(QObject, float, float, bool)
    def setLineHeight(self, document: QObject, line: float, paragraph: float, structured: bool = False) -> None:
        """Give the paragraphs of a QQuickTextDocument a fixed line height and
        the spacing that follows them. Read-only text only: the change is a
        formatting edit of the document.

        ``structured`` is for a Markdown-rendered document: a heading and a
        code block keep the rhythm the renderer gave them (a heading is not a
        paragraph of running text), and a list item keeps the reading line
        height without the paragraph gap, so a list stays a list.
        """
        quick_document = _as_quick_text_document(document)
        text_document = quick_document.textDocument() if quick_document is not None else None
        if text_document is None:
            return
        if not structured:
            cursor = QTextCursor(text_document)
            cursor.select(QTextCursor.SelectionType.Document)
            block = QTextBlockFormat()
            block.setLineHeight(float(line), QTextBlockFormat.LineHeightTypes.FixedHeight.value)
            block.setBottomMargin(float(paragraph))
            cursor.mergeBlockFormat(block)
            return
        block = text_document.firstBlock()
        while block.isValid():
            existing = block.blockFormat()
            level = existing.headingLevel()
            if level:
                self._style_heading(block, level)
            elif existing.nonBreakableLines():
                # A fenced code block. The renderer asks for "monospace", which
                # Qt resolves to whatever it finds; the real mono face, the size
                # and the bed that separates code from running text are set here.
                self._style_code(QTextCursor(block), block.length() - 1)  # without the separator
            else:
                updated = QTextBlockFormat(existing)
                updated.setLineHeight(float(line), QTextBlockFormat.LineHeightTypes.FixedHeight.value)
                updated.setBottomMargin(LIST_ITEM_BOTTOM if block.textList() is not None else float(paragraph))
                cursor = QTextCursor(block)
                cursor.setBlockFormat(updated)
            self._style_code_spans(block)
            block = block.next()

    def _style_heading(self, block: Any, level: int) -> None:
        """One Markdown heading, in the conversation face's semibold at the reading scale.

        The renderer only knows a heading is bolder; the hierarchy a reader
        sees - family, size, the air above it - is applied here, because a
        character format inside a document is out of QML's reach.
        """
        size = HEADING_PX.get(level, HEADING_MIN_PX)
        shape = QTextBlockFormat(block.blockFormat())
        shape.setTopMargin(0.0 if block.position() == 0 else HEADING_TOP)
        shape.setBottomMargin(HEADING_BOTTOM)
        # This block only. A BlockUnderCursor selection reaches back over the
        # previous block separator and would give the paragraph above it this
        # heading's shape.
        QTextCursor(block).setBlockFormat(shape)
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + max(0, block.length() - 1), QTextCursor.MoveMode.KeepAnchor)
        face = QFont()
        chat = self._families.get("chat")
        strong = chat or self._families.get("uiStrong") or self._families.get("reading")
        if strong:
            face.setFamilies([strong])
        face.setPixelSize(size)
        # The conversation face has a real SemiBold instance. The interface
        # fallback uses its semibold family's own weight: asking the regular
        # family for 600 would land on its Bold instance.
        face.setWeight(QFont.Weight(CHAT_STRONG_WEIGHT if chat else int(self._families.get("uiStrongWeight", 600))))
        heading = QTextCharFormat()
        heading.setFont(face)
        # Replaced, not merged: the renderer sizes a heading with a POINT size,
        # which would win over the pixel size set here and leave the scale to
        # whatever the document's base size happens to be.
        cursor.setCharFormat(heading)

    def _style_code_spans(self, block: Any) -> None:
        """Inline code inside running text: its own ink on its own bed."""
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            iterator += 1
            if not fragment.isValid() or not fragment.charFormat().fontFixedPitch():
                continue
            cursor = QTextCursor(block.document())
            cursor.setPosition(fragment.position())
            self._style_code(cursor, fragment.length())

    def _style_code(self, cursor: QTextCursor, length: int) -> None:
        """Exactly ``length`` characters from ``cursor``, set as code."""
        cursor.setPosition(cursor.position() + max(0, length), QTextCursor.MoveMode.KeepAnchor)
        face = QFont()
        face.setFamilies([self._mono, "monospace"])
        face.setFixedPitch(True)
        face.setPixelSize(CODE_PX)
        # Its own weight: merged onto running text set at Medium, the mono face
        # would be asked for a 500 it may not have and fall back to another family.
        face.setWeight(QFont.Weight.Normal)
        code = QTextCharFormat()
        code.setFont(face)
        code.setForeground(QColor(CODE_INK))
        code.setBackground(QColor(CODE_BED))
        cursor.mergeCharFormat(code)
