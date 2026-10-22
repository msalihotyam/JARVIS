"""scripts/orchbot.py, shadow mode v0: one test per rule, the loop guard, fail-closed input handling,
the atomic shadow.json write and the EVTAUDIT replays R4, R7, R12. Tmp-dir fixtures, a fake clock;
no sleep, no network, no processes, never data/jarvis.db."""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import orchbot  # noqa: E402

SHA = "4e73215246812b5e62972ac0c47ec8034f09adca"
SHA2 = "ea29fc7ab7fb14f2f05ef7ed0fc847dcad64acde"


def ts(s):
    return time.mktime(time.strptime(s, orchbot.TS_FMT))


class Clock:
    def __init__(self, s):
        self.t = ts(s)

    def __call__(self):
        return self.t

    def set(self, s):
        self.t = ts(s)


class BotCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="orchbot-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.conv = self.root / "build" / "conveyor"
        self.conv.mkdir(parents=True)
        self.clock = Clock("2026-10-15T12:00:00")
        self.sleeps = []

    def put(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return p

    def events(self, *evs):
        with open(self.conv / "events.jsonl", "a", encoding="utf-8") as f:
            for e in evs:
                f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")

    def run_bot(self, **kw):
        kw.setdefault("from_start", True)
        return orchbot.Bot(self.root, now=self.clock, sleep=self.sleeps.append, **kw).run()

    def acts(self, ds):
        return [(d["would_do"], d["rule"], d["target"]) for d in ds]

    def shadow(self):
        return json.loads((self.root / "build" / "bot" / "shadow.json").read_text(encoding="utf-8"))

    def lines(self):
        p = self.root / "build" / "bot" / "decisions.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def logged(self):
        return [d for d in self.lines() if "verdict" not in d]

    def verdicts(self):
        return [d for d in self.lines() if "verdict" in d]


class R4CiRedTest(BotCase):
    def setUp(self):
        super().setUp()
        self.put("build/conveyor/lanes.json", {"INT45": {"files": ["jarvis/store.py", "tests/test_store.py"]}})
        self.ev = {"ts": "2026-10-13T23:24:46", "event": "ci_result", "lane": "INT45", "sha": SHA,
                   "result": "failure", "url": "https://github.com/o/r/actions/runs/1"}

    def test_replay_r4_outside_failure_reruns_once_then_escalates(self):
        self.put("build/reports/INT45.md", "STATUS: BLOCKED\nFAIL: tests.test_ui.UiTests.test_glide\n")
        self.events(self.ev)
        self.assertEqual(self.acts(self.run_bot()), [("rerun_ci_once", "R4", "INT45")])
        self.events(self.ev)  # the same sha red again: no second rerun
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "R4", "INT45")])

    def test_conveyor_already_reran(self):
        self.put("build/conveyor/lanes.json", {"INT45": {"files": ["jarvis/store.py"], "ci": {"rerun": True}}})
        self.put("build/reports/INT45.md", "FAIL: tests.test_ui.UiTests.test_glide\n")
        self.events(self.ev)
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "R4", "INT45")])

    def test_own_test_failure_queues_a_fix(self):
        self.put("build/reports/INT45.md", "FAIL: tests.test_store.StoreTests.test_put\n")
        self.events(self.ev)
        self.assertEqual(self.acts(self.run_bot()), [("queue_fix_slice", "O5", "INT45")])

    def test_no_test_id_escalates(self):
        self.put("build/reports/INT45.md", "STATUS: BLOCKED\n")
        self.events(self.ev)
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "R4", "INT45")])


class O26Key1Test(BotCase):
    def lane(self, files, cand=True):
        self.put("build/conveyor/lanes.json", {"L1": {"files": files, "ci": {"cand": cand, "sha": SHA}}})

    def green(self):
        self.events({"ts": "2026-10-15T11:00:00", "event": "ci_result", "lane": "L1", "sha": SHA,
                     "result": "success"})

    def test_candidate_green_non_protected_writes_key1_once(self):
        self.lane(["jarvis/a.py", "tests/test_a.py"])
        self.green()
        self.events({"ts": "2026-10-15T11:01:00", "event": "finish", "lane": "L1", "status": "GREEN"})
        ds = self.run_bot()
        self.assertEqual(self.acts(ds), [("write_key1", "O26", "L1")])
        self.assertIn(SHA, ds[0]["detail"])
        self.assertEqual(set(ds[0]), {"ts", "saw", "would_do", "rule", "target", "detail"})

    def test_candidate_sha_line_in_report_counts(self):
        self.lane(["jarvis/a.py"], cand=False)
        self.put("build/reports/L1.md", f"STATUS: CI_PENDING\nCANDIDATE SHA: {SHA}\n")
        self.green()
        self.assertEqual(self.acts(self.run_bot()), [("write_key1", "O26", "L1")])

    def test_not_a_candidate_or_already_approved_is_none(self):
        self.lane(["jarvis/a.py"], cand=False)
        self.green()
        self.assertEqual(self.acts(self.run_bot()), [("none", "O26", "L1")])
        self.put(f"build/approvals/push-{SHA[:12]}-L9", "")
        self.put("build/conveyor/lanes.json", {"L9": {"files": ["jarvis/a.py"], "ci": {"cand": True}},
                                               "L8": {"files": ["jarvis/a.py"], "ci": {"cand": True}}})
        self.events({"ts": "2026-10-15T11:00:00", "event": "ci_result", "lane": "L9", "sha": SHA,
                     "result": "success"},
                    {"ts": "2026-10-15T11:00:00", "event": "ci_result", "lane": "L8", "sha": "1" + SHA[1:],
                     "result": "success"})
        self.assertEqual(self.acts(self.run_bot()), [("none", "O26", "L9"), ("write_key1", "O26", "L8")])


class ConvsafeTest(BotCase):
    def test_protected_range_asks_salih(self):
        self.put("build/conveyor/lanes.json", {"L1": {"files": [".github/workflows/tests.yml"],
                                                      "ci": {"cand": True}}})
        self.events({"ts": "2026-10-15T11:00:00", "event": "ci_result", "lane": "L1", "sha": SHA,
                     "result": "success"})
        self.assertEqual(self.acts(self.run_bot()), [("ask_salih_key1", "CONVSAFE", "L1")])

    def test_adjacent_file_in_report_asks_salih(self):
        self.put("build/conveyor/lanes.json", {"L1": {"files": ["jarvis/a.py"], "ci": {"cand": True}}})
        self.put("build/reports/L1.md", "STATUS: PASS\nADJACENT FILES: scripts/conveyor.py (4 lines, why)\n")
        self.events({"ts": "2026-10-15T11:00:00", "event": "ci_result", "lane": "L1", "sha": SHA,
                     "result": "success"})
        ds = self.run_bot()
        self.assertEqual(self.acts(ds), [("ask_salih_key1", "CONVSAFE", "L1")])
        self.assertIn("scripts/conveyor.py", ds[0]["detail"])

    def test_protected_fails_closed(self):
        self.assertIsNone(orchbot.protected(["jarvis/a.py", "tests/test_a.py"]))
        for files in ([], ['"odd name.py"'], ["scripts/*"], ["*"], [".claude/settings.json"],
                      ["scripts\\conveyor.py"], ["build/allow_list.txt"], ["tests/load_sensitive.py"],
                      ["scripts/"], ["scripts/conveyor.p?"], ["a/../scripts/conveyor.py"], [".Claude/x"],
                      ["JARVIS_ACTIVE_STATE.md"]):
            self.assertIsNotNone(orchbot.protected(files), files)

    def integration(self, picks, **reports):  # BOTREPLAY2: an INT report has PICKS, no FILES line
        self.put("build/conveyor/lanes.json", {"INT9": {"files": [], "ci": {"cand": True}}})
        self.put("build/reports/INT9.md", "STATUS: CI_PENDING\nCI BRANCH: ci/int9\n\nPICKS (a -> b):\n"
                 + "".join(f"  {p}\n" if p else "\n" for p in picks) + "LOCAL TESTS: ok\n")
        for lane, text in reports.items():
            self.put(f"build/reports/{lane}.md", text)
        self.events({"ts": "2026-10-15T11:00:00", "event": "ci_result", "lane": "INT9", "sha": SHA,
                     "result": "success"})
        return self.acts(self.run_bot())

    def test_integration_picks_with_files_write_key1(self):
        self.assertEqual(self.integration(["e3fc068 -> a89b825 DFTONE", "DFQUEUE 86a603f -> 04b050d"],
                                          DFTONE="STATUS: PASS\nFILES: jarvis/a.py\n",
                                          DFQUEUE="STATUS: PASS\nFILES: jarvis/b.py, tests/test_b.py\n"),
                         [("write_key1", "O26", "INT9")])

    def test_integration_pick_without_files_asks_salih(self):
        for picks, reps in ((["DFTONE a -> b", "INT9 fix (new) -> 816d1a1"], {}),  # glue: no report
                            (["DFTONE a -> b", "GLUE 1d1ae24"], {"GLUE": "STATUS: PASS\n"}),  # no FILES
                            (["DFTONE a -> b", "../../X9 -> c"], {})):  # never a path read
            with self.subTest(picks=picks):
                self.assertEqual(self.integration(picks, DFTONE="FILES: jarvis/a.py\n", **reps),
                                 [("ask_salih_key1", "CONVSAFE", "INT9")])
                (self.conv / "events.jsonl").unlink()
                shutil.rmtree(self.root / "build" / "bot", ignore_errors=True)

    def test_integration_picks_the_bot_cannot_fully_resolve_ask_salih(self):
        reps = {"DFTONE": "FILES: jarvis/a.py\n", "CONVPACE": "FILES: scripts/conveyor.py\n",
                "INT8": "STATUS: PASS\nFILES: jarvis/c.py\nPICKS (a -> b):\n  CONVPACE x -> y\n",
                "NOF": "FILES: none\n", "NOI": "FILES:\n  jarvis/x.py\n"}
        for picks in (["DFTONE a, CONVPACE b"],  # two picks on one line
                      ["DFTONE covers it; CONVPACE 1 -> 2"],  # a noted lane before the real pick
                      ["DFTONE a -> b", "INT8 x -> y"],  # a nested INT with its own PICKS
                      ["DFTONE a -> b", "NOF x"], ["DFTONE a -> b", "NOI x"],  # FILES with no path
                      ["DFTONE a -> b", "", "CONVPACE c -> d"],  # a blank line inside the block
                      []):  # a PICKS header with no pick lines
            with self.subTest(picks=picks):
                self.assertEqual(self.integration(picks, **reps), [("ask_salih_key1", "CONVSAFE", "INT9")])
                (self.conv / "events.jsonl").unlink()
                shutil.rmtree(self.root / "build" / "bot", ignore_errors=True)

    def test_integration_pick_touching_conveyor_asks_salih(self):
        self.assertEqual(self.integration(["e3f741a -> df463a5 CONVPACE"],
                                          CONVPACE="FILES: scripts/conveyor.py, tests/test_conveyor.py\n"),
                         [("ask_salih_key1", "CONVSAFE", "INT9")])

    def test_report_files_lines_are_parsed_loosely(self):
        for line in ("CHANGED FILES: `scripts/conveyor.py`:", "FILES: JARVIS_ACTIVE_STATE.md."):
            files = orchbot.Bot.changed(None, {"files": ["jarvis/a.py"]}, f"STATUS: PASS\n{line}\n")
            self.assertIsNotNone(orchbot.protected(files), line)


