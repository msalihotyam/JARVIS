"""One-turn sequencer (TALKTURN1, T2).

A Salih entry goes in, the brain is asked once, a JARVIS reply entry comes
out. The four collaborators (build_packet, ask_brain, append_entry,
has_reply) are injected callables; this module has no way to act on
anything. Telegram text can never become an action: brain output is reduced
to text + needs_you + proposal-inbox record names, everything else is dropped
and counted.

Pure and deterministic; no I/O, no network, no jarvis import. No exception
escapes. Status/reason texts are fixed ASCII and never echo input.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

MAX_REPLY_CHARS = 4000
MAX_PROPOSALS = 16
STEP_NAMES = frozenset({"build_packet", "ask_brain", "append_entry", "has_reply"})
# Entry kinds whose text may reach the brain; approval_card / taps never do.
BRAIN_KINDS = frozenset({"text", "voice", "transcript", "image", "file"})
_REPLY_FIELDS = frozenset({"text", "needs_you", "proposals"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])", re.IGNORECASE)

DONE, SKIP, FAILED, FAILED_APPEND, REFUSED = (
    "DONE", "SKIP", "FAILED", "FAILED_APPEND", "REFUSED")
REASONS = {
    "ok": "reply appended",
    "already_replied": "entry already has a reply",
    "bad_entry": "entry is not a valid log entry",
    "not_salih": "only Salih entries reach the brain",
    "bad_kind": "entry kind never reaches the brain",
    "bad_steps": "steps must be exactly the four callables",
    "has_reply_failed": "reply check failed",
    "packet_failed": "packet could not be built",
    "brain_failed": "brain gave no usable answer",
    "bad_reply": "reply is not text within the cap",
    "append_failed": "reply could not be appended",
    "internal": "turn could not be processed",
}


@dataclass(frozen=True)
class Reply:
    text: str
    needs_you: bool
    proposals: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    status: str
    reason: str
    reply: Reply | None = None
    dropped_fields: int = 0
    dropped_proposals: int = 0

    @property
    def message(self) -> str:
        return REASONS.get(self.reason, REASONS["internal"])


class _Stop(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def process(entry: object, steps: object, reply: object = None) -> Outcome:
    """Run one turn. Pass the reply of an earlier FAILED_APPEND to retry the append only."""
    try:
        return _process(entry, steps, reply)
    except _Stop as stop:
        return Outcome(stop.status, stop.reason)
    except RecursionError:
        return Outcome(FAILED, "internal")
    except Exception:  # fail closed: nothing escapes
        return Outcome(FAILED, "internal")


def _process(entry, steps, reply):
    if not isinstance(entry, Mapping):
        raise _Stop(REFUSED, "bad_entry")
    entry = dict(entry)  # one snapshot: the checked copy is the one build_packet gets
    entry_id, focus = _check_entry(entry)
    step = _check_steps(steps)
    try:
        replied = step["has_reply"](entry_id)
    except Exception:
        raise _Stop(FAILED, "has_reply_failed") from None
    if replied is True:
        return Outcome(SKIP, "already_replied")
    if replied is not False:
        raise _Stop(FAILED, "has_reply_failed")
    dropped_fields = dropped_names = 0
    if reply is None:
        try:
            packet = step["build_packet"](dict(entry))
        except Exception:
            raise _Stop(FAILED, "packet_failed") from None
        if type(packet) is not str or not packet:
            raise _Stop(FAILED, "packet_failed")
        try:
            raw = step["ask_brain"](packet)
        except Exception:
            raise _Stop(FAILED, "brain_failed") from None
        if raw is None:
            raise _Stop(FAILED, "brain_failed")
        reply, dropped_fields, dropped_names = _reduce(raw)
    elif not _valid_reply(reply):
        raise _Stop(REFUSED, "bad_reply")
    record = {"who": "jarvis", "kind": "text", "text": reply.text,
              "focus": focus, "reply_to": entry_id}
    try:
        appended = step["append_entry"](record)
    except Exception:
        appended = False
    if appended is False:
        return Outcome(FAILED_APPEND, "append_failed", reply, dropped_fields, dropped_names)
    return Outcome(DONE, "ok", reply, dropped_fields, dropped_names)


def _check_entry(entry):
    if not isinstance(entry, Mapping):
        raise _Stop(REFUSED, "bad_entry")
    who, kind = entry.get("who"), entry.get("kind")
    if type(who) is not str or who != "salih":
        raise _Stop(REFUSED, "not_salih")
    if type(kind) is not str or kind not in BRAIN_KINDS:
        raise _Stop(REFUSED, "bad_kind")
    entry_id, focus, text = entry.get("id"), entry.get("focus"), entry.get("text")
    if type(entry_id) is not int or entry_id < 0 or type(text) is not str:
        raise _Stop(REFUSED, "bad_entry")
    if focus is not None and type(focus) is not str:
        raise _Stop(REFUSED, "bad_entry")
    return entry_id, focus


def _check_steps(steps):
    if not isinstance(steps, Mapping) or set(steps.keys()) != STEP_NAMES:
        raise _Stop(REFUSED, "bad_steps")
    step = {name: steps[name] for name in STEP_NAMES}
    if not all(callable(fn) for fn in step.values()):
        raise _Stop(REFUSED, "bad_steps")
    return step


def _reduce(raw):
    """Keep only text, needs_you and proposal names; count everything dropped."""
    if not isinstance(raw, Mapping):
        raise _Stop(FAILED, "brain_failed")
    try:
        all_keys = list(raw.keys())
        keys = [k for k in all_keys if type(k) is str]  # exact str: no subclass __eq__ runs
        dropped_fields = len(all_keys) - sum(1 for k in keys if k in _REPLY_FIELDS)
        text = raw["text"] if "text" in keys else None
        needs_you = raw["needs_you"] if "needs_you" in keys else False
        names = raw["proposals"] if "proposals" in keys else ()
    except Exception:
        raise _Stop(FAILED, "bad_reply") from None
    if not _valid_text(text):
        raise _Stop(FAILED, "bad_reply")
    kept, dropped_names = _names(names)
    reply = Reply(text, type(needs_you) is bool and needs_you, kept)
    return reply, dropped_fields, dropped_names


def _valid_text(text):
    return type(text) is str and bool(text.strip()) and len(text) <= MAX_REPLY_CHARS


def _valid_name(name):
    return (type(name) is str and _NAME.fullmatch(name) is not None
            and _RESERVED.fullmatch(name) is None)


def _names(names):
    if type(names) not in (list, tuple):
        return (), 1
    kept = []
    for name in names:
        if _valid_name(name) and name not in kept and len(kept) < MAX_PROPOSALS:
            kept.append(name)
    return tuple(kept), len(names) - len(kept)


def _valid_reply(reply):
    return (type(reply) is Reply and _valid_text(reply.text)
            and type(reply.needs_you) is bool
            and type(reply.proposals) is tuple
            and len(reply.proposals) <= MAX_PROPOSALS
            and all(_valid_name(n) for n in reply.proposals))
