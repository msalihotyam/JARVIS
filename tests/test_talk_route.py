"""TALKROUTE1: inbound entry routing table (jarvis/talk_route.py). Pure; no file, network or DB."""

import ast
import itertools
import unittest
from collections.abc import Mapping
from pathlib import Path

from jarvis import talk_route as tr
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_TEXT, each, narrow_stdout

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "jarvis" / "talk_route.py"
ATT = (("0" * 64, "photo.jpg"),)
LABEL = "Sesli yanıt"


def entry(kind="text", text="merhaba", attachments=(), **extra):
    return {"who": "salih", "kind": kind, "text": text, "attachments": attachments, **extra}


class Hostile(dict):
    """Mapping whose every read raises (RuntimeError or RecursionError)."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def get(self, *args):
        raise self.error

    def __contains__(self, key):
        raise self.error


class StaticTests(unittest.TestCase):
    def test_pure_imports_and_no_io_calls(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "exec", "eval", "compile", "__import__", "input", "print"})
        self.assertLessEqual(imported, {"__future__", "math", "collections.abc", "dataclasses"})

    def test_nothing_in_the_app_imports_it(self):
        for path in (ROOT / "jarvis").rglob("*.py"):
            if path != SOURCE:
                with self.subTest(path=path.name):
                    self.assertNotIn("talk_route", path.read_text(encoding="utf-8", errors="replace"))

    def test_reason_texts_ascii(self):
        with narrow_stdout():
            for code, text in tr.REASONS.items():
                self.assertTrue(code.isascii() and text.isascii(), code)
                print(text)
            for path in tr.PATHS:
                print(path)


class AcceptanceTests(unittest.TestCase):
    def test_routing_table(self):
        cases = (
            (entry(), tr.TURN, "text"),
            (entry("voice", ""), tr.SPEECH_THEN_TURN, "voice"),
            (entry("approval_tap", ""), tr.CARD, "approval_tap"),
            (entry(text=LABEL), tr.MODE, "mode_label"),
            (entry(text="Yazılı yanıt"), tr.MODE, "mode_label"),
            (entry("image", "bak buna", ATT), tr.TURN, "attachment_text"),
            (entry("file", "", ATT), tr.HOLD, "attachment_hold"),
            (entry("unsupported", ""), tr.NOTE, "unsupported"),
            (entry("edit", "yeni metin"), tr.NOTE, "edit"),
        )
        for e, path, reason in cases:
            with self.subTest(kind=e["kind"], text=e["text"][:80]):
                r = tr.route(e)
                self.assertEqual((r.path, r.reason), (path, reason))

    def test_mode_carries_mode(self):
        self.assertEqual(tr.route(entry(text=LABEL)).mode, "voice")
        self.assertEqual(tr.route(entry(text="Yazılı yanıt")).mode, "text")

    def test_hold_carries_seconds_parameter(self):
        self.assertEqual(tr.route(entry("image", "", ATT), hold_seconds=7).seconds, 7)
        self.assertEqual(tr.route(entry("image", "  ", list(ATT)), hold_seconds=2.5).seconds, 2.5)
        self.assertEqual(tr.route(entry("image", "", ATT)).seconds, tr.DEFAULT_HOLD_SECONDS)

    def test_note_lines_fixed_turkish(self):
        for kind in ("edit", "unsupported"):
            self.assertEqual(tr.route(entry(kind)).note, tr.NOTES[kind])
        self.assertEqual(tr.route(entry(text="/start")).note, tr.NOTES["greeting"])

    def test_start_greeting_other_slash_plain_text(self):
        r = tr.route(entry(text="/start"))
        self.assertEqual((r.path, r.reason), (tr.NOTE, "greeting"))
        for text in ("/rm -rf x", "/help", "/start now", "/START", "/"):
            with self.subTest(text=text):
                e = entry(text=text)
                r = tr.route(e)
                self.assertEqual((r.path, r.reason), (tr.TURN, "slash_text"))
                self.assertEqual(e["text"], text)
                self.assertEqual(set(vars(r)), {"path", "reason", "seconds", "mode", "note"})

    def test_total_and_deterministic(self):
        texts, atts, fwds = ("hi", "", "/start", LABEL), ((), ATT), (None, True, False)
        for kind, text, att, fwd in itertools.product(tr.KINDS, texts, atts, fwds):
            e = entry(kind, text, att) if fwd is None else entry(kind, text, att, forwarded=fwd)
            with self.subTest(kind=kind, text=text, att=bool(att), fwd=fwd):
                r = tr.route(e)
                self.assertIn(r.path, tr.PATHS)
                self.assertIn(r.reason, tr.REASONS)
                self.assertEqual(r, tr.route(dict(e)))
                self.assertTrue(r.message.isascii())


class NegativeTests(unittest.TestCase):
    def assertRefused(self, r, reason):
        self.assertEqual((r.path, r.reason), (tr.REFUSED, reason))
        self.assertTrue(r.message.isascii())

    def test_approval_tap_never_turn(self):
        def check(text):
            for att in ((), ATT):
                for extra in ({}, {"forwarded": True}):
                    r = tr.route(entry("approval_tap", text, att, **extra))
                    self.assertEqual((r.path, r.reason), (tr.CARD, "approval_tap"))
        each(self, ("tamam", "/start", LABEL, "", *HOSTILE_TEXT), check)

    def test_forwarded_mode_label_is_turn(self):
        for fwd in (True, 1, "yes", "False", None, 0, [], {"from": "x"}):
            with self.subTest(fwd=repr(fwd)[:80]):
                r = tr.route(entry(text=LABEL, forwarded=fwd))
                self.assertEqual((r.path, r.reason), (tr.TURN, "forwarded"))
                self.assertIsNone(r.mode)
        self.assertEqual(tr.route(entry(text=LABEL, forwarded=False)).path, tr.MODE)
        self.assertEqual(tr.route(entry("image", LABEL, ATT)).path, tr.TURN)

    def test_lying_containers_refused(self):
        # CDX-TALKROUTE1FIX2 F1/F2: only an exact dict is read; overridable reads never decide
        class Hides(dict):  # value present, membership denies it
            def __contains__(self, key):
                return False if key == "forwarded" else super().__contains__(key)

        class Liar(dict):  # get and membership both deny a present forwarded=True
            def __contains__(self, key):
                return key != "forwarded" and super().__contains__(key)

            def get(self, key, default=None):
                return default if key == "forwarded" else super().get(key, default)

        class Claims(dict):  # membership claims it, get reports the caller default
            def __contains__(self, key):
                return key == "forwarded" or super().__contains__(key)

            def get(self, key, default=None):
                return default if key == "forwarded" else super().get(key, default)

        class Flips(dict):  # first get says True, any later get says False
            def get(self, key, *default):
                return True if key == "forwarded" else super().get(key, *default)

        class Raises(dict):
            def __contains__(self, key):
                raise RuntimeError("no")

        class View(Mapping):  # non-dict Mapping
            def __init__(self, data):
                self.data = data

            def __getitem__(self, key):
                return self.data[key]

            def __iter__(self):
                return iter(self.data)

            def __len__(self):
                return len(self.data)

        class Spoof:  # not a Mapping; __class__ claims dict so isinstance(x, dict) passes
            def __init__(self, data):
                self.data = data

            @property
            def __class__(self):
                return dict

            def get(self, key, default=None):
                return self.data.get(key, default)

            def __contains__(self, key):
                return key in self.data

        self.assertIsInstance(Spoof({}), dict)
        for text in (LABEL, "Yazılı yanıt", "/start", "merhaba"):
            for fwd in (True, False, None, "absent"):
                e = entry(text=text) if fwd == "absent" else entry(text=text, forwarded=fwd)
                for cls in (Hides, Liar, Claims, Flips, Raises, View, Spoof):
                    with self.subTest(text=text, fwd=fwd, cls=cls.__name__):
                        self.assertRefused(tr.route(cls(e)), "bad_entry")
        # F1 exact probe: its own __getitem__ and iteration report forwarded=True
        liar = Liar(entry(text=LABEL, forwarded=True))
        self.assertTrue(liar["forwarded"] is True and "forwarded" in list(liar))
        self.assertRefused(tr.route(liar), "bad_entry")

    def test_lying_keys_refused(self):
        class Key(str):  # str subclass key: own hash hides it from get("forwarded")
            def __hash__(self):
                return 7

            def __eq__(self, other):
                return str.__eq__(self, other)

        class Same(str):  # same hash and eq as the real key
            __hash__ = str.__hash__

        for key in (Key("forwarded"), Same("forwarded"), 1, ("forwarded",), None):
            with self.subTest(key=repr(key)):
                e = {**entry(text=LABEL), key: True}
                self.assertRefused(tr.route(e), "bad_entry")
        self.assertRefused(tr.route({Same("who"): "salih", "kind": "text", "text": "x"}), "bad_entry")

    def test_metaclass_eq_liar_type_refused(self):
        class Liar(type):  # type(x) == anything is True; only identity tells it apart
            def __eq__(cls, other):
                return True

            __hash__ = type.__hash__

        class Fake(metaclass=Liar):
            def __init__(self, truthy):
                self.truthy = truthy

            def __bool__(self):
                return self.truthy

            def __len__(self):
                return int(self.truthy)

        for truthy in (False, True):
            fake = Fake(truthy)
            for field, reason in (("who", "bad_who"), ("kind", "bad_kind"), ("text", "bad_text"),
                                  ("attachments", "bad_attachments")):
                with self.subTest(field=field, truthy=truthy):
                    r = tr.route({**entry(text=LABEL), field: fake})
                    self.assertEqual((r.path, r.reason), (tr.REFUSED, reason))

    def test_who_not_salih_refused(self):
        values = ("Salih", "SALIH", "salih ", " salih", "salıh", "jarvis", "system", "", None, 1,
                  ["salih"], *HOSTILE_TEXT, *HOSTILE_NUMBERS)
        each(self, values, lambda who: self.assertRefused(tr.route({**entry(), "who": who}), "bad_who"))
        self.assertRefused(tr.route({"kind": "text", "text": "x"}), "bad_who")

        class Str(str):
            def __eq__(self, other):
                return True
            __hash__ = str.__hash__
        self.assertRefused(tr.route({**entry(), "who": Str("x")}), "bad_who")

    def test_unknown_kind_refused(self):
        values = ("transcript", "approval_card", "button", "TEXT", "text ", "tеxt", "", None,
                  *HOSTILE_TEXT, *HOSTILE_NUMBERS)
        each(self, values, lambda kind: self.assertRefused(tr.route(entry(kind)), "bad_kind"))

    def test_hold_zero_or_negative_is_turn_at_once(self):
        for seconds in (0, -0.0, 0.0, -1, -2.5, -10**400):
            with self.subTest(seconds=seconds):
                r = tr.route(entry("image", "", ATT), hold_seconds=seconds)
                self.assertEqual((r.path, r.reason, r.seconds), (tr.TURN, "hold_off", None))

    def test_bad_hold_refused_on_every_kind(self):
        # every hostile number but -0.0 (a valid zero); 10**400 is above the cap
        bad = [n for n in HOSTILE_NUMBERS if not (type(n) is float and n == 0)] + [tr.MAX_HOLD_SECONDS + 1]

        def check(seconds):
            for kind in tr.KINDS:
                self.assertRefused(tr.route(entry(kind, "", ATT), hold_seconds=seconds), "bad_hold")
        each(self, bad, check)

    def test_lookalike_mode_label_is_turn(self):
        values = ("sesli yanıt", "SESLİ YANIT", "Sesli yanit", "Sesli Yanıt", " Sesli yanıt", "Sesli yanıt ",
                  "Sesli  yanıt", "Sesli yanıt", "Sesli​ yanıt", "Sеsli yanıt", "Sesli yanıṫ",
                  "Seslı yanıt", "﻿Sesli yanıt", "Sesli yanıt\n", "Sesli\tyanıt", "Seslі yanıt",
                  "/start ", "/Start", *[f"{LABEL}{t}" for t in HOSTILE_TEXT[:12]])
        def check(text):
            r = tr.route(entry(text=text))
            self.assertEqual(r.path, tr.TURN)
            self.assertIsNone(r.mode)
        each(self, values, check)

    def test_bad_entries_refused_never_raise(self):
        each(self, (None, "text", 1, ["who", "salih"], ("salih",), b"x", object(), *HOSTILE_TEXT[:5]),
             lambda e: self.assertRefused(tr.route(e), "bad_entry"))
        for error in (RuntimeError("boom"), RecursionError(), KeyError("who")):
            with self.subTest(error=type(error).__name__):
                self.assertRefused(tr.route(Hostile(error)), "bad_entry")
        each(self, (1, ["x"], b"x", *HOSTILE_NUMBERS[:6]),
             lambda t: self.assertRefused(tr.route(entry(text=t)), "bad_text"))
        each(self, ("photo.jpg", {"a": 1}, 1, True, b"x"),
             lambda a: self.assertRefused(tr.route(entry("image", "", a)), "bad_attachments"))
        self.assertRefused(tr.route(entry("text", "  ")), "empty")
        self.assertRefused(tr.route(entry("text", None, None)), "empty")

    def test_hostile_text_routes_as_turn(self):
        def check(text):
            r = tr.route(entry(text=text))
            self.assertEqual((r.path, r.reason), (tr.TURN, "text"))
            self.assertEqual(r, tr.route(entry(text=text)))
        each(self, HOSTILE_TEXT, check)


if __name__ == "__main__":
    unittest.main()
