import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import codex as jarvis_codex
from jarvis.workers import codex as wc
from jarvis.workers.base import AgentProfile, Worker

_SPEC = importlib.util.spec_from_file_location(
    "conveyor_workers", Path(__file__).resolve().parents[1] / "scripts" / "conveyor_workers.py")
conveyor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(conveyor)


class FakeRunner:
    """Records spawns; like the real runner it calls verify_cwd() first and spawns nothing if it raises."""

    def __init__(self, before_spawn=None):
        self.starts, self.kills, self.codes, self.out = [], [], {}, {}
        self.before_spawn = before_spawn

    def start(self, argv, cwd, env, stdin, verify_cwd):
        if self.before_spawn:
            self.before_spawn()
        verify_cwd()
        handle = len(self.starts)
        self.starts.append((argv, cwd, env, stdin))
        self.codes[handle] = None
        return handle

    def poll(self, handle):
        return self.codes[handle]

    def kill(self, handle):
        self.kills.append(handle)
        self.codes[handle] = 1

    def output(self, handle):
        return self.out.get(handle, "")


def profile(network=False, budget=2):
    return AgentProfile("codex-writer", frozenset(), frozenset(), network, 10, budget)


class Base(unittest.TestCase):
    def setUp(self):
        self.stage = Path(tempfile.mkdtemp(prefix="pm21-stage-")).resolve()
        self.outside = Path(tempfile.mkdtemp(prefix="pm21-out-")).resolve()
        self.addCleanup(shutil.rmtree, self.stage, True)
        self.addCleanup(shutil.rmtree, self.outside, True)
        (self.stage / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.exe = str(self.outside / "codex.exe")
        self.environ = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"), "TEMP": str(self.outside),
                        "OPENAI_API_KEY": "sk-secret", "CODEX_HOME": str(self.outside / "codex-home")}
        self.runner = FakeRunner()

    def worker(self, **kw):
        kw.setdefault("runner", self.runner)
        kw.setdefault("exe", self.exe)
        kw.setdefault("environ", self.environ)
        kw.setdefault("home", self.outside)
        return wc.CodexWriterWorker(kw.pop("profile", profile()), kw.pop("stage", self.stage), **kw)

    def refused(self, code, fn, *args, **kw):
        with self.assertRaises(wc.WorkerRefused) as cm:
            fn(*args, **kw)
        self.assertEqual(cm.exception.code, code)

    def assert_nothing_started(self):
        self.assertEqual(self.runner.starts, [])
        self.assertFalse((self.stage / ".codex-tmp").exists())


class ParityTests(Base):
    def test_argv_matches_conveyor_and_passes_both_checks(self):
        ours = wc.build_argv(self.exe, self.stage)
        self.assertEqual(ours, conveyor.CodexWorker(self.outside, exe=self.exe).argv(self.stage))
        self.assertEqual(conveyor.check_argv(ours, worktree=self.stage), ours)
        self.assertEqual(wc.check_argv(ours, self.exe, self.stage), ours)
        self.assertEqual(wc.CONFIGS, conveyor.CONFIGS)
        self.assertEqual(wc.PROHIBITED_ENV, conveyor.PROHIBITED_ENV)
        self.assertEqual(wc.SANDBOX_DIRS, conveyor.SANDBOX_DIRS)

    def test_scrubbed_env_matches_conveyor(self):
        environ = dict(self.environ, PATH=os.environ.get("PATH", ""), openai_base_url="u", CODEX_API_KEY="k",
                       CODEX_FOO="f", AZURE_OPENAI_ENDPOINT="e", XDG_CONFIG_HOME="x", ANTHROPIC_API_KEY="a",
                       USERPROFILE=str(self.outside))
        self.assertEqual(wc.scrubbed_env(environ), conveyor.scrubbed_env(environ))
        for key in ("route_var",):
            for name in environ:
                self.assertEqual(getattr(wc, key)(name), getattr(conveyor, key)(name), name)

    def test_node_options_is_the_one_deviation(self):
        environ = dict(self.environ, NODE_OPTIONS="--require evil.js", node_options="--import x")
        self.assertIn("NODE_OPTIONS", conveyor.scrubbed_env(environ))
        self.assertFalse({k.upper() for k in wc.scrubbed_env(environ)} & {"NODE_OPTIONS"})

    def test_sandbox_env_matches_conveyor(self):
        env = {"Temp": "inherited", "PIP_CACHE_DIR": "p", "SYSTEMROOT": "r"}
        ours = wc.sandbox_env(env, self.stage)
        self.assertEqual(ours, conveyor.sandbox_env(env, self.stage))
        self.assertNotIn("Temp", ours)
        self.assertEqual(ours["TEMP"], str(self.stage / ".codex-tmp" / "tmp"))
        self.assertEqual((self.stage / ".codex-tmp" / ".gitignore").read_text(encoding="utf-8"), "*\n")


