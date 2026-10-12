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

#: How text is rasterized, read once at startup; never a setting, never
#: persisted. T (the default, Salih 2026-10-12) = Qt's FreeType engine with
#: vertical hinting and stem darkening (build/reports/DFFONTLAB2.md). Dev
#: overrides (dev/font_try.ps1): native = the platform's DirectWrite path,
#: O = T without stem darkening. Unset or any other value is T.
FONT_RENDER_ENV = "JARVIS_FONT_RENDER"


def apply_font_render_env() -> str:
    """Before the QApplication exists: the mode's environment. Returns the mode.

    ``setdefault``, so a platform the caller already chose (the tests'
    offscreen) keeps winning."""
    mode = os.environ.get(FONT_RENDER_ENV)
    if mode not in ("native", "O"):
        mode = "T"
    if mode == "native" or sys.platform != "win32":
        return "native"
    os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=freetype")
    if mode == "T":
        os.environ.setdefault("FREETYPE_PROPERTIES", "autofitter:no-stem-darkening=0")
    return mode


def apply_font_render_hinting(app, mode: str) -> None:
    """After the QApplication exists: O and T hint vertically, app-wide."""
    if mode == "native":
        return
    from PySide6.QtGui import QFont

    font = app.font()
    font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    app.setFont(font)


def _text_focus_release():
    """An event filter for the window: a press outside the text editor that has
    focus - outside its whole control (an ancestor marked ``keepsTextFocus``,
    such as the composer with its chips and actions) - releases that focus.

    It only looks: the press is never consumed, so it still reaches whatever is
    under it, and nothing covers the window to catch it."""
    from PySide6.QtCore import QEvent, QObject

    class TextFocusRelease(QObject):
        def eventFilter(self, window, event):  # noqa: N802 - Qt's name
            if event.type() == QEvent.Type.MouseButtonPress:
                item = window.activeFocusItem()
                if item is not None and item.property("readOnly") is False and item.property("cursorPosition") is not None:
                    owner = item
                    while owner.parentItem() is not None and owner.property("keepsTextFocus") is not True:
                        owner = owner.parentItem()
                    region = owner if owner.property("keepsTextFocus") is True else item
                    if not region.contains(region.mapFromScene(event.scenePosition())):
                        # Its own focus is dropped (the window's scope would hand active
                        # focus straight back to its focused child).
                        item.setFocus(False)
            return False

    return TextFocusRelease()


def create_engine(bridge: Bridge, *, icon=None):
    """A QML engine with the bridge exposed as ``bridge``. Loads Main.qml.

    ``icon``, when given, is set on every root window explicitly. Qt already
    hands a new window the application's default icon (set on ``QApplication``
    before this runs), so this is belt-and-suspenders - but the Windows
    taskbar icon follows the native window handle, and setting it directly on
    the window is the one path that does not depend on that propagation
    happening before the platform window is realized.
    """
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow

    from jarvis.ui.i18n import localization
    from jarvis.ui.typography import Typography, load_fonts

    # Every text as native glyphs from the font engine (FreeType under T), the
    # explicit and the defaulted alike: a mix with Qt's distance-field glyphs reads dirty.
    QQuickWindow.setTextRenderType(QQuickWindow.TextRenderType.NativeTextRendering)
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
    engine._focus_release = _text_focus_release()
    for window in engine.rootObjects():
        window.installEventFilter(engine._focus_release)
        if icon is not None:
            window.setIcon(icon)
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


def main(argv: list[str] | None = None, *, instance: str | None = None) -> int:
    """Start JARVIS, or bring the running one to the front.

    One JARVIS per Windows user: the launch (``argv`` None, the process's own
    command line) claims the per-user instance name; an embedded call with an
    explicit ``argv`` claims only the ``instance`` it is given, if any - so a
    test never takes the real one. The lock comes before anything else; a
    launch that loses it asks the winner to show itself and returns 0 without
    a window, a tray icon or a store.
    """
    from jarvis.ui import single_instance

    name = instance or (single_instance.default_name() if argv is None else None)
    lock = single_instance.acquire(name) if name else None
    if name and lock is None:
        if single_instance.notify_running(name):
            return 0
        # Nobody answered: a JARVIS that was quitting may be gone now - take over.
        lock = single_instance.acquire(name)
        if lock is None:
            return 0
    # The Basic style is the one whose look this UI fully overrides; nothing
    # of the platform's stock widget styling is meant to show through.
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    font_render = apply_font_render_env()
    # A QApplication (a QGuiApplication with widgets) because the system tray
    # icon and its menu are widgets; the interface itself stays QML.
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from jarvis.ui.resources import asset_path
    from jarvis.ui.tray import TrayController, install_tray

    app = QApplication(argv if argv is not None else sys.argv)
    apply_font_render_hinting(app, font_render)
    # Later launches are heard from now on; the lock lives as long as the process.
    server = single_instance.InstanceServer(name, app) if name else None
    app._jarvis_instance = (lock, server)
    app.setApplicationName("JARVIS")
    app.setOrganizationName("JARVIS")
    # The approved mark, set once and reused: as the application default (new
    # windows inherit it) and explicitly on the main window and the tray
    # (below), so the taskbar and the window title bar both carry it whether
    # or not the platform propagates the application default in time.
    icon = QIcon(str(asset_path("app.ico")))
    app.setWindowIcon(icon)
    # The weather key is taken OUT of the environment before anything starts, so
    # no child process (Claude, Codex, Antigravity...) ever inherits it.
    weather_key = os.environ.pop(WEATHER_KEY_ENV, "").strip()
    bridge = Bridge(Backend(), UiPreferences(), weather_key=weather_key)
    engine = create_engine(bridge, icon=icon)
    if not engine.rootObjects():
        teardown(bridge, engine)      # the same orderly end: no late delivery into a deleted bridge
        return 1
    # Window X hides to the tray when there is one; Quit JARVIS (tray) is the
    # orderly exit, so a hidden last window must not end the process.
    window = engine.rootObjects()[0]
    if install_tray(app, bridge, window) is not None:
        app.setQuitOnLastWindowClosed(False)
    # A second launch shows this window exactly as the tray's Open does.
    # A quitting JARVIS stops answering, so a launch right after Quit takes over.
    if server is not None:
        server.showRequested.connect(TrayController(bridge, window, app).open)
        app.aboutToQuit.connect(server.close)
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
