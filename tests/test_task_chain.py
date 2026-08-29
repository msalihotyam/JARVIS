"""M11 S1 - Sequential Task Chain, contract layer only.

What this file pins, end to end:

    a trusted-boundary declaration -> a typed ordered ChainStep tuple ->
    ONE inert ChainState -> deterministic declaration digest -> session
    persistence that survives load -> save -> load

plus everything that must NOT happen: a chain that declares no step or more
than MAX_CHAIN_STEPS, a step that consumes itself or a later step, a step
that names a concrete task id instead of a predecessor INDEX, a step that
smuggles a model answer, a retrieved-memory reference or a nested chain, a
tampered cursor or task-id mapping that loads as a shorter usable chain, and
a corrupted chain that degrades to ABSENCE rather than to the typed INVALID
verdict.

S1 is the INERT layer: nothing here creates a task, requests an approval,
builds an attestation, opens an execution attempt or reserves a cent. Those
belong to the ordinary single-action tasks a later slice will create, and
this file asserts only that the declaration itself carries no such authority.
No live model, connector, auth or network call is ever made.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import inspect
import io
import json
import re
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis.app import (
    EXIT_AWAITING_APPROVAL,
    EXIT_INTERNAL,
    EXIT_INVALID_INPUT,
    EXIT_NOT_FOUND,
    EXIT_OK,
    OUTPUT_SCHEMA_VERSION,
    JarvisApp,
    TaskInputError,
    build_envelope,
    chain_declare_result,
    chain_show_result,
    decode_chain_declaration,
)
from jarvis.cli import main as cli_main
from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    ATTEMPT_NON_REPLAYABLE,
    CANONICAL_TARGET_CAPABILITIES,
    CHAIN_ENABLED_WRITE_MODES,
    CHAIN_ID_MAX_CHARS,
    CHAIN_LOCATOR_MAX_CHARS,
    CHAIN_NO_PREDECESSOR,
    CHAIN_OBJECTIVE_MAX_CHARS,
    CHAIN_SYMBOL_MAX_CHARS,
    LOCAL_READ_CAPABILITY,
    LOCAL_READ_TASK_REFERENCE_KEY,
    LOCAL_WRITE_CAPABILITY,
    LOCAL_WRITE_ENABLED_MODES,
    MAX_CHAIN_STEPS,
    MONTHLY_HARD_LIMIT_NANO_USD,
    ActionScope,
    AttemptState,
    BillingClaim,
    BillingClass,
    ChainBinding,
    ChainState,
    ChainStatus,
    ChainStep,
    ChainTaskMode,
    CostEstimate,
    DataClassification,
    ErrorCode,
    ExecutionAttempt,
    ExecutionClaimOutcome,
    LocalReadContextKind,
    LocalReadKind,
    LocalReadSnapshot,
    LocalWriteRecord,
    LoopStage,
    MemoryStore,
    ModelAnswer,
    MonetaryState,
    OutcomeStatus,
    RetrievedContext,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    SideEffectProfile,
    Task,
    TaskProgress,
    TaskStatus,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
    WriteSource,
    build_model_answer,
    canonical_path,
    chain_binding_refusal,
    chain_declaration_refusal,
    chain_id_refusal,
    chain_locator_refusal,
    chain_objective_refusal,
    chain_state_refusal,
    chain_step_refusal,
    chain_steps_from_declaration,
    chain_symbol_refusal,
    chain_task_refusal,
    local_read_snapshot_refusal,
    looks_like_path,
)
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import safe_cleanup

#: Sentinel for ``rewrite_binding``: remove the key rather than replace it.
_DROPPED = object()


class _LoudStr(str):
    """A ``str`` SUBCLASS. Not the declared type, however much it looks like it."""



READ_STEP = {
    "objective": "Read the project notes.",
    "capability": LOCAL_READ_CAPABILITY,
    "action": "read",
    "target": "notes.txt",
    "destination": "",
    "write_mode": "",
    "consumes": CHAIN_NO_PREDECESSOR,
}
MODEL_STEP = {
    "objective": "Summarize the notes in three bullet points.",
    "capability": "code_analysis",
    "action": "analyze",
    "target": "",
    "destination": "anthropic/claude-code",
    "write_mode": "",
    "consumes": 0,
}
WRITE_STEP = {
    "objective": "Save the summary as a new file.",
    "capability": LOCAL_WRITE_CAPABILITY,
    "action": "write",
    "target": "summary.md",
    "destination": "",
    "write_mode": "create",
    "consumes": 1,
}


def canonical_read_snapshot(text: str = "the project notes") -> LocalReadSnapshot:
    """Exactly what a real bounded local read stores. Passes the ONE contract."""
    raw = text.encode("utf-8")
    snapshot = LocalReadSnapshot(
        relative_path="notes.txt",
        byte_size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        excerpt=text,
        truncated=False,
        classification=DataClassification.INTERNAL,
        read_at="2026-08-28T00:00:00+00:00",
    )
    assert local_read_snapshot_refusal(snapshot) is None
    return snapshot


def declaration(count: int) -> list[dict[str, object]]:
    """The canonical read -> model -> write declaration, truncated to ``count``."""
    return [dict(item) for item in (READ_STEP, MODEL_STEP, WRITE_STEP)[:count]]


def chain(count: int, **overrides: object) -> ChainState:
    """An ACTIVE chain of ``count`` declared steps with no task created yet."""
    steps = chain_steps_from_declaration(declaration(count))
    base = ChainState(
        id="chain-1",
        steps=steps,
        step_task_ids=("",) * len(steps),
        cursor=0,
        status=ChainStatus.ACTIVE,
    )
    return replace(base, **overrides) if overrides else base


class ChainTestCase(unittest.TestCase):
    def assertUsable(self, candidate: ChainState) -> None:
        self.assertIsNone(chain_state_refusal(candidate))

    def assertRefused(self, candidate: ChainState, fragment: str) -> None:
        reason = chain_state_refusal(candidate)
        self.assertIsNotNone(reason, "the chain was accepted")
        assert reason is not None
        self.assertIn(fragment, reason)

    def assertLoadsInvalid(self, payload: object) -> None:
        """A malformed persisted chain is the typed INVALID verdict, whole."""
        loaded = ChainState.from_dict(payload)
        self.assertIs(loaded.status, ChainStatus.INVALID)
        # INVALID is never partially usable: no declaration survives it, so
        # nothing downstream can execute a shortened or repaired chain.
        self.assertEqual(loaded.steps, ())
        self.assertEqual(loaded.step_task_ids, ())
        self.assertEqual(loaded.id, "")
        self.assertEqual(loaded.cursor, 0)
        # PRESENT, not absent: a corrupted chain is a chain this session
        # already has. Absence has exactly one spelling, and it is None.
        self.assertTrue(loaded.present)
        self.assertIsNotNone(chain_state_refusal(loaded))


class DeclarationTests(ChainTestCase):
    """The trusted input boundary grammar: what a caller may declare."""

    def test_a_one_step_declaration_is_usable(self) -> None:
        self.assertIsNone(chain_declaration_refusal(declaration(1)))
        steps = chain_steps_from_declaration(declaration(1))
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].capability, LOCAL_READ_CAPABILITY)
        self.assertEqual(steps[0].consumes, CHAIN_NO_PREDECESSOR)
        self.assertUsable(chain(1))

    def test_a_two_step_declaration_is_usable(self) -> None:
        self.assertIsNone(chain_declaration_refusal(declaration(2)))
        steps = chain_steps_from_declaration(declaration(2))
        self.assertEqual([step.consumes for step in steps], [CHAIN_NO_PREDECESSOR, 0])
        self.assertUsable(chain(2))

    def test_a_three_step_declaration_is_usable(self) -> None:
        self.assertIsNone(chain_declaration_refusal(declaration(3)))
        steps = chain_steps_from_declaration(declaration(3))
        self.assertEqual(len(steps), MAX_CHAIN_STEPS)
        self.assertEqual([step.consumes for step in steps], [CHAIN_NO_PREDECESSOR, 0, 1])
        self.assertEqual(steps[2].write_mode, "create")
        self.assertUsable(chain(3))

    def test_the_declaration_order_is_preserved_verbatim(self) -> None:
        steps = chain_steps_from_declaration(declaration(3))
        self.assertEqual(
            [step.action for step in steps], ["read", "analyze", "write"]
        )

    def test_optional_step_keys_may_be_omitted(self) -> None:
        # A caller need not spell every field; the typed defaults are the
        # inert ones (no target, no destination, no write mode, no
        # predecessor), never a permissive guess.
        minimal = [
            {"objective": "Plan it.", "capability": "code_planning", "action": "plan"}
        ]
        self.assertIsNone(chain_declaration_refusal(minimal))
        step = chain_steps_from_declaration(minimal)[0]
        self.assertEqual(step.target, "")
        self.assertEqual(step.destination, "")
        self.assertEqual(step.write_mode, "")
        self.assertEqual(step.consumes, CHAIN_NO_PREDECESSOR)

    def test_a_declaration_that_is_not_a_list_is_refused(self) -> None:
        for payload in ({"capability": "file_read"}, "file_read", 3, None, ()):
            with self.subTest(payload=payload):
                self.assertIsNotNone(chain_declaration_refusal(payload))
                self.assertEqual(chain_steps_from_declaration(payload), ())

    def test_a_declaration_with_no_step_is_refused(self) -> None:
        self.assertEqual(
            chain_declaration_refusal([]), "chain declaration names no step"
        )
        self.assertEqual(chain_steps_from_declaration([]), ())

    def test_a_declaration_beyond_the_step_ceiling_is_refused(self) -> None:
        oversized = declaration(3) + [dict(READ_STEP)]
        self.assertEqual(len(oversized), MAX_CHAIN_STEPS + 1)
        self.assertEqual(
            chain_declaration_refusal(oversized),
            "chain declaration exceeds its step ceiling",
        )
        self.assertEqual(chain_steps_from_declaration(oversized), ())

    def test_a_step_that_is_not_an_object_is_refused(self) -> None:
        for item in ("file_read", 1, None, [], True):
            with self.subTest(item=item):
                self.assertIsNotNone(chain_declaration_refusal([item]))

    def test_a_malformed_step_refuses_the_whole_declaration(self) -> None:
        malformed = declaration(3)
        malformed[1]["action"] = ""
        reason = chain_declaration_refusal(malformed)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("chain step 1", reason)
        # Never a shortened declaration: refusing one step refuses all three.
        self.assertEqual(chain_steps_from_declaration(malformed), ())

    def test_a_step_level_fault_names_the_step_that_carries_it(self) -> None:
        # Persisted step faults all collapse to the empty step, so a load-path
        # assertion on the reason text is only meaningful at the DECLARATION
        # boundary, where the faulty field is still visible. Pinned so a test
        # cannot pass by receiving some other field's refusal.
        payload = declaration(2)
        payload[1]["target"] = " padded.txt"
        self.assertEqual(
            chain_declaration_refusal(payload),
            "chain step 1 is malformed: target is malformed: "
            "locator carries surrounding whitespace",
        )


class AuthoritySmugglingTests(ChainTestCase):
    """A step declares intent. It may never name authority of any kind."""

    def test_a_caller_supplied_task_id_is_refused(self) -> None:
        # The whole point of naming a predecessor by INDEX: a task id would
        # be a caller-chosen pointer at an arbitrary completed row.
        for key in ("task_id", "step_task_id", "id"):
            with self.subTest(key=key):
                payload = declaration(2)
                payload[1][key] = "0f8e-completed-task"
                self.assertEqual(
                    chain_declaration_refusal(payload),
                    "chain step 1 carries an unsupported key",
                )

    def test_a_model_answer_reference_is_refused(self) -> None:
        payload = declaration(2)
        payload[1]["model_answer_task_id"] = "some-task"
        self.assertEqual(
            chain_declaration_refusal(payload),
            "chain step 1 carries an unsupported key",
        )

    def test_a_read_reference_is_refused(self) -> None:
        for key in ("local_read_task_id", "local_read_task_ids"):
            with self.subTest(key=key):
                payload = declaration(1)
                payload[0][key] = "some-task"
                self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_a_retrieved_memory_field_is_refused(self) -> None:
        payload = declaration(1)
        payload[0]["retrieved_memory"] = ["a remembered instruction"]
        self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_a_nested_chain_is_refused(self) -> None:
        # No nesting: a chain is flat and sequential by construction, and a
        # step that could carry one would be a second orchestration system.
        for key in ("chain", "steps"):
            with self.subTest(key=key):
                payload = declaration(1)
                payload[0][key] = declaration(1)
                self.assertEqual(
                    chain_declaration_refusal(payload),
                    "chain step 0 carries an unsupported key",
                )

    def test_approval_and_billing_fields_are_refused(self) -> None:
        for key in ("approved", "fingerprint", "grant", "reserved_nano_usd"):
            with self.subTest(key=key):
                payload = declaration(1)
                payload[0][key] = "x"
                self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_a_caller_may_not_declare_progress(self) -> None:
        # Status, cursor and task ids are the persisted chain's fields, not a
        # declaration's: a caller declares intent, never progress.
        for key in ("status", "cursor", "step_task_ids"):
            with self.subTest(key=key):
                payload = declaration(1)
                payload[0][key] = "completed"
                self.assertIsNotNone(chain_declaration_refusal(payload))


class PredecessorReferenceTests(ChainTestCase):
    """A predecessor is an earlier index, and nothing else."""

    def test_a_self_reference_is_refused(self) -> None:
        payload = declaration(2)
        payload[1]["consumes"] = 1
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("consumes itself", reason)

    def test_the_first_step_may_not_consume_itself(self) -> None:
        payload = declaration(1)
        payload[0]["consumes"] = 0
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("consumes itself", reason)

    def test_a_forward_reference_is_refused(self) -> None:
        payload = declaration(3)
        payload[0]["consumes"] = 2
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("consumes a later step", reason)

    def test_a_reference_past_the_end_is_refused(self) -> None:
        payload = declaration(2)
        payload[1]["consumes"] = 7
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("consumes a later step", reason)

    def test_a_negative_reference_that_is_not_the_sentinel_is_refused(self) -> None:
        payload = declaration(2)
        payload[1]["consumes"] = -2
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("not a real step index", reason)

    def test_a_boolean_reference_is_refused(self) -> None:
        # ``True`` is an ``int`` in Python; it must never be read as step 1.
        payload = declaration(2)
        payload[1]["consumes"] = True
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("not an exact integer", reason)

    def test_a_non_integer_reference_is_refused(self) -> None:
        for value in ("0", 0.0, None, [0]):
            with self.subTest(value=value):
                payload = declaration(2)
                payload[1]["consumes"] = value
                self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_a_step_may_reach_back_past_its_immediate_predecessor(self) -> None:
        # Sequential execution order, not a linear dependency chain: step 2
        # consuming step 0 is a legitimate declaration. Shown with a NON-write
        # third step, because a create step carries the separate, stricter
        # rule that its producer must declare an outbound destination.
        payload = [
            dict(READ_STEP),
            dict(MODEL_STEP),
            dict(MODEL_STEP, objective="Re-read the notes.", consumes=0),
        ]
        self.assertIsNone(chain_declaration_refusal(payload))


class StepGrammarTests(ChainTestCase):
    """One step, judged at its position."""

    def test_a_step_position_outside_the_chain_is_refused(self) -> None:
        step = ChainStep(objective="Read it.", capability="file_read", action="read")
        self.assertIsNotNone(chain_step_refusal(step, 3, 3))
        self.assertIsNotNone(chain_step_refusal(step, -1, 3))

    def test_a_non_step_is_refused(self) -> None:
        self.assertEqual(
            chain_step_refusal({"capability": "file_read"}, 0, 1),
            "step is not an exact ChainStep",
        )

    def test_a_boolean_position_is_refused(self) -> None:
        step = ChainStep(objective="Read it.", capability="file_read", action="read")
        self.assertEqual(
            chain_step_refusal(step, True, 3),
            "step position is not an exact integer",
        )

    def test_a_blank_or_padded_symbol_is_refused_not_stripped(self) -> None:
        for value in ("", " read", "read ", "\tread"):
            with self.subTest(value=value):
                self.assertIsNotNone(chain_symbol_refusal(value))

    def test_a_symbol_at_its_ceiling_is_accepted_and_beyond_it_refused(self) -> None:
        self.assertIsNone(chain_symbol_refusal("a" * CHAIN_SYMBOL_MAX_CHARS))
        self.assertIsNotNone(chain_symbol_refusal("a" * (CHAIN_SYMBOL_MAX_CHARS + 1)))

    def test_a_nul_bearing_symbol_or_locator_is_refused(self) -> None:
        self.assertIsNotNone(chain_symbol_refusal("re\x00ad"))
        self.assertIsNotNone(chain_locator_refusal("no\x00tes.txt"))

    def test_an_empty_locator_is_allowed(self) -> None:
        self.assertIsNone(chain_locator_refusal(""))

    def test_a_locator_at_its_ceiling_is_accepted_and_beyond_it_refused(self) -> None:
        self.assertIsNone(chain_locator_refusal("a" * CHAIN_LOCATOR_MAX_CHARS))
        self.assertIsNotNone(chain_locator_refusal("a" * (CHAIN_LOCATOR_MAX_CHARS + 1)))

    def test_a_write_step_without_a_write_mode_is_refused(self) -> None:
        payload = declaration(3)
        payload[2]["write_mode"] = ""
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("declares no write mode", reason)

    def test_a_write_mode_without_the_write_capability_is_refused(self) -> None:
        payload = declaration(1)
        payload[0]["write_mode"] = "create"
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("without the write capability", reason)

    def test_an_unknown_write_mode_is_refused(self) -> None:
        payload = declaration(3)
        payload[2]["write_mode"] = "append"
        self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_the_overwrite_mode_is_refused_for_a_chain_step(self) -> None:
        # Recognized by the write GRAMMAR, refused by CHAIN policy: a chain
        # step declares no bytes, and an overwrite requires direct content
        # (a model answer may create a file but never replace one), so a
        # declared chain overwrite could never be materialized. Standalone
        # M10 overwrite is untouched.
        payload = declaration(3)
        payload[2]["write_mode"] = "overwrite"
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("may not declare this write mode", reason)

    def test_an_identifier_grammar_rejects_blank_padded_and_oversized(self) -> None:
        self.assertIsNone(chain_id_refusal("a" * CHAIN_ID_MAX_CHARS))
        for value in ("", " id", "id ", "a" * (CHAIN_ID_MAX_CHARS + 1), "i\x00d", 1):
            with self.subTest(value=value):
                self.assertIsNotNone(chain_id_refusal(value))


class ChainStateTests(ChainTestCase):
    """The persisted chain: declaration, cursor and created-task mapping."""

    def test_a_non_chain_is_refused(self) -> None:
        self.assertEqual(
            chain_state_refusal({"id": "chain-1"}), "chain is not an exact ChainState"
        )

    def test_a_chain_with_no_step_is_refused(self) -> None:
        self.assertRefused(
            ChainState(id="chain-1", steps=(), step_task_ids=()),
            "declares no step",
        )

    def test_a_chain_beyond_the_step_ceiling_is_refused(self) -> None:
        steps = chain_steps_from_declaration(declaration(3))
        oversized = ChainState(
            id="chain-1",
            steps=steps
            + (ChainStep(objective="Read it.", capability="file_read", action="read"),),
            step_task_ids=("",) * (MAX_CHAIN_STEPS + 1),
        )
        self.assertRefused(oversized, "exceeds its step ceiling")

    def test_a_blank_chain_id_is_refused(self) -> None:
        self.assertRefused(chain(2, id=""), "chain id is malformed")

    def test_a_status_that_is_not_the_enum_is_refused(self) -> None:
        self.assertRefused(chain(2, status="active"), "not an exact ChainStatus")

    def test_the_invalid_status_is_always_refused(self) -> None:
        self.assertRefused(chain(2, status=ChainStatus.INVALID), "identity is invalid")

    def test_steps_must_be_a_tuple_not_a_list(self) -> None:
        steps = chain_steps_from_declaration(declaration(2))
        self.assertRefused(chain(2, steps=list(steps)), "not a tuple")

    def test_task_ids_must_be_a_tuple_not_a_list(self) -> None:
        self.assertRefused(chain(2, step_task_ids=["", ""]), "not a tuple")

    def test_a_task_id_mapping_of_the_wrong_length_is_refused(self) -> None:
        for mapping in ((), ("",), ("", "", "")):
            with self.subTest(mapping=mapping):
                self.assertRefused(
                    chain(2, step_task_ids=mapping),
                    "do not match the declared steps",
                )

    def test_a_task_id_mapping_with_a_gap_is_refused(self) -> None:
        # A sequential chain cannot have created step 1's task before step 0's.
        self.assertRefused(
            chain(3, cursor=2, step_task_ids=("", "task-b", "task-c")),
            "skip an earlier step",
        )

    def test_a_duplicated_step_task_id_is_refused(self) -> None:
        self.assertRefused(
            chain(2, cursor=1, step_task_ids=("task-a", "task-a")),
            "same step task twice",
        )

    def test_a_malformed_step_task_id_is_refused(self) -> None:
        for value in (" task-a", "task-a ", "t\x00", "a" * (CHAIN_ID_MAX_CHARS + 1)):
            with self.subTest(value=value):
                self.assertRefused(
                    chain(2, step_task_ids=(value, "")),
                    "step task id is malformed",
                )

    def test_a_non_string_step_task_id_is_refused(self) -> None:
        self.assertRefused(chain(2, step_task_ids=(1, "")), "is not a string")

    def test_a_boolean_cursor_is_refused(self) -> None:
        self.assertRefused(chain(2, cursor=True), "not an exact integer")

    def test_a_non_integer_cursor_is_refused(self) -> None:
        for value in ("0", 0.0, None):
            with self.subTest(value=value):
                self.assertRefused(chain(2, cursor=value), "not an exact integer")

    def test_a_cursor_outside_the_chain_is_refused(self) -> None:
        for value in (-1, 2, 99):
            with self.subTest(value=value):
                self.assertRefused(chain(2, cursor=value), "outside the chain")

    def test_a_task_created_beyond_the_cursor_is_refused(self) -> None:
        # Step 1's task cannot exist while the chain still sits on step 0.
        self.assertRefused(
            chain(2, cursor=0, step_task_ids=("task-a", "task-b")),
            "beyond its cursor",
        )

    def test_a_cursor_past_an_uncreated_step_is_refused(self) -> None:
        self.assertRefused(
            chain(3, cursor=2, step_task_ids=("", "", "")),
            "advanced past a step it never created",
        )

    def test_the_step_at_the_cursor_may_be_created_or_not(self) -> None:
        self.assertUsable(chain(2, cursor=1, step_task_ids=("task-a", "")))
        self.assertUsable(chain(2, cursor=1, step_task_ids=("task-a", "task-b")))

    def test_a_completed_chain_must_have_created_every_step(self) -> None:
        self.assertRefused(
            chain(
                2,
                cursor=1,
                step_task_ids=("task-a", ""),
                status=ChainStatus.COMPLETED,
            ),
            "left a step uncreated",
        )

    def test_a_completed_chain_must_sit_on_its_last_step(self) -> None:
        # Caught by the cursor rule rather than by a second completion rule:
        # once every step has a task, "created beyond the cursor" already
        # forces the cursor onto the last step.
        self.assertRefused(
            chain(
                2,
                cursor=0,
                step_task_ids=("task-a", "task-b"),
                status=ChainStatus.COMPLETED,
            ),
            "beyond its cursor",
        )

    def test_a_completed_chain_is_usable(self) -> None:
        self.assertUsable(
            chain(
                3,
                cursor=2,
                step_task_ids=("task-a", "task-b", "task-c"),
                status=ChainStatus.COMPLETED,
            )
        )

    def test_a_failed_chain_keeps_its_completed_steps(self) -> None:
        # No rollback: a chain that stopped at step 1 still records that
        # step 0's task exists and finished. Nothing is unwound.
        self.assertUsable(
            chain(
                3,
                cursor=1,
                step_task_ids=("task-a", "task-b", ""),
                status=ChainStatus.FAILED,
            )
        )

    def test_a_cancelled_chain_before_any_step_is_usable(self) -> None:
        self.assertUsable(chain(3, cursor=0, status=ChainStatus.CANCELLED))

    def test_a_step_carries_exactly_its_declared_keys(self) -> None:
        # Six keys, and the ABSENCES are the contract: no classification, no
        # task id, no approval, no billing.
        self.assertEqual(
            set(ChainStep().to_dict()),
            {"objective", "capability", "action", "target", "destination",
             "write_mode", "consumes"},
        )

    def test_a_chain_carries_no_per_step_status_field(self) -> None:
        # Deliberate: "step k succeeded" is storage's answer, re-asked when it
        # matters. A persisted status here would be a forgeable second opinion.
        self.assertEqual(
            set(ChainState().to_dict()),
            {"id", "steps", "step_task_ids", "cursor", "status"},
        )

    def test_a_chain_carries_no_approval_attestation_or_billing_field(self) -> None:
        encoded = json.dumps(chain(3).to_dict())
        for forbidden in ("fingerprint", "attestation", "grant", "nano_usd", "attempt"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded)

    def test_present_means_existence_never_validity(self) -> None:
        # Every chain OBJECT is present, whatever it holds. Validity is a
        # different question with a different answer, and only
        # chain_state_refusal may answer it.
        self.assertTrue(chain(1).present)
        self.assertTrue(ChainState().present)
        self.assertTrue(ChainState.from_dict("garbage").present)
        self.assertIsNotNone(chain_state_refusal(ChainState()))


class DigestTests(ChainTestCase):
    """The declaration identity a later slice binds into an approval display."""

    def test_the_digest_is_deterministic(self) -> None:
        self.assertEqual(chain(3).digest, chain(3).digest)
        self.assertEqual(len(chain(3).digest), 64)

    def test_the_digest_ignores_everything_that_moves(self) -> None:
        # Cursor, created task ids and status all change as the chain runs;
        # the declaration identity must not, or it could not be shown with
        # every step.
        stable = chain(3).digest
        advanced = chain(
            3,
            cursor=2,
            step_task_ids=("task-a", "task-b", "task-c"),
            status=ChainStatus.COMPLETED,
        )
        self.assertEqual(advanced.digest, stable)

    def test_editing_the_declaration_changes_the_digest(self) -> None:
        edited = declaration(3)
        edited[2]["target"] = "elsewhere.md"
        other = chain_steps_from_declaration(edited)
        self.assertNotEqual(chain(3, steps=other).digest, chain(3).digest)

    def test_reordering_the_declaration_changes_the_digest(self) -> None:
        # Order IS the contract for a sequential chain; ``sort_keys`` orders
        # mapping keys and must never reorder the step list.
        forward = chain_steps_from_declaration(declaration(2))
        reversed_steps = (
            replace(forward[1], consumes=CHAIN_NO_PREDECESSOR),
            replace(forward[0], consumes=0),
        )
        swapped = chain(2, steps=reversed_steps)
        self.assertIsNone(chain_state_refusal(swapped))
        self.assertNotEqual(swapped.digest, chain(2).digest)

    def test_the_chain_id_is_part_of_the_identity(self) -> None:
        self.assertNotEqual(chain(3, id="chain-2").digest, chain(3).digest)

    def test_the_digest_survives_a_persistence_round_trip(self) -> None:
        original = chain(3, cursor=1, step_task_ids=("task-a", "task-b", ""))
        restored = ChainState.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(restored.digest, original.digest)


class SerializationTests(ChainTestCase):
    """Deterministic encoding and load -> save -> load integrity."""

    def test_the_encoding_is_byte_identical_for_equal_state(self) -> None:
        first = json.dumps(chain(3).to_dict(), sort_keys=True)
        second = json.dumps(chain(3).to_dict(), sort_keys=True)
        self.assertEqual(first, second)

    def test_a_round_trip_preserves_every_field(self) -> None:
        original = chain(3, cursor=2, step_task_ids=("task-a", "task-b", "task-c"))
        restored = ChainState.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(restored, original)

    def test_a_round_trip_is_stable_across_two_saves(self) -> None:
        original = chain(2, cursor=1, step_task_ids=("task-a", ""))
        once = ChainState.from_dict(original.to_dict())
        twice = ChainState.from_dict(once.to_dict())
        self.assertEqual(twice, original)
        self.assertEqual(json.dumps(twice.to_dict()), json.dumps(original.to_dict()))

    def test_the_steps_survive_as_exact_chain_steps(self) -> None:
        restored = ChainState.from_dict(chain(3).to_dict())
        for step in restored.steps:
            self.assertIs(type(step), ChainStep)
            self.assertIs(type(step.write_mode), str)
            self.assertIs(type(step.consumes), int)


class FailClosedLoadTests(ChainTestCase):
    """A malformed persisted chain is INVALID, never partially usable."""

    def test_a_non_mapping_loads_invalid(self) -> None:
        for payload in ("chain", 3, [], None, True):
            with self.subTest(payload=payload):
                self.assertLoadsInvalid(payload)

    def test_an_unknown_top_level_key_loads_invalid(self) -> None:
        payload = chain(2).to_dict()
        payload["approved"] = True
        self.assertLoadsInvalid(payload)

    def test_an_unknown_status_value_loads_invalid(self) -> None:
        payload = chain(2).to_dict()
        payload["status"] = "running"
        self.assertLoadsInvalid(payload)

    def test_a_non_string_status_loads_invalid(self) -> None:
        for value in (1, None, True, ["active"]):
            with self.subTest(value=value):
                payload = chain(2).to_dict()
                payload["status"] = value
                self.assertLoadsInvalid(payload)

    def test_the_invalid_status_reloads_as_invalid(self) -> None:
        # load -> resave -> load stays invalid: the verdict is durable.
        first = ChainState.from_dict({"status": "invalid"})
        self.assertIs(first.status, ChainStatus.INVALID)
        self.assertLoadsInvalid(first.to_dict())

    def test_a_missing_status_loads_invalid(self) -> None:
        payload = chain(2).to_dict()
        del payload["status"]
        self.assertLoadsInvalid(payload)

    def test_a_missing_step_list_loads_invalid(self) -> None:
        payload = chain(2).to_dict()
        del payload["steps"]
        self.assertLoadsInvalid(payload)

    def test_a_missing_task_id_mapping_loads_invalid(self) -> None:
        payload = chain(2).to_dict()
        del payload["step_task_ids"]
        self.assertLoadsInvalid(payload)

    def test_an_empty_step_list_loads_invalid(self) -> None:
        self.assertLoadsInvalid(
            {"id": "chain-1", "steps": [], "step_task_ids": [], "cursor": 0,
             "status": "active"}
        )

    def test_an_oversized_step_list_loads_invalid(self) -> None:
        payload = chain(3).to_dict()
        payload["steps"].append(dict(READ_STEP))
        payload["step_task_ids"].append("")
        self.assertLoadsInvalid(payload)

    def test_one_malformed_step_loads_the_whole_chain_invalid(self) -> None:
        # Never a shortened chain: dropping the bad step would leave a
        # declaration nobody approved.
        payload = chain(3).to_dict()
        payload["steps"][1]["action"] = ""
        self.assertLoadsInvalid(payload)

    def test_a_step_with_an_unknown_key_loads_invalid(self) -> None:
        payload = chain(2).to_dict()
        payload["steps"][1]["task_id"] = "smuggled"
        self.assertLoadsInvalid(payload)

    def test_a_persisted_self_reference_loads_invalid(self) -> None:
        payload = chain(2).to_dict()
        payload["steps"][1]["consumes"] = 1
        self.assertLoadsInvalid(payload)

    def test_a_persisted_forward_reference_loads_invalid(self) -> None:
        payload = chain(3).to_dict()
        payload["steps"][0]["consumes"] = 2
        self.assertLoadsInvalid(payload)

    def test_a_tampered_cursor_loads_invalid(self) -> None:
        for value in (-1, 3, "1", True, None, 1.0):
            with self.subTest(value=value):
                payload = chain(2).to_dict()
                payload["cursor"] = value
                self.assertLoadsInvalid(payload)

    def test_a_corrupted_task_id_mapping_loads_invalid(self) -> None:
        for mapping in (
            ["task-a"],
            ["task-a", "task-b", "task-c"],
            ["", "task-b"],
            ["task-a", "task-a"],
            [1, ""],
            "task-a",
        ):
            with self.subTest(mapping=mapping):
                payload = chain(2, cursor=1).to_dict()
                payload["step_task_ids"] = mapping
                self.assertLoadsInvalid(payload)

    def test_a_task_id_beyond_the_cursor_loads_invalid(self) -> None:
        payload = chain(2, cursor=0).to_dict()
        payload["step_task_ids"] = ["task-a", "task-b"]
        self.assertLoadsInvalid(payload)

    def test_a_tampered_chain_id_loads_invalid(self) -> None:
        for value in ("", " chain-1", 7, None):
            with self.subTest(value=value):
                payload = chain(2).to_dict()
                payload["id"] = value
                self.assertLoadsInvalid(payload)

    def test_a_step_list_that_is_not_a_list_loads_invalid(self) -> None:
        payload = chain(2).to_dict()
        payload["steps"] = {"0": dict(READ_STEP)}
        self.assertLoadsInvalid(payload)


class SessionPersistenceTests(ChainTestCase):
    """The additive session field: absence means no chain, never a repair."""

    def test_a_new_session_owns_no_chain(self) -> None:
        session = SessionState(objective="ship it")
        self.assertIsNone(session.chain)
        self.assertIsNone(session.to_dict()["chain"])

    def test_a_legacy_session_without_the_key_stays_valid(self) -> None:
        legacy = {
            "objective": "ship it",
            "id": "session-1",
            "active_task_id": None,
            "latest_task_id": None,
            "next_step": None,
            "project_id": None,
            "created_at": "2026-08-27T00:00:00Z",
            "updated_at": "2026-08-27T00:00:00Z",
        }
        restored = SessionState.from_dict(legacy)
        self.assertIsNone(restored.chain)
        self.assertEqual(restored.objective, "ship it")
        self.assertEqual(restored.id, "session-1")

    def test_an_explicit_null_chain_loads_as_no_chain(self) -> None:
        payload = SessionState(objective="ship it").to_dict()
        payload["chain"] = None
        self.assertIsNone(SessionState.from_dict(payload).chain)

    def test_a_session_round_trips_its_chain(self) -> None:
        session = SessionState(objective="ship it")
        session.chain = chain(3, cursor=1, step_task_ids=("task-a", "task-b", ""))
        restored = SessionState.from_dict(json.loads(json.dumps(session.to_dict())))
        self.assertEqual(restored.chain, session.chain)
        assert restored.chain is not None
        self.assertEqual(restored.chain.digest, session.chain.digest)

    def test_a_session_chain_survives_two_round_trips(self) -> None:
        session = SessionState(objective="ship it")
        session.chain = chain(2, cursor=1, step_task_ids=("task-a", ""))
        once = SessionState.from_dict(session.to_dict())
        twice = SessionState.from_dict(once.to_dict())
        self.assertEqual(twice.chain, session.chain)

    def test_a_malformed_session_chain_loads_invalid_not_absent(self) -> None:
        # The dangerous repair: absence reads as "free to start a chain", so a
        # tampered declaration must not clear the way for a new one.
        payload = SessionState(objective="ship it").to_dict()
        payload["chain"] = {"id": "chain-1", "steps": [], "step_task_ids": [],
                            "cursor": 0, "status": "active"}
        restored = SessionState.from_dict(payload)
        self.assertIsNotNone(restored.chain)
        assert restored.chain is not None
        self.assertIs(restored.chain.status, ChainStatus.INVALID)
        # Present, and therefore never "free to start a new chain".
        self.assertTrue(restored.chain.present)
        self.assertIsNotNone(chain_state_refusal(restored.chain))

    def test_a_non_mapping_session_chain_loads_invalid(self) -> None:
        for value in ("chain", 3, [], True):
            with self.subTest(value=value):
                payload = SessionState(objective="ship it").to_dict()
                payload["chain"] = value
                restored = SessionState.from_dict(payload)
                assert restored.chain is not None
                self.assertIs(restored.chain.status, ChainStatus.INVALID)

    def test_an_invalid_session_chain_survives_a_resave(self) -> None:
        payload = SessionState(objective="ship it").to_dict()
        payload["chain"] = {"status": "invalid"}
        once = SessionState.from_dict(payload)
        twice = SessionState.from_dict(once.to_dict())
        assert twice.chain is not None
        self.assertIs(twice.chain.status, ChainStatus.INVALID)

    def test_the_session_key_set_gains_exactly_one_field(self) -> None:
        self.assertEqual(
            set(SessionState(objective="ship it").to_dict()),
            {
                "objective",
                "id",
                "active_task_id",
                "latest_task_id",
                "next_step",
                "project_id",
                "chain",
                "created_at",
                "updated_at",
            },
        )


class InertnessTests(ChainTestCase):
    """S1 declares. It never executes, approves, attests or reserves."""

    def test_a_declaration_creates_no_task_and_no_id(self) -> None:
        state = chain(3)
        self.assertEqual(state.step_task_ids, ("", "", ""))
        self.assertEqual([task_id for task_id in state.step_task_ids if task_id], [])

    def test_the_step_ceiling_is_its_own_policy_surface(self) -> None:
        # Equal to LOCAL_READ_MAX_SOURCES today and free to diverge: the two
        # answer different questions and neither may alias the other.
        self.assertEqual(MAX_CHAIN_STEPS, 3)

    def test_the_declaration_grammar_is_provider_independent(self) -> None:
        # A capability is a symbol, not a vendor: nothing here knows which
        # connector will serve a step, and the registry still decides later.
        payload = [
            {
                "objective": "Do the thing.",
                "capability": "some_future_capability",
                "action": "do",
                "target": "x",
            }
        ]
        self.assertIsNone(chain_declaration_refusal(payload))


class WriteModeTypeTests(ChainTestCase):
    """F1. write_mode is an exact string, ungated by truthiness."""

    def test_the_empty_string_is_the_legitimate_non_write_default(self) -> None:
        self.assertIsNone(chain_declaration_refusal(declaration(1)))
        self.assertEqual(chain_steps_from_declaration(declaration(1))[0].write_mode, "")

    def test_every_falsy_non_string_write_mode_is_refused(self) -> None:
        # The whole finding: a truthiness gate admitted all of these, violating
        # the declared str type inside a chain the contract called valid.
        for value in (None, False, 0, 0.0, [], {}, (), b"", set(), frozenset()):
            with self.subTest(value=value):
                payload = declaration(1)
                payload[0]["write_mode"] = value
                self.assertEqual(
                    chain_declaration_refusal(payload),
                    "chain step 0 is malformed: write mode is not an exact string",
                )

    def test_a_truthy_non_string_write_mode_is_refused(self) -> None:
        for value in (1, True, ["create"], {"create": True}, b"create"):
            with self.subTest(value=value):
                payload = declaration(1)
                payload[0]["write_mode"] = value
                self.assertEqual(
                    chain_declaration_refusal(payload),
                    "chain step 0 is malformed: write mode is not an exact string",
                )

    def test_a_string_subclass_write_mode_is_refused(self) -> None:
        class Mode(str):
            pass

        payload = declaration(3)
        payload[2]["write_mode"] = Mode("create")
        self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_a_falsy_write_mode_no_longer_loads_as_an_active_chain(self) -> None:
        for value in (None, False, 0, []):
            with self.subTest(value=value):
                payload = chain(1).to_dict()
                payload["steps"][0]["write_mode"] = value
                self.assertLoadsInvalid(payload)

    def test_the_write_capability_coupling_is_unchanged(self) -> None:
        missing = declaration(3)
        missing[2]["write_mode"] = ""
        reason = chain_declaration_refusal(missing)
        assert reason is not None
        self.assertIn("declares no write mode", reason)
        stray = declaration(1)
        stray[0]["write_mode"] = "create"
        reason = chain_declaration_refusal(stray)
        assert reason is not None
        self.assertIn("without the write capability", reason)

    def test_every_accepted_chain_has_a_computable_digest(self) -> None:
        # A chain the contract calls valid must never carry a value the
        # canonical encoder cannot serialize.
        for count in (1, 2, 3):
            with self.subTest(count=count):
                self.assertEqual(len(chain(count).digest), 64)
        unencodable = ChainState(
            id="chain-1",
            steps=(
                ChainStep(
                    objective="Read it.",
                    capability="file_read",
                    action="read",
                    write_mode=set(),
                ),
            ),
            step_task_ids=("",),
        )
        self.assertIsNotNone(chain_state_refusal(unencodable))


class _StatefulStep(dict):
    """A mapping whose capability flips after its first read."""

    def __init__(self) -> None:
        super().__init__(READ_STEP)
        self.reads = 0

    def get(self, key, default=None):  # type: ignore[override]
        if key == "capability":
            self.reads += 1
            return LOCAL_READ_CAPABILITY if self.reads <= 1 else LOCAL_WRITE_CAPABILITY
        return super().get(key, default)


class ParseIdentityTests(ChainTestCase):
    """F2. The object judged is the object returned."""

    def test_a_stateful_mapping_cannot_swap_the_returned_step(self) -> None:
        # The gap this closes: the grammar parsed the item to judge it, then
        # the constructor parsed the SAME item again to build the result. A
        # mapping whose second read differs was accepted as one step and
        # handed back as another - one the very same grammar refuses.
        evil = _StatefulStep()
        steps = chain_steps_from_declaration([evil])
        self.assertEqual(evil.reads, 1, "the declaration was parsed twice")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].capability, LOCAL_READ_CAPABILITY)
        self.assertIsNone(chain_step_refusal(steps[0], 0, 1))

    def test_a_returned_step_always_satisfies_the_step_contract(self) -> None:
        for payload in (
            [_StatefulStep()],
            declaration(1),
            declaration(2),
            declaration(3),
        ):
            with self.subTest(payload=type(payload[0]).__name__):
                steps = chain_steps_from_declaration(payload)
                for index, step in enumerate(steps):
                    self.assertIsNone(chain_step_refusal(step, index, len(steps)))

    def test_a_refused_declaration_returns_no_step(self) -> None:
        payload = declaration(2)
        payload[1]["consumes"] = 1
        self.assertIsNotNone(chain_declaration_refusal(payload))
        self.assertEqual(chain_steps_from_declaration(payload), ())

    def test_the_returned_steps_match_a_second_parse_of_plain_input(self) -> None:
        payload = declaration(3)
        self.assertEqual(
            chain_steps_from_declaration(payload),
            tuple(ChainStep.from_dict(item) for item in payload),
        )


class InvalidPresenceTests(ChainTestCase):
    """F3. INVALID is present. Absence is None and only None."""

    def test_the_invalid_sentinel_is_present(self) -> None:
        self.assertTrue(ChainState.from_dict({"status": "invalid"}).present)

    def test_the_house_existence_gate_cannot_confuse_invalid_with_absence(self) -> None:
        # The exact idiom a later consumer would reach for. It must NOT clear
        # a corrupted chain out of the way.
        def free_to_declare(session: SessionState) -> bool:
            return session.chain is None or not session.chain.present

        empty = SessionState(objective="ship it")
        self.assertTrue(free_to_declare(empty))

        payload = empty.to_dict()
        payload["chain"] = {"status": "invalid"}
        corrupted = SessionState.from_dict(payload)
        self.assertFalse(free_to_declare(corrupted))
        assert corrupted.chain is not None
        self.assertIs(corrupted.chain.status, ChainStatus.INVALID)

    def test_a_corrupted_session_chain_is_never_free_to_declare(self) -> None:
        for value in ("garbage", 3, [], True, {}, {"status": "active"}):
            with self.subTest(value=value):
                payload = SessionState(objective="ship it").to_dict()
                payload["chain"] = value
                restored = SessionState.from_dict(payload)
                assert restored.chain is not None
                self.assertTrue(restored.chain.present)
                self.assertIsNotNone(chain_state_refusal(restored.chain))

    def test_validity_is_answered_only_by_the_refusal_contract(self) -> None:
        usable = chain(2)
        self.assertTrue(usable.present)
        self.assertIsNone(chain_state_refusal(usable))
        broken = ChainState.from_dict("garbage")
        self.assertTrue(broken.present)
        self.assertIsNotNone(chain_state_refusal(broken))


class ClassificationAuthorityTests(ChainTestCase):
    """F4. Data classification is policy, and policy is not declarable."""

    def test_classification_is_not_a_declarable_field(self) -> None:
        payload = declaration(1)
        payload[0]["classification"] = DataClassification.INTERNAL.value
        self.assertEqual(
            chain_declaration_refusal(payload),
            "chain step 0 carries an unsupported key",
        )

    def test_a_caller_cannot_claim_a_lower_classification(self) -> None:
        # The authority this closes: declaring PUBLIC for a SECRET target.
        for value in DataClassification:
            with self.subTest(value=value.value):
                payload = declaration(1)
                payload[0]["target"] = "/etc/secrets"
                payload[0]["classification"] = value.value
                self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_a_persisted_classification_loads_the_chain_invalid(self) -> None:
        payload = chain(2).to_dict()
        payload["steps"][1]["classification"] = "public"
        self.assertLoadsInvalid(payload)

    def test_a_step_holds_no_classification_attribute(self) -> None:
        self.assertFalse(hasattr(ChainStep(), "classification"))
        self.assertNotIn("classification", ChainStep().to_dict())

    def test_the_digest_has_no_classification_surface(self) -> None:
        # Two declarations identical but for a classification claim cannot
        # differ, because the claim has no spelling at all.
        encoded = json.dumps(chain(3).to_dict())
        self.assertNotIn("classification", encoded)
        self.assertNotIn("classification", json.dumps(
            {"id": chain(3).id, "steps": [s.to_dict() for s in chain(3).steps]}
        ))


class PostFixIntegrityTests(ChainTestCase):
    """The recheck the review asked for, run as a test rather than a claim."""

    def test_every_accepted_step_is_exact_type_valid(self) -> None:
        for count in (1, 2, 3):
            for index, step in enumerate(chain(count).steps):
                with self.subTest(count=count, index=index):
                    self.assertIs(type(step.capability), str)
                    self.assertIs(type(step.action), str)
                    self.assertIs(type(step.target), str)
                    self.assertIs(type(step.destination), str)
                    self.assertIs(type(step.write_mode), str)
                    self.assertIs(type(step.consumes), int)
                    self.assertIsNot(type(step.consumes), bool)

    def test_semantically_distinct_declarations_have_distinct_digests(self) -> None:
        seen = {}
        variants = {
            "base": declaration(3),
            "other target": declaration(3),
            "other action": declaration(3),
            "other capability": declaration(3),
            "other destination": declaration(3),
            "other objective": declaration(3),
            "other predecessor": declaration(3),
        }
        variants["other target"][0]["target"] = "elsewhere.txt"
        variants["other action"][0]["action"] = "peek"
        variants["other capability"][1]["capability"] = "code_planning"
        variants["other destination"][1]["destination"] = "anthropic/other"
        variants["other objective"][2]["objective"] = "Save it elsewhere."
        variants["other predecessor"][1]["consumes"] = CHAIN_NO_PREDECESSOR
        for name, payload in variants.items():
            steps = chain_steps_from_declaration(payload)
            self.assertEqual(len(steps), 3, name)
            digest = chain(3, steps=steps).digest
            self.assertNotIn(digest, seen, f"{name} collides with {seen.get(digest)}")
            seen[digest] = name

    def test_runtime_progress_still_does_not_alter_the_digest(self) -> None:
        stable = chain(3).digest
        for cursor, ids, status in (
            (0, ("", "", ""), ChainStatus.ACTIVE),
            (1, ("task-a", "", ""), ChainStatus.ACTIVE),
            (2, ("task-a", "task-b", "task-c"), ChainStatus.COMPLETED),
            (1, ("task-a", "task-b", ""), ChainStatus.FAILED),
            (0, ("", "", ""), ChainStatus.CANCELLED),
        ):
            with self.subTest(status=status.value):
                self.assertEqual(
                    chain(3, cursor=cursor, step_task_ids=ids, status=status).digest,
                    stable,
                )

    def test_invalid_still_survives_save_and_load(self) -> None:
        once = ChainState.from_dict("garbage")
        twice = ChainState.from_dict(once.to_dict())
        self.assertIs(twice.status, ChainStatus.INVALID)
        self.assertTrue(twice.present)

    def test_a_legacy_session_with_no_chain_still_means_none(self) -> None:
        restored = SessionState.from_dict(
            {"objective": "ship it", "id": "session-1",
             "created_at": "2026-08-27T00:00:00Z",
             "updated_at": "2026-08-27T00:00:00Z"}
        )
        self.assertIsNone(restored.chain)


# --------------------------------------------------------------------------
# S2. Trusted declaration boundary and the user-visible surface.
# --------------------------------------------------------------------------


def declaration_json(count: int, **document: object) -> str:
    payload = {"steps": declaration(count)}
    payload.update(document)
    return json.dumps(payload)


class BoundaryTestCase(ChainTestCase):
    """A real SQLite store and a real session; no core, connector or model."""

    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self.root)
        self.database = Path(self.root.name) / "jarvis.db"
        self.settings = Settings(memory_database=self.database)
        self.memory = SQLiteMemoryStore(self.database)
        self.session = SessionState(objective="ship it")
        self.memory.save_session(self.session)

    def stored(self) -> SessionState:
        loaded = self.memory.load_session(self.session.id)
        assert loaded is not None
        return loaded

    def raw_session_json(self) -> str:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (self.session.id,)
            ).fetchone()
        assert row is not None
        return row[0]

    def plant_chain(self, chain: ChainState | None) -> None:
        """Put a chain directly into storage, bypassing every API.

        Deliberately raw SQL: generic session persistence can no longer
        create a chain - that is the invariant under test - so a fixture
        needing an incumbent must write one the way a previously declared
        or externally corrupted row would already exist.
        """
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (self.session.id,)
            ).fetchone()
            document = json.loads(row[0])
            document["chain"] = None if chain is None else chain.to_dict()
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(document), self.session.id),
            )
            connection.commit()

    def plant_attempt(self, task_id: str, state: AttemptState) -> None:
        """Plant an attempt row directly.

        open_execution_attempt is create-or-observe and always inserts
        PREPARED, so an IN_FLIGHT or UNKNOWN_OUTCOME row - the two states
        that block a chain - can only be produced the way a crash produces
        one: as an existing row.
        """
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO execution_attempts (id, session_id, task_id, "
                "connector, fingerprint, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"attempt-{task_id}",
                    self.session.id,
                    task_id,
                    "local",
                    "f" * 64,
                    state.value,
                    "2026-08-28T00:00:00Z",
                    "2026-08-28T00:00:00Z",
                ),
            )
            connection.commit()

    def assertRefusedProposal(self, label: str, changes: dict) -> None:
        """One hostile proposal, on a fresh session, refused at both gates."""
        self.session = SessionState(objective=f"shape {label}")
        self.memory.save_session(self.session)
        self.assertEqual(self.declare(declaration_json(3)).exit_code, EXIT_OK)
        chain_state = self.chain_now()
        hostile = replace(self.derive(0), **changes)
        self.assertIsNotNone(
            chain_task_refusal(hostile, chain_state, 0, mode=ChainTaskMode.PROPOSAL),
            f"{label} passed PROPOSAL validation",
        )
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, hostile
        )
        self.assertEqual(verdict, "conflict", f"{label} was materialized")
        self.assertEqual(task_id, "")
        self.assertEqual(self.task_rows(), 0)
        self.assertEqual(self.chain_now().step_task_ids, ("", "", ""))

    def task_rows(self) -> int:
        """Tasks owned by THIS session; the race loop reuses one database."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE session_id = ?",
                (self.session.id,),
            ).fetchone()[0]

    def declare(self, raw: str) -> object:
        return chain_declare_result(self.memory, self.settings, self.session.id, raw)

    def assertNoSideEffects(self) -> None:
        """Declaring is inert: no task, attempt, approval, grant or reservation."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            for table in (
                "tasks",
                "execution_attempts",
                "pending_approvals",
                "permission_grants",
                "attempt_billing",
                "usage_ledger",
                "decision_log",
                "loop_checkpoints",
            ):
                count = connection.execute(
                    # The table names are fixed literals from the tuple above.
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                self.assertEqual(count, 0, f"{table} was written by a declaration")


class DeclarationBoundaryTests(BoundaryTestCase):
    """decode_chain_declaration: the document shell around the S1 grammar."""

    def test_a_one_step_document_is_accepted(self) -> None:
        steps = decode_chain_declaration(declaration_json(1))
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].capability, LOCAL_READ_CAPABILITY)

    def test_a_two_step_document_is_accepted(self) -> None:
        self.assertEqual(len(decode_chain_declaration(declaration_json(2))), 2)

    def test_a_three_step_document_is_accepted(self) -> None:
        self.assertEqual(
            len(decode_chain_declaration(declaration_json(3))), MAX_CHAIN_STEPS
        )

    def test_a_missing_steps_key_is_refused(self) -> None:
        with self.assertRaises(TaskInputError):
            decode_chain_declaration(json.dumps({}))

    def test_a_non_object_root_is_refused(self) -> None:
        for raw in ("[]", '"steps"', "3", "null"):
            with self.subTest(raw=raw), self.assertRaises(TaskInputError):
                decode_chain_declaration(raw)

    def test_invalid_json_is_refused(self) -> None:
        with self.assertRaises(TaskInputError):
            decode_chain_declaration("{not json")

    def test_a_duplicate_key_is_refused(self) -> None:
        with self.assertRaises(TaskInputError):
            decode_chain_declaration('{"steps": [], "steps": []}')

    def test_a_caller_owned_runtime_field_is_refused_by_name(self) -> None:
        # The whole point of the document allowlist: identity and progress
        # belong to JARVIS, so a caller naming one is told so.
        for key, value in (
            ("id", "chain-1"),
            ("cursor", 2),
            ("status", "completed"),
            ("step_task_ids", ["task-a"]),
            ("digest", "0" * 64),
        ):
            with self.subTest(key=key):
                with self.assertRaises(TaskInputError) as caught:
                    decode_chain_declaration(declaration_json(1, **{key: value}))
                self.assertIn(key, str(caught.exception))

    def test_a_step_level_smuggled_field_is_refused(self) -> None:
        for key in ("classification", "task_id", "model_answer_task_id", "chain"):
            with self.subTest(key=key):
                payload = declaration(1)
                payload[0][key] = "x"
                with self.assertRaises(TaskInputError):
                    decode_chain_declaration(json.dumps({"steps": payload}))

    def test_four_steps_are_refused(self) -> None:
        payload = declaration(3) + [dict(READ_STEP)]
        with self.assertRaises(TaskInputError):
            decode_chain_declaration(json.dumps({"steps": payload}))

    def test_self_forward_and_nested_references_stay_refused(self) -> None:
        selfref = declaration(2)
        selfref[1]["consumes"] = 1
        forward = declaration(3)
        forward[0]["consumes"] = 2
        nested = declaration(1)
        nested[0]["steps"] = declaration(1)
        for payload in (selfref, forward, nested):
            with (
                self.subTest(payload=payload[0].get("action")),
                self.assertRaises(TaskInputError),
            ):
                decode_chain_declaration(json.dumps({"steps": payload}))

    def test_the_boundary_reuses_the_one_step_grammar(self) -> None:
        # Same verdict from the document boundary and from the S1 grammar, so
        # the two can never drift into disagreeing about a step.
        payload = declaration(2)
        payload[1]["target"] = " padded"
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        with self.assertRaises(TaskInputError) as caught:
            decode_chain_declaration(json.dumps({"steps": payload}))
        assert reason is not None
        self.assertIn(reason, str(caught.exception))


class ChainCreationTests(BoundaryTestCase):
    """chain_declare_result: what a session gains, and what it does not."""

    def test_declaring_a_one_step_chain_persists_it(self) -> None:
        result = self.declare(declaration_json(1))
        self.assertEqual(result.exit_code, EXIT_OK)
        chain_block = result.payload["chain"]
        stored = self.stored().chain
        assert stored is not None
        self.assertEqual(len(stored.steps), 1)
        self.assertIs(stored.status, ChainStatus.ACTIVE)
        self.assertEqual(stored.cursor, 0)
        self.assertEqual(stored.step_task_ids, ("",))
        self.assertEqual(chain_block["digest"], stored.digest)
        self.assertNoSideEffects()

    def test_declaring_a_two_step_chain_persists_it(self) -> None:
        self.assertEqual(self.declare(declaration_json(2)).exit_code, EXIT_OK)
        stored = self.stored().chain
        assert stored is not None
        self.assertEqual(stored.step_task_ids, ("", ""))
        self.assertNoSideEffects()

    def test_declaring_a_three_step_chain_persists_it(self) -> None:
        self.assertEqual(self.declare(declaration_json(3)).exit_code, EXIT_OK)
        stored = self.stored().chain
        assert stored is not None
        self.assertEqual(len(stored.steps), MAX_CHAIN_STEPS)
        self.assertEqual(stored.step_task_ids, ("", "", ""))
        self.assertIsNone(chain_state_refusal(stored))
        self.assertNoSideEffects()

    def test_the_chain_id_is_generated_bounded_and_not_caller_controlled(self) -> None:
        first = ChainState.declared(chain_steps_from_declaration(declaration(1)))
        second = ChainState.declared(chain_steps_from_declaration(declaration(1)))
        self.assertNotEqual(first.id, second.id)
        for candidate in (first, second):
            self.assertIsNone(chain_id_refusal(candidate.id))
            self.assertLessEqual(len(candidate.id), CHAIN_ID_MAX_CHARS)
        self.declare(declaration_json(1))
        stored = self.stored().chain
        assert stored is not None
        self.assertNotIn("chain-1", stored.id)

    def test_the_declared_constructor_is_the_only_initial_shape(self) -> None:
        steps = chain_steps_from_declaration(declaration(3))
        fresh = ChainState.declared(steps)
        self.assertEqual(fresh.steps, steps)
        self.assertEqual(fresh.step_task_ids, ("", "", ""))
        self.assertEqual(fresh.cursor, 0)
        self.assertIs(fresh.status, ChainStatus.ACTIVE)
        self.assertIsNone(chain_state_refusal(fresh))

    def test_an_unknown_session_is_refused(self) -> None:
        result = chain_declare_result(
            self.memory, self.settings, "no-such-session", declaration_json(1)
        )
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)
        self.assertEqual(result.payload["error"]["code"], "unknown_session")

    def test_a_declaration_round_trips_through_storage(self) -> None:
        self.declare(declaration_json(3))
        reloaded = SQLiteMemoryStore(self.database).load_session(self.session.id)
        assert reloaded is not None and reloaded.chain is not None
        original = self.stored().chain
        assert original is not None
        self.assertEqual(reloaded.chain, original)
        self.assertEqual(reloaded.chain.digest, original.digest)


class OneChainRuleTests(BoundaryTestCase):
    """One active chain per session, and INVALID is not a free slot."""

    def test_a_second_declaration_is_refused(self) -> None:
        self.assertEqual(self.declare(declaration_json(1)).exit_code, EXIT_OK)
        first = self.stored().chain
        assert first is not None
        second = self.declare(declaration_json(2))
        self.assertEqual(second.exit_code, EXIT_INVALID_INPUT)
        self.assertIn("already owns a chain", second.payload["error"]["message"])
        still = self.stored().chain
        assert still is not None
        self.assertEqual(still, first)

    def test_a_declaration_against_an_invalid_chain_is_refused(self) -> None:
        # The F3 rule, enforced: a corrupted chain is PRESENT, so it is not a
        # blank slate that a new declaration may quietly occupy.
        self.plant_chain(ChainState.from_dict("garbage"))
        before = self.raw_session_json()
        result = self.declare(declaration_json(1))
        self.assertEqual(result.exit_code, EXIT_INVALID_INPUT)
        self.assertEqual(self.raw_session_json(), before)
        current = self.stored().chain
        assert current is not None
        self.assertIs(current.status, ChainStatus.INVALID)

    def test_a_terminal_chain_still_blocks_a_second_declaration(self) -> None:
        # Replacement policy for a finished chain is deliberately undefined,
        # so it fails closed rather than guessing.
        for status in (
            ChainStatus.COMPLETED,
            ChainStatus.FAILED,
            ChainStatus.CANCELLED,
        ):
            with self.subTest(status=status.value):
                self.plant_chain(
                    chain(1, cursor=0, step_task_ids=("task-a",), status=status)
                )
                self.assertEqual(
                    self.declare(declaration_json(1)).exit_code, EXIT_INVALID_INPUT
                )


class NoPartialMutationTests(BoundaryTestCase):
    """A refused declaration leaves the persisted session byte-identical."""

    def test_a_malformed_declaration_does_not_touch_the_session(self) -> None:
        before = self.raw_session_json()
        for raw in (
            "{not json",
            json.dumps({}),
            json.dumps({"steps": []}),
            json.dumps({"steps": declaration(3) + [dict(READ_STEP)]}),
            declaration_json(1, id="chain-1"),
            declaration_json(1, cursor=1),
        ):
            with self.subTest(raw=raw[:40]):
                result = self.declare(raw)
                self.assertEqual(result.exit_code, EXIT_INVALID_INPUT)
                self.assertEqual(self.raw_session_json(), before)
                self.assertIsNone(self.stored().chain)
                self.assertIsNone(result.payload["chain"])
        self.assertNoSideEffects()

    def test_a_smuggled_step_field_does_not_touch_the_session(self) -> None:
        before = self.raw_session_json()
        payload = declaration(1)
        payload[0]["classification"] = "public"
        result = self.declare(json.dumps({"steps": payload}))
        self.assertEqual(result.exit_code, EXIT_INVALID_INPUT)
        self.assertEqual(self.raw_session_json(), before)
        self.assertIsNone(self.stored().chain)

    def test_a_refused_second_declaration_does_not_touch_the_session(self) -> None:
        self.declare(declaration_json(1))
        before = self.raw_session_json()
        self.assertEqual(
            self.declare(declaration_json(3)).exit_code, EXIT_INVALID_INPUT
        )
        self.assertEqual(self.raw_session_json(), before)

    def test_a_legacy_session_without_a_chain_still_declares(self) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (self.session.id,)
            ).fetchone()
            legacy = json.loads(row[0])
            legacy.pop("chain", None)
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(legacy), self.session.id),
            )
            connection.commit()
        self.assertIsNone(self.stored().chain)
        self.assertEqual(self.declare(declaration_json(2)).exit_code, EXIT_OK)


class EnvelopeSurfaceTests(BoundaryTestCase):
    """The public chain block: presentation only, deterministic, fixed-key."""

    def test_the_output_schema_is_six(self) -> None:
        self.assertEqual(6, OUTPUT_SCHEMA_VERSION)

    def test_a_session_with_no_chain_renders_explicit_absence(self) -> None:
        result = chain_show_result(self.memory, self.settings, self.session.id)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertIn("chain", result.payload)
        self.assertIsNone(result.payload["chain"])
        self.assertEqual(result.payload["schema_version"], 6)

    def test_the_envelope_always_carries_the_chain_key(self) -> None:
        payload = build_envelope("run", self.settings.permission_mode)
        self.assertIn("chain", payload)
        self.assertIsNone(payload["chain"])

    def test_a_present_chain_renders_its_descriptive_state(self) -> None:
        self.declare(declaration_json(3))
        payload = chain_show_result(
            self.memory, self.settings, self.session.id
        ).payload
        block = payload["chain"]
        stored = self.stored().chain
        assert stored is not None
        self.assertEqual(
            set(block),
            {
                "id",
                "digest",
                "status",
                "usable",
                "cursor",
                "steps",
                "step_task_ids",
                "verdict",
            },
        )
        self.assertEqual(block["id"], stored.id)
        self.assertEqual(block["digest"], stored.digest)
        self.assertEqual(block["status"], "active")
        self.assertTrue(block["usable"])
        self.assertEqual(block["cursor"], 0)
        self.assertEqual(block["step_task_ids"], ["", "", ""])
        self.assertEqual([step["action"] for step in block["steps"]],
                         ["read", "analyze", "write"])

    def test_an_invalid_chain_renders_as_present_and_unusable(self) -> None:
        # It must NOT render as null: null is absence, and absence would tell
        # the reader this session is free to declare a chain.
        self.plant_chain(ChainState.from_dict("garbage"))
        block = chain_show_result(
            self.memory, self.settings, self.session.id
        ).payload["chain"]
        self.assertIsNotNone(block)
        self.assertEqual(block["status"], "invalid")
        self.assertFalse(block["usable"])
        self.assertEqual(block["steps"], [])

    def test_the_chain_block_carries_no_authority_field(self) -> None:
        self.declare(declaration_json(3))
        encoded = json.dumps(
            chain_show_result(self.memory, self.settings, self.session.id).payload
        )
        for forbidden in ("classification", "fingerprint", "attestation", "grant",
                          "nano_usd", "approved"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded)

    def test_the_rendered_envelope_is_deterministic(self) -> None:
        self.declare(declaration_json(3))
        first = chain_show_result(self.memory, self.settings, self.session.id)
        second = chain_show_result(self.memory, self.settings, self.session.id)
        self.assertEqual(first.render(), second.render())
        self.assertEqual(first.payload, second.payload)

    def test_an_unknown_session_cannot_be_shown(self) -> None:
        result = chain_show_result(self.memory, self.settings, "no-such-session")
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)


class CliSurfaceTests(BoundaryTestCase):
    """The CLI declares and shows. It can do nothing else to a chain."""

    def _run(self, argv: list[str]) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(argv, settings=self.settings)
        return code, json.loads(buffer.getvalue())

    def test_the_cli_declares_and_shows_a_chain(self) -> None:
        document = Path(self.root.name) / "chain.json"
        document.write_text(declaration_json(2), encoding="utf-8")
        code, payload = self._run(
            ["chain", "declare", "--session-id", self.session.id,
             "--chain-json", str(document)]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["command"], "chain-declare")
        self.assertEqual(payload["schema_version"], 6)
        self.assertEqual(len(payload["chain"]["steps"]), 2)
        code, shown = self._run(
            ["chain", "show", "--session-id", self.session.id]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(shown["chain"]["id"], payload["chain"]["id"])
        self.assertNoSideEffects()

    def test_the_cli_refuses_a_malformed_declaration(self) -> None:
        document = Path(self.root.name) / "bad.json"
        document.write_text('{"steps": []}', encoding="utf-8")
        code, payload = self._run(
            ["chain", "declare", "--session-id", self.session.id,
             "--chain-json", str(document)]
        )
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertIsNone(payload["chain"])
        self.assertIsNone(self.stored().chain)

    def test_the_cli_requires_a_sub_command(self) -> None:
        code, payload = self._run(["chain"])
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertIn("declare", payload["error"]["message"])

    def test_the_cli_exposes_no_chain_advance_or_execute_verb(self) -> None:
        # S2 declares and shows. Advancing, approving and executing are S3.
        for verb in ("advance", "run", "step", "execute", "approve", "retry"):
            with self.subTest(verb=verb):
                code, _payload = self._run(
                    ["chain", verb, "--session-id", self.session.id]
                )
                self.assertNotEqual(code, EXIT_OK)

    def test_showing_a_chain_builds_no_core_or_connector(self) -> None:
        # Structural inertness: the chain path never reaches the app factory,
        # so no AgentCore, registry or connector is constructed at all.
        def explode(_settings: Settings) -> object:
            raise AssertionError("the chain path built an application")

        document = Path(self.root.name) / "chain.json"
        document.write_text(declaration_json(1), encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                ["chain", "declare", "--session-id", self.session.id,
                 "--chain-json", str(document)],
                settings=self.settings,
                app_factory=explode,
            )
        self.assertEqual(code, EXIT_OK)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli_main(
                    ["chain", "show", "--session-id", self.session.id],
                    settings=self.settings,
                    app_factory=explode,
                ),
                EXIT_OK,
            )


class InertDeclarationTests(BoundaryTestCase):
    """Declaring a chain creates nothing that can act."""

    def test_a_declaration_writes_only_the_session_row(self) -> None:
        self.declare(declaration_json(3))
        self.assertNoSideEffects()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            sessions = connection.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0]
        self.assertEqual(sessions, 1)

    def test_a_declaration_creates_no_step_task_id(self) -> None:
        self.declare(declaration_json(3))
        stored = self.stored().chain
        assert stored is not None
        self.assertEqual([task_id for task_id in stored.step_task_ids if task_id], [])

    def test_the_session_active_task_pointer_is_untouched(self) -> None:
        before = self.stored()
        self.declare(declaration_json(2))
        after = self.stored()
        self.assertEqual(after.active_task_id, before.active_task_id)
        self.assertEqual(after.latest_task_id, before.latest_task_id)
        self.assertEqual(after.next_step, before.next_step)
        self.assertEqual(after.project_id, before.project_id)
        self.assertEqual(after.objective, before.objective)


# --------------------------------------------------------------------------
# S2 correction. Storage-owned atomic chain ownership.
# --------------------------------------------------------------------------


class AtomicDeclarationTests(BoundaryTestCase):
    """Exactly one declarer wins, decided by SQLite and not by Python."""

    def _race(self, attempt: int) -> tuple[list[str], str | None]:
        """Two threads declare different chains at a coordinated instant."""
        barrier = threading.Barrier(2)
        verdicts: list[str] = []
        lock = threading.Lock()

        def declare(count: int) -> None:
            # A SEPARATE store per thread: one shared Python object could hide
            # the race behind an accidental in-process serialisation.
            store = SQLiteMemoryStore(self.database)
            steps = chain_steps_from_declaration(declaration(count))
            candidate = ChainState.declared(steps)
            assert candidate is not None
            barrier.wait(timeout=10)
            verdict = store.declare_chain_if_absent(self.session.id, candidate)
            with lock:
                verdicts.append(verdict)

        threads = [
            threading.Thread(target=declare, args=(count,)) for count in (1, 3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), f"thread hung on attempt {attempt}")
        stored = self.stored().chain
        return verdicts, None if stored is None else stored.digest

    def test_two_concurrent_declarers_cannot_both_win(self) -> None:
        # Repeated, because a single pass proves nothing about a race. Each
        # round uses a fresh session so the previous winner is not the reason
        # the next loser loses.
        for attempt in range(12):
            with self.subTest(attempt=attempt):
                self.session = SessionState(objective=f"race {attempt}")
                self.memory.save_session(self.session)
                verdicts, digest = self._race(attempt)
                self.assertEqual(len(verdicts), 2)
                self.assertEqual(
                    sorted(verdicts),
                    ["declared", "occupied"],
                    f"attempt {attempt} produced {verdicts}",
                )
                self.assertIsNotNone(digest)

    def test_two_concurrent_app_declarations_cannot_both_win(self) -> None:
        """The whole boundary, not just the primitive, must be race-safe."""
        for attempt in range(8):
            with self.subTest(attempt=attempt):
                self.session = SessionState(objective=f"app race {attempt}")
                self.memory.save_session(self.session)
                barrier = threading.Barrier(2)
                codes: list[int] = []
                lock = threading.Lock()

                # Bound explicitly rather than closed over: a closure
                # capturing the loop-scoped objects would read whichever
                # round happened to be current when the thread ran.
                def declare(
                    count: int,
                    gate: threading.Barrier = barrier,
                    guard: threading.Lock = lock,
                    sink: list[int] = codes,
                    session_id: str = self.session.id,
                ) -> None:
                    store = SQLiteMemoryStore(self.database)
                    raw = declaration_json(count)
                    gate.wait(timeout=10)
                    result = chain_declare_result(
                        store, self.settings, session_id, raw
                    )
                    with guard:
                        sink.append(result.exit_code)

                threads = [
                    threading.Thread(target=declare, args=(count,))
                    for count in (1, 3)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(
                    sorted(codes), [EXIT_OK, EXIT_INVALID_INPUT]
                )
                stored = self.stored().chain
                assert stored is not None
                self.assertIsNone(chain_state_refusal(stored))

    def test_the_winner_is_the_chain_that_is_persisted(self) -> None:
        one = ChainState.declared(chain_steps_from_declaration(declaration(1)))
        three = ChainState.declared(chain_steps_from_declaration(declaration(3)))
        assert one is not None and three is not None
        self.assertEqual(
            self.memory.declare_chain_if_absent(self.session.id, one), "declared"
        )
        self.assertEqual(
            self.memory.declare_chain_if_absent(self.session.id, three), "occupied"
        )
        stored = self.stored().chain
        assert stored is not None
        self.assertEqual(stored.digest, one.digest)
        self.assertEqual(len(stored.steps), 1)

    def test_an_unknown_session_is_reported_distinctly(self) -> None:
        candidate = ChainState.declared(chain_steps_from_declaration(declaration(1)))
        assert candidate is not None
        self.assertEqual(
            self.memory.declare_chain_if_absent("no-such-session", candidate),
            "unknown",
        )

    def test_every_status_occupies_the_slot(self) -> None:
        candidate = ChainState.declared(chain_steps_from_declaration(declaration(1)))
        assert candidate is not None
        for status in ChainStatus:
            with self.subTest(status=status.value):
                incumbent = (
                    ChainState.from_dict("garbage")
                    if status is ChainStatus.INVALID
                    else chain(1, cursor=0, step_task_ids=("task-a",), status=status)
                )
                self.plant_chain(incumbent)
                self.assertEqual(
                    self.memory.declare_chain_if_absent(self.session.id, candidate),
                    "occupied",
                )

    def test_a_failed_write_rolls_back_completely(self) -> None:
        before = self.raw_session_json()
        candidate = ChainState.declared(chain_steps_from_declaration(declaration(2)))
        assert candidate is not None

        original = SQLiteMemoryStore._upsert_session

        def explode(connection, session, **kwargs):  # type: ignore[no-untyped-def]
            original(connection, session, **kwargs)
            raise sqlite3.OperationalError("forced failure after the write")

        # staticmethod(): a bare function would be rebound as an instance
        # method and receive ``self`` as the connection.
        with (
            mock.patch.object(
                SQLiteMemoryStore, "_upsert_session", staticmethod(explode)
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            self.memory.declare_chain_if_absent(self.session.id, candidate)
        self.assertEqual(self.raw_session_json(), before)
        self.assertIsNone(self.stored().chain)


class StaleSessionWriteTests(BoundaryTestCase):
    """A caller-held session is never authority over a newer persisted chain."""

    def test_a_stale_save_after_a_declaration_preserves_the_chain(self) -> None:
        # Interleaving A: load, declare, then save the pre-declaration object.
        stale = self.stored()
        self.assertIsNone(stale.chain)
        self.assertEqual(self.declare(declaration_json(3)).exit_code, EXIT_OK)
        winner = self.stored().chain
        assert winner is not None
        stale.next_step = "something unrelated"
        self.memory.save_session(stale)
        survived = self.stored().chain
        assert survived is not None
        self.assertEqual(survived, winner)
        self.assertEqual(survived.digest, winner.digest)
        self.assertEqual(self.stored().next_step, "something unrelated")

    def test_a_stale_object_cannot_replace_the_incumbent(self) -> None:
        # Interleaving C: the stale object carries a DIFFERENT chain.
        self.assertEqual(self.declare(declaration_json(1)).exit_code, EXIT_OK)
        winner = self.stored().chain
        assert winner is not None
        impostor = ChainState.declared(chain_steps_from_declaration(declaration(3)))
        assert impostor is not None
        stale = self.stored()
        stale.chain = impostor
        self.memory.save_session(stale)
        survived = self.stored().chain
        assert survived is not None
        self.assertEqual(survived.digest, winner.digest)
        self.assertNotEqual(survived.digest, impostor.digest)

    def test_a_stale_none_cannot_clear_the_incumbent(self) -> None:
        # Interleaving D: the stale object carries no chain at all.
        self.assertEqual(self.declare(declaration_json(2)).exit_code, EXIT_OK)
        winner = self.stored().chain
        assert winner is not None
        stale = self.stored()
        stale.chain = None
        self.memory.save_session(stale)
        survived = self.stored().chain
        assert survived is not None
        self.assertEqual(survived, winner)

    def test_a_generic_save_cannot_create_a_chain(self) -> None:
        forged = ChainState.declared(chain_steps_from_declaration(declaration(1)))
        assert forged is not None
        session = self.stored()
        session.chain = forged
        self.memory.save_session(session)
        self.assertIsNone(self.stored().chain)
        # And the caller's own object is corrected to what storage holds, so
        # it stops carrying a value storage just refused to honour.
        self.assertIsNone(session.chain)

    def test_an_ordinary_session_without_a_chain_still_saves(self) -> None:
        # Interleaving E: nothing about ordinary session persistence changes.
        session = self.stored()
        session.next_step = "carry on"
        session.active_task_id = "task-1"
        session.latest_task_id = "task-1"
        self.memory.save_session(session)
        reloaded = self.stored()
        self.assertEqual(reloaded.next_step, "carry on")
        self.assertEqual(reloaded.active_task_id, "task-1")
        self.assertEqual(reloaded.latest_task_id, "task-1")
        self.assertIsNone(reloaded.chain)

    def test_unrelated_session_fields_still_update_beside_a_chain(self) -> None:
        self.assertEqual(self.declare(declaration_json(1)).exit_code, EXIT_OK)
        winner = self.stored().chain
        assert winner is not None
        session = self.stored()
        session.next_step = "still moving"
        session.active_task_id = "task-9"
        self.memory.save_session(session)
        reloaded = self.stored()
        self.assertEqual(reloaded.next_step, "still moving")
        self.assertEqual(reloaded.active_task_id, "task-9")
        assert reloaded.chain is not None
        self.assertEqual(reloaded.chain, winner)

    def test_an_unreadable_row_is_not_an_empty_slot(self) -> None:
        # Fail closed: a row whose JSON cannot be read must not read as
        # "free to declare a chain".
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                ("{not json", self.session.id),
            )
            connection.commit()
        candidate = ChainState.declared(chain_steps_from_declaration(declaration(1)))
        assert candidate is not None
        self.assertEqual(
            self.memory.declare_chain_if_absent(self.session.id, candidate),
            "occupied",
        )


class DeclaredConstructorTests(ChainTestCase):
    """The trusted constructor never returns a state its own contract refuses."""

    def test_a_valid_declaration_produces_a_usable_chain(self) -> None:
        for count in (1, 2, 3):
            with self.subTest(count=count):
                built = ChainState.declared(
                    chain_steps_from_declaration(declaration(count))
                )
                assert built is not None
                self.assertIsNone(chain_state_refusal(built))
                self.assertEqual(len(built.digest), 64)
                self.assertEqual(built.step_task_ids, ("",) * count)
                self.assertEqual(built.cursor, 0)
                self.assertIs(built.status, ChainStatus.ACTIVE)

    def test_no_step_is_refused(self) -> None:
        self.assertIsNone(ChainState.declared(()))
        self.assertIsNone(ChainState.declared([]))

    def test_more_than_the_ceiling_is_refused(self) -> None:
        steps = chain_steps_from_declaration(declaration(3))
        self.assertIsNone(ChainState.declared(steps + (steps[0],)))

    def test_a_malformed_step_is_refused(self) -> None:
        for step in (
            ChainStep(),
            ChainStep(objective="Read it.", capability="file_read"),
            ChainStep(objective="Read it.", capability="", action="read"),
            ChainStep(objective="", capability="file_read", action="read"),
            ChainStep(objective="   ", capability="file_read", action="read"),
            ChainStep(objective="Read it.", capability="file_read", action=" read"),
            ChainStep(objective="Write it.", capability="file_write", action="write",
                      target="x"),
            ChainStep(objective="Read it.", capability="file_read", action="read",
                      write_mode="create"),
            ChainStep(objective="Read it.", capability="file_read", action="read",
                      target="a\x00b"),
        ):
            with self.subTest(step=step):
                self.assertIsNone(ChainState.declared((step,)))

    def test_a_wrong_exact_type_is_refused(self) -> None:
        for step in (
            ChainStep(objective="Read it.", capability="file_read", action="read",
                      consumes=True),
            ChainStep(objective="Read it.", capability="file_read", action="read",
                      write_mode=None),
            ChainStep(objective="Read it.", capability="file_read", action="read",
                      write_mode=0),
            ChainStep(objective="Read it.", capability="file_read", action="read",
                      target=b"bytes"),
            ChainStep(objective="Read it.", capability=1, action="read"),
            ChainStep(objective=7, capability="file_read", action="read"),
        ):
            with self.subTest(step=step):
                self.assertIsNone(ChainState.declared((step,)))

    def test_a_non_chain_step_object_is_refused(self) -> None:
        for item in ({"capability": "file_read"}, "file_read", 3, None, object()):
            with self.subTest(item=item):
                self.assertIsNone(ChainState.declared((item,)))

    def test_an_invalid_predecessor_is_refused(self) -> None:
        first = ChainStep(objective="Read it.", capability="file_read", action="read")
        for consumes in (0, 1, 5, -2):
            with self.subTest(consumes=consumes):
                second = ChainStep(
                    objective="Analyze it.",
                    capability="code_analysis",
                    action="x",
                    consumes=consumes,
                )
                if consumes == 0:
                    self.assertIsNotNone(ChainState.declared((first, second)))
                else:
                    self.assertIsNone(ChainState.declared((first, second)))

    def test_a_non_serializable_write_mode_is_refused(self) -> None:
        # The exact P2 case: a state whose digest could not be computed must
        # not exist, rather than fail later at the approval display.
        for value in (set(), frozenset(), object(), 1.5):
            with self.subTest(value=type(value).__name__):
                self.assertIsNone(
                    ChainState.declared(
                        (
                            ChainStep(
                                objective="Read it.",
                                capability="file_read",
                                action="read",
                                write_mode=value,
                            ),
                        )
                    )
                )

    def test_a_non_iterable_argument_is_refused(self) -> None:
        for value in (3, None, object()):
            with self.subTest(value=value):
                self.assertIsNone(ChainState.declared(value))

    def test_every_returned_state_passes_refusal_and_digests(self) -> None:
        built = [
            ChainState.declared(chain_steps_from_declaration(declaration(count)))
            for count in (1, 2, 3)
        ]
        for candidate in built:
            assert candidate is not None
            self.assertIsNone(chain_state_refusal(candidate))
            self.assertEqual(len(candidate.digest), 64)
        digests = {candidate.digest for candidate in built if candidate}
        self.assertEqual(len(digests), 3)

    def test_each_call_mints_a_fresh_identity(self) -> None:
        steps = chain_steps_from_declaration(declaration(1))
        ids = {
            candidate.id
            for candidate in (ChainState.declared(steps) for _ in range(8))
            if candidate is not None
        }
        self.assertEqual(len(ids), 8)


class AtomicInertnessTests(BoundaryTestCase):
    """The atomic primitive is still inert: it creates nothing that can act."""

    def test_a_declaration_through_storage_creates_no_side_effect_row(self) -> None:
        candidate = ChainState.declared(chain_steps_from_declaration(declaration(3)))
        assert candidate is not None
        self.assertEqual(
            self.memory.declare_chain_if_absent(self.session.id, candidate),
            "declared",
        )
        self.assertNoSideEffects()

    def test_a_refused_declaration_creates_no_side_effect_row(self) -> None:
        self.declare(declaration_json(1))
        self.declare(declaration_json(2))
        self.assertNoSideEffects()


# --------------------------------------------------------------------------
# S3 prerequisite. Declared objective, chain write policy, producer aperture.
# --------------------------------------------------------------------------


class ObjectiveDeclarationTests(ChainTestCase):
    """The one caller-controlled intent field a Task cannot do without."""

    def test_a_declared_objective_survives_verbatim(self) -> None:
        payload = declaration(1)
        payload[0]["objective"] = "  Read the notes, carefully.  "
        step = chain_steps_from_declaration(payload)[0]
        # Preserved, not stripped: an objective is prose a person wrote, and
        # for a model step it reaches the payload verbatim.
        self.assertEqual(step.objective, "  Read the notes, carefully.  ")

    def test_an_objective_survives_declaration_persistence_and_reload(self) -> None:
        text = "Summarize notes.txt into three bullet points."
        payload = declaration(2)
        payload[1]["objective"] = text
        steps = chain_steps_from_declaration(payload)
        built = ChainState.declared(steps)
        assert built is not None
        restored = ChainState.from_dict(json.loads(json.dumps(built.to_dict())))
        self.assertEqual(restored.steps[1].objective, text)
        self.assertEqual(restored, built)
        self.assertEqual(restored.digest, built.digest)

    def test_the_objective_changes_the_declaration_digest(self) -> None:
        base = chain_steps_from_declaration(declaration(2))
        edited = declaration(2)
        edited[1]["objective"] = "Something else entirely."
        other = chain_steps_from_declaration(edited)
        self.assertNotEqual(
            chain(2, steps=other).digest, chain(2, steps=base).digest
        )

    def test_the_objective_is_part_of_the_serialized_step(self) -> None:
        self.assertIn("objective", ChainStep().to_dict())
        encoded = json.dumps(chain(3).to_dict())
        self.assertIn("Read the project notes.", encoded)

    def test_a_missing_objective_is_refused(self) -> None:
        payload = declaration(1)
        del payload[0]["objective"]
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("objective is blank", reason)

    def test_a_blank_objective_is_refused(self) -> None:
        for value in ("", " ", "\t", "\n", "   \t  "):
            with self.subTest(value=repr(value)):
                payload = declaration(1)
                payload[0]["objective"] = value
                self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_a_non_string_objective_is_refused(self) -> None:
        for value in (1, True, None, ["read"], {"o": "read"}, b"read"):
            with self.subTest(value=value):
                payload = declaration(1)
                payload[0]["objective"] = value
                reason = chain_declaration_refusal(payload)
                self.assertIsNotNone(reason)
                assert reason is not None
                self.assertIn("objective is not a string", reason)

    def test_a_nul_bearing_objective_is_refused(self) -> None:
        payload = declaration(1)
        payload[0]["objective"] = "read\x00it"
        self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_an_objective_at_its_ceiling_is_accepted_and_beyond_it_refused(
        self,
    ) -> None:
        self.assertIsNone(chain_objective_refusal("a" * CHAIN_OBJECTIVE_MAX_CHARS))
        self.assertIsNotNone(
            chain_objective_refusal("a" * (CHAIN_OBJECTIVE_MAX_CHARS + 1))
        )
        payload = declaration(1)
        payload[0]["objective"] = "a" * (CHAIN_OBJECTIVE_MAX_CHARS + 1)
        self.assertIsNotNone(chain_declaration_refusal(payload))

    def test_the_ceiling_is_its_own_policy_surface(self) -> None:
        self.assertEqual(CHAIN_OBJECTIVE_MAX_CHARS, 480)

    def test_a_malformed_persisted_objective_invalidates_the_whole_chain(
        self,
    ) -> None:
        for value in ("", "   ", 7, None, "o\x00"):
            with self.subTest(value=value):
                payload = chain(2).to_dict()
                payload["steps"][1]["objective"] = value
                self.assertLoadsInvalid(payload)

    def test_a_persisted_oversized_objective_invalidates_the_chain(self) -> None:
        payload = chain(1).to_dict()
        payload["steps"][0]["objective"] = "a" * (CHAIN_OBJECTIVE_MAX_CHARS + 1)
        self.assertLoadsInvalid(payload)


class ChainWritePolicyTests(ChainTestCase):
    """A chain writes only what a chain produced, and never replaces a file."""

    def test_a_chain_may_not_declare_an_overwrite(self) -> None:
        payload = declaration(3)
        payload[2]["write_mode"] = "overwrite"
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("may not declare this write mode", reason)

    def test_a_persisted_overwrite_step_loads_the_chain_invalid(self) -> None:
        payload = chain(3).to_dict()
        payload["steps"][2]["write_mode"] = "overwrite"
        self.assertLoadsInvalid(payload)

    def test_the_enabled_chain_write_modes_are_create_only(self) -> None:
        self.assertEqual(CHAIN_ENABLED_WRITE_MODES, ("create",))
        # And the ordinary standalone policy is untouched: M10 still stands.
        self.assertIn("overwrite", LOCAL_WRITE_ENABLED_MODES)

    def test_there_is_no_content_field(self) -> None:
        self.assertNotIn("content", ChainStep().to_dict())
        self.assertFalse(hasattr(ChainStep(), "content"))
        payload = declaration(3)
        payload[2]["content"] = "bytes the caller wants written"
        self.assertEqual(
            chain_declaration_refusal(payload),
            "chain step 2 carries an unsupported key",
        )

    def test_a_create_step_must_consume_a_producer(self) -> None:
        payload = declaration(3)
        payload[2]["consumes"] = CHAIN_NO_PREDECESSOR
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("writes without consuming a producer", reason)

    def test_a_lone_create_step_is_refused(self) -> None:
        self.assertIsNotNone(chain_declaration_refusal([dict(WRITE_STEP)]))

    def test_a_create_step_consuming_a_non_producer_is_refused(self) -> None:
        # The read step declares no outbound destination, so it cannot be the
        # source of a model answer and cannot supply the write's bytes.
        payload = declaration(3)
        payload[2]["consumes"] = 0
        reason = chain_declaration_refusal(payload)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("declares no outbound destination", reason)

    def test_the_canonical_read_model_create_chain_is_accepted(self) -> None:
        self.assertIsNone(chain_declaration_refusal(declaration(3)))
        steps = chain_steps_from_declaration(declaration(3))
        self.assertEqual(steps[2].capability, LOCAL_WRITE_CAPABILITY)
        self.assertEqual(steps[2].write_mode, "create")
        self.assertEqual(steps[2].consumes, 1)
        self.assertTrue(steps[1].destination)
        built = ChainState.declared(steps)
        assert built is not None
        self.assertIsNone(chain_state_refusal(built))

    def test_a_persisted_create_without_a_producer_loads_invalid(self) -> None:
        payload = chain(3).to_dict()
        payload["steps"][2]["consumes"] = 0
        self.assertLoadsInvalid(payload)

    def test_non_write_steps_stay_declarable_without_a_producer(self) -> None:
        self.assertIsNone(chain_declaration_refusal(declaration(1)))
        self.assertIsNone(chain_declaration_refusal(declaration(2)))
        self.assertIsNone(
            chain_declaration_refusal([dict(MODEL_STEP, consumes=CHAIN_NO_PREDECESSOR)])
        )

    def test_self_and_forward_predecessor_rules_are_unchanged(self) -> None:
        selfref = declaration(3)
        selfref[2]["consumes"] = 2
        forward = declaration(3)
        forward[0]["consumes"] = 1
        for payload in (selfref, forward):
            with self.subTest(consumes=payload[0]["consumes"]):
                self.assertIsNotNone(chain_declaration_refusal(payload))


class ClassificationPolicyTests(ChainTestCase):
    """Classification stays policy-owned, and M11 chains stay INTERNAL."""

    def test_a_caller_still_cannot_declare_a_classification(self) -> None:
        for value in DataClassification:
            with self.subTest(value=value.value):
                payload = declaration(1)
                payload[0]["classification"] = value.value
                self.assertEqual(
                    chain_declaration_refusal(payload),
                    "chain step 0 carries an unsupported key",
                )

    def test_the_declaration_carries_no_classification_surface(self) -> None:
        self.assertNotIn("classification", json.dumps(chain(3).to_dict()))
        # The allowlist itself is pinned behaviourally above: a declared
        # classification is refused by name, whatever its value.


# --------------------------------------------------------------------------
# S3A. Trusted derivation, atomic materialization, progress CAS.
# --------------------------------------------------------------------------


class _EgressConnector(BaseConnector):
    """A provider-neutral stand-in that DECLARES external egress."""

    def __init__(self, name: str = "planner") -> None:
        self.manifest = ConnectorManifest(
            name=name,
            capabilities=("code_analysis", "code_planning"),
            default_side_effect=SideEffectProfile(
                effect=SideEffect.EXTERNAL_EGRESS,
                reversible=False,
                always_requires_approval=True,
            ),
        )

    def execute(self, task, session):  # pragma: no cover - never run in S3A
        raise AssertionError("S3A must not execute a connector")


class _LocalConnector(BaseConnector):
    """A stand-in that does NOT declare external egress."""

    def __init__(self, name: str = "local", capability: str = "file_read") -> None:
        self.manifest = ConnectorManifest(
            name=name,
            capabilities=(capability,),
            default_side_effect=SideEffectProfile(effect=SideEffect.LOCAL_READ),
        )

    def execute(self, task, session):  # pragma: no cover - never run in S3A
        raise AssertionError("S3A must not execute a connector")


class MaterializationTestCase(BoundaryTestCase):
    """A real store, a real registry, a declared chain. No execution."""

    def setUp(self) -> None:
        super().setUp()
        self.registry = ToolRegistry()
        self.registry.register(_EgressConnector())
        self.registry.register(_LocalConnector())
        self.core = AgentCore(
            memory=self.memory, registry=self.registry, settings=self.settings
        )
        self.assertEqual(self.declare(declaration_json(3)).exit_code, EXIT_OK)

    def chain_now(self) -> ChainState:
        stored = self.stored().chain
        assert stored is not None
        return stored

    def derive(self, index: int) -> Task:
        chain_state = self.chain_now()
        task, reason = self.core.build_chain_step_task(
            self.stored(), chain_state, index
        )
        self.assertIsNone(reason, f"derivation refused: {reason}")
        assert task is not None
        return task

    def materialize(self, index: int) -> tuple[str, str]:
        chain_state = self.chain_now()
        task = self.derive(index)
        return self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, index, task
        )

    def complete_task(self, task_id: str, **fields: object) -> None:
        """Drive a step task to a terminal STORED state without executing.

        A completed local READ is given the snapshot a real read would have
        produced, unless the caller pins ``local_read`` itself. Completing one
        WITHOUT a snapshot is not a state the product can reach - the M8
        aperture needs it and storage now proves it before binding a consumer
        - so a fixture that skipped it was testing an impossible row.
        """
        task = self.memory.load_task(task_id)
        assert task is not None
        task.status = TaskStatus.COMPLETED
        task.stage = LoopStage.COMPLETED
        if task.capability == LOCAL_READ_CAPABILITY and "local_read" not in fields:
            task.local_read = canonical_read_snapshot()
        for key, value in fields.items():
            setattr(task, key, value)
        self.memory.save_task(self.session.id, task)

    def finish_model_step(self, task_id: str) -> None:
        answer = build_model_answer("A three bullet summary of the notes.")
        self.complete_task(
            task_id, selected_connector="planner", model_answer=answer
        )

    def advance(self, index: int) -> str:
        chain_state = self.chain_now()
        return self.memory.advance_chain_step(
            self.session.id, chain_state.id, chain_state.digest, index
        )

class ChainStepDerivationTests(MaterializationTestCase):
    """Every field comes from the frozen declaration or from trusted policy."""

    def test_the_first_step_derives_a_read_task(self) -> None:
        task = self.derive(0)
        step = self.chain_now().steps[0]
        self.assertEqual(task.objective, step.objective)
        self.assertEqual(task.capability, LOCAL_READ_CAPABILITY)
        self.assertEqual(task.scope.action, step.action)
        self.assertEqual(task.scope.target, step.target)
        self.assertEqual(task.scope.destination, step.destination)
        self.assertIs(task.local_read_context_kind, LocalReadContextKind.NONE)
        self.assertEqual(task.write_mode, "")
        self.assertIs(task.write_source, WriteSource.DIRECT)

    def test_the_objective_is_copied_verbatim(self) -> None:
        self.assertEqual(self.derive(0).objective, "Read the project notes.")
        self.assertEqual(
            self.derive(0).objective, self.chain_now().steps[0].objective
        )

    def test_the_classification_is_explicitly_internal(self) -> None:
        for index in (0,):
            with self.subTest(index=index):
                self.assertIs(
                    self.derive(index).scope.data_classification,
                    DataClassification.INTERNAL,
                )

    def test_the_derived_task_carries_chain_provenance(self) -> None:
        chain_state = self.chain_now()
        task = self.derive(0)
        self.assertTrue(task.chain_binding.present)
        self.assertEqual(task.chain_binding.chain_id, chain_state.id)
        self.assertEqual(task.chain_binding.chain_digest, chain_state.digest)
        self.assertEqual(task.chain_binding.step_index, 0)
        self.assertIsNone(
            chain_task_refusal(
                task, chain_state, 0, mode=ChainTaskMode.PROPOSAL
            )
        )

    def test_the_model_step_consumes_the_read_through_the_m8_aperture(self) -> None:
        verdict, read_id = self.materialize(0)
        self.assertEqual(verdict, "created")
        self.complete_task(read_id, selected_connector="local")
        self.assertEqual(self.advance(0), "advanced")
        task = self.derive(1)
        self.assertIs(
            task.local_read_context_kind, LocalReadContextKind.REFERENCES
        )
        self.assertEqual(task.local_read_source_task_ids, (read_id,))
        self.assertEqual(task.write_source_task_id, "")

    def test_the_create_step_consumes_the_model_through_the_m7_aperture(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        _verdict, model_id = self.materialize(1)
        self.finish_model_step(model_id)
        self.advance(1)
        task = self.derive(2)
        self.assertIs(task.write_source, WriteSource.MODEL_ANSWER)
        self.assertEqual(task.write_source_task_id, model_id)
        self.assertEqual(task.write_mode, "create")
        self.assertEqual(task.write_content, "")
        self.assertEqual(task.local_read_source_task_ids, ())

    def test_a_predecessor_that_did_not_complete_is_refused(self) -> None:
        self.materialize(0)
        _task, reason = self.core.build_chain_step_task(
            self.stored(), replace(self.chain_now(), cursor=1), 1
        )
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("did not complete", reason)

    def test_a_producer_with_a_destination_but_no_egress_manifest_is_refused(
        self,
    ) -> None:
        # The whole point: a DECLARED destination is not producer authority.
        # This model step declares one, completed, and still cannot produce
        # write bytes, because the connector that actually ran does not
        # declare external egress.
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        _verdict, model_id = self.materialize(1)
        self.complete_task(
            model_id,
            selected_connector="local",
            model_answer=build_model_answer("bytes"),
        )
        self.advance(1)
        _task, reason = self.core.build_chain_step_task(
            self.stored(), self.chain_now(), 2
        )
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("not an external-model connector", reason)
        self.assertTrue(self.chain_now().steps[1].destination)

    def test_a_producer_with_no_selected_connector_is_refused(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        _verdict, model_id = self.materialize(1)
        self.complete_task(
            model_id,
            selected_connector=None,
            model_answer=build_model_answer("b"),
        )
        self.advance(1)
        _task, reason = self.core.build_chain_step_task(
            self.stored(), self.chain_now(), 2
        )
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("names no producer", reason)

    def test_a_producer_carrying_no_model_answer_is_refused(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        _verdict, model_id = self.materialize(1)
        self.complete_task(model_id, selected_connector="planner")
        self.advance(1)
        _task, reason = self.core.build_chain_step_task(
            self.stored(), self.chain_now(), 2
        )
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("carries no model answer", reason)

    def test_a_predecessor_from_another_session_is_refused(self) -> None:
        other = SessionState(objective="elsewhere")
        self.memory.save_session(other)
        stranger = Task(
            objective="o",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(action="read", target="notes.txt"),
            status=TaskStatus.COMPLETED,
            selected_connector="local",
        )
        self.memory.save_task(other.id, stranger)
        forged = replace(
            self.chain_now(), step_task_ids=(stranger.id, "", ""), cursor=1
        )
        _task, reason = self.core.build_chain_step_task(self.stored(), forged, 1)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("another session", reason)

    def test_an_ordinary_non_chain_predecessor_is_refused(self) -> None:
        ordinary = Task(
            objective="o",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(action="read", target="notes.txt"),
            status=TaskStatus.COMPLETED,
            selected_connector="local",
        )
        self.memory.save_task(self.session.id, ordinary)
        forged = replace(
            self.chain_now(), step_task_ids=(ordinary.id, "", ""), cursor=1
        )
        _task, reason = self.core.build_chain_step_task(self.stored(), forged, 1)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("not this chain's step task", reason)

    def test_a_step_index_outside_the_chain_is_refused(self) -> None:
        for index in (-1, 3, 99, True, "0"):
            with self.subTest(index=index):
                _task, reason = self.core.build_chain_step_task(
                    self.stored(), self.chain_now(), index
                )
                self.assertIsNotNone(reason)

    def test_an_unusable_chain_is_refused(self) -> None:
        _task, reason = self.core.build_chain_step_task(
            self.stored(), ChainState.from_dict("garbage"), 0
        )
        self.assertIsNotNone(reason)


class AtomicMaterializationTests(MaterializationTestCase):
    """Creation and binding are one effect, or neither."""

    def test_the_first_step_materializes_exactly_one_task(self) -> None:
        verdict, task_id = self.materialize(0)
        self.assertEqual(verdict, "created")
        self.assertEqual(self.task_rows(), 1)
        self.assertEqual(self.chain_now().step_task_ids, (task_id, "", ""))
        stored = self.memory.load_task(task_id)
        assert stored is not None
        self.assertEqual(stored.chain_binding.step_index, 0)

    def test_repeated_materialization_observes_the_same_task(self) -> None:
        _verdict, first = self.materialize(0)
        for _ in range(4):
            verdict, again = self.materialize(0)
            self.assertEqual(verdict, "observed")
            self.assertEqual(again, first)
        self.assertEqual(self.task_rows(), 1)

    def test_a_later_step_cannot_be_pre_created(self) -> None:
        for index in (1, 2):
            with self.subTest(index=index):
                chain_state = self.chain_now()
                task = Task(
                    objective="x",
                    capability="code_analysis",
                    scope=ActionScope(action="analyze"),
                    chain_binding=ChainBinding(
                        chain_state.id, chain_state.digest, index
                    ),
                )
                verdict, task_id = self.memory.materialize_chain_step(
                    self.session.id, chain_state.id, chain_state.digest, index, task
                )
                self.assertEqual(verdict, "conflict")
                self.assertEqual(task_id, "")
        self.assertEqual(self.task_rows(), 0)

    def test_a_stale_digest_is_refused(self) -> None:
        chain_state = self.chain_now()
        task = self.derive(0)
        verdict, _id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, "0" * 64, 0, task
        )
        self.assertEqual(verdict, "conflict")
        self.assertEqual(self.task_rows(), 0)

    def test_a_stale_chain_id_is_refused(self) -> None:
        chain_state = self.chain_now()
        verdict, _id = self.memory.materialize_chain_step(
            self.session.id, "other-chain", chain_state.digest, 0, self.derive(0)
        )
        self.assertEqual(verdict, "conflict")
        self.assertEqual(self.task_rows(), 0)

    def test_a_task_whose_binding_does_not_match_is_refused(self) -> None:
        chain_state = self.chain_now()
        for binding in (
            ChainBinding(),
            ChainBinding("other", chain_state.digest, 0),
            ChainBinding(chain_state.id, "1" * 64, 0),
            ChainBinding(chain_state.id, chain_state.digest, 1),
        ):
            with self.subTest(binding=binding):
                task = replace(self.derive(0), chain_binding=binding)
                verdict, _id = self.memory.materialize_chain_step(
                    self.session.id, chain_state.id, chain_state.digest, 0, task
                )
                self.assertEqual(verdict, "conflict")
        self.assertEqual(self.task_rows(), 0)

    def test_an_unknown_session_is_refused(self) -> None:
        chain_state = self.chain_now()
        verdict, _id = self.memory.materialize_chain_step(
            "no-such-session", chain_state.id, chain_state.digest, 0, self.derive(0)
        )
        self.assertEqual(verdict, "unknown")

    def test_a_reused_task_id_is_refused(self) -> None:
        _verdict, first = self.materialize(0)
        self.complete_task(first, selected_connector="local")
        self.advance(0)
        chain_state = self.chain_now()
        clash = replace(
            self.derive(1),
            id=first,
            chain_binding=ChainBinding(chain_state.id, chain_state.digest, 1),
        )
        verdict, _id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 1, clash
        )
        self.assertEqual(verdict, "conflict")
        self.assertEqual(self.task_rows(), 1)

    def test_a_forced_failure_leaves_neither_task_nor_binding(self) -> None:
        chain_state = self.chain_now()
        task = self.derive(0)
        original = SQLiteMemoryStore._upsert_session

        def explode(connection, session, **kwargs):  # type: ignore[no-untyped-def]
            original(connection, session, **kwargs)
            raise sqlite3.OperationalError("forced failure after both writes")

        with (
            mock.patch.object(
                SQLiteMemoryStore, "_upsert_session", staticmethod(explode)
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            self.memory.materialize_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0, task
            )
        # Neither an orphan task row nor a binding survives the rollback.
        self.assertEqual(self.task_rows(), 0)
        self.assertEqual(self.chain_now().step_task_ids, ("", "", ""))

    def test_two_concurrent_materializers_converge_on_one_task(self) -> None:
        for attempt in range(8):
            with self.subTest(attempt=attempt):
                self.session = SessionState(objective=f"materialize {attempt}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                chain_state = self.chain_now()
                barrier = threading.Barrier(2)
                results: list[tuple[str, str]] = []
                guard = threading.Lock()

                def race(
                    gate: threading.Barrier = barrier,
                    lock: threading.Lock = guard,
                    sink: list = results,
                    session_id: str = self.session.id,
                    state: ChainState = chain_state,
                ) -> None:
                    store = SQLiteMemoryStore(self.database)
                    core = AgentCore(
                        memory=store, registry=self.registry, settings=self.settings
                    )
                    session = store.load_session(session_id)
                    assert session is not None
                    task, reason = core.build_chain_step_task(session, state, 0)
                    assert task is not None, reason
                    gate.wait(timeout=10)
                    outcome = store.materialize_chain_step(
                        session_id, state.id, state.digest, 0, task
                    )
                    with lock:
                        sink.append(outcome)

                threads = [threading.Thread(target=race) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                    self.assertFalse(thread.is_alive())
                verdicts = sorted(verdict for verdict, _id in results)
                self.assertEqual(verdicts, ["created", "observed"])
                bound = {task_id for _v, task_id in results}
                self.assertEqual(len(bound), 1, "the two racers bound different tasks")
                self.assertEqual(
                    self.chain_now().step_task_ids[0], next(iter(bound))
                )
                self.assertEqual(self.task_rows(), 1)


class ChainProgressCasTests(MaterializationTestCase):
    """Storage decides advancement, from the stored task status alone."""

    def _settle_step(self, index: int) -> str:
        verdict, task_id = self.materialize(index)
        self.assertIn(verdict, ("created", "observed"))
        if index == 1:
            self.finish_model_step(task_id)
        else:
            self.complete_task(task_id, selected_connector="local")
        return task_id

    def test_a_completed_step_advances_the_cursor(self) -> None:
        self._settle_step(0)
        self.assertEqual(self.advance(0), "advanced")
        self.assertEqual(self.chain_now().cursor, 1)
        self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)

    def test_the_final_completed_step_completes_the_chain(self) -> None:
        self._settle_step(0)
        self.advance(0)
        self._settle_step(1)
        self.advance(1)
        self._settle_step(2)
        self.assertEqual(self.advance(2), "completed")
        chain_state = self.chain_now()
        self.assertIs(chain_state.status, ChainStatus.COMPLETED)
        self.assertEqual(chain_state.cursor, 2)
        self.assertTrue(all(chain_state.step_task_ids))

    def test_a_non_terminal_task_blocks_the_chain_in_place(self) -> None:
        self.materialize(0)
        self.assertEqual(self.advance(0), "blocked")
        self.assertEqual(self.chain_now().cursor, 0)
        self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)

    def test_an_unbound_step_cannot_advance(self) -> None:
        self.assertEqual(self.advance(0), "unbound")
        self.assertEqual(self.chain_now().cursor, 0)

    def test_an_in_flight_attempt_blocks_the_chain(self) -> None:
        _verdict, task_id = self.materialize(0)
        self.complete_task(task_id, selected_connector="local")
        self.plant_attempt(task_id, AttemptState.IN_FLIGHT)
        self.assertEqual(self.advance(0), "blocked")
        self.assertEqual(self.chain_now().cursor, 0)

    def test_an_unknown_outcome_attempt_blocks_the_chain(self) -> None:
        _verdict, task_id = self.materialize(0)
        self.complete_task(task_id, selected_connector="local")
        self.plant_attempt(task_id, AttemptState.UNKNOWN_OUTCOME)
        self.assertEqual(self.advance(0), "blocked")
        self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)

    def test_a_failed_step_fails_the_chain(self) -> None:
        _verdict, task_id = self.materialize(0)
        task = self.memory.load_task(task_id)
        assert task is not None
        task.status = TaskStatus.FAILED
        task.stage = LoopStage.FAILED
        self.memory.save_task(self.session.id, task)
        self.assertEqual(self.advance(0), "failed")
        self.assertIs(self.chain_now().status, ChainStatus.FAILED)
        self.assertEqual(self.chain_now().cursor, 0)

    def test_a_cancelled_step_cancels_the_chain(self) -> None:
        _verdict, task_id = self.materialize(0)
        task = self.memory.load_task(task_id)
        assert task is not None
        task.status = TaskStatus.CANCELLED
        task.stage = LoopStage.CANCELLED
        self.memory.save_task(self.session.id, task)
        self.assertEqual(self.advance(0), "cancelled")
        self.assertIs(self.chain_now().status, ChainStatus.CANCELLED)

    def test_a_stale_cursor_cannot_advance(self) -> None:
        self._settle_step(0)
        self.assertEqual(self.advance(0), "advanced")
        # The same driver tries again with its old index.
        chain_state = self.chain_now()
        self.assertEqual(
            self.memory.advance_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0
            ),
            "conflict",
        )
        self.assertEqual(self.chain_now().cursor, 1)

    def test_a_stale_digest_cannot_advance(self) -> None:
        self._settle_step(0)
        chain_state = self.chain_now()
        self.assertEqual(
            self.memory.advance_chain_step(
                self.session.id, chain_state.id, "0" * 64, 0
            ),
            "conflict",
        )
        self.assertEqual(self.chain_now().cursor, 0)

    def test_a_substituted_task_row_cannot_advance(self) -> None:
        _verdict, task_id = self.materialize(0)
        self.complete_task(task_id, selected_connector="local")
        # Swap the binding out from under the row: ownership no longer holds.
        task = self.memory.load_task(task_id)
        assert task is not None
        task.chain_binding = ChainBinding()
        self.memory.save_task(self.session.id, task)
        self.assertEqual(self.advance(0), "conflict")
        self.assertEqual(self.chain_now().cursor, 0)

    def test_a_declaration_drift_cannot_advance(self) -> None:
        _verdict, task_id = self.materialize(0)
        self.complete_task(task_id, selected_connector="local")
        drifted = declaration(3)
        drifted[0]["target"] = "elsewhere.txt"
        session = self.stored()
        assert session.chain is not None
        self.plant_chain(
            replace(session.chain, steps=chain_steps_from_declaration(drifted))
        )
        chain_state = self.chain_now()
        self.assertEqual(
            self.memory.advance_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0
            ),
            "conflict",
        )

    def test_a_generic_stale_save_cannot_change_progress(self) -> None:
        self._settle_step(0)
        stale = self.stored()
        self.advance(0)
        after = self.chain_now()
        stale.chain = replace(after, cursor=0, status=ChainStatus.FAILED)
        stale.next_step = "unrelated"
        self.memory.save_session(stale)
        current = self.chain_now()
        self.assertEqual(current.cursor, 1)
        self.assertIs(current.status, ChainStatus.ACTIVE)
        self.assertEqual(current.step_task_ids, after.step_task_ids)
        self.assertEqual(self.stored().next_step, "unrelated")

    def test_a_completed_chain_refuses_further_progress(self) -> None:
        self._settle_step(0)
        self.advance(0)
        self._settle_step(1)
        self.advance(1)
        self._settle_step(2)
        self.advance(2)
        chain_state = self.chain_now()
        self.assertEqual(
            self.memory.advance_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 2
            ),
            "inactive",
        )
        task = Task(
            objective="x",
            capability="code_analysis",
            scope=ActionScope(action="analyze"),
            chain_binding=ChainBinding(chain_state.id, chain_state.digest, 2),
        )
        self.assertEqual(
            self.memory.materialize_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 2, task
            )[0],
            "inactive",
        )


class ChainCancellationStorageTests(MaterializationTestCase):
    """Cancellation is storage-owned and stops what has not started."""

    def test_cancelling_an_active_chain(self) -> None:
        chain_state = self.chain_now()
        self.assertEqual(
            self.memory.cancel_chain(self.session.id, chain_state.id), "cancelled"
        )
        self.assertIs(self.chain_now().status, ChainStatus.CANCELLED)

    def test_no_step_may_be_materialized_after_cancellation(self) -> None:
        chain_state = self.chain_now()
        task = self.derive(0)
        self.memory.cancel_chain(self.session.id, chain_state.id)
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(verdict, "inactive")
        self.assertEqual(task_id, "")
        self.assertEqual(self.task_rows(), 0)

    def test_cancellation_preserves_completed_steps(self) -> None:
        _verdict, first = self.materialize(0)
        self.complete_task(first, selected_connector="local")
        self.advance(0)
        chain_state = self.chain_now()
        self.memory.cancel_chain(self.session.id, chain_state.id)
        cancelled = self.chain_now()
        self.assertIs(cancelled.status, ChainStatus.CANCELLED)
        self.assertEqual(cancelled.cursor, 1)
        self.assertEqual(cancelled.step_task_ids[0], first)
        self.assertIsNotNone(self.memory.load_task(first))

    def test_cancellation_cannot_be_advanced_over(self) -> None:
        _verdict, task_id = self.materialize(0)
        self.complete_task(task_id, selected_connector="local")
        chain_state = self.chain_now()
        self.memory.cancel_chain(self.session.id, chain_state.id)
        self.assertEqual(
            self.memory.advance_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0
            ),
            "inactive",
        )
        self.assertIs(self.chain_now().status, ChainStatus.CANCELLED)

    def test_cancelling_twice_is_terminal_not_an_error(self) -> None:
        chain_state = self.chain_now()
        self.memory.cancel_chain(self.session.id, chain_state.id)
        self.assertEqual(
            self.memory.cancel_chain(self.session.id, chain_state.id), "terminal"
        )

    def test_cancelling_the_wrong_chain_id_is_refused(self) -> None:
        self.assertEqual(
            self.memory.cancel_chain(self.session.id, "other-chain"), "conflict"
        )
        self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)

    def test_cancelling_an_unknown_session_is_reported(self) -> None:
        self.assertEqual(self.memory.cancel_chain("nope", "chain"), "unknown")

    def test_a_cancel_racing_materialization_never_yields_a_post_cancel_task(
        self,
    ) -> None:
        for attempt in range(8):
            with self.subTest(attempt=attempt):
                self.session = SessionState(objective=f"cancel race {attempt}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                chain_state = self.chain_now()
                task = self.derive(0)
                barrier = threading.Barrier(2)
                outcomes: dict[str, str] = {}
                guard = threading.Lock()

                def cancel(
                    gate: threading.Barrier = barrier,
                    lock: threading.Lock = guard,
                    sink: dict = outcomes,
                    session_id: str = self.session.id,
                    state: ChainState = chain_state,
                ) -> None:
                    store = SQLiteMemoryStore(self.database)
                    gate.wait(timeout=10)
                    with lock:
                        sink["cancel"] = store.cancel_chain(session_id, state.id)

                def make(
                    gate: threading.Barrier = barrier,
                    lock: threading.Lock = guard,
                    sink: dict = outcomes,
                    session_id: str = self.session.id,
                    state: ChainState = chain_state,
                    proposal: Task = task,
                ) -> None:
                    store = SQLiteMemoryStore(self.database)
                    gate.wait(timeout=10)
                    verdict, _id = store.materialize_chain_step(
                        session_id, state.id, state.digest, 0, proposal
                    )
                    with lock:
                        sink["materialize"] = verdict

                threads = [
                    threading.Thread(target=cancel),
                    threading.Thread(target=make),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                    self.assertFalse(thread.is_alive())
                current = self.chain_now()
                if outcomes["materialize"] == "created":
                    # Materialize won the lock: the task exists and the chain
                    # is cancelled AFTER it. Never a task created post-cancel.
                    self.assertEqual(outcomes["cancel"], "cancelled")
                    self.assertTrue(current.step_task_ids[0])
                else:
                    self.assertEqual(outcomes["materialize"], "inactive")
                    self.assertEqual(current.step_task_ids[0], "")
                self.assertIs(current.status, ChainStatus.CANCELLED)


class ChainInertnessTests(MaterializationTestCase):
    """S3A materializes rows. It still executes, approves and bills nothing."""

    def test_materialization_creates_no_execution_or_approval_state(self) -> None:
        self.materialize(0)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            for table in (
                "execution_attempts",
                "pending_approvals",
                "permission_grants",
                "attempt_billing",
                "usage_ledger",
                "decision_log",
            ):
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                self.assertEqual(count, 0, f"{table} was written by S3A")

    def test_a_materialized_task_is_pending_and_unstarted(self) -> None:
        _verdict, task_id = self.materialize(0)
        task = self.memory.load_task(task_id)
        assert task is not None
        self.assertIs(task.status, TaskStatus.PENDING)
        self.assertIs(task.stage, LoopStage.UNDERSTAND)
        self.assertIsNone(task.selected_connector)
        self.assertIsNone(task.result_snapshot)

    def test_the_driver_reaches_the_ordinary_lifecycle_entry_point(self) -> None:
        """S3B added a driver. It must ORCHESTRATE, never execute.

        Pinned structurally: the driver runs a step through ``resume`` -
        the same entry point a standalone task uses - so approval,
        billing and the attempt journal stay the existing authorities.
        A second execution path would have to bypass this.
        """
        self.assertTrue(hasattr(self.core, "run_chain"))
        source = inspect.getsource(AgentCore._run_chain_step)
        self.assertIn("self.resume(", source)
        for forbidden in ("connector.execute", "begin_execution", "_advance("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


# --------------------------------------------------------------------------
# S3A correction. A binding is necessary; it is never sufficient.
# --------------------------------------------------------------------------


#: Every immutable chain-derived authority field, and how to tamper with it.
#: Each entry must independently destroy chain ownership.
AUTHORITY_TAMPERS: tuple[tuple[str, dict], ...] = (
    ("objective", {"objective": "Do something else entirely."}),
    ("capability", {"capability": "code_planning"}),
    ("action", {"scope_action": "peek"}),
    ("target", {"scope_target": "elsewhere.txt"}),
    ("destination", {"scope_destination": "anthropic/claude-code"}),
    ("scope_connector", {"scope_connector": "planner"}),
    ("classification", {"scope_classification": DataClassification.PUBLIC}),
    ("classification-secret", {"scope_classification": DataClassification.SECRET}),
    ("write_mode", {"write_mode": "create"}),
    ("write_source", {"write_source": WriteSource.MODEL_ANSWER}),
    ("write_source_task_id", {"write_source_task_id": "forged-source"}),
    ("read_context_kind", {"local_read_context_kind": LocalReadContextKind.REFERENCES}),
    ("read_source_ids", {"local_read_source_task_ids": ("forged-source",)}),
    ("binding", {"chain_binding": ChainBinding()}),
    ("project_id", {"project_id": "some-project"}),
)


def tampered(task: Task, changes: dict) -> Task:
    """Apply one authority tamper to a task, scope fields included."""
    scope = task.scope
    if "scope_action" in changes:
        scope = replace(scope, action=changes["scope_action"])
    if "scope_target" in changes:
        scope = replace(scope, target=changes["scope_target"])
    if "scope_destination" in changes:
        scope = replace(scope, destination=changes["scope_destination"])
    if "scope_connector" in changes:
        scope = replace(scope, connector=changes["scope_connector"])
    if "scope_classification" in changes:
        scope = replace(scope, data_classification=changes["scope_classification"])
    plain = {
        key: value for key, value in changes.items() if not key.startswith("scope_")
    }
    return replace(task, scope=scope, **plain)


class HostileProposalTests(MaterializationTestCase):
    """F2. The complete proposal is judged before any row exists."""

    def _refuse(self, proposal: Task) -> None:
        chain_state = self.chain_now()
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, proposal
        )
        self.assertEqual(verdict, "conflict")
        self.assertEqual(task_id, "")
        self.assertEqual(self.task_rows(), 0)
        self.assertEqual(self.chain_now().step_task_ids, ("", "", ""))

    def test_every_authority_tamper_is_refused(self) -> None:
        for name, changes in AUTHORITY_TAMPERS:
            with self.subTest(field=name):
                self._refuse(tampered(self.derive(0), changes))

    def test_an_injected_connector_is_refused(self) -> None:
        self._refuse(replace(self.derive(0), selected_connector="planner"))

    def test_a_pre_advanced_proposal_is_refused(self) -> None:
        for changes in (
            {"status": TaskStatus.COMPLETED},
            {"stage": LoopStage.EXECUTE},
            {"result_snapshot": {"outcome": "success"}},
            {"write_content": "bytes"},
            {"plan": ["step"]},
            {"visible_assumption": "assumed"},
            {"cancellation_requested": True},
        ):
            with self.subTest(changes=sorted(changes)):
                self._refuse(replace(self.derive(0), **changes))

    def test_injected_context_or_preferences_are_refused(self) -> None:
        for changes in (
            {"context": {"working_directory": "/tmp"}},
            {"context": {LOCAL_READ_TASK_REFERENCE_KEY: "forged"}},
            {"preferences": {"preferred_connector": "planner"}},
        ):
            with self.subTest(changes=sorted(changes)):
                self._refuse(replace(self.derive(0), **changes))

    def test_an_injected_read_window_is_refused(self) -> None:
        self._refuse(
            replace(
                self.derive(0),
                local_read_kind=LocalReadKind.REGION,
                read_start_line=10,
                read_max_lines=5,
            )
        )

    def test_a_missing_expected_predecessor_source_is_refused(self) -> None:
        # Step 1 legitimately consumes step 0; a proposal that drops the
        # sanctioned read reference is not this chain's step task.
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        chain_state = self.chain_now()
        stripped = replace(
            self.derive(1),
            local_read_context_kind=LocalReadContextKind.NONE,
            local_read_source_task_ids=(),
        )
        verdict, _id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 1, stripped
        )
        self.assertEqual(verdict, "conflict")

    def test_extra_source_ids_are_refused(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        chain_state = self.chain_now()
        extra = replace(
            self.derive(1), local_read_source_task_ids=(read_id, "another-task")
        )
        verdict, _id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 1, extra
        )
        self.assertEqual(verdict, "conflict")

    def test_a_source_from_another_session_is_refused(self) -> None:
        other = SessionState(objective="elsewhere")
        self.memory.save_session(other)
        stranger = Task(
            objective="o",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(action="read"),
            status=TaskStatus.COMPLETED,
        )
        self.memory.save_task(other.id, stranger)
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        chain_state = self.chain_now()
        forged = replace(self.derive(1), local_read_source_task_ids=(stranger.id,))
        verdict, _id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 1, forged
        )
        self.assertEqual(verdict, "conflict")

    def test_a_forged_write_source_on_a_read_step_is_refused(self) -> None:
        self._refuse(
            replace(
                self.derive(0),
                write_source=WriteSource.MODEL_ANSWER,
                write_source_task_id="forged",
            )
        )

    def test_a_genuine_proposal_is_still_accepted(self) -> None:
        verdict, task_id = self.materialize(0)
        self.assertEqual(verdict, "created")
        self.assertTrue(task_id)


class ObservedProofTests(MaterializationTestCase):
    """F3. OBSERVED is a claim about a row, so the row is re-proved."""

    def _observe(self) -> str:
        chain_state = self.chain_now()
        return self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, self.derive(0)
        )[0]

    def test_a_genuine_bound_task_stays_idempotently_observed(self) -> None:
        self.materialize(0)
        for _ in range(3):
            self.assertEqual(self._observe(), "observed")
        self.assertEqual(self.task_rows(), 1)

    def test_a_missing_bound_task_row_is_refused(self) -> None:
        _verdict, task_id = self.materialize(0)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            connection.commit()
        self.assertEqual(self._observe(), "conflict")

    def test_a_bound_task_owned_by_another_session_is_refused(self) -> None:
        _verdict, task_id = self.materialize(0)
        other = SessionState(objective="elsewhere")
        self.memory.save_session(other)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?", (other.id, task_id)
            )
            connection.commit()
        self.assertEqual(self._observe(), "conflict")

    def test_every_authority_tamper_on_the_bound_task_is_refused(self) -> None:
        for name, changes in AUTHORITY_TAMPERS:
            with self.subTest(field=name):
                self.session = SessionState(objective=f"observe {name}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                _verdict, task_id = self.materialize(0)
                stored_task = self.memory.load_task(task_id)
                assert stored_task is not None
                self.memory.save_task(
                    self.session.id, tampered(stored_task, changes)
                )
                self.assertEqual(self._observe(), "conflict")

    def test_a_connector_named_before_plan_is_refused(self) -> None:
        _verdict, task_id = self.materialize(0)
        task = self.memory.load_task(task_id)
        assert task is not None
        # Still at UNDERSTAND: storage can prove this is not lifecycle
        # progression, whatever the connector name happens to be.
        task.selected_connector = "planner"
        self.memory.save_task(self.session.id, task)
        self.assertEqual(self._observe(), "conflict")

    def test_a_wrong_step_task_substituted_into_the_slot_is_refused(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        _verdict, model_id = self.materialize(1)
        # Point step 0's slot at step 1's task. The chain stays structurally
        # valid - one leading id, no duplicate - so only the ownership proof
        # can catch it, which is the point.
        session = self.stored()
        assert session.chain is not None
        self.plant_chain(
            replace(session.chain, step_task_ids=(model_id, "", ""), cursor=0)
        )
        self.assertEqual(self._observe(), "conflict")

    def test_a_declaration_digest_drift_is_refused(self) -> None:
        self.materialize(0)
        drifted = declaration(3)
        drifted[0]["objective"] = "A different intent."
        session = self.stored()
        assert session.chain is not None
        self.plant_chain(
            replace(session.chain, steps=chain_steps_from_declaration(drifted))
        )
        chain_state = self.chain_now()
        verdict, _id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, self.derive(0)
        )
        self.assertEqual(verdict, "conflict")


class LifecycleAwareAuthorityTests(MaterializationTestCase):
    """A genuinely progressing task keeps its authority; a tampered one loses it."""

    def test_a_planned_task_with_a_connector_remains_recognizable(self) -> None:
        _verdict, task_id = self.materialize(0)
        task = self.memory.load_task(task_id)
        assert task is not None
        # Exactly what the ordinary loop does at PLAN.
        task.stage = LoopStage.RISK_PERMISSION
        task.status = TaskStatus.RUNNING
        task.selected_connector = "local"
        task.plan = ["a", "b"]
        task.preferences = {"billing_preflight": "x"}
        self.memory.save_task(self.session.id, task)
        self.assertIsNone(
            chain_task_refusal(
                self.memory.load_task(task_id),
                self.chain_now(),
                0,
                mode=ChainTaskMode.STORED,
            )
        )
        chain_state = self.chain_now()
        self.assertEqual(
            self.memory.materialize_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0, self.derive(0)
            )[0],
            "observed",
        )

    def test_a_completed_task_with_products_remains_recognizable(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        _verdict, model_id = self.materialize(1)
        self.finish_model_step(model_id)
        self.assertIsNone(
            chain_task_refusal(
                self.memory.load_task(model_id),
                self.chain_now(),
                1,
                mode=ChainTaskMode.STORED,
            )
        )
        self.assertEqual(self.advance(1), "advanced")

    def test_the_proposal_mode_still_refuses_a_progressed_task(self) -> None:
        _verdict, task_id = self.materialize(0)
        self.complete_task(task_id, selected_connector="local")
        stored_task = self.memory.load_task(task_id)
        self.assertIsNotNone(
            chain_task_refusal(
                stored_task, self.chain_now(), 0, mode=ChainTaskMode.PROPOSAL
            )
        )

    def test_an_exact_mode_is_required(self) -> None:
        for mode in ("stored", None, 1):
            with self.subTest(mode=mode):
                self.assertIsNotNone(
                    chain_task_refusal(
                        self.derive(0), self.chain_now(), 0, mode=mode
                    )
                )


class TamperedAdvanceTests(MaterializationTestCase):
    """A COMPLETED row whose authority was tampered must never advance."""

    def test_every_authority_tamper_blocks_advancement(self) -> None:
        for name, changes in AUTHORITY_TAMPERS:
            with self.subTest(field=name):
                self.session = SessionState(objective=f"advance {name}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                _verdict, task_id = self.materialize(0)
                self.complete_task(task_id, selected_connector="local")
                stored_task = self.memory.load_task(task_id)
                assert stored_task is not None
                self.memory.save_task(
                    self.session.id, tampered(stored_task, changes)
                )
                # The row still says COMPLETED.
                reloaded = self.memory.load_task(task_id)
                assert reloaded is not None
                self.assertIs(reloaded.status, TaskStatus.COMPLETED)
                self.assertEqual(self.advance(0), "conflict")
                current = self.chain_now()
                self.assertEqual(current.cursor, 0)
                self.assertIs(current.status, ChainStatus.ACTIVE)

    def test_an_untampered_completed_task_still_advances(self) -> None:
        _verdict, task_id = self.materialize(0)
        self.complete_task(task_id, selected_connector="local")
        self.assertEqual(self.advance(0), "advanced")


class ProducerAuthorityTests(MaterializationTestCase):
    """Tampering the producer invalidates the CREATE derivation."""

    def _reach_create_step(self) -> str:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        _verdict, model_id = self.materialize(1)
        self.finish_model_step(model_id)
        self.advance(1)
        return model_id

    def test_a_genuine_producer_derives_the_create_step(self) -> None:
        model_id = self._reach_create_step()
        task, reason = self.core.build_chain_step_task(
            self.stored(), self.chain_now(), 2
        )
        self.assertIsNone(reason)
        assert task is not None
        self.assertEqual(task.write_source_task_id, model_id)

    def test_producer_authority_tampering_invalidates_the_derivation(self) -> None:
        exercised = 0
        for name, changes in AUTHORITY_TAMPERS:
            with self.subTest(field=name):
                self.session = SessionState(objective=f"producer {name}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                model_id = self._reach_create_step()
                producer = self.memory.load_task(model_id)
                assert producer is not None
                mutated = tampered(producer, changes)
                if mutated == producer:
                    # A no-op for THIS step shape - the model step already
                    # declares this destination / read-context kind - so
                    # there is nothing to detect. Not a gap: the same field
                    # is exercised on the read step elsewhere.
                    continue
                exercised += 1
                self.memory.save_task(self.session.id, mutated)
                _task, reason = self.core.build_chain_step_task(
                    self.stored(), self.chain_now(), 2
                )
                self.assertIsNotNone(reason, f"{name} tamper was accepted")
        # The loop must not pass by skipping everything.
        self.assertGreaterEqual(exercised, 10)


class _ChainAuthorityStore:
    """A minimal in-memory MemoryStore used to prove the abstraction is real."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def declare_chain_if_absent(self, session_id: str, chain: ChainState) -> str:
        self.calls.append("declare_chain_if_absent")
        return "declared"

    def materialize_chain_step(
        self,
        session_id: str,
        chain_id: str,
        chain_digest: str,
        step_index: int,
        task: Task,
    ) -> tuple[str, str]:
        self.calls.append("materialize_chain_step")
        return "created", task.id

    def advance_chain_step(
        self, session_id: str, chain_id: str, chain_digest: str, step_index: int
    ) -> str:
        self.calls.append("advance_chain_step")
        return "advanced"

    def cancel_chain(self, session_id: str, chain_id: str) -> str:
        self.calls.append("cancel_chain")
        return "cancelled"


