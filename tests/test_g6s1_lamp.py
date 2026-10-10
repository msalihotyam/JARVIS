"""Gate 6G S1 part A: the mechanical status lamp component and its layers.

* MechanicalLamp.qml loads offscreen with every layer; ONE `size` scales them all.
* One drive: ready slow, running normal, freezing eases to a stop, frozen = 0;
  meshing wheels turn opposite at a speed ~ 1/diameter.
* Ambient off = completely still: the drive does not run, nothing turns.
* The QML part map equals the generator's (assets/lamp/parts.json), whose
  pivots are the generator's source (scripts/lamp_assets/parts.py).
* The assets exist at master size and stay under the 3 MB cap.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from tests.test_ui import _APP, pump  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import Q_ARG, Q_RETURN_ARG, QMetaObject, QObject, QPointF, QUrl  # noqa: E402
from PySide6.QtGui import QImageReader  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402

from jarvis.ui.typography import FONT_DIR  # noqa: E402

QML_DIR = FONT_DIR.parent
ASSETS = QML_DIR / "assets" / "lamp"
GENERATOR = Path(__file__).resolve().parents[1] / "scripts" / "lamp_assets"
CAP = 3_000_000
MASTER = 1254


def part_map() -> dict:
    return json.loads((ASSETS / "parts.json").read_text(encoding="utf-8"))


class LampCase(unittest.TestCase):
    def make(self, state: str = "running", size: int = 120):
        engine = QQmlEngine()
        self.addCleanup(engine.deleteLater)
        component = QQmlComponent(engine)
        self.keep = getattr(self, "keep", []) + [engine, component]     # alive as long as the test
        component.setData(f"""import QtQuick
