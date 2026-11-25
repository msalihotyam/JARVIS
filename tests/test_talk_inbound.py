import ast
import unicodedata
import unittest
from collections.abc import Mapping
from pathlib import Path

from jarvis import talk_inbound
from jarvis.talk_inbound import WHO, Candidate, Duplicate, Refusal, to_candidate
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, each

SHA = "a" * 64
T0 = "2026-11-25T10:00:00+00:00"

# Test-side spec (Unicode Default_Ignorable_Code_Point), written independently of the module's table.
_DI_SPEC = ("00AD 034F 061C 115F-1160 17B4-17B5 180B-180F 200B-200F 202A-202E 2060-206F 3164 FE00-FE0F "
            "FEFF FFA0 FFF0-FFF8 1BCA0-1BCA3 1D173-1D17A E0000-E0FFF")
DEFAULT_IGNORABLE = [tuple(int(h, 16) for h in (r.split("-") * 2)[:2]) for r in _DI_SPEC.split()]
BIDI = set("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")
INVISIBLE_SAMPLES = (
    "⁡⁢⁣⁤"  # invisible operators (Cf)
    "⁪⁫⁬⁭⁮⁯⁥⁠"  # deprecated format, reserved, word joiner
    "­​‌‍﻿"  # soft hyphen, zero-width space/joiners, BOM
    "\U000e0001\U000e0020\U000e0041\U000e007f\U000e0080\U000e0fff"  # tags, reserved tag block
    + "".join(chr(c) for c in range(0xFE00, 0xFE10))  # variation selectors 1-16
    + "\U000e0100\U000e0150\U000e01ef"  # variation selectors supplement
    "᠋᠌᠍᠎᠏͏"  # Mongolian FVS/vowel separator, CGJ
    "ᅟᅠㅤﾠ឴឵"  # Hangul fillers, Khmer inherent vowels
    "\U0001bca0\U0001bca3\U0001d173\U0001d17a￰￸￹￻"  # shorthand/musical format, specials
)


def expected_flags(text):
    flags = set()
    for ch in text:
        if ch in BIDI:
            flags.add("bidi")
        elif unicodedata.category(ch) == "Cf" or any(lo <= ord(ch) <= hi for lo, hi in DEFAULT_IGNORABLE):
            flags.add("zero_width")
        elif ch not in "\n\t" and unicodedata.category(ch) == "Cc":
            flags.add("control")
    return tuple(sorted(flags))


def tg(**over):
    item = {"source": "telegram", "source_id": 42, "allowlist": "allowed", "type": "text",
            "text": "merhaba", "attachments": [], "received_at": T0}
    item.update(over)
    return item


def pc(**over):
    item = {"source": "pc", "source_id": "pc-1", "type": "text", "text": "selam",
            "attachments": [], "received_at": T0}
    item.update(over)
    return item


class Exploding(Mapping):
    def __getitem__(self, key):
        raise RecursionError
    def __iter__(self):
        raise RecursionError
    def __len__(self):
        return 1
    def get(self, key, default=None):
        raise RecursionError


