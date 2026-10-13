"""V2.2 deterministic, scoped skill matcher: fit, similar or none.

``match_skills`` looks for a saved skill that fits a new task. It is word
overlap only (KARAR-2, PLAN D5): no model, no embeddings, no network. Text is
folded with ``skill_types.fold_token`` (the one fold for task words, D13:
NFKC, Turkish dotted/dotless i to ``i``, case fold, accents dropped), then
split into words; Turkish and English stop words and pure numbers are
dropped. Two words match when they are equal or when the shorter one, at
least ``PREFIX_MIN`` letters long, starts the longer one (``rapor`` /
``raporunu``, ``send`` / ``sends``). A skill's words come from its name,
summary, input names and step action text, in that order.

``score`` is the share of the task's words the skill covers, in whole
percent; ``coverage`` is the share of the skill's name and summary words
that the task covers. A skill FITS when the score is at least
``FIT_MIN_SCORE``, at least ``FIT_MIN_MATCHED`` words match, at least one of
them is in the skill's name or summary, and the coverage is at least
``FIT_MIN_COVERAGE``; so a broad skill whose summary names many other things
(a delete step before an email step, say) is not a fit for a narrow task
that shares two of its words. A skill is SIMILAR when the score is at least
``SIMILAR_MIN_SCORE`` with at least ``SIMILAR_MIN_MATCHED`` matched words.
A one-word task is therefore never a fit.

Scope is an allowlist: a global skill is always considered; a project skill
only when ``project_id`` is that exact project. Another project's skill never
appears in any part of the result, not even as similar or stale. Disabled and
deleted skills never appear. A stale skill is never a fit and never a
candidate: it is listed only in ``needs_revalidation`` (with kind ``similar``
at most), because a stale skill is not used automatically and revalidating it
is a normal, approved run (S12).

Cost is bounded: the task is cut to ``MAX_TASK_CHARS`` and ``MAX_TASK_WORDS``;
each skill field is read up to ``FIELD_TEXT_MAX`` folded characters, a skill
up to ``SKILL_TEXT_MAX`` and ``MAX_SKILL_WORDS`` distinct words; a cut never
leaves a fragment of a word. A skill's words are computed once and kept in a
bounded in-memory cache (``CACHE_MAX`` skill versions, keyed by the whole
immutable skill value, so a different body under the same id and version is
never served stale words). The cache changes speed only, never a result.

A match is advice, never authority: a skill is a procedure. Using it does not
lower a risk tier, skip an approval or pre-approve anything; every step still
goes through Core policy and approval as an ordinary chain step. Nothing in a
result grants, implies or remembers approval.

Pure: stdlib and ``jarvis.skill_types`` only, no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from fractions import Fraction
from functools import lru_cache

from jarvis.skill_types import (
    ID_PATTERN,
    PROJECT_ID_PATTERN,
    Skill,
    SkillHealth,
    SkillScopeKind,
    SkillStatus,
    fold_token,
)

#: Minimum score (percent of task words covered) for a fit.
FIT_MIN_SCORE = 60
#: Minimum coverage (percent of the skill's name and summary words) for a fit.
FIT_MIN_COVERAGE = 60
#: Minimum number of matched task words for a fit.
FIT_MIN_MATCHED = 2
#: Minimum score for a similar skill (a candidate for adaptation).
SIMILAR_MIN_SCORE = 30
#: Minimum number of matched task words for a similar skill.
SIMILAR_MIN_MATCHED = 1
#: At most this many candidates, and this many ``needs_revalidation`` entries.
MAX_CANDIDATES = 5
#: Only this much of the task text is read (whole words only).
MAX_TASK_CHARS = 2_000
#: Only the first this many distinct task words are scored.
MAX_TASK_WORDS = 48
#: A longer word is cut to this many characters.
WORD_MAX = 40
#: Words shorter than this are dropped.
WORD_MIN = 2
#: A word of at least this many letters also matches its suffixed forms.
PREFIX_MIN = 4
#: Each skill field (name, summary, input name, step action) is read up to
#: this many folded characters (whole words only).
FIELD_TEXT_MAX = 600
#: A skill is read up to this many folded characters in all.
SKILL_TEXT_MAX = 3_000
#: A skill contributes at most this many distinct words.
MAX_SKILL_WORDS = 256
#: At most this many skills per call; more is refused (fail closed). Chosen
#: so that a cold call over this many maximum-size skills stays near 1-2 s
#: (measured and guarded by a test); a warm call (words cached) is far less.
MAX_SKILLS = 1_000
#: The word cache keeps at most this many skill versions (a full library).
CACHE_MAX = MAX_SKILLS
#: At most this many words are named in one reason line.
MAX_REASON_WORDS = 8

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Raw characters folded at a time, and the largest NFKC expansion of one
# character (U+FDFA becomes 18), so a field is never folded much past its cap.
_FOLD_CHUNK = 16
_MAX_EXPANSION = 18
# Runs of letters and digits: exactly the ``str.isalnum`` characters (the
# regex class is "not a non-word character and not an underscore").
_WORD_RUN = re.compile("[^" + chr(0x5C) + "W_]+")
# A "{name}" placeholder (D12: name is an input name, skill_types.ID_PATTERN).
# Placeholders in a skill's name, summary and step actions are not words: the
# value they stand for differs on every run (D18), so they are removed (each
# becomes a space) before the skill's words are read, for matching and for
# coverage. The TASK text is read as it is (its values are SKCORE's job).
_PLACEHOLDER = re.compile("[{]" + ID_PATTERN.pattern + "[}]")
# Above every character a word can hold (U+10FFFF is not alphanumeric).
_AFTER_ALL = chr(0x10FFFF)

# Function words and fillers that say nothing about WHAT the task is. Written
# naturally here; folded with ``fold_token`` below like every other word.
# Entries whose fold is also a Turkish content word are left out on purpose,
# so the stop list never swallows that word: "şu"/"şunu" (fold to su "water"
# and sunu "presentation"), "is" (iş "work") and "can" (can "life").
_STOP_WORDS_TR = (
    "ve veya ile ya da de ki mi mı mu mü bu o bir için gibi kadar daha çok en "
    "ama fakat ancak sonra önce şimdi lütfen bana beni benim bize bizim sen sana "
    "senin siz size ben biz onu bunu buna şuna ona onun bunun olan olarak "
    "her hem ne nasıl neden niye nerede hangi tüm bütün şey şeyi diye ise eğer "
    "hadi rica ederim acaba yine tekrar artık hep"
)
_STOP_WORDS_EN = (
    "a an the and or but nor to of in on at by for with from into onto as are "
    "was were be been it its this that these those my me mine our we us you your "
    "i please could would will should shall do does did then now all any "
    "some so if up out about just hey also after before again there here what how "
    "why when where which who"
)
_ASSISTANT_NAMES = "jarvis"
#: Turkish content words that no stop word may fold to (tested).
KEPT_CONTENT_WORDS = frozenset({"is", "su", "sunu", "can"})


class SkillMatchError(ValueError):
    """The matcher was called with input it refuses (fail closed)."""


class MatchKind(str, Enum):
    FIT = "fit"
    SIMILAR = "similar"
    NONE = "none"


def _wordish(char: str) -> bool:
    return char.isalnum() or unicodedata.category(char).startswith("M")


def cut_text(text: str, limit: int) -> str:
    """``text[:limit]`` without a fragment of a word at the end.

    When the cut falls inside a word (letters, digits or combining marks on
    both sides), the partial last word is dropped.
    """
    if len(text) <= limit:
        return text
    end = limit
    if _wordish(text[limit]):
        while end > 0 and _wordish(text[end - 1]):
            end -= 1
    return text[:end]


def _fold_capped(text: str, limit: int) -> str:
    """``fold_token`` of ``text``, read only up to ``limit`` folded characters.

    ASCII text never grows when folded, and neither does a text short enough
    that even the largest expansion stays under the cap: both are folded in
    one call. Any other text is folded ``_FOLD_CHUNK`` raw characters at a
    time and reading stops past the cap, so a text that expands under NFKC
    (U+FDFA becomes 18 characters) costs at most one chunk more than the cap.
    A word that straddles a chunk border is checked again as a whole word in
    ``_new_word``.
    """
    if text.isascii() or len(text) * _MAX_EXPANSION <= limit:
        return cut_text(fold_token(text), limit)
    parts: list[str] = []
    size = 0
    for start in range(0, len(text), _FOLD_CHUNK):
        piece = fold_token(text[start : start + _FOLD_CHUNK])
        parts.append(piece)
        size += len(piece)
        if size > limit:
            break
    return cut_text("".join(parts), limit)


def _candidates(folded: str) -> Iterator[str]:
    """Runs of letters and digits in folded text, each cut to ``WORD_MAX``."""
    for run in _WORD_RUN.findall(folded):
        word = run[:WORD_MAX]
        if len(word) >= WORD_MIN and not word.isdigit() and word not in STOP_WORDS:
            yield word


def _new_word(word: str, seen: dict[str, None]) -> bool:
    """Add a content word to ``seen`` if it is new and canonical.

    Every kept word satisfies ``fold_token(word) == word`` (ASCII words do by
    construction). A word that is not (possible only after a cut) is dropped,
    which can only lower a score: it fails toward no match.
    """
    if word in seen or (not word.isascii() and fold_token(word) != word):
        return False
    seen[word] = None
    return True


STOP_WORDS = frozenset(
    _WORD_RUN.findall(fold_token(" ".join((_STOP_WORDS_TR, _STOP_WORDS_EN, _ASSISTANT_NAMES))))
)


def words(text: str, limit: int | None = None) -> tuple[str, ...]:
    """The distinct content words of ``text``, in first-seen order."""
    seen: dict[str, None] = {}
    for word in _candidates(fold_token(text)):
        if _new_word(word, seen) and limit is not None and len(seen) >= limit:
            break
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class _WordIndex:
    exact: frozenset[str]
    ordered: tuple[str, ...]

    @classmethod
    def build(cls, items: Iterable[str]) -> _WordIndex:
        exact = frozenset(items)
        return cls(exact, tuple(sorted(exact)))


@dataclass(frozen=True, slots=True)
class _SkillWords:
    headline: _WordIndex  # name and summary
    body: _WordIndex  # headline, input names and step actions


def _without_placeholders(text: str) -> str:
    """``text`` with every "{name}" placeholder replaced by a space."""
    return _PLACEHOLDER.sub(" ", text)


@lru_cache(maxsize=CACHE_MAX)
def _skill_words(skill: Skill) -> _SkillWords:
    """A skill's words, bounded per field, per skill and in number.

    "{name}" placeholders in the name, summary and step actions are removed
    first (``_PLACEHOLDER``): they are never words of the skill.
    """
    fields = (
        [_without_placeholders(skill.name), _without_placeholders(skill.summary)]
        + [item.name for item in skill.inputs]
        + [_without_placeholders(step.action) for step in skill.steps]
    )
    seen: dict[str, None] = {}
    headline: tuple[str, ...] = ()
    budget = SKILL_TEXT_MAX
    for position, field in enumerate(fields):
        if budget > 0 and len(seen) < MAX_SKILL_WORDS:
            folded = _fold_capped(field, min(FIELD_TEXT_MAX, budget))
            budget -= len(folded)
            for word in _candidates(folded):
                if _new_word(word, seen) and len(seen) >= MAX_SKILL_WORDS:
                    break
        if position == 1:
            headline = tuple(seen)
    return _SkillWords(_WordIndex.build(headline), _WordIndex.build(seen))


@dataclass(frozen=True, slots=True)
class _TaskWords:
    """The task's words and the lookups every skill is compared with."""

    words: tuple[str, ...]
    long: tuple[str, ...]  # words of at least PREFIX_MIN letters
    by_key: dict[str, tuple[str, ...]]  # word or its long proper prefix -> words
    keys: frozenset[str]
    starts: tuple[str, ...]  # merged ranges of words that start with a long word
    ends: tuple[str, ...]

    @classmethod
    def build(cls, task_words: tuple[str, ...]) -> _TaskWords:
        by_key: dict[str, list[str]] = {}
        for word in task_words:
            by_key.setdefault(word, []).append(word)
            for size in range(PREFIX_MIN, len(word)):
                by_key.setdefault(word[:size], []).append(word)
        long = tuple(word for word in task_words if len(word) >= PREFIX_MIN)
        starts: list[str] = []
        ends: list[str] = []
        for word in sorted(long):
            if ends and word < ends[-1]:
                continue  # inside the range of a shorter word it starts with
            starts.append(word)
            ends.append(word + _AFTER_ALL)
        return cls(
            task_words,
            long,
            {key: tuple(value) for key, value in by_key.items()},
            frozenset(by_key),
            tuple(starts),
            tuple(ends),
        )

    def hits(self, index: _WordIndex) -> set[str]:
        """The task words that match some word of ``index``."""
        found: set[str] = set()
        for key in index.exact & self.keys:
            found.update(self.by_key[key])
        ordered = index.ordered
        for word in self.long:
            if word not in found:
                at = bisect_left(ordered, word)
                if at < len(ordered) and ordered[at].startswith(word):
                    found.add(word)
        return found

    def _in_ranges(self, word: str) -> bool:
        at = bisect_right(self.starts, word) - 1
        return at >= 0 and word < self.ends[at]

    def covered(self, index: _WordIndex) -> int:
        """How many words of ``index`` match some task word."""
        ordered = index.ordered
        in_ranges = sum(
            bisect_left(ordered, end) - bisect_left(ordered, start)
            for start, end in zip(self.starts, self.ends)
        )
        by_key = index.exact & self.keys
        return in_ranges + sum(1 for word in by_key if not self._in_ranges(word))


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillWithState:
    """One skill version offered for matching, with its computed state.

    ``status`` and ``health`` are computed from the skill's events elsewhere
    (skill_lifecycle); here they only filter and order, never authorise.
    """

    skill: Skill
    status: SkillStatus
    health: SkillHealth

    def __post_init__(self) -> None:
        if type(self.skill) is not Skill:
            raise SkillMatchError("skill must be a Skill")
        if type(self.status) is not SkillStatus:
            raise SkillMatchError("status must be a SkillStatus")
        if type(self.health) is not SkillHealth:
            raise SkillMatchError("health must be a SkillHealth")


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchCandidate:
    """One matched skill: how well it matched and why. Advice only."""

    skill: Skill
    kind: MatchKind
    score: int
    coverage: int
    reasons: tuple[str, ...]

    @property
    def skill_id(self) -> str:
        return self.skill.skill_id

    @property
    def version(self) -> int:
        return self.skill.version


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchResult:
    """``kind`` is ``best``'s kind, or ``none`` with no ``best``.

    ``best`` is always ``candidates[0]`` when there is one. Candidates list
    fits first, then similar skills; within each kind the order is score,
    coverage, success rate, last success (newest first), then skill_id.
    ``needs_revalidation`` lists in-scope stale skills that would have matched;
    they are never a fit and never ``best``.
    """

    kind: MatchKind
    best: MatchCandidate | None
    candidates: tuple[MatchCandidate, ...]
    needs_revalidation: tuple[MatchCandidate, ...]


