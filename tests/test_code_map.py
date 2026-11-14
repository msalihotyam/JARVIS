"""CODEMAP1: scripts/code_map.py maps a tree by parsing only, deterministically."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import code_map  # noqa: E402

FIXTURE = {
    "pkg/__init__.py": '"""Pkg doc.\n\nMore."""\n',
    "pkg/a.py": (
        '"""Module A."""\nimport os\nfrom . import b\n\n\nclass A(Base):\n    """The A."""\n\n'
        "    def hidden(self):\n        pass\n\n\nclass _Private:\n    pass\n\n\n"
        'def f(x, y=1) -> int:\n    """Does f.\n\n    Long."""\n    return x\n\n\n'
        "async def g(*a, k: str = 'v'):\n    pass\n\n\ndef _h():\n    pass\n"
    ),
    "pkg/b.py": "import pkg.a\nfrom pkg.sub.c import thing\n",
    "pkg/sub/c.py": "from ..a import f\nthing = f\n",
    "tools/t.py": "import pkg.a\nimport tools.tools_x\nimport json\n",
    "tools/tools_x.py": '"""X."""\n',
}


def make(tmp: Path, files: dict) -> None:
    for rel, text in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


class CodeMapTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "src"
        make(self.src, FIXTURE)

    def test_fixture_maps_to_expected_lines_and_edges(self):
        data = code_map.build(self.src, ("pkg", "tools"))
        lines = code_map.render(data).splitlines()
        for want in (
            "modules: 6  unparsed: 0",
            "## pkg/",
            "- pkg/__init__.py [3] Pkg doc.",
            "- pkg/a.py [29] Module A.",
            "  imports: pkg/b.py",
            "  class A(Base) @6 - The A.",
            "  def f(x, y=1) -> int @17 - Does f.",
            "  async def g(*a, k: str='v') @24",
            "## pkg/sub/",
            "  imports: pkg/a.py",
            "## tools/",
            "  imports: pkg/a.py, tools/tools_x.py",
        ):
            self.assertIn(want, lines)
        self.assertFalse([ln for ln in lines if "_Private" in ln or "_h(" in ln or "hidden" in ln])
        mods = data["modules"]
        self.assertEqual(mods["pkg/a.py"]["imported_by"], ["pkg/b.py", "pkg/sub/c.py", "tools/t.py"])
        self.assertEqual(mods["pkg/b.py"]["imports"], ["pkg/a.py", "pkg/sub/c.py"])
        self.assertEqual(mods["pkg/b.py"]["imported_by"], ["pkg/a.py"])
        self.assertEqual(mods["tools/tools_x.py"]["imported_by"], ["tools/t.py"])

    def test_syntax_error_is_listed_and_does_not_stop_the_rest(self):
        make(self.src, {"pkg/bad.py": "def broken(:\n    pass\n"})
        data = code_map.build(self.src, ("pkg", "tools"))
        self.assertEqual([u["path"] for u in data["unparsed"]], ["pkg/bad.py"])
        self.assertIn("pkg/a.py", data["modules"])
        self.assertNotIn("pkg/bad.py", data["modules"])
        self.assertTrue([ln for ln in code_map.render(data).splitlines() if ln.startswith("- pkg/bad.py [2] UNPARSED:")])

    def test_two_runs_are_byte_identical_and_only_out_is_written(self):
        before = sorted(p.relative_to(self.src).as_posix() for p in self.src.rglob("*"))
        outs = []
        for name in ("o1", "o2"):
            code_map.write(self.src, self.tmp / name, ("pkg", "tools"))
            outs.append({n: (self.tmp / name / n).read_bytes() for n in ("MAP.md", "map.json")})
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(before, sorted(p.relative_to(self.src).as_posix() for p in self.src.rglob("*")))
        self.assertEqual(json.loads(outs[0]["map.json"])["version"], 1)

    def test_real_tree_maps_and_map_is_compact(self):
        text, data = code_map.write(ROOT, self.tmp / "real")
        self.assertEqual(data["unparsed"], [])
        self.assertIn("jarvis/core.py", data["modules"])
        self.assertIn("scripts/code_map.py", data["modules"])
        self.assertLess(len(text.splitlines()), 2500)


if __name__ == "__main__":
    unittest.main()
