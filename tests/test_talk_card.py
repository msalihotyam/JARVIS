"""TALKCARD1: approval card state machine (digest-bound, single-use, expiring)."""

import ast
import hashlib
import itertools
import json
import unittest
from pathlib import Path

from jarvis import talk_card as tc
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_TEXT, each

ROOT = Path(__file__).resolve().parents[1]
NOW = 1000.0
TTL = 300.0
TEXT = "Send the weekly report to the team."
CLASS = "send_message"
CITED = ("turn-1", "turn-7")


def rng(start=1):
    counter = itertools.count(start)
    return lambda n: bytes([next(counter) % 256]) * n


def make(cards=None, text=TEXT, action_class=CLASS, cited=CITED, now=NOW, ttl=TTL, random_bytes=None):
    return tc.create(text, action_class, cited, now, ttl, random_bytes or rng(), cards=cards or {})


def tap(card_id, choice="approve", view="pc", sequence=1, time=NOW + 10):
    return tc.Tap(card_id, choice, view, sequence, time)


class CreateTests(unittest.TestCase):
    def test_create_binds_digest_id_and_expiry(self):  # A1
        out = make()
        self.assertEqual(out.verdict, tc.CREATED)
        card = out.card
        self.assertEqual(card.card_id, "card-" + "01" * 16)
        self.assertEqual(card.status, tc.PENDING)
        self.assertEqual((card.created_at, card.expires_at), (NOW, NOW + TTL))
        self.assertIs(out.cards[card.card_id], card)

    def test_digest_is_sha256_of_documented_canonical_form(self):  # A1
        def field(s):
            b = s.encode("utf-8")
            return len(b).to_bytes(8, "big") + b
        blob = field(tc.DIGEST_TAG) + field(TEXT) + field(CLASS) + (2).to_bytes(8, "big")
        blob += field(CITED[0]) + field(CITED[1])
        self.assertEqual(make().card.digest, hashlib.sha256(blob).hexdigest())

    def test_length_prefix_separates_fields(self):  # A1
        a = make(cited=("ab", "c")).card.digest
        b = make(cited=("a", "bc")).card.digest
        c = make(cited=("c", "ab")).card.digest
        self.assertEqual(len({a, b, c}), 3)

    def test_ttl_zero_negative_or_over_max_refused(self):  # N6
        for ttl in (0, -1, 0.0, tc.MAX_TTL_SECONDS + 1, 10**400):
            out = make(ttl=ttl)
            self.assertEqual((out.verdict, out.reason), (tc.REFUSED, "invalid ttl"), ttl)
        self.assertEqual(make(ttl=tc.MAX_TTL_SECONDS).verdict, tc.CREATED)

    def test_hostile_numbers_refused_for_ttl_and_now(self):  # N6
        each(self, HOSTILE_NUMBERS, lambda v: self.assertEqual(make(ttl=v).verdict, tc.REFUSED))
        # -0.0 is a finite clock value and stays valid as `now`; everything else is refused
        each(self, HOSTILE_NUMBERS, lambda v: self.assertEqual(
            make(now=v).verdict, tc.CREATED if type(v) is float and v == 0 else tc.REFUSED))

    def test_short_or_wrong_random_bytes_refused(self):  # N7
        for raw in (b"", b"x" * 15, "x" * 16, bytearray(16), None):
            out = make(random_bytes=lambda n, raw=raw: raw)
            self.assertEqual((out.verdict, out.reason)[0], tc.REFUSED, raw)
        self.assertEqual(make(random_bytes=lambda n: b"x" * 15).reason, "random bytes too short")

    def test_existing_id_refused(self):  # N7
        first = make(random_bytes=lambda n: b"\x07" * n)
        again = make(cards=first.cards, random_bytes=lambda n: b"\x07" * n)
        self.assertEqual((again.verdict, again.reason), (tc.REFUSED, "card id already exists"))
        self.assertEqual(len(first.cards), 1)

    def test_bad_content_refused(self):
        for kwargs in ({"text": ""}, {"text": b"x"}, {"text": "x" * (tc.MAX_TEXT_CHARS + 1)},
                       {"action_class": "send\nmsg"}, {"cited": ["t", 3]}, {"cited": "turn-1"},
                       {"cited": ("t",) * (tc.MAX_CITED + 1)}):
            out = make(**kwargs)
            self.assertEqual((out.verdict, out.reason), (tc.REFUSED, "invalid card content"), kwargs)

    def test_hostile_text_refused_or_bound_exactly(self):
        def check(value):
            for kwargs in ({"text": value}, {"action_class": value}, {"cited": (value,)}):
                out = make(**kwargs)
                if out.verdict == tc.REFUSED:
                    self.assertTrue(out.reason.isascii())
                    continue
                card = tc.resolve(out.cards, tap(out.card.card_id)).card
                args = {"text": TEXT, "action_class": CLASS, "cited": CITED, **kwargs}
                self.assertTrue(tc.verify(card, args["text"], args["action_class"], args["cited"]))
                key = next(iter(kwargs))
                args[key] = (value + " ",) if key == "cited" else value + " "
                self.assertFalse(tc.verify(card, args["text"], args["action_class"], args["cited"]))
        each(self, HOSTILE_TEXT, check)

    def test_bad_card_state_refused(self):
        for cards in (None, [], {"x": "y"}, {"card-" + "00" * 16: object()}):
            out = tc.create(TEXT, CLASS, CITED, NOW, TTL, rng(), cards=cards)
            self.assertEqual(out.verdict, tc.REFUSED, cards)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        out = make()
        self.cards, self.card = out.cards, out.card

    def test_approve_and_reject(self):  # A2, A4
        ok = tc.resolve(self.cards, tap(self.card.card_id))
        self.assertEqual(ok.verdict, tc.APPROVED)
        self.assertTrue(ok.approved)
        self.assertEqual(ok.card.digest, self.card.digest)
        no = tc.resolve(self.cards, tap(self.card.card_id, choice="reject"))
        self.assertEqual((no.verdict, no.card.status), (tc.REJECTED, tc.REJECTED))
        self.assertEqual(self.cards[self.card.card_id].status, tc.PENDING)  # input not mutated

    def test_first_valid_tap_is_final(self):  # A3
        first = tc.resolve(self.cards, tap(self.card.card_id, choice="reject"))
        second = tc.resolve(first.cards, tap(self.card.card_id, sequence=2))
        self.assertEqual((second.verdict, second.reason), (tc.REFUSED, "already resolved"))
        self.assertIsNone(second.record)
        self.assertEqual(second.card.status, tc.REJECTED)

    def test_pc_and_telegram_lower_sequence_wins(self):  # N3
        for low, high in (("pc", "telegram"), ("telegram", "pc")):
            first = tc.resolve(self.cards, tap(self.card.card_id, view=low, sequence=4))
            second = tc.resolve(first.cards, tap(self.card.card_id, "reject", high, 5))
            self.assertEqual(first.verdict, tc.APPROVED)
            self.assertEqual(first.record["view"], low)
            self.assertEqual((second.verdict, second.reason), (tc.REFUSED, "already resolved"))

    def test_expired_card_is_expired_and_refused(self):  # A3, N5
        for when in (NOW + TTL, NOW + TTL + 1):
            out = tc.resolve(self.cards, tap(self.card.card_id, time=when))
            self.assertEqual((out.verdict, out.reason), (tc.REFUSED, "card expired"))
            self.assertEqual((out.card.status, out.record["status"]), (tc.EXPIRED, tc.EXPIRED))
            late = tc.resolve(out.cards, tap(self.card.card_id, sequence=2, time=NOW + 1))
            self.assertEqual(late.reason, "already resolved")
            self.assertFalse(tc.verify(out.card, TEXT, CLASS, CITED))
        self.assertEqual(tc.resolve(self.cards, tap(self.card.card_id, time=NOW + TTL - 0.001)).verdict,
                         tc.APPROVED)

    def test_time_before_creation_or_not_finite_refused(self):  # N5
        self.assertEqual(tc.resolve(self.cards, tap(self.card.card_id, time=NOW - 1)).reason,
                         "tap before creation")
        self.assertEqual(tc.resolve(self.cards, tap(self.card.card_id, time=NOW)).verdict, tc.APPROVED)
        each(self, HOSTILE_NUMBERS, lambda v: self.assertEqual(
            tc.resolve(self.cards, tap(self.card.card_id, time=v)).verdict, tc.REFUSED))

    def test_unknown_or_malformed_card_id_refused(self):  # N1
        self.assertEqual(tc.resolve(self.cards, tap("card-" + "ff" * 16)).reason, "unknown card")
        bad = ["", None, 7, "card-" + "01" * 15, "card-" + "01" * 17, "CARD-" + "01" * 16,
               "card-" + "0G" * 16, " " + self.card.card_id, self.card.card_id.upper()]
        for card_id in bad:
            self.assertEqual(tc.resolve(self.cards, tap(card_id)).reason, "malformed card id", card_id)
        each(self, HOSTILE_TEXT, lambda v: self.assertEqual(tc.resolve(self.cards, tap(v)).verdict, tc.REFUSED))

    def test_only_exact_choice_in_a_tap_record_resolves(self):  # N2
        for choice in ("onayla", "yes", "approve ", "Approve", "APPROVE", "onaylıyorum", None, True, b"approve"):
            out = tc.resolve(self.cards, tap(self.card.card_id, choice=choice))
            self.assertEqual((out.verdict, out.reason), (tc.REFUSED, "invalid choice"), choice)
        each(self, HOSTILE_TEXT, lambda v: self.assertEqual(
            tc.resolve(self.cards, tap(self.card.card_id, choice=v)).verdict, tc.REFUSED))
        for not_a_tap in ("onaylıyorum", (self.card.card_id, "approve", "pc", 1, NOW + 1),
                          {"card_id": self.card.card_id, "choice": "approve"}, None):
            self.assertEqual(tc.resolve(self.cards, not_a_tap).reason, "not a tap record")

    def test_bad_view_or_sequence_refused(self):
        for view in ("PC", "sms", None, ""):
            self.assertEqual(tc.resolve(self.cards, tap(self.card.card_id, view=view)).reason, "invalid view")
        for seq in (-1, tc.MAX_SEQUENCE, 1.0, True, "1", None, 10**400):
            self.assertEqual(tc.resolve(self.cards, tap(self.card.card_id, sequence=seq)).reason,
                             "invalid sequence", seq)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        out = make()
        self.approved = tc.resolve(out.cards, tap(out.card.card_id)).card

    def test_verify_recomputes_digest(self):  # A4
        self.assertTrue(tc.verify(self.approved, TEXT, CLASS, CITED))
        self.assertTrue(tc.verify(self.approved, TEXT, CLASS, list(CITED)))

    def test_any_change_fails_verify(self):  # N4
        changed = [TEXT[:-1] + "!", TEXT + " ", " " + TEXT, TEXT.replace(" ", "  ", 1), TEXT.replace(" ", " ", 1),
                   TEXT.replace("e", "е", 1), TEXT.replace("l", "ı", 1), TEXT.replace("o", "ο", 1),
                   TEXT.replace("S", "Ｓ", 1),
                   TEXT.replace("t", "t​", 1), TEXT.lower()]
        for text in changed:
            self.assertFalse(tc.verify(self.approved, text, CLASS, CITED), text)
        self.assertFalse(tc.verify(self.approved, TEXT, "send_messages", CITED))
        self.assertFalse(tc.verify(self.approved, TEXT, CLASS, CITED[::-1]))
        self.assertFalse(tc.verify(self.approved, TEXT, CLASS, CITED[:1]))

    def test_nfc_equivalent_text_verifies(self):  # documented canonical form
        out = make(text="café")
        card = tc.resolve(out.cards, tap(out.card.card_id)).card
        self.assertTrue(tc.verify(card, "café", CLASS, CITED))

    def test_only_approved_card_verifies(self):
        out = make()
        self.assertFalse(tc.verify(out.card, TEXT, CLASS, CITED))
        rejected = tc.resolve(out.cards, tap(out.card.card_id, "reject")).card
        self.assertFalse(tc.verify(rejected, TEXT, CLASS, CITED))
        for forged in ("é" * 64, None, b"x"):
            card = tc.Card(self.approved.card_id, forged, NOW, NOW + TTL, tc.APPROVED)
            self.assertFalse(tc.verify(card, TEXT, CLASS, CITED))
        self.assertFalse(tc.verify(None, TEXT, CLASS, CITED))


