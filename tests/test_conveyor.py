"""Konveyor v1 (scripts/conveyor.py) with a fake claude runner; no network, no model calls."""
import contextlib
import errno
import importlib.util
import io
import json
import os
import re
import subprocess
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
        self.rm_out = "rm failed"

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
            return self.rm_rc, "removed" if self.rm_rc == 0 else self.rm_out.format(sid=args[1])
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
# powercfg /getactivescheme answers (GUIDs from LaptopControl PowerProfileApplyService.cs)
BAG_ACTIVE = "Power Scheme GUID: 9b1e4f52-2c7d-4a8e-b6f3-1d0a7c55b205  (LaptopControl - Çanta)"
OWN_ACTIVE = "Power Scheme GUID: 4a7c2e19-5b3d-4f81-9e26-0c8d1f3ab206  (LaptopControl - JARVIS İzleme)"
DEFAULT_ACTIVE = "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)"


class FakeGit:
    """Fake git/gh: a verified main one commit ahead of origin/main, CI green on ci/p2b."""

    def __init__(self):
        self.calls = []
        self.main, self.origin = MAIN, ORIGIN
        self.fail = {}  # args prefix tuple -> (rc, output) answered instead
        self.diff = "jarvis/core.py\n"  # CONVSAFE: scripts/conveyor.py needs the manual key 1
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
        if a[:3] == ("git", "diff", "--name-only") and a[-1] == f"{self.origin}..{self.main}":
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


class FakeProc:
    """A usage ledger subprocess: still running until rc is set."""

    def __init__(self, argv, **kw):
        self.argv, self.kw, self.rc, self.killed = argv, kw, None, False

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True


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
        self.ledger_procs = []  # never a real ledger subprocess
        self.c = Conveyor(self.root, self.fake, now=lambda: self.t,
                          send=lambda text: self.pushes.append(text) or "sent",
                          post=self.fake_post, key=lambda: self.key,
                          cmd=self.git, disk=self.fake_disk, projects=self.root / "projects",
                          mem=lambda: self.memory, idle=lambda: self.idle, procs=self.procs,
                          net=lambda: self.net, popen=self.fake_popen)
        self.base = self.root / "build" / "conveyor"
        self.hour = 12  # local hour for the night rule: never the real clock's
        hour = mock.patch.object(conveyor, "local_hour", lambda t: self.hour)
        hour.start()
        self.addCleanup(hour.stop)

    def fake_disk(self, path):
        if isinstance(self.free, Exception):
            raise self.free
        return mock.Mock(free=self.free)

    def fake_popen(self, argv, **kw):
        if getattr(self, "fake_popen_error", None):
            raise self.fake_popen_error
        self.ledger_procs.append(FakeProc(argv, **kw))
        return self.ledger_procs[-1]

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
            is_int = bool(conveyor.INT_RE.match(s["id"]))
            s = {"title": s["id"], "model": "sonnet", "effort": "medium", "tier": "T1",
                 "files": ["@main-checkout"] if is_int else [], "depends_on": [],
                 "prompt": f"build/conveyor/prompts/{s['id']}.md", **s}
            p = self.root / s["prompt"]
            if not p.exists():  # a valid default under lint_slice: CI target, granted files new
                p.write_text(f"TASK {s['id']}\nbody\n"
                             + (f"CI BRANCH: ci/{s['id'].lower()}\n" if is_int else "")
                             + "".join(f"{f} (new)\n" for f in s["files"]), encoding="utf-8")
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
        fable = conveyor.check_header("@jarvis lane=CORE model=fable effort=max base=main")
        self.assertEqual(fable, ({"lane": "CORE", "model": "fable", "effort": "max", "base": "main"}, None))
        cases = {"@jarvis lane=AGY0 model=opus effort=low base=main": "effort invalid: low",
                 "@jarvis lane=A model=opus effort=ultra base=main": "effort invalid: ultra",
                 "@jarvis lane=A model=gpt effort=high base=main": "model invalid: gpt",
                 "@jarvis lane=A model=claude-fable-5-1 effort=max base=main": "model invalid",
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
        self.assertIn("fable", conveyor.FAMILIES)
        self.assertIn("max", conveyor.EFFORTS)
        fam, eff = "|".join(conveyor.FAMILIES), "|".join(conveyor.EFFORTS)
        self.assertIn("export type Family = " + " | ".join(f"'{x}'" for x in conveyor.FAMILIES), src)
        self.assertIn("export type Effort = " + " | ".join(f"'{x}'" for x in conveyor.EFFORTS), src)
        self.assertIn(f"model=<{fam}> effort=<{eff}> base=<sha|main>", src)  # the contract comment
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

    def test_dispatch_fable_max(self):
        """The CLI takes `fable` as a --model alias and `max` as an --effort level: passed verbatim."""
        self.drop("a.md", "@jarvis lane=A model=fable effort=max base=main\nx\n")
        self.c.pass_once()
        (call,) = self.fake.bg_calls()
        self.assertEqual(call[:7], ["--bg", "--name", "A", "--model", "fable", "--effort", "max"])
        self.assertEqual((self.lanes()["A"]["model"], self.lanes()["A"]["effort"]), ("fable", "max"))

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
        self.drop("agy0.md", "@jarvis lane=AGY0 model=opus effort=ultra base=main\n")
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
                                  "preserved", "backlog", "breaker", "band", "attention",
                                  "outage_hold"})
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

    def test_lint_slice_held_others_dispatched(self):
        self.backlog({"id": "DEP", "depends_on": ["GHOST"]}, {"id": "MISS", "files": ["jarvis/nope.py"]},
                     {"id": "INTCONV", "files": ["@main-checkout"]}, {"id": "NUL", "depends_on": ["G\x00"]},
                     {"id": "OK"})
        (self.base / "prompts" / "MISS.md").write_text("TASK\n", encoding="utf-8")
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["OK"])
        v = self.view()
        self.assertEqual(v["DEP"], "held: unknown depends_on GHOST")
        self.assertEqual(v["NUL"], "held: unknown depends_on G\x00")  # a NUL path never crashes the pass
        self.assertTrue(v["MISS"].startswith("held: granted path jarvis/nope.py not in"))
        self.assertEqual(v["INTCONV"], "held: @main-checkout needs an integration (INT...) id")
        (item,) = [a for a in self.status()["attention"] if a["id"] == conveyor.BACKLOG_BAD_ID]
        self.assertIn("slice DEP: held: unknown depends_on GHOST", item["reason"])

    def test_invalid_slices_reported(self):
        self.backlog({"id": "A", "tier": "T9"}, {"id": "B"})
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["B"])
        self.assertTrue(any("bad tier" in r.get("error", "") for r in self.status()["backlog"]))
        (self.base / "backlog.json").write_text("{nope", encoding="utf-8")
        self.c.pass_once()
        self.assertTrue(any("backlog.json" in r.get("error", "") for r in self.status()["backlog"]))


class TestLintSlice(unittest.TestCase):
    HAVE = {"scripts/conveyor.py", "tests"}

    def lint(self, sid, files=(), prompt="TASK\n", deps=(), known=("A",)):
        return conveyor.lint_slice({"id": sid, "files": list(files), "depends_on": list(deps)},
                                   prompt, self.HAVE.__contains__, set(known))

    def test_prompt_readable_and_non_empty(self):
        self.assertEqual(self.lint("X", prompt=None), "prompt unreadable or not UTF-8")
        self.assertEqual(self.lint("X", prompt="﻿ \n\t"), "prompt empty")
        self.assertIsNone(self.lint("X", prompt="TASK X\n"))

    def test_integration_form_unambiguous(self):
        ci = "CI BRANCH: ci/int63\n"
        self.assertEqual(self.lint("INTCONV", ["@main-checkout"], "CI BRANCH: ci/intconv\n"),
                         "@main-checkout needs an integration (INT...) id")  # INT_RE: not INTC...
        self.assertIn('needs files ["@main-checkout"]', self.lint("INT61FIX", [], "CI BRANCH: ci/int61fix"))
        self.assertIn('needs files ["@main-checkout"]',
                      self.lint("INT63", ["@main-checkout", "scripts/conveyor.py"], ci))
        self.assertIsNone(self.lint("INT63", ["@main-checkout"], ci))
        self.assertIsNone(self.lint("INTCONV", ["scripts/conveyor.py"]))

    def test_integration_names_one_matching_ci_target(self):
        def lint(sid, prompt):
            return self.lint(sid, ["@main-checkout"], prompt)
        self.assertIn("names 0 CI BRANCH", lint("INT63", "push to ci/int63\n"))
        self.assertIn("names 2 CI BRANCH", lint("INT63", "CI BRANCH: ci/int63\nCI BRANCH: ci/int64\n"))
        self.assertIn("names 0 CI BRANCH", lint("INT63", "CI BRANCH: ci/\n"))
        self.assertIn("names 1 CI BRANCH", lint("INT63", "CI BRANCH: ci/int63\nCI BRANCH: ci/\n"))
        self.assertEqual(lint("INT63", "CI BRANCH: ci/int6\n"), "CI BRANCH ci/int6 does not match INT63")
        self.assertIn("does not match", lint("INT63", "CI BRANCH: ci/int630\n"))
        self.assertIn("does not match", lint("INT49B", "CI BRANCH: ci/int49\n"))
        self.assertIsNone(lint("INT63", "CI BRANCH: ci/int63\nre-state: CI BRANCH: `ci/int63`\n"))
        self.assertIsNone(lint("INT63", "Push to CI BRANCH: ci/int63.\n"))  # sentence dot is not the branch
        self.assertIsNone(lint("INT63", "CI BRANCH: ci/int63-2\n"))
        self.assertIsNone(lint("INT60R2", "CI BRANCH: ci/int60\n"))
        self.assertIsNone(lint("INT60-R", "CI BRANCH: ci/int60-2\n"))
        self.assertIsNone(self.lint("X", prompt="CI BRANCH: ci/a\nCI BRANCH: ci/b\n"))  # not integration

    def test_ci_target_is_the_whole_token(self):
        """CLEANFIX (CDXINT63C m1): a ci/<x> prefix of a longer token is not the target."""
        def lint(prompt):
            return self.lint("INT63", ["@main-checkout"], prompt)
        self.assertEqual(lint("CI BRANCH: ci/int63/foreign\n"), "CI BRANCH ci/int63/foreign is not one ci/<x> token")
        self.assertEqual(lint("CI BRANCH: ci/int63!x\n"), "CI BRANCH ci/int63!x is not one ci/<x> token")
        self.assertEqual(lint("CI BRANCH: `ci/int63`x\n"), "CI BRANCH ci/int63`x is not one ci/<x> token")
        self.assertIn("names 2 CI BRANCH", lint("CI BRANCH: ci/int63\nCI BRANCH: ci/int63/x\n"))
        self.assertIsNone(lint("Push to CI BRANCH: `ci/int63`.\n"))
        self.assertIsNone(lint("CI BRANCH: ci/int63"))  # end of prompt

    def test_granted_paths_exist(self):
        self.assertEqual(self.lint("X", ["jarvis/nope.py"]),
                         "granted path jarvis/nope.py not in the main checkout (mark it (new) in the prompt)")
        self.assertIn("granted path @none", self.lint("X", ["@none"]))
        self.assertIn("granted path docs/*.md", self.lint("X", ["docs/*.md"]))
        self.assertIn("jarvis/nope.py", self.lint("X", ["jarvis/nope.py"], "jarvis/new.py (new)\n"))
        self.assertIn("granted path a.py not in", self.lint("X", ["a.py"], "jarvis/a.py (new)\n"))  # whole path
        for bad in ("C:/Windows", "/etc", "../CONVLINT/scripts", "tests\\..\\..", ""):
            self.assertIn("is not relative inside the main checkout", self.lint("X", [bad], f"{bad} (new)\n"))
        self.assertIsNone(self.lint("X", ["scripts\\new.py"], "scripts/new.py (new)\n"))
        self.assertIsNone(self.lint("X", ["scripts/conveyor.py", "tests/*.py", "tests\\*"]))
        self.assertIsNone(self.lint("X", ["build/reports/X.md", "build\\reports\\Y.md"]))
        self.assertIsNone(self.lint("X", ["jarvis/new.py"], "- `jarvis/new.py` (NEW)\n"))

    def test_depends_on_known(self):
        self.assertEqual(self.lint("X", deps=["A", "GHOST", "B"]), "unknown depends_on GHOST, B")
        self.assertIsNone(self.lint("X", deps=["A"]))


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


THRASH = {"type": "assistant", "model": "<synthetic>", "apiError": "autocompact_thrashing", "error": "invalid_request",
          "isApiErrorMessage": True, "message": {"role": "assistant", "content": [{"type": "text", "text": (
              "Autocompact is thrashing: the context refilled to the limit within 3 turns of the previous "
              "compact, 3 times in a row.")}]}}  # TESTLOCK transcript, 2026-10-22T12:05:36Z


