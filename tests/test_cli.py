"""B1 CLI surface tests.

Every test patches the production connector class and the auth/model process
boundaries so that touching them fails immediately. No test starts a
subprocess, an auth probe or a model call.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis import app as app_module
from jarvis import cli
from jarvis.app import (
    MAX_TASK_JSON_BYTES,
    CommandResult,
    TaskInputError,
    create_app,
)
from jarvis.connectors import claude_code
from jarvis.contracts import (
    BillingClass,
    OutboundDataPreview,
    OutcomeStatus,
    PermissionMode,
    RiskLevel,
    SideEffect,
    StructuredError,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector
from tests.test_app import task_document


OBJECTIVE = "OBJECTIVE-MUST-NOT-APPEAR"
CONTEXT_SECRET = "CONTEXT-VALUE-MUST-NOT-APPEAR"
PREFERENCE_SECRET = "ASSUMPTION-MUST-NOT-APPEAR"


class FakeConnector(DummyConnector):
    """Fake with output the envelope assertions can pin down exactly."""

    def __init__(
        self,
        *,
        summary: str = "fake connector summary",
        next_step: str = "review the plan",
        preview: bool = False,
        preview_summary: str = "redacted outbound preview",
        reason: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._summary = summary
        self._next_step = next_step
        self._preview = preview
        self._preview_summary = preview_summary
        self._reason = reason

    def outbound_preview(self, task):
        if not self._preview:
            return None
        return OutboundDataPreview(
            destination=task.scope.destination,
            data_classification=task.scope.data_classification,
            field_names=("objective", "capability", "scope"),
            approximate_bytes=256,
            redacted_items=2,
            summary=self._preview_summary,
        )

    def permission_request(
        self,
        task,
        session_id,
        approval_ttl_minutes=30,
        *,
        scope_override=None,
        reason=None,
    ):
        return super().permission_request(
            task,
            session_id,
            approval_ttl_minutes,
            scope_override=scope_override,
            reason=reason or self._reason,
        )

    def execute(self, task, session):
        result = super().execute(task, session)
        # The summary is deliberately independent of the objective so the
        # objective can be asserted absent from every envelope.
        result.summary = self._summary
        if result.error is not None:
            result.error = StructuredError(
                result.error.code, self._summary, retryable=result.error.retryable
            )
        result.data = {
            "next_step": self._next_step,
            "diagnostic": "internal-only-diagnostic",
        }
        # Numeric telemetry must survive redaction even though the field names
        # contain "token".
        result.usage.input_tokens = 1234
        result.usage.output_tokens = 567
        return result


def _must_not_build(settings: Settings):
    raise AssertionError("The application must not be built for this invocation.")


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.settings = Settings(memory_database=self.database)
        self.process_mocks = []
        for target, attribute in (
            (claude_code.subprocess, "run"),
            (claude_code.subprocess, "Popen"),
            (claude_code.shutil, "which"),
        ):
            patcher = mock.patch.object(
                target,
                attribute,
                side_effect=AssertionError(f"{attribute} must not be called"),
            )
            self.process_mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        connector_patcher = mock.patch.object(
            app_module,
            "ClaudeCodeConnector",
            side_effect=AssertionError("The production connector must not be built."),
        )
        self.production_connector = connector_patcher.start()
        self.addCleanup(connector_patcher.stop)

    # -- helpers -----------------------------------------------------------

    def factory_for(self, connector: DummyConnector):
        def factory(settings: Settings):
            return create_app(
                settings=settings,
                memory=self.memory,
                registry=ToolRegistry(),
                connectors=(connector,),
            )

        return factory

    def invoke(self, argv, connector=None, settings=None, app_factory=None):
        factory = app_factory or self.factory_for(connector or FakeConnector())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                argv, settings=settings or self.settings, app_factory=factory
            )
        return code, out.getvalue(), err.getvalue()

    def task_file(self, **overrides) -> str:
        overrides.setdefault("objective", OBJECTIVE)
        path = self.root / "task.json"
        path.write_text(task_document(**overrides), encoding="utf-8")
        return str(path)

    def start_awaiting_approval(self, connector):
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()], connector
        )
        self.assertEqual(10, code)
        payload = json.loads(output)
        return payload, payload["session"]["id"], payload["task"]["id"]

    # -- status ------------------------------------------------------------

    def test_status_output_is_backward_compatible(self) -> None:
        expected = {
            "mode": "local",
            "schema_version": 3,
            "permission_mode": "session",
            "monthly_target_usd": 20.0,
            "monthly_hard_limit_usd": 20.0,
            "paid_api_enabled": False,
        }
        self.assertEqual(3, self.memory.schema_version())
        for argv in ([], ["status"]):
            with self.subTest(argv=argv):
                code, output, errors = self.invoke(argv, app_factory=_must_not_build)
                self.assertEqual(0, code)
                self.assertEqual(json.dumps(expected, indent=2) + "\n", output)
                self.assertEqual(list(expected), list(json.loads(output)))
                self.assertEqual("", errors)

    def test_status_never_builds_the_production_connector(self) -> None:
        # The default factory is used here; the patched production connector
        # class raises if it is constructed at all.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["status"], settings=self.settings)
        self.assertEqual(0, code)
        self.assertFalse(self.production_connector.called)
        self.assertEqual("local", json.loads(out.getvalue())["mode"])

    # -- dispatch ----------------------------------------------------------

    def test_run_dispatches_and_completes(self) -> None:
        code, output, _ = self.invoke(["run", "--task-json", self.task_file()])
        payload = json.loads(output)
        self.assertEqual(0, code)
        self.assertEqual("run", payload["command"])
        self.assertEqual("completed", payload["run_status"])

    def test_run_reads_task_json_from_stdin(self) -> None:
        document = task_document(objective=OBJECTIVE)
        with mock.patch.object(cli.sys, "stdin", io.StringIO(document)):
            code, output, _ = self.invoke(["run", "--task-json", "-"])
        self.assertEqual(0, code)
        self.assertEqual("run", json.loads(output)["command"])

    def test_resume_dispatches(self) -> None:
        connector = FakeConnector(risk=RiskLevel.MEDIUM)
        _, session_id, task_id = self.start_awaiting_approval(connector)
        code, output, _ = self.invoke(
            ["resume", "--session-id", session_id, "--task-id", task_id], connector
        )
        self.assertEqual(10, code)
        self.assertEqual("resume", json.loads(output)["command"])

    def test_clarify_dispatches(self) -> None:
        connector = FakeConnector()
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file(preferences={"needs_clarification": True})],
            connector,
        )
        self.assertEqual(11, code)
        started = json.loads(output)
        code, output, _ = self.invoke(
            [
                "clarify",
                "--session-id",
                started["session"]["id"],
                "--task-id",
                started["task"]["id"],
                "--text",
                "the core package only",
            ],
            connector,
        )
        self.assertEqual(0, code)
        self.assertEqual("clarify", json.loads(output)["command"])

    def test_approve_dispatches_and_executes(self) -> None:
        connector = FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS, preview=True)
        payload, session_id, task_id = self.start_awaiting_approval(connector)
        code, output, _ = self.invoke(
            [
                "approve",
                "--session-id",
                session_id,
                "--task-id",
                task_id,
                "--request-id",
                payload["approval"]["request_id"],
                "--fingerprint",
                payload["approval"]["fingerprint"],
            ],
            connector,
        )
        self.assertEqual(0, code)
        self.assertEqual("approve", json.loads(output)["command"])
        self.assertEqual(1, connector.execute_calls)

    def test_reject_dispatches_without_executing(self) -> None:
        connector = FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        payload, session_id, task_id = self.start_awaiting_approval(connector)
        code, output, _ = self.invoke(
            [
                "reject",
                "--session-id",
                session_id,
                "--task-id",
                task_id,
                "--request-id",
                payload["approval"]["request_id"],
                "--fingerprint",
                payload["approval"]["fingerprint"],
            ],
            connector,
        )
        self.assertEqual(20, code)
        self.assertEqual("reject", json.loads(output)["command"])
        self.assertEqual(0, connector.execute_calls)

    def test_cancel_dispatches(self) -> None:
        connector = FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        _, session_id, task_id = self.start_awaiting_approval(connector)
        code, output, _ = self.invoke(
            ["cancel", "--session-id", session_id, "--task-id", task_id], connector
        )
        self.assertEqual(21, code)
        self.assertEqual("cancel", json.loads(output)["command"])
        self.assertEqual(0, connector.execute_calls)

    # -- permission mode ---------------------------------------------------

    def test_permission_mode_precedence(self) -> None:
        code, output, _ = self.invoke(["run", "--task-json", self.task_file()])
        self.assertEqual("session", json.loads(output)["effective_permission_mode"])

        injected = replace(self.settings, permission_mode=PermissionMode.STRICT)
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()], settings=injected
        )
        self.assertEqual(10, code)
        self.assertEqual("strict", json.loads(output)["effective_permission_mode"])

        # An explicit flag wins over the injected settings.
        code, output, _ = self.invoke(
            ["--permission-mode", "smart_auto", "run", "--task-json", self.task_file()],
            settings=injected,
        )
        self.assertEqual("smart_auto", json.loads(output)["effective_permission_mode"])

        code, output, _ = self.invoke(
            ["--permission-mode", "strict", "status"], app_factory=_must_not_build
        )
        self.assertEqual("strict", json.loads(output)["permission_mode"])

    # -- exit codes --------------------------------------------------------

    def test_completed_exit_code(self) -> None:
        code, _, _ = self.invoke(["run", "--task-json", self.task_file()])
        self.assertEqual(0, code)

    def test_awaiting_approval_exit_code(self) -> None:
        code, _, _ = self.invoke(
            ["run", "--task-json", self.task_file()],
            FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS),
        )
        self.assertEqual(10, code)

    def test_awaiting_clarification_exit_code(self) -> None:
        code, _, _ = self.invoke(
            [
                "run",
                "--task-json",
                self.task_file(preferences={"needs_clarification": True}),
            ]
        )
        self.assertEqual(11, code)

    def test_not_found_exit_code(self) -> None:
        code, output, _ = self.invoke(
            ["resume", "--session-id", "nope", "--task-id", "nope"]
        )
        self.assertEqual(66, code)
        self.assertEqual("unknown_session", json.loads(output)["error"]["code"])

    def test_policy_refusal_exit_code(self) -> None:
        connector = FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        payload, session_id, task_id = self.start_awaiting_approval(connector)
        code, output, _ = self.invoke(
            [
                "approve",
                "--session-id",
                session_id,
                "--task-id",
                task_id,
                "--request-id",
                payload["approval"]["request_id"],
                "--fingerprint",
                "0" * 64,
            ],
            connector,
        )
        self.assertEqual(77, code)
        self.assertEqual("invalid_grant", json.loads(output)["error"]["code"])
        self.assertEqual(0, connector.execute_calls)

    def test_connector_failure_exit_code(self) -> None:
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()],
            FakeConnector(outcome=OutcomeStatus.ERROR),
        )
        self.assertEqual(70, code)
        self.assertEqual("connector_failed", json.loads(output)["error"]["code"])

    def test_invalid_task_json_exit_code(self) -> None:
        path = self.root / "broken.json"
        path.write_text('{"objective":', encoding="utf-8")
        code, output, _ = self.invoke(["run", "--task-json", str(path)])
        self.assertEqual(64, code)
        self.assertEqual("invalid_task", json.loads(output)["error"]["code"])

    def test_unreadable_task_file_exit_code(self) -> None:
        code, output, _ = self.invoke(
            ["run", "--task-json", str(self.root / "missing.json")]
        )
        self.assertEqual(64, code)
        self.assertEqual("invalid_task", json.loads(output)["error"]["code"])

    # -- argument failures -------------------------------------------------

    def test_argument_failure_emits_one_json_envelope(self) -> None:
        for argv in (
            ["approve"],
            ["run"],
            ["nonsense-command"],
            ["--permission-mode", "invalid", "status"],
            ["run", "--task-json", "task.json", "--unknown-flag"],
        ):
            with self.subTest(argv=argv):
                code, output, errors = self.invoke(argv, app_factory=_must_not_build)
                self.assertEqual(64, code)
                # Exactly one JSON document, and nothing on stderr.
                payload = json.loads(output)
                self.assertEqual("", errors)
                self.assertEqual("cli", payload["command"])
                self.assertEqual("invalid_task", payload["error"]["code"])
                self.assertIsNone(payload["run_status"])
                self.assertIsNone(payload["session"])
                self.assertIsNone(payload["task"])

    def test_help_exits_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(
                ["--help"], settings=self.settings, app_factory=_must_not_build
            )
        self.assertEqual(0, code)
        self.assertIn("usage:", out.getvalue())

    # -- bounded input -----------------------------------------------------

    def test_file_input_is_read_with_a_bound(self) -> None:
        opener = mock.mock_open(read_data=b"{}")
        with mock.patch.object(Path, "open", opener):
            cli._read_task_json(str(self.root / "task.json"))
        opener.return_value.read.assert_called_once_with(MAX_TASK_JSON_BYTES + 1)

    def test_stdin_input_is_read_with_a_bound(self) -> None:
        class RecordingStream:
            def __init__(self) -> None:
                self.requested: int | None = None

            def read(self, size: int = -1) -> bytes:
                self.requested = size
                return b"{}"

        stream = RecordingStream()
        with mock.patch.object(cli.sys, "stdin", mock.Mock(buffer=stream)):
            cli._read_task_json("-")
        self.assertEqual(MAX_TASK_JSON_BYTES + 1, stream.requested)

    def test_oversized_file_is_refused(self) -> None:
        path = self.root / "big.json"
        path.write_bytes(b'{"objective": "' + b"a" * MAX_TASK_JSON_BYTES + b'"}')
        with self.assertRaises(TaskInputError):
            cli._read_task_json(str(path))
        code, output, _ = self.invoke(["run", "--task-json", str(path)])
        self.assertEqual(64, code)
        self.assertIn("larger than", json.loads(output)["error"]["message"])

    def test_oversized_stdin_is_refused(self) -> None:
        oversized = '{"objective": "' + "a" * MAX_TASK_JSON_BYTES + '"}'
        with mock.patch.object(cli.sys, "stdin", io.StringIO(oversized)):
            code, output, _ = self.invoke(["run", "--task-json", "-"])
        self.assertEqual(64, code)
        self.assertEqual("invalid_task", json.loads(output)["error"]["code"])

    # -- output guarantees -------------------------------------------------

    def test_awaiting_approval_envelope_exposes_fingerprints_and_preview(self) -> None:
        connector = FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS, preview=True)
        payload, session_id, task_id = self.start_awaiting_approval(connector)
        persisted = self.memory.load_pending_approval(task_id, session_id)
        approval = payload["approval"]
        self.assertEqual(persisted.id, approval["request_id"])
        self.assertEqual(persisted.fingerprint, approval["fingerprint"])
        self.assertEqual(persisted.scope_fingerprint, approval["scope_fingerprint"])
        self.assertEqual(persisted.expires_at, approval["expires_at"])
        self.assertEqual(
            "redacted outbound preview", approval["outbound_preview"]["summary"]
        )
        self.assertEqual(2, approval["outbound_preview"]["redacted_items"])
        self.assertIsNone(payload["result"])

    def test_no_raw_payload_context_or_preferences_is_printed(self) -> None:
        connector = FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS, preview=True)
        path = self.task_file(
            context={"inputs": {"note": CONTEXT_SECRET}},
            preferences={"assumption": PREFERENCE_SECRET},
        )
        code, output, _ = self.invoke(["run", "--task-json", path], connector)
        self.assertEqual(10, code)
        self.assertNotIn(OBJECTIVE, output)
        self.assertNotIn(CONTEXT_SECRET, output)
        self.assertNotIn(PREFERENCE_SECRET, output)
        # The prompt and the serialized outbound payload never appear either.
        self.assertNotIn("JARVIS read-only planning request", output)
        payload = json.loads(output)
        self.assertNotIn("context", payload["task"])
        self.assertNotIn("preferences", payload["task"])
        self.assertNotIn("plan", payload["task"])
        self.assertNotIn("result_snapshot", payload["task"])

    def test_completed_output_is_limited_to_the_allowlist(self) -> None:
        code, output, _ = self.invoke(["run", "--task-json", self.task_file()])
        self.assertEqual(0, code)
        payload = json.loads(output)
        self.assertEqual(
            {
                "schema_version",
                "command",
                "effective_permission_mode",
                "run_status",
                "session",
                "task",
                "approval",
                "billing",
                "result",
                "error",
            },
            set(payload),
        )
        self.assertEqual({"summary", "usage", "verification"}, set(payload["result"]))
        self.assertEqual(
            {
                "provider",
                "model",
                "connector",
                "input_tokens",
                "output_tokens",
                "call_count",
                "billing_class",
                "monetary_state",
                "monetary_cost_usd",
                "reference_cost_usd",
                "billing_evidence",
            },
            set(payload["result"]["usage"]),
        )
        self.assertEqual(
            {"status", "method", "evidence"}, set(payload["result"]["verification"])
        )
        self.assertEqual({"id", "status", "stage", "selected_connector"}, set(payload["task"]))
        self.assertEqual(
            {"class", "monetary_maximum_usd", "resource_ceiling_usd"},
            set(payload["billing"]),
        )
        # Arbitrary result data and internal billing markers stay local.
        self.assertNotIn("internal-only-diagnostic", output)
        self.assertNotIn("billing_preflight_fingerprint", output)
        self.assertNotIn("billing_repreflight_attempts", output)
        self.assertEqual("review the plan", payload["session"]["next_step"])

    def test_error_details_are_never_printed(self) -> None:
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()],
            FakeConnector(verification_passes=False),
        )
        self.assertEqual(70, code)
        payload = json.loads(output)
        self.assertEqual({"code", "message", "retryable"}, set(payload["error"]))
        self.assertEqual("verification_failed", payload["error"]["code"])

    def test_credentials_in_a_permitted_summary_are_redacted(self) -> None:
        connector = FakeConnector(summary="leaked sk-abcdefghijklmnop from the tool")
        code, output, _ = self.invoke(["run", "--task-json", self.task_file()], connector)
        self.assertEqual(0, code)
        self.assertNotIn("sk-abcdefghijklmnop", output)
        self.assertIn("[REDACTED]", json.loads(output)["result"]["summary"])

    def test_the_same_agent_run_renders_byte_identically(self) -> None:
        connector = FakeConnector()
        code, first, _ = self.invoke(["run", "--task-json", self.task_file()], connector)
        self.assertEqual(0, code)
        payload = json.loads(first)
        argv = [
            "resume",
            "--session-id",
            payload["session"]["id"],
            "--task-id",
            payload["task"]["id"],
        ]
        _, one, _ = self.invoke(argv, connector)
        _, two, _ = self.invoke(argv, connector)
        self.assertEqual(one, two)
        self.assertEqual(
            one.encode("utf-8"), two.encode("utf-8"), "rendering must be byte-stable"
        )

    def test_unicode_renders_through_a_cp1254_stream(self) -> None:
        connector = FakeConnector(next_step="Türkçe sonraki adım")
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1254", newline="")
        factory = self.factory_for(connector)
        with contextlib.redirect_stdout(stream):
            code = cli.main(
                ["run", "--task-json", self.task_file()],
                settings=self.settings,
                app_factory=factory,
            )
            stream.flush()
        text = buffer.getvalue().decode("cp1254")
        stream.detach()
        self.assertEqual(0, code)
        self.assertIn("\\u", text)
        self.assertEqual("Türkçe sonraki adım", json.loads(text)["session"]["next_step"])

    def test_labelled_credentials_are_redacted_in_every_free_form_field(self) -> None:
        credential = "hunter2"
        # result summary
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()],
            FakeConnector(summary=f"connector said password={credential}"),
        )
        self.assertEqual(0, code)
        self.assertNotIn(credential, output)
        self.assertIn("[REDACTED]", output)

        # error message
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()],
            FakeConnector(
                outcome=OutcomeStatus.ERROR, summary=f"API_KEY: {credential} rejected"
            ),
        )
        self.assertEqual(70, code)
        self.assertNotIn(credential, output)
        self.assertIn("[REDACTED]", json.loads(output)["error"]["message"])

        # permission reason
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()],
            FakeConnector(
                side_effect=SideEffect.EXTERNAL_EGRESS,
                reason=f"Authorization: Basic-{credential}",
            ),
        )
        self.assertEqual(10, code)
        self.assertNotIn(credential, output)
        self.assertIn("[REDACTED]", json.loads(output)["approval"]["reason"])

        # session next step
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()],
            FakeConnector(next_step=f"rotate the token: {credential}"),
        )
        self.assertEqual(0, code)
        self.assertNotIn(credential, output)
        self.assertIn("[REDACTED]", json.loads(output)["session"]["next_step"])

        # scope string
        code, output, _ = self.invoke(
            [
                "run",
                "--task-json",
                self.task_file(
                    scope={"action": "read", "target": f"secret={credential}"}
                ),
            ],
            FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS),
        )
        self.assertEqual(10, code)
        self.assertNotIn(credential, output)

        # outbound preview string
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()],
            FakeConnector(
                side_effect=SideEffect.EXTERNAL_EGRESS,
                preview=True,
                preview_summary=f"payload carried api-key={credential}",
            ),
        )
        self.assertEqual(10, code)
        self.assertNotIn(credential, output)
        self.assertIn(
            "[REDACTED]", json.loads(output)["approval"]["outbound_preview"]["summary"]
        )

    def test_unlabelled_credential_shapes_remain_redacted(self) -> None:
        cases = (
            ("leaked sk-abcdefghijklmnop now", "sk-abcdefghijklmnop"),
            ("Bearer abcdefghijklmnop now", "abcdefghijklmnop"),
            (
                "jwt aaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc now",
                "aaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc",
            ),
        )
        for summary, credential in cases:
            with self.subTest(credential=credential):
                code, output, _ = self.invoke(
                    ["run", "--task-json", self.task_file()],
                    FakeConnector(summary=summary),
                )
                self.assertEqual(0, code)
                self.assertNotIn(credential, output)
                self.assertIn("[REDACTED]", json.loads(output)["result"]["summary"])

    def test_numeric_usage_telemetry_survives_redaction(self) -> None:
        code, output, _ = self.invoke(["run", "--task-json", self.task_file()])
        self.assertEqual(0, code)
        usage = json.loads(output)["result"]["usage"]
        self.assertEqual(1234, usage["input_tokens"])
        self.assertEqual(567, usage["output_tokens"])
        self.assertEqual(1, usage["call_count"])
        for field in ("input_tokens", "output_tokens", "call_count"):
            self.assertIsInstance(usage[field], int)

    def test_parser_refused_input_is_invalid_input_not_a_connector_failure(self) -> None:
        limit = sys.get_int_max_str_digits()
        huge_integer = (
            '{"objective": "o", "capability": "echo", "scope": {"action": "read"},'
            ' "context": {"inputs": ' + "9" * (limit + 10) + "}}"
            if limit
            else ""
        )
        deep_nesting = (
            '{"objective": "o", "capability": "echo", "scope": {"action": "read"},'
            ' "context": {"inputs": ' + "[" * 20000 + "]" * 20000 + "}}"
        )
        cases = [("deep_nesting", deep_nesting)]
        if limit:
            cases.append(("huge_integer", huge_integer))
        for label, document in cases:
            with self.subTest(case=label):
                path = self.root / f"{label}.json"
                path.write_text(document, encoding="utf-8")
                connector = FakeConnector()
                code, output, _ = self.invoke(
                    ["run", "--task-json", str(path)], connector
                )
                payload = json.loads(output)
                self.assertEqual(64, code)
                self.assertEqual("invalid_task", payload["error"]["code"])
                self.assertNotEqual("connector_failed", payload["error"]["code"])
                self.assertEqual(0, connector.execute_calls)
        self.assertFalse(self.production_connector.called)
        for process_mock in self.process_mocks:
            self.assertFalse(process_mock.called)

    def test_remember_for_session_flag_reaches_the_facade(self) -> None:
        class RecordingApp:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def approve(self, *args):
                self.calls.append(args)
                return CommandResult({"command": "approve"}, 0)

        for argv_suffix, expected in ((["--remember-for-session"], True), ([], False)):
            with self.subTest(expected=expected):
                recorder = RecordingApp()
                code, _, _ = self.invoke(
                    [
                        "approve",
                        "--session-id",
                        "session-1",
                        "--task-id",
                        "task-1",
                        "--request-id",
                        "request-1",
                        "--fingerprint",
                        "f" * 64,
                    ]
                    + argv_suffix,
                    app_factory=lambda settings, app=recorder: app,
                )
                self.assertEqual(0, code)
                self.assertEqual(1, len(recorder.calls))
                self.assertEqual(
                    ("session-1", "task-1", "request-1", "f" * 64, expected),
                    recorder.calls[0],
                )
                self.assertIs(expected, recorder.calls[0][4])

    def test_awaiting_cost_override_maps_to_exit_ten(self) -> None:
        # A metered route above the 20 USD hard cap, with the separate
        # paid-API gate deliberately not triggered.
        connector = FakeConnector(
            billing_class=BillingClass.METERED_MONETARY, estimate_max=25.0
        )
        code, output, _ = self.invoke(
            ["run", "--task-json", self.task_file()], connector
        )
        payload = json.loads(output)
        self.assertEqual(10, code)
        self.assertEqual("awaiting_cost_override", payload["run_status"])
        self.assertEqual("override_monthly_cost_cap", payload["approval"]["scope"]["action"])
        self.assertEqual("metered_monetary", payload["billing"]["class"])
        self.assertEqual(25.0, payload["billing"]["monetary_maximum_usd"])
        self.assertEqual(0, connector.execute_calls)

    def test_no_auth_probe_or_model_process_is_ever_reached(self) -> None:
        connector = FakeConnector(side_effect=SideEffect.EXTERNAL_EGRESS, preview=True)
        payload, session_id, task_id = self.start_awaiting_approval(connector)
        self.invoke(
            [
                "approve",
                "--session-id",
                session_id,
                "--task-id",
                task_id,
                "--request-id",
                payload["approval"]["request_id"],
                "--fingerprint",
                payload["approval"]["fingerprint"],
            ],
            connector,
        )
        self.assertFalse(self.production_connector.called)
        for process_mock in self.process_mocks:
            self.assertFalse(process_mock.called)


if __name__ == "__main__":
    unittest.main()
