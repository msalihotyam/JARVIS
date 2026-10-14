"""V2.1 memory lifetime policy (I19) - pure, no DB.

Every new record gets a lifetime: SESSION, PHASE or PERMANENT. The text sets
the ceiling and a request can only lower it, never raise it: a transient
statement ("bugün", "for now") is SESSION whatever was requested.

Markers match whole words on the memory guard's Turkish fold (I/İ/ı -> i,
marks dropped, so 'BUGÜN', 'Bugün' and 'bugun' are one marker). A suffixed
form is not the marker: 'bugünkü' (today's), 'şimdilikte' and 'todays' do not
match; only the suffixes listed below ('bu oturumda', 'bu sohbette',
'şu anlık', 'bu aşamada', 'bu sprintte') do.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import Enum

from .memory_guard import _fold


class Lifetime(str, Enum):
    SESSION = "session"
    PHASE = "phase"
    PERMANENT = "permanent"


SESSION_TTL = timedelta(days=7)
PHASE_TTL = timedelta(days=90)

_RANK = {Lifetime.SESSION: 0, Lifetime.PHASE: 1, Lifetime.PERMANENT: 2}


def _markers(*phrases: str) -> re.Pattern[str]:
    # Folded like the text, so the source can keep its Turkish letters; any whitespace run between words.
    return re.compile(r"\b(?:" + "|".join(_fold(p).replace(" ", r"\s+") for p in phrases) + r")\b")


# ponytail: keyword list, not NLP; a marker in a quote or a negation still counts (fail closed: shorter life).
_TRANSIENT = _markers(
    "bugün", "bu akşam", "şimdilik", "geçici olarak", "bu oturum(?:da)?",
    "bu sohbet(?:te)?", "şu an(?:lık)?",
    "for now", "today", "tonight", "this session", "this chat", "temporarily", "right now",
)
_PHASE = _markers(
    "bu sprint(?:te)?", "bu aşama(?:da)?", "bu milestone", "sürüme kadar",
    "this sprint", "this phase", "this milestone", "until (?:the )?release",
)


def is_transient(text: str) -> bool:
    return bool(_TRANSIENT.search(_fold(str(text))))


def is_phase(text: str) -> bool:
    return bool(_PHASE.search(_fold(str(text))))


def decide_lifetime(text: str, *, requested: Lifetime | str | None = None) -> Lifetime:
    """The text's ceiling, lowered to ``requested`` when that is shorter.
    An unknown ``requested`` raises ValueError (fail closed)."""
    if is_transient(text):
        ceiling = Lifetime.SESSION
    elif is_phase(text):
        ceiling = Lifetime.PHASE
    else:
        ceiling = Lifetime.PERMANENT
    if requested is None:
        return ceiling
    return min(ceiling, Lifetime(requested), key=_RANK.__getitem__)


def expires_for(lifetime: Lifetime | str, now: datetime) -> datetime | None:
    lifetime = Lifetime(lifetime)
    if lifetime is Lifetime.SESSION:
        return now + SESSION_TTL
    if lifetime is Lifetime.PHASE:
        return now + PHASE_TTL
    return None
