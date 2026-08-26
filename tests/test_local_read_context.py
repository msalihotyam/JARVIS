"""M8 - Bounded Multi-File Read Context.

What this file pins, end to end:

    1..3 completed same-session file_read tasks -> context.local_read_task_ids
    -> durable read-context identity -> CONTEXT_RETRIEVAL all-or-nothing
    resolution -> ONE frozen ordered bundle -> H1-bound external egress

plus everything that must NOT happen: a declared bundle that silently becomes
a context-LESS model request, a partial subset reaching the model, a source
re-read after the freeze, a transitively copied snapshot passing as a
producer, source task ids leaving the machine, and any change to the M3
singular hop.

The consumer is a provider-neutral ``PlanningConnector`` declaring
EXTERNAL_EGRESS; the producer is the real M5/M3 ``LocalFileReadConnector`` on
a temp root. No live model, connector, auth or network call is ever made.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from jarvis.app import (
    OUTPUT_SCHEMA_VERSION,
    TaskInputError,
    build_envelope,
    create_app,
    decode_task_json,
)
from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.connectors.claude_code import (
    EGRESS_TOOL_GRANTS,
    LOCAL_READ_CONTEXT_EGRESS_REFUSAL,
    SECRET_EGRESS_REFUSAL,
    ClaudeCodeConnector,
)
from jarvis.connectors.local_files import LocalFileReadConnector
from jarvis.contracts import (
    LOCAL_READ_CONTEXT_MAX_CHARS,
    LOCAL_READ_CONTEXT_REFERENCE_KEY,
    LOCAL_READ_EXCERPT_MAX_CHARS,
    LOCAL_READ_MAX_SOURCES,
    LOCAL_READ_SOURCE_MAX_CHARS,
    LOCAL_READ_TASK_REFERENCE_KEY,
    RETRIEVAL_MAX_CHARACTERS,
    RETRIEVAL_MAX_ITEMS,
    ActionScope,
    AgentRun,
    AttestationError,
    BillingClass,
    DataClassification,
    ErrorCode,
    LocalReadContextKind,
    LocalReadSnapshot,
    LoopStage,
    MonetaryState,
    OutcomeStatus,
    PermissionMode,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    SideEffectProfile,
    Task,
    TaskStatus,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
    local_read_context_refusal,
    local_read_reference_refusal,
)
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import Settings
from tests.helpers import safe_cleanup

#: Well-formed by the reference grammar, never a real task.
OTHER_ID = "1" * 36
READ_AT = "2026-01-01T00:00:00Z"


def snapshot(
    path: str,
    excerpt: str,
    classification: DataClassification = DataClassification.INTERNAL,
) -> LocalReadSnapshot:
    """A structurally valid snapshot, built the way a real read would."""
    return LocalReadSnapshot(
        relative_path=path,
        byte_size=len(excerpt.encode("utf-8")),
        sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        excerpt=excerpt,
        classification=classification,
        read_at=READ_AT,
    )


class PlanningConnector(BaseConnector):
    """Provider-neutral EXTERNAL_EGRESS consumer. Never touches a network.

    It stands in for the real Claude connector wherever the loop, not the
    payload, is under test, so no subprocess is ever a possibility.
    """

    def __init__(self, name: str = "test-planner") -> None:
        self.manifest = ConnectorManifest(
            name=name,
            capabilities=("code_analysis", "code_planning"),
            priority=50,
            default_risk=RiskLevel.LOW,
            default_side_effect=SideEffectProfile(effect=SideEffect.EXTERNAL_EGRESS),
            default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
        )
        self.seen: list[tuple[str, ...]] = []

    def canonical_outbound(self, task: Task) -> bytes:
        return json.dumps(
            {
                "objective": task.objective,
                "sources": [
                    {
                        "path": item.relative_path,
                        "byte_size": item.byte_size,
                        "truncated": item.truncated,
                        "content": item.excerpt,
                    }
                    for item in task.local_read_sources
                ],
            },
            sort_keys=True,
        ).encode("utf-8")

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        self.seen.append(tuple(item.relative_path for item in task.local_read_sources))
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary="planned",
            data={"next_step": "done"},
            usage=UsageRecord(
                provider="test-provider",
                model="test-model",
                connector=self.manifest.name,
                billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
                monetary_state=MonetaryState.NOT_APPLICABLE,
                call_count=1,
            ),
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        return VerificationRecord(
            status=VerificationStatus.PASSED,
            method="test_planner_check",
            evidence=("deterministic-test-evidence",),
        )


class LateLocalConnector(BaseConnector):
    """A NON-egress connector that only claims the task AFTER the freeze.

    The exact hostile shape the consumer policy exists for: ``can_handle`` is
    a function of the whole task, so this answers False while
    ``local_read_sources`` is empty and True once the bundle is frozen. A
    policy evaluated before the freeze would never see it; ``registry.select``
    at PLAN would. Its priority is high enough to win the route outright, and
    its effect is NONE - which under SMART_AUTO would not even require an
    approval - so if it is ever selected the excerpts reach a connector that
    attests no egress at all.
    """

    def __init__(self, name: str = "late-local") -> None:
        self.manifest = ConnectorManifest(
            name=name,
            capabilities=("code_analysis", "code_planning"),
            priority=999,
            default_risk=RiskLevel.LOW,
            default_side_effect=SideEffectProfile(effect=SideEffect.NONE),
            default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
        )
        self.execute_calls = 0
        self.saw: list[tuple[str, ...]] = []

    def can_handle(self, task: Task) -> bool:
        return bool(task.local_read_sources)

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        self.execute_calls += 1
        self.saw.append(tuple(item.relative_path for item in task.local_read_sources))
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary="late-local ran",
            data={"next_step": "done"},
            usage=UsageRecord(
                provider="test-provider",
                model="test-model",
                connector=self.manifest.name,
                billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
                monetary_state=MonetaryState.NOT_APPLICABLE,
                call_count=1,
            ),
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        return VerificationRecord(
            status=VerificationStatus.PASSED,
            method="late_local_check",
            evidence=("deterministic-test-evidence",),
        )


class LocalReadContextBase(unittest.TestCase):
    """A real SQLite store, a real local-read producer, a fake consumer."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.database = str(self.root / "jarvis.db")
        self.settings = Settings(
            memory_database=self.database,
            permission_mode=PermissionMode.SMART_AUTO,
        )
        self.memory = SQLiteMemoryStore(self.database)
        self.planner = PlanningConnector()
        self.reader = LocalFileReadConnector(allowed_roots=[self.root])
        self.app = create_app(
            settings=self.settings,
            memory=self.memory,
            connectors=(self.planner, self.reader),
        )
        self.session_id: str | None = None

    # -- fixtures ---------------------------------------------------------
    def write_file(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return path

    def complete_read(
        self,
        name: str,
        body: str = "alpha content\n",
        classification: DataClassification = DataClassification.INTERNAL,
    ) -> str:
        """Run a REAL local read to COMPLETED and return its task id."""
        self.write_file(name, body)
        document = {
            "objective": f"read {name}",
            "capability": "file_read",
            "scope": {
                "action": "read",
                "target": str(self.root / name),
                "data_classification": classification.value,
            },
            "context": {"working_directory": str(self.root)},
        }
        payload = self.app.run(
            json.dumps(document), session_id=self.session_id
        ).payload
        self.session_id = payload["session"]["id"]
        self.assertEqual("completed", payload["task"]["status"], payload.get("error"))
        return payload["task"]["id"]

    def consumer_document(
        self,
        ids: list[str] | Any,
        *,
        capability: str = "code_analysis",
        classification: DataClassification = DataClassification.INTERNAL,
        extra_context: dict[str, Any] | None = None,
    ) -> str:
        context: dict[str, Any] = {"working_directory": str(self.root)}
        if ids is not None:
            context[LOCAL_READ_CONTEXT_REFERENCE_KEY] = ids
        context.update(extra_context or {})
        return json.dumps(
            {
                "objective": "analyse the referenced files",
                "capability": capability,
                "scope": {
                    "action": "analyse",
                    "target": "repo",
                    "data_classification": classification.value,
                },
                "context": context,
            }
        )

    def run_consumer(self, ids: list[str] | Any, **kwargs: Any) -> dict[str, Any]:
        payload = self.app.run(
            self.consumer_document(ids, **kwargs), session_id=self.session_id
        ).payload
        self.session_id = payload["session"]["id"]
        return payload

    def approve(self, payload: dict[str, Any]) -> dict[str, Any]:
        approval = payload["approval"]
        return self.app.approve(
            session_id=payload["session"]["id"],
            task_id=payload["task"]["id"],
            request_id=approval["request_id"],
            fingerprint=approval["fingerprint"],
        ).payload

    # -- persisted-row tampering -----------------------------------------
    def query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
        """Raw SQLite on purpose: the product's own writers would re-apply the
        very validation these tests exist to exercise."""
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()

    def raw_state(self, task_id: str) -> dict[str, Any]:
        rows = self.query("SELECT state_json FROM tasks WHERE id = ?", (task_id,))
        self.assertTrue(rows)
        return json.loads(rows[0][0])

    def write_state(self, task_id: str, state: dict[str, Any]) -> None:
        """Edit a persisted task row the way something outside the product would."""
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE tasks SET state_json = ?, status = ?, stage = ? WHERE id = ?",
                (
                    json.dumps(state),
                    state.get("status", "pending"),
                    state.get("stage", "understand"),
                    task_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def tamper(self, task_id: str, mutate: Any) -> None:
        state = self.raw_state(task_id)
        mutate(state)
        self.write_state(task_id, state)


# ==========================================================================
# INPUT
# ==========================================================================
class InputContractTests(LocalReadContextBase):
    def decode(self, ids: Any, **kwargs: Any) -> Task:
        return decode_task_json(self.consumer_document(ids, **kwargs))

    def test_three_references_are_accepted_in_order(self) -> None:
        task = self.decode(["a", "b", "c"])
        self.assertIs(LocalReadContextKind.REFERENCES, task.local_read_context_kind)
        self.assertEqual(("a", "b", "c"), task.local_read_source_task_ids)
        self.assertEqual((), task.local_read_sources)

    def test_one_reference_stays_plural(self) -> None:
        task = self.decode(["only"])
        self.assertIs(LocalReadContextKind.REFERENCES, task.local_read_context_kind)
        self.assertEqual(("only",), task.local_read_source_task_ids)

    def test_references_never_remain_in_task_context(self) -> None:
        task = self.decode(["a", "b"])
        self.assertNotIn(LOCAL_READ_CONTEXT_REFERENCE_KEY, task.context)
        self.assertNotIn(LOCAL_READ_TASK_REFERENCE_KEY, task.context)

    def test_empty_list_is_refused(self) -> None:
        with self.assertRaises(TaskInputError):
            self.decode([])

    def test_four_references_are_refused(self) -> None:
        with self.assertRaises(TaskInputError):
            self.decode(["a", "b", "c", "d"])

    def test_non_list_shapes_are_refused(self) -> None:
        for value in ("ab", {"a": 1}, 3, True):
            with self.subTest(value=value), self.assertRaises(TaskInputError):
                self.decode(value)

    def test_a_bare_string_is_not_iterated_into_characters(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode("abc")
        self.assertIn("must be a list", str(caught.exception))

    def test_non_string_elements_are_refused(self) -> None:
        for value in (1, None, True, ["nested"], {"k": "v"}):
            with self.subTest(value=value), self.assertRaises(TaskInputError):
                self.decode([value])

    def test_blank_padded_nul_and_oversized_references_are_refused(self) -> None:
        for value in ("", " a", "a ", "\ta", "a\n", "a" * (LOCAL_READ_SOURCE_MAX_CHARS + 1), "a\x00b"):
            with self.subTest(value=repr(value)), self.assertRaises(TaskInputError):
                self.decode([value])

    def test_a_maximum_length_reference_is_accepted(self) -> None:
        reference = "a" * LOCAL_READ_SOURCE_MAX_CHARS
        self.assertEqual((reference,), self.decode([reference]).local_read_source_task_ids)

    def test_padding_is_refused_not_stripped(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode([" spaced"])
        self.assertIn("surrounding whitespace", str(caught.exception))

    def test_duplicate_references_are_refused(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode(["a", "b", "a"])
        self.assertIn("same task twice", str(caught.exception))

    def test_singular_and_plural_together_are_refused(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode(["a"], extra_context={LOCAL_READ_TASK_REFERENCE_KEY: "b"})
        self.assertIn("exactly one of", str(caught.exception))

    def test_an_empty_singular_alongside_a_plural_is_still_refused(self) -> None:
        """The XOR is on key PRESENCE, not on truthiness."""
        with self.assertRaises(TaskInputError):
            self.decode(["a"], extra_context={LOCAL_READ_TASK_REFERENCE_KEY: ""})

    def test_an_unknown_context_key_is_still_refused(self) -> None:
        with self.assertRaises(TaskInputError):
            self.decode(["a"], extra_context={"local_read_task_idz": "x"})

    def test_unsupported_consumer_capability_is_refused_by_the_loop(self) -> None:
        source = self.complete_read("a.txt")
        payload = self.run_consumer([source], capability="file_read")
        self.assertEqual("failed", payload["task"]["status"])
        self.assertEqual(ErrorCode.INVALID_TASK.value, payload["error"]["code"])

    def test_a_capability_no_connector_serves_is_refused(self) -> None:
        source = self.complete_read("a.txt")
        payload = self.run_consumer([source], capability="not_a_capability")
        self.assertEqual("failed", payload["task"]["status"])
        self.assertEqual(ErrorCode.INVALID_TASK.value, payload["error"]["code"])
        self.assertIn("no registered connector", payload["error"]["message"])


# ==========================================================================
# DURABLE IDENTITY / LOAD
# ==========================================================================
class DurableIdentityTests(unittest.TestCase):
    def base_state(self) -> dict[str, Any]:
        return Task(
            objective="o",
            capability="code_analysis",
            scope=ActionScope(action="analyse"),
        ).to_dict()

    def kind_of(self, state: dict[str, Any]) -> LocalReadContextKind:
        return Task.from_dict(state).local_read_context_kind

    def test_a_genuine_legacy_row_loads_as_none(self) -> None:
        """ONLY the complete absence of M8 state is a pre-M8 row."""
        state = self.base_state()
        for key in (
            "local_read_context_kind",
            "local_read_source_task_ids",
            "local_read_sources",
        ):
            state.pop(key, None)
        task = Task.from_dict(state)
        self.assertIs(LocalReadContextKind.NONE, task.local_read_context_kind)
        self.assertEqual((), task.local_read_source_task_ids)
        self.assertEqual((), task.local_read_sources)

    def test_kind_key_deleted_with_references_retained_is_invalid(self) -> None:
        state = self.base_state()
        state.pop("local_read_context_kind")
        state["local_read_source_task_ids"] = ["a", "b"]
        self.assertIs(LocalReadContextKind.INVALID, self.kind_of(state))

    def test_kind_key_deleted_post_freeze_is_invalid(self) -> None:
        state = self.base_state()
        state.pop("local_read_context_kind")
        state["local_read_source_task_ids"] = ["a"]
        state["local_read_sources"] = [snapshot("a.txt", "alpha\n").to_dict()]
        self.assertIs(LocalReadContextKind.INVALID, self.kind_of(state))

    def test_garbage_kinds_are_invalid(self) -> None:
        for value in (5, None, True, [], {}, "", "REFERENCES", "ref", "None"):
            state = self.base_state()
            state["local_read_context_kind"] = value
            with self.subTest(value=value):
                self.assertIs(LocalReadContextKind.INVALID, self.kind_of(state))

    def test_references_downgraded_to_none_with_state_is_invalid(self) -> None:
        state = self.base_state()
        state["local_read_context_kind"] = "none"
        state["local_read_source_task_ids"] = ["a"]
        self.assertIs(LocalReadContextKind.INVALID, self.kind_of(state))

    def test_none_carrying_frozen_sources_is_invalid(self) -> None:
        state = self.base_state()
        state["local_read_context_kind"] = "none"
        state["local_read_sources"] = [snapshot("a.txt", "alpha\n").to_dict()]
        self.assertIs(LocalReadContextKind.INVALID, self.kind_of(state))

    def test_missing_or_malformed_ids_under_references_are_invalid(self) -> None:
        for value in (None, "a", {"a": 1}, [1], [None], ["a", "a"], ["", "b"], [" a"]):
            state = self.base_state()
            state["local_read_context_kind"] = "references"
            if value is None:
                state.pop("local_read_source_task_ids", None)
            else:
                state["local_read_source_task_ids"] = value
            with self.subTest(value=value):
                self.assertIs(LocalReadContextKind.INVALID, self.kind_of(state))

    def test_malformed_frozen_source_invalidates_the_whole_bundle(self) -> None:
        """One bad element is never dropped from the tuple."""
        good = snapshot("a.txt", "alpha\n").to_dict()
        bad = dict(good)
        bad["sha256"] = "not-a-digest"
        state = self.base_state()
        state["local_read_context_kind"] = "references"
        state["local_read_source_task_ids"] = ["a", "b"]
        state["local_read_sources"] = [good, bad]
        task = Task.from_dict(state)
        self.assertIs(LocalReadContextKind.INVALID, task.local_read_context_kind)
        self.assertEqual((), task.local_read_sources)

    def test_non_list_frozen_sources_are_invalid(self) -> None:
        state = self.base_state()
        state["local_read_context_kind"] = "references"
        state["local_read_source_task_ids"] = ["a"]
        state["local_read_sources"] = {"a": 1}
        self.assertIs(LocalReadContextKind.INVALID, self.kind_of(state))

    def test_count_mismatch_is_invalid(self) -> None:
        state = self.base_state()
        state["local_read_context_kind"] = "references"
        state["local_read_source_task_ids"] = ["a", "b"]
        state["local_read_sources"] = [snapshot("a.txt", "alpha\n").to_dict()]
        self.assertIs(LocalReadContextKind.INVALID, self.kind_of(state))

    def test_pre_and_post_resolution_shapes_both_load(self) -> None:
        state = self.base_state()
        state["local_read_context_kind"] = "references"
        state["local_read_source_task_ids"] = ["a", "b"]
        self.assertIs(LocalReadContextKind.REFERENCES, self.kind_of(state))
        state["local_read_sources"] = [
            snapshot("a.txt", "alpha\n").to_dict(),
            snapshot("b.txt", "beta\n").to_dict(),
        ]
        self.assertIs(LocalReadContextKind.REFERENCES, self.kind_of(state))

    def test_invalid_survives_repeated_round_trips(self) -> None:
        state = self.base_state()
        state["local_read_context_kind"] = "invalid"
        task = Task.from_dict(state)
        for _ in range(3):
            self.assertIs(LocalReadContextKind.INVALID, task.local_read_context_kind)
            self.assertEqual((), task.local_read_source_task_ids)
            self.assertEqual((), task.local_read_sources)
            task = Task.from_dict(task.to_dict())

    def test_a_raw_string_kind_serializes_as_invalid(self) -> None:
        task = Task(
            objective="o",
            capability="code_analysis",
            scope=ActionScope(action="analyse"),
        )
        task.local_read_context_kind = "references"  # type: ignore[assignment]
        self.assertEqual("invalid", task.to_dict()["local_read_context_kind"])

    def test_the_shared_contract_rejects_non_tuple_collections(self) -> None:
        self.assertIsNotNone(
            local_read_context_refusal(LocalReadContextKind.REFERENCES, ["a"], ())
        )
        self.assertIsNotNone(
            local_read_context_refusal(LocalReadContextKind.REFERENCES, ("a",), [])
        )

    def test_the_reference_grammar_is_exact(self) -> None:
        self.assertIsNone(local_read_reference_refusal("a"))
        for value in ("", " a", "a ", "a" * 65, "a\x00", 1, None, True):
            with self.subTest(value=repr(value)):
                self.assertIsNotNone(local_read_reference_refusal(value))


# ==========================================================================
# SOURCE ELIGIBILITY / PRODUCER GATES
# ==========================================================================
class SourceGateTests(LocalReadContextBase):
    def assert_refused(self, payload: dict[str, Any], fragment: str = "") -> Task:
        self.assertEqual("failed", payload["task"]["status"])
        self.assertEqual(ErrorCode.INVALID_TASK.value, payload["error"]["code"])
        if fragment:
            self.assertIn(fragment, payload["error"]["message"])
        task = self.memory.load_task(payload["task"]["id"])
        self.assertEqual((), task.local_read_sources)
        self.assertIsNone(task.selected_connector)
        self.assertEqual([], self.planner.seen)
        return task

    def test_a_missing_source_is_refused(self) -> None:
        self.complete_read("a.txt")
        self.assert_refused(self.run_consumer([OTHER_ID]))

    def test_a_self_reference_is_refused(self) -> None:
        """Unreachable from input (the id is minted after decode), so the
        task is built programmatically the way the loop would see a tampered
        or machine-generated one."""
        self.complete_read("a.txt")
        task = Task(
            objective="analyse itself",
            capability="code_analysis",
            scope=ActionScope(
                action="analyse",
                target="repo",
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
            local_read_context_kind=LocalReadContextKind.REFERENCES,
        )
        task.local_read_source_task_ids = (task.id,)
        run = self.app.core.run(task, session_id=self.session_id)
        self.assertIs(RunStatus.FAILED, run.status)
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("its own local read", run.error.message)
        self.assertEqual((), self.memory.load_task(task.id).local_read_sources)
        self.assertEqual([], self.planner.seen)

    def test_a_foreign_session_source_is_refused(self) -> None:
        first = self.complete_read("a.txt")
        self.session_id = None  # a brand new session
        self.complete_read("b.txt")
        self.assert_refused(
            self.run_consumer([first]), "another session"
        )

    def test_an_incomplete_source_is_refused(self) -> None:
        source = self.complete_read("a.txt")
        self.tamper(source, lambda state: state.update(status="running"))
        self.assert_refused(self.run_consumer([source]), "did not complete")

    def test_a_failed_source_is_refused(self) -> None:
        source = self.complete_read("a.txt")
        self.tamper(source, lambda state: state.update(status="failed"))
        self.assert_refused(self.run_consumer([source]), "did not complete")

    def test_a_cancelled_source_is_refused(self) -> None:
        source = self.complete_read("a.txt")
        self.tamper(source, lambda state: state.update(status="cancelled"))
        self.assert_refused(self.run_consumer([source]), "did not complete")

    def test_a_wrong_capability_producer_is_refused(self) -> None:
        source = self.complete_read("a.txt")
        self.tamper(source, lambda state: state.update(capability="code_analysis"))
        self.assert_refused(self.run_consumer([source]), "not a local read")

    def test_an_unregistered_producer_connector_is_refused(self) -> None:
        source = self.complete_read("a.txt")
        self.tamper(
            source, lambda state: state.update(selected_connector="ghost-connector")
        )
        self.assert_refused(self.run_consumer([source]), "not a registered connector")

    def test_a_producer_without_the_read_capability_is_refused(self) -> None:
        """Capability forged to file_read, connector forged to the planner."""
        source = self.complete_read("a.txt")
        self.tamper(
            source,
            lambda state: state.update(selected_connector=self.planner.manifest.name),
        )
        self.assert_refused(
            self.run_consumer([source]), "does not perform local reads"
        )

    def test_a_source_naming_no_producer_is_refused(self) -> None:
        source = self.complete_read("a.txt")
        self.tamper(source, lambda state: state.update(selected_connector=None))
        self.assert_refused(self.run_consumer([source]), "names no producer")

    def test_a_transitively_copied_snapshot_is_refused(self) -> None:
        """A model task that itself carries a local read is not a producer.

        This is the headline forgery: the M3 singular hop copies a snapshot
        onto its CONSUMER, so a completed code_analysis task really does hold
        a structurally perfect LocalReadSnapshot.
        """
        source = self.complete_read("a.txt")
        singular = json.dumps(
            {
                "objective": "analyse one file",
                "capability": "code_analysis",
                "scope": {"action": "analyse", "target": "repo"},
                "context": {
                    "working_directory": str(self.root),
                    LOCAL_READ_TASK_REFERENCE_KEY: source,
                },
            }
        )
        payload = self.app.run(singular, session_id=self.session_id).payload
        payload = self.approve(payload)
        self.assertEqual("completed", payload["task"]["status"])
        carrier = self.memory.load_task(payload["task"]["id"])
        self.assertTrue(carrier.local_read.present)  # the forgery material
        self.planner.seen.clear()
        self.assert_refused(self.run_consumer([carrier.id]), "not a local read")

    def test_a_malformed_source_snapshot_is_refused(self) -> None:
        source = self.complete_read("a.txt")

        def break_snapshot(state: dict[str, Any]) -> None:
            state["local_read"]["sha256"] = "0"

        self.tamper(source, break_snapshot)
        self.assert_refused(self.run_consumer([source]))

    def test_a_source_with_no_snapshot_is_refused(self) -> None:
        source = self.complete_read("a.txt")

        def clear_snapshot(state: dict[str, Any]) -> None:
            state["local_read"] = LocalReadSnapshot().to_dict()

        self.tamper(source, clear_snapshot)
        self.assert_refused(self.run_consumer([source]), "carries no local read")

    def test_the_persisted_snapshot_classification_is_never_trusted(self) -> None:
        """A downgraded label on a SENSITIVE source must not buy eligibility."""
        source = self.complete_read(
            "a.txt", classification=DataClassification.SENSITIVE
        )

        def downgrade(state: dict[str, Any]) -> None:
            state["local_read"]["classification"] = DataClassification.PUBLIC.value

        self.tamper(source, downgrade)
        self.assert_refused(
            self.run_consumer([source], classification=DataClassification.PUBLIC),
            "classification ladder",
        )

    def test_the_frozen_snapshot_carries_the_rebound_classification(self) -> None:
        """The rebind is what the CONNECTOR later reads, so pin it directly.

        The ladder call already reads the source task's own scope, so a
        downgraded persisted label cannot buy loop-level eligibility. Its real
        prize is the frozen snapshot: without the rebind a tampered PUBLIC
        label would travel into the bundle and the egress boundary - which
        checks ``snapshot.classification`` - would believe it.
        """
        source = self.complete_read(
            "s.txt", classification=DataClassification.SENSITIVE
        )

        def downgrade(state: dict[str, Any]) -> None:
            state["local_read"]["classification"] = DataClassification.PUBLIC.value

        self.tamper(source, downgrade)
        self.assertIs(
            DataClassification.PUBLIC,
            self.memory.load_task(source).local_read.classification,
        )
        payload = self.run_consumer(
            [source], classification=DataClassification.SENSITIVE
        )
        self.assertEqual("awaiting_approval", payload["task"]["status"])
        frozen = self.memory.load_task(payload["task"]["id"]).local_read_sources
        self.assertEqual(1, len(frozen))
        self.assertIs(DataClassification.SENSITIVE, frozen[0].classification)

    def test_the_ladder_allows_equal_and_lower_source_classifications(self) -> None:
        public = self.complete_read(
            "p.txt", classification=DataClassification.PUBLIC
        )
        internal = self.complete_read(
            "i.txt", classification=DataClassification.INTERNAL
        )
        payload = self.run_consumer(
            [public, internal], classification=DataClassification.INTERNAL
        )
        self.assertEqual("awaiting_approval", payload["task"]["status"])

    def test_a_more_restrictive_source_is_refused(self) -> None:
        source = self.complete_read(
            "s.txt", classification=DataClassification.SENSITIVE
        )
        self.assert_refused(
            self.run_consumer([source], classification=DataClassification.INTERNAL),
            "classification ladder",
        )

    def test_a_secret_source_is_refused(self) -> None:
        source = self.complete_read("a.txt")
        self.tamper(
            source,
            lambda state: state["scope"].update(
                data_classification=DataClassification.SECRET.value
            ),
        )
        self.assert_refused(
            self.run_consumer([source], classification=DataClassification.SECRET)
        )

    def test_a_secret_consumer_is_refused(self) -> None:
        source = self.complete_read("a.txt")
        self.assert_refused(
            self.run_consumer([source], classification=DataClassification.SECRET),
            "classification ladder",
        )


# ==========================================================================
# ALL-OR-NOTHING RESOLUTION
# ==========================================================================
class AtomicResolutionTests(LocalReadContextBase):
    def test_one_bad_source_leaves_the_bundle_completely_empty(self) -> None:
        first = self.complete_read("a.txt")
        second = self.complete_read("b.txt")
        third = self.complete_read("c.txt")
        self.tamper(third, lambda state: state.update(status="running"))
        payload = self.run_consumer([first, second, third])

        self.assertEqual("failed", payload["task"]["status"])
        self.assertEqual(ErrorCode.INVALID_TASK.value, payload["error"]["code"])
        task = self.memory.load_task(payload["task"]["id"])
        # No partial bundle survived, in memory or in storage.
        self.assertEqual((), task.local_read_sources)
        self.assertEqual([], self.raw_state(task.id)["local_read_sources"])
        # And nothing downstream of CONTEXT_RETRIEVAL ever happened.
        self.assertIsNone(task.selected_connector)
        self.assertIsNone(payload["approval"])
        self.assertEqual([], self.planner.seen)
        self.assertEqual(
            0,
            self.query(
                "SELECT COUNT(*) FROM execution_attempts WHERE task_id = ?",
                (task.id,),
            )[0][0],
        )
        self.assertEqual(
            0,
            self.query(
                "SELECT COUNT(*) FROM permission_grants WHERE task_id = ?",
                (task.id,),
            )[0][0],
        )
        self.assertEqual([], self.memory.execution_attempts(task.id))
        self.assertIsNone(
            self.memory.load_pending_approval(task.id, payload["session"]["id"])
        )

    def test_a_bad_first_source_also_freezes_nothing(self) -> None:
        first = self.complete_read("a.txt")
        second = self.complete_read("b.txt")
        self.tamper(first, lambda state: state.update(status="running"))
        payload = self.run_consumer([first, second])
        self.assertEqual("failed", payload["task"]["status"])
        self.assertEqual(
            (), self.memory.load_task(payload["task"]["id"]).local_read_sources
        )


# ==========================================================================
# BUDGET / ORDER
# ==========================================================================
class BudgetAndOrderTests(LocalReadContextBase):
    def test_three_maximum_excerpts_are_exactly_at_budget(self) -> None:
        body = "x" * (LOCAL_READ_EXCERPT_MAX_CHARS * 2)
        ids = [
            self.complete_read(f"big{index}.txt", body=body) for index in range(3)
        ]
        payload = self.run_consumer(ids)
        self.assertEqual("awaiting_approval", payload["task"]["status"])
        task = self.memory.load_task(payload["task"]["id"])
        total = sum(len(item.excerpt) for item in task.local_read_sources)
        self.assertEqual(LOCAL_READ_CONTEXT_MAX_CHARS, total)

    def test_multibyte_excerpts_are_counted_in_code_points(self) -> None:
        body = "漢" * (LOCAL_READ_EXCERPT_MAX_CHARS * 2)
        ids = [
            self.complete_read(f"cjk{index}.txt", body=body) for index in range(3)
        ]
        payload = self.run_consumer(ids)
        self.assertEqual("awaiting_approval", payload["task"]["status"])
        task = self.memory.load_task(payload["task"]["id"])
        total = sum(len(item.excerpt) for item in task.local_read_sources)
        self.assertEqual(LOCAL_READ_CONTEXT_MAX_CHARS, total)
        # Code points, not bytes: the same bundle is far larger in UTF-8.
        self.assertGreater(
            sum(len(item.excerpt.encode("utf-8")) for item in task.local_read_sources),
            LOCAL_READ_CONTEXT_MAX_CHARS,
        )

    def test_the_budget_boundary_is_inclusive(self) -> None:
        ids = ("a", "b", "c")
        at_budget = tuple(
            snapshot(f"{name}.txt", "x" * 4000) for name in ids
        )
        self.assertIsNone(
            local_read_context_refusal(
                LocalReadContextKind.REFERENCES, ids, at_budget
            )
        )

    def test_an_over_long_excerpt_is_refused_by_its_own_ceiling(self) -> None:
        """Honest boundary: with 3 sources capped at 4000 each, the per-source
        ceiling is what an over-long excerpt hits. The 12000 bundle ceiling is
        DEFENSIVE and unreachable while those two constants hold - it exists so
        that raising LOCAL_READ_MAX_SOURCES cannot silently widen the bundle.
        """
        ids = ("a", "b", "c")
        over = (
            snapshot("a.txt", "x" * 4000),
            snapshot("b.txt", "x" * 4000),
            snapshot("c.txt", "x" * (LOCAL_READ_EXCERPT_MAX_CHARS + 1)),
        )
        reason = local_read_context_refusal(
            LocalReadContextKind.REFERENCES, ids, over
        )
        self.assertIsNotNone(reason)
        self.assertIn("frozen source is malformed", reason)

    def test_the_bundle_ceiling_binds_once_the_source_ceiling_is_raised(
        self,
    ) -> None:
        """The defensive bound, exercised the only way it is reachable."""
        ids = ("a", "b", "c", "d")
        four = tuple(
            snapshot(f"{name}.txt", "x" * 4000) for name in ("a", "b", "c", "d")
        )
        with mock.patch("jarvis.contracts.LOCAL_READ_MAX_SOURCES", 4):
            reason = local_read_context_refusal(
                LocalReadContextKind.REFERENCES, ids, four
            )
        self.assertIsNotNone(reason)
        self.assertIn("bundle character ceiling", reason)

    def test_more_than_three_sources_are_refused_by_the_contract(self) -> None:
        ids = ("a", "b", "c", "d")
        self.assertIsNotNone(
            local_read_context_refusal(LocalReadContextKind.REFERENCES, ids, ())
        )
        self.assertEqual(3, LOCAL_READ_MAX_SOURCES)

    def test_declared_order_is_preserved_end_to_end(self) -> None:
        first = self.complete_read("a.txt", body="alpha\n")
        second = self.complete_read("b.txt", body="beta\n")
        forward = self.run_consumer([first, second])
        task = self.memory.load_task(forward["task"]["id"])
        self.assertEqual(
            ["a.txt", "b.txt"],
            [item.relative_path for item in task.local_read_sources],
        )
        self.approve(forward)
        self.assertEqual([("a.txt", "b.txt")], self.planner.seen)

        self.planner.seen.clear()
        reverse = self.run_consumer([second, first])
        reversed_task = self.memory.load_task(reverse["task"]["id"])
        self.assertEqual(
            ["b.txt", "a.txt"],
            [item.relative_path for item in reversed_task.local_read_sources],
        )
        self.approve(reverse)
        self.assertEqual([("b.txt", "a.txt")], self.planner.seen)

    def test_reordering_changes_the_persisted_identity(self) -> None:
        first = self.complete_read("a.txt")
        second = self.complete_read("b.txt")
        forward = self.memory.load_task(
            self.run_consumer([first, second])["task"]["id"]
        )
        reverse = self.memory.load_task(
            self.run_consumer([second, first])["task"]["id"]
        )
        self.assertNotEqual(
            forward.local_read_source_task_ids, reverse.local_read_source_task_ids
        )


# ==========================================================================
# FREEZE / RESTART / TAMPERING
# ==========================================================================
class FreezeTests(LocalReadContextBase):
    def frozen_consumer(self) -> dict[str, Any]:
        first = self.complete_read("a.txt", body="alpha\n")
        second = self.complete_read("b.txt", body="beta\n")
        self.sources = [first, second]
        return self.run_consumer([first, second])

    def test_mutating_a_source_after_the_freeze_changes_nothing(self) -> None:
        payload = self.frozen_consumer()
        before = self.memory.load_task(payload["task"]["id"]).local_read_sources
        self.write_file("a.txt", "COMPLETELY DIFFERENT\n")
        self.tamper(
            self.sources[0],
            lambda state: state["local_read"].update(excerpt="tampered\n"),
        )
        after = self.memory.load_task(payload["task"]["id"]).local_read_sources
        self.assertEqual(before, after)
        self.approve(payload)
        self.assertEqual([("a.txt", "b.txt")], self.planner.seen)

    def test_deleting_a_source_task_after_the_freeze_changes_nothing(self) -> None:
        payload = self.frozen_consumer()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DELETE FROM tasks WHERE id = ?", (self.sources[0],))
            connection.commit()
        finally:
            connection.close()
        final = self.approve(payload)
        self.assertEqual("completed", final["task"]["status"])
        self.assertEqual([("a.txt", "b.txt")], self.planner.seen)

    def test_no_source_task_is_loaded_after_the_freeze(self) -> None:
        payload = self.frozen_consumer()
        sources = set(self.sources)
        real_load = self.memory.load_task
        loaded: list[str] = []

        def spy(task_id: str) -> Task | None:
            loaded.append(task_id)
            return real_load(task_id)

        with mock.patch.object(self.memory, "load_task", side_effect=spy):
            self.approve(payload)
        self.assertEqual(set(), sources & set(loaded))

    def test_a_crash_before_the_checkpoint_re_resolves(self) -> None:
        """Nothing in CONTEXT_RETRIEVAL is durable before its checkpoint."""
        payload = self.frozen_consumer()
        task_id = payload["task"]["id"]

        def rewind(state: dict[str, Any]) -> None:
            state["stage"] = LoopStage.CONTEXT_RETRIEVAL.value
            state["status"] = TaskStatus.RUNNING.value
            state["local_read_sources"] = []

        self.tamper(task_id, rewind)
        self.assertEqual((), self.memory.load_task(task_id).local_read_sources)
        resumed = self.app.resume(
            session_id=payload["session"]["id"], task_id=task_id
        ).payload
        self.assertEqual("awaiting_approval", resumed["task"]["status"])
        self.assertEqual(
            ["a.txt", "b.txt"],
            [
                item.relative_path
                for item in self.memory.load_task(task_id).local_read_sources
            ],
        )

    def test_frozen_sources_removed_after_the_checkpoint_fail_closed(self) -> None:
        payload = self.frozen_consumer()
        task_id = payload["task"]["id"]
        self.tamper(task_id, lambda state: state.update(local_read_sources=[]))
        resumed = self.app.resume(
            session_id=payload["session"]["id"], task_id=task_id
        ).payload
        self.assertEqual("failed", resumed["task"]["status"])
        self.assertEqual(ErrorCode.INVALID_TASK.value, resumed["error"]["code"])
        self.assertEqual([], self.planner.seen)

    def test_a_corrupted_kind_after_the_checkpoint_fails_closed(self) -> None:
        payload = self.frozen_consumer()
        task_id = payload["task"]["id"]
        self.tamper(
            task_id, lambda state: state.update(local_read_context_kind="garbage")
        )
        resumed = self.app.resume(
            session_id=payload["session"]["id"], task_id=task_id
        ).payload
        self.assertEqual("failed", resumed["task"]["status"])
        self.assertEqual(ErrorCode.INVALID_TASK.value, resumed["error"]["code"])
        self.assertEqual([], self.planner.seen)

    def test_a_deleted_kind_after_the_checkpoint_never_sends_a_bare_request(
        self,
    ) -> None:
        """The anti-degrade invariant, at the stage where it matters most."""
        payload = self.frozen_consumer()
        task_id = payload["task"]["id"]
        self.tamper(task_id, lambda state: state.pop("local_read_context_kind"))
        resumed = self.app.resume(
            session_id=payload["session"]["id"], task_id=task_id
        ).payload
        self.assertEqual("failed", resumed["task"]["status"])
        self.assertEqual(ErrorCode.INVALID_TASK.value, resumed["error"]["code"])
        self.assertEqual([], self.planner.seen)

    def test_an_id_count_mismatch_after_the_checkpoint_fails_closed(self) -> None:
        payload = self.frozen_consumer()
        task_id = payload["task"]["id"]
        self.tamper(
            task_id,
            lambda state: state.update(
                local_read_source_task_ids=state["local_read_source_task_ids"][:1]
            ),
        )
        resumed = self.app.resume(
            session_id=payload["session"]["id"], task_id=task_id
        ).payload
        self.assertEqual("failed", resumed["task"]["status"])
        self.assertEqual([], self.planner.seen)

    def test_the_plural_key_reappearing_in_context_fails_closed(self) -> None:
        payload = self.frozen_consumer()
        task_id = payload["task"]["id"]
        self.tamper(
            task_id,
            lambda state: state["context"].update(
                {LOCAL_READ_CONTEXT_REFERENCE_KEY: ["a"]}
            ),
        )
        resumed = self.app.resume(
            session_id=payload["session"]["id"], task_id=task_id
        ).payload
        self.assertEqual("failed", resumed["task"]["status"])
        self.assertIn("task context", resumed["error"]["message"])
        self.assertEqual([], self.planner.seen)

    def test_singular_and_plural_state_together_fail_closed(self) -> None:
        payload = self.frozen_consumer()
        task_id = payload["task"]["id"]
        self.tamper(
            task_id,
            lambda state: state["context"].update(
                {LOCAL_READ_TASK_REFERENCE_KEY: OTHER_ID}
            ),
        )
        resumed = self.app.resume(
            session_id=payload["session"]["id"], task_id=task_id
        ).payload
        self.assertEqual("failed", resumed["task"]["status"])
        self.assertIn("singular and a plural", resumed["error"]["message"])

    def test_injected_sources_before_resolution_fail_closed(self) -> None:
        source = self.complete_read("a.txt")
        payload = self.run_consumer([source])
        task_id = payload["task"]["id"]

        def inject(state: dict[str, Any]) -> None:
            state["stage"] = LoopStage.CONTEXT_RETRIEVAL.value
            state["status"] = TaskStatus.RUNNING.value
            state["local_read_sources"] = [
                snapshot("evil.txt", "injected\n").to_dict()
            ]

        self.tamper(task_id, inject)
        resumed = self.app.resume(
            session_id=payload["session"]["id"], task_id=task_id
        ).payload
        self.assertEqual("failed", resumed["task"]["status"])
        self.assertIn("before they were resolved", resumed["error"]["message"])
        self.assertEqual([], self.planner.seen)

    def test_a_damaged_bundle_at_execute_is_refused_before_any_authority(
        self,
    ) -> None:
        """Parked at EXECUTE, the identity check must fire BEFORE the gate.

        Its signature is the error code: the core check yields INVALID_TASK
        before ``open_execution_attempt``. Without it the damaged bundle would
        instead surface later, as the connector's EGRESS_REFUSED, once the
        re-attestation rebuilt the payload.
        """
        payload = self.frozen_consumer()
        task_id = payload["task"]["id"]

        def park_at_execute(state: dict[str, Any]) -> None:
            state["stage"] = LoopStage.EXECUTE.value
            state["status"] = TaskStatus.RUNNING.value
            state["local_read_sources"] = []

        self.tamper(task_id, park_at_execute)
        with mock.patch.object(
            PlanningConnector, "canonical_outbound", autospec=True
        ) as outbound:
            resumed = self.app.resume(
                session_id=payload["session"]["id"], task_id=task_id
            ).payload
        self.assertEqual("failed", resumed["task"]["status"])
        self.assertEqual(ErrorCode.INVALID_TASK.value, resumed["error"]["code"])
        # The discriminator: the refusal precedes the gate rebuild, so NO
        # attestation is ever recomputed for the damaged bundle. Without the
        # EXECUTE-stage check the loop would fall through to _unsatisfied_gate,
        # rebuild the payload, and only then regate.
        self.assertFalse(outbound.called)
        self.assertEqual([], self.memory.execution_attempts(task_id))
        self.assertEqual([], self.planner.seen)

    def test_the_frozen_bundle_is_retained_at_terminal_state(self) -> None:
        payload = self.frozen_consumer()
        final = self.approve(payload)
        self.assertEqual("completed", final["task"]["status"])
        task = self.memory.load_task(payload["task"]["id"])
        self.assertIs(LocalReadContextKind.REFERENCES, task.local_read_context_kind)
        self.assertEqual(2, len(task.local_read_sources))
        self.assertEqual(2, len(task.local_read_source_task_ids))


# ==========================================================================
# H1 / APPROVAL PROVENANCE
# ==========================================================================
class ProvenanceTests(LocalReadContextBase):
    def build(
        self, ids: tuple[str, ...], sources: tuple[LocalReadSnapshot, ...]
    ) -> Task:
        return Task(
            objective="analyse",
            capability="code_analysis",
            scope=ActionScope(
                action="analyse", data_classification=DataClassification.INTERNAL
            ),
            context={"working_directory": str(self.root)},
            local_read_context_kind=LocalReadContextKind.REFERENCES,
            local_read_source_task_ids=ids,
            local_read_sources=sources,
        )

    def reason_for(self, task: Task) -> str | None:
        return AgentCore._local_read_context_reason(task, self.planner)

    def fingerprint(self, task: Task) -> str:
        reason = AgentCore._handoff_reason(
            task, self.planner
        ) or AgentCore._local_read_context_reason(task, self.planner)
        return self.planner.permission_request(
            task, "session-1", 30, reason=reason
        ).fingerprint

    def test_the_reason_carries_count_characters_and_an_ordered_digest(self) -> None:
        task = self.build(
            ("A", "B"), (snapshot("a.txt", "alpha\n"), snapshot("b.txt", "beta\n"))
        )
        reason = self.reason_for(task)
        expected = hashlib.sha256(
            json.dumps(["A", "B"], separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        ).hexdigest()
        self.assertIn("will send 2 bounded local-read excerpt(s)", reason)
        self.assertIn("(11 characters;", reason)
        self.assertIn(expected, reason)

    def test_the_reason_never_carries_raw_source_task_ids(self) -> None:
        task = self.build(
            ("source-alpha-id", "source-beta-id"),
            (snapshot("a.txt", "alpha\n"), snapshot("b.txt", "beta\n")),
        )
        reason = self.reason_for(task)
        self.assertNotIn("source-alpha-id", reason)
        self.assertNotIn("source-beta-id", reason)

    def test_non_plural_tasks_keep_their_existing_reason(self) -> None:
        task = Task(
            objective="o", capability="code_analysis", scope=ActionScope(action="a")
        )
        self.assertIsNone(self.reason_for(task))

    def test_changing_a_source_id_invalidates_the_fingerprint(self) -> None:
        sources = (snapshot("a.txt", "alpha\n"), snapshot("b.txt", "beta\n"))
        task = self.build(("A", "B"), sources)
        base = self.fingerprint(task)
        task.local_read_source_task_ids = ("A", "C")
        self.assertNotEqual(base, self.fingerprint(task))

    def test_reordering_source_ids_invalidates_the_fingerprint(self) -> None:
        sources = (snapshot("a.txt", "alpha\n"), snapshot("b.txt", "beta\n"))
        task = self.build(("A", "B"), sources)
        base = self.fingerprint(task)
        task.local_read_source_task_ids = ("B", "A")
        self.assertNotEqual(base, self.fingerprint(task))

    def test_changing_one_excerpt_character_invalidates_the_fingerprint(self) -> None:
        first, second = snapshot("a.txt", "alpha\n"), snapshot("b.txt", "beta\n")
        task = self.build(("A", "B"), (first, second))
        base = self.fingerprint(task)
        task.local_read_sources = (first, replace(second, excerpt="betaX\n"))
        self.assertNotEqual(base, self.fingerprint(task))

    def test_reordering_the_frozen_bundle_invalidates_the_fingerprint(self) -> None:
        first, second = snapshot("a.txt", "alpha\n"), snapshot("b.txt", "beta\n")
        task = self.build(("A", "B"), (first, second))
        base = self.fingerprint(task)
        task.local_read_sources = (second, first)
        self.assertNotEqual(base, self.fingerprint(task))

    def test_an_identical_bundle_attests_identically(self) -> None:
        first, second = snapshot("a.txt", "alpha\n"), snapshot("b.txt", "beta\n")
        task = self.build(("A", "B"), (first, second))
        self.assertEqual(self.fingerprint(task), self.fingerprint(task))

    def test_an_approval_reason_reaches_the_envelope(self) -> None:
        first = self.complete_read("a.txt")
        second = self.complete_read("b.txt")
        payload = self.run_consumer([first, second])
        self.assertIn(
            "bounded local-read excerpt(s)", payload["approval"]["reason"]
        )
        self.assertNotIn(first, payload["approval"]["reason"])
        self.assertNotIn(second, payload["approval"]["reason"])

    def test_a_stale_fingerprint_cannot_approve_a_changed_bundle(self) -> None:
        first = self.complete_read("a.txt")
        second = self.complete_read("b.txt")
        payload = self.run_consumer([first, second])
        approval = payload["approval"]
        task_id = payload["task"]["id"]
        self.tamper(
            task_id,
            lambda state: state["local_read_sources"][0].update(
                excerpt="tampered content\n",
                sha256=hashlib.sha256(b"tampered content\n").hexdigest(),
                byte_size=len("tampered content\n"),
            ),
        )
        result = self.app.approve(
            session_id=payload["session"]["id"],
            task_id=task_id,
            request_id=approval["request_id"],
            fingerprint=approval["fingerprint"],
        ).payload
        self.assertNotEqual("completed", result["task"]["status"])
        self.assertEqual([], self.planner.seen)


# ==========================================================================
# CONNECTOR FAIL-CLOSED DEFENCE
# ==========================================================================
class ConnectorDefenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.connector = ClaudeCodeConnector(allowed_roots=[self.root])

    def build(
        self,
        kind: LocalReadContextKind,
        ids: tuple[str, ...],
        sources: tuple[LocalReadSnapshot, ...],
        classification: DataClassification = DataClassification.INTERNAL,
    ) -> Task:
        return Task(
            objective="analyse",
            capability="code_analysis",
            scope=ActionScope(action="analyse", data_classification=classification),
            context={"working_directory": str(self.root)},
            local_read_context_kind=kind,
            local_read_source_task_ids=ids,
            local_read_sources=sources,
        )

    def test_a_clean_none_task_emits_no_block(self) -> None:
        task = self.build(LocalReadContextKind.NONE, (), ())
        self.assertEqual((None, 0), self.connector._local_read_context(task))
        payload, _ = self.connector._sanitized_payload(task)
        self.assertNotIn("local_read_context", payload)

    def test_a_valid_three_source_bundle_emits_every_source_in_order(self) -> None:
        sources = (
            snapshot("a.txt", "alpha\n"),
            snapshot("b.txt", "beta\n"),
            snapshot("c.txt", "gamma\n"),
        )
        task = self.build(LocalReadContextKind.REFERENCES, ("A", "B", "C"), sources)
        block, _ = self.connector._local_read_context(task)
        self.assertEqual(["notice", "sources"], sorted(block))
        self.assertEqual(
            ["a.txt", "b.txt", "c.txt"], [item["path"] for item in block["sources"]]
        )
        self.assertEqual(
            ["byte_size", "content", "path", "truncated"], sorted(block["sources"][0])
        )

    def test_the_encoded_payload_preserves_source_order(self) -> None:
        sources = (
            snapshot("a.txt", "alpha\n"),
            snapshot("b.txt", "beta\n"),
            snapshot("c.txt", "gamma\n"),
        )
        task = self.build(LocalReadContextKind.REFERENCES, ("A", "B", "C"), sources)
        payload, _ = self.connector._sanitized_payload(task)
        encoded = self.connector._encode_payload(payload)
        self.assertLess(encoded.index("a.txt"), encoded.index("b.txt"))
        self.assertLess(encoded.index("b.txt"), encoded.index("c.txt"))

    def test_the_payload_never_carries_source_ids_or_local_only_fields(self) -> None:
        source = snapshot("a.txt", "alpha\n")
        task = self.build(
            LocalReadContextKind.REFERENCES, ("secret-source-id",), (source,)
        )
        payload, _ = self.connector._sanitized_payload(task)
        encoded = self.connector._encode_payload(payload)
        self.assertNotIn("secret-source-id", encoded)
        self.assertNotIn(source.sha256, encoded)
        self.assertNotIn(READ_AT, encoded)
        self.assertNotIn("classification", json.dumps(payload["local_read_context"]))

    def assert_refuses(self, task: Task, fragment: str = "") -> None:
        with self.assertRaises(AttestationError) as caught:
            self.connector._local_read_context(task)
        self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)
        self.assertIn(LOCAL_READ_CONTEXT_EGRESS_REFUSAL, str(caught.exception))
        if fragment:
            self.assertIn(fragment, str(caught.exception))
        # And the refusal propagates out of every payload consumer.
        with self.assertRaises(AttestationError):
            self.connector._sanitized_payload(task)
        with self.assertRaises(AttestationError):
            self.connector.canonical_outbound(task)
        with self.assertRaises(AttestationError):
            self.connector.outbound_preview(task)

    def test_none_carrying_injected_references_refuses(self) -> None:
        self.assert_refuses(
            self.build(LocalReadContextKind.NONE, ("A",), ()), "source references"
        )

    def test_none_carrying_injected_sources_refuses(self) -> None:
        self.assert_refuses(
            self.build(
                LocalReadContextKind.NONE, (), (snapshot("a.txt", "alpha\n"),)
            ),
            "frozen sources",
        )

    def test_invalid_refuses(self) -> None:
        self.assert_refuses(
            self.build(LocalReadContextKind.INVALID, (), ()), "identity is invalid"
        )

    def test_a_missing_bundle_refuses_instead_of_omitting_the_block(self) -> None:
        """The whole point: a declared bundle never silently disappears."""
        self.assert_refuses(
            self.build(LocalReadContextKind.REFERENCES, ("A", "B"), ()),
            "does not match its reference count",
        )

    def test_a_partial_bundle_refuses(self) -> None:
        self.assert_refuses(
            self.build(
                LocalReadContextKind.REFERENCES,
                ("A", "B"),
                (snapshot("a.txt", "alpha\n"),),
            )
        )

    def test_a_malformed_frozen_source_refuses(self) -> None:
        self.assert_refuses(
            self.build(
                LocalReadContextKind.REFERENCES, ("A",), (LocalReadSnapshot(),)
            )
        )

    def test_an_ineligible_source_classification_refuses(self) -> None:
        self.assert_refuses(
            self.build(
                LocalReadContextKind.REFERENCES,
                ("A",),
                (snapshot("a.txt", "alpha\n", DataClassification.SENSITIVE),),
                classification=DataClassification.PUBLIC,
            ),
            "classification ladder",
        )

    def test_a_raw_string_task_classification_refuses(self) -> None:
        """A raw ``"internal"`` compares and hashes equal to the member.

        Asserted against ``_local_read_context`` directly: a raw-string
        classification also breaks the SHARED payload builder's
        ``scope.to_dict()``, which is pre-existing behaviour and not what this
        test is pinning.
        """
        task = self.build(
            LocalReadContextKind.REFERENCES, ("A",), (snapshot("a.txt", "alpha\n"),)
        )
        task.scope = replace(task.scope, data_classification="internal")
        with self.assertRaises(AttestationError) as caught:
            self.connector._local_read_context(task)
        self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)
        self.assertIn("classification is malformed", str(caught.exception))

    def test_a_secret_task_keeps_its_own_refusal(self) -> None:
        """SECRET precedence is unchanged even with a broken bundle."""
        task = self.build(
            LocalReadContextKind.REFERENCES,
            ("A", "B"),
            (LocalReadSnapshot(),),
            classification=DataClassification.SECRET,
        )
        payload, _ = self.connector._sanitized_payload(task)
        self.assertNotIn("local_read_context", payload)
        with self.assertRaises(AttestationError) as caught:
            self.connector.egress_attestation(
                task,
                task.scope,
                RiskLevel.LOW,
                SideEffectProfile(effect=SideEffect.EXTERNAL_EGRESS),
            )
        self.assertEqual(SECRET_EGRESS_REFUSAL, str(caught.exception))

    def test_execute_refuses_a_broken_bundle_without_spawning_a_process(self) -> None:
        """The M8 execute-lifecycle correction, exercised directly.

        This call site runs after the attempt is claimed, so the refusal must
        be a RETURNED result: a raise would be an unknowable outcome for an
        EXTERNAL_EGRESS effect and would strand the attempt.
        """
        task = self.build(
            LocalReadContextKind.REFERENCES, ("A", "B"), (LocalReadSnapshot(),)
        )
        with mock.patch.object(
            ClaudeCodeConnector,
            "billing_route",
            return_value=(BillingClass.NO_ADDITIONAL_CHARGE, "test-route"),
        ):
            with mock.patch("subprocess.Popen") as popen:
                result = self.connector.execute(task, SessionState())
        self.assertFalse(popen.called)
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertIs(ErrorCode.EGRESS_REFUSED, result.error.code)
        self.assertFalse(result.error.retryable)
        self.assertEqual(0, result.usage.call_count)
        self.assertIn(LOCAL_READ_CONTEXT_EGRESS_REFUSAL, result.summary)

    def test_execute_still_runs_a_valid_bundle_up_to_the_process(self) -> None:
        """The guard is narrow: a clean bundle is not refused."""
        task = self.build(
            LocalReadContextKind.REFERENCES, ("A",), (snapshot("a.txt", "alpha\n"),)
        )
        with mock.patch.object(
            ClaudeCodeConnector,
            "billing_route",
            return_value=(BillingClass.NO_ADDITIONAL_CHARGE, "test-route"),
        ):
            with mock.patch("subprocess.Popen") as popen:
                popen.return_value.communicate.return_value = ("", "")
                popen.return_value.returncode = 0
                self.connector.execute(task, SessionState())
        # Reaching Popen at all proves the M8 guard did not fire.
        self.assertTrue(popen.called)


# ==========================================================================
# ISOLATION / COMPATIBILITY
# ==========================================================================
class IsolationTests(LocalReadContextBase):
    def test_no_excerpt_reaches_decisions_project_memory_or_retrieval(self) -> None:
        first = self.complete_read("a.txt", body="ALPHA_SENTINEL_TEXT\n")
        second = self.complete_read("b.txt", body="BETA_SENTINEL_TEXT\n")
        payload = self.run_consumer([first, second])
        final = self.approve(payload)
        session_id = final["session"]["id"]
        decisions = self.memory.list_decisions(session_id)
        blob = json.dumps([record.to_dict() for record in decisions], default=str)
        self.assertNotIn("ALPHA_SENTINEL_TEXT", blob)
        self.assertNotIn("BETA_SENTINEL_TEXT", blob)
        self.assertEqual([], self.memory.list_projects())
        projects = self.query("SELECT COUNT(*) FROM project_memory_items")[0][0]
        rows = self.query("SELECT state_json FROM tasks")
        self.assertEqual(0, projects)
        task = self.memory.load_task(payload["task"]["id"])
        self.assertEqual((), task.retrieved_context.items)
        # The excerpts live in exactly one place: the frozen bundle itself.
        persisted = json.dumps([row[0] for row in rows])
        self.assertEqual(2, persisted.count("ALPHA_SENTINEL_TEXT"))

    def test_source_read_tasks_write_no_decision(self) -> None:
        self.complete_read("a.txt")
        self.assertEqual([], self.memory.list_decisions(self.session_id))
        self.assertEqual(
            0,
            self.query(
                "SELECT COUNT(*) FROM decision_log WHERE session_id = ?",
                (self.session_id,),
            )[0][0],
        )

    def test_the_retrieval_budget_is_untouched_by_the_bundle(self) -> None:
        self.assertEqual(5, RETRIEVAL_MAX_ITEMS)
        self.assertEqual(4000, RETRIEVAL_MAX_CHARACTERS)
        self.assertEqual(3, LOCAL_READ_MAX_SOURCES)
        self.assertEqual(12000, LOCAL_READ_CONTEXT_MAX_CHARS)

    def test_no_filesystem_read_happens_during_the_consumer_task(self) -> None:
        first = self.complete_read("a.txt")
        second = self.complete_read("b.txt")
        payload = self.run_consumer([first, second])
        with mock.patch.object(
            Path, "read_text", side_effect=AssertionError("filesystem read")
        ):
            with mock.patch.object(
                Path, "read_bytes", side_effect=AssertionError("filesystem read")
            ):
                final = self.approve(payload)
        self.assertEqual("completed", final["task"]["status"])

    def test_no_external_tool_grants_exist(self) -> None:
        self.assertEqual((), EGRESS_TOOL_GRANTS)

    def test_the_envelope_is_unchanged_for_a_plural_task(self) -> None:
        first = self.complete_read("a.txt")
        second = self.complete_read("b.txt")
        payload = self.run_consumer([first, second])
        final = self.approve(payload)
        self.assertEqual(OUTPUT_SCHEMA_VERSION, final["schema_version"])
        self.assertEqual(5, final["schema_version"])
        self.assertIsNone(final["local_read"])
        self.assertNotIn("local_read_context", final)
        self.assertNotIn("local_reads", final)

    def test_the_envelope_key_set_is_unchanged(self) -> None:
        run = AgentRun(
            RunStatus.COMPLETED,
            SessionState(),
            Task(
                objective="o",
                capability="code_analysis",
                scope=ActionScope(action="a"),
            ),
        )
        self.assertEqual(
            [
                "approval",
                "billing",
                "command",
                "effective_permission_mode",
                "error",
                "local_read",
                "local_write",
                "model_answer",
                "progress",
                "result",
                "run_status",
                "schema_version",
                "session",
                "task",
            ],
            sorted(build_envelope("run", PermissionMode.STRICT, run)),
        )


class SingularCompatibilityTests(unittest.TestCase):
    """M3 is untouched: same grammar, same payload, same fingerprint."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.connector = ClaudeCodeConnector(allowed_roots=[self.root])
        self.singular = Task(
            objective="analyse one file",
            capability="code_analysis",
            scope=ActionScope(
                action="analyse", data_classification=DataClassification.INTERNAL
            ),
            context={
                "working_directory": str(self.root),
                LOCAL_READ_TASK_REFERENCE_KEY: "source-task-id",
            },
            local_read=snapshot("a.txt", "alpha content\n"),
        )

    def test_a_singular_task_is_none_kind_and_emits_no_plural_block(self) -> None:
        self.assertIs(
            LocalReadContextKind.NONE, self.singular.local_read_context_kind
        )
        payload, _ = self.connector._sanitized_payload(self.singular)
        self.assertIn("local_read", payload)
        self.assertNotIn("local_read_context", payload)

    def test_the_singular_block_field_set_is_unchanged(self) -> None:
        payload, _ = self.connector._sanitized_payload(self.singular)
        self.assertEqual(
            ["byte_size", "content", "notice", "path", "truncated"],
            sorted(payload["local_read"]),
        )

    def test_the_singular_payload_key_set_is_unchanged(self) -> None:
        payload, _ = self.connector._sanitized_payload(self.singular)
        self.assertEqual(
            [
                "capability",
                "context",
                "local_read",
                "objective",
                "plan",
                "preferences",
                "scope",
                "visible_assumption",
            ],
            sorted(payload),
        )

    def test_the_singular_golden_outbound_is_byte_stable(self) -> None:
        first = self.connector.canonical_outbound(self.singular)
        second = self.connector.canonical_outbound(self.singular)
        self.assertEqual(first, second)
        self.assertIn(b"alpha content", first)

    def test_m8_adds_no_reason_to_a_singular_task(self) -> None:
        self.assertIsNone(
            AgentCore._local_read_context_reason(self.singular, self.connector)
        )

    def test_the_m3_reference_still_travels_in_context(self) -> None:
        """Pinned as EXISTING M3 behaviour, deliberately not changed by M8."""
        payload, _ = self.connector._sanitized_payload(self.singular)
        self.assertEqual(
            "source-task-id", payload["context"][LOCAL_READ_TASK_REFERENCE_KEY]
        )

    def test_the_m3_input_grammar_still_strips(self) -> None:
        """Also pinned as EXISTING behaviour; the plural refuses padding."""
        document = json.dumps(
            {
                "objective": "o",
                "capability": "code_analysis",
                "scope": {"action": "a"},
                "context": {LOCAL_READ_TASK_REFERENCE_KEY: "  padded  "},
            }
        )
        task = decode_task_json(document)
        self.assertEqual("padded", task.context[LOCAL_READ_TASK_REFERENCE_KEY])
        self.assertIs(LocalReadContextKind.NONE, task.local_read_context_kind)


class ConsumerPolicyTests(LocalReadContextBase):
    """The consumer policy must judge the POST-FREEZE task, not the empty one."""

    def setUp(self) -> None:
        super().setUp()
        self.late = LateLocalConnector()
        self.app = create_app(
            settings=self.settings,
            memory=self.memory,
            connectors=(self.planner, self.late, self.reader),
        )

    def assert_never_routed(self, payload: dict[str, Any]) -> None:
        task_id = payload["task"]["id"]
        self.assertEqual("failed", payload["task"]["status"])
        self.assertEqual(ErrorCode.INVALID_TASK.value, payload["error"]["code"])
        # The hostile connector never ran and was never even recorded.
        self.assertEqual(0, self.late.execute_calls)
        self.assertEqual([], self.late.saw)
        self.assertIsNone(payload["task"]["selected_connector"])
        self.assertIsNone(self.memory.load_task(task_id).selected_connector)
        # Nothing downstream of PLAN was created.
        self.assertIsNone(payload["approval"])
        self.assertIsNone(
            self.memory.load_pending_approval(task_id, payload["session"]["id"])
        )
        self.assertEqual([], self.memory.execution_attempts(task_id))
        self.assertEqual(
            0,
            self.query(
                "SELECT COUNT(*) FROM permission_grants WHERE task_id = ?", (task_id,)
            )[0][0],
        )
        self.assertEqual([], self.planner.seen)

    def test_the_hostile_connector_is_invisible_before_the_freeze(self) -> None:
        """Pins the premise: a pre-freeze scan genuinely cannot see it."""
        empty = Task(
            objective="analyse",
            capability="code_analysis",
            scope=ActionScope(action="analyse"),
        )
        self.assertFalse(self.late.can_handle(empty))
        empty.local_read_sources = (snapshot("a.txt", "alpha\n"),)
        self.assertTrue(self.late.can_handle(empty))

    def test_a_late_non_egress_connector_never_receives_the_bundle(self) -> None:
        source = self.complete_read("a.txt")
        payload = self.run_consumer([source])
        self.assert_never_routed(payload)
        self.assertIn("external-model", payload["error"]["message"])

    def test_the_frozen_bundle_still_exists_but_was_never_routed(self) -> None:
        """The refusal is at PLAN, so the freeze happened and went nowhere."""
        source = self.complete_read("a.txt")
        payload = self.run_consumer([source])
        self.assert_never_routed(payload)
        task = self.memory.load_task(payload["task"]["id"])
        self.assertEqual(1, len(task.local_read_sources))
        self.assertEqual(LoopStage.FAILED.value, payload["task"]["stage"])

    def test_a_preferred_late_non_egress_connector_cannot_bypass_the_policy(
        self,
    ) -> None:
        """Preference chooses among ALLOWED routes; it never creates one."""
        source = self.complete_read("a.txt")
        document = json.loads(self.consumer_document([source]))
        document["preferences"] = {"preferred_connector": self.late.manifest.name}
        payload = self.app.run(
            json.dumps(document), session_id=self.session_id
        ).payload
        self.assert_never_routed(payload)

    def test_a_capable_low_priority_local_connector_still_refuses_the_route(
        self,
    ) -> None:
        """EVERY capable connector must be egress, not merely the winner.

        Here the hostile connector would LOSE selection to the planner, so the
        post-selection check never sees it. Only the enumeration policy can
        refuse this - which is what makes it the discriminating test for that
        control. A capable local connector is a live route the moment a
        priority, an availability or a suitability changes.
        """
        quiet = LateLocalConnector(name="late-local-quiet")
        quiet.manifest = replace(quiet.manifest, priority=1)
        self.late = quiet
        self.app = create_app(
            settings=self.settings,
            memory=self.memory,
            connectors=(self.planner, quiet, self.reader),
        )
        source = self.complete_read("a.txt")
        payload = self.run_consumer([source])
        self.assert_never_routed(payload)
        self.assertIn("external-model", payload["error"]["message"])

    def test_the_policy_refuses_when_the_registry_cannot_be_enumerated(self) -> None:
        """Uncertainty is a refusal, never an admission."""
        source = self.complete_read("a.txt")
        with mock.patch.object(
            self.app.core.registry, "list_names", side_effect=RuntimeError("boom")
        ):
            payload = self.run_consumer([source])
        self.assert_never_routed(payload)
        self.assertIn("could not be enumerated", payload["error"]["message"])

    def test_the_policy_refuses_when_a_connector_cannot_be_inspected(self) -> None:
        source = self.complete_read("a.txt")
        real_get = self.app.core.registry.get

        def hostile_get(name: str) -> Any:
            if name == self.late.manifest.name:
                raise RuntimeError("unreadable connector")
            return real_get(name)

        with mock.patch.object(
            self.app.core.registry, "get", side_effect=hostile_get
        ):
            payload = self.run_consumer([source])
        self.assert_never_routed(payload)
        self.assertIn("could not be inspected", payload["error"]["message"])

    def test_the_selected_connector_is_re_judged_after_selection(self) -> None:
        """Defence in depth: enumeration and selection are different paths.

        ``registry.select`` reads its own private mapping, so hiding the
        hostile connector from ``list_names`` makes the enumeration policy
        pass while selection still routes to it. Only the post-selection
        check can refuse that, which is what makes this test non-vacuous.
        """
        source = self.complete_read("a.txt")
        with mock.patch.object(
            self.app.core.registry,
            "list_names",
            return_value=(self.planner.manifest.name, "local-file-read"),
        ):
            payload = self.run_consumer([source])
        self.assert_never_routed(payload)
        self.assertIn("selected connector", payload["error"]["message"])


class ConsumerPolicyPositiveControlTests(LocalReadContextBase):
    """With only legitimate egress consumers, nothing changes."""

    def test_a_legitimate_bundle_still_completes_normally(self) -> None:
        first = self.complete_read("a.txt", body="alpha\n")
        second = self.complete_read("b.txt", body="beta\n")
        payload = self.run_consumer([first, second])
        self.assertEqual("awaiting_approval", payload["task"]["status"])
        self.assertEqual(
            self.planner.manifest.name, payload["task"]["selected_connector"]
        )
        self.assertIn(
            "bounded local-read excerpt(s)", payload["approval"]["reason"]
        )
        final = self.approve(payload)
        self.assertEqual("completed", final["task"]["status"])
        self.assertEqual([("a.txt", "b.txt")], self.planner.seen)

    def test_a_non_plural_task_is_not_subject_to_the_consumer_policy(self) -> None:
        """A NONE-kind task keeps whatever routing it always had."""
        late = LateLocalConnector(name="late-local-2")
        app = create_app(
            settings=self.settings,
            memory=self.memory,
            connectors=(self.planner, late, self.reader),
        )
        plain = json.dumps(
            {
                "objective": "analyse nothing in particular",
                "capability": "code_analysis",
                "scope": {"action": "analyse", "target": "repo"},
                "context": {"working_directory": str(self.root)},
            }
        )
        payload = app.run(plain).payload
        # No bundle, so the hostile connector still cannot handle it and the
        # ordinary egress route is chosen exactly as before M8.
        self.assertEqual(
            self.planner.manifest.name, payload["task"]["selected_connector"]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