class O5O10BlockedTest(BotCase):
    def blocked(self, lane, reason="tests"):
        self.events({"ts": "2026-10-15T11:00:00", "event": "finish", "lane": lane, "status": "BLOCKED",
                     "reason": reason})

    def test_needs_files_queues_fix_then_escalates_on_third(self):
        for lane in ("L2", "L2-AFX", "L2-R"):
            self.put(f"build/reports/{lane}.md", "STATUS: BLOCKED\nNEEDS FILES: jarvis/b.py\n")
            self.blocked(lane)
        self.assertEqual(self.acts(self.run_bot()), [("queue_fix_slice", "O5", "L2"),
                                                     ("queue_fix_slice", "O5", "L2"),
                                                     ("escalate_orchestrator", "O5", "L2")])

    def test_blocked_with_dependents_escalates(self):
        self.put("build/conveyor/backlog.json", {"slices": [{"id": "S2", "depends_on": ["L3"]}]})
        self.put("build/reports/L3.md", "STATUS: BLOCKED\nNEEDS FILES:\n")
        self.blocked("L3")
        self.blocked("L4")
        self.blocked("L5", reason="CI: red")
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "O10", "L3"),
                                                     ("none", "O10", "L4")])


class StateRulesTest(BotCase):
    def test_pager_backlog_invalid_escalates_once(self):
        self.put("build/conveyor/pager.json", {"backlog_invalid": "slice S1: depends_on loop"})
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "PAGER", "backlog.json")])
        self.assertEqual(self.run_bot(), [])
        self.put("build/conveyor/pager.json", {"backlog_invalid": None})
        self.assertEqual(self.run_bot(), [])

    def test_routine_attention_older_than_25_min_is_acked_once(self):
        old, young = "2026-10-15T11:30:00", "2026-10-15T11:50:00"
        self.put("build/conveyor/status.json", {"attention": [
            {"id": "cleanup:L1", "reason": "cleanup refused (git worktree remove rc=1: x); retried after x",
             "since": old},
            {"id": "L2", "reason": "FINDINGS: 3; reader needs_orchestrator: y", "since": old},
            {"id": "L3", "reason": "BLOCKED: tests", "since": old},
            {"id": "preserved:L4", "reason": "x", "since": young},
            {"id": "preserved:L5", "reason": "x", "since": old},
        ]})
        ack = self.put("build/conveyor/ack/" + orchbot.cv.ack_name("preserved:L5"), "")
        os.utime(ack, (ts("2026-10-15T11:30:01"),) * 2)
        self.assertGreaterEqual(ack.stat().st_mtime, ts(old))
        self.assertEqual(self.acts(self.run_bot()), [("ack", "O33", "cleanup:L1"), ("ack", "O33", "L2")])
        self.assertEqual(self.run_bot(), [])

    def test_ack_from_the_future_hides_nothing(self):  # BOTREPLAY2: a replay's fake now < a real ack's mtime
        old = "2026-10-15T11:30:00"
        self.put("build/conveyor/status.json", {"attention": [
            {"id": "L2", "reason": "FINDINGS: 3; reader needs_orchestrator: y", "since": old}]})
        ack = self.put("build/conveyor/ack/" + orchbot.cv.ack_name("L2"), "")
        os.utime(ack, (ts("2026-10-15T13:00:00"),) * 2)
        self.assertEqual(self.acts(self.run_bot()), [("ack", "O33", "L2")])

    def test_cleanup_refusal_not_on_main_is_kept_and_logged_once_a_day(self):
        old = "2026-10-15T11:30:00"
        unmerged = {
            "cleanup:MDREAD": "cleanup refused (worktree-mdread has commits not on main (first abc1234)); retried after x",
            "cleanup:C2": "cleanup refused (worktree-c2 Content Not On Main (tree differs)); retried after x",
            "cleanup:C3": "cleanup refused (claude rm refused: run claude rm 1f2e --discard-unpushed abc@x); retried",
            "cleanup:C4": "cleanup refused (claude rm refused: ...ed-unpushed abc@x); retried after x",
            "cleanup:C5": "cleanup refused (branch worktree-c5 kept: not provably on main (x)); retried after x",
            "cleanup:C6": "cleanup refused (worktree-c6 has merge commits); retried after x",
            "cleanup:C7": "cleanup refused (worktree x not clean); retried after x",
            "cleanup:C8": "cleanup refused (claude rm refused: git worktree remove rc=1: x); retried after x",
            "cleanup:C9": "",
        }
        proven = {
            "cleanup:OK1": "cleanup refused (git worktree remove rc=1: locked); retried after x",
            "cleanup:OK2": "cleanup refused (git update-ref -d worktree-ok2 rc=128: err); retried after x",
        }
        self.put("build/conveyor/status.json", {"attention": [
            {"id": i, "reason": r, "since": old} for i, r in (unmerged | proven).items()]})
        keep = [("none", "KEEP-UNMERGED", i) for i in unmerged]
        self.assertEqual(self.acts(self.run_bot()), keep + [("ack", "O33", i) for i in proven])
        self.clock.set("2026-10-15T23:59:00")
        self.assertEqual(self.run_bot(), [])
        self.assertFalse(any((self.conv / "ack" / orchbot.cv.ack_name(i)).exists() for i in unmerged))
        self.clock.set("2026-10-16T00:01:00")
        self.assertEqual(self.acts(self.run_bot()), keep)
        self.assertEqual(self.run_bot(), [])

    def test_quota_above_ceiling_holds_dispatch_once_per_episode(self):
        self.events({"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 80, "projected_pct": 95},
                    {"ts": "2026-10-15T11:05:00", "event": "pace", "usage_pct": 96},
                    {"ts": "2026-10-15T11:10:00", "event": "pace", "usage_pct": 50})
        self.assertEqual(self.acts(self.run_bot()), [("hold_dispatch", "O23", "dispatch")])
        self.assertEqual(self.acts(self.run_bot(quota_ceiling=99)), [])


class OutageTest(BotCase):
    def test_replay_r12_respawn_storm_one_hold_one_page(self):
        storm = [("17:06:17", "SEC2APP"), ("17:06:18", "SEC2CORE"), ("17:07:20", "SECREDTEAM"),
                 ("17:08:22", "SEC2APP"), ("17:08:23", "SEC2CORE"), ("17:09:25", "SECREDTEAM"),
                 ("17:10:27", "SEC2APP"), ("17:10:28", "SEC2CORE"), ("17:11:29", "SECREDTEAM"),
                 ("17:12:31", "SEC2APP"), ("17:12:32", "SEC2CORE"), ("17:13:35", "SECREDTEAM"),
                 ("17:15:39", "SEC2APP"), ("17:15:40", "SECREDTEAM"), ("17:15:41", "SEC2CORE"),
                 ("17:17:48", "SEC2APP"), ("17:17:48", "SEC2CORE"), ("17:18:50", "SECREDTEAM"),
                 ("17:18:51", "SKFEED"), ("17:20:38", "SEC2APP")]
        self.events(*({"ts": f"2026-10-15T{t}", "event": "respawn", "lane": lane, "id": "20c1c592", "rc": 0,
                       "output": "respawned 20c1c592\n"} for t, lane in storm))
        self.clock.set("2026-10-15T17:21:00")
        ds = self.run_bot()
        self.assertEqual(self.acts(ds), [("hold_dispatch", "EVTAUDIT1", "dispatch"),
                                         ("page_salih", "EVTAUDIT1", "salih")])
        self.assertIn("respawn SEC2APP", ds[0]["saw"])
        self.assertEqual(self.run_bot(), [])

    def test_api_error_text_holds_and_pages(self):
        self.events({"ts": "2026-10-15T11:00:00", "event": "respawn", "lane": "L1", "rc": 1,
                     "output": "API Error: 529 Overloaded"})
        self.assertEqual(self.acts(self.run_bot()), [("hold_dispatch", "EVTAUDIT1", "dispatch"),
                                                     ("page_salih", "EVTAUDIT1", "salih")])
        self.events({"ts": "2026-10-15T14:00:00", "event": "error", "lane": "L1",
                     "message": "Credit balance is too low"},
                    {"ts": "2026-10-15T14:01:00", "event": "error", "lane": "L2", "message": "plain bug"})
        self.assertEqual(self.acts(self.run_bot()), [("hold_dispatch", "EVTAUDIT1", "dispatch"),
                                                     ("page_salih", "EVTAUDIT1", "salih")])


class R7AndLoopGuardTest(BotCase):
    def blocked(self, t, reason="local main c991c1684f45 is not ea29fc7ab7fb"):
        return {"ts": f"2026-10-18T{t}", "event": "push_blocked",
                "approval": f"push-{SHA2}-intconv2", "reason": reason}

    def test_replay_r7_push_blocked_asks_salih_each_episode(self):
        self.events(self.blocked("14:57:29", "check error: TimeoutExpired"), self.blocked("15:49:51"),
                    self.blocked("19:01:43"))
        self.clock.set("2026-10-18T19:02:00")
        self.assertEqual([d["would_do"] for d in self.run_bot()], ["ask_salih_key1"] * 3)

    def test_same_action_same_target_3x_in_2h_escalates(self):
        self.events(self.blocked("10:00:00"), self.blocked("10:30:00"))
        self.clock.set("2026-10-18T10:31:00")
        self.assertEqual([d["would_do"] for d in self.run_bot()], ["ask_salih_key1"] * 2)
        self.events(self.blocked("11:00:00"))  # history survives the pass
        self.clock.set("2026-10-18T11:01:00")
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "LOOP", f"push-{SHA2}-intconv2")])


