"""STTREC1: 30-sentence recorder - session logic, atomic WAV files, path guard, phrases, static rules."""

import ast
import contextlib
import builtins
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from dev.stt_bench import record
from tests.adversarial import HOSTILE_PATH_PARTS, each

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCE = Path(record.__file__)
PCM = b"\x01\x00\xff\x7f" * 800  # 1600 frames = 0.1 s


class FakeSource:
    def __init__(self, data=PCM, start_error=None, stop_error=None):
        self.data, self.start_error, self.stop_error = data, start_error, stop_error
        self.started = 0

    def start(self):
        if self.start_error:
            raise self.start_error
        self.started += 1

    def stop(self):
        if self.stop_error:
            raise self.stop_error
        return self.data


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=FIXTURES))) / "audio"
        self.sentences = [f"cümle {n}" for n in range(1, 31)]

    def session(self, source=None, **kwargs):
        return record.Session(self.sentences, self.root / "run", source or FakeSource(), root=self.root, **kwargs)

    def take(self, session):
        session.toggle()
        session.toggle()

    def write_wav(self, name, frames=PCM):
        directory = self.root / "run"
        directory.mkdir(parents=True, exist_ok=True)
        with wave.open(str(directory / name), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(frames)

    def denied(self, error=PermissionError(13, "Erişim engellendi")):
        original = Path.is_file

        def is_file(path):
            if path.name.endswith(".wav"):
                raise error
            return original(path)

        return mock.patch.object(Path, "is_file", autospec=True, side_effect=is_file)

    def leftovers(self):
        return [p.name for p in (self.root / "run").glob("*") if not p.name.endswith(".wav")]


class SessionLogicTest(Base):
    def test_next_previous_clamp(self):
        s = self.session()
        s.previous()
        self.assertEqual(s.index, 0)
        for _ in range(40):
            s.next()
        self.assertEqual(s.index, 29)
        s.previous()
        self.assertEqual(s.index, 28)

    def test_toggle_records_and_status(self):
        s = self.session()
        self.assertEqual(s.status(0), "missing")
        s.toggle()
        self.assertTrue(s.recording)
        s.toggle()
        self.assertFalse(s.recording)
        self.assertEqual(s.status(0), "recorded")
        self.assertEqual(s.status(1), "missing")
        self.assertEqual(s.message, "01 kaydedildi.")

    def test_no_hold_path(self):
        for name in ("press", "release", "hold", "on_press", "on_release"):
            self.assertFalse(hasattr(record.Session, name), name)
        self.assertNotIn("pressed", SOURCE.read_text(encoding="utf-8"))
        self.assertNotIn("released", SOURCE.read_text(encoding="utf-8"))

    def test_navigation_refused_while_recording(self):
        s = self.session()
        s.toggle()
        s.next()
        self.assertEqual(s.index, 0)
        self.assertFalse(s.finish())
        self.assertTrue(s.message)

    def test_redo_rerecords_current(self):
        source = FakeSource()
        s = self.session(source)
        self.take(s)
        source.data = PCM * 2
        s.redo()
        self.assertTrue(s.recording)
        s.toggle()
        with wave.open(str(s.path(0)), "rb") as stream:
            self.assertEqual(stream.getnframes(), 3200)


class WavFileTest(Base):
    def test_name_and_format(self):
        s = self.session()
        s.next()
        s.next()
        self.take(s)
        path = self.root / "run" / "03.wav"
        self.assertTrue(path.is_file())
        with wave.open(str(path), "rb") as stream:
            self.assertEqual((stream.getframerate(), stream.getnchannels(), stream.getsampwidth()), (16000, 1, 2))
            self.assertEqual(stream.readframes(stream.getnframes()), PCM)
        self.assertEqual(self.leftovers(), [])


class AtomicReplaceTest(Base):
    def assert_kept(self, s, old):
        self.assertEqual(s.path(0).read_bytes(), old)
        self.assertFalse(s.recording)
        self.assertTrue(s.message)
        self.assertNotIn("kaydedildi", s.message)
        self.assertEqual(self.leftovers(), [])

    def test_failures_keep_old_file(self):
        cases = {
            "start raises": FakeSource(start_error=RuntimeError("mic yok")),
            "stop raises": FakeSource(stop_error=OSError("koptu")),
            "empty": FakeSource(data=b""),
            "none": FakeSource(data=None),
            "oversize": FakeSource(data=PCM * 10),
            "odd length": FakeSource(data=b"\x01\x02\x03"),
        }
        for label, source in cases.items():
            with self.subTest(label):
                self.write_wav("01.wav")
                old = (self.root / "run" / "01.wav").read_bytes()
                s = self.session(source, max_bytes=len(PCM) * 2)
                s.previous()  # resume starts at 02; go back to the recorded 01
                self.take(s)
                self.assert_kept(s, old)

    def test_failure_on_missing_sentence_leaves_no_file(self):
        s = self.session(FakeSource(data=b""))
        self.take(s)
        self.assertEqual(s.status(0), "missing")
        self.assertEqual(self.leftovers(), [])

    def test_rename_failure_keeps_old_and_removes_temp(self):
        self.write_wav("01.wav")
        old = (self.root / "run" / "01.wav").read_bytes()
        s = self.session(FakeSource(data=PCM * 2))
        s.previous()
        with mock.patch.object(record.os, "replace", side_effect=OSError("kilitli")):
            self.take(s)
        self.assert_kept(s, old)

    def assert_save_refused(self, s, old):
        self.assertFalse(s.recording)
        self.assertIn("Kaydedilemedi", s.message)
        if old is None:
            self.assertEqual(s.status(0), "missing")
        else:
            self.assert_kept(s, old)

    def test_occupied_target_dir_refused(self):
        # V1 review repro: the run directory path is a file -> mkdir FileExistsError
        self.root.mkdir(parents=True)
        (self.root / "run").write_bytes(b"x")
        s = self.session(FakeSource(data=PCM))
        self.take(s)
        self.assert_save_refused(s, None)
        self.assertEqual((self.root / "run").read_bytes(), b"x")

    def test_filesystem_step_failures_keep_old(self):
        # V2 mkdir denied, V3 unwritable dir (mkstemp), V4 write fails, V6 cleanup fails too, V7 non-OSError
        cases = {
            "mkdir denied": (Path, "mkdir", PermissionError("erişim yok")),
            "unwritable dir": (record.tempfile, "mkstemp", PermissionError("erişim yok")),
            "temp open fails": (builtins, "open", OSError("açılamadı")),  # F2 review P4: no temp, no fd leak
            "write fails": (record.wave.Wave_write, "writeframes", OSError("disk dolu")),
            "non-oserror": (record.wave.Wave_write, "writeframes", ValueError("bozuk")),
        }
        for label, (owner, name, error) in cases.items():
            with self.subTest(label):
                self.write_wav("01.wav")
                old = (self.root / "run" / "01.wav").read_bytes()
                s = self.session(FakeSource(data=PCM * 2))
                s.previous()
                s.toggle()
                with mock.patch.object(owner, name, side_effect=error):
                    s.toggle()
                self.assert_save_refused(s, old)

    def test_cleanup_failure_does_not_escape(self):
        # V6: replace fails and unlinking the temp fails as well
        self.write_wav("01.wav")
        old = (self.root / "run" / "01.wav").read_bytes()
        s = self.session(FakeSource(data=PCM * 2))
        s.previous()
        s.toggle()
        with mock.patch.object(record.os, "replace", side_effect=OSError("kilitli")), \
                mock.patch.object(Path, "unlink", side_effect=PermissionError("açık")):
            s.toggle()
        self.assertFalse(s.recording)
        self.assertIn("Kaydedilemedi", s.message)
        self.assertIn("geçici dosya kaldı", s.message)  # F2 review P10: a left temp is reported
        self.assertEqual(s.path(0).read_bytes(), old)

    def test_rerecord_replaces(self):
        self.write_wav("01.wav")
        s = self.session(FakeSource(data=PCM * 2))
        s.previous()
        self.take(s)
        with wave.open(str(s.path(0)), "rb") as stream:
            self.assertEqual(stream.getnframes(), 3200)
        self.assertEqual(self.leftovers(), [])


class ResumeAndFinishTest(Base):
    def test_resume_detects_existing(self):
        self.write_wav("01.wav")
        self.write_wav("02.wav", PCM * 5)
        s = self.session()
        self.assertEqual(s.index, 2)
        recorded, seconds = s.summary()
        self.assertEqual(recorded, 2)
        self.assertAlmostEqual(seconds, 0.6)

    def test_all_recorded_starts_at_first(self):
        for n in range(1, 31):
            self.write_wav(f"{n:02d}.wav")
        self.assertEqual(self.session().index, 0)

    def test_only_finish_ends(self):
        s = self.session()
        for _ in range(30):
            self.take(s)
            s.next()
        self.assertFalse(s.finished)
        self.assertTrue(s.finish())
        self.assertTrue(s.finished)
        s.toggle()
        self.assertFalse(s.recording)


def corrupt_chunk(path):
    """CDX-STTREC1FIX F1 class: a chunk header whose size runs past the file -> wave.open raises a bare RuntimeError."""
    data = bytearray(path.read_bytes())
    data[12:20] = b"f\0\0\0" + (1000).to_bytes(4, "little")  # unknown chunk; its skip seeks past the chunk end
    path.write_bytes(bytes(data))


class BadStr(Exception):
    def __str__(self):
        raise ValueError("str() itself fails")


def zero_rate(path):
    data = bytearray(path.read_bytes())
    data[24:28] = b"\0\0\0\0"
    path.write_bytes(bytes(data))


class UnreadableTest(Base):
    """Every refresh read (is_file, wave.open, header fields) turns into state "unreadable", never an exception."""

    def assert_unreadable(self, s, index=0):
        self.assertEqual(s.status(index), "unreadable")
        self.assertIn(f"{index + 1:02d}.wav okunamadı", s.unreadable[index])
        self.assertEqual(s.summary()[0], 0)

    def test_corrupt_chunk_header(self):
        # CDX F1: Session.summary() raised RuntimeError on cc50017
        self.write_wav("01.wav")
        corrupt_chunk(self.root / "run" / "01.wav")
        with self.assertRaises(RuntimeError):  # the fixture really is the bare-RuntimeError class
            wave.open(str(self.root / "run" / "01.wav"), "rb").close()
        s = self.session()
        self.assertEqual(s.summary(), (0, 0.0))
        self.assert_unreadable(s)
        self.assertIn("RuntimeError", s.unreadable[0])
        self.assertEqual(s.index, 0)  # an unreadable sentence is offered for re-recording

    def test_is_file_permission_denied(self):
        # CDX F2: Session.status(0) raised PermissionError on cc50017
        s = self.session()
        with self.denied():
            self.assert_unreadable(s)
            self.assertIn("PermissionError", s.unreadable[0])
            self.assertEqual(len(s.unreadable), record.COUNT)

    def test_other_read_failures(self):
        variants = {
            "oserror": lambda p: mock.patch.object(Path, "is_file", side_effect=OSError(5, "G/Ç hatası")),
            "not_riff": lambda p: p.write_bytes(b"not a wave file at all, 44 bytes long......"),
            "empty": lambda p: p.write_bytes(b""),
            "truncated": lambda p: p.write_bytes(p.read_bytes()[:20]),
            "zero_rate": zero_rate,
            "valueerror": lambda p: mock.patch.object(record.wave, "open", side_effect=ValueError("bozuk")),
            "str_raises": lambda p: mock.patch.object(record.wave, "open", side_effect=BadStr()),
        }
        for name, apply in variants.items():
            with self.subTest(name), tempfile.TemporaryDirectory(dir=FIXTURES) as tmp:
                self.root = Path(tmp) / "audio"
                self.write_wav("01.wav")
                s = self.session()
                patch = apply(self.root / "run" / "01.wav")
                with patch if hasattr(patch, "__enter__") else contextlib.nullcontext():
                    self.assert_unreadable(s)

    def test_unreadable_clears_when_readable_again(self):
        self.write_wav("01.wav")
        s = self.session()
        with self.denied():
            s.summary()
        self.assertEqual(s.status(0), "recorded")
        self.assertEqual(s.summary()[0], 1)
        self.assertEqual(s.unreadable, {})


class PathGuardTest(Base):
    def test_hostile_corpus(self):
        root = self.root.resolve()

        def check(part):
            try:
                result = record.allowed_dir(root / part, root)
            except ValueError:
                return
            self.assertTrue(result.is_relative_to(root), result)

        each(self, HOSTILE_PATH_PARTS, check)

    def test_explicit_refusals(self):
        refused = ["..", "x:stream", "x::$DATA", "CLAUDE~1", "PROGRA~1", "CON", "NUL.png", "con.txt",
                   "COM1", "LPT9", "a.", "a ", "aux "]

        def check(part):
            with self.assertRaises(ValueError):
                record.allowed_dir(self.root / part, self.root)

        each(self, refused, check)

    def test_absolute_and_unc_refused(self):
        values = ["\\\\host\\share\\audio", "//host/share", "\\\\?\\C:\\x", "\\\\.\\C:\\x", "\\\\localhost\\C$",
                  str(FIXTURES), "C:\\x", "", "a\0b"]

        def check(value):
            with self.assertRaises(ValueError):
                record.allowed_dir(value, self.root)

        each(self, values, check)

    def test_inside_root_allowed(self):
        self.assertEqual(record.allowed_dir(self.root / "run", self.root), (self.root / "run").resolve())
        self.assertEqual(record.allowed_dir(self.root, self.root), self.root.resolve())

    def test_session_refuses_outside(self):
        with self.assertRaises(ValueError):
            record.Session(self.sentences, FIXTURES, FakeSource(), root=self.root)

    def test_default_root_is_talk_audio(self):
        self.assertEqual(record.AUDIO_ROOT.parts[-3:], ("talk", "stt_bench", "audio"))


class PhrasesTest(Base):
    def write(self, payload):
        path = self.root.parent / "phrases.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        return path

    def test_real_file_has_30(self):
        sentences = record.load_sentences()
        self.assertEqual(len(sentences), 30)
        self.assertTrue(all(isinstance(s, str) and s for s in sentences))

    def test_malformed_refused(self):
        good = [{"id": f"{n:02d}", "reference": f"cümle {n}"} for n in range(1, 31)]
        bad = {
            "not json": "{nope",
            "object": {"a": 1},
            "29 entries": good[:29],
            "31 entries": good + [{"id": "31", "reference": "x"}],
            "wrong id": [{"id": "1", "reference": "x"}] + good[1:],
            "empty reference": [{"id": "01", "reference": "  "}] + good[1:],
            "non-dict": ["x"] + good[1:],
        }
        for label, payload in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                record.load_sentences(self.write(payload))
        self.assertEqual(len(record.load_sentences(self.write(good))), 30)

    def test_missing_file_refused(self):
        with self.assertRaises(ValueError):
            record.load_sentences(self.root.parent / "missing.json")

    def test_session_refuses_wrong_count(self):
        with self.assertRaises(ValueError):
            record.Session(self.sentences[:29], self.root, FakeSource(), root=self.root)


class StaticTest(unittest.TestCase):
    def test_no_network_and_multimedia_only_in_window(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        banned = {"socket", "urllib", "http", "requests"}
        owners = {}
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    owners.setdefault(id(node), func.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                self.assertNotIn(name.split(".")[0], banned, name)
                if "QtMultimedia" in name:
                    self.assertEqual(owners.get(id(node)), "window", name)


class WindowTest(Base):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_controls_and_clicks(self):
        from PySide6.QtWidgets import QLabel, QPushButton

        s = self.session()
        w = self.enterContext(_closing(record.build_window(s), s))
        w.show()
        buttons = {b.objectName(): b for b in w.findChildren(QPushButton)}
        self.assertEqual(set(buttons), {"previous", "toggle", "redo", "next", "done"})
        for b in buttons.values():
            self.assertGreaterEqual(b.minimumHeight(), 44)
            self.assertGreaterEqual(b.minimumWidth(), 44)
        sentence = w.findChild(QLabel, "sentence")
        self.assertGreaterEqual(sentence.font().pointSize(), 20)
        self.assertEqual(sentence.text(), "cümle 1")
        self.assertEqual(buttons["toggle"].text(), "Kaydet")
        buttons["toggle"].click()
        self.assertEqual(buttons["toggle"].text(), "Bitir")
        self.assertFalse(buttons["next"].isEnabled())
        buttons["toggle"].click()
        self.assertIn("1/30", w.findChild(QLabel, "progress").text())
        buttons["next"].click()
        self.assertEqual(sentence.text(), "cümle 2")
        w.close()
        self.assertTrue(w.isVisible())
        buttons["done"].click()
        self.assertFalse(w.isVisible())

    def test_failed_save_refreshes_window(self):
        # review repro through the window: occupied target -> toggle back to Kaydet, buttons enabled, message shown
        from PySide6.QtWidgets import QLabel, QPushButton

        self.root.mkdir(parents=True)
        (self.root / "run").write_bytes(b"x")
        s = self.session()
        w = self.enterContext(_closing(record.build_window(s), s))
        buttons = {b.objectName(): b for b in w.findChildren(QPushButton)}
        buttons["toggle"].click()
        buttons["toggle"].click()
        self.assertEqual(buttons["toggle"].text(), "Kaydet")
        for name in ("previous", "next", "redo", "done"):
            self.assertTrue(buttons[name].isEnabled(), name)
        self.assertIn("Kaydedilemedi", w.findChild(QLabel, "message").text())

    def test_raising_action_still_refreshes(self):
        # V8: an unexpected exception from an action never skips refresh()
        from PySide6.QtWidgets import QLabel, QPushButton

        s = self.session()
        w = self.enterContext(_closing(record.build_window(s), s))
        buttons = {b.objectName(): b for b in w.findChildren(QPushButton)}
        buttons["toggle"].click()
        with mock.patch.object(s, "_save", side_effect=RuntimeError("beklenmedik")):
            buttons["toggle"].click()
        self.assertEqual(buttons["toggle"].text(), "Kaydet")
        self.assertTrue(buttons["done"].isEnabled())
        self.assertIn("beklenmedik", w.findChild(QLabel, "message").text())

    def test_corrupt_file_at_startup(self):
        # CDX F1: build_window raised RuntimeError on cc50017
        from PySide6.QtWidgets import QLabel

        self.write_wav("01.wav")
        corrupt_chunk(self.root / "run" / "01.wav")
        s = self.session()
        w = self.enterContext(_closing(record.build_window(s), s))
        self.assertIn("okunamıyor", w.findChild(QLabel, "position").text())
        self.assertIn("01.wav okunamadı", w.findChild(QLabel, "message").text())
        self.assertIn("0/30", w.findChild(QLabel, "progress").text())

    def test_read_denied_folder_failed_save_refreshes(self):
        # CDX F2: is_file PermissionError + mkstemp PermissionError; on cc50017 the toggle stayed Bitir
        from PySide6.QtWidgets import QLabel, QPushButton

        s = self.session()
        w = self.enterContext(_closing(record.build_window(s), s))
        buttons = {b.objectName(): b for b in w.findChildren(QPushButton)}
        with self.denied(), mock.patch.object(record.tempfile, "mkstemp", side_effect=PermissionError(13, "yazma engellendi")):
            buttons["toggle"].click()
            buttons["toggle"].click()
        self.assertEqual(buttons["toggle"].text(), "Kaydet")
        for name in ("previous", "next", "redo", "done"):
            self.assertTrue(buttons[name].isEnabled(), name)
        text = w.findChild(QLabel, "message").text()
        self.assertIn("Kaydedilemedi", text)
        self.assertIn("01.wav okunamadı", text)
        self.assertIn("dosya daha okunamadı", text)


class _closing:
    def __init__(self, widget, session):
        self.widget, self.session = widget, session

    def __enter__(self):
        return self.widget

    def __exit__(self, *exc):
        self.session.finished = True
        self.widget.close()
        self.widget.deleteLater()


if __name__ == "__main__":
    unittest.main()
