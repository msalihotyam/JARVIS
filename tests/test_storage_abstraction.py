from __future__ import annotations

import copy
import unittest

from jarvis.contracts import (
    DecisionRecord,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    RunStatus,
    SessionState,
    Task,
    UsageRecord,
)
from jarvis.core import AgentCore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task


class InMemoryStore:
    """Small non-SQLite MemoryStore implementation used as a contract test."""

    def __init__(self) -> None:
        self.sessions: dict[str, SessionState] = {}
        self.tasks: dict[str, Task] = {}
        self.task_sessions: dict[str, str] = {}
        self.pending: dict[tuple[str, str], PermissionRequest] = {}
        self.remembered: set[tuple[str, str]] = set()
        self.decisions: list[DecisionRecord] = []
        self.usage: dict[str, UsageRecord] = {}

    def save_session(self, session: SessionState) -> None:
        self.sessions[session.id] = copy.deepcopy(session)

    def load_session(self, session_id: str) -> SessionState | None:
        return copy.deepcopy(self.sessions.get(session_id))

    def save_task(self, session_id: str, task: Task) -> None:
        self.tasks[task.id] = copy.deepcopy(task)
        self.task_sessions[task.id] = session_id

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
    ) -> None:
        self.save_session(session)
        self.save_task(session.id, task)
        if pending_request is not None:
            self.save_pending_approval(pending_request)

    def list_decisions(self, session_id: str) -> list[DecisionRecord]:
        return copy.deepcopy(
            [item for item in self.decisions if item.session_id == session_id]
        )

    def save_pending_approval(self, request: PermissionRequest) -> None:
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
    ) -> None:
        self.pending.pop((request.task_id, request.session_id), None)
        if status == "approved" and grant.remember_for_session:
            self.remembered.add((request.session_id, request.scope_fingerprint))

    def has_remembered_scope_grant(
        self, session_id: str, scope_fingerprint: str
    ) -> bool:
        return (session_id, scope_fingerprint) in self.remembered

    def append_usage(self, usage: UsageRecord) -> None:
        self.usage[usage.id] = copy.deepcopy(usage)

    def monthly_actual_cost(self, month: str | None = None) -> float:
        return sum(item.actual_cost_usd for item in self.usage.values())

    def finalize_task(
        self,
        session: SessionState,
        task: Task,
        decision: DecisionRecord,
        usage: UsageRecord,
        event_type: str,
        payload: dict,
    ) -> None:
        self.save_session(session)
        self.save_task(session.id, task)
        self.decisions.append(copy.deepcopy(decision))
        self.append_usage(usage)


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


if __name__ == "__main__":
    unittest.main()
