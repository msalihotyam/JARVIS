"""N6G22: the CI watch port (R17), pure over fixture text; no clock, no files, no network."""

from __future__ import annotations

import json
import unittest
from typing import Any

from jarvis.n6_ci_watch import (
    VERDICTS,
    CiVerdict,
    KnownRed,
    MalformedInput,
    ci_verdict,
    compare_finish,
    failures,
)

SHA = "a" * 40
OTHER = "b" * 40
REQUIRED = ("shard 1/2", "shard 2/2", "aggregate")

FAIL_TEXT = """======================================================================
FAIL: test_x (tests.test_a.A.test_x)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "x.py", line 1, in test_x
AssertionError: 1 != 2

======================================================================
ERROR: test_y (tests.test_b.B.test_y)
----------------------------------------------------------------------
Traceback (most recent call last):
KeyError: 'k'

----------------------------------------------------------------------
Ran 2 tests in 0.100s

FAILED (failures=1, errors=1)
"""
X = KnownRed("tests.test_a.A.test_x", "AssertionError: 1 != 2")
Y = KnownRed("tests.test_b.B.test_y", "KeyError: 'k'")


def run(name: str, *, sha: str = SHA, status: str = "completed",
        conclusion: str | None = "success", text: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "head_sha": sha, "status": status,
                           "conclusion": conclusion}
    if text is not None:
        out["output"] = {"summary": "tests failed", "text": text}
    return out


def doc(*runs: dict[str, Any], total: int | None = None) -> str:
    return json.dumps({"total_count": len(runs) if total is None else total,
                       "check_runs": list(runs)})


def green_doc(**override: dict[str, Any]) -> str:
    return doc(*(override.get(name, run(name)) for name in REQUIRED))


def ledger(*rows: dict[str, Any]) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


def result(outcome: str, sha: str = SHA, lane: str = "L1") -> dict[str, Any]:
    return {"event": "ci_result", "lane": lane, "sha": sha, "result": outcome, "url": "u"}


def rerun(rc: object = 0, sha: str = SHA) -> dict[str, Any]:
    return {"event": "ci_rerun", "lane": "L1", "sha": sha, "url": "u", "rc": rc,
            "reason": "first failure"}


def verdict(**kw: Any) -> CiVerdict:
    return ci_verdict(SHA, required=REQUIRED, **kw)