class TestFactNote(unittest.TestCase):
    """CTXRESUME: the fact note a nudged / resumed-in-place session starts with, built by code."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wt = Path(tmp.name)
        self.out = {("rev-parse", "--abbrev-ref", "HEAD"): "ctxresume\n",
                    ("log", "--oneline", "c82d2d6..HEAD"): "1111111 fix: b\n2222222 feat: a\n",
                    ("status", "--short"): " M scripts/conveyor.py\n?? tests/new.py\n",
                    ("diff", "--name-only", "c82d2d6", "--"): "scripts/conveyor.py\n"}
        self.calls = []

    def cmd(self, args, timeout=120):
        self.calls.append((args, timeout))
        n = len(conveyor.GIT_PIN)
        self.assertEqual(args[:n + 2], [*conveyor.GIT_PIN, "-C", str(self.wt)])
        return 0, self.out[tuple(args[n + 2:])]

    def test_note_from_a_fake_worktree(self):
        (self.wt / "build" / "reports").mkdir(parents=True)
        (self.wt / "build" / "reports" / "A.md").write_text(
            "STATUS: BLOCKED\nBRANCH: a\nRan 414 tests in 12.0s\nOK\nRUNS: 3 modules, 0 failures\nnotes\n",
            encoding="utf-8")
        self.assertEqual(conveyor.fact_note(self.cmd, self.wt, "c82d2d6", "A"), "\n".join([
            conveyor.FACT_HEAD, "branch: ctxresume",
            "report build/reports/A.md: present; STATUS: BLOCKED",
            "last test summary in report: RUNS: 3 modules, 0 failures",
            "commits c82d2d6..HEAD (max 10):", "  1111111 fix: b", "  2222222 feat: a",
            "git status --short (max 30):", "   M scripts/conveyor.py", "  ?? tests/new.py",
            "files changed vs c82d2d6:", "  scripts/conveyor.py", conveyor.FACT_TAIL]))
        self.assertEqual({t for _, t in self.calls}, {conveyor.FACT_GIT_TIMEOUT_S})
        self.assertEqual(len(self.calls), 4)

    def test_no_report_no_commits(self):
        self.out[("log", "--oneline", "c82d2d6..HEAD")] = ""
        note = conveyor.fact_note(self.cmd, self.wt, "c82d2d6", "A")
        self.assertIn("commits c82d2d6..HEAD (max 10):\n  none\n", note)
        self.assertIn("report build/reports/A.md: absent\nlast test summary in report: none\n", note)

    def test_size_cap_and_line_caps(self):
        self.out[("status", "--short")] = "".join(f"?? {'d' * 20}/{i}.py\n" for i in range(100))
        self.out[("diff", "--name-only", "c82d2d6", "--")] = "".join(f"{'ç' * 60}/{i}.py\n" for i in range(500))
        note = conveyor.fact_note(self.cmd, self.wt, "c82d2d6", "A")
        self.assertLessEqual(len(note.encode("utf-8")), conveyor.FACT_CAP)
        self.assertTrue(note.endswith("\n[... cut ...]\n" + conveyor.FACT_TAIL))
        self.assertIn("  ... 70 more\n", note)  # status capped at 30 lines before the byte cap
        self.assertIn("report build/reports/A.md: absent\nlast test summary in report: none\n", note)
        self.out[("log", "--oneline", "c82d2d6..HEAD")] = "".join(f"{i:07d} x\n" for i in range(12))
        self.assertIn("  ... 2 more\n", conveyor.fact_note(self.cmd, self.wt, "c82d2d6", "A"))

    def test_git_timeout_reads_unknown(self):
        def slow(args, timeout=120):
            self.calls.append((args, timeout))
            raise subprocess.TimeoutExpired(args, timeout)
        note = conveyor.fact_note(slow, self.wt, "c82d2d6", "A")
        for what in ("branch: unknown (branch", "  unknown (commits", "  unknown (status", "  unknown (files changed"):
            self.assertIn(what + ": TimeoutExpired)\n", note)
        self.assertEqual({t for _, t in self.calls}, {conveyor.FACT_GIT_TIMEOUT_S})
        self.assertTrue(note.endswith(conveyor.FACT_TAIL))

    def test_no_worktree_bad_base_bad_rc(self):
        note = conveyor.fact_note(self.cmd, None, "main", "A")
        self.assertIn("branch: unknown (branch: no lane worktree)\n", note)
        self.assertIn("report build/reports/A.md: unknown (no lane worktree)\n", note)
        self.assertEqual(self.calls, [])
        note = conveyor.fact_note(lambda args, timeout=120: (128, "fatal"), self.wt, "-x;rm", "A")
        self.assertIn("commits base..HEAD (max 10):\n  unknown (base: not a ref name)\n", note)
        self.assertIn("branch: unknown (branch: rc 128)\n", note)
        self.assertIn("report build/reports/../x.md: unknown (lane name)\n", conveyor.fact_note(self.cmd, self.wt, "c82d2d6", "../x"))

    def test_no_secrets(self):
        self.out[("log", "--oneline", "c82d2d6..HEAD")] = (
            "1111111 key sk-ant-api03-AbCdEf_123 and ghp_" + "a" * 36 + "\n2222222 token=hunter2 topic-xyz\n")
        with mock.patch.dict(os.environ, {"JARVIS_NTFY_TOPIC": "topic-xyz"}):
            note = conveyor.fact_note(self.cmd, self.wt, "c82d2d6", "A")
        for s in ("sk-ant-", "ghp_", "hunter2", "topic-xyz"):
            self.assertNotIn(s, note)
        self.assertIn("  1111111 key [secret] and [secret]\n  2222222 [secret] [topic]\n", note)

    def test_no_quoted_secrets(self):
        """CLEANFIX (CDXINT63C M3): JSON / quoted keys and values, both quote styles, go whole."""
        self.out[("log", "--oneline", "c82d2d6..HEAD")] = (
            '1111111 cfg "api_key": "abc def" end\n'
            "2222222 cfg 'token'='s3 cr \\' et' end\n"
            '3333333 {"password":"p w","x":1}\n')
        note = conveyor.fact_note(self.cmd, self.wt, "c82d2d6", "A")
        for s in ("abc", "def", "s3", "\\'", "p w", "api_key", "token", "password"):
            self.assertNotIn(s, note)
        self.assertIn('  1111111 cfg [secret] end\n  2222222 cfg [secret] end\n  3333333 {[secret],"x":1}\n',
                      note)


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
        note = conveyor.fact_note(self.git, self.c.worktree_of(self.a), "1234567", "A")  # CTXRESUME
        self.assertEqual(self.resumes(), [[
            "--bg", "--resume", sid, "--name", "A", "--model", "opus", "--effort", "high",
            "--settings", str(self.root / conveyor.SETTINGS), note + "\n\n" + conveyor.NUDGE]])
        self.assertTrue(note.startswith(conveyor.FACT_HEAD + "\nbranch: unknown (branch: rc 1)\n"))
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

    def test_nudge_resumes_with_the_lanes_fable_max(self):
        d = self.lanes()
        d["A"].update(model="fable", effort="max")
        (self.base / "lanes.json").write_text(json.dumps(d))
        self.net = True
        self.t += 600
        self.c.pass_once()
        (call,) = self.resumes()
        self.assertEqual(call[5:9], ["--model", "fable", "--effort", "max"])

    def test_network_down_no_nudge(self):
        self.t += 1200
        self.c.pass_once()
        self.assertEqual((self.resumes(), self.nudges()), ([], []))
        self.net = True  # back: the nudge goes out on the next pass
        self.c.pass_once()
        self.assertEqual(len(self.resumes()), 1)

    def test_git_timeout_note_reads_unknown_and_nudge_still_sent(self):  # CTXRESUME
        self.git.raise_on = tuple(conveyor.GIT_PIN)
        self.net = True
        self.t += 600
        self.c.pass_once()
        (call,) = self.resumes()
        note, nudge = call[-1].rsplit("\n\n", 1)
        self.assertEqual(nudge, conveyor.NUDGE)
        self.assertIn("branch: unknown (branch: TimeoutError)", note)
        self.assertIn("  unknown (commits: TimeoutError)", note)
        self.assertIn("  unknown (status: TimeoutError)", note)
        self.assertIn("  unknown (files changed: TimeoutError)", note)
        self.assertTrue(note.endswith(conveyor.FACT_TAIL))
        self.assertEqual([s["rc"] for s in self.nudges()[0]["steps"]], [0, 0])

    def transcript(self, *entries):
        p = self.root / "projects" / "x" / f"{self.a['sessionId']}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")

    def test_context_full_nudged_at_once(self):  # CTXRESUME: TESTLOCK/BOTAGREE 2026-10-22
        self.transcript({"type": "user", "message": {"role": "user", "content": "go"}}, THRASH,
                        {"type": "cost-state"}, {"type": "worktree-state"})
        self.net = True
        self.t += 60
        self.c.pass_once()
        (call,) = self.resumes()
        self.assertTrue(call[-1].startswith(conveyor.FACT_HEAD + "\n"))
        self.assertTrue(call[-1].endswith(conveyor.FACT_TAIL + "\n\n" + conveyor.NUDGE))
        (ev,) = self.nudges()
        self.assertEqual((ev["n"], ev["context_full"]), (1, True))

    def test_context_full_needs_network(self):
        self.transcript(THRASH)
        self.t += 60
        self.c.pass_once()
        self.assertEqual((self.resumes(), self.nudges()), ([], []))

    def test_context_full_text_elsewhere_keeps_the_timer(self):
        said = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "autocompact_thrashing Prompt is too long"}]}}
        self.transcript(THRASH, said)  # a later normal assistant entry decides
        self.net = True
        self.t += 60
        self.c.pass_once()
        self.assertEqual((self.resumes(), self.nudges()), ([], []))
        self.transcript({"type": "user", "isApiErrorMessage": True, "apiError": "autocompact_thrashing"})
        self.c.pass_once()
        self.assertEqual((self.resumes(), self.nudges()), ([], []))

    def test_prompt_too_long_api_error_counts(self):
        self.transcript({"type": "assistant", "isApiErrorMessage": True, "message": {
            "role": "assistant", "content": [{"type": "text", "text": "Prompt is too long"}]}})
        self.net = True
        self.t += 60
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

    def test_failing_page_flush_is_logged_once_per_error_and_the_loop_goes_on(self):
        (self.base / "PAUSE").write_text("")
        calls = []

        def broken_flush():  # also called inside a pass: the same error until the 3rd call
            calls.append(1)
            raise OSError("pager.json locked") if len(calls) <= 3 else ValueError("bad json")

        with mock.patch.object(self.c, "flush_pages", side_effect=broken_flush), \
                mock.patch.object(self.c, "pass_once", wraps=self.c.pass_once) as passes, \
                mock.patch.object(self.c, "own_lock", side_effect=[True, True, True, False]), \
                mock.patch.object(conveyor.time, "sleep"):
            self.assertEqual(self.c.loop(), 0)
        self.assertEqual(passes.call_count, 4)
        logged = [e["error"] for e in self.events() if e["event"] == "flush_error"]
        self.assertEqual(logged, ["OSError('pager.json locked')", "ValueError('bad json')"])

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

    def live(self, family, body, age=0):
        d = self.root / "build" / "lanes"
        d.mkdir(parents=True, exist_ok=True)
        if isinstance(body, dict):
            body = json.dumps({"updated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.t - age)), **body})
        (d / f"{family}.json").write_text(body, encoding="utf-8")

    def test_live_newer_than_ledger_wins(self):
        self.ledger(31, age=3600)
        self.live("opus", {"seven_pct": 33}, age=60)
        self.live("sonnet", {"seven_pct": 32}, age=120)
        self.assertEqual(self.c.usage_pct(), 33)
        self.assertEqual(self.c.usage_source, "live:opus")
        self.live("haiku", {"seven_pct": 34}, age=60)  # same timestamp: the higher % wins
        self.assertEqual((self.c.usage_pct(), self.c.usage_source), (34, "live:haiku"))
        self.c.pass_once()
        self.assertEqual((self.status()["pace"]["usage_pct"], self.status()["pace"]["usage_source"]),
                         (34, "live:haiku"))

    def test_ledger_newer_than_live_wins(self):
        self.ledger(40, age=10)
        self.live("opus", {"seven_pct": 33}, age=60)
        self.assertEqual((self.c.usage_pct(), self.c.usage_source), (40, "ledger"))

    def test_stale_live_ignored(self):
        self.ledger(31, age=3600)
        self.live("opus", {"seven_pct": 90}, age=conveyor.USAGE_FRESH_S + 1)
        self.live("sonnet", {"seven_pct": 90}, age=-3600)  # from the future: never wins forever
        self.assertEqual((self.c.usage_pct(), self.c.usage_source), (31, "ledger"))

    def test_garbage_live_ignored(self):
        self.ledger(31, age=3600)
        for i, body in enumerate(["not json", "[1, 2]", "42", "\"x\"", json.dumps({"seven_pct": 50})]):
            self.live(f"g{i}", body)  # last one: no updated_at
        for i, pct in enumerate([True, None, "50", -1, 100.5, float("nan")]):
            self.live(f"p{i}", {"seven_pct": pct})
        for i, at in enumerate([None, 5, "", "yesterday", "2026-13-40T00:00:00Z"]):
            self.live(f"t{i}", json.dumps({"seven_pct": 50, "updated_at": at}))
        (self.root / "build" / "lanes" / "dir.json").mkdir()
        (self.root / "build" / "lanes" / "bad.json").write_bytes(b"\xff\xfe\x00{")
        self.assertEqual((self.c.usage_pct(), self.c.usage_source), (31, "ledger"))
        self.live("sonnet", {"seven_pct": 33})
        self.assertEqual((self.c.usage_pct(), self.c.usage_source), (33, "live:sonnet"))

    def test_missing_dir_and_nothing_fresh(self):
        self.assertIsNone(self.c.usage_pct())
        self.assertIsNone(self.c.usage_source)
        self.ledger(31)
        self.assertEqual((self.c.usage_pct(), self.c.usage_source), (31, "ledger"))
        self.ledger(31, age=conveyor.USAGE_FRESH_S + 1)
        self.live("opus", {"seven_pct": 33}, age=conveyor.USAGE_FRESH_S + 1)
        self.assertEqual((self.c.usage_pct(), self.c.usage_source), (None, None))


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
        self.assertEqual(self.status()["pace"]["band"], "paced: hold at 97 %")

    def test_pace_bands_at_boundaries(self):
        for pct, want in ((52, 3), (52.1, 2), (57, 2), (57.1, 2), (62, 2), (62.1, 2), (96.9, 2)):
            self.fresh()
            self.paced()
            self.assertEqual(self.lanes_for(pct), want, pct)
            p = self.status()["pace"]
            self.assertEqual((p["mode"], p["pace_line"], p["usage_pct"], p["max_lanes"]),
                             ("paced", 50.0, pct, want))
            self.assertFalse(self.status()["usage_hold"])  # ahead of the line: the min_lanes floor
        self.assertEqual(p["band"], "paced: ahead of line -> min 2")

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
        self.assertEqual(self.status()["pace"]["max_lanes"], 2)  # 56 > pace_line 0 + 2: min_lanes

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
        self.assertEqual(self.status()["pace"]["max_lanes"], 2)

    def test_invalid_pace_is_paced_from_now(self):
        for bad in ("{not json", json.dumps({"mode": "warp"}), json.dumps({"mode": "paced"}),
                    json.dumps({"days": 0}), json.dumps([1])):
            self.fresh()
            (self.base / "pace.json").write_text(bad, encoding="utf-8")
            self.assertEqual(self.lanes_for(12.1), 2, bad)  # pace_line 0: ahead -> min_lanes 2
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
        for raw, want in ((0, 1), (-4, 1), (1, 1), (5, 5), (6, 6), (7, 7), (8, 8), (9, 8), (40, 8), (4.9, 4)):
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
        for pct, want in ((52, 5), (52.1, 2), (57, 2), (57.1, 2), (62, 2), (62.1, 2)):
            self.fresh()
            self.pace(mode="paced", window_start=conveyor.iso(self.t - 3.5 * 86400), max_lanes=5)
            self.assertEqual(self.lanes_for_8(pct), want, pct)
            self.assertEqual(self.status()["pace"]["max_lanes"], want)

    def test_paced_lower_bands_capped_by_max_lanes_1(self):
        for pct, want in ((52, 1), (55, 1), (60, 1), (62.1, 1)):  # default min 2 capped by max 1
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

    def paced_min_max(self, lo=2, hi=3):
        self.pace(mode="paced", window_start=conveyor.iso(self.t - 3.5 * 86400),
                  min_lanes=lo, max_lanes=hi)

    def test_paced_ahead_of_line_keeps_min_lanes(self):
        for pct, lo, want in ((52.1, 2, 2), (70, 2, 2), (96.9, 2, 2), (80, 3, 3), (80, 1, 1)):
            self.fresh()
            self.paced_min_max(lo)
            self.assertEqual(self.lanes_for_8(pct), want, (pct, lo))
            p = self.status()["pace"]
            self.assertEqual((p["max_lanes"], p["band"], p["error"]),
                             (want, f"paced: ahead of line -> min {want}", None))
            self.assertFalse(self.status()["usage_hold"])

    def test_paced_on_or_under_line_runs_max_lanes(self):
        for pct in (0, 50, 52):
            self.fresh()
            self.paced_min_max()
            self.assertEqual(self.lanes_for_8(pct), 3, pct)
            self.assertEqual(self.status()["pace"]["band"], "paced: on/under line -> max 3")
        self.fresh()  # a hand-set min_lanes above lanes_present (3) wins on the line too
        self.pace(mode="paced", window_start=conveyor.iso(self.t - 3.5 * 86400), min_lanes=5)
        self.assertEqual(self.lanes_for_8(50), 5)
        self.assertEqual(self.status()["pace"]["band"], "paced: on/under line -> max 5")

    def test_paced_hold_at_97_overrides_min_lanes(self):
        self.paced_min_max()
        self.assertEqual(self.lanes_for_8(97), 0)
        st = self.status()
        self.assertTrue(st["usage_hold"])
        self.assertEqual((st["pace"]["max_lanes"], st["pace"]["band"]), (0, "paced: hold at 97 %"))

    def test_min_lanes_above_max_lanes_is_invalid(self):
        for bad in ({"min_lanes": 3, "max_lanes": 2}, {"min_lanes": "2"}, {"min_lanes": True}):
            self.fresh()
            self.pace(**bad)
            self.c.pass_once()
            p = self.status()["pace"]
            self.assertTrue(p["error"], bad)
            self.assertEqual((p["mode"], p["window_start"]), ("paced", conveyor.iso(self.t)))
            self.assertIn(conveyor.PACE_BAD_ID, self.attn())

    def test_min_lanes_clamped_like_the_other_lane_keys(self):
        self.pace(mode="paced", window_start=conveyor.iso(self.t - 3.5 * 86400), min_lanes=40)
        self.assertEqual(self.lanes_for_8(80), 8)  # clamped to MAX_LANES_CAP; no max_lanes set
        self.assertIsNone(self.status()["pace"]["error"])

    def test_reset_keeps_min_and_max_lanes(self):
        self.pace(min_lanes=2, max_lanes=3)
        self.ledger(96)
        self.c.pass_once()
        self.t += 3600
        self.ledger(56)
        self.c.pass_once()
        self.assertEqual(self.pace_file(), {**conveyor.PACE_DEFAULTS, "mode": "paced",
                                            "window_start": conveyor.iso(self.t),
                                            "min_lanes": 2, "max_lanes": 3})
        self.assertEqual(self.status()["pace"]["max_lanes"], 2)

    def test_full_burn_ignores_min_lanes(self):
        self.pace(min_lanes=5)  # lanes_present 3: a floor of 5 would show
        self.assertEqual(self.lanes_for_8(90), 3)
        self.assertEqual(self.status()["pace"]["band"], "full_burn")
        self.fresh()
        self.pace(min_lanes=2, max_lanes=3)
        self.assertEqual(self.lanes_for_8(98), 0)
        self.assertEqual(self.status()["pace"]["band"], "full_burn")


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
        self.memory = (3.0, 50.0)  # back at the threshold: hysteresis keeps the hold
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])
        self.memory = (3.0 + conveyor.MEM_MARGIN_GB, 50.0)
        self.c.pass_once()
        self.assertEqual(self.fake.bg_calls(), [])  # threshold + margin, not yet held MEM_OK_S
        self.t += conveyor.MEM_OK_S
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
        self.assertEqual(self.fake.bg_calls(), [])  # released only after MEM_OK_S under the line
        self.t += conveyor.MEM_OK_S
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
                self.c.mem_hold = False  # the threshold alone, not the previous subtest's hysteresis
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
        self.assertEqual(self.status()["pace"]["max_lanes"], 8)  # clamped to MAX_LANES_CAP
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
        self.assertEqual(self.status()["pace"]["max_lanes"], 2)  # ahead of the line: min_lanes 2

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
        self.git.fail[("powercfg",)] = (0, OWN_ACTIVE)
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
        self.git.fail[("powercfg",)] = (0, OWN_ACTIVE)
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
        self.git.fail[("powercfg",)] = (0, OWN_ACTIVE)
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

    def bag_pace(self, active):
        self.pace(power_switch=True, power_switch_cmd=["LC.exe", "--activate", "{plan}"],
                  power_switch_query_cmd=["LCQ.exe", "--status"])
        self.git.fail[("LC.exe",)] = (0, "ok")
        self.git.fail[("LCQ.exe",)] = active

    def skips(self):
        return [e["reason"] for e in self.events() if e["event"] == "power_switch_skip"]

    def test_power_switch_skipped_while_bag_profile_active(self):
        self.bag_pace((0, BAG_ACTIVE))
        self.go_away()
        self.passes(3)
        self.assertEqual(self.lc_calls(), [])
        self.assertEqual(self.skips(), ["bag profile active"])
        self.assertEqual([c for c in self.git.calls if c[0] == "LCQ.exe"], [["LCQ.exe", "--status"]])
        self.assertNotIn(conveyor.POWER_SWITCH_ID, [a["id"] for a in self.status()["attention"]])

    def test_power_switch_skipped_when_profile_unreadable_or_unknown(self):
        for active in ((1, "error"), (0, ""), (0, "Power Scheme GUID: 1234 (Someone Else)")):
            self.git.calls.clear()
            self.bag_pace(active)
            self.idle = 600 if self.c.presence == "present" else 0
            self.passes(3)
            self.assertEqual(self.lc_calls(), [], active)
        reasons = self.skips()
        self.assertEqual(len(reasons), 3)
        self.assertTrue(reasons[0].startswith("active profile unreadable"), reasons)
        self.assertTrue(reasons[1].startswith("active profile unreadable"), reasons)
        self.assertTrue(reasons[2].startswith("unknown active profile"), reasons)

    def test_power_switch_query_timeout_no_switch(self):
        self.bag_pace((0, OWN_ACTIVE))
        self.git.raise_on = ("LCQ.exe",)
        self.go_away()
        self.passes(3)
        self.assertEqual(self.lc_calls(), [])
        self.assertEqual(self.skips(), ["active profile unreadable: TimeoutError"])

    def test_power_switch_from_own_or_default_plan_as_today(self):
        self.bag_pace((0, OWN_ACTIVE))
        self.go_away()
        self.assertEqual(self.lc_calls(), [["LC.exe", "--activate", "JarvisFocus"]])
        self.git.fail[("LCQ.exe",)] = (0, DEFAULT_ACTIVE)
        self.idle = 0
        self.passes(3)
        self.assertEqual(self.lc_calls()[1:], [["LC.exe", "--activate", "JarvisWatch"]])
        self.assertEqual(self.skips(), [])

    def test_power_switch_without_cmd_is_invalid(self):
        for cfg in ({"power_switch": True}, {"power_switch": "yes"},
                    {"power_switch_cmd": "LC.exe"}, {"lanes_away": "5"}, {"away_after_min": 0},
                    {"power_switch_query_cmd": "LCQ.exe"}, {"power_switch_query_cmd": []},
                    {"power_switch_skip_plans": "Bag"}, {"power_switch_skip_plans": [""]}):
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

    def escalated(self):
        self.t += conveyor.ATTENTION_PAGE_S + 3600
        self.c.pass_once()
        return "vermedi: A: " in "\n".join(self.pushes)

    def test_routine_pass_items_stay_on_the_board_without_a_page(self):
        """CONVPAGE: PASS + needs_orchestrator and PASS + FINDINGS never page Salih."""
        self.reply = answer({"kind": "needs_orchestrator", "summary": "entegre", "next": "n"})
        self.finish_a(extra="FINDINGS: küçük not\n")
        self.assertEqual(self.attn(), ["A", conveyor.DRY_ID])
        self.assertFalse(self.escalated())
        self.assertIn("A", self.attn())

    def test_findings_alone_never_page(self):
        self.finish_a(extra="FINDINGS: küçük not\n")
        self.assertIn("A", self.attn())
        self.assertFalse(self.escalated())

    def test_blocked_status_pages(self):
        self.finish_a(status="BLOCKED")
        self.assertIn("A", self.attn())
        self.assertTrue(self.escalated())

    def test_stuck_status_pages(self):
        self.finish_a(status="STUCK")
        self.assertIn("A", self.attn())
        self.assertTrue(self.escalated())

    def test_unknown_status_with_needs_orchestrator_pages(self):
        """CONVFIX61 F4: only a good status may be quiet; PARTIAL + needs_orchestrator pages."""
        self.reply = answer({"kind": "needs_orchestrator", "summary": "entegre", "next": "n"})
        self.finish_a(status="PARTIAL")
        self.assertIn("A", self.attn())
        self.assertNotIn("A", self.c.quiet)
        self.assertTrue(self.escalated())

    def test_unknown_status_with_pass_reading_pages(self):
        self.finish_a(status="PARTIAL")
        self.assertIn("A", self.attn())
        self.assertTrue(self.escalated())

    def test_status_loudness_is_an_allow_list(self):
        """Lane state set directly: lowercase, empty and missing statuses are unknown, so loud."""
        for status, item, quiet in (("pass", True, False), ("", True, False), (None, True, False),
                                    ("PARTIAL", True, False), ("MISSING", True, False),
                                    ("DEAD", True, False), ("FAIL", True, False),
                                    ("PASS", False, False), ("CI_PENDING", False, False)):
            with self.subTest(status=status):
                ln = {"finished_ts": 5, "dead": "x"}
                if status is not None:
                    ln["status"] = status
                ids = [i for i, _, _ in self.c.attention({"A": ln})]
                self.assertEqual("A" in ids, item)
                self.assertEqual("A" in self.c.quiet, quiet)
                ln["reading"] = {"kind": "needs_orchestrator", "summary": "s"}
                self.c.attention({"A": ln})  # a routine reading never quiets a non-good status
                self.assertEqual("A" in self.c.quiet, not item)

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

    def fake_ledger(self, force=False):
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
                     idle=lambda: self.idle, procs=self.procs, popen=self.fake_popen)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "usage_ledger.py").write_text("")
        (self.base / "usage_ledger.py").write_text("")
        c.usage_ledger()
        (self.root / "scripts" / "usage_ledger.py").unlink()
        self.ledger_procs[0].rc = 0
        c.usage_ledger()  # reaps
        c.usage_ledger(force=True)
        self.assertEqual([p.argv[1:] for p in self.ledger_procs],
                         [[str(self.root / "scripts" / "usage_ledger.py"), str(self.ledger)],
                          [str(self.base / "usage_ledger.py"), str(self.ledger)]])
        self.assertEqual(self.ledger_procs[0].argv[0], sys.executable)

    # SESSIONSWEEP: stopped prev copies are not doubt; transcripts archived after cleanup + ledger

    def redispatched(self, old_name="A"):
        old = self.finished()
        self.drop("a2.md", HDR.format("A"))
        self.c.pass_once()
        new = self.lanes()["A"]["id"]
        next(a for a in self.fake.agents if a["id"] == old).update(state="stopped", name=old_name)
        self.fake.agent("A").update(state="done", status="idle")
        self.report("A", "PASS")
        self.c.pass_once()
        self.assertEqual((self.lanes()["A"]["prev_ids"], self.lanes()["A"]["finished"]), ([old], True))
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        return old, new

    def test_stopped_prev_copy_is_removed_with_the_lane(self):
        old, new = self.redispatched()
        self.assertEqual(sorted(c[1] for c in self.cli("rm")), sorted([old, new]))
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")

    def test_stopped_prev_copy_with_other_doubt_still_refused(self):
        old, new = self.redispatched(old_name="X")
        self.assertEqual(self.cli("rm"), [])
        self.assertEqual([e["reason"] for e in self.events() if e["event"] == "cleanup_skip"],
                         [f"{old}: kind/name/state/session not as expected"])

    def slug(self):
        return re.sub(r"[^A-Za-z0-9]", "-", str(self.c.root))

    def archive_on(self, value=None):
        self.base.mkdir(parents=True, exist_ok=True)
        (self.base / "pace.json").write_text(json.dumps(
            {"session_archive_dir": str(self.root / "arch") if value is None else value}))

    def transcript(self, sid, proj=None, mtime=None):
        d = self.root / "projects" / (proj or self.slug())
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.jsonl"
        f.write_text("{}")
        t = self.t - 5 if mtime is None else mtime
        os.utime(f, (t, t))
        return f

    def counted(self, *sids, at=None):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps({
            "generated_at": conveyor.datetime.fromtimestamp(self.t if at is None else at).astimezone().isoformat(),
            "sessions": [{"session_id": s} for s in sids]}))

    def cleaned(self, lid="0000aaaa", **kw):
        return {"id": lid, "finished": True, "cleanup": {"at": conveyor.iso(self.t), "result": "removed"}, **kw}

    def archived(self):
        return [e for e in self.events() if e["event"].startswith("session_archive")]

    def test_archived_only_after_cleanup_removed_and_ledger(self):
        self.archive_on()
        sid = self.finished()
        f = self.transcript(self.fake.agent("A")["sessionId"])
        sub = f.with_suffix("")
        sub.mkdir()
        (sub / "x.jsonl").write_text("{}")
        os.utime(sub, (self.t - 5, self.t - 5))
        self.c.pass_once()
        self.assertTrue(f.exists())  # not cleaned yet
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()  # cleanup removes; this pass's listing still shows the session
        self.assertEqual(self.cli("rm"), [["rm", sid]])
        self.assertTrue(f.exists())
        self.assertEqual(self.archived(), [])
        self.c.pass_once()
        to = self.root / "arch" / time.strftime("%Y-%m", time.localtime(self.t)) / self.slug()
        self.assertFalse(f.exists() or sub.exists())
        self.assertTrue((to / f.name).is_file() and (to / sub.name / "x.jsonl").is_file())
        self.c.pass_once()
        self.assertEqual([(e["event"], e["lane"], sorted(e["paths"])) for e in self.archived()],
                         [("session_archived", "A", sorted([str(to / f.name), str(to / sub.name)]))])

    def test_archive_moves_prev_ids_and_keeps_foreign_and_own_sessions(self):
        self.archive_on()
        a, b = self.transcript("0000aaaa-1"), self.transcript("0000bbbb-1")
        foreign = self.transcript("0000aaaa-1", proj="C--other-project")
        salih = self.transcript("0000cccc-1")
        self.counted("0000aaaa-1", "0000bbbb-1", "0000cccc-1")
        res = self.c.archive_sessions({"A": self.cleaned(prev_ids=["0000bbbb"])}, [])
        self.assertEqual([(n, sorted(f)) for n, f in res], [("A", sorted([str(b), str(a)]))])
        self.assertFalse(a.exists() or b.exists())
        self.assertTrue(foreign.exists() and salih.exists())

    def test_archive_never_unfinished_uncleaned_preserved_attention_or_listed(self):
        self.archive_on()
        lanes = {"U": self.cleaned("0000a001", finished=False),
                 "E": {**self.cleaned("0000a002"), "cleanup": {"at": conveyor.iso(self.t), "result": "error"}},
                 "P": self.cleaned("0000a003", preserved={"reason": "x"}),
                 "B": self.cleaned("0000a004"), "L": self.cleaned("0000a005")}
        files = [self.transcript(f"0000a00{i}-1") for i in range(1, 6)]
        self.counted(*(f.stem for f in files))
        self.assertEqual(self.c.archive_sessions(lanes, [{"id": "0000a005"}], {"B"}), [])
        self.assertTrue(all(f.exists() for f in files))
        self.assertEqual(self.archived(), [])

    def test_archive_doubts_leave_the_session_and_log_once(self):
        self.archive_on()
        lanes = {"A": self.cleaned()}
        f = self.transcript("0000aaaa-1")
        for ledger, reason in [(None, "usage ledger has not counted the session since cleanup"),
                               ((self.t - 1, "0000aaaa-1"), "usage ledger has not counted the session since cleanup"),
                               ((self.t, "other"), "usage ledger has not counted the session since cleanup")]:
            if ledger:
                self.counted(ledger[1], at=ledger[0])
            self.c.archive_sessions(lanes, [])
            self.c.archive_sessions(lanes, [])
        self.counted("0000aaaa-1")
        os.utime(f, (self.t + 5, self.t + 5))
        self.c.archive_sessions(lanes, [])
        os.utime(f, (self.t - 5, self.t - 5))
        self.transcript("0000aaaa-2")
        self.c.archive_sessions(lanes, [])
        self.assertTrue(f.exists())
        self.assertEqual([e["reason"] for e in self.archived()],
                         ["usage ledger has not counted the session since cleanup",
                          "transcript modified after cleanup", "ambiguous session id"])

    def test_archive_off_without_an_absolute_dir(self):
        f = self.transcript("0000aaaa-1")
        self.counted("0000aaaa-1")
        for value in ["missing", "", "   ", "relative/dir", None]:
            if value != "missing":
                self.archive_on(value if value is not None else "")
            self.assertEqual(self.c.archive_sessions({"A": self.cleaned()}, []), [])
        self.assertTrue(f.exists())
        self.assertEqual(self.archived(), [])

    def test_archive_move_failure_logs_and_keeps(self):
        self.archive_on()
        f = self.transcript("0000aaaa-1")
        self.counted("0000aaaa-1")
        with mock.patch.object(conveyor.shutil, "move", side_effect=OSError("locked")):
            self.assertEqual(self.c.archive_sessions({"A": self.cleaned()}, []), [])
            self.assertEqual(self.c.archive_sessions({"A": self.cleaned()}, []), [])
        self.assertTrue(f.exists())
        ev = self.archived()
        self.assertEqual([(e["event"], e["lane"], e["moved"]) for e in ev], [("session_archive_failed", "A", [])])
        self.assertIn("locked", ev[0]["error"])

    def test_archive_corrupt_prev_ids_and_bad_dir_never_crash(self):
        self.archive_on("C:\\x\x00y" if os.name == "nt" else "/x\x00y")
        f = self.transcript("0000aaaa-1")
        self.counted("0000aaaa-1")
        for pv in [None, "0000aaaa", 5]:
            self.c.archive_seen.clear()
            self.assertEqual(self.c.archive_sessions({"A": self.cleaned(prev_ids=pv)}, []), [])
        self.assertTrue(f.exists())
        self.assertEqual({e["event"] for e in self.archived()}, {"session_archive_failed"})

    def test_archive_dry_run_and_cli_write_nothing(self):
        self.archive_on()
        f = self.transcript("0000aaaa-1")
        self.counted("0000aaaa-1")
        (self.base / "lanes.json").write_text(json.dumps({"A": self.cleaned()}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(conveyor.archive_cli(self.c, True), 0)
        self.assertEqual(out.getvalue().splitlines(), ["would move: 1 lanes, 1 paths", f"  A: {f.name}"])
        self.assertTrue(f.exists())
        self.assertFalse((self.root / "arch").exists())
        self.assertEqual(self.archived(), [])


class TestLedgerSubprocess(Base):
    """The usage ledger runs as a subprocess the loop polls, never waits on."""

    def setUp(self):
        super().setUp()
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "usage_ledger.py").write_text("")

    def errors(self):
        return [e for e in self.events() if e["event"] == "error"]

    def test_running_ledger_is_not_waited_on_and_not_restarted(self):
        self.c.usage_ledger()
        for _ in range(3):
            self.t += 60
            self.c.usage_ledger()
            self.c.usage_ledger(force=True)
        self.assertEqual(len(self.ledger_procs), 1)
        self.assertFalse(self.ledger_procs[0].killed)
        self.assertEqual(self.errors(), [])

    def test_timeout_kills_once_with_one_error(self):
        self.c.usage_ledger()
        self.t += conveyor.LEDGER_TIMEOUT_S - 1
        self.c.usage_ledger()
        self.assertFalse(self.ledger_procs[0].killed)
        self.t += 1
        self.c.usage_ledger()
        self.c.usage_ledger()
        self.assertTrue(self.ledger_procs[0].killed)
        self.assertEqual(len(self.errors()), 1)
        self.assertIn("killed", self.c.last_error)
        self.assertEqual(len(self.ledger_procs), 1)  # the next start waits for USAGE_S

    def test_exit_codes_and_cadence(self):
        self.c.usage_ledger()
        self.ledger_procs[0].rc = 1
        self.c.usage_ledger()
        self.c.usage_ledger()
        self.assertEqual(len(self.errors()), 1)
        self.assertIn("exit 1", self.c.last_error)
        self.t += conveyor.USAGE_S - 1
        self.c.usage_ledger()
        self.assertEqual(len(self.ledger_procs), 1)
        self.t += 1
        self.c.usage_ledger()
        self.ledger_procs[1].rc = 0
        self.c.usage_ledger()
        self.assertEqual((len(self.ledger_procs), len(self.errors())), (2, 1))

    def test_popen_error_is_one_error(self):
        self.fake_popen_error = OSError("no python")
        self.c.usage_ledger()
        self.c.usage_ledger()
        self.assertEqual(len(self.errors()), 1)
        self.assertIn("no python", self.c.last_error)

    def test_loop_beats_while_slow_ledger_runs(self):
        (self.base / "PAUSE").write_text("")
        beats, sleeps, t0 = [], [], self.t

        def sleep(s):
            sleeps.append(s)
            self.t += 100

        with mock.patch.object(self.c, "heartbeat", side_effect=lambda: beats.append(self.t)), \
                mock.patch.object(self.c, "own_lock", side_effect=lambda: len(sleeps) < 4), \
                mock.patch.object(conveyor.time, "sleep", side_effect=sleep):
            self.assertEqual(self.c.loop(), 0)
        self.assertLessEqual({t0, t0 + 100, t0 + 200, t0 + 300}, set(beats))  # every pass beat
        self.assertEqual(len(self.ledger_procs), 1)
        self.assertTrue(self.ledger_procs[0].killed)  # 4th pass: LEDGER_TIMEOUT_S after the start
        self.assertEqual(len(self.errors()), 1)

    def test_raising_pass_still_kills_a_timed_out_ledger(self):
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            self.t += 100

        with mock.patch.object(self.c, "pass_once", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.c, "own_lock", side_effect=lambda: len(sleeps) < 4), \
                mock.patch.object(conveyor.time, "sleep", side_effect=sleep):
            self.assertEqual(self.c.loop(), 0)
        self.assertEqual(len(self.ledger_procs), 1)
        self.assertTrue(self.ledger_procs[0].killed)
        self.assertEqual(sum("killed" in e["message"] for e in self.errors()), 1)

    finished = TestCleanup.finished
    cli = TestCleanup.cli

    def test_cleanup_removes_nothing_while_ledger_runs_or_fails(self):
        self.c.stop_finished = lambda *a: None
        self.finished()
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        self.assertEqual(len(self.ledger_procs), 1)  # cleanup started it, never waited
        self.c.pass_once()
        self.ledger_procs[0].rc = 1
        self.t += conveyor.USAGE_S
        self.c.pass_once()
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.assertTrue(any(e["event"] == "cleanup_skip" for e in self.events()))
        self.assertEqual([e["message"] for e in self.errors()], ["usage ledger: exit 1 (build/ledger/usage.log)"])


class TestSharedWrite(Base):
    """CONVSTATUS: a reader holding a shared file (PermissionError on os.replace) is retried, then
    one error event; the pass never aborts. The heartbeat stays fresh across the CLI loops."""

    fake_ledger, finished, cli = TestCleanup.fake_ledger, TestCleanup.finished, TestCleanup.cli

    def setUp(self):
        super().setUp()
        self.ledger_runs, self.ledger_error, self.ledger_skip = 0, None, set()
        self.c.usage_ledger = self.fake_ledger
        self.ledger = self.root / "build" / "ledger" / "usage.json"
        self.sleeps = []
        p = mock.patch.object(conveyor.time, "sleep", side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def errors(self):
        return [e["message"] for e in self.events() if e["event"] == "error"]

    def locked(self, *names, n=None, exc=None):
        """Patch os.replace: a target in `names` raises (PermissionError 13) its first n times (all if None)."""
        real, hits = os.replace, []

        def replace(src, dst):
            if Path(dst).name in names and (n is None or len(hits) < n):
                hits.append(dst)
                raise exc or PermissionError(13, "denied")
            return real(src, dst)
        return mock.patch.object(conveyor.os, "replace", side_effect=replace)

    def spies(self, *names):
        return [mock.patch.object(self.c, n, wraps=getattr(self.c, n)) for n in names]

    def test_transient_lock_retried_then_written(self):
        with self.locked("status.json", n=3):
            self.c.pass_once()
        self.assertIn("updated_at", self.status())
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.sleeps, [conveyor.REPLACE_GAP_S] * 3)

    def test_always_locked_one_error_pass_completes(self):
        with contextlib.ExitStack() as st:
            st.enter_context(self.locked("status.json"))
            backstop, pager_watch, flush = (st.enter_context(p) for p in
                                            self.spies("backstop", "pager_watch", "flush_pages"))
            self.c.loop(once=True)
        self.assertEqual(self.errors(), ["write status.json: PermissionError(13, 'denied') after 5 tries"])
        self.assertEqual(self.sleeps, [conveyor.REPLACE_GAP_S] * (conveyor.REPLACE_TRIES - 1))
        for m in (backstop, pager_watch, flush):
            m.assert_called()
        self.assertFalse((self.base / "status.json").exists())
        self.assertTrue((self.base / "running_files.json").exists())  # the rest of write_status ran

    def test_running_files_locked_status_still_written(self):
        with self.locked("running_files.json"):
            self.c.pass_once()
        self.assertIn("updated_at", self.status())
        self.assertEqual(self.errors(), ["write running_files.json: PermissionError(13, 'denied') after 5 tries"])
        self.assertIsNone(self.c.status_fail_ts)

    def test_running_files_other_error_status_still_ok(self):
        """CONVFIX61 F6: ok comes only from status.json; a running_files error is logged, not stale."""
        for exc in (ValueError("x"), OSError(5, "io")):
            with self.subTest(exc=exc):
                with self.locked("running_files.json", exc=exc):
                    self.c.pass_once()
                self.assertIn("updated_at", self.status())
                self.assertEqual(self.errors()[-1], f"running_files.json: {exc!r}")
                self.assertIsNone(self.c.status_fail_ts)
                self.t += conveyor.STATUS_STALE_S + 1
                self.assertNotIn("status_stale", [i for i, _, _ in self.c.attention({})])

    def test_lanes_locked_one_error_pass_continues(self):
        self.drop("A.md", HDR.format("A"))
        with self.locked("lanes.json"):
            self.c.pass_once()
        errors = self.errors()  # one per save_lanes call of the pass
        self.assertTrue(errors)
        self.assertEqual(set(errors), {"write lanes.json: PermissionError(13, 'denied') after 5 tries"})
        self.assertIn("updated_at", self.status())

    def test_pager_locked_page_does_not_raise(self):
        with self.locked("pager.json"):
            self.c.page("x")
        self.assertEqual(self.errors(), ["write pager.json: PermissionError(13, 'denied') after 5 tries"])

    def test_push_state_written_with_retry(self):
        self.c.push_file.write_text("{}", encoding="utf-8")
        with self.locked(self.c.push_file.name):
            self.c.push_gate({})
        self.assertEqual(self.errors(),
                         [f"write {self.c.push_file.name}: PermissionError(13, 'denied') after 5 tries"])

    def test_lanes_bad_branch_still_runs_backstop_and_flush(self):
        (self.base / "lanes.json").write_text("not json", encoding="utf-8")
        with contextlib.ExitStack() as st:
            st.enter_context(self.locked("status.json"))
            backstop, flush = (st.enter_context(p) for p in self.spies("backstop", "flush_pages"))
            self.c.pass_once()
        backstop.assert_called_once()
        flush.assert_called_once()
        self.assertIn("write status.json: PermissionError(13, 'denied') after 5 tries", self.errors())
        self.assertEqual((self.base / "lanes.json").read_text(encoding="utf-8"), "not json")

    def test_status_exception_does_not_end_the_pass(self):
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(self.c, "write_status", side_effect=ValueError("bad")))
            backstop, pager_watch, flush = (st.enter_context(p) for p in
                                            self.spies("backstop", "pager_watch", "flush_pages"))
            self.c.loop(once=True)
        self.assertEqual(self.errors(), ["status: ValueError('bad')"])
        for m in (backstop, pager_watch, flush):
            m.assert_called()

    def test_status_stale_item_after_ten_minutes(self):
        ids = lambda: [i[0] for i in self.c.attention(self.c.load_lanes())]  # noqa: E731
        with self.locked("status.json"):
            self.c.pass_once()  # first failed write starts the clock
        self.assertEqual(self.c.status_fail_ts, self.t)
        self.t += conveyor.STATUS_STALE_S - 1
        self.assertNotIn("status_stale", ids())
        self.t += 2
        self.assertIn("status_stale", ids())
        self.c.pass_once()  # unlocked: a good write clears it
        self.assertIsNone(self.c.status_fail_ts)
        self.assertNotIn("status_stale", ids())

    def test_other_os_errors_raise_without_retry(self):
        with self.locked("x.json", exc=OSError(errno.ENOSPC, "full")), self.assertRaises(OSError) as cm:
            self.c.write_atomic(self.base / "x.json", "{}")
        self.assertNotIsInstance(cm.exception, PermissionError)
        self.assertEqual(self.sleeps, [])

    def slow_cli(self):
        """Each stop/rm takes 70 s (> BEAT_S); returns the heartbeat age seen at each call."""
        ages, real = [], self.c.run

        def run(args, *a, **kw):
            if args[0] in ("stop", "rm"):
                ages.append(self.t - self.c.beat_ts)
                self.t += 70
            return real(args, *a, **kw)

        def heartbeat():
            self.c.beat_ts = self.t
        self.c.run = run
        for p in (mock.patch.object(self.c, "heartbeat", side_effect=heartbeat),
                  mock.patch.object(self.c, "own_lock", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        return ages

    def test_heartbeat_fresh_across_slow_cleanup(self):
        self.c.stop_finished = lambda *a: None
        sid = self.finished()
        ages = self.slow_cli()
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [["stop", sid], ["rm", sid]])
        self.assertTrue(all(a < conveyor.BEAT_S for a in ages), ages)

    def test_heartbeat_fresh_across_slow_early_stops(self):
        for n in ("A", "B"):
            self.drop(f"{n}.md", HDR.format(n))
        self.c.pass_once()
        for n in ("A", "B"):
            self.fake.agent(n).update({"state": "done", "status": "idle"})
            self.report(n, "PASS")
        ages = self.slow_cli()
        self.c.pass_once()
        self.assertEqual(len(self.cli("stop")), 2)
        self.assertTrue(all(a < conveyor.BEAT_S for a in ages), ages)


class TestPreserved(Base):
    """CONV11: a lane whose branch is not provably on main is preserved, never rm-retried."""

    fake_ledger = TestCleanup.fake_ledger
    finished = TestCleanup.finished
    cli = TestCleanup.cli

    def setUp(self):
        super().setUp()
        self.ledger_runs, self.ledger_error, self.ledger_skip = 0, None, set()
        self.c.usage_ledger = self.fake_ledger
        self.ledger = self.root / "build" / "ledger" / "usage.json"
        self.c.stop_finished = lambda *a: None
        self.git_fail, self.git_fail_on = None, ["git"]
        self.c.cmd = self.run_git
        self.g("init", "-q", "-b", "main")
        (self.root / "f.txt").write_text("base\n")
        self.g("add", "f.txt")
        self.g("commit", "-q", "-m", "base")
        self.wt = self.root / ".claude" / "worktrees" / "A"
        self.g("worktree", "add", "-q", "-b", "lane-a", str(self.wt))

    def g(self, *args, cwd=None):
        p = conveyor.subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
            cwd=cwd or self.root, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def run_git(self, args, timeout=120):
        pinned = args[:len(conveyor.GIT_PIN)] == conveyor.GIT_PIN  # every proof call is pinned
        a = ["git", *args[len(conveyor.GIT_PIN):]] if pinned else args
        if not (pinned or a[:2] in (["git", "worktree"], ["git", "cherry"], ["git", "-C"], ["git", "rev-list"],
                                    ["git", "update-ref"])
                or a[:3] in (["git", "log", "-m"], ["git", "diff", "--name-only"])) or a[-1].endswith("..main"):
            return self.git(args, timeout)  # the fake main/origin of every other test
        if self.git_fail and a[:len(self.git_fail_on)] == self.git_fail_on:
            return self.git_fail
        p = conveyor.subprocess.run(args, cwd=self.root, capture_output=True, text=True)
        return p.returncode, p.stdout

    def lane_commit(self):
        (self.wt / "g.txt").write_text("work\n")
        self.g("add", "g.txt", cwd=self.wt)
        self.g("commit", "-q", "-m", "work", cwd=self.wt)
        return self.g("rev-parse", "HEAD", cwd=self.wt)

    def run_cleanup(self):
        sid = self.finished(agent={"cwd": str(self.wt)})
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        return sid

    def assert_preserved_once(self):
        for _ in range(3):
            self.t += 60
            self.c.pass_once()
        self.assertEqual(self.cli("rm"), [])
        self.assertEqual(len([e for e in self.events() if e["event"] == "preserved"]), 1)
        self.assertEqual([i["id"] for i in self.status()["attention"] if i["id"].startswith("preserved:")],
                         ["preserved:A"])
        self.assertEqual([p["lane"] for p in self.status()["preserved"]], ["A"])
        return self.lanes()["A"]["preserved"]

    def test_integrated_by_cherry_pick_is_removed_once(self):
        sha = self.lane_commit()
        self.g("cherry-pick", sha)  # a new sha on main, same patch
        sid = self.run_cleanup()
        self.assertEqual(self.cli("rm"), [["rm", sid]])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")
        self.assertNotIn("preserved", self.lanes()["A"])
        self.c.pass_once()
        self.assertEqual(len(self.cli("rm")), 1)
        self.assertFalse(self.wt.exists())  # CONVSAFE: the clean, integrated worktree and branch go too
        self.assertEqual(self.g("branch", "--list", "lane-a"), "")
        self.assertEqual([e["lane"] for e in self.events() if e["event"] in ("worktree_removed", "branch_deleted")],
                         ["A", "A"])

    def test_unintegrated_commit_is_preserved_without_rm(self):
        sha = self.lane_commit()
        self.run_cleanup()
        pv = self.assert_preserved_once()
        self.assertEqual((pv["branch"], pv["first_unintegrated_sha"]), ("lane-a", sha))
        self.assertEqual(self.cli("stop"), [])

    def origin_main(self, rev="main"):
        self.g("update-ref", "refs/remotes/origin/main", self.g("rev-parse", rev))

    def main_commit(self, files):
        for n, text in files.items():
            (self.root / n).write_text(text)
        self.g("add", *files)
        self.g("commit", "-q", "-m", "main")

    def equivalent_pairs(self):
        return [e["pairs"] for e in self.events() if e["event"] == "cleanup_equivalent"]

    def assert_cleaned(self, sid):
        self.assertEqual(self.cli("rm"), [["rm", sid]])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")
        self.assertNotIn("preserved", self.lanes()["A"])
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.g("branch", "--list", "lane-a"), "")

    def test_cherry_pick_then_main_edit_is_cleaned_as_equivalent(self):
        """CLEANPATCH: off_main refuses (main edited g.txt later); the patch-id is on origin/main."""
        sha = self.lane_commit()
        self.g("cherry-pick", "-x", sha)  # -x: another sha (same second, same metadata = same sha)
        self.assertNotEqual(self.g("rev-parse", "main"), sha)
        self.main_commit({"g.txt": "work, edited on main\n"})
        self.origin_main()
        self.assertTrue(self.c.off_main("lane-a"))
        sid = self.run_cleanup()
        self.assert_cleaned(sid)
        pairs = self.equivalent_pairs()
        self.assertTrue(pairs)
        self.assertEqual({(p["commit"], p["how"]) for ps in pairs for p in ps}, {(sha, "patch-id")})

    def test_content_on_main_by_later_commit_is_cleaned_as_equivalent(self):
        sha = self.lane_commit()
        self.main_commit({"g.txt": "work\n", "h.txt": "more\n"})  # same hunk, other patch-id
        self.origin_main()
        sid = self.run_cleanup()
        self.assert_cleaned(sid)
        main = self.g("rev-parse", "main")
        self.assertEqual({(p["commit"], p["main"], p["how"]) for ps in self.equivalent_pairs() for p in ps},
                         {(sha, main, "content")})

    def test_one_real_commit_among_equivalent_ones_is_preserved(self):
        self.g("cherry-pick", "-x", self.lane_commit())
        (self.wt / "k.txt").write_text("real work\n")
        self.g("add", "k.txt", cwd=self.wt)
        self.g("commit", "-q", "-m", "real", cwd=self.wt)
        real = self.g("rev-parse", "HEAD", cwd=self.wt)
        self.origin_main()
        self.run_cleanup()
        pv = self.assert_preserved_once()
        self.assertEqual(pv["first_unintegrated_sha"], real)
        self.assertEqual(self.equivalent_pairs(), [])
        ok, why = self.c.merged_equivalent("lane-a")
        self.assertEqual((ok, why), (False, f"lane-a has commits not on origin/main (first {real[:7]})"))

    def test_patch_id_match_differing_in_whitespace_only_is_preserved(self):
        """patch-id ignores whitespace: an indentation change on main is not the lane's code."""
        sha = self.lane_commit()
        self.main_commit({"g.txt": "    work\n"})
        self.origin_main()
        self.assertEqual(self.g("cherry", "main", "lane-a"), f"- {sha}")
        self.run_cleanup()
        self.assertEqual(self.assert_preserved_once()["first_unintegrated_sha"], sha)
        self.assertEqual(self.equivalent_pairs(), [])
        self.assertEqual(self.c.merged_equivalent("lane-a"),
                         (False, f"lane-a has commits not on origin/main (first {sha[:7]})"))

    def test_hunk_elsewhere_in_file_is_not_equivalent(self):
        """CLEANFIX (CDXINT63C M1): main made the same edit to an identical block elsewhere in the
        file; patch-id matches, the lane's change is not on main."""
        block = "a\nb\nc\nB\nd\ne\nf\n"
        self.main_commit({"f2.txt": block * 2})
        self.g("merge", "-q", "--ff-only", "main", cwd=self.wt)
        (self.wt / "f2.txt").write_text(block.replace("B", "X") + block)
        self.g("commit", "-q", "-am", "first B", cwd=self.wt)
        sha = self.g("rev-parse", "HEAD", cwd=self.wt)
        self.main_commit({"f2.txt": block + block.replace("B", "X")})
        self.origin_main()
        self.assertEqual(self.g("cherry", "main", "lane-a"), f"- {sha}")
        self.assertEqual(self.c.merged_equivalent("lane-a"),
                         (False, f"lane-a has commits not on origin/main (first {sha[:7]})"))
        self.run_cleanup()
        self.assertEqual(self.assert_preserved_once()["first_unintegrated_sha"], sha)
        self.assertEqual(self.equivalent_pairs(), [])

    def test_content_landed_then_edited_on_main_is_not_equivalent(self):
        """No patch-id match ("+") and main edited the lines since: nothing proves the lane's code."""
        sha = self.lane_commit()
        self.main_commit({"g.txt": "work\n", "h.txt": "more\n"})
        self.main_commit({"g.txt": "work, edited on main\n"})
        self.origin_main()
        self.assertEqual(self.g("cherry", "main", "lane-a"), f"+ {sha}")
        self.assertEqual(self.c.merged_equivalent("lane-a"),
                         (False, f"lane-a has commits not on origin/main (first {sha[:7]})"))

    def test_cherry_pick_then_main_edit_names_the_cherry_pick_commit(self):
        sha = self.lane_commit()
        self.g("cherry-pick", "-x", sha)
        pick = self.g("rev-parse", "main")
        self.main_commit({"g.txt": "work, edited on main\n"})
        self.origin_main()
        self.assertEqual(self.c.merged_equivalent("lane-a"),
                         (True, [{"commit": sha, "main": pick, "how": "patch-id"}]))
        self.git_fail, self.git_fail_on = (128, ""), ["git", "rev-list", "--cherry-mark"]
        self.assertEqual(self.c.merged_equivalent("lane-a"),
                         (False, f"git rev-list --cherry-mark {sha[:7]}...origin/main rc=128"))

    def test_binary_beside_text_is_proved_binary_only_is_refused(self):
        """A .qm beside its .ts: same blob on origin/main is equivalent, another blob or a
        binary-only commit is not."""
        def lane(files, msg):
            for n, data in files.items():
                (self.wt / n).write_bytes(data)
            self.g("add", *files, cwd=self.wt)
            self.g("commit", "-q", "-m", msg, cwd=self.wt)
            return self.g("rev-parse", "HEAD", cwd=self.wt)
        sha = lane({"t.ts": b"text\n", "t.qm": b"q\0lane"}, "ts+qm")
        self.g("cherry-pick", "-x", sha)
        self.origin_main()
        self.assertEqual(self.c.merged_equivalent("lane-a"),
                         (True, [{"commit": sha, "main": self.g("rev-parse", "main"), "how": "patch-id"}]))
        other = lane({"t.ts": b"text 2\n", "t.qm": b"q\0lane 2"}, "ts+qm 2")
        self.main_commit({"t.ts": "text 2\n"})  # the text landed, but main rebuilt the .qm
        (self.root / "t.qm").write_bytes(b"q\0main 2")
        self.g("commit", "-q", "-am", "qm 2")
        self.origin_main()
        self.assertEqual(self.c.merged_equivalent("lane-a"),
                         (False, f"lane-a has commits not on origin/main (first {other[:7]})"))
        self.g("switch", "-q", "-c", "bin")
        (self.root / "t.qm").write_bytes(b"q\0bin")
        self.g("commit", "-q", "-am", "qm only")
        only = self.g("rev-parse", "HEAD")
        self.assertEqual(self.c.merged_equivalent("bin"),
                         (False, f"bin: commit {only[:7]} empty, binary-only or root (git diff rc=0)"))

    def test_equivalence_git_error_or_unknown_origin_is_preserved(self):
        sha = self.lane_commit()
        self.main_commit({"g.txt": "work\n", "h.txt": "more\n"})
        self.assertEqual(self.c.merged_equivalent("lane-a"), (False, "origin/main unknown"))
        self.origin_main()
        self.git_fail, self.git_fail_on = (128, ""), ["git", "merge-tree"]
        self.run_cleanup()
        pv = self.assert_preserved_once()
        self.assertEqual(pv["first_unintegrated_sha"], sha)
        self.assertEqual(self.equivalent_pairs(), [])
        self.git_fail_on = ["git", "cherry"]
        self.assertFalse(self.c.merged_equivalent("lane-a")[0])

    def discard_setup(self, origin="main"):
        sha = self.lane_commit()
        self.g("cherry-pick", "-x", sha)
        self.origin_main(origin)
        self.fake.rm_rc = 1
        self.fake.rm_out = ("Worktree has unpushed commits. To discard the worktree and its commits: "
                            f"claude rm {{sid}} --discard-unpushed {sha}@abc1234\n")

        def run(args, **kw):  # claude rm accepts the discard token
            if "--discard-unpushed" in args:
                self.fake.rm_rc = 0
            return self.fake(args, **kw)
        self.c.run = run
        return sha

    def test_rm_refusal_discards_only_proven_equivalent_commits(self):
        sha = self.discard_setup()
        sid = self.run_cleanup()
        self.assertEqual(self.cli("rm"), [["rm", sid], ["rm", sid, "--discard-unpushed", f"{sha}@abc1234"]])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")
        ev = [e for e in self.events() if e["event"] == "cleanup_equivalent" and e.get("session") == sid]
        self.assertEqual([(p["commit"], p["how"]) for p in ev[0]["pairs"]], [(sha, "patch-id")])
        self.assertFalse(self.wt.exists())

    def test_rm_refusal_never_discards_without_equivalence_or_when_dirty(self):
        self.discard_setup(origin="main~1")  # on local main only: origin/main lacks it
        self.run_cleanup()
        self.assertEqual([c for c in self.cli("rm") if "--discard-unpushed" in c], [])
        self.assertIs(self.lanes()["A"]["cleanup"]["in_main"], True)

    def test_rm_refusal_never_discards_dirty_worktree(self):
        self.discard_setup()
        (self.wt / "untracked.txt").write_text("x\n")
        self.run_cleanup()
        self.assertEqual([c for c in self.cli("rm") if "--discard-unpushed" in c], [])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "error")

    @staticmethod
    def cli_refusal(*toks):
        """CLEANRM: the claude CLI's refusal as production logged it (quotes mangled to U+FFFD)."""
        return ("Worktree .claude/worktrees/A has 1 commit: �work�. It exists on no remote, so "
                "deleting the worktree would lose it.\n  push it and run �claude rm {sid}� again, or "
                "discard the worktree and its commits: claude rm {sid} --discard-unpushed " + " ".join(toks) + "\n")

    def root_cleanup(self):
        """A lane spawned in the root that entered its worktree: `claude agents` cwd is the root."""
        sid = self.finished(agent={"cwd": str(self.root)})
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        return sid

    def assert_refused(self):
        self.assertEqual([c for c in self.cli("rm") if "--discard-unpushed" in c], [])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "error")
        self.assertEqual(self.equivalent_pairs(), [])
        self.assertEqual(self.cleanup_ids(), ["cleanup:A"])
        self.assertTrue(self.wt.exists())

    def test_rm_refusal_root_cwd_discards_exactly_the_listed_token(self):
        sha = self.discard_setup()
        tok = f"{sha}@24a6741462c516b8ec8cb6dd21c20030"
        self.fake.rm_out = self.cli_refusal(tok)
        sid = self.root_cleanup()
        self.assertEqual(self.cli("rm"), [["rm", sid], ["rm", sid, "--discard-unpushed", tok]])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")
        ev = [e for e in self.events() if e["event"] == "cleanup_equivalent" and e.get("session") == sid]
        self.assertEqual([(p["commit"], p["how"]) for p in ev[0]["pairs"]], [(sha, "patch-id")])
        self.assertEqual(self.cleanup_ids(), [])
        self.assertFalse(self.wt.exists())

    def test_rm_refusal_with_one_non_equivalent_sha_is_refused(self):
        sha = self.discard_setup()
        self.g("checkout", "-q", "-b", "other")
        (self.root / "o.txt").write_text("other\n")
        self.g("add", "o.txt")
        self.g("commit", "-q", "-m", "other")
        other = self.g("rev-parse", "HEAD")
        self.g("checkout", "-q", "main")
        for toks in ([f"{sha}@abc", f"{other}@abc"], [f"{other}@abc", f"{sha}@abc"]):  # either order
            with self.subTest(toks=toks):
                self.fake.rm_out = self.cli_refusal(*toks)
                self.assertIsNone(self.c.rm_discard({"id": "x", "cwd": str(self.wt)},
                                                    self.fake.rm_out.format(sid="x")))
        self.fake.rm_out = self.cli_refusal(f"{sha}@abc", f"{other}@abc")
        self.root_cleanup()
        self.assert_refused()

    def test_rm_refusal_uncommitted_changes_is_refused(self):
        self.discard_setup()
        self.fake.rm_out = ("Worktree .claude/worktrees/A has uncommitted changes, so deleting the worktree "
                            "would lose them.\n  commit or stash them and run �claude rm {sid}� again, or "
                            "delete the session from �claude agents� (ctrl+x twice) to discard them\n")
        self.root_cleanup()
        self.assert_refused()

    def test_rm_refusal_unparseable_is_refused(self):
        sha = self.discard_setup()
        self.fake.rm_out = self.cli_refusal(f"{sha}@abc")[:-30]  # truncated mid-token
        self.root_cleanup()
        self.assert_refused()

    def test_rm_refusal_parse_variants(self):
        """CLEANRM F1: only an exact `<40 lowercase hex>@<hex>` token list on the last line parses."""
        s, t = "a" * 40, "b" * 40
        ok = {"one": (f"{s}@ab", [s]), "two, any order": (f"{t}@1 {s}@2", [t, s]),
              "tab separated": (f"{s}@1\t{t}@2", [s, t]), "CRLF": (f"{s}@1\r\n", [s])}
        for k, (toks, shas) in ok.items():
            with self.subTest(k):
                r = conveyor.Conveyor.rm_refusal(self.cli_refusal(toks).format(sid="x"))
                self.assertEqual((r[0], r[2]), ("x", shas))
        bad = {"uppercase": f"{s.upper()}@ab", "short sha": f"{s[:39]}@ab", "long sha": f"{s}a@ab",
               "abbreviated": f"{s[:12]}@ab", "no @": s, "empty @": f"{s}@", "two @": f"{s}@a@b",
               "comma": f"{s}@1,{t}@2", "quoted": f"'{s}@1'", "trailing text": f"{s}@1 again",
               "unicode digit": f"{s[:39]}١@1", "fullwidth": f"{s[:39]}ａ@1", "empty": "",
               "ellipsis": f"{s}@1 …", "nul": f"{s}@1\x00"}
        for k, toks in bad.items():
            with self.subTest(k):
                self.assertIsNone(conveyor.Conveyor.rm_refusal(self.cli_refusal(toks).format(sid="x")))
        out = self.cli_refusal(f"{s}@1").format(sid="x")
        for k, text in {"line after": out + "more\n", "no prefix": out.replace("its commits: ", ""),
                        "uncommitted": out.replace("1 commit", "uncommitted changes"), "plain": "error\n",
                        "empty output": ""}.items():
            with self.subTest(k):
                self.assertIsNone(conveyor.Conveyor.rm_refusal(text))

    def test_rm_discard_needs_the_listed_id_and_a_worktree(self):
        sha = self.discard_setup()
        out = self.cli_refusal(f"{sha}@abc").format(sid="x")
        ln = {"worktree": str(self.wt)}
        self.assertEqual(self.c.rm_discard({"id": "x", "cwd": str(self.root)}, out, ln)[0], [f"{sha}@abc"])
        self.assertIsNone(self.c.rm_discard({"id": "y", "cwd": str(self.root)}, out, ln))  # another session
        self.assertIsNone(self.c.rm_discard({"id": "x", "cwd": str(self.root)}, out, {}))  # no worktree
        self.assertIsNone(self.c.rm_discard({"id": "x", "cwd": str(self.root)}, out,
                                            {"worktree": str(self.root)}))  # the root is not a worktree
        for cwd in (self.root.parent, self.root / "sub"):  # only a root cwd falls back to ln["worktree"]
            with self.subTest(cwd=cwd):
                self.assertIsNone(self.c.rm_discard({"id": "x", "cwd": str(cwd)}, out, ln))

    def test_rm_refusal_with_two_equivalent_shas_is_refused(self):
        """CLEANRM: one worktree has one HEAD; a second listed sha (here main's, equivalent) is not it."""
        sha = self.discard_setup()
        self.fake.rm_out = self.cli_refusal(f"{sha}@abc", f"{self.g('rev-parse', 'main')}@abc")
        self.root_cleanup()
        self.assertEqual([c for c in self.cli("rm") if "--discard-unpushed" in c], [])
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "error")
        self.assertEqual(self.equivalent_pairs(), [])
        self.assertTrue(self.lanes()["A"]["cleanup"]["in_main"])  # both on local main: waits for the push
        self.assertTrue(self.wt.exists())

    def test_rm_refusal_early_uncommitted_line_keeps_attention(self):
        """CLEANRM: in_main reads the whole refusal; an uncommitted line before the 300-char tail counts."""
        self.discard_setup()
        self.fake.rm_out = ("Worktree has uncommitted changes, so deleting it would lose them.\n" + "x" * 400 + "\n"
                            + self.cli_refusal(f"{self.g('rev-parse', 'main')}@abc"))
        self.root_cleanup()
        self.assert_refused()
        self.assertFalse(self.lanes()["A"]["cleanup"]["in_main"])

    def merge_side(self, extra):
        """lane-a: a --no-ff merge of side branch work that main cherry-picks, own file if extra."""
        self.g("checkout", "-q", "-b", "side", cwd=self.wt)
        self.g("cherry-pick", "-x", self.lane_commit())  # -x: not the same sha as the side commit
        self.g("checkout", "-q", "lane-a", cwd=self.wt)
        self.g("merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=self.wt)
        if extra:  # amend keeps both parents: an evil merge
            (self.wt / "h.txt").write_text("merge-only\n")
            self.g("add", "h.txt", cwd=self.wt)
            self.g("commit", "-q", "--amend", "--no-edit", cwd=self.wt)
        self.assertTrue(self.g("rev-list", "--merges", "main..lane-a"))
        return self.g("rev-parse", "HEAD", cwd=self.wt)

    def test_merge_with_content_not_on_main_is_preserved(self):
        sha = self.merge_side(extra=True)  # CONVMERGE: `git cherry` alone skips the merge
        self.run_cleanup()
        pv = self.assert_preserved_once()
        self.assertEqual((pv["branch"], pv["first_unintegrated_sha"]), ("lane-a", sha))
        self.assertIn("h.txt", pv["reason"])
        self.assertTrue(self.wt.exists())
        self.assertEqual(self.g("branch", "--list", "lane-a"), "+ lane-a")  # "+": checked out in the kept worktree

    def test_merge_with_all_content_on_main_is_kept(self):
        sha = self.merge_side(extra=False)  # CONVMERGE2: no content check proves a merge
        self.run_cleanup()
        pv = self.assert_preserved_once()
        self.assertEqual((pv["branch"], pv["first_unintegrated_sha"]), ("lane-a", sha))
        self.assertIn("merge commits", pv["reason"])
        self.assertTrue(self.wt.exists())
        self.assertEqual(self.g("branch", "--list", "lane-a"), "+ lane-a")

    def test_add_then_drop_merges_are_kept(self):
        """main A-B-C; lane M1(A,B) adds c.txt, M2(M1,C) drops it: tip tree == main, work lost if removed."""
        for n in ("b", "c"):  # the lane worktree stays at A
            (self.root / f"{n}.txt").write_text(f"{n}\n")
            self.g("add", f"{n}.txt")
            self.g("commit", "-q", "-m", n)
        b, c = self.g("rev-parse", "main~1"), self.g("rev-parse", "main")
        self.g("merge", "-q", "--no-ff", "--no-commit", b, cwd=self.wt)
        (self.wt / "c.txt").write_text("lane work\n")
        self.g("add", "c.txt", cwd=self.wt)
        self.g("commit", "-q", "-m", "m1", cwd=self.wt)
        self.g("merge", "-q", "--no-ff", "--no-commit", "-X", "theirs", c, cwd=self.wt)
        (self.wt / "c.txt").write_text("c\n")
        self.g("add", "c.txt", cwd=self.wt)
        self.g("commit", "-q", "-m", "m2", cwd=self.wt)
        self.assertEqual(self.g("diff", "--name-only", "main", "lane-a"), "")
        self.run_cleanup()
        self.assertIn("merge commits", self.assert_preserved_once()["reason"])
        self.assertTrue(self.wt.exists())
        self.assertEqual(self.g("branch", "--list", "lane-a"), "+ lane-a")

    def test_hostile_config_gives_same_answers(self):
        for k, v in (("log.diffMerges", "first-parent"), ("diff.ignoreSubmodules", "all"),
                     ("status.showUntrackedFiles", "no"), ("diff.relative", "true")):
            self.g("config", k, v)
        sha = self.merge_side(extra=True)
        self.assertEqual(self.c.off_main("lane-a"), ("lane-a content not on main (h.txt)", sha))
        self.g("config", "log.diffMerges", "off")
        self.assertEqual(self.c.off_main("lane-a"), ("lane-a content not on main (h.txt)", sha))
        self.g("checkout", "-q", "-b", "lane-b", "main")
        base = self.g("rev-parse", "main")
        self.g("update-index", "--add", "--cacheinfo", f"160000,{base},sub")  # a gitlink: ignoreSubmodules hides it
        self.g("commit", "-q", "-m", "gitlink")
        gl = self.g("rev-parse", "HEAD")
        self.g("checkout", "-q", "main")
        self.assertEqual(self.c.off_main("lane-b")[1], gl)
        self.g("-c", "diff.ignoreSubmodules=none", "cherry-pick", gl)
        self.assertIsNone(self.c.off_main("lane-b"))  # linear and picked: still removable
        (self.wt / "u.txt").write_text("untracked\n")  # showUntrackedFiles=no would hide it
        self.assertIn("not clean", self.c.remove_worktree("A", {"cleanup": {}, "worktree": str(self.wt)}))
        self.assertTrue(self.wt.exists())

    def test_branch_moved_after_proof_is_not_deleted(self):
        self.g("cherry-pick", self.lane_commit())
        self.g("worktree", "remove", str(self.wt))
        other = self.g("commit-tree", "-p", "main", "-m", "late", self.g("rev-parse", "main^{tree}"))
        proof = self.c.off_main

        def off_then_move(branch, tip=None):
            r = proof(branch, tip)
            self.g("update-ref", "refs/heads/lane-a", other)  # a lane commits between proof and delete
            return r
        self.c.off_main = off_then_move
        self.assertIn("update-ref -d lane-a rc=", self.c.remove_worktree("A", {"cleanup": {"branch": "lane-a"}}))
        self.assertEqual(self.g("rev-parse", "refs/heads/lane-a"), other)

    def test_branch_symref_to_main_never_deletes_main(self):
        self.g("worktree", "remove", str(self.wt))
        main = self.g("rev-parse", "main")
        self.g("symbolic-ref", "refs/heads/lane-a", "refs/heads/main")  # resolves to main's tip: "on main"
        self.assertIn("symbolic ref", self.c.remove_worktree("A", {"cleanup": {"branch": "lane-a"}}))
        self.assertEqual(self.g("rev-parse", "refs/heads/main"), main)
        self.assertEqual(self.g("symbolic-ref", "refs/heads/lane-a"), "refs/heads/main")

    def test_skip_worktree_or_assume_unchanged_edit_keeps_worktree(self):
        for flag in ("--skip-worktree", "--assume-unchanged"):
            self.g("update-index", flag, "f.txt", cwd=self.wt)
            (self.wt / "f.txt").write_text("hidden edit\n")  # git status shows nothing
            self.assertIn("skip-worktree", self.c.remove_worktree("A", {"cleanup": {}, "worktree": str(self.wt)}))
            self.assertEqual((self.wt / "f.txt").read_text(), "hidden edit\n")
            self.g("update-index", flag.replace("--", "--no-"), "f.txt", cwd=self.wt)
            (self.wt / "f.txt").write_text("base\n")

    def test_fsmonitor_config_cannot_hide_dirty_files(self):
        self.g("config", "core.fsmonitor", "true", cwd=self.wt)  # a hook that would report nothing
        (self.wt / "f.txt").write_text("edit\n")
        self.assertIn("not clean", self.c.remove_worktree("A", {"cleanup": {}, "worktree": str(self.wt)}))
        self.assertTrue(self.wt.exists())

    def test_merge_that_drops_picked_work_is_preserved(self):
        self.merge_side(extra=False)
        self.g("rm", "-q", "g.txt", cwd=self.wt)  # the merge undoes the side work main picked
        self.g("commit", "-q", "--amend", "--no-edit", cwd=self.wt)
        sha = self.g("rev-parse", "HEAD", cwd=self.wt)
        self.assertEqual(self.c.off_main("lane-a"), ("lane-a content not on main (g.txt)", sha))

    def test_whitespace_only_pick_is_not_integrated(self):
        self.lane_commit()
        (self.root / "g.txt").write_text("work  \n")  # cherry's patch-id ignores whitespace
        self.g("add", "g.txt")
        self.g("commit", "-q", "-m", "work")
        self.assertIn("(g.txt)", self.c.off_main("lane-a")[0])

    def test_tags_cannot_shadow_branch_or_main(self):
        sha = self.lane_commit()
        self.g("tag", "lane-a", "main")
        self.g("tag", "main", sha)
        self.assertEqual(self.c.off_main("lane-a"), (f"lane-a has commits not on main (first {sha[:7]})", sha))

    def test_partial_pick_is_preserved(self):
        self.g("cherry-pick", self.lane_commit())
        (self.wt / "h.txt").write_text("more\n")
        self.g("add", "h.txt", cwd=self.wt)
        self.g("commit", "-q", "-m", "more", cwd=self.wt)
        sha = self.g("rev-parse", "HEAD", cwd=self.wt)
        self.run_cleanup()
        self.assertEqual(self.assert_preserved_once()["first_unintegrated_sha"], sha)
        self.assertTrue(self.wt.exists())

    def test_missing_ref_or_git_error_mid_proof_is_not_integrated(self):
        self.assertIn("rc=", self.c.off_main("no-such-branch")[0])
        sha = self.merge_side(extra=False)
        self.assertEqual(self.c.off_main("lane-a"), (f"lane-a has merge commits (first {sha[:7]})", sha))
        self.git_fail = (128, "fatal: boom")
        for step in (["git", "rev-list"], ["git", "log", "-m"], ["git", "diff", "--name-only"],
                     ["git", "rev-list", "--merges"]):
            self.git_fail_on = step
            reason, first = self.c.off_main("lane-a")
            self.assertIn("rc=128", reason)
            self.assertEqual(first, None if step == ["git", "rev-list"] else sha)
        self.git_fail, self.git_fail_on = (0, "? junk\n"), ["git", "cherry"]
        self.assertIn("unparsable", self.c.off_main("lane-a")[0])

    def test_git_failure_is_preserved(self):
        self.git_fail = (128, "fatal: boom")
        self.run_cleanup()
        self.assertIsNone(self.assert_preserved_once()["branch"])

    def test_unknown_branch_is_preserved(self):
        self.g("worktree", "remove", "--force", str(self.wt))
        self.run_cleanup()
        self.assertIn("unknown", self.assert_preserved_once()["reason"])

    def test_rm_refused_for_integrated_branch_is_retried_next_day(self):
        self.fake.rm_rc = 1
        sid = self.run_cleanup()
        self.assertEqual(self.cli("rm"), [["rm", sid]])
        self.assertIn("refused", self.lanes()["A"]["cleanup"]["reason"])
        self.assertNotIn("preserved", self.lanes()["A"])
        for _ in range(3):
            self.t += 60
            self.c.pass_once()
        self.assertEqual(len(self.cli("rm")), 1)
        self.assertEqual([i["id"] for i in self.status()["attention"] if i["id"].startswith("cleanup:")],
                         ["cleanup:A"])
        self.assertEqual(len([e for e in self.events() if e["event"] == "cleanup_refused"]), 1)
        self.fake.rm_rc = 0
        self.t = self.lanes()["A"]["cleanup"]["retry_ts"]
        self.c.pass_once()
        self.assertEqual(len(self.cli("rm")), 2)
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")

    def cleanup_ids(self):
        return [i["id"] for i in self.status()["attention"] if i["id"].startswith("cleanup:")]

    def test_rm_refused_for_commits_in_main_is_retried_but_not_listed(self):
        """CONVPAGE: unpushed commits already on local main only wait for the next push."""
        sha = self.lane_commit()
        self.g("cherry-pick", sha)
        self.fake.rm_rc = 1
        self.fake.rm_out = ("Worktree has unpushed commits. To discard the worktree and its commits: "
                            f"claude rm {{sid}} --discard-unpushed {sha}@abc1234\n")
        self.run_cleanup()
        self.assertIs(self.lanes()["A"]["cleanup"]["in_main"], True)
        self.assertEqual(self.cleanup_ids(), [])
        self.fake.rm_rc = 0
        self.t = self.lanes()["A"]["cleanup"]["retry_ts"]
        self.c.pass_once()
        self.assertEqual(len(self.cli("rm")), 2)
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")
        self.assertNotIn("in_main", self.lanes()["A"]["cleanup"])

    def test_rm_refused_for_commits_not_in_main_is_listed(self):
        self.fake.rm_rc = 1
        self.fake.rm_out = ("To discard the worktree and its commits: "
                            f"claude rm {{sid}} --discard-unpushed {'d' * 40}@abc1234")
        self.run_cleanup()
        self.assertIs(self.lanes()["A"]["cleanup"]["in_main"], False)
        self.assertEqual(self.cleanup_ids(), ["cleanup:A"])

    def test_cleanup_refused_and_preserved_never_page(self):
        self.fake.rm_rc = 1
        self.run_cleanup()
        self.assertEqual(self.cleanup_ids(), ["cleanup:A"])
        self.t += conveyor.ATTENTION_PAGE_S + 60
        self.c.pass_once()
        self.assertNotIn("cleanup:A", "\n".join(self.pushes))
        self.assertEqual(self.cleanup_ids(), ["cleanup:A"])  # still on the board

    def registry_pages(self):
        text = "\n".join(self.pushes + self.c.load_pager().get("pending", []))
        return text.count("unreadable session record")

    def test_rm_unreadable_record_is_one_global_item_paged_once_a_day(self):
        """RMREADALERT: the unreadable-record refusal is one cleanup-registry item, paged once per
        24 h, never a per-lane cleanup item; success clears it."""
        self.fake.rm_rc = 1
        self.fake.rm_out = ("Couldn’t verify that no other session’s record names this directory "
                            "(a session record, or the directory itself, couldn’t be read)")
        self.run_cleanup()
        ids = [i["id"] for i in self.status()["attention"]]
        self.assertIn(conveyor.CLEANUP_REGISTRY_ID, ids)
        self.assertEqual(self.cleanup_ids(), [])
        self.assertEqual(len([e for e in self.events() if e["event"] == "cleanup_refused"]), 1)
        self.assertEqual(self.registry_pages(), 1)
        self.t += 3600  # the same refusal within 24 h of the page: no second page
        self.assertTrue(self.c.rm_registry(self.fake.rm_out))
        self.assertEqual(self.registry_pages(), 1)
        self.t = self.lanes()["A"]["cleanup"]["retry_ts"]  # the daily retry: 24 h since the page
        self.c.pass_once()
        self.assertEqual(len(self.cli("rm")), 2)
        self.assertEqual(self.registry_pages(), 2)
        self.assertEqual(self.cleanup_ids(), [])
        self.fake.rm_rc = 0
        self.t = self.lanes()["A"]["cleanup"]["retry_ts"]
        self.c.pass_once()
        self.assertEqual(self.lanes()["A"]["cleanup"]["result"], "removed")
        self.assertNotIn(conveyor.CLEANUP_REGISTRY_ID, [i["id"] for i in self.status()["attention"]])

    def test_rm_no_job_matching_and_not_listed_is_removed(self):
        self.fake.rm_rc = 1
        self.fake.rm_out = "No job matching '{sid}'"
        run = self.c.run

        def gone(args, **kw):
            r = run(args, **kw)
            if args[0] == "rm":  # the orchestrator stays listed: an empty listing proves nothing
                self.fake.agents = [a for a in self.fake.agents if a["id"] != args[1]] + [
                    {"id": "ffff0002", "name": "orchestrator", "kind": "interactive",
                     "state": "running", "status": "busy", "pid": 2, "sessionId": "ffff0002-x"}]
            return r
        self.c.run = gone
        self.run_cleanup()
        c = self.lanes()["A"]["cleanup"]
        self.assertEqual(c["result"], "removed")
        self.assertNotIn("retry_ts", c)
        self.assertEqual(self.cleanup_ids(), [])
        self.assertNotIn("--discard-unpushed", " ".join(" ".join(x) for x in self.cli("rm")))

    def test_rm_no_job_matching_while_listed_is_refused(self):
        self.fake.rm_rc = 1
        self.fake.rm_out = "No job matching '{sid}'"
        self.run_cleanup()
        self.assertIn("retry_ts", self.lanes()["A"]["cleanup"])
        self.assertEqual(self.cleanup_ids(), ["cleanup:A"])

    def test_preserved_never_pages(self):
        self.lane_commit()
        self.run_cleanup()
        self.assert_preserved_once()
        self.t += conveyor.ATTENTION_PAGE_S + 60
        self.c.pass_once()
        self.assertNotIn("preserved:A", "\n".join(self.pushes))

    def test_ack_name_is_windows_safe(self):
        """CONVFIX61 F3: the ack for cleanup:A is ack_name('cleanup:A'); cleanup_A (a lane id's
        ack name) does not clear it."""
        self.fake.rm_rc = 1
        self.run_cleanup()
        self.assertEqual(self.cleanup_ids(), ["cleanup:A"])
        (self.base / "ack").mkdir(exist_ok=True)
        for name, left in (("cleanup_A", ["cleanup:A"]), (conveyor.ack_name("cleanup:A"), [])):
            (self.base / "ack" / name).write_text("")
            os.utime(self.base / "ack" / name, (self.t + 5, self.t + 5))
            self.c.pass_once()
            self.assertEqual(self.cleanup_ids(), left)

    def test_ack_name_is_injective_and_plain_file_name(self):
        """CONVFIX61 F3: every non-plain id is '+'+hex; no two sample ids share a name and no
        name holds a Windows-forbidden char, a separator, a trailing dot/space or a device name."""
        ids = ["A", "INT61", "dry-x", "a", "cleanup:A", "cleanup_A", "preserved:A", ".", "..", "",
               "A.", "A ", " A", "CON", "nul", "com1.txt", "Lpt9", "İ", "ş", "+41", "+",
               "a/b", "a\\b", "../x", "x\x00", "A\x1f", "\ud800"] + list('<>:"/\\|?*')
        names = [conveyor.ack_name(i) for i in ids]
        self.assertEqual(len(set(names)), len(ids))
        for i in ("A", "INT61", "dry-x", "a", "cleanup_A"):
            self.assertEqual(conveyor.ack_name(i), i)  # plain ids keep their old ack name
        for i, n in zip(ids, names):
            with self.subTest(id=i):
                self.assertRegex(n, r"\A[A-Za-z0-9+][A-Za-z0-9_.-]*\Z")
                self.assertFalse(n.endswith("."))
                self.assertNotRegex(n, r"(?i)\A(CON|PRN|AUX|NUL|COM\d|LPT\d)(\..*)?\Z")
                self.assertTrue(n == i or n.startswith("+"))
        p = self.base / "ack"
        p.mkdir(exist_ok=True)
        for n in names:  # every name is one file inside ack/
            (p / n).write_text("")
            self.assertEqual((p / n).resolve().parent, p.resolve())


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

    def test_busy_foreign_young_working_never_stopped(self):
        self.fake.agents.append(  # not started by the conveyor: same lane name, alive, idle
            {"id": "ffff0001", "name": "A", "kind": "background", "state": "done", "status": "idle",
             "pid": 1, "sessionId": "ffff0001-x"})
        self.finished(agent={"status": "busy"})
        self.finished("C", agent={"state": "working"})  # idle, report under STOP_WORKING_S old
        for _ in range(3):
            self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.assertNotIn("stopped", self.lanes()["A"])
        self.fake.agent("A").update(status="idle")  # no longer busy: stopped now, foreign one never
        self.c.pass_once()
        self.assertEqual(self.cli("stop"), [["stop", self.lanes()["A"]["id"]]])
        self.assertIn("pid", next(a for a in self.fake.agents if a["id"] == "ffff0001"))

    def test_attention_lane_stopped_never_removed(self):
        b = self.finished("B", "BLOCKED")  # in attention until acked
        self.assertEqual(self.cli("stop", "rm"), [["stop", b]])
        self.assertEqual(self.lanes()["B"]["early_stopped"], [b])
        self.fake.agent("B").update(state="stopped")
        self.t += conveyor.CLEANUP_S
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [["stop", b]])

    def test_idle_working_stopped_once_after_stop_working_s(self):  # CONVIDLE
        sid = self.finished(agent={"state": "working"})
        self.t += conveyor.STOP_WORKING_S - 1
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.t += 1
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [["stop", sid]])
        for _ in range(3):
            self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [["stop", sid]])
        self.assertEqual(self.lanes()["A"]["early_stopped"], [sid])

    def test_idle_working_stopped_at_180_s(self):  # CLEANPATCH: 600 -> 180
        self.assertEqual(conveyor.STOP_WORKING_S, 180)
        sid = self.finished(agent={"state": "working"})
        self.t += 179
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.t += 1
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [["stop", sid]])

    def test_busy_or_unknown_status_working_left(self):
        self.finished(agent={"state": "working", "status": "busy"})
        self.finished("B", agent={"state": "working"})
        self.fake.agent("B").pop("status")
        self.t += conveyor.STOP_WORKING_S
        for _ in range(2):
            self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.assertNotIn("stopped", self.lanes()["A"])

    def test_working_without_collected_report_left(self):
        self.finished(agent={"state": "working"})
        self.lanes_edit(lambda d: d["A"].pop("report"))
        self.t += conveyor.STOP_WORKING_S
        self.c.pass_once()
        self.assertEqual(self.cli("stop", "rm"), [])
        self.assertNotIn("stopped", self.lanes()["A"])

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
        self.assertEqual(self.status()["breaker"], "closed")  # BREAKERV2: the chain stops, not every lane
        (ev,) = self.ev("auto_fix_failed")
        self.assertEqual((ev["lane"], ev["fix"], ev["status"]), ("INT9", "INT9-R", "BLOCKED"))
        # the orchestrator runs INT9 again by hand and it blocks again: attention only
        self.int_lane()
        self.finish("INT9", "BLOCKED", f"FAILURE: {FAILING}\n")
        self.assertEqual(len(self.ev("auto_fix")), 1)
        self.assertEqual(self.ev("auto_fix_skipped")[-1]["reason"], "auto-fix already used for this integration lane")
        self.assertEqual(self.fake.bg_names().count("INT9-AFX"), 1)

    def test_failed_fix_stops_the_chain_and_pages_without_the_breaker(self):
        """BREAKERV2 (EVTAUDIT R3): a failed -AFX stops its chain and re-raises INT9 with the
        fix lane's files; the breaker stays closed and other lanes keep dispatching."""
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        self.blocked_int()
        self.finish("INT9-AFX", "BLOCKED")
        self.c.pass_once()
        self.assertEqual(self.status()["breaker"], "closed")
        self.assertEqual(self.ev("breaker"), [])
        self.assertNotIn(conveyor.BREAKER_ID, self.attn())
        (ev,) = self.ev("auto_fix_failed")
        self.assertEqual((ev["lane"], ev["fix"], ev["status"], ev["files"]),
                         ("INT9", "INT9-AFX", "BLOCKED", ["jarvis/core.py", "scripts/x.py"]))
        self.assertNotIn("INT9-R", self.fake.bg_names())
        self.assertEqual(self.view()["INT9-R"], "dead")
        (item,) = [a for a in self.status()["attention"] if a["id"] == "INT9"]
        self.assertIn("auto-fix failed: INT9-AFX BLOCKED; chain stopped, files: jarvis/core.py, scripts/x.py",
                      item["reason"])
        self.assertNotIn("INT9", self.c.quiet)  # a blocking item: it pages
        self.drop("p.md", HDR.format("P"))
        self.c.pass_once()
        self.assertIn("P", self.fake.bg_names())
        self.t += conveyor.ATTENTION_PAGE_S
        self.c.pass_once()
        self.assertTrue(any("INT9" in p for p in self.pushes))

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

    def test_root_id(self):
        for name, root in (("INT5-2-AFX", "INT5"), ("int5-r", "INT5"), ("INT5-S", "INT5"),
                           ("INT5-AFX-R", "INT5"), ("-R", "-R"), ("A", "A"), ("A-2", "A-2")):
            self.assertEqual(conveyor.root_id(name), root, name)

    def test_one_root_counts_once(self):
        """BREAKERV2: a lane, its -AFX/-R and a second BLOCKED report of the same lane are one
        bad finish; review/dry lanes never count; an unknown name is its own root."""
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        self.blocked("A", "A-AFX", "A-R", "CDX1", "DRY1")
        self.finish("A", "BLOCKED")  # the same lane reports BLOCKED again
        self.assertEqual(self.status()["breaker"], "closed")
        self.blocked("ZZ")
        self.assertEqual(self.status()["breaker"], "closed")
        self.blocked("Q-7")  # any other name is its own root and counts (fail closed)
        self.assertEqual(self.status()["breaker"], "open")
        self.assertRegex(self.ev("breaker")[0]["reason"], r"^3 lanes .* within 2 h: A(-R)?, ZZ, Q-7$")

    def test_a_later_good_finish_closes_the_breaker(self):
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        self.drop("g.md", HDR.format("G"))
        self.drop("h.md", HDR.format("H"))
        self.c.pass_once()
        self.blocked("A", "B")
        self.drop("c.md", HDR.format("C"))
        self.c.pass_once()
        self.t += 60
        self.report("C", "BLOCKED", mtime=self.t)
        self.report("H", "PASS", mtime=self.t)  # GOOD in the pass that opens it: not after it
        self.c.pass_once()
        self.t += 60
        self.assertEqual(self.status()["breaker"], "open")
        self.finish("G", "PASS")
        self.assertEqual(self.status()["breaker"], "closed")
        self.assertNotIn(conveyor.BREAKER_ID, self.attn())
        self.assertEqual([(e["state"], e["reason"]) for e in self.ev("breaker")][1:], [("closed", "auto: G PASS")])
        self.blocked("D")  # the count starts again
        self.assertEqual(self.status()["breaker"], "closed")

    def open_with(self, *running):
        """Dispatch `running`, then A, B and C go BLOCKED one after the other: the breaker opens."""
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")
        for n in running:
            self.drop(f"{n}.md", HDR.format(n))
        self.c.pass_once()
        self.t += 60
        self.blocked("A", "B", "C")
        self.assertEqual(self.status()["breaker"], "open")

    def ap(self):
        return json.loads((self.base / "autopilot.json").read_text(encoding="utf-8"))

    def test_an_opener_root_or_review_lane_pass_leaves_it_open(self):  # BREAKERFIX F1
        self.open_with("A-R", "CDX9", "DRY9", "G")
        self.assertEqual(self.ap()["breaker"]["roots"], ["A", "B", "C"])
        for n in ("A-R", "CDX9", "DRY9"):
            self.finish(n, "PASS")
            self.assertEqual(self.status()["breaker"], "open", n)
        self.finish("G", "PASS")  # an unrelated root closes it
        self.assertEqual(self.status()["breaker"], "closed")
        self.assertEqual([(e["state"], e["reason"]) for e in self.ev("breaker")][1:], [("closed", "auto: G PASS")])

    def test_a_root_that_went_bad_while_open_cannot_close_it(self):  # BREAKERFIX F1
        self.open_with("D", "D-R", "G")
        self.finish("D", "BLOCKED")
        self.assertEqual(self.ap()["breaker"]["roots"], ["A", "B", "C", "D"])
        self.finish("D-R", "PASS")
        self.assertEqual(self.status()["breaker"], "open")
        self.finish("G", "PASS")
        self.assertEqual(self.status()["breaker"], "closed")

    def test_a_record_without_roots_closes_by_reset_only(self):  # BREAKERFIX F1 fail closed
        self.open_with("G")
        ap = self.ap()
        del ap["breaker"]["roots"]
        (self.base / "autopilot.json").write_text(json.dumps(ap), encoding="utf-8")
        self.finish("G", "PASS")
        self.assertEqual(self.status()["breaker"], "open")
        self.assertNotIn("roots", self.ap()["breaker"])

    def test_one_pass_gives_the_same_result_in_any_lane_order(self):  # BREAKERFIX F3
        seen = []
        for rev in (False, True):
            with self.subTest(reversed=rev):
                self.tearDown()
                self.setUp()
                self.open_with("D", "G", "A-R")
                self.report("G", "PASS", mtime=self.t)
                self.report("D", "BLOCKED", mtime=self.t - 30)
                self.report("A-R", "PASS", mtime=self.t - 20)
                lanes = self.lanes()
                (self.base / "lanes.json").write_text(json.dumps(dict(sorted(lanes.items(), reverse=rev))))
                self.c.pass_once()
                ap = self.ap()
                seen.append((self.status()["breaker"], ap.get("breaker"), ap["bad"],
                             [(e["state"], e["reason"]) for e in self.ev("breaker")]))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(seen[0][0], "closed")  # D's BLOCKED came first, then G closed it


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


