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


def tree(root, attention=(), hold=(), lanes=None, backlog=None, events=("",), write_lanes=True, view=None):
    """view: status.json backlog [{"id", "state"}]; default = every backlog slice in its ready state."""
    c = Path(root) / "build" / "conveyor"
    (c / "hold").mkdir(parents=True)
    for h in hold:
        (c / "hold" / h).write_text("held", encoding="utf-8")
    backlog = slices() if backlog is None else backlog
    if view is None:
        view = [{"id": s["id"], "state": "n6" if s.get("n6") else "ready"} for s in backlog]
    status = {"usage_pct": 16, "pace": {"pace_line": 24.9, "band": "paced"}, "attention": list(attention), "backlog": view}
    (c / "status.json").write_text(json.dumps(status), encoding="utf-8")
    (c / "backlog.json").write_text(json.dumps({"slices": backlog}), encoding="utf-8")
    if write_lanes:
        lanes = lanes if lanes is not None else {
            "RUN1": {"model": "sonnet", "state": "working", "started_at": "2026-11-14T10:30:00"}}
        (c / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
    (c / "events.jsonl").write_text("\n".join(events), encoding="utf-8")


def run(root, since=SINCE):
    assert orch_digest.main(["--root", str(root), "--now", NOW, "--since", since]) == 0
    o = Path(root) / "build" / "orch"
    return o.joinpath("DIGEST.md").read_bytes(), json.loads(o.joinpath("digest.json").read_text(encoding="utf-8"))


class DigestTests(unittest.TestCase):
    def test_reason_codes(self):
        att = [{"id": "INT-CONV", "reason": "auto-approval refused: touches scripts/conveyor.py, manual key 1", "since": "2026-11-14T09:00:00"},
               {"id": "INT-X", "reason": "auto-approval refused: chain INT: orchestrator key 1", "since": "2026-11-14T09:00:00"}]
        ev = [json.dumps({"ts": "2026-11-14T08:00:00", "event": "chain_refused", "src": "SLC", "reason": "finding line"})]
        lanes = {"RUN1": {"model": "sonnet", "state": "working", "started_at": "2026-11-14T10:30:00"},
                 "INT-CONV": {"state": "done"}, "CLREV-INT-CONV": {"state": "done"}}
        bl = slices()
        view = [{"id": s["id"], "state": "n6" if s.get("n6") else "ready"} for s in bl] + [
            {"id": "HELD1", "state": "held: BLOCKED by N0"}, {"id": "T2", "state": "needs_t2_approval"}]
        with tempfile.TemporaryDirectory() as t:
            tree(t, att, (), lanes, bl, events=ev, view=view)
            md, d = run(t)
        codes = {i["id"]: i["reason"] for i in d["items"]}
        self.assertEqual(codes, {"INT-CONV": "key1_green_protected", "INT-X": "key1_green_chain",
                                 "SLC": "chain_refused_finding", "HELD1": "held", "T2": "needs_t2_approval"})
        self.assertEqual(d["running"], [{"id": "RUN1", "model": "sonnet", "age": "1h30m"}])
        self.assertEqual({i["id"]: i["attempts"] for i in d["items"]}["INT-CONV"], 2)
        self.assertIn("1h30m", md.decode())

    def test_archive_hold_files_are_not_held(self):
        names = ["X.bak", "Y.orig", "PAUSE-Z", "A.pre-cl9", "B.superseded", "C.parked", "NOTE.md", "PLAIN"]
        with tempfile.TemporaryDirectory() as t:
            tree(t, hold=names)
            (Path(t) / "build" / "conveyor" / "hold" / "sub").mkdir()
            _, d = run(t)
        self.assertEqual([i for i in d["items"] if i["reason"] == "held"], [])

    def test_unreadable_inputs_become_unknown(self):
        with tempfile.TemporaryDirectory() as t:
            tree(t, events=["not json", json.dumps({"ts": NOW, "event": "x"})], write_lanes=False)
            _, d = run(t)
        unknown = {i["id"] for i in d["items"] if i["reason"] == "unknown"}
        self.assertEqual(unknown, {"input:lanes.json", "input:events.jsonl"})

    def test_ready_counts_split_tracks(self):
        bl = slices(2, 3, 4) + [{"id": "T", "title": "[N6] t", "approved": True}, {"id": "U", "title": "[N6] u"}]
        view = [{"id": s["id"], "state": "n6" if s.get("n6") else "ready"} for s in bl[:-1]] + [{"id": "U", "state": "dead"}]
        with tempfile.TemporaryDirectory() as t:
            tree(t, backlog=bl, view=view)
            _, d = run(t)
        self.assertEqual(d["ready"], {"n6": 3, "improve": 3, "other": 4})
        with tempfile.TemporaryDirectory() as t:
            tree(t, backlog=slices(1, 1, 0))
            _, d = run(t)
        self.assertEqual({i["reason"] for i in d["items"]}, {"queue_low_n6", "queue_low_improve"})

    def test_ready_only_from_view_states_when_every_slice_is_present(self):
        bl = slices(3, 3, 2)
        states = ["n6", "n6", "PASS", "ready", "ready", "BLOCKED", "waiting_deps", "ready"]
        view = [{"id": s["id"], "state": st} for s, st in zip(bl, states)] + [{"error": "bad slice"}]
        with tempfile.TemporaryDirectory() as t:
            tree(t, backlog=bl, view=view)
            _, d = run(t)
        self.assertEqual(d["ready"], {"n6": 2, "improve": 2, "other": 1})
        self.assertEqual([i for i in d["items"] if i["reason"].startswith("queue_low")], [])

    def test_mixed_aware_and_naive_times(self):
        lanes = {"A": {"model": "m", "state": "working", "started_at": "2026-11-14T10:00:00+00:00"},
                 "B": {"state": "done", "updated_at": "2026-11-14T11:00:00Z"},
                 "C": {"state": "done", "updated_at": 5}}
        ev = [json.dumps({"ts": "2026-11-14T08:00:00+05:00", "event": "chain_refused", "src": "S"})]
        att = [{"id": "Z", "reason": "blocked", "since": "2026-11-14T09:00:00-03:00"}]
        with tempfile.TemporaryDirectory() as t:
            tree(t, att, lanes=lanes, events=ev)
            _, d = run(t, since="2026-11-14T00:00:00+00:00")
        self.assertEqual([r["id"] for r in d["running"]], ["A"])
        self.assertEqual([f["id"] for f in d["finished"]], ["B"])

    def test_malformed_containers_do_not_crash(self):
        with tempfile.TemporaryDirectory() as t:
            tree(t, events=["[" * 100000 + "]" * 100000])
            c = Path(t) / "build" / "conveyor"
            (c / "backlog.json").write_text(json.dumps({"slices": None}), encoding="utf-8")
            (c / "status.json").write_text(json.dumps({"attention": 5, "backlog": [{"id": "\ud800x", "state": "held: y"}]}),
                                           encoding="utf-8")
            _, d = run(t)
        self.assertIn("input:events.jsonl", {i["id"] for i in d["items"] if i["reason"] == "unknown"})
        self.assertEqual(sum(i["reason"] == "held" for i in d["items"]), 1)

    def test_non_string_ids(self):
        att = [{"id": 7, "reason": "blocked"}]
        ev = [json.dumps({"ts": "2026-11-14T08:00:00", "event": "chain_refused", "src": 8})]
        bl = slices()
        view = [{"id": s["id"], "state": "n6" if s.get("n6") else "ready"} for s in bl] + [{"id": 9, "state": "held: x"}]
        with tempfile.TemporaryDirectory() as t:
            tree(t, att, backlog=bl, events=ev, view=view)
            _, d = run(t)
        self.assertEqual({i["id"]: i["reason"] for i in d["items"]}, {"7": "blocked", "8": "chain_refused_finding", "9": "held"})

    def test_bad_since_fails_loudly(self):
        with tempfile.TemporaryDirectory() as t:
            tree(t)
            with self.assertRaises(SystemExit) as e:
                orch_digest.main(["--root", t, "--now", NOW, "--since", "yesterday"])
            self.assertNotEqual(e.exception.code, 0)
            self.assertFalse((Path(t) / "build" / "orch").exists())

    def test_long_header_stays_under_cap(self):
        lanes = {f"RUN{i:03d}-" + "x" * 300: {"model": "m" * 50, "state": "working", "started_at": "2026-11-14T10:00:00"}
                 for i in range(60)}
        lanes.update({f"FIN{i:03d}-" + "y" * 300: {"state": "done", "updated_at": "2026-11-14T11:00:00"} for i in range(60)})
        with tempfile.TemporaryDirectory() as t:
            tree(t, lanes=lanes)
            ap = Path(t) / "build" / "approvals"
            ap.mkdir()
            for i in range(30):
                (ap / (f"A{i:02d}" + "z" * 200)).write_text("", encoding="utf-8")
            md, d = run(t)
        self.assertLessEqual(len(md.decode()), orch_digest.MAX_CHARS)
        self.assertEqual(len(d["running"]), 60)

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
