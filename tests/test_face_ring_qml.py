"""FACERING1: the status ring of canvas frame 3 as one QML component (StatusRing.qml).

* Six states (idle, listening, understanding, thinking, speaking, needs_you), level 0-1, count, size;
  an unknown state draws idle and is reported through a signal.
* Used at 72 px (desktop ring) and 28 px (header mark: outer ring, main ring and core only).
* Colour changes only for needs_you; reduced motion stops every animation, the shapes still differ.
* Runs offscreen against a fake `face` whose `tokens` carry the locked token names; a QML warning fails.
"""
from __future__ import annotations

import math
import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from PySide6.QtCore import Property, QObject, QPointF, Qt, QtMsgType, QUrl, qInstallMessageHandler  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

_APP = QGuiApplication.instance() or QGuiApplication([])

ROOT = Path(__file__).resolve().parents[1]
QML_DIR = ROOT / "jarvis" / "face" / "qml"
RING_QML = QML_DIR / "StatusRing.qml"
MOODS = ["idle", "listening", "understanding", "thinking", "speaking", "needs_you"]
NAMES = {"idle": "Boşta", "listening": "Dinliyor", "understanding": "Anlıyor", "thinking": "Düşünüyor",
         "speaking": "Konuşuyor", "needs_you": "Senden bir şey istiyor"}
PARTS = ["outerRing", "dashRing", "mainRing", "glow", "ripple1", "ripple2", "arcUnderstand", "arcThinkA",
         "arcThinkB", "core", "badge"]
RUNNING = {72: {"idle": 1, "listening": 0, "understanding": 1, "thinking": 3, "speaking": 1, "needs_you": 1},
           28: {"idle": 1, "listening": 0, "understanding": 0, "thinking": 1, "speaking": 0, "needs_you": 1}}

# The locked token set (FRONT specs 18, EK2 included), as flat keys like FACETOKENS1 as_qml_dict.
COLORS = {"base": "#0B0E13", "surface1": "#11161D", "surface2": "#171D26", "surface3": "#0E1218",
          "line1": "#1B222C", "line2": "#232B36", "text1": "#E7ECF2", "text2": "#AAB4C0", "text3": "#8793A1",
          "text_soft": "#C5CED8", "accent": "#5BC8F5", "accent_ink": "#06202C", "accent_surface": "#0F1C24",
          "accent_line": "#1D3A4A", "needs_you": "#F2B34A", "needs_you_ink": "#2A1C03", "error": "#F2777F",
          "ok": "#6FD3A3", "disabled_glyph": "#5E6978", "link": "#7FD4F7", "link_hover": "#B5E6FB"}
TYPE = {"body": (17, 28), "h2": (22, 30), "h3": (17, 26), "code": (14.5, 22), "small": (13, 20),
        "label": (12.5, 16), "turn": (16, 26), "cell": (15, 24), "meta": (13, 20), "control": (14, 20)}
RADIUS = {"panel": 12, "composer": 16, "control": 12, "code": 8, "pill": 999}
SPACE = (4, 6, 8, 10, 12, 14, 16, 20, 24)
MOTION = {"fast": 120, "medium": 200, "slow": 320, "curve_x1": 0.2, "curve_y1": 0, "curve_x2": 0, "curve_y2": 1,
          "breathe": 4000, "swell": 700, "arc": 1200, "arc2": 2400, "arc3": 3200, "ripple": 1600, "ask": 1600}
RING = {"box": 120, "outer_r": 54, "outer_w": 1.5, "dash_r": 46, "dash_w": 2, "dash_on": 2, "dash_off": 6,
        "main_r": 38, "main_w": 4, "glow_r": 20, "core_r": 12}
TOKENS: dict[str, object] = {
    **{f"color_{k}": v for k, v in COLORS.items()},
    **{f"type_{k}_size": s for k, (s, _) in TYPE.items()}, **{f"type_{k}_line": h for k, (_, h) in TYPE.items()},
    **{f"radius_{k}": v for k, v in RADIUS.items()}, **{f"space_{v}": v for v in SPACE},
    **{f"motion_{k}": v for k, v in MOTION.items()}, **{f"ring_{k}": v for k, v in RING.items()},
}


