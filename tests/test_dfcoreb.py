"""DFCOREB: the JarvisCore refresh - the Reactor II look on a swappable visual layer.

* JarvisCore.qml keeps the one logic block (state, tuning, power, the clock,
  the event language, the bridge hooks); the drawing is a design layer under
  components/core/, chosen by ONE `design` property through a small registry.
* An unknown or empty design falls back to the classic core (DFCORESKIN): never a blank core.
* The layer only reads `core`: no applyState / fire / stopEvent / Travel, no
  Timer, no animation; JarvisCore references no id inside a layer.
* Frozen is the only fully still state: the clock stops, nothing runs.
"""
from __future__ import annotations

import re
import unittest

from tests.test_ui import _APP, pump  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import Property, QObject, QUrl, Signal  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402

from jarvis.ui.typography import FONT_DIR  # noqa: E402

QML_DIR = FONT_DIR.parent
CORE = QML_DIR / "components" / "JarvisCore.qml"
LAYER = QML_DIR / "components" / "core" / "ReactorLayer.qml"

# Every state the core speaks, as the bridge reports it (executing for working / answer).
STATES = (("idle", False), ("awaiting_input", False), ("running", False), ("running", True),
          ("completed", True), ("awaiting_approval", False), ("error", False), ("unknown", False),
          ("idle", False), ("disabled", False))

# The public API before DFCOREB (root declarations of JarvisCore.qml at c0ebdd1)...
BASE_PROPS = {
    "active", "alive", "blocked", "blue0", "bootFocus", "bootGlow", "bootRim", "booted", "breath", "c", "calm",
    "circulation", "clockT", "dormant", "environment", "eventAnims", "eventLevel", "executing", "focusBoost",
    "hold", "holdTarget", "idleOrbitPeak", "idleOrbitSpeed", "idleWaveMs", "idleWavePeak", "inactiveSince", "jolt",
    "lastActivity", "lastTick", "level", "listening", "litA", "litB", "lvl", "mainSpeed", "markerGain", "nucleus",
    "okLevel", "orbitPhase", "pointA", "previous", "rampMs", "rank", "reasoning", "residual", "rimPulse",
    "ringEnergy", "ringFloor", "running", "s", "secondSpeed", "seenActivity", "sig0", "sigTint", "spin", "spin2",
    "squeeze", "state_", "strong", "sweepAngle", "sweepGain", "sweepSpeed", "typeBoost", "typing",
}
BASE_FUNCS = {"applyState", "power", "wake", "settleDormant", "settleBoot", "fire", "stopEvent",
              "packetOut", "packetDown", "packetIn"}
# ...plus `design` (the one writable selector) and the documented layer interface:
# the palette, the iris, the angles the clock now owns, the logic-owned event
# objects (formerly ids of drawn items) and the registry itself.
ADDED_PROPS = {"design", "designs", "blue", "glint", "info", "brass", "warning", "danger", "success", "coldMetal",
               "iris", "mainAngle", "secondAngle", "fieldAngle", "flow",
               "wave", "ringWave", "packet", "rimPass", "markerA", "markerB"}


class StubBridge(QObject):
    changed = Signal()
    inputAccepted = Signal()
    modelApplied = Signal()
    dataChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._executing = False

    def _get_executing(self) -> bool:
        return self._executing

    def _set_executing(self, value: bool) -> None:
        self._executing = value
        self.changed.emit()

    executing = Property(bool, _get_executing, _set_executing, notify=changed)
    composing = Property(bool, lambda self: False, notify=changed)
    activity = Property("QVariantList", lambda self: [], notify=changed)


def declarations(text: str) -> tuple[set[str], set[str], set[str]]:
    props = set(re.findall(r"^    (?:readonly )?property \S+ (\w+)", text, re.M))
    funcs = set(re.findall(r"^    function (\w+)", text, re.M))
    signals = set(re.findall(r"^    signal (\w+)", text, re.M))
    return props, funcs, signals


