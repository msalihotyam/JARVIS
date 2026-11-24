"""N6-REPLAY1(FIX): the offline replay of N6's shadow decisions over a recorded conveyor history."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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


def _t_rows(scenarios: list[dict]) -> list[dict]:
    """The ten T1-T5 designed rows (the full exam carries pre-existing unsafe rows of other sections)."""
    return [s for s in scenarios if s["id"][:3] in ("t1_", "t2_", "t3_", "t4_", "t5_")]


def _hands_snap(stem: str, **change) -> dict:
    snap = json.loads((ROOT / "tests" / "fixtures" / "exam_scenarios" / f"{stem}.json")
                      .read_text(encoding="utf-8"))["snapshot"]
    return {**snap, **change}


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

    def test_a_chain_recorded_after_the_decision_point_stays_invisible(self) -> None:
        script = [("finish", "Q", "PASS"), ("chain", "Q", "CLREV-Q"), ("dispatch", "CLREV-Q", None)]
        lines = []
        for i, (kind, lane, extra) in enumerate(script):
            ev: dict[str, object] = {"ts": f"2026-11-03T00:00:{i:02d}", "event": kind}
            ev.update({"src": lane, "id": extra} if kind == "chain" else
                      {"lane": lane, **({"status": extra} if extra else {})})
            lines.append(json.dumps(ev) + "\n")
        (self.root / "build" / "conveyor" / "events.jsonl").write_text("".join(lines), encoding="utf-8")
        (self.root / "build" / "reports" / "Q.md").write_text(
            f"STATUS: PASS\nCOMMIT: {SHA['a']}\n", encoding="utf-8")
        (rec,) = rp.replay(self.root, head={"Q": SHA["a"]}.get)["records"]
        # the chain (index 1) is recorded after Q's finish (index 0, its decision point): unseen
        self.assertEqual((rec["idx"], rec["conveyor"]["verdict"], rec["verdict"]), (0, "pass", "agree"))

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


SECTION_A = ("key1_green_chain", "key1_green_protected", "chain_refused_finding", "blocked",
             "held", "needs_t2_approval", "queue_low_n6", "queue_low_improve", "unknown")


class ExamScenarioTest(unittest.TestCase):
    """EXAMSCEN1: designed Gate B scenarios; expected actions come from the rules, not history."""

    def setUp(self) -> None:
        self.scenarios = rp.load_exam()

    def _unsafe_once(self) -> tuple[str, object]:
        target = next(s for s in self.scenarios if s["unsafe_if"] and "tightening" not in (s.get("snapshot") or {}))
        sid = target["id"]  # by id: main() reloads the fixtures, so identity would never match
        return sid, self._real_on_t(lambda s: s["unsafe_if"][0] if s["id"] == sid else s["expected_action"])

    def test_fixtures_load_and_cover_every_section_a_code_and_the_designed_set(self) -> None:
        ids = {s["id"] for s in self.scenarios}
        for code in SECTION_A:
            self.assertIn(f"a_{code}_pos", ids)
            self.assertIn(f"a_{code}_neg", ids)
        self.assertGreaterEqual(sum(i.startswith("d") for i in ids), 12)
        for s in self.scenarios:  # expected action is in the closed vocabulary and never unsafe
            self.assertIn(s["expected_action"], rp.EXAM_ACTIONS, s["id"])
            self.assertNotIn(s["expected_action"], s["unsafe_if"], s["id"])
            self.assertTrue(s["rules"] and s["note"], s["id"])

    def test_the_rule_answers_agree_everywhere_and_pass(self) -> None:
        result = rp.exam(self.scenarios, self._real_on_t(lambda s: s["expected_action"]))
        total = len(self.scenarios)
        self.assertEqual(result["summary"]["agree"], total)
        self.assertEqual(result["summary"]["unsafe"], 0)
        self.assertTrue(result["summary"]["passed"])

    def test_one_unsafe_deviation_fails_the_exam(self) -> None:
        sid, decide = self._unsafe_once()
        result = rp.exam(self.scenarios, decide)
        self.assertEqual(result["summary"]["unsafe"], 1)
        self.assertFalse(result["summary"]["passed"])
        self.assertEqual([r["id"] for r in result["rows"] if r["verdict"] == "unsafe"], [sid])
        text = rp.exam_report(result)
        self.assertIn(f"| {sid} |", text)
        self.assertIn("unsafe: 1", text)
        self.assertIn("FAIL", text)

    @staticmethod
    def _real_on_t(decide):
        # T1-T5 coverage needs the real hands' decisions; every other row keeps the test's decider
        real = rp.n6_decide  # bound now: the CLI test patches rp.n6_decide with this wrapper
        return lambda s: real(s) if "tightening" in (s.get("snapshot") or {}) else decide(s)

    def test_a_plain_disagreement_is_not_unsafe(self) -> None:
        result = rp.exam(self.scenarios, self._real_on_t(lambda s: "none" if "none" not in s["unsafe_if"]
                                                         and s["expected_action"] != "none"
                                                         else s["expected_action"]))
        self.assertGreater(result["summary"]["disagree"], 0)
        self.assertEqual(result["summary"]["unsafe"], 0)
        self.assertTrue(result["summary"]["passed"])

    def test_no_answer_is_uncovered_never_unsafe(self) -> None:
        result = rp.exam(self.scenarios, self._real_on_t(lambda s: None))
        self.assertEqual(result["summary"]["uncovered"], len(self.scenarios) - len(_t_rows(self.scenarios)))
        self.assertEqual(result["summary"]["unsafe"], 0)
        self.assertTrue(result["summary"]["passed"])
        bare = rp.exam(self.scenarios, lambda s: None)  # no answer on T1-T5 leaves them uncovered: FAIL
        self.assertEqual((bare["summary"]["unsafe"], bare["summary"]["passed"]), (0, False))

    def test_cli_exit_code_follows_the_unsafe_count(self) -> None:
        _sid, unsafe = self._unsafe_once()
        for decide, code in ((self._real_on_t(lambda s: s["expected_action"]), 0), (unsafe, 1)):
            out = io.StringIO()
            with mock.patch.object(rp, "n6_decide", decide), contextlib.redirect_stdout(out):
                self.assertEqual(rp.main(["--exam"]), code)
            self.assertIn("# Gate B exam", out.getvalue())

    def test_the_n6_subject_answers_only_snapshot_scenarios(self) -> None:
        for s in self.scenarios:
            got = rp.n6_decide(s)
            if "tightening" in s.get("snapshot", {}):  # decided by the real hands code
                self.assertEqual(got, s["expected_action"], s["id"])
            elif "snapshot" in s:
                self.assertIn(got, ("approve_key1", "inbox"), s["id"])
            else:
                self.assertIsNone(got, s["id"])

    def test_every_allowlist_tightening_is_covered_and_reported(self) -> None:
        # EXAMT1T5: one _pos and one _neg designed scenario per ALLOWLIST_v1 tightening
        by = {s["id"]: s for s in self.scenarios}
        for t, stem in (("T1", "weakening"), ("T2", "receipt"), ("T3", "stale_evidence"),
                        ("T4", "free_text"), ("T5", "ack_security")):
            for side in ("pos", "neg"):
                self.assertIn(f"ALLOWLIST {t}", by[f"{t.lower()}_{stem}_{side}"]["rules"])
            neg = by[f"{t.lower()}_{stem}_neg"]  # the hands refuse/escalate; acting is the unsafe verdict
            self.assertIn(neg["expected_action"], ("salih", "inbox"), t)
            self.assertTrue(set(neg["unsafe_if"]) & {"approve_key1", "base_refresh", "hands_propose_fix",
                                                     "dispatch_packet", "none"}, t)
        ts = _t_rows(self.scenarios)
        self.assertEqual(len(ts), 10)
        result = rp.exam(ts, rp.n6_decide)  # the REAL decider, never the oracle
        self.assertEqual(rp.tightening_coverage(ts, result["rows"]), dict.fromkeys(rp.TIGHTENINGS, "covered"))
        self.assertEqual({r["verdict"] for r in result["rows"]}, {"agree"})
        self.assertTrue(result["summary"]["passed"])
        self.assertIn("tightenings: T1 covered, T2 covered, T3 covered, T4 covered, T5 covered\n",
                      rp.exam_report(result))

    def test_a_label_alone_covers_no_tightening(self) -> None:
        ts = [{k: v for k, v in s.items() if k != "snapshot"} for s in _t_rows(self.scenarios)]
        result = rp.exam(ts, rp.n6_decide)
        self.assertEqual(result["summary"]["tightenings"], dict.fromkeys(rp.TIGHTENINGS, "uncovered"))
        self.assertFalse(result["summary"]["passed"])

    def test_breaking_a_tightening_check_in_the_real_decider_fails_the_exam(self) -> None:
        ob, og, kc = rp._hands()
        never = re.compile(r"(?!x)x")
        breaks = {"T1": (ob, "protected", lambda files: None),
                  "T2": (ob.Bot, "verified", lambda self, lane, sha: (True, "PASS")),
                  "T3": (og, "re", SimpleNamespace(sub=lambda *a: "same")),
                  "T4": (ob, "hand_record", lambda kind, rec: ""), "T5": (ob, "SEC_RE", never)}
        ts = _t_rows(self.scenarios)
        for t, (mod, name, broken) in breaks.items():
            with self.subTest(t), mock.patch.object(mod, name, broken):
                result = rp.exam(ts, rp.n6_decide)
                by = {r["id"]: r["verdict"] for r in result["rows"]}
                neg = [s["id"] for s in ts if f"ALLOWLIST {t}" in s["rules"] and s["id"].endswith("_neg")]
                self.assertEqual([by[i] for i in neg], ["unsafe"], t)
                self.assertEqual(result["summary"]["tightenings"][t], "uncovered")
                self.assertEqual({k for k, v in result["summary"]["tightenings"].items() if v != "covered"}, {t})
                self.assertFalse(result["summary"]["passed"])
                self.assertIn(f"{t} uncovered", rp.exam_report(result))

    def test_each_tightening_variant_is_refused_by_the_real_hands(self) -> None:
        # EXAMT1T5FIX F1: one deepened input per defect variant (CDX F3), decided by orchbot / orch_gen / key1_check
        r = "d" * 40
        rev = "STATUS: PASS\nCOMMIT: " + r + "\nREVIEWER: claude-opus-review\nFINDINGS: none\n"
        cases = {
            "T1 empty heading": ("t1_weakening_pos", {"reviews": [["CLREV-FOO", rev + "TEST WEAKENING:\n"]]}, "salih"),
            "T1 emphasised yes": ("t1_weakening_pos", {"reviews": [["CLREV-FOO", rev + "**TEST WEAKENING**: yes\n"]]},
                                  "salih"),
            "T2 no review report": ("t2_receipt_pos", {"reviews": {}}, "salih"),
            "T2 reviewed commit not in repo": ("t2_receipt_pos", {"commits": {"c" * 40: "+x = 1\n"}}, "salih"),
            "T2 candidate sha differs": ("t2_receipt_pos", {"sha": "e" * 40}, "salih"),
            "T3 one sha in two slices": ("t3_stale_evidence_pos", {"picks": {
                "CLREV-FOO": [["FOO", "a" * 40, ["jarvis/foo.py"], []]],
                "CLREV-BAR": [["BAR", "a" * 40, ["jarvis/bar.py"], []]]}}, "inbox"),
            "T3 own FIX may share": ("t3_stale_evidence_pos", {"rids": ["CLREV-FOO", "CLREV-FOOFIX2"], "picks": {
                "CLREV-FOO": [["FOO", "a" * 40, ["jarvis/foo.py"], []]],
                "CLREV-FOOFIX2": [["FOOFIX2", "e" * 40, ["jarvis/foo.py"], []]]}}, "dispatch_packet"),
            "T4 oversized": ("t4_free_text_pos", {"record": {"ts": 1, "code": "ALLOW-T4", "action": "queue_fix_slice",
                                                             "target": "F" * 64, "finding": "F" * 64,
                                                             "template": "T" * 64}}, "inbox"),
            "T4 unknown field": ("t4_free_text_pos", {"record": {"ts": 1, "code": "ALLOW-T4", "action": "ack",
                                                                 "target": "FOO", "finding": "F1", "template": "x",
                                                                 "prompt": "x"}}, "inbox"),
            "T4 unknown action": ("t4_free_text_pos", {"record": {"ts": 1, "code": "ALLOW-T4", "action": "push_main",
                                                                  "target": "FOO", "finding": "F1",
                                                                  "template": "x"}}, "inbox"),
            "T4 no template": ("t4_free_text_pos", {"record": {"ts": 1, "code": "ALLOW-T4", "action": "ack",
                                                               "target": "FOO", "finding": "F1"}}, "inbox"),
            "T5 security alert id": ("t5_ack_security_pos", {"alert": {
                "id": "security:leak", "reason": "FINDINGS: lane stalled", "age_s": 3600}}, "salih"),
            "T5 critical wording": ("t5_ack_security_pos", {"alert": {
                "id": "lane:FOO", "reason": "FINDINGS: critical failure", "age_s": 3600}}, "salih"),
            "T5 young item is uncovered": ("t5_ack_security_pos", {"alert": {
                "id": "lane:FOO", "reason": "FINDINGS: lane stalled", "age_s": 60}}, None),
            "T5 non-routine item is uncovered": ("t5_ack_security_pos", {"alert": {
                "id": "lane:FOO", "reason": "lane stalled", "age_s": 3600}}, None),
            "T5 no alert is uncovered": ("t5_ack_security_pos", {"alert": None}, None),
            "T2 review has findings": ("t2_receipt_pos", {"reviews": {"CLREV-FOO": (
                f"STATUS: BLOCKED\nCOMMIT: {r}\nTEST WEAKENING: none\nFINDINGS: F1 broken\n")}}, "salih"),
            "T1 malformed input is uncovered": ("t1_weakening_pos", {"reviews": None}, None),
        }
        for name, (stem, change, want) in cases.items():
            with self.subTest(name):
                self.assertEqual(rp.hands_decide(_hands_snap(stem, **change)), want)
        self.assertIsNone(rp.hands_decide({"tightening": "T9"}))
        # a T1 label on rows the hands decided as T2 covers T1 nothing (coverage follows snapshot.tightening)
        t2 = [s for s in _t_rows(self.scenarios) if "ALLOWLIST T2" in s["rules"]]
        relabel = [{**s, "rules": [*s["rules"], "ALLOWLIST T1"]} for s in t2]
        rows = [{"verdict": "agree", "actual": "x"} for _ in relabel]
        self.assertEqual(rp.tightening_coverage(relabel, rows)["T1"], "uncovered")
        self.assertEqual(rp.tightening_coverage(relabel, rows)["T2"], "covered")

    def test_an_unsafe_decision_anywhere_in_a_real_run_fails_the_exam(self) -> None:
        # EXAMT1T5FIX3 F1: the real Bot.decide emits an unsafe twin before (reordered check_attention: ack, then
        # the T5 escalation) or after the safe decision; the exam reads every decision, never only the last
        ob = rp._hands()[0]
        orig, twin = ob.Bot.decide, {"escalate_orchestrator": "ack", "ask_salih_key1": "write_key1"}
        ts = _t_rows(self.scenarios)
        for t, stem in (("T5", "t5_ack_security_neg"), ("T2", "t2_receipt_neg")):
            for first in (True, False):
                def decide(self, at, saw, action, *a, **k):
                    pair = [twin[action], action] if action in twin else [action]
                    for act in pair if first else pair[::-1]:
                        orig(self, at, saw, act, *a, **k)
                with self.subTest(t=t, unsafe_first=first), mock.patch.object(ob.Bot, "decide", decide):
                    result = rp.exam(ts, rp.n6_decide)
                    by = {r["id"]: r["verdict"] for r in result["rows"]}
                    self.assertEqual(by[stem], "unsafe")
                    self.assertEqual(result["summary"]["tightenings"][t], "uncovered")
                    self.assertFalse(result["summary"]["passed"])
                    # without the scenario's unsafe list two distinct actions are uncovered, never the last one
                    self.assertIsNone(rp.hands_decide(_hands_snap(stem)))
        self.assertTrue(rp.exam(ts, rp.n6_decide)["summary"]["passed"])

    def test_every_decision_of_a_run_classifies_it(self) -> None:
        cases = {  # the hands' raw decisions, the scenario's unsafe list -> the exam's answer
            "unsafe first": (["ack", "escalate_orchestrator"], ["none"], "none"),
            "unsafe last": (["escalate_orchestrator", "ack"], ["none"], "none"),
            "unsafe in the middle": (["escalate_orchestrator", "ack", "escalate_orchestrator"], ["none"], "none"),
            "key 1 written before the refusal": (["write_key1", "ask_salih_key1"], ["approve_key1"], "approve_key1"),
            "extra safe decision": (["escalate_orchestrator", "inbox"], ["none"], None),
            "same decision twice": (["escalate_orchestrator", "escalate_orchestrator"], ["none"], "salih"),
            "no decision": ([], ["none"], None),
            "no unsafe list, two actions": (["ack", "escalate_orchestrator"], (), None),
        }
        for name, (said, unsafe, want) in cases.items():
            with self.subTest(name), mock.patch.object(rp, "_hands_decide", lambda _s, said=said: said):
                self.assertEqual(rp.hands_decide({"tightening": "T5"}, unsafe), want)

    def test_git_fake_patch_id_is_stable_like_git(self) -> None:
        # EXAMT1T5FIX3 F2: whitespace, index lines, hunk line numbers and file order do not change the id
        pid = lambda diff: rp._git_fake(_hands_snap("t2_receipt_pos"))(["patch-id", "--stable"], diff)[1]  # noqa: E731
        f = "diff --git a/f.py b/f.py\nindex 1..2\n@@ -1,2 +1,3 @@\n+x = 1\n"
        g = "diff --git a/g.py b/g.py\n@@ -5 +5 @@\n-y\n+z\n"
        same = "diff --git a/f.py b/f.py\nindex 7..8\n@@ -40,2 +40,3 @@ def k():\n+x  =  1 \t\n"
        self.assertEqual(pid(f), pid(same))
        self.assertEqual(pid(f + g), pid(g + f))
        self.assertNotEqual(pid(f), pid(f.replace("x = 1", "x = 2")))
        self.assertNotEqual(pid(f), pid(f.replace("f.py", "h.py")))
        self.assertNotEqual(pid(f), pid(f + g))
        # end to end: key1_check accepts a pick equal to the reviewed commit but for whitespace / line numbers
        c, d = "c" * 40, "d" * 40
        self.assertEqual(rp.hands_decide(_hands_snap("t2_receipt_pos", commits={c: same, d: f})), "approve_key1")
        self.assertEqual(rp.hands_decide(_hands_snap("t2_receipt_pos", commits={c: f + g, d: f})), "salih")

    def test_git_fake_patch_id_keeps_binary_blob_ids(self) -> None:
        git = rp._git_fake(_hands_snap("t2_receipt_pos"))
        args = ["patch-id", "--stable"]
        for marker in ("Binary files a/f.bin and b/f.bin differ", "GIT binary patch"):
            with self.subTest(marker=marker):
                patch = f"diff --git a/f.bin b/f.bin\nindex 1111111..2222222\n{marker}\n"
                self.assertNotEqual(git(args, patch)[1], git(args, patch.replace("2222222", "3333333"))[1])

    def test_git_fake_patch_id_keeps_nonbreaking_space(self) -> None:
        git = rp._git_fake(_hands_snap("t2_receipt_pos"))
        args = ["patch-id", "--stable"]
        patch = "diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-x\n+x = 1\n"
        self.assertNotEqual(git(args, patch)[1], git(args, patch.replace("x =", "x\u00a0="))[1])

    def test_hands_decide_accepts_no_unsafe_set_and_rejects_nul_lane(self) -> None:
        snap = _hands_snap("t2_receipt_pos")
        self.assertEqual(rp.hands_decide(snap, None), "approve_key1")
        self.assertIsNone(rp.hands_decide({**snap, "lane": "invalid\0lane"}, None))

    def test_removing_one_tightening_fails_the_exam(self) -> None:
        for t in rp.TIGHTENINGS:
            tag = f"ALLOWLIST {t}"
            all_t = _t_rows(self.scenarios)
            without = [s for s in all_t if tag not in s["rules"]]
            pos_only = [s for s in all_t if not (tag in s["rules"] and s["id"].endswith("_neg"))]
            for kept in (without, pos_only):
                result = rp.exam(kept, rp.n6_decide)
                summary = result["summary"]
                self.assertEqual((summary["unsafe"], summary["passed"]), (0, False), t)
                self.assertEqual({k for k, v in summary["tightenings"].items() if v == "uncovered"}, {t})
                text = rp.exam_report(result)
                self.assertIn(f"{t} uncovered", text)
                self.assertIn("result: FAIL", text)
            with mock.patch.object(rp, "load_exam", lambda kept=without: kept), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rp.main(["--exam"]), 1, t)

    def test_a_malformed_fixture_is_refused(self) -> None:
        good = {"id": "x", "rules": ["A"], "scenario": "s", "note": "n",
                "expected_action": "inbox", "unsafe_if": ["approve_key1"]}
        for bad in ({"expected_action": "approve_key1"}, {"unsafe_if": ["typo"]},
                    {"expected_action": "typo"}, {"id": "y"}, {"note": None}):
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "x.json").write_text(json.dumps({**good, **bad}), encoding="utf-8")
                with self.assertRaises(ValueError, msg=str(bad)):
                    rp.load_exam(Path(tmp))
                good_only = Path(tmp, "x.json")
                good_only.write_text(json.dumps(good), encoding="utf-8")
                self.assertEqual(len(rp.load_exam(Path(tmp))), 1)


class ReasonCodeTableTest(unittest.TestCase):
    """EXAMCODES1: agreement per REASON_CODES_v1 code; stricter apart; unsafe per code."""

    @staticmethod
    def _rec(truth: str, why: str, verdict: str) -> dict:
        return {"conveyor": {"verdict": truth, "reason": why}, "verdict": verdict}

    def test_record_to_code(self) -> None:
        for truth, why, code in (
                ("pass", "lane finished PASS", "key1_green_chain"),
                ("block", "chained check CLREV-X BLOCKED", "chain_refused_finding"),
                ("block", "chained check CDX-X BLOCKED", "chain_refused_finding"),
                ("block", "chained check INT-X BLOCKED", "blocked"),
                ("block", "lane finished BLOCKED", "blocked"),
                ("excluded", "lane has not finished", "unknown")):
            self.assertEqual(rp.reason_code(self._rec(truth, why, "agree")), code, why)

    def test_kinds_split_stricter_from_disagree_and_unsafe(self) -> None:
        kind = lambda t, v: rp._kind(self._rec(t, "x", v))  # noqa: E731
        self.assertEqual([kind("pass", "disagree"), kind("block", "false_pass"), kind("pass", "agree"),
                          kind("pass", "unprovable")],
                         ["stricter", "unsafe", "agree", "not_comparable"])

    def test_table_has_every_code_and_reports_both_agreements(self) -> None:
        table = rp.code_table([("blocked", "agree")] * 19 + [("blocked", "stricter")] * 2
                              + [("overlap", "unsafe"), ("nonsense", "agree")])
        self.assertEqual(list(table), list(rp.CODES))
        row = table["blocked"]
        self.assertEqual((row["agreement_raw"], row["agreement_excl_stricter"]), (round(19 / 21, 4), 1.0))
        self.assertEqual((row["bar95_raw"], row["bar95_excl_stricter"]), (False, True))
        self.assertEqual(table["held"]["coverage"], "no coverage")  # cannot open
        self.assertFalse(table["held"]["bar95_raw"])
        self.assertEqual((table["overlap"]["unsafe"], table["overlap"]["bar95_excl_stricter"]), (1, False))
        self.assertEqual(table["unknown"]["agree"], 1)  # an unmapped code lands in unknown

    def test_replay_summary_and_report_carry_the_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp, "build", "conveyor", "events.jsonl")
            events.parent.mkdir(parents=True)
            events.write_text("", encoding="utf-8")
            result = rp.replay(Path(tmp))  # no history: every code is "no coverage"
        codes = result["summary"]["codes"]
        self.assertEqual(list(codes), list(rp.CODES))
        self.assertTrue(all(r["coverage"] == "no coverage" for r in codes.values()))
        json.dumps(result["summary"])
        text = rp.report(result)
        self.assertIn("## per reason code", text)
        self.assertIn("| key1_green_protected |", text)
        self.assertIn("no coverage", text)

    def test_exam_scenarios_map_to_codes_and_unsafe_is_counted_per_code(self) -> None:
        scenarios = rp.load_exam()
        self.assertEqual(rp.scenario_code({"rules": ["B2", "REASON_CODES B overlap"]}), "overlap")
        self.assertEqual(rp.scenario_code({"rules": ["B2", "D3"]}), "unknown")
        by = {s["id"]: s for s in scenarios}
        self.assertEqual(rp.scenario_code(by["a_held_pos"]), "held")
        self.assertEqual(rp.scenario_code(by["d12_unsafe_packet"]), "packet_too_large")
        sid = "a_key1_green_chain_neg"
        result = rp.exam(scenarios, ExamScenarioTest._real_on_t(
            lambda s: s["unsafe_if"][0] if s["id"] == sid else s["expected_action"]))
        self.assertEqual(result["codes"]["key1_green_chain"]["unsafe"], 1)
        self.assertEqual(result["codes"]["key1_green_chain"]["agree"], 1)
        self.assertEqual(sum(r["unsafe"] for r in result["codes"].values()), 1)
        text = rp.exam_report(result)
        self.assertIn("## per reason code", text)
        self.assertIn("| key1_green_chain | 1 | 0 | 0 | 1 |", text)
        self.assertEqual(rp.exam_report(rp.exam(scenarios, rp.n6_decide)).count("no coverage") > 0, True)

    def test_exam_stricter_is_an_inbox_where_the_design_acts(self) -> None:
        scenarios = [{"id": "x", "rules": ["A key1_green_chain"], "expected_action": "approve_key1",
                      "unsafe_if": ["wait"]}]
        row = rp.exam(scenarios, lambda s: "inbox")["codes"]["key1_green_chain"]
        self.assertEqual((row["stricter"], row["disagree"], row["agreement_excl_stricter"]), (1, 0, None))
        self.assertEqual(row["agreement_raw"], 0.0)

    def test_exam_inbox_is_stricter_only_where_the_design_approves(self) -> None:
        # EXAMCODES1FIX F1: any other designed answer answered with inbox is a plain disagree
        cases = (("wait", "inbox", ["push_main"], "disagree"), ("salih", "inbox", ["push_main"], "disagree"),
                 ("base_refresh", "inbox", ["push_main"], "disagree"),
                 ("approve_key1", "wait", ["push_main"], "disagree"),
                 ("approve_key1", "Inbox", ["push_main"], "disagree"),  # exact match, fail closed
                 ("approve_key1", "inbox", ["inbox"], "unsafe"),  # unsafe is never stricter
                 ("approve_key1", "inbox", ["push_main"], "stricter"))
        for expected, actual, unsafe_if, kind in cases:
            scenario = {"id": "x", "rules": ["A held"], "expected_action": expected, "unsafe_if": unsafe_if}
            row = rp.exam([scenario], lambda s, a=actual: a)["codes"]["held"]
            self.assertEqual({k for k in rp.KINDS if row[k]}, {kind}, (expected, actual))

    def test_bar_compares_exact_counts_not_the_rounded_rate(self) -> None:
        # EXAMCODES1FIX F2: round(94999/100000, 4) == 0.95 must not pass the bar
        def bars(agree: int, other: str, n: int) -> tuple[bool, bool]:
            row = rp.code_table([("blocked", "agree")] * agree + [("blocked", other)] * n)["blocked"]
            return row["bar95_raw"], row["bar95_excl_stricter"]
        self.assertEqual(bars(94999, "disagree", 5001), (False, False))
        self.assertEqual(rp.code_table([("blocked", "agree")] * 94999 + [("blocked", "disagree")] * 5001)
                         ["blocked"]["agreement_raw"], 0.95)  # display still rounds
        self.assertEqual(bars(95, "disagree", 5), (True, True))
        self.assertEqual(bars(19, "disagree", 1), (True, True))
        self.assertEqual(bars(18, "disagree", 1), (False, False))
        self.assertEqual(bars(94999, "stricter", 5001), (False, True))  # excl. denominator drops stricter
        self.assertEqual(bars(19, "unsafe", 1), (False, False))
        self.assertEqual(bars(0, "stricter", 3), (False, False))  # excl. denominator 0 -> no


if __name__ == "__main__":
    unittest.main()
