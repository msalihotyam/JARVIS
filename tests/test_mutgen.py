"""Tests for scripts/mutgen.py (deterministic mutation spec generator)."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual({s["name"]: s["new"] for s in specs}, {  # CDXINT140 F5
            "cmp-L3C9": "    if a != b:",
            "early-L4C8": "        pass",
            "ret-L4C8": "        return None",
            "num-L4C15": "        return 2",
            "not-L5C7": "    if a:",
            "raise-L6C8": "        pass",
            "bool-L7C11": "    flag = False",
            "ret-L8C4": "    return None  # a > b in a comment",
            "cmp-L8C13": "    return a <= b  # a > b in a comment",
        })
        for op, src, new in (("cmp", "x = a >= b\n", "x = a > b"), ("cmp", "x = a < b\n", "x = a <= b"),
                             ("cmp", "x = a == b\n", "x = a != b"), ("cmp", "x = a != b\n", "x = a == b"),
                             ("cmp", "x = a <= b\n", "x = a < b"), ("cmp", "x = a > b\n", "x = a >= b"),
                             ("bool", "x = False\n", "x = True"), ("num", "x = 0\n", "x = 1")):
            with self.subTest(src=src):
                got = mutgen.generate(src, "scripts/fx.py", ["t"])[0]
                self.assertEqual([(s["name"].split("-")[0], s["new"]) for s in got], [(op, new)])
        self.assertEqual((stats["ambiguous"], stats["invalid"], stats["capped"]), (0, 0, 0))

    def test_ambiguous_lines_are_skipped_and_counted(self):
        specs, stats = mutgen.generate(AMBIGUOUS, "scripts/fx.py", ["t"])
        self.assertEqual(specs, [])
        self.assertEqual(stats["ambiguous"], 2)

    def test_same_input_gives_byte_identical_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "fx.py").write_text(FIXTURE, encoding="utf-8")
            outs = [self._ok(["scripts/fx.py", "--repo", tmp, "--tests", "tests.t"])
                    for _ in (1, 2)]
        self.assertEqual(outs[0], outs[1])
        self.assertTrue(outs[0].startswith("[\n"))

    def _ok(self, argv):
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mutgen.main(argv), 0)
        return out.getvalue()

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
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit) as cm:
            mutgen.main([rel, "--repo", repo, "--tests", "t", *extra])
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
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
            self.assertEqual(ok.read_text(encoding="utf-8"), "x = 1\n")

    def test_no_out_option_and_no_file_writes(self):  # MUT1FIX4 (CDXINT140/141 F1-F3 class)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "scripts").mkdir()
            (repo / "tests").mkdir()
            (repo / "scripts" / "ok.py").write_text("x = 1 == 2\n", encoding="utf-8")
            (repo / "tests" / "test_ok.py").write_text("T\n", encoding="utf-8")

            def snapshot():
                return {p: p.read_bytes() if p.is_file() else None for p in repo.rglob("*")}

            before = snapshot()
            for opt in ("--out", "--ou", "--o", "--out=o.json"):
                with self.subTest(opt=opt):
                    argv = [opt] if "=" in opt else [opt, str(repo / "o.json")]
                    self.assertIn("unrecognized", self._run_main(tmp, "scripts/ok.py", *argv))
            out = self._ok(["scripts/ok.py", "--repo", tmp])  # default tests.test_ok
            self.assertEqual(snapshot(), before)  # stdout only: nothing created or changed
            self.assertIn("x = 1 != 2", out)
            self.assertEqual(json.loads(out)[0]["tests"], ["tests.test_ok"])

    def test_bad_test_id_exits_2(self):  # CDXINT141 F4
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "ok.py").write_text("x = 1 == 2\n", encoding="utf-8")
            for bad in ("/", "", ".", "a..b", "a.", ".a", "a b", "tests/test_x.py",
                        "tests.test-x", "1abc", "a\x00b", "tests.test_x\n"):
                with self.subTest(bad=bad):
                    err = self._run_main(tmp, "scripts/ok.py", "--tests", "tests.ok", bad)
                    self.assertIn("not a dotted test id", err)
            out = self._ok(["scripts/ok.py", "--repo", tmp, "--tests", "tests.t.C.test_x", "_m"])
            self.assertEqual(json.loads(out)[0]["tests"], ["tests.t.C.test_x", "_m"])

    def test_raw_bytes_compile_honours_the_coding_cookie(self):  # CDXINT140 F3
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            for data in (b"# -*- coding: bogus -*-\nx = 1 == 2\n",
                         b"#!/usr/bin/env python\n# coding: nosuch\nx = 1 == 2\n",
                         b"# coding: ascii\ns = '\xc3\xa9'\nx = 1 == 2\n"):
                with self.subTest(data=data[:30]):
                    (Path(tmp) / "scripts" / "fx.py").write_bytes(data)
                    self.assertIn("does not compile", self._run_main(tmp, "scripts/fx.py"))
            (Path(tmp) / "scripts" / "fx.py").write_bytes(b"# coding: utf-8\ns = '\xc3\xa9'\nx = 1 == 2\n")
            self.assertIn("x = 1 != 2", self._ok(["scripts/fx.py", "--repo", tmp, "--tests", "t"]))

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
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):  # POLISH1 F9
                self.assertIn("unreadable", self._run_main(tmp, "scripts/l1.py"))
            (Path(tmp) / "scripts" / "bom.py").write_bytes(b"\xef\xbb\xbfx = 1 == 2\n")
            self.assertIn("x = 1 != 2", self._ok(["scripts/bom.py", "--repo", tmp, "--tests", "t"]))
        mutgen.generate("x = " + "1" * 4300 + "\n", "scripts/fx.py", ["t"])  # int() digit limit

    def test_cli_rejects_lone_cr_and_keeps_crlf(self):  # CLREV-MUT1FIX F1
        def cli(tmp, data):
            (Path(tmp) / "scripts" / "fx.py").write_bytes(data)
            return subprocess.run([sys.executable, str(_ROOT / "scripts" / "mutgen.py"),
                                   "scripts/fx.py", "--repo", tmp, "--tests", "t"],
                                  capture_output=True, timeout=120)

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            for data in (b"x = 1\ry = 2 == 3\n", b"x = 1 == 2\r\ny = 2\r", b"x = 1\r\r\ny = 2\r\n",
                         b"\xef\xbb\xbfx = 1\ry = 2\n", b"s = '''a\rb'''\nx = 1 == 2\n"):
                with self.subTest(data=data):
                    r = cli(tmp, data)
                    self.assertEqual(r.returncode, 2, r.stderr[-200:])
                    self.assertIn(b"lone CR", r.stderr)
                    self.assertEqual(r.stdout, b"")
            crlf = b"def f(a):\r\n    if a == 1:\r\n        return True\r\n    return a\r\n"
            r = cli(tmp, crlf)
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
            specs = json.loads(r.stdout)
            self.assertTrue(specs)
            for s in specs:
                self.assertIsNotNone(mutate.apply_text(crlf, s["old"], s["new"])[0], s["name"])

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
