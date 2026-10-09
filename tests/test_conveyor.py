"""Konveyor v1 (scripts/conveyor.py) with a fake claude runner; no network, no model calls."""
import contextlib
import importlib.util
import json
import os
import sys
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
        self.key = ""  # reader off by default; never the real key lookup (env/registry)
        self.posts = []
        self.reply = (500, None)
        self.c = Conveyor(self.root, self.fake, now=lambda: self.t,
                          send=lambda text: self.pushes.append(text) or "sent",
                          post=self.fake_post, key=lambda: self.key)
        self.base = self.root / "build" / "conveyor"

    def fake_post(self, key, body):
        self.posts.append(body)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

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

    def attn(self):
        return [a["id"] for a in self.status()["attention"]]


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
        self.assertNotIn(conveyor.DRY_ID, self.attn())

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
                                  "usage_hold", "lanes", "backlog", "attention"})
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
        self.assertNotIn(conveyor.DRY_ID, self.attn())
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
        self.assertNotIn("OLD", self.attn())


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
        self.assertIn({"id": "A", "reason": "STATUS STUCK", "since": conveyor.iso(self.t)},
                      self.status()["attention"])
        self.assertFalse(any("STUCK" in p for p in self.pushes))  # to the orchestrator first
        self.assertEqual(self.fake.bg_names(), ["A", "A", "A"])
        self.t += conveyor.ATTENTION_PAGE_S
        self.c.pass_once()
        self.assertTrue(any("A: STATUS STUCK" in p for p in self.pushes))  # not acked: backstop page

    def test_stuck_lane_read_with_fixed_note(self):
        self.key = "k"
        self.reply = (200, {"content": [{"type": "text", "text": json.dumps(
            {"kind": "stalled", "summary": "ilerlemiyor", "next": "bak"})}], "usage": {}})
        self.setup_respawned()
        for _ in range(2):
            self.t += 601
            self.c.pass_once()
            new = self.fake.agents[-1]
            del new["pid"]
            self.c.pass_once()
            new.update(pid=9, status="idle")
        self.t += 601
        self.c.pass_once()
        (body,) = self.posts
        self.assertEqual(body["messages"][0]["content"], conveyor.STUCK_NOTE)
        self.assertEqual(self.lanes()["A"]["status"], "STUCK")
        self.assertEqual(self.attn(), ["A", conveyor.DRY_ID])
        self.assertEqual(self.status()["attention"][0]["reason"], "STATUS STUCK; reader stalled: ilerlemiyor")

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
    def test_bad_status_to_attention_then_backstop_with_rate_limit(self):
        t0 = self.t
        self.backlog({"id": "A"}, {"id": "B"}, {"id": "C"})
        self.c.pass_once()
        self.report("A", "BLOCKED")
        self.c.pass_once()
        self.assertEqual(self.pushes, [])  # the orchestrator first, not Salih
        self.assertEqual(self.status()["attention"],
                         [{"id": "A", "reason": "STATUS BLOCKED", "since": conveyor.iso(t0)}])
        self.t += 60
        self.report("B", "FAIL")
        self.report("C", "PASS")
        self.c.pass_once()
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.attn(), ["A", "B", conveyor.DRY_ID])
        self.t = t0 + conveyor.ATTENTION_PAGE_S  # A unacked 30 min: backstop page
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("A: STATUS BLOCKED", self.pushes[0])
        self.t += 60                             # B and dry due, inside the 10 min gap: held
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)
        self.t += 600
        self.c.pass_once()
        self.assertIn("B: STATUS FAIL\n", self.pushes[1])  # held pages, coalesced
        self.assertIn("boş", self.pushes[1])               # with "dry"
        self.assertNotIn("C: STATUS", " ".join(self.pushes))
        self.t += 3600
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 2)  # one backstop page per item

    def test_dry_to_attention_backstop_once(self):
        self.c.pass_once()
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.status()["attention"], [{"id": conveyor.DRY_ID, "since": conveyor.iso(self.t),
                                                       "reason": "kuyruk boş, çalışan lane yok"}])
        self.t += 3600
        self.c.pass_once()
        self.t += 3600
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("boş", self.pushes[0])

    def test_acked_item_leaves_attention_and_is_not_paged(self):
        self.c.pass_once()  # dry
        ack = self.base / "ack"
        ack.mkdir()
        (ack / conveyor.DRY_ID).write_text("")
        os.utime(ack / conveyor.DRY_ID, (self.t - 5, self.t - 5))  # older than the item: no ack
        self.c.pass_once()
        self.assertEqual(self.attn(), [conveyor.DRY_ID])
        os.utime(ack / conveyor.DRY_ID, (self.t + 5, self.t + 5))
        self.t += 3600
        self.c.pass_once()
        self.assertEqual(self.status()["attention"], [])
        self.assertEqual(self.pushes, [])

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

    def test_earlier_version_state_does_not_page(self):
        # v1 "dry": true (already paged) and a lane read by 137095c (no finished_ts, needs_salih paged)
        self.c.pager_flag("dry", True)
        (self.base / "lanes.json").write_text(json.dumps({"A": {
            "id": "0000aaaa", "model": "opus", "effort": "high", "base": "072d0fe",
            "started_at": conveyor.iso(self.t - 7200), "started_ts": self.t - 7200, "state": "done",
            "finished": True, "status": "BLOCKED", "prompt": "prompts/A.md",
            "reading": {"kind": "needs_salih", "summary": "s", "next": "n", "at": conveyor.iso(self.t - 7000)}}}))
        (self.base / "PAUSE").write_text("")
        self.c.pass_once()
        self.assertEqual(self.status()["attention"], [])
        (self.base / "PAUSE").unlink()
        self.c.pass_once()  # dispatch() gives the episode a time
        self.t += conveyor.ATTENTION_PAGE_S - 1
        self.c.pass_once()
        self.assertEqual(self.attn(), [conveyor.DRY_ID])
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

    def test_bad_report_after_state_done_still_in_attention(self):
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.fake.agent("A")["state"] = "done"
        self.c.pass_once()
        self.report("A", "STOP")
        self.c.pass_once()
        self.assertEqual(self.lanes()["A"]["status"], "STOP")
        self.assertIn("A", self.attn())
        self.assertEqual(self.pushes, [])
        self.t += conveyor.ATTENTION_PAGE_S  # unacked: the backstop page
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


