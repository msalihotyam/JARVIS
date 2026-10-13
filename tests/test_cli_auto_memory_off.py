"""JARVIS-started provider CLIs keep no memory of their own (Salih 2026-10-14).

Core owns memory. The Claude Code CLI gets CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 on
every spawn whatever the parent holds, and Codex runs keep their memories off.
Every subprocess is mocked: no CLI, model or network call happens here.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import jarvis.codex as codex
import jarvis.intent as intent
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.contracts import ActionScope, DataClassification, SessionState, Task
from tests.helpers import safe_cleanup
from tests.test_claude_connector import FakeProcess, OAUTH_MAX_AUTH_STATUS, patch_oauth_environment

FLAG = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
#: A parent that tries to keep the CLI's auto memory on, in both casings.
PARENT_TURNS_IT_ON = {FLAG: "0", FLAG.lower(): "0", "Claude_Code_Disable_Auto_Memory": "false"}


def flag_entries(environment: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in environment.items() if key.upper() == FLAG}


class ClaudeAutoMemoryOffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.connector = ClaudeCodeConnector(allowed_roots=(self.root,), default_working_directory=self.root)
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)
        parent = patch.dict(os.environ, PARENT_TURNS_IT_ON)
        parent.start()
        self.addCleanup(parent.stop)

    def assert_off(self, environment: dict[str, str]) -> None:
        self.assertEqual({FLAG: "1"}, flag_entries(environment))     # exactly one entry, JARVIS's own

    def test_the_builder_overrides_any_parent_value_and_casing(self) -> None:
        self.assert_off(ClaudeCodeConnector._sanitized_environment())

    def test_the_builder_drops_a_mixed_casing_key(self) -> None:
        # os.environ upper-cases keys on Windows; feed the other casings past it.
        parent = {FLAG.lower(): "0", "Claude_Code_Disable_Auto_Memory": "false", "CLAUDE_CONFIG_DIR": "/c"}
        with patch("jarvis.connectors.claude_code.trusted_environment", return_value=parent):
            child = ClaudeCodeConnector._sanitized_environment()
        self.assert_off(child)
        self.assertEqual("/c", child["CLAUDE_CONFIG_DIR"])

    def test_the_builder_sets_it_when_the_parent_has_none(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assert_off(ClaudeCodeConnector._sanitized_environment())

    def test_the_allowlist_and_the_billing_subtraction_are_unchanged(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/config/claude", "OPENAI_API_KEY": "placeholder",
                                     "claude_code_use_bedrock": "1", "ANTHROPIC_API_KEY": "placeholder"}):
            child = ClaudeCodeConnector._sanitized_environment()
        self.assertEqual("/config/claude", child["CLAUDE_CONFIG_DIR"])
        for gone in ("OPENAI_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY"):
            self.assertEqual([], [key for key in child if key.upper() == gone])

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_the_run_spawn_carries_it(self, popen) -> None:
        popen.return_value = FakeProcess("not-json", returncode=1)
        task = Task(objective="Analyze without changing files", capability="code_planning",
                    scope=ActionScope(action="analyze", target=str(self.root), destination="anthropic/claude-code",
                                      data_classification=DataClassification.SENSITIVE))
        with patch.object(ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS):
            self.connector.execute(task, SessionState())
        popen.assert_called_once()
        self.assert_off(popen.call_args[1]["env"])

    @patch("jarvis.connectors.claude_code.subprocess.run")
    def test_the_auth_status_probe_carries_it(self, run) -> None:
        run.return_value = MagicMock(returncode=0, stdout=json.dumps(OAUTH_MAX_AUTH_STATUS))
        with patch.object(ClaudeCodeConnector, "_argv0", return_value="C:/abs/claude.exe"):
            self.connector._auth_status()
        run.assert_called_once()
        self.assert_off(run.call_args[1]["env"])

    def test_the_model_catalog_probe_carries_it(self) -> None:
        process = MagicMock()
        process.stdout = iter([])
        located = MagicMock()
        located.argv0.return_value = "C:/abs/claude.exe"
        with patch.object(intent, "resolve_trusted_executable", return_value=located), \
                patch.object(intent.subprocess, "Popen", return_value=process) as popen:
            intent.claude_model_catalog()
        popen.assert_called_once()
        self.assert_off(popen.call_args[1]["env"])


class CodexMemoriesOffTests(unittest.TestCase):
    STATE = {"status": "available", "plan": "plus",
             "models": [{"id": "gpt-5.6-luna", "name": "L", "efforts": ["low"], "default_effort": "low"}]}

    def test_exec_ignores_the_user_config_persists_nothing_and_disables_memories(self) -> None:
        process = MagicMock(returncode=0)
        process.communicate.return_value = ("", "")
        adapter = codex.CodexIntentAdapter("system", "gpt-5.6-luna", "low", state=lambda: self.STATE)
        task = Task(objective="hi", capability=intent.INTENT_CAPABILITY, scope=ActionScope(action="understand", target=""))
        with patch.object(codex, "codex_executable", return_value="codex.exe"), \
                patch.object(codex.subprocess, "Popen", return_value=process) as popen:
            adapter.complete(task, SessionState(objective="hi"))
        argv = popen.call_args[0][0]
        self.assertEqual("exec", argv[1])
        self.assertIn("--ephemeral", argv)
        self.assertIn("--ignore-user-config", argv)               # the user's config.toml turns memories on
        self.assertIn(("--disable", "memories"), list(zip(argv, argv[1:])))

    def test_the_app_server_probe_disables_memories(self) -> None:
        process = MagicMock()
        process.stdout = iter([])
        with patch.object(codex.subprocess, "Popen", return_value=process) as popen:
            codex._app_server("C:/abs/codex.exe", [("account/read", None)], 0.01)
        argv = popen.call_args[0][0]
        self.assertEqual(["C:/abs/codex.exe", "app-server", "--disable", "memories"], argv)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
