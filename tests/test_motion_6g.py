"""Gate 6G K2: one motion contract.

* ONE duration gate: every `duration:` in the QML outside theme/Theme.qml is a
  Theme token or `Theme.dur(...)`, and `Theme.dur` (so every token) is 0 with
  ambient effects off. Transitions run 120-220 ms with no bounce; the few
  documented exceptions (below, each with its reason) still pass the gate.
* Travel is at most 4 px: one `Theme.travel` token, used by the three old
  offenders (Main page 8 px, PanelColumn 10 px, FeedView 8 px); no scale
  entrance moves an edge by a fixed fraction.
* One HoverTip: PowerControl, IconButton and TopBar use it, no stock ToolTip.
* Nothing loops forever except the documented state rhythms: the Freeze halo
  is static, the settings orb never spins.
* JarvisCore's idle wave and orbit have their own calmer look: fainter and
  slower than every real event's wave and packet.
* The no-effect list: approval attention and destructive dialogs never loop or
  move (destructive dialogs open at once: tests/test_projects_ui_6e.py p, ab);
  the attention strip changes tone at once.
* Known, outside the gate: the stock Basic ScrollBar's own hold + fade (Qt
  style code, not QML of ours).
"""
from __future__ import annotations

import re
import unittest

from tests.test_ui import _APP, UiTestCase, pump  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import Q_ARG, QMetaObject, QPointF, QUrl  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jarvis.ui.typography import FONT_DIR  # noqa: E402

QML_DIR = FONT_DIR.parent
THEME = QML_DIR / "theme" / "Theme.qml"
LOW, HIGH = 120, 220

# Documented exceptions to the 120-220 ms range: (file, literal or None for the
# whole file, reason). They still go through Theme.dur, so ambient off stops them.
EXCEPTIONS = (
    ("components/JarvisCore.qml", None,
     "the core's own choreography (boot, power, the event language, the state rhythms): roadmap "
     "6G K2 keeps the idle life and keeps real events distinct from it"),
    ("components/Background.qml", None,
     "the starfield's slow twinkle (2.6-4.4 s sines): ambient atmosphere, not a transition"),
    ("components/StatusOrb.qml", 3400,
     "the lit status orb's luminance breath: a rhythm of a real state, not a transition"),
    ("components/FeedView.qml", 320,
     "the live activity dots: a rhythm only while work really runs, not a transition"),
    ("components/FeedView.qml", 420,
     "the live activity dots: a rhythm only while work really runs, not a transition"),
    ("components/FreezeKeySwitch.qml", None,
     "the freeze switch lever (6G S2, Salih 2026-10-06): a toggle snaps (~80 ms + tiny settle), "
     "a knife switch swings slower and weighted; each move follows a real execution-state change"),
)
# Loops allowed to run forever, each a rhythm of a real state or the ambient atmosphere.
INFINITE = {
    "components/JarvisCore.qml": "listening / reasoning / executing sector rhythms",
    "components/StatusOrb.qml": "the lit orb's breath",
    "components/FeedView.qml": "the live activity dots while work runs",
    "components/Background.qml": "the starfield twinkle",
}
ANIMATION = re.compile(r"\b(\w+(?:Animation|Animator))\s*(?:on [\w.]+\s*)?\{")
DURATION = re.compile(r"\bduration:\s*([^;}\n]+?)\s*(?=[;}\n])")


def sources():
    for path in sorted(QML_DIR.rglob("*.qml")):
        yield path.relative_to(QML_DIR).as_posix(), path.read_text(encoding="utf-8")


def durations(text):
    """(line, animation type, expression) for every `duration:` binding."""
    for match in DURATION.finditer(text):
        kinds = ANIMATION.findall(text, 0, match.start())
        yield text.count("\n", 0, match.start()) + 1, kinds[-1] if kinds else "", match.group(1)


def gated(expression: str) -> bool:
    expression = expression.strip()
    if expression.startswith(("Theme.", "theme.dur(")):
        return True
    if "?" in expression:                  # a choice between two gated tokens
        return all(branch.strip().startswith("Theme.") for branch in expression.split("?", 1)[1].split(":"))
    return False


