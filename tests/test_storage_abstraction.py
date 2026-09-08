from __future__ import annotations

import copy
import dataclasses
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from jarvis.contracts import (
    chain_id_refusal,
    LINEAR_LOOP_STAGES,
    MONTHLY_HARD_LIMIT_NANO_USD,
    OPEN_RESERVATION_STATES,
    PROJECT_DECISION_CANDIDATE_WINDOW,
    PROJECT_MEMORY_CANDIDATE_WINDOW,
    PROJECT_MEMORY_TEXT_CAP,
    RETRIEVAL_CANDIDATE_WINDOW,
    RETRIEVAL_SUMMARY_CAP,
    AttemptState,
    CancellationOutcome,
    BillingClaim,
    BillingClass,
    BillingIntegrityError,
    BillingReconciliation,
    APPROVAL_PENDABLE_JOB_RUN_STATUSES,
    CLAIMABLE_JOB_RUN_STATUSES,
    DISPATCHABLE_JOB_RUN_STATUSES,
    MAX_JOB_LEASE,
    MIN_JOB_LEASE,
    ChainState,
    ChainStep,
    DataClassification,
    DecisionRecord,
    ExecutionAttempt,
    ExecutionClaimOutcome,
    JobError,
    JobKind,
    JobRecord,
    JobRun,
    JobRunDispatch,
    JobRunLease,
    JobRunStatus,
    JobRunSummary,
    JobSpec,
    JobSummary,
    LedgerSnapshot,
    LoopStage,
    MemoryOrigin,
    MemoryRetrievalQuery,
    MonetaryState,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    ProjectMemoryItem,
    ProjectMemoryKind,
    ProjectRecord,
    ReconciliationOutcome,
    ReconciliationResult,
    ReservationState,
    SETTLEABLE_JOB_RUN_STATUSES,
    SETTLED_JOB_RUN_STATUSES,
    UNKNOWNABLE_JOB_RUN_STATUSES,
    VISIBILITY_MAX_LIMIT,
    RetrievedContext,
    RetrievedMemoryItem,
    RetrievedMemoryKind,
    RunStatus,
    memory_lifecycle_refusal,
    SessionState,
    Task,
    TaskStatus,
    UsageRecord,
    build_retrieved_context,
    canonical_usd,
    canonical_utc_timestamp,
    is_expired,
    ledger_month,
    nano_usd,
    normalize_retrieved_text,
    redact_credentials,
    retrieval_tokens,
    truncate_retrieved_text,
    redact_local_paths,
    usd_from_nano,
    utc_now,
)
from jarvis.core import AgentCore
from jarvis.memory import ledger_timestamp
from jarvis.registry import ToolRegistry
from jarvis.schedules import (
    Interval,
    OneTime,
    ScheduleError,
    latest_occurrence,
    next_occurrence,
)
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task


_BLOCKING_STATUSES = frozenset(
    {
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.CANCELLED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
    }
)
_CANCELLATION_STATUSES = frozenset(
    {TaskStatus.CANCEL_REQUESTED, TaskStatus.CANCELLED}
)
_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
    }
)


#: Ledger-relevant identity of a usage row; the storage-owned ``recorded_at``
#: is excluded so an identical replay of the same attempt stays idempotent.
_USAGE_IDENTITY_FIELDS = (
    "id",
    "session_id",
    "task_id",
    "provider",
    "model",
    "connector",
    "input_tokens",
    "output_tokens",
    "call_count",
    "estimated_cost_usd",
    "billing_class",
    "monetary_state",
    "monetary_cost_usd",
    "reference_cost_usd",
    "billing_evidence",
)


def _same_normalized_usage(left: UsageRecord, right: UsageRecord) -> bool:
    return all(
        getattr(left, name) == getattr(right, name)
        for name in _USAGE_IDENTITY_FIELDS
    )


def _authoritative_usage(
    record: dict, reconciliation: BillingReconciliation
) -> UsageRecord:
    """Storage-owned identity and time plus validated connector telemetry."""
    billing_class = reconciliation.billing_class
    actual = reconciliation.actual_nano_usd
    reference = None
    if billing_class is BillingClass.METERED_MONETARY:
        if reconciliation.monetary_state is not MonetaryState.MEASURED:
            raise BillingIntegrityError("metered actual is not measured")
        if isinstance(actual, bool) or not isinstance(actual, int) or actual < 0:
            raise BillingIntegrityError("metered actual is not a valid amount")
        monetary_cost = float(usd_from_nano(actual))
    elif billing_class in (
        BillingClass.SUBSCRIPTION_INCLUDED,
        BillingClass.NO_ADDITIONAL_CHARGE,
    ):
        if reconciliation.monetary_state is not MonetaryState.NOT_APPLICABLE:
            raise BillingIntegrityError("unbilled route is not not_applicable")
        if actual not in (None, 0):
            raise BillingIntegrityError("unbilled route carries a monetary amount")
        monetary_cost = None
        if reconciliation.reference_cost_usd is not None:
            reference = float(
                canonical_usd(reconciliation.reference_cost_usd, "reference_cost_usd")
            )
    else:
        raise BillingIntegrityError("unknown route cannot be reconciled")
    return UsageRecord(
        provider=reconciliation.provider,
        model=reconciliation.model,
        connector=reconciliation.connector,
        session_id=record["session_id"],
        task_id=record["task_id"],
        input_tokens=reconciliation.input_tokens,
        output_tokens=reconciliation.output_tokens,
        # One claimed execution is exactly one external call.
        call_count=1,
        estimated_cost_usd=float(
            canonical_usd(reconciliation.estimated_cost_usd, "estimated_cost_usd")
        ),
        billing_class=billing_class,
        monetary_state=reconciliation.monetary_state,
        monetary_cost_usd=monetary_cost,
        reference_cost_usd=reference,
        billing_evidence=reconciliation.billing_evidence,
        id=record["usage_id"],
        # Same contract as the SQLite store: the monetary month is the one the
        # claim fixed, never the wall clock at reconciliation.
        recorded_at=ledger_timestamp(record["ledger_month"], record["created_at"]),
    )


def force_running(store, run_id) -> None:
    """Place a generation in RUNNING - a state no phase in this build writes.

    Both stores are driven through it so the two implementations are asked
    exactly the same question. In the double it is a dict replace; in SQLite it
    is one direct UPDATE, deliberately bypassing every method under test.
    """
    if isinstance(store, InMemoryStore):
        store.job_runs[run_id] = dataclasses.replace(
            store.job_runs[run_id], status=JobRunStatus.RUNNING
        )
        return
    import sqlite3

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "UPDATE job_runs SET status = ? WHERE id = ?",
            (JobRunStatus.RUNNING.value, run_id),
        )
        connection.commit()
    finally:
        connection.close()


def settle(store, run_id, status, *, now, worker="settler"):
    """Claim ``run_id`` and settle it under exactly the lease just acquired.

    Phase 2B0 fenced ``settle_job_run``: an outcome may only be written by the
    worker that can still prove it owns the generation, so a test that merely
    wants a generation closed has to hold a real lease to close it. Both
    stores are driven through this same helper, which is the point - a double
    that let an unfenced settlement through would stop being a contract test.
    """
    held = store.claim_job_run(run_id, worker, now=now, lease=timedelta(minutes=5))
    return store.settle_job_run(
        run_id, status, held.owner_token, held.fencing_epoch, now=now
    )