class ArgvTests(Base):
    def variants(self):
        good = wc.build_argv(self.exe, str(self.stage))
        insert = lambda *extra: good[:-1] + list(extra) + ["-"]  # noqa: E731
        return good, insert

    def test_each_banned_flag_refused_by_both(self):
        good, insert = self.variants()
        for flag in conveyor.BANNED_FLAGS:
            with self.subTest(flag=flag):
                argv = insert(flag)
                self.assertRaises(ValueError, conveyor.check_argv, argv, self.stage)
                self.assertRaises(wc.WorkerRefused, wc.check_argv, argv, self.exe, self.stage)
                self.assertRaises(wc.WorkerRefused, wc.check_argv, insert(flag, "value"), self.exe, self.stage)

    def test_unknown_attached_and_equals_forms_refused(self):
        good, insert = self.variants()
        for extra in (["--search"], ["-m", "o3"], ["--model=o3"], ["--sandbox=workspace-write"],
                      ["-cforced_login_method=chatgpt"], ["-C" + str(self.stage)], ["--json=1"],
                      ["--mcp-server", "x"], ["--skip-git-repo-check"], ["-s", "danger-full-access"],
                      ["-i", "img.png"], ["--output-last-message", "o.txt"], ["prompt text"], ["-"],
                      ["--ignore-user-config=false"], ["--dangerously-bypass-approvals-and-sandbox"]):
            with self.subTest(extra=extra):
                self.refused("flag_not_allowed", wc.check_argv, insert(*extra), self.exe, self.stage)

    def test_values_repeats_missing_and_shape(self):
        good, insert = self.variants()
        bad_value = [good[:9] + ["C:\\other"] + good[10:],  # -C elsewhere
                     good[:11] + ["danger-full-access"] + good[12:],
                     good[:11] + ["read-only"] + good[12:],
                     insert("-c", "model_provider=x"), insert("-c")]
        for argv in bad_value:
            with self.subTest(argv=argv):
                self.refused("flag_value_refused", wc.check_argv, argv, self.exe, self.stage)
        self.refused("flag_repeated", wc.check_argv, insert("--json"), self.exe, self.stage)
        self.refused("flag_repeated", wc.check_argv, insert("-c", wc.FORCED_LOGIN), self.exe, self.stage)
        self.refused("flag_missing", wc.check_argv, [a for a in good if a != "--ignore-user-config"],
                     self.exe, self.stage)
        self.refused("flag_missing", wc.check_argv, good[:4] + good[6:], self.exe, self.stage)
        self.refused("argv_command", wc.check_argv, ["other.exe"] + good[1:], self.exe, self.stage)
        self.refused("argv_command", wc.check_argv, [self.exe, "review"] + good[2:], self.exe, self.stage)
        self.refused("argv_command", wc.check_argv, good[:-1] + ["do it"], self.exe, self.stage)


class EnvTests(Base):
    def test_scrub_drops_route_key_provider_and_proxy(self):
        environ = dict(self.environ, openai_base_url="u", CODEX_API_KEY="k", CODEX_FOO="f",
                       AZURE_OPENAI_API_KEY="z", MY_API_KEY="m", ANTHROPIC_API_KEY="a", HTTPS_PROXY="p",
                       http_proxy="p", All_Proxy="p", NO_PROXY="*", npm_config_proxy="p", NODE_OPTIONS="n",
                       XDG_CONFIG_HOME="x", GITHUB_TOKEN="t")
        env = wc.scrubbed_env(environ)
        self.assertEqual({k for k in env if k != "PATH"}, {"SYSTEMROOT", "TEMP", "CODEX_HOME", "XDG_CONFIG_HOME"})

    def test_started_env_is_scrubbed_and_sandboxed(self):
        self.worker().start("do the slice")
        argv, cwd, env, stdin = self.runner.starts[0]
        self.assertNotIn("OPENAI_API_KEY", env)
        tmp = self.stage / ".codex-tmp"
        for key, sub in wc.SANDBOX_DIRS.items():
            self.assertEqual(env[key], str(tmp / sub))


