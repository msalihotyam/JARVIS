import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unicodedata import normalize

from dev.stt_bench import bench


class FakeEngine:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def transcribe(self, audio_path, hint):
        self.calls.append((audio_path.name, hint))
        output = self.outputs[audio_path.name]
        if isinstance(output, Exception):
            raise output
        return output


class SttBenchTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(dir=fixtures)))
        self.report = self.folder / "results.csv"

    def test_corpus(self):
        phrases = bench.load_phrases()
        self.assertEqual(len(phrases), 30)
        self.assertEqual(len({phrase.id for phrase in phrases}), 30)
        self.assertEqual(len({phrase.reference for phrase in phrases}), 30)
        for phrase in phrases:
            self.assertTrue(phrase.terms)
            self.assertEqual(bench.term_hit_rate(phrase.reference, phrase.terms), 1.0)

    def test_scores_and_deterministic_csv_with_fake_engine_only(self):
        phrase = bench.Phrase("01", "JARVIS kuyruğu aç", ("JARVIS",))
        audio = self.folder / "01.wav"
        audio.touch()
        engine = FakeEngine({"01.wav": ("Carvis kuyruğu aç", 0.25)})
        with patch("socket.socket", side_effect=AssertionError("network")), patch(
            "subprocess.Popen", side_effect=AssertionError("subprocess")
        ):
            rows = bench.run_benchmark(engine, [phrase], self.folder, self.report)
            self.assertIsNone(rows[0].corrected_term_hit_rate)
            rows = bench.run_benchmark(
                engine, [phrase], self.folder, self.report,
                corrector=lambda text: text.replace("Carvis", "JARVIS"),
            )
            first = self.report.read_bytes()
            second = self.folder / "second.csv"
            bench.run_benchmark(
                engine, [phrase], self.folder, second,
                corrector=lambda text: text.replace("Carvis", "JARVIS"),
            )
        self.assertEqual(first, second.read_bytes())
        self.assertIn(b"01,ok,0.3333333333333333,0.0,1.0,0.25", first)
        self.assertEqual(engine.calls, [("01.wav", ("JARVIS",))] * 3)

    def test_missing_and_failed_rows_continue_to_empty_transcript(self):
        phrases = [bench.Phrase(key, "Kuyruğu aç", ("kuyruğu",))
                   for key in ("missing", "failed", "empty")]
        for key in ("failed", "empty"):
            (self.folder / f"{key}.wav").touch()
        engine = FakeEngine({"failed.wav": RuntimeError("broken"), "empty.wav": ("", 0.5)})
        rows = bench.run_benchmark(engine, phrases, self.folder, self.report)
        self.assertEqual([row.status for row in rows], ["missing", "failed", "ok"])
        self.assertEqual(engine.calls, [("failed.wav", ("kuyruğu",)), ("empty.wav", ("kuyruğu",))])
        for row in rows[:2]:
            self.assertEqual(
                (row.word_error_rate, row.term_hit_rate, row.corrected_term_hit_rate, row.seconds),
                (None, None, None, None),
            )
        self.assertEqual(rows[2].word_error_rate, 1.0)
        self.assertEqual(rows[2].term_hit_rate, 0.0)

    def test_turkish_normalization_and_whole_term_matching(self):
        reference = "Çağrı, IŞIK ve İŞ kuyruğu."
        transcript = normalize("NFD", "çağrı ışık ve iş kuyruğu")
        self.assertEqual(bench.word_error_rate(reference, transcript), 0.0)
        self.assertEqual(bench.term_hit_rate(transcript, ("IŞIK", "İŞ kuyruğu")), 1.0)
        self.assertEqual(bench.term_hit_rate("işler iş başka kuyruk", ("İŞ kuyruk",)), 0.0)
        self.assertEqual(bench.word_error_rate("a b c d", "a x c d e f"), 0.75)

    def test_uppercase_i_matches_dotted_and_dotless_i(self):
        self.assertEqual(bench.term_hit_rate("Jarvis ile güne başladım", ("JARVIS",)), 1.0)
        self.assertEqual(bench.term_hit_rate("api ve istemci", ("API", "istemci")), 1.0)
        self.assertEqual(bench.term_hit_rate("ci raporu", ("CI",)), 1.0)
        self.assertEqual(bench.term_hit_rate("CI raporu", ("ci",)), 1.0)
        self.assertEqual(bench.word_error_rate("JARVIS", "Jarvis"), 0.0)
        self.assertEqual(bench.word_error_rate("Jarvis", "JARVIS"), 0.0)
        self.assertEqual(bench.word_error_rate("IŞIK", "ışık"), 0.0)
        self.assertEqual(bench.word_error_rate("İŞ", "iş"), 0.0)
        self.assertEqual(bench.word_error_rate("İŞ", "ış"), 1.0)
        self.assertEqual(bench.word_error_rate("ışık", "işik"), 1.0)
        self.assertEqual(bench.word_error_rate("ışık", "isik"), 1.0)
        self.assertEqual(bench.word_error_rate("iş", "İŞ".lower()), 0.0)
        self.assertEqual(bench.term_hit_rate("Jarvis'e sor", ("JARVIS",)), 1.0)
        self.assertEqual(bench.term_hit_rate("Jarvisi aç", ("JARVIS",)), 0.0)
        self.assertEqual(bench.term_hit_rate("", ("JARVIS",)), 0.0)
