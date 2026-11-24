"""DORAGATE1: the shared Dora/secret screen. Synthetic deny words only; no DB, no network."""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import dora_screen
from jarvis.dora_screen import screen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lesson_extract as L  # noqa: E402

WORD = "zqxvkmark"  # synthetic, not a real deny word
SYNTH = {"version": 3, "about": "synthetic test list",
         "shapes": [r"\bQQ-\d{4}\b"], "words": [WORD, "^plimb$"]}
# Built at run time so the source holds no literal secret-shaped token.
SK = "sk-" + "ant-" + "api03-" + "A1b2" * 6
GH = "gh" + "p_" + "Z9y8" * 9
GENERIC = "API_" + "KEY=" + "x7" * 20


def write_list(root: Path, data) -> Path:
    path = root / "deny.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


class DoraScreenTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db opened"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = write_list(self.root, SYNTH)
        patcher = mock.patch.object(dora_screen, "DENY_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clean_text_passes(self):
        self.assertEqual(screen("lane N6-3 accepted, tests green"), (True, ""))

    def test_neg_deny_word_refused_in_every_disguise(self):
        for text in (f"see {WORD} now", "Zq.X-v_K m4rk", "ZQXVKMARK!", "zqx​vkmark",
                     "plimb", "a PLIMB b"):
            self.assertEqual(screen(text), (False, "deny"), text)
        self.assertEqual(screen("plimbing"), (True, ""))  # '^plimb$' needs both boundaries

    def test_neg_deny_shape_refused(self):
        self.assertEqual(screen("ref QQ-1234 open"), (False, "deny"))

    def test_neg_secret_shapes_refused(self):
        for text in (f"key {SK}", f"token {GH}", GENERIC, f'"password": "{"p" * 40}"'):
            self.assertEqual(screen(text), (False, "secret"), text)
        self.assertEqual(screen("token=short"), (True, ""))
        self.assertEqual(screen("commit " + "a1" * 20), (True, ""))  # bare 40-hex is no secret

    def test_neg_github_token_refused_with_glued_prefix(self):  # CDX-DORAGATE1FIX3 F1
        body, pat = "A" * 36, "github" + "_pat_" + "B" * 30
        tokens = [f"gh{c}_{body}" for c in "pousr"] + [pat]
        for tok in tokens:
            for pre in ("", " ", "x", "7", "token", "_", "é", "a "):
                self.assertEqual(screen(pre + tok), (False, "secret"), (pre + tok)[:20])
        self.assertEqual(screen("x" + GH), (False, "secret"))
        self.assertEqual(screen("x" + GH[:2] + "​" + GH[2:]), (False, "secret"))  # zero-width

    def test_reason_never_holds_matched_text(self):
        for text in (WORD, SK, GH, GENERIC, "x" + GH, "xgithub" + "_pat_" + "B" * 30):
            ok, why = screen(text)
            self.assertFalse(ok)
            self.assertIn(why, {"deny", "secret"})
            self.assertNotIn(text, why)

    def test_neg_non_text_input_refused(self):
        for value in (None, b"bytes", 3, ["x"]):
            self.assertEqual(screen(value), (False, "doubt:type"))

    def test_neg_unreadable_or_malformed_list_refused(self):
        bad = [None, "not json", {"version": 1, "shapes": [], "words": ["two words"]},
               {"version": 1, "shapes": ["("], "words": []}, {"shapes": [], "words": []},
               # DORAGATE1FIX: an empty or wrong-shape list refuses everything, never allows
               {"version": 1, "shapes": [], "words": []}, [], {"version": 1},
               {"version": 1, "shapes": {}, "words": [WORD]}, {"version": 1, "shapes": "x", "words": []},
               {"version": 1, "shapes": [], "words": WORD}, {"version": 1, "shapes": [], "words": {WORD: 1}},
               {"version": 1, "shapes": [None], "words": [WORD]}, {"version": 1, "shapes": [], "words": [3]}]
        for data in bad:
            if data is None:
                self.path.unlink()
            else:
                write_list(self.root, data)
            self.assertEqual(screen("harmless text"), (False, "doubt:list"), data)

    def test_neg_empty_explicit_list_refused(self):  # CDX-DORAGATE1 F2
        self.assertEqual(screen("harmless text", (1, [], [])), (False, "doubt:list"))

    def test_secret_pattern_is_linear(self):  # CLREV-DORAGATE1 F1: was cubic on long runs
        for text in ("key" * 16667, "key-" * 12500, "Token" * 10000, "a" * 50000, "-" * 50000,
                     ("key=" + "x" * 31 + " ") * 1390, "pwd_" * 12500 + ":",
                     "xghp_" * 10000, "github" + "_pat_" * 4500, "gh" * 25000):
            start = time.perf_counter()
            dora_screen._SECRET.search(text)
            self.assertLess(time.perf_counter() - start, 0.1, text[:12])
        self.assertTrue(dora_screen._SECRET.search("cfg." + GENERIC))  # mid-text still found
        self.assertTrue(dora_screen._SECRET.search("my-api-token : " + "y" * 32))

    def test_neg_screen_error_refused(self):
        with mock.patch.object(dora_screen, "_screen", side_effect=RuntimeError("boom")):
            self.assertEqual(screen("harmless text"), (False, "doubt:screen"))

    def test_explicit_deny_argument_used(self):
        deny = dora_screen.load_deny(self.path)
        self.assertEqual(deny[0], 3)
        self.assertEqual(screen(WORD, deny), (False, "deny"))

    def test_lesson_extract_wrappers_use_shared_screen(self):
        deny = L.load_deny(self.path)
        self.assertTrue(L.denied(f"x {WORD}", deny))
        self.assertFalse(L.denied("clean line", deny))
        self.assertTrue(L.denied(None, deny))  # doubt drops
        self.assertIs(L.denied, dora_screen.denied)
        self.assertEqual(L.DENY_PATH.name, "lesson_deny_patterns.json")  # same file as before


if __name__ == "__main__":
    unittest.main()
