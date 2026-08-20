from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from jarvis.contracts import (
    LINEAR_LOOP_STAGES,
    RETRIEVAL_CANDIDATE_WINDOW,
    AttemptState,
    DataClassification,
    DecisionRecord,
    ExecutionAttempt,
    LoopStage,
    MemoryRetrievalQuery,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    RetrievedContext,
    RunStatus,
    SessionState,
    Task,
    TaskStatus,
    UsageRecord,
    build_retrieved_context,
    is_expired,
)
from jarvis.core import AgentCore
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
        return build_retrieved_context(query, current, candidates)

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
    ) -> bool:
        # Window A: an authoritative cancellation/terminal decision forbids start.
        task = self.tasks.get(task_id)
        if task is None or task.status in _BLOCKING_STATUSES:
            return False
        history = self.attempts.get(task_id, [])
        for index, attempt in enumerate(history):
            if attempt.id != attempt_id:
                continue
            if attempt.state is not AttemptState.PREPARED:
                return False
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
                    return False
                selected.append(grant)
            for grant in selected:
                grant["consumed_by_attempt"] = attempt_id
            history[index] = replace(attempt, state=AttemptState.IN_FLIGHT)
            return True
        return False

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
        self.usage[usage.id] = copy.deepcopy(usage)

    def monthly_monetary_cost(self, month: str | None = None) -> float:
        return sum(item.budget_amount_usd for item in self.usage.values())

    def finalize_task(
        self,
        session: SessionState,
        task: Task,
        decision: DecisionRecord,
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
        self.decisions.append(copy.deepcopy(decision))
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


if __name__ == "__main__":
    unittest.main()
