"""FACETOKENS1: the locked design values of the new face as plain data, plus a font plan.

One source of truth: QML reads ``as_qml_dict()``; nothing here renders, reads
files or touches the network. Stdlib only. Bad input gets a typed ``Refusal``
or the safe fallback, never an exception.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PosixPath, PurePath, PurePosixPath, PureWindowsPath, WindowsPath
from types import MappingProxyType

_HEX = re.compile(r"#[0-9A-F]{6}")

TOKENS = MappingProxyType({
    "color": MappingProxyType({
        "base": "#0B0E13",
        "surface1": "#11161D",
        "surface2": "#171D26",
        "surface3": "#0E1218",
        "line1": "#1B222C",
        "line2": "#232B36",
        "text1": "#E7ECF2",
        "text2": "#AAB4C0",
        "text3": "#8793A1",
        "accent": "#5BC8F5",
        "accent_ink": "#06202C",
        "accent_surface": "#0F1C24",
        "accent_line": "#1D3A4A",
        "needs_you": "#F2B34A",
        "needs_you_ink": "#2A1C03",
        "error": "#F2777F",
        "ok": "#6FD3A3",
        "disabled_glyph": "#5E6978",
        "link": "#7FD4F7",
        "link_hover": "#B5E6FB",
        "text_soft": "#C5CED8",
    }),
    "type": MappingProxyType({
        # (pixel size, line height)
        "body": (17, 28),
        "h2": (22, 30),
        "h3": (17, 26),
        "code": (14.5, 22),
        "small": (13, 20),
        "label": (12.5, 16),
        "turn": (16, 26),
        "cell": (15, 24),
        "meta": (13, 20),
        "control": (14, 20),
        "num": (28, 34),
        "num_unit": (17, 26),
        "mono": (13, 20),
        "label_weight": 600,
        "label_caps": 1,  # Qt Font.AllUppercase
        "label_spacing_em": 0.04,
        "num_weight": 600,
        "num_unit_weight": 500,
        "mono_weight": 500,
        "family_sans": "Inter",
        "family_mono": "JetBrains Mono",
    }),
    "size": MappingProxyType({
        "ring": 72,
        "control_min": 44,
        "reading_col": (640, 660),  # (min, max)
        "reader_width": 880,
        "rail": 320,
        "pane": 600,
        "composer_radius": 16,
        "mini_box": 460,
        "pane_header": 56,
        "tab": 36,
        "bar": 8,
        "tick_w": 2,
        "tick_h": 16,
    }),
    "motion": MappingProxyType({
        "fast": 120,
        "medium": 200,
        "slow": 320,
        "curve": (0.2, 0.0, 0.0, 1.0),  # cubic-bezier x1, y1, x2, y2
        "breathe": 4000,
        "swell": 700,
        "arc": 1200,
        "arc2": 2400,
        "arc3": 3200,
        "ripple": 1600,
        "ask": 1600,
    }),
    # The ring, drawn in a ``box`` x ``box`` area: radii (r) and stroke widths (w).
    "ring": MappingProxyType({
        "box": 120,
        "outer_r": 54,
        "outer_w": 1.5,
        "dash_r": 46,
        "dash_w": 2,
        "dash_on": 2,
        "dash_off": 6,
        "main_r": 38,
        "main_w": 4,
        "glow_r": 20,
        "core_r": 12,
    }),
    "radius": MappingProxyType({
        "panel": 12,
        "composer": 16,
        "control": 12,
        "code": 8,
        "pill": 999,
    }),
    # The spacing scale, keyed by its own value: QML reads ``space_8``.
    "space": MappingProxyType({str(n): n for n in (4, 6, 8, 10, 12, 14, 16, 20, 24)}),
})

#: What each colour is for. Lines are borders (never under text); disabled_glyph
#: is a non-text glyph and is exempt from the 4.5 text contrast rule.
ROLES = MappingProxyType({
    "base": "surface", "surface1": "surface", "surface2": "surface", "surface3": "surface",
    "accent_surface": "surface",
    "line1": "line", "line2": "line", "accent_line": "line",
    "text1": "text", "text2": "text", "text3": "text", "accent": "text", "accent_ink": "text",
    "needs_you": "text", "needs_you_ink": "text", "error": "text", "ok": "text",
    "link": "text", "link_hover": "text", "text_soft": "text",
    "disabled_glyph": "non_text",
})

_SURFACES = ("base", "surface1", "surface2", "surface3", "accent_surface")
_ON_SURFACES = ("text1", "text2", "text3", "accent", "needs_you", "error", "ok",
                "link", "link_hover", "text_soft")
#: Every (foreground, background) colour-name pair the design puts text on.
PAIRS = tuple((fg, bg) for fg in _ON_SURFACES for bg in _SURFACES) + (("accent_ink", "accent"), ("needs_you_ink", "needs_you"))
MIN_TEXT_CONTRAST = 4.5


@dataclass(frozen=True)
class Refusal:
    reason: str


def is_hex(value: object) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _luminance(colour: str) -> float:
    channels = []
    for i in (1, 3, 5):
        c = int(colour[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(fg: object, bg: object) -> float | Refusal:
    """WCAG 2 contrast ratio of two ``#RRGGBB`` colours (1.0 .. 21.0)."""
    if not (is_hex(fg) and is_hex(bg)):
        return Refusal("bad_colour")
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def motion(reduced: object = False) -> dict:
    """Durations in ms and the easing curve; anything but ``reduced is False`` means zero motion."""
    still = reduced is not False  # fail-closed: an unclear value gets the reduced-motion answer
    return {k: v if k == "curve" else 0 if still else v for k, v in TOKENS["motion"].items()}


