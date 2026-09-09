"""M5 - bounded single-file local write.

The acceptance chain this file pins end to end:

    task JSON -> mandatory approval -> exact-byte attestation -> atomic
    replace -> independent verification -> bounded typed record

plus the refusal surface (containment, secret deny, pre-state, size,
encoding), the atomicity guarantee, the H2 unknown-on-raise rule, the
decision/retrieval isolation and the executable-lifetime retention of the raw
intended bytes.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from jarvis.app import (
    OUTPUT_SCHEMA_VERSION,
    build_envelope,
    create_app,
    decode_task_json,
    show_result,
)
from jarvis.connectors import claude_code, local_files
from jarvis.connectors.base import BaseConnector
from jarvis.connectors.local_files import (
    LocalFileReadConnector,
    LocalFileWriteConnector,
)
from jarvis.contracts import (
    APPLIED_EFFECT_LOCAL_WRITE,
    LOCAL_WRITE_AMBIGUOUS,
    LOCAL_WRITE_CAPABILITY,
    LOCAL_WRITE_DID_NOT_LAND,
    LOCAL_WRITE_ENABLED_MODES,
    LOCAL_WRITE_FRAME_HEADER,
    LOCAL_WRITE_LANDED,
    LOCAL_WRITE_MAX_BYTES,
    LOCAL_WRITE_MODES,
    LOCAL_WRITE_STATUSES,
    LOCAL_WRITE_TEMP_PREFIX,
    MANDATORY_APPROVAL_EFFECTS,
    ActionScope,
    AttemptState,
    AttestationError,
    BillingClass,
    DataClassification,
    ErrorCode,
    LocalWriteRecord,
    LoopStage,
    MonetaryState,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    ReservationState,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    Task,
    TaskStatus,
    ToolResult,
    local_write_content_refusal,
    local_write_record_refusal,
    utc_now,
)
from jarvis.settings import Settings
from tests.helpers import DummyConnector, safe_cleanup
from tests.test_local_file_read import make_directory_link

MARKER = "WRITE-CONTENT-MARKER-7f31"

#: A plain Win32 open asking for write access and offering every share mode
#: itself, so the only thing that can refuse it is the share mode the
#: connector chose when it created the object.
_GENERIC_WRITE = 0x40000000
_SHARE_EVERYTHING = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_ERROR_SHARING_VIOLATION = 32


def open_for_write(path: Path) -> tuple[int | None, int]:
    """A second, independent writable HANDLE to the object at ``path``.

    Returns ``(handle, 0)`` if the object could be opened for writing, or
    ``(None, last_error)`` if the kernel refused. This is the attacker's
    move in the P1-B window: the object is exactly the one the connector is
    about to install, so a handle obtained here can rewrite the approved
    bytes without disturbing the object's identity at all.
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    handle = create(
        str(path), _GENERIC_WRITE, _SHARE_EVERYTHING, None, _OPEN_EXISTING, 0x80, None
    )
    if handle in (ctypes.c_void_p(-1).value, 0, None):
        return None, ctypes.get_last_error()
    return handle, 0


def write_task(
    target: str | Path,
    content: str = "hello\n",
    mode: str = "create",
    *,
    classification: DataClassification = DataClassification.INTERNAL,
    objective: str = "write the file",
) -> Task:
    task = Task(
        objective=objective,
        capability=LOCAL_WRITE_CAPABILITY,
        scope=ActionScope(
            action="write",
            target=str(target),
            data_classification=classification,
        ),
    )
    task.write_content = content
    task.write_mode = mode
    return task


class LocalWriteTestCase(unittest.TestCase):
    """One temp root, one write connector, one app - production composition."""

    permission_mode = PermissionMode.SESSION

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self._temp)
        self.root = Path(self._temp.name).resolve()
        self.settings = Settings(
            memory_database=self.root / "memory.db",
            permission_mode=self.permission_mode,
        )
        self.connector = LocalFileWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.app = create_app(settings=self.settings, connectors=(self.connector,))
        self.core = self.app.core
        self.memory = self.app.memory

    # -- helpers ------------------------------------------------------------

    def seed(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def prepared(self, task: Task) -> Task:
        """Bind the durable approved authority exactly as ``AgentCore`` does.

        Not an injection of made-up state: this calls the connector's own
        ``pre_execution_record``, which is the same derivation the core uses
        and which the core validates before persisting. Tests that drive
        ``execute`` directly have to stand in for the core, and this is what
        the core does.
        """
        record = self.connector.pre_execution_record(task)
        self.assertIsNotNone(record, "the authority could not be prepared")
        task.local_write = record
        return task

    def execute_prepared(self, task: Task):
        return self.connector.execute(self.prepared(task), SessionState())

    def request(self, task: Task, session_id: str | None = None):
        run = self.core.run(task, session_id=session_id)
        self.assertEqual(
            RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error)
        )
        return run

    def approve(self, task: Task, run):
        grant = PermissionGrant.for_request(run.permission_request)
        return self.core.resume(run.session.id, task.id, grant=grant)

    def perform(self, target, content: str = "hello\n", mode: str = "create", **kwargs):
        """One full approved write; returns (task, run)."""
        task = write_task(target, content, mode, **kwargs)
        return task, self.approve(task, self.request(task))

    def completed(self, target, content: str = "hello\n", mode: str = "create", **kw):
        task, run = self.perform(target, content, mode, **kw)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        return self.memory.load_task(task.id), run

    def refused(self, target, content: str = "hello\n", mode: str = "create", **kw):
        """The write is refused somewhere; returns the failing run."""
        task = write_task(target, content, mode, **kw)
        run = self.core.run(task)
        if run.status is RunStatus.AWAITING_APPROVAL:
            run = self.approve(task, run)
        self.assertEqual(RunStatus.FAILED, run.status)
        return task, run


# --------------------------------------------------------------------------
# 1. Untrusted input boundary
# --------------------------------------------------------------------------


class InputBoundaryTests(unittest.TestCase):
    def decode(self, **write: Any) -> Task:
        document: dict[str, Any] = {
            "objective": "write a file",
            "capability": LOCAL_WRITE_CAPABILITY,
            "scope": {"action": "write", "target": "notes.txt"},
        }
        if write:
            document["write"] = write
        return decode_task_json(json.dumps(document))

    def test_every_enabled_mode_decodes_exactly(self) -> None:
        for mode in LOCAL_WRITE_ENABLED_MODES:
            with self.subTest(mode=mode):
                task = self.decode(content="body\n", mode=mode)
                self.assertEqual("body\n", task.write_content)
                self.assertEqual(mode, task.write_mode)

    def test_overwrite_decodes_but_the_boundary_authorizes_nothing(self) -> None:
        """M10 enables overwrite as POLICY; everything else is asked later.

        The input boundary owns the grammar and the policy tuple. It does not
        know whether this host can pin a directory entry, what volume the
        target sits on, or what is currently on disk - so a decoded overwrite
        is a well-formed REQUEST and never an authorization. The connector
        asks all three before any approval can exist.
        """
        self.assertIn("overwrite", LOCAL_WRITE_MODES)
        self.assertIn("overwrite", LOCAL_WRITE_ENABLED_MODES)
        task = self.decode(content="body\n", mode="overwrite")
        self.assertEqual("overwrite", task.write_mode)
        self.assertEqual("body\n", task.write_content)

    def test_a_task_without_a_write_section_carries_nothing(self) -> None:
        task = self.decode()
        self.assertEqual("", task.write_content)
        self.assertEqual("", task.write_mode)

    def test_an_unknown_write_key_is_rejected(self) -> None:
        with self.assertRaises(Exception) as caught:
            self.decode(content="x", mode="create", encoding="utf-16")
        self.assertIn("encoding", str(caught.exception))

    def test_both_write_fields_are_required_together(self) -> None:
        for partial in ({"content": "x"}, {"mode": "create"}):
            with self.subTest(partial=sorted(partial)):
                with self.assertRaises(Exception):
                    self.decode(**partial)

    def test_a_non_string_content_is_refused(self) -> None:
        for value in (1, 1.5, True, None, ["a"], {"a": 1}):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    self.decode(content=value, mode="create")

    def test_a_nul_byte_is_refused(self) -> None:
        with self.assertRaises(Exception) as caught:
            self.decode(content="before\u0000after", mode="create")
        self.assertIn("NUL", str(caught.exception))

    def test_a_lone_surrogate_is_refused_not_replaced(self) -> None:
        # A lone surrogate survives JSON decoding but cannot be encoded. It
        # must fail closed rather than becoming a replacement character the
        # user never asked to write.
        # The escape has to be in the JSON TEXT, so the decoder produces a
        # real lone surrogate rather than a literal backslash sequence.
        raw = (
            '{"objective": "o", "capability": "'
            + LOCAL_WRITE_CAPABILITY
            + '", "scope": {"action": "write", "target": "notes.txt"},'
            ' "write": {"content": "a\\ud800b", "mode": "create"}}'
        )
        self.assertIn("\ud800", json.loads(raw)["write"]["content"])
        with self.assertRaises(Exception) as caught:
            decode_task_json(raw)
        self.assertIn("UTF-8", str(caught.exception))

    def test_exactly_at_the_ceiling_is_accepted(self) -> None:
        task = self.decode(content="a" * LOCAL_WRITE_MAX_BYTES, mode="create")
        self.assertEqual(LOCAL_WRITE_MAX_BYTES, len(task.write_content))

    def test_one_byte_past_the_ceiling_is_refused_never_truncated(self) -> None:
        with self.assertRaises(Exception) as caught:
            self.decode(content="a" * (LOCAL_WRITE_MAX_BYTES + 1), mode="create")
        self.assertIn("ceiling", str(caught.exception))

    def test_a_multibyte_payload_is_measured_in_BYTES(self) -> None:
        # The ceiling is physical, so two-byte characters count twice.
        over = "\u00e9" * ((LOCAL_WRITE_MAX_BYTES // 2) + 1)
        self.assertLessEqual(len(over), LOCAL_WRITE_MAX_BYTES)
        with self.assertRaises(Exception):
            self.decode(content=over, mode="create")

    def test_an_unknown_mode_is_refused(self) -> None:
        for mode in ("append", "CREATE", "", "overwrite ", 1):
            with self.subTest(mode=mode):
                with self.assertRaises(Exception):
                    self.decode(content="x", mode=mode)

    def test_the_outer_task_json_limit_is_unchanged(self) -> None:
        from jarvis.app import MAX_TASK_JSON_BYTES

        self.assertEqual(256 * 1024, MAX_TASK_JSON_BYTES)

    def test_content_is_never_transformed(self) -> None:
        # CRLF, tabs, trailing whitespace and a credential-shaped line all
        # survive byte for byte: this is the deliverable, not a diagnostic.
        raw = "a\r\n\tb  \nAWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\n"
        self.assertEqual(raw, self.decode(content=raw, mode="create").write_content)


# --------------------------------------------------------------------------
# 2. Approval policy - mandatory in every mode
# --------------------------------------------------------------------------


class ApprovalPolicyTests(LocalWriteTestCase):
    def _run_in(self, mode: PermissionMode):
        settings = Settings(
            memory_database=self.root / f"{mode.value}.db", permission_mode=mode
        )
        app = create_app(settings=settings, connectors=(self.connector,))
        task = write_task(self.root / "notes.txt")
        return app, task, app.core.run(task)

    def test_local_write_is_a_mandatory_approval_effect(self) -> None:
        self.assertIn(SideEffect.LOCAL_WRITE, MANDATORY_APPROVAL_EFFECTS)

    def test_the_effect_alone_makes_approval_mandatory(self) -> None:
        """The POLICY SET is load-bearing, not just this connector's flag.

        ``LocalFileWriteConnector`` also sets ``always_requires_approval``,
        so every test that goes through it would still pass if
        ``LOCAL_WRITE`` were dropped from ``MANDATORY_APPROVAL_EFFECTS``.
        A connector that declares the effect WITHOUT that flag isolates the
        set: under SMART_AUTO a LOW, reversible task would otherwise run
        unattended.
        """
        from tests.helpers import make_task

        connector = DummyConnector(
            name="plain-writer",
            side_effect=SideEffect.LOCAL_WRITE,
            reversible=True,
            always_requires_approval=False,
        )
        app = create_app(
            settings=Settings(
                memory_database=self.root / "plain.db",
                permission_mode=PermissionMode.SMART_AUTO,
            ),
            connectors=(connector,),
        )
        run = app.core.run(
            make_task(effect=SideEffect.LOCAL_WRITE, risk=RiskLevel.LOW)
        )
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertEqual(0, connector.execute_calls)

    def test_strict_mode_requires_approval(self) -> None:
        _, _, run = self._run_in(PermissionMode.STRICT)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)

    def test_session_mode_requires_approval(self) -> None:
        _, _, run = self._run_in(PermissionMode.SESSION)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)

    def test_smart_auto_requires_approval(self) -> None:
        # The mode that auto-approves LOW and reversible work. A write is
        # neither, and it is mandatory regardless.
        _, _, run = self._run_in(PermissionMode.SMART_AUTO)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertFalse((self.root / "notes.txt").exists())

    def test_no_mode_writes_a_byte_before_approval(self) -> None:
        for mode in PermissionMode:
            with self.subTest(mode=mode):
                target = self.root / f"{mode.value}.txt"
                settings = Settings(
                    memory_database=self.root / f"gate-{mode.value}.db",
                    permission_mode=mode,
                )
                app = create_app(settings=settings, connectors=(self.connector,))
                app.core.run(write_task(target))
                self.assertFalse(target.exists())

    def test_an_approval_for_a_write_may_not_be_remembered_at_all(self) -> None:
        """The user-facing approve surface refuses to remember a write."""
        task = write_task(self.root / "one.txt")
        run = self.request(task)
        request = run.permission_request
        result = self.app.approve(
            run.session.id,
            task.id,
            request.id,
            request.fingerprint,
            remember_for_session=True,
        )
        self.assertEqual(
            ErrorCode.INVALID_GRANT.value, result.payload["error"]["code"]
        )
        self.assertFalse((self.root / "one.txt").exists())

    def test_a_second_write_still_asks_after_an_approved_one(self) -> None:
        """Nothing about approving one write carries over to the next."""
        first = write_task(self.root / "one.txt")
        run = self.request(first)
        done = self.approve(first, run)
        self.assertEqual(RunStatus.COMPLETED, done.status, msg=str(done.error))

        second = write_task(self.root / "two.txt")
        again = self.core.run(second, session_id=run.session.id)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, again.status)
        self.assertFalse((self.root / "two.txt").exists())

    def test_the_effective_profile_is_irreversible_and_medium_risk(self) -> None:
        run = self.request(write_task(self.root / "notes.txt"))
        request = run.permission_request
        self.assertIs(SideEffect.LOCAL_WRITE, request.side_effect.effect)
        self.assertFalse(request.side_effect.reversible)
        self.assertTrue(request.side_effect.mandatory)
        self.assertGreaterEqual(request.risk_level, RiskLevel.MEDIUM)

    def test_a_rejected_approval_writes_nothing(self) -> None:
        task = write_task(self.root / "notes.txt")
        run = self.request(task)
        grant = PermissionGrant.for_request(run.permission_request, approved=False)
        decided = self.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.REJECTED, decided.status)
        self.assertFalse((self.root / "notes.txt").exists())

    def test_the_route_is_unbilled_and_opens_no_monetary_gate(self) -> None:
        stored, run = self.completed(self.root / "notes.txt")
        self.assertIs(BillingClass.NO_ADDITIONAL_CHARGE, stored.estimate.billing_class)
        self.assertEqual(0.0, stored.estimate.monetary_maximum_usd)
        self.assertIs(MonetaryState.NOT_APPLICABLE, run.result.usage.monetary_state)
        self.assertIsNone(run.result.usage.monetary_cost_usd)


