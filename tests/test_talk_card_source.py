"""TALKCARDSRC1: security-class cards are approved only from the PC."""

import ast
import pathlib
import unittest
from collections.abc import Mapping
from unittest import mock

from jarvis import talk_card
from jarvis import talk_card_source as src
from tests import adversarial

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "jarvis" / "talk_card_source.py"
PLAN_NAMES = ("scope", "priority", "plan", "lane_count", "pause_resume", "design_choice")
SECURITY_NAMES = ("protected_file_key", "permission_profile", "allowlist",
                  "destination_list", "irreversible_deletion", "reset_right")
CARD_ID = "card-" + "ab" * 16


def record(card_class=..., **extra):
    out = {"event": "created", "card_id": CARD_ID, **extra}
    if card_class is not ...:
        out["card_class"] = card_class
    return out


def tap(source, **extra):
    return {"event": "tap", "card_id": CARD_ID, "choice": "approve", "source": source, **extra}


class AllowTests(unittest.TestCase):
    def test_plan_class_from_pc_and_telegram(self):
        for name in PLAN_NAMES:
            for view in ("pc", "telegram"):
                with self.subTest(name=name, view=view):
                    self.assertEqual(src.allow(name, view), src.Decision(src.ALLOW))

    def test_security_class_pc_only(self):
        for name in SECURITY_NAMES:
            with self.subTest(name=name):
                self.assertTrue(src.allow(name, "pc").allowed)
                d = src.allow(name, "telegram")
                self.assertEqual(d.verdict, src.REFUSE)
                self.assertEqual(d.reason, "security card needs the pc")
                self.assertEqual(d.note, "Bu onay yalnız bilgisayardan verilir.")

    def test_unknown_or_missing_class_is_security(self):
        for name in ("deploy", "", "security", None):
            with self.subTest(name=name):
                self.assertEqual(src.class_of(name), src.SECURITY)
                self.assertTrue(src.allow(name, "pc").allowed)
                self.assertEqual(src.allow(name, "telegram").note, src.PC_ONLY_NOTE)
        self.assertTrue(src.check_tap(record(), tap("pc")).allowed)
        self.assertFalse(src.check_tap(record(), tap("telegram")).allowed)

    def test_class_lists_are_one_constant(self):
        self.assertEqual(set(src.CARD_CLASSES), {src.PLAN, src.SECURITY})
        self.assertEqual(src.CARD_CLASSES[src.PLAN], frozenset(PLAN_NAMES))
        self.assertEqual(src.CARD_CLASSES[src.SECURITY], frozenset(SECURITY_NAMES))
        self.assertFalse(src.CARD_CLASSES[src.PLAN] & src.CARD_CLASSES[src.SECURITY])
        with self.assertRaises(TypeError):
            src.CARD_CLASSES["plan"] = frozenset({"reset_right"})

    def test_refusal_reasons_ascii(self):
        cases = [src.allow("reset_right", "telegram"), src.allow(1, "pc"),
                 src.allow("scope", "sms"), src.allow("scope", "pc", x=1),
                 src.check_tap(None, None), src.check_tap(record("scope"), tap("pc", view="telegram"))]
        for d in cases:
            self.assertEqual(d.verdict, src.REFUSE)
            self.assertTrue(d.reason.isascii(), d.reason)
            self.assertIn(d.note, ("", src.PC_ONLY_NOTE))

    def test_deterministic(self):
        self.assertEqual(src.allow("reset_right", "telegram"), src.allow("reset_right", "telegram"))


