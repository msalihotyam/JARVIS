"""N6-SHADOWDIFF1: the daily shadow diff classifies each disagreement once, reads only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jarvis.n6_kpi import KpiRow, record

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("n6_shadow_diff", ROOT / "scripts" / "n6_shadow_diff.py")
assert _SPEC and _SPEC.loader
diff = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(diff)

TZ = timezone(timedelta(hours=3))
SHADOW = datetime(2026, 11, 1, tzinfo=TZ)
SHA = "a" * 40
DAY = date(2026, 11, 13)


def _ts(*ymd: int) -> float:
    return datetime(*ymd, tzinfo=TZ).timestamp()


def _note(lane: str, n6_reason: str, at: datetime = SHADOW) -> str:
    text = f"{lane} disagree: conveyor pass (lane finished PASS); n6 block ({n6_reason})"
    return json.dumps({"at": at.isoformat(), "text": text})


class ShadowDiffTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for sub in ("build/n6", "build/conveyor", "build/reports"):
            (self.root / sub).mkdir(parents=True)
        lanes = {
            "HEALED": {"finished": True, "status": "PASS", "finished_ts": _ts(2026, 11, 5),
                       "files": ["jarvis/x.py"]},
            "OLD": {"finished": True, "status": "PASS", "finished_ts": _ts(2026, 10, 10),
                    "files": ["jarvis/y.py"]},
            "GOOD": {"finished": True, "status": "PASS", "finished_ts": _ts(2026, 11, 6),
                     "files": ["jarvis/z.py"]},
        }
        self._w("build/conveyor/lanes.json", json.dumps(lanes))
        heal = {"ts": _ts(2026, 11, 5), "event": "heal", "lane": "HEALED", "was": "BLOCKED",
                "status": "PASS", "by": "HEALED2"}
        self._w("build/conveyor/events.jsonl", json.dumps(heal) + "\n")
        self._w("build/reports/HEALED.md", "STATUS: BLOCKED\nCOMMIT: none\n")
        self._w("build/reports/OLD.md", "STATUS: PASS\nBRANCH: b\n")
        self._w("build/reports/GOOD.md", f"STATUS: PASS\nCOMMIT: {SHA}\n")
        notes = [_note("HEALED", "report says STATUS: BLOCKED"),
                 _note("OLD", "report needs exactly one 40-hex COMMIT"),
                 "{not json", '{"at": "x", "text": 1}',
                 _note("GOOD", "report says ```", SHADOW + timedelta(days=6))]
        self._w("build/n6/notes.jsonl", "\n".join(notes) + "\n")
        self._w("build/n6/SHADOW_ON", "")
        record(self.root / "build/n6/kpi.db",
               KpiRow("GOOD", "n6", False, 0, 0, 0, 1.0, 0, SHADOW))

    def _w(self, rel: str, text: str) -> None:
        (self.root / rel).write_text(text, encoding="utf-8")

    def _records(self) -> dict[str, dict]:
        rep = diff.build(self.root, DAY)
        return {r["lane"]: r for r in rep["records"]}

    def test_healed_lane_with_blocked_report_is_orchestrator_wrong(self) -> None:
        r = self._records()["HEALED"]
        self.assertEqual((r["class"], r["rule"], r["behaviour"], r["era"]),
                         ("orchestrator_or_conveyor_wrong", "report_not_pass", "R12", "live"))
        self.assertIn("build/conveyor/events.jsonl:1", r["evidence"])

    def test_old_no_commit_report_is_stricter_format_sweep(self) -> None:
        r = self._records()["OLD"]
        self.assertEqual((r["class"], r["rule"], r["era"]), ("n6_stricter_format", "pre_rule", "sweep"))

    def test_live_block_of_valid_pass_report_is_n6_wrong(self) -> None:
        rep = diff.build(self.root, DAY)
        r = {x["lane"]: x for x in rep["records"]}["GOOD"]
        self.assertEqual((r["class"], r["rule"], r["era"]), ("n6_wrong", "rule_accepts", "live"))
        self.assertEqual(r["kpi"], {"n6": False})
        self.assertEqual(rep["live"]["n6_wrong"], 1)
        self.assertIn("GOOD (live, rule_accepts)", diff.render_md(rep))

    def test_same_inputs_give_identical_json(self) -> None:
        js, _ = diff.write(self.root, DAY)
        first = js.read_bytes()
        diff.write(self.root, DAY)
        self.assertEqual(js.read_bytes(), first)

    def test_inputs_stay_read_only(self) -> None:
        inputs = [p for p in self.root.rglob("*") if p.is_file()]

        def seen() -> dict[Path, tuple[str, int]]:
            return {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                    for p in inputs}

        before = seen()
        js, md = diff.write(self.root, DAY)
        self.assertEqual(seen(), before)
        written = {p for p in self.root.rglob("*") if p.is_file()} - set(inputs)
        self.assertEqual(written, {js, md})
        self.assertFalse((self.root / "build/n6/LIMITED_ON").exists())

    def test_malformed_note_lines_are_counted(self) -> None:
        rep = diff.build(self.root, DAY)
        self.assertEqual((rep["notes"]["lines"], rep["notes"]["malformed"]), (5, 2))
        self.assertEqual(len(rep["records"]), 3)


if __name__ == "__main__":
    unittest.main()