class InMemoryStore:
    """Small non-SQLite MemoryStore implementation used as a contract test."""

    def __init__(self) -> None:
        # ONE critical section for the operations SQLite makes atomic with
        # BEGIN IMMEDIATE. Without it the double would admit two winners
        # where the real store admits one, and a contract test that is
        # weaker than production certifies a hole instead of closing it.
        self._lock = threading.RLock()
        self.sessions: dict[str, SessionState] = {}
        self.tasks: dict[str, Task] = {}
        self.task_sessions: dict[str, str] = {}
        self.pending: dict[tuple[str, str], PermissionRequest] = {}
        self.remembered: set[tuple[str, str]] = set()
        #: Recorded grants as mutable dicts, mirroring the SQLite grant table
        #: including one-shot consumption bookkeeping.
        self.grants: list[dict] = []
        self.attempts: dict[str, list[ExecutionAttempt]] = {}
        self.decisions: list[DecisionRecord] = []
        self.usage: dict[str, UsageRecord] = {}
        #: H3: attempt-bound billing/reservation records, mirroring the SQLite
        #: ``attempt_billing`` table including storage-owned usage identity.
        self.billing: dict[str, dict] = {}
        #: M1: project identities and INSERT-only memory items.
        self.projects: dict[str, ProjectRecord] = {}
        self.memory_items: list[ProjectMemoryItem] = []
        #: Scheduler phase 1B: durable job definitions and their generations.
        self.jobs: dict[str, JobRecord] = {}
        self.job_runs: dict[str, JobRun] = {}

    def save_session(self, session: SessionState) -> None:
        self.sessions[session.id] = copy.deepcopy(session)

    def load_session(self, session_id: str) -> SessionState | None:
        return copy.deepcopy(self.sessions.get(session_id))

    def save_task(self, session_id: str, task: Task) -> None:
        # Explicit setup primitive: stages state directly, like the SQLite
        # store's forced write.
        self.tasks[task.id] = copy.deepcopy(task)
        self.task_sessions[task.id] = session_id

    def _save_task_guarded(self, session_id: str, task: Task) -> None:
        """Terminal immutability: a stale writer cannot demote a terminal
        task, mirroring the SQLite guarded upsert."""
        existing = self.tasks.get(task.id)
        if existing is not None and existing.status in _TERMINAL_STATUSES:
            return
        self.save_task(session_id, task)

    def load_task(self, task_id: str) -> Task | None:
        return copy.deepcopy(self.tasks.get(task_id))

    def task_session_id(self, task_id: str) -> str | None:
        return self.task_sessions.get(task_id)

    def persist_checkpoint(
        self,
        session: SessionState,
        task: Task,
        event_type: str,
        payload: dict | None = None,
        pending_request: PermissionRequest | None = None,
    ) -> bool:
        # One consistent operation: if a terminal writer already won, nothing
        # is written at all - no task, no session, no pending approval.
        existing = self.tasks.get(task.id)
        if existing is not None and existing.status in _TERMINAL_STATUSES:
            return False
        self.save_session(session)
        self.save_task(session.id, task)
        if pending_request is not None:
            self.save_pending_approval(pending_request)
        return True

    def list_decisions(self, session_id: str) -> list[DecisionRecord]:
        return copy.deepcopy(
            [item for item in self.decisions if item.session_id == session_id]
        )

    def retrieve_context(self, query: MemoryRetrievalQuery) -> RetrievedContext:
        # Storage-agnostic: the shared pipeline does the scoring, ranking,
        # eligibility and truncation; this store only supplies the bounded
        # candidate window and the recovered classifications.
        current = None
        if query.exclude_task_id is not None:
            current_task = self.tasks.get(query.exclude_task_id)
            current = (
                current_task.scope.data_classification
                if current_task is not None
                else None
            )
        window = [
            record
            for record in self.decisions
            if record.session_id == query.session_id
            and record.task_id != query.exclude_task_id
        ]
        window.sort(key=lambda record: record.id)
        window.sort(key=lambda record: record.created_at, reverse=True)
        window = window[:RETRIEVAL_CANDIDATE_WINDOW]
        candidates = []
        for record in window:
            source = self.tasks.get(record.task_id)
            candidates.append(
                (
                    record,
                    source.scope.data_classification
                    if source is not None
                    else DataClassification.SECRET,
                )
            )
        project_candidates: tuple[ProjectMemoryItem, ...] = ()
        if query.project_id is not None:
            # Memory V2 parity: candidate acquisition is partitioned per
            # canonical kind (one bounded window each), so a flood of newer
            # records in one category cannot push the others out of the
            # candidate set before fairness interleaving runs.
            partitioned: list[ProjectMemoryItem] = []
            for kind in ProjectMemoryKind:
                same_kind = [
                    item
                    for item in self.memory_items
                    if item.project_id == query.project_id
                    and item.active
                    and item.kind is kind
                ]
                same_kind.sort(key=lambda item: item.id)
                same_kind.sort(key=lambda item: item.created_at, reverse=True)
                partitioned.extend(same_kind[:PROJECT_MEMORY_CANDIDATE_WINDOW])
            project_candidates = tuple(copy.deepcopy(partitioned))
            # M2 parity: decisions of the same project's OTHER sessions. A
            # decision whose session is missing or bound to another project
            # never enters; the querying session is excluded so the two
            # decision windows stay disjoint.
            cross = []
            for record in self.decisions:
                if record.session_id == query.session_id:
                    continue
                if record.task_id == query.exclude_task_id:
                    continue
                session = self.sessions.get(record.session_id)
                if session is None or session.project_id != query.project_id:
                    continue
                cross.append(record)
            cross.sort(key=lambda record: record.id)
            cross.sort(key=lambda record: record.created_at, reverse=True)
            for record in cross[:PROJECT_DECISION_CANDIDATE_WINDOW]:
                source = self.tasks.get(record.task_id)
                candidates.append(
                    (
                        copy.deepcopy(record),
                        source.scope.data_classification
                        if source is not None
                        else DataClassification.SECRET,
                    )
                )
        return build_retrieved_context(
            query, current, candidates, project_candidates=project_candidates
        )

    def reprove_retrieved_rules(
        self,
        project_id: str | None,
        retrieved: RetrievedContext,
        query_text: str = "",
    ) -> RetrievedContext:
        # Memory V2 parity with the SQLite store: a surviving RULE is
        # RECONSTRUCTED from the authoritative current record - no snapshot
        # semantic field (summary, rationale, next step, source binding,
        # classification, created_at) is ever trusted into a trusted RULE.
        # Relevance/truncation metadata is recomputed under the same
        # deterministic rules as retrieval; an unprovable claim demotes.
        if not any(
            type(item) is RetrievedMemoryItem
            and item.kind is RetrievedMemoryKind.RULE
            for item in retrieved.items
        ):
            return retrieved
        query_tokens = retrieval_tokens(query_text)
        items: list[RetrievedMemoryItem] = []
        for item in retrieved.items:
            if (
                type(item) is not RetrievedMemoryItem
                or item.kind is not RetrievedMemoryKind.RULE
            ):
                items.append(item)
                continue
            proof: ProjectMemoryItem | None = None
            if project_id is not None and item.record_id:
                for record in self.memory_items:
                    if (
                        record.id == item.record_id
                        and record.project_id == project_id
                        and record.kind is ProjectMemoryKind.RULE
                        and record.origin is MemoryOrigin.EXPLICIT_USER
                        and record.lifecycle.value == "active"
                    ):
                        proof = record
                        break
            if proof is None:
                items.append(
                    replace(
                        item,
                        kind=RetrievedMemoryKind.FACT,
                        origin=MemoryOrigin.INFERRED,
                    )
                )
                continue
            summary, summary_truncated = truncate_retrieved_text(
                normalize_retrieved_text(proof.text), RETRIEVAL_SUMMARY_CAP
            )
            items.append(
                RetrievedMemoryItem(
                    kind=RetrievedMemoryKind.RULE,
                    record_id=proof.id,
                    source_task_id=proof.project_id,
                    summary=summary,
                    rationale="",
                    next_step=None,
                    classification=proof.classification,
                    created_at=proof.created_at,
                    relevance_score=len(
                        query_tokens & retrieval_tokens(proof.text)
                    ),
                    truncated=summary_truncated,
                    origin=proof.origin,
                )
            )
        return replace(retrieved, items=tuple(items))

    def save_project(self, project: ProjectRecord) -> None:
        if project.id in self.projects:
            raise ValueError(f"Project already exists: {project.id}")
        self.projects[project.id] = copy.deepcopy(project)

    def load_project(self, project_id: str) -> ProjectRecord | None:
        return copy.deepcopy(self.projects.get(project_id))

    def list_projects(self) -> list[ProjectRecord]:
        return copy.deepcopy(
            sorted(self.projects.values(), key=lambda p: (p.created_at, p.id))
        )

    def save_memory_item(
        self, item: ProjectMemoryItem, *, supersedes: str | None = None
    ) -> ProjectMemoryItem:
        # The same write-boundary contract as the SQLite store: the generic
        # surface refuses explicit-user provenance outright (trusted
        # provenance is established only by ``save_explicit_user_memory_item``),
        # normalize, hard-reject oversize (never truncate), canonical hygiene
        # helpers, INSERT-only with supersede-only lifecycle flags.
        if item.origin is MemoryOrigin.EXPLICIT_USER:
            raise ValueError(
                "explicit-user provenance requires the trusted "
                "explicit-human memory add path"
            )
        if item.kind is ProjectMemoryKind.RULE:
            raise ValueError("a RULE requires explicit-user provenance")
        return self._persist_memory_item(item, supersedes=supersedes)

    def save_explicit_user_memory_item(
        self,
        project_id: str,
        kind: str,
        classification: str,
        text: str,
        *,
        supersedes: str | None = None,
    ) -> ProjectMemoryItem:
        # Trusted explicit-human boundary parity: EXPLICIT_USER is
        # established INTERNALLY, never accepted from the caller.
        project_kind = (
            ProjectMemoryKind.RULE
            if kind == "preference"
            else ProjectMemoryKind(kind)
        )
        item = ProjectMemoryItem(
            project_id=project_id,
            kind=project_kind,
            text=text,
            classification=DataClassification(classification),
            origin=MemoryOrigin.EXPLICIT_USER,
            origin_detail="cli-memory-add",
        )
        return self._persist_memory_item(item, supersedes=supersedes)

    def _persist_memory_item(
        self, item: ProjectMemoryItem, *, supersedes: str | None
    ) -> ProjectMemoryItem:
        text = normalize_retrieved_text(item.text)
        if not text:
            raise ValueError("memory text must be a non-blank string")
        if len(text) > PROJECT_MEMORY_TEXT_CAP:
            raise ValueError(
                f"memory text is longer than {PROJECT_MEMORY_TEXT_CAP} characters"
            )
        text, _ = redact_credentials(text)
        text = redact_local_paths(text)
        stored = replace(item, text=text, active=True, superseded_by=None)
        project = self.projects.get(stored.project_id)
        if project is None or not project.active:
            raise ValueError(f"Unknown or inactive project: {stored.project_id}")
        if any(existing.id == stored.id for existing in self.memory_items):
            raise ValueError(f"Memory item already exists: {stored.id}")
        old_index: int | None = None
        if supersedes is not None:
            for index, existing in enumerate(self.memory_items):
                if existing.id == supersedes:
                    old_index = index
                    break
            predecessor = (
                None if old_index is None else self.memory_items[old_index]
            )
            if (
                predecessor is None
                or predecessor.project_id != stored.project_id
                or memory_lifecycle_refusal(
                    predecessor.active, predecessor.superseded_by
                ) is not None
                or predecessor.active is not True
            ):
                raise ValueError(
                    "supersedes must reference an active memory item "
                    "of the same project"
                )
        self.memory_items.append(copy.deepcopy(stored))
        if old_index is not None:
            self.memory_items[old_index] = replace(
                self.memory_items[old_index],
                active=False,
                superseded_by=stored.id,
                updated_at=utc_now(),
            )
        return stored

    def list_memory_items(
        self, project_id: str, *, include_inactive: bool = False
    ) -> list[ProjectMemoryItem]:
        items = [
            item
            for item in self.memory_items
            if item.project_id == project_id
            and (include_inactive or item.active)
        ]
        items.sort(key=lambda item: (item.created_at, item.id))
        return copy.deepcopy(items)

    # -- Scheduler phase 1B parity ----------------------------------------
    #
    # The same durable job semantics without SQL: storage-minted identity,
    # revision compare-and-set, occurrence uniqueness, at most one unsettled
    # generation per job, a never-reissued generation counter, durable
    # disable, history-preserving archive, and bounded projections. Nothing
    # here materializes a task, requests an approval or executes anything -
    # exactly like the SQLite store, and for the same reason.

    def _job_for_update(self, job_id: str, expected_revision) -> JobRecord:
        if type(expected_revision) is not int or expected_revision < 1:
            raise JobError("expected_revision is not an exact positive integer")
        record = self.jobs.get(job_id)
        if record is None:
            raise JobError("no usable job with that identity")
        if record.revision != expected_revision:
            raise JobError("stale job revision")
        return record

    @staticmethod
    def _job_next_due(spec: JobSpec, after) -> str | None:
        occurrence = next_occurrence(spec.schedule, after)
        return None if occurrence is None else occurrence.isoformat()

    def _require_active_project(self, project_id: str) -> None:
        project = self.projects.get(project_id)
        if project is None or not project.active:
            raise JobError(f"Unknown or inactive project: {project_id}")

    def create_job(self, spec: JobSpec, *, now) -> JobRecord:
        if type(spec) is not JobSpec:
            raise JobError("spec is not an exact JobSpec")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        self._require_active_project(spec.project_id)
        record = JobRecord(spec=spec, next_due_at_utc=self._job_next_due(spec, now))
        self.jobs[record.id] = record
        return record

    def load_job(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)

    def edit_job(
        self, job_id: str, expected_revision: int, spec: JobSpec, *, now
    ) -> JobRecord:
        if type(spec) is not JobSpec:
            raise JobError("spec is not an exact JobSpec")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        record = self._job_for_update(job_id, expected_revision)
        if record.archived:
            raise JobError("an archived job cannot be edited")
        self._require_active_project(spec.project_id)
        updated = dataclasses.replace(
            record,
            spec=spec,
            revision=record.revision + 1,
            next_due_at_utc=self._job_next_due(spec, now) if record.enabled else None,
            updated_at=utc_now(),
        )
        self.jobs[job_id] = updated
        return updated

    def set_job_enabled(
        self, job_id: str, expected_revision: int, enabled: bool, *, now
    ) -> JobRecord:
        if type(enabled) is not bool:
            raise JobError("enabled is not an exact boolean")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        record = self._job_for_update(job_id, expected_revision)
        if record.archived:
            raise JobError("an archived job cannot change enablement")
        updated = dataclasses.replace(
            record,
            enabled=enabled,
            next_due_at_utc=(
                self._job_next_due(record.spec, now) if enabled else None
            ),
            updated_at=utc_now(),
        )
        self.jobs[job_id] = updated
        return updated

    def archive_job(self, job_id: str, expected_revision: int) -> JobRecord:
        record = self._job_for_update(job_id, expected_revision)
        if record.archived:
            raise JobError("the job is already archived")
        updated = dataclasses.replace(
            record,
            enabled=False,
            next_due_at_utc=None,
            archived_at=utc_now(),
            updated_at=utc_now(),
        )
        self.jobs[job_id] = updated
        return updated

    def record_job_run(self, job_id: str, nominal_due_at_utc: str) -> JobRun:
        if canonical_utc_timestamp(nominal_due_at_utc) is None:
            raise JobError("nominal_due_at_utc is not a canonical UTC timestamp")
        moment = datetime.fromisoformat(nominal_due_at_utc)
        record = self.jobs.get(job_id)
        if record is None:
            raise JobError("no usable job with that identity")
        if not record.schedulable:
            raise JobError("a job that is not schedulable records no generation")
        try:
            occurrence = latest_occurrence(record.spec.schedule, moment)
        except ScheduleError:
            occurrence = None
        if occurrence is None or occurrence != moment:
            raise JobError("the schedule does not name that instant")
        # Same rule as the SQLite store: the occurrence is stored in the
        # ENGINE'S spelling, so one instant can never become two rows.
        nominal_due_at_utc = occurrence.isoformat()
        mine = [run for run in self.job_runs.values() if run.job_id == job_id]
        if any(not run.settled for run in mine):
            raise JobError("an unsettled generation of this job is already open")
        if any(
            run.job_revision == record.revision
            and run.nominal_due_at_utc == nominal_due_at_utc
            for run in mine
        ):
            raise JobError("that occurrence has already been recorded")
        run = JobRun(
            job_id=record.id,
            job_revision=record.revision,
            generation=record.next_generation,
            nominal_due_at_utc=nominal_due_at_utc,
            spec=record.spec,
        )
        self.job_runs[run.id] = run
        self.jobs[job_id] = dataclasses.replace(
            record,
            last_run_id=run.id,
            next_generation=record.next_generation + 1,
            next_due_at_utc=self._job_next_due(record.spec, moment),
            updated_at=utc_now(),
        )
        return run

    # -- Scheduler phase 2A parity ----------------------------------------
    #
    # The same due-generation coalescing and the same claim / lease / fencing
    # semantics with no SQL under them, written independently of the SQLite
    # store rather than delegating to it: the invariants have to be the
    # STORE'S. The shared halves are the ones that must not be reimplemented -
    # the pure schedule kernel, and ``JobRun.lease_is_live`` /
    # ``JobRun.claimable_at``, which are the single definition of expiry and
    # claimability that two stores could otherwise disagree about.

    @staticmethod
    def _lease_expiry(lease, now) -> str:
        if type(lease) is not timedelta:
            raise JobError("lease is not a timedelta")
        if lease < MIN_JOB_LEASE or lease > MAX_JOB_LEASE:
            raise JobError("lease is outside the approved window")
        return (now + lease).isoformat()

    def _owned_run(self, run_id: str) -> JobRun:
        run = self.load_job_run(run_id)
        if run is None:
            raise JobError("no usable generation with that identity")
        return run

    @staticmethod
    def _held_lease(run: JobRun, owner_token, fencing_epoch, now) -> None:
        if type(fencing_epoch) is not int or isinstance(fencing_epoch, bool):
            raise JobError("fencing_epoch is not an exact integer")
        if run.settled:
            raise JobError("the generation is already settled")
        if run.owner_token is None:
            raise JobError("the generation carries no lease")
        if run.owner_token != owner_token or run.fencing_epoch != fencing_epoch:
            raise JobError("the caller does not hold this lease")
        if not run.lease_is_live(now):
            raise JobError("the lease has expired")

    @staticmethod
    def _lease_result(run: JobRun) -> JobRunLease:
        return JobRunLease(
            run_id=run.id,
            job_id=run.job_id,
            job_revision=run.job_revision,
            generation=run.generation,
            owner_token=run.owner_token,
            fencing_epoch=run.fencing_epoch,
            lease_expires_at_utc=run.lease_expires_at_utc,
            claimed_at=run.claimed_at,
        )

    def materialize_due_job_run(
        self, job_id: str, expected_revision: int, *, now
    ) -> JobRun | None:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        now = now.astimezone(timezone.utc)
        record = self._job_for_update(job_id, expected_revision)
        if not record.schedulable or record.next_due_at_utc is None:
            return None
        due = datetime.fromisoformat(record.next_due_at_utc)
        if now < due:
            return None
        try:
            occurrence = latest_occurrence(record.spec.schedule, now)
        except ScheduleError as exc:
            raise JobError("the schedule could not name an occurrence") from exc
        if occurrence is None or occurrence < due:
            return None
        nominal = occurrence.isoformat()
        mine = [run for run in self.job_runs.values() if run.job_id == job_id]
        if any(not run.settled for run in mine):
            return None
        if any(
            run.job_revision == record.revision
            and run.nominal_due_at_utc == nominal
            for run in mine
        ):
            return None
        run = JobRun(
            job_id=record.id,
            job_revision=record.revision,
            generation=record.next_generation,
            nominal_due_at_utc=nominal,
            spec=record.spec,
        )
        self.job_runs[run.id] = run
        self.jobs[job_id] = dataclasses.replace(
            record,
            last_run_id=run.id,
            next_generation=record.next_generation + 1,
            next_due_at_utc=self._job_next_due(record.spec, occurrence),
            updated_at=utc_now(),
        )
        return run

    def claim_job_run(self, run_id, owner_token, *, now, lease) -> JobRunLease:
        if chain_id_refusal(owner_token) is not None:
            raise JobError("owner_token is not a usable identifier")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        now = now.astimezone(timezone.utc)
        expires = self._lease_expiry(lease, now)
        run = self._owned_run(run_id)
        if not run.claimable_at(now):
            raise JobError("the generation is not claimable")
        claimed = dataclasses.replace(
            run,
            status=JobRunStatus.CLAIMED,
            owner_token=owner_token,
            fencing_epoch=run.fencing_epoch + 1,
            lease_expires_at_utc=expires,
            claimed_at=now.isoformat(),
        )
        self.job_runs[run_id] = claimed
        return self._lease_result(claimed)

    def renew_job_run_lease(
        self, run_id, owner_token, fencing_epoch, *, now, lease
    ) -> JobRunLease:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        now = now.astimezone(timezone.utc)
        expires = self._lease_expiry(lease, now)
        run = self._owned_run(run_id)
        self._held_lease(run, owner_token, fencing_epoch, now)
        renewed = dataclasses.replace(run, lease_expires_at_utc=expires)
        self.job_runs[run_id] = renewed
        return self._lease_result(renewed)

    def release_job_run(self, run_id, owner_token, fencing_epoch, *, now) -> JobRun:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        now = now.astimezone(timezone.utc)
        run = self._owned_run(run_id)
        self._held_lease(run, owner_token, fencing_epoch, now)
        if run.status not in CLAIMABLE_JOB_RUN_STATUSES:
            raise JobError("the generation is not releasable")
        released = dataclasses.replace(
            run,
            status=JobRunStatus.PENDING,
            owner_token=None,
            lease_expires_at_utc=None,
        )
        self.job_runs[run_id] = released
        return released

    def settle_job_run(
        self,
        run_id: str,
        status: JobRunStatus,
        owner_token: str,
        fencing_epoch: int,
        *,
        now,
        blocking_code: str | None = None,
        condition_result: str | None = None,
    ) -> JobRun:
        if type(status) is not JobRunStatus:
            raise JobError("status is not an exact JobRunStatus")
        if status not in SETTLED_JOB_RUN_STATUSES:
            raise JobError("a generation settles only into a settled status")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        now = now.astimezone(timezone.utc)
        run = self._owned_run(run_id)
        self._held_lease(run, owner_token, fencing_epoch, now)
        if run.status not in SETTLEABLE_JOB_RUN_STATUSES:
            raise JobError("the generation is not settleable from its state")
        settled = dataclasses.replace(
            run,
            status=status,
            blocking_code=blocking_code,
            condition_result=condition_result,
            settled_at=now.isoformat(),
        )
        self.job_runs[run_id] = settled
        return settled

    def _fenced_transition(
        self, run_id, owner_token, fencing_epoch, now, *, sources, target, refusal
    ) -> JobRun:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        now = now.astimezone(timezone.utc)
        run = self._owned_run(run_id)
        self._held_lease(run, owner_token, fencing_epoch, now)
        if run.status not in sources:
            raise JobError(refusal)
        moved = dataclasses.replace(
            run, status=target, owner_token=None, lease_expires_at_utc=None
        )
        self.job_runs[run_id] = moved
        return moved

    def mark_job_run_unknown(
        self, run_id, owner_token, fencing_epoch, *, now
    ) -> JobRun:
        return self._fenced_transition(
            run_id,
            owner_token,
            fencing_epoch,
            now,
            sources=UNKNOWNABLE_JOB_RUN_STATUSES,
            target=JobRunStatus.UNKNOWN_OUTCOME,
            refusal="the generation has no attempt whose outcome could be unknown",
        )

    def await_job_run_approval(
        self, run_id, owner_token, fencing_epoch, *, now
    ) -> JobRun:
        return self._fenced_transition(
            run_id,
            owner_token,
            fencing_epoch,
            now,
            sources=APPROVAL_PENDABLE_JOB_RUN_STATUSES,
            target=JobRunStatus.AWAITING_APPROVAL,
            refusal="the generation is not awaiting-approval from its state",
        )

    def bind_job_run_dispatch(
        self, run_id, owner_token, fencing_epoch, *, now
    ) -> JobRunDispatch:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise JobError("now is not an aware datetime")
        now = now.astimezone(timezone.utc)
        # EVERYTHING below is one atomic operation, exactly as it is in SQLite:
        # validate the fenced claim, mint the session and chain, bind both ids
        # and move CLAIMED -> RUNNING. A loser must leave no second session and
        # no replacement binding behind, so it must not be able to interleave
        # between the check and the writes.
        with self._lock:
            return self._bind_job_run_dispatch_locked(
                run_id, owner_token, fencing_epoch, now
            )

    def _bind_job_run_dispatch_locked(
        self, run_id, owner_token, fencing_epoch, now
    ) -> JobRunDispatch:
        run = self._owned_run(run_id)
        self._held_lease(run, owner_token, fencing_epoch, now)
        if run.status not in DISPATCHABLE_JOB_RUN_STATUSES:
            raise JobError("the generation is not dispatchable from its state")
        if run.session_id is not None or run.chain_id is not None:
            raise JobError("the generation is already bound to a session")
        if run.spec.kind is JobKind.CONDITION_WATCH:
            raise JobError("a condition watch is not dispatchable in this phase")
        project = self.projects.get(run.spec.project_id)
        if project is None or not project.active:
            raise JobError(f"Unknown or inactive project: {run.spec.project_id}")
        chain = ChainState.declared(run.spec.steps)
        if chain is None:
            raise JobError("the snapshot declaration cannot form a chain")
        session = SessionState(
            objective=run.spec.label,
            project_id=run.spec.project_id,
            chain=chain,
        )
        # Strict insert, and the unique-session rule the SQLite partial index
        # enforces - both stated here so the double refuses exactly what
        # SQLite refuses rather than quietly accepting more.
        if session.id in self.sessions:
            raise JobError("the session identity is already taken")
        if any(
            other.session_id == session.id
            for other in self.job_runs.values()
            if other.id != run_id
        ):
            raise JobError("that session is already bound to a generation")
        self.sessions[session.id] = copy.deepcopy(session)
        self.job_runs[run_id] = dataclasses.replace(
            run,
            status=JobRunStatus.RUNNING,
            session_id=session.id,
            chain_id=chain.id,
        )
        return JobRunDispatch(
            run_id=run.id,
            job_id=run.job_id,
            generation=run.generation,
            project_id=run.spec.project_id,
            session_id=session.id,
            chain_id=chain.id,
            owner_token=owner_token,
            fencing_epoch=fencing_epoch,
            lease_expires_at_utc=run.lease_expires_at_utc,
        )

    def load_job_run(self, run_id: str) -> JobRun | None:
        run = self.job_runs.get(run_id)
        if run is None:
            return None
        owner = self.jobs.get(run.job_id)
        if owner is None or run.job_revision > owner.revision:
            return None
        return run

    def list_jobs(
        self, limit: int, *, include_archived: bool = False
    ) -> list[JobSummary]:
        if type(limit) is not int or isinstance(limit, bool) or limit <= 0:
            return []
        bound = min(limit, VISIBILITY_MAX_LIMIT)
        records = [
            record
            for record in self.jobs.values()
            if include_archived or not record.archived
        ]
        records.sort(key=lambda record: (record.created_at, record.id))
        return [
            JobSummary(
                id=record.id,
                updated_at=record.updated_at,
                kind=record.spec.kind.value,
                label=record.spec.label,
                project_id=record.project_id,
                enabled=record.enabled,
                archived=record.archived,
                revision=record.revision,
                timezone=record.spec.timezone,
                next_due_at_utc=record.next_due_at_utc,
                last_run_id=record.last_run_id,
            )
            for record in records[:bound]
        ]

    def job_summary(self, job_id: str) -> JobSummary | None:
        record = self.jobs.get(job_id)
        if record is None:
            return None
        matches = [
            summary
            for summary in self.list_jobs(VISIBILITY_MAX_LIMIT, include_archived=True)
            if summary.id == job_id
        ]
        return matches[0] if matches else None

    def list_job_runs(self, job_id: str, limit: int) -> list[JobRunSummary]:
        if type(limit) is not int or isinstance(limit, bool) or limit <= 0:
            return []
        bound = min(limit, VISIBILITY_MAX_LIMIT)
        runs = [run for run in self.job_runs.values() if run.job_id == job_id]
        runs.sort(key=lambda run: run.generation, reverse=True)
        return [
            JobRunSummary(
                id=run.id,
                job_id=run.job_id,
                job_revision=run.job_revision,
                generation=run.generation,
                nominal_due_at_utc=run.nominal_due_at_utc,
                status=run.status.value,
                settled=run.settled,
                snapshot_digest=run.snapshot_digest,
                session_id=run.session_id,
                task_id=run.task_id,
                attempt_id=run.attempt_id,
                blocking_code=run.blocking_code,
                condition_result=run.condition_result,
                claimed=run.owner_token is not None,
                fencing_epoch=run.fencing_epoch,
                lease_expires_at_utc=run.lease_expires_at_utc,
            )
            for run in runs[:bound]
        ]

    def save_pending_approval(self, request: PermissionRequest) -> None:
        # Terminal immutability extends to approval issuance: no fresh
        # approval opportunity may open on an already-terminal task.
        task = self.tasks.get(request.task_id)
        if task is not None and task.status in _TERMINAL_STATUSES:
            return
        self.pending[(request.task_id, request.session_id)] = copy.deepcopy(request)

    def load_pending_approval(
        self, task_id: str, session_id: str
    ) -> PermissionRequest | None:
        return copy.deepcopy(self.pending.get((task_id, session_id)))

    def resolve_approval(
        self,
        request: PermissionRequest,
        grant: PermissionGrant,
        status: str,
    ) -> bool:
        key = (request.task_id, request.session_id)
        existing = self.pending.get(key)
        if existing is None or existing.id != request.id:
            # Already resolved or superseded: this contender lost.
            return False
        self.pending.pop(key, None)
        if status == "approved":
            self.grants.append(
                {
                    "session_id": request.session_id,
                    "task_id": grant.task_id,
                    "fingerprint": grant.fingerprint,
                    "remember_for_session": bool(grant.remember_for_session),
                    "expires_at": grant.expires_at,
                    "consumed_by_attempt": None,
                }
            )
            if grant.remember_for_session:
                self.remembered.add((request.session_id, request.scope_fingerprint))
        return True

    def reject_task(
        self,
        request: PermissionRequest,
        grant: PermissionGrant,
        session: SessionState,
        task: Task,
        payload: dict,
    ) -> bool:
        key = (request.task_id, request.session_id)
        existing = self.pending.get(key)
        if existing is None or existing.id != request.id:
            return False
        persisted = self.tasks.get(task.id)
        if persisted is None or persisted.status in _TERMINAL_STATUSES:
            return False
        self.pending.pop(key, None)
        self.save_session(session)
        self._save_task_guarded(session.id, task)
        return True

    def execution_cancellation_requested(
        self, session_id: str, task_id: str
    ) -> bool:
        # The narrow bit the owner-local stop watcher polls: authoritative
        # state only, never a persisted document. The double's authoritative
        # state is the stored Task and its ownership map, so it answers from
        # those and nothing else.
        task = self.tasks.get(task_id)
        if task is None or self.task_sessions.get(task_id) != session_id:
            return False
        return task.status is TaskStatus.CANCEL_REQUESTED

    def cancel_task(
        self, session_id: str, task_id: str
    ) -> CancellationOutcome:
        # Two ids in, rebound rows out: the caller supplies no Task and no
        # SessionState, so nothing it decoded can select or redefine a row.
        # The protocol also takes no "the connector confirmed the stop"
        # argument - nothing can hold that evidence, because nothing signals
        # a connector.
        session = self.load_session(session_id)
        if session is None:
            return CancellationOutcome("unknown_session")
        task = self.load_task(task_id)
        if task is None or self.task_session_id(task_id) != session_id:
            # One non-disclosing refusal for missing and foreign alike.
            return CancellationOutcome("not_found")
        if task.status in _TERMINAL_STATUSES:
            return CancellationOutcome("terminal", session, task)
        history = self.attempts.get(task_id, [])
        latest = history[-1] if history else None
        if latest is not None and latest.state is AttemptState.UNKNOWN_OUTCOME:
            # No cancellation verdict is truthful about an unknown outcome.
            return CancellationOutcome("in_doubt", session, task)
        task.cancellation_requested = True
        session.latest_task_id = task_id
        if latest is not None and latest.state is AttemptState.IN_FLIGHT:
            # Never terminal: the execution owner concludes a live attempt.
            task.status = TaskStatus.CANCEL_REQUESTED
            verdict = "cancelling"
        else:
            task.status = TaskStatus.CANCELLED
            task.stage = LoopStage.CANCELLED
            session.active_task_id = None
            task.progress = replace(task.progress, open_issues=())
            verdict = "cancelled"
        self.save_session(session)
        self._save_task_guarded(session_id, task)
        return CancellationOutcome(verdict, session, task)

    def has_remembered_scope_grant(
        self, session_id: str, scope_fingerprint: str
    ) -> bool:
        return (session_id, scope_fingerprint) in self.remembered

    def has_approved_grant(
        self, session_id: str, task_id: str, fingerprint: str
    ) -> bool:
        return any(
            grant["session_id"] == session_id
            and grant["task_id"] == task_id
            and grant["fingerprint"] == fingerprint
            and not is_expired(grant["expires_at"])
            for grant in self.grants
        )

    def latest_execution_attempt(self, task_id: str) -> ExecutionAttempt | None:
        history = self.attempts.get(task_id)
        return history[-1] if history else None

    def execution_attempts(self, task_id: str) -> list[ExecutionAttempt]:
        """The COMPLETE history, oldest first - never just the newest row.

        The double keeps the same shape SQLite does, so a reader deciding
        whether an effect is established sees an older unresolved attempt even
        when a clean one was written above it.
        """
        return list(self.attempts.get(task_id, ()))

    def open_execution_attempt(self, attempt: ExecutionAttempt) -> ExecutionAttempt:
        history = self.attempts.setdefault(attempt.task_id, [])
        if history:
            return history[-1]
        prepared = replace(attempt, state=AttemptState.PREPARED)
        history.append(prepared)
        return prepared

    def begin_execution(
        self,
        attempt_id: str,
        session_id: str,
        task_id: str,
        one_shot_fingerprints,
        billing: BillingClaim,
    ) -> ExecutionClaimOutcome:
        # Billing is never inferred from absence: an explicit, exactly typed
        # claim is required for every route, zero-monetary ones included.
        if type(billing) is not BillingClaim:
            raise BillingIntegrityError("billing claim: exact BillingClaim required")
        # The hard cap is storage-owned policy, never the caller's field.
        if billing.limit_nano_usd != MONTHLY_HARD_LIMIT_NANO_USD:
            raise BillingIntegrityError(
                "billing claim: hard limit is not the canonical policy value"
            )
        # Window A: an authoritative cancellation/terminal decision forbids start.
        task = self.tasks.get(task_id)
        if task is None or task.status in _BLOCKING_STATUSES:
            return ExecutionClaimOutcome.LOST
        history = self.attempts.get(task_id, [])
        for index, attempt in enumerate(history):
            if attempt.id != attempt_id:
                continue
            if attempt.state is not AttemptState.PREPARED:
                # P1-D: ownership before money. A contender that lost the
                # attempt never triggers a billing recheck.
                return ExecutionClaimOutcome.LOST
            # H3: the cap is re-derived and verified inside the claim, so a
            # ledger that moved after approval cannot be spent against.
            snapshot = self.ledger_snapshot(billing.ledger_month)
            if not snapshot.valid:
                return ExecutionClaimOutcome.LEDGER_INVALID
            if (
                billing.ledger_month != ledger_month()
                or snapshot.total_nano_usd != billing.expected_exposure_nano_usd
            ):
                return ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED
            projected = snapshot.total_nano_usd + billing.reservation_nano_usd
            if projected > MONTHLY_HARD_LIMIT_NANO_USD:
                fingerprint = billing.cost_override_fingerprint
                if not fingerprint or fingerprint not in tuple(
                    one_shot_fingerprints
                ):
                    return ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED
                if not any(
                    candidate["session_id"] == session_id
                    and candidate["task_id"] == task_id
                    and candidate["fingerprint"] == fingerprint
                    and not candidate["remember_for_session"]
                    and not is_expired(candidate["expires_at"])
                    and candidate["consumed_by_attempt"] is None
                    for candidate in self.grants
                ):
                    return ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED
            # Validate every required one-shot grant first (all-or-nothing).
            selected = []
            for fingerprint in one_shot_fingerprints:
                grant = next(
                    (
                        candidate
                        for candidate in self.grants
                        if candidate["session_id"] == session_id
                        and candidate["task_id"] == task_id
                        and candidate["fingerprint"] == fingerprint
                        and not candidate["remember_for_session"]
                        and not is_expired(candidate["expires_at"])
                        and candidate["consumed_by_attempt"] is None
                    ),
                    None,
                )
                if grant is None:
                    return ExecutionClaimOutcome.LOST
                selected.append(grant)
            for grant in selected:
                grant["consumed_by_attempt"] = attempt_id
            history[index] = replace(attempt, state=AttemptState.IN_FLIGHT)
            if attempt_id in self.billing:
                return ExecutionClaimOutcome.LOST
            self.billing[attempt_id] = {
                "usage_id": str(uuid4()),
                "created_at": utc_now(),
                "session_id": session_id,
                "task_id": task_id,
                "ledger_month": billing.ledger_month,
                "billing_class": billing.billing_class,
                "reserved_nano_usd": int(billing.reservation_nano_usd),
                "state": ReservationState.ACTIVE,
                "actual_nano_usd": None,
            }
            return ExecutionClaimOutcome.CLAIMED
        return ExecutionClaimOutcome.LOST

    def mark_reservation_unresolved(self, attempt_id: str) -> None:
        record = self.billing.get(attempt_id)
        if record is not None and record["state"] is ReservationState.ACTIVE:
            record["state"] = ReservationState.UNRESOLVED

    def reconcile_usage(
        self, reconciliation: BillingReconciliation
    ) -> ReconciliationResult:
        record = self.billing.get(reconciliation.attempt_id)
        if record is None:
            return ReconciliationResult(
                ReconciliationOutcome.MISSING, reason="no_reservation"
            )
        if reconciliation.billing_class is not record["billing_class"]:
            return ReconciliationResult(
                ReconciliationOutcome.CONFLICT, reason="route_mismatch"
            )
        try:
            usage = _authoritative_usage(record, reconciliation)
        except BillingIntegrityError as exc:
            return ReconciliationResult(
                ReconciliationOutcome.CONFLICT, reason=str(exc)
            )
        existing = self.usage.get(record["usage_id"])
        if existing is not None or record["state"] is ReservationState.RECONCILED:
            if existing is None:
                return ReconciliationResult(
                    ReconciliationOutcome.CONFLICT, reason="missing_usage_row"
                )
            if _same_normalized_usage(existing, usage):
                return ReconciliationResult(
                    ReconciliationOutcome.IDEMPOTENT, usage=copy.deepcopy(existing)
                )
            return ReconciliationResult(
                ReconciliationOutcome.CONFLICT, reason="conflicting_replay"
            )
        self.usage[usage.id] = copy.deepcopy(usage)
        record["state"] = ReservationState.RECONCILED
        record["actual_nano_usd"] = int(reconciliation.actual_nano_usd or 0)
        return ReconciliationResult(ReconciliationOutcome.RECONCILED, usage=usage)

    def mark_execution_unknown(self, attempt_id: str) -> None:
        for task_id, history in self.attempts.items():  # noqa: B007
            for index, attempt in enumerate(history):
                if attempt.id == attempt_id and attempt.state is AttemptState.IN_FLIGHT:
                    history[index] = replace(
                        attempt, state=AttemptState.UNKNOWN_OUTCOME
                    )
                    return

    def conclude_execution(
        self,
        session: SessionState,
        task: Task,
        attempt_id: str,
        event_type: str,
        payload: dict,
        *,
        finalizing_cancellation: bool = False,
    ) -> str:
        if not finalizing_cancellation:
            persisted = self.tasks.get(task.id)
            if persisted is not None and persisted.status in _CANCELLATION_STATUSES:
                return "cancelled"
        history = self.attempts.get(task.id, [])
        for index, attempt in enumerate(history):
            if attempt.id != attempt_id:
                continue
            if attempt.state is not AttemptState.IN_FLIGHT:
                return "lost"
            history[index] = replace(attempt, state=AttemptState.FINISHED)
            self.save_session(session)
            self._save_task_guarded(session.id, task)
            return "finished"
        return "lost"

    def append_usage(self, usage: UsageRecord) -> None:
        """Compatibility insertion: validates money, never overwrites an id."""
        if usage.monetary_cost_usd is not None:
            canonical_usd(usage.monetary_cost_usd, "monetary_cost_usd")
        if usage.reference_cost_usd is not None:
            canonical_usd(usage.reference_cost_usd, "reference_cost_usd")
        existing = self.usage.get(usage.id)
        if existing is not None:
            if _same_normalized_usage(existing, usage):
                return
            raise BillingIntegrityError("usage id collides with a different record")
        self.usage[usage.id] = copy.deepcopy(usage)

    def ledger_snapshot(self, month: str | None = None) -> LedgerSnapshot:
        month_key = month or ledger_month()
        measured = 0
        for item in self.usage.values():
            if not item.counts_toward_monetary_budget:
                continue
            if item.recorded_at[:7] != month_key:
                continue
            try:
                measured += nano_usd(item.monetary_cost_usd, "monetary_cost_usd")
            except BillingIntegrityError:
                return LedgerSnapshot.invalid(month_key, "malformed_measured_row")
        reserved = 0
        unresolved = 0
        for record in self.billing.values():
            if record["ledger_month"] != month_key:
                continue
            if record["state"] not in OPEN_RESERVATION_STATES:
                continue
            reserved += int(record["reserved_nano_usd"])
            if record["state"] is ReservationState.UNRESOLVED:
                unresolved += int(record["reserved_nano_usd"])
        return LedgerSnapshot(
            month=month_key,
            measured_nano_usd=measured,
            reserved_nano_usd=reserved,
            unresolved_nano_usd=unresolved,
        )

    def monthly_monetary_cost(self, month: str | None = None) -> float:
        snapshot = self.ledger_snapshot(month)
        if not snapshot.valid:
            raise BillingIntegrityError(f"ledger invalid: {snapshot.reason}")
        return float(snapshot.measured_usd)

    def finalize_task(
        self,
        session: SessionState,
        task: Task,
        decision: DecisionRecord | None,
        usage: UsageRecord,
        event_type: str,
        payload: dict,
    ) -> bool:
        # All-or-nothing with lifecycle authority: a terminal writer that
        # already won blocks the decision/usage/session writes entirely.
        existing = self.tasks.get(task.id)
        if existing is not None and existing.status in _TERMINAL_STATUSES:
            return False
        self.save_session(session)
        self.save_task(session.id, task)
        # ``None`` means this terminal task records no judgement to inherit.
        if decision is not None:
            self.decisions.append(copy.deepcopy(decision))
        # Idempotent: the authoritative row was already reconciled, so this
        # rewrite is a no-op rather than a second ledger entry.
        self.append_usage(usage)
        return True


