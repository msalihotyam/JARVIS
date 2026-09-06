"""M10 - direct whole-file overwrite of an existing local file.

The acceptance chain this file pins end to end:

    completed HEAD file_read -> context.local_read_task_id -> GENUINE source
    resolution in AgentCore -> mode "overwrite" -> platform gate -> no-follow
    chain -> volume gate -> mandatory approval binding BOTH attested states ->
    a leaf handle that pins the object AND its directory entry -> re-digest
    through that same descriptor -> in-place rewrite -> verification

plus everything that must NOT happen: an overwrite bound to no reference, to
an injected snapshot, to a non-``file_read`` source, to an incomplete source,
to another session's source, to a REGION read, to a transitively copied
snapshot, or to a stale digest; a volume inspected before the reparse chain
has accepted the path; an overwrite on a platform that cannot pin the
approved directory entry; an overwrite routed into the create installer; a
model answer supplying the replacement bytes; and any change at all to create.

Two tests carry most of the weight. ``NamespaceAuthorityTests`` proves the
hostile rename is REFUSED - JARVIS authority is (object AND path), and a test
that accepted "the rename succeeded but we wrote to the old inode anyway"
would be asserting the violation. ``GenuineSourceTests`` proves that a
matching snapshot is not provenance: only an explicit reference to a real
completed ``file_read`` can authorize destroying a file.

NOTHING here injects ``task.local_read`` to make an overwrite SUCCEED. Every
positive test resolves its preimage through the real binder; injection
appears only where the injection itself is what must be refused.

No live model, connector, auth or network call is ever made.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis.app import TaskInputError, create_app, decode_task_json
from jarvis.connectors import local_files
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.connectors.local_files import (
    LocalFileReadConnector,
    LocalFileWriteConnector,
)
from jarvis.contracts import (
    LOCAL_READ_CAPABILITY,
    LOCAL_READ_TASK_REFERENCE_KEY,
    LOCAL_WRITE_CAPABILITY,
    LOCAL_WRITE_ENABLED_MODES,
    LOCAL_WRITE_MAX_BYTES,
    LOCAL_WRITE_MODES,
    LOCAL_WRITE_SOURCE_KEY,
    ActionScope,
    AttemptState,
    AttestationError,
    DataClassification,
    ErrorCode,
    LocalReadSnapshot,
    PermissionGrant,
    PermissionMode,
    RunStatus,
    SessionState,
    SideEffect,
    Task,
    TaskStatus,
    ToolResult,
    WriteSource,
)
from jarvis.destinations import (
    CLAUDE_CODE_DESTINATION,
    DestinationGrant,
)
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, open_app, safe_cleanup
from tests.test_model_answer import AnsweringConnector

#: 512 bytes per line, so a whole number of them lands exactly on the ceiling.
CEILING_LINE = "x" * 511 + "\n"
CEILING_LINES = LOCAL_WRITE_MAX_BYTES // 512

#: Well-formed by the reference grammar, never a real task. Used only where a
#: CONNECTOR is driven directly: the connector does not resolve references, so
#: this stands in for "the loop already proved provenance".
UNRESOLVED = "c" * 36

#: Windows share-mode facts, used only to drive hostile acts in the tests.
_DELETE = 0x00010000
_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE = ctypes.c_void_p(-1).value

WINDOWS_ONLY = "the platform cannot pin a directory entry, so overwrite is refused"


def open_with_delete_access(path: Path) -> bool:
    """Try to open ``path`` with DELETE access. ``True`` means it succeeded.

    DELETE access is what a rename or a delete needs, so this is the single
    question the whole entry-pinning guarantee answers.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        ctypes.c_wchar_p(str(path)),
        ctypes.c_uint32(_DELETE),
        ctypes.c_uint32(_SHARE_ALL),
        None,
        ctypes.c_uint32(_OPEN_EXISTING),
        ctypes.c_uint32(_FILE_ATTRIBUTE_NORMAL),
        None,
    )
    if handle in (_INVALID_HANDLE, 0, None):
        return False
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True


class OmittingModelConnector(AnsweringConnector):
    """A capable external-model connector that IGNORES its read context.

    Provider-neutral by construction: a test-local class with a name no
    product code references. ``canonical_outbound`` serializes the objective
    only - it never touches ``task.local_read`` - so the attested bytes
    provably do not contain the file the answer claims to be about, while
    every gate the product actually enforces still passes.
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("name", "omitting-model")
        super().__init__(**kwargs)

    def canonical_outbound(self, task: Task) -> bytes:
        return json.dumps({"objective": task.objective}, sort_keys=True).encode(
            "utf-8"
        )


class IncludingModelConnector(AnsweringConnector):
    """The honest counterpart: it DOES serialize the frozen read context.

    It exists to prove the point of the deferral. M10 refuses this connector
    exactly as it refuses the omitting one, because JARVIS cannot tell them
    apart from durable state. Refusing only the hostile one would assert a
    capability this product does not have.
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("name", "including-model")
        super().__init__(**kwargs)

    def canonical_outbound(self, task: Task) -> bytes:
        return json.dumps(
            {
                "objective": task.objective,
                "file": task.local_read.relative_path,
                "content": task.local_read.excerpt,
            },
            sort_keys=True,
        ).encode("utf-8")


#: The two real local connectors these fixtures compose, written out.
_LOCAL_ROWS = frozenset(
    {
        DestinationGrant("local-file-read", "file_read", CLAUDE_CODE_DESTINATION),
        DestinationGrant("local-file-write", "file_write", CLAUDE_CODE_DESTINATION),
    }
)


class WroteThenRaised(LocalFileWriteConnector):
    """Really performs the write and THEN raises: the hardest recovery case."""

    manifest = replace(LocalFileWriteConnector.manifest, name="wrote-then-raised")

    def execute(self, task: Task, session: SessionState) -> ToolResult:  # type: ignore[override]
        super().execute(task, session)
        raise RuntimeError("the result never made it back")


class RaisedBeforeWriting(LocalFileWriteConnector):
    """Raises before touching the disk at all."""

    manifest = replace(LocalFileWriteConnector.manifest, name="raised-before")

    def execute(self, task: Task, session: SessionState) -> ToolResult:  # type: ignore[override]
        raise RuntimeError("the write blew up mid-flight")


class EditTestCase(unittest.TestCase):
    """One temp root, the real read and write connectors, the real app."""

    permission_mode = PermissionMode.SMART_AUTO

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self._temp)
        self.root = Path(self._temp.name).resolve()
        self.database = self.root / "memory.db"
        self.settings = Settings(
            memory_database=self.database, permission_mode=self.permission_mode
        )
        self.reader = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.writer = LocalFileWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.app = open_app(
            settings=self.settings,
            connectors=(self.reader, self.writer),
            destination_policy=_LOCAL_ROWS,
        )
        self.core = self.app.core
        self.memory = self.app.memory
        self.session_id: str | None = None

    # -- fixtures -----------------------------------------------------------

    def seed(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def snapshot(
        self, name: str, *, start_line: int = 0, max_lines: int = 0
    ) -> LocalReadSnapshot:
        """A real snapshot from the real reader, WITHOUT a source task.

        Deliberately provenance-less: it exists so tests can inject a snapshot
        that matches perfectly and prove that matching is not authority. It is
        never used to make an overwrite succeed.
        """
        task = Task(
            objective="read the file",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(
                action="read",
                target=str(self.root / name),
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )
        if start_line:
            task.local_read_kind = local_files.LocalReadKind.REGION
            task.read_start_line = start_line
            task.read_max_lines = max_lines
        result = self.reader.execute(task, SessionState())
        self.assertIsNone(result.error, msg=str(result.summary))
        return result.data["local_read"]

    def read_document(self, name: str, window: dict | None = None) -> dict:
        document = {
            "objective": f"read {name}",
            "capability": LOCAL_READ_CAPABILITY,
            "scope": {"action": "read", "target": str(self.root / name)},
            "context": {"working_directory": str(self.root)},
        }
        if window is not None:
            document["read"] = window
        return document

    def run_document(self, document: dict):
        response = self.app.run(json.dumps(document), session_id=self.session_id)
        self.session_id = response.payload["session"]["id"]
        return response

    def completed_read(self, name: str, window: dict | None = None) -> str:
        """One GENUINE completed file_read in this session; returns its id."""
        payload = self.run_document(self.read_document(name, window)).payload
        self.assertEqual("completed", payload["task"]["status"], payload.get("error"))
        return payload["task"]["id"]

    def prepare_source(self, name: str, content: str) -> tuple[Path, str]:
        """Seed the file and read it for real. The only way to a preimage."""
        target = self.seed(name, content)
        return target, self.completed_read(name)

    def edit_task(
        self,
        name: str,
        content: str,
        snapshot: LocalReadSnapshot | None = None,
        *,
        read_task_id: str | None = None,
        mode: str = "overwrite",
    ) -> Task:
        """One write task. ``snapshot`` INJECTS a preimage and is for negative
        tests only; ``read_task_id`` is the real reference the loop resolves."""
        task = Task(
            objective=f"replace {name}",
            capability=LOCAL_WRITE_CAPABILITY,
            scope=ActionScope(
                action="write",
                target=str(self.root / name),
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )
        task.write_content = content
        task.write_mode = mode
        if read_task_id is not None:
            task.context[LOCAL_READ_TASK_REFERENCE_KEY] = read_task_id
        if snapshot is not None:
            task.local_read = snapshot
        return task

    # -- drivers ------------------------------------------------------------

    def bound(self, name: str, content: str, read_task_id: str):
        """A task whose preimage the REAL binder resolved from a real read.

        Never an injection: the task enters CONTEXT_RETRIEVAL carrying no
        snapshot at all, the explicit reference is resolved against the
        completed source read, and what ends up on the task is that source's
        own snapshot.
        """
        task = self.edit_task(name, content, read_task_id=read_task_id)
        self.assertFalse(task.local_read.present)
        run = self.core.run(task, session_id=self.session_id)
        self.assertEqual(
            RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error)
        )
        self.assertTrue(task.local_read.present)
        self.session_id = run.session.id
        return task, run

    def approve(self, task: Task, run):
        grant = PermissionGrant.for_request(run.permission_request)
        return self.core.resume(run.session.id, task.id, grant=grant)

    def completed_overwrite(self, name: str, content: str, read_task_id: str):
        task, run = self.bound(name, content, read_task_id)
        final = self.approve(task, run)
        self.assertEqual(RunStatus.COMPLETED, final.status, msg=str(final.error))
        return self.memory.load_task(task.id), final

    def prepared(self, task: Task) -> Task:
        record = self.writer.pre_execution_record(task)
        self.assertIsNotNone(record, "the authority could not be prepared")
        task.local_write = record
        return task

    def completed(self, task: Task):
        run = self.core.run(task, session_id=self.session_id)
        self.assertEqual(
            RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error)
        )
        self.session_id = run.session.id
        final = self.approve(task, run)
        self.assertEqual(RunStatus.COMPLETED, final.status, msg=str(final.error))
        return self.memory.load_task(task.id), final

    def refused(self, task: Task):
        run = self.core.run(task, session_id=self.session_id)
        if run.status is RunStatus.AWAITING_APPROVAL:
            self.session_id = run.session.id
            run = self.approve(task, run)
        self.assertEqual(RunStatus.FAILED, run.status)
        return run

    def refused_before_approval(self, task: Task, fragment: str = ""):
        """Refused with no approval requested and no attempt opened."""
        run = self.core.run(task, session_id=self.session_id)
        self.assertEqual(RunStatus.FAILED, run.status, msg=str(run.error))
        self.assertIsNone(run.permission_request)
        self.assertIsNone(self.memory.load_pending_approval(task.id, run.session.id))
        self.assertEqual([], self.memory.execution_attempts(task.id))
        if fragment:
            self.assertIn(fragment, run.error.message)
        return run

    # -- persisted-row tampering -------------------------------------------

    def raw_state(self, task_id: str) -> dict:
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row, f"no persisted row for {task_id}")
        return json.loads(row[0])

    def delete_task_row(self, task_id: str) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            connection.commit()
        finally:
            connection.close()

    def tamper(self, task_id: str, mutate) -> None:
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


