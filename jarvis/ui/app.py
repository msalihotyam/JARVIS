"""Application bootstrap: one QGuiApplication, one QML engine, one bridge.

Launch with ``python -m jarvis.ui``. The window opens full-screen; F11 and
the top-right controls handle minimize / restore / maximize / windowed /
close from inside the QML layer.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from jarvis.ui.backend import Backend
from jarvis.ui.bridge import Bridge
from jarvis.ui.preferences import UiPreferences

QML_DIR = Path(__file__).resolve().parent / "qml"
MAIN_QML = QML_DIR / "Main.qml"


def create_engine(bridge: Bridge):
    """A QML engine with the bridge exposed as ``bridge``. Loads Main.qml."""
    from PySide6.QtQml import QQmlApplicationEngine

    from jarvis.ui.typography import Typography, load_fonts

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(QML_DIR))
    engine.rootContext().setContextProperty("bridge", bridge)
    # The bundled typefaces are registered before any QML exists, so the theme
    # resolves the real family names from the first frame.
    engine._typography = Typography(load_fonts(), engine)
    engine.rootContext().setContextProperty("typography", engine._typography)
    engine.load(str(MAIN_QML))
    return engine


def main(argv: list[str] | None = None) -> int:
    # The Basic style is the one whose look this UI fully overrides; nothing
    # of the platform's stock widget styling is meant to show through.
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("JARVIS")
    app.setOrganizationName("JARVIS")
    bridge = Bridge(Backend(), UiPreferences())
    engine = create_engine(bridge)
    if not engine.rootObjects():
        return 1
    bridge.quitRequested.connect(app.quit)
    return app.exec()
