from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enum import Enum

from jarvis.app import build_envelope, create_app
from jarvis.connectors import claude_code
from jarvis.connectors.claude_code import (
    EXTERNAL_PREFERENCE_KEYS,
    PROHIBITED_BILLING_ENV_VARS,
    ClaudeCodeConnector,
    provider_safe_text,
)
from jarvis.contracts import (
    DIAGNOSTIC_DETAIL_MAX_CHARS,
    MODEL_ANSWER_RESULT_KEY,
    ActionScope,
    AttemptState,
    Availability,
    BillingClass,
    DataClassification,
    ErrorCode,
    LocalReadSnapshot,
    MonetaryState,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    RunStatus,
    SessionState,
    Task,
    TaskStatus,
    retrieval_eligible,
)
from jarvis.settings import Settings
from tests.helpers import safe_cleanup

#: Non-secret shape of ``claude auth status --json`` for a Max/OAuth session.
OAUTH_MAX_AUTH_STATUS = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "subscriptionType": "max",
}


#: Internal runtime/billing/approval markers AgentCore writes into
#: ``Task.preferences`` after the outbound payload has already been approved.
INTERNAL_PREFERENCES = {
    "billing_preflight_fingerprint": "subscription_included|0.000000000|0|0.100000000",
    "billing_repreflight_attempts": 2,
    "paid_api_task_approval": "internal-approval-id",
    "cost_cap_override": "internal-override-id",
    "unauthorized_runtime_marker": "must-not-egress",
}


