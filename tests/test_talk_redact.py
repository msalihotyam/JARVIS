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
    "\u015fifre nedir",
    "Passw\u00f6rter sind sicher",
)

# Turkish letters in both cases (incl. dotless i, dotted I) and emoji not right after a
# secret word must pass the mixed-script and Unicode-separator markers.
TURKISH_EMOJI_CLEAN = (
    "Привет мир, время 15:30",
    "\u00c7ok g\u00fczel bir g\u00fcn, \u015fehirde \u0131\u015f\u0131k var \U0001f600",
    "\u0130stanbul'da \u00f6\u011fle yeme\u011fi, \u00c7AR\u015eAMBA \u011e\u00d6\u015e\u00dc",
    "Parolan\u0131z\u0131 kimseyle payla\u015fmay\u0131n \U0001f44d",
    "\u015eifrenizi g\u00fcncellediniz mi? \U0001f512 Evet.",
    "Token s\u00fcresi doldu, yeniden giri\u015f yap\u0131n \u2705",
    "The password policy changed \U0001f389 \u2014 see the wiki",
    "Toplant\u0131 \u00e7ar\u015famba \u2615\ufe0f saat 10'da",
    "\u0130\u015f \u0131\u015f\u0131k \u00e7\u00f6p \u015f\u00fc\u011f\u00fc \u00d6\u00dc\u00c7\u015e\u011e\u0130 \U0001f44b\U0001f3fd",
)


