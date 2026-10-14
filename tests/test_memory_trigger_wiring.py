"""MEMTRIGWIRE (V2.1a M2c): the window fires the memory capture triggers.

Exactly four hooks: the window's conversation store is watched once
(PreCompact); a switch / new conversation leaves the old one (SessionEnd); an
archive leaves the archived one; quitting runs the bounded capture shutdown.
Capture off (a Global conversation, or "all projects memory" on) makes them
no-ops, and a failing trigger never breaks the window (I12).

The ``JarvisApp`` trigger methods are spies (patched before the UI case's own
cleanup, so no bridge ever reaches the process-wide ``MEMORY_TRIGGERS``).
Temp stores and a scripted model only: no live DB, model, auth or network call.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from jarvis.app import JarvisApp, MemoryTriggers
from jarvis.contracts import MemoryRecordActor
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.ui import backend as backend_module
from tests.test_dfnew import say
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import UiTestCase

HOOKS = ("watch_conversations", "conversation_left", "shutdown_memory_capture")


class MemoryTriggerWiringTests(UiTestCase):
    def setUp(self) -> None:
        self.spies = {}
        for name in HOOKS:                       # started before UiTestCase.setUp: stopped after its bridges
            patcher = patch.object(JarvisApp, name, autospec=True)
            self.spies[name] = patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
        store = SQLiteMemoryStore(self.database)
        self.memory = store
        project = store.create_project("A")
        self.c1 = store.create_conversation(project.id).id
        self.c2 = store.create_conversation(project.id).id
        for cid in (self.c1, self.c2):
            say(store, cid, f"merhaba {cid}")

    def left(self) -> list[str]:
        return [c.args[1] for c in self.spies["conversation_left"].call_args_list]

    def test_watch_is_installed_once_on_the_window_store(self) -> None:
        bridge = self.bridge()
        store = bridge.backend.conversations()
        bridge.backend.conversations()
        watch = self.spies["watch_conversations"]
        self.assertEqual(1, watch.call_count)
        self.assertIs(store, watch.call_args.args[1])
        self.assertTrue(bridge.openConversation(self.c1))
        self.assertIs(store, bridge._conversation_store.store)  # compaction of the open one reaches the hook

    def test_a_failed_watch_is_retried(self) -> None:
        watch = self.spies["watch_conversations"]
        watch.side_effect = RuntimeError("not yet")
        bridge = self.bridge()
        watch.side_effect = None
        calls = watch.call_count
        store = bridge.backend.conversations()
        bridge.backend.conversations()
        self.assertEqual(calls + 1, watch.call_count)
        self.assertIs(store, watch.call_args.args[1])

    def test_delete_never_fires(self) -> None:
        bridge = self.bridge()
        self.assertTrue(bridge.openConversation(self.c1))
        self.assertTrue(bridge.archiveConversation(self.c1))   # delete is the second step (Gate 6D-b)
        self.assertEqual([self.c1], self.left())
        bridge.previewDeleteConversation(self.c1)
        self.assertEqual("", bridge.conversationNotice)
        self.assertTrue(bridge.confirmDeleteConversation(self.c1))
        self.assertIsNone(self.memory.load_conversation(self.c1))
        self.assertEqual([self.c1], self.left())               # the delete added nothing

    def test_switch_and_new_leave_the_old_conversation_once(self) -> None:
        bridge = self.bridge()
        self.assertTrue(bridge.openConversation(self.c1))      # left the startup Global one: capture off
        self.assertEqual([], self.left())
        self.assertTrue(bridge.openConversation(self.c2))
        self.assertEqual([self.c1], self.left())
        self.assertTrue(bridge.openConversation(self.c2))      # the same id: nothing
        self.assertEqual([self.c1], self.left())
        self.assertTrue(bridge.newConversation())
        self.assertEqual([self.c1, self.c2], self.left())
        self.assertTrue(bridge.openConversation(self.c1))      # left a Global one again
        self.assertEqual([self.c1, self.c2], self.left())

    def test_archive_leaves_the_archived_conversation_once(self) -> None:
        bridge = self.bridge()
        self.assertTrue(bridge.openConversation(self.c1))
        self.assertTrue(bridge.archiveConversation(self.c1))   # the open one: the window moves on, no second fire
        self.assertEqual([self.c1], self.left())
        self.assertTrue(bridge.archiveConversation(self.c2))   # not open
        self.assertEqual([self.c1, self.c2], self.left())
        self.assertFalse(bridge.archiveConversation("c-unknown"))  # refused by the store
        self.assertEqual([self.c1, self.c2], self.left())

    def test_capture_off_makes_the_hooks_no_ops(self) -> None:
        self.memory.set_global_memory_toggle(self.c1, True, MemoryRecordActor.USER)
        bridge = self.bridge()
        self.assertTrue(bridge.openConversation(self.c1))
        self.assertTrue(bridge.openConversation(self.c2))      # c1 has all projects memory on
        self.assertTrue(bridge.archiveConversation(self.c1))
        self.assertTrue(bridge.newConversation())              # leaves c2: capture on
        self.assertEqual([self.c2], self.left())
        bridge.shutdown()                                      # the open one is Global
        shutdown = self.spies["shutdown_memory_capture"]
        self.assertEqual(1, shutdown.call_count)
        self.assertEqual((), shutdown.call_args.kwargs["open_ids"])

    def test_shutdown_session_ends_the_open_conversation_bounded(self) -> None:
        bridge = self.bridge()
        self.assertTrue(bridge.openConversation(self.c1))
        bridge.shutdown()
        shutdown = self.spies["shutdown_memory_capture"]
        self.assertEqual(1, shutdown.call_count)
        self.assertEqual({"open_ids": (self.c1,), "timeout": backend_module.MEMORY_CAPTURE_SHUTDOWN_SECONDS},
                         shutdown.call_args.kwargs)
        self.assertLessEqual(backend_module.MEMORY_CAPTURE_SHUTDOWN_SECONDS, 5.0)

    def test_a_raising_trigger_never_breaks_the_window(self) -> None:
        for spy in self.spies.values():
            spy.side_effect = RuntimeError("capture broke")
        bridge = self.bridge()
        self.assertIsNotNone(bridge.backend.conversations())
        self.assertTrue(bridge.openConversation(self.c1))
        self.assertTrue(bridge.openConversation(self.c2))
        self.assertEqual(self.c2, bridge._conversation_store.id)
        self.assertTrue(bridge.archiveConversation(self.c1))
        self.assertIsNotNone(self.memory.load_conversation(self.c1).archived_at)
        bridge.shutdown()
        self.assertEqual(1, self.spies["shutdown_memory_capture"].call_count)
        # The bridge's own guard too: a backend hook that raises.
        bridge2 = self.bridge()
        bridge2.backend.memory_capture_left = lambda cid: (_ for _ in ()).throw(RuntimeError("x"))
        self.assertTrue(bridge2.openConversation(self.c2))
        self.assertTrue(bridge2.newConversation())

    def test_shutdown_never_waits_on_a_running_capture_beyond_the_bound(self) -> None:
        triggers, entered, release = MemoryTriggers(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked(core, conversation_id, trigger, model, **kwargs):
            entered.set()
            release.wait(30)
        self.spies["shutdown_memory_capture"].side_effect = (
            lambda app, open_ids=(), timeout=5.0: triggers.shutdown(app, open_ids, timeout))
        bound = patch.object(backend_module, "MEMORY_CAPTURE_SHUTDOWN_SECONDS", 0.3)
        capture = patch.object(AgentCore, "capture_memory", blocked)
        bound.start()
        capture.start()
        self.addCleanup(bound.stop)
        self.addCleanup(capture.stop)
        bridge = self.bridge()
        self.assertTrue(bridge.openConversation(self.c1))
        started = time.monotonic()
        bridge.shutdown()
        elapsed = time.monotonic() - started
        self.assertTrue(entered.wait(5), "the open conversation's capture never ran")
        self.assertLess(elapsed, 2.5)                          # bound 0.3 s; the capture itself blocks for 30 s


if __name__ == "__main__":
    unittest.main()