SHA_A, SHA_B = "a1" + "0" * 38, "b2" + "0" * 38  # two integration commits stacked on ORIGIN


class StackGit(FakeGit):
    """FakeGit over one line of commits (oldest first): ancestry and diffs for any pair; a push
    moves origin/main."""

    def __init__(self, line):
        super().__init__()
        self.line = line
        self.memory = {s: SCHEMA for s in line}
        self.runs = []

    def __call__(self, args, timeout=120):
        a = tuple(args)
        if self.raise_on and a[:len(self.raise_on)] == self.raise_on or any(a[:len(k)] == k for k in self.fail):
            return super().__call__(args, timeout)
        pos = {s: i for i, s in enumerate(self.line)}
        refs = {"refs/remotes/origin/main": self.origin, "refs/heads/main": self.main}
        if a[:3] == ("git", "merge-base", "--is-ancestor"):
            self.calls.append(args)
            x, y = (refs.get(v, v) for v in a[3:5])
            return (0 if pos[x] <= pos[y] else 1, "") if x in pos and y in pos else (128, "fatal: bad object")
        if a[:3] == ("git", "diff", "--name-only"):
            self.calls.append(args)
            return 0, self.diff
        if a[:2] == ("git", "push"):
            self.calls.append(args)
            self.origin = a[3].split(":")[0]
            return 0, "ok"
        return super().__call__(args, timeout)

    def ci(self, sha, conclusion="success"):
        """The newest `tests` run for sha: completed with `conclusion`."""
        self.runs.insert(0, {"headSha": sha, "status": "completed", "conclusion": conclusion,
                             "workflowName": "tests", "url": f"https://github.com/x/y/actions/runs/{self.line.index(sha)}0"})


