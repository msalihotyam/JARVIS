"""Which models a fresh provider discovery retires, and when a saved choice follows.

Pure functions over what ONE live discovery returned; no persistence, no UI.
Nothing here names a model or orders versions: every successor is one the
provider itself stated in that same discovery.

CLAUDE. The CLI lists aliases (``opus[1m]``, ``sonnet``, ``haiku``) with the
concrete model each resolves to right now. A configured model stays pinned,
and remembers the alias (its LANE) it was chosen through. When a live
discovery shows that alias resolving to a different model of the same family
and the same context variant (``[1m]``), the pin follows it. No lane, no move.

CODEX. ``codex debug models`` states, per model, ``upgrade.model`` and
``upgrade.retirement_at``. A model is retired from normal selection when its
declared successor is offered in the same discovery (listed, safe, not
hidden) with the same specialty - a general model never becomes a
specialised one - or when its retirement time has passed. A selected model
moves only to such a declared successor.

Anything missing, malformed or unknown keeps a model visible and a choice
where it is.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_FAMILY = re.compile(r"\Aclaude-([a-z]+)-")
_VARIANT = re.compile(r"(\[[a-z0-9]+\])\Z")
_MAX_HOPS = 5


# -- Claude ------------------------------------------------------------------

def _rows(models: Any) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in models or [] if isinstance(m, dict) and isinstance(m.get("id"), str)}


def is_claude_alias(row_id: str) -> bool:
    """A CLI alias (a lane), not ``default`` and not a full model id."""
    return bool(row_id) and row_id != "default" and not row_id.startswith("claude-")


def claude_lane(models: Any, row_id: str, model: str) -> str:
    """The lane to record for ``model`` chosen through catalog row ``row_id``:
    the alias itself when that alias resolves to exactly ``model``, else ``""``."""
    row = _rows(models).get(row_id)
    return row_id if row and is_claude_alias(row_id) and model and row.get("resolved") == model else ""


def adopt_claude_lane(models: Any, model: str) -> str:
    """For a choice saved before lanes were recorded: the one alias that
    resolves to exactly ``model`` right now, or ``""`` when none or several do."""
    if not model:
        return ""
    matches = [rid for rid, row in _rows(models).items() if is_claude_alias(rid) and row.get("resolved") == model]
    return matches[0] if len(matches) == 1 else ""


def _same_lane(old: str, new: str) -> bool:
    """Same model family and same context variant; never a version comparison."""
    old_family, new_family = _FAMILY.match(old), _FAMILY.match(new)
    old_variant, new_variant = _VARIANT.search(old), _VARIANT.search(new)
    return (bool(old_family) and bool(new_family) and old_family.group(1) == new_family.group(1)
            and (old_variant.group(1) if old_variant else "") == (new_variant.group(1) if new_variant else ""))


def claude_migration(models: Any, lane: str, model: str, effort: str) -> tuple[str, str] | None:
    """``(new model, effort)`` when the recorded lane now resolves elsewhere
    within the same family and context variant; ``None`` to keep the choice."""
    row = _rows(models).get(lane)
    if not model or not row or not is_claude_alias(lane):
        return None
    target = row.get("resolved")
    if not isinstance(target, str) or not target or target == model or not _same_lane(model, target):
        return None
    efforts = row.get("efforts") if isinstance(row.get("efforts"), list) else []
    return target, effort if not effort or effort in efforts else ""


# -- Codex -------------------------------------------------------------------

def _passed(moment: Any, now: datetime) -> bool:
    if not isinstance(moment, str) or not moment:
        return False
    try:
        when = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        return False
    return when <= now


def codex_retirement(models: list[dict[str, Any]], upgrades: dict[str, dict[str, Any]],
                     now: datetime | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """``(models still offered, {retired id: successor id})``.

    ``models``: the safe, listed models of one discovery (each with ``id`` and
    ``specialty``). ``upgrades``: Codex's declarations for ANY model it
    returned, ``{id: {"model", "retirement_at", "specialty"}}``.
    """
    now = now or datetime.now(timezone.utc)
    offered = _rows(models)

    def specialty(model_id: str) -> str:
        source = offered.get(model_id) or upgrades.get(model_id) or {}
        return source.get("specialty") if isinstance(source.get("specialty"), str) else ""

    def expired(model_id: str) -> bool:
        return _passed((upgrades.get(model_id) or {}).get("retirement_at"), now)

    def successor(model_id: str, seen: frozenset[str]) -> str:
        declared = (upgrades.get(model_id) or {}).get("model")
        if not isinstance(declared, str) or declared in seen or len(seen) > _MAX_HOPS:
            return ""
        onward = successor(declared, seen | {declared})
        if onward:
            return onward
        return declared if declared in offered and not expired(declared) else ""

    retired = {}
    for model_id in set(upgrades) | set(offered):
        target = successor(model_id, frozenset({model_id}))
        if target and specialty(target) == specialty(model_id):
            retired[model_id] = target
    kept = [m for m in models if m.get("id") not in retired and not expired(m.get("id"))]
    return kept, retired


def codex_migration(selected: str, effort: str, offered: list[dict[str, Any]],
                    retired: dict[str, str]) -> tuple[str, str] | None:
    """``(new model, effort)`` when the selected model is retired to a
    successor that is offered; ``None`` keeps the stored choice as it is."""
    target = retired.get(selected) if selected else None
    row = _rows(offered).get(target) if target else None
    if row is None:
        return None
    efforts = row.get("efforts") if isinstance(row.get("efforts"), list) else []
    if effort in efforts:
        return target, effort
    default = row.get("default_effort")
    return target, default if default in efforts else ""
