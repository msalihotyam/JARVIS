"""Decision table: which path one logged inbound entry takes in the service (TALKROUTE1).

Pure and deterministic: no I/O, no network, no model calls; nothing in the running app
imports it yet. route() returns a Route for every input; a bad input is a typed refusal
(path REFUSED, ASCII reason) and no exception escapes. Nothing is ever run as a command:
text starting with "/" is plain text for a turn, except the exact greeting "/start".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TURN = "TURN"
SPEECH_THEN_TURN = "SPEECH_THEN_TURN"
CARD = "CARD"
MODE = "MODE"
HOLD = "HOLD"  # wait Route.seconds for the text that may follow, then TURN
NOTE = "NOTE"
REFUSED = "REFUSED"
PATHS = frozenset({TURN, SPEECH_THEN_TURN, CARD, MODE, HOLD, NOTE, REFUSED})

KINDS = ("text", "voice", "image", "file", "approval_tap", "edit", "unsupported")
WHO = "salih"
DEFAULT_HOLD_SECONDS = 5
MAX_HOLD_SECONDS = 600

# Exact reply-mode labels (the reply keyboard's button texts) -> mode. Matched raw: no strip/case/NFKC.
MODE_LABELS = {"Sesli yanıt": "voice", "Yazılı yanıt": "text"}
GREETING = "/start"

# Fixed Turkish lines for NOTE results; kept apart from the ASCII REASONS.
NOTES = {
    "greeting": "Merhaba, ben JARVIS. Yazabilir ya da sesli mesaj gönderebilirsin.",
    "edit": "Düzenlenen mesajları işleyemiyorum; lütfen yeni bir mesaj gönder.",
    "unsupported": "Bu mesaj türünü henüz işleyemiyorum.",
}

REASONS = {
    # routed
    "text": "plain text goes to a turn",
    "slash_text": "text starting with a slash is plain text, never a command",
    "forwarded": "forwarded content is plain text for a turn",
    "voice": "voice is transcribed, then goes to a turn",
    "approval_tap": "an approval tap goes to its card only",
    "mode_label": "exact reply-mode label sets the reply mode",
    "attachment_text": "attachment with text goes to a turn",
    "attachment_hold": "attachment alone waits for following text",
    "hold_off": "hold is zero or negative: attachment goes to a turn at once",
    "greeting": "start greeting",
    "edit": "edited messages are not processed",
    "unsupported": "unsupported message kind",
    # refused
    "bad_entry": "entry must be a plain dict with string keys",
    "bad_who": "entry author must be salih",
    "bad_kind": "entry kind is missing or not in the fixed list",
    "bad_text": "entry text must be a string",
    "bad_attachments": "entry attachments must be a list",
    "empty": "entry has neither text nor attachment",
    "bad_hold": "hold seconds must be a finite number no larger than the cap",
    "internal": "routing failed",
}


@dataclass(frozen=True)
class Route:
    path: str
    reason: str
    seconds: float | None = None  # HOLD only
    mode: str | None = None  # MODE only
    note: str | None = None  # NOTE only: fixed Turkish line

    @property
    def message(self) -> str:
        return f"{self.path}: {self.reason}: {REASONS[self.reason]}"


def route(entry: object, *, hold_seconds: object = DEFAULT_HOLD_SECONDS) -> Route:
    """Route one logged inbound entry; never raises."""
    try:
        return _route(entry, hold_seconds)
    except Exception:  # RecursionError included: fail closed
        return Route(REFUSED, "internal")


def _route(entry: object, hold_seconds: object) -> Route:
    if not _hold_ok(hold_seconds):
        return Route(REFUSED, "bad_hold")
    # Identity, not isinstance: a subclass or Mapping can override get/__contains__ and a spoofed
    # __class__ fools isinstance. Exact str keys: a str-subclass key with its own hash/eq could hide
    # or fake "forwarded". With both, iteration and get below run no caller code.
    if type(entry) is not dict or any(type(k) is not str for k in entry):
        return Route(REFUSED, "bad_entry")
    who, kind, text, attachments = (entry.get(k) for k in ("who", "kind", "text", "attachments"))
    is_forwarded = entry.get("forwarded", False) is not False  # absent or explicit False only
    if type(who) is not str or who != WHO:
        return Route(REFUSED, "bad_who")
    if type(kind) is not str or kind not in KINDS:
        return Route(REFUSED, "bad_kind")
    if text is None:
        text = ""
    if type(text) is not str:
        return Route(REFUSED, "bad_text")
    if attachments is None:
        attachments = ()
    if type(attachments) is not list and type(attachments) is not tuple:  # identity: `in` falls back to ==
        return Route(REFUSED, "bad_attachments")

    if kind == "approval_tap":  # before any text rule: a tap never becomes a turn
        return Route(CARD, "approval_tap")
    if kind in ("edit", "unsupported"):
        return Route(NOTE, kind, note=NOTES[kind])
    if kind == "voice":
        return Route(SPEECH_THEN_TURN, "voice")
    blank = not text.strip()
    if attachments:
        if not blank:
            return Route(TURN, "attachment_text")
        if hold_seconds <= 0:
            return Route(TURN, "hold_off")
        return Route(HOLD, "attachment_hold", seconds=hold_seconds)
    if blank:
        return Route(REFUSED, "empty")
    if is_forwarded:  # anything but an explicit False or an agreed absence counts as forwarded
        return Route(TURN, "forwarded")
    if text == GREETING:
        return Route(NOTE, "greeting", note=NOTES["greeting"])
    if text in MODE_LABELS:
        return Route(MODE, "mode_label", mode=MODE_LABELS[text])
    if text.startswith("/"):
        return Route(TURN, "slash_text")
    return Route(TURN, "text")


def _hold_ok(value: object) -> bool:
    # int is checked apart: math.isfinite(10**400) raises OverflowError
    return (type(value) is int or (type(value) is float and math.isfinite(value))) and value <= MAX_HOLD_SECONDS