CHAIN_AUTHORITY_METHODS = (
    "declare_chain_if_absent",
    "materialize_chain_step",
    "advance_chain_step",
    "cancel_chain",
)


class MemoryStoreContractTests(ChainTestCase):
    """F4. S3B must be able to depend on MemoryStore, not one implementation."""

    def test_the_protocol_declares_every_chain_authority_method(self) -> None:
        for name in CHAIN_AUTHORITY_METHODS:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(MemoryStore, name),
                    f"MemoryStore does not declare {name}",
                )

    def test_the_sqlite_store_satisfies_the_declared_signatures(self) -> None:
        for name in CHAIN_AUTHORITY_METHODS:
            with self.subTest(name=name):
                declared = inspect.signature(getattr(MemoryStore, name))
                concrete = inspect.signature(getattr(SQLiteMemoryStore, name))
                self.assertEqual(
                    list(declared.parameters), list(concrete.parameters)
                )

    def test_a_non_sqlite_store_can_implement_the_family(self) -> None:
        fake = _ChainAuthorityStore()
        for name in CHAIN_AUTHORITY_METHODS:
            self.assertTrue(callable(getattr(fake, name)))
        task = Task(
            objective="o", capability="file_read", scope=ActionScope(action="read")
        )
        self.assertEqual(fake.declare_chain_if_absent("s", ChainState()), "declared")
        self.assertEqual(
            fake.materialize_chain_step("s", "c", "d", 0, task), ("created", task.id)
        )
        self.assertEqual(fake.advance_chain_step("s", "c", "d", 0), "advanced")
        self.assertEqual(fake.cancel_chain("s", "c"), "cancelled")
        self.assertEqual(sorted(fake.calls), sorted(CHAIN_AUTHORITY_METHODS))

    def test_agent_core_reaches_for_no_sqlite_specific_api(self) -> None:
        # Every memory attribute AgentCore touches must exist on the
        # provider-independent surface, so S3B can depend on the protocol.
        source = inspect.getsource(AgentCore)
        used = set(re.findall(r"self\.memory\.([A-Za-z_][A-Za-z0-9_]*)", source))
        missing = sorted(name for name in used if not hasattr(MemoryStore, name))
        self.assertEqual(missing, [], f"AgentCore uses non-protocol APIs: {missing}")