# ==========================================================================
# GENUINE SOURCE - a matching snapshot is NOT provenance
# ==========================================================================
@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class GenuineSourceTests(EditTestCase):
    """P1. Structure matching says what a value IS, never where it came from.

    Every refusal here would be satisfied by the connector's path/digest/size
    comparison, and every one of them must still fail closed.
    """

    def test_a_genuine_completed_head_read_is_the_positive_control(self) -> None:
        target, read_id = self.prepare_source("notes.txt", "before\n")
        stored, _ = self.completed_overwrite("notes.txt", "after the edit\n", read_id)
        self.assertEqual("after the edit\n", target.read_text(encoding="utf-8"))
        self.assertEqual("overwrite", stored.local_write.mode)
        self.assertEqual(
            read_id, stored.context.get(LOCAL_READ_TASK_REFERENCE_KEY)
        )

    def test_a_perfectly_matching_injected_snapshot_with_no_reference(self) -> None:
        """The finding itself: matching is not authority."""
        target = self.seed("notes.txt", "before\n")
        injected = self.snapshot("notes.txt")
        run = self.refused_before_approval(
            self.edit_task("notes.txt", "after\n", injected), "completed local read"
        )
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_an_injected_snapshot_alongside_a_real_reference(self) -> None:
        """An overwrite must enter CONTEXT_RETRIEVAL carrying NO preimage."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        task = self.edit_task(
            "notes.txt", "after\n", self.snapshot("notes.txt"), read_task_id=read_id
        )
        self.refused_before_approval(task, "before the reference was resolved")
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_a_completed_non_file_read_source_is_refused(self) -> None:
        """A forged provenance: an ordinary completed task carrying a snapshot."""
        target = self.seed("notes.txt", "before\n")
        matching = self.snapshot("notes.txt")
        echo = DummyConnector(name="echo-source")
        app = open_app(
            settings=Settings(
                memory_database=self.root / "forged.db",
                permission_mode=PermissionMode.SMART_AUTO,
            ),
            connectors=(echo, self.reader, self.writer),
            destination_policy=_LOCAL_ROWS
            | frozenset(
                {
                    DestinationGrant(
                        "echo-source", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION
                    )
                }
            ),
        )
        source = Task(
            objective="pretend to be a read",
            capability="echo",
            scope=ActionScope(
                action="echo",
                target="anything",
                data_classification=DataClassification.INTERNAL,
            ),
        )
        run = app.core.run(source)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        source_id, session_id = source.id, run.session.id
        connection = sqlite3.connect(self.root / "forged.db")
        try:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (source_id,)
            ).fetchone()
            document = json.loads(row[0])
            document["local_read"] = matching.to_dict()
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), source_id),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(app.memory.load_task(source_id).local_read.present)
        task = self.edit_task("notes.txt", "after\n", read_task_id=source_id)
        failed = app.core.run(task, session_id=session_id)
        self.assertEqual(RunStatus.FAILED, failed.status)
        self.assertIs(ErrorCode.INVALID_TASK, failed.error.code)
        self.assertIn("not a file read", failed.error.message)
        self.assertEqual([], app.memory.execution_attempts(task.id))
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_a_source_produced_by_a_non_reader_connector_is_refused(self) -> None:
        """The producer gate, asked of the MANIFEST and never of a name."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        self.tamper(read_id, lambda row: row.__setitem__("selected_connector", "echo"))
        task = self.edit_task("notes.txt", "after\n", read_task_id=read_id)
        run = self.refused_before_approval(task, "not produced by a file reader")
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_an_incomplete_source_is_refused(self) -> None:
        """A read that did not complete is not evidence about anything."""
        target = self.seed("notes.txt", "before\n")
        failed = self.run_document(self.read_document("missing.txt")).payload
        self.assertEqual("failed", failed["task"]["status"])
        self.refused_before_approval(
            self.edit_task(
                "notes.txt", "after\n", read_task_id=failed["task"]["id"]
            ),
            "did not complete",
        )
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_a_cross_session_source_is_refused(self) -> None:
        target = self.seed("notes.txt", "before\n")
        first = self.completed_read("notes.txt")
        self.session_id = None  # a brand-new session for the write
        self.refused_before_approval(
            self.edit_task("notes.txt", "after\n", read_task_id=first),
            "another session",
        )
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_a_region_source_is_refused(self) -> None:
        target = self.seed("notes.txt", "one\ntwo\nthree\n")
        region_id = self.completed_read(
            "notes.txt", {"start_line": 1, "max_lines": 1}
        )
        self.refused_before_approval(
            self.edit_task("notes.txt", "after\n", read_task_id=region_id),
            "bounded region",
        )
        self.assertEqual("one\ntwo\nthree\n", target.read_text(encoding="utf-8"))

    def test_a_transitively_copied_snapshot_is_refused(self) -> None:
        """A consumer that legitimately CARRIES a snapshot is not a reader."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        carrier = Task(
            objective="analyse the file",
            capability="echo",
            scope=ActionScope(
                action="analyse",
                target="repo",
                data_classification=DataClassification.INTERNAL,
            ),
            context={LOCAL_READ_TASK_REFERENCE_KEY: read_id},
        )
        app = open_app(
            settings=self.settings,
            connectors=(DummyConnector(name="carrier"), self.reader, self.writer),
            destination_policy=_LOCAL_ROWS
            | frozenset(
                {
                    DestinationGrant(
                        "carrier", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION
                    )
                }
            ),
        )
        run = app.core.run(carrier, session_id=self.session_id)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        self.assertTrue(app.memory.load_task(carrier.id).local_read.present)
        task = self.edit_task("notes.txt", "after\n", read_task_id=carrier.id)
        failed = app.core.run(task, session_id=self.session_id)
        self.assertEqual(RunStatus.FAILED, failed.status)
        self.assertIn("not a file read", failed.error.message)
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_a_task_cannot_reference_its_own_read(self) -> None:
        self.seed("notes.txt", "before\n")
        task = self.edit_task("notes.txt", "after\n")
        task.context[LOCAL_READ_TASK_REFERENCE_KEY] = task.id
        self.refused_before_approval(task, "its own local read")

    # -- the relationship must survive persistence --------------------------

    def test_removing_the_reference_after_approval_refuses_on_resume(self) -> None:
        """Regression 2: a corrupted row cannot keep the snapshot and drop the
        reference that justified it."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        task, run = self.bound("notes.txt", "after\n", read_id)
        grant = PermissionGrant.for_request(run.permission_request)
        self.tamper(task.id, lambda row: row["context"].pop(
            LOCAL_READ_TASK_REFERENCE_KEY, None
        ))
        reloaded = self.memory.load_task(task.id)
        self.assertTrue(reloaded.local_read.present)
        self.assertNotIn(LOCAL_READ_TASK_REFERENCE_KEY, reloaded.context)
        decided = self.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.INVALID_TASK, decided.error.code)
        self.assertIn("must reference", decided.error.message)
        self.assertEqual([], self.memory.execution_attempts(task.id))
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_repointing_the_reference_after_approval_refuses_on_resume(self) -> None:
        """Regression 9: the old grant cannot reach the mutation."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        self.seed("other.txt", "before\n")
        other_id = self.completed_read("other.txt")
        task, run = self.bound("notes.txt", "after\n", read_id)
        grant = PermissionGrant.for_request(run.permission_request)
        self.tamper(
            task.id,
            lambda row: row["context"].__setitem__(
                LOCAL_READ_TASK_REFERENCE_KEY, other_id
            ),
        )
        decided = self.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.INVALID_TASK, decided.error.code)
        self.assertIn("referenced read's own snapshot", decided.error.message)
        self.assertEqual([], self.memory.execution_attempts(task.id))
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_swapping_the_bound_snapshot_after_approval_refuses(self) -> None:
        target, read_id = self.prepare_source("notes.txt", "before\n")
        task, run = self.bound("notes.txt", "after\n", read_id)
        grant = PermissionGrant.for_request(run.permission_request)
        forged = replace(self.snapshot("notes.txt"), excerpt="tampered\n")
        self.tamper(
            task.id, lambda row: row.__setitem__("local_read", forged.to_dict())
        )
        decided = self.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIn("referenced read's own snapshot", decided.error.message)
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_a_source_that_vanishes_after_approval_refuses(self) -> None:
        """The reference must still RESOLVE, not merely have resolved once.

        With the row gone the source cannot be shown to belong to this
        session, so the relation fails closed; which of the resolution steps
        notices first is not the point, and the message is only asserted to be
        one of this gate's.
        """
        target, read_id = self.prepare_source("notes.txt", "before\n")
        task, run = self.bound("notes.txt", "after\n", read_id)
        grant = PermissionGrant.for_request(run.permission_request)
        self.delete_task_row(read_id)
        decided = self.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.INVALID_TASK, decided.error.code)
        self.assertIn("Overwrite preimage is not usable", decided.error.message)
        self.assertEqual([], self.memory.execution_attempts(task.id))
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_create_is_never_asked_for_a_preimage(self) -> None:
        stored, _ = self.completed(
            self.edit_task("fresh.txt", "created\n", mode="create")
        )
        self.assertEqual(
            "created\n", (self.root / "fresh.txt").read_text(encoding="utf-8")
        )
        self.assertFalse(stored.local_read.present)


# ==========================================================================
# BASIC - the capability itself, always through the real binder
# ==========================================================================
@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class DirectOverwriteTests(EditTestCase):
    def test_a_bound_overwrite_replaces_the_whole_file(self) -> None:
        target, read_id = self.prepare_source("notes.txt", "before\n")
        stored, _ = self.completed_overwrite("notes.txt", "after the edit\n", read_id)
        self.assertEqual(b"after the edit\n", target.read_bytes())
        record = stored.local_write
        self.assertEqual("overwrite", record.mode)
        self.assertTrue(record.pre_present)
        self.assertEqual("written", record.status)

    def test_the_record_binds_both_attested_states_exactly(self) -> None:
        _, read_id = self.prepare_source("notes.txt", "before\n")
        source = self.memory.load_task(read_id).local_read
        stored, _ = self.completed_overwrite("notes.txt", "after\n", read_id)
        record = stored.local_write
        self.assertEqual(source.sha256, record.pre_sha256)
        self.assertEqual(source.byte_size, record.pre_byte_size)
        self.assertEqual(hashlib.sha256(b"after\n").hexdigest(), record.intended_sha256)
        self.assertEqual(6, record.intended_byte_size)

    def test_the_inode_is_preserved_because_the_write_is_in_place(self) -> None:
        """No temp file, no rename: identity, permissions and links survive."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        before = os.stat(target)
        self.completed_overwrite("notes.txt", "after\n", read_id)
        after = os.stat(target)
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    def test_no_temporary_is_created_on_the_edit_route(self) -> None:
        _, read_id = self.prepare_source("notes.txt", "before\n")
        self.completed_overwrite("notes.txt", "after\n", read_id)
        self.assertEqual([], sorted(p.name for p in self.root.glob(".jarvis-write-*")))

    def test_a_replacement_exactly_at_the_ceiling_succeeds(self) -> None:
        target, read_id = self.prepare_source("notes.txt", "small\n")
        content = "y" * (LOCAL_WRITE_MAX_BYTES - 1) + "\n"
        self.assertEqual(LOCAL_WRITE_MAX_BYTES, len(content.encode("utf-8")))
        self.completed_overwrite("notes.txt", content, read_id)
        self.assertEqual(LOCAL_WRITE_MAX_BYTES, target.stat().st_size)

    def test_a_replacement_one_byte_over_the_ceiling_is_refused(self) -> None:
        target, read_id = self.prepare_source("notes.txt", "small\n")
        content = "y" * (LOCAL_WRITE_MAX_BYTES + 1)
        run = self.refused_before_approval(
            self.edit_task("notes.txt", content, read_task_id=read_id), "ceiling"
        )
        self.assertIs(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertEqual("small\n", target.read_text(encoding="utf-8"))

    def test_a_preimage_over_the_ceiling_can_never_even_be_read(self) -> None:
        """Nothing can plan an overwrite of it: the HEAD read refuses first."""
        self.seed("big.txt", "z" * (LOCAL_WRITE_MAX_BYTES + 1))
        payload = self.run_document(self.read_document("big.txt")).payload
        self.assertEqual("failed", payload["task"]["status"])
        self.assertIn("read ceiling", payload["error"]["message"])

    def test_a_preimage_over_the_ceiling_is_refused_at_the_connector(self) -> None:
        """The ceiling itself, reached by driving the connector directly."""
        self.seed("big.txt", "z" * (LOCAL_WRITE_MAX_BYTES + 1))
        task = self.edit_task("big.txt", "small\n", read_task_id=UNRESOLVED)
        with self.assertRaises(AttestationError) as caught:
            self.writer.canonical_outbound(task)
        self.assertIn("already exceeds", str(caught.exception))

    def test_the_preimage_ceiling_is_exact(self) -> None:
        """Exactly at the ceiling binds; the +1 sentinel tells them apart and
        never becomes part of the digest."""
        target, read_id = self.prepare_source(
            "edge.txt", CEILING_LINE * CEILING_LINES
        )
        self.assertEqual(LOCAL_WRITE_MAX_BYTES, target.stat().st_size)
        self.assertEqual(
            LOCAL_WRITE_MAX_BYTES, self.memory.load_task(read_id).local_read.byte_size
        )
        self.completed_overwrite("edge.txt", "tiny\n", read_id)
        self.assertEqual("tiny\n", target.read_text(encoding="utf-8"))

    def test_verification_reads_the_destination_back_independently(self) -> None:
        _, read_id = self.prepare_source("notes.txt", "before\n")
        stored, _ = self.completed_overwrite("notes.txt", "after\n", read_id)
        result = self.core.memory.load_task(stored.id)
        self.assertEqual("written", result.local_write.status)
        self.assertIs(TaskStatus.COMPLETED, result.status)

    def test_a_unicode_replacement_lands_byte_for_byte(self) -> None:
        target, read_id = self.prepare_source("notes.txt", "before\n")
        content = "merhaba dünya\n# é 😀\n"
        self.completed_overwrite("notes.txt", content, read_id)
        self.assertEqual(content.encode("utf-8"), target.read_bytes())

    def test_crlf_content_is_written_verbatim_and_never_normalized(self) -> None:
        """The bytes are the deliverable; nothing rewrites them."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        self.completed_overwrite("notes.txt", "a\r\nb\r\n", read_id)
        self.assertEqual(b"a\r\nb\r\n", target.read_bytes())

    def test_a_truncated_or_redacted_excerpt_still_binds(self) -> None:
        """DIRECT overwrite does not require excerpt fidelity.

        The caller supplies the complete postimage explicitly, so nothing is
        reconstructed from a partial view. The excerpt is a display artefact;
        the binding is the whole-file DIGEST, exact either way.
        """
        target, read_id = self.prepare_source(
            "notes.txt", "api_key = 'AKIAIOSFODNN7EXAMPLE'\n" * 400
        )
        source = self.memory.load_task(read_id).local_read
        self.assertTrue(source.truncated)
        self.assertNotEqual(
            source.sha256, hashlib.sha256(source.excerpt.encode("utf-8")).hexdigest()
        )
        self.completed_overwrite("notes.txt", "cleaned\n", read_id)
        self.assertEqual("cleaned\n", target.read_text(encoding="utf-8"))

    def test_a_replanned_overwrite_after_a_landed_write_is_refused(self) -> None:
        """The preimage binding is also the replay guard: once the bytes have
        landed the file no longer matches the digest the plan was made
        against, so a repeat of the same plan fails closed on its own."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        self.completed_overwrite("notes.txt", "replaced\n", read_id)
        self.refused_before_approval(
            self.edit_task("notes.txt", "replaced\n", read_task_id=read_id),
            "changed since",
        )
        self.assertEqual("replaced\n", target.read_text(encoding="utf-8"))


# ==========================================================================
# PREIMAGE VALUE - what the connector itself can and cannot prove
# ==========================================================================
@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class PreimageValueTests(EditTestCase):
    """The connector compares a VALUE. Driven directly, with the reference
    standing in for the provenance the loop proves separately."""

    def refuse_at_intent(self, task: Task, fragment: str) -> None:
        with self.assertRaises(AttestationError) as caught:
            self.writer.canonical_outbound(task)
        self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)
        self.assertIn(fragment, str(caught.exception))
        self.assertIsNone(self.writer.outbound_preview(task))
        self.assertIsNone(self.writer.pre_execution_record(task))

    def test_a_missing_reference_is_refused_by_the_connector_too(self) -> None:
        self.seed("notes.txt", "before\n")
        self.refuse_at_intent(
            self.edit_task("notes.txt", "after\n", self.snapshot("notes.txt")),
            "context.local_read_task_id",
        )

    def test_a_missing_snapshot_is_refused(self) -> None:
        self.seed("notes.txt", "before\n")
        self.refuse_at_intent(
            self.edit_task("notes.txt", "after\n", read_task_id=UNRESOLVED),
            "none bound",
        )

    def test_a_region_snapshot_is_never_edit_authority(self) -> None:
        self.seed("notes.txt", "one\ntwo\nthree\n")
        region = self.snapshot("notes.txt", start_line=1, max_lines=1)
        self.assertEqual(1, region.start_line)
        self.refuse_at_intent(
            self.edit_task("notes.txt", "after\n", region, read_task_id=UNRESOLVED),
            "bounded REGION",
        )

    def test_a_snapshot_of_another_file_is_refused(self) -> None:
        self.seed("notes.txt", "before\n")
        self.seed("other.txt", "before\n")
        self.refuse_at_intent(
            self.edit_task(
                "notes.txt", "after\n", self.snapshot("other.txt"),
                read_task_id=UNRESOLVED,
            ),
            "different file",
        )

    def test_a_tampered_digest_is_refused(self) -> None:
        self.seed("notes.txt", "before\n")
        snapshot = replace(self.snapshot("notes.txt"), sha256="a" * 64)
        self.refuse_at_intent(
            self.edit_task(
                "notes.txt", "after\n", snapshot, read_task_id=UNRESOLVED
            ),
            "changed since",
        )

    def test_a_tampered_byte_size_is_refused(self) -> None:
        self.seed("notes.txt", "before\n")
        original = self.snapshot("notes.txt")
        snapshot = replace(original, byte_size=original.byte_size + 1)
        self.refuse_at_intent(
            self.edit_task(
                "notes.txt", "after\n", snapshot, read_task_id=UNRESOLVED
            ),
            "changed since",
        )

    def test_a_structurally_malformed_snapshot_is_refused(self) -> None:
        self.seed("notes.txt", "before\n")
        snapshot = replace(self.snapshot("notes.txt"), read_at="not a timestamp")
        self.refuse_at_intent(
            self.edit_task(
                "notes.txt", "after\n", snapshot, read_task_id=UNRESOLVED
            ),
            "structurally valid",
        )

    def test_an_absent_overwrite_target_is_refused_as_a_contradiction(self) -> None:
        """Mode and prior state are ONE authority: an overwrite of nothing is
        never quietly downgraded into a create."""
        self.refuse_at_intent(
            self.edit_task("missing.txt", "after\n", read_task_id=UNRESOLVED),
            "state changed",
        )
        self.assertFalse((self.root / "missing.txt").exists())

    # -- the same questions, through the real loop --------------------------

    def test_a_file_changed_after_the_read_is_refused(self) -> None:
        target, read_id = self.prepare_source("notes.txt", "before\n")
        target.write_text("somebody else edited this\n", encoding="utf-8", newline="")
        self.refused_before_approval(
            self.edit_task("notes.txt", "after\n", read_task_id=read_id),
            "changed since",
        )
        self.assertEqual(
            "somebody else edited this\n", target.read_text(encoding="utf-8")
        )

    def test_a_file_deleted_after_the_read_is_refused(self) -> None:
        target, read_id = self.prepare_source("notes.txt", "before\n")
        target.unlink()
        self.refused_before_approval(
            self.edit_task("notes.txt", "after\n", read_task_id=read_id),
            "state changed",
        )
        self.assertFalse(target.exists())

    def test_a_file_replaced_after_the_read_is_refused(self) -> None:
        target, read_id = self.prepare_source("notes.txt", "before\n")
        target.unlink()
        target.write_text("a different file entirely\n", encoding="utf-8", newline="")
        self.refused_before_approval(
            self.edit_task("notes.txt", "after\n", read_task_id=read_id),
            "changed since",
        )
        self.assertEqual(
            "a different file entirely\n", target.read_text(encoding="utf-8")
        )

    def test_a_read_of_another_file_is_refused_through_the_loop(self) -> None:
        self.seed("notes.txt", "before\n")
        self.seed("other.txt", "before\n")
        other_id = self.completed_read("other.txt")
        self.refused_before_approval(
            self.edit_task("notes.txt", "after\n", read_task_id=other_id),
            "different file",
        )


