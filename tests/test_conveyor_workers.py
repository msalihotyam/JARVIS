"""WORKERADAPT: the codex worker adapter with a FAKE codex exe; no model, network or auth call."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


FLAGS = ["--ignore-user-config", "-c", "forced_login_method=chatgpt"]


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
        self.w = CodexWorker(self.root, exe="fake-codex.exe", popen=self.popen, now=lambda: self.t,
                             reader=lambda: self.state, environ=self.environ,
                             run=lambda argv, **kw: self.runs.append(argv))

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
        self.assertEqual(p.kw["env"], {"PATH": "C:/bin", "CODEX_HOME": "C:/codex-home"})
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


class FakeCodex:
    def __init__(self, headroom, why=None):
        self.headroom, self.why, self.started, self.state = headroom, why, [], "running"
        self.vio, self.stopped = None, []

    def admit(self):
        return self.headroom, self.why

    def violation(self, name):
        return self.vio

    def stop(self, sid):
        self.stopped.append(sid)
        return True

    def start(self, name, wt, text):
        self.started.append((name, wt, text))
        return "codex-7"

    def status(self, sid):
        return self.state


class ConveyorCodexTest(Base):
    def setUp(self):
        super().setUp()
        self.events = []
        self.c.event = lambda kind, **kw: self.events.append((kind, kw))
        self.git.fail[("git", "worktree", "add")] = (0, "")

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


if __name__ == "__main__":
    sys.exit(unittest.main())
