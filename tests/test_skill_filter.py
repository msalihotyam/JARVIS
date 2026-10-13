import dataclasses
import inspect
import time
import unicodedata
import unittest
from unittest import mock

from jarvis import skill_filter
from jarvis.contracts import DataClassification
from jarvis.memory_filter import MAX_CANDIDATE_CHARS, classify_candidate_text
from jarvis.skill_filter import (
    MAX_FIELD_NAME_CHARS,
    MAX_FIELDS,
    FieldVerdict,
    check_fields,
    preview_text,
)

COMBINING_ACUTE = chr(0x301)
FULLWIDTH_PATH = "".join(chr(0xFF00 + ord(c) - 0x20) for c in "C:\\Users\\salih\\notes.txt")

CLEAN_DRAFT = (
    ("name", "Haftalık test raporu"),
    ("goal", "Run the unit tests and summarise the failures for the weekly report."),
    ("steps-1-text", "Depoyu aç ve birim testlerini çalıştır."),
    ("steps-2-text", "Open build/reports and write a short summary of failing tests."),
    ("steps-3-text", "The metadata/ folder is regenerated on build; leave it as is."),
    ("checks-1", "Rapor dosyası oluştu mu kontrol et."),
)


def one(text, field="steps-1-text", classification=None):
    return check_fields([(field, text)], classification)


class RefusedCategoryTests(unittest.TestCase):
    def assertRefused(self, verdict, category, field="steps-1-text"):
        self.assertIsInstance(verdict, FieldVerdict)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.category, category)
        self.assertEqual(verdict.field, field)

    def test_api_key_password_and_private_key_in_a_step_are_refused(self):
        samples = {
            "api_key=abc123def456ghi": "env_secret",
            "Use the api key: abc123def456ghi in the header": "credential",
            "Set the header to Bearer abcdefghijklmnop123": "credential",
            "run deploy --token s3cr3tvalue": "credential",
            "password: hunter2": "credential",
            "Wi-Fi şifresi Kediler2024": "credential",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA": "credential",
            "OPENAI_API_KEY=abc123def": "env_secret",
        }
        for text, category in samples.items():
            with self.subTest(text=text):
                self.assertRefused(one(text), category)

    def test_tc_kimlik_and_card_number_are_refused(self):
        self.assertRefused(one("Formdaki TC kimlik no 10000000146 olsun"), "national_id")
        self.assertRefused(one("Kart 4111 1111 1111 1111 ile öde"), "payment_card")
        self.assertRefused(one("IBAN TR33 0006 1005 1978 6457 8413 26"), "iban")

    def test_protected_state_paths_are_refused_even_without_content(self):
        for text in (
            "open data\\jarvis.db and look at the tasks",
            "copy ~\\.jarvis\\config.json to the desktop",
            "read data/settings.json first",
            "the .jarvis folder holds the profile",
            "back up jarvis.db every night",
        ):
            with self.subTest(text=text):
                self.assertTrue(classify_candidate_text(text).allowed, "memory_filter allows it")
                self.assertRefused(one(text), "protected_path")

    def test_absolute_local_paths_are_refused_not_redacted(self):
        for text in (
            "save it to C:\\Users\\salih\\Desktop\\out.txt",
            "write /home/salih/notes/today.md",
            "share \\\\server\\share\\plan.xlsx",
        ):
            with self.subTest(text=text):
                self.assertTrue(classify_candidate_text(text).allowed, "memory_filter allows it")
                self.assertRefused(one(text), "local_path")

    def test_full_width_path_is_caught_on_the_normalised_spelling(self):
        text = "save it to " + FULLWIDTH_PATH
        self.assertTrue(classify_candidate_text(text).allowed)
        self.assertRefused(one(text), "local_path")

    def test_credential_redaction_catches_what_the_nfkc_scan_misses(self):
        # memory_filter scans the NFKC form, where the combining mark fuses with
        # the key's last letter and the key shape no longer ends on a boundary.
        # The raw-text redaction comparison is what refuses it.
        text = "deploy with sk-abcdefghijklmo" + COMBINING_ACUTE + " then check"
        self.assertTrue(classify_candidate_text(text).allowed)
        verdict = one(text)
        self.assertRefused(verdict, "credential")
        self.assertEqual(verdict.reason, "credential redaction would change the text")

    def test_other_memory_filter_categories_pass_through(self):
        self.assertRefused(one("Annemi 0532 123 45 67 numarasından ara"), "phone")
        self.assertRefused(one("x" * (MAX_CANDIDATE_CHARS + 1)), "too_long")

    def test_reason_never_echoes_the_text(self):
        verdict = one("api_key=abc123def456ghi")
        self.assertNotIn("abc123", verdict.reason)
        self.assertNotIn("api_key", verdict.reason)
        self.assertEqual(verdict.reason, "contains a .env-style secret assignment")

    def test_first_refused_field_is_named(self):
        fields = list(CLEAN_DRAFT) + [("inputs-2-default", "password: hunter2"), ("steps-9-text", "ok")]
        verdict = check_fields(fields)
        self.assertRefused(verdict, "credential", field="inputs-2-default")