class FailClosedTest(BotCase):
    def test_bad_event_line_escalates_and_the_pass_goes_on(self):
        self.events("{not json", "[1, 2]", {"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99})
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "events.jsonl")] * 2
                         + [("hold_dispatch", "O23", "dispatch")])

    def test_half_written_line_waits_for_the_next_pass(self):
        line = json.dumps({"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99})
        with open(self.conv / "events.jsonl", "w", encoding="utf-8") as f:
            f.write(line[:10])
        self.assertEqual(self.run_bot(), [])
        with open(self.conv / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(line[10:] + "\n")
        self.assertEqual(self.acts(self.run_bot()), [("hold_dispatch", "O23", "dispatch")])
        self.assertEqual(self.shadow()["cursor"], (self.conv / "events.jsonl").stat().st_size)

    def test_first_pass_starts_at_the_end_and_shrink_escalates(self):
        self.events({"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99})
        self.assertEqual(self.run_bot(from_start=False), [])
        (self.conv / "events.jsonl").write_text("", encoding="utf-8")
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "events.jsonl")])
        self.assertEqual(self.shadow()["cursor"], 0)

    def test_corrupt_shadow_json_escalates_and_resumes_at_the_end(self):
        self.events({"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99})
        self.put("build/bot/shadow.json", "{half")
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "shadow.json")])
        self.assertEqual(self.shadow()["cursor"], (self.conv / "events.jsonl").stat().st_size)

    def test_deeply_nested_line_escalates_and_the_pass_goes_on(self):
        self.events("[" * 100000, {"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99})
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "events.jsonl"),
                                                     ("hold_dispatch", "O23", "dispatch")])

    def test_replaced_events_file_escalates_and_replays_from_0(self):
        self.events({"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 50})
        self.assertEqual(self.run_bot(), [])
        (self.conv / "events.jsonl").unlink()
        self.events({"ts": "2026-10-15T11:10:00", "event": "pace", "usage_pct": 99, "lane": "rotated-file"})
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "events.jsonl"),
                                                     ("hold_dispatch", "O23", "dispatch")])

    def test_bad_shadow_shape_resets_and_the_pass_completes(self):
        self.put("build/bot/shadow.json", {"cursor": 0, "history": [["x"]]})
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "shadow.json")])
        self.assertEqual([h[1:] for h in self.shadow()["history"]], [["escalate_orchestrator", "shadow.json"]])

    def test_decisions_jsonl_unwritable_escalates_into_shadow(self):
        (self.root / "build" / "bot" / "decisions.jsonl").mkdir(parents=True)
        self.events({"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99})
        self.run_bot()
        self.assertEqual(self.acts(self.shadow()["decisions"]), [
            ("hold_dispatch", "O23", "dispatch"), ("escalate_orchestrator", "FAILCLOSED", "decisions.jsonl")])

    def test_bad_lanes_json_escalates_once_per_episode(self):
        self.put("build/conveyor/lanes.json", "[]")
        self.put("build/conveyor/status.json", "{oops")
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "lanes.json"),
                                                     ("escalate_orchestrator", "FAILCLOSED", "status.json")])
        self.clock.set("2026-10-15T13:00:00")
        self.assertEqual(self.run_bot(), [])
        self.clock.set("2026-10-15T15:30:00")
        self.assertEqual(len(self.run_bot()), 2)

    def test_handler_error_escalates(self):
        self.put("build/conveyor/lanes.json", {"L1": ["not", "a", "dict"]})
        self.events({"ts": "2026-10-15T11:00:00", "event": "ci_result", "lane": "L1", "sha": SHA,
                     "result": "success"})
        # the same bad lane also stops the key-1 ask (BOTASKS), which escalates on its own
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "orchbot"),
                                                     ("escalate_orchestrator", "FAILCLOSED", "asks.json")])