class FakeFace(QObject):
    """The one context object QML may touch; only its tokens matter to the ring."""

    def __init__(self) -> None:
        super().__init__()
        self._tokens = dict(TOKENS)

    def _get_tokens(self) -> dict:
        return self._tokens

    tokens = Property("QVariantMap", _get_tokens, constant=True)


HARNESS = b"""
import QtQuick
Window {
    id: win
    width: 220; height: 88; visible: true
    color: face.tokens.color_base
    property int clicks: 0
    property int unknownCount: 0
    property string lastUnknown: ""
    StatusRing {
        objectName: "ring72"; x: 8; y: 8; size: 72
        onClicked: win.clicks++
        onUnknownState: (name) => { win.unknownCount++; win.lastUnknown = name }
    }
    StatusRing {
        objectName: "ring28"; x: 168; y: 30; size: 28
        onClicked: win.clicks++
        onUnknownState: (name) => { win.unknownCount++; win.lastUnknown = name }
    }
}
"""


def pump(ms: int = 30) -> None:
    QTest.qWait(ms)


def anims(obj: QObject) -> list[QObject]:
    return [o for o in obj.findChildren(QObject) if "Anim" in o.metaObject().className()]


def on_line(px: QColor, a: QColor, b: QColor, tol: float = 6.0) -> float | None:
    """Where px sits on the a -> b blend (0..1), or None when it is off that line."""
    p = (px.red(), px.green(), px.blue())
    s, e = (a.red(), a.green(), a.blue()), (b.red(), b.green(), b.blue())
    d = [e[i] - s[i] for i in range(3)]
    t = sum((p[i] - s[i]) * d[i] for i in range(3)) / sum(v * v for v in d)
    dist = math.dist(p, [s[i] + t * d[i] for i in range(3)])
    return t if dist <= tol and -0.03 <= t <= 1.03 else None


def split_code(text: str) -> tuple[str, list[str]]:
    """QML text with comments removed and string literals emptied, plus the literals."""
    strings: list[str] = []

    def keep(m: re.Match) -> str:
        if m.group(0).startswith("/"):
            return ""
        strings.append(m.group(0)[1:-1])
        return '""'

    rx = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*.*?\*/'
    return re.sub(rx, keep, text, flags=re.S), strings


JS_WORDS = {"import", "readonly", "property", "real", "int", "bool", "string", "color", "var", "alias", "signal",
            "function", "return", "typeof", "if", "else", "true", "false", "null", "undefined", "on", "in"}
BUILT_INS = {"Math", "String", "isNaN", "isFinite", "qsTr", "Easing", "Animation", "Accessible", "Qt", "parent",
             "width", "height", "visible", "state", "x", "y", "opacity", "scale", "rotation", "implicitWidth",
             "implicitHeight", "strokeWidth", "radiusX", "face"}


def free_names(code: str) -> set[str]:
    """Root names split_code() output reads that are not the ring's own, a type it uses, a built-in or face."""
    # shortcut: regex, not a QML parser: misses multiline ternaries, spreads, non-ASCII names and scope
    # (an own name counts anywhere in the file); upgrade to qmllint's unqualified-access check when it ships here

    code = re.sub(r"^\s*import\b[^\n]*", "", code, flags=re.M)
    own = {a or b for a, b in re.findall(r"\b(?:property\s+\w+|function|signal|var)\s+(\w+)|\bid:\s*(\w+)", code)}
    for params in re.findall(r"\b(?:function|signal)\s+\w+\s*\(([^)]*)\)", code):
        own |= {p.split()[-1] for p in params.split(",") if p.strip()}
    types = set(re.findall(r"\b([A-Z]\w*)\s*(?:on\s+\w+\s*)?\{", code))
    body = re.sub(r"(^|[{;,])(\s*)[A-Za-z_][\w.]*\s*:", r"\1\2", code, flags=re.M)   # a binding's left side
    body = re.sub(r"\b[A-Z]\w*\s*(?:on\s+\w+\s*)?\{", "{", body)                     # Type { / Type on prop {
    return set(re.findall(r"(?<![\w.])[A-Za-z_]\w*", body)) - own - types - JS_WORDS - BUILT_INS