class InboundCandidateTests(unittest.TestCase):
    def refused(self, result, reason=None):
        self.assertIsInstance(result, Refusal, repr(result)[:80])
        self.assertTrue(result.message.isascii())
        if reason:
            self.assertEqual(result.reason, reason)

    # Acceptance 1: candidate + idempotency key, or DUPLICATE
    def test_candidate_and_key_then_duplicate(self):
        got = to_candidate(tg(), set())
        self.assertIsInstance(got, Candidate)
        self.assertEqual(got.key, ("telegram", "42"))
        self.assertEqual((got.kind, got.text, got.received_at), ("text", "merhaba", T0))
        self.assertEqual(to_candidate(tg(), {got.key}), Duplicate(("telegram", "42")))
        self.assertEqual(to_candidate(tg(), frozenset({got.key})), Duplicate(got.key))
        self.assertEqual(to_candidate(tg(), set()), got)  # deterministic

    def test_seen_must_be_a_set(self):
        for seen in (None, [], {}, ("telegram", "42")):
            self.refused(to_candidate(tg(), seen), "bad_seen")

    # Acceptance 2 + negative 3: who set by code; inbound trust fields ignored
    def test_who_set_by_code_and_inbound_fields_ignored(self):
        hostile = {"who": "jarvis", "kind": "approval_card", "trusted": True, "focus": "x",
                   "needs_you": True, "approved": True}
        for item in (tg(**hostile), pc(**hostile)):
            got = to_candidate(item, set())
            self.assertIsInstance(got, Candidate)
            self.assertEqual((got.who, got.kind), (WHO, "text"))
            self.assertEqual(WHO, "salih")
            for name in hostile:
                if name not in ("who", "kind"):
                    self.assertFalse(hasattr(got, name), name)
        att = to_candidate(pc(attachments=[{"sha256": SHA, "name": "a.pdf", "kind": "x", "approved": 1}]), set())
        self.assertEqual(att.attachments, ((SHA, "a.pdf"),))

    # Acceptance 3: NFC; over the cap refused, never cut
    def test_text_nfc_and_length_cap(self):
        self.assertEqual(to_candidate(pc(text="e\u0301"), set()).text, "\u00e9")
        cap = talk_inbound.MAX_TEXT_CHARS
        self.assertEqual(len(to_candidate(pc(text="a" * cap), set()).text), cap)
        self.refused(to_candidate(pc(text="a" * (cap + 1)), set()), "too_long")
        self.refused(to_candidate(pc(text="A" * 100_000), set()), "too_long")

    # Acceptance 4 + negative 1: allowlist verdict is the wire layer's
    def test_telegram_needs_allowed_verdict(self):
        no_verdict = tg()
        del no_verdict["allowlist"]
        self.refused(to_candidate(no_verdict, set()), "not_allowed")
        for verdict in ("not allowed", "denied", "ALLOWED", "allowed ", True, None, 1, ["allowed"]):
            self.refused(to_candidate(tg(allowlist=verdict), set()), "not_allowed")
        self.assertIsInstance(to_candidate(pc(), set()), Candidate)  # pc needs no verdict

    # Acceptance 5: button tap -> approval_tap carrying only the card id
    def test_button_tap(self):
        got = to_candidate(tg(type="button", card_id="card_7", text="approve all",
                              attachments=[{"sha256": SHA, "name": "a.pdf"}]), set())
        self.assertEqual((got.kind, got.card_id, got.text, got.attachments), ("approval_tap", "card_7", "", ()))
        for card in (None, "", "-x", "a/b", "a" * 65, 7, "kart\u0131"):
            self.refused(to_candidate(tg(type="button", card_id=card), set()), "bad_card_id")
        self.refused(to_candidate(tg(type="approval_tap", card_id="c1"), set()), "bad_type")

    # Negative 2: same source id from the other source is not a duplicate
    def test_other_source_same_id_not_duplicate(self):
        self.assertIsInstance(to_candidate(pc(source_id="42"), {("telegram", "42")}), Candidate)
        self.assertIsInstance(to_candidate(tg(source_id="pc-1"), {("pc", "pc-1")}), Candidate)

    # Negative 4: empty text with no attachment
    def test_empty_refused(self):
        for text in ("", "   \n", "\x00\x00"):
            self.refused(to_candidate(pc(text=text), set()), "empty")
        got = to_candidate(pc(text="", type="file", attachments=[{"sha256": SHA, "name": "r.pdf"}]), set())
        self.assertEqual(got.kind, "file")

    # Negative 5: attachment without sha256 or with a path-like name
    def test_attachment_sha_and_name(self):
        for rec in ({"name": "a.pdf"}, {"sha256": "A" * 64, "name": "a.pdf"}, {"sha256": SHA[:-1], "name": "a"}):
            self.refused(to_candidate(pc(attachments=[rec]), set()), "bad_sha256")
        plain = {"DATA", "g\u0435t", "d\u043ecs"}  # plain words, not paths
        def check(name):
            got = to_candidate(pc(attachments=[{"sha256": SHA, "name": name}]), set())
            if name in plain:
                self.assertIsInstance(got, Candidate)
            else:
                self.refused(got, "bad_name")
        each(self, HOSTILE_PATH_PARTS + ("a/b", "", None, "x" * 256, "a\x00b", "COM\u00b9"), check)
        self.refused(to_candidate(pc(attachments="a.pdf"), set()), "bad_attachments")
        self.refused(to_candidate(pc(attachments=[{"sha256": SHA, "name": "a"}] * 11), set()), "bad_attachments")

    # Negative 6: NUL removed; bidi and zero-width kept but flagged
    def test_hostile_text(self):
        got = to_candidate(pc(text="a\x00b"), set())
        self.assertEqual((got.text, got.flags), ("ab", ()))
        bidi = to_candidate(pc(text="a\u202eb"), set())
        self.assertEqual((bidi.text, bidi.flags), ("a\u202eb", ("bidi",)))
        zw = to_candidate(pc(text="a\u200bb"), set())
        self.assertEqual((zw.text, zw.flags), ("a\u200bb", ("zero_width",)))

        def check(text):  # expected text and flags derived from the input, not the output
            got = to_candidate(pc(text=text), set())
            if isinstance(got, Refusal):
                self.assertIn(got.reason, ("too_long", "bad_text", "empty"))
                return
            want = unicodedata.normalize("NFC", text.replace("\x00", ""))
            self.assertEqual((got.text, got.flags), (want, expected_flags(want)))
        each(self, HOSTILE_TEXT + tuple(f"a{ch}b" for ch in INVISIBLE_SAMPLES), check)
        self.refused(to_candidate(pc(text="a\ud800b"), set()), "bad_text")

    # Negative 6 by class: every default-ignorable or Cf char outside bidi is kept and flagged zero_width
    def test_invisible_chars_flagged(self):
        def check(ch):
            for text in (f"a{ch}b", f"{ch}ab", f"ab{ch}", f"a{ch}{ch}\u200bb"):
                got = to_candidate(pc(text=text), set())
                self.assertEqual((got.text, got.flags), (text, ("zero_width",)), repr(text)[:80])
            mixed = to_candidate(pc(text=f"a{ch}\u202eb\x07"), set())
            self.assertEqual(mixed.flags, ("bidi", "control", "zero_width"))
        each(self, INVISIBLE_SAMPLES, check)
        for ch in "\u202a\u202e\u2066\u2069\u200e\u200f\u061c":  # bidi stays bidi only
            self.assertEqual(to_candidate(pc(text=f"a{ch}b"), set()).flags, ("bidi",))
        for text in ("a\u00a0b", "a\u2028b", "\u0131\u00e9\u015f", "a\u3000b", "e\u0301"):  # visible: no flag
            self.assertEqual(to_candidate(pc(text=text), set()).flags, (), repr(text))

    def test_allowlist_read_once(self):
        class Flip(dict):
            calls = 0
            def get(self, key, default=None):
                if key == "allowlist":
                    Flip.calls += 1
                    return "not allowed" if Flip.calls == 1 else "allowed"
                return super().get(key, default)
        self.refused(to_candidate(Flip(tg()), set()), "not_allowed")
        self.assertEqual(Flip.calls, 1)

    def test_hostile_fields_refused(self):
        for bad in ({"source": "email"}, {"source": None}, {"type": "transcript"}, {"type": None},
                    {"received_at": "2026-11-25T10:00:00"}, {"received_at": "yesterday"},
                    {"received_at": 0}, {"text": b"bytes"}, {"text": ["x"]}):
            self.refused(to_candidate(pc(**bad), set()))
        def check(value):
            self.refused(to_candidate(pc(source_id=value), set()), "bad_source_id")
        numbers = tuple(v for v in HOSTILE_NUMBERS if v != "1")  # "1" is a valid plain id
        each(self, numbers + ("", "a/b", "a b", "-1", "x" * 129), check)
        for item in (None, [], "x", b"{}"):
            self.refused(to_candidate(item, set()), "bad_item")

    def test_no_exception_escapes(self):
        self.refused(to_candidate(Exploding(), set()), "internal")
        self.refused(to_candidate(pc(attachments=[Exploding()]), set()), "internal")

    def test_static_imports(self):
        tree = ast.parse(Path(talk_inbound.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names.add(node.module)
        allowed = {"__future__", "re", "unicodedata", "collections.abc", "dataclasses", "datetime"}
        self.assertLessEqual(names, allowed)
        for refused in talk_inbound.REASONS.values():
            self.assertTrue(refused.isascii())


if __name__ == "__main__":
    unittest.main()
