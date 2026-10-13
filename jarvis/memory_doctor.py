"""Memory Doctor: pure health checks over a V2.1 typed-memory snapshot.

The store (DOCSTORE) reads the database once, read-only, and hands over a
:class:`MemoryDoctorSnapshot`. :func:`diagnose` only looks at that snapshot:
no SQL, no I/O, no clock reads, no model calls. Every finding may carry a
:class:`RepairProposal`, which is plain data; applying one is the explicit
user boundary's job (DOCREPAIR), one repair at a time. Nothing here mutates
a record (I8: corrections are status events and supersessions, never UPDATE
or DELETE).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from jarvis.contracts import (
    MemoryRecord,
    MemoryRecordKind,
    MemoryRecordStatus,
    canonical_utc_timestamp,
    normalize_symbol,
)

# A memory nobody re-verified for this long is reported as stale.
STALE_AFTER = timedelta(days=180)
# A proposal waiting for approval longer than this is reported.
PENDING_MAX_AGE = timedelta(days=14)
# An approved lesson nobody re-verified for this many days is proposed for expiry.
LESSON_FRESHNESS_DAYS = 90
# Newer records of these kinds can contradict an older lesson.
LESSON_CONTRADICTING_KINDS = frozenset(
    {MemoryRecordKind.DECISION, MemoryRecordKind.RULE, MemoryRecordKind.LESSON}
)

# Statuses that are, or may become, delivered to a model (I12). Only these
# are checked for content and scope problems.
LIVE_STATUSES = frozenset({MemoryRecordStatus.PENDING, MemoryRecordStatus.APPROVED})

# M1 ``project_memory_items.kind`` values with a V2.1 equivalent. The legacy
# v6 "preference" was already migrated to "fact" by M1 itself.
LEGACY_M1_KIND_MAP: dict[str, MemoryRecordKind] = {
    "fact": MemoryRecordKind.FACT,
    "decision": MemoryRecordKind.DECISION,
    "rule": MemoryRecordKind.RULE,
}


class FindingCode(StrEnum):
    ORPHAN_SCOPE = "ORPHAN_SCOPE"
    STALE = "STALE"
    CONFLICT_UNRESOLVED = "CONFLICT_UNRESOLVED"
    SUPERSESSION_CYCLE = "SUPERSESSION_CYCLE"
    DANGLING_SUPERSESSION = "DANGLING_SUPERSESSION"
    FTS_DRIFT = "FTS_DRIFT"
    PENDING_TOO_OLD = "PENDING_TOO_OLD"
    FILTER_VIOLATION = "FILTER_VIOLATION"
    LEGACY_M1_UNMAPPED = "LEGACY_M1_UNMAPPED"
    LESSON_STALE = "LESSON_STALE"
    LESSON_CONTRADICTED = "LESSON_CONTRADICTED"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RepairAction(StrEnum):
    """Closed set of repairs the user may approve; each maps to an existing
    insert-only store operation (status event, supersession, FTS rebuild)."""

    RETRACT = "retract"
    EXPIRE = "expire"
    SUPERSEDE = "supersede"
    REBUILD_FTS = "rebuild_fts"


@dataclass(frozen=True, slots=True)
class RepairProposal:
    """A suggested fix. Pure data; diagnose() never applies it.

    For SUPERSEDE, ``record_ids[0]`` is the record that stays and the rest
    are the ones it supersedes.
    """

    action: RepairAction
    record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    code: FindingCode
    severity: Severity
    record_ids: tuple[str, ...]
    summary_key: str
    repair: RepairProposal | None = None

    @property
    def finding_id(self) -> str:
        """Stable id so a UI can name the finding it wants repaired."""
        return f"{self.code.value}:{','.join(self.record_ids)}"


@dataclass(frozen=True, slots=True)
class DoctorRecordState:
    """One stored record with its current (latest event) status."""

    record: MemoryRecord
    status: MemoryRecordStatus

    def __post_init__(self) -> None:
        # The rules compare statuses by identity; a raw string would slip past.
        if type(self.record) is not MemoryRecord or type(self.status) is not MemoryRecordStatus:
            raise ValueError("DoctorRecordState needs a MemoryRecord and a MemoryRecordStatus")


@dataclass(frozen=True, slots=True)
class LegacyM1Item:
    """One row of the old ``project_memory_items`` table, read alongside."""

    item_id: str
    project_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class MemoryDoctorSnapshot:
    """Everything diagnose() looks at; produced read-only by the store.

    ``fts_row_count`` is the number of rows in ``memory_records_fts``; it
    must equal the number of records (every insert is indexed).
    """

    taken_at: str
    records: tuple[DoctorRecordState, ...]
    known_project_ids: frozenset[str]
    known_conversation_ids: frozenset[str]
    fts_row_count: int
    legacy_items: tuple[LegacyM1Item, ...] = field(default=())

    def __post_init__(self) -> None:
        taken_at = canonical_utc_timestamp(self.taken_at)
        if taken_at is None:
            raise ValueError("taken_at must be a timezone-aware timestamp")
        object.__setattr__(self, "taken_at", taken_at)


def diagnose(
    snapshot: MemoryDoctorSnapshot,
    *,
    is_storable: Callable[[str], bool],
) -> tuple[Finding, ...]:
    """Return every finding in ``snapshot``, in a deterministic order.

    ``is_storable`` is the never-store filter (memory_filter). It has no
    default so a caller cannot silently skip it; if it raises, the text
    counts as refused (fail-closed, I5).
    """
    now = _ts(snapshot.taken_at)
    by_id = {state.record.record_id: state for state in snapshot.records}
    findings: list[Finding] = []
    findings += _orphan_scope(snapshot)
    findings += _stale(snapshot.records, now)
    findings += _conflicts(snapshot.records)
    findings += _supersession_cycles(by_id)
    findings += _dangling_supersession(snapshot.records, by_id)
    findings += _fts_drift(snapshot)
    findings += _pending_too_old(snapshot.records, now)
    findings += _filter_violations(snapshot.records, is_storable)
    findings += _legacy_unmapped(snapshot.legacy_items)
    findings += _lesson_stale(snapshot.records, now)
    findings += _lesson_contradicted(snapshot.records, now)
    order = list(FindingCode)
    findings.sort(key=lambda f: (order.index(f.code), f.record_ids))
    return tuple(findings)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _live(records: Iterable[DoctorRecordState]) -> list[DoctorRecordState]:
    return [state for state in records if state.status in LIVE_STATUSES]


def _orphan_scope(snapshot: MemoryDoctorSnapshot) -> list[Finding]:
    found = []
    for state in _live(snapshot.records):
        scope = state.record.scope
        if scope.project_id in snapshot.known_project_ids and (
            scope.conversation_id is None
            or scope.conversation_id in snapshot.known_conversation_ids
        ):
            continue
        rid = (state.record.record_id,)
        found.append(Finding(
            FindingCode.ORPHAN_SCOPE, Severity.WARNING, rid,
            "memory_doctor.orphan_scope", RepairProposal(RepairAction.RETRACT, rid),
        ))
    return found


def _stale(records: Iterable[DoctorRecordState], now: datetime) -> list[Finding]:
    found = []
    for state in records:
        if state.status is not MemoryRecordStatus.APPROVED:
            continue
        record = state.record
        rid = (record.record_id,)
        if record.expires_at is not None and _ts(record.expires_at) <= now:
            found.append(Finding(
                FindingCode.STALE, Severity.WARNING, rid,
                "memory_doctor.stale.expired", RepairProposal(RepairAction.EXPIRE, rid),
            ))
        elif now - _ts(record.last_verified_at or record.created_at) > STALE_AFTER:
            found.append(Finding(
                FindingCode.STALE, Severity.INFO, rid, "memory_doctor.stale.unverified",
            ))
    return found


def _conflicts(records: Iterable[DoctorRecordState]) -> list[Finding]:
    # Grouped per project, not per conversation: a conversation-scoped record
    # is delivered alongside project-wide ones, so the two can contradict.
    groups: dict[tuple[str, MemoryRecordKind, str], list[MemoryRecord]] = {}
    for state in records:
        if state.status is MemoryRecordStatus.APPROVED:
            record = state.record
            key = (record.scope.project_id, record.kind, record.subject_key)
            groups.setdefault(key, []).append(record)
    found = []
    for group in groups.values():
        if len(group) < 2:
            continue
        # Newest first; the proposal keeps it and supersedes the others.
        group.sort(key=lambda r: (_ts(r.created_at), r.record_id), reverse=True)
        ids = tuple(r.record_id for r in group)
        found.append(Finding(
            FindingCode.CONFLICT_UNRESOLVED, Severity.WARNING, tuple(sorted(ids)),
            "memory_doctor.conflict_unresolved",
            RepairProposal(RepairAction.SUPERSEDE, ids),
        ))
    return found


def _supersession_cycles(by_id: dict[str, DoctorRecordState]) -> list[Finding]:
    # Each record names at most one predecessor, so following ``supersedes``
    # is a walk on a functional graph: every start either ends (missing or
    # None) or reaches a cycle. ``done`` makes the whole pass O(n).
    done: set[str] = set()
    cycles: set[tuple[str, ...]] = set()
    for start in sorted(by_id):
        path: list[str] = []
        on_path: dict[str, int] = {}
        current: str | None = start
        while current is not None and current in by_id and current not in done:
            if current in on_path:
                cycles.add(tuple(sorted(path[on_path[current]:])))
                break
            on_path[current] = len(path)
            path.append(current)
            current = by_id[current].record.supersedes
        done.update(path)
    return [
        Finding(FindingCode.SUPERSESSION_CYCLE, Severity.ERROR, ids,
                "memory_doctor.supersession_cycle")
        for ids in cycles
    ]


def _dangling_supersession(
    records: Iterable[DoctorRecordState], by_id: dict[str, DoctorRecordState]
) -> list[Finding]:
    found = []
    for state in records:
        target_id = state.record.supersedes
        if target_id is None:
            continue
        target = by_id.get(target_id)
        if target is not None and (
            target.record.scope.project_id == state.record.scope.project_id
        ):
            continue
        found.append(Finding(
            FindingCode.DANGLING_SUPERSESSION, Severity.ERROR,
            (state.record.record_id,), "memory_doctor.dangling_supersession",
        ))
    return found


def _fts_drift(snapshot: MemoryDoctorSnapshot) -> list[Finding]:
    if snapshot.fts_row_count == len(snapshot.records):
        return []
    return [Finding(
        FindingCode.FTS_DRIFT, Severity.WARNING, (), "memory_doctor.fts_drift",
        RepairProposal(RepairAction.REBUILD_FTS, ()),
    )]


def _pending_too_old(records: Iterable[DoctorRecordState], now: datetime) -> list[Finding]:
    found = []
    for state in records:
        if state.status is not MemoryRecordStatus.PENDING:
            continue
        if now - _ts(state.record.created_at) > PENDING_MAX_AGE:
            rid = (state.record.record_id,)
            found.append(Finding(
                FindingCode.PENDING_TOO_OLD, Severity.INFO, rid,
                "memory_doctor.pending_too_old", RepairProposal(RepairAction.EXPIRE, rid),
            ))
    return found


def _filter_violations(
    records: Iterable[DoctorRecordState], is_storable: Callable[[str], bool]
) -> list[Finding]:
    found = []
    for state in _live(records):
        try:
            storable = is_storable(state.record.text) is True
        except Exception:  # fail-closed (I5): a filter error is a refusal
            storable = False
        if storable:
            continue
        rid = (state.record.record_id,)
        found.append(Finding(
            FindingCode.FILTER_VIOLATION, Severity.ERROR, rid,
            "memory_doctor.filter_violation", RepairProposal(RepairAction.RETRACT, rid),
        ))
    return found


def _legacy_unmapped(items: Iterable[LegacyM1Item]) -> list[Finding]:
    return [
        Finding(
            FindingCode.LEGACY_M1_UNMAPPED, Severity.INFO, (item.item_id,),
            "memory_doctor.legacy_m1_unmapped",
        )
        for item in items
        if item.kind not in LEGACY_M1_KIND_MAP
    ]


def _lesson_stale(records: Iterable[DoctorRecordState], now: datetime) -> list[Finding]:
    found = []
    for state in records:
        record = state.record
        if state.status is not MemoryRecordStatus.APPROVED or record.kind is not MemoryRecordKind.LESSON:
            continue
        if record.expires_at is not None and _ts(record.expires_at) <= now:
            continue  # STALE already proposes expiring it
        if now - _ts(record.last_verified_at or record.created_at) > timedelta(days=LESSON_FRESHNESS_DAYS):
            rid = (record.record_id,)
            found.append(Finding(
                FindingCode.LESSON_STALE, Severity.INFO, rid,
                "memory_doctor.lesson_stale", RepairProposal(RepairAction.EXPIRE, rid),
            ))
    return found


def _lesson_contradicted(records: Iterable[DoctorRecordState], now: datetime) -> list[Finding]:
    # Same subject in the same scope (project and scope kind, never across
    # projects, I6); a newer approved record that says something else.
    # Already-expired records are left to STALE on both sides.
    groups: dict[tuple[str | None, str, str], list[MemoryRecord]] = {}
    for state in records:
        record = state.record
        if record.expires_at is not None and _ts(record.expires_at) <= now:
            continue
        if state.status is MemoryRecordStatus.APPROVED and record.kind in LESSON_CONTRADICTING_KINDS:
            key = (record.scope.project_id, record.scope.kind.value, record.subject_key)
            groups.setdefault(key, []).append(record)
    found = []
    for group in groups.values():
        for lesson in group:
            if lesson.kind is not MemoryRecordKind.LESSON:
                continue
            folded = normalize_symbol(lesson.text)
            newer = sorted(
                other.record_id for other in group
                if _ts(other.created_at) > _ts(lesson.created_at)
                and normalize_symbol(other.text) != folded
            )
            if newer:
                rid = (lesson.record_id,)
                found.append(Finding(
                    FindingCode.LESSON_CONTRADICTED, Severity.WARNING, (*rid, *newer),
                    "memory_doctor.lesson_contradicted", RepairProposal(RepairAction.EXPIRE, rid),
                ))
    return found
