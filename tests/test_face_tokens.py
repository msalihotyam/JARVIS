"""FACETOKENS1: locked design values, contrast pairs and the font plan of the new face."""
from __future__ import annotations

import ast
import re
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path, PurePosixPath, PureWindowsPath
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
    "needs_you_ink": "#2A1C03", "error": "#F2777F", "ok": "#6FD3A3", "disabled_glyph": "#5E6978",
    "link": "#7FD4F7", "link_hover": "#B5E6FB", "text_soft": "#C5CED8",
}
RADIUS = {"panel": 12, "composer": 16, "control": 12, "code": 8, "pill": 999}
SPACE = (4, 6, 8, 10, 12, 14, 16, 20, 24)
DURATIONS = {"breathe": 4000, "swell": 700, "arc": 1200, "arc2": 2400, "arc3": 3200,
             "ripple": 1600, "ask": 1600}
RING = {"box": 120, "outer_r": 54, "outer_w": 1.5, "dash_r": 46, "dash_w": 2, "dash_on": 2,
        "dash_off": 6, "main_r": 38, "main_w": 4, "glow_r": 20, "core_r": 12}
TYPE_EXTRA = {"label_weight": 600, "label_caps": 1, "label_spacing_em": 0.04, "num_weight": 600,
              "num_unit_weight": 500, "mono_weight": 500}
