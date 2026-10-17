"""V2.2 skill records: a skill is a procedure, never an authority.

A ``Skill`` says HOW a job was done before: numbered steps with action text
and an optional capability hint, the inputs those steps name, and checks. It
never says WHETHER anything may run. No record here has an approval, grant,
trust, permission, auto-run, risk, side-effect or remember field, and
``from_dict`` refuses such a key anywhere in its input (``AuthorityKeyError``).
When a skill is used, each step becomes an ordinary chain step and Core policy
and approval decide its risk, side effect and approval exactly as for a step
the user typed; nothing here can lower a tier, skip an approval or pre-approve.
Status and health are not stored on a skill: they are computed from its events
(``SkillEventKind``) elsewhere.

Every record is a frozen dataclass validated at construction, so an instance
is always well formed. ``to_dict`` gives plain JSON data and ``from_dict`` is
its strict inverse: exact key set (an unknown or a missing key is refused),
exact types (no coercion), bounded sizes, ids ``[a-z0-9_-]{1,64}``, a project
scope iff a project id (Core's project-id grammar), and at most
``MAX_JSON_BYTES`` of compact UTF-8 JSON per record. Every refusal is a
``SkillRecordError`` (a ``ValueError``). Text in these records is untrusted
data, never instructions.

Pure: stdlib only, no I/O, no import of other JARVIS modules.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Self

MAX_STEPS = 30
NAME_MAX = 80
#: Step action and check text, summaries, descriptions, reasons, lessons, errors.
TEXT_MAX = 500
#: A run's task text and the user's correction of its result.
LONG_TEXT_MAX = 2_000
MAX_INPUTS = 16
MAX_CHECKS = 16
MAX_SIGNATURE_TOKENS = 32
TOKEN_MAX = 40
MAX_VERSION = 1_000_000
MAX_COUNT = 1_000_000_000
#: Ceiling for one record's compact UTF-8 JSON (``to_dict``).
MAX_JSON_BYTES = 16 * 1024

#: Every id (skill, draft, run, experience), input name and capability hint.
#: Use with ``fullmatch``: ``$`` would accept a final newline.
ID_PATTERN = re.compile(r"[a-z0-9_-]{1,64}")
#: Project ids: a copy of Core's ``contracts.PROJECT_ID_PATTERN`` (a 3-64
#: character lowercase slug), so a skill scope or a run names only a project
#: Core could have. Copied, not imported, to keep this module pure; a test
#: cross-checks it against Core's ``validate_project_id``. Use with ``fullmatch``.
PROJECT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
#: The longest key ``from_dict`` reads. A longer key is refused before any
#: normalisation (every real field name is far shorter).
KEY_MAX = 64
#: A key matching this anywhere in ``from_dict`` input is refused, ignoring case.
FORBIDDEN_KEY_PATTERN = re.compile(
    r"approv|grant|trust|permission|auto_run|pre_approved|risk|side_effect|remember",
    re.IGNORECASE,
)
# The same words with everything but a-z and 0-9 squeezed out of the key,
# so auto-run, autoRun and "side effect" are refused too.
_FORBIDDEN_SQUEEZED = re.compile(
    r"approv|grant|trust|permission|autorun|preapproved|risk|sideeffect|remember"
)
_SEPARATORS = re.compile("[^0-9a-z]+")
# Text may not hide anything: a shown draft must read exactly like its text.
# Refused in every text field: these Unicode categories (controls, surrogates,
# invisible format characters such as zero-width, BOM, soft hyphen, TAG and
# bidi characters, private use, unassigned code points and noncharacters, line
# and paragraph separators) ...
_BAD_TEXT_CATEGORIES = frozenset({"Cc", "Cs", "Cf", "Co", "Cn", "Zl", "Zp"})
# ... except tab, newline and carriage return; and, whatever the Unicode
# database of the running Python says, these explicit ranges (C0/C1 controls,
# soft hyphen, ALM, zero-width and LRM/RLM, separators and bidi embeddings,
# bidi isolates, BOM, surrogates, TAG characters). Code points rather than
# escape sequences keep this source plain ASCII.
_ALLOWED_TEXT_CONTROLS = frozenset(map(chr, (0x09, 0x0A, 0x0D)))
_BAD_TEXT_RANGES = (
    (0x00, 0x08),
    (0x0B, 0x0C),
    (0x0E, 0x1F),
    (0x7F, 0x9F),
    (0x00AD, 0x00AD),
    (0x061C, 0x061C),
    (0x200B, 0x200F),
    (0x2028, 0x202E),
    (0x2066, 0x2069),
    (0xFEFF, 0xFEFF),
    (0xD800, 0xDFFF),
    (0xE0000, 0xE007F),
)
# D17: every Default_Ignorable_Code_Point (Unicode DerivedCoreProperties) as an
# explicit list, so the refusal does not depend on the running Python's Unicode
# version, plus U+2800 (braille blank). These are refused too in every text
# field, in names and in task_signature tokens: variation selectors (enough to
# encode any byte), CGJ, Mongolian FVS, Khmer inherent vowels and Hangul fillers
# are category Mn or Lo, so the category check alone lets them hide a payload.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
    (0x2800, 0x2800),
)
_BAD_TEXT_CHAR = re.compile(
    "["
    + "".join(
        f"{chr(low)}-{chr(high)}" for low, high in _BAD_TEXT_RANGES + _INVISIBLE_RANGES
    )
    + "]"
)
# Folding a task word: dotted capital I and dotless i both become plain i.
_TURKISH_I = {0x0130: "i", 0x0131: "i"}
_TIMESTAMP_MAX = 40
# The deepest real record tree is four containers deep; the cap is generous.
_MAX_DEPTH = 8
_MAX_NODES = 4_096


class SkillRecordError(ValueError):
    """A skill record, or the data offered for one, was refused."""


class AuthorityKeyError(SkillRecordError):
    """The data carried an approval, trust, permission, risk-like key."""


def fold_token(token: str, *, identity: bool = False) -> str:
    """The one canonical form of a task word (``Experience.task_signature``).

    NFKC; dotted capital I (U+0130) and dotless i (U+0131) become ``i``;
    casefold; NFKD with every nonspacing mark (category Mn) dropped; NFC. So
    HAFTALIK, Haftalik and the Turkish dotless spelling all fold to
    ``haftalik``, and a fullwidth word folds to its plain form. Pure,
    deterministic and idempotent. Every consumer that stores or matches task
    words calls this, so a lesson is found under the same word it was stored
    under. Raises ``SkillRecordError`` for a non-string.

    ``identity=True`` (a skill's exact identity) folds only case, NFC/NFD and
    the Turkish I/i pairs: every mark stays, so a cedilla s, a dotted o or an
    accented "resume" is a different text than its plain spelling (CDXINT54F
    O1). It also skips the compatibility forms (NFC, lower), so a
    superscript, circled, ligature, roman-numeral or fullwidth character names
    a different object than its plain spelling.
    """
    if type(token) is not str:
        raise SkillRecordError("a task word must be a string")
    if identity:
        text = unicodedata.normalize("NFC", token).translate(_TURKISH_I).lower()
        return unicodedata.normalize("NFC", text)
    text = unicodedata.normalize("NFKC", token).translate(_TURKISH_I).casefold()
    kept = [c for c in unicodedata.normalize("NFKD", text) if unicodedata.category(c) != "Mn"]
    return unicodedata.normalize("NFC", "".join(kept))


class SkillScopeKind(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"


class SkillOrigin(str, Enum):
    TAUGHT_FROM_RUN = "taught_from_run"
    ADAPTED = "adapted"
    USER_EDIT = "user_edit"
    VERSION_AFTER_FAILURE = "version_after_failure"


class SkillEventKind(str, Enum):
    """What can happen to a skill; status and health are computed from these."""

    USED_SUCCESS = "used_success"
    USED_FAILURE = "used_failure"
    ENABLED = "enabled"
    DISABLED = "disabled"
    DELETED = "deleted"
    STALE_MARKED = "stale_marked"
    REVALIDATED = "revalidated"
    ROLLED_BACK = "rolled_back"
    DRAFT_APPROVED = "draft_approved"
    DRAFT_REJECTED = "draft_rejected"


class SkillStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    STALE = "stale"
    DELETED = "deleted"


class ExperienceKind(str, Enum):
    SUCCESS_PATH = "success_path"
    WRONG_TURN = "wrong_turn"
    AVOID = "avoid"
    RECOVERY = "recovery"
    BROKEN_WORKFLOW = "broken_workflow"


class RunOutcome(str, Enum):
    """How a finished run or one of its steps ended: the final ``RunStatus``
    and ``TaskStatus`` values. History only, never an approval."""

    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


_NEW_SKILL_ORIGINS = frozenset({SkillOrigin.TAUGHT_FROM_RUN, SkillOrigin.ADAPTED})


# -- checks used at construction ------------------------------------------


def _check_type(value: Any, expected: type, field: str) -> None:
    if type(value) is not expected:
        raise SkillRecordError(f"{field} must be a {expected.__name__}")


def _check_id(value: Any, field: str) -> None:
    if type(value) is not str or ID_PATTERN.fullmatch(value) is None:
        raise SkillRecordError(f"{field} must be 1-64 characters of a-z, 0-9, _ or -")


def _check_optional_id(value: Any, field: str) -> None:
    if value is not None:
        _check_id(value, field)


def _check_project_id(value: Any, field: str) -> None:
    if type(value) is not str or PROJECT_ID_PATTERN.fullmatch(value) is None:
        raise SkillRecordError(
            f"{field} must be a 3-64 character lowercase slug ([a-z0-9][a-z0-9-]{{2,63}})"
        )


def _check_int(value: Any, field: str, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise SkillRecordError(f"{field} must be an integer from {low} to {high}")


def _check_name(value: Any, field: str) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= NAME_MAX
        or value != value.strip()
        or not value.isprintable()
        or _BAD_TEXT_CHAR.search(value)
    ):
        raise SkillRecordError(
            f"{field} must be one line of 1-{NAME_MAX} printable characters "
            "without surrounding spaces"
        )


def _check_text(value: Any, field: str, limit: int, *, allow_empty: bool = False) -> None:
    if type(value) is not str:
        raise SkillRecordError(f"{field} must be a string")
    if len(value) > limit:
        raise SkillRecordError(f"{field} is longer than {limit} characters")
    if not allow_empty and not value.strip():
        raise SkillRecordError(f"{field} must not be empty")
    if _BAD_TEXT_CHAR.search(value) or any(
        unicodedata.category(char) in _BAD_TEXT_CATEGORIES
        and char not in _ALLOWED_TEXT_CONTROLS
        for char in value
    ):
        raise SkillRecordError(
            f"{field} contains a control, invisible, private-use or unassigned character"
        )


def _check_optional_text(value: Any, field: str, limit: int) -> None:
    if value is not None:
        _check_text(value, field, limit)


def _check_tuple(value: Any, field: str, item_type: type, low: int, high: int) -> None:
    if type(value) is not tuple:
        raise SkillRecordError(f"{field} must be a tuple")
    if not low <= len(value) <= high:
        raise SkillRecordError(f"{field} must hold {low} to {high} items")
    if any(type(item) is not item_type for item in value):
        raise SkillRecordError(f"{field} items must be {item_type.__name__}")


def _check_unique(values: tuple[Any, ...], field: str) -> None:
    if len(set(values)) != len(values):
        raise SkillRecordError(f"{field} repeats an item")


def _check_timestamp(value: Any, field: str) -> None:
    # One spelling only (``utc_now()``'s), so stamps compare as text.
    if type(value) is str and len(value) <= _TIMESTAMP_MAX:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if (
            parsed is not None
            and parsed.utcoffset() == timedelta(0)
            and parsed.isoformat() == value
        ):
            return
    raise SkillRecordError(f"{field} must be a UTC ISO-8601 timestamp as utc_now() writes it")


def _check_size(record: _Record) -> None:
    text = json.dumps(
        record.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise SkillRecordError(
            f"{type(record).__name__} is larger than {MAX_JSON_BYTES} bytes of JSON"
        )


def _check_procedure(record: Skill | SkillDraft) -> None:
    """The parts a saved skill and a draft share."""
    _check_name(record.name, "name")
    _check_text(record.summary, "summary", TEXT_MAX)
    _check_type(record.scope, SkillScope, "scope")
    _check_tuple(record.inputs, "inputs", SkillInput, 0, MAX_INPUTS)
    _check_tuple(record.steps, "steps", SkillStep, 1, MAX_STEPS)
    _check_tuple(record.checks, "checks", str, 0, MAX_CHECKS)
    for check in record.checks:
        _check_text(check, "checks item", TEXT_MAX)
    _check_type(record.provenance, SkillProvenance, "provenance")
    names = tuple(item.name for item in record.inputs)
    _check_unique(names, "inputs")
    for position, step in enumerate(record.steps, start=1):
        if step.index != position:
            raise SkillRecordError("steps must be numbered 1..n in order")
        if not set(step.inputs) <= set(names):
            raise SkillRecordError(f"step {position} names an undeclared input")


# -- parsing used by from_dict --------------------------------------------


def _is_forbidden_key(key: str) -> bool:
    folded = unicodedata.normalize("NFKC", key).casefold()
    return bool(
        FORBIDDEN_KEY_PATTERN.search(folded)
        or _FORBIDDEN_SQUEEZED.search(_SEPARATORS.sub("", folded))
    )


def _scan(data: Any) -> None:
    """Refuse an authority key ANYWHERE in ``data``, before anything else.

    Iterative and bounded: a deep, cyclic or huge structure is refused without
    recursion. Known and unknown branches are scanned alike, so a forbidden
    key is reported as such even where an unknown key would be refused anyway.
    """
    stack: list[tuple[Any, int]] = [(data, 1)]
    nodes = 1
    while stack:
        value, depth = stack.pop()
        if isinstance(value, Mapping):
            children = []
            if len(value) > _MAX_NODES:
                raise SkillRecordError("record data has too many elements")
            for key, item in value.items():
                if type(key) is not str:
                    raise SkillRecordError("record data keys must be strings")
                if len(key) > KEY_MAX:
                    # Before any normalisation: NFKC can expand a key 18-fold.
                    raise SkillRecordError(f"record data has a key longer than {KEY_MAX}")
                if _is_forbidden_key(key):
                    raise AuthorityKeyError(
                        "record data carries an approval, trust, permission, risk, "
                        "side-effect, auto-run or remember key; a skill is a "
                        "procedure, never an authority"
                    )
                children.append(item)
        elif isinstance(value, (list, tuple)):
            children = list(value[: _MAX_NODES + 1])
        else:
            continue
        if not children:
            continue
        nodes += len(children)
        if nodes > _MAX_NODES:
            raise SkillRecordError("record data has too many elements")
        if depth >= _MAX_DEPTH:
            raise SkillRecordError("record data is nested too deeply")
        stack.extend((child, depth + 1) for child in children)


def _object(data: Any, record_type: type, where: str) -> dict[str, Any]:
    if type(data) is not dict:
        raise SkillRecordError(f"{where} must be an object")
    expected = {item.name for item in fields(record_type)}
    if not set(data) <= expected:
        raise SkillRecordError(f"{where} has an unknown key")
    missing = expected - set(data)
    if missing:
        raise SkillRecordError(f"{where} is missing {', '.join(sorted(missing))}")
    return data


def _array(value: Any, where: str, limit: int) -> list[Any]:
    if type(value) is not list:
        raise SkillRecordError(f"{where} must be an array")
    if len(value) > limit:
        raise SkillRecordError(f"{where} holds more than {limit} items")
    return value


def _enum(enum_type: type[Enum], value: Any, where: str) -> Any:
    if type(value) is str:
        for member in enum_type:
            if member.value == value:
                return member
    allowed = ", ".join(member.value for member in enum_type)
    raise SkillRecordError(f"{where} must be one of {allowed}")


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, _Record):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


class _Record:
    """Plain-data conversion shared by every record (all frozen dataclasses)."""

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON data: enums as values, tuples as lists, records as objects."""
        return {item.name: _plain(getattr(self, item.name)) for item in fields(self)}

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        """The strict inverse of ``to_dict``; raises ``SkillRecordError``."""
        _scan(data)
        return cls._parse(data, cls.__name__)

    @classmethod
    def _parse(cls, data: Any, where: str) -> Self:
        # Only concrete records parse; the shared base refuses like any record.
        raise SkillRecordError(f"{where} is not a concrete skill record")


# -- records --------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillScope(_Record):
    """Where a skill or a lesson applies: everywhere, or one project only."""

    kind: SkillScopeKind
    project_id: str | None

    def __post_init__(self) -> None:
        _check_type(self.kind, SkillScopeKind, "scope.kind")
        if self.kind is SkillScopeKind.PROJECT:
            _check_project_id(self.project_id, "scope.project_id")
        elif self.project_id is not None:
            raise SkillRecordError("a global scope has no project_id")

    @classmethod
    def _parse(cls, data: Any, where: str) -> SkillScope:
        d = _object(data, cls, where)
        return cls(
            kind=_enum(SkillScopeKind, d["kind"], f"{where}.kind"),
            project_id=d["project_id"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillInput(_Record):
    """A named parameter the steps may refer to; a name, never a value."""

    name: str
    description: str
    required: bool

    def __post_init__(self) -> None:
        _check_id(self.name, "input.name")
        _check_text(self.description, "input.description", TEXT_MAX, allow_empty=True)
        _check_type(self.required, bool, "input.required")

    @classmethod
    def _parse(cls, data: Any, where: str) -> SkillInput:
        return cls(**_object(data, cls, where))


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillStep(_Record):
    """One numbered step (1-based): what to do in words, plus a capability hint.

    A step only PROPOSES. It has no risk, side-effect or approval field: Core
    decides all three when the step runs as an ordinary chain step, and may
    ignore the hint. ``inputs`` names the skill inputs the step uses.
    """

    index: int
    action: str
    capability: str | None
    inputs: tuple[str, ...]
    check: str | None

    def __post_init__(self) -> None:
        _check_int(self.index, "step.index", 1, MAX_STEPS)
        _check_text(self.action, "step.action", TEXT_MAX)
        _check_optional_id(self.capability, "step.capability")
        _check_tuple(self.inputs, "step.inputs", str, 0, MAX_INPUTS)
        for name in self.inputs:
            _check_id(name, "step.inputs item")
        _check_unique(self.inputs, "step.inputs")
        _check_optional_text(self.check, "step.check", TEXT_MAX)

    @classmethod
    def _parse(cls, data: Any, where: str) -> SkillStep:
        d = _object(data, cls, where)
        return cls(
            index=d["index"],
            action=d["action"],
            capability=d["capability"],
            inputs=tuple(_array(d["inputs"], f"{where}.inputs", MAX_INPUTS)),
            check=d["check"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillProvenance(_Record):
    """Where a skill version came from: lineage only, never a vouching.

    ``taught_from_run`` names its run and has no parent; every other origin
    names the parent skill version it was made from.
    """

    origin: SkillOrigin
    source_run_id: str | None
    parent_skill_id: str | None
    parent_version: int | None
    created_at: str

    def __post_init__(self) -> None:
        _check_type(self.origin, SkillOrigin, "provenance.origin")
        _check_optional_id(self.source_run_id, "provenance.source_run_id")
        _check_optional_id(self.parent_skill_id, "provenance.parent_skill_id")
        if self.parent_version is not None:
            _check_int(self.parent_version, "provenance.parent_version", 1, MAX_VERSION)
        if (self.parent_skill_id is None) != (self.parent_version is None):
            raise SkillRecordError("parent_skill_id and parent_version go together")
        _check_timestamp(self.created_at, "provenance.created_at")
        if self.origin is SkillOrigin.TAUGHT_FROM_RUN:
            if self.source_run_id is None or self.parent_skill_id is not None:
                raise SkillRecordError("a taught skill names its run and has no parent")
        elif self.parent_skill_id is None:
            raise SkillRecordError(f"origin {self.origin.value} needs a parent version")

    @classmethod
    def _parse(cls, data: Any, where: str) -> SkillProvenance:
        d = _object(data, cls, where)
        return cls(
            origin=_enum(SkillOrigin, d["origin"], f"{where}.origin"),
            source_run_id=d["source_run_id"],
            parent_skill_id=d["parent_skill_id"],
            parent_version=d["parent_version"],
            created_at=d["created_at"],
        )


def _procedure_fields(d: dict[str, Any], where: str) -> dict[str, Any]:
    inputs = _array(d["inputs"], f"{where}.inputs", MAX_INPUTS)
    steps = _array(d["steps"], f"{where}.steps", MAX_STEPS)
    return {
        "name": d["name"],
        "summary": d["summary"],
        "scope": SkillScope._parse(d["scope"], f"{where}.scope"),
        "inputs": tuple(
            SkillInput._parse(item, f"{where}.inputs[{i}]") for i, item in enumerate(inputs)
        ),
        "steps": tuple(
            SkillStep._parse(item, f"{where}.steps[{i}]") for i, item in enumerate(steps)
        ),
        "checks": tuple(_array(d["checks"], f"{where}.checks", MAX_CHECKS)),
        "provenance": SkillProvenance._parse(d["provenance"], f"{where}.provenance"),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class Skill(_Record):
    """One immutable skill version: a reusable procedure, never an authority.

    No status or health field: both are computed from the skill's events. A
    taught or adapted skill is version 1 of a NEW skill (an adaptation never
    changes its parent); a user edit or a version after a failure is a later
    version of the same skill and names an earlier one as its parent.
    """

    skill_id: str
    version: int
    name: str
    summary: str
    scope: SkillScope
    inputs: tuple[SkillInput, ...]
    steps: tuple[SkillStep, ...]
    checks: tuple[str, ...]
    provenance: SkillProvenance

    def __post_init__(self) -> None:
        _check_id(self.skill_id, "skill_id")
        _check_int(self.version, "version", 1, MAX_VERSION)
        _check_procedure(self)
        prov = self.provenance
        if prov.origin in _NEW_SKILL_ORIGINS:
            if self.version != 1 or prov.parent_skill_id == self.skill_id:
                raise SkillRecordError(
                    "a taught or adapted skill is version 1 of a new skill"
                )
        elif (
            prov.parent_skill_id != self.skill_id
            or prov.parent_version is None
            or prov.parent_version >= self.version
        ):
            raise SkillRecordError(
                "a new version names an earlier version of the same skill"
            )
        _check_size(self)

    @classmethod
    def _parse(cls, data: Any, where: str) -> Skill:
        d = _object(data, cls, where)
        return cls(
            skill_id=d["skill_id"], version=d["version"], **_procedure_fields(d, where)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillDraft(_Record):
    """A proposed skill, saved only on the user's explicit approval of it.

    ``target_skill_id`` is set only for a new-version proposal and must be the
    parent's skill; a taught or adapted draft makes a new skill and has none.
    """

    draft_id: str
    name: str
    summary: str
    scope: SkillScope
    inputs: tuple[SkillInput, ...]
    steps: tuple[SkillStep, ...]
    checks: tuple[str, ...]
    provenance: SkillProvenance
    reason: str
    target_skill_id: str | None

    def __post_init__(self) -> None:
        _check_id(self.draft_id, "draft_id")
        _check_procedure(self)
        _check_text(self.reason, "reason", TEXT_MAX)
        _check_optional_id(self.target_skill_id, "target_skill_id")
        if self.provenance.origin in _NEW_SKILL_ORIGINS:
            if self.target_skill_id is not None:
                raise SkillRecordError("a taught or adapted draft has no target_skill_id")
        elif (
            self.target_skill_id is None
            or self.provenance.parent_skill_id != self.target_skill_id
        ):
            raise SkillRecordError("a version proposal targets its parent's skill")
        _check_size(self)

    @classmethod
    def _parse(cls, data: Any, where: str) -> SkillDraft:
        d = _object(data, cls, where)
        return cls(
            draft_id=d["draft_id"],
            reason=d["reason"],
            target_skill_id=d["target_skill_id"],
            **_procedure_fields(d, where),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillHealth(_Record):
    """Use counters computed from a skill's events. Never stored on a skill
    and never a trust signal: a healthy skill's steps still meet Core policy
    and approval like any other step."""

    use_count: int
    success_count: int
    failure_count: int
    consecutive_failures: int
    last_success_at: str | None
    last_failure_at: str | None

    def __post_init__(self) -> None:
        for name in ("use_count", "success_count", "failure_count", "consecutive_failures"):
            _check_int(getattr(self, name), name, 0, MAX_COUNT)
        if self.success_count + self.failure_count > self.use_count:
            raise SkillRecordError("successes and failures cannot exceed uses")
        if self.consecutive_failures > self.failure_count:
            raise SkillRecordError("consecutive_failures cannot exceed failure_count")
        for count, stamp, name in (
            (self.success_count, self.last_success_at, "last_success_at"),
            (self.failure_count, self.last_failure_at, "last_failure_at"),
        ):
            if stamp is not None:
                _check_timestamp(stamp, name)
            if (stamp is None) != (count == 0):
                raise SkillRecordError(f"{name} is set exactly when its count is not 0")

    @classmethod
    def _parse(cls, data: Any, where: str) -> SkillHealth:
        return cls(**_object(data, cls, where))


@dataclass(frozen=True, slots=True, kw_only=True)
class Experience(_Record):
    """One lesson from a finished run: advice only. An ``avoid`` lesson can
    only add caution; no lesson grants, skips or pre-approves anything.
    ``task_signature`` holds the tokens a task is matched on, each already in
    its canonical form (``fold_token(token) == token``)."""

    experience_id: str
    kind: ExperienceKind
    skill_id: str | None
    task_signature: tuple[str, ...]
    text: str
    scope: SkillScope
    source_run_id: str
    created_at: str

    def __post_init__(self) -> None:
        _check_id(self.experience_id, "experience_id")
        _check_type(self.kind, ExperienceKind, "kind")
        _check_optional_id(self.skill_id, "skill_id")
        _check_tuple(self.task_signature, "task_signature", str, 1, MAX_SIGNATURE_TOKENS)
        for token in self.task_signature:
            if (
                not 1 <= len(token) <= TOKEN_MAX
                or " " in token
                or not token.isprintable()
                or _BAD_TEXT_CHAR.search(token)
            ):
                raise SkillRecordError(
                    f"task_signature tokens are 1-{TOKEN_MAX} printable characters "
                    "without spaces"
                )
            if fold_token(token) != token:
                raise SkillRecordError(
                    "task_signature tokens must be canonical: fold_token(token) == token"
                )
        _check_unique(self.task_signature, "task_signature")
        _check_text(self.text, "text", TEXT_MAX)
        _check_type(self.scope, SkillScope, "scope")
        _check_id(self.source_run_id, "source_run_id")
        _check_timestamp(self.created_at, "created_at")
        _check_size(self)

    @classmethod
    def _parse(cls, data: Any, where: str) -> Experience:
        d = _object(data, cls, where)
        return cls(
            experience_id=d["experience_id"],
            kind=_enum(ExperienceKind, d["kind"], f"{where}.kind"),
            skill_id=d["skill_id"],
            task_signature=tuple(
                _array(d["task_signature"], f"{where}.task_signature", MAX_SIGNATURE_TOKENS)
            ),
            text=d["text"],
            scope=SkillScope._parse(d["scope"], f"{where}.scope"),
            source_run_id=d["source_run_id"],
            created_at=d["created_at"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStep(_Record):
    """One executed step of a finished run, as history."""

    action: str
    capability: str | None
    outcome: RunOutcome
    error: str | None

    def __post_init__(self) -> None:
        _check_text(self.action, "run step action", TEXT_MAX)
        _check_optional_id(self.capability, "run step capability")
        _check_type(self.outcome, RunOutcome, "run step outcome")
        _check_optional_text(self.error, "run step error", TEXT_MAX)

    @classmethod
    def _parse(cls, data: Any, where: str) -> RunStep:
        d = _object(data, cls, where)
        return cls(
            action=d["action"],
            capability=d["capability"],
            outcome=_enum(RunOutcome, d["outcome"], f"{where}.outcome"),
            error=d["error"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillRef(_Record):
    """One exact skill version."""

    skill_id: str
    version: int

    def __post_init__(self) -> None:
        _check_id(self.skill_id, "skill_id")
        _check_int(self.version, "version", 1, MAX_VERSION)

    @classmethod
    def _parse(cls, data: Any, where: str) -> SkillRef:
        return cls(**_object(data, cls, where))


@dataclass(frozen=True, slots=True, kw_only=True)
class RunSummary(_Record):
    """The plain view of one FINISHED run that the pure skill modules read
    (SKLEARN maps Core runs to it). History, never authority: that a step ran
    or was approved once approves nothing again."""

    run_id: str
    task_text: str
    project_id: str | None
    status: RunOutcome
    steps: tuple[RunStep, ...]
    user_correction: str | None
    matched_skill: SkillRef | None

    def __post_init__(self) -> None:
        _check_id(self.run_id, "run_id")
        _check_text(self.task_text, "task_text", LONG_TEXT_MAX)
        if self.project_id is not None:
            _check_project_id(self.project_id, "project_id")
        _check_type(self.status, RunOutcome, "status")
        _check_tuple(self.steps, "steps", RunStep, 0, MAX_STEPS)
        _check_optional_text(self.user_correction, "user_correction", LONG_TEXT_MAX)
        if self.matched_skill is not None:
            _check_type(self.matched_skill, SkillRef, "matched_skill")
        _check_size(self)

    @classmethod
    def _parse(cls, data: Any, where: str) -> RunSummary:
        d = _object(data, cls, where)
        steps = _array(d["steps"], f"{where}.steps", MAX_STEPS)
        matched = d["matched_skill"]
        return cls(
            run_id=d["run_id"],
            task_text=d["task_text"],
            project_id=d["project_id"],
            status=_enum(RunOutcome, d["status"], f"{where}.status"),
            steps=tuple(
                RunStep._parse(item, f"{where}.steps[{i}]") for i, item in enumerate(steps)
            ),
            user_correction=d["user_correction"],
            matched_skill=(
                None if matched is None else SkillRef._parse(matched, f"{where}.matched_skill")
            ),
        )


#: The V2.2 plan's data-model name for ``RunSummary``.
SkillRunSummary = RunSummary
