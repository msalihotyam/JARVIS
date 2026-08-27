"""M7 - Explicit Model Answer Handoff.

What this file pins, end to end:

    completed model task -> write.model_answer_task_id -> durable write-source
    identity -> CONTEXT_RETRIEVAL resolution -> frozen write_content ->
    UNCHANGED M5 attestation / approval / write -> digest continuity

plus everything that must NOT happen: a handoff that degrades into a direct
write when its persisted identity is corrupted, a source that is re-read after
the freeze, model text that chooses a path or a mode, a Claude call anywhere on
the write path, and any retrieval, decision or project-memory side channel.

The source connector is the M6 ``AnsweringConnector`` (a provider-neutral
EXTERNAL_EGRESS producer); the write connector is the real M5
``LocalFileWriteConnector`` on a temp root. Nothing in the M5 native write
implementation, the frame, H2 or H3 is touched or relaxed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
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
from jarvis.connectors.claude_code import EGRESS_TOOL_GRANTS, ClaudeCodeConnector
from jarvis.connectors.local_files import LocalFileWriteConnector
from jarvis.contracts import (
    LOCAL_WRITE_CAPABILITY,
    LOCAL_WRITE_ENABLED_MODES,
    LOCAL_WRITE_SOURCE_KEY,
    LOCAL_WRITE_SOURCE_MAX_CHARS,
    RETRIEVAL_MAX_CHARACTERS,
    RETRIEVAL_MAX_ITEMS,
    AttemptState,
    DataClassification,
    ErrorCode,
    LoopStage,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    ProjectRecord,
    RetrievedContext,
    RunStatus,
    SessionState,
    SideEffect,
    Task,
    TaskStatus,
    ToolResult,
    WriteSource,
    build_model_answer,
    canonical_path,
    handoff_eligible,
    local_write_identity_refusal,
    local_write_source_refusal,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import Settings
from tests.helpers import DummyConnector, make_task, safe_cleanup
from tests.test_model_answer import AnsweringConnector

#: A realistic deliverable: indentation, a blank line, non-ASCII text and a
#: trailing newline - every one of which must reach the disk byte for byte.
PROPOSED_FILE = (
    "def test_greeting():\n"
    '    assert greet() == "merhaba dünya"\n'
    "\n"
    "    # unicode: é 😀\n"
)
TARGET_NAME = "test_proposed.py"
DEFAULT_WRITE_REASON = "Connector 'local-file-write' will execute the scoped action."
#: Well-formed by the reference grammar, never a real task.
OTHER_ID = "1" * 36


class _Crash(BaseException):
    """Not an ``Exception``: no product handler may swallow it."""


class RaisingWriter(LocalFileWriteConnector):
    """A write connector that raises before touching the disk (H2 in doubt)."""

    manifest = replace(LocalFileWriteConnector.manifest, name="raising-writer")

    def execute(self, task: Task, session: SessionState) -> ToolResult:  # type: ignore[override]
        raise RuntimeError("the write blew up mid-flight")


class _Foreign(str, Enum):
    """A foreign ``str`` enum that compares equal to a real classification."""

    INTERNAL = "internal"


@dataclass(frozen=True)
class _LookAlike:
    """Every field of a ``ModelAnswer``, none of its identity."""

    content: str
    character_count: int
    sha256: str
    truncated: bool
    produced_at: str

    @property
    def present(self) -> bool:
        return True


def _set(key: str, value: Any) -> Callable[[dict], None]:
    def mutate(document: dict) -> None:
        document[key] = value

    return mutate


def _drop(key: str) -> Callable[[dict], None]:
    def mutate(document: dict) -> None:
        document.pop(key, None)

    return mutate


#: Single-field corruptions of a persisted handoff row. Each one must load as
#: the typed INVALID verdict and fail closed; none may load as DIRECT.
CORRUPTIONS: tuple[tuple[str, Callable[[dict], None]], ...] = (
    ("reference key deleted", _drop("write_source_task_id")),
    ("blank reference", _set("write_source_task_id", "")),
    ("non-string reference", _set("write_source_task_id", 5)),
    ("oversized reference", _set("write_source_task_id", "x" * 65)),
    ("padded reference", _set("write_source_task_id", " " + OTHER_ID + " ")),
    ("kind key deleted", _drop("write_source")),
    ("kind garbage", _set("write_source", "bogus")),
    ("kind non-string", _set("write_source", 7)),
    ("kind null", _set("write_source", None)),
    ("kind flipped to direct", _set("write_source", "direct")),
)


class HandoffTestCase(unittest.TestCase):
    """One temp root, one answering egress connector, one real write connector."""

    permission_mode = PermissionMode.SESSION

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self._temp)
        self.root = Path(self._temp.name).resolve()
        self.database = self.root / "memory.db"
        self.settings = Settings(
            memory_database=self.database, permission_mode=self.permission_mode
        )
        self.answering = AnsweringConnector(answer_text=PROPOSED_FILE)
        # A second "echo" connector with NO external effect and a lower
        # priority: only ever selected through ``preferred_connector``, so a
        # completed non-egress task can be forged into a fake producer.
        self.plain = DummyConnector(name="plain-echo", priority=1)
        self.writer = LocalFileWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.app = create_app(
            settings=self.settings,
            connectors=(self.answering, self.plain, self.writer),
        )
        self.core = self.app.core
        self.memory = self.app.memory

    # -- sources ------------------------------------------------------------

    def source_task(
        self, classification: DataClassification = DataClassification.INTERNAL
    ) -> Task:
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS, classification=classification)
        task.objective = "propose a test module"
        return task

    def completed_source(
        self,
        text: str | None = None,
        *,
        classification: DataClassification = DataClassification.INTERNAL,
    ) -> tuple[Task, SessionState]:
        """One approved external-model run; returns (stored task, session)."""
        if text is not None:
            self.answering.answer_text = text
        task = self.source_task(classification)
        run = self.core.run(task)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
        run = self.core.resume(
            run.session.id,
            task.id,
            grant=PermissionGrant.for_request(run.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        stored = self.memory.load_task(task.id)
        self.assertTrue(stored.model_answer.present)
        return stored, run.session

    # -- targets ------------------------------------------------------------

    def handoff_document(
        self,
        source_id: str,
        *,
        name: str = TARGET_NAME,
        classification: str = "internal",
        mode: str = "create",
        capability: str = LOCAL_WRITE_CAPABILITY,
        **write_extra: Any,
    ) -> dict[str, Any]:
        write: dict[str, Any] = {"mode": mode, LOCAL_WRITE_SOURCE_KEY: source_id}
        write.update(write_extra)
        return {
            "objective": "create the proposed test module",
            "capability": capability,
            "scope": {
                "action": "write",
                "target": str(self.root / name),
                "data_classification": classification,
            },
            "write": write,
        }

    def handoff_task(self, source_id: str, **kwargs: Any) -> Task:
        return decode_task_json(json.dumps(self.handoff_document(source_id, **kwargs)))

    def direct_task(self, *, name: str = "direct.txt", content: str = "direct\n") -> Task:
        return decode_task_json(
            json.dumps(
                {
                    "objective": "write a file",
                    "capability": LOCAL_WRITE_CAPABILITY,
                    "scope": {"action": "write", "target": str(self.root / name)},
                    "write": {"mode": "create", "content": content},
                }
            )
        )

    def request(self, task: Task, session_id: str | None):
        run = self.core.run(task, session_id=session_id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
        return run

    def approve(self, task: Task, run):
        return self.core.resume(
            run.session.id, task.id, grant=PermissionGrant.for_request(run.permission_request)
        )

    def completed_handoff(self, source: Task, session: SessionState, **kwargs: Any):
        task = self.handoff_task(source.id, **kwargs)
        run = self.approve(task, self.request(task, session.id))
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        return self.memory.load_task(task.id), run

    def refused(self, task: Task, session_id: str | None, detail: str):
        run = self.core.run(task, session_id=session_id)
        self.assert_refused_before_authority(task, run, detail)
        return run

    def assert_refused_before_authority(self, task: Task, run, detail: str) -> None:
        """Terminal INVALID_TASK, nothing copied, nothing selected, nothing asked."""
        self.assertEqual(RunStatus.FAILED, run.status, msg=str(run.error))
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn(detail, run.error.message)
        stored = self.memory.load_task(task.id)
        self.assertIs(TaskStatus.FAILED, stored.status)
        self.assertEqual("", stored.write_content)
        self.assertIsNone(stored.selected_connector)
        self.assertIsNone(self.memory.load_pending_approval(task.id, run.session.id))
        self.assertEqual([], self.memory.execution_attempts(task.id))
        kinds = self.event_types(run.session.id, task.id)
        self.assertNotIn("plan_created", kinds)
        self.assertNotIn("approval_requested", kinds)
        self.assertFalse((self.root / TARGET_NAME).exists())

    def event_types(self, session_id: str, task_id: str) -> list[str]:
        return [
            event["event_type"]
            for event in self.memory.list_events(session_id)
            if event["task_id"] == task_id
        ]

    # -- storage ------------------------------------------------------------

    def stored(self, task_id: str) -> Task:
        """Reload through a FRESH store, so nothing in memory can help."""
        return SQLiteMemoryStore(self.database).load_task(task_id)

    def raw_state(self, task_id: str) -> dict[str, Any]:
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        finally:
            connection.close()
        return json.loads(row[0])

    def tamper(self, task_id: str, mutate: Callable[[dict], None]) -> None:
        """Corrupt a persisted row the way an out-of-product edit would.

        Raw SQLite on purpose: the product's own writers would re-apply the
        very validation these tests exist to exercise.
        """
        document = self.raw_state(task_id)
        mutate(document)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task_id),
            )
            connection.commit()
        finally:
            connection.close()

    def grant_consumed(self, fingerprint: str) -> bool:
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT consumed_by_attempt FROM permission_grants WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        finally:
            connection.close()
        return row is not None and row[0] is not None

    def fresh_app(self, connectors=None):
        """A brand-new process: new store, new registry, the same database."""
        if connectors is None:
            connectors = (
                AnsweringConnector(answer_text=self.answering.answer_text),
                DummyConnector(name="plain-echo", priority=1),
                LocalFileWriteConnector(
                    allowed_roots=(self.root,), default_working_directory=self.root
                ),
            )
        return create_app(settings=self.settings, connectors=connectors)

    # -- lifecycle fixtures -------------------------------------------------

    def frozen_before_resolution(self, source: Task, session: SessionState) -> Task:
        """A handoff row persisted at CONTEXT_RETRIEVAL with nothing copied.

        Exactly the state a crash at the ``context_retrieved`` checkpoint
        leaves behind: the reference is durable, the bytes are not.
        """
        task = self.handoff_task(source.id)
        real = SQLiteMemoryStore.persist_checkpoint

        def crash(store, session_, task_, event_type, payload=None, pending_request=None):
            if event_type == "context_retrieved":
                raise _Crash()
            return real(store, session_, task_, event_type, payload, pending_request)

        with mock.patch.object(
            SQLiteMemoryStore, "persist_checkpoint", crash
        ), self.assertRaises(_Crash):
            self.core.run(task, session_id=session.id)
        row = self.stored(task.id)
        self.assertIs(LoopStage.CONTEXT_RETRIEVAL, row.stage)
        self.assertEqual("", row.write_content)
        self.assertIs(WriteSource.MODEL_ANSWER, row.write_source)
        self.assertEqual(source.id, row.write_source_task_id)
        return task

    def prepared_attempt(self, source: Task, session: SessionState):
        """A handoff approved and PREPARED, crashed before the claim.

        The H2 state a restart finds: the grant is recorded and unconsumed,
        the pre-execution record is durable, the attempt is PREPARED, and the
        connector boundary was never crossed.
        """
        task = self.handoff_task(source.id)
        run = self.request(task, session.id)
        grant = PermissionGrant.for_request(run.permission_request)

        def crash(store, *args, **kwargs):
            raise _Crash()

        with mock.patch.object(
            SQLiteMemoryStore, "begin_execution", crash
        ), self.assertRaises(_Crash):
            self.core.resume(run.session.id, task.id, grant=grant)
        attempts = self.memory.execution_attempts(task.id)
        self.assertEqual(1, len(attempts))
        self.assertIs(AttemptState.PREPARED, attempts[0].state)
        self.assertFalse(self.grant_consumed(grant.fingerprint))
        self.assertFalse((self.root / TARGET_NAME).exists())
        return task, run, grant


# --------------------------------------------------------------------------
# 1. Input contract and the two canonical validators
# --------------------------------------------------------------------------


class InputContractTests(unittest.TestCase):
    def decode(self, write: dict[str, Any] | None, capability: str = LOCAL_WRITE_CAPABILITY, **top):
        document: dict[str, Any] = {
            "objective": "write",
            "capability": capability,
            "scope": {"action": "write", "target": "notes.txt"},
        }
        if write is not None:
            document["write"] = write
        document.update(top)
        return decode_task_json(json.dumps(document))

    def test_direct_content_derives_the_direct_kind(self) -> None:
        task = self.decode({"mode": "create", "content": "x\n"})
        self.assertIs(WriteSource.DIRECT, task.write_source)
        self.assertEqual("", task.write_source_task_id)
        self.assertEqual("x\n", task.write_content)

    def test_a_reference_derives_the_handoff_kind(self) -> None:
        task = self.decode({"mode": "create", LOCAL_WRITE_SOURCE_KEY: OTHER_ID})
        self.assertIs(WriteSource.MODEL_ANSWER, task.write_source)
        self.assertEqual(OTHER_ID, task.write_source_task_id)
        self.assertEqual("", task.write_content)
        self.assertEqual("create", task.write_mode)

    def test_no_write_section_is_a_direct_write_of_nothing(self) -> None:
        task = self.decode(None)
        self.assertIs(WriteSource.DIRECT, task.write_source)
        self.assertEqual("", task.write_source_task_id)

    def test_both_sources_are_rejected(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode({"mode": "create", "content": "x", LOCAL_WRITE_SOURCE_KEY: OTHER_ID})
        self.assertIn("exactly one", str(caught.exception))

    def test_neither_source_is_rejected(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode({"mode": "create"})
        self.assertIn("exactly one", str(caught.exception))

    def test_the_kind_can_never_be_supplied(self) -> None:
        with self.assertRaises(TaskInputError):
            self.decode({"mode": "create", "content": "x"}, write_source="model_answer")
        with self.assertRaises(TaskInputError):
            self.decode({"mode": "create", "content": "x", "write_source": "model_answer"})
        with self.assertRaises(TaskInputError):
            self.decode({"mode": "create", "content": "x"}, write_source_task_id=OTHER_ID)

    def test_a_non_string_reference_is_refused(self) -> None:
        for value in (5, 1.5, True, None, ["a"], {"a": 1}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TaskInputError) as caught:
                    self.decode({"mode": "create", LOCAL_WRITE_SOURCE_KEY: value})
                self.assertIn(LOCAL_WRITE_SOURCE_KEY, str(caught.exception))

    def test_a_blank_reference_is_refused(self) -> None:
        with self.assertRaises(TaskInputError):
            self.decode({"mode": "create", LOCAL_WRITE_SOURCE_KEY: ""})

    def test_a_padded_reference_is_refused_not_canonicalized(self) -> None:
        for value in (" " + OTHER_ID, OTHER_ID + " ", OTHER_ID + "\n", "\t" + OTHER_ID):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TaskInputError) as caught:
                    self.decode({"mode": "create", LOCAL_WRITE_SOURCE_KEY: value})
                self.assertIn("whitespace", str(caught.exception))

    def test_the_reference_length_boundary_is_exact(self) -> None:
        task = self.decode(
            {"mode": "create", LOCAL_WRITE_SOURCE_KEY: "x" * LOCAL_WRITE_SOURCE_MAX_CHARS}
        )
        self.assertEqual(LOCAL_WRITE_SOURCE_MAX_CHARS, len(task.write_source_task_id))
        with self.assertRaises(TaskInputError):
            self.decode(
                {"mode": "create", LOCAL_WRITE_SOURCE_KEY: "x" * (LOCAL_WRITE_SOURCE_MAX_CHARS + 1)}
            )

    def test_a_nul_reference_is_refused(self) -> None:
        with self.assertRaises(TaskInputError):
            self.decode({"mode": "create", LOCAL_WRITE_SOURCE_KEY: "ab\x00cd"})

    def test_an_unknown_write_key_is_rejected(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode({"mode": "create", LOCAL_WRITE_SOURCE_KEY: OTHER_ID, "source": "x"})
        self.assertIn("source", str(caught.exception))

    def test_overwrite_plus_reference_is_refused_at_the_input_boundary(self) -> None:
        """M10 defers ModelAnswer -> overwrite, so the pair never becomes a task.

        Overwrite itself is enabled policy now; what is refused is generating
        the replacement bytes from a model answer, because nothing durable
        proves the exact read context reached the actual model invocation.
        """
        with self.assertRaises(TaskInputError) as caught:
            self.decode({"mode": "overwrite", LOCAL_WRITE_SOURCE_KEY: OTHER_ID})
        self.assertIn("write.mode", str(caught.exception))
        self.assertIn("may not replace", str(caught.exception))
        self.assertIn("overwrite", LOCAL_WRITE_ENABLED_MODES)

    def test_the_reference_is_only_valid_for_file_write(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode({"mode": "create", LOCAL_WRITE_SOURCE_KEY: OTHER_ID}, capability="code_planning")
        self.assertIn(LOCAL_WRITE_CAPABILITY, str(caught.exception))

    def test_the_reference_never_lives_under_context(self) -> None:
        # ``context`` is serialized verbatim into the Claude payload; the
        # reference is write-input authority and is refused there outright.
        with self.assertRaises(TaskInputError) as caught:
            self.decode(
                {"mode": "create", "content": "x"}, context={LOCAL_WRITE_SOURCE_KEY: OTHER_ID}
            )
        self.assertIn(LOCAL_WRITE_SOURCE_KEY, str(caught.exception))

    def test_the_reference_grammar_is_one_function(self) -> None:
        self.assertIsNone(local_write_source_refusal(OTHER_ID))
        self.assertIsNone(local_write_source_refusal("x" * LOCAL_WRITE_SOURCE_MAX_CHARS))
        for value, fragment in (
            (5, "not a string"),
            (None, "not a string"),
            ("", "empty"),
            ("a\x00b", "NUL"),
            (" x", "whitespace"),
            ("x ", "whitespace"),
            ("x" * (LOCAL_WRITE_SOURCE_MAX_CHARS + 1), "ceiling"),
        ):
            with self.subTest(value=repr(value)):
                reason = local_write_source_refusal(value)
                self.assertIsNotNone(reason)
                self.assertIn(fragment, reason)

    def test_the_identity_pair_contract(self) -> None:
        self.assertIsNone(local_write_identity_refusal(WriteSource.DIRECT, ""))
        self.assertIsNone(local_write_identity_refusal(WriteSource.MODEL_ANSWER, OTHER_ID))
        self.assertIn("exact WriteSource", local_write_identity_refusal("direct", ""))
        self.assertIn("exact WriteSource", local_write_identity_refusal(None, ""))
        self.assertIn("invalid", local_write_identity_refusal(WriteSource.INVALID, ""))
        self.assertIn("invalid", local_write_identity_refusal(WriteSource.INVALID, OTHER_ID))
        self.assertIn(
            "direct write carries", local_write_identity_refusal(WriteSource.DIRECT, OTHER_ID)
        )
        self.assertIn("not a string", local_write_identity_refusal(WriteSource.DIRECT, 5))
        self.assertIn(
            "malformed", local_write_identity_refusal(WriteSource.MODEL_ANSWER, "")
        )
        self.assertIn(
            "malformed", local_write_identity_refusal(WriteSource.MODEL_ANSWER, " " + OTHER_ID)
        )
        self.assertIn(
            "not a string", local_write_identity_refusal(WriteSource.MODEL_ANSWER, None)
        )

    def test_the_handoff_ladder_matrix_is_exact(self) -> None:
        order = (
            DataClassification.PUBLIC,
            DataClassification.INTERNAL,
            DataClassification.SENSITIVE,
            DataClassification.SECRET,
        )
        for source in order:
            for target in order:
                with self.subTest(source=source.value, target=target.value):
                    expected = (
                        source is not DataClassification.SECRET
                        and order.index(source) <= order.index(target)
                    )
                    self.assertEqual(expected, handoff_eligible(source, target))
        # Exact types only: a raw string and a foreign str enum compare equal
        # to a member and are refused precisely because of that.
        self.assertFalse(handoff_eligible("public", DataClassification.INTERNAL))
        self.assertFalse(handoff_eligible(DataClassification.PUBLIC, "internal"))
        self.assertFalse(handoff_eligible(_Foreign.INTERNAL, DataClassification.INTERNAL))
        self.assertFalse(handoff_eligible(DataClassification.INTERNAL, _Foreign.INTERNAL))
        self.assertFalse(handoff_eligible(None, DataClassification.SECRET))


# --------------------------------------------------------------------------
# 2. Durable identity: persisted corruption never becomes a direct write
# --------------------------------------------------------------------------


class PersistedIdentityTests(HandoffTestCase):
    def test_a_handoff_kind_survives_save_and_load(self) -> None:
        source, session = self.completed_source()
        task = self.frozen_before_resolution(source, session)
        raw = self.raw_state(task.id)
        self.assertEqual("model_answer", raw["write_source"])
        self.assertEqual(source.id, raw["write_source_task_id"])
        loaded = self.stored(task.id)
        self.assertIs(WriteSource.MODEL_ANSWER, loaded.write_source)
        self.assertEqual(source.id, loaded.write_source_task_id)

    def test_every_corruption_of_a_pre_resolution_handoff_loads_invalid(self) -> None:
        source, session = self.completed_source()
        for label, mutate in CORRUPTIONS:
            with self.subTest(label=label):
                task = self.frozen_before_resolution(source, session)
                self.tamper(task.id, mutate)
                loaded = self.stored(task.id)
                self.assertIs(WriteSource.INVALID, loaded.write_source)
                self.assertIsNot(WriteSource.DIRECT, loaded.write_source)
                run = self.core.resume(session.id, task.id)
                self.assert_refused_before_authority(
                    task, run, "Write source identity is not usable"
                )

    def test_every_corruption_of_a_frozen_handoff_fails_closed_at_the_gate(self) -> None:
        source, session = self.completed_source()
        for label, mutate in CORRUPTIONS:
            with self.subTest(label=label):
                task = self.handoff_task(source.id)
                run = self.request(task, session.id)
                requested_before = self.event_types(session.id, task.id).count(
                    "approval_requested"
                )
                self.tamper(task.id, mutate)
                self.assertIs(WriteSource.INVALID, self.stored(task.id).write_source)
                again = self.core.resume(session.id, task.id)
                self.assertEqual(RunStatus.FAILED, again.status, msg=str(again.error))
                self.assertIs(ErrorCode.INVALID_TASK, again.error.code)
                self.assertIn("Write source identity is not usable", again.error.message)
                self.assertIsNone(again.permission_request)
                self.assertEqual(
                    requested_before,
                    self.event_types(session.id, task.id).count("approval_requested"),
                    "a corrupted handoff was offered a fresh approval",
                )
                self.assertEqual([], self.memory.execution_attempts(task.id))
                self.assertFalse((self.root / TARGET_NAME).exists())
                self.assertIs(TaskStatus.FAILED, self.stored(task.id).status)
                # Terminal erasure of the frozen bytes still happens (M5).
                self.assertEqual("", self.stored(task.id).write_content)
                # And the grant that was never issued can never be found.
                self.assertFalse(self.grant_consumed(run.permission_request.fingerprint))

    def test_no_malformed_handoff_can_become_a_zero_byte_direct_write(self) -> None:
        """THE regression the design correction exists for.

        A handoff whose reference disappeared must not fall through to M5 as
        a direct write of nothing: with the durable kind gone, the empty
        ``write_content`` would be a legal 0-byte create and the user would
        be shown a perfectly ordinary "create 0 bytes" approval.
        """
        source, session = self.completed_source()
        for label, mutate in (
            ("reference erased", _set("write_source_task_id", "")),
            ("reference key deleted", _drop("write_source_task_id")),
            ("kind key deleted", _drop("write_source")),
            ("kind flipped to direct", _set("write_source", "direct")),
        ):
            with self.subTest(label=label):
                task = self.frozen_before_resolution(source, session)
                self.tamper(task.id, mutate)
                run = self.core.resume(session.id, task.id)
                self.assertIsNot(RunStatus.AWAITING_APPROVAL, run.status)
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertIsNone(run.permission_request)
                self.assertIsNone(self.memory.load_pending_approval(task.id, session.id))
                self.assertNotIn("approval_requested", self.event_types(session.id, task.id))
                self.assertFalse((self.root / TARGET_NAME).exists())
                self.assertIs(WriteSource.INVALID, self.stored(task.id).write_source)

    def test_a_direct_task_with_a_forged_reference_is_refused_and_never_resolved(
        self,
    ) -> None:
        source, session = self.completed_source()
        task = self.direct_task()
        run = self.request(task, session.id)
        self.tamper(task.id, _set("write_source_task_id", source.id))
        self.assertIs(WriteSource.INVALID, self.stored(task.id).write_source)
        loads: list[str] = []
        real = SQLiteMemoryStore.load_task

        def watched(store, task_id):
            loads.append(task_id)
            return real(store, task_id)

        with mock.patch.object(SQLiteMemoryStore, "load_task", watched):
            again = self.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.FAILED, again.status)
        self.assertIs(ErrorCode.INVALID_TASK, again.error.code)
        self.assertNotIn(source.id, loads, "a direct write consumed a forged reference")
        self.assertFalse((self.root / "direct.txt").exists())
        self.assertFalse(self.grant_consumed(run.permission_request.fingerprint))

    def test_invalid_survives_save_and_load(self) -> None:
        source, session = self.completed_source()
        task = self.frozen_before_resolution(source, session)
        self.tamper(task.id, _set("write_source", "bogus"))
        loaded = self.memory.load_task(task.id)
        self.assertIs(WriteSource.INVALID, loaded.write_source)
        self.memory.save_task(session.id, loaded)
        self.assertEqual("invalid", self.raw_state(task.id)["write_source"])
        self.assertIs(WriteSource.INVALID, self.stored(task.id).write_source)
        self.memory.save_task(session.id, self.stored(task.id))
        self.assertEqual("invalid", self.raw_state(task.id)["write_source"])
        self.assertIs(WriteSource.INVALID, self.stored(task.id).write_source)

    def test_a_legacy_direct_row_without_the_keys_still_writes(self) -> None:
        task = self.direct_task()
        run = self.request(task, None)
        self.tamper(task.id, lambda document: (document.pop("write_source"), document.pop("write_source_task_id")))
        loaded = self.stored(task.id)
        self.assertIs(WriteSource.DIRECT, loaded.write_source)
        self.assertEqual("", loaded.write_source_task_id)
        decided = self.approve(task, run)
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        self.assertEqual(b"direct\n", (self.root / "direct.txt").read_bytes())

    def test_a_non_member_kind_serializes_as_the_invalid_verdict(self) -> None:
        task = self.direct_task()
        task.write_source = "direct"  # type: ignore[assignment]
        self.assertEqual("invalid", task.to_dict()["write_source"])
        self.assertIs(WriteSource.INVALID, Task.from_dict(task.to_dict()).write_source)


# --------------------------------------------------------------------------
# 3. Stage consistency
# --------------------------------------------------------------------------


class StageConsistencyTests(HandoffTestCase):
    def test_injected_content_before_resolution_is_refused(self) -> None:
        source, session = self.completed_source()
        task = self.frozen_before_resolution(source, session)
        self.tamper(task.id, _set("write_content", "injected\n"))
        run = self.core.resume(session.id, task.id)
        self.assert_refused_before_authority(task, run, "before the source was resolved")

    def test_a_handoff_on_a_non_write_task_is_refused_in_the_loop(self) -> None:
        source, session = self.completed_source()
        task = make_task()
        task.write_source = WriteSource.MODEL_ANSWER
        task.write_source_task_id = source.id
        run = self.core.run(task, session_id=session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("only valid for a file_write task", run.error.message)
        self.assertEqual("", self.stored(task.id).write_content)

    def test_frozen_content_after_resolution_is_accepted(self) -> None:
        source, session = self.completed_source()
        stored, _ = self.completed_handoff(source, session)
        self.assertEqual(PROPOSED_FILE.encode("utf-8"), (self.root / TARGET_NAME).read_bytes())
        self.assertIs(WriteSource.MODEL_ANSWER, stored.write_source)

    def test_content_blanked_after_resolution_is_refused(self) -> None:
        source, session = self.completed_source()
        task = self.handoff_task(source.id)
        run = self.request(task, session.id)
        self.tamper(task.id, _set("write_content", ""))
        again = self.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.FAILED, again.status)
        self.assertIs(ErrorCode.INVALID_TASK, again.error.code)
        self.assertIn("no frozen content", again.error.message)
        self.assertIsNone(again.permission_request)
        self.assertEqual(1, self.event_types(session.id, task.id).count("approval_requested"))
        self.assertEqual([], self.memory.execution_attempts(task.id))
        self.assertFalse((self.root / TARGET_NAME).exists())
        self.assertFalse(self.grant_consumed(run.permission_request.fingerprint))

    def test_terminal_erasure_keeps_the_source_identity(self) -> None:
        source, session = self.completed_source()
        stored, _ = self.completed_handoff(source, session)
        self.assertEqual("", stored.write_content)
        self.assertIs(WriteSource.MODEL_ANSWER, stored.write_source)
        self.assertEqual(source.id, stored.write_source_task_id)
        raw = self.raw_state(stored.id)
        self.assertEqual("", raw["write_content"])
        self.assertEqual("model_answer", raw["write_source"])
        self.assertEqual(source.id, raw["write_source_task_id"])
        # The bytes are gone; their digest is not, and it is the source's.
        self.assertEqual(source.model_answer.sha256, stored.local_write.intended_sha256)
        self.assertEqual("written", stored.local_write.status)


# --------------------------------------------------------------------------
# 4. Source eligibility - every refusal before plan, approval or attempt
# --------------------------------------------------------------------------


class SourceEligibilityTests(HandoffTestCase):
    def test_a_missing_source_is_refused(self) -> None:
        _, session = self.completed_source()
        self.refused(self.handoff_task(OTHER_ID), session.id, "Model answer handoff is not usable")

    def test_a_task_cannot_hand_off_its_own_answer(self) -> None:
        _, session = self.completed_source()
        task = self.handoff_task(OTHER_ID)
        task.write_source_task_id = task.id
        self.refused(task, session.id, "its own model answer")

    def test_a_source_from_another_session_is_refused(self) -> None:
        source, _ = self.completed_source()
        run = self.refused(self.handoff_task(source.id), None, "another session")
        self.assertNotIn("merhaba", json.dumps(run.error.to_dict()))

    def test_a_foreign_project_is_refused_through_session_isolation(self) -> None:
        self.memory.save_project(ProjectRecord(id="proj-a", name="A"))
        self.memory.save_project(ProjectRecord(id="proj-b", name="B"))
        task = self.source_task()
        task.project_id = "proj-a"
        run = self.core.run(task)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
        run = self.core.resume(
            run.session.id, task.id, grant=PermissionGrant.for_request(run.permission_request)
        )
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        document = self.handoff_document(task.id)
        document["project_id"] = "proj-b"
        self.refused(decode_task_json(json.dumps(document)), None, "another session")

    def test_an_awaiting_approval_source_is_refused(self) -> None:
        pending = self.source_task()
        run = self.core.run(pending)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.refused(self.handoff_task(pending.id), run.session.id, "did not complete")

    def test_a_failed_source_is_refused(self) -> None:
        self.answering.outcome = OutcomeStatus.ERROR
        try:
            task = self.source_task()
            run = self.core.run(task)
            run = self.core.resume(
                run.session.id, task.id, grant=PermissionGrant.for_request(run.permission_request)
            )
            self.assertEqual(RunStatus.FAILED, run.status)
        finally:
            self.answering.outcome = OutcomeStatus.SUCCESS
        self.refused(self.handoff_task(task.id), run.session.id, "did not complete")

    def test_a_cancelled_source_is_refused(self) -> None:
        task = self.source_task()
        run = self.core.run(task)
        cancelled = self.core.cancel(run.session.id, task.id)
        self.assertEqual(RunStatus.CANCELLED, cancelled.status)
        self.refused(self.handoff_task(task.id), run.session.id, "did not complete")

    def test_a_valid_answer_on_a_forced_non_completed_source_is_still_refused(self) -> None:
        # Isolates the COMPLETED gate: the answer IS present and valid.
        source, session = self.completed_source()
        source.status = TaskStatus.FAILED
        self.memory.save_task(session.id, source)
        self.assertTrue(self.memory.load_task(source.id).model_answer.present)
        self.refused(self.handoff_task(source.id), session.id, "did not complete")

    def test_an_unregistered_producer_is_refused(self) -> None:
        source, session = self.completed_source()
        app = self.fresh_app(
            connectors=(
                LocalFileWriteConnector(
                    allowed_roots=(self.root,), default_working_directory=self.root
                ),
            )
        )
        task = self.handoff_task(source.id)
        run = app.core.run(task, session_id=session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("not a registered connector", run.error.message)
        self.assertEqual("", self.stored(task.id).write_content)
        self.assertFalse((self.root / TARGET_NAME).exists())

    def test_a_forged_answer_on_a_non_egress_producer_is_refused(self) -> None:
        plain = make_task()
        plain.preferences["preferred_connector"] = "plain-echo"
        run = self.core.run(plain)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        self.assertEqual("plain-echo", self.memory.load_task(plain.id).selected_connector)
        forged = build_model_answer("forged answer\n").to_dict()
        self.tamper(plain.id, _set("model_answer", forged))
        self.assertTrue(self.memory.load_task(plain.id).model_answer.present)
        self.refused(self.handoff_task(plain.id), run.session.id, "not an external-model connector")

    def test_a_source_without_a_producer_name_is_refused(self) -> None:
        source, session = self.completed_source()
        self.tamper(source.id, _set("selected_connector", None))
        self.refused(self.handoff_task(source.id), session.id, "names no producer")

    def test_a_look_alike_answer_is_refused(self) -> None:
        source, session = self.completed_source()
        real = SQLiteMemoryStore.load_task

        def swapped(store, task_id):
            loaded = real(store, task_id)
            if loaded is not None and task_id == source.id:
                loaded.model_answer = _LookAlike(**loaded.model_answer.to_dict())  # type: ignore[assignment]
            return loaded

        with mock.patch.object(SQLiteMemoryStore, "load_task", swapped):
            self.refused(self.handoff_task(source.id), session.id, "carries no model answer")

    def test_a_malformed_persisted_answer_is_refused(self) -> None:
        source, session = self.completed_source()
        self.tamper(
            source.id,
            lambda document: document["model_answer"].__setitem__("sha256", "0" * 64),
        )
        self.assertFalse(self.memory.load_task(source.id).model_answer.present)
        self.refused(self.handoff_task(source.id), session.id, "carries no model answer")

    def test_a_truncated_answer_is_refused(self) -> None:
        source, session = self.completed_source("def f():\n    pass\n" * 1000)
        self.assertTrue(source.model_answer.truncated)
        self.refused(self.handoff_task(source.id), session.id, "truncated")

    def test_a_blank_answer_is_refused(self) -> None:
        for text in ("\n\n", "   \n\t\n", " "):
            with self.subTest(text=repr(text)):
                source, session = self.completed_source(text)
                self.refused(self.handoff_task(source.id), session.id, "blank")

    def test_a_nul_answer_is_refused(self) -> None:
        source, session = self.completed_source("a\x00b\n")
        self.refused(self.handoff_task(source.id), session.id, "not writable content")

    def test_a_malformed_source_classification_is_refused(self) -> None:
        source, session = self.completed_source()
        real = SQLiteMemoryStore.load_task
        for bad in ("internal", _Foreign.INTERNAL):
            with self.subTest(bad=repr(bad)):

                def swapped(store, task_id, bad=bad):
                    loaded = real(store, task_id)
                    if loaded is not None and task_id == source.id:
                        loaded.scope = replace(loaded.scope, data_classification=bad)
                    return loaded

                with mock.patch.object(SQLiteMemoryStore, "load_task", swapped):
                    self.refused(
                        self.handoff_task(source.id), session.id, "classification is malformed"
                    )

    def test_a_malformed_target_classification_is_refused(self) -> None:
        source, session = self.completed_source()
        task = self.handoff_task(source.id)
        task.scope = replace(task.scope, data_classification=_Foreign.INTERNAL)  # type: ignore[arg-type]
        run = self.core.run(task, session_id=session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertEqual("", self.stored(task.id).write_content)

    def test_the_full_classification_matrix(self) -> None:
        """Upward or equal only; SECRET source never; SECRET target allowed.

        The test producer does not refuse SECRET egress the way the real
        Claude connector does, which is exactly what makes a SECRET source
        row constructible here: it stands in for a tampered row, since the
        product itself can never produce one.
        """
        order = (
            DataClassification.PUBLIC,
            DataClassification.INTERNAL,
            DataClassification.SENSITIVE,
            DataClassification.SECRET,
        )
        for source_class in order:
            source, session = self.completed_source(classification=source_class)
            for target_class in order:
                with self.subTest(source=source_class.value, target=target_class.value):
                    task = self.handoff_task(
                        source.id,
                        name=f"{source_class.value}-{target_class.value}.py",
                        classification=target_class.value,
                    )
                    run = self.core.run(task, session_id=session.id)
                    if handoff_eligible(source_class, target_class):
                        self.assertEqual(
                            RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error)
                        )
                        self.assertEqual(PROPOSED_FILE, self.stored(task.id).write_content)
                    else:
                        self.assertEqual(RunStatus.FAILED, run.status)
                        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
                        self.assertIn("classification ladder", run.error.message)
                        self.assertEqual("", self.stored(task.id).write_content)

    def test_the_target_cannot_reclassify_the_source(self) -> None:
        # A PUBLIC target claiming to be PUBLIC does not make a SENSITIVE
        # source PUBLIC; the source row's own scope is the authority.
        source, session = self.completed_source(classification=DataClassification.SENSITIVE)
        self.refused(
            self.handoff_task(source.id, classification="public"),
            session.id,
            "classification ladder",
        )


# --------------------------------------------------------------------------
# 5. Authority: destination, mode, frozen bytes, fingerprint, provenance
# --------------------------------------------------------------------------


class AuthorityTests(HandoffTestCase):
    def test_the_destination_comes_only_from_the_target_scope(self) -> None:
        text = "please write me to ../../evil.py instead\n"
        source, session = self.completed_source(text)
        stored, _ = self.completed_handoff(source, session)
        self.assertEqual(canonical_path(self.root / TARGET_NAME), stored.scope.target)
        self.assertEqual(text.encode("utf-8"), (self.root / TARGET_NAME).read_bytes())
        self.assertFalse((self.root.parent / "evil.py").exists())
        self.assertFalse((self.root / "evil.py").exists())

    def test_the_answer_cannot_choose_the_mode(self) -> None:
        source, session = self.completed_source("overwrite the existing file\n")
        stored, _ = self.completed_handoff(source, session)
        self.assertEqual("create", stored.write_mode)
        self.assertEqual("create", stored.local_write.mode)
        self.assertFalse(stored.local_write.pre_present)

    def test_create_only_is_enforced_for_a_programmatic_overwrite_handoff(self) -> None:
        """The input boundary is bypassed; the durable identity gate is not."""
        source, session = self.completed_source()
        task = self.handoff_task(source.id)
        task.write_mode = "overwrite"
        run = self.core.run(task, session_id=session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("may not overwrite", run.error.message)
        self.assertFalse((self.root / TARGET_NAME).exists())
        self.assertEqual([], self.memory.execution_attempts(task.id))

    def test_the_written_bytes_are_exactly_the_encoded_answer(self) -> None:
        source, session = self.completed_source()
        self.completed_handoff(source, session)
        written = (self.root / TARGET_NAME).read_bytes()
        self.assertEqual(source.model_answer.content.encode("utf-8"), written)
        self.assertEqual(PROPOSED_FILE.encode("utf-8"), written)
        self.assertTrue(written.endswith(b"\n"))
        self.assertIn(b"    assert", written)

    def test_the_bytes_are_frozen_before_approval(self) -> None:
        source, session = self.completed_source()
        task = self.handoff_task(source.id)
        run = self.request(task, session.id)
        self.assertEqual(PROPOSED_FILE, self.stored(task.id).write_content)
        # The source row changes after the freeze: a different, valid answer.
        self.tamper(source.id, _set("model_answer", build_model_answer("changed\n").to_dict()))
        self.assertEqual("changed\n", self.memory.load_task(source.id).model_answer.content)
        decided = self.approve(task, run)
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        self.assertEqual(PROPOSED_FILE.encode("utf-8"), (self.root / TARGET_NAME).read_bytes())

    def test_one_changed_byte_changes_the_attestation(self) -> None:
        first, session = self.completed_source(PROPOSED_FILE)
        second, _ = self.completed_source(PROPOSED_FILE[:-1] + "!")  # hmm
        run_first = self.request(self.handoff_task(first.id), session.id)
        # ``completed_source`` opened its own session; the second handoff must
        # live in that session, so request it there.
        self.assertNotEqual(first.model_answer.sha256, second.model_answer.sha256)
        run_second = self.request(self.handoff_task(second.id), self.memory.task_session_id(second.id))
        self.assertNotEqual(
            run_first.permission_request.attestation.outbound_digest,
            run_second.permission_request.attestation.outbound_digest,
        )

    def test_identical_inputs_attest_identically_and_a_new_destination_does_not(self) -> None:
        source, session = self.completed_source()
        same_a = self.request(self.handoff_task(source.id), session.id)
        same_b = self.request(self.handoff_task(source.id), session.id)
        elsewhere = self.request(self.handoff_task(source.id, name="other.py"), session.id)
        self.assertEqual(
            same_a.permission_request.attestation.outbound_digest,
            same_b.permission_request.attestation.outbound_digest,
        )
        self.assertNotEqual(
            same_a.permission_request.attestation.outbound_digest,
            elsewhere.permission_request.attestation.outbound_digest,
        )

    def test_the_approval_reason_names_the_source_and_the_exact_digest(self) -> None:
        source, session = self.completed_source()
        run = self.request(self.handoff_task(source.id), session.id)
        reason = run.permission_request.reason
        self.assertIn(source.id, reason)
        self.assertIn(f"sha256 {source.model_answer.sha256}", reason)
        self.assertIn(f"{len(PROPOSED_FILE.encode('utf-8'))} bytes", reason)
        self.assertNotIn("merhaba", reason)
        # The reason is bound: change it and the fingerprint changes.
        request = run.permission_request
        self.assertNotEqual(
            request.fingerprint, replace(request, reason=reason + " tampered").fingerprint
        )
        # The envelope shows the same reason, unredacted.
        payload = build_envelope("run", PermissionMode.SESSION, run)
        self.assertEqual(reason, payload["approval"]["reason"])

    def test_direct_writes_keep_their_default_reason_and_frame(self) -> None:
        task = self.direct_task()
        run = self.request(task, None)
        self.assertEqual(DEFAULT_WRITE_REASON, run.permission_request.reason)
        self.assertEqual(
            hashlib.sha256(self.writer.canonical_outbound(self.stored(task.id))).hexdigest(),
            run.permission_request.attestation.outbound_digest,
        )

    def test_digest_continuity_is_an_invariant(self) -> None:
        source, session = self.completed_source()
        stored, _ = self.completed_handoff(source, session)
        expected = hashlib.sha256(source.model_answer.content.encode("utf-8")).hexdigest()
        self.assertEqual(expected, source.model_answer.sha256)
        self.assertEqual(expected, stored.local_write.intended_sha256)
        self.assertEqual(
            expected, hashlib.sha256((self.root / TARGET_NAME).read_bytes()).hexdigest()
        )
        self.assertEqual(
            len(source.model_answer.content.encode("utf-8")),
            stored.local_write.intended_byte_size,
        )

    def test_tampered_content_after_approval_cannot_use_the_old_grant(self) -> None:
        source, session = self.completed_source()
        task = self.handoff_task(source.id)
        run = self.request(task, session.id)
        request = run.permission_request
        grant = PermissionGrant.for_request(request)
        self.assertTrue(self.memory.resolve_approval(request, grant, "approved"))
        self.tamper(task.id, _set("write_content", "tampered\n"))
        again = self.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, again.status, msg=str(again.error))
        self.assertNotEqual(request.id, again.permission_request.id)
        self.assertNotEqual(request.fingerprint, again.permission_request.fingerprint)
        self.assertFalse(self.grant_consumed(grant.fingerprint))
        self.assertFalse((self.root / TARGET_NAME).exists())
        with_grant = self.core.resume(session.id, task.id, grant=grant)
        self.assertIsNot(RunStatus.COMPLETED, with_grant.status)
        self.assertFalse((self.root / TARGET_NAME).exists())
        self.assertEqual([], self.memory.execution_attempts(task.id))

    def test_a_changed_reference_after_approval_cannot_use_the_old_grant(self) -> None:
        source, session = self.completed_source()
        task = self.handoff_task(source.id)
        run = self.request(task, session.id)
        request = run.permission_request
        grant = PermissionGrant.for_request(request)
        self.assertTrue(self.memory.resolve_approval(request, grant, "approved"))
        self.tamper(task.id, _set("write_source_task_id", OTHER_ID))
        again = self.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, again.status, msg=str(again.error))
        self.assertNotEqual(request.id, again.permission_request.id)
        self.assertNotEqual(request.fingerprint, again.permission_request.fingerprint)
        self.assertIn(OTHER_ID, again.permission_request.reason)
        self.assertNotIn(source.id, again.permission_request.reason)
        # The frozen bytes were never re-resolved from the new reference.
        self.assertEqual(PROPOSED_FILE, self.stored(task.id).write_content)
        self.assertFalse(self.grant_consumed(grant.fingerprint))
        self.assertFalse((self.root / TARGET_NAME).exists())
        with_grant = self.core.resume(session.id, task.id, grant=grant)
        self.assertIsNot(RunStatus.COMPLETED, with_grant.status)
        self.assertFalse((self.root / TARGET_NAME).exists())

    def test_kind_corruption_after_approval_cannot_use_the_old_grant(self) -> None:
        source, session = self.completed_source()
        task = self.handoff_task(source.id)
        run = self.request(task, session.id)
        grant = PermissionGrant.for_request(run.permission_request)
        self.tamper(task.id, _set("write_source", "direct"))
        decided = self.core.resume(session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.INVALID_TASK, decided.error.code)
        self.assertFalse(self.grant_consumed(grant.fingerprint))
        self.assertEqual([], self.memory.execution_attempts(task.id))
        self.assertFalse((self.root / TARGET_NAME).exists())

    def test_one_answer_may_feed_two_targets_under_two_approvals(self) -> None:
        source, session = self.completed_source()
        first = self.handoff_task(source.id, name="a.py")
        second = self.handoff_task(source.id, name="b.py")
        run_first = self.request(first, session.id)
        run_second = self.request(second, session.id)
        self.assertNotEqual(run_first.permission_request.id, run_second.permission_request.id)
        self.assertNotEqual(
            run_first.permission_request.fingerprint, run_second.permission_request.fingerprint
        )
        self.assertEqual(RunStatus.COMPLETED, self.approve(first, run_first).status)
        # The second is still waiting for ITS approval.
        self.assertFalse((self.root / "b.py").exists())
        self.assertEqual(RunStatus.COMPLETED, self.approve(second, run_second).status)
        self.assertEqual((self.root / "a.py").read_bytes(), (self.root / "b.py").read_bytes())
        self.assertEqual(1, len(self.memory.execution_attempts(first.id)))
        self.assertEqual(1, len(self.memory.execution_attempts(second.id)))


# --------------------------------------------------------------------------
# 6. Crash / restart
# --------------------------------------------------------------------------


class CrashRestartTests(HandoffTestCase):
    def test_a_restart_before_the_context_checkpoint_resolves_the_same_bytes(self) -> None:
        source, session = self.completed_source()
        task = self.frozen_before_resolution(source, session)
        app = self.fresh_app()
        run = app.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
        self.assertEqual(PROPOSED_FILE, self.stored(task.id).write_content)
        decided = app.core.resume(
            session.id, task.id, grant=PermissionGrant.for_request(run.permission_request)
        )
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        self.assertEqual(PROPOSED_FILE.encode("utf-8"), (self.root / TARGET_NAME).read_bytes())

    def test_a_restart_after_the_context_checkpoint_never_reads_the_source(self) -> None:
        source, session = self.completed_source()
        task = self.handoff_task(source.id)
        run = self.request(task, session.id)
        real = SQLiteMemoryStore.load_task

        def guarded(store, task_id):
            if task_id == source.id:
                raise AssertionError("the source was re-read after the freeze")
            return real(store, task_id)

        app = self.fresh_app()
        with mock.patch.object(SQLiteMemoryStore, "load_task", guarded):
            decided = app.core.resume(
                session.id, task.id, grant=PermissionGrant.for_request(run.permission_request)
            )
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        self.assertEqual(PROPOSED_FILE.encode("utf-8"), (self.root / TARGET_NAME).read_bytes())

    def test_a_restart_at_approval_regates_from_frozen_bytes_only(self) -> None:
        source, session = self.completed_source()
        task = self.handoff_task(source.id)
        run = self.request(task, session.id)
        real = SQLiteMemoryStore.load_task

        def guarded(store, task_id):
            if task_id == source.id:
                raise AssertionError("the source was re-read after the freeze")
            return real(store, task_id)

        app = self.fresh_app()
        with mock.patch.object(SQLiteMemoryStore, "load_task", guarded):
            again = app.core.resume(session.id, task.id)
        # Same frozen bytes, same attestation: the pending request is reused.
        self.assertEqual(RunStatus.AWAITING_APPROVAL, again.status)
        self.assertEqual(run.permission_request.id, again.permission_request.id)
        self.assertEqual(run.permission_request.reason, again.permission_request.reason)

    def test_a_restart_before_execute_reclaims_the_prepared_attempt(self) -> None:
        source, session = self.completed_source()
        task, _, grant = self.prepared_attempt(source, session)
        app = self.fresh_app()
        decided = app.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        attempts = app.memory.execution_attempts(task.id)
        self.assertEqual(1, len(attempts))
        self.assertIs(AttemptState.FINISHED, attempts[0].state)
        self.assertTrue(self.grant_consumed(grant.fingerprint))
        self.assertEqual(PROPOSED_FILE.encode("utf-8"), (self.root / TARGET_NAME).read_bytes())

    def test_h2_unknown_outcome_semantics_are_unchanged(self) -> None:
        raiser = RaisingWriter(allowed_roots=(self.root,), default_working_directory=self.root)
        app = create_app(
            settings=Settings(
                memory_database=self.root / "raising.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(AnsweringConnector(answer_text=PROPOSED_FILE), raiser),
        )
        source_task = self.source_task()
        run = app.core.run(source_task)
        run = app.core.resume(
            run.session.id, source_task.id, grant=PermissionGrant.for_request(run.permission_request)
        )
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        task = self.handoff_task(source_task.id)
        run = app.core.run(task, session_id=run.session.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
        decided = app.core.resume(
            run.session.id, task.id, grant=PermissionGrant.for_request(run.permission_request)
        )
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, decided.error.code)
        attempt = app.memory.latest_execution_attempt(task.id)
        self.assertIs(AttemptState.UNKNOWN_OUTCOME, attempt.state)
        stored = app.memory.load_task(task.id)
        # In doubt: the frozen bytes and the identity are both retained.
        self.assertEqual(PROPOSED_FILE, stored.write_content)
        self.assertIs(WriteSource.MODEL_ANSWER, stored.write_source)
        calls = {"count": 0}
        original = RaisingWriter.execute

        def counting(connector, task_, session_):
            calls["count"] += 1
            return original(connector, task_, session_)

        with mock.patch.object(RaisingWriter, "execute", counting):
            again = app.core.resume(run.session.id, task.id)
        self.assertEqual(0, calls["count"], "an in-doubt handoff write was replayed")
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, again.error.code)


# --------------------------------------------------------------------------
# 7. PREPARED attempt on resume with malformed identity (H2 untouched)
# --------------------------------------------------------------------------


class PreparedAttemptTests(HandoffTestCase):
    def test_a_malformed_identity_with_a_prepared_attempt_refuses_without_touching_h2(
        self,
    ) -> None:
        source, session = self.completed_source()
        task, _, grant = self.prepared_attempt(source, session)
        before = self.memory.execution_attempts(task.id)
        self.tamper(task.id, _set("write_source", "direct"))
        decided = self.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.INVALID_TASK, decided.error.code)
        self.assertIsNot(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, decided.error.code)
        after = self.memory.execution_attempts(task.id)
        self.assertEqual(1, len(after))
        self.assertEqual(before[0].id, after[0].id)
        self.assertIs(AttemptState.PREPARED, after[0].state)
        self.assertFalse(self.grant_consumed(grant.fingerprint))
        self.assertFalse((self.root / TARGET_NAME).exists())
        self.assertIs(TaskStatus.FAILED, self.stored(task.id).status)
        # A later resume reports the terminal task and still never claims.
        reported = self.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.FAILED, reported.status)
        self.assertIs(AttemptState.PREPARED, self.memory.latest_execution_attempt(task.id).state)

    def test_tampered_content_with_a_prepared_attempt_regates_without_claiming(
        self,
    ) -> None:
        """At EXECUTE the recorded grant is compared by fingerprint.

        This is the stage where a persisted approval is consulted directly
        (``_unsatisfied_gate`` -> ``has_approved_grant``), so it is where a
        content change after approval must be caught by the re-attestation
        rather than by the mere absence of a grant object.
        """
        source, session = self.completed_source()
        task, run, grant = self.prepared_attempt(source, session)
        self.tamper(task.id, _set("write_content", "tampered\n"))
        again = self.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, again.status, msg=str(again.error))
        self.assertNotEqual(run.permission_request.id, again.permission_request.id)
        self.assertNotEqual(
            run.permission_request.fingerprint, again.permission_request.fingerprint
        )
        attempts = self.memory.execution_attempts(task.id)
        self.assertEqual(1, len(attempts))
        self.assertIs(AttemptState.PREPARED, attempts[0].state)
        self.assertFalse(self.grant_consumed(grant.fingerprint))
        self.assertFalse((self.root / TARGET_NAME).exists())

    def test_a_changed_reference_with_a_prepared_attempt_regates_without_claiming(
        self,
    ) -> None:
        source, session = self.completed_source()
        task, _, grant = self.prepared_attempt(source, session)
        self.tamper(task.id, _set("write_source_task_id", OTHER_ID))
        again = self.core.resume(session.id, task.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, again.status, msg=str(again.error))
        self.assertIn(OTHER_ID, again.permission_request.reason)
        attempts = self.memory.execution_attempts(task.id)
        self.assertEqual(1, len(attempts))
        self.assertIs(AttemptState.PREPARED, attempts[0].state)
        self.assertFalse(self.grant_consumed(grant.fingerprint))
        self.assertFalse((self.root / TARGET_NAME).exists())


# --------------------------------------------------------------------------
# 8. Egress / retrieval / decision isolation and the public contract
# --------------------------------------------------------------------------


class IsolationTests(HandoffTestCase):
    def test_the_write_path_never_selects_or_invokes_claude(self) -> None:
        claude = ClaudeCodeConnector(allowed_roots=(self.root,), default_working_directory=self.root)
        app = create_app(
            settings=Settings(
                memory_database=self.root / "isolated.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(
                AnsweringConnector(answer_text=PROPOSED_FILE),
                claude,
                LocalFileWriteConnector(
                    allowed_roots=(self.root,), default_working_directory=self.root
                ),
            ),
        )
        with mock.patch(
            "jarvis.connectors.claude_code.subprocess.Popen", side_effect=AssertionError
        ), mock.patch(
            "jarvis.connectors.claude_code.subprocess.run", side_effect=AssertionError
        ), mock.patch(
            "jarvis.connectors.claude_code.shutil.which", side_effect=AssertionError
        ):
            source_task = self.source_task()
            run = app.core.run(source_task)
            run = app.core.resume(
                run.session.id,
                source_task.id,
                grant=PermissionGrant.for_request(run.permission_request),
            )
            self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
            task = self.handoff_task(source_task.id)
            self.assertFalse(claude.can_handle(task))
            run = app.core.run(task, session_id=run.session.id)
            self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
            decided = app.core.resume(
                run.session.id, task.id, grant=PermissionGrant.for_request(run.permission_request)
            )
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        self.assertEqual("local-file-write", app.memory.load_task(task.id).selected_connector)
        self.assertEqual((), EGRESS_TOOL_GRANTS)

    def test_resolution_invokes_no_model(self) -> None:
        source, session = self.completed_source()
        calls_before = self.answering.execute_calls
        self.completed_handoff(source, session)
        self.assertEqual(calls_before, self.answering.execute_calls)

    def test_a_task_with_write_state_adds_nothing_to_the_claude_payload(self) -> None:
        claude = ClaudeCodeConnector(allowed_roots=(self.root,), default_working_directory=self.root)
        document = json.dumps(
            {
                "objective": "o",
                "capability": "code_planning",
                "scope": {"action": "analyze", "target": str(self.root)},
            }
        )
        plain = decode_task_json(document)
        marked = decode_task_json(document)
        marked.write_source = WriteSource.MODEL_ANSWER
        marked.write_source_task_id = OTHER_ID
        marked.write_content = PROPOSED_FILE
        plain_payload, _ = claude._sanitized_payload(plain)
        marked_payload, _ = claude._sanitized_payload(marked)
        self.assertEqual(set(plain_payload), set(marked_payload))
        serialized = json.dumps(marked_payload, ensure_ascii=False)
        for forbidden in ("write_source", "write_content", "merhaba", OTHER_ID):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(claude.canonical_outbound(plain), claude.canonical_outbound(marked))

    def test_resolution_uses_load_task_not_retrieval(self) -> None:
        source, session = self.completed_source()
        loads: list[str] = []
        real = SQLiteMemoryStore.load_task

        def watched(store, task_id):
            loads.append(task_id)
            return real(store, task_id)

        with mock.patch.object(
            SQLiteMemoryStore, "retrieve_context", lambda store, query: RetrievedContext()
        ), mock.patch.object(SQLiteMemoryStore, "load_task", watched):
            stored, _ = self.completed_handoff(source, session)
        self.assertEqual(1, loads.count(source.id))
        self.assertEqual(PROPOSED_FILE.encode("utf-8"), (self.root / TARGET_NAME).read_bytes())
        self.assertEqual((), stored.retrieved_context.items)

    def test_the_write_task_carries_no_answer_decision_or_memory(self) -> None:
        source, session = self.completed_source()
        decisions_before = len(self.memory.list_decisions(session.id))
        stored, _ = self.completed_handoff(source, session)
        self.assertFalse(stored.model_answer.present)
        self.assertEqual(decisions_before, len(self.memory.list_decisions(session.id)))
        connection = sqlite3.connect(self.database)
        try:
            count = connection.execute("SELECT COUNT(*) FROM project_memory_items").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, count)
        # A later task in the session inherits nothing from the write.
        later = self.source_task()
        run = self.core.run(later, session_id=session.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
        for item in self.memory.load_task(later.id).retrieved_context.items:
            self.assertNotEqual(stored.id, item.source_task_id)
        self.assertEqual(5, RETRIEVAL_MAX_ITEMS)
        self.assertEqual(4_000, RETRIEVAL_MAX_CHARACTERS)

    def test_the_public_envelope_is_unchanged(self) -> None:
        source, session = self.completed_source()
        _, run = self.completed_handoff(source, session)
        direct = self.direct_task()
        direct_run = self.approve(direct, self.request(direct, session.id))
        self.assertEqual(RunStatus.COMPLETED, direct_run.status, msg=str(direct_run.error))
        handoff_payload = build_envelope("resume", PermissionMode.SESSION, run)
        direct_payload = build_envelope("resume", PermissionMode.SESSION, direct_run)
        self.assertEqual(5, OUTPUT_SCHEMA_VERSION)
        self.assertEqual(5, handoff_payload["schema_version"])
        self.assertEqual(set(direct_payload), set(handoff_payload))
        serialized = json.dumps(handoff_payload)
        self.assertNotIn("write_source", serialized)
        self.assertNotIn("merhaba", serialized)
        self.assertIsNone(handoff_payload["model_answer"])
        self.assertEqual(
            source.model_answer.sha256, handoff_payload["local_write"]["intended_sha256"]
        )
        self.assertEqual("written", handoff_payload["local_write"]["status"])


if __name__ == "__main__":
    unittest.main()
