"""The bundled typefaces, and the one text-layout control QML lacks.

IBM Plex Sans (Regular / Medium / SemiBold) carries the interface and the
conversation; Space Grotesk (the official variable font, whose weight axis
supplies a genuine 600) carries the wordmark, the greeting and the instrument
numerals. Both are SIL Open Font License 1.1 files shipped unmodified under
``qml/fonts`` with their license notices, and are registered with Qt once per
process - never downloaded, never taken from the machine's installed fonts.

A selectable ``TextEdit`` has no ``lineHeight`` property, so the conversation's
fixed line height and paragraph spacing are applied to its text document here.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Property, QObject, Slot
from PySide6.QtGui import QFontDatabase, QTextBlockFormat, QTextCursor

FONT_DIR = Path(__file__).resolve().parent / "qml" / "fonts"
FACES = {
    "sans": ("IBMPlexSans-Regular.ttf", "IBMPlexSans-Medium.ttf", "IBMPlexSans-SemiBold.ttf"),
    "display": ("SpaceGrotesk[wght].ttf",),
}

_families: dict[str, str] | None = None


def load_fonts() -> dict[str, str]:
    """Register the bundled faces (once) and return the family names Qt reports.

    Requires a QGuiApplication. A face that fails to load is simply absent from
    the result; the theme then keeps its fallback family.
    """
    global _families
    if _families is None:
        found: dict[str, str] = {}
        for role, files in FACES.items():
            for name in files:
                font_id = QFontDatabase.addApplicationFont(str(FONT_DIR / name))
                names = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
                if names:
                    found.setdefault(role, names[0])
        _families = found
    return dict(_families)


class Typography(QObject):
    """Exposed to QML as ``typography``: the loaded family names, and a fixed
    line height for a TextEdit's document."""

    def __init__(self, families: dict[str, str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._families = dict(families)

    @Property(str, constant=True)
    def sans(self) -> str:
        return self._families.get("sans", "")

    @Property(str, constant=True)
    def display(self) -> str:
        return self._families.get("display", "")

    @Slot(QObject, float, float)
    def setLineHeight(self, document: QObject, line: float, paragraph: float) -> None:
        """Give every paragraph of a QQuickTextDocument a fixed line height and
        the spacing that follows it. Read-only text only: the change is a
        formatting edit of the document."""
        text_document = document.textDocument() if document is not None else None
        if text_document is None:
            return
        cursor = QTextCursor(text_document)
        cursor.select(QTextCursor.SelectionType.Document)
        block = QTextBlockFormat()
        block.setLineHeight(float(line), QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        block.setBottomMargin(float(paragraph))
        cursor.mergeBlockFormat(block)