class DurationGateTests(unittest.TestCase):
    def test_every_duration_outside_the_theme_goes_through_the_gate(self) -> None:
        bad = [f"{name}:{line}: {expr}" for name, text in sources() if name != "theme/Theme.qml"
               for line, _kind, expr in durations(text) if not gated(expr)]
        self.assertEqual([], bad)
        literal = [f"{name}:{line}" for name, text in sources() if name != "theme/Theme.qml"
                   for line, _kind, expr in durations(text) if re.match(r"\d", expr)]
        self.assertEqual([], literal)

    def test_transitions_run_120_to_220_ms_unless_documented(self) -> None:
        used, bad = set(), []
        for name, text in sources():
            for line, kind, expr in durations(text):
                if kind == "PauseAnimation":                     # a hold, not motion
                    continue
                inner = re.fullmatch(r"(?:Theme|theme)\.dur\((.*)\)", expr.strip())
                for number in (int(n) for n in re.findall(r"\b\d+\b", inner.group(1) if inner else "")):
                    if LOW <= number <= HIGH:
                        continue
                    rule = next((e for e in EXCEPTIONS if e[0] == name and e[1] in (None, number)), None)
                    if rule is None:
                        bad.append(f"{name}:{line}: {expr}")
                    else:
                        used.add(rule)
        self.assertEqual([], bad)
        self.assertEqual(set(EXCEPTIONS), used, "an exception no longer needed")

    def test_the_theme_tokens_are_gated_and_in_range(self) -> None:
        text = THEME.read_text(encoding="utf-8")
        self.assertIn("function dur(ms) { return ambient ? ms : 0 }", text)
        tokens = dict(re.findall(r"readonly property int (\w+): dur\((\d+)\)", text))
        self.assertEqual({"panelIn", "panelOut", "reflow", "pageMs", "approvalIn", "focusMs", "themeMs",
                          "hoverMs", "pressMs", "attachMs"}, set(tokens))
        for name, value in tokens.items():
            self.assertTrue(LOW <= int(value) <= HIGH, (name, value))
        # The one plain number: the confirm guard is an input guard, not motion, so ambient
        # off never removes it (K2 fix-up).
        self.assertIn("readonly property int confirmGuardMs: 300", text)
        self.assertNotRegex(text, r"readonly property int (?!confirmGuardMs:)\w+(?:Ms|In|Out|reflow): \d")
        for _line, _kind, expr in durations(text):
            self.assertTrue(expr.startswith("theme.dur("), expr)

    def test_no_bounce(self) -> None:
        bounce = [name for name, text in sources() if re.search(r"Easing\.\w*(Back|Elastic|Bounce)\b", text)]
        self.assertEqual([], bounce)

    def test_theme_dur_is_zero_with_ambient_off(self) -> None:
        engine = QQmlEngine()
        self.addCleanup(engine.deleteLater)
        component = QQmlComponent(engine)
        component.setData(b"""import QtQuick
import "theme"
QtObject {
    function setAmbient(on) { Theme.ambient = on }
    readonly property var values: [Theme.dur(180), Theme.dur(2600), Theme.travel, Theme.panelIn, Theme.panelOut,
        Theme.reflow, Theme.pageMs, Theme.approvalIn, Theme.focusMs, Theme.themeMs, Theme.hoverMs,
        Theme.pressMs, Theme.attachMs]
}""", QUrl.fromLocalFile(str(QML_DIR / "motion_probe.qml")))
        probe = component.create()
        self.assertIsNotNone(probe, component.errorString())
        values = [int(v) for v in probe.property("values").toVariant()]
        self.assertEqual([180, 2600, 4], values[:3])
        self.assertTrue(all(LOW <= v <= HIGH for v in values[3:]), values)
        probe.setAmbient(False)
        self.assertEqual([0] * len(values), [int(v) for v in probe.property("values").toVariant()])
        probe.setAmbient(True)
        self.assertEqual(values, [int(v) for v in probe.property("values").toVariant()])


class TravelTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (QML_DIR / name).read_text(encoding="utf-8")

    def test_one_travel_token_of_at_most_4_px(self) -> None:
        self.assertIn("readonly property int travel: ambient ? 4 : 0", self.read("theme/Theme.qml"))
        self.assertEqual([], [name for name, text in sources() if "Theme.panelTravel" in text])

    def test_the_three_offenders_use_it(self) -> None:
        self.assertIn('PropertyAction { target: pageSlide; property: "y"; value: Theme.travel }', self.read("Main.qml"))
        self.assertIn("transform: Translate { id: slide; x: Theme.travel }", self.read("components/PanelColumn.qml"))
        self.assertIn("transform: Translate { id: slide; y: modelData.streamed === true ? 0 : Theme.travel }",
                      self.read("components/FeedView.qml"))

    def test_no_fixed_travel_anywhere(self) -> None:
        for name, text in sources():
            for block in re.findall(r"Translate \{([^}]*)\}", text):
                for binding in re.findall(r"\b[xy]:\s*([^;\n]+)", block):
                    self.assertTrue(all(int(n) <= 4 for n in re.findall(r"\b\d+\b", binding)), (name, binding))
            # A scale entrance from a fixed fraction moves a wide card's edges by more than 4 px.
            self.assertNotRegex(text, r"scale:[^;\n]*\?\s*1\s*:\s*0\.\d", name)
            self.assertNotRegex(text, r'property: "scale"; from: 0\.\d', name)
            # A scale entrance divides the travel by the LONGER side, so no edge moves more.
            for divisor in re.findall(r"Theme\.travel / (Math\.max\([^)]*\))", text):
                self.assertIn(divisor, ("Math.max(1, width, height)", "Math.max(1, dialog.width, dialog.height)"), name)


class NoForeverTests(unittest.TestCase):
    def test_only_documented_rhythms_loop_forever(self) -> None:
        looping = {name for name, text in sources() if "Animation.Infinite" in text or "loops: -1" in text}
        self.assertEqual(set(INFINITE), looping)

    def test_the_freeze_halo_is_static_and_the_settings_orb_never_spins(self) -> None:
        power = (QML_DIR / "components/PowerControl.qml").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"loops:|Animation\.Infinite|SequentialAnimation on", power))
        bar = (QML_DIR / "components/TopBar.qml").read_text(encoding="utf-8")
        orb = bar[bar.index('objectName: "settingsOrb"'):bar.index('id: wordmark')]
        self.assertIsNone(re.search(r"RotationAnimator|RotationAnimation|loops:|Animation\.Infinite", orb))

    def test_no_effect_surfaces_never_loop(self) -> None:
        for name in ("components/ApprovalCard.qml", "overlays/ApprovalOverlay.qml", "components/AttentionStrip.qml",
                     "components/SafetyCards.qml", "components/ConversationDeleteDialog.qml",
                     "components/JarvisDialog.qml", "overlays/ConfirmDialog.qml"):
            self.assertIsNone(re.search(r"loops:|Animation\.Infinite", (QML_DIR / name).read_text(encoding="utf-8")), name)
        dialog = (QML_DIR / "components/JarvisDialog.qml").read_text(encoding="utf-8")
        self.assertIn("readonly property bool moves: Theme.ambient && !danger", dialog)
        strip = (QML_DIR / "components/AttentionStrip.qml").read_text(encoding="utf-8")
        self.assertNotIn("Behavior on", strip)                 # attention changes tone at once
        confirm = (QML_DIR / "overlays/ConfirmDialog.qml").read_text(encoding="utf-8")
        self.assertEqual(3, confirm.count("enabled: !dialog.danger;"))


class HoverTipSourceTests(unittest.TestCase):
    def test_power_icon_button_and_top_bar_use_the_one_hover_tip(self) -> None:
        for name in ("components/PowerControl.qml", "components/IconButton.qml", "components/TopBar.qml"):
            text = (QML_DIR / name).read_text(encoding="utf-8")
            self.assertIn("HoverTip {", text, name)
            self.assertNotIn("ToolTip", text, name)
            self.assertNotIn("tipText", text, name)


