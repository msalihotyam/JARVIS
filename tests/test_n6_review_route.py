"""N6-11: review routing by tier (G3). Negative tests first; nothing here opens a database."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from jarvis.work_review import G3Exception, ReviewRecord, ReviewRefused, route_reviews

PASS = json.dumps({"verdict": "PASS", "findings": []})


def _out(verdict: str, severity: object) -> str:
    finding = {"severity": severity, "file": "jarvis/x.py", "line": 1, "text": "t"}
    return json.dumps({"verdict": verdict, "findings": [finding]})


def _claude(output: str = PASS, session: str = "claude-review") -> ReviewRecord:
    return ReviewRecord("claude", session, output)


def _codex(output: str = PASS, session: str = "codex-review") -> ReviewRecord:
    return ReviewRecord("codex", session, output)


RECORD = G3Exception(who="Salih", why="docs-only wording fix", slice_id="S1")


def _route(tier: object = "T2", state_machine: object = False, reviews=(), exception: object = None):
    return route_reviews(
        slice_id="S1",
        tier=tier,
        state_machine=state_machine,
        writer="claude",
        writer_session="writer-session",
        reviews=list(reviews),
        exception=exception,
    )


class N6ReviewRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("sqlite3.connect", side_effect=AssertionError("no database in routing"))
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- negative ---------------------------------------------------------------------------------
    def test_state_machine_with_same_provider_reviews_only_is_blocked(self) -> None:
        reviews = [_claude(session="c1"), _claude(session="c2")]
        decision = _route(tier="T1", state_machine=True, reviews=reviews)
        self.assertEqual((decision.outcome, decision.kind, decision.reason), ("blocked", "two_providers", "provider_missing"))

    def test_t2_with_one_provider_is_blocked(self) -> None:
        for reviews in ([_codex()], [_claude()]):
            with self.subTest(provider=reviews[0].provider):
                decision = _route(tier="T2", reviews=reviews)
                self.assertEqual((decision.outcome, decision.reason), ("blocked", "provider_missing"))

    def test_t2_with_claude_pass_and_codex_unparsable_is_blocked(self) -> None:
        decision = _route(tier="T2", reviews=[_claude(), _codex("not json")])
        self.assertEqual((decision.outcome, decision.reason), ("blocked", "unparsable"))

    def test_unknown_severity_or_verdict_blocks(self) -> None:
        bad = [
            _out("PASS", "minor"),
            _out("PASS", "MINOR "),
            _out("PASS", "MINОR"),  # Cyrillic O homoglyph
            _out("PASS", ""),
            _out("PASS", None),
            _out("PASS", "CRITICAL"),
            json.dumps({"verdict": "APPROVED", "findings": []}),
            json.dumps({"verdict": "pass", "findings": []}),
        ]
        for output in bad:
            with self.subTest(output=output[:80]):
                t2 = _route(tier="T2", reviews=[_claude(), _codex(output)])
                self.assertEqual((t2.outcome, t2.reason), ("blocked", "unparsable"))
                t1 = _route(tier="T1", reviews=[_codex(output)])
                self.assertEqual((t1.outcome, t1.reason), ("blocked", "unparsable"))

    def test_exception_without_a_record_is_refused(self) -> None:
        for exc in (
            True,
            "Salih said ok",
            {"who": "Salih", "why": "x", "slice_id": "S1"},
            G3Exception(who="", why="x", slice_id="S1"),
            G3Exception(who="Salih", why="  ", slice_id="S1"),
            G3Exception(who="Salih", why="x", slice_id=""),
            G3Exception(who="Salih", why="x", slice_id="OTHER"),
            G3Exception(who=None, why="x", slice_id="S1"),  # type: ignore[arg-type]
        ):
            with self.subTest(exc=repr(exc)[:80]):
                with self.assertRaises(ReviewRefused) as caught:
                    _route(tier="T1", reviews=[_claude()], exception=exc)
                self.assertEqual(caught.exception.code, "exception_unrecorded")

    def test_exception_on_t2_or_state_machine_or_t0_is_refused_even_with_both_passes(self) -> None:
        both = [_claude(), _codex()]
        for tier, sm in (("T2", False), ("T1", True), ("T2", True), ("T0", False), ("t1", False)):
            with self.subTest(tier=tier, state_machine=sm):
                with self.assertRaises(ReviewRefused) as caught:
                    _route(tier=tier, state_machine=sm, reviews=both, exception=RECORD)
                self.assertEqual(caught.exception.code, "exception_not_allowed")

    def test_unknown_tier_needs_two_providers(self) -> None:
        for tier in (None, "", "T3", "t1", " T1", "T1 ", 1, "LOW"):
            with self.subTest(tier=repr(tier)):
                decision = _route(tier=tier, reviews=[_codex()])
                self.assertEqual((decision.outcome, decision.kind), ("blocked", "two_providers"))
                with self.assertRaises(ReviewRefused):
                    _route(tier=tier, reviews=[_claude()], exception=RECORD)
                self.assertEqual(_route(tier=tier, reviews=[_claude(), _codex()]).outcome, "accepted")

    def test_non_bool_state_machine_counts_as_state_machine(self) -> None:
        for sm in (None, 0, "no", "False"):
            with self.subTest(sm=repr(sm)):
                decision = _route(tier="T1", state_machine=sm, reviews=[_codex()])
                self.assertEqual((decision.outcome, decision.kind), ("blocked", "two_providers"))

    def test_no_review_never_passes(self) -> None:
        for tier, exc in (("T0", None), ("T1", None), ("T1", RECORD), ("T2", None)):
            with self.subTest(tier=tier, exception=exc is not None):
                decision = _route(tier=tier, reviews=[], exception=exc)
                self.assertEqual((decision.outcome, decision.reason), ("blocked", "no_review"))

    def test_t1_same_provider_without_exception_is_blocked(self) -> None:
        decision = _route(tier="T1", reviews=[_claude()])
        self.assertEqual((decision.outcome, decision.kind, decision.reason), ("blocked", "cross_provider", "provider_missing"))

    def test_writer_session_review_is_refused(self) -> None:
        for reviews, exc in (([_claude(session="writer-session"), _codex()], None), ([_claude(session="writer-session")], RECORD)):
            with self.subTest(exception=exc is not None):
                with self.assertRaises(ReviewRefused) as caught:
                    _route(tier="T1" if exc else "T2", reviews=reviews, exception=exc)
                self.assertEqual(caught.exception.code, "own_session")

    def test_bad_inputs_are_refused(self) -> None:
        cases = (
            ("reviewer_unknown", dict(reviews=[ReviewRecord("jules", "j1", PASS), _codex()])),
            ("session_reused", dict(reviews=[_claude(session="s"), _codex(session="s")])),
            ("own_session", dict(reviews=[_claude(session=""), _codex()])),
        )
        for code, kwargs in cases:
            with self.subTest(code=code):
                with self.assertRaises(ReviewRefused) as caught:
                    _route(tier="T2", **kwargs)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ReviewRefused) as caught:
            route_reviews(slice_id="S1", tier="T2", state_machine=False, writer="gpt",
                          writer_session="w", reviews=[_claude(), _codex()])
        self.assertEqual(caught.exception.code, "writer_unknown")

    def test_blocking_finding_blocks_even_with_both_providers(self) -> None:
        decision = _route(tier="T2", reviews=[_claude(), _codex(_out("CHANGES_REQUIRED", "MAJOR"))])
        self.assertEqual((decision.outcome, decision.reason), ("blocked", "blocking_findings"))
        self.assertEqual(len(decision.blocking), 1)

    # --- positive ---------------------------------------------------------------------------------
    def test_t1_non_state_machine_with_recorded_exception_is_allowed(self) -> None:
        decision = _route(tier="T1", reviews=[_claude(session="separate")], exception=RECORD)
        self.assertEqual((decision.outcome, decision.kind), ("accepted", "g3_exception"))

    def test_t2_with_claude_and_codex_pass_is_allowed(self) -> None:
        decision = _route(tier="T2", reviews=[_claude(_out("PASS", "MINOR")), _codex()])
        self.assertEqual((decision.outcome, decision.kind), ("accepted", "two_providers"))
        self.assertEqual([f.severity for f in decision.logged], ["MINOR"])

    def test_t1_cross_provider_pass_is_allowed(self) -> None:
        decision = _route(tier="T1", reviews=[_codex()])
        self.assertEqual((decision.outcome, decision.kind), ("accepted", "cross_provider"))


if __name__ == "__main__":
    unittest.main()
