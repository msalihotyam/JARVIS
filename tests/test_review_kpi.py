"""REVIEWKPI1: review_kpi parses fixture report trees and joins finding outcomes."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("review_kpi", ROOT / "scripts" / "review_kpi.py")
kpi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kpi)

SHA = "a" * 40


def rep(status, *lines):
    return "\n".join([f"STATUS: {status}", f"COMMIT: {SHA}", *lines]) + "\n"


class ReviewKpiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.reports = self.root / "build" / "reports"
        self.reports.mkdir(parents=True)

    def put(self, name, text):
        (self.reports / name).write_text(text, encoding="utf-8")

    def events(self, *rows):
        p = self.root / "build" / "conveyor" / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows), encoding="utf-8")

    def outcomes(self, data):
        return {(r["report"], f["id"]): f["outcome"] for r in data["reviews"] for f in r["findings"]}

    def test_parse_series_round_and_finding(self):
        self.put("CLREV-AFIX2.md", rep("BLOCKED", "F1 MAJOR: scripts/a.py:10 - broken"))
        r = kpi.build(self.root)["reviews"][0]
        self.assertEqual((r["reviewer"], r["slice"], r["series"], r["round"], r["status"]),
                         ("claude", "AFIX2", "A", 2, "BLOCKED"))
        self.assertEqual(r["findings"], [{"id": "F1", "severity": "MAJOR", "file": "scripts/a.py", "line": 10,
                                          "outcome": "open"}])

    def test_fixed_and_rounds_to_green(self):
        self.put("CLREV-B.md", rep("BLOCKED", "F1 MAJOR: scripts/y.py:5 - bad"))
        self.put("BFIX.md", rep("PASS", "FILES: scripts/y.py (+3/-1)"))
        self.put("CLREV-BFIX.md", rep("PASS", "FINDINGS: none"))
        data = kpi.build(self.root)
        self.assertEqual(self.outcomes(data), {("CLREV-B.md", "F1"): "fixed"})
        g = data["groups"]["claude"]
        self.assertEqual((g["first_round_pass"], g["rounds_to_green_mean"], g["fix_rounds_mean"]), (0.0, 2, 1))

    def test_no_fix_when_next_round_not_clean(self):
        self.put("CLREV-B.md", rep("BLOCKED", "F1 MAJOR: scripts/y.py:5 - bad"))
        self.put("BFIX.md", rep("PASS", "FILES: scripts/y.py (+3/-1)"))
        self.put("CDX-BFIX.md", rep("BLOCKED", "F1 MINOR: scripts/q.py:900 - other"))
        self.assertEqual(self.outcomes(kpi.build(self.root))[("CLREV-B.md", "F1")], "open")

    def test_repeated_same_class_within_window(self):
        self.put("CLREV-A.md", rep("BLOCKED", "F1 MAJOR: scripts/conveyor.py:4065 - x"))
        self.put("CDX-AFIX.md", rep("BLOCKED", "F1 MAJOR: scripts/conveyor.py:4082 - x again"))
        out = self.outcomes(kpi.build(self.root))
        self.assertEqual(out, {("CLREV-A.md", "F1"): "repeated", ("CDX-AFIX.md", "F1"): "open"})

    def test_check_dismissal_by_location_not_codex_number(self):
        self.put("CDX-C.md", rep("BLOCKED", "F1 MINOR: scripts/z.py:7 - a", "F2 MINOR: scripts/z.py:200 - b",
                                 "CHECK: Codex F2 scripts/z.py:99 - Not a defect, rejected.",
                                 "CHECK: z.py:7 downgraded to NIT."))
        out = self.outcomes(kpi.build(self.root))
        self.assertEqual(out, {("CDX-C.md", "F1"): "dismissed", ("CDX-C.md", "F2"): "open"})

    def test_dismissed_when_int_landed_or_review_passed(self):
        self.put("CLREV-D.md", rep("BLOCKED", "F1 MAJOR: scripts/w.py:1 - x"))
        self.put("CLREV-E.md", rep("BLOCKED", "F1 MAJOR: scripts/w.py:1 - x"))
        self.put("CLREV-G.md", rep("PASS", "F1 NIT: scripts/w.py:3 - style"))
        self.events({"ts": "2026-11-01T10:00:00", "event": "ci_result", "lane": "INT-D-R", "result": "success"},
                    {"ts": "2026-11-01T10:00:00", "event": "chain", "src": "E", "step": "INT", "id": "INT-E"})
        out = self.outcomes(kpi.build(self.root))
        self.assertEqual(set(out.values()), {"dismissed"})

    def test_malformed_counted_never_dropped(self):
        self.put("CLREV-M1.md", "no status here\n")
        self.put("CLREV-M2.md", rep("BLOCKED", "F1 CHECK: scripts/a.py:1 - odd"))
        self.put("CDX-M3.md", rep("BLOCKED", "FINDINGS: none"))
        self.put("CDX-M4.md", "STATUS: PASS\nFINDINGS: none\n")
        self.put("CDX-M5.md", rep("BLOCKED", "F1 MAJOR: no location"))
        self.put("CLREV-OK.md", rep("PASS", "FINDINGS: none"))
        self.put("CLREV78.md", rep("PASS"))
        data = kpi.build(self.root)
        self.assertEqual(sorted(m["report"] for m in data["malformed"]),
                         ["CDX-M3.md", "CDX-M4.md", "CDX-M5.md", "CLREV-M1.md", "CLREV-M2.md"])
        self.assertEqual((data["groups"]["all"]["malformed"], data["groups"]["claude"]["malformed"],
                          data["groups"]["codex"]["malformed"]), (5, 2, 3))
        self.assertEqual((data["groups"]["all"]["reviews"], data["legacy_names_skipped"]), (1, 1))

    def test_aggregates_by_day_and_bad_events(self):
        self.put("CLREV-H.md", rep("PASS", "FINDINGS: none"))
        self.put("CDX-H.md", rep("BLOCKED", "F1 MAJOR: a.py:1 - x", "F2 MINOR: b.py:2 - y"))
        self.events({"ts": "2026-11-02T01:00:00", "event": "finish", "lane": "CLREV-H", "status": "PASS"},
                    {"ts": "2026-11-03T01:00:00", "event": "finish", "lane": "CDX-H", "status": "BLOCKED"},
                    "{broken", "[1]")
        data = kpi.build(self.root)
        self.assertEqual(data["events_unparsable"], 2)
        self.assertEqual(data["by_day"]["2026-11-02"]["claude"]["first_round_pass"], 1.0)
        codex = data["by_day"]["2026-11-03"]["codex"]
        self.assertEqual((codex["reviews"], codex["findings_per_review"]["MAJOR"], codex["outcomes"]["open"]),
                         (1, 1.0, 2))
        self.assertNotIn("rounds_to_green_mean", codex)
        self.assertEqual(data["groups"]["all"]["first_round_pass"], 0.5)

    def test_main_writes_json_and_prints_table(self):
        self.put("CLREV-K.md", rep("PASS", "FINDINGS: none"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(kpi.main(["--root", str(self.root)]), 0)
        data = json.loads((self.root / "build" / "orch" / "review_kpi.json").read_text(encoding="utf-8"))
        self.assertEqual(data["groups"]["claude"]["reviews"], 1)
        self.assertIn("1st-pass", buf.getvalue())
        self.assertEqual(sorted(p.name for p in self.reports.iterdir()), ["CLREV-K.md"])


if __name__ == "__main__":
    unittest.main()
