import ast
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from jarvis import telegram_files as tf


PDF = {"file_id": "BQACAgQAAx0", "file_size": 2048, "mime_type": "application/pdf",
       "file_name": "report.pdf"}
VOICE = {"file_id": "AwACAgQAAx0", "file_size": 4096, "duration": 12, "mime_type": "audio/ogg"}
MIB = 1024 * 1024


class TelegramFileTests(unittest.TestCase):
    def plan(self, base, **changes):
        drop = {key for key, value in changes.items() if value is None}
        descriptor = {k: v for k, v in {**base, **changes}.items() if k not in drop}
        return tf.plan_download(descriptor, voice=base is VOICE)

    def test_accepted_file_plan(self):
        plan = self.plan(PDF)
        self.assertEqual(
            (plan.file_id, plan.kind, plan.extension, plan.size, plan.voice, plan.duration),
            ("BQACAgQAAx0", "pdf", ".pdf", 2048, False, None),
        )
        self.assertEqual((plan.mime_hint, plan.name_hint), ("application/pdf", "report.pdf"))
        self.assertEqual(plan.verify, "verify content after download")
        self.assertIs(plan.trusted, False)

    def test_accepted_voice_plan(self):
        plan = self.plan(VOICE)
        self.assertEqual((plan.kind, plan.extension, plan.voice, plan.duration),
                         ("ogg", ".ogg", True, 12))
        self.assertEqual(self.plan(VOICE, mime_type=None).kind, "ogg")
        self.assertEqual(self.plan(VOICE, mime_type="audio/opus").extension, ".opus")
        self.assertEqual(self.plan(VOICE, duration=0).duration, 0)

    def test_kind_allowlist(self):
        expected = {"png", "jpg", "webp", "pdf", "txt", "md", "csv", "json", "xlsx", "docx"}
        self.assertEqual(set(tf.FILE_MIMES.values()), expected)
        self.assertEqual(set(tf.VOICE_MIMES.values()), {"ogg", "opus"})
        for mime, kind in tf.FILE_MIMES.items():
            with self.subTest(mime=mime):
                plan = self.plan(PDF, mime_type=mime, file_name=None)
                self.assertEqual((plan.kind, plan.extension), (kind, "." + kind))
        self.assertEqual(self.plan(PDF, mime_type="Text/Plain; charset=utf-8",
                                   file_name="a.TXT").kind, "txt")
        self.assertEqual(self.plan(PDF, mime_type="image/jpeg", file_name="a.jpeg").kind, "jpg")

    def test_size_and_duration_caps_are_parameters(self):
        self.assertEqual(tf.DEFAULT_MAX_BYTES, 20 * MIB)
        self.assertIsInstance(self.plan(PDF, file_size=20 * MIB), tf.DownloadPlan)
        self.assertEqual(self.plan(PDF, file_size=20 * MIB + 1), tf.Refusal.SIZE_OVER_CAP)
        self.assertEqual(tf.plan_download(PDF, max_bytes=2047), tf.Refusal.SIZE_OVER_CAP)
        self.assertIsInstance(tf.plan_download(PDF, max_bytes=2048), tf.DownloadPlan)
        self.assertEqual(tf.plan_download(VOICE, voice=True, max_voice_seconds=11),
                         tf.Refusal.DURATION_OVER_CAP)
        self.assertIsInstance(tf.plan_download(VOICE, voice=True, max_voice_seconds=12),
                              tf.DownloadPlan)
        for limits in ({"max_bytes": 0}, {"max_bytes": True}, {"max_bytes": "20"},
                       {"max_voice_seconds": -1}, {"max_voice_seconds": 1.5}, {"voice": 1}):
            with self.subTest(limits=limits):
                self.assertEqual(tf.plan_download(PDF, **limits), tf.Refusal.INVALID_LIMIT)

    def test_mime_and_name_are_hints_only(self):
        plan = self.plan(PDF, file_name="cv.pdf")
        self.assertEqual(plan.verify, tf.VERIFY_NOTE)
        self.assertEqual(plan.extension, ".pdf")
        self.assertNotIn("cv", plan.extension)

    def test_file_id_passes_through_opaque(self):
        odd = "AgAD/../x:y%2e~1 " + "Z" * 200
        self.assertEqual(self.plan(PDF, file_id=odd).file_id, odd)
        self.assertEqual(self.plan(PDF, file_id="i" * 256).file_id, "i" * 256)

    def test_bad_size_refused(self):
        for size in (None, 0, -1, True, False, "2048", 2048.0, [2048]):
            with self.subTest(size=size):
                self.assertEqual(self.plan(PDF, file_size=size), tf.Refusal.SIZE_INVALID)
        self.assertEqual(self.plan(PDF, file_size=10**30), tf.Refusal.SIZE_OVER_CAP)

    def test_unsafe_name_dropped_never_used(self):
        for name in ("..\\evil.pdf", "a..pdf", "../../etc/passwd", "dir/x.pdf", "dir\\x.pdf",
                     "x.pdf:ads", "C:x.pdf", "REPORT~1.PDF", "CON", "nul.pdf", "Com1.pdf",
                     "lpt\u00b9.pdf", "x.pdf.", "x.pdf ", "a\uff0f.pdf", "a\uff3c.pdf", "a\uff1a.pdf",
                     "x\x00.pdf", "x\u202egpj.pdf", "", "a" * 256, 5, None, ["x.pdf"]):
            with self.subTest(name=repr(name)[:40]):
                plan = self.plan(PDF, file_name=name)
                self.assertIsInstance(plan, tf.DownloadPlan)
                self.assertIsNone(plan.name_hint)
                self.assertEqual((plan.kind, plan.extension), ("pdf", ".pdf"))

    def test_mime_outside_allowlist_or_disagreeing_refused(self):
        for mime in (None, "", "application/x-msdownload", "text/html", "image/svg+xml",
                     "audio/ogg", "application/pdf\x00", 7, ["application/pdf"]):
            with self.subTest(mime=mime):
                self.assertEqual(self.plan(PDF, mime_type=mime), tf.Refusal.MIME_NOT_ALLOWED)
        self.assertEqual(self.plan(VOICE, mime_type="application/pdf"),
                         tf.Refusal.MIME_NOT_ALLOWED)
        for name in ("report.exe", "report.png", "report.pdf.exe", "photo.jpg", "x.docm"):
            with self.subTest(name=name):
                self.assertEqual(self.plan(PDF, file_name=name), tf.Refusal.MIME_NAME_MISMATCH)
        self.assertEqual(self.plan(VOICE, file_name="note.opus"), tf.Refusal.MIME_NAME_MISMATCH)

    def test_bad_file_id_refused(self):
        for file_id in (None, "", "i" * 257, "abc\n", "a\x00b", "a\x1bb", "a\u200bb",
                        "\ud800", 123, b"abc", ["abc"]):
            with self.subTest(file_id=repr(file_id)[:40]):
                self.assertEqual(self.plan(PDF, file_id=file_id), tf.Refusal.FILE_ID_INVALID)

    def test_bad_voice_duration_refused(self):
        for duration in (None, -1, True, "12", 12.0, float("nan"), float("inf"), [12]):
            with self.subTest(duration=duration):
                self.assertEqual(self.plan(VOICE, duration=duration),
                                 tf.Refusal.DURATION_INVALID)
        self.assertEqual(self.plan(VOICE, duration=tf.DEFAULT_MAX_VOICE_SECONDS + 1),
                         tf.Refusal.DURATION_OVER_CAP)

    def test_hostile_inputs_fail_closed(self):
        class Recursive(dict):
            def get(self, *args):
                return self.get(*args)

        class Exploding(dict):
            def get(self, *args):
                raise KeyError("boom")

        nested = current = {}
        for _ in range(100_000):
            current["file_id"] = current = {}
        for descriptor in (None, "x", b"x", 3, [PDF], Recursive(PDF), Exploding(PDF)):
            with self.subTest(descriptor=type(descriptor).__name__):
                self.assertEqual(tf.plan_download(descriptor), tf.Refusal.INVALID_DESCRIPTOR)
        self.assertEqual(tf.plan_download(nested), tf.Refusal.FILE_ID_INVALID)
        with patch.object(tf, "_plan", side_effect=RecursionError):
            self.assertEqual(tf.plan_download(PDF), tf.Refusal.INVALID_DESCRIPTOR)

    def test_deterministic(self):
        self.assertEqual(self.plan(PDF), self.plan(PDF))
        self.assertEqual(self.plan(VOICE), self.plan(VOICE))

    def test_refusal_texts_ascii_under_cp1252(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        with patch.object(sys, "stdout", stream):
            for refusal in tf.Refusal:
                self.assertTrue(refusal.text.isascii() and refusal.text.isprintable())
                self.assertTrue(refusal.value.isascii())
                print(refusal.value, refusal.text)
            sys.stdout.flush()

    def test_module_has_no_io_or_network_imports(self):
        tree = ast.parse(Path(tf.__file__).read_text(encoding="utf-8"))
        forbidden = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "shutil",
                     "tempfile", "requests", "ssl", "asyncio", "sqlite3", "importlib"}
        imports, names = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Name):
                names.add(node.id)
        self.assertFalse(imports & forbidden)
        self.assertFalse(names & {"open", "__import__", "eval", "exec"})

    # The only sanctioned importer, by exact package-relative path; test_telegram_client
    # guards that nothing else in jarvis/ or scripts/ names telegram_client.
    SANCTIONED = ("telegram_client.py",)

    @classmethod
    def _users(cls, package):
        return [path.relative_to(package).as_posix() for path in package.rglob("*.py")
                if path.relative_to(package).as_posix() not in ("telegram_files.py",) + cls.SANCTIONED
                and "telegram_files" in path.read_text(encoding="utf-8", errors="replace")]

    def test_running_app_does_not_import_module(self):
        self.assertEqual(self.SANCTIONED, ("telegram_client.py",))
        self.assertEqual(self._users(Path(tf.__file__).parent), [])

    def test_import_guard_still_flags_any_other_importer(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "sub").mkdir()
            line = "from jarvis.telegram_files import DownloadPlan\n"
            for name in ("telegram_files.py", "telegram_client.py", "other.py", "sub/telegram_client.py"):
                (package / name).write_text(line, encoding="utf-8")
            self.assertEqual(sorted(self._users(package)), ["other.py", "sub/telegram_client.py"])
