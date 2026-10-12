import time
import unittest
from unittest import mock

from jarvis import memory_filter
from jarvis.memory_filter import MAX_CANDIDATE_CHARS, FilterVerdict, classify_candidate_text

# category -> (refused samples, near-miss samples that stay allowed)
CASES = {
    "protected_path": (
        [
            "data\\jarvis.db içeriği: SQLite format 3",
            "~\\.jarvis\\config.json contents: {\"theme\": \"dark\"}",
            ".env.local dosyası şöyle:\nOPENAI_BASE=x",
            "C:\\JARVIS\\.env contents: BASE_URL=http://x",
            "config/.env: BASE_URL=http://x",
        ],
        [
            "Never commit .env files to git",
            "data klasörü korunur",
            "The metadata/ folder is regenerated on build",
        ],
    ),
    "env_secret": (
        [
            "OPENAI_API_KEY=abc123def",
            "export DB_PASSWORD=hunter2",
            "Ayar satırı:\nGITHUB_TOKEN = ghx_local_value",
            "OPENAIAPIKEY=abc123",
        ],
        ["MAX_TOKENS=4096", "DEBUG=true", "KEYBOARD_LAYOUT=tr"],
    ),
    "credential": (
        [
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA",
            "postgres://admin:s3cretpw@db.local/app",
            "Wi-Fi şifresi: Kediler2024",
            "my password is hunter2",
            "use ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
            "-----BEGIN PGP PRIVATE KEY BLOCK-----",
            "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
            "xoxb-" + "1234567890-abcdefghij",
            "Wi-Fi şifresi Kediler2024",
            "şifrem 1234",
        ],
        [
            "Reset your password from the settings page",
            "Şifre politikası en az 12 karakter",
            "Open https://example.com:8080/path",
        ],
    ),
    "payment_card": (
        [
            "4111 1111 1111 1111",
            "Kart numaram 5500-0000-0000-0004",
            "Kart: 4111 1111 1111 1111 12/27",
            "kart 4111 1111 1111 1111 123",
            "no 1 4111 1111 1111 1111",
        ],
        ["4111 1111 1111 1112", "Card ending 5500-0000-0000-0005 expired"],
    ),
    "iban": (
        [
            "TR33 0006 1005 1978 6457 8413 26",
            "IBAN: DE89 3704 0044 0532 0130 00",
            "GB82 WEST 1234 5698 7654 32 hesabına",
            "iban: tr33 0006 1005 1978 6457 8413 26",
        ],
        ["Flight TK1234 departs at 10:30", "ISO code TR34 is not an account"],
    ),
    "national_id": (
        ["TC kimlik no: 10000000146", "Kimlik 12345678950", "My SSN is 123-45-6789"],
        ["Kimlik 10000000147", "Order 12345678901 shipped", "SSN format 000-12-3456"],
    ),
    "phone": (
        [
            "Beni 0532 123 45 67'den ara",
            "+90 532 123 4567",
            "call me at (415) 555-0132",
            "office +44 20 7946 0958",
            "Ev telefonum 0212 345 67 89",
        ],
        ["Upgrade to version 10.2.3.4", "Meeting on 2026-10-12 at 14:30", "ticket 4521 closed"],
    ),
    "address": (
        [
            "Moda Mah. Bahariye Cad. No:12 D:3 Kadıköy",
            "I live at 221B Baker Street, London",
            "My home address is 5 Elm Rd",
        ],
        ["I live on Baker Street", "Atatürk Caddesi çok kalabalık", "Sokak hayvanları için mama aldım"],
    ),
    "health": (
        [
            "I was diagnosed with diabetes last year",
            "Depresyon için antidepresan kullanıyorum",
            "Annem hamile",
            "kronik hastalık geçmişim var",
            "I am depressed lately",
        ],
        ["Cancel the deploy", "Open the diagnostics panel", "Tansu ile toplantı", "Hastane yolu kapalı"],
    ),
}


class CategoryTableTests(unittest.TestCase):
    def test_every_category_refuses_positives_and_allows_near_misses(self):
        for category, (positives, near_misses) in CASES.items():
            self.assertGreaterEqual(len(positives), 2, category)
            self.assertGreaterEqual(len(near_misses), 2, category)
            for text in positives:
                with self.subTest(category=category, sample=text):
                    verdict = classify_candidate_text(text)
                    self.assertFalse(verdict.allowed)
                    self.assertEqual(verdict.category, category)
            for text in near_misses:
                with self.subTest(category=category, near_miss=text):
                    self.assertEqual(
                        classify_candidate_text(text),
                        FilterVerdict(True, None, "no never-stored category matched"),
                    )

    def test_reason_never_echoes_the_sensitive_text(self):
        verdict = classify_candidate_text("my password is hunter2")
        self.assertNotIn("hunter2", verdict.reason)

    def test_zero_width_characters_do_not_hide_a_match(self):
        self.assertEqual(classify_candidate_text("4111\u200b 1111 1111 1111").category, "payment_card")


class FailClosedTests(unittest.TestCase):
    def test_too_long_is_refused(self):
        verdict = classify_candidate_text("a" * (MAX_CANDIDATE_CHARS + 1))
        self.assertEqual((verdict.allowed, verdict.category), (False, "too_long"))
        self.assertTrue(classify_candidate_text("a" * MAX_CANDIDATE_CHARS).allowed)

    def test_nfkc_expansion_cannot_bypass_the_length_cap(self):
        verdict = classify_candidate_text("ﷺ" * MAX_CANDIDATE_CHARS)  # 1 char -> 18 under NFKC
        self.assertEqual((verdict.allowed, verdict.category), (False, "too_long"))

    def test_exception_inside_filter_is_refused(self):
        with mock.patch.object(memory_filter, "_scan", side_effect=RuntimeError("boom")):
            verdict = classify_candidate_text("harmless note")
        self.assertEqual((verdict.allowed, verdict.category), (False, "filter_error"))

    def test_non_string_input_is_refused(self):
        for value in (None, b"bytes", 42):
            with self.subTest(value=value):
                verdict = classify_candidate_text(value)
                self.assertEqual((verdict.allowed, verdict.category), (False, "filter_error"))


class BoundedRuntimeTests(unittest.TestCase):
    ADVERSARIAL = (
        "a-" * 50_000,
        "0" * 100_000,
        "1 " * 50_000,
        "+1 " * 33_334,
        "mah " * 25_000,
        "data\\" * 20_000,
        "a://" * 25_000,
        "a://b:" * 16_667,
        "x=" * 50_000,
        "A1" * 50_000,
        "TR00 " * 20_000,
        "1 Abc " * 16_667,
        ".env " * 20_000,
        "şifre" * 20_000,
    )

    def test_scan_is_linear_on_100k_adversarial_strings(self):
        for text in self.ADVERSARIAL:
            with self.subTest(shape=text[:8]):
                self.assertGreaterEqual(len(text), 100_000)
                start = time.perf_counter()
                memory_filter._scan(text)
                self.assertLess(time.perf_counter() - start, 1.0)

    def test_public_entry_refuses_100k_input_fast(self):
        start = time.perf_counter()
        verdict = classify_candidate_text("a-" * 50_000)
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertEqual(verdict.category, "too_long")


if __name__ == "__main__":
    unittest.main()
