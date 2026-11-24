import json
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.talk_glossary import DEFAULT_GLOSSARY_PATH, Glossary, GlossaryRow, Replacement


class TalkGlossaryTests(unittest.TestCase):
    def load_rows(self, rows):
        with patch.object(Path, "read_text", return_value=json.dumps(rows)):
            return Glossary.load()

    def test_seed_and_ordered_replacements(self):
        glossary = Glossary.load()
        terms = {row.canonical for row in glossary.rows}
        self.assertGreaterEqual(len(terms), 40)
        self.assertTrue({"N6", "key1", "INT", "CLREV", "konveyör", "Devir B"} <= terms)
        self.assertEqual(
            glossary.correct("en altı, EN ALTI!"),
            ("N6, N6!", [Replacement("en altı", "N6"), Replacement("EN ALTI", "N6")]),
        )

    def test_alias_inside_longer_word_is_preserved(self):
        glossary = Glossary.load()
        self.assertEqual(glossary.correct("ben altın ve en altın"), ("ben altın ve en altın", []))

    def test_alias_followed_by_any_mark_or_joiner_inside_word_is_preserved(self):
        glossary = Glossary.load()
        # Mn outside U+0300-036F, Mn, Mc, Me, Cf ZWNJ, Cf ZWJ, Cf soft hyphen
        marks = ("᪰", "⃐", "ः", "⃝", "‌", "‍", "­", "＿", "‿", "⁀", "\U0001f3fb")
        for mark in marks:
            for text in (f"en altı{mark}nın", f"x{mark}en altı"):
                with self.subTest(mark=hex(ord(mark)), text=text):
                    self.assertEqual(glossary.correct(text), (text, []))
        self.assertEqual(glossary.correct("en altı᪰"), ("en altı᪰", []))

    def test_decomposed_transcript_matches_precomposed_alias(self):
        glossary = self.load_rows([{"canonical": "N6", "aliases": ["í"]}])
        self.assertEqual(glossary.correct("İ́"), ("N6", [Replacement("İ́", "N6")]))
        self.assertEqual(glossary.correct("í"), ("N6", [Replacement("í", "N6")]))

    def test_aliases_equal_after_fold_and_nfc_are_refused_at_load(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.load_rows([
                {"canonical": "A", "aliases": ["í"]},
                {"canonical": "B", "aliases": ["İ́"]},
            ])

    def test_turkish_case_folding(self):
        glossary = self.load_rows([
            {"canonical": "N6", "aliases": ["ılık", "ileri"]},
        ])
        self.assertEqual(
            glossary.correct("ILIK İLERİ I\u0307LERI\u0307 ilik ıleri"),
            ("N6 N6 N6 ilik ıleri", [
                Replacement("ILIK", "N6"),
                Replacement("İLERİ", "N6"),
                Replacement("I\u0307LERI\u0307", "N6"),
            ]),
        )

    def test_ambiguous_alias_is_refused_at_load(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.load_rows([
                {"canonical": "N6", "aliases": ["ILIK"]},
                {"canonical": "N7", "aliases": ["ılık"]},
            ])

    def test_correction_is_idempotent_across_phrase_boundaries(self):
        glossary = self.load_rows([
            {"canonical": "Devir B", "aliases": ["devri be"]},
            {"canonical": "N6", "aliases": ["B plan"]},
        ])
        corrected, changes = glossary.correct("devri be plan")
        self.assertEqual(corrected, "Devir B plan")
        self.assertEqual(changes, [Replacement("devri be", "Devir B")])
        self.assertEqual(glossary.correct(corrected), (corrected, []))

    def test_hints_include_only_complete_canonical_terms_within_budget(self):
        glossary = self.load_rows([
            {"canonical": "N6", "aliases": ["en altı"]},
            {"canonical": "Devir B", "aliases": []},
        ])
        self.assertEqual(glossary.build_hint(0), "")
        self.assertEqual(glossary.build_hint(10), "N6")
        self.assertEqual(glossary.build_hint(11), "N6, Devir B")

    def test_candidate_does_not_write_to_glossary(self):
        before = DEFAULT_GLOSSARY_PATH.read_bytes()
        glossary = Glossary.load()
        _, changes = glossary.correct("en altı")
        self.assertEqual(changes[0].candidate_row("Review"), GlossaryRow("N6", ("en altı",), "Review"))
        self.assertEqual(DEFAULT_GLOSSARY_PATH.read_bytes(), before)

    def test_malformed_files_raise_without_publishing_rows(self):
        existing = Glossary.load()
        payloads = (
            "{",
            '[{"canonical":"Test","aliases":["deneme"]},{"canonical":"Bad","aliases":"bad"}]',
            '[{"canonical":"Test","canonical":"Other","aliases":[]}]',
        )
        for payload in payloads:
            with self.subTest(payload=payload), patch.object(Path, "read_text", return_value=payload):
                with self.assertRaises(ValueError):
                    Glossary.load()
        self.assertEqual(existing.correct("deneme"), ("deneme", []))
        self.assertEqual(Glossary.load().rows, existing.rows)
