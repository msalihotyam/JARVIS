"""ORCHGEN: scripts/orch_gen.py on a temp repo with fake reports (never the real backlog)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import conveyor  # noqa: E402
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
                rc, out = self.run_gen("review", "PMA", "--write")
                self.assertEqual(rc, 2, out)
                self.assertIn("REFUSED", out)
                self.assertIn(why, out)
                self.assertEqual(self.backlog()["slices"], [])
        rc, out = self.run_gen("review", "PMA", "PMAFIX")  # Claude + Codex in one review
        self.assertEqual(rc, 2, out)

    def test_write_then_integration_orders_stacked_picks(self):
        rc, out = self.run_gen("review", "PMA", "--write")
        self.assertEqual(rc, 0, out)
        self.assertIn("OK: stub", out)
        self.assertTrue((self.root / "build/conveyor/hold/backlog.json.bak-CDXINT1").is_file())
        self.assert_lints("CDXINT1")
        rc, out = self.run_gen("review", "PMAFIX", "--write")
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
            ({"status": "BLOCKED"}, "not BLOCKED (partial"),
            ({"status": "BLOCKED (not partial: nothing usable)"}, "not BLOCKED (partial"),
        )
        for kw, why in cases:
            with self.subTest(why, **kw):
                setup(**kw)
                self.assert_review(False, why, "PMAFIX", "--with-partial", "PMA")
        setup()
        # "partial" only in a title line, not the STATUS line conveyor.report_status reads
        self.put("build/reports/PMA.md", f"# PMA (prior STATUS: partial)\nSTATUS: BLOCKED\nCOMMIT: {self.sha_a}\n"
                 "FILES: jarvis/foo.py\nWorker: codex\n")
        self.assert_review(False, "not BLOCKED (partial", "PMAFIX", "--with-partial", "PMA")
        setup()
        self.assert_review(False, "report STATUS is BLOCKED, not PASS", "PMAFIX", "PMA")
        self.assert_review(False, "name each slice once", "PMAFIX", "PMA", "--with-partial", "PMA")
        # roles swapped: the later slice is the ancestor, so it does not build on the partial
        self.report("PMAFIX", self.sha_b, "jarvis/foo.py", status="BLOCKED (partial)", extra="Worker: codex\n")
        self.report("PMA", self.sha_a, "jarvis/foo.py", extra=f"Worker: codex\nBASE: PMAFIX AT {self.sha_b}\n")
        self.assert_review(False, "does not build on PMAFIX", "PMA", "--with-partial", "PMAFIX")
        self.assertEqual(self.backlog()["slices"], [])

    def test_ui_files_add_test_ui(self):
        g = orch_gen.Gen(self.root)
        self.assertEqual(g.test_groups(["jarvis/foo.py"]), [["tests.test_foo"]])
        self.assertEqual(g.test_groups(["jarvis/ui/qml/Main.qml", "jarvis/foo.py"]),
                         [["tests.test_foo"], ["tests.test_ui"]])
        self.assertEqual(g.test_groups(["jarvis/nothing.py", "README.md"]), [])


if __name__ == "__main__":
    unittest.main()
