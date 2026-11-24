"""N6ROLLDRILL1: the fallback-switch rehearsal runs only in its own temp copy, loses or doubles
no slice, resolves a flip between intent and receipt as UNKNOWN_OUTCOME, and leaves one manager."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if "n6_fallback_drill" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("n6_fallback_drill", ROOT / "scripts" / "n6_fallback_drill.py")
    sys.modules["n6_fallback_drill"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["n6_fallback_drill"])
drill = sys.modules["n6_fallback_drill"]
lr, n6_lease = drill.lr, drill.n6_lease
LIVE_SWITCH = ROOT / "build" / "n6" / "LIMITED_ON"


class FallbackDrillTests(unittest.TestCase):
    def setUp(self):
        self.live_before = LIVE_SWITCH.exists()
        self.root, self.token = drill.make_root()
        self.addCleanup(drill.cleanup, self.root, self.token)
        self.d = drill.Drill(self.root, self.token)

    def tearDown(self):
        self.assertEqual(LIVE_SWITCH.exists(), self.live_before)  # the live switch is never touched

    def _accept(self):
        receipt = self.d.run()
        self.assertEqual(receipt["verdict"], "PASS")
        self.assertEqual(receipt["counts"], {"conveyor_starts": {"S1": 1}, "n6_claims": {"S1": 1, "S2": 1}})
        self.assertEqual((receipt["before"]["switch"], receipt["after"]["switch"],
                          receipt["restored"]["switch"]), ("N6", "conveyor", "N6"))
        self.assertEqual(receipt["after"]["lanes"]["S1"], {"finished": True, "status": "PASS"})
        self.assertEqual(len(receipt["after"]["started"]), 1)
        self.assertGreater(receipt["restored"]["lease"]["epoch"], receipt["before"]["lease"]["epoch"])
        self.assertTrue((self.d.n6 / "drill_receipt.json").is_file())
        self.assertNotIn(str(self.root), str(receipt))  # no absolute temp path or token in the receipt
        self.assertNotIn(self.token, str(receipt))
        return receipt

    def test_flip_conveyor_finishes_once(self):
        r = self._accept()
        self.assertEqual(r["after"]["deps"]["S1"], "done")
        self.assertIn("n6: S1: PASS", r["steps"])

    def test_a_flip_between_intent_and_receipt_is_unknown_outcome(self):
        d = self.d
        d.switch(True)
        self.assertEqual(d.tick(), ["S1: claimed for the conveyor"])
        epoch = drill._get(d.n6 / "claim.json")["epoch"]
        self.assertEqual(d.resolve("S1", epoch), drill.NOT_STARTED)
        self.assertIsNone(d.take(crash_before_answer=True))  # started, no receipt
        d.switch(False)
        d.clock = drill.T0 + drill.TTL + 1  # the lease has expired too
        self.assertEqual(d.resolve("S1", epoch), "unknown_outcome")
        self.assertEqual(d.tick(), ["S1: claim pending"])  # the real _settle: no blind hand-back
        self.assertIsNone(d.dispatch())  # never a blind retry
        self.assertEqual(len(d.started("S1")), 1)
        self.assertEqual(d.take(), "refused")  # the late answer, against the real state
        self.assertEqual(d.tick(), ["S1: handed back (switch off)"])
        d.switch(True)
        d.clock += 60
        d.tick()
        self.assertEqual(d.claims["S1"], 1)  # S1 is never re-claimed
        self.assertEqual((d.starts["S1"], len(d.started("S1"))), (1, 1))

    def test_b_two_managers_after_the_flip_one_wins(self):
        d = self.d
        d.switch(True)
        d.tick()
        claim = drill._get(d.n6 / "claim.json")
        old = n6_lease.Lease(**claim)
        d.switch(False)
        d.take()
        d.tick()  # handed back: the pre-flip lease is released
        path = d.n6 / "lease.json"
        now = d.clock + 1
        a = n6_lease.acquire(path, "manager-a", "S1", drill.TTL, now)
        b = n6_lease.acquire(path, "manager-b", "S1", drill.TTL, now)
        self.assertEqual(sum(x is not None for x in (a, b)), 1)
        self.assertFalse(n6_lease.check(path, old, now))
        with self.assertRaises(n6_lease.Fenced):
            n6_lease.apply_event(path, old, "late-write", now)

    def test_c_refuses_with_t2_or_protected_lane_in_flight(self):
        lr._put(self.d.conv / "backlog.json", {"slices": [
            {"id": "S1", "tier": "T1", "n6": True, "depends_on": []},
            {"id": "BIG", "tier": "T2", "n6": False, "depends_on": []}]})
        lr._put(self.d.conv / "lanes.json", {"BIG": {"status": None, "finished": False}})
        with self.assertRaisesRegex(drill.DrillRefused, "BIG"):
            self.d.run()
        lr._put(self.d.conv / "lanes.json", {"S1": {"status": None, "finished": False,
                                                   "files": ["scripts/conveyor.py"]}})
        with self.assertRaisesRegex(drill.DrillRefused, "protected"):
            self.d.run()
        self.assertFalse((self.d.n6 / "LIMITED_ON").exists())  # refused before any switch

    def test_d_refuses_a_path_it_did_not_create(self):
        with self.assertRaises(drill.DrillRefused):
            drill.Drill(ROOT, self.token)
        with self.assertRaises(drill.DrillRefused):
            drill.cleanup(ROOT, self.token)
        plain = tempfile.mkdtemp(prefix="n6drill-plain-")
        try:
            with self.assertRaisesRegex(drill.DrillRefused, "marker"):
                drill.Drill(plain, self.token)
        finally:
            shutil.rmtree(plain)
        with self.assertRaisesRegex(drill.DrillRefused, "mismatch"):
            drill.Drill(self.root, "0" * 32)

    def test_protected_paths(self):
        for p in ("scripts/conveyor.py", ".claude/x", "data\\jarvis.db", "a/.env.local", "k.pem", "./uv.lock"):
            self.assertTrue(drill._protected(p), p)
        for p in ("scripts/n6_fallback_drill.py", "jarvis/n6_lease.py", "dev/notes.md", "jarvis/tariot.md",
                  "docs/jarvis/x.md", "tests/test_env.py", "dev/data/x.md"):
            self.assertFalse(drill._protected(p), p)

    def test_protected_paths_match_conveyor_class(self):  # N6ROLLDRILL1FIX: CDX F1 variants
        cases = {
            "case": ("Scripts/Conveyor.py", ".Claude/x", "DATA/x", "K.PEM", "Dev/Dashboard/a.js", "UV.LOCK"),
            "separators": ("scripts\\ci.ps1", ".github\\workflows/ci.yml", ".\\./scripts/quality.ps1"),
            "folding": ("a/../data/x", "./././.claude/x", "jarvis/../scripts/test_parallel.py"),
            "win32 trailing": ("scripts./conveyor.py", ".claude ./x", "scripts/conveyor.py. ", "uv.lock."),
            ".jarvis": (".jarvis/config", "a/.jarvis/x", ".JARVIS/k"),
            "riot": ("riot/vgk.sys", "Riot Vanguard/x", "a/riotx/y"),
            "uv.lock anywhere": ("sub/uv.lock", "a/b/uv.lock"),
            ".env": (".env", "cfg/.env.prod", ".ENV.local"),
            "ext": ("certs/client.crt", "c.cer", "k.p12", "x.sqlite3", "a.key", "b.pfx", "c.sqlite", "d.db"),
            "fail closed": ("", " ", ".", "..", "../x", "/etc/x", "C:/x", "scripts/conveyor.py:s", "data:x"),
            "8.3 and glob": ("scr~1/conveyor.py", "scripts/CONVEY~1.PY", "scripts/*", "data/[a]", "dat?/x"),
            "dir or ancestor": ("data", ".claude", ".github/workflows", ".github", "dev/dashboard", "scripts", "scripts/"),
            "trailing slash": ("data/", ".claude/", ".github/workflows/", "dev/dashboard/", "data/ ", "data/.", "data/x/../"),
            "dot segments": ("a/.. /data/x", "x/.. ./data/x", ".. /data/x", "x/. /data/y", "a/.../b"),
            "unicode": ("R\u0130OT/x", "\uff24\uff21\uff34\uff21/x", "\u212a.pem", "k.pem\u200b", "uv\u00adlock".replace("\u00ad", ".")
                        + "\u200d"),
        }
        for variant, paths in cases.items():
            for p in paths:
                self.assertTrue(drill._protected(p), f"{variant}: {p!r}")


if __name__ == "__main__":
    unittest.main()
