"""scripts/usage_ledger.py on synthetic transcripts in a temp dir; never the real ~/.claude."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "usage_ledger", Path(__file__).resolve().parents[1] / "scripts" / "usage_ledger.py")
ul = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ul)


def A(mid, out, sid="s1", **u):
    return {"type": "assistant", "sessionId": sid, "timestamp": "2026-10-08T21:30:00Z",
            "message": {"id": mid, "model": "m1",
                        "usage": {"input_tokens": 1, "output_tokens": out, **u}}}


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.out = self.d / "o" / "usage.json"
        self.proj = self.d / "c--Users-realk-Masa-st--JARVIS"
        self.proj.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def transcript(self, name, lines):
        p = self.proj / name
        p.write_text("\n".join(json.dumps(x) if not isinstance(x, str) else x for x in lines),
                     encoding="utf-8")
        return p

    def run_main(self):
        return ul.main(self.d, self.out)

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


if __name__ == "__main__":
    unittest.main()
