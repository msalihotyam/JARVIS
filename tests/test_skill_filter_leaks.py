"""SKFILTERLEAK: the S1-notes section 4 leak classes skill_filter refuses.

One test per class: the leaking spellings are refused (and hidden by
preview_text), a near miss with a bad checksum or no secret shape stays allowed.
"""

import time
import unittest

from jarvis.skill_filter import check_fields, preview_text


def arabic_indic(digits):
    return "".join(chr(0x660 + int(char)) for char in digits)


class LeakClassTests(unittest.TestCase):
    def assertRefused(self, text, category):
        verdict = check_fields([("steps-1-text", text)])
        self.assertFalse(verdict.allowed, text)
        self.assertEqual(verdict.category, category, text)
        self.assertEqual(preview_text(text), f"[HIDDEN: {category}]")

    def assertAllowed(self, text):
        self.assertTrue(check_fields([("steps-1-text", text)]).allowed, text)
        self.assertEqual(preview_text(text), text)

    def test_card_with_dot_slash_or_double_space_separators(self):
        for text in (
            "Kart 4111.1111.1111.1111",
            "Kart 4111/1111/1111/1111",
            "Kart 4111  1111  1111  1111",
            "Kart 4111–1111–1111–1111",
            "Kart 4111\t1111\t1111\t1111",
            "Kart 4111 ́1111 1111 1111",
        ):
            self.assertRefused(text, "payment_card")
        self.assertAllowed("Kart 4111.1111.1111.1112")  # Luhn fails

    def test_grouped_tc_kimlik(self):
        for text in (
            "Kimlik 123 456 789 50",
            "Kimlik 123.456.789.50",
            "Kimlik 123 45 67 89 50",
        ):
            self.assertRefused(text, "national_id")
        self.assertAllowed("Kimlik 123.456.789.51")  # checksum fails

    def test_separated_iban(self):
        self.assertRefused("IBAN TR33-0006-1005-1978-6457-8413-26", "iban")
        self.assertRefused("IBAN TR33.0006.1005.1978.6457.8413.26", "iban")
        self.assertRefused("IBAN TR33–0006–1005–1978–6457–8413–26", "iban")
        self.assertAllowed("IBAN TR33-0006-1005-1978-6457-8413-27")  # mod 97 fails

    def test_labelled_password_without_digit(self):
        self.assertRefused("Parolam KediTopu", "credential")
        self.assertRefused("Wi-Fi parolası kedicik", "credential")
        self.assertRefused("my password kedicik", "credential")
        self.assertAllowed("Parola alanını doldur.")
        self.assertAllowed("Enter the password field.")

    def test_short_unquoted_secret_or_key(self):
        for text in (
            "password hunter2",
            "password - hunter2",
            "pw hunter2",
            "the password for the router is hunter2",
            "key sk-AbC123",
            "Use sk-AbC123 here.",
            "Use SK-AbC123 here.",
        ):
            self.assertRefused(text, "credential")
        self.assertAllowed("The password field is required.")
        self.assertAllowed("sk-learn paketini kur.")
        self.assertAllowed("Press key F5.")

    def test_lower_case_pem_header_and_bare_base64_key_body(self):
        for text in (
            "-----begin rsa private key-----",
            "——begin rsa private key",
            "-----BEGIN RSA\nPRIVATE KEY-----",
            "BEGIN RSA PRIVATE KEY",
        ):
            self.assertRefused(text, "credential")
        for body in (
            "MIIEvQIBADANBgkqhkiGwBAQEFAASCBKcwggSjAgEAAoIBAQC" + "q" * 30,
            "MHcCAQEEIBQvLmzaXxQrVzPpKfaLqRuWhTbNcGsDkEoJyFiAvHnUoAoGCCqGSM",
        ):
            self.assertRefused(body, "credential")
        self.assertAllowed("-----BEGIN CERTIFICATE-----")
        self.assertAllowed("MIIabcdefABCDEFabcdef")  # too short for a key body

    def test_arabic_indic_digits(self):
        self.assertRefused("Kimlik " + arabic_indic("12345678950"), "national_id")
        self.assertAllowed("Kimlik " + arabic_indic("12345678951"))

    def test_combining_mark_between_digits(self):
        self.assertRefused("Kimlik 1234́5678950", "national_id")
        self.assertRefused("Kimlik 1234ᅟ5678950", "national_id")
        self.assertAllowed("Kimlik 1234́5678951")

    def test_digit_folding_never_hides_an_earlier_refusal(self):
        # A mark or Arabic-Indic digit before a split value must not join it
        # to the neighbour and break a boundary the other rules rely on.
        for text, category in (
            ("9́1⠀2345678950", "national_id"),
            ("9́4⠀111 1111 1111 1111", "payment_card"),
            ("1́şif⠀re: x", "credential"),
            ("١T⠀R33 0006 1005 1978 6457 8413 26", "iban"),
            ("1́da⠀ta/x", "protected_path"),
        ):
            self.assertRefused(text, category)

    def test_dash_runs_stay_linear(self):
        start = time.perf_counter()
        check_fields([("steps-1-text", "-" * 7999)])
        self.assertLess(time.perf_counter() - start, 0.5)


if __name__ == "__main__":
    unittest.main()
