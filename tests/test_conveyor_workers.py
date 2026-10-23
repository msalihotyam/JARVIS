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


class CheckArgvTest(unittest.TestCase):
    OK = ["codex", "exec", "--json", "-C", "wt", "--sandbox", "workspace-write", "-"]

    def test_default_argv_passes(self):
        self.assertEqual(check_argv(list(self.OK)), self.OK)
        check_argv(["codex", "exec", "-s", "read-only", "-"])
        check_argv(["codex", "exec", "--sandbox=workspace-write", "-"])
        # a lane id / worktree path with a banned word is not a flag
        check_argv(["codex", "exec", "-C", "C:/wt/BYPASSFIX-credit", "--sandbox", "workspace-write", "-"])

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
            ["codex", "exec", "-C", "--yolo", "--sandbox", "workspace-write", "-"],
            ["codex", "exec", "--json", "-"],  # no sandbox
            self.OK[:-1] + ["-s", "read-only"],  # two sandboxes
            ["codex", "exec", "--sandbox"],  # no value
            ["codex", "exec", "--sandbox", "workspace"],
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
        self.sessions = Path(tmp.name) / "codex-home" / "sessions"
        self.wt = self.root / ".claude" / "worktrees" / "CDXA"
        self.wt.mkdir(parents=True)
        self.t = T0
        self.procs = []
        self.w = CodexWorker(self.root, exe="fake-codex.exe", popen=self.popen,
                             sessions_dir=self.sessions, now=lambda: self.t)

    def popen(self, argv, **kw):
        self.procs.append(FakeCodexProc(argv, **kw))
        return self.procs[-1]

    def rollout(self, lines, age=0.0, name="rollout-1.jsonl"):
        p = self.sessions / "2026" / "10" / "24" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.utime(p, (T0 - age, T0 - age))
        return p

    @staticmethod
    def event(primary, secondary=None):
        rl = {"primary": {"used_percent": primary, "window_minutes": 300}}
        if secondary is not None:
            rl["secondary"] = {"used_percent": secondary, "window_minutes": 10080}
        return json.dumps({"timestamp": "2026-10-24T10:00:00Z", "type": "event_msg",
                           "payload": {"type": "token_count", "info": None, "rate_limits": rl}})

    def test_usage_unknown_is_none(self):
        self.assertIsNone(self.w.usage())  # no sessions dir at all
        self.rollout(["not json", '{"type": "event_msg"}', ""])
        self.assertIsNone(self.w.usage())  # no rate_limits anywhere
        self.rollout([self.event(150)], name="rollout-2.jsonl")
        self.assertIsNone(self.w.usage())  # out of range
        self.rollout([self.event(20)], age=workers.CODEX_USAGE_MAX_AGE_S + 1, name="rollout-3.jsonl")
        for p in self.sessions.rglob("*.jsonl"):
            if p.name != "rollout-3.jsonl":
                p.unlink()
        self.assertIsNone(self.w.usage())  # stale

    def test_usage_is_live_newest_snapshot_never_summed(self):
        self.rollout([self.event(90)], age=600, name="old.jsonl")
        self.rollout([self.event(10, 40), self.event(30, 60), "garbage"], age=5)
        self.assertEqual(self.w.usage(), 40)  # the newest file's last snapshot, tighter window

    def test_start_status_stop_collect(self):
        sid = self.w.start("CDXA", self.wt, "@jarvis lane=CDXA\nDo it.\n")
        self.assertEqual(sid, "codex-4242")
        p = self.procs[0]
        self.assertEqual(p.argv, ["fake-codex.exe", "exec", "--json", "-C", str(self.wt),
                                  "--sandbox", "workspace-write", "-"])
        self.assertEqual(p.kw["cwd"], str(self.wt))
        self.assertEqual(p.stdin_text, "@jarvis lane=CDXA\nDo it.\n")
        log = self.root / "build" / "conveyor" / "codex" / "CDXA.jsonl"
        self.assertEqual(Path(p.kw["stdout"].name), log)
        self.assertTrue(log.is_file())
        self.assertEqual(self.w.status(sid), "running")
        self.assertEqual(self.w.status("codex-1"), "unknown")
        self.assertTrue(self.w.stop(sid))
        self.assertTrue(p.terminated)
        self.assertEqual(self.w.status(sid), "exited:1")
        self.assertFalse(self.w.stop(sid))
        self.assertFalse(self.w.stop("codex-1"))
        self.assertIsNone(self.w.collect("CDXA", self.wt))
        main_report = self.root / "build" / "reports" / "CDXA.md"
        main_report.parent.mkdir(parents=True)
        main_report.write_text("STATUS: PASS\n", encoding="utf-8")
        self.assertEqual(self.w.collect("CDXA", self.wt), main_report)
        wt_report = self.wt / "build" / "reports" / "CDXA.md"
        wt_report.parent.mkdir(parents=True)
        wt_report.write_text("STATUS: PASS\n", encoding="utf-8")
        self.assertEqual(self.w.collect("CDXA", self.wt), wt_report)

    def test_no_codex_on_path_never_starts(self):
        w = CodexWorker(self.root, popen=self.popen, sessions_dir=self.sessions, now=lambda: self.t)
        with mock.patch.object(workers.shutil, "which", lambda name: None):
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
    def __init__(self, headroom):
        self.headroom, self.started, self.state = headroom, [], "running"

    def usage(self):
        return self.headroom

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

    def test_unknown_or_low_headroom_holds_without_start(self):
        for h in (None, workers.CODEX_MIN_HEADROOM - 0.5, 0):
            with self.subTest(headroom=h):
                self.c.codex = FakeCodex(h)
                self.assertIsNone(self.c.start_codex("CDXA", "1234567", HDR.format("CDXA")))
                self.assertEqual(self.c.codex.started, [])
                self.assertEqual(self.events[-1], ("codex_hold", {"lane": "CDXA", "headroom": h}))
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