class ClassificationTests(unittest.TestCase):
    def test_secret_and_sensitive_sources_are_refused(self):
        for value in (DataClassification.SECRET, DataClassification.SENSITIVE, "secret", "sensitive"):
            with self.subTest(value=value):
                verdict = check_fields(CLEAN_DRAFT, value)
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.category, "classification")
                self.assertIsNone(verdict.field)

    def test_unknown_classification_is_refused(self):
        for value in ("top-secret", "SECRET", 3, True, object()):
            with self.subTest(value=value):
                verdict = check_fields(CLEAN_DRAFT, value)
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.category, "classification")

    def test_public_internal_and_unknown_origin_allow_a_clean_draft(self):
        for value in (None, DataClassification.PUBLIC, DataClassification.INTERNAL, "internal"):
            with self.subTest(value=value):
                self.assertTrue(check_fields(CLEAN_DRAFT, value).allowed)

    def test_secret_classification_refuses_even_empty_fields(self):
        self.assertFalse(check_fields([], DataClassification.SECRET).allowed)


class AllowedTests(unittest.TestCase):
    def test_clean_tr_en_draft_is_allowed(self):
        verdict = check_fields(CLEAN_DRAFT)
        self.assertEqual(verdict, FieldVerdict(True, None, None, "no never-stored category matched"))

    def test_generator_and_items_view_are_accepted(self):
        self.assertTrue(check_fields(pair for pair in CLEAN_DRAFT).allowed)
        self.assertTrue(check_fields(dict(CLEAN_DRAFT).items()).allowed)

    def test_field_count_is_bounded(self):
        clean = ("steps-1-text", "Testleri çalıştır.")
        self.assertTrue(check_fields([clean] * MAX_FIELDS).allowed)
        verdict = check_fields([clean] * (MAX_FIELDS + 1))
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.category, "too_many_fields")


class FailClosedTests(unittest.TestCase):
    def assertFilterError(self, verdict):
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.category, "filter_error")

    def test_malformed_input_refuses(self):
        for fields in (
            "steps",
            b"steps",
            {"steps-1-text": "ok"},
            [["steps-1-text", "ok"]],
            [("steps-1-text",)],
            [(None, "ok")],
            [(7, "ok")],
            None,
            42,
        ):
            with self.subTest(fields=fields):
                self.assertFilterError(check_fields(fields))

    def test_non_str_text_refuses_and_names_the_field(self):
        for text in (None, 7, b"bytes", ["x"]):
            with self.subTest(text=text):
                verdict = one(text)
                self.assertFilterError(verdict)
                self.assertEqual(verdict.field, "steps-1-text")

    def test_exception_in_any_check_refuses(self):
        targets = ("classify_candidate_text", "redact_credentials", "redact_local_paths")
        for target in targets:
            with self.subTest(target=target), mock.patch.object(
                skill_filter, target, side_effect=RuntimeError("boom")
            ):
                self.assertFilterError(check_fields(CLEAN_DRAFT))

    def test_exception_from_the_iterable_refuses(self):
        def broken():
            yield CLEAN_DRAFT[0]
            raise RuntimeError("boom")

        self.assertFilterError(check_fields(broken()))


