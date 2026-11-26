"""FACETOKENS1: locked design values, contrast pairs and the font plan of the new face."""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

from jarvis.face import tokens
from jarvis.face.tokens import (
    FONT_DIR, PAIRS, ROLES, TOKENS, Refusal, as_qml_dict, contrast, font_plan, motion,
)
from tests.adversarial import HOSTILE_PATH_PARTS, HOSTILE_TEXT, each

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "jarvis" / "face" / "tokens.py"

COLORS = {
    "base": "#0B0E13", "surface1": "#11161D", "surface2": "#171D26", "surface3": "#0E1218",
    "line1": "#1B222C", "line2": "#232B36", "text1": "#E7ECF2", "text2": "#AAB4C0",
    "text3": "#8793A1", "accent": "#5BC8F5", "accent_ink": "#06202C",
    "accent_surface": "#0F1C24", "accent_line": "#1D3A4A", "needs_you": "#F2B34A",
    "error": "#F2777F", "ok": "#6FD3A3", "disabled_glyph": "#5E6978",
}
SANS = ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf")
MONO = ("JetBrainsMono-Regular.ttf", "JetBrainsMono-Medium.ttf", "JetBrainsMono-SemiBold.ttf")
ALL_FONTS = [str(FONT_DIR / n) for n in SANS + MONO]


def bad_hex(colors):
    """Colour names whose value is not exactly #RRGGBB (upper-case hex)."""
    return [k for k, v in colors.items() if not (type(v) is str and re.fullmatch(r"#[0-9A-F]{6}", v))]


def untested(colors, roles, pairs):
    """Colours with no role, and text/surface colours that no PAIRS entry uses."""
    used = {name for pair in pairs for name in pair}
    return sorted(k for k in colors
                  if k not in roles or (roles[k] in ("text", "surface") and k not in used))


def translucent(flat):
    """Keys/values that smell of blending: gradient, rgba, opacity, alpha, or #RRGGBBAA."""
    words = ("gradient", "rgba", "opacity", "alpha")
    hits = []
    for k, v in flat.items():
        text = f"{k} {v}".lower()
        if any(w in text for w in words) or re.search(r"#[0-9a-f]{8}\b", text):
            hits.append(k)
    return hits


class AcceptanceTest(unittest.TestCase):
    def test_tokens_are_immutable(self):
        with self.assertRaises(TypeError):
            TOKENS["color"]["base"] = "#000000"  # type: ignore[index]
        with self.assertRaises(TypeError):
            TOKENS["extra"] = {}  # type: ignore[index]
        for group in TOKENS.values():
            for value in group.values():
                self.assertIn(type(value), (str, int, float, tuple))

    def test_locked_values(self):
        self.assertEqual(dict(TOKENS["color"]), COLORS)
        self.assertEqual(dict(TOKENS["type"]), {
            "body": (17, 28), "h2": (22, 30), "h3": (17, 26), "code": (14.5, 22),
            "small": (13, 20), "label": (12.5, 16),
            "family_sans": "Inter", "family_mono": "JetBrains Mono",
        })
        self.assertEqual(dict(TOKENS["size"]), {
            "ring": 72, "control_min": 44, "reading_col": (640, 660), "reader_width": 880,
            "rail": 320, "pane": 600, "composer_radius": 16, "mini_box": 460,
        })
        self.assertEqual(dict(TOKENS["motion"]), {
            "fast": 120, "medium": 200, "slow": 320, "curve": (0.2, 0.0, 0.0, 1.0),
        })

    def test_motion_reduced_is_zero(self):
        self.assertEqual(motion(), {"fast": 120, "medium": 200, "slow": 320,
                                    "curve": (0.2, 0.0, 0.0, 1.0)})
        for reduced in (True, None, 1, "yes", "false", 0):  # anything but False is still
            m = motion(reduced)
            self.assertEqual((m["fast"], m["medium"], m["slow"]), (0, 0, 0), reduced)

    def test_as_qml_dict_flat_and_stable(self):
        flat = as_qml_dict()
        for k, v in flat.items():
            self.assertRegex(k, r"^[a-z][a-z0-9_]*$")
            self.assertIn(type(v), (str, int, float), k)
        expected = {f"color_{n}" for n in COLORS}
        expected |= {f"type_{s}_{p}" for s in ("body", "h2", "h3", "code", "small", "label")
                     for p in ("size", "line")}
        expected |= {"type_family_sans", "type_family_mono", "size_reading_col_min",
                     "size_reading_col_max", "motion_fast", "motion_medium", "motion_slow",
                     "motion_curve_x1", "motion_curve_y1", "motion_curve_x2", "motion_curve_y2"}
        expected |= {f"size_{n}" for n in ("ring", "control_min", "reader_width", "rail",
                                           "pane", "composer_radius", "mini_box")}
        self.assertEqual(set(flat), expected)
        self.assertEqual((flat["type_code_size"], flat["size_reading_col_max"]), (14.5, 660))
        flat["color_base"] = "#FFFFFF"
        self.assertEqual(as_qml_dict()["color_base"], "#0B0E13")


