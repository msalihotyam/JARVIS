"""Warm Claude Code session for the intent adapter (opt-in, fake process only)."""

from __future__ import annotations

import gc
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jarvis.intent as intent
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.contracts import ActionScope, SessionState, Task
from tests.helpers import safe_cleanup
from tests.test_claude_connector import OAUTH_MAX_AUTH_STATUS, patch_oauth_environment

MARKER = "EARLIER-TURN-MARKER"
MISSING = object()          # a result without total_cost_usd


class WarmFake:
    """A scripted stream-json Claude Code process: replies are queued as stdin lines arrive."""

    def __init__(self, script: "WarmTest", args: list[str], kwargs: dict) -> None:
        self.script, self.args, self.kwargs = script, args, kwargs
        self.returncode = None
        self.written: list[str] = []
        self.out: list[str] = []
        self.stdin = self.stdout = self
        self.closed = 0
        self.waited = False

    def write(self, text: str) -> None:
        self.written.append(text)
        for line in text.splitlines():
            self.script.respond(self, json.loads(line))

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed += 1

    def readline(self) -> str:
        if self.returncode is not None or not self.out:
            return ""
        return self.out.pop(0)

    def emit(self, event: dict) -> None:
        self.out.append(json.dumps(event) + "\n")

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        if self.returncode is None:
            self.returncode = -9

    terminate = kill

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode

    def user_lines(self) -> list[str]:
        return [line for line in self.written if not json.loads(line).get("type", "").startswith("control_")]


class WarmTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        root = Path(temp.name)
        env = patch_oauth_environment()
        env.start()
        self.addCleanup(env.stop)
        auth = patch.object(ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS)
        auth.start()
        self.addCleanup(auth.stop)
        self.processes: list[WarmFake] = []
        popen = patch("jarvis.connectors.claude_code.subprocess.Popen", side_effect=self.spawn)
        self.popen = popen.start()
        self.addCleanup(popen.stop)
        self.now = 0.0
        self.adapter = intent.ClaudeIntentAdapter(allowed_roots=(root,), default_working_directory=root)
        self.adapter._clock = lambda: self.now
        self.adapter.max_budget_usd = 1.0
        self.adapter.warm_claude = True
        self.adapter.warm_lane = "c1"
        self.init_ok = True
        self.init: dict = {}
        self.callbacks: list[dict] = []
        self.costs = [0.1, 0.2, 0.3, 0.4]
        self.is_error = False
        self.crash = False
        self.send_init = True
        self.proof = True
        self.init_reply: dict | None = None     # replaces the initialize control_response
        self.proof_input: dict | None = None    # replaces the proof's {"prompt": <this turn's text>}
        self.after: list[dict] = []             # left in the pipe after the result
        self.addCleanup(intent._WARM_PROCESSES.clear)

    def spawn(self, args, **kwargs) -> WarmFake:
        process = WarmFake(self, list(args), kwargs)
        self.processes.append(process)
        return process

    def respond(self, process: WarmFake, event: dict) -> None:
        kind = event.get("type")
        if kind == "control_request" and event["request"]["subtype"] == "initialize":
            if self.init_reply is not None:
                process.emit(self.init_reply)
            elif self.init_ok:
                process.emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": event["request_id"], "response": {}}})
            return
        if kind in ("control_request", "control_response"):
            return
        model = process.args[process.args.index("--model") + 1] if "--model" in process.args else "claude"
        if self.send_init:
            process.emit({"type": "system", "subtype": "init", "apiKeySource": "none", "tools": [],
                          "mcp_servers": [], "permissionMode": "plan", "model": model, **self.init})
        for index, request in enumerate(self.callbacks):
            process.emit({"type": "control_request", "request_id": f"cb{index}", "request": request})
        if self.proof:
            proof = self.proof_input
            if proof is None:
                proof = {"hook_event_name": "UserPromptSubmit", "prompt": event["message"]["content"][-1]["text"]}
            process.emit({"type": "control_request", "request_id": "proof",
                          "request": {"subtype": "hook_callback", "callback_id": "jarvis-proof", "input": proof}})
        if self.crash:
            return
        result = {"type": "result", "subtype": "success", "is_error": self.is_error, "result": "ok"}
        cost = self.costs.pop(0)
        if cost is not MISSING:
            result["total_cost_usd"] = cost
        process.emit(result)
        for extra in self.after:
            process.emit(extra)

    def task(self, objective: str = "what time is it") -> Task:
        return Task(
            objective=objective,
            capability=intent.INTENT_CAPABILITY,
            scope=ActionScope(action="understand", target=""),
            context={"conversation": [{"role": "user", "text": MARKER}]},
        )

    def turn(self, objective: str = "what time is it"):
        return self.adapter.execute(self.task(objective), SessionState())

    def controls(self, process: WarmFake) -> list[str]:
        return [json.loads(line)["request"]["subtype"] for line in process.written
                if json.loads(line)["type"] == "control_request"]

    def replies(self, process: WarmFake) -> dict:
        return {
            reply["response"]["request_id"]: reply["response"]
            for reply in map(json.loads, process.written) if reply["type"] == "control_response"
        }

    def code(self, result):
        return result.error.code if result.error is not None else None


