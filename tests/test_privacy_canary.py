"""CANARY1 - a classified record's marker never reaches a non-model sink.

A unique canary is seeded into a classified project record through the
store's explicit-user boundary (temp SQLite, never data/jarvis.db). The
flows that write outside the process other than the model call are then
driven with fakes: the notification ledger payload (NotificationCore), the
tray/in-app sink (NotifySink), the phone path (n6_notify submit / flush /
evening_summary), every log record, and every file left in the temp dir
except the store itself. The canary must appear in none of them.

CANARY1FIX: the ledger and the n6 phone path are fed by the real producer,
work_driver.drive -> _notify, with the record in its reach (the worker task
and the worker results the driver consumes hold it); fake Ports only.
Limit: _CanaryPorts.notify mirrors work_ports.build_ports' notify closure
(same NotificationCore.notify call) rather than running build_ports itself.

No network, no live connector, no wall clock (fixed datetimes, fake clocks).
"""

from __future__ import annotations

import base64
import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from jarvis import n6_notify, work_driver
from jarvis.memory import SQLiteMemoryStore
from jarvis.notify import NotificationCore
from jarvis.notify_sink import NotifySink
from tests.helpers import safe_cleanup
from tests.test_work_driver import FakePorts, result, run_drive

# ASCII, no dot/slash (n6_notify strips links), not credential-shaped.
CANARY = "KANARYA-DORA-7F3A9C"
AT = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
RUN = "run-canary"
MAIN = "c" * 40
NOW = 1_800_000_000.0


# The canary, its distinctive fragments and its common encodings (case-insensitive).
NEEDLES = tuple(n.lower() for n in (
    CANARY, "KANARYA", "7F3A9C", CANARY.encode().hex(),
    base64.b64encode(CANARY.encode()).decode().rstrip("=")))


def assert_no_canary(sinks: dict[str, str]) -> None:
    """Raise AssertionError naming every sink whose output holds the canary."""
    leaked = sorted(name for name, text in sinks.items()
                    if any(n in text.lower() for n in NEEDLES))
    if leaked:
        raise AssertionError(f"canary leaked into: {', '.join(leaked)}")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        # message, formatted traceback/stack, and every record attribute (extra=, args)
        self.lines += [record.getMessage(), logging.Formatter().format(record), str(vars(record))]


