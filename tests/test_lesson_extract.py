import contextlib
import io
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
        # LESSONS1FIX3: a hit drops every chunk within +-1 line, so O10 (line 12, next to O4) goes too
        self.assertEqual(rep["dropped_dora"], 4)
        sent = "\n".join(api.payloads)
        for mark in ("MARKERX", "MARKERY", "MARKERZ", "12345678901", "OTHERMARK"):
            self.assertNotIn(mark, sent)
            for body in files.values():
                self.assertNotIn(mark.encode(), body)
        rows = [json.loads(x) for x in files["rules_classification.jsonl"].decode().splitlines()]
        cls = {r["rule"]: r["class"] for r in rows}
        self.assertEqual([cls["O4"], cls["O5"], cls["O6"], cls["O10"]], ["dropped_dora"] * 4)
        self.assertEqual(cls["O3"], "topic")
        self.assertEqual(rep["sent"], rep["chunks"] - 4)
        self.assertIn("ORCH_RULES.md 4/8", files["run_report.md"].decode())
        self.assertNotIn("warning", rep)

    def test_drop_ratio_over_40_percent_warns(self):
        self.write("ORCH_RULES.md", DENIED)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            _, rep, files = self.run_once()
        report = files["run_report.md"].decode()
        self.assertEqual((rep["dropped_dora"], rep["sent"]), (3, 2))
        self.assertIn("ORCH_RULES.md 3/3", report)
        self.assertIn("dropped 3/5 chunks (> 40%)", report)
        self.assertIn("WARNING: dropped 3/5", err.getvalue())

    def test_word_split_over_chunk_boundary_is_never_sent(self):  # CDX-LESSONS1FIX2 F5
        self.write("kayit/sohbet/S2.md", "\n".join(f"guvenli satir {i}" for i in range(39)) + "\nyapi\nkredi son\n")
        self.assertGreater(len([c for c in L.chunks(self.root)[0] if c["source"].endswith("S2.md")]), 1)
        api, rep, files = self.run_once()
        self.assertEqual(rep["dropped_dora"], 2)
        self.assert_never_sent(files, api, "yapi", "kredi")
        self.assertIn("Orkestrator kuyrugu", "\n".join(api.payloads))

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

    def assert_never_sent(self, files, api, *marks):
        sent = "\n".join(api.payloads)
        for mark in marks:
            self.assertNotIn(mark, sent)
            for body in files.values():
                self.assertNotIn(mark.encode(), body)

    def test_phone_split_over_chunk_boundary_is_never_sent(self):  # CDX F2
        cases = {"line_limit": "\n".join(f"guvenli satir {i}" for i in range(39)) + "\nara 0532\n123 45 67 son\n",
                 "blank_padded": "\n".join("x" for _ in range(38)) + "\nara 0532\n\n\n123 45 67 son\n"}
        for name, body in cases.items():
            with self.subTest(name):
                self.write("kayit/sohbet/S2.md", body)
                self.assertGreater(len([c for c in L.chunks(self.root)[0] if c["source"].endswith("S2.md")]), 1)
                api, rep, files = self.run_once(out=name)
                self.assertEqual(rep["dropped_dora"], 2)
                self.assert_never_sent(files, api, "0532", "123 45 67")
                self.assertIn("Orkestrator kuyrugu", "\n".join(api.payloads))

    def test_source_name_is_screened(self):  # CDX F3, CDX-LESSONS1FIX2 F3
        self.write("kayit/sohbet/05321234567.md", "## Not\nTemiz bir ders metni burada.\n")
        self.write("kayit/sohbet/dora_notes.md", "## Not\nBaska temiz bir metin.\n")
        api, rep, files = self.run_once()
        self.assertEqual(rep["dropped_dora"], 2)
        self.assert_never_sent(files, api, "05321234567", "Temiz bir ders", "dora_notes", "Baska temiz")

    def test_email_shape_is_bounded(self):  # CDX F4
        deny = L.load_deny()
        self.assertTrue(L.denied("a" * 100 + "@x.com", deny))
        self.assertTrue(L.denied("kisi.ad+etiket@ornek.com.tr", deny))
        self.assertFalse(L.denied("a" * 40000, deny))
        self.assertFalse(L.denied("." * 40000 + "@", deny))


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
    # LESSONS1FIX2 (CDX F1): unbounded separator runs, comma, digits next to an ISO date
    "wide_gap_4_spaces": "532    123 45 67",
    "wide_gap_12_spaces": "0532            123 45 67",
    "tab_gap": "532\t123\t45\t67",
    "blank_lines_inside": "532\n\n\n123 45 67",
    "separator_line_inside": "0532\n---\n123 45 67",
    "comma_sep": "532,123,45,67",
    "comma_space_sep": "532, 123, 45, 67",
    "comma_plus90": "+90,532,123,45,67",
    "digits_before_iso_date": "53 2026-11-14",
    "digits_around_iso_date": "5 2026-11-14 3",
    "call_center_444_wide": "444     1     444",
    # LESSONS1FIX2 review: any non-word character separates digits
    "hash_sep": "532#123#45#67",
    "semicolon_sep": "532;123;45;67",
    "plus_sep": "532+123+45+67",
    "tilde_eq_amp_sep": "532~123=45&67",
    "angle_backslash_sep": "532<123>45\\67",
    "quoted_groups": "\"532\" '123' \"45\" '67'",
    "brackets": "[532] {123} [45] {67}",
    "backticks": "`0532` `123` `45` `67`",
    "blockquote": "> 0532 123\n> 45 67",
    "keycap_digits": "5️⃣3️⃣2️⃣ 123 45 67",
    "combining_marks": "5̲3̲2̲ 1͏23 45 67",
}
OTHER_SHAPES = {  # LESSONS1FIX2 review: id, IBAN, e-mail and word variants
    "id_3_3_3_2": "123 456 789 01",
    "id_5_3_3": "12345-678-901",
    "iban_dashes": "TR17-0006-1000-0000-1234-5678-90",
    "iban_double_space": "TR17  0006  1000  0000  1234  5678  90",
    "bban_24_digits": "000610000000123456789012",
    "email_spaced_at": "salih @ example.com",
    "email_quoted_local": "\"salih\"@example.com",
    "email_bracket_at": "salih[at]example.com",
    "word_zwsp": "do​ra",
    "word_soft_hyphen": "mu­steri",
    "word_fullwidth": "ｄｏｒａ",
    "bank_double_space": "yapi  kredi",
    "bank_nbsp": "garanti bbva",
    "bank_newline": "is\nbankasi",
    # LESSONS1FIX3: CDX-LESSONS1FIX2 F1-F6 and the letters-skeleton classes
    "F1_email_space_before_at": "salih @example.com",
    "F1_email_space_after_at": "salih@ example.com",
    "F1_email_space_after_dot": "salih@example. com",
    "F2_decomposed_marks": "müşteri",
    "F2_upper_dotted": "MÜŞTERİ",
    "F3_underscore_file": "dora_notes.md",
    "F3_underscore_name": "dora_config",
    "F4_hyphen_garanti": "Garanti-BBVA",
    "F4_hyphen_yapi": "Yapi-Kredi",
    "F4_glued_dotless_i": "YapıKredi",
    "F6_glued_label_iso_date": "Tel53 2026-11-14",
    "tckn_split": "1234567890 1",
    "email_ideographic_dot": "ali@ornek。com",
    "dora_dots": "D.O.R.A",
    "dora_spaced": "d o r a",
    "dora_suffix": "Doraya",
    "teb_apostrophe": "TEB'in",
    "homoglyph_cyrillic_d": "ԁora",
    "homoglyph_cyrillic_o": "DоRA",
    # LESSONS1FIX4: CLREV-LESSONS1FIX3 F1 (separator runs 1..4) and F3 (leet, small caps, spelled e-mail)
    "F1_double_dash": "12345 -- 67890 1",
    "F1_spaced_dash": "12345 - 678901",
    "F1_newlines": "12345\n67890\n1",
    "F1_crlf_underscore": "12345\r\n67890_1",
    "F3_leet_dora": "d0ra",
    "F3_leet_musteri": "mu5t3r1",
    "F3_small_caps_dora": "ᴅora",
    "F3_small_caps_all": "ᴅᴏʀᴀ",
    "F3_spelled_email": "salih at example dot com",
    "F3_paren_email": "salih (at) example (dot) com",
    "F3_bracket_email": "salih [AT] example [DOT] com",
    # FIX4 review hits: a nearby number must not hide the id; '@' + spelled dot; Turkish 'nokta'
    "R_list_number": "1. 12345 678901",
    "R_table_cell": "| 12345 678901 | 3 |",
    "R_at_sign_dot": "salih@example dot com",
    "R_nokta": "salih at gmail nokta com",
}
SHA_DATE_RULE = "O9 kural 3ac9e3786a660ebac394c44a993e4801b4396058 2026-11-14 notu"
NOT_PHONES = ("O52 kurali 2026-11-14 12:30 tarihinde", "600000 ms, 80 cagri, 45 dk",
              "PR 5321 merged 2026", "O52 600000 3600 1234567", "plan 2.1, 4.2, 8 bolum",
              "sha e0bcc99a 4dee6b68", "O27 UIRED5 3 test", SHA_DATE_RULE,
              # LESSONS1FIX3: words need their skeleton boundary, so these stay clean
              "kod orani", "ette bu", "tebrik", "en paralel", "test sayini", "endorphin", "isbn",
              # LESSONS1FIX4 review: dates, hex shas under leet, at/dot inside words
              "2026-11-14 (124 tests)", "2026-11-14 9:30 toplanti", "14.11.2026 9:30",
              "commit a7eb1f0c merged", "update dotfiles", "Validate dotenv loading")
