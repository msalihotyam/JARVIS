"""DFFONT: T is the default font render, and the interface text renders crisp under it.

Salih 2026-10-12: "T". Under Qt's FreeType engine the interface face fell back
from Segoe UI Variable to the static Segoe UI, whose 400 stems at 12-15 px are
about one pixel wide and turn grey in grayscale. The fix registers the system's
Segoe UI Variable file for FreeType only, draws its regular at 500 (the weight
of the accepted conversation text) and fully hints the 12 px tracked label.

No test starts JARVIS or opens data\\jarvis.db: main() is never called, the
font database is probed in a child process that opens no window, and the
Theme tokens are read from a bare QML engine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401 - the live-database guard
from PySide6.QtCore import QUrl
from PySide6.QtGui import QFont
from PySide6.QtQml import QQmlComponent, QQmlEngine

from jarvis.ui import app as ui_app
from jarvis.ui import typography
from tests.test_ui import _APP  # noqa: F401 - the application

ROOT = Path(__file__).resolve().parents[1]
QML_DIR = ROOT / "jarvis" / "ui" / "qml"
FONT_ENV = ("QT_QPA_PLATFORM", "FREETYPE_PROPERTIES", ui_app.FONT_RENDER_ENV)
T_ENV = {"QT_QPA_PLATFORM": "windows:fontengine=freetype", "FREETYPE_PROPERTIES": "autofitter:no-stem-darkening=0"}


class FakeApp:
    def __init__(self):
        self.fonts = []

    def font(self):
        return QFont()

    def setFont(self, font):  # noqa: N802 - Qt's name
        self.fonts.append(font)


def mode(value, platform="win32"):
    """apply_font_render_env on a clean font environment: (mode, what it set, the app font hinting)."""
    env = {k: v for k, v in os.environ.items() if k not in FONT_ENV}
    if value is not None:
        env[ui_app.FONT_RENDER_ENV] = value
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "platform", platform):
        chosen = ui_app.apply_font_render_env()
        seen = {k: os.environ.get(k) for k in ("QT_QPA_PLATFORM", "FREETYPE_PROPERTIES")}
    app = FakeApp()
    ui_app.apply_font_render_hinting(app, chosen)
    return chosen, seen, [f.hintingPreference() for f in app.fonts]


class DefaultTests(unittest.TestCase):
    def test_unset_is_t(self):
        self.assertEqual(mode(None), ("T", T_ENV, [QFont.HintingPreference.PreferVerticalHinting]))

    def test_an_unknown_value_is_t(self):
        for value in ("", "o", "t", "OT", "freetype", " O", "Native"):
            with self.subTest(value=value):
                self.assertEqual(mode(value), ("T", T_ENV, [QFont.HintingPreference.PreferVerticalHinting]))

    def test_native_and_o_stay_dev_overrides(self):
        self.assertEqual(mode("native"), ("native", {"QT_QPA_PLATFORM": None, "FREETYPE_PROPERTIES": None}, []))
        self.assertEqual(mode("O"), ("O", {"QT_QPA_PLATFORM": "windows:fontengine=freetype", "FREETYPE_PROPERTIES": None},
                                     [QFont.HintingPreference.PreferVerticalHinting]))
        self.assertEqual(mode("T"), ("T", T_ENV, [QFont.HintingPreference.PreferVerticalHinting]))

    def test_off_windows_the_default_is_native(self):
        self.assertEqual(mode(None, "linux")[0], "native")


def resolved(platform: str) -> dict:
    """typography.load_fonts() in a child process on that Qt platform (no window is opened)."""
    code = ("import json, sys; from PySide6.QtGui import QGuiApplication; app = QGuiApplication([]); "
            "from jarvis.ui.typography import load_fonts; print(json.dumps(load_fonts()))")
    env = {**os.environ, "QT_QPA_PLATFORM": platform, "PYTHONPATH": str(ROOT)}
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@unittest.skipUnless(sys.platform == "win32" and typography.SYSTEM_UI_VARIABLE.is_file(), "Windows 11 Segoe UI Variable")
class FaceTests(unittest.TestCase):
    def test_freetype_gets_the_variable_face_not_the_static_fallback(self):
        faces = resolved("windows:fontengine=freetype")
        self.assertEqual((faces["ui"], faces["uiStrong"], faces["uiStrongWeight"], faces["uiVariable"]),
                         ("Segoe UI Variable", "Segoe UI Variable", 600, True))

    def test_directwrite_keeps_its_own_family(self):
        faces = resolved("windows")
        self.assertEqual((faces["ui"], faces["uiStrong"], faces["uiStrongWeight"], faces["uiVariable"]),
                         ("Segoe UI Variable Text", "Segoe UI Variable Text Semibold", 400, False))


TOKENS = ("label", "hudLabel", "caption", "captionStrong", "body", "bodyStrong", "lead", "pageRow", "dateText", "composerChip")


def tokens(families: dict) -> dict:
    """The Theme font tokens with this typography in the context."""
    engine = QQmlEngine()
    engine.addImportPath(str(QML_DIR))
    face = typography.Typography(families, engine)
    engine.rootContext().setContextProperty("typography", face)
    component = QQmlComponent(engine)
    props = "\n".join(f"    property font {n}: Theme.{n}" for n in TOKENS)
    component.setData(f'import QtQuick\nimport "theme"\nQtObject {{\n{props}\n}}\n'.encode(),
                      QUrl.fromLocalFile(str(QML_DIR / "dffont_probe.qml")))
    obj = component.create()
    assert obj is not None, component.errorString()
    found = {n: QFont(obj.property(n)) for n in TOKENS}
    engine.deleteLater()
    return found


def axes(font: QFont) -> dict:
    return {bytes(tag.toString()).decode(): round(font.variableAxisValue(tag), 2) for tag in font.variableAxisTags()}


VARIABLE = {"ui": "Segoe UI Variable", "uiStrong": "Segoe UI Variable", "uiStrongWeight": 600, "uiVariable": True}
STATIC = {"ui": "Segoe UI", "uiStrong": "Segoe UI", "uiStrongWeight": 600}


class TokenTests(unittest.TestCase):
    def test_the_variable_face_draws_its_regular_at_the_conversation_weight(self):
        t = tokens(VARIABLE)
        for name, px in (("caption", 13), ("body", 14), ("lead", 15), ("pageRow", 15), ("dateText", 13)):
            with self.subTest(token=name):
                f = t[name]
                self.assertEqual((f.family(), f.pixelSize(), f.weight()), ("Segoe UI Variable", px, QFont.Weight(500)))
                self.assertEqual(axes(f), {"wght": 500.0, "opsz": 10.5})
                self.assertEqual(f.hintingPreference(), QFont.HintingPreference.PreferDefaultHinting)
        for name, px in (("captionStrong", 13), ("bodyStrong", 14), ("composerChip", 13)):
            with self.subTest(token=name):
                f = t[name]
                self.assertEqual((f.pixelSize(), f.weight()), (px, QFont.Weight(600)))
                self.assertEqual(axes(f), {"wght": 600.0, "opsz": 10.5})

    def test_the_tracked_label_is_fully_hinted_and_keeps_its_size_weight_and_tracking(self):
        t = tokens(VARIABLE)
        for name in ("label", "hudLabel"):       # PROJELER, SECILI, OPENAI - CODEX; the motto
            with self.subTest(token=name):
                f = t[name]
                self.assertEqual((f.pixelSize(), f.weight(), round(f.letterSpacing(), 1)), (12, QFont.Weight(600), 0.7))
                self.assertEqual(f.hintingPreference(), QFont.HintingPreference.PreferFullHinting)
                self.assertEqual(axes(f), {"wght": 600.0, "opsz": 10.5})

    def test_a_static_face_is_untouched(self):
        """DirectWrite, offscreen (the UI tests) and Windows 10: today's tokens exactly."""
        t = tokens(STATIC)
        self.assertEqual((t["caption"].weight(), axes(t["caption"]), t["caption"].hintingPreference()),
                         (QFont.Weight.Normal, {}, QFont.HintingPreference.PreferDefaultHinting))
        self.assertEqual((t["label"].weight(), axes(t["label"]), t["label"].hintingPreference()),
                         (QFont.Weight(600), {}, QFont.HintingPreference.PreferDefaultHinting))


if __name__ == "__main__":
    unittest.main()
