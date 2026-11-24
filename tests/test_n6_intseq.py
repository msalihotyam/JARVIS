"""N6G27INTSEQ: G27 integration sequencing (R31), shadow only. Pure functions: no clock, no I/O."""

from __future__ import annotations

import itertools
import unittest
from typing import Any

from jarvis.n6_intseq import compare_dispatch, sequence

MAIN = "a" * 40
OLD = "b" * 40
_n = itertools.count(1)


def sha() -> str:
    return f"{next(_n):040x}"


def cand(sid: str, files: list[str], *, key: str = "auto", age: int = 10,
         protected: bool = False, base: str = MAIN, **extra: Any) -> dict[str, Any]:
    c: dict[str, Any] = {"id": sid, "picks": [sha()], "files": files, "key": key,
                         "protected": protected, "patch_ids": [sha()], "age_s": age, "base": base}
    c.update(extra)
    return c


def verdicts(seq: Any) -> dict[str, str]:
    return {i: v for i, v, _ in seq.verdicts}


class N6IntSeqTests(unittest.TestCase):
    def test_o13_manual_blocks_overlapping_auto(self) -> None:
        seq = sequence([cand("INT-A", ["jarvis/x.py"], age=1),
                        cand("INT-M", ["jarvis/x.py", "y.py"], key="manual", age=5)], MAIN)
        self.assertEqual(seq.ordered, ("INT-M",))
        self.assertEqual(seq.verdict("INT-A"),
                         ("held", "O13: manual-key INT INT-M overlaps jarvis/x.py"))

    def test_o13_holds_even_when_manual_is_not_ordered(self) -> None:
        # manual needs a rebase: still blocks the overlapping auto; a disjoint auto goes
        seq = sequence([cand("INT-M", ["x.py"], key="manual", base=OLD),
                        cand("INT-A", ["x.py"]), cand("INT-B", ["z.py"])], MAIN)
        self.assertEqual(verdicts(seq), {"INT-M": "needs_rebase", "INT-A": "held",
                                         "INT-B": "ordered"})
        # manual in the other protected class: not batched, still blocks
        seq = sequence([cand("INT-P", ["x.py"], key="manual", protected=True, age=1),
                        cand("INT-Q", ["q.py"], key="manual", age=9),
                        cand("INT-A", ["x.py"], age=50)], MAIN)
        self.assertEqual(verdicts(seq), {"INT-P": "held", "INT-Q": "ordered", "INT-A": "held"})
        self.assertIn("O13", seq.verdict("INT-A")[1])  # type: ignore[index]

    def test_disjoint_scopes_batch_overlap_held(self) -> None:
        seq = sequence([cand("INT-1", ["a.py"], age=30), cand("INT-2", ["b.py"], age=20),
                        cand("INT-3", ["a.py", "c.py"], age=10)], MAIN)
        self.assertEqual(seq.ordered, ("INT-1", "INT-2"))
        self.assertEqual(seq.verdict("INT-3"), ("held", "scope overlaps INT-1 on a.py"))

    def test_slice_and_own_fix_may_share_a_file(self) -> None:
        seq = sequence([cand("INT-X", ["a.py"], age=30), cand("INT-XFIX", ["a.py"], age=20)],
                       MAIN)
        self.assertEqual(seq.ordered, ("INT-X", "INT-XFIX"))

    def test_shared_contract_held(self) -> None:
        seq = sequence([cand("INT-1", ["a.py"], age=30, contracts=["ledger-v1"]),
                        cand("INT-2", ["b.py"], age=20, contracts=["ledger-v1", "z"]),
                        cand("INT-3", ["c.py"], age=10, contracts=["other"])], MAIN)
        self.assertEqual(seq.ordered, ("INT-1", "INT-3"))
        self.assertEqual(seq.verdict("INT-2"), ("held", "shares contract ledger-v1 with INT-1"))

    def test_protected_never_batched_with_unprotected(self) -> None:
        seq = sequence([cand("INT-P", ["p.py"], key="manual", protected=True, age=99),
                        cand("INT-U", ["u.py"], key="manual", age=5),
                        cand("INT-V", ["v.py"], age=50)], MAIN)
        self.assertEqual(seq.ordered, ("INT-P",))
        self.assertEqual(verdicts(seq), {"INT-P": "ordered", "INT-U": "held", "INT-V": "held"})
        self.assertIn("protected class", seq.verdict("INT-V")[1])  # type: ignore[index]

    def test_protected_with_auto_key_is_unknown(self) -> None:
        seq = sequence([cand("INT-P", ["p.py"], protected=True)], MAIN)
        self.assertEqual(seq.verdict("INT-P"), ("unknown", "a protected range with an auto key"))
        self.assertEqual(seq.ordered, ())

    def test_manual_first_then_oldest_first(self) -> None:
        seq = sequence([cand("INT-A1", ["1.py"], age=5), cand("INT-A2", ["2.py"], age=500),
                        cand("INT-M1", ["3.py"], key="manual", age=1),
                        cand("INT-M2", ["4.py"], key="manual", age=7)], MAIN)
        self.assertEqual(seq.ordered, ("INT-M2", "INT-M1", "INT-A2", "INT-A1"))

    def test_needs_rebase_never_ordered(self) -> None:
        seq = sequence([cand("INT-R", ["r.py"], base=OLD, age=999), cand("INT-S", ["s.py"])],
                       MAIN)
        self.assertEqual(seq.ordered, ("INT-S",))
        self.assertEqual(seq.verdict("INT-R"), ("needs_rebase", "base bbbbbbb is not main aaaaaaa"))

    def test_malformed_fields_are_unknown_never_guessed(self) -> None:
        bad: list[dict[str, Any]] = [
            cand("INT-1", ["a.py"], key="maybe"), cand("INT-2", "a.py"),  # type: ignore[arg-type]
            cand("INT-3", ["../x.py"]), cand("INT-4", ["C:/x.py"]), cand("INT-5", ["a.py"], age=-1),
            cand("INT-6", ["a.py"], age=True), cand("INT-7", ["a.py"], protected=1),
            cand("INT-8", ["a.py"], base="main"), cand("INT-9", ["a.py"], contracts=[""]),
            cand("INT-10", ["a.py"], stray=1), cand("SLICE1", ["a.py"]),
            cand("INT-11", ["a.py"], patch_ids=[]), cand("INT-12", ["a.py"], picks=["x"]),
        ]
        no_base = cand("INT-13", ["a.py"], key="manual")
        del no_base["base"]
        seq = sequence([*bad, no_base, 7], MAIN)
        self.assertEqual(seq.ordered, ())
        self.assertEqual(set(verdicts(seq).values()), {"unknown"})
        self.assertEqual(len(seq.verdicts), 15)
        self.assertIn("<candidate 14>", verdicts(seq))

    def test_unknown_candidate_holds_auto_keys(self) -> None:
        seq = sequence([cand("INT-?", ["a.py"], key=None), cand("INT-A", ["z.py"]),
                        cand("INT-M", ["m.py"], key="manual")], MAIN)
        self.assertEqual(verdicts(seq), {"INT-?": "unknown", "INT-A": "held", "INT-M": "ordered"})
        self.assertTrue(seq.attention)

    def test_malformed_main_makes_all_unknown(self) -> None:
        for main in (None, "abc", MAIN.upper()):
            seq = sequence([cand("INT-1", ["a.py"])], main)
            self.assertEqual((seq.ordered, verdicts(seq)), ((), {"INT-1": "unknown"}))

    def test_duplicate_id_and_shared_patch_id_unknown(self) -> None:
        a, b = cand("INT-1", ["a.py"], key="manual"), cand("INT-1", ["b.py"], key="manual")
        seq = sequence([a, b, cand("INT-2", ["c.py"], key="manual")], MAIN)
        self.assertEqual([v for _, v, _ in seq.verdicts], ["unknown", "unknown", "ordered"])
        c = cand("INT-3", ["d.py"], key="manual")
        d = cand("INT-4", ["e.py"], key="manual", patch_ids=list(c["patch_ids"]))
        self.assertEqual(verdicts(sequence([c, d], MAIN)), {"INT-3": "unknown",
                                                            "INT-4": "unknown"})

    def test_path_separator_and_case_are_one_file(self) -> None:
        seq = sequence([cand("INT-M", ["Jarvis\\Core.py"], key="manual"),
                        cand("INT-A", ["jarvis/./core.py"])], MAIN)
        self.assertEqual(seq.verdict("INT-A"),
                         ("held", "O13: manual-key INT INT-M overlaps jarvis/core.py"))

    def test_input_order_does_not_change_the_result(self) -> None:
        cands = [cand("INT-1", ["a.py"], age=3), cand("INT-2", ["a.py"], age=3),
                 cand("INT-3", ["b.py"], key="manual", age=1),
                 cand("INT-4", ["b.py", "c.py"], age=9), cand("INT-5", ["d.py"], age=3)]
        first = sequence(cands, MAIN)
        for perm in itertools.permutations(cands):
            seq = sequence(perm, MAIN)
            self.assertEqual(seq.ordered, first.ordered)
            self.assertEqual(set(seq.verdicts), set(first.verdicts))
        self.assertEqual(first.ordered, ("INT-3", "INT-1", "INT-5"))

    def test_max_batch(self) -> None:
        cands = [cand(f"INT-{i}", [f"{i}.py"], age=100 - i) for i in range(8)]
        seq = sequence(cands, MAIN)
        self.assertEqual(seq.ordered, tuple(f"INT-{i}" for i in range(6)))
        self.assertEqual(seq.verdict("INT-7"), ("held", "batch is full (6)"))
        self.assertEqual(sequence(cands, MAIN, max_batch=1).ordered, ("INT-0",))
        self.assertEqual(set(verdicts(sequence(cands, MAIN, max_batch=0)).values()), {"unknown"})


