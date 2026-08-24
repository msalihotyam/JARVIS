from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from uuid import uuid4

from jarvis.contracts import (
    LINEAR_LOOP_STAGES,
    MONTHLY_HARD_LIMIT_NANO_USD,
    OPEN_RESERVATION_STATES,
    PROJECT_DECISION_CANDIDATE_WINDOW,
    PROJECT_MEMORY_CANDIDATE_WINDOW,
    PROJECT_MEMORY_TEXT_CAP,
    RETRIEVAL_CANDIDATE_WINDOW,
    AttemptState,
    BillingClaim,
    BillingClass,
    BillingIntegrityError,
    BillingReconciliation,
    DataClassification,
    DecisionRecord,
    ExecutionAttempt,
    ExecutionClaimOutcome,
    LedgerSnapshot,
    LoopStage,
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
    RetrievedContext,
    RetrievedMemoryKind,
    RunStatus,
    SessionState,
    Task,
    TaskStatus,
    UsageRecord,
    build_retrieved_context,
    canonical_usd,
    is_expired,
    ledger_month,
    nano_usd,
    normalize_retrieved_text,
    redact_credentials,
    redact_local_paths,
    usd_from_nano,
    utc_now,
)
from jarvis.core import AgentCore
from jarvis.memory import ledger_timestamp
from jarvis.registry import ToolRegistry
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


class InMemoryStore:
    """Small non-SQLite MemoryStore implementation used as a contract test."""

    def __init__(self) -> None:
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
            active_items = [
                item
                for item in self.memory_items
                if item.project_id == query.project_id and item.active
            ]
            active_items.sort(key=lambda item: item.id)
            active_items.sort(key=lambda item: item.created_at, reverse=True)
            project_candidates = tuple(
                copy.deepcopy(active_items[:PROJECT_MEMORY_CANDIDATE_WINDOW])
            )
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
        # The same write-boundary contract as the SQLite store: normalize,
        # hard-reject oversize (never truncate), canonical hygiene helpers,
        # INSERT-only with supersede-only lifecycle flags.
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
            if (
                old_index is None
                or self.memory_items[old_index].project_id != stored.project_id
                or not self.memory_items[old_index].active
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

    def cancel_task(
        self,
        session: SessionState,
        task: Task,
        *,
        connector_cancelled: bool,
    ) -> str:
        persisted = self.tasks.get(task.id)
        if persisted is None:
            return "not_found"
        if persisted.status in _TERMINAL_STATUSES:
            return "terminal"
        history = self.attempts.get(task.id, [])
        latest = history[-1] if history else None
        in_flight = latest is not None and latest.state is AttemptState.IN_FLIGHT
        if not in_flight or connector_cancelled:
            task.status = TaskStatus.CANCELLED
            task.stage = LoopStage.CANCELLED
            session.active_task_id = None
            task.progress = replace(task.progress, open_issues=())
            outcome = "cancelled"
        else:
            task.status = TaskStatus.CANCEL_REQUESTED
            outcome = "cancelling"
        self.save_session(session)
        self._save_task_guarded(session.id, task)
        return outcome

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


if __name__ == "__main__":
    unittest.main()
