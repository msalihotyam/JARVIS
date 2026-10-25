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


class ConstructionTests(unittest.TestCase):
    """L0/L1 hold by construction: extra content can only raise the lane."""

    @classmethod
    def setUpClass(cls):
        cls.rows = _rows()

    def test_task_clause_lifts_every_l0_l1_row(self):
        for row in self.rows:
            if row["lane"] not in ("L0", "L1"):
                continue
            for tail in (" ve faturayı yatır", " sonra dosyayı sil", ", ahmete mesaj at",
                         "; arabayı yıka"):
                with self.subTest(text=row["text"] + tail):
                    self.assertIn(route(row["text"] + tail).lane, ("L2", "L3"))

    def test_negation_prefix_never_l0(self):
        for row in self.rows:
            if row["lane"] != "L0":
                continue
            for head in ("hayır ", "sakın ", "asla ", "değil "):
                with self.subTest(text=head + row["text"]):
                    self.assertNotEqual(route(head + row["text"]).lane, "L0")

    def test_negated_verbs_never_l0(self):
        for text in ("kapatma", "pencereyi kapatıverme", "KAPATIVERME", "kapatmayın", "durma",
                     "iptal etme", "vazgeçme", "saati söylemeyin", "saate bakma",
                     "saatin bakımı nedir"):
            with self.subTest(text=text):
                self.assertNotEqual(route(text).lane, "L0")

    def test_unrecognised_content_goes_up(self):
        for text in ("Bana pizza söyler misin?", "Bana pizza söyleyebilir misin?",
                     "Ahmet'e ne zaman geleceğini sorar mısın?", "Gram altın şu an ne kadar?",
                     "CPU sıcaklığı kaç derece?", "faturayı yatır ne dersin",
                     "faturayı ne zaman yatıracaksın", "çiçekleri sula ne dersin",
                     "süper yatır", "HARİKA FATURAYI YATIR"):
            with self.subTest(text=text):
                self.assertEqual(route(text).lane, "L2")
        self.assertEqual(route("Her salı faturayı yatır").lane, "L3")
        for text in ("", "   "):
            self.assertEqual(route(text).lane, "L1")
        self.assertEqual(route("TEŞEKKÜRLER").intent, "thanks")

    def test_device_state_live_data_and_recurring(self):
        for text in ("şu an kaç derece", "ışık yak olur mu", "ses açık mı",
                     "internet çalışıyor mu", "benzin kaça", "galatasaray kaç kaç",
                     "iftara ne kadar var", "kahve demle ne dersin", "saat kaç tokyoda",
                     "İstanbul’da saat kaç"):
            with self.subTest(text=text):
                self.assertEqual(route(text).lane, "L2")
        self.assertNotEqual(route("yeter mi").lane, "L0")
        for text in ("hergün dosyaları yedekle", "haftada bir yedek al",
                     "saat başı haber ver", "pazartesileri raporu gönder",
                     "her iki saatte bir kontrol et", "sürekli diski izle"):
            with self.subTest(text=text):
                self.assertEqual(route(text).lane, "L3")


class DefectClassTests(unittest.TestCase):
    """ROUTERTRC5: one test per CDXINT79 defect-class variant."""

    def assertUp(self, texts):
        for text in texts:
            with self.subTest(text=text):
                self.assertIn(route(text).lane, ("L2", "L3"))

    def test_clause_separators(self):
        self.assertUp(("nasılsın, ekranı kilitle", "bir fıkra anlat ve maili gönder",
                       "bir espri yap sonra bilgisayarı kapat", "bir fıkra anlatıp ışıkları kapa",
                       "Bir şaka yapıp kapıyı kapa.", "naber; mail gönder"))

    def test_question_form_verb_is_request(self):
        self.assertUp(("Pencereyi kapatabilir miyim?", "kapatabilirmiyim", "kapatabilir mi",
                       "Kahve ısmarlayacak mısın?", "kahve ısmarlarmısın",
                       "Botu konuşlandırır mısın?", "Yazıcıyı tanımlar mısın?",
                       "Ahmet'e cevap verir misin?", "kapıyı açar mısın"))
        self.assertEqual(route("bana bir fıkra anlatır mısın").lane, "L1")

    def test_upper_case(self):
        self.assertUp(("KAHVE ISMARLAYACAK MISIN", "İSTANBUL'DA KAÇ DERECE"))

    def test_suffix_negation(self):
        self.assertUp(("Bir daha konuşmayın.", "Bir daha unutmayın", "onu silmesin",
                       "Bir daha kaçırma.", "Şaka yapmanı istemiyorum.", "şaka yapma",
                       "kapatma", "pencereyi kapatma", "bunu silmene gerek yok"))
        self.assertEqual(route("anlamadım").lane, "L1")

    def test_unicode_apostrophes(self):
        self.assertUp(("İstanbul’da kaç derece", "Tokyo´da saat kaçtır", "İstanbul'da kaç derece?"))

    def test_empty_and_punctuation(self):
        for text in ("", "   ", "?!."):
            self.assertEqual(route(text).lane, "L1")

    def test_negation_and_task_ordering(self):
        self.assertUp(("istemiyorum şaka yap", "şaka yap istemiyorum",
                       "maili gönder sonra fıkra anlat", "fıkra anlat sonra maili gönder",
                       "teşekkürler şimdi maili gönder", "Boş ver, bunu bana ver."))

    def test_truncated_forms(self):
        self.assertUp(("kapatabilir mi", "saat kaçt", "O an saat kaçtı?"))

    def test_live_data_on_known_words(self):
        self.assertUp(("bugün ne var", "bugün haber var mı", "bana haber var mı",
                       "bugün hava nasıl", "Dün ayın kaçıydı?", "Son dakika gelişmeleri neler?",
                       "ETH ne kadar?", "Kargom nerede?", "kaç derece", "bu gün ne var",
                       "bu dakika ne var", "bana bugün bilgi ver",
                       "şu an için kısa bir bilgi ver", "kahve kaç"))
        for text in ("bugün nasılsın", "bugün çok yorgunum", "suyun kaynama noktası kaç derece"):
            self.assertEqual(route(text).lane, "L1", text)

    def test_l0_word_once_and_no_modal(self):
        self.assertNotEqual(route("dur dur").lane, "L0")
        self.assertUp(("Pencereyi kapatabilir miyim?",))
        self.assertNotEqual(route("pencereyi kapat pencereyi").lane, "L0")