# ==========================================================================
# PLATFORM POLICY - overwrite exists only where the entry can be pinned
# ==========================================================================
class PlatformPolicyTests(EditTestCase):
    """Runs on EVERY platform: the flag is what is under test, not the OS."""

    def test_the_flag_is_derived_and_never_hardcoded(self) -> None:
        """On a host with no handle-install primitive the flag MUST be false.

        That cannot be observed from a host that HAS the primitive, so the
        derivation itself is pinned as well as its current value: a hardcoded
        ``True`` would silently enable an overwrite where the approved
        directory entry cannot be pinned, and no runtime assertion available
        here would notice.
        """
        source = Path(local_files.__file__).read_text(encoding="utf-8")
        self.assertIn("_ENTRY_PINNED_OVERWRITE = _WINDOWS_HANDLE_INSTALL", source)
        self.assertEqual(
            local_files._WINDOWS_HANDLE_INSTALL, local_files._ENTRY_PINNED_OVERWRITE
        )

    def test_an_unpinnable_platform_refuses_before_any_approval(self) -> None:
        target = self.seed("notes.txt", "before\n")
        read_id = self.completed_read("notes.txt")
        task = self.edit_task("notes.txt", "after\n", read_task_id=read_id)
        with mock.patch.object(local_files, "_ENTRY_PINNED_OVERWRITE", False):
            run = self.refused_before_approval(
                task, "cannot pin the approved directory entry"
            )
        self.assertIs(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_an_unpinnable_platform_cannot_attest_or_preview(self) -> None:
        self.seed("notes.txt", "before\n")
        task = self.edit_task(
            "notes.txt", "after\n", self.snapshot("notes.txt"),
            read_task_id=UNRESOLVED,
        )
        with mock.patch.object(local_files, "_ENTRY_PINNED_OVERWRITE", False):
            with self.assertRaises(AttestationError) as caught:
                self.writer.canonical_outbound(task)
            self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)
            self.assertIsNone(self.writer.outbound_preview(task))
            self.assertIsNone(self.writer.pre_execution_record(task))

    @unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
    def test_an_unpinnable_platform_never_performs_the_mutation(self) -> None:
        """Even driving the connector directly past every earlier gate."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        task, _ = self.bound("notes.txt", "after\n", read_id)
        self.prepared(task)
        with mock.patch.object(local_files, "_ENTRY_PINNED_OVERWRITE", False):
            result = self.writer.execute(task, SessionState())
        self.assertIsNotNone(result.error)
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_create_is_untouched_on_an_unpinnable_platform(self) -> None:
        with mock.patch.object(local_files, "_ENTRY_PINNED_OVERWRITE", False):
            stored, _ = self.completed(
                self.edit_task("fresh.txt", "created\n", mode="create")
            )
        self.assertEqual(
            "created\n", (self.root / "fresh.txt").read_text(encoding="utf-8")
        )
        self.assertEqual("create", stored.local_write.mode)
        self.assertFalse(stored.local_write.pre_present)

    def test_reads_are_untouched_on_an_unpinnable_platform(self) -> None:
        self.seed("notes.txt", "before\n")
        with mock.patch.object(local_files, "_ENTRY_PINNED_OVERWRITE", False):
            snapshot = self.snapshot("notes.txt")
        self.assertEqual("notes.txt", snapshot.relative_path)
        self.assertEqual(7, snapshot.byte_size)


# ==========================================================================
# VOLUME ELIGIBILITY - and the order it may be asked in
# ==========================================================================
@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class VolumeEligibilityTests(EditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.target, self.read_id = self.prepare_source("notes.txt", "before\n")

    def task(self) -> Task:
        return self.edit_task("notes.txt", "after\n", read_task_id=self.read_id)

    def unchanged(self) -> None:
        self.assertEqual("before\n", self.target.read_text(encoding="utf-8"))

    def test_the_real_temp_volume_is_eligible(self) -> None:
        """The control: without this, every refusal below proves nothing."""
        self.assertIsNone(local_files._volume_refusal(self.target))

    def test_a_removable_volume_is_refused(self) -> None:
        with mock.patch.object(
            local_files, "_windows_volume_facts", lambda path: (2, "NTFS")
        ):
            self.refused_before_approval(self.task(), "local fixed NTFS")
        self.unchanged()

    def test_a_network_volume_is_refused(self) -> None:
        with mock.patch.object(
            local_files, "_windows_volume_facts", lambda path: (4, "NTFS")
        ):
            self.refused_before_approval(self.task(), "local fixed NTFS")
        self.unchanged()

    def test_a_non_ntfs_filesystem_is_refused(self) -> None:
        for filesystem in ("ReFS", "FAT32", "exFAT", "DrvFs", ""):
            with self.subTest(filesystem=filesystem):
                with mock.patch.object(
                    local_files,
                    "_windows_volume_facts",
                    lambda path, name=filesystem: (3, name),
                ):
                    self.refused_before_approval(self.task(), "local fixed NTFS")
                self.unchanged()

    def test_an_unidentifiable_volume_is_refused(self) -> None:
        with mock.patch.object(
            local_files, "_windows_volume_facts", lambda path: None
        ):
            self.refused_before_approval(self.task(), "could not be identified")
        self.unchanged()

    def test_a_unc_anchor_is_refused_without_asking_the_operating_system(self) -> None:
        """P2. Deciding eligibility must never itself be network activity."""
        calls: list[Path] = []

        def spy(path: Path):
            calls.append(path)
            return (3, "NTFS")

        with mock.patch.object(local_files, "_windows_volume_facts", spy):
            remote = local_files._volume_refusal(Path(r"\\server\share\notes.txt"))
            anchorless = local_files._volume_refusal(Path("relative/notes.txt"))
        self.assertIn("local fixed NTFS", remote or "")
        self.assertIn("local fixed NTFS", anchorless or "")
        self.assertEqual([], calls)

    def test_the_handle_recheck_refuses_at_execution_time(self) -> None:
        """The approval-time check asked a pathname. This one asks the object
        about to be rewritten, so a volume swapped underneath the name after
        approval is caught with no bytes moved."""
        task, _ = self.bound("notes.txt", "after\n", self.read_id)
        self.prepared(task)
        with mock.patch.object(
            local_files, "_windows_handle_filesystem", lambda descriptor: "ReFS"
        ):
            result = self.writer.execute(task, SessionState())
        self.assertIsNotNone(result.error)
        self.assertIn("local fixed NTFS", result.summary)
        self.unchanged()

    def test_an_unanswerable_handle_recheck_refuses(self) -> None:
        task, _ = self.bound("notes.txt", "after\n", self.read_id)
        self.prepared(task)
        with mock.patch.object(
            local_files, "_windows_handle_filesystem", lambda descriptor: None
        ):
            result = self.writer.execute(task, SessionState())
        self.assertIsNotNone(result.error)
        self.unchanged()


@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class VolumeCallOrderTests(EditTestCase):
    """P2. Nothing may inspect the target's volume until the no-follow
    root/parent/reparse-chain policy has accepted the path."""

    def setUp(self) -> None:
        super().setUp()
        self.target, self.read_id = self.prepare_source("notes.txt", "before\n")
        self.calls: list[Path] = []

    def spy(self, path: Path):
        self.calls.append(path)
        return (3, "NTFS")

    def test_the_volume_is_never_queried_when_the_chain_is_refused(self) -> None:
        """The seam is ``_reject_reparse_chain``, driven to refuse the way an
        attacker-controlled junction below the root would make it refuse."""

        def refuse_chain(connector, root, parts):
            raise local_files._WriteRefused(local_files._WRITE_NOT_REGULAR)

        task, _ = self.bound("notes.txt", "after\n", self.read_id)
        self.calls.clear()
        with mock.patch.object(local_files, "_windows_volume_facts", self.spy):
            with mock.patch.object(
                local_files._RootBoundLocalConnector,
                "_reject_reparse_chain",
                refuse_chain,
            ):
                with self.assertRaises(AttestationError) as caught:
                    self.writer.canonical_outbound(task)
        self.assertEqual(
            [], self.calls, msg="the volume was queried before the chain refused"
        )
        self.assertIn("regular file", str(caught.exception))
        self.assertEqual("before\n", self.target.read_text(encoding="utf-8"))

    def test_an_accepted_chain_still_reaches_volume_validation(self) -> None:
        """The control: the order must not simply have removed the check."""
        task, _ = self.bound("notes.txt", "after\n", self.read_id)
        self.calls.clear()
        with mock.patch.object(local_files, "_windows_volume_facts", self.spy):
            self.writer.canonical_outbound(task)
        self.assertNotEqual([], self.calls)

    def test_a_volume_failure_after_an_accepted_chain_still_fails_closed(self) -> None:
        task, _ = self.bound("notes.txt", "after\n", self.read_id)
        with mock.patch.object(
            local_files, "_windows_volume_facts", lambda path: None
        ):
            with self.assertRaises(AttestationError) as caught:
                self.writer.canonical_outbound(task)
        self.assertIn("could not be identified", str(caught.exception))

    def test_create_never_queries_the_volume_at_all(self) -> None:
        task = self.edit_task("fresh.txt", "created\n", mode="create")
        with mock.patch.object(local_files, "_windows_volume_facts", self.spy):
            self.writer.canonical_outbound(task)
        self.assertEqual([], self.calls)


# ==========================================================================
# WINDOWS ENTRY PINNING - the OS sharing contract, exercised for real
# ==========================================================================
@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class EntryPinningTests(EditTestCase):
    """Every assertion of record here is that a HOSTILE ACT WAS REFUSED.

    A test asserting instead that our write still landed on the held object
    would be asserting the authority violation, not the guarantee.
    """

    def setUp(self) -> None:
        super().setUp()
        self.target = self.seed("foo.py", "approved\n")
        self._outside = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self._outside)
        self.outside = Path(self._outside.name).resolve()

    def pin(self):
        """Hold the leaf exactly as the connector's edit route holds it."""
        parent_handle = self.writer._open_windows_parent(self.root)
        descriptor = local_files._windows_open_in_parent(parent_handle, "foo.py")
        held = [True]

        def release() -> None:
            if not held:
                return
            held.clear()
            os.close(descriptor)
            local_files._windows_close_handle(parent_handle)

        self.addCleanup(release)
        return descriptor, release

    def test_a_rename_inside_the_root_is_blocked(self) -> None:
        self.pin()
        with self.assertRaises(OSError):
            os.rename(self.target, self.root / "moved.py")
        self.assertTrue(self.target.exists())

    def test_a_rename_out_of_the_allowed_root_is_blocked(self) -> None:
        """The case that would otherwise put approved bytes outside the roots."""
        self.pin()
        with self.assertRaises(OSError):
            os.rename(self.target, self.outside / "foo.py")
        self.assertTrue(self.target.exists())
        self.assertFalse((self.outside / "foo.py").exists())

    def test_an_unlink_is_blocked(self) -> None:
        self.pin()
        with self.assertRaises(OSError):
            os.unlink(self.target)
        self.assertTrue(self.target.exists())

    def test_a_third_party_write_open_is_blocked(self) -> None:
        self.pin()
        with self.assertRaises(OSError):
            open(self.target, "r+b").close()

    def test_a_delete_access_open_is_blocked(self) -> None:
        """DELETE access is what rename and delete both need."""
        self.pin()
        self.assertFalse(open_with_delete_access(self.target))

    def test_substituting_a_new_file_at_the_leaf_is_blocked(self) -> None:
        self.pin()
        with self.assertRaises(OSError):
            open(self.target, "x").close()

    def test_the_control_proves_the_handle_is_what_blocks_them(self) -> None:
        """Without this, every assertion above could pass for another reason."""
        _, release = self.pin()
        release()
        os.rename(self.target, self.root / "moved.py")
        self.assertTrue((self.root / "moved.py").exists())
        os.rename(self.root / "moved.py", self.target)
        self.assertTrue(open_with_delete_access(self.target))

    def test_a_pre_existing_writer_makes_our_own_open_fail_closed(self) -> None:
        """The sharing check runs BOTH ways, which is what makes 'our open
        succeeded' itself the proof that no conflicting handle existed."""
        holder = open(self.target, "r+b")
        self.addCleanup(holder.close)
        parent_handle = self.writer._open_windows_parent(self.root)
        self.addCleanup(local_files._windows_close_handle, parent_handle)
        with self.assertRaises(local_files._WriteRefused) as caught:
            local_files._windows_open_in_parent(parent_handle, "foo.py")
        self.assertIn("conflicting", caught.exception.reason)

    def test_a_file_held_elsewhere_for_writing_refuses_the_overwrite(self) -> None:
        read_id = self.completed_read("foo.py")
        task, run = self.bound("foo.py", "replaced\n", read_id)
        holder = open(self.target, "r+b")
        self.addCleanup(holder.close)
        final = self.approve(task, run)
        self.assertEqual(RunStatus.FAILED, final.status)
        self.assertIn("conflicting", final.error.message)
        self.assertEqual("approved\n", self.target.read_text(encoding="utf-8"))

    def test_an_absent_leaf_is_never_created_by_the_leaf_open(self) -> None:
        parent_handle = self.writer._open_windows_parent(self.root)
        self.addCleanup(local_files._windows_close_handle, parent_handle)
        with self.assertRaises(local_files._TargetMoved):
            local_files._windows_open_in_parent(parent_handle, "absent.py")
        self.assertFalse((self.root / "absent.py").exists())