SHA_C = "c3" + "0" * 38  # a sibling candidate: a child of ORIGIN off the line


class CandGit(StackGit):
    """StackGit with the main checkout (clean, on main), `git merge --ff-only`, a failed-run log,
    the candidate's picks, and SHA_C forked from ORIGIN."""

    def __init__(self):
        super().__init__([ORIGIN, SHA_A, SHA_B])
        self.memory[SHA_C] = SCHEMA
        self.dirty, self.failed_log, self.picks_log = "", "", ""

    def __call__(self, args, timeout=120):
        a = tuple(args)
        if a[:3] == ("git", "merge-base", "--is-ancestor"):
            refs = {"HEAD": self.main, "refs/heads/main": self.main, "refs/remotes/origin/main": self.origin}
            x, y = (refs.get(v, v) for v in a[3:5])
            if SHA_C in (x, y):
                self.calls.append(args)
                return (0 if x == y or (x, y) == (ORIGIN, SHA_C) else 1), ""
            args = [*a[:3], x, y]
        answers = {("git", "symbolic-ref"): (0, "refs/heads/main\n"), ("git", "status"): (0, self.dirty),
                   ("gh", "run", "view"): (0, self.failed_log), ("git", "log", "--reverse"): (0, self.picks_log)}
        for k, v in answers.items():
            if a[:len(k)] == k:
                self.calls.append(args)
                return v
        if a[:3] == ("git", "merge", "--ff-only"):
            self.calls.append(args)
            self.main = a[3]
            return 0, ""
        return super().__call__(args, timeout)

    def ci(self, sha, conclusion="success"):
        self.runs.insert(0, {"headSha": sha, "status": "completed", "conclusion": conclusion, "workflowName": "tests",
                             "url": f"https://github.com/x/y/actions/runs/{(self.line + [SHA_C]).index(sha)}0"})


