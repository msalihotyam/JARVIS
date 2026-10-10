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

from jarvis.codex import CODEX_EFFORT_VALUES, CODEX_MODEL_PATTERN
from jarvis.settings import CLAUDE_MODEL_PATTERN

PREFERENCES_VERSION = 5
THEMES = ("space", "dark", "command")
#: Execution-status lamp skins: the canonical glass orb, the HUD variant and
#: the mechanical lamp (Gate 6G S1, the default; the others stay selectable).
STATUS_INDICATORS = ("orb", "hud", "mechanical")
#: Freeze control skins (Gate 6G S2): 1 classic key, 2 redesigned key,
#: 3 chrome toggle, 4 knife switch. Presentation only.
FREEZE_KEYS = (1, 2, 3, 4)
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
#: Interface languages, as stable locale ids (never the visible option name).
LANGUAGES = ("en-US", "tr-TR")
#: The chat / understanding providers a person can choose.
CHAT_PROVIDERS = ("claude", "codex")

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


def canonical_chat() -> dict[str, str]:
    # claude_lane: the CLI alias (e.g. "opus[1m]") the configured Claude model was
    # chosen through, recorded for exactly claude_lane_model - so the pin can
    # follow that alias to a newer model, and a model set elsewhere never inherits it.
    return {"provider": "claude", "codex_model": "", "codex_effort": "", "claude_lane": "", "claude_lane_model": ""}


def _model_id(value: Any, pattern: Any) -> bool:
    return type(value) is str and (value == "" or bool(pattern.match(value)))


def _valid_chat(candidate: Any) -> bool:
    return (isinstance(candidate, dict) and set(candidate) == set(canonical_chat())
            and candidate["provider"] in CHAT_PROVIDERS
            and _model_id(candidate["codex_model"], CODEX_MODEL_PATTERN)
            and candidate["codex_effort"] in ("",) + CODEX_EFFORT_VALUES
            and _model_id(candidate["claude_lane"], CLAUDE_MODEL_PATTERN)
            and _model_id(candidate["claude_lane_model"], CLAUDE_MODEL_PATTERN))


