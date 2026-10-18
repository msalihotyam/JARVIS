"""scripts/usage_ledger.py on synthetic transcripts in a temp dir; never the real ~/.claude."""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "usage_ledger", Path(__file__).resolve().parents[1] / "scripts" / "usage_ledger.py")
ul = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ul)


def A(mid, out, sid="s1", **u):
    return {"type": "assistant", "sessionId": sid, "timestamp": "2026-10-08T21:30:00Z",
            "message": {"id": mid, "model": "m1",
                        "usage": {"input_tokens": 1, "output_tokens": out, **u}}}


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=ul.IST)


def body(sid, n, seed):
    """n transcript lines of one session: users, titles, repeated assistant ids, junk, CRLF."""
    out = []
    for i in range(seed, seed + n):
        ts = f"2026-10-{8 + i % 3:02d}T{i % 24:02d}:{i % 60:02d}:00Z"
        k = i % 7
        if k == 0:
            x = {"type": "user", "sessionId": sid, "timestamp": ts,
                 "message": {"content": f"@jarvis lane=L{i % 4} çğü {i}" if i % 3 else f"plain {i}"}}
        elif k == 1:
            x = {"type": "custom-title", "sessionId": sid, "customTitle": f"title {i}"}
        elif k == 2:
            x = None
        else:
            x = {**A(f"{sid}-{i // 5}", i % 11, sid=sid, cache_read_input_tokens=i), "timestamp": ts}
        line = "not json {" if x is None else json.dumps(x, ensure_ascii=False)
        out.append(line + ("\r\n" if i % 5 == 0 else "\n"))
    return "".join(out).encode("utf-8")


