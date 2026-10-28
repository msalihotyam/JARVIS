"""PM20 Claude writer worker: argv allowlist, stage cwd, env scrub, lifecycle.

No real process is ever started: every test injects FakeRunner.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from jarvis.workers.base import AgentProfile, Worker
from jarvis.workers.claude import (
    DENIED_TOOLS,
    EMPTY_MCP_CONFIG,
    ClaudeWriterWorker,
    WorkerRefused,
    check_argv,
    scrubbed_env,
)

COMMAND = str(Path(tempfile.gettempdir()).resolve() / "claude-cli-not-run.exe")


class FakeRunner:
    def __init__(self, stdout: str = "", exit_code: int | None = 0) -> None:
        self.started: list[tuple] = []
        self.kills = 0
        self.exit_code = exit_code
        self.stdout = stdout

    def start(self, argv, cwd, env, stdin):
        self.started.append((list(argv), cwd, dict(env), stdin))
        return len(self.started)

    def poll(self, handle):
        return self.exit_code

    def kill(self, handle):
        self.kills += 1
        self.exit_code = 1

    def output(self, handle):
        return self.stdout


def profile(**overrides) -> AgentProfile:
    values = dict(
        name="writer",
        tools=frozenset({"Read", "Edit", "Write"}),
        paths=frozenset({"jarvis/x.py"}),
        network=False,
        max_turns=12,
        budget_sessions=2,
    )
    values.update(overrides)
    return AgentProfile(**values)


def payload(**fields) -> str:
    base = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    base.update(fields)
    return "log line\n" + json.dumps(base)


class WorkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pm20-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.runner = FakeRunner(stdout=payload())

    def worker(self, stage=None, *, prof=None, environ=None, **kwargs) -> ClaudeWriterWorker:
        kwargs.setdefault("max_budget_usd", 0.5)
        return ClaudeWriterWorker(
            prof or profile(),
            self.stage if stage is None else stage,
            runner=self.runner,
            command=COMMAND,
            environ=environ if environ is not None else {"PATH": ""},
            **kwargs,
        )


class ArgvTests(WorkerTestCase):
    def test_golden_argv(self) -> None:
        self.assertEqual(
            self.worker().build_argv(),
            [
                COMMAND, "-p", "--output-format", "json",
                "--permission-mode", "acceptEdits", "--safe-mode",
                "--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG,
                "--tools", "Edit,Read,Write",
                "--disallowedTools", "Bash,NotebookEdit,Task,WebFetch,WebSearch",
                "--disable-slash-commands", "--no-chrome", "--no-session-persistence",
                "--max-turns", "12", "--max-budget-usd", "0.5000",
            ],
        )  # fmt: skip

        self.assertEqual(json.loads(EMPTY_MCP_CONFIG), {"mcpServers": {}})

    def test_golden_argv_with_model_and_effort(self) -> None:
        argv = self.worker(model="claude-opus-5-5", effort="high").build_argv()
        self.assertEqual(argv[-4:], ["--model", "claude-opus-5-5", "--effort", "high"])
        check_argv(argv, COMMAND)

    def test_turns_and_budget_come_from_profile_and_settings(self) -> None:
        argv = self.worker(prof=profile(max_turns=7), max_budget_usd=2).build_argv()
        self.assertEqual(argv[argv.index("--max-turns") + 1], "7")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.0000")
        for denied in ("Bash", "WebFetch", "WebSearch"):
            self.assertNotIn(denied, argv[argv.index("--tools") + 1])
            self.assertIn(denied, DENIED_TOOLS)

    def test_banned_and_malformed_flags_are_refused(self) -> None:
        good = self.worker().build_argv()
        servers = json.dumps({"mcpServers": {"x": {"command": "evil"}}})
        cases = {
            "skip_permissions": good + ["--dangerously-skip-permissions"],
            "add_dir": good + ["--add-dir", "C:\\"],
            "settings": good + ["--settings", "{}"],
            "allowed_tools": good + ["--allowedTools", "Bash"],
            "system_prompt": good + ["--append-system-prompt", "x"],
            "resume": good + ["--resume", "abc"],
            "attached_form": good + ["--add-dir=C:\\"],
            "repeated": good + ["--no-chrome"],
            "positional": good + ["do things"],
            "mcp_servers": _replace(good, "--mcp-config", servers),
            "bypass_mode": _replace(good, "--permission-mode", "bypassPermissions"),
            "default_mode": _replace(good, "--permission-mode", "dontAsk"),
            "bash_tool": _replace(good, "--tools", "Read,Bash"),
            "web_tool": _replace(good, "--tools", "Read,WebFetch"),
            "dup_tool": _replace(good, "--tools", "Read,Read"),
            "empty_tools": _replace(good, "--tools", ""),
            "weak_deny": _replace(good, "--disallowedTools", "Bash"),
            "zero_turns": _replace(good, "--max-turns", "0"),
            "zero_budget": _replace(good, "--max-budget-usd", "0.0000"),
            "flag_model": good + ["--model", "--add-dir"],
            "value_missing": good + ["--model"],
            "missing_strict": [a for a in good if a != "--strict-mcp-config"],
            "missing_safe_mode": [a for a in good if a != "--safe-mode"],
            "missing_turns": good[: good.index("--max-turns")] + good[good.index("--max-turns") + 2 :],
            "wrong_command": ["claude", *good[1:]],
        }
        for name, argv in cases.items():
            with self.subTest(name), self.assertRaises(WorkerRefused):
                check_argv(argv, COMMAND)

    def test_banned_flag_from_build_argv_never_reaches_runner(self) -> None:
        class Sneaky(ClaudeWriterWorker):
            def build_argv(self):
                return super().build_argv() + ["--dangerously-skip-permissions"]

        sneaky = Sneaky(profile(), self.stage, runner=self.runner, command=COMMAND, max_budget_usd=0.5)
        with self.assertRaises(WorkerRefused) as caught:
            sneaky.start("task")
        self.assertEqual(caught.exception.code, "flag_not_allowed")
        self.assertEqual(self.runner.started, [])

    def test_constructor_refusals(self) -> None:
        cases = {
            "relative_command": dict(command="claude"),
            "nan_budget": dict(max_budget_usd=float("nan")),
            "bool_budget": dict(max_budget_usd=True),
            "zero_budget": dict(max_budget_usd=0),
            "bad_model": dict(model="-x"),
            "bad_effort": dict(effort="extreme"),
        }
        for name, overrides in cases.items():
            args = dict(runner=self.runner, command=COMMAND, max_budget_usd=0.5)
            args.update(overrides)
            with self.subTest(name), self.assertRaises(WorkerRefused):
                ClaudeWriterWorker(profile(), self.stage, **args)


def _replace(argv: list[str], flag: str, value: str) -> list[str]:
    out = list(argv)
    out[out.index(flag) + 1] = value
    return out


class RefusalTests(WorkerTestCase):
    def assert_refused(self, worker: ClaudeWriterWorker, code: str, task: str = "task") -> None:
        with self.assertRaises(WorkerRefused) as caught:
            worker.start(task)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.runner.started, [])

    def test_profile_refusals(self) -> None:
        cases = {
            "profile_tools": profile(tools=frozenset({"Read", "Bash"})),
            "profile_network": profile(network=True),
            "budget_sessions_exhausted": profile(budget_sessions=0),
        }
        for code, prof in cases.items():
            with self.subTest(code):
                self.assert_refused(self.worker(prof=prof), code)
        self.assert_refused(self.worker(prof=profile(tools=frozenset())), "profile_tools")

    def test_empty_task_refused(self) -> None:
        self.assert_refused(self.worker(), "task_empty", task="  ")

    def test_session_budget_is_consumed_per_start(self) -> None:
        worker = self.worker(prof=profile(budget_sessions=1))
        worker.start("task")
        with self.assertRaises(WorkerRefused) as caught:
            worker.start("again")
        self.assertEqual(caught.exception.code, "budget_sessions_exhausted")
        self.assertEqual(len(self.runner.started), 1)

    def test_stage_refusals(self) -> None:
        cases = {
            "stage_not_absolute": "stage",
            "stage_missing": self.root / "absent",
        }
        for code, stage in cases.items():
            with self.subTest(code):
                self.assert_refused(self.worker(stage), code)
        dotted = str(self.stage) + os.sep + ".." + os.sep + "stage"
        self.assert_refused(self.worker(dotted), "stage_not_absolute")
        afile = self.root / "file.txt"
        afile.write_text("x", encoding="utf-8")
        self.assert_refused(self.worker(afile), "stage_not_directory")

    def test_link_escape_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            try:
                import _winapi

                _winapi.CreateJunction(str(outside), str(link))
            except (ImportError, OSError):
                self.skipTest("no symlink or junction support")
        self.assert_refused(self.worker(link), "stage_escape")

    def test_cwd_mismatch_refused(self) -> None:
        other = self.root / "other"
        other.mkdir()

        class Drifting(ClaudeWriterWorker):
            def launch_cwd(self):
                return other

        drifting = Drifting(profile(), self.stage, runner=self.runner, command=COMMAND, max_budget_usd=0.5)
        self.assert_refused(drifting, "cwd_mismatch")


class EnvTests(unittest.TestCase):
    def test_scrub(self) -> None:
        env = scrubbed_env(
            {
                "PATH": "",
                "SystemRoot": "C:\\Windows",
                "CLAUDE_CODE_OAUTH_TOKEN": "keep",
                "CLAUDE_CONFIG_DIR": "C:\\cfg",
                "ANTHROPIC_API_KEY": "sk-x",
                "ANTHROPIC_BASE_URL": "http://evil",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "CLAUDE_EXTRA_API_KEY": "x",
                "OPENAI_API_KEY": "x",
                "GITHUB_TOKEN": "x",
                "https_proxy": "http://user:pw@proxy:8080",
                "HTTP_PROXY": "http://user:pw@proxy:8080",
                "NO_PROXY": "x",
                "claude_code_disable_auto_memory": "0",
                "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "9",
                "claude_code_use_foundry": "1",
                "CLAUDE_ENV_FILE": "C:\\evil.sh",
                "CLAUDE_CODE_SHELL": "C:\\evil.exe",
                "NODE_OPTIONS": "--require C:\\evil.js",
            }
        )
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "keep")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "C:\\cfg")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"], "1")
        upper = {key.upper() for key in env}
        for gone in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_EXTRA_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_ENV_FILE", "CLAUDE_CODE_SHELL", "NODE_OPTIONS",
        ):  # fmt: skip
            self.assertNotIn(gone, upper)
        self.assertNotIn("claude_code_disable_auto_memory", env)
        self.assertNotIn("http://user:pw@proxy:8080", env.values())


class LifecycleTests(WorkerTestCase):
    def test_start_status_collect(self) -> None:
        self.runner.exit_code = None
        worker = self.worker(environ={"PATH": "", "ANTHROPIC_API_KEY": "sk-x"})
        self.assertIsInstance(worker, Worker)
        run_id = worker.start("do the slice")
        self.assertEqual(run_id, "claude-writer-1")
        argv, cwd, env, stdin = self.runner.started[0]
        self.assertEqual(cwd, str(self.stage))
        self.assertEqual(stdin, "do the slice")
        self.assertNotIn("do the slice", argv)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(worker.status(run_id), "running")
        with self.assertRaises(WorkerRefused):
            worker.collect(run_id)
        self.runner.exit_code = 0
        self.assertEqual(worker.status(run_id), "exited")
        result = worker.collect(run_id)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.summary, "done")
        self.assertEqual(result.changed_files, ())
        self.assertIn("exit_code=0", result.evidence)
        self.assertTrue(any("unverified" in item for item in result.evidence))

    def test_stop_is_idempotent(self) -> None:
        self.runner.exit_code = None
        worker = self.worker()
        run_id = worker.start("task")
        worker.stop(run_id)
        worker.stop(run_id)
        self.assertEqual(self.runner.kills, 1)
        self.assertEqual(worker.status(run_id), "stopped")
        self.assertEqual(worker.collect(run_id).status, "failed")

    def test_stop_after_exit_does_not_kill(self) -> None:
        worker = self.worker()
        run_id = worker.start("task")
        worker.stop(run_id)
        self.assertEqual(self.runner.kills, 0)

    def test_unknown_run_id(self) -> None:
        worker = self.worker()
        for call in (worker.status, worker.stop, worker.collect):
            with self.subTest(call.__name__), self.assertRaises(KeyError):
                call("claude-writer-9")

    def test_collect_status_mapping_keeps_claims_as_data(self) -> None:
        cases = [
            (payload(), 0, "completed"),
            (payload(subtype="error_max_turns", is_error=True), 1, "partial"),
            (payload(subtype="error_max_budget_usd", is_error=True), 1, "blocked_by_gate"),
            (payload(is_error=True), 0, "failed"),
            (payload(), 3, "failed"),
            (payload(subtype="SUCCESS; rm -rf"), 0, "failed"),
            ("not json at all", 0, "failed"),
            ("", 0, "failed"),
        ]
        for stdout, code, expected in cases:
            with self.subTest(expected=expected, stdout=stdout[-40:]):
                self.runner = FakeRunner(stdout=stdout, exit_code=code)
                worker = self.worker()
                result = worker.collect(worker.start("task"))
                self.assertEqual(result.status, expected)
                self.assertEqual(result.changed_files, ())
        self.runner = FakeRunner(stdout=payload(result="x" * 5000, files=["../../etc"]))
        worker = self.worker()
        result = worker.collect(worker.start("task"))
        self.assertEqual(len(result.summary), 2000)
        self.assertEqual(result.changed_files, ())
        self.runner = FakeRunner(stdout=payload(subtype="weird!"))
        worker = self.worker()
        self.assertIn("subtype=unknown", worker.collect(worker.start("task")).evidence)


if __name__ == "__main__":
    unittest.main()
