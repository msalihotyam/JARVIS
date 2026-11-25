import ast
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

from jarvis import talk_label
from jarvis.talk_label import NAME_CAP, Refusal, pc_title, telegram_line

HOSTILE_CHARS = ["\n", "\r", "\t", "\x00", " ", " ", "\u0085",
                 "‮", "‭", "⁦", "⁧", "⁨", "⁩",
                 "​", "‌", "‍", "‎", "‏", "﻿",
                 "؜", "\ud800"]
BAD_TYPES = [None, 1, 1.5, b"x", ["x"], ("x",), {"x": 1}, {"x"}, object()]


def _one_line(test, text):
    test.assertIsInstance(text, str)
    test.assertEqual(text, " ".join(text.split()))
    for ch in HOSTILE_CHARS:
        test.assertNotIn(ch, text)


class DeepMapping(dict):
    def __contains__(self, key):
        raise RecursionError("deep")


class TalkLabelTests(unittest.TestCase):
    # Acceptance 1
    def test_pc_title_project_and_topic(self):
        self.assertEqual(pc_title({"project": "JARVIS", "topic": "Voice"}),
                         "JARVIS · Voice")

    def test_pc_title_topic_alone_without_project(self):
        self.assertEqual(pc_title({"topic": "Voice"}), "Voice")
        self.assertEqual(pc_title({"project": None, "topic": "Voice"}), "Voice")

    def test_extra_keys_ignored(self):
        self.assertEqual(pc_title({"topic": "Voice", "id": 7}), "Voice")

    # Acceptance 2
    def test_telegram_line_on_change(self):
        self.assertEqual(telegram_line({"topic": "A"}, {"project": "P", "topic": "B"}),
                         "→ P · B")
        self.assertEqual(telegram_line(None, {"topic": "B"}), "→ B")
        self.assertEqual(telegram_line({"project": "P", "topic": "B"}, {"topic": "B"}),
                         "→ B")

    def test_telegram_line_nothing_when_unchanged(self):
        focus = {"project": "P", "topic": "B"}
        self.assertIsNone(telegram_line(focus, dict(focus)))

    # Acceptance 3
    def test_names_capped_with_ellipsis(self):
        title = pc_title({"project": "p" * 200, "topic": "t" * 200})
        project, topic = title.split(" · ")
        self.assertEqual(project, "p" * (NAME_CAP - 1) + "…")
        self.assertEqual(topic, "t" * (NAME_CAP - 1) + "…")
        self.assertEqual(pc_title({"topic": "t" * NAME_CAP}), "t" * NAME_CAP)
        _one_line(self, telegram_line(None, {"topic": "x\n" * 500}))

    def test_cap_compares_full_names(self):
        old = {"topic": "a" * 60 + "1"}
        self.assertIsNotNone(telegram_line(old, {"topic": "a" * 60 + "2"}))

    # Negative 1
    def test_control_bidi_zero_width_cleaned(self):
        for ch in HOSTILE_CHARS:
            with self.subTest(ch=ascii(ch)):
                focus = {"project": f"Pro{ch}ject", "topic": f"to{ch}pic{ch}"}
                for text in (pc_title(focus), telegram_line(None, focus)):
                    _one_line(self, text)
        self.assertEqual(pc_title({"topic": "a\nb\tc\x00d"}), "a b c d")
        self.assertEqual(pc_title({"topic": "abc‮def"}), "abcdef")
        self.assertEqual(pc_title({"topic": "a​b"}), "ab")

    # Negative 2
    def test_command_url_mention_inert(self):
        for name in ("/start", "/ban@bot", "https://evil.example/x", "evil.example",
                     "@admin", "mailto:a@b.c", "\\\\host\\share"):
            with self.subTest(name=name):
                for text in (pc_title({"topic": name}),
                             telegram_line(None, {"topic": name})[2:]):
                    self.assertFalse(text.startswith("/"))
                    for bad in ("/", "@", "://", ".", "\\", ":"):
                        self.assertNotIn(bad, text)

    # Negative 3
    def test_empty_or_whitespace_refused(self):
        # Default_Ignorable blanks outside Cf: CGJ, variation selectors, Hangul fillers, ...
        ignorable = ("͏", "︀", "️", "ㅤ", "ﾠ", "ᅟ", "ᅠ",
                     "឴", "᠋", "\U000e0100", "͏ ️ㅤ")
        for name in ("", " ", "\t\n", "​‮", "　") + ignorable:
            with self.subTest(name=ascii(name)):
                self.assertEqual(pc_title({"topic": name}), Refusal("name_empty"))
                self.assertEqual(pc_title({"project": name, "topic": "x"}),
                                 Refusal("name_empty"))
                self.assertEqual(telegram_line(None, {"topic": name}),
                                 Refusal("name_empty"))

    # Negative 4
    def test_case_and_unicode_form_not_a_change(self):
        pairs = [("Proje", "PROJE"), ("café", "café"),
                 ("İstanbul", "istanbul"), ("ISPARTA", "ısparta"),
                 ("Straße", "STRASSE"), ("Ａbc", "abc"),
                 # lower()/casefold() of U+0130 is the decomposed "i" + U+0307
                 ("İstanbul", "İstanbul".lower()), ("ISTANBUL", "i̇stanbul")]
        for a, b in pairs:
            with self.subTest(a=ascii(a), b=ascii(b)):
                self.assertIsNone(telegram_line({"project": a, "topic": a},
                                                {"project": b, "topic": b}))

    def test_project_appearing_is_a_change(self):
        self.assertIsNotNone(telegram_line({"topic": "T"}, {"project": "T", "topic": "T"}))

    # Negative 5
    def test_non_str_refused(self):
        for bad in BAD_TYPES[1:]:
            with self.subTest(bad=type(bad).__name__):
                self.assertEqual(pc_title({"topic": bad}), Refusal("name_not_str"))
                self.assertEqual(pc_title({"project": bad, "topic": "x"}),
                                 Refusal("name_not_str"))
                self.assertEqual(telegram_line({"topic": bad}, {"topic": "x"}),
                                 Refusal("name_not_str"))
        self.assertEqual(pc_title({"topic": None}), Refusal("name_not_str"))

    def test_wrong_focus_type_or_missing_field_refused(self):
        for bad in BAD_TYPES:
            if not isinstance(bad, dict):
                with self.subTest(bad=type(bad).__name__):
                    self.assertEqual(pc_title(bad), Refusal("focus_not_mapping"))
                    self.assertEqual(telegram_line(None, bad), Refusal("focus_not_mapping"))
                    if bad is not None:  # old=None means no previous focus
                        self.assertEqual(telegram_line(bad, {"topic": "x"}),
                                         Refusal("focus_not_mapping"))
        self.assertEqual(pc_title({"project": "P"}), Refusal("topic_missing"))

    def test_deep_and_hostile_input_never_raises(self):
        deep = {"topic": "x"}
        for _ in range(10000):
            deep = {"topic": deep}
        self.assertEqual(pc_title(deep), Refusal("name_not_str"))
        self.assertEqual(pc_title(DeepMapping(topic="x")), Refusal("input_too_deep"))
        with mock.patch.object(talk_label, "_clean", side_effect=ValueError("boom")):
            self.assertEqual(pc_title({"topic": "x"}), Refusal("input_invalid"))

    def test_refusals_ascii_under_cp1252(self):
        raw = io.BytesIO()
        wrapped = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        reasons = ["name_not_str", "name_empty", "focus_not_mapping", "topic_missing",
                   "input_too_deep", "input_invalid"]
        with mock.patch.object(sys, "stdout", wrapped):
            for reason in reasons:
                print(Refusal(reason), Refusal(reason).reason)
            sys.stdout.flush()
        self.assertTrue(all(r.isascii() and r.isprintable() for r in reasons))
        self.assertIn(b"name_empty", raw.getvalue())

    def test_deterministic(self):
        focus = {"project": "P‮", "topic": "/x@y"}
        self.assertEqual({pc_title(focus) for _ in range(5)}, {pc_title(dict(focus))})

    def test_no_io_or_network_imports(self):
        banned = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess",
                  "shutil", "tempfile", "sqlite3", "requests", "ssl", "asyncio"}
        tree = ast.parse(Path(talk_label.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in (
                    "open", "__import__", "eval", "exec"):
                self.fail(f"forbidden call {node.func.id}")
        self.assertFalse(names & banned, names & banned)


if __name__ == "__main__":
    unittest.main()
