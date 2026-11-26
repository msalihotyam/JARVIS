"""Approval source rule (TALKCARDSRC1, Salih 2026-11-25, ADR Q2).

Where may a card tap come from: plan-class cards from the PC or the phone
(telegram), security-class cards only from the PC. Checked before a tap
reaches the card logic (the card module's resolve), so a refused tap leaves
the card PENDING and it can still be approved on the PC.

The card class is read from the card record made at creation, the view from
the tap log entry's "source" (set by code); a tap can carry neither a class
nor an override. Matching is exact: any other spelling (case, spaces,
look-alikes) or a missing class is security class. Pure: no I/O, nothing in
the running app imports this module; it grants nothing by itself.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

ALLOW = "ALLOW"
REFUSE = "REFUSE"
PLAN = "plan"
SECURITY = "security"
PC = "pc"
TELEGRAM = "telegram"
PC_ONLY_NOTE = "Bu onay yalnız bilgisayardan verilir."

# The one source of both class lists; never read from config or the brain.
CARD_CLASSES = MappingProxyType({
    PLAN: frozenset({"scope", "priority", "plan", "lane_count", "pause_resume",
                     "design_choice"}),
    SECURITY: frozenset({"protected_file_key", "permission_profile", "allowlist",
                         "destination_list", "irreversible_deletion", "reset_right"}),
})
_VIEWS = {PLAN: (PC, TELEGRAM), SECURITY: (PC,)}
# Keys a tap entry may not carry: the class lives on the card, overrides do not exist.
_TAP_FORBIDDEN = ("card_class", "class", "classes", "override", "allowed_views")


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str = ""
    note: str = ""

    @property
    def allowed(self):
        return self.verdict == ALLOW


def _refuse(reason, note=""):
    return Decision(REFUSE, reason, note)


def class_of(card_class):
    """PLAN only for an exact plan-list name; everything else (unknown, None) is SECURITY."""
    if type(card_class) is str and card_class in CARD_CLASSES[PLAN]:
        return PLAN
    return SECURITY


def allow(card_class, view, *extra, **overrides):
    """ALLOW or REFUSE (with reason) a tap of a card of card_class from view."""
    try:
        if extra or overrides:
            return _refuse("extra arguments refused")
        if card_class is not None and type(card_class) is not str:
            return _refuse("invalid card class")
        if type(view) is not str or view not in (PC, TELEGRAM):
            return _refuse("invalid view")
        kind = class_of(card_class)
        if view in _VIEWS[kind]:
            return Decision(ALLOW)
        return _refuse("security card needs the pc", PC_ONLY_NOTE)
    except Exception:  # noqa: BLE001 - fail closed, RecursionError included
        return _refuse("internal error")


def check_tap(card_record, tap_entry, *extra, **overrides):
    """allow() for one tap log entry against the card's creation record.

    Class from card_record["card_class"] (missing -> security), view from
    tap_entry["source"]; a tap "view" that disagrees with the source, a
    different card id, or a class/override key on the tap is REFUSED.
    """
    try:
        if extra or overrides:
            return _refuse("extra arguments refused")
        if not isinstance(card_record, Mapping) or not isinstance(tap_entry, Mapping):
            return _refuse("not a record")
        card, tap = dict(card_record), dict(tap_entry)
        card_id = card.get("card_id")
        if type(card_id) is not str or type(tap.get("card_id")) is not str \
                or tap["card_id"] != card_id:
            return _refuse("card mismatch")
        if any(key in tap for key in _TAP_FORBIDDEN):
            return _refuse("tap cannot carry a class or override")
        source = tap.get("source")
        if "view" in tap and tap["view"] != source:
            return _refuse("view does not match source")
        return allow(card.get("card_class"), source)
    except Exception:  # noqa: BLE001 - fail closed, RecursionError included
        return _refuse("internal error")
