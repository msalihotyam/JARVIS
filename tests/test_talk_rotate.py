import ast
import io
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from jarvis import talk_rotate as tr

SOURCE = Path(__file__).resolve().parents[1] / "jarvis" / "talk_rotate.py"


def turn(tid, text="hi", **extra):
    return {"id": tid, "role": "salih", "text": text, **extra}


class RaisingMapping(Mapping):
    def __init__(self, exc):
        self.exc = exc

    def __getitem__(self, key):
        raise self.exc

    def __iter__(self):
        raise self.exc

    def __len__(self):
        return 1

    def get(self, key, default=None):
        raise self.exc


class DecideTests(unittest.TestCase):
    def test_keep_below_threshold(self):
        self.assertEqual(tr.decide(69, 100), tr.Decision(tr.KEEP, "BELOW_THRESHOLD"))

    def test_rotate_at_threshold(self):
        self.assertEqual(tr.decide(70, 100), tr.Decision(tr.ROTATE, "AT_THRESHOLD"))
        self.assertEqual(tr.decide(100, 100, 1.0).verdict, tr.ROTATE)
        self.assertEqual(tr.decide(40, 100, 0.4, [turn(1)]).verdict, tr.ROTATE)

    def test_pending_defers_at_any_usage(self):
        turns = [turn(1), turn(2, pending=True)]
        for used in (0, 50, 90, None):
            self.assertEqual(tr.decide(used, 100, turns=turns),
                             tr.Decision(tr.DEFER, "TURN_PENDING"))

    def test_bad_usage_rotates_never_keeps(self):
        cases = {None: "USED_INVALID", -1: "USED_INVALID", float("nan"): "USED_INVALID",
                 float("inf"): "USED_INVALID", True: "USED_INVALID", False: "USED_INVALID",
                 "10": "USED_INVALID", 101: "USED_OVER_LIMIT"}
        for used, reason in cases.items():
            got = tr.decide(used, 100, turns=[turn(1)])
            self.assertEqual(got, tr.Decision(tr.ROTATE, reason), repr(used))

    def test_bad_threshold_or_limit_refused(self):
        for th in (0, -0.1, 1.01, float("nan"), float("inf"), True, "0.7", None):
            self.assertEqual(tr.decide(1, 100, th), tr._refuse("THRESHOLD_INVALID"), repr(th))
        for lim in (0, -5, float("nan"), float("inf"), True, None, "100"):
            self.assertEqual(tr.decide(1, lim), tr._refuse("LIMIT_INVALID"), repr(lim))

    def test_turn_order_and_duplicates_refused(self):
        self.assertEqual(tr.decide(1, 100, turns=[turn(2), turn(1)]).code, "TURN_OUT_OF_ORDER")
        self.assertEqual(tr.decide(1, 100, turns=[turn(1), turn(1)]).code, "TURN_DUPLICATE_ID")
        self.assertEqual(tr.decide(1, 100, turns=[turn(1), turn(3), turn(1)]).code,
                         "TURN_DUPLICATE_ID")

    def test_wrong_types_refused(self):
        for turns in ("abc", None, {"id": 1}, (t for t in [turn(1)]), [1]):
            self.assertEqual(tr.decide(1, 100, turns=turns).code, "TURNS_INVALID")
        for bad in (turn(True), turn(-1), turn("1"), turn(1, text=None),
                    turn(1, topic=3), turn(1, pending="yes"), turn(1, approval_open=1),
                    {"id": 1, "text": "x"}):
            self.assertEqual(tr.decide(1, 100, turns=[bad]).code, "TURN_FIELD_INVALID")

    def test_deep_input_and_raising_mapping_refused(self):
        deep = []
        for _ in range(50000):
            deep = [deep]
        self.assertEqual(tr.decide(1, 100, turns=[turn(1, text=deep)]).code,
                         "TURN_FIELD_INVALID")
        got = tr.decide(1, 100, turns=[RaisingMapping(RecursionError())])
        self.assertEqual(got, tr._refuse("INPUT_TOO_DEEP"))
        got = tr.decide(1, 100, turns=[RaisingMapping(KeyError("x"))])
        self.assertEqual(got, tr._refuse("INPUT_INVALID"))

    def test_deterministic(self):
        turns = [turn(1), turn(5, topic="a")]
        self.assertEqual(tr.decide(70, 100, 0.7, turns), tr.decide(70, 100, 0.7, list(turns)))


