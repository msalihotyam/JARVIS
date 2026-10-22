"""V21HARD2: capture lifecycle survives locks, cancels and restarts (fake providers only)."""

from __future__ import annotations

import contextlib
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import jarvis.memory as memory_module
from jarvis import codex, intent, memory_capture
from jarvis.app import MemoryTriggers
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.contracts import (
    ActionScope,
    DataClassification,
    ErrorCode,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    SessionState,
    Task,
)
from jarvis.conversation import append_turn
from jarvis.core import (
    MemoryCaptureOutcome,
    _capture_run_connection,
    _claim_capture_run,
    _finish_capture_run,
    _release_stale_capture_runs,
)
from jarvis.memory import MemoryRecordRefused, SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from tests.helpers import isolated_settings, open_core
from tests.test_claude_connector import OAUTH_MAX_AUTH_STATUS, patch_oauth_environment
from tests.test_memory_capture_core import FakeModel

NOW = "2026-10-18T12:00:00+00:00"
SESSION_END = MemoryRecordOrigin.CAPTURE_SESSION_END


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
    def ledger(self, run_id: str, outcome: str | None, counts: str) -> None:
        """One run row as any writer may have left it (finished or not)."""
        with _capture_run_connection(self.store) as connection:
            connection.execute(
                "INSERT INTO memory_capture_runs (run_id, conversation_id, trigger, started_at, finished_at, "
                "outcome, counts) VALUES (?, ?, 'session_end', ?, ?, ?, ?)",
                (run_id, self.cid, NOW, None if outcome is None else NOW, outcome, counts))

    def mark(self) -> int | None:
        return MemoryTriggers()._mark_for(self.store, self.cid)  # a fresh process: no in-process mark

    def test_a_restart_rederives_the_mark_from_the_one_finish_write(self) -> None:
        self.assertTrue(_claim_capture_run(self.store, "r1", self.cid, "session_end", 7))
        _finish_capture_run(self.store, MemoryCaptureOutcome(run_id="r1", outcome="ok", approved=1), 7)
        self.assertEqual(7, self.mark())
        # A lower later run never moves it back.
        self.assertTrue(_claim_capture_run(self.store, "r0", self.cid, "session_end", 2))
        _finish_capture_run(self.store, MemoryCaptureOutcome(run_id="r0", outcome="ok"), 2)
        self.assertEqual(7, self.mark())

    def test_a_refused_run_with_no_record_leaves_its_range_offered(self) -> None:
        # (c) a refusal before any write: the turns go to the next leave.
        for run_id, outcome in (("r2", "provider_error"), ("r3", "skipped:short"),
                                ("r4", "malformed_answer"), ("r5", "parse_error")):
            self.assertTrue(_claim_capture_run(self.store, run_id, self.cid, "session_end", 3))
            _finish_capture_run(self.store, MemoryCaptureOutcome(run_id=run_id, outcome=outcome), 3)
        self.assertEqual(0, self.mark())

    def test_a_failed_finish_after_records_still_covers_the_range(self) -> None:
        # (a) the claim carries the mark; the finish write never happened.
        self.ledger("r1", None, '{"through_seq": 6}')
        self.assertEqual(6, self.mark())

    def test_an_aborted_run_after_a_committed_record_covers_its_range(self) -> None:
        # (b) aborted (cancel, shutdown or stale sweep) after one record.
        self.ledger("r1", "aborted", '{"approved": 1, "pending": 0, "through_seq": 5}')
        self.assertEqual(5, self.mark())

    def test_a_swept_open_run_covers_its_range(self) -> None:
        self.assertTrue(_claim_capture_run(self.store, "r1", self.cid, "session_end", 4))
        with _capture_run_connection(self.store) as connection:
            connection.execute("UPDATE memory_capture_runs SET started_at = ?", ("2026-10-18T10:00:00+00:00",))
        self.assertEqual(1, _release_stale_capture_runs(self.store, NOW))
        self.assertEqual(4, self.mark())

    def test_every_outcome_that_may_have_written_covers_its_range(self) -> None:
        cases = (("internal_error", '{"approved": 0, "pending": 0, "through_seq": 3}', 3),
                 ("some_future_outcome", '{"through_seq": 4}', 4),
                 ("provider_error", '{"approved": 0, "pending": 1, "through_seq": 5}', 5),
                 ("ok", '{"approved": 0, "pending": 0, "through_seq": 6}', 6))
        for index, (outcome, counts, expected) in enumerate(cases):
            with self.subTest(outcome=outcome):
                self.ledger(f"r{index}", outcome, counts)
                self.assertEqual(expected, self.mark())

    def cite(self, record_id: str, turn_ids: str, origin: str = "capture_session_end") -> None:
        """One record as a capture (or any writer) left it, citing its turns."""
        with self.store._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO memory_records (record_id, kind, text, subject_key, project_id, scope_kind, "
                "origin, actor, confidence, classification, created_at, source_conversation_id, "
                "source_turn_ids) VALUES (?, 'fact', 'x', 'x', ?, 'project', ?, 'model', 0.9, 'internal', "
                "?, ?, ?)", (record_id, self.project, origin, NOW, self.cid, turn_ids))

    def test_idle_claims_and_unmarked_rows_add_nothing_of_their_own(self) -> None:
        self.ledger("idle-1", None, "{}")
        self.ledger("r1", "ok", '{"approved": 2}')
        self.cite("m0", '["t9-0123456789"]', origin="explicit_user")
        self.assertEqual(0, self.mark())

    def test_a_row_older_than_the_mark_covers_the_turns_its_records_cite(self) -> None:
        # A run from before through_seq (aborted, or ok whose mark write failed).
        self.ledger("r1", "aborted", '{"approved": 1, "pending": 0}')
        self.cite("m1", '["t3-0123456789", "t5-abcdef0123"]')
        self.cite("m2", '["t4-0123456789"]', origin="capture_precompact")
        self.assertEqual(5, self.mark())

    def test_an_unreadable_cited_turn_captures_nothing(self) -> None:
        for index, turn_ids in enumerate(('not json', '{"t5-x": 1}', '"t5-x"', '[5]', '["5-x"]',
                                          '["t-x"]', '["tx-1"]', '["t٥-x"]')):
            with self.subTest(turn_ids=turn_ids):
                self.cite(f"bad{index}", turn_ids)
                self.assertIsNone(self.mark())
                with self.store._connection(write=True) as connection:
                    connection.execute("DELETE FROM memory_records WHERE record_id = ?", (f"bad{index}",))

    def test_an_unreadable_mark_captures_nothing(self) -> None:
        for index, counts in enumerate(('not json', '[]', '{"through_seq": "7"}', '{"through_seq": -1}',
                                        '{"through_seq": true}', '{"through_seq": 7, "approved": "1"}')):
            with self.subTest(counts=counts):
                self.ledger(f"bad{index}", "ok", counts)
                self.assertIsNone(self.mark())
                with _capture_run_connection(self.store) as connection:
                    connection.execute("DELETE FROM memory_capture_runs WHERE run_id = ?", (f"bad{index}",))
        with mock.patch.object(self.store, "_connection", side_effect=OSError("locked")):
            self.assertIsNone(self.mark())

    def test_the_in_process_mark_follows_the_same_rule(self) -> None:
        triggers = MemoryTriggers()
        turns = [SimpleNamespace(seq=2), SimpleNamespace(seq=3)]
        triggers._mark(self.cid, turns, MemoryCaptureOutcome(run_id="r1", outcome="provider_error"))
        self.assertEqual(0, triggers._mark_for(self.store, self.cid))
        triggers._mark(self.cid, turns, MemoryCaptureOutcome(run_id="r1", outcome="aborted"))
        self.assertEqual(3, triggers._mark_for(self.store, self.cid))
        triggers._mark(self.cid, [SimpleNamespace(seq=1)], MemoryCaptureOutcome(run_id="r2", outcome="ok"))
        self.assertEqual(3, triggers._mark_for(self.store, self.cid))


class FinishFailsEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        skip = mock.patch.object(memory_capture, "extraction_skip_reason", return_value=None)
        skip.start()
        self.addCleanup(skip.stop)
        settings = isolated_settings(self)
        self.store = SQLiteMemoryStore(settings.memory_database)
        self.core = open_core(ToolRegistry(), self.store, settings, destination_policy=frozenset())
        self.cid = self.store.create_conversation(self.store.create_project("Alpha").id).id
        ids = []

        def change(state):
            for index, text in enumerate(("We deploy on Fridays only.", "The build uses uv.")):
                state, turn = append_turn(state, role="user", kind="user", text=text, message=text, key=f"k{index}")
                ids.append(turn.id)
            return state

        self.store.update_conversation_state(self.cid, change)
        self.last = ids[-1]
        self.model = FakeModel(json.dumps({"candidates": [{
            "kind": "fact", "text": "The build uses uv.", "subject_key": "tooling",
            "confidence": 0.9, "evidence_turn_ids": [self.last]}]}))

    def test_a_restart_after_a_failed_finish_write_never_captures_again(self) -> None:
        with mock.patch("jarvis.core._finish_capture_run", side_effect=OSError("database is locked")):
            first = self.core.capture_memory(self.cid, SESSION_END, self.model)
        self.assertEqual(("ok", 1), (first.outcome, first.approved))
        top = int(self.last.split("-")[0][1:])
        self.assertEqual(top, MemoryTriggers()._mark_for(self.store, self.cid))


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


class DeleteWinsTests(StoreTest):
    def capture(self, scope_conversation=None) -> MemoryRecord:
        return MemoryRecord(
            kind=MemoryRecordKind.FACT, text="We deploy on Fridays.", subject_key="deploys",
            scope=MemoryRecordScope(self.project, scope_conversation),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END, actor=MemoryRecordActor.MODEL,
            confidence=0.9, classification=DataClassification.INTERNAL,
            created_at=NOW, source_conversation_id=self.cid)

    def test_a_capture_finishing_after_the_delete_is_dropped(self) -> None:
        kept = self.store.insert_record(self.capture()).record.record_id  # positive control: before the delete
        self.store.archive_conversation(self.cid)
        preview = self.store.conversation_delete_preview(self.cid)
        self.store.delete_conversation(self.cid, confirm=self.cid, expected_next_seq=preview["expected_next_seq"],
                                       expected_title=preview["title"])
        for scope_conversation in (None, self.cid):
            with self.subTest(scope_conversation=scope_conversation):
                with self.assertRaises(MemoryRecordRefused):
                    self.store.insert_record(self.capture(scope_conversation))
        self.assertEqual([kept], [v.record.record_id for v in self.store.list_records(
            MemoryRecordScope(self.project, None))])


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
