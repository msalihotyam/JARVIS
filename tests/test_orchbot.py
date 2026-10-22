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
        self.assertEqual(self.acts(self.run_bot()), [("escalate_orchestrator", "FAILCLOSED", "orchbot")])


class ShadowWriteTest(BotCase):
    def test_permission_error_retried_5x_then_one_escalation(self):
        self.events({"ts": "2026-10-15T11:00:00", "event": "pace", "usage_pct": 99})
        # facts.md is out of this test (FactsTest covers its own write failure)
        with mock.patch.object(orchbot.os, "replace", side_effect=PermissionError("held")) as rep, \
                mock.patch.object(orchbot.Bot, "write_facts"):
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
                         ("key1:l1", "write_key1", "write_key1", "orchestrator"))
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
        self.assertEqual((v["key"], v["bot"], v["orchestrator"]["did"]), ("key1:l2", None, "write_key1"))
        ag = self.shadow()["agreement"]
        self.assertEqual(ag["key1"], {"agree": 0, "total": 2})
        self.assertEqual([r["verdict"] for r in ag["recent"]], ["unmatched", "missed"])

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
        for want in [f"- local: {SHA[:10]} (status.json)", f"- origin: {SHA2[:10]} (status.json)",
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
        cases = {"build/conveyor/status.json": ["- local: unknown (status.json)", "- alive: unknown (status.json)",
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


if __name__ == "__main__":
    unittest.main()