# ==========================================================================
# THE MUTATION WINDOW - what happens between the checks and the syscall
# ==========================================================================
@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class MutationProbeTests(EditTestCase):
    def test_a_file_changed_after_approval_is_refused(self) -> None:
        """The window ``_intent`` alone cannot close.

        ``execute`` re-derives the intent and compares it with the durable
        approved record, but that derivation still asks a NAME. Between it and
        the pinned open there is a gap, and the pre-state is therefore
        digested again through the descriptor that is about to be rewritten.
        The change is injected exactly in that gap.
        """
        target, read_id = self.prepare_source("notes.txt", "before\n")
        task, _ = self.bound("notes.txt", "after\n", read_id)
        self.prepared(task)
        real = local_files._windows_open_in_parent

        def sneak(parent_handle, leaf):
            target.write_text("third party\n", encoding="utf-8", newline="")
            return real(parent_handle, leaf)

        with mock.patch.object(local_files, "_windows_open_in_parent", sneak):
            result = self.writer.execute(task, SessionState())
        self.assertIsNotNone(result.error)
        self.assertIn("state changed", result.summary)
        self.assertEqual("third party\n", target.read_text(encoding="utf-8"))

    def test_the_mutation_opens_no_pathname_at_all(self) -> None:
        """The rewrite goes through the descriptor that proved the pre-state.

        Any pathname reopen between the verification and the mutation would
        reintroduce exactly the substitution window the pinned handle exists
        to close, so the count of path-based opens inside the route is
        asserted to be zero rather than merely assumed.
        """
        target, read_id = self.prepare_source("notes.txt", "before\n")
        task, _ = self.bound("notes.txt", "after\n", read_id)
        self.prepared(task)
        opened: list[object] = []
        real_open = os.open
        real_route = LocalFileWriteConnector._inplace_overwrite

        def counting(path, *args, **kwargs):
            opened.append(path)
            return real_open(path, *args, **kwargs)

        def traced(connector, intent):
            with mock.patch.object(local_files.os, "open", counting):
                return real_route(connector, intent)

        failure = None
        try:
            with mock.patch.object(
                LocalFileWriteConnector, "_inplace_overwrite", traced
            ):
                result = self.writer.execute(task, SessionState())
        except OSError as exc:  # a pathname reopen can also just fail
            failure, result = exc, None
        self.assertEqual([], opened, msg=f"the route opened a pathname: {opened}")
        self.assertIsNone(failure)
        self.assertIsNone(result.error, msg=str(result.summary))
        self.assertEqual("after\n", target.read_text(encoding="utf-8"))


