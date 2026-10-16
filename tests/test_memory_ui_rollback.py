"""SEC2BRIDGE: undo this conversation's records, the memory-off switch, a reply's sources.

* The Memory page's "Undo all from this conversation" asks the app for a
  preview (confirm=False) and opens a dialog naming its count; the confirm
  click calls the app once with confirm=True, the cancel click calls nothing.
* The bridge's conversationMemoryOff follows the app's answer both ways; a
  refusal leaves it where the app last said.
* A reply's usedMemory / memoryNoneRelevant reach the feed unchanged.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import jarvis.app as app_module
from tests.test_g6k6 import K6Case
from tests.test_ui import settled


def item(record_id, kind, text, status="pending", **extra):
    return {"record_id": record_id, "kind": kind, "text": text, "status": status, "project_id": "p1",
            "source_conversation_id": "c1", "subject_key": "", **extra}


class UndoConversationTests(K6Case):
    def setUp(self) -> None:
        super().setUp()
        self.ui = self.bridge()
        self.addCleanup(setattr, app_module.MEMORY_TRIGGERS, "batch_listener", None)
        self.undos: list[tuple] = []
        self.batch = {"ok": True, "conversation_id": "c1", "pending": [item("m1", "decision", "We ship weekly")],
                      "auto_saved": []}

    def undo(self, conversation_id, confirm=False):
        self.undos.append((conversation_id, confirm))
        return {"ok": True, "conversation_id": conversation_id, "project_id": "p1", "confirmed": confirm,
                "count": 2, "approved": 1, "pending": 1, "skipped": 0}

    def open_with_batch(self):
        root = self.window(self.ui)
        self.page(self.ui, root, "memory")
        worker = threading.Thread(target=app_module.MEMORY_TRIGGERS.batch_listener, args=("c1", self.batch))
        worker.start()
        worker.join()
        settled(800, lambda: self.assertEqual(1, len(self.named(root, "memoryBatchCard"))))
        return root

    def patched(self):
        return (mock.patch.object(self.backend, "undo_conversation_records", self.undo),
                mock.patch.object(self.backend, "memory_approval_batch", lambda cid: self.batch))

    def ask(self, root) -> None:
        self.click(root, "memoryUndoConversation", until=lambda: self.item(root, "memoryUndoConversationConfirm").isVisible())
        self.assertEqual([("c1", False)], self.undos)                    # the preview changes nothing
        self.assertIn("2 records from this conversation", self.item(root, "memoryUndoConversationCount").property("text"))

    def test_confirm_calls_the_app_once_with_confirm(self) -> None:
        undo, batch = self.patched()
        with undo, batch:
            root = self.open_with_batch()
            self.ask(root)
            settled(800, lambda: self.assertTrue(self.item(root, "memoryUndoConversationConfirm").property("enabled")))
            self.click(root, "memoryUndoConversationConfirm", until=lambda: ("c1", True) in self.undos)
            settled(500, lambda: self.assertEqual([("c1", False), ("c1", True)], self.undos))
            settled(800, lambda: self.assertEqual([], self.named(root, "memoryUndoConversationConfirm")))

    def test_cancel_makes_no_call(self) -> None:
        undo, batch = self.patched()
        with undo, batch:
            root = self.open_with_batch()
            self.ask(root)
            self.click(root, "memoryUndoConversationCancel")
            settled(800, lambda: self.assertEqual([], self.named(root, "memoryUndoConversationConfirm")))
            self.assertEqual([("c1", False)], self.undos)                # no confirm=True call
            self.assertEqual([], [u for u in self.undos if u[1]])


class MemoryOffAndSourcesTests(K6Case):
    def setUp(self) -> None:
        super().setUp()
        self.ui = self.bridge()
        if not self.ui._conversation_store.id:
            self.assertTrue(self.ui.newConversation())
        self.cid = self.ui._conversation_store.id
        self.assertTrue(self.cid)

    def test_the_switch_round_trips_through_the_app(self) -> None:
        sets: list[tuple] = []

        def answer(conversation_id, off):
            sets.append((conversation_id, off))
            return {"ok": True, "conversation_id": conversation_id, "conversation_memory_off": off}
        with mock.patch.object(self.backend, "set_conversation_memory_off", answer):
            self.assertFalse(self.ui.conversationMemoryOff)
            self.ui.setConversationMemoryOff(True)
            self.assertTrue(self.ui.conversationMemoryOff)
            self.ui.setConversationMemoryOff(False)
            self.assertFalse(self.ui.conversationMemoryOff)
        self.assertEqual([(self.cid, True), (self.cid, False)], sets)
        refused = {"ok": False, "error": {"code": "invalid_task", "message": "refused"}}
        with mock.patch.object(self.backend, "set_conversation_memory_off", lambda cid, off: refused):
            self.ui.setConversationMemoryOff(True)
        self.assertFalse(self.ui.conversationMemoryOff)                  # the app said no: it stays off=False

    def test_feed_items_keep_the_reply_sources_unchanged(self) -> None:
        used = [{"record_id": "m1", "kind": "decision", "label": "We ship weekly", "revision": 3}]
        self.ui._feed.append({"id": "r1", "kind": "jarvis", "turn": "t1", "msg": self.ui._redacted("Hello"), "ts": 0})
        self.ui._feed.append({"id": "r2", "kind": "jarvis", "turn": "t2", "msg": self.ui._redacted("Hi"), "ts": 0})
        answers = {"t1": {"usedMemory": used, "memoryNoneRelevant": False},
                   "t2": {"usedMemory": [], "memoryNoneRelevant": True}}
        with mock.patch.object(self.backend, "reply_memory_sources", lambda cid, turn: answers[turn]):
            self.ui._read_reply_sources(self.cid, ["t1", "t2"])
        rows = {row["id"]: row for row in self.ui.feed if row.get("id") in ("r1", "r2")}
        self.assertEqual(used, rows["r1"]["usedMemory"])
        self.assertIs(False, rows["r1"]["memoryNoneRelevant"])
        self.assertEqual([], rows["r2"]["usedMemory"])
        self.assertIs(True, rows["r2"]["memoryNoneRelevant"])


if __name__ == "__main__":
    unittest.main()