def invisible_samples(char):
    """The review's examples with ``char`` hidden inside the number, key or path."""
    return (
        ("Kimlik 1234" + char + "5678950", "national_id"),
        ("Kart 4111" + char + "1111" + char + "1111" + char + "1111", "payment_card"),
        ("IBAN TR33" + char + "0006100519786457841326", "iban"),
        ("Use sk-abcdef" + char + "ghijklmnopqrstuv", "credential"),
        ("Open the .jar" + char + "vis\\ folder", "protected_path"),
    )


class InvisibleCharacterTests(unittest.TestCase):
    """Every Cf character, U+034F, U+180E and the variation selectors are removed
    from the cleaned form, and memory_filter runs on that form too."""

    def test_samples_are_refused_without_invisible_characters(self):
        for text, category in invisible_samples(""):
            with self.subTest(text=text):
                self.assertFalse(one(text).allowed)
                self.assertEqual(one(text).category, category)

    def assertClassRefused(self, code_points):
        for code_point in code_points:
            for text, category in invisible_samples(chr(code_point)):
                with self.subTest(code_point=hex(code_point), category=category):
                    verdict = one(text)
                    self.assertFalse(verdict.allowed)
                    self.assertEqual(verdict.category, category)
                    self.assertEqual(verdict.field, "steps-1-text")

    def test_bidi_marks_and_isolates(self):
        self.assertClassRefused((0x200E, 0x200F, 0x202A, 0x202C, 0x202E, 0x2066, 0x2069))

    def test_arabic_letter_mark(self):
        self.assertClassRefused((0x061C,))

    def test_zero_width_soft_hyphen_word_joiner_and_bom(self):
        self.assertClassRefused((0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))

    def test_invisible_math_operators(self):
        self.assertClassRefused((0x2061, 0x2062, 0x2063, 0x2064))

    def test_mongolian_vowel_separator(self):
        self.assertClassRefused((0x180E,))

    def test_tag_block(self):
        self.assertClassRefused((0xE0001, 0xE0020, 0xE0041, 0xE007F))

    def test_combining_grapheme_joiner(self):
        self.assertClassRefused((0x034F,))

    def test_variation_selectors(self):
        self.assertClassRefused((0xFE00, 0xFE07, 0xFE0F))

    def test_variation_selectors_supplement(self):
        self.assertClassRefused((0xE0100, 0xE0150, 0xE01EF))

    def test_every_format_character_is_removed(self):
        format_chars = [
            code_point
            for code_point in range(0x110000)
            if unicodedata.category(chr(code_point)) == "Cf"
        ]
        self.assertGreater(len(format_chars), 100)
        for code_point in format_chars:
            with self.subTest(code_point=hex(code_point)):
                verdict = one("Kimlik 1234" + chr(code_point) + "5678950")
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.category, "national_id")

    def test_clean_text_with_invisible_characters_stays_allowed(self):
        text = "Depoyu" + chr(0x200B) + " aç ve " + chr(0x200E) + "testleri çalıştır." + chr(0xFE0F)
        self.assertTrue(one(text).allowed)


# D17: every Default_Ignorable_Code_Point as explicit inclusive ranges, plus
# U+2800. Kept here independently of the module so dropping a range fails.
D17_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
    (0x2800, 0x2800),
)


def d17_samples(char):
    """The re-review's probes with ``char`` hidden inside the value."""
    return (
        ("Kimlik 1234" + char + "5678950", "national_id"),
        ("Kart 4111" + char + "1111" + char + "1111" + char + "1111", "payment_card"),
        ("Use sk-abcdef" + char + "ghijklmnopqrstuv", "credential"),
        ("Mail salih" + char + "@dora.com.tr", "email"),
        ("Open jarvis" + char + ".db now", "protected_path"),
    )


