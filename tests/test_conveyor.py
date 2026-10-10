"""Konveyor v1 (scripts/conveyor.py) with a fake claude runner; no network, no model calls."""
import contextlib
import importlib.util
import json
import os
import re
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

HDR = "@jarvis lane={} model=opus effort=high base=1234567\n"  # FakeGit's main (MAIN) starts 1234567


class FakeClaude:
    """Fake claude runner: tracks bg sessions, answers `agents --json`."""

    def __init__(self):
        self.agents = []
        self.calls = []
        self.cwds = []
        self.n = 0
        self.fail_agents = False
        self.rm_rc = 0

    def __call__(self, args, timeout=120, cwd=None):
        self.calls.append(args)
        self.cwds.append(cwd)
        if args[0] == "agents":
            return (1, "boom") if self.fail_agents else (0, json.dumps(self.agents))
        if args[:2] == ["--bg", "--resume"]:  # continues that session under the same id
            a = next(a for a in self.agents if a["sessionId"] == args[2])
            a.update(pid=500 + len(self.calls), state="working", status="busy")
            return 0, f"backgrounded · {a['id']} · x\n"
        if args[0] == "--bg":
            self.n += 1
            sid = f"{self.n:08x}"
            self.agents.append({"id": sid, "pid": 100 + self.n, "kind": "background", "status": "busy",
                                "name": args[args.index("--name") + 1], "state": "working",
                                "sessionId": sid + "-0000-4000-8000-000000000000"})
            return 0, f"backgrounded · {sid} · x\n"
        if args[0] == "respawn":
            return 0, "respawned"
        if args[0] == "stop":
            next(a for a in self.agents if a["id"] == args[1]).pop("pid", None)
            return 0, "stopped"
        if args[0] == "rm":
            if self.rm_rc == 0:
                self.agents = [a for a in self.agents if a["id"] != args[1]]
            return self.rm_rc, "removed" if self.rm_rc == 0 else "rm failed"
        return 1, "unknown"

    def bg_calls(self):
        return [c for c in self.calls if c[0] == "--bg"]

    def bg_names(self):
        return [c[2] for c in self.bg_calls()]

    def agent(self, name):
        return next(a for a in reversed(self.agents) if a.get("name") == name)


class FakeProcs:
    """Fake process API: a snapshot {pid: (ppid, exe)}, creation times, recorded priority calls."""

    def __init__(self):
        self.procs = {}  # pid -> (ppid, exe, created)
        self.set = []  # (pid, class) calls
        self.fail = set()  # pids whose set_priority raises
        self.tree_error = None

    def tree(self):
        if self.tree_error:
            raise self.tree_error
        return {p: (pp, exe) for p, (pp, exe, _) in self.procs.items()}

    def created(self, pid):
        if pid not in self.procs:
            raise OSError("gone")
        return self.procs[pid][2]

    def set_priority(self, pid, cls):
        if pid in self.fail:
            raise OSError("denied")
        self.set.append((pid, cls))


MAIN = "1234567" + "a" * 33  # local main (the approved sha)
ORIGIN = "b" * 40  # origin/main, its parent
SCHEMA = "X = 1\nSCHEMA_VERSION = 10\n"


class FakeGit:
    """Fake git/gh: a verified main one commit ahead of origin/main, CI green on ci/p2b."""

    def __init__(self):
        self.calls = []
        self.main, self.origin = MAIN, ORIGIN
        self.fail = {}  # args prefix tuple -> (rc, output) answered instead
        self.diff = "jarvis/core.py\nscripts/conveyor.py\n"
        self.memory = {ORIGIN: SCHEMA, MAIN: SCHEMA}
        self.contains = "  origin/ci/p2b\n  origin/feature\n"
        self.runs = [{"headSha": MAIN, "status": "completed", "conclusion": "success",
                      "workflowName": "tests", "url": "https://github.com/x/y/actions/runs/1"}]
        self.raise_on = None

    def __call__(self, args, timeout=120):
        self.calls.append(args)
        a = tuple(args)
        if self.raise_on and a[:len(self.raise_on)] == self.raise_on:
            raise TimeoutError("slow")
        for k, v in self.fail.items():
            if a[:len(k)] == k:
                return v
        if a == ("git", "rev-parse", "--verify", "refs/heads/main"):
            return 0, self.main + "\n"
        if a == ("git", "fetch", "origin", "main"):
            return 0, ""
        if a == ("git", "rev-parse", "--verify", "refs/remotes/origin/main"):
            return 0, self.origin + "\n"
        if a == ("git", "merge-base", "--is-ancestor", self.origin, self.main):
            return 0, ""
        if a == ("git", "diff", "--name-only", f"{self.origin}..{self.main}"):
            return 0, self.diff
        if a[:2] == ("git", "show") and a[2].endswith(":jarvis/memory.py"):
            text = self.memory.get(a[2].split(":")[0])
            return (0, text) if text is not None else (128, "fatal: no such path")
        if a == ("git", "branch", "-r", "--contains", self.main):
            return 0, self.contains
        if a[:3] == ("gh", "run", "list"):
            return 0, json.dumps(self.runs)
        if a[:2] == ("git", "push"):
            return 0, "ok"
        return 1, "unexpected"

    def pushes(self):
        return [c for c in self.calls if c[:2] == ["git", "push"]]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = FakeClaude()
        self.git = FakeGit()
        self.free = 100e9
        self.memory = (64.0, 20.0)  # (free GB, commit %): never the real machine's memory
        self.idle = 0.0  # user idle seconds: never the real machine's input
        self.procs = FakeProcs()  # never the real machine's processes
        self.net = False  # API host reachable: never the real network
        self.t = float(int(time.time()))
        self.pushes = []
        self.key = ""  # reader off by default; never the real key lookup (env/registry)
        self.posts = []
        self.reply = (500, None)
        self.c = Conveyor(self.root, self.fake, now=lambda: self.t,
                          send=lambda text: self.pushes.append(text) or "sent",
                          post=self.fake_post, key=lambda: self.key,
                          cmd=self.git, disk=self.fake_disk, projects=self.root / "projects",
                          mem=lambda: self.memory, idle=lambda: self.idle, procs=self.procs,
                          net=lambda: self.net)
        self.base = self.root / "build" / "conveyor"
        self.hour = 12  # local hour for the night rule: never the real clock's
        hour = mock.patch.object(conveyor, "local_hour", lambda t: self.hour)
        hour.start()
        self.addCleanup(hour.stop)

    def fake_disk(self, path):
        if isinstance(self.free, Exception):
            raise self.free
        return mock.Mock(free=self.free)

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

    def attn_ex_dry(self):
        return [i for i in self.attn() if i != conveyor.DRY_ID]

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
        self.assertIsNotNone(parse_header("﻿@jarvis lane=A-1 model=sonnet effort=xhigh base=abcdef1\r\n"))

    def test_invalid(self):
        for bad in ["", "TASK X", "@jarvis lane=A model=opus effort=huge base=072d0fe",
                    "@jarvis lane=A model=opus effort=high", "@jarvis lane=A B model=opus effort=high base=072d0fe",
                    "@jarvis lane=A model=opus effort=high base=xyz"]:
            self.assertIsNone(parse_header(bad), bad)

    def test_lint_rules(self):
        """The jarvis-flow lint's parseHeader: any field order, exact families and efforts."""
        ok = conveyor.check_header("@jarvis base=main effort=medium model=haiku lane=g6.k3\nbody")
        self.assertEqual(ok, ({"lane": "g6.k3", "model": "haiku", "effort": "medium", "base": "main"}, None))
        cases = {"@jarvis lane=AGY0 model=opus effort=low base=main": "effort invalid: low",
                 "@jarvis lane=A model=opus effort=max base=main": "effort invalid: max",
                 "@jarvis lane=A model=claude-opus-5-5 effort=high base=main": "model invalid",
                 "@jarvis lane=A model=opus effort=High base=main": "effort invalid",
                 "@jarvis lane=-A model=opus effort=high base=main": "lane invalid",
                 "@jarvis lane=" + "A" * 65 + " model=opus effort=high base=main": "lane invalid",
                 "@jarvis lane=A model=opus effort=high base=ABCDEF1": "base invalid",
                 "@jarvis lane=A model=opus effort=high base=main lane=B": "duplicate field: lane",
                 "@jarvis lane=A model=opus effort=high base=main tier=T1": "unknown field: tier",
                 "@jarvis lane=A model=opus effort=high base=main x": "bad token: x",
                 "@jarvis lane=A model=opus base=main": "missing field: effort",
                 "@jarvisx lane=A model=opus effort=high base=main": "not an '@jarvis"}
        for line, why in cases.items():
            hdr, reason = conveyor.check_header(line)
            self.assertIsNone(hdr, line)
            self.assertIn(why, reason, line)

    def test_rules_match_lint_source(self):
        """Drift guard: the table in conveyor.py equals header.ts's constants."""
        src = (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "jarvis-flow" / "hooks"
               / "lib" / "header.ts").read_text(encoding="utf-8")

        def arr(name):
            m = re.search(rf"export const {name}: readonly \w+\[\] = \[([^\]]*)\]", src)
            self.assertIsNotNone(m, name)
            return tuple(re.findall(r"'([^']*)'", m.group(1)))

        def regex(name):
            m = re.search(rf"const {name} = /(.*)/\s*$", src, re.M)
            self.assertIsNotNone(m, name)
            return m.group(1)
        self.assertEqual(arr("FAMILIES"), conveyor.FAMILIES)
        self.assertEqual(arr("EFFORTS"), conveyor.EFFORTS)
        self.assertEqual(regex("LANE"), conveyor.LANE_RE.pattern)
        self.assertEqual(regex("SHA"), conveyor.SHA_HDR_RE.pattern)
        m = re.search(r"const REQUIRED = \[([^\]]*)\]", src)
        self.assertEqual(tuple(re.findall(r"'([^']*)'", m.group(1))), conveyor.HEADER_KEYS)
        self.assertIn(r"/^([a-z]+)=(\S+)$/", src)  # token shape mirrored in check_header
        self.assertIn(r"/^@jarvis(\s|$)/", src)  # header detection mirrored in check_header
        lint = (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "jarvis-flow" / "hooks"
                / "lib" / "lint.ts").read_text(encoding="utf-8")
        # lint.ts blocks every header parseHeader calls invalid, and a sha base that is not
        # canonical main's HEAD (check_header's `main`); its model/effort checks are per session
        self.assertIn("if (parsed.kind === 'invalid') return { kind: 'block'", lint)
        self.assertIn("header.base !== 'main' && ctx.mainHead !== null && !shaMatches(header.base, ctx.mainHead)",
                      lint)
        self.assertIn("if (x.length < 7 || y.length < 7) return false", src)
        self.assertIn("return x.startsWith(y) || y.startsWith(x)", src)

    def test_base_must_be_main_head(self):
        line = "@jarvis lane=A model=opus effort=high base=abcdef1"
        self.assertIsNotNone(conveyor.check_header(line)[0])  # main unknown: passes, like the lint
        self.assertIsNotNone(conveyor.check_header(line, "abcdef1234567890")[0])
        self.assertIsNotNone(conveyor.check_header(line.replace("abcdef1", "abcdef1234"), "abcdef12")[0])
        hdr, why = conveyor.check_header(line, "1234567" + "a" * 33)
        self.assertIsNone(hdr)
        self.assertEqual(why, "base abcdef1 is not main 1234567aaaaa")
        self.assertIsNotNone(conveyor.check_header(line.replace("abcdef1", "main"), "1234567")[0])

    def test_js_whitespace(self):
        self.assertIsNotNone(parse_header("@jarvis lane=A model=opus　effort=high base=main"))
        self.assertIsNone(parse_header("@jarvis lane=A\x1cmodel=opus effort=high base=main"))


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

    def test_lint_invalid_header_moved_to_rejected_with_attention(self):
        """Incident A: effort=low passed the old check, the lint dropped it, an empty session
        held a slot. Now it is never dispatched, moved (not deleted), and in attention."""
        text = "@jarvis lane=AGY0 model=opus effort=low base=main\nbody\n"
        self.drop("agy0.md", text)
        self.drop("ok.md", HDR.format("B"), age=1)
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["B"])
        rej = self.base / "inbox" / "rejected"
        self.assertEqual((rej / "agy0.md").read_text(encoding="utf-8"), text)
        self.assertIn("effort invalid: low", (rej / "agy0.md.reason.txt").read_text())
        (item,) = [a for a in self.status()["attention"] if a["id"] == "reject-agy0"]
        self.assertTrue(item["reason"].startswith("header invalid: effort invalid: low"))
        self.assertNotIn("AGY0", self.lanes())
        # the same file name again: the earlier rejected file is kept, not overwritten
        self.t += 1
        self.drop("agy0.md", "@jarvis lane=AGY0 model=opus effort=max base=main\n")
        self.c.pass_once()
        self.assertEqual((rej / "agy0.md").read_text(encoding="utf-8"), text)
        self.assertEqual(len(list(rej.glob("*agy0.md"))), 2)
        self.assertEqual(len([i for i in self.attn() if i.startswith("reject-")]), 2)
        # acked: gone from attention, never backstop-paged after
        (self.base / "ack").mkdir()
        for i in self.attn():
            (self.base / "ack" / i).write_text("")
            os.utime(self.base / "ack" / i, (self.t, self.t))
        self.t += conveyor.ATTENTION_PAGE_S + 1
        self.c.pass_once()
        self.assertEqual([i for i in self.attn() if i.startswith("reject-")], [])
        self.assertEqual(self.pushes, [])

    def test_stale_base_rejected_like_the_lint(self):
        self.drop("old.md", "@jarvis lane=OLD model=opus effort=high base=072d0fe\n")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        self.assertIn(f"base 072d0fe is not main {MAIN[:12]}",
                      (self.base / "inbox" / "rejected" / "old.md.reason.txt").read_text())
        self.assertIn("reject-old", self.attn())
        self.git.fail[("git", "rev-parse")] = (128, "fatal")  # main unknown: passes, like the lint
        self.drop("old2.md", "@jarvis lane=OLD2 model=opus effort=high base=072d0fe\n")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["OLD2"])

    def test_unacked_reject_backstop_pages_once(self):
        self.drop("x.md", "@jarvis lane=X model=gpt effort=high base=main\n")
        self.c.pass_once()
        self.assertEqual(self.pushes, [])  # orchestrator first, never straight to the phone
        self.t += conveyor.ATTENTION_PAGE_S
        self.c.pass_once()
        self.t += conveyor.PAGE_GAP_S
        self.c.pass_once()
        self.assertEqual(len([p for p in self.pushes if "reject-x" in p]), 1)

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
                                  "usage_hold", "pace", "disk_hold", "disk_free_gb",
                                  "memory_hold", "memory", "presence", "lanes",
                                  "backlog", "breaker", "band", "attention"})
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
        self.git.main = "abc1234" + "0" * 33  # the kept base must be main's HEAD (lint rule)
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

    def test_lint_invalid_slices_held_in_attention(self):
        self.backlog({"id": "LOW", "effort": "low"}, {"id": "OLD"}, {"id": "OK"})
        (self.base / "prompts" / "OLD.md").write_text(
            "@jarvis lane=OLD model=opus effort=high base=072d0fe\nTASK\n", encoding="utf-8")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["OK"])
        self.assertTrue(self.view()["OLD"].startswith("held: base 072d0fe is not main"))
        (item,) = [a for a in self.status()["attention"] if a["id"] == conveyor.BACKLOG_BAD_ID]
        self.assertIn("bad effort", item["reason"])
        self.assertIn("slice OLD: held: base 072d0fe is not main", item["reason"])
        since = item["since"]
        self.t += 120
        self.c.pass_once()
        (item,) = [a for a in self.status()["attention"] if a["id"] == conveyor.BACKLOG_BAD_ID]
        self.assertEqual(item["since"], since)  # stable: an ack holds
        self.backlog({"id": "OK"})
        self.c.pass_once()
        self.assertNotIn(conveyor.BACKLOG_BAD_ID, self.attn())

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


