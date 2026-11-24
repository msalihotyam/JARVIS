"""ORCHGEN: scripts/orch_gen.py on a temp repo with fake reports (never the real backlog)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import conveyor  # noqa: E402
import orch_decisions  # noqa: E402
import orch_gen  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


class OrchGenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orchgen-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = r = self.tmp / "repo"
        r.mkdir()
        self.git("init", "-q", "-b", "main")
        self.put("jarvis/foo.py", "x = 1\n")
        self.put("tests/test_foo.py", "pass\n")
        self.commit("base")
        subprocess.run([*GIT, "init", "-q", "--bare", str(self.tmp / "origin.git")], check=True)
        self.git("remote", "add", "origin", str(self.tmp / "origin.git"))
        self.git("push", "-q", "origin", "main")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "-c", "lane")
        self.put("jarvis/foo.py", "x = 2\n")
        self.put("tests/test_foo_extra.py", "pass\n")
        self.sha_a = self.commit("feat: PMA")
        self.put("jarvis/foo.py", "x = 3\n")
        self.sha_b = self.commit("fix: PMAFIX\n\nWorker: codex")
        self.put("build/conveyor/backlog.json", '{\n  "slices": []\n}')
        self.put("build/orch/check_backlog.py", "print('OK: stub')\n")
        self.put("build/orch/preflight.py", "print('preflight: 0 pending, 0 errors, 0 warnings')\n")
        (r / "build/conveyor/prompts").mkdir()
        self.report("PMA", self.sha_a, "jarvis/foo.py; tests/test_foo_extra.py (new)")
        self.report("PMAFIX", self.sha_b, "`jarvis/foo.py`")

    def git(self, *args):
        return subprocess.run([*GIT, "-C", str(self.root), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def put(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, msg):
        self.git("add", "jarvis", "tests")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def report(self, sid, sha, files, status="PASS", extra=""):
        self.put(f"build/reports/{sid}.md",
                 f"STATUS: {status}\nBRANCH: worktree-{sid}\nCOMMIT: {sha}\nFILES: {files}\n{extra}")

    def run_gen(self, *argv):
        out = []
        rc = orch_gen.main(["--root", str(self.root), *argv], out=out.append)
        return rc, "\n".join(out)

    def backlog(self):
        return json.loads((self.root / "build/conveyor/backlog.json").read_text(encoding="utf-8"))

    def prompt(self, sid):
        return (self.root / f"build/conveyor/prompts/{sid}.md").read_text(encoding="utf-8")

    def assert_lints(self, sid):
        s = next(x for x in self.backlog()["slices"] if x["id"] == sid)
        text = self.prompt(sid)
        self.assertIsNone(conveyor.validate_slice(s))
        self.assertIsNone(conveyor.check_header(text)[1])
        ids = {x["id"] for x in self.backlog()["slices"]}
        self.assertIsNone(conveyor.lint_slice(s, text, lambda p: (self.root / p).exists(), ids))
        self.assertNotIn("approved", s)

    def test_claude_work_gets_codex_review_and_dry_run_writes_nothing(self):
        before = (self.root / "build/conveyor/backlog.json").read_bytes()
        rc, out = self.run_gen("review", "PMA")
        self.assertEqual(rc, 0, out)
        self.assertIn("@jarvis lane=CDXINT1 model=opus effort=medium base=main", out)
        self.assertIn(f"Scope: PMA {self.sha_a} (jarvis/foo.py, tests/test_foo_extra.py)", out)
        self.assertIn("dry-run", out)
        self.assertEqual((self.root / "build/conveyor/backlog.json").read_bytes(), before)
        self.assertEqual(list((self.root / "build/conveyor/prompts").iterdir()), [])
        self.assertFalse((self.root / "build/conveyor/hold").exists())

    def test_codex_work_gets_claude_review(self):
        for extra, sid, sha in (("", "PMAFIX", self.sha_b), ("WORKER: codex\n", "PMA", self.sha_a)):
            if extra:  # the report line alone also marks Codex work
                self.report(sid, sha, "jarvis/foo.py", extra=extra)
            rc, out = self.run_gen("review", sid)
            self.assertEqual(rc, 0, out)
            self.assertIn("@jarvis lane=CLREV1 ", out)
            self.assertIn("WRITTEN BY THE CODEX WORKER", out)

    def spec(self, **over):
        sp = {"id": "PV9", "title": "t", "tier": "T2", "model": "opus", "effort": "high", "size": "MEDIUM",
              "goal": "g", "tests": "a test", "run": ["tests.test_foo"],
              "owned": ["jarvis/foo.py", "jarvis/new_mod.py"], "commit": "feat: PV9 x"}
        sp.update(over)
        sp = {k: v for k, v in sp.items() if v is not None}
        self.put("spec.json", json.dumps(sp))
        return str(self.root / "spec.json")

    def test_slice_daily_cap(self):
        day = [1_800_000_000.0]  # injected clock: the cap counts per UTC day

        def write(sid):
            out = []
            rc = orch_gen.main(["--root", str(self.root), "slice", "--spec",
                                self.spec(id=sid, commit=f"feat: {sid} x"), "--write"],
                               out=out.append, now=lambda: day[0])
            return rc, "\n".join(out)

        cap = orch_gen.DAILY_CAP["slice"]
        for n in range(cap):
            rc, out = write(f"PV{n}")
            self.assertEqual(rc, 0, out)
        rc, out = write(f"PV{cap}")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"REFUSED: ALLOW-T5 daily slice cap {cap} reached; escalate to the orchestrator", out)
        self.assertNotIn(f"PV{cap}", [s["id"] for s in self.backlog()["slices"]])
        self.assertFalse((self.root / f"build/conveyor/prompts/PV{cap}.md").exists())
        day[0] += 86400  # a new UTC day resets it
        rc, out = write(f"PV{cap}")
        self.assertEqual(rc, 0, out)

    def test_fix_daily_cap(self):
        day = [1_800_000_000.0]  # injected clock: the cap counts per UTC day
        self.put("build/reports/CLREV7.md", "STATUS: BLOCKED\nF1 MAJOR foo breaks\n")
        self.put("build/conveyor/backlog.json", json.dumps({"slices": [
            {"id": "PMAFIX", "tier": "T2", "files": ["jarvis/foo.py"], "prompt": "build/conveyor/prompts/PMAFIX.md"}]}))

        def write(fid):
            out = []
            rc = orch_gen.main(["--root", str(self.root), "fix", "--spec",
                                self.spec(id=fid, review="CLREV7", source="PMAFIX", size="SMALL",
                                          commit=f"fix: {fid} y"), "--write"],
                               out=out.append, now=lambda: day[0])
            return rc, "\n".join(out)

        cap = orch_gen.DAILY_CAP["fix"]
        fids = [f"PV9FIX{chr(65 + n)}" for n in range(cap + 1)]
        for fid in fids[:cap]:
            rc, out = write(fid)
            self.assertEqual(rc, 0, out)
        before = (self.root / "build/conveyor/backlog.json").read_bytes()
        rc, out = write(fids[cap])
        self.assertEqual(rc, 2, out)
        self.assertIn(f"REFUSED: ALLOW-T5 daily fix cap {cap} reached; escalate to the orchestrator", out)
        self.assertEqual((self.root / "build/conveyor/backlog.json").read_bytes(), before)
        self.assertFalse((self.root / f"build/conveyor/prompts/{fids[cap]}.md").exists())
        day[0] += 86400  # a new UTC day resets it
        rc, out = write(fids[cap])
        self.assertEqual(rc, 0, out)

    def test_slice_spec_chained_lints_and_dry_run_writes_nothing(self):
        before = (self.root / "build/conveyor/backlog.json").read_bytes()
        rc, out = self.run_gen("slice", "--spec", self.spec(chain=True, split_ok=True))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"@jarvis lane=PV9 model=opus effort=high base={self.base}\n", out)
        self.assertIn("PV9: T2, MEDIUM <= 50 tool calls, SPLIT-OK.\nCHAIN: CLREV+CDX\n", out)
        self.assertIn("Owned: jarvis/foo.py, jarvis/new_mod.py (new), build/reports/PV9.md (new)", out)
        self.assertEqual((self.root / "build/conveyor/backlog.json").read_bytes(), before)
        self.assertEqual(list((self.root / "build/conveyor/prompts").iterdir()), [])
        rc, out = self.run_gen("slice", "--spec", self.spec(chain=True), "--write")
        self.assertEqual(rc, 0, out)
        self.assertIn("preflight: 0 pending, 0 errors", out)
        self.assert_lints("PV9")
        self.assertEqual(self.backlog()["slices"][0]["chain"], ["CLREV", "CDX", "INT"])

    def test_tests_list_renders_one_dash_line_per_test(self):
        rc, out = self.run_gen("slice", "--spec", self.spec(tests=["one", "two", "three"]))
        self.assertEqual(rc, 0, out)
        self.assertIn("\nTests:\n- one\n- two\n- three\nRun: python", out)
        self.assertNotIn("['one'", out)

    def test_max_calls_sets_the_size_line(self):
        rc, out = self.run_gen("slice", "--spec", self.spec(max_calls=35))
        self.assertEqual(rc, 0, out)
        self.assertIn("PV9: T2, MEDIUM <= 35 tool calls.", out)

    def test_max_calls_and_calls_must_be_a_positive_int(self):
        for key in ("max_calls", "calls"):
            for bad in (True, -1, 0, "30", 1.5):
                rc, out = self.run_gen("slice", "--spec", self.spec(**{key: bad}), "--write")
                self.assertEqual(rc, 2, f"{key}={bad!r}: {out}")
                self.assertIn(f"REFUSED: spec {key} must be a positive integer", out)
                self.assertEqual(self.backlog()["slices"], [])
                self.assertEqual(list((self.root / "build/conveyor/prompts").iterdir()), [])
            rc, out = self.run_gen("slice", "--spec", self.spec(**{key: 35}))
            self.assertEqual(rc, 0, out)
            self.assertIn("PV9: T2, MEDIUM <= 35 tool calls.", out)

    def test_fix_spec_series_is_the_source_series_or_source_id(self):
        self.put("build/reports/CLREV7.md", "STATUS: BLOCKED\nF1 MAJOR foo breaks\n")
        for fid, series, want in (("PV9FIX", None, "PMAFIX"), ("PV9FIXB", "PMASER", "PMASER")):
            src = {"id": "PMAFIX", "tier": "T2", "files": ["jarvis/foo.py"],
                   "prompt": "build/conveyor/prompts/PMAFIX.md"}
            if series:
                src["series"] = series
            self.put("build/conveyor/backlog.json", json.dumps({"slices": [src]}))
            rc, out = self.run_gen("fix", "--spec", self.spec(id=fid, review="CLREV7", source="PMAFIX",
                                                             size="SMALL", commit=f"fix: {fid} y"), "--write")
            self.assertEqual(rc, 0, out)
            s = next(x for x in self.backlog()["slices"] if x["id"] == fid)
            self.assertEqual(s["series"], want)

    def test_int_accepts_a_stack_only_file_no_pick_changed(self):
        # tests/test_foo_extra.py is in the tree at sha_b but only sha_a changed it
        # tests/test_foo.py is in the tree but no pick changed it
        self.report("PMAFIX", self.sha_b, "jarvis/foo.py; tests/test_foo_extra.py; tests/test_foo.py")
        self.assertEqual(self.run_gen("review", "PMA", "--write", "--focus", "x")[0], 0)
        self.assertEqual(self.run_gen("review", "PMAFIX", "--write", "--focus", "x")[0], 0)
        for rid in ("CDXINT1", "CLREV2"):
            self.put(f"build/reports/{rid}.md", f"STATUS: PASS\nCOMMIT: reviewed {self.sha_a}\n")
        rc, out = self.run_gen("int", "CLREV2", "CDXINT1")
        self.assertEqual(rc, 0, out)
        self.assertIn("Expected files only: jarvis/foo.py, tests/test_foo_extra.py.", out)
        self.assertNotIn("tests/test_foo.py", out.split("Expected files only:")[1].split("\n")[0])

    def test_int_refuses_overlapping_scopes(self):  # ALLOWLIST T3: batch INT needs disjoint scopes
        self.report("QZ", self.sha_b, "jarvis/foo.py")
        self.assertEqual(self.run_gen("review", "PMA", "--write", "--focus", "x")[0], 0)
        self.assertEqual(self.run_gen("review", "QZ", "--write", "--focus", "x")[0], 0)
        for rid in ("CDXINT1", "CLREV2"):
            self.put(f"build/reports/{rid}.md", f"STATUS: PASS\nCOMMIT: reviewed {self.sha_a}\n")
        rc, out = self.run_gen("int", "CLREV2", "CDXINT1")
        self.assertEqual(rc, 2, out)
        self.assertIn("scopes not disjoint: jarvis/foo.py in PMA and QZ", out)

    def test_int_refuses_a_prefix_id_that_is_not_its_fix(self):  # FIX3 F4: PMA2 is not PMA's FIX
        self.report("PMA2", self.sha_b, "jarvis/foo.py")
        self.assertEqual(self.run_gen("review", "PMA", "--write", "--focus", "x")[0], 0)
        self.assertEqual(self.run_gen("review", "PMA2", "--write", "--focus", "x")[0], 0)
        for rid in ("CDXINT1", "CLREV2"):
            self.put(f"build/reports/{rid}.md", f"STATUS: PASS\nCOMMIT: reviewed {self.sha_a}\n")
        rc, out = self.run_gen("int", "CLREV2", "CDXINT1")
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r"scopes not disjoint: jarvis/foo\.py in PMA2? and PMA2?\b")

    def test_gen_caps_negative_or_bad_key_is_refused(self):  # FIX3 F2
        for bad in ({"slice:2027-01-15": -100}, {"slice:2027-01-15": True}, {"slice:zzzz": 0}, {"slice": 0}, [],
                    {"slice:2027-01-15": 4, "slice:zz:2027-01-15": 0}, {"other:2027-01-15": 0}):
            self.put("build/orch/gen_caps.json", json.dumps(bad))
            with self.assertRaisesRegex(orch_gen.Refuse, "ALLOW-T5 cap file malformed", msg=bad):
                orch_gen.daily_count(self.root, "slice", 1_800_000_000.0, add=1)

    def test_gen_caps_backward_clock_keeps_the_newest_day(self):  # FIX3 F3
        t, cap = 1_800_000_000.0, orch_gen.DAILY_CAP["slice"]
        for _ in range(cap):
            orch_gen.daily_count(self.root, "slice", t + 86400, add=1)
        self.assertEqual(orch_gen.daily_count(self.root, "slice", t), cap)  # an older clock reads the newest day
        self.assertEqual(orch_gen.daily_count(self.root, "slice", t, add=1), cap + 1)
        self.assertEqual(orch_gen.daily_count(self.root, "slice", t + 2 * 86400), 0)  # a forward day still resets

    def test_spec_without_tests_or_run_is_refused(self):
        for k in ("tests", "run"):
            rc, out = self.run_gen("slice", "--spec", self.spec(**{k: None}), "--write")
            self.assertEqual(rc, 2, out)
            self.assertIn(f"REFUSED: spec has no {k}", out)
        self.assertEqual(self.backlog()["slices"], [])

    def test_preflight_error_rolls_back(self):
        before = (self.root / "build/conveyor/backlog.json").read_bytes()
        self.put("build/orch/preflight.py", "import sys\nprint('preflight: 1 errors')\nsys.exit(1)\n")
        rc, out = self.run_gen("slice", "--spec", self.spec(), "--write")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED: preflight.py failed", out)
        self.assertEqual((self.root / "build/conveyor/backlog.json").read_bytes(), before)
        self.assertFalse((self.root / "build/conveyor/prompts/PV9.md").exists())

    def test_fix_spec_over_three_commit_stack(self):
        self.put("jarvis/bar.py", "y = 1\n")
        sha_c = self.commit("fix: PMAFIX2")
        self.put("build/conveyor/backlog.json", json.dumps({"slices": [
            {"id": "PMAFIX2", "stack": [self.sha_a, self.sha_b], "review_base": self.base,
             "chain": ["CLREV", "CDX", "INT"], "tier": "T2", "files": ["jarvis/foo.py"],
             "prompt": "build/conveyor/prompts/PMAFIX2.md"}]}))
        self.report("PMAFIX2", sha_c, "jarvis/bar.py (new)")
        self.put("build/reports/CLREV7.md", "STATUS: BLOCKED\nF1 MAJOR foo breaks\nF2 NIT naming\n")
        rc, out = self.run_gen("fix", "--spec", self.spec(id="PV9FIX", review="CLREV7", source="PMAFIX2",
                                                         size="SMALL", commit="fix: PV9FIX y"), "--write")
        self.assertEqual(rc, 0, out)
        s = next(x for x in self.backlog()["slices"] if x["id"] == "PV9FIX")
        self.assertEqual(s["stack"], [self.sha_a, self.sha_b, sha_c])
        self.assertEqual(s["review_base"], self.base)
        self.assertEqual(s["files"], ["jarvis/foo.py", "jarvis/new_mod.py", "tests/test_foo_extra.py",
                                      "jarvis/bar.py", "build/reports/PV9FIX.md"])
        text = self.prompt("PV9FIX")
        self.assertTrue(text.startswith("@jarvis lane=PV9FIX model=opus effort=high base=main\n"
                                        "LANE RULES: build/conveyor/notes/LANE_RULES.md (CORE + FIX)\n"
                                        "PV9FIX: T2, SMALL <= 35.\nCHAIN: CLREV+CDX\n"), text)
        self.assertIn(f"BASE AT {sha_c} (stack ", text)
        self.assertIn("READ FIRST: build/reports/CLREV7.md (F1, F2)\nFindings (data):\n    > F1 MAJOR foo", text)
        self.assertIn("Stack-only grant (do not edit): tests/test_foo_extra.py, jarvis/bar.py\n", text)
        self.assert_lints("PV9FIX")

    def test_refusals(self):
        cases = {
            "BLOCKED": lambda: self.report("PMA", self.sha_a, "jarvis/foo.py", status="BLOCKED"),
            "no COMMIT": lambda: self.put("build/reports/PMA.md", "STATUS: PASS\nFILES: jarvis/foo.py\n"),
            "not changed by": lambda: self.report("PMA", self.sha_a, "jarvis/foo.py; jarvis/other.py"),
            "no FILES": lambda: self.report("PMA", self.sha_a, ""),
            "2 COMMITs": lambda: self.report("PMA", self.sha_a, "jarvis/foo.py",
                                             extra=f"COMMIT: {self.sha_b}\n"),
            "is not a commit": lambda: self.report("PMA", "0" * 40, "jarvis/foo.py"),
        }
        for why, make in cases.items():
            with self.subTest(why):
                make()
                rc, out = self.run_gen("review", "PMA", "--focus", "x", "--write")
                self.assertEqual(rc, 2, out)
                self.assertIn("REFUSED", out)
                self.assertIn(why, out)
                self.assertEqual(self.backlog()["slices"], [])
        rc, out = self.run_gen("review", "PMA", "PMAFIX")  # Claude + Codex in one review
        self.assertEqual(rc, 2, out)

    def test_write_then_integration_orders_stacked_picks(self):
        rc, out = self.run_gen("review", "PMA", "--write", "--focus", "the PM10 gate")
        self.assertEqual(rc, 0, out)
        self.assertIn("OK: stub", out)
        self.assertTrue((self.root / "build/conveyor/hold/backlog.json.bak-CDXINT1").is_file())
        self.assert_lints("CDXINT1")
        rc, out = self.run_gen("review", "PMAFIX", "--write", "--focus", "x")
        self.assertEqual(rc, 0, out)
        self.assert_lints("CLREV2")
        self.assertTrue((self.root / "build/conveyor/backlog.json").read_text(encoding="utf-8").endswith("]\n}"))
        rc, out = self.run_gen("int", "CLREV2", "CDXINT1")
        self.assertEqual(rc, 2, out)  # reviews not PASS yet
        for rid in ("CDXINT1", "CLREV2"):
            self.put(f"build/reports/{rid}.md", f"STATUS: PASS\nCOMMIT: reviewed {self.sha_a}\n")
        rc, out = self.run_gen("int", "CLREV2", "CDXINT1", "--write")
        self.assertEqual(rc, 0, out)
        text = self.prompt("INT1")
        self.assertTrue(text.startswith("@jarvis lane=INT1 model=sonnet effort=medium base=main\n"))
        self.assertIn(f"local main == origin/main == {self.base};", text)
        self.assertIn(f"commit only: PMA {self.sha_a}, PMAFIX {self.sha_b}.", text)
        self.assertIn("Expected files only: jarvis/foo.py, tests/test_foo_extra.py.", text)
        self.assertIn("one group per run: tests.test_foo tests.test_foo_extra (0 failures", text)
        self.assertNotIn("tests.test_ui", text)
        self.assertEqual(conveyor.CI_TARGET_RE.findall(text), ["int1"])
        self.assert_lints("INT1")
        self.assertEqual([s["id"] for s in self.backlog()["slices"]], ["CDXINT1", "CLREV2", "INT1"])

    def assert_review(self, ok, why, *argv):
        rc, out = self.run_gen("review", *argv)
        self.assertEqual(rc, 0 if ok else 2, out)
        self.assertIn(why, out)
        return out

    def test_files_separators_annotations_and_own_report(self):
        both = f"Scope: PMA {self.sha_a} (jarvis/foo.py, tests/test_foo_extra.py)"
        for files in ("jarvis/foo.py\n  - tests/test_foo_extra.py (new)\nRUNS: tests.test_foo 3 tests",
                      "jarvis/foo.py (PM10 section: CHECK_ALLOWLIST, Refusal; validate), "
                      "tests/test_foo_extra.py (new, 13 tests)",
                      "jarvis/foo.py (a (b, c); d); tests/test_foo_extra.py",
                      "build/reports/PMA.md, jarvis/foo.py, tests/test_foo_extra.py"):
            with self.subTest(files):
                self.report("PMA", self.sha_a, files)
                self.assert_review(True, both, "PMA")
        for files, why in (("jarvis/foo.py (unbalanced", "not changed by"),
                           ("jarvis/foo.py\n  - jarvis/other.py (new)", "jarvis/other.py"),
                           ("jarvis/foo.py\n  jarvis/other.py: new helper", "jarvis/other.py"),
                           ("jarvis/foo.py\n  Note: x\n  jarvis/other.py", "Note"),
                           ("build/reports/PMA.md", "no FILES"),
                           ("jarvis/foo.py, build/reports/PMAFIX.md", "build/reports/PMAFIX.md")):
            with self.subTest(files):
                self.report("PMA", self.sha_a, files)
                self.assert_review(False, why, "PMA")

    def test_jules_report_without_files_uses_the_commit(self):
        head = f"STATUS: PASS\nCOMMIT: {self.sha_a}\nBRANCH: jules/PMA\n"
        self.put("build/reports/PMA.md", head + "Worker: jules (its own claims untrusted)\n")
        self.assert_review(True, f"Scope: PMA {self.sha_a} (jarvis/foo.py, tests/test_foo_extra.py)", "PMA")
        for text, why in ((head + "Worker: jules\nFILES:\n", "no FILES"),
                          (head, "no FILES"),
                          (head.replace("jules/PMA", "lane/PMA") + "Worker: jules\n", "no FILES"),
                          (head + "Worker: jules\nWorker: codex\n", "both the Jules and the Codex")):
            with self.subTest(why):
                self.put("build/reports/PMA.md", text)
                self.assert_review(False, why, "PMA")

    def test_with_partial_predecessor(self):
        def setup(status="BLOCKED (partial: fix list 1-3 done; 4 open)", base=None):
            self.report("PMA", self.sha_a, "jarvis/foo.py", status=status, extra="Worker: codex\n")
            self.report("PMAFIX", self.sha_b, "jarvis/foo.py", extra=base if base is not None
                        else f"BASE: PMA AT {self.sha_a}\n")
        setup()
        out = self.assert_review(True, "@jarvis lane=CLREV1 ", "PMAFIX", "--with-partial", "PMA")
        self.assertIn(f"PMA {self.sha_a} (jarvis/foo.py, tests/test_foo_extra.py) [partial, finished by PMAFIX];"
                      f" PMAFIX {self.sha_b} (jarvis/foo.py); review as one diff main..{self.sha_b[:12]}", out)
        cases = (
            ({"base": f"BASE: PMB AT {self.sha_a}\n"}, "no reviewed report names BASE: PMA"),
            ({"base": f"BASE: PMA AT {self.sha_b}\n"}, "is not PMA's COMMIT"),
            ({"base": ""}, "no reviewed report names BASE: PMA"),
            ({"status": "PASS"}, "not BLOCKED (partial"),
            ({"status": "DROPPED"}, "not BLOCKED (partial"),
            ({"status": "STUCK"}, "not BLOCKED (partial"),
            ({"status": "BLOCKED (not partial: nothing usable)"}, "not BLOCKED (partial"),
            ({"status": "BLOCKED: tests red"}, "or a plain BLOCKED"),
            # a plain BLOCKED needs the full 40-hex COMMIT as BASE, not a prefix or another sha
            ({"status": "BLOCKED", "base": f"BASE: PMA AT {self.sha_a[:12]}\n"}, "is not PMA's COMMIT"),
            ({"status": "BLOCKED", "base": f"BASE: PMA AT {self.sha_b}\n"}, "is not PMA's COMMIT"),
            ({"status": "BLOCKED", "base": f"BASE: PMB AT {self.sha_a}\n"}, "no reviewed report names"),
            ({"status": "BLOCKED", "base": ""}, "no reviewed report names BASE: PMA"),
        )
        for kw, why in cases:
            with self.subTest(why, **kw):
                setup(**kw)
                self.assert_review(False, why, "PMAFIX", "--with-partial", "PMA")
        with self.subTest("plain BLOCKED, no COMMIT"):
            self.put("build/reports/PMA.md", "STATUS: BLOCKED\nFILES: jarvis/foo.py\nWorker: codex\n")
            self.assert_review(False, "no COMMIT", "PMAFIX", "--with-partial", "PMA")
        # a plain BLOCKED predecessor named as BASE with its exact COMMIT is accepted
        setup(status="BLOCKED")
        self.assert_review(True, f"PMA {self.sha_a} (jarvis/foo.py, tests/test_foo_extra.py) [partial, "
                                 "finished by PMAFIX]", "PMAFIX", "--with-partial", "PMA")
        # "partial" only in a title line: the STATUS line conveyor.report_status reads is plain
        self.put("build/reports/PMA.md", f"# PMA (prior STATUS: partial)\nSTATUS: BLOCKED\nCOMMIT: {self.sha_a}\n"
                 "FILES: jarvis/foo.py\nWorker: codex\n")
        self.assert_review(True, "[partial, finished by PMAFIX]", "PMAFIX", "--with-partial", "PMA")
        self.report("PMAFIX", self.sha_b, "jarvis/foo.py", extra=f"BASE: PMA AT {self.sha_a[:12]}\n")
        self.assert_review(False, "is not PMA's COMMIT", "PMAFIX", "--with-partial", "PMA")
        setup()
        self.assert_review(False, "report STATUS is BLOCKED, not PASS", "PMAFIX", "PMA")
        self.assert_review(False, "name each slice once", "PMAFIX", "PMA", "--with-partial", "PMA")
        # roles swapped: the later slice is the ancestor, so it does not build on the partial
        self.report("PMAFIX", self.sha_b, "jarvis/foo.py", status="BLOCKED (partial)", extra="Worker: codex\n")
        self.report("PMA", self.sha_a, "jarvis/foo.py", extra=f"Worker: codex\nBASE: PMAFIX AT {self.sha_b}\n")
        self.assert_review(False, "does not build on PMAFIX", "PMA", "--with-partial", "PMAFIX")
        self.assertEqual(self.backlog()["slices"], [])

    def test_int_takes_a_plain_blocked_stacked_predecessor(self):
        self.report("PMA", self.sha_a, "jarvis/foo.py", status="BLOCKED", extra="Worker: codex\n")
        self.report("PMAFIX", self.sha_b, "jarvis/foo.py", extra=f"BASE: PMA AT {self.sha_a}\n")
        rc, out = self.run_gen("review", "PMAFIX", "--with-partial", "PMA", "--write", "--focus", "x")
        self.assertEqual(rc, 0, out)
        self.put("build/reports/CLREV1.md", "STATUS: PASS\n")
        rc, out = self.run_gen("int", "CLREV1")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"commit only: PMA {self.sha_a}, PMAFIX {self.sha_b}.", out)
        self.assertIn(f"PMA.md BLOCKED (stacked: PMAFIX.md names BASE: PMA AT {self.sha_a}), PMAFIX.md PASS "
                      "naming the COMMITs below.", out)
        for why, make in (
                ("is not PMA's COMMIT", lambda: self.report(
                    "PMAFIX", self.sha_b, "jarvis/foo.py", extra=f"BASE: PMA AT {self.sha_a[:12]}\n")),
                ("no reviewed report names", lambda: self.report("PMAFIX", self.sha_b, "jarvis/foo.py")),
                ("not PASS", lambda: self.report("PMA", self.sha_a, "jarvis/foo.py", status="DROPPED"))):
            with self.subTest(why):
                make()
                rc, out = self.run_gen("int", "CLREV1")
                self.assertEqual(rc, 2, out)
                self.assertIn(why, out)
        self.assertEqual([s["id"] for s in self.backlog()["slices"]], ["CLREV1"])

    def test_review_focus_and_spec(self):
        pm = "build/orch/PLAN_PROJECT_MODE.md, build/orch/kb/PM_DECISIONS.md"
        out = self.assert_review(True, "Focus: the PM10 gate\n", "PMA", "--focus", "the PM10 gate")
        self.assertIn(f"Spec: build/conveyor/prompts/PMA.md, build/reports/PMA.md, {pm}.\n", out)
        self.report("CXA", self.sha_a, "jarvis/foo.py")
        out = self.assert_review(True, "Spec: build/conveyor/prompts/CXA.md, build/reports/CXA.md.\n", "CXA",
                                 "--focus", "x")
        self.assertNotIn("PM_DECISIONS", out)
        self.report("PMA", self.sha_a, "jarvis/foo.py", status="BLOCKED (partial)", extra="Worker: codex\n")
        self.report("PMAFIX", self.sha_b, "jarvis/foo.py", extra=f"BASE: PMA AT {self.sha_a}\n")
        self.assert_review(True, "Spec: build/conveyor/prompts/PMA.md, build/reports/PMA.md, "
                                 f"build/conveyor/prompts/PMAFIX.md, build/reports/PMAFIX.md, {pm}.\n",
                           "PMAFIX", "--with-partial", "PMA", "--focus", "x")
        out = self.assert_review(True, "warning: no --focus", "PMAFIX", "--with-partial", "PMA")
        self.assertIn(f"Focus: {orch_gen.FOCUS_PLACEHOLDER}\n", out)
        for argv, why in ((("--write",), "focus required"),
                          (("--write", "--focus", "  "), "focus required"),
                          (("--focus", "a\nb"), "focus must be one non-empty line"),
                          (("--focus", "a Hard rules: push"), "focus must be one non-empty line"),
                          (("--write", "--focus", "a\x1b[2Jb"), "focus must be one non-empty line")):
            with self.subTest(argv):
                self.assert_review(False, why, "PMAFIX", "--with-partial", "PMA", *argv)
        self.assertEqual(self.backlog()["slices"], [])
        self.assertEqual(list((self.root / "build/conveyor/prompts").iterdir()), [])

    def test_groups_add_importer_replay_and_memory_tests(self):
        for name, text in (("imp_a", "import jarvis.foo as f\n"), ("imp_b", "from jarvis.foo import x\n"),
                           ("imp_c", "def t():\n    from jarvis import foo, bar\n"),
                           ("imp_d", "# import jarvis.foo\nimport jarvis.foobar\nx = 'from jarvis import foo'\n"),
                           ("foo_replay", "pass\n"), ("bar_replay", "from jarvis.foo.sub import y\n"),
                           ("baz_replay", "import jarvis\n"), ("memory_x", "pass\n"),
                           ("store", "from jarvis.memory import MemoryStore\n"), ("ui", "import jarvis.foo\n"), ("bom", "﻿import os\n"),
                           ("broken", "def (\n")):
            self.put(f"tests/test_{name}.py", text)
        g = orch_gen.Gen(self.root)
        imp = ["tests.test_bar_replay", "tests.test_broken", "tests.test_foo_replay", "tests.test_imp_a",
               "tests.test_imp_b", "tests.test_imp_c"]
        self.assertEqual(g.test_groups(["jarvis/foo.py"]), [["tests.test_foo", *imp], ["tests.test_ui"]])
        self.assertEqual(g.test_groups(["jarvis/foo.py", "tests/test_imp_c.py"]),
                         [["tests.test_foo", "tests.test_imp_c", *(m for m in imp if m != "tests.test_imp_c")],
                          ["tests.test_ui"]])
        self.assertEqual(g.test_groups(["jarvis/foobar.py"]), [["tests.test_broken", "tests.test_imp_d"]])
        mem = [["tests.test_broken", "tests.test_memory_x", "tests.test_store"]]
        self.assertEqual(g.test_groups(["jarvis/contracts.py"]), mem)
        self.assertEqual(g.test_groups(["jarvis/memory.py"]), mem)
        self.assertEqual(g.test_groups(["README.md"]), [])

    def test_ui_files_add_test_ui(self):
        g = orch_gen.Gen(self.root)
        self.assertEqual(g.test_groups(["jarvis/foo.py"]), [["tests.test_foo"]])
        self.assertEqual(g.test_groups(["jarvis/ui/qml/Main.qml", "jarvis/foo.py"]),
                         [["tests.test_foo"], ["tests.test_ui"]])
        self.assertEqual(g.test_groups(["jarvis/nothing.py", "README.md"]), [])

    # QUEUEFILL1: fill = an approved READY spec -> a slice when a track runs low
    T = 1_800_000_000.0
    DEC = "D-20261124-0000-FILL"

    def fill_state(self, n6_ready=0, lanes=None):
        view = [{"id": f"X{i}", "state": "n6"} for i in range(n6_ready)]
        self.put("build/conveyor/status.json", json.dumps({"backlog": view}))
        self.put("build/conveyor/lanes.json", json.dumps(lanes or {}))
        if not (self.root / orch_decisions.REL).exists():
            orch_decisions.add(self.DEC, "authority", "test", "2026-11-24", path=self.root / orch_decisions.REL)

    def ready(self, track, seq, sid, **over):
        sp = {"id": sid, "title": ("[N6] " if track == "n6" else "[IYILESTIRME] ") + sid, "tier": "T2",
              "model": "opus", "effort": "high", "size": "MEDIUM", "goal": "g", "tests": "a test",
              "run": ["tests.test_foo"], "owned": ["jarvis/foo.py"], "commit": f"feat: {sid} x",
              "track": track, "priority": 1, "approved_by": "orchestrator", "decision": self.DEC}
        sp.update(over)
        rel = f"build/orch/ready/{track}/{seq:03d}-{sid}.json"
        self.put(rel, json.dumps({k: v for k, v in sp.items() if v is not None}))
        return self.root / rel

    def fill(self, *argv):
        out = []
        rc = orch_gen.main(["--root", str(self.root), "fill", *argv], out=out.append, now=lambda: self.T)
        return rc, "\n".join(out)

    def fill_log(self):
        return [json.loads(x) for x in (self.root / "build/orch/ready/fill.jsonl").read_text("utf-8").splitlines()]

    def test_fill_low_n6_is_picked_before_improve(self):
        self.fill_state()
        self.ready("improve", 1, "IM1", priority=9)
        self.ready("n6", 2, "NB1")
        self.ready("n6", 1, "NA1")  # same priority, older seq wins
        rc, out = self.fill()
        self.assertEqual(rc, 0, out)
        self.assertIn("picked n6/001-NA1.json", out)
        self.assertEqual(self.fill_log()[-1]["result"], "dry_run")
        self.assertEqual(self.fill_log()[-1]["reason"], "queue_low_n6")

    def test_fill_improve_only_when_n6_is_not_low(self):
        self.fill_state(n6_ready=2)
        self.ready("n6", 1, "NA1")
        self.ready("improve", 1, "IM1")
        rc, out = self.fill("--track", "n6")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fill_log()[-1]["result"], "no_track_low")
        rc, out = self.fill()
        self.assertEqual(rc, 0, out)
        self.assertIn("picked improve/001-IM1.json", out)

    def test_fill_skips_unapproved_inactive_and_malformed(self):
        self.fill_state()
        orch_decisions.add("D-20261124-0001-OLD", "authority", "test", "x", path=self.root / orch_decisions.REL)
        orch_decisions.deactivate("D-20261124-0001-OLD", path=self.root / orch_decisions.REL)
        self.ready("n6", 1, "NU1", priority=9, approved_by="somebody")
        self.ready("n6", 2, "NI1", priority=9, decision="D-20261124-0001-OLD")
        self.ready("n6", 3, "NM1", priority="9")
        self.ready("n6", 4, "NT1", priority=9, title="no prefix")
        self.ready("n6", 5, "NS1", priority=9, size=None)  # lacks a slice spec field
        self.put("build/orch/ready/n6/bad name.json", "{}")
        self.ready("n6", 6, "NG1")
        rc, out = self.fill()
        self.assertEqual(rc, 0, out)
        self.assertIn("picked n6/006-NG1.json", out)
        self.assertEqual(sorted(self.fill_log()[-1]["skipped"]),
                         ["001-NU1.json:unapproved", "002-NI1.json:inactive_decision", "003-NM1.json:malformed",
                          "004-NT1.json:malformed", "005-NS1.json:malformed", "<bad-name>:malformed"])

    def test_fill_skips_a_spec_overlapping_a_running_lane_or_a_queued_slice(self):
        self.fill_state(lanes={"L1": {"id": "L1", "state": "working", "files": ["jarvis/foo.py"]}})
        self.ready("n6", 1, "NA1", priority=5)
        self.ready("n6", 2, "NB1", owned=["jarvis/other.py"])
        self.ready("n6", 3, "NC1", owned=["jarvis/other.py"])
        rc, out = self.fill("--write")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fill_log()[-1]["skipped"], ["001-NA1.json:overlap"])
        rc, out = self.fill()  # NB1 is now queued (no lane, no report): NC1 overlaps it
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.fill_log()[-1]["skipped"], ["001-NA1.json:overlap", "003-NC1.json:overlap"])
        self.assertEqual(self.fill_log()[-1]["result"], "queue_empty")

    def test_fill_daily_cap_refuses(self):
        self.fill_state()
        p = self.ready("n6", 1, "NA1")
        day = orch_gen.time.strftime("%Y-%m-%d", orch_gen.time.gmtime(self.T))
        self.put("build/orch/gen_caps.json", json.dumps({f"slice:{day}": orch_gen.DAILY_CAP["slice"]}))
        rc, out = self.fill("--write")
        self.assertEqual(rc, 2, out)
        self.assertIn("daily slice cap", out)
        self.assertTrue(p.exists())
        self.assertEqual(self.backlog()["slices"], [])
        self.assertEqual(self.fill_log()[-1]["reason"], "cap_reached")

    def test_fill_write_moves_the_spec_and_adds_one_slice(self):
        self.fill_state()
        p = self.ready("n6", 1, "NA1")
        rc, out = self.fill("--write")
        self.assertEqual(rc, 0, out)
        self.assertEqual([s["id"] for s in self.backlog()["slices"]], ["NA1"])
        self.assertEqual(sorted(x.name for x in (self.root / "build/conveyor/prompts").iterdir()), ["NA1.md"])
        self.assert_lints("NA1")
        self.assertFalse(p.exists())
        day = orch_gen.time.strftime("%Y-%m-%d", orch_gen.time.gmtime(self.T))
        self.assertTrue((self.root / f"build/orch/ready/used/{day}/001-NA1.json").is_file())
        log = self.fill_log()
        self.assertEqual(len(log), 1)
        self.assertEqual({k: log[0][k] for k in ("track", "spec", "result", "reason")},
                         {"track": "n6", "spec": "001-NA1.json", "result": "written", "reason": "queue_low_n6"})
        self.assertEqual(orch_gen.daily_count(self.root, "slice", self.T), 1)

    def test_fill_dry_run_changes_nothing(self):
        self.fill_state()
        p = self.ready("n6", 1, "NA1")
        before = (self.root / "build/conveyor/backlog.json").read_bytes()
        rc, out = self.fill()
        self.assertEqual(rc, 0, out)
        self.assertIn("dry-run", out)
        self.assertEqual((self.root / "build/conveyor/backlog.json").read_bytes(), before)
        self.assertEqual(list((self.root / "build/conveyor/prompts").iterdir()), [])
        self.assertTrue(p.exists())
        self.assertFalse((self.root / "build/orch/ready/used").exists())
        self.assertFalse((self.root / "build/orch/gen_caps.json").exists())
        self.assertEqual(len(self.fill_log()), 1)

    def test_fill_nothing_ready_is_queue_empty(self):
        self.fill_state()
        rc, out = self.fill("--write")
        self.assertEqual(rc, 1, out)
        self.assertIn("queue_empty", out)
        self.assertEqual(self.fill_log()[-1]["result"], "queue_empty")
        self.assertEqual(self.backlog()["slices"], [])


class DepotFillTest(unittest.TestCase):
    """QUEUEFILL2 depot-fill: sha-bound approvals, deterministic head, lock, intent/receipt (injected clock)."""
    T = 1_800_000_000.0
    setUp_repo = OrchGenTest.setUp
    git, put, commit, report = OrchGenTest.git, OrchGenTest.put, OrchGenTest.commit, OrchGenTest.report

    def setUp(self):
        self.setUp_repo()
        self.put("build/conveyor/status.json", '{"backlog": []}')
        self.put("build/conveyor/lanes.json", "{}")
        self.bl = self.root / "build/conveyor/backlog.json"
        self.run_dir = self.root / "build/orch/specs/_run"
        self.day = orch_gen.datetime.fromtimestamp(self.T, orch_gen.timezone.utc).strftime("%Y-%m-%d")

    def spec(self, sid, cls="improve", order=1, approve=True, **kw):
        sp = {"id": sid, "class": cls, "tier": "T2", "order": order, "objective": f"do {sid}",
              "owned_files": ["jarvis/foo.py", "tests/test_foo.py"], "acceptance": ["it works"],
              "negative_tests": [], "depends_on": [], "worker": "opus", "effort": "high", "contracts": []}
        sp.update(kw)
        raw = json.dumps(sp).encode()
        self.put(f"build/orch/specs/{sid}.json", raw.decode())
        if approve:
            self.put(f"build/orch/specs/approvals/{sid}.json", json.dumps({
                "spec_id": sid, "sha256": orch_gen.hashlib.sha256(raw).hexdigest(), "approver": "orchestrator",
                "at": f"2026-11-24T10:00:0{order % 10}Z"}))
        return sp

    def fill(self, *argv, git=orch_gen.real_git):
        out = []
        rc = orch_gen.main(["--root", str(self.root), "depot-fill", *argv], git=git, out=out.append, now=lambda: self.T)
        return rc, "\n".join(out)

    def ids(self):
        return [s["id"] for s in json.loads(self.bl.read_text(encoding="utf-8"))["slices"]]

    def inbox(self):
        p = self.run_dir / "inbox.json"
        return [i["code"] for i in json.loads(p.read_text(encoding="utf-8"))["items"]] if p.is_file() else []

    def state(self):
        return json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))

    def assert_untouched(self, *argv, rc=0, has=""):
        before = self.bl.read_bytes()
        r, out = self.fill(*argv)
        self.assertEqual((r, self.bl.read_bytes()), (rc, before), out)
        self.assertIn(has, out)
        return out

    def test_dry_run_is_default_and_writes_nothing(self):  # P5
        self.spec("DQA")
        out = self.assert_untouched(has="dry-run; nothing written")
        self.assertFalse(self.run_dir.exists())
        self.assertIn("@jarvis lane=DQA", out)

    def test_write_orders_n6_first_and_binds_origin_sha(self):  # P1 N5 N16 P2 P4
        self.spec("DQI2", order=2)
        self.spec("DQI1", order=1)
        self.spec("DQI3", order=3)
        self.spec("DQN", cls="n6", order=9)
        rc, out = self.fill("--write")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ids(), ["DQN", "DQI1", "DQI2"])  # N6 first, then order; at most 2 per round
        head = (self.root / "build/conveyor/prompts/DQI1.md").read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(head.endswith(f"base={self.base}"), head)
        st = self.state()
        self.assertEqual((st["DQI1"]["state"], st["DQI1"]["slice"], st.get("DQI3")), ("CONSUMED", "DQI1", None))
        self.assertFalse(list((self.root / "build/conveyor/hold").glob("backlog.json.bak-DQI3")))
        self.assert_untouched("--write", has="improve has 2 ready")  # P4 re-run: written slices count as ready

    def test_negative_cases_leave_backlog_identical(self):
        w = ("--write", "--class", "improve")
        self.spec("DQS")
        self.spec("DQS", approve=False, objective="edited after approval")
        self.assertIn("DQS: N1", self.assert_untouched(*w))  # N1 approval sha256 no longer matches
        self.spec("DQU", approve=False)
        self.assertIn("DQU: N2 no approval", self.assert_untouched(*w))
        self.spec("DQD", depends_on=["NOTMERGED"])
        self.assertIn("DQD: N6", self.assert_untouched(*w))
        self.assertEqual(self.inbox(), ["spec_needed:improve"])  # N6: one item per outage
        self.assert_untouched(*w)
        self.assertEqual(self.inbox(), ["spec_needed:improve"])
        self.assertIn("scripts/conveyor.py", conveyor.PROTECTED_FILES)
        self.spec("DQP", tier="T1", owned_files=["scripts/conveyor.py"])
        self.assertIn("DQP: N12", self.assert_untouched(*w))  # P3: T1 + protected file refused
        self.assertEqual(self.state()["DQP"]["state"], "REJECTED")

    def test_n11_consumed_and_taken_ids_are_skipped(self):
        self.spec("DQC")
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "state.json").write_text('{"DQC": {"state": "CONSUMED", "slice": "DQC"}}', encoding="utf-8")
        self.assertIn("DQC: N11 CONSUMED", self.assert_untouched("--write"))
        self.spec("PMA")  # an id already in the backlog is a duplicate
        self.bl.write_text('{"slices": [{"id": "PMA", "title": "x"}]}', encoding="utf-8")
        self.assertIn("PMA: N11 already in the backlog", self.assert_untouched("--write"))

    def test_stale_trigger_is_noop_without_inbox(self):  # N3
        self.spec("DQA")
        self.bl.write_text(json.dumps({"slices": [{"id": i, "title": f"[IYILESTIRME] {i}"} for i in ("R1", "R2")]}),
                           encoding="utf-8")
        self.put("build/conveyor/status.json", json.dumps({"backlog": [{"id": "R1", "state": "ready"},
                                                                        {"id": "R2", "state": "ready"}]}))
        self.assert_untouched("--write", "--class", "improve", has="nothing to fill")
        self.assertEqual(self.inbox(), [])

    def test_propose_must_be_the_class_head(self):  # N4
        self.spec("DQ1", order=1)
        self.spec("DQ2", order=2)
        self.assertIn("N4", self.assert_untouched("--write", "--propose", "DQ2", rc=2))
        self.assertIn("N4", self.assert_untouched("--write", "--class", "n6", "--propose", "DQ1", rc=2))

    def test_check_failure_rolls_back_then_rejects(self):  # N7
        self.spec("DQF")
        self.put("build/orch/preflight.py", "import sys\nsys.exit(1)\n")
        for n, st in ((1, "READY"), (2, "REJECTED")):
            self.assert_untouched("--write", "--class", "improve", rc=1)
            self.assertEqual((self.state()["DQF"]["attempts"], self.state()["DQF"]["state"]), (n, st))
            self.assertFalse((self.root / "build/conveyor/prompts/DQF.md").exists())
            self.assertFalse(list((self.root / "build/conveyor/hold").glob("backlog.json.bak-DQF")))

    def test_daily_cap(self):  # N8
        self.spec("DQA")
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "caps.json").write_text(json.dumps({f"improve:{self.day}": 2}), encoding="utf-8")
        self.assert_untouched("--write", "--class", "improve", has="N8 daily cap")

    def test_intent_without_receipt_reconciles_never_retries(self):  # N9
        self.spec("DQA")
        self.spec("DQB", order=2)
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "journal.jsonl").write_text(
            '{"kind": "intent", "id": "DQA"}\n{"kind": "intent", "id": "PMX"}\n', encoding="utf-8")
        self.bl.write_text('{"slices": [{"id": "PMX", "title": "[N6] x"}]}', encoding="utf-8")
        rc, out = self.fill("--write", "--class", "improve")
        self.assertEqual(rc, 0, out)
        st = self.state()
        self.assertEqual((st["DQA"]["state"], st["DQA"]["reason"], st["PMX"]["state"]),
                         ("REJECTED", "UNKNOWN_OUTCOME", "CONSUMED"))
        self.assertIn("unknown_outcome:DQA", self.inbox())
        self.assertEqual(self.ids(), ["PMX", "DQB"])  # exactly one new slice; DQA is never retried

    def test_lock_and_moved_base_refuse(self):  # N10 N16
        self.spec("DQA")
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "fill.lock").write_text("", encoding="utf-8")
        self.assertIn("N10", self.assert_untouched("--write", rc=2))
        (self.run_dir / "fill.lock").unlink()
        calls = []

        def moving(root, *args):
            if args[:1] == ("rev-parse",) and "refs/remotes/origin/main" in args:
                calls.append(1)
                return 0, ("a" if len(calls) == 1 else "b") * 40 + "\n"
            return orch_gen.real_git(root, *args)
        before = self.bl.read_bytes()
        rc, out = self.fill("--write", git=moving)
        self.assertEqual((rc, self.bl.read_bytes()), (2, before), out)
        self.assertIn("N10/N16", out)
        self.assertFalse((self.run_dir / "fill.lock").exists())
        rc, out = self.fill("--write", git=lambda root, *a: (0, "main\n") if a[:1] == ("rev-parse",)
                            else orch_gen.real_git(root, *a))
        self.assertEqual((rc, self.bl.read_bytes()), (2, before), out)
        self.assertIn("N16", out)

    def old_lock(self, age, pid=4242):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "fill.lock").write_text(json.dumps({"pid": pid, "at": self.T - age}), encoding="utf-8")

    def test_stale_lock_of_a_dead_pid_is_reclaimed_once(self):  # QUEUEFILL2FIX
        self.old_lock(3600)
        with mock.patch.object(orch_gen, "pid_alive", return_value=False):
            rc, out = self.fill("--write", "--class", "improve")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("N10", out)
        lines = [json.loads(x) for x in (self.run_dir / "journal.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual([x for x in lines if x["kind"] == "lock_reclaimed"],
                         [{"kind": "lock_reclaimed", "pid": 4242, "at": "2027-01-15T08:00:00Z"}])
        self.assertFalse((self.run_dir / "fill.lock").exists())

    def test_live_young_or_malformed_lock_still_refuses(self):  # QUEUEFILL2FIX
        self.spec("DQA")
        for alive, body in ((True, {"pid": 4242, "at": self.T - 3600}), (False, {"pid": 4242, "at": self.T - 60}),
                            (False, {"pid": True, "at": self.T - 3600}), (False, {"pid": 4242, "at": "-inf"}),
                            (False, {"pid": 4242, "at": float("-inf")}), (False, [4242, 0]), (False, {"pid": -1, "at": 0})):
            with self.subTest(alive=alive, body=body):
                self.run_dir.mkdir(parents=True, exist_ok=True)
                (self.run_dir / "fill.lock").write_text(json.dumps(body), encoding="utf-8")
                with mock.patch.object(orch_gen, "pid_alive", return_value=alive):
                    self.assertIn("N10", self.assert_untouched("--write", rc=2))
                self.assertTrue((self.run_dir / "fill.lock").exists())
                self.assertFalse((self.run_dir / "journal.jsonl").exists())

    def test_pid_alive_sees_this_process(self):  # a ctypes slip would make every lock look dead
        self.assertTrue(orch_gen.pid_alive(os.getpid()))

    def test_strict_schema_rejects_free_text(self):  # N13
        good = json.loads(json.dumps(self.spec("DQA", approve=False)))
        for bad in ({**good, "note": "x"}, {**good, "objective": "a\nb"}, {**good, "order": "1"},
                    {**good, "owned_files": ["../x; rm"]}, {**good, "objective": "x" * 400},
                    {**good, "acceptance": []}):
            with self.assertRaises(orch_gen.Refuse):
                orch_gen.parse_spec(json.dumps(bad).encode(), "DQA")
        with self.assertRaises(orch_gen.Refuse):
            orch_gen.parse_spec(b"{" + b" " * orch_gen.SPEC_MAX + b"}", "DQA")
        with self.assertRaises(orch_gen.Refuse):
            orch_gen.parse_spec(b'{"id": "DQA", "id": "DQA"}', "DQA")

    def test_empty_depot_raises_queue_empty_once(self):  # N15
        self.assert_untouched("--write")
        self.assert_untouched("--write")
        self.assertEqual(self.inbox(), ["queue_empty"])


if __name__ == "__main__":
    unittest.main()
