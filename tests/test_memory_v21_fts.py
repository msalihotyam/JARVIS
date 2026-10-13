"""V2.1a M3 record search: FTS5 with quoted folded tokens, LIKE fallback.

Every database is a throwaway in TEMP; nothing opens the operator's database.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import memory
from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
)
from jarvis.memory import MemoryRecordApproval, SQLiteMemoryStore
from tests.helpers import safe_cleanup

S = MemoryRecordStatus
K = MemoryRecordKind
NEEDS_FTS = unittest.skipUnless(
    memory.MEMORY_RECORDS_FTS5_AVAILABLE, "SQLite build has no FTS5"
)


class RecordSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "memory.db"
        self.store = SQLiteMemoryStore(self.database)
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id

    def add(self, text, *, kind=K.FACT, subject="tooling", project=None,
            conversation=None) -> str:
        record = MemoryRecord(
            kind=kind, text=text, subject_key=subject,
            scope=MemoryRecordScope(project or self.alpha, conversation),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.MODEL, confidence=0.8,
            classification=DataClassification.INTERNAL,
        )
        return self.store.insert_record(record).record.record_id

    def scope(self, project=None, conversation=None) -> MemoryRecordScope:
        return MemoryRecordScope(project or self.alpha, conversation)

    def ids(self, query, **kwargs) -> set[str]:
        scope = kwargs.pop("scope", None) or self.scope()
        return {
            view.record.record_id
            for view, _ in self.store.search_records(query, scope, **kwargs)
        }

    def fallback_ids(self, query, **kwargs) -> set[str]:
        with mock.patch.object(memory, "memory_records_fts_available", return_value=False):
            return self.ids(query, **kwargs)

    def fixture(self) -> dict[str, str]:
        return {
            "uv": self.add("the build uses uv for locking"),
            "pytest": self.add("tests run with unittest not pytest", subject="testing"),
            "both": self.add("unittest runs the build checks", subject="build"),
            "beta": self.add("the build uses uv in beta too", project=self.beta),
        }

    @NEEDS_FTS
    def test_fts_and_fallback_return_same_ids(self) -> None:
        f = self.fixture()
        cases = {
            "build": {f["uv"], f["both"]},
            "unittest": {f["pytest"], f["both"]},
            "build unittest": {f["both"]},
            "testing": {f["pytest"]},
            "nothing here": set(),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.ids(query), expected)
                self.assertEqual(self.fallback_ids(query), expected)

    def test_fts_operators_in_user_text_are_literal(self) -> None:
        f = self.fixture()
        literal = self.add('use "quotes" OR near NEAR star* here')
        for search in (self.ids, self.fallback_ids):
            for query in ('build OR pytest', 'build*', 'NEAR(build pytest)',
                          '"build', 'build" OR "pytest', 'uv AND NOT build',
                          'text:build', '^build', '-build', '(', '"', '*', 'OR'):
                with self.subTest(search=search.__name__, query=query):
                    found = search(query)
                    self.assertNotIn(f["beta"], found)
                    if query == 'build OR pytest':
                        # OR is a literal word: no record has all three.
                        self.assertEqual(found, set())
            self.assertEqual(search('OR NEAR quotes star'), {literal})
            self.assertEqual(search('"'), set())

    def test_cross_project_records_never_returned(self) -> None:
        f = self.fixture()
        for search in (self.ids, self.fallback_ids):
            with self.subTest(search=search.__name__):
                self.assertEqual(search("beta"), set())
                self.assertEqual(search("uv"), {f["uv"]})
                self.assertEqual(search("beta", scope=self.scope(self.beta)), {f["beta"]})

    def test_typed_filters_and_conversation_scope(self) -> None:
        fact = self.add("deploy goes through the conveyor")
        rule = self.add("deploy needs two keys", kind=K.RULE)  # PENDING
        lesson = self.add("deploy lesson", kind=K.LESSON, subject="ops")
        mine = self.add("deploy in this chat", conversation="c-one")
        self.add("deploy in another chat", conversation="c-two")
        self.store.approve_record(rule, MemoryRecordApproval(self.alpha, rule))
        for search in (self.ids, self.fallback_ids):
            with self.subTest(search=search.__name__):
                self.assertEqual(search("deploy", kinds=[K.RULE]), {rule})
                self.assertEqual(search("deploy", kinds=[K.LESSON, K.RULE]), {rule, lesson})
                self.assertEqual(search("deploy", statuses=[S.PENDING]), set())
                self.assertEqual(search("deploy", statuses=[S.APPROVED]),
                                 {fact, rule, lesson})
                self.assertEqual(len(search("deploy", limit=1)), 1)
                self.assertEqual(search("deploy", scope=self.scope(conversation="c-one")),
                                 {fact, rule, lesson, mine})

    def test_results_carry_status_and_rank(self) -> None:
        rule = self.add("rank me please", kind=K.RULE)
        [(view, rank)] = self.store.search_records("rank", self.scope())
        self.assertEqual(view.record.record_id, rule)
        self.assertIs(view.status, S.PENDING)
        self.assertIsInstance(rank, float)

    def test_bad_arguments_refused(self) -> None:
        for kwargs in ({"limit": 0}, {"limit": 501}, {"kinds": ["fact"]},
                       {"statuses": ["approved"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.search_records("x", self.scope(), **kwargs)
        with self.assertRaises(ValueError):
            self.store.search_records(None, self.scope())
        with self.assertRaises(ValueError):
            self.store.search_records(" ".join(["w"] * 33), self.scope())
        self.assertEqual(self.store.search_records("  ?! ", self.scope()), [])

    @NEEDS_FTS
    def test_turkish_folding_via_fts(self) -> None:
        isik = self.add("Salonda ışık yanık kalsın", subject="ev")
        city = self.add("Toplantı İstanbul ofisinde", subject="iş")
        for search in (self.ids, self.fallback_ids):
            for query in ("IŞIK", "ışık", "Işık", "isik"):
                with self.subTest(search=search.__name__, query=query):
                    self.assertEqual(search(query), {isik})
            for query in ("İstanbul", "ISTANBUL", "istanbul"):
                with self.subTest(search=search.__name__, query=query):
                    self.assertEqual(search(query), {city})

    @NEEDS_FTS
    def test_index_is_folded_and_records_keep_raw_text(self) -> None:
        rid = self.add("Salonda IŞIK yanık kalsın", subject="Işık")
        connection = sqlite3.connect(self.database)
        try:
            raw = connection.execute(
                "SELECT text, subject_key FROM memory_records WHERE record_id = ?",
                (rid,)).fetchone()
            indexed = connection.execute(
                "SELECT text, subject_key FROM memory_records_fts WHERE record_id = ?",
                (rid,)).fetchone()
            counts = (
                connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM memory_records_fts").fetchone()[0],
            )
        finally:
            connection.close()
        # subject_key is the contract-normalized key (casefold + NFKD keeps dotless ı).
        self.assertEqual(raw, ("Salonda IŞIK yanık kalsın", "isık"))
        self.assertEqual(indexed, ("salonda isik yanik kalsin", "isik"))
        self.assertEqual(counts, (1, 1))

    @NEEDS_FTS
    def test_refused_insert_writes_no_index_row(self) -> None:
        with self.assertRaises(ValueError):
            self.add("never indexed", project="no-such-project")
        connection = sqlite3.connect(self.database)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM memory_records_fts").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_fold_helper(self) -> None:
        for text in ("IŞIK", "ışık", "Işık", "isik", "İŞİK"):
            with self.subTest(text=text):
                self.assertEqual(memory._fold_search_text(text), "isik")
        self.assertEqual(memory._fold_search_text("İstanbul"), "istanbul")


if __name__ == "__main__":
    unittest.main()
