"""REDFIRST: scripts/red_first.py on its own temp git repos (never this repo, never data/)."""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import red_first  # noqa: E402

CALC = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b\n"  # mul is buggy
TEST = (
    "import unittest\nimport calc\n\n\nclass CalcTests(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n"
)


def git(repo, *args):
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args]
    return subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, check=True)


class RedFirstTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="redfirst-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo, self.scratch = self.root / "repo", self.root / "scratch"
        self.repo.mkdir()
        self.scratch.mkdir()
        git(self.repo, "init", "-q")
        self.commit("base", {"calc.py": CALC, "test_calc.py": TEST})

    def commit(self, msg, files):
        for name, text in files.items():
            (self.repo / name).write_bytes(text.encode())
        git(self.repo, "add", *files)
        git(self.repo, "commit", "-q", "-m", msg)

    def run_main(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = red_first.main(["--repo", str(self.repo), "--scratch-root", str(self.scratch),
                                   "--label", "t"])
        return code, buf.getvalue()

    def assert_clean(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        listing = git(self.repo, "worktree", "list").stdout.strip().splitlines()
        self.assertEqual(len(listing), 1, listing)

    def test_new_test_guarding_a_fix_is_red_on_the_parent(self):
        self.commit("fix mul", {
            "calc.py": CALC[:CALC.rindex("a + b")] + "a * b\n",
            "test_calc.py": TEST + "    def test_mul(self):\n        self.assertEqual(calc.mul(2, 3), 6)\n",
        })
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("test_calc.CalcTests.test_mul RED", out)
        self.assertNotIn("test_add", out)  # unchanged tests are not run
        self.assertIn("RESTORED: True", out)
        self.assert_clean()

    def test_test_passing_on_the_parent_is_green_and_exits_1(self):
        self.commit("add neg", {
            "calc.py": CALC + "\n\ndef neg(a):\n    return -a\n",
            "test_calc.py": TEST + "    def test_add_again(self):\n        self.assertEqual(calc.add(2, 2), 4)\n",
        })
        code, out = self.run_main()
        self.assertEqual(code, 1, out)
        self.assertIn("test_calc.CalcTests.test_add_again GREEN", out)
        self.assert_clean()

    def test_test_only_commit_is_na(self):
        self.commit("more tests", {
            "test_calc.py": TEST + "    def test_zero(self):\n        self.assertEqual(calc.add(0, 0), 0)\n",
        })
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("N/A: test-only commit", out)
        self.assert_clean()

    def test_source_change_without_new_test_exits_1(self):
        self.commit("add neg", {"calc.py": CALC + "\n\ndef neg(a):\n    return -a\n"})
        code, out = self.run_main()
        self.assertEqual(code, 1, out)
        self.assertIn("NO NEW TESTS", out)
        self.assert_clean()

    def test_worktree_removed_after_a_failure(self):
        self.commit("fix mul", {
            "calc.py": CALC + "\n\ndef neg(a):\n    return -a\n",
            "test_calc.py": TEST + "    def test_neg(self):\n        self.assertEqual(calc.neg(1), -1)\n",
        })
        with mock.patch.object(red_first, "run_tests", side_effect=RuntimeError("boom")):
            code, out = self.run_main()
        self.assertEqual(code, 3, out)
        self.assertIn("ERROR: RuntimeError: boom", out)
        self.assert_clean()
        self.assertEqual((self.repo / "calc.py").read_bytes(), (CALC + "\n\ndef neg(a):\n    return -a\n").encode())

    def test_only_testcase_methods_run_and_unloadable_ids_are_error(self):  # CLREV F1
        self.commit("fix mul", {
            "calc.py": CALC[:CALC.rindex("a + b")] + "a * b\n",
            "test_calc.py": TEST + (
                "\n\nclass Mixin:\n    def test_mul(self):\n        self.assertEqual(calc.mul(2, 3), 6)\n"
                "\n\nclass MixTests(Mixin, unittest.TestCase):\n    @staticmethod\n    def test_s():\n        pass\n"
                "\n\nclass Ödd(dict):\n    def test_x(self):\n        pass\n"),
        })
        code, out = self.run_main()
        self.assertEqual(code, 3, out)
        self.assertIn("test_calc.MixTests.test_mul RED", out)
        self.assertNotIn("import/syntax", out)
        self.assertIn("test_calc.Ödd.test_x ERROR", out)
        self.assertIn("test_calc.MixTests.test_s ERROR", out)
        self.assertNotIn("Mixin.test", out)
        self.assert_clean()
        cyc = "class A(B):\n    def test_a(self): pass\nclass B(A, unittest.TestCase):\n    pass\n"
        self.assertEqual(red_first.changed_ids("t.py", None, cyc), ["t.A.test_a", "t.B.test_a"])

    def test_unexpected_error_is_one_line_exit_3(self):  # CLREV F2
        with mock.patch.object(red_first, "run", side_effect=OSError("disk\nfull ş")):
            code, out = self.run_main()
        self.assertEqual((code, out), (3, "ERROR: OSError: disk full \\u015f\n"))

    def test_changed_ids_only_added_or_changed_methods(self):
        new = TEST.replace("calc.add(1, 2), 3", "calc.add(2, 1), 3") + "    def test_x(self):\n        pass\n"
        self.assertEqual(red_first.changed_ids("tests/test_calc.py", TEST, new),
                         ["tests.test_calc.CalcTests.test_add", "tests.test_calc.CalcTests.test_x"])
        self.assertEqual(red_first.changed_ids("test_calc.py", TEST, TEST), [])
        self.assertEqual([red_first.is_test_file(p) for p in
                          ("tests/test_a.py", "tests/helper.py", "test_a.py", "scripts/test_parallel.py", "calc.py")],
                         [True, True, True, False, False])


if __name__ == "__main__":
    unittest.main()