def lines(*xs):
    return "".join(json.dumps(x) + "\n" for x in xs).encode("utf-8")


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.out = self.d / "o" / "usage.json"
        self.proj = self.d / "c--Users-realk-Masa-st--JARVIS"
        self.proj.mkdir()
        self.state = self.out.with_name("usage_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def transcript(self, name, lines):
        p = self.proj / name
        p.write_text("\n".join(json.dumps(x) if not isinstance(x, str) else x for x in lines),
                     encoding="utf-8")
        return p

    def run_main(self):
        return ul.main(self.d, self.out, now=lambda: NOW, clock=lambda: 0)

    def check(self):
        """One incremental run; its usage.json and state equal a full parse (no state) of the
        same files from the same usage.json. Returns the lines the incremental run read."""
        before = self.out.read_bytes() if self.out.exists() else None
        with mock.patch.object(ul, "_feed", wraps=ul._feed) as feed:
            self.run_main()
        inc, inc_state = self.out.read_bytes(), self.state.read_bytes()
        if before is None:
            self.out.unlink()
        else:
            self.out.write_bytes(before)
        self.state.unlink()
        self.run_main()
        self.assertEqual(self.out.read_bytes(), inc)
        self.assertEqual(self.state.read_bytes(), inc_state)
        return feed.call_count

    def corpus(self):
        wt = self.d / "c--Users-realk-Masa-st--JARVIS--claude-worktrees-X"
        files = [(self.proj / "a.jsonl", "s1", 3000), (self.proj / "s1" / "subagents" / "agent-1.jsonl", "s1", 2000),
                 (self.proj / "b.jsonl", "s2", 3000), (wt / "c.jsonl", "s3", 2000)]
        for i, (p, sid, n) in enumerate(files):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body(sid, n, i * 7919))
        return [p for p, _, _ in files]

    def test_dupes_missing_usage_header_day(self):
        user = {"type": "user", "sessionId": "s1", "timestamp": "2026-10-08T21:00:00Z",
                "message": {"content": "@jarvis lane=ABC1 model=x\nsecret body"}}
        nousage = {"type": "assistant", "sessionId": "s1", "timestamp": "2026-10-08T21:40:00Z",
                   "message": {"id": "m9", "model": "m1"}}
        self.transcript("s1.jsonl", [user, A("m1", 5, cache_read_input_tokens=100),
                                     A("m1", 7, cache_read_input_tokens=100), nousage, "not json"])
        r = self.run_main()
        s = r["sessions"][0]
        self.assertEqual((s["lane"], s["messages"], s["output_tokens"], s["total_tokens"]),
                         ("ABC1", 1, 7, 108))
        self.assertEqual(s["models"], ["m1"])
        self.assertIn("2026-10-09", r["by_day"])  # 21:00Z = 00:00 Istanbul next day
        self.assertEqual(r["by_lane"]["ABC1"]["total_tokens"], 108)
        self.assertNotIn("secret", self.out.read_text(encoding="utf-8"))

    def test_title_fallback_truncates(self):
        t = {"type": "custom-title", "sessionId": "s1", "customTitle": "x" * 80}
        self.transcript("s1.jsonl", [t, A("m1", 1)])
        self.assertEqual(len(self.run_main()["sessions"][0]["lane"]), 60)

    def test_session_kept_after_its_transcript_is_gone(self):
        gone = self.transcript("s1.jsonl", [A("m1", 5)])
        self.transcript("s2.jsonl", [A("m2", 3, sid="s2")])
        self.run_main()
        gone.unlink()  # the conveyor removed the lane session
        r = self.run_main()
        self.assertEqual(sorted(s["session_id"] for s in r["sessions"]), ["s1", "s2"])
        self.assertEqual(r["by_day"]["2026-10-09"]["sessions"], 2)
        self.assertEqual(r["by_lane"]["s1"]["output_tokens"], 5)

    def test_grown_transcript_updates_smaller_never_replaces(self):
        p = self.transcript("s1.jsonl", [A("m1", 5)])
        self.run_main()
        p.write_text(json.dumps(A("m1", 5)) + "\n" + json.dumps(A("m2", 4)), encoding="utf-8")
        self.assertEqual(self.run_main()["sessions"][0]["output_tokens"], 9)
        p.write_text(json.dumps(A("m1", 5)), encoding="utf-8")  # partly gone
        self.assertEqual(self.run_main()["sessions"][0]["output_tokens"], 9)

    def test_unreadable_ledger_is_never_overwritten(self):
        self.transcript("s1.jsonl", [A("m1", 5)])
        self.out.parent.mkdir()
        for bad in ("{not json", '{"sessions": "x"}', "[]"):
            self.out.write_text(bad, encoding="utf-8")
            with self.assertRaises((ValueError, KeyError, TypeError)):
                self.run_main()
            self.assertEqual(self.out.read_text(encoding="utf-8"), bad)

    def test_incremental_equals_full_on_10k_lines(self):
        files = self.corpus()
        self.assertEqual(sum(len(f.read_bytes().splitlines()) for f in files), 10000)
        self.assertEqual(self.check(), 10000)
        self.assertEqual(self.check(), 0)  # nothing new: nothing read
        with open(files[0], "ab") as fh:
            fh.write(body("s1", 40, 3) + b'{"type": "assistant", "sessionId": "s1"')
        self.assertEqual(self.check(), 41)  # the new lines and the unfinished one
        self.assertEqual(self.check(), 1)  # an unfinished line is re-read until it ends
        with open(files[0], "ab") as fh:
            fh.write(b', "timestamp": "2026-10-09T01:00:00Z", "message": {"id": "late", "usage": {"output_tokens": 9}}}\n')
        self.assertEqual(self.check(), 1)
        self.assertIn('"late"', self.state.read_text(encoding="utf-8"))
        (self.proj / "s1" / "subagents" / "agent-2.jsonl").write_bytes(body("s1", 30, 5))
        self.assertEqual(self.check(), 30)  # a new file

    def test_changed_shrunk_or_rewritten_file_is_parsed_whole(self):
        p = self.proj / "a.jsonl"
        rows = [A(f"m{i}", i) for i in range(10)]
        p.write_bytes(lines(*rows))
        self.check()
        rows[9], rows[10:] = A("m9", 99), [A("m10", 1)]  # grown, but a line before the offset changed
        p.write_bytes(lines(*rows))
        self.assertEqual(self.check(), 11)
        p.write_bytes(lines(*rows[:5]))  # shrunk
        self.assertEqual(self.check(), 5)
        st = p.stat()
        p.write_bytes(lines(*rows[:4], A("m4", 7)))  # same size, new bytes, new mtime
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        self.assertEqual(p.stat().st_size, st.st_size)
        self.assertEqual(self.check(), 5)

    def test_vanished_file_is_skipped(self):
        a = self.proj / "a.jsonl"
        a.write_bytes(lines(A("m1", 5)))
        (self.proj / "b.jsonl").write_bytes(lines(A("m2", 3, sid="s2")))
        real = ul._scan

        def scan(path, prev):
            if path.name == "a.jsonl":
                raise FileNotFoundError(path)  # gone between listing and reading
            return real(path, prev)
        with mock.patch.object(ul, "_scan", scan):
            r = self.run_main()
        self.assertEqual([s["session_id"] for s in r["sessions"]], ["s2"])
        self.assertNotIn(str(a), json.loads(self.state.read_text(encoding="utf-8")))
        self.assertEqual(self.check(), 1)  # back: parsed whole
        a.unlink()
        self.assertEqual(self.check(), 0)
        self.assertEqual(sorted(s["session_id"] for s in json.loads(self.out.read_text(encoding="utf-8"))["sessions"]),
                         ["s1", "s2"])

    def test_unreadable_file_fails_the_run(self):
        a = self.proj / "a.jsonl"
        a.write_bytes(lines(A("m1", 5)))
        self.run_main()
        before = self.out.read_bytes()

        def scan(path, prev):
            raise PermissionError(path)  # locked or denied: its new tokens would go uncounted
        with mock.patch.object(ul, "_scan", scan), self.assertRaises(PermissionError):
            self.run_main()
        self.assertEqual(self.out.read_bytes(), before)  # no fresh generated_at over a stale count

    def test_bad_state_parses_whole(self):
        p = self.proj / "a.jsonl"
        p.write_bytes(lines(A("m1", 5), A("m2", 4), A("m3", 1)))
        self.check()
        entry = json.loads(self.state.read_text(encoding="utf-8"))[str(p)]

        def ses(**kw):  # right shape, wrong values: would fail later, in every run
            return {str(p): {**entry, "ses": [[sid, {**d, **kw}] for sid, d in entry["ses"]]}}
        for bad in ("{", "[]", {str(p): None}, {str(p): {**entry, "ino": "x"}},
                    {str(p): {**entry, "offset": 10 ** 9}}, {str(p): {**entry, "offset": -1}},
                    {str(p): {**entry, "mark": "00"}}, {str(p): {**entry, "ses": [["s1", {}]]}},
                    ses(ts0=5), ses(ts0="x"), ses(ts1=None), ses(models=[5]), ses(title=7),
                    ses(msgs=[["m1", "a", 0, 0, 0]])):
            self.state.write_text(bad if isinstance(bad, str) else json.dumps(bad), encoding="utf-8")
            self.assertEqual(self.check(), 3, bad)

    def test_checkpoint_keeps_progress_of_a_killed_run(self):
        for n in "abcd":
            (self.proj / f"{n}.jsonl").write_bytes(body(n, 10, 0))
        real, calls = ul._scan, []

        def scan(path, prev):
            calls.append(path)
            if len(calls) == 3:
                raise RuntimeError("killed")
            return real(path, prev)
        ticks = iter(range(0, 10 ** 6, 100))
        with mock.patch.object(ul, "_scan", scan), self.assertRaises(RuntimeError):
            ul.main(self.d, self.out, now=lambda: NOW, clock=lambda: next(ticks))
        self.assertEqual(sorted(json.loads(self.state.read_text(encoding="utf-8"))), sorted(map(str, calls[:2])))
        self.assertFalse(self.out.exists())
        self.assertEqual(self.check(), 20)  # only the two files the killed run did not finish


if __name__ == "__main__":
    unittest.main()
