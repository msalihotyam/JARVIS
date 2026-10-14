"""V2.1a M4: the Memory page's approval cards (MemoryApprovalCard).

* One card per pending Decision / Rule / Preference of the shown project, with
  its kind, text and source conversation; a fact (approved at once) has none.
* Approve / Reject go through the bridge slots to the app (Core decides); the
  card leaves once the record is no longer pending.
* Nothing pending reads as an honest empty line; a replaced record shows its
  chain (what replaced it, and on the newer card what it replaces).

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import unittest

from jarvis.conversation import append_turn
from tests.test_g6k6 import K6Case
from tests.test_ui import settled


class MemoryApprovalPageTests(K6Case):
    def setUp(self) -> None:
        super().setUp()
        self.ui = self.bridge()
        self.app = self.factory(self.store.load().settings)   # the same TEMP database
        self.memory = self.app.memory
        self.project = self.memory.create_project("Alpha").id
        self.conversation = self.memory.create_conversation(self.project).id

    def remembered(self, text: str, kind: str) -> str:
        created = []

        def change(state):
            state, turn = append_turn(state, role="user", kind="user", text=text, message=text)
            created.append(turn)
            return state

        self.memory.update_conversation_state(self.conversation, change)
        return self.app.remember_message(self.conversation, created[0].id, kind)["record"]["record_id"]

    def open_page(self):
        root = self.window(self.ui)
        self.page(self.ui, root, "memory")
        return root

    def texts(self, root, name):
        return [i.property("text") for i in self.named(root, name)]

    def test_one_card_per_pending_decision_rule_preference(self) -> None:
        self.remembered("We ship weekly", "decision")
        self.remembered("Never force-push", "rule")
        self.remembered("Dark mode please", "preference")
        self.remembered("Deploys run on Fridays", "fact")      # approved at once: no card
        root = self.open_page()
        settled(800, lambda: self.assertEqual(3, len(self.named(root, "memoryApprovalCard"))))
        self.assertEqual({"We ship weekly", "Never force-push", "Dark mode please"}, set(self.texts(root, "memoryApprovalText")))
        self.assertEqual({"Decision", "Rule", "Preference"}, {t.title() for t in self.texts(root, "memoryApprovalKind")})
        self.assertEqual(3, len(self.named(root, "memoryApprove")))
        self.assertEqual(3, len(self.named(root, "memoryReject")))
        self.assertTrue(all(t.startswith("Source: ") and len(t) > len("Source: ") for t in self.texts(root, "memoryApprovalSource")))
        self.assertEqual([], self.named(root, "memoryPendingEmpty"))
        for theme in ("dark", "space"):                          # both themes draw the card and its buttons
            self.ui.setTheme(theme)
            settled(300, lambda: self.assertEqual(3, len(self.named(root, "memoryApprove"))))

    def test_approve_calls_the_slot_and_the_card_leaves(self) -> None:
        record = self.remembered("We ship weekly", "decision")
        root = self.open_page()
        settled(800, lambda: self.assertEqual(1, len(self.named(root, "memoryApprovalCard"))))
        self.click(root, "memoryApprove", until=lambda: self.memory.current_status(record).value == "approved")
        settled(800, lambda: self.assertEqual([], self.named(root, "memoryApprovalCard")))
        self.assertTrue(self.item(root, "memoryPendingEmpty").isVisible())

    def test_reject_calls_the_slot_and_the_card_leaves(self) -> None:
        record = self.remembered("Never force-push", "rule")
        root = self.open_page()
        settled(800, lambda: self.assertEqual(1, len(self.named(root, "memoryApprovalCard"))))
        self.click(root, "memoryReject", until=lambda: self.memory.current_status(record).value == "rejected")
        settled(800, lambda: self.assertEqual([], self.named(root, "memoryApprovalCard")))

    def test_nothing_pending_is_an_honest_empty_line(self) -> None:
        self.remembered("Deploys run on Fridays", "fact")
        root = self.open_page()
        settled(800, lambda: self.assertTrue(self.item(root, "memoryPendingEmpty").isVisible()))
        self.assertIn("No decision, rule or preference is waiting for your approval.", self.shown_texts(root))
        self.assertEqual([], self.named(root, "memoryApprovalCard"))
        self.assertEqual([], self.named(root, "memoryApprove"))

    def test_a_replaced_record_shows_its_chain(self) -> None:
        root = self.open_page()
        settled(800, lambda: self.assertTrue(self.item(root, "memoryPendingEmpty").isVisible()))
        base = {"project_id": self.project, "conversation_id": None, "origin": "explicit_user", "classification": "internal",
                "subject_key": "release", "created_at": "", "expires_at": None}
        self.ui.memoryRecordsDone.emit({"ok": True, "project_id": self.project, "records": [
            dict(base, record_id="mem_old", kind="decision", text="We ship monthly", status="superseded", supersedes=None),
            dict(base, record_id="mem_new", kind="decision", text="We ship weekly", status="pending", supersedes="mem_old")]})
        settled(500, lambda: self.assertEqual(1, len(self.named(root, "memoryChain"))))
        self.assertEqual(["Replaced by: We ship weekly"], self.texts(root, "memoryApprovalReplacedBy"))
        self.assertEqual(["Replaces: We ship monthly"], self.texts(root, "memoryApprovalReplaces"))
        self.assertEqual({"Replaced by a newer record", "Awaiting your approval"}, set(self.texts(root, "memoryApprovalStatus")))
        self.assertEqual(1, len(self.named(root, "memoryApprove")))      # only the pending card asks
        # Another project's answer never replaces this page's records.
        self.ui.memoryRecordsDone.emit({"ok": True, "project_id": "other", "records": []})
        settled(200, lambda: self.assertEqual(1, len(self.named(root, "memoryChain"))))


if __name__ == "__main__":
    unittest.main()
