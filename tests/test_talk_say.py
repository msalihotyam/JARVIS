import ast
import io
import random
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path

from jarvis import talk_say
from jarvis.talk_say import CUES, MAX_INPUT_CHARS, SPOKEN_CAP, Refusal, Speech, prepare_speech


def say(text):
    result = prepare_speech({"text": text})
    assert isinstance(result, Speech), result
    return result


class SpokenTextTests(unittest.TestCase):
    """Acceptance 1, 2, 4, 5."""

    def test_each_kind_replaced_by_its_cue_once(self):
        text = (
            "Önce şunu çalıştır:\n```python\nprint('gizli')\n```\n"
            "Sonra yine:\n```\nx = 1\n```\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n"
            "Dosya C:\\dev\\JARVIS\\jarvis\\core.py ve jarvis/talk_say.py içinde.\n"
            "Bkz https://example.com/a?b=1 ve www.example.org.\n"
            "Commit a910ffb7c261d43706f7e5756f49d009b1124482 ile 7fcb077 geldi."
        )
        r = say(text)
        for kind, cue in CUES.items():
            self.assertEqual(r.spoken.count(cue), 1, (kind, r.spoken[:80]))
        self.assertEqual(r.text_only, ("code", "table", "path", "url", "hash"))
        for leak in ("print", "gizli", "x = 1", "core.py", "talk_say", "example", "a910ffb", "7fcb077", "| a"):
            self.assertNotIn(leak, r.spoken)
        self.assertIn("Önce şunu çalıştır", r.spoken)
        self.assertFalse(r.rest_in_text)

    def test_inline_code_and_link_target_not_spoken(self):
        r = say("`git log -1` çalıştır, sonra [belgeye](https://x.test/doc) bak.")
        self.assertNotIn("git log", r.spoken)
        self.assertNotIn("x.test", r.spoken)
        self.assertIn("belgeye", r.spoken)
        self.assertEqual(r.text_only, ("code", "url"))

    def test_markdown_marks_removed(self):
        r = say("# Başlık\n> alıntı **kalın** *eğik* __alt__ ~~sil~~\n- madde\n* iki\n---\n<b>etiket</b> \\*kaçış\\*")
        for mark in ("#", ">", "*", "_", "~", "<b>", "\\"):
            self.assertNotIn(mark, r.spoken)
        self.assertEqual(r.spoken, "Başlık\nalıntı kalın eğik alt sil\nmadde\niki\netiket kaçış")
        self.assertEqual(r.text_only, ())

    def test_turkish_characters_preserved(self):
        text = "Çağrı ışığı söndü; ĞÜŞİÖÇ ğüşıöç İstanbul."
        self.assertEqual(say(text).spoken, text)

    def test_snake_case_word_kept(self):
        self.assertEqual(say("değer my_value oldu").spoken, "değer my_value oldu")

    def test_plain_number_is_not_a_hash(self):
        self.assertEqual(say("Toplam 12345678 adım.").spoken, "Toplam 12345678 adım.")

    def test_deterministic(self):
        text = "Bak: `x` ve https://a.b/c\n| t |\n" * 20
        self.assertEqual(prepare_speech({"text": text}), prepare_speech({"text": text}))


class CapTests(unittest.TestCase):
    """Acceptance 3."""

    def test_short_text_not_flagged(self):
        r = say("Kısa cevap.")
        self.assertFalse(r.rest_in_text)
        self.assertNotIn(talk_say.REST, r.text_only)

    def test_long_text_capped_at_sentence_and_flagged(self):
        r = say("Bu bir cümledir. " * 100)
        self.assertLessEqual(len(r.spoken), SPOKEN_CAP)
        self.assertTrue(r.spoken.endswith("."))
        self.assertTrue(r.rest_in_text)
        self.assertEqual(r.text_only, (talk_say.REST,))

    def test_unbroken_text_hard_cut(self):
        r = say("ş" * 5000)
        self.assertEqual(r.spoken, "ş" * SPOKEN_CAP)
        self.assertTrue(r.rest_in_text)


