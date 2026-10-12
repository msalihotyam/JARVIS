"""V2.1a M3: task-targeted selection over typed memory records (pure, no I/O).

Core hands in records with their current status (from the append-only event
log), the task query and the conversation's scope; this module decides which
records may be delivered and reports the ones a human should look at:

- only APPROVED, unexpired records that no valid newer record supersedes are
  eligible (FACT/LESSON are auto-APPROVED by Core and drop out once
  RETRACTED); pending/rejected/retracted/superseded/expired never are;
- scope is exact: the conversation's own project only, a conversation-scoped
  record only in that conversation, other projects only when the query's
  per-conversation ``global_readonly`` toggle is on - then every such record
  carries its source project as provenance;
- per (project, subject_key) the newest valid DECISION/RULE wins; the older
  ones are reported as ``conflicts`` and never chosen;
- score = relevance (caller's FTS input, else token overlap) x freshness decay
  (last_verified_at, else created_at) x confidence; ties break by record_id.

Records are untrusted data: nothing here grants authority, writes, or reads
text as instructions.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections.abc import Set
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import AbstractSet, Iterable

from jarvis.contracts import (
    MemoryRecord,
    MemoryRecordKind,
    MemoryRecordScope,
    MemoryRecordScopeKind,
    MemoryRecordStatus,
)

#: Freshness halves every this many days since the last verification.
FRESHNESS_HALF_LIFE = timedelta(days=90)
#: A valid record not verified for longer than this is reported as stale.
STALE_AFTER = timedelta(days=180)

_SUPERSEDING_KINDS = frozenset({MemoryRecordKind.DECISION, MemoryRecordKind.RULE})


@dataclass(frozen=True, slots=True)
class RecordWithStatus:
    """One record, its current event-log status and an optional FTS relevance
    (non-negative, higher is better; ``None`` -> token-overlap fallback)."""

    record: MemoryRecord
    status: MemoryRecordStatus
    relevance: float | None = None

    def __post_init__(self) -> None:
        if type(self.record) is not MemoryRecord:
            raise ValueError("record must be a MemoryRecord")
        if type(self.status) is not MemoryRecordStatus:
            raise ValueError("status must be a MemoryRecordStatus")
        # The range check alone refuses NaN, infinities and huge ints (no overflow).
        if self.relevance is not None and (
            type(self.relevance) not in (int, float)
            or not 0 <= self.relevance <= sys.float_info.max
        ):
            raise ValueError("relevance must be a finite number >= 0")


@dataclass(frozen=True, slots=True)
class TaskQuery:
    text: str
    global_readonly: bool = False

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise ValueError("query text must be a string")
        if type(self.global_readonly) is not bool:
            raise ValueError("global_readonly must be a bool")


@dataclass(frozen=True, slots=True)
class SelectedMemory:
    record: MemoryRecord
    score: float
    #: The source project when the record comes from another project through
    #: the global read-only toggle; ``None`` for the conversation's own project.
    source_project_id: str | None = None


@dataclass(frozen=True, slots=True)
class Selection:
    chosen: tuple[SelectedMemory, ...]
    stale: tuple[SelectedMemory, ...]
    conflicts: tuple[SelectedMemory, ...]
    orphans: tuple[SelectedMemory, ...]


def _tokens(text: str) -> frozenset[str]:
    folded = "".join(
        char
        for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    )
    return frozenset(t for t in re.split(r"[\W_]+", folded) if len(t) > 1)


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def select_for_task(
    records_with_status: Iterable[RecordWithStatus],
    query: TaskQuery,
    scope: MemoryRecordScope,
    *,
    now: datetime,
    limit: int,
    live_project_ids: AbstractSet[str],
    live_conversation_ids: AbstractSet[str],
) -> Selection:
    """Select at most ``limit`` records for one task. Bad input raises ValueError."""
    if type(query) is not TaskQuery:
        raise ValueError("query must be a TaskQuery")
    if type(scope) is not MemoryRecordScope:
        raise ValueError("scope must be a MemoryRecordScope")
    if type(now) is not datetime or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be an int >= 0")
    for live_ids in (live_project_ids, live_conversation_ids):
        if not isinstance(live_ids, Set) or any(type(i) is not str for i in live_ids):
            raise ValueError("live ids must be a set of strings")
    items = list(records_with_status)
    for item in items:
        if type(item) is not RecordWithStatus:
            raise ValueError("records_with_status must hold RecordWithStatus")
    if len({item.record.record_id for item in items}) != len(items):
        raise ValueError("records_with_status repeats a record_id")

    home = (
        scope.project_id if scope.kind is MemoryRecordScopeKind.PROJECT else None
    )

    def visible(record: MemoryRecord) -> bool:
        conversation = record.scope.conversation_id
        if conversation is not None and conversation != scope.conversation_id:
            return False
        return record.scope.project_id == home or query.global_readonly

    def valid(item: RecordWithStatus) -> bool:
        expires = item.record.expires_at
        return item.status is MemoryRecordStatus.APPROVED and (
            expires is None or _instant(expires) > now
        )

    by_id_in_project = {
        (item.record.scope.project_id, item.record.record_id): item.record
        for item in items
    }

    def supersedes(item: RecordWithStatus) -> bool:
        # A pointer counts only from a valid record this conversation can see,
        # to a record of the same project and normalized subject; a FACT/LESSON
        # (possibly model-made, auto-approved) never retires a DECISION/RULE.
        record = item.record
        if record.supersedes is None or not valid(item) or not visible(record):
            return False
        target = by_id_in_project.get((record.scope.project_id, record.supersedes))
        return (
            target is not None
            and target.subject_key == record.subject_key
            and (
                target.kind not in _SUPERSEDING_KINDS
                or record.kind in _SUPERSEDING_KINDS
            )
        )

    superseded = {
        (item.record.scope.project_id, item.record.supersedes)
        for item in items
        if supersedes(item)
    }
    eligible = [
        item
        for item in items
        if visible(item.record)
        and valid(item)
        and (item.record.scope.project_id, item.record.record_id) not in superseded
    ]

    query_tokens = _tokens(query.text)

    def labelled(item: RecordWithStatus) -> SelectedMemory:
        record = item.record
        if item.relevance is not None:
            relevance = float(item.relevance)
        elif not query_tokens:
            relevance = 1.0
        else:
            words = _tokens(record.text) | _tokens(record.subject_key)
            relevance = len(query_tokens & words) / len(query_tokens)
        verified = _instant(record.last_verified_at or record.created_at)
        age = max(now - verified, timedelta(0))
        freshness = 0.5 ** (age / FRESHNESS_HALF_LIFE)
        project = record.scope.project_id
        return SelectedMemory(
            record=record,
            score=relevance * freshness * record.confidence,
            source_project_id=None if project == home else project,
        )

    orphans: list[SelectedMemory] = []
    stale: list[SelectedMemory] = []
    live: list[RecordWithStatus] = []
    for item in eligible:
        record = item.record
        conversation = record.scope.conversation_id
        if record.scope.project_id not in live_project_ids or (
            conversation is not None and conversation not in live_conversation_ids
        ):
            orphans.append(labelled(item))
            continue
        live.append(item)
        verified = _instant(record.last_verified_at or record.created_at)
        if now - verified > STALE_AFTER:
            stale.append(labelled(item))

    # Newest valid DECISION/RULE per (project, subject) wins; the rest conflict.
    winners: dict[tuple[str | None, str], RecordWithStatus] = {}
    for item in live:
        record = item.record
        if record.kind not in _SUPERSEDING_KINDS:
            continue
        key = (record.scope.project_id, record.subject_key)
        best = winners.get(key)
        if best is None or (_instant(record.created_at), record.record_id) > (
            _instant(best.record.created_at),
            best.record.record_id,
        ):
            winners[key] = item
    conflicts: list[SelectedMemory] = []
    candidates: list[SelectedMemory] = []
    for item in live:
        record = item.record
        if (
            record.kind in _SUPERSEDING_KINDS
            and winners[(record.scope.project_id, record.subject_key)] is not item
        ):
            conflicts.append(labelled(item))
        else:
            candidates.append(labelled(item))

    ranked = sorted(
        (c for c in candidates if c.score > 0),
        key=lambda c: (-c.score, c.record.record_id),
    )

    def by_id(selected: list[SelectedMemory]) -> tuple[SelectedMemory, ...]:
        return tuple(sorted(selected, key=lambda s: s.record.record_id))

    return Selection(
        chosen=tuple(ranked[:limit]),
        stale=by_id(stale),
        conflicts=by_id(conflicts),
        orphans=by_id(orphans),
    )
