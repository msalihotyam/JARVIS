"""FACEBAR1: the bottom bar of canvas frame 5 (Composer.qml with TalkButton, ModeSwitch, SendButton, Waveform).

* One body at three widths (640 window, 660 full screen, 460 ring box = "mini"); it draws face.composer
  and calls face slots only; colour only on the listening talk button and on Send when canSend.
* Driven by a fake `face` (FACEVIEW1 is not awaited); tokens carry exactly the contract names and values.
* Zero QML warnings in every state and width; no file, network, clipboard or database access.
* `python tests/test_face_composer_qml.py --shots` writes offscreen screenshots to build/face_shots/FACEBAR1/.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"                 # an inherited platform must not win
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
if "--shots" in sys.argv:
    os.environ["QT_QUICK_BACKEND"] = "software"           # offscreen grabs need the software scene graph

from PySide6.QtCore import (Property, Q_ARG, QEvent, QMetaObject, QObject, QPointF, Qt, QtMsgType,  # noqa: E402
                            QUrl, Signal, Slot, qInstallMessageHandler)
from PySide6.QtGui import (QColor, QGuiApplication, QImage, QInputMethodEvent, QKeyEvent,  # noqa: E402
                           QPainter)
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402,F401 - items come back as QQuickItem
from PySide6.QtTest import QTest  # noqa: E402

_APP = QGuiApplication.instance() or QGuiApplication([])

ROOT = Path(__file__).resolve().parents[1]
QML = ROOT / "jarvis" / "face" / "qml"
OWNED = ["Composer.qml", "TalkButton.qml", "ModeSwitch.qml", "SendButton.qml", "Waveform.qml"]
WIDTHS = {"window": 640, "full": 660, "mini": 460}
STATES = ["empty", "typing", "listening", "transcribing", "understood"]
SHOTS = ROOT / "build" / "face_shots" / "FACEBAR1"

_SEEN: list[str] = []
_RECORD = [False]
_MESSAGES: list[str] = []


def _audit(event: str, args: tuple) -> None:
    if not _RECORD[0]:
        return
    if event == "open" and ("jarvis.db" in str(args[0]) or re.search(r"[wax+]", str(args[1] or ""))):
        _SEEN.append(f"{event} {args[0]!r} {args[1]!r}")
    elif event.split(".")[0] in ("socket", "sqlite3", "subprocess", "urllib", "http", "winreg"):
        _SEEN.append(event)


sys.addaudithook(_audit)


def _message(kind, context, text) -> None:
    from_qml = (context.file or "").endswith(".qml") or (context.category or "").startswith("qml")
    if kind != QtMsgType.QtDebugMsg and (from_qml or re.search(r"\.qml|QML|Binding loop|Polish loop|Layout", text)):
        _MESSAGES.append(text)


qInstallMessageHandler(_message)

COLORS = {"base": "#0B0E13", "surface1": "#11161D", "surface2": "#171D26", "surface3": "#0E1218",
          "line1": "#1B222C", "line2": "#232B36", "text1": "#E7ECF2", "text2": "#AAB4C0", "text3": "#8793A1",
          "text_soft": "#C4CCD6", "accent": "#5BC8F5", "accent_ink": "#06202C", "accent_surface": "#0F1C24",
          "accent_line": "#1D3A4A", "needs_you": "#F2B34A", "error": "#F2777F", "ok": "#6FD3A3",
          "disabled_glyph": "#5E6978", "link": "#7FD3F7", "link_hover": "#A6E1FA"}
TYPE = {"body": (17, 28), "h2": (22, 30), "h3": (17, 26), "code": (14.5, 22), "small": (13, 20),
        "label": (12.5, 16), "turn": (16, 26), "cell": (15, 24), "meta": (13, 20), "control": (14, 20)}
RADIUS = {"panel": 12, "composer": 16, "control": 12, "code": 8, "pill": 999}
SPACE = [4, 6, 8, 10, 12, 14, 16, 20, 24]
MOTION = {"fast": 120, "medium": 200, "slow": 320}
CURVE = {"x1": 0.2, "y1": 0, "x2": 0, "y2": 1}
ACCENTS = {COLORS[k].lower() for k in ("accent", "accent_ink", "accent_surface", "accent_line")}


def make_tokens(reduced: bool = False) -> dict:
    out = {f"color_{k}": v for k, v in COLORS.items()}
    for k, (size, line) in TYPE.items():
        out[f"type_{k}_size"], out[f"type_{k}_line"] = size, line
    out.update({f"radius_{k}": v for k, v in RADIUS.items()})
    out.update({f"space_{v}": v for v in SPACE})
    out.update({f"motion_{k}": 0 if reduced else v for k, v in MOTION.items()})
    out.update({f"motion_curve_{k}": v for k, v in CURVE.items()})
    return out


def make_composer(state: str = "empty", text: str = "", can_send: bool = False, **kw) -> dict:
    out = {"state": state, "text": text, "canSend": can_send, "sendReason": "" if can_send else "empty",
           "talkLabel": "Bitir" if state == "listening" else "Konuş",
           "elapsed": 65 if state == "listening" else None, "mode": "sesli", "corrections": []}
    out.update(kw)
    return out


LEVELS = [0.15, 0.4, 0.8, 0.55, 1.0, 0.3, 0.65, 0.2, 0.9, 0.45]
VIEW = {"empty": make_composer("empty"),
        "typing": make_composer("typing", "Yarın saat üçte toplantı var mı?", True),
        "listening": make_composer("listening"),
        "transcribing": make_composer("transcribing"),
        "understood": make_composer("understood", "Yarın saat üçte toplantı var mı?", True,
                                    corrections=[["üç", "üçte"]])}


class FakeFace(QObject):
    """The `face` context object of the QML contract, with recording slots and settable values."""
    tokensChanged = Signal()
    composerChanged = Signal()
    linkChanged = Signal()
    talkAvailableChanged = Signal()
    attachAvailableChanged = Signal()
    levelsChanged = Signal()

    def __init__(self, reduced: bool = False, **values) -> None:
        super().__init__()
        self.values = {"tokens": make_tokens(reduced), "composer": make_composer(), "link": "ok",
                       "talkAvailable": True, "attachAvailable": True, "levels": []}
        self.values.update(values)
        self.calls: list[tuple] = []

    def set(self, **kw) -> None:
        for key, value in kw.items():
            self.values[key] = value
            getattr(self, key + "Changed").emit()

    echo = True                                           # False: a model that does not write the edit back
    tokens = Property("QVariantMap", lambda s: s.values["tokens"], notify=tokensChanged)
    composer = Property("QVariantMap", lambda s: s.values["composer"], notify=composerChanged)
    link = Property(str, lambda s: s.values["link"], notify=linkChanged)
    talkAvailable = Property(bool, lambda s: s.values["talkAvailable"], notify=talkAvailableChanged)
    attachAvailable = Property(bool, lambda s: s.values["attachAvailable"], notify=attachAvailableChanged)
    levels = Property("QVariantList", lambda s: s.values["levels"], notify=levelsChanged)

    @Slot(str)
    def textEdited(self, text: str) -> None:
        self.calls.append(("textEdited", text))
        if self.echo:
            self.values["composer"] = {**self.values["composer"], "text": text}   # the model echoes the text back
            self.composerChanged.emit()

    @Slot()
    def send(self) -> None:
        self.calls.append(("send",))

    @Slot(str)
    def talkToggle(self, source: str) -> None:
        self.calls.append(("talkToggle", source))

    @Slot()
    def talkCancel(self) -> None:
        self.calls.append(("talkCancel",))

    @Slot(str)
    def modeSet(self, mode: str) -> None:
        self.calls.append(("modeSet", mode))

    @Slot()
    def undoCorrections(self) -> None:
        self.calls.append(("undoCorrections",))

    @Slot(str)
    def copyText(self, entry_id: str) -> None:
        self.calls.append(("copyText", entry_id))

    @Slot(str)
    def linkActivated(self, url: str) -> None:
        self.calls.append(("linkActivated", url))


HOST = """import QtQuick
Window {
    id: win
    property string auditJson: ""
    property int attachCount: 0
    width: %(w)d + 2 * face.tokens.space_24
    height: %(h)d
    color: face.tokens.color_base
    visible: true
    Composer {
        id: comp
        objectName: "composer"
        variant: "%(v)s"
        x: face.tokens.space_24
        width: %(w)d
        anchors.bottom: parent.bottom
        anchors.bottomMargin: face.tokens.space_24
        onAttachRequested: win.attachCount++
    }
    function owner(it) {
        for (var p = it; p; p = p.parent)
            if (p.objectName !== "")
                return p.objectName
        return ""
    }
    function audit() {
        var rows = []
        var walk = function (it) {
            var k = String(it)
            var at = it.mapToItem(comp, 0, 0)
            var r = { k: k, s: it.objectName, n: owner(it), x: at.x, y: at.y, w: it.width, h: it.height,
                      v: it.visible, en: it.enabled,
                      fmt: it.textFormat === undefined ? -1 : it.textFormat,
                      text: typeof it.text === "string" ? it.text.slice(0, 80) : "",
                      color: it.color === undefined ? "" : String(it.color),
                      bc: it.border !== undefined && it.border.width > 0 ? String(it.border.color) : "",
                      font: it.font === undefined ? "" : it.font.family,
                      radius: it.radius === undefined ? -1 : it.radius }
            var o = k.indexOf("QQuickMouseArea") === 0 ? it.parent : k.indexOf("QQuickTextEdit") === 0 ? it : null
            if (o) {
                r.on = o.objectName
                r.acc = o.Accessible.name
                r.ow = o.width
                r.oh = o.height
            }
            rows.push(r)
            for (var i = 0; i < it.children.length; i++)
                walk(it.children[i])
        }
        walk(comp)
        auditJson = JSON.stringify(rows)
    }
}
"""


def open_host(key: str = "window", reduced: bool = False, **values):
    """Composer at one width in a host window, against a fresh FakeFace; returns (face, engine, window)."""
    face = FakeFace(reduced, **values)
    engine = QQmlEngine()
    engine.warnings.connect(lambda errors: _MESSAGES.extend(e.toString() for e in errors))
    engine.rootContext().setContextProperty("face", face)
    comp = QQmlComponent(engine, engine)
    comp.setData((HOST % {"w": WIDTHS[key], "h": 420, "v": key}).encode(),
                 QUrl.fromLocalFile(str(QML / "FacebarHost.qml")))
    win = comp.create()
    if win is None:
        raise AssertionError(comp.errorString())
    QTest.qWaitForWindowExposed(win)
    win.requestActivate()
    QTest.qWaitForWindowActive(win)
    QTest.qWait(50)
    return face, engine, win


def find(win, name: str):
    todo = [win.contentItem()]
    while todo:
        it = todo.pop()
        if it.objectName() == name:
            return it
        todo.extend(it.childItems())
    raise AssertionError(f"no item named {name}")


class Rig(unittest.TestCase):
    def setUp(self) -> None:
        self.opened: list = []
        _MESSAGES.clear()
        _SEEN.clear()
        _RECORD[0] = True

    def tearDown(self) -> None:
        self.close_all()
        _RECORD[0] = False
        self.assertEqual(_MESSAGES, [], "QML warnings")
        self.assertEqual(_SEEN, [], "file, network or database access")

    def open_bar(self, key: str = "window", reduced: bool = False, **values):
        self.face, self.engine, self.win = open_host(key, reduced, **values)
        self.opened.append((self.face, self.engine, self.win))
        return self.win

    def close_all(self) -> None:
        for _face, _engine, win in self.opened:
            win.close()
            win.deleteLater()
        QTest.qWait(1)
        for _face, engine, _win in self.opened:
            engine.deleteLater()
        QTest.qWait(1)
        self.opened.clear()

    def set(self, **kw) -> None:
        self.face.set(**kw)
        QTest.qWait(50)                                   # let layouts and positioners polish

    def item(self, name: str):
        return find(self.win, name)

    def click(self, name: str, double: bool = False) -> None:
        it = self.item(name)
        pt = it.mapToScene(QPointF(it.width() / 2, it.height() / 2)).toPoint()
        (QTest.mouseDClick if double else QTest.mouseClick)(self.win, Qt.LeftButton, Qt.NoModifier, pt)
        QTest.qWait(5)

    def rows(self) -> list:
        QMetaObject.invokeMethod(self.win, "audit")
        return json.loads(self.win.property("auditJson"))

    def row(self, name: str, rows: list) -> dict:
        return next(r for r in rows if r["s"] == name)

    def texts(self, rows: list, name: str) -> list:
        return [r["text"] for r in rows if r["n"] == name and r["k"].startswith("QQuickText(") and r["v"]]

    def check_frame(self, width: int, composer: dict, link: str = "ok") -> list:
        """Plain text everywhere, accent only where allowed, every control named, 44 x 44, inside the bar."""
        rows = self.rows()
        allowed = ({"talkHalo", "talkFace", "talkGlyph"} if composer.get("state") == "listening" else set()) | \
            ({"sendButton", "sendGlyph"} if composer.get("canSend") is True and link != "unreachable" else set())
        for r in rows:
            if not r["v"]:
                continue
            tag = f'{r["k"]} {r["n"]}'
            if r["color"].lower() in ACCENTS or r["bc"].lower() in ACCENTS:
                self.assertIn(r["s"] or r["n"], allowed, tag)
            if r["k"].startswith(("QQuickText(", "QQuickTextEdit(")):
                self.assertEqual(r["fmt"], 0, tag)
            if "on" in r:
                self.assertTrue(r["on"] and r["acc"], tag)
                self.assertGreaterEqual(min(r["ow"], r["oh"]), 44, tag)
                self.assertTrue(-2 <= r["x"] and r["x"] + r["w"] <= width + 2, tag)
        return rows


_LEX = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*')
_BANNED = re.compile(r'Clipboard|clipboard|grabWindow|grabToImage|FileDialog|FolderDialog|XMLHttpRequest|'
                     r'openUrlExternally|QtMultimedia|MediaDevices|AudioInput|Camera|LocalStorage|Qt\.labs|'
                     r'QtWebSockets|WebSocket|https?://|jarvis\.db|import\s+"\.\./|bridge\.')
COMPOSER_FIELDS = {"state", "text", "canSend", "sendReason", "talkLabel", "elapsed", "mode", "corrections"}
FACE_NAMES = {"tokens", "composer", "link", "talkAvailable", "attachAvailable", "levels",
              "textEdited", "send", "talkToggle", "talkCancel", "modeSet", "undoCorrections"}


def _view(name: str, keep_strings: bool = False) -> str:
    """The QML source without comments; string literals emptied unless keep_strings."""
    text = (QML / name).read_text(encoding="utf-8")
    return _LEX.sub(lambda m: "" if m.group().startswith("//") else (m.group() if keep_strings else '""'), text)


class StaticContract(unittest.TestCase):
    """Negative 2: the QML contract read from the source of the five owned files."""

    def test_no_bare_numbers_and_no_literal_colours(self) -> None:
        for name in OWNED:
            with self.subTest(name):
                numbers = set(re.findall(r"(?<![\w.])\d+(?:\.\d+)?", _view(name)))
                self.assertLessEqual(numbers, {"0", "1", "2"})
                kept = _view(name, keep_strings=True)
                self.assertIsNone(re.search(r"#[0-9A-Fa-f]{3,8}\b", kept))
                self.assertIsNone(re.search(r"Qt\.(?:rgba|rgb|hsla|hsva|hsl|tint|color)\s*\(", kept))
                self.assertIsNone(re.search(r"color\s*:\s*\"", kept))

    def test_no_state_machinery_in_qml(self) -> None:
        for name in OWNED:
            with self.subTest(name):
                code = _view(name)
                for pattern in (r"\bTimer\s*\{", r"\bBehavior\b", r"PressAndHold", r"\bShortcut\b",
                                r"\bConnections\b", r"\bStates?\s*\{", r"face(?:\.\w+)+\s*=(?!=)"):
                    self.assertIsNone(re.search(pattern, code), pattern)
                self.assertEqual(len(re.findall(r"\bKeys\.", code)), 1 if name == "Composer.qml" else 0)
        keys = set(re.findall(r"Qt\.Key_(\w+)", _view("Composer.qml")))
        self.assertEqual(keys, {"Return", "Enter"})                    # no other key is bound

    def test_face_is_the_only_door(self) -> None:
        for name in OWNED:
            with self.subTest(name):
                code, kept = _view(name), _view(name, keep_strings=True)
                self.assertLessEqual(set(re.findall(r"\bface\.(\w+)", code)), FACE_NAMES)
                fields = set(re.findall(r"\bface\.composer\.(\w+)", code))
                if name == "Composer.qml":
                    fields |= set(re.findall(r"(?<![\w.])c\.(\w+)", code))
                self.assertLessEqual(fields, COMPOSER_FIELDS)
                self.assertTrue(all(a == '"button"' for a in re.findall(r"talkToggle\(([^)]*)\)", kept)))
                imports = set(re.findall(r"^import\s+(.+?)\s*$", code, re.M))
                self.assertLessEqual(imports, {"QtQuick", "QtQuick.Layouts"})
                self.assertIsNone(_BANNED.search((QML / name).read_text(encoding="utf-8")))

    def test_every_text_is_plain(self) -> None:
        for name in OWNED:
            with self.subTest(name):
                code = _view(name)
                formats = re.findall(r"\btextFormat\s*:\s*([\w.]+)", code)
                self.assertEqual(len(re.findall(r"\b(?:Text|TextEdit)\s*\{", code)), len(formats))
                self.assertTrue(formats == [] or all(f.endswith(".PlainText") for f in formats), formats)

    def test_tokens_are_contract_names(self) -> None:
        known = set(make_tokens())
        for name in OWNED:
            with self.subTest(name):
                used = set(re.findall(r"(?:\bt|\bface\.tokens)\.(\w+)", _view(name)))
                self.assertTrue(used)
                self.assertLessEqual(used, known)

    def test_no_foreign_module_names(self) -> None:
        pattern = re.compile("|".join(p + "_" for p in ("talk", "telegram", "brain")))
        for path in [QML / n for n in OWNED] + [Path(__file__)]:
            with self.subTest(path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))


def colour(name: str) -> str:
    return COLORS[name].lower()


def bars(rows: list) -> list:
    return [r for r in rows if r["n"] == "waveBars" and r["s"] == "" and r["v"]
            and not r["k"].startswith("QQuickRepeater(")]


class FrameMatrix(Rig):
    """Negatives 1 and 6: each state at each width, normal and reduced motion, with zero QML warnings."""

    def test_states_widths_and_motion(self) -> None:
        for key, width in WIDTHS.items():
            for reduced in (False, True):
                self.open_bar(key, reduced)
                for state, levels in [(s, LEVELS) for s in VIEW] + [("listening", [])]:
                    with self.subTest(key=key, reduced=reduced, state=state, levels=len(levels)):
                        self.set(composer=VIEW[state], levels=levels)
                        self.check_frame(width, VIEW[state])
                        self.assertEqual(_MESSAGES, [])
                with self.subTest(key=key, reduced=reduced, link="unreachable"):
                    self.set(composer=VIEW["typing"], link="unreachable")
                    self.check_frame(width, VIEW["typing"], "unreachable")
                self.assertEqual(self.face.calls, [])
                self.close_all()


class Look(Rig):
    """Acceptance 1-3: the body, the tool row, the two places colour appears and the five states."""

    def near(self, a: float, b: float) -> None:
        self.assertAlmostEqual(a, b, delta=1)

    def test_a1_body_and_tool_row(self) -> None:
        width = WIDTHS["window"]
        self.open_bar()
        rows = self.check_frame(width, VIEW["empty"])
        body, glow = self.row("composerBody", rows), self.row("composerHighlight", rows)
        self.assertEqual((body["radius"], body["color"], body["bc"]), (16, colour("surface1"), colour("line2")))
        self.assertEqual((glow["v"], glow["h"]), (True, 1))
        hint = self.row("composerPlaceholder", rows)
        self.assertEqual((hint["v"], hint["text"], hint["color"]),
                         (True, "JARVIS'e yaz ya da Konuş'a tıkla", colour("text3")))
        talk, plus, mode, send, field = (self.row(n, rows) for n in
                                         ("talkButton", "attachButton", "modeSwitch", "sendButton", "composerScroll"))
        self.near(talk["x"], 10)                                           # padding 8 / 10 / 10
        self.near(plus["x"] - (talk["x"] + talk["w"]), 6)                  # gap 6
        self.near(send["x"] - (mode["x"] + mode["w"]), 6)
        self.near(send["x"] + send["w"], width - 10)
        self.near(body["y"] + body["h"] - (send["y"] + send["h"]), 10)
        self.near(send["y"] - (field["y"] + field["h"]), 8)
        self.assertEqual((send["w"], send["h"], mode["h"]), (44, 44, 44))
        fills = {r["n"]: r["color"] for r in rows if r["n"] in ("modeYazili", "modeSesli")
                 and r["k"].startswith("QQuickRectangle") and r["v"]}
        self.assertEqual(fills, {"modeSesli": colour("line2")})
        self.set(composer=make_composer("typing", "bir\niki\nüç", True))     # grows with the text
        scroll = self.item("composerScroll")
        self.near(scroll.height(), 3 * scroll.property("lineH"))

    def test_a2_colour_in_two_places(self) -> None:
        self.open_bar()
        rows = self.rows()
        self.assertEqual(self.row("sendButton", rows)["color"], colour("surface2"))
        self.assertEqual(self.row("sendGlyph", rows)["color"], colour("disabled_glyph"))
        self.set(composer=VIEW["typing"])
        rows = self.check_frame(WIDTHS["window"], VIEW["typing"])
        self.assertEqual(self.row("sendButton", rows)["color"], colour("accent"))
        self.assertEqual(self.row("sendGlyph", rows)["color"], colour("accent_ink"))
        self.set(composer=VIEW["listening"], levels=[0.3, 0.0])
        rows = self.check_frame(WIDTHS["window"], VIEW["listening"])
        face, halo, talk = (self.row(n, rows) for n in ("talkFace", "talkHalo", "talkButton"))
        self.assertEqual((face["color"], face["bc"], halo["color"]),
                         (colour("accent_surface"), colour("accent"), colour("accent_line")))
        self.near(halo["w"] - talk["w"], 2 * 4)
        self.set(levels=[0.3, 1.0])                                        # the halo widens with the newest level
        rows = self.rows()
        self.near(self.row("talkHalo", rows)["w"] - self.row("talkButton", rows)["w"], 2 * 8)

    def test_a3_states(self) -> None:
        width = WIDTHS["window"]
        self.open_bar()
        self.set(composer=VIEW["listening"], levels=LEVELS)
        rows = self.check_frame(width, VIEW["listening"])
        found = bars(rows)
        self.assertEqual(len(found), len(LEVELS))
        self.assertEqual({b["w"] for b in found}, {3})
        xs = sorted(b["x"] for b in found)
        self.assertEqual({round(b - a, 3) for a, b in zip(xs, xs[1:])}, {6})
        self.assertGreater(len({b["h"] for b in found}), 2)
        self.assertEqual((self.row("waveFlat", rows)["v"], self.row("composerScroll", rows)["v"]), (False, False))
        self.assertEqual(self.texts(rows, "listeningRow"), ["Dinliyorum"])
        self.assertEqual(self.row("talkGlyph", rows)["radius"], 2)       # the stop square
        clock = self.row("talkClock", rows)
        self.assertEqual((clock["v"], clock["text"], clock["font"]), (True, "1:05", "JetBrains Mono"))
        self.assertEqual((self.texts(rows, "talkButton"), self.texts(rows, "cancelTalk")), (["Bitir"], ["Vazgeç"]))
        self.set(levels=[])
        rows = self.rows()
        self.assertEqual((self.row("waveFlat", rows)["v"], bars(rows)), (True, []))
        self.set(composer=VIEW["transcribing"])
        rows = self.check_frame(width, VIEW["transcribing"])
        line, label = self.row("transcribingLine", rows), self.row("transcribingLabel", rows)
        self.assertEqual((line["v"], line["h"]), (True, 2))
        self.assertEqual((label["v"], label["text"], label["color"]), (True, "Yazıya çevriliyor…", colour("text2")))
        self.assertFalse(self.row("talkButton", rows)["en"])
        self.assertTrue(self.item("transcribingLine").property("moving"))
        self.set(composer=VIEW["understood"])
        rows = self.check_frame(width, VIEW["understood"])
        self.assertEqual(self.texts(rows, "understoodStrip"), ["Anladığım"])
        self.assertEqual(self.row("correctionSummary", rows)["text"], "üç → üçte")
        self.assertTrue(self.row("composerScroll", rows)["v"])
        self.assertFalse(self.item("composerText").property("readOnly"))


class Behaviour(Rig):
    """Acceptance 4-8 and negatives 3-5: clicks, availability, keys, the link notice, reduced motion, input."""
    near = Look.near

    def press(self, key, modifier=Qt.NoModifier) -> None:
        QTest.keyClick(self.win, key, modifier)
        QTest.qWait(5)

    def sends(self) -> int:
        return self.face.calls.count(("send",))

    def shown(self, *names) -> tuple:
        rows = self.rows()
        return tuple(self.row(n, rows)["v"] for n in names)

    def names(self, rows: list) -> dict:
        return {r["on"]: r["acc"] for r in rows if "on" in r and r["v"]}

    def commit(self, text: str) -> None:
        ev = QInputMethodEvent()                                            # an input-method commit, not a key
        ev.setCommitString(text)
        QGuiApplication.sendEvent(self.win, ev)
        QTest.qWait(5)

    def test_a4_clicks_call_only_face_slots(self) -> None:
        self.open_bar(composer=VIEW["listening"], levels=LEVELS)
        for name in ("talkButton", "cancelTalk", "modeYazili", "modeSesli"):
            self.click(name)
        self.set(composer=VIEW["typing"])
        self.click("sendButton")
        self.set(composer=VIEW["understood"])
        self.click("undoCorrections")
        self.click("attachButton")
        self.set(composer=VIEW["transcribing"])
        self.click("talkButton")                                            # disabled while transcribing
        self.assertEqual(self.face.calls, [("talkToggle", "button"), ("talkCancel",), ("modeSet", "yazili"),
                                           ("modeSet", "sesli"), ("send",), ("undoCorrections",)])
        self.assertEqual(self.win.property("attachCount"), 1)

    def test_a5_no_dead_controls(self) -> None:
        self.open_bar(talkAvailable=False, attachAvailable=False)
        self.assertEqual(self.shown("talkButton", "attachButton"), (False, False))
        self.check_frame(WIDTHS["window"], make_composer())
        self.set(talkAvailable=True, attachAvailable=True)
        self.assertEqual(self.shown("talkButton", "attachButton"), (True, True))
        self.close_all()
        self.open_bar("mini")
        self.assertEqual(self.shown("talkButton", "attachButton"), (True, False))
        self.check_frame(WIDTHS["mini"], make_composer())

    def test_a6_enter_sends_shift_enter_breaks_the_line(self) -> None:
        self.open_bar(composer=VIEW["typing"])
        self.item("composerText").forceActiveFocus()
        self.press(Qt.Key_Return)
        self.press(Qt.Key_Enter, Qt.KeypadModifier)
        self.assertEqual(self.sends(), 2)
        for key, mod in ((Qt.Key_Escape, Qt.NoModifier), (Qt.Key_Return, Qt.ControlModifier),
                         (Qt.Key_Return, Qt.AltModifier)):
            self.press(key, mod)
        self.assertEqual(self.sends(), 2)                                   # no other key is bound
        self.set(composer=make_composer())
        self.face.calls.clear()
        self.press(Qt.Key_Return)
        self.assertEqual(self.face.calls, [])                               # Enter with nothing to send
        self.press(Qt.Key_A)
        self.press(Qt.Key_Return, Qt.ShiftModifier)
        self.press(Qt.Key_B)
        self.assertEqual(self.sends(), 0)
        self.assertEqual(self.face.calls[-1][1].splitlines(), ["a", "b"])

    def test_a7_unreachable_link_keeps_the_text(self) -> None:
        self.open_bar(composer=VIEW["typing"], link="unreachable")
        rows = self.check_frame(WIDTHS["window"], VIEW["typing"], "unreachable")
        notice, body = self.row("linkNotice", rows), self.row("composerBody", rows)
        self.assertEqual((notice["v"], notice["text"], notice["color"]),
                         (True, "Predator'a ulaşılamıyor", colour("text2")))
        self.assertEqual(self.item("linkNotice").property("lineCount"), 1)
        self.assertLessEqual(notice["y"] + notice["h"], body["y"])           # above the bar
        self.assertEqual(self.row("sendButton", rows)["color"], colour("surface2"))
        self.click("sendButton")
        self.item("composerText").forceActiveFocus()
        self.press(Qt.Key_Return)
        self.assertEqual(self.face.calls, [])
        self.assertEqual(self.row("composerText", self.rows())["text"], VIEW["typing"]["text"])
        self.set(link="ok")
        self.assertEqual(self.shown("linkNotice"), (False,))

    def test_a8_reduced_motion_is_still(self) -> None:
        self.open_bar(reduced=True, composer=VIEW["listening"], levels=[1.0])
        rows = self.rows()
        self.near(self.row("talkHalo", rows)["w"] - self.row("talkButton", rows)["w"], 2 * 4)
        self.assertEqual((self.row("waveFlat", rows)["v"], bars(rows)), (True, []))
        self.set(composer=VIEW["transcribing"])
        line, runner = self.item("transcribingLine"), self.item("transcribingRunner")
        start = runner.x()
        QTest.qWait(200)
        self.assertEqual((line.property("moving"), runner.x(), runner.width()), (False, start, line.width()))
        self.close_all()
        self.open_bar(composer=VIEW["transcribing"])
        runner = self.item("transcribingRunner")
        start = runner.x()
        QTest.qWait(250)
        self.assertTrue(self.item("transcribingLine").property("moving"))
        self.assertNotEqual(runner.x(), start)

    def test_n3_dim_send_calls_nothing_and_a_double_click_sends_once(self) -> None:
        self.open_bar()
        self.click("sendButton")
        self.click("sendButton", double=True)
        self.assertEqual(self.face.calls, [])
        self.set(composer=VIEW["typing"])
        self.click("sendButton", double=True)
        self.assertEqual(self.face.calls, [("send",)])

    def test_n4_eight_line_cap_and_a_megabyte_paste(self) -> None:
        text = "\n".join(f"satır {i}" for i in range(12))
        self.open_bar(composer=make_composer("typing", text, True))
        field, edit = self.item("composerScroll"), self.item("composerText")
        line = field.property("lineH")
        self.near(field.height(), 8 * line)
        self.assertGreater(edit.height(), field.height() + line)
        edit.setProperty("cursorPosition", len(text))
        QTest.qWait(20)
        self.assertGreater(field.property("contentY"), 0)                   # scrolls inside, following the cursor
        self.set(composer=make_composer())
        self.face.calls.clear()
        payload = (("Merhaba ğüşİıöç dünya. " * 4 + "\n") * 12000)[:1_000_000]
        began = time.perf_counter()                                         # monotonic, not a wall-clock read
        QMetaObject.invokeMethod(edit, "insert", Q_ARG(int, 0), Q_ARG(str, payload))
        QTest.qWait(20)
        self.assertLess(time.perf_counter() - began, 20)
        self.assertEqual(len(self.face.calls), 1)
        self.assertTrue(self.face.calls[0] == ("textEdited", payload), "the paste reaches textEdited unchanged")
        self.assertLessEqual(field.height(), 8 * line + 1)

    def test_n5_exact_labels_and_names(self) -> None:
        self.open_bar()
        rows = self.check_frame(WIDTHS["window"], make_composer())
        self.assertEqual(self.names(rows), {"talkButton": "Konuş", "attachButton": "Ekle", "modeYazili": "Yazılı",
                                            "modeSesli": "Sesli", "sendButton": "Gönder", "composerText": "Mesaj"})
        self.assertEqual([self.texts(rows, n) for n in ("talkButton", "modeYazili", "modeSesli", "attachButton")],
                         [["Konuş"], ["Yazılı"], ["Sesli"], ["+"]])
        self.set(composer=VIEW["listening"])
        rows = self.check_frame(WIDTHS["window"], VIEW["listening"])
        names = self.names(rows)
        self.assertEqual((names["talkButton"], names["cancelTalk"]), ("Bitir", "Vazgeç"))
        self.assertEqual((self.texts(rows, "talkButton"), self.texts(rows, "cancelTalk")), (["Bitir"], ["Vazgeç"]))
        self.set(composer=VIEW["understood"])
        rows = self.check_frame(WIDTHS["window"], VIEW["understood"])
        self.assertEqual(self.texts(rows, "understoodStrip"), ["Anladığım"])
        self.assertEqual((self.texts(rows, "undoCorrections"), self.names(rows)["undoCorrections"]),
                         (["Düzeltmeleri geri al"], "Düzeltmeleri geri al"))

    def test_adversarial_model_values(self) -> None:
        html = "<b>kalın</b> <a href='x'>bağ</a> &amp;"
        cases = [{"state": 5}, {"canSend": "yes", "text": "x"}, {"canSend": 1}, {"text": None}, {"text": 3},
                 {"state": "listening", "elapsed": "abc"}, {"state": "listening", "elapsed": -5},
                 {"state": "listening", "elapsed": 1e13}, {"state": "understood", "corrections": "x"},
                 {"state": "understood", "corrections": [[1, 2], None, ["a"]]}, {"mode": 7},
                 {"state": "listening", "talkLabel": None}, {"state": "listening", "talkLabel": html},
                 {"text": html}, {"state": "understood", "corrections": [[html, "<i>x</i>"]]}]
        self.open_bar()
        for kw in cases:
            with self.subTest(kw=kw):
                composer = {**make_composer(), **kw}
                self.set(composer=composer)
                rows = self.check_frame(WIDTHS["window"], composer)
                if "elapsed" in kw:
                    self.assertEqual(self.row("talkClock", rows)["text"], "")
                if kw.get("text") == html:
                    self.assertEqual(self.row("composerText", rows)["text"], html)
                if "talkLabel" in kw:
                    self.assertEqual(self.texts(rows, "talkButton"), [kw["talkLabel"] or "Konuş"])
                if kw.get("state") == "understood":
                    want = html + " → <i>x</i>" if kw["corrections"] == [[html, "<i>x</i>"]] else ""
                    self.assertEqual(self.row("correctionSummary", rows)["text"], want)
        self.set(composer=VIEW["listening"], levels=[math.nan, 5, -1, "x", None])
        rows = self.check_frame(WIDTHS["window"], VIEW["listening"])
        self.assertEqual(len(bars(rows)), 5)
        self.set(composer={}, levels=[])
        self.check_frame(WIDTHS["window"], {})
        self.assertEqual(self.face.calls, [])

    def key(self, key, modifier=Qt.NoModifier, repeat=False, kind=QEvent.Type.KeyPress) -> None:
        QGuiApplication.sendEvent(self.win, QKeyEvent(kind, key.value, modifier, "\r", repeat))
        QTest.qWait(5)

    def test_n6_hidden_text_takes_no_input(self) -> None:
        typed = VIEW["typing"]["text"]
        keys = ((Qt.Key_X, Qt.NoModifier), (Qt.Key_Return, Qt.NoModifier), (Qt.Key_Space, Qt.NoModifier),
                (Qt.Key_Backspace, Qt.NoModifier), (Qt.Key_Delete, Qt.NoModifier), (Qt.Key_Z, Qt.ControlModifier),
                (Qt.Key_Return, Qt.ShiftModifier), (Qt.Key_Enter, Qt.KeypadModifier), (Qt.Key_Tab, Qt.NoModifier))
        for width in WIDTHS:
            with self.subTest(width=width):
                self.open_bar(width, composer=VIEW["typing"])
                edit = self.item("composerText")
                edit.forceActiveFocus()
                self.assertTrue(edit.hasActiveFocus())
                for state in ("listening", "transcribing"):
                    self.face.calls.clear()
                    self.set(composer=make_composer(state, typed, True))    # the CDX repro: focused, then hidden
                    edit.forceActiveFocus()                                 # refused while hidden
                    for key, mod in keys:
                        self.press(key, mod)
                    self.commit("q")
                    self.click("composerScroll")
                    self.assertEqual(self.face.calls, [], state)
                    self.assertEqual((edit.hasActiveFocus(), edit.isEnabled(), edit.property("text")),
                                     (False, False, typed))
                    self.set(composer=make_composer("typing", typed, True))
                    self.assertTrue(edit.hasActiveFocus(), "focus comes back with the field")
                    self.press(Qt.Key_Return)
                    self.assertEqual(self.face.calls, [("send",)])
                self.face.calls.clear()
                self.item("composerBody").setVisible(False)                 # hidden by an ancestor, not by state
                QTest.qWait(5)
                edit.forceActiveFocus()
                self.press(Qt.Key_X)
                self.press(Qt.Key_Return)
                self.commit("q")
                self.assertEqual((self.face.calls, edit.isEnabled()), ([], False))
                self.item("composerBody").setVisible(True)
                self.close_all()

    def test_n7_model_text_and_held_enter(self) -> None:
        for text in ("a\r\nb", "a\rb", "a b", "a b", "a b"):
            with self.subTest(text=repr(text)):
                self.open_bar(composer=make_composer("typing", text, True))
                self.set(composer=make_composer("typing", text + "c", True))
                self.assertEqual(self.face.calls, [])                       # Qt's own rewrite is not a user edit
                self.item("composerText").forceActiveFocus()
                self.press(Qt.Key_Z)
                self.assertEqual([c[0] for c in self.face.calls], ["textEdited"])
                self.close_all()
        self.open_bar(composer=VIEW["typing"])
        edit = self.item("composerText")
        edit.forceActiveFocus()
        for repeat in (False, True, True):
            self.key(Qt.Key_Return, repeat=repeat)
        self.key(Qt.Key_Enter, Qt.KeypadModifier, True)
        self.key(Qt.Key_Return, repeat=True, kind=QEvent.Type.KeyRelease)
        self.assertEqual(self.face.calls, [("send",)])                      # a held Enter sends once
        self.assertEqual(edit.property("text"), VIEW["typing"]["text"])     # and adds no line

    def test_n8_stopped_runner_starts_at_zero(self) -> None:
        self.open_bar(composer=VIEW["transcribing"])
        line, runner = self.item("transcribingLine"), self.item("transcribingRunner")
        QTest.qWait(250)
        self.assertTrue(line.property("moving"))
        self.set(tokens=make_tokens(reduced=True))                          # reduced motion switched on mid-run
        self.assertEqual((line.property("moving"), runner.x(), runner.width()), (False, 0, line.width()))
        self.close_all()
        self.open_bar(composer=VIEW["transcribing"])
        line, runner = self.item("transcribingLine"), self.item("transcribingRunner")
        QTest.qWait(250)
        self.set(composer=VIEW["typing"])
        self.set(tokens=make_tokens(reduced=True))
        self.set(composer=VIEW["transcribing"])                             # a still transcribe after an animated one
        self.assertEqual((line.property("moving"), runner.x(), runner.width()), (False, 0, line.width()))

    def test_n9_edit_back_to_model_text_is_reported(self) -> None:
        self.open_bar(composer=make_composer("typing", "ab", True))
        self.face.echo = False
        self.item("composerText").forceActiveFocus()
        for k in (Qt.Key_End, Qt.Key_C, Qt.Key_Backspace):
            self.press(k)
        self.assertEqual(self.face.calls, [("textEdited", "abc"), ("textEdited", "ab")])   # ending on the model text is an edit too


def shots() -> None:
    """Offscreen screenshots of each state into build/face_shots/FACEBAR1/ (gitignored) plus a contact sheet."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    plan = [("window", s, False, {"composer": VIEW[s], "levels": LEVELS}) for s in VIEW] + [
        ("window", "listening-flat", False, {"composer": VIEW["listening"]}),
        ("window", "typing-unreachable", False, {"composer": VIEW["typing"], "link": "unreachable"}),
        ("full", "understood", False, {"composer": VIEW["understood"]}),
        ("mini", "listening", False, {"composer": VIEW["listening"], "levels": LEVELS}),
        ("window", "listening-reduced", True, {"composer": VIEW["listening"], "levels": LEVELS})]
    images = []
    for key, label, reduced, values in plan:
        _face, engine, win = open_host(key, reduced, **values)
        QTest.qWait(100)
        top = max(0, int(find(win, "composer").mapToScene(QPointF(0, 0)).y()) - 8)
        image = win.grabWindow()
        image = image.copy(0, top, image.width(), image.height() - top)
        image.save(str(SHOTS / f"{key}-{label}.png"))
        images.append(image)
        win.close()
        win.deleteLater()
        engine.deleteLater()
        QTest.qWait(1)
    sheet = QImage(max(i.width() for i in images) + 16, sum(i.height() + 8 for i in images) + 8,
                   QImage.Format.Format_RGB32)
    sheet.fill(QColor(COLORS["line1"]))
    painter = QPainter(sheet)
    y = 8
    for image in images:
        painter.drawImage(8, y, image)
        y += image.height() + 8
    painter.end()
    sheet.save(str(SHOTS / "contact_sheet.png"))
    print(f"{len(images)} shots and contact_sheet.png in {SHOTS}")


if __name__ == "__main__":
    if "--shots" in sys.argv:
        shots()
    else:
        unittest.main()