class NegativeTests(unittest.TestCase):
    """Negative 1-5."""

    def test_unclosed_fence_rest_is_code(self):
        r = say("Açıklama burada.\n```\ngizli_kod()\nDüz yazı gibi görünen satır.\n")
        self.assertEqual(r.spoken, "Açıklama burada.\n" + CUES["code"])
        self.assertEqual(r.text_only, ("code",))

    def test_tilde_fence_closed_only_by_tilde(self):
        r = say("~~~\nkod\n```\nhâlâ kod\n~~~\nsonra")
        self.assertEqual(r.spoken, CUES["code"] + "\nsonra")

    def test_only_code_speaks_only_the_cue(self):
        for text in ("```\nprint(1)\n```", "```py\na\n```\n\n```\nb\n```", "`x`"):
            self.assertEqual(say(text).spoken, CUES["code"], text)

    def test_control_bidi_zero_width_removed(self):
        hostile = "a\u202eb\u2066c\u200bd\u200ce\u200df\ufeffg\x07h\x1bi\u00adj\x00k"
        self.assertEqual(say(hostile).spoken, "abcdefghijk")
        self.assertEqual(say("güven\u202elik\u200b").spoken, "güvenlik")

    def test_empty_and_invisible_refused(self):
        for text in ("", "   \n\t", "\u200b\u202e\x00", "***", "---\n___"):
            r = prepare_speech({"text": text})
            self.assertIsInstance(r, Refusal, repr(text))
            self.assertEqual(r.reason, "empty")

    def test_over_limit_refused_at_limit_accepted(self):
        self.assertIsInstance(prepare_speech({"text": "a" * MAX_INPUT_CHARS}), Speech)
        r = prepare_speech({"text": "a" * (MAX_INPUT_CHARS + 1)})
        self.assertEqual((r.ok, r.reason), (False, "too_long"))

    def test_fuzz_malformed_markdown_never_raises(self):
        rng = random.Random(20261125)
        atoms = ["```", "~~~", "`", "**", "*", "_", "__", "~~", "#", "> ", "- ", "|", "|---|", "[", "]",
                 "(", ")", "![", "](", "\n", " ", "\\", "<b>", "</", "http://", "www.", "C:\\", "/", "./",
                 "a1b2c3d4", "ş", "İ", "ı", "\u202e", "\u200b", "\x00", "\r", "\t", "metin", ".", "x.py"]
        for i in range(500):
            text = "".join(rng.choice(atoms) for _ in range(rng.randint(0, 120)))
            r = prepare_speech({"text": text})
            self.assertIsInstance(r, (Speech, Refusal), i)
            if r.ok:
                self.assertLessEqual(len(r.spoken), SPOKEN_CAP, i)
                self.assertFalse(any(c in r.spoken for c in "\x00\u202e\u200b\r\t"), i)
            else:
                self.assertIn(r.reason, talk_say._REFUSALS, i)


class RefusalTests(unittest.TestCase):
    """Typed refusals: wrong type, missing field, deep nesting, hostile mapping."""

    def test_wrong_type_missing_field_deep_nesting(self):
        deep = {}
        cur = deep
        for _ in range(50000):
            cur["text"] = {}
            cur = cur["text"]
        cases = {
            "wrong_type": [None, "text", b"text", ["text"], {"text": 1}, {"text": b"x"}, deep],
            "missing_field": [{}, {"body": "x"}],
        }
        for reason, inputs in cases.items():
            for value in inputs:
                r = prepare_speech(value)
                self.assertEqual((r.ok, r.reason), (False, reason), type(value).__name__)

    def test_hostile_mapping_never_escapes(self):
        class Boom(Mapping):
            def __init__(self, exc):
                self.exc = exc

            def __getitem__(self, key):
                raise self.exc

            def __contains__(self, key):
                return True

            def __iter__(self):
                return iter(())

            def __len__(self):
                return 1

        self.assertEqual(prepare_speech(Boom(RecursionError())).reason, "too_deep")
        self.assertEqual(prepare_speech(Boom(KeyError("text"))).reason, "malformed")

    def test_refusal_texts_ascii_under_cp1252_strict(self):
        old = sys.stdout
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        try:
            for reason, message in talk_say._REFUSALS.items():
                self.assertTrue(message.isascii() and message.isprintable(), reason)
                print(reason, message)
            sys.stdout.flush()
        finally:
            sys.stdout = old


class PurityTests(unittest.TestCase):
    def test_no_io_or_network_imports(self):
        tree = ast.parse(Path(talk_say.__file__).read_text(encoding="utf-8"))
        banned = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "shutil",
                  "tempfile", "sqlite3", "ssl", "requests", "asyncio", "importlib"}
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("open", "__import__", "eval", "exec"):
                self.fail(f"forbidden call {node.func.id}")
        self.assertFalse(names & banned, names & banned)


if __name__ == "__main__":
    unittest.main()