def canonical(payload) -> str:
    """Deterministic encoding used only for comparing outbound payloads."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def patch_oauth_environment():
    """Deterministic Claude Max/OAuth environment without metered credentials."""
    clean = {
        key: value
        for key, value in os.environ.items()
        if key not in PROHIBITED_BILLING_ENV_VARS
    }
    return patch.dict(os.environ, clean, clear=True)


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
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            max_budget_usd=0.15,
        )
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)
        # The auth-status probe is local and non-model; it is mocked so the
        # suite never depends on the developer's live session.
        auth = patch.object(
            ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS
        )
        auth.start()
        self.addCleanup(auth.stop)

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

    def test_approval_scope_destination_matches_the_outbound_preview(self) -> None:
        task = self.task()
        request = self.connector.permission_request(task, "session-1")
        self.assertEqual(
            "anthropic/claude-code", request.outbound_preview.destination
        )
        # The user approves exactly the destination they are shown.
        self.assertEqual(
            request.outbound_preview.destination, request.scope.destination
        )
        self.assertEqual("anthropic/claude-code", request.scope.destination)
        self.assertEqual("claude-code-plan", request.scope.connector)

    def test_only_allowlisted_preferences_reach_the_outbound_payload(self) -> None:
        task = self.task()
        task.visible_assumption = "Use option B"
        task.preferences = {
            "preferred_connector": "claude-code-plan",
            **INTERNAL_PREFERENCES,
        }
        payload, _ = self.connector._sanitized_payload(task)
        prompt, _ = self.connector._build_prompt(task)
        preview = self.connector.outbound_preview(task)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)

        self.assertEqual({"preferred_connector": "claude-code-plan"}, payload["preferences"])
        for key in INTERNAL_PREFERENCES:
            with self.subTest(preference=key):
                self.assertNotIn(key, payload["preferences"])
                self.assertNotIn(key, serialized)
                self.assertNotIn(key, prompt)
                self.assertNotIn(f"preferences.{key}", preview.field_names)
        self.assertNotIn("must-not-egress", prompt)
        self.assertIn("preferences.preferred_connector", preview.field_names)

    def test_visible_assumption_is_an_explicit_external_field(self) -> None:
        task = self.task()
        task.visible_assumption = "Use option B"
        task.preferences = {
            "preferred_connector": "claude-code-plan",
            "clarification": "Use option B",
            "assumption": "internal assumption copy",
            "needs_clarification": True,
            "clarification_question": "Which option?",
            **INTERNAL_PREFERENCES,
        }
        payload, _ = self.connector._sanitized_payload(task)
        prompt, _ = self.connector._build_prompt(task)
        preview = self.connector.outbound_preview(task)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)

        self.assertEqual("Use option B", payload["visible_assumption"])
        self.assertIn('"visible_assumption": "Use option B"', prompt)
        self.assertIn("visible_assumption", preview.field_names)
        self.assertGreater(preview.approximate_bytes, len("Use option B"))
        # The outbound preference object is unchanged by the new field.
        self.assertEqual(
            {"preferred_connector": "claude-code-plan"}, payload["preferences"]
        )
        # The raw keys the value is derived from never leave the machine.
        for key in (
            "clarification",
            "assumption",
            "needs_clarification",
            "clarification_question",
            *INTERNAL_PREFERENCES,
        ):
            with self.subTest(preference=key):
                self.assertNotIn(key, payload["preferences"])
                self.assertNotIn(f'"{key}"', serialized)
                self.assertNotIn(f'"{key}"', prompt)
                self.assertNotIn(f"preferences.{key}", preview.field_names)
        self.assertNotIn("internal assumption copy", prompt)
        self.assertNotIn("Which option?", prompt)

    def test_visible_assumption_is_redacted(self) -> None:
        task = self.task()
        task.visible_assumption = "Reuse token sk-super-secret-123456789 as agreed"
        _, baseline_count = self.connector._sanitized_payload(self.task())
        payload, count = self.connector._sanitized_payload(task)
        self.assertNotIn("super-secret", json.dumps(payload, default=str))
        self.assertIn("[REDACTED]", payload["visible_assumption"])
        self.assertEqual(baseline_count + 1, count)

    def test_secret_classification_hides_the_visible_assumption(self) -> None:
        task = self.task()
        task.scope = ActionScope(
            action="analyze",
            target=str(self.root),
            destination="anthropic/claude-code",
            data_classification=DataClassification.SECRET,
        )
        task.visible_assumption = "classified clarification text"
        payload, count = self.connector._sanitized_payload(task)
        self.assertNotIn(
            "classified clarification text", json.dumps(payload, default=str)
        )
        self.assertEqual(
            "[REDACTED_SECRET_CLASSIFICATION]", payload["visible_assumption"]
        )
        self.assertGreaterEqual(count, 7)

    def test_approved_payload_survives_post_approval_internal_writes(self) -> None:
        task = self.task({"repository": "jarvis"})
        task.visible_assumption = "Use option B and inspect only the fixture."
        task.preferences = {"preferred_connector": "claude-code-plan"}

        approved_payload, approved_redactions = self.connector._sanitized_payload(task)
        approved_prompt, _ = self.connector._build_prompt(task)
        approved_preview = self.connector.outbound_preview(task)

        # Exactly what AgentCore records after the user approves.
        task.preferences.update(INTERNAL_PREFERENCES)

        execute_payload, execute_redactions = self.connector._sanitized_payload(task)
        execute_prompt, _ = self.connector._build_prompt(task)
        execute_preview = self.connector.outbound_preview(task)

        self.assertEqual(
            sha256(canonical(approved_payload)), sha256(canonical(execute_payload))
        )
        self.assertEqual(sha256(approved_prompt), sha256(execute_prompt))
        self.assertEqual(approved_redactions, execute_redactions)
        self.assertEqual(
            approved_preview.approximate_bytes, execute_preview.approximate_bytes
        )
        self.assertEqual(
            approved_preview.redacted_items, execute_preview.redacted_items
        )
        self.assertEqual(approved_preview.field_names, execute_preview.field_names)
        self.assertEqual(
            approved_preview.destination, execute_preview.destination
        )
        self.assertEqual(approved_payload["scope"], execute_payload["scope"])
        self.assertEqual(
            "anthropic/claude-code", execute_payload["scope"]["destination"]
        )
        self.assertEqual(
            "Use option B and inspect only the fixture.",
            execute_payload["visible_assumption"],
        )

    def test_canonical_payload_survives_a_persistence_round_trip(self) -> None:
        task = self.task({"working_directory": str(self.root), "repository": "jarvis"})
        task.visible_assumption = "Use option B"
        task.preferences = {"preferred_connector": "claude-code-plan"}
        before_prompt, _ = self.connector._build_prompt(task)

        # Storage serializes with sorted keys, so a reloaded task has a
        # different mapping order than the in-memory task that was approved.
        stored = json.dumps(task.to_dict(), sort_keys=True, default=str)
        task.context["retrieved_decision_count"] = 0
        reloaded = Task.from_dict(json.loads(stored))
        reloaded.preferences.update(INTERNAL_PREFERENCES)
        after_prompt, _ = self.connector._build_prompt(reloaded)

        self.assertEqual(sha256(before_prompt), sha256(after_prompt))
        self.assertEqual(before_prompt, after_prompt)

    def test_allowlisted_preferences_are_still_deeply_redacted(self) -> None:
        task = self.task()
        task.preferences = {"preferred_connector": "sk-super-secret-123456789"}
        payload, count = self.connector._sanitized_payload(task)
        self.assertEqual("[REDACTED]", payload["preferences"]["preferred_connector"])
        self.assertGreaterEqual(count, 1)

        # Recursion still applies to any structured value that is allowlisted.
        task.preferences = {
            "external_profile": {"nested": {"api_key": "sk-super-secret-123456789"}},
            **INTERNAL_PREFERENCES,
        }
        with patch.object(
            claude_code,
            "EXTERNAL_PREFERENCE_KEYS",
            ("preferred_connector", "external_profile"),
        ):
            nested_payload, nested_count = self.connector._sanitized_payload(task)
        serialized = json.dumps(nested_payload, ensure_ascii=False, default=str)
        self.assertNotIn("super-secret", serialized)
        self.assertGreaterEqual(nested_count, 1)
        for key in INTERNAL_PREFERENCES:
            self.assertNotIn(key, serialized)

    def test_external_preference_allowlist_is_minimal(self) -> None:
        self.assertEqual(("preferred_connector",), EXTERNAL_PREFERENCE_KEYS)

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

    def test_broad_file_reading_tools_are_not_granted(self) -> None:
        # P1-A: the Read/Glob/Grep tools would let the external model pull
        # file contents outside the attested prompt, so this path grants none.
        command = self.connector._command("safe prompt")
        tools_value = command[command.index("--tools") + 1]
        self.assertEqual("", tools_value)
        for tool in ("Read", "Glob", "Grep"):
            self.assertNotIn(tool, tools_value)
        serialized = json.dumps(command)
        self.assertNotIn("Read,Glob,Grep", serialized)

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
        self.assertEqual(OutcomeStatus.SUCCESS, result.outcome)
        # M6: verification reads the DURABLE ``task.model_answer``, which
        # AgentCore lifts off the result before ``_sanitize_result`` runs.
        # Without that lift the verifier fails closed - it will not vouch for
        # an answer that was never bound. This mirrors the M5 write verifier,
        # which depends on ``_bind_local_write`` in exactly the same way.
        unbound = self.connector.verify(task, result, SessionState())
        self.assertFalse(unbound.passed)
        self.assertEqual("model_answer_integrity", unbound.method)
        task.model_answer = result.data[MODEL_ANSWER_RESULT_KEY]
        verification = self.connector.verify(task, result, SessionState())
        self.assertTrue(verification.passed)
        self.assertEqual("model_answer_integrity", verification.method)
        self.assertIn("sha256=" + task.model_answer.sha256, verification.evidence)
        invoked = popen.call_args.args[0]
        self.assertIn("--safe-mode", invoked)

    # -- M6 / P2-A: the REAL result path, not a test double -----------------

    def answer_for(self, result_value):
        """Drive the production execute() and return its typed answer.

        Deliberately the real connector: the defect this pins lived in the
        connector's own reading of the provider payload, so a DummyConnector
        that calls ``build_model_answer`` itself would have proved nothing -
        and did not.
        """
        with patch(
            "jarvis.connectors.claude_code.subprocess.Popen"
        ) as popen, patch.object(
            ClaudeCodeConnector,
            "billing_route",
            lambda self: (BillingClass.SUBSCRIPTION_INCLUDED, "test_route"),
        ):
            popen.return_value = FakeProcess(
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": result_value,
                        "total_cost_usd": 0,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "modelUsage": {"claude-test": {}},
                    }
                )
            )
            result = self.connector.execute(self.task(), SessionState())
        self.assertEqual(OutcomeStatus.SUCCESS, result.outcome)
        return result, result.data[MODEL_ANSWER_RESULT_KEY]

    def test_the_real_path_preserves_the_provider_bytes_exactly(self) -> None:
        """Whitespace is DATA in a deliverable, not formatting noise.

        The connector reads the provider result twice on purpose: coerced and
        stripped for the diagnostic summary, raw for the typed answer. Before
        that separation existed, the summary's reading was the only one, so
        every real answer silently lost its leading indentation, its trailing
        whitespace and its trailing newline.
        """
        provider = "  leading\nbody\ntrailing  \n"
        _, answer = self.answer_for(provider)
        self.assertEqual(provider, answer.content)
        self.assertEqual(len(provider), answer.character_count)
        self.assertTrue(answer.content.startswith("  "))
        self.assertTrue(answer.content.endswith("trailing  \n"))

    def test_the_real_path_normalizes_only_line_endings(self) -> None:
        _, answer = self.answer_for("a\r\nb\r\n")
        self.assertEqual("a\nb\n", answer.content)
        _, lone_cr = self.answer_for("a\rb\r")
        self.assertEqual("a\nb\n", lone_cr.content)

    def test_the_real_path_refuses_a_non_string_provider_result(self) -> None:
        """A protocol violation must not be dressed up as a model answer.

        ``str()`` on the provider value would render ``{'k': 'v'}`` to the
        user as something the model wrote, with a valid digest and a passing
        verification. The typed contract always rejected non-strings; the
        producer path now lets it.
        """
        for value in (12345, 3.5, True, ["a", "b"], {"k": "v"}, None, 0, False, []):
            with self.subTest(value=repr(value)):
                _, answer = self.answer_for(value)
                self.assertFalse(answer.present, f"{value!r} became an answer")
                self.assertEqual("", answer.content)

    def test_the_real_path_leaves_the_diagnostic_summary_untouched(self) -> None:
        """H4 semantics are unchanged by the P2-A separation."""
        result, _ = self.answer_for("  leading\nbody\ntrailing  \n")
        # Still the coerced, stripped, single-line diagnostic reading.
        self.assertEqual("leading\nbody\ntrailing", result.summary)
        structured, _ = self.answer_for({"k": "v"})
        self.assertEqual("{'k': 'v'}", structured.summary)
        empty, _ = self.answer_for("")
        self.assertEqual("Claude Code completed the request.", empty.summary)

    # -- M7 rider B: a provider result that is not valid Unicode ------------

    def _surrogate_payload(self, *, is_error: bool) -> str:
        """Provider JSON whose ``result`` carries a lone surrogate.

        ``json.dumps`` escapes it as ``\\ud800``, exactly as a JavaScript
        runtime serializing a broken string would, and ``json.loads`` on the
        CLI side hands back a Python ``str`` that cannot be UTF-8 encoded.
        """
        return json.dumps(
            {
                "type": "result",
                "subtype": "error" if is_error else "success",
                "is_error": is_error,
                "result": "x\ud800y",
                "api_error_status": 500 if is_error else None,
                "total_cost_usd": 0,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "modelUsage": {"claude-test": {}},
            }
        )

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_a_lone_surrogate_provider_result_is_a_clean_connector_failure(
        self, popen
    ) -> None:
        """Both provider shapes: the success path and the pre-existing
        ``is_error`` path, whose summary used to carry the raw text."""
        for is_error in (False, True):
            with self.subTest(is_error=is_error):
                popen.return_value = FakeProcess(
                    self._surrogate_payload(is_error=is_error),
                    returncode=1 if is_error else 0,
                )
                result = self.connector.execute(self.task(), SessionState())
                self.assertEqual(OutcomeStatus.ERROR, result.outcome)
                self.assertEqual(ErrorCode.CONNECTOR_FAILED, result.error.code)
                self.assertEqual(
                    "Claude Code returned a response that is not valid Unicode text.",
                    result.error.message,
                )
                self.assertEqual(result.error.message, result.summary)
                self.assertTrue(result.error.retryable)
                self.assertNotIn(MODEL_ANSWER_RESULT_KEY, result.data)
                for text in (
                    result.summary,
                    result.error.message,
                    json.dumps(result.error.details, ensure_ascii=False),
                    json.dumps(result.data, ensure_ascii=False, default=str),
                ):
                    self.assertNotIn("\ud800", text)
                self.assertEqual(1, result.usage.call_count)
                self.assertEqual(BillingClass.SUBSCRIPTION_INCLUDED, result.usage.billing_class)
                self.assertEqual(MonetaryState.NOT_APPLICABLE, result.usage.monetary_state)

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_a_lone_surrogate_provider_result_finalizes_cleanly_through_the_core(
        self, popen
    ) -> None:
        """The task persists and finalizes FAILED; it is not left in doubt.

        Without the guard the success shape raises inside ``execute`` and H2
        marks the attempt UNKNOWN_OUTCOME; the ``is_error`` shape returns a
        summary SQLite cannot bind, so the execution conclusion raises and
        the attempt stays IN_FLIGHT. Neither is a clean failure.
        """
        for is_error in (False, True):
            with self.subTest(is_error=is_error):
                popen.return_value = FakeProcess(
                    self._surrogate_payload(is_error=is_error),
                    returncode=1 if is_error else 0,
                )
                app = create_app(
                    settings=Settings(memory_database=self.root / f"surrogate-{int(is_error)}.db"),
                    connectors=(self.connector,),
                )
                task = self.task()
                with patch.object(
                    ClaudeCodeConnector, "availability", return_value=Availability(True)
                ):
                    run = app.core.run(task)
                    self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
                    decided = app.core.resume(
                        run.session.id,
                        task.id,
                        grant=PermissionGrant.for_request(run.permission_request),
                    )
                self.assertEqual(RunStatus.FAILED, decided.status)
                self.assertEqual(ErrorCode.CONNECTOR_FAILED, decided.error.code)
                attempt = app.memory.latest_execution_attempt(task.id)
                self.assertIs(AttemptState.FINISHED, attempt.state)
                stored = app.memory.load_task(task.id)
                self.assertIs(TaskStatus.FAILED, stored.status)
                self.assertFalse(stored.model_answer.present)
                self.assertNotIn("\ud800", json.dumps(stored.to_dict(), ensure_ascii=False))

    # -- M7 P2: provider METADATA that is not valid Unicode -----------------
    #
    # Rider B guards ``result``. Independent review showed that the other
    # provider-controlled strings - ``subtype``/``stop_reason``/``type`` on
    # the ``is_error`` path (persisted through ``StructuredError.details``)
    # and the first ``modelUsage`` key (persisted as ``UsageRecord.model``) -
    # could still carry a lone surrogate into a SQLite write, so the usage or
    # conclusion write raised and the attempt was stranded IN_FLIGHT.

    #: Wrapped around every malformed provider value so a persisted surface
    #: can be searched for the WHOLE value: it must be dropped, not repaired
    #: around the surrogate.
    META_MARKER = "PROVIDER-META-7e3a"

    def _provider_payload(self, *, is_error: bool, **fields) -> str:
        payload = {
            "type": "result",
            "subtype": "error" if is_error else "success",
            "is_error": is_error,
            "result": "Request failed upstream." if is_error else "Plan ready",
            "api_error_status": 500 if is_error else None,
            "total_cost_usd": 0,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {"claude-test": {}},
        }
        payload.update(fields)
        return json.dumps(payload)

    def _through_the_core(self, popen, payload: str, *, is_error: bool, name: str):
        """Real AgentCore + SQLiteMemoryStore; returns (app, task, run, db)."""
        popen.return_value = FakeProcess(payload, returncode=1 if is_error else 0)
        database = self.root / f"{name}.db"
        app = create_app(
            settings=Settings(memory_database=database), connectors=(self.connector,)
        )
        task = self.task()
        with patch.object(
            ClaudeCodeConnector, "availability", return_value=Availability(True)
        ):
            run = app.core.run(task)
            self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
            decided = app.core.resume(
                run.session.id,
                task.id,
                grant=PermissionGrant.for_request(run.permission_request),
            )
        return app, task, decided, database

    @staticmethod
    def _every_persisted_string(database: Path, run) -> str:
        """Every text cell of every table, plus the public envelope."""
        parts = [
            json.dumps(
                build_envelope("resume", PermissionMode.SESSION, run), ensure_ascii=False
            )
        ]
        connection = sqlite3.connect(database)
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ]
            for table in tables:
                for row in connection.execute(f'SELECT * FROM "{table}"'):
                    parts.extend(cell for cell in row if isinstance(cell, str))
        finally:
            connection.close()
        return "\n".join(parts)

    def _assert_finished_and_hygienic(self, app, task, run, database) -> None:
        self.assertIs(
            AttemptState.FINISHED, app.memory.latest_execution_attempt(task.id).state
        )
        persisted = self._every_persisted_string(database, run)
        self.assertNotIn(self.META_MARKER, persisted)
        self.assertNotIn("\ud800", persisted)
        # The in-memory run is what the CLI renders from.
        if run.result is not None:
            self.assertNotIn(
                "\ud800", json.dumps(run.result.usage.to_dict(), ensure_ascii=False)
            )
        if run.error is not None:
            self.assertNotIn("\ud800", json.dumps(run.error.to_dict(), ensure_ascii=False))

    def _assert_malformed_metadata_field_is_dropped(self, popen, field: str) -> None:
        app, task, run, database = self._through_the_core(
            popen,
            self._provider_payload(is_error=True, **{field: f"{self.META_MARKER}\ud800"}),
            is_error=True,
            name=f"meta-{field}",
        )
        # A normal, understood connector failure - never UNKNOWN_OUTCOME and
        # never a stranded IN_FLIGHT attempt - with the bad field absent and
        # every other detail intact.
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.CONNECTOR_FAILED, run.error.code)
        self.assertEqual("Request failed upstream.", run.error.message)
        details = run.error.details
        if field == "type":
            # H4 drops ``response_type`` structurally on every path
            # (``response`` is a forbidden detail key); the gate is uniformity.
            self.assertNotIn("response_type", details)
        else:
            self.assertIn(field, details)
            self.assertIsNone(details[field])
        self.assertEqual(500, details["api_error_status"])
        self.assertIs(TaskStatus.FAILED, app.memory.load_task(task.id).status)
        self._assert_finished_and_hygienic(app, task, run, database)

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_a_lone_surrogate_subtype_is_a_normal_connector_failure(self, popen) -> None:
        self._assert_malformed_metadata_field_is_dropped(popen, "subtype")

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_a_lone_surrogate_stop_reason_is_a_normal_connector_failure(
        self, popen
    ) -> None:
        self._assert_malformed_metadata_field_is_dropped(popen, "stop_reason")

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_a_lone_surrogate_response_type_is_a_normal_connector_failure(
        self, popen
    ) -> None:
        self._assert_malformed_metadata_field_is_dropped(popen, "type")

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_a_lone_surrogate_model_name_on_success_completes_unattributed(
        self, popen
    ) -> None:
        """The answer, the ledger row and every billing field survive; only
        the model NAME is absent - the same row an omitted ``modelUsage``
        already produces, so nothing paid can be undercounted."""
        app, task, run, database = self._through_the_core(
            popen,
            self._provider_payload(
                is_error=False, modelUsage={f"{self.META_MARKER}\ud800": {}}
            ),
            is_error=False,
            name="model-success",
        )
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        stored = app.memory.load_task(task.id)
        self.assertIs(TaskStatus.COMPLETED, stored.status)
        self.assertEqual("Plan ready", stored.model_answer.content)
        (usage,) = app.memory.usage_records()
        self.assertIsNone(usage.model)
        self.assertEqual(
            (1, 1, 1), (usage.input_tokens, usage.output_tokens, usage.call_count)
        )
        self.assertEqual(BillingClass.SUBSCRIPTION_INCLUDED, usage.billing_class)
        self.assertEqual(MonetaryState.NOT_APPLICABLE, usage.monetary_state)
        self._assert_finished_and_hygienic(app, task, run, database)

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_a_lone_surrogate_model_name_on_error_is_a_normal_connector_failure(
        self, popen
    ) -> None:
        app, task, run, database = self._through_the_core(
            popen,
            self._provider_payload(
                is_error=True, modelUsage={f"{self.META_MARKER}\ud800": {}}
            ),
            is_error=True,
            name="model-error",
        )
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.CONNECTOR_FAILED, run.error.code)
        self.assertIs(TaskStatus.FAILED, app.memory.load_task(task.id).status)
        (usage,) = app.memory.usage_records()
        self.assertIsNone(usage.model)
        self.assertEqual(1, usage.call_count)
        self._assert_finished_and_hygienic(app, task, run, database)

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_valid_non_ascii_provider_metadata_is_persisted_exactly(self, popen) -> None:
        """Control: the gate is an encodability rule, not an ASCII rule."""
        subtype = "yeniden_dene\u2014a\u011f\u2192zaman_a\u015f\u0131m\u0131\u2713"
        stop_reason = "durduruldu\u2014\u2713"
        model = "claude-\u00fcn\u00efcode-4\u2192"
        for is_error in (False, True):
            with self.subTest(is_error=is_error):
                app, task, run, database = self._through_the_core(
                    popen,
                    self._provider_payload(
                        is_error=is_error,
                        subtype=subtype,
                        stop_reason=stop_reason,
                        modelUsage={model: {}},
                    ),
                    is_error=is_error,
                    name=f"unicode-{int(is_error)}",
                )
                self.assertIs(
                    AttemptState.FINISHED,
                    app.memory.latest_execution_attempt(task.id).state,
                )
                (usage,) = app.memory.usage_records()
                self.assertEqual(model, usage.model)
                persisted = self._every_persisted_string(database, run)
                self.assertIn(model, persisted)
                if is_error:
                    self.assertEqual(RunStatus.FAILED, run.status)
                    self.assertEqual(subtype, run.error.details["subtype"])
                    self.assertEqual(stop_reason, run.error.details["stop_reason"])
                    self.assertIn(subtype, persisted)
                    self.assertIn(stop_reason, persisted)
                else:
                    self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))

    def test_provider_safe_text_is_the_one_exact_rule(self) -> None:
        class Subclassed(str):
            pass

        keep = "erreur\u2014r\u00e9seau\u2192\u2713"
        self.assertIs(keep, provider_safe_text(keep))
        self.assertEqual("", provider_safe_text(""))
        at_cap = "m" * DIAGNOSTIC_DETAIL_MAX_CHARS
        self.assertIs(at_cap, provider_safe_text(at_cap))
        for rejected in (
            "x\ud800y",
            "\udfff",
            at_cap + "m",
            Subclassed("subclass"),
            None,
            5,
            0,
            True,
            2.5,
            ["end_turn"],
            {"a": "b"},
            b"bytes",
        ):
            with self.subTest(value=repr(rejected)):
                self.assertIsNone(provider_safe_text(rejected))

    @patch("jarvis.connectors.claude_code.subprocess.Popen")
    def test_every_optional_provider_string_is_gated_at_the_connector(
        self, popen
    ) -> None:
        """Connector-level: all five metadata fields malformed at once."""
        malformed = f"{self.META_MARKER}\ud800"
        popen.return_value = FakeProcess(
            self._provider_payload(
                is_error=True,
                type=malformed,
                subtype=malformed,
                stop_reason=malformed,
                api_error_status=malformed,
                modelUsage={malformed: {}},
            ),
            returncode=1,
        )
        result = self.connector.execute(self.task(), SessionState())
        self.assertEqual(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual(ErrorCode.CONNECTOR_FAILED, result.error.code)
        self.assertFalse(result.error.retryable)
        self.assertIsNone(result.usage.model)
        # ``data`` is the ungated connector view of the metadata: every gated
        # field is already absent-as-None there, including ``response_type``,
        # which H4 then drops from ``details`` structurally.
        for key in ("response_type", "subtype", "stop_reason", "api_error_status"):
            self.assertIn(key, result.data)
            self.assertIsNone(result.data[key])
        for key in ("subtype", "stop_reason", "api_error_status"):
            self.assertIsNone(result.error.details[key])
        self.assertNotIn("response_type", result.error.details)
        self.assertEqual(0, result.error.details["redacted_items"])
        serialized = json.dumps(
            {
                "summary": result.summary,
                "error": result.error.to_dict(),
                "data": result.data,
                "usage": result.usage.to_dict(),
            },
            ensure_ascii=False,
            default=str,
        )
        self.assertNotIn(self.META_MARKER, serialized)
        self.assertNotIn("\ud800", serialized)

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
        self.assertEqual(BillingClass.SUBSCRIPTION_INCLUDED, result.usage.billing_class)
        self.assertEqual(MonetaryState.NOT_APPLICABLE, result.usage.monetary_state)
        self.assertIsNone(result.usage.monetary_cost_usd)
        self.assertEqual(0.0, result.usage.reference_cost_usd)
        self.assertEqual(401, result.error.details["api_error_status"])


class LocalReadPayloadTests(unittest.TestCase):
    """M3 - the file excerpt travels as attested outbound bytes, and the
    model is still granted no filesystem access of its own."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.connector = ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )

    def task(
        self,
        snapshot: LocalReadSnapshot | None = None,
        classification: DataClassification = DataClassification.INTERNAL,
    ) -> Task:
        task = Task(
            objective="Analyze the file",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=classification,
            ),
        )
        if snapshot is not None:
            task.local_read = snapshot
        return task

    @staticmethod
    def snapshot(
        excerpt: str = "alpha beta\n",
        classification: DataClassification = DataClassification.INTERNAL,
    ) -> LocalReadSnapshot:
        return LocalReadSnapshot(
            relative_path="docs/notes.txt",
            byte_size=len(excerpt.encode("utf-8")),
            sha256="c" * 64,
            excerpt=excerpt,
            truncated=False,
            classification=classification,
            read_at="2026-01-01T00:00:00+00:00",
        )

    def test_the_block_carries_exactly_the_allowed_fields(self) -> None:
        payload, _ = self.connector._sanitized_payload(self.task(self.snapshot()))
        self.assertEqual(
            {"notice", "path", "byte_size", "truncated", "content"},
            set(payload["local_read"]),
        )
        self.assertEqual("docs/notes.txt", payload["local_read"]["path"])
        self.assertEqual("alpha beta\n", payload["local_read"]["content"])

    def test_the_digest_and_read_time_stay_local(self) -> None:
        snapshot = self.snapshot()
        payload, _ = self.connector._sanitized_payload(self.task(snapshot))
        serialized = json.dumps(payload["local_read"])
        self.assertNotIn(snapshot.sha256, serialized)
        self.assertNotIn(snapshot.read_at, serialized)

    def test_a_task_with_no_read_keeps_the_previous_payload_shape(self) -> None:
        payload, _ = self.connector._sanitized_payload(self.task())
        self.assertNotIn("local_read", payload)

    def test_the_content_is_part_of_the_attested_outbound_bytes(self) -> None:
        first = self.connector.canonical_outbound(self.task(self.snapshot("one\n")))
        second = self.connector.canonical_outbound(self.task(self.snapshot("two\n")))
        self.assertNotEqual(
            hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest()
        )
        self.assertIn("one", first.decode("utf-8"))

    def test_the_excerpt_is_never_concatenated_into_the_command_line(self) -> None:
        # The content may only exist inside the labelled JSON payload; it must
        # never reach the system prompt or a separate CLI argument.
        prompt, _ = self.connector._build_prompt(self.task(self.snapshot("MARKER\n")))
        command = self.connector._command(prompt)
        occurrences = [part for part in command if "MARKER" in part]
        self.assertEqual([prompt], occurrences)
        self.assertEqual("", ",".join(claude_code.EGRESS_TOOL_GRANTS))

    def test_a_secret_task_never_serializes_the_excerpt(self) -> None:
        task = self.task(self.snapshot("SECRET-MARKER\n"), DataClassification.SECRET)
        payload, _ = self.connector._sanitized_payload(task)
        self.assertNotIn("local_read", payload)
        self.assertNotIn("SECRET-MARKER", json.dumps(payload, default=str))

    def test_an_ineligible_snapshot_drops_the_whole_block(self) -> None:
        task = self.task(
            self.snapshot("HIGHER-MARKER\n", DataClassification.SENSITIVE),
            DataClassification.PUBLIC,
        )
        payload, _ = self.connector._sanitized_payload(task)
        self.assertNotIn("local_read", payload)
        self.assertNotIn("HIGHER-MARKER", json.dumps(payload, default=str))

    def test_a_raw_string_classification_drops_the_block(self) -> None:
        """The exact-enum guard is load-bearing, not decorative.

        ``DataClassification`` is a ``str, Enum``, so the raw string
        ``"public"`` compares equal to ``DataClassification.PUBLIC`` AND hashes
        identically. It therefore satisfies the rank lookup and
        ``retrieval_eligible`` silently. Only the exact-type check at the
        egress boundary stops a SECRET-origin snapshot whose classification
        field was overwritten with that string from egressing.
        """
        snapshot = self.snapshot(
            "SECRET-ORIGIN-MARKER\n", DataClassification.SECRET
        )
        # Prove the premise rather than assuming it.
        self.assertTrue(retrieval_eligible(DataClassification.INTERNAL, "public"))
        self.assertFalse(isinstance("public", DataClassification))

        object.__setattr__(snapshot, "classification", "public")
        payload, _ = self.connector._sanitized_payload(self.task(snapshot))
        self.assertNotIn("local_read", payload)
        self.assertNotIn("SECRET-ORIGIN-MARKER", json.dumps(payload, default=str))

    def test_a_foreign_enum_classification_drops_the_block(self) -> None:
        """Same hazard through a different door: a foreign ``str`` enum with
        the same value is equally eligible under the ladder."""

        class ForeignClassification(str, Enum):
            PUBLIC = "public"

        self.assertTrue(
            retrieval_eligible(
                DataClassification.INTERNAL, ForeignClassification.PUBLIC
            )
        )
        self.assertFalse(
            isinstance(ForeignClassification.PUBLIC, DataClassification)
        )

        snapshot = self.snapshot(
            "FOREIGN-ENUM-MARKER\n", DataClassification.SECRET
        )
        object.__setattr__(snapshot, "classification", ForeignClassification.PUBLIC)
        payload, _ = self.connector._sanitized_payload(self.task(snapshot))
        self.assertNotIn("local_read", payload)
        self.assertNotIn("FOREIGN-ENUM-MARKER", json.dumps(payload, default=str))

    def test_a_malformed_current_classification_drops_the_block(self) -> None:
        """The same exact-type rule on the other side of the ladder.

        Exercised against ``_local_read`` directly: a raw-string scope
        classification makes ``ActionScope.to_dict()`` raise before
        ``_sanitized_payload`` ever reaches the local-read block, so the whole
        payload is refused anyway. This asserts the local-read guard itself
        rather than relying on that unrelated failure.
        """
        task = self.task(self.snapshot("CURRENT-SIDE-MARKER\n"))
        object.__setattr__(task.scope, "data_classification", "internal")
        block, redactions = self.connector._local_read(task)
        self.assertIsNone(block)
        self.assertEqual(0, redactions)

    def test_the_outbound_preview_counts_the_local_read_fields(self) -> None:
        preview = self.connector.outbound_preview(self.task(self.snapshot()))
        self.assertIn("local_read.content", preview.field_names)
        self.assertIn("local_read.notice", preview.field_names)


if __name__ == "__main__":
    unittest.main()
