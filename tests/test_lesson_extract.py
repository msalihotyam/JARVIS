import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lesson_extract as L  # noqa: E402

RULES = """# Rules header
O1 first rule
   continued
- O2 second rule
O2a addendum
## O3 heading rule
- body of three

## Other section
free text
O1 GUNCELLEME later update
O10 tenth rule text
"""
DENIED = "O4 musteri bilgisi MARKERX\nO5 kimlik 12345678901 MARKERY\nO6 MÜŞTERİ dosyasi MARKERZ\n"
LESSON = {f: "x" for f in L.FIELDS if f != "alinti"}
OUTPUTS = ("candidates.jsonl", "rules_classification.jsonl", "not_in_code.md", "run_report.md")


class Refused(Exception):
    pass


class Err(Exception):
    pass


def chunk_text(content):
    return content.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]


def good_answer(content):
    quote = max(chunk_text(content).splitlines(), key=len)
    return json.dumps({"lessons": [dict(LESSON, alinti=quote)], "classification": "topic"})


class FakeApi:
    ApiRefused = Refused
    ApiError = Err
    LEDGER_NAME = "ledger.jsonl"

    def __init__(self, root, answer=good_answer, refuse_at=None):
        self.ROOT = str(root)
        self.answer, self.refuse_at = answer, refuse_at
        self.payloads, self.ok = [], 0

    def open_envelope(self, ceiling, purpose):
        assert purpose.startswith("ders")
        return "e1"

    def call(self, model, system, messages, max_tokens, purpose, envelope=None):
        self.payloads.append(json.dumps([model, system, messages, purpose, envelope], ensure_ascii=False))
        if self.refuse_at and len(self.payloads) >= self.refuse_at:
            raise Refused("envelope: remaining 0.0000 < est 0.0500 USD")
        assert purpose.startswith("ders") and envelope == "e1"
        self.ok += 1
        return self.answer(messages[0]["content"])

    def read_ledger(self, path):
        return [{"envelope": "e1", "ceiling": 1.0}] + [{"envelope": "e1", "usd": 0.01}] * self.ok


class LessonExtractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = self.tmp / "orch"
        (self.root / "kayit" / "sohbet").mkdir(parents=True)
        self.write("ORCH_RULES.md", RULES)
        self.write("kayit/KARARLAR.md", "# Kararlar\nKarar: test akisi duzgun calisti.\n")
        self.write("kayit/sohbet/S1.md", "## Sohbet\nOrkestrator kuyrugu sirayla isledi.\n")
        self.write("kayit/OTHER.md", "OTHERMARK not a source\n")

    def write(self, rel, text):
        with open(self.root / rel, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def run_once(self, out="out", **kw):
        api = FakeApi(self.tmp, **kw)
        rep = L.run(self.root, self.tmp / out, 1.0, api)
        return api, rep, {n: (self.tmp / out / n).read_bytes() for n in OUTPUTS}

    def test_rules_one_chunk_per_rule_with_line_pointers(self):
        cs = L.chunk_rules(self.root / "ORCH_RULES.md", "ORCH_RULES.md")
        self.assertEqual([c["rule"] for c in cs], [1, 2, 3, 10, None])
        by = {c["rule"]: c for c in cs}
        self.assertEqual(by[1]["spans"], [[2, 3], [11, 11]])
        self.assertEqual(by[1]["text"], "O1 first rule\n   continued\nO1 GUNCELLEME later update")
        self.assertEqual(L.pointer(by[1]), "ORCH_RULES.md:2-3,11-11")
        self.assertEqual(by[2]["spans"], [[4, 5]])
        self.assertEqual(by[3]["spans"], [[6, 8]])
        self.assertEqual(by[10]["spans"], [[12, 12]])
        self.assertEqual(by[None]["spans"], [[9, 10]])

    def test_denied_chunk_never_sent_never_written(self):
        self.write("ORCH_RULES.md", RULES + DENIED)
        api, rep, files = self.run_once()
        self.assertEqual(rep["dropped_dora"], 3)
        sent = "\n".join(api.payloads)
        for mark in ("MARKERX", "MARKERY", "MARKERZ", "12345678901", "OTHERMARK"):
            self.assertNotIn(mark, sent)
            for body in files.values():
                self.assertNotIn(mark.encode(), body)
        rows = [json.loads(x) for x in files["rules_classification.jsonl"].decode().splitlines()]
        cls = {r["rule"]: r["class"] for r in rows}
        self.assertEqual([cls["O4"], cls["O5"], cls["O6"]], ["dropped_dora"] * 3)
        self.assertEqual(rep["sent"], rep["chunks"] - 3)

    def test_quote_not_verbatim_is_dropped(self):
        def answer(content):
            ok = json.loads(good_answer(content))
            ok["lessons"].append(dict(LESSON, alinti="this quote is NOT IN CHUNK"))
            return json.dumps(ok)
        _, rep, files = self.run_once(answer=answer)
        self.assertGreater(rep["sent"], 0)
        self.assertEqual(rep["quote_dropped"], rep["sent"])
        self.assertEqual(rep["candidates"], rep["sent"])
        cands = [json.loads(x) for x in files["candidates.jsonl"].decode().splitlines()]
        self.assertNotIn("NOT IN CHUNK", files["candidates.jsonl"].decode())
        self.assertTrue(all(c["dogrulama_durumu"] == "aday" for c in cands))
        self.assertEqual(cands[0]["kaynak"], "ORCH_RULES.md:2-3,11-11")

    def test_envelope_ceiling_stops_cleanly_with_partial_counts(self):
        with self.assertRaises(ValueError):
            L.run(self.root, self.tmp / "x", 0, FakeApi(self.tmp))
        _, rep, files = self.run_once(refuse_at=2)
        self.assertTrue(rep["partial"])
        self.assertTrue(rep["stop"].startswith("refused at "))
        self.assertEqual((rep["sent"], rep["candidates"]), (1, 1))
        self.assertEqual(rep["usd_spent"], "0.0100")
        report = files["run_report.md"].decode()
        self.assertIn("partial: True", report)
        self.assertIn("sent: 1", report)
        rows = [json.loads(x) for x in files["rules_classification.jsonl"].decode().splitlines()]
        self.assertEqual([r["class"] for r in rows], ["topic"] + ["unclassified"] * 3)

    def test_two_runs_are_byte_identical(self):
        _, _, first = self.run_once("out1")
        _, _, second = self.run_once("out2")
        self.assertEqual(first, second)
        self.assertTrue(first["candidates.jsonl"])


PHONES = {  # LESSONS1FIX: every Turkish phone shape is dropped before any call
    "landline_0_area": "0212 123 45 67",
    "mobile_unprefixed": "532 123 45 67",
    "mobile_0": "0532 123 45 67",
    "mobile_plus90": "+90 532 123 45 67",
    "mobile_plus90_tight": "+905321234567",
    "mobile_90_no_plus": "905321234567",
    "mobile_0090": "0090 532 123 4567",
    "landline_parens": "(0212) 123 45 67",
    "landline_plus90_parens": "+90 (216) 123-45-67",
    "landline_unprefixed_3_7": "312 1234567",
    "contiguous_10": "5321234567",
    "dots": "532.123.45.67",
    "dashes": "0532-123-45-67",
    "slash": "0212/123 45 67",
    "double_space": "532  123  45  67",
    "en_dash": "532–123–45–67",
    "nbsp": "532 123 45 67",
    "fullwidth_digits": "５３２ １２３ ４５ ６７",
    "service_0850": "0850 123 45 67",
    "call_center_444": "444 1 444",
    "after_letters": "Tel05321234567",
    "in_sentence": "ara: 232 123 45 67 lutfen",
    "grouping_2_2_3": "05 32 12 34 567",
    "grouping_3_2_2_3": "532 12 34 567",
    "grouping_4_4_3": "0532 1234 567",
    "split_last_digit": "+90 532 123 45 6 7",
    "spaced_digits": "0 5 3 2 1 2 3 4 5 6 7",
    "zero_width_sep": "0532​123​45​67",
    "underscore_sep": "532_123_45_67",
    "minus_sign_sep": "532−123−45−67",
    "middle_dot_sep": "532·123·45·67",
    "arabic_indic_digits": "٥٣٢ 123 45 67",
    "line_break_inside": "532\n123 45 67",
    "soft_hyphen_sep": "0532­123­45­67",
    "word_joiner_sep": "0532⁠123⁠45⁠67",
    "colon_sep": "0532:123:45:67",
    "pipe_sep": "532|123|45|67",
    "star_sep": "532*123*45*67",
    "call_center_444_2_2": "444 12 34",
    "devanagari_digits": "५३२ १२३ ४५ ६७",
}
NOT_PHONES = ("O52 kurali 2026-11-14 12:30 tarihinde", "600000 ms, 80 cagri, 45 dk",
              "PR 5321 merged 2026", "O52 600000 3600 1234567", "O29 (2026-10-18) kural",
              "O2-7, 2026-11-07 notu", "plan 2.1, 4.2, 8, 11.4, 12 bolum",
              "sha e0bcc99a 4dee6b68", "O27 UIRED5 3 test")


class PhoneShapeTests(unittest.TestCase):
    def test_each_phone_shape_is_denied(self):
        deny = L.load_deny()
        for name, text in PHONES.items():
            with self.subTest(name):
                self.assertTrue(L.denied(f"O9 kural {text} son", deny))

    def test_rule_text_without_phones_is_not_denied(self):
        deny = L.load_deny()
        for text in NOT_PHONES:
            with self.subTest(text):
                self.assertFalse(L.denied(text, deny))


if __name__ == "__main__":
    unittest.main()