class TestDeadSession(Base):
    """Incident A: dispatched sessions whose prompt never arrived held 2 of 3 slots."""

    def transcript(self, a, *types):
        d = self.root / "projects" / "C--repo"
        d.mkdir(parents=True, exist_ok=True)
        meta = [{"type": "custom-title"}, {"type": "system", "subtype": "informational"}]
        rows = meta + [{"type": t} for t in types]
        (d / f"{a['sessionId']}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")

    def start3(self):
        for i, n in enumerate("ABCD"):
            self.drop(f"{n}.md", HDR.format(n), age=i)
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B", "C"])

    def test_undelivered_prompt_frees_slot_after_5_min(self):
        self.start3()
        a, b, c = (self.fake.agent(n) for n in "ABC")
        a.update(status="idle", state="blocked")  # the lint dropped its prompt
        self.transcript(a)                           # metadata only, no message
        self.transcript(b, "user", "assistant")      # delivered
        b.update(status="idle", state="blocked")     # e.g. waiting on a permission
        self.transcript(c)                           # no message yet, but busy
        self.t += conveyor.DEAD_S - 1
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B", "C"])  # not before 5 min
        self.t += 1
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B", "C", "D"])
        ln = self.lanes()
        self.assertEqual((ln["A"]["status"], ln["A"]["finished"]), ("DEAD", True))
        self.assertIn("prompt not delivered", ln["A"]["dead"])
        self.assertTrue(ln["B"]["delivered"])
        self.assertNotIn("status", ln["B"])
        self.assertNotIn("status", ln["C"])
        (item,) = [x for x in self.status()["attention"] if x["id"] == "A"]
        self.assertTrue(item["reason"].startswith("prompt not delivered / session gone: "))
        self.assertEqual(self.posts, [])  # no reader call for a lane that never ran
        self.assertEqual(self.pushes, [])  # attention first, not the phone
        self.assertNotIn(["stop", a["id"]], self.fake.calls)  # the session is left alone
        self.assertNotIn(["rm", a["id"]], self.fake.calls)

    def test_session_gone_frees_slot(self):
        self.start3()
        self.fake.agents = [x for x in self.fake.agents if x["name"] != "A"]  # archived/removed
        self.t += conveyor.DEAD_S
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B", "C", "D"])
        self.assertIn("session gone", self.lanes()["A"]["dead"])
        self.assertIn("A", self.attn())

    def test_no_transcript_found_keeps_the_slot(self):
        self.start3()
        self.fake.agent("A").update(status="idle", state="blocked")
        self.t += conveyor.DEAD_S * 3
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B", "C"])
        self.assertFalse(self.lanes()["A"].get("finished"))

    def test_dead_lane_later_report_finishes_it(self):
        self.start3()
        a = self.fake.agent("A")
        a.update(status="idle", state="blocked")
        self.transcript(a)
        self.t += conveyor.DEAD_S
        self.c.pass_once()
        self.assertEqual(self.lanes()["A"]["status"], "DEAD")
        self.report("A", "PASS", mtime=self.t)  # the orchestrator rescued the session in place
        self.c.pass_once()
        self.assertEqual(self.lanes()["A"]["status"], "PASS")
        self.assertNotIn("A", self.attn())

    def test_report_wins_over_dead(self):
        self.start3()
        a = self.fake.agent("A")
        a.update(status="idle", state="blocked")
        self.transcript(a)
        self.t += conveyor.DEAD_S
        self.report("A", "PASS", mtime=self.t)
        self.c.pass_once()
        self.assertEqual(self.lanes()["A"]["status"], "PASS")


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

    def test_redispatch_with_stale_base_not_sent(self):
        a, _ = self.setup_respawned()
        self.git.main = "9" * 40  # main moved since dispatch: the lint would drop the prompt
        self.t += conveyor.IDLE_REDISPATCH_S + 1
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.assertNotIn(["stop", a["id"]], self.fake.calls)
        ln = self.lanes()["A"]
        self.assertEqual(ln["status"], "DEAD")
        self.assertEqual(ln["dead"], "re-dispatch not sent: base 1234567 is not main 999999999999")
        self.assertIn("A", self.attn())

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


class TestBlockedLane(Base):
    """CONV9: G6R1, INT18, G6R1B sat 'blocked' after a network loss (2026-10-10)."""

    def setUp(self):
        super().setUp()
        self.drop("a.md", HDR.format("A") + "do things\n")
        self.c.pass_once()
        self.a = self.fake.agent("A")
        self.wt = self.root / ".claude" / "worktrees" / "a"
        self.wt.mkdir(parents=True)
        old = self.wt / "x.py"
        old.write_text("x")
        os.utime(old, (self.t - 9000, self.t - 9000))
        self.block()
        self.c.pass_once()  # first seen blocked: the episode starts now

    def block(self):
        self.a.update(state="blocked", status="idle", cwd=str(self.wt))

    def resumes(self):
        return [c for c in self.fake.calls if c[:2] == ["--bg", "--resume"]]

    def nudges(self):
        return [e for e in self.events() if e["event"] == "nudge"]

    def test_under_nudge_time_nothing(self):
        self.net = True
        self.t += 599
        self.c.pass_once()
        self.assertEqual((self.resumes(), self.nudges()), ([], []))
        self.assertNotIn(["stop", self.a["id"]], self.fake.calls)

    def test_nudge_after_10_min_with_network(self):
        self.net = True
        self.t += 600
        self.c.pass_once()
        sid = self.a["sessionId"]
        self.assertEqual(self.resumes(), [[
            "--bg", "--resume", sid, "--name", "A", "--model", "opus", "--effort", "high",
            "--settings", str(self.root / conveyor.SETTINGS), conveyor.NUDGE]])
        stop = self.fake.calls.index(["stop", self.a["id"]])
        self.assertLess(stop, self.fake.calls.index(self.resumes()[0]))  # stopped first
        self.assertEqual(self.fake.cwds[self.fake.calls.index(self.resumes()[0])], str(self.wt))
        (ev,) = self.nudges()
        self.assertEqual((ev["lane"], ev["id"], ev["n"]), ("A", self.a["id"], 1))
        self.assertEqual([s["rc"] for s in ev["steps"]], [0, 0])
        ln = self.lanes()["A"]
        self.assertEqual((ln["id"], ln["nudges"]), (self.a["id"], 1))
        self.t += 3600  # resumed and busy: nothing more
        self.c.pass_once()
        self.assertEqual(len(self.resumes()), 1)
        self.assertNotIn("blocked_since", self.lanes()["A"])

    def test_network_down_no_nudge(self):
        self.t += 1200
        self.c.pass_once()
        self.assertEqual((self.resumes(), self.nudges()), ([], []))
        self.net = True  # back: the nudge goes out on the next pass
        self.c.pass_once()
        self.assertEqual(len(self.resumes()), 1)

    def test_at_most_two_nudges_15_min_apart(self):
        self.net = True
        for dt in (600, 600, 300, 900, 900):  # 10, 20, 25, 40, 55 min blocked
            self.t += dt
            self.c.pass_once()
            self.block()  # the resumed turn hit the error again before the next pass
        self.assertEqual([e["n"] for e in self.nudges()], [1, 2])
        self.assertEqual(len(self.resumes()), 2)
        times = [e["ts"] for e in self.nudges()]
        self.assertEqual(times, [conveyor.iso(self.t - 2700), conveyor.iso(self.t - 1800)])

    def test_report_present_no_nudge(self):
        self.net = True
        self.report("A", "PASS", mtime=self.t)
        self.t += 3000
        self.c.pass_once()
        self.c.pass_once()
        self.assertEqual((self.resumes(), self.nudges()), ([], []))
        self.assertTrue(self.lanes()["A"]["finished"])
        self.assertNotIn("A", self.attn())

    def test_attention_once_after_45_min_not_paged_at_night(self):
        self.hour = 3
        self.t += conveyor.BLOCKED_ATTENTION_S - 1
        self.c.pass_once()
        self.assertNotIn("A", self.attn())
        self.t += 1
        self.c.pass_once()
        (item,) = [x for x in self.status()["attention"] if x["id"] == "A"]
        self.assertTrue(item["reason"].startswith("lane blocked, needs orchestrator: "))
        self.assertEqual(item["since"], conveyor.iso(self.t))
        self.t += conveyor.ATTENTION_PAGE_S
        self.c.pass_once()
        self.assertEqual(len([e for e in self.events() if e["event"] == "blocked_attention"]), 1)
        self.assertIn("A", self.attn())
        self.assertEqual(self.pushes, [])  # night: the orchestrator's item, no phone page
        self.assertEqual(self.resumes(), [])  # the network stayed down
        ack = self.base / "ack"
        ack.mkdir()
        (ack / "A").write_text("")
        os.utime(ack / "A", (self.t, self.t))
        self.c.pass_once()
        self.assertNotIn("A", self.attn())

    def test_worktree_activity_defers_attention(self):
        self.t += conveyor.BLOCKED_ATTENTION_S
        f = self.wt / "new.py"
        f.write_text("work")
        os.utime(f, (self.t - 60, self.t - 60))
        self.c.pass_once()
        self.assertNotIn("A", self.attn())

    def test_pace_override_is_clamped(self):
        self.net = True
        (self.base / "pace.json").write_text(json.dumps({**conveyor.PACE_DEFAULTS, "blocked_nudge_min": 1}))
        self.t += 299
        self.c.pass_once()
        self.assertEqual(self.resumes(), [])
        self.t += 1  # 1 min is clamped to 5
        self.c.pass_once()
        self.assertEqual(len(self.resumes()), 1)


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


RESET_ALERT = "Haftalık kota %{}: sıfırlama hakkını şimdi kullanabilirsin"