# --------------------------------------------------------------------------
# S3A final. Frozen scope connector, pristine proposals, PLAN-aware lifecycle.
# --------------------------------------------------------------------------


#: Every runtime/policy field trusted derivation leaves at its canonical
#: default. A proposal carrying anything else is not what derivation produces,
#: so it may not be materialized - and no later stage is trusted to clean it.
PRISTINE_DEFAULTS: tuple[tuple[str, dict], ...] = (
    ("estimate", {"estimate": CostEstimate(maximum_usd=5.0)}),
    ("risk_level", {"risk_level": RiskLevel.HIGH}),
    (
        "side_effect",
        {
            "side_effect": SideEffectProfile(
                effect=SideEffect.LOCAL_WRITE,
                reversible=False,
                always_requires_approval=False,
            )
        },
    ),
    (
        "retrieved_context",
        {"retrieved_context": RetrievedContext(candidate_count=7)},
    ),
    (
        "progress",
        {"progress": TaskProgress(completed_stages=(LoopStage.UNDERSTAND,))},
    ),
    ("status", {"status": TaskStatus.RUNNING}),
    ("stage", {"stage": LoopStage.EXECUTE}),
    ("plan", {"plan": ["smuggled step"]}),
    ("visible_assumption", {"visible_assumption": "smuggled assumption"}),
    ("selected_connector", {"selected_connector": "planner"}),
    ("result_snapshot", {"result_snapshot": {"outcome": "success"}}),
    ("write_content", {"write_content": "smuggled bytes"}),
    ("context", {"context": {"working_directory": "/tmp"}}),
    ("preferences", {"preferences": {"preferred_connector": "planner"}}),
    ("local_read_sources", {"local_read_sources": (LocalReadSnapshot(),)}),
    ("cancellation_requested", {"cancellation_requested": True}),
)


