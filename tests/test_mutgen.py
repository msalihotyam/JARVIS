"""Tests for scripts/mutgen.py (deterministic mutation spec generator)."""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mutgen = _load("mutgen")
mutate = _load("mutate")

FIXTURE = '''def f(a, b):
    """a == b and True in a docstring are never mutated"""
    if a == b:
        return 1
    if not a:
        raise ValueError("x")
    flag = True
    return a < b  # a > b in a comment
'''

AMBIGUOUS = '''def g(x, y):
    if x == y:
        pass
    if x == y:
        pass
'''


class MutgenTests(unittest.TestCase):
    def test_each_operator_yields_a_unique_applicable_spec(self):
        specs, stats = mutgen.generate(FIXTURE, "scripts/fx.py", ["tests.test_fx"])
        self.assertEqual({s["name"].split("-")[0] for s in specs}, set(mutgen.OPS))
        data = FIXTURE.encode()
        for s in specs:
            self.assertEqual(FIXTURE.count(s["old"]), 1, s["name"])
            self.assertIsNotNone(mutate.apply_text(data, s["old"], s["new"])[0], s["name"])
            self.assertEqual((s["file"], s["tests"]), ("scripts/fx.py", ["tests.test_fx"]))
            self.assertNotIn("docstring", s["old"])
        cmp8 = [s["new"] for s in specs if s["name"] == "cmp-L8C13"]
        self.assertEqual(cmp8, ["    return a <= b  # a > b in a comment"])
        self.assertEqual((stats["ambiguous"], stats["invalid"], stats["capped"]), (0, 0, 0))

    def test_ambiguous_lines_are_skipped_and_counted(self):
        specs, stats = mutgen.generate(AMBIGUOUS, "scripts/fx.py", ["t"])
        self.assertEqual(specs, [])
        self.assertEqual(stats["ambiguous"], 2)

    def test_same_input_gives_byte_identical_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "fx.py").write_text(FIXTURE, encoding="utf-8")
            outs = []
            for n in (1, 2):
                out = Path(tmp) / f"o{n}.json"
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = mutgen.main(["scripts/fx.py", "--repo", tmp, "--tests", "tests.t",
                                      "--out", str(out)])
                self.assertEqual(rc, 0)
                outs.append(out.read_bytes())
        self.assertEqual(outs[0], outs[1])
        self.assertTrue(outs[0].startswith(b"[\n"))

    def test_cap_is_enforced_and_keeps_every_operator(self):
        text = "".join(f"n{i:03d} = {i + 1000}\n" for i in range(100))
        text += "".join(f"c{i} = n000 == {i + 2000}\n" for i in range(3))
        for cap in (60, 500):
            specs, stats = mutgen.generate(text, "scripts/fx.py", ["t"], cap=cap)
            self.assertEqual(len(specs), 60)
            self.assertEqual(stats["capped"], 106 - 60)
            self.assertEqual(sum(s["name"].startswith("cmp-") for s in specs), 3)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mutgen.main(["scripts/fx.py", "--tests", "t", "--cap", "61"])

    def test_file_without_final_newline_does_not_crash(self):  # CDXINT139 F8
        full, _ = mutgen.generate(FIXTURE, "scripts/fx.py", ["t"])
        bare, _ = mutgen.generate(FIXTURE.rstrip("\n"), "scripts/fx.py", ["t"])
        self.assertEqual([s["name"] for s in bare], [s["name"] for s in full])
        for text in ("", "# only a comment", "x = 1   ", "def f():\n    if x:\n        return 1",
                     "class C:\n    def m(self):\n        raise E"):
            with self.subTest(text=text[:30]):
                mutgen.generate(text, "scripts/fx.py", ["t"])
        with self.assertRaises(ValueError):  # lone CR: ast and split disagree on rows
            mutgen.generate("x = 1\ry = 2\n", "scripts/fx.py", ["t"])

    def _run_main(self, repo, rel, *extra):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as cm:
            mutgen.main([rel, "--repo", repo, "--tests", "t", "--out", str(Path(repo) / "o.json"),
                         *extra])
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse((Path(repo) / "o.json").exists())
        return err.getvalue()

    def test_targets_outside_the_repo_are_refused(self):  # CDXINT139 F9
        with tempfile.TemporaryDirectory() as tmp:
            repo, outside = Path(tmp) / "repo", Path(tmp) / "outside"
            (repo / "scripts").mkdir(parents=True)
            outside.mkdir()
            (outside / "x.py").write_text("x = 1\n", encoding="utf-8")
            (repo / "data").mkdir()
            (repo / "data" / "d.py").write_text("x = 1\n", encoding="utf-8")
            (repo / ".git").mkdir()
            ok = repo / "scripts" / "ok.py"
            ok.write_text("x = 1\n", encoding="utf-8")
            link = repo / "scripts" / "link"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except OSError:  # no symlink privilege on Windows: a junction needs none
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            for rel in ("../outside/x.py", "scripts/../../outside/x.py", "scripts/link/x.py",
                        "data/d.py", "scripts/missing.py", "scripts"):
                with self.subTest(rel=rel):
                    self.assertIn("error:", self._run_main(str(repo), rel))
            self.assertIn("protected", self._run_main(str(repo / "data"), "d.py"))
            for out in (ok, repo / "data" / "o.json", repo / "missing" / "o.json"):
                with self.subTest(out=out.name):
                    self.assertIn("--out", self._run_main(str(repo), "scripts/ok.py", "--out", str(out)))
            self.assertEqual(ok.read_text(encoding="utf-8"), "x = 1\n")
            self.assertFalse((repo / "data" / "o.json").exists())

    def test_original_must_compile(self):  # CDXINT139 F10
        for text in ("return 1\n", "yield 1\n", "nonlocal x\n", "x = (\n", "x = 1\0\n"):
            with self.subTest(text=text[:20]), self.assertRaises((SyntaxError, ValueError)):
                mutgen.generate(text, "scripts/fx.py", ["t"])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "fx.py").write_text("x = 1\nreturn x\n", encoding="utf-8")
            self.assertIn("does not compile", self._run_main(tmp, "scripts/fx.py"))
            deep = Path(tmp) / "scripts" / "deep.py"
            deep.write_text("x = " + "(" * 100000 + "1" + ")" * 100000 + "\n", encoding="utf-8")
            self.assertIn("does not compile", self._run_main(tmp, "scripts/deep.py"))
            (Path(tmp) / "scripts" / "l1.py").write_bytes(b"s = '\xe9'\n")
            self.assertIn("not UTF-8", self._run_main(tmp, "scripts/l1.py"))
            (Path(tmp) / "scripts" / "bom.py").write_bytes(b"\xef\xbb\xbfx = 1 == 2\n")
            with contextlib.redirect_stderr(io.StringIO()):
                rc = mutgen.main(["scripts/bom.py", "--repo", tmp, "--tests", "t",
                                  "--out", str(Path(tmp) / "b.json")])
            self.assertEqual(rc, 0)
            self.assertIn(b"x = 1 != 2", (Path(tmp) / "b.json").read_bytes())
        mutgen.generate("x = " + "1" * 4300 + "\n", "scripts/fx.py", ["t"])  # int() digit limit

    def test_output_is_identical_across_hash_seeds(self):  # CDXINT139 F12
        text = FIXTURE + "".join(f"n{i:03d} = {i + 1000} if a != {i} else True\n"
                                 for i in range(40))
        outs = []
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "fx.py").write_text(text, encoding="utf-8")
            for seed in ("0", "1"):
                env = dict(os.environ, PYTHONHASHSEED=seed)
                r = subprocess.run([sys.executable, str(_ROOT / "scripts" / "mutgen.py"),
                                    "scripts/fx.py", "--repo", tmp, "--tests", "t"],
                                   capture_output=True, env=env, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr[-200:])
                outs.append((r.stdout, r.stderr))
        self.assertIn(b"capped=", outs[0][1])
        self.assertNotIn(b"capped=0 ", outs[0][1])
        self.assertEqual(outs[0], outs[1])


if __name__ == "__main__":
    unittest.main()
