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

The left rail is the same idea without sizes: an ordered list of summary
cards and a visible flag each. Hiding a card, on either side, is layout only:
nothing here can disable Memory, stop a task or touch any backend data.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

PREFERENCES_VERSION = 4
THEMES = ("space", "dark", "command")
#: Execution-status lamp skins: the canonical glass orb and the HUD variant.
STATUS_INDICATORS = ("orb", "hud")
PROFILES = ("normal", "focus", "monitoring")
PAGES = ("home", "tasks", "automations", "memory", "history")
#: Pages earlier documents may name that no longer exist (Settings became a
#: drawer over the current page); they load as the Command Center.
_RETIRED_PAGES = {"settings": "home"}

#: The panels the canonical layout knows. Order is the canonical order.
PANEL_IDS = ("system", "workflow", "activity", "automations")
#: Panels that appear only when they have something to show, unless pinned.
CONTEXTUAL_PANELS = frozenset({"workflow"})
#: The left rail's summary cards, in canonical order.
RAIL_IDS = ("tasks", "updates", "focus", "plan", "memory")

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
        # The conversation-first 1920x1080 composition: a 386 px column
        # beside a 1231 px conversation, 20 px apart.
        "normal": {
            "column_width": 0.236,
            "order": list(PANEL_IDS),
            "panels": {
                "system": _panel(share=0.16),
                "workflow": _panel(pinned=True, share=0.38),
                "activity": _panel(share=0.46),
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


def canonical_rail() -> dict[str, Any]:
    return {"order": list(RAIL_IDS), "visible": {name: True for name in RAIL_IDS}}


def legacy_rail() -> dict[str, Any]:
    """The rail for a document saved before Memory existed: the four cards
    that person always had, Memory waiting in the Cards inventory."""
    rail = canonical_rail()
    rail["visible"]["memory"] = False
    return rail


def canonical_preferences() -> dict[str, Any]:
    return {
        "version": PREFERENCES_VERSION,
        "theme": "space",
        "ambient_effects": True,
        "status_indicator": "orb",
        "profile": "normal",
        "page": "home",
        "rail": canonical_rail(),
        "profiles": canonical_profiles(),
        "packages": {name: {"profile": "normal", "profiles": package_profiles(name)}
                     for name in THEMES if name != "space"},
    }


def package_profiles(name: str) -> dict[str, dict[str, Any]]:
    profiles = canonical_profiles()
    if name == "command":
        for layout in profiles.values():
            layout["column_width"] = 0.24
            layout["order"] = ["workflow", "system", "activity", "automations"]
        profiles["normal"]["panels"] = {
            "workflow": _panel(pinned=True, share=0.30),
            "system": _panel(share=0.28),
            "activity": _panel(share=0.24),
            "automations": _panel(share=0.18),
        }
    return profiles


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
    # Preserve only a complete, valid v1 document. Validation below still
    # rejects malformed old fields; migration never writes product config.
    if isinstance(candidate, dict) and candidate.get("version") == 1 and set(candidate) - {"status_indicator"} == set(defaults) - {"packages", "status_indicator", "rail"}:
        candidate = {**candidate, "version": 3, "packages": defaults["packages"]}
    # Version 2 -> 3: the conversation-first grid replaced every layout; the
    # person's other choices are kept.
    if isinstance(candidate, dict) and candidate.get("version") == 2 and set(candidate) == set(defaults) - {"rail"}:
        kept = {key: candidate[key] for key in ("theme", "ambient_effects", "status_indicator", "page")}
        candidate = {**defaults, **kept, "version": 3}
        candidate.pop("rail")
    # Version 3 -> 4: the rail became editable and gained Memory. Everything
    # saved is kept; Memory arrives hidden, restorable from Cards.
    if isinstance(candidate, dict) and candidate.get("version") == 3 and "rail" not in candidate:
        candidate = {**candidate, "version": PREFERENCES_VERSION, "rail": legacy_rail()}
    # A complete document from before the status lamp existed keeps every
    # choice and gains the default lamp.
    if isinstance(candidate, dict) and set(candidate) == set(defaults) - {"status_indicator"}:
        candidate = {**candidate, "status_indicator": defaults["status_indicator"]}
    if not isinstance(candidate, dict) or set(candidate) != set(defaults):
        return defaults
    if candidate["version"] != PREFERENCES_VERSION:
        return defaults
    if candidate["theme"] not in THEMES or type(candidate["ambient_effects"]) is not bool:
        return defaults
    if candidate["status_indicator"] not in STATUS_INDICATORS:
        return defaults
    if candidate["page"] in _RETIRED_PAGES:
        candidate = {**candidate, "page": _RETIRED_PAGES[candidate["page"]]}
    if candidate["profile"] not in PROFILES or candidate["page"] not in PAGES:
        return defaults
    rail = candidate["rail"]
    if (not isinstance(rail, dict) or set(rail) != {"order", "visible"}
            or not isinstance(rail["order"], list) or sorted(rail["order"]) != sorted(RAIL_IDS)
            or not isinstance(rail["visible"], dict) or set(rail["visible"]) != set(RAIL_IDS)
            or any(type(flag) is not bool for flag in rail["visible"].values())):
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
    packages = candidate["packages"]
    if not isinstance(packages, dict) or set(packages) != {"dark", "command"}:
        return defaults
    validated_packages = {}
    for name, package in packages.items():
        if not isinstance(package, dict) or set(package) != {"profile", "profiles"} or package["profile"] not in PROFILES:
            return defaults
        pack_profiles = package["profiles"]
        if not isinstance(pack_profiles, dict) or set(pack_profiles) != set(PROFILES):
            return defaults
        checked = {key: _valid_layout(value) for key, value in pack_profiles.items()}
        if any(value is None for value in checked.values()):
            return defaults
        validated_packages[name] = {"profile": package["profile"], "profiles": checked}
    return {
        "version": PREFERENCES_VERSION,
        "theme": candidate["theme"],
        "ambient_effects": candidate["ambient_effects"],
        "status_indicator": candidate["status_indicator"],
        "profile": candidate["profile"],
        "page": candidate["page"],
        "rail": {"order": list(rail["order"]), "visible": dict(rail["visible"])},
        "profiles": validated,
        "packages": validated_packages,
    }


def default_preferences_path() -> Path:
    return Path.home() / ".jarvis" / "ui-preferences.json"


class UiPreferences:
    """Load-validate-save of the one UI document. Never raises on a bad file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_preferences_path()
        self.data = self.load()
        # The last arrangement known to be good: what a failed save restores.
        self._last_good = deepcopy(self.data)

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
        self._last_good = deepcopy(self.data)
        return True

    def restore_last_layout(self) -> None:
        """Put back the layout (rail, stacks, profiles) of the last good
        document, after a save failed. Other choices are left as they are."""
        for key in ("rail", "profiles", "packages"):
            self.data[key] = deepcopy(self._last_good[key])

    # -- layout mutations: every one bounded, snapped and deterministic -------

    def layout(self, profile: str | None = None) -> dict[str, Any]:
        return self.package()["profiles"][profile or self.package()["profile"]]

    def package(self) -> dict[str, Any]:
        # MAIN and DARK share ONE macro layout: switching between them never
        # moves a panel. Only the Command preset keeps its own geometry.
        return self.data["packages"]["command"] if self.data["theme"] == "command" else self.data

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

    def set_rail_visible(self, card: str, visible: bool) -> None:
        if card in RAIL_IDS:
            self.data["rail"]["visible"][card] = bool(visible)

    def move_card(self, region: str, card: str, step: int) -> None:
        """Swap a card with its nearest VISIBLE neighbour in one direction,
        inside its own region. Hidden cards keep their stored places; at
        either end nothing moves."""
        if region == "rail":
            order, shown = self.data["rail"]["order"], self.data["rail"]["visible"]
        elif region == "stack":
            layout = self.layout()
            order, shown = layout["order"], {k: p["visible"] for k, p in layout["panels"].items()}
        else:
            return
        if card not in order or not shown[card] or step == 0:
            return
        index = order.index(card)
        others = range(index - 1, -1, -1) if step < 0 else range(index + 1, len(order))
        target = next((i for i in others if shown[order[i]]), None)
        if target is not None:
            order[index], order[target] = order[target], order[index]

    def reset_layout(self, profile: str | None = None) -> None:
        name = profile or self.package()["profile"]
        self.package()["profiles"][name] = package_profiles(self.data["theme"])[name]
        # The rail belongs to the MAIN / DARK workspace; the Command preset
        # has none, so its reset leaves the rail alone.
        if self.data["theme"] != "command":
            self.data["rail"] = canonical_rail()

    def reset_all_layouts(self) -> None:
        self.package()["profiles"] = package_profiles(self.data["theme"])
