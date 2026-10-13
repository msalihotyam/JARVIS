"""Deterministic 'never stored' filter for skill and experience text (V2.2 S1).

Core is the authority here (S7): every text field of a skill draft, a skill
version or an experience note is checked by plain rules - never by a model -
before it may be stored, and the store checks it again on every write. A skill
is a procedure, never an authority: this module only decides whether text may
be kept; it grants, implies or records no approval, trust, risk or permission.

A field is refused when any of these hold:

* ``memory_filter.classify_candidate_text`` refuses it (secrets, payment cards,
  IBANs, government ids, phone numbers, addresses, health data, ...);
* ``contracts.redact_credentials`` or ``contracts.redact_local_paths`` would
  change it. The text is refused, never silently redacted: the user edits the
  draft. Both run on the raw text AND on the normalised form memory_filter
  scans, because the two spellings can disagree (a combining mark after a key
  hides it from the NFKC scan, a full-width path hides from the raw one);
* it names JARVIS' own protected state (``data\\``, ``~\\.jarvis\\``,
  ``jarvis.db``) - a procedure never needs to point at them;
* it holds an e-mail address (personal data under S7; SKDRAFT turns run
  addresses into ``{email}`` inputs instead);
* the source classification is SENSITIVE or SECRET, or is not a known one;
* a field name is not an id (``[a-z0-9_-]{1,64}``) or itself matches a
  never-stored category (``field_name``; the name is never echoed);
* anything goes wrong while deciding (``filter_error``): fail closed.

Every text check, memory_filter included, runs on the raw text AND on a
cleaned form: NFKC with every invisible character removed - every format (Cf)
character plus the D17 list (every Default_Ignorable_Code_Point as explicit,
Unicode-version-independent ranges, plus U+2800) - so an invisible character
inside a number, key or path cannot hide it. Removal can only expose a match,
never hide one. A text longer than memory_filter's limit is refused before
any normalisation (NFKC can expand a short input many times).

``preview_text`` returns the text unchanged when it would be allowed and a
fixed ``[HIDDEN: <category>]`` marker otherwise - never a partly masked copy.
It is display-only and must never be stored. Verdict reasons name the
category only and never echo the checked text.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .contracts import (
    DataClassification,
    redact_credentials,
    redact_local_paths,
)
from .memory_filter import MAX_CANDIDATE_CHARS, classify_candidate_text

#: Upper bound on fields per call; a skill has far fewer (SKTYPES caps steps,
#: inputs and checks), so a longer list is a caller bug and is refused.
MAX_FIELDS = 256
#: Field names are caller-chosen ids such as ``steps-3-text``.
MAX_FIELD_NAME_CHARS = 64
_FIELD_NAME_PATTERN = re.compile(r"[a-z0-9_-]{1,64}")

_REFUSED_CLASSIFICATIONS = frozenset({DataClassification.SENSITIVE, DataClassification.SECRET})

# Invisible characters removed for the cleaned form (D17): every format (Cf)
# character, plus every Default_Ignorable_Code_Point (Unicode
# DerivedCoreProperties) as an explicit, version-independent list - soft
# hyphen, CGJ, ALM, Hangul fillers, Khmer inherent vowels, Mongolian free
# variation selectors and vowel separator, zero-width and bidi controls,
# invisible operators, variation selectors, BOM, the TAG block - plus U+2800.
# Inclusive ranges, built from code points so the source holds no literal
# invisible characters.
_D17_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_EXTRA_INVISIBLE = frozenset(
    code_point for first, last in _D17_INVISIBLE_RANGES for code_point in range(first, last + 1)
)

# JARVIS' own state: the data folder, the per-user ~/.jarvis folder and the
# database file. memory_filter refuses these only when content is quoted;
# a skill step refuses any mention.
_PROTECTED_STATE_PATTERN = re.compile(
    r"(?<![\w.])data[\\/]|(?<!\w)\.jarvis(?!\w)|(?<![\w.])jarvis\.db(?!\w)",
    re.IGNORECASE,
)

# E-mail addresses (S7 personal data): a local-part character, "@" and a
# domain character or an IP literal. Deliberately loose - TR/EN, non-ASCII,
# single-label hosts (localhost) and any local-part length all match - and
# linear: one fixed-width window per "@".
_EMAIL_PATTERN = re.compile(r"[\w.+%-]@(?:[\w-]|\[[^\]\s])")

#: preview_text shows this instead of any text the filter refuses.
HIDDEN_MARKER = "[HIDDEN: {category}]"


@dataclass(frozen=True)
class FieldVerdict:
    """Outcome of ``check_fields``; ``field`` names the first refused field."""

    allowed: bool
    field: str | None
    category: str | None
    reason: str


_ALLOWED = FieldVerdict(True, None, None, "no never-stored category matched")


def _error(field: str | None = None) -> FieldVerdict:
    return FieldVerdict(False, field, "filter_error", "filter failed; refused")


def _drop_invisible(text: str) -> str:
    return "".join(
        char
        for char in text
        if unicodedata.category(char) != "Cf" and ord(char) not in _EXTRA_INVISIBLE
    )


def _normalise(text: str) -> str:
    """The cleaned form: NFKC with every invisible character removed."""
    return _drop_invisible(unicodedata.normalize("NFKC", _drop_invisible(text)))


def _credential_redaction_changes(text: str) -> bool:
    redacted, count = redact_credentials(text)
    return count > 0 or redacted != text


def _local_path_redaction_changes(text: str) -> bool:
    return redact_local_paths(text) != text


def _check_classification(classification: object) -> FieldVerdict | None:
    if classification is None:
        return None
    try:
        value = DataClassification(classification)
    except ValueError:
        return FieldVerdict(False, None, "classification", "unknown source classification; refused")
    if value in _REFUSED_CLASSIFICATIONS:
        return FieldVerdict(
            False, None, "classification", f"source is classified {value.value}; never stored"
        )
    return None


def _check_text(field: str | None, text: str) -> FieldVerdict | None:
    # Length first, before any normalisation: NFKC can expand a character
    # 18-fold, so an over-long text is refused unread.
    if len(text) > MAX_CANDIDATE_CHARS:
        return FieldVerdict(
            False, field, "too_long", f"longer than {MAX_CANDIDATE_CHARS} characters"
        )
    cleaned = _normalise(text)
    for form in (text, cleaned):
        verdict = classify_candidate_text(form)
        if not verdict.allowed:
            return FieldVerdict(False, field, verdict.category, verdict.reason)
    for form in (text, cleaned):
        if _credential_redaction_changes(form):
            return FieldVerdict(
                False, field, "credential", "credential redaction would change the text"
            )
        if _local_path_redaction_changes(form):
            return FieldVerdict(False, field, "local_path", "contains an absolute local path")
        if _PROTECTED_STATE_PATTERN.search(form):
            return FieldVerdict(False, field, "protected_path", "names protected JARVIS state")
        if _EMAIL_PATTERN.search(form):
            return FieldVerdict(False, field, "email", "contains an e-mail address")
    return None


def _check_name(name: str) -> FieldVerdict | None:
    # The name is never echoed (field=None): a refused name may itself be the leak.
    if _FIELD_NAME_PATTERN.fullmatch(name) is None:
        return FieldVerdict(False, None, "field_name", "field name is not an id; refused")
    if _check_text(None, name) is not None:
        return FieldVerdict(
            False, None, "field_name", "field name matches a never-stored category; refused"
        )
    return None


def _check_all(
    fields: Iterable[tuple[str, str]], source_classification: object
) -> FieldVerdict:
    refused = _check_classification(source_classification)
    if refused is not None:
        return refused
    if isinstance(fields, (str, bytes, bytearray, Mapping)):
        raise TypeError("fields must be an iterable of (field_name, text) pairs")
    for position, item in enumerate(fields):
        if position >= MAX_FIELDS:
            return FieldVerdict(False, None, "too_many_fields", f"more than {MAX_FIELDS} fields")
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("each field must be a (field_name, text) tuple")
        name, text = item
        if not isinstance(name, str):
            raise TypeError("field name must be a str")
        refused = _check_name(name)
        if refused is not None:
            return refused
        if not isinstance(text, str):
            return _error(name)
        refused = _check_text(name, text)
        if refused is not None:
            return refused
    return _ALLOWED


def check_fields(
    fields: Iterable[tuple[str, str]],
    source_classification: DataClassification | str | None = None,
) -> FieldVerdict:
    """Decide whether every field may be stored. Fail-closed.

    ``fields`` is an iterable of ``(field_name, text)`` tuples; the first
    refused field decides the verdict. ``source_classification`` is the
    classification of the material the text came from (``None`` = unknown
    origin, judged on the text alone).
    """
    try:
        return _check_all(fields, source_classification)
    except Exception:  # noqa: BLE001 - any failure refuses the fields (S7)
        return _error()


def preview_text(text: str) -> str:
    """What a refused draft may show for ``text``. Never store it.

    Text the filter would allow comes back unchanged; anything it refuses -
    and any error - comes back as the fixed ``[HIDDEN: <category>]`` marker,
    never as a partly masked copy.
    """
    try:
        if not isinstance(text, str):
            return HIDDEN_MARKER.format(category="filter_error")
        refused = _check_text(None, text)
        if refused is None:
            return text
        return HIDDEN_MARKER.format(category=refused.category or "filter_error")
    except Exception:  # noqa: BLE001 - a preview never falls back to the raw text
        return HIDDEN_MARKER.format(category="filter_error")