class ContrastTest(unittest.TestCase):
    def test_known_ratios(self):
        self.assertAlmostEqual(contrast("#FFFFFF", "#000000"), 21.0, places=6)
        self.assertAlmostEqual(contrast("#000000", "#FFFFFF"), 21.0, places=6)
        self.assertAlmostEqual(contrast("#5BC8F5", "#5BC8F5"), 1.0, places=6)
        self.assertAlmostEqual(contrast(COLORS["text3"], COLORS["surface2"]), 5.42, delta=0.01)

    def test_every_pair_passes_aa(self):
        self.assertGreater(len(PAIRS), 30)
        for fg, bg in PAIRS:
            ratio = contrast(COLORS[fg], COLORS[bg])
            self.assertGreaterEqual(ratio, 4.5, f"{fg} on {bg}: {ratio:.2f}")

    def test_disabled_glyph_is_non_text_and_exempt(self):
        self.assertEqual(ROLES["disabled_glyph"], "non_text")
        self.assertNotIn("disabled_glyph", {n for p in PAIRS for n in p})
        self.assertLess(contrast(COLORS["disabled_glyph"], COLORS["base"]), 4.5)

    def test_no_untested_colour(self):
        self.assertEqual(untested(COLORS, ROLES, PAIRS), [])
        self.assertEqual(set(ROLES), set(COLORS))
        doctored = dict(COLORS, text4="#FFFFFF")
        self.assertEqual(untested(doctored, dict(ROLES, text4="text"), PAIRS), ["text4"])
        self.assertEqual(untested(doctored, ROLES, PAIRS), ["text4"])
        dropped = tuple(p for p in PAIRS if "surface3" not in p)
        self.assertEqual(untested(COLORS, ROLES, dropped), ["surface3"])

    def test_bad_colour_is_typed_refusal(self):
        bad = ["#12345", "#0b0e13", "#0B0E13\n", "#0B0E13FF", "0B0E13", "#GGGGGG", "# B0E13",
               "#+B0E13", "#٠١٢٣٤٥", "", None, 0x0B0E13, b"#0B0E13", ("#0B0E13",)]

        def check(value):
            self.assertEqual(contrast(value, "#000000"), Refusal("bad_colour"))
            self.assertEqual(contrast("#000000", value), Refusal("bad_colour"))
        each(self, bad + list(HOSTILE_TEXT), check)


def foreign_imports(source):
    """Imports that are relative, not stdlib, or network/process modules."""
    banned = {"urllib", "http", "socket", "ssl", "ftplib", "subprocess", "asyncio"}
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = ["." * node.level + (node.module or "")]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if name.startswith(".") or top not in sys.stdlib_module_names or top in banned:
                out.append(name)
    return out


