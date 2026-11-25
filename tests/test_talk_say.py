import ast
import io
import random
import sys
import time
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
            "Önce şunu çalıştır:\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n"
            "Dosya C:\\dev\\JARVIS\\jarvis\\core.py ve jarvis/talk_say.py içinde.\n"
            "Bkz https://example.com/a?b=1 ve www.example.org.\n"
            "Commit a910ffb7c261d43706f7e5756f49d009b1124482 ile 7fcb077 geldi.\n"
            "```python\nprint('gizli')\n```\n"
            "Sonra yine:\n```\nx = 1\n```"
        )
        r = say(text)
        for kind, cue in CUES.items():
            self.assertEqual(r.spoken.count(cue), 1, (kind, r.spoken[:80]))
        self.assertEqual(r.text_only, ("code", "table", "path", "url", "hash", talk_say.REST))
        for leak in ("print", "gizli", "x = 1", "Sonra yine", "core.py", "talk_say", "example", "a910ffb",
                     "7fcb077", "| a"):
            self.assertNotIn(leak, r.spoken)
        self.assertIn("Önce şunu çalıştır", r.spoken)
        self.assertTrue(r.rest_in_text)  # the first fence cuts the rest of the answer

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
        self.assertEqual(r.text_only, ("code", talk_say.REST))
        self.assertTrue(r.rest_in_text)

    def test_fence_cuts_the_rest_whatever_closes_it(self):
        r = say("~~~\nkod\n```\nhâlâ kod\n~~~\nsonra")
        self.assertEqual((r.spoken, r.text_only, r.rest_in_text), (CUES["code"], ("code", talk_say.REST), True))

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


