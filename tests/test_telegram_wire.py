import ast
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from jarvis import telegram_wire as wire


class TelegramWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).with_name("fixtures") / "telegram" / "updates.json"
        cls.updates = json.loads(fixture.read_text(encoding="utf-8"))

    def parse(self, update, allowed=frozenset({-42})):
        return wire.parse_update(json.dumps(update).encode(), allowed)

    def test_supported_updates(self):
        envelope = (-42, 7, 123)
        expected = {
            "text": wire.TextMessage(*envelope, "hello"),
            "voice": wire.VoiceNote(*envelope, "voice", 3, "audio/ogg"),
            "photo": wire.Photo(*envelope, "large", 2048, None, "photo"),
            "document": wire.Document(*envelope, "doc", 17, "application/pdf", "document"),
            "callback": wire.ButtonCallback(*envelope, "query", "button"),
        }
        for name, record in expected.items():
            with self.subTest(name=name):
                result = self.parse(self.updates[name])
                self.assertEqual(result, record)
                self.assertIs(result.trusted, False)

    def test_optional_media_metadata(self):
        message = dict(self.updates["document"]["message"], document={"file_id": "doc"})
        message.pop("caption")
        self.assertEqual(
            self.parse({"message": message}),
            wire.Document(-42, 7, 123, "doc", None, None, None),
        )

    def test_unknown_and_authority_fields_are_dropped(self):
        extras = dict(mode="execute", id=42, state="active", approval=True, trusted=True)
        message = dict(self.updates["text"]["message"], **extras)
        message["chat"] = {"id": -42, "approval": True, "mode": "execute"}
        record = self.parse(dict(message=message, **extras))
        self.assertEqual(record, wire.TextMessage(-42, 7, 123, "hello"))
        for name in extras.keys() - {"trusted"}:
            self.assertFalse(hasattr(record, name))
        self.assertIs(record.trusted, False)

    def test_sender_allowlist(self):
        self.assertTrue(wire.is_allowed_chat(-42, {-42}))
        self.assertFalse(wire.is_allowed_chat(True, {1}))
        self.assertFalse(wire.is_allowed_chat(1, {True}))
        self.assertFalse(wire.is_allowed_chat(42, {42.0}))
        for name, update in self.updates.items():
            for allowed in (set(), {42}):
                with self.subTest(name=name, allowed=allowed):
                    self.assertEqual(self.parse(update, allowed), wire.Refusal.CHAT_NOT_ALLOWED)

    def test_oversize_is_refused_before_parsing(self):
        with patch.object(wire.json, "loads", side_effect=AssertionError("parsed oversize")):
            self.assertEqual(
                wire.parse_update(b" " * (256 * 1024 + 1), {-42}),
                wire.Refusal.TOO_LARGE,
            )

    def test_invalid_json(self):
        for raw in (b"{", b"[]", b"null", b"99", b"\xff", b"[" * 2000,
                    b'{"message": {}, "message": {}}', b'{"extra": NaN}'):
            with self.subTest(raw=raw[:32]):
                self.assertEqual(wire.parse_update(raw, {-42}), wire.Refusal.INVALID_JSON)

    def test_invalid_and_unsupported_update_shapes(self):
        message = self.updates["text"]["message"]
        for update in (
            {"message": dict(message, message_id=True)},
            {"message": dict(message, text="\ud800")},
            {"message": dict(message, voice={"file_id": "voice", "duration": 3})},
            {"message": dict(message, chat={"id": "-42"})},
            {"message": message, "callback_query": {}},
            {"callback_query": {"id": "inline", "data": "button"}},
            {"edited_message": message},
        ):
            with self.subTest(update=update):
                self.assertEqual(self.parse(update), wire.Refusal.INVALID_UPDATE)

    def test_outbound_text_is_inert_and_bounded(self):
        result = wire.format_notification(
            "task_done", title="/open", body="https://example.com ／run @bot www.x.test",
        )
        self.assertEqual(result, "task done\nopen\nhttps example com run bot www x test")
        long_result = wire.format_notification("done", body="𐐀" * 4096)
        self.assertLessEqual(len(long_result.encode("utf-16-le")) // 2, 4096)
        self.assertTrue(long_result.startswith("done\n"))

    def test_wire_has_no_network_or_process_imports(self):
        tree = ast.parse(Path(wire.__file__).read_text(encoding="utf-8"))
        forbidden = {"socket", "http", "urllib", "requests", "subprocess"}
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertFalse(imports & forbidden)

    def test_core_does_not_import_wire(self):
        run = subprocess.run(
            [sys.executable, "-c",
             "import sys; import jarvis.core; "
             "assert 'jarvis.telegram_wire' not in sys.modules"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
