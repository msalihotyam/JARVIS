"""V2.1b Memory Doctor: app payload, CLI 'memory doctor' and confirmed repairs.

Every database is an isolated TEMP one (isolated_settings); nothing opens
the operator's database and no model or connector is built.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from jarvis import memory
from jarvis.app import EXIT_INVALID_INPUT, EXIT_OK
from jarvis.cli import main
from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from tests.helpers import isolated_settings, open_app

S = MemoryRecordStatus


def days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class MemoryDoctorCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.store = SQLiteMemoryStore(self.settings.memory_database)
        self.alpha = self.store.create_project("Alpha").id
        self.orphan = self.add("this chat prefers short answers", "style", conversation=str(uuid4()))
        self.old = self.add("lint with flake8", "lint", created_at=days_ago(2))
        self.new = self.add("lint with ruff", "lint", created_at=days_ago(1), supersedes=self.old)

    def add(self, text, subject, *, conversation=None, **extra) -> str:
        record = MemoryRecord(
            kind=MemoryRecordKind.FACT, text=text, subject_key=subject,
            scope=MemoryRecordScope(self.alpha, conversation),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.MODEL, confidence=0.8,
            classification=DataClassification.INTERNAL, **extra,
        )
        # A guard-less capture FACT/LESSON is now PENDING (SECCAPTURE): stand
        # in for Core's clean user_stated guard.
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (), MemoryRecordKind.FACT,
                            content_hash(text)) if MemoryRecordKind.FACT in (MemoryRecordKind.FACT, MemoryRecordKind.LESSON) else None
        return self.store.insert_record(record, guard).record.record_id

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["memory", "doctor", *argv], settings=self.settings)
        return code, out.getvalue()

    def report(self, *argv: str) -> dict[str, str]:
        code, out = self.cli("--json", *argv)
        self.assertEqual(code, EXIT_OK, out)
        return {f["finding_id"]: f["code"] for f in json.loads(out)["findings"]}

    def test_findings_listed_without_record_text(self) -> None:
        orphan_id, conflict_id = f"ORPHAN_SCOPE:{self.orphan}", f"CONFLICT_UNRESOLVED:{','.join(sorted((self.old, self.new)))}"
        self.assertEqual(self.report(), {orphan_id: "ORPHAN_SCOPE", conflict_id: "CONFLICT_UNRESOLVED"})
        self.assertEqual(self.report("--project", self.alpha), self.report())
        code, text = self.cli()
        self.assertEqual(code, EXIT_OK)
        self.assertIn(f"{orphan_id}  memory_doctor.orphan_scope  (repair: retract)", text)
        self.assertNotIn("short answers", text)
        _, raw = self.cli("--json")
        self.assertNotIn("short answers", raw)
        code, out = self.cli("--project", "no-such-project", "--json")
        self.assertEqual(code, EXIT_INVALID_INPUT, out)

    def test_repair_without_confirmation_is_refused(self) -> None:
        finding = f"ORPHAN_SCOPE:{self.orphan}"
        for extra in ((), ("--confirm", "ORPHAN_SCOPE:other")):
            code, out = self.cli("--repair", finding, *extra)
            self.assertEqual(code, EXIT_INVALID_INPUT, out)
            self.assertEqual(self.store.current_status(self.orphan), S.APPROVED)
        code, out = self.cli("--repair", finding, "--confirm", finding)
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(json.loads(out)["status"], "retracted")
        self.assertEqual(self.store.current_status(self.orphan), S.RETRACTED)
        self.assertNotIn(finding, self.report())
        # The finding is gone, so repeating the confirmed command is refused.
        code, out = self.cli("--repair", finding, "--confirm", finding)
        self.assertEqual(code, EXIT_INVALID_INPUT, out)
        self.assertEqual(self.store.current_status(self.orphan), S.RETRACTED)

    def test_project_scope_and_flag_misuse_are_refused(self) -> None:
        beta = self.store.create_project("Beta").id
        finding = f"ORPHAN_SCOPE:{self.orphan}"
        for argv in (
            ("--project", beta, "--repair", finding, "--confirm", finding),
            ("--confirm", finding),
            ("--rebuild-fts", "--project", self.alpha),
            ("--rebuild-fts", "--repair", finding, "--confirm", finding),
        ):
            code, out = self.cli(*argv)
            self.assertEqual(code, EXIT_INVALID_INPUT, (argv, out))
        self.assertEqual(self.store.current_status(self.orphan), S.APPROVED)

    def test_linked_three_record_chain_resolves_oldest_first(self) -> None:
        newest = self.add("lint with ruff and mypy", "lint", supersedes=self.new)
        for remaining in (1, 0):
            report = self.report()
            conflict = next(fid for fid, code in report.items() if code == "CONFLICT_UNRESOLVED")
            code, out = self.cli("--repair", conflict, "--confirm", conflict)
            self.assertEqual(code, EXIT_OK, out)
            self.assertEqual(json.loads(out)["remaining"], remaining)
        self.assertEqual(
            [self.store.current_status(rid) for rid in (self.old, self.new, newest)],
            [S.SUPERSEDED, S.SUPERSEDED, S.APPROVED],
        )
        self.assertNotIn("CONFLICT_UNRESOLVED", self.report().values())

    @unittest.skipUnless(memory.MEMORY_RECORDS_FTS5_AVAILABLE, "SQLite build has no FTS5")
    def test_rebuild_fixes_drift(self) -> None:
        with contextlib.closing(sqlite3.connect(self.settings.memory_database)) as connection, connection:
            connection.execute("DELETE FROM memory_records_fts WHERE record_id = ?", (self.new,))
        self.assertIn("FTS_DRIFT:", self.report())
        code, out = self.cli("--rebuild-fts")
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(json.loads(out)["indexed"], 3)
        self.assertNotIn("FTS_DRIFT:", self.report())
        with contextlib.closing(sqlite3.connect(self.settings.memory_database)) as connection, connection:
            connection.execute("DELETE FROM memory_records_fts WHERE record_id = ?", (self.old,))
        code, out = self.cli("--repair", "FTS_DRIFT:")
        self.assertEqual(code, EXIT_INVALID_INPUT, out)
        code, out = self.cli("--repair", "FTS_DRIFT:", "--confirm", "FTS_DRIFT:")
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("FTS_DRIFT:", self.report())

    def test_app_boundary_supersedes_one_conflict_record(self) -> None:
        app = open_app(destination_policy=(), settings=self.settings)
        payload = app.memory_doctor_payload(self.alpha)
        self.assertTrue(payload["ok"])
        conflict = next(f for f in payload["findings"] if f["code"] == "CONFLICT_UNRESOLVED")
        self.assertEqual(conflict["repair"], {"action": "supersede", "record_ids": [self.new, self.old]})
        refused = app.apply_memory_repair(conflict["finding_id"])
        self.assertFalse(refused["ok"])
        self.assertEqual(self.store.current_status(self.old), S.APPROVED)
        done = app.apply_memory_repair(conflict["finding_id"], confirm=conflict["finding_id"])
        self.assertEqual(done, {
            "ok": True, "finding_id": conflict["finding_id"], "action": "supersede",
            "record_id": self.old, "status": "superseded", "remaining": 0,
        })
        self.assertEqual(self.store.current_status(self.new), S.APPROVED)
        self.assertNotIn(conflict["finding_id"], {f["finding_id"] for f in app.memory_doctor_payload()["findings"]})
        self.assertFalse(app.memory_doctor_payload(123)["ok"])
        # A conflict whose newer record does not name the older one: the store
        # refuses the supersession and nothing changes (fail-closed).
        first, second = self.add("ci on windows", "ci"), self.add("ci on linux", "ci")
        unlinked = f"CONFLICT_UNRESOLVED:{','.join(sorted((first, second)))}"
        self.assertFalse(app.apply_memory_repair(unlinked, confirm=unlinked)["ok"])
        self.assertEqual({self.store.current_status(first), self.store.current_status(second)}, {S.APPROVED})


if __name__ == "__main__":
    unittest.main()
