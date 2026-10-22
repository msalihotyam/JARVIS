"""V2.2 save-as-skill: when to offer, and deterministic drafts from a run.

``should_offer`` decides whether JARVIS may OFFER to save a finished run as a
skill. ``build_drafts`` turns the steps that actually ran into one or more
``SkillDraft`` records, with no model call (PLAN D9). ``parse_user_edited_draft``
reads the draft back from the approval card, which is untrusted input.

An offer is a question to the user, never a save: a draft becomes a skill only
on the user's explicit approval of that draft, elsewhere (SkillStore). Nothing
here grants, implies or remembers any approval: a draft is a procedure, and
when it is used each step meets Core policy and approval like any other step.

The caller decides whether skills are enabled at all (``skills_enabled``) and
runs the secret/personal-data filter (skill_filter) over every draft before it
is shown or saved. Drafts keep parameter VALUES out of the procedure: every
URL, e-mail address, path, quoted text, number, date, time and long opaque
token in a step becomes a named input, and the step text says ``{name}``.

Pure: stdlib and ``jarvis.skill_types`` only, no I/O, no Core or app import.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from jarvis.skill_types import (
    MAX_CHECKS,
    MAX_INPUTS,
    MAX_JSON_BYTES,
    NAME_MAX,
    TEXT_MAX,
    RunOutcome,
    RunStep,
    RunSummary,
    SkillDraft,
    SkillInput,
    SkillOrigin,
    SkillProvenance,
    SkillRecordError,
    SkillScope,
    SkillScopeKind,
    SkillStep,
    fold_token,
)

#: A suppression entry that turns every save-as-skill offer off.
GLOBAL_SUPPRESSION = "global"
#: Prefix of a task kind id (``tk-`` + 32 hex characters; an id per skill_types).
TASK_KIND_PREFIX = "tk-"
#: At most this many drafts come from one run; more segments make one draft.
MAX_DRAFTS = 4
#: Ceiling for the raw edited-draft payload the UI sends back (JSON text).
EDITED_PAYLOAD_MAX_BYTES = 4 * MAX_JSON_BYTES
#: Capabilities that only answer in words. A run whose executed steps are all
#: of these (or have no capability) is a chat answer, not a procedure.
CHAT_CAPABILITIES = frozenset(
    {
        "chat",
        "chat_reply",
        "conversation",
        "general_chat",
        "intent_understanding",
        "research",
        "web_research",
    }
)


class OfferReason(str, Enum):
    """Why a run was, or was not, offered as a skill."""

    OFFER = "offer"
    INVALID_INPUT = "invalid_input"
    ALREADY_OFFERED = "already_offered"
    NOT_SUCCEEDED = "not_succeeded"
    SKILL_USED = "skill_used"
    CHAT_ONLY = "chat_only"
    TRIVIAL = "trivial"
    SUPPRESSED = "suppressed"
    NO_DRAFT = "no_draft"


@dataclass(frozen=True, slots=True, kw_only=True)
class OfferDecision:
    """Whether to ASK the user about saving a run as a skill. An offer saves
    nothing and approves nothing; ``task_kind`` is what a "do not offer this
    again" suppression stores."""

    offer: bool
    reason: OfferReason
    task_kind: str | None


# -- text helpers ----------------------------------------------------------


def _words(text: str) -> list[str]:
    """The task words of ``text``, folded with ``skill_types.fold_token`` (the
    one fold for task words, PLAN D13: Turkish dotted/dotless i included)."""
    return re.findall(r"\w+", fold_token(text))


def _refused_in_text(char: str) -> bool:
    """Whether skill_types refuses ``char`` in a record's text field. Asked of
    skill_types itself (a one-character input description), so the answer is
    exactly its rule: the D17 invisible list, U+2800, controls, format,
    private-use, surrogate and unassigned characters; tab, newline and CR pass
    (D12 handles them)."""
    try:
        SkillInput(name="probe", description=char, required=False)
    except SkillRecordError:
        return True
    return False


def _visible(text: str) -> str:
    """``text`` without every character skill_types refuses in text (PLAN D17:
    invisible or unsafe; emoji + U+FE0F keeps the emoji). Run text from tools
    and users goes through this before any draft record is built, so the
    visible text stays and nothing hidden reaches a draft."""
    refused = {char for char in set(text) if _refused_in_text(char)}
    if not refused:
        return text
    return "".join(char for char in text if char not in refused)


