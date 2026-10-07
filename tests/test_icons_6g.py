"""Gate 6G K1: one drawn icon set replaces every unicode glyph icon.

* No QML string an icon could be drawn from holds a code point the bundled
  fonts lack: a glyph-only literal (no letter or digit - what an icon, a
  marker or a separator is written as) must be covered by EVERY bundled font;
  a symbol inside running text by every file of the bundled interface face
  (`typography.FACES["sans"]`, the face that text renders in when no system
  face is resolved). Coverage comes from the font files themselves.
* Every icon name renders at 12, 16 and 24 px with no QML warning; an unknown
  name draws a visible fallback and warns, also when a name changes later.
"""
from __future__ import annotations

import unicodedata
import unittest

from tests.test_ui import _APP, pump  # noqa: F401 - the offscreen platform and the application

import shiboken6  # noqa: E402
from PySide6.QtCore import QUrl, qInstallMessageHandler  # noqa: E402
from PySide6.QtGui import QRawFont  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402

from jarvis.ui.typography import FACES, FONT_DIR  # noqa: E402

QML_DIR = FONT_DIR.parent
COMPONENTS = QML_DIR / "components"


def qml_string_literals(source: str):
    """(line, decoded text) for every string literal in QML / JS source;
    comments and regular-expression literals are skipped, escapes decoded."""
    out, i, line, n, prev = [], 0, 1, len(source), "("
    while i < n:
        ch = source[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch == "/" and not source.startswith(("//", "/*"), i) and prev in "(,=:[!&|?{};+-*%<>~^":
            i, in_class = i + 1, False                   # a regex literal: up to its closing, unescaped "/"
            while i < n and source[i] != "\n" and (in_class or source[i] != "/"):
                in_class = (in_class or source[i] == "[") and source[i] != "]"
                i += 2 if source[i] == "\\" else 1
            i += 1
            prev = "/"
        elif source.startswith("//", i):
            i = source.find("\n", i)
            i = n if i < 0 else i
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += source.count("\n", i, end)
            i = end
        elif ch in "\"'`":
            start, text, i = line, [], i + 1
            while i < n and source[i] != ch:
                c = source[i]
                if c == "\\" and i + 1 < n:
                    nxt = source[i + 1]
                    if nxt == "u" and source[i + 2:i + 6].isalnum() and len(source[i + 2:i + 6]) == 4:
                        text.append(chr(int(source[i + 2:i + 6], 16)))
                        i += 6
                        continue
                    if nxt == "x":
                        text.append(chr(int(source[i + 2:i + 4], 16)))
                        i += 4
                        continue
                    text.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                    i += 2
                    continue
                if c == "\n":
                    line += 1
                text.append(c)
                i += 1
            out.append((start, "".join(text)))
            i += 1
            prev = ch
        else:
            prev = prev if ch.isspace() else ch
            i += 1
    return out


def _wordy(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LNM"


def violations(source: str, everywhere, interface):
    """(line, char, literal) for each code point a literal may not use."""
    found = []
    for line, text in qml_string_literals(source):
        glyph_only = not any(_wordy(c) for c in text)
        for c in text:
            if c.isspace() or (ord(c) < 128 and not glyph_only) or _wordy(c):
                continue
            if not (everywhere(c) if glyph_only else interface(c)):
                found.append((line, c, text))
    return found


class IconGlyphCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        bundled = sorted(FONT_DIR.glob("*.ttf"))
        self.assertTrue(bundled, "no bundled fonts")
        raw = {path.name: QRawFont(str(path), 16) for path in bundled}
        self.assertTrue(all(font.isValid() for font in raw.values()))
        sans = [raw[name] for name in FACES["sans"]]
        self.everywhere = lambda c: all(font.supportsCharacter(ord(c)) for font in raw.values())
        self.interface = lambda c: all(font.supportsCharacter(ord(c)) for font in sans)

    def test_the_check_catches_a_glyph_the_fonts_lack(self) -> None:
        # Not vacuous: an icon written as a font glyph the bundled fonts lack fails.
        self.assertFalse(self.everywhere("✕"))
        self.assertEqual([(1, "✕", "✕")], violations('glyph: "\\u2715"', self.everywhere, self.interface))
        self.assertEqual([(2, "⟳", "⟳")], violations('// "⟳"\nText { text: "⟳" }', self.everywhere, self.interface))
        self.assertEqual([], violations('text: qsTr("Review") + "  ›"', self.everywhere, self.interface))
        # A backtick inside a regular expression does not open a string that swallows what follows.
        self.assertEqual([(2, "✕", "✕")],
                         violations('s.split(/(```x```)/)' + chr(10) + 'glyph: "✕"', self.everywhere, self.interface))

    def test_no_qml_string_uses_a_code_point_outside_the_bundled_fonts(self) -> None:
        sources = sorted(QML_DIR.rglob("*.qml")) + sorted(QML_DIR.rglob("*.js"))
        self.assertGreater(len(sources), 40)
        bad = [f"{path.relative_to(QML_DIR)}:{line}: U+{ord(c):04X} {c!r} in {text!r}"
               for path in sources
               for line, c, text in violations(path.read_text(encoding="utf-8"), self.everywhere, self.interface)]
        self.assertEqual([], bad)


SHEET = b"""
import QtQuick
import QtQuick.Window
import "icons.js" as Icons
Window {
    width: 960; height: 520; visible: true; color: "#000000"
    property var names: Icons.names.concat(["no-such-icon"])
    Flow {
        anchors.fill: parent; anchors.margins: 8; spacing: 8
        Repeater {
            model: names.length * 3
            Icon {
                objectName: "icon_" + names[Math.floor(index / 3)] + "_" + size
                name: names[Math.floor(index / 3)]
                size: [12, 16, 24][index % 3]
                tint: "#ffffff"
            }
        }
    }
}
"""


class IconRenderTests(unittest.TestCase):
    def test_every_icon_renders_at_12_16_and_24_and_an_unknown_name_is_visible_and_warns(self) -> None:
        messages: list[str] = []
        previous = qInstallMessageHandler(lambda mode, context, text: messages.append(text))
        self.addCleanup(qInstallMessageHandler, previous)
        engine = QQmlEngine()
        self.addCleanup(engine.deleteLater)
        component = QQmlComponent(engine)
        component.setData(SHEET, QUrl.fromLocalFile(str(COMPONENTS / "IconSheetTest.qml")))
        window = component.create()
        self.assertIsNotNone(window, component.errorString())
        self.addCleanup(lambda: shiboken6.isValid(window) and window.hide())
        pump(300)
        image = window.grabWindow()
        def items(item):
            for child in item.childItems():
                yield child
                yield from items(child)
        icons = {item.objectName(): item for item in items(window.contentItem()) if item.objectName().startswith("icon_")}
        names = sorted({key.split("_")[1] for key in icons})
        self.assertIn("close", names)
        self.assertIn("no-such-icon", names)
        self.assertGreaterEqual(len(names), 45)
        for name in names:
            for size in (12, 16, 24):
                with self.subTest(name=name, size=size):
                    item = icons.get(f"icon_{name}_{size}")
                    self.assertIsNotNone(item)
                    self.assertEqual((size, size), (item.width(), item.height()))
                    origin = item.mapToScene(item.boundingRect().topLeft())
                    lit = sum(1 for x in range(int(origin.x()), int(origin.x()) + size)
                              for y in range(int(origin.y()), int(origin.y()) + size)
                              if image.pixelColor(x, y).lightness() > 60)
                    self.assertGreater(lit, size // 4, "nothing drawn")
        unknown = [m for m in messages if 'unknown icon name "no-such-icon"' in m]
        self.assertEqual(3, len(unknown))                    # one per instance, each size
        self.assertEqual([], [m for m in messages if m not in unknown])
        # A name changed later is judged by its NEW value: unknown warns, known never does.
        messages.clear()
        item = icons["icon_close_16"]
        item.setProperty("name", "still-no-such-icon")
        pump(50)
        item.setProperty("name", "check")
        pump(50)
        self.assertEqual(['Icon: unknown icon name "still-no-such-icon"'], [m for m in messages if "Icon:" in m])
        self.assertTrue(item.property("known"))


if __name__ == "__main__":
    unittest.main()