# --------------------------------------------------------------------------
# 3. H1 exact-byte attestation
# --------------------------------------------------------------------------


class AttestationTests(LocalWriteTestCase):
    def fingerprint(self, task: Task) -> str:
        return self.connector.permission_request(task, "fixed-session").fingerprint

    def frame(self, task: Task) -> bytes:
        return self.connector.canonical_outbound(task)

    def test_the_frame_is_deterministic_and_carries_the_exact_bytes(self) -> None:
        task = write_task(self.root / "notes.txt", "exact\u00e9\n")
        frame = self.frame(task)
        self.assertEqual(frame, self.frame(task))
        header, rest = frame.split(b"\n", 1)
        self.assertEqual(LOCAL_WRITE_FRAME_HEADER, header)
        meta, payload = rest.split(b"\n", 1)
        self.assertEqual("exact\u00e9\n".encode("utf-8"), payload)
        decoded = json.loads(meta.decode("ascii"))
        self.assertEqual(
            {
                "path",
                "mode",
                "pre_present",
                "pre_sha256",
                "pre_byte_size",
                "intended_sha256",
                "intended_byte_size",
            },
            set(decoded),
        )
        self.assertEqual(len(payload), decoded["intended_byte_size"])
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(), decoded["intended_sha256"]
        )

    def test_the_header_can_never_contain_a_literal_newline(self) -> None:
        # ensure_ascii framing: the payload is unambiguously everything after
        # the second newline, whatever the path or content contain.
        task = write_task(self.root / "odd\u00e9.txt", "a\nb\nc\n", "create")
        _, rest = self.frame(task).split(b"\n", 1)
        meta, payload = rest.split(b"\n", 1)
        self.assertNotIn(b"\n", meta)
        self.assertEqual(b"a\nb\nc\n", payload)

    def test_one_changed_byte_changes_the_fingerprint(self) -> None:
        base = write_task(self.root / "notes.txt", "aaa\n")
        other = write_task(self.root / "notes.txt", "aab\n")
        self.assertNotEqual(self.fingerprint(base), self.fingerprint(other))

    def test_a_different_path_changes_the_fingerprint(self) -> None:
        one = write_task(self.root / "one.txt", "same\n")
        two = write_task(self.root / "two.txt", "same\n")
        self.assertNotEqual(self.fingerprint(one), self.fingerprint(two))

    def test_an_unbound_overwrite_cannot_be_attested_at_all(self) -> None:
        """No approval can ever be requested for an overwrite M10 refuses.

        M5 refused every overwrite at this surface. M10 refuses one carrying
        no completed whole-file read of the target - the same guarantee in the
        same place, so nothing downstream has to remember that it would have
        failed later.
        """
        self.seed("notes.txt", "prior\n")
        overwrite = write_task(self.root / "notes.txt", "new\n", "overwrite")
        with self.assertRaises(AttestationError) as caught:
            self.fingerprint(overwrite)
        self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)
        self.assertIsNone(self.connector.outbound_preview(overwrite))

    def test_a_create_over_an_existing_file_cannot_be_attested(self) -> None:
        self.seed("notes.txt", "prior\n")
        create = write_task(self.root / "notes.txt", "new\n", "create")
        with self.assertRaises(AttestationError):
            self.fingerprint(create)

    def test_a_changed_prior_state_changes_the_fingerprint(self) -> None:
        """The core M5 binding: approval covers ONE prior state.

        The two prior contents are the SAME LENGTH on purpose. The approval
        preview quotes the prior byte size, so differing lengths would move
        the fingerprint through ``display_digest`` even if the canonical
        frame carried no pre-state at all. Equal lengths isolate the frame:
        only its ``pre_sha256`` can tell these two apart.
        """
        target = self.root / "notes.txt"
        task = write_task(target, "new\n", "create")
        before = self.fingerprint(task)
        before_summary = self.connector.outbound_preview(task).summary
        # The destination appearing IS a prior-state change, and a create can
        # no longer be attested against it at all.
        self.seed("notes.txt", "aaaaa\n")
        with self.assertRaises(AttestationError):
            self.fingerprint(task)
        target.unlink()
        self.assertEqual(before, self.fingerprint(task))
        self.assertEqual(
            before_summary, self.connector.outbound_preview(task).summary
        )

    def test_the_frame_is_stable_across_approve_resume(self) -> None:
        task = write_task(self.root / "notes.txt", "stable\n")
        run = self.request(task)
        reloaded = self.memory.load_task(task.id)
        self.assertEqual(self.frame(task), self.frame(reloaded))
        self.assertEqual(
            run.permission_request.fingerprint,
            self.connector.permission_request(reloaded, run.session.id).fingerprint,
        )

    def test_a_stale_grant_after_the_target_changes_does_not_execute(self) -> None:
        task = write_task(self.root / "notes.txt", "authorized\n", "create")
        run = self.request(task)
        grant = PermissionGrant.for_request(run.permission_request)
        # A third party creates the file between approval and execution.
        self.seed("notes.txt", "someone got there first\n")
        decided = self.core.resume(run.session.id, task.id, grant=grant)
        self.assertNotEqual(RunStatus.COMPLETED, decided.status)
        self.assertEqual(
            "someone got there first\n",
            (self.root / "notes.txt").read_text(encoding="utf-8"),
        )

    def _forgetful(self, effect: SideEffect) -> BaseConnector:
        """A connector that declares an exact-byte effect and implements no
        canonical outbound at all - the exact shape the guard exists for."""
        from jarvis.connectors.base import ConnectorManifest
        from jarvis.contracts import SideEffectProfile

        class Forgetful(BaseConnector):
            manifest = ConnectorManifest(
                name=f"forgetful-{effect.value}",
                capabilities=(LOCAL_WRITE_CAPABILITY,),
                default_risk=RiskLevel.MEDIUM,
                default_side_effect=SideEffectProfile(
                    effect=effect, reversible=False, always_requires_approval=True
                ),
                default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            )

            def execute(self, task, session):  # pragma: no cover - never reached
                raise AssertionError("must never execute")

        connector = Forgetful()
        self.assertIs(
            BaseConnector.canonical_outbound, type(connector).canonical_outbound
        )
        return connector

    def test_a_write_connector_without_canonical_outbound_fails_closed(self) -> None:
        connector = self._forgetful(SideEffect.LOCAL_WRITE)
        with self.assertRaises(AttestationError) as caught:
            connector.permission_request(write_task(self.root / "x.txt"), "session")
        self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)

    def test_a_destructive_connector_without_canonical_outbound_fails_closed(
        self,
    ) -> None:
        connector = self._forgetful(SideEffect.DESTRUCTIVE)
        with self.assertRaises(AttestationError):
            connector.permission_request(write_task(self.root / "x.txt"), "session")

    def test_an_egress_connector_without_canonical_outbound_still_fails_closed(
        self,
    ) -> None:
        """The original guard, unchanged: EXTERNAL_EGRESS is bit-identical."""
        connector = self._forgetful(SideEffect.EXTERNAL_EGRESS)
        with self.assertRaises(AttestationError):
            connector.permission_request(write_task(self.root / "x.txt"), "session")

    def test_a_read_connector_is_untouched_by_the_widened_guard(self) -> None:
        """M3 regression: a task may still STRENGTHEN a read's effect.

        Strengthening is a hardening mechanism, not a claim that the read
        will write, so it must still produce an approval request rather than
        an attestation failure.
        """
        reader = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.seed("notes.txt", "readable\n")
        task = Task(
            objective="read it",
            capability="file_read",
            scope=ActionScope(action="read", target=str(self.root / "notes.txt")),
        )
        for effect in (SideEffect.LOCAL_WRITE, SideEffect.DESTRUCTIVE):
            with self.subTest(effect=effect):
                task.side_effect = replace(
                    task.side_effect, effect=effect, reversible=False
                )
                request = reader.permission_request(task, "session")
                self.assertIs(effect, request.side_effect.effect)

    def test_the_preview_is_metadata_only(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n", "create")
        preview = self.connector.outbound_preview(task)
        serialized = json.dumps(preview.to_dict(), default=str)
        self.assertNotIn(MARKER, serialized)
        self.assertIn("notes.txt", preview.summary)
        self.assertIn("create", preview.summary)
        self.assertEqual(len(MARKER) + 1, preview.approximate_bytes)

    def test_the_preview_never_shows_an_absolute_path(self) -> None:
        task = write_task(self.root / "deep" / "notes.txt")
        (self.root / "deep").mkdir()
        preview = self.connector.outbound_preview(task)
        serialized = json.dumps(preview.to_dict(), default=str)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.root).casefold(), serialized.casefold())

    def test_pre_m5_attestation_digests_are_unchanged(self) -> None:
        """The shared EgressAttestation contract did not move.

        M5 binds the pre-state through the canonical outbound frame precisely
        so no field had to be added here; a new field would have changed every
        persisted attestation digest in the product.
        """
        from jarvis.contracts import EgressAttestation, SideEffectProfile

        attestation = EgressAttestation(
            scope=ActionScope(action="analyze", target="x").normalized(),
            risk_level=RiskLevel.LOW,
            side_effect=SideEffectProfile(),
            outbound_digest="a" * 64,
        )
        self.assertEqual(
            {
                "scope",
                "risk_level",
                "side_effect",
                "outbound_digest",
                "billing_gate",
                "cost_gate",
                "working_directory",
                "allowed_roots",
                "tool_grants",
            },
            set(attestation.to_dict()),
        )


# --------------------------------------------------------------------------
# 4. Create / overwrite authority
# --------------------------------------------------------------------------