class ShadowWriteTest(BotCase):
    def test_permission_error_retried_5x_then_one_escalation(self):
        self.events({"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99})
        # facts.md / asks.json are out of this test (FactsTest / AsksTest cover their own write failure)
        with mock.patch.object(orchbot.os, "replace", side_effect=PermissionError("held")) as rep, \
                mock.patch.object(orchbot.Bot, "write_facts"), mock.patch.object(orchbot.Bot, "write_asks"):
            ds = self.run_bot()
        self.assertEqual(rep.call_count, 5)
        self.assertEqual(self.sleeps, [0.2] * 4)
        self.assertEqual(self.acts(ds)[-1], ("escalate_orchestrator", "FAILCLOSED", "shadow.json"))
        self.assertFalse((self.root / "build" / "bot" / "shadow.json").exists())
        self.assertEqual([d["would_do"] for d in self.logged()], ["hold_dispatch", "escalate_orchestrator"])

    def test_retry_succeeds_and_keeps_the_last_50(self):
        self.events(*({"ts": "2026-10-15T11:00:00", "event": "push_blocked", "approval": f"push-{i:07x}",
                       "reason": "x"} for i in range(60)))
        real = orchbot.os.replace
        calls = []

        def flaky(a, b):
            calls.append(a)
            if len(calls) == 1:
                raise PermissionError("held")
            return real(a, b)

        with mock.patch.object(orchbot.os, "replace", side_effect=flaky):
            self.run_bot()
        st = self.shadow()
        self.assertEqual(self.sleeps, [0.2])
        self.assertEqual(len(st["decisions"]), 50)
        self.assertEqual(st["decisions"][-1]["target"], "push-000003b")
        self.assertEqual(st["counters"]["ask_salih_key1"], 60)
        self.assertEqual(len(self.logged()), 60)
        self.assertFalse((self.root / "build" / "bot" / "shadow.json.tmp").exists())


class AgreementTest(BotCase):
    """BOTAGREE: a bot decision vs the orchestrator's actual action on the same target within MATCH_S."""
    def setUp(self):
        super().setUp()
        self.put("build/conveyor/lanes.json", {"L1": {"files": ["jarvis/a.py"], "ci": {"cand": True, "sha": SHA}}})
        self.events({"ts": "2026-10-15T11:00:00", "event": "ci_result", "lane": "L1", "sha": SHA,
                     "result": "success"})
        self.assertEqual(self.acts(self.run_bot()), [("write_key1", "O26", "L1")])  # pending at 11:00, baseline

    def approve(self, at, text="approved", name=f"push-{SHA}-L1"):
        p = self.put(f"build/approvals/{name}", text)
        os.utime(p, (ts(at), ts(at)))

    def seed(self, agreement):
        s = self.shadow()  # keep the cursor so the green event is not replayed
        s["agreement"] = agreement
        self.put("build/bot/shadow.json", s)

    def kinds(self):
        return [v["verdict"] for v in self.verdicts()]

    def test_agree_and_an_idempotent_re_pass(self):
        self.approve("2026-10-15T11:30:00")
        self.clock.set("2026-10-15T12:05:00")
        self.run_bot()
        self.assertEqual(self.kinds(), ["agree"])
        v = self.verdicts()[0]
        self.assertEqual((v["key"], v["bot"]["would_do"], v["orchestrator"]["did"], v["orchestrator"]["side"]),
                         (f"key1:l1@{SHA[:7]}", "write_key1", "write_key1", "orchestrator"))
        ag = self.shadow()["agreement"]
        self.assertEqual(ag["key1"], {"agree": 1, "total": 1})
        self.assertEqual(ag["totals"], {"write_key1": {"agree": 1}})
        self.assertEqual(ag["rules"], {"O26": {"agree": 1}})
        self.assertEqual((ag["pending"], ag["actual"], ag["recent"]), ([], [], []))
        self.assertEqual(ag["since"], "2026-10-15")
        for at in ("2026-10-15T12:06:00", "2026-10-15T14:00:00"):
            self.clock.set(at)
            self.run_bot()
        self.assertEqual(self.kinds(), ["agree"])
        again = self.shadow()["agreement"]
        self.assertEqual({k: again[k] for k in ("totals", "rules", "key1", "recent")},
                         {k: ag[k] for k in ("totals", "rules", "key1", "recent")})

    def test_disagree_keeps_both_sides(self):
        self.approve("2026-10-15T11:30:00", "approved by salih")
        self.clock.set("2026-10-15T12:05:00")
        self.run_bot()
        self.assertEqual(self.kinds(), ["disagree"])
        ag = self.shadow()["agreement"]
        self.assertEqual(ag["key1"], {"agree": 0, "total": 1})
        self.assertEqual(len(ag["recent"]), 1)
        r = ag["recent"][0]
        self.assertEqual((r["bot"]["would_do"], r["bot"]["at"], r["orchestrator"]["did"], r["orchestrator"]["at"]),
                         ("write_key1", "2026-10-15T11:00:00", "ask_salih_key1", "2026-10-15T11:30:00"))

    def test_unmatched_after_the_window(self):
        self.clock.set("2026-10-15T12:00:00")
        self.run_bot()
        self.assertEqual(self.kinds(), [])
        self.clock.set("2026-10-15T12:00:01")
        self.run_bot()
        self.assertEqual(self.kinds(), ["unmatched"])
        v = self.verdicts()[0]
        self.assertIsNone(v["orchestrator"])
        self.assertEqual(self.shadow()["agreement"]["key1"], {"agree": 0, "total": 1})

    def test_missed_an_action_the_bot_never_proposed(self):
        self.approve("2026-10-15T12:10:00", name=f"push-{SHA2}-L2")
        self.clock.set("2026-10-15T13:10:00")
        self.run_bot()
        self.assertEqual(self.kinds(), ["unmatched"])  # the 11:00 L1 decision; the L2 action is still in its window
        self.clock.set("2026-10-15T13:10:01")
        self.run_bot()
        self.assertEqual(self.kinds(), ["unmatched", "missed"])
        v = self.verdicts()[1]
        self.assertEqual((v["key"], v["bot"], v["orchestrator"]["did"]), (f"key1:l2@{SHA2[:7]}", None, "write_key1"))
        ag = self.shadow()["agreement"]
        self.assertEqual(ag["key1"], {"agree": 0, "total": 2})
        self.assertEqual([r["verdict"] for r in ag["recent"]], ["unmatched", "missed"])

    def test_key1_agree_key_keeps_sha(self):
        k = orchbot.agree_key
        self.assertNotEqual(k("write_key1", f"push-{SHA}-l"), k("write_key1", f"push-{SHA2}-l"))
        self.assertEqual(k("write_key1", f"push-{SHA}-L"), f"key1:l@{SHA[:7]}")
        self.assertEqual(k("write_key1", "L"), "key1:l")

    def test_window_edge_3600_matches(self):
        self.approve("2026-10-15T12:00:00")
        self.clock.set("2026-10-15T12:00:00")
        self.run_bot()
        self.assertEqual(self.kinds(), ["agree"])

    def test_window_edge_3601_is_unmatched_then_missed(self):
        self.approve("2026-10-15T12:00:01")
        self.clock.set("2026-10-15T12:00:01")
        self.run_bot()
        self.assertEqual(self.kinds(), ["unmatched"])
        self.clock.set("2026-10-15T13:00:02")
        self.run_bot()
        self.assertEqual(self.kinds(), ["unmatched", "missed"])

    def test_malformed_block_resets_to_empty(self):
        no_rule = [{"at": 0, "action": "write_key1", "key": "key1:l1"}]
        for bad in ({"pending": "x"}, {"pending": no_rule}, "x", {"key1": {"agree": "1", "total": 1}}, {"known": {"acks": []}}):
            with self.subTest(bad=bad):
                self.seed(bad)
                ds = self.run_bot()
                self.assertEqual(self.acts(ds), [("escalate_orchestrator", "FAILCLOSED", "shadow.json agreement")])
                ag = self.shadow()["agreement"]
                self.assertEqual(set(ag), set(orchbot.agree_blank()))
                self.assertEqual((ag["pending"], ag["key1"], ag["totals"]), ([], {"agree": 0, "total": 0}, {}))

    def test_graduation_thresholds(self):
        now = self.clock()
        for days, agree, total, ready in ((6, 10, 10, False), (7, 9, 9, False), (7, 9, 10, False),
                                          (7, 10, 10, True)):
            with self.subTest(days=days, agree=agree, total=total):
                self.seed({"start": now - days * 86400, "key1": {"agree": agree, "total": total}})
                self.assertEqual(self.run_bot(), [])
                g = self.shadow()["agreement"]["graduation"]
                self.assertEqual(g, {"days": days, "pushes": total, "agree_pct": round(100 * agree / total, 1),
                                     "ready": ready})


class FactsTest(BotCase):
    SECTIONS = ["## main", "## conveyor", "## quota", "## running lanes", "## next slices", "## last reports",
                "## approvals and push gate", "## attention", "## bot graduation"]

    def fixtures(self):
        self.put("build/conveyor/status.json", {
            "updated_at": "2026-10-15T11:58:00", "paused": False, "band": ["day"], "usage_pct": 42.5,
            "usage_hold": False, "memory_hold": True, "memory": {"free_gb": 2.5}, "breaker": "closed",
            "main": {"local": SHA, "origin": SHA2},
            "lanes": [{"id": "BOTX", "name": "BOTX", "model": "opus", "effort": "medium", "state": "running",
                       "started_at": "2026-10-15T11:30:00"}],
            "backlog": [{"id": "A", "state": "ready"}, {"id": "B", "state": "waiting_deps"}],
            "attention": [{"id": "lane:Z", "reason": "report missing", "since": "2026-10-15T10:00:00"}]})
        self.put("build/conveyor/pace.json", {"window_start": "2026-10-13T09:00:00", "days": 7})
        self.put("build/conveyor/backlog.json", {"slices": [{"id": "A"}, {"id": "B", "depends_on": ["A"]}]})
        self.report("OLD", f"STATUS: BLOCKED\nCOMMIT: {SHA2}\n", "2026-10-15T09:00:00")
        self.report("NEW", f"STATUS: PASS\nBRANCH: new\nCOMMIT: {SHA}\nCANDIDATE SHA: {SHA2}\nCI BRANCH: ci/new\n",
                    "2026-10-15T10:00:00")
        self.put(f"build/approvals/push-{SHA}-botfacts", "ok")
        self.put(f"build/approvals/done/push-{SHA2[:12]}", "ok")
        self.put(".git/refs/heads/main", SHA + "\n")
        self.put(".git/packed-refs", f"# pack-refs with: peeled\n{SHA2} refs/remotes/origin/main\n")

    def test_main_from_git_refs_without_git(self):
        self.fixtures()
        self.put(".git/refs/remotes/origin/main", SHA + "\n")  # loose ref wins over packed-refs
        self.assertEqual((orchbot.git_ref(self.root, "refs/heads/main"),
                          orchbot.git_ref(self.root, "refs/remotes/origin/main")), (SHA, SHA))
        (self.root / ".git" / "refs" / "remotes" / "origin" / "main").unlink()
        self.assertEqual(orchbot.git_ref(self.root, "refs/remotes/origin/main"), SHA2)  # packed-refs
        self.put(".git/refs/heads/main", "not a sha\n")
        self.assertIsNone(orchbot.git_ref(self.root, "refs/heads/main"))
        shutil.rmtree(self.root / ".git")
        self.put(".git", "gitdir: elsewhere")  # a worktree .git file: not read
        self.assertIsNone(orchbot.git_ref(self.root, "refs/heads/main"))
        self.run_bot()
        self.assertIn("- local: unknown (.git)\n", self.facts())

    def test_running_lanes_live_only_capped(self):
        self.fixtures()
        hist = [{"id": f"H{i}", "model": "opus", "effort": "x", "state": s, "started_at": "2026-10-01T00:00:00"}
                for i, s in enumerate(["done", "failed", None, "missing", "reported", ["working"]])]
        live = [{"id": f"W{i:02}", "model": "opus", "effort": "x", "state": s, "started_at": "2026-10-15T11:50:00"}
                for i, s in enumerate(["working", "running", "starting", "blocked"] * 3)]
        self.put("build/conveyor/status.json", {"lanes": hist + live})
        self.run_bot()
        text = self.facts()
        self.assertFalse(any(f"- H{i}:" in text for i in range(6)))
        self.assertEqual(sum(x.startswith("- W") for x in text.splitlines()), orchbot.LANES_MAX)
        self.assertIn("- +2 more (status.json)\n", text)

    def report(self, name, text, at):
        p = self.put(f"build/reports/{name}.md", text)
        os.utime(p, (ts(at), ts(at)))

    def facts(self):
        return (self.root / "build" / "bot" / "facts.md").read_text(encoding="utf-8")

    def test_full_render_fixed_order_every_line_sourced(self):
        self.fixtures()
        self.run_bot()
        text = self.facts()
        self.assertEqual([x for x in text.splitlines() if x.startswith("## ")], self.SECTIONS)
        for x in text.splitlines():
            if x.startswith("- ") or x.startswith("# "):
                self.assertRegex(x, r"\([\w./ +-]+\)$")
        for want in [f"- local: {SHA[:10]} (.git)", f"- origin: {SHA2[:10]} (.git)",
                     "- alive: yes, status written 2 min ago (status.json)", "- paused: no (status.json)",
                     "- band: day (status.json)", "- usage: 42.5% (status.json)", "- usage hold: no (status.json)",
                     f"- window reset: {orchbot.cv.iso(ts('2026-10-13T09:00:00') + 7 * 86400)} (pace.json)",
                     "- memory hold: yes, free 2.5 GB (status.json)",
                     "- BOTX: opus/medium, running 30 min, state running (status.json)",
                     "- A: ready (status.json)", "- B: waits on A (status.json + backlog.json)",
                     f"- NEW: PASS, commit {SHA[:10]}, candidate {SHA2[:10]}, ci ci/new (build/reports/NEW.md)",
                     f"- OLD: BLOCKED, commit {SHA2[:10]} (build/reports/OLD.md)",
                     "- push gate breaker: closed (status.json)", f"- push-{SHA}-botfacts pending (build/approvals)",
                     "- lane:Z: report missing since 2026-10-15T10:00:00 (status.json)",
                     "- days 0/7, key-1 pushes 0/10, agree 0%, ready no (shadow.json)"]:
            self.assertIn(want + "\n", text)
        self.assertLess(text.index("- NEW:"), text.index("- OLD:"))
        self.assertNotIn(SHA2[:12] + " pending", text)

    def test_each_missing_source_prints_unknown(self):
        self.fixtures()
        cases = {".git": ["- local: unknown (.git)", "- origin: unknown (.git)"],
                 "build/conveyor/status.json": ["- alive: unknown (status.json)",
                                                "- usage: unknown (status.json)", "- push gate breaker: unknown"],
                 "build/conveyor/pace.json": ["- window reset: unknown (pace.json)"],
                 "pace.json days": ["- window reset: unknown (pace.json)"],
                 "build/conveyor/backlog.json": ["- B: waits on unknown (backlog.json)"],
                 "backlog.json slices": ["- B: waits on unknown (backlog.json)"],
                 "backlog.json ids": ["- B: waits on unknown (status.json + backlog.json)"],
                 "build/reports": ["## last reports\n- unknown (build/reports)"],
                 "build/approvals": ["- unknown (build/approvals)"]}
        for gone, wants in cases.items():
            with self.subTest(gone=gone):
                self.fixtures()
                p = self.root / gone
                if gone == "pace.json days":
                    self.put("build/conveyor/pace.json", {"window_start": "2026-10-13T09:00:00", "days": "7"})
                elif gone == "backlog.json slices":
                    self.put("build/conveyor/backlog.json", {"slices": 5})
                elif gone == "backlog.json ids":
                    self.put("build/conveyor/backlog.json", {"slices": [{"id": ["A"]}]})
                elif p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.write_text("{not json", encoding="utf-8")
                self.run_bot()
                for want in wants:
                    self.assertIn(want, self.facts())
                self.assertEqual([x for x in self.facts().splitlines() if x.startswith("## ")], self.SECTIONS)
        b = orchbot.Bot(self.root, now=self.clock, sleep=self.sleeps.append)
        b.st = {"agreement": {"graduation": {"days": "x"}}}
        self.assertIn("- graduation: unknown (shadow.json)\n", b.facts())

    def test_size_cap_trims_oldest_report_rows_first(self):
        self.fixtures()
        pad = " ".join(["padding"] * 45)
        for i in range(8):
            self.report(f"R{i}", f"STATUS: PASS {pad}\n", f"2026-10-15T0{i}:30:00")
        self.put("build/conveyor/status.json", {"attention": [
            {"id": f"att{i}", "reason": "a" * 100, "since": "2026-10-15T10:00:00"} for i in range(20)]})
        self.run_bot()
        text = self.facts()
        self.assertLessEqual(len(text.encode("utf-8")), orchbot.FACTS_MAX)
        self.assertIn("- R7: PASS", text)  # newest stays
        self.assertNotIn("- R0: PASS", text)  # oldest goes first
        self.assertRegex(text, r"- \.\.\. \d+ more rows not shown \(size cap")
        self.assertEqual(sum(x.startswith("- att") for x in text.splitlines()), 20)  # attention untouched
        self.assertEqual([x for x in text.splitlines() if x.startswith("## ")], self.SECTIONS)

    def test_atomic_write_old_file_kept_on_failure(self):
        self.fixtures()
        self.run_bot()
        old = self.facts()
        self.assertFalse((self.root / "build" / "bot" / "facts.md.tmp").exists())
        real = orchbot.os.replace

        def held(a, b):
            if Path(b).name == "facts.md":
                raise PermissionError("held")
            return real(a, b)

        self.clock.set("2026-10-15T13:00:00")
        with mock.patch.object(orchbot.os, "replace", side_effect=held):
            ds = self.run_bot()
            ds += self.run_bot()
        self.assertEqual(self.facts(), old)
        self.assertEqual(self.acts(ds), [("escalate_orchestrator", "FAILCLOSED", "facts.md")])

    def test_no_secret_patterns(self):
        self.fixtures()
        topic = "jarvis-topic-q9"
        self.put("build/conveyor/status.json", {
            "lanes": [{"id": f"https://ntfy.sh/{topic}", "model": "sk-ant-api03-AAAABBBBCCCC", "effort": "x",
                       "started_at": "2026-10-15T11:00:00", "state": "ghp_" + "a1" * 18}],
            "attention": [{"id": "t", "reason": f"token=hunter2secret topic {topic} " + "Z9" * 20,
                           "since": "x"}]})
        self.report("LEAK", "STATUS: PASS key: abc123def\nCOMMIT: AKIAABCDEFGHIJKL\n", "2026-10-15T11:00:00")
        with mock.patch.dict(os.environ, {"JARVIS_NTFY_TOPIC": topic}):
            self.run_bot()
        text = self.facts()
        for bad in (topic, "ntfy.sh", "sk-ant", "ghp_", "hunter2", "abc123def", "AKIA", "Z9Z9Z9Z9"):
            self.assertNotIn(bad, text)
        self.assertLessEqual(len(text.encode("utf-8")), orchbot.FACTS_MAX)

    def test_quoted_key_secret_redacted(self):
        for raw in ('"token": "hunter2secret"', "'key'='hunter2secret'", '{"password":"hunter2secret"}',
                    "api_key=hunter2secret", "access_token=hunter2secret", "Authorization: Bearer hunter2secret",
                    "token => hunter2secret", '"token" : "a hunter2secret"'):
            with self.subTest(raw=raw):
                self.assertNotIn("hunter2", orchbot.clean(raw))
                self.assertNotIn("hunter2", orchbot.ask_text(raw, 300))


class AsksTest(BotCase):
    KEYS = {"id", "title", "blocks", "where", "time", "why", "steps", "cmds", "then", "since", "source"}

    def asks(self):
        return json.loads((self.root / "build" / "bot" / "asks.json").read_text(encoding="utf-8"))["asks"]

    def ask_log(self):
        return [d["detail"] for d in self.logged() if d["rule"] == "ASK"]

    def key1_lane(self, lane="INT63", files=(".github/workflows/tests.yml",)):
        self.put("build/conveyor/lanes.json", {lane: {"files": list(files), "status": "GREEN",
                                                      "ci": {"cand": True, "sha": SHA}}})

    def test_key1_opens_then_closes_on_the_approval_file(self):
        self.key1_lane()
        self.run_bot()
        a, = self.asks()
        self.assertEqual(a["id"], "key1:INT63")
        self.assertEqual(set(a), self.KEYS)
        self.assertEqual(a["source"]["rule"], "CONVSAFE")
        self.assertEqual(a["cmds"][0]["text"], f"New-Item -ItemType File build\\approvals\\push-{SHA}-INT63")
        self.run_bot()  # still open: logged once, since kept
        self.assertEqual(self.asks()[0]["since"], a["since"])
        self.assertEqual(self.shadow()["asks_open"], 1)
        self.put(f"build/approvals/push-{SHA}-INT63", "")
        self.clock.set("2026-10-15T12:10:00")
        self.run_bot()
        self.assertEqual(self.asks(), [])
        self.assertEqual(self.shadow()["asks_open"], 0)
        self.assertEqual(self.ask_log(), ["open key1:INT63", "close key1:INT63"])

    def test_key1_not_protected_or_not_green_is_no_ask(self):
        self.key1_lane(files=["jarvis/a.py"])
        self.run_bot()
        self.assertEqual(self.asks(), [])
        self.put("build/conveyor/lanes.json", {"L1": {"files": [".github/workflows/tests.yml"], "status": "BLOCKED",
                                                      "ci": {"cand": True, "sha": SHA}}})
        self.run_bot()
        self.assertEqual(self.asks(), [])
        self.assertEqual(self.ask_log(), [])

    STARTS = {100: ts("2026-10-15T11:00:00"), 200: ts("2026-10-15T12:25:00")}

    def push(self, lane_files, lane="L2", approval="L2"):
        self.put("build/conveyor/lanes.json", {lane: {"files": lane_files}})
        self.put("build/conveyor/status.json", {"pid": 100})
        self.run_bot(started=self.STARTS.get)
        self.events({"ts": "2026-10-15T12:05:00", "event": "push", "sha": SHA,
                     "approval": f"push-{SHA}-{approval}"})
        self.clock.set("2026-10-15T12:10:00")
        self.run_bot(started=self.STARTS.get)

    def test_restart_opens_after_a_conveyor_push_and_closes_on_a_new_pid(self):
        self.push(["scripts/conveyor.py", "tests/test_conveyor.py"])
        a, = self.asks()
        self.assertEqual((a["id"], a["source"]["rule"], a["time"]), ("restart", "O39", "6 dk"))
        self.assertEqual(a["cmds"][0]["text"], orchbot.RESTART_CMD)
        self.clock.set("2026-10-15T12:20:00")
        self.run_bot(started=self.STARTS.get)  # same pid: still open
        self.assertEqual(len(self.asks()), 1)
        self.put("build/conveyor/status.json", {"pid": 200})
        self.clock.set("2026-10-15T12:30:00")
        self.run_bot(started=self.STARTS.get)
        self.assertEqual(self.asks(), [])
        self.assertEqual(self.ask_log(), ["open restart", "close restart"])

    def test_restart_live_int63b_push_while_old_conveyor_runs(self):
        # live case (c): INT63B pushed scripts/conveyor.py, approval name is lowercase, conveyor started before
        self.push(["scripts/conveyor.py"], lane="INT63B", approval="int63b")
        a, = self.asks()
        self.assertEqual(a["id"], "restart")
        self.assertIn("INT63B", a["why"])

    def test_restart_conveyor_started_after_the_push_is_no_ask(self):
        self.STARTS = {100: ts("2026-10-15T12:06:00")}
        self.push(["scripts/conveyor.py"])
        self.assertEqual(self.asks(), [])
        self.assertEqual(self.ask_log(), [])

    def test_restart_not_asked_when_conveyor_py_unchanged(self):
        self.push(["jarvis/a.py"])
        self.assertEqual(self.asks(), [])

    def test_restart_unreadable_start_escalates_once(self):
        def gone(pid):
            raise OSError("no such process")

        for started in (gone, lambda pid: None):
            with self.subTest(started=started):
                shutil.rmtree(self.root / "build" / "bot", ignore_errors=True)
                (self.conv / "events.jsonl").unlink(missing_ok=True)
                self.put("build/conveyor/lanes.json", {"L2": {"files": ["scripts/conveyor.py"]}})
                self.events({"ts": "2026-10-15T11:05:00", "event": "push", "sha": SHA,
                             "approval": f"push-{SHA}-L2"})
                self.put("build/conveyor/status.json", {"pid": 100})
                ds = self.run_bot(started=started) + self.run_bot(started=started)
                self.assertEqual(self.asks(), [])
                self.assertEqual(self.acts(ds), [("escalate_orchestrator", "FAILCLOSED", "asks.json")])

    def test_push_of_unknown_lane_escalates(self):
        self.put("build/conveyor/lanes.json", {})
        self.events({"ts": "2026-10-15T11:05:00", "event": "push", "sha": SHA, "approval": f"push-{SHA}-ZZ"})
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "asks.json")])

    def test_t2_opens_when_it_alone_keeps_the_queue_dry_and_closes(self):
        self.put("build/conveyor/status.json", {"backlog": [{"id": "T2X", "state": "needs_t2_approval"},
                                                            {"id": "D1", "state": "done"}], "lanes": []})
        self.run_bot()
        a, = self.asks()
        self.assertEqual((a["id"], a["source"]["rule"]), ("t2:T2X", "T2"))
        self.put("build/conveyor/status.json", {"backlog": [{"id": "T2X", "state": "needs_t2_approval"},
                                                            {"id": "R1", "state": "ready"}], "lanes": []})
        self.run_bot()
        self.assertEqual(self.asks(), [])
        self.assertEqual(self.ask_log(), ["open t2:T2X", "close t2:T2X"])

    def test_t2_not_asked_while_a_lane_works(self):
        self.put("build/conveyor/status.json", {"backlog": [{"id": "T2X", "state": "needs_t2_approval"}],
                                                "lanes": [{"id": "L1", "state": "working"}]})
        self.run_bot()
        self.assertEqual(self.asks(), [])

    def test_t2_lanes_missing_or_not_list_is_unknown_not_idle(self):
        for lanes in ({}, {"lanes": None}, {"lanes": {"L1": "working"}}):
            with self.subTest(lanes=lanes):
                shutil.rmtree(self.root / "build" / "bot", ignore_errors=True)
                self.put("build/conveyor/status.json",
                         {"backlog": [{"id": "T2X", "state": "needs_t2_approval"}], **lanes})
                ds = self.run_bot() + self.run_bot()
                self.assertEqual(self.asks(), [])
                self.assertEqual(self.acts(ds), [("escalate_orchestrator", "FAILCLOSED", "asks.json")])

    def test_key1_cmd_too_long_fails_closed(self):
        self.key1_lane(lane="L" + "x" * 300)  # cmd naming the approval file would pass the 300 cap
        ds = self.run_bot() + self.run_bot()
        self.assertEqual(self.asks(), [])
        self.assertEqual(self.acts(ds), [("escalate_orchestrator", "FAILCLOSED", "asks.json")])

    def test_ask_id_and_source_sanitized(self):
        topic = "jarvis-topic-q9"
        self.put("build/conveyor/status.json", {"lanes": [], "backlog": [
            {"id": f"https://ntfy.sh/{topic} token=hunter2secret C:\\Users\\x\\.jarvis\\k " + "T" * 200,
             "state": "needs_t2_approval"}]})
        with mock.patch.dict(os.environ, {"JARVIS_NTFY_TOPIC": topic}):
            self.run_bot()
        a, = self.asks()
        text = json.dumps([a["id"], a["source"]], ensure_ascii=False)
        for bad in (topic, "ntfy.sh", "hunter2", "C:\\\\Users", ".jarvis"):
            self.assertNotIn(bad, text)
        self.assertLessEqual(len(a["id"]), orchbot.ASK_MAX["where"])
        self.assertEqual(a["source"]["rule"], "T2")

    def test_ask_id_prefix_smuggled_path_and_collisions(self):
        ids = ["t2:aC:\\ProgramData\\x", "t2:a\\\\srv\\share", "t2:a~\\.jarvis\\k", "T" * 90 + "a", "T" * 90 + "b"]
        self.put("build/conveyor/status.json", {"lanes": [], "backlog": [
            {"id": i, "state": "needs_t2_approval"} for i in ids]})
        self.run_bot()
        got = [a["id"] for a in self.asks()]
        self.assertEqual(len(set(got)), len(ids))
        for bad in ("ProgramData", "srv", ".jarvis"):
            self.assertFalse(any(bad in g for g in got), bad)

    def test_t2_unreadable_or_live_non_working_lane_is_busy(self):
        for lane in ("L1", {"state": "blocked"}, {"state": "Starting"}, {"state": ["working"]}):
            with self.subTest(lane=lane):
                shutil.rmtree(self.root / "build" / "bot", ignore_errors=True)
                self.put("build/conveyor/status.json",
                         {"backlog": [{"id": "T2X", "state": "needs_t2_approval"}], "lanes": [lane]})
                self.run_bot()
                self.assertEqual(self.asks(), [])

    def test_text_limits_and_where(self):
        long = "L" + "x" * 200  # longest lane whose key-1 cmd still fits uncut (m6)
        self.key1_lane(lane=long)
        self.put("build/conveyor/status.json", {"backlog": [{"id": "T" * 300, "state": "needs_t2_approval"}],
                                                "lanes": []})
        self.run_bot()
        asks = self.asks()
        self.assertEqual(len(asks), 2)
        for a in asks:
            self.assertLessEqual(len(a["title"]), 60)
            self.assertLessEqual(len(a["why"]), 140)
            self.assertLessEqual(len(a["blocks"]), 140)
            self.assertLessEqual(len(a["steps"]), 3)
            self.assertTrue(all(len(s) <= 90 for s in a["steps"]))
            self.assertFalse(a["where"].startswith("Bilgi"))
            self.assertTrue(a["cmds"] and all(set(c) == {"label", "text"} for c in a["cmds"]))

    def test_no_secrets_or_outside_paths(self):
        topic = "jarvis-topic-q9"
        lane = "sk-ant-api03-AAAABBBBCCCC"
        self.put("build/conveyor/lanes.json", {lane: {"files": ["C:\\Users\\x\\.jarvis\\k.pem", "~/.ssh/id",
                                                                  ".github/workflows/tests.yml"],
                                                      "status": "GREEN", "ci": {"cand": True, "sha": SHA}}})
        self.put("build/conveyor/status.json", {"backlog": [
            {"id": f"https://ntfy.sh/{topic} token=hunter2secret ghp_{'a1' * 18} AKIAABCDEFGHIJKL",
             "state": "needs_t2_approval"}], "lanes": []})
        with mock.patch.dict(os.environ, {"JARVIS_NTFY_TOPIC": topic}):
            ds = self.run_bot()
        asks = self.asks()
        # m6: the key-1 cmd would carry the sk-ant lane, so it is refused (fail closed), never rewritten
        self.assertEqual([a["id"] for a in asks], [asks[0]["id"]])
        self.assertEqual(asks[0]["source"]["rule"], "T2")
        self.assertIn(("escalate_orchestrator", "FAILCLOSED", "asks.json"), self.acts(ds))
        text = json.dumps(asks, ensure_ascii=False)  # M2: id and source checked too
        for bad in (topic, "ntfy.sh", "sk-ant", "ghp_", "hunter2", "AKIA", "C:\\\\Users", "~/", ".jarvis"):
            self.assertNotIn(bad, text)

    def test_atomic_write_old_file_kept_on_failure(self):
        self.key1_lane()
        self.run_bot()
        p = self.root / "build" / "bot" / "asks.json"
        old = p.read_text(encoding="utf-8")
        real = orchbot.os.replace

        def held(a, b):
            if Path(b).name == "asks.json":
                raise PermissionError("held")
            return real(a, b)

        self.put(f"build/approvals/push-{SHA}-INT63", "")
        self.clock.set("2026-10-15T13:00:00")
        with mock.patch.object(orchbot.os, "replace", side_effect=held):
            ds = self.run_bot()
            ds += self.run_bot()
        self.assertEqual(p.read_text(encoding="utf-8"), old)
        self.assertFalse(p.with_name("asks.json.tmp").exists())
        self.assertEqual(self.acts([d for d in ds if d["rule"] != "ASK"]),
                         [("escalate_orchestrator", "FAILCLOSED", "asks.json")])

    def test_malformed_input_gives_no_ask_and_one_escalate(self):
        for rel, data in (("build/conveyor/lanes.json", {"L1": "nope"}),
                          ("build/conveyor/lanes.json", {"L1": {"files": ["a"], "status": "GREEN",
                                                                "ci": {"cand": True, "sha": "zz"}}}),
                          ("build/conveyor/lanes.json", {"L1; rm x": {"files": [".github/workflows/tests.yml"],
                                                                      "status": "GREEN",
                                                                      "ci": {"cand": True, "sha": SHA}}}),
                          ("build/conveyor/status.json", {"backlog": "nope"}),
                          ("build/conveyor/status.json", {"backlog": [{"state": "needs_t2_approval"}]})):
            with self.subTest(data=data):
                shutil.rmtree(self.root / "build" / "bot", ignore_errors=True)
                for f in ("lanes.json", "status.json"):
                    (self.conv / f).unlink(missing_ok=True)
                self.put(rel, data)
                ds = self.run_bot() + self.run_bot()
                self.assertEqual(self.asks(), [])
                self.assertEqual(self.acts(ds), [("escalate_orchestrator", "FAILCLOSED", "asks.json")])
                self.assertEqual(self.ask_log(), [])


