"""scripts/test_select.py: changed-file test selection over the import graph, failing safe."""
from __future__ import annotations

import contextlib
import io
import subprocess  # nosec B404
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_wider_dynamic_load_markers_are_seen(self) -> None:  # CDXINT139 F1
        for src in ("import runpy\nrunpy.run_module('x')\n", "import importlib\nimportlib.reload(m)\n",
                    "from importlib import reload\nreload (m)\n", "exec (CODE)\n",
                    "import builtins\nbuiltins.exec(CODE)\n", "import pkgutil\npkgutil.walk_packages(['.'])\n",
                    "import unittest\nunittest.TestLoader().loadTestsFromName('x')\n",
                    "import unittest\nunittest.defaultTestLoader.discover('tests')\n",
                    "from importlib.util import module_from_spec\n",
                    "import importlib.util\nspec.loader.exec_module(m)\n", "X = eval(CODE)\n"):
            with self.subTest(src=src):
                self.write({"jarvis/plug.py": src, "tests/test_plug.py": "import jarvis.plug\n"})
                self.assertEqual(self.select("jarvis/other.py"), ["tests.test_other", "tests.test_plug"])

    def test_changed_dynamic_load_module_selects_all(self) -> None:  # CDXINT139 F2
        self.write({"jarvis/plug.py": "import runpy\nrunpy.run_path(P)\n", "tests/test_plug.py": "import jarvis.plug\n"})
        ids, reason = test_select.select(self.root, ["jarvis/plug.py"])
        self.assertEqual(ids, ALL + ["tests.test_plug"])
        self.assertIn("ALL: dynamic-load module changed jarvis/plug.py", reason)

    def test_source_scanning_test_is_always_selected(self) -> None:  # CDXINT139 F3
        self.write({"tests/test_scan.py": "from pathlib import Path\n"
                    "for p in Path(__file__).parents[1].joinpath('jarvis').rglob('*.py'):\n    p.read_text()\n",
                    "tests/test_walk.py": "import os\nNAMES = [f for _, _, fs in os.walk('tests') for f in fs]\n",
                    "tests/test_tmp.py": "import tempfile\nopen(tempfile.mktemp()).read()\n",
                    "tests/test_cwd.py": "import os\nopen(os.path.join(os.getcwd(), 'scripts', 'tool.py'))\n",
                    "tests/test_exe.py": "from sys import executable\nCMD = [executable, 'scripts/tool.py']\n"})
        self.assertEqual(self.select("jarvis/other.py"), ["tests.test_cwd", "tests.test_exe", "tests.test_other",
                                                          "tests.test_scan", "tests.test_walk"])

    def test_all_is_listed_from_test_files_not_the_parse(self) -> None:  # CDXINT139 F4
        self.write({"tests/test_zz_broken.py": "def (:\n"})
        ids, reason = test_select.select(self.root, ["jarvis/other.py"])
        self.assertEqual(ids, ALL + ["tests.test_zz_broken"])
        self.assertIn("tests/test_zz_broken.py", reason)

    def test_unreadable_or_vanishing_file_selects_all(self) -> None:  # CDXINT139 F5
        real, calls = Path.read_bytes, []

        def vanish_after_build(path: Path) -> bytes:  # build reads all 10 files, the scan then fails
            calls.append(path)
            if len(calls) > len(TREE):
                raise FileNotFoundError(str(path))
            return real(path)

        for effect in (PermissionError("denied"), vanish_after_build):
            calls.clear()
            with self.subTest(effect=effect), mock.patch.object(Path, "read_bytes", autospec=True,
                                                               side_effect=effect):
                ids, reason = test_select.select(self.root, ["jarvis/other.py"])
            self.assertEqual(ids, ALL)
            self.assertTrue(reason.startswith("ALL: read error"), reason)

    def test_git_diff_failure_selects_all(self) -> None:  # CDXINT139 F6
        for exc in (subprocess.CalledProcessError(128, ["git"]), FileNotFoundError("git")):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(exc=type(exc).__name__), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err), mock.patch.object(subprocess, "run", side_effect=exc):
                self.assertEqual(test_select.main(["--root", str(self.root), "--diff", "a..b"]), 0)
            self.assertEqual(out.getvalue(), "\n".join(ALL) + "\n")
            self.assertIn("ALL: git diff failed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
