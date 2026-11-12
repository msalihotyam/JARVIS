"""N6-REPLAY1(FIX): the offline replay of N6's shadow decisions over a recorded conveyor history."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("n6_replay", ROOT / "scripts" / "n6_replay.py")
assert _spec and _spec.loader
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

SHA = {k: k * 40 for k in "acdef123"}
HEADS = {"A": SHA["a"], "C": SHA["c"], "E": SHA["f"], "H": SHA["d"],  # E: a different head
         "R": SHA["1"], "W": SHA["2"], "P": SHA["3"]}
REPORTS = {"A": f"STATUS: PASS\nCOMMIT: {SHA['a']}\n", "B": "STATUS: BLOCKED\n",
           "C": f"STATUS: PASS\nCOMMIT: {SHA['c']}\n", "E": f"STATUS: PASS\nCOMMIT: {SHA['e']}\n",
           "H": f"STATUS: PASS\nCOMMIT: {SHA['d']}\n", "R": f"STATUS: PASS\nCOMMIT: {SHA['1']}\n",
           "W": f"STATUS: PASS\nCOMMIT: {SHA['2']}\n", "P": f"STATUS: PASS\nCOMMIT: {SHA['3']}\n"}


def _events() -> list[str]:
    script = [("dispatch", "A", None), ("finish", "A", "PASS"),
              ("finish", "B", "BLOCKED"),
              ("finish", "C", "PASS"), ("chain", "C", "CLREV-C"), ("finish", "CLREV-C", "BLOCKED"),
              ("finish", "D", "PASS"),  # no report
              ("finish", "E", "PASS"),
              ("finish", "F", "GREEN"),
              ("finish", "H", "PASS"), ("finish", "INT-H", "BLOCKED"), ("chain", "H", "INT-H"),
              ("dispatch", "Z", None),  # never finishes: no record
              # R: its child passed, then was redispatched and blocked after R's decision
              ("finish", "R", "PASS"), ("chain", "R", "CLREV-R"), ("finish", "CLREV-R", "PASS"),
              ("dispatch", "CLREV-R", None), ("finish", "CLREV-R", "BLOCKED"),
              # W finished twice (indices 18 and 22): today's report proves neither decision
              ("finish", "W", "BLOCKED"), ("chain", "W", "CLREV-W"), ("finish", "CLREV-W", "BLOCKED"),
              ("dispatch", "W", None), ("finish", "W", "PASS"),
              # P: the chain is recorded while P runs and its child blocks before P finishes
              ("dispatch", "P", None), ("chain", "P", "CLREV-P"), ("finish", "CLREV-P", "BLOCKED"),
              ("finish", "P", "PASS")]
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
                  json.dumps({"ts": "2026-11-01T00:01:02", "event": "chain", "src": "A"}),  # no id
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
        s, recs = result["summary"], result["records"]
        by = {r["lane"]: r for r in recs if r["lane"] != "W"}
        w = {r["finish"]: r for r in recs if r["lane"] == "W"}
        self.assertEqual(s["totals"], {"agree": 6, "different": 0, "not_comparable": 11})
        self.assertEqual(s["verdicts"], {"agree": 6, "disagree": 0, "false_pass": 0, "excluded": 1,
                                         "unprovable": 9, "unknown": 1})
        self.assertEqual((s["lanes"], s["decisions"], s["reports_found"], s["false_pass"]),
                         (15, 17, 8, 0))
        self.assertEqual(s["behaviours"]["R06"], {"agree": 3, "different": 0, "not_comparable": 10,
                                                  "agreement": 1.0})
        self.assertEqual(s["behaviours"]["R05"]["agree"], 2)
        self.assertEqual(s["behaviours"]["R17"]["not_comparable"], 1)
        self.assertEqual(s["behaviours"]["unmapped"]["agree"], 1)
        self.assertEqual(sorted(s["behaviours"]), ["R05", "R06", "R17", "unmapped"])
        # INT-H finished before its chain event was recorded: the chain that moves H's decision
        # point stays visible (at >= its index), so both verdicts see INT-H BLOCKED.
        self.assertEqual((by["H"]["verdict"], by["H"]["conveyor"], by["H"]["n6"]["verdict"]),
                         ("agree", {"verdict": "block", "reason": "chained check INT-H BLOCKED"},
                          "block"))
        # R is judged when its child first finished (PASS), not on the child's later BLOCKED rerun.
        self.assertEqual((by["R"]["verdict"], by["R"]["conveyor"]["verdict"]), ("agree", "pass"))
        # W finished twice: each finish is its own decision on its own state, both unprovable.
        self.assertEqual(sorted(w), [18, 22])
        self.assertEqual((w[18]["conveyor"]["reason"], w[18]["verdict"]),
                         ("lane finished BLOCKED", "unprovable"))
        self.assertEqual((w[22]["conveyor"]["verdict"], w[22]["verdict"]), ("pass", "unprovable"))
        self.assertEqual(by["P"]["verdict"], "agree")
        self.assertEqual(by["P"]["n6"]["reason"], "chained check CLREV-P BLOCKED")
        self.assertEqual(by["C"]["n6"]["reason"], "chained check CLREV-C BLOCKED")
        self.assertEqual(by["D"]["verdict"], "unprovable")
        self.assertNotIn("Z", by)
        self.assertEqual(rp.replay(self.root)["summary"]["would_pass_with_head"], 4)  # A E R W
        self.assertIn("| R06 | 3 | 0 | 10 | 100.0% |", rp.report(result))
        self.assertIn("## limits", rp.report(result))

    def test_sibling_redispatch_and_redispatch_without_refinish(self) -> None:
        script = [("finish", "A", "PASS"), ("chain", "A", "C1"), ("chain", "A", "C2"),
                  ("finish", "C1", "BLOCKED"), ("dispatch", "C1", None), ("finish", "C2", "PASS"),
                  ("finish", "G", "PASS"), ("dispatch", "G", None)]
        lines = []
        for i, (kind, lane, extra) in enumerate(script):
            ev: dict[str, object] = {"ts": f"2026-11-02T00:00:{i:02d}", "event": kind}
            ev.update({"src": lane, "id": extra} if kind == "chain" else
                      {"lane": lane, **({"status": extra} if extra else {})})
            lines.append(json.dumps(ev) + "\n")
        (self.root / "build" / "conveyor" / "events.jsonl").write_text("".join(lines), encoding="utf-8")
        (self.root / "build" / "reports" / "G.md").write_text(REPORTS["A"], encoding="utf-8")
        by = {r["lane"]: r for r in rp.replay(self.root, head={"A": SHA["a"], "G": SHA["a"]}.get)["records"]}
        # C1 blocked, then was redispatched before its sibling C2 finished (the decision point):
        # at that point C1 is unfinished, so the conveyor itself waits on it - not comparable.
        self.assertEqual(by["A"]["conveyor"], {"verdict": "excluded", "reason": "chained check C1 pending"})
        self.assertEqual((by["A"]["verdict"], by["A"]["outcome"]), ("excluded", "not_comparable"))
        self.assertEqual((by["G"]["verdict"], by["G"]["class"].endswith("lane redispatched after this finish")),
                         ("unprovable", True))

    def test_false_pass_is_counted_when_n6_passes_a_block(self) -> None:
        with mock.patch.object(rp, "n6_verdict", return_value=("pass", "forced")):
            result = rp.replay(self.root, head=HEADS.get)
        by = {r["lane"]: r for r in result["records"] if r["lane"] != "W"}
        self.assertEqual((by["C"]["outcome"], by["C"]["verdict"], by["C"]["conveyor"]["verdict"]),
                         ("different", "false_pass", "block"))
        self.assertEqual(result["summary"]["false_pass"], result["summary"]["verdicts"]["false_pass"])
        self.assertEqual(sorted(r["lane"] for r in result["records"] if r["verdict"] == "false_pass"),
                         ["B", "C", "H", "P"])  # exactly the comparable blocks

    def test_refuses_a_linked_replay_directory(self) -> None:
        result = rp.replay(self.root)
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (self.root / "build" / "n6").mkdir()
        try:
            import _winapi  # a junction needs no symlink privilege on Windows
            _winapi.CreateJunction(str(outside), str(self.root / "build" / "n6" / "replay"))
        except ImportError:
            os.symlink(outside, self.root / "build" / "n6" / "replay", target_is_directory=True)
        with self.assertRaises(SystemExit):
            rp.write(self.root, "r1", result)
        self.assertEqual(list(outside.iterdir()), [])

    def test_malformed_lines_counted_and_skipped(self) -> None:
        s = rp.replay(self.root)["summary"]
        self.assertEqual(s["malformed"], {"bad_lane": 2, "bad_status": 1, "bad_ts": 1,
                                          "not_json": 1, "not_object": 1})
        self.assertEqual((s["malformed_total"], s["lines"]), (6, 33))

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

    def test_refuses_a_symlinked_output_file(self) -> None:
        result = rp.replay(self.root)
        out = self.root / "build" / "n6" / "replay" / "s"
        out.mkdir(parents=True)
        outside = self.root / "outside.txt"
        outside.write_bytes(b"keep")
        try:
            os.symlink(outside, out / "summary.json")
            patch: contextlib.AbstractContextManager[object] = contextlib.nullcontext()
        except OSError:  # no symlink privilege: fake the link the code must refuse
            patch = mock.patch.object(rp.Path, "is_symlink", lambda p: p.name == "summary.json")
        with patch, self.assertRaises(SystemExit):
            rp.write(self.root, "s", result)
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertFalse((out / "report.md").exists())

    def test_hardlinked_output_file_is_replaced_not_written_through(self) -> None:
        result = rp.replay(self.root)
        out = self.root / "build" / "n6" / "replay" / "h"
        out.mkdir(parents=True)
        outside = self.root / "outside.txt"
        outside.write_bytes(b"keep")
        os.link(outside, out / "report.md")
        rp.write(self.root, "h", result)
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertEqual((out / "report.md").read_text(encoding="utf-8"), rp.report(result))
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["decisions.jsonl", "report.md", "summary.json"])


if __name__ == "__main__":
    unittest.main()
