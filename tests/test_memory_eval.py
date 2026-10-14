import contextlib
import importlib.util
import io
import json
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
# They hold for the original (LEGACY_TYPES) queries; ALL_FLOORS cover the whole
# set incl. exact/update/tr_en (measured 2026-10-14, recall@5 / MRR: fts
# 0.455/0.473, like 0.500/0.509, fts_or 0.600/0.618, like_or 0.645/0.645).
LEGACY_TYPES = {"keyword", "paraphrase", "morphology", "cross-lingual"}
GATE_TYPES = {"exact", "update", "tr_en"}
ALL_FLOORS = {
    "fts": (0.45, 0.47),
    "like": (0.49, 0.50),
    "fts_or": (0.59, 0.61),
    "like_or": (0.64, 0.64),
}
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
        cls.fixture = json.loads(memory_eval.DEFAULT_FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_size(self):
        self.assertEqual(self.result["records"], 74)
        self.assertGreaterEqual(self.result["queries"], 25)

    def test_gate_query_types_in_fixture(self):
        records = {r["id"]: r for r in self.fixture["records"]}
        self.assertEqual(len(records), len(self.fixture["records"]))
        queries = self.fixture["queries"]
        self.assertEqual(len({q["id"] for q in queries}), len(queries))
        for kind in GATE_TYPES:
            self.assertGreaterEqual(sum(q["type"] == kind for q in queries), 8, kind)
        self.assertEqual({q["lang"] for q in queries if q["type"] == "tr_en"}, {"tr", "en"})
        order = list(records)
        for q in queries:
            for label in q["expected"] + q.get("stale", []):
                self.assertEqual(records[label]["project"], q["project"], q["id"])
            if q["type"] == "update":
                self.assertTrue(q["stale"], q["id"])
                # The stale record is older: listed (inserted) before the expected one.
                self.assertLess(max(map(order.index, q["stale"])),
                                min(map(order.index, q["expected"])), q["id"])
            else:
                self.assertNotIn("stale", q, q["id"])

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
            legacy = [q for q in mode["queries"] if q["type"] in LEGACY_TYPES]
            self.assertEqual(len(legacy), 29, name)
            self.assertGreaterEqual(sum(q["recall"] for q in legacy) / len(legacy), recall_floor, name)
            self.assertGreaterEqual(sum(q["rr"] for q in legacy) / len(legacy), mrr_floor, name)
            recall_floor, mrr_floor = ALL_FLOORS[name]
            self.assertGreaterEqual(mode["recall_at_5"], recall_floor, name)
            self.assertGreaterEqual(mode["recall_at_10"], recall_floor, name)
            self.assertGreaterEqual(mode["mrr"], mrr_floor, name)

    def test_recall_at_10_and_by_type(self):
        types = {q["type"] for q in self.fixture["queries"]}
        self.assertTrue(GATE_TYPES | LEGACY_TYPES <= types)
        for name, mode in self.result["modes"].items():
            self.assertGreaterEqual(mode["recall_at_10"], mode["recall_at_5"], name)
            self.assertEqual(set(mode["recall_by_type"]), types, name)
            self.assertEqual(set(mode["recall_by_type_at_10"]), types, name)
            for kind in types:
                at5, at10 = mode["recall_by_type"][kind], mode["recall_by_type_at_10"][kind]
                self.assertTrue(0.0 <= at5 <= at10 <= 1.0, (name, kind))
            for q in mode["queries"]:
                self.assertGreaterEqual(q["recall_at_10"], q["recall"], (name, q["id"]))

    def test_update_hit_counts_only_above_stale(self):
        queries = {q["id"]: q for q in self.fixture["queries"]}
        for name, mode in self.result["modes"].items():
            for q in mode["queries"]:
                if q["type"] != "update":
                    continue
                spec = queries[q["id"]]
                got = q["got"]
                first_stale = min((got.index(s) for s in spec["stale"] if s in got), default=len(got))
                hit = any(e in got[:first_stale] for e in spec["expected"])
                self.assertEqual(q["recall_at_10"], 1.0 if hit else 0.0, (name, q["id"]))

    def test_gate_input_feeds_embeddings_gate(self):
        for name, mode in self.result["modes"].items():
            gate = memory_eval.gate_input(mode)
            self.assertEqual(set(gate), {"recall_at_10", "recall_by_type"}, name)
            self.assertEqual(gate["recall_at_10"], mode["recall_at_10"], name)
            self.assertEqual(gate["recall_by_type"], mode["recall_by_type_at_10"], name)
            self.assertTrue(GATE_TYPES | {"paraphrase"} <= set(gate["recall_by_type"]), name)
            verdict, reasons = memory_eval.embeddings_gate(gate, gate)
            self.assertEqual(verdict, "REJECT", name)  # no gain against itself
            self.assertNotIn("exact missing", reasons)
            self.assertNotIn("update missing", reasons)

    def test_gate_input_fails_closed(self):
        mode = self.result["modes"]["like_or"]
        baseline = memory_eval.gate_input(self.result["modes"]["like"])
        self.assertEqual(memory_eval.embeddings_gate(baseline, memory_eval.gate_input({}))[0], "REJECT")
        for kind in ("exact", "update"):
            partial = dict(mode, recall_by_type_at_10={
                t: v for t, v in mode["recall_by_type_at_10"].items() if t != kind})
            verdict, reasons = memory_eval.embeddings_gate(baseline, memory_eval.gate_input(partial))
            self.assertEqual(verdict, "REJECT", kind)
            self.assertIn(f"{kind} missing", reasons)

    def test_gate_cli_is_deterministic(self):
        outputs = []
        for _ in range(2):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(memory_eval.main(["--gate", "like", "like_or"]), 0)
            outputs.append(out.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertRegex(outputs[0].splitlines()[0], r"^gate like -> like_or: (ADOPT|REJECT)$")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(memory_eval.main(["--gate", "like", "nope"]), 2)

    def test_json_output(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(memory_eval.main(["--json"]), 0)
        self.assertIn("fts_or" if memory.MEMORY_RECORDS_FTS5_AVAILABLE else "like",
                      json.loads(out.getvalue())["modes"])


if __name__ == "__main__":
    unittest.main()
