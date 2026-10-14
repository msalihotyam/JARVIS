"""MEMFILTER2: obfuscated spellings the never-store filter must still refuse.

Every input from build/v22/S1_NOTES.md section 4 is refused here, plus the
spec's extra spellings; the control table keeps ordinary text allowed.
"""

import time
import unittest

from jarvis import memory_filter
from jarvis.memory_filter import FilterVerdict, classify_candidate_text

ALLOWED = FilterVerdict(True, None, "no never-stored category matched")
COMBINING_ACUTE = "́"
PKCS8_BODY = (
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"
    "MzEfYyjiWA4R4/M2bS1GB4t7NXp98C3SC6dVMvDuictGeurT8jNbvJZHtCSuYEvu"
    "NMoSfm76oqFvAp8Gy0iz5sxjZmSnXyCdPEovGhLa0VzMaQ8s+CLOyS56YyCFGeJZ"
    "qgtzJ6GR3eqoYSW9b9UMvkBpZODSctWSNGj3P7jRFDO5VoTwCQAWbFnOjDfH5Ulgp2"
)


def arabic_indic(digits: str) -> str:
    return "".join(chr(0x660 + int(d)) for d in digits)


def devanagari(digits: str) -> str:
    return "".join(chr(0x966 + int(d)) for d in digits)


# D17 default-ignorables (S1_NOTES section 6) plus the blank braille cell.
D17_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF), (0x2800, 0x2800),
)

# S1_NOTES section 4, one entry per listed input -> expected category.
S1_SECTION4 = {
    "Kart 4111.1111.1111.1111": "payment_card",
    "Kart 4111/1111/1111/1111": "payment_card",
    "Kart 4111  1111  1111  1111": "payment_card",
    "Kimlik 123 456 789 50": "national_id",
    "123.456.789.50": "national_id",
    "IBAN TR33-0006-1005-1978-6457-8413-26": "iban",
    "Parolam KediTopu": "credential",
    "password hunter2": "credential",
    "key sk-AbC123": "credential",
    "-----begin rsa private key-----": "credential",
    PKCS8_BODY[:200]: "credential",
    "TC " + arabic_indic("10000000146"): "national_id",
    "Kimlik 1234" + COMBINING_ACUTE + "5678950": "national_id",
    "Kimlik 1234ᅟ5678950": "national_id",
    "Kimlik 1234ㅤ5678950": "national_id",
}

# The spec's further spellings of the same families.
SPEC_EXTRAS = {
    "Kart 4111-1111-1111-1111": "payment_card",
    "Kart 4111 - 1111 - 1111 - 1111": "payment_card",
    "Kart 5500.0000.0000.0004 son kullanma 12/27": "payment_card",
    "Kart " + devanagari("4111111111111111"): "payment_card",
    "Kart 4111" + COMBINING_ACUTE + " 1111 1111 1111": "payment_card",
    "Kimlik 123-456-789-50": "national_id",
    "Kimlik 123 45 67 89 50": "national_id",
    "Kimlik 123.45.67.89.50": "national_id",
    "Kimlik 123-45-67-89-50": "national_id",
    "Kimlik 123  456  789  50": "national_id",
    "IBAN TR33 0006 1005 1978 6457 8413 26": "iban",
    "IBAN TR33  0006  1005  1978  6457  8413  26": "iban",
    "IBAN GB82-WEST-1234-5698-7654-32": "iban",
    "IBAN GB82 WEST" + COMBINING_ACUTE + " 1234 5698 7654 32": "iban",
    "sifrem: kedi": "credential",
    "Şifremiz KediTopu": "credential",
    "şifrem\nKediTopu": "credential",
    "password - hunter2": "credential",
    "password -> hunter2": "credential",
    "password\nhunter2": "credential",
    "key - sk-AbC123": "credential",
    "token 0123456789abcdef0123": "credential",
    "password: hunter2": "credential",
    "wifi pwd hunter2": "credential",
    "api key AbC123xyz": "credential",
    "token: x9Kq2LmZ": "credential",
    "secret abc123def456": "credential",
    "-----Begin Private Key-----": "credential",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----": "credential",
    "----- BEGIN PRIVATE KEY -----": "credential",
    "----BEGIN RSA PRIVATE  KEY----": "credential",
    "Kart 4111–1111–1111–1111": "payment_card",
    "Kart 4111−1111−1111−1111": "payment_card",
    "Kart 4111ः1111ः1111ः1111": "payment_card",
    "Kart 3782 822463 10005": "payment_card",
    "Kart 3782.822463.10005": "payment_card",
    "Kart 4111.1111.1111.1111.003": "payment_card",
    "Kimlik 123–456–789–50": "national_id",
    "Kimlik 123 - 456 - 789 - 50": "national_id",
    "IBAN TR33—0006—1005—1978—6457—8413—26": "iban",
    "IBAN TR33 - 0006 - 1005 - 1978 - 6457 - 8413 - 26": "iban",
    "IBAN: TR 33 0006 1005 1978 6457 8413 26": "iban",
    "pasted:\n" + PKCS8_BODY[:64]: "credential",
}