class FlagOffTest(WarmTest):
    def test_flag_off_is_todays_per_turn_path(self) -> None:
        self.adapter.warm_claude = False
        with patch.object(ClaudeCodeConnector, "_run_process", return_value=(1, "", "")) as base:
            self.turn()
            self.turn()
        self.assertEqual(base.call_count, 2)
        self.popen.assert_not_called()
        self.assertIn(MARKER, base.call_args.args[3])      # the window is sent every turn
        self.assertEqual(self.adapter._warm, {})

    def test_no_lane_is_todays_per_turn_path(self) -> None:
        self.adapter.warm_lane = None
        with patch.object(ClaudeCodeConnector, "_run_process", return_value=(1, "", "")) as base:
            self.turn()
        base.assert_called_once()
        self.popen.assert_not_called()


class WarmSessionTest(WarmTest):
    def test_argv_stays_hardened_without_the_prompt(self) -> None:
        self.turn("SECRET-REQUEST")
        args, kwargs = self.processes[0].args, self.processes[0].kwargs
        for flag in ("--safe-mode", "--strict-mcp-config"):
            self.assertIn(flag, args)
        self.assertNotIn("--bare", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")
        self.assertEqual(args[args.index("--input-format") + 1], "stream-json")
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertFalse(any("SECRET-REQUEST" in arg for arg in args))
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)
        self.assertFalse(kwargs["shell"])

    def test_initialize_registers_deny_and_proof_hooks_first(self) -> None:
        self.turn()
        first = json.loads(self.processes[0].written[0])
        self.assertEqual(first["request"]["subtype"], "initialize")
        self.assertEqual(first["request"]["hooks"], intent.WARM_HOOKS)

    def test_refused_initialize_sends_nothing(self) -> None:
        self.init_ok = False
        result = self.turn()
        self.assertEqual(self.code(result), intent.ErrorCode.EGRESS_REFUSED)
        self.assertEqual(self.processes[0].user_lines(), [])
        self.assertEqual(self.processes[0].returncode, -9)

    def test_second_turn_reuses_the_process_without_the_window(self) -> None:
        self.turn()
        self.turn()
        self.assertEqual(self.popen.call_count, 1)
        first, second = self.processes[0].user_lines()
        self.assertIn(MARKER, first)
        self.assertNotIn(MARKER, second)
        self.assertEqual(self.adapter._warm["c1"].cost, 0.2)

    def test_stdin_digest_is_the_exact_line(self) -> None:
        self.turn()
        self.turn("second")
        digests = [hashlib.sha256(line.encode("utf-8")).hexdigest() for line in self.processes[0].user_lines()]
        self.assertEqual(self.adapter._warm["c1"].digests, digests)

    def test_init_drift_kills_and_refuses(self) -> None:
        for drift in ({"tools": ["Bash"]}, {"apiKeySource": "ANTHROPIC_API_KEY"}, {"mcp_servers": [{"name": "x"}]},
                      {"permissionMode": "bypassPermissions"}):
            with self.subTest(drift=drift):
                self.init = drift
                result = self.turn()
                self.assertEqual(self.code(result), intent.ErrorCode.EGRESS_REFUSED)
                self.assertEqual(self.processes[-1].returncode, -9)
                self.assertNotIn("c1", self.adapter._warm)

    def test_ungranted_tool_is_denied_and_permission_prompts_refused(self) -> None:
        self.callbacks = [
            {"subtype": "hook_callback", "callback_id": "jarvis-deny", "input": {"tool_name": "Bash"}},
            {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}},
            {"subtype": "hook_callback", "callback_id": "jarvis-proof", "input": {}},
        ]
        self.turn()
        replies = {
            reply["response"]["request_id"]: reply["response"]
            for reply in map(json.loads, self.processes[0].written) if reply["type"] == "control_response"
        }
        self.assertEqual(replies["cb0"]["response"]["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(replies["cb1"]["response"]["behavior"], "deny")
        self.assertEqual(replies["cb2"]["response"], {"continue": True})
        self.assertTrue(self.adapter._warm["c1"].proven)

    def test_budget_is_the_turn_delta_and_overflow_interrupts(self) -> None:
        self.costs = [0.1, 1.3]
        self.turn()
        result = self.turn()
        self.assertEqual(self.code(result), intent.ErrorCode.CONNECTOR_FAILED)
        controls = [json.loads(line)["request"]["subtype"] for line in self.processes[0].written
                    if json.loads(line)["type"] == "control_request"]
        self.assertEqual(controls[-1], "interrupt")
        self.assertEqual(self.processes[0].returncode, -9)
        self.assertNotIn("c1", self.adapter._warm)

    def test_lane_near_its_cap_is_recycled(self) -> None:
        self.costs = [0.5, 0.85, 0.1]           # deltas 0.5 and 0.35 stay under the turn cap
        self.turn()
        self.turn()
        self.assertEqual(self.processes[0].returncode, -9)
        self.turn()
        self.assertEqual(self.popen.call_count, 2)
        self.assertIn(MARKER, self.processes[1].user_lines()[0])

    def test_error_result_kills_and_forgets_the_route(self) -> None:
        self.is_error = True
        with patch.object(self.adapter, "forget_billing_route") as forget:
            self.turn()
        forget.assert_called()
        self.assertEqual(self.processes[0].returncode, -9)
        self.assertNotIn("c1", self.adapter._warm)

    def test_idle_lanes_are_reaped(self) -> None:
        self.turn()
        self.now = intent.WARM_IDLE_SECONDS + 1
        self.adapter.warm_lane = "c2"
        self.turn()
        self.assertEqual(self.processes[0].returncode, -9)
        self.assertEqual(set(self.adapter._warm), {"c2"})

    def test_idle_own_lane_respawns_with_the_window(self) -> None:
        self.turn()
        self.now = intent.WARM_IDLE_SECONDS + 1
        self.turn()
        self.assertEqual(self.popen.call_count, 2)
        self.assertEqual(self.processes[0].returncode, -9)
        self.assertIn(MARKER, self.processes[1].user_lines()[0])

    def test_crash_respawns_and_resends_the_window(self) -> None:
        self.crash = True
        self.turn()
        self.assertNotIn("c1", self.adapter._warm)
        self.crash = False
        self.turn()
        self.assertEqual(self.popen.call_count, 2)
        self.assertIn(MARKER, self.processes[1].user_lines()[0])


class WarmFailClosedTest(WarmTest):
    def assertDropped(self, result, code) -> None:
        self.assertEqual(self.code(result), code)
        self.assertEqual(self.processes[-1].returncode, -9)
        self.assertNotIn("c1", self.adapter._warm)

    def test_spawn_turn_without_init_is_refused(self) -> None:
        self.send_init = False
        self.assertDropped(self.turn(), intent.ErrorCode.EGRESS_REFUSED)

    def test_unreadable_cost_is_an_overflow(self) -> None:
        for cost in (MISSING, None, float("nan"), float("inf"), True, "0.1"):
            with self.subTest(cost=cost):
                self.costs = [cost]
                self.assertDropped(self.turn(), intent.ErrorCode.CONNECTOR_FAILED)
                self.assertEqual(self.controls(self.processes[-1])[-1], "interrupt")

    def test_decreasing_cost_is_an_overflow(self) -> None:
        self.costs = [0.2, 0.1]
        self.assertIsNone(self.code(self.turn()))
        self.assertDropped(self.turn(), intent.ErrorCode.CONNECTOR_FAILED)

    def test_malformed_initialize_reply_is_refused(self) -> None:
        self.init_reply = {"type": "control_response", "response": ["x"]}
        self.assertDropped(self.turn(), intent.ErrorCode.EGRESS_REFUSED)
        self.assertEqual(self.processes[0].user_lines(), [])

    def test_malformed_control_requests_are_denied(self) -> None:
        self.callbacks = [
            "x",
            {"subtype": "hook_callback", "callback_id": "jarvis-deny", "input": "Bash"},
            {"subtype": "hook_callback", "callback_id": "jarvis-deny", "input": {"tool_name": ["Bash"]}},
            {"subtype": "hook_callback", "callback_id": "jarvis-deny", "input": {"tool_name": {"a": 1}}},
        ]
        self.assertIsNone(self.code(self.turn()))
        replies = self.replies(self.processes[0])
        self.assertEqual(replies["cb0"]["subtype"], "error")
        for key in ("cb1", "cb2", "cb3"):
            self.assertEqual(replies[key]["response"]["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_malformed_init_tools_are_drift(self) -> None:
        for tools in ([["Bash"]], [1], "Bash"):
            with self.subTest(tools=tools):
                self.init = {"tools": tools}
                self.assertDropped(self.turn(), intent.ErrorCode.EGRESS_REFUSED)

    def test_missing_proof_is_refused(self) -> None:
        self.proof = False
        self.assertDropped(self.turn(), intent.ErrorCode.EGRESS_REFUSED)

    def test_proof_without_this_turns_prompt_is_refused(self) -> None:
        for proof in ({}, {"prompt": "another request"}, {"prompt": 7}, ["what time is it"]):
            with self.subTest(proof=proof):
                self.proof_input = proof
                self.assertDropped(self.turn(), intent.ErrorCode.EGRESS_REFUSED)

    def test_stale_proof_and_result_from_an_earlier_turn_are_refused(self) -> None:
        stale_proof = {"type": "control_request", "request_id": "late", "request": {
            "subtype": "hook_callback", "callback_id": "jarvis-proof", "input": {"prompt": "first request"}}}
        self.after = [stale_proof, {"type": "result", "subtype": "success", "is_error": False,
                                    "result": "stale", "total_cost_usd": 0.15}]
        self.assertIsNone(self.code(self.turn("first request")))
        self.after = []
        self.assertDropped(self.turn("second request"), intent.ErrorCode.EGRESS_REFUSED)
        self.assertEqual(self.popen.call_count, 1)

    def test_interrupted_turn_drops_the_session(self) -> None:
        with patch.object(self.adapter, "_collect_output", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.turn()
        self.assertEqual(self.processes[0].returncode, -9)
        self.assertNotIn("c1", self.adapter._warm)

    def test_exit_reaps_a_collected_adapters_processes(self) -> None:
        self.turn()
        self.adapter = None
        gc.collect()
        intent._close_warm_sessions()
        process = self.processes[0]
        self.assertEqual((process.returncode, process.waited, process.closed), (-9, True, 2))
        self.assertEqual(intent._WARM_PROCESSES, set())

    def test_dead_on_reuse_respawns_with_the_window(self) -> None:
        first = self.turn()
        original = self.adapter._warm_alive

        def alive_then_die() -> bool:
            alive = original()
            self.processes[0].kill()
            return alive

        with patch.object(self.adapter, "_warm_alive", side_effect=alive_then_die):
            second = self.turn()
        self.assertEqual(self.code(second), self.code(first))
        self.assertEqual(self.popen.call_count, 2)
        self.assertEqual(len(self.processes[0].user_lines()), 1)
        self.assertIn(MARKER, self.processes[1].user_lines()[0])

    def test_dropped_window_state_is_per_task(self) -> None:
        self.turn()
        self.assertEqual(self.adapter._warm_dropped, set())
        first, other = self.task(), self.task()
        self.adapter._sanitized_payload(first)
        self.adapter.warm_lane = "c9"
        self.adapter._sanitized_payload(other)
        self.assertEqual(self.adapter._warm_dropped, {first.id})

    def test_exit_reaps_warm_processes(self) -> None:
        self.turn()
        intent._close_warm_sessions()
        process = self.processes[0]
        self.assertEqual((process.returncode, process.waited, process.closed), (-9, True, 2))
        self.assertEqual(intent._WARM_PROCESSES, set())


if __name__ == "__main__":
    unittest.main()
