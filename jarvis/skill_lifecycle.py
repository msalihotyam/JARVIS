"""V2.2 skill lifecycle: versions, adaptation, rollback, health and status.

A skill is a procedure, never an authority, and nothing here changes that.
This module only builds immutable values and reads a skill's event history:

* ``adapt`` makes a NEW skill (new id, version 1, origin ``adapted``) that
  names its parent version; the parent is never changed.
* ``next_version`` makes the next version of the SAME skill (origin
  ``user_edit`` or ``version_after_failure``); older versions stay as they are.
* ``rollback_event`` returns the ``rolled_back`` event to append. Rolling back
  moves the active-version pointer; no version is ever rewritten or removed.
* ``health`` and ``status`` are computed from events every time; they are
  never stored on a skill.

Status precedence is deleted > disabled > stale > active. A skill is stale
after ``STALE_AFTER`` consecutive failures, or after a ``stale_marked`` event
(for example one that links a ``broken_workflow`` experience, see
``stale_mark_event``) that no success of the ACTIVE version has followed yet.
Only a success of the version active at that point clears stale (S12): a
success of an older, rolled-away version is counted in health but clears
nothing. ``use_events`` records a clearing success together with a
``revalidated`` event. Stale is a reason to ask, never a reason to skip a
check: a stale skill is not used automatically, and every step of any skill
still meets Core policy and approval as an ordinary chain step. Health and
status are history, never trust: no count, status or event lowers a tier,
skips an approval or pre-approves anything.

Only executed runs count (D16, D6/D10): a declined announcement ("I am using
skill X" refused by the user) or a Core approval the user denied is not a use
of the skill and never a failure. No event is recorded for it, so it changes
neither health nor stale; only ``used_success``/``used_failure`` events (one
per run that actually executed the skill) do.

Scope moves (D15, D8): the only allowed scope changes are none (the scope
stays as it is) and project -> global (the user's choice on the approval
card). A project skill, adaptation or version never moves to ANOTHER project,
and a GLOBAL one never moves into a project (so project A -> global ->
project B is refused at the second move). ``adapt``/``next_version`` enforce
it when building and ``SkillHistory`` enforces it again when reading saved
versions.

Which record to run: SKCORE and SKMATCH must take ``usable_skill(history)``
(None for a deleted or disabled skill); ``active_skill`` is a record lookup
for display only. ``adapt`` and ``next_version`` take the skill's
``history=`` so a deleted parent is refused; SKSTORE must always pass it.

Values built here are not saved: persistence, the secret/personal-data filter
on write and the user's explicit approval of a new skill or version belong to
the store and the draft flow. Events are applied in the order given (append
order), so they are passed as a tuple or list (or inside a ``SkillHistory``);
an unordered collection (set, frozenset, dict view, generator) is refused.
Timestamps are recorded, not used for ordering. Event ids/sequence numbers
(telling a retried duplicate row from a real second use) are SKSTORE's job:
it builds the tuple in its row order and never de-duplicates by value.

Errors: every refusal raises ``SkillLifecycleError`` (a ``SkillRecordError``),
except that ``adapt`` and ``next_version`` pass through ``SkillRecordError``
(bad ``now``, unknown key or malformed value in ``changes``) and
``AuthorityKeyError`` (an authority-like key anywhere in ``changes``) from
``jarvis.skill_types``. Callers catch ``SkillRecordError``, the common base.

Pure: stdlib and ``jarvis.skill_types`` only, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from uuid import uuid4

from jarvis.skill_types import (
    ID_PATTERN,
    MAX_VERSION,
    Experience,
    ExperienceKind,
    Skill,
    SkillEventKind,
    SkillHealth,
    SkillInput,
    SkillOrigin,
    SkillProvenance,
    SkillRecordError,
    SkillScope,
    SkillScopeKind,
    SkillStatus,
    SkillStep,
)

#: Consecutive failures after which a skill is stale (V2.2 plan, D10).
STALE_AFTER = 3

#: The procedure fields a change may set. Id, version and provenance are the
#: lifecycle's own; anything else (an authority-like key included) is refused.
CHANGEABLE_FIELDS = frozenset({"name", "summary", "scope", "inputs", "steps", "checks"})
_LIFECYCLE_FIELDS = frozenset({"skill_id", "version", "provenance"})
_PROCEDURE_FIELDS = ("name", "summary", "scope", "inputs", "steps", "checks")

_VERSION_ORIGINS = frozenset({SkillOrigin.USER_EDIT, SkillOrigin.VERSION_AFTER_FAILURE})
#: Events that must name a skill version: the one used, rolled back to or saved.
_VERSIONED_KINDS = frozenset(
    {
        SkillEventKind.USED_SUCCESS,
        SkillEventKind.USED_FAILURE,
        SkillEventKind.ROLLED_BACK,
        SkillEventKind.DRAFT_APPROVED,
    }
)
#: Events that move the active-version pointer.
_POINTER_KINDS = frozenset({SkillEventKind.DRAFT_APPROVED, SkillEventKind.ROLLED_BACK})
_TIMESTAMP_MAX = 40
#: How deep a ``changes`` value may nest (a skill needs about 4 levels); deeper
#: or cyclic data is refused before any recursion can run out.
_CHANGES_MAX_DEPTH = 32


class SkillLifecycleError(SkillRecordError):
    """A lifecycle operation was refused (fail closed)."""


def _check_id(value: Any, field: str) -> None:
    if type(value) is not str or ID_PATTERN.fullmatch(value) is None:
        raise SkillLifecycleError(f"{field} must be 1-64 characters of a-z, 0-9, _ or -")


def _check_version(value: Any, field: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_VERSION:
        raise SkillLifecycleError(f"{field} must be an integer from 1 to {MAX_VERSION}")


def _check_timestamp(value: Any, field: str) -> None:
    # The same single spelling skill_types accepts (``utc_now()``'s).
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
    raise SkillLifecycleError(f"{field} must be a UTC ISO-8601 timestamp as utc_now() writes it")


def _event_kind(value: Any) -> SkillEventKind | None:
    if type(value) is str:
        for member in SkillEventKind:
            if member.value == value:
                return member
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillEvent:
    """One thing that happened to a skill. History only, never an approval.

    ``version`` names the version used, rolled back to or saved (required for
    those kinds, optional otherwise). ``experience_id`` is only set on a
    ``stale_marked`` event, to link the experience that marked the skill.
    There is no free-text field, so an event can carry no secret.
    """

    skill_id: str
    kind: SkillEventKind
    version: int | None
    at: str
    experience_id: str | None = None

    def __post_init__(self) -> None:
        _check_id(self.skill_id, "event.skill_id")
        if type(self.kind) is not SkillEventKind:
            raise SkillLifecycleError("event.kind must be a SkillEventKind")
        if self.version is not None:
            _check_version(self.version, "event.version")
        elif self.kind in _VERSIONED_KINDS:
            raise SkillLifecycleError(f"a {self.kind.value} event names a version")
        _check_timestamp(self.at, "event.at")
        if self.experience_id is not None:
            if self.kind is not SkillEventKind.STALE_MARKED:
                raise SkillLifecycleError("only a stale_marked event links an experience")
            _check_id(self.experience_id, "event.experience_id")

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON data; ``from_dict`` is its strict inverse."""
        data = {item.name: getattr(self, item.name) for item in fields(self)}
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: Any) -> SkillEvent:
        """Exact key set, exact types, no coercion.

        Raises ``SkillLifecycleError`` (also for any bad field value).
        """
        if type(data) is not dict:
            raise SkillLifecycleError("SkillEvent must be an object")
        expected = {item.name for item in fields(cls)}
        if set(data) != expected:
            raise SkillLifecycleError(
                "SkillEvent must have exactly the keys " + ", ".join(sorted(expected))
            )
        kind = _event_kind(data["kind"])
        if kind is None:
            raise SkillLifecycleError("SkillEvent.kind is not a known event kind")
        return cls(
            skill_id=data["skill_id"],
            kind=kind,
            version=data["version"],
            at=data["at"],
            experience_id=data["experience_id"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillHistory:
    """Every saved version of ONE skill plus its events in append order.

    Refused (``SkillLifecycleError``) when ill formed: no version, another
    skill's version or event, a repeated version number, an event naming a
    version that is not here, or a version that moved its parent version's
    project procedure to ANOTHER project (D15; staying in the project or
    becoming global is allowed, D8). A version whose parent version is not
    held here cannot be checked against it and is accepted.
    """

    versions: tuple[Skill, ...]
    events: tuple[SkillEvent, ...]

    def __post_init__(self) -> None:
        if type(self.versions) is not tuple or not self.versions:
            raise SkillLifecycleError("a history holds a tuple of at least one version")
        if any(type(item) is not Skill for item in self.versions):
            raise SkillLifecycleError("history versions must be Skill records")
        if type(self.events) is not tuple or any(
            type(item) is not SkillEvent for item in self.events
        ):
            raise SkillLifecycleError("history events must be a tuple of SkillEvent")
        skill_id = self.versions[0].skill_id
        if any(item.skill_id != skill_id for item in self.versions + self.events):
            raise SkillLifecycleError("a history holds one skill only")
        numbers = [item.version for item in self.versions]
        if len(set(numbers)) != len(numbers):
            raise SkillLifecycleError("a history repeats a version")
        known = set(numbers)
        if any(item.version is not None and item.version not in known for item in self.events):
            raise SkillLifecycleError("an event names a version the history does not hold")
        by_number = {item.version: item for item in self.versions}
        for item in self.versions:
            origin = item.provenance
            if origin.parent_skill_id == skill_id and origin.parent_version in by_number:
                _check_scope_move(by_number[origin.parent_version].scope, item.scope)

    @property
    def skill_id(self) -> str:
        return self.versions[0].skill_id

    @property
    def latest_version(self) -> int:
        return max(item.version for item in self.versions)

    def get(self, version: int) -> Skill:
        """The saved record of ``version``; an unknown version raises
        ``SkillLifecycleError``."""
        for item in self.versions:
            if item.version == version:
                return item
        raise SkillLifecycleError(f"skill {self.skill_id} has no version {version!r}")


# -- building new skills and versions -------------------------------------


def _plain(value: Any, depth: int = 1) -> Any:
    if depth > _CHANGES_MAX_DEPTH:
        raise SkillLifecycleError("changes are nested too deeply (or are cyclic)")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (SkillScope, SkillInput, SkillStep)):
        return value.to_dict()
    if isinstance(value, (tuple, list)):
        return [_plain(item, depth + 1) for item in value]
    if type(value) is dict:
        return {key: _plain(item, depth + 1) for key, item in value.items()}
    return value


def _changed(base: Skill, changes: Any) -> dict[str, Any]:
    """``base`` as plain data with ``changes`` laid over it."""
    if not isinstance(changes, Mapping):
        raise SkillLifecycleError("changes must be a mapping of field to new value")
    if any(type(key) is not str for key in changes):
        raise SkillLifecycleError("change keys must be strings")
    lifecycle = _LIFECYCLE_FIELDS.intersection(changes)
    if lifecycle:
        raise SkillLifecycleError(
            "the lifecycle sets " + ", ".join(sorted(lifecycle)) + "; a change cannot"
        )
    data = base.to_dict()
    # Unknown keys, an authority-like key anywhere (AuthorityKeyError) and any
    # malformed value are refused by Skill.from_dict on the merged data.
    data.update({key: _plain(value) for key, value in changes.items()})
    return data


def _check_scope_move(parent: SkillScope, child: SkillScope) -> None:
    """D15/S8: the scope stays as it is, or a project procedure becomes global
    (D8, the user's choice). A project one never moves to another project and
    a global one never moves into a project."""
    if child.kind is not SkillScopeKind.PROJECT:
        return  # unchanged global, or project -> global
    if parent.kind is not SkillScopeKind.PROJECT:
        raise SkillLifecycleError(
            "a global skill stays global, it never moves into a project"
        )
    if child.project_id != parent.project_id:
        raise SkillLifecycleError(
            "a project skill may stay in its project or become global, "
            "never move to another project"
        )


def _check_in_history(skill: Skill, history: Any, what: str) -> SkillHistory:
    if type(history) is not SkillHistory:
        raise SkillLifecycleError("history must be a SkillHistory")
    if history.skill_id != skill.skill_id or history.get(skill.version) != skill:
        raise SkillLifecycleError(f"the {what} is not a saved version in this history")
    if status(history) is SkillStatus.DELETED:
        raise SkillLifecycleError(f"a deleted skill is not brought back (the {what} is deleted)")
    return history


def _same_procedure(a: Skill, b: Skill) -> bool:
    return all(getattr(a, name) == getattr(b, name) for name in _PROCEDURE_FIELDS)


def _build(
    base: Skill, changes: Any, skill_id: str, version: int, provenance: SkillProvenance
) -> Skill:
    try:
        data = _changed(base, changes)
    except RecursionError:  # belt and braces; _plain is depth-bounded
        raise SkillLifecycleError("changes are nested too deeply (or are cyclic)") from None
    data["skill_id"] = skill_id
    data["version"] = version
    data["provenance"] = provenance.to_dict()
    result = Skill.from_dict(data)
    _check_scope_move(base.scope, result.scope)
    if _same_procedure(base, result):
        raise SkillLifecycleError("nothing changed: the procedure equals its parent's")
    return result


def adapt(
    parent: Skill,
    changes: Mapping[str, Any],
    now: str,
    *,
    skill_id: str | None = None,
    source_run_id: str | None = None,
    history: SkillHistory | None = None,
) -> Skill:
    """A NEW skill made from ``parent``: a new id, version 1, origin ``adapted``.

    The provenance names ``parent``'s skill id and version; ``parent`` itself
    is untouched (records are immutable and nothing is written here). The new
    id is minted here unless given; it may not be the parent's. A project
    parent's adaptation stays in that project or becomes global; a global
    parent's adaptation stays global (D15). Given the parent's ``history``, a
    deleted parent is refused (D4); SKSTORE must always pass it. Saving the
    result still needs the user's explicit approval (S5).

    Raises ``SkillLifecycleError`` for every lifecycle refusal (bad parent,
    id, history, deleted parent, scope move, no-op, lifecycle key, nesting),
    ``SkillRecordError`` from ``jarvis.skill_types`` for a bad ``now``,
    ``source_run_id``, unknown key or malformed value, and its subclass
    ``AuthorityKeyError`` for an authority-like key anywhere in ``changes``.
    """
    if type(parent) is not Skill:
        raise SkillLifecycleError("adapt needs a saved Skill as its parent")
    if history is not None:
        _check_in_history(parent, history, "parent")
    new_id = str(uuid4()) if skill_id is None else skill_id
    _check_id(new_id, "skill_id")
    if new_id == parent.skill_id:
        raise SkillLifecycleError("an adaptation is a new skill with a new skill_id")
    provenance = SkillProvenance(
        origin=SkillOrigin.ADAPTED,
        source_run_id=source_run_id,
        parent_skill_id=parent.skill_id,
        parent_version=parent.version,
        created_at=now,
    )
    return _build(parent, changes, new_id, 1, provenance)


def next_version(
    skill: Skill,
    changes: Mapping[str, Any],
    origin: SkillOrigin | str,
    now: str,
    *,
    history: SkillHistory | None = None,
    source_run_id: str | None = None,
) -> Skill:
    """The next version of the SAME skill, made from ``skill``.

    ``origin`` is ``user_edit`` or ``version_after_failure``. The provenance
    names ``skill``'s version as the parent; ``skill`` and every older version
    stay as they are. The new number is ``skill.version + 1``, or, given the
    ``history``, one past its latest version (so editing an older, rolled-back
    version never collides); given the history a deleted skill gets no new
    version (D4). SKSTORE must always pass ``history``. A project version stays
    in its project or becomes global; a global version stays global (D15).
    Saving the result still needs the user's explicit approval (S5).

    Raises as ``adapt`` does (``SkillLifecycleError`` for lifecycle refusals
    incl. a bad origin; ``SkillRecordError``/``AuthorityKeyError`` passed
    through from ``jarvis.skill_types``).
    """
    if type(skill) is not Skill:
        raise SkillLifecycleError("next_version needs a saved Skill")
    if type(origin) is str:
        origin = next((item for item in SkillOrigin if item.value == origin), None)
    if type(origin) is not SkillOrigin or origin not in _VERSION_ORIGINS:
        raise SkillLifecycleError("a new version's origin is user_edit or version_after_failure")
    if history is None:
        number = skill.version + 1
    else:
        number = _check_in_history(skill, history, "skill").latest_version + 1
    _check_version(number, "version")
    provenance = SkillProvenance(
        origin=origin,
        source_run_id=source_run_id,
        parent_skill_id=skill.skill_id,
        parent_version=skill.version,
        created_at=now,
    )
    return _build(skill, changes, skill.skill_id, number, provenance)


# -- reading the history ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Replay:
    health: SkillHealth
    stale: bool
    deleted: bool
    disabled: bool


def _source(source: Any) -> tuple[tuple[SkillEvent, ...], int | None]:
    """The events in order plus, for a history, its latest version."""
    if type(source) is SkillHistory:
        return source.events, source.latest_version
    if type(source) not in (tuple, list):
        raise SkillLifecycleError(
            "events must be an ordered tuple or list of SkillEvent (or a SkillHistory); "
            "a set or other unordered collection is refused"
        )
    result = tuple(source)
    if any(type(item) is not SkillEvent for item in result):
        raise SkillLifecycleError("events must be SkillEvent values")
    if len({item.skill_id for item in result}) > 1:
        raise SkillLifecycleError("events of one skill only")
    return result, None


def _replay(events: tuple[SkillEvent, ...], latest: int | None) -> _Replay:
    """Walk the events once, tracking the active-version pointer.

    The active version at each point is the one named by the last
    ``draft_approved``/``rolled_back`` event so far; before any such event it
    is ``latest`` when a history without pointer events is given, otherwise
    the highest version named so far. Health/stale bookkeeping only: which
    version may RUN is ``usable_skill``'s (approved versions only).
    """
    has_pointer = any(event.kind in _POINTER_KINDS for event in events)
    fallback = latest if latest is not None and not has_pointer else 0
    pointer = None
    successes = failures = streak = 0
    last_success = last_failure = None
    marked = deleted = disabled = False
    for event in events:
        kind = event.kind
        if kind not in _POINTER_KINDS and event.version is not None:
            fallback = max(fallback, event.version)
        if kind in _POINTER_KINDS:
            pointer = event.version
        elif kind is SkillEventKind.USED_SUCCESS:
            successes += 1
            last_success = event.at
            if event.version == (fallback if pointer is None else pointer):
                streak = 0
                marked = False
        elif kind is SkillEventKind.USED_FAILURE:
            failures += 1
            streak += 1
            last_failure = event.at
        elif kind is SkillEventKind.STALE_MARKED:
            marked = True
        elif kind is SkillEventKind.DELETED:
            deleted = True
        elif kind is SkillEventKind.DISABLED:
            disabled = True
        elif kind is SkillEventKind.ENABLED:
            disabled = False
    return _Replay(
        health=SkillHealth(
            use_count=successes + failures,
            success_count=successes,
            failure_count=failures,
            consecutive_failures=streak,
            last_success_at=last_success,
            last_failure_at=last_failure,
        ),
        stale=marked or streak >= STALE_AFTER,
        deleted=deleted,
        disabled=disabled,
    )


def health(events: Sequence[SkillEvent] | SkillHistory) -> SkillHealth:
    """Use counters from ``used_success``/``used_failure`` events, in order.

    ``events`` is a tuple or list of one skill's events in append order, or
    its ``SkillHistory`` (exact active version, see ``status``).
    ``consecutive_failures`` counts the failures since the last success of
    the version active at that point (a success of an older version does not
    reset it, S12). Only executed runs count (D16): a declined announcement or
    a denied approval is not a use and never a failure; it records no event
    and changes nothing here.
    History only: a good record never vouches for a step.

    Raises ``SkillLifecycleError`` for anything else (a set, None, a mix of
    skills, a non-event).
    """
    return _replay(*_source(events)).health


def status(events: Sequence[SkillEvent] | SkillHistory) -> SkillStatus:
    """deleted > disabled > stale > active, from the events in order.

    ``events`` is a tuple or list of one skill's events in append order, or
    its ``SkillHistory``. Deleted is final (a tombstone; a later ``enabled``
    does not revive it). Disabled when the last ``enabled``/``disabled`` event
    is ``disabled``. Stale per the module rules; only a success of the active
    version clears it. Before the first ``draft_approved``/``rolled_back``
    event the active version is the highest version named so far, or, for a
    ``SkillHistory`` without any such event, its latest version (health
    bookkeeping only; such a skill is never run, see ``usable_skill``), so
    pass the history when you have it. A declined
    announcement or a denied approval is not a use and never a failure (D16);
    it records nothing.

    Raises ``SkillLifecycleError`` for anything else (a set, None, a mix of
    skills, a non-event).
    """
    state = _replay(*_source(events))
    if state.deleted:
        return SkillStatus.DELETED
    if state.disabled:
        return SkillStatus.DISABLED
    if state.stale:
        return SkillStatus.STALE
    return SkillStatus.ACTIVE


def approved_versions(history: SkillHistory) -> frozenset[int]:
    """The versions that have their own ``draft_approved`` event.
    Raises ``SkillLifecycleError`` for a non-history."""
    if type(history) is not SkillHistory:
        raise SkillLifecycleError("history must be a SkillHistory")
    return frozenset(
        event.version for event in history.events if event.kind is SkillEventKind.DRAFT_APPROVED
    )


def _active(history: SkillHistory) -> int | None:
    """The version named by the last ``draft_approved``/``rolled_back`` event
    when it has its own ``draft_approved`` event; None otherwise (no pointer,
    or a pointer at an unapproved row: fail closed, no fallback)."""
    pointer = None
    for event in history.events:
        if event.kind in _POINTER_KINDS:
            pointer = event.version
    return pointer if pointer in approved_versions(history) else None


def active_version(history: SkillHistory) -> int:
    """The version in use: the one named by the last ``draft_approved`` or
    ``rolled_back`` event, and only when it has its own ``draft_approved``
    event. There is no "latest row" fallback: a history without such a
    version raises ``SkillLifecycleError`` (as does a non-history)."""
    if type(history) is not SkillHistory:
        raise SkillLifecycleError("history must be a SkillHistory")
    version = _active(history)
    if version is None:
        raise SkillLifecycleError(f"skill {history.skill_id} has no approved active version")
    return version


def active_skill(history: SkillHistory) -> Skill:
    """The saved record of ``active_version(history)``, whatever the status.

    For display only (a deleted or disabled skill's record is returned too).
    SKCORE and SKMATCH must use ``usable_skill``. Raises
    ``SkillLifecycleError`` for a non-history.
    """
    version = active_version(history)
    return history.get(version)


def usable_skill(history: SkillHistory) -> Skill | None:
    """The record SKCORE/SKMATCH may run: the active version, or None for a
    deleted or disabled skill and for one without an approved active version
    (a row with no ``draft_approved`` event of its own is never run).

    A stale skill is returned (S12: revalidation is a normal, approved run);
    the caller still reads ``status(history)`` and never uses a stale skill
    automatically. Nothing returned here is an approval. Raises
    ``SkillLifecycleError`` for a non-history.
    """
    if type(history) is not SkillHistory:
        raise SkillLifecycleError("history must be a SkillHistory")
    if status(history) in (SkillStatus.DELETED, SkillStatus.DISABLED):
        return None
    version = _active(history)
    return None if version is None else history.get(version)


# -- events to append ------------------------------------------------------


def rollback_event(history: SkillHistory, target_version: int, now: str) -> SkillEvent:
    """The ``rolled_back`` event that makes ``target_version`` active again.

    Only the pointer moves: no version is rewritten or removed. Refused
    (``SkillLifecycleError``) for a non-history, a deleted skill, an unknown
    version, a version without its own ``draft_approved`` event and the
    version already active.
    """
    if type(history) is not SkillHistory:
        raise SkillLifecycleError("history must be a SkillHistory")
    if status(history) is SkillStatus.DELETED:
        raise SkillLifecycleError("a deleted skill cannot be rolled back")
    _check_version(target_version, "target_version")
    history.get(target_version)
    if target_version not in approved_versions(history):
        raise SkillLifecycleError(f"version {target_version} was never approved")
    if target_version == _active(history):
        raise SkillLifecycleError(f"version {target_version} is already the active version")
    return SkillEvent(
        skill_id=history.skill_id,
        kind=SkillEventKind.ROLLED_BACK,
        version=target_version,
        at=now,
    )


def use_events(
    history: SkillHistory, version: int, succeeded: bool, now: str
) -> tuple[SkillEvent, ...]:
    """The events that record one finished, EXECUTED use of ``version``.

    Call it only for a run that actually executed the skill (D16): a declined
    announcement or a Core approval the user denied is not a use and never a
    failure, so no event is recorded for it (``succeeded=False`` means the
    executed run failed). ``version`` is the version that run executed
    (normally the one ``usable_skill`` returned). A success of the ACTIVE
    version on a stale skill also yields ``revalidated`` (the stale state
    clears); a success of another, older version is recorded but clears
    nothing (S12). The failure that makes a skill stale also yields
    ``stale_marked``. A use is history, never an approval of the next use.

    Raises ``SkillLifecycleError`` for a non-history, a non-bool
    ``succeeded``, an unknown version or a deleted or disabled skill, and
    (from ``SkillEvent``) a bad ``now``.
    """
    if type(history) is not SkillHistory:
        raise SkillLifecycleError("history must be a SkillHistory")
    if type(succeeded) is not bool:
        raise SkillLifecycleError("succeeded must be a bool")
    _check_version(version, "version")
    history.get(version)
    before = status(history)
    if before in (SkillStatus.DELETED, SkillStatus.DISABLED):
        raise SkillLifecycleError(f"a {before.value} skill is not used")
    kind = SkillEventKind.USED_SUCCESS if succeeded else SkillEventKind.USED_FAILURE
    used = SkillEvent(skill_id=history.skill_id, kind=kind, version=version, at=now)
    after = _replay(history.events + (used,), history.latest_version)
    if succeeded:
        if before is SkillStatus.STALE and not after.stale:
            return used, SkillEvent(
                skill_id=history.skill_id,
                kind=SkillEventKind.REVALIDATED,
                version=version,
                at=now,
            )
        return (used,)
    if before is SkillStatus.ACTIVE and after.stale:
        return used, SkillEvent(
            skill_id=history.skill_id, kind=SkillEventKind.STALE_MARKED, version=version, at=now
        )
    return (used,)


def stale_mark_event(experience: Experience, now: str) -> SkillEvent:
    """The ``stale_marked`` event that links a ``broken_workflow`` experience
    to its skill. Refused (``SkillLifecycleError``) for a non-experience, any
    other kind and an experience without a skill."""
    if type(experience) is not Experience:
        raise SkillLifecycleError("stale_mark_event needs an Experience")
    if experience.kind is not ExperienceKind.BROKEN_WORKFLOW:
        raise SkillLifecycleError("only a broken_workflow experience marks a skill stale")
    if experience.skill_id is None:
        raise SkillLifecycleError("the experience names no skill")
    return SkillEvent(
        skill_id=experience.skill_id,
        kind=SkillEventKind.STALE_MARKED,
        version=None,
        at=now,
        experience_id=experience.experience_id,
    )
