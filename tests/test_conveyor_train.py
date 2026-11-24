"""TRAIN1: ready chain slices enter main together as one train INT (manifest, CAS, bisect on red)."""
import json
import unittest

from tests import test_conveyor as tc

conveyor, ORIGIN = tc.conveyor, tc.ORIGIN


class TestTrain(tc.TestChain):
    def setUp(self):
        super().setUp()
        (self.base / "pace.json").write_text(json.dumps({"train": True}), encoding="utf-8")
        self.git.fail[("git", "diff-tree")] = (0, "diff --git a/x b/x\n+x\n")
        self.c.chain_pending = []

    @staticmethod
    def mem(src, sha, files, at, approved=True):
        return {"key": f"{src}@{sha}", "src": src, "sha": sha, "picks": [sha], "files": files, "tier": "T1",
                "approved": approved, "g3": None, "reports": [f"build/reports/CLREV-{src}.md"],
                "last": f"CLREV-{src}", "at": at}

    def train(self, ap, lanes=None):
        self.c.chain_train(lanes or {}, ap, set())
        return ap

    def text(self, tid):
        return (self.root / "build/conveyor/prompts" / f"{tid}.md").read_text(encoding="utf-8")

    def test_two_ready_slices_one_train_and_manifest(self):
        a, b = "1" * 40, "2" * 40
        ap = self.train({"train_ready": [self.mem("A", a, ["scripts/a.py"], 1),
                                         self.mem("B", b, ["scripts/b.py"], 2)]})
        self.assertEqual(ap["train_open"]["id"], "INT-TRAIN1")
        t = self.text("INT-TRAIN1")
        self.assertIn(f"PICKS: {a} {b}", t)
        self.assertIn("EXPECTED: scripts/a.py, scripts/b.py", t)
        self.assertIn("CONFLICT: scripts/a.py, scripts/b.py", t)
        self.assertIn(f"base={ORIGIN}", t)
        self.assertIn(f"git cherry-pick {a} {b}", t)
        m = json.loads((self.base / "trains" / "INT-TRAIN1.json").read_text(encoding="utf-8"))
        self.assertEqual((m["start"], m["files"]), (ORIGIN, ["scripts/a.py", "scripts/b.py"]))
        self.assertEqual([(x["src"], x["sha"], x["picks"], x["reports"]) for x in m["members"]],
                         [("A", a, [a], ["build/reports/CLREV-A.md"]), ("B", b, [b], ["build/reports/CLREV-B.md"])])
        self.assertTrue(all(len(x["patch_id"]) == 64 for x in m["members"]))
        self.assertIs(ap["chain_slices"][-1]["approved"], True)
        self.assertEqual(self.c.chain_pending[-1]["step"], "TRAIN")

    def test_overlapping_slices_separate_trains(self):
        x = "9" * 40
        ap = self.train({"train_ready": [self.mem("A", "1" * 40, ["scripts/a.py"], 1),
                                         self.mem("B", "2" * 40, ["scripts/a.py", "scripts/b.py"], 2),
                                         self.mem("C", "3" * 40, ["scripts/c.py"], 3)]})
        self.assertEqual(ap["train_open"]["members"], ["A@" + "1" * 40, "C@" + "3" * 40])
        self.git.fail[("git", "merge-base", "--is-ancestor", x, ORIGIN)] = (0, "")
        lanes = {"INT-TRAIN1": {"finished": True, "status": "GREEN", "ci": {"cand": True, "sha": x}}}
        self.train(ap, lanes)
        self.assertEqual(self.c.chain_pending[-1]["step"], "TRAIN-LANDED")
        self.assertEqual([m["src"] for m in ap["train_ready"]], ["B"])
        self.train(ap, lanes)
        self.assertEqual((ap["train_open"]["id"], ap["train_open"]["members"]), ("INT-TRAIN2", ["B@" + "2" * 40]))

    def test_cas_main_moved_rebuilds_nothing_pushed(self):
        ap = self.train({"train_ready": [self.mem("A", "1" * 40, ["scripts/a.py"], 1),
                                         self.mem("B", "2" * 40, ["scripts/b.py"], 2)]})
        self.git.origin = "d" * 40
        self.train(ap, {"INT-TRAIN1": {"finished": True, "status": "GREEN"}})
        self.assertEqual(self.c.chain_pending[-1]["step"], "TRAIN-INVALID")
        self.assertIsNone(ap["train_open"])
        self.assertEqual(len(ap["train_ready"]), 2)
        self.assertNotIn("train_failed", ap)
        self.train(ap, {"INT-TRAIN1": {"finished": True, "status": "GREEN"}})
        self.assertEqual(ap["train_open"]["start"], "d" * 40)
        self.assertIn("base=" + "d" * 40, self.text(ap["train_open"]["id"]))
        self.assertEqual(self.git.pushes(), [])

    def test_red_train_members_back_and_bisect(self):
        ap = self.train({"train_ready": [self.mem(s, str(i) * 40, [f"scripts/{s}.py"], i)
                                         for i, s in enumerate("ABCD", 1)]})
        lanes = {"INT-TRAIN1": {"finished": True, "status": "BLOCKED"}}
        self.train(ap, lanes)
        self.assertEqual(self.c.chain_pending[-1]["step"], "TRAIN-RED")
        self.assertEqual((ap["train_cap"], len(ap["train_ready"])), (2, 4))
        self.assertEqual(len(ap["train_failed"][0]["members"]), 4)
        self.train(ap, lanes)
        self.assertEqual(len(ap["train_open"]["members"]), 2)
        one = self.train({"train_ready": [self.mem("E", "5" * 40, ["scripts/e.py"], 1)]})
        self.train(one, {one["train_open"]["id"]: {"finished": True, "status": "BLOCKED"}})
        self.assertEqual((self.c.chain_pending[-1]["step"], one["train_ready"]), ("TRAIN-PARKED", []))

    def test_protected_member_whole_train_waits_for_key1(self):
        ap = self.train({"train_ready": [self.mem("A", "1" * 40, ["scripts/a.py"], 1),
                                         self.mem("B", "2" * 40, ["scripts/b.py"], 2, approved=False)]})
        tid = ap["train_open"]["id"]
        self.assertNotIn("approved", ap["chain_slices"][-1])
        self.assertIn(conveyor.CHAIN_KEY1, str(self.c.chain_int_refusal(tid, {}, ap)))

    def test_flow_train_on_and_off(self):
        self.start()
        self.finish("SRC", "PASS", self.PICK)
        self.finish("CLREV-SRC", "PASS", self.REVPICK)
        self.assertNotIn("INT-SRC", self.ids())
        self.assertTrue((self.base / "trains" / "INT-TRAIN1.json").exists())