KEY = "sk-ant-SECRET-reader-key-42"


def answer(obj, tin=1000, tout=50):
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return 200, {"content": [{"type": "text", "text": text}],
                 "usage": {"input_tokens": tin, "output_tokens": tout}}


class TestReader(Base):
    def setUp(self):
        super().setUp()
        self.key = KEY
        self.reply = answer({"kind": "pass", "summary": "testler yeşil", "next": "entegre et"})

    def report(self, name, status, where="wt", mtime=None, extra=""):
        # self.t moves ahead of the wall clock here: date reports after the lane start
        return super().report(name, status, where, self.t + 1 if mtime is None else mtime, extra)

    def finish_a(self, status="PASS", extra=""):
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        rep = self.report("A", status, extra=extra)
        self.c.pass_once()
        return rep

    def lane_a(self):
        return next(x for x in self.status()["lanes"] if x["name"] == "A")

    def all_files_text(self):
        return {p: p.read_bytes().decode("utf-8", "replace") for p in self.root.rglob("*") if p.is_file()}

    def test_valid_reading_recorded(self):
        rep = self.finish_a()
        (body,) = self.posts
        self.assertEqual((body["model"], body["max_tokens"], body["temperature"], body["output_config"]),
                         ("claude-haiku-5-5", 300, 0, {"effort": "low"}))
        self.assertEqual(body["system"], conveyor.READER_SYSTEM)
        self.assertEqual(body["messages"], [{"role": "user", "content": rep.read_text(encoding="utf-8")}])
        r = self.lane_a()["reading"]
        self.assertEqual((r["kind"], r["summary"], r["next"]), ("pass", "testler yeşil", "entegre et"))
        self.assertTrue(r["at"])
        ev = [e for e in self.events() if e["event"] == "reader"]
        self.assertEqual([(e["lane"], e["reading"]) for e in ev],
                         [("A", {"kind": "pass", "summary": "testler yeşil", "next": "entegre et"})])
        self.assertEqual(self.attn(), [conveyor.DRY_ID])  # a pass reading needs no attention
        (row,) = [json.loads(x) for x in (self.root / "build" / "ledger" / "api.jsonl").read_text().splitlines()]
        self.assertEqual({k: row[k] for k in ("model", "lane", "input_tokens", "output_tokens")},
                         {"model": "claude-haiku-5-5", "lane": "A",
                          "input_tokens": 1000, "output_tokens": 50})
        self.assertAlmostEqual(row["est_usd"], 0.000125)  # $0.10 / $0.50 per MTok
        self.c.pass_once()
        self.assertEqual(len(self.posts), 1)  # one reading per finish

    def test_request_has_no_prefill_and_low_effort(self):
        self.finish_a()
        (body,) = self.posts
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertEqual([m["role"] for m in body["messages"]], ["user"])
        self.assertNotEqual(body["messages"][-1]["role"], "assistant")  # Haiku 5.5: prefill is a 400

    def test_invalid_output_needs_orchestrator(self):
        self.reply = answer("```json\n{\"kind\": \"pass\"}\n```")
        self.finish_a()
        r = self.lane_a()["reading"]
        self.assertEqual((r["kind"], r["summary"]), ("needs_orchestrator", "reader: invalid output"))
        self.assertEqual(self.attn(), ["A", conveyor.DRY_ID])
        self.assertEqual(self.lanes()["A"]["status"], "PASS")

    def test_parse_reading_is_strict(self):
        ok = {"kind": "blocked", "summary": "s", "next": "n"}
        self.assertEqual(conveyor.parse_reading(answer(ok)[1]), ok)
        for bad in [{**ok, "kind": "approve_push"}, {**ok, "extra": 1}, {"kind": "pass", "summary": "s"},
                    {**ok, "summary": "x" * 141}, {**ok, "next": ""}, {**ok, "summary": 5}, [ok], "nope"]:
            self.assertIsNone(conveyor.parse_reading(answer(bad)[1]), bad)
        for resp in [None, {}, {"content": "x"}, {"content": [{"type": "text"}]}]:
            self.assertIsNone(conveyor.parse_reading(resp), resp)

    def test_http_error_and_exception_fail_closed(self):
        self.reply = (529, None)
        self.finish_a()
        self.assertIsNone(self.lane_a()["reading"])
        self.assertEqual(self.lanes()["A"]["status"], "PASS")
        err = [e for e in self.events() if e["event"] == "reader_error"]
        self.assertEqual([(e["lane"], e["status"]) for e in err], [("A", 529)])
        self.assertFalse((self.root / "build" / "ledger" / "api.jsonl").exists())
        self.reply = TimeoutError("boom")
        self.drop("b.md", HDR.format("B"))
        self.c.pass_once()
        self.report("B", "PASS")
        self.assertTrue(self.c.pass_once())
        self.assertEqual([e["status"] for e in self.events() if e["event"] == "reader_error"],
                         [529, "TimeoutError"])
        self.assertEqual(self.lanes()["B"]["status"], "PASS")

    def test_anthropic_post_request_and_errors(self):
        with mock.patch.object(conveyor.urllib.request, "urlopen", side_effect=TimeoutError()) as op:
            self.assertEqual(conveyor.anthropic_post(KEY, {"x": 1}), ("TimeoutError", None))
        req = op.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_header("X-api-key"), KEY)
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(op.call_args[1]["timeout"], 20)
        self.assertEqual(json.loads(req.data), {"x": 1})
        http = conveyor.urllib.error.HTTPError(conveyor.READER_URL, 401, "no", {}, None)
        with mock.patch.object(conveyor.urllib.request, "urlopen", side_effect=http):
            self.assertEqual(conveyor.anthropic_post(KEY, {}), (401, None))

    def test_no_key_disabled_once_per_day(self):
        self.key = ""
        self.finish_a()
        self.drop("b.md", HDR.format("B"))
        self.c.pass_once()
        self.report("B", "PASS")
        self.c.pass_once()
        self.assertEqual(self.posts, [])
        self.assertEqual([e["event"] for e in self.events()].count("reader_disabled"), 1)
        self.t += 86400
        self.drop("c.md", HDR.format("C"))
        self.c.pass_once()
        self.report("C", "PASS")
        self.c.pass_once()
        self.assertEqual([e["event"] for e in self.events()].count("reader_disabled"), 2)
        self.assertIsNone(self.lane_a()["reading"])

    def test_budget_cap_stops_calls(self):
        led = self.root / "build" / "ledger"
        led.mkdir(parents=True)
        month, last = conveyor.iso(self.t)[:7], conveyor.iso(self.t - 40 * 86400)
        rows = [{"ts": f"{month}-01T00:00:00", "est_usd": 12}, {"ts": last, "est_usd": 100},
                {"ts": f"{month}-02T00:00:00", "est_usd": 7.99}]
        (led / "api.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "junk\n")
        self.finish_a()
        self.assertEqual(len(self.posts), 1)  # 19.99 this month: still under the cap
        with open(led / "api.jsonl", "a") as f:
            f.write(json.dumps({"ts": f"{month}-03T00:00:00", "est_usd": 0.01}) + "\n")
        self.drop("b.md", HDR.format("B"))
        self.c.pass_once()
        self.report("B", "PASS")
        self.c.pass_once()
        self.assertEqual(len(self.posts), 1)
        err = [e for e in self.events() if e["event"] == "reader_error"]
        self.assertEqual([(e["lane"], e["status"]) for e in err], [("B", "budget")])

    def test_report_truncated_head_and_tail(self):
        self.finish_a(extra="H" * 20000 + "T" * 20000 + "\n")
        content = self.posts[0]["messages"][0]["content"]
        self.assertLessEqual(len(content), conveyor.READER_MAX_CHARS + 40)
        self.assertTrue(content.startswith("```\nSTATUS: PASS\nHHH"))
        self.assertTrue(content.endswith("TTT\n```\n"))

    def test_key_never_written(self):
        self.reply = answer({"kind": "needs_salih", "summary": "onay gerek", "next": "Salih onaylasın"})
        self.finish_a()
        for i, reply in enumerate([(401, None), RuntimeError("bad key " + KEY), answer("garbage")]):
            self.reply = reply
            n = "BCD"[i]
            self.drop(f"{n}.md", HDR.format(n))
            self.c.pass_once()
            self.report(n, "BLOCKED")
            self.t += 700
            self.c.pass_once()
        self.assertEqual(len(self.posts), 4)
        for body in self.posts:
            self.assertNotIn(KEY, json.dumps(body))
        files = self.all_files_text()
        self.assertIn(self.base / "events.jsonl", files)
        self.assertIn(self.root / "build" / "ledger" / "api.jsonl", files)
        for p, text in files.items():
            self.assertNotIn(KEY, text, p)
        self.assertNotIn(KEY, " ".join(self.pushes))

    def test_needs_salih_pages_others_do_not(self):
        self.reply = answer({"kind": "blocked", "summary": "engel_bir", "next": "n"})
        self.finish_a()
        self.reply = answer({"kind": "needs_orchestrator", "summary": "engel_iki", "next": "n"})
        self.drop("b.md", HDR.format("B"))
        self.c.pass_once()
        self.report("B", "PASS")
        self.t += 700
        self.c.pass_once()
        self.reply = answer({"kind": "needs_salih", "summary": "Salih karar vermeli", "next": "n"})
        self.drop("c.md", HDR.format("C"))
        self.c.pass_once()
        self.report("C", "PASS")
        self.t += 700
        self.c.pass_once()
        pushed = "\n".join(self.pushes)
        self.assertIn("C: Salih karar vermeli", pushed)
        self.assertNotIn("engel_bir", pushed)
        self.assertNotIn("engel_iki", pushed)
        self.assertEqual(self.attn(), ["A", "B", "C", conveyor.DRY_ID])

    def test_needs_salih_not_paged_again_by_backstop(self):
        self.reply = answer({"kind": "needs_salih", "summary": "karar", "next": "n"})
        self.finish_a()
        self.t += conveyor.ATTENTION_PAGE_S + 3600
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 2)
        self.assertEqual(self.pushes[0], "A: karar")
        self.assertIn(conveyor.DRY_ID, self.pushes[1])  # only the dry backstop
        self.assertNotIn("A:", self.pushes[1])

    def test_needs_salih_page_respects_gap(self):
        self.c.page("earlier")
        self.c.flush_pages()
        self.reply = answer({"kind": "needs_salih", "summary": "karar", "next": "n"})
        self.finish_a()
        self.assertEqual(self.pushes, ["earlier"])  # held inside PAGE_GAP_S
        self.t += conveyor.PAGE_GAP_S
        self.c.pass_once()
        self.assertIn("A: karar", self.pushes[-1])

    def test_reading_never_changes_status_or_deps(self):
        self.reply = answer({"kind": "pass", "summary": "iyi", "next": "n"})
        self.finish_a(status="BLOCKED")
        self.assertEqual(self.lanes()["A"]["status"], "BLOCKED")
        self.assertEqual(self.status()["attention"][0]["reason"], "STATUS BLOCKED")  # pass reading: status stays
        self.assertNotIn("A: STATUS BLOCKED", "\n".join(self.pushes))
        self.reply = answer({"kind": "needs_salih", "summary": "dur", "next": "n"})
        self.backlog({"id": "X"}, {"id": "Y", "depends_on": ["X"]})
        self.c.pass_once()
        self.report("X", "PASS")
        self.c.pass_once()
        self.assertEqual(self.lanes()["X"]["status"], "PASS")
        self.assertEqual(self.fake.bg_names(), ["A", "X", "Y"])  # reading did not hold Y

    def test_ack_clears_attention_only_for_this_run(self):
        ack = self.base / "ack"
        ack.mkdir()
        (ack / "A").write_text("")
        os.utime(ack / "A", (self.t - 3600, self.t - 3600))  # an ack from an earlier A
        self.reply = answer({"kind": "blocked", "summary": "s", "next": "n"})
        self.finish_a()
        self.assertEqual(self.attn(), ["A", conveyor.DRY_ID])
        os.utime(ack / "A", (self.t + 5, self.t + 5))
        self.c.pass_once()
        self.assertEqual(self.attn(), [conveyor.DRY_ID])
        self.assertTrue((ack / "A").exists())

    def test_reader_key_env_then_registry(self):
        with mock.patch.dict(os.environ, {"JARVIS_READER_API_KEY": " k1 "}):
            self.assertEqual(conveyor.reader_key(), "k1")

        class Reg:
            HKEY_CURRENT_USER = 1

            @staticmethod
            def OpenKey(root, path):
                assert (root, path) == (1, "Environment")
                return contextlib.nullcontext("h")

            @staticmethod
            def QueryValueEx(h, name):
                assert name == "JARVIS_READER_API_KEY"
                return "k2", 1

        env = {k: v for k, v in os.environ.items() if k != "JARVIS_READER_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(conveyor.sys, "platform", "win32"), \
                mock.patch.dict(sys.modules, {"winreg": Reg}):
            self.assertEqual(conveyor.reader_key(), "k2")
            Reg.QueryValueEx = staticmethod(mock.Mock(side_effect=FileNotFoundError()))
            self.assertEqual(conveyor.reader_key(), "")


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