class FrozenScopeConnectorTests(MaterializationTestCase):
    """ActionScope.connector is frozen chain authority and must stay empty."""

    def test_trusted_derivation_leaves_the_scope_connector_empty(self) -> None:
        self.assertEqual(self.derive(0).scope.connector, "")

    def test_a_scope_connector_is_refused_at_materialization(self) -> None:
        chain_state = self.chain_now()
        hostile = tampered(self.derive(0), {"scope_connector": "planner"})
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, hostile
        )
        self.assertEqual(verdict, "conflict")
        self.assertEqual(task_id, "")
        self.assertEqual(self.task_rows(), 0)
        self.assertEqual(self.chain_now().step_task_ids, ("", "", ""))

    def test_a_scope_connector_is_refused_on_the_observed_path(self) -> None:
        _verdict, task_id = self.materialize(0)
        stored_task = self.memory.load_task(task_id)
        assert stored_task is not None
        self.memory.save_task(
            self.session.id, tampered(stored_task, {"scope_connector": "planner"})
        )
        chain_state = self.chain_now()
        self.assertEqual(
            self.memory.materialize_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0, self.derive(0)
            )[0],
            "conflict",
        )

    def test_a_scope_connector_blocks_advancement(self) -> None:
        _verdict, task_id = self.materialize(0)
        self.complete_task(task_id, selected_connector="local")
        stored_task = self.memory.load_task(task_id)
        assert stored_task is not None
        self.memory.save_task(
            self.session.id, tampered(stored_task, {"scope_connector": "planner"})
        )
        reloaded = self.memory.load_task(task_id)
        assert reloaded is not None
        self.assertIs(reloaded.status, TaskStatus.COMPLETED)
        self.assertEqual(self.advance(0), "conflict")
        self.assertEqual(self.chain_now().cursor, 0)
        self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)

    def test_a_scope_connector_invalidates_the_producer_proof(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        _verdict, model_id = self.materialize(1)
        self.finish_model_step(model_id)
        self.advance(1)
        producer = self.memory.load_task(model_id)
        assert producer is not None
        self.memory.save_task(
            self.session.id, tampered(producer, {"scope_connector": "planner"})
        )
        _task, reason = self.core.build_chain_step_task(
            self.stored(), self.chain_now(), 2
        )
        self.assertIsNotNone(reason)

    def test_the_scope_connector_is_not_the_selected_connector(self) -> None:
        # Two different fields answering two different questions. A legitimate
        # post-PLAN selection must not be read as a frozen-scope violation.
        _verdict, task_id = self.materialize(0)
        task = self.memory.load_task(task_id)
        assert task is not None
        task.stage = LoopStage.RISK_PERMISSION
        task.selected_connector = "local"
        self.memory.save_task(self.session.id, task)
        reloaded = self.memory.load_task(task_id)
        assert reloaded is not None
        self.assertEqual(reloaded.scope.connector, "")
        self.assertEqual(reloaded.selected_connector, "local")
        self.assertIsNone(
            chain_task_refusal(
                reloaded, self.chain_now(), 0, mode=ChainTaskMode.STORED
            )
        )


class PristineProposalTests(MaterializationTestCase):
    """A first materialization accepts exactly what derivation produces."""

    def test_every_non_default_runtime_or_policy_field_is_refused(self) -> None:
        for name, changes in PRISTINE_DEFAULTS:
            with self.subTest(field=name):
                # A FRESH session per field. Sharing one would let a single
                # accepted proposal cascade into every later assertion, so a
                # one-field regression would look like fifteen.
                self.session = SessionState(objective=f"pristine {name}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                chain_state = self.chain_now()
                hostile = replace(self.derive(0), **changes)
                self.assertNotEqual(hostile, self.derive(0), f"{name} was a no-op")
                verdict, task_id = self.memory.materialize_chain_step(
                    self.session.id, chain_state.id, chain_state.digest, 0, hostile
                )
                self.assertEqual(verdict, "conflict", f"{name} was accepted")
                self.assertEqual(task_id, "")
                self.assertEqual(self.task_rows(), 0)
                self.assertEqual(self.chain_now().step_task_ids, ("", "", ""))

    def test_the_validator_names_each_pristine_violation(self) -> None:
        chain_state = self.chain_now()
        for name, changes in PRISTINE_DEFAULTS:
            with self.subTest(field=name):
                self.assertIsNotNone(
                    chain_task_refusal(
                        replace(self.derive(0), **changes),
                        chain_state,
                        0,
                        mode=ChainTaskMode.PROPOSAL,
                    )
                )

    def test_a_pristine_proposal_matches_trusted_derivation(self) -> None:
        # The law, stated as a test: what derivation produces is what the
        # boundary accepts, field for field apart from the minted identity.
        derived = self.derive(0)
        self.assertIsNone(
            chain_task_refusal(
                derived, self.chain_now(), 0, mode=ChainTaskMode.PROPOSAL
            )
        )
        self.assertEqual(derived.estimate, CostEstimate())
        self.assertIs(derived.risk_level, RiskLevel.LOW)
        self.assertEqual(derived.side_effect, SideEffectProfile())
        self.assertEqual(derived.retrieved_context, RetrievedContext())
        self.assertEqual(derived.progress, TaskProgress())
        self.assertEqual(derived.scope.connector, "")
        self.assertEqual(self.materialize(0)[0], "created")

    def test_pristine_defaults_are_relaxed_once_the_task_is_stored(self) -> None:
        # These fields are ordinary lifecycle state: pinned at the boundary,
        # then owned by the loop. Pinning them in STORED mode would fail every
        # genuinely running chain task.
        _verdict, task_id = self.materialize(0)
        task = self.memory.load_task(task_id)
        assert task is not None
        task.stage = LoopStage.COST_PREFLIGHT
        task.status = TaskStatus.RUNNING
        task.estimate = CostEstimate(maximum_usd=1.0)
        task.risk_level = RiskLevel.HIGH
        task.side_effect = SideEffectProfile(effect=SideEffect.LOCAL_READ)
        task.progress = TaskProgress(completed_stages=(LoopStage.UNDERSTAND,))
        self.memory.save_task(self.session.id, task)
        self.assertIsNone(
            chain_task_refusal(
                self.memory.load_task(task_id),
                self.chain_now(),
                0,
                mode=ChainTaskMode.STORED,
            )
        )


class SelectedConnectorLifecycleTests(MaterializationTestCase):
    """Selection happens AT plan, so PLAN itself is still pre-selection."""

    def _with_stage(self, task_id: str, stage: LoopStage, connector: object) -> None:
        task = self.memory.load_task(task_id)
        assert task is not None
        task.stage = stage
        task.status = TaskStatus.RUNNING
        task.selected_connector = connector
        self.memory.save_task(self.session.id, task)

    def test_no_pre_plan_stage_may_name_a_connector(self) -> None:
        _verdict, task_id = self.materialize(0)
        for stage in (
            LoopStage.UNDERSTAND,
            LoopStage.CONTEXT_RETRIEVAL,
            LoopStage.CLARIFY_OR_ASSUME,
            LoopStage.PLAN,
        ):
            with self.subTest(stage=stage.value):
                self._with_stage(task_id, stage, "planner")
                self.assertIsNotNone(
                    chain_task_refusal(
                        self.memory.load_task(task_id),
                        self.chain_now(),
                        0,
                        mode=ChainTaskMode.STORED,
                    )
                )
                self.assertEqual(self.advance(0), "conflict")

    def test_plan_itself_is_still_pre_selection(self) -> None:
        # The one-stage-late bug, pinned through the public surface:
        # selection HAPPENS at PLAN and the choice is persisted only on the
        # way out of it, so a task whose stored stage is still PLAN has not
        # selected anything and may not name one.
        _verdict, task_id = self.materialize(0)
        self._with_stage(task_id, LoopStage.PLAN, "planner")
        reason = chain_task_refusal(
            self.memory.load_task(task_id),
            self.chain_now(),
            0,
            mode=ChainTaskMode.STORED,
        )
        self.assertEqual(reason, "task named a connector before it was planned")

    def test_a_post_plan_stage_may_name_a_connector(self) -> None:
        _verdict, task_id = self.materialize(0)
        for stage in (
            LoopStage.RISK_PERMISSION,
            LoopStage.COST_PREFLIGHT,
            LoopStage.TOOL_SELECTION,
            LoopStage.EXECUTE,
        ):
            with self.subTest(stage=stage.value):
                self._with_stage(task_id, stage, "local")
                self.assertIsNone(
                    chain_task_refusal(
                        self.memory.load_task(task_id),
                        self.chain_now(),
                        0,
                        mode=ChainTaskMode.STORED,
                    )
                )

    def test_a_pre_plan_stage_without_a_connector_is_fine(self) -> None:
        _verdict, task_id = self.materialize(0)
        for stage in (LoopStage.CONTEXT_RETRIEVAL, LoopStage.PLAN):
            with self.subTest(stage=stage.value):
                self._with_stage(task_id, stage, None)
                self.assertIsNone(
                    chain_task_refusal(
                        self.memory.load_task(task_id),
                        self.chain_now(),
                        0,
                        mode=ChainTaskMode.STORED,
                    )
                )

    def test_a_malformed_connector_is_refused_at_any_stage(self) -> None:
        _verdict, task_id = self.materialize(0)
        for stage in (LoopStage.RISK_PERMISSION, LoopStage.EXECUTE):
            with self.subTest(stage=stage.value):
                self._with_stage(task_id, stage, "")
                self.assertIsNotNone(
                    chain_task_refusal(
                        self.memory.load_task(task_id),
                        self.chain_now(),
                        0,
                        mode=ChainTaskMode.STORED,
                    )
                )

    def test_which_connector_is_legitimate_stays_deferred(self) -> None:
        # Storage proves SHAPE, not registry legitimacy: a post-PLAN task
        # naming a connector no registry would have chosen still passes the
        # storage-level check, and S3B must re-prove it through AgentCore.
        _verdict, task_id = self.materialize(0)
        self._with_stage(task_id, LoopStage.EXECUTE, "a-connector-that-does-not-exist")
        self.assertIsNone(
            chain_task_refusal(
                self.memory.load_task(task_id),
                self.chain_now(),
                0,
                mode=ChainTaskMode.STORED,
            )
        )


# --------------------------------------------------------------------------
# S3A final. Empty-LOOKING is not the canonical default.
# --------------------------------------------------------------------------


#: Values that pass a truthiness or ``.present`` check yet are NOT the exact
#: canonical default trusted derivation produces. Every entry must be refused,
#: and every entry asserts it really does differ from the default first, so no
#: case can pass because it was a no-op.
NONCANONICAL_DEFAULTS: tuple[tuple[str, dict], ...] = (
    (
        "local_read-not-present-but-not-default",
        {"local_read": LocalReadSnapshot(classification=DataClassification.PUBLIC)},
    ),
    (
        "local_write-not-present-but-not-default",
        {"local_write": LocalWriteRecord(pre_present=True)},
    ),
    (
        "model_answer-not-present-but-not-default",
        {"model_answer": ModelAnswer(content="hidden")},
    ),
    ("plan-empty-tuple", {"plan": ()}),
    ("context-empty-list", {"context": []}),
    ("preferences-empty-list", {"preferences": []}),
    ("local_read_sources-empty-list", {"local_read_sources": []}),
    ("cancellation_requested-zero", {"cancellation_requested": 0}),
    ("write_content-zero", {"write_content": 0}),
    # Additional exact-type gaps found while auditing the same rule.
    ("write_content-false", {"write_content": False}),
    ("write_content-bytes", {"write_content": b""}),
    ("plan-empty-string", {"plan": ""}),
    ("context-empty-tuple", {"context": ()}),
    ("preferences-none", {"preferences": None}),
    ("local_read_sources-empty-string", {"local_read_sources": ""}),
    ("estimate-wrong-type", {"estimate": None}),
    ("side_effect-wrong-type", {"side_effect": None}),
    ("retrieved_context-wrong-type", {"retrieved_context": None}),
    ("progress-wrong-type", {"progress": None}),
    ("local_read-wrong-type", {"local_read": None}),
    ("local_write-wrong-type", {"local_write": None}),
    ("model_answer-wrong-type", {"model_answer": None}),
)


class ExactCanonicalDefaultTests(MaterializationTestCase):
    """PROPOSAL means "IS the trusted initial state", not "looks empty"."""

    def test_the_codex_reproductions_really_are_not_the_default(self) -> None:
        # The premise of the whole battery: each hostile value passes the
        # truthiness or .present check the old code relied on, and is still a
        # different object from the canonical default.
        self.assertFalse(
            LocalReadSnapshot(classification=DataClassification.PUBLIC).present
        )
        self.assertNotEqual(
            LocalReadSnapshot(classification=DataClassification.PUBLIC),
            LocalReadSnapshot(),
        )
        self.assertFalse(LocalWriteRecord(pre_present=True).present)
        self.assertNotEqual(LocalWriteRecord(pre_present=True), LocalWriteRecord())
        self.assertFalse(ModelAnswer(content="hidden").present)
        self.assertNotEqual(ModelAnswer(content="hidden"), ModelAnswer())
        for falsy in ((), [], "", 0, False, b""):
            with self.subTest(falsy=repr(falsy)):
                self.assertFalse(bool(falsy))

    def test_the_present_lookalikes_are_refused(self) -> None:
        """The three Codex reproductions, each a VALID instance.

        Kept separate from the wider battery on purpose: every value here
        is a well-formed object of the right type, so only an exact-default
        comparison can reject it - a ``.present`` check accepts all three,
        and no downstream serialization failure would catch them either.
        """
        chain_state = self.chain_now()
        canonical = self.derive(0)
        for name, changes in (
            (
                "local_read",
                {
                    "local_read": LocalReadSnapshot(
                        classification=DataClassification.PUBLIC
                    )
                },
            ),
            ("local_write", {"local_write": LocalWriteRecord(pre_present=True)}),
            ("model_answer", {"model_answer": ModelAnswer(content="hidden")}),
        ):
            with self.subTest(field=name):
                hostile = replace(canonical, **changes)
                self.assertFalse(getattr(hostile, name).present)
                self.assertIsNotNone(
                    chain_task_refusal(
                        hostile, chain_state, 0, mode=ChainTaskMode.PROPOSAL
                    ),
                    f"{name} lookalike passed PROPOSAL validation",
                )

    def test_every_noncanonical_default_fails_proposal_validation(self) -> None:
        chain_state = self.chain_now()
        canonical = self.derive(0)
        for name, changes in NONCANONICAL_DEFAULTS:
            with self.subTest(field=name):
                hostile = replace(canonical, **changes)
                # Compared by TYPE AND VALUE, per field: whole-task equality
                # cannot see 0 vs False, which is precisely one of the
                # confusions this battery exists to catch.
                for field_name, value in changes.items():
                    original = getattr(canonical, field_name)
                    self.assertNotEqual(
                        (type(value), value),
                        (type(original), original),
                        f"{name} was a no-op",
                    )
                self.assertIsNotNone(
                    chain_task_refusal(
                        hostile, chain_state, 0, mode=ChainTaskMode.PROPOSAL
                    ),
                    f"{name} passed PROPOSAL validation",
                )

    def test_every_noncanonical_default_is_refused_at_materialization(self) -> None:
        for name, changes in NONCANONICAL_DEFAULTS:
            with self.subTest(field=name):
                # A fresh session per case, so one accepted proposal cannot
                # cascade into the rest and inflate the failure count.
                self.session = SessionState(objective=f"exact {name}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                chain_state = self.chain_now()
                hostile = replace(self.derive(0), **changes)
                verdict, task_id = self.memory.materialize_chain_step(
                    self.session.id, chain_state.id, chain_state.digest, 0, hostile
                )
                self.assertEqual(verdict, "conflict", f"{name} was materialized")
                self.assertEqual(task_id, "")
                self.assertEqual(self.task_rows(), 0)
                self.assertEqual(self.chain_now().step_task_ids, ("", "", ""))

    def test_the_canonical_falsy_values_are_still_accepted(self) -> None:
        # The rule must not become "reject everything falsy": the canonical
        # defaults ARE falsy, and they are what derivation produces.
        canonical = self.derive(0)
        self.assertIs(canonical.cancellation_requested, False)
        self.assertEqual(canonical.write_content, "")
        self.assertIs(type(canonical.write_content), str)
        self.assertEqual(canonical.plan, [])
        self.assertIs(type(canonical.plan), list)
        self.assertEqual(canonical.context, {})
        self.assertIs(type(canonical.context), dict)
        self.assertEqual(canonical.preferences, {})
        self.assertIs(type(canonical.preferences), dict)
        self.assertEqual(canonical.local_read_sources, ())
        self.assertIs(type(canonical.local_read_sources), tuple)
        self.assertEqual(canonical.local_read, LocalReadSnapshot())
        self.assertEqual(canonical.local_write, LocalWriteRecord())
        self.assertEqual(canonical.model_answer, ModelAnswer())
        self.assertIsNone(
            chain_task_refusal(
                canonical, self.chain_now(), 0, mode=ChainTaskMode.PROPOSAL
            )
        )
        self.assertEqual(self.materialize(0)[0], "created")

    def test_false_is_accepted_where_zero_is_refused(self) -> None:
        chain_state = self.chain_now()
        canonical = self.derive(0)
        self.assertIsNone(
            chain_task_refusal(
                replace(canonical, cancellation_requested=False),
                chain_state,
                0,
                mode=ChainTaskMode.PROPOSAL,
            )
        )
        for value in (0, 0.0, None, ""):
            with self.subTest(value=repr(value)):
                self.assertIsNotNone(
                    chain_task_refusal(
                        replace(canonical, cancellation_requested=value),
                        chain_state,
                        0,
                        mode=ChainTaskMode.PROPOSAL,
                    )
                )

    def test_the_empty_string_is_accepted_where_other_falsy_values_are_not(
        self,
    ) -> None:
        chain_state = self.chain_now()
        canonical = self.derive(0)
        self.assertIsNone(
            chain_task_refusal(
                replace(canonical, write_content=""),
                chain_state,
                0,
                mode=ChainTaskMode.PROPOSAL,
            )
        )
        for value in (0, False, b"", None, ()):
            with self.subTest(value=repr(value)):
                self.assertIsNotNone(
                    chain_task_refusal(
                        replace(canonical, write_content=value),
                        chain_state,
                        0,
                        mode=ChainTaskMode.PROPOSAL,
                    )
                )

    def test_only_construction_varying_fields_may_differ(self) -> None:
        # The law, stated as a test: two independently derived proposals for
        # the same step differ ONLY in id, created_at and updated_at.
        first = self.derive(0)
        second = self.derive(0)
        varying = {"id", "created_at", "updated_at"}
        for field in dataclasses.fields(Task):
            with self.subTest(field=field.name):
                if field.name in varying:
                    continue
                self.assertEqual(
                    getattr(first, field.name),
                    getattr(second, field.name),
                    f"{field.name} varies between two derivations",
                )
        self.assertNotEqual(first.id, second.id)


# --------------------------------------------------------------------------
# S3A final. Falsy is not canonical: HEAD window and predecessor routing.
# --------------------------------------------------------------------------


#: Wrong-type falsy values for the HEAD read window. Each is falsy, so each
#: passed the old truthiness test; none is the canonical integer 0.
HEAD_WINDOW_HOSTILES: tuple[tuple[str, dict], ...] = (
    ("read_start_line-False", {"read_start_line": False}),
    ("read_start_line-float", {"read_start_line": 0.0}),
    ("read_start_line-str", {"read_start_line": ""}),
    ("read_start_line-none", {"read_start_line": None}),
    ("read_max_lines-False", {"read_max_lines": False}),
    ("read_max_lines-float", {"read_max_lines": 0.0}),
    ("read_max_lines-str", {"read_max_lines": ""}),
    ("read_max_lines-none", {"read_max_lines": None}),
)

#: Wrong-type falsy values for the routing fields of a step with NO consumed
#: predecessor, where the canonical shapes are exactly "" and exactly ().
ROUTING_HOSTILES: tuple[tuple[str, dict], ...] = (
    ("write_source_task_id-zero", {"write_source_task_id": 0}),
    ("write_source_task_id-False", {"write_source_task_id": False}),
    ("write_source_task_id-none", {"write_source_task_id": None}),
    ("write_source_task_id-bytes", {"write_source_task_id": b""}),
    ("local_read_source_task_ids-list", {"local_read_source_task_ids": []}),
    ("local_read_source_task_ids-str", {"local_read_source_task_ids": ""}),
    ("local_read_source_task_ids-False", {"local_read_source_task_ids": False}),
    ("local_read_source_task_ids-none", {"local_read_source_task_ids": None}),
)


class HeadWindowExactShapeTests(MaterializationTestCase):
    """A chain read is HEAD-only, and 0 means the integer 0."""

    def test_the_canonical_head_window_is_exactly_integer_zero(self) -> None:
        canonical = self.derive(0)
        self.assertIs(canonical.local_read_kind, LocalReadKind.HEAD)
        self.assertIs(type(canonical.read_start_line), int)
        self.assertIs(type(canonical.read_max_lines), int)
        self.assertEqual(canonical.read_start_line, 0)
        self.assertEqual(canonical.read_max_lines, 0)
        self.assertIsNone(
            chain_task_refusal(
                canonical, self.chain_now(), 0, mode=ChainTaskMode.PROPOSAL
            )
        )

    def test_every_falsy_head_value_fails_proposal_validation(self) -> None:
        chain_state = self.chain_now()
        canonical = self.derive(0)
        for name, changes in HEAD_WINDOW_HOSTILES:
            with self.subTest(field=name):
                for value in changes.values():
                    self.assertFalse(bool(value), f"{name} is not falsy")
                self.assertIsNotNone(
                    chain_task_refusal(
                        replace(canonical, **changes),
                        chain_state,
                        0,
                        mode=ChainTaskMode.PROPOSAL,
                    ),
                    f"{name} passed PROPOSAL validation",
                )

    def test_every_falsy_head_value_is_refused_at_materialization(self) -> None:
        for name, changes in HEAD_WINDOW_HOSTILES:
            with self.subTest(field=name):
                self.assertRefusedProposal(f"head {name}", changes)

    def test_a_real_region_window_is_still_refused(self) -> None:
        self.assertRefusedProposal(
            "region",
            {
                "local_read_kind": LocalReadKind.REGION,
                "read_start_line": 10,
                "read_max_lines": 5,
            },
        )


class RoutingExactShapeTests(MaterializationTestCase):
    """Predecessor-free routing is exactly "" and exactly ()."""

    def test_the_canonical_predecessor_free_routing_is_exact(self) -> None:
        canonical = self.derive(0)
        self.assertIs(canonical.write_source, WriteSource.DIRECT)
        self.assertIs(type(canonical.write_source_task_id), str)
        self.assertEqual(canonical.write_source_task_id, "")
        self.assertIs(canonical.local_read_context_kind, LocalReadContextKind.NONE)
        self.assertIs(type(canonical.local_read_source_task_ids), tuple)
        self.assertEqual(canonical.local_read_source_task_ids, ())
        self.assertIsNone(
            chain_task_refusal(
                canonical, self.chain_now(), 0, mode=ChainTaskMode.PROPOSAL
            )
        )

    def test_every_falsy_routing_value_fails_proposal_validation(self) -> None:
        chain_state = self.chain_now()
        canonical = self.derive(0)
        for name, changes in ROUTING_HOSTILES:
            with self.subTest(field=name):
                for value in changes.values():
                    self.assertFalse(bool(value), f"{name} is not falsy")
                self.assertIsNotNone(
                    chain_task_refusal(
                        replace(canonical, **changes),
                        chain_state,
                        0,
                        mode=ChainTaskMode.PROPOSAL,
                    ),
                    f"{name} passed PROPOSAL validation",
                )

    def test_every_falsy_routing_value_is_refused_at_materialization(self) -> None:
        for name, changes in ROUTING_HOSTILES:
            with self.subTest(field=name):
                self.assertRefusedProposal(f"routing {name}", changes)

    def test_a_read_consumer_needs_the_exact_reference_tuple(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        chain_state = self.chain_now()
        canonical = self.derive(1)
        self.assertEqual(canonical.local_read_source_task_ids, (read_id,))
        for name, value in (
            ("list", [read_id]),
            ("extra", (read_id, read_id)),
            ("empty-tuple", ()),
            ("empty-list", []),
            ("string", read_id),
            ("none", None),
        ):
            with self.subTest(shape=name):
                hostile = replace(canonical, local_read_source_task_ids=value)
                self.assertIsNotNone(
                    chain_task_refusal(
                        hostile, chain_state, 1, mode=ChainTaskMode.PROPOSAL
                    ),
                    f"{name} passed PROPOSAL validation",
                )
                verdict, task_id = self.memory.materialize_chain_step(
                    self.session.id, chain_state.id, chain_state.digest, 1, hostile
                )
                self.assertEqual(verdict, "conflict")
                self.assertEqual(task_id, "")
        # Positive control: the genuine routing still materializes.
        self.assertEqual(self.materialize(1)[0], "created")

    def test_a_write_consumer_needs_the_exact_source_id(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        _verdict, model_id = self.materialize(1)
        self.finish_model_step(model_id)
        self.advance(1)
        chain_state = self.chain_now()
        canonical = self.derive(2)
        self.assertEqual(canonical.write_source_task_id, model_id)
        self.assertIs(canonical.write_source, WriteSource.MODEL_ANSWER)
        for name, changes in (
            ("bytes-id", {"write_source_task_id": model_id.encode()}),
            ("empty-id", {"write_source_task_id": ""}),
            ("none-id", {"write_source_task_id": None}),
            ("wrong-id", {"write_source_task_id": read_id}),
            ("direct-source", {"write_source": WriteSource.DIRECT}),
            ("read-refs", {"local_read_source_task_ids": (read_id,)}),
            ("read-refs-list", {"local_read_source_task_ids": []}),
            (
                "read-kind",
                {"local_read_context_kind": LocalReadContextKind.REFERENCES},
            ),
        ):
            with self.subTest(shape=name):
                hostile = replace(canonical, **changes)
                self.assertIsNotNone(
                    chain_task_refusal(
                        hostile, chain_state, 2, mode=ChainTaskMode.PROPOSAL
                    ),
                    f"{name} passed PROPOSAL validation",
                )
                verdict, task_id = self.memory.materialize_chain_step(
                    self.session.id, chain_state.id, chain_state.digest, 2, hostile
                )
                self.assertEqual(verdict, "conflict")
                self.assertEqual(task_id, "")
        # Positive control: the genuine MODEL -> CREATE routing materializes.
        self.assertEqual(self.materialize(2)[0], "created")

    def test_a_read_consumer_may_not_carry_a_write_source(self) -> None:
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.advance(0)
        chain_state = self.chain_now()
        canonical = self.derive(1)
        for name, changes in (
            ("model-source", {"write_source": WriteSource.MODEL_ANSWER}),
            ("source-id", {"write_source_task_id": read_id}),
            ("source-id-zero", {"write_source_task_id": 0}),
        ):
            with self.subTest(shape=name):
                self.assertIsNotNone(
                    chain_task_refusal(
                        replace(canonical, **changes),
                        chain_state,
                        1,
                        mode=ChainTaskMode.PROPOSAL,
                    ),
                    f"{name} passed PROPOSAL validation",
                )


# --------------------------------------------------------------------------
# S3B. The driver orchestrates. Every step runs the ordinary way.
# --------------------------------------------------------------------------


class _RecordingConnector(BaseConnector):
    """A connector that records calls instead of reaching anything real."""

    def __init__(
        self,
        name: str,
        capabilities: tuple[str, ...],
        effect: SideEffect = SideEffect.NONE,
        *,
        answer: str | None = None,
        snapshot: str | None = None,
        outcome: OutcomeStatus = OutcomeStatus.SUCCESS,
    ) -> None:
        self.manifest = ConnectorManifest(
            name=name,
            capabilities=capabilities,
            default_side_effect=SideEffectProfile(
                effect=effect,
                reversible=effect is not SideEffect.EXTERNAL_EGRESS,
                always_requires_approval=effect is SideEffect.EXTERNAL_EGRESS,
            ),
            # A connector that declares no route stays UNKNOWN and is
            # refused before execution - the fail-closed default. These
            # stubs are local and free, so they say so explicitly rather
            # than inheriting a refusal the tests would explain away.
            default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
        )
        self.answer = answer
        self.snapshot = snapshot
        self.outcome = outcome
        self.calls: list[str] = []

    def canonical_outbound(self, task):
        return b"canonical-outbound-bytes"

    def verify(self, task, result, session):
        """An independent check, as the ordinary VERIFY stage requires."""
        return VerificationRecord(
            status=(
                VerificationStatus.PASSED
                if result.ok
                else VerificationStatus.FAILED
            ),
            method="recording_connector_check",
            evidence=("deterministic-test-evidence",),
        )

    def execute(self, task, session):
        self.calls.append(task.id)
        data: dict[str, object] = {}
        if self.answer is not None:
            data["model_answer"] = build_model_answer(self.answer)
        if self.snapshot is not None:
            raw = self.snapshot.encode("utf-8")
            data["local_read"] = LocalReadSnapshot(
                relative_path="notes.txt",
                byte_size=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                excerpt=self.snapshot,
                truncated=False,
                classification=DataClassification.INTERNAL,
                read_at="2026-08-28T00:00:00+00:00",
            )
        return ToolResult(
            outcome=self.outcome,
            summary="recorded",
            data=data,
            # H3 reconciles every claimed execution against an
            # authoritative route, so a connector that reports no usage
            # fails the task. These stubs are free and say so.
            usage=UsageRecord(
                provider="test-provider",
                model="test-model",
                connector=self.manifest.name,
                billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
                monetary_state=MonetaryState.NOT_APPLICABLE,
                call_count=1,
            ),
        )


class DriverTestCase(BoundaryTestCase):
    """A real store, a real registry and connectors that never leave the box."""

    def setUp(self) -> None:
        super().setUp()
        self.reader = _RecordingConnector(
            "reader",
            (LOCAL_READ_CAPABILITY,),
            snapshot="the project notes, in full",
        )
        self.planner = _RecordingConnector(
            "planner",
            ("code_analysis", "code_planning"),
            SideEffect.EXTERNAL_EGRESS,
            answer="A three bullet summary of the notes.",
        )
        self.registry = ToolRegistry()
        self.registry.register(self.reader)
        self.registry.register(self.planner)
        self.core = AgentCore(
            memory=self.memory, registry=self.registry, settings=self.settings
        )
        self.app = JarvisApp(self.core, self.memory, self.settings)
        self.assertEqual(self.declare(declaration_json(2)).exit_code, EXIT_OK)

    def chain_now(self) -> ChainState:
        stored = self.stored().chain
        assert stored is not None
        return stored

    def drive(self) -> tuple[object, str]:
        return self.core.run_chain(self.session.id)

    def approve_pending(self, run) -> object:
        """Approve exactly the request the driver surfaced, the ordinary way."""
        request = run.permission_request
        assert request is not None
        return self.app.approve(
            self.session.id, request.task_id, request.id, request.fingerprint
        )

    def connector_calls(self) -> list[str]:
        return self.reader.calls + self.planner.calls


class DriverHappyPathTests(DriverTestCase):
    """READ then MODEL, each through the ordinary lifecycle."""

    def test_the_first_step_runs_and_the_chain_advances(self) -> None:
        run, verdict = self.drive()
        chain_state = self.chain_now()
        if verdict == "advanced":
            self.assertEqual(chain_state.cursor, 1)
            self.assertTrue(chain_state.step_task_ids[0])
        else:
            # A step that needs approval stops the driver, which is correct.
            self.assertIn(verdict, ("awaiting_approval", "blocked", "failed"))
        self.assertIsNotNone(run)

    def test_the_driver_binds_exactly_one_task_per_step(self) -> None:
        self.drive()
        self.drive()
        chain_state = self.chain_now()
        bound = [task_id for task_id in chain_state.step_task_ids if task_id]
        self.assertEqual(len(bound), len(set(bound)))
        self.assertLessEqual(len(bound), 2)
        self.assertEqual(self.task_rows(), len(bound))

    def test_repeated_driving_never_reruns_a_completed_step(self) -> None:
        for _ in range(4):
            self.drive()
        first = self.chain_now().step_task_ids[0]
        if first:
            self.assertLessEqual(
                self.reader.calls.count(first), 1, "a completed step re-executed"
            )

    def test_an_unknown_session_is_reported(self) -> None:
        run, verdict = self.core.run_chain("no-such-session")
        self.assertEqual(verdict, "unknown")
        self.assertIsNotNone(run.error)

    def test_a_session_with_no_chain_is_reported(self) -> None:
        other = SessionState(objective="no chain here")
        self.memory.save_session(other)
        _run, verdict = self.core.run_chain(other.id)
        self.assertEqual(verdict, "unknown")


class DriverApprovalTests(DriverTestCase):
    """Every step keeps ordinary per-task approval. Nothing is inherited."""

    def test_an_egress_step_stops_the_driver_for_approval(self) -> None:
        """One drive: the read completes, the model step stops for approval.

        Deterministic on purpose - no skip escape. EXTERNAL_EGRESS is a
        mandatory-approval effect in every permission mode, so this chain
        ALWAYS pauses at step 1, and a driver that reported anything else
        would be reporting progress it did not have.
        """
        run, verdict = self.drive()
        self.assertEqual(verdict, "awaiting_approval")
        self.assertIsNotNone(run.permission_request)
        self.assertEqual(run.status, RunStatus.AWAITING_APPROVAL)
        chain_state = self.chain_now()
        # It advanced OFF the completed read and stopped ON the model step.
        self.assertEqual(chain_state.cursor, 1)
        self.assertIs(chain_state.status, ChainStatus.ACTIVE)
        # The unapproved step ran no connector.
        self.assertEqual(self.planner.calls, [])

    def test_the_approval_names_the_step_task(self) -> None:
        run, verdict = self.drive()
        self.assertEqual(verdict, "awaiting_approval")
        request = run.permission_request
        assert request is not None
        chain_state = self.chain_now()
        self.assertEqual(request.session_id, self.session.id)
        self.assertIn(request.task_id, chain_state.step_task_ids)
        # The envelope makes the step visible alongside the approval.
        payload = self.app.run_chain(self.session.id).payload
        self.assertIsNotNone(payload["chain"])
        self.assertEqual(payload["chain"]["cursor"], chain_state.cursor)

    def test_an_approved_step_proceeds_and_the_chain_completes(self) -> None:
        """The ordinary approval path carries the chain to COMPLETED."""
        run, verdict = self.drive()
        self.assertEqual(verdict, "awaiting_approval")
        approved = self.approve_pending(run)
        self.assertEqual(approved.exit_code, EXIT_OK)
        # The model step ran exactly once, through the ordinary path.
        self.assertEqual(len(self.planner.calls), 1)
        _run, verdict = self.drive()
        self.assertEqual(verdict, "completed")
        chain_state = self.chain_now()
        self.assertIs(chain_state.status, ChainStatus.COMPLETED)
        self.assertEqual(chain_state.cursor, 1)
        self.assertTrue(all(chain_state.step_task_ids))
        # Driving a finished chain executes nothing further.
        calls = list(self.connector_calls())
        _run, verdict = self.drive()
        self.assertEqual(verdict, ChainStatus.COMPLETED.value)
        self.assertEqual(self.connector_calls(), calls)

    def test_no_grant_is_reused_across_steps(self) -> None:
        # Whatever grants exist, each is bound to exactly one task.
        for _ in range(4):
            self.drive()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT task_id, COUNT(*) FROM permission_grants GROUP BY task_id"
            ).fetchall()
        for task_id, count in rows:
            with self.subTest(task_id=task_id):
                self.assertGreaterEqual(count, 1)
        task_ids = [row[0] for row in rows]
        self.assertEqual(len(task_ids), len(set(task_ids)))

    def test_the_driver_never_approves_anything_itself(self) -> None:
        source = inspect.getsource(AgentCore.run_chain) + inspect.getsource(
            AgentCore._run_chain_step
        )
        for forbidden in ("PermissionGrant", "resolve_approval", "approved=True"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class DriverAuthorityTests(DriverTestCase):
    """Tampered authority stops the driver before any connector runs."""

    def _current_task(self) -> str:
        """Drive once, then name the task the chain is actually SITTING on.

        One drive completes the read step and stops at the model step's
        approval, so the cursor has moved. Tampering the step the chain has
        already left would prove nothing - the driver would never look at it
        again.
        """
        self.drive()
        chain_state = self.chain_now()
        current = chain_state.step_task_ids[chain_state.cursor]
        self.assertTrue(current, "no task is bound at the cursor")
        return current

    def _reset_calls(self) -> None:
        self.reader.calls.clear()
        self.planner.calls.clear()

    def test_a_tampered_binding_stops_the_driver(self) -> None:
        task_id = self._current_task()
        task = self.memory.load_task(task_id)
        assert task is not None
        task.chain_binding = ChainBinding()
        self.memory.save_task(self.session.id, task)
        self._reset_calls()
        _run, verdict = self.drive()
        self.assertEqual(verdict, "conflict")
        self.assertEqual(self.connector_calls(), [])

    def test_tampered_immutable_authority_stops_the_driver(self) -> None:
        for name, changes in AUTHORITY_TAMPERS:
            with self.subTest(field=name):
                self.session = SessionState(objective=f"driver {name}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(2)).exit_code, EXIT_OK
                )
                task_id = self._current_task()
                task = self.memory.load_task(task_id)
                assert task is not None
                mutated = tampered(task, changes)
                if mutated == task:
                    # A no-op for this step's shape; the same field is
                    # exercised on another step elsewhere.
                    continue
                self.memory.save_task(self.session.id, mutated)
                self._reset_calls()
                _run, verdict = self.drive()
                self.assertEqual(verdict, "conflict", f"{name} was driven")
                self.assertEqual(self.connector_calls(), [])

    def test_a_tampered_selected_connector_is_refused_by_the_registry(self) -> None:
        task_id = self._current_task()
        current = self.memory.load_task(task_id)
        assert current is not None
        # A connector the REGISTRY would never choose for this task: one that
        # does not declare its capability, one that is not registered at all,
        # and one whose name merely looks like a registered one.
        wrong = (
            "reader"
            if current.capability not in self.reader.manifest.capabilities
            else "planner"
        )
        for hostile in (wrong, "not-registered", "reader "):
            with self.subTest(connector=hostile):
                task = self.memory.load_task(task_id)
                assert task is not None
                task.status = TaskStatus.RUNNING
                task.stage = LoopStage.RISK_PERMISSION
                task.selected_connector = hostile
                self.memory.save_task(self.session.id, task)
                self._reset_calls()
                _run, verdict = self.drive()
                self.assertEqual(verdict, "conflict", f"{hostile} was accepted")
                self.assertEqual(self.connector_calls(), [])

    def test_a_legitimate_selected_connector_is_accepted(self) -> None:
        task_id = self._current_task()
        task = self.memory.load_task(task_id)
        assert task is not None
        legitimate = (
            "reader"
            if task.capability in self.reader.manifest.capabilities
            else "planner"
        )
        task.status = TaskStatus.RUNNING
        task.stage = LoopStage.RISK_PERMISSION
        task.selected_connector = legitimate
        self.memory.save_task(self.session.id, task)
        self.assertIsNone(
            self.core._chain_connector_refusal(self.memory.load_task(task_id))
        )

    def test_an_unselected_connector_is_not_a_refusal(self) -> None:
        task_id = self._current_task()
        task = self.memory.load_task(task_id)
        assert task is not None
        task.selected_connector = None
        self.memory.save_task(self.session.id, task)
        self.assertIsNone(
            self.core._chain_connector_refusal(self.memory.load_task(task_id))
        )

    def test_a_foreign_task_in_the_slot_stops_the_driver(self) -> None:
        other = SessionState(objective="elsewhere")
        self.memory.save_session(other)
        stranger = Task(
            objective="o",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(action="read"),
        )
        self.memory.save_task(other.id, stranger)
        chain_state = self.chain_now()
        self.plant_chain(replace(chain_state, step_task_ids=(stranger.id, "")))
        self._reset_calls()
        _run, verdict = self.drive()
        self.assertEqual(verdict, "conflict")
        self.assertEqual(self.connector_calls(), [])

    def test_a_declaration_drift_stops_the_driver(self) -> None:
        self._current_task()
        drifted = declaration(2)
        drifted[0]["objective"] = "A different intent."
        chain_state = self.chain_now()
        self.plant_chain(
            replace(chain_state, steps=chain_steps_from_declaration(drifted))
        )
        cursor_before = self.chain_now().cursor
        self._reset_calls()
        _run, verdict = self.drive()
        # Either gate may catch it first - derivation refuses the drifted
        # predecessor, or storage refuses the changed digest. Both are safe
        # stops, and the invariant is that nothing ran and nothing moved.
        self.assertIn(verdict, ("conflict", "refused"))
        self.assertEqual(self.connector_calls(), [])
        self.assertEqual(self.chain_now().cursor, cursor_before)


class DriverInDoubtTests(DriverTestCase):
    """H2 owns replay policy, and it says never."""

    def _settle_current_with_attempt(self, state: AttemptState) -> str:
        """Put the cursor's task in doubt: stored COMPLETED, attempt unsettled.

        This is the shape a crash leaves behind, and the one H2 refuses to
        replay. The task must be terminal so the driver reaches the advance
        gate rather than stopping earlier at an approval.
        """
        self.drive()
        chain_state = self.chain_now()
        task_id = chain_state.step_task_ids[chain_state.cursor]
        self.assertTrue(task_id, "no task is bound at the cursor")
        task = self.memory.load_task(task_id)
        assert task is not None
        task.status = TaskStatus.COMPLETED
        task.stage = LoopStage.COMPLETED
        self.memory.save_task(self.session.id, task)
        self.plant_attempt(task_id, state)
        return task_id

    def test_an_in_flight_attempt_stops_the_driver_without_replay(self) -> None:
        self._settle_current_with_attempt(AttemptState.IN_FLIGHT)
        cursor_before = self.chain_now().cursor
        before = list(self.connector_calls())
        _run, verdict = self.drive()
        self.assertEqual(verdict, "blocked")
        self.assertEqual(self.connector_calls(), before)
        self.assertEqual(self.chain_now().cursor, cursor_before)
        self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)

    def test_an_unknown_outcome_attempt_stops_the_driver(self) -> None:
        self._settle_current_with_attempt(AttemptState.UNKNOWN_OUTCOME)
        cursor_before = self.chain_now().cursor
        before = list(self.connector_calls())
        for _ in range(3):
            _run, verdict = self.drive()
            # Repeated driving must never turn an in-doubt step into progress.
            self.assertEqual(verdict, "blocked")
            self.assertEqual(self.connector_calls(), before)
            self.assertEqual(self.chain_now().cursor, cursor_before)
            self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)


class DriverTerminalTests(DriverTestCase):
    """Failure, rejection and cancellation each have one deterministic end."""

    def test_a_failed_step_fails_the_chain(self) -> None:
        self.drive()
        chain_state = self.chain_now()
        task_id = chain_state.step_task_ids[chain_state.cursor]
        if not task_id:
            self.skipTest("no task is bound at the cursor")
        task = self.memory.load_task(task_id)
        assert task is not None
        task.status = TaskStatus.FAILED
        task.stage = LoopStage.FAILED
        self.memory.save_task(self.session.id, task)
        _run, verdict = self.drive()
        self.assertEqual(verdict, "failed")
        self.assertIs(self.chain_now().status, ChainStatus.FAILED)

    def test_a_rejected_step_cancels_the_chain(self) -> None:
        self.drive()
        chain_state = self.chain_now()
        task_id = chain_state.step_task_ids[chain_state.cursor]
        if not task_id:
            self.skipTest("no task is bound at the cursor")
        task = self.memory.load_task(task_id)
        assert task is not None
        task.status = TaskStatus.REJECTED
        task.stage = LoopStage.CANCELLED
        self.memory.save_task(self.session.id, task)
        _run, verdict = self.drive()
        self.assertEqual(verdict, "cancelled")
        self.assertIs(self.chain_now().status, ChainStatus.CANCELLED)

    def test_a_cancelled_chain_is_never_driven(self) -> None:
        chain_state = self.chain_now()
        self.memory.cancel_chain(self.session.id, chain_state.id)
        before = list(self.connector_calls())
        _run, verdict = self.drive()
        self.assertEqual(verdict, ChainStatus.CANCELLED.value)
        self.assertEqual(self.connector_calls(), before)
        self.assertEqual(
            [task_id for task_id in self.chain_now().step_task_ids if task_id], []
        )

    def test_an_invalid_chain_is_never_driven(self) -> None:
        self.plant_chain(ChainState.from_dict("garbage"))
        _run, verdict = self.drive()
        self.assertEqual(verdict, "conflict")
        self.assertEqual(self.connector_calls(), [])


class DriverConcurrencyTests(DriverTestCase):
    """One authoritative attempt per step, whoever is driving."""

    def test_two_runners_never_duplicate_a_step_task(self) -> None:
        for attempt in range(6):
            with self.subTest(attempt=attempt):
                self.session = SessionState(objective=f"race {attempt}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(2)).exit_code, EXIT_OK
                )
                barrier = threading.Barrier(2)
                verdicts: list[str] = []
                guard = threading.Lock()

                def race(
                    gate: threading.Barrier = barrier,
                    lock: threading.Lock = guard,
                    sink: list = verdicts,
                    session_id: str = self.session.id,
                ) -> None:
                    store = SQLiteMemoryStore(self.database)
                    core = AgentCore(
                        memory=store, registry=self.registry, settings=self.settings
                    )
                    gate.wait(timeout=10)
                    _run, verdict = core.run_chain(session_id)
                    with lock:
                        sink.append(verdict)

                threads = [threading.Thread(target=race) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=40)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(len(verdicts), 2)
                bound = [t for t in self.chain_now().step_task_ids if t]
                self.assertEqual(len(bound), len(set(bound)))
                self.assertEqual(self.task_rows(), len(bound))

    def test_a_runner_racing_a_cancel_never_advances_a_cancelled_chain(self) -> None:
        for attempt in range(6):
            with self.subTest(attempt=attempt):
                self.session = SessionState(objective=f"cancel race {attempt}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(2)).exit_code, EXIT_OK
                )
                chain_state = self.chain_now()
                barrier = threading.Barrier(2)

                def runner(
                    gate: threading.Barrier = barrier,
                    session_id: str = self.session.id,
                ) -> None:
                    store = SQLiteMemoryStore(self.database)
                    core = AgentCore(
                        memory=store, registry=self.registry, settings=self.settings
                    )
                    gate.wait(timeout=10)
                    core.run_chain(session_id)

                def canceller(
                    gate: threading.Barrier = barrier,
                    session_id: str = self.session.id,
                    chain_id: str = chain_state.id,
                ) -> None:
                    store = SQLiteMemoryStore(self.database)
                    gate.wait(timeout=10)
                    store.cancel_chain(session_id, chain_id)

                threads = [
                    threading.Thread(target=runner),
                    threading.Thread(target=canceller),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=40)
                    self.assertFalse(thread.is_alive())
                current = self.chain_now()
                self.assertIn(
                    current.status, (ChainStatus.CANCELLED, ChainStatus.ACTIVE)
                )
                # Whatever the interleaving, no step beyond the cursor exists.
                created = [t for t in current.step_task_ids if t]
                self.assertLessEqual(len(created), current.cursor + 1)

    def test_a_stale_session_save_cannot_move_the_chain(self) -> None:
        stale = self.stored()
        self.drive()
        after = self.chain_now()
        stale.chain = replace(after, cursor=0, status=ChainStatus.FAILED)
        stale.next_step = "unrelated"
        self.memory.save_session(stale)
        current = self.chain_now()
        self.assertEqual(current.cursor, after.cursor)
        self.assertIs(current.status, after.status)
        self.assertEqual(current.step_task_ids, after.step_task_ids)


class DriverSurfaceTests(DriverTestCase):
    """The CLI and envelope say where the chain is and why it stopped."""

    def test_the_envelope_reports_the_chain_and_a_verdict(self) -> None:
        result = self.app.run_chain(self.session.id)
        payload = result.payload
        self.assertEqual(payload["command"], "chain-run")
        self.assertEqual(payload["schema_version"], 6)
        block = payload["chain"]
        self.assertIsNotNone(block)
        self.assertIn("verdict", block)
        self.assertIsNotNone(block["verdict"])
        for key in ("id", "status", "cursor", "step_task_ids"):
            with self.subTest(key=key):
                self.assertIn(key, block)

    def test_other_commands_carry_a_null_verdict(self) -> None:
        block = chain_show_result(
            self.memory, self.settings, self.session.id
        ).payload["chain"]
        self.assertIsNone(block["verdict"])

    def test_the_cli_drives_the_chain(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                ["chain", "run", "--session-id", self.session.id],
                settings=self.settings,
                app_factory=lambda _settings: self.app,
            )
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["command"], "chain-run")
        self.assertIsNotNone(payload["chain"]["verdict"])
        self.assertIn(code, (EXIT_OK, EXIT_AWAITING_APPROVAL, EXIT_INVALID_INPUT))

    def test_the_cli_run_verb_builds_a_real_application(self) -> None:
        # declare/show are local-only; run must NOT be, because it executes.
        def explode(_settings: Settings) -> object:
            raise AssertionError("chain run did not build an application")

        with contextlib.redirect_stdout(io.StringIO()):
            code = cli_main(
                ["chain", "run", "--session-id", self.session.id],
                settings=self.settings,
                app_factory=explode,
            )
        self.assertEqual(code, EXIT_INTERNAL)

    def test_declare_and_show_remain_local_only(self) -> None:
        def explode(_settings: Settings) -> object:
            raise AssertionError("a local chain command built an application")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli_main(
                    ["chain", "show", "--session-id", self.session.id],
                    settings=self.settings,
                    app_factory=explode,
                ),
                EXIT_OK,
            )


class DriverBillingTests(DriverTestCase):
    """No chain-level billing shortcut exists."""

    def test_the_driver_touches_no_billing_api(self) -> None:
        source = inspect.getsource(AgentCore.run_chain) + inspect.getsource(
            AgentCore._run_chain_step
        )
        for forbidden in (
            "BillingClaim",
            "reserve",
            "reconcile_usage",
            "MONTHLY_HARD_LIMIT",
            "ledger",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_each_step_reserves_through_the_ordinary_path(self) -> None:
        for _ in range(3):
            self.drive()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            reservations = connection.execute(
                "SELECT attempt_id, task_id FROM attempt_billing"
            ).fetchall()
        attempts = [row[0] for row in reservations]
        self.assertEqual(len(attempts), len(set(attempts)))
        chain_state = self.chain_now()
        for _attempt_id, task_id in reservations:
            with self.subTest(task_id=task_id):
                self.assertIn(task_id, chain_state.step_task_ids)


# --------------------------------------------------------------------------
# S3B P1-1. Cancellation authority and the H2 execution claim, arbitrated
# atomically. Real SQLite, separate stores and separate cores per actor - the
# TOCTOU is a cross-store one, so a test that shares a store proves nothing.
# --------------------------------------------------------------------------


class ChainArbitrationTestCase(DriverTestCase):
    """Two independent actors over ONE real database, and the raw probes.

    Every actor gets its own ``SQLiteMemoryStore`` and its own ``AgentCore``,
    because the window under test is a cross-store one: a harness that shared
    a store would be testing Python object lifetimes, not storage arbitration.
    """

    def free_claim(self) -> BillingClaim:
        """The ordinary zero-monetary claim, against the LIVE ledger.

        Exposure is re-read rather than assumed: the claim is refused unless
        it matches storage exactly, which is the H3 rule these tests must not
        weaken to make an arbitration point.
        """
        snapshot = self.memory.ledger_snapshot()
        return BillingClaim(
            ledger_month=snapshot.month,
            expected_exposure_nano_usd=snapshot.total_nano_usd,
            reservation_nano_usd=0,
            limit_nano_usd=MONTHLY_HARD_LIMIT_NANO_USD,
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            gate_state="chain_arbitration_test",
        )

    def other_store(self) -> SQLiteMemoryStore:
        """A SEPARATE store over the same database, as another process has."""
        return SQLiteMemoryStore(self.database)

    def runner_core(self, memory: MemoryStore) -> AgentCore:
        return AgentCore(
            memory=memory, registry=self.registry, settings=self.settings
        )

    def cancel_from_another_store(self) -> str:
        chain_state = self.chain_now()
        return self.other_store().cancel_chain(self.session.id, chain_state.id)

    def claimed_attempts(self) -> list[tuple[str, str]]:
        """Every attempt that ever left PREPARED, by task and state."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return [
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT task_id, state FROM execution_attempts WHERE state != ?",
                    (AttemptState.PREPARED.value,),
                ).fetchall()
            ]

    def reservations(self) -> int:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM attempt_billing"
            ).fetchone()[0]

    def opened_attempt(self, task_id: str, fingerprint: str) -> ExecutionAttempt:
        return self.memory.open_execution_attempt(
            ExecutionAttempt(
                session_id=self.session.id,
                task_id=task_id,
                connector="reader",
                fingerprint=fingerprint,
            )
        )

    def attempt_rows(self) -> list[tuple[str, str]]:
        """EVERY attempt row, whatever its state, by task and state."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return [
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT task_id, state FROM execution_attempts"
                ).fetchall()
            ]

    def consumed_grants(self) -> int:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM permission_grants "
                "WHERE consumed_by_attempt IS NOT NULL"
            ).fetchone()[0]

    def bound_then_cancelled(self) -> str:
        """Bind step 0 the ordinary way, cancelling from a separate store.

        The cancel commits while the runner sits between binding the step and
        claiming it, which is the only interleaving that leaves an
        authoritatively bound, never-executed step behind a CANCELLED chain -
        exactly the row the association rules have to hold on to.
        """
        runner_store = self.other_store()
        core = self.runner_core(runner_store)
        ordinary = runner_store.open_execution_attempt
        done: list[str] = []

        def cancel_then_open(attempt):
            if not done:
                done.append(self.cancel_from_another_store())
            return ordinary(attempt)

        with mock.patch.object(
            runner_store, "open_execution_attempt", cancel_then_open
        ):
            core.run_chain(self.session.id)
        self.assertEqual(done, ["cancelled"])
        bound = self.chain_now().step_task_ids[0]
        self.assertTrue(bound, "the step was never bound")
        self.assertIs(self.chain_now().status, ChainStatus.CANCELLED)
        self.assertEqual(self.connector_calls(), [])
        return bound

    def rewrite_binding(self, task_id: str, value: object) -> None:
        """Put an arbitrary ``chain_binding`` into the PERSISTED task row.

        Raw SQL on purpose: no API can produce these rows, which is the point
        - the question is what storage does when it MEETS one, however it got
        there. ``_DROPPED`` removes the key entirely, as a truncated or
        older-shaped document would.
        """
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            document = json.loads(row[0])
            if value is _DROPPED:
                document.pop("chain_binding", None)
            else:
                document["chain_binding"] = value
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task_id),
            )
            connection.commit()

    def assertNothingExecuted(self, message: str = "") -> None:
        """No connector call, no claimed ownership, no money, no grant."""
        self.assertEqual(self.connector_calls(), [], message)
        self.assertEqual(self.claimed_attempts(), [], message)
        self.assertEqual(self.reservations(), 0, message)
        self.assertEqual(self.consumed_grants(), 0, message)


