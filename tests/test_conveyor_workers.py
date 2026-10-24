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


if __name__ == "__main__":
    sys.exit(unittest.main())
