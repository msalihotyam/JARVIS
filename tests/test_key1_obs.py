"""KEY1OBS1: scripts/key1_obs.py classifies bot vs real key 1 (K2/K4), fails closed, writes nothing."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import key1_obs  # noqa: E402

A, B, C, D, E = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
NOW = "2026-11-24T12:00:00Z"


def bot(at, act, lane, sha=None, live=False):
    d = {"ts": at, "rule": "O26", "target": lane, "detail": f"build/approvals/push-{sha}-{lane}" if sha else "x"}
    d.update({"mode": "live", "did": act} if live else {"would_do": act, "saw": "ci green"})
    return d


def key1(sha, lane, by, at):
    return f"KEY1 v1 sha={sha} ci=ci/{lane.lower()} lane={lane} by={by} checks=ci at={at}\n"


class Key1ObsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "build" / "bot").mkdir(parents=True)
        self.done = self.root / "build" / "approvals" / "done"
        self.done.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, decisions, files, raw=""):
        with open(self.root / "build" / "bot" / "decisions.jsonl", "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(d) + "\n" for d in decisions) + raw)
        for name, text in files.items():
            (self.done / name).write_text(text, encoding="ascii")

    def run_obs(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = key1_obs.main(["--root", str(self.root), "--now", NOW, *extra])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*"))

    def test_classification_and_k2(self):
        self.write([
            bot("2026-11-24T08:00:00Z", "write_key1", "INT-X", A),        # agree
            bot("2026-11-24T08:00:00Z", "write_key1", "INT-Y", B),        # unsafe: no real key 1 within 2 h
            bot("2026-11-24T08:00:00Z", "write_key1", "INT-Z", C),        # unsafe: real refusal
            bot("2026-11-24T08:00:00Z", "ask_salih_key1", "INT-W"),      # stricter (matched by lane)
            bot("2026-11-24T11:30:00Z", "write_key1", "INT-P", E),        # pending (< 2 h, nothing yet)
            {"ts": "2026-11-24T08:00:00Z", "would_do": "ack", "target": "x"},
            {"ts": "2026-11-24T08:00:00Z", "verdict": "agree", "bot": None, "orchestrator": None},
        ], {
            f"push-{A}-int-x": key1(A, "INT-X", "key1_check", "2026-11-24T08:30:00Z"),
            f"push-{B}-int-y": key1(B, "INT-Y", "key1_check", "2026-11-24T11:00:00Z"),  # 3 h later: too late
            f"push-{C}-int-z.refused": key1(C, "INT-Z", "key1_check", "2026-11-24T08:10:00Z"),
            f"push-{D}-int-w": key1(D, "INT-W", "manual reason=ok", "2026-11-24T08:20:00Z"),
        })
        code, out, _ = self.run_obs()
        self.assertEqual(code, 0, out)
        self.assertIn("agree 1, unsafe 2, bot-stricter 1, unmatched 1, pending 1", out)  # B's late key 1 unmatched
        self.assertIn("K2: 1/10 matching, unsafe 2", out)
        self.assertIn(f"INT-X {A} | real key1 by=key1_check at=2026-11-24T08:30:00Z AGREE", out)
        self.assertIn(f"INT-Y {B} | no real key1 UNSAFE", out)
        self.assertIn(f"INT-Z {C} | no real key1 + refusal push-{C}-int-z.refused UNSAFE", out)
        self.assertIn("ask_salih_key1 INT-W - | real key1 by=manual at=2026-11-24T08:20:00Z STRICTER", out)
        self.assertIn(f"real key1 by=key1_check INT-Y {B} | no bot decision UNMATCHED", out)
        self.assertIn("K4: 0 hands key1 audited, mismatches 0", out)

    def test_since_filters(self):
        self.write([bot("2026-11-20T08:00:00Z", "write_key1", "INT-Y", B)], {})
        code, out, _ = self.run_obs("--since", "2026-11-21T00:00:00Z")
        self.assertEqual(code, 0)
        self.assertIn("K2: 0/10 matching, unsafe 0", out)

    def test_hands_counted_for_k4(self):
        self.write([bot("2026-11-24T08:00:00Z", "write_key1", "INT-X", A, live=True)], {
            f"push-{A}-int-x": key1(A, "INT-X", "hands", "2026-11-24T08:00:05Z"),
            f"push-{B}-int-y": key1(B, "INT-Y", "hands", "2026-11-24T09:00:00Z"),  # no live bot write: mismatch
        })
        code, out, _ = self.run_obs()
        self.assertEqual(code, 0, out)
        self.assertIn("K4: 2 hands key1 audited, mismatches 1", out)
        self.assertIn("K2: 0/10 matching, unsafe 0", out)  # live writes and hands files never count for K2
        self.assertIn("unmatched 0", out)

    def test_malformed_line_exits_2(self):
        self.write([bot("2026-11-24T08:00:00Z", "write_key1", "INT-X", A)], {}, raw="{not json\n")
        code, out, err = self.run_obs()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("line 2", err)

    def test_malformed_approval_and_missing_input_exit_2(self):
        for bad in ("KEY1 v1 garbage\n", "key1 V1 sha=x\n", "﻿KEY1 v1 sha=zz\n", f" KEY1 v1 sha={A} lane=X\n",
                    "KEY1v1 sha=x\n", "KEY1  v1 sha=x\n", "KEY1 v1\n"):
            with self.subTest(bad=bad):
                (self.done / f"push-{A}-int-x").write_text(bad, encoding="utf-8")
                self.write([], {})
                self.assertEqual(self.run_obs()[0], 2)
        os.remove(self.root / "build" / "bot" / "decisions.jsonl")
        os.remove(self.done / f"push-{A}-int-x")
        self.assertEqual(self.run_obs()[0], 2)

    def test_legacy_free_text_approval_skipped(self):
        files = {f"push-{A}-int-x": key1(A, "INT-X", "key1_check", "2026-11-24T08:30:00Z")}
        self.write([bot("2026-11-24T08:00:00Z", "write_key1", "INT-X", A)], files)
        code, base, _ = self.run_obs()
        self.assertEqual(code, 0, base)
        for i, text in enumerate(("key 1 by the orchestrator (O26, CONVSAFE) ok\n", "garbage\n", "", "\n",
                                  "key1 INT-X (protected: conveyor.py): ff from c450cc2\n", "KEY1 v10 x\n")):
            (self.done / f"push-{B}-int{i}").write_text(text, encoding="ascii")
        code, out, err = self.run_obs()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, base)  # legacy files neither match nor count

    def test_naive_local_decision_ts_matches_z_approval(self):
        # decisions.jsonl ts is naive local time (conveyor.iso); the approval at= is UTC Z: same instant must agree
        utc = key1_obs.ts("2026-11-24T08:00:00Z")
        local = datetime.fromtimestamp(utc).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(key1_obs.ts(local), utc)
        self.assertEqual(key1_obs.ts(local + ".5"), utc + 0.5)
        self.assertEqual(key1_obs.ts("2026-11-24T11:00:00+03:00"), utc)
        self.assertEqual(key1_obs.ts(" 2026-11-24t08:00:00z "), utc)
        self.assertEqual(key1_obs.load_approvals(self.done), [])
        (self.done / f"push-{B}-int-b").write_text("﻿" + key1(B, "INT-B", "key1_check", "2026-11-24T08:00:00Z"),
                                                   encoding="utf-8")
        self.assertEqual([a["sha"] for a in key1_obs.load_approvals(self.done)], [B])  # BOM before a valid header
        os.remove(self.done / f"push-{B}-int-b")
        self.write([bot(local, "write_key1", "INT-X", A)],
                   {f"push-{A}-int-x": key1(A, "INT-X", "key1_check", "2026-11-24T08:00:05Z")})
        code, out, _ = self.run_obs()
        self.assertEqual(code, 0, out)
        self.assertIn("K2: 1/10 matching, unsafe 0", out)
        self.assertIn("unmatched 0", out)

    def test_lane_match_ignores_case(self):
        self.write([bot("2026-11-24T08:00:00Z", "ask_salih_key1", "int-w")],
                   {f"push-{D}-int-w": key1(D, "INT-W", "key1_check", "2026-11-24T08:20:00Z")})
        code, out, _ = self.run_obs()
        self.assertEqual(code, 0, out)
        self.assertIn("bot-stricter 1, unmatched 0", out)

    def test_nothing_written(self):
        self.write([bot("2026-11-24T08:00:00Z", "write_key1", "INT-X", A)],
                   {f"push-{A}-int-x": key1(A, "INT-X", "key1_check", "2026-11-24T08:30:00Z")})
        before = self.snapshot()
        self.assertEqual(self.run_obs()[0], 0)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