class BundleTests(unittest.TestCase):
    SUMS = {"b": "sum-b", "a": "sum-a"}

    def test_parts_and_order(self):
        turns = [turn(1, topic="a"), turn(2, role="brain", topic="b"), turn(3)]
        b = tr.build_bundle("style", self.SUMS, turns, 10_000)
        self.assertEqual(b.style, "style")
        self.assertEqual(b.summaries, (("a", "sum-a"), ("b", "sum-b")))
        self.assertEqual([t["id"] for t in b.turns], [1, 2, 3])
        self.assertEqual(b.chars, 5 + 12 + 6)
        self.assertEqual(b.dropped, ())

    def test_last_50_turns_only(self):
        turns = [turn(i, text=str(i)) for i in range(80)]
        b = tr.build_bundle("s", {}, turns, 10_000)
        self.assertEqual([t["id"] for t in b.turns], list(range(30, 80)))

    def test_oldest_trimmed_first_exact_boundary(self):
        turns = [turn(i, text="x" * 10, topic="a") for i in range(5)]
        fixed = len("st") + len("a") + len("sum")
        b = tr.build_bundle("st", {"a": "sum"}, turns, fixed + 50)
        self.assertEqual((len(b.turns), b.dropped, b.chars), (5, (), fixed + 50))
        b = tr.build_bundle("st", {"a": "sum"}, turns, fixed + 49)
        self.assertEqual([t["id"] for t in b.turns], [1, 2, 3, 4])
        self.assertEqual((b.dropped, b.summaries), ((0,), (("a", "sum"),)))
        self.assertLessEqual(b.chars, fixed + 49)

    def test_open_approval_card_always_kept(self):
        turns = [turn(0, text="CARD", approval_open=True)]
        turns += [turn(i, text="y" * 10) for i in range(1, 80)]
        b = tr.build_bundle("", {}, turns, 4 + 20)
        self.assertEqual([t["id"] for t in b.turns], [0, 78, 79])
        self.assertTrue(b.turns[0]["approval_open"])
        b = tr.build_bundle("", {}, turns, 4)
        self.assertEqual([t["id"] for t in b.turns], [0])
        self.assertEqual(tr.build_bundle("", {}, turns, 3), tr._refuse("BUDGET_TOO_SMALL"))

    def test_summaries_over_budget_refused_not_truncated(self):
        got = tr.build_bundle("s", {"a": "x" * 100}, [turn(1, topic="a")], 50)
        self.assertEqual(got, tr._refuse("BUDGET_TOO_SMALL"))
        self.assertNotIsInstance(got, tr.Bundle)

    def test_untouched_summary_left_out(self):
        b = tr.build_bundle("s", {"a": "sa", "zz": "z" * 10}, [turn(1, topic="a")], 500)
        self.assertEqual((b.summaries, b.chars), ((("a", "sa"),), 1 + 3 + 2))
        b = tr.build_bundle("s", {"zz": "z"}, [turn(1)], 500)
        self.assertEqual((b.summaries, b.chars), ((), 1 + 2))
        self.assertEqual(tr.build_bundle("s", {"a": "sa"}, [], 500).summaries, ())

    def test_untouched_summary_not_counted_against_budget(self):
        b = tr.build_bundle("s", {"a": "sa", "zz": "z" * 100}, [turn(1, topic="a")], 50)
        self.assertEqual((b.summaries, b.chars, b.dropped), ((("a", "sa"),), 6, ()))

    def test_summary_kept_for_any_touching_turn(self):
        # outside the 50-turn window, trimmed for budget, or only an open card
        old = [turn(0, topic="old")] + [turn(i, text="x") for i in range(1, 60)]
        self.assertEqual(tr.build_bundle("", {"old": "o"}, old, 500).summaries, (("old", "o"),))
        trim = [turn(0, text="x" * 10, topic="t"), turn(1, text="y")]
        b = tr.build_bundle("", {"t": "s"}, trim, 3)
        self.assertEqual((b.summaries, b.dropped), ((("t", "s"),), (0,)))
        card = [turn(0, text="C", topic="c", approval_open=True)]
        self.assertEqual(tr.build_bundle("", {"c": "s", "d": "s"}, card, 3).summaries,
                         (("c", "s"),))

    def test_topic_match_exact(self):
        sums = {"A": "up", "a": "lo", "": "empty"}
        b = tr.build_bundle("", sums, [turn(1, topic="a"), turn(2, topic="")], 500)
        self.assertEqual(b.summaries, (("", "empty"), ("a", "lo")))
        got = tr.build_bundle("", {"A": "up"}, [turn(1, topic="a")], 500)
        self.assertEqual(got, tr._refuse("SUMMARY_MISSING"))
        self.assertEqual(tr.build_bundle("", {"a ": "x"}, [turn(1, topic="a")], 500),
                         tr._refuse("SUMMARY_MISSING"))

    def test_lying_mapping_views_refused(self):
        class Lying(Mapping):  # keys()/in say "a", items() disagrees
            def __init__(self, items):
                self._items = items

            def __getitem__(self, key):
                return "x"

            def __iter__(self):
                return iter(["a"])

            def __len__(self):
                return 1

            def __contains__(self, key):
                return True

            def items(self):
                return list(self._items)

        one = [turn(1, topic="a")]
        self.assertEqual(tr.build_bundle("", Lying([]), one, 500), tr._refuse("SUMMARY_MISSING"))
        self.assertEqual(tr.build_bundle("", Lying([("a", "1"), ("a", "22")]), one, 500),
                         tr._refuse("SUMMARIES_INVALID"))

    def test_untouched_bad_summary_still_refused(self):
        got = tr.build_bundle("s", {"a": "sa", "zz": None}, [turn(1, topic="a")], 500)
        self.assertEqual(got, tr._refuse("SUMMARIES_INVALID"))

    def test_bad_input_refused(self):
        cases = [
            ((None, {}, [], 10), "STYLE_INVALID"),
            (("s", [("a", "b")], [], 10), "SUMMARIES_INVALID"),
            (("s", {1: "b"}, [], 10), "SUMMARIES_INVALID"),
            (("s", {"a": None}, [], 10), "SUMMARIES_INVALID"),
            (("s", {}, [], 0), "BUDGET_INVALID"),
            (("s", {}, [], True), "BUDGET_INVALID"),
            (("s", {}, [], 10.0), "BUDGET_INVALID"),
            (("s", {}, [turn(1, topic="zz")], 10), "SUMMARY_MISSING"),
            (("s", {}, [turn(2), turn(1)], 10), "TURN_OUT_OF_ORDER"),
            (("s", {}, [turn(1), turn(1)], 10), "TURN_DUPLICATE_ID"),
            (("s", {}, "turns", 10), "TURNS_INVALID"),
            (("s", RaisingMapping(RecursionError()), [], 10), "INPUT_TOO_DEEP"),
        ]
        for args, code in cases:
            self.assertEqual(tr.build_bundle(*args), tr._refuse(code), code)

    def test_deterministic_across_insertion_order(self):
        turns = [turn(1, topic="a"), turn(2, topic="b")]
        one = tr.build_bundle("s", {"a": "1", "b": "2"}, turns, 100)
        two = tr.build_bundle("s", {"b": "2", "a": "1"}, list(turns), 100)
        self.assertEqual(one, two)


class PurityTests(unittest.TestCase):
    BANNED = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "requests",
              "shutil", "sqlite3", "importlib", "ctypes", "tempfile"}

    def test_no_io_or_network_imports(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                self.assertNotIn(name.split(".")[0], self.BANNED, name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "__import__", "eval", "exec"})

    def test_refusal_texts_ascii_under_cp1252_strict(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with mock.patch.object(sys, "stdout", out):
            for code, text in tr.REFUSALS.items():
                self.assertTrue(text.isascii() and text.isprintable(), code)
                print(code, tr._refuse(code).text)
            sys.stdout.flush()
        self.assertIn(b"BUDGET_TOO_SMALL", raw.getvalue())
