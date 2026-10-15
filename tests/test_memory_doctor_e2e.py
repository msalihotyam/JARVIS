"""V2.1b gate: history import and Memory Doctor end to end.

import fixture -> preview -> confirm scope -> PENDING records -> approve some
-> doctor finds the seeded stale/conflict/orphan/FTS drift -> CLI and app
repairs, each refused until confirmed -> diagnose clean.

Fake provider only; the database is the test's own TEMP one (isolated_settings).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from jarvis import memory
from jarvis.app import EXIT_INVALID_INPUT, EXIT_OK
from jarvis.cli import main
from jarvis.contracts import (
    MODEL_ANSWER_RESULT_KEY,
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
    OutcomeStatus,
    ToolResult,
    build_model_answer,
)
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from tests.helpers import isolated_settings, open_app, safe_cleanup

S = MemoryRecordStatus


def days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class FakeImportModel:
    """Proposes a fact, a decision and a rule per batch, citing its first turn."""

    name = "fake-import"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, task, session):
        self.prompts.append(task.objective)
        turn = re.findall(r'"id": "(i\d+-[0-9a-f]{16})"', task.objective)[0]
        candidates = [
            {"kind": kind, "text": f"Imported {kind} {len(self.prompts)}.",
             "subject_key": f"imported-{kind}", "confidence": 0.9, "evidence_turn_ids": [turn]}
            for kind in ("fact", "decision", "rule")
        ]
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS, summary="ok",
            data={MODEL_ANSWER_RESULT_KEY: build_model_answer(json.dumps({"candidates": candidates}))},
        )


class MemoryDoctorImportE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.app = open_app(destination_policy=(), settings=self.settings)
        self.model = FakeImportModel()
        self.app.intent_model = self.model
        self.store = self.app.memory
        self.alpha = self.store.create_project("Alpha").id
        self.beta = self.store.create_project("Beta").id
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.export = Path(temp.name) / "conversations.json"
        self.export.write_text(json.dumps([
            {"name": "Alpha planning", "chat_messages": [{"sender": "human", "text": "We ship on Fridays."}]},
            {"name": "Beta notes", "chat_messages": [{"sender": "human", "text": "Beta private roadmap."}]},
        ]), encoding="utf-8")

    def counts(self) -> dict[str, int]:
        with contextlib.closing(sqlite3.connect(self.settings.memory_database)) as db:
            return {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # nosec B608
                for table in ("memory_records", "memory_record_events", "memory_capture_runs")
            }

    def seed(self, text: str, subject: str, **extra) -> str:
        """A clean, approved capture FACT in Alpha (Core's user_stated guard stands in)."""
        record = MemoryRecord(
            kind=MemoryRecordKind.FACT, text=text, subject_key=subject,
            scope=MemoryRecordScope(self.alpha, extra.pop("conversation", None)),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END, actor=MemoryRecordActor.MODEL,
            confidence=0.8, classification=DataClassification.INTERNAL, **extra,
        )
        guard = RecordGuard(Provenance.USER_STATED, SessionTaint.USER_TYPED, (), MemoryRecordKind.FACT,
                            content_hash(text))
        record_id = self.store.insert_record(record, guard).record.record_id
        self.assertIs(self.store.current_status(record_id), S.APPROVED)
        return record_id

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["memory", "doctor", *argv], settings=self.settings)
        return code, out.getvalue()

    def cli_findings(self) -> dict[str, str]:
        code, out = self.cli("--json")
        self.assertEqual(code, EXIT_OK, out)
        return {f["finding_id"]: f["code"] for f in json.loads(out)["findings"]}

    def app_findings(self) -> dict[str, dict]:
        payload = self.app.memory_doctor_payload()
        self.assertTrue(payload["ok"], payload)
        return {f["finding_id"]: f for f in payload["findings"]}

    @unittest.skipUnless(memory.MEMORY_RECORDS_FTS5_AVAILABLE, "SQLite build has no FTS5")
    def test_import_then_doctor_repairs_to_clean(self) -> None:
        # 1. Preview reads the fixture and writes nothing; no model call.
        before = self.counts()
        plan = self.app.import_history_preview(self.export)
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.model.prompts, [])
        self.assertEqual(
            [(g["group_id"], g["proposed_project_id"], g["scope_status"]) for g in plan["groups"]],
            [("0", self.alpha, "unconfirmed"), ("1", self.beta, "unconfirmed")],
        )

        # 2. Only the confirmed group is extracted; everything lands PENDING (I11).
        result = self.app.import_history_run(plan["plan_id"], {"0": self.alpha})
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["pending"], result["approved"], result["skipped_groups"]), (3, 0, ["1"]))
        self.assertEqual(len(self.model.prompts), 1)
        self.assertNotIn("Beta private roadmap", self.model.prompts[0])
        pending = self.app.memory_records_payload(self.alpha, {"statuses": ["pending"]})["records"]
        imported = sorted(r["record_id"] for r in pending)
        self.assertEqual(len(imported), 3, pending)
        self.assertEqual(self.app.memory_records_payload(self.beta, {"statuses": ["pending"]})["records"], [])
        # Import alone never surfaces a doctor finding.
        self.assertEqual(self.app_findings(), {})

        # 3. The user approves one, rejects one and leaves one pending.
        approved, rejected, left = imported
        self.assertFalse(self.app.approve_memory_record(self.beta, approved)["ok"])  # wrong project
        self.assertIs(self.store.current_status(approved), S.PENDING)
        self.assertTrue(self.app.approve_memory_record(self.alpha, approved)["ok"])
        self.assertTrue(self.app.reject_memory_record(self.alpha, rejected)["ok"])
        self.assertEqual(
            [self.store.current_status(rid) for rid in imported], [S.APPROVED, S.REJECTED, S.PENDING]
        )
        self.assertEqual(self.app_findings(), {})

        # 4. Seed the four faults the doctor must find.
        stale = self.seed("deploy window is tuesday", "deploy", expires_at=days_ago(1),
                          created_at=days_ago(30))
        orphan = self.seed("this chat prefers short answers", "style", conversation=str(uuid4()))
        old = self.seed("lint with flake8", "lint", created_at=days_ago(2))
        new = self.seed("lint with ruff", "lint", created_at=days_ago(1), supersedes=old)
        with contextlib.closing(sqlite3.connect(self.settings.memory_database)) as db, db:
            db.execute("DELETE FROM memory_records_fts WHERE record_id = ?", (approved,))  # index only
        ids = {
            "stale": f"STALE:{stale}",
            "orphan": f"ORPHAN_SCOPE:{orphan}",
            "conflict": f"CONFLICT_UNRESOLVED:{','.join(sorted((old, new)))}",
            "drift": "FTS_DRIFT:",
        }
        expected = {ids["stale"]: "STALE", ids["orphan"]: "ORPHAN_SCOPE",
                    ids["conflict"]: "CONFLICT_UNRESOLVED", ids["drift"]: "FTS_DRIFT"}
        self.assertEqual(self.cli_findings(), expected)
        found = self.app_findings()
        self.assertEqual({fid: f["code"] for fid, f in found.items()}, expected)
        self.assertEqual(found[ids["stale"]]["repair"], {"action": "expire", "record_ids": [stale]})
        # Findings carry ids, never record text.
        code, text = self.cli()
        self.assertEqual(code, EXIT_OK, text)
        self.assertIn(ids["orphan"], text)
        self.assertNotIn("short answers", text)
        self.assertNotIn("short answers", json.dumps(found))

        # 5. Every repair is refused until the finding id is repeated.
        for name, finding in ids.items():
            other = ids["stale" if name == "orphan" else "orphan"]  # another valid finding id
            for confirm in ((), ("--confirm", other)):
                code, out = self.cli("--repair", finding, *confirm)
                self.assertEqual(code, EXIT_INVALID_INPUT, out)
            self.assertFalse(self.app.apply_memory_repair(finding)["ok"])
            self.assertFalse(self.app.apply_memory_repair(finding, confirm=other)["ok"])
            self.assertFalse(self.app.apply_memory_repair(finding, confirm=finding + "x")["ok"])
        self.assertEqual(self.cli_findings(), expected)
        self.assertEqual([self.store.current_status(rid) for rid in (stale, orphan, old, new)], [S.APPROVED] * 4)

        # 6a. CLI repairs: orphan retracted, FTS rebuilt.
        code, out = self.cli("--repair", ids["orphan"], "--confirm", ids["orphan"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(json.loads(out)["status"], "retracted")
        code, out = self.cli("--repair", ids["drift"], "--confirm", ids["drift"])
        self.assertEqual(code, EXIT_OK, out)
        # 6b. App repairs: conflict superseded (newest kept), stale expired.
        done = self.app.apply_memory_repair(ids["conflict"], confirm=ids["conflict"])
        self.assertEqual((done["ok"], done["record_id"], done["status"]), (True, old, "superseded"), done)
        done = self.app.apply_memory_repair(ids["stale"], confirm=ids["stale"])
        self.assertEqual((done["ok"], done["record_id"], done["status"]), (True, stale, "expired"), done)
        # A repeated, now-stale confirmation is refused.
        self.assertFalse(self.app.apply_memory_repair(ids["stale"], confirm=ids["stale"])["ok"])

        # 7. Diagnose clean on both surfaces; the user's import decisions are untouched.
        self.assertEqual(self.cli_findings(), {})
        self.assertEqual(self.app_findings(), {})
        self.assertEqual(
            [self.store.current_status(rid) for rid in (*imported, stale, orphan, old, new)],
            [S.APPROVED, S.REJECTED, S.PENDING, S.EXPIRED, S.RETRACTED, S.SUPERSEDED, S.APPROVED],
        )


if __name__ == "__main__":
    unittest.main()