class TestCandidate(AutopilotBase):
    """CONVINT: an INT lane reports a CANDIDATE SHA on top of local main; only CI green moves main."""

    def setUp(self):
        super().setUp()
        self.git = self.c.cmd = CandGit()

    def cand(self, name, sha, main=ORIGIN):
        self.drop(f"{name}.md", INT_HDR.format(name) + "INTEGRATE\n")
        self.c.pass_once()
        self.git.main = main
        self.t += 60
        self.finish(name, "CI_PENDING", f"CANDIDATE SHA: {sha}\nCI: pushed ci/{name.lower()}, run in progress\n")

    def reason(self, name):
        (item,) = [a for a in self.status()["attention"] if a["id"] == name]
        return item["reason"]

    def test_green_candidate_fast_forwards_main_then_pushes(self):
        self.cand("INT1", SHA_B)
        ln = self.lanes()["INT1"]
        self.assertEqual((ln["status"], ln["ci"]["sha"], ln["ci"]["cand"]), ("CI_PENDING", SHA_B, True))
        self.assertEqual((self.git.main, self.git.pushes()), (ORIGIN, []))  # main never holds a pending candidate
        self.git.ci(SHA_B)
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1"]["status"], "GREEN")
        self.assertIn(["git", "merge", "--ff-only", SHA_B], self.git.calls)
        self.assertEqual([e["sha"] for e in self.ev("fast_forward")], [SHA_B])
        self.assertEqual((self.git.main, self.git.pushes()), (SHA_B, [["git", "push", "origin", f"{SHA_B}:refs/heads/main"]]))

    def test_dirty_main_checkout_blocks_the_fast_forward_and_the_push(self):
        self.git.dirty = " M jarvis/core.py\n"
        self.cand("INT1", SHA_B)
        self.git.ci(SHA_B)
        self.c.pass_once()
        self.assertEqual((self.git.main, self.git.pushes(), self.ev("fast_forward")), (ORIGIN, [], []))
        self.assertIn("main checkout has local changes", self.ev("push_blocked")[-1]["reason"])

    def test_sibling_candidate_built_on_the_old_main_is_refused(self):
        self.cand("INT1", SHA_A)
        self.cand("INT2", SHA_C)
        self.git.ci(SHA_A)
        self.git.ci(SHA_C)
        self.c.pass_once()
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{SHA_A}:refs/heads/main"]])
        self.assertEqual(self.git.main, SHA_A)
        self.assertNotIn(["git", "merge", "--ff-only", SHA_C], self.git.calls)
        self.assertIn(f"is not {SHA_C}", self.ev("push_blocked")[-1]["reason"])

    def test_candidate_not_on_top_of_local_main_blocks(self):
        self.cand("INT1", SHA_A, main=SHA_B)
        self.assertEqual(self.lanes()["INT1"]["status"], "BLOCKED")
        self.assertIn(f"CANDIDATE SHA {SHA_A[:7]} is not on top of local main", self.reason("INT1"))

    def test_red_candidate_splits_and_names_every_pick(self):
        self.git.failed_log = "FAIL: test_x (tests.test_core.TestCore.test_x)\n"
        self.git.picks_log = f"@@{SHA_A} feat: core\njarvis/core.py\n\n@@{SHA_B} feat: ui\njarvis/ui.py\n"
        self.cand("INT1", SHA_B)
        self.git.ci(SHA_B, "failure")
        self.c.pass_once()
        self.assertEqual((self.git.main, self.git.pushes()), (ORIGIN, []))
        (ev,) = self.ev("auto_split")
        self.assertEqual((ev["lane"], ev["fixes"]), ("INT1", ["INT1-1-AFX"]))
        listing = f"INT1-S takes {SHA_B[:7]}; INT1-1-AFX fixes {SHA_A[:7]}"
        self.assertIn(listing, self.reason("INT1"))
        self.assertIn(f"CONVEYOR AUTO-SPLIT: {listing}", (self.root / self.lanes()["INT1"]["report"]).read_text(encoding="utf-8"))
        s = (self.base / "prompts" / "auto" / "INT1-S.md").read_text(encoding="utf-8")
        self.assertIn(f"  {SHA_B} feat: ui", s)
        afx = (self.base / "prompts" / "auto" / "INT1-1-AFX.md").read_text(encoding="utf-8")
        self.assertIn(SHA_A, afx)
        self.assertIn("tests.test_core.TestCore.test_x", afx)

    def test_unmapped_red_candidate_falls_back_to_one_fix_chain(self):
        self.git.failed_log = "ERROR: test_y (tests.test_other.T.test_y)\n"
        self.git.picks_log = f"@@{SHA_A} feat: core\njarvis/core.py\n@@{SHA_B} feat: ui\njarvis/ui.py\n"
        self.cand("INT1", SHA_B)
        self.git.ci(SHA_B, "failure")
        self.c.pass_once()
        self.assertEqual((self.ev("auto_split"), self.git.main), ([], ORIGIN))
        self.assertIn("auto-fix queued: INT1-AFX, then INT1-R", self.reason("INT1"))