def _session():
    """Two cards: one approved, one expired; returns (records, cards, taps)."""
    random_bytes = rng()
    a = tc.create(TEXT, CLASS, CITED, NOW, TTL, random_bytes, cards={})
    b = tc.create("Delete draft", "delete", ("turn-2",), NOW, TTL, random_bytes, cards=a.cards)
    taps = [tap(a.card.card_id, sequence=1), tap(b.card.card_id, "reject", "telegram", 2, NOW + TTL)]
    r1 = tc.resolve(b.cards, taps[0])
    r2 = tc.resolve(r1.cards, taps[1])
    return [a.record, b.record, r1.record, r2.record], r2.cards, taps


class LogTests(unittest.TestCase):
    def test_every_state_change_returns_a_record(self):  # A5
        records, _, _ = _session()
        self.assertEqual([r["event"] for r in records], ["created", "created", "tap", "tap"])
        self.assertEqual([r.get("status") for r in records[2:]], [tc.APPROVED, tc.EXPIRED])
        json.dumps(records)  # plain JSON-safe values

    def test_rebuild_after_restart_refuses_replayed_tap(self):  # N8
        records, cards, taps = _session()
        log = json.loads(json.dumps(records))
        rebuilt = tc.rebuild(log)
        self.assertEqual(rebuilt.verdict, tc.REBUILT)
        self.assertEqual(rebuilt.cards, cards)
        for t in taps:
            out = tc.resolve(rebuilt.cards, t)
            self.assertEqual((out.verdict, out.reason), (tc.REFUSED, "already resolved"))

    def test_corrupt_log_refused(self):  # N8
        records, _, _ = _session()
        tampered_status = dict(records[2], status=tc.REJECTED)
        tampered_time = dict(records[3], time=NOW + 1)
        bad_logs = [
            records + [records[2]],                        # replayed tap
            records + [records[0]],                        # duplicate create
            [records[2]],                                  # tap before create
            records[:2] + [tampered_status],
            records[:3] + [tampered_time],
            records[:1] + [dict(records[1], expires_at=NOW)],
            records[:1] + [dict(records[1], digest="0" * 63)],
            records + [{"event": "approve_all"}],
            records + [dict(records[2], extra=1)],
            records + ["tap"],
            "records",
            None,
        ]
        for log in bad_logs:
            self.assertEqual(tc.rebuild(log).reason, "corrupt log", log)
        self.assertEqual(tc.rebuild([]).cards, {})