class CiVerdictTests(unittest.TestCase):
    def test_green_needs_every_required_success_on_the_exact_sha(self) -> None:
        got = verdict(checks=green_doc())
        self.assertEqual((got.verdict, got.sha), ("green", SHA))
        self.assertEqual(verdict(checks=green_doc(), events=ledger(result("success"))).verdict,
                         "green")

    def test_red_carries_check_names_and_test_ids(self) -> None:
        got = verdict(checks=green_doc(**{"shard 2/2": run(
            "shard 2/2", conclusion="failure", text=FAIL_TEXT.replace("\n", "\r\n"))}))
        self.assertEqual(got.verdict, "red")
        self.assertEqual(got.failing_checks, ("shard 2/2",))
        self.assertEqual(got.test_ids, ("tests.test_a.A.test_x", "tests.test_b.B.test_y"))

    def test_red_without_parsed_tests_and_non_success_conclusions(self) -> None:
        for conclusion in ("failure", "cancelled", "neutral", "skipped", None):
            with self.subTest(conclusion=conclusion):
                got = verdict(checks=green_doc(aggregate=run("aggregate",
                                                             conclusion=conclusion)))
                self.assertEqual((got.verdict, got.failing_checks, got.test_ids),
                                 ("red", ("aggregate",), ()))

    def test_pending_while_a_required_check_runs(self) -> None:
        got = verdict(checks=green_doc(aggregate=run("aggregate", status="in_progress",
                                                     conclusion=None)))
        self.assertEqual(got.verdict, "pending")

    def test_non_required_failure_is_ignored(self) -> None:
        got = verdict(checks=doc(*(run(n) for n in REQUIRED),
                                 run("lint", conclusion="failure")))
        self.assertEqual(got.verdict, "green")

    def test_rerun_pending_after_one_started_rerun(self) -> None:
        events = ledger(result("failure"), rerun(0))
        self.assertEqual(verdict(events=events).verdict, "rerun-pending")
        self.assertEqual(verdict(events=events, checks=green_doc()).verdict, "rerun-pending")
        self.assertEqual(verdict(events=ledger(rerun(0), result("failure"))).verdict, "red")

    def test_rerun_not_started_or_repeated_is_unknown(self) -> None:
        for events in (ledger(rerun(False)), ledger(rerun(1)), ledger(rerun("0")),
                       ledger(rerun(0), rerun(0))):
            with self.subTest(events=events):
                self.assertEqual(verdict(events=events).verdict, "unknown")

    def test_ledger_only(self) -> None:
        self.assertEqual(verdict(events=ledger(result("failure"))).verdict, "red")
        # workflow-level success cannot prove each required check: not green
        self.assertEqual(verdict(events=ledger(result("success"))).verdict, "unknown")
        self.assertEqual(verdict(events=ledger(result("success", sha=OTHER))).verdict,
                         "unknown")

    def test_ledger_and_document_must_agree(self) -> None:
        red = green_doc(aggregate=run("aggregate", conclusion="failure"))
        self.assertEqual(verdict(events=ledger(result("failure")), checks=red).verdict, "red")
        self.assertEqual(verdict(events=ledger(result("success")), checks=red).verdict,
                         "unknown")
        self.assertEqual(verdict(events=ledger(result("failure")), checks=green_doc()).verdict,
                         "unknown")
        self.assertEqual(verdict(events=ledger(result("success"), result("failure"))).verdict,
                         "unknown")

    def test_verdicts_are_the_typed_set(self) -> None:
        self.assertEqual(VERDICTS, ("green", "red", "pending", "rerun-pending",
                                    "known-red-only", "unknown"))