class TestCIHandoff(AutopilotBase):
    """CONV12: an integration lane finishes CI_PENDING; the conveyor waits for its GitHub CI."""

    def setUp(self):
        super().setUp()
        self.git = self.c.cmd = StackGit([ORIGIN, SHA_A, SHA_B])

    def int_lane(self, name, main):
        """Dispatch integration lane `name` from the inbox; it then commits up to `main`."""
        self.drop(f"{name}.md", INT_HDR.format(name) + "INTEGRATE\n")
        self.c.pass_once()
        self.assertIn(name, self.fake.bg_names())
        self.git.main = main
        self.t += 60

    def pending(self, name, sha):
        self.finish(name, "CI_PENDING", f"NEW MAIN SHA: {sha}\nCI: pushed ci/{name.lower()}, run in progress\n")

    def text(self, name):
        return Path(self.lanes()[name]["report"]).read_text(encoding="utf-8")

    def test_success_turns_green_writes_key1_and_the_gate_pushes(self):
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        ln = self.lanes()["INT1"]
        self.assertEqual((ln["status"], ln["ci"]["sha"], ln["ci"]["branch"]), ("CI_PENDING", SHA_A, "ci/int1"))
        self.assertEqual([(e["lane"], e["sha"]) for e in self.ev("ci_pending")], [("INT1", SHA_A)])
        self.assertEqual((self.approvals(), self.git.pushes(), self.attn_ex_dry()), ([], [], []))
        self.git.ci(SHA_A)
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1"]["status"], "GREEN")
        self.assertTrue(self.text("INT1").endswith("\nCI: https://github.com/x/y/actions/runs/10 success\n"))
        self.assertIn("STATUS: CI_PENDING\n", self.text("INT1"))  # the lane's lines are untouched
        (ev,) = self.ev("ci_result")
        self.assertEqual((ev["lane"], ev["sha"], ev["result"]), ("INT1", SHA_A, "success"))
        (ev,) = self.ev("auto_approve")
        self.assertEqual(ev["approval"], f"push-{SHA_A}-int1")
        self.assertEqual(self.approvals(), [f"done/push-{SHA_A}-int1"])
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{SHA_A}:refs/heads/main"]])
        self.assertEqual(self.attn_ex_dry(), [])

    def test_failure_or_cancelled_blocks_failure_alone_gets_a_fix_chain(self):
        for conclusion in ("failure", "cancelled"):
            with self.subTest(conclusion):
                self.tearDown()
                self.setUp()
                self.int_lane("INT1", SHA_A)
                self.pending("INT1", SHA_A)
                self.git.ci(SHA_A, conclusion)
                # CONVSAFE / CIRERUN: re-run once, a second cancellation or failure is the result
                self.git.fail[("gh", "run", "rerun")] = (0, "")
                self.c.pass_once()
                self.assertEqual(self.lanes()["INT1"]["status"], "CI_PENDING")
                self.assertEqual([(e["lane"], e["reason"]) for e in self.ev("ci_rerun")],
                                 [("INT1", "first failure" if conclusion == "failure" else "cancelled")])
                self.t += 60
                self.c.pass_once()
                self.assertEqual(self.lanes()["INT1"]["status"], "BLOCKED")
                self.assertEqual(len(self.ev("ci_rerun")), 1)
                line = f"CI: https://github.com/x/y/actions/runs/10 {conclusion}"
                self.assertTrue(self.text("INT1").endswith(f"\n{line}\n"))
                self.assertEqual((self.approvals(), self.git.pushes()), ([], []))
                self.assertEqual(len(self.ev("auto_fix")), conclusion == "failure")  # CONVAUTO
                (item,) = [a for a in self.status()["attention"] if a["id"] == "INT1"]
                self.assertIn(f"STATUS BLOCKED; {line}", item["reason"])
                self.assertIn("auto-fix queued: INT1-AFX, then INT1-R" if conclusion == "failure" else
                              "auto-fix skipped: CI-handoff lane without its own failed CI run", item["reason"])

    def test_ci_failure_queues_one_fix_chain_and_the_rerun_goes_green(self):
        self.git.diff = "jarvis/core.py\nscripts/conveyor.py\n"
        self.int_lane("INT1", SHA_A)  # dispatched while local main is MAIN
        self.pending("INT1", SHA_A)
        self.git.ci(SHA_A, "failure")
        self.c.pass_once()
        (ev,) = self.ev("auto_fix")
        self.assertEqual((ev["lane"], ev["fix"], ev["rerun"], ev["files"]),
                         ("INT1", "INT1-AFX", "INT1-R", ["jarvis/core.py", "scripts/conveyor.py"]))
        self.assertIn(["git", "diff", "--name-only", f"{MAIN}..{SHA_A}"], self.git.calls)  # start..its CI sha
        tpl = self.base / "prompts" / "_autofix_ci_template.md"
        self.assertEqual(tpl.read_text(encoding="utf-8"), conveyor.AUTOFIX_CI_TEMPLATE)
        call = self.fake.bg_calls()[-1]
        self.assertEqual(call[:7], ["--bg", "--name", "INT1-AFX", "--model", "opus", "--effort", "high"])
        for part in ("@jarvis lane=INT1-AFX model=opus effort=high base=main\n",
                     "CI run: https://github.com/x/y/actions/runs/10 (sha " + SHA_A + ", branch ci/int1)",
                     "gh run view 10 --log-failed", "  jarvis/core.py\n  scripts/conveyor.py",
                     f"CURRENT local main\n   ({SHA_A})", "FIX COMMIT: <40-hex sha> or none", "never push"):
            self.assertIn(part, call[-1])
        self.finish("INT1-AFX", "PASS", "FIX COMMIT: x\n")
        call = self.fake.bg_calls()[-1]
        self.assertEqual(call[:3], ["--bg", "--name", "INT1-R"])
        for part in (f"Local main already holds INT1's picks ({SHA_A}; CI run 10 failed)",
                     "CI BRANCH: ci/int1-2", "build/reports/INT1-R.md"):
            self.assertIn(part, call[-1])
        self.assertTrue(call[-1].endswith("INTEGRATE\n"))
        self.git.diff = "jarvis/core.py\n"  # CONVSAFE: a conveyor.py range needs the manual key 1
        self.git.main = SHA_B  # the fix commit on top of the failed sha
        self.pending("INT1-R", SHA_B)
        self.assertEqual(self.lanes()["INT1-R"]["status"], "CI_PENDING")  # not stacked on failed INT1
        self.git.ci(SHA_B)
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1-R"]["status"], "GREEN")
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{SHA_B}:refs/heads/main"]])
        self.assertEqual(len(self.ev("auto_fix")), 1)
        self.assertEqual(self.status()["breaker"], "closed")

    def test_failed_rerun_ci_never_gets_a_second_chain(self):
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.git.ci(SHA_A, "failure")
        self.c.pass_once()
        self.finish("INT1-AFX", "PASS", "FIX COMMIT: none\n")
        self.pending("INT1-R", SHA_A)  # a flake: no fix commit, the same sha on ci/int1-r
        self.git.runs.insert(0, {"headSha": SHA_A, "status": "completed", "conclusion": "failure",
                                 "workflowName": "tests", "url": "https://github.com/x/y/actions/runs/11"})
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1-R"]["status"], "BLOCKED")
        self.assertEqual(len(self.ev("auto_fix")), 1)
        self.assertNotIn("INT1-R-AFX", self.fake.bg_names())
        self.assertEqual(self.fake.bg_names().count("INT1-AFX"), 1)
        self.assertEqual(self.status()["breaker"], "closed")  # BREAKERV2: the chain stops, not every lane
        self.assertEqual(self.ev("auto_fix_failed")[0]["fix"], "INT1-R")
        self.assertEqual(self.git.pushes(), [])

    def reruns(self):
        return [c for c in self.git.calls if c[:3] == ["gh", "run", "rerun"]]

    def first_failure(self):
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.git.ci(SHA_A, "failure")
        self.git.fail[("gh", "run", "rerun")] = (0, "")
        self.c.pass_once()
        self.t += 60

    def test_first_ci_failure_reruns_failed_jobs_once_and_stays_pending(self):  # CIRERUN
        self.first_failure()
        self.assertEqual(self.lanes()["INT1"]["status"], "CI_PENDING")
        self.assertEqual(self.reruns(), [["gh", "run", "rerun", "10", "--failed"]])
        (ev,) = self.ev("ci_rerun")
        self.assertEqual((ev["lane"], ev["sha"], ev["rc"], ev["reason"]), ("INT1", SHA_A, 0, "first failure"))
        self.assertEqual((self.ev("auto_fix"), self.ev("ci_result")), ([], []))
        self.assertEqual((self.approvals(), self.git.pushes()), ([], []))

    def test_rerun_success_turns_green_and_the_gate_pushes(self):  # CIRERUN
        self.first_failure()
        self.git.ci(SHA_A)  # the re-run attempt goes green
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1"]["status"], "GREEN")
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{SHA_A}:refs/heads/main"]])
        self.assertEqual((len(self.reruns()), self.ev("auto_fix")), (1, []))

    def test_second_ci_failure_goes_to_auto_fix_without_another_rerun(self):  # CIRERUN
        self.first_failure()
        self.git.ci(SHA_A, "failure")
        self.c.pass_once()
        self.c.pass_once()  # the flag holds: no rerun on a later pass either
        self.assertEqual(self.lanes()["INT1"]["status"], "BLOCKED")
        self.assertEqual(len(self.reruns()), 1)
        (afx,) = self.ev("auto_fix")
        self.assertEqual((afx["fix"], afx["rerun"]), ("INT1-AFX", "INT1-R"))
        self.assertEqual(self.git.pushes(), [])

    def test_a_rerun_that_does_not_start_goes_to_auto_fix_in_the_same_pass(self):  # CIRERUN fail closed
        for how in ("rc", "raise"):
            with self.subTest(how):
                self.tearDown()
                self.setUp()
                self.int_lane("INT1", SHA_A)
                self.pending("INT1", SHA_A)
                self.git.ci(SHA_A, "failure")
                if how == "rc":
                    self.git.fail[("gh", "run", "rerun")] = (1, "boom")
                else:
                    self.git.raise_on = ("gh", "run", "rerun")
                self.c.pass_once()
                self.assertEqual(self.lanes()["INT1"]["status"], "BLOCKED")
                (ev,) = self.ev("ci_rerun")
                self.assertEqual(ev["rc"], 1 if how == "rc" else "TimeoutError")
                (afx,) = self.ev("auto_fix")
                self.assertEqual(afx["fix"], "INT1-AFX")
                self.assertEqual(self.git.pushes(), [])

    def test_a_failure_after_a_cancel_rerun_is_not_rerun_again(self):  # CIRERUN: one automatic rerun per sha
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.git.ci(SHA_A, "cancelled")
        self.git.fail[("gh", "run", "rerun")] = (0, "")
        self.c.pass_once()
        self.assertEqual(self.reruns(), [["gh", "run", "rerun", "10"]])
        self.assertEqual(self.lanes()["INT1"]["status"], "CI_PENDING")
        self.t += 60
        self.git.ci(SHA_A, "failure")
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1"]["status"], "BLOCKED")
        self.assertEqual(len(self.reruns()), 1)
        self.assertEqual(len(self.ev("ci_rerun")), 1)
        self.assertEqual(len(self.ev("auto_fix")), 1)

    def test_a_second_lane_or_a_redispatch_on_the_same_sha_gets_no_second_rerun(self):  # BREAKERFIX F2
        for who in ("INT2", "INT1"):  # another lane / the same lane with a fresh ci record
            with self.subTest(who):
                self.tearDown()
                self.setUp()
                self.first_failure()
                lanes = self.lanes()
                ln = lanes.pop("INT1")
                del ln["ci"]
                lanes[who] = ln
                (self.base / "lanes.json").write_text(json.dumps(lanes))
                self.git.ci(SHA_A, "failure")
                self.c.pass_once()
                self.assertEqual(self.lanes()[who]["status"], "BLOCKED")
                self.assertEqual(len(self.reruns()), 1)
                self.assertEqual(len(self.ev("ci_rerun")), 1)
                ap = json.loads((self.base / "autopilot.json").read_text(encoding="utf-8"))
                self.assertEqual(ap["ci_rerun"][SHA_A]["lane"], "INT1")

    def test_unreadable_autopilot_state_means_no_rerun(self):  # BREAKERFIX F2 fail closed
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.git.ci(SHA_A, "failure")
        self.git.fail[("gh", "run", "rerun")] = (0, "")
        (self.base / "autopilot.json").write_text("{broken", encoding="utf-8")
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1"]["status"], "BLOCKED")
        self.assertEqual((self.reruns(), self.ev("ci_rerun")), ([], []))

    def test_dependents_of_a_blocked_int_wait_for_its_rerun(self):
        lanes = {"INT1": {"status": "BLOCKED"}, "INT1-R": {"status": "CI_PENDING"}, "F": {"status": "BLOCKED"},
                 "F-R": {"status": "GREEN"}}
        self.assertFalse(self.c.slice_done("INT1", lanes))  # a product lane waits for CI success
        self.assertTrue(self.c.slice_done("INT1", lanes, for_int=True))  # the next INT stacks on it
        lanes["INT1-R"]["status"] = "GREEN"
        self.assertTrue(self.c.slice_done("INT1", lanes))
        lanes["INT1-R"]["status"] = "BLOCKED"
        self.assertFalse(self.c.slice_done("INT1", lanes, for_int=True))
        self.assertFalse(self.c.slice_done("F", lanes))  # not an integration lane: no re-run rule
        self.backlog({"id": "INT1", "files": ["@main-checkout"]}, {"id": "G", "depends_on": ["INT1"]})
        self.c.pass_once()
        self.git.main = SHA_A
        self.pending("INT1", SHA_A)
        self.git.ci(SHA_A, "failure")
        self.c.pass_once()
        self.assertEqual(self.view()["G"], "waiting_deps")
        self.finish("INT1-AFX", "PASS", "FIX COMMIT: x\n")
        self.git.main = SHA_B
        self.pending("INT1-R", SHA_B)
        self.git.ci(SHA_B)
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1-R"]["status"], "GREEN")
        self.assertIn("G", self.fake.bg_names())

    def test_gh_unreadable_keeps_waiting_never_green(self):
        self.int_lane("INT1", SHA_A)
        self.git.fail[("gh",)] = (1, "auth")
        t0 = self.t
        self.pending("INT1", SHA_A)
        for at in (1800, 3600, 5400, 7199, 7200, 9000):
            self.t = t0 + at
            self.c.pass_once()
            self.assertEqual(self.lanes()["INT1"]["status"], "CI_PENDING")
            self.assertEqual(self.git.pushes(), [])
            if at == 7199:
                self.assertEqual(self.pushes, [])  # no page before 2 h
        errors = [e for e in self.events() if e["event"] == "error" and e["message"].startswith("CI wait INT1")]
        self.assertEqual(len(errors), 3)  # at 0, 1 h and 2 h
        self.assertEqual(self.pushes, ["INT1: CI 2 saattir sonuçsuz (gh run list ci/int1 rc=1)"])
        self.git.fail.clear()
        self.git.ci(SHA_A)
        self.t += 60
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1"]["status"], "GREEN")
        self.assertEqual(len(self.git.pushes()), 1)

    def test_next_int_dispatches_and_the_pending_sha_is_pushed_under_it(self):
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.int_lane("INT2", SHA_B)  # starts while INT1 waits, commits on top of SHA_A
        self.assertFalse(self.lanes()["INT2"].get("finished"))
        self.git.ci(SHA_A)
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1"]["status"], "GREEN")
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{SHA_A}:refs/heads/main"]])

    def test_backlog_shows_ci_pending_and_the_next_int_may_depend_on_it(self):
        self.backlog({"id": "INT1", "files": ["@main-checkout"]},
                     {"id": "INT2", "files": ["@main-checkout"], "depends_on": ["INT1"]},
                     {"id": "F", "depends_on": ["INT1"]})
        self.c.pass_once()
        self.assertEqual(self.view(), {"INT1": "running", "INT2": "waiting_deps", "F": "waiting_deps"})
        self.git.main = SHA_A
        self.pending("INT1", SHA_A)
        self.assertEqual(self.view(), {"INT1": "ci_pending", "INT2": "running", "F": "waiting_deps"})
        self.assertEqual(self.fake.bg_names(), ["INT1", "INT2"])

    def test_push_order_holds_with_two_stacked_pending_shas(self):
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.int_lane("INT2", SHA_B)
        self.pending("INT2", SHA_B)
        self.git.ci(SHA_B)  # the later sha's CI finishes first: it waits for SHA_A
        self.c.pass_once()
        self.t += 60
        self.assertEqual(self.lanes()["INT2"]["status"], "CI_PENDING")
        self.assertEqual((self.approvals(), self.git.pushes()), ([], []))
        self.git.ci(SHA_A)
        for _ in range(2):
            self.c.pass_once()
            self.t += 60
        self.assertEqual([c[3] for c in self.git.pushes()], [f"{SHA_A}:refs/heads/main", f"{SHA_B}:refs/heads/main"])
        self.assertEqual([self.lanes()[n]["status"] for n in ("INT1", "INT2")], ["GREEN", "GREEN"])

    def test_hand_key1_stacked_on_a_pending_sha_is_refused(self):
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.int_lane("INT2", SHA_B)
        self.pending("INT2", SHA_B)
        self.git.ci(SHA_B)
        d = self.root / "build" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"push-{SHA_B}-int2").write_text("orchestrator\n", encoding="utf-8")
        self.c.pass_once()
        self.assertEqual(self.git.pushes(), [])
        (ev,) = self.ev("push_blocked")
        self.assertEqual(ev["reason"], f"stacked on INT1 {SHA_A[:7]} whose CI is pending")

    def test_first_fails_then_the_stacked_second_is_blocked(self):
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.int_lane("INT2", SHA_B)
        self.pending("INT2", SHA_B)
        self.git.ci(SHA_A, "failure")
        self.c.pass_once()
        lanes = self.lanes()
        self.assertEqual((lanes["INT1"]["status"], lanes["INT2"]["status"]), ("BLOCKED", "BLOCKED"))
        self.assertEqual(lanes["INT2"]["ci_why"], f"stacked on failed {SHA_A[:7]} (INT1)")
        attn = {a["id"]: a["reason"] for a in self.status()["attention"]}
        self.assertIn(f"stacked on failed {SHA_A[:7]} (INT1)", attn["INT2"])
        self.assertIn("CI: https://github.com/x/y/actions/runs/10 failure", attn["INT1"])
        self.assertEqual([e["lane"] for e in self.ev("auto_fix")], ["INT1"])  # CONVAUTO: one chain, not for INT2
        self.assertIn("auto-fix skipped: CI-handoff lane without its own failed CI run", attn["INT2"])
        self.git.ci(SHA_B)  # its own CI green later: still BLOCKED, never pushed
        self.t += 60
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT2"]["status"], "BLOCKED")
        self.assertEqual((self.approvals(), self.git.pushes()), ([], []))
        self.assertFalse([c for c in self.git.calls if "reset" in c])  # main is the orchestrator's

    def test_the_rerun_is_not_blocked_by_an_int_stacked_on_the_failure(self):
        sha_c = "c3" + "0" * 38  # INT1-R's fix commit on top of INT2's picks
        self.git.line.append(sha_c)
        self.git.memory[sha_c] = SCHEMA
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.int_lane("INT2", SHA_B)
        self.pending("INT2", SHA_B)
        self.git.ci(SHA_A, "failure")
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT2"]["ci"]["stacked_on"], "INT1")
        self.finish("INT1-AFX", "PASS", "FIX COMMIT: x\n")
        self.git.main = sha_c
        self.pending("INT1-R", sha_c)
        self.assertEqual(self.lanes()["INT1-R"]["status"], "CI_PENDING")  # neither INT1 nor INT2 blocks it
        self.git.ci(sha_c)
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1-R"]["status"], "GREEN")
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{sha_c}:refs/heads/main"]])
        self.assertEqual(self.lanes()["INT2"]["status"], "BLOCKED")  # no chain of its own, its item stays
        self.assertEqual([e["lane"] for e in self.ev("auto_fix")], ["INT1"])

    def test_failed_sha_holds_stacked_pushes_until_acked_not_for_a_day(self):
        self.int_lane("INT1", SHA_A)
        self.pending("INT1", SHA_A)
        self.int_lane("INT2", SHA_B)
        self.pending("INT2", SHA_B)
        self.git.ci(SHA_A, "failure")
        self.c.pass_once()
        self.git.ci(SHA_B)
        self.t += 2 * 86400  # older than CI_STACK_S: a failure still counts
        d = self.root / "build" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"push-{SHA_B}-int2").write_text("orchestrator\n", encoding="utf-8")
        self.c.pass_once()
        self.assertEqual(self.git.pushes(), [])
        self.assertEqual(self.ev("push_blocked")[-1]["reason"], f"stacked on INT1 {SHA_A[:7]} whose CI is failed")
        ack = self.base / "ack"
        ack.mkdir(exist_ok=True)
        for n in ("INT1", "INT2"):  # the orchestrator decided (here: keep main, push B by hand)
            (ack / n).write_text("")
            os.utime(ack / n, (self.t, self.t))
        self.t += conveyor.PUSH_RETRY_S
        self.c.pass_once()
        self.assertEqual(self.git.pushes(), [["git", "push", "origin", f"{SHA_B}:refs/heads/main"]])

    def test_bad_handoff_reports_are_blocked(self):
        cases = {"not an integration lane": ("G6X", SHA_A, "not an integration lane"),
                 "no sha line": ("INT1", None, "NEW MAIN SHA"),
                 "sha not on local main": ("INT1", SHA_B, "is not on local main")}
        for label, (name, sha, why) in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                if name.startswith("INT"):
                    self.int_lane(name, SHA_A)
                else:
                    self.drop("g.md", HDR.format(name))
                    self.c.pass_once()
                    self.git.main = SHA_A
                self.finish(name, "CI_PENDING", f"NEW MAIN SHA: {sha}\nci/int1\n" if sha else "ci/int1\n")
                ln = self.lanes()[name]
                self.assertEqual(ln["status"], "BLOCKED")
                self.assertIn(why, ln["ci_why"])
                self.assertEqual((self.approvals(), self.git.pushes()), ([], []))

    def test_plain_green_report_works_as_before(self):
        self.git = self.c.cmd = FakeGit()
        AutopilotBase.int_lane(self)
        self.finish("INT9", "GREEN", GREEN)
        self.assertEqual(self.approvals(), [f"done/push-{MAIN}-p2b"])
        self.assertEqual(self.ev("ci_pending") + self.ev("ci_result"), [])