class D17InvisibleTests(unittest.TestCase):
    """D17: the cleaned form drops every code point of the D17 list before
    memory_filter, the redaction checks and the e-mail check."""

    def assertProbesRefused(self, code_points):
        for code_point in code_points:
            for text, category in d17_samples(chr(code_point)):
                with self.subTest(code_point=hex(code_point), category=category):
                    verdict = one(text)
                    self.assertFalse(verdict.allowed)
                    self.assertEqual(verdict.category, category)
                    self.assertEqual(verdict.field, "steps-1-text")
                    self.assertEqual(preview_text(text), "[HIDDEN: " + category + "]")

    def test_probes_are_refused_without_invisible_characters(self):
        self.assertProbesRefused(())
        for text, category in d17_samples(""):
            with self.subTest(text=text):
                self.assertEqual(one(text).category, category)

    def test_mongolian_free_variation_selectors(self):
        self.assertProbesRefused(range(0x180B, 0x1810))

    def test_khmer_inherent_vowels(self):
        self.assertProbesRefused((0x17B4, 0x17B5))

    def test_hangul_fillers(self):
        self.assertProbesRefused((0x115F, 0x1160, 0x3164, 0xFFA0))

    def test_braille_blank_and_other_d17_points(self):
        self.assertProbesRefused((0x2800, 0x206A, 0x206F, 0xFFF0, 0xFFF8, 0x1BCA0, 0x1D173, 0xE0000, 0xE0FFF))

    def test_every_d17_code_point_is_removed(self):
        code_points = [cp for first, last in D17_RANGES for cp in range(first, last + 1)]
        self.assertGreater(len(code_points), 4000)
        for code_point in code_points:
            with self.subTest(code_point=hex(code_point)):
                verdict = one("Kimlik 1234" + chr(code_point) + "5678950")
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.category, "national_id")

    def test_clean_text_with_d17_characters_stays_allowed(self):
        text = "Raporu" + chr(0x180B) + " aç, " + chr(0x3164) + "özeti" + chr(0x17B4) + " yaz."
        self.assertTrue(one(text).allowed)


class LengthBeforeNormalisationTests(unittest.TestCase):
    """The length limit is checked before NFKC (U+FDFA expands 18-fold)."""

    HUGE = chr(0xFDFA) * 2_000_000

    def test_huge_expanding_text_is_refused_quickly(self):
        start = time.perf_counter()
        verdict = one(self.HUGE)
        preview = preview_text(self.HUGE)
        elapsed = time.perf_counter() - start
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.category, "too_long")
        self.assertEqual(verdict.field, "steps-1-text")
        self.assertEqual(preview, "[HIDDEN: too_long]")
        self.assertLess(elapsed, 1.0)

    def test_over_long_text_is_never_normalised(self):
        real_normalize = unicodedata.normalize

        def guarded(form, text):
            if len(text) > MAX_CANDIDATE_CHARS:
                raise AssertionError("over-long text was normalised")
            return real_normalize(form, text)

        with mock.patch.object(skill_filter.unicodedata, "normalize", side_effect=guarded):
            self.assertEqual(one("x" * (MAX_CANDIDATE_CHARS + 1)).category, "too_long")
            self.assertEqual(preview_text(self.HUGE), "[HIDDEN: too_long]")

    def test_text_at_the_limit_is_still_checked(self):
        self.assertTrue(one("a" * MAX_CANDIDATE_CHARS).allowed)
        self.assertEqual(one(chr(0xFDFA) * 1000).category, "too_long")


class EmailTests(unittest.TestCase):
    def test_email_addresses_are_refused(self):
        for text in (
            "Mail salih@dora.com.tr adresine",
            "Raporu ahmet.yılmaz@şirket.com.tr adresine gönder.",
            "Kopyayı ayşe_öztürk@örnek.tr kişisine de at.",
            "Send the summary to john.doe+reports@example.co.uk.",
            "Notify root@localhost when the job ends.",
            "Bildirimi admin@127.0.0.1 adresine yolla.",
            "Write to postmaster@[192.168.1.1] if it fails.",
            "Ping " + "a" * 80 + "@example.com first.",
            "Mail salih" + chr(0xFF20) + "dora.com.tr adresine",
            "Mail salih" + chr(0x200B) + "@dora.com.tr adresine",
        ):
            with self.subTest(text=text):
                self.assertTrue(classify_candidate_text(text).allowed, "memory_filter allows it")
                verdict = one(text)
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.category, "email")
                self.assertEqual(verdict.reason, "contains an e-mail address")

    def test_at_sign_without_an_address_is_allowed(self):
        for text in (
            "@salih'e haber ver.",
            "Use the @property decorator in Python.",
            "Fiyat 5 @ 10 TL olarak yaz.",
            "E-posta adresini {email} alanına yaz.",
        ):
            with self.subTest(text=text):
                self.assertTrue(one(text).allowed)