def canonical_rail() -> dict[str, Any]:
    """Every card in canonical order. Updates starts hidden: it lists the same
    activity notes the Live intelligence feed already shows, so on the default
    workspace it only repeated them; it stays in the Cards inventory."""
    return {"order": list(RAIL_IDS), "visible": {name: name != "updates" for name in RAIL_IDS}}


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
        "status_indicator": "mechanical",
        "freeze_key": 1,
        "profile": "normal",
        "page": "home",
        # An application preference, not layout: Reset layout never touches it.
        "language": "en-US",
        # Who answers General Chat and interprets requests, and each provider's
        # own model / effort. Inference routing only - never authority. Claude's
        # model and effort are operator configuration (claude_model / claude_effort).
        "chat": canonical_chat(),
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
    if isinstance(candidate, dict) and candidate.get("version") == 1 and set(candidate) - {"status_indicator", "language", "chat", "freeze_key"} == set(defaults) - {"packages", "status_indicator", "rail", "language", "chat", "freeze_key"}:
        candidate = {**candidate, "version": 3, "packages": defaults["packages"]}
    # Version 2 -> 3: the conversation-first grid replaced every layout; the
    # person's other choices are kept.
    if isinstance(candidate, dict) and candidate.get("version") == 2 and set(candidate) - {"language", "chat", "freeze_key"} == set(defaults) - {"rail", "language", "chat", "freeze_key"}:
        kept = {key: candidate[key] for key in ("theme", "ambient_effects", "status_indicator", "page")}
        candidate = {**defaults, **kept, "version": 3}
        candidate.pop("rail")
    # Version 3 -> 4: the rail became editable and gained Memory. Everything
    # saved is kept; Memory arrives hidden, restorable from Cards.
    if isinstance(candidate, dict) and candidate.get("version") == 3 and "rail" not in candidate:
        candidate = {**candidate, "version": 4, "rail": legacy_rail()}
    # Version 4 -> 5: Updates leaves the default rail (it duplicated the Live
    # intelligence feed). A saved layout keeps everything else the person chose.
    if isinstance(candidate, dict) and candidate.get("version") == 4 and isinstance(candidate.get("rail"), dict)             and isinstance(candidate["rail"].get("visible"), dict):
        candidate = {**candidate, "version": PREFERENCES_VERSION,
                     "rail": {**candidate["rail"], "visible": {**candidate["rail"]["visible"], "updates": False}}}
    # A document from before the language setting keeps every choice and gains
    # English: an existing installation never switches language on its own.
    if isinstance(candidate, dict) and "version" in candidate and "language" not in candidate:
        candidate = {**candidate, "language": defaults["language"]}
    # A document from before provider choice keeps everything and answers with Claude.
    if isinstance(candidate, dict) and "version" in candidate and "chat" not in candidate:
        candidate = {**candidate, "chat": canonical_chat()}
    # A choice saved before Claude lanes keeps everything; no lane is known yet.
    if (isinstance(candidate, dict) and isinstance(candidate.get("chat"), dict)
            and not {"claude_lane", "claude_lane_model"} & set(candidate["chat"])):
        candidate = {**candidate, "chat": {**candidate["chat"], "claude_lane": "", "claude_lane_model": ""}}
    # A document from before freeze-key skins keeps every choice and the classic key.
    if isinstance(candidate, dict) and "version" in candidate and "freeze_key" not in candidate:
        candidate = {**candidate, "freeze_key": defaults["freeze_key"]}
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
    if type(candidate["freeze_key"]) is not int or candidate["freeze_key"] not in FREEZE_KEYS:
        return defaults
    if candidate["page"] in _RETIRED_PAGES:
        candidate = {**candidate, "page": _RETIRED_PAGES[candidate["page"]]}
    if candidate["profile"] not in PROFILES or candidate["page"] not in PAGES:
        return defaults
    if candidate["language"] not in LANGUAGES or not _valid_chat(candidate["chat"]):
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
        "freeze_key": candidate["freeze_key"],
        "profile": candidate["profile"],
        "page": candidate["page"],
        "language": candidate["language"],
        "chat": dict(candidate["chat"]),
        "rail": {"order": list(rail["order"]), "visible": dict(rail["visible"])},
        "profiles": validated,
        "packages": validated_packages,
    }


#: Gate 6F F6. At most this many pinned conversations.
PIN_LIMIT = 10
SIDEBAR_VERSION = 1
#: Genel's key among the per-scope groups (project ids start with "p-").
GLOBAL_SCOPE = "global"
#: The date groups' default: Bugün and Dün open, Daha eski collapsed.
DEFAULT_GROUPS = {"today": True, "yesterday": True, "earlier": False}


def canonical_sidebar() -> dict[str, Any]:
    return {"version": SIDEBAR_VERSION, "pins": [], "pins_open": True, "groups": {}}


def validate_sidebar(candidate: Any) -> dict[str, Any]:
    """The sidebar document, or the canonical one when ANY part is malformed."""
    defaults = canonical_sidebar()
    if not isinstance(candidate, dict) or set(candidate) != set(defaults) or candidate["version"] != SIDEBAR_VERSION:
        return defaults
    pins, groups = candidate["pins"], candidate["groups"]
    if (not isinstance(pins, list) or len(pins) > PIN_LIMIT or len(set(map(str, pins))) != len(pins)
            or any(type(pin) is not str or not pin for pin in pins) or type(candidate["pins_open"]) is not bool):
        return defaults
    if not isinstance(groups, dict) or any(
            type(scope) is not str or not isinstance(state, dict) or set(state) != set(DEFAULT_GROUPS)
            or any(type(flag) is not bool for flag in state.values()) for scope, state in groups.items()):
        return defaults
    return {"version": SIDEBAR_VERSION, "pins": list(pins), "pins_open": candidate["pins_open"],
            "groups": {scope: dict(state) for scope, state in groups.items()}}


TREE_VERSION = 1


