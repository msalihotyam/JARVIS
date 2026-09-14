"""The Windows system tray: where JARVIS lives while its window is hidden.

Three operations stay distinct. Window X HIDES JARVIS here - the process,
the session and any running work continue (unless JARVIS is frozen, which
is a separate choice). Freeze / Resume JARVIS in the menu is the SAME bridge
path as the red control; Resume still asks in the window. Quit JARVIS is the
one normal way to end the application, and it asks first whenever real state
says work is active or unresolved.

``TrayController`` holds the decisions and needs no widget; ``install_tray``
wires it to a ``QSystemTrayIcon`` (which needs a ``QApplication``).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QWindow

#: Ring colours for the restrained state badge: normal, frozen, attention.
_RING = {"running": "#3aa8f0", "frozen": "#ff5c6c", "attention": "#ffb347"}


class TrayController(QObject):
    """What the tray says and does, from the bridge's authoritative state."""

    def __init__(self, bridge, window, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.window = window

    def _attention(self) -> bool:
        return any(item["key"] != "execution" for item in self.bridge.safety["attention"])

    def status_text(self) -> str:
        state = self.bridge.executionState
        base = {"frozen": "Frozen", "freeze_requested": "Freeze requested", "resuming": "Resuming",
                "unconfirmed": "State unconfirmed", "refused": "Configuration refused"}.get(state, "Running")
        if self._attention():
            return f"JARVIS — {base} · Attention required" if base != "Running" else "JARVIS — Attention required"
        return f"JARVIS — {base}"

    def badge(self) -> str:
        if not self.bridge.executionEnabled:
            return "frozen"
        return "attention" if self._attention() else "running"

    def lifecycle_action(self) -> tuple[str, bool]:
        """The menu's Freeze / Resume entry: its label and whether it acts."""
        if self.bridge.executionState == "refused":
            return "Frozen — configuration refused", False
        return ("Freeze JARVIS", True) if self.bridge.executionEnabled else ("Resume JARVIS", True)

    @Slot()
    def open(self) -> None:
        """Show the one existing window, as it was, in front."""
        window = self.window
        if not window.isVisible() or window.visibility() == QWindow.Visibility.Minimized:
            restore = window.property("restoreVisibility")
            window.setVisibility(QWindow.Visibility(restore) if restore else QWindow.Visibility.Windowed)
        window.raise_()
        window.requestActivate()

    @Slot()
    def toggle_lifecycle(self) -> None:
        if self.bridge.executionState == "refused":
            return
        if self.bridge.executionEnabled:
            self.bridge.freezeJarvis()          # immediate, exactly like the red control
        else:
            self.open()                          # Resume is confirmed in the window
            self.bridge.resumeConfirmRequested.emit()

    @Slot()
    def quit(self) -> None:
        if self.bridge.quitWarning:
            self.open()                          # the confirmation needs the window
        self.bridge.requestQuit()


def _icon(badge: str) -> QIcon:
    """A small JARVIS mark drawn in code (the app-icon milestone is later)."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#2a5f8f"), 3))
    painter.setBrush(QColor("#061424"))
    painter.drawEllipse(3, 3, 58, 58)
    painter.setPen(QPen(QColor(_RING.get(badge, _RING["running"])), 9))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(16, 16, 32, 32)
    painter.end()
    return QIcon(pixmap)


def install_tray(app, bridge, window):
    """Create the tray icon and its menu, or return None when the platform has no tray."""
    from PySide6.QtWidgets import QMenu, QSystemTrayIcon

    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    controller = TrayController(bridge, window, app)
    icon = QSystemTrayIcon(_icon("running"), app)
    menu = QMenu()
    menu.addAction("Open JARVIS", controller.open)
    lifecycle = menu.addAction("Freeze JARVIS", controller.toggle_lifecycle)
    menu.addSeparator()
    menu.addAction("Quit JARVIS", controller.quit)
    icon.setContextMenu(menu)

    def refresh() -> None:
        label, enabled = controller.lifecycle_action()
        lifecycle.setText(label)
        lifecycle.setEnabled(enabled)
        icon.setToolTip(controller.status_text())
        icon.setIcon(_icon(controller.badge()))

    told = []

    def hidden() -> None:
        if not told:                             # once per run, never on every X
            told.append(True)
            icon.showMessage("JARVIS", "JARVIS is still running in the background.",
                             QSystemTrayIcon.MessageIcon.Information, 4000)

    icon.activated.connect(lambda reason: controller.open() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
    bridge.attentionChanged.connect(refresh)
    bridge.hiddenToTray.connect(hidden)
    app.aboutToQuit.connect(icon.hide)
    refresh()
    icon.show()
    bridge.setTrayAvailable(True)
    # The application keeps the Python references alive for its lifetime.
    app._jarvis_tray = (icon, menu, controller)
    return icon