class TestUsageGuard(Base):
    def ledger(self, pct, age=0):
        d = self.root / "build" / "ledger"
        d.mkdir(parents=True, exist_ok=True)
        end = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.t - age))
        (d / "slices.jsonl").write_text(
            "garbage\n" + json.dumps({"end": end, "seven_day": {"start": 1, "end": pct}}) + "\n")

    def test_over_limit_holds_new_lanes(self):
        self.ledger(98)  # full_burn (pace.json default): full_burn_limit_pct
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        self.assertTrue(self.status()["usage_hold"])
        self.assertEqual(self.pushes, [RESET_ALERT.format(98)])  # the hold itself never pages

    def test_old_high_row_still_holds(self):
        self.ledger(98, age=7 * 3600)  # no new rows while held: the reading must not expire in hours
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])

    def test_under_limit_or_stale_dispatches(self):
        self.ledger(97)
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.ledger(99, age=conveyor.USAGE_FRESH_S + 1)
        self.drop("b.md", HDR.format("B"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B"])
        self.assertIsNone(self.status()["usage_pct"])


class TestPaceGovernor(TestUsageGuard):
    """pace.json modes, pace bands, the reset alert and reset detection, on a fixed clock."""

    def fresh(self):
        self.tearDown()
        self.setUp()

    def pace(self, **kw):
        cfg = {**conveyor.PACE_DEFAULTS, **kw}
        (self.base / "pace.json").write_text(json.dumps(cfg), encoding="utf-8")

    def pace_file(self):
        return json.loads((self.base / "pace.json").read_text(encoding="utf-8"))

    def paced(self, days_ago=3.5):  # 3.5 of 7 days elapsed: pace_line exactly 50
        self.pace(mode="paced", window_start=conveyor.iso(self.t - days_ago * 86400))

    def lanes_for(self, pct):
        """Effective max lanes for one reading, three prompts waiting."""
        self.ledger(pct)
        for n in "XYZ":
            self.drop(f"{n}.md", HDR.format(n))
        self.c.pass_once()
        return len(self.fake.bg_calls())

    def test_missing_written_with_full_burn_defaults(self):
        self.c.pass_once()
        self.assertEqual(self.pace_file(), conveyor.PACE_DEFAULTS)
        self.assertEqual(self.status()["pace"]["mode"], "full_burn")

    def test_full_burn_limit(self):
        self.assertEqual(self.lanes_for(97.9), 3)
        self.assertFalse(self.status()["usage_hold"])
        self.fresh()
        self.assertEqual(self.lanes_for(98), 0)
        self.assertEqual((self.status()["pace"]["limit_pct"], self.status()["pace"]["max_lanes"]), (98, 0))

    def test_full_burn_limit_is_read_from_pace_json(self):
        self.pace(full_burn_limit_pct=90)
        self.assertEqual(self.lanes_for(90), 0)

    def test_paced_limit_97(self):
        self.paced(days_ago=7)  # pace_line 100: every band allows 3, only the limit holds
        self.assertEqual(self.lanes_for(96.9), 3)
        self.fresh()
        self.paced(days_ago=7)
        self.assertEqual(self.lanes_for(97), 0)
        self.assertTrue(self.status()["usage_hold"])
        self.assertEqual(self.status()["pace"]["limit_pct"], 97)

    def test_pace_bands_at_boundaries(self):
        for pct, want in ((52, 3), (52.1, 2), (57, 2), (57.1, 1), (62, 1), (62.1, 0)):
            self.fresh()
            self.paced()
            self.assertEqual(self.lanes_for(pct), want, pct)
            p = self.status()["pace"]
            self.assertEqual((p["mode"], p["pace_line"], p["usage_pct"], p["max_lanes"]),
                             ("paced", 50.0, pct, want))
            self.assertFalse(self.status()["usage_hold"])  # band 0 holds dispatch, not via the limit
        self.assertEqual(p["band"], "over")

    def test_band_zero_leaves_running_lanes(self):
        self.paced()
        self.assertEqual(self.lanes_for(40), 3)
        self.ledger(70)
        self.drop("w.md", HDR.format("W"))
        self.c.pass_once()
        self.assertEqual(len(self.fake.bg_calls()), 3)
        self.assertFalse([c for c in self.fake.calls if c[0] in ("stop", "rm")])
        self.assertEqual(len(self.c.running(self.lanes())), 3)

    def test_running_lanes_count_against_the_band(self):
        self.paced()
        self.assertEqual(self.lanes_for(40), 3)
        self.ledger(55)  # band 2, three running (integration lanes alike): nothing new
        self.drop("w.md", HDR.format("W"))
        self.c.pass_once()
        self.assertEqual(len(self.fake.bg_calls()), 3)

    def test_model_and_effort_unchanged(self):
        self.paced()
        self.ledger(57)  # band 2
        self.drop("a.md", "@jarvis lane=A model=sonnet effort=xhigh base=main\nx\n")
        self.backlog({"id": "S", "model": "haiku", "effort": "medium"})
        self.c.pass_once()
        got = {c[2]: (c[c.index("--model") + 1], c[c.index("--effort") + 1])
               for c in self.fake.bg_calls()}
        self.assertEqual(got, {"A": ("sonnet", "xhigh"), "S": ("haiku", "medium")})

    def test_reset_alert_once(self):
        self.ledger(94.9)
        self.c.pass_once()
        self.ledger(95)
        self.c.pass_once()
        self.t += conveyor.PAGE_GAP_S
        self.ledger(96)
        self.c.pass_once()
        self.assertEqual(self.pushes, [RESET_ALERT.format(95)])
        self.assertIn(conveyor.RESET_ALERT_ID, self.attn())

    def test_no_reset_alert_in_paced_mode(self):
        self.paced(days_ago=7)
        self.ledger(96)
        self.c.pass_once()
        self.assertEqual(self.pushes, [])
        self.assertNotIn(conveyor.RESET_ALERT_ID, self.attn())

    def test_reset_detection_switches_to_paced(self):
        self.ledger(96)
        self.c.pass_once()
        self.t += 3600
        self.ledger(56)  # a 40-point drop: the reset was used
        self.c.pass_once()
        self.assertEqual(self.pace_file(), {**conveyor.PACE_DEFAULTS, "mode": "paced",
                                            "window_start": conveyor.iso(self.t)})
        (ev,) = [e for e in self.events() if e["event"] == "quota_reset"]
        self.assertEqual((ev["before"], ev["after"]), (96, 56))
        attn = {a["id"]: a["reason"] for a in self.status()["attention"]}
        self.assertEqual(attn[conveyor.RESET_DONE_ID], "kota sıfırlandı, tempo modu açık")
        self.assertNotIn(conveyor.RESET_ALERT_ID, attn)  # paced: the alert episode is over
        self.assertEqual(self.status()["pace"]["max_lanes"], 0)  # 56 > pace_line 0 + 12

    def test_smaller_drop_is_no_reset(self):
        self.ledger(96)
        self.c.pass_once()
        self.ledger(56.1)
        self.c.pass_once()
        self.assertEqual(self.pace_file()["mode"], "full_burn")
        self.assertNotIn(conveyor.RESET_DONE_ID, self.attn())

    def test_hand_edit_is_reread_every_pass(self):
        self.ledger(60)
        self.c.pass_once()
        self.assertEqual(self.status()["pace"]["max_lanes"], 3)
        self.paced()
        self.c.pass_once()
        self.assertEqual(self.status()["pace"]["max_lanes"], 1)

    def test_invalid_pace_is_paced_from_now(self):
        for bad in ("{not json", json.dumps({"mode": "warp"}), json.dumps({"mode": "paced"}),
                    json.dumps({"days": 0}), json.dumps([1])):
            self.fresh()
            (self.base / "pace.json").write_text(bad, encoding="utf-8")
            self.assertEqual(self.lanes_for(12.1), 0, bad)  # pace_line 0: above 0 + 12
            p = self.status()["pace"]
            self.assertEqual((p["mode"], p["window_start"], p["pace_line"]),
                             ("paced", conveyor.iso(self.t), 0.0))
            self.assertTrue(p["error"])
            self.assertIn(conveyor.PACE_BAD_ID, self.attn())
            self.assertEqual((self.base / "pace.json").read_text(encoding="utf-8"), bad)  # left as is

    def lanes_for_8(self, pct):
        """Effective max lanes for one reading, eight prompts waiting."""
        self.ledger(pct)
        for n in "ABCDEFGH":
            self.drop(f"{n}.md", HDR.format(n))
        self.c.pass_once()
        return len(self.fake.bg_calls())

    def test_max_lanes_clamp(self):
        for raw, want in ((0, 1), (-4, 1), (1, 1), (5, 5), (6, 6), (7, 6), (40, 6), (4.9, 4)):
            self.fresh()
            self.pace(max_lanes=raw)
            self.assertEqual(self.lanes_for_8(10), want, raw)
            self.assertEqual(self.status()["pace"]["max_lanes"], want)
            self.assertIsNone(self.status()["pace"]["error"])

    def test_max_lanes_not_a_number_is_invalid(self):
        for bad in ('"5"', "true", "null"):
            self.fresh()
            (self.base / "pace.json").write_text('{"max_lanes": ' + bad + "}", encoding="utf-8")
            self.c.pass_once()
            self.assertTrue(self.status()["pace"]["error"], bad)
            self.assertIn(conveyor.PACE_BAD_ID, self.attn())

    def test_full_burn_uses_max_lanes(self):
        self.pace(max_lanes=5)
        self.assertEqual(self.lanes_for_8(90), 5)
        self.assertEqual(self.status()["pace"]["band"], "full_burn")

    def test_max_lanes_reread_every_pass(self):
        self.pace(max_lanes=2)
        self.assertEqual(self.lanes_for_8(10), 2)
        self.pace(max_lanes=4)
        self.c.pass_once()
        self.assertEqual(len(self.fake.bg_calls()), 4)

    def test_paced_bands_with_max_lanes_5(self):
        for pct, want in ((52, 5), (52.1, 2), (57, 2), (57.1, 1), (62, 1), (62.1, 0)):
            self.fresh()
            self.pace(mode="paced", window_start=conveyor.iso(self.t - 3.5 * 86400), max_lanes=5)
            self.assertEqual(self.lanes_for_8(pct), want, pct)
            self.assertEqual(self.status()["pace"]["max_lanes"], want)

    def test_paced_lower_bands_capped_by_max_lanes_1(self):
        for pct, want in ((52, 1), (55, 1), (60, 1), (62.1, 0)):
            self.fresh()
            self.pace(mode="paced", window_start=conveyor.iso(self.t - 3.5 * 86400), max_lanes=1)
            self.assertEqual(self.lanes_for_8(pct), want, pct)

    def test_paced_unknown_usage_at_most_3(self):
        self.pace(mode="paced", window_start=conveyor.iso(self.t - 3.5 * 86400), max_lanes=6)
        for n in "ABCDEFGH":
            self.drop(f"{n}.md", HDR.format(n))
        self.c.pass_once()
        self.assertEqual(len(self.fake.bg_calls()), 3)
        self.assertEqual(self.status()["pace"]["band"], "unknown")


class TestMemoryGuard(Base):
    def test_low_free_memory_holds_and_releases(self):
        self.memory = (2.9, 50.0)
        self.drop("a.md", HDR.format("A") + "x")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        st = self.status()
        self.assertTrue(st["memory_hold"])
        self.assertEqual(st["memory"], {"free_gb": 2.9, "commit_pct": 50.0})
        self.assertEqual(st["pace"]["band"], "memory hold")
        self.memory = (3.0, 50.0)
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.assertFalse(self.status()["memory_hold"])
        self.assertEqual(self.status()["pace"]["band"], "full_burn")

    def test_high_commit_charge_holds(self):
        self.memory = (32.0, 90.1)
        self.drop("a.md", HDR.format("A") + "x")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        self.assertTrue(self.status()["memory_hold"])
        self.memory = (32.0, 90.0)
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])

    def test_min_free_mem_read_from_pace_json(self):
        (self.base / "pace.json").write_text(json.dumps({"min_free_mem_gb": 8}), encoding="utf-8")
        self.memory = (7.5, 10.0)
        self.drop("a.md", HDR.format("A") + "x")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])

    def test_max_commit_pct_read_from_pace_json_and_clamped(self):
        for pct, mem, hold in ((95, 94.0, False), (95, 95.5, True), (70, 85.0, True),
                               (70, 80.0, False), (99, 97.5, True), (99, 96.9, False)):
            with self.subTest(pct=pct, mem=mem):
                (self.base / "pace.json").write_text(json.dumps({"max_commit_pct": pct}), encoding="utf-8")
                self.memory = (32.0, mem)
                self.c.memory = self.c.mem()
                self.c.govern()
                self.assertEqual(self.c.mem_hold, hold)
        self.assertEqual(conveyor.PACE_DEFAULTS["max_commit_pct"], 90)

    def test_running_lanes_untouched(self):
        self.drop("a.md", HDR.format("A") + "x")
        self.c.pass_once()
        self.memory = (1.0, 99.0)
        self.drop("b.md", HDR.format("B") + "x")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.assertFalse([c for c in self.fake.calls if c[0] in ("stop", "rm")])
        self.assertEqual(self.c.running(self.lanes()), ["A"])

    def test_unknown_memory_no_hold_logged_once(self):
        self.memory = None
        self.drop("a.md", HDR.format("A") + "x")
        self.c.pass_once()
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.assertFalse(self.status()["memory_hold"])
        self.assertIsNone(self.status()["memory"])
        self.assertEqual(len([e for e in self.events() if e["event"] == "memory_unknown"]), 1)

    def test_real_mem_shape(self):
        m = conveyor.real_mem()  # read-only stdlib call; None off Windows
        if m is not None:
            self.assertGreater(m[0], 0)
            self.assertTrue(0 <= m[1] <= 100)