# ==========================================================================
# NAMESPACE AUTHORITY - the load-bearing property of the whole milestone
# ==========================================================================
@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class NamespaceAuthorityTests(EditTestCase):
    def test_the_approved_path_still_names_the_approved_object(self) -> None:
        """The approval names project/foo.py. While the edit handle is held, a
        third party tries to move that file somewhere else.

        SUCCESS for JARVIS is that the HOSTILE RENAME IS REFUSED. A descriptor
        alone would pin only the inode, and a write through it would then land
        at a path no approval ever named - possibly outside the allowed roots.
        Authority here is (object AND path), so both must still hold at the
        instant of mutation.
        """
        project = self.root / "project"
        project.mkdir()
        target = project / "foo.py"
        target.write_text("approved\n", encoding="utf-8", newline="")
        read_id = self.completed_read("project/foo.py")
        self.assertEqual(
            "project/foo.py", self.memory.load_task(read_id).local_read.relative_path
        )

        parent_handle = self.writer._open_windows_parent(project)
        descriptor = local_files._windows_open_in_parent(parent_handle, "foo.py")
        try:
            outside_root = self.root / "elsewhere"
            outside_root.mkdir()
            with self.assertRaises(OSError):
                os.rename(target, project / "renamed.py")
            with self.assertRaises(OSError):
                os.rename(target, outside_root / "foo.py")
            with self.assertRaises(OSError):
                os.unlink(target)
            self.assertFalse(open_with_delete_access(target))
        finally:
            os.close(descriptor)
            local_files._windows_close_handle(parent_handle)

        # And the approved edit then lands at the approved path, not anywhere
        # a rename could have moved it to.
        self.completed_overwrite("project/foo.py", "replaced\n", read_id)
        self.assertEqual("replaced\n", target.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "elsewhere" / "foo.py").exists())
        self.assertFalse((project / "renamed.py").exists())


