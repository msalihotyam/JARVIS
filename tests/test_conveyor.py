"""Konveyor v1 (scripts/conveyor.py) with a fake claude runner; no network, no model calls."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "conveyor", Path(__file__).resolve().parents[1] / "scripts" / "conveyor.py")
conveyor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(conveyor)
Conveyor, parse_header, files_overlap = conveyor.Conveyor, conveyor.parse_header, conveyor.files_overlap

HDR = "@jarvis lane={} model=opus effort=high base=072d0fe\n"


class FakeClaude:
    """Fake claude runner: tracks bg sessions, answers `agents --json`."""

    def __init__(self):
        self.agents = []
        self.calls = []
        self.n = 0
        self.fail_agents = False

    def __call__(self, args, timeout=120):
        self.calls.append(args)
        if args[0] == "agents":
            return (1, "boom") if self.fail_agents else (0, json.dumps(self.agents))
        if args[0] == "--bg":
            self.n += 1
            sid = f"{self.n:08x}"
            self.agents.append({"id": sid, "pid": 100 + self.n, "kind": "background", "status": "busy",
                                "name": args[args.index("--name") + 1], "state": "working"})
            return 0, f"backgrounded · {sid} · x\n"
        if args[0] == "respawn":
            return 0, "respawned"
        if args[0] == "stop":
            next(a for a in self.agents if a["id"] == args[1]).pop("pid", None)
            return 0, "stopped"
        return 1, "unknown"

    def bg_calls(self):
        return [c for c in self.calls if c[0] == "--bg"]

    def bg_names(self):
        return [c[2] for c in self.bg_calls()]

    def agent(self, name):
        return next(a for a in reversed(self.agents) if a.get("name") == name)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = FakeClaude()
        self.t = float(int(time.time()))
        self.pushes = []
        self.c = Conveyor(self.root, self.fake, now=lambda: self.t,
                          send=lambda text: self.pushes.append(text) or "sent")
        self.base = self.root / "build" / "conveyor"

    def tearDown(self):
        self.tmp.cleanup()

    def drop(self, fname, text, age=0):
        p = self.base / "inbox" / fname
        p.write_text(text, encoding="utf-8")
        os.utime(p, (self.t - 1000 + age, self.t - 1000 + age))
        return p

    def events(self):
        f = self.base / "events.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def status(self):
        return json.loads((self.base / "status.json").read_text(encoding="utf-8"))

    def lanes(self):
        return json.loads((self.base / "lanes.json").read_text())

    def report(self, name, status, where="wt", mtime=None, extra=""):
        d = self.root / ".claude" / "worktrees" / where / "build" / "reports" if where else \
            self.root / "build" / "reports"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.md"
        p.write_text(f"```\nSTATUS: {status}\n{extra}```\n", encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def backlog(self, *slices):
        (self.base / "prompts").mkdir(exist_ok=True)
        out = []
        for s in slices:
            s = {"title": s["id"], "model": "sonnet", "effort": "medium", "tier": "T1",
                 "files": [], "depends_on": [], "prompt": f"build/conveyor/prompts/{s['id']}.md", **s}
            p = self.root / s["prompt"]
            if not p.exists():
                p.write_text(f"TASK {s['id']}\nbody\n", encoding="utf-8")
            out.append(s)
        (self.base / "backlog.json").write_text(json.dumps({"slices": out}), encoding="utf-8")

    def view(self):
        return {r["id"]: r["state"] for r in self.status()["backlog"] if "id" in r}


class TestHeader(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_header("@jarvis lane=THIZ model=opus effort=high base=072d0fe"),
                         {"lane": "THIZ", "model": "opus", "effort": "high", "base": "072d0fe"})
        self.assertEqual(parse_header("@jarvis lane=A model=opus effort=high base=main")["base"], "main")

    def test_bom_and_crlf(self):
        self.assertIsNotNone(parse_header("﻿@jarvis lane=A-1 model=sonnet effort=max base=abcdef1\r\n"))

    def test_invalid(self):
        for bad in ["", "TASK X", "@jarvis lane=A model=opus effort=huge base=072d0fe",
                    "@jarvis lane=A model=opus effort=high", "@jarvis lane=A B model=opus effort=high base=072d0fe",
                    "@jarvis lane=A model=opus effort=high base=xyz"]:
            self.assertIsNone(parse_header(bad), bad)


class TestOverlap(unittest.TestCase):
    def test_overlap(self):
        self.assertTrue(files_overlap(["jarvis/ui/**"], ["jarvis/ui/qml/X.qml"]))
        self.assertTrue(files_overlap(["scripts/conveyor.py"], ["scripts\\*.py"]))
        self.assertTrue(files_overlap(["tests/test_a*.py"], ["tests/*_ui.py"]))  # conservative
        self.assertFalse(files_overlap(["scripts/conveyor.py"], ["tests/test_conveyor.py"]))
        self.assertFalse(files_overlap(["jarvis/a.py"], ["jarvis/b.py"]))
        self.assertFalse(files_overlap([], ["jarvis/**"]))


class TestInbox(Base):
    def test_dispatch_moves_and_records(self):
        self.drop("a.md", HDR.format("A") + "do things")
        self.c.pass_once()
        (call,) = self.fake.bg_calls()
        self.assertEqual(call[:9], ["--bg", "--name", "A", "--model", "opus", "--effort", "high",
                                    "--settings", conveyor.SETTINGS])
        self.assertEqual(call[9], HDR.format("A") + "do things")
        self.assertEqual(list((self.base / "inbox").glob("*.md")), [])
        self.assertEqual(len(list((self.base / "inbox" / "started").glob("*-A.md"))), 1)
        self.assertEqual([e["event"] for e in self.events()], ["dispatch"])

    def test_reject_path(self):
        self.drop("bad.md", "no header here\n")
        self.c.pass_once()
        rej = self.base / "inbox" / "rejected"
        self.assertTrue((rej / "bad.md").exists())
        self.assertIn("header", (rej / "bad.md.reason.txt").read_text())
        self.assertEqual(self.fake.bg_calls(), [])

    def test_pause_flag(self):
        (self.base / "PAUSE").write_text("")
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        self.assertTrue((self.base / "inbox" / "a.md").exists())
        self.assertTrue(self.status()["paused"])
        self.assertEqual(self.pushes, [])  # paused is not "dry"

    def test_max_three_lanes_oldest_first(self):
        for i, n in enumerate("EDCBA"):
            self.drop(f"{n}.md", HDR.format(n), age=i)
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["E", "D", "C"])
        self.c.pass_once()
        self.assertEqual(len(self.fake.bg_calls()), 3)
        self.fake.agents[0]["state"] = "done"
        del self.fake.agents[0]["pid"]
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["E", "D", "C", "B"])

    def test_same_name_guard(self):
        self.fake.agents.append({"id": "deadbeef", "pid": 1, "name": "A", "state": "working", "status": "busy"})
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        self.assertTrue((self.base / "inbox" / "a.md").exists())

    def test_idle_or_finished_session_does_not_block_same_name(self):
        self.fake.agents.append({"id": "deadbeef", "pid": 1, "name": "A", "state": "working", "status": "idle"})
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        # B reported, its session lingers busy: a new B still goes
        self.drop("b.md", HDR.format("B"))
        self.c.pass_once()
        self.report("B", "PASS")
        self.c.pass_once()
        self.drop("b2.md", HDR.format("B"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B", "B"])

    def test_finish_and_respawn(self):
        self.drop("a.md", HDR.format("A"))
        self.drop("b.md", HDR.format("B"), age=1)
        self.c.pass_once()
        a, b = self.fake.agents
        del a["pid"]                       # A interrupted (reboot) mid-work
        b["state"] = "done"
        del b["pid"]                       # B finished
        self.c.pass_once()
        self.c.pass_once()
        self.assertEqual([c for c in self.fake.calls if c[0] == "respawn"], [["respawn", a["id"]]])
        kinds = [e["event"] for e in self.events()]
        self.assertEqual(kinds.count("finish"), 1)
        self.assertEqual(kinds.count("respawn"), 1)

    def test_status_shape(self):
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        s = self.status()
        self.assertEqual(set(s), {"updated_at", "pid", "paused", "last_error", "usage_pct",
                                  "usage_hold", "lanes", "backlog"})
        self.assertFalse(s["paused"])
        self.assertIsNone(s["last_error"])
        (lane,) = s["lanes"]
        self.assertEqual({k: lane[k] for k in ("name", "model", "effort", "state")},
                         {"name": "A", "model": "opus", "effort": "high", "state": "working"})
        self.assertTrue(lane["started_at"] and lane["updated_at"])

    def test_agents_error_is_logged_and_survives(self):
        self.fake.fail_agents = True
        self.assertFalse(self.c.pass_once())
        self.assertIn("agents", self.status()["last_error"])
        self.assertEqual(self.events()[-1]["event"], "error")


class TestBacklog(Base):
    def test_order_deps_and_generated_header(self):
        self.backlog({"id": "A"}, {"id": "B", "depends_on": ["A"]}, {"id": "C"})
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "C"])
        call = self.fake.bg_calls()[0]
        self.assertEqual(call[4], "sonnet")
        self.assertEqual(call[9], "@jarvis lane=A model=sonnet effort=medium base=main\nTASK A\nbody\n")
        self.assertEqual(self.view()["B"], "waiting_deps")
        self.report("A", "PASS")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "C", "B"])
        self.assertEqual(self.lanes()["A"]["status"], "PASS")

    def test_prompt_header_replaced_by_backlog_values(self):
        self.backlog({"id": "A", "model": "opus", "effort": "high"})
        (self.base / "prompts" / "A.md").write_text(
            "@jarvis lane=X model=haiku effort=low base=abc1234\nTASK\n", encoding="utf-8")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls()[0][9], "@jarvis lane=A model=opus effort=high base=abc1234\nTASK\n")

    def test_failed_dependency_is_not_done(self):
        self.backlog({"id": "A"}, {"id": "B", "depends_on": ["A"]})
        self.c.pass_once()
        self.report("A", "BLOCKED")
        self.c.pass_once()
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.assertEqual(self.view()["B"], "waiting_deps")

    def test_overlap_waits(self):
        self.backlog({"id": "A", "files": ["jarvis/ui/**"]},
                     {"id": "B", "files": ["jarvis/ui/qml/X.qml"]},
                     {"id": "C", "files": ["scripts/x.py"]})
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "C"])
        self.assertEqual(self.view()["B"], "overlap")
        self.report("A", "PASS")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "C", "B"])

    def test_t2_gate(self):
        self.backlog({"id": "A", "tier": "T2"}, {"id": "B", "tier": "T2", "approved": True})
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["B"])
        self.assertEqual(self.view()["A"], "needs_t2_approval")

    def test_inbox_goes_first_and_blocks_backlog(self):
        self.backlog({"id": "S"})
        self.fake.agents.append({"id": "deadbeef", "pid": 1, "name": "A", "state": "working", "status": "busy"})
        self.drop("a.md", HDR.format("A"))  # waits on the same-name guard
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), [])
        self.assertEqual(self.pushes, [])  # a waiting inbox file is not "dry"
        self.fake.agents[0]["status"] = "idle"
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "S"])

    def test_already_reported_slice_is_skipped(self):
        self.report("A", "PASS", where=None)
        self.backlog({"id": "A"}, {"id": "B", "depends_on": ["A"]})
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["B"])

    def test_invalid_slices_reported(self):
        self.backlog({"id": "A", "tier": "T9"}, {"id": "B"})
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["B"])
        self.assertTrue(any("bad tier" in r.get("error", "") for r in self.status()["backlog"]))
        (self.base / "backlog.json").write_text("{nope", encoding="utf-8")
        self.c.pass_once()
        self.assertTrue(any("backlog.json" in r.get("error", "") for r in self.status()["backlog"]))


class TestFinishedByReport(Base):
    def start3(self):
        self.backlog({"id": "A"}, {"id": "B"}, {"id": "C"}, {"id": "D"})
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B", "C"])

    def test_report_frees_slot_even_if_session_blocked(self):
        self.start3()
        self.fake.agent("A").update(state="blocked", status="idle")
        self.report("A", "PASS")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B", "C", "D"])
        self.assertTrue(self.lanes()["A"]["finished"])

    def test_finished_lane_never_respawned(self):
        self.start3()
        self.report("A", "PASS")
        self.c.pass_once()
        del self.fake.agent("A")["pid"]  # reboot: the finished session is gone
        self.c.pass_once()
        self.c.pass_once()
        self.assertEqual([c for c in self.fake.calls if c[0] == "respawn"], [])

    def test_unreported_lane_respawned_once(self):
        self.start3()
        del self.fake.agent("B")["pid"]
        self.c.pass_once()
        self.c.pass_once()
        self.assertEqual([c for c in self.fake.calls if c[0] == "respawn"], [["respawn", self.fake.agent("B")["id"]]])

    def test_stale_report_ignored(self):
        self.report("A", "PASS", mtime=self.t - 3600)  # an earlier lane named A
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.c.pass_once()
        self.assertFalse(self.lanes()["A"].get("finished"))

    def test_report_copied_to_canonical_keeping_old(self):
        old = self.report("A", "PASS", where=None, mtime=self.t - 3600, extra="old\n")
        self.backlog({"id": "B"})
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        src = self.report("A", "BLOCKED", extra="new\n")
        self.c.pass_once()
        self.assertEqual(old.read_text(encoding="utf-8"), src.read_text(encoding="utf-8"))
        kept = [p for p in old.parent.glob("A.*.md")]
        self.assertEqual(len(kept), 1)
        self.assertIn("old", kept[0].read_text(encoding="utf-8"))
        self.assertEqual(self.lanes()["A"]["report"], str(old))
        self.c.pass_once()  # no second copy
        self.assertEqual(len(list(old.parent.glob("A.*.md"))), 1)

    def test_v0_finished_lane_gets_status_without_page(self):
        (self.base / "lanes.json").write_text(json.dumps({"OLD": {
            "id": "0000aaaa", "model": "opus", "effort": "high", "base": "072d0fe",
            "started_at": conveyor.iso(self.t - 60), "state": "stopped", "respawned": True,
            "finished": True, "prompt": "prompts/OLD.md"}}))
        self.report("OLD", "BLOCKED")
        self.c.pass_once()
        self.assertEqual(self.lanes()["OLD"]["status"], "BLOCKED")
        self.assertFalse(any("OLD" in p for p in self.pushes))


class TestRedispatch(Base):
    def setup_respawned(self, cwd_files_age=None):
        self.drop("a.md", HDR.format("A") + "do things\n")
        self.c.pass_once()
        a = self.fake.agent("A")
        wt = self.root / ".claude" / "worktrees" / "a"
        wt.mkdir(parents=True)
        f = wt / "x.py"
        f.write_text("x")
        os.utime(f, (self.t - 5000, self.t - 5000))
        a["cwd"] = str(wt)
        del a["pid"]
        self.c.pass_once()                 # respawn
        a.update(pid=7, status="idle")     # back, but did not resume its turn
        return a, wt

    def test_idle_respawned_lane_redispatched_from_prompt(self):
        a, _ = self.setup_respawned()
        self.t += 599
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.t += 2
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "A"])
        text = self.fake.bg_calls()[1][9]
        self.assertTrue(text.startswith(HDR.format("A") + conveyor.CONTINUE))
        self.assertTrue(text.endswith("do things\n"))
        ln = self.lanes()["A"]
        self.assertEqual(ln["prev_ids"], [a["id"]])
        self.assertNotEqual(ln["id"], a["id"])
        self.assertIn(["stop", a["id"]], self.fake.calls)  # the silent session is stopped
        self.t += 3600
        self.c.pass_once()  # the new session is busy: nothing more
        self.assertEqual(len(self.fake.bg_calls()), 2)

    def test_second_stop_respawns_again(self):
        a, wt = self.setup_respawned()
        self.c.pass_once()                 # seen alive again
        (wt / "work.py").write_text("x")   # it resumed and worked
        self.t += 3600
        del a["pid"]                       # second reboot
        self.c.pass_once()
        self.assertEqual([c for c in self.fake.calls if c[0] == "respawn"], [["respawn", a["id"]]] * 2)
        self.assertEqual(self.fake.bg_names(), ["A"])

    def test_stuck_after_two_redispatches_frees_slot(self):
        self.setup_respawned()
        for _ in range(2):
            self.t += 601
            self.c.pass_once()             # redispatch
            new = self.fake.agents[-1]
            del new["pid"]                 # it stops again
            self.c.pass_once()             # respawn
            new.update(pid=9, status="idle")
        self.assertEqual(self.fake.bg_names(), ["A", "A", "A"])
        self.t += 601
        self.c.pass_once()
        ln = self.lanes()["A"]
        self.assertEqual((ln["finished"], ln["status"]), (True, "STUCK"))
        self.assertTrue(any("STUCK" in p for p in self.pushes))
        self.assertEqual(self.fake.bg_names(), ["A", "A", "A"])

    def test_pause_holds_redispatch(self):
        self.setup_respawned()
        (self.base / "PAUSE").write_text("")
        self.t += 700
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        (self.base / "PAUSE").unlink()
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "A"])

    def test_activity_prevents_redispatch(self):
        _, wt = self.setup_respawned()
        (wt / "new.txt").write_text("work")
        os.utime(wt / "new.txt", (self.t + 10, self.t + 10))
        self.t += 700
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])

    def test_test_ledger_row_counts_as_activity(self):
        self.setup_respawned()
        led = self.root / "build" / "flakes"
        led.mkdir(parents=True)
        row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.t + 30)), "lane": "A"}
        (led / "ledger.jsonl").write_text(json.dumps(row) + "\n")
        self.t += 700
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])


class TestPager(Base):
    def test_bad_status_paged_with_rate_limit(self):
        self.backlog({"id": "A"}, {"id": "B"}, {"id": "C"})
        self.c.pass_once()
        self.report("A", "BLOCKED")
        self.c.pass_once()
        self.assertEqual(self.pushes, ["A: STATUS BLOCKED"])
        self.t += 60
        self.report("B", "FAIL")
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)   # inside the 10 min gap: held
        self.report("C", "PASS")
        self.t += 600
        self.c.pass_once()
        self.assertTrue(self.pushes[1].startswith("B: STATUS FAIL\n"))  # held page, coalesced
        self.assertIn("boş", self.pushes[1])                            # with "dry"
        self.assertNotIn("C: STATUS", " ".join(self.pushes))

    def test_dry_paged_once(self):
        self.c.pass_once()
        self.t += 3600
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("boş", self.pushes[0])

    def test_three_crashes_in_a_row_page_once(self):
        (self.base / "PAUSE").write_text("")
        self.fake.fail_agents = True
        for _ in range(2):
            self.c.loop(once=True)
            self.t += 60
        self.assertEqual(self.pushes, [])
        self.c.loop(once=True)
        self.t += 600
        self.c.loop(once=True)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("3", self.pushes[0])

    def test_success_resets_crash_count(self):
        (self.base / "PAUSE").write_text("")
        self.fake.fail_agents = True
        self.c.loop(once=True)
        self.c.loop(once=True)
        self.fake.fail_agents = False
        self.c.loop(once=True)
        self.fake.fail_agents = True
        self.c.loop(once=True)
        self.c.loop(once=True)
        self.assertEqual(self.pushes, [])

    def test_ntfy_without_topic_makes_no_call(self):
        with mock.patch.dict(os.environ, {"JARVIS_NTFY_TOPIC": "bad topic/../x"}), \
                mock.patch.object(conveyor.urllib.request, "urlopen") as op:
            self.assertEqual(conveyor.ntfy_send("hi"), "no-topic")
            op.assert_not_called()

    def test_ntfy_topic_not_logged_and_failed_page_retried(self):
        self.c.send = conveyor.ntfy_send
        self.c.page("A: STATUS BLOCKED")
        with mock.patch.dict(os.environ, {"JARVIS_NTFY_TOPIC": "secret_topic_42"}), \
                mock.patch.object(conveyor.urllib.request, "urlopen", side_effect=OSError("x secret_topic_42")):
            self.c.pass_once()
        for f in ("events.jsonl", "status.json", "pager.json"):
            self.assertNotIn("secret_topic_42", (self.base / f).read_text(encoding="utf-8"), f)
        self.assertEqual(self.events()[-1]["result"], "error OSError")
        self.assertIn("A: STATUS BLOCKED", self.c.pager_flag("pending"))

    def test_bad_report_after_state_done_still_paged(self):
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.fake.agent("A")["state"] = "done"
        self.c.pass_once()
        self.report("A", "STOP")
        self.c.pass_once()
        self.assertEqual(self.lanes()["A"]["status"], "STOP")
        self.t += 600  # the "dry" push went first; this one waits out the gap
        self.c.pass_once()
        self.assertIn("A: STATUS STOP", self.pushes[-1])


class TestUsageGuard(Base):
    def ledger(self, pct, age=0):
        d = self.root / "build" / "ledger"
        d.mkdir(parents=True, exist_ok=True)
        end = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.t - age))
        (d / "slices.jsonl").write_text(
            "garbage\n" + json.dumps({"end": end, "seven_day": {"start": 1, "end": pct}}) + "\n")

    def test_over_limit_holds_new_lanes(self):
        self.ledger(85)
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        self.assertTrue(self.status()["usage_hold"])
        self.assertEqual(self.pushes, [])

    def test_old_high_row_still_holds(self):
        self.ledger(95, age=7 * 3600)  # no new rows while held: the reading must not expire in hours
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])

    def test_under_limit_or_stale_dispatches(self):
        self.ledger(84)
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.ledger(99, age=conveyor.USAGE_FRESH_S + 1)
        self.drop("b.md", HDR.format("B"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B"])
        self.assertIsNone(self.status()["usage_pct"])


class TestLock(Base):
    def test_live_lock_blocks(self):
        (self.base / "conveyor.lock").write_text(json.dumps({"pid": -5, "heartbeat": self.t - 60}))
        self.assertFalse(self.c.acquire_lock())

    def test_stale_lock_taken_over(self):
        (self.base / "conveyor.lock").write_text(json.dumps({"pid": -5, "heartbeat": self.t - 301}))
        self.assertTrue(self.c.acquire_lock())
        self.assertTrue(self.c.own_lock())

    def test_corrupt_or_missing_lock(self):
        self.assertTrue(self.c.acquire_lock())
        (self.base / "conveyor.lock").write_text("garbage")
        self.assertTrue(self.c.acquire_lock())


if __name__ == "__main__":
    unittest.main()
