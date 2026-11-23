import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import key1_check  # noqa: E402
import orchbot  # noqa: E402
import orchbot_replay  # noqa: E402

SHA = "a" * 40
REAL_VERIFIED = orchbot.Bot.verified


def ts(s):
    return orchbot.parse_ts(s)


class ReplayTest(unittest.TestCase):
    """BOTREPLAY2: the replay is deterministic, read-only on its inputs, and reports every verdict."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        evs = [{"ts": "2026-10-15T10:00:00", "event": "dispatch", "lane": "L1"},
               {"ts": "2026-10-15T10:30:00", "event": "finish", "lane": "L1", "status": "PASS"},
               {"ts": "2026-10-15T10:40:00", "event": "ci_pending", "lane": "L1", "sha": SHA},
               {"ts": "2026-10-15T11:00:00", "event": "ci_result", "lane": "L1", "sha": SHA,
                "result": "success"},
               {"ts": "2026-10-15T12:00:00", "event": "reader", "lane": "L2",
                "reading": {"kind": "needs_orchestrator", "summary": "y"}}]
        self.put("build/conveyor/events.jsonl", "".join(json.dumps(e) + "\n" for e in evs), "2026-10-15T09:00:00")
        self.put("build/reports/L1.md", f"STATUS: PASS\nCANDIDATE SHA: {SHA}\nFILES: jarvis/a.py\n",
                 "2026-10-15T10:30:00")
        # ALLOWT1T5FIX5: a verified receipt; the real verify shells out, so the replay gets a PASS verifier
        self.verify_calls = []
        patch = mock.patch.object(orchbot.Bot, "verified",
                                  lambda bot, lane, sha: self.verify_calls.append((lane, sha)) or (True, "PASS"))
        patch.start()
        self.addCleanup(patch.stop)
        self.key1(f"build/approvals/done/push-{SHA}-l1", "2026-10-15T11:10:00")
        self.put("build/conveyor/ack/L2", "", "2026-10-15T12:40:00")
        self.put("build/conveyor/hold/backlog.json.bak-1", {"slices": [{"id": "A1"}]}, "2026-10-15T09:00:00")
        self.put("build/conveyor/hold/backlog.json.bak-2", {"slices": [{"id": "A1"}, {"id": "L1FIX-1"}]},
                 "2026-10-15T11:20:00")
        self.put("build/orch/ORCH_NOTEBOOK.md", "- 2026-10-15 11:05 L1 key 1 written\n", "2026-10-15T11:05:00")

    def put(self, rel, data, at):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        os.utime(p, (ts(at),) * 2)

    def key1(self, rel, at, by="key1_check", reason=None, base=None, tail=""):
        """KEY1PATH1FIX4: a real write_key1 file for base or rel's name, moved to rel as a push (done/) or a
        forger (mismatched name) would; tail is text appended after the header the writer wrote."""
        m = orchbot.cv.APPROVAL_RE.fullmatch(base or Path(rel).name)
        p = orchbot.cv.write_key1(self.root / "keys", m.group(1), f"ci/{m.group(2)}", m.group(2), by,
                                  ("approval-for",), reason, now=0)
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(p, dest)
        with open(dest, "a", encoding="ascii") as f:
            f.write(tail)
        os.utime(dest, (ts(at),) * 2)
        return dest

    def test_load_actuals_takes_the_key1_side_from_the_validated_header_never_the_body(self):
        b, c, d, e, f = (x * 40 for x in "bcdef")
        at = "2026-10-15T11:30:00"
        self.key1(f"build/approvals/push-{b}-l2", at, by="conveyor", tail="approved by salih\n")
        self.key1(f"build/approvals/done/push-{c}-l3.20261015-113000", at, base=f"push-{c}-l3",
                  tail="auto-approved by the conveyor\n")
        self.key1(f"build/approvals/push-{d}-l4", at, by="manual", reason="approved by salih")
        self.key1(f"build/approvals/push-{e}-l5", at, by="manual", reason="approved under O42 pre-approval")
        self.key1(f"build/approvals/push-{f}-l6", at, by="manual", reason="checked by hand",
                  tail="auto-approved by the conveyor\n")
        # no key 1: a body-only file, a header under another sha's name, a .refused record
        self.put(f"build/approvals/push-{'1' * 40}-l7", "auto-approved by the conveyor\n", at)
        self.key1(f"build/approvals/push-{'2' * 40}-l8", at, base=f"push-{'3' * 40}-l8")
        self.key1(f"build/approvals/done/push-{'4' * 40}-l9.refused", at, base=f"push-{'4' * 40}-l9")
        acts, _ = orchbot_replay.load_actuals(self.root, ts("2026-10-15T00:00:00"), ts("2026-10-15T23:00:00"))
        got = {a["what"].split(":")[0]: (a["action"], a["side"]) for a in acts if a["action"] != "ack"
               and a["action"] != "queue_fix_slice"}
        self.assertEqual(got, {
            f"push-{SHA}-l1": ("write_key1", "orchestrator"),
            f"push-{b}-l2": ("write_key1", "conveyor"),
            f"push-{c}-l3.20261015-113000": ("write_key1", "orchestrator"),
            f"push-{d}-l4": ("ask_salih_key1", "orchestrator"),
            f"push-{e}-l5": ("write_key1", "conveyor"),
            f"push-{f}-l6": ("write_key1", "orchestrator")})
        self.assertTrue(all(a["what"].split(": ", 1)[1].startswith("KEY1 v1 ") for a in acts
                            if a["action"] in ("write_key1", "ask_salih_key1")))

    def snapshot(self):
        return {str(p): (p.stat().st_mtime, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in self.root.rglob("*") if p.is_file() and "replay" not in p.parts}

    def run_replay(self):
        argv = ["--root", str(self.root), "--since", "2026-10-15T00:00:00", "--until", "2026-10-15T23:00:00"]
        with open(os.devnull, "w") as null, mock.patch("sys.stdout", null):
            self.assertEqual(orchbot_replay.main(argv), 0)
        out = self.root / "build" / "bot" / "replay" / "20261015T000000_20261015T230000"
        return out, (out / "summary.json").read_bytes()

    def test_same_inputs_give_a_byte_identical_summary_and_inputs_stay_untouched(self):
        before = self.snapshot()
        out, first = self.run_replay()
        _, second = self.run_replay()
        self.assertEqual(first, second)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["decisions.jsonl", "report.md", "summary.json"])
        self.assertEqual(list((self.root / "build" / "bot").iterdir()), [self.root / "build" / "bot" / "replay"])

    def test_summary_counts_each_action_and_report_lists_misses(self):
        out, body = self.run_replay()
        s = json.loads(body)
        self.assertEqual(set(s), {"since", "until", "events", "actuals", "decisions", "would_do", "per_action",
                                  "verdicts"})
        self.assertEqual(s["events"], 5)
        self.assertEqual(s["per_action"]["write_key1"]["agree"], 1)
        self.assertIn(("L1", SHA), self.verify_calls)
        self.assertEqual(s["per_action"]["ack"]["agree"], 0)  # BOTFIX3C: no attention item -> excluded
        self.assertEqual(s["per_action"]["queue_fix_slice"], {"n": 1, "agree": 0, "disagree": 0, "unmatched": 0,
                                                              "missed": 1})
        for row in s["per_action"].values():
            self.assertEqual(row["n"], sum(row[k] for k in orchbot_replay.KINDS))
        md = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("## disagreements and misses", md)
        self.assertIn("L1FIX-1", md)

    def test_without_a_verified_receipt_the_replay_asks_salih_not_key1(self):
        self.put("build/reports/L1.md", "STATUS: PASS\nFILES: jarvis/a.py\n", "2026-10-15T10:30:00")
        with mock.patch.object(orchbot.Bot, "verified", REAL_VERIFIED), \
                mock.patch.object(key1_check, "verify", side_effect=AssertionError("verify ran")) as verify:
            out, body = self.run_replay()
        verify.assert_not_called()  # no CANDIDATE SHA line: refused before any verify/gh call
        per = json.loads(body)["per_action"]
        self.assertNotIn("write_key1", per)
        self.assertEqual(per["ask_salih_key1"]["n"], 1)
        self.assertIn("ALLOW-T2", (out / "decisions.jsonl").read_text(encoding="utf-8"))

    def test_odd_inputs_never_crash_the_replay(self):
        with open(self.root / "build/conveyor/events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-10-15T12:10:00", "event": "dispatch", "lane": ["X"]}) + "\n")
        self.put("build/conveyor/hold/backlog.json.bak-3", "{not json", "2026-10-15T11:30:00")
        self.put("build/conveyor/hold/backlog.json.bak-4", "[1]", "2026-10-15T11:31:00")
        self.assertEqual(json.loads(self.run_replay()[1])["events"], 6)
        (self.root / "build/conveyor/events.jsonl").unlink()
        self.assertEqual(json.loads(self.run_replay()[1])["events"], 0)


LOGS = ("events.jsonl", "ORCH_NOTEBOOK.md")


def old_load_events(conv, since, until):
    """The pre-REPLAYSTREAM whole-file reader, kept as the reference the streamed one must match."""
    out = []
    try:
        lines = (conv / "events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        at = orchbot.parse_ts(ev.get("ts")) if isinstance(ev, dict) else None
        if at is not None and since <= at <= until:
            out.append((at, ev))
    return out


class StreamTest(unittest.TestCase):
    """REPLAYSTREAM (CDXINT65 F6): logs are streamed line by line, never read whole, with unchanged results."""

    setUp, put, key1, run_replay = ReplayTest.setUp, ReplayTest.put, ReplayTest.key1, ReplayTest.run_replay

    def no_whole_reads(self):
        real_text, real_bytes = Path.read_text, Path.read_bytes

        def guard(real):
            def read(p, *a, **k):
                if p.name in LOGS:
                    raise AssertionError(f"whole-file read of {p.name}")
                return real(p, *a, **k)
            return read
        return mock.patch.multiple(Path, read_text=guard(real_text), read_bytes=guard(real_bytes))

    def test_replay_never_reads_a_log_whole_and_gives_the_same_summary(self):
        _, plain = self.run_replay()
        with self.no_whole_reads():
            _, guarded = self.run_replay()
        self.assertEqual(guarded, plain)
        conv = self.root / "build" / "conveyor"
        self.assertEqual(orchbot_replay.load_events(conv, 0, float("inf")), old_load_events(conv, 0, float("inf")))
        self.assertEqual(json.loads(guarded)["events"], 5)

    def test_line_variants_match_the_old_reader(self):
        good = [json.dumps({"ts": f"2026-10-15T1{i}:00:00", "event": "dispatch", "lane": f"L{i}"}) for i in range(6)]
        raw = (good[0] + "\n" + good[1] + "\r\n" + good[2] + "\r" + "\n\n   \n{not json\n[1, 2]\n\"s\"\n"
               + json.dumps({"event": "no ts"}) + "\n" + json.dumps({"ts": "2026-10-17T10:00:00"}) + "\n"
               + good[3] + "\n").encode() + b"\xff\xfe garbage\n" + good[4].encode() + b"\n" + good[5].encode()
        conv = self.root / "build" / "conveyor"
        (conv / "events.jsonl").write_bytes(raw)
        since, until = ts("2026-10-15T00:00:00"), ts("2026-10-15T23:00:00")
        new = orchbot_replay.load_events(conv, since, until)
        self.assertEqual(new, old_load_events(conv, since, until))
        self.assertEqual([ev["lane"] for _, ev in new], [f"L{i}" for i in range(6)])
        with self.no_whole_reads():
            self.assertEqual(orchbot_replay.load_events(conv, since, until), new)

    def test_unicode_line_separators_inside_a_string_no_longer_drop_the_event(self):
        conv = self.root / "build" / "conveyor"
        evs = [{"ts": "2026-10-15T10:00:00", "event": "reader", "lane": "L1", "note": f"a{sep}b"}
               for sep in (" ", " ", "\x85")]  # json.dumps escapes \x0c/\x1c-\x1e itself
        (conv / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs),
                                           encoding="utf-8")
        got = orchbot_replay.load_events(conv, 0, float("inf"))
        self.assertEqual([ev for _, ev in got], evs)  # splitlines() split these lines and lost every event
        self.assertEqual(old_load_events(conv, 0, float("inf")), [])

    def test_missing_logs_give_empty_results_without_reading_whole(self):
        (self.root / "build/conveyor/events.jsonl").unlink()
        (self.root / "build/orch/ORCH_NOTEBOOK.md").unlink()
        with self.no_whole_reads():
            self.assertEqual(orchbot_replay.load_events(self.root / "build" / "conveyor", 0, float("inf")), [])
            self.assertEqual(orchbot_replay.notebook(self.root), [])

    def test_notebook_streams_crlf_lines_and_keeps_the_line_text(self):
        p = self.root / "build/orch/ORCH_NOTEBOOK.md"
        p.write_bytes(b"# notes\r\n- 2026-10-15 11:05 L1 key 1 written\r\n- 2026-10-15 11:06: L2 ok\n- bad line\n"
                      b"- 2026-13-15 11:07 bad date\n- 2026-10-15 11:08 last")
        with self.no_whole_reads():
            got = orchbot_replay.notebook(self.root)
        self.assertEqual([line for _, line in got], ["- 2026-10-15 11:05 L1 key 1 written",
                                                      "- 2026-10-15 11:06: L2 ok", "- 2026-10-15 11:08 last"])
        self.assertEqual(got[0][0], ts("2026-10-15T11:05:00"))


if __name__ == "__main__":
    unittest.main()
