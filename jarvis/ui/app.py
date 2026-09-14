"""Application bootstrap: one QApplication, one QML engine, one bridge, one tray icon.

Launch with ``python -m jarvis.ui``. The window opens full-screen; F11 and
the top-right controls handle minimize / restore / windowed and hide-to-tray
from inside the QML layer; Quit JARVIS in the system tray ends the application.
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
    # A QApplication (a QGuiApplication with widgets) because the system tray
    # icon and its menu are widgets; the interface itself stays QML.
    from PySide6.QtWidgets import QApplication

    from jarvis.ui.tray import install_tray

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("JARVIS")
    app.setOrganizationName("JARVIS")
    bridge = Bridge(Backend(), UiPreferences())
    engine = create_engine(bridge)
    if not engine.rootObjects():
        return 1
    # Window X hides to the tray when there is one; Quit JARVIS (tray) is the
    # orderly exit, so a hidden last window must not end the process.
    if install_tray(app, bridge, engine.rootObjects()[0]) is not None:
        app.setQuitOnLastWindowClosed(False)
    bridge.quitRequested.connect(app.quit)
    return app.exec()
