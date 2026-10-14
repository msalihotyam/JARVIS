"""SEC2BUDGET: pure memory injection token budget packer with kind priority (I18)."""

from __future__ import annotations

import unittest

from jarvis.contracts import MemoryRecordKind
from jarvis.memory_retrieval import (
    KIND_PRIORITY,
    MEMORY_INJECT_TOKEN_BUDGET,
    pack_for_budget,
)

K = MemoryRecordKind


def pack(items, **kwargs):
    """Items are (name, kind, cost) in rank order."""
    return pack_for_budget(items, kind_of=lambda i: i[1], cost=lambda i: i[2], **kwargs)


def names(items):
    return [i[0] for i in items]


class PackForBudgetTests(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual(MEMORY_INJECT_TOKEN_BUDGET, 1500)
        self.assertEqual(KIND_PRIORITY, (K.RULE, K.DECISION, K.PREFERENCE, K.FACT, K.LESSON))

    def test_over_budget_input_never_exceeds_budget_minus_reserved(self) -> None:
        items = [(f"r{n}", list(K)[n % 5], 37 + n % 11) for n in range(200)]
        for budget, reserved in ((1500, 0), (1500, 120), (300, 299), (50, 50), (0, 0)):
            packed, dropped = pack(items, budget=budget, reserved=reserved)
            self.assertLessEqual(sum(i[2] for i in packed), budget - reserved)
            self.assertCountEqual(packed + dropped, items)
        packed, dropped = pack(items)
        self.assertTrue(packed and dropped)
        self.assertLessEqual(sum(i[2] for i in packed), MEMORY_INJECT_TOKEN_BUDGET)

    def test_rule_wins_over_higher_ranked_fact(self) -> None:
        items = [("fact", K.FACT, 60), ("rule", K.RULE, 60)]
        packed, dropped = pack(items, budget=100)
        self.assertEqual(names(packed), ["rule"])
        self.assertEqual(names(dropped), ["fact"])

    def test_kind_priority_then_rank_order_unknown_last(self) -> None:
        items = [
            ("lesson", K.LESSON, 1),
            ("other", "unknown", 1),
            ("fact1", K.FACT, 1),
            ("pref", K.PREFERENCE, 1),
            ("fact2", K.FACT, 1),
            ("decision", K.DECISION, 1),
            ("rule1", K.RULE, 1),
            ("rule2", K.RULE, 1),
        ]
        packed, dropped = pack(items)
        self.assertEqual(
            names(packed),
            ["rule1", "rule2", "decision", "pref", "fact1", "fact2", "lesson", "other"],
        )
        self.assertEqual(dropped, ())

    def test_oversize_single_item_dropped_not_cut(self) -> None:
        big = ("big", K.RULE, MEMORY_INJECT_TOKEN_BUDGET + 1)
        packed, dropped = pack([big])
        self.assertEqual(packed, ())
        self.assertEqual(dropped, (big,))
        self.assertIs(dropped[0], big)

    def test_smaller_later_item_fits_after_skip(self) -> None:
        items = [("a", K.RULE, 70), ("b", K.RULE, 50), ("c", K.FACT, 30)]
        packed, dropped = pack(items, budget=100)
        self.assertEqual(names(packed), ["a", "c"])
        self.assertEqual(names(dropped), ["b"])

    def test_reserved_is_subtracted(self) -> None:
        packed, dropped = pack([("a", K.RULE, 80)], budget=100, reserved=21)
        self.assertEqual((names(packed), names(dropped)), ([], ["a"]))
        packed, _ = pack([("a", K.RULE, 80)], budget=100, reserved=20)
        self.assertEqual(names(packed), ["a"])

    def test_deterministic(self) -> None:
        items = [(f"r{n}", list(K)[(n * 7) % 5], 10 + (n * 13) % 90) for n in range(60)]
        first = pack(items)
        for _ in range(3):
            self.assertEqual(pack(list(items)), first)

    def test_bad_input_raises(self) -> None:
        for kwargs in (
            {"budget": -1},
            {"reserved": -1},
            {"budget": 10, "reserved": 11},
            {"budget": 1.5},
            {"budget": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                pack([], **kwargs)
        for size in (-1, 1.0, True, None):
            with self.subTest(size=size), self.assertRaises(ValueError):
                pack([("a", K.RULE, size)])


if __name__ == "__main__":
    unittest.main()
