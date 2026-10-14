"""SECIDLE: idle capture trigger, its run-ledger claim and the quota hand-off.

A fake clock drives the idle time; the watch thread polls never (hour-long
or one-minute interval, the test polls itself) and is started only to prove
shutdown stops it and that watching sets the current-app resolver. Core is a
stub (fake providers only).
"""

from __future__ import annotations

import unittest
from unittest import mock

from jarvis.app import (
    MEMORY_IDLE_TRIGGER_SECONDS,
    JarvisApp,
    MemoryTriggers,
    _quota_remaining_pct,
    _replying,
)
from jarvis.contracts import MemoryRecordOrigin
from jarvis.conversation import PersistedConversation
from jarvis.core import _capture_run_connection
from jarvis.memory import SQLiteMemoryStore
from tests.helpers import isolated_settings

SESSION_END = MemoryRecordOrigin.CAPTURE_SESSION_END
#: Two user turns past the 200-character minimum, so Core's skip rule passes.
LONG = "The build uses uv and the tests run with python -m unittest only. " * 2


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeModel:
    def __init__(self, observed: dict | None = None) -> None:
        self.observed = observed or {}


class StubCore:
    def __init__(self, outcome: str = "ok") -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, MemoryRecordOrigin, bool]] = []
        self.models: list[object] = []
        self.stops: list[object] = []
        self.during = lambda: None

    def capture_memory(self, conversation_id, trigger, model, *, turns=None, stop=None):
        self.calls.append((conversation_id, trigger, turns is None))
        self.models.append(model)
        self.stops.append(stop)
        self.during()
        return mock.Mock(outcome=self.outcome)


class IdleTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.store = SQLiteMemoryStore(self.settings.memory_database)
        self.cid = self.store.create_conversation(self.store.create_project("Alpha").id).id
        conversation = PersistedConversation(self.store, self.cid)
        for text in (LONG, LONG):
            self.assertIsNotNone(conversation.append(role="user", kind="user", text=text, message=text))
        self.clock = Clock()

    def make(self, outcome: str = "ok", model: FakeModel | None = None,
             core: StubCore | None = None) -> tuple[JarvisApp, StubCore]:
        core = core or StubCore(outcome)
        app = JarvisApp(core, self.store, self.settings, intent_model=model or FakeModel())
        app.memory_triggers = MemoryTriggers(clock=self.clock)
        # What start_idle_watch sets, without starting the watch thread.
        app.memory_triggers._idle_target = app
        self.addCleanup(app.memory_triggers.shutdown, app, (), 5.0)
        return app, core

    def idle(self, app: JarvisApp) -> int:
        """Pass the idle time, poll once, and wait for the worker it started."""
        self.clock.now += MEMORY_IDLE_TRIGGER_SECONDS
        scheduled = app.memory_triggers.poll_idle()
        with app.memory_triggers._lock:
            threads = list(app.memory_triggers._threads.values())
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        return scheduled

    def runs(self) -> list[tuple[str, str, str | None]]:
        with _capture_run_connection(self.store) as connection:
            return [tuple(row) for row in connection.execute(
                "SELECT run_id, trigger, outcome FROM memory_capture_runs ORDER BY started_at")]

    def test_idle_fires_once_after_the_idle_time(self) -> None:
        app, core = self.make()
        app.memory_triggers.touch(self.cid)
        self.clock.now += MEMORY_IDLE_TRIGGER_SECONDS - 1
        self.assertEqual(0, app.memory_triggers.poll_idle())
        self.clock.now -= MEMORY_IDLE_TRIGGER_SECONDS - 1
        self.assertEqual(1, self.idle(app))
        self.assertEqual([(self.cid, SESSION_END, True)], core.calls)
        self.assertEqual(0, self.idle(app))  # not re-armed without new activity
        self.assertEqual(1, len(core.calls))
        [(run_id, trigger, outcome)] = self.runs()
        self.assertTrue(run_id.startswith("idle-"))
        self.assertEqual(("session_end", "ok"), (trigger, outcome))

    def test_second_idle_in_the_same_window_makes_no_call(self) -> None:
        # A failed run leaves the turns uncaptured (no high-water mark):
        # only the window's claim stops the second idle.
        app, core = self.make(outcome="provider_error")
        app.memory_triggers.touch(self.cid)
        self.assertEqual(1, self.idle(app))
        app.memory_triggers.touch(self.cid)
        self.assertEqual(1, self.idle(app))
        self.assertEqual(1, len(core.calls))
        self.assertEqual([("session_end", "provider_error")], [run[1:] for run in self.runs()])

    def test_restart_idle_over_the_same_turns_makes_no_call(self) -> None:
        core = StubCore()
        first, _ = self.make(core=core)
        first.memory_triggers.touch(self.cid)
        self.idle(first)
        second, _ = self.make(core=core)  # a new process: no high-water mark
        second.memory_triggers.touch(self.cid)
        self.idle(second)
        self.assertEqual(1, len(core.calls))

    def test_new_turns_open_a_new_window(self) -> None:
        app, core = self.make(outcome="provider_error")
        app.memory_triggers.touch(self.cid)
        self.idle(app)
        PersistedConversation(self.store, self.cid).append(role="user", kind="user", text=LONG, message=LONG)
        app.memory_triggers.touch(self.cid)
        self.idle(app)
        self.assertEqual(2, len(core.calls))

    def test_temporary_conversation_makes_no_call(self) -> None:
        app, core = self.make()
        app.memory_triggers.touch(self.cid, temporary=True)
        self.assertEqual(0, self.idle(app))
        app.memory_triggers.touch(self.cid)
        app.memory_triggers.touch(self.cid, temporary=1)  # not exactly False: temporary
        self.assertEqual(0, self.idle(app))
        self.assertEqual([], core.calls)
        self.assertEqual([], self.runs())

    def test_quota_below_floor_makes_no_call(self) -> None:
        low = FakeModel({"r1": {"rate_limits": {"windows": {"five_hour": {"utilization": 0.95}}}}})
        app, core = self.make(model=low)
        app.memory_triggers.touch(self.cid)
        self.assertEqual(1, self.idle(app))
        self.assertEqual([], core.calls)
        self.assertEqual([], self.runs())

    def test_short_session_makes_no_call(self) -> None:
        cid = self.store.create_conversation(self.store.create_project("Beta").id).id
        PersistedConversation(self.store, cid).append(role="user", kind="user", text="hi", message="hi")
        app, core = self.make()
        app.memory_triggers.touch(cid)
        self.idle(app)
        self.assertEqual([], core.calls)

    def test_global_conversation_makes_no_call_and_no_run_row(self) -> None:
        cid = self.store.create_conversation(None).id
        conversation = PersistedConversation(self.store, cid)
        for text in (LONG, LONG):
            conversation.append(role="user", kind="user", text=text, message=text)
        app, core = self.make()
        app.memory_triggers.touch(cid)
        self.assertEqual(1, self.idle(app))
        self.assertEqual([], core.calls)
        self.assertEqual([], self.runs())

    def test_a_streaming_reply_is_not_idle(self) -> None:
        app, core = self.make()
        app.memory_triggers.touch(self.cid)
        with _replying(self.cid):
            self.assertEqual(0, self.idle(app))
        self.assertIn(self.cid, app.memory_triggers._active)  # still armed
        self.assertEqual(1, self.idle(app))
        self.assertEqual(1, len(core.calls))

    def test_quota_remaining_reads_only_reported_limits(self) -> None:
        self.assertIsNone(_quota_remaining_pct(FakeModel()))
        self.assertIsNone(_quota_remaining_pct(object()))
        self.assertIsNone(_quota_remaining_pct(FakeModel({"r": {"rate_limits": {}}})))
        claude = {"r": {"rate_limits": {"windows": {"a": {"utilization": 0.25}, "b": {"utilization": 0.6}}}}}
        self.assertAlmostEqual(40.0, _quota_remaining_pct(FakeModel(claude)))
        codex = {"r": {"quota": {"primary": {"used_percent": 30.0}, "checked_at": 5}}}
        self.assertAlmostEqual(70.0, _quota_remaining_pct(FakeModel(codex)))
        reached = {"r": {"quota": {"primary": {"used_percent": 30.0}, "limit_reached": True}}}
        self.assertEqual(0.0, _quota_remaining_pct(FakeModel(reached)))
        # The LAST report counts, not an older one.
        later = {"old": {"quota": {"primary": {"used_percent": 99.0}}}, "new": codex["r"]}
        self.assertAlmostEqual(70.0, _quota_remaining_pct(FakeModel(later)))

    def test_submit_text_arms_the_idle_trigger(self) -> None:
        app, _ = self.make()
        with mock.patch.object(app, "_submit_text", side_effect=RuntimeError("turn failed")):
            with self.assertRaises(RuntimeError):
                app.submit_text("hello", conversation_id=self.cid)
        self.assertIn(self.cid, app.memory_triggers._active)
        with mock.patch.object(app, "_submit_text", return_value="done"):
            app.memory_triggers._active.clear()
            self.assertEqual("done", app.submit_text("hello"))
        self.assertEqual({}, app.memory_triggers._active)

    def test_shutdown_stops_the_idle_watch_and_disarms(self) -> None:
        app, core = self.make()
        triggers = app.memory_triggers
        triggers.touch(self.cid)
        triggers.start_idle_watch(app, interval=3600)
        watcher = triggers._watcher
        self.assertTrue(watcher.is_alive())
        triggers.shutdown(app, (), 5.0)
        self.assertFalse(watcher.is_alive())
        self.assertEqual({}, triggers._active)
        triggers.touch(self.cid)  # closed: no longer armed
        self.assertEqual(0, self.idle(app))
        self.assertEqual([], core.calls)

    def test_idle_capture_never_writes_after_shutdown(self) -> None:
        # Halted before the job runs: no claim, no call.
        app, core = self.make()
        app.memory_triggers.touch(self.cid)
        app.memory_triggers._stop.stop()
        self.assertEqual(1, self.idle(app))
        self.assertEqual(([], []), (core.calls, self.runs()))
        # Halted during the model call: the capture got the stop, and the
        # claimed run is never finished (shutdown's abort closes it).
        app, core = self.make()
        triggers = app.memory_triggers
        core.during = triggers.halt
        triggers.touch(self.cid)
        self.assertEqual(1, self.idle(app))
        self.assertEqual([triggers._stop], core.stops)
        [(run_id, trigger, outcome)] = self.runs()
        self.assertTrue(run_id.startswith("idle-"))
        self.assertEqual(("session_end", None), (trigger, outcome))

    def test_idle_uses_the_current_app_after_a_model_switch(self) -> None:
        first, first_core = self.make()
        switched = FakeModel()
        second_core = StubCore()
        second = JarvisApp(second_core, self.store, self.settings, intent_model=switched)
        current = [first]
        first.watch_conversations(self.store, lambda: current[0])
        with mock.patch.object(first, "_submit_text", return_value="done"):
            first.submit_text("hello", conversation_id=self.cid)  # armed by the old app
        current[0] = second  # the person switches the model
        self.assertEqual(1, self.idle(first))
        self.assertEqual([], first_core.calls)
        self.assertEqual([(self.cid, SESSION_END, True)], second_core.calls)
        self.assertEqual([switched], second_core.models)
        self.assertIs(first.memory_triggers._idle_target(), second)


if __name__ == "__main__":
    unittest.main()