import "components"
import "theme"
Item {{
    property Item lamp: lamp
    readonly property string a11y: lamp.Accessible.name
    function setAmbient(on) {{ Theme.ambient = on }}
    function spinOf(n) {{ return lamp.spin(n) }}
    function advance(seconds) {{ for (let t = 0; t < seconds; t += 1 / 60) lamp.step(1 / 60) }}
    MechanicalLamp {{ id: lamp; size: {size}; executionState: "{state}" }}
}}""".encode(), QUrl.fromLocalFile(str(QML_DIR / "lamp_probe.qml")))
        root = component.create()
        self.assertIsNotNone(root, component.errorString())
        self.addCleanup(root.deleteLater)
        root.setAmbient(True)
        return root, root.property("lamp")

    def call(self, root, name, *args):
        values = [Q_ARG("QVariant", a) for a in args]
        return QMetaObject.invokeMethod(root, name, Q_RETURN_ARG("QVariant"), *values)

    def walk(self, lamp):
        found, stack = [], [lamp]
        while stack:
            found.append(stack.pop())
            stack.extend(found[-1].childItems())
        return found

    def items(self, lamp):
        return {node.objectName(): node for node in self.walk(lamp) if node.objectName()}


class LampLoadTests(LampCase):
    def test_loads_offscreen_with_every_layer(self) -> None:
        _root, lamp = self.make()
        items = self.items(lamp)
        meta = part_map()
        names = ([f"lampLayer_{n}" for n in meta["layers"]] + [f"lampPart_{p['n']}" for p in meta["parts"]]
                 + [f"lampStrip_{s['n']}" for s in meta["strips"]])
        for name in names:
            self.assertIn(name, items)
        images = [i for i in self.walk(lamp) if i.property("source") is not None and i.property("progress") is not None]
        self.assertEqual(4 + 19 + 3 * 2, len(images))
        for image in images:                                                   # loaded (Image.Ready)
            self.assertEqual(1.0, image.property("progress"), image.property("source"))
            self.assertGreater(image.property("implicitWidth"), 0, image.property("source"))

    def test_one_size_scales_every_layer(self) -> None:
        meta = part_map()
        for size in (120, 60, 33):
            with self.subTest(size=size):
                _root, lamp = self.make("frozen", size=size)           # at rest
                self.assertEqual((size, size), (lamp.property("implicitWidth"), lamp.property("implicitHeight")))
                k, items = size / MASTER, self.items(lamp)

                def at(item, x, y):
                    point = item.mapToItem(lamp, QPointF(x, y))
                    return round(point.x(), 3), round(point.y(), 3)

                for name, geo in meta["layers"].items():
                    item = items[f"lampLayer_{name}"]
                    self.assertEqual((round(geo["x"] * k, 3), round(geo["y"] * k, 3)), at(item, 0, 0), name)
                    self.assertEqual((round((geo["x"] + geo["w"]) * k, 3), round((geo["y"] + geo["h"]) * k, 3)),
                                     at(item, geo["w"], geo["h"]), name)
                for geo in meta["parts"]:                                # a part turns about its pivot
                    item = items[f"lampPart_{geo['n']}"]
                    self.assertEqual((round(geo["cx"] * k, 3), round(geo["cy"] * k, 3)),
                                     at(item, geo["cx"] - geo["x"], geo["cy"] - geo["y"]), geo["n"])
                for geo in meta["strips"]:
                    item = items[f"lampStrip_{geo['n']}"]
                    self.assertEqual((round(geo["x0"] * k, 3), round(geo["y0"] * k, 3)), at(item, 0, geo["hw"]), geo["n"])

    def test_the_accessible_name_says_the_state_in_words(self) -> None:
        root, lamp = self.make("ready")
        words = {}
        for state in ("ready", "running", "freezing", "frozen", "bogus"):
            lamp.setProperty("executionState", state)
            words[state] = root.property("a11y")
        self.assertEqual("JARVIS status: ready", words["ready"])
        self.assertEqual("JARVIS status: running an operation", words["running"])
        self.assertEqual("JARVIS status: freeze requested", words["freezing"])
        self.assertEqual("JARVIS status: frozen", words["frozen"])
        self.assertEqual(words["ready"], words["bogus"])
        self.assertIn("Accessible.role: Accessible.Indicator", (QML_DIR / "components" / "MechanicalLamp.qml").read_text(encoding="utf-8"))


class LampDriveTests(LampCase):
    def test_each_state_settles_on_its_speed(self) -> None:
        for state, speed, swing in (("ready", 0.25, 12), ("running", 1, 30), ("freezing", 0, 0), ("frozen", 0, 0)):
            with self.subTest(state=state):
                _root, lamp = self.make(state)
                self.assertAlmostEqual(speed, lamp.property("speed"))
                self.assertAlmostEqual(swing, lamp.property("swing"))
        _root, lamp = self.make("bogus")                         # an unknown state reads as ready
        self.assertAlmostEqual(0.25, lamp.property("speed"))
        self.assertEqual(24, lamp.property("base"))              # LAMP APPROVED: 24 deg/s at 100%

    def test_the_speed_eases_between_states(self) -> None:
        root, lamp = self.make("running")
        lamp.setProperty("executionState", "ready")
        self.call(root, "advance", 0.2)
        self.assertTrue(0.25 < lamp.property("speed") < 1, lamp.property("speed"))      # no jump
        self.call(root, "advance", 6)
        self.assertAlmostEqual(0.25, lamp.property("speed"), places=3)
        lamp.setProperty("executionState", "running")
        self.call(root, "advance", 0.2)
        self.assertTrue(0.25 < lamp.property("speed") < 1)
        self.call(root, "advance", 6)
        self.assertAlmostEqual(1, lamp.property("speed"), places=3)

    def test_freezing_winds_down_in_about_four_seconds_and_frozen_is_zero(self) -> None:
        root, lamp = self.make("running")
        lamp.setProperty("executionState", "freezing")
        elapsed = 0.0
        while lamp.property("speed") > 0 and elapsed < 10:
            self.call(root, "advance", 0.1)
            elapsed += 0.1
        self.assertEqual(0, lamp.property("speed"))
        self.assertEqual(0, lamp.property("swing"))
        self.assertTrue(3 <= elapsed <= 6, elapsed)
        self.assertFalse(lamp.property("moving"))
        lamp.setProperty("executionState", "frozen")
        motor = lamp.property("motor")
        self.call(root, "advance", 2)
        self.assertEqual(motor, lamp.property("motor"))                # frozen: nothing turns
        self.assertEqual(0, self.call(root, "spinOf", "w7"))           # the cage rests

    def test_meshing_wheels_turn_opposite_at_speed_over_diameter(self) -> None:
        root, lamp = self.make("running")
        self.call(root, "advance", 1)
        self.assertAlmostEqual(24, lamp.property("motor"), delta=0.5)          # one second at 100%
        w1, w2, w3 = (self.call(root, "spinOf", n) for n in ("w1", "w2", "w3"))
        self.assertGreater(w1, 0)
        self.assertLess(w2, 0)                                                  # w1 meshes w2
        self.assertGreater(w3, 0)                                               # w2 meshes w3
        self.assertAlmostEqual(abs(w2) / w1, 195 / 103, places=6)
        self.assertAlmostEqual(abs(w3) / w1, 195 / 67, places=6)
        self.assertNotEqual(0, self.call(root, "spinOf", "w7"))                # the balance swings
        ticks = []                                                             # the escapement rests, then ticks 15 deg
        for _ in range(240):
            self.call(root, "advance", 1 / 60)
            ticks.append(self.call(root, "spinOf", "w4"))
        self.assertTrue(all(b <= a for a, b in zip(ticks, ticks[1:])))
        resting = [t for t in ticks if abs(t / 15 - round(t / 15)) < 1e-9]
        self.assertTrue(0.6 * len(ticks) <= len(resting) < len(ticks), len(resting))

    def test_the_drive_runs_live_only_while_something_moves(self) -> None:
        _root, lamp = self.make("running")
        drive = lamp.findChild(QObject, "lampDrive")
        self.assertIsNotNone(drive)
        self.assertTrue(drive.property("running"))
        motor = lamp.property("motor")
        pump(300)
        self.assertGreater(lamp.property("motor"), motor)


class LampAmbientTests(LampCase):
    def test_ambient_off_is_completely_still(self) -> None:
        for how in ("lamp", "theme"):
            with self.subTest(how=how):
                root, lamp = self.make("running")
                if how == "lamp":
                    lamp.setProperty("ambient", False)
                else:
                    root.setAmbient(False)
                drive = lamp.findChild(QObject, "lampDrive")
                self.assertFalse(lamp.property("moving"))
                self.assertFalse(drive.property("running"))
                motor, phase = lamp.property("motor"), lamp.property("cagePhase")
                pump(250)
                self.assertEqual((motor, phase), (lamp.property("motor"), lamp.property("cagePhase")))


class LampAssetTests(LampCase):
    def test_the_qml_part_map_equals_the_generator_s(self) -> None:
        meta = part_map()
        _root, lamp = self.make()
        qml_parts = {p["n"]: p for p in lamp.property("parts").toVariant()}
        self.assertEqual({p["n"] for p in meta["parts"]}, set(qml_parts))
        for part in meta["parts"]:
            for key in ("x", "y", "w", "h", "cx", "cy"):
                self.assertEqual(part[key], qml_parts[part["n"]][key], (part["n"], key))
            self.assertEqual(part.get("r"), qml_parts[part["n"]].get("r"), part["n"])
        qml_strips = {s["n"]: s for s in lamp.property("strips").toVariant()}
        self.assertEqual({s["n"] for s in meta["strips"]}, set(qml_strips))
        for strip in meta["strips"]:
            for key in ("w", "h", "x0", "y0", "ang", "len", "hw", "pitch", "sg"):
                self.assertAlmostEqual(strip[key], qml_strips[strip["n"]][key], places=9, msg=(strip["n"], key))
        self.assertEqual(meta["layers"], lamp.property("layers").toVariant())
        # The pivots are the generator's own geometry (master px).
        sys.path.insert(0, str(GENERATOR))
        try:
            import parts as source
        finally:
            sys.path.remove(str(GENERATOR))
        pivots = {f"w{k}": (cx, cy, r) for k, (cx, cy, r, _kind) in source.W.items()}
        pivots.update({n: v for n, v in source.PLANETS.items()})
        pivots.update({f"l{k}": (*pv, None) for k, (pv, _poly) in source.LEVERS.items() if k in (16, 17)})
        self.assertEqual(set(pivots), {p["n"] for p in meta["parts"]})
        for part in meta["parts"]:
            cx, cy, r = pivots[part["n"]]
            self.assertEqual((cx, cy), (part["cx"], part["cy"]), part["n"])
            self.assertEqual(r, part.get("r"), part["n"])

    def test_assets_exist_at_master_size_under_the_cap(self) -> None:
        meta = part_map()
        sizes = dict(meta["layers"])
        sizes.update({p["n"]: p for p in meta["parts"]})
        sizes.update({s["n"]: s for s in meta["strips"]})
        total = 0
        for name, geo in sizes.items():
            path = ASSETS / f"{name}.png"
            self.assertTrue(path.is_file(), path)
            size = QImageReader(str(path)).size()
            self.assertEqual((geo["w"], geo["h"]), (size.width(), size.height()), name)
            total += path.stat().st_size
        self.assertEqual(sorted(p.name for p in ASSETS.glob("*.png")), sorted(f"{n}.png" for n in sizes))
        self.assertEqual((MASTER, MASTER), (meta["W"], meta["H"]))               # master resolution
        print(f"\nlamp assets: {len(sizes)} PNG, {total} bytes", file=sys.stderr)
        self.assertLessEqual(total, CAP)


if __name__ == "__main__":
    unittest.main()
