"""Gate 6G C-5: every ALLOWED_SKIPS entry in scripts/test_parallel.py is derived from the code.

Each allowed id names a real test method whose module skips with exactly that reason, and
no schema-10 rollback proof may ever be allowed to skip. Source is read as text; no test
module is imported and nothing runs.
"""

import ast
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("test_parallel", ROOT / "scripts" / "test_parallel.py")
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)

ROLLBACK = "the rollback release's commit is not in this clone"


def _methods(tree: ast.Module) -> set[str]:
    return {f"{cls.name}.{fn.name}" for cls in tree.body if isinstance(cls, ast.ClassDef)
            for fn in cls.body if isinstance(fn, ast.FunctionDef)}


def _string_literals(tree: ast.Module) -> set[str]:
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


class AllowListTests(unittest.TestCase):
    def test_every_allowed_skip_is_a_real_test_that_skips_with_that_reason(self) -> None:
        self.assertTrue(tp.ALLOWED_SKIPS)
        for test_id, reason in tp.ALLOWED_SKIPS.items():
            with self.subTest(test_id):
                package, module, rest = test_id.split(".", 2)
                self.assertEqual("tests", package)
                tree = ast.parse((ROOT / "tests" / f"{module}.py").read_text(encoding="utf-8"))
                self.assertIn(rest, _methods(tree))
                self.assertIn(reason, _string_literals(tree))

    def test_a_rollback_proof_is_never_allowed_to_skip(self) -> None:
        self.assertNotIn(ROLLBACK, tp.ALLOWED_SKIPS.values())
        self.assertFalse([t for t in tp.ALLOWED_SKIPS if "rollback" in t.lower()])


if __name__ == "__main__":
    unittest.main()