class TestPresence(Base):
    """CONV6 presence modes: fake idle source, fake process API, fixed clock."""

    def passes(self, n, step=60):
        for _ in range(n):
            self.c.pass_once()
            self.t += step

    def go_away(self):
        self.idle = 600
        self.passes(3)  # pending at +0, still held at +60, counts at +120
        self.assertEqual(self.c.presence, "away")

    def pace(self, **kw):
        (self.base / "pace.json").write_text(json.dumps(kw), encoding="utf-8")

    def test_thresholds_and_hysteresis(self):
        self.c.pass_once()
        self.assertEqual(self.status()["presence"], {"state": "present", "idle_s": 0})
        self.idle = 599  # under away_after_min 10
        self.passes(4)
        self.assertEqual(self.c.presence, "present")
        self.idle = 600
        self.passes(2)  # held 60 s: not yet
        self.assertEqual(self.c.presence, "present")
        self.c.pass_once()  # held 120 s
        self.assertEqual(self.status()["presence"]["state"], "away")
        self.assertEqual(self.status()["pace"]["presence"], "away")
        self.assertEqual([e["state"] for e in self.events() if e["event"] == "presence"], ["away"])
        self.t += 60
        self.idle = 300  # between the thresholds: unchanged
        self.passes(4)
        self.assertEqual(self.c.presence, "away")
        self.idle = 10  # input: pending present
        self.passes(1)
        self.idle = 70  # no input for a minute: the pending change is dropped
        self.passes(1)
        self.idle = 5
        self.passes(2)
        self.assertEqual(self.c.presence, "away")  # only 60 s held since the restart
        self.passes(1)
        self.assertEqual(self.c.presence, "present")
        self.assertEqual([e["state"] for e in self.events() if e["event"] == "presence"],
                         ["away", "present"])

    def test_away_after_min_from_pace_json(self):
        self.pace(away_after_min=5)
        self.idle = 300
        self.passes(3)
        self.assertEqual(self.c.presence, "away")

    def test_idle_unavailable_is_present_logged_once(self):
        for src in (lambda: None, mock.Mock(side_effect=OSError("x"))):
            self.c.idle = src
            self.c.presence = "away"
            self.passes(3)
            self.assertEqual(self.c.presence, "present")
            self.assertIsNone(self.status()["presence"]["idle_s"])
        self.assertEqual([e["event"] for e in self.events()].count("presence_unknown"), 1)

    def test_lanes_per_presence(self):
        for i in range(7):
            self.drop(f"{i}.md", HDR.format(f"L{i}"))
        self.c.pass_once()
        self.assertEqual(len(self.fake.bg_calls()), 3)  # lanes_present default
        self.assertEqual(self.status()["pace"]["max_lanes"], 3)
        self.go_away()
        self.assertEqual(len(self.fake.bg_calls()), 5)  # lanes_away default
        self.assertEqual(self.status()["pace"]["max_lanes"], 5)

    def test_lanes_present_away_from_pace_json_and_max_lanes_override(self):
        self.pace(lanes_present=2, lanes_away=9)
        self.c.pass_once()
        self.assertEqual(self.status()["pace"]["max_lanes"], 2)
        self.go_away()
        self.assertEqual(self.status()["pace"]["max_lanes"], 6)  # clamped to MAX_LANES_CAP
        self.pace(lanes_present=2, lanes_away=4, max_lanes=1)
        self.c.pass_once()
        self.assertEqual(self.status()["pace"]["max_lanes"], 1)  # a hand-set max_lanes wins

    def test_away_still_under_memory_guard_and_paced_bands(self):
        self.go_away()
        self.memory = (1.0, 20.0)
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        self.assertEqual(self.status()["pace"]["band"], "memory hold")
        self.memory = (64.0, 20.0)
        self.pace(mode="paced", window_start=conveyor.iso(self.t - 3.5 * 86400))
        (self.root / "build" / "ledger").mkdir(parents=True)
        row = {"end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.t)),
               "seven_day": {"start": 55, "end": 55}}
        (self.root / "build" / "ledger" / "slices.jsonl").write_text(json.dumps(row) + "\n")
        self.c.pass_once()
        self.assertEqual(self.status()["pace"]["max_lanes"], 2)  # band +7 caps away's 5

    def own_tree(self):
        """L1's session (pid 101) with a child and grandchild; a pid-reuse orphan claiming
        101 as parent; an unrelated process; another claude session not started here."""
        self.drop("a.md", HDR.format("L1"))
        self.c.pass_once()
        self.procs.procs = {101: (1, "claude.exe", 1000), 500: (101, "bash.exe", 1001),
                            501: (500, "python.exe", 1002), 502: (101, "node.exe", 999),
                            900: (7, "vlc.exe", 5), 300: (1, "claude.exe", 10),
                            301: (300, "bash.exe", 11)}
        self.fake.agents.append({"id": "ffffffff", "pid": 300, "name": "OTHER", "kind": "background",
                                 "status": "busy", "state": "working"})

    def test_priority_only_own_tree(self):
        self.own_tree()
        self.c.pass_once()
        below, normal = conveyor.BELOW_NORMAL, conveyor.NORMAL
        self.assertEqual(sorted(self.procs.set), [(101, below), (500, below), (501, below)])
        self.c.pass_once()  # same pid, same presence: not re-applied
        self.assertEqual(len(self.procs.set), 3)
        self.procs.set = []
        self.go_away()
        self.assertEqual(sorted(self.procs.set), [(101, normal), (500, normal), (501, normal)])
        self.procs.set = []
        self.idle = 0
        self.passes(3)
        self.assertEqual(sorted(self.procs.set), [(101, below), (500, below), (501, below)])

    def test_priority_new_dispatch_and_respawned_pid(self):
        self.own_tree()
        self.c.pass_once()
        self.drop("b.md", HDR.format("L2"))
        self.c.pass_once()  # dispatched: pid 102 known from the next listing
        self.procs.procs[102] = (1, "claude.exe", 2000)
        self.procs.set = []
        self.c.pass_once()
        self.assertEqual(self.procs.set, [(102, conveyor.BELOW_NORMAL)])
        self.fake.agent("L1")["pid"] = 103  # respawned session, new pid
        self.procs.procs[103] = (1, "claude.exe", 3000)
        self.procs.set = []
        self.c.pass_once()
        self.assertEqual(self.procs.set, [(103, conveyor.BELOW_NORMAL)])

    def test_priority_not_claude_exe_is_untouched(self):
        self.own_tree()
        self.procs.procs[101] = (1, "notepad.exe", 1000)  # pid reused by something else
        self.c.pass_once()
        self.assertEqual(self.procs.set, [])

    def test_priority_failure_logged_never_fatal_not_retried(self):
        self.own_tree()
        self.procs.fail = {500}
        self.c.pass_once()
        self.assertEqual(sorted(self.procs.set),
                         [(101, conveyor.BELOW_NORMAL), (501, conveyor.BELOW_NORMAL)])
        (ev,) = [e for e in self.events() if e["event"] == "priority"]
        self.assertEqual(ev["failed"], ["500: OSError"])
        self.c.pass_once()
        self.assertEqual(len([e for e in self.events() if e["event"] == "priority"]), 1)
        self.procs.tree_error = OSError("snapshot")
        self.go_away()
        self.assertTrue([e for e in self.events() if e["event"] == "priority_error"])
        self.assertIsNone(self.status()["last_error"])
        self.c.procs = None  # off Windows
        self.idle = 0
        self.passes(3)
        self.assertEqual(len([e for e in self.events() if e["event"] == "priority_error"]), 2)

    def lc_calls(self):
        return [c for c in self.git.calls if c[0] == "LC.exe"]

    def test_power_switch_off_by_default(self):
        self.c.pass_once()
        cfg = json.loads((self.base / "pace.json").read_text(encoding="utf-8"))
        self.assertEqual((cfg["power_switch"], cfg["power_switch_cmd"]), (False, []))
        self.go_away()
        self.assertEqual([c for c in self.git.calls if c[0] != "git"], [])
        self.assertNotIn("power_switch", [e["event"] for e in self.events()])

    def test_power_switch_once_per_change(self):
        self.pace(power_switch=True, power_switch_cmd=["LC.exe", "--activate", "{plan}"])
        self.git.fail[("LC.exe",)] = (0, "ok")
        self.passes(3)
        self.assertEqual(self.lc_calls(), [])  # starting present is not a change
        self.go_away()
        self.passes(3)
        self.assertEqual(self.lc_calls(), [["LC.exe", "--activate", "JarvisFocus"]])
        self.idle = 0
        self.passes(3)
        self.assertEqual(self.lc_calls()[1:], [["LC.exe", "--activate", "JarvisWatch"]])

    def test_power_switch_failure_attention_no_retry(self):
        self.pace(power_switch=True, power_switch_cmd=["LC.exe", "--activate", "{plan}"])
        self.git.fail[("LC.exe",)] = (3, "refused")
        self.go_away()
        self.passes(5)
        self.assertEqual(len(self.lc_calls()), 1)
        (item,) = [a for a in self.status()["attention"] if a["id"] == conveyor.POWER_SWITCH_ID]
        self.assertIn("away: rc=3", item["reason"])
        self.git.fail[("LC.exe",)] = (0, "ok")
        self.idle = 0
        self.passes(3)
        self.assertEqual(len(self.lc_calls()), 2)
        self.assertNotIn(conveyor.POWER_SWITCH_ID, [a["id"] for a in self.status()["attention"]])

    def test_power_switch_missing_exe(self):
        self.pace(power_switch=True, power_switch_cmd=["missing.exe", "{plan}"])
        calls = []

        def cmd(args, timeout=120):
            if args[0] == "missing.exe":
                calls.append(args)
                raise FileNotFoundError("missing.exe")
            return self.git(args, timeout)
        self.c.cmd = cmd
        self.go_away()
        self.passes(3)
        self.assertEqual(len(calls), 1)
        (item,) = [a for a in self.status()["attention"] if a["id"] == conveyor.POWER_SWITCH_ID]
        self.assertIn("FileNotFoundError", item["reason"])

    def test_power_switch_without_cmd_is_invalid(self):
        for cfg in ({"power_switch": True}, {"power_switch": "yes"},
                    {"power_switch_cmd": "LC.exe"}, {"lanes_away": "5"}, {"away_after_min": 0}):
            self.pace(**cfg)
            self.c.pass_once()
            self.assertIn(conveyor.PACE_BAD_ID, [a["id"] for a in self.status()["attention"]], cfg)