# ==========================================================================
# MODELANSWER -> OVERWRITE - deferred, for every connector alike
# ==========================================================================
class ModelAnswerDeferralTests(unittest.TestCase):
    """Provider-neutral: the refusal is keyed on durable JARVIS task fields."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self._temp)
        self.root = Path(self._temp.name).resolve()

    def decode(self, write: dict) -> Task:
        return decode_task_json(
            json.dumps(
                {
                    "objective": "write a file",
                    "capability": LOCAL_WRITE_CAPABILITY,
                    "scope": {"action": "write", "target": "notes.txt"},
                    "write": write,
                }
            )
        )

    def build(self, producer):
        settings = Settings(
            memory_database=self.root / f"{producer.manifest.name}.db",
            permission_mode=PermissionMode.SESSION,
        )
        writer = LocalFileWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        app = open_app(
            settings=settings,
            connectors=(producer, writer),
            destination_policy=_LOCAL_ROWS
            | frozenset(
                {
                    DestinationGrant(
                        producer.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )
        return app, writer

    def completed_source(self, app, snapshot: LocalReadSnapshot | None = None):
        task = Task(
            objective="propose a replacement",
            capability="echo",
            scope=ActionScope(
                action="analyse",
                target="repo",
                # The producer's manifest declares an external egress, so the
                # scope must name a destination Core actually registered.
                destination=CLAUDE_CODE_DESTINATION,
                data_classification=DataClassification.INTERNAL,
            ),
        )
        if snapshot is not None:
            task.local_read = snapshot
        run = app.core.run(task)
        if run.status is RunStatus.AWAITING_APPROVAL:
            grant = PermissionGrant.for_request(run.permission_request)
            run = app.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        stored = app.memory.load_task(task.id)
        self.assertTrue(stored.model_answer.present)
        return stored, run.session.id

    def handoff_task(self, source_id: str, target: Path, mode: str) -> Task:
        task = Task(
            objective="write the answer",
            capability=LOCAL_WRITE_CAPABILITY,
            scope=ActionScope(
                action="write",
                target=str(target),
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )
        task.write_mode = mode
        task.write_source = WriteSource.MODEL_ANSWER
        task.write_source_task_id = source_id
        return task

    # -- the typed input boundary ------------------------------------------

    def test_overwrite_plus_a_model_answer_reference_never_becomes_a_task(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode({"mode": "overwrite", LOCAL_WRITE_SOURCE_KEY: "1" * 36})
        message = str(caught.exception)
        self.assertIn("write.mode", message)
        self.assertIn("may not replace", message)

    def test_overwrite_with_direct_content_still_decodes(self) -> None:
        task = self.decode({"mode": "overwrite", "content": "body\n"})
        self.assertEqual("overwrite", task.write_mode)
        self.assertIs(WriteSource.DIRECT, task.write_source)

    def test_create_plus_a_model_answer_reference_still_decodes(self) -> None:
        task = self.decode({"mode": "create", LOCAL_WRITE_SOURCE_KEY: "1" * 36})
        self.assertIs(WriteSource.MODEL_ANSWER, task.write_source)

    # -- the durable identity gate -----------------------------------------

    def test_a_hostile_connector_never_authorizes_an_overwrite(self) -> None:
        """The connector's answer is valid, its source task carries the exact
        complete-view snapshot, and its outbound bytes provably never
        contained the file. The overwrite is refused."""
        app, _ = self.build(OmittingModelConnector())
        target = self.root / "foo.py"
        target.write_text("approved\n", encoding="utf-8", newline="")
        reader = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        read = Task(
            objective="read",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(
                action="read",
                target=str(target),
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )
        snapshot = reader.execute(read, SessionState()).data["local_read"]
        source, session_id = self.completed_source(app, snapshot)
        task = self.handoff_task(source.id, target, "overwrite")
        run = app.core.run(task, session_id=session_id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("may not overwrite", run.error.message)
        self.assertEqual([], app.memory.execution_attempts(task.id))
        self.assertEqual("approved\n", target.read_text(encoding="utf-8"))

    def test_an_honest_connector_is_refused_identically(self) -> None:
        """The point of the deferral: JARVIS cannot tell the two apart from
        durable state, so it refuses both rather than pretending it detected
        the bad one."""
        app, _ = self.build(IncludingModelConnector())
        target = self.root / "bar.py"
        target.write_text("approved\n", encoding="utf-8", newline="")
        source, session_id = self.completed_source(app)
        task = self.handoff_task(source.id, target, "overwrite")
        run = app.core.run(task, session_id=session_id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("may not overwrite", run.error.message)
        self.assertEqual([], app.memory.execution_attempts(task.id))
        self.assertEqual("approved\n", target.read_text(encoding="utf-8"))

    def test_create_still_works_through_both_connectors(self) -> None:
        for producer in (OmittingModelConnector(), IncludingModelConnector()):
            with self.subTest(producer=producer.manifest.name):
                app, _ = self.build(producer)
                target = self.root / f"{producer.manifest.name}.txt"
                source, session_id = self.completed_source(app)
                task = self.handoff_task(source.id, target, "create")
                run = app.core.run(task, session_id=session_id)
                self.assertEqual(
                    RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error)
                )
                grant = PermissionGrant.for_request(run.permission_request)
                run = app.core.resume(run.session.id, task.id, grant=grant)
                self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
                self.assertEqual(
                    source.model_answer.content, target.read_text(encoding="utf-8")
                )

    def test_the_refusal_names_no_connector(self) -> None:
        """Provider neutrality, asserted rather than assumed."""
        app, _ = self.build(OmittingModelConnector())
        source, session_id = self.completed_source(app)
        task = self.handoff_task(source.id, self.root / "x.py", "overwrite")
        run = app.core.run(task, session_id=session_id)
        for name in ("omitting-model", "claude", "including-model", "openai"):
            self.assertNotIn(name, run.error.message.lower())


# ==========================================================================
# CREATE COMPATIBILITY - nothing about create moved
# ==========================================================================
class CreateCompatibilityTests(EditTestCase):
    def test_the_create_preview_strings_and_fields_are_unchanged(self) -> None:
        task = self.edit_task("fresh.txt", "created\n", mode="create")
        preview = self.writer.outbound_preview(task)
        self.assertEqual(("path", "mode", "byte_size", "sha256"), preview.field_names)
        self.assertEqual("local write fresh.txt: create 8 bytes", preview.summary)

    @unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
    def test_the_overwrite_preview_names_the_bytes_being_destroyed(self) -> None:
        _, read_id = self.prepare_source("notes.txt", "before\n")
        source = self.memory.load_task(read_id).local_read
        task, _ = self.bound("notes.txt", "after\n", read_id)
        preview = self.writer.outbound_preview(task)
        self.assertEqual(
            (
                "path",
                "mode",
                "byte_size",
                "sha256",
                "pre_byte_size",
                "pre_sha256",
            ),
            preview.field_names,
        )
        self.assertIn(f"was 7, sha256 {source.sha256}", preview.summary)

    def test_create_still_refuses_an_existing_target(self) -> None:
        target = self.seed("notes.txt", "already here\n")
        self.refused_before_approval(
            self.edit_task("notes.txt", "new\n", mode="create"), "state changed"
        )
        self.assertEqual("already here\n", target.read_text(encoding="utf-8"))

    def test_a_create_still_installs_atomically_from_a_handle(self) -> None:
        self.completed(self.edit_task("fresh.txt", "created\n", mode="create"))
        self.assertEqual(
            "created\n", (self.root / "fresh.txt").read_text(encoding="utf-8")
        )
        self.assertEqual([], sorted(p.name for p in self.root.glob(".jarvis-write-*")))
        self.assertEqual(1, (self.root / "fresh.txt").stat().st_nlink)

    @unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
    def test_an_overwrite_can_never_enter_the_create_installer(self) -> None:
        """The guard names the ONE mode it serves, so widening the policy
        tuple can never route an edit into an installer whose whole guarantee
        is the destination's absence."""
        target, read_id = self.prepare_source("notes.txt", "before\n")
        task, _ = self.bound("notes.txt", "after\n", read_id)
        intent = self.writer._intent(task)
        with self.assertRaises(local_files._WriteRefused) as caught:
            self.writer._atomic_write(intent)
        self.assertIn("create installer", caught.exception.reason)
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_a_create_can_never_enter_the_overwrite_route(self) -> None:
        task = self.edit_task("fresh.txt", "created\n", mode="create")
        intent = self.writer._intent(task)
        with self.assertRaises(local_files._WriteRefused):
            self.writer._inplace_overwrite(intent)
        self.assertFalse((self.root / "fresh.txt").exists())

    def test_the_policy_tuple_is_platform_neutral(self) -> None:
        self.assertEqual(("create", "overwrite"), LOCAL_WRITE_ENABLED_MODES)
        self.assertEqual(("create", "overwrite"), LOCAL_WRITE_MODES)


# ==========================================================================
# H2 - an overwrite in doubt is never replayed
# ==========================================================================
@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class UnknownOutcomeTests(EditTestCase):
    def in_doubt(self, connector_class, content: str = "replaced\n"):
        target = self.seed("notes.txt", "before\n")
        connector = connector_class(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        reader = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        app = open_app(
            settings=Settings(
                memory_database=self.root / f"{connector.manifest.name}.db",
                permission_mode=PermissionMode.SMART_AUTO,
            ),
            connectors=(reader, connector),
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        "local-file-read", "file_read", CLAUDE_CODE_DESTINATION
                    ),
                    DestinationGrant(
                        connector.manifest.name,
                        "file_write",
                        CLAUDE_CODE_DESTINATION,
                    ),
                }
            ),
        )
        read = app.run(json.dumps(self.read_document("notes.txt"))).payload
        self.assertEqual("completed", read["task"]["status"], read.get("error"))
        session_id, read_id = read["session"]["id"], read["task"]["id"]
        task = self.edit_task("notes.txt", content, read_task_id=read_id)
        run = app.core.run(task, session_id=session_id)
        self.assertEqual(
            RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error)
        )
        grant = PermissionGrant.for_request(run.permission_request)
        decided = app.core.resume(run.session.id, task.id, grant=grant)
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, decided.error.code)
        return app, task, run.session.id, decided, target

    def test_a_raise_after_the_mutation_is_unknown_and_diagnosed_as_landed(self) -> None:
        app, task, _, decided, target = self.in_doubt(WroteThenRaised)
        attempt = app.memory.latest_execution_attempt(task.id)
        self.assertIs(AttemptState.UNKNOWN_OUTCOME, attempt.state)
        self.assertEqual(
            local_files.LOCAL_WRITE_LANDED,
            decided.error.details.get("local_write_diagnosis"),
        )
        self.assertEqual("replaced\n", target.read_text(encoding="utf-8"))

    def test_a_raise_before_the_mutation_diagnoses_as_did_not_land(self) -> None:
        _, _, _, decided, target = self.in_doubt(RaisedBeforeWriting)
        self.assertEqual(
            local_files.LOCAL_WRITE_DID_NOT_LAND,
            decided.error.details.get("local_write_diagnosis"),
        )
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))

    def test_a_partial_or_third_party_state_diagnoses_as_ambiguous(self) -> None:
        """The documented overloading of the third label: for an overwrite it
        may also mean the in-place rewrite was interrupted."""
        app, task, session_id, _, target = self.in_doubt(WroteThenRaised)
        target.write_text("neither state\n", encoding="utf-8", newline="")
        again = app.core.resume(session_id, task.id)
        self.assertEqual(
            local_files.LOCAL_WRITE_AMBIGUOUS,
            again.error.details.get("local_write_diagnosis"),
        )

    def test_a_resume_never_performs_a_second_overwrite(self) -> None:
        app, task, session_id, _, target = self.in_doubt(WroteThenRaised)
        calls = []
        original = LocalFileWriteConnector._inplace_overwrite

        def counting(connector, intent):
            calls.append(intent.relative_path)
            return original(connector, intent)

        with mock.patch.object(
            LocalFileWriteConnector, "_inplace_overwrite", counting
        ):
            again = app.core.resume(session_id, task.id)
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, again.error.code)
        self.assertEqual([], calls)
        self.assertEqual("replaced\n", target.read_text(encoding="utf-8"))

    def test_the_prepared_authority_is_durable_before_the_boundary(self) -> None:
        app, task, _, _, _ = self.in_doubt(RaisedBeforeWriting)
        stored = app.memory.load_task(task.id)
        self.assertEqual("overwrite", stored.local_write.mode)
        self.assertTrue(stored.local_write.pre_present)
        self.assertEqual("prepared", stored.local_write.status)


# ==========================================================================
# THE PUBLIC DOCUMENT SHAPE - the real M3 hop, end to end
# ==========================================================================
@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class PublicShapeTests(EditTestCase):
    def edit_document(self, name: str, content: str, read_task_id: str) -> dict:
        return {
            "objective": f"replace {name}",
            "capability": LOCAL_WRITE_CAPABILITY,
            "scope": {"action": "write", "target": str(self.root / name)},
            "context": {
                "working_directory": str(self.root),
                LOCAL_READ_TASK_REFERENCE_KEY: read_task_id,
            },
            "write": {"mode": "overwrite", "content": content},
        }

    def test_the_documented_request_shape_completes(self) -> None:
        target = self.seed("notes.txt", "before\n")
        read_id = self.completed_read("notes.txt")
        payload = self.run_document(
            self.edit_document("notes.txt", "after the edit\n", read_id)
        ).payload
        approval = payload["approval"]
        final = self.app.approve(
            session_id=payload["session"]["id"],
            task_id=payload["task"]["id"],
            request_id=approval["request_id"],
            fingerprint=approval["fingerprint"],
        ).payload
        self.assertEqual("completed", final["task"]["status"], final.get("error"))
        self.assertEqual("after the edit\n", target.read_text(encoding="utf-8"))
        block = final["local_write"]
        self.assertEqual("overwrite", block["mode"])
        self.assertTrue(block["pre_present"])

    def test_the_write_always_requires_approval(self) -> None:
        self.seed("notes.txt", "before\n")
        read_id = self.completed_read("notes.txt")
        payload = self.run_document(
            self.edit_document("notes.txt", "after\n", read_id)
        ).payload
        self.assertEqual("awaiting_approval", payload["task"]["status"])
        self.assertIsNotNone(payload.get("approval"))
        self.assertEqual("before\n", (self.root / "notes.txt").read_text("utf-8"))

    def test_a_region_read_reference_is_refused_through_the_loop(self) -> None:
        target = self.seed("notes.txt", "one\ntwo\nthree\n")
        read_id = self.completed_read("notes.txt", {"start_line": 1, "max_lines": 1})
        payload = self.run_document(
            self.edit_document("notes.txt", "after\n", read_id)
        ).payload
        self.assertEqual("failed", payload["task"]["status"])
        self.assertIn("bounded region", payload["error"]["message"])
        self.assertEqual("one\ntwo\nthree\n", target.read_text(encoding="utf-8"))

    def test_the_stored_task_keeps_the_read_provenance(self) -> None:
        self.seed("notes.txt", "before\n")
        read_id = self.completed_read("notes.txt")
        payload = self.run_document(
            self.edit_document("notes.txt", "after\n", read_id)
        ).payload
        stored = self.memory.load_task(payload["task"]["id"])
        self.assertEqual("notes.txt", stored.local_read.relative_path)
        self.assertEqual(0, stored.local_read.start_line)
        self.assertEqual(read_id, stored.context[LOCAL_READ_TASK_REFERENCE_KEY])
        self.assertIs(TaskStatus.AWAITING_APPROVAL, stored.status)

    def test_the_schemas_are_unchanged(self) -> None:
        from jarvis.app import OUTPUT_SCHEMA_VERSION
        from jarvis.connectors.claude_code import EGRESS_TOOL_GRANTS
        from jarvis.memory import SCHEMA_VERSION

        # Scheduler phase 1B owns the current SQLite schema (8); the
        # output envelope contract is untouched by it and remains at 6.
        self.assertEqual(8, SCHEMA_VERSION)
        self.assertEqual(6, OUTPUT_SCHEMA_VERSION)
        self.assertEqual((), EGRESS_TOOL_GRANTS)

    def test_the_write_effect_is_honest(self) -> None:
        profile = self.writer.manifest.default_side_effect
        self.assertIs(SideEffect.LOCAL_WRITE, profile.effect)
        self.assertFalse(profile.reversible)
        self.assertTrue(profile.always_requires_approval)



