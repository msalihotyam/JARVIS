import importlib.util
import unittest
from pathlib import Path

from jarvis import memory

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("memory_eval", ROOT / "scripts" / "memory_eval.py")
memory_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memory_eval)

# Floors measured 2026-10-13 on base 5de568b (recall@5 / MRR): fts 0.586/0.621,
# like 0.638/0.655, fts_or 0.862/0.897, like_or 0.914/0.897. Floors sit just
# below so a retrieval regression fails; raise them when retrieval improves.
FLOORS = {
    "fts": (0.58, 0.62),
    "like": (0.63, 0.65),
    "fts_or": (0.86, 0.89),
    "like_or": (0.91, 0.89),
}


class MemoryEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = memory_eval.run_eval()

    def test_fixture_size(self):
        self.assertEqual(self.result["records"], 40)
        self.assertGreaterEqual(self.result["queries"], 25)

    def test_no_cross_project_leak_in_any_mode(self):
        self.assertTrue(self.result["modes"])
        for name, mode in self.result["modes"].items():
            self.assertEqual(mode["leaks"], 0, name)

    def test_recall_and_mrr_floors(self):
        expected = {"like", "like_or"}
        if memory.MEMORY_RECORDS_FTS5_AVAILABLE:
            expected |= {"fts", "fts_or"}
        self.assertEqual(set(self.result["modes"]), expected)
        for name in expected:
            recall_floor, mrr_floor = FLOORS[name]
            mode = self.result["modes"][name]
            self.assertGreaterEqual(mode["recall_at_5"], recall_floor, name)
            self.assertGreaterEqual(mode["mrr"], mrr_floor, name)

    def test_json_output(self):
        import contextlib
        import io
        import json

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(memory_eval.main(["--json"]), 0)
        self.assertIn("fts_or" if memory.MEMORY_RECORDS_FTS5_AVAILABLE else "like",
                      json.loads(out.getvalue())["modes"])


if __name__ == "__main__":
    unittest.main()