class KnownRedTests(unittest.TestCase):
    def failing(self) -> str:
        return green_doc(**{"shard 1/2": run("shard 1/2", conclusion="failure",
                                             text=FAIL_TEXT)})

    def test_every_failure_known_is_its_own_state_not_green(self) -> None:
        got = verdict(checks=self.failing(), known_red=[X, Y])
        self.assertEqual(got.verdict, "known-red-only")
        self.assertEqual(got.test_ids, (X.test_id, Y.test_id))
        self.assertEqual(verdict(checks=self.failing(), known_red=(X, Y),
                                 events=ledger(result("failure"))).verdict, "known-red-only")
        extra = FAIL_TEXT.replace("errors=1)", "errors=1, skipped=2, expected failures=1)")
        checks = green_doc(**{"shard 1/2": run("shard 1/2", conclusion="failure", text=extra)})
        self.assertEqual(verdict(checks=checks, known_red=[X, Y]).verdict, "known-red-only")

    def test_rule_inside_a_message_is_red(self) -> None:
        text = ("FAIL: test_x (tests.test_a.A.test_x)\n" + "-" * 70 + "\nAssertionError: 1 != 2\n"
                + "-" * 70 + "\nNEW FAILURE DETAIL\n" + "-" * 70
                + "\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n")
        self.assertEqual(failures(text), ((X.test_id, ""),))
        checks = green_doc(**{"shard 1/2": run("shard 1/2", conclusion="failure", text=text)})
        self.assertEqual(verdict(checks=checks, known_red=[X]).verdict, "red")
        cut = text.split("NEW FAILURE")[0]  # output cut after an inner rule or mid-block
        for part in (cut, cut.rstrip("-\n")):
            with self.subTest(part=part):
                self.assertEqual(failures(part), ((X.test_id, ""),))

    def test_huge_or_bare_values_are_malformed_not_a_crash(self) -> None:
        bare = CiVerdict.__new__(CiVerdict)
        for case, (n6, row) in enumerate((
                (bare, {"event": "finish", "lane": "L1", "status": "GREEN"}),
                (verdict(), {"event": "finish", "lane": 10 ** 5000}),
                (verdict(), {"event": "finish", "lane": "L1", "status": 10 ** 5000}))):
            with self.subTest(case=case):
                self.assertEqual(compare_finish(n6, row, events="").agreement, "disagree")

    def test_partial_or_inexact_match_is_red(self) -> None:
        wrong_sig = KnownRed(Y.test_id, "KeyError: 'other'")
        for known in ([X], [X, wrong_sig], []):
            with self.subTest(known=known):
                self.assertEqual(verdict(checks=self.failing(), known_red=known).verdict, "red")

    def test_known_red_with_a_check_still_running_is_pending(self) -> None:
        checks = doc(run("shard 1/2", conclusion="failure", text=FAIL_TEXT),
                     run("shard 2/2", status="queued", conclusion=None), run("aggregate"))
        self.assertEqual(verdict(checks=checks, known_red=[X, Y]).verdict, "pending")

    def test_unaccounted_failure_text_is_red(self) -> None:
        sub = FAIL_TEXT.replace("ERROR: test_y (tests.test_b.B.test_y)",
                                "ERROR: test_y (tests.test_b.B.test_y) (k=1)")
        for text in (sub, FAIL_TEXT.replace("FAILED (failures=1, errors=1)", ""),
                     FAIL_TEXT.replace("errors=1", "errors=2"),
                     FAIL_TEXT + "\nnpm ERR! lint failed\nFAILED (failures=1)\n",
                     FAIL_TEXT.replace("errors=1)", "errors=1, unexpected successes=1)"),
                     FAIL_TEXT.replace("failures=1", "failures=" + "9" * 5000),
                     FAIL_TEXT.replace("errors=1", "errors=١"),
                     FAIL_TEXT.replace("errors=1)", "errors=1, bogus=0)"),
                     FAIL_TEXT.replace("errors=1)", "errors=1, errors=0)"),
                     FAIL_TEXT.replace("failures=1, errors=1", "failures=1,, errors=1"),
                     FAIL_TEXT + "\nFAILED (skipped=1)\n"):
            with self.subTest(text=text[-60:]):
                checks = green_doc(**{"shard 1/2": run("shard 1/2", conclusion="failure",
                                                       text=text)})
                self.assertEqual(verdict(checks=checks, known_red=[X, Y]).verdict, "red")

    def test_malformed_known_red_entry_is_unknown(self) -> None:
        for known in ([("tests.test_a.A.test_x", "sig")], [KnownRed("", "sig")], "XY"):
            with self.subTest(known=known):
                self.assertEqual(verdict(checks=green_doc(), known_red=known).verdict,  # type: ignore[arg-type]
                                 "unknown")


