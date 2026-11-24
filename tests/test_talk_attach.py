import io
import stat
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from jarvis.talk_attach import AttachmentRefused, intake_attachment


def office_zip(*names):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"")
    return buffer.getvalue()


PNG = b"\x89PNG\r\n\x1a\ncontent"
PDF = b"%PDF-1.7\n"


class AttachmentIntakeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def assert_refused(self, reason, name, content, **kwargs):
        with self.assertRaises(AttachmentRefused) as caught:
            intake_attachment(kwargs.pop("root", self.root), name, content, **kwargs)
        self.assertEqual(caught.exception.code, reason)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_allowlisted_content_and_record(self):
        formats = {
            "png": PNG,
            "jpg": b"\xff\xd8\xff\xe0",
            "webp": b"RIFF\x04\x00\x00\x00WEBP",
            "pdf": PDF,
            "txt": b"\xef\xbb\xbfhello",
            "md": b"# Heading",
            "csv": b"a,b\n1,2\n",
            "json": b'{"x": 1}',
            "xlsx": office_zip("[Content_Types].xml", "xl/workbook.xml"),
            "docx": office_zip("[Content_Types].xml", "word/document.xml"),
        }
        for kind, content in formats.items():
            with self.subTest(kind=kind), patch.object(ZipFile, "open", side_effect=AssertionError):
                name = f"attached.{kind}"
                record = intake_attachment(self.root, name, content)
                digest = sha256(content).hexdigest()
                self.assertEqual(record.sha256, digest)
                self.assertEqual(record.bytes, len(content))
                self.assertEqual(record.kind, kind)
                self.assertEqual(record.display_name, name)
                self.assertEqual(record.relative_path, f"{digest}.{kind}")
                self.assertEqual((self.root / record.relative_path).read_bytes(), content)

    def test_same_attachment_deduplicates(self):
        first = intake_attachment(self.root, "attached.PNG", PNG)
        with patch("jarvis.talk_attach.os.replace", side_effect=AssertionError):
            second = intake_attachment(self.root, "attached.PNG", PNG)
        self.assertEqual(first, second)
        self.assertEqual(list(self.root.iterdir()), [self.root / first.relative_path])

    def test_display_name_and_path(self):
        for name in (r"..\..\x.png", r"C:\Windows\x.png", "/tmp/x.png",
                     "folder/" + "a" * 140 + "\u202e\x01.png"):
            with self.subTest(name=name):
                record = intake_attachment(self.root, name, PNG)
                self.assertEqual((self.root / record.relative_path).resolve().parent, self.root.resolve())
                self.assertFalse(set(record.display_name) & set("/\\\u202e\x01"))
                self.assertLessEqual(len(record.display_name), 120)
                self.assertTrue(record.display_name.endswith(".png"))

    def test_signature_mismatches(self):
        for name, content in (("wrong.png", PDF), ("wrong.txt", PDF),
                              ("wrong.xlsx", office_zip("[Content_Types].xml", "word/document.xml"))):
            with self.subTest(name=name):
                self.assert_refused("kind_mismatch", name, content)

    def test_denied_extensions_and_invalid_office_archives(self):
        plain_zip = office_zip("readme.txt")
        for ext in ("exe", "ps1", "bat", "cmd", "lnk", "js", "zip"):
            with self.subTest(ext=ext):
                self.assert_refused("unsupported_extension", f"file.{ext}", plain_zip)
        nul_part = office_zip("[Content_Types].xml", "word/document.xml!suffix").replace(
            b"word/document.xml!suffix", b"word/document.xml\x00suffix",
        )
        backslash_part = office_zip("[Content_Types].xml", "word/document.xml").replace(
            b"word/document.xml", br"word\document.xml",
        )
        for content in (plain_zip, b"PK\x03\x04truncated", office_zip("word/document.xml"),
                        nul_part, backslash_part,
                        office_zip("[Content_Types].xml", "word/document.xml", "xl/workbook.xml")):
            self.assert_refused("invalid_content", "file.docx", content)

    def test_invalid_text(self):
        for content in (b"hi\x00", b"hi\xff"):
            self.assert_refused("invalid_content", "file.txt", content)

    def test_empty_and_size_cap(self):
        self.assert_refused("empty", "file.png", b"")
        self.assert_refused("too_large", "file.png", PNG, max_bytes=len(PNG) - 1)
        intake_attachment(self.root, "file.png", PNG, max_bytes=len(PNG))

    def test_invalid_roots(self):
        missing = self.root / "missing"
        self.assert_refused("invalid_root", "file.png", PNG, root=missing)
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "not-a-directory"
            file.touch()
            self.assert_refused("invalid_root", "file.png", PNG, root=file)
        # Simulate lstat so this case also runs where symlink creation is denied.
        with patch.object(Path, "lstat") as lstat:
            lstat.return_value.st_mode = stat.S_IFLNK
            lstat.return_value.st_file_attributes = 0
            self.assert_refused("invalid_root", "file.png", PNG)

    def test_failed_publication_cleans_up(self):
        with patch("jarvis.talk_attach.os.replace", side_effect=OSError):
            self.assert_refused("storage_error", "file.png", PNG)

    def test_existing_hash_path_must_contain_matching_bytes(self):
        destination = self.root / f"{sha256(PNG).hexdigest()}.png"
        destination.write_bytes(b"corrupt")
        with self.assertRaises(AttachmentRefused) as caught:
            intake_attachment(self.root, "attached.png", PNG)
        self.assertEqual(caught.exception.code, "storage_conflict")
        self.assertEqual(destination.read_bytes(), b"corrupt")