class ActionClosedSetTests(unittest.TestCase):
    """ROUTERTRC6: L0 action intents only on an exact closed-set utterance."""

    def assertNotL0(self, texts):
        for text in texts:
            with self.subTest(text=text):
                self.assertNotEqual(route(text).lane, "L0")

    def test_closed_set_and_allowed_wrappers(self):
        for text, intent in (("iptal", "cancel"), ("İptal et.", "cancel"), ("dur", "cancel"),
                             ("kapat", "close_window"), ("Tamam kapat", "close_window"),
                             ("pencereyi kapat", "close_window"),
                             ("Jarvis, iptal et lütfen.", "cancel"),
                             ("jarvis kapat", "close_window"), ("kapat lütfen", "close_window")):
            with self.subTest(text=text):
                self.assertEqual((route(text).lane, route(text).intent), ("L0", intent))

    def test_any_extra_token_is_not_l0(self):
        rows = [r["text"] for r in _rows() if r["intent"] in ("cancel", "close_window")]
        self.assertTrue(rows)
        extras = ("o", "yok", "bunu", "şunu", "hey", "tamam", "bir", "şimdi", "değil", "mı",
                  "ve", "x", "hemen", "neyse", "pencereyi", "et", "dur", "kapat", "iptal",
                  "jarvis", "lütfen", "dakika", "gerek")
        for text in rows:
            words = text.split()
            for extra in extras:
                for i in range(len(words) + 1):
                    if (i == 0 and extra == "jarvis") or (i == len(words) and extra == "lütfen"):
                        continue  # the two allowed wrappers
                    cand = " ".join(words[:i] + [extra] + words[i:])
                    core = normalize(cand).split()
                    core = core[1:] if core[:1] == ["jarvis"] else core
                    core = core[:-1] if core[-1:] == ["lutfen"] else core
                    if " ".join(core) in ("iptal et", "tamam kapat", "pencereyi kapat"):
                        continue  # the result is itself a closed-set phrase
                    with self.subTest(text=cand):
                        self.assertNotEqual(route(cand).lane, "L0")

    def test_cdxint80_r1_to_r5(self):
        self.assertNotL0(("Boşver, bunu bana ver.", "İptal yok.", "O pencereyi kapatabilir.",
                          "O an saat kaç olmuş?", "O günün tarihi ne?"))

    def test_cdxint80_minor_inputs_go_up(self):
        for text in ("Kahve ne kadar?", "Bir kahve kaç?", "Yemek için rezervasyon istiyorum.",
                     "Tarih kaç?"):
            with self.subTest(text=text):
                self.assertEqual(route(text).lane, "L2")

    def test_separators(self):
        self.assertNotL0(("Boşver, bunu bana ver.", "iptal, yok", "kapat; kapat",
                          "iptal,, iptal..", "tamam kapat,, kapat"))

    def test_contractions(self):
        self.assertNotL0(("Boşver bunu bana ver", "boşver", "bos ver", "vazgeçtim"))

    def test_case(self):
        self.assertNotL0(("İPTAL YOK", "KAPAT BUNU"))
        self.assertEqual(route("İPTAL").intent, "cancel")

    def test_suffix_and_negation(self):
        self.assertNotL0(("O pencereyi kapatabilir.", "kapatma", "iptal etme", "kapatsana",
                          "kapatır mısın", "durma"))

    def test_unicode(self):
        self.assertNotL0(("İptal’i yok", "iptal yok", "kapat bunu"))
        self.assertEqual(route("iptal et").intent, "cancel")

    def test_empty_and_raw(self):
        for text in ("", "jarvis", "lütfen", "jarvis lütfen"):
            with self.subTest(text=text):
                self.assertNotEqual(route(text).lane, "L0")

    def test_ordering(self):
        self.assertNotL0(("yok iptal", "kapat tamam", "lütfen jarvis kapat", "kapat pencereyi",
                          "et iptal"))

    def test_truncation_and_o(self):
        self.assertNotL0(("O an saat kaç olmuş?", "O günün tarihi ne?", "o kapat", "kapa",
                          "ipta"))
        self.assertEqual(route("saat kaç").intent, "time.now")


if __name__ == "__main__":
    unittest.main()