# ==========================================================================
# P2 - NOTHING FOLLOWS THE CHAIN BEFORE THE CHAIN POLICY ACCEPTS IT
# ==========================================================================
class _FollowLog:
    """Every FOLLOWING inspection aimed at a guarded subtree, in order.

    "Following" is the whole distinction, and it is finer than "which
    syscall". A no-follow ``lstat`` OF the guarded reparse point is the scan
    doing its job - it is how the point is seen as itself - so it is not
    recorded. An ``lstat`` or ``stat`` of anything BELOW it is a different
    act: the OS must traverse the reparse point to answer, so the destination
    has already been visited. ``Path.resolve``, ``realpath``, ``Path.is_dir``,
    ``Path.stat``, ``os.stat``, a volume query and an ``open`` all dereference
    even when aimed at the point itself, so for those the point counts too.

    Every one of them is an inspection AIMED BY a path - so if the path has
    not been accepted yet, the aim belongs to whoever planted the reparse
    point.

    The chain gate itself is recorded unconditionally, because the claim is
    about ORDER: no following event before the first ``chain`` entry.
    """

    def __init__(self, guarded: Path) -> None:
        self._guarded = os.path.normcase(str(guarded))
        self.events: list[str] = []

    def _text(self, value: object) -> str | None:
        if isinstance(value, int) or value is None:
            return None
        try:
            return os.path.normcase(os.fspath(value))  # type: ignore[arg-type]
        except TypeError:
            return None

    def _aimed(self, value: object) -> bool:
        text = self._text(value)
        return text is not None and (
            text == self._guarded or text.startswith(self._guarded + os.sep)
        )

    def _below(self, value: object) -> bool:
        """Strictly under the guarded point: answering means traversing it."""
        text = self._text(value)
        return text is not None and text.startswith(self._guarded + os.sep)

    def note(self, label: str, value: object) -> None:
        if self._aimed(value):
            self.events.append(label)

    def note_below(self, label: str, value: object) -> None:
        if self._below(value):
            self.events.append(label)

    def always(self, label: str) -> None:
        self.events.append(label)

    @property
    def following(self) -> list[str]:
        return [event for event in self.events if event != "chain"]

    @property
    def before_chain(self) -> list[str]:
        head = (
            self.events[: self.events.index("chain")]
            if "chain" in self.events
            else self.events
        )
        return [event for event in head if event != "chain"]


class _FollowOrderCase(EditTestCase):
    """Shared instrumentation for every P2 order regression.

    The claim all of them make is the same one, on three different
    caller-controlled paths: until the no-follow scan has accepted a path,
    nothing may perform an operation that DEREFERENCES it.
    """

    def _spy(self, guarded: Path, owner=None) -> _FollowLog:
        """Instrument every dangerous operation, then the gate.

        ``owner`` is the class the chain gate is patched on; the gate lives on
        the SHARED base, so patching a subclass only instruments that
        subclass. Defaults to the write connector, which is what the overwrite
        tests drive.
        """
        log = _FollowLog(guarded)
        owner = owner or LocalFileWriteConnector
        real_resolve = Path.resolve
        real_is_dir = Path.is_dir
        real_path_stat = Path.stat
        real_realpath = os.path.realpath
        real_stat = os.stat
        real_lstat = os.lstat
        real_open = os.open
        real_facts = local_files._windows_volume_facts
        real_parent = LocalFileWriteConnector._open_windows_parent
        real_chain = owner._reject_reparse_chain

        def resolve(self, *a, **k):
            log.note("resolve", self)
            return real_resolve(self, *a, **k)

        def is_dir(self, *a, **k):
            log.note("is_dir", self)
            return real_is_dir(self, *a, **k)

        def path_stat(self, *a, **k):
            # ``Path.lstat`` routes here with follow_symlinks=False; only the
            # FOLLOWING form is a violation.
            if k.get("follow_symlinks", True):
                log.note("Path.stat", self)
            else:
                log.note_below("Path.lstat", self)
            return real_path_stat(self, *a, **k)

        def realpath(path, *a, **k):
            log.note("realpath", path)
            return real_realpath(path, *a, **k)

        def stat(path, *a, **k):
            if k.get("follow_symlinks", True) and k.get("dir_fd") is None:
                log.note("stat", path)
            return real_stat(path, *a, **k)

        def lstat(path, *a, **k):
            # An lstat OF the guarded point is the no-follow scan working as
            # designed; an lstat of anything UNDER it traversed the point.
            log.note_below("lstat", path)
            return real_lstat(path, *a, **k)

        def opened(path, *a, **k):
            log.note("open", path)
            return real_open(path, *a, **k)

        def facts(path):
            log.note("volume", path)
            return real_facts(path)

        def parent(connector, path):
            log.note("open", path)
            return real_parent(connector, path)

        def chain(connector, root, parts):
            log.always("chain")
            return real_chain(connector, root, parts)

        patches = (
            mock.patch.object(Path, "resolve", resolve),
            mock.patch.object(Path, "is_dir", is_dir),
            mock.patch.object(Path, "stat", path_stat),
            mock.patch.object(os.path, "realpath", realpath),
            mock.patch.object(os, "stat", stat),
            mock.patch.object(os, "lstat", lstat),
            mock.patch.object(os, "open", opened),
            mock.patch.object(local_files, "_windows_volume_facts", facts),
            mock.patch.object(
                LocalFileWriteConnector, "_open_windows_parent", parent
            ),
            mock.patch.object(owner, "_reject_reparse_chain", chain),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return log

    def _junction(self, link: Path, target: Path) -> None:
        """A real NTFS junction, which needs no privilege - unlike a symlink.

        Always a LOCAL directory: the point being proved is that the
        dangerous call is never REACHED with an attacker-chosen path, and
        aiming at a network share to prove it would be the very round-trip
        this milestone exists to prevent.
        """
        try:
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
            )
        except OSError:  # pragma: no cover - no cmd.exe, i.e. not Windows
            completed = None
        if completed is not None and completed.returncode == 0 and link.exists():
            return
        # Off Windows a directory symlink is the same shape of attack; where
        # neither is available there is nothing to regress against.
        try:  # pragma: no cover - platform dependent
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):  # pragma: no cover
            detail = "" if completed is None else completed.stderr.strip()
            self.skipTest(f"directory links unavailable here: {detail}")


@unittest.skipUnless(local_files._ENTRY_PINNED_OVERWRITE, WINDOWS_ONLY)
class PreChainFollowOrderTests(_FollowOrderCase):
    """P2 (Codex finding 2, second half). The ORDER is the security property.

    An overwrite must decide everything decidable about the NAME, prove
    lexical containment, and run the no-follow root/parent/reparse scan BEFORE
    anything traverses the chain. Until then the product has not agreed to
    look at the destination at all - and on Windows every traversing call is
    an ``open``: a junction aimed at a UNC share turns a canonicalization, or
    a ``GetDriveTypeW``, into an outbound network round-trip carrying the
    user's credentials. Refusing late is not the same as refusing early here,
    because the damage is done by the LOOKING.

    Both directions are pinned. The negative proves a refused chain is refused
    with nothing followed; the positive proves the accepted order still runs
    end to end, so the negative cannot be satisfied by refusing everything.

    No network is touched by either test: the junction aims at a local
    directory. What is proved is that the volume API and the resolver are
    never REACHED with an attacker-chosen path, which is what a UNC aim would
    need in order to become traffic.
    """

    def test_a_refused_junction_chain_is_refused_with_nothing_followed(self) -> None:
        """THE regression. The preimage is perfect, so only the chain can refuse.

        The decoy carries byte-identical content, so its digest and size match
        the snapshot the real reader produced, and the target's root-relative
        name is unchanged. Every value-shaped check therefore says yes, and the
        no-follow chain scan is the only thing left that can say no - which is
        what makes the assertion load-bearing rather than incidental.
        """
        content = "preimage bytes\n"
        self.seed("sub/notes.txt", content)
        read_id = self.completed_read("sub/notes.txt")

        decoy = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, decoy, True)
        (decoy / "notes.txt").write_text(content, encoding="utf-8", newline="")

        junction = self.root / "sub"
        shutil.rmtree(junction)
        self._junction(junction, decoy)

        task = self.edit_task("sub/notes.txt", "replacement\n", read_task_id=read_id)
        log = self._spy(junction)
        run = self.refused_before_approval(task)

        # 1. Nothing followed the chain at any point - not before the gate, and
        #    not after it either, since the gate is terminal. ``is_dir`` is in
        #    the instrumented set precisely because the write chain used to
        #    call it on the parent BEFORE this gate, and it dereferences.
        self.assertEqual([], log.before_chain, f"followed first: {log.events}")
        self.assertEqual([], log.following, f"followed at all: {log.events}")
        # 2. The gate is what ran, so the refusal is the chain policy's own and
        #    not an earlier accident that would mask a regression here.
        self.assertIn("chain", log.events)
        self.assertIsNotNone(run.error)
        # 3. The decoy is untouched: no approval, no attempt, no bytes.
        #    ``refused_before_approval`` already pinned the first two.
        self.assertEqual(content, (decoy / "notes.txt").read_text(encoding="utf-8"))

    def test_the_ordinary_overwrite_still_follows_the_accepted_order(self) -> None:
        """Positive control: lexical -> chain -> volume -> open, in that order.

        Without it the test above could be satisfied by a product that refuses
        every overwrite.
        """
        target, read_id = self.prepare_source("plain.txt", "before\n")
        log = self._spy(target)
        task = self.edit_task("plain.txt", "after\n", read_task_id=read_id)
        self.completed(task)

        self.assertEqual("after\n", target.read_text(encoding="utf-8"))
        # Nothing resolved the target by following it, ever.
        self.assertNotIn("resolve", log.events)
        self.assertNotIn("realpath", log.events)
        # And the two inspections that DO follow it ran after the gate.
        self.assertIn("volume", log.events)
        self.assertIn("open", log.events)
        self.assertLess(log.events.index("chain"), log.events.index("volume"))
        self.assertLess(log.events.index("chain"), log.events.index("open"))