class CoreLayerTests(unittest.TestCase):
    def load(self, design: str, size: int = 162):
        engine = QQmlEngine()
        self.addCleanup(engine.deleteLater)
        warnings: list[str] = []
        engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
        bridge = StubBridge()
        self.addCleanup(bridge.deleteLater)
        engine.rootContext().setContextProperty("bridge", bridge)
        component = QQmlComponent(engine)
        component.setData(f"""import QtQuick
import "components"
JarvisCore {{ width: {size}; height: {size}; active: false; design: "{design}" }}""".encode(),
                          QUrl.fromLocalFile(str(QML_DIR / "dfcoreb_probe.qml")))
        core = component.create()
        self.assertIsNotNone(core, component.errorString())
        self.addCleanup(core.deleteLater)
        self.addCleanup(component.deleteLater)                    # keeps the component (and its object) alive
        return core, bridge, warnings

    @staticmethod
    def layer(core):
        return [o for o in core.findChildren(QObject) if o.metaObject().className().startswith("ReactorLayer")]

    @staticmethod
    def running(core) -> list[str]:
        return [o.metaObject().className() for o in core.findChildren(QObject)
                if re.search(r"Animation|Timer", o.metaObject().className()) and o.property("running")]

    def test_every_state_cycles_without_a_warning_and_frozen_is_fully_still(self) -> None:
        core, bridge, warnings = self.load("reactor")
        self.assertEqual(1, len(self.layer(core)))
        core.setProperty("active", True)                          # boot: dormant structure -> stable life
        pump(2400)
        for state, executing in STATES:
            bridge.setProperty("executing", executing)
            core.setProperty("state_", state)
            pump(700)
        pump(1500)                                                # the power-down has run
        self.assertTrue(core.property("dormant"))
        self.assertEqual(1.0, core.property("iris"))              # the chamber sealed
        self.assertEqual([], self.running(core))                  # the clock stopped, nothing animating
        angles = [core.property(n) for n in ("mainAngle", "secondAngle", "sweepAngle", "flow", "clockT")]
        pump(300)
        self.assertEqual(angles, [core.property(n) for n in ("mainAngle", "secondAngle", "sweepAngle", "flow", "clockT")])
        core.setProperty("state_", "idle")                        # and it comes back
        pump(300)
        self.assertNotEqual([], self.running(core))
        self.assertEqual([], warnings)

    def test_unknown_or_empty_design_falls_back_to_the_classic_core(self) -> None:
        # DFCORESKIN: the fallback is the classic core (the default skin), no longer the reactor.
        for design in ("", "bogus"):
            with self.subTest(design=design):
                core, _bridge, warnings = self.load(design, 324)
                pump(50)
                self.assertEqual(1, len([o for o in core.findChildren(QObject)
                                         if o.metaObject().className().startswith("ClassicLayer")]))
                self.assertEqual([], warnings)

    def test_the_layer_only_draws(self) -> None:
        layer = LAYER.read_text(encoding="utf-8")
        for name in ("applyState", "fire", "stopEvent", "Travel"):
            self.assertNotRegex(layer, rf"\b(?:function|component) {name}\b")
            self.assertNotRegex(layer, rf"\b{name}\s*\(")
        self.assertNotRegex(layer, r"\bTimer\s*\{")
        self.assertNotRegex(layer, r"Animation\s*\{|Behavior on")
        self.assertIn("required property Item core", layer)

    def test_the_logic_references_no_layer_id(self) -> None:
        layer_ids = set(re.findall(r"\bid: (\w+)", LAYER.read_text(encoding="utf-8")))
        # Code only: the design NAME "reactor" (a string) and the comments are not references.
        logic = re.sub(r'"[^"\n]*"|//[^\n]*', "", CORE.read_text(encoding="utf-8"))
        self.assertTrue(layer_ids)
        self.assertEqual(set(), {i for i in layer_ids if re.search(rf"\b{i}\b", logic)})

    def test_the_public_api_is_today_s_plus_design_and_the_layer_interface(self) -> None:
        props, funcs, signals = declarations(CORE.read_text(encoding="utf-8"))
        self.assertEqual(BASE_PROPS | ADDED_PROPS, props)
        self.assertEqual(BASE_FUNCS, funcs)
        self.assertEqual(set(), signals)
        writable = set(re.findall(r"^    property \S+ (\w+)", CORE.read_text(encoding="utf-8"), re.M))
        self.assertEqual({"design", "iris", "mainAngle", "secondAngle", "fieldAngle", "flow"},
                         writable & ADDED_PROPS)


if __name__ == "__main__":
    unittest.main()
