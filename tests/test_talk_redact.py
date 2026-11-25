"""TALKREDACT2: whole-message replace when any secret marker is present."""

import ast
import pathlib
import time
import unittest
from unittest import mock

from jarvis import talk_redact
from jarvis.talk_redact import FIXED_LINE, RedactResult, Refusal, redact

ROOT = pathlib.Path(__file__).resolve().parents[1]
TG = "7123456789:AAEfAkE0tOkEnForTestsOnly1234567890ab"

# Every probe that leaked through TALKREDACT1 masking (CDX-/CLREV-TALKREDACT1FIX*),
# with the secret fragment that must never reach Telegram.
LEAK_PROBES = (
    ("CDX1-F1a", r'{\"password\": \"hunter2 moresecret\"}', "hunter2"),
    ("CDX1-F1b", r'{\"token\":\"tok en\"}', "tok en"),
    ("CDX1-F2", "12345678:AAAAAA AAAAAA AAAAAA AAAAAA AAAAAA AAAAA", "AAAAAA"),
    ("CDX1-F3a", '{"password!": "hunter2"}', "hunter2"),
    ("CDX1-F3b", '{"password?": "hunter2"}', "hunter2"),
    ("CDX2-F1a", '{"secret": {"token": "hunter2"}}', "hunter2"),
    ("CDX2-F1b", '{"secrets": {"password": "hunter2 more"}}', "hunter2"),
    ("CDX2-F2", '{"password": "hunter2 moresecret', "hunter2"),
    ("CDX2-F3", "7123456789:" + " ".join("AAEfAkE0tOkEnForTestsOnly1234567890ab"), "A A E f"),
    ("CDX2-F4", "7123456789%3AAAEfAkE0tOkEnForTest sOnly1234567890", "sOnly"),
    ("CDX2-F5", '{"password!!!!!!!!!!!!!!!!!!": "hunter2"}', "hunter2"),
    ("CDX3-F1a", 'PASSWORD="first\nhunter2"', "hunter2"),
    ("CDX3-F1b", "PASSWORD='first\nhunter2'", "hunter2"),
    ("CDX3-F1c", 'export DB_PASSWORD="line1\nhunter2 more"\nUSER=bob', "hunter2"),
    ("CDX3-F2", '{"secret": {"a": "\\\\"}",\n"b": "hunter2"}}', "hunter2"),
    ("CDX4-F1a", 'PASSWORD = ( # :-)\n"hunter2"\n)', "hunter2"),
    ("CDX4-F1b", "token: [ # a]\nhunter2\n]", "hunter2"),
    ("CDX4-F1c", 'password: { # }\n"x": "hunter2"\n}', "hunter2"),
    ("CDX4-F2", "password = \"\"\"a\"'''b'\nhunter2\n\"\"\"", "hunter2"),
    ("CLREV3-F1a", 'SECRET="line1\nline2leak"\nnext=1', "line2leak"),
    ("CLREV3-F1b", 'API_KEY="a b\nleak3 c"', "leak3"),
    ("CLREV3-F1c", "export SECRET='l1\nleak2'\nX=1", "leak2"),
    ("CLREV4-F1a", 'password: {"a": "}\n",\n"b": "hunter2"}', "hunter2"),
    ("CLREV4-F1b", 'password: {\n"a": "x}\ny",\n"b": "hunter2"\n}', "hunter2"),
    # Check items from the same reports.
    ("CHK-list", '["password", "a b"]', "a b"),
    ("CHK-nextline", '{"password"\n: hunter2}', "hunter2"),
    ("CHK-pct3d", "password%3Dhunter2", "hunter2"),
    ("CHK-ratio", "token\u2236hunter2", "hunter2"),
    ("CHK-multi", 'password: "x"\n"y\nhunter2"\nok', "hunter2"),
    ("CHK-cookie", '{"Cookie": "sid=hunter2"}', "hunter2"),
    ("CHK-basic", '{"Authorization": "Basic aHVudGVyMg=="}', "aHVudGVyMg"),
    ("CHK-yaml", "db:\n  password: |\n    hunter2\n", "hunter2"),
    ("CHK-fwcolon", "password\uff1ahunter2", "hunter2"),
)

SHAPES = (
    ("telegram_bot_token", "bot " + TG),
    ("openai_key", "key sk-abcdefghijklmnop1234"),
    ("github_token", "ghp_abcdefghijklmnop1234 and github_pat_abcdefghijklmnopqrstuv"),
    ("jwt", "eyJhbGciOiJI.eyJzdWIiOiIx.c2ln"),
    ("pem", "-----BEGIN OPENSSH PRIVATE KEY-----"),
    ("bearer", "send Bearer abcdefghijklmnop1234"),
    ("aws_key", "id AKIAABCDEFGHIJKLMNOP"),
    ("google_api_key", "AIza" + "B" * 35),
    ("secret_word", "my sifre => hunter2"),
)

CLEAN = (
    "token budget is low",
    "the password policy changed",
    "We met in Asia last year; the cookie jar was empty.",
    "def add(a, b):\n    return a + b\n",
    "for i in range(10):\n    print(i)\n",
    "Merhaba, toplant\u0131 saat 15:30'da. Parola politikas\u0131 de\u011fi\u015fti",
)


