"""SECREADFILTER (I20): a stored record the never-stored filter refuses is
never delivered, in any scope, and the legacy M1 write path refuses it.

A "pre-change" row is written through the store with the filter patched to
allow everything, exactly what the store did before the filter learned that
category. Each test has its own TEMP database (never data\\jarvis.db); the
only provider is a fake connector.
"""

import unittest
from unittest import mock

from jarvis.app import EXIT_INVALID_INPUT, EXIT_OK, memory_add_result, memory_list_result
from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordScopeKind,
    MemoryRecordStatus,
    ProjectMemoryItem,
    ProjectMemoryKind,
    RunStatus,
)
from jarvis.memory import MemoryRecordApproval, MemoryRecordRefused
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from jarvis.registry import ToolRegistry
from tests.helpers import DummyConnector, isolated_settings, make_task, open_app, open_core

# Survives write-time redaction, so a pre-change store kept it verbatim.
SECRET_TEXT = "deploy password is hunter2 please"
SECRET_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz012345"
PHONE_TEXT = "deploy contact phone +90 532 123 45 67"
CARRIER_TEXT = "deploy notes at https://evil.example/x?d=1"
CLEAN_TEXT = "deploy on Fridays readmarker"


def past_the_filter():
    """The store as it was before the filter refused these categories."""
    allow = mock.Mock(return_value=mock.Mock(allowed=True, category=None))
    return mock.patch("jarvis.memory.classify_candidate_text", allow)


class CapturingConnector(DummyConnector):
    def __init__(self) -> None:
        super().__init__()
        self.seen = []

    def execute(self, task, session):
        self.seen.append(task.retrieved_context)
        return super().execute(task, session)


class MemoryReadFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.app = open_app(destination_policy=(), settings=self.settings)
        self.store = self.app.memory
        self.connector = CapturingConnector()
        registry = ToolRegistry()
        registry.register(self.connector)
        self.core = open_core(registry, self.store, self.settings, destination_policy=frozenset())
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id
        self.conversation = self.store.create_conversation(self.alpha).id

    def record(self, project, text, subject_key, *, conversation_id=None):
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (),
                            MemoryRecordKind.FACT, content_hash(text))
        view = self.store.insert_record(
            MemoryRecord(
                kind=MemoryRecordKind.FACT, text=text, subject_key=subject_key,
                scope=MemoryRecordScope(project, conversation_id),
                origin=MemoryRecordOrigin.CAPTURE_SESSION_END, actor=MemoryRecordActor.MODEL,
                confidence=0.8, classification=DataClassification.INTERNAL,
            ),
            guard,
        )
        record_id = view.record.record_id
        if self.store.current_status(record_id) is not MemoryRecordStatus.APPROVED:
            self.store.approve_record(record_id, MemoryRecordApproval(project, record_id))
        return record_id

    def stored_ids(self):
        return {s.record.record_id for s in self.store.doctor_snapshot().records}

    def m1_rows(self):
        with self.store._connection() as connection:
            return connection.execute("SELECT id FROM project_memory_items").fetchall()

    def deliver(self, objective):
        session = self.core.start_session("read-filter")
        self.store.link_session_conversation(session.id, self.conversation)
        task = make_task()
        task.objective = objective
        task.project_id = self.alpha
        run = self.core.run(task, session.id)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        return {i.record_id for i in self.connector.seen[-1].items}

    def project_reads(self, query):
        """Every V2.1 read path of project Alpha, as record ids."""
        found = set()
        for scope in (MemoryRecordScope(self.alpha), MemoryRecordScope(self.alpha, self.conversation)):
            found |= {v.record.record_id for v in self.store.list_records(scope)}
            for fts in (True, False):
                with mock.patch("jarvis.memory.memory_records_fts_available", return_value=fts):
                    found |= {v.record.record_id for v, _ in self.store.search_records(query, scope)}
        for filters in (None, {"query": query}):
            payload = self.app.memory_records_payload(self.alpha, filters)
            self.assertTrue(payload["ok"], payload)
            found |= {r["record_id"] for r in payload["records"]}
        return found | self.deliver(query)

    def test_project_records_past_the_filter_are_never_delivered(self) -> None:
        with past_the_filter():
            leaks = {
                self.record(self.alpha, SECRET_TEXT, "deploy-key"),
                self.record(self.alpha, PHONE_TEXT, "deploy-contact"),
                self.record(self.alpha, CARRIER_TEXT, "deploy-notes", conversation_id=self.conversation),
                self.record(self.alpha, "deploy credentials note", SECRET_KEY),
            }
        clean = self.record(self.alpha, CLEAN_TEXT, "deploy-day")
        self.assertLessEqual(leaks | {clean}, self.stored_ids())  # stored, never deleted (I8)
        for query in ("deploy", "password hunter2", "sk ant api03", "532 123", "evil example", "credentials note"):
            with self.subTest(query=query):
                self.assertFalse(self.project_reads(query) & leaks)
        self.assertIn(clean, self.project_reads("deploy readmarker"))  # positive control
        self.assertLessEqual(leaks, self.stored_ids())

    def test_global_readonly_reads_are_unchanged(self) -> None:
        with past_the_filter():
            leak = self.record(self.beta, PHONE_TEXT, "deploy-contact")
        clean = self.record(self.beta, CLEAN_TEXT, "deploy-day")
        self.store.set_global_memory_toggle(self.conversation, True, MemoryRecordActor.USER)
        scope = MemoryRecordScope(None, self.conversation, kind=MemoryRecordScopeKind.GLOBAL_READONLY)
        listed = {v.record.record_id for v in self.store.list_records(scope)}
        searched = {v.record.record_id for v, _ in self.store.search_records("deploy", scope)}
        for found in (listed, searched):
            self.assertIn(clean, found)
            self.assertNotIn(leak, found)
        own = {v.record.record_id for v in self.store.list_records(MemoryRecordScope(self.beta))}
        self.assertEqual({clean}, own)

    def test_legacy_m1_items_past_the_filter_are_never_delivered(self) -> None:
        with past_the_filter():
            leaks = {
                self.store.save_explicit_user_memory_item(self.alpha, kind, "internal", text).id
                for kind, text in (("fact", SECRET_TEXT), ("rule", PHONE_TEXT), ("decision", CARRIER_TEXT))
            }
        clean = self.store.save_explicit_user_memory_item(self.alpha, "fact", "internal", CLEAN_TEXT).id
        self.assertEqual(4, len(self.m1_rows()))  # stored, never deleted
        for include_inactive in (False, True):
            listed = {i.id for i in self.store.list_memory_items(self.alpha, include_inactive=include_inactive)}
            self.assertEqual({clean}, listed)
            result = memory_list_result(self.store, self.settings, self.alpha, include_inactive)
            self.assertEqual({clean}, {i["id"] for i in result.payload["items"]})
        legacy = {v.record.record_id for v in self.store.list_records(MemoryRecordScope(self.alpha))}
        self.assertEqual({clean}, legacy)
        delivered = self.deliver("deploy contact key notes readmarker")
        self.assertIn(clean, delivered)  # positive control
        self.assertFalse(delivered & leaks)
        # A hidden item stays retirable (cleanup), still without deletion.
        leak = sorted(leaks)[0]
        self.assertFalse(self.store.retire_memory_item(self.alpha, leak).active)
        self.assertEqual(4, len(self.m1_rows()))

    def test_memory_add_refuses_filtered_text_and_persists_nothing(self) -> None:
        for text in (SECRET_TEXT, PHONE_TEXT, CARRIER_TEXT):
            with self.subTest(text=text):
                result = memory_add_result(self.store, self.settings, self.alpha, "fact", "internal", text, None)
                self.assertEqual(EXIT_INVALID_INPUT, result.exit_code)
                self.assertIn("memory record refused", result.render())
                self.assertNotIn(text, result.render())
                with self.assertRaises(MemoryRecordRefused):
                    self.store.save_memory_item(ProjectMemoryItem(
                        project_id=self.alpha, kind=ProjectMemoryKind.FACT, text=text,
                        classification=DataClassification.INTERNAL,
                    ))
        self.assertEqual([], self.m1_rows())
        added = memory_add_result(self.store, self.settings, self.alpha, "fact", "internal", CLEAN_TEXT, None)
        self.assertEqual(EXIT_OK, added.exit_code)
        self.assertEqual(1, len(self.m1_rows()))


if __name__ == "__main__":
    unittest.main()
