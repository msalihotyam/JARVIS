"""N6-REPLAY1: the offline replay of N6's shadow decisions over a recorded conveyor history."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("n6_replay", ROOT / "scripts" / "n6_replay.py")
assert _spec and _spec.loader
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

SHA = {k: k * 40 for k in "acdef"}
HEADS = {"A": SHA["a"], "C": SHA["c"], "E": SHA["f"], "H": SHA["d"]}  # E: a different head
REPORTS = {"A": f"STATUS: PASS\nCOMMIT: {SHA['a']}\n", "B": "STATUS: BLOCKED\n",
           "C": f"STATUS: PASS\nCOMMIT: {SHA['c']}\n", "E": f"STATUS: PASS\nCOMMIT: {SHA['e']}\n",
           "H": f"STATUS: PASS\nCOMMIT: {SHA['d']}\n"}


def _events() -> list[str]:
    script = [("dispatch", "A", None), ("finish", "A", "PASS"),
              ("finish", "B", "BLOCKED"),
              ("finish", "C", "PASS"), ("chain", "C", "CLREV-C"), ("finish", "CLREV-C", "BLOCKED"),
              ("finish", "D", "PASS"),  # no report
              ("finish", "E", "PASS"),
              ("finish", "F", "GREEN"),
              ("finish", "H", "PASS"), ("finish", "INT-H", "BLOCKED"), ("chain", "H", "INT-H"),
              ("dispatch", "Z", None)]  # never finishes: no record
    out = []
    for i, (kind, lane, extra) in enumerate(script):
        ev: dict[str, object] = {"ts": f"2026-11-01T00:00:{i:02d}", "event": kind}
        if kind == "chain":
            ev.update(src=lane, id=extra)
        else:
            ev["lane"] = lane
            if extra:
                ev["status"] = extra
        out.append(json.dumps(ev))
    return out + ["not json", "[1]", json.dumps({"event": "finish", "lane": "X"}),
                  json.dumps({"ts": "2026-11-01T00:01:00", "event": "finish", "lane": "../evil"}),
                  json.dumps({"ts": "2026-11-01T00:01:01", "event": "finish", "lane": "A", "status": []}),
                  ""]


def _fixture(root: Path) -> None:
    conveyor = root / "build" / "conveyor"
    (conveyor / "ack").mkdir(parents=True)
    (conveyor / "events.jsonl").write_text("\n".join(_events()) + "\n", encoding="utf-8")
    (conveyor / "ack" / "A.json").write_text("{}", encoding="utf-8")
    (root / "build" / "reports").mkdir()
    for lane, text in REPORTS.items():
        (root / "build" / "reports" / f"{lane}.md").write_text(text, encoding="utf-8")
    (root / "build" / "approvals" / "done").mkdir(parents=True)
    (root / "build" / "approvals" / "done" / "A.json").write_text("{}", encoding="utf-8")
    (root / "build" / "hold").mkdir()
    (root / "build" / "hold" / "backlog.json.bak-1").write_text("[]", encoding="utf-8")
    (root / "build" / "ORCH_NOTEBOOK.md").write_text("# notes\n", encoding="utf-8")


def _tree(root: Path) -> dict[str, tuple[str, int]]:
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def _main(*argv: str) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        rp.main(list(argv))


class ReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _fixture(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_known_counts(self) -> None:
        result = rp.replay(self.root, head=HEADS.get)
        s, by = result["summary"], {r["lane"]: r for r in result["records"]}
        self.assertEqual(s["totals"], {"agree": 5, "different": 2, "not_comparable": 2})
        self.assertEqual(s["verdicts"], {"agree": 5, "disagree": 1, "false_pass": 1,
                                         "excluded": 1, "unknown": 1})
        self.assertEqual((s["lanes"], s["reports_found"], s["false_pass"]), (9, 5, 1))
        self.assertEqual(s["behaviours"]["R06"], {"agree": 4, "different": 1, "not_comparable": 1,
                                                  "agreement": 0.8})
        self.assertEqual(s["behaviours"]["R05"]["agree"], 1)
        self.assertEqual(s["behaviours"]["R17"]["not_comparable"], 1)
        self.assertEqual(s["behaviours"]["unmapped"]["different"], 1)
        # H was decided before its INT chain event was recorded: N6 passed, the conveyor blocked.
        self.assertEqual((by["H"]["verdict"], by["H"]["conveyor"]["verdict"]), ("false_pass", "block"))
        self.assertEqual(by["C"]["n6"]["reason"], "chained check CLREV-C BLOCKED")
        self.assertNotIn("Z", by)
        self.assertEqual(rp.replay(self.root)["summary"]["would_pass_with_head"], 3)  # A, E, H
        self.assertIn("| R06 | 4 | 1 | 1 | 80.0% |", rp.report(result))
        self.assertIn("## limits", rp.report(result))

    def test_malformed_lines_counted_and_skipped(self) -> None:
        s = rp.replay(self.root)["summary"]
        self.assertEqual(s["malformed"], {"bad_lane": 1, "bad_status": 1, "bad_ts": 1,
                                          "not_json": 1, "not_object": 1})
        self.assertEqual((s["malformed_total"], s["lines"]), (5, 18))

    def test_summary_is_byte_identical(self) -> None:
        _main("--root", str(self.root), "--run", "r1")
        _main("--root", str(self.root), "--run", "r2")
        replay = self.root / "build" / "n6" / "replay"
        self.assertEqual((replay / "r1" / "summary.json").read_bytes(),
                         (replay / "r2" / "summary.json").read_bytes())
        self.assertEqual((replay / "r1" / "decisions.jsonl").read_bytes(),
                         (replay / "r2" / "decisions.jsonl").read_bytes())

    def test_inputs_read_only(self) -> None:
        before = _tree(self.root)
        _main("--root", str(self.root))
        after = _tree(self.root)
        written = {k for k in after if Path(k).parts[:3] == ("build", "n6", "replay")}
        self.assertEqual({k: v for k, v in after.items() if k not in written}, before)
        self.assertEqual({Path(k).name for k in written},
                         {"decisions.jsonl", "report.md", "summary.json"})
        for name in ("kpi.db", "SHADOW_ON", "LIMITED_ON"):
            self.assertFalse((self.root / "build" / "n6" / name).exists())

    def test_refuses_to_write_outside_replay(self) -> None:
        result = rp.replay(self.root)
        outside = Path(self._tmp.name).parent / "n6_replay_outside_probe"
        for run in ("../x", "..", "", ".", str(outside)):
            with self.subTest(run=run), self.assertRaises(SystemExit):
                rp.write(self.root, run, result)
        self.assertFalse(outside.exists())
        self.assertFalse((self.root / "build" / "n6" / "x").exists())


if __name__ == "__main__":
    unittest.main()
