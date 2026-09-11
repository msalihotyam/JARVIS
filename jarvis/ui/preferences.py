"""UI-only preferences: theme, ambient effects, layout. Never authority.

Persisted beside the operator's config as ``~/.jarvis/ui-preferences.json``
- a separate file on purpose, so nothing here can ever be read as
``execution_enabled``, a permission mode or a grant. Bounded, versioned,
validated on load; anything malformed falls back to the canonical defaults
rather than being repaired field by field into a layout nobody chose.

The layout model is deliberately simple and deterministic: the right column
is an ORDERED STACK of panels, each with a height share, a visible flag and
a pin flag, plus one column-width fraction. A stack cannot overlap, cannot
leave the screen and reflows by itself when a panel is hidden.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PREFERENCES_VERSION = 1
THEMES = ("space", "dark")
PROFILES = ("normal", "focus", "monitoring")
PAGES = ("home", "tasks", "automations", "memory", "history")
#: Pages earlier documents may name that no longer exist (Settings became a
#: drawer over the current page); they load as the Command Center.
_RETIRED_PAGES = {"settings": "home"}

#: The panels the canonical layout knows. Order is the canonical order.
PANEL_IDS = ("system", "workflow", "activity", "automations")
#: Panels that appear only when they have something to show, unless pinned.
CONTEXTUAL_PANELS = frozenset({"workflow"})

#: Bounds, in fractions of the content area. The stack's height shares are
#: normalized on render, so a share is a proportion, not a pixel count.
MIN_COLUMN_WIDTH, MAX_COLUMN_WIDTH = 0.22, 0.45
MIN_SHARE, MAX_SHARE = 0.12, 0.70
SHARE_STEP = 0.02


def _panel(visible: bool = True, pinned: bool = False, share: float = 0.33) -> dict[str, Any]:
    return {"visible": visible, "pinned": pinned, "share": share}


def canonical_profiles() -> dict[str, dict[str, Any]]:
    """Fresh canonical layouts. A new dict every call: never a shared default."""
    return {
        "normal": {
            "column_width": 0.31,
            "order": list(PANEL_IDS),
            "panels": {
                "system": _panel(share=0.30),
                "workflow": _panel(share=0.36),
                "activity": _panel(share=0.34),
                "automations": _panel(visible=False, share=0.20),
            },
        },
        "focus": {
            "column_width": 0.24,
            "order": list(PANEL_IDS),
            "panels": {
                "system": _panel(visible=False, share=0.30),
                "workflow": _panel(pinned=True, share=0.45),
                "activity": _panel(visible=False, share=0.30),
                "automations": _panel(visible=False, share=0.25),
            },
        },
        "monitoring": {
            "column_width": 0.42,
            "order": list(PANEL_IDS),
            "panels": {
                "system": _panel(share=0.24),
                "workflow": _panel(pinned=True, share=0.26),
                "activity": _panel(share=0.26),
                "automations": _panel(share=0.24),
            },
        },
    }


def canonical_preferences() -> dict[str, Any]:
    return {
        "version": PREFERENCES_VERSION,
        "theme": "space",
        "ambient_effects": True,
        "profile": "normal",
        "page": "home",
        "profiles": canonical_profiles(),
    }


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def snap(value: float, step: float = SHARE_STEP) -> float:
    return round(round(value / step) * step, 4)


def _valid_layout(candidate: Any) -> dict[str, Any] | None:
    """One profile layout, or ``None``. Exact keys, exact types, in bounds."""
    if not isinstance(candidate, dict) or set(candidate) != {"column_width", "order", "panels"}:
        return None
    width = candidate["column_width"]
    if type(width) not in (int, float) or not MIN_COLUMN_WIDTH <= width <= MAX_COLUMN_WIDTH:
        return None
    order = candidate["order"]
    if not isinstance(order, list) or sorted(order) != sorted(PANEL_IDS):
        return None
    panels = candidate["panels"]
    if not isinstance(panels, dict) or set(panels) != set(PANEL_IDS):
        return None
    for panel in panels.values():
        if not isinstance(panel, dict) or set(panel) != {"visible", "pinned", "share"}:
            return None
        if type(panel["visible"]) is not bool or type(panel["pinned"]) is not bool:
            return None
        share = panel["share"]
        if type(share) not in (int, float) or not MIN_SHARE <= share <= MAX_SHARE:
            return None
    return {
        "column_width": float(width),
        "order": list(order),
        "panels": {name: dict(panel) for name, panel in panels.items()},
    }


def validate_preferences(candidate: Any) -> dict[str, Any]:
    """Canonical defaults unless the WHOLE document is acceptable.

    All-or-nothing on purpose: a document that is half right describes a
    state nobody chose, and defaulting the broken half silently would leave
    the user with a layout they never saw.
    """
    defaults = canonical_preferences()
    if not isinstance(candidate, dict) or set(candidate) != set(defaults):
        return defaults
    if candidate["version"] != PREFERENCES_VERSION:
        return defaults
    if candidate["theme"] not in THEMES or type(candidate["ambient_effects"]) is not bool:
        return defaults
    if candidate["page"] in _RETIRED_PAGES:
        candidate = {**candidate, "page": _RETIRED_PAGES[candidate["page"]]}
    if candidate["profile"] not in PROFILES or candidate["page"] not in PAGES:
        return defaults
    profiles = candidate["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILES):
        return defaults
    validated = {}
    for name, layout in profiles.items():
        valid = _valid_layout(layout)
        if valid is None:
            return defaults
        validated[name] = valid
    return {
        "version": PREFERENCES_VERSION,
        "theme": candidate["theme"],
        "ambient_effects": candidate["ambient_effects"],
        "profile": candidate["profile"],
        "page": candidate["page"],
        "profiles": validated,
    }


def default_preferences_path() -> Path:
    return Path.home() / ".jarvis" / "ui-preferences.json"


class UiPreferences:
    """Load-validate-save of the one UI document. Never raises on a bad file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_preferences_path()
        self.data = self.load()

    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return canonical_preferences()
        return validate_preferences(raw)

    def save(self) -> bool:
        """Persist the CURRENT validated document; ``False`` when it cannot."""
        self.data = validate_preferences(self.data)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            return False
        return True

    # -- layout mutations: every one bounded, snapped and deterministic -------

    def layout(self, profile: str | None = None) -> dict[str, Any]:
        return self.data["profiles"][profile or self.data["profile"]]

    def set_visible(self, panel: str, visible: bool) -> None:
        if panel in PANEL_IDS:
            self.layout()["panels"][panel]["visible"] = bool(visible)

    def set_pinned(self, panel: str, pinned: bool) -> None:
        if panel in PANEL_IDS:
            self.layout()["panels"][panel]["pinned"] = bool(pinned)

    def move(self, panel: str, delta: int) -> None:
        """Reorder within the stack by whole positions; never off either end."""
        order = self.layout()["order"]
        if panel not in order or delta == 0:
            return
        index = order.index(panel)
        target = int(clamp(index + delta, 0, len(order) - 1))
        order.insert(target, order.pop(index))

    def set_share(self, panel: str, share: float) -> None:
        if panel in PANEL_IDS:
            self.layout()["panels"][panel]["share"] = clamp(snap(float(share)), MIN_SHARE, MAX_SHARE)

    def set_column_width(self, width: float) -> None:
        self.layout()["column_width"] = clamp(snap(float(width)), MIN_COLUMN_WIDTH, MAX_COLUMN_WIDTH)

    def reset_layout(self, profile: str | None = None) -> None:
        name = profile or self.data["profile"]
        self.data["profiles"][name] = canonical_profiles()[name]

    def reset_all_layouts(self) -> None:
        self.data["profiles"] = canonical_profiles()