class LiveMotionTests(UiTestCase):
    ambient = True      # THIZ: its subject is motion
    def window(self):
        bridge = self.bridge()
        root = self.load(bridge)
        root.showNormal()
        root.resize(1920, 1080)
        pump(350)
        return bridge, root

    def item(self, root, name):
        def find(node):
            if node.objectName() == name:
                return node
            for child in node.childItems():
                found = find(child)
                if found is not None:
                    return found
            return None
        found = find(root.contentItem())
        self.assertIsNotNone(found, name)
        return found

    def tip_of(self, owner):
        tips = [c for c in owner.childItems() if "HoverTip" in c.metaObject().className()]
        self.assertEqual(1, len(tips), owner.objectName())
        return tips[0]

    def test_the_settings_orb_and_the_freeze_control_show_their_text_in_the_hover_tip(self) -> None:
        _bridge, root = self.window()
        for name, text in (("settingsOrb", "Settings"), ("killSwitchControl", "Freeze JARVIS")):
            with self.subTest(name=name):
                owner = self.item(root, name)
                tip = self.tip_of(owner)
                self.assertEqual(text, tip.property("text"))
                QTest.mouseMove(root, owner.mapToScene(QPointF(owner.width() / 2, owner.height() / 2)).toPoint())
                for _ in range(100):
                    if tip.property("shown"):
                        break
                    pump(10)
                self.assertTrue(tip.property("shown"))
                QTest.mouseMove(root, root.contentItem().mapToScene(QPointF(960, 600)).toPoint())
                pump(60)
                self.assertFalse(tip.property("shown"))

    def test_a_destructive_confirm_dialog_guards_its_confirm_with_ambient_on_and_off(self) -> None:
        # K2 fix-up: ConfirmDialog (archive job, ...) with danger has no motion and a
        # motion-free guard: confirm is off for Theme.confirmGuardMs after it opens and off
        # the moment it closes; a non-destructive one (Quit) is unchanged.
        import time
        bridge, root = self.window()
        accept = self.item(root, "confirmAccept")
        dialog = accept.parentItem()
        while dialog is not None and dialog.property("armed") is None:
            dialog = dialog.parentItem()
        self.assertIsNotNone(dialog)
        for ambient in (True, False):
            with self.subTest(ambient=ambient):
                bridge.setAmbientEffects(ambient)
                pump(100)
                dialog.setProperty("danger", True)
                called = time.monotonic()
                dialog.setProperty("open", True)
                self.assertFalse(accept.isEnabled())                      # not right after it opens
                for _ in range(100):
                    if accept.isEnabled():
                        break
                    pump(10)
                self.assertTrue(accept.isEnabled())
                self.assertGreaterEqual(time.monotonic() - called, 0.3 - 0.03)
                dialog.setProperty("open", False)
                self.assertFalse(accept.isEnabled())                      # off the moment it closes
                dialog.setProperty("danger", False)
                dialog.setProperty("open", True)
                self.assertTrue(accept.isEnabled())                       # non-destructive: unchanged
                pump(350)                                                 # up past the guard window
                # A destructive re-ask while it is still open (Quit up, then Archive) is guarded again.
                QMetaObject.invokeMethod(dialog, "ask", Q_ARG("QVariant", "Archive job"), Q_ARG("QVariant", "Archive?"),
                                         Q_ARG("QVariant", "Archive"), Q_ARG("QVariant", True), Q_ARG("QVariant", ""),
                                         Q_ARG("QVariant", "Back"))
                self.assertTrue(dialog.property("open"))
                self.assertFalse(accept.isEnabled())
                dialog.setProperty("open", False)
                pump(300)

    def test_the_core_s_idle_life_is_fainter_and_slower_than_every_real_event(self) -> None:
        _bridge, root = self.window()
        core = self.item(root, "jarvisCore")
        peak, ms = core.property("idleWavePeak"), core.property("idleWaveMs")
        orbit, speed = core.property("idleOrbitPeak"), core.property("idleOrbitSpeed")
        text = (QML_DIR / "components/JarvisCore.qml").read_text(encoding="utf-8")
        idle = text[text.index("id: ambientAnim"):text.index("id: launchAnim")]
        events = text.replace(idle, "")
        # The idle wave uses its own look, nothing else does.
        self.assertIn("value: core.idleWavePeak", idle)
        self.assertEqual(2, idle.count("duration: Theme.dur(core.idleWaveMs)"))
        wave_peaks = [float(v) for v in re.findall(r'target: (?:wave|ringWave); property: "opacity"; value: ([\d.]+)', events)]
        wave_ms = [int(v) for v in re.findall(r'target: (?:wave|ringWave); property: "(?:scale|opacity)"; to: [\d.]+; '
                                              r'duration: Theme\.dur\((\d+)\)', events)]
        packets = [v for v in (float(v) for v in re.findall(r"packet\.opacity = ([\d.]+)", events)) if v > 0]
        self.assertEqual(4, len(wave_peaks))          # success, major, model, sent
        self.assertTrue(wave_ms and packets)
        self.assertLessEqual(peak * 4, min(wave_peaks))         # a quarter of the faintest event wave at most
        self.assertGreater(ms, max(wave_ms))                     # slower than the slowest event wave
        self.assertLess(orbit, min(packets))                     # fainter than every event packet
        self.assertLess(speed, 360 / 61)                         # slower than before K2
        self.assertIn("core.orbitPhase + core.idleOrbitSpeed", text)
        self.assertIn("opacity: core.idleOrbitPeak *", text)


if __name__ == "__main__":
    unittest.main()