class TestIdleWatchdog(Base):
    """CONV12: INT38 sat idle after pushing ci/int38 and never wrote its report (2026-10-12)."""

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

    def resumes(self):
        return [c for c in self.fake.calls if c[:2] == ["--bg", "--resume"]]

    def go_idle(self):
        self.a.update(status="idle", cwd=str(self.wt))

    def test_one_nudge_then_stuck(self):
        self.go_idle()
        self.c.pass_once()  # first seen idle: the 15 min start now
        self.t += conveyor.IDLE_NO_REPORT_S - 1
        self.c.pass_once()
        self.assertEqual(self.resumes(), [])
        self.t += 1
        self.c.pass_once()
        (call,) = self.resumes()
        self.assertEqual((call[2], call[-1]), (self.a["sessionId"], conveyor.IDLE_NUDGE))
        (ev,) = [e for e in self.events() if e["event"] == "idle_nudge"]
        self.assertEqual(ev["lane"], "A")
        self.assertNotIn("nudges", self.lanes()["A"])  # the blocked-lane budget is untouched
        self.c.pass_once()  # the resumed turn is busy
        self.go_idle()  # ... and ends again without a report
        self.c.pass_once()
        self.t += conveyor.IDLE_NO_REPORT_S
        self.c.pass_once()
        ln = self.lanes()["A"]
        self.assertEqual((ln["finished"], ln["status"]), (True, "STUCK"))
        self.assertEqual(len(self.resumes()), 1)
        self.assertIn("A", self.attn())
        self.assertTrue(any("STUCK" in p for p in self.pushes))

    def test_busy_session_never_nudged(self):
        for _ in range(8):
            self.t += conveyor.IDLE_NO_REPORT_S
            self.c.pass_once()
        self.assertEqual(self.resumes(), [])
        self.assertFalse(self.lanes()["A"].get("finished"))
        self.assertFalse([e for e in self.events() if e["event"] == "idle_nudge"])


class TestResumeInPlace(Base):
    """CONV12 / LANE_RULES R3: a feature lane BLOCKED only on NEEDS FILES is resumed once."""

    def setUp(self):
        super().setUp()
        self.backlog({"id": "F", "files": ["jarvis/a.py"]}, {"id": "G", "files": ["jarvis/c.py"]})
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["F", "G"])
        self.t += 60

    def resumes(self):
        return [c for c in self.fake.calls if c[:2] == ["--bg", "--resume"]]

    def blocked(self, needs="NEEDS FILES: jarvis/b.py, tests/test_b.py\n"):
        self.report("F", "BLOCKED", mtime=self.t, extra=needs)
        self.c.pass_once()

    def test_eligible_resumes_the_same_session_with_extended_files(self):
        sid = self.fake.agent("F")["sessionId"]
        self.blocked()
        ln = self.lanes()["F"]
        self.assertFalse(ln.get("finished"))
        self.assertEqual(ln["files"], ["jarvis/a.py", "jarvis/b.py", "tests/test_b.py"])
        (call,) = self.resumes()
        note, grant = call[-1].rsplit("\n\n", 1)  # CTXRESUME: the fact note comes first
        self.assertEqual((call[2], grant), (sid, "You may now edit jarvis/b.py, tests/test_b.py "
                         "(orchestrator-approved by rule R3). Finish your goal and rewrite your report."))
        self.assertTrue(note.startswith(conveyor.FACT_HEAD + "\n") and note.endswith(conveyor.FACT_TAIL))
        (ev,) = [e for e in self.events() if e["event"] == "resume_in_place"]
        self.assertEqual(ev["paths"], ["jarvis/b.py", "tests/test_b.py"])
        self.assertNotIn("F", self.attn())
        self.t += 60
        self.c.pass_once()  # the old BLOCKED report is spent
        self.assertFalse(self.lanes()["F"].get("finished"))
        self.report("F", "PASS", mtime=self.t)
        self.c.pass_once()
        self.assertEqual(self.lanes()["F"]["status"], "PASS")

    def test_not_resumed(self):
        cases = {"security path": "NEEDS FILES: jarvis/policy_proxy.py\n",
                 "the conveyor": "NEEDS FILES: scripts/conveyor.py\n",
                 "a running lane's file": "NEEDS FILES: jarvis/c.py\n",
                 "four paths": "NEEDS FILES: a/1.py, a/2.py, a/3.py, a/4.py\n",
                 "a folder": "NEEDS FILES: jarvis/\n",
                 "a folder by dot": "NEEDS FILES: jarvis/.\n",
                 "another blocker": f"NEEDS FILES: jarvis/b.py\nFAILURE: {FAILING}\n",
                 "none": "NEEDS FILES: none\n"}
        for label, needs in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.blocked(needs)
                ln = self.lanes()["F"]
                self.assertEqual((ln["finished"], ln["status"], ln["files"]), (True, "BLOCKED", ["jarvis/a.py"]))
                self.assertEqual(self.resumes(), [])
                (item,) = [a for a in self.status()["attention"] if a["id"] == "F"]
                self.assertIn("resume in place refused: ", item["reason"])

    def test_second_blocked_goes_to_attention(self):
        self.blocked()
        self.t += 60
        self.blocked("NEEDS FILES: jarvis/d.py\n")
        ln = self.lanes()["F"]
        self.assertEqual((ln["finished"], ln["status"]), (True, "BLOCKED"))
        self.assertEqual(len(self.resumes()), 1)
        self.assertIn("F", self.attn())


class TestInterimReport(AutopilotBase):
    """CONVINTERIM: a STATUS line with "(interim)" is not a finish; 45 min without a final
    report -> a normal BLOCKED finish (breaker count, attention item, backstop page)."""
    def start(self):
        self.backlog({"id": "A"}, {"id": "B", "depends_on": ["A"]})
        self.c.pass_once()
        self.t += 60
        self.t0 = self.t
        self.finish("A", "BLOCKED (interim)")

    def bad(self):
        return [x[0] for x in json.loads((self.base / "autopilot.json").read_text())["bad"]]

    def test_interim_is_not_a_finish(self):
        self.start()
        self.t = self.t0 + conveyor.INTERIM_TIMEOUT_S - 60
        self.c.pass_once()
        self.assertFalse(self.lanes()["A"].get("finished"))
        self.assertEqual(self.ev("finish"), [])
        self.assertNotIn("A", self.attn())
        self.assertEqual(self.view()["B"], "waiting_deps")
        self.assertEqual(self.bad(), [])
        self.assertEqual(self.pushes, [])
        self.assertFalse([c for c in self.fake.calls if c[0] in ("stop", "rm")])

    def test_final_pass_within_45_min_is_recorded_as_today(self):
        self.start()
        self.t = self.t0 + conveyor.INTERIM_TIMEOUT_S - 60
        self.finish("A", "PASS")
        ln = self.lanes()["A"]
        self.assertEqual((ln["finished"], ln["status"]), (True, "PASS"))
        self.assertEqual([e["status"] for e in self.ev("finish")], ["PASS"])
        self.assertNotIn("reason", self.ev("finish")[0])
        self.assertNotIn("A", self.attn())
        self.assertNotEqual(self.view()["B"], "waiting_deps")

    def test_no_final_in_45_min_is_blocked_attention_breaker_page(self):
        self.start()
        self.t = self.t0 + 1800
        self.finish("A", "BLOCKED (interim)")  # a rewritten interim report does not reset the clock
        self.t = self.t0 + conveyor.INTERIM_TIMEOUT_S
        self.c.pass_once()
        ln = self.lanes()["A"]
        self.assertEqual((ln["finished"], ln["status"]), (True, "BLOCKED"))
        (ev,) = self.ev("finish")
        self.assertEqual((ev["status"], ev["reason"]), ("BLOCKED", conveyor.INTERIM_WHY))
        (item,) = [a for a in self.status()["attention"] if a["id"] == "A"]
        self.assertEqual(item["reason"], f"STATUS BLOCKED; {conveyor.INTERIM_WHY}")
        self.assertEqual(self.bad(), ["A"])
        self.assertEqual(self.view()["B"], "waiting_deps")
        self.assertEqual(self.pushes, [])
        self.t += conveyor.ATTENTION_PAGE_S
        self.c.pass_once()
        self.assertEqual(len(self.pushes), 1)
        self.assertIn(f"A: STATUS BLOCKED; {conveyor.INTERIM_WHY}", self.pushes[0])

    def test_session_done_on_an_interim_report_still_times_out(self):
        self.start()
        self.fake.agent("A").update(state="done", status="idle")
        self.c.pass_once()
        self.assertFalse(self.lanes()["A"].get("finished"))
        self.assertEqual(self.ev("finish"), [])
        self.t = self.t0 + conveyor.INTERIM_TIMEOUT_S
        self.c.pass_once()
        ln = self.lanes()["A"]
        self.assertEqual((ln["finished"], ln["status"]), (True, "BLOCKED"))
        self.assertEqual(self.bad(), ["A"])

    def test_a_final_report_resets_the_interim_clock(self):
        self.backlog({"id": "F", "files": ["jarvis/a.py"]})
        self.c.pass_once()
        self.t += 60
        self.finish("F", "BLOCKED (interim)")
        self.finish("F", "BLOCKED", extra="NEEDS FILES: jarvis/b.py\n")  # resumed in place (R3)
        self.assertNotIn("interim_since", self.lanes()["F"])
        self.t += conveyor.INTERIM_TIMEOUT_S
        t1 = self.t
        self.finish("F", "BLOCKED (interim)")  # a new clock starts here
        self.t = t1 + conveyor.INTERIM_TIMEOUT_S - 60
        self.c.pass_once()
        ln = self.lanes()["F"]
        self.assertFalse(ln.get("finished"))
        self.assertEqual(ln["interim_since"], t1)

    def test_final_blocked_without_marker_blocks_at_once(self):
        self.backlog({"id": "A"}, {"id": "B", "depends_on": ["A"]})
        self.c.pass_once()
        self.t += 60
        self.finish("A", "BLOCKED", extra="waiting for an interim CI result\n")
        ln = self.lanes()["A"]
        self.assertEqual((ln["finished"], ln["status"]), (True, "BLOCKED"))
        self.assertIn("A", self.attn())
        self.assertEqual(self.bad(), ["A"])


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


