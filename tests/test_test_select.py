"""scripts/test_select.py: changed-file test selection over the import graph, failing safe."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_select  # noqa: E402

TREE = {
    "jarvis/__init__.py": "",
    "jarvis/leaf.py": "X = 1\n",
    "jarvis/mid.py": "from jarvis import leaf\n",
    "jarvis/other.py": "Y = 2\n",
    "scripts/tool.py": "import jarvis.other\n",
    "tests/__init__.py": "",
    "tests/helpers.py": "",
    "tests/test_leaf.py": "import jarvis.leaf\n",
    "tests/test_mid.py": "from jarvis.mid import leaf\n",
    "tests/test_other.py": "from tests import helpers\nimport tool\n",
}
ALL = ["tests.test_leaf", "tests.test_mid", "tests.test_other"]


class TestSelectTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.write(TREE)

    def write(self, files: dict[str, str]) -> None:
        for rel, text in files.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text, encoding="utf-8")

    def select(self, *paths: str) -> list[str]:
        return test_select.select(self.root, list(paths))[0]

    def test_leaf_change_selects_only_its_importers_tests(self) -> None:
        self.assertEqual(self.select("jarvis/other.py"), ["tests.test_other"])
        self.assertEqual(self.select("jarvis/mid.py"), ["tests.test_mid"])
        self.assertEqual(self.select("tests/test_leaf.py"), ["tests.test_leaf"])

    def test_transitive_import_is_followed(self) -> None:
        self.assertEqual(self.select("jarvis/leaf.py"), ["tests.test_leaf", "tests.test_mid"])
        self.assertEqual(self.select("jarvis/__init__.py"), ALL)  # package init runs on every import

    def test_unknown_file_type_or_helper_selects_all(self) -> None:
        self.write({"README.md": "x\n"})
        for path in ("README.md", "pyproject.toml", "jarvis/gone.py", "tests/helpers.py",
                     "scripts/test_durations.json", ".github/workflows/ci.yml"):
            ids, reason = test_select.select(self.root, ["jarvis/other.py", path])
            self.assertEqual(ids, ALL, path)
            self.assertTrue(reason.startswith("ALL:"), reason)

    def test_parse_error_selects_all(self) -> None:
        self.write({"jarvis/bad.py": "def (:\n"})
        ids, reason = test_select.select(self.root, ["jarvis/other.py"])
        self.assertEqual(ids, ALL)
        self.assertIn("jarvis/bad.py", reason)

    def test_dynamic_load_is_always_selected(self) -> None:
        self.write({"jarvis/plug.py": "import importlib\nimportlib.import_module(NAME)\n",
                    "tests/test_plug.py": "import jarvis.plug\n",
                    "tests/test_spawn.py": "import sys\nCMD = [sys.executable, '-c', 'pass']\n"})
        self.assertEqual(self.select("jarvis/other.py"),
                         ["tests.test_other", "tests.test_plug", "tests.test_spawn"])

    def test_output_is_sorted_and_deterministic(self) -> None:
        def run(*paths: str) -> str:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(test_select.main(["--root", str(self.root), *paths]), 0)
            return out.getvalue()

        first = run("jarvis/other.py", "jarvis/leaf.py")
        self.assertEqual(first, "\n".join(ALL) + "\n")
        self.assertEqual(run("jarvis/leaf.py", "./jarvis/other.py"), first)
        self.assertEqual(run(), "")


if __name__ == "__main__":
    unittest.main()