class CheckTapTests(unittest.TestCase):
    def test_class_from_record_view_from_source(self):
        self.assertTrue(src.check_tap(record("scope"), tap("telegram")).allowed)
        self.assertTrue(src.check_tap(record("reset_right"), tap("pc")).allowed)
        self.assertEqual(src.check_tap(record("reset_right"), tap("telegram")).note,
                         src.PC_ONLY_NOTE)

    def test_tap_cannot_carry_class_or_override(self):
        for key in ("card_class", "class", "classes", "override", "allowed_views"):
            with self.subTest(key=key):
                d = src.check_tap(record("reset_right"), tap("telegram", **{key: "scope"}))
                self.assertEqual(d.verdict, src.REFUSE)
                d = src.check_tap(record("scope"), tap("pc", **{key: "scope"}))
                self.assertEqual(d.verdict, src.REFUSE)

    def test_view_pc_with_source_telegram_refused(self):
        d = src.check_tap(record("scope"), tap("telegram", view="pc"))
        self.assertEqual((d.verdict, d.reason), (src.REFUSE, "view does not match source"))
        d = src.check_tap(record("reset_right"), tap("telegram", view="pc"))
        self.assertEqual(d.verdict, src.REFUSE)
        self.assertTrue(src.check_tap(record("scope"), tap("pc", view="pc")).allowed)

    def test_card_text_says_plan_but_class_security(self):
        rec = record("reset_right", text="plan: approve the plan", action_class="plan")
        d = src.check_tap(rec, tap("telegram"))
        self.assertEqual(d.verdict, src.REFUSE)
        self.assertEqual(d.note, src.PC_ONLY_NOTE)

    def test_card_id_mismatch_refused(self):
        other = dict(tap("pc"), card_id="card-" + "cd" * 16)
        self.assertEqual(src.check_tap(record("scope"), other).reason, "card mismatch")
        no_id = {k: v for k, v in record("scope").items() if k != "card_id"}
        self.assertEqual(src.check_tap(no_id, tap("pc")).verdict, src.REFUSE)


class PhoneRefusalLeavesCardOpenTests(unittest.TestCase):
    def test_refused_phone_tap_then_pc_approves(self):
        out = talk_card.create("delete the archive", "irreversible_deletion", [], 100.0, 60.0,
                               lambda n: b"\x01" * n, cards={})
        self.assertEqual(out.verdict, talk_card.CREATED)
        cards, card_rec = out.cards, dict(out.record, card_class="irreversible_deletion")
        card_id = out.card.card_id

        phone = {"card_id": card_id, "choice": "approve", "source": "telegram"}
        d = src.check_tap(card_rec, phone)
        self.assertEqual((d.verdict, d.note), (src.REFUSE, "Bu onay yalnız bilgisayardan verilir."))
        # The refused tap never reaches talk_card.resolve: the card stays PENDING.
        self.assertEqual(cards[card_id].status, talk_card.PENDING)

        pc = {"card_id": card_id, "choice": "approve", "source": "pc"}
        self.assertTrue(src.check_tap(card_rec, pc).allowed)
        res = talk_card.resolve(cards, talk_card.Tap(card_id, "approve", "pc", 1, 110.0))
        self.assertEqual(res.verdict, talk_card.APPROVED)


class _Boom(str):
    def __eq__(self, other):
        raise RecursionError

    __hash__ = str.__hash__


class _BombMapping(Mapping):
    def __getitem__(self, key):
        raise RecursionError

    def __iter__(self):
        return iter(["card_id"])

    def __len__(self):
        return 1