# LESSONS1FIX2: comma is now a separator, so ten digits in a comma list read as a phone (fail closed)
# LESSONS1FIX3: shapes also run on the unmasked text (CDX F6), so a rule id glued to an ISO date
# reads as ten digits; any carve-out would reopen "Tel53 2026-11-14" (fail closed)
ACCEPTED_FALSE_POSITIVES = ("plan 2.1, 4.2, 8, 11.4, 12 bolum", "O52 2026-11-14 notu",
                            "O29 (2026-10-18) kural", "O2-7, 2026-11-07 notu")


class PhoneShapeTests(unittest.TestCase):
    def test_each_phone_shape_is_denied(self):
        deny = L.load_deny()
        for name, text in PHONES.items():
            with self.subTest(name):
                self.assertTrue(L.denied(f"O9 kural {text} son", deny))

    def test_each_other_shape_is_denied(self):
        deny = L.load_deny()
        for name, text in OTHER_SHAPES.items():
            with self.subTest(name):
                self.assertTrue(L.denied(f"O9 kural {text} son", deny))

    def test_rule_text_without_phones_is_not_denied(self):
        deny = L.load_deny()
        for text in NOT_PHONES:
            with self.subTest(text):
                self.assertFalse(L.denied(text, deny))

    def test_accepted_false_positives_are_denied(self):
        deny = L.load_deny()
        for text in ACCEPTED_FALSE_POSITIVES:
            with self.subTest(text):
                self.assertTrue(L.denied(text, deny))


if __name__ == "__main__":
    unittest.main()
