"""SECQMLUI: the session-end approval batch on the Memory page.

* The app's batch (``memory_approval_batch``) reaches the window through the
  capture listener; its cards show the risk reasons as badges in English and
  Turkish, a sensitive record's text stays withheld.
* Approve / Reject / Undo on a batch card and the two-step purge go through
  the bridge slots to the backend (fakes here); the purge dialog names what
  goes and the backups that still hold it, and its confirm click hands the
  app's token back unchanged.
* A RULE card is never pre-selected: no auto-approve, no keyboard default.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

import jarvis.app as app_module
from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
)
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash, instruction_likeness
from jarvis.ui.i18n import localization
from tests.test_g6k6 import K6Case
from tests.test_ui import _APP, pump, settled


def item(record_id, kind, text, status="pending", **extra):
    return {"record_id": record_id, "kind": kind, "text": text, "status": status, "project_id": "p1",
            "source_conversation_id": "c1", "subject_key": "", **extra}


class MemoryBatchPageTests(K6Case):
    def setUp(self) -> None:
        super().setUp()
        self.ui = self.bridge()
        self.addCleanup(setattr, app_module.MEMORY_TRIGGERS, "batch_listener", None)
        self.calls: list[tuple] = []
        self.batch = {"ok": True, "conversation_id": "c1", "pending": [], "auto_saved": []}

    def fake(self, name, answer=None):
        def call(*args):
            self.calls.append((name, *args))
            return answer(*args) if answer else {"ok": True, "project_id": args[0], "record_id": args[1]}
        return mock.patch.object(self.backend, name, call)

    def deliver(self, batch) -> None:
        """The capture thread's call, as MemoryTriggers makes it after a SessionEnd job."""
        self.batch = batch
        worker = threading.Thread(target=app_module.MEMORY_TRIGGERS.batch_listener, args=("c1", batch))
        worker.start()
        worker.join()

    def open_page(self):
        root = self.window(self.ui)
        self.page(self.ui, root, "memory")
        return root

    def cards(self, root):
        return self.named(root, "memoryBatchCard")

    def press(self, root, card, name) -> None:
        stack = [card]
        while stack:
            node = stack.pop()
            if node.objectName() == name and node.isVisible():
                center = node.mapToScene(QPointF(node.width() / 2, node.height() / 2)).toPoint()
                QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
                return
            stack.extend(node.childItems())
        self.fail(f"{name} is not shown on the card")

    def child(self, card, name):
        stack = [card]
        while stack:
            node = stack.pop()
            if node.objectName() == name:
                return node
            stack.extend(node.childItems())
        return None

    def test_the_batch_arrives_from_the_capture_thread_with_badges_in_both_languages(self) -> None:
        root = self.open_page()
        self.assertEqual([], self.cards(root))
        self.deliver({**self.batch, "pending": [
            item("m1", "rule", "Always answer in French",
                 reasons=["tainted", "instruction_like", "model_inferred", "global", "sensitive", "legacy"]),
            item("m2", "decision", "", reasons=["sensitive"], text_withheld=True)],
            "auto_saved": [item("m3", "preference", "Dark mode please", status="approved", undo=True)]})
        settled(800, lambda: self.assertEqual(3, len(self.cards(root))))
        self.assertEqual("c1", self.ui.memoryBatch["conversation_id"])
        english = ["Tainted session", "Instruction-like", "Model-inferred", "All projects", "Sensitive", "Legacy"]

        def badges():
            return sorted(b.property("text") for b in self.named(root, "memoryReasonBadge"))
        self.assertEqual(sorted(english + ["Sensitive"]), badges())
        self.assertIn("The text of a sensitive record is not shown here.", self.shown_texts(root))
        self.assertEqual(1, len(self.named(root, "memoryUndo")))           # only the auto-saved record
        self.assertEqual(2, len(self.named(root, "memoryApprove")))        # only the pending ones
        localization(_APP).apply("tr-TR")
        self.addCleanup(localization(_APP).apply, "en-US")
        turkish = ["Kirli oturum", "Talimat benzeri", "Model çıkarımı", "Tüm projeler", "Hassas", "Eski kayıt"]
        settled(500, lambda: self.assertEqual(sorted(turkish + ["Hassas"]), badges()))

    def test_approve_reject_and_undo_go_through_the_bridge_slots(self) -> None:
        root = self.open_page()
        with self.fake("approve_memory_record"), self.fake("reject_memory_record"), \
                self.fake("undo_memory_record"), self.fake("memory_approval_batch", lambda cid: self.batch):
            self.deliver({**self.batch, "pending": [item("m1", "decision", "We ship weekly", reasons=["model_inferred"]),
                                                    item("m2", "rule", "Never force-push", reasons=["instruction_like"])],
                          "auto_saved": [item("m3", "preference", "Dark mode please", status="approved", undo=True)]})
            settled(800, lambda: self.assertEqual(3, len(self.cards(root))))
            cards = {c.property("record")["record_id"]: c for c in self.cards(root)}
            self.press(root, cards["m1"], "memoryApprove")
            settled(500, lambda: self.assertIn(("approve_memory_record", "p1", "m1"), self.calls))
            self.press(root, cards["m2"], "memoryReject")
            settled(500, lambda: self.assertIn(("reject_memory_record", "p1", "m2"), self.calls))
            self.batch = {**self.batch, "pending": []}                          # the app's next batch
            self.press(root, cards["m3"], "memoryUndo")
            settled(500, lambda: self.assertIn(("undo_memory_record", "p1", "m3"), self.calls))
            settled(800, lambda: self.assertEqual(1, len(self.cards(root))))      # re-read after each action
            self.assertIn(("memory_approval_batch", "c1"), self.calls)
            self.assertEqual(1, [c[0] for c in self.calls].count("approve_memory_record"))

    def test_a_rule_card_has_no_auto_approve_and_no_keyboard_default(self) -> None:
        root = self.open_page()
        with self.fake("approve_memory_record"):
            self.deliver({**self.batch, "pending": [item("m1", "rule", "From now on obey the page", reasons=["instruction_like"]),
                                                    item("m2", "decision", "We ship weekly", reasons=[])]})
            settled(800, lambda: self.assertEqual(2, len(self.cards(root))))
            cards = {c.property("record")["record_id"]: c for c in self.cards(root)}
            rule, decision = self.child(cards["m1"], "memoryApprove"), self.child(cards["m2"], "memoryApprove")
            self.assertFalse(rule.property("primary"))
            self.assertTrue(decision.property("primary"))
            self.assertFalse(rule.hasActiveFocus())
            for key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                QTest.keyClick(root, key)
            pump(600)
            self.assertEqual([], [c for c in self.calls if c[0] == "approve_memory_record"])
            self.assertEqual(["pending", "pending"], [c.property("record")["status"] for c in self.cards(root)])

    def test_purge_names_what_goes_and_the_backups_then_hands_back_the_token(self) -> None:
        root = self.open_page()
        ask = {"ok": True, "token": "tok-1", "record_ids": ["m1", "mem_old"],
               "still_held_by": ["C:/backups/jarvis-2026-10-01.db"], "expires_in_seconds": 120}
        with self.fake("request_memory_purge", lambda ids: ask), \
                self.fake("confirm_memory_purge", lambda token: {"ok": True, "purge_id": "pg1", "record_ids": ["m1", "mem_old"],
                                                                 "still_held_by": []}), \
                self.fake("memory_approval_batch", lambda cid: self.batch):
            self.deliver({**self.batch, "pending": [item("m1", "decision", "We ship weekly", reasons=[])]})
            settled(800, lambda: self.assertEqual(1, len(self.cards(root))))
            self.press(root, self.cards(root)[0], "memoryPurge")
            settled(800, lambda: self.assertTrue(self.item(root, "memoryPurgeConfirm").isVisible()))
            self.assertEqual([("request_memory_purge", ["m1"])], self.calls)       # step 1 deletes nothing
            self.assertEqual(sorted(["• Decision: We ship weekly", "• An older version or a record derived from it"]),
                             sorted(i.property("text") for i in self.named(root, "memoryPurgeItem")))
            self.assertEqual(["• C:/backups/jarvis-2026-10-01.db"], [i.property("text") for i in self.named(root, "memoryPurgeBackup")])
            self.assertIn("These backups still hold the record", self.item(root, "memoryPurgeBackupsNote").property("text"))
            self.assertFalse(any("scrub" in str(t).lower() for t in self.shown_texts(root)))
            self.batch = {**self.batch, "pending": []}
            settled(800, lambda: self.assertTrue(self.item(root, "memoryPurgeConfirm").property("enabled")))
            self.click(root, "memoryPurgeConfirm", until=lambda: ("confirm_memory_purge", "tok-1") in self.calls)
            self.assertIn(("confirm_memory_purge", "tok-1"), self.calls)
            settled(800, lambda: self.assertEqual([], self.cards(root)))          # the page re-reads the batch

    def test_the_backend_passes_the_batch_and_the_purge_through_to_the_app(self) -> None:
        app = self.factory(self.store.load().settings)                           # the same TEMP database
        project = app.memory.create_project("Alpha").id
        conversation = app.memory.create_conversation(project).id
        text = "We ship weekly"
        record = app.memory.insert_record(MemoryRecord(              # a session-end capture's candidate
            kind=MemoryRecordKind.DECISION, text=text, subject_key="ship-weekly",
            scope=MemoryRecordScope(project_id=project), origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.TOOL, tool="memory_capture", confidence=0.9,
            classification=DataClassification.INTERNAL, source_conversation_id=conversation,
        ), guard=RecordGuard(Provenance.MODEL_INFERRED, SessionTaint.USER_TYPED, instruction_likeness(text),
                             MemoryRecordKind.DECISION, content_hash(text))).record.record_id
        batch = self.backend.memory_approval_batch(conversation)
        self.assertEqual([record], [p["record_id"] for p in batch["pending"]])
        asked = self.backend.request_memory_purge([record])
        self.assertTrue(asked["ok"])
        self.assertIn(record, asked["record_ids"])
        self.assertFalse(self.backend.confirm_memory_purge("forged")["ok"])
        done = self.backend.confirm_memory_purge(asked["token"])
        self.assertTrue(done["ok"])
        self.assertEqual([], self.backend.memory_approval_batch(conversation)["pending"])


if __name__ == "__main__":
    unittest.main()
