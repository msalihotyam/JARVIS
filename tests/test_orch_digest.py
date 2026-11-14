"""ORCHDIGEST1: scripts/orch_digest.py on fixture trees. No wall clock, no live data."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import orch_digest  # noqa: E402

NOW = "2026-11-14T12:00:00"
SINCE = "2026-11-14T00:00:00"


def slices(n6=3, improve=3, other=2):
    out = [{"id": f"N{i}", "title": "x", "n6": True, "approved": True} for i in range(n6)]
    out += [{"id": f"I{i}", "title": "[IYILESTIRME] x", "approved": True} for i in range(improve)]
    out += [{"id": f"O{i}", "title": "plain", "approved": True} for i in range(other)]
    return out


def tree(root, attention=(), hold=(), lanes=None, backlog=None, events=("",), write_lanes=True):
    c = Path(root) / "build" / "conveyor"
    (c / "hold").mkdir(parents=True)
    for h in hold:
        (c / "hold" / h).write_text("held", encoding="utf-8")
    status = {"usage_pct": 16, "pace": {"pace_line": 24.9, "band": "paced"}, "attention": list(attention), "backlog": []}
    (c / "status.json").write_text(json.dumps(status), encoding="utf-8")
    (c / "backlog.json").write_text(json.dumps({"slices": slices() if backlog is None else backlog}), encoding="utf-8")
    if write_lanes:
        lanes = lanes if lanes is not None else {
            "RUN1": {"model": "sonnet", "state": "working", "started_at": "2026-11-14T10:30:00"}}
        (c / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
    (c / "events.jsonl").write_text("\n".join(events), encoding="utf-8")


def run(root):
    assert orch_digest.main(["--root", str(root), "--now", NOW, "--since", SINCE]) == 0
    o = Path(root) / "build" / "orch"
    return o.joinpath("DIGEST.md").read_bytes(), json.loads(o.joinpath("digest.json").read_text(encoding="utf-8"))


class DigestTests(unittest.TestCase):
    def test_reason_codes(self):
        att = [{"id": "INT-CONV", "reason": "auto-approval refused: touches scripts/conveyor.py, manual key 1", "since": "2026-11-14T09:00:00"},
               {"id": "INT-X", "reason": "auto-approval refused: chain INT: orchestrator key 1", "since": "2026-11-14T09:00:00"}]
        ev = [json.dumps({"ts": "2026-11-14T08:00:00", "event": "chain_refused", "src": "SLC", "reason": "finding line"})]
        lanes = {"RUN1": {"model": "sonnet", "state": "working", "started_at": "2026-11-14T10:30:00"},
                 "INT-CONV": {"state": "done"}, "CLREV-INT-CONV": {"state": "done"}}
        with tempfile.TemporaryDirectory() as t:
            tree(t, att, ["HELD1"], lanes, events=ev)
            md, d = run(t)
        codes = {i["id"]: i["reason"] for i in d["items"]}
        self.assertEqual(codes, {"INT-CONV": "key1_green_protected", "INT-X": "key1_green_chain",
                                 "SLC": "chain_refused_finding", "HELD1": "held"})
        self.assertEqual(d["running"], [{"id": "RUN1", "model": "sonnet", "age": "1h30m"}])
        self.assertEqual({i["id"]: i["attempts"] for i in d["items"]}["INT-CONV"], 2)
        self.assertIn("1h30m", md.decode())

    def test_unreadable_inputs_become_unknown(self):
        with tempfile.TemporaryDirectory() as t:
            tree(t, events=["not json", json.dumps({"ts": NOW, "event": "x"})], write_lanes=False)
            _, d = run(t)
        unknown = {i["id"] for i in d["items"] if i["reason"] == "unknown"}
        self.assertEqual(unknown, {"input:lanes.json", "input:events.jsonl"})

    def test_ready_counts_split_tracks(self):
        bl = slices(2, 3, 4) + [{"id": "T", "title": "[N6] t", "approved": True}, {"id": "U", "title": "[N6] u"}]
        with tempfile.TemporaryDirectory() as t:
            tree(t, backlog=bl)
            _, d = run(t)
        self.assertEqual(d["ready"], {"n6": 3, "improve": 3, "other": 4})
        with tempfile.TemporaryDirectory() as t:
            tree(t, backlog=slices(1, 1, 0))
            _, d = run(t)
        self.assertEqual({i["reason"] for i in d["items"]}, {"queue_low_n6", "queue_low_improve"})

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as t:
            tree(t, hold=["H1"])
            first = run(t)[0]
            self.assertEqual(first, run(t)[0])

    def test_cap_with_counted_truncation(self):
        att = [{"id": f"S{i:03d}", "reason": "auto-approval refused: chain INT: orchestrator key 1 " + "y" * 60,
                "since": "2026-11-14T09:00:00"} for i in range(200)]
        with tempfile.TemporaryDirectory() as t:
            tree(t, att)
            md, d = run(t)
        text = md.decode()
        self.assertLessEqual(len(text), orch_digest.MAX_CHARS)
        self.assertEqual(len(d["items"]), 200)
        shown = text.count("\n- ")
        self.assertIn(f"truncated {200 - shown} more items", text)
        self.assertGreater(200 - shown, 0)


if __name__ == "__main__":
    unittest.main()
