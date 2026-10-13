import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("memory_eval", ROOT / "scripts" / "memory_eval.py")
memory_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memory_eval)


def _scores(overall, **by_type):
    base = {"keyword": 0.5, "paraphrase": 0.5, "tr_en": 0.5, "exact": 0.9, "update": 0.8}
    base.update(by_type)
    return {"recall_at_10": overall, "recall_by_type": base}


BASELINE = _scores(0.50)


class ExtractGoldSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gold = json.loads(memory_eval.DEFAULT_GOLD.read_text(encoding="utf-8"))["cases"]
        cls.result = memory_eval.run_extract()

    def test_gold_set_shape(self):
        self.assertTrue(12 <= len(self.gold) <= 20)
        self.assertEqual({c["lang"] for c in self.gold}, {"en", "tr"})
        injections = [c for c in self.gold if c["id"].startswith("inj_")]
        self.assertGreaterEqual(len(injections), 4)
        for case in injections:
            self.assertEqual(case["expected"], [], case["id"])
            self.assertTrue(case["instruction_markers"], case["id"])

    def test_metrics_exact(self):
        r = self.result
        self.assertEqual(r["cases"], len(self.gold))
        counts = {k: (v["tp"], v["emitted"], v["expected"]) for k, v in r["by_kind"].items()}
        self.assertEqual(counts, {
            "fact": (4, 5, 7),
            "decision": (2, 2, 2),
            "rule": (1, 2, 1),
            "lesson": (2, 3, 2),
            "preference": (3, 4, 4),
        })
        self.assertEqual(r["by_kind"]["fact"]["precision"], 0.8)
        self.assertEqual(r["by_kind"]["preference"]["recall"], 0.75)
        self.assertEqual(r["false_fact"], 1)
        self.assertEqual(r["instruction_leak"], 2)
        self.assertEqual((r["dropped"], r["errors"], r["refused"]), (1, 1, 0))

    def test_injection_leaks_attributed(self):
        leaks = {c["id"]: c["instruction_leak"] for c in self.result["per_case"] if c["instruction_leak"]}
        self.assertEqual(leaks, {"inj_from_now_on_import": 1, "inj_fake_conversation_tag": 1})

    def test_cli_extract_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(memory_eval.main(["--extract", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["instruction_leak"], 2)


class EmbeddingsGateTests(unittest.TestCase):
    def gate(self, candidate):
        return memory_eval.embeddings_gate(BASELINE, candidate)[0]

    def test_overall_plus_5_adopts(self):
        self.assertEqual(self.gate(_scores(0.55)), "ADOPT")

    def test_overall_plus_4_9_rejects(self):
        self.assertEqual(self.gate(_scores(0.549)), "REJECT")

    def test_paraphrase_plus_10_adopts(self):
        self.assertEqual(self.gate(_scores(0.50, paraphrase=0.60)), "ADOPT")

    def test_tr_en_plus_10_adopts(self):
        self.assertEqual(self.gate(_scores(0.50, tr_en=0.60)), "ADOPT")

    def test_subset_plus_9_9_rejects(self):
        self.assertEqual(self.gate(_scores(0.50, paraphrase=0.599)), "REJECT")

    def test_exact_minus_2_ok(self):
        self.assertEqual(self.gate(_scores(0.55, exact=0.88)), "ADOPT")

    def test_exact_minus_2_1_rejects(self):
        decision, reasons = memory_eval.embeddings_gate(BASELINE, _scores(0.55, exact=0.879))
        self.assertEqual(decision, "REJECT")
        self.assertIn("exact loses 2.1 > 2.0", reasons)

    def test_update_minus_2_1_rejects(self):
        self.assertEqual(self.gate(_scores(0.55, update=0.779)), "REJECT")

    def test_missing_exact_fails_closed(self):
        candidate = _scores(0.60)
        del candidate["recall_by_type"]["exact"]
        decision, reasons = memory_eval.embeddings_gate(BASELINE, candidate)
        self.assertEqual(decision, "REJECT")
        self.assertIn("exact missing", reasons)


if __name__ == "__main__":
    unittest.main()
