"""Conversation Context Core: ONE canonical conversation, whatever answers it.

Same-conversation context management, and nothing more (no cross-project or
long-term memory). Pure Python: no Qt, no provider, no network. The only I/O
is the conversation file itself and, for re-hydration, JARVIS's own snapshot of
a file the person attached.

What it holds
-------------
* TURNS - the canonical record of what was said and shown: the person's
  messages, JARVIS's replies and the clean, user-facing task results. Each turn
  keeps its provenance (who said it, what produced it) and the identity of the
  references it carried. Retrying the same turn never adds it twice.
* COMPACTED - when the record outgrows the raw-turn bound, the oldest turns
  keep only a short, deterministic digest line. Older history compacts; it does
  not disappear.
* PINS - the person's own still-active decisions and constraints, with
  supersession: a later correction retires what it replaces, and a retired pin
  never becomes active again. Only the person's own words can create a pin -
  never a reply, a task result, a quote, a file, web content or connector
  output.
* TITLES - the display names of the tasks (and sessions) this conversation
  started, by their internal id: an automatic one taken deterministically from
  the person's first request, and the person's own rename, which always wins.
  A title is a label only; the id stays the identity.

What it produces
----------------
``assemble_context`` turns a conversation and a provider's reported limits
into the four parts a call carries, in this conceptual order:
checkpoint -> pinned -> recent raw turns (verbatim) -> the current turn. The
budget is provider-aware (a context window, an output reserve and, for a CLI
whose request travels on the command line, that transport's ceiling) and never
fills the window. Recent turns are kept whole; what no longer fits is carried
by the checkpoint instead of being dropped.

Trust
-----
Everything here is DATA for the model. A digest line says whose words it
holds; a task result is marked as a result; the checkpoint is never an
instruction. Pins are the person's own decisions - they still grant nothing:
authority, approvals and destinations stay where they always were.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol
from uuid import uuid4
from weakref import WeakKeyDictionary

SCHEMA_VERSION = 1

#: One turn is kept whole up to this size; past it the text is clipped with a
#: visible marker. A General Chat reply has its own, larger ceiling below.
TURN_MAX_CHARS = 8_000
#: DFREPLY (Salih 2026-10-12): the longest General Chat reply JARVIS keeps
#: (``jarvis.intent.CHAT_REPLY_MAX_CHARS`` is this value). A reply turn holds it
#: plus its cut-short marker; a schema-10 reader takes any turn text up to 100,000.
REPLY_MAX_CHARS = 50_000
#: Why a reply the person saw was cut short. Stored as the reply's last line
#: (``cut_reply``), never as a new turn kind, so an older build still reads it.
CUT_REASONS = ("too_long", "cancelled", "timed_out", "error")
_CUT_LINE = re.compile(r"\n\n\[reply cut short: (" + "|".join(CUT_REASONS) + r")\]\Z")
#: The length of one digest line in the checkpoint.
DIGEST_MAX_CHARS = 240
#: Raw turns the record keeps; older turns keep their digest only.
RAW_TURNS_MAX = 160
#: Digest lines the record keeps; older ones are counted, not kept.
COMPACTED_MAX = 600
#: Pins the record keeps (active and retired); retired ones go first.
PINS_MAX = 60
#: Active pins a call carries at most (newest last).
PINS_SHOWN_MAX = 12
PIN_MAX_CHARS = 300
#: Named tasks and sessions the record keeps; the oldest automatic names go first.
TITLES_MAX = 400
TITLE_MAX_CHARS = 120
#: An automatic title: at most this many words and characters.
TITLE_WORDS = 9
TITLE_CHARS = 60

#: The share of a provider's usable window the conversation may take, and a
#: hard ceiling on top: context is carried, not stuffed.
CONTEXT_SHARE = 0.25
CONTEXT_TOKEN_CAP = 24_000
#: Of what remains after the pins, the most recent turns get this share when
#: not everything fits; the rest carries the checkpoint.
RECENT_SHARE = 0.75
#: The newest turns kept verbatim whenever they fit at all.
MIN_RECENT_TURNS = 2
#: Kept free below a command-line transport's ceiling.
TRANSPORT_MARGIN_CHARS = 1_500
#: A re-hydrated file carries at most this much text into one turn.
REHYDRATE_MAX_CHARS = 6_000


# --------------------------------------------------------------------------
# Token estimation
# --------------------------------------------------------------------------


class TokenEstimator(Protocol):
    """How many tokens a text costs. Replaceable by an exact counter later."""

    def tokens(self, text: str) -> int: ...


@dataclass(frozen=True)
class CharEstimator:
    """A deliberately CONSERVATIVE estimate: one token per ``chars_per_token``
    characters. Turkish and code tokenize worse than English prose, so the
    default over-counts rather than under-counts - a call that is estimated too
    large is merely trimmed early; one estimated too small would not fit."""

    chars_per_token: float = 3.0

    def tokens(self, text: str) -> int:
        return math.ceil(len(text) / self.chars_per_token) if text else 0


ESTIMATOR = CharEstimator()


def argv_chars(text: str) -> int:
    """What ``text`` costs on a Windows command line: every quote and backslash
    may be escaped once more when the argument is quoted."""
    return len(text) + text.count('"') + text.count("\\")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# --------------------------------------------------------------------------
# Provider-reported limits
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextProfile:
    """What a provider adapter REPORTS about one call: never conversation state.

    ``transport_chars`` is the ceiling of a request that travels on a command
    line (``None`` when it travels on stdin); ``fixed_chars`` is what that
    transport already spends on the system prompt and the fixed arguments.
    """

    context_window_tokens: int = 128_000
    output_reserve_tokens: int = 8_000
    transport_chars: int | None = None
    fixed_chars: int = 0


DEFAULT_PROFILE = ContextProfile()


def context_profile_of(model: Any) -> ContextProfile:
    """The profile an adapter reports, or the conservative default."""
    report = getattr(model, "context_profile", None)
    try:
        profile = report() if callable(report) else None
    except Exception:  # noqa: BLE001 - a broken report falls back, never fails a call
        profile = None
    return profile if isinstance(profile, ContextProfile) else DEFAULT_PROFILE


# --------------------------------------------------------------------------
# The canonical record
# --------------------------------------------------------------------------

ROLES = ("user", "assistant")
KINDS = ("user", "reply", "result")


@dataclass(frozen=True)
class Turn:
    """One canonical turn.

    ``text`` is what a later call is given for it (a user turn's words plus a
    short provenance line per reference; a result's text behind its marker).
    ``message`` is the person's OWN words and nothing else - the only text a
    pin may come from. ``refs`` are the references the turn carried, by
    identity; a local ``path`` in them is a file handle that never leaves the
    machine.
    """

    id: str
    seq: int
    role: str
    kind: str
    text: str
    message: str = ""
    capability: str = ""
    destination: str = ""
    refs: tuple = ()
    ts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "seq": self.seq, "role": self.role, "kind": self.kind, "text": self.text,
                "message": self.message, "capability": self.capability, "destination": self.destination,
                "refs": [dict(r) for r in self.refs], "ts": self.ts}


@dataclass(frozen=True)
class Compacted:
    """A turn that left the raw record: its digest and whose words it holds."""

    seq: int
    role: str
    kind: str
    digest: str
    capability: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "role": self.role, "kind": self.kind, "digest": self.digest,
                "capability": self.capability}


@dataclass(frozen=True)
class Pin:
    """One of the person's own decisions or constraints."""

    id: str
    kind: str                  # "decision" | "constraint"
    text: str
    turn_seq: int
    status: str = "active"     # "active" | "superseded"
    superseded_by: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "text": self.text, "turn_seq": self.turn_seq,
                "status": self.status, "superseded_by": self.superseded_by}