def canonical_tree() -> dict[str, Any]:
    """Gate 6G Sidebar v2: PROJELER open (L2), the plain conversations shown
    (P1), the projects whose conversation list is collapsed (T1; default all
    open) and the pinned projects, newest pin first (T2 "Sabitle")."""
    return {"version": TREE_VERSION, "projects_open": True, "plain_open": True, "closed": [], "pinned_projects": []}


def validate_tree(candidate: Any) -> dict[str, Any]:
    """The tree document, or the canonical one when ANY part is malformed."""
    defaults = canonical_tree()
    if not isinstance(candidate, dict) or set(candidate) != set(defaults) or candidate["version"] != TREE_VERSION:
        return defaults
    if type(candidate["projects_open"]) is not bool or type(candidate["plain_open"]) is not bool:
        return defaults
    for key in ("closed", "pinned_projects"):
        ids = candidate[key]
        if not isinstance(ids, list) or len(set(map(str, ids))) != len(ids) or any(type(i) is not str or not i for i in ids):
            return defaults
    return {"version": TREE_VERSION, "projects_open": candidate["projects_open"], "plain_open": candidate["plain_open"],
            "closed": list(candidate["closed"]), "pinned_projects": list(candidate["pinned_projects"])}


SIDEBAR_LAYOUT_VERSION = 1
HIDE_MODES = ("", "grow", "keep")
# E1': px at the 1920 layout; the default is the widest, a drag only narrows.
LEFT_WIDTH, RIGHT_WIDTH = (240, 340), (300, 386)


def canonical_sidebar_layout() -> dict[str, Any]:
    """Gate 6G Sidebar v2 slice 2: the hide mode (V5: "" docked, "grow" the chat
    widens, "keep" the space stays), tiles mode (V6) and the column widths (E1')."""
    return {"version": SIDEBAR_LAYOUT_VERSION, "hide_mode": "", "tiles": False,
            "left": LEFT_WIDTH[1], "right": RIGHT_WIDTH[1]}


def validate_sidebar_layout(candidate: Any) -> dict[str, Any]:
    """The layout document, or the canonical one when ANY part is malformed."""
    defaults = canonical_sidebar_layout()
    if not isinstance(candidate, dict) or set(candidate) != set(defaults) or candidate["version"] != SIDEBAR_LAYOUT_VERSION:
        return defaults
    if candidate["hide_mode"] not in HIDE_MODES or type(candidate["tiles"]) is not bool:
        return defaults
    for key, (low, high) in (("left", LEFT_WIDTH), ("right", RIGHT_WIDTH)):
        if type(candidate[key]) is not int or not low <= candidate[key] <= high:
            return defaults
    return dict(candidate)


ORDER_VERSION = 1


def canonical_order() -> dict[str, Any]:
    """Gate 6G Sidebar v2 DRAG: the sidebar's manual project order (ids, top first)."""
    return {"version": ORDER_VERSION, "projects": []}


def validate_order(candidate: Any) -> dict[str, Any]:
    """The order document, or the canonical one when ANY part is malformed."""
    defaults = canonical_order()
    if not isinstance(candidate, dict) or set(candidate) != set(defaults) or candidate["version"] != ORDER_VERSION:
        return defaults
    ids = candidate["projects"]
    if not isinstance(ids, list) or len(set(map(str, ids))) != len(ids) or any(type(i) is not str or not i for i in ids):
        return defaults
    return {"version": ORDER_VERSION, "projects": list(ids)}