def _one_line(text: str) -> str:
    """Printable, single-spaced, stripped."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(cleaned.split())


#: Tab, newline and carriage return: never in a drafted step action (PLAN D12:
#: the action becomes ONE line of a chain objective).
_LINE_BREAKS = (chr(9), chr(10), chr(13))
_LINE_BREAKS_TO_SPACE = str.maketrans({ch: " " for ch in _LINE_BREAKS})
_SPACE_RUN = re.compile(" {2,}")


def _single_line(text: str) -> str:
    """PLAN D12: tab, newline and CR become a space, runs of spaces become
    one, ends trimmed. Placeholders hold no spaces, so they stay intact."""
    return _SPACE_RUN.sub(" ", text.translate(_LINE_BREAKS_TO_SPACE)).strip(" ")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


#: Literal braces from run text become parentheses before placeholders go in,
#: so every brace in a drafted step is a placeholder (PLAN D12).
_LITERAL_BRACES = str.maketrans({"{": "(", "}": ")"})
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
#: Input kinds that name the thing a step acts on; the first one a step uses
#: is its primary object and comes first in ``SkillStep.inputs`` (PLAN D12).
_OBJECT_KINDS = frozenset({"path", "url", "email"})


def _literal(text: str) -> str:
    return text.translate(_LITERAL_BRACES)


def _placeholders_ok(action: str, inputs: tuple[str, ...]) -> bool:
    """PLAN D12: every brace in ``action`` belongs to a ``{name}`` placeholder
    whose name is one of ``inputs``."""
    for match in _PLACEHOLDER.finditer(action):
        if match.group(1) not in inputs:
            return False
    rest = _PLACEHOLDER.sub("", action)
    return "{" not in rest and "}" not in rest


# -- verbs -------------------------------------------------------------------

_VERB_GROUPS: dict[str, tuple[str, ...]] = {
    "open": ("open", "launch", "start", "ac", "baslat"),
    "close": ("close", "quit", "exit", "kapat"),
    "create": ("create", "make", "new", "add", "olustur", "yarat", "ekle"),
    "read": ("read", "view", "show", "display", "get", "fetch", "oku", "goster", "getir"),
    "write": ("write", "save", "store", "yaz", "kaydet"),
    "edit": ("edit", "update", "modify", "change", "duzenle", "guncelle", "degistir"),
    "send": ("send", "email", "mail", "post", "reply", "forward", "gonder", "yanitla"),
    "search": ("search", "find", "look", "lookup", "query", "ara", "bul"),
    "download": ("download", "indir"),
    "upload": ("upload", "yukle"),
    "copy": ("copy", "duplicate", "kopyala"),
    "move": ("move", "transfer", "tasi"),
    "rename": ("rename",),
    "delete": ("delete", "remove", "erase", "sil", "kaldir"),
    "run": ("run", "execute", "build", "calistir", "derle"),
    "list": ("list", "enumerate", "listele"),
    "summarize": ("summarize", "summarise", "ozetle"),
    "navigate": ("navigate", "go", "visit", "browse", "git"),
    "verify": ("verify", "check", "confirm", "validate", "ensure", "test", "dogrula", "kontrol"),
}
#: Folded word -> canonical verb (open/launch/start -> open; Turkish too).
_VERBS = {fold_token(word): verb for verb, words in _VERB_GROUPS.items() for word in words}
_CHECK_VERB = "verify"
_STOP_WORDS = frozenset(
    fold_token(word)
    for word in (
        "the", "and", "for", "from", "into", "onto", "with", "this", "that", "then",
        "all", "its", "via", "using", "use", "each", "every", "there", "here", "your",
        "bir", "ile", "icin", "sonra", "daha", "gibi", "olan", "her", "tum", "sey",
    )
)
_STEM = 5


def _normalised_verb(words: list[str]) -> str:
    if not words:
        return "-"
    for word in (words[0], words[-1], *words):
        if word in _VERBS:
            return _VERBS[word]
    return words[0][:32]


# -- parameters ----------------------------------------------------------------

_EXT = r"\.[A-Za-z][A-Za-z0-9]{1,7}"
#: Any scheme (https, ftp, file, ...), or a "www." host, up to the next space:
#: the query string (``?token=...``) is part of the value, never left behind.
_URL = re.compile(r"(?<![\w+.-])(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)[^\s<>\"'`]+")
#: A scheme-less host name with a path (example.com/reset?token=...).
_HOST_URL = re.compile(
    r"(?<![\w@./\\-])[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,24}(?::\d{1,5})?/[^\s<>\"'`]*"
)
#: Local and domain parts may be non-ASCII; a dotless domain (localhost) counts.
_EMAIL = re.compile(r"(?<![\w.+%-])[\w.%+-]+@[\w-]+(?:\.[\w-]+)*")
#: A UNC path: server, share and file name are all part of the value.
_UNC_PATH = re.compile(r"(?<!\\)(?:\\){2,}[^\s\\/\"'<>|]+(?:\\[^\s\"'<>|]*)?")
_WIN_PATH = re.compile(r"(?<!\w)[A-Za-z]:[\\/][^\s\"'<>|]*")
_POSIX_PATH = re.compile(r"(?<![\w/.~])(?:~|\.{1,2})?/[^\s\"'<>|]+")
_REL_PATH = re.compile(r"(?<![\w./\\~-])[\w.-]+(?:[/\\][\w.-]+)*[/\\][\w-]+" + _EXT + r"(?!\w)")
_TOKEN = re.compile(
    r"(?<![\w+/=-])(?=[A-Za-z0-9_+/=-]*\d)(?=[A-Za-z0-9_+/=-]*[A-Za-z])[A-Za-z0-9_+/=-]{20,}"
)
_FILENAME = re.compile(r"(?<![\w.\\/-])[\w-]+(?:\.[\w-]+)*" + _EXT + r"(?!\w)")
_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_TIME = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?![\d:])")
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*")
_TRAILING = ".,;:!?)]}"
_QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("`", "`"),
    (chr(0x201C), chr(0x201D)),
    (chr(0x2018), chr(0x2019)),
)
_QUOTED = tuple(
    re.compile(
        r"(?<!\w)" + re.escape(left) + r"([^" + re.escape(right) + r"\r\n]{1,200})"
        + re.escape(right) + r"(?!\w)"
    )
    for left, right in _QUOTE_PAIRS
)
#: Unquoted kinds, in priority order (an earlier kind wins a tie).
_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", _URL),
    ("email", _EMAIL),
    ("url", _HOST_URL),
    ("path", _UNC_PATH),
    ("path", _WIN_PATH),
    ("path", _POSIX_PATH),
    ("path", _REL_PATH),
    ("value", _TOKEN),
    ("path", _FILENAME),
    ("date", _DATE),
    ("time", _TIME),
    ("number", _NUMBER),
)


@dataclass(frozen=True, slots=True)
class _Param:
    start: int
    end: int
    value: str
    kind: str


def _given(value: str, folded_task: str) -> bool:
    """Whether the user typed ``value`` in the task, as a whole word or more
    (so a number 3 is not "given" by a task that only says Q3)."""
    pattern = r"(?<!\w)" + re.escape(fold_token(value)) + r"(?!\w)"
    return re.search(pattern, folded_task) is not None


def _quoted_kind(value: str) -> str:
    for kind, pattern in _KINDS:
        if pattern.fullmatch(value):
            return kind
    return "text"


def _find_params(text: str) -> tuple[_Param, ...]:
    """Every parameter value in ``text``, left to right, never overlapping."""
    found: list[tuple[int, int, int, str, str]] = []
    for pattern in _QUOTED:
        for match in pattern.finditer(text):
            value = match.group(1)
            if value.strip():
                found.append((match.start(1), 0, match.end(1), value, _quoted_kind(value)))
    for priority, (kind, pattern) in enumerate(_KINDS, start=1):
        for match in pattern.finditer(text):
            start, end = match.span()
            if kind == "url" or kind == "path":
                while end > start + 1 and text[end - 1] in _TRAILING:
                    end -= 1
            found.append((start, priority, end, text[start:end], kind))
    found.sort(key=lambda item: (item[0], item[1], -item[2]))
    chosen: list[_Param] = []
    taken_until = 0
    for start, _priority, end, value, kind in found:
        if start >= taken_until and end > start:
            chosen.append(_Param(start, end, value, kind))
            taken_until = end
    return tuple(chosen)


# -- step analysis ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Analysed:
    step: RunStep
    #: The step's action with every refused character removed (``_visible``).
    text: str
    params: tuple[_Param, ...]
    verb: str
    stems: frozenset[str]

    @property
    def is_check(self) -> bool:
        return self.verb == _CHECK_VERB


def _without_params(text: str, params: tuple[_Param, ...]) -> str:
    parts, cursor = [], 0
    for param in params:
        parts.append(text[cursor : param.start])
        parts.append(" ")
        cursor = param.end
    parts.append(text[cursor:])
    return "".join(parts)


def _analyse(step: RunStep) -> _Analysed:
    text = _visible(step.action)
    params = _find_params(text)
    words = [word for word in _words(_without_params(text, params)) if not word.isdigit()]
    verb = _normalised_verb(words)
    stems = frozenset(
        word[:_STEM]
        for word in words
        if len(word) >= 3 and word not in _VERBS and word not in _STOP_WORDS
    )
    return _Analysed(step, text, params, verb, stems)


def _executed(run: RunSummary) -> tuple[RunStep, ...]:
    """The steps that actually ran to completion; failed, rejected and
    cancelled steps are not part of a procedure."""
    return tuple(step for step in run.steps if step.outcome is RunOutcome.COMPLETED)


def task_kind(run: RunSummary) -> str:
    """The deterministic kind of a run's work: its executed steps' capabilities
    and normalised verbs, in order (parameter values do not count)."""
    parts: list[str] = []
    for step in _executed(run):
        part = f"{step.capability or '-'}:{_analyse(step).verb}"
        if not parts or parts[-1] != part:
            parts.append(part)
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return TASK_KIND_PREFIX + digest[:32]


#: A draft names a second input of a kind ``kind_2``; the kind is the name
#: without that suffix.
_INPUT_SUFFIX = re.compile(r"_\d+$")


def step_signature(steps: tuple[SkillStep, ...]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """b8 (AWM): the abstract shape of a procedure - each step's capability and
    the kinds of the targets it takes, in order. Words never count: the same
    work on the same kinds of things has one signature. Deterministic and
    text-free; only compared, never stored or shown."""
    return tuple(
        (
            step.capability or "-",
            tuple(sorted(_INPUT_SUFFIX.sub("", name) for name in step.inputs)),
        )
        for step in steps
    )


def step_family(steps: tuple[SkillStep, ...]) -> tuple[str, ...]:
    """b8: the coarser family of a procedure - its capabilities in order, a
    repeated capability counted once, target kinds ignored."""
    family: list[str] = []
    for step in steps:
        capability = step.capability or "-"
        if not family or family[-1] != capability:
            family.append(capability)
    return tuple(family)


# -- segments ----------------------------------------------------------------------


def _components(analysed: tuple[_Analysed, ...]) -> list[list[int]]:
    """Independent segments, contiguous and in order. Conservative: a new
    segment starts only on positive evidence on both sides - the step and the
    segment so far each have values and goal words, and share neither. A
    verification step never starts a segment. Segments that turn out to share
    a value or a goal word with any other segment are not independent: then
    the whole run is one segment."""
    groups: list[list[int]] = []
    values: list[set[str]] = []
    goals: list[set[str]] = []
    for i, item in enumerate(analysed):
        mine = {param.value for param in item.params}
        if (
            groups
            and not item.is_check
            and mine
            and item.stems
            and values[-1]
            and goals[-1]
            and not mine & values[-1]
            and not item.stems & goals[-1]
        ):
            groups.append([])
            values.append(set())
            goals.append(set())
        elif not groups:
            groups.append([])
            values.append(set())
            goals.append(set())
        groups[-1].append(i)
        values[-1] |= mine
        goals[-1] |= item.stems
    for a in range(len(groups)):
        for b in range(a + 1, len(groups)):
            if values[a] & values[b] or goals[a] & goals[b]:
                return [list(range(len(analysed)))]
    return groups


@dataclass(frozen=True, slots=True)
class _Segment:
    steps: tuple[SkillStep, ...]
    inputs: tuple[SkillInput, ...]
    values: dict[str, str]
    checks: tuple[str, ...]
    user_input: bool


def _template(run: RunSummary, analysed: tuple[_Analysed, ...]) -> _Segment | None:
    """Steps with ``{name}`` in place of every parameter value, or None when
    the result would not fit a draft (fail closed: no draft)."""
    task = fold_token(_visible(run.task_text))
    names: dict[str, str] = {}
    kind_of: dict[str, str] = {}
    kinds_used: dict[str, int] = {}
    inputs: list[SkillInput] = []
    user_input = False
    steps: list[SkillStep] = []
    checks: list[str] = []
    for index, item in enumerate(analysed, start=1):
        parts, cursor, used = [], 0, []
        for param in item.params:
            if param.value not in names:
                count = kinds_used.get(param.kind, 0) + 1
                kinds_used[param.kind] = count
                name = param.kind if count == 1 else f"{param.kind}_{count}"
                names[param.value] = name
                kind_of[name] = param.kind
                given = _given(param.value, task)
                user_input = user_input or given
                inputs.append(
                    SkillInput(
                        name=name,
                        description=(
                            f"A {param.kind} the user gave in the task"
                            if given
                            else f"A {param.kind} that may change between runs"
                        ),
                        required=True,
                    )
                )
            name = names[param.value]
            parts.append(_literal(item.text[cursor : param.start]))
            parts.append("{" + name + "}")
            cursor = param.end
            if name not in used:
                used.append(name)
        parts.append(_literal(item.text[cursor:]))
        action = _single_line("".join(parts))
        # Primary object first (the first path, URL or e-mail the step uses,
        # else its first input); the rest in order of first use.
        primary = next((name for name in used if kind_of[name] in _OBJECT_KINDS), None)
        if primary is not None:
            used.remove(primary)
            used.insert(0, primary)
        if len(inputs) > MAX_INPUTS or len(action) > TEXT_MAX:
            return None
        if not _placeholders_ok(action, tuple(used)):
            return None
        try:
            steps.append(
                SkillStep(
                    index=index,
                    action=action,
                    capability=item.step.capability,
                    inputs=tuple(used),
                    check=None,
                )
            )
        except SkillRecordError:
            return None
        if item.is_check and action not in checks:
            checks.append(action)
    return _Segment(tuple(steps), tuple(inputs), dict(names), tuple(checks[:MAX_CHECKS]), user_input)


def _qualifies(segment: _Segment) -> bool:
    """Repeatable and non-trivial: two or more executed steps, or one step
    that takes a value the user gave."""
    return len(segment.steps) >= 2 or segment.user_input


def _summary_text(run: RunSummary, segment: _Segment) -> str:
    """The task text with this segment's values as ``{name}`` and any other
    recognised value left out as ``...``."""
    text = _visible(run.task_text)
    parts, cursor = [], 0
    for param in _find_params(text):
        parts.append(_literal(text[cursor : param.start]))
        name = segment.values.get(param.value)
        parts.append("{" + name + "}" if name else "...")
        cursor = param.end
    parts.append(_literal(text[cursor:]))
    return _one_line("".join(parts))


def _draft_id(run: RunSummary, part: int, total: int) -> str:
    digest = hashlib.sha256(f"{run.run_id}:{part}:{total}".encode("utf-8")).hexdigest()
    return "draft-" + digest[:32]


def _steps_text(segment: _Segment) -> str:
    count = len(segment.steps)
    return f"{count} executed step" + ("" if count == 1 else "s")


def _make_draft(
    run: RunSummary, segment: _Segment, part: int, total: int, created_at: str
) -> SkillDraft:
    summary = _summary_text(run, segment)
    if total == 1:
        name = _clip(summary, NAME_MAX)
        reason = (
            f"Taught from a finished run: {_steps_text(segment)}. "
            "Saved only if you approve this draft."
        )
    else:
        name = _clip(_one_line(segment.steps[0].action), NAME_MAX)
        summary = f"Part {part} of {total}: {summary}"
        reason = (
            f"Taught from a finished run, independent part {part} of {total}: "
            f"{_steps_text(segment)}. Saved only if you approve this draft."
        )
    if len(summary) > TEXT_MAX:
        summary = _clip(summary, TEXT_MAX - 3) + "..."
    if run.project_id is None:
        scope = SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None)
    else:
        scope = SkillScope(kind=SkillScopeKind.PROJECT, project_id=run.project_id)
    return SkillDraft(
        draft_id=_draft_id(run, part, total),
        name=name or "Skill from a finished run",
        summary=summary or "Steps from a finished run",
        scope=scope,
        inputs=segment.inputs,
        steps=segment.steps,
        checks=segment.checks,
        provenance=SkillProvenance(
            origin=SkillOrigin.TAUGHT_FROM_RUN,
            source_run_id=run.run_id,
            parent_skill_id=None,
            parent_version=None,
            created_at=created_at,
        ),
        reason=reason,
        target_skill_id=None,
    )


def _drafts(run: RunSummary, created_at: str) -> tuple[SkillDraft, ...]:
    """The drafts of ``run``; both ``build_drafts`` and ``should_offer`` use
    this, so they agree. Run text is untrusted: no ``SkillRecordError`` from
    building a record escapes, a run whose records cannot be built has no
    draft (and so no offer)."""
    try:
        return _segment_drafts(run, created_at)
    except SkillRecordError:
        return ()


def _segment_drafts(run: RunSummary, created_at: str) -> tuple[SkillDraft, ...]:
    analysed = tuple(_analyse(step) for step in _executed(run))
    if not analysed:
        return ()
    groups = _components(analysed)
    if 1 < len(groups) <= MAX_DRAFTS:
        segments = [_template(run, tuple(analysed[i] for i in group)) for group in groups]
        if all(segment is not None and _qualifies(segment) for segment in segments):
            try:
                return tuple(
                    _make_draft(run, segment, part, len(segments), created_at)
                    for part, segment in enumerate(segments, start=1)
                    if segment is not None
                )
            except SkillRecordError:
                pass
    whole = _template(run, analysed)
    if whole is None or not _qualifies(whole):
        return ()
    try:
        return (_make_draft(run, whole, 1, 1, created_at),)
    except SkillRecordError:
        return ()


def _now() -> str:
    # The same spelling as ``jarvis.contracts.utc_now`` (not imported: pure).
    return datetime.now(timezone.utc).isoformat()


# -- public API --------------------------------------------------------------------


def build_drafts(run: RunSummary, *, created_at: str | None = None) -> tuple[SkillDraft, ...]:
    """Drafts of the procedure ``run`` followed, from its EXECUTED steps only.

    Deterministic, no model call: the same run and ``created_at`` give the
    same drafts (ids included). Independent segments (no shared value and no
    shared goal word) become separate drafts when each one is repeatable on
    its own; otherwise the whole run is one draft. Scope is the run's project,
    else global. A run that would not fit a valid draft gives ``()``.
    """
    if type(run) is not RunSummary:
        raise SkillRecordError("build_drafts needs a RunSummary")
    stamp = _now() if created_at is None else created_at
    # A bad timestamp is the caller's error, not "no draft": refuse it here.
    SkillProvenance(
        origin=SkillOrigin.TAUGHT_FROM_RUN,
        source_run_id=run.run_id,
        parent_skill_id=None,
        parent_version=None,
        created_at=stamp,
    )
    return _drafts(run, stamp)


def _id_set(values: Any) -> frozenset[str] | None:
    """A finite collection of strings, else None (a one-shot iterator, a
    string or a mapping is refused rather than guessed at)."""
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Collection):
        return None
    try:
        items = frozenset(values)
    except TypeError:
        return None
    if any(type(item) is not str for item in items):
        return None
    return items


def should_offer(
    run: RunSummary, suppressions: Collection[str], offered_run_ids: Collection[str]
) -> OfferDecision:
    """Whether to ask the user about saving ``run`` as a skill.

    Only repeatable, non-trivial work is offered: a completed run, not a
    chat answer, not an existing skill used (adaptations and new versions
    are the lifecycle's), two or more executed steps or one step with a value
    the user gave, a task kind the user has not suppressed (``GLOBAL_SUPPRESSION``
    suppresses all), and not already offered for this run. Bad input never
    offers.
    """
    suppressed = _id_set(suppressions)
    offered = _id_set(offered_run_ids)
    if type(run) is not RunSummary or suppressed is None or offered is None:
        return OfferDecision(offer=False, reason=OfferReason.INVALID_INPUT, task_kind=None)
    kind = task_kind(run)

    def no(reason: OfferReason) -> OfferDecision:
        return OfferDecision(offer=False, reason=reason, task_kind=kind)

    if run.run_id in offered:
        return no(OfferReason.ALREADY_OFFERED)
    if run.status is not RunOutcome.COMPLETED:
        return no(OfferReason.NOT_SUCCEEDED)
    if run.matched_skill is not None:
        return no(OfferReason.SKILL_USED)
    executed = tuple(_analyse(step) for step in _executed(run))
    if all(
        item.step.capability is None or item.step.capability in CHAT_CAPABILITIES
        for item in executed
    ):
        return no(OfferReason.CHAT_ONLY)
    task = fold_token(_visible(run.task_text))
    if len(executed) < 2 and not any(
        _given(param.value, task) for item in executed for param in item.params
    ):
        return no(OfferReason.TRIVIAL)
    if GLOBAL_SUPPRESSION in suppressed or kind in suppressed:
        return no(OfferReason.SUPPRESSED)
    if not _drafts(run, _now()):
        return no(OfferReason.NO_DRAFT)
    return OfferDecision(offer=True, reason=OfferReason.OFFER, task_kind=kind)


def _refuse_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SkillRecordError("the edited draft repeats a key")
        result[key] = value
    return result


def parse_user_edited_draft(payload: Any, *, original: SkillDraft) -> SkillDraft:
    """The draft as the user edited it on the approval card: untrusted input.

    ``payload`` is the draft's ``to_dict`` data, or that data as JSON text
    (repeated keys refused). Parsing is ``SkillDraft.from_dict``: exact keys,
    exact types, bounded sizes, and any approval, trust, permission, risk,
    side-effect, auto-run or remember key anywhere refused (``AuthorityKeyError``).

    ``original`` (required) is the draft that was offered. The edit may change
    the procedure but never its lineage: ``draft_id``, ``provenance`` (with
    ``source_run_id``) and ``target_skill_id`` must stay. Scope may stay as it
    was or become global; a project draft never moves to another project and
    a global draft never moves into one (PLAN D8/D15). Every brace in a step's
    action must be a ``{name}`` placeholder of one of that step's inputs, and
    the action must be one line: no tab, newline or carriage return (D12).
    Raises ``SkillRecordError``; the result approves nothing.
    """
    if type(original) is not SkillDraft:
        raise SkillRecordError("original must be the offered SkillDraft")
    if isinstance(payload, str):
        # Length first: a str is at least one UTF-8 byte per character, so an
        # oversized paste is refused before anything is encoded or parsed.
        if len(payload) > EDITED_PAYLOAD_MAX_BYTES:
            raise SkillRecordError("the edited draft is too large")
        try:
            size = len(payload.encode("utf-8"))
        except UnicodeError:
            raise SkillRecordError("the edited draft is not valid text") from None
        if size > EDITED_PAYLOAD_MAX_BYTES:
            raise SkillRecordError("the edited draft is too large")
        try:
            payload = json.loads(payload, object_pairs_hook=_refuse_duplicate_keys)
        except SkillRecordError:
            raise
        except (ValueError, RecursionError):
            raise SkillRecordError("the edited draft is not valid JSON") from None
    draft = SkillDraft.from_dict(payload)
    if (
        draft.draft_id != original.draft_id
        or draft.provenance != original.provenance
        or draft.target_skill_id != original.target_skill_id
    ):
        raise SkillRecordError("an edit cannot change a draft's id or lineage")
    if draft.scope != original.scope and draft.scope.kind is not SkillScopeKind.GLOBAL:
        raise SkillRecordError("an edit may keep the draft's scope or make it global only")
    for skill_step in draft.steps:
        if any(ch in skill_step.action for ch in _LINE_BREAKS):
            raise SkillRecordError(
                "a step action must be one line (no tab, newline or carriage return)"
            )
        if not _placeholders_ok(skill_step.action, skill_step.inputs):
            raise SkillRecordError(
                "every brace in a step must be a {name} placeholder of one of its inputs"
            )
    return draft