def _in_scope(skill: Skill, project_id: str | None) -> bool:
    """Allowlist: global always; project only for the exact same project."""
    scope = skill.scope
    if scope.kind is SkillScopeKind.GLOBAL:
        return True
    if scope.kind is SkillScopeKind.PROJECT:
        return project_id is not None and scope.project_id == project_id
    return False


def _named(label: str, items: Iterable[str]) -> str:
    listed = list(items)
    shown = ", ".join(listed[:MAX_REASON_WORDS])
    more = len(listed) - MAX_REASON_WORDS
    return f"{label}: {shown}" + (f" (+{more})" if more > 0 else "")


def _assess(
    skill: Skill, task: _TaskWords
) -> tuple[MatchKind, int, int, tuple[str, ...]]:
    index = _skill_words(skill)
    body_hits = task.hits(index.body)
    matched = [word for word in task.words if word in body_hits]
    score = 100 * len(matched) // len(task.words)
    if score < SIMILAR_MIN_SCORE or len(matched) < SIMILAR_MIN_MATCHED:
        return MatchKind.NONE, score, 0, ()
    head_hits = task.hits(index.headline)
    in_headline = [word for word in matched if word in head_hits]
    head_size = len(index.headline.exact)
    covered = task.covered(index.headline)
    coverage = 100 * covered // head_size if head_size else 0
    fit = (
        score >= FIT_MIN_SCORE
        and len(matched) >= FIT_MIN_MATCHED
        and bool(in_headline)
        and coverage >= FIT_MIN_COVERAGE
    )
    kind = MatchKind.FIT if fit else MatchKind.SIMILAR
    reasons = [
        f"scope: {skill.scope.kind.value}",
        f"score {score}: {len(matched)} of {len(task.words)} task words matched",
        _named("matched words", matched),
    ]
    if in_headline:
        reasons.append(_named("in name or summary", in_headline))
    reasons.append(
        f"coverage {coverage}: {covered} of {head_size} name and summary words matched"
    )
    if kind is MatchKind.SIMILAR:
        if score < FIT_MIN_SCORE:
            reasons.append(f"not a fit: score below {FIT_MIN_SCORE}")
        if len(matched) < FIT_MIN_MATCHED:
            reasons.append(f"not a fit: fewer than {FIT_MIN_MATCHED} matched words")
        if not in_headline:
            reasons.append("not a fit: no task word in its name or summary")
        if coverage < FIT_MIN_COVERAGE:
            reasons.append(f"not a fit: coverage below {FIT_MIN_COVERAGE}")
    return kind, score, coverage, tuple(reasons)