class PreChainReadFollowOrderTests(_FollowOrderCase):
    """The same order property, on the READ route.

    The read path had its own pre-policy traversal: ``execute`` used to
    ``lstat`` the WHOLE target name before the secure no-follow open. Naming
    ``root/sub/notes.txt`` in one call makes the OS walk ``sub`` to answer, so
    a junction planted there was traversed before the chain scan had refused a
    single component - the same "the damage is done by the LOOKING" the write
    path was fixed for, one capability over.

    ``lstat`` is instrumented here only BELOW the junction, because an
    ``lstat`` of the junction itself is the no-follow scan working as
    designed. That distinction is what makes the assertion load-bearing.
    """

    MARKER = "READ-DECOY-MARKER-9f21"

    def _planted(self) -> tuple[Path, Path]:
        """A junction at ``root/sub`` aimed at a local decoy holding MARKER."""
        decoy = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, decoy, True)
        (decoy / "notes.txt").write_text(f"{self.MARKER}\n", encoding="utf-8")
        junction = self.root / "sub"
        self._junction(junction, decoy)
        return junction, decoy

    def test_a_parent_junction_read_is_refused_with_nothing_followed(self) -> None:
        junction, decoy = self._planted()
        log = self._spy(junction, owner=LocalFileReadConnector)
        response = self.run_document(self.read_document("sub/notes.txt"))
        payload = response.payload

        self.assertEqual([], log.before_chain, f"followed first: {log.events}")
        self.assertEqual([], log.following, f"followed at all: {log.events}")
        self.assertIn("chain", log.events)
        # Refused, and the decoy's bytes reached nothing at all. A read is
        # auto-approved, so the attempt that ran is the honest thing to pin:
        # it asked for no approval and it bound no snapshot.
        task_id = payload["task"]["id"]
        self.assertNotEqual("completed", payload["task"]["status"])
        self.assertIsNone(payload["approval"])
        self.assertFalse(self.memory.load_task(task_id).local_read.present)
        self.assertNotIn(self.MARKER, json.dumps(payload, default=str))
        self.assertNotIn(
            self.MARKER, Path(self.database).read_bytes().decode("latin-1")
        )
        self.assertEqual(
            f"{self.MARKER}\n", (decoy / "notes.txt").read_text(encoding="utf-8")
        )

    def test_an_ordinary_head_read_still_works(self) -> None:
        """Positive control: the negative cannot be met by refusing everything."""
        self.seed("plain.txt", "line one\nline two\n")
        payload = self.run_document(self.read_document("plain.txt")).payload
        self.assertEqual("completed", payload["task"]["status"], payload.get("error"))
        self.assertEqual(
            "line one\nline two\n", payload["local_read"]["excerpt"]
        )

    def test_an_ordinary_region_read_still_works(self) -> None:
        """M9 REGION security is untouched by the reordering."""
        self.seed("region.txt", "".join(f"line {n}\n" for n in range(1, 21)))
        payload = self.run_document(
            self.read_document("region.txt", {"start_line": 3, "max_lines": 2})
        ).payload
        self.assertEqual("completed", payload["task"]["status"], payload.get("error"))
        self.assertEqual("line 3\nline 4\n", payload["local_read"]["excerpt"])
        self.assertEqual(3, payload["local_read"]["start_line"])
        self.assertEqual(4, payload["local_read"]["end_line"])


class WorkingDirectoryFollowTests(_FollowOrderCase):
    """P2-B. ``context["working_directory"]`` is caller input like any other.

    It used to be ``Path(requested).resolve()``, which dereferences before a
    single resource-policy question has been asked - so a junction supplied
    here was followed, and the containment check that followed was then asked
    about the DESTINATION rather than about the name the caller gave. A
    junction aimed outside the roots was therefore laundered into a clean
    in-root answer, and on Windows the dereference itself is an ``open``.

    All three cases below are LOCAL: nothing aims at a share, because the
    property being proved is that the dereference is never reached.
    """

    def _reader_wd(self, path: Path) -> Path:
        return self.reader._working_directory({"working_directory": str(path)})

    def test_an_in_root_working_directory_junction_is_refused(self) -> None:
        real = self.root / "real-wd"
        real.mkdir()
        (real / "witness.txt").write_text("in root\n", encoding="utf-8")
        link = self.root / "wd-link"
        self._junction(link, real)

        log = self._spy(link, owner=LocalFileReadConnector)
        with self.assertRaises(ValueError) as caught:
            self._reader_wd(link)
        self.assertEqual([], log.before_chain, f"followed first: {log.events}")
        self.assertEqual([], log.following, f"followed at all: {log.events}")
        self.assertIn("chain", log.events)
        self.assertIn("Working directory", str(caught.exception))

    def test_a_working_directory_junction_out_of_root_is_refused(self) -> None:
        """The one ``resolve()`` actively laundered.

        The destination is outside the allowed roots, so a product that
        dereferenced first would have to refuse it on containment - and a
        product that dereferenced and got an in-root answer would accept it.
        Neither happens: the junction is refused as itself, and where it
        pointed is never learned.
        """
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        link = self.root / "wd-escape"
        self._junction(link, outside)

        log = self._spy(link, owner=LocalFileReadConnector)
        with self.assertRaises(ValueError) as caught:
            self._reader_wd(link)
        self.assertEqual([], log.before_chain, f"followed first: {log.events}")
        self.assertEqual([], log.following, f"followed at all: {log.events}")
        self.assertIn("chain", log.events)
        self.assertIn("Working directory", str(caught.exception))

    def test_an_ordinary_local_working_directory_is_accepted(self) -> None:
        """Positive control, plus the lexical rules that must still hold."""
        ordinary = self.root / "plain-wd"
        ordinary.mkdir()
        self.assertEqual(ordinary, self._reader_wd(ordinary))
        # ``..`` is still collapsed lexically rather than followed...
        self.assertEqual(
            ordinary, self._reader_wd(self.root / "plain-wd" / ".." / "plain-wd")
        )
        # ...and still cannot leave the roots.
        with self.assertRaises(ValueError):
            self._reader_wd(self.root.parent)
        # A working directory that is not a directory at all is refused.
        self.seed("not-a-dir.txt", "x\n")
        with self.assertRaises(ValueError):
            self._reader_wd(self.root / "not-a-dir.txt")


class ClaudeWorkingDirectoryFollowTests(_FollowOrderCase):
    """P2-B, one connector over: the EGRESS route's working directory.

    ``ClaudeCodeConnector._working_directory`` had the identical defect the
    local connectors were just fixed for - ``Path(requested).resolve()``
    followed by ``path.is_dir()``, both of them dereferencing the
    caller-supplied name before a single resource-policy question had been
    asked. It matters more here, not less: this is the working directory an
    external CLI process is launched in, and it is bound into the egress
    attestation, so a laundered junction would be attested as the clean in-root
    name it never was.

    Every case is LOCAL - the junctions aim at ordinary temp directories -
    because the property being proved is that the dereference is never
    REACHED, not what lies beyond it.
    """

    def setUp(self) -> None:
        super().setUp()
        # Built before the spy so its construction-time root binding cannot be
        # mistaken for an inspection of a caller-supplied path.
        self.claude = ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )

    def _claude_wd(self, path: Path) -> Path:
        return self.claude._working_directory({"working_directory": str(path)})

    def test_an_in_root_working_directory_junction_is_refused(self) -> None:
        real = self.root / "real-wd"
        real.mkdir()
        (real / "witness.txt").write_text("in root\n", encoding="utf-8")
        link = self.root / "wd-link"
        self._junction(link, real)

        log = self._spy(link, owner=ClaudeCodeConnector)
        with self.assertRaises(ValueError) as caught:
            self._claude_wd(link)
        self.assertEqual([], log.before_chain, f"followed first: {log.events}")
        self.assertEqual([], log.following, f"followed at all: {log.events}")
        self.assertIn("chain", log.events)
        self.assertIn("Working directory", str(caught.exception))

    def test_a_working_directory_junction_out_of_root_is_refused(self) -> None:
        """The one ``resolve()`` actively laundered.

        The destination is outside the allowed roots, so a product that
        dereferenced first would decide containment against the DESTINATION.
        Here the junction is refused as itself and where it pointed is never
        learned - the decoy directory is never visited at all.
        """
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "witness.txt").write_text("outside\n", encoding="utf-8")
        link = self.root / "wd-escape"
        self._junction(link, outside)

        log = self._spy(link, owner=ClaudeCodeConnector)
        with self.assertRaises(ValueError) as caught:
            self._claude_wd(link)
        self.assertEqual([], log.before_chain, f"followed first: {log.events}")
        self.assertEqual([], log.following, f"followed at all: {log.events}")
        self.assertIn("chain", log.events)
        self.assertIn("Working directory", str(caught.exception))
        self.assertEqual(
            "outside\n", (outside / "witness.txt").read_text(encoding="utf-8")
        )

    def test_an_ordinary_local_working_directory_is_accepted(self) -> None:
        """Positive control, plus the lexical rules that must still hold."""
        ordinary = self.root / "plain-wd"
        ordinary.mkdir()
        log = self._spy(ordinary, owner=ClaudeCodeConnector)

        self.assertEqual(ordinary, self._claude_wd(ordinary))
        # Accepted, and accepted without ever dereferencing the name.
        self.assertIn("chain", log.events)
        self.assertEqual([], log.following, f"followed at all: {log.events}")
        # ``..`` is still collapsed lexically rather than followed...
        self.assertEqual(
            ordinary, self._claude_wd(self.root / "plain-wd" / ".." / "plain-wd")
        )
        # ...and still cannot leave the roots.
        with self.assertRaises(ValueError):
            self._claude_wd(self.root.parent)
        # A working directory that is not a directory at all is refused.
        self.seed("claude-not-a-dir.txt", "x\n")
        with self.assertRaises(ValueError):
            self._claude_wd(self.root / "claude-not-a-dir.txt")
        self.assertEqual([], log.following, f"followed at all: {log.events}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
