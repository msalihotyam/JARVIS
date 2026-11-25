import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from jarvis import telegram_media as tm
from tests import adversarial

R = tm.Refusal
SMALL = {"file_id": "AgAC-small", "file_unique_id": "u1", "width": 90, "height": 60,
         "file_size": 1_000}
LARGE = {"file_id": "AgAC-large", "file_unique_id": "u2", "width": 1280, "height": 853,
         "file_size": 200_000}
PDF = {"file_id": "BQACAgQAAx0", "file_size": 2048, "mime_type": "application/pdf",
       "file_name": "report.pdf", "file_unique_id": "u3"}
VOICE = {"file_id": "AwACAgQAAx0", "file_size": 4096, "duration": 12, "mime_type": "audio/ogg"}


def msg(**fields):
    return {"message_id": 7, "date": 1, "chat": {"id": 1}, "from": {"id": 2}, **fields}


class AcceptanceTests(unittest.TestCase):
    def test_photo_largest_size_within_cap(self):
        (photo,) = tm.describe(msg(photo=[SMALL, LARGE]))
        self.assertEqual((photo.kind, photo.file.file_id, photo.width, photo.height),
                         ("photo", "AgAC-large", 1280, 853))
        self.assertEqual(photo.file.file_size, 200_000)
        (photo,) = tm.describe(msg(photo=[SMALL, LARGE]), max_photo_bytes=199_999)
        self.assertEqual(photo.file.file_id, "AgAC-small")
        self.assertIs(photo.trusted, False)

    def test_document_voice_audio_records(self):
        (doc,) = tm.describe(msg(document=PDF))
        self.assertEqual(doc.kind, "document")
        self.assertEqual(doc.file, tm.TgFile("BQACAgQAAx0", 2048, "application/pdf",
                                             "report.pdf", None))
        self.assertEqual(doc.file.mapping(), {"file_id": "BQACAgQAAx0", "file_size": 2048,
                                              "mime_type": "application/pdf",
                                              "file_name": "report.pdf"})
        (voice,) = tm.describe(msg(voice=VOICE))
        self.assertEqual((voice.kind, voice.file.duration, voice.file.mime_type),
                         ("voice", 12, "audio/ogg"))
        (audio,) = tm.describe(msg(audio={**VOICE, "file_name": "song.mp3"}))
        self.assertEqual((audio.kind, audio.file.file_name), ("audio", "song.mp3"))

    def test_caption_marked(self):
        photo, caption = tm.describe(msg(photo=[SMALL], caption="look\nhere"))
        self.assertEqual((caption.kind, caption.text, caption.not_salihs_words),
                         ("caption", "look\nhere", False))
        self.assertEqual(tm.describe(msg(text="plain words")), ())

    def test_forward_and_quote_flagged(self):
        for key, value in (("forward_origin", {"type": "user"}), ("forward_from", {"id": 3}),
                           ("forward_date", 5), ("is_automatic_forward", True)):
            with self.subTest(key=key):
                (forward,) = tm.describe(msg(text="hello", **{key: value}))
                self.assertEqual((forward.kind, forward.text, forward.not_salihs_words),
                                 ("forward", "hello", True))
        out = tm.describe(msg(document=PDF, caption="c", forward_origin={}))
        self.assertTrue(all(item.not_salihs_words for item in out))
        (quote,) = tm.describe(msg(text="mine", quote={"text": "theirs", "position": 0}))
        self.assertEqual((quote.kind, quote.text, quote.not_salihs_words),
                         ("quote", "theirs", True))

    def test_album_key_carried(self):
        (photo,) = tm.describe(msg(photo=[SMALL], media_group_id="13579"))
        (doc,) = tm.describe(msg(document=PDF, media_group_id="13579"))
        self.assertEqual((photo.album_key, doc.album_key), ("13579", "13579"))
        self.assertIsNone(tm.describe(msg(document=PDF))[0].album_key)

    def test_edit_is_marker_only(self):
        edit, photo, caption = tm.describe(msg(edit_date=9, photo=[SMALL], caption="new"))
        self.assertEqual((edit.kind, edit.edit_of, edit.file, edit.text), ("edit", 7, None, None))
        self.assertEqual((photo.kind, caption.text), ("photo", "new"))
        self.assertEqual(tm.describe(msg(edit_date=9, text="fixed")),
                         (tm.Descriptor("edit", edit_of=7),))
        for bad in adversarial.HOSTILE_NUMBERS + (0,):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertEqual(tm.describe(msg(edit_date=bad, text="x")), R.EDIT_INVALID)

    def test_unsupported_fixed_list(self):
        self.assertEqual(tm.UNSUPPORTED_TYPES, ("sticker", "location", "contact", "poll",
                                                "video", "video_note", "animation", "dice"))
        for name in tm.UNSUPPORTED_TYPES:
            with self.subTest(name=name):
                (item,) = tm.describe(msg(**{name: {"secret": "dropped"}}))
                self.assertEqual((item.kind, item.type_name, item.file), ("unsupported", name, None))


