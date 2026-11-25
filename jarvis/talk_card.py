"""Approval card state machine (TALKCARD1, design section 7).

A card is bound to the digest of exactly what is approved, is single-use and
expires; the first valid tap is final. Pure: collaborators (random_bytes) are
injected callables, nothing here does I/O, and every state change comes back
as a log record. Nothing in the running app imports this module; it grants
nothing by itself.

Canonical form (input of the sha256 digest), all strings NFC-normalised and
then encoded as exact UTF-8 bytes (no trimming, no case folding):
    field(b)  = len(b) as 8-byte big-endian unsigned + b
    canonical = field(DIGEST_TAG) + field(text) + field(action_class)
                + len(cited) as 8-byte big-endian unsigned
                + field(cited[0]) + field(cited[1]) + ...
Cited turn ids keep their order. NFC-equivalent spellings give one digest;
any other change (one character, whitespace, a look-alike) gives another.

Sequence: taps are stamped by the single inbox sequencer in arrival order, so
the tap with the lower sequence is processed first and wins; any later tap on
the same card is REFUSED "already resolved".
"""

import hashlib
import hmac
import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace

DIGEST_TAG = "jarvis.talk_card.v1"
MIN_RANDOM_BYTES = 16
MAX_TTL_SECONDS = 24 * 3600.0
MAX_TEXT_CHARS = 8000
MAX_FIELD_CHARS = 128
MAX_CITED = 64
MAX_SEQUENCE = 2**63
CHOICES = ("approve", "reject")
VIEWS = ("pc", "telegram")
ID_PREFIX = "card-"

PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
CREATED = "CREATED"
REFUSED = "REFUSED"
REBUILT = "REBUILT"

# Default-ignorable letters/marks that render as nothing (category alone misses them).
_INVISIBLE = frozenset(
    [0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x17B4, 0x17B5]
    + list(range(0x180B, 0x1810))
    + list(range(0xFE00, 0xFE10))
    + list(range(0xE0100, 0xE01F0))
)


@dataclass(frozen=True)
class Card:
    card_id: str
    digest: str
    created_at: float
    expires_at: float
    status: str = PENDING


@dataclass(frozen=True)
class Tap:
    card_id: object
    choice: object
    view: object
    sequence: object
    time: object


@dataclass(frozen=True)
class Outcome:
    verdict: str
    reason: str = ""
    card: object = None
    record: object = None
    cards: object = None

    @property
    def approved(self):
        return self.verdict == APPROVED


def _refused(reason, cards=None, card=None, record=None):
    return Outcome(REFUSED, reason, card, record, cards)