def dispatch(*lanes: object) -> list[dict[str, object]]:
    return [{"event": "dispatch", "lane": lane, "id": "x"} for lane in lanes]


class N6IntSeqShadowTests(unittest.TestCase):
    seq = sequence([cand("INT-1", ["a.py"], age=30), cand("INT-2", ["b.py"], age=20),
                    cand("INT-3", ["a.py"], age=10)], MAIN)

    def test_agree_on_same_order(self) -> None:
        events = [{"event": "finish", "lane": "INT-2"}, *dispatch("SLICE9", "INT-1", "INT-2",
                                                                 "INT-1", "INT-OTHER")]
        shadow = compare_dispatch(self.seq, events)
        self.assertEqual(shadow.verdict, "agree", shadow.reason)
        self.assertEqual(shadow.conveyor, ("INT-1", "INT-2", "INT-OTHER"))

    def test_disagree_on_order(self) -> None:
        shadow = compare_dispatch(self.seq, dispatch("INT-2", "INT-1"))
        self.assertEqual(shadow.verdict, "disagree")

    def test_disagree_when_held_candidate_dispatched(self) -> None:
        shadow = compare_dispatch(self.seq, dispatch("INT-3"))
        self.assertEqual(shadow.verdict, "disagree")
        self.assertIn("scope overlaps INT-1", shadow.reason)

    def test_unknown_without_shared_or_on_malformed(self) -> None:
        self.assertEqual(compare_dispatch(self.seq, dispatch("PRINTX", "INTERIM")).verdict,
                         "unknown")
        self.assertEqual(compare_dispatch(self.seq, dispatch("INT-1", 5)).verdict, "unknown")
        self.assertEqual(compare_dispatch(self.seq, ["not an event"]).verdict, "unknown")


if __name__ == "__main__":
    unittest.main()
