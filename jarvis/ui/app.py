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
from jarvis.ui.weather import KEY_ENV as WEATHER_KEY_ENV

QML_DIR = Path(__file__).resolve().parent / "qml"
MAIN_QML = QML_DIR / "Main.qml"

#: The in-app scheduler loop. One sweep shortly after the window is up, then
#: one every 30 seconds for as long as the application runs - hidden to the
#: tray included. V1 deliberately stops at the process: nothing is registered
#: with Windows, so JARVIS runs no reminder while it is closed.
FIRST_SWEEP_MS = 2_500
SWEEP_INTERVAL_MS = 30_000


def create_engine(bridge: Bridge):
    """A QML engine with the bridge exposed as ``bridge``. Loads Main.qml."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QQmlApplicationEngine

    from jarvis.ui.i18n import localization
    from jarvis.ui.typography import Typography, load_fonts

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(QML_DIR))
    engine.rootContext().setContextProperty("bridge", bridge)
    # The bundled typefaces are registered before any QML exists, so the theme
    # resolves the real family names from the first frame.
    engine._typography = Typography(load_fonts(), engine)
    engine.rootContext().setContextProperty("typography", engine._typography)
    # The one localization path: the persisted language is applied before the
    # first frame, and a later choice retranslates QML and every bridge text.
    i18n = localization(QCoreApplication.instance())
    i18n.attach(engine)
    i18n.apply(bridge.language)
    engine.rootContext().setContextProperty("i18n", i18n)
    bridge.languageChanged.connect(lambda: i18n.apply(bridge.language))
    i18n.languageChanged.connect(bridge.retranslated)
    engine.load(str(MAIN_QML))
    return engine


def install_scheduler_sweeps(bridge: Bridge):
    """Start the periodic sweep and return its timer (owned by the bridge).

    This is a DRIVER for the existing scheduler, never a second one: every
    sweep is the same ``scheduler_once`` call the Automations button makes.
    """
    from PySide6.QtCore import QTimer

    timer = QTimer(bridge)
    timer.setInterval(SWEEP_INTERVAL_MS)
    timer.timeout.connect(bridge.sweepDueWork)
    timer.start()
    QTimer.singleShot(FIRST_SWEEP_MS, bridge.sweepDueWork)
    return timer


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
    # The weather key is taken OUT of the environment before anything starts, so
    # no child process (Claude, Codex, Antigravity...) ever inherits it.
    weather_key = os.environ.pop(WEATHER_KEY_ENV, "").strip()
    bridge = Bridge(Backend(), UiPreferences(), weather_key=weather_key)
    engine = create_engine(bridge)
    if not engine.rootObjects():
        return 1
    # Window X hides to the tray when there is one; Quit JARVIS (tray) is the
    # orderly exit, so a hidden last window must not end the process.
    if install_tray(app, bridge, engine.rootObjects()[0]) is not None:
        app.setQuitOnLastWindowClosed(False)
    bridge.quitRequested.connect(app.quit)
    # Due work is swept while the application runs, the tray included; the
    # timer belongs to the bridge, so quitting ends it with the process.
    install_scheduler_sweeps(bridge)
    try:
        return app.exec()
    finally:
        teardown(bridge, engine)


def teardown(bridge: Bridge, engine) -> None:
    """End the QML tree while the bridge it reads is still whole.

    Left to Python, ``main``'s locals are released in slot order when it
    returns - the bridge BEFORE the engine - and every live binding then
    re-evaluates against a null ``bridge`` (a TypeError per binding). So the
    order is explicit: the bridge stops its clocks and refuses late deliveries,
    the engine (and with it the window and every binding) is deleted now, and
    only then may the bridge go.
    """
    import shiboken6

    bridge.shutdown()
    if shiboken6.isValid(engine):
        shiboken6.delete(engine)