class SafetyTests(unittest.TestCase):
    def test_recursion_error_from_injected_callable_is_refused(self):
        def boom(n):
            raise RecursionError("deep")
        out = make(random_bytes=boom)
        self.assertEqual((out.verdict, out.reason), (tc.REFUSED, "internal error"))

    def test_refusal_reasons_are_ascii(self):
        out = make()
        reasons = {make(ttl=0).reason, make(text="").reason, make(random_bytes=lambda n: b"").reason,
                   tc.resolve(out.cards, tap("x")).reason, tc.resolve(out.cards, "x").reason,
                   tc.rebuild(None).reason, make(random_bytes=lambda n: 1 / 0).reason}
        self.assertTrue(all(r and r.isascii() for r in reasons), reasons)

    def test_module_imports_no_io(self):
        tree = ast.parse((ROOT / "jarvis" / "talk_card.py").read_text(encoding="utf-8"))
        allowed = {"hashlib", "hmac", "math", "unicodedata", "collections", "dataclasses"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                self.assertIn(name.split(".")[0], allowed)
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(calls & {"open", "__import__", "eval", "exec", "compile", "print", "input"})

    def test_nothing_in_the_app_references_the_module(self):
        own = ROOT / "jarvis" / "talk_card.py"
        hits = [p for d in ("jarvis", "scripts") for p in (ROOT / d).rglob("*.py")
                if p != own and "talk_card" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