class FailClosedTests(unittest.TestCase):
    def assertUnknown(self, got: CiVerdict) -> None:
        self.assertEqual(got.verdict, "unknown", got.reason)

    def test_sha_mismatch_even_on_success(self) -> None:
        self.assertUnknown(verdict(checks=green_doc(aggregate=run("aggregate", sha=OTHER))))

    def test_missing_required_check(self) -> None:
        self.assertUnknown(verdict(checks=doc(run("shard 1/2"), run("shard 2/2"))))
        self.assertUnknown(verdict(checks=doc(run("shard 1/2", conclusion="failure",
                                                  text=FAIL_TEXT), run("shard 2/2")),
                                   known_red=[X, Y]))

    def test_duplicated_run(self) -> None:
        self.assertUnknown(verdict(checks=doc(*(run(n) for n in REQUIRED), run("aggregate"))))

    def test_partial_document(self) -> None:
        self.assertUnknown(verdict(checks=doc(*(run(n) for n in REQUIRED), total=4)))

    def test_malformed_document(self) -> None:
        bad_output = run("aggregate", conclusion="failure")
        bad_output["output"] = {"text": 7}
        for checks in ("{", "[]", json.dumps({"check_runs": []}), doc(),
                       json.dumps({"total_count": 1, "check_runs": [1]}),
                       doc({"name": "aggregate", "status": "completed"}),
                       green_doc(aggregate=bad_output)):
            with self.subTest(checks=checks):
                self.assertUnknown(verdict(checks=checks))

    def test_malformed_ledger(self) -> None:
        for events in ("{\n", "[]\n", ledger(result("success"))[:-1], "\x00\x00",
                       ledger({"event": "ci_result", "sha": SHA})):
            with self.subTest(events=events):
                self.assertUnknown(verdict(events=events, checks=green_doc()))

    def test_ledger_nul_fill_is_tolerated(self) -> None:
        events = "\x00\x00" + ledger(result("failure")) + "\x00"
        self.assertEqual(verdict(events=events).verdict, "red")

    def test_bad_arguments(self) -> None:
        self.assertUnknown(ci_verdict("A" * 40, required=REQUIRED, checks=green_doc()))
        self.assertUnknown(ci_verdict(SHA[:39], required=REQUIRED, checks=green_doc()))
        self.assertUnknown(ci_verdict(None, required=REQUIRED, checks=green_doc()))  # type: ignore[arg-type]
        for required in ("aggregate", (), ("aggregate", "aggregate"), ("aggregate", "")):
            with self.subTest(required=required):
                self.assertUnknown(ci_verdict(SHA, required=required, checks=green_doc()))
        self.assertUnknown(verdict())
        self.assertUnknown(verdict(checks=123))  # type: ignore[arg-type]
        self.assertUnknown(verdict(events=123, checks=green_doc()))  # type: ignore[arg-type]

    def test_hostile_json_is_unknown_not_a_crash(self) -> None:
        deep, huge = "[" * 100000 + "]" * 100000, "1" * 5000
        for text in (deep, huge):
            with self.subTest(size=len(text)):
                self.assertUnknown(verdict(checks=text))
                self.assertUnknown(verdict(events=text + "\n", checks=green_doc()))
        for bad in ([1], {"x": 1}):  # unhashable result values
            with self.subTest(result=bad):
                self.assertUnknown(verdict(events=ledger(result(bad)),  # type: ignore[arg-type]
                                           checks=green_doc()))

    def test_duplicate_json_keys_are_unknown(self) -> None:
        checks = green_doc().replace('"conclusion": "success"',
                                     '"conclusion": "failure", "conclusion": "success"', 1)
        self.assertUnknown(verdict(checks=checks))
        row = f'{{"event": "ci_result", "sha": "{SHA}", "result": "failure", "result": "success"}}\n'
        self.assertUnknown(verdict(events=row, checks=green_doc()))

    def test_rerun_then_failure_after_failed_rerun_is_red(self) -> None:
        events = ledger(result("failure"), rerun(1), result("failure"))
        self.assertEqual(verdict(events=events).verdict, "red")


class FailuresTests(unittest.TestCase):
    def test_blocks_and_signatures(self) -> None:
        self.assertEqual(failures(FAIL_TEXT), (("tests.test_a.A.test_x", X.signature),
                                               ("tests.test_b.B.test_y", Y.signature)))
        self.assertEqual(failures("nothing here\n"), ())

    def test_non_text_is_malformed(self) -> None:
        for text in (None, b"FAIL: x (a.b)", 7):
            with self.subTest(text=text), self.assertRaises(MalformedInput):
                failures(text)  # type: ignore[arg-type]