class FontPlanTest(unittest.TestCase):
    def assert_family(self, entry, chosen, reason=""):
        self.assertEqual((entry["chosen"], entry["reason"]), (chosen, reason))
        if chosen == "bundled":
            self.assertEqual(len(entry["families"]), 1)
            self.assertEqual(len(entry["files"]), 3)
        else:
            self.assertEqual(entry["files"], ())

    def test_all_weights_bundled(self):
        for given in (ALL_FONTS, tuple(ALL_FONTS), set(ALL_FONTS), frozenset(ALL_FONTS),
                      [FONT_DIR / n for n in SANS + MONO], list(reversed(ALL_FONTS)) + ALL_FONTS):
            plan = font_plan(given)
            self.assertEqual(set(plan), {"sans", "mono"})
            self.assert_family(plan["sans"], "bundled")
            self.assert_family(plan["mono"], "bundled")
            self.assertEqual(plan["sans"]["families"], ("Inter",))
            self.assertEqual(plan["mono"]["families"], ("JetBrains Mono",))
            self.assertEqual(plan["sans"]["files"], tuple(ALL_FONTS[:3]))
            self.assertEqual(plan["mono"]["files"], tuple(ALL_FONTS[3:]))

    def test_missing_weight_falls_back_whole_family(self):
        chains = {"sans": ("Segoe UI Variable", "Segoe UI"), "mono": ("Cascadia Mono", "Consolas")}
        for i, name in enumerate(ALL_FONTS):
            hit, other = ("sans", "mono") if i < 3 else ("mono", "sans")
            plan = font_plan([f for f in ALL_FONTS if f != name])
            self.assert_family(plan[hit], "fallback", "incomplete_family")
            self.assertEqual(plan[hit]["families"], chains[hit])
            self.assert_family(plan[other], "bundled")
        plan = font_plan([])
        self.assertEqual({k: v["chosen"] for k, v in plan.items()},
                         {"sans": "fallback", "mono": "fallback"})

    def test_hostile_file_names_never_count(self):
        good = ALL_FONTS[0]
        base = str(FONT_DIR)
        variants = [
            base + "\\INTER-~1.TTF", good + ":x", good + ":$DATA", base + "\\Inter-Regular.otf",
            good + " ", good + ".", base + "\\..\\Inter-Regular.ttf",
            base + "\\sub\\Inter-Regular.ttf",
            "\\\\host\\share\\Inter-Regular.ttf", "\\\\?\\" + good, "\\\\.\\" + good,
            base + "\\CON", base + "\\NUL.ttf", base + "\\COM1.ttf", "Inter-Regular.ttf",
            str(ROOT / "jarvis" / "ui" / "qml" / "fonts" / "Inter-Regular.ttf"),
            good.encode(), None, 7, [good], ("x", good),
        ] + [base + "\\" + part for part in HOSTILE_PATH_PARTS]

        def check(variant):
            plan = font_plan(ALL_FONTS[1:] + [variant])
            self.assert_family(plan["sans"], "fallback", "incomplete_family")
            self.assert_family(plan["mono"], "bundled")
        each(self, variants, check)

    def test_bad_input_is_full_fallback(self):
        bad = [None, 7, ALL_FONTS[0], ALL_FONTS[0].encode(), (f for f in ALL_FONTS),
               iter(ALL_FONTS), dict.fromkeys(ALL_FONTS), FONT_DIR]

        def check(value):
            plan = font_plan(value)
            self.assert_family(plan["sans"], "fallback", "bad_input")
            self.assert_family(plan["mono"], "fallback", "bad_input")
        each(self, bad, check)

    def test_container_subclass_never_raises(self):
        def boom(*_a, **_k):
            raise RuntimeError("boom")

        bad = []
        for base in (list, tuple, set, frozenset):
            bad.append(type("Plain", (base,), {})(ALL_FONTS))  # benign subclass: identity, not isinstance
            for attr in ("__iter__", "__len__", "__contains__", "__getitem__"):
                bad.append(type("Raising", (base,), {attr: boom})(ALL_FONTS))

        def check(value):
            plan = font_plan(value)
            self.assert_family(plan["sans"], "fallback", "bad_input")
            self.assert_family(plan["mono"], "fallback", "bad_input")
        each(self, bad, check)

    def test_raising_item_never_escapes(self):
        def boom(*_a, **_k):
            raise RuntimeError("boom")

        def hostile(attrs):
            return type("Hostile", (type(Path(FONT_DIR)),), attrs)(FONT_DIR / SANS[0])

        class Benign(type(Path(FONT_DIR))):
            pass

        class Text(str):
            pass

        # PureWindowsPath(item) reads _raw_paths: raising there escaped before the fix.
        items = [hostile({"_raw_paths": property(boom, lambda _s, _v: None)}), Benign(ALL_FONTS[0]),
                 Text(ALL_FONTS[0]), PureWindowsPath(ALL_FONTS[0])]
        items += [hostile({a: property(boom)}) for a in ("_str", "parts", "parent", "name", "drive", "root")]
        items += [hostile({a: boom}) for a in ("__fspath__", "__str__", "__eq__", "__hash__")]

        def check(i):  # by index: repr() of a hostile item raises
            item = items[i]
            plan = font_plan(ALL_FONTS[1:] + [item])  # exact types only: a subclass never counts
            want = "bundled" if type(item) is PureWindowsPath else "fallback"
            self.assert_family(plan["sans"], want, "" if want == "bundled" else "incomplete_family")
            self.assert_family(plan["mono"], "bundled")
        each(self, range(len(items)), check)

    def test_error_while_iterating_is_full_fallback(self):
        def boom(_item):
            raise RuntimeError("boom")

        with mock.patch.object(tokens, "_accepted_name", boom):
            plan = font_plan(ALL_FONTS)
        self.assert_family(plan["sans"], "fallback", "bad_input")
        self.assert_family(plan["mono"], "fallback", "bad_input")

        def interrupt(_item):
            raise KeyboardInterrupt

        with mock.patch.object(tokens, "_accepted_name", interrupt), self.assertRaises(KeyboardInterrupt):
            font_plan(ALL_FONTS)  # only Exception is caught; Ctrl-C still stops the caller

    def test_metaclass_eq_does_not_fake_exact_type(self):
        class EqAll(type):  # `type(x) in (...)` would match through this __eq__
            __eq__ = lambda cls, other: True  # noqa: E731
            __hash__ = type.__hash__

        class FakeList(metaclass=EqAll):
            def __iter__(self):
                return iter(ALL_FONTS)

        class FakePath(metaclass=EqAll):
            def __init__(self, path):
                self.path = path

            def __fspath__(self):
                return self.path

        plan = font_plan(FakeList())
        self.assert_family(plan["sans"], "fallback", "bad_input")
        self.assert_family(plan["mono"], "fallback", "bad_input")
        plan = font_plan(ALL_FONTS[1:] + [FakePath(ALL_FONTS[0])])
        self.assert_family(plan["sans"], "fallback", "incomplete_family")
        self.assertEqual(plan["mono"]["chosen"], "bundled")


