"""E1-WARMTASK: execute steps of one run reuse one warm Claude process (fakes only)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.connectors import claude_code
from jarvis.connectors.claude_code import PROHIBITED_BILLING_ENV_VARS, ClaudeCodeConnector
from jarvis.contracts import ActionScope, DataClassification, ErrorCode, OutcomeStatus, SessionState, Task
from tests.helpers import safe_cleanup
from tests.test_claude_connector import OAUTH_MAX_AUTH_STATUS, patch_oauth_environment


class Fake:
    """A scripted stream-json CLI: each user line gets init + a cumulative-cost result."""

    def __init__(self, test: "WarmTaskTests", args: list[str]) -> None:
        self.test, self.args, self.returncode, self.out, self.total = test, args, None, [], 0.0
        self.stdin = self.stdout = self
        self.users: list[dict] = []

    def write(self, text: str) -> None:
        for line in text.splitlines():
            self.users.append(json.loads(line))
            self.test.respond(self)

    def readline(self) -> str:
        return self.out.pop(0) if self.returncode is None and self.out else ""

    def emit(self, event: dict) -> None:
        self.out.append(json.dumps(event) + "\n")

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        if self.returncode is None:
            self.returncode = -9

    terminate = kill

    def wait(self, timeout=None):
        return self.returncode

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class WarmTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.root = Path(temp.name).resolve()
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root, max_budget_usd=1.0)
        self.now = 1000.0
        self.connector._clock = lambda: self.now
        self.connector.warm_run = "run-1"
        for patcher in (
            patch_oauth_environment(),
            patch.object(ClaudeCodeConnector, "_argv0", return_value="C:/fake/bin/claude.exe"),
            patch.object(ClaudeCodeConnector, "_auth_config_fingerprint", return_value=((1, 100),)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        auth = patch.object(ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS)
        self.auth = auth.start()
        self.addCleanup(auth.stop)
        self.processes: list[Fake] = []
        popen = patch("jarvis.connectors.claude_code.subprocess.Popen", side_effect=self.spawn)
        self.popen = popen.start()
        self.addCleanup(popen.stop)
        self.addCleanup(claude_code.close_warm_task_processes)
        self.cost, self.is_error, self.key_source, self.on_turn = 0.01, False, "none", None

    def spawn(self, args, **kwargs) -> Fake:
        process = Fake(self, list(args))
        self.processes.append(process)
        return process

    def respond(self, process: Fake) -> None:
        if self.on_turn is not None:
            self.on_turn(process)
        process.emit({"type": "system", "subtype": "init", "apiKeySource": self.key_source,
                      "tools": [], "mcp_servers": [], "permissionMode": "plan", "model": "claude-test"})
        process.total += self.cost
        process.emit({"type": "result", "subtype": "error" if self.is_error else "success",
                      "is_error": self.is_error, "result": "Plan ready", "total_cost_usd": process.total,
                      "usage": {"input_tokens": 10, "output_tokens": 4}, "modelUsage": {"claude-test": {}}})

    def task(self) -> Task:
        return Task(
            objective="Analyze without changing files",
            capability="code_planning",
            scope=ActionScope(action="analyze", target=str(self.root), destination="anthropic/claude-code",
                              data_classification=DataClassification.SENSITIVE),
            context={},
        )

    def step(self):
        return self.connector.execute(self.task(), SessionState())

    def test_second_step_of_a_run_reuses_one_live_process(self) -> None:
        outputs, real = [], self.connector._run_warm
        with patch.object(self.connector, "_run_warm", side_effect=lambda *a: outputs.append(real(*a)) or outputs[-1]):
            first, second = self.step(), self.step()
        self.assertEqual([OutcomeStatus.SUCCESS] * 2, [first.outcome, second.outcome])
        self.popen.assert_called_once()
        process = self.processes[0]
        self.assertEqual(2, len(process.users))
        self.assertIsNone(process.poll())
        self.assertEqual("stream-json", process.args[process.args.index("--input-format") + 1])
        self.assertNotIn(process.users[0]["message"]["content"][0]["text"], process.args)
        # The CLI's running total is billed per step.
        costs = [json.loads(out[1].splitlines()[-1])["total_cost_usd"] for out in outputs]
        self.assertAlmostEqual(0.01, costs[0])
        self.assertAlmostEqual(0.01, costs[1])

    def test_billing_route_runs_before_every_step(self) -> None:
        with patch.object(self.connector, "billing_route", wraps=self.connector.billing_route) as route:
            self.step()
            self.step()
        self.assertEqual(2, route.call_count)
        self.popen.assert_called_once()

    def test_a_prohibited_env_on_a_later_step_refuses_and_kills(self) -> None:
        self.step()
        with patch.dict(os.environ, {PROHIBITED_BILLING_ENV_VARS[0]: "x"}):
            result = self.step()
        self.assertEqual(ErrorCode.BILLING_ROUTE_UNKNOWN, result.error.code)
        self.assertIsNotNone(self.processes[0].poll())
        self.popen.assert_called_once()
        self.assertEqual({}, self.connector._warm_tasks)

    def test_a_step_after_an_auth_error_spawns_fresh_and_reprobes(self) -> None:
        self.step()
        self.is_error = True
        self.assertNotEqual(OutcomeStatus.SUCCESS, self.step().outcome)
        self.assertIsNotNone(self.processes[0].poll())
        probes, self.is_error = self.auth.call_count, False
        self.assertEqual(OutcomeStatus.SUCCESS, self.step().outcome)
        self.assertEqual(2, len(self.processes))
        self.assertEqual(probes + 1, self.auth.call_count)

    def test_a_different_tool_set_does_not_reuse(self) -> None:
        self.step()
        real = self.connector._command

        def with_read(prompt: str) -> list[str]:
            argv = real(prompt)
            argv[argv.index("--tools") + 1] = "Read"
            return argv

        with patch.object(self.connector, "_command", side_effect=with_read):
            self.step()
        self.assertEqual(2, len(self.processes))
        self.assertIsNotNone(self.processes[0].poll())
        self.assertIn("Read", self.processes[1].args)

    def test_cancel_kills_the_warm_process_and_the_next_step_respawns(self) -> None:
        self.step()
        self.on_turn = lambda process: self.connector.cancel(next(iter(self.connector._processes)))
        self.assertNotEqual(OutcomeStatus.SUCCESS, self.step().outcome)
        self.assertIsNotNone(self.processes[0].poll())
        self.on_turn = None
        self.assertEqual(OutcomeStatus.SUCCESS, self.step().outcome)
        self.assertEqual(2, len(self.processes))

    def test_no_reuse_across_two_runs_and_run_end_reaps(self) -> None:
        self.step()
        self.connector.warm_run = "run-2"
        self.step()
        self.assertEqual(2, len(self.processes))
        self.assertIsNone(self.processes[0].poll())
        self.connector.end_warm_run("run-1")
        self.assertIsNotNone(self.processes[0].poll())
        self.assertIsNone(self.processes[1].poll())

    def test_run_end_during_a_step_does_not_put_the_process_back(self) -> None:
        self.on_turn = lambda process: self.connector.end_warm_run("run-1")
        self.assertEqual(OutcomeStatus.SUCCESS, self.step().outcome)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertEqual({}, self.connector._warm_tasks)

    def test_auth_config_change_respawns(self) -> None:
        self.step()
        with patch.object(ClaudeCodeConnector, "_auth_config_fingerprint", return_value=((2, 200),)):
            self.step()
        self.assertEqual(2, len(self.processes))
        self.assertIsNotNone(self.processes[0].poll())

    def test_dead_on_reuse_respawns(self) -> None:
        self.step()
        self.processes[0].returncode = 1
        self.assertEqual(OutcomeStatus.SUCCESS, self.step().outcome)
        self.assertEqual(2, len(self.processes))

    def test_idle_process_is_reaped_and_respawned(self) -> None:
        self.step()
        self.now += 601
        self.step()
        self.assertEqual(2, len(self.processes))
        self.assertIsNotNone(self.processes[0].poll())

    def test_app_exit_reaps_every_warm_process(self) -> None:
        self.step()
        claude_code.close_warm_task_processes()
        self.assertIsNotNone(self.processes[0].poll())
        self.assertEqual(set(), claude_code._WARM_TASK_PROCESSES)

    def test_init_drift_refuses_kills_and_reprobes(self) -> None:
        self.step()
        self.key_source = "ANTHROPIC_API_KEY"
        self.assertEqual(ErrorCode.EGRESS_REFUSED, self.step().error.code)
        self.assertIsNotNone(self.processes[0].poll())
        probes, self.key_source = self.auth.call_count, "none"
        self.assertEqual(OutcomeStatus.SUCCESS, self.step().outcome)
        self.assertEqual(probes + 1, self.auth.call_count)