class FieldNameTests(unittest.TestCase):
    def assertNameRefused(self, name):
        verdict = check_fields([(name, "Testleri çalıştır.")])
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.category, "field_name")
        self.assertIsNone(verdict.field, "a refused name is never echoed")
        if name:
            self.assertNotIn(name, verdict.reason)

    def test_id_names_are_accepted(self):
        for name in ("name", "goal", "steps-1-text", "inputs_2_default", "a" * MAX_FIELD_NAME_CHARS):
            with self.subTest(name=name):
                self.assertTrue(check_fields([(name, "Testleri çalıştır.")]).allowed)

    def test_names_outside_the_id_grammar_are_refused(self):
        for name in (
            "",
            "Steps",
            "steps[1].text",
            "a" * (MAX_FIELD_NAME_CHARS + 1),
            "name\n",
            "adı",
            "C:\\Users",
            "step 1",
            "steps" + chr(0x200B),
        ):
            with self.subTest(name=name):
                self.assertNameRefused(name)

    def test_names_matching_a_never_stored_category_are_refused(self):
        for name in ("10000000146", "sk-abcdefghijklmnopqrstuvwx", "4111111111111111"):
            with self.subTest(name=name):
                self.assertNameRefused(name)


class PreviewTests(unittest.TestCase):
    def test_refused_text_is_replaced_by_a_fixed_marker(self):
        samples = {
            "Wi-Fi şifresi: Kediler2024": "credential",
            "my password is hunter2": "credential",
            "DB_PASS=hunter2": "env_secret",
            "PIN=4821": "env_secret",
            "parola = s3cr3t": "credential",
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq": "credential",
            "sk_live_abcdefghijklmnop1234": "credential",
            "https://salih:hunter2@host.com/x": "credential",
            "Ev adresim: Atatürk Cad. No:5": "address",
            "Kart 4111 1111 1111 1111 ile öde": "payment_card",
            "Formdaki TC kimlik no 10000000146 olsun": "national_id",
            "save to C:\\Users\\salih\\out.txt": "local_path",
            "Mail salih@dora.com.tr adresine": "email",
            "Kimlik 1234" + chr(0x200E) + "5678950": "national_id",
        }
        for text, category in samples.items():
            with self.subTest(text=text):
                self.assertEqual(one(text).category, category)
                self.assertEqual(preview_text(text), "[HIDDEN: " + category + "]")

    def test_marker_never_carries_any_part_of_the_text(self):
        hidden_key = "sk-abcdefghijklmo" + COMBINING_ACUTE
        shown = preview_text("use " + hidden_key + " and " + FULLWIDTH_PATH)
        self.assertEqual(shown, "[HIDDEN: credential]")

    def test_allowed_text_is_returned_unchanged(self):
        for _name, text in CLEAN_DRAFT:
            with self.subTest(text=text):
                self.assertEqual(preview_text(text), text)

    def test_over_long_text_is_hidden(self):
        self.assertEqual(preview_text("a" * (MAX_CANDIDATE_CHARS * 2)), "[HIDDEN: too_long]")

    def test_preview_fails_closed(self):
        self.assertEqual(preview_text(None), "[HIDDEN: filter_error]")
        for target in ("classify_candidate_text", "redact_credentials", "redact_local_paths"):
            with self.subTest(target=target), mock.patch.object(
                skill_filter, target, side_effect=RuntimeError("boom")
            ):
                self.assertEqual(preview_text("Testleri çalıştır."), "[HIDDEN: filter_error]")


class AuthorityBoundaryTests(unittest.TestCase):
    def test_verdict_carries_no_authority_fields(self):
        names = {f.name for f in dataclasses.fields(FieldVerdict)}
        self.assertEqual(names, {"allowed", "field", "category", "reason"})

    def test_module_is_pure(self):
        source = inspect.getsource(skill_filter)
        for forbidden in ("sqlite3", "UPDATE ", "DELETE ", "INSERT ", "open(", "logging", "settings"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
