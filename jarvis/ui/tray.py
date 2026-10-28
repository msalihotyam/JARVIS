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

import sys

from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QTimer, Slot
from PySide6.QtGui import QIcon, QWindow

from jarvis.ui.i18n import QT_TRANSLATE_NOOP, tx
from jarvis.ui.resources import asset_path

#: Ring colours for the restrained state badge: normal, frozen, attention.
#: The approved mark ships pre-tinted per state as ``tray-<state>.png``; this
#: stays as the one place the mapping from badge name to meaning is written
#: down, even though nothing here mixes a colour at runtime any more.
_RING = {"running": "#3aa8f0", "frozen": "#ff5c6c", "attention": "#ffb347"}

#: One ``QIcon`` per badge state, loaded once from the approved artwork and
#: reused - ``refresh()`` asks for one on every bridge state change.
_ICONS: dict[str, QIcon] = {}

# The hide-to-tray notice: a 2 s timeout hint, then taken down at 2.5 s,
# because Windows 10/11 shows a tray balloon as a toast and largely ignores
# the hint. Re-issuing the balloon with an empty title and text sets NIF_INFO
# with an empty szInfo, the documented way to remove it (NOTIFYICONDATA);
# it touches neither the icon, its tooltip nor its menu.
_NOTICE_MS = 2000
_NOTICE_DISMISS_MS = 2500

# PM28: project-mode notifications are polled from the outbox and stay up
# (no empty-balloon dismissal); NotifySink throttles them.
_OUTBOX_POLL_MS = 2000
_TOAST_MS = 8000


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
        base = tx("Tray", {"frozen": QT_TRANSLATE_NOOP("Tray", "Frozen"),
                           "freeze_requested": QT_TRANSLATE_NOOP("Tray", "Freeze requested"),
                           "resuming": QT_TRANSLATE_NOOP("Tray", "Resuming"),
                           "unconfirmed": QT_TRANSLATE_NOOP("Tray", "State unconfirmed"),
                           "refused": QT_TRANSLATE_NOOP("Tray", "Configuration refused")}.get(state, QT_TRANSLATE_NOOP("Tray", "Running")))
        attention = tx("Tray", QT_TRANSLATE_NOOP("Tray", "Attention required"))
        if self._attention():
            return f"JARVIS — {base} · {attention}" if state not in ("running", "unknown") else f"JARVIS — {attention}"
        return f"JARVIS — {base}"

    def badge(self) -> str:
        if not self.bridge.executionEnabled:
            return "frozen"
        return "attention" if self._attention() else "running"

    def lifecycle_action(self) -> tuple[str, bool]:
        """The menu's Freeze / Resume entry: its label and whether it acts."""
        if self.bridge.executionState == "refused":
            return tx("Tray", QT_TRANSLATE_NOOP("Tray", "Frozen — configuration refused")), False
        return ((tx("Tray", QT_TRANSLATE_NOOP("Tray", "Freeze JARVIS")), True) if self.bridge.executionEnabled
                else (tx("Tray", QT_TRANSLATE_NOOP("Tray", "Resume JARVIS")), True))

    def menu_labels(self) -> tuple[str, str]:
        """Open and Quit, in the current language."""
        return tx("Tray", QT_TRANSLATE_NOOP("Tray", "Open JARVIS")), tx("Tray", QT_TRANSLATE_NOOP("Tray", "Quit JARVIS"))

    def hidden_message(self) -> str:
        return tx("Tray", QT_TRANSLATE_NOOP("Tray", "JARVIS is running in the background."))

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
        window = self.window
        if self.bridge.quitWarning:
            self.open()                          # the confirmation needs the window
        elif self.bridge.ambientEffects and window.isVisible() and window.visibility() != QWindow.Visibility.Minimized:
            # DFWIN2B: on screen, the quit powers down first; its quitNow confirms the quit once.
            # Ambient off there is no effect to play (Theme.ambient is ambientEffects): the direct path.
            fx = window.findChild(QObject, "windowPowerFx")
            if fx is not None:
                QMetaObject.invokeMethod(fx, "powerOff", Q_ARG("QVariant", "quit"))
                return
        self.bridge.requestQuit()


