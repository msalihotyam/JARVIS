"""CANARY1 - a classified record's marker never reaches a non-model sink.

A unique canary is seeded into a classified project record through the
store's explicit-user boundary (temp SQLite, never data/jarvis.db). The
flows that write outside the process other than the model call are then
driven with fakes: the notification ledger payload (NotificationCore), the
tray/in-app sink (NotifySink), the phone path (n6_notify submit / flush /
evening_summary), every log record, and every file left in the temp dir
except the store itself. The canary must appear in none of them.

No network, no live connector, no wall clock (fixed datetimes, fake clocks).
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jarvis import n6_notify
from jarvis.memory import SQLiteMemoryStore
from jarvis.notify import NotificationCore
from jarvis.notify_sink import NotifySink
from tests.helpers import safe_cleanup

# ASCII, no dot/slash (n6_notify strips links), not credential-shaped.
CANARY = "KANARYA-DORA-7F3A9C"
AT = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
STEP = "s-canary-1"


def assert_no_canary(sinks: dict[str, str]) -> None:
    """Raise AssertionError naming every sink whose output holds the canary."""
    leaked = sorted(name for name, text in sinks.items() if CANARY in text)
    if leaked:
        raise AssertionError(f"canary leaked into: {', '.join(leaked)}")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class PrivacyCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, temp)
        self.root = Path(temp.name)
        self.db = self.root / "canary.db"
        self.logs = _Capture()
        root_logger = logging.getLogger()
        level = root_logger.level
        root_logger.addHandler(self.logs)
        root_logger.setLevel(logging.DEBUG)
        self.addCleanup(root_logger.setLevel, level)
        self.addCleanup(root_logger.removeHandler, self.logs)
        # Seed: the classified record carrying the canary.
        self.store = SQLiteMemoryStore(self.db)
        project = self.store.create_project("Dora gizli proje")
        self.project_id = project.id
        self.store.save_explicit_user_project_instruction(
            project.id, f"Dora müşteri notu {CANARY} paylaşma.", "internal"
        )
        self.events: list = []

    # -- fakes ---------------------------------------------------------------
    def _write(self, event, *, idempotency_key: str, severity: str) -> bool:
        self.events.append(event)
        return True

    def _events(self, run_id: str) -> list:
        return [e for e in self.events if e.run_id == run_id]

    def _drive(self) -> dict[str, str]:
        """Drive every covered sink with the canary record in reach; return outputs."""
        record = self.store.load_project_instruction(self.project_id)
        history = self.store.list_project_instruction_history(self.project_id)
        # Positive controls: the canary really is stored and survives n6 cleaning,
        # so a clean sink below is not an artefact of redaction.
        self.assertIn(CANARY, record.text)
        self.assertEqual(n6_notify.clean(CANARY), CANARY)
        self.assertTrue(history)

        core = NotificationCore(self._write, self._events)
        for seq, point in enumerate(("slice_started", "needs_input", "done"), 1):
            core.notify("run-canary", point, f"{STEP}:{point}:1", seq=seq, at=AT, step_key=STEP)

        rows = [
            {"outbox_id": i + 1, "kind": e.kind.value, "severity": "high",
             "payload_json": e.payload_json, "title": record.text, "text": record.text}
            for i, e in enumerate(self.events)
        ]
        ticks = iter([100.0, 200.0, 300.0])
        sink = NotifySink(lambda after: [r for r in rows if r["outbox_id"] > after],
                          clock=lambda: next(ticks))
        sink.poll()
        rows.append({"outbox_id": 99, "kind": "needs_input", "severity": "high",
                     "payload_json": json.dumps({"note": record.text}), "text": record.text})
        sink.poll()
        toast = sink.toast()

        state = self.root / "n6"
        state.mkdir(exist_ok=True)
        state = state / "notify.json"
        # The real producer (work_driver._notify) passes identifiers only.
        for kind in ("slice_started", "needs_input", "job_finished"):
            n6_notify.submit(state, n6_notify.Event(kind, f"{STEP}:{kind}:1", STEP))
        sent: list[str] = []
        seqs = iter(range(100, 200))
        n6_notify.flush(state, core=core, run_id="run-canary", next_seq=lambda: next(seqs),
                        at=AT, send=sent.append)
        delivered: list[str] = []
        n6_notify.evening_summary(state, delivered.append)

        return {
            "notification payloads": "\n".join(e.payload_json for e in self.events),
            "notify_sink recent": json.dumps(sink.recent(), ensure_ascii=False),
            "notify_sink toast": json.dumps(toast, ensure_ascii=False),
            "n6 phone send": "\n".join(sent),
            "n6 evening summary": "\n".join(delivered),
            "logs": "\n".join(self.logs.lines),
        }

    def _files(self) -> dict[str, str]:
        """Raw bytes of every temp file except the store that legitimately holds it."""
        out = {}
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            if path.name.startswith(self.db.name):
                continue
            out[f"file {path.relative_to(self.root).as_posix()}"] = path.read_bytes().decode(
                "utf-8", "replace")
        return out

    def test_canary_absent_from_every_covered_sink(self) -> None:
        sinks = self._drive()
        sinks.update(self._files())
        self.assertIn("file n6/notify.json", sinks)  # the n6 state file was written and scanned
        self.assertTrue(self.events)                   # the ledger really got events
        assert_no_canary(sinks)

    def test_injected_leak_is_caught(self) -> None:
        sinks = self._drive()
        leaky: list[str] = []
        record = self.store.load_project_instruction(self.project_id)
        leaky.append(f"toast: {record.text}")  # a test-local fake sink echoing the record
        sinks["fake leaky sink"] = "\n".join(leaky)
        with self.assertRaisesRegex(AssertionError, "fake leaky sink"):
            assert_no_canary(sinks)


if __name__ == "__main__":
    unittest.main()
