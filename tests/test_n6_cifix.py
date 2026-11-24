"""N6CIFIX1: fix-chain start decision from a red CI verdict (proposal only, shadow)."""

from __future__ import annotations

import copy
import re
import unittest
from pathlib import Path

from jarvis import n6_cifix, work_supervisor
from jarvis.n6_cifix import NOTE, REASONS, decide

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
ID = "tests.test_alpha.AlphaTests.test_one"
CAND = {"event": "int_candidate", "int": "INT94", "sha": SHA}
REC = {"event": "fix_proposed", "int": "INT94", "sha": SHA, "failing": [ID], "chain": 1}


def verdict(**over: object) -> dict[str, object]:
    base: dict[str, object] = {"int": "INT94", "sha": SHA, "failing": [ID],
                               "source": "own_ci_run", "ci": "red", "requester": "INT94"}
    base.update(over)
    return base


def ledger(*rows: object):
    return lambda: [copy.deepcopy(r) for r in rows]


def fixes(n: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for i in range(1, n + 1):
        rows += [{"event": "fix_lane", "int": "INT94", "lane": f"INT94F{i}"},
                 {"event": "fix_failed", "int": "INT94", "lane": f"INT94F{i}"}]
    return rows


class _Base(unittest.TestCase):
    def refused(self, result: n6_cifix.Result, reason: str) -> None:
        self.assertEqual(result, n6_cifix.Result("refused", reason))
        self.assertIn(reason, REASONS)


class AcceptanceTests(_Base):
    def test_red_verdict_proposes_one_chain(self) -> None:
        r = decide(verdict(), ledger(CAND))
        self.assertEqual((r.outcome, r.reason), ("propose", "proposed"))
        self.assertEqual(r.proposal, {"int": "INT94", "sha": SHA, "failing": [ID],
                                      "chain": 1, "note": "no key1, cannot start a chain"})
        self.assertEqual(r.record, REC)

    def test_note_text_is_exact(self) -> None:
        self.assertEqual(NOTE, "no key1, cannot start a chain")

    def test_reader_is_the_only_io_and_inputs_are_not_mutated(self) -> None:
        calls: list[int] = []
        rows = [CAND]
        v = verdict()
        snapshot = copy.deepcopy((v, rows))

        def read() -> list[object]:
            calls.append(1)
            return rows

        decide(v, read)
        self.assertEqual(calls, [1])
        self.assertEqual((v, rows), snapshot)

    def test_fix_cap_is_imported_not_redefined(self) -> None:
        self.assertIs(n6_cifix.FIX_CAP, work_supervisor.FIX_CAP)
        self.assertIs(n6_cifix.fix_chain, work_supervisor.fix_chain)
        source = Path(n6_cifix.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*FIX_CAP\s*[:=]", source, re.MULTILINE))

    def test_second_failed_fix_needs_a_decision(self) -> None:
        r = decide(verdict(), ledger(CAND, REC, *fixes(2)))
        self.assertEqual(r, n6_cifix.Result("decision_needed", "fix_cap_reached"))
        self.assertIn(r.reason, REASONS)

    def test_more_failed_fixes_never_a_third_fix(self) -> None:
        r = decide(verdict(), ledger(CAND, REC, *fixes(3)))
        self.assertEqual(r.outcome, "decision_needed")

    def test_one_failed_fix_does_not_start_another_chain(self) -> None:
        self.refused(decide(verdict(), ledger(CAND, REC, *fixes(1))), "chain_exists")

    def test_restart_same_input_same_result_no_second_row(self) -> None:
        first = decide(verdict(), ledger(CAND))
        self.assertEqual(decide(verdict(), ledger(CAND)), first)
        again = decide(verdict(), ledger(CAND, first.record))
        self.assertEqual((again.outcome, again.reason, again.proposal),
                         (first.outcome, first.reason, first.proposal))
        self.assertIsNone(again.record)
        self.assertEqual(decide(verdict(), ledger(CAND, first.record)), again)


class NegativeTests(_Base):
    def test_1_no_failing_ids_and_no_own_ci_run(self) -> None:
        self.refused(decide(verdict(failing=[], source="manual"), ledger(CAND)), "not_own_ci_run")
        self.refused(decide(verdict(failing=[]), ledger(CAND)), "no_failing_ids")
        self.refused(decide(verdict(source="other_run"), ledger(CAND)), "not_own_ci_run")

    def test_2_second_chain_for_same_int_name_variants(self) -> None:
        other = ["tests.test_beta"]
        for name in ("INT94", "int94", "Int94", "INT94 ", " INT94", "INT94.", "INT94. ."):
            with self.subTest(name=name):
                self.refused(decide(verdict(int=name, failing=other), ledger(CAND, REC)),
                             "chain_exists")

    def test_2_unicode_look_alike_int_names_refused(self) -> None:
        for name in ("ІNT94", "ＩＮＴ94", "INT94\u200b", "INTΙ" + "94",
                     "INT94 ", "ÍNT94", "INT٩٤"):
            with self.subTest(name=ascii(name)):
                self.refused(decide(verdict(int=name), ledger(CAND)), "bad_int")
                row = dict(CAND, int=name)
                self.refused(decide(verdict(), ledger(CAND, row)), "ledger_ambiguous")

    def test_3_sha_mismatch_or_malformed(self) -> None:
        self.refused(decide(verdict(sha="b" * 40), ledger(CAND)), "sha_mismatch")
        for sha in ("A" * 40, "a" * 39, "a" * 41, "g" * 40, " " + "a" * 39, "a" * 40 + "\n",
                    None, 7, ["a" * 40]):
            with self.subTest(sha=sha):
                self.refused(decide(verdict(sha=sha), ledger(CAND)), "bad_sha")

    def test_4_only_known_red_ids_failing(self) -> None:
        self.refused(decide(verdict(), ledger(CAND), known_red=[ID]), "only_known_red")
        self.refused(decide(verdict(), ledger(CAND), known_red=("tests.test_alpha",)),
                     "only_known_red")
        mixed = decide(verdict(failing=[ID, "tests.test_beta"]), ledger(CAND),
                       known_red=["tests.test_alpha"])
        self.assertEqual(mixed.proposal["failing"], ["tests.test_beta"])
        self.assertEqual(decide(verdict(), ledger(CAND), known_red=["tests.test_alph"]).outcome,
                         "propose")
        for bad in ("tests.test_alpha", [["x"]], [None], ["..\\x"], {"a": 1}):
            with self.subTest(bad=bad):
                self.refused(decide(verdict(), ledger(CAND), known_red=bad), "bad_verdict")

    def test_5_ci_api_error_missing_workflow_base_red_wait(self) -> None:
        for ci, reason in (("api_error", "wait_ci_api_error"),
                           ("missing_workflow", "wait_missing_workflow"),
                           ("base_red", "wait_base_red")):
            with self.subTest(ci=ci):
                self.refused(decide(verdict(ci=ci, failing=[]), ledger(CAND)), reason)
        for ci in ("green", "RED", "", None, ["red"]):
            with self.subTest(ci=ci):
                self.refused(decide(verdict(ci=ci), ledger(CAND)), "bad_verdict")

    def test_6_requester_is_a_fix_lane(self) -> None:
        for lane in ("INT94FIX1", "n6fix", "CDX_Fix_2"):
            with self.subTest(lane=lane):
                self.refused(decide(verdict(requester=lane), ledger(CAND)),
                             "requester_is_fix_lane")
        rows = (CAND, {"event": "fix_lane", "int": "INT7", "lane": "LANE7"})
        self.refused(decide(verdict(requester="lane7"), ledger(*rows)), "requester_is_fix_lane")
        for bad in ("", " x", "a/b", "..", None, ["INT94"], "x" * 65):
            with self.subTest(bad=bad):
                self.refused(decide(verdict(requester=bad), ledger(CAND)), "bad_requester")

    def test_7_malformed_failing_ids_refused_never_echoed(self) -> None:
        for raw in ("", " ", "\t", "..\\x", "C:\\x", "a:b", "/etc/x", "tests.test_a\u202e",
                    "tests.test_a\x00", "tests.test_a\n", "tests.test_a b", "tests.test_\u0430",
                    "tests.test_a;rm", "tests.test_a.."):
            with self.subTest(raw=ascii(raw)):
                r = decide(verdict(failing=[ID, raw]), ledger(CAND))
                self.refused(r, "bad_failing_ids")
                if raw.strip():
                    self.assertNotIn(raw, repr(r))
        many = [f"tests.test_m{i}" for i in range(10_000)]
        self.refused(decide(verdict(failing=many), ledger(CAND)), "too_many_ids")
        self.refused(decide(verdict(failing=many[:201]), ledger(CAND)), "too_many_ids")
        self.refused(decide(verdict(failing=[ID] * 10_000), ledger(CAND)), "too_many_ids")
        dup = decide(verdict(failing=[ID, ID, "tests.test_b", ID]), ledger(CAND))
        self.assertEqual(dup.proposal["failing"], [ID, "tests.test_b"])
        for bad in ("tests.test_b", {ID: 1}, None, 5):
            with self.subTest(bad=bad):
                self.refused(decide(verdict(failing=bad), ledger(CAND)), "bad_failing_ids")

    def test_8_deeply_nested_or_wrong_type_verdict(self) -> None:
        deep: list[object] = []
        for _ in range(100_000):
            deep = [deep]
        self.refused(decide(deep, ledger(CAND)), "bad_verdict")
        self.refused(decide(verdict(failing=deep), ledger(CAND)), "bad_failing_ids")
        self.refused(decide(verdict(int=deep), ledger(CAND)), "bad_int")
        self.refused(decide(verdict(int=["INT94"]), ledger(CAND)), "bad_int")
        self.refused(decide(verdict(sha={"a": 1}), ledger(CAND)), "bad_sha")
        self.refused(decide(verdict(source=["own_ci_run"]), ledger(CAND)), "not_own_ci_run")
        for v in (None, "INT94", [verdict()], dict(verdict(), extra=1),
                  {k: v for k, v in verdict().items() if k != "ci"}):
            with self.subTest(v=type(v).__name__):
                self.refused(decide(v, ledger(CAND)), "bad_verdict")

        class Sneaky(dict):
            pass

        self.refused(decide(Sneaky(verdict()), ledger(CAND)), "bad_verdict")

        def recurse() -> list[object]:
            raise RecursionError("deep")

        self.refused(decide(verdict(), recurse), "ledger_unreadable")
        self.refused(decide(verdict(), lambda: [CAND, deep]), "ledger_ambiguous")

    def test_9_ledger_unreadable_or_ambiguous(self) -> None:
        def broken() -> list[object]:
            raise OSError("disk")

        self.refused(decide(verdict(), broken), "ledger_unreadable")
        for raw in (None, "rows", {"event": "x"}, iter([CAND]), 5):
            with self.subTest(raw=type(raw).__name__):
                self.refused(decide(verdict(), lambda raw=raw: raw), "ledger_unreadable")
        self.refused(decide(verdict(), "not callable"), "ledger_unreadable")
        other = dict(CAND, sha="b" * 40)
        for rows in ((CAND, other), (CAND, {"event": "int_retired", "int": "INT94"}),
                     (CAND, *fixes(1)), (CAND, REC, dict(REC, failing=["tests.test_b"])),
                     (CAND, dict(CAND, note="x")), (CAND, {"int": "INT94", "sha": SHA})):
            with self.subTest(rows=rows):
                self.refused(decide(verdict(), ledger(*rows)), "ledger_ambiguous")
        self.refused(decide(verdict(), ledger()), "no_candidate")
        self.refused(decide(verdict(), ledger(dict(CAND, int="INT95"))), "no_candidate")


class FailureClassTests(_Base):
    """Failure classes from the dropped N6G23AUTOFIX reviews."""

    def test_non_normalized_paths_refused_not_normalized_past_a_check(self) -> None:
        for raw in ("tests/./test_a", "tests//test_a", "tests\\.\\test_a", "tests.test_a.",
                    "tests.test_a/./b", "tests.test_a\\b", "tests..test_a", "./tests.test_a"):
            with self.subTest(raw=raw):
                r = decide(verdict(failing=[raw]), ledger(CAND))
                self.refused(r, "bad_failing_ids")
                self.assertNotIn(raw, repr(r))
        for name in ("INT94/./x", "INT94//x", "INT94\\.\\x", "data\\INT94", "INT94/"):
            with self.subTest(name=name):
                self.refused(decide(verdict(int=name), ledger(CAND)), "bad_int")
        self.assertEqual(n6_cifix.int_key("INT94. "), n6_cifix.int_key("int94"))

    def test_sqlite_sidecars_beyond_db_dash(self) -> None:
        for raw in ("tests.test_a.sqlite-wal", "tests.test_a.sqlite3-shm",
                    "tests.test_a.sqlite-journal", "tests.test_a.db-wal", "tests.test_jarvis.db",
                    "tests.test_a.sqlite3", "tests.test_a.sqlite", "tests.test_a.DB",
                    "tests.test_a.Wal", "tests.test_a.Shm", "tests.test_a.Journal"):
            with self.subTest(raw=raw):
                self.refused(decide(verdict(failing=[ID, raw]), ledger(CAND)), "bad_failing_ids")
                self.refused(decide(verdict(), ledger(CAND, dict(REC, failing=[raw]))),
                             "ledger_ambiguous")

    def test_unhashable_or_non_str_event_fields_typed_refusal(self) -> None:
        bad_rows = (
            dict(CAND, event=["int_candidate"]), dict(CAND, event={"a": 1}), dict(CAND, event=1),
            dict(CAND, int=["INT94"]), dict(CAND, int={"INT94": 1}), dict(CAND, sha=[SHA]),
            dict(REC, failing=ID), dict(REC, failing={ID: 1}), dict(REC, failing=[[ID]]),
            dict(REC, chain="1"), dict(REC, chain=True), dict(REC, chain=2), dict(REC, chain=[1]),
            {"event": "fix_lane", "int": "INT94", "lane": {"x": 1}},
            {"event": "fix_failed", "int": "INT94", "lane": ["L1"]},
            {"event": {"k": "v"}, "int": "INT94"},
        )
        for row in bad_rows:
            with self.subTest(row=row):
                self.refused(decide(verdict(), ledger(CAND, REC, row)), "ledger_ambiguous")

    def test_malformed_sibling_never_shrinks_the_count(self) -> None:
        bad_sibling = {"event": "fix_failed", "int": "INT94", "lane": None}
        self.refused(decide(verdict(), ledger(CAND, REC, *fixes(1), bad_sibling)),
                     "ledger_ambiguous")
        other = {"event": "fix_failed", "int": ["INT7"], "lane": "INT7F1"}
        self.refused(decide(verdict(), ledger(CAND, other)), "ledger_ambiguous")
        self.refused(decide(verdict(), ledger(CAND, "junk")), "ledger_ambiguous")
        self.refused(decide(verdict(), ledger(CAND, None)), "ledger_ambiguous")
        dup = fixes(1)[1]
        r = decide(verdict(), ledger(CAND, REC, *fixes(1), dup))
        self.assertEqual(r.outcome, "decision_needed")  # duplicates count, never dropped

    def test_recognized_event_with_non_str_lane_refused(self) -> None:
        for lane in (5, None, b"L1", ("L1",), 1.0):
            for event in ("fix_lane", "fix_failed"):
                with self.subTest(lane=lane, event=event):
                    row = {"event": event, "int": "INT94", "lane": lane}
                    self.refused(decide(verdict(), ledger(CAND, REC, row)), "ledger_ambiguous")

    def test_every_refusal_reason_is_declared(self) -> None:
        self.assertEqual(len(set(REASONS)), len(REASONS))
        self.refused(decide(verdict(), lambda: [CAND], known_red=iter(())), "bad_verdict")


class ShadowTests(unittest.TestCase):
    def test_nothing_in_the_app_or_scripts_imports_the_module(self) -> None:
        own = Path(n6_cifix.__file__).resolve()
        hits = [str(p.relative_to(ROOT)) for top in ("jarvis", "scripts")
                for p in (ROOT / top).rglob("*.py")
                if p.resolve() != own and "n6_cifix" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(hits, [])

    def test_module_has_no_io_or_clock_imports(self) -> None:
        source = Path(n6_cifix.__file__).read_text(encoding="utf-8")
        for name in ("sqlite3", "subprocess", "socket", "urllib", "time", "datetime", "os",
                     "pathlib", "requests", "io", "shutil"):
            with self.subTest(name=name):
                self.assertIsNone(re.search(rf"^\s*(?:import|from)\s+{name}\b", source, re.MULTILINE))
        self.assertIsNone(re.search(r"\bopen\(", source))


if __name__ == "__main__":
    unittest.main()