SIZE_EXTRA = {"pane_header": 56, "tab": 36, "bar": 8, "tick_w": 2, "tick_h": 16}
#: EK3 spec names that main already holds under its own name (main's name wins, no duplicate).
EK3_SAME = {
    "pane": ("surface3", "#0E1218"), "tile": ("surface1", "#11161D"), "ok": ("ok", "#6FD3A3"),
    "faint": ("disabled_glyph", "#5E6978"), "chip_now_fill": ("accent_surface", "#0F1C24"),
    "chip_now_line": ("accent_line", "#1D3A4A"), "line1": ("line1", "#1B222C"),
    "line2": ("line2", "#232B36"), "surface2": ("surface2", "#171D26"), "text": ("text1", "#E7ECF2"),
    "text2": ("text2", "#AAB4C0"), "text_soft": ("text_soft", "#C5CED8"),
    "muted": ("text3", "#8793A1"), "accent": ("accent", "#5BC8F5"),
    "needs_you": ("needs_you", "#F2B34A"),
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


def non_numeric(flat):
    """Keys of a flat QML dict that must be numbers but are not exactly int or float (bool counts as bad)."""
    text_keys = {k for k in flat if k.startswith("color_") or k.startswith("type_family_")}
    return sorted(k for k, v in flat.items() if k not in text_keys and type(v) not in (int, float))


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
            "turn": (16, 26), "cell": (15, 24), "meta": (13, 20), "control": (14, 20),
            "family_sans": "Inter", "family_mono": "JetBrains Mono",
            "num": (28, 34), "num_unit": (17, 26), "mono": (13, 20), **TYPE_EXTRA,
        })
        self.assertEqual(dict(TOKENS["radius"]), RADIUS)
        self.assertEqual(dict(TOKENS["space"]), {str(n): n for n in SPACE})
        self.assertEqual(dict(TOKENS["size"]), {
            "ring": 72, "control_min": 44, "reading_col": (640, 660), "reader_width": 880,
            "rail": 320, "pane": 600, "composer_radius": 16, "mini_box": 460, **SIZE_EXTRA,
        })
        self.assertEqual(dict(TOKENS["motion"]), {
            "fast": 120, "medium": 200, "slow": 320, "curve": (0.2, 0.0, 0.0, 1.0), **DURATIONS,
        })
        self.assertEqual(dict(TOKENS["ring"]), RING)

    def test_motion_reduced_is_zero(self):
        self.assertEqual(motion(), {"fast": 120, "medium": 200, "slow": 320,
                                    "curve": (0.2, 0.0, 0.0, 1.0), **DURATIONS})
        for reduced in (True, None, 1, "yes", "false", 0):  # anything but False is still
            m = motion(reduced)
            self.assertEqual((m["fast"], m["medium"], m["slow"]), (0, 0, 0), reduced)
            self.assertEqual({k: m[k] for k in DURATIONS}, dict.fromkeys(DURATIONS, 0), reduced)
            self.assertEqual(m["curve"], (0.2, 0.0, 0.0, 1.0), reduced)

    def test_as_qml_dict_flat_and_stable(self):
        flat = as_qml_dict()
        for k, v in flat.items():
            self.assertRegex(k, r"^[a-z][a-z0-9_]*$")
            self.assertIn(type(v), (str, int, float), k)
        expected = {f"color_{n}" for n in COLORS}
        expected |= {f"type_{s}_{p}" for s in ("body", "h2", "h3", "code", "small", "label",
                                               "turn", "cell", "meta", "control",
                                               "num", "num_unit", "mono")
                     for p in ("size", "line")}
        expected |= {f"radius_{n}" for n in RADIUS} | {f"space_{n}" for n in SPACE}
        expected |= {"type_family_sans", "type_family_mono", "size_reading_col_min",
                     "size_reading_col_max", "motion_fast", "motion_medium", "motion_slow",
                     "motion_curve_x1", "motion_curve_y1", "motion_curve_x2", "motion_curve_y2"}
        expected |= {f"size_{n}" for n in ("ring", "control_min", "reader_width", "rail",
                                           "pane", "composer_radius", "mini_box")}
        expected |= {f"type_{n}" for n in TYPE_EXTRA} | {f"size_{n}" for n in SIZE_EXTRA}
        expected |= {f"motion_{n}" for n in DURATIONS} | {f"ring_{n}" for n in RING}
        self.assertEqual(set(flat), expected)
        self.assertEqual((flat["type_code_size"], flat["size_reading_col_max"]), (14.5, 660))
        self.assertEqual((flat["type_turn_line"], flat["radius_pill"], flat["space_24"],
                          flat["color_link_hover"]), (26, 999, 24, "#B5E6FB"))
        flat["color_base"] = "#FFFFFF"
        self.assertEqual(as_qml_dict()["color_base"], "#0B0E13")

    def test_ek2_ek3_exact_values(self):
        flat = as_qml_dict()
        self.assertEqual(flat["color_needs_you_ink"], "#2A1C03")
        for spec, (name, value) in EK3_SAME.items():
            self.assertEqual(flat[f"color_{name}"], value, spec)
        for n, v in RING.items():
            self.assertEqual(flat[f"ring_{n}"], v, n)
        for n, v in DURATIONS.items():
            self.assertEqual(flat[f"motion_{n}"], v, n)
        for n, v in TYPE_EXTRA.items():
            self.assertEqual(flat[f"type_{n}"], v, n)
        for n, v in SIZE_EXTRA.items():
            self.assertEqual(flat[f"size_{n}"], v, n)
        self.assertEqual({k: flat[k] for k in ("type_label_size", "type_label_line", "type_num_size",
                                               "type_num_line", "type_num_unit_size",
                                               "type_num_unit_line", "type_mono_size",
                                               "type_mono_line", "size_pane")},
                         {"type_label_size": 12.5, "type_label_line": 16, "type_num_size": 28,
                          "type_num_line": 34, "type_num_unit_size": 17, "type_num_unit_line": 26,
                          "type_mono_size": 13, "type_mono_line": 20, "size_pane": 600})


