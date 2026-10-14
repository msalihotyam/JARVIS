"""DFCORESKIN: core skins - today's core stays the default, Reactor II selectable in settings.

* The preference ``core_skin`` lives in a sibling file (``<prefs>.core-skin.json``):
  default and every fallback "classic"; the main document is never touched.
* Bridge ``coreSkin`` / ``setCoreSkin``: the theme's shape (unknown ignored,
  notify only on a real change).
* The drawer's "Core appearance" cards swap only the visual layer: the logic
  keeps its state (frozen stays frozen and still, off stays off).
"""
from __future__ import annotations

import json
import re
import unittest

from tests import test_dfcoreb, test_ui
from tests.test_dfcoreb import CORE, QML_DIR, StubBridge
from tests.test_ui import UiTestCase, pump

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402

from jarvis.ui.preferences import CORE_SKINS, UiPreferences  # noqa: E402

CLASSIC = QML_DIR / "components" / "core" / "ClassicLayer.qml"
INVALID = ("", "bogus", 1, None, "x" * 10240)


def layers(core) -> list[str]:
    """The design layer the core's Loader holds now (a replaced one may still await deletion)."""
    return [o.property("item").metaObject().className().split("_")[0] for o in core.findChildren(QObject)
            if o.metaObject().className() == "QQuickLoader" and o.property("item") is not None
            and re.match(r"(Classic|Reactor)Layer", o.property("item").metaObject().className())]


class CoreSkinPreferenceTests(UiTestCase):
    def prefs(self) -> UiPreferences:
        return UiPreferences(self.prefs_path)

    def test_default_is_classic(self) -> None:
        self.assertEqual(("classic", "reactor"), CORE_SKINS)
        self.assertEqual("classic", self.prefs().core_skin)

    def test_reactor_round_trips(self) -> None:
        self.assertTrue(self.prefs().set_core_skin("reactor"))
        self.assertEqual("reactor", self.prefs().core_skin)
        self.assertEqual({"version": 1, "skin": "reactor"},
                         json.loads(self.prefs().core_skin_path.read_text(encoding="utf-8")))

    def test_invalid_values_load_as_classic(self) -> None:
        prefs = self.prefs()
        for value in INVALID:
            with self.subTest(value=str(value)[:12]):
                prefs.core_skin_path.write_text(json.dumps({"version": 1, "skin": value}), encoding="utf-8")
                self.assertEqual("classic", self.prefs().core_skin)
                self.assertFalse(prefs.set_core_skin(value))
        for raw in ("{", "[]", '{"version": 2, "skin": "reactor"}', '{"version": 1, "skin": "reactor", "x": 1}'):
            with self.subTest(raw=raw):
                prefs.core_skin_path.write_text(raw, encoding="utf-8")
                self.assertEqual("classic", self.prefs().core_skin)

    def test_an_existing_file_without_the_key_loads_as_before(self) -> None:
        prefs = self.prefs()
        prefs.data["theme"] = "dark"
        self.assertTrue(prefs.save())
        before = self.prefs_path.read_bytes()
        loaded = self.prefs()
        self.assertEqual(prefs.data, loaded.data)
        self.assertEqual("classic", loaded.core_skin)
        self.assertTrue(loaded.set_core_skin("reactor"))
        self.assertEqual(before, self.prefs_path.read_bytes())       # the main document is never touched
        self.assertNotIn("core_skin", self.prefs().data)
        self.assertEqual("dark", self.prefs().data["theme"])

    def test_bridge_ignores_unknown_and_notifies_once_per_real_change(self) -> None:
        bridge = self.bridge()
        notes: list[int] = []
        bridge.prefsChanged.connect(lambda: notes.append(1))
        self.assertEqual("classic", bridge.property("coreSkin"))
        for value in ("bogus", "", "classic"):
            bridge.setCoreSkin(value)
        self.assertEqual([], notes)
        bridge.setCoreSkin("reactor")
        bridge.setCoreSkin("reactor")
        self.assertEqual(([1], "reactor"), (notes, bridge.property("coreSkin")))
        bridge.setCoreSkin("classic")
        self.assertEqual(([1, 1], "classic"), (notes, bridge.property("coreSkin")))
        self.assertEqual("classic", UiPreferences(self.prefs_path).core_skin)