def _micros(stamp: str | None) -> int | None:
    if stamp is None:
        return None
    return (datetime.fromisoformat(stamp) - _EPOCH) // timedelta(microseconds=1)


def _order_key(kind: MatchKind, score: int, coverage: int, entry: SkillWithState) -> tuple:
    health = entry.health
    # A never-used skill has no success rate: it ranks as 0, below any success.
    rate = (
        Fraction(health.success_count, health.use_count) if health.use_count else Fraction(0)
    )
    last = _micros(health.last_success_at)
    return (
        0 if kind is MatchKind.FIT else 1,
        -score,
        -coverage,
        -rate,
        (1, 0) if last is None else (0, -last),
        entry.skill.skill_id,
    )


def _entries(skills_with_state: Iterable[SkillWithState]) -> tuple[SkillWithState, ...]:
    if isinstance(skills_with_state, (str, bytes)) or not isinstance(
        skills_with_state, Iterable
    ):
        raise SkillMatchError("skills_with_state must be an iterable of SkillWithState")
    entries: list[SkillWithState] = []
    seen: set[str] = set()
    for entry in skills_with_state:
        if len(entries) >= MAX_SKILLS:
            raise SkillMatchError(f"more than {MAX_SKILLS} skills offered")
        if type(entry) is not SkillWithState:
            raise SkillMatchError("skills_with_state items must be SkillWithState")
        if entry.skill.skill_id in seen:
            # Two versions of one skill: which one is active is ambiguous.
            raise SkillMatchError("a skill is offered more than once")
        seen.add(entry.skill.skill_id)
        entries.append(entry)
    return tuple(entries)


