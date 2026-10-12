"""One JARVIS window per Windows user.

The lock is a named mutex in the session's ``Local\\`` namespace: creating it
is atomic (two launches at the same moment get exactly one creator), and
Windows destroys it with the last handle, so a crashed or killed JARVIS never
leaves a stale lock behind. Messages go over a ``QLocalServer`` named pipe
that only the same user may open (a pipe alone is no lock: on Windows a second
``listen`` on the same name succeeds).

A launch that does not get the lock asks the running JARVIS to come to the
front and ends; it never creates a window, a tray icon or a store.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time

from PySide6.QtCore import Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

SHOW = b"show\n"
#: How long a losing launch keeps trying to reach the winner, which may still
#: be between taking the lock and listening.
NOTIFY_TIMEOUT_MS = 5_000
_ERROR_ACCESS_DENIED = 5            # held by an elevated JARVIS of this user
_ERROR_ALREADY_EXISTS = 183
_ASFW_ANY = -1


def default_name() -> str:
    """The per-user name of the lock and the pipe (pipe names are machine-wide).

    The account name from Windows itself, never from environment variables a
    shell may set differently."""
    return f"JARVIS-UI-{os.getlogin()}"


def acquire(name: str):
    """Take the lock: a handle to keep for the life of the process, or None
    when another JARVIS of this user already holds it."""
    if os.name != "nt":
        return True     # ponytail: Windows-only lock; add a QLockFile here if JARVIS ever ships elsewhere
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_ACCESS_DENIED:
            return None
        raise ctypes.WinError(error)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return handle


def release(handle) -> None:
    """Give the lock back (tests; a running JARVIS keeps it until it exits)."""
    if os.name == "nt" and handle:
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))


def notify_running(name: str, timeout_ms: int | None = None) -> bool:
    """Ask the JARVIS holding the lock to come to the front; True once it read it."""
    if os.name == "nt":
        # This launch has the foreground (the shell just started it); the
        # running JARVIS may take it, or Windows would only flash its button.
        ctypes.WinDLL("user32").AllowSetForegroundWindow(_ASFW_ANY)
    deadline = time.monotonic() + (NOTIFY_TIMEOUT_MS if timeout_ms is None else timeout_ms) / 1000
    while True:
        socket = QLocalSocket()
        socket.connectToServer(name)
        if socket.waitForConnected(500):
            socket.write(SHOW)
            sent = socket.waitForBytesWritten(1000)
            # The server hangs up once it has read the request: that is the receipt.
            read = sent and (socket.state() == QLocalSocket.LocalSocketState.UnconnectedState
                             or socket.waitForDisconnected(2000))
            socket.abort()
            return bool(read)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


class InstanceServer(QLocalServer):
    """Listens for later launches; ``showRequested`` once per "show" request."""

    showRequested = Signal()

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.newConnection.connect(self._accept)
        if not self.listen(name):
            # A leftover socket file (not on Windows, where pipes die with
            # their process); the lock already proves no JARVIS serves it.
            QLocalServer.removeServer(name)
            if not self.listen(name):
                log.warning("single instance: cannot listen on %r (%s); later launches will not reach this JARVIS",
                            name, self.errorString())

    def _accept(self) -> None:
        while self.hasPendingConnections():
            socket = self.nextPendingConnection()
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket) -> None:
        if SHOW.strip() in bytes(socket.readAll()):
            socket.disconnectFromServer()
            self.showRequested.emit()