class CreateOverwriteTests(LocalWriteTestCase):
    def test_create_of_an_absent_target_succeeds(self) -> None:
        stored, _ = self.completed(self.root / "notes.txt", "created\n", "create")
        self.assertEqual(
            "created\n", (self.root / "notes.txt").read_text(encoding="utf-8")
        )
        record = stored.local_write
        self.assertEqual("notes.txt", record.relative_path)
        self.assertEqual("create", record.mode)
        self.assertFalse(record.pre_present)
        self.assertEqual("", record.pre_sha256)
        self.assertEqual(0, record.pre_byte_size)
        self.assertEqual("written", record.status)

    def test_create_into_an_existing_subdirectory_succeeds(self) -> None:
        (self.root / "docs").mkdir()
        stored, _ = self.completed(self.root / "docs" / "notes.txt")
        self.assertEqual("docs/notes.txt", stored.local_write.relative_path)

    def test_create_when_the_target_exists_is_refused(self) -> None:
        self.seed("notes.txt", "already here\n")
        _, run = self.refused(self.root / "notes.txt", "new\n", "create")
        self.assertEqual(
            "already here\n", (self.root / "notes.txt").read_text(encoding="utf-8")
        )

    def test_an_unbound_overwrite_is_refused_before_anything_is_approved(self) -> None:
        """M10 binds an overwrite to a completed read of the file it replaces.

        A snapshot that merely MATCHES is not provenance, so the durable
        reference is what the loop demands - and it demands it before an
        approval, an attestation or an attempt can exist. The existing file is
        left exactly as it was. The rest of the provenance surface is pinned
        in ``tests/test_local_file_edit.py``.
        """
        self.seed("notes.txt", "before\n")
        task = write_task(self.root / "notes.txt", "after\n", "overwrite")
        run = self.core.run(task)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("completed local read", run.error.message)
        self.assertEqual([], self.memory.execution_attempts(task.id))
        self.assertIsNone(self.memory.load_pending_approval(task.id, run.session.id))
        self.assertEqual(
            "before\n", (self.root / "notes.txt").read_text(encoding="utf-8")
        )

    def test_overwrite_of_an_absent_target_is_refused_too(self) -> None:
        """Not "the mode is unsupported" any more, and not silently a create.

        An overwrite naming a file that does not exist cannot have a completed
        read of it either, so it is refused for want of provenance and never
        downgraded. That mode and prior state are ONE authority is pinned
        directly against the connector in ``tests/test_local_file_edit.py``.
        """
        _, run = self.refused(self.root / "missing.txt", "new\n", "overwrite")
        self.assertIs(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("completed local read", run.error.message)
        self.assertFalse((self.root / "missing.txt").exists())

    def test_a_directory_target_is_refused(self) -> None:
        (self.root / "adir").mkdir()
        self.refused(self.root / "adir", "x\n", "create")
        self.assertTrue((self.root / "adir").is_dir())

    def test_a_missing_parent_is_refused_and_no_directory_is_created(self) -> None:
        _, run = self.refused(self.root / "absent" / "notes.txt")
        self.assertIn("never creates directories", run.error.message)
        self.assertFalse((self.root / "absent").exists())

    def test_an_empty_file_can_be_created(self) -> None:
        stored, _ = self.completed(self.root / "empty.txt", "", "create")
        self.assertEqual(b"", (self.root / "empty.txt").read_bytes())
        self.assertEqual(0, stored.local_write.intended_byte_size)
        self.assertEqual(
            hashlib.sha256(b"").hexdigest(), stored.local_write.intended_sha256
        )

    def test_exactly_the_ceiling_is_written(self) -> None:
        payload = "a" * LOCAL_WRITE_MAX_BYTES
        self.completed(self.root / "big.txt", payload, "create")
        self.assertEqual(
            LOCAL_WRITE_MAX_BYTES, (self.root / "big.txt").stat().st_size
        )

    def test_an_existing_target_is_never_touched_whatever_its_size(self) -> None:
        """Create-only: an existing file is a refusal, not a replacement."""
        self.seed("huge.txt", "a" * (LOCAL_WRITE_MAX_BYTES + 1))
        self.refused(self.root / "huge.txt", "small\n", "create")
        self.assertEqual(
            LOCAL_WRITE_MAX_BYTES + 1, (self.root / "huge.txt").stat().st_size
        )

    def test_an_existing_binary_target_is_left_alone(self) -> None:
        (self.root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
        self.refused(self.root / "blob.bin", "text\n", "create")
        self.assertEqual(
            b"\xff\xfe\x00\x01", (self.root / "blob.bin").read_bytes()
        )


# --------------------------------------------------------------------------
# 5. Containment - the M3/M4 chain, re-anchored on the parent
# --------------------------------------------------------------------------


class ContainmentTests(LocalWriteTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.outside, True)

    def test_a_target_outside_the_roots_is_refused(self) -> None:
        _, run = self.refused(self.outside / "escape.txt")
        self.assertIs(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertFalse((self.outside / "escape.txt").exists())

    def test_a_dot_dot_route_out_of_the_roots_is_refused(self) -> None:
        target = self.root / ".." / self.outside.name / "escape.txt"
        self.refused(target)
        self.assertFalse((self.outside / "escape.txt").exists())

    def test_every_denied_secret_name_is_refused(self) -> None:
        for name in (".env", "id_rsa", "key.pem", ".npmrc", "secret.p12"):
            with self.subTest(name=name):
                _, run = self.refused(self.root / name)
                self.assertIn("secret-file policy", run.error.message)
                self.assertFalse((self.root / name).exists())

    def test_a_path_inside_dot_ssh_is_refused(self) -> None:
        (self.root / ".ssh").mkdir()
        self.refused(self.root / ".ssh" / "anything.txt")
        self.assertFalse((self.root / ".ssh" / "anything.txt").exists())

    def test_a_credential_bearing_name_is_refused(self) -> None:
        _, run = self.refused(self.root / "api_key=HUNTER2.txt")
        self.assertIn("credential-bearing", run.error.message)

    def test_an_alternate_data_stream_target_is_refused(self) -> None:
        _, run = self.refused(self.root / "notes.txt:hidden")
        self.assertIsNotNone(run.error)
        self.assertFalse((self.root / "notes.txt").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_a_symlink_target_pointing_outside_is_refused(self) -> None:
        victim = self.outside / "victim.txt"
        victim.write_text("outside\n", encoding="utf-8")
        link = self.root / "innocent.txt"
        try:
            os.symlink(victim, link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlink creation not permitted")
        self.refused(link, "overwritten\n", "overwrite")
        self.assertEqual("outside\n", victim.read_text(encoding="utf-8"))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_a_symlink_target_pointing_at_a_denied_file_is_refused(self) -> None:
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh" / "id_rsa").write_text("PRIVATE\n", encoding="utf-8")
        link = self.root / "harmless.txt"
        try:
            os.symlink(self.root / ".ssh" / "id_rsa", link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlink creation not permitted")
        self.refused(link, "x\n", "overwrite")
        self.assertEqual(
            "PRIVATE\n", (self.root / ".ssh" / "id_rsa").read_text(encoding="utf-8")
        )

    def test_a_hard_linked_target_is_refused(self) -> None:
        victim = self.outside / "secret.txt"
        victim.write_text("outside content\n", encoding="utf-8")
        link = self.root / "innocent.txt"
        try:
            os.link(victim, link)
        except (OSError, NotImplementedError, AttributeError):  # pragma: no cover
            self.skipTest("this platform cannot create a hard link here")
        self.assertGreater(os.lstat(link).st_nlink, 1)
        # Refused as an existing target either way, and the hard-link policy
        # is what keeps a multiply-linked file out of the write path at all.
        _, run = self.refused(link, "replaced\n", "create")
        self.assertIsNotNone(run.error)
        self.assertEqual("outside content\n", victim.read_text(encoding="utf-8"))

    def test_a_junction_in_the_parent_chain_never_completes_a_run(self) -> None:
        inside = self.root / "level0"
        inside.mkdir()
        (self.outside / "decoy.txt").write_text("decoy\n", encoding="utf-8")
        shutil.rmtree(inside)
        if not make_directory_link(self.outside, inside):  # pragma: no cover
            self.skipTest("this platform cannot create a directory link")
        self.refused(self.root / "level0" / "planted.txt")
        self.assertFalse((self.outside / "planted.txt").exists())

    def test_a_parent_repointed_after_validation_writes_nothing_outside(self) -> None:
        """The window between validating the chain and creating the temp."""
        inside = self.root / "level0"
        inside.mkdir()
        target = inside / "notes.txt"
        state = {"done": False}
        original = LocalFileWriteConnector._chain

        def patched(connector, resolved):
            result = original(connector, resolved)
            if not state["done"]:
                state["done"] = True
                shutil.rmtree(inside)
                if not make_directory_link(self.outside, inside):  # pragma: no cover
                    self.skipTest("this platform cannot create a directory link")
            return result

        task = write_task(target)
        with mock.patch.object(LocalFileWriteConnector, "_chain", patched):
            result = self.execute_prepared(task)
        self.assertTrue(state["done"], "the race window was never reached")
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertFalse((self.outside / "notes.txt").exists())

    def test_a_parent_repointed_after_it_is_pinned_cannot_move_the_write(self) -> None:
        """P1: the destination is bound to the parent OBJECT, not to its name.

        The window this closes is the one AFTER every check has passed. The
        chain is validated, the parent is pinned, and only then is the
        parent's NAME taken away and re-pointed at a directory outside the
        roots - a junction needs no privilege, so this is a race an attacker
        can really run. A destination named by pathname would resolve through
        that junction at the syscall and land outside, and re-checking the
        pathname first would not help: a check has a window after it.

        Both halves of the mutation are asserted. The temporary must never
        appear outside the roots (the create is rooted at the pinned parent
        too, so no byte is ever written through the planted name), and the
        installed file must land in the directory OBJECT that was approved.
        """
        parent = self.root / "docs"
        parent.mkdir()
        target = parent / "notes.txt"
        moved = self.root / "docs-moved"
        state: dict[str, Any] = {"swapped": False, "outside_at_install": None}
        create = LocalFileWriteConnector._create_temporary
        install = LocalFileWriteConnector._install_from_handle

        def patched_create(connector, parent_fd, parent_handle, temporary):
            if not state["swapped"]:
                state["swapped"] = True
                # Only the parent is pinned at this instant - no temporary
                # exists yet - so the swap is one the platform really allows.
                os.rename(parent, moved)
                if not make_directory_link(self.outside, parent):  # pragma: no cover
                    self.skipTest("this platform cannot create a directory link")
            return create(connector, parent_fd, parent_handle, temporary)

        def patched_install(connector, descriptor, parent_fd, parent_handle, leaf):
            state["outside_at_install"] = sorted(
                entry.name for entry in self.outside.iterdir()
            )
            return install(connector, descriptor, parent_fd, parent_handle, leaf)

        with mock.patch.object(
            LocalFileWriteConnector, "_create_temporary", patched_create
        ), mock.patch.object(
            LocalFileWriteConnector, "_install_from_handle", patched_install
        ):
            result = self.execute_prepared(write_task(target, "authorized\n"))
        self.assertTrue(state["swapped"], "the race window was never reached")
        self.assertIs(OutcomeStatus.SUCCESS, result.outcome, msg=str(result.error))
        self.assertEqual(
            [],
            sorted(entry.name for entry in self.outside.iterdir()),
            "the write escaped the approved root through the planted parent",
        )
        self.assertEqual(
            [], state["outside_at_install"], "the temporary was written outside"
        )
        self.assertEqual(
            "authorized\n", (moved / "notes.txt").read_text(encoding="utf-8")
        )

    @unittest.skipUnless(
        local_files._WINDOWS_HANDLE_INSTALL, "no Windows handle-bound route"
    )
    def test_the_windows_mutation_names_only_a_leaf_under_the_pinned_parent(
        self,
    ) -> None:
        """The same invariant stated structurally rather than raced once.

        Windows cannot walk the chain by directory handle, so the whole
        argument rests on this: the create and the rename both take the
        VERIFIED parent handle as their NT ``RootDirectory`` and supply a
        bare leaf, leaving nothing in the destination for the object manager
        to re-resolve. If either one ever named a directory again, the race
        above would be back and this fails first.
        """
        pinned: list[int] = []
        created: list[tuple[int, str]] = []
        installed: list[tuple[int, str]] = []
        open_parent = LocalFileWriteConnector._open_windows_parent
        real_create = local_files._windows_create_in_parent
        real_install = local_files._windows_install_by_handle

        def watched_open(connector, directory):
            handle = open_parent(connector, directory)
            pinned.append(handle)
            return handle

        def watched_create(parent_handle, leaf):
            created.append((parent_handle, leaf))
            return real_create(parent_handle, leaf)

        def watched_install(descriptor, parent_handle, leaf):
            installed.append((parent_handle, leaf))
            return real_install(descriptor, parent_handle, leaf)

        with mock.patch.object(
            LocalFileWriteConnector, "_open_windows_parent", watched_open
        ), mock.patch.object(
            local_files, "_windows_create_in_parent", watched_create
        ), mock.patch.object(
            local_files, "_windows_install_by_handle", watched_install
        ):
            self.completed(self.root / "docs.txt")
        self.assertEqual(1, len(pinned), "the parent was not pinned exactly once")
        self.assertEqual(1, len(created))
        self.assertEqual(1, len(installed))
        self.assertEqual("docs.txt", installed[0][1])
        self.assertTrue(created[0][1].startswith(LOCAL_WRITE_TEMP_PREFIX))
        for root, leaf in created + installed:
            self.assertEqual(pinned[0], root, "an unpinned root was used")
            self.assertEqual(leaf, Path(leaf).name, f"not a bare leaf: {leaf}")
            for separator in ("\\", "/", ":"):
                self.assertNotIn(separator, leaf)

    def test_a_root_swapped_for_a_link_elsewhere_writes_nothing(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        connector = LocalFileWriteConnector(
            allowed_roots=(workspace,), default_working_directory=workspace
        )
        shutil.rmtree(workspace)
        if not make_directory_link(self.outside, workspace):  # pragma: no cover
            self.skipTest("this platform cannot create a directory link")
        task = write_task(workspace / "planted.txt")
        record = connector.pre_execution_record(task)
        if record is not None:
            task.local_write = record
        result = connector.execute(task, SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertFalse((self.outside / "planted.txt").exists())

    def test_a_root_with_no_recorded_identity_writes_nothing(self) -> None:
        connector = LocalFileWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        connector._root_identity = {}
        task = write_task(self.root / "notes.txt")
        record = connector.pre_execution_record(task)
        if record is not None:
            task.local_write = record
        result = connector.execute(task, SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertFalse((self.root / "notes.txt").exists())

    def test_a_working_directory_outside_the_roots_is_refused(self) -> None:
        task = write_task(self.root / "notes.txt")
        task.context["working_directory"] = str(self.outside)
        with self.assertRaises(AttestationError) as caught:
            self.connector.resource_boundary(task)
        self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)

    def test_a_working_directory_that_does_not_exist_is_refused(self) -> None:
        task = write_task(self.root / "notes.txt")
        task.context["working_directory"] = str(self.root / "nope")
        with self.assertRaises(AttestationError):
            self.connector.resource_boundary(task)

    @unittest.skipUnless(
        local_files._DIRECTORY_HANDLE_TRAVERSAL, "no directory-handle traversal"
    )
    def test_the_posix_parent_walk_is_no_follow_at_every_step(self) -> None:
        """O_NOFOLLOW is load-bearing for a write and has no inode backstop.

        A read binds the opened file's (st_dev, st_ino) against a prior stat,
        so a followed component is still caught. A create has no prior inode,
        so this flag is the only thing refusing a component that became a
        link.
        """
        (self.root / "docs").mkdir()
        seen: list[int] = []
        real_open = local_files.os.open

        def recording(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is not None or Path(str(path)) == self.root:
                seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(local_files.os, "open", recording):
            self.execute_prepared(write_task(self.root / "docs" / "notes.txt"))
        self.assertTrue(seen, "no handle-relative open was observed")
        for flags in seen:
            self.assertTrue(flags & os.O_NOFOLLOW, f"open without O_NOFOLLOW: {flags}")


# --------------------------------------------------------------------------
# 6. Atomicity
# --------------------------------------------------------------------------


class AtomicityTests(LocalWriteTestCase):
    """The install itself, and what it can and cannot be made to do."""

    ATTACKER = "ATTACKER-SUBSTITUTED-BYTES-2c9d"
    THIRD_PARTY = "THIRD-PARTY-CONTENT-5e10"

    def orphans(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                entry.name
                for entry in self.root.iterdir()
                if entry.name.startswith(LOCAL_WRITE_TEMP_PREFIX)
            )
        )

    def race_at_install(self, plant, target, content="authorized\n"):
        """Run one write with ``plant`` firing at the mutation boundary.

        ``_install_from_handle`` is the seam on purpose: it is the last thing
        before the mutating syscall, so a race planted here is strictly
        inside the only window that remains. Nothing earlier would prove
        anything - the pre-write checks already cover that ground.
        """
        original = LocalFileWriteConnector._install_from_handle
        state = {"done": False}

        def patched(connector, descriptor, parent_fd, parent_handle, leaf):
            if not state["done"]:
                state["done"] = True
                plant(Path(target).parent, leaf)
            return original(connector, descriptor, parent_fd, parent_handle, leaf)

        with mock.patch.object(
            LocalFileWriteConnector, "_install_from_handle", patched
        ):
            result = self.execute_prepared(write_task(target, content, "create"))
        self.assertTrue(state["done"], "the race window was never reached")
        return result

    # -- P1-A: the source of the install cannot be substituted --------------

    def test_the_installed_object_comes_from_the_handle_not_a_name(self) -> None:
        """P1-A, stated as the property rather than as a refusal.

        The temporary's NAME is swapped for an attacker's file in the last
        instant before the install. Under a pathname rename that file would
        become the destination. Here the source is the open descriptor, so
        the swap changes nothing about what gets installed - the write simply
        succeeds and the destination holds the authorized bytes.
        """
        temp_names: list[str] = []

        def swap(parent, leaf):
            for entry in parent.iterdir():
                if entry.name.startswith(LOCAL_WRITE_TEMP_PREFIX):
                    temp_names.append(entry.name)
                    decoy = parent / "decoy.tmp"
                    decoy.write_text(self.ATTACKER, encoding="utf-8")
                    try:
                        os.replace(decoy, entry)
                    except OSError:
                        # Windows keeps our handle open, so the name cannot
                        # even be taken. Either way the source is the handle.
                        decoy.unlink()

        target = self.root / "notes.txt"
        result = self.race_at_install(swap, target)
        self.assertTrue(temp_names, "no temporary was observed")
        self.assertIs(OutcomeStatus.SUCCESS, result.outcome, msg=str(result.error))
        self.assertEqual("authorized\n", target.read_text(encoding="utf-8"))
        self.assertNotIn(self.ATTACKER, target.read_text(encoding="utf-8"))

    def test_the_install_never_renames_or_links_by_pathname(self) -> None:
        """P1-A as a structural property, provable on either platform.

        A source pathname is re-resolved inside the kernel at the syscall, so
        any install that names its source is substitutable no matter what was
        checked beforehand. This asserts the install never does that: no
        unconditional ``os.replace`` at all, and any ``os.link`` sourced only
        from ``/proc/self/fd``, which always resolves to the object behind
        our own descriptor.
        """
        linked: list[str] = []
        real_link = local_files.os.link

        def forbidden_replace(*args, **kwargs):
            raise AssertionError("the install used an unconditional pathname replace")

        def watched_link(source, destination, **kwargs):
            linked.append(str(source))
            return real_link(source, destination, **kwargs)

        with mock.patch.object(
            local_files.os, "replace", forbidden_replace
        ), mock.patch.object(local_files.os, "link", watched_link):
            self.completed(self.root / "notes.txt", "content\n", "create")
        for source in linked:
            self.assertTrue(
                source.startswith(str(local_files._PROC_FD)),
                f"install linked from a substitutable pathname: {source}",
            )

    def test_a_platform_without_a_handle_install_fails_closed(self) -> None:
        """No handle-sourced primitive means no write at all.

        The only alternative would be a pathname rename, which is exactly the
        substitutable operation the design refuses to perform.
        """
        target = self.root / "notes.txt"
        with mock.patch.object(
            local_files, "_WINDOWS_HANDLE_INSTALL", False
        ), mock.patch.object(local_files, "_PROC_FD_INSTALL", False):
            result = self.execute_prepared(write_task(target))
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertIn("install a file from an open handle", result.error.message)
        self.assertFalse(target.exists())

    def test_the_install_route_is_a_handle_route_on_this_platform(self) -> None:
        self.assertTrue(
            local_files._WINDOWS_HANDLE_INSTALL or local_files._PROC_FD_INSTALL,
            "no handle-sourced install primitive was detected",
        )

    # -- P1-B: the temporary's BYTES cannot be substituted either -----------

    def race_a_second_writer(self, target, content, attacker=None):
        """Run one write, racing a writable second open of the TEMPORARY.

        The seam is the install, which is after the bytes are written and
        fsynced and while the authoritative descriptor is still open - the
        exact window in which the approved bytes are complete but not yet
        installed. Nothing here touches a pathname the connector uses: the
        temporary is located by asking the kernel what object the connector's
        own descriptor refers to, so the race is against that object itself.
        """
        import msvcrt

        state: dict[str, Any] = {"reached": False, "opened": None, "error": None}
        original = LocalFileWriteConnector._install_from_handle

        def patched(connector, descriptor, parent_fd, parent_handle, leaf):
            state["reached"] = True
            # The authoritative handle is still open at this instant; if it
            # were not, this raises and the window under test does not exist.
            os.fstat(descriptor)
            temporary = local_files._final_path_from_handle(descriptor)
            self.assertIsNotNone(temporary, "the temporary object has no path")
            state["temporary"] = temporary
            handle, error = open_for_write(temporary)
            state["opened"] = handle is not None
            state["error"] = error
            if handle is not None:
                second = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_WRONLY)
                try:
                    if attacker is not None:
                        os.write(second, attacker)
                        os.fsync(second)
                finally:
                    os.close(second)
            return original(connector, descriptor, parent_fd, parent_handle, leaf)

        with mock.patch.object(
            LocalFileWriteConnector, "_install_from_handle", patched
        ):
            result = self.execute_prepared(write_task(target, content, "create"))
        self.assertTrue(state["reached"], "the install seam was never reached")
        return result, state

    @unittest.skipUnless(
        local_files._WINDOWS_HANDLE_INSTALL, "no Windows handle-bound route"
    )
    def test_no_second_writer_can_reach_the_temporary_before_install(self) -> None:
        """P1-B: the approved bytes are the installed bytes, guaranteed by the OS.

        Proving the source object cannot be substituted is only half of
        exact-byte authority. The other half is that the object's CONTENTS
        cannot be substituted either, and identity answers nothing there: a
        rewritten temporary is still the same object, with the same
        (device, index), passing every check this code could make after the
        fact. So the guarantee is placed where it has no window - the share
        mode fixed when the object is created - and this exercises it: a real
        ``CreateFileW`` for ``GENERIC_WRITE``, on the real object, in the real
        window, is refused by the kernel with a sharing violation.

        ``ERROR_SHARING_VIOLATION`` specifically, not merely "some failure":
        an absent name would answer ``ERROR_FILE_NOT_FOUND`` and would mean
        the race never reached the object at all.
        """
        target = self.root / "notes.txt"
        result, state = self.race_a_second_writer(target, "authorized\n")
        self.assertFalse(
            state["opened"], "a second writable handle reached the temporary"
        )
        self.assertEqual(
            _ERROR_SHARING_VIOLATION,
            state["error"],
            "the writable open failed for the wrong reason",
        )
        self.assertIs(OutcomeStatus.SUCCESS, result.outcome, msg=str(result.error))
        self.assertEqual(b"authorized\n", target.read_bytes())

    @unittest.skipUnless(
        local_files._WINDOWS_HANDLE_INSTALL, "no Windows handle-bound route"
    )
    def test_granting_write_sharing_would_install_substituted_bytes(self) -> None:
        """The same race with the share mode put back the way it was.

        This is the mutation that keeps the test above honest. Re-granting
        ``FILE_SHARE_WRITE`` on the temporary is the ONLY change, and it is
        enough for the attacker's open to succeed and for the connector to
        report success over bytes nobody attested or approved - which is the
        original P1 exactly. If the guarantee above ever degrades into a
        constant comparison or an after-the-fact digest check, this stops
        distinguishing the two share modes and fails.
        """
        target = self.root / "notes.txt"
        attacker = self.ATTACKER.encode("utf-8")
        with mock.patch.object(local_files, "_TEMPORARY_SHARE", _SHARE_EVERYTHING):
            result, state = self.race_a_second_writer(
                target, "authorized\n", attacker=attacker
            )
        self.assertTrue(
            state["opened"], "write sharing was granted but the open still failed"
        )
        self.assertIs(OutcomeStatus.SUCCESS, result.outcome, msg=str(result.error))
        self.assertEqual(
            attacker,
            target.read_bytes(),
            "the mutation did not reproduce the substituted-byte install",
        )

    @unittest.skipUnless(
        local_files._WINDOWS_HANDLE_INSTALL, "no Windows handle-bound route"
    )
    def test_the_temporary_shares_nothing_at_all(self) -> None:
        """The share mode is a security parameter, so it is stated as one.

        The behavioural proof is above; this only pins that no share bit is
        granted, so widening it is a deliberate edit rather than a silent
        one. ``FILE_SHARE_DELETE`` is included: the rename runs through the
        temporary's own handle and needs ``DELETE`` ACCESS, never DELETE
        SHARING, which is why none is granted.
        """
        self.assertEqual(0, local_files._TEMPORARY_SHARE)
        self.assertTrue(local_files._TEMPORARY_ACCESS & 0x00010000, "no DELETE access")

    # -- P1-A/C: the destination precondition is enforced, not checked ------

    def test_a_destination_that_appears_late_is_never_clobbered(self) -> None:
        """A create must still mean absent AT THE MUTATION, not before it.

        The destination is planted in the last instant, past every check this
        code makes. Only the syscall's own semantics can refuse it, and they
        do: the install refuses to replace an existing name.
        """
        target = self.root / "notes.txt"
        result = self.race_at_install(
            lambda parent, leaf: self.seed("notes.txt", self.THIRD_PARTY), target
        )
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual(self.THIRD_PARTY, target.read_text(encoding="utf-8"))

    def test_no_late_race_leaves_anything_but_absent_or_intended(self) -> None:
        """Whatever is planted, the destination is one of exactly two files."""
        authorized = "the authorized bytes\n"

        def swap(parent, leaf):
            for entry in parent.iterdir():
                if entry.name.startswith(LOCAL_WRITE_TEMP_PREFIX):
                    decoy = parent / "decoy.tmp"
                    decoy.write_text(self.ATTACKER, encoding="utf-8")
                    try:
                        os.replace(decoy, entry)
                    except OSError:
                        decoy.unlink()

        plants = {
            "substituted_source": swap,
            "destination_appeared": lambda p, leaf: self.seed(
                "notes.txt", self.THIRD_PARTY
            ),
            "no_race": lambda p, leaf: None,
        }
        for label, plant in plants.items():
            with self.subTest(race=label):
                target = self.root / "notes.txt"
                if target.exists():
                    target.unlink()
                self.race_at_install(plant, target, authorized)
                observed = (
                    target.read_text(encoding="utf-8") if target.exists() else None
                )
                self.assertIn(observed, (None, authorized, self.THIRD_PARTY))
                if observed is not None:
                    self.assertNotIn(self.ATTACKER, observed)

    # -- ordinary atomicity -------------------------------------------------

    def test_the_destination_is_never_opened_for_writing(self) -> None:
        """Not "it happens to be installed" - it is never opened to write."""
        target = self.root / "notes.txt"
        opens: list[tuple[str, int]] = []
        real_open = local_files.os.open

        def recording(path, flags, *args, **kwargs):
            opens.append((str(path), flags))
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(local_files.os, "open", recording):
            self.completed(target, "content\n", "create")
        writable = os.O_WRONLY | os.O_RDWR
        for name, flags in opens:
            if flags & writable:
                self.assertIn(
                    LOCAL_WRITE_TEMP_PREFIX,
                    Path(name).name,
                    f"a non-temporary path was opened for writing: {name}",
                )
                self.assertNotEqual(target.name, Path(name).name)

    def test_the_temporary_lives_in_the_destination_directory(self) -> None:
        (self.root / "docs").mkdir()
        self.completed(self.root / "docs" / "notes.txt")
        # Nothing is left behind anywhere, and the install was same-directory
        # by construction: a cross-directory install could not be atomic.
        self.assertEqual((), self.orphans())
        self.assertEqual(
            [],
            [
                entry.name
                for entry in (self.root / "docs").iterdir()
                if entry.name.startswith(LOCAL_WRITE_TEMP_PREFIX)
            ],
        )

    def test_the_temporary_is_flushed_before_it_is_installed(self) -> None:
        order: list[str] = []
        real_fsync = local_files.os.fsync
        original = LocalFileWriteConnector._install_from_handle

        def fsync(descriptor):
            order.append("fsync")
            return real_fsync(descriptor)

        def install(connector, *args, **kwargs):
            order.append("install")
            return original(connector, *args, **kwargs)

        with mock.patch.object(
            local_files.os, "fsync", fsync
        ), mock.patch.object(
            LocalFileWriteConnector, "_install_from_handle", install
        ):
            self.completed(self.root / "notes.txt")
        self.assertIn("fsync", order)
        self.assertIn("install", order)
        self.assertLess(order.index("fsync"), order.index("install"))

    def test_a_create_leaves_the_destination_with_exactly_one_name(self) -> None:
        """The link's other half is dropped, or the file could never be
        written again under this connector's own hard-link policy."""
        self.completed(self.root / "notes.txt", "content\n", "create")
        self.assertEqual(1, os.stat(self.root / "notes.txt").st_nlink)
        self.assertEqual((), self.orphans())

    def test_a_failing_write_leaves_no_destination_at_all(self) -> None:
        def exploding(*args, **kwargs):
            raise OSError(errno.ENOSPC, "full")

        task = self.prepared(write_task(self.root / "notes.txt", "new\n", "create"))
        with mock.patch.object(local_files.os, "write", exploding):
            with self.assertRaises(OSError):
                self.connector.execute(task, SessionState())
        self.assertFalse((self.root / "notes.txt").exists())

    def test_the_destination_is_only_ever_absent_or_intended(self) -> None:
        """Whatever fails, the file is the whole new one or not there."""
        intended = "the new content\n"
        target = self.root / "notes.txt"
        for failing in ("write", "fsync"):
            with self.subTest(failing=failing):

                def exploding(*args, **kwargs):
                    raise OSError(errno.EIO, "boom")

                task = self.prepared(write_task(target, intended, "create"))
                with mock.patch.object(local_files.os, failing, exploding):
                    with self.assertRaises(OSError):
                        self.connector.execute(task, SessionState())
                observed = (
                    target.read_text(encoding="utf-8") if target.exists() else None
                )
                self.assertIn(observed, (None, intended))

    def test_a_failing_install_leaves_no_destination(self) -> None:
        def exploding(connector, *args, **kwargs):
            raise OSError(errno.EIO, "install blew up")

        task = self.prepared(write_task(self.root / "notes.txt", "new\n", "create"))
        with mock.patch.object(
            LocalFileWriteConnector, "_install_from_handle", exploding
        ):
            with self.assertRaises(OSError):
                self.connector.execute(task, SessionState())
        self.assertFalse((self.root / "notes.txt").exists())

    def test_a_successful_write_leaves_no_temporary_behind(self) -> None:
        self.completed(self.root / "notes.txt")
        self.assertEqual((), self.orphans())

    def test_an_orphan_temporary_is_reported_and_never_deleted(self) -> None:
        orphan = self.root / f"{LOCAL_WRITE_TEMP_PREFIX}deadbeef.tmp"
        orphan.write_text("partial\n", encoding="utf-8")
        task = write_task(self.root / "notes.txt")
        self.assertEqual((orphan.name,), self.connector.orphan_temporaries(task))
        self.assertTrue(orphan.exists(), "an orphan must never be auto-deleted")


# --------------------------------------------------------------------------
# P1-B. The durable approved record is the authority; execution only agrees
# --------------------------------------------------------------------------


class PreparedAuthorityTests(LocalWriteTestCase):
    """Execution may never manufacture a new authority for itself.

    Without this, a destination that changed between approval and execution
    would simply be re-observed at execute time and become an accepted
    pre-state - a self-granted authority for an operation nobody approved.
    """

    def test_execution_without_a_prepared_authority_is_refused(self) -> None:
        task = write_task(self.root / "notes.txt")
        result = self.connector.execute(task, SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertIn("no durable approved authority", result.error.message)
        self.assertFalse((self.root / "notes.txt").exists())

    def test_a_record_that_is_not_prepared_is_not_authority(self) -> None:
        task = self.prepared(write_task(self.root / "notes.txt"))
        for status in ("written", "refused", "unknown"):
            with self.subTest(status=status):
                task.local_write = replace(task.local_write, status=status)
                result = self.connector.execute(task, SessionState())
                self.assertIs(OutcomeStatus.ERROR, result.outcome)
                self.assertFalse((self.root / "notes.txt").exists())

    def test_a_malformed_prepared_record_is_not_authority(self) -> None:
        task = self.prepared(write_task(self.root / "notes.txt"))
        task.local_write = replace(task.local_write, intended_sha256="NOT-A-DIGEST")
        result = self.connector.execute(task, SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertFalse((self.root / "notes.txt").exists())

    def test_a_destination_appearing_after_preparation_refuses(self) -> None:
        """absent -> present between preparation and execution."""
        target = self.root / "notes.txt"
        task = self.prepared(write_task(target, "authorized\n", "create"))
        self.seed("notes.txt", "someone got there first\n")
        result = self.connector.execute(task, SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual(
            "someone got there first\n", target.read_text(encoding="utf-8")
        )

    def test_execution_never_adopts_the_newly_observed_state(self) -> None:
        """The refusal is the point, but so is what the record still says."""
        target = self.root / "notes.txt"
        task = self.prepared(write_task(target, "authorized\n", "create"))
        approved = task.local_write
        self.seed("notes.txt", "appeared\n")
        self.connector.execute(task, SessionState())
        # The approved authority is untouched: nothing re-derived from the
        # filesystem was written back over it.
        self.assertEqual(approved, task.local_write)
        self.assertFalse(task.local_write.pre_present)

    def test_a_tampered_prepared_path_refuses(self) -> None:
        task = self.prepared(write_task(self.root / "notes.txt"))
        task.local_write = replace(task.local_write, relative_path="elsewhere.txt")
        result = self.connector.execute(task, SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertIn("no longer matches the authority", result.error.message)
        self.assertFalse((self.root / "notes.txt").exists())
        self.assertFalse((self.root / "elsewhere.txt").exists())

    def test_changed_intended_bytes_after_preparation_refuse(self) -> None:
        """Same length, different bytes: only the digest can tell."""
        task = self.prepared(write_task(self.root / "notes.txt", "aaaaa\n", "create"))
        task.write_content = "bbbbb\n"
        result = self.connector.execute(task, SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertIn("no longer matches the authority", result.error.message)
        self.assertFalse((self.root / "notes.txt").exists())

    def test_a_changed_intended_size_after_preparation_refuses(self) -> None:
        task = self.prepared(write_task(self.root / "notes.txt", "short\n", "create"))
        task.write_content = "a much longer body than before\n"
        result = self.connector.execute(task, SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertFalse((self.root / "notes.txt").exists())

    def test_a_tampered_prepared_mode_refuses(self) -> None:
        task = self.prepared(write_task(self.root / "notes.txt"))
        task.local_write = replace(
            task.local_write, mode="overwrite", pre_present=True,
            pre_sha256="a" * 64, pre_byte_size=3,
        )
        result = self.connector.execute(task, SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertFalse((self.root / "notes.txt").exists())

    def test_the_same_record_feeds_approval_execution_and_diagnosis(self) -> None:
        """One authority, four surfaces - fingerprint to diagnosis."""
        task = write_task(self.root / "notes.txt", "authorized\n", "create")
        run = self.request(task)
        stored = self.memory.load_task(task.id)
        prepared_now = self.connector.pre_execution_record(stored)
        self.assertEqual(
            self.connector._authority_of_record(prepared_now),
            self.connector._authority_of(self.connector._intent(stored)),
        )
        decided = self.approve(task, run)
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        final = self.memory.load_task(task.id).local_write
        self.assertEqual(
            self.connector._authority_of_record(prepared_now),
            self.connector._authority_of_record(final),
        )


# --------------------------------------------------------------------------
# P2. Preparation is fail-closed, not best effort
# --------------------------------------------------------------------------


class PreparationFailClosedTests(LocalWriteTestCase):
    """No write boundary is crossed without durable approved metadata."""

    def drive(self, patch_target, replacement):
        app = create_app(
            settings=Settings(
                memory_database=self.root / "failclosed.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(self.connector,),
        )
        task = write_task(self.root / "notes.txt", MARKER + "\n", "create")
        run = app.core.run(task)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
        grant = PermissionGrant.for_request(run.permission_request)
        calls = {"execute": 0}
        original_execute = LocalFileWriteConnector.execute

        def counting(connector, task_, session):
            calls["execute"] += 1
            return original_execute(connector, task_, session)

        with mock.patch.object(
            LocalFileWriteConnector, patch_target, replacement
        ), mock.patch.object(LocalFileWriteConnector, "execute", counting):
            decided = app.core.resume(run.session.id, task.id, grant=grant)
        return app, task, decided, calls

    def assert_refused_without_touching_anything(self, app, task, decided, calls):
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.EGRESS_REFUSED, decided.error.code)
        self.assertEqual(0, calls["execute"], "the connector boundary was crossed")
        self.assertIsNone(
            app.memory.latest_execution_attempt(task.id),
            "an execution attempt was opened despite the refusal",
        )
        self.assertFalse((self.root / "notes.txt").exists())
        self.assertNotIn(MARKER, json.dumps(decided.error.to_dict(), default=str))

    def test_a_raising_preparation_refuses_the_write(self) -> None:
        def exploding(connector, task):
            raise RuntimeError(f"preparation blew up carrying {MARKER}")

        app, task, decided, calls = self.drive("pre_execution_record", exploding)
        self.assert_refused_without_touching_anything(app, task, decided, calls)
        self.assertEqual("preparation_failed", decided.error.details.get("reason"))
        # Only the exception TYPE survives. The connector was asked about the
        # bytes being written, so its message is exactly where those bytes
        # could plausibly appear - and it is dropped rather than scrubbed.
        self.assertEqual("RuntimeError", decided.error.details.get("exception_type"))
        self.assertNotIn("blew up carrying", json.dumps(decided.error.to_dict()))

    def test_a_preparation_that_yields_nothing_refuses_the_write(self) -> None:
        app, task, decided, calls = self.drive(
            "pre_execution_record", lambda connector, task: None
        )
        self.assert_refused_without_touching_anything(app, task, decided, calls)
        self.assertEqual("no_prepared_record", decided.error.details.get("reason"))

    def test_a_malformed_prepared_record_refuses_the_write(self) -> None:
        app, task, decided, calls = self.drive(
            "pre_execution_record", lambda connector, task: LocalWriteRecord()
        )
        self.assert_refused_without_touching_anything(app, task, decided, calls)
        self.assertEqual(
            "malformed_prepared_record", decided.error.details.get("reason")
        )

    def test_a_wrongly_typed_prepared_record_refuses_the_write(self) -> None:
        app, task, decided, calls = self.drive(
            "pre_execution_record", lambda connector, task: {"relative_path": "x"}
        )
        self.assert_refused_without_touching_anything(app, task, decided, calls)

    def test_a_connector_that_cannot_prepare_refuses_the_write(self) -> None:
        class Unpreparable(LocalFileWriteConnector):
            manifest = replace(
                LocalFileWriteConnector.manifest, name="unpreparable-writer"
            )
            pre_execution_record = None

        connector = Unpreparable(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        app = create_app(
            settings=Settings(
                memory_database=self.root / "unpreparable.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(connector,),
        )
        task = write_task(self.root / "notes.txt")
        run = app.core.run(task)
        grant = PermissionGrant.for_request(run.permission_request)
        decided = app.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.EGRESS_REFUSED, decided.error.code)
        self.assertEqual(
            "connector_cannot_prepare", decided.error.details.get("reason")
        )
        self.assertIsNone(app.memory.latest_execution_attempt(task.id))
        self.assertFalse((self.root / "notes.txt").exists())

    def test_a_failed_preparation_checkpoint_refuses_the_write(self) -> None:
        """Storage refusing the persist must stop the write, not be ignored."""
        app = create_app(
            settings=Settings(
                memory_database=self.root / "nopersist.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(self.connector,),
        )
        task = write_task(self.root / "notes.txt")
        run = app.core.run(task)
        grant = PermissionGrant.for_request(run.permission_request)
        real_persist = app.memory.persist_checkpoint
        calls = {"execute": 0}
        original_execute = LocalFileWriteConnector.execute

        def refusing(session, task_, event_type, payload=None, pending_request=None):
            if event_type == "local_write_prepared":
                return False
            return real_persist(session, task_, event_type, payload, pending_request)

        def counting(connector, task_, session):
            calls["execute"] += 1
            return original_execute(connector, task_, session)

        with mock.patch.object(
            app.memory, "persist_checkpoint", refusing
        ), mock.patch.object(LocalFileWriteConnector, "execute", counting):
            decided = app.core.resume(run.session.id, task.id, grant=grant)
        self.assertNotEqual(RunStatus.COMPLETED, decided.status)
        self.assertEqual(0, calls["execute"])
        self.assertIsNone(app.memory.latest_execution_attempt(task.id))
        self.assertFalse((self.root / "notes.txt").exists())


# --------------------------------------------------------------------------
# 7. H2 - the unknown-on-raise rule
# --------------------------------------------------------------------------


class RaisingWriteConnector(LocalFileWriteConnector):
    """A write connector that raises BEFORE touching the disk."""

    manifest = replace(LocalFileWriteConnector.manifest, name="raising-writer")

    def execute(self, task: Task, session: SessionState) -> ToolResult:  # type: ignore[override]
        raise RuntimeError("the write blew up mid-flight")


class WroteThenRaisedConnector(LocalFileWriteConnector):
    """A write connector that really writes and THEN raises.

    The hardest recovery case and the reason a durable pre-execution record
    exists: the bytes landed, the result never came back, and only the two
    attested states can say so.
    """

    manifest = replace(LocalFileWriteConnector.manifest, name="wrote-then-raised")

    def execute(self, task: Task, session: SessionState) -> ToolResult:  # type: ignore[override]
        super().execute(task, session)
        raise RuntimeError("the result never made it back")


class ExecutionJournalTests(LocalWriteTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.raiser = RaisingWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.app = create_app(
            settings=Settings(
                memory_database=self.root / "journal.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(self.raiser,),
        )
        self.core = self.app.core
        self.memory = self.app.memory

    def test_the_effect_set_is_exactly_the_three_unknowable_effects(self) -> None:
        from jarvis.core import _UNKNOWABLE_ON_RAISE_EFFECTS

        self.assertEqual(
            {
                SideEffect.EXTERNAL_EGRESS,
                SideEffect.LOCAL_WRITE,
                SideEffect.DESTRUCTIVE,
            },
            set(_UNKNOWABLE_ON_RAISE_EFFECTS),
        )

    def test_a_raising_write_is_in_doubt_and_never_a_clean_retry(self) -> None:
        task = write_task(self.root / "notes.txt")
        run = self.request(task)
        decided = self.approve(task, run)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, decided.error.code)
        self.assertFalse(decided.error.retryable)
        attempt = self.memory.latest_execution_attempt(task.id)
        self.assertIs(AttemptState.UNKNOWN_OUTCOME, attempt.state)

    def test_a_task_in_doubt_is_never_replayed_on_resume(self) -> None:
        task = write_task(self.root / "notes.txt")
        run = self.request(task)
        self.approve(task, run)
        calls = {"count": 0}
        original = RaisingWriteConnector.execute

        def counting(connector, task_, session):
            calls["count"] += 1
            return original(connector, task_, session)

        with mock.patch.object(RaisingWriteConnector, "execute", counting):
            again = self.core.resume(run.session.id, task.id)
        self.assertEqual(0, calls["count"], "an in-doubt write was replayed")
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, again.error.code)

    def test_the_task_is_left_recoverable_not_fabricated_terminal(self) -> None:
        task = write_task(self.root / "notes.txt")
        run = self.request(task)
        self.approve(task, run)
        stored = self.memory.load_task(task.id)
        self.assertNotIn(
            stored.status,
            (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED),
        )

    def test_a_harmless_effect_still_fails_cleanly(self) -> None:
        """The rule must not generalize to every connector that raises."""

        class Exploding(DummyConnector):
            def execute(self, task, session):
                raise RuntimeError("boom")

        connector = Exploding(name="harmless-exploder")
        app = create_app(
            settings=Settings(
                memory_database=self.root / "harmless.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(connector,),
        )
        from tests.helpers import make_task

        run = app.core.run(make_task())
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIs(ErrorCode.CONNECTOR_FAILED, run.error.code)
        self.assertTrue(run.error.retryable)


# --------------------------------------------------------------------------
# 8. Unknown-outcome diagnosis
# --------------------------------------------------------------------------


class DiagnosisTests(LocalWriteTestCase):
    def record_for(self, task: Task, target: Path, prior: str | None, intended: str):
        content = intended.encode("utf-8")
        return LocalWriteRecord(
            relative_path=target.relative_to(self.root).as_posix(),
            mode="create" if prior is None else "overwrite",
            pre_present=prior is not None,
            pre_sha256=(
                "" if prior is None else hashlib.sha256(prior.encode()).hexdigest()
            ),
            pre_byte_size=0 if prior is None else len(prior.encode()),
            intended_sha256=hashlib.sha256(content).hexdigest(),
            intended_byte_size=len(content),
            status="unknown",
            written_at=utc_now(),
        )

    def test_a_destination_matching_the_intended_state_reads_as_landed(self) -> None:
        target = self.seed("notes.txt", "the new content\n")
        task = write_task(target, "the new content\n", "overwrite")
        task.local_write = self.record_for(task, target, "prior\n", "the new content\n")
        self.assertEqual(LOCAL_WRITE_LANDED, self.connector.diagnose_write(task))

    def test_a_destination_matching_the_prior_state_reads_as_not_landed(self) -> None:
        target = self.seed("notes.txt", "prior\n")
        task = write_task(target, "the new content\n", "overwrite")
        task.local_write = self.record_for(task, target, "prior\n", "the new content\n")
        self.assertEqual(LOCAL_WRITE_DID_NOT_LAND, self.connector.diagnose_write(task))

    def test_an_absent_destination_for_a_create_reads_as_not_landed(self) -> None:
        target = self.root / "notes.txt"
        task = write_task(target, "brand new\n", "create")
        task.local_write = self.record_for(task, target, None, "brand new\n")
        self.assertEqual(LOCAL_WRITE_DID_NOT_LAND, self.connector.diagnose_write(task))

    def test_a_third_party_change_reads_as_ambiguous(self) -> None:
        target = self.seed("notes.txt", "something else entirely\n")
        task = write_task(target, "the new content\n", "overwrite")
        task.local_write = self.record_for(task, target, "prior\n", "the new content\n")
        self.assertEqual(LOCAL_WRITE_AMBIGUOUS, self.connector.diagnose_write(task))

    def test_a_record_that_fails_the_contract_reads_as_ambiguous(self) -> None:
        target = self.seed("notes.txt", "whatever\n")
        task = write_task(target, "x\n", "overwrite")
        task.local_write = LocalWriteRecord()
        self.assertEqual(LOCAL_WRITE_AMBIGUOUS, self.connector.diagnose_write(task))

    def test_the_diagnosis_reaches_the_in_doubt_report(self) -> None:
        """The real exception path, with no record injected by the test.

        The metadata is durable BEFORE the boundary, so a raise that never
        touched the disk is diagnosed as such rather than as ambiguous.
        """
        raiser = RaisingWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        app = create_app(
            settings=Settings(
                memory_database=self.root / "diagnosed.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(raiser,),
        )
        task = write_task(self.root / "notes.txt")
        run = app.core.run(task)
        grant = PermissionGrant.for_request(run.permission_request)
        decided = app.core.resume(run.session.id, task.id, grant=grant)
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, decided.error.code)
        self.assertEqual(
            LOCAL_WRITE_DID_NOT_LAND,
            decided.error.details.get("local_write_diagnosis"),
        )

    # -- P2: the real exception path, nothing injected by the test ----------

    def in_doubt_run(self, connector, target, content="authorized\n", mode="create"):
        """Drive a real approved write to UNKNOWN_OUTCOME through AgentCore."""
        app = create_app(
            settings=Settings(
                memory_database=self.root / f"{connector.manifest.name}.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(connector,),
        )
        task = write_task(target, content, mode)
        run = app.core.run(task)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
        grant = PermissionGrant.for_request(run.permission_request)
        decided = app.core.resume(run.session.id, task.id, grant=grant)
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, decided.error.code)
        return app, task, run.session.id, decided

    def test_the_attested_metadata_is_durable_before_the_boundary(self) -> None:
        connector = RaisingWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        app, task, _, _ = self.in_doubt_run(connector, self.root / "notes.txt")
        # Reloaded from storage, not from the in-memory object.
        record = app.memory.load_task(task.id).local_write
        self.assertTrue(record.present)
        self.assertEqual("notes.txt", record.relative_path)
        self.assertEqual("create", record.mode)
        self.assertFalse(record.pre_present)
        self.assertEqual(
            hashlib.sha256("authorized\n".encode("utf-8")).hexdigest(),
            record.intended_sha256,
        )
        self.assertEqual(11, record.intended_byte_size)
        self.assertEqual("prepared", record.status)

    def test_a_raise_before_the_write_diagnoses_as_did_not_land(self) -> None:
        connector = RaisingWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        _, _, _, decided = self.in_doubt_run(connector, self.root / "notes.txt")
        self.assertEqual(
            LOCAL_WRITE_DID_NOT_LAND,
            decided.error.details.get("local_write_diagnosis"),
        )
        self.assertFalse((self.root / "notes.txt").exists())

    def test_a_raise_after_the_write_diagnoses_as_landed(self) -> None:
        connector = WroteThenRaisedConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        _, _, _, decided = self.in_doubt_run(connector, self.root / "notes.txt")
        self.assertEqual(
            LOCAL_WRITE_LANDED, decided.error.details.get("local_write_diagnosis")
        )
        self.assertEqual(
            "authorized\n", (self.root / "notes.txt").read_text(encoding="utf-8")
        )

    def test_a_third_party_state_after_a_raise_diagnoses_as_ambiguous(self) -> None:
        connector = WroteThenRaisedConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        app, task, session_id, _ = self.in_doubt_run(
            connector, self.root / "notes.txt"
        )
        self.seed("notes.txt", "someone else entirely\n")
        again = app.core.resume(session_id, task.id)
        self.assertEqual(
            LOCAL_WRITE_AMBIGUOUS, again.error.details.get("local_write_diagnosis")
        )

    def test_a_restart_diagnoses_from_storage_alone(self) -> None:
        """A fresh app over the same database - nothing in memory survives."""
        connector = WroteThenRaisedConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        _, task, session_id, _ = self.in_doubt_run(connector, self.root / "notes.txt")
        restarted = create_app(
            settings=Settings(
                memory_database=self.root / f"{connector.manifest.name}.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(
                WroteThenRaisedConnector(
                    allowed_roots=(self.root,), default_working_directory=self.root
                ),
            ),
        )
        after = restarted.core.resume(session_id, task.id)
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, after.error.code)
        self.assertEqual(
            LOCAL_WRITE_LANDED, after.error.details.get("local_write_diagnosis")
        )

    def test_the_in_doubt_path_never_replays_or_advances(self) -> None:
        connector = WroteThenRaisedConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        app, task, session_id, _ = self.in_doubt_run(
            connector, self.root / "notes.txt"
        )
        digest_before = hashlib.sha256(
            (self.root / "notes.txt").read_bytes()
        ).hexdigest()
        calls = {"count": 0}
        original = WroteThenRaisedConnector.execute

        def counting(self_, task_, session):
            calls["count"] += 1
            return original(self_, task_, session)

        with mock.patch.object(WroteThenRaisedConnector, "execute", counting):
            again = app.core.resume(session_id, task.id)
        self.assertEqual(0, calls["count"], "an in-doubt write was replayed")
        self.assertIs(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, again.error.code)
        stored = app.memory.load_task(task.id)
        self.assertNotIn(
            stored.status,
            (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED),
        )
        self.assertEqual(
            digest_before,
            hashlib.sha256((self.root / "notes.txt").read_bytes()).hexdigest(),
        )
        self.assertIs(
            AttemptState.UNKNOWN_OUTCOME,
            app.memory.latest_execution_attempt(task.id).state,
        )

    def test_the_diagnosis_never_advances_or_repairs_anything(self) -> None:
        target = self.seed("notes.txt", "prior\n")
        task = write_task(target, "new\n", "overwrite")
        task.local_write = self.record_for(task, target, "prior\n", "new\n")
        before = target.read_bytes()
        self.connector.diagnose_write(task)
        self.assertEqual(before, target.read_bytes())
        self.assertIs(TaskStatus.PENDING, task.status)


# --------------------------------------------------------------------------
# 9. Verification
# --------------------------------------------------------------------------


class VerifyTests(LocalWriteTestCase):
    def test_a_matching_destination_passes_with_metadata_evidence(self) -> None:
        _, run = self.completed(self.root / "notes.txt", MARKER + "\n")
        verification = run.result.verification
        self.assertEqual("passed", verification.status.value)
        joined = " ".join(verification.evidence)
        self.assertIn("notes.txt", joined)
        self.assertNotIn(MARKER, joined)

    def test_a_post_write_third_party_change_fails_verification(self) -> None:
        task = write_task(self.root / "notes.txt", "authorized\n")
        run = self.request(task)
        original = LocalFileWriteConnector.execute

        def tampering(connector, task_, session):
            result = original(connector, task_, session)
            (self.root / "notes.txt").write_text("tampered\n", encoding="utf-8")
            return result

        with mock.patch.object(LocalFileWriteConnector, "execute", tampering):
            decided = self.approve(task, run)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertIs(ErrorCode.VERIFICATION_FAILED, decided.error.code)

    def test_a_deleted_destination_fails_verification(self) -> None:
        task = write_task(self.root / "notes.txt")
        run = self.request(task)
        original = LocalFileWriteConnector.execute

        def deleting(connector, task_, session):
            result = original(connector, task_, session)
            (self.root / "notes.txt").unlink()
            return result

        with mock.patch.object(LocalFileWriteConnector, "execute", deleting):
            decided = self.approve(task, run)
        self.assertEqual(RunStatus.FAILED, decided.status)

    def test_a_task_with_no_record_fails_verification(self) -> None:
        task = write_task(self.root / "notes.txt")
        verification = self.connector.verify(task, mock.Mock(), SessionState())
        self.assertEqual("failed", verification.status.value)


# --------------------------------------------------------------------------
# 10. Decision / retrieval isolation
# --------------------------------------------------------------------------


class DecisionIsolationTests(LocalWriteTestCase):
    def test_a_completed_write_creates_zero_decisions(self) -> None:
        task = write_task(self.root / "notes.txt")
        run = self.request(task)
        decided = self.approve(task, run)
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        self.assertEqual([], self.memory.list_decisions(run.session.id))

    def test_a_refused_write_creates_zero_decisions(self) -> None:
        self.seed("notes.txt", "already here\n")
        task = write_task(self.root / "notes.txt", "x\n", "create")
        run = self.core.run(task)
        if run.status is RunStatus.AWAITING_APPROVAL:
            run = self.approve(task, run)
        self.assertEqual([], self.memory.list_decisions(run.session.id))

    def test_a_rejected_write_creates_zero_decisions(self) -> None:
        task = write_task(self.root / "notes.txt")
        run = self.request(task)
        grant = PermissionGrant.for_request(run.permission_request, approved=False)
        self.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual([], self.memory.list_decisions(run.session.id))

    def test_a_later_task_inherits_nothing_from_the_write(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        self.approve(task, run)
        later = write_task(self.root / "second.txt")
        second = self.core.run(later, session_id=run.session.id)
        stored = self.memory.load_task(later.id)
        self.assertEqual((), stored.retrieved_context.items)
        self.assertEqual(0, stored.retrieved_context.character_count)
        self.assertNotIn(MARKER, json.dumps(stored.retrieved_context.to_dict()))
        self.assertIsNotNone(second)

    def test_a_normal_task_still_writes_its_decision(self) -> None:
        app = create_app(
            settings=Settings(
                memory_database=self.root / "normal.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(DummyConnector(),),
        )
        from tests.helpers import make_task

        run = app.core.run(make_task())
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        self.assertEqual(1, len(app.memory.list_decisions(run.session.id)))


# --------------------------------------------------------------------------
# 11. Retention of the raw intended bytes
# --------------------------------------------------------------------------


class RetentionTests(LocalWriteTestCase):
    """Executable lifetime only, erased logically at terminal state.

    Note what these tests deliberately do NOT assert: that the bytes have
    physically left the database file. ``secure_delete`` is off,
    ``auto_vacuum`` is NONE and no VACUUM runs, so previous bytes may remain
    in freed pages, the WAL or filesystem slack. M5 claims LOGICAL erasure
    from canonical task state and nothing more.
    """

    def persisted(self, task_id: str) -> str:
        return self.memory.load_task(task_id).write_content

    def test_the_bytes_are_retained_while_awaiting_approval(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        self.request(task)
        self.assertEqual(MARKER + "\n", self.persisted(task.id))

    def test_the_bytes_survive_a_reload_and_still_attest_identically(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        reloaded = self.memory.load_task(task.id)
        self.assertEqual(
            self.connector.canonical_outbound(task),
            self.connector.canonical_outbound(reloaded),
        )
        self.assertEqual(
            run.permission_request.fingerprint,
            self.connector.permission_request(reloaded, run.session.id).fingerprint,
        )

    def test_the_bytes_are_retained_for_an_attempt_in_doubt(self) -> None:
        raiser = RaisingWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        app = create_app(
            settings=Settings(
                memory_database=self.root / "doubt.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(raiser,),
        )
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = app.core.run(task)
        grant = PermissionGrant.for_request(run.permission_request)
        app.core.resume(run.session.id, task.id, grant=grant)
        attempt = app.memory.latest_execution_attempt(task.id)
        self.assertIs(AttemptState.UNKNOWN_OUTCOME, attempt.state)
        self.assertEqual(MARKER + "\n", app.memory.load_task(task.id).write_content)

    # -- complete attempt history --------------------------------------------
    #
    # The bytes are kept for exactly one reason: recovery may still need to
    # re-attest them for an attempt nobody concluded. That attempt does not
    # stop existing because a later clean one was written above it, so the
    # erasure decision must read EVERY attempt, not the newest row.
    #
    # Production writes ONE attempt row per task, so a multi-row history is
    # corrupted or migrated state; it is planted directly for exactly that
    # reason - this rule is what the store falls back on once its one-row
    # invariant has already failed. The task is then written terminal through
    # the ordinary guarded upsert, which is the single place erasure happens.

    def plant_history(self, session_id: str, task_id: str, *states) -> None:
        import contextlib
        import sqlite3

        with contextlib.closing(
            sqlite3.connect(self.memory.database_path)
        ) as connection:
            connection.executemany(
                "INSERT INTO execution_attempts (id, session_id, task_id, "
                "connector, fingerprint, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        f"attempt-{task_id}-{index}",
                        session_id,
                        task_id,
                        "local",
                        "f" * 64,
                        state.value,
                        "2026-08-28T00:00:00Z",
                        "2026-08-28T00:00:00Z",
                    )
                    for index, state in enumerate(states)
                ],
            )
            connection.commit()

    def terminal_write_with_history(self, *states) -> str:
        """A task holding bytes, made terminal over exactly this history."""
        from jarvis.contracts import LoopStage, TaskStatus

        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        self.assertEqual(MARKER + "\n", self.persisted(task.id))
        self.plant_history(run.session.id, task.id, *states)
        stored = self.memory.load_task(task.id)
        stored.status = TaskStatus.COMPLETED
        stored.stage = LoopStage.COMPLETED
        self.memory.save_task(run.session.id, stored)
        return task.id

    def test_an_older_in_flight_under_a_newer_finished_keeps_the_bytes(
        self,
    ) -> None:
        task_id = self.terminal_write_with_history(
            AttemptState.IN_FLIGHT, AttemptState.FINISHED
        )
        self.assertEqual(MARKER + "\n", self.persisted(task_id))

    def test_an_older_unknown_under_a_newer_finished_keeps_the_bytes(
        self,
    ) -> None:
        task_id = self.terminal_write_with_history(
            AttemptState.UNKNOWN_OUTCOME, AttemptState.FINISHED
        )
        self.assertEqual(MARKER + "\n", self.persisted(task_id))

    def test_a_wholly_established_history_still_erases_the_bytes(self) -> None:
        # THE CONTROL: nothing unresolved anywhere, so the existing terminal
        # cleanup is unchanged.
        task_id = self.terminal_write_with_history(
            AttemptState.FINISHED, AttemptState.FINISHED
        )
        self.assertEqual("", self.persisted(task_id))

    def test_the_bytes_are_erased_on_completion(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        decided = self.approve(task, run)
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        self.assertEqual("", self.persisted(task.id))
        # The metadata that outlives it is enough to explain what happened.
        self.assertEqual("written", self.memory.load_task(task.id).local_write.status)

    def test_the_bytes_are_erased_on_a_pre_execution_failure(self) -> None:
        task = write_task(self.root / "absent" / "notes.txt", MARKER + "\n")
        run = self.core.run(task)
        if run.status is RunStatus.AWAITING_APPROVAL:
            run = self.approve(task, run)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual("", self.persisted(task.id))

    def test_the_bytes_are_erased_on_a_post_execution_failure(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        original = LocalFileWriteConnector.execute

        def tampering(connector, task_, session):
            result = original(connector, task_, session)
            (self.root / "notes.txt").write_text("tampered\n", encoding="utf-8")
            return result

        with mock.patch.object(LocalFileWriteConnector, "execute", tampering):
            decided = self.approve(task, run)
        self.assertEqual(RunStatus.FAILED, decided.status)
        self.assertEqual("", self.persisted(task.id))

    def test_the_bytes_are_erased_on_rejection(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        grant = PermissionGrant.for_request(run.permission_request, approved=False)
        decided = self.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.REJECTED, decided.status)
        self.assertEqual("", self.persisted(task.id))

    def test_the_bytes_are_erased_on_cancellation(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        self.core.cancel(run.session.id, task.id)
        stored = self.memory.load_task(task.id)
        self.assertIs(TaskStatus.CANCELLED, stored.status)
        self.assertEqual("", stored.write_content)

    def test_a_terminal_reload_resave_reload_stays_empty(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        self.approve(task, run)
        first = self.memory.load_task(task.id)
        again = Task.from_dict(json.loads(json.dumps(first.to_dict())))
        third = Task.from_dict(json.loads(json.dumps(again.to_dict())))
        self.assertEqual("", again.write_content)
        self.assertEqual("", third.write_content)

    def test_a_terminal_resume_never_needs_the_bytes(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        self.approve(task, run)
        again = self.core.resume(run.session.id, task.id)
        self.assertEqual(RunStatus.COMPLETED, again.status)
        self.assertEqual("", self.memory.load_task(task.id).write_content)

    def test_a_lost_guarded_write_does_not_erase(self) -> None:
        """If terminal arbitration rolls back, so does the erasure."""
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        self.request(task)
        stored = self.memory.load_task(task.id)
        self.assertEqual(MARKER + "\n", stored.write_content)
        # A stale writer carrying a terminal object loses to the live row.
        loser = self.memory.load_task(task.id)
        loser.status = TaskStatus.COMPLETED
        loser.write_content = MARKER + "\n"
        session = self.memory.load_session(self.memory.task_session_id(task.id))
        # Make the live row terminal first, so the stale write loses.
        self.core.cancel(session.id, task.id)
        won = self.memory.persist_checkpoint(session, loser, "stale_terminal", {})
        self.assertFalse(won, "the stale writer must not win")
        self.assertIs(TaskStatus.CANCELLED, self.memory.load_task(task.id).status)

    def test_the_raw_row_carries_the_bytes_before_terminal(self) -> None:
        """Expected, and the reason terminal-time erasure exists at all."""
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        self.request(task)
        raw = Path(self.memory.database_path).read_bytes().decode("latin-1")
        self.assertIn(MARKER, raw)


# --------------------------------------------------------------------------
# 12. Raw-byte hygiene on every other surface
# --------------------------------------------------------------------------


class HygieneTests(LocalWriteTestCase):
    def test_the_bytes_reach_no_surface_but_the_task_row(self) -> None:
        task = write_task(self.root / "notes.txt", MARKER + "\n")
        run = self.request(task)
        session_id = run.session.id

        # [1] The approval request the user reads.
        request = run.permission_request
        self.assertNotIn(MARKER, json.dumps(request.to_dict(), default=str))

        # [2] The persisted pending approval.
        pending = self.memory.load_pending_approval(task.id, session_id)
        self.assertNotIn(MARKER, json.dumps(pending.to_dict(), default=str))

        decided = self.approve(task, run)
        self.assertEqual(RunStatus.COMPLETED, decided.status, msg=str(decided.error))
        stored = self.memory.load_task(task.id)

        # [3] Events, checkpoints and decisions.
        self.assertNotIn(
            MARKER, json.dumps(self.memory.list_events(session_id), default=str)
        )
        self.assertNotIn(
            MARKER,
            json.dumps(
                [d.to_dict() for d in self.memory.list_decisions(session_id)],
                default=str,
            ),
        )

        # [4] The result snapshot and the typed record.
        self.assertNotIn(MARKER, json.dumps(stored.result_snapshot, default=str))
        self.assertNotIn(MARKER, json.dumps(stored.local_write.to_dict(), default=str))

        # [5] Usage and billing.
        self.assertNotIn(
            MARKER, json.dumps(decided.result.usage.to_dict(), default=str)
        )

        # [6] The public envelope.
        envelope = build_envelope("run", PermissionMode.SESSION, decided)
        self.assertNotIn(MARKER, json.dumps(envelope, default=str))
        self.assertIsNotNone(envelope["local_write"])

    def test_a_refusal_message_never_quotes_the_content(self) -> None:
        _, run = self.refused(self.root / ".env", MARKER + "\n")
        self.assertNotIn(MARKER, json.dumps(run.error.to_dict(), default=str))

    def test_a_refusal_message_never_shows_an_absolute_path(self) -> None:
        self.seed("notes.txt", "here\n")
        _, run = self.refused(self.root / "notes.txt", "x\n", "create")
        serialized = json.dumps(run.error.to_dict(), default=str)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.root).casefold(), serialized.casefold())

    def test_the_envelope_block_is_metadata_only(self) -> None:
        _, run = self.completed(self.root / "notes.txt", MARKER + "\n")
        block = build_envelope("run", PermissionMode.SESSION, run)["local_write"]
        self.assertEqual(
            {
                "relative_path",
                "mode",
                "pre_present",
                "pre_sha256",
                "pre_byte_size",
                "intended_sha256",
                "intended_byte_size",
                "status",
                "written_at",
            },
            set(block),
        )
        self.assertNotIn(MARKER, json.dumps(block, default=str))

    def test_a_tampered_record_renders_no_envelope_block(self) -> None:
        _, run = self.completed(self.root / "notes.txt")
        stored = self.memory.load_task(run.task.id)
        stored.local_write = replace(stored.local_write, intended_sha256="NOT-A-DIGEST")
        payload = build_envelope(
            "run", PermissionMode.SESSION, replace(run, task=stored)
        )
        self.assertIsNone(payload["local_write"])

    def test_the_envelope_shape_is_fixed_for_a_task_that_wrote_nothing(self) -> None:
        app = create_app(
            settings=Settings(
                memory_database=self.root / "shape.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(DummyConnector(),),
        )
        from tests.helpers import make_task

        run = app.core.run(make_task())
        payload = build_envelope("run", PermissionMode.SESSION, run)
        self.assertIn("local_write", payload)
        self.assertIsNone(payload["local_write"])
        self.assertEqual(OUTPUT_SCHEMA_VERSION, payload["schema_version"])


# --------------------------------------------------------------------------
# 13. Model isolation - the bytes can never egress
# --------------------------------------------------------------------------


class ClaudeIsolationTests(LocalWriteTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )

    def consumer(self) -> Task:
        task = Task(
            objective="analyze the project",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
        )
        task.write_content = MARKER + "\n"
        task.write_mode = "create"
        return task

    def test_the_typed_write_field_cannot_egress_through_the_payload(self) -> None:
        """The reason the bytes are a top-level field and not a context key.

        ``_sanitized_payload`` serializes ``task.context`` verbatim, so a
        ``context["write_content"]`` design would have handed the model up to
        64 KiB of file content. A typed field is not in the outbound key set
        and cannot be reached at all.
        """
        payload, _ = self.claude._sanitized_payload(self.consumer())
        serialized = json.dumps(payload, default=str)
        self.assertNotIn(MARKER, serialized)
        self.assertNotIn("write_content", serialized)
        self.assertNotIn("write_mode", serialized)

    def test_the_bytes_reach_neither_the_outbound_nor_the_argv(self) -> None:
        task = self.consumer()
        outbound = self.claude.canonical_outbound(task).decode("utf-8")
        self.assertNotIn(MARKER, outbound)
        self.assertNotIn(MARKER, json.dumps(self.claude._command(outbound)))

    def test_a_completed_write_record_does_not_egress_either(self) -> None:
        stored, _ = self.completed(self.root / "notes.txt", MARKER + "\n")
        task = self.consumer()
        task.write_content = ""
        task.local_write = stored.local_write
        payload, _ = self.claude._sanitized_payload(task)
        serialized = json.dumps(payload, default=str)
        self.assertNotIn("local_write", payload)
        self.assertNotIn(MARKER, serialized)

    def test_the_model_is_still_granted_no_tools_at_all(self) -> None:
        self.assertEqual((), claude_code.EGRESS_TOOL_GRANTS)
        self.assertEqual((), self.claude.external_tool_grants(self.consumer()))
        command = self.claude._command("prompt")
        for forbidden in ("Read", "Write", "Edit", "Glob", "Grep"):
            self.assertNotIn(forbidden, command)


# --------------------------------------------------------------------------
# 14. The structural contract for the persisted record
# --------------------------------------------------------------------------


class BlockingWriteConnector(LocalFileWriteConnector):
    """A write that is genuinely IN FLIGHT while the test acts on it.

    Not a simulation of the race: the attempt really is IN_FLIGHT and the
    reservation really is committed for the whole time ``execute`` is parked
    here, because the core has already crossed both before calling it. The
    test cancels from another thread in exactly that window, then releases
    the write, which then really lands.

    ``cancel`` is deliberately NOT overridden. The base connector returns
    ``False`` - a local write has no stop, and that is the whole premise of
    this case: the operator asks, nothing can honour it, and the bytes reach
    the disk anyway.
    """

    manifest = replace(LocalFileWriteConnector.manifest, name="blocking-writer")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.executions = 0

    def execute(self, task: Task, session: SessionState) -> ToolResult:  # type: ignore[override]
        self.executions += 1
        self.entered.set()
        # Parked with the claim held. Everything the operator does now
        # happens against a durably IN_FLIGHT attempt.
        assert self.release.wait(timeout=30), "the test never released the write"
        return super().execute(task, session)


class CancelledAfterWriteTests(LocalWriteTestCase):
    """P2. A cancellation that arrives too late to stop an approved write.

    The lifecycle answer is unchanged and is not on trial here: the operator
    asked before the result came back, so the task ends CANCELLED and H2 is
    right to keep it there. What is on trial is whether the surface an
    operator reads AFTERWARDS still admits that the write landed. It cannot
    be undone - M5 keeps no backup - so a view that says only "cancelled" is
    describing the workflow while implying something false about the disk.
    """

    def setUp(self) -> None:
        super().setUp()
        self.connector = BlockingWriteConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.settings = replace(
            self.settings, memory_database=self.root / "cancelled.db"
        )
        self.app = create_app(settings=self.settings, connectors=(self.connector,))
        self.core = self.app.core
        self.memory = self.app.memory

    # -- the coordinated real flow --------------------------------------

    def cancel_after_write_starts(self, content: str = "durable bytes\n"):
        """Approve, block in the write, cancel, release. Returns the run."""
        target = self.root / "landed.txt"
        task = write_task(target, content)
        run = self.core.run(task)
        self.assertIs(run.status, RunStatus.AWAITING_APPROVAL, msg=str(run.error))
        grant = PermissionGrant.for_request(run.permission_request)
        session_id = run.session.id
        finished: list[Any] = []

        def drive() -> None:
            finished.append(self.core.resume(session_id, task.id, grant=grant))

        worker = threading.Thread(target=drive, name="write-owner")
        worker.start()
        self.addCleanup(self.connector.release.set)
        try:
            self.assertTrue(
                self.connector.entered.wait(timeout=30), "the write never started"
            )
            # THE window: the attempt is durably IN_FLIGHT right now.
            attempt = self.memory.latest_execution_attempt(task.id)
            assert attempt is not None
            self.assertIs(attempt.state, AttemptState.IN_FLIGHT)
            outcome = self.memory.cancel_task(session_id, task.id)
            # Storage refuses to terminalize a claimed attempt: the intent is
            # durable, the verdict is not final, and the owner concludes it.
            self.assertEqual(outcome.verdict, "cancelling")
        finally:
            self.connector.release.set()
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the owner never finished")
        self.assertEqual(len(finished), 1)
        return task, session_id, target, finished[0]

    def events(self, task_id: str) -> list[str]:
        with contextlib.closing(sqlite3.connect(self.settings.memory_database)) as db:
            return [
                str(row[0])
                for row in db.execute(
                    "SELECT event_type FROM event_log WHERE task_id = ? ORDER BY id",
                    (task_id,),
                )
            ]

    def shown(self, session_id: str, task_id: str) -> dict:
        return show_result(self.memory, self.settings, session_id, task_id).payload

    # -- what actually happened -----------------------------------------

    def test_the_approved_bytes_really_reached_the_disk(self) -> None:
        content = "durable bytes\n"
        _, _, target, _ = self.cancel_after_write_starts(content)
        self.assertTrue(target.exists(), "the write did not land")
        # Bytes, not decoded text: the exact approved content, unaltered.
        self.assertEqual(target.read_bytes(), content.encode("utf-8"))
        self.assertEqual(self.connector.executions, 1, "the write ran more than once")

    def test_the_lifecycle_answer_is_still_cancelled(self) -> None:
        """Unchanged, and deliberately so: the operator asked in time."""
        task, _, _, run = self.cancel_after_write_starts()
        self.assertIs(run.status, RunStatus.CANCELLED)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIs(stored.status, TaskStatus.CANCELLED)
        self.assertIs(stored.stage, LoopStage.CANCELLED)

    def test_the_record_and_the_journal_agree_that_it_landed(self) -> None:
        task, _, _, _ = self.cancel_after_write_starts()
        stored = self.memory.load_task(task.id)
        assert stored is not None
        self.assertIsNone(local_write_record_refusal(stored.local_write))
        self.assertEqual(stored.local_write.status, "written")
        attempt = self.memory.latest_execution_attempt(task.id)
        assert attempt is not None
        self.assertIs(attempt.state, AttemptState.FINISHED)
        trail = self.events(task.id)
        self.assertIn("cancellation_requested", trail)
        self.assertIn("execution_cancelled", trail)

    def test_the_money_was_reconciled_and_nothing_was_refunded(self) -> None:
        task, _, _, _ = self.cancel_after_write_starts()
        attempt = self.memory.latest_execution_attempt(task.id)
        assert attempt is not None
        billing = self.memory.attempt_billing_record(attempt.id)
        assert billing is not None
        self.assertEqual(billing["state"], ReservationState.RECONCILED.value)
        # A local write is free, so the honest amount is zero - not a refund
        # of something that was never charged.
        self.assertEqual(int(billing["reserved_nano_usd"]), 0)
        self.assertEqual(int(billing["actual_nano_usd"]), 0)

    # -- and what the operator is told afterwards ------------------------

    def test_show_states_the_applied_effect(self) -> None:
        """THE regression. Cancelled, and it happened anyway."""
        task, session_id, _, _ = self.cancel_after_write_starts()
        payload = self.shown(session_id, task.id)
        self.assertEqual(payload["task"]["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(
            payload["effect_applied"],
            {"kind": APPLIED_EFFECT_LOCAL_WRITE, "status": "written"},
        )

    def test_show_states_it_without_the_content_or_the_path(self) -> None:
        content = "SECRET-WRITE-CONTENT-CANARY\n"
        task, session_id, target, _ = self.cancel_after_write_starts(content)
        rendered = show_result(
            self.memory, self.settings, session_id, task.id
        ).render()
        self.assertNotIn("SECRET-WRITE-CONTENT-CANARY", rendered)
        self.assertNotIn(str(target), rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn(hashlib.sha256(content.encode()).hexdigest(), rendered)
        # Two constant-vocabulary values, and nothing else at all.
        self.assertEqual(
            set(self.shown(session_id, task.id)["effect_applied"]), {"kind", "status"}
        )

    def test_reading_it_repeatedly_writes_nothing(self) -> None:
        task, session_id, _, _ = self.cancel_after_write_starts()
        before = self.database_content()
        for _ in range(3):
            self.assertIsNotNone(self.shown(session_id, task.id)["effect_applied"])
        self.assertEqual(before, self.database_content(), "show wrote a row")

    def test_resume_after_the_fact_never_writes_again(self) -> None:
        task, session_id, target, _ = self.cancel_after_write_starts()
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        before = self.database_content()
        run = self.core.resume(session_id, task.id)
        self.assertIs(run.status, RunStatus.CANCELLED)
        self.assertEqual(self.connector.executions, 1, "the write ran again")
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), digest)
        after = self.database_content()
        for table in ("tasks", "execution_attempts", "attempt_billing", "usage_ledger"):
            with self.subTest(table=table):
                self.assertEqual(before[table], after[table])

    def database_content(self) -> dict:
        with contextlib.closing(sqlite3.connect(self.settings.memory_database)) as db:
            tables = sorted(
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            )
            return {table: list(db.execute(f"SELECT * FROM {table}")) for table in tables}

    # -- negative controls, on this same real flow -----------------------

    def test_a_cancellation_before_any_write_shows_no_effect(self) -> None:
        """THE control M2 has to fail: CANCELLED alone proves nothing."""
        task = write_task(self.root / "never.txt", "unwritten\n")
        run = self.core.run(task)
        self.assertIs(run.status, RunStatus.AWAITING_APPROVAL)
        session_id = run.session.id
        cancelled = self.core.cancel(session_id, task.id)
        self.assertIs(cancelled.status, RunStatus.CANCELLED)
        self.assertFalse((self.root / "never.txt").exists())
        self.assertEqual(self.connector.executions, 0)
        payload = self.shown(session_id, task.id)
        self.assertEqual(payload["task"]["status"], TaskStatus.CANCELLED.value)
        self.assertIsNone(payload["effect_applied"], "an effect was invented")

    def test_a_prepared_but_unexecuted_write_shows_no_effect(self) -> None:
        """The authority exists and the bytes do not. Nothing was applied."""
        task = write_task(self.root / "pending.txt", "pending\n")
        run = self.core.run(task)
        self.assertIs(run.status, RunStatus.AWAITING_APPROVAL)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        record = self.connector.pre_execution_record(stored)
        assert record is not None
        self.assertEqual(record.status, "prepared")
        stored.local_write = record
        self.memory.save_task(run.session.id, stored)
        self.assertIsNone(
            self.shown(run.session.id, task.id)["effect_applied"],
            "a prepared intent was reported as an applied effect",
        )


class RecordContractTests(unittest.TestCase):
    def valid(self, **overrides: Any) -> LocalWriteRecord:
        base = {
            "relative_path": "docs/notes.txt",
            "mode": "overwrite",
            "pre_present": True,
            "pre_sha256": "a" * 64,
            "pre_byte_size": 12,
            "intended_sha256": "b" * 64,
            "intended_byte_size": 34,
            "status": "written",
            "written_at": utc_now(),
        }
        base.update(overrides)
        return LocalWriteRecord(**base)

    def test_a_valid_record_is_accepted(self) -> None:
        self.assertIsNone(local_write_record_refusal(self.valid()))

    def test_a_foreign_object_is_refused(self) -> None:
        for value in (None, {}, "record", 1, object()):
            with self.subTest(value=value):
                self.assertIsNotNone(local_write_record_refusal(value))

    def test_an_empty_record_is_refused(self) -> None:
        self.assertIsNotNone(local_write_record_refusal(LocalWriteRecord()))
        self.assertFalse(LocalWriteRecord().present)

    def test_absolute_and_traversal_paths_are_refused(self) -> None:
        for path in ("/etc/passwd", "c:/x.txt", "../x.txt", "a/../b", "a\\b", "a:b"):
            with self.subTest(path=path):
                self.assertIsNotNone(
                    local_write_record_refusal(self.valid(relative_path=path))
                )

    def test_a_credential_bearing_path_is_refused(self) -> None:
        self.assertIsNotNone(
            local_write_record_refusal(
                self.valid(relative_path="api_key=HUNTER2.txt")
            )
        )

    def test_an_unknown_mode_is_refused(self) -> None:
        for mode in ("append", "CREATE", "", None, 1):
            with self.subTest(mode=mode):
                self.assertIsNotNone(
                    local_write_record_refusal(self.valid(mode=mode))
                )

    def test_an_unknown_status_is_refused(self) -> None:
        for status in ("done", "", None, True):
            with self.subTest(status=status):
                self.assertIsNotNone(
                    local_write_record_refusal(self.valid(status=status))
                )

    def test_every_declared_status_is_accepted(self) -> None:
        for status in LOCAL_WRITE_STATUSES:
            with self.subTest(status=status):
                self.assertIsNone(local_write_record_refusal(self.valid(status=status)))

    def test_boolean_numeric_fields_are_refused(self) -> None:
        # ``True`` is an ``int``; an exact type check is what catches it.
        self.assertIsNotNone(
            local_write_record_refusal(self.valid(intended_byte_size=True))
        )
        self.assertIsNotNone(
            local_write_record_refusal(self.valid(pre_byte_size=True))
        )

    def test_a_non_boolean_presence_flag_is_refused(self) -> None:
        for flag in (1, "true", None):
            with self.subTest(flag=flag):
                self.assertIsNotNone(
                    local_write_record_refusal(self.valid(pre_present=flag))
                )

    def test_the_byte_size_boundaries_are_exact(self) -> None:
        self.assertIsNone(
            local_write_record_refusal(
                self.valid(intended_byte_size=LOCAL_WRITE_MAX_BYTES)
            )
        )
        for size in (-1, LOCAL_WRITE_MAX_BYTES + 1):
            with self.subTest(size=size):
                self.assertIsNotNone(
                    local_write_record_refusal(self.valid(intended_byte_size=size))
                )

    def test_uppercase_and_malformed_digests_are_refused(self) -> None:
        for digest in ("A" * 64, "a" * 63, "a" * 65, "z" * 64, "", None):
            with self.subTest(digest=digest):
                self.assertIsNotNone(
                    local_write_record_refusal(self.valid(intended_sha256=digest))
                )

    def test_an_absent_pre_state_may_not_carry_a_digest_or_size(self) -> None:
        for overrides in (
            {"pre_sha256": "a" * 64, "pre_byte_size": 0},
            {"pre_sha256": "", "pre_byte_size": 5},
        ):
            with self.subTest(**overrides):
                self.assertIsNotNone(
                    local_write_record_refusal(
                        self.valid(mode="create", pre_present=False, **overrides)
                    )
                )

    def test_the_mode_must_agree_with_the_pre_state(self) -> None:
        self.assertIsNotNone(
            local_write_record_refusal(
                self.valid(mode="create", pre_present=True)
            )
        )
        self.assertIsNotNone(
            local_write_record_refusal(
                self.valid(
                    mode="overwrite", pre_present=False, pre_sha256="", pre_byte_size=0
                )
            )
        )

    def test_a_malformed_timestamp_is_refused(self) -> None:
        for stamp in ("", "not-a-time", "2026-01-01", "2026-01-01T00:00:00", None):
            with self.subTest(stamp=stamp):
                self.assertIsNotNone(
                    local_write_record_refusal(self.valid(written_at=stamp))
                )

    def test_a_valid_record_round_trips_byte_identically(self) -> None:
        record = self.valid()
        self.assertEqual(record, LocalWriteRecord.from_dict(record.to_dict()))

    def test_every_malformed_row_loads_as_the_empty_record(self) -> None:
        for broken in (
            None,
            {},
            "row",
            {"relative_path": "/abs.txt"},
            {**self.valid().to_dict(), "mode": "append"},
            {**self.valid().to_dict(), "intended_sha256": "short"},
            {**self.valid().to_dict(), "pre_present": 1},
            {**self.valid().to_dict(), "written_at": "nope"},
        ):
            with self.subTest(broken=broken):
                self.assertEqual(LocalWriteRecord(), LocalWriteRecord.from_dict(broken))

    def test_a_task_row_with_a_broken_record_loads_closed(self) -> None:
        task = Task(objective="o", capability=LOCAL_WRITE_CAPABILITY, scope=ActionScope(action="write"))
        document = task.to_dict()
        document["local_write"] = {"relative_path": "../escape.txt"}
        self.assertFalse(Task.from_dict(document).local_write.present)

    def test_a_task_row_with_oversized_content_loads_closed(self) -> None:
        task = Task(objective="o", capability=LOCAL_WRITE_CAPABILITY, scope=ActionScope(action="write"))
        document = task.to_dict()
        document["write_content"] = "a" * (LOCAL_WRITE_MAX_BYTES + 1)
        self.assertEqual("", Task.from_dict(document).write_content)

    def test_a_task_row_with_a_forged_mode_loads_closed(self) -> None:
        task = Task(objective="o", capability=LOCAL_WRITE_CAPABILITY, scope=ActionScope(action="write"))
        document = task.to_dict()
        document["write_mode"] = "append"
        self.assertEqual("", Task.from_dict(document).write_mode)

    def test_the_content_grammar_matches_the_input_boundary(self) -> None:
        self.assertIsNone(local_write_content_refusal(""))
        self.assertIsNone(local_write_content_refusal("a" * LOCAL_WRITE_MAX_BYTES))
        self.assertIsNotNone(local_write_content_refusal("a\u0000b"))
        self.assertIsNotNone(local_write_content_refusal(b"bytes"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