class NegativeTests(unittest.TestCase):
    def test_bad_photo_refused(self):
        self.assertEqual(tm.describe(msg(photo=[])), R.PHOTO_INVALID)
        self.assertEqual(tm.describe(msg(photo=[SMALL, LARGE]), max_photo_bytes=999),
                         R.PHOTO_OVER_CAP)
        for field in ("width", "height", "file_size"):
            for bad in adversarial.HOSTILE_NUMBERS + (1.0, 0, -1):
                with self.subTest(field=field, bad=repr(bad)[:20]):
                    self.assertEqual(tm.describe(msg(photo=[{**SMALL, field: bad}])),
                                     R.PHOTO_INVALID)
        for bad in (None, "x", {"0": SMALL}, [SMALL] * 17, [None], [[SMALL]]):
            with self.subTest(photo=repr(bad)[:20]):
                self.assertEqual(tm.describe(msg(photo=bad)), R.PHOTO_INVALID)
        for limit in adversarial.HOSTILE_NUMBERS + (0, 1.5):
            with self.subTest(limit=repr(limit)[:20]):
                self.assertEqual(tm.describe(msg(photo=[SMALL]), max_photo_bytes=limit),
                                 R.INVALID_LIMIT)

    def test_conflicting_fields_ambiguous(self):
        for fields in ({"photo": [SMALL], "document": PDF}, {"voice": VOICE, "audio": VOICE},
                       {"document": PDF, "sticker": {}}, {"text": "a", "caption": "b"},
                       {"text": "a", "photo": [SMALL]}, {"caption": "lonely"},
                       {"media_group_id": "1", "text": "a"}):
            with self.subTest(fields=sorted(fields)):
                self.assertEqual(tm.describe(msg(**fields)), R.AMBIGUOUS)

    def test_bad_file_id_refused(self):
        for bad in (None, "", "i" * 257, "a\nb", "a\x00b", "a\x1bb", "a​b", "\ud800", 5):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertEqual(tm.describe(msg(document={**PDF, "file_id": bad})),
                                 R.FILE_ID_INVALID)
                self.assertEqual(tm.describe(msg(photo=[{**SMALL, "file_id": bad}])),
                                 R.FILE_ID_INVALID)
        self.assertEqual(tm.describe(msg(document={**PDF, "file_id": "i" * 256}))[0]
                         .file.file_id, "i" * 256)

    def test_unknown_fields_dropped(self):
        (doc,) = tm.describe(msg(document={**PDF, "thumbnail": {"x": 1}}, reply_markup={},
                                 web_app_data={"data": "onayla"}, via_bot={"id": 1}))
        self.assertEqual(doc.file.mapping().keys(), {"file_id", "file_size", "mime_type",
                                                     "file_name"})
        self.assertEqual(tm.describe(msg(web_app_data={"data": "onayla"})), ())

    def test_long_caption_refused_not_cut(self):
        ok = tm.describe(msg(photo=[SMALL], caption="c" * tm.MAX_CAPTION_CHARS))
        self.assertEqual(len(ok[1].text), tm.MAX_CAPTION_CHARS)
        self.assertEqual(tm.describe(msg(photo=[SMALL], caption="c" * 1025)),
                         R.CAPTION_OVER_CAP)
        self.assertEqual(tm.describe(msg(text="t" * 4097, forward_date=1)), R.TEXT_OVER_CAP)

    def test_hostile_text_refused_or_kept_exactly(self):
        def check(text):
            out = tm.describe(msg(photo=[SMALL], caption=text))
            self.assertTrue(isinstance(out, R) or out[1].text == text)
        adversarial.each(self, adversarial.HOSTILE_TEXT, check)
        self.assertEqual(tm.describe(msg(photo=[SMALL], caption="a\x00b")), R.TEXT_INVALID)

    def test_forwarded_onayla_is_plain_data(self):
        out = tm.describe(msg(text="onayla", forward_origin={"type": "hidden_user"}))
        self.assertEqual(out, (tm.Descriptor("forward", text="onayla", not_salihs_words=True),))
        self.assertIs(out[0].trusted, False)

    def test_hostile_inputs_fail_closed(self):
        class Recursive(dict):
            def __contains__(self, key):
                return key in self

        class Exploding(dict):
            def get(self, *args):
                raise KeyError("boom")

        for message in (None, "x", b"x", 3, [msg()], Recursive(msg()), adversarial.deep_json()):
            with self.subTest(message=type(message).__name__):
                self.assertEqual(tm.describe(message), R.INVALID_MESSAGE)
        self.assertEqual(tm.describe(msg(document=Exploding(PDF))), R.INVALID_MESSAGE)
        for bad in adversarial.HOSTILE_NUMBERS + (0,):
            with self.subTest(message_id=repr(bad)[:20]):
                self.assertEqual(tm.describe({**msg(), "message_id": bad}),
                                 R.MESSAGE_ID_INVALID)
        with patch.object(tm, "_describe", side_effect=RecursionError):
            self.assertEqual(tm.describe(msg()), R.INVALID_MESSAGE)


class ModuleTests(unittest.TestCase):
    def test_deterministic(self):
        message = msg(photo=[SMALL, LARGE], caption="c", media_group_id="1", forward_date=1)
        self.assertEqual(tm.describe(message), tm.describe(message))

    def test_refusal_texts_ascii(self):
        with adversarial.narrow_stdout() as stream:
            for refusal in R:
                self.assertTrue(refusal.text.isascii() and refusal.text.isprintable())
                self.assertTrue(refusal.value.isascii())
                print(refusal.value, refusal.text)
            stream.flush()

    def test_module_has_no_io_or_network_imports(self):
        tree = ast.parse(Path(tm.__file__).read_text(encoding="utf-8"))
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
        self.assertFalse(names & {"open", "__import__", "eval", "exec", "telegram_files"})

    def test_running_app_does_not_import_module(self):
        package = Path(tm.__file__).parent
        users = [path.name for path in package.rglob("*.py")
                 if path.name != "telegram_media.py"
                 and "telegram_media" in path.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(users, [])