class CoreSkinLayerTests(unittest.TestCase):
    load = test_dfcoreb.CoreLayerTests.load
    running = staticmethod(test_dfcoreb.CoreLayerTests.running)
    STILL = ("mainAngle", "secondAngle", "sweepAngle", "flow", "clockT", "iris")

    def default(self):
        engine = QQmlEngine()
        self.addCleanup(engine.deleteLater)
        bridge = StubBridge()
        self.addCleanup(bridge.deleteLater)
        engine.rootContext().setContextProperty("bridge", bridge)
        component = QQmlComponent(engine)
        component.setData(b'import QtQuick\nimport "components"\nJarvisCore { width: 162; height: 162; active: false }',
                          QUrl.fromLocalFile(str(QML_DIR / "dfcoreskin_probe.qml")))
        core = component.create()
        self.assertIsNotNone(core, component.errorString())
        self.addCleanup(core.deleteLater)
        self.addCleanup(component.deleteLater)
        return core

    def test_without_a_design_binding_the_core_is_classic(self) -> None:
        core = self.default()
        pump(50)
        self.assertEqual("classic", core.property("design"))
        self.assertEqual(["ClassicLayer"], layers(core))

    def test_a_switch_while_frozen_stays_frozen_and_still(self) -> None:
        core, bridge, warnings = self.load("classic")
        core.setProperty("active", True)
        pump(2400)
        core.setProperty("state_", "disabled")
        pump(1500)
        self.assertTrue(core.property("dormant"))
        self.assertEqual([], self.running(core))
        still = [core.property(n) for n in self.STILL]
        for design, layer in (("reactor", "ReactorLayer"), ("classic", "ClassicLayer")):
            core.setProperty("design", design)
            pump(300)
            self.assertEqual([layer], layers(core))
            self.assertTrue(core.property("dormant"))
            self.assertEqual([], self.running(core))
            self.assertEqual(still, [core.property(n) for n in self.STILL])
        self.assertEqual([], warnings)

    def test_a_switch_while_off_stays_off(self) -> None:
        core, _bridge, warnings = self.load("classic")
        pump(300)
        off = (core.property("alive"), self.running(core), core.property("clockT"))
        for design in ("reactor", "classic"):
            core.setProperty("design", design)
            pump(300)
            self.assertFalse(core.property("active"))
            self.assertEqual(off, (core.property("alive"), self.running(core), core.property("clockT")))
        self.assertEqual([], warnings)

    def test_the_classic_layer_only_draws(self) -> None:
        layer = CLASSIC.read_text(encoding="utf-8")
        for name in ("applyState", "fire", "stopEvent", "Travel"):
            self.assertNotRegex(layer, rf"\b(?:function|component) {name}\b")
            self.assertNotRegex(layer, rf"\b{name}\s*\(")
        self.assertNotRegex(layer, r"\bTimer\s*\{")
        self.assertNotRegex(layer, r"Animation\s*\{|Behavior on")
        self.assertIn("required property Item core", layer)
        layer_ids = set(re.findall(r"\bid: (\w+)", layer))
        logic = re.sub(r'"[^"\n]*"|//[^\n]*', "", CORE.read_text(encoding="utf-8"))
        self.assertEqual(set(), {i for i in layer_ids if re.search(rf"\b{i}\b", logic)})


class CoreSkinDrawerTests(UiTestCase):
    item = test_ui.PresentationTests.item
    click = test_ui.PresentationTests.click
    window = test_ui.PresentationTests.window

    def test_the_drawer_switches_the_live_core_without_a_warning(self) -> None:
        from PySide6.QtCore import qInstallMessageHandler
        messages: list[str] = []
        previous = qInstallMessageHandler(lambda mode, context, text: messages.append(text))
        self.addCleanup(qInstallMessageHandler, previous)
        bridge = self.bridge()
        root = self.window(bridge)
        core = self.item(root, "jarvisCore")
        self.assertEqual(["ClassicLayer"], layers(core))
        self.click(root, "settingsOrb")
        scroll = self.item(root, "coreSkin_classic").parentItem()
        while scroll.metaObject().className() != "QQuickFlickable":     # the selector sits last in the panel
            scroll = scroll.parentItem()
        scroll.setProperty("contentY", scroll.property("contentHeight") - scroll.property("height"))
        pump(100)
        self.click(root, "coreSkin_reactor")
        self.assertEqual(("reactor", ["ReactorLayer"]), (bridge.property("coreSkin"), layers(core)))
        self.assertTrue(self.item(root, "coreSkin_reactor").property("selected"))
        self.click(root, "coreSkin_classic")
        self.assertEqual(("classic", ["ClassicLayer"]), (bridge.property("coreSkin"), layers(core)))
        self.assertEqual("classic", UiPreferences(self.prefs_path).core_skin)
        self.assertEqual([], [m for m in messages if ".qml" in m])


if __name__ == "__main__":
    unittest.main()