class TestOneIntegrationLane(Base):
    def test_second_inbox_int_waits(self):
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        for n in ("INT7", "int8", "INTERACT1", "B"):
            self.drop(f"{n}.md", HDR.format(n) + "x")
        self.c.pass_once()
        self.assertEqual(sorted(self.fake.bg_names()), ["B", "INT7", "INTERACT1"])
        self.assertTrue((self.base / "inbox" / "int8.md").exists())
        self.report("INT7", "PASS")
        self.t += 60
        self.c.pass_once()
        self.assertIn("int8", self.fake.bg_names())

    def test_backlog_int_waits_for_running_int(self):
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        self.drop("i.md", HDR.format("INT1") + "x")
        self.c.pass_once()
        self.backlog({"id": "INT-2"}, {"id": "S"})
        self.c.pass_once()
        self.assertEqual(sorted(self.fake.bg_names()), ["INT1", "S"])
        self.assertEqual(self.view()["INT-2"], "int_busy")


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
        self.assertEqual((body["model"], body["max_tokens"], body["output_config"]),
                         ("claude-haiku-5-5", 300, {"effort": "low"}))
        self.assertNotIn("temperature", body)
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

    def test_fenced_json_accepted(self):
        self.reply = answer("```json\n" + json.dumps({"kind": "pass", "summary": "s", "next": "n"}) + "\n```")
        self.finish_a()
        self.assertEqual(self.lane_a()["reading"]["kind"], "pass")

    def test_prose_wrapped_single_object_accepted(self):
        self.reply = answer('Here: {"kind": "blocked", "summary": "s {x}", "next": "n"} done.')
        self.finish_a()
        self.assertEqual(self.lane_a()["reading"]["kind"], "blocked")

    def test_two_objects_or_bad_fields_need_orchestrator(self):
        ok = '{"kind": "pass", "summary": "s", "next": "n"}'
        for text in (ok + " " + ok, '{"kind": "nope", "summary": "s", "next": "n"}',
                     '{"kind": "pass", "summary": "s", "next": "n", "extra": 1}'):
            with self.subTest(text=text[:40]):
                self.setUp()
                self.reply = answer(text)
                self.finish_a()
                self.assertEqual(self.lane_a()["reading"]["summary"], "reader: invalid output")
        # over 140 characters is the model miscounting, not a bad answer: clipped (CONV2)
        self.setUp()
        self.reply = answer('{"kind": "pass", "summary": "' + "x" * 141 + '", "next": "n"}')
        self.finish_a()
        self.assertEqual(self.lane_a()["reading"]["summary"], "x" * 139 + "…")

    def test_real_failure_shapes_accepted(self):
        """Shapes of the 2026-10-09 'reader: invalid output' answers (AGY0 and AGY0B: same
        report, same 164 output tokens, one accepted): a Turkish field over 140 characters,
        and the near-misses a well-formed answer can take."""
        long_tr = ("Lane düşürüldü: effort=low başlığı jarvis-flow lint tarafından kabul edilmedi, "
                   "oturum boş kaldı ve iki slotu kırk dakika tuttu; AGY0C ile değiştirildi.")
        self.assertGreater(len(long_tr), 140)
        shapes = {
            "long summary": json.dumps({"kind": "needs_orchestrator", "summary": long_tr,
                                        "next": "AGY0C'yi izle"}, ensure_ascii=False),
            "long next": json.dumps({"kind": "blocked", "summary": "AGY1 BLOCKED",
                                     "next": long_tr}, ensure_ascii=False),
            "raw newline in string": '{"kind": "pass", "summary": "G6K3 PASS\nreview PASS", "next": "entegre et"}',
            "JSON fence label upper": '```JSON\n{"kind": "pass", "summary": "s", "next": "n"}\n```',
            "prose then object": 'Okuma:\n{"kind": "blocked", "summary": "INT6 BLOCKED: CI {tests} kırmızı", '
                                 '"next": "CI\'yi yeniden koş"}',
        }
        for label, text in shapes.items():
            with self.subTest(label):
                r = conveyor.parse_reading(answer(text)[1])
                self.assertIsNotNone(r, label)
                self.assertTrue(all(0 < len(r[k]) <= 140 for k in ("summary", "next")), label)
        r = conveyor.parse_reading(answer(shapes["long summary"])[1])
        self.assertEqual(r["summary"], long_tr[:139] + "…")
        self.assertEqual(r["kind"], "needs_orchestrator")

    def test_invalid_output_logs_redacted_snippet(self):
        raw = ('{"kind": "pass", "summary": "s", "next": "n"} ' * 2 + KEY + " sk-ant-api03-OTHER_key-x "
               + "ntfy-Topic_77 " + "y" * 500)
        self.reply = (200, {**answer(raw)[1], "stop_reason": "max_tokens"})
        with mock.patch.dict(os.environ, {"JARVIS_NTFY_TOPIC": "ntfy-Topic_77"}):
            self.finish_a()
        self.assertNotIn("ntfy-Topic_77", json.dumps(self.events()))
        (e,) = [e for e in self.events() if e["event"] == "reader"]
        self.assertEqual(e["reading"]["summary"], "reader: invalid output")
        self.assertEqual(e["stop_reason"], "max_tokens")
        self.assertLessEqual(len(e["raw"]), 300)
        self.assertTrue(e["raw"].startswith('{"kind": "pass"'))
        self.assertNotIn(KEY, json.dumps(self.events()))
        self.assertNotIn("sk-ant-", e["raw"])
        self.assertNotIn("raw", self.lane_a()["reading"])  # status.json carries no model text
        ok = [x for x in self.events() if x["event"] == "reader" and "raw" in x]
        self.assertEqual(len(ok), 1)

    def test_pass_error_logs_filename_and_location(self):
        def boom():
            raise FileNotFoundError(2, "No such file", "C:/x/gone.md")
        self.c.pass_once = boom
        self.c.loop(once=True)
        (e,) = [x for x in self.events() if x["event"] == "error"]
        self.assertEqual(e["file"], "C:/x/gone.md")
        self.assertRegex(e["at"], r"^conveyor\.py:\d+$")  # innermost conveyor frame (loop's call)

    def test_inbox_file_vanishing_before_stat_is_skipped(self):
        self.drop("a.md", HDR.format("A"))
        real = Path.glob
        ghost = self.base / "inbox" / "ghost.md"

        def glob(path, pat):
            out = list(real(path, pat))
            return [ghost] + out if path == self.base / "inbox" and pat == "*.md" else out
        with mock.patch.object(Path, "glob", glob):
            self.c.pass_once()
        self.assertEqual(len(self.fake.bg_calls()), 1)

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
                    {**ok, "next": ""}, {**ok, "next": "  "}, {**ok, "summary": 5}, [ok], "nope"]:
            self.assertIsNone(conveyor.parse_reading(answer(bad)[1]), bad)
        self.assertEqual(conveyor.parse_reading(answer({**ok, "summary": "x" * 141})[1])["summary"],
                         "x" * 139 + "…")  # clipped, not rejected (CONV2)
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

    def test_api_error_type_and_message_recorded(self):
        msg = "temperature is not supported " + "x" * 300 + KEY
        self.reply = (400, {"type": "error", "error": {"type": "invalid_request_error", "message": msg}})
        self.finish_a()
        (e,) = [e for e in self.events() if e["event"] == "reader_error"]
        self.assertEqual((e["lane"], e["status"], e["error_type"]), ("A", 400, "invalid_request_error"))
        self.assertEqual(e["error_message"], msg[:200])
        self.assertNotIn(KEY, json.dumps(self.events()))
        self.assertNotIn("temperature", self.posts[0])

    def test_anthropic_post_returns_error_body(self):
        import io
        http = conveyor.urllib.error.HTTPError(conveyor.READER_URL, 400, "bad", {}, io.BytesIO(b'{"error": {"type": "t"}}'))
        with mock.patch.object(conveyor.urllib.request, "urlopen", side_effect=http):
            self.assertEqual(conveyor.anthropic_post(KEY, {}), (400, {"error": {"type": "t"}}))

    def test_anthropic_post_request_and_errors(self):
        with mock.patch.object(conveyor.urllib.request, "urlopen", side_effect=TimeoutError()) as op:
            self.assertEqual(conveyor.anthropic_post(KEY, {"x": 1}), ("TimeoutError", None))
        req = op.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_header("X-api-key"), KEY)
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(op.call_args[1]["timeout"], 60)
        self.assertEqual(conveyor.READER_TIMEOUT_S, 60)
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

    def test_needs_salih_is_attention_not_a_page(self):
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
        self.assertEqual(self.pushes, [])  # the reader never pages Salih directly
        self.assertEqual(self.attn(), ["A", "B", "C", conveyor.DRY_ID])
        reasons = {i["id"]: i["reason"] for i in self.status()["attention"]}
        self.assertEqual(reasons["C"], "reader says needs_salih: Salih karar vermeli")
        self.assertEqual(reasons["A"], "reader blocked: engel_bir")

    def test_needs_salih_paged_once_by_escalation(self):
        self.reply = answer({"kind": "needs_salih", "summary": "karar", "next": "n"})
        self.finish_a()
        self.assertEqual(self.pushes, [])
        self.t += conveyor.ATTENTION_PAGE_S + 3600
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)  # A and the dry item escalate together
        self.assertIn("A: reader says needs_salih: karar", self.pushes[0])
        self.t += conveyor.ATTENTION_PAGE_S + 3600
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)  # once per item

    def test_needs_salih_ack_prevents_escalation(self):
        self.reply = answer({"kind": "needs_salih", "summary": "karar", "next": "n"})
        self.finish_a()
        ack = self.base / "ack"
        ack.mkdir()
        (ack / "A").write_text("")
        os.utime(ack / "A", (self.t + 5, self.t + 5))
        self.t += conveyor.ATTENTION_PAGE_S + 3600
        self.c.pass_once()
        self.assertNotIn("A:", "\n".join(self.pushes))
        self.assertNotIn("A", self.attn())

    def test_needs_salih_reading_does_not_page_inside_gap(self):
        self.c.page("earlier")
        self.c.flush_pages()
        self.reply = answer({"kind": "needs_salih", "summary": "karar", "next": "n"})
        self.finish_a()
        self.t += conveyor.PAGE_GAP_S
        self.c.pass_once()
        self.assertEqual(self.pushes, ["earlier"])  # no reader page, even after the gap

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


