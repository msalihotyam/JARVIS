"""WORKERADAPT: the codex worker adapter with a FAKE codex exe; no model, network or auth call."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.trusted_exec import sanitized_path
from tests.test_conveyor import HDR, Base, conveyor

_SPEC = importlib.util.spec_from_file_location(
    "conveyor_workers", Path(__file__).resolve().parents[1] / "scripts" / "conveyor_workers.py")
workers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workers)
CodexWorker, check_argv = workers.CodexWorker, workers.check_argv

T0 = 1_800_000_000.0  # the injected clock; never the wall clock


class FakeCodexProc:
    def __init__(self, argv, **kw):
        self.argv, self.kw, self.pid, self.rc, self.terminated = argv, kw, 4242, None, False
        self.stdin_text = kw["stdin"].read().decode("utf-8")

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = 1


FLAGS = ["--ignore-user-config", "-c", "forced_login_method=chatgpt", "-c", "windows.sandbox=mxc"]


class CheckArgvTest(unittest.TestCase):
    OK = ["codex", "exec", "--json", *FLAGS, "-C", "wt", "--sandbox", "workspace-write", "-"]

    def test_default_argv_passes(self):
        self.assertEqual(check_argv(list(self.OK)), self.OK)
        check_argv(["codex", "exec", *FLAGS, "-s", "read-only", "-"])
        check_argv(["codex", "exec", "--sandbox=workspace-write", *FLAGS, "-"])
        # a lane id / worktree path with a banned word is not a flag
        check_argv(["codex", "exec", *FLAGS, "-C", "C:/wt/BYPASSFIX-credit", "--sandbox", "workspace-write", "-"])

    def test_config_is_an_exact_allowlist_of_the_forced_login(self):
        sb = ["--sandbox", "workspace-write", "-"]
        bad = [
            ["codex", "exec", "--ignore-user-config", "-c", "forced_login_method=apikey", *sb],
            ["codex", "exec", "--ignore-user-config", "-c", "model_provider=oss", *sb],
            ["codex", "exec", "--ignore-user-config", "-c", "forced_login_method=chatgpt ", *sb],
            ["codex", "exec", *FLAGS, "-c", "forced_login_method=chatgpt", *sb],  # twice
            ["codex", "exec", "--ignore-user-config", *sb],  # no forced login
            ["codex", "exec", "-c", "forced_login_method=chatgpt", *sb],  # user config loaded
            ["codex", "exec", *FLAGS, "--ignore-user-config", *sb],  # ignore twice
            ["codex", "exec", "--ignore-user-config=false", "-c", "forced_login_method=chatgpt", *sb],
            ["codex", "exec", "--ignore-user-config", "-c=forced_login_method=chatgpt", *sb],
            ["codex", "exec", "--ignore-user-config", "-cforced_login_method=chatgpt", *sb],
            ["codex", "exec", "--ignore-user-config", "--config", "forced_login_method=chatgpt", *sb],
            ["codex", "exec", "--ignore-user-config", "--config=forced_login_method=chatgpt", *sb],
            ["codex", "exec", *sb[:-1], "--ignore-user-config", "-c"],  # -c with no value
        ]
        for argv in bad:
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                check_argv(argv)

    def test_windows_sandbox_is_mxc_exactly_once(self):
        # CODEXSANDBOX: no -c windows.sandbox = no Windows sandbox = every command blocked
        login, sb = ["--ignore-user-config", "-c", "forced_login_method=chatgpt"], ["--sandbox", "workspace-write", "-"]
        check_argv(["codex", "exec", "-c", "windows.sandbox=mxc", *login, *sb])  # order is free
        bad = [
            ["codex", "exec", *login, *sb],  # missing
            ["codex", "exec", *FLAGS, "-c", "windows.sandbox=mxc", *sb],  # twice
            ["codex", "exec", *login, "-c", "windows.sandbox=elevated", *sb],
            ["codex", "exec", *login, "-c", "windows.sandbox=unelevated", *sb],
            ["codex", "exec", *login, "-c", "windows.sandbox=MXC", *sb],
            ["codex", "exec", *login, "-c", 'windows.sandbox="mxc"', *sb],
            ["codex", "exec", *login, "-c", "windows.sandbox = mxc", *sb],
            ["codex", "exec", *login, "-c", "windows={sandbox='mxc'}", *sb],
            ["codex", "exec", *login, "-cwindows.sandbox=mxc", *sb],
            ["codex", "exec", *login, "--config", "windows.sandbox=mxc", *sb],
            ["codex", "exec", *login, "--config=windows.sandbox=mxc", *sb],
            ["codex", "exec", "--ignore-user-config", "-c", "windows.sandbox=mxc", "-c", "windows.sandbox=mxc", *sb],
            ["codex", "exec", *FLAGS, "-c", "sandbox_mode=workspace-write", *sb],
            ["codex", "exec", *FLAGS, "-c", "sandbox_workspace_write.writable_roots=['C:/']", *sb],
            ["codex", "exec", *FLAGS, "-c", "sandbox_workspace_write.network_access=true", *sb],
            ["codex", "exec", *FLAGS, "-c", "approval_policy=never", *sb],
            ["codex", "exec", "-c", "windows.sandbox=mxc", "-c", "forced_login_method=chatgpt", *sb],  # user config
            # --enable/--disable are -c features.<name>=bool: config outside the allowlist
            ["codex", "exec", *FLAGS, "--enable", "elevated_windows_sandbox", *sb],
            ["codex", "exec", *FLAGS, "--enable=experimental_windows_sandbox", *sb],
            ["codex", "exec", *FLAGS, "--disable", "windows_sandbox", *sb],
            ["codex", "exec", *FLAGS, "--cd", "C:/", *sb],  # the worker's root is only ever -C
        ]
        for argv in bad:
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                check_argv(argv)

    def test_weaker_or_widening_argv_refused(self):
        bad = [
            ["codex", "exec", "--sandbox", "danger-full-access", "-"],
            ["codex", "exec", "--sandbox=danger-full-access", "-"],
            ["codex", "exec", "-s", "DANGER-FULL-ACCESS", "-"],
            self.OK[:-1] + ["--yolo"],
            self.OK[:-1] + ["--dangerously-bypass-approvals-and-sandbox"],
            self.OK[:-1] + ["--full-auto"],
            self.OK[:-1] + ["-c", "sandbox_mode=danger-full-access"],
            self.OK[:-1] + ["--config=x=1"],
            self.OK[:-1] + ["--profile", "p"],
            self.OK[:-1] + ["--add-dir", "C:/"],
            self.OK[:-1] + ["--use-credits"],
            self.OK[:-1] + ["--allow-overage"],
            self.OK[:-1] + ["-csandbox_mode=danger-full-access"],  # attached short values
            self.OK[:-1] + ["-cx=y"],
            self.OK[:-1] + ["-pP"],
            ["codex", "exec", *FLAGS, "-C", "--yolo", "--sandbox", "workspace-write", "-"],
            ["codex", "exec", *FLAGS, "--json", "-"],  # no sandbox
            self.OK[:-1] + ["-s", "read-only"],  # two sandboxes
            ["codex", "exec", *FLAGS, "--sandbox"],  # no value
            ["codex", "exec", *FLAGS, "--sandbox", "workspace"],
            ["codex", "login"],
            ["codex"],
        ]
        for argv in bad:
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                check_argv(argv)

    def test_cd_must_be_exactly_the_lane_worktree(self):
        wt = "C:/repo/.claude/worktrees/CDXA"
        argv = lambda *cd: ["codex", "exec", *FLAGS, *cd, "--sandbox", "workspace-write", "-"]  # noqa: E731
        check_argv(argv("-C", wt), worktree=wt)
        check_argv(argv("-C", "c:\\repo\\.claude\\worktrees\\CDXA\\"), worktree=wt)  # normalised
        for cd in (("-C", "C:/repo"), ("-C", "C:/repo/.claude/worktrees/CDXB"), (),
                   ("-C", wt, "-C", wt), ("-C", wt + "/sub"), ("-C", wt, "-CC:/repo")):
            with self.subTest(cd=cd), self.assertRaises(ValueError):
                check_argv(argv(*cd), worktree=wt)


class HeadroomTest(unittest.TestCase):
    def test_tighter_window_wins_and_bad_values_are_unknown(self):
        self.assertEqual(workers.headroom({"primary": {"used_percent": 30}, "secondary": {"used_percent": 75.5}}), 24.5)
        self.assertEqual(workers.headroom({"primary": {"used_percent": 0}}), 100)
        for rl in ({}, {"primary": None}, {"primary": {"used_percent": 101}}, {"primary": {"used_percent": -1}},
                   {"primary": {"used_percent": "5"}}, {"primary": {"used_percent": True}},
                   {"primary": {"used_percent": 5}, "secondary": {}}, {"primary": 5}):
            with self.subTest(rl=rl):
                self.assertIsNone(workers.headroom(rl))


class CodexWorkerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.wt = self.root / ".claude" / "worktrees" / "CDXA"
        self.wt.mkdir(parents=True)
        self.t = T0
        self.procs = []
        self.state = self.fresh()
        self.environ = {"PATH": "C:/bin", "CODEX_HOME": "C:/codex-home", "OPENAI_API_KEY": "sk-x",
                        "openai_base_url": "http://proxy", "CODEX_API_KEY": "k", "OPENAI_ORG_ID": "o",
                        "AZURE_OPENAI_ENDPOINT": "e", "ANTHROPIC_BASE_URL": "u", "X_API_KEY": "k"}
        self.runs = []  # never the real taskkill: the fake pid could be a real process
        self.pstart = {4242: 111}  # pid -> creation time; never the real process table
        self.w = CodexWorker(self.root, exe="fake-codex.exe", popen=self.popen, now=lambda: self.t,
                             reader=lambda: self.state, environ=self.environ,
                             run=lambda argv, **kw: self.runs.append(argv),
                             proc_start=lambda pid: self.pstart.get(pid))

    def popen(self, argv, **kw):
        self.procs.append(FakeCodexProc(argv, **kw))
        return self.procs[-1]

    @staticmethod
    def fresh(used=30, **quota):
        q = {"checked_at": int(T0), "has_credits": False, "primary": {"used_percent": used}}
        q.update(quota)
        return {"status": "available", "quota": q}

    def test_admit_needs_a_fresh_subscription_reading_without_credits(self):
        self.assertEqual(self.w.admit(), (70, None))
        self.state = self.fresh(95)
        self.assertEqual(self.w.admit(), (5, "low"))

    def test_admit_refuses_unknown_stale_api_key_limit_and_credits(self):
        def boom():
            raise RuntimeError("app-server down")
        cases = [
            (None, "unknown"), ("not a dict", "unknown"), ({}, "unknown"),
            ({"status": "api_key_route", "quota": self.fresh()["quota"]}, "api_key_route"),
            ({"status": "not_logged_in"}, "not_logged_in"),
            ({"status": "available", "quota": None}, "unknown"),
            (self.fresh(checked_at=int(T0) - workers.CODEX_STATE_MAX_AGE_S - 1), "stale"),
            (self.fresh(checked_at=int(T0) + 5), "stale"),  # from the future
            (self.fresh(checked_at=T0), "stale"),  # float: not the app-server's int
            (self.fresh(checked_at=True), "stale"),
            (self.fresh(checked_at=None), "stale"),
            (self.fresh(limit_reached="primary"), "limit_reached"),
            (self.fresh(has_credits=True), "has_credits"),
            (self.fresh(has_credits=None), "unknown"),
            (self.fresh(has_credits="false"), "unknown"),
            (self.fresh(primary={"used_percent": "5"}), "unknown"),
        ]
        no_credits_key = self.fresh()
        del no_credits_key["quota"]["has_credits"]
        cases.append((no_credits_key, "unknown"))
        for state, why in cases:
            with self.subTest(state=state):
                self.state = state
                self.assertEqual(self.w.admit(), (None, why))
        self.w.reader = boom
        self.assertEqual(self.w.admit(), (None, "unknown"))
        self.assertEqual(self.procs, [])

    def test_start_status_stop_collect(self):
        sid = self.w.start("CDXA", self.wt, "@jarvis lane=CDXA\nDo it.\n")
        self.assertEqual(sid, "codex-4242")
        p = self.procs[0]
        self.assertEqual(p.argv, ["fake-codex.exe", "exec", "--json", *FLAGS, "-C", str(self.wt),
                                  "--sandbox", "workspace-write", "-"])
        # no key, base URL, org or provider override reaches the child; the login home stays
        # allowlist (jarvis/codex.py route): PATH keeps only real, trusted directories
        path = sanitized_path(self.environ)
        self.assertEqual(p.kw["env"], {"CODEX_HOME": "C:/codex-home", **({"PATH": path} if path else {})})
        self.assertEqual(p.kw["cwd"], str(self.wt))
        self.assertEqual(p.stdin_text, "@jarvis lane=CDXA\nDo it.\n")
        log = self.root / "build" / "conveyor" / "codex" / "CDXA.jsonl"
        self.assertEqual(Path(p.kw["stdout"].name), log)
        self.assertTrue(log.is_file())
        self.assertEqual(self.w.status(sid), "running")
        self.assertEqual(self.w.status("codex-1"), "unknown")
        self.assertTrue(self.w.stop(sid))
        self.assertTrue(p.terminated)
        if os.name == "nt":  # the whole tree, so no codex child outlives the stop
            self.assertEqual([a[0].lower().endswith("\\system32\\taskkill.exe") for a in self.runs], [True])
            self.assertEqual(self.runs[0][1:], ["/PID", "4242", "/T", "/F"])
        self.assertEqual(self.w.status(sid), "exited:1")
        self.assertFalse(self.w.stop(sid))
        self.assertFalse(self.w.stop("codex-1"))
        self.assertLessEqual(len(self.runs), 1)
        self.assertIsNone(self.w.collect("CDXA", self.wt))
        main_report = self.root / "build" / "reports" / "CDXA.md"
        main_report.parent.mkdir(parents=True)
        main_report.write_text("STATUS: PASS\n", encoding="utf-8")
        self.assertEqual(self.w.collect("CDXA", self.wt), main_report)
        wt_report = self.wt / "build" / "reports" / "CDXA.md"
        wt_report.parent.mkdir(parents=True)
        wt_report.write_text("STATUS: PASS\n", encoding="utf-8")
        self.assertEqual(self.w.collect("CDXA", self.wt), wt_report)

    def events(self, *objs, end="\n"):
        with open(self.root / workers.CODEX_LOG_DIR / "CDXA.jsonl", "a", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(o) for o in objs) + end)

    def test_mid_run_limit_or_credit_events_are_a_violation(self):
        log = self.root / workers.CODEX_LOG_DIR / "CDXA.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text('{"type": "error", "message": "usage limit reached"}\n', encoding="utf-8")
        self.w.start("CDXA", self.wt, "x")
        self.assertIsNone(self.w.violation("CDXA"))  # an earlier run's events are not this run's
        msg = {"type": "item.completed", "item": {"type": "agent_message",
                                                  "text": "I hit a rate limit; buy credits"}}
        self.events({"type": "thread.started"}, msg)
        self.assertIsNone(self.w.violation("CDXA"))  # the agent's text never counts
        cases = [
            ({"type": "token_count", "rate_limits": {"rate_limit_reached_type": "primary"}}, "limit_reached"),
            ({"type": "account", "credits": {"hasCredits": True}}, "credits"),
            ({"type": "token_count", "rate_limits": {"primary": {"usedPercent": 100}}}, "limit_reached"),
            ({"type": "error", "message": "You've hit your usage_limit"}, "limit_reached"),
            ({"type": "turn.failed", "error": {"message": "Purchase credits to continue"}}, "credits"),
            ({"type": "error", "message": "unexpected status 429 Too Many Requests"}, "limit_reached"),
            ({"type": "error", "message": "Quota exceeded. Check your plan and billing details."},
             "limit_reached"),
            ({"type": "turn.failed", "error": {"code": "rate_limit_exceeded"}}, "limit_reached"),
        ]
        for ev, why in cases:
            with self.subTest(ev=ev):
                self.events(ev)
                self.assertEqual(self.w.violation("CDXA"), why)
                self.assertIsNone(self.w.violation("CDXA"))  # read once
        self.events({"type": "error", "message": "rate limit"}, end="")  # half-written line
        self.assertIsNone(self.w.violation("CDXA"))
        self.events()  # the line is completed
        self.assertEqual(self.w.violation("CDXA"), "limit_reached")
        self.assertEqual(self.w.violation("NEVER"), "log_unreadable")
        log.write_text("", encoding="utf-8")  # shrunk below the read offset: blind = stop
        self.assertEqual(self.w.violation("CDXA"), "log_truncated")

    def test_limit_words_inside_tool_payloads_never_count(self):
        self.w.start("CDXA", self.wt, "x")
        nested = {"rate_limits": {"primary": {"used_percent": 100}}, "credits": {"has_credits": True}}
        for item in ({"type": "command_execution", "aggregated_output": "rate limit 429 credits quota"},
                     {"type": "mcp_tool_call", "result": nested},
                     {"type": "command_execution", "aggregated_output": json.dumps(nested), **nested}):
            with self.subTest(item=item):
                self.events({"type": "item.completed", "item": item})
                self.assertIsNone(self.w.violation("CDXA"))

    def test_start_records_pid_and_creation_time(self):
        sid = self.w.start("CDXA", self.wt, "x")
        info = self.w.info[sid]
        self.assertEqual((info["pid"], info["start"], info["offset"]), (4242, 111, 0))
        self.assertEqual(Path(info["log"]), self.root / workers.CODEX_LOG_DIR / "CDXA.jsonl")

    def test_attach_after_restart(self):
        info = {"pid": 4242, "start": 111, "log": "x", "offset": 0}
        self.assertEqual(self.w.attach("CDXA", "codex-4242", info), "attached")
        self.assertEqual(self.w.status("codex-4242"), "running")
        self.pstart[4242] = 222  # the pid now belongs to another process: ours finished
        self.assertEqual(self.w.status("codex-4242"), "exited:?")
        self.assertFalse(self.w.stop("codex-4242"))
        self.assertEqual(self.runs, [])
        self.assertEqual(self.w.attach("CDXA", "codex-4242", info), "gone")
        del self.pstart[4242]
        self.assertEqual(self.w.attach("CDXA", "codex-4242", info), "gone")
        for bad in ({**info, "pid": "x"}, {**info, "pid": 0}, None):
            with self.subTest(info=bad):
                self.assertEqual(self.w.attach("CDXA", "codex-4242", bad), "unverified")
        self.pstart[4242] = 111
        self.assertEqual(self.w.attach("CDXA", "codex-4242", {**info, "start": None}), "unverified")
        self.assertEqual(self.w.status("codex-4242"), "unknown")

    def test_unreadable_pid_is_unverified_and_stopped(self):
        def boom(pid):
            raise OSError("access denied")
        self.w.proc_start = boom
        info = {"pid": 4242, "start": 111, "log": "x", "offset": 0}
        self.assertEqual(self.w.attach("CDXA", "codex-4242", info), "unverified")
        self.assertEqual(self.w.status("codex-4242"), "unknown")
        self.assertTrue(self.w.stop("codex-4242"))
        if os.name == "nt":
            self.assertEqual(self.runs[0][1:], ["/PID", "4242", "/T", "/F"])
        self.assertFalse(self.w.stop("codex-4242"))

    def test_start_without_a_creation_time_is_refused_and_killed(self):
        def boom(pid):
            raise OSError("access denied")
        for proc_start in (lambda pid: None, boom):
            with self.subTest(proc_start=proc_start):
                self.runs.clear()
                self.w.proc_start = proc_start
                with self.assertRaises(RuntimeError):
                    self.w.start("CDXA", self.wt, "x")
                self.assertTrue(self.procs[-1].terminated)
                self.assertEqual((self.w.procs, self.w.info), ({}, {}))
                if os.name == "nt":
                    self.assertEqual(self.runs[0][1:], ["/PID", "4242", "/T", "/F"])

    def test_no_codex_on_path_never_starts(self):
        w = CodexWorker(self.root, popen=self.popen, now=lambda: self.t)
        with mock.patch.object(workers, "_codex_exe", lambda: None):
            with self.assertRaises(FileNotFoundError):
                w.start("CDXA", self.wt, "x")
        self.assertEqual(self.procs, [])


class WorkerFieldTest(unittest.TestCase):
    def test_worker_validation(self):
        s = {"id": "WA1", "model": "opus", "effort": "high", "tier": "T2", "prompt": "x.md"}
        self.assertIsNone(conveyor.validate_slice(s))
        self.assertIsNone(conveyor.validate_slice({**s, "worker": "claude"}))
        self.assertIsNone(conveyor.validate_slice({**s, "worker": "codex"}))
        for bad in ("gpt", "Codex", "", None, ["codex"]):
            with self.subTest(worker=bad):
                self.assertEqual(conveyor.validate_slice({**s, "worker": bad}), "bad worker")
        self.assertIsNone(conveyor.validate_slice({**s, "worker": "auto"}))  # ROUTE1
        self.assertIsNone(conveyor.validate_slice({**s, "worker": "jules", "linux_ok": True}))
        for flag in ({}, {"linux_ok": False}, {"linux_ok": "true"}, {"linux_ok": 1}):
            with self.subTest(flag=flag):  # no explicit flag = may need UI/Windows tests = refused
                self.assertEqual(conveyor.validate_slice({**s, "worker": "jules", **flag}),
                                 "jules needs linux_ok: true (no UI/Windows tests)")

    def test_class_validation(self):  # ROUTE1
        s = {"id": "WA1", "model": "opus", "effort": "high", "tier": "T1", "prompt": "x.md"}
        for ok in ("A", "B", "C"):
            self.assertIsNone(conveyor.validate_slice({**s, "class": ok}))
        for bad in ("D", "b", "", None, ["C"], 1):
            with self.subTest(cls=bad):
                self.assertEqual(conveyor.validate_slice({**s, "class": bad}), "bad class")


class FakeCodex:
    def __init__(self, headroom, why=None):
        self.headroom, self.why, self.started, self.state = headroom, why, [], "running"
        self.vio, self.stopped = None, []
        self.info = {"codex-7": {"pid": 4242, "start": 111, "log": "x", "offset": 0}}
        self.attach_result, self.attach_calls, self.after_stop = "attached", [], "gone"

    def attach(self, name, sid, info):
        self.attach_calls.append((name, sid, info))
        if self.attach_result == "attached":
            self.state = "running"
        return self.attach_result

    def admit(self):
        return self.headroom, self.why

    def violation(self, name):
        return self.vio

    def stop(self, sid):
        self.stopped.append(sid)
        self.attach_result = self.after_stop
        return True

    def start(self, name, wt, text):
        self.started.append((name, wt, text))
        return "codex-7"

    def status(self, sid):
        return self.state


class RouteTest(Base):
    """ROUTE1: worker "auto" is codex only when every rule holds; anything else is claude."""
    def setUp(self):
        super().setUp()
        self.c.usage, self.c.codex = 80, FakeCodex(50)  # claude 20% left < codex 50%
        self.s = {"id": "RT1", "worker": "auto", "tier": "T1", "class": "C", "files": ["docs/x.md"]}

    def test_all_rules_hold_routes_codex(self):
        self.assertEqual(self.c.route(self.s), ("codex", "claude headroom 20 < codex 50"))
        self.assertEqual(self.c.route({**self.s, "class": "B", "tier": "T0"})[0], "codex")

    def test_explicit_worker_is_kept(self):
        for w in ("claude", "codex"):
            self.assertEqual(self.c.route({**self.s, "worker": w}), (w, "explicit"))
        self.assertEqual(self.c.route({k: v for k, v in self.s.items() if k != "worker"}), ("claude", "explicit"))

    def test_tier_and_class_rules(self):
        self.assertEqual(self.c.route({**self.s, "tier": "T2"}), ("claude", "tier T2"))
        self.assertEqual(self.c.route({**self.s, "class": "A"}), ("claude", "class A"))
        self.assertEqual(self.c.route({k: v for k, v in self.s.items() if k != "class"}),
                         ("claude", "class unmarked"))

    def test_protected_file_veto(self):
        for f in ("scripts/x.py", "scripts", "Scripts/X.py", "scripts\\conveyor.py", "./scripts/a.py",
                  "docs/../scripts/a.py", ".claude/skills/a.md", ".github/workflows/ci.yml", ".github/CODEOWNERS",
                  "CLAUDE.md", "docs/AGENTS.md", "agents.md", "uv.lock", "data/x.json", "data\\jarvis.db",
                  ".env.local", "docs/key.pem", "dev/dashboard/a.js", "docs/*", "docs/**", "jarvis/?.py",
                  "../x.md", "/etc/x", "C:/x.md", "", " ", ".", "jarvis/ui/a.db",
                  "scripts./x.py", ".claude./settings.json", ".github. /ci.yml", "CLAUDE.md.", "docs/AGENTS.md.",
                  "uv.lock.", "data./x.json", "docs/key.pem.", "CLAUDE~1/settings.json", "GITHUB~1/workflows/ci.yml"):
            with self.subTest(f=f):
                self.assertTrue(conveyor.codex_veto(f))
                self.assertEqual(self.c.route({**self.s, "files": ["docs/ok.md", f]}),
                                 ("claude", f"protected file {f}"))
        for f in ("docs/x.md", "tests/test_x.py", "jarvis/ui/strings.py", "docs/scripts/x.md"):
            self.assertFalse(conveyor.codex_veto(f), f)
        for f in (None, 1, ["docs/x.md"]):
            self.assertTrue(conveyor.codex_veto(f), f)
        self.assertEqual(self.c.route({**self.s, "files": []}), ("claude", "no files"))
        self.assertEqual(self.c.route({k: v for k, v in self.s.items() if k != "files"}), ("claude", "no files"))

    def test_unknown_claude_usage_is_claude(self):
        for u in (None, "80", True):
            with self.subTest(usage=u):
                self.c.usage = u
                self.assertEqual(self.c.route(self.s), ("claude", "claude usage unknown"))

    def test_codex_admission_refused_is_claude(self):
        for h, why in ((None, "unknown"), (None, "stale"), (5, "low"), (None, "api_key_route")):
            with self.subTest(why=why):
                self.c.codex = FakeCodex(h, why)
                self.assertEqual(self.c.route(self.s), ("claude", f"codex {why}"))
        for h in (None, "50", True):
            with self.subTest(h=h):
                self.c.codex = FakeCodex(h)
                self.assertEqual(self.c.route(self.s), ("claude", "codex headroom unknown"))
        self.c.codex.admit = lambda: (_ for _ in ()).throw(OSError("boom"))
        self.assertEqual(self.c.route(self.s), ("claude", "codex admit OSError"))

    def test_claude_not_scarcer_is_claude(self):
        for u, h in ((50, 50), (20, 50)):
            with self.subTest(usage=u):
                self.c.usage, self.c.codex = u, FakeCodex(h)
                self.assertEqual(self.c.route(self.s), ("claude", f"claude headroom {100 - u} >= codex {h}"))

    def test_dispatch_emits_one_route_event_per_auto_slice(self):
        events = []
        self.c.event = lambda kind, **kw: events.append((kind, kw))
        self.git.fail[("git", "worktree", "add")] = (0, "")
        self.backlog({"id": "RTA", "worker": "auto", "class": "C", "files": ["docs/rta.md"]},
                     {"id": "RTB", "worker": "auto", "class": "A", "files": ["docs/rtb.md"]},
                     {"id": "RTC"})
        lanes = {}
        self.c.max_lanes = 5
        self.c.dispatch_backlog(lanes, set())
        routes = [kw for k, kw in events if k == "route"]
        self.assertEqual(sorted((r["lane"], r["worker"]) for r in routes), [("RTA", "codex"), ("RTB", "claude")])
        self.assertEqual([n for n, *_ in self.c.codex.started], ["RTA"])
        self.assertEqual([lanes[n].get("worker") for n in ("RTA", "RTB", "RTC")], ["codex", None, None])
        self.assertEqual(sorted(self.fake.bg_names()), ["RTB", "RTC"])


class ConveyorCodexTest(Base):
    def setUp(self):
        super().setUp()
        self.events = []
        self.c.event = lambda kind, **kw: self.events.append((kind, kw))
        self.git.fail[("git", "worktree", "add")] = (0, "")
        self.c.codex_commit = lambda name, ln, report: None  # CODEXCOMMIT: real git in CodexCommitTest

    def test_refused_admission_holds_without_start(self):
        for h, why in ((None, "unknown"), (None, "stale"), (None, "api_key_route"),
                       (None, "limit_reached"), (None, "has_credits"), (5, "low")):
            with self.subTest(why=why):
                self.c.codex = FakeCodex(h, why)
                self.assertIsNone(self.c.start_codex("CDXA", "1234567", HDR.format("CDXA")))
                self.assertEqual(self.c.codex.started, [])
                self.assertEqual(self.events[-1], ("codex_hold", {"lane": "CDXA", "headroom": h, "reason": why}))
        self.assertFalse(any(c[:3] == ["git", "worktree", "add"] for c in self.git.calls))
        self.assertEqual(self.fake.bg_calls(), [])

    def test_codex_start_makes_worktree_and_never_runs_claude(self):
        self.c.codex = FakeCodex(50)
        self.assertEqual(self.c.start_codex("CDXA", "1234567", "prompt"), "codex-7")
        wt = self.root / ".claude" / "worktrees" / "CDXA"
        self.assertIn(["git", "worktree", "add", "-b", "worktree-CDXA", str(wt), "1234567"], self.git.calls)
        self.assertEqual(self.c.codex.started, [("CDXA", wt, "prompt")])
        self.assertEqual(self.fake.calls, [])

    def test_failed_worktree_never_starts(self):
        self.c.codex = FakeCodex(50)
        self.git.fail[("git", "worktree", "add")] = (128, "fatal: exists")
        self.assertIsNone(self.c.start_codex("CDXA", "1234567", "prompt"))
        self.assertEqual(self.c.codex.started, [])

    def test_failed_start_drops_the_fresh_worktree(self):
        self.c.codex = FakeCodex(50)
        self.c.codex.start = lambda *a: (_ for _ in ()).throw(FileNotFoundError("codex not on PATH"))
        self.assertIsNone(self.c.start_codex("CDXA", "1234567", "prompt"))
        wt = self.root / ".claude" / "worktrees" / "CDXA"
        self.assertIn(["git", "worktree", "remove", str(wt)], self.git.calls)
        self.assertIn(["git", "branch", "-D", "worktree-CDXA"], self.git.calls)
        self.assertFalse(any("--force" in c for c in self.git.calls))

    def lane(self):
        prompt = self.base / "prompts" / "CDXA.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(HDR.format("CDXA"), encoding="utf-8")
        lanes = {}
        self.c.record(lanes, "CDXA", "codex-7", "opus", "high", "1234567", prompt, worker="codex")
        return lanes

    def test_codex_track_running_then_missing(self):
        self.c.codex = FakeCodex(50)
        lanes = self.lane()
        self.assertEqual(lanes["CDXA"]["worker"], "codex")
        self.c.codex_track(lanes)
        self.assertFalse(lanes["CDXA"].get("finished"))
        self.c.codex.state = "exited:0"
        self.c.codex_track(lanes)
        self.assertEqual((lanes["CDXA"]["finished"], lanes["CDXA"]["status"]), (True, "MISSING"))
        self.assertEqual(self.fake.calls, [])

    def test_mid_run_limit_event_stops_the_lane(self):
        self.c.codex = FakeCodex(50)
        lanes = self.lane()
        self.c.codex_track(lanes)
        self.assertEqual(self.c.codex.stopped, [])
        self.c.codex.vio = "limit_reached"
        self.c.codex_track(lanes)
        self.assertEqual(self.c.codex.stopped, ["codex-7"])
        self.assertEqual((lanes["CDXA"]["finished"], lanes["CDXA"]["status"]), (True, "MISSING"))
        self.assertEqual(self.events[-1], ("codex_stop", {"lane": "CDXA", "id": "codex-7",
                                                         "reason": "limit_reached"}))
        self.c.codex_track(lanes)  # a stopped lane is not stopped again
        self.assertEqual(self.c.codex.stopped, ["codex-7"])
        self.assertEqual(self.fake.calls, [])

    def test_codex_track_finishes_on_the_report_file(self):
        self.c.codex = FakeCodex(50)
        lanes = self.lane()
        report = Path(lanes["CDXA"]["worktree"]) / "build" / "reports" / "CDXA.md"
        report.parent.mkdir(parents=True)
        report.write_text("STATUS: PASS\nCOMMIT: " + "a" * 40 + "\n", encoding="utf-8")
        os.utime(report, (self.t + 1, self.t + 1))
        self.c.codex_track(lanes)
        self.assertTrue(lanes["CDXA"]["finished"])
        self.assertEqual(lanes["CDXA"]["status"], "PASS")
        self.assertEqual(self.c.codex.stopped, ["codex-7"])  # still running at PASS: stopped first

    def test_refused_conveyor_commit_blocks_with_attention(self):
        self.c.codex = FakeCodex(50)
        self.c.codex_commit = lambda name, ln, report: "ungranted files changed: x.py"
        lanes = self.lane()
        report = Path(lanes["CDXA"]["worktree"]) / "build" / "reports" / "CDXA.md"
        report.parent.mkdir(parents=True)
        report.write_text("STATUS: PASS\n", encoding="utf-8")
        os.utime(report, (self.t + 1, self.t + 1))
        self.c.codex_track(lanes)
        ln = lanes["CDXA"]
        self.assertEqual((ln["finished"], ln["status"]), (True, "BLOCKED"))
        self.assertEqual(ln["codex_commit"], "ungranted files changed: x.py")
        self.assertIn(("codex_commit", {"lane": "CDXA", "id": "codex-7",
                                        "reason": "ungranted files changed: x.py"}), self.events)
        self.assertTrue(any(i[0] == "CDXA" and "conveyor commit refused: ungranted files changed: x.py"
                            in str(i) for i in self.c.attention(lanes)))

    def test_interim_report_of_an_exited_session_is_missing(self):
        self.c.codex = FakeCodex(50)
        lanes = self.lane()
        report = Path(lanes["CDXA"]["worktree"]) / "build" / "reports" / "CDXA.md"
        report.parent.mkdir(parents=True)
        report.write_text("STATUS: PASS (interim)\n", encoding="utf-8")
        os.utime(report, (self.t + 1, self.t + 1))
        self.c.codex_track(lanes)
        self.assertFalse(lanes["CDXA"].get("finished"))
        self.c.codex.state = "exited:0"
        self.c.codex_track(lanes)
        self.assertEqual((lanes["CDXA"]["finished"], lanes["CDXA"]["status"]), (True, "MISSING"))

    def restarted(self, result, after_stop="gone", report=False):
        self.c.codex = FakeCodex(50)
        lanes = self.lane()
        lanes["CDXA"]["codex"] = dict(self.c.codex.info["codex-7"])
        self.c.codex.state, self.c.codex.attach_result = "unknown", result
        self.c.codex.after_stop = after_stop
        if report:
            f = Path(lanes["CDXA"]["worktree"]) / "build" / "reports" / "CDXA.md"
            f.parent.mkdir(parents=True)
            f.write_text("STATUS: PASS\n", encoding="utf-8")
            os.utime(f, (self.t + 1, self.t + 1))
        self.c.codex_track(lanes)
        self.assertEqual(self.c.codex.attach_calls[0], ("CDXA", "codex-7", lanes["CDXA"]["codex"]))
        return lanes

    def test_restart_reattaches_a_verified_session(self):
        lanes = self.restarted("attached")
        self.assertFalse(lanes["CDXA"].get("finished"))
        self.assertEqual(lanes["CDXA"]["state"], "working")
        self.assertEqual(self.c.codex.stopped, [])

    def test_restart_stops_an_unverified_session_with_one_attention(self):
        lanes = self.restarted("unverified")
        ln = lanes["CDXA"]
        self.assertEqual(self.c.codex.stopped, ["codex-7"])
        self.assertEqual((ln["finished"], ln["status"], ln["state"]), (True, "MISSING", "stopped"))
        self.assertIn("not verifiable", ln["codex_unverified"])
        self.assertEqual(self.events[-1][0], "codex_unverified")
        items = [i for i in self.c.attention(lanes) if i[0] == "CDXA"]
        self.assertEqual(len(items), 1)
        self.assertIn("not verifiable", items[0][1])
        self.assertEqual(len(self.c.codex.attach_calls), 2)  # stopped, then proven gone
        self.c.codex_track(lanes)  # a stopped lane is not re-attached or stopped again
        self.assertEqual((len(self.c.codex.attach_calls), self.c.codex.stopped), (2, ["codex-7"]))

    def test_restart_holds_a_session_not_provably_stopped(self):
        lanes = self.restarted("unverified", after_stop="unverified")
        ln = lanes["CDXA"]
        self.assertFalse(ln.get("finished"))
        self.assertEqual((ln["status"], ln["state"]), ("MISSING", "unverified"))
        self.assertIn("not provably stopped", ln["codex_unverified"])
        self.assertEqual(len([i for i in self.c.attention(lanes) if i[0] == "CDXA"]), 1)
        self.c.codex_track(lanes)  # retried every tick, one event
        self.assertEqual(self.c.codex.stopped, ["codex-7", "codex-7"])
        self.assertEqual([e[0] for e in self.events].count("codex_unverified"), 1)

    def test_restart_with_an_unverified_session_and_its_report_finishes_without_a_kill(self):
        lanes = self.restarted("unverified", report=True)
        self.assertEqual((lanes["CDXA"]["finished"], lanes["CDXA"]["status"]), (True, "PASS"))
        self.assertEqual(self.c.codex.stopped, [])

    def test_restart_with_a_gone_session_is_missing(self):
        lanes = self.restarted("gone")
        self.assertEqual((lanes["CDXA"]["finished"], lanes["CDXA"]["status"]), (True, "MISSING"))
        self.assertEqual(self.c.codex.stopped, [])

    def test_restart_of_a_lane_without_persisted_info_raises_attention(self):
        self.c.codex = FakeCodex(50)
        lanes = self.lane()
        self.c.codex.state = "unknown"
        self.c.codex_track(lanes)
        self.assertEqual((lanes["CDXA"]["finished"], lanes["CDXA"]["status"]), (True, "MISSING"))
        self.assertIn("no persisted pid", lanes["CDXA"]["codex_unverified"])
        self.assertEqual(self.c.codex.attach_calls, [])
        self.assertEqual(len([i for i in self.c.attention(lanes) if i[0] == "CDXA"]), 1)

    def finished_lane(self):
        self.c.codex = FakeCodex(50)
        lanes = self.lane()
        report = Path(lanes["CDXA"]["worktree"]) / "build" / "reports" / "CDXA.md"
        report.parent.mkdir(parents=True)
        report.write_text("STATUS: PASS\n", encoding="utf-8")
        lanes["CDXA"].update(finished=True, status="PASS", report=str(report),
                             finished_ts=self.c.now() - conveyor.CLEANUP_S - 1)
        self.c.codex.state = "exited:0"
        return lanes

    def test_codex_cleanup_preserves_unintegrated_work(self):
        lanes = self.finished_lane()
        with mock.patch.object(self.c, "unintegrated", return_value=("off_main", "worktree-CDXA", "abc")), \
                mock.patch.object(self.c, "worktree_step") as step:
            self.c.cleanup(lanes, [], set())
        self.assertIn("preserved", lanes["CDXA"])
        self.assertNotIn("cleanup", lanes["CDXA"])
        step.assert_not_called()

    def test_codex_cleanup_removes_an_integrated_worktree(self):
        lanes = self.finished_lane()
        with mock.patch.object(self.c, "unintegrated", return_value=None) as unint, \
                mock.patch.object(self.c, "worktree_step") as step:
            self.c.cleanup(lanes, [], set())
        self.assertEqual(unint.call_args[0][0], {"cwd": lanes["CDXA"]["worktree"]})
        self.assertEqual(lanes["CDXA"]["cleanup"]["result"], "removed")
        step.assert_called_once_with("CDXA", lanes["CDXA"])
        self.assertEqual(self.fake.calls, [])

    def test_codex_cleanup_skips_running_young_or_attention_lanes(self):
        for case in ("running", "young", "attention"):
            with self.subTest(case=case):
                lanes = self.finished_lane()
                if case == "running":
                    self.c.codex.state = "running"
                if case == "young":
                    lanes["CDXA"]["finished_ts"] = self.c.now()
                with mock.patch.object(self.c, "unintegrated") as unint, \
                        mock.patch.object(self.c, "worktree_step") as step:
                    self.c.cleanup(lanes, [], {"CDXA"} if case == "attention" else set())
                unint.assert_not_called()
                step.assert_not_called()
                self.assertNotIn("cleanup", lanes["CDXA"])
                shutil.rmtree(Path(lanes["CDXA"]["worktree"]) / "build")


class CodexCommitTest(Base):
    """CODEXCOMMIT: the conveyor (outside the sandbox) commits a PASS codex lane's granted
    files in a real temp git repo; anything else is refused with HEAD unchanged."""

    def setUp(self):
        super().setUp()
        self.events = []
        self.c.event = lambda kind, **kw: self.events.append((kind, kw))
        self.c.cmd = conveyor.real_cmd
        self.wt = self.root / "wt"
        self.wt.mkdir()
        for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"],
                     ["config", "commit.gpgsign", "false"]):
            self.assertEqual(self.git_(*args)[0], 0)
        (self.wt / "a.txt").write_text("a\n", encoding="utf-8")
        self.git_("add", "a.txt")
        self.assertEqual(self.git_("commit", "-q", "-m", "init")[0], 0)
        self.head = self.git_("rev-parse", "HEAD")[1].strip()
        self.report = self.root / "CDXA.md"  # outside the repo: never in its status
        self.ln = {"worktree": str(self.wt), "files": ["a.txt", "b.txt", ".env", "data/x.db"],
                   "id": "codex-7"}

    def git_(self, *args):
        return conveyor.real_cmd(["git", "-C", str(self.wt), *args])

    def run_(self, msg="COMMIT MESSAGE: fix(conveyor): CDXA granted files"):
        self.report.write_text(f"STATUS: PASS\nCOMMIT: {'f' * 40}\n{msg}\nFILES: a.txt\n",
                               encoding="utf-8")
        return self.c.codex_commit("CDXA", self.ln, self.report)

    def test_pass_commits_only_the_granted_files_and_writes_the_sha(self):
        (self.wt / "a.txt").write_text("a2\n", encoding="utf-8")
        (self.wt / "b.txt").write_text("b\n", encoding="utf-8")
        self.assertIsNone(self.run_())
        sha = self.git_("rev-parse", "HEAD")[1].strip()
        self.assertNotEqual(sha, self.head)
        out = self.git_("show", "--name-only", "--format=%B", "HEAD")[1]
        self.assertIn("fix(conveyor): CDXA granted files", out)
        self.assertIn("Worker: codex", out)
        self.assertEqual(sorted(x for x in out.splitlines()[-2:]), ["a.txt", "b.txt"])
        lines = self.report.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:2], ["STATUS: PASS", f"COMMIT: {sha}"])
        self.assertEqual(sum(x.startswith("COMMIT:") for x in lines), 1)  # the lane's own line is gone
        self.assertEqual(self.events, [("codex_commit", {"lane": "CDXA", "id": "codex-7", "sha": sha})])
        self.assertEqual(self.git_("status", "--porcelain")[1], "")
        self.report.write_text(f"# CDXA\n**STATUS: PASS**\n**COMMIT:** {'f' * 40}\n", encoding="utf-8")
        (self.wt / "a.txt").write_text("a3\n", encoding="utf-8")
        self.assertIsNone(self.c.codex_commit("CDXA", self.ln, self.report))
        sha = self.git_("rev-parse", "HEAD")[1].strip()
        self.assertEqual(self.report.read_text(encoding="utf-8").splitlines(),
                         ["# CDXA", "**STATUS: PASS**", f"COMMIT: {sha}"])  # prefixed fake gone, ours after STATUS

    def test_ungranted_file_is_refused_and_nothing_is_committed(self):
        (self.wt / "a.txt").write_text("a2\n", encoding="utf-8")
        (self.wt / "evil.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.run_(), "ungranted files changed: evil.py")
        self.assertEqual(self.git_("rev-parse", "HEAD")[1].strip(), self.head)
        self.assertEqual(self.events, [])

    def test_protected_paths_are_refused_even_when_granted(self):
        (self.wt / ".env").write_text("K=1\n", encoding="utf-8")
        (self.wt / "data").mkdir()
        (self.wt / "data" / "x.db").write_text("x", encoding="utf-8")
        self.assertEqual(self.run_(), "protected paths refused: .env, data/x.db")
        self.assertEqual(self.git_("rev-parse", "HEAD")[1].strip(), self.head)
        for p in ("uv.lock", "data/a.txt", "x/.env.local", "dev/dashboard/x.js", "k.PEM",
                  "C:\\Users\\u\\.jarvis\\c.json", "a.sqlite3"):
            self.assertTrue(conveyor.protected_path(p), p)
        self.assertFalse(conveyor.protected_path("scripts/conveyor.py"))

    def test_message_is_sanitised_and_falls_back(self):
        for msg, want in (("COMMIT MESSAGE: fix(x): CDXA a\x01b   c", "fix(x): CDXA a b c"),
                          ("COMMIT MESSAGE: fix(x): other lane", "chore: CDXA codex lane"),
                          ("COMMIT MESSAGE: rm -rf; CDXA", "chore: CDXA codex lane"),
                          ("", "chore: CDXA codex lane")):
            with self.subTest(msg=msg):
                (self.wt / "a.txt").write_text(msg + "\n", encoding="utf-8")
                self.assertIsNone(self.run_(msg))
                self.assertEqual(self.git_("log", "-1", "--format=%s")[1].strip(), want)

    def test_nothing_changed_is_refused(self):
        self.assertEqual(self.run_(), "no changes to commit")
        self.assertEqual(self.c.codex_commit("CDXA", {"id": "x"}, self.report), "no lane worktree")


JulesWorker, JULES_API = workers.JulesWorker, workers.JULES_API
BASE, HEAD = "b" * 40, "c" * 40
KEY = "AQ.secret-jules-key-123"
PR = "https://github.com/salih/JARVIS/pull/7"


class JulesWorkerTest(unittest.TestCase):
    """JULESADAPT: fake HTTP + fake git only; never the Jules API, never the real key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.keyf = self.root / "key" / "jules.key"
        self.keyf.parent.mkdir()
        self.keyf.write_text(KEY + "\n", encoding="utf-8")
        self.t = T0
        self.http_calls, self.replies = [], {}
        self.git_calls, self.g = [], {}
        self.revs = {"refs/remotes/origin/main": BASE, "refs/heads/jules/JX^{commit}": HEAD, "HEAD": HEAD}
        self.diff = "scripts/a.py\0"
        self.w = JulesWorker(self.root, http=self.http, now=lambda: self.t, key_path=self.keyf, git=self.fgit)
        self.wt = self.root / "wt"

    def http(self, method, url, headers, body=None):
        self.http_calls.append((method, url, headers, body))
        r = self.replies.get(method, (200, {}))
        if callable(r) and not isinstance(r, Exception):
            r = r(url)
        if isinstance(r, Exception):
            raise r
        return r

    def fgit(self, a):
        self.git_calls.append(list(a))
        sub = a[2]
        if sub == "remote":
            return self.g.get("remote", (0, "git@github.com:salih/JARVIS.git\n"))
        if sub == "rev-parse":
            v = self.revs.get(a[-1])
            return (0, v + "\n") if v else (1, "")
        if sub == "diff":
            return 0, self.diff
        return self.g.get(sub, (0, ""))

    def ledger(self, rows):
        p = self.root / workers.JULES_LEDGER
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rows if isinstance(rows, str) else json.dumps(rows), encoding="utf-8")

    def test_missing_or_bad_key_disables_without_http(self):
        for text in (None, "", "  \n", "two words"):
            with self.subTest(key=text):
                if text is None:
                    self.keyf.unlink(missing_ok=True)
                else:
                    self.keyf.write_text(text, encoding="utf-8")
                self.assertEqual(self.w.admit(BASE), (None, "disabled: no key file"))
                self.assertEqual(self.w.status("jules-abc"), "unknown")
                self.assertFalse(self.w.stop("jules-abc"))
                with self.assertRaises(RuntimeError):
                    self.w.start("JX", BASE, "task")
        self.assertEqual(self.http_calls, [])

    def test_admit_ok_makes_no_http_call(self):
        self.assertEqual(self.w.admit(BASE), (15, None))
        self.assertEqual(self.http_calls, [])

    def test_daily_limit_holds_until_rows_age_out(self):
        self.ledger([{"sid": f"jules-{i}", "ts": T0 - 100, "done": True} for i in range(100)])
        self.assertEqual(self.w.admit(BASE), (0, "daily limit 100/100 in 24 h"))
        self.t = T0 - 100 + workers.JULES_WINDOW_S + 1
        self.assertEqual(self.w.usage(), (0, 0))
        self.assertEqual(self.w.admit(BASE), (15, None))

    def test_concurrent_limit_holds_and_open_sessions_never_age_out(self):
        self.ledger([{"sid": f"jules-{i}", "ts": T0 - 3 * 86400, "done": False} for i in range(15)])
        self.assertEqual(self.w.admit(BASE), (0, "concurrent limit 15/15"))
        self.w._done("jules-0")
        self.assertEqual(self.w.usage(), (0, 14))
        self.assertEqual(self.w.admit(BASE), (1, None))

    def test_unreadable_ledger_is_no_headroom(self):
        for raw in ("{", "{}", '[{"ts": "x"}]', "[1]"):
            with self.subTest(raw=raw):
                self.ledger(raw)
                self.assertIsNone(self.w.usage())
                self.assertEqual(self.w.admit(BASE), (None, "usage ledger unreadable"))

    def test_base_and_origin_checks(self):
        self.assertEqual(self.w.admit("main"), (15, "base is not a commit sha"))
        self.assertEqual(self.w.admit("b" * 7), (15, "base is not a commit sha"))
        self.revs["refs/remotes/origin/main"] = "d" * 40
        self.assertEqual(self.w.admit(BASE), (15, "origin/main is not the slice base"))
        del self.revs["refs/remotes/origin/main"]
        self.assertEqual(self.w.admit(BASE), (15, "origin/main is not the slice base"))
        self.g["remote"] = (0, "https://gitlab.com/salih/JARVIS.git\n")
        self.assertEqual(self.w.admit(BASE), (15, "origin is not a GitHub repo"))
        self.assertEqual(self.http_calls, [])

    def test_start_posts_the_session_with_the_key_header_only(self):
        self.replies["POST"] = (200, {"name": "sessions/abc123", "id": "abc123"})
        self.assertEqual(self.w.start("JX", BASE, "do the slice", ["scripts/a.py"]), "jules-abc123")
        [(method, url, headers, body)] = self.http_calls
        self.assertEqual((method, url, headers), ("POST", f"{JULES_API}/sessions", {"X-Goog-Api-Key": KEY}))
        self.assertEqual(body["sourceContext"], {"source": "sources/github/salih/JARVIS",
                                                 "githubRepoContext": {"startingBranch": "main"}})
        self.assertEqual((body["automationMode"], body["requirePlanApproval"], body["title"]),
                         ("AUTO_CREATE_PR", False, "JX"))
        self.assertIn("jules/JX", body["prompt"])
        self.assertIn(BASE, body["prompt"])
        self.assertIn("scripts/a.py", body["prompt"])
        self.assertNotIn(KEY, json.dumps(body))
        self.assertEqual(self.w.usage(), (1, 1))
        self.assertEqual(self.w.info["jules-abc123"], {"session": "abc123", "base": BASE, "branch": "jules/JX"})
        ledger = (self.root / workers.JULES_LEDGER).read_text(encoding="utf-8")
        self.assertNotIn(KEY, ledger)

    def test_start_without_a_session_id_fails(self):
        for reply in ((200, {}), (200, {"id": "a/b"}), (200, ["x"])):
            with self.subTest(reply=reply):
                self.replies["POST"] = reply
                with self.assertRaises(RuntimeError):
                    self.w.start("JX", BASE, "t")
                self.t += 1
        self.assertEqual(self.w.usage(), (3, 0))  # a lost reply may have made a session: counted daily

    def test_unwritable_ledger_never_posts(self):
        self.ledger("{")
        with self.assertRaises(RuntimeError):
            self.w.start("JX", BASE, "t")
        self.assertEqual(self.http_calls, [])

    def test_key_never_in_errors(self):
        import traceback
        for reply in ((500, {}), (403, {"error": KEY}), RuntimeError(f"boom {KEY}"),
                      OSError(f"X-Goog-Api-Key: {KEY}"), (None, {})):
            with self.subTest(reply=type(reply).__name__):
                self.replies["POST"] = reply
                with self.assertRaises(RuntimeError) as cm:
                    self.w.start("JX", BASE, "t")
                e = cm.exception
                self.assertNotIn(KEY, "".join(traceback.format_exception(e)))
                self.assertIsNone(e.__context__)
                self.assertIsNone(e.__cause__)
        self.replies["POST"] = (500, {})
        with self.assertRaises(RuntimeError) as cm:
            self.w.start("JX", BASE, "t")
        self.assertEqual(str(cm.exception), "jules POST sessions: HTTP 500")

    def test_status_mapping(self):
        self.ledger([{"sid": "jules-abc", "ts": T0, "done": False}])
        for state, want in (("QUEUED", "running"), ("IN_PROGRESS", "running"),
                            ("AWAITING_PLAN_APPROVAL", "waiting"), ("PAUSED", "waiting"),
                            ("STRANGE", "unknown"), (None, "unknown")):
            with self.subTest(state=state):
                self.replies["GET"] = (200, {"state": state})
                self.assertEqual(self.w.status("jules-abc"), want)
        self.assertEqual(self.w.usage(), (1, 1))
        self.replies["GET"] = (500, {})
        self.assertEqual(self.w.status("jules-abc"), "unknown")
        self.replies["GET"] = (200, {"state": "FAILED"})
        self.assertEqual(self.w.status("jules-abc"), "failed")
        self.assertEqual(self.w.usage(), (1, 0))
        self.replies["GET"] = (200, {"state": "COMPLETED"})
        self.assertEqual(self.w.status("jules-abc"), "completed")
        self.assertEqual(self.http_calls[0][:2], ("GET", f"{JULES_API}/sessions/abc"))
        n = len(self.http_calls)
        self.assertEqual(self.w.status("jules-a/../b"), "unknown")  # never a crafted path
        self.assertEqual(len(self.http_calls), n)

    def test_stop_deletes_the_session(self):
        self.ledger([{"sid": "jules-abc", "ts": T0, "done": False}])
        self.assertTrue(self.w.stop("jules-abc"))
        self.assertEqual(self.http_calls[-1][:2], ("DELETE", f"{JULES_API}/sessions/abc"))
        self.assertEqual(self.w.usage(), (1, 0))
        self.replies["DELETE"] = (404, {})
        self.assertFalse(self.w.stop("jules-abc"))

    def test_waiting_states_and_raw_state_tracking(self):
        for st in workers.JULES_WAITING:
            with self.subTest(state=st):
                self.replies["GET"] = (200, {"state": st})
                self.assertEqual(self.w.status("jules-abc"), "waiting")
                self.assertEqual(self.w.raw["jules-abc"], st)
        self.replies["GET"] = (500, {})
        self.assertEqual(self.w.status("jules-abc"), "unknown")
        self.assertEqual(self.w.raw["jules-abc"], "PAUSED")  # a read blip keeps the last state
        for data, raw in (({}, "none"), ({"state": 7}, "none"), ({"state": "X" * 99}, "X" * 64)):
            self.replies["GET"] = (200, data)
            self.w.status("jules-abc")
            self.assertEqual(self.w.raw["jules-abc"], raw)

    def test_release_frees_the_cap_without_delete(self):
        self.ledger([{"sid": "jules-abc", "ts": T0, "done": False}])
        self.assertEqual(self.w.usage(), (1, 1))
        self.w.release("jules-abc")
        self.assertEqual(self.w.usage(), (1, 0))
        self.assertEqual(self.http_calls, [])

    def reply(self, acts, state="AWAITING_USER_FEEDBACK"):
        self.replies["GET"] = lambda url: (200, {"activities": acts} if "/activities" in url else {"state": state})

    def summary(self):
        f = self.root / workers.JULES_DIR / "JX.json"
        self.assertLessEqual(f.stat().st_size, workers.JULES_WHY_MAX)
        return f.read_text(encoding="utf-8")

    def test_why_saves_newest_activities_first(self):
        acts = [{"createTime": f"2026-10-24T21:{m:02d}:00Z", "originator": "agent",
                 "agentMessaged": {"agentMessage": f"msg {m}"}} for m in range(15)]
        acts += ["raw", None, {"createTime": "2026-10-24T20:00:00Z", "planGenerated": {"plan": {"steps": [
            {"title": "a"}, {"title": "b"}, "x"]}}}]
        self.reply(acts[::-1])
        line = self.w.why("jules-abc", "JX")
        self.assertEqual(line, "AWAITING_USER_FEEDBACK msg 14")
        data = json.loads(self.summary())
        self.assertEqual(data["state"], "AWAITING_USER_FEEDBACK")
        self.assertEqual([a["agent"] for a in data["activities"]], [f"msg {m}" for m in range(14, 4, -1)])
        self.assertEqual(data["errors"], [])
        self.assertEqual([c[:2] for c in self.http_calls], [("GET", f"{JULES_API}/sessions/abc"),
                                                            ("GET", f"{JULES_API}/sessions/abc/activities?pageSize=100")])
        self.reply([acts[-1], {"sessionFailed": {"reason": {"code": 3}}, "description": ""}], state="FAILED")
        self.assertEqual(self.w.why("jules-abc", "JX"), 'FAILED a; b')
        self.assertEqual(json.loads(self.summary())["activities"],[{"plan": "a; b", "time": "2026-10-24T20:00:00Z"}, {"error": '{"code": 3}'}])

    def test_why_truncates_fields_and_caps_the_file(self):
        big = "ğ" * 2000
        self.reply([{"createTime": f"t{i}", "agentMessaged": {"agentMessage": big}, "description": big}
                    for i in range(10)], state=big)
        line = self.w.why("jules-abc", "JX")
        self.assertLessEqual(len(line), 200)
        self.assertNotIn("\n", line)
        data = json.loads(self.summary())
        self.assertEqual(len(data["state"]), workers.JULES_TEXT_MAX)
        self.assertTrue(data["activities"])
        self.assertEqual(data["activities"][0]["time"], "t9")  # the oldest were dropped
        self.assertTrue(all(len(v) <= workers.JULES_TEXT_MAX for a in data["activities"] for v in a.values()))

    def test_why_records_read_errors_and_never_raises(self):
        self.replies["GET"] = RuntimeError(KEY)
        line = self.w.why("jules-abc", "JX")
        data = json.loads(self.summary())
        self.assertEqual(data["errors"], ["jules GET sessions/abc: RuntimeError",
                                          "jules GET sessions/abc/activities?pageSize=100: RuntimeError"])
        self.assertEqual(line, "jules GET sessions/abc: RuntimeError")
        self.replies["GET"] = lambda url: (500, {}) if "/activities" in url else (200, ["not", "a", "dict"])
        self.w.why("jules-abc", "JX")
        self.assertEqual(json.loads(self.summary())["errors"],
                         ["jules GET sessions/abc/activities?pageSize=100: HTTP 500"])
        n = len(self.http_calls)
        self.assertIn("bad jules session id", self.w.why("jules-a/../b", "JX"))
        self.assertEqual(len(self.http_calls), n)
        self.keyf.unlink()
        self.assertIn("no key file", self.w.why("jules-abc", "JX"))

    def test_why_never_echoes_the_key_or_headers(self):
        self.reply([{"agentMessaged": {"agentMessage": f"your key is {KEY}"}, "description": {"k": KEY}}],
                   state=KEY)
        line = self.w.why("jules-abc", "JX")
        text = self.summary()
        for s in (text, line):
            self.assertNotIn(KEY, s)
            self.assertNotIn("X-Goog-Api-Key", s)
        self.assertIn("your key is [key]", text)
        self.reply([{"agentMessaged": {"agentMessage": "x" * 490 + KEY}}], state="\x1b[2J" + "x" * 491 + KEY)
        line = self.w.why("jules-abc", "JX")  # a key straddling the 500-char clip leaves no prefix behind
        self.assertNotIn(KEY[:5], self.summary() + line)
        self.assertTrue(line.isprintable())
        self.keyf.write_text("e", encoding="utf-8")  # a tiny key rewrites values only, never the summary's own fields
        self.reply([{"agentMessaged": {"agentMessage": "hello"}}], state="e")
        self.assertEqual(self.w.why("jules-abc", "JX"), "[key] h[key]llo")
        self.assertEqual(sorted(json.loads(self.summary())), ["activities", "errors", "lane", "sid", "state"])

    def test_why_caps_the_bytes_on_disk_under_a_crlf_newline(self):
        real = Path.write_text

        def crlf(path, data, encoding=None, errors=None, newline=None):  # Windows text mode
            return real(path, data, encoding=encoding, errors=errors, newline="\r\n" if newline is None else newline)
        big = "ğ\x01" * 2000  # 2-byte and 6-byte (\u0001) characters
        self.reply([{"createTime": f"t{i}", "agentMessaged": {"agentMessage": "ğ" * 2000}}
                    for i in range(10)], state=big)  # ~2000 B state + ~1000 B each: one activity fits
        with mock.patch.object(Path, "write_text", crlf):
            self.w.why("jules-abc", "JX")
        raw = (self.root / workers.JULES_DIR / "JX.json").read_bytes()
        self.assertLessEqual(len(raw), workers.JULES_WHY_MAX)
        self.assertNotIn(b"\r\n", raw)
        data = json.loads(raw.decode("utf-8"))
        self.assertTrue(data["state"])
        self.assertTrue(data["activities"])

    def test_why_scrubs_the_key_as_a_dict_key(self):
        self.reply([{"sessionFailed": {"reason": {KEY: 1}}, "description": {KEY: {KEY: "x"}}},
                    {"description": {"x" * 488: {KEY: 1}}}], state={KEY: KEY})  # 2nd: a key straddling the clip
        line = self.w.why("jules-abc", "JX")
        text = self.summary()
        self.assertNotIn(KEY[:5], text + line)
        self.assertIn("[key]", text)

    def test_why_line_never_rejoins_a_key_split_by_a_control_char(self):
        self.reply([{"agentMessaged": {"agentMessage": KEY[:6] + "\x01" + KEY[6:]}}])
        self.assertNotIn(KEY, self.w.why("jules-abc", "JX"))

    def test_why_bad_lane_name_saves_nothing(self):
        self.reply([])
        for name in ("../x", "a b", "", "x" * 129):
            with self.subTest(name=name):
                self.assertIn("(summary not saved: OSError)", self.w.why("jules-abc", name))
        self.assertFalse((self.root / workers.JULES_DIR).exists())
        self.assertFalse((self.root / "build" / "conveyor" / "x.json").exists())

    def collect(self, protected=lambda p: False):
        return self.w.collect("jules-abc", "JX", self.wt, BASE, ["scripts/a.py"], protected)

    def test_collect_passes_a_verified_result(self):
        self.replies["GET"] = (200, {"state": "COMPLETED", "outputs": [{"pullRequest": {"url": PR}}]})
        report, status = self.collect()
        self.assertEqual(status, "PASS")
        self.assertEqual(report, self.wt / "build" / "reports" / "JX.md")
        lines = report.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:3], ["STATUS: PASS", f"COMMIT: {HEAD}", "BRANCH: jules/JX"])
        root = ["-C", str(self.root)]
        self.assertIn([*root, "fetch", "--no-tags", "origin", "+refs/pull/7/head:refs/heads/jules/JX"], self.git_calls)
        self.assertIn([*root, "merge-base", "--is-ancestor", BASE, HEAD], self.git_calls)
        self.assertIn(["-C", str(self.wt), "merge", "--ff-only", HEAD], self.git_calls)
        self.assertFalse(any("push" in c for c in self.git_calls))

    def test_collect_blocks_untrusted_results(self):
        good = {"state": "COMPLETED", "outputs": [{"pullRequest": {"url": PR}}]}
        cases = (
            ("no PR", {"outputs": []}, {}, "expected exactly one pull request"),
            ("two PRs", {"outputs": [{"pullRequest": {"url": PR}}] * 2}, {}, "expected exactly one pull request"),
            ("foreign PR", {"outputs": [{"pullRequest": {"url": "https://github.com/evil/JARVIS/pull/7"}}]}, {},
             "pull request is not on our origin"),
            ("fetch fails", good, {"fetch": (128, "")}, "fetch of the pull request head failed"),
            ("wrong base", good, {"merge-base": (1, "")}, "result is not based on the slice base"),
            ("ungranted", good, {"diff": "scripts/a.py\0uv.lock\0"}, "ungranted files: uv.lock"),
            ("protected", good, {"diff": "scripts/a.py\0data/jarvis.db\0"}, "protected files: data/jarvis.db"),
            ("empty diff", good, {"diff": ""}, "empty or unreadable diff"),
            ("no commits", good, {"head": BASE}, "no commits on the result branch"),
            ("ff fails", good, {"merge": (1, "")}, "fast-forward of the lane worktree failed"),
        )
        for base in (None, "-x", "main"):
            with self.subTest(base=base):
                self.git_calls.clear()
                report, status = self.w.collect("jules-abc", "JX", self.wt, base, ["scripts/a.py"], lambda p: False)
                self.assertEqual(status, "BLOCKED")
                self.assertIn("BLOCKED: slice base is not a commit sha", report.read_text(encoding="utf-8"))
                self.assertEqual(self.git_calls, [])
        for label, data, tweak, why in cases:
            with self.subTest(case=label):
                self.git_calls.clear()
                self.replies["GET"] = (200, data)
                self.g = {k: v for k, v in tweak.items() if k in ("fetch", "merge-base", "merge")}
                self.diff = tweak.get("diff", "scripts/a.py\0")
                self.revs["refs/heads/jules/JX^{commit}"] = tweak.get("head", HEAD)
                report, status = self.collect(protected=lambda p: p.startswith("data/"))
                self.assertEqual(status, "BLOCKED")
                text = report.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("STATUS: BLOCKED\n"))
                self.assertIn(f"BLOCKED: {why}", text)
                if label != "ff fails":
                    self.assertFalse(any("merge" in c and "--ff-only" in c for c in self.git_calls))