@dataclass(frozen=True)
class Title:
    """The display name of one task or session (``id``): the automatic one and
    the person's own. The visible name is ``custom`` or else ``auto``."""

    id: str
    auto: str = ""
    custom: str = ""

    @property
    def shown(self) -> str:
        return self.custom or self.auto

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "auto": self.auto, "custom": self.custom}


@dataclass(frozen=True)
class ConversationState:
    """An immutable snapshot of the conversation: safe to hand to a worker."""

    turns: tuple[Turn, ...] = ()
    compacted: tuple[Compacted, ...] = ()
    omitted: int = 0           # digests that no longer fit the record, counted
    pins: tuple[Pin, ...] = ()
    keys: tuple[str, ...] = () # idempotency keys of every turn ever appended
    next_seq: int = 1
    titles: tuple[Title, ...] = ()

    @property
    def active_pins(self) -> tuple[Pin, ...]:
        return tuple(pin for pin in self.pins if pin.active)

    def to_dict(self) -> dict[str, Any]:
        return {"turns": [t.to_dict() for t in self.turns], "compacted": [c.to_dict() for c in self.compacted],
                "omitted": self.omitted, "pins": [p.to_dict() for p in self.pins], "keys": list(self.keys),
                "next_seq": self.next_seq, "titles": [t.to_dict() for t in self.titles]}


class CorruptConversation(ValueError):
    """The stored conversation cannot be trusted. Nothing from it is used."""


def _str(value: Any, name: str, limit: int = 100_000) -> str:
    if type(value) is not str or len(value) > limit:
        raise CorruptConversation(f"'{name}' is not a bounded string")
    return value


def _int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise CorruptConversation(f"'{name}' is not a non-negative integer")
    return value


def state_from_dict(data: Any) -> ConversationState:
    """Strictly re-validate a stored conversation. Any doubt raises."""
    if not isinstance(data, dict):
        raise CorruptConversation("the record is not an object")
    turns, seen = [], set()
    for raw in data.get("turns") if isinstance(data.get("turns"), list) else _bad("turns"):
        if not isinstance(raw, dict):
            raise CorruptConversation("a turn is not an object")
        role, kind = _str(raw.get("role"), "role"), _str(raw.get("kind"), "kind")
        if role not in ROLES or kind not in KINDS:
            raise CorruptConversation("a turn has an unknown role or kind")
        refs = raw.get("refs")
        if not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs):
            raise CorruptConversation("a turn's references are not a list of objects")
        turn = Turn(id=_str(raw.get("id"), "id", 200), seq=_int(raw.get("seq"), "seq"), role=role, kind=kind,
                    text=_str(raw.get("text"), "text"), message=_str(raw.get("message"), "message"),
                    capability=_str(raw.get("capability"), "capability", 200),
                    destination=_str(raw.get("destination"), "destination", 400),
                    refs=tuple(dict(r) for r in refs), ts=_int(raw.get("ts"), "ts"))
        if turn.id in seen or (turns and turn.seq <= turns[-1].seq):
            raise CorruptConversation("turn ids repeat or turns are out of order")
        seen.add(turn.id)
        turns.append(turn)
    compacted = []
    for raw in data.get("compacted") if isinstance(data.get("compacted"), list) else _bad("compacted"):
        if not isinstance(raw, dict):
            raise CorruptConversation("a digest is not an object")
        compacted.append(Compacted(seq=_int(raw.get("seq"), "seq"), role=_str(raw.get("role"), "role"),
                                   kind=_str(raw.get("kind"), "kind"), digest=_str(raw.get("digest"), "digest"),
                                   capability=_str(raw.get("capability"), "capability", 200)))
    pins = []
    for raw in data.get("pins") if isinstance(data.get("pins"), list) else _bad("pins"):
        if not isinstance(raw, dict):
            raise CorruptConversation("a pin is not an object")
        pin = Pin(id=_str(raw.get("id"), "id", 200), kind=_str(raw.get("kind"), "kind"),
                  text=_str(raw.get("text"), "text", PIN_MAX_CHARS * 2), turn_seq=_int(raw.get("turn_seq"), "turn_seq"),
                  status=_str(raw.get("status"), "status"), superseded_by=_str(raw.get("superseded_by"), "superseded_by", 200))
        if pin.kind not in ("decision", "constraint") or pin.status not in ("active", "superseded"):
            raise CorruptConversation("a pin has an unknown kind or status")
        pins.append(pin)
    keys = data.get("keys")
    if not isinstance(keys, list) or not all(type(k) is str for k in keys):
        raise CorruptConversation("'keys' is not a list of strings")
    next_seq = _int(data.get("next_seq"), "next_seq")
    if turns and next_seq <= turns[-1].seq:
        raise CorruptConversation("'next_seq' is behind the last turn")
    # A record written before titles existed simply has none.
    titles, named = [], set()
    for raw in data.get("titles", []) if isinstance(data.get("titles", []), list) else _bad("titles"):
        if not isinstance(raw, dict):
            raise CorruptConversation("a title is not an object")
        title = Title(id=_str(raw.get("id"), "id", 200), auto=_str(raw.get("auto"), "auto", TITLE_MAX_CHARS),
                      custom=_str(raw.get("custom"), "custom", TITLE_MAX_CHARS))
        if not title.id or title.id in named:
            raise CorruptConversation("a title has no id or repeats one")
        named.add(title.id)
        titles.append(title)
    return ConversationState(turns=tuple(turns), compacted=tuple(compacted), omitted=_int(data.get("omitted"), "omitted"),
                             pins=tuple(pins), keys=tuple(keys), next_seq=max(next_seq, 1), titles=tuple(titles))


def _bad(name: str) -> list:
    raise CorruptConversation(f"'{name}' is not a list")