class _CanaryPorts(FakePorts):
    """Driver ports whose task and worker results hold the record; notify feeds the ledger."""

    def __init__(self, text: str, core: NotificationCore, seqs) -> None:
        # a: accepted with the record in its summary; b: asks a question quoting it.
        super().__init__({"a": [result(summary=f"done: {text}")],
                          "b": [result(summary=f"{text}?")]})
        self.text, self.core, self.seqs = text, core, seqs

    def task(self, step, stage, findings):
        return {**super().task(step, stage, findings), "brief": self.text}

    def needs_input(self, worker_result):
        return worker_result.summary.endswith("?")

    def notify(self, point, dedupe_key, key):
        super().notify(point, dedupe_key, key)
        self.core.notify(RUN, point, dedupe_key, seq=next(self.seqs), at=AT, step_key=key)


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

    def _n6(self, state: Path) -> work_driver.N6:
        def slice_of(step):
            lane = f"N6-{step.ordinal}"
            return ({"id": lane, "header": f"@jarvis lane={lane} model=opus effort=high base={MAIN}",
                     "files": [f"{step.slug}.py"], "depends_on": []}, MAIN, {}, [])
        return work_driver.N6(state, lambda: NOW, slice_of, watchdog=lambda s, r: "continue")

    def _reach(self, ports: _CanaryPorts) -> None:
        """The record reached the producer: every worker task and consumed result held it."""
        for slug, worker in ports.workers.items():
            self.assertTrue(worker.tasks, slug)
            for task in worker.tasks:
                self.assertIn(CANARY, json.dumps(task, ensure_ascii=False), slug)
            self.assertIn(CANARY, worker.script[0].summary, slug)

    def _drive(self, text: str | None = None) -> dict[str, str]:
        """Drive every covered sink with the canary record in reach; return outputs."""
        record = self.store.load_project_instruction(self.project_id)
        history = self.store.list_project_instruction_history(self.project_id)
        # Positive controls: the canary really is stored and survives n6 cleaning,
        # so a clean sink below is not an artefact of redaction.
        self.assertIn(CANARY, record.text)
        self.assertEqual(n6_notify.clean(CANARY), CANARY)
        self.assertTrue(history)
        text = record.text if text is None else text

        core = NotificationCore(self._write, self._events)
        seqs = iter(range(1, 1000))

        # Ledger: the real producer (drive -> _notify -> ports.notify) feeds NotificationCore.
        ports = _CanaryPorts(text, core, seqs)
        run_drive(ports)
        self._reach(ports)
        self.assertEqual(ports.points(),
                         ["slice_started", "slice_accepted", "slice_started", "needs_input"])
        # every producer notify landed in the ledger (none swallowed by _notify)
        self.assertEqual(len(self.events), len(ports.points()))

        # Phone path: the same producer with N6 set routes _notify into n6_notify.submit.
        state_dir = self.root / "n6"
        state_dir.mkdir(exist_ok=True)
        state = state_dir / "notify.json"
        n6_ports = _CanaryPorts(text, core, seqs)
        n6_ports.n6 = self._n6(state_dir)
        run_drive(n6_ports)
        self._reach(n6_ports)
        self.assertEqual(n6_ports.points(), [])  # every notify went through n6_notify
        stored = state.read_text("utf-8")
        self.assertIn("needs_input", stored)     # real producer events reached the n6 state
        sent: list[str] = []
        n6_notify.flush(state, core=core, run_id=RUN, next_seq=lambda: next(seqs),
                        at=AT, send=sent.append)
        delivered: list[str] = []
        n6_notify.evening_summary(state, delivered.append)
        self.assertTrue(sent)       # the phone sink really received producer events
        self.assertTrue(delivered)  # and so did the evening summary

        rows = [
            {"outbox_id": i + 1, "kind": e.kind.value, "severity": "high",
             "payload_json": e.payload_json, "title": record.text, "text": record.text}
            for i, e in enumerate(self.events)
        ]
        ticks = iter([100.0, 200.0, 300.0])
        sink = NotifySink(lambda after: [r for r in rows if r["outbox_id"] > after],
                          clock=lambda: next(ticks))
        sink.poll()
        rows.append({"outbox_id": 999, "kind": "needs_input", "severity": "high",
                     "payload_json": json.dumps({"note": record.text}), "text": record.text})
        sink.poll()
        toast = sink.toast()

        return {
            "notification payloads": "\n".join(
                f"{e.payload_json}\n{e.step_key}" for e in self.events),
            "n6 stored events": stored,
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
            if path.parent == self.root and path.name in {
                    self.db.name + s for s in ("", "-wal", "-shm", "-journal")}:
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
        record = self.store.load_project_instruction(self.project_id)
        sinks["fake leaky sink"] = f"toast: {record.text}"  # a test-local sink echoing the record
        with self.assertRaisesRegex(AssertionError, "fake leaky sink"):
            assert_no_canary(sinks)

    # -- NEGATIVE TESTS: one per defect-class variant (CANARY1FIX) ----------
    def test_neg_leaky_ledger_producer_is_caught(self) -> None:
        """V1: a producer whose dedupe carries the record shows up in the ledger scan."""
        real = work_driver._notify

        def leaky(ports, point, dedupe, step_key=None):
            real(ports, point, f"{dedupe}:{ports.text}", step_key)

        with mock.patch.object(work_driver, "_notify", leaky):
            sinks = self._drive()
        with self.assertRaisesRegex(AssertionError, "notification payloads"):
            assert_no_canary(sinks)

    def test_neg_leaky_n6_producer_is_caught(self) -> None:
        """V2: a producer Event carrying the record shows up in the n6 state and phone scan."""
        real = n6_notify.Event
        record = self.store.load_project_instruction(self.project_id)

        def leaky(kind, key, text, recommendation=""):
            return real(kind, key, f"{text} {record.text}", recommendation)

        with mock.patch.object(n6_notify, "Event", leaky):
            sinks = self._drive()
        with self.assertRaisesRegex(AssertionError, "n6 phone send, n6 stored events"):
            assert_no_canary(sinks)

    def test_neg_hollow_reach_is_rejected(self) -> None:
        """V3: a producer run whose task and results lack the canary fails the reach control."""
        with self.assertRaises(AssertionError):
            self._drive(text="Dora müşteri notu yok")


if __name__ == "__main__":
    unittest.main()
