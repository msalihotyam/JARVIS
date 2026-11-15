"""Tests for scripts/mutgen.py (deterministic mutation spec generator)."""

import contextlib
import importlib.util
import io
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


if __name__ == "__main__":
    unittest.main()