class StageTests(Base):
    def assert_start_refused(self, code, worker=None):
        self.refused(code, (worker or self.worker()).start, "task")
        self.assertEqual(self.runner.starts, [])

    def test_bad_stage_paths_refused(self):
        cases = {"stage_not_absolute": "relative\\stage", "stage_missing": self.outside / "nope",
                 "stage_not_directory": self.stage / "app.py"}
        for code, stage in cases.items():
            with self.subTest(code=code):
                self.assert_start_refused(code, self.worker(stage=stage))
        self.assert_start_refused("stage_not_absolute", self.worker(stage=str(self.stage / "x" / "..")))
        self.assert_nothing_started()

    def test_codex_project_config_refused(self):
        for name in (".codex", ".CODEX", "sub/.Codex"):
            with self.subTest(name=name):
                (self.stage / name).mkdir(parents=True)
                self.assert_start_refused("stage_has_codex_config")
                shutil.rmtree(self.stage / name.split("/")[0])
        (self.stage / ".codex").write_text("[mcp_servers.x]\n", encoding="utf-8")
        self.assert_start_refused("stage_has_codex_config")
        self.assert_nothing_started()

    def test_hard_link_refused(self):
        os.link(self.stage / "app.py", self.outside / "alias.py")
        self.assert_start_refused("stage_has_link")
        self.assert_nothing_started()

    def test_symlink_refused(self):
        try:
            os.symlink(self.outside, self.stage / "out", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks need privilege here")
        self.assert_start_refused("stage_has_link")
        self.assert_nothing_started()

    @unittest.skipUnless(os.name == "nt", "junctions are Windows only")
    def test_junction_inside_and_at_root_refused(self):
        import _winapi
        sub = self.stage / "deep" / "er"
        sub.mkdir(parents=True)
        _winapi.CreateJunction(str(self.outside), str(sub / "j"))
        self.assert_start_refused("stage_has_link")
        os.rmdir(sub / "j")
        root = self.outside / "root-j"
        _winapi.CreateJunction(str(self.stage), str(root))
        self.addCleanup(os.rmdir, root)
        self.refused("stage_escape", self.worker(stage=root).start, "task")
        self.assert_nothing_started()

    def test_link_planted_before_spawn_refused_by_verify_cwd(self):
        self.runner.before_spawn = lambda: (self.stage / ".codex").mkdir()
        self.refused("stage_has_codex_config", self.worker().start, "task")
        self.assertEqual(self.runner.starts, [])

    def test_stage_changed_before_spawn(self):
        worker = self.worker()
        other = Path(tempfile.mkdtemp(prefix="pm21-other-")).resolve()
        self.addCleanup(shutil.rmtree, other, True)
        self.runner.before_spawn = lambda: setattr(worker, "stage", other)
        self.refused("stage_changed", worker.start, "task")
        self.assertEqual(self.runner.starts, [])
        self.assertEqual(worker._runs, {})

    def test_exe_home_and_codex_home_overlap_refused(self):
        self.assert_start_refused("exe_in_stage", self.worker(exe=str(self.stage / "codex.exe")))
        self.assert_start_refused("stage_contains_home", self.worker(home=self.stage / "me"))
        inside = dict(self.environ, CODEX_HOME=str(self.stage / "ch"))
        self.assert_start_refused("stage_codex_home_overlap", self.worker(environ=inside))
        around = dict(self.environ, CODEX_HOME=str(self.stage.parent))
        self.assert_start_refused("stage_codex_home_overlap", self.worker(environ=around))
        self.assert_nothing_started()

    def test_sandbox_env_failure_refused(self):
        (self.stage / ".codex-tmp" / ".gitignore").mkdir(parents=True)
        self.assert_start_refused("sandbox_env_failed")


class LifecycleTests(Base):
    def test_worker_contract_and_exe_checks(self):
        self.assertIsInstance(self.worker(), Worker)
        self.refused("command_not_absolute", self.worker, exe="codex.exe")
        with mock.patch.object(jarvis_codex, "codex_executable", return_value=None):
            self.refused("codex_missing", self.worker, exe=None)

    def test_profile_task_and_budget_refused_before_anything(self):
        self.refused("profile_network", self.worker(profile=profile(network=True)).start, "task")
        self.refused("budget_sessions_exhausted", self.worker(profile=profile(budget=0)).start, "task")
        for task in ("", "  \n", None):
            self.refused("task_empty", self.worker().start, task)
        self.assert_nothing_started()

    def test_start_status_collect_completed(self):
        worker = self.worker()
        run_id = worker.start("--yolo do the slice")
        self.assertEqual(run_id, "codex-writer-1")
        argv, cwd, env, stdin = self.runner.starts[0]
        self.assertEqual(argv, wc.build_argv(self.exe, self.stage))
        self.assertEqual((cwd, stdin), (str(self.stage), "--yolo do the slice"))
        self.assertNotIn("--yolo do the slice", argv)
        self.assertEqual(worker.status(run_id), "running")
        self.refused("run_still_running", worker.collect, run_id)
        self.runner.codes[0] = 0
        self.runner.out[0] = "\n".join([
            "not json", "[1]", '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"' + "y" * 3000 + '"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":7}}',
            '{"type":"item.completed","item":{"type":"command_execution","text":"rm -rf"}}'])
        self.assertEqual(worker.status(run_id), "exited")
        result = worker.collect(run_id)
        self.assertEqual((result.status, result.summary, result.changed_files),
                         ("completed", "y" * wc.SUMMARY_LIMIT, ()))
        self.assertEqual(result.evidence, ("exit_code=0", wc.UNVERIFIED))
        self.assertEqual(worker.start("next"), "codex-writer-2")  # .codex-tmp from run 1 is no link
        self.refused("budget_sessions_exhausted", worker.start, "third")

    def test_failed_exit_or_failure_event(self):
        for code, out in ((1, ""), (0, '{"type":"turn.failed","error":{}}'), (0, '{"type":"error"}')):
            with self.subTest(code=code, out=out):
                runner = FakeRunner()
                worker = self.worker(runner=runner)
                run_id = worker.start("task")
                runner.codes[0], runner.out[0] = code, out
                self.assertEqual(worker.collect(run_id).status, "failed")

    def test_stop_is_idempotent(self):
        worker = self.worker()
        run_id = worker.start("task")
        worker.stop(run_id)
        worker.stop(run_id)
        self.assertEqual(self.runner.kills, [0])
        self.assertEqual(worker.status(run_id), "stopped")
        result = worker.collect(run_id)
        self.assertEqual((result.status, result.summary), ("failed", "worker was stopped before it finished"))

    def test_collect_refused_while_killed_tree_still_running(self):
        worker = self.worker()
        run_id = worker.start("task")
        with mock.patch.object(self.runner, "kill"):  # kill issued, tree not gone yet
            worker.stop(run_id)
        self.refused("run_still_running", worker.collect, run_id)

    def test_unknown_run_refused(self):
        worker = self.worker()
        for call in (worker.status, worker.stop, worker.collect):
            self.refused("unknown_run", call, "codex-writer-9")

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-only")
    def test_exe_reached_through_junction_into_stage_refused(self):
        import _winapi
        link = self.outside / "via"
        _winapi.CreateJunction(str(self.stage), str(link))
        self.addCleanup(os.rmdir, link)
        self.refused("exe_in_stage", self.worker(exe=str(link / "codex.exe")).start, "task")
        self.assertEqual(self.runner.starts, [])

    def test_stop_after_exit_does_not_kill(self):
        worker = self.worker()
        run_id = worker.start("task")
        self.runner.codes[0] = 0
        worker.stop(run_id)
        self.assertEqual(self.runner.kills, [])
        self.assertEqual(worker.status(run_id), "stopped")


if __name__ == "__main__":
    unittest.main()
