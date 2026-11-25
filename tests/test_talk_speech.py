"""TALKSPEECH1: speech seam (AUD0 F9) - interfaces, fake engines, voice in/out with text fallback."""

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis import talk_speech as ts
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, each

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "jarvis" / "talk_speech.py"
GOOD = ts.fake_audio_record(b"RIFF....WAVEfmt ")


def record(**changes):
    return {**GOOD, **changes}


class Spy:
    """Callable that records calls and returns value or raises error."""

    def __init__(self, value=None, error=None):
        self.value, self.error, self.calls = value, error, []

    def __call__(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.value


class HostileMapping(dict):
    def get(self, *args):
        raise RecursionError("deep")


def ascii_codes(case, result):
    for key in ("reason", "flag", "fallback"):
        value = result.get(key)
        if value is not None:
            case.assertTrue(value.isascii(), (key, value))


class InterfaceTests(unittest.TestCase):
    def test_protocols_and_fakes(self):
        self.assertTrue(hasattr(ts.SpeechToText, "transcribe"))
        self.assertTrue(hasattr(ts.TextToSpeech, "speak"))
        stt = ts.FakeSpeechToText("selam", 2.5)
        self.assertEqual(stt.transcribe(GOOD, "h"), ("selam", 2.5))
        self.assertEqual(stt.calls, [(GOOD, "h")])
        tts = ts.FakeTextToSpeech()
        first, second = tts.speak("merhaba"), tts.speak("merhaba")
        self.assertEqual(first, second)
        self.assertIsNone(ts.check_audio_record(first))
        self.assertEqual(tts.calls, ["merhaba", "merhaba"])

    def test_record_object_with_attributes(self):
        class Rec:
            kind, sha256, bytes, relative_path = (GOOD["kind"], GOOD["sha256"], GOOD["bytes"],
                                                  "a/b.wav")
        self.assertIsNone(ts.check_audio_record(Rec()))


class RecordRefusalTests(unittest.TestCase):
    def assert_refused_before_engine(self, rec, reason):
        stt, hint, hear = ts.FakeSpeechToText(), Spy("hint"), Spy("heard")
        result = ts.voice_in(rec, stt, hint, hear)
        self.assertEqual(result, {"ok": False, "reason": reason})
        self.assertEqual((stt.calls, hint.calls, hear.calls), ([], [], []))

    def test_missing_or_bad_sha256(self):
        for bad in (None, "", "A" * 64, "g" * 64, "0" * 63, 123):
            with self.subTest(bad=bad):
                self.assert_refused_before_engine(record(sha256=bad), "record_no_sha256")
        rec = dict(GOOD)
        del rec["sha256"]
        self.assert_refused_before_engine(rec, "record_no_sha256")

    def test_named_path_attacks(self):
        for path in ("..", "a/../b", "/x", "C:\\x", "\\\\host\\share", "x:stream",
                     "PROGRA~1", "voice/~1.wav", "", None, "a" * 261):
            with self.subTest(path=path):
                self.assert_refused_before_engine(record(relative_path=path), "record_bad_path")

    def test_hostile_path_corpus(self):
        # "DATA" is a plain name under the caller's root; protected-dir policy is the root's job.
        parts = [p for p in HOSTILE_PATH_PARTS if p != "DATA"]
        each(self, parts, lambda p: self.assert_refused_before_engine(
            record(relative_path=p), "record_bad_path"))
        each(self, parts, lambda p: self.assert_refused_before_engine(
            record(relative_path=f"voice/{p}/x.wav"), "record_bad_path"))

    def test_wrong_kind_and_bad_size(self):
        self.assert_refused_before_engine(record(kind="png"), "record_wrong_kind")
        self.assert_refused_before_engine(None, "record_wrong_kind")
        self.assert_refused_before_engine("audio", "record_wrong_kind")
        each(self, HOSTILE_NUMBERS + (0, -1, ts.MAX_AUDIO_BYTES + 1),
             lambda n: self.assert_refused_before_engine(record(bytes=n), "record_bad_size"))

    def test_hostile_mapping_does_not_escape(self):
        self.assertEqual(ts.voice_in(HostileMapping(GOOD), ts.FakeSpeechToText(), Spy(), Spy()),
                         {"ok": False, "reason": "input_too_deep"})


class HintTests(unittest.TestCase):
    def test_within_cap_kept(self):
        self.assertEqual(ts.cut_hint("  JARVIS, Salih  "), "JARVIS, Salih")
        self.assertEqual(ts.cut_hint(None), "")

    def test_over_cap_cut_at_term_boundary(self):
        terms = [f"term{i:03d}" for i in range(60)]
        hint = ", ".join(terms)
        cut = ts.cut_hint(hint)
        self.assertLessEqual(len(cut), ts.MAX_HINT)
        self.assertTrue(cut)
        self.assertTrue(set(cut.split(", ")) <= set(terms))
        self.assertTrue(hint.startswith(cut))

    def test_single_oversized_term_gives_empty(self):
        self.assertEqual(ts.cut_hint("x" * (ts.MAX_HINT + 50)), "")

    def test_engine_receives_cut_hint(self):
        stt = ts.FakeSpeechToText()
        ts.voice_in(GOOD, stt, lambda: "ab " * 200, Spy())
        hint = stt.calls[0][1]
        self.assertLessEqual(len(hint), ts.MAX_HINT)
        self.assertEqual(set(hint.split()), {"ab"})

    def test_failing_or_bad_build_hint_means_empty_hint(self):
        for build in (Spy(error=ValueError("x")), Spy(error=RecursionError()), Spy(42), None):
            with self.subTest(build=build):
                stt = ts.FakeSpeechToText()
                self.assertTrue(ts.voice_in(GOOD, stt, build, Spy())["ok"])
                self.assertEqual(stt.calls[0][1], "")


class VoiceInTests(unittest.TestCase):
    def test_transcript_passed_to_hear(self):
        hear = Spy("heard")
        result = ts.voice_in(GOOD, ts.FakeSpeechToText("ışık aç", 3), Spy("JARVIS"), hear)
        self.assertEqual(result, {"ok": True, "text": "ışık aç", "seconds": 3.0, "heard": "heard"})
        self.assertEqual(hear.calls, [("ışık aç",)])

    def assert_not_understood(self, result, flag):
        self.assertEqual(result, {"ok": False, "reason": "not_understood", "flag": flag,
                                  "fallback": "text"})

    def test_bad_engine_text(self):
        cases = (("", "empty"), ("  \n\t ", "empty"), (None, "not_text"), (b"x", "not_text"),
                 (42, "not_text"), ("A" * (ts.MAX_TEXT + 1), "too_long"))
        for text, flag in cases:
            with self.subTest(text=repr(text)[:40]):
                hear = Spy()
                self.assert_not_understood(
                    ts.voice_in(GOOD, ts.FakeSpeechToText(text), Spy(""), hear), flag)
                self.assertEqual(hear.calls, [])

    def test_hostile_text_never_escapes(self):
        def check(text):
            result = ts.voice_in(GOOD, ts.FakeSpeechToText(text), Spy(""), Spy("ok"))
            ascii_codes(self, result)
            if not result["ok"]:
                self.assertEqual(result["reason"], "not_understood")
        each(self, HOSTILE_TEXT, check)
        for text, flag in (("a\x00b", "unsafe_text"), ("a\ud800b", "unsafe_text"),
                           ("a\u202eb", "unsafe_text"), ("A" * 100_000, "too_long")):
            self.assert_not_understood(ts.voice_in(GOOD, ts.FakeSpeechToText(text), Spy(""),
                                                   Spy()), flag)

    def test_bad_result_shape_and_seconds(self):
        class Shape:
            def __init__(self, value):
                self.value = value

            def transcribe(self, rec, hint):
                return self.value
        for value in (None, "text", ("a",), ("a", 1, 2)):
            with self.subTest(value=value):
                self.assert_not_understood(ts.voice_in(GOOD, Shape(value), Spy(""), Spy()),
                                           "bad_result")
        each(self, HOSTILE_NUMBERS + (-1, ts.MAX_SECONDS + 1), lambda s: self.assert_not_understood(
            ts.voice_in(GOOD, ts.FakeSpeechToText("a", s), Spy(""), Spy()), "bad_seconds"))

    def test_engine_raises_falls_back(self):
        for error in (RuntimeError("boom"), RecursionError(), MemoryError(), KeyError("k")):
            with self.subTest(error=type(error).__name__):
                self.assert_not_understood(ts.voice_in(
                    GOOD, ts.FakeSpeechToText(error=error), Spy(""), Spy()), "engine_failed")
        self.assert_not_understood(ts.voice_in(GOOD, None, Spy(""), Spy()), "no_engine")

    def test_hear_raises_falls_back(self):
        for error in (ValueError("x"), RecursionError()):
            self.assert_not_understood(ts.voice_in(GOOD, ts.FakeSpeechToText(), Spy(""),
                                                   Spy(error=error)), "hear_failed")


class VoiceOutTests(unittest.TestCase):
    def test_audio_and_text(self):
        prepare, tts = Spy("merhaba efendim"), ts.FakeTextToSpeech()
        result = ts.voice_out("Merhaba.", tts, prepare)
        self.assertEqual(result["text"], "Merhaba.")
        self.assertIsNone(result["flag"])
        self.assertEqual(result["audio"], ts.fake_audio_record("merhaba efendim".encode()))
        self.assertEqual((prepare.calls, tts.calls), ([("Merhaba.",)], ["merhaba efendim"]))
        self.assertIsNone(ts.voice_out("a", ts.FakeTextToSpeech(), None)["flag"])

    def assert_text_only(self, result, text, flag):
        self.assertEqual(result, {"ok": True, "audio": None, "text": text, "flag": flag})

    def test_no_engine_or_engine_failure_is_text_only(self):
        self.assert_text_only(ts.voice_out("hi", None, None), "hi", "no_engine")
        for error in (OSError("x"), RecursionError(), MemoryError()):
            self.assert_text_only(ts.voice_out("hi", ts.FakeTextToSpeech(error=error), None),
                                  "hi", "engine_failed")
        self.assert_text_only(ts.voice_out("hi", ts.FakeTextToSpeech(), Spy(error=ValueError())),
                              "hi", "prepare_failed")
        self.assert_text_only(ts.voice_out("hi", ts.FakeTextToSpeech(), Spy(None)), "hi",
                              "not_text")

    def test_bad_audio_record_is_text_only(self):
        for bad in (record(kind="png"), record(bytes=0), record(sha256=None),
                    record(relative_path="../x.wav"), None, "audio", b"", 42):
            with self.subTest(bad=repr(bad)[:40]):
                # A plain engine, since FakeTextToSpeech(record=None) means its default record.
                tts = SimpleNamespace(speak=lambda text, bad=bad: bad)
                self.assert_text_only(ts.voice_out("hi", tts, None), "hi", "bad_audio")
        self.assert_text_only(ts.voice_out(
            "hi", ts.FakeTextToSpeech(record=HostileMapping(GOOD)), None), "hi", "engine_failed")

    def test_invalid_text_refused(self):
        for text, reason in ((None, "text_invalid"), (b"x", "text_invalid"), (1, "text_invalid"),
                             ("A" * (ts.MAX_TEXT + 1), "text_invalid"), ("  ", "text_empty")):
            self.assertEqual(ts.voice_out(text, ts.FakeTextToSpeech(), None),
                             {"ok": False, "reason": reason})

    def test_hostile_text_never_escapes(self):
        def check(text):
            tts = ts.FakeTextToSpeech()
            result = ts.voice_out(text, tts, None)
            ascii_codes(self, result)
            if result["ok"]:
                self.assertEqual(result["text"], text)
                if result["flag"]:
                    self.assertIsNone(result["audio"])
                    self.assertEqual(tts.calls, [])
        each(self, HOSTILE_TEXT, check)
        for text in ("a\x00b", "a\ud800b", "a\u202eb"):
            self.assert_text_only(ts.voice_out(text, ts.FakeTextToSpeech(), None), text,
                                  "unsafe_text")


class StaticImportTests(unittest.TestCase):
    ALLOWED = {"hashlib", "math", "re", "unicodedata", "collections.abc", "typing"}
    BANNED = {"torch", "whisper", "pyaudio", "sounddevice", "subprocess", "socket"}

    def test_imports_stdlib_allowlist_only(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names.add(node.module)
            elif isinstance(node, ast.Name):
                self.assertNotIn(node.id, {"__import__", "importlib", "open", "eval", "exec"})
        self.assertLessEqual(names, self.ALLOWED)
        roots = {name.split(".")[0] for name in names}
        self.assertFalse(roots & self.BANNED)
        self.assertFalse({n for n in names if n.startswith(("jarvis", "telegram"))})


class ImportIsolationTests(unittest.TestCase):
    def test_core_and_app_do_not_import_module(self):
        code = ("import sys, jarvis.core, jarvis.app\n"
                "assert 'jarvis.talk_speech' not in sys.modules\n"
                "import jarvis.talk_speech\n"
                "assert 'jarvis.talk_speech' in sys.modules\n"
                "print('isolated')\n")
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,  # nosec B603
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("isolated", proc.stdout)


if __name__ == "__main__":
    unittest.main()