class VersionClaude(FakeClaude):
    """FakeClaude that also answers `--version` (a tuple, or an exception to raise)."""

    def __init__(self):
        super().__init__()
        self.version = (0, "2.1.123 (Claude Code)\n")

    def __call__(self, args, timeout=120, cwd=None):
        if args == ["--version"]:
            self.calls.append(args)
            self.version_timeout = timeout
            if isinstance(self.version, Exception):
                raise self.version
            return self.version
        return super().__call__(args, timeout, cwd)


class TestVersionPin(Base):
    """pace.json claude_version (CONVPIN): dispatch only on an equal `claude --version`."""

    WANT = "2.1.123"

    def setUp(self):
        super().setUp()
        self.fake = self.c.run = VersionClaude()

    def pin(self, want=WANT):
        (self.base / "pace.json").write_text(
            json.dumps({**conveyor.PACE_DEFAULTS, "claude_version": want}), encoding="utf-8")

    def version_calls(self):
        return [c for c in self.fake.calls if c == ["--version"]]

    def assert_held(self, have):
        self.assertEqual(self.fake.bg_calls(), [])
        self.assertEqual(len(list((self.base / "inbox").glob("*.md"))), 1)  # the prompt waits
        attn = [(a["id"], a["reason"]) for a in self.status()["attention"]]
        self.assertEqual([a for a in attn if a[0] == conveyor.CC_VERSION_ID],
                         [(conveyor.CC_VERSION_ID, f"cc-version: have {have}, want {self.WANT}")])
        self.assertNotIn("error", [e["event"] for e in self.events()])

    def test_equal_version_dispatches_one_read_per_pass(self):
        self.pin()
        self.drop("a.md", HDR.format("A"))
        self.drop("b.md", HDR.format("B"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A", "B"])
        self.assertEqual(len(self.version_calls()), 1)
        self.assertEqual(self.fake.version_timeout, conveyor.CC_VERSION_TIMEOUT_S)
        self.assertNotIn(conveyor.CC_VERSION_ID, self.attn())

    def test_different_version_no_dispatch_one_attention_item(self):
        self.pin()
        self.fake.version = (0, "2.1.124 (Claude Code)\n")
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.c.pass_once()
        self.assert_held("2.1.124")
        self.assertEqual(len([e for e in self.events() if e["event"] == "cc_version_hold"]), 1)
        self.fake.version = (0, "2.1.123 (Claude Code)\n")  # the orchestrator fixed it
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.assertNotIn(conveyor.CC_VERSION_ID, self.attn())

    def test_unreadable_or_timeout_no_dispatch(self):
        for answer in ((1, "boom"), (0, "garbage"), (0, ""),
                       subprocess.TimeoutExpired(["claude", "--version"], 20), OSError("no exe")):
            with self.subTest(answer=answer):
                self.tearDown()
                self.setUp()
                self.pin()
                self.fake.version = answer
                self.drop("a.md", HDR.format("A"))
                self.c.pass_once()
                self.assert_held("unreadable")

    def test_key_absent_unchanged_behaviour(self):
        self.fake.version = (0, "9.9.9 (Claude Code)\n")
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.assertEqual(self.version_calls(), [])
        self.pin()  # pinned, then unpinned: the attention item goes with the key
        self.fake.version = (1, "boom")
        self.c.pass_once()
        self.assertIn(conveyor.CC_VERSION_ID, self.attn())
        (self.base / "pace.json").write_text(json.dumps(conveyor.PACE_DEFAULTS), encoding="utf-8")
        self.c.pass_once()
        self.assertNotIn(conveyor.CC_VERSION_ID, self.attn())

    def test_mismatch_never_redispatches_or_fails_a_lane(self):
        a, _ = TestRedispatch.setup_respawned(self)
        self.pin()
        self.fake.version = (0, "2.1.124\n")
        self.t += conveyor.IDLE_REDISPATCH_S + 1
        self.c.pass_once()
        self.assertEqual(self.fake.bg_names(), ["A"])
        self.assertNotIn(["stop", a["id"]], self.fake.calls)
        ln = self.lanes()["A"]
        self.assertEqual(ln.get("redispatches", 0), 0)
        self.assertFalse(ln.get("finished"))
        self.assertNotIn(ln.get("status"), ("DEAD", "STUCK"))
        self.assertIn(conveyor.CC_VERSION_ID, self.attn())
        self.assertIsNone(self.c.start("B", "opus", "high", HDR.format("B")))  # start() checks too

    def test_invalid_pin_still_holds_and_survives_quota_reset(self):
        for bad in ("", 2.1, " 2.1.123"):
            with self.subTest(bad=bad):
                self.tearDown()
                self.setUp()
                self.pin(bad)
                self.drop("a.md", HDR.format("A"))
                self.c.pass_once()
                self.assertEqual(self.fake.bg_calls(), [])
                self.assertIn(conveyor.PACE_BAD_ID, self.attn())
                self.assertIn(conveyor.CC_VERSION_ID, self.attn())
        self.tearDown()
        self.setUp()
        self.pin()
        TestUsageGuard.ledger(self, 96)
        self.c.pass_once()
        self.t += 3600
        TestUsageGuard.ledger(self, 56)  # a reset: pace.json is rewritten
        self.c.pass_once()
        pace = json.loads((self.base / "pace.json").read_text(encoding="utf-8"))
        self.assertEqual((pace["mode"], pace["claude_version"]), ("paced", self.WANT))


class TestConvHeal(AutopilotBase):
    """CONVHEAL: self-healing ledger, outage hold, early death, memory hysteresis, findings."""

    def lane(self, name, age=0):
        self.drop(f"{name}.md", HDR.format(name) + "TASK\n", age=age)

    def set_lane(self, name, **kw):
        p = self.base / "lanes.json"
        data = self.lanes()
        data[name].update(kw)
        p.write_text(json.dumps(data), encoding="utf-8")

    def test_report_reader_shapes(self):
        d = self.root / "r"
        d.mkdir()
        shapes = {"# CONVX report\nSTATUS: COMPLETE\n": "COMPLETE", "`STATUS: DONE`\n": "DONE",
                  "STATUS: PASS (local green) and more\n": "PASS", "```\nSTATUS: BLOCKED\n```\n": "BLOCKED",
                  "**STATUS:** GREEN\n": "GREEN", "AMENDED: x\nWRITTEN BY: y\n\nSTATUS: OK\n": "OK",
                  "# STATUS: PASS\n": "PASS", "STATUS: PASS (interim)\n": conveyor.INTERIM,
                  "": None, "x\n" * 16 + "STATUS: PASS\n": None}
        for i, (text, want) in enumerate(shapes.items()):
            p = d / f"{i}.md"
            p.write_text(text, encoding="utf-8")
            self.assertEqual(conveyor.report_status(p), want, text)
        self.assertIsNone(conveyor.report_status(d / "absent.md"))

    def test_lane_ending_without_report_is_missing_with_attention(self):
        self.lane("N")
        self.c.pass_once()
        self.fake.agent("N")["state"] = "done"
        self.t += 60
        self.c.pass_once()
        self.assertEqual(self.lanes()["N"]["status"], "MISSING")
        self.assertIn("N", self.attn())

    def test_dropped_lane_with_later_pass_report_shows_pass(self):
        self.lane("N")
        self.c.pass_once()
        self.set_lane("N", finished=True, status="DROPPED", finished_ts=self.t)
        self.t += 60
        self.report("N", "PASS", mtime=self.t)
        self.c.pass_once()
        self.assertEqual(self.lanes()["N"]["status"], "PASS")
        self.assertTrue(self.ev("heal"))
        self.assertNotIn("N", self.attn())

    def test_blocked_lane_healed_by_continuation_pass(self):
        self.lane("X")
        self.c.pass_once()
        self.set_lane("X", finished=True, status="BLOCKED", finished_ts=self.t)
        self.lane("X2")
        self.t += 60
        self.c.pass_once()
        self.set_lane("X2", finished=True, status="PASS", finished_ts=self.t)
        self.t += 60
        self.c.pass_once()
        self.assertEqual((self.lanes()["X"]["status"], self.lanes()["X"]["healed_by"]), ("PASS", "X2"))
        self.assertNotIn("X", self.attn())

    def test_digit_final_lane_is_not_healed_by_another_number(self):
        self.lane("INT4")
        self.c.pass_once()
        self.set_lane("INT4", finished=True, status="BLOCKED", finished_ts=self.t)
        self.lane("INT41")
        self.t += 60
        self.c.pass_once()
        self.set_lane("INT41", finished=True, status="PASS", finished_ts=self.t)
        self.t += 60
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT4"]["status"], "BLOCKED")
        self.assertIn("INT4", self.attn())

    def test_task_text_never_pauses_and_a_gone_paused_session_releases_the_hold(self):
        self.lane("A")
        self.c.pass_once()
        self.fake.agent("A")["summary"] = "fix authentication: unauthorized replies"
        self.t += 60
        self.c.pass_once()
        self.assertFalse(self.status()["outage_hold"])
        self.fake.agent("A")["error"] = "API Error: 529 overloaded"
        self.t += 60
        self.c.pass_once()
        self.assertTrue(self.status()["outage_hold"])
        self.fake.agents.remove(self.fake.agent("A"))
        self.t += 60
        self.c.pass_once()
        self.t += 60
        self.c.pass_once()
        self.assertNotEqual(self.lanes()["A"].get("state"), "PAUSED")
        self.assertFalse(self.status()["outage_hold"])

    def test_api_error_holds_dispatch_pauses_and_clears_on_progress(self):
        self.lane("A")
        self.c.pass_once()
        self.fake.agent("A")["error"] = "API Error: 529 overloaded"
        self.t += 60
        self.c.pass_once()
        self.assertEqual(self.lanes()["A"]["state"], "PAUSED")
        self.assertTrue(self.status()["outage_hold"])
        self.assertIn("outage", self.attn())
        self.assertEqual(sum("Kesinti" in p for p in self.pushes), 1)
        self.lane("B", age=100)
        self.t += 60
        self.c.pass_once()
        self.assertNotIn("B", self.fake.bg_names())
        self.assertEqual(self.lanes()["A"].get("redispatches", 0), 0)
        self.t += conveyor.OUTAGE_NUDGE_S
        self.c.pass_once()
        self.assertEqual(len(self.ev("outage_nudge")), 1)
        self.assertEqual(sum("Kesinti" in p for p in self.pushes), 1)
        wt = self.root / "wtA"
        wt.mkdir()
        (wt / "f.py").write_text("x", encoding="utf-8")
        os.utime(wt / "f.py", (self.t + 30, self.t + 30))
        self.fake.agent("A")["cwd"] = str(wt)
        self.t += 60
        self.c.pass_once()
        self.assertTrue(self.ev("outage_clear"))
        self.assertFalse(self.status()["outage_hold"])
        self.t += 60
        self.c.pass_once()
        self.assertIn("B", self.fake.bg_names())

    def test_two_idle_lanes_hold_without_redispatch(self):
        (self.base / "pace.json").write_text(json.dumps({**conveyor.PACE_DEFAULTS, "max_lanes": 6}), encoding="utf-8")
        self.lane("A")
        self.lane("B", age=1)
        self.c.pass_once()
        for n in "AB":
            self.fake.agent(n)["status"] = "idle"
        self.t += 60
        self.c.pass_once()
        self.t += conveyor.OUTAGE_IDLE_S + 1
        self.c.pass_once()
        (ev,) = self.ev("outage_hold")
        self.assertEqual(ev["why"], "2 lanes idle, no progress")
        self.assertEqual({self.lanes()[n]["state"] for n in "AB"}, {"PAUSED"})
        self.assertEqual(self.fake.bg_names().count("A"), 1)

    def test_early_death_redispatches_once_in_same_worktree(self):
        run = self.fake
        self.c.run = lambda args, **kw: (0, "stopped") if args[0] == "stop" else run(args, **kw)
        self.lane("N")
        self.c.pass_once()
        lane = self.lanes()["N"]
        self.assertEqual(lane["id"], self.fake.agent("N")["id"])
        self.assertEqual(lane["worktree"], str(self.root / ".claude" / "worktrees" / "N"))
        (self.root / ".claude" / "worktrees" / "N").mkdir(parents=True)
        self.fake.agents.remove(self.fake.agent("N"))
        self.t += conveyor.DEAD_S + 1
        self.c.pass_once()
        self.assertTrue(self.ev("early_death"))
        self.assertEqual(self.fake.bg_names().count("N"), 2)
        self.fake.agents.remove(self.fake.agent("N"))
        self.t += conveyor.DEAD_S + 1
        self.c.pass_once()
        self.assertEqual(len(self.ev("early_death")), 1)
        self.assertEqual(self.fake.bg_names().count("N"), 2)

    def test_memory_hold_hysteresis_and_throttle(self):
        self.memory = (1.0, 20.0)
        self.lane("A")
        self.c.pass_once()
        self.assertNotIn("A", self.fake.bg_names())
        self.memory = (4.0, 20.0)
        self.t += 400
        self.c.pass_once()
        self.assertNotIn("A", self.fake.bg_names())
        self.memory = (4.5, 20.0)
        self.t += 60
        self.c.pass_once()
        self.assertNotIn("A", self.fake.bg_names())
        self.t += conveyor.MEM_OK_S
        self.c.pass_once()
        self.c.pass_once()
        self.assertIn("A", self.fake.bg_names())
        self.lane("B", age=500)
        self.t += 60
        self.c.pass_once()
        self.assertNotIn("B", self.fake.bg_names())
        self.t += conveyor.MEM_GAP_S
        self.c.pass_once()
        self.assertIn("B", self.fake.bg_names())

    def test_cdx_dry_blocked_is_a_finding_not_a_breaker_count(self):
        (self.base / "pace.json").write_text(json.dumps({**conveyor.PACE_DEFAULTS, "max_lanes": 6}), encoding="utf-8")
        names = ["CDX1", "DRY2", "CDX3"]
        for i, n in enumerate(names):
            self.lane(n, age=i)
        self.c.pass_once()
        for n in names:
            self.report(n, "BLOCKED", where=n, mtime=self.t)
        self.t += 60
        self.c.pass_once()
        self.assertEqual(self.status()["breaker"], "closed")
        for n in names:
            self.assertIn(n, self.attn())

    def test_stuck_inbox_prompt_does_not_block_the_rest(self):
        start = self.c.start
        self.c.start = lambda name, *a, **kw: None if name == "X" else start(name, *a, **kw)
        self.lane("X")
        self.lane("Y", age=1)
        self.c.pass_once()
        self.assertIn("inbox:X.md", self.attn())
        self.assertIn("Y", self.fake.bg_names())

    def test_running_files_written_each_pass(self):
        self.lane("N")
        self.c.pass_once()
        data = json.loads((self.base / "running_files.json").read_text(encoding="utf-8"))
        self.assertEqual(data, {"N": []})

    def test_pass_with_findings_raises_attention_without_breaker_or_page(self):
        self.lane("F1")
        self.c.pass_once()
        self.report("F1", "PASS", where="F1", mtime=self.t, extra="FINDINGS: x is broken\n")
        self.t += 60
        self.c.pass_once()
        self.assertIn("F1", self.attn())
        self.assertFalse([p for p in self.pushes if "FINDINGS" in p or "x is broken" in p])
        self.assertEqual(self.status()["breaker"], "closed")
        self.lane("F2", age=100)
        self.c.pass_once()
        self.report("F2", "PASS", where="F2", mtime=self.t, extra="FINDINGS: none\n")
        self.t += 60
        self.c.pass_once()
        self.assertNotIn("F2", self.attn())


class TestConvHealAutoFix(AutopilotBase):
    def setUp(self):
        super().setUp()
        self.git.fail[PICKED] = (0, "jarvis/core.py\nscripts/x.py\n")

    blocked_int = TestAutoFix.blocked_int

    def test_rerun_dead_when_fix_lane_ends_non_good(self):
        self.blocked_int()
        self.finish("INT9-AFX", "BLOCKED")
        self.assertEqual(self.view()["INT9-R"], "dead")

    def test_fix_lane_may_use_needs_files_grant(self):
        self.blocked_int()
        self.finish("INT9-AFX", "BLOCKED", "NEEDS FILES: jarvis/b.py\n")
        (ev,) = self.ev("resume_in_place")
        self.assertEqual(ev["lane"], "INT9-AFX")


class TestConvSafe(AutopilotBase):
    """CONVSAFE: protected paths need the manual key 1; unreadable lanes.json holds; status pages."""

    def setUp(self):
        super().setUp()
        self.git = self.c.cmd = StackGit([ORIGIN, SHA_A, SHA_B])

    def int_lane(self, name, main):
        """Dispatch integration lane `name` from the inbox; it then commits up to `main`."""
        self.drop(f"{name}.md", INT_HDR.format(name) + "INTEGRATE\n")
        self.c.pass_once()
        self.assertIn(name, self.fake.bg_names())
        self.git.main = main
        self.t += 60

    def pending(self, name, sha):
        self.finish(name, "CI_PENDING", f"NEW MAIN SHA: {sha}\nCI: pushed ci/{name.lower()}, run in progress\n")

    def test_protected_path_gets_no_automatic_key1(self):
        for path in ("scripts/conveyor.py", "scripts/ci.ps1", ".github/workflows/tests.yml",
                     ".claude/settings.json", "tests/load_sensitive_list.txt", '"\\303\\274.txt"'):
            with self.subTest(path):
                self.tearDown()
                self.setUp()
                self.git.diff = f"jarvis/core.py\n{path}\n"
                self.int_lane("INT1", SHA_A)
                self.pending("INT1", SHA_A)
                self.git.ci(SHA_A)
                self.c.pass_once()
                self.assertEqual(self.lanes()["INT1"]["status"], "GREEN")
                self.assertEqual((self.ev("auto_approve"), self.approvals(), self.git.pushes()), ([], [], []))

    def test_unreadable_lanes_json_holds_dispatch_and_is_never_overwritten(self):
        f = self.base / "lanes.json"
        f.write_text("{bad", encoding="utf-8")
        self.drop("a.md", HDR.format("A"))
        self.c.pass_once()
        self.assertEqual(f.read_text(encoding="utf-8"), "{bad")
        self.assertEqual(self.fake.bg_names(), [])
        self.assertIn("lanes-invalid", [a["id"] for a in self.status()["attention"]])

    def test_api_outage_and_stale_orchestrator_pages(self):
        def pages(word):
            return [p for p in json.loads(self.c.pager_file.read_text(encoding="utf-8"))["pending"] if word in p]
        lanes = {n: {"paused": True, "api_err_ts": self.t - k} for n, k in (("A", 0), ("B", 300))}
        self.c.pager_watch(lanes)
        self.c.pager_watch(lanes)
        self.assertEqual(len(pages("hesap")), 1)
        self.t += conveyor.API_PAGE_EVERY_S
        self.c.pager_watch(lanes)
        self.assertEqual(len(pages("hesap")), 2)
        hb = self.root / "build" / "orch" / "heartbeat"
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text("")
        os.utime(hb, (self.t - conveyor.ORCH_STALE_S - 60,) * 2)
        self.c.pager_watch({})
        self.c.pager_watch({})
        self.assertEqual(len(pages("devam")), 1)


if __name__ == "__main__":
    unittest.main()
