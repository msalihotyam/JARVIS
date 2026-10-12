"""V2.1 memory consolidation planner (pure; no I/O, no database).

``plan_consolidation`` reads typed records with their current status and
returns a plan of status events and supersession rows. It never deletes and
never applies anything: the store applies the plan (append-only events plus
supersession rows). Only live (approved or pending) records take part, so
planning again over an applied plan yields no new events.

Authority (I1/I3/I4): a decision, rule or preference that is approved is never
auto-superseded; the planner only proposes a conflict for the user. A pending
record never supersedes anything (not even another pending one), so input
awaiting approval cannot displace any record. A newer approved FACT/LESSON
does supersede older ones by design; that is retractable.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from jarvis.contracts import (
    MemoryRecord,
    MemoryRecordStatus,
    canonical_utc_timestamp,
    needs_user_approval,
)

_LIVE = frozenset({MemoryRecordStatus.APPROVED, MemoryRecordStatus.PENDING})


@dataclass(frozen=True, slots=True)
class ConsolidationEvent:
    record_id: str
    status: MemoryRecordStatus
    reason: str


@dataclass(frozen=True, slots=True)
class Supersession:
    record_id: str
    superseded_by: str
    reason: str


@dataclass(frozen=True, slots=True)
class Merge:
    kept_record_id: str
    merged_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConsolidationConflict:
    """A proposal for the user; the store never applies it."""

    newer_record_id: str
    older_record_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ConsolidationPlan:
    events: tuple[ConsolidationEvent, ...] = ()
    supersessions: tuple[Supersession, ...] = ()
    merges: tuple[Merge, ...] = ()
    conflicts: tuple[ConsolidationConflict, ...] = ()


def _timestamp(value: object, name: str) -> datetime:
    if canonical_utc_timestamp(value) is None:
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    return datetime.fromisoformat(value)  # type: ignore[arg-type]


def _normalized_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _age(item: tuple[MemoryRecord, MemoryRecordStatus]) -> tuple[datetime, str]:
    record = item[0]
    return _timestamp(record.created_at, "created_at"), record.record_id


def _rank(item: tuple[MemoryRecord, MemoryRecordStatus]) -> tuple:
    # Authority first, then newest: an approved record outranks a pending one.
    return item[1] is MemoryRecordStatus.APPROVED, *_age(item)


def _protected(item: tuple[MemoryRecord, MemoryRecordStatus]) -> bool:
    record, status = item
    return status is MemoryRecordStatus.APPROVED and needs_user_approval(record.kind)


def plan_consolidation(
    records_with_status: Iterable[tuple[MemoryRecord, MemoryRecordStatus]],
    *,
    now: str,
) -> ConsolidationPlan:
    """Plan dedup, supersession and expiry. Malformed input fails closed."""
    now_at = _timestamp(now, "now")
    seen: set[str] = set()
    groups: dict[tuple, list] = defaultdict(list)
    events: list[ConsolidationEvent] = []
    supersessions: list[Supersession] = []
    merges: list[Merge] = []
    conflicts: list[ConsolidationConflict] = []

    for pair in records_with_status:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("each entry must be a (MemoryRecord, MemoryRecordStatus) pair")
        record, status = pair
        if not isinstance(record, MemoryRecord) or not isinstance(status, MemoryRecordStatus):
            raise ValueError("each entry must be a (MemoryRecord, MemoryRecordStatus) pair")
        if record.record_id in seen:
            raise ValueError(f"duplicate record_id {record.record_id!r}")
        seen.add(record.record_id)
        if status not in _LIVE:
            continue
        if record.expires_at is not None and _timestamp(record.expires_at, "expires_at") < now_at:
            events.append(ConsolidationEvent(record.record_id, MemoryRecordStatus.EXPIRED, "expired"))
            continue
        scope = record.scope
        # Never merge across projects, conversations or classifications (I6).
        key = (scope.project_id, scope.conversation_id, record.kind, record.subject_key, record.classification)
        groups[key].append(pair)

    def supersede(old: MemoryRecord, new: MemoryRecord, reason: str) -> None:
        events.append(ConsolidationEvent(old.record_id, MemoryRecordStatus.SUPERSEDED, reason))
        supersessions.append(Supersession(old.record_id, new.record_id, reason))

    for group in groups.values():
        buckets: dict[str, list] = defaultdict(list)
        for item in group:
            buckets[_normalized_text(item[0].text)].append(item)

        survivors = []
        for bucket in buckets.values():
            bucket.sort(key=_rank)
            kept = bucket[-1]
            merged = []
            for loser in bucket[:-1]:
                if kept[1] is not MemoryRecordStatus.APPROVED or _protected(loser):
                    conflicts.append(
                        ConsolidationConflict(kept[0].record_id, loser[0].record_id, "duplicate")
                    )
                else:
                    supersede(loser[0], kept[0], "duplicate")
                    merged.append(loser[0].record_id)
            if merged:
                merges.append(Merge(kept[0].record_id, tuple(sorted(merged))))
            survivors.append(kept)

        if len(survivors) < 2:
            continue
        survivors.sort(key=_age)
        if needs_user_approval(survivors[0][0].kind):
            newest = survivors[-1]
            for older in survivors[:-1]:
                conflicts.append(
                    ConsolidationConflict(newest[0].record_id, older[0].record_id, "conflict")
                )
            continue
        approved = [item for item in survivors if item[1] is MemoryRecordStatus.APPROVED]
        if not approved:
            continue
        winner = approved[-1]
        for older in survivors[: survivors.index(winner)]:
            supersede(older[0], winner[0], "newer")

    return ConsolidationPlan(
        events=tuple(sorted(events, key=lambda e: e.record_id)),
        supersessions=tuple(sorted(supersessions, key=lambda s: s.record_id)),
        merges=tuple(sorted(merges, key=lambda m: m.kept_record_id)),
        conflicts=tuple(sorted(conflicts, key=lambda c: (c.newer_record_id, c.older_record_id))),
    )