class TestCleanup(Base):
    """Finished lane sessions are stopped and `claude rm`ed after CLEANUP_S, usage counted first."""

    def setUp(self):
        super().setUp()
        self.ledger_runs = 0
        self.ledger_error = None
        self.ledger_skip = set()  # session ids the fake ledger does not count
        self.c.usage_ledger = self.fake_ledger
        self.ledger = self.root / "build" / "ledger" / "usage.json"
        self.c.stop_finished = lambda *a: None  # these cover the rm path with a live session (TestEarlyStop)

    def fake_ledger(self):
        self.ledger_runs += 1
        if self.ledger_error:
            raise self.ledger_error
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps({
            "generated_at": conveyor.datetime.fromtimestamp(self.t).astimezone().isoformat(),
            "sessions": [{"session_id": a["sessionId"]} for a in self.fake.agents
                         if a.get("sessionId") and a["sessionId"] not in self.ledger_skip]}))

    def finished(self, name="A", status="PASS", agent=None):
        self.drop(f"{name}.md", HDR.format(name))
        self.c.pass_once()
        a = self.fake.agent(name)
        a.update({"state": "done", "status": "idle", **(agent or {})})
        self.report(name, status)
        self.c.pass_once()
        self.assertTrue(self.lanes()[name]["finished"])
        return a["id"]

    def cli(self, *cmds):
        return [c for c in self.fake.calls if c[0] in cmds]

    def test_stop_then_rm_after_grace_only_lane_sessions(self):
        self.fake.agents += [  # Salih's / the orchestrator's sessions: never in lanes.json
            {"id": "ffff0001", "name": "A", "kind": "background", "state": "done", "status": "idle",
             "pid": 1, "sessionId": "ffff0001-x"},
            {"id": "ffff0002", "name": "orchestrator", "kind": "interactive", "state": "done",
             "sessionId": "ffff0002-x"}]
        sid = self.finished()
        self.t += conveyor.CLEANUP_S - 1
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.t += 1
        self.c.pass_once()
        self.assertEqual(self.ledger_runs, 1)  # counted after the grace, before the rm
        self.assertEqual(self.cli("stop", "rm"), [["stop", sid], ["rm", sid]])
        ev = [e for e in self.events() if e["event"] == "cleanup"]
        self.assertEqual((len(ev), ev[0]["lane"], ev[0]["result"]), (1, "A", "removed"))
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")
        self.c.pass_once()
        self.assertEqual(len(self.cli("stop", "rm")), 2)
        self.assertEqual(sorted(a["id"] for a in self.fake.agents), ["ffff0001", "ffff0002"])

    def test_fresh_ledger_is_not_rerun_and_stopped_session_only_removed(self):
        sid = self.finished()
        self.fake.agent("A").pop("pid")
        self.t += conveyor.CLEANUP_S
        self.fake_ledger()
        self.c.pass_once()
        self.assertEqual(self.ledger_runs, 1)
        self.assertEqual(self.cli("stop", "rm"), [["rm", sid]])

    def test_doubt_leaves_session_and_logs_once(self):
        for st in ({"state": "working", "status": "busy"}, {"state": "mystery"},
                   {"name": "renamed"}, {"sessionId": None}, {"kind": "interactive"}):
            with self.subTest(st=st):
                self.tearDown()
                self.setUp()
                self.finished(agent=st)
                self.t += conveyor.CLEANUP_S
                self.c.pass_once()
                self.c.pass_once()
                self.assertEqual(self.cli("stop", "rm"), [])
                self.assertEqual(len([e for e in self.events() if e["event"] == "cleanup_skip"]), 1)

    def test_usage_not_counted_leaves_session(self):
        sid = self.finished()
        self.ledger_skip.add(self.fake.agent("A")["sessionId"])
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.ledger_skip.clear()
        self.ledger_error = OSError("disk")
        self.t += 60
        self.c.pass_once()  # within USAGE_S: the ledger is not re-run every pass
        self.assertEqual(self.ledger_runs, 1)
        self.t += conveyor.USAGE_S
        self.c.pass_once()
        self.assertEqual((self.ledger_runs, self.cli("stop", "rm")), (2, []))
        self.assertIn("usage ledger", self.status()["last_error"])
        self.ledger_error = None
        self.c.pass_once()
        self.assertEqual(self.ledger_runs, 2)
        self.t += conveyor.USAGE_S
        self.c.pass_once()
        self.assertEqual(self.cli("rm"), [["rm", sid]])

    def test_cli_error_leaves_session_for_good(self):
        sid = self.finished()
        self.fake.rm_rc = 1
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        self.c.pass_once()
        self.assertEqual(self.cli("rm"), [["rm", sid]])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "error")
        self.assertIn(sid, [a["id"] for a in self.fake.agents])

    def test_fresh_listing_busy_or_timeout_leaves_session(self):
        sid = self.finished()
        self.t += conveyor.CLEANUP_S
        run = self.c.run
        listings = []

        def reattached(args, timeout=120):  # Salih attaches between the pass's listing and the rm
            out = run(args, timeout)
            if args[0] == "agents":
                listings.append(args)
                if len(listings) == 2:  # the fresh listing right before stop/rm
                    return 0, json.dumps([{**a, "status": "busy"} for a in self.fake.agents])
            return out
        self.c.run = reattached
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.assertEqual([e["reason"] for e in self.events() if e["event"] == "cleanup_skip"],
                         [f"{sid}: kind/name/state/session not as expected"])

        def timeout(args, timeout=120):
            if args[0] == "stop":
                raise conveyor.subprocess.TimeoutExpired(args, timeout)
            return run(args, timeout)
        self.c.run = timeout
        self.c.pass_once()
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "error")
        self.assertIn(sid, [a["id"] for a in self.fake.agents])

    def test_same_name_redispatch_keeps_old_session_for_cleanup(self):
        old = self.finished()
        self.drop("a2.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.lanes()["A"]["prev_ids"], [old])
        self.assertNotEqual(self.lanes()["A"]["id"], old)

    def test_unreported_or_unacked_lane_is_kept(self):
        b = self.finished("B", "BLOCKED")  # in attention until the orchestrator acks it
        self.drop("c.md", HDR.format("C"))
        self.c.pass_once()
        self.fake.agent("C").update(state="done", status="idle")  # finished without a report
        self.c.pass_once()
        self.t += conveyor.CLEANUP_S * 2
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        (self.base / "ack").mkdir()
        (self.base / "ack" / "B").touch()
        os.utime(self.base / "ack" / "B", (self.t, self.t))
        self.c.pass_once()
        self.assertEqual([c[1] for c in self.cli("rm")], [b])

    def test_lane_finished_before_finished_ts_uses_report_mtime(self):
        rep = self.report("OLD", "PASS", where=None, mtime=self.t - conveyor.CLEANUP_S)
        self.fake.agents.append({"id": "0000aaaa", "name": "OLD", "kind": "background", "state": "done",
                                 "sessionId": "0000aaaa-x"})
        (self.base / "lanes.json").write_text(json.dumps({"OLD": {
            "id": "0000aaaa", "model": "opus", "effort": "high", "started_at": conveyor.iso(self.t - 9000),
            "state": "done", "finished": True, "status": "PASS", "report": str(rep)}}))
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [["rm", "0000aaaa"]])

    def test_at_most_per_pass(self):
        lanes = {}
        for i in range(conveyor.CLEANUP_PER_PASS + 1):
            rep = self.report(f"L{i}", "PASS", where=None, mtime=self.t - conveyor.CLEANUP_S)
            self.fake.agents.append({"id": f"0000000{i}", "name": f"L{i}", "kind": "background",
                                     "state": "done", "sessionId": f"0000000{i}-x"})
            lanes[f"L{i}"] = {"id": f"0000000{i}", "finished": True, "status": "PASS", "report": str(rep),
                              "started_at": conveyor.iso(self.t - 9000)}
        (self.base / "lanes.json").write_text(json.dumps(lanes))
        self.c.pass_once()
        self.assertEqual(len(self.cli("rm")), conveyor.CLEANUP_PER_PASS)
        self.c.pass_once()
        self.assertEqual(len(self.cli("rm")), conveyor.CLEANUP_PER_PASS + 1)

    def test_ledger_prefers_scripts_copy(self):
        c = Conveyor(self.root, self.fake, now=lambda: self.t, cmd=self.git, disk=self.fake_disk,
                     projects=self.root / "projects", mem=lambda: self.memory,
                     idle=lambda: self.idle, procs=self.procs)
        mod = "from pathlib import Path\ndef main(out):\n    Path(out).parent.mkdir(parents=True, exist_ok=True)\n" \
              "    Path(out).write_text('{}')\n"
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "usage_ledger.py").write_text(mod.replace("'{}'", "'scripts'"))
        (self.base / "usage_ledger.py").write_text(mod.replace("'{}'", "'build'"))
        c.usage_ledger()
        self.assertEqual(self.ledger.read_text(), "scripts")
        (self.root / "scripts" / "usage_ledger.py").unlink()
        c.usage_ledger()
        self.assertEqual(self.ledger.read_text(), "build")


class TestEarlyStop(Base):
    """A finished lane's live session is `claude stop`ped at once (memory); rm stays at CLEANUP_S."""

    fake_ledger, finished, cli = TestCleanup.fake_ledger, TestCleanup.finished, TestCleanup.cli

    def setUp(self):  # as TestCleanup, early stop left on
        super().setUp()
        self.ledger_runs, self.ledger_error, self.ledger_skip = 0, None, set()
        self.c.usage_ledger = self.fake_ledger
        self.ledger = self.root / "build" / "ledger" / "usage.json"

    def test_finished_stopped_once_then_rm_path_unchanged(self):
        sid = self.finished()
        self.assertEqual(self.cli("stop", "rm"), [["stop", sid]])
        self.assertTrue(self.lanes()["A"]["stopped"]["ok"])
        for _ in range(3):
            self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [["stop", sid]])
        self.assertEqual(len([e for e in self.events() if e["event"] == "early_stop"]), 1)
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [["stop", sid], ["rm", sid]])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")

    def stopped_state(self, sid):
        self.fake.agent("A").update(state="stopped")  # what claude reports after the stop
        self.assertEqual(self.lanes()["A"]["early_stopped"], [sid])

    def test_early_stopped_by_us_removed_after_cleanup_s(self):
        sid = self.finished()
        self.stopped_state(sid)
        self.t += conveyor.CLEANUP_S - 1
        self.c.pass_once()
        self.assertEqual(self.cli("rm"), [])
        self.t += 1
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [["stop", sid], ["rm", sid]])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")

    def test_stopped_not_by_us_is_doubt(self):
        sid = self.finished()
        self.lanes_edit(lambda d: d["A"].pop("early_stopped"))
        self.fake.agent("A").update(state="stopped")
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        self.assertEqual(self.cli("rm"), [])
        self.assertEqual([e["reason"] for e in self.events() if e["event"] == "cleanup_skip"],
                         [f"{sid}: kind/name/state/session not as expected"])

    def test_early_stopped_in_attention_or_uncounted_kept(self):
        sid = self.finished()
        self.stopped_state(sid)
        self.ledger_skip.add(self.fake.agent("A")["sessionId"])
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        self.assertEqual(self.cli("rm"), [])
        b = self.finished("B", "BLOCKED")  # in attention until acked
        self.fake.agent("B").update(state="stopped")
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        self.assertNotIn(["rm", b], self.fake.calls)

    def lanes_edit(self, fn):
        d = self.lanes()
        fn(d)
        (self.base / "lanes.json").write_text(json.dumps(d))

    def test_busy_attention_foreign_never_stopped(self):
        self.fake.agents.append(  # not started by the conveyor: same lane name, alive, idle
            {"id": "ffff0001", "name": "A", "kind": "background", "state": "done", "status": "idle",
             "pid": 1, "sessionId": "ffff0001-x"})
        self.finished(agent={"status": "busy"})
        self.finished("B", "BLOCKED")  # in attention
        self.finished("C", agent={"state": "working"})
        for _ in range(3):
            self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.assertNotIn("stopped", self.lanes()["A"])
        self.fake.agent("A").update(status="idle")  # no longer busy: stopped now, foreign one never
        self.c.pass_once()
        self.assertEqual(self.cli("stop"), [["stop", self.lanes()["A"]["id"]]])
        self.assertIn("pid", next(a for a in self.fake.agents if a["id"] == "ffff0001"))

    def test_fresh_listing_busy_not_stopped(self):
        run = self.c.run
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.fake.agent("A").update(state="done", status="idle")
        self.report("A", "PASS")
        calls = []

        def reattached(args, timeout=120):  # Salih attaches between the pass's listing and the stop
            out = run(args, timeout)
            if args[0] == "agents":
                calls.append(args)
                if len(calls) == 2:
                    return 0, json.dumps([{**a, "status": "busy"} for a in self.fake.agents])
            return out
        self.c.run = reattached
        self.c.pass_once()
        self.assertTrue(self.lanes()["A"]["finished"])
        self.assertEqual(self.cli("stop"), [])
        self.assertNotIn("stopped", self.lanes()["A"])

    def test_stop_failure_logged_not_fatal_not_retried(self):
        run = self.c.run

        def timeout(args, timeout=120):
            if args[0] == "stop":
                raise conveyor.subprocess.TimeoutExpired(args, timeout)
            return run(args, timeout)
        self.c.run = timeout
        self.finished()
        self.c.pass_once()
        self.assertFalse(self.lanes()["A"]["stopped"]["ok"])
        (ev,) = [e for e in self.events() if e["event"] == "early_stop"]
        self.assertEqual(ev["steps"][0]["rc"], "TimeoutExpired")
        self.assertEqual(self.c.last_error, None)