class ChainClaimArbitrationTests(ChainArbitrationTestCase):
    """A cancel and an execution claim are ONE decision, whichever wins."""

    # -- A. CANCEL WINS BEFORE THE EXECUTION CLAIM ----------------------

    def test_a_cancel_committed_before_the_claim_executes_nothing(self) -> None:
        """Materialize, cancel, then attempt execution: nothing is claimed.

        The cancel commits from a separate store at the exact moment the
        runner has bound the step and is one call away from claiming it -
        ``open_execution_attempt`` is the last thing before ``begin_execution``
        - so the runner reaches the claim holding a chain storage has already
        cancelled. This is the interleaving a driver-side reload cannot see.
        """
        runner_store = self.other_store()
        core = self.runner_core(runner_store)
        cancelled: list[str] = []
        ordinary = runner_store.open_execution_attempt

        def cancel_then_open(attempt):
            if not cancelled:
                cancelled.append(self.cancel_from_another_store())
            return ordinary(attempt)

        with mock.patch.object(
            runner_store, "open_execution_attempt", cancel_then_open
        ):
            _run, verdict = core.run_chain(self.session.id)

        self.assertEqual(cancelled, ["cancelled"])
        self.assertEqual(self.connector_calls(), [], "a cancelled chain executed")
        self.assertEqual(self.claimed_attempts(), [], "an attempt was claimed")
        self.assertEqual(self.reservations(), 0, "money was reserved after a cancel")
        current = self.chain_now()
        self.assertIs(current.status, ChainStatus.CANCELLED)
        self.assertEqual(current.cursor, 0, "a cancelled chain advanced")
        self.assertNotEqual(verdict, "advanced")

    def test_a_cancelled_chain_can_never_claim_a_later_attempt(self) -> None:
        """Not just this pass: no later runner can claim the bound step."""
        runner_store = self.other_store()
        core = self.runner_core(runner_store)
        ordinary = runner_store.open_execution_attempt
        done: list[str] = []

        def cancel_then_open(attempt):
            if not done:
                done.append(self.cancel_from_another_store())
            return ordinary(attempt)

        with mock.patch.object(
            runner_store, "open_execution_attempt", cancel_then_open
        ):
            core.run_chain(self.session.id)
        bound = self.chain_now().step_task_ids[0]
        self.assertTrue(bound, "the step was never bound")
        for _ in range(3):
            fresh = self.runner_core(self.other_store())
            _run, verdict = fresh.run_chain(self.session.id)
            self.assertEqual(verdict, ChainStatus.CANCELLED.value)
        # The ordinary single-task entry point is refused too: the CHAIN, not
        # the driver, is what withdrew the authority.
        direct = self.runner_core(self.other_store()).resume(self.session.id, bound)
        self.assertIsNot(direct.status, RunStatus.COMPLETED)
        self.assertEqual(self.connector_calls(), [])
        self.assertEqual(self.claimed_attempts(), [])

    def test_the_claim_itself_refuses_a_cancelled_chain(self) -> None:
        """The storage primitive, asked directly, with no driver involved."""
        runner_store = self.other_store()
        core = self.runner_core(runner_store)
        task, reason = core.build_chain_step_task(self.stored(), self.chain_now(), 0)
        self.assertIsNone(reason)
        assert task is not None
        chain_state = self.chain_now()
        outcome, task_id = runner_store.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(outcome, "created")
        attempt = self.opened_attempt(task_id, "a" * 64)
        self.assertEqual(self.cancel_from_another_store(), "cancelled")
        self.assertIs(
            runner_store.begin_execution(
                attempt.id, self.session.id, task_id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.LOST,
        )
        self.assertEqual(self.claimed_attempts(), [])
        self.assertEqual(self.reservations(), 0)

    # -- B. THE EXECUTION CLAIM WINS BEFORE THE CANCEL ------------------

    def test_a_claim_committed_before_the_cancel_runs_exactly_once(self) -> None:
        """Ownership is durable first; the cancel then stops the CHAIN only.

        The cancel commits from a separate store after ``begin_execution`` has
        returned CLAIMED and while the connector is executing. Cancellation
        may prevent the chain advancing; it may not pretend the attempt it
        lost the race to never started, and it may not let the step run twice.
        """
        runner_store = self.other_store()
        core = self.runner_core(runner_store)
        cancelled: list[str] = []
        ordinary_execute = self.reader.execute

        def cancel_then_execute(task, session):
            if not cancelled:
                cancelled.append(self.cancel_from_another_store())
            return ordinary_execute(task, session)

        with mock.patch.object(self.reader, "execute", cancel_then_execute):
            _run, verdict = core.run_chain(self.session.id)

        self.assertEqual(cancelled, ["cancelled"])
        self.assertEqual(len(self.reader.calls), 1, "the step did not run once")
        self.assertEqual(self.planner.calls, [], "a cancelled chain ran a later step")
        current = self.chain_now()
        self.assertIs(current.status, ChainStatus.CANCELLED)
        self.assertEqual(current.cursor, 0, "a cancelled chain advanced")
        self.assertNotEqual(verdict, "advanced")
        # The attempt that won is recorded as having happened, not erased.
        self.assertEqual(len(self.claimed_attempts()), 1)

    def test_a_repeated_runner_cannot_replay_the_claimed_step(self) -> None:
        cancelled: list[str] = []
        ordinary_execute = self.reader.execute

        def cancel_then_execute(task, session):
            if not cancelled:
                cancelled.append(self.cancel_from_another_store())
            return ordinary_execute(task, session)

        with mock.patch.object(self.reader, "execute", cancel_then_execute):
            self.runner_core(self.other_store()).run_chain(self.session.id)
        bound = self.chain_now().step_task_ids[0]
        for _ in range(3):
            self.runner_core(self.other_store()).run_chain(self.session.id)
        if bound:
            self.runner_core(self.other_store()).resume(self.session.id, bound)
        self.assertEqual(len(self.reader.calls), 1, "the step replayed")
        self.assertEqual(len(self.claimed_attempts()), 1)
        self.assertIs(self.chain_now().status, ChainStatus.CANCELLED)

    # -- The arbitration is where the ownership is granted --------------

    def test_the_arbitration_lives_inside_the_claim_transaction(self) -> None:
        """Not a driver reload, not a lock: the same transaction, by source."""
        source = inspect.getsource(SQLiteMemoryStore.begin_execution)
        self.assertIn("_chain_claim_authority", source)
        proof = inspect.getsource(SQLiteMemoryStore._chain_claim_authority)
        # Everything it decides on is read from the CALLER'S connection.
        for reader in ("_stored_task", "_stored_chain"):
            with self.subTest(reader=reader):
                self.assertIn(f"self.{reader}(connection", proof)
        for forbidden in ("threading", "Lock", "sqlite3.connect", "_connection("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, proof)

    def test_the_exact_bindings_the_claim_arbitrates(self) -> None:
        """Each single tampering of the bound authority loses the claim."""
        runner_store = self.other_store()
        core = self.runner_core(runner_store)
        task, _reason = core.build_chain_step_task(self.stored(), self.chain_now(), 0)
        assert task is not None
        chain_state = self.chain_now()
        outcome, task_id = runner_store.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(outcome, "created")
        after = self.chain_now()
        self.assertEqual(len(after.steps), 2)
        tamperings = {
            "not active": replace(after, status=ChainStatus.CANCELLED),
            "another chain": replace(after, id="chain-elsewhere"),
            "declaration drift": replace(
                after, steps=chain(1).steps, step_task_ids=(task_id,)
            ),
            "cursor moved on": replace(after, cursor=1, step_task_ids=(task_id, "")),
            "slot emptied": replace(after, step_task_ids=("", "")),
        }
        for label, planted in tamperings.items():
            with self.subTest(label=label):
                self.plant_chain(planted)
                attempt = self.opened_attempt(task_id, "b" * 64)
                self.assertIs(
                    runner_store.begin_execution(
                        attempt.id, self.session.id, task_id, [], self.free_claim()
                    ),
                    ExecutionClaimOutcome.LOST,
                    f"{label} still claimed execution",
                )
                self.assertEqual(self.claimed_attempts(), [])
        # The untampered chain is still claimable, so the proof above is not
        # simply refusing everything.
        self.plant_chain(after)
        attempt = self.opened_attempt(task_id, "b" * 64)
        self.assertIs(
            runner_store.begin_execution(
                attempt.id, self.session.id, task_id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.CLAIMED,
        )

    def test_an_ordinary_task_claim_is_unchanged(self) -> None:
        """A task with no chain binding keeps exactly its old authority."""
        store = self.other_store()
        loose = Task(
            objective="stand alone",
            capability="code_analysis",
            scope=ActionScope(action="analyze", destination="anthropic/claude-code"),
        )
        store.save_task(self.session.id, loose)
        attempt = store.open_execution_attempt(
            ExecutionAttempt(
                session_id=self.session.id,
                task_id=loose.id,
                connector="planner",
                fingerprint="c" * 64,
            )
        )
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, loose.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.CLAIMED,
        )


# --------------------------------------------------------------------------
# S3B P1-3. Chain ASSOCIATION is the session's slot, not only the task's
# binding. Removing state from a task must never grant it authority.
# --------------------------------------------------------------------------


#: Every way a persisted binding can stop naming a chain. Each loads back
#: as canonical ABSENCE - which is exactly what made this a fail-open.
_BINDING_ERASURES = {
    "exact empty binding": {"chain_id": "", "chain_digest": "", "step_index": -1},
    "key removed entirely": _DROPPED,
    "null binding": None,
    "binding is not an object": "chain-1",
    "partial: chain without digest": {
        "chain_id": "not-a-uuid",
        "chain_digest": "",
        "step_index": 0,
    },
    "partial: digest without chain": {
        "chain_id": "",
        "chain_digest": "a" * 64,
        "step_index": 0,
    },
    "corrupt digest": {
        "chain_id": "chain-1",
        "chain_digest": "zz",
        "step_index": 0,
    },
    "index out of range": {
        "chain_id": "chain-1",
        "chain_digest": "a" * 64,
        "step_index": 99,
    },
}


class ChainAssociationAuthorityTests(ChainArbitrationTestCase):
    """A bound step stays a bound step even with its binding damaged."""

    def assertLoadsAsAbsent(self, task_id: str) -> None:
        """The damage really did degrade to canonical absence, not to junk."""
        reloaded = self.memory.load_task(task_id)
        assert reloaded is not None
        self.assertFalse(
            reloaded.chain_binding.present,
            "the fixture did not actually erase the binding",
        )
        self.assertIsNone(chain_binding_refusal(reloaded.chain_binding))

    def assertStillNamedByTheChain(self, task_id: str) -> None:
        self.assertIn(task_id, self.chain_now().step_task_ids)

    def test_an_erased_binding_never_erases_chain_association(self) -> None:
        """The P1, at the storage primitive and through ordinary resume."""
        for label, damage in _BINDING_ERASURES.items():
            with self.subTest(damage=label):
                self.session = SessionState(objective=f"erased {label}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(2)).exit_code, EXIT_OK
                )
                bound = self.bound_then_cancelled()
                self.rewrite_binding(bound, damage)
                self.assertLoadsAsAbsent(bound)
                # The session still authoritatively names it. That alone is
                # the association; the task's own field is only a claim.
                self.assertStillNamedByTheChain(bound)

                store = self.other_store()
                attempt = self.opened_attempt(bound, "d" * 64)
                self.assertIs(
                    store.begin_execution(
                        attempt.id, self.session.id, bound, [], self.free_claim()
                    ),
                    ExecutionClaimOutcome.LOST,
                    f"{label} claimed execution",
                )
                # The ordinary entry point, not just the primitive.
                run = self.runner_core(self.other_store()).resume(
                    self.session.id, bound
                )
                self.assertIsNot(run.status, RunStatus.COMPLETED)
                self.assertNothingExecuted(label)

    def test_the_damaged_task_is_still_refused_after_repeated_resume(self) -> None:
        bound = self.bound_then_cancelled()
        self.rewrite_binding(bound, _BINDING_ERASURES["exact empty binding"])
        for _ in range(3):
            self.runner_core(self.other_store()).resume(self.session.id, bound)
            self.runner_core(self.other_store()).run_chain(self.session.id)
        self.assertNothingExecuted("a damaged binding executed on a later pass")

    def test_an_erased_binding_is_refused_while_the_chain_is_still_active(
        self,
    ) -> None:
        """Not only after a cancel: the proof is gone, so the claim is gone."""
        store = self.other_store()
        core = self.runner_core(store)
        chain_state = self.chain_now()
        task, reason = core.build_chain_step_task(self.stored(), chain_state, 0)
        self.assertIsNone(reason)
        assert task is not None
        outcome, bound = store.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(outcome, "created")
        self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)
        self.rewrite_binding(bound, _BINDING_ERASURES["exact empty binding"])
        self.assertLoadsAsAbsent(bound)
        attempt = self.opened_attempt(bound, "e" * 64)
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, bound, [], self.free_claim()
            ),
            ExecutionClaimOutcome.LOST,
        )
        self.assertNothingExecuted()

    def test_an_unrelated_ordinary_task_stays_claimable(self) -> None:
        """POSITIVE CONTROL. A chain in the session is not a quarantine.

        The session owns a real ACTIVE chain; this task is simply not in any
        of its slots, so it is an ordinary task and claims ordinarily.
        """
        store = self.other_store()
        self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)
        loose = Task(
            objective="stand alone",
            capability="code_analysis",
            scope=ActionScope(action="analyze", destination="anthropic/claude-code"),
        )
        store.save_task(self.session.id, loose)
        self.assertNotIn(loose.id, self.chain_now().step_task_ids)
        self.assertFalse(loose.chain_binding.present)
        attempt = store.open_execution_attempt(
            ExecutionAttempt(
                session_id=self.session.id,
                task_id=loose.id,
                connector="planner",
                fingerprint="f" * 64,
            )
        )
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, loose.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.CLAIMED,
        )

    def test_a_foreign_task_cannot_manufacture_chain_association(self) -> None:
        """A binding is a CLAIM. Only a slot makes it authority.

        A task nobody's chain names, wearing this chain's exact id, digest and
        cursor index, is refused: the cursor slot holds a different task id.
        """
        store = self.other_store()
        chain_state = self.chain_now()
        forged = Task(
            objective=chain_state.steps[0].objective,
            capability=chain_state.steps[0].capability,
            scope=ActionScope(
                action=chain_state.steps[0].action,
                target=chain_state.steps[0].target,
                data_classification=DataClassification.INTERNAL,
            ),
            chain_binding=ChainBinding(
                chain_id=chain_state.id,
                chain_digest=chain_state.digest,
                step_index=chain_state.cursor,
            ),
        )
        store.save_task(self.session.id, forged)
        self.assertNotIn(forged.id, self.chain_now().step_task_ids)
        attempt = store.open_execution_attempt(
            ExecutionAttempt(
                session_id=self.session.id,
                task_id=forged.id,
                connector="reader",
                fingerprint="a1" * 32,
            )
        )
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, forged.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.LOST,
        )
        self.assertNothingExecuted()

    def test_a_slot_task_claimed_under_another_session_is_refused(self) -> None:
        """The owning session decides, not whoever presents the claim."""
        bound = self.bound_then_cancelled()
        stranger = SessionState(objective="a session with no chain")
        self.memory.save_session(stranger)
        store = self.other_store()
        attempt = self.opened_attempt(bound, "b2" * 32)
        self.assertIs(
            store.begin_execution(
                attempt.id, stranger.id, bound, [], self.free_claim()
            ),
            ExecutionClaimOutcome.LOST,
        )
        self.assertNothingExecuted()

    def test_association_is_read_from_the_owning_session_in_transaction(
        self,
    ) -> None:
        """By source: the slot lookup is on the caller's connection."""
        proof = inspect.getsource(SQLiteMemoryStore._chain_claim_authority)
        self.assertIn("self._stored_chain(connection, owner)", proof)
        self.assertIn("step_task_ids", proof)
        # The old fail-open spelling must not come back.
        self.assertNotIn("if not binding.present:\n            return True", proof)
        for forbidden in ("threading", "Lock", "sqlite3.connect", "_connection("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, proof)


