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

    def assertForwarded(self, e):
        r = tr.route(e)
        self.assertEqual((r.path, r.reason, r.mode, r.note), (tr.TURN, "forwarded", None, None))

    def test_forwarded_get_and_contains_disagree_is_turn(self):
        class Hides(dict):  # value present, membership denies it (CDX-TALKROUTE1 F1)
            def __contains__(self, key):
                return False if key == "forwarded" else super().__contains__(key)

        class Claims(dict):  # membership claims it, get reports the caller default
            def __contains__(self, key):
                return key == "forwarded" or super().__contains__(key)

            def get(self, key, default=None):
                return default if key == "forwarded" else super().get(key, default)

        class View(Mapping):  # non-dict Mapping, membership denies a present value
            def __init__(self, data):
                self.data = data

            def __getitem__(self, key):
                return self.data[key]

            def __iter__(self):
                return iter(self.data)

            def __len__(self):
                return len(self.data)

            def __contains__(self, key):
                return False

        for text in (LABEL, "Yazılı yanıt", "/start"):
            for fwd in (True, 1, "yes", None, [], 0):
                with self.subTest(text=text, fwd=repr(fwd)):
                    self.assertForwarded(Hides(entry(text=text, forwarded=fwd)))
                    self.assertForwarded(View(entry(text=text, forwarded=fwd)))
            with self.subTest(text=text, claims=True):
                self.assertForwarded(Claims(entry(text=text)))
        # explicit False through get is still honoured even if membership denies it
        self.assertEqual(tr.route(Hides(entry(text=LABEL, forwarded=False))).path, tr.MODE)

    def test_forwarded_read_once(self):
        class Flips(dict):  # first get says True, any later get says False
            calls = 0

            def get(self, key, *default):
                if key != "forwarded":
                    return super().get(key, *default)
                Flips.calls += 1
                return Flips.calls == 1

        self.assertForwarded(Flips(entry(text=LABEL)))
        self.assertEqual(Flips.calls, 1)

    def test_forwarded_membership_raises_refused(self):
        class Raises(dict):
            def __contains__(self, key):
                raise RuntimeError("no")

        self.assertRefused(tr.route(Raises(entry(text=LABEL))), "bad_entry")
        self.assertForwarded(Raises(entry(text=LABEL, forwarded=True)))  # value decides; no membership asked

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
            self.assertIn(r.path, (tr.TURN, tr.REFUSED))
            self.assertEqual(r, tr.route(entry(text=text)))
        each(self, HOSTILE_TEXT, check)


if __name__ == "__main__":
    unittest.main()