def _num(value):
    """Finite float from a real int/float (bool, str, NaN, inf, overflow -> None)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except OverflowError:
        return None
    return out if math.isfinite(out) else None


def _visible(value, limit, allow_newline=False):
    """NFC str of at most limit chars with no control/format/invisible char, else None."""
    if type(value) is not str or not value or len(value) > limit:
        return None
    for ch in value:
        if allow_newline and ch in "\n\t":
            continue
        if unicodedata.category(ch).startswith("C") or ord(ch) in _INVISIBLE:
            return None
    return unicodedata.normalize("NFC", value)


def _field(text):
    data = text.encode("utf-8")
    return len(data).to_bytes(8, "big") + data


def _digest(text, action_class, cited):
    """sha256 hex of the canonical form, or None when any part is invalid."""
    text = _visible(text, MAX_TEXT_CHARS, allow_newline=True)
    action_class = _visible(action_class, MAX_FIELD_CHARS)
    if text is None or action_class is None or type(cited) not in (list, tuple):
        return None
    if len(cited) > MAX_CITED:
        return None
    ids = [_visible(c, MAX_FIELD_CHARS) for c in cited]
    if any(i is None for i in ids):
        return None
    blob = _field(DIGEST_TAG) + _field(text) + _field(action_class)
    blob += len(ids).to_bytes(8, "big") + b"".join(_field(i) for i in ids)
    return hashlib.sha256(blob).hexdigest()


def _well_formed_id(card_id):
    if type(card_id) is not str or len(card_id) != len(ID_PREFIX) + 32:
        return False
    tail = card_id[len(ID_PREFIX):]
    return card_id.startswith(ID_PREFIX) and all(c in "0123456789abcdef" for c in tail)


def _cards(cards):
    if not isinstance(cards, Mapping):
        return None
    out = dict(cards)
    if not all(isinstance(c, Card) and c.card_id == k for k, c in out.items()):
        return None
    return out


def create(text, action_class, cited, now, ttl, random_bytes, *, cards):
    """New PENDING card bound to digest(text, action_class, cited); CREATED or REFUSED."""
    try:
        state = _cards(cards)
        if state is None:
            return _refused("malformed card state")
        now_f, ttl_f = _num(now), _num(ttl)
        if now_f is None:
            return _refused("invalid time")
        if ttl_f is None or ttl_f <= 0 or ttl_f > MAX_TTL_SECONDS:
            return _refused("invalid ttl")
        digest = _digest(text, action_class, cited)
        if digest is None:
            return _refused("invalid card content")
        raw = random_bytes(MIN_RANDOM_BYTES)
        if type(raw) is not bytes or len(raw) < MIN_RANDOM_BYTES:
            return _refused("random bytes too short")
        card_id = ID_PREFIX + raw[:MIN_RANDOM_BYTES].hex()
        if card_id in state:
            return _refused("card id already exists")
        expires_at = now_f + ttl_f
        # same predicate as rebuild(), so float rounding never yields an unreplayable card
        if not math.isfinite(expires_at) or not 0 < expires_at - now_f <= MAX_TTL_SECONDS:
            return _refused("invalid ttl")
        card = Card(card_id, digest, now_f, expires_at)
        record = {"event": "created", "card_id": card_id, "digest": digest,
                  "created_at": now_f, "expires_at": expires_at}
        state[card_id] = card
        return Outcome(CREATED, "", card, record, state)
    except Exception:  # noqa: BLE001 - fail closed, RecursionError included
        return _refused("internal error")


def resolve(cards, tap):
    """Apply one tap: APPROVED / REJECTED, or REFUSED with a reason.

    Only a Tap resolves a card. A tap at or after expiry marks the card EXPIRED
    (counts as rejection) and is REFUSED "card expired"; that change still
    carries a record and the new cards.
    """
    try:
        state = _cards(cards)
        if state is None:
            return _refused("malformed card state")
        if not isinstance(tap, Tap):
            return _refused("not a tap record")
        if not _well_formed_id(tap.card_id):
            return _refused("malformed card id")
        card = state.get(tap.card_id)
        if card is None:
            return _refused("unknown card")
        if type(tap.choice) is not str or tap.choice not in CHOICES:
            return _refused("invalid choice")
        if type(tap.view) is not str or tap.view not in VIEWS:
            return _refused("invalid view")
        seq = tap.sequence
        if type(seq) is not int or not 0 <= seq < MAX_SEQUENCE:
            return _refused("invalid sequence")
        when = _num(tap.time)
        if when is None:
            return _refused("invalid time")
        if card.status != PENDING:
            return _refused("already resolved", card=card)
        if when < card.created_at:
            return _refused("tap before creation", card=card)
        if when >= card.expires_at:
            status = EXPIRED
        else:
            status = APPROVED if tap.choice == "approve" else REJECTED
        new = replace(card, status=status)
        record = {"event": "tap", "card_id": card.card_id, "choice": tap.choice,
                  "view": tap.view, "sequence": seq, "time": when, "status": status}
        state[card.card_id] = new
        if status == EXPIRED:
            return _refused("card expired", state, new, record)
        return Outcome(status, "", new, record, state)
    except Exception:  # noqa: BLE001 - fail closed, RecursionError included
        return _refused("internal error")


def verify(card, text, action_class, cited):
    """True only for an APPROVED card whose digest matches this exact content."""
    try:
        if not isinstance(card, Card) or card.status != APPROVED:
            return False
        digest = _digest(text, action_class, cited)
        if digest is None or type(card.digest) is not str:
            return False
        return hmac.compare_digest(card.digest, digest)
    except Exception:  # noqa: BLE001 - non-ASCII forged digest raises TypeError
        return False


def _created(record, state):
    """Card from a 'created' record, or None when it is malformed or a duplicate."""
    if set(record) != {"event", "card_id", "digest", "created_at", "expires_at"}:
        return None
    card_id, digest = record["card_id"], record["digest"]
    created, expires = _num(record["created_at"]), _num(record["expires_at"])
    if not _well_formed_id(card_id) or card_id in state or created is None or expires is None:
        return None
    if type(digest) is not str or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None
    if not 0 < expires - created <= MAX_TTL_SECONDS:
        return None
    return Card(card_id, digest, created, expires)


def rebuild(records):
    """Cards rebuilt from the log (e.g. after a restart): REBUILT, or REFUSED "corrupt log".

    Tap records are replayed through resolve() and must reproduce themselves
    exactly, so a replayed or forged tap cannot resolve a card twice.
    """
    try:
        if type(records) not in (list, tuple):
            return _refused("corrupt log")
        state = {}
        for record in records:
            if not isinstance(record, Mapping):
                return _refused("corrupt log")
            record = dict(record)
            if record.get("event") == "created":
                card = _created(record, state)
                if card is None:
                    return _refused("corrupt log")
                state[card.card_id] = card
            elif record.get("event") == "tap" and len(record) == 7:
                tap = Tap(record.get("card_id"), record.get("choice"), record.get("view"),
                          record.get("sequence"), record.get("time"))
                out = resolve(state, tap)
                if out.record != record:
                    return _refused("corrupt log")
                state = out.cards
            else:
                return _refused("corrupt log")
        return Outcome(REBUILT, "", None, None, state)
    except Exception:  # noqa: BLE001 - fail closed, RecursionError included
        return _refused("corrupt log")