def match_skills(
    task_text: str,
    skills_with_state: Iterable[SkillWithState],
    project_id: str | None,
) -> MatchResult:
    """Match a task against the offered skills; see the module docstring.

    Offer one entry per skill (its active version). Wrong argument types, a
    malformed ``project_id`` (Core's project id pattern), a skill offered
    twice or more than ``MAX_SKILLS`` skills raise ``SkillMatchError``.
    """
    if type(task_text) is not str:
        raise SkillMatchError("task_text must be a string")
    if project_id is not None and (
        type(project_id) is not str or PROJECT_ID_PATTERN.fullmatch(project_id) is None
    ):
        raise SkillMatchError(
            "project_id must be None or a project id (3-64 of a-z, 0-9 and -, "
            "not starting with -)"
        )
    entries = _entries(skills_with_state)
    task_words = words(cut_text(task_text, MAX_TASK_CHARS), MAX_TASK_WORDS)
    if not task_words:
        return MatchResult(kind=MatchKind.NONE, best=None, candidates=(), needs_revalidation=())
    task = _TaskWords.build(task_words)

    ranked: list[tuple[tuple, MatchCandidate]] = []
    stale: list[tuple[tuple, MatchCandidate]] = []
    for entry in entries:
        skill = entry.skill
        if not _in_scope(skill, project_id):
            continue
        if entry.status is SkillStatus.ACTIVE:
            target = ranked
        elif entry.status is SkillStatus.STALE:
            target = stale
        else:  # disabled, deleted, or anything unknown: never offered
            continue
        kind, score, coverage, reasons = _assess(skill, task)
        if kind is MatchKind.NONE:
            continue
        if target is stale:
            reasons = reasons + (
                f"stale: needs revalidation before use (would be {kind.value})",
            )
            kind = MatchKind.SIMILAR
        candidate = MatchCandidate(
            skill=skill, kind=kind, score=score, coverage=coverage, reasons=reasons
        )
        target.append((_order_key(kind, score, coverage, entry), candidate))

    ranked.sort(key=lambda pair: pair[0])
    stale.sort(key=lambda pair: pair[0])
    candidates = tuple(candidate for _, candidate in ranked[:MAX_CANDIDATES])
    best = candidates[0] if candidates else None
    return MatchResult(
        kind=MatchKind.NONE if best is None else best.kind,
        best=best,
        candidates=candidates,
        needs_revalidation=tuple(candidate for _, candidate in stale[:MAX_CANDIDATES]),
    )