class TestTrainPushCas(tc.TestChain):
    """TRAIN1FIX: the push gate itself refuses a train candidate not built on the current origin/main."""
    TID = "INT-TRAIN1"

    def gate(self, manifest, tid=TID, write=True):
        self.git.main, self.git.origin = tc.MAIN, ORIGIN
        self.git.contains, self.git.fail = "  origin/ci/int-train1\n", {}
        p = self.base / "trains" / f"{tid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(manifest, bytes):
            p.write_bytes(manifest)
        elif manifest is not None:
            p.write_text(manifest if isinstance(manifest, str) else json.dumps(manifest), encoding="utf-8")
        lanes = {tid: {"finished": True, "status": "GREEN",
                       "ci": {"sha": tc.MAIN, "branch": "ci/int-train1", "cand": True}}}
        return self.c.push_checks(f"push-{tc.MAIN}-int-train1", lanes)

    def refused(self, why):
        self.assertIsNotNone(why)
        self.assertIn(f"origin/main is {ORIGIN[:7]}: rebuilt", why)
        self.assertEqual(self.git.pushes(), [])

    def test_matching_start_passes_cas(self):
        why, sha, url = self.gate({"id": self.TID, "start": ORIGIN})
        self.assertEqual((why, sha), (None, tc.MAIN))
        self.assertTrue(url)

    def test_moved_main_refused(self):
        why = self.gate({"id": self.TID, "start": "d" * 40})[0]
        self.refused(why)
        self.assertIn("train INT-TRAIN1 was built on ddddddd", why)

    def test_start_near_misses_refused(self):
        for start in (ORIGIN[:7], ORIGIN.upper(), ORIGIN + "\n", " " + ORIGIN, "", None, 0, [ORIGIN]):
            with self.subTest(start=start):
                self.refused(self.gate({"id": self.TID, "start": start})[0])
        self.refused(self.gate({"id": self.TID})[0])

    def test_lost_or_broken_manifest_refused(self):
        for manifest in (None, "", "{not json", b"\xff\xfe\x00", "[]", '"start"', "null", "5"):
            with self.subTest(manifest=manifest):
                p = self.base / "trains" / f"{self.TID}.json"
                if p.is_file():
                    p.unlink()
                self.refused(self.gate(manifest)[0])
        p.unlink()
        p.mkdir()  # the manifest path is a directory: OSError, still refused
        self.refused(self.gate(None)[0])

    def test_lower_case_train_id_still_checked(self):
        self.refused(self.gate({"start": "d" * 40}, tid="int-train1")[0])

    def test_stacked_candidate_and_shared_sha_still_checked(self):
        cand = "c" * 40  # candidate on top of local main: head != want, sha resolves to the candidate
        self.git.main, self.git.origin, self.git.fail = tc.MAIN, ORIGIN, {
            ("git", "merge-base", "--is-ancestor", tc.MAIN, cand): (0, ""),
            ("git", "merge-base", "--is-ancestor", ORIGIN, cand): (0, "")}
        (self.base / "trains").mkdir(parents=True, exist_ok=True)
        (self.base / "trains" / f"{self.TID}.json").write_text(json.dumps({"start": "d" * 40}), encoding="utf-8")
        ci = {"sha": cand, "branch": "ci/int-train1", "cand": True}
        lanes = {"INT-X": {"finished": True, "ci": dict(ci)}, self.TID: {"finished": True, "ci": ci}}
        why, sha, _ = self.c.push_checks(f"push-{cand}-int-train1", lanes)
        self.assertEqual(sha, cand)
        self.refused(why)

    def test_non_train_lane_has_no_train_cas(self):
        self.git.main, self.git.contains = tc.MAIN, "  origin/ci/int-x\n"
        lanes = {"INT-X": {"finished": True, "status": "GREEN", "ci": {"sha": tc.MAIN, "cand": True}}}
        self.assertIsNone(self.c.push_checks(f"push-{tc.MAIN}-int-x", lanes)[0])


for _n in dir(tc.TestChain):  # only the train tests run here; TestChain runs in tests.test_conveyor
    if _n.startswith("test_"):
        setattr(TestTrain, _n, None)
        setattr(TestTrainPushCas, _n, None)

if __name__ == "__main__":
    unittest.main()