class StaticTest(unittest.TestCase):
    def test_colours_are_rrggbb(self):
        doctored = {"a": "#12345", "b": "#0b0e13", "c": "#0B0E13\n", "d": "#0B0E13FF",
                    "e": None, "f": "rgb(1,2,3)", "ok": "#0B0E13"}
        self.assertEqual(bad_hex(doctored), ["a", "b", "c", "d", "e", "f"])
        self.assertEqual(bad_hex(TOKENS["color"]), [])
        for value in doctored.values():
            self.assertEqual(tokens.is_hex(value), not bad_hex({"x": value}), repr(value))

    def test_no_translucent_token(self):
        self.assertEqual(translucent(as_qml_dict()), [])
        for doctored in ({"color_veil_alpha": "#000000"}, {"x": "rgba(0,0,0,.5)"},
                         {"x": "#0B0E13FF"}, {"opacity_x": 1}, {"g": "linear-gradient"}):
            self.assertEqual(translucent(doctored), list(doctored))
        text = SOURCE.read_text(encoding="utf-8").lower()
        for word in ("gradient", "rgba", "opacity", "alpha"):
            self.assertNotIn(word, text)

    def test_stdlib_only_and_no_io(self):
        self.assertEqual(foreign_imports("import requests\nfrom . import x\nimport urllib.request"),
                         ["requests", ".", "urllib.request"])
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        self.assertEqual(foreign_imports(SOURCE.read_text(encoding="utf-8")), [])
        io = {"open", "read_text", "read_bytes", "exists", "is_file", "iterdir", "glob",
              "resolve", "stat", "urlopen", "listdir", "scandir"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                self.assertNotIn(name, io, ast.dump(node)[:80])

    def test_no_heartbeat_definition(self):
        for path in (SOURCE, Path(__file__)):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defs = {n.name for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.assertNotIn("heartbeat", defs, path.name)