class AcceptanceTests(unittest.TestCase):
    def test_clean_text_is_same_object_not_flagged(self):
        for text in CLEAN:
            with self.subTest(text=text):
                result = redact(text)
                self.assertIs(result.text, text)
                self.assertEqual((result.flagged, result.kinds), (False, ()))

    def test_every_old_leak_probe_gives_only_the_fixed_line(self):
        for name, probe, secret in LEAK_PROBES:
            with self.subTest(probe=name):
                result = redact(probe)
                self.assertIsInstance(result, RedactResult)
                self.assertTrue(result.flagged)
                self.assertEqual(result.text, FIXED_LINE)
                self.assertNotIn(secret, result.text)

    def test_each_shape_names_its_kind(self):
        for kind, text in SHAPES:
            with self.subTest(kind=kind):
                result = redact(text)
                self.assertEqual(result.text, FIXED_LINE)
                self.assertIn(kind, result.kinds)

    def test_kinds_are_sorted_and_listed_once(self):
        text = f"{TG}\n{TG.replace(':', '%3A')}\npassword=a\ntoken: b\n{TG}"
        kinds = redact(text).kinds
        self.assertEqual(kinds, tuple(sorted(set(kinds))))
        self.assertIn("telegram_bot_token", kinds)
        self.assertIn("secret_word", kinds)

    def test_contracts_detector_counts_as_marker(self):
        with mock.patch.object(talk_redact, "redact_credentials", return_value=("x", 1)):
            result = redact("plain words")
        self.assertEqual((result.text, result.kinds), (FIXED_LINE, ("credential",)))

    def test_fixed_line_itself_is_not_flagged(self):
        result = redact(FIXED_LINE)
        self.assertFalse(result.flagged)
        self.assertIs(result.text, FIXED_LINE)


class NegativeTests(unittest.TestCase):
    def test_non_str_is_refused(self):
        for value in (None, b"password=x", 12, ["token: x"]):
            with self.subTest(value=value):
                self.assertEqual(redact(value), Refusal("input_not_text"))

    def test_detector_failures_refuse_never_return_text(self):
        cases = ((RecursionError, "input_too_deep"), (ValueError, "redaction_failed"),
                 (MemoryError, "redaction_failed"))
        for error, reason in cases:
            with self.subTest(error=error.__name__):
                with mock.patch.object(talk_redact, "redact_credentials", side_effect=error):
                    result = redact("hello there")
                self.assertEqual(result, Refusal(reason))
                self.assertFalse(hasattr(result, "text"))

    def test_secret_word_without_separator_is_not_flagged(self):
        for text in ("token budget is low", "reset your password today", "secret plans\nnone"):
            with self.subTest(text=text):
                self.assertFalse(redact(text).flagged)

    def test_hidden_or_look_alike_markers_are_flagged(self):
        hidden = (
            "pass\u200bword=hunter2",
            "pass\u202eword=hunter2",
            "to\u2060ken: hunter2",
            "\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44=hunter2",
            "p\u0430ssw\u043erd=hunter2",
            "\u0422\u041e\u041a\u0415N: hunter2",
            "\u015e\u0130FRE: hunter2",
            "p\u0336assword=hunter2",
            "7123456789\u200b:" + TG.split(":")[1],
            "sk\u2010abcdefghijklmnop1234",
        )
        for text in hidden:
            with self.subTest(text=ascii(text)):
                self.assertEqual(redact(text).text, FIXED_LINE)

    def test_ten_megabytes_stays_linear(self):
        for text in ("a b\n" * 2_500_000, "a b\n" * 2_499_999 + "password=x"):
            with self.subTest(flagged=text.endswith("x")):
                start = time.perf_counter()
                result = redact(text)
                self.assertLess(time.perf_counter() - start, 30.0)
                self.assertIsInstance(result, RedactResult)


class PerformanceTests(unittest.TestCase):
    def test_pathological_inputs_are_fast(self):
        n = 200_000
        units = ("token ", "1 ", "1", "12345678:" + " " * 7, "a", "eyJa.", "-", "\n",
                 "sk-", "bearer ", "\\", '"', "%3a", "password ", "1  ", "ghp_", "AKIA",
                 "\u200b", "\uff10", "{\"a\":", "password\n", "-----", "1:")
        for unit in units:
            text = (unit * (n // len(unit) + 1))[:n]
            with self.subTest(unit=ascii(unit)):
                start = time.perf_counter()
                redact(text)
                self.assertLess(time.perf_counter() - start, 0.25)
        tail = "12345678:" + " " * n
        start = time.perf_counter()
        redact(tail)
        self.assertLess(time.perf_counter() - start, 0.25)


class StaticTests(unittest.TestCase):
    def test_imports_are_pure(self):
        tree = ast.parse((ROOT / "jarvis" / "talk_redact.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
        self.assertLessEqual(names, {"dataclasses", "re", "unicodedata", "jarvis.contracts"})

    def test_refusal_reasons_are_ascii(self):
        for reason in ("input_not_text", "input_too_deep", "redaction_failed"):
            self.assertTrue(reason.isascii())
        source = (ROOT / "jarvis" / "talk_redact.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Refusal":
                self.assertTrue(node.args[0].value.isascii())

    def test_running_app_does_not_import_module(self):
        for path in (ROOT / "jarvis").rglob("*.py"):
            if path.name != "talk_redact.py":
                with self.subTest(path=path.name):
                    self.assertNotIn("talk_redact", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
