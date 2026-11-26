"""FACESHELL1: the new PC window is a clickable empty shell (sample data, no backend).

* Three sizes in one window: ring only (always on top), small (420x560), large (content pane).
* The ring shows six states; a demo button cycles them; push-to-talk is press-and-hold.
* Every control is reachable by mouse; the bottom row fits at 420 wide; no size exceeds the screen.
* Nothing from the old window or the backend is imported; no file, network, microphone,
  clipboard or screen access; data/jarvis.db is never opened; it loads without a QML warning.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess  # nosec B404 - runs this repo's own `python -m jarvis.face.shell --check`
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from PySide6.QtCore import QObject, QPointF, Qt, QtMsgType, qInstallMessageHandler  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402,F401 - items come back as QQuickItem
from PySide6.QtTest import QTest  # noqa: E402

_APP = QGuiApplication.instance() or QGuiApplication([])

from jarvis.face import shell  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FACE = ROOT / "jarvis" / "face"
MOODS = ["idle", "listening", "understanding", "thinking", "speaking", "needs_you"]
BOTTOM = ["pttButton", "textInput", "attachButton", "replyVoice", "replyText"]

_SEEN: list[str] = []
_RECORD = [False]


def _audit(event: str, args: tuple) -> None:
    if not _RECORD[0]:
        return
    if event == "open" and ("jarvis.db" in str(args[0]) or re.search(r"[wax+]", str(args[1] or ""))):
        _SEEN.append(f"{event} {args[0]!r} {args[1]!r}")
    elif event.split(".")[0] in ("socket", "sqlite3", "subprocess", "urllib", "http", "winreg"):
        _SEEN.append(event)


sys.addaudithook(_audit)


def pump(ms: int = 30) -> None:
    QTest.qWait(ms)


SHELL_IMPORTS = {"__future__", "os", "sys", "pathlib", "PySide6.QtQml", "PySide6.QtGui"}
# Pure computation only (FACEGEO1 geometry, FACEMD1, FACETOKENS1, FACECOMP1): no file, network, process or thread.
PURE_IMPORTS = {"math", "fractions", "decimal", "typing", "dataclasses", "enum", "functools", "itertools",
                "operator", "collections", "collections.abc", "re", "string", "unicodedata", "types", "json",
                "urllib.parse"}  # string parser only; urllib / urllib.request / http / socket stay refused


def _imports(source: str):
    """(module, imported names) for every import statement in the source."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from ((a.name, {a.name}) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module, {a.name for a in node.names}


def _import_ok(file_name: str, module, names: set) -> bool:
    if file_name == "capture.py" and module == "PySide6.QtCore":  # FACECAP1 grabber's PNG buffer only
        return names == {"QBuffer", "QIODevice"}
    if module == "urllib.parse" and module in names:  # "import urllib.parse" binds urllib, whose .request may be loaded
        return False
    return module in SHELL_IMPORTS | PURE_IMPORTS \
        and not re.match(r"^jarvis\.(ui|app|core|memory|connectors)", module)


