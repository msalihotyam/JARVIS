import json
import statistics
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

    def test_no_l3_under_routed(self):
        under = [r["text"] for r in self.rows
                 if r["lane"] == "L3" and route(r["text"]).lane != "L3"]
        self.assertEqual(under, [])

    def test_timing(self):
        # Median of many single calls with a generous bound: CI load spikes
        # move the tail, not the median.
        samples = []
        for _ in range(3):
            for row in self.rows:
                start = time.perf_counter()
                route(row["text"])
                samples.append(time.perf_counter() - start)
        self.assertLessEqual(statistics.median(samples), 0.05)


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

    def test_task_word_refuses_l0(self):
        for text in ("tarihi güncelle", "TARİHİ GÜNCELLE", "tarihi güncellesene",
                     "tarihi güncelleyebilir misin", "tarihi-güncelle",
                     "güncelle tarihi", "saati ayarla", "tarihi kaydet"):
            with self.subTest(text=text):
                self.assertEqual(route(text).lane, "L2")
        self.assertEqual(route("pencereyi kapat").intent, "close_window")
        self.assertEqual(route("iptal et").intent, "cancel")
        self.assertEqual(route("haftanın hangi günündeyiz").intent, "date.today")

    def test_requests_and_live_data_are_not_chat(self):
        for text, lane in (("oturumu kilitle", "L2"), ("altın kaç lira", "L2"),
                           ("gram altın ne kadar", "L2"),
                           ("oturumu kilitleyebilir misin ve sonucu söyle", "L2"),
                           ("sıfırdan bir e-ticaret sitesi istiyorum", "L3"),
                           ("bana bir uygulama lazım", "L3")):
            with self.subTest(text=text):
                self.assertEqual(route(text).lane, lane)
        self.assertEqual(route("bana bir fıkra anlatır mısın").lane, "L1")

    def test_every_weekday_recurs(self):
        for day in ("pazartesi", "salı", "çarşamba", "perşembe", "cuma",
                    "cumartesi", "pazar"):
            with self.subTest(day=day):
                self.assertEqual(route(f"her {day} beni uyandır").lane, "L3")

    def test_unrecognised_request_fails_upward(self):
        # One case per variant of the ROUTERTRC3 class.
        for variant, text in (("plain", "şunu hallet"), ("case", "ŞUNU HALLET"),
                              ("separator", "şunu-hallet"),
                              ("suffix -sene", "halletsene şunu"),
                              ("suffix -ir misin", "halleder misin"),
                              ("want", "kahve ısmarlamanı istiyorum"),
                              ("lazım", "bunu halletmek lazım"),
                              ("ascii-folded", "sunu hallet"),
                              ("order", "hallet şunu"), ("truncated", "şunu hall"),
                              ("relay verb", "ahmete sor gelecek mi"),
                              ("order verb", "bana pizza söyle"),
                              ("mixed clause", "nasılsın, bir de pr at"),
                              ("mixed conjunction", "öneri ver ve uygula"),
                              ("acknowledged order", "tamam bitir"),
                              ("proposal", "devam edelim mi")):
            with self.subTest(variant=variant):
                self.assertEqual(route(text).lane, "L2")
        for text in ("bana motivasyon verecek bir söz söyle", "oyun oynamak istiyorum",
                     "canım sıkılıyor", "bana bir fıkra anlat", "peki", "",
                     "merhaba, nasılsın?", "tamam anladım"):
            with self.subTest(text=text):
                self.assertEqual(route(text).lane, "L1")

    def test_apostrophe_like_marks(self):
        for mark in "'´’ʼ′`＇":
            with self.subTest(mark=hex(ord(mark))):
                self.assertEqual(normalize(f"İstanbul{mark}da"), "istanbulda")
        self.assertEqual(route("İPTAL").intent, "cancel")


if __name__ == "__main__":
    unittest.main()
