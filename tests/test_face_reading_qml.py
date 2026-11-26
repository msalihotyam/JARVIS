"""FACEREAD1: canvas frame 1, the reading view - one 640 px reading column, no chat bubbles, an open page.

* Salih's turns are thin panels; JARVIS answers are open article text with a row of text buttons.
* It draws face.rows only and calls face slots; here it runs against a fake `face` (no backend).
* Typed refusal: an unknown or malformed block draws nothing and throws nothing; one QML warning fails.
* Nothing is wider than the column; a wide table or code block scrolls sideways inside its own box.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject, QPoint, QPointF, Property,  # noqa: E402
                            Qt, QtMsgType, QUrl, Signal, Slot, qInstallMessageHandler)
from PySide6.QtGui import QFont, QGuiApplication, QWheelEvent  # noqa: E402
from PySide6.QtQml import QQmlExpression  # noqa: E402
from PySide6.QtQuick import QQuickView  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

_APP = QGuiApplication.instance() or QGuiApplication([])

ROOT = Path(__file__).resolve().parents[1]
QML = ROOT / "jarvis" / "face" / "qml"
OWNED = ("ReadingView.qml", "SalihTurn.qml", "JarvisTurn.qml", "Blocks.qml")
COLUMN, LEFT, HIT = 640, 130, 44                  # the test window is 900 wide, so the column starts at 130
LONG = "Ü" * 10_000                               # one 10 000-character word

COLORS = {"base": "#0B0E13", "surface1": "#11161D", "surface2": "#171D26", "surface3": "#0E1218",
          "line1": "#1B222C", "line2": "#232B36", "text1": "#E7ECF2", "text2": "#AAB4C0", "text3": "#8793A1",
          "text_soft": "#9AA5B2", "accent": "#5BC8F5", "accent_ink": "#06202C", "accent_surface": "#0F1C24",
          "accent_line": "#1D3A4A", "needs_you": "#F2B34A", "error": "#F2777F", "ok": "#6FD3A3",
          "disabled_glyph": "#5E6978", "link": "#7FD3F7", "link_hover": "#A9E2FA"}
TYPE = {"body": (17, 28), "h2": (22, 30), "h3": (17, 26), "code": (14.5, 22), "small": (13, 20),
        "label": (12.5, 16), "turn": (16, 26), "cell": (15, 24), "meta": (13, 20), "control": (14, 20)}
TOKENS: dict = {f"color_{k}": v for k, v in COLORS.items()}
TOKENS.update({f"type_{k}_size": size for k, (size, _) in TYPE.items()})
TOKENS.update({f"type_{k}_line": line for k, (_, line) in TYPE.items()})
TOKENS.update({"radius_panel": 12, "radius_composer": 16, "radius_control": 12, "radius_code": 8, "radius_pill": 999})
TOKENS.update({f"space_{n}": n for n in (4, 6, 8, 10, 12, 14, 16, 20, 24)})
TOKENS.update({"motion_fast": 120, "motion_base": 200, "motion_slow": 320, "motion_curve": [0.2, 0, 0, 1]})
COMPOSER = {"state": "idle", "text": "", "canSend": False, "sendReason": "", "talkLabel": "", "elapsed": 0,
            "mode": "text", "corrections": []}

ROLES = ("entryId", "kind", "who", "sourceLabel", "time", "voiceSeconds", "readMinutes", "blocks", "cardId", "chips")
DEFAULTS = {"entryId": "", "kind": "turn", "who": "jarvis", "sourceLabel": "", "time": "", "voiceSeconds": -1,
            "readMinutes": 0, "blocks": [], "cardId": "", "chips": []}
FIRST_ROLE = 0x0101                               # Qt.UserRole + 1


class Rows(QAbstractListModel):
    """face.rows as the view sees it: ten roles, one dict per row, a default for every missing role."""

    def __init__(self, rows=()) -> None:
        super().__init__()
        self._rows = [dict(r) for r in rows]

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def roleNames(self) -> dict:
        return {FIRST_ROLE + i: name.encode() for i, name in enumerate(ROLES)}

    def data(self, index, role=0):
        k = getattr(role, "value", role) - FIRST_ROLE
        if not index.isValid() or not 0 <= index.row() < len(self._rows) or not 0 <= k < len(ROLES):
            return None
        return self._rows[index.row()].get(ROLES[k], DEFAULTS[ROLES[k]])

    def append(self, row: dict) -> None:
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
        self._rows.append(dict(row))
        self.endInsertRows()

    def extend_last(self, blocks: list) -> None:      # a streamed answer grows in place
        self._rows[-1]["blocks"] = blocks
        last = self.index(len(self._rows) - 1)
        self.dataChanged.emit(last, last)

    def reset(self, rows) -> None:
        self.beginResetModel()
        self._rows = [dict(r) for r in rows]
        self.endResetModel()


class Face(QObject):
    """The fake `face`: tokens, rows, the rest of the contract, and the eight slots, each call recorded."""

    talkChanged = Signal()

    def __init__(self, rows: Rows, talk: bool = False) -> None:
        super().__init__()
        self._rows, self._talk, self.calls = rows, talk, []

    def set_talk(self, on: bool) -> None:
        self._talk = on
        self.talkChanged.emit()

    tokens = Property("QVariantMap", lambda self: TOKENS, constant=True)
    rows = Property(QObject, lambda self: self._rows, constant=True)
    composer = Property("QVariantMap", lambda self: dict(COMPOSER), constant=True)
    link = Property(str, lambda self: "ok", constant=True)
    talkAvailable = Property(bool, lambda self: self._talk, notify=talkChanged)
    attachAvailable = Property(bool, lambda self: False, constant=True)
    levels = Property("QVariantList", lambda self: [], constant=True)

    @Slot(str)
    def textEdited(self, text: str) -> None:
        self.calls.append(("textEdited", text))

    @Slot()
    def send(self) -> None:
        self.calls.append(("send",))

    @Slot(str)
    def talkToggle(self, origin: str) -> None:
        self.calls.append(("talkToggle", origin))

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
    def copyText(self, entry: str) -> None:
        self.calls.append(("copyText", entry))

    @Slot(str)
    def linkActivated(self, url: str) -> None:
        self.calls.append(("linkActivated", url))


_SEEN: list = []                                  # network, process or live-database events seen while a view is up
_RECORD = [False]
_WATCH = ("socket.", "urllib.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.startfile", "webbrowser.")


def _audit(event, args):
    if _RECORD[0] and (event.startswith(_WATCH) or (event == "open" and "jarvis.db" in str(args[0] if args else ""))):
        _SEEN.append(event)


sys.addaudithook(_audit)


def pump(ms=30):
    QTest.qWait(ms)


def walk(item):
    yield item
    for child in item.childItems():
        yield from walk(child)


def num(value):
    return getattr(value, "value", value)


def source(name):
    return (QML / name).read_text(encoding="utf-8")


def no_comments(text):
    return re.sub(r"//[^\n]*", "", text)


def code_only(text):                              # comments and string literals removed
    return re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', no_comments(text))


class StaticContractTests(unittest.TestCase):
    """The QML contract read from the source: what the files may name, draw with and react to."""

    def test_imports_and_no_outside_world(self):
        allowed = {"import QtQuick", "import QtQuick.Templates as T"}
        banned = re.compile(r"openUrlExternally|XMLHttpRequest|LocalStorage|FileDialog|WebSocket|"
                            r"\bQt\.(?:include|createComponent|createQmlObject|resolvedUrl)\b|\bsource\s*:|\bfetch\s*\(")
        for name in OWNED:
            with self.subTest(name):
                text = source(name)
                imports = {line.strip() for line in text.splitlines() if line.strip().startswith("import ")}
                self.assertLessEqual(imports, allowed)
                self.assertIsNone(banned.search(code_only(text)))

    def test_colours_and_sizes_come_from_tokens(self):
        named = {"ReadingView.qml": {"readonly property int columnWidth: 640", "readonly property int hitMin: 44"}}
        for name in OWNED:
            with self.subTest(name):
                text = no_comments(source(name))
                self.assertIsNone(re.search(r"#[0-9A-Fa-f]{3,8}\b", text))
                self.assertIsNone(re.search(r"\bQt\.(?:rgba|hsla|hsva|lighter|darker|tint|color)\b", text))
                self.assertIsNone(re.search(r'\bcolor\s*:\s*"(?!transparent")', text))
                for line in code_only(source(name)).splitlines():
                    if line.strip() in named.get(name, set()):
                        continue
                    bare = re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", line)
                    self.assertLessEqual(set(bare), {"0", "1", "2"}, line)

    def test_every_text_is_plain(self):
        for name in OWNED:
            with self.subTest(name):
                text = code_only(source(name))
                texts = len(re.findall(r"\b(?:Text|TextEdit)\s*\{", text))
                plain = len(re.findall(r"textFormat:\s*(?:Text|TextEdit)\.PlainText\b", text))
                self.assertEqual(texts, plain)
                self.assertIsNone(re.search(r"RichText|StyledText|MarkdownText|AutoText|onLinkActivated", text))

    def test_no_motion_timer_or_press_and_hold(self):
        banned = re.compile(r"\b(?:Timer|onPressAndHold|pressAndHold|Behavior|Transition|transitions|states|\w*Animation)\b")
        for name in OWNED:
            with self.subTest(name):
                self.assertIsNone(banned.search(code_only(source(name))))

    def test_every_clickable_has_a_name(self):
        names = props = areas = 0
        for name in OWNED:
            text = no_comments(source(name))
            props += len(re.findall(r"property string accessibleName\b", text))
            names += len(re.findall(r"Accessible\.name:\s*accessibleName\b", text))
            areas += len(re.findall(r"\bMouseArea\s*\{", text))
        self.assertEqual(props, names)
        self.assertGreater(areas, 0)
        self.assertLessEqual(areas, names)

    def test_no_other_module_named(self):
        words = [w + "_" for w in ("talk", "telegram", "brain")]
        files = [QML / n for n in OWNED] + [Path(__file__), ROOT / "scripts" / "test_durations.d" / "FACEREAD1.json"]
        for path in files:
            text = path.read_text(encoding="utf-8")
            for word in words:
                self.assertNotIn(word, text, path.name)


def para(text, **span):
    return {"t": "p", "spans": [{"s": text, **span}]}


def salih(eid, text, **row):
    return {"entryId": eid, "kind": "turn", "who": "salih", "sourceLabel": "PC", "time": "09:41",
            "blocks": [{"t": "p", "spans": [{"s": text}]}], **row}


def jarvis(eid, blocks, **row):
    return {"entryId": eid, "kind": "turn", "who": "jarvis", "time": "09:42", "blocks": list(blocks), **row}


def chat(n):
    return [salih(f"s{i}", f"Soru {i}") if i % 2 == 0 else jarvis(f"j{i}", [para("Uzun bir cevap satırı. " * 12)])
            for i in range(n)]


_NOISE: list = []


def _qt_message(mode, context, message):          # a QML message reaching Qt's log fails the running test
    if mode != QtMsgType.QtInfoMsg and re.search(r"\.qml|QML|Binding loop|Polish loop|Layout", message):
        _NOISE.append(message)


qInstallMessageHandler(_qt_message)

C = {k: v.lower() for k, v in COLORS.items()}
TABLE = {"t": "table", "head": ["Ad", "Değer"], "rows": [["bir", "1"], ["iki", "2"]], "align": ["left", "right"]}
STYLED = {"t": "p", "spans": [{"s": "Kalın", "b": True}, {"s": " ve "}, {"s": "kod", "c": True}, {"s": " ile "},
                              {"s": "buraya", "href": "ornek.test/yol"}, {"s": " git."}]}
ALL_BLOCKS = [{"t": "h", "level": 2, "spans": [{"s": "Başlık iki"}]}, {"t": "h", "level": 3, "text": "Başlık üç"},
              para("Düz paragraf."), STYLED,
              {"t": "ul", "items": ["bir", {"spans": [{"s": "iki"}], "items": ["alt"]}]},
              {"t": "ol", "items": ["bir", {"text": "iki", "items": ["alt"]}]},
              TABLE, {"t": "code", "lang": "py", "text": "print('merhaba')"},
              {"t": "note", "spans": [{"s": "Bir not."}]}, {"t": "rule"}]
WIDE = {"t": "table", "head": [f"Sütun {i}" for i in range(30)], "rows": [[f"değer {i}" for i in range(30)]] * 2}
WIDE_CASES = {
    "salih word": [salih("s0", LONG)],
    "answer word": [jarvis("j0", [para(LONG)])],
    "styled word": [jarvis("j0", [para(LONG, b=True)])],
    "heading word": [jarvis("j0", [{"t": "h", "level": 2, "spans": [{"s": LONG}]}])],
    "list word": [jarvis("j0", [{"t": "ul", "items": [LONG, {"text": "a", "items": [LONG]}]}])],
    "note word": [jarvis("j0", [{"t": "note", "spans": [{"s": LONG}]}])],
    "cell word": [jarvis("j0", [{"t": "table", "head": ["a"], "rows": [[LONG]]}])],
    "wide table": [jarvis("j0", [WIDE])],
    "wide code": [jarvis("j0", [{"t": "code", "text": LONG}])],
}
BAD = [None, 7, "metin", [], {}, {"t": "video"}, {"t": "h", "level": "x", "spans": 5},
       {"t": "p", "spans": [None, 3, {"s": None}, {"s": "ok", "href": 9}]}, {"t": "ul", "items": [None, 4, {"items": "x"}]},
       {"t": "table", "head": "x", "rows": [None, "y", [1, None]], "align": 5}, {"t": "code", "text": 5},
       {"t": "note"}, {"t": "rule", "level": []}]


def spread(item):                                 # every visible item, a sideways-scrolling box counted as one
    if not item.isVisible():
        return
    yield item
    if item.objectName() in ("tableBox", "codeBox"):
        return
    for child in item.childItems():
        yield from spread(child)


class ReadingViewTests(unittest.TestCase):
    """The reading view against the fake face: layout, type, width, actions, scrolling and refusal."""

    def setUp(self):
        self.warnings, self.view = [], None
        _NOISE.clear()
        _SEEN.clear()
        _RECORD[0] = True

    def tearDown(self):
        _RECORD[0] = False
        self._close()
        self.assertEqual(self.warnings + _NOISE, [])
        self.assertEqual(_SEEN, [])

    def load(self, rows=(), talk=False):
        self.rows, self.view = Rows(rows), QQuickView()
        self.face = Face(self.rows, talk)
        self.view.engine().warnings.connect(lambda found: self.warnings.extend(w.toString() for w in found))
        self.view.rootContext().setContextProperty("face", self.face)
        self.view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        self.view.resize(900, 700)
        self.view.setSource(QUrl.fromLocalFile(str(QML / "ReadingView.qml")))
        self.assertEqual(self.view.status(), QQuickView.Status.Ready, [e.toString() for e in self.view.errors()])
        self.view.show()
        pump(80)
        self.root = self.view.rootObject()
        self.flick = self.one("readingList")

    def _close(self):
        if self.view is not None:
            self.view.close()
            self.view.deleteLater()
            self.view = None
            pump(10)

    def at(self, item):                           # the item's rectangle in the window
        p = item.mapToScene(QPointF(0, 0))
        return p.x(), p.y(), item.width(), item.height()

    def items(self, name=None, kind=None, under=None):
        found = [i for i in walk(under or self.root) if i.isVisible()
                 and (name is None or i.objectName() == name) and (kind is None or i.inherits(kind))]
        return sorted(found, key=lambda i: (round(self.at(i)[1]), self.at(i)[0]))

    def one(self, name, under=None):
        found = self.items(name, under=under)
        self.assertEqual(len(found), 1, name)
        return found[0]

    def color(self, item):
        return item.property("color").name()

    def js(self, item, expr):       # enums and grouped values PySide cannot convert are read through QML itself
        result = QQmlExpression(self.view.rootContext(), item, expr).evaluate()
        return result[0] if isinstance(result, tuple) else result

    def font(self, item):
        return item.property("font")

    def shown(self, blocks):                      # the blocks a Blocks column draws
        return [c for c in blocks.childItems() if c.inherits("QQuickLoader") and c.isVisible()]

    def click(self, item):
        c = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(c.x()), int(c.y())))
        pump()

    def wheel(self, notches):                     # positive notches scroll up, towards older turns
        pos = QPointF(450, 350)
        event = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, 120 * notches), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QGuiApplication.sendEvent(self.view, event)
        pump()

    def test_layout_column_padding_and_gaps(self):
        self.load([salih("s0", "Merhaba"), jarvis("j0", [para("Cevap.")]), salih("s1", "İkinci soru")])
        boxes = [self.at(b) for b in self.items("turnBody")]
        self.assertEqual([(x, w) for x, _, w, _ in boxes], [(LEFT, COLUMN)] * 3)
        self.assertEqual(boxes[0][1], TOKENS["space_16"])
        self.assertEqual(boxes[1][1] - boxes[0][1] - boxes[0][3], TOKENS["space_24"])
        self.assertEqual(boxes[2][1] - boxes[1][1] - boxes[1][3], TOKENS["space_12"])

    def test_salih_turn_is_a_thin_panel(self):
        self.load([salih("s0", "Bugün ne var?", voiceSeconds=75)])
        panel, content = self.one("salihTurn"), self.one("salihContent")
        self.assertEqual((self.color(panel), self.js(panel, "String(border.color)"), self.js(panel, "border.width")),
                         (C["surface1"], C["line1"], 1))
        self.assertEqual(panel.property("radius"), TOKENS["radius_panel"])
        (px, py, pw, ph), (cx, cy, cw, ch) = self.at(panel), self.at(content)
        self.assertEqual((cy - py, cx - px, px + pw - cx - cw, py + ph - cy - ch), (14, 20, 20, 12))
        who, pill, time, voice = (self.one(n) for n in ("salihWho", "sourcePill", "salihTime", "voiceText"))
        self.assertEqual((who.property("text"), self.color(who), self.font(who).weight()), ("Sen", C["text1"], QFont.Weight.DemiBold))
        label = pill.childItems()[0]
        self.assertEqual((self.color(pill), label.property("text"), self.color(label)), (C["surface2"], "PC", C["text2"]))
        self.assertEqual((time.property("text"), self.color(time), voice.property("text")), ("09:41", C["text3"], "sesli · 1:15"))
        self.assertTrue(self.one("micGlyph").isVisible())
        body = self.one("runText", under=self.one("salihBlocks"))
        self.assertEqual((body.property("text"), self.color(body)), ("Bugün ne var?", C["text1"]))
        self.assertLess(abs(self.font(body).pixelSize() - TYPE["turn"][0]), 1)

    def test_jarvis_answer_is_open_text_with_text_buttons(self):
        self.load([jarvis("j0", [para("Cevap.")], readMinutes=3), jarvis("j1", [para("İkinci.")])])
        first = self.items("jarvisTurn")[0]
        self.assertEqual(self.items(kind="QQuickRectangle", under=first), [])      # no panel, no bubble
        who, time = self.one("jarvisWho", under=first), self.one("jarvisTime", under=first)
        self.assertEqual((who.property("text"), self.color(who), self.font(who).weight()),
                         ("JARVIS", C["accent"], QFont.Weight.DemiBold))
        self.assertEqual((time.property("text"), self.color(time)), ("09:42", C["text3"]))
        self.assertEqual([t.property("text") for t in self.items("readTime")], ["yaklaşık 3 dk okuma"])
        buttons = self.items("copyButton") + self.items("collapseButton")
        self.assertEqual(len(buttons), 4)
        for button in buttons:
            self.assertEqual((button.height(), self.color(button.childItems()[0])), (HIT, C["text2"]))
        self.assertEqual({b.childItems()[0].property("text") for b in buttons}, {"Daralt", "Kopyala"})
        self.assertEqual(self.items("readAloudButton"), [])

    def test_headings_paragraph_and_styled_spans(self):
        self.load([jarvis("j0", ALL_BLOCKS)])
        heads = [self.one("runText", under=h) for h in self.items("headingBlock")]
        self.assertEqual([h.property("text") for h in heads], ["Başlık iki", "Başlık üç"])
        for head, key in zip(heads, ("h2", "h3")):
            self.assertLess(abs(self.font(head).pixelSize() - TYPE[key][0]), 1)
            self.assertEqual((self.font(head).weight(), self.color(head)), (QFont.Weight.DemiBold, C["text1"]))
        plain, styled = self.items("paraBlock")
        body = self.one("runText", under=plain)
        self.assertEqual((body.property("text"), self.color(body)), ("Düz paragraf.", C["text1"]))
        self.assertLess(abs(self.font(body).pixelSize() - TYPE["body"][0]), 1)
        flow = self.one("runFlow", under=styled)
        pieces = {p.property("text").strip(): p for p in self.items(kind="QQuickTextEdit", under=flow)}
        self.assertEqual(self.font(pieces["Kalın"]).weight(), QFont.Weight.DemiBold)
        code = pieces["kod"]
        self.assertEqual(self.font(code).family(), "Cascadia Mono")
        self.assertLess(abs(self.font(code).pixelSize() - TYPE["code"][0]), 1)
        self.assertEqual([self.color(r) for r in self.items(kind="QQuickRectangle", under=code)], [C["surface3"]])
        link = pieces["buraya"]
        self.assertEqual((self.color(link), self.font(link).underline()), (C["link"], True))
        self.assertEqual(self.color(pieces["ve"]), C["text1"])

    def test_lists_hang_their_markers_two_levels_deep(self):
        self.load([jarvis("j0", ALL_BLOCKS)])
        lists = self.items("listBlock")
        self.assertEqual(len(lists), 2)
        for block, marks, indent in zip(lists, (["•", "•", "◦"], ["1.", "2.", "1."]), (24, 32)):
            with self.subTest(marks[0]):
                x0 = self.at(block)[0]
                markers, texts = self.items("listMarker", under=block), self.items("runText", under=block)
                self.assertEqual([m.property("text") for m in markers], marks)
                self.assertEqual([t.property("text") for t in texts], ["bir", "iki", "alt"])
                self.assertEqual([self.at(m)[0] - x0 for m in markers], [0, 0, indent])
                self.assertEqual([self.at(t)[0] - x0 for t in texts], [indent, indent, 2 * indent])
                self.assertEqual({self.color(m) for m in markers}, {C["text2"]})

    def test_table_head_cells_and_row_rules(self):
        self.load([jarvis("j0", [TABLE])])
        box = self.one("tableBox")
        head = self.items("runText", under=self.one("tableHead"))
        self.assertEqual([h.property("text") for h in head], ["Ad", "Değer"])
        for cell in head:
            f = self.font(cell)
            self.assertEqual((self.color(cell), f.capitalization(), f.weight()),
                             (C["text3"], QFont.Capitalization.AllUppercase, QFont.Weight.Medium))
            self.assertLess(abs(f.pixelSize() - TYPE["label"][0]), 1)
        top = self.at(head[0])[1]
        cells = [c for c in self.items("runText", under=box) if self.at(c)[1] > top]
        self.assertEqual([c.property("text") for c in cells], ["bir", "1", "iki", "2"])
        for cell in cells:
            self.assertLess(abs(self.font(cell).pixelSize() - TYPE["cell"][0]), 1)
        self.assertEqual(self.js(cells[1], "Number(horizontalAlignment)"), 2)          # Qt.AlignRight
        rules = self.items("tableRule", under=box)
        self.assertEqual([(r.height(), self.color(r)) for r in rules], [(1, C["line1"])] * 2)
        for rule, first in zip(rules, cells[::2]):
            self.assertLess(self.at(head[0])[1], self.at(rule)[1])
            self.assertLessEqual(self.at(rule)[1] + 1, self.at(first)[1])
        self.assertEqual({r.objectName() for r in self.items(kind="QQuickRectangle", under=box)}, {"tableRule"})

    def test_code_note_and_rule(self):
        self.load([jarvis("j0", ALL_BLOCKS[7:])])
        block, code = self.one("codeBlock"), self.one("codeText")
        self.assertEqual((self.color(block), block.property("radius")), (C["surface3"], TOKENS["radius_code"]))
        self.assertEqual((code.property("text"), self.font(code).family()), ("print('merhaba')", "Cascadia Mono"))
        self.assertLess(abs(self.font(code).pixelSize() - TYPE["code"][0]), 1)
        note = self.one("notePanel")
        self.assertEqual((self.color(note), note.property("radius")), (C["surface1"], TOKENS["radius_panel"]))
        self.assertEqual(self.items(kind="QQuickRectangle", under=note), [note])        # no stripe
        self.assertEqual(self.color(self.one("runText", under=note)), C["text2"])
        rule = self.one("ruleLine")
        self.assertEqual((rule.height(), self.color(rule), rule.width()), (1, C["line1"], COLUMN))

    def test_nothing_is_wider_than_the_column(self):
        for name, rows in WIDE_CASES.items():
            with self.subTest(name):
                self.load(rows)
                boxes = []
                for body in self.items("turnBody"):
                    for item in spread(body):
                        x, _, w, _ = self.at(item)
                        if w > 0:
                            self.assertTrue(LEFT - 0.5 <= x and x + w <= LEFT + COLUMN + 0.5, (item.objectName(), x, w))
                        if item.objectName() in ("tableBox", "codeBox") and item.property("contentWidth") > item.width() + 0.5:
                            boxes.append(item)
                self.assertEqual(len(boxes), int(name.startswith("wide")))
                for box in boxes:
                    self.assertTrue(box.property("clip"))
                    self.assertEqual(len(self.items("sideScroll", under=box)), 1)
                self._close()

    def test_copy_collapse_and_read_aloud(self):
        self.load([jarvis("j0", ALL_BLOCKS[:3])])
        blocks, collapse = self.one("answerBlocks"), self.one("collapseButton")
        self.assertEqual(len(self.shown(blocks)), 3)
        self.click(collapse)
        self.assertEqual((len(self.shown(blocks)), collapse.childItems()[0].property("text")), (1, "Genişlet"))
        self.click(collapse)
        self.assertEqual((len(self.shown(blocks)), collapse.childItems()[0].property("text")), (3, "Daralt"))
        self.click(self.one("copyButton"))
        self.assertEqual(self.items("readAloudButton"), [])
        self.face.set_talk(True)
        pump()
        self.click(self.one("readAloudButton"))
        self.assertEqual(self.face.calls, [("copyText", "j0"), ("talkToggle", "j0")])

    def test_link_reports_its_address_only(self):
        self.load([jarvis("j0", [STYLED])])
        area = self.one("linkArea")
        piece = area.parentItem()
        self.assertEqual((piece.property("text").strip(), self.color(piece), self.font(piece).underline()),
                         ("buraya", C["link"], True))
        self.assertGreaterEqual(min(area.width(), area.height()), HIT)
        self.assertEqual(area.property("accessibleName"), "Bağlantı: ornek.test/yol")
        self.click(area)
        self.assertEqual(self.face.calls, [("linkActivated", "ornek.test/yol")])

    def test_salih_text_stays_literal(self):
        raw = "<b>kalın</b> **yıldız** [bağ](http://ornek.test) `kod` &amp; <img src=x>"
        styled = salih("s1", "", blocks=[{"t": "p", "spans": [{"s": "gizli", "b": True, "href": "ornek.test"}]}])
        self.load([salih("s0", raw), styled])
        texts = self.items("runText")
        self.assertEqual([t.property("text") for t in texts], [raw, "gizli"])
        self.assertEqual(self.items("runFlow") + self.items("linkArea"), [])
        self.assertEqual({self.font(t).weight() for t in texts}, {QFont.Weight.Normal})
        self.assertEqual({self.js(t, "Number(textFormat)") for t in texts}, {0})

    def test_text_selects_by_mouse_and_a_drag_never_scrolls(self):
        self.load(chat(40))
        self.assertFalse(self.flick.property("interactive"))
        inside = [t for t in self.items("runText") if self.at(t)[1] >= 0 and self.at(t)[1] + self.at(t)[3] <= 680]
        x, y, w, h = self.at(inside[-1])
        before = self.flick.property("contentY")
        start, end = QPoint(int(x + 2), int(y + h - 4)), QPoint(int(x + w - 4), int(y + 4))
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        for step in range(1, 6):
            QTest.mouseMove(self.view, start + (end - start) * step / 5)
            pump(10)
        QTest.mouseRelease(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        pump()
        self.assertNotEqual(inside[-1].property("selectedText"), "")
        self.assertEqual(self.flick.property("contentY"), before)
        self.assertTrue(all(t.property("selectByMouse") for t in self.items("runText")))

    def test_follows_the_bottom_while_there(self):
        self.load(chat(40))
        self.assertTrue(self.flick.property("atYEnd"))
        self.rows.append(jarvis("j40", [para("Yeni. " * 40)]))
        pump(60)
        self.assertTrue(self.flick.property("atYEnd"))
        self.rows.extend_last([para("Yeni. " * 120)])
        pump(60)
        self.assertTrue(self.flick.property("atYEnd"))
        self.assertEqual(self.items("newAnswerPill"), [])

    def test_scrolled_up_the_view_stays_and_a_pill_appears(self):
        self.load(chat(40))
        self.wheel(3)
        self.assertFalse(self.flick.property("atYEnd"))
        self.assertFalse(self.root.property("following"))
        anchor = next(r for r in self.items("readingRow") if 0 <= self.at(r)[1] <= 600)
        y = self.at(anchor)[1]
        self.rows.append(jarvis("j40", [para("Kısa.")]))
        pump(60)
        self.rows.extend_last([para("Uzayan cevap. " * 60)])
        pump(60)
        self.assertLess(abs(self.at(anchor)[1] - y), 0.5)
        self.assertFalse(self.flick.property("atYEnd"))
        pill = self.one("newAnswerPill")
        self.assertEqual((pill.height(), pill.property("accessibleName"), pill.childItems()[0].property("text")),
                         (HIT, "Yeni cevap", "Yeni cevap"))
        self.assertGreaterEqual(self.one("readingScroll").width(), HIT)
        self.click(pill)
        self.assertTrue(self.flick.property("atYEnd"))
        self.assertEqual(self.items("newAnswerPill"), [])

    def test_earlier_row_is_one_centred_line(self):
        self.load([{"entryId": "e0", "kind": "earlier", "readMinutes": 6, "time": "09:10 – 09:40"}, salih("s0", "Soru")])
        line = self.one("earlierLine")
        x, _, w, _ = self.at(line)
        self.assertEqual((line.property("text"), x, w, self.color(line)),
                         ("Önceki 6 tur · 09:10 – 09:40 · Göster", LEFT, COLUMN, C["text3"]))
        self.assertEqual((self.js(line, "Number(horizontalAlignment)"), line.property("lineCount")), (4, 1))

    def test_empty_and_three_hundred_rows_load_quietly(self):
        self.load()
        self.assertEqual(self.items("readingRow") + self.items("newAnswerPill"), [])
        self._close()
        self.load(chat(300))
        pump(100)
        self.assertEqual(self.flick.property("count"), 300)
        self.assertTrue(self.flick.property("atYEnd"))

    def test_every_block_type_alone_loads_quietly(self):
        for block in ALL_BLOCKS:
            with self.subTest(block["t"]):
                self.load([jarvis("j0", [block])])
                self.assertEqual(len(self.shown(self.one("answerBlocks"))), 1)
                self.assertEqual(self.warnings + _NOISE, [])
                self._close()

    def test_malformed_blocks_and_rows_draw_nothing_and_throw_nothing(self):
        self.load([jarvis("j0", BAD), jarvis("j1", []), {**jarvis("j2", []), "blocks": {"t": "p"}},
                   {**jarvis("j3", []), "blocks": "metin"}, jarvis("j4", [para("x")], who="kimse")])
        self.assertEqual([len(self.shown(b)) for b in self.items("answerBlocks")], [7, 0, 0, 0])
        self.assertEqual([b.property("item") is None for b in self.items("turnBody")].count(True), 1)

    def test_every_clickable_is_named_and_large_enough(self):
        self.load([salih("s0", "Soru"), jarvis("j0", ALL_BLOCKS), jarvis("j1", [WIDE])], talk=True)
        areas = self.items(kind="QQuickMouseArea")
        self.assertGreater(len(areas), 0)
        for area in areas:
            owner = area if area.objectName() else area.parentItem()
            with self.subTest(owner.objectName()):
                self.assertNotEqual(owner.objectName(), "")
                self.assertNotEqual(owner.property("accessibleName") or "", "")
                self.assertGreaterEqual(min(area.width(), area.height()), HIT)
        bars = self.items("readingScroll") + self.items("sideScroll")
        self.assertGreaterEqual(len(bars), 2)
        for bar in bars:
            self.assertNotEqual(bar.property("accessibleName") or "", "")
            self.assertGreaterEqual(min(bar.width(), bar.height()), HIT)
        for text in self.items(kind="QQuickText") + self.items(kind="QQuickTextEdit"):
            self.assertEqual(self.js(text, "Number(textFormat)"), 0)


if __name__ == "__main__":
    unittest.main()
