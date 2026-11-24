"""N6G26CLEANUP: the G26 cleanup / preservation dry-run (R21), pure, shadow only, not wired."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis import n6_cleanup as c

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
A, B, C, D, E = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
OLD = (NOW - timedelta(hours=2)).isoformat()


def lane(**kw: Any) -> dict[str, Any]:
    name = kw.get("lane", "L1")
    rec: dict[str, Any] = {"lane": name, "worktree": f"C:\\dev\\JARVIS\\.claude\\worktrees\\{name}",
                           "branch": f"worktree-{name}", "head": A, "status": "PASS",
                           "finished_ts": OLD}
    rec.update(kw)
    return rec


def ev(**kw: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {"report": True, "receipt": "INT PICKS a..", "merged": True}
    rec.update(kw)
    return rec


def run(rec: Any, main: Any = frozenset({A}), evidence: Any = None, *, clock: Any = NOW,
        timeout: Any = HOUR) -> c.Plan:
    evs = {"L1": ev()} if evidence is None else evidence
    out = c.plan([rec], main, evs, clock=lambda: clock, timeout=timeout)
    return next(iter(out.values()))


class N6CleanupTests(unittest.TestCase):
    def test_remove_when_head_on_main_evidence_and_old(self) -> None:
        self.assertEqual(run(lane()).verdict, "remove")

    def test_remove_through_commit_list_and_verified_picks(self) -> None:
        rec = lane(head=C, commits=[B, C])
        evs = {"L1": ev(picks={B: D, C: E})}
        self.assertEqual(run(rec, frozenset({A, D, E}), evs).verdict, "remove")

    def test_no_evidence_is_preserved_always(self) -> None:
        self.assertEqual(run(lane(), evidence={}), c.Plan("preserve", "no evidence"))

    def test_incomplete_evidence_is_preserved(self) -> None:
        for gap, name in (({"report": False}, "report"), ({"receipt": None}, "receipt"),
                          ({"receipt": "  "}, "receipt"), ({"merged": False}, "merged proof")):
            p = run(lane(), evidence={"L1": ev(**gap)})
            self.assertEqual(p.verdict, "preserve", gap)
            self.assertIn(name, p.reason)
        p = run(lane(), evidence={"L1": {}})
        self.assertEqual(p.reason, "no evidence: report, receipt, merged proof")

    def test_commit_not_on_main_is_preserved(self) -> None:
        p = run(lane(head=C, commits=[B, C]), frozenset({A, B}))
        self.assertEqual(p.verdict, "preserve")
        self.assertTrue(p.reason.startswith("commits not on main: cccccccccccc"))

    def test_pick_target_off_main_is_preserved(self) -> None:
        p = run(lane(head=C, commits=[C]), frozenset({A}), {"L1": ev(picks={C: D})})
        self.assertEqual(p.verdict, "preserve")

    def test_head_off_main_without_list_or_with_stale_list_is_preserved(self) -> None:
        p = run(lane(head=C), frozenset({A}))
        self.assertIn("no commit list", p.reason)
        p = run(lane(head=C, commits=[B]), frozenset({A, B}))  # tip moved past the list
        self.assertIn("tip moved", p.reason)
        self.assertEqual(p.verdict, "preserve")

    def test_too_young_and_future_are_preserved(self) -> None:
        exact = run(lane(finished_ts=(NOW - HOUR).isoformat()))
        self.assertEqual(exact.verdict, "preserve")  # strictly older than the timeout
        self.assertTrue(exact.reason.startswith("too young"))
        future = run(lane(finished_ts=(NOW + HOUR).isoformat()))
        self.assertEqual(future.verdict, "preserve")

    def test_unfinished_or_unknown_status_is_preserved(self) -> None:
        for status in ("RUNNING", "STUCK", "MISSING", "NEWSTATUS", ""):
            p = run(lane(status=status))
            self.assertEqual(p.verdict, "preserve", status)
            self.assertTrue(p.reason.startswith("unknown state"), status)

    def test_main_and_ci_branches_are_never_removed(self) -> None:
        for branch in ("main", "refs/heads/main", "ci/x", "refs/heads/ci/y", "Main", " main ",
                       "heads/main", "CI/z", "HEAD", "origin/main", "refs/remotes/origin/x"):
            p = run(lane(branch=branch))
            self.assertEqual(p.verdict, "preserve", branch)
            self.assertIn("protected branch", p.reason)

    def test_only_this_managers_worktrees_are_removed(self) -> None:
        for path in ("C:/dev/JARVIS", ".", "C:/", "C:/dev/JARVIS/.claude/worktrees/L2",
                     "C:/x/.claude/worktrees/../worktrees/L1", "C:/dev/JARVIS/.claude/worktrees"):
            p = run(lane(worktree=path))
            self.assertEqual(p.verdict, "preserve", path)
            self.assertIn("not a lane worktree", p.reason)
        self.assertEqual(run(lane(worktree="c:/DEV/jarvis/.claude/worktrees/l1/")).verdict,
                         "remove")

    def test_two_lanes_claiming_one_target_are_unknown(self) -> None:
        for shared in ({"worktree": lane()["worktree"].upper()}, {"branch": "WORKTREE-L1"}):
            other = lane(lane="L2", status="RUNNING", head=B, **shared)
            out = c.plan([lane(), other], frozenset({A}), {"L1": ev()}, clock=lambda: NOW,
                         timeout=HOUR)
            self.assertEqual({p.verdict for p in out.values()}, {"unknown"}, shared)
            self.assertIn("claimed by more than one lane", out["L1"].reason)

    def test_malformed_lane_input_is_unknown(self) -> None:
        cases = [lane(head="A" * 40), lane(head=A[:39]), lane(head=None), lane(worktree=""),
                 lane(branch=None), lane(status=1), lane(commits=[A, "x"]), lane(commits=A),
                 lane(finished_ts="yesterday"), lane(finished_ts="2026-11-24T10:00:00"),
                 lane(finished_ts=None), lane(lane="../x"), "L1", None]
        for rec in cases:
            self.assertEqual(run(rec).verdict, "unknown", rec)

    def test_malformed_evidence_is_unknown(self) -> None:
        for bad in (ev(report=1), ev(merged="yes"), ev(receipt=5), ev(picks={A: "x"}),
                    ev(picks=[A]), "yes"):
            self.assertEqual(run(lane(), evidence={"L1": bad}).verdict, "unknown", bad)

    def test_malformed_shared_input_makes_every_lane_unknown(self) -> None:
        naive = datetime(2026, 11, 24, 12)  # noqa: DTZ001 - the naive clock under test
        for kw in ({"main": [A]}, {"main": frozenset({"x"})}, {"evidence": [ev()]},
                   {"clock": naive}, {"clock": NOW.isoformat()}, {"timeout": timedelta(0)},
                   {"timeout": 3600}):
            self.assertEqual(run(lane(), **kw).verdict, "unknown", kw)
        self.assertEqual(c.plan("L1", frozenset(), {}, clock=lambda: NOW, timeout=HOUR),
                         {"#lanes": c.Plan("unknown", "malformed: lanes is not a list")})

    def test_duplicate_lane_ids_are_unknown(self) -> None:
        out = c.plan([lane(), lane()], frozenset({A}), {"L1": ev()}, clock=lambda: NOW,
                     timeout=HOUR)
        self.assertEqual(out["L1"].verdict, "unknown")
        self.assertIn("duplicate", out["L1"].reason)

    def test_clock_is_read_once(self) -> None:
        calls: list[int] = []

        def clock() -> datetime:
            calls.append(1)
            return NOW

        c.compare([lane()], frozenset({A}), {"L1": ev()}, [], clock=clock, timeout=HOUR)
        self.assertEqual(len(calls), 1)

    # shadow compare against the conveyor's cleanup / cleanup_refused / preserved events

    def shadow(self, events: Any, recs: Any = None, evidence: Any = None) -> dict[str, Any]:
        recs = [lane()] if recs is None else recs
        evs = {"L1": ev()} if evidence is None else evidence
        return c.compare(recs, frozenset({A}), evs, events, clock=lambda: NOW, timeout=HOUR)

    def verdict(self, events: Any, **kw: Any) -> str:
        return self.shadow(events, **kw)["records"][0]["verdict"]

    def test_agree_when_both_remove(self) -> None:
        out = self.shadow([{"event": "cleanup", "lane": "L1", "result": "removed"}])
        rec = out["records"][0]
        self.assertEqual(rec["verdict"], "agree")
        self.assertEqual(rec["ts"], NOW.isoformat())
        self.assertEqual(out["agreement"], 1.0)

    def test_false_remove_when_n6_removes_what_conveyor_kept(self) -> None:
        events = [{"event": "preserved", "lane": "L1", "reason": "unpushed", "branch": "b",
                   "sha": A}]
        self.assertEqual(self.verdict(events), "false_remove")

    def test_disagree_when_n6_preserves_what_conveyor_removed(self) -> None:
        events = [{"event": "cleanup", "lane": "L1", "result": "removed"}]
        self.assertEqual(self.verdict(events, evidence={}), "disagree")

    def test_agree_when_both_preserve(self) -> None:
        events = [{"event": "cleanup_refused", "lane": "L1", "step": "worktree", "reason": "dirty"}]
        out = self.shadow(events, evidence={})
        self.assertEqual(out["records"][0]["verdict"], "agree")
        self.assertEqual(out["records"][0]["conveyor"]["reason"],
                         "cleanup_refused worktree: dirty")

    def test_last_event_wins_removed_then_refused_is_preserve(self) -> None:
        events = [{"event": "cleanup", "lane": "L1", "result": "removed"},
                  {"event": "cleanup_refused", "lane": "L1", "step": "worktree", "reason": "x"}]
        self.assertEqual(self.shadow(events)["records"][0]["conveyor"]["verdict"], "preserve")
        self.assertEqual(self.verdict(events), "false_remove")

    def test_cleanup_error_is_preserve(self) -> None:
        events = [{"event": "cleanup", "lane": "L1", "result": "error"}]
        self.assertEqual(c.conveyor_verdicts(events), {"L1": ("preserve", "cleanup error")})

    def test_preserved_released_clears_the_lane(self) -> None:
        events = [{"event": "preserved", "lane": "L1", "reason": "r"},
                  {"event": "preserved_released", "lane": "L1"}]
        self.assertEqual(self.verdict(events), "excluded")
        events.append({"event": "cleanup", "lane": "L1", "result": "removed"})
        self.assertEqual(self.verdict(events), "agree")

    def test_lane_without_conveyor_event_is_excluded(self) -> None:
        events = [{"event": "cleanup_skip", "lane": "L1"},
                  {"event": "cleanup", "lane": "OTHER", "result": "removed"},
                  {"event": "cleanup", "lane": 5, "result": "removed"}]
        out = self.shadow(events)
        self.assertEqual(out["records"][0]["verdict"], "excluded")
        self.assertIsNone(out["agreement"])

    def test_n6_unknown_is_unknown_not_agree(self) -> None:
        events = [{"event": "cleanup", "lane": "L1", "result": "removed"}]
        self.assertEqual(self.verdict(events, recs=[lane(head="bad")]), "unknown")

    def test_malformed_events_are_unknown(self) -> None:
        for events in ("x", None, [{"event": "cleanup"}, "line"], {"L1": "removed"}):
            out = self.shadow(events)
            self.assertEqual(out["records"][0]["verdict"], "unknown", events)
            self.assertEqual(out["totals"]["unknown"], 1)

    def test_totals_and_agreement_rate(self) -> None:
        recs = [lane(), lane(lane="L2"), lane(lane="L3"), lane(lane="L4")]
        evs = {"L1": ev(), "L2": ev(), "L3": ev()}
        events = [{"event": "cleanup", "lane": n, "result": "removed"} for n in ("L1", "L4")]
        events.append({"event": "preserved", "lane": "L2", "reason": "r"})
        out = self.shadow(events, recs=recs, evidence=evs)
        self.assertEqual(out["totals"], {"agree": 1, "disagree": 1, "false_remove": 1,
                                         "excluded": 1, "unknown": 0})
        self.assertAlmostEqual(out["agreement"], 1 / 3)

    # hard limits: pure, shadow only, not wired

    def test_module_has_no_io_and_is_not_wired(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "jarvis" / "n6_cleanup.py").read_text(encoding="utf-8")
        for word in ("subprocess", "import os", "socket", "pathlib", "open(", "shutil",
                     "urllib", "requests", ".now(", "time.time"):
            self.assertNotIn(word, source, word)
        for wired in ("scripts/conveyor.py", "scripts/orchbot.py"):
            path = root / wired
            if path.exists():
                self.assertNotIn("n6_cleanup", path.read_text(encoding="utf-8"), wired)


if __name__ == "__main__":
    unittest.main()
