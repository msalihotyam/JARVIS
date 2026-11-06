"""MUTATE: scripts/mutate.py on its own temp git repos (never this repo, never data/)."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mutate  # noqa: E402

CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n\n\ndef dup():\n    return 1 + 1\n"
TEST = (
    "import unittest\nimport calc\n\n\nclass CalcTests(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n"
)
RED_TEST = TEST + "    def test_red(self):\n        self.assertEqual(calc.add(1, 1), 3)\n"
ID = "test_calc.CalcTests.test_add"


def git(repo, *args):
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args]
    return subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, check=True)


class MutateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mutate-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo, self.scratch = self.root / "repo", self.root / "scratch"
        self.repo.mkdir()
        self.scratch.mkdir()

    def make_repo(self, test=TEST):
        git(self.repo, "init", "-q")
        (self.repo / "calc.py").write_bytes(CALC.encode())
        (self.repo / "test_calc.py").write_bytes(test.encode())
        git(self.repo, "add", "calc.py", "test_calc.py")
        git(self.repo, "commit", "-q", "-m", "base")

    def run_main(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mutate.main(["--repo", str(self.repo), "--scratch-root", str(self.scratch),
                                "--label", "t", *argv])
        return code, buf.getvalue()

    def one(self, old, new, file="calc.py", tests=(ID,)):
        return self.run_main("--file", file, "--old", old, "--new", new, "--tests", *tests)

    def assert_clean(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        listing = git(self.repo, "worktree", "list").stdout.strip().splitlines()
        self.assertEqual(len(listing), 1, listing)
        self.assertEqual((self.repo / "calc.py").read_bytes(), CALC.encode())

    # --- exit codes ---
    def test_caught_mutation_exits_0(self):
        self.make_repo()
        code, out = self.one("return a + b", "return a - b")
        self.assertEqual(code, 0, out)
        self.assertIn("M1 CAUGHT", out)
        self.assertIn("M1 RESTORED: True", out)
        self.assert_clean()

    def test_missed_mutation_exits_1(self):
        self.make_repo()
        code, out = self.one("return a - b", "return a + b")  # sub() is not tested
        self.assertEqual(code, 1, out)
        self.assertIn("M1 MISSED", out)
        self.assert_clean()

    def test_zero_matches_exits_2(self):
        self.make_repo()
        code, out = self.one("return a * b", "return 0")
        self.assertEqual(code, 2, out)
        self.assertIn("M1 NOT APPLIED: 0 matches", out)
        self.assert_clean()

    def test_two_matches_exit_2(self):
        self.make_repo()
        code, out = self.one("return a", "return b")  # matches add and sub
        self.assertEqual(code, 2, out)
        self.assertIn("NOT APPLIED: 2 matches", out)
        self.assert_clean()

    def test_empty_old_and_noop_exit_2(self):
        self.make_repo()
        self.assertEqual(self.one("", "x")[0], 2)
        self.assertEqual(self.one("return a + b", "return a + b")[0], 2)
        self.assert_clean()

    def test_red_baseline_exits_3_without_mutating(self):
        self.make_repo(RED_TEST)
        code, out = self.one("return a + b", "return a - b", tests=("test_calc.CalcTests",))
        self.assertEqual(code, 3, out)
        self.assertIn("BASELINE RED", out)
        self.assertNotIn("M1 CAUGHT", out)
        self.assert_clean()

    def test_syntax_error_is_error_caught_not_caught(self):
        self.make_repo()
        code, out = self.one("return a + b", "return (")
        self.assertEqual(code, 0, out)
        self.assertIn("M1 ERROR-CAUGHT", out)
        self.assertNotIn("M1 CAUGHT", out)
        self.assert_clean()

    def test_spec_file_shares_one_worktree(self):
        self.make_repo()
        spec = self.root / "spec.json"
        spec.write_text(json.dumps([
            {"name": "A", "file": "calc.py", "old": "return a + b", "new": "return 0", "tests": [ID]},
            {"name": "B", "file": "calc.py", "old": "return a - b", "new": "return 0", "tests": [ID]},
        ]), encoding="utf-8")
        code, out = self.run_main("--spec", str(spec))
        self.assertEqual(code, 1, out)
        self.assertIn("A CAUGHT", out)
        self.assertIn("B MISSED", out)
        self.assert_clean()

    # --- cleanup on failure ---
    def test_scratch_removed_when_the_test_run_raises(self):
        self.make_repo()
        with mock.patch.object(mutate, "run_tests", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.one("return a + b", "return a - b")
        self.assert_clean()

    def test_scratch_removed_on_ctrl_c_mid_mutation(self):
        self.make_repo()
        calls = []

        def fake(*a, **k):
            calls.append(1)
            if len(calls) == 2:  # baseline passed, the mutated run is interrupted
                raise KeyboardInterrupt
            return 0, ""

        with mock.patch.object(mutate, "run_tests", fake):
            code, out = self.one("return a + b", "return a - b")
        self.assertEqual(code, 3, out)
        self.assertIn("INTERRUPTED", out)
        self.assert_clean()

    def test_unremovable_scratch_is_reported_not_forced(self):
        self.make_repo()
        real = mutate._git

        def no_remove(repo, *args):
            if args[:2] == ("worktree", "remove"):
                assert "--force" not in args
                return subprocess.CompletedProcess(args, 1, "", "locked")
            return real(repo, *args)

        with mock.patch.object(mutate, "_git", no_remove):
            code, out = self.one("return a + b", "return a - b")
        self.assertEqual(code, 3, out)
        self.assertIn("SCRATCH LEFT:", out)
        git(self.repo, "worktree", "remove", "--force", str(next(self.scratch.iterdir())))

    # --- guards ---
    def test_file_escaping_the_worktree_is_refused(self):
        self.make_repo()
        outside = self.root / "outside.py"
        outside.write_bytes(b"x = 1\n")
        for bad in ("../../outside.py", str(outside), "..\\..\\outside.py"):
            code, out = self.one("x = 1", "x = 2", file=bad)
            self.assertEqual(code, 2, out)
            self.assertIn("file escapes the worktree", out)
        self.assertEqual(outside.read_bytes(), b"x = 1\n")
        self.assert_clean()

    def test_protected_and_missing_files_are_refused(self):
        root = self.root / "r"
        (root / "data").mkdir(parents=True)
        for rel in ("data/jarvis.db", "uv.lock", "sub/uv.lock"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(b"x")
            self.assertIsNone(mutate.check_target(root, rel)[0], rel)
        self.assertEqual(mutate.check_target(root, "nope.py")[1], "file not found")

    def test_file_over_64_mib_is_refused(self):
        big, ok = self.root / "big.py", self.root / "ok.py"
        mutate.sparse_file(big, mutate.MAX_BYTES + 1)  # sparse, never written (C11)
        ok.write_bytes(b"x = 1\n")
        self.assertEqual(mutate.check_target(self.root, "big.py"), (None, "file over 64 MiB"))
        self.assertEqual(mutate.check_target(self.root, "ok.py")[1], None)
        with self.assertRaises(ValueError):
            mutate.sparse_file(self.root / "huge", mutate.MAX_BYTES + 2)

    def test_crlf_checkout_still_single_match(self):
        data = b"a = 1\r\nb = 2\r\n"
        self.assertEqual(mutate.apply_text(data, "a = 1\nb = 2", "a = 9\nb = 2"), (b"a = 9\r\nb = 2\r\n", None))
        self.assertEqual(mutate.apply_text(b"x\nx\n", "x", "y"), (None, "2 matches"))

    # --- probe set ---
    def test_probe_set(self):
        git_stub = mutate.fake_git(128, "", "fatal")
        self.assertEqual(git_stub("status").returncode, 128)
        self.assertEqual(git_stub.calls[0][0], ("status",))
        self.assertEqual(len(mutate.grid(["ab"], prefixes=("", "x"), seps=("-", "_"))), 5 * 2 * 2)
        calls = []
        wrapped = mutate.interleave(lambda: calls.append("fn"), 2, lambda: calls.append("inject"))
        wrapped(), wrapped()
        self.assertEqual(calls, ["fn", "inject", "fn"])
        now = mutate.fake_clock(2.0, 10.0)
        self.assertEqual((now(), now(), now()), (10.0, 12.0, 14.0))
        scan = mutate.fake_scandir({"d": {"f": 3}, "g": 5})
        self.assertEqual(sorted(e.name for e in scan(".")), ["d", "g"])
        self.assertEqual([(e.name, e.is_file(), e.stat().st_size) for e in scan("d")], [("f", True, 3)])
        values = mutate.bad_values()
        self.assertIn(True, values)
        self.assertEqual(len(values), 8)
        replaced, winerror, _ = mutate.held_file_replace(self.root)
        self.assertIsInstance(replaced, bool)
        if not replaced:
            self.assertTrue(winerror is None or isinstance(winerror, int))


if __name__ == "__main__":
    unittest.main()