class StorageAbstractionTests(unittest.TestCase):
    def test_agent_core_runs_with_non_sqlite_memory_store(self) -> None:
        registry = ToolRegistry()
        registry.register(DummyConnector())
        memory = InMemoryStore()
        core = AgentCore(
            registry,
            memory,
            Settings(permission_mode=PermissionMode.SMART_AUTO),
        )

        run = core.run(make_task())

        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(run.session.id, memory.task_session_id(run.task.id))
        self.assertEqual(1, len(memory.list_decisions(run.session.id)))
        # Progress enrichment rides inside the existing session/task objects;
        # the store above needed no new protocol methods for it.
        stored_task = memory.load_task(run.task.id)
        self.assertEqual(LINEAR_LOOP_STAGES, stored_task.progress.completed_stages)
        self.assertEqual((), stored_task.progress.open_issues)
        stored_session = memory.load_session(run.session.id)
        self.assertIsNone(stored_session.active_task_id)
        self.assertEqual(run.task.id, stored_session.latest_task_id)

    def test_project_memory_flows_through_a_non_sqlite_store(self) -> None:
        # M1 is storage-agnostic: identity, explicit memory and project-scoped
        # retrieval work through the protocol alone, with the shared pipeline
        # doing all scoring/eligibility work.
        registry = ToolRegistry()
        registry.register(DummyConnector())
        memory = InMemoryStore()
        memory.save_project(ProjectRecord(id="proj-a", name="Project A"))
        memory.save_memory_item(
            ProjectMemoryItem(
                project_id="proj-a",
                kind=ProjectMemoryKind.FACT,
                text="echo capability uses resource-a for this project",
                classification=DataClassification.INTERNAL,
            )
        )
        core = AgentCore(
            registry,
            memory,
            Settings(permission_mode=PermissionMode.SMART_AUTO),
        )
        task = make_task()
        task.project_id = "proj-a"
        run = core.run(task)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual("proj-a", run.session.project_id)
        stored_task = memory.load_task(run.task.id)
        kinds = {item.kind for item in stored_task.retrieved_context.items}
        self.assertIn(RetrievedMemoryKind.FACT, kinds)
        # An unknown project fails closed through the same protocol.
        ghost = make_task()
        ghost.project_id = "ghost-proj"
        refused = core.run(ghost)
        self.assertEqual(RunStatus.FAILED, refused.status)

    def test_cross_session_decisions_flow_through_a_non_sqlite_store(self) -> None:
        # M2 is storage-agnostic: a decision recorded by one session of a
        # project is retrievable from another session of the SAME project
        # through the protocol alone; a foreign project's session sees none.
        registry = ToolRegistry()
        registry.register(DummyConnector())
        memory = InMemoryStore()
        memory.save_project(ProjectRecord(id="proj-a", name="Project A"))
        memory.save_project(ProjectRecord(id="proj-b", name="Project B"))
        core = AgentCore(
            registry,
            memory,
            Settings(permission_mode=PermissionMode.SMART_AUTO),
        )
        first = make_task()
        first.project_id = "proj-a"
        origin = core.run(first)
        self.assertEqual(RunStatus.COMPLETED, origin.status)
        # A second, fresh session of the SAME project sees the decision.
        second = make_task()
        second.project_id = "proj-a"
        run = core.run(second)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertNotEqual(origin.session.id, run.session.id)
        stored = memory.load_task(run.task.id)
        cross_ids = {
            item.record_id
            for item in stored.retrieved_context.items
            if item.kind is RetrievedMemoryKind.DECISION
        }
        self.assertIn(memory.decisions[0].id, cross_ids)
        # A session of ANOTHER project never sees it.
        foreign = make_task()
        foreign.project_id = "proj-b"
        foreign_run = core.run(foreign)
        self.assertEqual(RunStatus.COMPLETED, foreign_run.status)
        foreign_stored = memory.load_task(foreign_run.task.id)
        self.assertEqual(
            set(),
            {
                item.record_id
                for item in foreign_stored.retrieved_context.items
            },
        )

    def test_retrieval_flows_through_a_non_sqlite_store(self) -> None:
        registry = ToolRegistry()
        registry.register(DummyConnector())
        memory = InMemoryStore()
        core = AgentCore(
            registry,
            memory,
            Settings(permission_mode=PermissionMode.SMART_AUTO),
        )
        session = core.start_session("retrieval abstraction")

        first = make_task()
        first.objective = "alpha shared marker"
        self.assertEqual(RunStatus.COMPLETED, core.run(first, session.id).status)

        second = make_task()
        second.objective = "alpha shared follow-up"
        run = core.run(second, session.id)

        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, len(run.task.retrieved_context.items))
        self.assertEqual(
            "alpha shared marker", run.task.retrieved_context.items[0].summary
        )
        self.assertEqual(1, run.task.context["retrieved_decision_count"])

    def test_expanded_billing_contract_is_storage_neutral(self) -> None:
        # H3: the fake store implements the ledger snapshot, the typed claim
        # with its reservation, and attempt-bound usage reconciliation without
        # any SQLite-specific behaviour.
        registry = ToolRegistry()
        connector = DummyConnector(name="metered", estimate_max=0.20, actual_cost=0.05)
        registry.register(connector)
        memory = InMemoryStore()
        core = AgentCore(
            registry,
            memory,
            Settings(permission_mode=PermissionMode.SMART_AUTO),
        )

        run = core.run(make_task())

        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)
        attempt = memory.latest_execution_attempt(run.task.id)
        record = memory.billing[attempt.id]
        self.assertIs(BillingClass.METERED_MONETARY, record["billing_class"])
        self.assertEqual(nano_usd("0.20"), record["reserved_nano_usd"])
        self.assertIs(ReservationState.RECONCILED, record["state"])
        self.assertEqual(nano_usd("0.05"), record["actual_nano_usd"])
        # Storage owns the ledger identity; the connector's id never appears.
        self.assertEqual(record["usage_id"], run.result.usage.id)
        snapshot = memory.ledger_snapshot()
        self.assertTrue(snapshot.valid)
        self.assertEqual(nano_usd("0.05"), snapshot.measured_nano_usd)
        self.assertEqual(0, snapshot.reserved_nano_usd)
        self.assertFalse(snapshot.has_unresolved_liability)
        self.assertAlmostEqual(0.05, memory.monthly_monetary_cost())

    def test_fake_store_refuses_a_conflicting_duplicate_usage_id(self) -> None:
        memory = InMemoryStore()
        first = UsageRecord(
            id="dup",
            billing_class=BillingClass.METERED_MONETARY,
            monetary_state=MonetaryState.MEASURED,
            monetary_cost_usd=1.00,
        )
        memory.append_usage(first)
        memory.append_usage(first)
        self.assertEqual(1, len(memory.usage))
        with self.assertRaises(BillingIntegrityError):
            memory.append_usage(
                UsageRecord(
                    id="dup",
                    billing_class=BillingClass.METERED_MONETARY,
                    monetary_state=MonetaryState.MEASURED,
                    monetary_cost_usd=9.99,
                )
            )
        with self.assertRaises(BillingIntegrityError):
            memory.append_usage(
                UsageRecord(
                    billing_class=BillingClass.METERED_MONETARY,
                    monetary_state=MonetaryState.MEASURED,
                    monetary_cost_usd=float("nan"),
                )
            )

    def test_fake_store_claim_refuses_a_moved_ledger(self) -> None:
        memory = InMemoryStore()
        session = SessionState(objective="claim")
        memory.save_session(session)
        task = make_task()
        memory.save_task(session.id, task)
        attempt = memory.open_execution_attempt(
            ExecutionAttempt(session.id, task.id, "metered", "fp")
        )
        stale = BillingClaim(
            ledger_month=ledger_month(),
            expected_exposure_nano_usd=nano_usd("5.00"),
            reservation_nano_usd=nano_usd("0.10"),
            limit_nano_usd=nano_usd("20.00"),
            billing_class=BillingClass.METERED_MONETARY,
            gate_state="gate",
        )
        self.assertIs(
            ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED,
            memory.begin_execution(attempt.id, session.id, task.id, [], stale),
        )
        self.assertEqual({}, memory.billing)
        self.assertIs(
            AttemptState.PREPARED, memory.latest_execution_attempt(task.id).state
        )