class ContrastTest(unittest.TestCase):
    def test_known_ratios(self):
        self.assertAlmostEqual(contrast("#FFFFFF", "#000000"), 21.0, places=6)
        self.assertAlmostEqual(contrast("#000000", "#FFFFFF"), 21.0, places=6)
        self.assertAlmostEqual(contrast("#5BC8F5", "#5BC8F5"), 1.0, places=6)
        self.assertAlmostEqual(contrast(COLORS["text3"], COLORS["surface2"]), 5.42, delta=0.01)
        self.assertAlmostEqual(contrast(COLORS["link"], COLORS["surface2"]), 10.21, delta=0.01)

    def test_every_pair_passes_aa(self):
        self.assertGreater(len(PAIRS), 30)
        surfaces = [k for k, r in ROLES.items() if r == "surface"]
        for fg in ("link", "link_hover", "text_soft"):  # new text colours: every surface
            self.assertEqual({bg for f, bg in PAIRS if f == fg}, set(surfaces), fg)
        for fg, bg in PAIRS:
            ratio = contrast(COLORS[fg], COLORS[bg])
            self.assertGreaterEqual(ratio, 4.5, f"{fg} on {bg}: {ratio:.2f}")

    def test_needs_you_ink_pair(self):
        self.assertEqual({p for p in PAIRS if "needs_you_ink" in p}, {("needs_you_ink", "needs_you")})
        self.assertGreaterEqual(contrast("#2A1C03", "#F2B34A"), 4.5)
        self.assertFalse({p for p in PAIRS if "disabled_glyph" in p})  # faint: no pair

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

        for fatal in (KeyboardInterrupt, SystemExit):  # Ctrl-C / exit still stop the caller
            def interrupt(_item, fatal=fatal):
                raise fatal

            with mock.patch.object(tokens, "_accepted_name", interrupt), self.assertRaises(fatal):
                font_plan(ALL_FONTS)

    def test_non_fatal_base_exception_is_full_fallback(self):
        class Custom(BaseException):
            pass

        def evil_str(exc):
            def boom(*_a, **_k):
                raise exc

            return type("Evil", (str,), {"replace": boom, "split": boom, "__getitem__": boom})

        path_types = (PureWindowsPath, PurePosixPath, Path)
        for exc in (Custom, GeneratorExit, StopIteration):  # StopIteration must not end the loop silently
            for kind in path_types:  # exact path types pass the identity check; the str subclass parses lazily
                for spot in (0, len(ALL_FONTS) - 1):  # hostile item first and last
                    items = list(ALL_FONTS)
                    items[spot] = kind(evil_str(exc)("C:/x/" + Path(ALL_FONTS[spot]).name))
                    with self.subTest(exc=exc.__name__, kind=kind.__name__, spot=spot):
                        try:
                            plan = font_plan(items)
                        except Custom:  # GeneratorExit/Custom are BaseException: catch so a hit fails, not stops
                            self.fail("Custom escaped")
                        except GeneratorExit:
                            self.fail("GeneratorExit escaped")
                        self.assert_family(plan["sans"], "fallback", "bad_input")
                        self.assert_family(plan["mono"], "fallback", "bad_input")
            with mock.patch.object(tokens, "_accepted_name", evil_str(exc)("x").replace):
                plan = font_plan(ALL_FONTS)
            self.assert_family(plan["sans"], "fallback", "bad_input")
            self.assert_family(plan["mono"], "fallback", "bad_input")

    def test_str_subclass_name_never_counts(self):
        class Name(str):
            def __eq__(self, _o):
                raise GeneratorExit

            __hash__ = str.__hash__

        real = PureWindowsPath(FONT_DIR / SANS[0])
        with mock.patch.object(tokens, "PureWindowsPath", lambda _i: SimpleNamespace(parent=real.parent, name=Name(SANS[0]))):
            plan = font_plan(ALL_FONTS)  # every name is a subclass: none counts, none of its code runs later
        self.assert_family(plan["sans"], "fallback", "incomplete_family")
        self.assert_family(plan["mono"], "fallback", "incomplete_family")

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

    def test_numbers_only_outside_colour_and_family(self):
        self.assertEqual(non_numeric(as_qml_dict()), [])
        doctored = {"color_x": "#000000", "type_family_x": "Inter", "radius_a": True,
                    "space_b": "8", "size_c": None, "type_d_size": (1, 2), "ok": 4, "ok2": 1.5}
        self.assertEqual(non_numeric(doctored), ["radius_a", "size_c", "space_b", "type_d_size"])
        for group in ("radius", "space"):
            for v in TOKENS[group].values():
                self.assertIs(type(v), int, group)
                self.assertGreater(v, 0, group)

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