# Ordinary text that must stay allowed (over-blocking guard).
CONTROLS = (
    "Bugün hava çok güzel, yürüyüşe çıktım",
    "Remind me to call mom tomorrow",
    "Toplantı 14.10.2026 saat 15:30'da",
    "Meeting moved to 2026-10-14",
    "Tarih: 14/10/2026",
    "1.5.2026 - 7.5.2026 arası izindeyim",
    "Saat 09.30'da kahvaltı",
    "Version 3.12.4 released",
    "Upgrade from v2.1.0 to v2.2.0",
    "Python 3.13 ve Qt 6.8 kullanıyoruz",
    "Sipariş no 784512369 kargoya verildi",
    "Order #100234 shipped",
    "Fiyatı 1.250,00 TL",
    "Price was $1,299.99",
    "Toplam 12.500 TL ödendi",
    "Skor 3-2 bitti",
    "Kafede 3 çay, 2 kahve içtik",
    "Şifremi unuttum, sıfırlama linki istemem lazım",
    "Reset your password from the settings page",
    "The keyboard shortcut is Ctrl+K",
    "Press key F5 to refresh",
    "max token 4096 olarak ayarlandı",
    "API key rotation is every 90 days",
    "Secret santa çekilişi Cuma",
    "Sunucu 192.168.1.10 üzerinde",
    "Kimlik 123 456 789 51",
    "Kimlik 123 45 67 89 51",
    "Ödeme 4111.1111.1111.1112",
    "Naïve résumé 2026",
    # review finding 1: date ranges, date/time pairs and IP lists are not cards
    "Kampanya 01.10.2026 - 31.10.2026 arasi",
    "18.09.2027 11.15-14.15",
    "03/06/2024 27/12/2026",
    "2026/08/27-2026/05/08",
    "sunucu 94.168.196.69 8080",
    "Siparis 781286 - 22.05.2021",
    "Sunucular 192.168.100.200 ve 10.20.30.40",
    # review finding 2: label words in ordinary prose
    "Şifrem unutuldu",
    "Parolam değişti",
    "şifremiz yenilendi",
    "Key base64",
    "API key sha256",
    "issue key PROJ-123",
    "token utf8mb4",
    "secret santa2025",
    "Yıllar 2020—2021 arası",
    "e" + COMBINING_ACUTE + " 1 ve 2",
    # the raw-redaction proof in skill_filter/experience relies on this staying allowed
    "deploy with sk-abcdefghijklmo" + COMBINING_ACUTE + " then check",
)


class ObfuscatedSpellingTests(unittest.TestCase):
    def assertRefusedAs(self, text, category):
        verdict = classify_candidate_text(text)
        self.assertFalse(verdict.allowed, text)
        self.assertEqual(verdict.category, category, text)

    def test_every_s1_section4_input_is_refused(self):
        for text, category in S1_SECTION4.items():
            with self.subTest(text=text):
                self.assertRefusedAs(text, category)

    def test_spec_spellings_are_refused(self):
        for text, category in SPEC_EXTRAS.items():
            with self.subTest(text=text):
                self.assertRefusedAs(text, category)

    def test_any_d17_character_between_digits_does_not_hide_a_tc_kimlik(self):
        for low, high in D17_RANGES:
            for code in range(low, high + 1):
                with self.subTest(code=hex(code)):
                    self.assertRefusedAs("Kimlik 1234" + chr(code) + "5678950", "national_id")

    def test_marks_between_digits_do_not_hide_a_card(self):
        for mark in (COMBINING_ACUTE, "⃣", "ᅟ", "ᅠ", "ㅤ", "ﾠ"):
            with self.subTest(mark=hex(ord(mark))):
                self.assertRefusedAs("4111" + mark + "1111" + mark + "1111" + mark + "1111", "payment_card")

    def test_phone_numbers_are_not_compacted_into_an_id(self):
        for text in ("Beni 0532 123 45 67'den ara", "+90 532 123 45 67", "0212 345 67 89"):
            with self.subTest(text=text):
                self.assertRefusedAs(text, "phone")


class OverBlockingGuardTests(unittest.TestCase):
    def test_controls_stay_allowed(self):
        self.assertGreaterEqual(len(CONTROLS), 20)
        for text in CONTROLS:
            with self.subTest(text=text):
                self.assertEqual(classify_candidate_text(text), ALLOWED)

    def test_marks_survive_outside_numbers(self):
        self.assertEqual(memory_filter._normalise("sk-abcx" + COMBINING_ACUTE + " x"), "sk-abcx" + COMBINING_ACUTE + " x")
        self.assertEqual(memory_filter._normalise("12" + COMBINING_ACUTE + "34"), "1234")


class BoundedRuntimeTests(unittest.TestCase):
    ADVERSARIAL = (
        "1." * 50_000,
        "1 - " * 25_000,
        "123 " * 25_000,
        "1" + COMBINING_ACUTE * 99_999,
        ("1" + COMBINING_ACUTE) * 50_000,
        "TR33-" * 20_000,
        "key " * 25_000,
        "password" + " " * 99_992,
        "password" + "\t:" * 49_996,
        "-" * 100_000,
        "4111." * 20_000,
        "1.2/" * 25_000,
        "password " * 11_112,
        "token a1" * 12_500,
        "M" * 100_000,
        "MII" + "A" * 99_997,
        arabic_indic("1") * 100_000,
    )

    def test_scan_is_linear_on_100k_adversarial_strings(self):
        for text in self.ADVERSARIAL:
            with self.subTest(shape=text[:8]):
                self.assertGreaterEqual(len(text), 100_000)
                start = time.perf_counter()
                memory_filter._scan(text)
                self.assertLess(time.perf_counter() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
