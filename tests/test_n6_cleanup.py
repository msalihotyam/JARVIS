"""N6G26CLEANUP: the G26 cleanup / preservation dry-run (R21), pure, shadow only, not wired."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from jarvis import n6_cleanup as c

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
A, B, C, D, E = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
OLD = (NOW - timedelta(hours=2)).isoformat()
ROOT = "C:\\dev\\JARVIS"  # the injected checkout that owns .claude/worktrees


class Berlin(tzinfo):
    """CET/CEST for 2026 only (Windows has no tzdata): CEST from 03-29 02:00 to 10-25 03:00 wall."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        assert dt is not None
        wall = dt.replace(tzinfo=None, fold=0)
        start, end = datetime(2026, 3, 29, 2), datetime(2026, 10, 25, 2)
        summer = start <= wall < end or (end <= wall < end + HOUR and not dt.fold)
        return timedelta(hours=2 if summer else 1)

    def dst(self, dt: datetime | None) -> timedelta:
        return self.utcoffset(dt) - HOUR

    def tzname(self, dt: datetime | None) -> str:
        return "CEST" if self.dst(dt) else "CET"


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
        timeout: Any = HOUR, root: Any = ROOT) -> c.Plan:
    evs = {"L1": ev()} if evidence is None else evidence
    out = c.plan([rec], main, evs, clock=lambda: clock, timeout=timeout, root=root)
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

    def test_supplied_list_must_end_at_the_head_even_with_the_head_on_main(self) -> None:
        for commits in ([B], [], [A, B]):
            p = run(lane(head=A, commits=commits), frozenset({A, B}))
            self.assertEqual(p.verdict, "preserve", commits)
            self.assertIn("tip moved", p.reason)
        p = run(lane(head=A, commits=[B, A]), frozenset({A}))  # listed B never reached main
        self.assertEqual(p.verdict, "preserve")
        self.assertTrue(p.reason.startswith("commits not on main: bbbbbbbbbbbb"))
        self.assertEqual(run(lane(head=A, commits=[B, A]), frozenset({A, B})).verdict, "remove")

    def test_malformed_branch_name_is_unknown(self) -> None:
        for branch in ("-D", "--force", "topic..old", "a b", " x y ", "x\x01", "x\x7f", "x~1",
                       "x^", "x:y", "x?", "x*", "x[", "x\\y", "x.lock", "x.lock/y", "@{u}",
                       "x@{1}", " x", "x​", "a//b", "x/", "x.", ".x", "a/.b", "@",
                       "/topic", "/a/b", "/", "//x", "refs/heads//x", "heads//x"):
            p = run(lane(branch=branch))
            self.assertEqual(p.verdict, "unknown", repr(branch))
            self.assertIn("branch", p.reason)
        for branch in ("feature/x-1", "worktree-L1", "a.b", "x@y", "lock.x"):
            self.assertEqual(run(lane(branch=branch)).verdict, "remove", branch)

    def test_too_young_and_future_are_preserved(self) -> None:
        exact = run(lane(finished_ts=(NOW - HOUR).isoformat()))
        self.assertEqual(exact.verdict, "preserve")  # strictly older than the timeout
        self.assertTrue(exact.reason.startswith("too young"))
        future = run(lane(finished_ts=(NOW + HOUR).isoformat()))
        self.assertEqual(future.verdict, "preserve")

    def test_age_is_real_elapsed_time_across_dst(self) -> None:
        tz, t90 = Berlin(), timedelta(minutes=90)
        # spring forward: 01:30 CET -> 03:30 CEST is 1h real, 2h on the wall clock
        start = datetime(2026, 3, 29, 1, 30, tzinfo=tz)
        young = run(lane(finished_ts=start), clock=datetime(2026, 3, 29, 3, 30, tzinfo=tz), timeout=t90)
        self.assertEqual(young, c.Plan("preserve", "too young: 3600s <= 5400s"))
        self.assertEqual(run(lane(finished_ts=start), clock=datetime(2026, 3, 29, 4, 30, tzinfo=tz),
                             timeout=t90).verdict, "remove")
        # fall back: 02:30 CEST (fold 0) -> 02:30 CET (fold 1) is 1h real, 0 on the wall clock
        back = datetime(2026, 10, 25, 2, 30, tzinfo=tz)
        self.assertEqual(run(lane(finished_ts=back), clock=back.replace(fold=1),
                             timeout=timedelta(minutes=30)).verdict, "remove")
        self.assertEqual(run(lane(finished_ts=back), clock=back.replace(fold=1), timeout=HOUR),
                         c.Plan("preserve", "too young: 3600s <= 3600s"))
        # mixed: an ISO fixed-offset finish against a DST-zone clock
        self.assertEqual(run(lane(finished_ts="2026-03-29T01:30:00+01:00"),
                             clock=datetime(2026, 3, 29, 3, 30, tzinfo=tz), timeout=t90).verdict,
                         "preserve")

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

    def test_worktree_outside_the_injected_root_is_preserved(self) -> None:
        for path in ("../.claude/worktrees/L1", "..\\.claude\\worktrees\\L1",
                     ".claude/worktrees/L1", "C:/foreign/.claude/worktrees/L1",
                     "D:/dev/JARVIS/.claude/worktrees/L1", "/.claude/worktrees/L1",
                     "C:/dev/JARVIS/./.claude/worktrees/L1", "C:/dev/JARVIS/.claude/worktrees/L1/.",
                     "C:/dev/JARVIS/x/../.claude/worktrees/L1", "C:/dev/JARVIS/.claude/worktrees/L1/..",
                     "C:/dev/JARVIS/.claude/worktrees/L1/sub", "C:/dev/JARVIS /.claude/worktrees/L1",
                     "C:/dev/JARVIS/.claude/worktrees/L1.", "C:/dev/JARVIS/.claude/worktrees/L1 x",
                     "\\\\server\\share\\.claude\\worktrees\\L1", "C:/dev/JARVIS//.claude/worktrees/L1"):
            p = run(lane(worktree=path))
            self.assertEqual(p.verdict, "preserve", path)
            self.assertIn("not a lane worktree", p.reason)
        # Windows drops a trailing dot: lane "L1." would own L1's directory
        self.assertEqual(run(lane(lane="L1.", worktree=f"{ROOT}\\.claude\\worktrees\\L1")).verdict,
                         "unknown")

    def test_malformed_root_makes_every_lane_unknown(self) -> None:
        for root in (None, "", "  ", 5, "dev/JARVIS", "C:dev\\JARVIS", "../JARVIS", ".",
                     "C:/dev/../JARVIS", "C:/dev/./JARVIS", "C:\\dev\\JARVIS\\.."):
            p = run(lane(), root=root)
            self.assertEqual(p.verdict, "unknown", root)
            self.assertIn("root", p.reason)
        self.assertEqual(run(lane(), root="/dev/JARVIS").verdict, "preserve")  # not ours

    def test_two_lanes_claiming_one_target_are_unknown(self) -> None:
        for shared in ({"worktree": lane()["worktree"].upper()}, {"branch": "WORKTREE-L1"},
                       {"branch": "refs/heads/worktree-L1"}, {"branch": "heads/Worktree-L1"},
                       {"branch": "/worktree-L1"}, {"branch": "/refs/heads/worktree-L1"}):
            other = lane(lane="L2", status="RUNNING", head=B, **shared)
            out = c.plan([lane(), other], frozenset({A}), {"L1": ev()}, clock=lambda: NOW,
                         timeout=HOUR, root=ROOT)
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
        self.assertEqual(c.plan("L1", frozenset(), {}, clock=lambda: NOW, timeout=HOUR, root=ROOT),
                         {"#lanes": c.Plan("unknown", "malformed: lanes is not a list")})

    def test_unicode_or_spaced_worktree_spelling_is_not_ours(self) -> None:
        base = "C:\\dev\\JARVIS\\.claude\\"
        for wt in (base + "wor\u212atrees\\L1", base + "worktree\u017f\\L1",
                   base + "worktrees\\L1\u00a0", base + "worktrees\\L1\u3000",
                   " " + base + "worktrees\\L1", "\u00a0" + base + "worktrees\\L1"):
            got = run(lane(worktree=wt))
            self.assertEqual(got.verdict, "preserve", repr(wt))
            self.assertIn("not a lane worktree", got.reason)
        got = run(lane(worktree="C:\\Users\\stra\u00dfe\\J\\.claude\\worktrees\\L1"),
                  root="C:\\Users\\strasse\\J")
        self.assertEqual(got.verdict, "preserve")

    def test_unc_spaced_bare_or_non_ascii_root_makes_every_lane_unknown(self) -> None:
        for root in ("//", "\\\\", "/", "C:\\", "  C:\\dev\\JARVIS", "C:\\dev\\JARVIS ",
                     "\\\\.\\C:\\dev\\JARVIS", "\\\\server\\share\\JARVIS", "C:\\dev\\JARV\u0130S"):
            self.assertEqual(run(lane(), root=root).verdict, "unknown", repr(root))

    def test_any_spelling_of_one_worktree_directory_collides(self) -> None:
        base = "C:\\dev\\JARVIS\\.claude\\worktrees\\"
        for wt in (base + ".\\L1", "\\\\?\\" + base + "L1", base + "L1.", base + "x\\..\\L1",
                   base + "L1\\.", base + "l1 "):
            other = lane(lane="L2", status="RUNNING", head=B, worktree=wt)
            out = c.plan([lane(), other], frozenset({A}), {"L1": ev()}, clock=lambda: NOW,
                         timeout=HOUR, root=ROOT)
            self.assertEqual(out["L1"].verdict, "unknown", repr(wt))
            self.assertIn("worktree claimed by more than one lane", out["L1"].reason)

    def test_nested_or_prefixed_branch_spellings_are_judged_canonically(self) -> None:
        for branch in ("refs/heads/-x", "heads/-x", "heads/@", "refs/heads/"):
            self.assertEqual(run(lane(branch=branch)).verdict, "unknown", branch)
        for branch in ("refs/heads/heads/ci/x", "refs/heads/heads/main", "refs/tags/v1",
                       "refs/heads/heads/remotes/o/x", "refs/remotes/origin/x"):
            got = run(lane(branch=branch))
            self.assertEqual(got.verdict, "preserve", branch)
            self.assertIn("protected branch", got.reason)

    def test_hostile_input_is_unknown_not_an_exception(self) -> None:
        class Raising(tzinfo):
            def utcoffset(self, dt: datetime | None) -> timedelta | None:
                raise RuntimeError("tz")

        class Far(tzinfo):
            def utcoffset(self, dt: datetime | None) -> timedelta | None:
                return timedelta(hours=30)

        class Boom(dict[str, Any]):
            def get(self, *args: Any) -> Any:
                raise RuntimeError("get")

        class Odd(datetime):
            def isoformat(self, *args: Any, **kw: Any) -> str:
                raise RuntimeError("iso")

        for tz in (Raising(), Far()):
            self.assertEqual(run(lane(), clock=datetime(2026, 11, 24, 12, tzinfo=tz)).verdict,
                             "unknown")
            self.assertEqual(run(lane(finished_ts=datetime(2026, 11, 1, tzinfo=tz))).verdict,
                             "unknown")
        out = c.plan([Boom(lane())], frozenset({A}), {}, clock=lambda: NOW, timeout=HOUR, root=ROOT)
        self.assertEqual(list(out), ["#plan"])
        self.assertEqual(out["#plan"].verdict, "unknown")
        res = c.compare([lane()], frozenset({A}), {"L1": ev()}, [],
                        clock=lambda: Odd(2026, 11, 24, 12, tzinfo=UTC), timeout=HOUR, root=ROOT)
        self.assertEqual((res["records"], res["agreement"]), ([], None))
        self.assertIn("input raised RuntimeError", res["error"])

    def test_non_callable_or_raising_clock_is_unknown_not_an_exception(self) -> None:
        def boom() -> datetime:
            raise RuntimeError("clock down")

        for clock in (None, NOW, 5, boom):
            out = c.plan([lane()], frozenset({A}), {"L1": ev()}, clock=clock, timeout=HOUR,
                         root=ROOT)
            self.assertEqual(out["L1"].verdict, "unknown", clock)
            self.assertIn("clock", out["L1"].reason)
            shadow = c.compare([lane()], frozenset({A}), {"L1": ev()},
                               [{"event": "cleanup", "lane": "L1", "result": "removed"}],
                               clock=clock, timeout=HOUR, root=ROOT)
            self.assertEqual(shadow["records"][0]["verdict"], "unknown", clock)
            self.assertIsNone(shadow["ts"])
            self.assertIsNone(shadow["agreement"])

    def test_duplicate_lane_ids_are_unknown(self) -> None:
        out = c.plan([lane(), lane()], frozenset({A}), {"L1": ev()}, clock=lambda: NOW,
                     timeout=HOUR, root=ROOT)
        self.assertEqual(out["L1"].verdict, "unknown")
        self.assertIn("duplicate", out["L1"].reason)

    def test_clock_is_read_once(self) -> None:
        calls: list[int] = []

        def clock() -> datetime:
            calls.append(1)
            return NOW

        c.compare([lane()], frozenset({A}), {"L1": ev()}, [], clock=clock, timeout=HOUR, root=ROOT)
        self.assertEqual(len(calls), 1)

    # shadow compare against the conveyor's cleanup / cleanup_refused / preserved events

    def shadow(self, events: Any, recs: Any = None, evidence: Any = None) -> dict[str, Any]:
        recs = [lane()] if recs is None else recs
        evs = {"L1": ev()} if evidence is None else evidence
        return c.compare(recs, frozenset({A}), evs, events, clock=lambda: NOW, timeout=HOUR,
                         root=ROOT)

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

    def test_cleanup_without_a_known_result_is_unknown_not_agree(self) -> None:
        running = [lane(status="RUNNING")]  # N6 preserves: a guessed "preserve" would agree
        for extra in ({}, {"result": "weird"}, {"result": None}, {"result": 5},
                      {"result": "Removed"}, {"result": " removed"}):
            events = [{"event": "cleanup", "lane": "L1", **extra}]
            self.assertEqual(c.conveyor_verdicts(events)["L1"][0], "unknown", extra)
            out = self.shadow(events, recs=running)
            self.assertEqual(out["records"][0]["verdict"], "unknown", extra)
            self.assertIsNone(out["agreement"])
        events = [{"event": "cleanup", "lane": "L1"},
                  {"event": "cleanup_refused", "lane": "L1", "step": "worktree", "reason": "x"}]
        self.assertEqual(self.verdict(events, recs=running), "agree")  # a later known event decides

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