def _icon(badge: str) -> QIcon:
    """The approved JARVIS mark, tinted for this status.

    An unrecognised badge falls back to ``running``, exactly as the old
    painted version's colour lookup did - ``badge()`` only ever returns one
    of the three names, so this is defence, not a reachable path.
    """
    name = badge if badge in _RING else "running"
    cached = _ICONS.get(name)
    if cached is None:
        cached = QIcon(str(asset_path(f"tray-{name}.png")))
        _ICONS[name] = cached
    return cached


def outbox_fetch(bridge):
    """``fetch(after_id)`` over the bridge's store, or None without one.

    A store failure reads as "nothing new" so a poll never breaks the tray.
    """
    conversations = getattr(getattr(bridge, "backend", None), "conversations", None)
    if not callable(conversations):
        return None

    def fetch(after_id: int) -> list:
        try:
            store = conversations()
            return [] if store is None else store.notify_outbox_rows(after_id=after_id)
        except Exception:
            return []
    return fetch


def deliver(icon, sink) -> None:
    """One poll: new rows in, at most one toast out (high = Warning icon)."""
    from PySide6.QtWidgets import QSystemTrayIcon

    sink.poll()
    toast = sink.toast()
    if toast is not None:
        title, text, importance = toast
        kind = QSystemTrayIcon.MessageIcon.Warning if importance == "high" else QSystemTrayIcon.MessageIcon.Information
        icon.showMessage(title, text, kind, _TOAST_MS)


def install_tray(app, bridge, window):
    """Create the tray icon and its menu, or return None when the platform has no tray."""
    from PySide6.QtWidgets import QMenu, QSystemTrayIcon

    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    controller = TrayController(bridge, window, app)
    icon = QSystemTrayIcon(_icon("running"), app)
    menu = QMenu()
    open_action = menu.addAction("", controller.open)
    lifecycle = menu.addAction("", controller.toggle_lifecycle)
    menu.addSeparator()
    quit_action = menu.addAction("", controller.quit)
    icon.setContextMenu(menu)

    def refresh() -> None:
        # Every label is re-read here, so a language switch reaches the menu too.
        open_label, quit_label = controller.menu_labels()
        open_action.setText(open_label)
        quit_action.setText(quit_label)
        label, enabled = controller.lifecycle_action()
        lifecycle.setText(label)
        lifecycle.setEnabled(enabled)
        icon.setToolTip(controller.status_text())
        icon.setIcon(_icon(controller.badge()))

    told = []

    def hidden() -> None:
        if not told:                             # once per run, never on every X
            told.append(True)
            icon.showMessage("JARVIS", controller.hidden_message(),
                             QSystemTrayIcon.MessageIcon.Information, _NOTICE_MS)
            if sys.platform == "win32":          # the empty-balloon removal is a Windows contract
                QTimer.singleShot(_NOTICE_DISMISS_MS, icon, lambda: icon.showMessage(
                    "", "", QSystemTrayIcon.MessageIcon.NoIcon, 0))

    icon.activated.connect(lambda reason: controller.open() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
    bridge.attentionChanged.connect(refresh)
    bridge.languageChanged.connect(refresh)
    bridge.hiddenToTray.connect(hidden)
    app.aboutToQuit.connect(icon.hide)
    clicked = getattr(icon, "messageClicked", None)
    if clicked is not None:
        clicked.connect(controller.open)
    fetch = outbox_fetch(bridge)
    if fetch is not None:
        from jarvis.notify_sink import NotifySink

        app._jarvis_notify = sink = NotifySink(fetch)
        poll = QTimer(icon)
        poll.setInterval(_OUTBOX_POLL_MS)
        poll.timeout.connect(lambda: deliver(icon, sink))
        poll.start()
    refresh()
    icon.show()
    bridge.setTrayAvailable(True)
    # The application keeps the Python references alive for its lifetime.
    app._jarvis_tray = (icon, menu, controller)
    return icon