def ordered_projects(records: list[dict[str, Any]], order: list[str]) -> list[str]:
    """Every record's id in the sidebar's order: a project the order does not
    know yet (new) on top, newest first; then the order's known ids."""
    known = {record["id"] for record in records}
    fresh = sorted((r for r in records if r["id"] not in order),
                   key=lambda r: (str(r.get("created_at") or ""), r["id"]), reverse=True)
    return [r["id"] for r in fresh] + [i for i in order if i in known]


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

    # -- Gate 6F F6: the sidebar's pins and collapsed groups -------------------
    #
    # Kept in a SIBLING file (``<prefs>.sidebar.json``), never in the main
    # document: validate_preferences is all-or-nothing on its key set, so an
    # added key would make every earlier release (8fbbaef included) read the
    # whole document as defaults - theme, language, layout lost on a rollback.
    # A release that does not know this file never opens it. Local to this PC,
    # never a conversation's data: pinning changes no conversation.

    @property
    def sidebar_path(self) -> Path:
        return self.path.with_suffix(".sidebar.json")

    @property
    def sidebar(self) -> dict[str, Any]:
        if not hasattr(self, "_sidebar"):
            self._sidebar = self._load_sidebar()
        return self._sidebar

    def _load_sidebar(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.sidebar_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return canonical_sidebar()
        return validate_sidebar(raw)

    def save_sidebar(self) -> bool:
        self._sidebar = validate_sidebar(self.sidebar)
        try:
            self.sidebar_path.parent.mkdir(parents=True, exist_ok=True)
            self.sidebar_path.write_text(json.dumps(self._sidebar, indent=2), encoding="utf-8")
        except OSError:
            return False
        return True

    def prune_sidebar(self, conversation_ids: set[str], scopes: set[str]) -> None:
        """Forget pins of conversations no longer listed (archived, deleted,
        unknown) and the groups of scopes no longer known. In memory: the
        next sidebar write persists it."""
        self.sidebar["pins"] = [pin for pin in self.sidebar["pins"] if pin in conversation_ids]
        self.sidebar["groups"] = {key: value for key, value in self.sidebar["groups"].items() if key in scopes | {GLOBAL_SCOPE}}

    def pin(self, conversation_id: str) -> bool:
        """A new pin goes to the top; refused at the limit."""
        pins = self.sidebar["pins"]
        if not conversation_id or conversation_id in pins or len(pins) >= PIN_LIMIT:
            return False
        pins.insert(0, conversation_id)
        return True

    def unpin(self, conversation_id: str) -> bool:
        if conversation_id not in self.sidebar["pins"]:
            return False
        self.sidebar["pins"].remove(conversation_id)
        return True

    def move_pin(self, conversation_id: str, index: int) -> bool:
        """Put a pin at ``index`` (clamped to the list)."""
        pins = self.sidebar["pins"]
        if conversation_id not in pins:
            return False
        at = pins.index(conversation_id)
        target = int(clamp(int(index), 0, len(pins) - 1))
        if target == at:
            return False
        pins.insert(target, pins.pop(at))
        return True

    def groups_open(self, scope: str) -> dict[str, bool]:
        return {**DEFAULT_GROUPS, **self.sidebar["groups"].get(scope or GLOBAL_SCOPE, {})}

    def set_group_open(self, scope: str, key: str, open_: bool) -> bool:
        if key == "pinned":
            changed = self.sidebar["pins_open"] != bool(open_)
            self.sidebar["pins_open"] = bool(open_)
            return changed
        if key not in DEFAULT_GROUPS:
            return False
        groups = self.groups_open(scope)
        changed = groups[key] != bool(open_)
        groups[key] = bool(open_)
        self.sidebar["groups"][scope or GLOBAL_SCOPE] = groups
        return changed

    # -- Gate 6G Sidebar v2: the project tree's and the plain list's states -----
    #
    # A second sibling file (``<prefs>.sidebar-tree.json``) for the same reason
    # as the F6 one: validate_sidebar is all-or-nothing on its key set, so an
    # added key would read every F6 document (its pins) as the defaults.

    @property
    def tree_path(self) -> Path:
        return self.path.with_suffix(".sidebar-tree.json")

    @property
    def tree(self) -> dict[str, Any]:
        if not hasattr(self, "_tree"):
            try:
                self._tree = validate_tree(json.loads(self.tree_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, UnicodeDecodeError):
                self._tree = canonical_tree()
        return self._tree

    def save_tree(self) -> bool:
        self._tree = validate_tree(self.tree)
        try:
            self.tree_path.parent.mkdir(parents=True, exist_ok=True)
            self.tree_path.write_text(json.dumps(self._tree, indent=2), encoding="utf-8")
        except OSError:
            return False
        return True

    def set_tree_flag(self, key: str, value: bool) -> bool:
        """``projects_open`` (L2) or ``plain_open`` (P1)."""
        if key not in ("projects_open", "plain_open") or self.tree[key] == bool(value):
            return False
        self.tree[key] = bool(value)
        return True

    def set_tree_member(self, key: str, project_id: str, member: bool) -> bool:
        """Add (on top) or remove a project id in ``closed`` or ``pinned_projects``."""
        ids = self.tree.get(key) if key in ("closed", "pinned_projects") else None
        if ids is None or not project_id or (project_id in ids) == bool(member):
            return False
        if member:
            ids.insert(0, project_id)
        else:
            ids.remove(project_id)
        return True

    # -- Gate 6G Sidebar v2 slice 2: hide mode, tiles mode, column widths -------
    #
    # A third sibling file (``<prefs>.sidebar-layout.json``): ``column_width``
    # and its 0.22 floor stay as they are, so a schema-10 release still reads
    # the main document whole (rollback). A bad file reads as the defaults and
    # never touches the main document.

    @property
    def sidebar_layout_path(self) -> Path:
        return self.path.with_suffix(".sidebar-layout.json")

    @property
    def sidebar_layout(self) -> dict[str, Any]:
        if not hasattr(self, "_sidebar_layout"):
            try:
                raw = json.loads(self.sidebar_layout_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                raw = None
            self._sidebar_layout = validate_sidebar_layout(raw)
        return self._sidebar_layout

    def set_sidebar_layout(self, changes: dict[str, Any]) -> bool:
        """Apply ``changes`` when the result is valid and different; then save."""
        candidate = {**self.sidebar_layout, **changes}
        if candidate == self.sidebar_layout or validate_sidebar_layout(candidate) != candidate:
            return False
        self._sidebar_layout = candidate
        try:
            self.sidebar_layout_path.parent.mkdir(parents=True, exist_ok=True)
            self.sidebar_layout_path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        except OSError:
            pass
        return True

    # -- Gate 6G Sidebar v2 slice 3: the projects' manual order (DRAG) ---------
    #
    # A fourth sibling file (``<prefs>.sidebar-order.json``), like the pins: the
    # main document and the tree file keep their exact key sets, so a schema-10
    # release still reads both whole (rollback) and never opens this one.

    @property
    def order_path(self) -> Path:
        return self.path.with_suffix(".sidebar-order.json")

    @property
    def project_order(self) -> list[str]:
        if not hasattr(self, "_order"):
            try:
                raw = json.loads(self.order_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                raw = None
            self._order = validate_order(raw)
        return list(self._order["projects"])

    def set_project_order(self, ids: list[str]) -> bool:
        """Keep ``ids`` (top first) when valid and different; then save."""
        candidate = {"version": ORDER_VERSION, "projects": list(ids)}
        if candidate["projects"] == self.project_order or validate_order(candidate) != candidate:
            return False
        self._order = candidate
        try:
            self.order_path.parent.mkdir(parents=True, exist_ok=True)
            self.order_path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        except OSError:
            pass
        return True

    # -- G6W3: the project chat's work panel, collapsed or not -------------------
    #
    # A fifth sibling file (``<prefs>.work-panel.json``) for the same rollback
    # reason; a bad file reads as open (the default).

    @property
    def work_panel_path(self) -> Path:
        return self.path.with_suffix(".work-panel.json")

    @property
    def work_panel_collapsed(self) -> bool:
        if not hasattr(self, "_work_panel"):
            try:
                raw = json.loads(self.work_panel_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                raw = None
            valid = isinstance(raw, dict) and set(raw) == {"version", "collapsed"} and raw["version"] == 1 \
                and type(raw["collapsed"]) is bool
            self._work_panel = raw["collapsed"] if valid else False
        return self._work_panel

    def set_work_panel_collapsed(self, collapsed: bool) -> bool:
        """Keep the state when different; then save."""
        if self.work_panel_collapsed == bool(collapsed):
            return False
        self._work_panel = bool(collapsed)
        try:
            self.work_panel_path.parent.mkdir(parents=True, exist_ok=True)
            self.work_panel_path.write_text(json.dumps({"version": 1, "collapsed": self._work_panel}), encoding="utf-8")
        except OSError:
            pass
        return True
