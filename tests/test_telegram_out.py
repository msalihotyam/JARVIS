"""TGSEND1: jarvis.telegram_out builds Bot API bodies without network, fail closed."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import unittest
from collections.abc import Mapping
from pathlib import Path

from jarvis import telegram_out as tg
from jarvis.telegram_out import Refusal, Request, build_request

CHAT = 424242
SHA = "a" * 64
SOURCE = Path(tg.__file__).read_text(encoding="utf-8")


def text_action(**extra):
    action = {"kind": "text", "chat_id": CHAT, "text": "hello"}
    action.update(extra)
    return action


def media_action(kind, path="files/ab/voice.ogg", **extra):
    action = {"kind": kind, "chat_id": CHAT, "file": {"sha256": SHA, "path": path}}
    action.update(extra)
    return action


class _Hostile(Mapping):
    def __getitem__(self, key):
        raise RecursionError("deep")

    def __contains__(self, key):
        raise RecursionError("deep")

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


class _SneakyInt(int):
    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = int.__hash__


class TelegramOutTests(unittest.TestCase):
    def refused(self, result, reason=None):
        self.assertIsInstance(result, Refusal, msg=repr(result)[:80])
        if reason is not None:
            self.assertEqual(result.reason, reason)

    # --- acceptance ---------------------------------------------------------

    def test_text_message_body(self):
        req = build_request(text_action(), CHAT)
        self.assertIsInstance(req, Request)
        self.assertEqual(req.method, "sendMessage")
        self.assertEqual(req.body, {
            "chat_id": CHAT,
            "disable_notification": False,
            "text": "hello",
            "link_preview_options": {"is_disabled": True},
        })
        self.assertNotIn("parse_mode", req.body)
        self.assertEqual(req.stripped, ())

    def test_silent_sets_disable_notification(self):
        req = build_request(text_action(silent=True), CHAT)
        self.assertIs(req.body["disable_notification"], True)
        self.refused(build_request(text_action(silent="yes"), CHAT), "bad_silent_type")

    def test_media_methods_carry_stored_file_record(self):
        for kind, method in (("voice", "sendVoice"), ("photo", "sendPhoto"),
                             ("document", "sendDocument")):
            req = build_request(media_action(kind, silent=True), CHAT)
            self.assertEqual(req.method, method)
            self.assertEqual(req.body, {
                "chat_id": CHAT,
                "disable_notification": True,
                kind: {"sha256": SHA, "path": "files/ab/voice.ogg"},
            })

    def test_media_backslash_path_normalised_and_bad_sha_refused(self):
        req = build_request(media_action("document", path="files\\ab\\x.pdf"), CHAT)
        self.assertEqual(req.body["document"]["path"], "files/ab/x.pdf")
        for sha in ("A" * 64, "a" * 63, "g" * 64, 5):
            action = media_action("photo")
            action["file"]["sha256"] = sha
            self.refused(build_request(action, CHAT), "bad_sha256")
        self.refused(build_request(media_action("photo", file=[1]), CHAT), "bad_file_record")
        self.refused(build_request({"kind": "voice", "chat_id": CHAT}, CHAT), "missing_file")

    def test_approval_button_callback_is_card_id_only(self):
        action = text_action(kind="approval", card_id="card-7f3a")
        req = build_request(action, CHAT)
        self.assertEqual(req.method, "sendMessage")
        self.assertEqual(req.body["reply_markup"], {
            "inline_keyboard": [[{"text": tg.APPROVAL_BUTTON, "callback_data": "card-7f3a"}]],
        })
        self.refused(build_request(text_action(kind="approval"), CHAT), "missing_card_id")

    def test_method_allowlist_is_exactly_four(self):
        self.assertEqual(tg.ALLOWED_METHODS,
                         {"sendMessage", "sendVoice", "sendPhoto", "sendDocument"})
        self.assertLessEqual(set(tg.KIND_METHOD.values()), tg.ALLOWED_METHODS)
        req = build_request(text_action(method="sendMessage"), CHAT)
        self.assertEqual(req.method, "sendMessage")

    def test_no_token_or_url_in_output(self):
        action = text_action(token="123456789:AAH-secret", url="https://api.telegram.org/x",
                             parse_mode="HTML")
        req = build_request(action, CHAT)
        dumped = json.dumps([req.method, req.body])
        for needle in ("token", "secret", "http", "telegram.org", "parse_mode", "HTML"):
            self.assertNotIn(needle, dumped)
        token = "123456789:" + "A" * 35
        for bad in (text_action(text=f"key {token}"),
                    text_action(text="see https://x.example"),
                    text_action(text="www.example.com"),
                    text_action(text="api.telegram.org/bot1/sendMessage"),
                    text_action(kind="approval", card_id="https://a.b")):
            self.refused(build_request(bad, CHAT), "token_or_url_in_output")

    def test_deterministic(self):
        action = text_action(kind="approval", card_id="c1", text="x\u202ey", silent=True)
        self.assertEqual(build_request(action, CHAT), build_request(dict(action), CHAT))

    # --- negatives ----------------------------------------------------------

    def test_chat_id_mismatch_and_types_refused(self):
        self.refused(build_request(text_action(chat_id=CHAT + 1), CHAT), "chat_id_mismatch")
        for bad in (True, str(CHAT), float(CHAT), None, _SneakyInt(1)):
            self.refused(build_request(text_action(chat_id=bad), CHAT), "bad_chat_id_type")
        for bad in (True, str(CHAT), None, _SneakyInt(CHAT)):
            self.refused(build_request(text_action(), bad), "bad_configured_chat_id")
        action = text_action()
        del action["chat_id"]
        self.refused(build_request(action, CHAT), "missing_chat_id")

    def test_other_methods_refused(self):
        for method in ("deleteMessage", "forwardMessage", "setWebhook", "getUpdates",
                       "SENDMESSAGE", "", None, ["sendMessage"]):
            self.refused(build_request(text_action(method=method), CHAT), "method_not_allowed")
        self.refused(build_request(text_action(method="sendPhoto"), CHAT),
                     "method_kind_mismatch")
        for kind in ("delete", "forward", "webhook", None, ""):
            self.refused(build_request(text_action(kind=kind), CHAT), "unknown_kind")

    def test_text_length_limit(self):
        self.assertIsInstance(build_request(text_action(text="a" * 4096), CHAT), Request)
        self.refused(build_request(text_action(text="a" * 4097), CHAT), "text_too_long")
        # Astral chars count as two UTF-16 units, as Telegram counts them.
        self.refused(build_request(text_action(text="\U0001F600" * 2049), CHAT), "text_too_long")
        for bad in ("", "  \n", "\u200b\u202e", 7, None):
            self.refused(build_request(text_action(text=bad), CHAT))

    def test_callback_data_rules(self):
        ok = "c" * 64
        req = build_request(text_action(kind="approval", card_id=ok), CHAT)
        self.assertEqual(req.body["reply_markup"]["inline_keyboard"][0][0]["callback_data"], ok)
        self.refused(build_request(text_action(kind="approval", card_id="c" * 65), CHAT),
                     "card_id_too_long")
        self.refused(build_request(text_action(kind="approval", card_id="\u00e9" * 33), CHAT),
                     "card_id_too_long")
        for bad in ("", "card 1", "card\u202e1", "caf\u00e9", "c\u200b1", "c\x001", "c\t1"):
            self.refused(build_request(text_action(kind="approval", card_id=bad), CHAT),
                         "bad_card_id")
        self.refused(build_request(text_action(kind="approval", card_id=12), CHAT),
                     "bad_card_id_type")

    def test_invisible_characters_stripped_and_reported(self):
        req = build_request(text_action(text="a\x00b\u202ec\u200bd\u2066e\ufefff\n\tg"), CHAT)
        self.assertEqual(req.body["text"], "abcdef\n\tg")
        self.assertEqual(req.stripped, ("U+0000", "U+200B", "U+202E", "U+2066", "U+FEFF"))

    def test_link_forms_in_text_refused(self):
        # TGSEND1FIX F1: one per variant Telegram clients auto-link or resolve.
        for text in ("mailto:x@y.z", "javascript:alert(1)", "JaVaScRiPt:x", "tel:+1",
                     "data:text/html,x", "vbscript:x", "tg:resolve", "http:\\\\x.y",
                     "http:/x.y", "https:\\x", "localhost:8080", "1mailto:x@y.z",
                     "-javascript:alert(1)", "_tel:+1", "go mailto:x", "t.me/joinchat/abc",
                     "evil.com/login", "evil.com", "x@evil.com",
                     "\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444",
                     "evil.c\u043em", "evil\u3002com", "evil\uff0ecom", "evil\uff61com",
                     "\uff48\uff54\uff54\uff50\uff53\uff1a\uff0f\uff0fx",
                     "\uff4d\uff41\uff49\uff4c\uff54\uff4f\uff1ax", "ht\u200btps://x.y",
                     "evil\u034f.com", "1.2.3.4/x", "1http://x"):
            with self.subTest(text=text):
                self.refused(build_request(text_action(text=text), CHAT),
                             "token_or_url_in_output")

    def test_marked_idn_links_refused(self):
        # Review hits: combining marks or U+FE0F right before the dot or in the TLD.
        for text in ("\u0939\u093f\u0902\u0926\u0940.com/login", "\u0e17\u0e35\u0e48.com",
                     "evil\ufe0f.com", "x.\u092d\u093e\u0930\u0924"):
            with self.subTest(text=text):
                self.refused(build_request(text_action(text=text), CHAT),
                             "token_or_url_in_output")

    def test_str_subclasses_refused(self):
        class Hostile(str):
            def replace(self, *args):
                return "C:/Windows/System32/x.dll"

        self.refused(build_request(text_action(text=Hostile("hi")), CHAT), "bad_text_type")
        self.refused(build_request(text_action(kind="approval", card_id=Hostile("c1")), CHAT),
                     "bad_card_id_type")
        self.refused(build_request(media_action("document", path=Hostile("a/b.txt")), CHAT),
                     "bad_path_type")

    def test_link_forms_in_card_id_refused(self):
        for card_id in ("mailto:x@y.z", "javascript:x", "t.me/x", "a.bc", "http:\\\\x",
                        "1.2.3.4"):
            with self.subTest(card_id=card_id):
                self.refused(build_request(text_action(kind="approval", card_id=card_id),
                                           CHAT), "token_or_url_in_output")

    def test_times_and_prose_still_pass(self):
        for text in ("15:00", "at 10:30 ok", "Note: x", "e.g. x", "wait...ok", "v1.2",
                     "@someone #tag +905551234567"):
            with self.subTest(text=text):
                self.assertEqual(build_request(text_action(text=text), CHAT).body["text"], text)

    def test_default_ignorable_stripped_per_char(self):
        # TGSEND1FIX F2: Default_Ignorable code points outside Cc/Cf/Cs.
        for code in (0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0xFE00, 0xE0100, 0x2065,
                     0x17B4, 0x180B, 0xFFF0):
            with self.subTest(code=hex(code)):
                req = build_request(text_action(text=f"a{chr(code)}b"), CHAT)
                self.assertEqual(req.body["text"], "ab")
                self.assertEqual(req.stripped, (f"U+{code:04X}",))
        heart = build_request(text_action(text="\u2764\ufe0f"), CHAT)  # U+FE0F kept: emoji
        self.assertEqual((heart.body["text"], heart.stripped), ("\u2764\ufe0f", ()))
        self.refused(build_request(text_action(text="\u034f\u3164\uffa0"), CHAT), "empty_text")

    def test_windows_invalid_path_chars_refused(self):
        # TGSEND1FIX F3: * ? " < > | in any component.
        for ch in '*?"<>|':
            for path in (f"a{ch}b.txt", f"files/ab/x{ch}.ogg"):
                with self.subTest(path=path):
                    self.refused(build_request(media_action("document", path=path), CHAT),
                                 "bad_path_chars")

    def test_bad_paths_refused(self):
        cases = {
            "a/../b": "path_traversal", "..": "path_traversal", "a//b": "path_traversal",
            "./a": "path_traversal", "a/": "path_traversal", "": "bad_path_chars",
            "/etc/x": "path_absolute", "\\x": "path_absolute",
            "\\\\srv\\share\\x": "path_absolute", "//srv/share/x": "path_absolute",
            "C:\\x": "path_colon", "C:x": "path_colon", "a.txt:stream": "path_colon",
            "a.txt::$DATA": "path_colon", "PROGRA~1/x": "path_short_name",
            "a/DOCUME~2.TXT": "path_short_name", "CON": "path_reserved_name",
            "nul.txt": "path_reserved_name", "con/x": "path_reserved_name",
            "a/LPT1": "path_reserved_name", "Com3.log": "path_reserved_name",
            "CON .txt": "path_reserved_name", "a./b": "path_trailing_dot_space",
            "a /b": "path_trailing_dot_space", "a\x00b": "bad_path_chars",
            "caf\u00e9.txt": "bad_path_chars", "x" * 241: "bad_path_chars",
        }
        for path, reason in cases.items():
            with self.subTest(path=path[:40]):
                self.refused(build_request(media_action("document", path=path), CHAT), reason)
        self.refused(build_request(media_action("document", path=b"a"), CHAT), "bad_path_type")
        for ok in ("a/b.txt", "console.log", "files/connect.ogg", "a~b"):
            self.assertIsInstance(build_request(media_action("document", path=ok), CHAT),
                                  Request)

    def test_wrong_types_and_deep_input_never_raise(self):
        deep: dict = {}
        node = deep
        for _ in range(10000):
            node["x"] = {}
            node = node["x"]
        for action in (None, [], "text", 5, deep, _Hostile(),
                       text_action(text=deep), media_action("voice", file=deep),
                       media_action("voice", file=_Hostile())):
            with self.subTest(action=type(action).__name__):
                self.refused(build_request(action, CHAT))
        self.refused(build_request(_Hostile(), CHAT), "invalid_input")

    # --- static guarantees ---------------------------------------------------

    def test_module_imports_no_io_or_network(self):
        tree = ast.parse(SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "__import__", "exec", "eval",
                                                "compile", "getattr"})
        self.assertEqual(imported, {"__future__", "json", "re", "unicodedata",
                                    "collections.abc", "dataclasses"})

    def test_refusals_ascii_and_cp1252_printable(self):
        reasons = set(re.findall(r'_Refused\("([^"]+)"\)', SOURCE))
        reasons |= {f"missing_{key}" for key in ("chat_id", "kind", "text", "card_id",
                                                  "file", "sha256", "path")}
        reasons.add("invalid_input")
        self.assertGreater(len(reasons), 20)
        stripped = build_request(text_action(text="a\u202e\x00b"), CHAT).stripped
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with contextlib.redirect_stdout(out):
            for reason in sorted(reasons):
                self.assertTrue(reason.isascii() and reason.isprintable())
                print(str(Refusal(reason)))
            print(" ".join(stripped))
        out.flush()
        self.assertIn(b"refused: chat_id_mismatch", raw.getvalue())


if __name__ == "__main__":
    unittest.main()
