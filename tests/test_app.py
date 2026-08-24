"""B1 application boundary tests.

Every test here uses a fake connector and temporary persistence. No test
constructs a live Claude route, starts a subprocess or performs a model call.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis import app as app_module
from jarvis.app import (
    ALLOWED_DESTINATION,
    MAX_TASK_JSON_BYTES,
    REDACTED,
    TaskInputError,
    build_default_app,
    build_envelope,
    create_app,
    decode_task_json,
)
from jarvis.connectors import claude_code
from jarvis.contracts import (
    AgentRun,
    BillingClass,
    CostEstimate,
    DataClassification,
    ErrorCode,
    LoopStage,
    PermissionMode,
    RiskLevel,
    RunStatus,
    SideEffect,
    StructuredError,
    TaskProgress,
    TaskStatus,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DummyConnector, safe_cleanup


def task_document(**overrides: object) -> str:
    document: dict[str, object] = {
        "objective": "echo this",
        "capability": "echo",
        "scope": {"action": "read", "target": "resource-a"},
    }
    document.update(overrides)
    return json.dumps(document)


def past_timestamp() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


class TaskInputBoundaryTests(unittest.TestCase):
    def test_accepts_every_documented_field(self) -> None:
        task = decode_task_json(
            json.dumps(
                {
                    "objective": "analyse the loop",
                    "capability": "code_analysis",
                    "scope": {
                        "action": "read",
                        "target": "resource-a",
                        "destination": "anthropic/claude-code",
                        "data_classification": "sensitive",
                    },
                    "context": {
                        "working_directory": "workspace",
                        "inputs": {"files": ["a.py"], "depth": 2},
                    },
                    "preferences": {
                        "preferred_connector": "dummy",
                        "needs_clarification": True,
                        "clarification_question": "Which module?",
                        "assumption": "Only the core package.",
                    },
                }
            )
        )
        self.assertEqual("analyse the loop", task.objective)
        self.assertEqual("code_analysis", task.capability)
        self.assertEqual("read", task.scope.action)
        self.assertEqual("resource-a", task.scope.target)
        self.assertEqual(ALLOWED_DESTINATION, task.scope.destination)
        self.assertIs(DataClassification.SENSITIVE, task.scope.data_classification)
        self.assertEqual(
            {"working_directory": "workspace", "inputs": {"files": ["a.py"], "depth": 2}},
            task.context,
        )
        self.assertEqual(
            {
                "preferred_connector": "dummy",
                "needs_clarification": True,
                "clarification_question": "Which module?",
                "assumption": "Only the core package.",
            },
            task.preferences,
        )

    def test_project_id_is_validated_at_the_boundary(self) -> None:
        accepted = decode_task_json(task_document(project_id="proj-a"))
        self.assertEqual("proj-a", accepted.project_id)
        absent = decode_task_json(task_document())
        self.assertIsNone(absent.project_id)
        for bad in ("Not A Slug", "a/b", "x" * 65, 7, {"id": "proj-a"}):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(TaskInputError):
                    decode_task_json(task_document(project_id=bad))

    def test_documented_defaults_and_internal_state(self) -> None:
        task = decode_task_json(task_document(scope={"action": "read"}))
        self.assertEqual("", task.scope.target)
        self.assertEqual(ALLOWED_DESTINATION, task.scope.destination)
        self.assertIs(DataClassification.INTERNAL, task.scope.data_classification)
        self.assertEqual({}, task.context)
        self.assertEqual({}, task.preferences)
        # Lifecycle, risk and billing state are generated internally.
        self.assertIs(TaskStatus.PENDING, task.status)
        self.assertIs(LoopStage.UNDERSTAND, task.stage)
        self.assertEqual([], task.plan)
        self.assertEqual(TaskProgress(), task.progress)
        self.assertIs(RiskLevel.LOW, task.risk_level)
        self.assertIs(SideEffect.NONE, task.side_effect.effect)
        self.assertIs(BillingClass.UNKNOWN, task.estimate.billing_class)
        self.assertIsNone(task.visible_assumption)
        self.assertIsNone(task.selected_connector)
        self.assertIsNone(task.result_snapshot)
        self.assertFalse(task.cancellation_requested)
        self.assertTrue(task.id)
        self.assertTrue(task.created_at)

    def test_generates_a_fresh_task_id_for_every_decode(self) -> None:
        first = decode_task_json(task_document())
        second = decode_task_json(task_document())
        self.assertNotEqual(first.id, second.id)

    def test_rejects_protected_top_level_fields(self) -> None:
        protected = (
            "id",
            "session_id",
            "status",
            "stage",
            "plan",
            "progress",
            "latest_task_id",
            "completed_stages",
            "open_issues",
            "artifacts",
            "estimate",
            "risk_level",
            "side_effect",
            "selected_connector",
            "visible_assumption",
            "result_snapshot",
            "cancellation_requested",
            "created_at",
            "updated_at",
            "permission_request",
            "grant",
            "fingerprint",
            "scope_fingerprint",
            "billing_class",
            "monetary_cost_usd",
        )
        for field in protected:
            with self.subTest(field=field):
                with self.assertRaises(TaskInputError) as caught:
                    decode_task_json(task_document(**{field: "x"}))
                self.assertIn(field, str(caught.exception))

    def test_rejects_protected_and_unknown_nested_fields(self) -> None:
        cases = (
            ("scope", {"action": "read", "connector": "claude-code-plan"}),
            ("scope", {"action": "read", "fingerprint": "abc"}),
            ("scope", {"action": "read", "billing_class": "no_additional_charge"}),
            ("context", {"clarification": "already answered"}),
            ("context", {"grant": {"approved": True}}),
            ("preferences", {"clarification": "already answered"}),
            ("preferences", {"paid_api_task_approval": "request-id"}),
            ("preferences", {"cost_cap_override": "request-id"}),
            ("preferences", {"billing_preflight_fingerprint": "abc"}),
            ("preferences", {"billing_repreflight_attempts": 9}),
            ("preferences", {"anything_else": "value"}),
            # Progress and session-navigation authority are internal state.
            ("scope", {"action": "read", "progress": {}}),
            ("context", {"progress": {"completed_stages": ["execute"]}}),
            ("preferences", {"progress": {"open_issues": []}}),
            ("preferences", {"latest_task_id": "task-1"}),
        )
        for section, payload in cases:
            with self.subTest(section=section, payload=payload):
                document = {
                    "objective": "o",
                    "capability": "echo",
                    "scope": {"action": "read"},
                }
                document[section] = payload
                with self.assertRaises(TaskInputError):
                    decode_task_json(json.dumps(document))

    def test_rejects_malformed_and_non_object_json(self) -> None:
        for raw in ('{"objective":', "[]", '"text"', "12", "null"):
            with self.subTest(raw=raw):
                with self.assertRaises(TaskInputError):
                    decode_task_json(raw)

    def test_rejects_invalid_utf8(self) -> None:
        with self.assertRaises(TaskInputError):
            decode_task_json(b'{"objective": "\xff\xfe"}')

    def test_rejects_duplicate_keys(self) -> None:
        raw = '{"objective": "a", "objective": "b", "capability": "echo", "scope": {"action": "read"}}'
        with self.assertRaises(TaskInputError) as caught:
            decode_task_json(raw)
        self.assertIn("Duplicate", str(caught.exception))

    def test_rejects_non_finite_numbers(self) -> None:
        for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(literal=literal):
                raw = (
                    '{"objective": "o", "capability": "echo", '
                    '"scope": {"action": "read"}, '
                    f'"context": {{"inputs": {literal}}}}}'
                )
                with self.assertRaises(TaskInputError):
                    decode_task_json(raw)

    def test_rejects_input_above_the_size_limit(self) -> None:
        oversized = '{"objective": "' + ("a" * MAX_TASK_JSON_BYTES) + '"}'
        self.assertGreater(len(oversized.encode("utf-8")), MAX_TASK_JSON_BYTES)
        with self.assertRaises(TaskInputError) as caught:
            decode_task_json(oversized)
        self.assertIn("larger than", str(caught.exception))

    def test_rejects_a_foreign_destination(self) -> None:
        with self.assertRaises(TaskInputError):
            decode_task_json(
                task_document(
                    scope={"action": "read", "destination": "https://example.invalid/x"}
                )
            )

    def test_accepts_the_allowed_destination_in_any_casing(self) -> None:
        task = decode_task_json(
            task_document(scope={"action": "read", "destination": "Anthropic/Claude-Code"})
        )
        self.assertEqual(ALLOWED_DESTINATION, task.scope.destination)

    def test_rejects_retrieval_fields_at_the_boundary(self) -> None:
        # The retrieval snapshot and its metadata are internal state; task
        # JSON supplying them is rejected, never ignored.
        cases = (
            task_document(retrieved_context={"items": []}),
            task_document(retrieved_memory=[]),
            task_document(retrieval={"max_items": 50}),
            task_document(scope={"action": "read", "retrieved_context": {}}),
            task_document(context={"retrieved_context": {"items": []}}),
            task_document(context={"retrieved_memory": []}),
            task_document(preferences={"retrieved_context": {}}),
            task_document(preferences={"relevance_score": 5}),
        )
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(TaskInputError):
                    decode_task_json(raw)
        # A valid document always starts from an empty internal snapshot.
        self.assertEqual((), decode_task_json(task_document()).retrieved_context.items)

    def test_rejects_an_integer_above_the_digit_limit(self) -> None:
        limit = sys.get_int_max_str_digits()
        if limit == 0:  # pragma: no cover - only when the guard is disabled
            self.skipTest("The interpreter integer digit limit is disabled")
        digits = "9" * (limit + 10)
        raw = (
            '{"objective": "o", "capability": "echo", '
            '"scope": {"action": "read"}, '
            f'"context": {{"inputs": {digits}}}}}'
        )
        self.assertLess(len(raw.encode("utf-8")), MAX_TASK_JSON_BYTES)
        with self.assertRaises(TaskInputError):
            decode_task_json(raw)

    def test_rejects_excessively_nested_json(self) -> None:
        raw = (
            '{"objective": "o", "capability": "echo", '
            '"scope": {"action": "read"}, '
            '"context": {"inputs": ' + "[" * 20000 + "]" * 20000 + "}}"
        )
        self.assertLess(len(raw.encode("utf-8")), MAX_TASK_JSON_BYTES)
        with self.assertRaises(TaskInputError):
            decode_task_json(raw)

    def test_parser_failures_keep_their_specific_messages(self) -> None:
        # The generic parser normalization must not swallow the precise
        # boundary messages raised from the decode hooks.
        cases = (
            (
                '{"objective": "a", "objective": "b", "capability": "echo",'
                ' "scope": {"action": "read"}}',
                "Duplicate",
            ),
            (
                '{"objective": "o", "capability": "echo",'
                ' "scope": {"action": "read"}, "context": {"inputs": NaN}}',
                "Non-finite",
            ),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(TaskInputError) as caught:
                    decode_task_json(raw)
                self.assertIn(expected, str(caught.exception))

    def test_rejects_wrongly_typed_values(self) -> None:
        cases = (
            {"objective": 1, "capability": "echo", "scope": {"action": "read"}},
            {"objective": "o", "capability": "echo", "scope": "read"},
            {"objective": "o", "capability": "echo", "scope": {"action": ""}},
            {"objective": "", "capability": "echo", "scope": {"action": "read"}},
            {
                "objective": "o",
                "capability": "echo",
                "scope": {"action": "read"},
                "preferences": {"needs_clarification": "yes"},
            },
            {
                "objective": "o",
                "capability": "echo",
                "scope": {"action": "read", "data_classification": "top-secret"},
            },
        )
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(TaskInputError):
                    decode_task_json(json.dumps(document))


class PresentationRedactionTests(unittest.TestCase):
    """The envelope redaction policy is owned by ``jarvis.app``."""

    def envelope_message(self, text: str) -> str:
        payload = build_envelope(
            "run",
            PermissionMode.SESSION,
            error=StructuredError(ErrorCode.INVALID_TASK, text),
        )
        return payload["error"]["message"]

    def test_labelled_credentials_are_redacted(self) -> None:
        cases = (
            ("password=hunter2", "hunter2"),
            ("password: hunter2", "hunter2"),
            ("passwd = hunter2", "hunter2"),
            ("api_key: abc123", "abc123"),
            ("api-key=abc123", "abc123"),
            ("api key: abc123", "abc123"),
            ("API_KEY: abc123", "abc123"),
            ("Api-Key = abc123", "abc123"),
            ("authorization: Basic-secret-value", "Basic-secret-value"),
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("private_key=my-private-value", "my-private-value"),
            ("private key: my-private-value", "my-private-value"),
            ("cookie: session-value", "session-value"),
            ("secret=my-secret", "my-secret"),
            ("SECRET = my-secret", "my-secret"),
            ("token: opaque-token-value", "opaque-token-value"),
        )
        for text, credential in cases:
            with self.subTest(text=text):
                message = self.envelope_message(f"connector said {text} today")
                self.assertNotIn(credential, message)
                self.assertIn(REDACTED, message)

    def test_identifier_separated_credential_labels_are_redacted(self) -> None:
        # '_' and '-' are identifier separators, not word content: a
        # sensitive term exposed after them must still be recognized.
        cases = (
            "openai_api_key=HUNTER2",
            "client_secret: HUNTER2",
            "access_token=HUNTER2",
            "github_token: HUNTER2",
            "session_cookie=HUNTER2",
            "database_password=HUNTER2",
            "client-secret=HUNTER2",
            "github-token: HUNTER2",
            "OpenAI_API_Key=HUNTER2",
            "CLIENT_SECRET: HUNTER2",
        )
        for text in cases:
            with self.subTest(text=text):
                message = self.envelope_message(f"connector said {text} today")
                self.assertNotIn("HUNTER2", message)
                self.assertIn(REDACTED, message)

    def test_sensitive_terms_inside_ordinary_words_are_left_alone(self) -> None:
        # A sensitive term embedded in an ordinary alphanumeric word is not a
        # credential, even when assignment syntax follows the word.
        for text in (
            "mytoken=value",
            "passwordless=value",
            "secretary=value",
            "tokenizer=value",
            "open tokenizer.py and secret_handling.md",
            "Review token telemetry",
        ):
            with self.subTest(text=text):
                self.assertEqual(text, self.envelope_message(text))

    def test_unlabelled_credential_shapes_stay_redacted(self) -> None:
        cases = (
            ("leaked sk-abcdefghijklmnop here", "sk-abcdefghijklmnop"),
            ("Bearer abcdefghijklmnop rest", "abcdefghijklmnop"),
            (
                "jwt aaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc end",
                "aaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc",
            ),
        )
        for text, credential in cases:
            with self.subTest(text=text):
                message = self.envelope_message(text)
                self.assertNotIn(credential, message)
                self.assertIn(REDACTED, message)

    def test_ordinary_text_is_left_alone(self) -> None:
        message = self.envelope_message("Objective, capability and scope are required.")
        self.assertEqual("Objective, capability and scope are required.", message)

    def test_redaction_policy_is_not_borrowed_from_the_connector(self) -> None:
        # The connector's pattern names are not bound in this module at all.
        self.assertFalse(hasattr(app_module, "SECRET_KEY_PATTERN"))
        self.assertFalse(hasattr(app_module, "SECRET_VALUE_PATTERNS"))
        # The policy is defined locally instead.
        self.assertTrue(hasattr(app_module, "_SECRET_LABEL"))
        source = (Path(__file__).parents[1] / "jarvis" / "app.py").read_text(
            encoding="utf-8"
        )
        connector_imports = [
            line
            for line in source.splitlines()
            if "jarvis.connectors.claude_code" in line
        ]
        self.assertEqual(
            ["from jarvis.connectors.claude_code import ClaudeCodeConnector"],
            connector_imports,
        )


class AppCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.settings = Settings(memory_database=self.database)

    def build(
        self,
        connector: DummyConnector | None = None,
        mode: PermissionMode = PermissionMode.SESSION,
    ):
        connector = connector if connector is not None else DummyConnector()
        app = create_app(
            settings=replace(self.settings, permission_mode=mode),
            memory=self.memory,
            registry=ToolRegistry(),
            connectors=(connector,),
        )
        return app, connector

    def awaiting_approval(self, app):
        result = app.run(task_document())
        self.assertEqual(10, result.exit_code)
        approval = result.payload["approval"]
        return (
            result.payload["session"]["id"],
            result.payload["task"]["id"],
            approval["request_id"],
            approval["fingerprint"],
        )

    # -- run ---------------------------------------------------------------

    def test_run_creates_a_fresh_session_and_task(self) -> None:
        app, connector = self.build()
        result = app.run(task_document())
        self.assertEqual(0, result.exit_code)
        self.assertEqual(RunStatus.COMPLETED.value, result.payload["run_status"])
        self.assertEqual(1, connector.execute_calls)
        self.assertTrue(result.payload["session"]["id"])
        self.assertTrue(result.payload["task"]["id"])
        second = app.run(task_document())
        self.assertNotEqual(
            result.payload["session"]["id"], second.payload["session"]["id"]
        )
        self.assertNotEqual(result.payload["task"]["id"], second.payload["task"]["id"])

    def test_run_inside_an_existing_session(self) -> None:
        app, _ = self.build()
        session = app.core.start_session("existing")
        result = app.run(task_document(), session.id)
        self.assertEqual(0, result.exit_code)
        self.assertEqual(session.id, result.payload["session"]["id"])

    def test_envelope_session_carries_the_project_binding(self) -> None:
        from jarvis.contracts import ProjectRecord

        app, _ = self.build()
        unbound = app.run(task_document())
        self.assertIsNone(unbound.payload["session"]["project_id"])
        self.memory.save_project(ProjectRecord(id="proj-a", name="Project A"))
        bound = app.run(task_document(project_id="proj-a"))
        self.assertEqual(0, bound.exit_code)
        self.assertEqual("proj-a", bound.payload["session"]["project_id"])
        # An unknown project fails closed with the deterministic input error.
        refused = app.run(task_document(project_id="ghost-proj"))
        self.assertEqual(64, refused.exit_code)
        self.assertEqual("invalid_task", refused.payload["error"]["code"])

    def test_run_with_an_unknown_session_is_not_found(self) -> None:
        app, connector = self.build()
        result = app.run(task_document(), "no-such-session")
        self.assertEqual(66, result.exit_code)
        self.assertEqual("unknown_session", result.payload["error"]["code"])
        self.assertEqual(0, connector.execute_calls)

    def test_run_with_invalid_task_json_is_invalid_input(self) -> None:
        app, connector = self.build()
        result = app.run('{"objective": "only"}')
        self.assertEqual(64, result.exit_code)
        self.assertEqual("invalid_task", result.payload["error"]["code"])
        self.assertEqual(0, connector.execute_calls)

    def test_run_without_a_capable_connector_is_invalid_input(self) -> None:
        app, _ = self.build()
        result = app.run(task_document(capability="unsupported_capability"))
        self.assertEqual(64, result.exit_code)
        self.assertEqual("no_connector", result.payload["error"]["code"])

    # -- resume ------------------------------------------------------------

    def test_resume_without_a_grant_keeps_waiting(self) -> None:
        app, connector = self.build(mode=PermissionMode.STRICT)
        session_id, task_id, _, _ = self.awaiting_approval(app)
        result = app.resume(session_id, task_id)
        self.assertEqual(10, result.exit_code)
        self.assertEqual(
            RunStatus.AWAITING_APPROVAL.value, result.payload["run_status"]
        )
        self.assertEqual(0, connector.execute_calls)

    def test_resume_of_an_unknown_task_is_not_found(self) -> None:
        app, _ = self.build()
        session = app.core.start_session("empty")
        result = app.resume(session.id, "no-such-task")
        self.assertEqual(66, result.exit_code)

    def test_resume_of_a_completed_task_reports_completion(self) -> None:
        app, connector = self.build()
        first = app.run(task_document())
        result = app.resume(
            first.payload["session"]["id"], first.payload["task"]["id"]
        )
        self.assertEqual(0, result.exit_code)
        self.assertEqual(RunStatus.COMPLETED.value, result.payload["run_status"])
        self.assertEqual(1, connector.execute_calls)

    # -- clarify -----------------------------------------------------------

    def test_clarification_round_trip(self) -> None:
        app, connector = self.build()
        started = app.run(
            task_document(
                preferences={
                    "needs_clarification": True,
                    "clarification_question": "Which module?",
                }
            )
        )
        self.assertEqual(11, started.exit_code)
        self.assertEqual(
            RunStatus.AWAITING_CLARIFICATION.value, started.payload["run_status"]
        )
        self.assertEqual(0, connector.execute_calls)
        answered = app.clarify(
            started.payload["session"]["id"],
            started.payload["task"]["id"],
            "The core package only.",
        )
        self.assertEqual(0, answered.exit_code)
        self.assertEqual(RunStatus.COMPLETED.value, answered.payload["run_status"])
        self.assertEqual(1, connector.execute_calls)

    def test_blank_clarification_is_rejected_without_mutation(self) -> None:
        app, _ = self.build()
        started = app.run(task_document(preferences={"needs_clarification": True}))
        task_id = started.payload["task"]["id"]
        before = self.memory.load_task(task_id)
        result = app.clarify(started.payload["session"]["id"], task_id, "   ")
        self.assertEqual(64, result.exit_code)
        after = self.memory.load_task(task_id)
        self.assertEqual(before.status, after.status)
        self.assertEqual(before.preferences, after.preferences)

    def test_cross_session_clarification_is_rejected_without_mutation(self) -> None:
        app, _ = self.build()
        started = app.run(task_document(preferences={"needs_clarification": True}))
        task_id = started.payload["task"]["id"]
        other = app.core.start_session("other")
        before = self.memory.load_task(task_id)
        result = app.clarify(other.id, task_id, "answer from another session")
        self.assertEqual(66, result.exit_code)
        self.assertEqual("task_not_found", result.payload["error"]["code"])
        after = self.memory.load_task(task_id)
        self.assertEqual(before.status, after.status)
        self.assertEqual(before.stage, after.stage)
        self.assertEqual(before.preferences, after.preferences)
        self.assertIsNone(self.memory.load_session(other.id).active_task_id)

    def test_clarification_of_an_unknown_session_is_not_found(self) -> None:
        app, _ = self.build()
        started = app.run(task_document(preferences={"needs_clarification": True}))
        result = app.clarify(
            "no-such-session", started.payload["task"]["id"], "answer"
        )
        self.assertEqual(66, result.exit_code)
        self.assertEqual("unknown_session", result.payload["error"]["code"])

    # -- approve / reject --------------------------------------------------

    def test_approve_uses_the_exact_persisted_request(self) -> None:
        app, connector = self.build(
            DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        )
        session_id, task_id, request_id, fingerprint = self.awaiting_approval(app)
        persisted = self.memory.load_pending_approval(task_id, session_id)
        self.assertEqual(persisted.id, request_id)
        self.assertEqual(persisted.fingerprint, fingerprint)
        result = app.approve(session_id, task_id, request_id, fingerprint)
        self.assertEqual(0, result.exit_code)
        self.assertEqual(1, connector.execute_calls)

    def test_request_id_mismatch_never_executes(self) -> None:
        app, connector = self.build(
            DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        )
        session_id, task_id, _, fingerprint = self.awaiting_approval(app)
        result = app.approve(session_id, task_id, "not-the-request-id", fingerprint)
        self.assertEqual(77, result.exit_code)
        self.assertEqual("invalid_grant", result.payload["error"]["code"])
        self.assertEqual(0, connector.execute_calls)

    def test_fingerprint_mismatch_never_executes(self) -> None:
        app, connector = self.build(
            DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        )
        session_id, task_id, request_id, _ = self.awaiting_approval(app)
        result = app.approve(session_id, task_id, request_id, "0" * 64)
        self.assertEqual(77, result.exit_code)
        self.assertEqual(0, connector.execute_calls)

    def test_missing_pending_request_is_not_found(self) -> None:
        app, connector = self.build()
        completed = app.run(task_document())
        result = app.approve(
            completed.payload["session"]["id"],
            completed.payload["task"]["id"],
            "request-id",
            "0" * 64,
        )
        self.assertEqual(66, result.exit_code)
        self.assertEqual("task_not_found", result.payload["error"]["code"])
        self.assertEqual(1, connector.execute_calls)

    def test_expired_request_never_executes(self) -> None:
        app, connector = self.build(
            DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        )
        session_id, task_id, request_id, fingerprint = self.awaiting_approval(app)
        persisted = self.memory.load_pending_approval(task_id, session_id)
        self.memory.save_pending_approval(
            replace(persisted, expires_at=past_timestamp())
        )
        result = app.approve(session_id, task_id, request_id, fingerprint)
        self.assertEqual(77, result.exit_code)
        self.assertEqual("stale_grant", result.payload["error"]["code"])
        self.assertEqual(0, connector.execute_calls)

    def test_cross_session_approval_is_not_found(self) -> None:
        app, connector = self.build(
            DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        )
        _, task_id, request_id, fingerprint = self.awaiting_approval(app)
        other = app.core.start_session("other")
        result = app.approve(other.id, task_id, request_id, fingerprint)
        self.assertEqual(66, result.exit_code)
        self.assertEqual(0, connector.execute_calls)

    def test_approval_is_one_shot_by_default(self) -> None:
        app, _ = self.build(DummyConnector(risk=RiskLevel.MEDIUM))
        session_id, task_id, request_id, fingerprint = self.awaiting_approval(app)
        persisted = self.memory.load_pending_approval(task_id, session_id)
        result = app.approve(session_id, task_id, request_id, fingerprint)
        self.assertEqual(0, result.exit_code)
        self.assertFalse(
            self.memory.has_remembered_scope_grant(
                session_id, persisted.scope_fingerprint
            )
        )

    def test_eligible_session_grant_may_be_remembered(self) -> None:
        app, _ = self.build(DummyConnector(risk=RiskLevel.MEDIUM))
        session_id, task_id, request_id, fingerprint = self.awaiting_approval(app)
        persisted = self.memory.load_pending_approval(task_id, session_id)
        result = app.approve(
            session_id, task_id, request_id, fingerprint, remember_for_session=True
        )
        self.assertEqual(0, result.exit_code)
        self.assertTrue(
            self.memory.has_remembered_scope_grant(
                session_id, persisted.scope_fingerprint
            )
        )

    def test_remembrance_is_refused_outside_the_reuse_conditions(self) -> None:
        cases = (
            ("strict", DummyConnector(risk=RiskLevel.MEDIUM), PermissionMode.STRICT),
            (
                "smart_auto",
                DummyConnector(risk=RiskLevel.MEDIUM),
                PermissionMode.SMART_AUTO,
            ),
            (
                "mandatory",
                DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS),
                PermissionMode.SESSION,
            ),
            ("high_risk", DummyConnector(risk=RiskLevel.HIGH), PermissionMode.SESSION),
            (
                "irreversible",
                DummyConnector(side_effect=SideEffect.LOCAL_WRITE, reversible=False),
                PermissionMode.SESSION,
            ),
        )
        for label, connector, mode in cases:
            with self.subTest(case=label):
                self.setUp()
                app, connector = self.build(connector, mode)
                session_id, task_id, request_id, fingerprint = self.awaiting_approval(
                    app
                )
                persisted = self.memory.load_pending_approval(task_id, session_id)
                result = app.approve(
                    session_id,
                    task_id,
                    request_id,
                    fingerprint,
                    remember_for_session=True,
                )
                self.assertEqual(77, result.exit_code)
                self.assertEqual(0, connector.execute_calls)
                # No grant was created and the request is still pending.
                self.assertFalse(
                    self.memory.has_remembered_scope_grant(
                        session_id, persisted.scope_fingerprint
                    )
                )
                self.assertIsNotNone(
                    self.memory.load_pending_approval(task_id, session_id)
                )

    def test_reject_never_executes(self) -> None:
        app, connector = self.build(
            DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        )
        session_id, task_id, request_id, fingerprint = self.awaiting_approval(app)
        result = app.reject(session_id, task_id, request_id, fingerprint)
        self.assertEqual(20, result.exit_code)
        self.assertEqual(RunStatus.REJECTED.value, result.payload["run_status"])
        self.assertEqual(0, connector.execute_calls)

    def test_reject_validates_the_request_like_approve(self) -> None:
        app, connector = self.build(
            DummyConnector(side_effect=SideEffect.EXTERNAL_EGRESS)
        )
        session_id, task_id, request_id, _ = self.awaiting_approval(app)
        result = app.reject(session_id, task_id, request_id, "0" * 64)
        self.assertEqual(77, result.exit_code)
        self.assertEqual(0, connector.execute_calls)

    # -- cancel ------------------------------------------------------------

    def test_cancel_marks_the_task_cancelled(self) -> None:
        app, connector = self.build(mode=PermissionMode.STRICT)
        session_id, task_id, _, _ = self.awaiting_approval(app)
        result = app.cancel(session_id, task_id)
        self.assertEqual(21, result.exit_code)
        self.assertEqual(RunStatus.CANCELLED.value, result.payload["run_status"])
        self.assertEqual(0, connector.execute_calls)

    def test_cancel_of_an_unknown_task_is_not_found(self) -> None:
        app, _ = self.build()
        session = app.core.start_session("empty")
        result = app.cancel(session.id, "no-such-task")
        self.assertEqual(66, result.exit_code)


class CompositionRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.settings = Settings(memory_database=self.database)

    def test_create_app_uses_only_the_injected_parts(self) -> None:
        memory = SQLiteMemoryStore(self.database)
        registry = ToolRegistry()
        connector = DummyConnector()
        app = create_app(
            settings=self.settings,
            memory=memory,
            registry=registry,
            connectors=(connector,),
        )
        self.assertIs(memory, app.memory)
        self.assertIs(memory, app.core.memory)
        self.assertIs(registry, app.core.registry)
        self.assertEqual(("dummy",), registry.list_names())
        self.assertIs(connector, registry.get("dummy"))
        self.assertIs(self.settings.permission_mode, app.permission_mode)

    def test_default_app_registers_claude_without_touching_any_process(self) -> None:
        # Any auth probe, subprocess or model call during construction fails.
        with mock.patch.object(
            claude_code.subprocess, "run", side_effect=AssertionError("no subprocess")
        ), mock.patch.object(
            claude_code.subprocess, "Popen", side_effect=AssertionError("no subprocess")
        ), mock.patch.object(
            claude_code.shutil, "which", side_effect=AssertionError("no probe")
        ):
            app = build_default_app(self.settings)

        connector = app.core.registry.get("claude-code-plan")
        startup_directory = Path.cwd().resolve()
        self.assertEqual(
            ("claude-code-plan", "local-file-read"),
            app.core.registry.list_names(),
        )
        self.assertEqual((startup_directory,), connector.allowed_roots)
        self.assertEqual(startup_directory, connector.default_working_directory)
        self.assertEqual(
            self.settings.claude_call_budget_usd, connector.max_budget_usd
        )
        self.assertIs(self.settings, app.settings)

    def test_default_app_binds_the_local_reader_to_the_same_startup_root(self) -> None:
        # M3: the local reader and the Claude connector share exactly one
        # boundary, and constructing it touches no filesystem and no process.
        with mock.patch.object(
            claude_code.subprocess, "run", side_effect=AssertionError("no subprocess")
        ), mock.patch.object(
            claude_code.shutil, "which", side_effect=AssertionError("no probe")
        ):
            app = build_default_app(self.settings)

        reader = app.core.registry.get("local-file-read")
        startup_directory = Path.cwd().resolve()
        self.assertEqual((startup_directory,), reader.allowed_roots)
        self.assertEqual(startup_directory, reader.default_working_directory)
        # The billing route is declared, not inherited: the manifest default
        # is UNKNOWN and would be refused before execution.
        self.assertEqual(
            BillingClass.NO_ADDITIONAL_CHARGE, reader.manifest.default_billing_class
        )
        self.assertFalse(reader.manifest.requires_paid_api)
        # No external tool grant and no canonical outbound override: a local
        # read is not an egress path at all.
        self.assertEqual((), reader.external_tool_grants(mock.Mock()))


class ExitCodeMappingTests(unittest.TestCase):
    """Structured error codes map to stable, documented CLI exit codes."""

    def test_billing_integrity_failure_maps_to_the_policy_exit_code(self) -> None:
        self.assertEqual(77, app_module.EXIT_POLICY)
        self.assertEqual(
            app_module.EXIT_POLICY, app_module._ERROR_EXIT[ErrorCode.BILLING_INTEGRITY_FAILED]
        )
        run = AgentRun(
            RunStatus.FAILED,
            None,
            None,
            error=StructuredError(
                ErrorCode.BILLING_INTEGRITY_FAILED, "monetary integrity failed"
            ),
        )
        self.assertEqual(77, app_module.JarvisApp._exit_code(run))

    def test_every_error_code_has_an_exit_code(self) -> None:
        missing = [
            code.value for code in ErrorCode if code not in app_module._ERROR_EXIT
        ]
        self.assertEqual([], missing)


class BillingEnvelopeTests(unittest.TestCase):
    """A malformed monetary amount degrades to null instead of breaking JSON."""

    def test_non_finite_billing_amounts_render_as_null(self) -> None:
        estimate = CostEstimate(
            billing_class=BillingClass.METERED_MONETARY,
            maximum_usd=float("nan"),
            resource_ceiling_usd=float("inf"),
        )
        payload = app_module._billing_payload(estimate)
        self.assertIsNone(payload["monetary_maximum_usd"])
        self.assertIsNone(payload["resource_ceiling_usd"])
        # ``allow_nan=False`` renders it without raising.
        self.assertIn(
            "null", app_module.CommandResult(payload, app_module.EXIT_OK).render()
        )

    def test_valid_billing_amounts_stay_plain_floats(self) -> None:
        estimate = CostEstimate(
            billing_class=BillingClass.METERED_MONETARY,
            maximum_usd=0.25,
            resource_ceiling_usd=5.0,
        )
        payload = app_module._billing_payload(estimate)
        self.assertEqual(0.25, payload["monetary_maximum_usd"])
        self.assertEqual(5.0, payload["resource_ceiling_usd"])


if __name__ == "__main__":
    unittest.main()