class TestPushGate(Base):
    def approve(self, name=f"push-{MAIN}-p2b"):
        d = self.root / "build" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("integration report read, ok\n", encoding="utf-8")
        return d / name

    def assert_blocked(self, name, why):
        self.c.pass_once()
        self.assertEqual(self.git.pushes(), [])
        self.assertTrue((self.root / "build" / "approvals" / name).is_file())
        (ev,) = [e for e in self.events() if e["event"] == "push_blocked"]
        self.assertEqual(ev["approval"], name)
        self.assertIn(why, ev["reason"])
        self.assertIn(name, self.attn())
        self.assertFalse([e for e in self.events() if e["event"] == "push"])

    def test_happy_path_pushes_exactly_main_and_moves_approval(self):
        f = self.approve()
        self.c.pass_once()
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{MAIN}:refs/heads/main"]])
        self.assertFalse(any("--force" in a or "-f" == a for c in self.git.calls for a in c))
        self.assertIn(["gh", "run", "list", "--branch", "ci/p2b", "--limit", "50", "--json",
                       "headSha,conclusion,status,workflowName,url"], self.git.calls)
        self.assertFalse(f.exists())
        self.assertTrue((f.parent / "done" / f.name).is_file())
        (ev,) = [e for e in self.events() if e["event"] == "push"]
        self.assertEqual(ev["sha"], MAIN)
        self.assertEqual(ev["ci_run"], "https://github.com/x/y/actions/runs/1")
        self.assertEqual(self.pushes, ["main 1234567 GitHub'a gönderildi"])
        self.assertEqual(self.attn_ex_dry(), [])
        self.c.pass_once()  # moved: never pushed twice
        self.assertEqual(len(self.git.pushes()), 1)

    def test_short_sha_and_ci_branch_by_contains(self):
        self.approve("push-1234567")
        self.c.pass_once()
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{MAIN}:refs/heads/main"]])
        self.assertIn(["git", "branch", "-r", "--contains", MAIN], self.git.calls)

    def test_ntfy_respects_page_gap(self):
        self.c.pager_flag("last_push", self.t - 60)
        self.approve()
        self.c.pass_once()
        self.assertEqual(len(self.git.pushes()), 1)
        self.assertEqual(self.pushes, [])  # held for the gap
        self.t += conveyor.PAGE_GAP_S
        self.c.pass_once()
        self.assertEqual(self.pushes, ["main 1234567 GitHub'a gönderildi"])

    def test_no_approval_no_git(self):
        self.c.pass_once()
        self.assertEqual(self.git.calls, [])
        d = self.root / "build" / "approvals"
        (d / "done").mkdir(parents=True)
        (d / "done" / f"push-{MAIN}").write_text("old")
        (d / f"notes-{MAIN}").write_text("x")
        self.c.pass_once()
        self.assertEqual(self.git.calls, [])

    def test_bad_approval_name(self):
        self.approve("push-xyz1234")
        self.assert_blocked("push-xyz1234", "approval name")
        self.assertEqual(self.git.calls, [])

    def test_local_main_mismatch(self):
        self.approve()
        self.git.main = "c" * 40
        self.assert_blocked(f"push-{MAIN}-p2b", "local main")

    def test_local_main_unreadable(self):
        self.approve()
        self.git.fail[("git", "rev-parse", "--verify", "refs/heads/main")] = (128, "fatal")
        self.assert_blocked(f"push-{MAIN}-p2b", "local main")

    def test_fetch_fails(self):
        self.approve()
        self.git.fail[("git", "fetch")] = (1, "network")
        self.assert_blocked(f"push-{MAIN}-p2b", "git fetch")

    def test_origin_already_there(self):
        self.approve()
        self.git.origin = MAIN
        self.assert_blocked(f"push-{MAIN}-p2b", "already on origin/main")

    def test_not_fast_forward(self):
        self.approve()
        self.git.fail[("git", "merge-base")] = (1, "")
        self.assert_blocked(f"push-{MAIN}-p2b", "not a fast-forward")

    def test_uv_lock_blocks(self):
        for diff in ("uv.lock\n", "jarvis/a.py\nsub/uv.lock\n"):
            with self.subTest(diff=diff):
                self.tearDown()
                self.setUp()
                self.approve()
                self.git.diff = diff
                self.assert_blocked(f"push-{MAIN}-p2b", "uv.lock")

    def test_schema_version_changed_blocks(self):
        for rev, text in ((MAIN, "SCHEMA_VERSION = 11\n"), (MAIN, None), (ORIGIN, "X = 1\n"),
                          (MAIN, SCHEMA + "SCHEMA_VERSION = 11\n")):
            with self.subTest(rev=rev, text=text):
                self.tearDown()
                self.setUp()
                self.approve()
                self.git.memory[rev] = text
                self.assert_blocked(f"push-{MAIN}-p2b", "SCHEMA_VERSION = 10")

    def test_ci_not_success_or_other_sha_blocks(self):
        run = self.git.runs[0]
        for change in ({"conclusion": "failure"}, {"status": "in_progress", "conclusion": ""},
                       {"headSha": "d" * 40}, {"workflowName": "other"}):
            with self.subTest(change=change):
                self.tearDown()
                self.setUp()
                self.approve()
                self.git.runs = [{**run, **change}]
                self.assert_blocked(f"push-{MAIN}-p2b", "CI tests on ci/p2b is not success")

    def test_newest_run_failed_after_older_success(self):
        self.approve()
        self.git.runs = [{**self.git.runs[0], "conclusion": "failure"}, self.git.runs[0]]
        self.assert_blocked(f"push-{MAIN}-p2b", "is not success")

    def test_gh_fails_or_garbage(self):
        for ans in ((1, "auth"), (0, "not json"), (0, "{}")):
            with self.subTest(ans=ans):
                self.tearDown()
                self.setUp()
                self.approve()
                self.git.fail[("gh",)] = ans
                self.assert_blocked(f"push-{MAIN}-p2b", "gh run list ci/p2b")

    def test_ci_branch_not_unique(self):
        for contains in ("  origin/feature\n", "  origin/ci/a\n  origin/ci/b\n"):
            with self.subTest(contains=contains):
                self.tearDown()
                self.setUp()
                self.approve("push-1234567")
                self.git.contains = contains
                self.assert_blocked("push-1234567", "name the approval push-<sha>-<ci>")

    def test_check_exception_blocks(self):
        self.approve()
        self.git.raise_on = ("git", "fetch")
        self.assert_blocked(f"push-{MAIN}-p2b", "check error: TimeoutError")

    def test_push_failure_blocks(self):
        f = self.approve()
        self.git.fail[("git", "push")] = (1, "rejected")
        self.c.pass_once()
        self.assertEqual(len(self.git.pushes()), 1)
        self.assertTrue(f.is_file())
        (ev,) = [e for e in self.events() if e["event"] == "push_blocked"]
        self.assertIn("git push rc=1", ev["reason"])
        self.assertFalse([e for e in self.events() if e["event"] == "push"])

    def test_retry_at_most_once_per_10_min(self):
        f = self.approve()
        self.git.fail[("git", "fetch")] = (1, "network")
        self.c.pass_once()
        n = len(self.git.calls)
        self.t += conveyor.PUSH_RETRY_S - 1
        self.c.pass_once()
        self.assertEqual(len(self.git.calls), n)
        self.assertIn(f.name, self.attn())
        since = self.status()["attention"][0]["since"]
        self.t += 1
        self.git.fail.clear()
        self.c.pass_once()
        self.assertEqual(len(self.git.pushes()), 1)
        self.assertEqual(self.attn_ex_dry(), [])
        self.assertEqual(len([e for e in self.events() if e["event"] == "push_blocked"]), 1)
        self.assertTrue(since)

    def test_blocked_item_acked_and_gone_with_approval(self):
        f = self.approve()
        self.git.main = "c" * 40
        self.c.pass_once()
        (self.base / "ack").mkdir()
        (self.base / "ack" / f.name).touch()
        self.c.pass_once()
        self.assertEqual(self.attn_ex_dry(), [])
        f.unlink()
        self.c.pass_once()
        self.assertEqual(json.loads((self.base / "push.json").read_text()), {})


START = "c" * 40  # local main when the integration lane started (FakeGit's MAIN is its new main)
INT_HDR = "@jarvis lane={} model=sonnet effort=medium base=main\n"
GREEN = f"NEW MAIN SHA: {MAIN} (local, not pushed)\nCI: https://github.com/x/y/actions/runs/1 success, branch ci/p2b\n"
FAILING = "tests.test_conversation_attach.AttachWindowTests.test_e_entry_only"
PICKED = ("git", "diff", "--name-only", f"{START}..{MAIN}")  # the files of the commits INT9 picked


class AutopilotBase(Base):
    def int_lane(self, name="INT9", body="INTEGRATE picks a, b\n"):
        """Dispatch integration lane `name` from the inbox while local main is START."""
        self.git.main = START
        self.drop(f"{name}.md", INT_HDR.format(name) + body)
        self.c.pass_once()
        self.git.main = MAIN
        self.assertIn(name, self.fake.bg_names())
        self.assertEqual(self.lanes()[name]["main_at_start"], START)
        self.t += 60

    def finish(self, name, status, extra=""):
        self.report(name, status, extra=extra, mtime=self.t)
        self.c.pass_once()
        self.t += 60

    def approvals(self):
        d = self.root / "build" / "approvals"
        return sorted(p.relative_to(d).as_posix() for p in d.rglob("push-*")) if d.exists() else []

    def ev(self, kind):
        return [e for e in self.events() if e["event"] == kind]


