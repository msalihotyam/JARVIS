"""Last filter before any text leaves for Telegram (TALKREDACT2).

Whole-message rule: if the text holds ANY secret marker, the outbound copy is
replaced whole by ``FIXED_LINE``; otherwise the text goes out byte-identical
(the same object). No partial masking, no format parsing: every masking design
leaked a new shape (quotes, nested objects, multi-line values, comments).
Over-masking is accepted, leaking is not.

Markers are detected on a folded copy only (zero-width/bidi removed, NFKD,
combining marks dropped, look-alike letters/colons mapped, casefold):
a secret word followed anywhere on its line by ``=``, ``:`` (also ``=>`` and a
quote-colon) or ``%3A``/``%3D`` (or by one at the start of the next non-blank
line); Telegram bot tokens (also split by whitespace runs or ``%3A``); sk- and
GitHub keys; JWTs; any PEM BEGIN line; Bearer tokens; AWS AKIA/ASIA ids; Google
AIza keys; and anything ``jarvis.contracts.redact_credentials`` would change.

Pure: no I/O, no network, deterministic, linear time. A non-str input, a
too-deep input or any detector exception gives a typed Refusal, never the text.
"""

from dataclasses import dataclass
import re
import unicodedata

from jarvis.contracts import redact_credentials

FIXED_LINE = "[gizli i\u00e7erik var; PC'de bak]"


@dataclass(frozen=True)
class RedactResult:
    text: str
    flagged: bool
    kinds: tuple


@dataclass(frozen=True)
class Refusal:
    reason: str  # input_not_text | input_too_deep | redaction_failed (callers treat as flagged)


# Default-ignorable code points (zero-width, bidi, fillers, variation selectors, tags).
_IGNORABLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
    "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufffb\U00013430-\U0001343f"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
# Combining marks left after NFKD (p\u0336assword, pa\u0301ssword, \u015fifre -> sifre).
_MARKS = re.compile("[\u0300-\u036f\u0483-\u0489\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]")
# Cyrillic, Greek, Armenian, small-capital look-alikes plus colon/equals look-alikes
# that NFKD keeps apart. shortcut: a hand list, not the Unicode confusables table;
# switch to the confusables data if a new look-alike slips by.
_LOOKALIKE = str.maketrans(dict(zip(
    "\u0430\u0435\u043e\u0440\u0441\u0445\u0443\u043a\u043c\u0442\u043d\u0432\u0456\u0458\u0455\u0501\u051d\u04bb\u051b" "\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425\u0423\u0406\u0408\u0405" "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7\u03bf\u03c1\u03c4\u03bd\u03b9\u03ba\u03c9\u03c5\u03b1\u03b5"
    "\u0585\u057d\u0570\u1d00\u0299\u1d04\u1d05\u1d07\ua730\u0262\u029c\u026a\u1d0a\u1d0b\u029f\u1d0d\u0274\u1d0f\u1d18\u0280\ua731\u1d1b\u1d1c\u1d20\u1d21\u028f\u1d22\u0131" "\u2236\ua789\u0589\u02f8\u02d0\u05c3\ua78a" "\u2010\u2011\u2012\u2013\u2014\u2015\u2212",
    "aeopcxykmtHBijsdwhq" "ABEKMHOPCTXYIJS" "ABEZHIKMNOPTYXoptvikwuae"
    "ouhabcdefghijklmnoprstuvwyzi" "::::::=" "-------",
)))

_WORD = re.compile(
    r"password|passwd|passphrase|pwd|token|secret|credential|api[_. -]?key"
    r"|private[_. -]?key|access[_. -]?key|authorization|cookie|sifre|parola"
)
# A quote-comma also counts (["password", "v"]); over-masks prose like '"the token", he said'.
_SEP = re.compile(r"[:=]|%3[ad]|[\"'`]\s*,")
_NEXT_SEP = re.compile(r"\s*(?:[:=]|%3[ad]|,)")
# Split id is bounded (a start at every digit of "1 1 1 ..."); classes are disjoint
# from whitespace, so each attempt is bounded and the scan stays linear.
_LOWER_SHAPES = (
    ("telegram_bot_token", re.compile(r"(?<!\d)\d{8,}\s*(?::|%3a)\s*[a-z0-9_-]{35,}")),
    ("telegram_bot_token", re.compile(
        r"(?<!\d)(?:\d\s*){7,15}\d\s*(?::|%3a)(?:\s*[a-z0-9_-]){35,}")),
    ("openai_key", re.compile(r"(?<![a-z0-9])sk-[a-z0-9_-]{12,}")),
    ("github_token", re.compile(r"(?<![a-z0-9])(?:gh[pousr]_[a-z0-9]{16,}|github_pat_[a-z0-9_]{20,})")),
    # Start only at a run start and skip to its first "eyj" (a later one cannot fit
    # better); possessive scans keep it one pass per run, same matches as unanchored.
    ("jwt", re.compile(
        r"(?<![a-z0-9_-])(?:(?!eyj)[a-z0-9_-])*+eyj[a-z0-9_-]{2,}+\.eyj[a-z0-9_-]{2,}+\.")),
    ("pem", re.compile(r"-----\s*begin")),
    ("bearer", re.compile(r"(?<![a-z0-9])bearer\s+[a-z0-9._~+/-]{16,}")),
)
# Case matters for these prefixes ("asia" is a word); run on the folded, uncased copy.
_CASED_SHAPES = (
    ("aws_key", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
)


def _fold(text):
    text = unicodedata.normalize("NFKD", _IGNORABLE.sub("", text))
    return _IGNORABLE.sub("", _MARKS.sub("", text).translate(_LOOKALIKE))


def _secret_word(lower):
    # Only the first word of a line matters: it has the longest tail, so later ones
    # on that line cannot add a separator (one pass, linear).
    pos, size = 0, len(lower)
    while True:
        match = _WORD.search(lower, pos)
        if match is None:
            return False
        end = lower.find("\n", match.end())
        end = size if end < 0 else end
        if _SEP.search(lower, match.end(), end) or _NEXT_SEP.match(lower, end):
            return True
        pos = end + 1


def _kinds(text):
    folded = _fold(text)
    lower = folded.casefold()
    found = {kind for kind, shape in _LOWER_SHAPES if shape.search(lower)}
    found.update(kind for kind, shape in _CASED_SHAPES if shape.search(folded))
    if _secret_word(lower):
        found.add("secret_word")
    if redact_credentials(text)[0] != text or (
            folded != text and redact_credentials(folded)[0] != folded):
        found.add("credential")
    return tuple(sorted(found))


def redact(text):
    if not isinstance(text, str):
        return Refusal("input_not_text")
    try:
        kinds = _kinds(text)
    except RecursionError:
        return Refusal("input_too_deep")
    except Exception:  # fail closed: a broken detector never lets the text out
        return Refusal("redaction_failed")
    if not kinds:
        return RedactResult(text, False, ())
    return RedactResult(FIXED_LINE, True, kinds)
