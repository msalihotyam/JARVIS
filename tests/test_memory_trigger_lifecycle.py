"""MEMTRIGFIX: capture-trigger lifecycle.

PreCompact resolves the app when the capture runs; quit never lets a capture
write after it returns (slow fake provider, its process terminated); the bound
covers the whole quit path, a refused or hanging config included; PreCompact
and the window use Core's own gate; the skip policy reads the session, not
the batch (SECCAPTURE F1). Fake providers and TEMP stores only.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess  # nosec B404 - a local sleeping child stands in for a model CLI
import sys
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

import jarvis.app as app_module
from jarvis.app import JarvisApp, MemoryTriggers
from jarvis.contracts import (
    MODEL_ANSWER_RESULT_KEY,
    MemoryRecordActor,
    MemoryRecordOrigin,
    OutcomeStatus,
    ToolResult,
    build_model_answer,
)
from jarvis.conversation import PersistedConversation, append_turn, register_precompact_hook
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import ConfigStore
from jarvis.ui import backend as backend_module
from jarvis.ui.backend import Backend
from tests.helpers import isolated_settings, open_core

PRECOMPACT = MemoryRecordOrigin.CAPTURE_PRECOMPACT
SESSION_END = MemoryRecordOrigin.CAPTURE_SESSION_END
#: Long enough that the session passes the skip policy (2 user turns, 200 chars).
WORDS = ("We deploy the service on Fridays only, after the nightly suite is green and the "
         "release notes are reviewed by the whole team.")


class SlowModel:
    """Quotes the first transcript turn as a fact; may block and may start a
    sleeping child process that ``cancel`` terminates, as a CLI adapter does."""

    name = "fake-slow"

    def __init__(self, *, block: bool = False, spawn: bool = False) -> None:
        self.block, self.spawn = block, spawn
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls: list[str] = []
        self.cancelled: list[str] = []
        self.process: subprocess.Popen | None = None

    def complete(self, task, session):
        self.calls.append(task.id)
        if self.spawn:
            self.process = subprocess.Popen(  # nosec B603 - fixed argv, no shell
                [sys.executable, "-c", "import time; time.sleep(30)"])
        self.started.set()
        if self.block:
            self.release.wait(10)
        transcript = task.objective.split("<transcript>\n", 1)[1].split("\n</transcript>", 1)[0]
        first = json.loads(transcript)[0]
        reply = json.dumps({"candidates": [{
            "kind": "fact", "text": first["text"], "subject_key": "deploys",
            "confidence": 0.9, "evidence_turn_ids": [first["id"]]}]})
        return ToolResult(outcome=OutcomeStatus.SUCCESS, summary="ok",
                          data={MODEL_ANSWER_RESULT_KEY: build_model_answer(reply)})

    def cancel(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        return True


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.database = self.settings.memory_database
        self.store = SQLiteMemoryStore(self.database)
        self.project = self.store.create_project("Alpha").id
        self.cid = self.store.create_conversation(self.project).id
        self.addCleanup(register_precompact_hook, self.store, None)

    def say(self, conversation_id, *texts, role="user", raw_turns_max=200):
        doomed: list = []

        def change(state):
            for text in texts:
                kind = "user" if role == "user" else "reply"
                state, _ = append_turn(state, role=role, kind=kind, text=text,
                                       message=text if role == "user" else "",
                                       key=f"k{state.next_seq}", raw_turns_max=raw_turns_max,
                                       on_compact=doomed.extend)
            return state

        self.store.update_conversation_state(conversation_id, change)
        return tuple(doomed)

    def core(self, store=None):
        return open_core(ToolRegistry(), store or self.store, self.settings, destination_policy=frozenset())

    def app(self, model, triggers=None) -> JarvisApp:
        app = JarvisApp(self.core(), self.store, self.settings, intent_model=model)
        app.memory_triggers = triggers or MemoryTriggers()
        return app

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql, params).fetchall()

    def records(self) -> int:
        return self.rows("SELECT COUNT(*) FROM memory_records")[0][0]

    def outcomes(self) -> list:
        return [row[0] for row in self.rows("SELECT outcome FROM memory_capture_runs")]

    def settle(self, triggers: MemoryTriggers) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with triggers._lock:
                if not triggers._threads and not triggers._jobs:
                    return
            time.sleep(0.01)
        self.fail("capture worker did not settle")

    def finish(self, model: SlowModel, threads) -> None:
        """Let the slow provider return (after shutdown) and wait out its thread."""
        model.release.set()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())


class BackendBase(_Base):
    """A Backend over a real config file; its factory builds the app with
    ``self.current`` as the model at build time."""

    def setUp(self) -> None:
        super().setUp()
        self.config_path = Path(self.database).parent / "config.json"
        self.config = ConfigStore(self.config_path)
        self.config.mutate(lambda s: replace(s, memory_database=self.database))
        self.triggers = MemoryTriggers()
        patcher = mock.patch.object(app_module, "MEMORY_TRIGGERS", self.triggers)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.triggers.halt)
        self.current = SlowModel()
        self.built: list[SlowModel] = []

        def factory(settings):
            self.built.append(self.current)
            return JarvisApp(self.core(SQLiteMemoryStore(settings.memory_database)),
                             SQLiteMemoryStore(settings.memory_database), settings,
                             intent_model=self.current)
        self.backend = Backend(store=self.config, app_factory=factory)
        window = self.backend.conversations()
        self.addCleanup(register_precompact_hook, window, None)


class PreCompactResolvesTheCurrentApp(BackendBase):
    def test_a_model_change_after_watching_applies_to_the_next_capture(self) -> None:
        watched_with = self.current
        self.assertEqual([watched_with], self.built)           # the watch itself built an app
        self.say(self.cid, WORDS, WORDS + " Again.")
        newer = self.current = SlowModel(block=True)           # the model changes after the watch
        conversation = PersistedConversation(self.backend.conversations(), self.cid, raw_turns_max=2)
        started = time.monotonic()
        self.assertIsNotNone(conversation.append(role="user", kind="user", text=WORDS + " Third.",
                                                 message=WORDS + " Third."))
        self.assertTrue(newer.started.wait(10))
        # I12: compaction committed while the capture is still blocked in the provider.
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(1, len(self.store.load_conversation_state(self.cid).compacted))
        newer.release.set()
        self.settle(self.triggers)
        self.assertEqual([], watched_with.calls)
        self.assertEqual(1, len(newer.calls))
        self.assertEqual(["ok"], self.outcomes())
        self.assertEqual(1, self.records())                    # positive control: the answer stores


class ShutdownNeverWritesLate(_Base):
    def test_a_capture_past_the_bound_never_writes_and_its_process_is_terminated(self) -> None:
        model = SlowModel(block=True, spawn=True)
        self.addCleanup(lambda: model.process and model.process.kill())
        self.addCleanup(model.release.set)
        app = self.app(model)
        self.say(self.cid, WORDS, WORDS + " Again.")
        app.conversation_left(self.cid)
        self.assertTrue(model.started.wait(10))
        started = time.monotonic()
        aborted = app.shutdown_memory_capture(open_ids=(self.cid,), timeout=0.5)
        self.assertLess(time.monotonic() - started, 2.0)
        threads = list(app.memory_triggers._threads.values())
        self.assertEqual((self.cid,), aborted)
        self.assertEqual(model.calls, model.cancelled)         # the running call was told to stop
        self.assertIsNotNone(model.process.wait(5))            # ... and its child process is gone
        self.assertEqual(["aborted"], self.outcomes())
        self.finish(model, threads)                            # the provider now answers, late
        self.assertEqual(0, self.records())
        self.assertEqual(["aborted"], self.outcomes())
        self.assertFalse(app.conversation_left(self.cid))

    def test_a_queued_job_never_claims_a_run_after_shutdown(self) -> None:
        model = SlowModel(block=True)
        self.addCleanup(model.release.set)
        other = self.store.create_conversation(self.project).id
        self.say(self.cid, WORDS, WORDS + " Again.")
        self.say(other, WORDS, WORDS + " Other.")
        app = self.app(model)
        app.conversation_left(self.cid)
        self.assertTrue(model.started.wait(10))
        app.conversation_left(self.cid)                        # queued behind the running one
        app.shutdown_memory_capture(open_ids=(other,), timeout=0.3)
        threads = list(app.memory_triggers._threads.values())
        self.finish(model, threads)
        self.assertEqual(0, self.records())
        self.assertEqual({"aborted"}, set(self.outcomes()))
        resolver = mock.Mock(return_value=app)                 # halted: no app (no store) is even built
        app.memory_triggers._run(resolver, self.cid, SESSION_END, None)
        resolver.assert_not_called()


class TheBoundCoversTheWholeQuitPath(BackendBase):
    def start_capture(self) -> tuple[SlowModel, list]:
        self.current = model = SlowModel(block=True)
        self.addCleanup(model.release.set)
        self.say(self.cid, WORDS, WORDS + " Again.")
        self.backend.memory_capture_left(self.cid)
        self.assertTrue(model.started.wait(10))
        return model, list(self.triggers._threads.values())

    def test_a_refused_config_at_quit_still_aborts_and_nothing_writes_late(self) -> None:
        model, threads = self.start_capture()
        self.config_path.write_text("{ not json", encoding="utf-8")
        self.assertFalse(self.config.load().usable)
        started = time.monotonic()
        self.backend.memory_capture_shutdown(self.cid)
        self.assertLess(time.monotonic() - started, backend_module.MEMORY_CAPTURE_SHUTDOWN_SECONDS + 1.0)
        self.assertEqual(["aborted"], self.outcomes())          # through the app the run resolved
        self.finish(model, threads)
        self.assertEqual(0, self.records())
        self.assertEqual(["aborted"], self.outcomes())

    def test_a_hanging_config_load_at_quit_never_hangs_and_nothing_writes_late(self) -> None:
        model, threads = self.start_capture()
        unblock = threading.Event()
        self.addCleanup(unblock.set)
        load = self.config.load

        def hanging():
            unblock.wait(10)
            return load()
        bound = mock.patch.object(backend_module, "MEMORY_CAPTURE_SHUTDOWN_SECONDS", 0.6)
        bound.start()
        self.addCleanup(bound.stop)
        with mock.patch.object(self.config, "load", hanging):
            started = time.monotonic()
            self.backend.memory_capture_shutdown(self.cid)
            self.assertLess(time.monotonic() - started, 2.0)
        unblock.set()                                          # the late quit path resumes, the capture
        for thread in threading.enumerate():                   # thread still alive ...
            if thread.name == "jarvis-capture-quit":
                thread.join(10)
        self.assertEqual([None], self.outcomes())              # ... and aborts nothing late
        self.finish(model, threads)
        self.assertEqual(0, self.records())                    # nothing recorded: fail-closed
        self.assertEqual([None], self.outcomes())              # the run row stays as claimed


class PreCompactUsesCoresGate(_Base):
    def compact(self, conversation_id: str) -> None:
        conversation = PersistedConversation(self.store, conversation_id, raw_turns_max=1)
        for text in ("one", "two", "three"):
            self.assertIsNotNone(conversation.append(role="user", kind="user", text=text, message=text))

    def test_no_thread_where_core_would_refuse(self) -> None:
        app = self.app(SlowModel())
        app.watch_conversations(self.store)
        used = self.store.create_conversation(self.project).id
        self.store.set_global_memory_toggle(used, True, MemoryRecordActor.USER)
        self.store.set_global_memory_toggle(used, False, MemoryRecordActor.USER)   # ever used counts
        global_one = self.store.create_conversation(None).id
        with mock.patch.object(app.memory_triggers, "schedule", return_value=True) as schedule, \
                mock.patch("threading.Thread.start") as start:
            self.compact(used)
            self.compact(global_one)
            schedule.assert_not_called()
            start.assert_not_called()
            self.compact(self.cid)                               # positive control: gate open
            self.assertEqual(2, schedule.call_count)
            self.assertEqual({self.cid}, {c.args[1] for c in schedule.call_args_list})

    def test_the_window_reads_cores_ever_used_gate_not_the_current_toggle(self) -> None:
        backend = Backend(store=mock.Mock(load=lambda: mock.Mock(usable=True, settings=self.settings)))
        with mock.patch.object(backend, "conversations", return_value=self.store):
            self.assertTrue(backend._capture_on(self.cid))
            self.store.set_global_memory_toggle(self.cid, True, MemoryRecordActor.USER)
            self.store.set_global_memory_toggle(self.cid, False, MemoryRecordActor.USER)
            self.assertFalse(backend._capture_on(self.cid))


class SkipReadsTheSession(_Base):
    """SECCAPTURE F1: the skip policy decides on the session's user turns."""

    def test_precompact_on_a_long_session_captures_its_one_turn_batch(self) -> None:
        model = SlowModel()
        batch = self.say(self.cid, WORDS, WORDS + " Again.", WORDS + " Third.", raw_turns_max=2)
        self.assertEqual(1, len(batch))
        result = self.core().capture_memory(self.cid, PRECOMPACT, model, turns=batch)
        self.assertEqual("ok", result.outcome)
        self.assertEqual(1, len(model.calls))

    def test_a_short_or_empty_session_is_still_skipped(self) -> None:
        model = SlowModel()
        short = self.say(self.cid, "hi", "ok", "fine", raw_turns_max=2)
        self.assertEqual("skipped:too_short",
                         self.core().capture_memory(self.cid, PRECOMPACT, model, turns=short).outcome)
        empty = self.store.create_conversation(self.project).id
        replies = self.say(empty, WORDS, WORDS, WORDS, role="assistant", raw_turns_max=2)
        self.assertEqual("skipped:empty",
                         self.core().capture_memory(empty, PRECOMPACT, model, turns=replies).outcome)
        self.assertEqual([], model.calls)

    def test_a_reply_only_batch_of_a_long_session_is_skipped(self) -> None:
        model = SlowModel()
        self.say(self.cid, "Hello.", role="assistant")
        self.say(self.cid, WORDS, WORDS + " Again.")
        reply = self.say(self.cid, WORDS + " Third.", raw_turns_max=3)   # compacts the reply only
        self.assertEqual(["assistant"], [t.role for t in reply])
        self.assertEqual("skipped:empty",
                         self.core().capture_memory(self.cid, PRECOMPACT, model, turns=reply).outcome)
        self.assertEqual([], model.calls)

    def test_a_turn_in_both_the_batch_and_the_record_counts_once(self) -> None:
        model = SlowModel()
        self.say(self.cid, WORDS + " " + WORDS)                  # one user turn, over 200 chars
        turns = self.store.load_conversation_state(self.cid).turns
        result = self.core().capture_memory(self.cid, SESSION_END, model, turns=turns)
        self.assertEqual("skipped:too_short", result.outcome)
        self.assertEqual([], model.calls)


if __name__ == "__main__":
    unittest.main()
