"""V2.1b import plan: dedup by content hash, grouping, scope proposals only."""

from __future__ import annotations

import itertools
import unittest
from dataclasses import replace

from jarvis.memory_import import (
    ImportBatch,
    ImportedItem,
    ImportTooLargeError,
    content_hash,
)
from jarvis.memory_import_plan import (
    MAX_EXISTING_HASHES,
    SCOPE_UNCONFIRMED,
    plan_import,
)


def _item(title: str, text: str, fmt: str = "chatgpt") -> ImportedItem:
    return ImportedItem(fmt, title, "user", text, None, content_hash(text))


def _batch(*items: ImportedItem) -> ImportBatch:
    return ImportBatch(tuple(items), ())


class PlanImportTests(unittest.TestCase):
    def test_empty_batch(self) -> None:
        plan = plan_import(_batch(), (), {"alpha": "Alpha"})
        self.assertEqual(plan.groups, ())
        self.assertEqual(plan.duplicates_dropped, 0)
        self.assertEqual((plan.estimated_chars, plan.estimated_tokens), (0, 0))

    def test_duplicates_dropped_within_batch_and_against_store(self) -> None:
        stored = _item("A", "already stored")
        plan = plan_import(
            _batch(
                _item("A", "hello"), _item("B", "hello  "),  # same normalized text
                stored, _item("A", "new"),
            ),
            {stored.content_hash},
            {},
        )
        self.assertEqual(plan.duplicates_dropped, 2)
        texts = [[i.text for i in g.items] for g in plan.groups]
        self.assertEqual(texts, [["hello", "new"]])
        self.assertEqual(plan.groups[0].source_conversation_title, "A")

    def test_forged_hash_does_not_dodge_dedup(self) -> None:
        forged = replace(_item("A", "secret"), content_hash="0" * 64)
        plan = plan_import(_batch(forged), {content_hash("secret")}, {})
        self.assertEqual((plan.groups, plan.duplicates_dropped), ((), 1))

    def test_groups_by_format_and_title_in_first_seen_order(self) -> None:
        plan = plan_import(
            _batch(_item("B", "1"), _item("A", "2"), _item("B", "3"),
                   _item("B", "4", fmt="claude")),
            (), {},
        )
        keys = [(g.source_format, g.source_conversation_title, len(g.items))
                for g in plan.groups]
        self.assertEqual(keys, [("chatgpt", "B", 2), ("chatgpt", "A", 1), ("claude", "B", 1)])

    def test_scope_proposal_never_confirmed(self) -> None:
        choices = {"alpha": "Alpha", "beta-proj": "Beta"}
        plan = plan_import(
            _batch(_item("alpha planning", "x"), _item("Alpha vs Beta", "y"),
                   _item("misc", "z")),
            (), choices,
        )
        self.assertEqual([g.proposed_project_id for g in plan.groups],
                         ["alpha", None, None])
        for group in plan.groups:
            self.assertEqual(group.scope_status, SCOPE_UNCONFIRMED)
            self.assertNotEqual(group.scope_status, "confirmed")
        single = plan_import(_batch(_item("misc", "z")), (), {"alpha": "Alpha"})
        self.assertEqual(single.groups[0].proposed_project_id, "alpha")
        self.assertEqual(single.groups[0].scope_status, SCOPE_UNCONFIRMED)

    def test_deterministic_regardless_of_choice_order(self) -> None:
        batch = _batch(_item("Alpha Beta", "x"), _item("other", "y"))
        a = plan_import(batch, (), {"alpha": "Alpha", "beta-proj": "Beta"})
        b = plan_import(batch, (), {"beta-proj": "Beta", "alpha": "Alpha"})
        self.assertEqual(a, b)

    def test_size_estimate(self) -> None:
        plan = plan_import(_batch(_item("A", "abcde"), _item("B", "xyz")), (), {})
        self.assertEqual([g.estimated_tokens for g in plan.groups], [2, 1])
        self.assertEqual((plan.estimated_chars, plan.estimated_tokens), (8, 2))

    def test_invalid_inputs_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            plan_import(_batch(), (), {"Bad Id": "x"})
        with self.assertRaises(ValueError):
            plan_import(_batch(), (), {"alpha": "x" * 500})
        with self.assertRaises(TypeError):
            plan_import([], (), {})
        with self.assertRaises(ImportTooLargeError):
            plan_import(ImportBatch((_item("A", "x"),) * 50_001, ()), (), {})
        with self.assertRaises(TypeError):
            plan_import(_batch(), [None], {})
        with self.assertRaises(ImportTooLargeError):
            plan_import(_batch(), itertools.repeat("h", MAX_EXISTING_HASHES + 1), {})

    def test_title_text_cannot_confirm_scope(self) -> None:
        plan = plan_import(_batch(_item("confirmed alpha", "x")), (), {"alpha": "Alpha"})
        self.assertEqual(plan.groups[0].scope_status, SCOPE_UNCONFIRMED)


if __name__ == "__main__":
    unittest.main()