class MainTest(BotCase):
    def test_cli_exit_0_even_on_a_broken_root(self):
        self.events({"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99})
        self.assertEqual(orchbot.main(["--shadow", "--once", "--root", str(self.root)], now=self.clock), 0)
        self.assertEqual(self.shadow()["counters"]["passes"], 1)
        (self.root / "build" / "bot" / "shadow.json").unlink()
        shutil.rmtree(self.root / "build" / "bot")
        self.put("build/bot", "a file where the dir should be")
        self.assertEqual(orchbot.main(["--shadow", "--root", str(self.root)], now=self.clock), 0)

    def test_bad_command_line_exits_0(self):
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(orchbot.main(["--once"]), 0)
        self.assertIn("--shadow", err.getvalue())


class LiveTest(BotCase):
    """BOTLIVE: each live action vs shadow, OFF wins, malformed or never-live config is shadow, PAUSE
    ownership, the loop guard demotes."""
    L2 = "FINDINGS: 3; reader needs_orchestrator: y"
    QUOTA = {"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99}

    def live(self, **cfg):
        return self.put("build/bot/live.json", cfg)

    def attention(self, since, iid="L2", reason=L2):
        self.put("build/conveyor/status.json", {"attention": [{"id": iid, "reason": reason, "since": since}]})

    def recs(self, ds):
        return [d for d in ds if d.get("mode") == "live"]

    def shadow_acts(self, ds):
        return self.acts([d for d in ds if "mode" not in d])

    def ack(self, iid="L2"):
        return self.conv / "ack" / orchbot.cv.ack_name(iid)

    def pause(self):
        return self.conv / "PAUSE"

    def test_ack_live_touches_the_ack_and_logs_before_after_undo(self):
        self.live(ack=True)
        self.attention("2026-10-15T11:30:00")
        ds = self.run_bot()
        self.assertEqual(self.shadow_acts(ds), [("ack", "O33", "L2")])
        self.assertTrue(self.ack().exists())
        self.assertEqual(self.ack().stat().st_mtime, ts("2026-10-15T12:00:00"))
        [r] = self.recs(ds)
        self.assertEqual((r["did"], r["rule"], r["target"], r["before"]), ("ack", "O33", "L2", "absent"))
        self.assertEqual(r["file"], "build/conveyor/ack/" + orchbot.cv.ack_name("L2"))
        self.assertEqual(r["undo"], "delete build/conveyor/ack/" + orchbot.cv.ack_name("L2"))
        self.assertEqual(r["after"]["bytes"], 0)
        self.assertIn(r, self.logged())
        self.clock.set("2026-10-15T12:10:00")
        self.assertEqual(self.run_bot(), [])  # the ack now hides the item

    def test_ack_shadow_touches_nothing(self):
        self.live(ack=False, asks=True)
        self.attention("2026-10-15T11:30:00")
        ds = self.run_bot()
        self.assertEqual(self.shadow_acts(ds), [("ack", "O33", "L2")])
        self.assertFalse(self.ack().exists())
        self.assertEqual([r["target"] for r in self.recs(ds)], ["asks"])

    def test_ack_live_respects_keep_unmerged(self):
        self.live(ack=True)
        iid = "cleanup:MDREAD"
        self.attention("2026-10-15T11:30:00", iid, "cleanup refused (worktree-mdread has commits not on main "
                                                   "(first abc1234)); retried after x")
        ds = self.run_bot()
        self.assertEqual(self.acts(ds), [("none", "KEEP-UNMERGED", iid)])
        self.assertFalse(self.ack(iid).exists())

    def test_asks_live_marks_them_for_the_pano_and_shadow_does_not(self):
        self.put("build/conveyor/lanes.json", {"INT63": {"files": [".github/workflows/tests.yml"],
                                                         "status": "GREEN", "ci": {"cand": True, "sha": SHA}}})
        path = self.root / "build" / "bot" / "asks.json"
        self.run_bot()
        body = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("live", body)
        self.assertEqual([("pano" in a) for a in body["asks"]], [False])
        self.live(asks=True)
        self.clock.set("2026-10-15T12:05:00")
        [r] = self.recs(self.run_bot())
        self.assertEqual((r["did"], r["target"], r["file"], r["before"], r["after"]),
                         ("mark", "asks", "build/bot/asks.json", "shadow", "live"))
        self.assertEqual(r["undo"], "set asks false in build/bot/live.json")
        body = json.loads(path.read_text(encoding="utf-8"))
        self.assertIs(body["live"], True)
        self.assertEqual([(a["id"], a["pano"]) for a in body["asks"]], [("key1:INT63", True)])
        self.clock.set("2026-10-15T12:10:00")
        self.assertEqual(self.recs(self.run_bot()), [])  # marked once, not every pass
        self.live(asks=False)
        self.clock.set("2026-10-15T12:15:00")
        self.assertEqual([r["did"] for r in self.recs(self.run_bot())], ["unmark"])
        self.assertNotIn("live", json.loads(path.read_text(encoding="utf-8")))

    def test_hold_dispatch_live_pauses_then_releases_its_own_pause(self):
        self.live(hold_dispatch=True)
        self.events(self.QUOTA)
        ds = self.run_bot()
        self.assertEqual(self.shadow_acts(ds), [("hold_dispatch", "O23", "dispatch")])
        self.assertEqual(self.pause().read_text(encoding="utf-8"), "bot: O23")
        [r] = self.recs(ds)
        self.assertEqual((r["did"], r["before"], r["undo"]), ("hold_dispatch", "absent", "delete build/conveyor/PAUSE"))
        self.clock.set("2026-10-15T12:30:00")
        self.assertEqual(self.run_bot(), [])
        self.assertTrue(self.pause().exists())  # the quota episode is still open
        self.clock.set("2026-10-15T13:30:00")
        [r] = self.recs(self.run_bot())
        self.assertEqual((r["did"], r["after"], r["undo"]), ("unpause", "absent",
                                                            "recreate build/conveyor/PAUSE with 'bot: O23'"))
        self.assertFalse(self.pause().exists())

    def test_hold_dispatch_shadow_writes_no_pause(self):
        self.events(self.QUOTA)
        self.assertEqual(self.shadow_acts(self.run_bot()), [("hold_dispatch", "O23", "dispatch")])
        self.assertFalse(self.pause().exists())

    def test_a_pause_the_bot_did_not_create_is_never_removed(self):
        self.live(hold_dispatch=True)
        self.put("build/conveyor/PAUSE", "salih")
        self.events(self.QUOTA)
        [r] = self.recs(self.run_bot())
        self.assertEqual((r["did"], r["undo"]), ("kept", "none: a PAUSE the bot did not create is kept"))
        self.clock.set("2026-10-15T15:00:00")
        self.run_bot()
        self.assertEqual(self.pause().read_text(encoding="utf-8"), "salih")

    def test_a_bot_pause_a_human_edited_is_no_longer_the_bots(self):
        self.live(hold_dispatch=True)
        self.events(self.QUOTA)
        self.run_bot()
        self.put("build/conveyor/PAUSE", "salih: keep it")
        self.clock.set("2026-10-15T15:00:00")
        self.assertEqual(self.recs(self.run_bot()), [])
        self.assertEqual(self.pause().read_text(encoding="utf-8"), "salih: keep it")
        self.assertNotIn("pause", self.shadow().get("live", {}))

    def test_off_wins_over_live_json(self):
        self.live(ack=True, asks=True, hold_dispatch=True)
        self.put("build/bot/OFF", "")
        self.attention("2026-10-15T11:30:00")
        self.events(self.QUOTA)
        ds = self.run_bot()
        self.assertEqual(self.recs(ds), [])
        self.assertEqual(self.shadow_acts(ds), [("hold_dispatch", "O23", "dispatch"), ("ack", "O33", "L2")])
        self.assertFalse(self.ack().exists() or self.pause().exists())
        self.assertNotIn("live", json.loads((self.root / "build/bot/asks.json").read_text(encoding="utf-8")))

    def test_malformed_live_json_is_shadow_and_escalates(self):
        for n, body in enumerate(("{not json", {"ack": "yes"}, {"ack": 1}, [True])):
            with self.subTest(body=body):
                self.put("build/bot/live.json", body if isinstance(body, str) else json.dumps(body))
                self.attention(f"2026-10-15T11:{n:02d}:00")
                ds = self.run_bot()
                self.assertEqual(self.recs(ds), [])
                self.assertIn(("escalate_orchestrator", "FAILCLOSED", "live.json"), self.acts(ds))
                self.assertFalse(self.ack().exists())
                self.clock.t += 3 * 3600  # a new episode for the next bad file

    def test_never_live_actions_cannot_be_enabled(self):
        self.live(ack=True, write_key1=True, queue_fix_slice=True)
        self.attention("2026-10-15T11:30:00")
        ds = self.run_bot()
        self.assertEqual(self.recs(ds), [])
        self.assertIn(("escalate_orchestrator", "FAILCLOSED", "live.json"), self.acts(ds))
        self.assertFalse(self.ack().exists())  # one bad key makes the whole file shadow

    def test_page_salih_never_goes_live(self):
        self.live(hold_dispatch=False, page_salih=True)
        self.events({"ts": "2026-10-15T11:00:00", "event": "error", "lane": "L1",
                     "message": "API Error: 529 overloaded"})
        ds = self.run_bot()
        self.assertIn(("escalate_orchestrator", "LIVE", "live.json"), self.shadow_acts(ds))
        self.assertEqual(self.recs(ds), [])
        self.assertFalse((self.root / "build" / "bot" / "page.json").exists())
        self.assertFalse(self.pause().exists())

    def test_loop_guard_demotes_ack_to_shadow_until_reset(self):
        self.live(ack=True)
        for now, since in (("12:00", "11:30"), ("12:40", "12:05"), ("13:20", "12:45")):
            self.clock.set(f"2026-10-15T{now}:00")
            self.attention(f"2026-10-15T{since}:00")
            ds = self.run_bot()
        self.assertEqual(self.shadow_acts(ds), [("escalate_orchestrator", "LOOP", "L2")])
        [r] = self.recs(ds)
        self.assertEqual((r["did"], r["rule"], r["target"], r["after"]), ("demote", "LOOP", "ack", "shadow"))
        self.assertEqual(r["undo"], "delete live.demoted.ack in build/bot/shadow.json")
        self.assertIn("ack", self.shadow()["live"]["demoted"])
        self.assertEqual(self.ack().stat().st_mtime, ts("2026-10-15T12:40:00"))  # the third ack never ran
        self.clock.set("2026-10-15T13:30:00")
        self.attention("2026-10-15T13:00:00")
        ds = self.run_bot()
        self.assertEqual(self.recs(ds), [])  # still demoted although live.json says ack
        self.assertEqual(self.ack().stat().st_mtime, ts("2026-10-15T12:40:00"))

    def test_pure_shadow_keeps_no_live_state(self):
        self.attention("2026-10-15T11:30:00")
        self.events(self.QUOTA)
        self.assertEqual(self.recs(self.run_bot()), [])
        self.assertNotIn("live", self.shadow())


if __name__ == "__main__":
    unittest.main()