# --------------------------------------------------------------------------
# S3B P1-4. Authoritative OWNERSHIP is proven before a task is classified as
# anything at all. Being unbound is the default, never a way past ownership.
# --------------------------------------------------------------------------


class ExecutionOwnerAuthorityTests(ChainArbitrationTestCase):
    """The tasks table owns the row. An attempt row never does."""

    def stranger(self, objective: str = "another session") -> SessionState:
        """A second real session, with no chain of its own."""
        other = SessionState(objective=objective)
        self.memory.save_session(other)
        return other

    def ordinary_task(self, session_id: str) -> Task:
        task = Task(
            objective="stand alone",
            capability="code_analysis",
            scope=ActionScope(action="analyze", destination="anthropic/claude-code"),
        )
        self.memory.save_task(session_id, task)
        self.assertEqual(self.memory.task_session_id(task.id), session_id)
        return task

    def attempt_for(
        self, store: SQLiteMemoryStore, session_id: str, task_id: str, mark: str
    ) -> ExecutionAttempt:
        """Open the attempt UNDER THE CLAIMING session, as an attacker would.

        ``open_execution_attempt`` writes whatever session id it is handed, so
        this is the whole point: the attempt row agrees with the claimant and
        disagrees with storage. Only the tasks table can settle that.
        """
        return store.open_execution_attempt(
            ExecutionAttempt(
                session_id=session_id,
                task_id=task_id,
                connector="planner",
                fingerprint=mark * 32,
            )
        )

    def assertStillPrepared(self, task_id: str) -> None:
        latest = self.memory.latest_execution_attempt(task_id)
        assert latest is not None
        self.assertIs(latest.state, AttemptState.PREPARED)

    # -- 1. FOREIGN UNBOUND ORDINARY TASK -------------------------------

    def test_a_foreign_unbound_ordinary_task_is_never_claimable(self) -> None:
        """THE P1. No binding, no slot - and still not this caller's task."""
        owner_session = self.stranger("owner of the task")
        loose = self.ordinary_task(owner_session.id)
        self.assertFalse(loose.chain_binding.present)
        self.assertNotIn(loose.id, self.chain_now().step_task_ids)
        store = self.other_store()
        attempt = self.attempt_for(store, self.session.id, loose.id, "a1")
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, loose.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.LOST,
            "a foreign ordinary task was claimed",
        )
        self.assertStillPrepared(loose.id)
        self.assertNothingExecuted("foreign ordinary task")

    def test_the_foreign_claim_stays_lost_however_often_it_is_repeated(
        self,
    ) -> None:
        owner_session = self.stranger("owner of the task")
        loose = self.ordinary_task(owner_session.id)
        store = self.other_store()
        attempt = self.attempt_for(store, self.session.id, loose.id, "a2")
        for _ in range(4):
            self.assertIs(
                store.begin_execution(
                    attempt.id, self.session.id, loose.id, [], self.free_claim()
                ),
                ExecutionClaimOutcome.LOST,
            )
            self.assertStillPrepared(loose.id)
        self.assertNothingExecuted("repeated foreign claim")

    def test_a_foreign_claim_is_refused_in_both_directions(self) -> None:
        """Neither session may claim the other's task; it is not a hierarchy."""
        owner_session = self.stranger("owner of the task")
        mine = self.ordinary_task(self.session.id)
        theirs = self.ordinary_task(owner_session.id)
        store = self.other_store()
        for label, claimant, task_id in (
            ("theirs claimed by me", self.session.id, theirs.id),
            ("mine claimed by them", owner_session.id, mine.id),
        ):
            with self.subTest(case=label):
                attempt = self.attempt_for(store, claimant, task_id, "a3")
                self.assertIs(
                    store.begin_execution(
                        attempt.id, claimant, task_id, [], self.free_claim()
                    ),
                    ExecutionClaimOutcome.LOST,
                    label,
                )
                self.assertStillPrepared(task_id)
        self.assertNothingExecuted("cross-session claims")

    # -- 2. FOREIGN TASK NAMED BY THE CALLER'S CHAIN --------------------

    def test_a_caller_chain_slot_cannot_manufacture_ownership(self) -> None:
        """The caller's own chain naming someone else's task proves nothing.

        The claiming session owns a real ACTIVE chain, and its cursor slot is
        rewritten to name a task that belongs to a DIFFERENT session. The
        refusal must come from ownership, before any chain is consulted -
        association is read from the OWNING session's chain, never the
        caller's.
        """
        owner_session = self.stranger("owner of the task")
        foreign = self.ordinary_task(owner_session.id)
        chain_state = self.chain_now()
        self.assertIs(chain_state.status, ChainStatus.ACTIVE)
        self.plant_chain(
            replace(chain_state, step_task_ids=(foreign.id, ""))
        )
        planted = self.chain_now()
        self.assertEqual(planted.step_task_ids[planted.cursor], foreign.id)
        self.assertEqual(self.memory.task_session_id(foreign.id), owner_session.id)
        store = self.other_store()
        attempt = self.attempt_for(store, self.session.id, foreign.id, "b1")
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, foreign.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.LOST,
            "a caller-controlled slot manufactured ownership",
        )
        self.assertStillPrepared(foreign.id)
        self.assertNothingExecuted("caller-controlled slot")

    def test_a_caller_chain_slot_plus_a_matching_binding_still_fails(self) -> None:
        """Even a perfectly shaped binding cannot cross a session boundary."""
        owner_session = self.stranger("owner of the task")
        chain_state = self.chain_now()
        foreign = Task(
            objective=chain_state.steps[0].objective,
            capability=chain_state.steps[0].capability,
            scope=ActionScope(
                action=chain_state.steps[0].action,
                target=chain_state.steps[0].target,
                data_classification=DataClassification.INTERNAL,
            ),
            chain_binding=ChainBinding(
                chain_id=chain_state.id,
                chain_digest=chain_state.digest,
                step_index=chain_state.cursor,
            ),
        )
        self.memory.save_task(owner_session.id, foreign)
        self.plant_chain(replace(chain_state, step_task_ids=(foreign.id, "")))
        self.assertEqual(self.memory.task_session_id(foreign.id), owner_session.id)
        store = self.other_store()
        attempt = self.attempt_for(store, self.session.id, foreign.id, "b2")
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, foreign.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.LOST,
        )
        self.assertNothingExecuted("forged binding across sessions")

    # -- 3. SAME-SESSION ORDINARY POSITIVE CONTROL ----------------------

    def test_a_same_session_ordinary_task_still_claims_normally(self) -> None:
        """POSITIVE CONTROL. Ordinary H2 behaviour is untouched.

        Same session, no binding, not in any slot - and the session owns a
        real chain besides, which must not quarantine anything.
        """
        self.assertIs(self.chain_now().status, ChainStatus.ACTIVE)
        mine = self.ordinary_task(self.session.id)
        self.assertNotIn(mine.id, self.chain_now().step_task_ids)
        store = self.other_store()
        attempt = self.attempt_for(store, self.session.id, mine.id, "c1")
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, mine.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.CLAIMED,
        )

    def test_the_ordinary_claim_is_unchanged_in_a_chainless_session(self) -> None:
        plain = self.stranger("no chain at all")
        mine = self.ordinary_task(plain.id)
        store = self.other_store()
        attempt = self.attempt_for(store, plain.id, mine.id, "c2")
        self.assertIs(
            store.begin_execution(
                attempt.id, plain.id, mine.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.CLAIMED,
        )

    # -- 4. SAME-SESSION VALID CHAIN TASK -------------------------------

    def test_a_same_session_chain_task_still_arbitrates_normally(self) -> None:
        """The full chain proof still runs, and still both accepts and refuses."""
        store = self.other_store()
        core = self.runner_core(store)
        chain_state = self.chain_now()
        task, reason = core.build_chain_step_task(self.stored(), chain_state, 0)
        self.assertIsNone(reason)
        assert task is not None
        outcome, bound = store.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(outcome, "created")
        settled = self.chain_now()
        attempt = self.opened_attempt(bound, "d1" * 32)
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, bound, [], self.free_claim()
            ),
            ExecutionClaimOutcome.CLAIMED,
            "the ordinary chain claim regressed",
        )
        # And the refusals it must still make, each restored afterwards.
        for label, planted in (
            ("cancelled", replace(settled, status=ChainStatus.CANCELLED)),
            ("digest drift", replace(settled, steps=chain(1).steps,
                                     step_task_ids=(bound,))),
            ("cursor moved on", replace(settled, cursor=1,
                                        step_task_ids=(bound, ""))),
            ("slot emptied", replace(settled, step_task_ids=("", ""))),
        ):
            with self.subTest(refusal=label):
                self.plant_chain(planted)
                with contextlib.closing(
                    sqlite3.connect(self.database)
                ) as connection:
                    connection.execute(
                        "UPDATE execution_attempts SET state = ? WHERE id = ?",
                        (AttemptState.PREPARED.value, attempt.id),
                    )
                    connection.commit()
                self.assertIs(
                    store.begin_execution(
                        attempt.id, self.session.id, bound, [], self.free_claim()
                    ),
                    ExecutionClaimOutcome.LOST,
                    label,
                )
        self.plant_chain(settled)

    # -- 5. SAME-SESSION ABSENT/MALFORMED BINDING (NO REGRESSION) -------

    def test_the_erased_binding_fix_has_not_regressed(self) -> None:
        """Same session, slot-named, binding damaged: still fail-closed."""
        for label in ("exact empty binding", "key removed entirely", "null binding"):
            with self.subTest(damage=label):
                self.session = SessionState(objective=f"owner regression {label}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(2)).exit_code, EXIT_OK
                )
                bound = self.bound_then_cancelled()
                self.rewrite_binding(bound, _BINDING_ERASURES[label])
                reloaded = self.memory.load_task(bound)
                assert reloaded is not None
                self.assertFalse(reloaded.chain_binding.present)
                self.assertIn(bound, self.chain_now().step_task_ids)
                self.assertEqual(self.memory.task_session_id(bound), self.session.id)
                store = self.other_store()
                attempt = self.opened_attempt(bound, "e1" * 32)
                self.assertIs(
                    store.begin_execution(
                        attempt.id, self.session.id, bound, [], self.free_claim()
                    ),
                    ExecutionClaimOutcome.LOST,
                    label,
                )
                self.runner_core(self.other_store()).resume(self.session.id, bound)
                self.assertNothingExecuted(label)

    # -- The ORDER itself, which is the whole correction ----------------

    def test_ownership_is_proven_before_any_classification(self) -> None:
        """By source: nothing may be decided above the ownership proof."""
        proof = inspect.getsource(SQLiteMemoryStore._chain_claim_authority)
        owner_check = proof.index("if owner != session_id:")
        for later in (
            "binding = task.chain_binding",
            "chain_binding_refusal(binding)",
            "self._stored_chain(connection, owner)",
            "if not binding.present:",
            "return not named",
            "chain.status is not ChainStatus.ACTIVE",
        ):
            with self.subTest(after=later):
                self.assertGreater(
                    proof.index(later),
                    owner_check,
                    f"{later} is decided before ownership is proven",
                )
        # Exactly one ownership proof, and it is the hoisted one.
        self.assertEqual(proof.count("if owner != session_id:"), 1)
        # Association is read from the OWNER, never from the caller.
        self.assertIn("self._stored_chain(connection, owner)", proof)
        self.assertNotIn("self._stored_chain(connection, session_id)", proof)

    def test_the_claim_contract_states_the_ownership_rule(self) -> None:
        """A conforming store cannot read the contract and still fail open."""
        contract = inspect.getdoc(MemoryStore.begin_execution) or ""
        for phrase in ("AUTHORITATIVE OWNERSHIP", "``session_id``", "LOST"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, contract)
        self.assertIn("OWNING session's chain", contract)


# --------------------------------------------------------------------------
# M11 closeout wave A. Four fail-open holes around the same boundary: an
# unusable owning chain, a hostile step-task id, a malformed row's identity,
# and a context field nothing proved was a mapping.
# --------------------------------------------------------------------------