class FakeJules:
    def __init__(self, headroom=15, why=None):
        self.headroom, self.hold_why, self.state = headroom, why, "running"
        self.started, self.stopped, self.collected, self.info = [], [], [], {}
        self.raw, self.released, self.calls, self.text = {}, [], [], "asked a question"

    def admit(self, base):
        return self.headroom, self.hold_why

    def why(self, sid, name):
        self.calls.append(("why", sid, name))
        if isinstance(self.text, Exception):
            raise self.text
        return self.text

    def release(self, sid):
        self.released.append(sid)

    def start(self, name, base, text, files):
        self.started.append((name, base, text, files))
        self.info["jules-abc"] = {"session": "abc", "base": base, "branch": f"jules/{name}"}
        return "jules-abc"

    def status(self, sid):
        return self.state

    def stop(self, sid):
        self.stopped.append(sid)
        self.calls.append(("stop", sid))
        return True

    def collect(self, sid, name, worktree, base, files, protected):
        self.collected.append((sid, name, worktree, base, files, protected))
        return Path(worktree) / "build" / "reports" / f"{name}.md", "PASS"


class ConveyorJulesTest(Base):
    def setUp(self):
        super().setUp()
        self.events = []
        self.c.event = lambda kind, **kw: self.events.append((kind, kw))
        self.git.fail[("git", "worktree", "add")] = (0, "")
        self.c.jules = FakeJules()

    def test_refused_admission_holds_without_worktree(self):
        self.c.jules = FakeJules(None, "disabled: no key file")
        self.assertIsNone(self.c.start_jules("JX", BASE, "p", ["scripts/a.py"]))
        self.assertEqual(self.events[-1], ("jules_hold", {"lane": "JX", "headroom": None,
                                                          "reason": "disabled: no key file"}))
        self.assertEqual(self.c.jules.started, [])
        self.assertFalse(any(c[:3] == ["git", "worktree", "add"] for c in self.git.calls))
        self.assertEqual(self.fake.calls, [])

    def test_start_makes_the_worktree_at_the_base_and_never_runs_claude(self):
        self.assertEqual(self.c.start_jules("JX", BASE, "p", ["scripts/a.py"]), "jules-abc")
        wt = self.root / ".claude" / "worktrees" / "JX"
        self.assertIn(["git", "worktree", "add", "-b", "worktree-JX", str(wt), BASE], self.git.calls)
        self.assertEqual(self.c.jules.started, [("JX", BASE, "p", ["scripts/a.py"])])
        self.assertEqual(self.fake.calls, [])

    def test_failed_start_drops_the_fresh_worktree(self):
        self.c.jules.start = lambda *a: (_ for _ in ()).throw(RuntimeError("jules POST sessions: HTTP 500"))
        self.assertIsNone(self.c.start_jules("JX", BASE, "p", []))
        wt = self.root / ".claude" / "worktrees" / "JX"
        self.assertIn(["git", "worktree", "remove", str(wt)], self.git.calls)
        self.assertIn(["git", "branch", "-D", "worktree-JX"], self.git.calls)
        self.assertFalse(any("--force" in c for c in self.git.calls))

    def lane(self):
        prompt = self.base / "prompts" / "JX.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(HDR.format("JX"), encoding="utf-8")
        lanes = {}
        self.c.record(lanes, "JX", "jules-abc", "opus", "high", BASE, prompt, ["scripts/a.py"], worker="jules")
        lanes["JX"]["jules"] = {"session": "abc", "base": BASE, "branch": "jules/JX"}
        return lanes

    def test_track_running_then_completed_collects_and_finishes(self):
        lanes = self.lane()
        finished = []
        self.c.finish = lambda name, ln, report, status, **kw: finished.append((name, report, status))
        self.c.jules_track(lanes)
        self.assertEqual((lanes["JX"]["state"], lanes["JX"].get("finished")), ("working", None))
        self.c.jules.state = "completed"
        self.c.jules_track(lanes)
        wt = lanes["JX"]["worktree"]
        self.assertEqual(self.c.jules.collected,
                         [("jules-abc", "JX", wt, BASE, ["scripts/a.py"], conveyor.protected_path)])
        self.assertEqual(finished, [("JX", Path(wt) / "build" / "reports" / "JX.md", "PASS")])
        self.assertEqual(self.events[-1], ("jules_collect", {"lane": "JX", "id": "jules-abc", "status": "PASS"}))
        self.assertEqual(self.fake.calls, [])

    def test_track_collect_error_is_missing_not_a_crash(self):
        lanes = self.lane()
        self.c.jules.state = "completed"
        self.c.jules.collect = lambda *a: (_ for _ in ()).throw(OSError("disk"))
        self.c.jules_track(lanes)
        ln = lanes["JX"]
        self.assertEqual((ln["finished"], ln["status"], ln["jules_why"]),
                         (True, "MISSING", "jules collect error OSError"))
        self.assertEqual(self.c.jules.stopped, ["jules-abc"])
        self.assertEqual(self.c.jules.calls, [("stop", "jules-abc")])  # no summary read for a collect error

    def test_track_failed_or_late_saves_why_then_missing(self):
        for st, late, stopped in (("failed", False, []), ("running", True, ["jules-abc"]),
                                  ("unknown", True, ["jules-abc"])):
            with self.subTest(state=st, late=late):
                self.c.jules = FakeJules()
                self.c.jules.state = st
                lanes = self.lane()
                t0 = self.t
                if late:
                    self.t += workers.JULES_MAX_S + 1
                self.c.jules_track(lanes)
                self.t = t0
                ln = lanes["JX"]
                self.assertEqual((ln["finished"], ln["status"], ln["state"]), (True, "MISSING", st))
                self.assertEqual(ln["jules_why"],
                                 f"jules {st}" + (" past the time limit" if late else "") + ": asked a question")
                self.assertEqual(self.c.jules.stopped, stopped)
                self.assertEqual(self.c.jules.calls[0], ("why", "jules-abc", "JX"))  # why BEFORE stop
                self.assertEqual(self.c.jules.released, [])
                self.c.jules_track(lanes)  # a finished lane is never polled or stopped again
                self.assertEqual(self.c.jules.stopped, stopped)
                self.assertEqual(len(self.c.jules.calls), 1 + len(stopped))

    def test_track_waiting_leaves_the_session_open_and_blocks(self):
        for late in (False, True):  # DECISION: waiting past the time limit is not deleted either
            with self.subTest(late=late):
                self.c.jules = FakeJules()
                self.c.jules.state = "waiting"
                lanes = self.lane()
                finished = []

                def finish(name, ln, report, status, **kw):
                    finished.append((name, report, status))
                    ln.update(finished=True, status=status)
                self.c.finish = finish
                t0 = self.t
                if late:
                    self.t += workers.JULES_MAX_S + 1
                self.c.jules_track(lanes)
                self.t = t0
                ln = lanes["JX"]
                why = "jules waiting" + (" past the time limit" if late else "") + ": asked a question"
                self.assertEqual((ln["state"], ln["status"], ln["jules_why"]), ("waiting", "BLOCKED", why))
                self.assertEqual(self.c.jules.stopped, [])
                self.assertEqual(self.c.jules.released, ["jules-abc"])
                report = Path(ln["worktree"]) / "build" / "reports" / "JX.md"
                self.assertEqual(finished, [("JX", report, "BLOCKED")])
                text = report.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("STATUS: BLOCKED\n"))
                self.assertIn(f"BLOCKED: {why}\n", text)
                self.c.jules_track(lanes)
                self.assertEqual((self.c.jules.stopped, self.c.jules.released, len(finished)), ([], ["jules-abc"], 1))

    def test_track_summary_error_never_stalls_the_pass(self):
        lanes = self.lane()
        self.c.jules.state = "failed"
        self.c.jules.text = ValueError("boom")
        self.c.jules_track(lanes)
        self.assertEqual((lanes["JX"]["status"], lanes["JX"]["jules_why"]), ("MISSING", "jules failed: summary error ValueError"))
        self.c.jules = FakeJules()
        self.c.jules.state, self.c.jules.text = "failed", "x" * 500
        lanes = self.lane()
        self.c.jules_track(lanes)
        self.assertEqual(len(lanes["JX"]["jules_why"]), 200)

    def test_track_emits_jules_state_only_on_change(self):
        lanes = self.lane()
        for raw in (None, "QUEUED", "QUEUED", None, "IN_PROGRESS", "IN_PROGRESS"):
            if raw is not None:
                self.c.jules.raw["jules-abc"] = raw
            self.c.jules_track(lanes)
        states = [kw for kind, kw in self.events if kind == "jules_state"]
        self.assertEqual(states, [{"lane": "JX", "old": None, "new": "QUEUED"},
                                  {"lane": "JX", "old": "QUEUED", "new": "IN_PROGRESS"}])
        self.assertEqual(lanes["JX"]["jules_state"], "IN_PROGRESS")


if __name__ == "__main__":
    sys.exit(unittest.main())