class AcceptanceTests(unittest.TestCase):
    def test_clean_text_is_same_object_not_flagged(self):
        for text in CLEAN:
            with self.subTest(text=text):
                result = redact(text)
                self.assertIs(result.text, text)
                self.assertEqual((result.flagged, result.kinds), (False, ()))

    def test_turkish_prose_and_emoji_stay_clean(self):
        for text in TURKISH_EMOJI_CLEAN:
            with self.subTest(text=ascii(text)):
                result = redact(text)
                self.assertIs(result.text, text)
                self.assertIs(result.flagged, False)

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
        for text in ("token budget is low", "reset your password today", "secret plans\nnone",
                     "the token", "password\tpolicy"):
            with self.subTest(text=text):
                self.assertFalse(redact(text).flagged)

    def test_complement_rule_over_holds_by_design(self):
        # TALKREDACT2FIX4: after a secret word only "spaces/tabs + ASCII letter" or the
        # end is clean, so these (clean before FIX4) are now held; over-holding is accepted.
        for text in ("Token “budget” is a misleading name.",
                     "Şifre — yeni kural, token–tabanlı giriş",
                     "token.", "şifre.", "the cookie, she said"):
            with self.subTest(text=ascii(text)):
                result = redact(text)
                self.assertEqual(result.text, FIXED_LINE)
                self.assertIn("unicode_separator", result.kinds)

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

    def test_case_variants_of_look_alikes_are_flagged(self):
        # CDX-TALKREDACT2FIX F1: only one case of each look-alike was listed.
        for text in ("PASSԜORD=hunter2", "ԀՕ ТОКЕN: x",
                     "passԝord=hunter2", "հօ cօօkie: x",
                     "PASSWՕRD=x", "βearer abcdefghijklmnop1234",
                     "χ seсret=hunter2", "tοkεn=hunter2",
                     "paςςword=hunter2"):
            with self.subTest(text=ascii(text)):
                self.assertEqual(redact(text).text, FIXED_LINE)

    def test_marks_and_format_chars_of_any_script_are_dropped(self):
        # CDX-TALKREDACT2FIX F2: Mn/Mc/Me/Cf outside the old hand-picked blocks.
        for mark in ("ְ", "֑", "َ", "ٰ", "ँ", "ा", "ั",
                     "⃝", "؀", "۝", "\U000110bd", "᪰", "〪",
                     "\x00", "\x1b", "\x7f", "\x9f"):
            with self.subTest(mark=ascii(mark)):
                self.assertEqual(redact(f"pass{mark}word=hunter2").text, FIXED_LINE)

    def test_long_split_telegram_ids_are_flagged(self):
        # CDX-TALKREDACT2FIX F3: split ids longer than 16 digits.
        token = " ".join("A" * 35)
        for digits in ("1" * 17, "1" * 30, "12 34567890123456789", "1" * 16):
            with self.subTest(digits=len(digits)):
                self.assertIn("telegram_bot_token", redact(digits + ":" + token).kinds)

    def test_cdx_fix2_probes_are_flagged(self):
        # CDX-TALKREDACT2FIX2 F1 (palochka) and F2 (U+2254, U+2E2C): the exact probes.
        cases = (("credentiaӏ=x", "mixed_script"), ("CREDENTIAӀ=x", "mixed_script"),
                 ("password≔hunter2", "unicode_separator"),
                 ("pwd≔x", "unicode_separator"), ("token≔x", "unicode_separator"),
                 ("password⸬x", "unicode_separator"))
        for text, kind in cases:
            with self.subTest(text=ascii(text)):
                result = redact(text)
                self.assertEqual(result.text, FIXED_LINE)
                self.assertIn(kind, result.kinds)

    def test_mixed_script_words_are_flagged(self):
        # Class rule, not a map entry: Latin plus another script in one word holds.
        for text in ("creΔential=x", "tokeռ=x", "Ꮪecret=x", "ꓑassword=x",
                     "Ꮪecret plans", "passσword", "aж"):
            with self.subTest(text=ascii(text)):
                result = redact(text)
                self.assertEqual(result.text, FIXED_LINE)
                self.assertIn("mixed_script", result.kinds)

    def test_single_script_lookalike_word_before_separator_is_flagged(self):
        # Whole word in Lisu/Cherokee look-alikes: one script, so not mixed_script.
        lisu = "ꓑꓮꓢꓢꓪꓳꓣꓓ"
        for text in (lisu + "=hunter2", lisu + " = hunter2", lisu + " %3Dx",
                     lisu + "≔x", "ᏚᎬᏟᎡᎬᎢ: x"):
            with self.subTest(text=ascii(text)):
                result = redact(text)
                self.assertEqual(result.text, FIXED_LINE)
                self.assertIn("unicode_separator", result.kinds)

    def test_unicode_separator_after_secret_word_is_flagged(self):
        # Any non-ASCII char that is not a Latin letter right after a secret word,
        # past its identifier tail, whitespace and ASCII quotes/punctuation.
        for text in ("password≕x", "token⦂x", "secret܃x", "pwd⹀x",
                     "password \U0001f511 hunter2", "password ≔ x", "DB_PASSWORD_1≔x",
                     "passwords≔x", '"password"≔"x"', "token budget; password≔x",
                     "password ꓽ x", "password ः hunter2", "passwordःhunter2",
                     "password ⃝ x", "password ̈ x", "password؀x",
                     "şifre≔x", "PASSWORD ≔ x"):
            with self.subTest(text=ascii(text)):
                result = redact(text)
                self.assertEqual(result.text, FIXED_LINE)
                self.assertIn("unicode_separator", result.kinds)

    def test_fix3_f1_latin_tail_letter_then_separator_is_flagged(self):
        # CLREV-TALKREDACT2FIX3 F1: a non-ASCII Latin letter in the tail hid the U+2254.
        for text in ("passwordæ≔hunter2", "passwordł≔hunter2",
                     "tokenø≔x", "secretə≔x"):
            with self.subTest(text=ascii(text)):
                self.assertEqual(redact(text).text, FIXED_LINE)

    def test_review_probes_of_the_series_are_flagged(self):
        # Every CLREV/CDX probe of TALKREDACT2..FIX3 not pinned above, plus the
        # complement rule's own class: any follower but "spaces + ASCII letter" holds.
        probes = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",  # CLREV-TALKREDACT2
            "password＝x", "password﹕x", "password：x",
            "passwordˑx", "passwordꓽx", "passwordःx", "password᛬x",
            "password→x", "secret܃x", "token፥x",
            "соокіе≔x", "password ≔x",
            "password　≔x", "“password”≔x", "token\U0001f511x",
            "password»x", "password«x", "password›x", "password„x",
            "passwordǃx", "password٠x", "password\nx", "passwørd≔x",
            "password؀x", "password 1", "password 'x'", "password (x)",
            "password is≔x", "token ist꞉ x",
        )
        for text in probes:
            with self.subTest(text=ascii(text)):
                self.assertEqual(redact(text).text, FIXED_LINE)

    def test_shadowed_and_far_words_are_flagged(self):
        # F2 (FIX4): "pwd" shadowed "private key"/"api key", "secret" shadowed "sifre",
        # and a 16-char skip-ahead jumped past a word stretched by marks.
        probes = (
            "pɍɨvate key:hunter2", "p\xf8\xf8vate key:hunter2",
            "pɍɨvate key\xb7hunter2", "p\xf8" + "̶" * 20 + "\xf8i key\xb7hunter2",
            "s\xf8\xf8re\xf8 hunter2", "api  key\xb7hunter2", "api\tkey\xb7hunter2",
        )
        for text in probes:
            with self.subTest(text=ascii(text)):
                self.assertEqual(redact(text).text, FIXED_LINE)

    def test_ten_megabytes_stays_linear(self):
        for text in ("a b\n" * 2_500_000, "a b\n" * 2_499_999 + "password=x"):
            with self.subTest(flagged=text.endswith("x")):
                start = time.perf_counter()
                result = redact(text)
                self.assertLess(time.perf_counter() - start, 30.0)
                self.assertIsInstance(result, RedactResult)


def _best(fn, text, runs=3):
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn(text)
        best = min(best, time.perf_counter() - start)
    return best