class StaticContractTests(unittest.TestCase):
    """The QML contract read from the source: imports, tokens only, no state change, plain text."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = RING_QML.read_text(encoding="utf-8")
        cls.code, cls.strings = split_code(cls.raw)
        head, rest = cls.raw.split("// -- ratios, angles and counts", 1)
        block, tail = rest.split("// -- end --", 1)
        cls.block = [split_code(line)[0].strip() for line in block.splitlines()[1:]]
        cls.outside = split_code(head + tail)[0]

    def test_only_qt_quick_and_the_face_tokens_nothing_banned(self) -> None:
        imports = {line.strip() for line in self.code.splitlines() if line.strip().startswith("import")}
        self.assertEqual({"import QtQuick", "import QtQuick.Shapes"}, imports)
        banned = r"Clipboard|clipboard|grabWindow|grabToImage|FileDialog|FolderDialog|XMLHttpRequest|" \
                 r"openUrlExternally|QtMultimedia|MediaDevices|AudioInput|Camera|LocalStorage|Qt\.labs|" \
                 r"QtWebSockets|WebSocket|https?://|jarvis\.db|import \"\.\./|bridge\."
        self.assertNotRegex(self.raw, banned)
        self.assertNotRegex(self.code, r"\bLoader\b|Qt\.include|Qt\.createComponent|Qt\.createQmlObject")
        self.assertEqual({"tokens"}, set(re.findall(r"\bface\s*\.\s*(\w+)", self.code)))

    def test_every_free_name_is_the_rings_own_a_built_in_or_face(self) -> None:
        self.assertEqual(set(), free_names(self.code))
        for leak in ("x: otherCtx.x", "y: otherCtx", "z: ring.t(otherCtx)", "z: ring.moving ? otherCtx.y : 0"):
            self.assertEqual({"otherCtx"}, free_names(self.code + "\nItem { " + leak + " }"), leak)

    def test_no_colour_or_size_literal_outside_the_ratio_block(self) -> None:
        self.assertNotIn("#", self.raw)
        self.assertNotRegex(self.code, r"Qt\.(rgba|hsla|hsva|lighter|darker|tint|color)\b")
        for text in self.strings:
            if text != "transparent":
                self.assertFalse(QColor.isValidColorName(text), text)
        self.assertLessEqual(set(re.findall(r"(?<![\w.])\d+(?:\.\d+)?", self.outside)), {"0", "1", "2"})
        lines = [line for line in self.block if line]
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^readonly property (real|int) \w+: \d+(\.\d+)?$")

    def test_durations_and_font_sizes_come_from_tokens_and_every_token_exists(self) -> None:
        durations = re.findall(r"duration:\s*([^;\n]+)", self.raw)
        self.assertTrue(durations)
        for value in durations:
            self.assertTrue(value.startswith('ring.t("motion_'), value)
        sizes = re.findall(r"pixelSize:\s*([^;\n]+)", self.raw)
        self.assertTrue(sizes)
        for value in sizes:
            self.assertIn('ring.t("type_', value)
        names = {s for s in self.strings if re.match(r"(color|type|radius|space|motion|ring)_", s)}
        self.assertTrue(names)
        self.assertLessEqual(names, set(TOKENS))

    def test_no_state_change_timer_press_and_hold_or_rich_text(self) -> None:
        props = r"state|mood|level|count|size|elapsed|reducedMotion|breath|pulse|ripple"
        self.assertNotRegex(self.outside, rf"\b(?:{props})\s*(?:=(?!=)|\+\+|--|[-+*/%]=)")
        self.assertNotRegex(self.outside, r"\bTimer\b|[pP]ressAndHold|\bTapHandler\b")
        self.assertNotRegex(self.outside, r"RichText|StyledText|MarkdownText|AutoText")
        texts = len(re.findall(r"\bText\s*\{", self.outside))
        self.assertGreater(texts, 0)
        self.assertEqual(texts, len(re.findall(r"textFormat:\s*Text\.PlainText", self.outside)))

    def test_the_one_clickable_item_is_named_and_spoken(self) -> None:
        self.assertEqual(1, len(re.findall(r"\bMouseArea\s*\{", self.outside)))
        self.assertRegex(self.raw, r'MouseArea\s*\{[^}]*objectName:\s*"ringHit"[^}]*'
                                   r'Accessible\.name:\s*ring\.accessibleName')


class RingCase(unittest.TestCase):
    """Loads the harness (a 72 px and a 28 px ring) against FakeFace; any QML warning fails."""

    def setUp(self) -> None:
        self.warnings: list[str] = []
        self.engine = QQmlApplicationEngine()
        self.engine.warnings.connect(lambda errors: self.warnings.extend(e.toString() for e in errors))
        previous = qInstallMessageHandler(self._message)
        self.addCleanup(qInstallMessageHandler, previous)
        self.face = FakeFace()
        self.engine.rootContext().setContextProperty("face", self.face)
        self.engine.loadData(HARNESS, QUrl.fromLocalFile(str(QML_DIR / "_harness.qml")))
        self.assertEqual(1, len(self.engine.rootObjects()), self.warnings)
        self.win = self.engine.rootObjects()[0]
        self.addCleanup(self._close)
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

    def ring(self, size: int) -> QQuickItem:
        found = self.win.findChild(QQuickItem, f"ring{size}")
        self.assertIsNotNone(found, size)
        return found

    def part(self, ring: QQuickItem, name: str):
        found = ring.findChild(QObject, name)
        self.assertIsNotNone(found, name)
        return found

    def set(self, ms: int = 30, **props) -> None:
        for size in (72, 28):
            for key, value in props.items():
                self.ring(size).setProperty(key, value)
        pump(ms)

    def shown_parts(self, ring: QQuickItem) -> set[str]:
        return {name for name in PARTS if self.part(ring, name).isVisible()}

    def running(self, ring: QQuickItem) -> int:
        return sum(1 for a in anims(ring) if a.property("running"))


class RingStateTests(RingCase):
    """Acceptance 1-10: the six states, both sizes, reduced motion and the hit area."""

    def test_properties_state_level_count_and_size(self) -> None:
        for size in (72, 28):
            ring = self.ring(size)
            self.assertEqual((size, size), (ring.width(), ring.height()))
            self.assertEqual(("idle", "idle"), (ring.property("state"), ring.property("mood")))
            self.assertEqual(MOODS, ring.property("moods").toVariant())
            self.assertEqual((0, 0), (ring.property("level"), ring.property("count")))
        self.set(mood="thinking")                         # FACESHELL1 name is the same property
        self.assertEqual("thinking", self.ring(72).property("state"))

    def test_idle_is_three_static_rings_and_a_breathing_core(self) -> None:
        ring = self.ring(72)
        self.assertEqual({"outerRing", "dashRing", "mainRing", "core"}, self.shown_parts(ring))
        breath = self.part(ring, "breathAnim")
        self.assertEqual((0.5, 1.0, 4000), (breath.property("from"), breath.property("to"), breath.property("duration")))
        self.assertEqual(["breathAnim"], [a.objectName() for a in anims(ring) if a.property("running")])
        core = self.part(ring, "core")                    # the running breath phase drives the core, no clock read
        self.assertAlmostEqual(ring.property("breath"), core.opacity(), places=6)
        self.assertTrue(0.5 - 1e-6 <= core.opacity() <= 1 + 1e-6)

    def test_listening_is_full_strength_and_swells_with_the_level(self) -> None:
        self.set(state="listening", reducedMotion=True)
        ring, main = self.ring(72), self.part(self.ring(72), "mainRing")
        self.assertAlmostEqual(1.0, main.opacity())
        for level, want in ((0, 1.0), (0.5, 1.035), (1, 1.07), (0, 1.0)):
            self.set(level=level)
            self.assertAlmostEqual(want, ring.property("mainScale"), places=6)
            self.assertAlmostEqual(want, main.scale(), places=6)
        self.set(reducedMotion=False)                     # no in-flight sample: only where the swell
        self.set(level=1, ms=900)                         # ends is checked, after a wait past motion_swell
        self.assertAlmostEqual(1.07, main.scale(), places=3)
        self.set(state="idle", level=0, ms=900)
        self.assertAlmostEqual(0.6, main.opacity())
        self.assertAlmostEqual(1.0, main.scale())

    def test_a_swell_lands_at_once_when_reduced_motion_turns_on_or_the_ring_hides(self) -> None:
        self.set(state="listening")
        rings = [self.ring(size) for size in (72, 28)]
        for key, stop in (("reducedMotion", True), ("visible", False)):
            for level, want in ((1, 1.07), (0, 1.0)):     # up to the full swell and back down
                for ring in rings:
                    ring.setProperty("level", level)      # the swell is now in flight: no pump, not there yet
                    self.assertGreater(abs(self.part(ring, "mainRing").scale() - want), 0.01, (key, level))
                    ring.setProperty(key, stop)
                for ring in rings:
                    for lvl in (level, 0.5, level):        # and a level change while stopped lands at once too
                        ring.setProperty("level", lvl)
                        at = (key, lvl, ring.objectName())
                        scale = self.part(ring, "mainRing").scale()
                        self.assertAlmostEqual(ring.property("mainScale"), scale, places=6, msg=at)
                        self.assertEqual(0, self.running(ring), at)   # the swell animation itself stopped
                    self.assertAlmostEqual(want, scale, places=6, msg=at)
                self.set(**{key: not stop})
                for ring in rings:                         # back in motion: no stale swell, nothing in flight
                    at = (key, "resumed", level, ring.objectName())
                    self.assertAlmostEqual(want, self.part(ring, "mainRing").scale(), places=6, msg=at)
                    self.assertEqual(0, self.running(ring), at)

    def test_understanding_turns_a_quarter_arc_once_per_arc(self) -> None:
        self.set(state="understanding")
        arc = self.part(self.ring(72), "arcUnderstand")
        self.assertTrue(arc.isVisible())
        turn = [(a.property("from"), a.property("to"), a.property("duration"), a.property("running")) for a in anims(arc)]
        self.assertEqual([(0.0, 360.0, 1200, True)], turn)
        sweeps = [o.property("sweepAngle") for o in arc.findChildren(QObject) if "PathAngleArc" in o.metaObject().className()]
        self.assertEqual([90.0], sweeps)

    def test_thinking_turns_two_arcs_opposite_ways_and_can_show_the_elapsed_label(self) -> None:
        self.set(state="thinking")
        ring = self.ring(72)
        for name, want in (("arcThinkA", (0.0, 360.0, 2400)), ("arcThinkB", (360.0, 0.0, 3200))):
            spin = anims(self.part(ring, name))
            self.assertEqual([want], [(a.property("from"), a.property("to"), a.property("duration")) for a in spin])
            self.assertTrue(spin[0].property("running"), name)
        self.assertTrue(self.part(ring, "breathAnim").property("running"))
        label = self.part(ring, "elapsedLabel")
        self.assertFalse(label.isVisible())
        self.set(elapsed="12 sn")
        self.assertTrue(label.isVisible())
        self.assertEqual(80, label.x())                   # 72 px ring + space_8
        self.assertGreater(ring.width(), 72)
        self.assertFalse(self.part(self.ring(28), "elapsedLabel").isVisible())
        self.set(elapsed="")
        self.assertFalse(label.isVisible())
        self.assertEqual(72, ring.width())

    def test_speaking_draws_two_ripples_half_a_cycle_apart(self) -> None:
        self.set(state="speaking", ms=120)
        ring = self.ring(72)
        one, two = self.part(ring, "ripple1"), self.part(ring, "ripple2")
        self.assertTrue(one.isVisible() and two.isVisible())
        anim = self.part(ring, "rippleAnim")
        self.assertEqual((1600, True), (anim.property("duration"), anim.property("running")))
        self.assertAlmostEqual(0.5, (two.property("p") - one.property("p")) % 1, places=5)
        self.assertAlmostEqual(1 - one.property("p"), one.opacity(), places=5)
        self.set(reducedMotion=True)
        self.assertEqual((0.0, 0.5), (one.property("p"), two.property("p")))

    def test_needs_you_has_its_colour_a_pulse_and_a_count_badge(self) -> None:
        for mood in MOODS[:5]:
            self.set(state=mood)
            self.assertEqual("#5bc8f5", self.ring(72).property("stroke").name(), mood)
        self.set(state="needs_you")
        ring = self.ring(72)
        self.assertEqual("#f2b34a", ring.property("stroke").name())
        pulse = self.part(ring, "pulseAnim")
        self.assertEqual((1600, True), (pulse.property("duration"), pulse.property("running")))
        badge = self.part(ring, "badge")
        self.assertFalse(badge.isVisible())
        self.set(count=3)
        self.assertTrue(badge.isVisible())
        self.assertEqual((72, 0), (badge.x() + badge.width(), badge.y()))   # upper right
        digits = self.part(ring, "badgeText")
        self.assertEqual(("#f2b34a", "#2a1c03"), (badge.property("color").name(), digits.property("color").name()))
        self.assertEqual("3", digits.property("text"))
        self.set(count=0)
        self.assertFalse(badge.isVisible())

    def test_accessible_names_are_turkish_with_the_count_for_needs_you(self) -> None:
        for mood in MOODS:
            self.set(state=mood)
            for size in (72, 28):
                want = NAMES[mood] + " (0)" if mood == "needs_you" else NAMES[mood]   # the count, even 0
                self.assertEqual((want, NAMES[mood]),
                                 (self.ring(size).property("accessibleName"), self.ring(size).property("label")))
        self.set(state="needs_you", count=0)
        self.assertFalse(self.part(self.ring(72), "badge").isVisible())   # "(0)" is spoken, the badge stays hidden
        for count, shown in ((4, "4"), (10, "9+"), (-4, "0"), (math.nan, "0"), (0, "0")):
            self.set(state="needs_you", count=count)
            for size in (72, 28):
                self.assertEqual(f"Senden bir şey istiyor ({shown})", self.ring(size).property("accessibleName"))
        labels = self.ring(72).property("labels").toVariant()
        self.assertEqual(NAMES, {m: labels[m] for m in MOODS})

    def test_reduced_motion_stops_everything_and_the_states_still_differ_by_shape(self) -> None:
        self.set(reducedMotion=True, count=3, level=1)
        looks = set()
        for mood in MOODS:
            self.set(state=mood)
            for size in (72, 28):
                self.assertEqual(0, self.running(self.ring(size)), (mood, size))
            ring = self.ring(72)
            main = self.part(ring, "mainRing")
            looks.add((frozenset(self.shown_parts(ring)), round(main.scale(), 4), round(main.opacity(), 4)))
            self.assertAlmostEqual(1.0, self.part(ring, "core").opacity(), msg=mood)
        self.assertEqual(6, len(looks))

    def test_reduced_motion_turned_on_mid_animation_stops_it_in_every_state_at_both_sizes(self) -> None:
        for mood in MOODS:
            self.set(state=mood, count=3, level=1)
            self.set(reducedMotion=False, ms=60)   # level before motion: else the swell's NumberAnimation is still running
            for size in (72, 28):
                self.assertEqual(RUNNING[size][mood], self.running(self.ring(size)), (mood, size))
            for size in (72, 28):
                self.ring(size).setProperty("reducedMotion", True)    # no wait: it must hold at once
            for size in (72, 28):
                ring, at = self.ring(size), (mood, size)
                self.assertEqual(0, self.running(ring), at)
                self.assertAlmostEqual(1.0, self.part(ring, "core").opacity(), msg=at)
                self.assertAlmostEqual(ring.property("mainScale"), self.part(ring, "mainRing").scale(), places=6, msg=at)
                self.assertEqual((0.0, 0.5), (self.part(ring, "ripple1").property("p"),
                                              self.part(ring, "ripple2").property("p")), at)
            self.set(reducedMotion=False, ms=60)
            for size in (72, 28):                 # hiding stops the loops too (they gate on moving, not animate)
                self.ring(size).setProperty("visible", False)
                self.assertEqual(0, self.running(self.ring(size)), (mood, size, "hidden"))
            self.set(visible=True, reducedMotion=True)

    def test_at_28_px_only_the_outer_ring_main_ring_and_core_are_drawn(self) -> None:
        self.set(count=3, elapsed="7 sn", level=1)
        ring = self.ring(28)
        for mood in MOODS:
            self.set(state=mood)
            self.assertEqual({"outerRing", "mainRing", "core"}, self.shown_parts(ring), mood)
            self.assertFalse(self.part(ring, "elapsedLabel").isVisible(), mood)
            self.assertEqual(28, ring.width(), mood)

    def test_the_hit_area_is_at_least_44_and_a_click_is_reported(self) -> None:
        clicks = 0
        for size in (72, 28):
            hit = self.part(self.ring(size), "ringHit")
            self.assertGreaterEqual(min(hit.width(), hit.height()), 44, size)
            QTest.mouseClick(self.win, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             hit.mapToScene(QPointF(2, 2)).toPoint())
            pump()
            clicks += 1
            self.assertEqual(clicks, self.win.property("clicks"), size)


class RingNegativeTests(RingCase):
    """Negative 1-5: unknown state, clamping, no animation growth, no warning, one stroke colour."""

    def test_an_unknown_state_draws_idle_and_is_reported(self) -> None:
        idle = self.shown_parts(self.ring(72))
        self.set(state="bogus")
        ring = self.ring(72)
        self.assertEqual(("idle", "Boşta"), (ring.property("shown"), ring.property("label")))
        self.assertEqual(idle, self.shown_parts(ring))
        self.assertEqual(1, self.running(ring))
        self.assertEqual((2, "bogus"), (self.win.property("unknownCount"), self.win.property("lastUnknown")))

    def test_level_and_count_are_clamped_and_the_badge_caps_at_9_plus(self) -> None:
        ring = self.ring(72)
        self.set(state="listening", reducedMotion=True)
        for level, want in ((1.5, 1.0), (-3, 0.0), (math.nan, 0.0), (math.inf, 1.0), (0.25, 0.25)):
            self.set(level=level)
            self.assertAlmostEqual(want, ring.property("lv"), msg=level)
        self.set(state="needs_you")
        for count, want in ((-4, "0"), (math.nan, "0"), (3.7, "3"), (9, "9"), (10, "9+"), (1e12, "9+"),
                            (math.inf, "9+")):
            self.set(count=count)
            self.assertEqual(want, ring.property("badgeText"), count)
        self.set(count=10)
        self.assertEqual("Senden bir şey istiyor (9+)", ring.property("accessibleName"))
        self.assertTrue(self.part(ring, "badge").isVisible())
        self.set(count=-4)
        self.assertFalse(self.part(ring, "badge").isVisible())

    def test_a_thousand_state_switches_do_not_grow_the_animations(self) -> None:
        rings = [self.ring(72), self.ring(28)]
        before = [(len(anims(r)), len(r.findChildren(QObject))) for r in rings]
        cycle = MOODS + ["bogus"]
        for i in range(1000):
            for r in rings:
                r.setProperty("state", cycle[i % len(cycle)])
            if i % 100 == 0:
                pump(5)
        pump()
        self.assertEqual(before, [(len(anims(r)), len(r.findChildren(QObject))) for r in rings])
        for mood in MOODS:
            self.set(state=mood)
            for size in (72, 28):
                self.assertEqual(RUNNING[size][mood], self.running(self.ring(size)), (mood, size))

    def test_no_warning_in_any_state_at_either_size(self) -> None:
        self.set(level=0.6, count=12, elapsed="7 sn")
        for reduced in (False, True):
            self.set(reducedMotion=reduced)
            for mood in MOODS + ["bogus"]:
                self.set(state=mood, ms=60)
        self.assertEqual([], self.warnings)

    def test_accent_is_the_only_stroke_colour_except_in_needs_you(self) -> None:
        base, accent, ask = QColor(COLORS["base"]), QColor(COLORS["accent"]), QColor(COLORS["needs_you"])
        self.set(count=0, level=0)
        for mood in MOODS:
            self.set(state=mood, ms=120)
            target = ask if mood == "needs_you" else accent
            shot = self.ring(72).window().grabWindow()
            strongest = 0.0
            for y in range(shot.height()):
                for x in range(shot.width()):
                    px = shot.pixelColor(x, y)
                    if max(abs(px.red() - base.red()), abs(px.green() - base.green()),
                           abs(px.blue() - base.blue())) <= 3:
                        continue
                    t = on_line(px, base, target)
                    self.assertIsNotNone(t, (mood, x, y, px.name()))
                    strongest = max(strongest, t)
            self.assertGreaterEqual(strongest, 0.45, mood)


if __name__ == "__main__":
    unittest.main()