class TestAutoApproval(AutopilotBase):
    def test_happy_path_writes_key1_and_the_gate_pushes(self):
        self.int_lane()
        self.finish("INT9", "GREEN", GREEN)
        name = f"push-{MAIN}-p2b"
        self.assertEqual(self.approvals(), [f"done/{name}"])
        self.assertTrue((self.root / "build" / "approvals" / "done" / name).read_text(
            encoding="utf-8").startswith("auto-approved by the conveyor"))
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{MAIN}:refs/heads/main"]])
        (ev,) = self.ev("auto_approve")
        self.assertEqual((ev["lane"], ev["approval"], ev["ci_run"]),
                         ("INT9", name, "https://github.com/x/y/actions/runs/1"))
        self.assertEqual(self.status()["band"], ["auto-approved 1234567"])
        self.assertEqual(self.attn_ex_dry(), [])
        self.t += conveyor.AUTO_LINE_S
        self.c.pass_once()
        self.assertEqual(self.status()["band"], [])

    def test_existing_orchestrator_key_is_kept(self):
        self.int_lane()
        d = self.root / "build" / "approvals"
        d.mkdir(parents=True)
        (d / f"push-{MAIN}-p2b").write_text("orchestrator\n", encoding="utf-8")
        self.finish("INT9", "GREEN", GREEN)
        self.assertEqual((d / "done" / f"push-{MAIN}-p2b").read_text(encoding="utf-8"), "orchestrator\n")
        self.assertEqual(len(self.git.pushes()), 1)

    def test_each_refusal_reason(self):
        cases = {
            "status not exactly GREEN": ("GREEN (CI red)", GREEN, None, "single 'STATUS: GREEN'"),
            "two STATUS lines": ("GREEN", GREEN + "STATUS: BLOCKED\n", None, "single 'STATUS: GREEN'"),
            "short sha": ("GREEN", GREEN.replace(MAIN, MAIN[:7]), None, "NEW MAIN SHA: <40 hex>"),
            "no sha line": ("GREEN", GREEN.replace("NEW MAIN SHA", "NEW MAIN"), None, "NEW MAIN SHA"),
            "two sha lines": ("GREEN", GREEN + f"NEW MAIN SHA: {START}\n", None, "NEW MAIN SHA"),
            "uppercase sha": ("GREEN", GREEN.replace(MAIN, MAIN.upper()), None, "NEW MAIN SHA"),
            "no ci branch": ("GREEN", GREEN.replace("ci/p2b", "x"), None, "0 ci/* branches"),
            "two ci branches": ("GREEN", GREEN + "also ci/other\n", None, "2 ci/* branches"),
            "ci branch with a slash": ("GREEN", GREEN.replace("ci/p2b", "ci/p2b/x"), None, "0 ci/* branches"),
            "no CI url": ("GREEN", GREEN.replace("https://", "see "), None, "0 CI run URLs"),
            "two CI urls": ("GREEN", GREEN + "https://github.com/x/y/actions/runs/2\n", None, "2 CI run URLs"),
            "local main moved": ("GREEN", GREEN, lambda g: setattr(g, "main", "d" * 40), "local main"),
            "CI not success": ("GREEN", GREEN, lambda g: g.runs[0].update(conclusion="failure"),
                               "CI tests on ci/p2b is not success"),
            "CI pending": ("GREEN", GREEN, lambda g: g.runs[0].update(status="in_progress", conclusion=""),
                           "is not success"),
            "uv.lock": ("GREEN", GREEN, lambda g: setattr(g, "diff", "uv.lock\n"), "uv.lock changed"),
            "schema changed": ("GREEN", GREEN, lambda g: g.memory.update({MAIN: "SCHEMA_VERSION = 11\n"}),
                               "SCHEMA_VERSION = 10"),
            "already pushed": ("GREEN", GREEN, lambda g: setattr(g, "origin", MAIN), "already on origin/main"),
            "other run verified": ("GREEN", GREEN.replace("runs/1", "runs/7"), None, "not the report's"),
            "git timeout": ("GREEN", GREEN, lambda g: setattr(g, "raise_on", ("git", "fetch")),
                            "check error: TimeoutError"),
        }
        for label, (status, extra, change, why) in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.int_lane()
                if change:
                    change(self.git)
                self.finish("INT9", status, extra)
                self.assertEqual(self.approvals(), [])
                self.assertEqual(self.git.pushes(), [])
                self.assertEqual(self.ev("auto_approve"), [])
                (ev,) = self.ev("auto_approve_refused")
                self.assertIn(why, ev["reason"])
                (item,) = [a for a in self.status()["attention"] if a["id"] == "INT9"]
                self.assertIn("auto-approval refused: ", item["reason"])

    def test_no_approval_while_another_int_lane_is_unfinished(self):
        self.int_lane()
        lanes = self.lanes()
        lanes["INT10"] = {"id": "0000beef", "model": "sonnet", "effort": "medium", "base": "main",
                          "started_at": conveyor.iso(self.t), "started_ts": self.t, "state": "working",
                          "prompt": "inbox/started/x.md", "files": []}
        (self.base / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
        self.finish("INT9", "GREEN", GREEN)
        self.assertEqual(self.approvals(), [])
        self.assertEqual(self.git.pushes(), [])
        (ev,) = self.ev("auto_approve_refused")
        self.assertEqual(ev["reason"], "integration lane INT10 is running")

    def test_only_green_integration_reports(self):
        for name, status in (("INT9", "PASS"), ("INT9", "OK"), ("G6X", "GREEN")):
            with self.subTest(name=name, status=status):
                self.tearDown()
                self.setUp()
                self.int_lane(name) if name.startswith("INT") else self.drop("g.md", HDR.format(name))
                self.c.pass_once()
                self.finish(name, status, GREEN)
                self.assertEqual(self.approvals(), [])
                self.assertEqual(self.ev("auto_approve") + self.ev("auto_approve_refused"), [])

    def test_lanes_finished_before_the_autopilot_are_left(self):
        self.int_lane()
        self.report("INT9", "GREEN", extra=GREEN)
        ap = json.loads((self.base / "autopilot.json").read_text())
        ap["since"] = self.t + 3600  # the autopilot began after this lane finished
        (self.base / "autopilot.json").write_text(json.dumps(ap))
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT9"]["status"], "GREEN")
        self.assertEqual(self.approvals(), [])

    def test_branch_at_sentence_end_is_taken(self):
        self.int_lane()
        self.finish("INT9", "GREEN", GREEN.replace("branch ci/p2b", "branch ci/p2b."))
        self.assertEqual(self.approvals(), [f"done/push-{MAIN}-p2b"])

    def test_approvals_path_not_a_folder_is_refused(self):
        self.int_lane()
        (self.root / "build" / "approvals").write_text("x", encoding="utf-8")
        self.finish("INT9", "GREEN", GREEN)
        self.assertEqual(self.git.pushes(), [])
        self.assertEqual(self.ev("auto_approve"), [])
        self.assertEqual(len(self.ev("auto_approve_refused")), 1)
        self.assertEqual(self.status()["band"], [])

    def test_fix_lane_by_name_never_gets_key1(self):
        self.int_lane("INT9-AFX")
        self.finish("INT9-AFX", "GREEN", GREEN)
        self.assertEqual(self.approvals(), [])
        self.assertEqual(self.ev("auto_approve") + self.ev("auto_approve_refused"), [])

    def test_malformed_state_fails_closed(self):
        for bad in ({"since": 1, "breaker": {"reason": "x"}}, {"since": 1, "bad": [5]},
                    {"since": 1, "chains": {"INT1": []}}, {"since": 1, "slices": [{"id": "X"}]},
                    {"since": "now"}, []):
            with self.subTest(bad=bad):
                self.tearDown()
                self.setUp()
                (self.base / "autopilot.json").write_text(json.dumps(bad), encoding="utf-8")
                self.int_lane()
                self.finish("INT9", "GREEN", GREEN)
                self.assertEqual(self.approvals(), [])
                self.assertEqual(self.status()["breaker"], "open")
                self.assertEqual(self.view(), {})

    def test_unreadable_state_fails_closed(self):
        (self.base / "autopilot.json").write_text("{broken", encoding="utf-8")
        self.int_lane()
        self.drop("p.md", HDR.format("P"))
        self.finish("INT9", "GREEN", GREEN)
        self.assertEqual(self.approvals(), [])
        self.assertNotIn("P", self.fake.bg_names())
        self.assertEqual(self.status()["breaker"], "open")
        self.assertEqual((self.base / "autopilot.json").read_text(encoding="utf-8"), "{broken")


class TestAutoFix(AutopilotBase):
    def setUp(self):
        super().setUp()
        self.git.fail[PICKED] = (0,"jarvis/core.py\nscripts/x.py\n")

    def blocked_int(self):
        self.int_lane()
        self.finish("INT9", "BLOCKED", f"FAILURE:\n  {FAILING}\n  line 367: assertFalse\n")

    def test_one_fix_lane_then_the_rerun(self):
        self.blocked_int()
        (ev,) = self.ev("auto_fix")
        self.assertEqual((ev["fix"], ev["rerun"], ev["files"]), ("INT9-AFX", "INT9-R", ["jarvis/core.py", "scripts/x.py"]))
        call = self.fake.bg_calls()[-1]
        self.assertEqual(call[:7], ["--bg", "--name", "INT9-AFX", "--model", "opus", "--effort", "high"])
        text = call[-1]
        self.assertTrue(text.startswith("@jarvis lane=INT9-AFX model=opus effort=high base=main\n"))
        for part in (FAILING, "  jarvis/core.py\n  scripts/x.py", f"CURRENT local main ({MAIN})",
                     "Never weaken", "INT9-R"):
            self.assertIn(part, text)
        tpl = self.base / "prompts" / "_autofix_template.md"
        self.assertEqual(tpl.read_text(encoding="utf-8"), conveyor.AUTOFIX_TEMPLATE)
        self.assertEqual(self.view()["INT9-R"], "waiting_deps")
        self.assertIn("auto-fix queued: INT9-AFX, then INT9-R", self.status()["attention"][0]["reason"])
        self.finish("INT9-AFX", "PASS", "FIX COMMIT: x\n")
        call = self.fake.bg_calls()[-1]
        self.assertEqual(call[:7], ["--bg", "--name", "INT9-R", "--model", "sonnet", "--effort", "medium"])
        self.assertTrue(call[-1].startswith("@jarvis lane=INT9-R model=sonnet effort=medium base=main\n"
                                            "INT9-R re-runs integration lane INT9"))
        self.assertTrue(call[-1].endswith("INTEGRATE picks a, b\n"))
        self.finish("INT9-R", "GREEN", GREEN)  # the re-run is an integration lane: auto key 1
        self.assertEqual(len(self.ev("auto_approve")), 1)
        self.assertEqual(self.status()["breaker"], "closed")

    def test_edited_template_is_used(self):
        (self.base / "prompts").mkdir()
        (self.base / "prompts" / "_autofix_template.md").write_text(
            "@jarvis lane=x model=opus effort=high base=main\nFIX {INT}: {TESTS} {{literal}}\n", encoding="utf-8")
        self.blocked_int()
        self.assertTrue(self.fake.bg_calls()[-1][-1].endswith(f"FIX INT9:   {FAILING} {{{{literal}}}}\n"))

    def test_never_a_second_attempt(self):
        self.blocked_int()
        self.finish("INT9-AFX", "PASS")
        self.finish("INT9-R", "BLOCKED", f"FAILURE: {FAILING}\n")
        self.assertEqual(len(self.ev("auto_fix")), 1)
        self.assertNotIn("INT9-R-AFX", self.fake.bg_names())
        self.assertEqual(self.status()["breaker"], "open")  # the chain failed
        # the orchestrator runs INT9 again by hand and it blocks again: attention only
        (self.base / "breaker-reset").touch()
        self.int_lane()
        self.finish("INT9", "BLOCKED", f"FAILURE: {FAILING}\n")
        self.assertEqual(len(self.ev("auto_fix")), 1)
        self.assertEqual(self.ev("auto_fix_skipped")[-1]["reason"], "auto-fix already used for this integration lane")
        self.assertEqual(self.fake.bg_names().count("INT9-AFX"), 1)

    def test_failed_fix_opens_the_breaker_and_the_rerun_never_runs(self):
        self.blocked_int()
        self.finish("INT9-AFX", "BLOCKED")
        self.c.pass_once()
        self.assertEqual(self.status()["breaker"], "open")
        self.assertIn(conveyor.BREAKER_ID, self.attn())
        self.assertIn("auto-fix chain failed: INT9-AFX BLOCKED", self.ev("breaker")[0]["reason"])
        self.assertNotIn("INT9-R", self.fake.bg_names())

    def test_skips(self):
        cases = {
            "no test id": ("FAILURE: shard 2 red\n", None, "names no failing test id"),
            "nothing picked": (f"FAILURE: {FAILING}\n",
                               lambda g: g.fail.update({PICKED: (0, "")}), "no picked files"),
            "git diff fails": (f"FAILURE: {FAILING}\n",
                               lambda g: g.fail.update({PICKED: (128, "bad")}), "rc=128"),
        }
        for label, (extra, change, why) in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.int_lane()
                if change:
                    change(self.git)
                self.finish("INT9", "BLOCKED", extra)
                self.assertIn(why, self.ev("auto_fix_skipped")[0]["reason"])
                self.assertEqual(self.ev("auto_fix"), [])
                self.assertNotIn("INT9-AFX", self.fake.bg_names())
                self.assertIn("auto-fix skipped", self.status()["attention"][0]["reason"])

    def test_unknown_start_main_skips(self):
        self.drop("i.md", INT_HDR.format("INT9"))
        self.git.fail[("git", "rev-parse", "--verify", "refs/heads/main")] = (128, "fatal")
        self.c.pass_once()
        del self.git.fail[("git", "rev-parse", "--verify", "refs/heads/main")]
        self.t += 60
        self.finish("INT9", "BLOCKED", f"FAILURE: {FAILING}\n")
        self.assertIn("unknown", self.ev("auto_fix_skipped")[0]["reason"])


class TestBreaker(AutopilotBase):
    def blocked(self, *names):
        for n in names:
            self.drop(f"{n}.md", HDR.format(n))
            self.c.pass_once()
            self.t += 60
            self.finish(n, "BLOCKED")

    def test_three_blocked_in_two_hours_holds_product_slices_only(self):
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        self.drop("r.md", HDR.format("RUN"))
        self.c.pass_once()
        self.blocked("A", "B")
        self.assertEqual(self.status()["breaker"], "closed")
        self.blocked("C")
        st = self.status()
        self.assertEqual((st["breaker"], st["band"]), ("open", ["breaker: open"]))
        self.assertIn(conveyor.BREAKER_ID, self.attn())
        self.assertIn("3 lanes finished BLOCKED/STOP/FAIL within 2 h: A, B, C", self.ev("breaker")[0]["reason"])
        n = len(self.fake.calls)
        self.drop("p.md", HDR.format("P"))
        self.backlog({"id": "S"}, {"id": "INT-B"})
        self.drop("i.md", HDR.format("INT11"))
        self.c.pass_once()
        self.assertTrue((self.base / "inbox" / "p.md").exists())
        self.assertNotIn("P", self.fake.bg_names())
        self.assertIn("INT11", self.fake.bg_names())  # integration lanes may still run
        self.assertEqual(self.view()["S"], "breaker_open")
        self.assertFalse([c for c in self.fake.calls[n:] if c[0] in ("stop", "rm")])  # running lanes untouched
        self.assertEqual(self.lanes()["RUN"]["state"], "working")
        # an ack hides the item but does not close the breaker; an old reset does not either
        (self.base / "ack").mkdir()
        (self.base / "ack" / conveyor.BREAKER_ID).touch()
        reset = self.base / "breaker-reset"
        reset.touch()
        os.utime(reset, (self.t - 7200, self.t - 7200))
        self.t += 60
        self.c.pass_once()
        self.assertEqual(self.status()["breaker"], "open")
        self.assertNotIn("P", self.fake.bg_names())
        os.utime(reset, (self.t, self.t))
        self.t += 60
        self.c.pass_once()
        self.assertEqual(self.status()["breaker"], "closed")
        self.assertIn("P", self.fake.bg_names())
        self.assertEqual([e["state"] for e in self.ev("breaker")], ["open", "closed"])

    def test_blocked_spread_over_more_than_two_hours(self):
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        self.blocked("A", "B")
        self.t += conveyor.BREAKER_WINDOW_S
        self.blocked("C")
        self.assertEqual(self.status()["breaker"], "closed")

    def test_reset_starts_a_new_count(self):
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        self.blocked("A", "B", "C")
        (self.base / "breaker-reset").touch()
        os.utime(self.base / "breaker-reset", (self.t, self.t))
        self.c.pass_once()
        self.assertEqual(self.status()["breaker"], "closed")
        self.blocked("D")
        self.assertEqual(self.status()["breaker"], "closed")


class TestNightQuiet(AutopilotBase):
    def test_escalation_waits_for_the_morning(self):
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.finish("A", "BLOCKED")
        self.hour = 3
        self.t += conveyor.ATTENTION_PAGE_S
        self.c.pass_once()
        self.t += conveyor.PAGE_GAP_S
        self.c.pass_once()
        self.assertEqual(self.pushes, [])
        self.hour = 8
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("A: STATUS BLOCKED", self.pushes[0])

    def test_breaker_open_pages_at_night(self):
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        self.hour = 3
        for n in "ABC":
            self.drop(f"{n}.md", HDR.format(n))
        self.c.pass_once()
        for n in "ABC":
            self.report(n, "BLOCKED")
        self.c.pass_once()
        self.assertEqual(self.status()["breaker"], "open")
        self.t += conveyor.ATTENTION_PAGE_S
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("breaker", self.pushes[0])


class TestDiskGuard(Base):
    def test_low_disk_holds_dispatch_and_releases(self):
        self.free = 5e9
        self.drop("a.md", HDR.format("A") + "x")
        self.c.pass_once()
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        st = self.status()
        self.assertTrue(st["disk_hold"])
        self.assertEqual(st["disk_free_gb"], 5.0)
        self.assertEqual(self.attn(), [conveyor.DISK_ID])
        self.assertEqual(len([e for e in self.events() if e["event"] == "disk_hold"]), 1)
        self.free = 10e9
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.assertFalse(self.status()["disk_hold"])
        self.assertEqual(self.attn(), [])

    def test_new_episode_new_item(self):
        self.free = 5e9
        self.c.pass_once()
        (self.base / "ack").mkdir()
        (self.base / "ack" / conveyor.DISK_ID).touch()
        self.c.pass_once()
        self.assertEqual(self.attn_ex_dry(), [])
        self.free = 50e9
        self.c.pass_once()
        self.free = 5e9
        self.t += 60
        self.c.pass_once()
        self.assertEqual(self.attn_ex_dry(), [conveyor.DISK_ID])
        self.assertEqual(len([e for e in self.events() if e["event"] == "disk_hold"]), 2)

    def test_unreadable_disk_holds(self):
        self.free = OSError("gone")
        self.drop("a.md", HDR.format("A") + "x")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        self.assertTrue(self.status()["disk_hold"])


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
