import ast
import contextlib
import io
import pathlib
import unittest
import unicodedata

from jarvis import talk_hear
from jarvis.talk_hear import _fold as fold
from jarvis.talk_hear import build_shown, correction_candidates

MODULE = pathlib.Path(talk_hear.__file__)


def fixed(text, replacements):
    return lambda raw: (text, replacements)


class TalkHearTests(unittest.TestCase):
    # A1 shown text, "Anladığım: " line, replacements listed
    def test_shown_line_and_replacements(self):
        raw = "jar vis takvimi aç"
        got = build_shown(raw, fixed("Jarvis takvimi aç", [{"start": 0, "end": 7, "text": "Jarvis"}]))
        self.assertEqual(got, {"ok": True, "shown": "Jarvis takvimi aç",
                               "line": "Anladığım: Jarvis takvimi aç",
                               "replacements": [{"start": 0, "end": 7, "heard": "jar vis", "meant": "Jarvis"}],
                               "uncorrected": False})

    def test_no_replacements_shows_raw(self):
        got = build_shown("merhaba", fixed("merhaba", []))
        self.assertEqual((got["ok"], got["line"], got["replacements"]), (True, "Anladığım: merhaba", []))

    def test_replacements_sorted_and_must_match_text(self):
        raw = "aa bb cc"
        reps = [{"start": 6, "end": 8, "text": "C"}, {"start": 0, "end": 2, "text": "A"}]
        got = build_shown(raw, fixed("A bb C", reps))
        self.assertEqual([r["meant"] for r in got["replacements"]], ["A", "C"])
        self.assertEqual(build_shown(raw, fixed("X bb C", reps)), {"ok": False, "reason": "replacements_mismatch"})

    # A2 word-level differences, heard -> meant, limits
    def test_candidates_heard_to_meant(self):
        got = correction_candidates("Anladığım jarviz takvimi aç ve notu kaydet",
                                    "Anladığım Jarvis takvimi aç ve notu kaydet")
        self.assertEqual(got, {"ok": True, "candidates": [{"heard": "jarviz", "meant": "Jarvis"}]})

    def test_at_most_five_candidates(self):
        heard = " ".join(f"w{i} x{i}" for i in range(7))
        meant = " ".join(f"w{i} y{i}" for i in range(7))
        got = correction_candidates(heard, meant)
        self.assertEqual(got["candidates"], [{"heard": f"x{i}", "meant": f"y{i}"} for i in range(5)])

    def test_side_over_four_words_dropped(self):
        base = "bir iki üç dört beş altı yedi sekiz dokuz on on bir"
        got = correction_candidates(base + " a b c d e", base + " q")
        self.assertEqual(got["candidates"], [])
        got = correction_candidates(base + " a b c d", base + " q")
        self.assertEqual(got["candidates"], [{"heard": "a b c d", "meant": "q"}])

    # A3 records only, nothing written
    def test_returns_records_and_leaves_inputs_alone(self):
        reps = [{"start": 0, "end": 3, "text": "Jar"}]
        before = [dict(r) for r in reps]
        build_shown("jar on", fixed("Jar on", reps))
        self.assertEqual(reps, before)
        names = {n.id for n in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))) if isinstance(n, ast.Name)}
        self.assertFalse(names & {"open", "__import__", "exec", "eval"})

    # A4 no difference -> no candidates
    def test_no_difference_no_candidates(self):
        self.assertEqual(correction_candidates("takvimi aç", "takvimi aç"), {"ok": True, "candidates": []})
        self.assertEqual(correction_candidates("TAKVİMİ AÇ.", "takvimi aç"), {"ok": True, "candidates": []})

    # A5 Turkish folding and NFC
    def test_turkish_fold_and_nfc(self):
        self.assertEqual(fold("KIZ"), fold("kız"))
        self.assertNotEqual(fold("KIZ"), fold("kiz"))
        self.assertEqual(fold("İstanbul"), "istanbul")
        decomposed = unicodedata.normalize("NFD", "İzmir")
        self.assertNotEqual(decomposed, "İzmir")
        self.assertEqual(fold(decomposed), "izmir")
        got = correction_candidates(f"{decomposed} hava durumu nasıl", "izmir hava durumu nasıl")
        self.assertEqual(got["candidates"], [])
        got = correction_candidates("KIZ kulesi nerede bugün", "kiz kulesi nerede bugün")
        self.assertEqual(got["candidates"], [{"heard": "KIZ", "meant": "kiz"}])
        # casefold, not lower(): the words are equal, so no candidate
        self.assertEqual(fold("Straße"), fold("STRASSE"))
        self.assertEqual(correction_candidates("a Straße b c d", "a STRASSE b c d")["candidates"], [])

    def test_fold_private_and_public_api_refuses_non_text(self):
        self.assertFalse(hasattr(talk_hear, "fold"))
        for shown, corrected, reason in ((None, "x", "shown_not_text"), ("x", None, "corrected_not_text"),
                                         (b"x", "x", "shown_not_text"), ("x", 3, "corrected_not_text")):
            self.assertEqual(correction_candidates(shown, corrected), {"ok": False, "reason": reason})

    # N1 more than half changed -> new sentence
    def test_more_than_half_changed_no_candidates(self):
        self.assertEqual(correction_candidates("bir iki üç dört", "bir X Y dört")["candidates"],
                         [{"heard": "iki üç", "meant": "X Y"}])
        self.assertEqual(correction_candidates("bir iki üç dört", "bir X Y Z"),
                         {"ok": True, "candidates": [], "note": "new_sentence"})
        self.assertEqual(correction_candidates("jar vis aç", "Jarvis aç")["candidates"], [])

    # N2 unsafe candidates dropped
    def test_unsafe_candidates_dropped(self):
        base = "lütfen şunu aç sonra bana haber ver"
        for bad in ("C:\\notes", "src/app.py", "https://x.com", "www.x.com", "/deploy", "~home",
                    "a\x07b", "a\u202eb", "a\u200fb", "a\u2066b", "a\u200bb",
                    # scheme-only URIs, case, later word, edge punctuation, NFKC, homoglyph
                    "mailto:a@b.c", "javascript:alert(1", "tel:123", "data:text", "ms-settings:x",
                    "JavaScript:x", "MAILTO:a", "a\u00e7 www.x.com", "a\u00e7 mailto:a", "(javascript:alert(1)",
                    "javascript\uff1ax", "a\uff0fb", "x\uff0ecom", "x\u3002com", "\u0458avascript:x",
                    "java\u200bscript:x", "14:30", "javascript\u2236x", "javascript\ua789x",
                    "a\u2044b", "a\u2215b", "a\u29f8b", "x\ufe52com", "x\u2024com", "a\uff20b",
                    # separators NFKC does not fold (Lm, Sm, Lo, So), combining marks, fold before domain
                    "javascript\u02d0x", "src\u29f5app", "a\ua4fdb", "a\u2571b", "a\u0338b",
                    "x.c\u0336om", "github\u0336.com", "x.i\u0307o", "x.\u0130o", "github.\u0130o", "x.IO",
                    "g\u043eogle", "x.c\u03bfm", "a\u00a6b",
                    # NFKC to several dots, non-word char before the dot, fold after NFKC, compat forms
                    "x\u2026com", "x\u2025com", "x..com", "foo_.com", "foo-.com", "\u1d42ww.x",
                    "a\u2100b", "a\ufe55b", "a\ufe6bb", "\U0001d416\U0001d416\U0001d416.xy",
                    # DECISION TAKEN: bare domains and emails count as URLs; non-ASCII punctuation drops
                    "x.com", "github.com", "a@b", "Ankara\u2019ya"):
            with self.subTest(bad=repr(bad)):
                got = correction_candidates(base.replace("şunu", "dosya"), base.replace("şunu", bad))
                self.assertEqual(got["candidates"], [])
                got = correction_candidates(base.replace("şunu", bad), base.replace("şunu", "dosya"))
                self.assertEqual(got["candidates"], [])
        # normal words, a trailing colon and a decimal number stay candidates
        for good, meant in (("saat:", "saat"), ("İzmir", "İzmir"), ("3.5", "3.5"), ("Göğüş", "Göğüş"),
                            ("ÇIĞLIK", "ÇIĞLIK"), ("Ankara'ya", "Ankara'ya"), ("Kâğıt", "Kâğıt"),
                            ("DİKKAT", "DİKKAT"), (unicodedata.normalize("NFD", "Göğüş"), "Göğüş")):
            with self.subTest(good=good):
                got = correction_candidates(base.replace("şunu", "dosya"), base.replace("şunu", good))
                self.assertEqual(got["candidates"], [{"heard": "dosya", "meant": meant}])

    # N3 raw empty or too long
    def test_raw_empty_or_too_long_refused(self):
        never = fixed("x", [])
        for raw, reason in (("", "raw_empty"), ("  \n", "raw_empty"), ("a" * 4001, "raw_too_long"),
                            (None, "raw_not_text"), (b"x", "raw_not_text")):
            self.assertEqual(build_shown(raw, never), {"ok": False, "reason": reason})
        self.assertTrue(build_shown("a" * 4000, fixed("a" * 4000, []))["ok"])
        self.assertEqual(correction_candidates("a" * 4001, "a")["reason"], "shown_too_long")

    # N4 spans outside or overlapping
    def test_bad_spans_refused(self):
        raw = "abcdef"
        for reps in ([{"start": 4, "end": 7, "text": "x"}], [{"start": -1, "end": 2, "text": "x"}],
                     [{"start": 2, "end": 2, "text": "x"}], [{"start": 0, "end": 3, "text": "x"},
                                                             {"start": 2, "end": 4, "text": "y"}],
                     [{"start": True, "end": 2, "text": "x"}], [{"start": 0, "end": 2}], ["0-2"], "0-2"):
            with self.subTest(reps=repr(reps)[:80]):
                self.assertEqual(build_shown(raw, fixed("x", reps)),
                                 {"ok": False, "reason": "replacement_span_invalid"})

    # N5 corrector raises -> uncorrected
    def test_corrector_raises_shows_raw_uncorrected(self):
        def boom(raw):
            raise RuntimeError("down")
        self.assertEqual(build_shown("jar vis", boom),
                         {"ok": True, "shown": "jar vis", "line": "Anladığım: jar vis",
                          "replacements": [], "uncorrected": True})

    # Common rules
    def test_wrong_types_and_deep_input_refused(self):
        self.assertEqual(build_shown("x", None)["reason"], "corrector_not_callable")
        self.assertEqual(build_shown("x", lambda raw: "x")["reason"], "corrector_bad_result")
        self.assertEqual(build_shown("x", lambda raw: (1, []))["reason"], "corrected_not_text")
        self.assertEqual(correction_candidates(["x"], "x")["reason"], "shown_not_text")
        deep = []
        for _ in range(100000):
            deep = [deep]
        self.assertEqual(build_shown("x", fixed("x", deep))["reason"], "replacement_span_invalid")

        class Deep(dict):
            def get(self, key, default=None):
                raise RecursionError
        self.assertEqual(build_shown("x", fixed("x", [Deep()])), {"ok": False, "reason": "input_too_deep"})

    def test_refusals_ascii_under_cp1252(self):
        refusals = [build_shown("", None), build_shown("ğ" * 4001, None), build_shown("x", None),
                    correction_candidates(None, "ı"), build_shown("ışık", fixed("ş", [{"start": 9, "end": 10, "text": "ş"}]))]
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        with contextlib.redirect_stdout(stream):
            for refusal in refusals:
                self.assertFalse(refusal["ok"])
                print(refusal)
                self.assertTrue(refusal["reason"].isascii() and refusal["reason"].isprintable())
        stream.flush()

    def test_deterministic(self):
        args = ("a b c d e f g h", "a B x d e y g h")
        self.assertEqual(correction_candidates(*args), correction_candidates(*args))

    def test_no_io_or_network_imports(self):
        forbidden = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "requests",
                     "importlib", "shutil", "tempfile", "sqlite3"}
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                found.add((node.module or "").split(".")[0])
        self.assertTrue(found)
        self.assertFalse(found & forbidden, found)


if __name__ == "__main__":
    unittest.main()