_TUPLE_KEYS = {
    "type": ("size", "line"),
    "size": ("min", "max"),
    "motion": ("x1", "y1", "x2", "y2"),
}


def as_qml_dict() -> dict:
    """A fresh flat ``{group_name[_part]: str | int | float}`` for QML (e.g. ``type_body_size``)."""
    out: dict = {}
    for group, values in TOKENS.items():
        for name, value in values.items():
            if isinstance(value, tuple):
                for part, item in zip(_TUPLE_KEYS[group], value):
                    out[f"{group}_{name}_{part}"] = item
            else:
                out[f"{group}_{name}"] = value
    return out


# -- font plan ---------------------------------------------------------------

#: Where bundled fonts live. Path arithmetic only: nothing is read, resolved or listed.
FONT_DIR = Path(__file__).parent / "fonts"
_FONT_DIR_W = PureWindowsPath(FONT_DIR)
_WEIGHTS = ("Regular", "Medium", "SemiBold")  # 400, 500, 600
FONT_FAMILIES = MappingProxyType({
    "sans": MappingProxyType({
        "family": TOKENS["type"]["family_sans"],
        "files": tuple(f"Inter-{w}.ttf" for w in _WEIGHTS),
        "fallback": ("Segoe UI Variable", "Segoe UI"),
    }),
    "mono": MappingProxyType({
        "family": TOKENS["type"]["family_mono"],
        "files": tuple(f"JetBrainsMono-{w}.ttf" for w in _WEIGHTS),
        "fallback": ("Cascadia Mono", "Consolas"),
    }),
})


def _is_exact(value: object, types: tuple) -> bool:
    return any(type(value) is t for t in types)  # `is`, not `in`: `in` falls back to ==


def _accepted_name(item: object) -> str | None:
    """The file name if ``item`` is a path directly in FONT_DIR, else None (exact match only)."""
    # Exact types by identity: a subclass, or a metaclass __eq__, can fake what PureWindowsPath reads.
    if not _is_exact(item, (str, PurePath, PureWindowsPath, PurePosixPath, Path, WindowsPath, PosixPath)):
        return None
    path = PureWindowsPath(item)
    if path.parent != _FONT_DIR_W:
        return None
    name = path.name
    return name if type(name) is str else None  # a str subclass's __eq__/__hash__ would run outside the try


def font_plan(available_files: object) -> dict:
    """Per family: the bundled files when all three weights are present, else the fallback chain.

    Pure: the caller lists what it found; this never reads, downloads or raises.
    A family is all bundled or all fallback, never partial.
    """
    names: set = set()
    # Exact type: a subclass can override __iter__ and raise.
    bad_input = not _is_exact(available_files, (list, tuple, set, frozenset))
    if not bad_input:
        try:
            # Called in the body, not via map(): a StopIteration from map() would end the loop silently.
            names = {_accepted_name(item) for item in available_files}
            names.discard(None)
        except (KeyboardInterrupt, SystemExit):  # Ctrl-C / exit still stop the caller
            raise
        except BaseException:  # noqa: BLE001 - any other hostile raise (GeneratorExit, custom) -> full fallback
            names, bad_input = set(), True
    plan = {}
    for key, spec in FONT_FAMILIES.items():
        if not bad_input and all(f in names for f in spec["files"]):
            plan[key] = {
                "chosen": "bundled",
                "families": (spec["family"],),
                "files": tuple(str(FONT_DIR / f) for f in spec["files"]),
                "reason": "",
            }
        else:
            plan[key] = {
                "chosen": "fallback",
                "families": spec["fallback"],
                "files": (),
                "reason": "bad_input" if bad_input else "incomplete_family",
            }
    return plan
