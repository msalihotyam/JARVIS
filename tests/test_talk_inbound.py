import ast
import unittest
from collections.abc import Mapping
from pathlib import Path

from jarvis import talk_inbound
from jarvis.talk_inbound import WHO, Candidate, Duplicate, Refusal, to_candidate
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, each

SHA = "a" * 64
T0 = "2026-11-25T10:00:00+00:00"


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

        def check(text):
            got = to_candidate(pc(text=text), set())
            if isinstance(got, Refusal):
                self.assertIn(got.reason, ("too_long", "bad_text"))
                return
            self.assertNotIn("\x00", got.text)
            if any(ch in got.text for ch in "\u202a\u202e\u2066\u2069"):
                self.assertIn("bidi", got.flags)
            if any(ch in got.text for ch in "\u200b\u200d\ufeff"):
                self.assertIn("zero_width", got.flags)
        each(self, HOSTILE_TEXT, check)
        self.refused(to_candidate(pc(text="a\ud800b"), set()), "bad_text")

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