class SchedulerStorageParityTests(unittest.TestCase):
    """The non-SQLite double enforces the same durable job semantics.

    Scheduler phase 1B declares its storage surface on the provider-neutral
    ``MemoryStore`` contract, so the invariants have to be the STORE'S rather
    than SQLite's. These are the same proofs the SQLite suite makes, asked of
    an implementation with no database under it at all.
    """

    NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.memory = InMemoryStore()
        self.memory.save_project(ProjectRecord(id="proj-a", name="Project A"))

    def spec(self, *, label: str = "hourly digest", project_id: str = "proj-a"):
        return JobSpec(
            project_id=project_id,
            kind=JobKind.RECURRING,
            label=label,
            schedule=Interval(anchor=self.NOW, every=timedelta(hours=1)),
            steps=(
                ChainStep(
                    objective="summarize the log",
                    capability="echo",
                    action="read",
                    target="notes.txt",
                ),
            ),
        )

    def test_creation_is_a_record_of_intent_and_nothing_else(self) -> None:
        record = self.memory.create_job(self.spec(), now=self.NOW)
        self.assertEqual(1, record.revision)
        self.assertEqual(
            (self.NOW + timedelta(hours=1)).isoformat(), record.next_due_at_utc
        )
        self.assertEqual(record, self.memory.load_job(record.id))
        # No task, session, approval, grant, attempt, reservation or usage.
        self.assertEqual({}, self.memory.tasks)
        self.assertEqual({}, self.memory.sessions)
        self.assertEqual({}, self.memory.pending)
        self.assertEqual([], self.memory.grants)
        self.assertEqual({}, self.memory.attempts)
        self.assertEqual({}, self.memory.billing)
        self.assertEqual({}, self.memory.usage)
        with self.assertRaises(JobError):
            self.memory.create_job(self.spec(project_id="proj-missing"), now=self.NOW)

    def test_revision_compare_and_set_refuses_a_stale_writer(self) -> None:
        record = self.memory.create_job(self.spec(), now=self.NOW)
        edited = self.memory.edit_job(
            record.id, record.revision, self.spec(label="one"), now=self.NOW
        )
        self.assertEqual(2, edited.revision)
        with self.assertRaisesRegex(JobError, "stale job revision"):
            self.memory.edit_job(
                record.id, record.revision, self.spec(label="two"), now=self.NOW
            )
        self.assertEqual("one", self.memory.load_job(record.id).spec.label)

    def test_occurrence_identity_and_one_unsettled_generation_hold(self) -> None:
        record = self.memory.create_job(self.spec(), now=self.NOW)
        first = self.memory.record_job_run(record.id, record.next_due_at_utc)
        self.assertEqual(1, first.generation)
        current = self.memory.load_job(record.id)
        with self.assertRaisesRegex(JobError, "already open"):
            self.memory.record_job_run(record.id, current.next_due_at_utc)
        settle(self.memory, first.id, JobRunStatus.COMPLETED, now=self.NOW)
        with self.assertRaisesRegex(JobError, "already been recorded"):
            self.memory.record_job_run(record.id, first.nominal_due_at_utc)
        second = self.memory.record_job_run(record.id, current.next_due_at_utc)
        self.assertEqual(2, second.generation)
        # An instant the schedule does not name is not an occurrence.
        with self.assertRaisesRegex(JobError, "does not name"):
            self.memory.record_job_run(
                record.id, (self.NOW + timedelta(minutes=90)).isoformat()
            )

    def test_an_old_generation_snapshot_stays_immutable(self) -> None:
        record = self.memory.create_job(self.spec(), now=self.NOW)
        original = record.spec
        run = self.memory.record_job_run(record.id, record.next_due_at_utc)
        self.memory.edit_job(
            record.id, record.revision, self.spec(label="renamed"), now=self.NOW
        )
        reloaded = self.memory.load_job_run(run.id)
        self.assertEqual(original, reloaded.spec)
        self.assertEqual(original.digest, reloaded.snapshot_digest)
        self.assertEqual(1, reloaded.job_revision)

    def test_disable_enable_and_archive_behave_the_same(self) -> None:
        record = self.memory.create_job(self.spec(), now=self.NOW)
        disabled = self.memory.set_job_enabled(
            record.id, record.revision, False, now=self.NOW
        )
        self.assertIsNone(disabled.next_due_at_utc)
        with self.assertRaises(JobError):
            self.memory.record_job_run(record.id, record.next_due_at_utc)
        later = self.NOW + timedelta(hours=5, minutes=30)
        enabled = self.memory.set_job_enabled(
            record.id, disabled.revision, True, now=later
        )
        self.assertEqual(
            (self.NOW + timedelta(hours=6)).isoformat(), enabled.next_due_at_utc
        )
        run = self.memory.record_job_run(record.id, enabled.next_due_at_utc)
        archived = self.memory.archive_job(
            record.id, self.memory.load_job(record.id).revision
        )
        self.assertTrue(archived.archived)
        self.assertIsNone(archived.next_due_at_utc)
        # History is preserved, and the archived job stays reachable.
        self.assertIsNotNone(self.memory.load_job_run(run.id))
        self.assertEqual([], self.memory.list_jobs(10))
        self.assertEqual(
            [record.id],
            [s.id for s in self.memory.list_jobs(10, include_archived=True)],
        )

    def test_claim_lease_and_fencing_match_the_sqlite_store_exactly(self) -> None:
        """The SAME phase-2A script, run against both stores, agrees.

        Written as one script over two stores rather than as two suites: a
        semantic difference between the SQLite implementation and the double
        can only be caught by asking them the same questions in the same order
        and comparing the answers, not by asserting each one separately
        against what its own author expected.

        Every instant is EXPLICIT, so the comparison is deterministic and no
        step sleeps.
        """
        import tempfile
        from pathlib import Path

        from jarvis.memory import SQLiteMemoryStore
        from tests.helpers import safe_cleanup

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        sqlite_store = SQLiteMemoryStore(Path(temp.name) / "parity.db")
        sqlite_store.save_project(ProjectRecord(id="proj-a", name="Project A"))

        lease = timedelta(minutes=5)
        # Two whole periods late: both stores must COALESCE to one generation
        # at the latest due occurrence, not replay the one they missed.
        late = self.NOW + timedelta(hours=2, minutes=30)

        def script(store) -> list:
            trace: list = []
            record = store.create_job(self.spec(), now=self.NOW)
            self.assertIsNone(
                store.materialize_due_job_run(
                    record.id, record.revision, now=self.NOW
                )
            )
            run = store.materialize_due_job_run(
                record.id, record.revision, now=late
            )
            trace.append(("generation", run.generation, run.nominal_due_at_utc))
            trace.append(
                ("next_due", store.load_job(record.id).next_due_at_utc)
            )
            first = store.claim_job_run(run.id, "worker-a", now=late, lease=lease)
            trace.append(("claim", first.fencing_epoch, first.lease_expires_at_utc))
            # A live lease cannot be stolen.
            with self.assertRaises(JobError):
                store.claim_job_run(run.id, "worker-b", now=late, lease=lease)
            # A stale epoch cannot renew, the true owner can.
            with self.assertRaises(JobError):
                store.renew_job_run_lease(
                    run.id, "worker-a", first.fencing_epoch - 1,
                    now=late + timedelta(minutes=1), lease=lease,
                )
            renewed = store.renew_job_run_lease(
                run.id, "worker-a", first.fencing_epoch,
                now=late + timedelta(minutes=1), lease=lease,
            )
            trace.append(
                ("renew", renewed.fencing_epoch, renewed.lease_expires_at_utc)
            )
            # Once expired, a different worker takes over with a NEWER epoch
            # and the displaced owner is fenced out.
            expired = late + timedelta(minutes=30)
            taken = store.claim_job_run(
                run.id, "worker-b", now=expired, lease=lease
            )
            trace.append(("takeover", taken.fencing_epoch, taken.owner_token))
            with self.assertRaises(JobError):
                store.renew_job_run_lease(
                    run.id, "worker-a", first.fencing_epoch,
                    now=expired, lease=lease,
                )
            released = store.release_job_run(
                run.id, "worker-b", taken.fencing_epoch, now=expired
            )
            trace.append(
                (
                    "release",
                    released.status.value,
                    released.owner_token,
                    released.lease_expires_at_utc,
                    released.fencing_epoch,
                )
            )
            reloaded = store.load_job_run(run.id)
            trace.append(("reload", reloaded.status.value, reloaded.fencing_epoch))
            summary = store.list_job_runs(record.id, 10)[0]
            trace.append(
                (
                    "summary",
                    summary.claimed,
                    summary.fencing_epoch,
                    summary.lease_expires_at_utc,
                )
            )
            # An uncertain outcome fails CLOSED in both stores. Closing a
            # generation is FENCED in both too, so each settlement here is
            # made by a worker holding the lease it settles under.
            settle(store, run.id, JobRunStatus.CANCELLED, now=expired)
            blocked = store.materialize_due_job_run(
                record.id, store.load_job(record.id).revision, now=expired
            )
            settle(store, blocked.id, JobRunStatus.COMPLETED, now=expired)
            trace.append(("blocked_generation", blocked.generation))
            return trace

        self.assertEqual(script(self.memory), script(sqlite_store))

    def test_concluding_a_generation_is_fenced_identically_in_both_stores(
        self,
    ) -> None:
        # Phase 2B0. The double is a CONTRACT test, so a double that let an
        # unfenced settlement - or an unfenced unknown/await transition -
        # through would quietly certify the very hole this phase closed. The
        # same adversarial script runs against both stores and the refusals
        # themselves are part of the compared trace.
        import tempfile
        from pathlib import Path

        from jarvis.memory import SQLiteMemoryStore
        from tests.helpers import safe_cleanup

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        sqlite_store = SQLiteMemoryStore(Path(temp.name) / "fencing.db")
        sqlite_store.save_project(ProjectRecord(id="proj-a", name="Project A"))

        lease = timedelta(minutes=5)
        at = self.NOW + timedelta(hours=1)
        during = at + timedelta(minutes=1)
        after = at + lease + timedelta(minutes=1)

        def refusal(call) -> str:
            try:
                call()
            except JobError:
                return "refused"
            return "ALLOWED"

        def script(store) -> list:
            trace: list = []
            record = store.create_job(self.spec(), now=self.NOW)
            run = store.materialize_due_job_run(record.id, record.revision, now=at)
            held = store.claim_job_run(run.id, "worker-a", now=at, lease=lease)
            # Every shape of stale proof, refused the same way by both.
            for owner, epoch, when in (
                ("worker-b", held.fencing_epoch, during),
                ("worker-a", held.fencing_epoch - 1, during),
                ("worker-a", held.fencing_epoch + 1, during),
                ("worker-a", held.fencing_epoch, after),
            ):
                trace.append(
                    (
                        "settle_refused",
                        refusal(
                            lambda o=owner, e=epoch, w=when: store.settle_job_run(
                                run.id, JobRunStatus.COMPLETED, o, e, now=w
                            )
                        ),
                    )
                )
            # A CLAIMED generation is bound to no attempt, so both stores
            # refuse to call its outcome uncertain AND refuse to park it for an
            # approval: there is nothing yet to be uncertain about or to
            # approve.
            trace.append(
                (
                    "unknown_from_claimed",
                    refusal(
                        lambda: store.mark_job_run_unknown(
                            run.id, "worker-a", held.fencing_epoch, now=during
                        )
                    ),
                )
            )
            trace.append(
                (
                    "park_from_claimed",
                    refusal(
                        lambda: store.await_job_run_approval(
                            run.id, "worker-a", held.fencing_epoch, now=during
                        )
                    ),
                )
            )
            # An approval requirement is DISCOVERED by an attempt that is
            # already under way, so parking starts from RUNNING in both stores.
            force_running(store, run.id)
            parked = store.await_job_run_approval(
                run.id, "worker-a", held.fencing_epoch, now=during
            )
            trace.append(
                (
                    "parked",
                    parked.status.value,
                    parked.owner_token,
                    parked.lease_expires_at_utc,
                    parked.fencing_epoch,
                    parked.settled,
                    parked.settled_at,
                )
            )
            # Parked: unclaimable, unsettleable, and it blocks the job.
            trace.append(
                (
                    "parked_claim",
                    refusal(
                        lambda: store.claim_job_run(
                            run.id, "worker-b", now=after, lease=lease
                        )
                    ),
                )
            )
            trace.append(
                (
                    "parked_settle",
                    refusal(
                        lambda: store.settle_job_run(
                            run.id,
                            JobRunStatus.COMPLETED,
                            "worker-a",
                            held.fencing_epoch,
                            now=during,
                        )
                    ),
                )
            )
            trace.append(
                (
                    "parked_blocks",
                    store.materialize_due_job_run(
                        record.id,
                        store.load_job(record.id).revision,
                        now=at + timedelta(days=365),
                    ),
                )
            )
            return trace

        self.assertEqual(script(self.memory), script(sqlite_store))

    def test_dispatch_binding_is_refused_identically_in_both_stores(self) -> None:
        # Phase 2B1. The double is a CONTRACT test, so it must refuse exactly
        # what SQLite refuses: the same fencing proof, the same CLAIMED-only
        # source state, the same immutable binding, the same condition-watch
        # refusal. A double that bound more freely would certify a hole.
        import tempfile
        from pathlib import Path

        from jarvis.memory import SQLiteMemoryStore
        from tests.helpers import safe_cleanup

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        sqlite_store = SQLiteMemoryStore(Path(temp.name) / "binding.db")
        sqlite_store.save_project(ProjectRecord(id="proj-a", name="Project A"))

        lease = timedelta(minutes=5)
        at = self.NOW + timedelta(hours=1)
        during = at + timedelta(minutes=1)
        expired = at + lease + timedelta(minutes=1)

        def refusal(call) -> str:
            try:
                call()
            except JobError:
                return "refused"
            return "ALLOWED"

        def script(store) -> list:
            trace: list = []
            record = store.create_job(self.spec(), now=self.NOW)
            run = store.materialize_due_job_run(record.id, record.revision, now=at)
            held = store.claim_job_run(run.id, "worker-a", now=at, lease=lease)
            for label, owner, epoch, when in (
                ("wrong_owner", "worker-b", held.fencing_epoch, during),
                ("stale_epoch", "worker-a", held.fencing_epoch - 1, during),
                ("expired", "worker-a", held.fencing_epoch, expired),
            ):
                trace.append(
                    (
                        f"bind_{label}",
                        refusal(
                            lambda o=owner, e=epoch, w=when: store.bind_job_run_dispatch(
                                run.id, o, e, now=w
                            )
                        ),
                    )
                )
            bound = store.bind_job_run_dispatch(
                run.id, "worker-a", held.fencing_epoch, now=during
            )
            after = store.load_job_run(run.id)
            trace.append(
                (
                    "bound",
                    after.status.value,
                    after.session_id == bound.session_id,
                    after.chain_id == bound.chain_id,
                    after.settled,
                )
            )
            session = store.load_session(bound.session_id)
            trace.append(
                (
                    "session",
                    session.project_id,
                    session.chain.id == bound.chain_id,
                    session.chain.status.value,
                    tuple(session.chain.steps) == tuple(run.spec.steps),
                )
            )
            # The binding is immutable and RUNNING is not claimable.
            trace.append(
                (
                    "rebind",
                    refusal(
                        lambda: store.bind_job_run_dispatch(
                            run.id, "worker-a", held.fencing_epoch, now=during
                        )
                    ),
                )
            )
            trace.append(
                (
                    "reclaim_running",
                    refusal(
                        lambda: store.claim_job_run(
                            run.id, "worker-b", now=expired, lease=lease
                        )
                    ),
                )
            )
            return trace

        self.assertEqual(script(self.memory), script(sqlite_store))

    def test_synchronized_concurrent_binders_produce_one_winner_in_the_double(
        self,
    ) -> None:
        # F4. DETERMINISTIC, not timing luck: every binder is held at a barrier
        # until all of them have arrived, so they really do contend. The double
        # must then behave like SQLite - one winner, one session, no orphan,
        # and the winner's binding is the one that stuck.
        import threading

        record = self.memory.create_job(self.spec(), now=self.NOW)
        at = self.NOW + timedelta(hours=1)
        run = self.memory.materialize_due_job_run(record.id, record.revision, now=at)
        held = self.memory.claim_job_run(
            run.id, "worker-a", now=at, lease=timedelta(minutes=5)
        )
        during = at + timedelta(minutes=1)

        binders = 4
        barrier = threading.Barrier(binders)
        results: list = []
        guard = threading.Lock()

        def bind() -> None:
            barrier.wait()
            try:
                outcome = self.memory.bind_job_run_dispatch(
                    run.id, "worker-a", held.fencing_epoch, now=during
                )
            except JobError as exc:
                outcome = exc
            with guard:
                results.append(outcome)

        threads = [threading.Thread(target=bind) for _ in range(binders)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [r for r in results if isinstance(r, JobRunDispatch)]
        self.assertEqual(1, len(winners), results)
        self.assertEqual(binders - 1, len([r for r in results if isinstance(r, JobError)]))
        # EXACTLY ONE session exists, and it is the winner's - no orphan was
        # created by a loser that got as far as minting one.
        self.assertEqual(1, len(self.memory.sessions))
        self.assertIn(winners[0].session_id, self.memory.sessions)
        stored = self.memory.load_job_run(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertEqual(winners[0].session_id, stored.session_id)
        self.assertEqual(winners[0].chain_id, stored.chain_id)
        session = self.memory.load_session(winners[0].session_id)
        self.assertEqual(winners[0].chain_id, session.chain.id)

    def test_an_unknown_outcome_is_terminal_in_both_stores(self) -> None:
        # RUNNING is a state no phase-2B0 transition can reach, so both stores
        # are placed in it directly - exactly as a future dispatch, or a crash
        # mid-transition, would leave the durable row.
        import tempfile
        from pathlib import Path

        from jarvis.memory import SQLiteMemoryStore
        from tests.helpers import safe_cleanup

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        sqlite_store = SQLiteMemoryStore(Path(temp.name) / "unknown.db")
        sqlite_store.save_project(ProjectRecord(id="proj-a", name="Project A"))

        lease = timedelta(minutes=5)
        at = self.NOW + timedelta(hours=1)
        during = at + timedelta(minutes=1)

        def script(store) -> list:
            trace: list = []
            record = store.create_job(self.spec(), now=self.NOW)
            run = store.materialize_due_job_run(record.id, record.revision, now=at)
            held = store.claim_job_run(run.id, "worker-a", now=at, lease=lease)
            force_running(store, run.id)
            marked = store.mark_job_run_unknown(
                run.id, "worker-a", held.fencing_epoch, now=during
            )
            trace.append(
                (
                    "marked",
                    marked.status.value,
                    marked.owner_token,
                    marked.lease_expires_at_utc,
                    marked.fencing_epoch,
                    marked.settled,
                    marked.settled_at,
                )
            )
            long_after = at + timedelta(days=365)
            for label, call in (
                (
                    "reclaim",
                    lambda: store.claim_job_run(
                        run.id, "worker-b", now=long_after, lease=lease
                    ),
                ),
                (
                    "settle",
                    lambda: store.settle_job_run(
                        run.id,
                        JobRunStatus.COMPLETED,
                        "worker-a",
                        held.fencing_epoch,
                        now=during,
                    ),
                ),
                (
                    "remark",
                    lambda: store.mark_job_run_unknown(
                        run.id, "worker-a", held.fencing_epoch, now=during
                    ),
                ),
                (
                    "release",
                    lambda: store.release_job_run(
                        run.id, "worker-a", held.fencing_epoch, now=during
                    ),
                ),
            ):
                try:
                    call()
                    trace.append((label, "ALLOWED"))
                except JobError:
                    trace.append((label, "refused"))
            trace.append(
                (
                    "blocks_forever",
                    store.materialize_due_job_run(
                        record.id, store.load_job(record.id).revision, now=long_after
                    ),
                )
            )
            trace.append(("status", store.load_job_run(run.id).status.value))
            return trace

        self.assertEqual(script(self.memory), script(sqlite_store))

    def test_an_uncertain_outcome_is_never_reclaimed_by_either_store(self) -> None:
        # The safety property, asked of the double directly: a lease expiring
        # is a coordination fact and never evidence that the previous owner's
        # external action did not happen.
        record = self.memory.create_job(self.spec(), now=self.NOW)
        due = self.NOW + timedelta(hours=1)
        run = self.memory.materialize_due_job_run(
            record.id, record.revision, now=due
        )
        self.memory.claim_job_run(
            run.id, "worker-a", now=due, lease=timedelta(minutes=5)
        )
        for status in (JobRunStatus.RUNNING, JobRunStatus.UNKNOWN_OUTCOME):
            self.memory.job_runs[run.id] = dataclasses.replace(
                self.memory.job_runs[run.id], status=status
            )
            long_after = due + timedelta(days=365)
            with self.assertRaises(JobError):
                self.memory.claim_job_run(
                    run.id, "worker-b", now=long_after, lease=timedelta(minutes=5)
                )
            # And it blocks every later generation, by the one-unsettled rule.
            self.assertIsNone(
                self.memory.materialize_due_job_run(
                    record.id,
                    self.memory.load_job(record.id).revision,
                    now=long_after,
                )
            )

    def test_a_one_time_job_materializes_at_most_once_in_the_double(self) -> None:
        one_time = JobSpec(
            project_id="proj-a",
            kind=JobKind.ONE_TIME,
            label="single shot",
            schedule=OneTime(at=self.NOW + timedelta(hours=1)),
            steps=(
                ChainStep(
                    objective="summarize the log",
                    capability="echo",
                    action="read",
                    target="notes.txt",
                ),
            ),
        )
        record = self.memory.create_job(one_time, now=self.NOW)
        later = self.NOW + timedelta(days=30)
        run = self.memory.materialize_due_job_run(
            record.id, record.revision, now=later
        )
        self.assertEqual((self.NOW + timedelta(hours=1)).isoformat(),
                         run.nominal_due_at_utc)
        settle(self.memory, run.id, JobRunStatus.COMPLETED, now=later)
        self.assertIsNone(self.memory.load_job(record.id).next_due_at_utc)
        self.assertIsNone(
            self.memory.materialize_due_job_run(
                record.id,
                self.memory.load_job(record.id).revision,
                now=later + timedelta(days=365),
            )
        )

    def test_projections_are_bounded_and_carry_no_declaration(self) -> None:
        record = self.memory.create_job(
            JobSpec(
                project_id="proj-a",
                kind=JobKind.RECURRING,
                label="nightly export",
                schedule=Interval(anchor=self.NOW, every=timedelta(hours=1)),
                steps=(
                    ChainStep(
                        objective="email the api_key=HUNTER2 report",
                        capability="echo",
                        action="read",
                        target="C:/Users/me/secret.txt",
                    ),
                ),
            ),
            now=self.NOW,
        )
        self.memory.record_job_run(record.id, record.next_due_at_utc)
        payload = repr(self.memory.list_jobs(10)) + repr(
            self.memory.list_job_runs(record.id, 10)
        )
        self.assertNotIn("HUNTER2", payload)
        self.assertNotIn("secret.txt", payload)
        self.assertEqual("nightly export", self.memory.job_summary(record.id).label)
        for bad in (0, -1, True, "5", None):
            self.assertEqual([], self.memory.list_jobs(bad))
            self.assertEqual([], self.memory.list_job_runs(record.id, bad))


if __name__ == "__main__":
    unittest.main()