#: Persisted ``chain`` payloads that rebuild as the typed INVALID verdict -
#: PRESENT, so the session has a chain, but carrying no usable declaration.
_INVALID_CHAIN_PAYLOADS = {
    "not an object": "garbage",
    "empty object": {},
    "steps are not a list": {"id": "chain-1", "steps": "read", "cursor": 0},
    "cursor is not an integer": {
        "id": "chain-1",
        "steps": [dict(READ_STEP)],
        "step_task_ids": [""],
        "cursor": "0",
        "status": "active",
    },
}

#: Task ids the persisted chain contract can never hold, so materialization
#: must never mint one. Each violates exactly one ``chain_id_refusal`` rule.
_HOSTILE_TASK_IDS = {
    "empty": "",
    "leading whitespace": " " + "a" * 36,
    "trailing whitespace": "a" * 36 + "\n",
    "embedded NUL": "a" * 18 + chr(0) + "b" * 17,
    "oversized": "a" * (CHAIN_ID_MAX_CHARS + 1),
    "str subclass": _LoudStr("a" * 36),
    "not a string": 12345,
    "bytes": b"a" * 36,
    "none": None,
}


class InvalidOwningChainTests(ChainArbitrationTestCase):
    """A1. An unusable owning chain refuses classification, never grants it."""

    def corrupt_owning_chain(self, payload: object) -> None:
        """Plant a chain that rebuilds as INVALID, the way corruption would."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (self.session.id,)
            ).fetchone()
            document = json.loads(row[0])
            document["chain"] = payload
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(document), self.session.id),
            )
            connection.commit()
        loaded = self.stored().chain
        assert loaded is not None
        self.assertIs(loaded.status, ChainStatus.INVALID)
        self.assertTrue(loaded.present, "an INVALID chain is still PRESENT")
        self.assertEqual(loaded.step_task_ids, ())

    def test_an_invalid_chain_cannot_resurrect_a_cancelled_step(self) -> None:
        """THE A1 REPRODUCTION, end to end, for every INVALID spelling.

        Materialize, cancel, erase the binding, then corrupt the OWNING chain
        so it loads INVALID with an empty slot tuple - the exact combination
        that used to answer "not a chain task" with confidence and let a
        direct resume run a cancelled chain's step.
        """
        for label, payload in _INVALID_CHAIN_PAYLOADS.items():
            for damage in ("exact empty binding", "key removed entirely"):
                with self.subTest(chain=label, binding=damage):
                    self.session = SessionState(objective=f"A1 {label} {damage}")
                    self.memory.save_session(self.session)
                    self.assertEqual(
                        self.declare(declaration_json(2)).exit_code, EXIT_OK
                    )
                    bound = self.bound_then_cancelled()
                    self.rewrite_binding(bound, _BINDING_ERASURES[damage])
                    reloaded = self.memory.load_task(bound)
                    assert reloaded is not None
                    self.assertFalse(reloaded.chain_binding.present)
                    self.corrupt_owning_chain(payload)

                    store = self.other_store()
                    attempt = self.opened_attempt(bound, "f1" * 32)
                    self.assertIs(
                        store.begin_execution(
                            attempt.id, self.session.id, bound, [], self.free_claim()
                        ),
                        ExecutionClaimOutcome.LOST,
                        f"{label} + {damage} claimed execution",
                    )
                    run = self.runner_core(self.other_store()).resume(
                        self.session.id, bound
                    )
                    self.assertIsNot(run.status, RunStatus.COMPLETED)
                    self.runner_core(self.other_store()).run_chain(self.session.id)
                    self.assertNothingExecuted(f"{label} + {damage}")
                    settled = self.memory.load_task(bound)
                    assert settled is not None
                    self.assertIsNot(settled.status, TaskStatus.COMPLETED)
                    self.assertIs(self.stored().chain.status, ChainStatus.INVALID)

    def test_an_invalid_chain_refuses_a_bound_task_too(self) -> None:
        """Not only the erased-binding path: an intact binding loses as well."""
        store = self.other_store()
        core = self.runner_core(store)
        chain_state = self.chain_now()
        task, _reason = core.build_chain_step_task(self.stored(), chain_state, 0)
        assert task is not None
        outcome, bound = store.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(outcome, "created")
        self.corrupt_owning_chain("garbage")
        attempt = self.opened_attempt(bound, "f2" * 32)
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, bound, [], self.free_claim()
            ),
            ExecutionClaimOutcome.LOST,
        )
        self.assertNothingExecuted()

    def test_a_genuinely_chainless_ordinary_task_stays_claimable(self) -> None:
        """POSITIVE CONTROL 1. Absent is not unusable."""
        plain = SessionState(objective="no chain at all")
        self.memory.save_session(plain)
        self.assertIsNone(self.memory.load_session(plain.id).chain)
        store = self.other_store()
        loose = Task(
            objective="stand alone",
            capability="code_analysis",
            scope=ActionScope(action="analyze", destination="anthropic/claude-code"),
        )
        store.save_task(plain.id, loose)
        attempt = store.open_execution_attempt(
            ExecutionAttempt(
                session_id=plain.id,
                task_id=loose.id,
                connector="planner",
                fingerprint="f3" * 32,
            )
        )
        self.assertIs(
            store.begin_execution(
                attempt.id, plain.id, loose.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.CLAIMED,
        )

    def test_an_ordinary_task_under_a_valid_chain_stays_claimable(self) -> None:
        """POSITIVE CONTROL 2. A usable chain quarantines nothing."""
        current = self.chain_now()
        self.assertIsNone(chain_state_refusal(current))
        self.assertIs(current.status, ChainStatus.ACTIVE)
        store = self.other_store()
        loose = Task(
            objective="stand alone",
            capability="code_analysis",
            scope=ActionScope(action="analyze", destination="anthropic/claude-code"),
        )
        store.save_task(self.session.id, loose)
        self.assertNotIn(loose.id, self.chain_now().step_task_ids)
        attempt = store.open_execution_attempt(
            ExecutionAttempt(
                session_id=self.session.id,
                task_id=loose.id,
                connector="planner",
                fingerprint="f4" * 32,
            )
        )
        self.assertIs(
            store.begin_execution(
                attempt.id, self.session.id, loose.id, [], self.free_claim()
            ),
            ExecutionClaimOutcome.CLAIMED,
        )

    def test_the_refusal_precedes_ordinary_classification(self) -> None:
        """By source: the poison check is above the ordinary early return."""
        proof = inspect.getsource(SQLiteMemoryStore._chain_claim_authority)
        poison = proof.index("chain_state_refusal(chain) is not None")
        for later in ("named = chain is not None", "if not binding.present:",
                      "return not named"):
            with self.subTest(after=later):
                self.assertGreater(proof.index(later), poison, later)


class MaterializedTaskIdTests(MaterializationTestCase):
    """A2. A step task id is judged by the rule the persisted chain uses."""

    def test_every_hostile_task_id_is_refused_and_writes_nothing(self) -> None:
        for label, hostile in _HOSTILE_TASK_IDS.items():
            with self.subTest(task_id=label):
                chain_state = self.chain_now()
                task = self.derive(0)
                object.__setattr__(task, "id", hostile)
                verdict, task_id = self.memory.materialize_chain_step(
                    self.session.id, chain_state.id, chain_state.digest, 0, task
                )
                self.assertEqual(verdict, "conflict", label)
                self.assertEqual(task_id, "")
                self.assertEqual(self.task_rows(), 0, label)
                self.assertEqual(
                    self.chain_now().step_task_ids, ("", "", ""), label
                )
        self.assertNoSideEffects()

    def test_the_id_rule_is_the_persisted_chain_rule(self) -> None:
        """One helper, not a restatement that could drift from the loader."""
        source = inspect.getsource(SQLiteMemoryStore.materialize_chain_step)
        self.assertIn("chain_id_refusal(task.id)", source)
        self.assertNotIn("isinstance(task.id, str)", source)
        # Each hostile id really is refused by that shared rule, and a
        # legitimate one really is not - so the gate is not refusing all.
        for label, hostile in _HOSTILE_TASK_IDS.items():
            with self.subTest(task_id=label):
                self.assertIsNotNone(chain_id_refusal(hostile), label)
        self.assertIsNone(chain_id_refusal(self.derive(0).id))

    def test_a_refused_id_could_never_have_loaded_back(self) -> None:
        """The reason the rule is that rule: such a chain reads as INVALID."""
        chain_state = self.chain_now()
        for label, hostile in _HOSTILE_TASK_IDS.items():
            if not isinstance(hostile, str) or not hostile:
                # ``""`` is the legitimate "no task created yet" marker, so a
                # chain holding it loads fine. It is refused at
                # materialization for a different reason - it is not an
                # IDENTIFIER - and that is asserted above.
                continue
            with self.subTest(task_id=label):
                would_be = replace(
                    chain_state, step_task_ids=(hostile, "", "")
                )
                self.assertIsNotNone(chain_state_refusal(would_be), label)

    def test_a_legitimate_id_still_materializes(self) -> None:
        """POSITIVE CONTROL: the ordinary path is untouched."""
        verdict, task_id = self.materialize(0)
        self.assertEqual(verdict, "created")
        self.assertIsNone(chain_id_refusal(task_id))
        self.assertEqual(self.chain_now().step_task_ids[0], task_id)
        self.assertIsNone(chain_state_refusal(self.chain_now()))


class MalformedTaskRowCollisionTests(MaterializationTestCase):
    """A3. An unreadable row still owns its id, whoever owns the row."""

    def plant_malformed_task(self, task_id: str, owner: str) -> bytes:
        """Insert a task row whose JSON cannot be rebuilt. Returns the bytes."""
        payload = "{not valid json at all"
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO tasks (id, session_id, state_json, status, stage, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    owner,
                    payload,
                    TaskStatus.PENDING.value,
                    LoopStage.UNDERSTAND.value,
                    "2026-08-28T00:00:00Z",
                ),
            )
            connection.commit()
        # The decode-based reader really does report it as ABSENT - the exact
        # confusion under test. Asked through ``_stored_task``, which is what
        # materialization used to trust, a damaged row and a missing row are
        # the same answer.
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(
                SQLiteMemoryStore._stored_task(connection, task_id),
                "the fixture row is not actually undecodable",
            )
            self.assertTrue(
                SQLiteMemoryStore._task_row_exists(connection, task_id),
                "the fixture row was never inserted",
            )
        return self.raw_task_row(task_id)

    def raw_task_row(self, task_id: str) -> bytes:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT id, session_id, state_json, status, stage, updated_at "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        assert row is not None
        return repr(tuple(row)).encode("utf-8")

    def collide(self, owner: str, label: str) -> None:
        chain_state = self.chain_now()
        task = self.derive(0)
        before = self.plant_malformed_task(task.id, owner)
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(verdict, "conflict", label)
        self.assertEqual(task_id, "")
        # The malformed row is untouched, byte for byte, and still foreign.
        self.assertEqual(self.raw_task_row(task.id), before, label)
        self.assertEqual(self.chain_now().step_task_ids, ("", "", ""), label)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE id = ?", (task.id,)
                ).fetchone()[0],
                1,
                label,
            )

    def test_a_malformed_same_session_row_blocks_reuse(self) -> None:
        self.collide(self.session.id, "same session")

    def test_a_malformed_foreign_session_row_blocks_reuse(self) -> None:
        stranger = SessionState(objective="another owner")
        self.memory.save_session(stranger)
        self.collide(stranger.id, "foreign session")

    def test_a_readable_row_still_blocks_reuse(self) -> None:
        """The pre-existing rule is unchanged, not merely widened."""
        chain_state = self.chain_now()
        task = self.derive(0)
        squatter = Task(
            objective="already here",
            capability="code_analysis",
            scope=ActionScope(action="analyze"),
        )
        object.__setattr__(squatter, "id", task.id)
        self.memory.save_task(self.session.id, squatter)
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(verdict, "conflict")
        self.assertEqual(task_id, "")
        self.assertEqual(self.chain_now().step_task_ids, ("", "", ""))

    def test_existence_is_asked_of_the_primary_key(self) -> None:
        """By source: not of a decoder that conflates absence with damage."""
        source = inspect.getsource(SQLiteMemoryStore.materialize_chain_step)
        self.assertIn("self._task_row_exists(connection, task.id)", source)
        self.assertNotIn("self._stored_task(connection, task.id)", source)
        probe = inspect.getsource(SQLiteMemoryStore._task_row_exists)
        self.assertIn("SELECT 1 FROM tasks WHERE id = ?", probe)
        for forbidden in ("json.loads", "Task.from_dict"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, probe)


#: Every persisted ``context`` that is not the canonical mapping. ``null``
#: is the one a real document produces; the rest are the shapes a hostile
#: or truncated one does.
_CONTEXT_SHAPES = {
    "null": None,
    "list": [],
    "populated list": ["local_read_task_id"],
    "string": "local_read_task_id",
    "integer": 0,
    "boolean": False,
    "tuple": (),
}


class MalformedContextTypeTests(MaterializationTestCase):
    """A4. A validator must return a refusal, never raise one."""

    def broken(self, index: int, shape: object) -> Task:
        task = self.derive(index)
        object.__setattr__(task, "context", shape)
        return task

    def test_every_non_mapping_context_refuses_deterministically(self) -> None:
        """Both modes, every shape: a string reason, never an exception."""
        chain_state = self.chain_now()
        for label, shape in _CONTEXT_SHAPES.items():
            for mode in ChainTaskMode:
                with self.subTest(context=label, mode=mode.value):
                    try:
                        reason = chain_task_refusal(
                            self.broken(0, shape), chain_state, 0, mode=mode
                        )
                    except Exception as exc:  # noqa: BLE001 - that IS the bug
                        self.fail(
                            f"{label}/{mode.value} raised "
                            f"{type(exc).__name__} instead of refusing"
                        )
                    self.assertIsInstance(reason, str)
                    self.assertIn("context", reason)

    def test_a_malformed_context_is_refused_at_materialization(self) -> None:
        for label, shape in _CONTEXT_SHAPES.items():
            with self.subTest(context=label):
                chain_state = self.chain_now()
                verdict, task_id = self.memory.materialize_chain_step(
                    self.session.id,
                    chain_state.id,
                    chain_state.digest,
                    0,
                    self.broken(0, shape),
                )
                self.assertEqual(verdict, "conflict", label)
                self.assertEqual(task_id, "")
                self.assertEqual(self.task_rows(), 0, label)
        self.assertNoSideEffects()

    def test_a_malformed_context_blocks_the_observed_and_advance_paths(
        self,
    ) -> None:
        """A row that turns malformed AFTER binding loses authority too."""
        for label, shape in _CONTEXT_SHAPES.items():
            with self.subTest(context=label):
                self.session = SessionState(objective=f"A4 {label}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                chain_state = self.chain_now()
                _verdict, bound = self.materialize(0)
                self.complete_task(bound, selected_connector="local")
                with contextlib.closing(
                    sqlite3.connect(self.database)
                ) as connection:
                    row = connection.execute(
                        "SELECT state_json FROM tasks WHERE id = ?", (bound,)
                    ).fetchone()
                    document = json.loads(row[0])
                    document["context"] = shape
                    connection.execute(
                        "UPDATE tasks SET state_json = ? WHERE id = ?",
                        (json.dumps(document), bound),
                    )
                    connection.commit()
                # OBSERVED: re-materializing the same step re-proves the row.
                self.assertEqual(
                    self.memory.materialize_chain_step(
                        self.session.id,
                        chain_state.id,
                        chain_state.digest,
                        0,
                        self.derive(0),
                    ),
                    ("conflict", ""),
                    label,
                )
                # ADVANCE: a completed step that lost authority cannot move.
                self.assertEqual(self.advance(0), "conflict", label)
                self.assertEqual(self.chain_now().cursor, 0, label)
                self.assertIs(self.chain_now().status, ChainStatus.ACTIVE, label)

    def test_a_malformed_context_stops_the_driver_without_executing(
        self,
    ) -> None:
        """The driver/resume path, with real connectors watching."""
        reader = _RecordingConnector(
            "reader", (LOCAL_READ_CAPABILITY,), snapshot="notes"
        )
        registry = ToolRegistry()
        registry.register(reader)
        core = AgentCore(
            memory=self.memory, registry=registry, settings=self.settings
        )
        chain_state = self.chain_now()
        _verdict, bound = self.materialize(0)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (bound,)
            ).fetchone()
            document = json.loads(row[0])
            document["context"] = None
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), bound),
            )
            connection.commit()
        _run, verdict = core.run_chain(self.session.id)
        self.assertEqual(verdict, "conflict")
        self.assertEqual(reader.calls, [], "a malformed context executed")
        self.assertEqual(self.chain_now().cursor, 0)
        self.assertEqual(self.chain_now().step_task_ids[0], bound)
        self.assertEqual(chain_state.id, self.chain_now().id)

    def test_the_shape_is_proven_before_the_membership_test(self) -> None:
        """By source: no key access above the type proof."""
        proof = inspect.getsource(chain_task_refusal)
        guard = proof.index("if type(task.context) is not dict:")
        self.assertGreater(
            proof.index("LOCAL_READ_TASK_REFERENCE_KEY in task.context"),
            guard,
            "membership is tested before the shape is proven",
        )

    def test_the_canonical_context_is_still_accepted(self) -> None:
        """POSITIVE CONTROL: the empty mapping is the trusted default."""
        task = self.derive(0)
        self.assertEqual(task.context, {})
        self.assertIs(type(task.context), dict)
        for mode in ChainTaskMode:
            with self.subTest(mode=mode.value):
                self.assertIsNone(
                    chain_task_refusal(task, self.chain_now(), 0, mode=mode)
                )


# --------------------------------------------------------------------------
# M11 closeout wave B. What is BOUND must be what is STORED, and what a
# consumer depends on must still be true at the moment it is bound.
# --------------------------------------------------------------------------


#: Declared scope spellings persistence used to rewrite underneath an accepted
#: proposal. Each is (field, declared value, step index it belongs on).
_NONCANONICAL_SCOPE = {
    "uppercase action": ("action", "READ", 0),
    "mixed-case action": ("action", "ReAd", 0),
    "internally spaced action": ("action", "read  it", 0),
    "tabbed action": ("action", "read\tit", 0),
    "uppercase target": ("target", "NOTES.TXT", 0),
    "mixed-case target": ("target", "Notes.Txt", 0),
    "spaced target": ("target", "my  notes.txt", 0),
    "uppercase destination": ("destination", "ANTHROPIC/CLAUDE-CODE", 1),
    "mixed-case destination": ("destination", "Anthropic/Claude-Code", 1),
    "spaced destination": ("destination", "anthropic/claude  code", 1),
    "uppercase URL destination": ("destination", "HTTPS://API.EXAMPLE.COM/v1", 1),
    "URL without a path": ("destination", "https://api.example.com", 1),
}

#: Values the grammar refuses OUTRIGHT. Canonicalization must never repair
#: one into acceptance - that would be a silent widening.
_UNREPAIRABLE_SCOPE = {
    "padded action": ("action", " read", 0),
    "trailing-space action": ("action", "read ", 0),
    "padded target": ("target", " notes.txt", 0),
    "trailing-newline target": ("target", "notes.txt\n", 0),
    "padded destination": ("destination", " anthropic/claude-code", 1),
    "NUL action": ("action", "re" + chr(0) + "ad", 0),
    "NUL target": ("target", "no" + chr(0) + "tes.txt", 0),
}


class PersistedRoundTripAuthorityTests(BoundaryTestCase):
    """B1. An accepted declaration can never bind an unusable task."""

    def setUp(self) -> None:
        super().setUp()
        self.registry = ToolRegistry()
        self.registry.register(_EgressConnector())
        self.registry.register(_LocalConnector())
        self.core = AgentCore(
            memory=self.memory, registry=self.registry, settings=self.settings
        )

    def chain_now(self) -> ChainState:
        stored = self.stored().chain
        assert stored is not None
        return stored

    def declare_with(self, field: str, value: object, index: int) -> object:
        """A fresh session declaring the canonical chain with ONE field changed."""
        self.session = SessionState(objective=f"B1 {field}={value!r}")
        self.memory.save_session(self.session)
        steps = declaration(3)
        steps[index][field] = value
        return self.declare(json.dumps({"steps": steps}))

    def persisted(self, task: Task) -> Task:
        """The task EXACTLY as the row will hold it, rebuilt as the loader does."""
        return Task.from_dict(task.to_dict())

    def complete_like_the_loop(self, task_id: str, **fields: object) -> None:
        """Terminal STORED state, with the result a real run would have left."""
        stored_task = self.memory.load_task(task_id)
        assert stored_task is not None
        stored_task.status = TaskStatus.COMPLETED
        stored_task.stage = LoopStage.COMPLETED
        for key, value in fields.items():
            setattr(stored_task, key, value)
        self.memory.save_task(self.session.id, stored_task)

    def settle_every_step(self, label: str) -> None:
        """Walk all three steps, proving the round trip at EVERY one.

        The invariant is asserted where it broke: a proposal the boundary
        accepted, serialized the way the row will hold it, rebuilt the way the
        loader will rebuild it, must still prove STORED authority.
        """
        finishers = (
            {"selected_connector": "local", "local_read": canonical_read_snapshot()},
            {
                "selected_connector": "planner",
                "model_answer": build_model_answer("A summary."),
            },
            {"selected_connector": "local"},
        )
        for index in range(3):
            chain_state = self.chain_now()
            task, reason = self.core.build_chain_step_task(
                self.stored(), chain_state, index
            )
            self.assertIsNone(reason, f"{label} step {index}: {reason}")
            assert task is not None
            self.assertIsNone(
                chain_task_refusal(
                    task, chain_state, index, mode=ChainTaskMode.PROPOSAL
                ),
                f"{label} step {index}: PROPOSAL",
            )
            self.assertIsNone(
                chain_task_refusal(
                    self.persisted(task), chain_state, index, mode=ChainTaskMode.STORED
                ),
                f"{label} step {index}: the persisted form lost its authority",
            )
            verdict, task_id = self.memory.materialize_chain_step(
                self.session.id, chain_state.id, chain_state.digest, index, task
            )
            self.assertEqual(verdict, "created", f"{label} step {index}")
            reloaded = self.memory.load_task(task_id)
            assert reloaded is not None
            self.assertIsNone(
                chain_task_refusal(
                    reloaded, self.chain_now(), index, mode=ChainTaskMode.STORED
                ),
                f"{label} step {index}: the BOUND row fails its own proof",
            )
            self.complete_like_the_loop(task_id, **finishers[index])
            expected = "completed" if index == 2 else "advanced"
            self.assertEqual(
                self.memory.advance_chain_step(
                    self.session.id, chain_state.id, chain_state.digest, index
                ),
                expected,
                f"{label} step {index}",
            )

    def test_every_accepted_declaration_freezes_the_persisted_spelling(
        self,
    ) -> None:
        """The declaration holds what the row will hold. A FIXED POINT.

        The scope built from a step, normalized the way persistence normalizes
        it, must equal the step itself - otherwise the declaration froze a
        spelling that can never exist in storage.
        """
        for label, (field, value, index) in _NONCANONICAL_SCOPE.items():
            with self.subTest(spelling=label):
                self.assertEqual(
                    self.declare_with(field, value, index).exit_code,
                    EXIT_OK,
                    f"{label} was refused at the boundary",
                )
                for step in self.chain_now().steps:
                    scope = ActionScope(
                        action=step.action,
                        target=step.target,
                        destination=step.destination,
                    ).normalized()
                    self.assertEqual(scope.action, step.action, label)
                    self.assertEqual(scope.destination, step.destination, label)
                    if not looks_like_path(step.target):
                        self.assertEqual(scope.target, step.target, label)

    def test_every_accepted_declaration_round_trips_into_stored_authority(
        self,
    ) -> None:
        """THE B1 INVARIANT, at every step of every noncanonical spelling."""
        for label, (field, value, index) in _NONCANONICAL_SCOPE.items():
            with self.subTest(spelling=label):
                self.assertEqual(
                    self.declare_with(field, value, index).exit_code, EXIT_OK
                )
                self.settle_every_step(label)
                self.assertIs(self.chain_now().status, ChainStatus.COMPLETED, label)

    def test_the_bound_row_proves_stored_authority_when_read_back(self) -> None:
        """Not the in-memory object: the row, re-read from the database."""
        for label, (field, value, index) in _NONCANONICAL_SCOPE.items():
            if index != 0:
                continue
            with self.subTest(spelling=label):
                self.assertEqual(
                    self.declare_with(field, value, index).exit_code, EXIT_OK
                )
                chain_state = self.chain_now()
                task, _reason = self.core.build_chain_step_task(
                    self.stored(), chain_state, 0
                )
                assert task is not None
                verdict, task_id = self.memory.materialize_chain_step(
                    self.session.id, chain_state.id, chain_state.digest, 0, task
                )
                self.assertEqual(verdict, "created", label)
                reloaded = self.memory.load_task(task_id)
                assert reloaded is not None
                self.assertIsNone(
                    chain_task_refusal(
                        reloaded, self.chain_now(), 0, mode=ChainTaskMode.STORED
                    ),
                    f"{label}: the BOUND row fails its own authority proof",
                )

    def test_a_drifting_representation_is_refused_and_binds_nothing(self) -> None:
        """A step planted noncanonically cannot bind, however it got there.

        The declaration boundary can no longer produce this, so it is planted
        directly - which is exactly the case the transactional round-trip
        proof exists for: storage does not get to assume its input came
        through the boundary.
        """
        for label, (field, value, index) in _NONCANONICAL_SCOPE.items():
            if index != 0 or field == "target":
                continue
            with self.subTest(spelling=label):
                self.session = SessionState(objective=f"planted {label}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(2)).exit_code, EXIT_OK
                )
                honest = self.chain_now()
                drifted = replace(
                    honest,
                    steps=(replace(honest.steps[0], **{field: value}),)
                    + honest.steps[1:],
                )
                self.plant_chain(drifted)
                planted = self.chain_now()
                self.assertEqual(getattr(planted.steps[0], field), value)
                task, reason = self.core.build_chain_step_task(
                    self.stored(), planted, 0
                )
                self.assertIsNone(reason, label)
                assert task is not None
                self.assertIsNone(
                    chain_task_refusal(
                        task, planted, 0, mode=ChainTaskMode.PROPOSAL
                    ),
                    f"{label}: the PROPOSAL should still pass; that is the bug",
                )
                self.assertEqual(
                    self.memory.materialize_chain_step(
                        self.session.id, planted.id, planted.digest, 0, task
                    ),
                    ("conflict", ""),
                    label,
                )
                self.assertEqual(self.task_rows(), 0, label)
                self.assertEqual(self.chain_now().step_task_ids, ("", ""), label)

    def test_canonicalization_never_repairs_a_refused_declaration(self) -> None:
        """It respells what the grammar ACCEPTED. It never rescues anything."""
        for label, (field, value, index) in _UNREPAIRABLE_SCOPE.items():
            with self.subTest(spelling=label):
                steps = declaration(3)
                steps[index][field] = value
                self.assertIsNotNone(
                    chain_declaration_refusal(steps),
                    f"{label} was silently repaired into acceptance",
                )
                self.assertEqual(chain_steps_from_declaration(steps), ())
                self.assertNotEqual(
                    self.declare_with(field, value, index).exit_code,
                    EXIT_OK,
                    label,
                )
                self.assertIsNone(self.stored().chain, label)

    def test_a_canonical_declaration_is_completely_unaffected(self) -> None:
        """POSITIVE CONTROL: the ordinary declaration is byte-identical."""
        canonical = chain_steps_from_declaration(declaration(3))
        self.assertEqual(len(canonical), 3)
        for index, raw in enumerate(declaration(3)):
            with self.subTest(step=index):
                self.assertEqual(canonical[index].action, raw["action"])
                self.assertEqual(canonical[index].target, raw["target"])
                self.assertEqual(canonical[index].destination, raw["destination"])
                self.assertEqual(canonical[index].objective, raw["objective"])
                self.assertEqual(canonical[index].capability, raw["capability"])

    def test_canonicalization_is_idempotent_and_survives_reload(self) -> None:
        """A reloaded chain re-parses to exactly the same steps and digest."""
        for label, (field, value, index) in _NONCANONICAL_SCOPE.items():
            with self.subTest(spelling=label):
                self.assertEqual(
                    self.declare_with(field, value, index).exit_code, EXIT_OK
                )
                once = self.chain_now()
                twice = ChainState.from_dict(json.loads(json.dumps(once.to_dict())))
                self.assertEqual(twice.steps, once.steps, label)
                self.assertEqual(twice.digest, once.digest, label)
                self.assertIsNone(chain_state_refusal(twice), label)

    def test_the_stored_proof_runs_before_the_slot_is_bound(self) -> None:
        """By source, and in the required order."""
        source = inspect.getsource(SQLiteMemoryStore.materialize_chain_step)
        marks = {
            "chain": source.index("self._chain_for_update("),
            "proposal": source.index("mode=ChainTaskMode.PROPOSAL"),
            "task_id": source.index("chain_id_refusal(task.id)"),
            "exists": source.index("self._task_row_exists(connection, task.id)"),
            "predecessor": source.index("self._chain_predecessor_refusal("),
            "round_trip": source.index("persisted = Task.from_dict(task.to_dict())"),
            "stored": source.index(
                "persisted, stored, step_index, mode=ChainTaskMode.STORED"
            ),
            "candidate": source.index("chain_state_refusal(candidate)"),
            "write_task": source.index("self._upsert_task("),
            "bind": source.index("chain_authority=True"),
        }
        order = [
            "chain", "proposal", "task_id", "exists", "predecessor",
            "round_trip", "stored", "candidate", "write_task", "bind",
        ]
        positions = [marks[name] for name in order]
        self.assertEqual(positions, sorted(positions), f"out of order: {marks}")


class PredecessorReProofTests(MaterializationTestCase):
    """B2. A derivation-time proof is not a materialization-time fact."""

    def settled_consumer(self) -> tuple[Task, str, str]:
        """READ and MODEL settled the ordinary way; returns the CREATE proposal."""
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.assertEqual(self.advance(0), "advanced")
        _verdict, model_id = self.materialize(1)
        self.finish_model_step(model_id)
        self.assertEqual(self.advance(1), "advanced")
        consumer, reason = self.core.build_chain_step_task(
            self.stored(), self.chain_now(), 2
        )
        self.assertIsNone(reason, f"derivation refused: {reason}")
        assert consumer is not None
        return consumer, read_id, model_id

    def materialize_consumer(self, consumer: Task) -> tuple[str, str]:
        chain_state = self.chain_now()
        return self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 2, consumer
        )

    def assertConsumerRefused(self, consumer: Task, label: str) -> str:
        """Refused, and nothing written. Returns the verdict for the caller.

        The verdict is not pinned to one spelling here: a tampering that also
        invalidates the CHAIN loses at the earlier ``inactive`` gate, which is
        the same fail-closed outcome reached sooner. What must hold in every
        case is that no consumer row exists and no slot moved.
        """
        before = self.chain_now().step_task_ids
        rows = self.task_rows()
        verdict, task_id = self.materialize_consumer(consumer)
        self.assertNotEqual(verdict, "created", label)
        self.assertEqual(task_id, "", label)
        self.assertEqual(self.chain_now().step_task_ids, before, label)
        self.assertEqual(self.task_rows(), rows, f"{label}: a consumer row was written")
        return verdict

    def tamper_predecessor(self, task_id: str, **fields: object) -> None:
        stored_task = self.memory.load_task(task_id)
        assert stored_task is not None
        for key, value in fields.items():
            setattr(stored_task, key, value)
        self.memory.save_task(self.session.id, stored_task)

    def test_a_tampered_predecessor_objective_refuses_the_consumer(self) -> None:
        """THE B2 REPRODUCTION."""
        consumer, _read_id, model_id = self.settled_consumer()
        self.tamper_predecessor(model_id, objective="something else entirely")
        # The chain itself is untouched here, so the refusal is the
        # predecessor re-proof and nothing else.
        self.assertEqual(
            self.assertConsumerRefused(consumer, "tampered objective"), "conflict"
        )

    def test_every_authority_representative_refuses_the_consumer(self) -> None:
        """One tampering per authority surface the derivation relied on."""
        tamperings = {
            "objective": {"objective": "rewritten"},
            "capability": {"capability": LOCAL_READ_CAPABILITY},
            "scope action": {
                "scope": ActionScope(
                    action="exfiltrate",
                    destination="anthropic/claude-code",
                    data_classification=DataClassification.INTERNAL,
                )
            },
            "scope destination": {
                "scope": ActionScope(
                    action="analyze",
                    destination="https://elsewhere.example.com/v1",
                    data_classification=DataClassification.INTERNAL,
                )
            },
            "classification": {
                "scope": ActionScope(
                    action="analyze",
                    destination="anthropic/claude-code",
                    data_classification=DataClassification.SECRET,
                )
            },
            "chain binding": {"chain_binding": ChainBinding()},
            "result: model answer removed": {"model_answer": ModelAnswer()},
            "routing: producer name removed": {"selected_connector": None},
            "status: reopened": {"status": TaskStatus.RUNNING},
        }
        for label, fields in tamperings.items():
            with self.subTest(tampering=label):
                self.session = SessionState(objective=f"B2 {label}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                consumer, _read_id, model_id = self.settled_consumer()
                self.tamper_predecessor(model_id, **fields)
                self.assertConsumerRefused(consumer, label)

    def test_a_foreign_owned_predecessor_refuses_the_consumer(self) -> None:
        consumer, _read_id, model_id = self.settled_consumer()
        stranger = SessionState(objective="another owner")
        self.memory.save_session(stranger)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?",
                (stranger.id, model_id),
            )
            connection.commit()
        self.assertConsumerRefused(consumer, "foreign predecessor")

    def test_an_unreadable_predecessor_refuses_the_consumer(self) -> None:
        consumer, _read_id, model_id = self.settled_consumer()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                ("{not json", model_id),
            )
            connection.commit()
        self.assertConsumerRefused(consumer, "unreadable predecessor")

    def test_a_predecessor_that_is_not_completed_refuses_the_consumer(self) -> None:
        for status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.FAILED):
            with self.subTest(status=status.value):
                self.session = SessionState(objective=f"B2 {status.value}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                consumer, _read_id, model_id = self.settled_consumer()
                with contextlib.closing(
                    sqlite3.connect(self.database)
                ) as connection:
                    row = connection.execute(
                        "SELECT state_json FROM tasks WHERE id = ?", (model_id,)
                    ).fetchone()
                    document = json.loads(row[0])
                    document["status"] = status.value
                    connection.execute(
                        "UPDATE tasks SET state_json = ?, status = ? WHERE id = ?",
                        (json.dumps(document), status.value, model_id),
                    )
                    connection.commit()
                self.assertConsumerRefused(consumer, status.value)

    def test_a_slot_mismatch_refuses_the_consumer(self) -> None:
        """The consumer must be routed at the CHAIN's predecessor, not another."""
        consumer, read_id, model_id = self.settled_consumer()
        self.assertEqual(consumer.write_source_task_id, model_id)
        for label, forged in (
            ("routed at the read step", read_id),
            ("routed at itself", consumer.id),
            ("routed at nothing", ""),
            ("routed at an unknown task", "0" * 36),
        ):
            with self.subTest(routing=label):
                candidate = Task.from_dict(consumer.to_dict())
                candidate.write_source_task_id = forged
                self.assertConsumerRefused(candidate, label)

    def test_an_emptied_predecessor_slot_refuses_the_consumer(self) -> None:
        consumer, read_id, _model_id = self.settled_consumer()
        current = self.chain_now()
        self.plant_chain(replace(current, step_task_ids=(read_id, "", "")))
        self.assertConsumerRefused(consumer, "emptied slot")

    def test_an_unchanged_predecessor_still_materializes(self) -> None:
        """POSITIVE CONTROL: the honest CREATE step is completely unaffected."""
        consumer, _read_id, model_id = self.settled_consumer()
        verdict, task_id = self.materialize_consumer(consumer)
        self.assertEqual(verdict, "created")
        self.assertEqual(task_id, consumer.id)
        self.assertEqual(self.chain_now().step_task_ids[2], task_id)
        self.assertIsNone(chain_state_refusal(self.chain_now()))
        bound = self.memory.load_task(task_id)
        assert bound is not None
        self.assertEqual(bound.write_source_task_id, model_id)
        self.assertIsNone(
            chain_task_refusal(bound, self.chain_now(), 2, mode=ChainTaskMode.STORED)
        )

    def test_a_predecessor_free_step_is_not_gated_by_this(self) -> None:
        """Step 0 consumes nothing, so the re-proof must not touch it."""
        verdict, task_id = self.materialize(0)
        self.assertEqual(verdict, "created")
        self.assertTrue(task_id)

    def test_the_reproof_reads_storage_and_not_the_registry(self) -> None:
        """Provider independence: no manifest, no connector, no registry."""
        whole = inspect.getsource(SQLiteMemoryStore._chain_predecessor_refusal)
        self.assertIn("self._stored_task(connection, predecessor_id)", whole)
        self.assertIn("mode=ChainTaskMode.STORED", whole)
        # The CODE, not the prose: the docstring is allowed to explain where
        # the registry question lives, and a substring scan over it would be
        # testing the comment rather than the behaviour.
        proof = whole.split('"""', 2)[-1]
        self.assertNotIn("Provider-neutral by construction", proof)
        for forbidden in (
            "registry", "manifest", "SideEffect", "can_handle",
            "EXTERNAL_EGRESS", "sqlite3.connect", "_connection(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, proof)

    def test_the_registry_verdict_is_still_re_proved_before_any_write_byte(
        self,
    ) -> None:
        """What storage cannot know, AgentCore still refuses - twice.

        Storage pins that the predecessor still NAMES a producer; WHICH
        connectors are legitimate external-model producers stays a registry
        question. It is asked at derivation and asked again by the resolver
        before the answer is used, so a producer swapped to a non-egress
        connector after binding still yields no write.
        """
        consumer, _read_id, model_id = self.settled_consumer()
        verdict, task_id = self.materialize_consumer(consumer)
        self.assertEqual(verdict, "created")
        self.tamper_predecessor(model_id, selected_connector="local")
        source = self.memory.load_task(model_id)
        assert source is not None
        self.assertIsNotNone(
            self.core._chain_producer_refusal(source),
            "a non-egress producer was accepted",
        )
        rebuilt, reason = self.core.build_chain_step_task(
            self.stored(), self.chain_now(), 2
        )
        self.assertIsNone(rebuilt, f"derivation should refuse, got {reason}")
        bound = self.memory.load_task(task_id)
        assert bound is not None
        self.assertEqual(bound.write_content, "", "write bytes were bound")


# --------------------------------------------------------------------------
# M11 F5. A COMPLETED read is not the prerequisite. Having the SNAPSHOT is.
# --------------------------------------------------------------------------


#: Persisted ``local_read`` payloads a real read could never have produced.
#: Every one of these degrades to an UNUSABLE snapshot on load - which is
#: exactly why "capability == file_read and status == COMPLETED" was not
#: enough. A payload the load boundary legitimately normalizes (a non-string
#: excerpt becomes a bounded string) is not listed: that is the loader doing
#: its job, not a hole, and the test below asserts unusability outright so a
#: wrong entry here fails rather than passing quietly.
_UNUSABLE_SNAPSHOTS = {
    "key removed entirely": _DROPPED,
    "null": None,
    "not an object": "notes.txt",
    "a list": [],
    "an integer": 0,
    "empty relative path": {"relative_path": ""},
    "absolute path": {"relative_path": "C:/secrets/notes.txt"},
    "malformed digest": {"sha256": "nope"},
    "uppercase digest": {"sha256": "A" * 64},
    "byte size is not an integer": {"byte_size": "17"},
    "byte size is negative": {"byte_size": -1},
    "noncanonical excerpt": {"excerpt": "a\r\nb"},
    "truncated is not a boolean": {"truncated": 1},
    "unknown classification": {"classification": "top-secret"},
    "read timestamp is not canonical": {"read_at": "yesterday"},
    "bounded region": {"start_line": 2, "end_line": 3},
}


class ReadSnapshotPrerequisiteTests(MaterializationTestCase):
    """F5. READ -> MODEL binds only on a usable authoritative snapshot."""

    def plant_snapshot(self, task_id: str, payload: object) -> None:
        """Write an arbitrary ``local_read`` into the PERSISTED row.

        Raw SQL because no API produces these; the question is what storage
        does when it MEETS one. A partial mapping is merged onto the canonical
        snapshot, so exactly one field is hostile and the refusal cannot be
        blamed on anything else.
        """
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            document = json.loads(row[0])
            if payload is _DROPPED:
                document.pop("local_read", None)
            elif isinstance(payload, dict):
                merged = dict(canonical_read_snapshot().to_dict())
                merged.update(payload)
                document["local_read"] = merged
            else:
                document["local_read"] = payload
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task_id),
            )
            connection.commit()

    def settled_read(self) -> str:
        """Step 0 materialized and COMPLETED the ordinary way."""
        verdict, read_id = self.materialize(0)
        self.assertEqual(verdict, "created")
        self.complete_task(read_id, selected_connector="local")
        self.assertEqual(self.advance(0), "advanced")
        return read_id

    def consumer_verdict(self) -> tuple[str, str]:
        chain_state = self.chain_now()
        consumer, reason = self.core.build_chain_step_task(
            self.stored(), chain_state, 1
        )
        self.assertIsNone(reason, f"derivation refused: {reason}")
        assert consumer is not None
        return self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 1, consumer
        )

    def assertConsumerRefused(self, label: str) -> None:
        before = self.chain_now().step_task_ids
        rows = self.task_rows()
        self.assertEqual(self.consumer_verdict(), ("conflict", ""), label)
        self.assertEqual(self.chain_now().step_task_ids, before, label)
        self.assertEqual(self.task_rows(), rows, f"{label}: a consumer row appeared")

    def test_no_unusable_snapshot_can_bind_a_model_consumer(self) -> None:
        """THE F5 REPRODUCTION, over every way a snapshot can be unusable."""
        for label, payload in _UNUSABLE_SNAPSHOTS.items():
            with self.subTest(snapshot=label):
                self.session = SessionState(objective=f"F5 {label}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                read_id = self.settled_read()
                self.plant_snapshot(read_id, payload)
                # The predecessor is still a COMPLETED file_read owned by this
                # session and still passes the full STORED proof: the ONLY
                # thing wrong with it is its result.
                reloaded = self.memory.load_task(read_id)
                assert reloaded is not None
                self.assertIs(reloaded.status, TaskStatus.COMPLETED, label)
                self.assertEqual(reloaded.capability, LOCAL_READ_CAPABILITY, label)
                self.assertIsNone(
                    chain_task_refusal(
                        reloaded, self.chain_now(), 0, mode=ChainTaskMode.STORED
                    ),
                    label,
                )
                self.assertIsNotNone(
                    local_read_snapshot_refusal(reloaded.local_read), label
                )
                self.assertConsumerRefused(label)

    def test_an_absent_snapshot_is_refused_even_though_it_is_well_formed(
        self,
    ) -> None:
        """The default snapshot is a VALID object. It is simply not present."""
        read_id = self.settled_read()
        self.plant_snapshot(read_id, LocalReadSnapshot().to_dict())
        reloaded = self.memory.load_task(read_id)
        assert reloaded is not None
        self.assertIs(type(reloaded.local_read), LocalReadSnapshot)
        self.assertFalse(reloaded.local_read.present)
        self.assertConsumerRefused("absent but well formed")

    def test_a_bounded_region_snapshot_is_refused_as_cross_task_context(
        self,
    ) -> None:
        """M8's own rule, re-proved here: a region describes only its lines."""
        read_id = self.settled_read()
        region = replace(
            canonical_read_snapshot("alpha\nbeta"), start_line=1, end_line=2
        )
        self.assertIsNone(local_read_snapshot_refusal(region))
        self.plant_snapshot(read_id, region.to_dict())
        reloaded = self.memory.load_task(read_id)
        assert reloaded is not None
        # Structurally VALID, so the refusal is the region rule and nothing else.
        self.assertIsNone(local_read_snapshot_refusal(reloaded.local_read))
        self.assertTrue(reloaded.local_read.present)
        self.assertConsumerRefused("bounded region")

    def test_a_valid_snapshot_still_binds_the_model_consumer(self) -> None:
        """POSITIVE CONTROL. The honest READ -> MODEL hop is unaffected."""
        read_id = self.settled_read()
        reloaded = self.memory.load_task(read_id)
        assert reloaded is not None
        self.assertTrue(reloaded.local_read.present)
        self.assertIsNone(local_read_snapshot_refusal(reloaded.local_read))
        verdict, task_id = self.consumer_verdict()
        self.assertEqual(verdict, "created")
        self.assertEqual(self.chain_now().step_task_ids[1], task_id)
        bound = self.memory.load_task(task_id)
        assert bound is not None
        self.assertEqual(bound.local_read_source_task_ids, (read_id,))
        self.assertIsNone(
            chain_task_refusal(bound, self.chain_now(), 1, mode=ChainTaskMode.STORED)
        )

    def test_a_valid_but_hostile_classification_is_neutralized_not_trusted(
        self,
    ) -> None:
        """A snapshot claiming SECRET is not authority; the SOURCE SCOPE is.

        ``secret`` is a real enum value, so the row loads structurally clean.
        M8 rebinds the classification from the source task's own trusted
        scope - which the chain STORED proof already pins to INTERNAL - so the
        claim carries nothing and the hop is still legitimate.
        """
        read_id = self.settled_read()
        self.plant_snapshot(read_id, {"classification": "secret"})
        reloaded = self.memory.load_task(read_id)
        assert reloaded is not None
        self.assertIs(reloaded.local_read.classification, DataClassification.SECRET)
        self.assertIsNone(local_read_snapshot_refusal(reloaded.local_read))
        # The authority that actually decides, and what it says.
        self.assertIs(
            reloaded.scope.data_classification, DataClassification.INTERNAL
        )
        verdict, task_id = self.consumer_verdict()
        self.assertEqual(verdict, "created")
        bound = self.memory.load_task(task_id)
        assert bound is not None
        self.assertIs(
            bound.scope.data_classification, DataClassification.INTERNAL
        )

    def test_the_other_predecessor_proofs_are_retained(self) -> None:
        """F5 adds a prerequisite; it replaces none of the existing ones.

        Each tampering lands AFTER the consumer is derived - the core-side
        proof would refuse earlier otherwise, and it is the transaction-local
        re-proof that is under test here.
        """
        tamperings = {
            "foreign owner": "owner",
            "not completed": "status",
            "tampered objective": "objective",
        }
        for label, kind in tamperings.items():
            with self.subTest(tampering=label):
                self.session = SessionState(objective=f"F5 retained {label}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(3)).exit_code, EXIT_OK
                )
                read_id = self.settled_read()
                chain_state = self.chain_now()
                consumer, reason = self.core.build_chain_step_task(
                    self.stored(), chain_state, 1
                )
                self.assertIsNone(reason, label)
                assert consumer is not None
                if kind == "owner":
                    stranger = SessionState(objective="another owner")
                    self.memory.save_session(stranger)
                    with contextlib.closing(
                        sqlite3.connect(self.database)
                    ) as connection:
                        connection.execute(
                            "UPDATE tasks SET session_id = ? WHERE id = ?",
                            (stranger.id, read_id),
                        )
                        connection.commit()
                elif kind == "status":
                    with contextlib.closing(
                        sqlite3.connect(self.database)
                    ) as connection:
                        document = json.loads(
                            connection.execute(
                                "SELECT state_json FROM tasks WHERE id = ?",
                                (read_id,),
                            ).fetchone()[0]
                        )
                        document["status"] = TaskStatus.RUNNING.value
                        connection.execute(
                            "UPDATE tasks SET state_json = ?, status = ? "
                            "WHERE id = ?",
                            (
                                json.dumps(document),
                                TaskStatus.RUNNING.value,
                                read_id,
                            ),
                        )
                        connection.commit()
                else:
                    self.complete_task(read_id, objective="rewritten")
                before = self.chain_now().step_task_ids
                rows = self.task_rows()
                self.assertEqual(
                    self.memory.materialize_chain_step(
                        self.session.id,
                        chain_state.id,
                        chain_state.digest,
                        1,
                        consumer,
                    ),
                    ("conflict", ""),
                    label,
                )
                self.assertEqual(self.chain_now().step_task_ids, before, label)
                self.assertEqual(self.task_rows(), rows, label)

    def test_the_snapshot_proof_uses_the_one_contract_and_no_registry(
        self,
    ) -> None:
        """Provider independence: the shared contract, never a manifest."""
        whole = inspect.getsource(SQLiteMemoryStore._chain_predecessor_refusal)
        proof = whole.split('"""', 2)[-1]
        self.assertIn("local_read_snapshot_refusal(snapshot)", proof)
        self.assertIn("self._stored_task(connection, predecessor_id)", proof)
        for forbidden in (
            "registry", "manifest", "SideEffect", "can_handle",
            "EXTERNAL_EGRESS", "sqlite3.connect", "_connection(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, proof)


# --------------------------------------------------------------------------
# M11 F6. A malformed context refuses deterministically at RUNTIME too.
# --------------------------------------------------------------------------


class _LoudDict(dict):
    """A ``dict`` SUBCLASS. Not the declared exact type."""

    def __contains__(self, key: object) -> bool:  # pragma: no cover - never run
        raise AssertionError("membership was tested on an unproven mapping")


#: Every persisted ``context`` that is not the canonical exact mapping.
_RUNTIME_CONTEXTS = {
    "null": None,
    "list": [],
    "populated list": ["local_read_task_id"],
    "string": "local_read_task_id",
    "integer": 0,
    "boolean": False,
}


class RuntimeContextTypeTests(ChainArbitrationTestCase):
    """F6. No membership test on ``task.context`` before its shape is proven."""

    def plant_context(self, task_id: str, payload: object) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            document = json.loads(row[0])
            document["context"] = payload
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task_id),
            )
            connection.commit()

    def bound_step_task(self) -> str:
        store = self.other_store()
        core = self.runner_core(store)
        chain_state = self.chain_now()
        task, reason = core.build_chain_step_task(self.stored(), chain_state, 0)
        self.assertIsNone(reason)
        assert task is not None
        verdict, bound = store.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(verdict, "created")
        return bound

    def test_direct_resume_refuses_instead_of_raising(self) -> None:
        """THE F6 REPRODUCTION: ``resume`` used to raise ``TypeError``."""
        for label, payload in _RUNTIME_CONTEXTS.items():
            with self.subTest(context=label):
                self.session = SessionState(objective=f"F6 {label}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(2)).exit_code, EXIT_OK
                )
                bound = self.bound_step_task()
                self.plant_context(bound, payload)
                reloaded = self.memory.load_task(bound)
                assert reloaded is not None
                self.assertIsNot(type(reloaded.context), dict, label)
                try:
                    run = self.runner_core(self.other_store()).resume(
                        self.session.id, bound
                    )
                except Exception as exc:  # noqa: BLE001 - that IS the bug
                    self.fail(
                        f"{label}: resume raised {type(exc).__name__} "
                        f"instead of refusing"
                    )
                self.assertIs(run.status, RunStatus.FAILED, label)
                assert run.error is not None
                self.assertIs(run.error.code, ErrorCode.INVALID_TASK, label)
                self.assertIn("context", run.error.message, label)
                self.assertNothingExecuted(label)

    def test_the_refusal_is_terminal_and_repeatable(self) -> None:
        """A bad row stays refused, and the task stops rather than spinning."""
        bound = self.bound_step_task()
        self.plant_context(bound, None)
        for _ in range(3):
            run = self.runner_core(self.other_store()).resume(self.session.id, bound)
            self.assertIs(run.status, RunStatus.FAILED)
        settled = self.memory.load_task(bound)
        assert settled is not None
        self.assertIs(settled.status, TaskStatus.FAILED)
        self.assertNothingExecuted("repeated resume")

    def test_the_driver_stops_without_executing(self) -> None:
        for label, payload in _RUNTIME_CONTEXTS.items():
            with self.subTest(context=label):
                self.session = SessionState(objective=f"F6 driver {label}")
                self.memory.save_session(self.session)
                self.assertEqual(
                    self.declare(declaration_json(2)).exit_code, EXIT_OK
                )
                bound = self.bound_step_task()
                self.plant_context(bound, payload)
                try:
                    _run, verdict = self.runner_core(self.other_store()).run_chain(
                        self.session.id
                    )
                except Exception as exc:  # noqa: BLE001
                    self.fail(
                        f"{label}: run_chain raised {type(exc).__name__}"
                    )
                self.assertNotEqual(verdict, "advanced", label)
                self.assertEqual(self.chain_now().cursor, 0, label)
                self.assertNothingExecuted(label)

    def test_an_exact_dict_subclass_is_refused_without_being_consulted(
        self,
    ) -> None:
        """The contract is EXACT ``dict``, so the subclass is never asked.

        ``_LoudDict.__contains__`` raises if it is ever reached, so this
        proves the type gate runs before any membership test rather than
        merely alongside one.
        """
        bound = self.bound_step_task()
        task = self.memory.load_task(bound)
        assert task is not None
        task.context = _LoudDict()
        run = self.runner_core(self.other_store())._local_read_context_failure(
            self.stored(), task
        )
        self.assertIsNotNone(run)
        assert run is not None
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.INVALID_TASK)
        self.assertIn("context", run.error.message)

    def test_the_type_guard_precedes_every_membership_test(self) -> None:
        """By source, in the one runtime gate every later reader sits behind."""
        proof = inspect.getsource(AgentCore._local_read_context_failure)
        guard = proof.index("if type(task.context) is not dict:")
        for later in (
            "LOCAL_READ_CONTEXT_REFERENCE_KEY in task.context",
            "LOCAL_READ_TASK_REFERENCE_KEY in task.context",
        ):
            with self.subTest(after=later):
                self.assertGreater(proof.index(later), guard, later)

    def test_the_existing_refusal_path_is_reused(self) -> None:
        """No new lifecycle mechanism: the ordinary terminal invalid-task one."""
        proof = inspect.getsource(AgentCore._local_read_context_failure)
        self.assertIn("self._terminal_failure(", proof)
        self.assertIn("ErrorCode.INVALID_TASK", proof)
        self.assertEqual(proof.count("return self._terminal_failure("), 1)

    def test_the_storage_paths_still_refuse_the_same_row(self) -> None:
        """Materialize and advance keep their own independent refusal."""
        bound = self.bound_step_task()
        chain_state = self.chain_now()
        self.plant_context(bound, None)
        store = self.other_store()
        core = self.runner_core(store)
        task, _reason = core.build_chain_step_task(self.stored(), chain_state, 0)
        assert task is not None
        self.assertEqual(
            store.materialize_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0, task
            ),
            ("conflict", ""),
        )
        self.assertEqual(
            store.advance_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0
            ),
            "conflict",
        )
        self.assertEqual(self.chain_now().cursor, 0)
        self.assertNothingExecuted()

    def test_the_canonical_context_still_runs(self) -> None:
        """POSITIVE CONTROL: an ordinary empty mapping is untouched."""
        bound = self.bound_step_task()
        task = self.memory.load_task(bound)
        assert task is not None
        self.assertEqual(task.context, {})
        self.assertIs(type(task.context), dict)
        self.assertIsNone(
            self.runner_core(self.other_store())._local_read_context_failure(
                self.stored(), task
            )
        )
        run = self.runner_core(self.other_store()).resume(self.session.id, bound)
        self.assertIsNot(run.status, RunStatus.FAILED)
        self.assertEqual(self.reader.calls, [bound])