class HostileTests(unittest.TestCase):
    def assert_phone_refused(self, name):
        self.assertEqual(src.class_of(name), src.SECURITY)
        d = src.allow(name, "telegram")
        self.assertEqual(d.verdict, src.REFUSE)
        self.assertEqual(src.check_tap(record(name), tap("telegram")).verdict, src.REFUSE)

    def test_case_space_lookalike_class_is_security(self):
        variants = ("Scope", "SCOPE", " scope", "scope ", "sco pe", "lane count", "lane-count",
                    "Plan", "PLAN", "plаn", "ѕcope", "ｓｃｏｐｅ",
                    "sc​ope", "plan‍", "рlan", "plan\n", "scope\x00")
        adversarial.each(self, variants, self.assert_phone_refused)
        adversarial.each(self, adversarial.HOSTILE_TEXT, self.assert_phone_refused)

    def test_hostile_numbers_as_class_or_view(self):
        def check(value):
            if value is None or type(value) is str:
                self.assertEqual(src.allow(value, "telegram").verdict, src.REFUSE)
            else:
                self.assertEqual(src.allow(value, "pc").reason, "invalid card class")
            self.assertEqual(src.allow("scope", value).reason, "invalid view")
            self.assertEqual(src.check_tap(record("scope"), tap(value)).verdict, src.REFUSE)
        adversarial.each(self, adversarial.HOSTILE_NUMBERS, check)

    def test_hostile_text_as_view(self):
        adversarial.each(self, adversarial.HOSTILE_TEXT + ("PC", " pc", "Telegram", "pс"),
                         lambda v: self.assertEqual(src.allow("scope", v).reason, "invalid view"))

    def test_wrong_types_refused(self):
        for bad in (None, [], "card", 1, ("card_id", CARD_ID), b"x", object()):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(src.check_tap(bad, tap("pc")).reason, "not a record")
                self.assertEqual(src.check_tap(record("scope"), bad).reason, "not a record")
        for bad in (["scope"], {"scope"}, b"scope", 1.0, ("scope",)):
            with self.subTest(card_class=repr(bad)):
                self.assertEqual(src.allow(bad, "pc").reason, "invalid card class")
                self.assertEqual(src.check_tap(record(bad), tap("pc")).verdict, src.REFUSE)
        self.assertEqual(src.check_tap(dict(record("scope"), card_id=1), tap("pc")).verdict,
                         src.REFUSE)

    def test_class_list_or_override_in_call_refused(self):
        plan_list = frozenset({"reset_right"})
        calls = [
            lambda: src.allow("reset_right", "telegram", plan_list),
            lambda: src.allow("reset_right", "telegram", True),
            lambda: src.allow("reset_right", "telegram", override=True),
            lambda: src.allow("reset_right", "telegram", classes={"plan": plan_list}),
            lambda: src.allow("reset_right", "telegram", allowed_views=("telegram",)),
            lambda: src.allow("scope", "pc", override=False),
            lambda: src.check_tap(record("reset_right"), tap("telegram"), plan_list),
            lambda: src.check_tap(record("reset_right"), tap("telegram"), override=True),
            lambda: src.check_tap(record("scope"), tap("pc"), card_classes=src.CARD_CLASSES),
        ]
        for i, call in enumerate(calls):
            with self.subTest(i=i):
                self.assertEqual(call(), src.Decision(src.REFUSE, "extra arguments refused"))

    def test_no_exception_escapes(self):
        self.assertEqual(src.allow(_Boom("scope"), "pc").reason, "invalid card class")
        self.assertEqual(src.allow("scope", _Boom("pc")).reason, "invalid view")
        with mock.patch.object(src, "class_of", side_effect=RecursionError):
            self.assertEqual(src.allow("scope", "pc"), src.Decision(src.REFUSE, "internal error"))
        self.assertEqual(src.check_tap(_BombMapping(), tap("pc")).reason, "internal error")
        self.assertEqual(src.check_tap(record("scope"), _BombMapping()).reason, "internal error")
        self.assertEqual(src.check_tap(record("scope"), tap(_Boom("pc"), view="pc")).verdict,
                         src.REFUSE)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.source = MODULE.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_imports_pure_stdlib_only(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, ("open", "exec", "eval", "__import__", "compile"))
        self.assertEqual(names, {"collections.abc", "dataclasses", "types"})

    def test_texts_ascii_except_fixed_note(self):
        strings = [n.value for n in ast.walk(self.tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        non_ascii = {s for s in strings if not s.isascii()}
        self.assertEqual(non_ascii, {"Bu onay yalnız bilgisayardan verilir."})

    def test_one_constant_assignment(self):
        targets = [t.id for n in ast.walk(self.tree) if isinstance(n, ast.Assign)
                   for t in n.targets if isinstance(t, ast.Name)]
        self.assertEqual(targets.count("CARD_CLASSES"), 1)

    def test_nothing_in_app_imports_module(self):
        hits = []
        for folder in ("jarvis", "scripts"):
            for path in (ROOT / folder).rglob("*.py"):
                if path != MODULE and "talk_card_source" in path.read_text(
                        encoding="utf-8", errors="replace"):
                    hits.append(str(path))
        self.assertEqual(hits, [])

    def test_source_guards(self):
        self.assertNotIn("def " + "heartbeat", self.source)
        self.assertNotIn("project" + "_folders", self.source)