class ReviewFixTests(unittest.TestCase):
    """TALKSAY1FIX: review findings F1-F5."""

    def test_pipe_less_table_body_not_spoken(self):
        for table in ("a | b\n--- | ---\n1 | gizli1\n2 | gizli2\n",
                      "a | b\n:---|---:\ngizli1 |\n| gizli2\n",
                      "a | b |\n---|---|\n1 \\| gizli1 | x\n",
                      "a|b\n-|:-\n1|gizli1\n"):
            r = say("Önce.\n" + table + "Son.")
            self.assertEqual(r.spoken, "Önce.\n" + CUES["table"] + "\nSon.", table)
            self.assertEqual(r.text_only, ("table",))

    def test_table_ends_at_blank_or_pipe_less_line(self):
        r = say("a | b\n--- | ---\n1 | 2\n\nx | y düz yazı")
        self.assertEqual(r.spoken, CUES["table"] + "\nx y düz yazı")

    def test_long_line_inputs_stay_fast(self):
        n = MAX_INPUT_CHARS - 10
        hostile = [" " * n + "x", " #" * (n // 2) + "x", "a." * (n // 2), "a-." * (n // 3), "a-" * (n // 2),
                   "a/" * (n // 2), "a.py." * (n // 5), "a" + "_" * n + "a", "\t" * n + "x", "|---" * (n // 4) + "x"]
        for text in hostile:
            start = time.perf_counter()
            self.assertIsInstance(prepare_speech({"text": text}), (Speech, Refusal))
            self.assertLess(time.perf_counter() - start, 1.0, text[:8])  # was 1.5-5 s when quadratic

    def test_kinds_inside_html_tag_reported(self):
        cases = {"x <a href=https://e.com> y": ("x " + CUES["url"] + " y", ("url",)),
                 "<img src=C:\\x\\y.png> z": (CUES["path"] + " z", ("path",)),
                 "<b>etiket</b>": ("etiket", ())}
        for text, (spoken, kinds) in cases.items():
            r = say(text)
            self.assertEqual((r.spoken, r.text_only), (spoken, kinds), text)

    def test_autolink_brackets_dropped(self):
        self.assertEqual(say("<https://x.y/z> bak").spoken, CUES["url"] + " bak")
        self.assertEqual(say("<www.x.org> bak").spoken, CUES["url"] + " bak")
        self.assertEqual(say("< ./a/b.py > bak").spoken, CUES["path"] + " bak")

    def test_indented_code_not_spoken(self):
        for text in ("Metin.\n\n    rm -rf gizli\n\n    gizli2\nSon.", "    gizli\nSon.", "a\n\n\tgizli\nSon."):
            r = say(text)
            self.assertNotIn("gizli", r.spoken, text)
            self.assertEqual(r.spoken.count(CUES["code"]), 1, text)
            self.assertTrue(r.spoken.endswith("Son."), text)
        self.assertEqual(say("```\nkod\n```\n    gizli\nSon.").spoken, CUES["code"])  # fence cuts the rest

    def test_indented_line_after_paragraph_is_text(self):
        self.assertEqual(say("Metin\n    devamı").spoken, "Metin\ndevamı")


class ReviewFix2Tests(unittest.TestCase):
    """TALKSAY1FIX2/3: CDX-TALKSAY1FIX F1, F2, F4-F8; the first fence-ish line cuts the rest (fail closed)."""

    CONTAINER_VARIANTS = (  # (input, spoken before the cue); from build/reports/TALKSAY1FIX2.md
        ("1. Calistir:\n    ```bash\n    rm -rf gizli\n    ```\nSon.", "1. Calistir:\n"),
        ("- Step:\n    ```\n    rm -rf gizli\n    ```\nSon.", "Step:\n"),
        ("> ```\n> rm -rf gizli\n> ```\nSon.", ""),
        ("> - ```sh\n>   gizli\n>   ```\nSon.", ""),
        ("1) ~~~\n   gizli\n   ~~~\nSon.", ""),
        ("* Adim\n\t```\n\tgizli\n\t```\nSon.", "Adim\n"),
        ("Metin\n        ```\n        gizli\n````\nSon.", "Metin\n"),
        ("- a\n  ```\n  ``` gizli\n  gizli2\n  ```\nSon.", "a\n"),
        ("> > ```\n> > gizli\n> > ```\nSon.", ""),
    )

    def assert_cut(self, text, before):
        r = say(text)
        self.assertEqual((r.spoken, r.text_only, r.rest_in_text),
                         (before + CUES["code"], ("code", talk_say.REST), True), text)

    def test_fence_inside_container_cuts_the_rest(self):
        for text, before in self.CONTAINER_VARIANTS:
            self.assert_cut(text, before)

    def test_fence_like_line_inside_fence_never_reopens_speech(self):
        for text in (  # H1 probes: a nested closer used to close the block early
            "```markdown\n> ```\n> rm -rf GIZLI\n> ```\n```\nSon.",
            "```\n- ```\nGIZLI\n```\nSon.",
            "```markdown\n- Kur:\n    ```bash\n    pip install x\n    ```\n- GIZLI calistir\n```\nSon.",
            "```python\nDOC = \'\'\'\n    ```\n\'\'\'\nos.system('GIZLI')\n```\nSon.",
            "• ```bash\n  rm GIZLI\n  ```\nSon.",
        ):
            self.assert_cut(text, "")

    def test_any_prefix_mix_cuts_and_mid_line_fence_does_not(self):
        for text in ("Basla.\n- ```\n  gizli\n\nSon gizli2", "Basla.\n \t> \t- 2) + ~~~\ngizli",
                     "Basla.\n**```\ngizli", "Basla.\n-```gizli", "Basla.\n>>>~~~~ gizli\nSon.",
                     "Basla.\n- [ ] ```\ngizli", "Basla.\n> - [x] ~~~\ngizli"):
            self.assert_cut(text, "Basla.\n")
        r = say("a ```b``` c\nSon.")  # not at line start: an inline code span
        self.assertEqual((r.spoken, r.rest_in_text), ("a " + CUES["code"] + " c\nSon.", False))

    def test_code_span_closes_only_on_equal_run(self):
        for text in ("``a`secret`b`` bak", "`` ` secret ` `` bak", "x ``` `secret` ``` bak"):
            r = say(text)
            self.assertNotIn("secret", r.spoken, text)
            self.assertTrue(r.spoken.endswith(CUES["code"] + " bak"), text)
        self.assertEqual(say("a ` b `` c").spoken, "a b c")  # unmatched runs are literal marks

    def test_backtick_runs_stay_linear(self):
        n = MAX_INPUT_CHARS - 10
        for text in ("x " + "`" * n, "x" + " `" * (n // 2), "``x`" * (n // 4), "> " * (n // 2) + "x",
                     "- " * (n // 2) + "x", "1. " * (n // 3) + "x", "> " * (n // 2) + "```"):
            start = time.perf_counter()
            self.assertIsInstance(prepare_speech({"text": text}), (Speech, Refusal))
            self.assertLess(time.perf_counter() - start, 1.0, text[:8])  # "x "+"`"*n was 1.18 s

    def test_url_keeps_balanced_parentheses(self):
        r = say("Bak https://en.wikipedia.org/wiki/Function_(mathematics)#History simdi")
        self.assertEqual(r.spoken, "Bak " + CUES["url"] + " simdi")
        self.assertEqual(say("(bkz. https://x.org/a) son").spoken, "(bkz. " + CUES["url"] + " ) son")
        self.assertEqual(say("[wiki](https://x.org/F_(m)) son").spoken, "wiki " + CUES["url"] + " son")

    def test_setext_underline_dropped(self):
        self.assertEqual(say("Baslik\n===\nMetin").spoken, "Baslik\nMetin")

    def test_dotfile_relative_path_not_spoken(self):
        for text in ("Dosya secrets/.env", "Dosya a/b/.gitignore"):
            self.assertEqual((say(text).spoken, say(text).text_only), ("Dosya " + CUES["path"], ("path",)), text)

    def test_image_target_reported_as_url(self):
        r = prepare_speech({"text": "![](https://example.com/logo.png)"})
        self.assertIsInstance(r, Speech)
        self.assertEqual((r.spoken, r.text_only), (CUES["url"], ("url",)))
        r = say("Logo ![](https://x/l.png) burada")
        self.assertEqual((r.spoken, r.text_only), ("Logo " + CUES["url"] + " burada", ("url",)))

    def test_fuzz_nothing_after_first_fence_ish_line_spoken(self):
        rng = random.Random(20261125)
        marks = (" ", "  ", "\t", ">", "> ", "- ", "* ", "+ ", "• ", "1. ", "2) ")
        words = (  # no backticks: the head's only cue is the fence's
            "Metin", "bir", "_kod_", "**kalin**", "| a |", "---", "x", "1.", "> alinti", "")
        for case in range(500):
            head = [rng.choice(("", "- ", "> ", "1. ")) + " ".join(rng.choices(words, k=3))
                    for _ in range(rng.randint(0, 3))]
            if case % 5 == 0:  # the 9 container variants, every secret line tagged
                variant, before = self.CONTAINER_VARIANTS[case // 5 % 9]
                head += [before.rstrip("\n")] if before else []
                fence, *tail = variant.split("\n")[1 if before else 0:]
            else:
                fence = "".join(rng.choices(marks, k=rng.randint(0, 4))) + rng.choice("`~") * rng.randint(3, 5)
                fence += rng.choice(("", "bash", " py"))
                tail = [rng.choice(("", "    ", "> ", "- ")) + rng.choice(("", "```", "~~~", "rm ", "Son."))
                        for _ in range(rng.randint(0, 5))]
            tags = [f"zq{case}t{k}" for k in range(len(tail) + 1)]
            text = "\n".join(head + [fence + " " + tags[0]] + [t + " " + g for t, g in zip(tail, tags[1:])])
            start = time.perf_counter()
            r = prepare_speech({"text": text})
            self.assertLess(time.perf_counter() - start, 0.05, text)
            self.assertIsInstance(r, Speech, text)
            self.assertTrue(r.rest_in_text, text)
            self.assertEqual(r.spoken.count(CUES["code"]), 1, text)
            self.assertTrue(r.spoken.endswith(CUES["code"]), text)
            self.assertNotIn("zq", r.spoken, text)


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
