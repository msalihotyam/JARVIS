"""V21HARD2: capture lifecycle survives locks, cancels and restarts (fake providers only)."""

from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

import jarvis.memory as memory_module
from jarvis import codex, intent
from jarvis.app import MemoryTriggers
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.contracts import (
    ActionScope,
    DataClassification,
    ErrorCode,
    SessionState,
    Task,
)
from jarvis.core import (
    MemoryCaptureOutcome,
    _capture_run_connection,
    _claim_capture_run,
    _finish_capture_run,
    _release_stale_capture_runs,
)
from jarvis.memory import SQLiteMemoryStore
from tests.helpers import isolated_settings
from tests.test_claude_connector import OAUTH_MAX_AUTH_STATUS, patch_oauth_environment

NOW = "2026-10-18T12:00:00+00:00"


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteMemoryStore(isolated_settings(self).memory_database)
        self.project = self.store.create_project("Alpha").id
        self.cid = self.store.create_conversation(self.project).id

    def rows(self) -> dict[str, tuple]:
        with _capture_run_connection(self.store) as connection:
            return {r[0]: tuple(r[1:]) for r in connection.execute(
                "SELECT run_id, outcome, finished_at FROM memory_capture_runs")}


class ConsolidationLockScopeTests(StoreTest):
    def test_only_the_apply_holds_the_writer(self) -> None:
        held = []

        @contextlib.contextmanager
        def writing():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        planned = []
        real_plan = memory_module.plan_consolidation

        def plan(*args, **kwargs):
            planned.append(bool(held))
            return real_plan(*args, **kwargs)

        applied = []
        with mock.patch.object(memory_module, "plan_consolidation", side_effect=plan), \
                mock.patch.object(self.store, "apply_consolidation_plan",
                                  side_effect=lambda p: applied.append(bool(held)) or {}):
            self.store.consolidate_scope(self.project, NOW, writing=writing())
        self.assertEqual([False], planned)
        self.assertEqual([True], applied)


class DurableMarkTests(StoreTest):
    def test_a_restart_rederives_the_mark_from_the_ok_run(self) -> None:
        self.assertTrue(_claim_capture_run(self.store, "r1", self.cid, "session_end"))
        _finish_capture_run(self.store, MemoryCaptureOutcome(run_id="r1", outcome="ok"))
        first = MemoryTriggers()
        first._apps[self.cid] = SimpleNamespace(memory=self.store)
        first._mark(self.cid, [SimpleNamespace(seq=4), SimpleNamespace(seq=7)],
                    MemoryCaptureOutcome(run_id="r1", outcome="ok"))
        self.assertEqual(7, MemoryTriggers()._mark_for(self.store, self.cid))
        # A lower later mark never moves it back.
        first._mark(self.cid, [SimpleNamespace(seq=2)], MemoryCaptureOutcome(run_id="r1", outcome="ok"))
        self.assertEqual(7, MemoryTriggers()._mark_for(self.store, self.cid))

    def test_a_refused_run_leaves_no_durable_mark(self) -> None:
        self.assertTrue(_claim_capture_run(self.store, "r2", self.cid, "session_end"))
        refused = MemoryCaptureOutcome(run_id="r2", outcome="provider_error")
        _finish_capture_run(self.store, refused)
        first = MemoryTriggers()
        first._apps[self.cid] = SimpleNamespace(memory=self.store)
        first._mark(self.cid, [SimpleNamespace(seq=3)], refused)
        self.assertEqual(0, MemoryTriggers()._mark_for(self.store, self.cid))


class StaleRunSweepTests(StoreTest):
    def test_only_old_open_rows_are_released(self) -> None:
        for run_id in ("old", "fresh", "done", "unknown"):
            self.assertTrue(_claim_capture_run(self.store, run_id, self.cid, "session_end"))
        _finish_capture_run(self.store, MemoryCaptureOutcome(run_id="done", outcome="ok"))
        _finish_capture_run(self.store, MemoryCaptureOutcome(run_id="unknown", outcome="unknown_outcome"))
        with _capture_run_connection(self.store) as connection:
            connection.execute("UPDATE memory_capture_runs SET started_at = ? WHERE run_id != 'fresh'",
                               ("2026-10-18T10:00:00+00:00",))
            connection.execute("UPDATE memory_capture_runs SET started_at = ? WHERE run_id = 'fresh'",
                               ("2026-10-18T11:30:00+00:00",))
        before = self.rows()
        self.assertEqual(1, _release_stale_capture_runs(self.store, NOW))
        after = self.rows()
        self.assertEqual(("aborted", NOW), after["old"])
        for kept in ("fresh", "done", "unknown"):
            self.assertEqual(before[kept], after[kept])
        # Released, never replayed: the run id stays claimed.
        self.assertFalse(_claim_capture_run(self.store, "old", self.cid, "session_end"))
        self.assertEqual(0, _release_stale_capture_runs(self.store, NOW))


class CancelBeforeSpawnTests(unittest.TestCase):
    def test_claude_code_never_spawns_after_an_early_cancel(self) -> None:
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)
        connector = ClaudeCodeConnector()
        task = Task(objective="Analyze", capability="code_planning",
                    scope=ActionScope(action="analyze", target="", destination="anthropic/claude-code",
                                      data_classification=DataClassification.INTERNAL))
        self.assertFalse(connector.cancel(task.id))
        with mock.patch.object(ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS), \
                mock.patch("jarvis.connectors.claude_code.subprocess.Popen") as popen:
            result = connector.execute(task, SessionState())
        popen.assert_not_called()
        self.assertIs(ErrorCode.CANCELLED, result.error.code)
        self.assertNotIn(task.id, connector._cancelled)

    def test_codex_never_spawns_after_an_early_cancel(self) -> None:
        state = {"status": "available", "models": [{"id": "m", "name": "M", "efforts": ["low"]}]}
        adapter = codex.CodexIntentAdapter("system", "m", "low", state=lambda: state)
        task = Task(objective="hi", capability=intent.INTENT_CAPABILITY, scope=ActionScope(action="understand", target=""))
        self.assertFalse(adapter.cancel(task.id))
        with mock.patch.object(codex, "codex_executable", return_value="codex.exe"), \
                mock.patch.object(codex.subprocess, "Popen") as popen:
            result = adapter.complete(task, SessionState(objective="hi"))
        popen.assert_not_called()
        self.assertIs(ErrorCode.CANCELLED, result.error.code)
        self.assertNotIn(task.id, adapter._cancelled)


if __name__ == "__main__":
    unittest.main()
