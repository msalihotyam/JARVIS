"""V2.1 SEC2: lifetime, memory-off, reply sources and conversation rollback in the store (SEC2STORE).

Every database is a throwaway in TEMP; nothing opens the operator's database.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
)
from jarvis.memory import (
    SCHEMA_VERSION,
    MemoryRecordApproval,
    MemoryRecordRefused,
    SQLiteMemoryStore,
)
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from jarvis.memory_lifetime import SESSION_TTL, Lifetime
from tests.helpers import safe_cleanup

S = MemoryRecordStatus
K = MemoryRecordKind
USER = MemoryRecordActor.USER
MODEL = MemoryRecordActor.MODEL
TRANSIENT = "use the staging database for now"
PLAIN = "the zorbaquilt pipeline deploys nightly"


class Sec2StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "memory.db"
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id
        self.c1 = self.store.create_conversation(self.alpha).id
        self.c2 = self.store.create_conversation(self.alpha).id

    def insert(self, text=PLAIN, *, kind=K.FACT, project=None, source=None, scoped=None,
               lifetime=None, subject="deploy") -> str:
        record = MemoryRecord(
            kind=kind, text=text, subject_key=subject,
            scope=MemoryRecordScope(project or self.alpha, scoped),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END, actor=MODEL, confidence=0.8,
            classification=DataClassification.INTERNAL, source_conversation_id=source,
        )
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (), kind, content_hash(text))
        return self.store.insert_record(record, guard, lifetime=lifetime).record.record_id

    def rows(self, sql: str, args=()) -> list[tuple]:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def listed(self, for_conversation_id=None) -> set[str]:
        return {v.record.record_id for v in self.store.list_records(
            MemoryRecordScope(self.alpha), for_conversation_id=for_conversation_id)}

    def searched(self, word, for_conversation_id=None) -> set[str]:
        return {v.record.record_id for v, _ in self.store.search_records(
            word, MemoryRecordScope(self.alpha), for_conversation_id=for_conversation_id)}

    # I19 lifetime -------------------------------------------------------

    def test_transient_text_requested_permanent_is_stored_session_with_expiry(self) -> None:
        record_id = self.insert(TRANSIENT, source=self.c1, lifetime=Lifetime.PERMANENT)
        self.assertIs(self.store.record_lifetime(record_id), Lifetime.SESSION)
        (view,) = [v for v in self.store.list_records(MemoryRecordScope(self.alpha), for_conversation_id=self.c1)]
        created = datetime.fromisoformat(view.record.created_at)
        self.assertEqual(view.record.expires_at, (created + SESSION_TTL).isoformat())

    def test_lifetime_is_written_for_every_insert_and_request_only_lowers(self) -> None:
        plain = self.insert(PLAIN)
        transient = self.insert(TRANSIENT, subject="db", source=self.c1)
        lowered = self.insert("the build runs on windows", subject="ci", lifetime="phase")
        explicit = self.store.insert_explicit_user_record(
            self.alpha, "fact", "internal", "deploy to staging today", "staging").record.record_id
        self.assertIs(self.store.record_lifetime(plain), Lifetime.PERMANENT)
        self.assertIs(self.store.record_lifetime(transient), Lifetime.SESSION)
        self.assertIs(self.store.record_lifetime(lowered), Lifetime.PHASE)
        self.assertIs(self.store.record_lifetime(explicit), Lifetime.SESSION)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_record_lifetime"), [(4,)])
        self.assertEqual(self.rows("SELECT expires_at FROM memory_records WHERE record_id = ?", (plain,)), [(None,)])
        self.assertIs(self.store.record_lifetime("no-such-record"), Lifetime.PERMANENT)  # legacy: no row
        with self.assertRaises(ValueError):
            self.insert("another fact", subject="x", lifetime="forever")
        self.assertEqual(SCHEMA_VERSION, 10)

    def test_session_record_only_reaches_its_source_conversation(self) -> None:
        session = self.insert("zorbaquilt runs on port 9 for now", source=self.c1)
        scoped = self.insert("zorbaquilt cache is warm today", subject="cache", scoped=self.c1)
        permanent = self.insert(PLAIN, subject="pipeline", source=self.c1)
        self.assertEqual(self.listed(None), {permanent})
        self.assertEqual(self.listed(self.c2), {permanent})
        self.assertEqual(self.listed(self.c1), {permanent, session})
        self.assertEqual(self.searched("zorbaquilt"), {permanent})
        self.assertEqual(self.searched("zorbaquilt", self.c2), {permanent})
        self.assertEqual(self.searched("zorbaquilt", self.c1), {permanent, session})
        own = {v.record.record_id for v in self.store.list_records(
            MemoryRecordScope(self.alpha, self.c1), for_conversation_id=self.c1)}
        self.assertEqual(own, {permanent, session, scoped})
        with self.assertRaises(ValueError):  # a chat cannot ask for another chat's SESSION records
            self.store.list_records(MemoryRecordScope(self.alpha, self.c1), for_conversation_id=self.c2)
        with self.assertRaises(ValueError):
            self.store.search_records("zorbaquilt", MemoryRecordScope(self.alpha, self.c2),
                                      for_conversation_id=self.c1)

    # I21 memory-off ----------------------------------------------------

    def test_memory_off_refuses_every_insert_path(self) -> None:
        self.store.set_conversation_memory_off(self.c1, True, USER)
        self.assertTrue(self.store.is_memory_off(self.c1))
        self.assertFalse(self.store.is_memory_off(self.c2))
        before = self.rows("SELECT COUNT(*) FROM memory_records")
        attempts = (
            lambda: self.insert(source=self.c1),
            lambda: self.insert(scoped=self.c1),
            lambda: self.store.insert_explicit_user_record(
                self.alpha, "fact", "internal", PLAIN, "deploy", conversation_id=self.c1),
            lambda: self.store.insert_explicit_user_record(
                self.alpha, "fact", "internal", PLAIN, "deploy", source_conversation_id=self.c1),
        )
        for attempt in attempts:
            with self.assertRaises(MemoryRecordRefused) as caught:
                attempt()
            self.assertEqual(caught.exception.category, "memory_off")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_records"), before)
        self.insert(source=self.c2)  # another conversation still writes
        self.store.set_conversation_memory_off(self.c1, False, USER)
        self.assertFalse(self.store.is_memory_off(self.c1))
        self.assertTrue(self.store.was_ever_memory_off(self.c1))
        self.assertFalse(self.store.was_ever_memory_off(self.c2))
        self.insert(PLAIN, subject="again", source=self.c1)

    def test_memory_off_reads_nothing_and_toggle_is_user_only(self) -> None:
        self.insert(PLAIN, source=self.c2)
        self.store.set_conversation_memory_off(self.c1, True, USER)
        self.assertEqual(self.listed(self.c1), set())
        self.assertEqual(self.searched("zorbaquilt", self.c1), set())
        self.assertEqual(self.store.list_records(MemoryRecordScope(self.alpha, self.c1)), [])
        for actor in (MODEL, MemoryRecordActor.TOOL):
            with self.assertRaises(ValueError):
                self.store.set_conversation_memory_off(self.c1, False, actor)
        with self.assertRaises(ValueError):
            self.store.set_conversation_memory_off("c-" + "0" * 32, True, USER)
        self.assertTrue(self.store.is_memory_off(self.c1))

    # I22 reply sources -------------------------------------------------

    def test_reply_sources_and_revision(self) -> None:
        fact = self.insert(PLAIN)
        decision = self.insert("we ship on fridays", kind=K.DECISION, subject="ship")
        self.assertEqual(self.store.record_revision(fact), 1)
        self.assertEqual(self.store.record_revision(decision), 1)
        self.store.approve_record(decision, MemoryRecordApproval(self.alpha, decision))
        self.store.undo(fact, USER)
        self.assertEqual(self.store.record_revision(fact), 2)
        self.assertEqual(self.store.record_revision(decision), 2)
        self.assertEqual(self.store.record_revision("no-such-record"), 0)

        self.assertIsNone(self.store.sources_for_turn(self.c1, "t1"))
        self.store.record_reply_sources(self.c1, "t1", [(fact, K.FACT, 2), (decision, "decision", 2)])
        self.assertEqual(self.store.sources_for_turn(self.c1, "t1"), [(fact, K.FACT, 2), (decision, K.DECISION, 2)])
        self.store.record_reply_sources(self.c1, "t2", [])
        self.assertEqual(self.store.sources_for_turn(self.c1, "t2"), [])
        self.assertEqual(self.rows(
            "SELECT outcome, record_id, kind, revision FROM memory_reply_sources WHERE turn_id = 't2'"),
            [("none", None, None, None)])
        with self.assertRaises(ValueError):
            self.store.record_reply_sources(self.c1, "t1", [])  # once per turn
        for bad in ([(fact, "opinion", 1)], [(fact, K.FACT, -1)], [(fact, K.FACT, 1), (fact, K.FACT, 1)]):
            with self.assertRaises(ValueError):
                self.store.record_reply_sources(self.c1, "t3", bad)
        self.assertIsNone(self.store.sources_for_turn(self.c1, "t3"))

    # I23 per-conversation rollback ---------------------------------------

    def test_conversation_rollback_uses_events_and_tombstones_only(self) -> None:
        approved = self.insert(PLAIN, source=self.c1)
        pending = self.insert("we ship on fridays", kind=K.DECISION, subject="ship", scoped=self.c1)
        retracted = self.insert("the cache is redis", subject="cache", source=self.c1)
        self.store.undo(retracted, USER)
        other_conversation = self.insert("the queue is rabbit", subject="queue", source=self.c2)
        other_project = self.insert("beta uses go", subject="lang", project=self.beta, source=self.c1)
        self.assertEqual(
            {v.record.record_id: v.status for v in self.store.records_from_conversation(self.c1, self.alpha)},
            {approved: S.APPROVED, pending: S.PENDING, retracted: S.RETRACTED})
        records = self.rows("SELECT COUNT(*) FROM memory_records")
        tombstones = self.rows("SELECT COUNT(*) FROM memory_tombstones")[0][0]

        for actor in (MODEL, MemoryRecordActor.TOOL):
            with self.assertRaises(ValueError):
                self.store.retract_conversation_records(self.c1, self.alpha, actor)
        with self.assertRaises(ValueError):
            self.store.retract_conversation_records(self.c1, "no-such-project")
        self.assertEqual(self.store.retract_conversation_records(self.c1, self.alpha), (1, 1, 1))

        status = {v.record.record_id: v.status for v in self.store.records_from_conversation(self.c1, self.alpha)}
        self.assertEqual(status, {approved: S.RETRACTED, pending: S.REJECTED, retracted: S.RETRACTED})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_records"), records)
        self.assertEqual(
            sorted(self.rows("SELECT reason FROM memory_tombstones ORDER BY rowid")[tombstones:]),
            [("rejected",), ("retracted",)])
        days = {reason: (datetime.fromisoformat(exp) - datetime.fromisoformat(at)).days
                for reason, at, exp in self.rows("SELECT reason, created_at, expires_at FROM memory_tombstones")}
        self.assertEqual(days, {"retracted": 365, "rejected": 180})
        self.assertEqual(self.store.records_from_conversation(self.c2, self.alpha)[0].status, S.APPROVED)
        self.assertEqual(self.store.records_from_conversation(self.c1, self.beta)[0].record.record_id, other_project)
        self.assertEqual(self.store.records_from_conversation(self.c1, self.beta)[0].status, S.APPROVED)
        self.assertEqual(self.store.records_from_conversation(self.c2, self.alpha)[0].record.record_id,
                         other_conversation)
        events = self.rows("SELECT COUNT(*) FROM memory_record_events")
        self.assertEqual(self.store.retract_conversation_records(self.c1, self.alpha), (0, 0, 3))  # idempotent
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_record_events"), events)


if __name__ == "__main__":
    unittest.main()
