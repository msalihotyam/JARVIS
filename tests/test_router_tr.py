import json
import time
import unittest
from pathlib import Path

from jarvis.router_tr import Route, normalize, route

_CORPUS = Path(__file__).parent / "fixtures" / "router_tr_corpus.jsonl"
_L0_INTENTS = {"time.now", "date.today", "greet", "thanks", "close_window", "cancel"}


def _rows():
    with _CORPUS.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _rows()

    def test_corpus_shape(self):
        self.assertGreaterEqual(len(self.rows), 220)
        for row in self.rows:
            self.assertEqual(set(row), {"text", "lane", "intent"})
            self.assertIn(row["lane"], {"L0", "L1", "L2", "L3"})
            if row["lane"] == "L0":
                self.assertIn(row["intent"], _L0_INTENTS)

    def test_misroute_at_most_five_percent(self):
        bad = [r["text"] for r in self.rows if route(r["text"]).lane != r["lane"]]
        self.assertLessEqual(len(bad) / len(self.rows), 0.05, bad)

    def test_no_task_under_routed(self):
        under = [r["text"] for r in self.rows
                 if r["lane"] in ("L2", "L3") and route(r["text"]).lane in ("L0", "L1")]
        self.assertEqual(under, [])

    def test_l0_intents(self):
        for row in self.rows:
            if row["lane"] == "L0":
                with self.subTest(text=row["text"]):
                    got = route(row["text"])
                    self.assertEqual((got.lane, got.intent), ("L0", row["intent"]))

    def test_timing(self):
        texts = [r["text"] for r in self.rows]
        start = time.perf_counter()
        for text in texts:
            route(text)
        per_call = (time.perf_counter() - start) / len(texts)
        self.assertLessEqual(per_call, 0.020)


class BehaviourTests(unittest.TestCase):
    def test_turkish_casefold(self):
        self.assertEqual(normalize("İSTANBUL"), "istanbul")
        self.assertEqual(normalize("IŞIK"), "isik")
        self.assertEqual(normalize("Iğdır"), "igdir")
        self.assertEqual(route("SAAT KAÇ?").intent, "time.now")
        self.assertEqual(route("İPTAL").intent, "cancel")
        self.assertEqual(route("TEŞEKKÜRLER").intent, "thanks")

    def test_missing_turkish_characters(self):
        self.assertEqual(route("saat kac").intent, "time.now")
        self.assertEqual(route("bugun gunlerden ne").intent, "date.today")
        self.assertEqual(route("tesekkur ederim").intent, "thanks")

    def test_punctuation_and_dictation(self):
        self.assertEqual(route("...saat, kaç?!").intent, "time.now")
        self.assertEqual(route("  Tamam, kapat.  ").intent, "close_window")

    def test_route_fields_and_determinism(self):
        got = route("bana bir tablo yap")
        self.assertIsInstance(got, Route)
        self.assertEqual(got.lane, "L2")
        self.assertTrue(0.0 <= got.confidence <= 1.0)
        self.assertTrue(got.reason)
        self.assertEqual(got, route("bana bir tablo yap"))

    def test_empty_input(self):
        for text in ("", "   ", "?!."):
            self.assertIn(route(text).lane, ("L1", "L2"))

    def test_long_work_is_l3(self):
        self.assertEqual(route("bu repoyu baştan yaz").lane, "L3")
        self.assertEqual(route("şu projeyi kur").lane, "L3")


if __name__ == "__main__":
    unittest.main()