# --------------------------------------------------------------------------
# S3B P2. What a PREPARED attempt row MEANS, and why a lost one is inert.
# --------------------------------------------------------------------------


class PreparedAttemptSemanticsTests(ChainArbitrationTestCase):
    """PREPARED is a re-arbitrated claim slot, never execution authority.

    H2 defines PREPARED as "the connector boundary has NOT been crossed, safe
    to reclaim/retry after restart", and pre-M11 storage tests already pin
    that a LOST claim leaves the attempt PREPARED and claimable
    (``test_a_lost_claim_leaves_the_attempt_claimable``,
    ``test_restart_from_prepared_may_safely_proceed``). So the row surviving a
    lost arbitration is the ESTABLISHED contract, not a leak - deleting it
    would destroy those invariants and would need a second arbitration path to
    decide when deletion is safe.

    What actually has to hold is that the surviving row carries NO authority:
    ownership is granted only by the PREPARED -> IN_FLIGHT CAS inside
    ``begin_execution``, which re-runs the whole arbitration every time it is
    asked. These tests pin that, and pin that a cancelled chain can never
    return to ACTIVE, so the lost attempt can never be claimed again.
    """

    def prepared_rows(self) -> list[tuple[str, str]]:
        return [row for row in self.attempt_rows() if row[1] == "prepared"]

    def lost_between_open_and_begin(self) -> tuple[str, str]:
        """Case B: the cancel lands after open, before begin. Returns ids."""
        runner_store = self.other_store()
        core = self.runner_core(runner_store)
        done: list[str] = []
        ordinary = runner_store.open_execution_attempt
        opened: list[str] = []

        def cancel_then_open(attempt):
            if not done:
                done.append(self.cancel_from_another_store())
            result = ordinary(attempt)
            opened.append(result.id)
            return result

        with mock.patch.object(
            runner_store, "open_execution_attempt", cancel_then_open
        ):
            core.run_chain(self.session.id)
        self.assertEqual(done, ["cancelled"])
        self.assertEqual(len(opened), 1, "the runner never opened an attempt")
        return self.chain_now().step_task_ids[0], opened[0]

    # -- What the row is, and what it is not ----------------------------

    def test_prepared_is_the_replayable_state_by_contract(self) -> None:
        """The H2 state machine, not this file's opinion of it."""
        self.assertNotIn(AttemptState.PREPARED, ATTEMPT_NON_REPLAYABLE)
        for state in (
            AttemptState.IN_FLIGHT,
            AttemptState.UNKNOWN_OUTCOME,
            AttemptState.FINISHED,
        ):
            with self.subTest(state=state.value):
                self.assertIn(state, ATTEMPT_NON_REPLAYABLE)

    def test_a_lost_claim_leaves_exactly_one_inert_prepared_row(self) -> None:
        """CASE B. The row survives; nothing else does."""
        task_id, _attempt_id = self.lost_between_open_and_begin()
        self.assertEqual(self.prepared_rows(), [(task_id, "prepared")])
        self.assertNothingExecuted("a lost claim left more than a PREPARED row")

    def test_the_lost_prepared_attempt_never_acquires_authority(self) -> None:
        """Asked again and again, with the SAME attempt id: always LOST."""
        task_id, attempt_id = self.lost_between_open_and_begin()
        store = self.other_store()
        for _ in range(4):
            self.assertIs(
                store.begin_execution(
                    attempt_id, self.session.id, task_id, [], self.free_claim()
                ),
                ExecutionClaimOutcome.LOST,
            )
            latest = store.latest_execution_attempt(task_id)
            assert latest is not None
            self.assertIs(latest.state, AttemptState.PREPARED)
        # And through the ordinary paths, which is what actually runs.
        for _ in range(3):
            self.runner_core(self.other_store()).resume(self.session.id, task_id)
            self.runner_core(self.other_store()).run_chain(self.session.id)
        self.assertNothingExecuted("a lost PREPARED attempt acquired authority")

    def test_cancellation_is_terminal_so_the_claim_can_never_reopen(self) -> None:
        """The reason the refusal is permanent and not merely current."""
        task_id, _attempt_id = self.lost_between_open_and_begin()
        chain_state = self.chain_now()
        store = self.other_store()
        # No API returns a cancelled chain to ACTIVE, and the one slot stays
        # occupied, so no redeclaration can revive the step either.
        self.assertEqual(
            store.cancel_chain(self.session.id, chain_state.id), "terminal"
        )
        replacement = ChainState.declared(chain_steps_from_declaration(declaration(2)))
        assert replacement is not None
        self.assertEqual(
            store.declare_chain_if_absent(self.session.id, replacement), "occupied"
        )
        self.assertIs(self.chain_now().status, ChainStatus.CANCELLED)
        self.assertEqual(
            store.materialize_chain_step(
                self.session.id,
                chain_state.id,
                chain_state.digest,
                0,
                Task(
                    objective="x",
                    capability=LOCAL_READ_CAPABILITY,
                    scope=ActionScope(action="read", target="notes.txt"),
                ),
            ),
            ("inactive", ""),
        )
        self.assertEqual(
            store.advance_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0
            ),
            "inactive",
        )
        self.assertNothingExecuted()
        self.assertEqual(self.prepared_rows(), [(task_id, "prepared")])

    def test_a_prepared_row_reserves_nothing_and_consumes_nothing(self) -> None:
        """CASE A and B: no money, no grant, either side of the open."""
        # A. cancel before the runner ever opens an attempt.
        self.assertEqual(self.cancel_from_another_store(), "cancelled")
        _run, verdict = self.runner_core(self.other_store()).run_chain(
            self.session.id
        )
        self.assertEqual(verdict, ChainStatus.CANCELLED.value)
        self.assertEqual(self.attempt_rows(), [], "cancel-before-open opened one")
        self.assertNothingExecuted("cancel before open")

    def test_a_prepared_row_blocks_no_legitimate_chain_progress(self) -> None:
        """It is not an in-doubt outcome, so it stops nothing.

        A fresh chain, a step completed the ordinary way, and a PREPARED row
        sitting on it: the chain still advances. Only IN_FLIGHT and
        UNKNOWN_OUTCOME block, which is the pre-existing H2 rule.
        """
        self.session = SessionState(objective="prepared blocks nothing")
        self.memory.save_session(self.session)
        self.assertEqual(self.declare(declaration_json(2)).exit_code, EXIT_OK)
        chain_state = self.chain_now()
        core = self.runner_core(self.other_store())
        task, reason = core.build_chain_step_task(self.stored(), chain_state, 0)
        self.assertIsNone(reason)
        assert task is not None
        outcome, bound = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.assertEqual(outcome, "created")
        self.opened_attempt(bound, "c3" * 32)
        self.assertEqual(self.prepared_rows(), [(bound, "prepared")])
        stored_task = self.memory.load_task(bound)
        assert stored_task is not None
        stored_task.status = TaskStatus.COMPLETED
        stored_task.stage = LoopStage.COMPLETED
        stored_task.selected_connector = "reader"
        self.memory.save_task(self.session.id, stored_task)
        self.assertEqual(
            self.memory.advance_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0
            ),
            "advanced",
        )

    def test_an_in_doubt_attempt_is_still_never_replayed(self) -> None:
        """The states that DO block are untouched by any of this."""
        chain_state = self.chain_now()
        core = self.runner_core(self.other_store())
        task, _reason = core.build_chain_step_task(self.stored(), chain_state, 0)
        assert task is not None
        _outcome, bound = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, task
        )
        self.opened_attempt(bound, "d4" * 32)
        # COMPLETED first, so "blocked" can only ever be the ATTEMPT talking:
        # an unfinished task is blocked for a different reason entirely, and
        # that would make the assertion below say nothing.
        stored_task = self.memory.load_task(bound)
        assert stored_task is not None
        stored_task.status = TaskStatus.COMPLETED
        stored_task.stage = LoopStage.COMPLETED
        stored_task.selected_connector = "reader"
        self.memory.save_task(self.session.id, stored_task)
        for state in (AttemptState.IN_FLIGHT, AttemptState.UNKNOWN_OUTCOME):
            with self.subTest(state=state.value):
                with contextlib.closing(
                    sqlite3.connect(self.database)
                ) as connection:
                    connection.execute(
                        "UPDATE execution_attempts SET state = ? WHERE task_id = ?",
                        (state.value, bound),
                    )
                    connection.commit()
                latest = self.memory.latest_execution_attempt(bound)
                assert latest is not None
                self.assertIs(latest.state, state)
                # A resume here is exactly what a replay would look like.
                self.runner_core(self.other_store()).resume(self.session.id, bound)
                self.assertEqual(self.connector_calls(), [], state.value)
                self.assertEqual(
                    self.memory.advance_chain_step(
                        self.session.id, chain_state.id, chain_state.digest, 0
                    ),
                    "blocked",
                )
        # CONTROL: the same completed step, with the attempt back in the
        # replayable state, advances. So the refusals above were the attempt.
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE execution_attempts SET state = ? WHERE task_id = ?",
                (AttemptState.PREPARED.value, bound),
            )
            connection.commit()
        self.assertEqual(
            self.memory.advance_chain_step(
                self.session.id, chain_state.id, chain_state.digest, 0
            ),
            "advanced",
        )


# --------------------------------------------------------------------------
# S3B P1-2. Target canonicalization is authority the ORDINARY runtime grants,
# and only where it grants it.
# --------------------------------------------------------------------------


class TargetCanonicalizationAuthorityTests(MaterializationTestCase):
    """STORED may accept the runtime's ONE rewrite. Nothing else, nowhere else."""

    def stored_refusal(self, task: Task, index: int) -> str | None:
        return chain_task_refusal(
            task, self.chain_now(), index, mode=ChainTaskMode.STORED
        )

    def retargeted(self, index: int, target: str) -> Task:
        task = self.derive(index)
        task.scope = replace(task.scope, target=target)
        return task

    def reach_the_write_step(self) -> None:
        """Settle steps 0 and 1 the ordinary way so step 2 can be derived."""
        _verdict, read_id = self.materialize(0)
        self.complete_task(read_id, selected_connector="local")
        self.assertEqual(self.advance(0), "advanced")
        _verdict, model_id = self.materialize(1)
        self.finish_model_step(model_id)
        self.assertEqual(self.advance(1), "advanced")

    def test_the_predicate_is_the_one_the_runtime_uses(self) -> None:
        """One constant, named by both sides. A second list would drift."""
        self.assertEqual(
            CANONICAL_TARGET_CAPABILITIES,
            (LOCAL_READ_CAPABILITY, LOCAL_WRITE_CAPABILITY),
        )
        runtime = inspect.getsource(AgentCore._bind_canonical_local_target)
        validator = inspect.getsource(chain_task_refusal)
        for label, source in (("runtime", runtime), ("validator", validator)):
            with self.subTest(source=label):
                self.assertIn("CANONICAL_TARGET_CAPABILITIES", source)
        # Neither side re-spells the capability set by hand.
        self.assertNotIn(
            f'"{LOCAL_READ_CAPABILITY}", "{LOCAL_WRITE_CAPABILITY}"', runtime
        )

    def test_a_local_read_keeps_its_trusted_canonical_target(self) -> None:
        declared = self.chain_now().steps[0].target
        self.assertEqual(declared, "notes.txt")
        self.assertIsNone(
            self.stored_refusal(self.retargeted(0, canonical_path(declared)), 0)
        )
        self.assertIsNone(self.stored_refusal(self.derive(0), 0))

    def test_a_local_write_keeps_its_trusted_canonical_target(self) -> None:
        self.reach_the_write_step()
        declared = self.chain_now().steps[2].target
        self.assertEqual(declared, "summary.md")
        self.assertIsNone(
            self.stored_refusal(self.retargeted(2, canonical_path(declared)), 2)
        )

    def test_the_runtime_really_produces_that_exact_value(self) -> None:
        """The accepted spelling is the one the ordinary loop writes, not a
        second guess at what canonicalization means."""
        task = self.derive(0)
        self.assertIsNone(
            self.core._bind_canonical_local_target(self.stored(), task)
        )
        self.assertEqual(
            task.scope.target, canonical_path(self.chain_now().steps[0].target)
        )
        self.assertIsNone(self.stored_refusal(task, 0))

    def test_a_non_local_capability_may_not_canonicalize_its_target(self) -> None:
        """The P1: a model step's symbolic target is never rewritten.

        A ONE-STEP chain, so the ONLY thing separating the accepted task from
        the refused one is the target spelling. Nothing here consumes a
        predecessor, names a write mode or routes a reference, so a refusal
        cannot come from anywhere else.
        """
        symbolic = "alpha"
        self.session = SessionState(objective="symbolic target")
        self.memory.save_session(self.session)
        self.assertEqual(
            self.declare(
                json.dumps(
                    {
                        "steps": [
                            {
                                "objective": "Plan against the symbol.",
                                "capability": "code_planning",
                                "action": "plan",
                                "target": symbolic,
                                "destination": "anthropic/claude-code",
                                "consumes": CHAIN_NO_PREDECESSOR,
                            }
                        ]
                    }
                )
            ).exit_code,
            EXIT_OK,
        )
        chain_state = self.chain_now()
        self.assertNotIn(chain_state.steps[0].capability, CANONICAL_TARGET_CAPABILITIES)
        self.assertEqual(chain_state.steps[0].target, symbolic)
        declared_task = self.derive(0)
        # The ordinary runtime leaves this target alone, so no legitimate
        # stored task can ever carry the canonical spelling.
        self.assertIsNone(
            self.core._bind_canonical_local_target(self.stored(), declared_task)
        )
        self.assertEqual(declared_task.scope.target, symbolic)
        # NON-VACUOUS: the declared spelling holds authority in both modes.
        for mode in ChainTaskMode:
            with self.subTest(accepted=mode.value):
                self.assertIsNone(
                    chain_task_refusal(declared_task, chain_state, 0, mode=mode)
                )
        widened = self.retargeted(0, canonical_path(symbolic))
        self.assertNotEqual(widened.scope.target, symbolic)
        for mode in ChainTaskMode:
            with self.subTest(refused=mode.value):
                self.assertIsNotNone(
                    chain_task_refusal(widened, chain_state, 0, mode=mode),
                    "a non-local capability kept authority under a rewrite",
                )
        # Nothing approves, attempts or executes on the back of it.
        verdict, task_id = self.memory.materialize_chain_step(
            self.session.id, chain_state.id, chain_state.digest, 0, widened
        )
        self.assertEqual(verdict, "conflict")
        self.assertEqual(task_id, "")
        self.assertEqual(self.chain_now().step_task_ids, ("",))
        self.assertNoSideEffects()

    def test_a_proposal_always_carries_the_declaration_spelling(self) -> None:
        self.assertIsNotNone(
            chain_task_refusal(
                self.retargeted(0, canonical_path(self.chain_now().steps[0].target)),
                self.chain_now(),
                0,
                mode=ChainTaskMode.PROPOSAL,
            ),
            "a PROPOSAL was accepted with a canonicalized target",
        )
        self.assertNoSideEffects()
        self.reach_the_write_step()
        for index in (2,):
            with self.subTest(index=index):
                declared = self.chain_now().steps[index].target
                canonical = self.retargeted(index, canonical_path(declared))
                self.assertNotEqual(canonical.scope.target, declared)
                self.assertIsNotNone(
                    chain_task_refusal(
                        canonical,
                        self.chain_now(),
                        index,
                        mode=ChainTaskMode.PROPOSAL,
                    ),
                    "a PROPOSAL was accepted with a canonicalized target",
                )
                verdict, task_id = self.memory.materialize_chain_step(
                    self.session.id,
                    self.chain_now().id,
                    self.chain_now().digest,
                    index,
                    canonical,
                )
                self.assertEqual(verdict, "conflict")
                self.assertEqual(task_id, "")
        # Step 2 was never bound, whatever the earlier steps left behind.
        self.assertEqual(self.chain_now().step_task_ids[2], "")

    def test_every_unsanctioned_target_spelling_is_refused(self) -> None:
        """Only the EXACT sanctioned transformation, never a near miss.

        Platform-honest by construction: a candidate is asserted refused only
        when it really is a different string from the one the runtime writes.
        A spelling ``canonical_path`` collapses onto the sanctioned value IS
        the sanctioned value, and refusing it would be a claim about path
        semantics this test does not get to invent.
        """
        declared = self.chain_now().steps[0].target
        sanctioned = canonical_path(declared)
        candidates = {
            "arbitrary path": canonical_path("/etc/passwd"),
            "different filename": canonical_path("notes-other.txt"),
            "different directory": canonical_path("sub/notes.txt"),
            "outside the root": canonical_path("../notes.txt"),
            "parent escape verbatim": "../notes.txt",
            "case only": sanctioned.upper(),
            "duplicate separator": sanctioned.replace("/", "//", 1),
            "trailing separator": sanctioned + "/",
            "dot segment": sanctioned.replace("/", "/./", 1),
            "neighbouring name": sanctioned + ".bak",
            "prefix": sanctioned + "x",
        }
        checked = 0
        for label, target in candidates.items():
            if target in (declared, sanctioned):
                # Platform semantics collapse this onto the sanctioned value.
                continue
            with self.subTest(label=label):
                checked += 1
                self.assertIsNotNone(
                    self.stored_refusal(self.retargeted(0, target), 0),
                    f"{label} was accepted as a chain target",
                )
        self.assertGreaterEqual(checked, 6)
        self.assertNoSideEffects()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
