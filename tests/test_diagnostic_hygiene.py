"""H4 - persisted diagnostic hygiene.

Presentation redaction is not enough: these tests inspect the RAW SQLite rows
(task JSON, result snapshots, loop checkpoints, event payloads, decision log)
and assert that credential-bearing, path-bearing, oversized or malformed
diagnostic text never became authoritative persisted state in the first place.

Every test uses fake connectors and a temporary SQLite store. No test starts a
live connector, auth probe, model subprocess, MCP or network call.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from enum import Enum
from pathlib import Path
from unittest import mock

from jarvis.contracts import (
    DIAGNOSTIC_DETAIL_KEY_MAX_CHARS,
    DIAGNOSTIC_INT_MAX,
    DIAGNOSTIC_INT_MIN,
    DIAGNOSTIC_DETAIL_MAX_CHARS,
    DIAGNOSTIC_MAX_DETAILS,
    DIAGNOSTIC_MESSAGE_MAX_CHARS,
    REDACTED_PATH,
    UNAVAILABLE_DIAGNOSTIC,
    RETRIEVAL_MAX_ITEMS,
    RETRIEVAL_RATIONALE_CAP,
    RETRIEVAL_SUMMARY_CAP,
    VERIFICATION_MAX_EVIDENCE,
    AttemptState,
    Availability,
    BillingClass,
    DataClassification,
    DecisionRecord,
    ErrorCode,
    LoopStage,
    MonetaryState,
    OutboundDataPreview,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    RetrievedContext,
    RetrievedMemoryItem,
    RetrievedMemoryKind,
    RiskLevel,
    RunStatus,
    SideEffect,
    SideEffectProfile,
    StructuredError,
    Task,
    TaskStatus,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
    credential_bearing,
    diagnostic_from_exception,
    redact_credentials,
    redact_local_paths,
    sanitize_diagnostic_details,
    sanitize_diagnostic_text,
    sanitize_result_snapshot,
)
from jarvis.connectors.local_files import LocalFileReadConnector
from jarvis.core import AgentCore
from jarvis.destinations import (
    CLAUDE_CODE_DESTINATION,
    DestinationGrant,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, make_task, open_app, open_core, safe_cleanup


#: A distinctive value that must never appear in any persisted row.
SECRET = "HUNTER2SECRETVALUE"

#: Every credential shape the canonical policy must remove from diagnostics.
CREDENTIAL_MESSAGES = (
    f"api_key: {SECRET}",
    f"api-key={SECRET}",
    f"openai_api_key={SECRET}",
    f"password={SECRET}",
    f"database_password={SECRET}",
    f"access_token={SECRET}",
    f"github-token: {SECRET}",
    f"client_secret={SECRET}",
    f"session_cookie={SECRET}",
    f"API_KEY: {SECRET}",
    f"Client_Secret = {SECRET}",
    f"AUTHORIZATION: {SECRET}",
    f"sk-{SECRET}abcdefghijkl",
    f"Bearer {SECRET}abcdefgh",
    f"eyJhbGciOiJIUzI1NiJ9.{SECRET}abcdef.{SECRET}ghijkl",
)

#: Ordinary prose and file names that merely mention token/secret. These must
#: survive readable; over-redaction is a defect too.
SAFE_MESSAGES = (
    "tokenizer.py",
    "secret_handling.md",
    "Review token telemetry",
    "the secret sauce of this token design is ordinary prose",
    "password handling documentation",
    "no credentials here at all",
)

#: Machine-local locations that must not persist as raw provenance.
PATH_MESSAGES = (
    r"crashed in C:\Users\realk\Masaustu\JARVIS\jarvis\core.py",
    r"fixture at C:\Users\realk\AppData\Local\Temp\pytest-9182\jarvis.db",
    r"interpreter C:\Program Files\Python313\python.exe missing",
    r"share \\build-server\releases\artifact.zip",
    "/home/someone/.cache/uv/archive-v0/tool.py exploded",
    "/usr/local/lib/python3.13/site-packages/anthropic/_client.py",
)

#: Fragments that reveal the machine, as opposed to the useful basename.
MACHINE_FRAGMENTS = (
    "realk",
    "Masaustu",
    "AppData",
    "pytest-9182",
    "Program Files",
    "build-server",
    "someone",
    "site-packages",
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class StageRaiser(DummyConnector):
    """Raises a chosen message at one chosen stage boundary.

    ``after_calls`` lets the raise happen *after* connector selection has
    already probed the connector, so the AgentCore stage boundary is exercised
    rather than the earlier registry-selection boundary.
    """

    def __init__(
        self,
        where: str,
        message: str = "boom",
        *,
        after_calls: int = 0,
        name: str = "raiser",
        exception: type[BaseException] = RuntimeError,
        **kwargs: object,
    ) -> None:
        super().__init__(name=name, **kwargs)
        self.where = where
        self.message = message
        self.after_calls = after_calls
        self.exception = exception
        self.availability_calls = 0
        self.estimate_calls = 0
        self.verify_calls = 0

    def _boom(self) -> None:
        raise self.exception(self.message)

    def availability(self) -> Availability:
        self.availability_calls += 1
        if self.where == "availability" and self.availability_calls > self.after_calls:
            self._boom()
        return super().availability()

    def estimate_cost(self, task):
        self.estimate_calls += 1
        if self.where == "estimate" and self.estimate_calls > self.after_calls:
            self._boom()
        return super().estimate_cost(task)

    def permission_request(self, task, session_id, approval_ttl_minutes=30, **kwargs):
        if self.where == "attestation":
            self._boom()
        return super().permission_request(
            task, session_id, approval_ttl_minutes, **kwargs
        )

    def execute(self, task, session) -> ToolResult:
        if self.where == "execute":
            self.execute_calls += 1
            self._boom()
        return super().execute(task, session)

    def verify(self, task, result, session) -> VerificationRecord:
        self.verify_calls += 1
        if self.where == "verify" and self.verify_calls > self.after_calls:
            self._boom()
        return super().verify(task, result, session)


class TalkativeConnector(DummyConnector):
    """Succeeds, but authors credential-bearing free-form result text."""

    def __init__(self, message: str, *, name: str = "talkative", **kwargs: object):
        super().__init__(name=name, **kwargs)
        self.message = message

    def execute(self, task, session) -> ToolResult:
        result = super().execute(task, session)
        result.summary = self.message
        result.data = {
            "next_step": self.message,
            "diagnostic": self.message,
            "nested": {"deep": self.message},
        }
        return result

    def verify(self, task, result, session) -> VerificationRecord:
        return VerificationRecord(
            status=VerificationStatus.PASSED,
            method=self.message,
            evidence=tuple(f"evidence {self.message}" for _ in range(20)),
        )


# --------------------------------------------------------------------------
# Shared setup
# --------------------------------------------------------------------------


class DiagnosticHygieneTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)

    def store(self) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(self.database)

    def core(
        self,
        connector,
        memory=None,
        *,
        mode: PermissionMode = PermissionMode.SMART_AUTO,
        **overrides,
    ) -> AgentCore:
        registry = ToolRegistry()
        registry.register(connector)
        return open_core(
            registry,
            memory or self.memory,
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
            settings=Settings(
                memory_database=self.database,
                permission_mode=mode,
                **overrides,
            ),
        )

    #: Every table that can hold free-form persisted text.
    PERSISTED_COLUMNS = (
        ("tasks", "state_json"),
        ("sessions", "state_json"),
        ("pending_approvals", "request_json"),
        ("permission_grants", "grant_json"),
        ("loop_checkpoints", "checkpoint_json"),
        ("event_log", "payload_json"),
        ("decision_log", "decision_json"),
        ("usage_ledger", "billing_evidence"),
    )

    def reservations(self, task_id: str) -> list[dict]:
        """The H3 attempt-billing rows for a task, straight from SQLite."""
        connection = sqlite3.connect(self.database)
        try:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM attempt_billing WHERE task_id = ?", (task_id,)
                )
            ]
        finally:
            connection.close()

    def raw_rows(self) -> dict[str, list[str]]:
        """Every persisted free-form column, straight from SQLite."""
        connection = sqlite3.connect(self.database)
        try:
            dump: dict[str, list[str]] = {}
            for table, column in self.PERSISTED_COLUMNS:
                dump[table] = [
                    row[0]
                    for row in connection.execute(f"SELECT {column} FROM {table}")
                    if row[0] is not None
                ]
            return dump
        finally:
            connection.close()

    def raw_text(self) -> str:
        return "\n".join(
            item for rows in self.raw_rows().values() for item in rows
        )

    def assert_no_leak(self, *, secret: str = SECRET, paths: bool = False) -> None:
        """No secret and no machine-local provenance in any persisted row."""
        for table, rows in self.raw_rows().items():
            blob = "\n".join(rows)
            self.assertNotIn(secret, blob, f"secret leaked into {table}")
            if paths:
                for fragment in MACHINE_FRAGMENTS:
                    self.assertNotIn(
                        fragment, blob, f"machine detail '{fragment}' leaked into {table}"
                    )

    def resave_round_trip(self, session_id: str, task_id: str):
        """Load, resave through the ordinary primitive, reload.

        Nothing unsafe may be reintroduced by a write-back, and the second
        round trip must be byte-identical to the first.
        """
        store = self.store()
        loaded = store.load_task(task_id)
        store.save_task(session_id, loaded)
        reloaded = store.load_task(task_id)
        self.assertEqual(
            json.dumps(loaded.to_dict(), sort_keys=True),
            json.dumps(reloaded.to_dict(), sort_keys=True),
        )
        return reloaded

    def assert_bounded(self) -> None:
        """No persisted free-form string exceeds the documented bound."""
        for table, rows in self.raw_rows().items():
            for row in rows:
                for value in _free_form_strings(row):
                    self.assertLessEqual(
                        len(value),
                        DIAGNOSTIC_MESSAGE_MAX_CHARS,
                        f"unbounded diagnostic persisted in {table}: {value[:60]!r}",
                    )


#: JSON keys whose values are bounded diagnostic text (as opposed to typed task
#: content such as the objective, or canonical scope identifiers).
_DIAGNOSTIC_KEYS = frozenset(
    {"message", "summary", "reason", "assumption", "question", "method", "rationale"}
)


def _free_form_strings(blob: str):
    """Every bounded-diagnostic string inside one persisted JSON row."""
    try:
        document = json.loads(blob)
    except (TypeError, ValueError):
        return []
    found: list[str] = []

    def walk(node, key=""):
        if isinstance(node, dict):
            for name, value in node.items():
                walk(value, str(name))
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and key in _DIAGNOSTIC_KEYS:
            found.append(node)

    walk(document)
    return found


# --------------------------------------------------------------------------
# H4-B: the sanitization policy itself
# --------------------------------------------------------------------------


class SanitizationPolicyTests(unittest.TestCase):
    def test_every_credential_shape_is_removed(self) -> None:
        for message in CREDENTIAL_MESSAGES:
            with self.subTest(message=message):
                cleaned = sanitize_diagnostic_text(message)
                self.assertNotIn(SECRET, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_safe_text_survives_readable(self) -> None:
        for message in SAFE_MESSAGES:
            with self.subTest(message=message):
                self.assertEqual(message, sanitize_diagnostic_text(message))

    def test_non_strings_never_become_diagnostic_text(self) -> None:
        for value in (None, 5, 1.5, True, {"a": 1}, ["a"], object(), b"bytes"):
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    UNAVAILABLE_DIAGNOSTIC, sanitize_diagnostic_text(value)
                )

    def test_m6_did_not_widen_the_diagnostic_pipeline(self) -> None:
        """M6 is additive: it created no exception to H4.

        The typed model answer bypasses this pipeline by being a separate
        typed contract - the same way ``LocalReadSnapshot`` already does -
        NOT by relaxing anything here. If a later change tries to buy
        deliverable fidelity by loosening the diagnostic bound instead, this
        fails first.
        """
        from jarvis.contracts import MODEL_ANSWER_MAX_CHARS

        self.assertEqual(240, DIAGNOSTIC_MESSAGE_MAX_CHARS)
        self.assertNotEqual(MODEL_ANSWER_MAX_CHARS, DIAGNOSTIC_MESSAGE_MAX_CHARS)
        multiline = sanitize_diagnostic_text(
            "first line\nsecond line\nC:\\Users\\someone\\project\\core.py tail"
        )
        # Still collapsed, still path-redacted: unchanged by M6.
        self.assertNotIn("\n", multiline)
        self.assertIn("[PATH]", multiline)
        self.assertNotIn("someone", multiline)

    def test_the_bound_is_explicit_and_deterministic(self) -> None:
        self.assertEqual(240, DIAGNOSTIC_MESSAGE_MAX_CHARS)
        cleaned = sanitize_diagnostic_text("y" * 10_000)
        self.assertEqual(DIAGNOSTIC_MESSAGE_MAX_CHARS, len(cleaned))
        self.assertTrue(cleaned.endswith("…"))
        # Deterministic: the same input always yields the same bytes.
        self.assertEqual(cleaned, sanitize_diagnostic_text("y" * 10_000))
        # A credential beyond the cap is still removed, because redaction runs
        # before truncation.
        buried = sanitize_diagnostic_text("z" * 400 + f" api_key: {SECRET}")
        self.assertNotIn(SECRET, buried)

    def test_sanitization_is_idempotent(self) -> None:
        for message in CREDENTIAL_MESSAGES + SAFE_MESSAGES + PATH_MESSAGES:
            with self.subTest(message=message):
                once = sanitize_diagnostic_text(message)
                self.assertEqual(once, sanitize_diagnostic_text(once))

    def test_pathological_whitespace_and_line_endings_collapse(self) -> None:
        cleaned = sanitize_diagnostic_text("a\r\nb\r\tc\n\n\n     d")
        self.assertEqual("a b c d", cleaned)
        self.assertEqual("", sanitize_diagnostic_text("   \n\t  "))

    def test_details_are_bounded_allowlisted_scalars(self) -> None:
        details = sanitize_diagnostic_details(
            {
                "ok": 1,
                "flag": True,
                "empty": None,
                "text": f"password={SECRET}",
                "nan": float("nan"),
                "real": 1.25,
                "nested": {"deep": SECRET},
                "listy": [SECRET],
                "obj": object(),
                "blob": "q" * 500,
            }
        )
        self.assertEqual(1, details["ok"])
        self.assertIs(True, details["flag"])
        self.assertIsNone(details["empty"])
        # A non-finite float is dropped, never turned into a fabricated value.
        self.assertNotIn("nan", details)
        self.assertEqual(1.25, details["real"])
        self.assertNotIn(SECRET, json.dumps(details))
        # Structural blobs are dropped rather than stringified.
        self.assertNotIn("nested", details)
        self.assertNotIn("listy", details)
        self.assertNotIn("obj", details)
        self.assertLessEqual(len(details["blob"]), DIAGNOSTIC_DETAIL_MAX_CHARS)

    def test_details_count_and_key_length_are_capped(self) -> None:
        many = sanitize_diagnostic_details({f"k{index}": index for index in range(50)})
        self.assertEqual(DIAGNOSTIC_MAX_DETAILS, len(many))
        long_key = sanitize_diagnostic_details({"k" * 500: 1})
        self.assertEqual(
            DIAGNOSTIC_DETAIL_KEY_MAX_CHARS, len(next(iter(long_key)))
        )
        self.assertEqual({}, sanitize_diagnostic_details("not-a-mapping"))
        self.assertEqual({}, sanitize_diagnostic_details(None))


class PathHygieneTests(unittest.TestCase):
    def test_machine_local_paths_are_reduced_to_a_safe_basename(self) -> None:
        for message in PATH_MESSAGES:
            with self.subTest(message=message):
                cleaned = sanitize_diagnostic_text(message)
                self.assertIn(REDACTED_PATH, cleaned)
                for fragment in MACHINE_FRAGMENTS:
                    self.assertNotIn(fragment, cleaned)

    def test_no_terminal_segment_is_ever_preserved(self) -> None:
        # A regex over free diagnostic text cannot prove a terminal segment is
        # a file rather than a user directory, so the whole path goes.
        self.assertEqual(
            f"crashed in {REDACTED_PATH}",
            sanitize_diagnostic_text(r"crashed in C:\Users\realk\JARVIS\jarvis\core.py"),
        )
        self.assertEqual(
            REDACTED_PATH,
            sanitize_diagnostic_text("/usr/lib/python3/anthropic/_client.py"),
        )
        self.assertEqual(REDACTED_PATH, sanitize_diagnostic_text(r"C:\Users\realk\\"))
        self.assertEqual(
            REDACTED_PATH,
            sanitize_diagnostic_text("/var/data/" + "n" * 200),
        )

    def test_urls_are_not_treated_as_local_paths(self) -> None:
        # An outbound endpoint is an attested typed value elsewhere; destroying
        # it would hide the very thing a diagnostic exists to report.
        message = "https://api.example.com/v1/messages timed out"
        self.assertEqual(message, sanitize_diagnostic_text(message))


# --------------------------------------------------------------------------
# H4-A: the safe diagnostic contract
# --------------------------------------------------------------------------


class StructuredErrorHygieneTests(unittest.TestCase):
    def test_construction_sanitizes_message_and_details(self) -> None:
        error = StructuredError(
            ErrorCode.CONNECTOR_FAILED,
            f"api_key: {SECRET} at C:\\Users\\realk\\x.py",
            details={"note": f"password={SECRET}", "blob": {"deep": SECRET}},
            retryable=True,
        )
        encoded = json.dumps(error.to_dict())
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn("realk", encoded)
        self.assertNotIn("blob", error.details)
        self.assertTrue(error.retryable)

    def test_exception_objects_never_reach_persisted_state(self) -> None:
        error = diagnostic_from_exception(
            ErrorCode.NO_CONNECTOR,
            ValueError(f"boom access_token={SECRET}"),
            stage=LoopStage.PLAN,
        )
        self.assertNotIn(SECRET, json.dumps(error.to_dict()))
        # Only bounded, non-secret structural tags survive.
        self.assertEqual("ValueError", error.details["exception_type"])
        self.assertEqual("plan", error.details["stage"])
        self.assertNotIn("traceback", error.details)

    def test_a_pathological_exception_still_yields_a_diagnostic(self) -> None:
        class Hostile(Exception):
            def __str__(self) -> str:
                raise RuntimeError("no message for you")

        error = diagnostic_from_exception(ErrorCode.CONNECTOR_FAILED, Hostile())
        self.assertEqual("", error.message)
        self.assertEqual("Hostile", error.details["exception_type"])

    def test_malformed_persisted_errors_load_conservatively(self) -> None:
        error = StructuredError.from_dict(
            {
                "code": "not_a_real_code",
                "message": f"sk-{SECRET}abcdefghij",
                "details": {"nested": {"x": SECRET}},
                "retryable": "yes-please",
            }
        )
        self.assertIs(ErrorCode.INVALID_TASK, error.code)
        self.assertNotIn(SECRET, json.dumps(error.to_dict()))
        self.assertIs(False, error.retryable)
        self.assertEqual({}, error.details)
        self.assertIsNone(StructuredError.from_dict(None))
        self.assertIsNone(StructuredError.from_dict("not-a-mapping"))

    def test_result_snapshots_are_allowlisted_and_scrubbed(self) -> None:
        safe = sanitize_result_snapshot(
            {
                "outcome": "success",
                "summary": f"password={SECRET}",
                "data": {"next_step": "fine", "diagnostic": SECRET, "deep": {"x": 1}},
                "error": None,
                "usage": {"call_count": 1},
                "verification": {
                    "status": "passed",
                    "method": f"api_key: {SECRET}",
                    "evidence": [f"api_key: {SECRET}"] * 40,
                },
                "attacker_field": SECRET,
            }
        )
        encoded = json.dumps(safe)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn("attacker_field", safe)
        self.assertEqual({"next_step": "fine"}, safe["data"])
        self.assertLessEqual(
            len(safe["verification"]["evidence"]), VERIFICATION_MAX_EVIDENCE
        )
        self.assertIsNone(sanitize_result_snapshot("not-a-mapping"))
        self.assertEqual(safe, sanitize_result_snapshot(safe))


# --------------------------------------------------------------------------
# Raw persistence: no credential survives anywhere
# --------------------------------------------------------------------------


class PersistedDiagnosticLeakTests(DiagnosticHygieneTestCase):
    def test_credential_bearing_connector_exceptions_never_persist(self) -> None:
        for message in CREDENTIAL_MESSAGES:
            with self.subTest(message=message):
                self.setUp()
                connector = StageRaiser("execute", message)
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(ErrorCode.CONNECTOR_FAILED, run.error.code)
                self.assert_no_leak()
                self.assert_bounded()

    def test_credential_bearing_stage_exceptions_never_persist(self) -> None:
        for where, after in (
            ("estimate", 1),
            ("availability", 1),
            ("attestation", 0),
            ("verify", 0),
        ):
            for message in CREDENTIAL_MESSAGES[:6]:
                with self.subTest(where=where, message=message):
                    self.setUp()
                    connector = StageRaiser(where, message, after_calls=after)
                    run = self.core(connector, self.store()).run(make_task())
                    self.assertEqual(RunStatus.FAILED, run.status)
                    self.assert_no_leak()
                    self.assert_bounded()

    def test_credential_bearing_success_text_never_persists(self) -> None:
        # A connector that *succeeds* but authors credential-bearing summary,
        # data and verification evidence. The completed decision record is the
        # history future work would inherit.
        for message in CREDENTIAL_MESSAGES[:6]:
            with self.subTest(message=message):
                self.setUp()
                connector = TalkativeConnector(message)
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.COMPLETED, run.status)
                self.assert_no_leak()
                self.assert_bounded()
                decisions = self.store().list_decisions(run.session.id)
                self.assertEqual(1, len(decisions))
                self.assertNotIn(SECRET, json.dumps(decisions[0].to_dict()))

    def test_arbitrary_connector_data_is_not_persisted(self) -> None:
        connector = TalkativeConnector("plain summary")
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        snapshot = self.store().load_task(run.task.id).result_snapshot
        # Only the single key the loop consumes survives.
        self.assertEqual({"next_step"}, set(snapshot["data"]))
        self.assertNotIn("diagnostic", json.dumps(snapshot))
        self.assertNotIn("nested", json.dumps(snapshot))

    def test_machine_local_paths_never_persist_raw(self) -> None:
        for message in PATH_MESSAGES:
            with self.subTest(message=message):
                self.setUp()
                connector = StageRaiser("execute", message)
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assert_no_leak(paths=True)

    def test_safe_diagnostic_prose_stays_readable(self) -> None:
        for message in SAFE_MESSAGES:
            with self.subTest(message=message):
                self.setUp()
                connector = StageRaiser("execute", message)
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(message, run.error.message)
                self.assertIn(message, self.raw_text())


class OversizedDiagnosticTests(DiagnosticHygieneTestCase):
    def test_huge_diagnostics_are_bounded_and_restart_stable(self) -> None:
        connector = StageRaiser("execute", "Q" * 200_000)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(DIAGNOSTIC_MESSAGE_MAX_CHARS, len(run.error.message))
        self.assert_bounded()

        # Restart: reloading and reporting the terminal task gives byte-
        # equivalent safe diagnostic state, and re-saving writes the same bytes.
        stored = self.store().load_task(run.task.id)
        first = json.dumps(stored.to_dict(), sort_keys=True)
        second = json.dumps(Task.from_dict(json.loads(first)).to_dict(), sort_keys=True)
        self.assertEqual(first, second)
        replay = self.core(StageRaiser("execute", "Q" * 200_000), self.store()).resume(
            run.session.id, run.task.id
        )
        self.assertEqual(RunStatus.FAILED, replay.status)
        self.assertEqual(run.error.message, replay.error.message)

    def test_a_huge_diagnostic_renders_without_crashing(self) -> None:
        from jarvis.app import build_envelope

        connector = StageRaiser("execute", "Q" * 200_000)
        run = self.core(connector, self.store()).run(make_task())
        envelope = build_envelope("run", PermissionMode.SMART_AUTO, run)
        rendered = json.dumps(envelope, allow_nan=False)
        self.assertIn("connector_failed", rendered)
        self.assertLessEqual(
            len(envelope["error"]["message"]), DIAGNOSTIC_MESSAGE_MAX_CHARS
        )


# --------------------------------------------------------------------------
# H4-D: stage-boundary normalization
# --------------------------------------------------------------------------


class StageBoundaryNormalizationTests(DiagnosticHygieneTestCase):
    def assert_terminal_failure(self, run, code: ErrorCode, stage: str) -> None:
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(code, run.error.code)
        self.assertIs(TaskStatus.FAILED, run.task.status)
        self.assertIs(LoopStage.FAILED, run.task.stage)
        self.assertEqual(stage, run.error.details.get("stage"))
        events = [item["event_type"] for item in self.store().list_events(run.session.id)]
        self.assertIn("task_failed", events)
        self.assertNotIn("task_completed", events)
        self.assertEqual([], self.store().list_decisions(run.session.id))
        # A reloaded terminal task reports the same failure; it never retries.
        replay = self.core(DummyConnector(name="raiser"), self.store()).resume(
            run.session.id, run.task.id
        )
        self.assertEqual(RunStatus.FAILED, replay.status)
        self.assertEqual(code, replay.error.code)

    def test_context_retrieval_failure_is_normalized(self) -> None:
        connector = StageRaiser("none")
        memory = self.store()
        core = self.core(connector, memory)
        with mock.patch.object(
            memory, "retrieve_context", side_effect=RuntimeError(f"api_key: {SECRET}")
        ):
            run = core.run(make_task())
        self.assert_terminal_failure(
            run, ErrorCode.INVALID_TASK, LoopStage.CONTEXT_RETRIEVAL.value
        )
        self.assertEqual(0, connector.execute_calls)
        self.assert_no_leak()

    def test_planning_failure_is_normalized(self) -> None:
        connector = StageRaiser("none")
        core = self.core(connector, self.store())
        with mock.patch(
            "jarvis.core.build_task_plan", side_effect=RuntimeError(f"password={SECRET}")
        ):
            run = core.run(make_task())
        self.assert_terminal_failure(run, ErrorCode.INVALID_TASK, LoopStage.PLAN.value)
        self.assertEqual(0, connector.execute_calls)
        self.assert_no_leak()

    def test_connector_selection_failure_is_normalized(self) -> None:
        connector = StageRaiser("estimate", f"client_secret={SECRET}")
        run = self.core(connector, self.store()).run(make_task())
        # Selection probes the connector first; either boundary is fail-closed.
        self.assert_terminal_failure(run, ErrorCode.NO_CONNECTOR, LoopStage.PLAN.value)
        self.assertEqual(0, connector.execute_calls)
        self.assert_no_leak()

    def test_cost_estimation_failure_is_normalized(self) -> None:
        connector = StageRaiser("estimate", f"access_token={SECRET}", after_calls=1)
        run = self.core(connector, self.store()).run(make_task())
        self.assert_terminal_failure(
            run, ErrorCode.BILLING_ROUTE_UNKNOWN, LoopStage.COST_PREFLIGHT.value
        )
        self.assertEqual(0, connector.execute_calls)
        # The malformed probe never became the task's billing route.
        self.assertIs(BillingClass.UNKNOWN, run.task.estimate.billing_class)
        self.assert_no_leak()

    def test_tool_availability_failure_is_normalized(self) -> None:
        connector = StageRaiser("availability", f"session_cookie={SECRET}", after_calls=1)
        run = self.core(connector, self.store()).run(make_task())
        self.assert_terminal_failure(
            run, ErrorCode.CONNECTOR_UNAVAILABLE, LoopStage.TOOL_SELECTION.value
        )
        self.assertEqual(0, connector.execute_calls)
        self.assert_no_leak()

    def test_attestation_failure_is_normalized(self) -> None:
        connector = StageRaiser("attestation", f"Bearer {SECRET}abcdefgh")
        run = self.core(connector, self.store()).run(make_task())
        self.assert_terminal_failure(
            run, ErrorCode.EGRESS_REFUSED, LoopStage.RISK_PERMISSION.value
        )
        self.assertEqual(0, connector.execute_calls)
        self.assert_no_leak()

    def test_verification_failure_is_normalized(self) -> None:
        connector = StageRaiser("verify", f"api_key: {SECRET}")
        run = self.core(connector, self.store()).run(make_task())
        self.assert_terminal_failure(
            run, ErrorCode.VERIFICATION_FAILED, LoopStage.VERIFY.value
        )
        # Execution genuinely happened once and is not repeated.
        self.assertEqual(1, connector.execute_calls)
        self.assert_no_leak()

    def test_malformed_persisted_result_fails_deterministically(self) -> None:
        for snapshot in (
            {"outcome": "not-an-outcome", "summary": "x"},
            {"summary": "no outcome at all"},
            {"outcome": "success", "summary": "x", "verification": {"status": "bogus"}},
            {"outcome": f"api_key: {SECRET}", "summary": "x"},
        ):
            with self.subTest(snapshot=snapshot):
                self.setUp()
                connector = DummyConnector(name="raiser")
                core = self.core(connector, self.store())
                session = core.start_session("malformed result")
                task = make_task()
                task.selected_connector = connector.manifest.name
                task.stage = LoopStage.VERIFY
                task.status = TaskStatus.VERIFYING
                task.result_snapshot = snapshot
                self.memory.save_task(session.id, task)
                run = core.resume(session.id, task.id)
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(ErrorCode.VERIFICATION_FAILED, run.error.code)
                self.assertEqual(0, connector.execute_calls)
                # Terminal, so a restart reports it instead of retrying.
                again = core.resume(session.id, task.id)
                self.assertEqual(RunStatus.FAILED, again.status)


# --------------------------------------------------------------------------
# H2/H3 interaction
# --------------------------------------------------------------------------


class ExecutionAuthorityInteractionTests(DiagnosticHygieneTestCase):
    def test_a_raising_egress_connector_keeps_h2_authority(self) -> None:
        # The external outcome is unknowable; H2 owns it and diagnostics must
        # not reinterpret UNKNOWN_OUTCOME as an ordinary failure.
        connector = StageRaiser(
            "execute",
            f"api_key: {SECRET}",
            side_effect=SideEffect.EXTERNAL_EGRESS,
        )
        core = self.core(connector, self.store())
        waiting = core.run(make_task(effect=SideEffect.EXTERNAL_EGRESS))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
        run = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request),
        )
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, run.error.code)
        attempt = self.store().latest_execution_attempt(run.task.id)
        self.assertIs(AttemptState.UNKNOWN_OUTCOME, attempt.state)
        self.assertNotIn(SECRET, self.raw_text())
        # Not terminal: the task stays recoverable and is never replayed.
        fresh = StageRaiser(
            "execute", "quiet", side_effect=SideEffect.EXTERNAL_EGRESS
        )
        replay = self.core(fresh, self.store()).resume(run.session.id, run.task.id)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, replay.error.code)
        self.assertEqual(0, fresh.execute_calls)

    def test_verification_failure_preserves_h3_accounting(self) -> None:
        # The connector boundary was genuinely crossed and money was measured;
        # a later diagnostic failure must not erase that accounting.
        connector = StageRaiser(
            "verify", f"password={SECRET}", estimate_max=0.20, actual_cost=0.05
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.VERIFICATION_FAILED, run.error.code)
        self.assertEqual(1, connector.execute_calls)
        store = self.store()
        self.assertAlmostEqual(0.05, store.monthly_monetary_cost())
        attempt = store.latest_execution_attempt(run.task.id)
        self.assertIs(AttemptState.FINISHED, attempt.state)
        record = store.attempt_billing_record(attempt.id)
        self.assertEqual("reconciled", record["state"])
        self.assertEqual(50_000_000, record["actual_nano_usd"])
        self.assertNotIn(SECRET, self.raw_text())

    def test_a_diagnostic_failure_creates_no_completed_decision(self) -> None:
        connector = StageRaiser("verify", "verifier exploded")
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual([], self.store().list_decisions(run.session.id))
        events = [item["event_type"] for item in self.store().list_events(run.session.id)]
        self.assertNotIn("task_completed", events)
        self.assertIn("task_failed", events)


# --------------------------------------------------------------------------
# H4-G: legacy / adversarial persisted rows
# --------------------------------------------------------------------------


class LegacyPersistenceTests(DiagnosticHygieneTestCase):
    UNSAFE_SNAPSHOT = {
        "outcome": "error",
        "summary": f"legacy failure api_key: {SECRET}",
        "data": {
            "next_step": f"password={SECRET}",
            "diagnostic": SECRET,
            "traceback": ["frame"] * 50,
        },
        "error": {
            "code": "connector_failed",
            "message": (
                "boom at C:\\Users\\realk\\AppData\\Local\\Temp\\x.py "
                f"api_key: {SECRET}"
            ),
            "details": {
                "traceback": "Traceback (most recent call last): ...",
                "env": {"ANTHROPIC_API_KEY": SECRET},
                "command": f"claude --token={SECRET}",
                "huge": "H" * 5000,
            },
            "retryable": True,
        },
        "usage": {"call_count": 1},
        "verification": {
            "status": "failed",
            "method": f"client_secret={SECRET}",
            "evidence": [f"access_token={SECRET}"] * 30,
        },
        "attacker_field": SECRET,
    }

    def seed_unsafe_task(self) -> tuple[str, str]:
        """Write a pre-H4 style task row straight into SQLite."""
        core = self.core(DummyConnector(name="legacy"), self.store())
        session = core.start_session("legacy")
        task = make_task()
        task.status = TaskStatus.FAILED
        task.stage = LoopStage.FAILED
        self.memory.save_task(session.id, task)
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task.id,)
            ).fetchone()
            document = json.loads(row[0])
            document["result_snapshot"] = self.UNSAFE_SNAPSHOT
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document, ensure_ascii=False), task.id),
            )
            connection.commit()
        finally:
            connection.close()
        # The unsafe text really is on disk before the test starts.
        self.assertIn(SECRET, self.raw_text())
        return session.id, task.id

    def test_unsafe_legacy_diagnostics_do_not_survive_load(self) -> None:
        _, task_id = self.seed_unsafe_task()
        loaded = self.store().load_task(task_id)
        encoded = json.dumps(loaded.result_snapshot)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn("realk", encoded)
        self.assertNotIn("Traceback", encoded)
        self.assertNotIn("attacker_field", encoded)
        self.assertNotIn("ANTHROPIC_API_KEY", encoded)
        self.assertEqual({"next_step"}, set(loaded.result_snapshot["data"]))

    def test_load_resave_reload_drops_the_unsafe_original(self) -> None:
        session_id, task_id = self.seed_unsafe_task()
        store = self.store()
        loaded = store.load_task(task_id)
        # Resave through the ordinary persistence primitive.
        store.save_task(session_id, loaded)
        self.assert_no_leak(paths=True)
        reloaded = store.load_task(task_id)
        # Byte-equivalent across the second round trip.
        self.assertEqual(
            json.dumps(loaded.to_dict(), sort_keys=True),
            json.dumps(reloaded.to_dict(), sort_keys=True),
        )

    def test_a_resumed_legacy_task_reports_safely(self) -> None:
        session_id, task_id = self.seed_unsafe_task()
        connector = DummyConnector(name="legacy")
        run = self.core(connector, self.store()).resume(session_id, task_id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(0, connector.execute_calls)
        # Nothing unsafe becomes active state again: the reported diagnostic,
        # the restored result and the loaded snapshot are all scrubbed.
        reported = json.dumps(run.result.error.to_dict())
        self.assertNotIn(SECRET, reported)
        self.assertNotIn("realk", reported)
        self.assertNotIn("Traceback", reported)
        self.assertNotIn(SECRET, json.dumps(run.task.result_snapshot))
        self.assertNotIn(SECRET, run.result.summary)
        # H4 sanitizes on load and on the next write; it deliberately does not
        # retro-rewrite untouched historical rows (that would be a migration,
        # and a terminal task is immutable). The stale bytes stay put until
        # something writes, and nothing unsafe can be written back.
        self.assertIn(SECRET, self.raw_text())

    def test_a_legacy_decision_row_is_not_rewritten_unsafely(self) -> None:
        # Decisions are historical records; a completed task must still be the
        # only thing that produces one, and nothing unsafe is added later.
        connector = TalkativeConnector(f"api_key: {SECRET}")
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        decision = self.store().list_decisions(run.session.id)[0]
        self.assertNotIn(SECRET, decision.summary)
        self.assertNotIn(SECRET, decision.rationale)
        self.assertLessEqual(len(decision.summary), DIAGNOSTIC_MESSAGE_MAX_CHARS)
        self.assertLessEqual(len(decision.rationale), DIAGNOSTIC_MESSAGE_MAX_CHARS)

    def test_direct_usage_and_decision_writes_stay_bounded(self) -> None:
        # A record written straight through the storage primitives is still
        # subject to the same diagnostic bounds via its typed contracts.
        core = self.core(DummyConnector(name="direct"), self.store())
        session = core.start_session("direct")
        task = make_task()
        self.memory.save_task(session.id, task)
        self.memory.finalize_task(
            session,
            task,
            DecisionRecord(
                session_id=session.id,
                task_id=task.id,
                summary=sanitize_diagnostic_text(f"api_key: {SECRET}"),
                rationale=sanitize_diagnostic_text("R" * 5000),
            ),
            UsageRecord(
                billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
                monetary_state=MonetaryState.NOT_APPLICABLE,
            ),
            "task_completed",
            {"summary": sanitize_diagnostic_text(f"password={SECRET}")},
        )
        self.assert_no_leak()
        self.assert_bounded()


# --------------------------------------------------------------------------
# P1-A: nested result_snapshot fields are exact allowlists
# --------------------------------------------------------------------------


class NestedSnapshotAllowlistTests(DiagnosticHygieneTestCase):
    """``usage`` and ``verification`` are default-deny at the nested boundary."""

    def round_trip(self, snapshot: dict) -> dict:
        """Persist a snapshot through real SQLite and read it back."""
        core = self.core(DummyConnector(name="nested"), self.store())
        session = core.start_session("nested")
        task = make_task()
        task.status = TaskStatus.FAILED
        task.stage = LoopStage.FAILED
        task.result_snapshot = snapshot
        self.memory.save_task(session.id, task)
        loaded = self.store().load_task(task.id)
        # Resave and reload: the second round trip must be byte-identical.
        self.store().save_task(session.id, loaded)
        reloaded = self.store().load_task(task.id)
        self.assertEqual(
            json.dumps(loaded.to_dict(), sort_keys=True),
            json.dumps(reloaded.to_dict(), sort_keys=True),
        )
        return reloaded.result_snapshot

    def test_unknown_verification_fields_never_survive(self) -> None:
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "verification": {
                    "status": "passed",
                    "method": "check",
                    "evidence": ["proof"],
                    "payload": f"api_key: {SECRET}",
                    "raw": {"deep": f"password={SECRET}"},
                    "metadata": [f"access_token={SECRET}", {"x": SECRET}],
                    "huge": "H" * 50_000,
                    "nested": {"a": {"b": {"c": SECRET}}},
                },
            }
        )
        self.assertEqual(
            {"status", "method", "evidence"}, set(snapshot["verification"])
        )
        self.assert_no_leak()
        self.assertNotIn(SECRET, json.dumps(snapshot))
        # The canonical fields still work.
        self.assertEqual("passed", snapshot["verification"]["status"])
        self.assertEqual(["proof"], snapshot["verification"]["evidence"])

    def test_verification_evidence_is_bounded_and_scrubbed(self) -> None:
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "verification": {
                    "status": "passed",
                    "method": f"client_secret={SECRET}",
                    "evidence": [f"api_key: {SECRET}"] * 40
                    + [{"nested": SECRET}, ["listy"], None, "E" * 5000],
                },
            }
        )
        evidence = snapshot["verification"]["evidence"]
        self.assertLessEqual(len(evidence), VERIFICATION_MAX_EVIDENCE)
        for item in evidence:
            self.assertIsInstance(item, str)
            self.assertLessEqual(len(item), DIAGNOSTIC_DETAIL_MAX_CHARS)
        self.assertNotIn(SECRET, json.dumps(snapshot))
        self.assert_no_leak()

    def test_unknown_usage_fields_never_survive(self) -> None:
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "usage": {
                    "call_count": 1,
                    "input_tokens": 12,
                    "attacker_field": SECRET,
                    "raw": {"deep": SECRET},
                    "metadata": [SECRET],
                    "huge": "H" * 50_000,
                },
            }
        )
        self.assertEqual({"call_count", "input_tokens"}, set(snapshot["usage"]))
        self.assertEqual(1, snapshot["usage"]["call_count"])
        self.assertEqual(12, snapshot["usage"]["input_tokens"])
        self.assertNotIn(SECRET, json.dumps(snapshot))
        self.assert_no_leak()

    def test_credential_bearing_billing_evidence_never_survives(self) -> None:
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "usage": {
                    "billing_evidence": f"api_key: {SECRET}",
                    "provider": f"--token {SECRET}",
                    "model": r"C:\Users\realk\models\m.bin",
                    "connector": "claude-code",
                },
            }
        )
        usage = snapshot["usage"]
        self.assertNotIn(SECRET, json.dumps(snapshot))
        self.assertIn("[REDACTED]", usage["billing_evidence"])
        self.assertIn("[REDACTED]", usage["provider"])
        self.assertNotIn("realk", usage["model"])
        # A safe identifier is untouched.
        self.assertEqual("claude-code", usage["connector"])
        self.assert_no_leak(paths=True)

    def test_nested_blobs_in_usage_are_dropped_not_stringified(self) -> None:
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "usage": {
                    "input_tokens": {"deep": SECRET},
                    "output_tokens": [SECRET],
                    "provider": {"deep": SECRET},
                    "monetary_cost_usd": 0.05,
                },
            }
        )
        # Counts normalize to the legacy-safe zero; a non-string free-form
        # field is dropped entirely. Nothing is stringified.
        self.assertEqual(0, snapshot["usage"]["input_tokens"])
        self.assertEqual(0, snapshot["usage"]["output_tokens"])
        self.assertNotIn("provider", snapshot["usage"])
        self.assertEqual(0.05, snapshot["usage"]["monetary_cost_usd"])
        self.assertNotIn(SECRET, json.dumps(snapshot))

    def test_bool_and_string_counts_never_become_active_state(self) -> None:
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "usage": {
                    "call_count": True,
                    "input_tokens": "7",
                    "output_tokens": 3.5,
                },
            }
        )
        for field in ("call_count", "input_tokens", "output_tokens"):
            self.assertIs(int, type(snapshot["usage"][field]))
            self.assertEqual(0, snapshot["usage"][field])
        restored = AgentCore._result_from_dict(snapshot)
        self.assertEqual(0, restored.usage.call_count)
        self.assertIs(int, type(restored.usage.call_count))

    def test_malformed_monetary_values_normalize_to_fail_closed(self) -> None:
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "usage": {
                    "billing_class": "metered_monetary",
                    "monetary_state": "measured",
                    "monetary_cost_usd": None,
                    "reference_cost_usd": -1.0,
                    "estimated_cost_usd": "not-a-number",
                },
            }
        )
        usage = snapshot["usage"]
        # A *measured* metered row with no amount is not a valid ledger shape;
        # it degrades to the fail-closed state instead of persisting a
        # combination that would raise on a later write.
        self.assertEqual("unknown", usage["monetary_state"])
        self.assertIsNone(usage["monetary_cost_usd"])
        self.assertIsNone(usage["reference_cost_usd"])
        self.assertEqual(0.0, usage["estimated_cost_usd"])
        restored = AgentCore._result_from_dict(snapshot)
        self.assertIsNotNone(restored)
        self.assertFalse(restored.usage.counts_toward_monetary_budget)
        self.assertEqual(0.0, restored.usage.budget_amount_usd)

    def test_oversized_usage_integers_normalize_to_zero(self) -> None:
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "usage": {"input_tokens": 10**5000, "output_tokens": 2**64},
            }
        )
        self.assertEqual(0, snapshot["usage"]["input_tokens"])
        self.assertEqual(0, snapshot["usage"]["output_tokens"])
        # Still valid JSON, still loadable.
        json.dumps(snapshot)

    def test_raw_string_billing_enums_are_dropped_fail_closed(self) -> None:
        # An enum-like field must carry an exact canonical value. A tampered
        # one is dropped, so the restored record falls back to the fail-closed
        # UNKNOWN route that authorizes nothing - the ledger, not this
        # snapshot, is the authority.
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "usage": {
                    "billing_class": "bogus",
                    "monetary_state": f"api_key: {SECRET}",
                },
            }
        )
        self.assertNotIn("billing_class", snapshot["usage"])
        self.assertNotIn("monetary_state", snapshot["usage"])
        self.assertNotIn(SECRET, json.dumps(snapshot))
        restored = AgentCore._result_from_dict(snapshot)
        self.assertIs(BillingClass.UNKNOWN, restored.usage.billing_class)
        self.assertIs(MonetaryState.UNKNOWN, restored.usage.monetary_state)
        self.assertFalse(restored.usage.counts_toward_monetary_budget)

    def test_credential_bearing_outcome_never_survives(self) -> None:
        for outcome in (f"api_key: {SECRET}", "O" * 50_000, {"evil": SECRET}, 7):
            with self.subTest(outcome=repr(outcome)[:40]):
                snapshot = self.round_trip(
                    {"outcome": outcome, "summary": "fine"}
                )
                self.assertIsNone(snapshot["outcome"])
                self.assertNotIn(SECRET, json.dumps(snapshot))
                # Unrestorable, so the loop hits its deterministic failure.
                self.assertIsNone(AgentCore._result_from_dict(snapshot))

    def test_credential_bearing_verification_status_never_survives(self) -> None:
        for status in (f"api_key: {SECRET}", "S" * 50_000, "bogus", 7):
            with self.subTest(status=repr(status)[:40]):
                snapshot = self.round_trip(
                    {
                        "outcome": "success",
                        "summary": "fine",
                        "verification": {"status": status, "method": "m"},
                    }
                )
                self.assertIsNone(snapshot["verification"]["status"])
                self.assertNotIn(SECRET, json.dumps(snapshot))
                self.assertIsNone(AgentCore._result_from_dict(snapshot))

    def test_a_non_string_verification_status_fails_closed(self) -> None:
        snapshot = self.round_trip(
            {
                "outcome": "success",
                "summary": "fine",
                "verification": {"status": {"evil": SECRET}, "method": "m"},
            }
        )
        self.assertIsNone(snapshot["verification"]["status"])
        self.assertIsNone(AgentCore._result_from_dict(snapshot))
        self.assertNotIn(SECRET, json.dumps(snapshot))

    def test_a_clean_snapshot_round_trips_unchanged(self) -> None:
        connector = DummyConnector(name="clean")
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        stored = self.store().load_task(run.task.id)
        first = json.dumps(stored.to_dict(), sort_keys=True)
        second = json.dumps(
            Task.from_dict(json.loads(first)).to_dict(), sort_keys=True
        )
        self.assertEqual(first, second)
        # The canonical typed fields survive intact.
        usage = stored.result_snapshot["usage"]
        self.assertEqual("no_additional_charge", usage["billing_class"])
        self.assertEqual("not_applicable", usage["monetary_state"])
        self.assertEqual(1, usage["call_count"])
        self.assertEqual(
            "passed", stored.result_snapshot["verification"]["status"]
        )


# --------------------------------------------------------------------------
# P1-B: terminal path segments and command-line credentials
# --------------------------------------------------------------------------


class TerminalPathSegmentTests(unittest.TestCase):
    #: A terminal directory is exactly the local account/machine detail that
    #: must not survive, so an unproven terminal segment keeps nothing.
    DIRECTORY_PATHS = (
        r"C:\Users\realk",
        r"C:\Users\realk\AppData\Local\Temp",
        r"C:\Users\realk\Masaustu\JARVIS",
        r"D:\repos\jarvis",
        "/home/realk",
        "/home/realk/.cache/uv",
        "/Users/realk/Library/Caches",
        "/usr/local/lib/python3.13/site-packages",
        "/tmp/pytest-of-realk/pytest-91",
    )
    #: A dotted terminal segment is indistinguishable from a user directory
    #: (``john.doe``), so even an obvious-looking filename is redacted whole.
    FILE_PATHS = (
        r"C:\Users\realk\JARVIS\jarvis\core.py",
        r"C:\Users\realk\AppData\Local\Temp\pytest-9\jarvis.db",
        r"C:\Program Files\Python313\python.exe",
        "/usr/lib/python3.13/anthropic/_client.py",
        r"\\build-server\releases\artifact.zip",
    )
    #: The exact leak the conservative policy exists to close.
    DOTTED_USER_PATHS = (
        r"C:\Users\john.doe",
        "/home/john.doe",
        r"C:\Users\john.doe\AppData\Local\Temp",
        "/tmp/pytest-of-john.doe/pytest-9",
        "/Users/jane.roe/Library/Caches",
    )

    def test_terminal_directories_are_fully_redacted(self) -> None:
        for path in self.DIRECTORY_PATHS:
            with self.subTest(path=path):
                cleaned = sanitize_diagnostic_text(f"failed at {path}")
                self.assertEqual(f"failed at {REDACTED_PATH}", cleaned)
                self.assertNotIn("realk", cleaned)

    def test_file_looking_paths_are_redacted_whole(self) -> None:
        for path in self.FILE_PATHS:
            with self.subTest(path=path):
                self.assertEqual(
                    f"failed at {REDACTED_PATH}",
                    sanitize_diagnostic_text(f"failed at {path}"),
                )

    def test_dotted_user_directories_never_leak(self) -> None:
        # ``C:\Users\john.doe`` used to survive as ``[PATH]/john.doe``.
        for path in self.DOTTED_USER_PATHS:
            with self.subTest(path=path):
                cleaned = sanitize_diagnostic_text(f"failed at {path}")
                self.assertEqual(f"failed at {REDACTED_PATH}", cleaned)
                for fragment in ("john.doe", "jane.roe"):
                    self.assertNotIn(fragment, cleaned)

    def test_a_version_or_dotfile_directory_is_redacted(self) -> None:
        self.assertEqual(
            REDACTED_PATH, sanitize_diagnostic_text("/usr/lib/python3.13")
        )
        self.assertEqual(
            REDACTED_PATH, sanitize_diagnostic_text("/home/realk/.cache")
        )


class CommandLineCredentialTests(unittest.TestCase):
    FLAGS = (
        "--api-key",
        "--api_key",
        "--apikey",
        "--access-token",
        "--access_token",
        "--token",
        "--password",
        "--passwd",
        "--client-secret",
        "--client_secret",
        "--secret",
        "--authorization",
        "--private-key",
        "--session-cookie",
        "--API-KEY",
        "--Token",
        "-token",
    )

    def test_space_and_equals_forms_are_redacted(self) -> None:
        for flag in self.FLAGS:
            for joiner in (" ", "="):
                with self.subTest(flag=flag, joiner=joiner):
                    cleaned = sanitize_diagnostic_text(
                        f"claude {flag}{joiner}{SECRET} --print"
                    )
                    self.assertNotIn(SECRET, cleaned)
                    self.assertIn("[REDACTED]", cleaned)
                    # The flag itself survives, so the diagnostic stays useful.
                    self.assertIn(flag.split("=")[0], cleaned)

    def test_a_command_line_credential_never_persists(self) -> None:
        for text in (
            f"spawn failed: claude --api-key {SECRET} -p prompt",
            f"spawn failed: claude --token={SECRET}",
        ):
            with self.subTest(text=text):
                self.assertNotIn(SECRET, sanitize_diagnostic_text(text))

    def test_quoted_values_are_consumed_whole(self) -> None:
        # The whole quoted argument goes, including the spaces inside it: no
        # secret tail and no stray quote may survive.
        for text in (
            f'--password "{SECRET} TAILSECRET"',
            f"--password '{SECRET} TAILSECRET'",
            f'--api-key="{SECRET} TAIL"',
            f"--api-key='{SECRET} TAIL'",
            f"--CLIENT_SECRET '{SECRET} tail'",
            f'--Access-Token "{SECRET} tail" --print',
            f'claude --session-cookie "{SECRET} a b c" then',
        ):
            with self.subTest(text=text):
                cleaned = sanitize_diagnostic_text(text)
                self.assertNotIn(SECRET, cleaned)
                self.assertNotIn("TAIL", cleaned)
                self.assertNotIn("tail", cleaned)
                self.assertNotIn('"', cleaned)
                self.assertNotIn("'", cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_previous_detection_is_preserved(self) -> None:
        for message in CREDENTIAL_MESSAGES:
            with self.subTest(message=message):
                self.assertNotIn(SECRET, sanitize_diagnostic_text(message))

    def test_safe_text_is_not_over_redacted(self) -> None:
        safe = SAFE_MESSAGES + (
            "run the suite with --verbose and --failfast",
            "the --help output lists token telemetry",
            "mytoken=value",
            "passwordless=value",
            "tokenizer.py and secret_handling.md are fine",
        )
        for message in safe:
            with self.subTest(message=message):
                self.assertEqual(message, sanitize_diagnostic_text(message))


class SharedCredentialPolicyTests(unittest.TestCase):
    """The command-line form joins the one canonical policy, not a fork."""

    def test_redact_credentials_covers_command_line_syntax(self) -> None:
        redacted, count = redact_credentials(f"claude --api-key {SECRET}")
        self.assertGreaterEqual(count, 1)
        self.assertNotIn(SECRET, redacted)
        self.assertTrue(credential_bearing(f"--token {SECRET}"))

    def test_the_existing_negative_cases_still_pass(self) -> None:
        for message in SAFE_MESSAGES:
            with self.subTest(message=message):
                redacted, count = redact_credentials(message)
                self.assertEqual(message, redacted)
                self.assertEqual(0, count)
                self.assertFalse(credential_bearing(message))


# --------------------------------------------------------------------------
# P1-C: malformed verifier results fail inside the verification boundary
# --------------------------------------------------------------------------


class _ForeignStatus(Enum):
    PASSED = "passed"


class MalformedVerifierConnector(DummyConnector):
    """Returns a chosen malformed value from ``verify``."""

    def __init__(self, payload: object, *, name: str = "bad-verifier", **kwargs):
        super().__init__(name=name, **kwargs)
        self.payload = payload

    def verify(self, task, result, session):
        return self.payload


def _record_with(status: object) -> VerificationRecord:
    """A VerificationRecord whose status bypassed the typed constructor."""
    record = VerificationRecord(
        status=VerificationStatus.PASSED, method="m", evidence=("e",)
    )
    object.__setattr__(record, "status", status)
    return record


class MalformedVerifierBoundaryTests(DiagnosticHygieneTestCase):
    MALFORMED = (
        None,
        {},
        {"status": "passed", "evidence": ["e"]},
        "passed",
        7,
        VerificationStatus.PASSED,
        _record_with("passed"),
        _record_with(_ForeignStatus.PASSED),
        _record_with(None),
    )

    def test_every_malformed_verifier_result_fails_deterministically(self) -> None:
        for payload in self.MALFORMED:
            with self.subTest(payload=repr(payload)[:40]):
                self.setUp()
                connector = MalformedVerifierConnector(payload)
                core = self.core(connector, self.store())
                run = core.run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(ErrorCode.VERIFICATION_FAILED, run.error.code)
                self.assertEqual("verify", run.error.details.get("stage"))
                # The connector ran exactly once and is never re-run.
                self.assertEqual(1, connector.execute_calls)
                self.assertIs(
                    AttemptState.FINISHED,
                    self.store().latest_execution_attempt(run.task.id).state,
                )
                fresh = MalformedVerifierConnector(payload)
                replay = self.core(fresh, self.store()).resume(
                    run.session.id, run.task.id
                )
                self.assertEqual(RunStatus.FAILED, replay.status)
                self.assertEqual(0, fresh.execute_calls)
                # No raw malformed value was persisted.
                self.assertNotIn("_ForeignStatus", self.raw_text())
                self.assertEqual([], self.store().list_decisions(run.session.id))

    def test_malformed_evidence_fails_inside_the_boundary(self) -> None:
        for evidence in (
            None,
            "not-a-sequence",
            ({"nested": SECRET},),
            (None,),
            ([SECRET],),
            (object(),),
        ):
            with self.subTest(evidence=repr(evidence)[:40]):
                self.setUp()
                record = VerificationRecord(
                    status=VerificationStatus.PASSED, method="m", evidence=("e",)
                )
                object.__setattr__(record, "evidence", evidence)
                connector = MalformedVerifierConnector(record)
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(ErrorCode.VERIFICATION_FAILED, run.error.code)
                self.assertEqual(1, connector.execute_calls)
                self.assert_no_leak()

    def test_h3_accounting_survives_a_malformed_verifier(self) -> None:
        connector = MalformedVerifierConnector(
            None, estimate_max=0.20, actual_cost=0.05
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.VERIFICATION_FAILED, run.error.code)
        store = self.store()
        self.assertAlmostEqual(0.05, store.monthly_monetary_cost())
        attempt = store.latest_execution_attempt(run.task.id)
        self.assertIs(AttemptState.FINISHED, attempt.state)
        record = store.attempt_billing_record(attempt.id)
        self.assertEqual("reconciled", record["state"])
        self.assertEqual(50_000_000, record["actual_nano_usd"])
        # And a restart neither re-executes nor re-charges.
        fresh = MalformedVerifierConnector(None, estimate_max=0.20, actual_cost=0.05)
        self.core(fresh, self.store()).resume(run.session.id, run.task.id)
        self.assertEqual(0, fresh.execute_calls)
        self.assertAlmostEqual(0.05, self.store().monthly_monetary_cost())

    def test_credential_bearing_verifier_evidence_never_persists(self) -> None:
        connector = MalformedVerifierConnector(
            VerificationRecord(
                status=VerificationStatus.PASSED,
                method=f"api_key: {SECRET}",
                evidence=tuple(f"--token {SECRET}" for _ in range(30)),
            )
        )
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertLessEqual(len(run.result.verification.evidence), VERIFICATION_MAX_EVIDENCE)
        self.assert_no_leak()
        self.assert_bounded()

    def test_a_connector_supplied_result_verification_is_never_fatal(self) -> None:
        # A malformed record attached to the connector's own ToolResult runs
        # after the connector boundary and before H3 reconciliation, so it is
        # discarded rather than raised - stranding the lifecycle would be worse.
        class BadResultVerification(DummyConnector):
            def execute(self, task, session):
                result = super().execute(task, session)
                object.__setattr__(result.verification, "status", "passed")
                return result

        connector = BadResultVerification(name="bad-result")
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)


# --------------------------------------------------------------------------
# P1-D: diagnostic details are total, bounded and persistence-safe
# --------------------------------------------------------------------------


class DiagnosticDetailBoundsTests(DiagnosticHygieneTestCase):
    def test_the_sanitizer_is_total(self) -> None:
        class Hostile:
            def __repr__(self) -> str:
                raise RuntimeError("no repr")

            def __eq__(self, other: object) -> bool:
                raise RuntimeError("no eq")

            def __hash__(self) -> int:
                return 0

        cases = (
            None,
            "not-a-mapping",
            7,
            [1, 2],
            (1, 2),
            {},
            {5: "int key"},
            {(1, 2): "tuple key"},
            {None: "none key"},
            {b"bytes": "bytes key"},
            {"ok": 1, 5: "mixed", None: "keys"},
            {"hostile": Hostile()},
            {"big": 10**5000},
            {"deep": {"a": {"b": {"c": 1}}}},
        )
        for case in cases:
            with self.subTest(case=type(case).__name__):
                details = sanitize_diagnostic_details(case)
                self.assertIsInstance(details, dict)
                # Always JSON-safe, always deterministic.
                self.assertEqual(
                    json.dumps(details, sort_keys=True),
                    json.dumps(sanitize_diagnostic_details(case), sort_keys=True),
                )

    def test_non_string_keys_are_dropped_fail_closed(self) -> None:
        details = sanitize_diagnostic_details(
            {5: "int", (1, 2): "tuple", None: "none", b"b": "bytes", "kept": 1}
        )
        self.assertEqual({"kept": 1}, details)

    def test_integers_outside_the_signed_range_are_dropped(self) -> None:
        # The labels avoid ``str(value)``: stringifying these integers is
        # itself what trips Python's JSON/int digit limit, which is exactly the
        # hazard the sanitizer exists to keep away from persistence.
        for label, value in (
            ("10**5000", 10**5000),
            ("int64_max + 1", DIAGNOSTIC_INT_MAX + 1),
            ("int64_min - 1", DIAGNOSTIC_INT_MIN - 1),
            ("-10**5000", -(10**5000)),
        ):
            with self.subTest(value=label):
                self.assertEqual({}, sanitize_diagnostic_details({"n": value}))

    def test_boundary_integers_are_kept(self) -> None:
        for value in (0, 1, -1, DIAGNOSTIC_INT_MAX, DIAGNOSTIC_INT_MIN):
            with self.subTest(value=value):
                self.assertEqual(
                    {"n": value}, sanitize_diagnostic_details({"n": value})
                )

    def test_collections_and_objects_are_dropped(self) -> None:
        self.assertEqual(
            {},
            sanitize_diagnostic_details(
                {
                    "t": (1, 2),
                    "l": [1],
                    "d": {"a": 1},
                    "s": {1, 2},
                    "b": b"bytes",
                    "o": object(),
                }
            ),
        )

    def test_non_finite_floats_are_dropped_and_finite_kept(self) -> None:
        self.assertEqual({}, sanitize_diagnostic_details({"n": float("nan")}))
        self.assertEqual({}, sanitize_diagnostic_details({"i": float("inf")}))
        self.assertEqual({}, sanitize_diagnostic_details({"j": float("-inf")}))
        self.assertEqual({"f": 1.5}, sanitize_diagnostic_details({"f": 1.5}))

    def test_huge_and_credential_strings_are_bounded_and_scrubbed(self) -> None:
        details = sanitize_diagnostic_details(
            {"huge": "H" * 200_000, "cred": f"--api-key {SECRET}"}
        )
        self.assertLessEqual(len(details["huge"]), DIAGNOSTIC_DETAIL_MAX_CHARS)
        self.assertNotIn(SECRET, details["cred"])

    def test_an_oversized_detail_reaches_a_safe_terminal_failure(self) -> None:
        # An exception carrying a hostile payload must still produce a clean
        # terminal failure with valid persisted JSON and intact H2/H3 state.
        class HugeIntError(RuntimeError):
            pass

        error = HugeIntError("boom")
        error.args = ("boom",)
        connector = StageRaiser("execute", "boom", estimate_max=0.20)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        details = StructuredError(
            ErrorCode.CONNECTOR_FAILED,
            "boom",
            details={"n": 10**5000, 5: "bad key", "d": {"x": 1}},
        )
        self.assertEqual({}, details.details)
        json.dumps(details.to_dict())
        # H2/H3 state is untouched by the diagnostic path.
        attempt = self.store().latest_execution_attempt(run.task.id)
        self.assertIs(AttemptState.FINISHED, attempt.state)
        self.assert_bounded()


# --------------------------------------------------------------------------
# P1-C: stage return validation lives inside each narrow boundary
# --------------------------------------------------------------------------


class _ForeignAvailability(Enum):
    AVAILABLE = "available"


class MalformedReturnConnector(DummyConnector):
    """Returns a chosen malformed value from one stage helper."""

    def __init__(
        self,
        where: str,
        payload: object,
        *,
        after_calls: int = 0,
        name: str = "malformed-return",
        **kwargs: object,
    ) -> None:
        super().__init__(name=name, **kwargs)
        self.where = where
        self.payload = payload
        self.after_calls = after_calls
        self.availability_calls = 0

    def availability(self):
        self.availability_calls += 1
        if self.where == "availability" and self.availability_calls > self.after_calls:
            return self.payload
        return super().availability()

    def permission_request(self, task, session_id, approval_ttl_minutes=30, **kwargs):
        if self.where == "permission_request":
            return self.payload
        return super().permission_request(
            task, session_id, approval_ttl_minutes, **kwargs
        )


def _broken_permission_request(**overrides) -> PermissionRequest:
    """A real PermissionRequest whose typed field was replaced after build."""
    connector = DummyConnector(name="probe")
    request = connector.permission_request(make_task(), "session-probe")
    for name, value in overrides.items():
        object.__setattr__(request, name, value)
    return request


def _valid_preview() -> OutboundDataPreview:
    """The exact preview contract, every field the type it declares."""
    return OutboundDataPreview(
        # The preview must name the SAME destination the scope carries, and
        # the scope's destination is the one Core registered.
        destination=CLAUDE_CODE_DESTINATION,
        data_classification=DataClassification.INTERNAL,
        field_names=("objective", "capability"),
        approximate_bytes=256,
        redacted_items=1,
        summary="redacted outbound preview",
    )


def _nested_broken_request(path: str, value: object) -> PermissionRequest:
    """A real PermissionRequest with ONE nested typed field replaced.

    ``path`` is dotted, e.g. ``"attestation.scope.data_classification"``. The
    connector shares one ``ActionScope`` and one ``SideEffectProfile`` between
    the request and its attestation, so the attestation is given private copies
    first: without that, a mutation aimed at the attestation would be caught by
    the request-level check instead and the nested rule would go untested.
    """
    connector = DummyConnector(name="probe")
    request = connector.permission_request(make_task(), "session-probe")
    object.__setattr__(request, "outbound_preview", _valid_preview())
    object.__setattr__(
        request,
        "attestation",
        replace(
            request.attestation,
            scope=replace(request.attestation.scope),
            side_effect=replace(request.attestation.side_effect),
        ),
    )
    target: object = request
    parts = path.split(".")
    for name in parts[:-1]:
        target = getattr(target, name)
    object.__setattr__(target, parts[-1], value)
    return request


def _retrieved_item(**overrides) -> RetrievedMemoryItem:
    """An EXACT RetrievedMemoryItem with one field replaced.

    Every case below is a real ``RetrievedMemoryItem`` instance: the outer type
    check passes, which is precisely why the nested fields have to be checked.
    """
    fields = {
        "kind": RetrievedMemoryKind.DECISION,
        "record_id": "decision-1",
        "source_task_id": "task-1",
        "summary": "a prior decision",
        "rationale": "because it was decided",
        "next_step": "continue",
        "classification": DataClassification.INTERNAL,
        "created_at": "2026-01-01T00:00:00+00:00",
        "relevance_score": 3,
        "truncated": False,
    }
    fields.update(overrides)
    return RetrievedMemoryItem(**fields)


def _retrieved_context(**overrides) -> RetrievedContext:
    """An EXACT RetrievedContext carrying one exact-but-malformed item."""
    fields = {
        "items": (_retrieved_item(**overrides.pop("item", {})),),
        "candidate_count": 1,
        "character_count": 32,
        "truncated": False,
    }
    fields.update(overrides)
    return RetrievedContext(**fields)


class StageReturnValidationTests(DiagnosticHygieneTestCase):
    """A malformed return value is a boundary failure, not an escaping error."""

    def assert_safe_boundary_failure(
        self, run, code: ErrorCode, stage: str, connector
    ) -> None:
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(code, run.error.code)
        self.assertEqual(stage, run.error.details.get("stage"))
        self.assertIs(TaskStatus.FAILED, run.task.status)
        self.assertIs(LoopStage.FAILED, run.task.stage)
        # The connector boundary was never crossed.
        self.assertEqual(0, connector.execute_calls)
        store = self.store()
        events = [item["event_type"] for item in store.list_events(run.session.id)]
        self.assertIn("task_failed", events)
        self.assertNotIn("task_completed", events)
        self.assertEqual([], store.list_decisions(run.session.id))
        self.assertIsNone(store.latest_execution_attempt(run.task.id))
        # Nothing raw was persisted, and the whole row stays bounded.
        self.assert_no_leak()
        self.assert_bounded()
        for marker in ("_ForeignAvailability", "object at 0x", "dict_keys"):
            self.assertNotIn(marker, self.raw_text())

    def assert_restart_does_not_repeat(self, run, connector_factory) -> None:
        """A terminal task reports; it never re-enters the broken boundary."""
        fresh = connector_factory()
        replay = self.core(fresh, self.store()).resume(run.session.id, run.task.id)
        self.assertEqual(RunStatus.FAILED, replay.status)
        self.assertEqual(run.error.code, replay.error.code)
        self.assertEqual(0, fresh.execute_calls)
        self.assertEqual(0, getattr(fresh, "availability_calls", 0))
        # And again: still terminal, still no work.
        again = self.core(connector_factory(), self.store()).resume(
            run.session.id, run.task.id
        )
        self.assertEqual(RunStatus.FAILED, again.status)

    # -- context retrieval --------------------------------------------------

    def test_malformed_retrieved_context_is_a_boundary_failure(self) -> None:
        for payload in (
            None,
            {},
            {"items": [], "candidate_count": 0},
            "retrieved",
            7,
            [],
            RetrievedContext(items=("not-an-item",)),
            RetrievedContext(candidate_count="many"),
            RetrievedContext(truncated="yes"),
        ):
            with self.subTest(payload=repr(payload)[:44]):
                self.setUp()
                connector = DummyConnector(name="retrieval")
                memory = self.store()
                core = self.core(connector, memory)
                with mock.patch.object(
                    memory, "retrieve_context", return_value=payload
                ):
                    run = core.run(make_task())
                self.assert_safe_boundary_failure(
                    run,
                    ErrorCode.INVALID_TASK,
                    LoopStage.CONTEXT_RETRIEVAL.value,
                    connector,
                )
                self.assertEqual(
                    "TypeError", run.error.details.get("exception_type")
                )
                self.assert_restart_does_not_repeat(
                    run, lambda: DummyConnector(name="retrieval")
                )

    def test_a_valid_retrieved_context_still_flows(self) -> None:
        connector = DummyConnector(name="retrieval")
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)

    # -- P1-1: nested retrieval fields --------------------------------------

    #: Every case is an EXACT ``RetrievedMemoryItem``/``RetrievedContext``, so
    #: an outer type check passes. Each one used to survive validation and then
    #: raise (``item.kind.value``) during ``to_dict`` - after the task had
    #: accepted the snapshot, outside the protected stage boundary.
    MALFORMED_RETRIEVALS = {
        # A raw string that compares EQUAL to the enum member but has no
        # ``.value``: the exact leak this rule exists to close.
        "raw string kind": {"item": {"kind": "decision"}},
        "raw string classification": {"item": {"classification": "internal"}},
        "foreign enum kind": {"item": {"kind": _ForeignStatus.PASSED}},
        "non-string record id": {"item": {"record_id": 7}},
        "non-string source task id": {"item": {"source_task_id": None}},
        "non-string summary": {"item": {"summary": 7}},
        "non-string rationale": {"item": {"rationale": None}},
        "non-string next step": {"item": {"next_step": 7}},
        "non-string created_at": {"item": {"created_at": None}},
        "bool relevance score": {"item": {"relevance_score": True}},
        "string relevance score": {"item": {"relevance_score": "3"}},
        "float relevance score": {"item": {"relevance_score": 1.5}},
        "negative relevance score": {"item": {"relevance_score": -1}},
        "oversized relevance score": {"item": {"relevance_score": 2**70}},
        "non-bool truncated": {"item": {"truncated": "yes"}},
        "oversized summary": {"item": {"summary": "S" * (RETRIEVAL_SUMMARY_CAP + 1)}},
        "oversized rationale": {
            "item": {"rationale": "R" * (RETRIEVAL_RATIONALE_CAP + 1)}
        },
        "negative candidate count": {"candidate_count": -1},
        "bool candidate count": {"candidate_count": True},
        "negative character count": {"character_count": -1},
        "oversized character count": {"character_count": 10_000},
        "oversized item count": {"items": tuple(
            _retrieved_item(record_id=f"decision-{index}")
            for index in range(RETRIEVAL_MAX_ITEMS + 1)
        )},
        "items is a list": {"items": [_retrieved_item()]},
    }

    def test_malformed_nested_retrieval_is_a_boundary_failure(self) -> None:
        for label, overrides in self.MALFORMED_RETRIEVALS.items():
            with self.subTest(case=label):
                self.setUp()
                connector = DummyConnector(name="retrieval", estimate_max=0.20)
                memory = self.store()
                core = self.core(connector, memory)
                payload = _retrieved_context(**overrides)
                with mock.patch.object(
                    memory, "retrieve_context", return_value=payload
                ) as probe:
                    # No exception escapes the stage boundary.
                    run = core.run(make_task())
                self.assertEqual(1, probe.call_count)
                self.assert_safe_boundary_failure(
                    run,
                    ErrorCode.INVALID_TASK,
                    LoopStage.CONTEXT_RETRIEVAL.value,
                    connector,
                )
                self.assertEqual(
                    "TypeError", run.error.details.get("exception_type")
                )
                # Nothing malformed was accepted as task state.
                self.assertEqual((), run.task.retrieved_context.items)
                store = self.store()
                self.assertEqual((), store.load_task(run.task.id)
                                 .retrieved_context.items)
                # Zero approval, grant, attempt, usage and execution authority.
                self.assertEqual([], self.raw_rows()["pending_approvals"])
                self.assertEqual([], self.raw_rows()["permission_grants"])
                self.assertEqual([], self.reservations(run.task.id))
                self.assertEqual([], store.usage_records())
                self.assertEqual(0.0, store.monthly_monetary_cost())
                # A restart reports the terminal state; the malformed boundary
                # is never invoked again.
                replay_store = self.store()
                with mock.patch.object(
                    replay_store, "retrieve_context", return_value=payload
                ) as replay_probe:
                    replay = self.core(
                        DummyConnector(name="retrieval", estimate_max=0.20),
                        replay_store,
                    ).resume(run.session.id, run.task.id)
                self.assertEqual(0, replay_probe.call_count)
                self.assertEqual(RunStatus.FAILED, replay.status)
                self.assertEqual(run.error.code, replay.error.code)

    def test_a_valid_retrieval_snapshot_with_items_still_flows(self) -> None:
        connector = DummyConnector(name="retrieval")
        memory = self.store()
        core = self.core(connector, memory)
        payload = _retrieved_context()
        with mock.patch.object(memory, "retrieve_context", return_value=payload):
            run = core.run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)
        # The exact typed snapshot survived intact and round-tripped.
        self.assertEqual(payload, run.task.retrieved_context)
        restored = self.store().load_task(run.task.id).retrieved_context
        self.assertEqual(payload.to_dict(), restored.to_dict())

    # -- P1-1: nested permission/preview/attestation fields -----------------

    #: Every case is an EXACT ``PermissionRequest``. Each used to pass the
    #: outer check and then raise on ``.value`` during fingerprint or display
    #: serialization - after a pending approval row would already exist.
    MALFORMED_PERMISSIONS = {
        "non-string task id": ("task_id", None),
        "non-string session id": ("session_id", 7),
        "raw string scope classification": (
            "scope.data_classification",
            "internal",
        ),
        "non-string scope action": ("scope.action", None),
        "non-string scope target": ("scope.target", 7),
        "non-string scope destination": ("scope.destination", 7),
        "raw string side effect": ("side_effect.effect", "none"),
        "non-bool reversible": ("side_effect.reversible", "yes"),
        "int approval flag": ("side_effect.always_requires_approval", 1),
        "raw string preview classification": (
            "outbound_preview.data_classification",
            "internal",
        ),
        "non-string preview destination": ("outbound_preview.destination", None),
        "preview field names is a list": (
            "outbound_preview.field_names",
            ["objective"],
        ),
        "non-string preview field name": ("outbound_preview.field_names", (7,)),
        "bool preview byte count": ("outbound_preview.approximate_bytes", True),
        "string preview byte count": ("outbound_preview.approximate_bytes", "256"),
        "negative preview byte count": ("outbound_preview.approximate_bytes", -1),
        "float preview redacted count": ("outbound_preview.redacted_items", 1.5),
        "oversized preview byte count": (
            "outbound_preview.approximate_bytes",
            2**70,
        ),
        "non-string preview summary": ("outbound_preview.summary", 7),
        "raw string attestation classification": (
            "attestation.scope.data_classification",
            "internal",
        ),
        "non-string attestation scope target": ("attestation.scope.target", 7),
        "int attestation risk level": ("attestation.risk_level", 3),
        "raw string attestation effect": ("attestation.side_effect.effect", "none"),
        "non-bool attestation reversible": (
            "attestation.side_effect.reversible",
            "yes",
        ),
        "non-string outbound digest": ("attestation.outbound_digest", None),
        "non-string billing gate": ("attestation.billing_gate", 7),
        "non-string cost gate": ("attestation.cost_gate", None),
        "non-string working directory": ("attestation.working_directory", 7),
        "allowed roots is a list": ("attestation.allowed_roots", ["c:/work"]),
        "non-string allowed root": ("attestation.allowed_roots", (7,)),
        "tool grants is a string": ("attestation.tool_grants", "read"),
        "non-string tool grant": ("attestation.tool_grants", (None,)),
    }

    def test_malformed_nested_permission_data_is_a_boundary_failure(self) -> None:
        for label, (path, value) in self.MALFORMED_PERMISSIONS.items():
            with self.subTest(case=label):
                self.setUp()
                payload = _nested_broken_request(path, value)
                connector = MalformedReturnConnector(
                    "permission_request", payload, estimate_max=0.20
                )
                # No exception escapes the stage boundary.
                run = self.core(connector, self.store()).run(make_task())
                self.assert_safe_boundary_failure(
                    run,
                    ErrorCode.EGRESS_REFUSED,
                    LoopStage.RISK_PERMISSION.value,
                    connector,
                )
                store = self.store()
                # Fail-closed BEFORE any pending approval or approval event.
                self.assertIsNone(
                    store.load_pending_approval(run.task.id, run.session.id)
                )
                self.assertEqual([], self.raw_rows()["pending_approvals"])
                self.assertEqual([], self.raw_rows()["permission_grants"])
                self.assertNotIn(
                    "approval_requested",
                    [item["event_type"] for item in store.list_events(run.session.id)],
                )
                # No execution, billing or ledger authority was created.
                self.assertEqual([], self.reservations(run.task.id))
                self.assertEqual([], store.usage_records())
                self.assertEqual(0.0, store.monthly_monetary_cost())
                self.assert_restart_does_not_repeat(
                    run,
                    lambda: MalformedReturnConnector("permission_request", payload),
                )

    def test_a_valid_preview_and_attestation_still_flow(self) -> None:
        class PreviewConnector(DummyConnector):
            def outbound_preview(self, task):
                return _valid_preview()

        connector = PreviewConnector(
            name="egress", side_effect=SideEffect.EXTERNAL_EGRESS
        )
        run = self.core(connector, self.store()).run(
            make_task(effect=SideEffect.EXTERNAL_EGRESS)
        )
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertEqual(0, connector.execute_calls)
        store = self.store()
        pending = store.load_pending_approval(run.task.id, run.session.id)
        self.assertIsNotNone(pending)
        # The exact typed preview and attestation reached the approval intact.
        self.assertEqual(_valid_preview(), pending.outbound_preview)
        self.assertIs(
            DataClassification.INTERNAL,
            pending.outbound_preview.data_classification,
        )
        self.assertIs(SideEffect.EXTERNAL_EGRESS, pending.attestation.side_effect.effect)
        self.assertIn(
            "approval_requested",
            [item["event_type"] for item in store.list_events(run.session.id)],
        )

    # -- permission request -------------------------------------------------

    def test_malformed_permission_request_is_a_boundary_failure(self) -> None:
        for payload in (
            None,
            {},
            {"task_id": "t", "fingerprint": "f"},
            "request",
            7,
            _broken_permission_request(risk_level=3),
            _broken_permission_request(scope={"action": "read"}),
            _broken_permission_request(side_effect={"effect": "none"}),
            _broken_permission_request(reason=None),
            _broken_permission_request(attestation={"outbound_digest": "x"}),
            _broken_permission_request(outbound_preview={"destination": "x"}),
            _broken_permission_request(id=""),
            _broken_permission_request(expires_at=None),
        ):
            with self.subTest(payload=repr(payload)[:44]):
                self.setUp()
                connector = MalformedReturnConnector("permission_request", payload)
                run = self.core(connector, self.store()).run(make_task())
                self.assert_safe_boundary_failure(
                    run,
                    ErrorCode.EGRESS_REFUSED,
                    LoopStage.RISK_PERMISSION.value,
                    connector,
                )
                # No approval row was opened with malformed data.
                self.assertIsNone(
                    self.store().load_pending_approval(run.task.id, run.session.id)
                )
                self.assertNotIn(
                    "approval_requested",
                    [
                        item["event_type"]
                        for item in self.store().list_events(run.session.id)
                    ],
                )
                self.assert_restart_does_not_repeat(
                    run,
                    lambda: MalformedReturnConnector("permission_request", payload),
                )

    # -- availability -------------------------------------------------------

    def test_malformed_availability_is_a_boundary_failure(self) -> None:
        for payload in (
            None,
            {},
            {"available": True},
            "available",
            True,
            _ForeignAvailability.AVAILABLE,
            Availability(available="yes"),
            Availability(available=True, reason=None),
        ):
            with self.subTest(payload=repr(payload)[:44]):
                self.setUp()
                # The registry probes availability once during selection, so
                # the malformed value is returned on the *second* call, which
                # is the TOOL_SELECTION boundary under test.
                connector = MalformedReturnConnector(
                    "availability", payload, after_calls=1
                )
                run = self.core(connector, self.store()).run(make_task())
                self.assert_safe_boundary_failure(
                    run,
                    ErrorCode.CONNECTOR_UNAVAILABLE,
                    LoopStage.TOOL_SELECTION.value,
                    connector,
                )
                self.assert_restart_does_not_repeat(
                    run,
                    lambda: MalformedReturnConnector(
                        "availability", payload, after_calls=1
                    ),
                )

    def test_a_genuinely_unavailable_tool_still_reports_normally(self) -> None:
        connector = DummyConnector(name="down", available=False)
        run = self.core(connector, self.store()).run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.CONNECTOR_UNAVAILABLE, run.error.code)
        self.assertEqual(0, connector.execute_calls)

    # -- H2/H3 authority is untouched by these boundaries -------------------

    def test_no_execution_authority_is_created_by_a_malformed_boundary(self) -> None:
        for where, payload, after in (
            ("permission_request", None, 0),
            ("availability", None, 1),
        ):
            with self.subTest(where=where):
                self.setUp()
                connector = MalformedReturnConnector(
                    where, payload, after_calls=after, estimate_max=0.20
                )
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                store = self.store()
                self.assertIsNone(store.latest_execution_attempt(run.task.id))
                self.assertEqual([], self.reservations(run.task.id))
                self.assertEqual([], store.usage_records())
                self.assertEqual(0.0, store.monthly_monetary_cost())


class MalformedSnapshotRestartTests(DiagnosticHygieneTestCase):
    """A tampered snapshot must not raise an unstructured error out of resume."""

    def staged_at(self, stage: LoopStage, snapshot: dict, connector):
        core = self.core(connector, self.store())
        session = core.start_session("tampered")
        task = make_task()
        task.selected_connector = connector.manifest.name
        task.stage = stage
        task.status = TaskStatus.VERIFYING
        task.result_snapshot = snapshot
        self.memory.save_task(session.id, task)
        return core, session, task

    def test_a_tampered_persist_snapshot_fails_deterministically(self) -> None:
        # A measured metered row with no amount would raise BillingIntegrityError
        # inside the terminal ledger write. It must become a structured failure.
        connector = DummyConnector(name="tampered", estimate_max=0.20)
        core, session, task = self.staged_at(
            LoopStage.PERSIST,
            {
                "outcome": "success",
                "summary": "restored",
                "usage": {
                    "billing_class": "metered_monetary",
                    "monetary_state": "measured",
                    "monetary_cost_usd": 0.05,
                    "id": "tampered-usage-id",
                },
                "verification": {
                    "status": "passed",
                    "method": "m",
                    "evidence": ["e"],
                },
            },
            connector,
        )
        run = core.resume(session.id, task.id)
        # Either it completes on a fully valid restored record, or it fails
        # deterministically - never an unstructured exception out of resume.
        self.assertIn(run.status, (RunStatus.COMPLETED, RunStatus.FAILED))
        if run.status is RunStatus.FAILED:
            self.assertIn(
                run.error.code,
                (
                    ErrorCode.BILLING_INTEGRITY_FAILED,
                    ErrorCode.VERIFICATION_FAILED,
                    ErrorCode.CONNECTOR_FAILED,
                ),
            )
        self.assertEqual(0, connector.execute_calls)
        self.assert_bounded()

    def test_resume_never_raises_for_any_tampered_snapshot(self) -> None:
        tampered = (
            {"outcome": "success", "summary": "x", "usage": {"call_count": True}},
            {"outcome": "success", "summary": "x", "usage": {"input_tokens": "7"}},
            {
                "outcome": "success",
                "summary": "x",
                "usage": {"billing_class": "bogus", "monetary_state": "bogus"},
            },
            {
                "outcome": "success",
                "summary": "x",
                "usage": {
                    "billing_class": "metered_monetary",
                    "monetary_state": "measured",
                    "monetary_cost_usd": None,
                },
            },
            {"outcome": f"api_key: {SECRET}", "summary": "x"},
            {"outcome": "success", "summary": "x", "verification": {"status": SECRET}},
            {"outcome": "success", "summary": "S" * 50_000},
            {"outcome": "success", "summary": "x", "usage": {"nested": {"a": 1}}},
        )
        for snapshot in tampered:
            with self.subTest(snapshot=str(sorted(snapshot))[:48]):
                self.setUp()
                connector = DummyConnector(name="tampered", estimate_max=0.20)
                core, session, task = self.staged_at(
                    LoopStage.VERIFY, snapshot, connector
                )
                run = core.resume(session.id, task.id)
                self.assertIn(run.status, (RunStatus.COMPLETED, RunStatus.FAILED))
                # No connector execution, no ledger movement, no leak.
                self.assertEqual(0, connector.execute_calls)
                store = self.store()
                self.assertEqual(0.0, store.monthly_monetary_cost())
                self.assertIsNone(store.latest_execution_attempt(task.id))
                self.assert_no_leak()
                self.assert_bounded()
                # A second resume is equally deterministic.
                again = self.core(
                    DummyConnector(name="tampered", estimate_max=0.20), self.store()
                ).resume(session.id, task.id)
                self.assertEqual(run.status, again.status)


# --------------------------------------------------------------------------
# P1-1: request <-> attestation <-> manifest semantic consistency
# --------------------------------------------------------------------------


#: A connector whose manifest owns a HIGH, irreversible, always-approved
#: external egress. Nothing it returns may present the operation as anything
#: weaker than that.
EGRESS_MANIFEST = {
    "risk": RiskLevel.HIGH,
    "side_effect": SideEffect.EXTERNAL_EGRESS,
    "reversible": False,
    "always_requires_approval": True,
}
#: A local manifest whose only non-weakenable property is the approval flag.
MANDATORY_MANIFEST = {
    "risk": RiskLevel.LOW,
    "side_effect": SideEffect.LOCAL_WRITE,
    "reversible": True,
    "always_requires_approval": True,
}
#: A local manifest whose only non-weakenable property is irreversibility.
IRREVERSIBLE_MANIFEST = {
    "risk": RiskLevel.LOW,
    "side_effect": SideEffect.LOCAL_WRITE,
    "reversible": False,
    "always_requires_approval": False,
}

#: Exactly the profile that would slip past SMART_AUTO unapproved.
_WEAK_PROFILE = SideEffectProfile(
    effect=SideEffect.NONE, reversible=True, always_requires_approval=False
)


def _overwrite(target: object, **fields: object) -> None:
    """Replace fields on an already-built frozen contract object."""
    for name, value in fields.items():
        object.__setattr__(target, name, value)


def _egress_task():
    return make_task(effect=SideEffect.EXTERNAL_EGRESS)


#: label -> (manifest kwargs, task factory, mutation applied to the request).
#: Every case returns a perfectly *typed* request; only the security
#: relationships between request, attestation and manifest are false.
SEMANTIC_DRIFT_CASES = {
    # 1. The attestation still says HIGH/EXTERNAL_EGRESS; the request the
    #    approval policy actually reads says LOW/NONE/reversible.
    "strong attestation, weakened request": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(
            request, risk_level=RiskLevel.LOW, side_effect=_WEAK_PROFILE
        ),
    ),
    # 2. Both sides agree - and both are below the manifest floor.
    "request and attestation both weakened": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: (
            _overwrite(request, risk_level=RiskLevel.LOW, side_effect=_WEAK_PROFILE),
            _overwrite(
                request.attestation,
                risk_level=RiskLevel.LOW,
                side_effect=_WEAK_PROFILE,
            ),
        ),
    ),
    # 3. The attestation covers a different scope than the request displays.
    "request scope differs from attestation scope": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(
            request.attestation,
            scope=replace(request.scope, target="somewhere-else"),
        ),
    ),
    # 4. Bound to another task / another session.
    "wrong task binding": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(request, task_id="some-other-task"),
    ),
    "wrong session binding": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(request, session_id="some-other-session"),
    ),
    # 5/6. The approval display describes a different egress than the one the
    #      request authorizes.
    "preview destination mismatch": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(
            request,
            outbound_preview=replace(_valid_preview(), destination="elsewhere"),
        ),
    ),
    "preview classification mismatch": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(
            request,
            outbound_preview=replace(
                _valid_preview(), data_classification=DataClassification.SENSITIVE
            ),
        ),
    ),
    # 7/8. Request and attestation disagree on risk / effect.
    "risk mismatch between request and attestation": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(
            request.attestation, risk_level=RiskLevel.CRITICAL
        ),
    ),
    "side effect mismatch between request and attestation": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(
            request.attestation,
            side_effect=SideEffectProfile(
                effect=SideEffect.DESTRUCTIVE,
                reversible=False,
                always_requires_approval=True,
            ),
        ),
    ),
    # 9. Connector-owned EXTERNAL_EGRESS presented as something local.
    "external egress downgraded to none": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(
            request,
            side_effect=SideEffectProfile(
                effect=SideEffect.NONE,
                reversible=False,
                always_requires_approval=True,
            ),
        ),
    ),
    "external egress downgraded to local read": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(
            request,
            side_effect=SideEffectProfile(
                effect=SideEffect.LOCAL_READ,
                reversible=False,
                always_requires_approval=True,
            ),
        ),
    ),
    # 10. Mandatory approval dropped.
    "mandatory approval weakened to optional": (
        MANDATORY_MANIFEST,
        make_task,
        lambda request: _overwrite(
            request,
            side_effect=SideEffectProfile(
                effect=SideEffect.LOCAL_WRITE,
                reversible=True,
                always_requires_approval=False,
            ),
        ),
    ),
    # 11. Irreversibility dropped.
    "irreversible weakened to reversible": (
        IRREVERSIBLE_MANIFEST,
        make_task,
        lambda request: _overwrite(
            request,
            side_effect=SideEffectProfile(
                effect=SideEffect.LOCAL_WRITE,
                reversible=True,
                always_requires_approval=False,
            ),
        ),
    ),
    # Risk alone, with everything else intact.
    "risk level below the manifest floor": (
        EGRESS_MANIFEST,
        _egress_task,
        lambda request: _overwrite(request, risk_level=RiskLevel.LOW),
    ),
}

#: A mismatch is a mismatch in every mode; none of them may become authority.
PERMISSION_MODES = (
    PermissionMode.STRICT,
    PermissionMode.SESSION,
    PermissionMode.SMART_AUTO,
)


class SemanticDriftConnector(DummyConnector):
    """Correctly typed, but its request contradicts its own manifest."""

    def __init__(self, mutate, *, preview: bool = True, **kwargs: object) -> None:
        super().__init__(name="drift", **kwargs)
        self.mutate = mutate
        self.preview = preview

    def outbound_preview(self, task):
        return _valid_preview() if self.preview else None

    def permission_request(self, task, session_id, approval_ttl_minutes=30, **kwargs):
        request = super().permission_request(
            task, session_id, approval_ttl_minutes, **kwargs
        )
        self.mutate(request)
        return request


class RequestAttestationSemanticsTests(DiagnosticHygieneTestCase):
    """A typed request is not an authoritative one.

    Field types say nothing about the security *relationships* between the
    request, its attestation and the connector manifest. A HIGH /
    EXTERNAL_EGRESS connector that returned a LOW / NONE / reversible request
    used to run straight through SMART_AUTO: task COMPLETED, one execute call,
    no permission request ever shown.
    """

    def assert_refused(self, connector, run) -> None:
        """Deterministic EGRESS_REFUSED with zero authority of any kind."""
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertIs(TaskStatus.FAILED, run.task.status)
        self.assertIs(LoopStage.FAILED, run.task.stage)
        # The connector boundary was never crossed.
        self.assertEqual(0, connector.execute_calls)
        store = self.store()
        # No approval was ever opened, displayed or granted.
        self.assertIsNone(store.load_pending_approval(run.task.id, run.session.id))
        self.assertEqual([], self.raw_rows()["pending_approvals"])
        self.assertEqual([], self.raw_rows()["permission_grants"])
        events = [item["event_type"] for item in store.list_events(run.session.id)]
        self.assertNotIn("approval_requested", events)
        self.assertNotIn("task_completed", events)
        self.assertIn("task_failed", events)
        # No execution, billing or decision authority.
        self.assertIsNone(store.latest_execution_attempt(run.task.id))
        self.assertEqual([], self.reservations(run.task.id))
        self.assertEqual([], store.usage_records())
        self.assertEqual(0.0, store.monthly_monetary_cost())
        self.assertEqual([], store.list_decisions(run.session.id))
        self.assert_no_leak()
        self.assert_bounded()

    def test_every_semantic_drift_is_refused_in_every_mode(self) -> None:
        for label, (manifest, task_factory, mutate) in SEMANTIC_DRIFT_CASES.items():
            for mode in PERMISSION_MODES:
                with self.subTest(case=label, mode=mode.value):
                    self.setUp()
                    connector = SemanticDriftConnector(mutate, **manifest)
                    run = self.core(connector, self.store(), mode=mode).run(
                        task_factory()
                    )
                    self.assert_refused(connector, run)

    def test_the_reported_regression_no_longer_executes(self) -> None:
        # The exact reported case: HIGH / EXTERNAL_EGRESS manifest, LOW / NONE
        # / reversible request, SMART_AUTO. It used to complete.
        connector = SemanticDriftConnector(
            lambda request: _overwrite(
                request, risk_level=RiskLevel.LOW, side_effect=_WEAK_PROFILE
            ),
            **EGRESS_MANIFEST,
        )
        run = self.core(
            connector, self.store(), mode=PermissionMode.SMART_AUTO
        ).run(_egress_task())
        self.assertNotEqual(RunStatus.COMPLETED, run.status)
        self.assert_refused(connector, run)

    def test_a_restart_never_re_enters_the_drifting_boundary(self) -> None:
        connector = SemanticDriftConnector(
            lambda request: _overwrite(request, side_effect=_WEAK_PROFILE),
            **EGRESS_MANIFEST,
        )
        run = self.core(connector, self.store()).run(_egress_task())
        self.assert_refused(connector, run)
        fresh = SemanticDriftConnector(
            lambda request: _overwrite(request, side_effect=_WEAK_PROFILE),
            **EGRESS_MANIFEST,
        )
        replay = self.core(fresh, self.store()).resume(run.session.id, run.task.id)
        self.assertEqual(RunStatus.FAILED, replay.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, replay.error.code)
        self.assertEqual(0, fresh.execute_calls)

    def test_an_exactly_matching_request_still_flows(self) -> None:
        # Nothing about a faithful connector changed: a mandatory egress still
        # reaches the approval gate with its preview and attestation intact.
        for mode in PERMISSION_MODES:
            with self.subTest(mode=mode.value):
                self.setUp()
                connector = SemanticDriftConnector(
                    lambda request: None, **EGRESS_MANIFEST
                )
                run = self.core(connector, self.store(), mode=mode).run(
                    _egress_task()
                )
                self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
                self.assertEqual(0, connector.execute_calls)
                store = self.store()
                pending = store.load_pending_approval(run.task.id, run.session.id)
                self.assertIsNotNone(pending)
                self.assertIs(RiskLevel.HIGH, pending.risk_level)
                self.assertIs(SideEffect.EXTERNAL_EGRESS, pending.side_effect.effect)
                self.assertEqual(_valid_preview(), pending.outbound_preview)
                self.assertIn(
                    "approval_requested",
                    [item["event_type"] for item in store.list_events(run.session.id)],
                )

    def test_a_low_risk_local_task_still_auto_approves(self) -> None:
        # The floor is a floor, not a new gate: a genuinely LOW, reversible,
        # non-mandatory operation is unchanged under SMART_AUTO.
        connector = SemanticDriftConnector(lambda request: None, preview=False)
        run = self.core(
            connector, self.store(), mode=PermissionMode.SMART_AUTO
        ).run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, connector.execute_calls)

    def test_a_stronger_than_floor_request_is_allowed(self) -> None:
        # Strengthening is legal; only weakening is refused. A LOW/reversible
        # manifest asked for HIGH and irreversible must still be honoured.
        connector = SemanticDriftConnector(
            lambda request: (
                _overwrite(
                    request,
                    risk_level=RiskLevel.CRITICAL,
                    side_effect=SideEffectProfile(
                        effect=SideEffect.LOCAL_WRITE,
                        reversible=False,
                        always_requires_approval=True,
                    ),
                ),
                _overwrite(
                    request.attestation,
                    risk_level=RiskLevel.CRITICAL,
                    side_effect=request.side_effect,
                ),
            ),
            preview=False,
            risk=RiskLevel.LOW,
            side_effect=SideEffect.LOCAL_WRITE,
        )
        run = self.core(
            connector, self.store(), mode=PermissionMode.SMART_AUTO
        ).run(make_task())
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertEqual(0, connector.execute_calls)
        pending = self.store().load_pending_approval(run.task.id, run.session.id)
        self.assertIs(RiskLevel.CRITICAL, pending.risk_level)


# --------------------------------------------------------------------------
# P1-2: an absolute path whose terminal segment contains a space
# --------------------------------------------------------------------------


#: Real absolute paths whose LAST segment contains a space. The earlier pattern
#: allowed spaces only in inner segments, so it stopped at the first word of the
#: terminal directory and left the second one next to the marker:
#: ``C:\Users\john.doe\My Documents`` -> ``[PATH] Documents``.
SPACED_PATH_CASES = (
    # Windows drive
    r"C:\Users\john.doe\My Documents",
    r"C:\Users\john.doe\Project Apollo\secret.txt",
    r"D:\Program Files\Project Apollo",
    # UNC
    "\\\\server\\share\\Project Apollo",
    "\\\\fileserver\\team share\\Project Apollo\\plan.docx",
    # POSIX
    "/home/john.doe/My Documents",
    "/home/john.doe/Project Apollo/secret.txt",
    "/Users/john.doe/Library/Application Support/Project Apollo",
)

#: Nothing identifying the account, the share, the project or the file may
#: survive next to the marker.
SPACED_PATH_FRAGMENTS = (
    "john.doe",
    "Documents",
    "Apollo",
    "Project",
    "share",
    "server",
    "Users",
    "Program Files",
    "Library",
    "Application",
    "secret.txt",
    "plan.docx",
)

#: Every quoting and boundary form the diagnostic can arrive in. None of these
#: wrappers contains a fragment, so any fragment found is a real leak.
PATH_WRAPPERS = (
    "{}",
    "'{}'",
    '"{}"',
    "failed at {}",
    "failed at {}.",
    "failed at {}, retrying",
    "could not open {}; giving up",
)


class SpacedAbsolutePathTests(unittest.TestCase):
    """An absolute path is redacted as one fixed marker, spaces included."""

    def test_no_path_fragment_survives_any_quoting_or_boundary(self) -> None:
        for path in SPACED_PATH_CASES:
            for wrapper in PATH_WRAPPERS:
                message = wrapper.format(path)
                with self.subTest(message=message):
                    cleaned = sanitize_diagnostic_text(message)
                    self.assertIn(REDACTED_PATH, cleaned)
                    for fragment in SPACED_PATH_FRAGMENTS:
                        self.assertNotIn(
                            fragment,
                            cleaned,
                            f"path fragment {fragment!r} survived in {cleaned!r}",
                        )

    def test_the_shared_path_policy_is_the_one_that_changed(self) -> None:
        # ``redact_local_paths`` is the single policy; the sanitizer is one of
        # its callers, not a second implementation.
        for path in SPACED_PATH_CASES:
            with self.subTest(path=path):
                self.assertEqual(REDACTED_PATH, redact_local_paths(path))

    def test_redaction_is_idempotent(self) -> None:
        for path in SPACED_PATH_CASES:
            for wrapper in PATH_WRAPPERS:
                message = wrapper.format(path)
                with self.subTest(message=message):
                    once = sanitize_diagnostic_text(message)
                    self.assertEqual(once, sanitize_diagnostic_text(once))

    def test_urls_and_safe_prose_are_untouched(self) -> None:
        # Over-redaction inside a path is the safe direction; misclassifying a
        # URL or ordinary prose as a path is not.
        for message in SAFE_MESSAGES + (
            "https://api.example.com/v1/messages timed out",
            "https://example.com/Project Apollo/status was 503",
            "read/write contention on the queue",
            "the plan and/or the assumption needs review",
            "compare a/b results before deciding",
        ):
            with self.subTest(message=message):
                self.assertEqual(message, sanitize_diagnostic_text(message))

    def test_the_existing_path_cases_still_hold(self) -> None:
        # The earlier space-free regressions must not weaken.
        self.assertEqual(
            f"crashed in {REDACTED_PATH}",
            sanitize_diagnostic_text(r"crashed in C:\Users\realk\JARVIS\jarvis\core.py"),
        )
        self.assertEqual(
            f"failed at {REDACTED_PATH}",
            sanitize_diagnostic_text(r"failed at C:\Users\john.doe"),
        )
        self.assertEqual(
            f"failed at {REDACTED_PATH}", sanitize_diagnostic_text("failed at /home/john.doe")
        )

    def test_matching_stays_bounded_on_adversarial_input(self) -> None:
        # Spaces inside a segment are bounded and every alternative is anchored
        # by a required separator, so a long run of space-separated words after
        # a slash cannot make the matcher explode.
        adversarial = (
            "/a/" + "b " * 4000,
            "C:\\a\\" + "b " * 4000,
            "\\\\a\\" + "b " * 4000,
            "/" + "a/" * 4000,
            "/a/b " + "c" * 100_000,
        )
        for message in adversarial:
            with self.subTest(message=message[:32]):
                started = time.perf_counter()
                cleaned = sanitize_diagnostic_text(message)
                elapsed = time.perf_counter() - started
                self.assertLess(elapsed, 5.0, "path matching did not stay bounded")
                self.assertLessEqual(len(cleaned), DIAGNOSTIC_MESSAGE_MAX_CHARS)


class SpacedAbsolutePathPersistenceTests(DiagnosticHygieneTestCase):
    """A spaced path must not reach SQLite, nor come back on a resave."""

    def test_spaced_paths_never_persist_and_survive_a_resave(self) -> None:
        for path in SPACED_PATH_CASES:
            with self.subTest(path=path):
                self.setUp()
                connector = StageRaiser("execute", f"open failed for {path}")
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assert_bounded()
                for fragment in SPACED_PATH_FRAGMENTS:
                    self.assertNotIn(fragment, self.raw_text())
                    self.assertNotIn(fragment, run.error.message)
                # Persist -> load -> resave -> reload reintroduces nothing.
                reloaded = self.resave_round_trip(run.session.id, run.task.id)
                encoded = json.dumps(reloaded.to_dict())
                for fragment in SPACED_PATH_FRAGMENTS:
                    self.assertNotIn(fragment, encoded)
                    self.assertNotIn(fragment, self.raw_text())


# --------------------------------------------------------------------------
# P1-3: an unterminated quoted CLI credential value
# --------------------------------------------------------------------------


#: Each case pairs a diagnostic with a unique tail marker. The quote never
#: closes, so the fallback to a single whitespace-delimited token used to stop
#: at the first space and publish the rest of the secret.
UNTERMINATED_CREDENTIALS = (
    (f'spawn --password "{SECRET} TAILSECRET_LEAK_9988', "TAILSECRET_LEAK_9988"),
    (f"spawn --password '{SECRET} TAILSECRET_LEAK_7711", "TAILSECRET_LEAK_7711"),
    (f'claude --api-key="{SECRET} TAILSECRET_LEAK_5522', "TAILSECRET_LEAK_5522"),
    (f"claude --api-key='{SECRET} TAILSECRET_LEAK_4433", "TAILSECRET_LEAK_4433"),
    (f"run --CLIENT_SECRET '{SECRET} TAILSECRET_LEAK_3344", "TAILSECRET_LEAK_3344"),
    (f'run --Client-Secret "{SECRET} TAILSECRET_LEAK_2255', "TAILSECRET_LEAK_2255"),
    (f'auth --access-token "{SECRET} TAILSECRET_LEAK_1166', "TAILSECRET_LEAK_1166"),
    (f"auth --access_token '{SECRET} TAILSECRET_LEAK_9900", "TAILSECRET_LEAK_9900"),
    (f'auth --token "{SECRET} TAILSECRET_LEAK_8811', "TAILSECRET_LEAK_8811"),
    (f'auth --api_key "{SECRET} TAILSECRET_LEAK_5544', "TAILSECRET_LEAK_5544"),
    (f'auth --session-cookie "{SECRET} TAILSECRET_LEAK_7722', "TAILSECRET_LEAK_7722"),
    (f"auth --session_cookie '{SECRET} TAILSECRET_LEAK_6633", "TAILSECRET_LEAK_6633"),
    (f'auth --Authorization "{SECRET} TAILSECRET_LEAK_1199', "TAILSECRET_LEAK_1199"),
    (f"auth --private-key '{SECRET} TAILSECRET_LEAK_2288", "TAILSECRET_LEAK_2288"),
)

#: A balanced quoted credential still ends at its closing quote, so genuinely
#: independent arguments after it survive. The space form is used because the
#: ``=`` form additionally matches the older assignment rule, whose value
#: deliberately runs to the end of the field.
BALANCED_CREDENTIALS = (
    (f'claude --password "{SECRET} TAILX" --print', "--print"),
    (f"claude --password '{SECRET} TAILX' --print", "--print"),
    (f'claude --access-token "{SECRET} TAILX" --verbose', "--verbose"),
    (f"claude --session-cookie '{SECRET} a b c' then", "then"),
)


class UnterminatedCredentialQuoteTests(unittest.TestCase):
    """Once a credential quote opens, the whole remaining value is secret."""

    def test_no_tail_survives_an_unterminated_quote(self) -> None:
        for text, marker in UNTERMINATED_CREDENTIALS:
            with self.subTest(text=text):
                cleaned = sanitize_diagnostic_text(text)
                self.assertNotIn(SECRET, cleaned)
                self.assertNotIn(marker, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_the_shared_policy_is_the_one_that_changed(self) -> None:
        for text, marker in UNTERMINATED_CREDENTIALS:
            with self.subTest(text=text):
                self.assertTrue(credential_bearing(text))
                redacted, count = redact_credentials(text)
                self.assertGreaterEqual(count, 1)
                self.assertNotIn(SECRET, redacted)
                self.assertNotIn(marker, redacted)

    def test_a_balanced_quote_still_ends_at_its_closing_quote(self) -> None:
        for text, survivor in BALANCED_CREDENTIALS:
            with self.subTest(text=text):
                cleaned = sanitize_diagnostic_text(text)
                self.assertNotIn(SECRET, cleaned)
                self.assertNotIn("TAILX", cleaned)
                self.assertIn("[REDACTED]", cleaned)
                self.assertIn(survivor, cleaned)

    def test_safe_text_is_still_not_over_redacted(self) -> None:
        for message in SAFE_MESSAGES + (
            "run the suite with --verbose and --failfast",
            "tokenizer.py and secret_handling.md are fine",
            "the --help output lists token telemetry",
            "see secret_handling.md before touching tokenizer.py",
        ):
            with self.subTest(message=message):
                self.assertEqual(message, sanitize_diagnostic_text(message))
                redacted, count = redact_credentials(message)
                self.assertEqual(message, redacted)
                self.assertEqual(0, count)

    def test_sanitization_is_idempotent(self) -> None:
        for text, _ in UNTERMINATED_CREDENTIALS + BALANCED_CREDENTIALS:
            with self.subTest(text=text):
                once = sanitize_diagnostic_text(text)
                self.assertEqual(once, sanitize_diagnostic_text(once))

    def test_the_structured_error_contract_carries_nothing(self) -> None:
        for text, marker in UNTERMINATED_CREDENTIALS:
            with self.subTest(text=text):
                error = StructuredError(
                    ErrorCode.CONNECTOR_FAILED, text, details={"note": text}
                )
                encoded = json.dumps(error.to_dict())
                self.assertNotIn(SECRET, encoded)
                self.assertNotIn(marker, encoded)
                # A persisted diagnostic reloads byte-identically.
                restored = StructuredError.from_dict(error.to_dict())
                self.assertEqual(error.to_dict(), restored.to_dict())


class UnterminatedCredentialPersistenceTests(DiagnosticHygieneTestCase):
    """The tail of an unterminated credential never reaches SQLite."""

    def test_unterminated_credentials_never_persist(self) -> None:
        for text, marker in UNTERMINATED_CREDENTIALS:
            with self.subTest(text=text):
                self.setUp()
                connector = StageRaiser("execute", text)
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(ErrorCode.CONNECTOR_FAILED, run.error.code)
                self.assertNotIn(marker, run.error.message)
                self.assertNotIn(marker, self.raw_text())
                self.assert_no_leak()
                self.assert_bounded()
                # Persist -> load -> resave -> reload reintroduces nothing.
                reloaded = self.resave_round_trip(run.session.id, run.task.id)
                encoded = json.dumps(reloaded.to_dict())
                self.assertNotIn(SECRET, encoded)
                self.assertNotIn(marker, encoded)
                self.assertNotIn(marker, self.raw_text())
                # And a second sanitization of what was stored is a no-op.
                stored = reloaded.result_snapshot["error"]["message"]
                self.assertEqual(stored, sanitize_diagnostic_text(stored))


# --------------------------------------------------------------------------
# P1-2: a safety bound must not become the visible end of a path
# --------------------------------------------------------------------------


#: A terminal component with more words than any per-segment ceiling, and a
#: chain with more components than any segment-count ceiling. Both used to end
#: the match early and leave the remainder beside the marker
#: (``[PATH] TAILMARK`` / ``[PATH]seg17/seg18/TAILMARK``).
_LONG_COMPONENT = " ".join(f"word{index}" for index in range(1, 15))
_DEEP_POSIX = "/".join(f"seg{index}" for index in range(24))
_DEEP_WINDOWS = "\\".join(f"seg{index}" for index in range(24))

UNBOUNDED_PATH_CASES = (
    # 10+ word final component
    f"/home/john.doe/{_LONG_COMPONENT} TAILMARK",
    f"C:\\Users\\john.doe\\{_LONG_COMPONENT} TAILMARK",
    f"\\\\server\\share\\{_LONG_COMPONENT} TAILMARK",
    # >16 path segments
    f"/{_DEEP_POSIX}/TAILMARK",
    f"C:\\{_DEEP_WINDOWS}\\TAILMARK",
    f"\\\\server\\{_DEEP_WINDOWS}\\TAILMARK",
    # both at once
    f"/{_DEEP_POSIX}/{_LONG_COMPONENT} TAILMARK",
    f"C:\\{_DEEP_WINDOWS}\\{_LONG_COMPONENT} TAILMARK",
)

#: Nothing from inside a recognized path may survive, at any depth or length.
UNBOUNDED_PATH_FRAGMENTS = (
    "TAILMARK",
    "john.doe",
    "word9",
    "word10",
    "word14",
    "seg16",
    "seg17",
    "seg23",
    "share",
    "server",
    "Users",
)


class UnboundedAbsolutePathTests(unittest.TestCase):
    """Parser ceilings bound the work, never the visible end of a path."""

    def test_no_suffix_survives_past_any_internal_ceiling(self) -> None:
        for path in UNBOUNDED_PATH_CASES:
            for wrapper in PATH_WRAPPERS:
                message = wrapper.format(path)
                with self.subTest(message=message[:60]):
                    cleaned = sanitize_diagnostic_text(message)
                    self.assertIn(REDACTED_PATH, cleaned)
                    for fragment in UNBOUNDED_PATH_FRAGMENTS:
                        self.assertNotIn(
                            fragment,
                            cleaned,
                            f"path fragment {fragment!r} survived in {cleaned!r}",
                        )

    def test_the_shared_scanner_collapses_the_whole_path(self) -> None:
        for path in UNBOUNDED_PATH_CASES:
            with self.subTest(path=path[:60]):
                self.assertEqual(REDACTED_PATH, redact_local_paths(path))

    def test_quoted_forms_consume_through_the_matching_quote(self) -> None:
        for path in UNBOUNDED_PATH_CASES:
            for quote in ("'", '"'):
                with self.subTest(path=path[:40], quote=quote):
                    # Closed: the whole quoted argument goes, quotes included.
                    closed = sanitize_diagnostic_text(
                        f"opening {quote}{path}{quote} then"
                    )
                    self.assertEqual(f"opening {REDACTED_PATH} then", closed)
                    # Unclosed: fail closed over the rest of the field.
                    unclosed = sanitize_diagnostic_text(f"opening {quote}{path}")
                    self.assertEqual(f"opening {REDACTED_PATH}", unclosed)

    def test_redaction_is_idempotent(self) -> None:
        for path in UNBOUNDED_PATH_CASES:
            for wrapper in PATH_WRAPPERS:
                with self.subTest(path=path[:40]):
                    once = sanitize_diagnostic_text(wrapper.format(path))
                    self.assertEqual(once, sanitize_diagnostic_text(once))

    def test_urls_and_prose_are_still_untouched(self) -> None:
        for message in SAFE_MESSAGES + (
            "https://api.example.com/v1/messages timed out",
            "https://example.com/Project Apollo/status was 503",
            "read/write contention on the queue",
            "the plan and/or the assumption needs review",
            "compare a/b results before deciding",
            "ratio was 50/50 across the board",
            "see section 3/4 of the doc",
        ):
            with self.subTest(message=message):
                self.assertEqual(message, sanitize_diagnostic_text(message))

    def test_a_lone_separator_is_still_not_a_path(self) -> None:
        # The POSIX root needs a second separator, so a bare word is prose.
        for message in ("/tmp is the target", "use /flag to enable it"):
            with self.subTest(message=message):
                self.assertEqual(message, sanitize_diagnostic_text(message))

    def test_the_scanner_is_linear_on_large_input(self) -> None:
        # No repeated-regex backtracking and no quadratic rescan: each of these
        # is far past any previous ceiling.
        adversarial = (
            "/" + "a/" * 60_000,
            "/a/" + "b " * 60_000,
            "/x " * 60_000,
            "C:\\a\\" + "b " * 60_000,
            "\\\\a\\" + "b/" * 60_000,
            ("C:\\a\\b /c/d \\\\e\\f " * 20_000),
            "no separators here at all " * 20_000,
        )
        for message in adversarial:
            with self.subTest(message=message[:24]):
                started = time.perf_counter()
                cleaned = redact_local_paths(message)
                elapsed = time.perf_counter() - started
                self.assertLess(elapsed, 5.0, "path scanning did not stay linear")
                self.assertEqual(cleaned, redact_local_paths(cleaned))


class UnboundedAbsolutePathPersistenceTests(DiagnosticHygieneTestCase):
    """Nothing past a parser ceiling reaches SQLite, or comes back on a resave."""

    def test_deep_and_long_paths_never_persist(self) -> None:
        for path in UNBOUNDED_PATH_CASES:
            with self.subTest(path=path[:60]):
                self.setUp()
                connector = StageRaiser("execute", f"open failed for {path}")
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assert_bounded()
                for fragment in UNBOUNDED_PATH_FRAGMENTS:
                    self.assertNotIn(fragment, run.error.message)
                    self.assertNotIn(fragment, self.raw_text())
                reloaded = self.resave_round_trip(run.session.id, run.task.id)
                encoded = json.dumps(reloaded.to_dict())
                for fragment in UNBOUNDED_PATH_FRAGMENTS:
                    self.assertNotIn(fragment, encoded)
                    self.assertNotIn(fragment, self.raw_text())


# --------------------------------------------------------------------------
# P1-3: an escaped quote inside a credential value does not close it
# --------------------------------------------------------------------------


#: ``\"``/``\'`` inside the value used to be read as the closing quote, handing
#: back everything after it. Each case carries a unique tail marker.
ESCAPED_QUOTE_CREDENTIALS = (
    # double quote, escaped inner quote, proper final close
    (f'run --password "{SECRET} foo\\"bar TAILSECRET_LEAK_42" --print', "TAILSECRET_LEAK_42", "--print"),
    (f'run --api-key="{SECRET} foo\\"bar TAILSECRET_LEAK_43" --print', "TAILSECRET_LEAK_43", None),
    # double quote, escaped inner quote, never closed
    (f'run --password "{SECRET} foo\\"bar TAILSECRET_LEAK_44', "TAILSECRET_LEAK_44", None),
    (f'run --api-key="{SECRET} foo\\"bar TAILSECRET_LEAK_45', "TAILSECRET_LEAK_45", None),
    # single quote, escaped inner quote, proper final close
    (f"run --password '{SECRET} foo\\'bar TAILSECRET_LEAK_46' --print", "TAILSECRET_LEAK_46", "--print"),
    (f"run --api-key='{SECRET} foo\\'bar TAILSECRET_LEAK_47' --print", "TAILSECRET_LEAK_47", None),
    # single quote, escaped inner quote, never closed
    (f"run --password '{SECRET} foo\\'bar TAILSECRET_LEAK_48", "TAILSECRET_LEAK_48", None),
    (f"run --api-key='{SECRET} foo\\'bar TAILSECRET_LEAK_49", "TAILSECRET_LEAK_49", None),
    # mixed-case flags, both joiners
    (f'run --PASSWORD "{SECRET} foo\\"bar TAILSECRET_LEAK_50" --verbose', "TAILSECRET_LEAK_50", "--verbose"),
    (f"run --Client_Secret '{SECRET} foo\\'bar TAILSECRET_LEAK_51' --verbose", "TAILSECRET_LEAK_51", "--verbose"),
    (f'run --Access-Token "{SECRET} foo\\"bar TAILSECRET_LEAK_52" --print', "TAILSECRET_LEAK_52", "--print"),
    (f"run --SESSION_COOKIE '{SECRET} foo\\'bar TAILSECRET_LEAK_53' --print", "TAILSECRET_LEAK_53", "--print"),
    (f'run --token "{SECRET} foo\\"bar\\"baz TAILSECRET_LEAK_54" --print', "TAILSECRET_LEAK_54", "--print"),
    # a flag named inside the secret is part of the secret, not a new value
    (f'run --secret "{SECRET} --token inner TAILSECRET_LEAK_55" --print', "TAILSECRET_LEAK_55", "--print"),
)


class EscapedQuoteCredentialTests(unittest.TestCase):
    """An escaped quote belongs to the secret; only an unescaped one closes."""

    def test_no_tail_survives_an_escaped_inner_quote(self) -> None:
        for text, marker, survivor in ESCAPED_QUOTE_CREDENTIALS:
            with self.subTest(text=text[:64]):
                cleaned = sanitize_diagnostic_text(text)
                self.assertNotIn(SECRET, cleaned)
                self.assertNotIn(marker, cleaned)
                self.assertNotIn("bar", cleaned)
                self.assertIn("[REDACTED]", cleaned)
                if survivor is not None:
                    # A balanced value ends at its real closing quote, so a
                    # genuinely independent argument still survives.
                    self.assertIn(survivor, cleaned)

    def test_the_shared_policy_is_the_one_that_changed(self) -> None:
        for text, marker, _ in ESCAPED_QUOTE_CREDENTIALS:
            with self.subTest(text=text[:64]):
                self.assertTrue(credential_bearing(text))
                redacted, count = redact_credentials(text)
                self.assertGreaterEqual(count, 1)
                self.assertNotIn(SECRET, redacted)
                self.assertNotIn(marker, redacted)

    def test_sanitization_is_idempotent(self) -> None:
        for text, _, _ in ESCAPED_QUOTE_CREDENTIALS:
            with self.subTest(text=text[:64]):
                once = sanitize_diagnostic_text(text)
                self.assertEqual(once, sanitize_diagnostic_text(once))

    def test_safe_negatives_are_preserved(self) -> None:
        for message in SAFE_MESSAGES + (
            "run the suite with --verbose and --failfast",
            "tokenizer.py and secret_handling.md are fine",
            "the --help output lists token telemetry",
            "mytoken=value",
            "passwordless=value",
            r"the regex uses \" to escape a quote",
            "he said the token design is fine",
        ):
            with self.subTest(message=message):
                self.assertEqual(message, sanitize_diagnostic_text(message))
                redacted, count = redact_credentials(message)
                self.assertEqual(message, redacted)
                self.assertEqual(0, count)

    def test_the_previous_credential_regressions_still_hold(self) -> None:
        for text, marker in UNTERMINATED_CREDENTIALS:
            with self.subTest(text=text[:48]):
                cleaned = sanitize_diagnostic_text(text)
                self.assertNotIn(SECRET, cleaned)
                self.assertNotIn(marker, cleaned)
        for text, survivor in BALANCED_CREDENTIALS:
            with self.subTest(text=text[:48]):
                cleaned = sanitize_diagnostic_text(text)
                self.assertNotIn(SECRET, cleaned)
                self.assertNotIn("TAILX", cleaned)
                self.assertIn(survivor, cleaned)


class EscapedQuoteCredentialPersistenceTests(DiagnosticHygieneTestCase):
    """The tail after an escaped quote never reaches SQLite."""

    def test_escaped_quote_credentials_never_persist(self) -> None:
        for text, marker, _ in ESCAPED_QUOTE_CREDENTIALS:
            with self.subTest(text=text[:64]):
                self.setUp()
                connector = StageRaiser("execute", text)
                run = self.core(connector, self.store()).run(make_task())
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(ErrorCode.CONNECTOR_FAILED, run.error.code)
                self.assertNotIn(marker, run.error.message)
                self.assertNotIn(marker, self.raw_text())
                self.assert_no_leak()
                self.assert_bounded()
                # Persist -> load -> resave -> reload reintroduces nothing.
                reloaded = self.resave_round_trip(run.session.id, run.task.id)
                encoded = json.dumps(reloaded.to_dict())
                self.assertNotIn(SECRET, encoded)
                self.assertNotIn(marker, encoded)
                self.assertNotIn(marker, self.raw_text())
                stored = reloaded.result_snapshot["error"]["message"]
                self.assertEqual(stored, sanitize_diagnostic_text(stored))

    def test_the_structured_error_contract_carries_nothing(self) -> None:
        for text, marker, _ in ESCAPED_QUOTE_CREDENTIALS:
            with self.subTest(text=text[:64]):
                error = StructuredError(
                    ErrorCode.CONNECTOR_FAILED, text, details={"note": text}
                )
                encoded = json.dumps(error.to_dict())
                self.assertNotIn(SECRET, encoded)
                self.assertNotIn(marker, encoded)
                restored = StructuredError.from_dict(error.to_dict())
                self.assertEqual(error.to_dict(), restored.to_dict())


# --------------------------------------------------------------------------
# H4 x M3: a local read adds a new content path, and it must stay out of
# every diagnostic surface.
# --------------------------------------------------------------------------


class LocalReadDiagnosticHygieneTests(unittest.TestCase):
    MARKER = "FILE-CONTENT-MARKER-4c81"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.memory = SQLiteMemoryStore(self.root / "jarvis.db")
        self.connector = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.core = open_core(
            self._registry(),
            self.memory,
            Settings(
                memory_database=self.memory.database_path,
                permission_mode=PermissionMode.SESSION,
            ),
            # The shipped local-read tuple, written out rather than borrowed:
            # this fixture composes the real connector, so it declares the
            # real row.
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        "local-file-read", "file_read", CLAUDE_CODE_DESTINATION
                    )
                }
            ),
        )

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(self.connector)
        return registry

    def _task(self, target: Path) -> Task:
        from jarvis.contracts import ActionScope

        return Task(
            objective="read the file",
            capability="file_read",
            scope=ActionScope(
                action="read",
                target=str(target),
                data_classification=DataClassification.INTERNAL,
            ),
        )

    def test_a_denied_file_is_never_read_so_nothing_can_leak(self) -> None:
        denied = self.root / ".env"
        denied.write_text(f"TOKEN={self.MARKER}\n", encoding="utf-8")
        run = self.core.run(self._task(denied))
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        stored = self.memory.load_task(run.task.id)
        everything = json.dumps(
            {
                "task": stored.to_dict(),
                "events": self.memory.list_events(run.session.id),
                "error": run.error.to_dict(),
            },
            default=str,
        )
        self.assertNotIn(self.MARKER, everything)

    def test_a_refusal_message_carries_no_absolute_path(self) -> None:
        missing = self.root / "definitely-absent.txt"
        run = self.core.run(self._task(missing))
        self.assertEqual(RunStatus.FAILED, run.status)
        # ``StructuredError`` construction is the sanitization boundary: any
        # absolute path in connector text collapses to the fixed marker.
        self.assertNotIn(str(self.root), json.dumps(run.error.to_dict()))

    def test_read_content_reaches_the_typed_field_and_nowhere_else(self) -> None:
        target = self.root / "notes.txt"
        target.write_text(f"{self.MARKER}\n", encoding="utf-8")
        run = self.core.run(self._task(target))
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        stored = self.memory.load_task(run.task.id)
        self.assertIn(self.MARKER, stored.local_read.excerpt)
        # Exactly one occurrence in the whole persisted task document.
        self.assertEqual(1, json.dumps(stored.to_dict()).count(self.MARKER))
        # And none at all in the diagnostic/audit surfaces.
        self.assertNotIn(
            self.MARKER,
            json.dumps(self.memory.list_events(run.session.id), default=str),
        )
        self.assertNotIn(
            self.MARKER, json.dumps(sanitize_result_snapshot(stored.result_snapshot))
        )
        self.assertNotIn(
            self.MARKER,
            json.dumps(
                [d.to_dict() for d in self.memory.list_decisions(run.session.id)]
            ),
        )

    def test_verification_evidence_is_metadata_only(self) -> None:
        target = self.root / "notes.txt"
        target.write_text(f"{self.MARKER}\n", encoding="utf-8")
        run = self.core.run(self._task(target))
        stored = self.memory.load_task(run.task.id)
        evidence = json.dumps(stored.result_snapshot["verification"])
        self.assertNotIn(self.MARKER, evidence)
        self.assertIn(stored.local_read.sha256, evidence)


if __name__ == "__main__":
    unittest.main()
