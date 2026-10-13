"""V2.1a M2: SessionEnd and PreCompact capture triggers (fake providers only)."""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from jarvis.app import JarvisApp, MemoryTriggers, conversation_delete_result
from jarvis.contracts import MemoryRecordOrigin
from jarvis.conversation import PersistedConversation, register_precompact_hook
from jarvis.memory import SQLiteMemoryStore
from tests.helpers import isolated_settings

PRECOMPACT = MemoryRecordOrigin.CAPTURE_PRECOMPACT
SESSION_END = MemoryRecordOrigin.CAPTURE_SESSION_END


class StubCore:
    """Records what the app asks Core to capture; Core itself is out of scope."""

    def __init__(self, store: SQLiteMemoryStore, gate: threading.Event | None = None,
                 fail: bool = False) -> None:
        self.store = store
        self.gate = gate
        self.fail = fail
        self.outcome = "ok"
        self.calls: list[tuple[str, MemoryRecordOrigin, tuple[str, ...] | None, tuple[str, ...]]] = []
        self.aborted: list[str] = []
        self.started = threading.Event()

    def capture_memory(self, conversation_id, trigger, model, *, turns=None):
        # What is still raw in the store when the capture runs.
        raw = tuple(t.text for t in self.store.load_conversation_state(conversation_id).turns)
        self.calls.append((conversation_id, trigger,
                           None if turns is None else tuple(t.text for t in turns), raw))
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("capture failed")
        return mock.Mock(outcome=self.outcome)

    def abort_capture_runs(self, conversation_id):
        self.aborted.append(conversation_id)
        return 1


class MemoryTriggersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.store = SQLiteMemoryStore(self.settings.memory_database)
        self.project = self.store.create_project("Alpha").id
        self.cid = self.store.create_conversation(self.project).id
        self.addCleanup(register_precompact_hook, self.store, None)

    def make_app(self, **core) -> tuple[JarvisApp, StubCore]:
        stub = StubCore(self.store, **core)
        app = JarvisApp(stub, self.store, self.settings, intent_model=object())
        app.memory_triggers = MemoryTriggers()
        self.addCleanup(app.memory_triggers.shutdown, app, (), 5.0)
        return app, stub

    def say(self, conversation: PersistedConversation, *texts: str) -> None:
        for text in texts:
            self.assertIsNotNone(conversation.append(role="user", kind="user", text=text, message=text))

    def settle(self, triggers: MemoryTriggers) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with triggers._lock:
                if not triggers._threads and not triggers._jobs:
                    return
            time.sleep(0.01)
        self.fail("capture worker did not settle")

    def test_precompact_gets_exactly_the_compacted_turns_before_compaction(self) -> None:
        app, stub = self.make_app(gate=threading.Event())
        app.watch_conversations(self.store)
        conversation = PersistedConversation(self.store, self.cid, raw_turns_max=3)
        self.say(conversation, "t1", "t2", "t3")
        self.assertEqual([], stub.calls)
        self.say(conversation, "t4")
        # The hook ran inside the write transaction, before compaction: the
        # doomed turn is exactly t1 (capturing after the loop would see t2..t4).
        self.assertTrue(stub.started.wait(5))
        stub.gate.set()
        self.settle(app.memory_triggers)
        self.assertEqual([(self.cid, PRECOMPACT, ("t1",))], [c[:3] for c in stub.calls])
        state = self.store.load_conversation_state(self.cid)
        self.assertEqual(("t2", "t3", "t4"), tuple(t.text for t in state.turns))
        self.assertEqual(1, len(state.compacted))

    def test_compaction_proceeds_when_capture_raises(self) -> None:
        app, stub = self.make_app(fail=True)
        app.watch_conversations(self.store)
        conversation = PersistedConversation(self.store, self.cid, raw_turns_max=2)
        self.say(conversation, "a", "b", "c", "d")
        self.settle(app.memory_triggers)
        state = self.store.load_conversation_state(self.cid)
        self.assertEqual(("c", "d"), tuple(t.text for t in state.turns))
        self.assertEqual(2, len(state.compacted))
        self.assertTrue(all(c[1] is PRECOMPACT for c in stub.calls))

        def boom(cid, turns):
            raise RuntimeError("hook failed")
        register_precompact_hook(self.store, boom)
        self.say(conversation, "e")
        state = self.store.load_conversation_state(self.cid)
        self.assertEqual(("d", "e"), tuple(t.text for t in state.turns))
        self.assertEqual(3, len(state.compacted))

    def test_unregistered_store_never_fires(self) -> None:
        app, stub = self.make_app()
        conversation = PersistedConversation(self.store, self.cid, raw_turns_max=1)
        self.say(conversation, "a", "b", "c")
        self.settle(app.memory_triggers)
        self.assertEqual([], stub.calls)
        self.assertEqual(2, len(self.store.load_conversation_state(self.cid).compacted))

    def test_session_end_fires_once_per_leave_on_uncaptured_turns(self) -> None:
        app, stub = self.make_app()
        conversation = PersistedConversation(self.store, self.cid)
        self.say(conversation, "x1", "x2")
        with mock.patch.object(self.store, "consolidate_scope", wraps=self.store.consolidate_scope) as consolidate:
            self.assertTrue(app.conversation_left(self.cid))
            self.settle(app.memory_triggers)
            self.assertEqual([(self.cid, SESSION_END, None)], [c[:3] for c in stub.calls])
            self.assertEqual(1, consolidate.call_count)
            self.assertEqual(self.project, consolidate.call_args.args[0])
            # A second leave with nothing new captures nothing, still consolidates.
            app.conversation_left(self.cid)
            self.settle(app.memory_triggers)
            self.assertEqual(1, len(stub.calls))
            # A refused run (provider error) does not advance the mark.
            self.say(conversation, "x3")
            stub.outcome = "provider_error"
            app.conversation_left(self.cid)
            self.settle(app.memory_triggers)
            self.assertEqual((self.cid, SESSION_END, ("x3",)), stub.calls[-1][:3])
            stub.outcome = "ok"
            app.conversation_left(self.cid)
            self.settle(app.memory_triggers)
        self.assertEqual((self.cid, SESSION_END, ("x3",)), stub.calls[-1][:3])
        self.assertEqual(3, len(stub.calls))

    def test_at_most_one_run_per_conversation(self) -> None:
        gate = threading.Event()
        app, stub = self.make_app(gate=gate)
        PersistedConversation(self.store, self.cid).append(role="user", kind="user", text="q", message="q")
        app.conversation_left(self.cid)
        self.assertTrue(stub.started.wait(5))
        app.conversation_left(self.cid)
        with app.memory_triggers._lock:
            self.assertEqual(1, len(app.memory_triggers._threads))
        gate.set()
        self.settle(app.memory_triggers)
        self.assertEqual(1, len(stub.calls))

    def test_delete_never_captures(self) -> None:
        app, stub = self.make_app()
        conversation = PersistedConversation(self.store, self.cid)
        self.say(conversation, "secret")
        self.store.archive_conversation(self.cid)
        record = self.store.load_conversation(self.cid)
        state = self.store.load_conversation_state(self.cid)
        with mock.patch.object(MemoryTriggers, "schedule") as schedule:
            result = conversation_delete_result(self.store, self.settings, self.cid, confirm=self.cid,
                                                expected_next_seq=state.next_seq, expected_title=record.title)
        self.assertNotIn("error", result.payload)
        schedule.assert_not_called()
        self.assertEqual([], stub.calls)

    def test_shutdown_is_bounded_and_records_abort(self) -> None:
        gate = threading.Event()
        app, stub = self.make_app(gate=gate)
        self.addCleanup(gate.set)
        self.say(PersistedConversation(self.store, self.cid), "long")
        started = time.monotonic()
        aborted = app.shutdown_memory_capture(open_ids=(self.cid,), timeout=0.2)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual((self.cid,), aborted)
        self.assertEqual([self.cid], stub.aborted)
        self.assertFalse(app.conversation_left(self.cid))

    def test_shutdown_reports_no_abort_core_did_not_record(self) -> None:
        gate = threading.Event()
        app, stub = self.make_app(gate=gate)
        self.addCleanup(gate.set)
        self.say(PersistedConversation(self.store, self.cid), "long")
        with mock.patch.object(stub, "abort_capture_runs", side_effect=AttributeError):
            self.assertEqual((), app.shutdown_memory_capture(open_ids=(self.cid,), timeout=0.2))


if __name__ == "__main__":
    unittest.main()