class StaticTests(unittest.TestCase):
    def test_python_imports_nothing_from_the_old_window_backend_or_io(self) -> None:
        for path in FACE.glob("*.py"):
            for module, names in _imports(path.read_text(encoding="utf-8")):
                with self.subTest(file=path.name, module=module):
                    self.assertTrue(_import_ok(path.name, module, names), (module, sorted(names)))
            self.assertNotIn("jarvis.db", path.read_text(encoding="utf-8"))

    def test_the_import_guard_rejects_io_and_the_old_window(self) -> None:
        banned = ["socket", "subprocess", "urllib", "urllib.request", "http.client", "sqlite3", "winreg", "shutil",
                  "tempfile", "ctypes", "threading", "asyncio", "io", "requests", "PySide6.QtCore", "PySide6.QtNetwork",
                  "jarvis.ui.bridge", "jarvis.core", "jarvis.memory", "jarvis.app", "jarvis.connectors.base"]
        for module in banned:
            for source in (f"import {module}", f"from {module} import x", f"import math\nimport {module} as m"):
                with self.subTest(source=source):
                    self.assertFalse(all(_import_ok("geometry.py", m, n) for m, n in _imports(source)))
        self.assertFalse(_import_ok("capture.py", "PySide6.QtCore", {"QBuffer", "QIODevice", "QFile"}))
        self.assertFalse(all(_import_ok("geometry.py", m, n) for m, n in _imports("from . import shell")))
        for module in sorted(PURE_IMPORTS):
            self.assertTrue(all(_import_ok("geometry.py", m, n) for m, n in _imports(f"from {module} import x")))

    def test_only_urllib_parse_is_allowed_from_the_url_and_network_modules(self) -> None:
        refused = ["import urllib", "import urllib.request", "from urllib import request", "from urllib import parse",
                   "from urllib.request import urlopen", "import urllib.error", "from urllib.response import addinfourl",
                   "import http", "import http.client", "import socket", "import urllib.parse, urllib.request",
                   "import URLLIB.PARSE", "import urllib.parse.x", "from urllib.parser import x",
                   "import urllib.parse", "import urllib.parse as p", "import urllib . parse"]
        for source in refused:
            with self.subTest(source=source):
                self.assertFalse(all(_import_ok("md_blocks.py", m, n) for m, n in _imports(source)))
        for source in ("from urllib.parse import urlsplit", "from urllib.parse import urlsplit as split, quote"):
            with self.subTest(source=source):
                self.assertTrue(all(_import_ok("md_blocks.py", m, n) for m, n in _imports(source)))

    def test_qml_has_no_file_network_microphone_clipboard_or_screen_access(self) -> None:
        banned = r"Clipboard|clipboard|grabWindow|grabToImage|FileDialog|FolderDialog|XMLHttpRequest|" \
                 r"openUrlExternally|QtMultimedia|MediaDevices|AudioInput|Camera|LocalStorage|Qt\.labs|" \
                 r"QtWebSockets|WebSocket|https?://|jarvis\.db|import \"\.\./|bridge\."
        for path in (FACE / "qml").glob("*.qml"):
            with self.subTest(file=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), banned)


class ShellWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.warnings: list[str] = []
        self.engine = QQmlApplicationEngine()
        self.engine.warnings.connect(lambda errors: self.warnings.extend(e.toString() for e in errors))
        previous = qInstallMessageHandler(self._message)
        self.addCleanup(qInstallMessageHandler, previous)
        _SEEN.clear()
        _RECORD[0] = True
        self.addCleanup(_RECORD.__setitem__, 0, False)
        shell.build(self.engine)
        self.assertEqual(1, len(self.engine.rootObjects()), self.warnings)
        self.win = self.engine.rootObjects()[0]
        self.addCleanup(self._close)
        self.win.show()
        self.assertTrue(QTest.qWaitForWindowExposed(self.win))
        pump(50)

    def _message(self, kind, _context, text) -> None:
        if kind != QtMsgType.QtDebugMsg and re.search(r"\.qml|QML|Binding loop|Polish loop|Layout", text):
            self.warnings.append(text)

    def _close(self) -> None:
        self.win.close()
        self.engine.deleteLater()
        pump(10)

    def tearDown(self) -> None:
        self.assertEqual([], self.warnings)
        self.assertEqual([], _SEEN)                       # no jarvis.db, no write, no socket / sqlite / process

    def item(self, name: str):
        found = self.win.findChild(QObject, name)
        self.assertIsNotNone(found, name)
        return found

    def click(self, name: str) -> None:
        item = self.item(name)
        self.assertTrue(item.isVisible(), name)
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(self.win, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        pump()

    def assert_on_screen(self) -> None:
        avail = self.win.screen().availableGeometry()
        self.assertLessEqual(self.win.width(), avail.width())
        self.assertLessEqual(self.win.height(), avail.height())

    def test_default_is_small_with_focus_turns_and_the_bottom_row_in_order(self) -> None:
        self.assertEqual("small", self.win.property("mode"))
        self.assertEqual(420, self.win.width())
        self.assertLessEqual(self.win.height(), 560)
        self.assert_on_screen()
        y = lambda n: self.item(n).mapToScene(QPointF(0, 0)).y()  # noqa: E731
        self.assertLess(y("focusLabel"), y("turns"))
        self.assertLess(y("turns"), y("bottomRow"))
        self.assertTrue(self.item("focusLabel").property("text"))
        turns = self.win.property("turns").toVariant()
        texts, whos = [t["text"] for t in turns], [t["who"] for t in turns]
        self.assertTrue(any(t.startswith("Anladığım: ") for t in texts))
        self.assertIn("Telefon", whos)
        xs = []
        for name in BOTTOM:                               # every control visible, inside the 420 window
            item = self.item(name)
            left = item.mapToScene(QPointF(0, 0)).x()
            self.assertTrue(item.isVisible() and item.width() > 0, name)
            self.assertGreaterEqual(left, 0, name)
            self.assertLessEqual(left + item.width(), self.win.width(), name)
            xs.append(left)
        self.assertEqual(sorted(xs), xs)
        self.assertEqual(["Sesli", "Yazılı"], [self.item(n).property("text") for n in BOTTOM[3:]])

    def test_clicking_the_ring_switches_ring_only_and_small(self) -> None:
        self.click("statusRing")
        self.assertEqual("ring", self.win.property("mode"))
        self.assertTrue(self.win.flags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(self.item("ringOnly").isVisible())
        self.assertLessEqual(max(self.win.width(), self.win.height()), 120)
        self.assert_on_screen()
        self.click("ringOnly")
        self.assertEqual("small", self.win.property("mode"))
        self.assertFalse(self.win.flags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertEqual(420, self.win.width())

    def test_the_grow_button_switches_small_and_large_with_a_content_pane(self) -> None:
        self.assertFalse(self.item("contentPane").isVisible())
        self.click("growButton")
        self.assertEqual("large", self.win.property("mode"))
        self.assertTrue(self.item("contentPane").isVisible())
        self.assertGreater(self.win.width(), 420)
        self.assert_on_screen()
        pane = self.item("contentPane")
        self.assertGreaterEqual(pane.mapToScene(QPointF(0, 0)).x(), self.item("bottomRow").width())
        self.click("growButton")
        self.assertEqual("small", self.win.property("mode"))
        self.assertFalse(self.item("contentPane").isVisible())

    def test_the_demo_button_cycles_the_six_ring_states(self) -> None:
        ring = self.item("statusRing")
        self.assertEqual(MOODS, ring.property("moods").toVariant())
        seen = [ring.property("mood")]
        for _ in MOODS:
            self.click("stateDemo")
            seen.append(ring.property("mood"))
        self.assertEqual(MOODS + ["idle"], seen)
        labels = ring.property("labels").toVariant()
        self.assertEqual(6, len({labels[m] for m in MOODS}))

    def test_push_to_talk_is_press_and_hold(self) -> None:
        ptt, ring = self.item("pttButton"), self.item("statusRing")
        point = ptt.mapToScene(QPointF(ptt.width() / 2, ptt.height() / 2)).toPoint()
        QTest.mousePress(self.win, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        pump()
        self.assertEqual("listening", ring.property("mood"))
        QTest.mouseRelease(self.win, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        pump()
        self.assertEqual("understanding", ring.property("mood"))

    def test_attach_menu_items_show_a_placeholder_only(self) -> None:
        for name, label in (("attachPaste", "Yapıştır"), ("attachFile", "Dosya"), ("attachShot", "Ekran görüntüsü")):
            with self.subTest(item=name):
                self.click("attachButton")
                pump(150)                                 # the menu's open transition
                self.assertTrue(self.item("attachMenu").property("opened"))
                self.click(name)
                pump(150)
                self.assertFalse(self.item("attachMenu").property("visible"))
                self.assertTrue(self.item("placeholder").isVisible())
                self.assertIn(label, self.win.property("placeholder"))
                self.assertIn("yer tutucu", self.win.property("placeholder"))

    def test_reply_mode_switch_is_sesli_or_yazili(self) -> None:
        self.assertEqual("voice", self.win.property("replyMode"))
        self.click("replyText")
        self.assertEqual("text", self.win.property("replyMode"))
        self.assertTrue(self.item("replyText").property("checked"))
        self.assertFalse(self.item("replyVoice").property("checked"))
        self.click("replyVoice")
        self.assertEqual("voice", self.win.property("replyMode"))


class StartTests(unittest.TestCase):
    def test_python_m_jarvis_face_shell_starts_on_its_own(self) -> None:
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        done = subprocess.run([sys.executable, "-m", "jarvis.face.shell", "--check"], cwd=ROOT, env=env,  # nosec B603
                              capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertNotRegex(done.stderr, r"\.qml:\d|QML|Binding loop")


if __name__ == "__main__":
    unittest.main()
