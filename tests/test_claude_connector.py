from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.contracts import (
    ActionScope,
    DataClassification,
    OutcomeStatus,
    SessionState,
    Task,
)


class FakeProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.terminated = False

    def communicate(self, timeout=None):
        return self._stdout, self._stderr

    def poll(self):
        return None if not self.terminated else self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class ClaudeConnectorSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )

    def task(self, context=None) -> Task:
        return Task(
            objective="Analyze without changing files",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.SENSITIVE,
            ),
            context=context or {},
        )

    def test_rejects_working_directory_outside_allowed_roots(self) -> None:
        outside = self.root.parent
        with self.assertRaises(ValueError):
            self.connector._working_directory({"working_directory": str(outside)})

    def test_deep_redaction_and_outbound_preview(self) -> None:
        task = self.task(
            {
                "nested": {
                    "api_key": "sk-super-secret-123456789",
                    "notes": ["Bearer abcdefghijklmnop", {"password": "hidden"}],
                }
            }
        )
        payload, count = self.connector._sanitized_payload(task)
        serialized = json.dumps(payload)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("abcdefghijklmnop", serialized)
        self.assertNotIn("hidden", serialized)
        self.assertGreaterEqual(count, 3)
        preview = self.connector.outbound_preview(task)
        self.assertGreaterEqual(preview.redacted_items, 3)
        self.assertGreater(preview.approximate_bytes, 0)
        self.assertEqual(DataClassification.SENSITIVE, preview.data_classification)

    def test_secret_classification_redacts_entire_scope_details(self) -> None:
        task = Task(
            objective="classified objective",
            capability="code_planning",
            scope=ActionScope(
                action="classified-action",
                target="C:/classified/target",
                connector="classified-connector",
                destination="classified-destination",
                data_classification=DataClassification.SECRET,
            ),
            context={"note": "classified context"},
        )
        payload, count = self.connector._sanitized_payload(task)
        serialized = json.dumps(payload)
        for secret_value in (
            "classified objective",
            "classified-action",
            "C:/classified/target",
            "classified-connector",
            "classified-destination",
            "classified context",
        ):
            self.assertNotIn(secret_value, serialized)
        self.assertEqual("secret", payload["scope"]["data_classification"])
        self.assertGreaterEqual(count, 6)

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_unreadable_cli_detail_is_redacted_before_storage(self, popen) -> None:
        popen.return_value = FakeProcess(
            "not-json",
            returncode=1,
            stderr=(
                "Authorization: Bearer abcdefghijklmnop "
                "api_key=sk-super-secret-123456789"
            ),
        )
        result = self.connector.execute(self.task(), SessionState())
        detail = result.error.details["cli_detail"]
        self.assertNotIn("abcdefghijklmnop", detail)
        self.assertNotIn("sk-super-secret", detail)
        self.assertIn("[REDACTED]", detail)

    def test_command_is_isolated_read_only_and_budgeted(self) -> None:
        command = self.connector._command("safe prompt")
        for required in (
            "--safe-mode",
            "--strict-mcp-config",
            "--tools",
            "Read,Glob,Grep",
            "--no-session-persistence",
            "--permission-mode",
            "plan",
            "--max-budget-usd",
            "0.1500",
            "--no-chrome",
        ):
            self.assertIn(required, command)
        self.assertNotIn("--bare", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_mocked_cli_result_has_connector_specific_verification(self, popen) -> None:
        popen.return_value = FakeProcess(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "Plan ready",
                    "total_cost_usd": 0,
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                    "modelUsage": {"claude-test": {}},
                }
            )
        )
        task = self.task()
        result = self.connector.execute(task, SessionState())
        verification = self.connector.verify(task, result, SessionState())
        self.assertEqual(OutcomeStatus.SUCCESS, result.outcome)
        self.assertTrue(verification.passed)
        invoked = popen.call_args.args[0]
        self.assertIn("--safe-mode", invoked)

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_mocked_auth_failure_preserves_usage(self, popen) -> None:
        popen.return_value = FakeProcess(
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "result": "Failed to authenticate",
                    "api_error_status": 401,
                    "total_cost_usd": 0,
                    "usage": {},
                }
            ),
            returncode=1,
        )
        result = self.connector.execute(self.task(), SessionState())
        self.assertEqual(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual(0, result.usage.actual_cost_usd)
        self.assertEqual(401, result.error.details["api_error_status"])


if __name__ == "__main__":
    unittest.main()