def _integrity(body: dict[str, Any]) -> str:
    return hashlib.sha256(json_text(body).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Appending: idempotent, compacting, pinning
# --------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 40].rstrip() + " [... clipped for context ...]"


def cut_reply(text: str, reason: str) -> str:
    """A reply that was cut short, as the turn stores it: what was shown, at most
    ``REPLY_MAX_CHARS``, then one marker line naming why. Plain text in the turn's
    own field, so a later call (and an older build) reads it as said."""
    if reason not in CUT_REASONS:
        raise ValueError(f"unknown cut reason {reason!r}")
    return f"{str(text)[:REPLY_MAX_CHARS]}\n\n[reply cut short: {reason}]"


def plain_reply(text: str) -> str:
    """A reply that was NOT cut, as the turn stores it: one that merely ends with
    the marker's words (a model can write them) gets a trailing newline, so it is
    never read back as cut short."""
    return f"{text}\n" if _CUT_LINE.search(text) else text


def split_cut(text: str) -> tuple[str, str]:
    """``(shown text, reason)`` of a stored reply; reason ``""`` when it was not cut."""
    found = _CUT_LINE.search(text)
    return (text[: found.start()], found.group(1)) if found else (text, "")


def digest(text: str) -> str:
    """One deterministic line for a turn: its opening, whitespace collapsed."""
    line = " ".join(str(text).split())
    return line if len(line) <= DIGEST_MAX_CHARS else line[: DIGEST_MAX_CHARS - 1].rstrip() + "…"


def append_turn(state: ConversationState, *, role: str, kind: str, text: str, message: str = "",
                capability: str = "", destination: str = "", refs: Iterable[Mapping[str, Any]] = (),
                key: str = "", now: int | None = None, raw_turns_max: int = RAW_TURNS_MAX,
                on_compact: Callable[[tuple[Turn, ...]], None] | None = None) -> tuple[ConversationState, Turn | None]:
    """The state with one more turn, or the same state when ``key`` was seen.

    ``key`` is the turn's idempotency key: a retry, a re-delivered result or a
    reopen that replays a turn finds it and adds nothing.

    ``on_compact`` (PreCompact) is called with exactly the raw turns about to
    compact, BEFORE they do. It may only look; whatever it raises is dropped,
    so a capture failure never blocks compaction.
    """
    if role not in ROLES or kind not in KINDS:
        raise ValueError(f"unknown role/kind {role}/{kind}")
    text = _clip(str(text), REPLY_MAX_CHARS + 64 if kind == "reply" else TURN_MAX_CHARS)
    if not text.strip():
        return state, None
    if key and key in state.keys:
        return state, None
    seq = state.next_seq
    turn = Turn(id=f"t{seq}-{hashlib.sha256(f'{seq}:{key}:{text}'.encode()).hexdigest()[:10]}", seq=seq,
                role=role, kind=kind, text=text, message=str(message)[:TURN_MAX_CHARS] if role == "user" else "",
                capability=str(capability)[:200], destination=str(destination)[:400],
                refs=tuple(dict(r) for r in refs), ts=int(now if now is not None else time.time()))
    turns = state.turns + (turn,)
    compacted, omitted = state.compacted, state.omitted
    if on_compact is not None and len(turns) > raw_turns_max:
        try:
            on_compact(turns[: len(turns) - raw_turns_max])
        except Exception:  # noqa: BLE001 - capture never blocks compaction (I12)
            pass
    while len(turns) > raw_turns_max:
        # Older history compacts into the checkpoint; it does not disappear.
        old, turns = turns[0], turns[1:]
        compacted = compacted + (Compacted(seq=old.seq, role=old.role, kind=old.kind,
                                           digest=digest(old.text), capability=old.capability),)
    if len(compacted) > COMPACTED_MAX:
        omitted += len(compacted) - COMPACTED_MAX
        compacted = compacted[-COMPACTED_MAX:]
    pins = update_pins(state.pins, turn) if role == "user" else state.pins
    keys = state.keys + ((key,) if key else ())
    return replace(state, turns=turns, compacted=compacted, omitted=omitted, pins=pins,
                   keys=keys[-(raw_turns_max * 4):], next_seq=seq + 1), turn


# -- titles -------------------------------------------------------------------
#
# Deterministic and conservative, like the pins: a title is taken from the
# person's own first request - its first sentence that says anything, with a
# leading greeting or courtesy dropped - and is never a paraphrase. Nothing that
# is not in the request can appear in it.

_TITLE_FILLER = frozenset({
    "lütfen", "rica", "etsem", "ederim", "şimdi", "acaba", "hey", "merhaba", "selam", "jarvis", "please", "hi",
    "hello", "now", "kindly", "can", "could", "would", "you",
})
_TITLE_TRAILING = frozenset({"lütfen", "please", "jarvis"})
#: Joining words that stay lower-case inside a title, per language ("on" is ten in Turkish).
_TITLE_MINOR_TR = frozenset({"ve", "ile", "veya", "ya", "da", "de", "ki", "mi", "mı", "mu", "mü"})
_TITLE_MINOR_EN = frozenset({"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "by", "with"})
_TURKISH_LETTERS = frozenset("çğıöşüÇĞİÖŞÜ")
_PUNCT = ",.;:!?\"'()[]{}«»“”‘’…-–—"


def _bare(word: str) -> str:
    return word.strip(_PUNCT).casefold()


def _capital(word: str, turkish: bool) -> str:
    """A word as a title shows it. A word that already has a capital (GitHub,
    JARVIS) or is not a plain word (auth.py, v2) is kept exactly as written; a
    trailing comma or the like stays where it is."""
    core = word.rstrip(_PUNCT)
    if any(ch.isupper() for ch in word) or not core.isalpha():
        return word
    first = core[0]
    upper = {"i": "İ", "ı": "I"}.get(first, first.upper()) if turkish else first.upper()
    return upper + word[1:]


def task_title(request: str) -> str:
    """A short, readable name for the task a request started, or "" when the
    request says nothing a title could be made of."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", str(request or "")):
        words = [w for w in sentence.split() if w.strip(_PUNCT)]
        while words and _bare(words[0]) in _TITLE_FILLER:
            words.pop(0)
        while words and _bare(words[-1]) in _TITLE_TRAILING:
            words.pop()
        if not any(any(ch.isalpha() for ch in w) for w in words):
            continue
        cut = len(words) > TITLE_WORDS
        words = words[:TITLE_WORDS]
        turkish = any(ch in _TURKISH_LETTERS for ch in sentence)
        minor = _TITLE_MINOR_TR if turkish else _TITLE_MINOR_EN
        shown = [w.strip(_PUNCT) if i in (0, len(words) - 1) else w for i, w in enumerate(words)]
        shown = [_capital(w, turkish) if i == 0 or _bare(w) not in minor else w.casefold()
                 for i, w in enumerate(shown) if w]
        while len(" ".join(shown)) > TITLE_CHARS and len(shown) > 1:
            shown.pop()
            cut = True
        return " ".join(shown) + ("…" if cut else "")
    return ""


def name_titles(state: ConversationState, ids: Iterable[str], auto: str) -> ConversationState:
    """``auto`` as the automatic title of every id that has none yet: the first
    request names a task (and its session) once, and nothing renames it later."""
    auto = str(auto or "").strip()[:TITLE_MAX_CHARS]
    known = {t.id for t in state.titles}
    fresh = tuple(Title(id=str(i)[:200], auto=auto) for i in dict.fromkeys(ids) if i and str(i) not in known)
    if not auto or not fresh:
        return state
    return replace(state, titles=_bounded_titles(state.titles + fresh))


def rename_title(state: ConversationState, id: str, custom: str) -> ConversationState:
    """The person's own name for ``id``; an empty name changes nothing."""
    custom = " ".join(str(custom or "").split())[:TITLE_MAX_CHARS]
    if not custom or not id:
        return state
    kept = tuple(t for t in state.titles if t.id != id)
    old = next((t for t in state.titles if t.id == id), Title(id=str(id)[:200]))
    return replace(state, titles=_bounded_titles(kept + (replace(old, custom=custom),)))


def _bounded_titles(titles: tuple[Title, ...]) -> tuple[Title, ...]:
    titles = list(titles)
    while len(titles) > TITLES_MAX:
        oldest = next((i for i, t in enumerate(titles) if not t.custom), 0)
        titles.pop(oldest)
    return tuple(titles)


# -- pins ---------------------------------------------------------------------
#
# Deterministic, bilingual (Turkish / English) cue grammar. It is conservative on
# purpose: a missed pin still leaves the turn in the recent history and the
# checkpoint, while a pin taken from anywhere but the person's own words could
# make untrusted text look like an instruction - so nothing else can create one.

_CONSTRAINT = re.compile(
    r"(\bdo not\b|\bdon't\b|\bnever\b|\bmust not\b|\bnot reopen\b|\basla\b|\bsakın\b|"
    r"\b\w*(yapma|dokunma|değiştirme|açma|ekleme|silme|kaldırma|gönderme|kullanma|"
    r"commit etme|push etme|başlatma)\b)")
_DECISION = re.compile(
    r"(\bkarar\w*|\bkullanacağız\b|\bkullanalım\b|\bkullanıyoruz\b|\bkullan\b|\bolsun\b|\bseçtik\b|\bseçtim\b|"
    r"\btercih\w*|\bbundan sonra\b|\bartık\b|\bher zaman\b|\bfrom now on\b|\bwe will use\b|\bwe'll use\b|"
    r"\blet's use\b|\bwe use\b|\bdecided\b|\bdecision\b|\bgo with\b|\bstick with\b|\balways\b)")
_CORRECTION = re.compile(
    r"(\bhayır\b|\byanlış\b|\bvazgeç\w*|\bonun yerine\b|\byerine\b|\bdüzeltme\b|\bdüzelt\w*|\bson karar\b|"
    r"\biptal\b|\bdeğil\b|\binstead\b|\bactually\b|\bcorrection\b|\bscratch that\b|\bchange of plan\b|\bnot\b.*\bbut\b)")
_LIFT = re.compile(
    r"(\b\w+(abilirsin|ebilirsin)\b|\bserbestsin\b|\bkısıt\w* kaldır\w*|\bizin veriyorum\b|\byou may now\b|\bno longer\b|"
    r"\blift the\b)")

#: A sentence that REPORTS, QUOTES, EXEMPLIFIES, NEGATES or SUPPOSES a
#: decision is not one: "X yapma demedim", "örnek: X yapma", "Ahmet 'silme'
#: dedi", "eğer X yapma dersem". Nothing in it is pinned.
_NOT_OWN = re.compile(
    r"(\bdemedim\b|\bdememiştim\b|\bdemiyorum\b|\bdedi\b|\bdemiş\b|\bdiyor\b|\bsöyledi\b|\byazmış\b|"
    r"\bdiye bir\b|\börnek\w*|\bmesela\b|\beğer\b|\bşayet\b|\bdersem\b|\bdesem\b|\bdiyelim\b|"
    r"\bvarsayalım\b|\bdidn't say\b|\bdid not say\b|\bnever said\b|\bsaid\b|\bsays\b|\btold\b|"
    r"\bfor example\b|\be\.g\.|\bexample\b|\bif i say\b|\bsuppose\b|\bhypothetical\w*|\bwhat if\b|"
    r"[\"“”«»„])")

#: Words that say HOW something is said, not WHAT it is about. A 5-letter stem
#: of anything else is a subject word two pins can share.
_STOP_STEMS = frozenset({
    "karar", "kulla", "olsun", "olaca", "artık", "bunda", "sonra", "hayır", "yerin", "onun", "tercı", "terci",
    "seçti", "seçtı", "düzel", "düzet", "yanlı", "vazge", "değil", "iptal", "zaman", "öneml", "kesin", "lütfe",
    "şimdi", "proje", "jarvi", "bunu", "şunu", "için", "olara", "gibi", "daha", "çok", "ancak", "fakat",
    "always", "never", "decid", "decis", "using", "going", "stick", "inste", "actua", "corre", "chang",
    "scrat", "that", "this", "with", "from", "will", "shoul", "must", "project", "proje", "yapma", "dokun",
    "açma", "reope", "tekra", "olmas", "yapal", "yapac", "ediyo", "edece", "diyor", "istiy",
})


def _fold(text: str) -> str:
    """Turkish-aware lower case: İ -> i and I -> ı before lowering."""
    return str(text).replace("İ", "i").replace("I", "ı").lower()


def _folds(text: str) -> tuple[str, str]:
    return _fold(text), str(text).casefold()


def _matches(pattern: re.Pattern, text: str) -> bool:
    return any(pattern.search(form) for form in _folds(text))


def _stems(text: str) -> frozenset[str]:
    words = re.findall(r"[^\W\d_]{4,}", _fold(text))
    return frozenset(word[:5] for word in words if word[:5] not in _STOP_STEMS and word[:4] not in _STOP_STEMS)


def _sentences(message: str) -> list[str]:
    # A semicolon stays inside its sentence: "DO NOT REOPEN; tekrar açma." is one
    # constraint, lifted or kept as one.
    parts = re.split(r"(?<=[.!?\n])\s+", str(message).strip())
    return [" ".join(p.split()) for p in parts if p and p.strip()]


def pin_candidates(message: str) -> list[tuple[str, str]]:
    """``(kind, sentence)`` for each sentence of the person's message that is a
    decision, a constraint, or a ``lift`` - an explicit statement that an
    earlier constraint no longer applies. Questions are never pinned."""
    out = []
    for sentence in _sentences(message):
        if sentence.endswith("?") or len(sentence) < 8 or _matches(_NOT_OWN, sentence):
            continue
        if _matches(_LIFT, sentence):
            out.append(("lift", sentence[:PIN_MAX_CHARS]))
        elif _matches(_CONSTRAINT, sentence):
            out.append(("constraint", sentence[:PIN_MAX_CHARS]))
        elif _matches(_DECISION, sentence):
            out.append(("decision", sentence[:PIN_MAX_CHARS]))
    return out


def update_pins(pins: tuple[Pin, ...], turn: Turn) -> tuple[Pin, ...]:
    """The pins after one of the person's turns. Supersession, deterministically:

    * only an explicit CORRECTION ("hayır", "düzeltme", "onun yerine",
      "instead", "son karar", …) retires anything. It retires the active pins
      of its kind that share the MOST subject words with it - so two scopes of
      one subject ("frontend'de …" / "backend'de …") stay apart - or, sharing
      none, the most recent decision;
    * without a correction, a new decision is added beside the others: two
      live decisions are shown oldest first, the later one holds, and the
      person's current message overrides every pin;
    * a CONSTRAINT ("DO NOT …", "asla …", "… yapma") is retired only by a
      correction or an explicit lift ("… açabilirsin", "you may now …") about
      the same subject;
    * a retired pin never becomes active again.

    Pins are DERIVED conversation state with provenance (``turn_seq``): never a
    source of authority, and never above what the person says now.
    """
    if turn.role != "user" or not turn.message:
        return pins
    pins = list(pins)
    for index, (kind, sentence) in enumerate(pin_candidates(turn.message)):
        new = Pin(id=f"p{turn.seq}-{index}", kind=kind, text=sentence, turn_seq=turn.seq)
        if any(p.id == new.id for p in pins):
            continue
        if any(p.active and _fold(p.text) == _fold(sentence) for p in pins):
            continue                                   # said again: still the same pin
        stems = _stems(sentence)
        if kind == "lift":
            # Retires the constraints it names; it binds nothing itself.
            targets = _closest([p for p in pins if p.active and p.kind == "constraint"], stems)
        elif _matches(_CORRECTION, sentence):
            active = [p for p in pins if p.active and p.kind == kind]
            targets = _closest(active, stems) or (active[-1:] if kind == "decision" else [])
        else:
            targets = []
        retired = {p.id for p in targets}
        pins = [replace(p, status="superseded", superseded_by=new.id) if p.id in retired else p for p in pins]
        if kind != "lift":
            pins.append(new)
    # Bounded: retired pins leave first, then the oldest.
    while len(pins) > PINS_MAX:
        victim = next((p for p in pins if not p.active), pins[0])
        pins.remove(victim)
    return tuple(pins)


def _closest(pins: list[Pin], stems: frozenset[str]) -> list[Pin]:
    """The pins sharing the most subject words with ``stems`` (none if none)."""
    scored = [(len(_stems(p.text) & stems), p) for p in pins]
    best = max((score for score, _ in scored), default=0)
    return [p for score, p in scored if best and score == best]


# --------------------------------------------------------------------------
# The durable store
# --------------------------------------------------------------------------


#: Windows refuses to replace or open a file for a moment while another
#: process (the other window, a virus scanner, the indexer) has it open; such a
#: moment is retried briefly instead of being treated as a failed write.
_BUSY_RETRIES = 25
_BUSY_WAIT_SECONDS = 0.04


def _retry_busy(action):
    for attempt in range(_BUSY_RETRIES):
        try:
            return action()
        except PermissionError:
            if attempt == _BUSY_RETRIES - 1:
                raise
            time.sleep(_BUSY_WAIT_SECONDS)


@contextmanager
def _file_lock(path: Path):
    """A cross-process advisory lock on ``path`` - the same idiom the
    configuration store uses (``jarvis.settings``). The lock FILE means nothing
    on its own; the operating system's lock on the open handle excludes a
    second writer. Raises ``OSError`` when it cannot be taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - closed in finally
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class ConversationStore:
    """The conversation file: one atomic JSON document with an integrity digest.

    A record that cannot be read or does not verify is NOT used: ``open``
    moves it aside, starts from nothing and reports why - it never invents a
    replacement conversation.

    Nothing makes JARVIS a single instance, so two windows can share this
    file. Every append is therefore read-modify-write under a cross-process
    lock: the turn is applied to the file's CURRENT content, never to a stale
    copy, so neither writer loses the other's turns. A write that cannot take
    the lock keeps the turn in memory and writes it with the next append.
    """

    def __init__(self, path: str | Path, state: ConversationState | None = None,
                 raw_turns_max: int = RAW_TURNS_MAX) -> None:
        self.path = Path(path)
        self.state = state or ConversationState()
        self.raw_turns_max = raw_turns_max
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        #: Changes not yet written (a lock that could not be taken), each a pure
        #: function of the record, re-applied to the file's current content.
        self._pending: list[Any] = []
        #: The last problem a write met ("" when none).
        self.diagnostic = ""
        self._instance = hashlib.sha256(f"{os.getpid()}:{time.time_ns()}:{id(self)}".encode()).hexdigest()[:12]
        self._auto = 0

    @classmethod
    def open(cls, path: str | Path, raw_turns_max: int = RAW_TURNS_MAX) -> tuple["ConversationStore", str]:
        """The store for ``path`` and a diagnostic ("" when all is well)."""
        store = cls(path, raw_turns_max=raw_turns_max)
        try:
            with _file_lock(store.lock_path):
                if not store.path.exists():
                    return store, ""
                try:
                    store.state = load_state(store.path)
                    return store, ""
                except CorruptConversation as exc:
                    kept = store.path.with_name(f"{store.path.stem}.corrupt-{int(time.time())}{store.path.suffix}")
                    try:
                        _retry_busy(lambda: os.replace(store.path, kept))
                    except OSError:
                        kept = store.path
                    return store, f"{exc} (kept as {kept.name})"
        except OSError as exc:
            # The lock itself could not be taken: nothing is read, nothing invented.
            return store, f"the conversation file could not be opened: {type(exc).__name__}"

    def append(self, **fields: Any) -> Turn | None:
        # Every append has an identity, so re-applying it on the file's current
        # content (``flush``) can never add it twice.
        if not fields.get("key"):
            self._auto += 1
            fields["key"] = f"auto:{self._instance}:{self._auto}"
        state, turn = append_turn(self.state, raw_turns_max=self.raw_turns_max, **fields)
        if turn is not None:
            self.state = state
            fields = dict(fields, now=turn.ts)
            self._change(lambda base: append_turn(base, raw_turns_max=self.raw_turns_max, **fields)[0])
        return turn

    def name_auto(self, ids: Iterable[str], auto: str) -> None:
        """Name the ids that have no title yet (see ``name_titles``)."""
        ids = tuple(ids)
        if name_titles(self.state, ids, auto) is not self.state:
            self._change(lambda base: name_titles(base, ids, auto))

    def rename(self, id: str, custom: str) -> bool:
        """The person's rename of ``id``; False (and nothing changed) when empty."""
        if rename_title(self.state, id, custom) is self.state:
            return False
        self._change(lambda base: rename_title(base, id, custom))
        return True

    def title(self, id: str) -> str:
        return next((t.shown for t in self.state.titles if t.id == id), "")

    def _change(self, apply) -> None:
        self.state = apply(self.state)
        self._pending.append(apply)
        self.flush()

    def flush(self) -> None:
        """Write the pending appends on top of what the file holds NOW."""
        if not self._pending:
            return
        try:
            with _file_lock(self.lock_path):
                base = self.state
                if self.path.exists():
                    try:
                        base = load_state(self.path)
                    except CorruptConversation as exc:
                        # Someone left a record that does not verify: it is set
                        # aside, never merged, and this window's own state stands.
                        self.diagnostic = str(exc)
                        kept = self.path.with_name(f"{self.path.stem}.corrupt-{int(time.time())}{self.path.suffix}")
                        _retry_busy(lambda: os.replace(self.path, kept))
                        base, self._pending = self.state, []
                for apply in self._pending:
                    base = apply(base)
                self.state = base
                self.save()
                self._pending = []
        except OSError as exc:
            self.diagnostic = f"the conversation file could not be written: {type(exc).__name__}"

    def save(self) -> None:
        body = {"version": SCHEMA_VERSION, "state": self.state.to_dict()}
        document = {**body, "integrity": _integrity(body)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        _retry_busy(lambda: os.replace(temporary, self.path))


#: PreCompact hooks per Core store, set by the app (``register_precompact_hook``).
#: Keyed by store so every ``PersistedConversation`` over it - the window makes
#: many - gets the hook without being told; no entry means no hook (no-op).
_PRECOMPACT_HOOKS: WeakKeyDictionary = WeakKeyDictionary()


def register_precompact_hook(store: Any, hook: Callable[[str, tuple[Turn, ...]], None] | None) -> None:
    """Install (``None``: remove) ``hook(conversation_id, turns)`` for ``store``.

    It runs inside the store's write transaction, before compaction, with the
    exact turns about to compact: it must only snapshot and hand off.
    A transaction that then rolls back still fired the hook; Core's
    run-id ledger dedupes the re-offer, so no pre-commit hook was added."""
    if hook is None:
        _PRECOMPACT_HOOKS.pop(store, None)
    else:
        _PRECOMPACT_HOOKS[store] = hook


class PersistedConversation:
    """One conversation held in the Core store, with the surface the window
    uses: ``state``, ``append``, ``name_auto``, ``rename``, ``title``.

    Every change is applied to the STORED record inside one store transaction
    (``update_conversation_state``: read, verify, change, write), so two
    windows never lose each other's turns, and ``state`` is then what was
    written. A change the store refuses - a record that does not verify, an
    archived conversation, a storage failure - is NOT kept anywhere: the
    verified state stays as it was and ``diagnostic`` says why (fail closed).

    ``store`` ``None`` is a conversation that cannot be saved at all (the
    configuration was refused): it lives in memory for this window only and
    ``diagnostic`` says so from the start.
    """

    def __init__(self, store: Any, conversation_id: str | None, state: ConversationState | None = None,
                 raw_turns_max: int = RAW_TURNS_MAX) -> None:
        self.store = store
        self.id = conversation_id
        self.state = state or ConversationState()
        self.raw_turns_max = raw_turns_max
        self.diagnostic = "" if store is not None else "the conversation store is unavailable"
        self._instance = hashlib.sha256(f"{os.getpid()}:{time.time_ns()}:{id(self)}".encode()).hexdigest()[:12]
        self._auto = 0

    def append(self, **fields: Any) -> Turn | None:
        if not fields.get("key"):
            self._auto += 1
            fields["key"] = f"auto:{self._instance}:{self._auto}"
        state, turn = append_turn(self.state, raw_turns_max=self.raw_turns_max, **fields)
        if turn is None:
            return None
        fields = dict(fields, now=turn.ts)
        # The hook fires on the stored apply only (it runs once, on the
        # authoritative record), never on the local preview above.
        try:
            hook = _PRECOMPACT_HOOKS.get(self.store) if self.store is not None and self.id is not None else None
        except TypeError:  # a store that cannot be weakly keyed has no hook
            hook = None
        on_compact = None if hook is None else (lambda turns, cid=self.id: hook(cid, turns))
        return turn if self._change(
            lambda base: append_turn(base, raw_turns_max=self.raw_turns_max, on_compact=on_compact, **fields)[0]
        ) else None

    def name_auto(self, ids: Iterable[str], auto: str) -> None:
        ids = tuple(ids)
        if name_titles(self.state, ids, auto) is not self.state:
            self._change(lambda base: name_titles(base, ids, auto))

    def rename(self, id: str, custom: str) -> bool:
        if rename_title(self.state, id, custom) is self.state:
            return False
        return self._change(lambda base: rename_title(base, id, custom))

    def title(self, id: str) -> str:
        return next((t.shown for t in self.state.titles if t.id == id), "")

    def flush(self) -> None:
        """Nothing is ever pending: every change was written, or refused."""

    def _change(self, apply) -> bool:
        if self.store is None or self.id is None:
            self.state = apply(self.state)
            return False
        try:
            self.state = self.store.update_conversation_state(self.id, apply)
        except Exception as exc:  # noqa: BLE001 - storage boundary; fail closed
            self.diagnostic = f"the conversation could not be saved: {type(exc).__name__}"
            return False
        self.diagnostic = ""
        return True


def load_state(path: str | Path) -> ConversationState:
    return read_verified(path)[0]


def read_verified(path: str | Path) -> tuple[ConversationState, str]:
    """The conversation file's state AND the integrity digest it was verified
    against. READ ONLY: nothing is renamed, locked or rewritten here, so the
    legacy file of a rollback release can be read without disturbing it."""
    try:
        document = json.loads(_retry_busy(lambda: Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise CorruptConversation(f"the conversation file could not be read: {type(exc).__name__}") from exc
    if not isinstance(document, dict) or document.get("version") != SCHEMA_VERSION:
        raise CorruptConversation("unknown conversation schema version")
    body = {"version": document.get("version"), "state": document.get("state")}
    integrity = _integrity(body)
    if document.get("integrity") != integrity:
        raise CorruptConversation("the conversation file does not match its integrity digest")
    return state_from_dict(document.get("state")), integrity


# --------------------------------------------------------------------------
# The same record, stored as a database row instead of a file
# --------------------------------------------------------------------------
#
# The row keeps exactly what the file keeps - the state and its integrity
# digest over ``{"version", "state"}`` - so one validator (``state_from_dict``)
# and one digest (``_integrity``) serve both. A conversation's identity is its
# id, never its title, provider, project name or timestamps.

CONVERSATION_ID = re.compile(r"^c-[0-9a-f]{32}$")
CONVERSATION_ORIGINS = ("jarvis", "legacy-import")
CONVERSATION_TITLE_MAX = 120


def new_conversation_id() -> str:
    return "c-" + uuid4().hex


def encode_state(state: ConversationState) -> tuple[str, str]:
    """``(state_json, integrity)`` for a row, from a state already built by
    this module - never from unvalidated input."""
    data = state.to_dict()
    return json_text(data), _integrity({"version": SCHEMA_VERSION, "state": data})


def decode_state(state_json: Any, integrity: Any) -> ConversationState:
    """A stored row back into a state, verified BEFORE anything is used.
    Nothing is repaired: a mismatch or a malformed record raises."""
    if type(state_json) is not str or type(integrity) is not str:
        raise CorruptConversation("the stored conversation is not text")
    try:
        data = json.loads(state_json)
    except ValueError as exc:
        raise CorruptConversation("the stored conversation is not JSON") from exc
    if _integrity({"version": SCHEMA_VERSION, "state": data}) != integrity:
        raise CorruptConversation("the stored conversation does not match its integrity digest")
    return state_from_dict(data)


@dataclass(frozen=True)
class ConversationRecord:
    """What a conversation IS, apart from what was said in it.

    ``project_id`` is its owner: ``None`` means Global. The owner is fixed at
    creation - the one exception is the one-way attach of a Global
    conversation to a project (Gate 6C) - and is context, never authority: it
    grants nothing."""

    id: str
    project_id: str | None
    title: str
    origin: str
    created_at: str
    updated_at: str
    archived_at: str | None = None


# --------------------------------------------------------------------------
# Assembly: provider-aware, budgeted, recent-first
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextPlan:
    """What ONE call carries of the conversation, in reading order."""

    checkpoint: dict[str, Any] | None
    pinned: tuple[dict[str, Any], ...]
    recent: tuple[dict[str, str], ...]
    budget_tokens: int
    used_tokens: int
    budget_chars: int | None
    used_chars: int
    stats: dict[str, int] = field(default_factory=dict)


def _as_state(conversation: Any) -> ConversationState:
    """A plain list of ``{role, text}`` turns (the older call shape) becomes a
    state with no pins and no provenance - still budgeted the same way."""
    if isinstance(conversation, ConversationState):
        return conversation
    state = ConversationState()
    for item in conversation or ():
        if not isinstance(item, Mapping) or item.get("role") not in ROLES or type(item.get("text")) is not str:
            continue
        text = "".join(ch for ch in item["text"] if ch >= " " or ch in "\n\t").strip()
        if text:
            state, _ = append_turn(state, role=item["role"], kind="user" if item["role"] == "user" else "reply",
                                   text=text, now=0, raw_turns_max=10**9)
    return state


def _line(role: str, kind: str, capability: str, text: str, superseded: bool) -> str:
    who = "user" if role == "user" else ("task result" + (f" ({capability})" if capability else "")
                                         if kind == "result" else "jarvis")
    return f"{who}: {text}" + (" [a later decision replaced this]" if superseded else "")


def _pins_for(state: ConversationState, current_text: str) -> tuple[Pin, ...]:
    """The active pins as THIS call must see them: the current message's own
    corrections and lifts are applied first, so a pin it replaces is never
    shown as still in force. Its own new decisions are not repeated - they are
    the request itself."""
    now = state.next_seq
    view = update_pins(state.pins, Turn(id="current", seq=now, role="user", kind="user",
                                        text=current_text, message=current_text))
    return tuple(p for p in view if p.active and p.turn_seq != now)


def assemble_context(conversation: Any, profile: ContextProfile = DEFAULT_PROFILE, current_text: str = "", *,
                     extra_chars: int = 0, estimator: TokenEstimator = ESTIMATOR) -> ContextPlan:
    """The conversation part of ONE call, inside this provider's budget.

    Priority, highest first: the current message (never cut; it is outside
    this plan) -> the active pins -> the newest turns verbatim -> the
    checkpoint of everything older. ``extra_chars`` is what the rest of the
    call already spends (the catalog, this turn's references and files).
    """
    state = _as_state(conversation)
    usable = max(0, profile.context_window_tokens - profile.output_reserve_tokens)
    budget = min(int(usable * CONTEXT_SHARE), CONTEXT_TOKEN_CAP)
    budget -= estimator.tokens(current_text) + estimator.tokens("x" * max(0, extra_chars))
    budget = max(0, budget)
    chars = None
    if profile.transport_chars is not None:
        chars = max(0, profile.transport_chars - profile.fixed_chars - TRANSPORT_MARGIN_CHARS
                    - argv_chars(current_text) - max(0, extra_chars))
    used_tokens, used_chars = 0, 0

    def cost(value: Any) -> tuple[int, int]:
        encoded = json_text(value)
        return estimator.tokens(encoded), argv_chars(encoded)

    def fits(tokens: int, length: int, token_limit: int, char_limit: int | None) -> bool:
        return used_tokens + tokens <= token_limit and (char_limit is None or used_chars + length <= char_limit)

    # 1. The person's still-active decisions and constraints (newest kept).
    pinned: list[dict[str, Any]] = []
    for pin in reversed(_pins_for(state, current_text)[-PINS_SHOWN_MAX:]):
        item = {"kind": pin.kind, "text": pin.text, "said_at_turn": pin.turn_seq}
        t, c = cost(item)
        if not fits(t, c, int(budget * 0.3) or budget, None if chars is None else int(chars * 0.3)):
            break
        pinned.insert(0, item)
        used_tokens, used_chars = used_tokens + t, used_chars + c

    # 2. The newest turns, verbatim.
    rendered = [{"role": turn.role, "text": turn.text} for turn in state.turns]
    costs = [cost(item) for item in rendered]
    everything = sum(t for t, _ in costs), sum(c for _, c in costs)
    older_exists = bool(state.compacted or state.omitted)
    all_fit = (not older_exists and used_tokens + everything[0] <= budget
               and (chars is None or used_chars + everything[1] <= chars))
    share = 1.0 if all_fit else RECENT_SHARE
    recent_tokens = used_tokens + int((budget - used_tokens) * share)
    recent_chars = None if chars is None else used_chars + int((chars - used_chars) * share)
    picked = 0
    for index in range(len(rendered) - 1, -1, -1):
        t, c = costs[index]
        keep_floor = picked < MIN_RECENT_TURNS and fits(t, c, budget, chars)
        if not (fits(t, c, recent_tokens, recent_chars) or keep_floor):
            break
        used_tokens, used_chars = used_tokens + t, used_chars + c
        picked += 1
    recent = tuple(rendered[len(rendered) - picked:])

    # 3. The checkpoint: every older turn as one digest line, newest first
    #    until the budget ends; the rest is counted, never invented.
    in_force = _pins_for(state, current_text)
    retired_at = {p.turn_seq for p in state.pins if p not in in_force} - {p.turn_seq for p in in_force}
    older = [(c.seq, _line(c.role, c.kind, c.capability, c.digest, c.seq in retired_at)) for c in state.compacted]
    older += [(t.seq, _line(t.role, t.kind, t.capability, digest(t.text), t.seq in retired_at))
              for t in state.turns[: len(state.turns) - picked]]
    lines: list[str] = []
    base = {"summarized_turns": 0, "omitted_turns": 0, "lines": []}
    t0, c0 = cost(base)
    if older and fits(t0, c0, budget, chars):
        used_tokens, used_chars = used_tokens + t0, used_chars + c0
        for _, line in reversed(older):
            t, c = cost(line)
            if not fits(t + 1, c + 1, budget, chars):
                break
            lines.insert(0, line)
            used_tokens, used_chars = used_tokens + t + 1, used_chars + c + 1
    omitted = state.omitted + (len(older) - len(lines))
    checkpoint = ({"summarized_turns": len(lines), "omitted_turns": omitted, "lines": lines}
                  if (lines or omitted) else None)
    return ContextPlan(checkpoint=checkpoint, pinned=tuple(pinned), recent=recent, budget_tokens=budget,
                       used_tokens=used_tokens, budget_chars=chars, used_chars=used_chars,
                       stats={"turns": len(state.turns), "recent": len(recent), "checkpoint_lines": len(lines),
                              "omitted": omitted, "pinned": len(pinned)})


def context_fields(plan: ContextPlan) -> dict[str, Any]:
    """The plan as the call's context keys. ``conversation`` keeps its shape."""
    fields: dict[str, Any] = {"conversation": [dict(turn) for turn in plan.recent]}
    if plan.checkpoint is not None:
        fields["conversation_checkpoint"] = plan.checkpoint
    if plan.pinned:
        fields["pinned"] = [dict(pin) for pin in plan.pinned]
    return fields


# --------------------------------------------------------------------------
# References: provenance always, content again only when named again
# --------------------------------------------------------------------------


def turn_refs(groups: Any, attachments: Any = ()) -> list[dict[str, Any]]:
    """The references a turn carried, by identity: which group owned what.

    The quoted text itself is not repeated (its digest is kept); a file keeps
    its name, kind, digest and JARVIS's own snapshot path - a local handle that
    is used only for re-hydration and never leaves the machine.
    """
    refs: list[dict[str, Any]] = []

    def file_ref(item: Any, group: str) -> dict[str, Any] | None:
        if not isinstance(item, Mapping) or item.get("kind") not in ("text", "image"):
            return None
        return {"type": "file", "kind": str(item["kind"]), "name": str(item.get("name") or "")[:120],
                "sha256": str(item.get("sha256") or ""), "path": str(item.get("path") or ""), "group": group}

    for index, raw in enumerate(groups if isinstance(groups, (list, tuple)) else []):
        if not isinstance(raw, Mapping):
            continue
        label = chr(ord("A") + index) if index < 26 else str(index + 1)
        primary = raw.get("reference") if isinstance(raw.get("reference"), Mapping) else {}
        quoted = str(primary.get("text") or "")
        refs.append({"type": "quote", "group": label,
                     "sha256": hashlib.sha256(quoted.encode("utf-8")).hexdigest() if quoted else ""})
        refs += [r for r in (file_ref(a, label) for a in raw.get("attachments") or []) if r]
    refs += [r for r in (file_ref(a, "") for a in (attachments if isinstance(attachments, (list, tuple)) else [])) if r]
    return refs


def rehydrate(conversation: Any, current_text: str, present: Iterable[str] = (),
              limit: int = REHYDRATE_MAX_CHARS) -> list[dict[str, Any]]:
    """Earlier TEXT files the person names again in this message.

    Re-read from JARVIS's own snapshot - only when the file's digest still
    matches what was attached - and returned as data with its provenance: the
    turn it came with and the group that owned it. A snapshot that is gone
    (a restart, a new session) or has changed is returned WITHOUT content and
    marked ``available: false``, so the call knows only the file's identity and
    can say so - it is never re-read from elsewhere and never guessed. A file
    already attached to this turn, or an image, is not returned.
    """
    state = _as_state(conversation)
    folded = _fold(current_text)
    present = {_fold(name) for name in present}
    found: dict[str, dict[str, Any]] = {}
    for turn in state.turns:
        for ref in turn.refs:
            name = str(ref.get("name") or "")
            if (ref.get("type") != "file" or ref.get("kind") != "text" or len(name) < 3
                    or _fold(name) in present or _fold(name) not in folded):
                continue
            path = Path(str(ref.get("path") or ""))
            try:
                data = path.read_bytes() if str(ref.get("path") or "") else b""
            except OSError:
                data = b""
            entry = {"name": name, "from_turn": turn.seq, "group": str(ref.get("group") or "")}
            if not data or hashlib.sha256(data).hexdigest() != ref.get("sha256"):
                # Gone or changed: its identity is known, its content is not.
                found[_fold(name)] = {**entry, "available": False,
                                      "note": "The content of this file is no longer available to JARVIS; only its name is known."}
                continue
            text = data.decode("utf-8", errors="replace")
            found[_fold(name)] = {**entry, "available": True, "file_text": _clip(text, limit),
                                  "truncated": len(text) > limit}
    return list(found.values())


__all__ = [
    "CONTEXT_TOKEN_CAP", "COMPACTED_MAX", "CUT_REASONS", "DEFAULT_PROFILE", "ESTIMATOR", "PINS_SHOWN_MAX",
    "RAW_TURNS_MAX", "REPLY_MAX_CHARS", "TURN_MAX_CHARS", "CharEstimator", "Compacted", "ContextPlan",
    "ContextProfile", "ConversationState", "ConversationStore", "CorruptConversation", "Pin", "Title",
    "TokenEstimator", "Turn", "append_turn", "argv_chars", "assemble_context", "context_fields",
    "context_profile_of", "cut_reply", "digest", "json_text", "load_state", "name_titles", "pin_candidates",
    "plain_reply", "rehydrate", "rename_title", "split_cut", "state_from_dict", "task_title", "turn_refs",
    "update_pins",
]