def _baseline(n):
    # Same-length plain ASCII on the same machine, so the budget follows the runner's speed.
    return _best(redact, ("a b " * (n // 4 + 1))[:n])


def _slow_bounds(seconds, baseline):
    broken = []
    if seconds >= 10 * baseline + 0.05:
        broken.append("relative")
    if seconds >= 2.0:
        broken.append("absolute")
    return broken


def _quadratic(text):
    # Deliberately O(n^2): rescans every 8th prefix; the relative bound must catch it.
    return sum(text.count("a b", 0, i) for i in range(0, len(text), 8))


class PerformanceTests(unittest.TestCase):
    def test_pathological_inputs_are_fast(self):
        n = 200_000
        baseline = _baseline(n)
        units = ("token ", "1 ", "1", "12345678:" + " " * 7, "a", "eyJa.", "-", "\n",
                 "sk-", "bearer ", "\\", '"', "%3a", "password ", "1  ", "ghp_", "AKIA",
                 "\u200b", "\uff10", "{\"a\":", "password\n", "-----", "1:",
                 "eyJa", "eyj_", "xeyJa", "EYJA", "ж", "token≔", "tokentoken",
                 "tokeń", "≔ ", "password_ç", "á")
        units += ("pwd", "pwd a ", "p" + "̶" * 50)  # FIX4 per-word scan
        for unit in units:
            text = (unit * (n // len(unit) + 1))[:n]
            with self.subTest(unit=ascii(unit)):
                best = _best(redact, text)
                self.assertEqual(_slow_bounds(best, baseline), [], f"{best:.3f}s vs {baseline:.3f}s")
        for tail in ("12345678:" + " " * n, "eyJaa." + "eyJa" * (n // 4)):
            with self.subTest(tail=ascii(tail[:12])):
                best = _best(redact, tail)
                self.assertEqual(_slow_bounds(best, baseline), [], f"{best:.3f}s vs {baseline:.3f}s")

    def test_relative_bound_catches_a_quadratic_scan(self):
        n = 200_000
        baseline = _baseline(n)
        seconds = _best(_quadratic, ("a b " * (n // 4 + 1))[:n], runs=1)
        self.assertIn("relative", _slow_bounds(seconds, baseline), f"{seconds:.3f}s vs {baseline:.3f}s")

    def test_distinct_non_latin_inputs_are_fast(self):
        # CLREV-TALKREDACT2FIX4 F1: per-char unicodedata lookups made these 0.3-0.6 s.
        n = 200_000
        baseline = _baseline(n)

        def cjk(i):
            return chr(0x4E00 + i % 20992)

        texts = {
            "cjk_spaced": "".join(cjk(i) + " " for i in range(n // 2)),
            "cjk_unspaced": "".join(cjk(i) for i in range(n)),
            "cjk_500_repeated": "".join(cjk(i) for i in range(500)) * (n // 500),
            "cyrillic": "".join(chr(0x400 + i % 256) + ("" if i % 6 else " ")
                                for i in range(n))[:n],
            "mixed": "".join(("a", chr(0x430 + i % 32), cjk(i), "ж1 ", chr(0x3B1 + i % 24))[i % 5]
                             for i in range(n))[:n],
        }
        for name, text in texts.items():
            with self.subTest(input=name):
                best = _best(redact, text)
                self.assertEqual(_slow_bounds(best, baseline), [], f"{best:.3f}s vs {baseline:.3f}s")
                self.assertIsInstance(redact(text), RedactResult)
        self.assertEqual(redact(texts["cjk_spaced"]).text, texts["cjk_spaced"])

    def test_foreign_word_before_unicode_separator_still_flagged(self):
        # The _SEP_WORD prefilter must keep every word the exact check would flag.
        for text in ("密码 ≔ x", "密码：x", "пароль » = x", "ᏢᎪᏚᏚ: x", "a 密码→x",
                     "ꓑꓮꓢꓢ %3A x"):
            with self.subTest(text=ascii(text)):
                self.assertEqual(redact(text).text, FIXED_LINE)

    def test_jwt_inside_a_token_run_is_still_flagged(self):
        # The anchored start must not lose an "eyJ" that follows other token chars.
        for text in ("xeyJhbGciOi.eyJzdWIiOi.sig", "ab_-eyeyJhbGciOi.eyJzdWIiOi.sig",
                     "EYJHBGCIOI.EYJZDWIIOI.SIG", "eyjeyJhbGciOi.eyJzdWIiOi.sig"):
            with self.subTest(text=text):
                result = redact(text)
                self.assertIs(result.flagged, True)
                self.assertIn("jwt", result.kinds)
        for text in ("xeyJa.eyJzdWIiOi", "eyJ.eyJzdWIiOi.sig", "eyJhbGciOi.eyJ.sig"):
            with self.subTest(text=text):
                self.assertNotIn("jwt", redact(text).kinds)


class StaticTests(unittest.TestCase):
    def test_imports_are_pure(self):
        tree = ast.parse((ROOT / "jarvis" / "talk_redact.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
        self.assertLessEqual(names, {"dataclasses", "functools", "re", "unicodedata", "jarvis.contracts"})

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