class CompareFinishTests(unittest.TestCase):
    EVENTS = ledger(result("failure", sha=OTHER), result("success"))

    def compare(self, n6: str, status: str = "GREEN", events: str | None = None,
                **finish: Any) -> tuple[str, str]:
        row = {"event": "finish", "lane": "L1", "id": "L1", "status": status,
               "reason": "CI: u failure", **finish}
        got = compare_finish(CiVerdict(SHA, n6, "r"), row,
                             events=self.EVENTS if events is None else events)
        self.assertEqual((got.lane, got.sha, got.n6, got.conveyor),
                         (str(row["lane"]), SHA, n6, str(row["status"])))
        return got.agreement, got.reason

    def test_reason_codes(self) -> None:
        cases = [
            (("green", "GREEN"), {}, ("agree", "both_green")),
            (("red", "BLOCKED"), {}, ("agree", "both_red")),
            (("known-red-only", "BLOCKED"), {}, ("agree", "both_red")),
            (("red", "GREEN"), {}, ("disagree", "false_red")),
            (("known-red-only", "GREEN"), {}, ("disagree", "false_red")),
            (("green", "BLOCKED"), {}, ("disagree", "false_green")),
            (("unknown", "GREEN"), {}, ("disagree", "n6_unknown")),
            (("pending", "GREEN"), {}, ("disagree", "n6_not_final")),
            (("rerun-pending", "BLOCKED"), {}, ("disagree", "n6_not_final")),
            (("green", "CI_PENDING"), {}, ("disagree", "finish_not_ci")),
            (("red", "BLOCKED"), {"reason": "stacked on failed X1"},
             ("disagree", "finish_stacked_base")),
            (("green", "GREEN"), {"event": "chain"}, ("disagree", "finish_malformed")),
            (("green", "GREEN"), {"lane": "../x"}, ("disagree", "finish_malformed")),
            (("green", "GREEN"), {"lane": None}, ("disagree", "finish_malformed")),
            (("green", "GREEN"), {"events": "{\n"}, ("disagree", "events_malformed")),
            (("green", "GREEN"), {"events": ledger(result("success"), result("failure",
                                                                             sha=OTHER))},
             ("disagree", "sha_unbound")),
            (("green", "GREEN"), {"events": ledger(result("success", lane="L2"))},
             ("disagree", "sha_unbound")),
            (("green", "BLOCKED"), {"reason": "NEW MAIN SHA aaaaaaa is not on local main"},
             ("disagree", "finish_blocked_other")),
            (("green", "BLOCKED"), {"reason": None}, ("disagree", "finish_blocked_other")),
            (("GREEN", "GREEN"), {}, ("disagree", "n6_malformed")),
        ]
        for (n6, status), extra, want in cases:
            with self.subTest(n6=n6, status=status, extra=extra):
                self.assertEqual(self.compare(n6, status, **extra), want)

    def test_non_mapping_finish_is_malformed(self) -> None:
        for finish in (None, [], "finish"):
            with self.subTest(finish=finish):
                got = compare_finish(CiVerdict(SHA, "green", "r"), finish,  # type: ignore[arg-type]
                                     events=self.EVENTS)
                self.assertEqual((got.agreement, got.reason), ("disagree", "finish_malformed"))

    def test_non_verdict_is_malformed_not_a_crash(self) -> None:
        row = {"event": "finish", "lane": "L1", "status": "GREEN"}
        for n6 in (None, "green", {"sha": SHA}, CiVerdict(None, "green", "r"),  # type: ignore[arg-type]
                   CiVerdict(SHA, None, "r")):  # type: ignore[arg-type]
            with self.subTest(n6=n6):
                got = compare_finish(n6, row, events=self.EVENTS)  # type: ignore[arg-type]
                self.assertEqual((got.agreement, got.reason), ("disagree", "n6_malformed"))
                self.assertEqual(compare_finish(n6, None,  # type: ignore[arg-type]
                                                events=self.EVENTS).reason, "finish_malformed")


if __name__ == "__main__":
    unittest.main()
