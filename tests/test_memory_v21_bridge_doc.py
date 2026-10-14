"""V2.1a M4 bridge slots: "Oturumu sıkıştır" and the Memory Doctor.
Each slot runs the real app method over a throwaway TEMP database and emits
its answer whole; a refusal (a reply still streaming, a stale finding)
reaches the window as a visible error, never silence. No model or connector
is called."""

from __future__ import annotations

import unittest
from unittest import mock

from jarvis import app as app_module
from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
)
from jarvis.conversation import PersistedConversation
from jarvis.memory import MemoryRecordApproval
from tests.test_ui import UiTestCase

TEXTS = tuple(f"mesaj {n}" for n in range(10))


class CompactAndDoctorBridgeTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ui = self.bridge()
        self.app = self.factory(self.store.load().settings)   # the same TEMP database, for setup and checks
        self.memory = self.app.memory
        self.project = self.memory.create_project("Alpha").id
        self.cid = self.memory.create_conversation(self.project).id
        conversation = PersistedConversation(self.memory, self.cid)
        for n, text in enumerate(TEXTS):
            role = "user" if n % 2 == 0 else "assistant"
            conversation.append(role=role, kind=role if role == "user" else "reply", text=text,
                                message=text if role == "user" else "", key=f"k{n}", now=1000 + n)
        self.answers: dict[str, list[dict]] = {"compact": [], "doctor": [], "repair": []}
        self.ui.compactConversationDone.connect(self.answers["compact"].append)
        self.ui.memoryDoctorDone.connect(self.answers["doctor"].append)
        self.ui.memoryRepairDone.connect(self.answers["repair"].append)

    def raw(self) -> tuple[str, ...]:
        return tuple(t.text for t in self.memory.load_conversation_state(self.cid).turns)

    def add(self, text: str, subject: str, **extra) -> str:
        record = MemoryRecord(
            kind=MemoryRecordKind.FACT, text=text, subject_key=subject,
            scope=MemoryRecordScope(self.project, None), origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.MODEL, confidence=0.8, classification=DataClassification.INTERNAL, **extra,
        )
        record_id = self.memory.insert_record(record).record.record_id
        # A guard-less capture record starts PENDING (SECCAPTURE); the human approve makes it active.
        self.memory.approve_record(record_id, MemoryRecordApproval(self.project, record_id))
        return record_id

    def assertRefusedVisibly(self, answer: dict, feed_before: int) -> None:
        self.assertIs(answer["ok"], False)
        self.assertGreater(len(self.ui.feed), feed_before)   # this call said so, not an earlier one
        self.assertIsInstance(answer["error"], str)
        self.assertTrue(answer["error"])
        self.assertEqual(self.ui.feed[-1]["kind"], "error")

    # -- compaction --------------------------------------------------------------

    def test_compact_slot_compacts_through_the_app_and_shows_the_new_usage(self) -> None:
        self.assertTrue(self.ui.openConversation(self.cid))
        self.ui.compactConversation(self.cid)
        answer, = self.answers["compact"]
        self.assertIs(answer["ok"], True)
        self.assertEqual(answer["conversation_id"], self.cid)
        self.assertEqual(TEXTS[-6:], self.raw())   # COMPACT_KEEP_LAST, the app's default
        expected = self.app.context_usage_payload(self.cid)
        self.assertEqual(expected["used_tokens"], answer["used_tokens"])
        self.assertEqual(expected["used_tokens"], self.ui.contextUsage["used"])

    def test_compact_is_refused_while_a_reply_is_streaming(self) -> None:
        self.assertTrue(self.ui.openConversation(self.cid))
        usage, feed = dict(self.ui.contextUsage), len(self.ui.feed)
        with app_module._replying(self.cid):   # a reply of this process still in flight for it
            self.ui.compactConversation(self.cid)
        answer, = self.answers["compact"]
        self.assertRefusedVisibly(answer, feed)
        self.assertIn("streaming", answer["error"])
        self.assertEqual(TEXTS, self.raw())
        self.assertEqual(usage, self.ui.contextUsage)   # a refusal never blanks the shown usage
        self.ui.compactConversation(self.cid)   # once it finished, the same click compacts
        self.assertIs(self.answers["compact"][-1]["ok"], True)

    def test_compact_refusals_and_worker_failure_are_visible(self) -> None:
        for conversation_id in ("", "c-" + "0" * 32):
            with self.subTest(conversation_id=conversation_id):
                feed = len(self.ui.feed)
                self.ui.compactConversation(conversation_id)
                self.assertRefusedVisibly(self.answers["compact"][-1], feed)
        feed = len(self.ui.feed)
        with mock.patch.object(self.ui.backend, "compact_conversation", side_effect=RuntimeError("boom")):
            self.ui.compactConversation(self.cid)
        self.assertRefusedVisibly(self.answers["compact"][-1], feed)
        self.assertEqual(TEXTS, self.raw())

    # -- Memory Doctor -----------------------------------------------------------

    def test_doctor_slot_reports_one_project_or_all(self) -> None:
        old = self.add("lint with flake8", "lint")
        new = self.add("lint with ruff", "lint", supersedes=old)
        self.ui.memoryDoctor(self.project)
        self.ui.memoryDoctor("")
        scoped, everything = self.answers["doctor"]
        self.assertEqual((scoped["ok"], scoped["project_id"]), (True, self.project))
        self.assertEqual((everything["ok"], everything["project_id"]), (True, ""))
        self.assertEqual(scoped["findings"], self.app.memory_doctor_payload(self.project)["findings"])
        self.assertEqual(everything["findings"], self.app.memory_doctor_payload()["findings"])
        conflict = next(f for f in scoped["findings"] if f["code"] == "CONFLICT_UNRESOLVED")
        self.assertEqual(conflict["repair"], {"action": "supersede", "record_ids": [new, old]})
        feed = len(self.ui.feed)
        self.ui.memoryDoctor("no-such-project")
        self.assertRefusedVisibly(self.answers["doctor"][-1], feed)

    def test_repair_slot_applies_one_finding_and_refuses_a_stale_one(self) -> None:
        old = self.add("lint with flake8", "lint")
        new = self.add("lint with ruff", "lint", supersedes=old)
        self.ui.memoryDoctor(self.project)
        conflict = next(f for f in self.answers["doctor"][-1]["findings"] if f["code"] == "CONFLICT_UNRESOLVED")
        self.ui.applyMemoryRepair(conflict["finding_id"])
        done, = self.answers["repair"]
        self.assertEqual((done["ok"], done["finding_id"], done["action"], done["record_id"], done["status"]),
                         (True, conflict["finding_id"], "supersede", old, "superseded"))
        self.assertEqual(self.memory.current_status(old), MemoryRecordStatus.SUPERSEDED)
        self.assertEqual(self.memory.current_status(new), MemoryRecordStatus.APPROVED)
        for finding_id in (conflict["finding_id"], "CONFLICT_UNRESOLVED:made-up", ""):
            with self.subTest(finding_id=finding_id):
                feed = len(self.ui.feed)
                self.ui.applyMemoryRepair(finding_id)
                self.assertRefusedVisibly(self.answers["repair"][-1], feed)
                self.assertEqual(self.answers["repair"][-1]["finding_id"], finding_id)
        self.assertEqual(self.memory.current_status(new), MemoryRecordStatus.APPROVED)


if __name__ == "__main__":
    unittest.main()
