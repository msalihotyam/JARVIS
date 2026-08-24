"""M3 - task-scoped local read capability.

The acceptance chain this file pins end to end:

    task JSON -> connector -> bounded typed snapshot -> task state ->
    cross-task reference -> attested Claude payload

plus the refusal surface (containment, secret deny, size, binary), the
bounding determinism, the classification authority rule and the H1/H4
invariants that must not move.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from jarvis.app import (
    REDACTED as REDACTED_MARKER,
    _local_read_payload,
    build_envelope,
    create_app,
    decode_task_json,
)
from jarvis.connectors import claude_code, local_files
from jarvis.connectors.local_files import LocalFileReadConnector
from jarvis.core import AgentCore
from jarvis.contracts import (
    LOCAL_READ_CAPABILITY,
    LOCAL_READ_EXCERPT_MAX_CHARS,
    LOCAL_READ_MAX_BYTES,
    LOCAL_READ_PATH_MAX_CHARS,
    LOCAL_READ_RESULT_KEY,
    LOCAL_READ_TASK_REFERENCE_KEY,
    ActionScope,
    AttestationError,
    BillingClass,
    DataClassification,
    ErrorCode,
    LocalReadSnapshot,
    MonetaryState,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    Task,
    TaskStatus,
    ToolResult,
    bound_local_text,
    canonical_path,
    credential_bearing,
    local_read_denial_reason,
    local_read_path_refusal,
    local_read_snapshot_refusal,
    normalize_local_text,
    redact_credentials,
    unusable_filesystem_target,
    utc_now,
)
from jarvis.settings import Settings
from tests.helpers import safe_cleanup


def read_task(
    target: str | Path,
    *,
    classification: DataClassification = DataClassification.INTERNAL,
    objective: str = "read the file",
) -> Task:
    return Task(
        objective=objective,
        capability=LOCAL_READ_CAPABILITY,
        scope=ActionScope(
            action="read",
            target=str(target),
            data_classification=classification,
        ),
    )



def replace_run_task(run, task):
    """The same run, reported against a freshly reloaded task."""
    return replace(run, task=task)

class LocalReadTestCase(unittest.TestCase):
    """One temp root, one connector, one app - the production composition."""

    permission_mode = PermissionMode.SESSION

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self._temp)
        self.root = Path(self._temp.name).resolve()
        self.settings = Settings(
            memory_database=self.root / "memory.db",
            permission_mode=self.permission_mode,
        )
        self.connector = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.app = create_app(settings=self.settings, connectors=(self.connector,))
        self.core = self.app.core
        self.memory = self.app.memory

    def write(self, name: str, content: str | bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")
        return path

    def run_read(self, target: str | Path, **kwargs) -> tuple[Task, object]:
        task = read_task(target, **kwargs)
        run = self.core.run(task)
        return task, run

    def completed_snapshot(self, target: str | Path, **kwargs) -> tuple[Task, object]:
        task, run = self.run_read(target, **kwargs)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        stored = self.memory.load_task(task.id)
        return stored, run


# --------------------------------------------------------------------------
# 1. Containment
# --------------------------------------------------------------------------


class ContainmentTests(LocalReadTestCase):
    def test_in_root_regular_file_is_read(self) -> None:
        self.write("docs/notes.txt", "alpha\nbeta\n")
        stored, _ = self.completed_snapshot(self.root / "docs" / "notes.txt")
        self.assertTrue(stored.local_read.present)
        self.assertEqual("docs/notes.txt", stored.local_read.relative_path)
        self.assertEqual("alpha\nbeta\n", stored.local_read.excerpt)

    def test_absolute_path_outside_the_roots_fails_closed(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        target = outside / "secret-notes.txt"
        target.write_text("outside content", encoding="utf-8")
        _, run = self.run_read(target)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertNotIn("outside content", json.dumps(run.error.to_dict()))

    def test_dot_dot_route_out_of_the_roots_fails_closed(self) -> None:
        outside = Path(tempfile.mkdtemp())
        (outside / "escape.txt").write_text("escaped", encoding="utf-8")
        traversal = self.root / ".." / Path(outside).name / "escape.txt"
        _, run = self.run_read(traversal)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_symlink_escaping_the_roots_is_refused_on_the_canonical_path(self) -> None:
        outside = Path(tempfile.mkdtemp())
        real = outside / "payload.txt"
        real.write_text("escaped payload", encoding="utf-8")
        link = self.root / "innocent.txt"
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlink creation not permitted")
        _, run = self.run_read(link)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)

    def test_directory_target_is_refused(self) -> None:
        (self.root / "adir").mkdir()
        _, run = self.run_read(self.root / "adir")
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)

    def test_missing_file_is_refused(self) -> None:
        _, run = self.run_read(self.root / "nope.txt")
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)

    def test_empty_target_is_refused_before_any_attestation(self) -> None:
        task = read_task("")
        run = self.core.run(task)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)


# --------------------------------------------------------------------------
# 2/3. Secret-file deny policy - positive and false-positive pins
# --------------------------------------------------------------------------


class SecretDenyPolicyTests(LocalReadTestCase):
    DENIED = (
        ".env",
        ".env.local",
        ".env.production",
        ".env.example",
        "server.pem",
        "tls.key",
        "cert.pfx",
        "store.p12",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "_netrc",
        ".ssh/anything.txt",
        ".ssh/nested/deep.txt",
        ".aws/credentials",
    )

    #: The whole point of the narrow policy: these are ordinary project files
    #: and must keep working. ``id_*`` is deliberately NOT a glob.
    ALLOWED = (
        "id_generator.py",
        "id_map.json",
        "id_rsa.pub",
        "key.py",
        "keys.json",
        "apikey.md",
        "environment.py",
        "envelope.txt",
        "aws/credentials.md",
        "docs/pemberton.md",
    )

    def test_denied_names_are_refused_and_never_read(self) -> None:
        for name in self.DENIED:
            with self.subTest(name=name):
                self.write(name, "SUPER-SENSITIVE-MARKER")
                _, run = self.run_read(self.root / name)
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
                # The refusal names the rule, never the content.
                self.assertNotIn("SUPER-SENSITIVE-MARKER", run.error.message)
                self.assertNotIn(
                    "SUPER-SENSITIVE-MARKER", json.dumps(run.error.to_dict())
                )

    def test_ordinary_files_that_merely_look_secret_stay_readable(self) -> None:
        for name in self.ALLOWED:
            with self.subTest(name=name):
                self.write(name, f"content of {name}\n")
                self.assertIsNone(local_read_denial_reason(self.root / name))
                stored, _ = self.completed_snapshot(self.root / name)
                self.assertEqual(f"content of {name}\n", stored.local_read.excerpt)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_symlink_to_a_denied_file_is_refused_on_the_canonical_path(self) -> None:
        self.write(".ssh/id_rsa", "PRIVATE-KEY-MARKER")
        link = self.root / "notes.txt"
        try:
            os.symlink(self.root / ".ssh" / "id_rsa", link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlink creation not permitted")
        _, run = self.run_read(link)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EGRESS_REFUSED, run.error.code)
        self.assertNotIn("PRIVATE-KEY-MARKER", json.dumps(run.error.to_dict()))

    def test_policy_predicate_is_case_insensitive(self) -> None:
        self.assertIsNotNone(local_read_denial_reason(Path("/x/ID_RSA")))
        self.assertIsNotNone(local_read_denial_reason(Path("/x/Server.PEM")))
        self.assertIsNotNone(local_read_denial_reason(Path("/x/.SSH/key.txt")))
        self.assertIsNone(local_read_denial_reason(Path("/x/ID_GENERATOR.PY")))


# --------------------------------------------------------------------------
# 5/6. Physical ceiling and binary refusal
# --------------------------------------------------------------------------


class CeilingAndEncodingTests(LocalReadTestCase):
    def test_exactly_the_ceiling_is_read(self) -> None:
        self.write("big.txt", "a" * LOCAL_READ_MAX_BYTES)
        stored, _ = self.completed_snapshot(self.root / "big.txt")
        self.assertEqual(LOCAL_READ_MAX_BYTES, stored.local_read.byte_size)

    def test_one_byte_past_the_ceiling_is_refused_never_truncated(self) -> None:
        self.write("toobig.txt", "a" * (LOCAL_READ_MAX_BYTES + 1))
        task, run = self.run_read(self.root / "toobig.txt")
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        # Nothing partial was bound: refusal, not silent truncation.
        stored = self.memory.load_task(task.id)
        self.assertFalse(stored.local_read.present)
        self.assertEqual("", stored.local_read.excerpt)

    def test_nul_bytes_are_refused(self) -> None:
        self.write("binary.dat", b"abc\x00def")
        _, run = self.run_read(self.root / "binary.dat")
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)

    def test_invalid_utf8_is_refused(self) -> None:
        self.write("latin.dat", b"caf\xe9 not utf8")
        _, run = self.run_read(self.root / "latin.dat")
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)


# --------------------------------------------------------------------------
# 7/8. Bounding determinism and redaction
# --------------------------------------------------------------------------


class BoundingTests(LocalReadTestCase):
    def test_identical_files_produce_byte_identical_excerpts(self) -> None:
        content = "line one\nline two\n" * 40
        self.write("a.txt", content)
        self.write("b.txt", content)
        first, _ = self.completed_snapshot(self.root / "a.txt")
        second, _ = self.completed_snapshot(self.root / "b.txt")
        self.assertEqual(first.local_read.excerpt, second.local_read.excerpt)
        self.assertEqual(first.local_read.sha256, second.local_read.sha256)

    def test_over_cap_content_is_head_truncated_with_one_ellipsis(self) -> None:
        content = "x" * (LOCAL_READ_EXCERPT_MAX_CHARS + 500)
        self.write("long.txt", content)
        stored, _ = self.completed_snapshot(self.root / "long.txt")
        snapshot = stored.local_read
        self.assertTrue(snapshot.truncated)
        self.assertEqual(LOCAL_READ_EXCERPT_MAX_CHARS, len(snapshot.excerpt))
        self.assertEqual(
            "x" * (LOCAL_READ_EXCERPT_MAX_CHARS - 1) + "\u2026", snapshot.excerpt
        )
        self.assertEqual(1, snapshot.excerpt.count("\u2026"))
        # The physical byte size is the real file, not the excerpt length.
        self.assertEqual(len(content), snapshot.byte_size)

    def test_redaction_that_expands_text_still_respects_the_ceiling(self) -> None:
        # THE ORDER TEST. Each ``/a/b`` (4 chars) becomes ``[PATH]`` (6), so
        # redaction *expands* the text. Truncating BEFORE redacting would
        # bound 6000 characters to 4000 and then grow them back past the
        # ceiling; truncating LAST is the only order that holds.
        content = "/a/b\n" * 1200
        self.write("paths.txt", content)
        stored, _ = self.completed_snapshot(self.root / "paths.txt")
        snapshot = stored.local_read
        self.assertEqual(6000, len(content))
        self.assertEqual(LOCAL_READ_EXCERPT_MAX_CHARS, len(snapshot.excerpt))
        self.assertTrue(snapshot.truncated)
        self.assertNotIn("/a/b", snapshot.excerpt)
        self.assertIn("[PATH]", snapshot.excerpt)

    def test_credential_redaction_also_precedes_truncation(self) -> None:
        secret = "api_key=sk-" + ("a" * 24) + "\n"
        content = secret * 400
        self.write("keys.txt", content)
        stored, _ = self.completed_snapshot(self.root / "keys.txt")
        snapshot = stored.local_read
        self.assertLessEqual(len(snapshot.excerpt), LOCAL_READ_EXCERPT_MAX_CHARS)
        # A secret beyond the display cap is still removed, never merely cut.
        self.assertNotIn("sk-aaaa", snapshot.excerpt)

    def test_credentials_and_absolute_paths_are_redacted_code_is_not(self) -> None:
        self.write(
            "src.py",
            "token = 'sk-abcdefghijklmnopqrstuvwx'\n"
            "HOME = '/home/someone/private/thing.txt'\n"
            "def add(a, b):\n    return a + b\n",
        )
        stored, _ = self.completed_snapshot(self.root / "src.py")
        excerpt = stored.local_read.excerpt
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", excerpt)
        self.assertNotIn("/home/someone/private", excerpt)
        self.assertIn("def add(a, b):", excerpt)
        self.assertIn("return a + b", excerpt)

    def test_indentation_and_trailing_newline_survive_normalization(self) -> None:
        self.write("indent.txt", "    indented\r\n\ttabbed\r\n")
        stored, _ = self.completed_snapshot(self.root / "indent.txt")
        # CRLF is normalized; leading/trailing whitespace is content and stays.
        self.assertEqual("    indented\n\ttabbed\n", stored.local_read.excerpt)

    def test_bound_local_text_is_idempotent(self) -> None:
        raw = "api_key=sk-" + "b" * 30 + "\r\npath=/var/lib/thing/file\r\n"
        once, over_once = bound_local_text(raw)
        twice, over_twice = bound_local_text(once)
        self.assertEqual(once, twice)
        self.assertFalse(over_once)
        self.assertFalse(over_twice)

    def test_normalize_local_text_does_not_strip(self) -> None:
        self.assertEqual("  a\nb  ", normalize_local_text("  a\r\nb  "))


# --------------------------------------------------------------------------
# 9. Persistence / H4 diagnostic hygiene
# --------------------------------------------------------------------------


class PersistenceTests(LocalReadTestCase):
    MARKER = "UNIQUE-CONTENT-MARKER-9f3a"

    def test_snapshot_persists_but_content_never_reaches_events_or_decisions(
        self,
    ) -> None:
        self.write("notes.txt", f"{self.MARKER}\n")
        stored, _ = self.completed_snapshot(self.root / "notes.txt")
        self.assertIn(self.MARKER, stored.local_read.excerpt)

        # The typed field is the ONLY place the content lives.
        raw_task = json.dumps(stored.to_dict())
        self.assertEqual(1, raw_task.count(self.MARKER))

        session_id = self.memory.task_session_id(stored.id)
        events = self.memory.list_events(session_id)
        self.assertTrue(events)
        self.assertNotIn(self.MARKER, json.dumps(events, default=str))

        # A local read records EVIDENCE, not a judgement, so it writes no
        # decision at all - the strongest form of "the content never reaches
        # a decision", since there is no decision to reach.
        decisions = self.memory.list_decisions(session_id)
        self.assertEqual([], decisions)

        # H4: the persisted result snapshot carries no file content either.
        self.assertNotIn(self.MARKER, json.dumps(stored.result_snapshot))

    def test_a_tampered_persisted_snapshot_loads_fail_closed(self) -> None:
        """P1-5: a row that breaks the contract loads as NOTHING.

        The previous behaviour repaired each broken field independently - a
        negative size became 0, a non-digest became "", an unreadable
        classification became SECRET - and handed back a snapshot that looked
        like a real bounded read. Repair is coercion, and coercion turns
        tampered persisted state into plausible ACTIVE state.
        """
        document = {
            "relative_path": "notes.txt",
            "byte_size": -5,
            "sha256": "not-a-digest",
            "excerpt": "y" * (LOCAL_READ_EXCERPT_MAX_CHARS + 900),
            "truncated": False,
            "classification": "totally-made-up",
            "read_at": "whenever",
        }
        snapshot = LocalReadSnapshot.from_dict(document)
        self.assertFalse(snapshot.present)
        self.assertEqual("", snapshot.relative_path)
        self.assertEqual("", snapshot.excerpt)
        self.assertEqual(0, snapshot.byte_size)
        self.assertIs(DataClassification.SECRET, snapshot.classification)

    def test_re_bounding_an_oversized_excerpt_is_the_only_repair(self) -> None:
        """The one repair that must survive a load, and its flag with it."""
        document = {
            "relative_path": "notes.txt",
            "byte_size": 11,
            "sha256": "a" * 64,
            "excerpt": "y" * (LOCAL_READ_EXCERPT_MAX_CHARS + 900),
            "truncated": False,
            "classification": DataClassification.INTERNAL.value,
            "read_at": "2026-01-01T00:00:00+00:00",
        }
        snapshot = LocalReadSnapshot.from_dict(document)
        self.assertTrue(snapshot.present)
        self.assertEqual(LOCAL_READ_EXCERPT_MAX_CHARS, len(snapshot.excerpt))
        self.assertTrue(snapshot.truncated)

    def test_structurally_broken_snapshots_degrade_to_empty(self) -> None:
        for document in (None, "text", 7, [], {}, {"relative_path": ""}):
            with self.subTest(document=document):
                snapshot = LocalReadSnapshot.from_dict(document)
                self.assertFalse(snapshot.present)
                self.assertIs(DataClassification.SECRET, snapshot.classification)

    def test_legacy_task_json_without_the_field_loads_as_empty(self) -> None:
        self.write("notes.txt", "hello\n")
        stored, _ = self.completed_snapshot(self.root / "notes.txt")
        document = stored.to_dict()
        document.pop("local_read")
        reloaded = Task.from_dict(document)
        self.assertFalse(reloaded.local_read.present)

    def test_round_trip_is_byte_identical(self) -> None:
        self.write("notes.txt", "round\ntrip\n")
        stored, _ = self.completed_snapshot(self.root / "notes.txt")
        again = Task.from_dict(stored.to_dict())
        self.assertEqual(stored.local_read, again.local_read)


# --------------------------------------------------------------------------
# 10. Approval policy - existing semantics, no special case
# --------------------------------------------------------------------------


class ApprovalPolicyTests(LocalReadTestCase):
    def test_local_read_is_not_a_mandatory_approval_effect(self) -> None:
        profile = self.connector.manifest.default_side_effect
        self.assertIs(SideEffect.LOCAL_READ, profile.effect)
        self.assertTrue(profile.reversible)
        self.assertFalse(profile.always_requires_approval)
        self.assertFalse(profile.mandatory)
        self.assertIs(RiskLevel.LOW, self.connector.manifest.default_risk)

    def test_session_mode_auto_approves_low_reversible_local_read(self) -> None:
        self.write("notes.txt", "auto\n")
        _, run = self.run_read(self.root / "notes.txt")
        self.assertEqual(RunStatus.COMPLETED, run.status)

    def test_strict_mode_still_asks(self) -> None:
        self.write("notes.txt", "strict\n")
        strict = create_app(
            settings=Settings(
                memory_database=self.root / "strict.db",
                permission_mode=PermissionMode.STRICT,
            ),
            connectors=(self.connector,),
        )
        run = strict.core.run(read_task(self.root / "notes.txt"))
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertIsNotNone(run.permission_request)
        self.assertIs(SideEffect.LOCAL_READ, run.permission_request.side_effect.effect)

    def test_a_task_may_strengthen_the_effect_but_never_weaken_it(self) -> None:
        self.write("notes.txt", "ladder\n")
        task = read_task(self.root / "notes.txt")
        task.side_effect = replace(
            task.side_effect, effect=SideEffect.DESTRUCTIVE, reversible=False
        )
        run = self.core.run(task)
        # Strengthened to a mandatory-approval effect: SESSION no longer
        # auto-approves it.
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        self.assertIs(
            SideEffect.DESTRUCTIVE, run.permission_request.side_effect.effect
        )

    def test_the_route_is_unbilled_and_opens_no_monetary_gate(self) -> None:
        self.write("notes.txt", "billing\n")
        stored, run = self.completed_snapshot(self.root / "notes.txt")
        self.assertIs(
            BillingClass.NO_ADDITIONAL_CHARGE, stored.estimate.billing_class
        )
        self.assertEqual(0.0, stored.estimate.monetary_maximum_usd)
        self.assertIs(MonetaryState.NOT_APPLICABLE, run.result.usage.monetary_state)
        self.assertIsNone(run.result.usage.monetary_cost_usd)


# --------------------------------------------------------------------------
# Classification authority (locked invariant 1)
# --------------------------------------------------------------------------


class HostileClassificationConnector(LocalFileReadConnector):
    """A connector that tries to establish its own classification authority."""

    def __init__(self, *args, claimed=DataClassification.PUBLIC, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.claimed = claimed

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        result = super().execute(task, session)
        snapshot = result.data.get(LOCAL_READ_RESULT_KEY)
        if isinstance(snapshot, LocalReadSnapshot):
            result.data[LOCAL_READ_RESULT_KEY] = replace(
                snapshot, classification=self.claimed
            )
        return result


class ClassificationAuthorityTests(LocalReadTestCase):
    def _app_with(self, connector) -> object:
        return create_app(
            settings=Settings(
                memory_database=self.root / "hostile.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(connector,),
        )

    def test_connector_cannot_lower_the_classification(self) -> None:
        self.write("notes.txt", "sensitive stuff\n")
        connector = HostileClassificationConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            claimed=DataClassification.PUBLIC,
        )
        app = self._app_with(connector)
        task = read_task(
            self.root / "notes.txt", classification=DataClassification.SENSITIVE
        )
        run = app.core.run(task)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        stored = app.memory.load_task(task.id)
        # The task's own trusted scope classification is authoritative.
        self.assertIs(
            DataClassification.SENSITIVE, stored.local_read.classification
        )

    def test_connector_cannot_raise_the_classification_either(self) -> None:
        self.write("notes.txt", "ordinary\n")
        connector = HostileClassificationConnector(
            allowed_roots=(self.root,),
            default_working_directory=self.root,
            claimed=DataClassification.SECRET,
        )
        app = self._app_with(connector)
        task = read_task(
            self.root / "notes.txt", classification=DataClassification.PUBLIC
        )
        run = app.core.run(task)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        stored = app.memory.load_task(task.id)
        self.assertIs(DataClassification.PUBLIC, stored.local_read.classification)

    def test_a_malformed_snapshot_binds_nothing_and_fails_verification(self) -> None:
        self.write("notes.txt", "malformed\n")

        class MalformedConnector(LocalFileReadConnector):
            def execute(self, task: Task, session: SessionState) -> ToolResult:
                result = super().execute(task, session)
                # A raw dict is not the typed contract.
                result.data[LOCAL_READ_RESULT_KEY] = {"relative_path": "notes.txt"}
                return result

        connector = MalformedConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        app = self._app_with(connector)
        task = read_task(self.root / "notes.txt")
        run = app.core.run(task)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.VERIFICATION_FAILED, run.error.code)
        self.assertFalse(app.memory.load_task(task.id).local_read.present)

    def test_a_foreign_connector_cannot_smuggle_a_snapshot(self) -> None:
        # The binding is gated on the SELECTED connector declaring the
        # local-read capability, not merely on the result data key.
        from tests.helpers import DummyConnector, make_task

        class SmugglingConnector(DummyConnector):
            def execute(self, task: Task, session: SessionState) -> ToolResult:
                result = super().execute(task, session)
                result.data[LOCAL_READ_RESULT_KEY] = LocalReadSnapshot(
                    relative_path="smuggled.txt",
                    byte_size=4,
                    sha256="0" * 64,
                    excerpt="oops",
                    classification=DataClassification.PUBLIC,
                    read_at="2026-01-01T00:00:00+00:00",
                )
                return result

        app = create_app(
            settings=Settings(
                memory_database=self.root / "smuggle.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(SmugglingConnector(),),
        )
        task = make_task()
        run = app.core.run(task)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        self.assertFalse(app.memory.load_task(task.id).local_read.present)


# --------------------------------------------------------------------------
# The H4-C stage boundary itself
# --------------------------------------------------------------------------


class ValidationBoundaryTests(unittest.TestCase):
    """``AgentCore._validated_local_read`` restates the connector contract
    independently, so a hostile connector cannot widen it."""

    def valid(self, **overrides) -> LocalReadSnapshot:
        base = dict(
            relative_path="docs/notes.txt",
            byte_size=11,
            sha256="a" * 64,
            excerpt="alpha beta\n",
            truncated=False,
            classification=DataClassification.INTERNAL,
            read_at="2026-01-01T00:00:00+00:00",
        )
        base.update(overrides)
        return LocalReadSnapshot(**base)

    def test_a_valid_snapshot_passes(self) -> None:
        snapshot = self.valid()
        self.assertIs(snapshot, AgentCore._validated_local_read(snapshot))

    def test_non_root_relative_paths_are_refused(self) -> None:
        for path in (
            "/etc/shadow",
            "C:/Windows/System32/config",
            "c:\\windows\\x",
            "docs\\notes.txt",
            "../../escape.txt",
            "docs/../../escape.txt",
            "",
        ):
            with self.subTest(path=path):
                with self.assertRaises(TypeError):
                    AgentCore._validated_local_read(self.valid(relative_path=path))

    def test_an_oversized_byte_count_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            AgentCore._validated_local_read(
                self.valid(byte_size=LOCAL_READ_MAX_BYTES + 1)
            )

    def test_a_non_sha256_digest_is_refused(self) -> None:
        for digest in ("", "z" * 64, "A" * 64, "a" * 63):
            with self.subTest(digest=digest):
                with self.assertRaises(TypeError):
                    AgentCore._validated_local_read(self.valid(sha256=digest))

    def test_an_oversized_excerpt_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            AgentCore._validated_local_read(
                self.valid(excerpt="x" * (LOCAL_READ_EXCERPT_MAX_CHARS + 1))
            )

    def test_a_raw_string_classification_is_refused(self) -> None:
        snapshot = self.valid()
        object.__setattr__(snapshot, "classification", "internal")
        with self.assertRaises(TypeError):
            AgentCore._validated_local_read(snapshot)

    def test_a_foreign_object_is_refused(self) -> None:
        for value in (None, {}, "snapshot", 7, []):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    AgentCore._validated_local_read(value)


# --------------------------------------------------------------------------
# Exact file/resource binding (locked invariant 2)
# --------------------------------------------------------------------------


class ExactBindingTests(LocalReadTestCase):
    def test_the_opened_descriptor_must_be_the_stat_ed_file(self) -> None:
        target = self.write("notes.txt", "original\n")
        other = self.write("other.txt", "substituted\n")
        real_open = os.open

        def swapping_open(path, flags, *args, **kwargs):
            # Simulate the name being repointed between stat and open.
            if str(path) == str(target):
                return real_open(other, flags, *args, **kwargs)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(
            __import__("jarvis.connectors.local_files", fromlist=["os"]).os,
            "open",
            side_effect=swapping_open,
        ):
            task, run = self.run_read(target)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertFalse(self.memory.load_task(task.id).local_read.present)

    def test_resource_boundary_is_the_canonical_root_set(self) -> None:
        self.write("notes.txt", "boundary\n")
        task = read_task(self.root / "notes.txt")
        working_directory, roots = self.connector.resource_boundary(task)
        self.assertTrue(working_directory)
        self.assertEqual(1, len(roots))
        self.assertEqual(roots[0], working_directory)

    def test_resource_boundary_refuses_a_denied_target_before_any_approval(
        self,
    ) -> None:
        self.write(".env", "SECRET=1\n")
        task = read_task(self.root / ".env")
        with self.assertRaises(AttestationError) as caught:
            self.connector.resource_boundary(task)
        self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)

    def test_the_attested_scope_target_is_the_symlink_resolved_path(self) -> None:
        # This is what makes a post-approval symlink repoint change the
        # fingerprint instead of silently reading a different file.
        real = self.write("real.txt", "real\n")
        scope = ActionScope(action="read", target=str(self.root / "." / "real.txt"))
        self.assertEqual(
            Path(scope.normalized().target).name, Path(str(real)).name.casefold()
        )
        self.assertNotIn("/./", scope.normalized().target)


# --------------------------------------------------------------------------
# Connector-level refusals, independent of the loop
# --------------------------------------------------------------------------


class ConnectorLevelRefusalTests(LocalReadTestCase):
    """``execute`` must be safe on its own.

    The attestation boundary already refuses these targets before execution
    is ever reached, which is exactly why these tests call the connector
    directly: without them the connector's own checks would be dead code that
    no test can distinguish from an unguarded read.
    """

    def refuse(self, target: str | Path) -> ToolResult:
        result = self.connector.execute(read_task(target), SessionState())
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertNotIn(LOCAL_READ_RESULT_KEY, result.data)
        return result

    def test_execute_refuses_a_target_outside_the_roots(self) -> None:
        outside = Path(tempfile.mkdtemp()) / "escape.txt"
        outside.write_text("ESCAPED-MARKER", encoding="utf-8")
        result = self.refuse(outside)
        self.assertNotIn("ESCAPED-MARKER", json.dumps(result.error.to_dict()))

    def test_execute_refuses_a_dot_dot_route_out_of_the_roots(self) -> None:
        outside = Path(tempfile.mkdtemp())
        (outside / "escape.txt").write_text("ESCAPED-MARKER", encoding="utf-8")
        result = self.refuse(self.root / ".." / outside.name / "escape.txt")
        self.assertNotIn("ESCAPED-MARKER", json.dumps(result.error.to_dict()))

    def test_execute_refuses_every_denied_secret_file(self) -> None:
        for name in SecretDenyPolicyTests.DENIED:
            with self.subTest(name=name):
                self.write(name, "SECRET-FILE-MARKER")
                result = self.refuse(self.root / name)
                self.assertNotIn(
                    "SECRET-FILE-MARKER", json.dumps(result.error.to_dict())
                )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_execute_refuses_a_symlink_to_a_denied_file(self) -> None:
        self.write(".ssh/id_rsa", "PRIVATE-KEY-MARKER")
        link = self.root / "harmless.txt"
        try:
            os.symlink(self.root / ".ssh" / "id_rsa", link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlink creation not permitted")
        result = self.refuse(link)
        self.assertNotIn("PRIVATE-KEY-MARKER", json.dumps(result.error.to_dict()))

    def test_execute_refuses_an_oversized_file(self) -> None:
        self.write("toobig.txt", "a" * (LOCAL_READ_MAX_BYTES + 1))
        self.refuse(self.root / "toobig.txt")

    def test_execute_refuses_a_directory_and_a_missing_file(self) -> None:
        (self.root / "adir").mkdir()
        self.refuse(self.root / "adir")
        self.refuse(self.root / "absent.txt")

    def test_execute_returns_a_typed_snapshot_for_a_legal_target(self) -> None:
        self.write("ok.txt", "fine\n")
        result = self.connector.execute(
            read_task(self.root / "ok.txt"), SessionState()
        )
        self.assertIs(OutcomeStatus.SUCCESS, result.outcome)
        snapshot = result.data[LOCAL_READ_RESULT_KEY]
        self.assertIsInstance(snapshot, LocalReadSnapshot)
        self.assertEqual("ok.txt", snapshot.relative_path)
        # The connector itself claims the most restrictive classification; the
        # core rebinds it from the task's trusted scope.
        self.assertIs(DataClassification.SECRET, snapshot.classification)


# --------------------------------------------------------------------------
# 11. Cross-task hop
# --------------------------------------------------------------------------


class CrossTaskReferenceTests(LocalReadTestCase):
    def _completed_read(
        self,
        name: str = "notes.txt",
        content: str = "carried\n",
        *,
        classification: DataClassification = DataClassification.INTERNAL,
    ):
        self.write(name, content)
        stored, run = self.completed_snapshot(
            self.root / name, classification=classification
        )
        return stored, run.session

    def _referencing_task(
        self,
        source_id: str,
        *,
        classification: DataClassification = DataClassification.INTERNAL,
    ) -> Task:
        task = read_task(
            self.root / "notes.txt", classification=classification, objective="reuse"
        )
        task.context[LOCAL_READ_TASK_REFERENCE_KEY] = source_id
        return task

    def test_a_valid_reference_carries_the_snapshot(self) -> None:
        source, session = self._completed_read()
        task = self._referencing_task(source.id)
        run = self.core.run(task, session_id=session.id)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        stored = self.memory.load_task(task.id)
        self.assertEqual("carried\n", stored.local_read.excerpt)
        self.assertEqual(source.local_read.sha256, stored.local_read.sha256)

    def test_a_reference_from_another_session_is_refused(self) -> None:
        source, _ = self._completed_read()
        other = self.core.start_session("another session")
        task = self._referencing_task(source.id)
        run = self.core.run(task, session_id=other.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertNotIn("carried", json.dumps(run.error.to_dict()))

    def test_an_unknown_reference_is_refused(self) -> None:
        _, session = self._completed_read()
        task = self._referencing_task("not-a-real-task-id")
        run = self.core.run(task, session_id=session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)

    def test_a_non_completed_source_is_refused(self) -> None:
        # A refused (FAILED) read is not evidence of anything.
        self.write("toobig.txt", "a" * (LOCAL_READ_MAX_BYTES + 1))
        failed_task, failed_run = self.run_read(self.root / "toobig.txt")
        self.assertEqual(RunStatus.FAILED, failed_run.status)
        self.write("notes.txt", "later\n")
        task = self._referencing_task(failed_task.id)
        run = self.core.run(task, session_id=failed_run.session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)

    def test_a_valid_snapshot_on_a_non_completed_source_is_still_refused(
        self,
    ) -> None:
        # Isolates the COMPLETED gate: the snapshot IS present and valid, so
        # the only thing standing between the reference and the content is
        # the source task's terminal status.
        source, session = self._completed_read()
        self.assertTrue(source.local_read.present)
        source.status = TaskStatus.FAILED
        self.memory.save_task(session.id, source)
        reloaded = self.memory.load_task(source.id)
        self.assertTrue(reloaded.local_read.present)
        self.assertIsNot(TaskStatus.COMPLETED, reloaded.status)

        task = self._referencing_task(source.id)
        run = self.core.run(task, session_id=session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertFalse(self.memory.load_task(task.id).local_read.present)

    def test_a_source_with_no_snapshot_is_refused(self) -> None:
        source, session = self._completed_read()
        source.local_read = LocalReadSnapshot()
        self.memory.save_task(session.id, source)
        task = self._referencing_task(source.id)
        run = self.core.run(task, session_id=session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        # Same shadowing hazard as the self-reference case: ``_validated_local_read``
        # would also refuse an empty snapshot, so the detail is what pins the
        # dedicated ``snapshot.present`` gate.
        self.assertIn("carries no local read", run.error.message)

    def test_the_classification_ladder_is_reapplied_on_the_hop(self) -> None:
        # The SOURCE TASK was SENSITIVE; a PUBLIC consumer may not see it.
        source, session = self._completed_read(
            classification=DataClassification.SENSITIVE
        )
        self.assertIs(
            DataClassification.SENSITIVE, source.scope.data_classification
        )
        task = self._referencing_task(
            source.id, classification=DataClassification.PUBLIC
        )
        run = self.core.run(task, session_id=session.id)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertNotIn("carried", json.dumps(run.error.to_dict()))

    def test_a_secret_source_is_never_eligible_for_any_hop(self) -> None:
        source, session = self._completed_read(
            classification=DataClassification.SECRET
        )
        self.assertIs(DataClassification.SECRET, source.scope.data_classification)
        for classification in DataClassification:
            with self.subTest(classification=classification):
                task = self._referencing_task(
                    source.id, classification=classification
                )
                run = self.core.run(task, session_id=session.id)
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)

    def test_a_task_cannot_reference_its_own_read(self) -> None:
        self.write("notes.txt", "self\n")
        task = read_task(self.root / "notes.txt")
        task.context[LOCAL_READ_TASK_REFERENCE_KEY] = task.id
        run = self.core.run(task)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        # The status/code pair alone would NOT pin this guard: every later
        # gate in the hop produces exactly the same pair, so removing the
        # self-reference check would leave the COMPLETED gate to refuse and
        # this test would still pass. The bounded refusal DETAIL is what
        # identifies which gate actually fired.
        self.assertIn("its own local read", run.error.message)
        self.assertNotIn("did not complete", run.error.message)

    def _tamper_persisted_snapshot(self, task_id: str, **overrides) -> None:
        """Corrupt a persisted snapshot the way an out-of-product edit would.

        Raw SQLite on purpose: going through the product's own writers would
        re-apply the very validation this test exists to exercise.
        """
        connection = sqlite3.connect(self.memory.database_path)
        try:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            document = json.loads(row[0])
            document["local_read"].update(overrides)
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task_id),
            )
            connection.commit()
        finally:
            connection.close()

    def test_a_source_tampered_after_completion_is_revalidated_and_refused(
        self,
    ) -> None:
        """A snapshot edited outside the product never reaches a consumer.

        Every other gate is deliberately SATISFIED - same session, COMPLETED
        source, eligible classification - so the only thing that can refuse
        this reference is structural re-validation of the referenced
        snapshot. P1-5 made that one shared contract, so the load boundary is
        the first place it fires and the tampered path never becomes active
        state at all.

        The referencing task uses a capability no registered connector
        handles, so if the hop wrongly succeeded the run would fail later with
        NO_CONNECTOR instead of INVALID_TASK, and the tampered snapshot would
        be visible on the task. Both are asserted.
        """
        source, session = self._completed_read()
        self.assertTrue(source.local_read.present)
        self.assertIs(DataClassification.INTERNAL, source.local_read.classification)

        # A genuinely malformed shape: an absolute path pointing at a
        # credential file, and a non-hex digest.
        self._tamper_persisted_snapshot(
            source.id,
            relative_path="/home/victim/.ssh/id_rsa",
            sha256="NOT-A-DIGEST",
        )
        reloaded_source = self.memory.load_task(source.id)
        self.assertEqual("", reloaded_source.local_read.relative_path)
        self.assertFalse(reloaded_source.local_read.present)
        self.assertIs(TaskStatus.COMPLETED, reloaded_source.status)

        task = Task(
            objective="analyze using the earlier read",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                data_classification=DataClassification.INTERNAL,
            ),
        )
        task.context[LOCAL_READ_TASK_REFERENCE_KEY] = source.id
        run = self.core.run(task, session_id=session.id)

        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        # The tampered path never reaches a diagnostic either.
        self.assertNotIn("/home/victim", json.dumps(run.error.to_dict()))

        carried = self.memory.load_task(task.id)
        self.assertFalse(carried.local_read.present)
        self.assertEqual("", carried.local_read.relative_path)
        # No connector was ever selected and no execution was ever attempted:
        # the refusal happens at CONTEXT_RETRIEVAL, before TOOL_SELECTION.
        self.assertIsNone(carried.selected_connector)
        self.assertIsNone(self.memory.latest_execution_attempt(task.id))

    def test_the_reference_is_accepted_at_the_input_boundary(self) -> None:
        document = json.dumps(
            {
                "objective": "reuse a read",
                "capability": "code_analysis",
                "scope": {"action": "analyze", "target": "src"},
                "context": {LOCAL_READ_TASK_REFERENCE_KEY: " task-123 "},
            }
        )
        task = decode_task_json(document)
        self.assertEqual(
            "task-123", task.context[LOCAL_READ_TASK_REFERENCE_KEY]
        )
        # Content is never accepted from input, only a reference.
        self.assertFalse(task.local_read.present)

    def test_input_cannot_supply_local_read_content(self) -> None:
        from jarvis.app import TaskInputError

        document = json.dumps(
            {
                "objective": "smuggle",
                "capability": "code_analysis",
                "scope": {"action": "analyze", "target": "src"},
                "context": {"local_read": {"excerpt": "injected"}},
            }
        )
        with self.assertRaises(TaskInputError):
            decode_task_json(document)


# --------------------------------------------------------------------------
# 12/13/14. End-to-end: the attested Claude payload
# --------------------------------------------------------------------------


class OutboundPayloadTests(LocalReadTestCase):
    EXPECTED_FIELDS = {"notice", "path", "byte_size", "truncated", "content"}

    def setUp(self) -> None:
        super().setUp()
        self.claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )

    def _task_with_snapshot(
        self,
        *,
        classification: DataClassification = DataClassification.INTERNAL,
        snapshot_classification: DataClassification | None = None,
        content: str = "alpha beta\n",
    ) -> Task:
        self.write("notes.txt", content)
        stored, _ = self.completed_snapshot(
            self.root / "notes.txt", classification=classification
        )
        task = Task(
            objective="analyze the file",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=classification,
            ),
        )
        snapshot = stored.local_read
        if snapshot_classification is not None:
            snapshot = replace(snapshot, classification=snapshot_classification)
        task.local_read = snapshot
        return task

    def _payload(self, task: Task) -> dict:
        payload, _ = self.claude._sanitized_payload(task)
        return payload

    def test_the_payload_carries_exactly_the_allowed_local_read_fields(self) -> None:
        payload = self._payload(self._task_with_snapshot())
        block = payload["local_read"]
        self.assertEqual(self.EXPECTED_FIELDS, set(block))
        self.assertEqual("notes.txt", block["path"])
        self.assertEqual("alpha beta\n", block["content"])
        self.assertFalse(block["truncated"])
        self.assertEqual(claude_code.LOCAL_READ_NOTICE, block["notice"])

    def test_digest_absolute_path_and_timestamp_never_egress(self) -> None:
        task = self._task_with_snapshot()
        payload = self._payload(task)
        serialized = json.dumps(payload, default=str)
        self.assertNotIn(task.local_read.sha256, serialized)
        self.assertNotIn(task.local_read.read_at, serialized)
        block = json.dumps(payload["local_read"], default=str)
        # ``scope.target`` is authoritative H1 state and legitimately carries
        # the canonical path; the local-read block must not.
        self.assertNotIn(str(self.root), block)
        self.assertNotIn(str(self.root).casefold(), block.casefold())
        self.assertNotIn("classification", block)
        self.assertNotIn("sha256", block)

    def test_a_task_without_a_read_contributes_no_key_at_all(self) -> None:
        task = Task(
            objective="analyze",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
            ),
        )
        self.assertNotIn("local_read", self._payload(task))

    def test_a_secret_task_produces_no_local_read_block(self) -> None:
        task = self._task_with_snapshot(classification=DataClassification.INTERNAL)
        task.scope = replace(
            task.scope, data_classification=DataClassification.SECRET
        )
        payload = self._payload(task)
        self.assertNotIn("local_read", payload)
        self.assertNotIn("alpha beta", json.dumps(payload, default=str))

    def test_a_snapshot_above_the_current_classification_drops_the_block(self) -> None:
        task = self._task_with_snapshot(
            classification=DataClassification.PUBLIC,
            snapshot_classification=DataClassification.SENSITIVE,
        )
        payload = self._payload(task)
        self.assertNotIn("local_read", payload)
        # It did not consume a retrieval slot either; retrieval is untouched.
        self.assertNotIn("retrieved_memory", payload)

    def test_a_secret_snapshot_never_egresses(self) -> None:
        task = self._task_with_snapshot(
            snapshot_classification=DataClassification.SECRET
        )
        self.assertNotIn("local_read", self._payload(task))

    def test_a_tampered_oversized_excerpt_drops_the_block(self) -> None:
        """Default-deny, not degrade: the whole block goes, not just the tail.

        P1-5 made every boundary judge the snapshot by the same structural
        contract, and an excerpt past the ceiling fails it. Silently trimming
        an oversized excerpt would still hand the model a block asserting a
        bounded, trustworthy read that the snapshot no longer describes.
        """
        task = self._task_with_snapshot()
        task.local_read = replace(
            task.local_read,
            excerpt="z" * (LOCAL_READ_EXCERPT_MAX_CHARS + 1000),
            truncated=False,
        )
        payload = self._payload(task)
        self.assertNotIn("local_read", payload)
        self.assertNotIn("zzzz", json.dumps(payload, default=str))

    def test_an_excerpt_that_is_not_canonical_drops_the_block(self) -> None:
        """A live credential in a persisted excerpt is not re-redacted here.

        No real read could have produced it - ``bound_local_text`` removed
        credentials before the excerpt was ever stored, and that function is
        a fixed point - so an excerpt carrying one is a tampered row, not
        content to be cleaned up. Re-redacting would let it egress wearing a
        clean face; the whole block is dropped instead.
        """
        task = self._task_with_snapshot()
        secret = "api_key=" + "d" * 20 + " "
        repeats = (LOCAL_READ_EXCERPT_MAX_CHARS // len(secret)) + 1
        task.local_read = replace(task.local_read, excerpt=(secret * repeats)[
            :LOCAL_READ_EXCERPT_MAX_CHARS
        ])
        payload = self._payload(task)
        self.assertNotIn("local_read", payload)
        self.assertNotIn("dddddddddd", json.dumps(payload, default=str))

    def test_a_tampered_absolute_path_drops_the_block(self) -> None:
        for bad in ("/etc/shadow", "C:/Windows/x", "..\\..\\x", "../../x"):
            with self.subTest(bad=bad):
                task = self._task_with_snapshot()
                task.local_read = replace(task.local_read, relative_path=bad)
                self.assertNotIn("local_read", self._payload(task))

    def test_a_credential_in_a_persisted_excerpt_never_egresses(self) -> None:
        task = self._task_with_snapshot()
        task.local_read = replace(
            task.local_read, excerpt="api_key=sk-" + "c" * 30 + "\n"
        )
        payload = self._payload(task)
        self.assertNotIn("local_read", payload)
        self.assertNotIn("sk-cccc", json.dumps(payload, default=str))
        self.assertNotIn(
            "sk-cccc", self.claude.canonical_outbound(task).decode("utf-8")
        )

    def test_the_outbound_digest_covers_the_file_content(self) -> None:
        task = self._task_with_snapshot(content="first version\n")
        first = self.claude.canonical_outbound(task)
        task.local_read = replace(task.local_read, excerpt="second version\n")
        second = self.claude.canonical_outbound(task)
        self.assertNotEqual(first, second)
        self.assertIn("first version", first.decode("utf-8"))

    def test_the_payload_is_byte_identical_across_approve_resume(self) -> None:
        task = self._task_with_snapshot()
        first = self.claude.canonical_outbound(task)
        # A full persistence round trip, exactly like approve -> resume.
        reloaded = Task.from_dict(json.loads(json.dumps(task.to_dict())))
        self.assertEqual(first, self.claude.canonical_outbound(reloaded))

    def test_the_model_is_granted_no_tools_at_all(self) -> None:
        # The whole reason JARVIS reads the bytes itself.
        self.assertEqual((), claude_code.EGRESS_TOOL_GRANTS)
        self.assertEqual((), self.claude.external_tool_grants(Task(
            objective="x", capability="code_analysis", scope=ActionScope(action="a")
        )))

    def test_the_notice_frames_the_content_as_untrusted(self) -> None:
        notice = claude_code.LOCAL_READ_NOTICE.casefold()
        self.assertIn("untrusted", notice)
        self.assertIn("do not follow", notice)


# --------------------------------------------------------------------------
# THE ACCEPTANCE CRITERION: one unbroken chain through the real loop
# --------------------------------------------------------------------------


class EndToEndAcceptanceTests(LocalReadTestCase):
    """task JSON -> loop -> snapshot -> reference -> attested Claude payload.

    Nothing here constructs a snapshot by hand: every artefact is produced by
    ``AgentCore.run`` from decoded task JSON, so the chain that is asserted is
    the chain the product actually executes.
    """

    MARKER = "ACCEPTANCE-CONTENT-MARKER"

    def test_the_whole_chain_from_task_json_to_outbound_bytes(self) -> None:
        target = self.write("src/module.py", f"# {self.MARKER}\ndef go():\n    return 1\n")

        # [1] Untrusted task JSON -> a real read task.
        read_document = json.dumps(
            {
                "objective": "read the module",
                "capability": LOCAL_READ_CAPABILITY,
                "scope": {"action": "read", "target": str(target)},
            }
        )
        read = decode_task_json(read_document)
        read_run = self.core.run(read)
        self.assertEqual(RunStatus.COMPLETED, read_run.status, msg=str(read_run.error))
        session_id = read_run.session.id

        # [2] The snapshot is bound, bounded and persisted.
        stored_read = self.memory.load_task(read.id)
        self.assertEqual("src/module.py", stored_read.local_read.relative_path)
        self.assertIn(self.MARKER, stored_read.local_read.excerpt)

        # [3] A second task JSON references it - by id, never by content.
        analyze_document = json.dumps(
            {
                "objective": "analyze the module",
                "capability": "code_analysis",
                "scope": {
                    "action": "analyze",
                    "target": str(self.root),
                    "destination": "anthropic/claude-code",
                },
                "context": {LOCAL_READ_TASK_REFERENCE_KEY: read.id},
            }
        )
        analyze = decode_task_json(analyze_document)
        self.assertFalse(analyze.local_read.present)

        # [4] The hop happens inside the loop. The analysis connector is not
        # registered here, so the run stops after CONTEXT_RETRIEVAL - which is
        # exactly the point: the hop is the loop's work, not the connector's.
        self.core.run(analyze, session_id=session_id)
        carried = self.memory.load_task(analyze.id)
        self.assertTrue(carried.local_read.present)
        self.assertEqual(stored_read.local_read, carried.local_read)

        # [5] That loop-produced task is what the Claude connector serializes.
        claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        payload, _ = claude._sanitized_payload(carried)
        block = payload["local_read"]
        self.assertEqual(
            {"notice", "path", "byte_size", "truncated", "content"}, set(block)
        )
        self.assertEqual("src/module.py", block["path"])
        self.assertIn(self.MARKER, block["content"])

        # [6] And the content is inside the attested canonical outbound bytes.
        outbound = claude.canonical_outbound(carried)
        self.assertIn(self.MARKER, outbound.decode("utf-8"))
        attestation = claude.egress_attestation(
            carried,
            carried.scope.normalized(),
            RiskLevel.MEDIUM,
            claude.manifest.default_side_effect,
        )
        self.assertEqual(
            hashlib.sha256(outbound).hexdigest(), attestation.outbound_digest
        )

        # [7] The model still receives no tools, so these bytes are everything
        # it can see of the file.
        self.assertEqual((), attestation.tool_grants)

        # [8] Tampering with the persisted snapshot after approval changes the
        # digest, so a stale grant can no longer authorize the call.
        carried.local_read = replace(carried.local_read, excerpt="something else\n")
        self.assertNotEqual(
            attestation.outbound_digest,
            hashlib.sha256(claude.canonical_outbound(carried)).hexdigest(),
        )


# --------------------------------------------------------------------------
# 15. M1/M2 memory invariants are untouched
# --------------------------------------------------------------------------


class MemoryIsolationTests(LocalReadTestCase):
    def test_a_local_read_writes_no_project_memory(self) -> None:
        self.write("notes.txt", "MEMORY-MARKER\n")
        stored, run = self.completed_snapshot(self.root / "notes.txt")
        # No project exists at all, so no memory item can have been written.
        self.assertEqual([], self.memory.list_projects())
        decisions = self.memory.list_decisions(run.session.id)
        self.assertNotIn("MEMORY-MARKER", json.dumps([d.to_dict() for d in decisions]))

    def test_the_snapshot_does_not_consume_the_retrieval_quota(self) -> None:
        self.write("notes.txt", "quota\n")
        stored, _ = self.completed_snapshot(self.root / "notes.txt")
        self.assertEqual(0, stored.retrieved_context.character_count)
        self.assertEqual((), stored.retrieved_context.items)
        self.assertTrue(stored.local_read.present)


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


class EnvelopeTests(LocalReadTestCase):
    def test_the_envelope_carries_the_bounded_block(self) -> None:
        self.write("notes.txt", "envelope\n")
        _, run = self.completed_snapshot(self.root / "notes.txt")
        payload = build_envelope("run", PermissionMode.SESSION, run)
        block = payload["local_read"]
        self.assertEqual(
            {
                "relative_path",
                "byte_size",
                "sha256",
                "classification",
                "truncated",
                "excerpt_characters",
                "excerpt",
                "read_at",
            },
            set(block),
        )
        self.assertEqual("notes.txt", block["relative_path"])
        self.assertEqual("envelope\n", block["excerpt"])

    def test_a_tampered_snapshot_produces_no_envelope_block(self) -> None:
        """The public envelope re-validates, it does not mask.

        ``LocalReadSnapshot.from_dict`` loads defensively but deliberately
        does not re-derive the path shape or the digest format, so a row
        edited outside the product reaches memory intact. The envelope is the
        boundary the USER reads, so it applies the same re-validation the
        Claude egress boundary applies - and drops the whole block rather
        than printing a masked path, which would still assert that a bounded,
        trustworthy read had happened.
        """
        self.write("notes.txt", "envelope\n")
        _, run = self.completed_snapshot(self.root / "notes.txt")
        task = run.task

        hostile = (
            {"relative_path": "/home/victim/.ssh/id_rsa"},
            {"relative_path": "C:\\Users\\victim\\.ssh\\id_rsa"},
            {"relative_path": "../../../etc/shadow"},
            {"relative_path": "docs/../../escape.txt"},
            {"sha256": "NOT-A-DIGEST"},
            {"sha256": "A" * 64},
            {"sha256": ""},
            {"byte_size": LOCAL_READ_MAX_BYTES + 1},
            {"excerpt": "z" * (LOCAL_READ_EXCERPT_MAX_CHARS + 1)},
            {"classification": "internal"},
            {"truncated": "yes"},
            {
                "relative_path": "/home/victim/.ssh/id_rsa",
                "sha256": "NOT-A-DIGEST",
                "byte_size": -1,
            },
        )
        for overrides in hostile:
            with self.subTest(overrides=overrides):
                task.local_read = replace(run.task.local_read)
                for field, value in overrides.items():
                    object.__setattr__(task.local_read, field, value)
                payload = build_envelope("run", PermissionMode.SESSION, run)
                self.assertIsNone(
                    payload["local_read"],
                    msg=f"tampered snapshot rendered: {overrides}",
                )
                serialized = json.dumps(payload)
                self.assertNotIn("/home/victim", serialized)
                self.assertNotIn("C:\\Users\\victim", serialized)
                self.assertNotIn("NOT-A-DIGEST", serialized)
                self.assertNotIn("etc/shadow", serialized)

    def test_a_valid_snapshot_still_renders_the_full_block(self) -> None:
        # The re-validation must not narrow the v3 contract for good rows.
        self.write("notes.txt", "still fine\n")
        _, run = self.completed_snapshot(self.root / "notes.txt")
        payload = build_envelope("run", PermissionMode.SESSION, run)
        self.assertEqual(3, payload["schema_version"])
        self.assertIsNotNone(payload["local_read"])
        self.assertEqual("still fine\n", payload["local_read"]["excerpt"])

    def test_the_envelope_never_shows_an_absolute_path(self) -> None:
        self.write("deep/nested/notes.txt", "deep\n")
        _, run = self.completed_snapshot(self.root / "deep" / "nested" / "notes.txt")
        payload = build_envelope("run", PermissionMode.SESSION, run)
        self.assertEqual("deep/nested/notes.txt", payload["local_read"]["relative_path"])
        self.assertNotIn(str(self.root), json.dumps(payload))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
# --------------------------------------------------------------------------
# P1-1. The approved DIRECTORY CHAIN, proven at open time
# --------------------------------------------------------------------------


def make_directory_link(target: Path, link: Path) -> bool:
    """Point ``link`` at directory ``target``, however this platform can.

    A directory symlink where that is permitted, an NTFS junction otherwise -
    junctions matter because creating one needs no privilege at all, which is
    exactly what makes the intermediate-directory swap a realistic attack on
    Windows rather than a theoretical one.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    try:  # pragma: no cover - Windows only
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return True
    except Exception:  # pragma: no cover - unsupported platform
        return False


class ParentDirectoryRaceTests(LocalReadTestCase):
    """A parent component re-pointed AFTER validation must yield zero bytes.

    ``resolve()`` proves a pathname, not an object. Everything the connector
    checked - containment, the secret-file policy, the pre-open stat - was
    checked against a chain that no longer exists by the time ``open`` runs,
    and the device/inode binding agrees with the swapped file because the
    swap landed before the stat too. Only a check that binds the OPEN itself
    to the approved chain can refuse this.
    """

    MARKER = "OUTSIDE-ROOT-MARKER-4b1c"

    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.session = self.core.start_session("race")

    def _stage(self, depth: int) -> tuple[Path, Path, Path]:
        """An in-root chain and an identical-looking one outside the root.

        Returns the target inside the root, the intermediate directory that
        will be swapped, and the outside directory it will be swapped for.
        """
        segments = [f"level{index}" for index in range(depth)]
        inside_directory = self.root.joinpath(*segments)
        inside_directory.mkdir(parents=True)
        target = inside_directory / "notes.txt"
        target.write_text("inside content\n", encoding="utf-8")

        decoy = self.outside.joinpath(*segments[1:])
        decoy.mkdir(parents=True, exist_ok=True)
        (decoy / "notes.txt").write_text(f"{self.MARKER}\n", encoding="utf-8")
        return target, self.root / segments[0], self.outside

    def _swap(self, swapped: Path, replacement: Path) -> None:
        shutil.rmtree(swapped)
        if not make_directory_link(replacement, swapped):  # pragma: no cover
            self.skipTest("this platform cannot create a directory link")

    def _race(self, depth: int, *, seam: str) -> Any:
        """Run one read with the swap landing inside the chosen race window.

        ``seam`` names the connector call the swap is coordinated with, which
        is what makes the race deterministic instead of timing-dependent:

        * ``policy`` - the secret-file policy check, i.e. AFTER containment
          and the path-name checks but BEFORE the pre-open stat. This is the
          window the device/inode binding cannot see, because the stat and
          the open then both observe the swapped file and agree.
        * ``chain`` - after the pre-open no-follow component scan has already
          passed, so only the open itself can still refuse. This isolates the
          handle-level guarantee from the pathname-level one.
        """
        target, swapped, replacement = self._stage(depth)
        task = read_task(target)
        state = {"done": False}

        def once() -> None:
            if not state["done"]:
                state["done"] = True
                self._swap(swapped, replacement)

        if seam == "policy":
            original = local_files.local_read_denial_reason

            def patched(path):
                once()
                return original(path)

            with mock.patch.object(
                local_files, "local_read_denial_reason", patched
            ):
                result = self.connector.execute(task, self.session)
        else:
            original_chain = LocalFileReadConnector._reject_reparse_chain

            def patched_chain(connector, root, parts):
                original_chain(connector, root, parts)
                once()

            with mock.patch.object(
                LocalFileReadConnector, "_reject_reparse_chain", patched_chain
            ):
                result = self.connector.execute(task, self.session)
        self.assertTrue(state["done"], "the race window was never reached")
        return result

    def _assert_no_outside_bytes(self, result: Any) -> None:
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        serialized = json.dumps(
            {
                "summary": result.summary,
                "data": result.data,
                "error": None if result.error is None else result.error.to_dict(),
            },
            default=str,
        )
        self.assertNotIn(self.MARKER, serialized)

    def test_an_intermediate_directory_repointed_after_validation_is_refused(
        self,
    ) -> None:
        self._assert_no_outside_bytes(self._race(1, seam="policy"))

    def test_a_nested_intermediate_directory_repoint_is_refused(self) -> None:
        # The swapped component is three levels above the file, so nothing
        # about the final component or its parent changed by name.
        self._assert_no_outside_bytes(self._race(3, seam="policy"))

    def test_the_open_itself_proves_containment_without_the_pre_open_scan(
        self,
    ) -> None:
        """The pathname scan is not what makes this safe.

        With the pre-open component scan already satisfied, the swap lands in
        the last remaining window. Where the platform opens through directory
        handles the swapped component cannot be traversed at all; on Windows
        the opened handle is asked for its own authoritative final path and
        that answer lands outside the roots. Either way: zero bytes.
        """
        self._assert_no_outside_bytes(self._race(2, seam="chain"))

    def test_a_junction_already_in_place_never_completes_a_run(self) -> None:
        """The same attack without any race at all, end to end through the loop."""
        target, swapped, replacement = self._stage(1)
        self._swap(swapped, replacement)
        task, run = self.run_read(target)
        self.assertEqual(RunStatus.FAILED, run.status)
        stored = self.memory.load_task(task.id)
        self.assertFalse(stored.local_read.present)
        self.assertNotIn(self.MARKER, json.dumps(stored.to_dict(), default=str))
        self.assertNotIn(
            self.MARKER,
            Path(self.memory.database_path).read_bytes().decode("latin-1"),
        )


# --------------------------------------------------------------------------
# P1-2. One canonical identity per filesystem target, whatever its spelling
# --------------------------------------------------------------------------


@contextlib.contextmanager
def working_directory(path: Path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class CanonicalTargetBindingTests(LocalReadTestCase):
    """A bare relative name is a FILESYSTEM target, not a symbolic one.

    ``normalize_locator`` routes on shape: no separator and no drive letter
    meant ``README.md`` fell through to ``normalize_symbol`` and stayed
    symbolic, while ``.\\README.md`` canonicalized. Two fingerprints for one
    file - and the symbolic one was never re-resolved, so a grant issued
    against it stayed valid while the name was pointed somewhere else.
    """

    permission_mode = PermissionMode.STRICT
    MARKER = "REPOINTED-TARGET-MARKER-77ac"

    def _bind(self, spelling: str) -> Task:
        task = read_task(spelling)
        refusal = self.core._bind_canonical_local_target(
            SessionState(objective="binding"), task
        )
        self.assertIsNone(refusal, msg=str(refusal))
        return task

    def _bound_scope(self, spelling: str) -> ActionScope:
        return self._bind(spelling).scope.normalized()

    def _scope_fingerprint(self, spelling: str) -> str:
        request = self.connector.permission_request(
            self._bind(spelling), "fixed-session-id"
        )
        return request.scope_fingerprint

    def test_bare_and_dot_relative_spellings_share_one_identity(self) -> None:
        self.write("README.md", "readme\n")
        with working_directory(self.root):
            spellings = ("README.md", ".\\README.md", "./README.md")
            targets = {self._bound_scope(name).target for name in spellings}
            fingerprints = {self._scope_fingerprint(name) for name in spellings}
        self.assertEqual(1, len(targets), targets)
        self.assertEqual(1, len(fingerprints), fingerprints)
        self.assertEqual({canonical_path(self.root / "README.md")}, targets)

    def test_alternate_dot_and_parent_spellings_share_one_identity(self) -> None:
        self.write("docs/README.md", "readme\n")
        with working_directory(self.root):
            spellings = (
                "docs/README.md",
                "./docs/README.md",
                "docs/../docs/README.md",
                str(self.root / "docs" / "README.md"),
            )
            targets = {self._bound_scope(name).target for name in spellings}
        self.assertEqual(1, len(targets), targets)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_a_bare_relative_symlink_target_binds_to_the_real_file(self) -> None:
        real = self.write("real.md", "real content\n")
        link = self.root / "README.md"
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlink creation not permitted")
        with working_directory(self.root):
            bare = self._bound_scope("README.md")
        # The symbolic spelling no longer survives anywhere: the authority is
        # the file the link resolves to, not the name that pointed at it.
        self.assertEqual(canonical_path(real), bare.target)
        self.assertNotEqual("readme.md", bare.target)

    def test_the_root_relative_display_name_is_still_the_only_public_form(
        self,
    ) -> None:
        """Canonical identity is authority; the safe display name is separate."""
        self.write("docs/notes.txt", "display\n")
        with working_directory(self.root):
            app = create_app(
                settings=Settings(
                    memory_database=self.root / "display.db",
                    permission_mode=PermissionMode.SESSION,
                ),
                connectors=(self.connector,),
            )
            task = read_task("docs/notes.txt")
            run = app.core.run(task)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        stored = app.memory.load_task(task.id)
        self.assertEqual("docs/notes.txt", stored.local_read.relative_path)
        envelope = build_envelope("run", PermissionMode.SESSION, run)
        self.assertEqual(
            "docs/notes.txt", envelope["local_read"]["relative_path"]
        )
        self.assertNotIn(
            str(self.root).casefold(),
            json.dumps(envelope["local_read"], default=str).casefold(),
        )

    def _approve_bare_name(self, name: str = "notes.txt"):
        """Reach an issued approval for a BARE relative target."""
        with working_directory(self.root):
            task = read_task(name)
            run = self.core.run(task)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status)
        return task, run, PermissionGrant.for_request(run.permission_request)

    def _assert_no_marker_anywhere(self, task_id: str) -> None:
        stored = self.memory.load_task(task_id)
        self.assertFalse(stored.local_read.present)
        self.assertNotIn(self.MARKER, json.dumps(stored.to_dict(), default=str))
        self.assertNotIn(
            self.MARKER,
            Path(self.memory.database_path).read_bytes().decode("latin-1"),
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_a_stale_approval_executes_zero_times_after_the_identity_changes(
        self,
    ) -> None:
        """Approve a bare name, then point that name at ANOTHER LEGAL FILE.

        The decoy is an ordinary readable file inside the same allowed root,
        so containment, the secret-file policy and the path grammar all still
        say yes - nothing but the resource IDENTITY changed. That is the
        whole point: before P1-2 the approval fingerprint covered the SYMBOL
        ``notes.txt``, which a repoint does not change, while execution
        resolved the name live and read whatever it now pointed at. Canonical
        binding makes the fingerprint the FILE, so the repoint changes the
        fingerprint and the stale grant authorizes nothing.
        """
        approved = self.write("notes.txt", "approved content\n")
        self.write("other.txt", f"{self.MARKER}\n")
        task, run, grant = self._approve_bare_name()

        approved.unlink()
        try:
            os.symlink(self.root / "other.txt", self.root / "notes.txt")
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlink creation not permitted")
        with working_directory(self.root):
            resumed = self.core.resume(run.session.id, task.id, grant)

        self.assertNotEqual(RunStatus.COMPLETED, resumed.status)
        self.assertIsNone(self.memory.latest_execution_attempt(task.id))
        self._assert_no_marker_anywhere(task.id)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_a_repoint_out_of_the_roots_is_refused_as_well(self) -> None:
        """The same repoint, aimed outside: containment refuses independently."""
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "evil.txt").write_text(f"{self.MARKER}\n", encoding="utf-8")
        approved = self.write("notes.txt", "approved content\n")
        task, run, grant = self._approve_bare_name()

        approved.unlink()
        try:
            os.symlink(outside / "evil.txt", self.root / "notes.txt")
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlink creation not permitted")
        with working_directory(self.root):
            resumed = self.core.resume(run.session.id, task.id, grant)

        self.assertNotEqual(RunStatus.COMPLETED, resumed.status)
        self.assertIsNone(self.memory.latest_execution_attempt(task.id))
        self._assert_no_marker_anywhere(task.id)

    def test_an_unchanged_target_still_executes_under_its_own_approval(self) -> None:
        """The corrections must refuse a changed identity, not every resume."""
        self.write("notes.txt", "approved content\n")
        task, run, grant = self._approve_bare_name()
        with working_directory(self.root):
            resumed = self.core.resume(run.session.id, task.id, grant)
        self.assertEqual(RunStatus.COMPLETED, resumed.status, msg=str(resumed.error))
        stored = self.memory.load_task(task.id)
        self.assertEqual("notes.txt", stored.local_read.relative_path)
        self.assertEqual("approved content\n", stored.local_read.excerpt)


# --------------------------------------------------------------------------
# P1-3. The SOURCE TASK, never its snapshot, is classification authority
# --------------------------------------------------------------------------


class SourceTaskClassificationAuthorityTests(LocalReadTestCase):
    """A persisted snapshot classification is data, not authority.

    ``LocalReadSnapshot.classification`` lives in a task row. A row edited
    outside the product can say PUBLIC about a SECRET task's read, and
    because ``DataClassification`` derives from ``str`` a raw ``"public"``
    satisfies both the rank lookup and ``retrieval_eligible`` silently. The
    hop therefore reads the SOURCE TASK's own ``scope.data_classification``
    and rebuilds the snapshot from it before any ladder comparison.
    """

    MARKER = "SOURCE-AUTHORITY-MARKER-51de"

    def _source(self, classification: DataClassification):
        self.write("notes.txt", f"{self.MARKER}\n")
        stored, run = self.completed_snapshot(
            self.root / "notes.txt", classification=classification
        )
        return stored, run.session

    def _tamper_snapshot_classification(self, task_id: str, value: Any) -> None:
        connection = sqlite3.connect(self.memory.database_path)
        try:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            document = json.loads(row[0])
            document["local_read"]["classification"] = value
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task_id),
            )
            connection.commit()
        finally:
            connection.close()

    def _consumer(self, source_id: str, classification: DataClassification) -> Task:
        task = Task(
            objective="analyze using the earlier read",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                data_classification=classification,
            ),
        )
        task.context[LOCAL_READ_TASK_REFERENCE_KEY] = source_id
        return task

    def _run_hop(self, source, session, classification: DataClassification):
        task = self._consumer(source.id, classification)
        run = self.core.run(task, session_id=session.id)
        return task, run

    def test_a_tampered_public_snapshot_cannot_lower_a_sensitive_source(
        self,
    ) -> None:
        source, session = self._source(DataClassification.SENSITIVE)
        self._tamper_snapshot_classification(
            source.id, DataClassification.PUBLIC.value
        )
        reloaded = self.memory.load_task(source.id)
        self.assertIs(DataClassification.PUBLIC, reloaded.local_read.classification)

        task, run = self._run_hop(source, session, DataClassification.PUBLIC)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("classification ladder", run.error.message)
        carried = self.memory.load_task(task.id)
        self.assertFalse(carried.local_read.present)
        self.assertNotIn(self.MARKER, json.dumps(carried.to_dict(), default=str))

    def test_a_tampered_public_snapshot_cannot_lower_a_secret_source(self) -> None:
        source, session = self._source(DataClassification.SECRET)
        self._tamper_snapshot_classification(
            source.id, DataClassification.PUBLIC.value
        )
        for classification in DataClassification:
            with self.subTest(classification=classification):
                task, run = self._run_hop(source, session, classification)
                self.assertEqual(RunStatus.FAILED, run.status)
                self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
                self.assertFalse(
                    self.memory.load_task(task.id).local_read.present
                )

    def test_a_raw_string_snapshot_classification_cannot_grant_eligibility(
        self,
    ) -> None:
        # A raw ``str`` compares AND hashes equal to the enum member, so it
        # would satisfy the ladder silently if the snapshot were authority.
        source, session = self._source(DataClassification.SENSITIVE)
        self._tamper_snapshot_classification(source.id, "public")
        task, run = self._run_hop(source, session, DataClassification.PUBLIC)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertFalse(self.memory.load_task(task.id).local_read.present)

    def test_a_snapshot_claiming_higher_than_its_source_cannot_raise_authority(
        self,
    ) -> None:
        """Authority is the source task in BOTH directions, not a maximum."""
        source, session = self._source(DataClassification.INTERNAL)
        self._tamper_snapshot_classification(
            source.id, DataClassification.SENSITIVE.value
        )
        task, run = self._run_hop(source, session, DataClassification.INTERNAL)
        carried = self.memory.load_task(task.id)
        self.assertTrue(carried.local_read.present)
        self.assertIs(
            DataClassification.INTERNAL, carried.local_read.classification
        )

    def test_an_internal_source_with_a_matching_snapshot_succeeds(self) -> None:
        source, session = self._source(DataClassification.INTERNAL)
        task, run = self._run_hop(source, session, DataClassification.INTERNAL)
        carried = self.memory.load_task(task.id)
        self.assertTrue(carried.local_read.present)
        self.assertIn(self.MARKER, carried.local_read.excerpt)
        self.assertIs(
            DataClassification.INTERNAL, carried.local_read.classification
        )

    def test_no_connector_or_model_runs_on_a_classification_refusal(self) -> None:
        source, session = self._source(DataClassification.SENSITIVE)
        task, run = self._run_hop(source, session, DataClassification.PUBLIC)
        self.assertEqual(RunStatus.FAILED, run.status)
        carried = self.memory.load_task(task.id)
        self.assertIsNone(carried.selected_connector)
        self.assertIsNone(self.memory.latest_execution_attempt(task.id))

    def test_a_malformed_source_task_classification_fails_closed(self) -> None:
        source, session = self._source(DataClassification.INTERNAL)
        loaded = self.memory.load_task(source.id)
        object.__setattr__(loaded.scope, "data_classification", "internal")
        with mock.patch.object(
            self.memory, "load_task", side_effect=lambda task_id: (
                loaded if task_id == source.id else self.memory.__class__.load_task(
                    self.memory, task_id
                )
            )
        ):
            task, run = self._run_hop(source, session, DataClassification.INTERNAL)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIn("classification is malformed", run.error.message)


# --------------------------------------------------------------------------
# P1-4. A credential-bearing path NAME fails closed, it is never redacted
# --------------------------------------------------------------------------


class CredentialBearingPathTests(LocalReadTestCase):
    """The path is a resource identifier, so it is refused, not rewritten.

    Redacting ``api_key=HUNTER2.txt`` into ``api_key=[REDACTED].txt`` would
    make the approved identity and the displayed identity two different
    strings while still asserting that a bounded, trustworthy read happened.
    Refusing keeps them the same string.
    """

    SECRET = "HUNTER2"

    CREDENTIAL_NAMES = (
        "api_key=HUNTER2.txt",
        "password=HUNTER2.md",
        "token=HUNTER2.log",
        "notes --api-key HUNTER2.txt",
        "config/authorization=HUNTER2.txt",
    )
    SAFE_NAMES = ("apikey.md", "tokenizer.py", "secret_handling.md", "id_rsa.pub")

    def test_the_resource_boundary_refuses_before_any_approval_exists(self) -> None:
        for name in self.CREDENTIAL_NAMES:
            with self.subTest(name=name):
                self.write(name, "harmless content\n")
                task = read_task(self.root / name)
                with self.assertRaises(AttestationError) as caught:
                    self.connector.resource_boundary(task)
                self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)
                self.assertNotIn(self.SECRET, caught.exception.args[0])

    def test_a_credential_bearing_name_never_completes_a_run(self) -> None:
        for name in self.CREDENTIAL_NAMES:
            with self.subTest(name=name):
                self.write(name, "harmless content\n")
                task, run = self.run_read(self.root / name)
                self.assertEqual(RunStatus.FAILED, run.status)
                stored = self.memory.load_task(task.id)
                self.assertFalse(stored.local_read.present)

    def test_safe_names_that_merely_mention_credentials_stay_readable(self) -> None:
        for name in self.SAFE_NAMES:
            with self.subTest(name=name):
                self.write(name, "ordinary project file\n")
                stored, _ = self.completed_snapshot(self.root / name)
                self.assertEqual(name, stored.local_read.relative_path)

    def test_the_secret_marker_reaches_no_boundary_at_all(self) -> None:
        """Approval preview, envelope, canonical outbound and argv."""
        name = "api_key=HUNTER2.txt"
        self.write(name, "harmless content\n")
        strict = create_app(
            settings=Settings(
                memory_database=self.root / "credential.db",
                permission_mode=PermissionMode.STRICT,
            ),
            connectors=(self.connector,),
        )
        task = read_task(self.root / name)
        run = strict.core.run(task)
        self.assertNotEqual(RunStatus.COMPLETED, run.status)
        # No approval request was ever issued, so there is no preview to leak.
        self.assertIsNone(run.permission_request)
        envelope = build_envelope("run", PermissionMode.STRICT, run)
        self.assertIsNone(envelope["local_read"])
        self.assertNotIn(self.SECRET, json.dumps(envelope, default=str))

        # And a snapshot carrying such a path never egresses either.
        claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        consumer = Task(
            objective="analyze",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
            ),
        )
        consumer.local_read = LocalReadSnapshot(
            relative_path=name,
            byte_size=18,
            sha256="a" * 64,
            excerpt="harmless content\n",
            truncated=False,
            classification=DataClassification.INTERNAL,
            read_at="2026-01-01T00:00:00+00:00",
        )
        payload, _ = claude._sanitized_payload(consumer)
        self.assertNotIn("local_read", payload)
        outbound = claude.canonical_outbound(consumer)
        self.assertNotIn(self.SECRET, outbound.decode("utf-8"))
        argv = claude._command(outbound.decode("utf-8"))
        self.assertNotIn(self.SECRET, json.dumps(argv))
        self.assertIsNone(_local_read_payload(consumer.local_read))

    def test_the_shared_credential_policy_is_what_decides(self) -> None:
        # No name-shaped guessing: the refusal is the same predicate every
        # other egress boundary already uses.
        for name in self.CREDENTIAL_NAMES:
            self.assertTrue(credential_bearing(name), name)
            self.assertEqual(
                "path is credential-bearing", local_read_path_refusal(name)
            )
        for name in self.SAFE_NAMES:
            self.assertFalse(credential_bearing(name), name)
            self.assertIsNone(local_read_path_refusal(name))


# --------------------------------------------------------------------------
# P1-5. ONE bounded, exactly-typed structural contract
# --------------------------------------------------------------------------


class SnapshotStructuralContractTests(LocalReadTestCase):
    """The same contract at every authority boundary, not four look-alikes."""

    def valid(self, **overrides) -> LocalReadSnapshot:
        base = dict(
            relative_path="docs/notes.txt",
            byte_size=11,
            sha256="a" * 64,
            excerpt="alpha beta\n",
            truncated=False,
            classification=DataClassification.INTERNAL,
            read_at="2026-01-01T00:00:00+00:00",
        )
        base.update(overrides)
        return LocalReadSnapshot(**base)

    def assert_refused_everywhere(self, snapshot: LocalReadSnapshot) -> None:
        """Every boundary must agree, or the contract is not centralized."""
        self.assertIsNotNone(local_read_snapshot_refusal(snapshot))
        with self.assertRaises(TypeError):
            AgentCore._validated_local_read(snapshot)
        self.assertIsNone(_local_read_payload(snapshot))
        claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        task = Task(
            objective="analyze",
            capability="code_analysis",
            scope=ActionScope(action="analyze", target=str(self.root)),
        )
        task.local_read = snapshot
        block, _ = claude._local_read(task)
        self.assertIsNone(block)

    def test_the_valid_snapshot_is_accepted_at_every_boundary(self) -> None:
        snapshot = self.valid()
        self.assertIsNone(local_read_snapshot_refusal(snapshot))
        self.assertIs(snapshot, AgentCore._validated_local_read(snapshot))
        self.assertIsNotNone(_local_read_payload(snapshot))

    def test_a_hundred_thousand_character_path_is_refused(self) -> None:
        self.assert_refused_everywhere(self.valid(relative_path="a" * 100_000))

    def test_the_path_length_boundary_is_exact(self) -> None:
        for length, refused in (
            (LOCAL_READ_PATH_MAX_CHARS - 1, False),
            (LOCAL_READ_PATH_MAX_CHARS, False),
            (LOCAL_READ_PATH_MAX_CHARS + 1, True),
        ):
            with self.subTest(length=length):
                snapshot = self.valid(relative_path="a" * length)
                self.assertEqual(
                    refused, local_read_snapshot_refusal(snapshot) is not None
                )

    def test_a_non_string_path_is_refused(self) -> None:
        for value in (None, 7, b"docs/notes.txt", ["docs"], Path("docs/notes.txt")):
            with self.subTest(value=value):
                self.assert_refused_everywhere(self.valid(relative_path=value))

    def test_boolean_numeric_fields_are_refused(self) -> None:
        self.assert_refused_everywhere(self.valid(byte_size=True))
        self.assert_refused_everywhere(self.valid(byte_size=False))

    def test_oversized_and_negative_byte_sizes_are_refused(self) -> None:
        for size in (-1, LOCAL_READ_MAX_BYTES + 1, 2**63, 10**40):
            with self.subTest(size=size):
                self.assert_refused_everywhere(self.valid(byte_size=size))

    def test_the_byte_size_boundary_is_exact(self) -> None:
        for size in (0, LOCAL_READ_MAX_BYTES - 1, LOCAL_READ_MAX_BYTES):
            with self.subTest(size=size):
                self.assertIsNone(
                    local_read_snapshot_refusal(self.valid(byte_size=size))
                )

    def test_the_excerpt_boundary_is_exact(self) -> None:
        for length, refused in (
            (LOCAL_READ_EXCERPT_MAX_CHARS - 1, False),
            (LOCAL_READ_EXCERPT_MAX_CHARS, False),
            (LOCAL_READ_EXCERPT_MAX_CHARS + 1, True),
        ):
            with self.subTest(length=length):
                snapshot = self.valid(excerpt="x" * length)
                self.assertEqual(
                    refused, local_read_snapshot_refusal(snapshot) is not None
                )

    def test_a_non_string_read_at_is_refused(self) -> None:
        for value in (None, 7, 1.5, True, datetime(2026, 1, 1, tzinfo=timezone.utc)):
            with self.subTest(value=value):
                self.assert_refused_everywhere(self.valid(read_at=value))

    def test_malformed_timestamp_strings_are_refused(self) -> None:
        for value in (
            "",
            "whenever",
            "2026-01-01",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00+02:00",
            "2026-13-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00" + "0" * 200,
        ):
            with self.subTest(value=value):
                self.assert_refused_everywhere(self.valid(read_at=value))

    def test_the_canonical_timestamp_this_repo_writes_is_accepted(self) -> None:
        self.assertIsNone(local_read_snapshot_refusal(self.valid(read_at=utc_now())))
        self.assertIsNone(
            local_read_snapshot_refusal(self.valid(read_at="2026-01-01T00:00:00Z"))
        )

    def test_uppercase_and_malformed_digests_are_refused(self) -> None:
        for digest in ("A" * 64, "a" * 63, "a" * 65, "", "g" * 64, None, 7, "0X" * 32):
            with self.subTest(digest=digest):
                self.assert_refused_everywhere(self.valid(sha256=digest))

    def test_non_boolean_truncation_and_foreign_classifications_are_refused(
        self,
    ) -> None:
        for value in (0, 1, "false", None):
            with self.subTest(truncated=value):
                self.assert_refused_everywhere(self.valid(truncated=value))
        for value in ("internal", "public", None, 3):
            with self.subTest(classification=value):
                self.assert_refused_everywhere(self.valid(classification=value))

    def test_malformed_persisted_combinations_load_closed(self) -> None:
        base = {
            "relative_path": "notes.txt",
            "byte_size": 11,
            "sha256": "a" * 64,
            "excerpt": "alpha\n",
            "truncated": False,
            "classification": DataClassification.INTERNAL.value,
            "read_at": "2026-01-01T00:00:00+00:00",
        }
        self.assertTrue(LocalReadSnapshot.from_dict(dict(base)).present)
        for override in (
            {"relative_path": "a" * 100_000},
            {"relative_path": "/etc/shadow"},
            {"relative_path": 7},
            {"relative_path": "api_key=HUNTER2.txt"},
            {"byte_size": True},
            {"byte_size": -1},
            {"byte_size": LOCAL_READ_MAX_BYTES + 1},
            {"byte_size": "11"},
            {"sha256": "A" * 64},
            {"sha256": None},
            {"truncated": 1},
            {"read_at": 12345},
            {"read_at": "whenever"},
            {"read_at": None},
        ):
            with self.subTest(override=override):
                document = dict(base)
                document.update(override)
                snapshot = LocalReadSnapshot.from_dict(document)
                self.assertFalse(snapshot.present)
                self.assertEqual("", snapshot.excerpt)
                self.assertIs(DataClassification.SECRET, snapshot.classification)

    def test_load_resave_reload_stays_bounded_and_fail_closed(self) -> None:
        self.write("notes.txt", "round\n")
        stored, _ = self.completed_snapshot(self.root / "notes.txt")
        again = Task.from_dict(json.loads(json.dumps(stored.to_dict())))
        third = Task.from_dict(json.loads(json.dumps(again.to_dict())))
        self.assertEqual(stored.local_read, third.local_read)
        self.assertIsNone(local_read_snapshot_refusal(third.local_read))

        # A row broken in storage stays broken-closed across the round trip.
        document = stored.to_dict()
        document["local_read"]["read_at"] = "whenever"
        reloaded = Task.from_dict(document)
        self.assertFalse(reloaded.local_read.present)
        replayed = Task.from_dict(json.loads(json.dumps(reloaded.to_dict())))
        self.assertFalse(replayed.local_read.present)

    def test_an_oversized_scalar_never_reaches_task_persistence(self) -> None:
        self.write("notes.txt", "persist\n")
        stored, _ = self.completed_snapshot(self.root / "notes.txt")
        stored.local_read = replace(
            stored.local_read, relative_path="a" * 100_000
        )
        session_id = self.memory.task_session_id(stored.id)
        # ``to_dict`` is what persistence writes; the load boundary refuses it.
        reloaded = Task.from_dict(json.loads(json.dumps(stored.to_dict())))
        self.assertFalse(reloaded.local_read.present)
        self.assertIsNotNone(session_id)


# --------------------------------------------------------------------------
# P2-6. An explicitly empty authority set is invalid, never "the CWD"
# --------------------------------------------------------------------------


class AllowedRootsConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self._temp)
        self.root = Path(self._temp.name).resolve()

    def test_omitting_the_roots_uses_the_documented_default(self) -> None:
        connector = LocalFileReadConnector(default_working_directory=self.root)
        self.assertEqual((self.root,), connector.allowed_roots)
        explicit_none = LocalFileReadConnector(
            allowed_roots=None, default_working_directory=self.root
        )
        self.assertEqual((self.root,), explicit_none.allowed_roots)

    def test_an_explicitly_empty_root_set_is_rejected(self) -> None:
        for empty in ((), [], set(), (name for name in ())):
            with self.subTest(empty=type(empty).__name__):
                with self.assertRaises(ValueError):
                    LocalFileReadConnector(
                        allowed_roots=empty, default_working_directory=self.root
                    )

    def test_one_valid_root_succeeds(self) -> None:
        connector = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.assertEqual((self.root,), connector.allowed_roots)
        connector = LocalFileReadConnector(
            allowed_roots=(name for name in (str(self.root),)),
            default_working_directory=self.root,
        )
        self.assertEqual((self.root,), connector.allowed_roots)

    def test_invalid_or_blank_root_values_fail_closed(self) -> None:
        for bad, expected in (
            (7, TypeError),
            (None, TypeError),
            (b"/tmp", TypeError),
            ("", ValueError),
            ("   ", ValueError),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(expected):
                    LocalFileReadConnector(
                        allowed_roots=(self.root, bad),
                        default_working_directory=self.root,
                    )

    def test_no_empty_authority_set_can_ever_fall_back_to_the_cwd(self) -> None:
        """The rule, stated as the property it protects."""
        with self.assertRaises(ValueError):
            LocalFileReadConnector(
                allowed_roots=(), default_working_directory=self.root
            )
# --------------------------------------------------------------------------
# R2-1. The chain is only as strong as the ROOT it starts from
# --------------------------------------------------------------------------


class AllowedRootIdentityTests(LocalReadTestCase):
    """Containment is anchored to the root OBJECT, not to the root's name.

    Every containment decision this connector makes ends at "…and that is
    inside an allowed root". If the only thing tying the open to the root is
    the root's PATHNAME, the whole chain inherits that weakness: re-point the
    allowed root itself between construction and open and a read that passes
    every check is contained in the wrong directory. The root's
    ``(st_dev, st_ino)`` is therefore recorded when the authority set is
    created and re-proven at open time - by ``fstat`` on the opened root
    handle where the platform walks directory handles, and by ``lstat`` at
    the head of the no-follow scan where it cannot.
    """

    MARKER = "SWAPPED-ROOT-MARKER-2ad9"

    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.outside, True)
        # A nested root, so the root itself can be swapped without disturbing
        # the test database that lives in the temp directory above it.
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.scoped = LocalFileReadConnector(
            allowed_roots=(self.workspace,),
            default_working_directory=self.workspace,
        )
        self.session = self.core.start_session("root identity")

    def test_the_root_identity_is_recorded_when_authority_is_created(self) -> None:
        info = os.stat(self.workspace)
        self.assertEqual(
            {self.workspace: (info.st_dev, info.st_ino)}, self.scoped._root_identity
        )

    def test_a_root_swapped_for_a_link_elsewhere_yields_zero_bytes(self) -> None:
        target = self.workspace / "notes.txt"
        target.write_text("inside content\n", encoding="utf-8")
        decoy = self.outside / "notes.txt"
        decoy.write_text(f"{self.MARKER}\n", encoding="utf-8")

        shutil.rmtree(self.workspace)
        if not make_directory_link(self.outside, self.workspace):  # pragma: no cover
            self.skipTest("this platform cannot create a directory link")

        result = self.scoped.execute(read_task(target), self.session)
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertNotIn(
            self.MARKER,
            json.dumps(
                {"summary": result.summary, "data": result.data}, default=str
            ),
        )

    def test_a_root_whose_identity_no_longer_matches_fails_closed(self) -> None:
        """The mechanism itself, independent of what the platform permits."""
        self.write("workspace/notes.txt", "inside content\n")
        task = read_task(self.workspace / "notes.txt")
        self.assertIs(
            OutcomeStatus.SUCCESS, self.scoped.execute(task, self.session).outcome
        )
        recorded = self.scoped._root_identity[self.workspace]
        self.scoped._root_identity[self.workspace] = (
            recorded[0] + 1,
            recorded[1] + 1,
        )
        result = self.scoped.execute(task, self.session)
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertIn("stale authorization", result.summary)

    def test_a_root_with_no_recorded_identity_fails_closed(self) -> None:
        self.write("workspace/notes.txt", "inside content\n")
        task = read_task(self.workspace / "notes.txt")
        self.scoped._root_identity.clear()
        result = self.scoped.execute(task, self.session)
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertNotIn("inside content", str(result.data))

    def test_a_root_that_does_not_exist_yet_reads_nothing(self) -> None:
        absent = LocalFileReadConnector(
            allowed_roots=(self.root / "not-created",),
            default_working_directory=self.root,
        )
        self.assertEqual({}, absent._root_identity)


# --------------------------------------------------------------------------
# R2-2. The excerpt must BE what a real read would have stored
# --------------------------------------------------------------------------


class ExcerptCanonicalityTests(LocalReadTestCase):
    """Bounded and typed is not the same as hygienic.

    A persisted excerpt under the ceiling can still carry raw CRLF, an
    absolute local path or a live credential - none of which survive
    ``bound_local_text``, so none of which any real read could have produced.
    Because that function is a fixed point, canonicality is decidable
    exactly, and a row that fails it is refused rather than cleaned up.
    """

    def valid(self, **overrides) -> LocalReadSnapshot:
        base = dict(
            relative_path="docs/notes.txt",
            byte_size=11,
            sha256="a" * 64,
            excerpt="alpha beta\n",
            truncated=False,
            classification=DataClassification.INTERNAL,
            read_at="2026-01-01T00:00:00+00:00",
        )
        base.update(overrides)
        return LocalReadSnapshot(**base)

    NON_CANONICAL = (
        ("crlf", "alpha\r\nbeta\n"),
        ("bare cr", "alpha\rbeta\n"),
        ("live credential", "api_key=HUNTER2\n"),
        ("aws access key", "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"),
        ("aws secret key", "aws_secret_access_key=wJalrXUtnFEMI\n"),
        ("bare akia value", "AKIAIOSFODNN7EXAMPLE\n"),
        ("openai style key", "sk-" + "a" * 30 + "\n"),
        ("absolute posix path", "see /home/victim/.ssh/id_rsa for details\n"),
        ("absolute windows path", "see C:\\Users\\victim\\secrets.txt\n"),
    )

    def test_bound_local_text_is_a_fixed_point(self) -> None:
        samples = [text for _, text in self.NON_CANONICAL]
        samples += [
            "a" * (LOCAL_READ_EXCERPT_MAX_CHARS + 500),
            "x" * (LOCAL_READ_EXCERPT_MAX_CHARS - 10) + " token=SECRETVALUE",
            "y" * (LOCAL_READ_EXCERPT_MAX_CHARS - 5) + "password=P",
            "",
            "plain text\n",
        ]
        for sample in samples:
            with self.subTest(sample=sample[:32]):
                once, first_truncated = bound_local_text(sample)
                twice, second_truncated = bound_local_text(once)
                self.assertEqual(once, twice)
                self.assertLessEqual(len(once), LOCAL_READ_EXCERPT_MAX_CHARS)
                self.assertFalse(second_truncated and not first_truncated)

    def test_non_canonical_excerpts_are_refused_at_every_boundary(self) -> None:
        claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        for label, excerpt in self.NON_CANONICAL:
            with self.subTest(label=label):
                snapshot = self.valid(excerpt=excerpt)
                self.assertEqual(
                    "excerpt is not the canonical bounded representation",
                    local_read_snapshot_refusal(snapshot),
                )
                with self.assertRaises(TypeError):
                    AgentCore._validated_local_read(snapshot)
                self.assertIsNone(_local_read_payload(snapshot))
                task = Task(
                    objective="analyze",
                    capability="code_analysis",
                    scope=ActionScope(action="analyze", target=str(self.root)),
                )
                task.local_read = snapshot
                block, _ = claude._local_read(task)
                self.assertIsNone(block)

    def test_a_non_canonical_persisted_excerpt_loads_as_nothing(self) -> None:
        for label, excerpt in self.NON_CANONICAL:
            with self.subTest(label=label):
                document = self.valid().to_dict()
                document["excerpt"] = excerpt
                self.assertFalse(LocalReadSnapshot.from_dict(document).present)

    def test_a_real_read_of_adversarial_content_stays_canonical(self) -> None:
        """The refusal must not be reachable from any legitimate file."""
        bodies = {
            "crlf.txt": "alpha\r\nbeta\r\n",
            "creds.env.txt": "api_key=HUNTER2\nAWS_SECRET_ACCESS_KEY=wJalrX\n",
            "paths.txt": "see C:\\Users\\victim\\x and /home/victim/y\n",
            "big.txt": "z" * (LOCAL_READ_EXCERPT_MAX_CHARS + 400),
            "split.txt": "q" * (LOCAL_READ_EXCERPT_MAX_CHARS - 8) + " token=LIVEVALUE",
        }
        for name, body in bodies.items():
            with self.subTest(name=name):
                self.write(name, body)
                stored, _ = self.completed_snapshot(self.root / name)
                self.assertTrue(stored.local_read.present)
                self.assertIsNone(local_read_snapshot_refusal(stored.local_read))
                excerpt = stored.local_read.excerpt
                self.assertEqual(excerpt, bound_local_text(excerpt)[0])
                self.assertNotIn("\r", excerpt)
                self.assertNotIn("HUNTER2", excerpt)
                self.assertNotIn("wJalrX", excerpt)
                self.assertNotIn("LIVEVALUE", excerpt)

    def test_the_round_trip_of_a_real_read_stays_canonical(self) -> None:
        self.write("notes.txt", "api_key=HUNTER2\nplain line\n")
        stored, _ = self.completed_snapshot(self.root / "notes.txt")
        replayed = Task.from_dict(json.loads(json.dumps(stored.to_dict())))
        self.assertTrue(replayed.local_read.present)
        self.assertEqual(stored.local_read, replayed.local_read)
        self.assertIsNone(local_read_snapshot_refusal(replayed.local_read))


# --------------------------------------------------------------------------
# R2-3. Compound credential key names (AWS and friends)
# --------------------------------------------------------------------------


class CompoundCredentialPolicyTests(LocalReadTestCase):
    """``secret`` and ``token`` are anchored with ``\b`` and ``_`` is a word
    character, so ``secret_access_key=`` matched nothing - and ``key`` alone
    is deliberately not a label, so ``access_key_id=`` matched nothing
    either. The two most common cloud credentials in the world sat outside
    the shared policy entirely."""

    COMPOUND = (
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
        "aws_secret_access_key = wJalrXUtnFEMIK7MDENG",
        "access_key_id: AKIAIOSFODNN7EXAMPLE",
        "secret_access_key: abc123def456ghi789",
        "secret-key=abc123def456",
        "api_secret=abc123def456",
        "--aws-access-key AKIAIOSFODNN7EXAMPLE",
        "--secret-access-key wJalrXUtnFEMIK7MDENG",
    )
    BARE_VALUES = ("AKIAIOSFODNN7EXAMPLE", "ASIAIOSFODNN7EXAMPLE")
    SAFE = (
        "apikey.md",
        "tokenizer.py",
        "secret_handling.md",
        "id_rsa.pub",
        "key.py",
        "keys.json",
        "access.log",
        "accesskey.md",
        "my_access_key_docs.md",
        "secret_access_notes.md",
        "Review token telemetry",
        "the access key rotation policy",
    )

    def test_the_shared_policy_recognizes_compound_keys(self) -> None:
        for sample in self.COMPOUND + self.BARE_VALUES:
            with self.subTest(sample=sample):
                self.assertTrue(credential_bearing(sample))
                redacted, count = redact_credentials(sample)
                self.assertGreaterEqual(count, 1)
                self.assertIn(REDACTED_MARKER, redacted)

    def test_the_compound_value_never_survives_redaction(self) -> None:
        for sample in self.COMPOUND:
            with self.subTest(sample=sample):
                value = sample.split("=")[-1].split(":")[-1].split(" ")[-1].strip()
                self.assertNotIn(value, redact_credentials(sample)[0])

    def test_safe_text_and_file_names_are_untouched(self) -> None:
        for sample in self.SAFE:
            with self.subTest(sample=sample):
                self.assertFalse(credential_bearing(sample))
                self.assertEqual((sample, 0), redact_credentials(sample))

    def test_compound_keys_in_file_content_never_egress(self) -> None:
        secret = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"
        key_id = "AKIAIOSFODNN7EXAMPLE"
        self.write(
            "config/aws.txt",
            f"AWS_ACCESS_KEY_ID={key_id}\nAWS_SECRET_ACCESS_KEY={secret}\n",
        )
        stored, run = self.completed_snapshot(self.root / "config" / "aws.txt")
        self.assertNotIn(secret, stored.local_read.excerpt)
        self.assertNotIn(key_id, stored.local_read.excerpt)

        envelope = build_envelope("run", PermissionMode.SESSION, run)
        rendered = json.dumps(envelope, default=str)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(key_id, rendered)
        self.assertNotIn(
            secret,
            Path(self.memory.database_path).read_bytes().decode("latin-1"),
        )

        claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        consumer = Task(
            objective="analyze",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
            ),
        )
        consumer.local_read = stored.local_read
        payload, _ = claude._sanitized_payload(consumer)
        self.assertIn("local_read", payload)
        outbound = claude.canonical_outbound(consumer).decode("utf-8")
        self.assertNotIn(secret, outbound)
        self.assertNotIn(key_id, outbound)
        self.assertNotIn(secret, json.dumps(claude._command(outbound)))
        self.assertNotIn(key_id, json.dumps(claude._command(outbound)))

    def test_a_compound_credential_file_name_is_refused(self) -> None:
        for name in ("aws_secret_access_key=x.txt", "access_key_id=AKIA.txt"):
            with self.subTest(name=name):
                self.assertEqual(
                    "path is credential-bearing", local_read_path_refusal(name)
                )
                self.write(name, "harmless\n")
                _, run = self.run_read(self.root / name)
                self.assertEqual(RunStatus.FAILED, run.status)


# --------------------------------------------------------------------------
# R2-4. A target no filesystem can name fails INSIDE the lifecycle
# --------------------------------------------------------------------------


NUL = chr(0)


class UnusableTargetLifecycleTests(LocalReadTestCase):
    """A NUL in the target used to be the platform's decision.

    POSIX rejects it inside ``Path.resolve``; Windows accepts it there and
    rejects it later in ``lstat``/``open``. So on POSIX the canonical-binding
    step raised straight out of the loop - after ``run()`` had registered the
    task and pointed the session at it - leaving a registered task with no
    terminal state and a session still naming it active. It is refused as an
    invalid task instead, identically on every platform.
    """

    def test_canonical_path_never_raises_for_an_unresolvable_target(self) -> None:
        # Faithful POSIX simulation: the platform refuses to resolve at all.
        with mock.patch.object(
            Path, "resolve", side_effect=ValueError("embedded null byte")
        ):
            value = canonical_path("dir/a" + NUL + "b.txt")
        self.assertIsInstance(value, str)
        self.assertIn(NUL, value)

    def test_the_predicate_names_only_the_impossible(self) -> None:
        self.assertEqual(
            "the target contains a NUL character",
            unusable_filesystem_target("notes" + NUL + ".txt"),
        )
        for safe in ("notes.txt", "docs/a b.txt", "", "C:/x/y.md"):
            self.assertIsNone(unusable_filesystem_target(safe))

    def _assert_clean_terminal(self, target: str) -> None:
        task = read_task(target)
        run = self.core.run(task)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("NUL character", run.error.message)
        stored = self.memory.load_task(task.id)
        self.assertIs(TaskStatus.FAILED, stored.status)
        self.assertFalse(stored.local_read.present)
        # The orphan this fixes: a registered task with no terminal state and
        # a session still pointing at it.
        session = self.memory.load_session(run.session.id)
        self.assertIsNone(session.active_task_id)
        self.assertIsNone(self.memory.latest_execution_attempt(task.id))

    def test_a_nul_target_fails_closed_in_the_loop(self) -> None:
        for target in (
            str(self.root / "notes.txt") + NUL + "extra",
            str(self.root) + "/a" + NUL + "b.txt",
            "notes" + NUL + ".txt",
        ):
            with self.subTest(target=repr(target[-24:])):
                self._assert_clean_terminal(target)

    def test_it_stays_clean_when_the_platform_refuses_to_resolve(self) -> None:
        """The POSIX behaviour, exercised on any platform."""
        with mock.patch.object(
            Path, "resolve", side_effect=ValueError("embedded null byte")
        ):
            self._assert_clean_terminal(str(self.root) + "/a" + NUL + "b.txt")

    def test_the_input_boundary_refuses_a_nul_before_the_loop(self) -> None:
        from jarvis.app import TaskInputError

        document = json.dumps(
            {
                "objective": "read it",
                "capability": LOCAL_READ_CAPABILITY,
                "scope": {"action": "read", "target": "notes" + NUL + ".txt"},
            }
        )
        with self.assertRaises(TaskInputError) as caught:
            decode_task_json(document)
        self.assertIn("NUL", str(caught.exception))

    def test_the_connector_refuses_rather_than_raising_when_driven_directly(
        self,
    ) -> None:
        session = self.core.start_session("direct")
        task = read_task(str(self.root) + "/a" + NUL + "b.txt")
        with mock.patch.object(
            Path, "resolve", side_effect=ValueError("embedded null byte")
        ):
            result = self.connector.execute(task, session)
            with self.assertRaises(AttestationError):
                self.connector.resource_boundary(task)
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
# --------------------------------------------------------------------------
# R2-P2. A file with two names is only inside the roots by one of them
# --------------------------------------------------------------------------


class HardLinkContainmentTests(LocalReadTestCase):
    """A hard link is not a link, which is exactly why nothing caught it.

    ``resolve`` has nothing to dereference, it is not a reparse point, and
    ``GetFinalPathNameByHandleW`` answers with the in-root name the handle was
    opened by - so an in-root name for a file that also lives outside the
    roots passed containment, the secret-file policy, the no-follow chain
    scan and the device/inode binding, and the bytes came from outside
    anyway. Containment is a statement about NAMES; a file with two of them
    is only inside the roots by one.

    The tradeoff is deliberate: M3 refuses multiply-linked regular files so
    strict allowed-root isolation holds without exception, and legitimately
    hardlinked files (git worktrees, dedup and package caches) are an
    accepted compatibility cost for now.
    """

    MARKER = "HARDLINK-OUTSIDE-MARKER-6c02"

    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.outside, True)

    def _hard_link(self, target: Path, link: Path) -> None:
        try:
            os.link(target, link)
        except (OSError, NotImplementedError, AttributeError):  # pragma: no cover
            self.skipTest("this platform cannot create a hard link here")

    def test_an_ordinary_single_link_file_still_reads(self) -> None:
        self.write("docs/notes.txt", "ordinary content\n")
        self.assertEqual(1, os.lstat(self.root / "docs" / "notes.txt").st_nlink)
        stored, _ = self.completed_snapshot(self.root / "docs" / "notes.txt")
        self.assertTrue(stored.local_read.present)
        self.assertEqual("docs/notes.txt", stored.local_read.relative_path)
        self.assertEqual("ordinary content\n", stored.local_read.excerpt)

    def test_an_in_root_hard_link_to_an_outside_file_is_refused(self) -> None:
        victim = self.outside / "secret.txt"
        victim.write_text(f"{self.MARKER}\n", encoding="utf-8")
        link = self.root / "innocent.txt"
        self._hard_link(victim, link)
        self.assertGreater(os.lstat(link).st_nlink, 1)

        task, run = self.run_read(link)
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, run.error.code)
        self.assertIn("more than one filesystem name", run.error.message)
        self.assertFalse(self.memory.load_task(task.id).local_read.present)

    def test_a_hard_link_between_two_in_root_files_is_refused_too(self) -> None:
        """The policy is about the OPENED file, not about where the other
        name happens to be: proving where every name lives is exactly what
        the filesystem will not tell us."""
        self.write("first.txt", "shared content\n")
        self._hard_link(self.root / "first.txt", self.root / "second.txt")
        _, run = self.run_read(self.root / "second.txt")
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIn("more than one filesystem name", run.error.message)

    def test_the_outside_marker_reaches_no_surface_at_all(self) -> None:
        victim = self.outside / "secret.txt"
        victim.write_text(f"{self.MARKER}\n", encoding="utf-8")
        link = self.root / "innocent.txt"
        self._hard_link(victim, link)

        task = read_task(link)
        session = self.core.start_session("hardlink")

        # [1] The connector result itself.
        result = self.connector.execute(task, session)
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertNotIn(
            self.MARKER,
            json.dumps(
                {
                    "summary": result.summary,
                    "data": result.data,
                    "error": None if result.error is None else result.error.to_dict(),
                },
                default=str,
            ),
        )

        # [2] The whole run, through the loop.
        task, run = self.run_read(link)
        stored = self.memory.load_task(task.id)
        self.assertFalse(stored.local_read.present)

        # [3] Task snapshot and every persisted diagnostic.
        self.assertNotIn(self.MARKER, json.dumps(stored.to_dict(), default=str))
        self.assertNotIn(self.MARKER, json.dumps(run.error.to_dict(), default=str))
        session_id = self.memory.task_session_id(task.id)
        self.assertNotIn(
            self.MARKER, json.dumps(self.memory.list_events(session_id), default=str)
        )
        self.assertNotIn(
            self.MARKER,
            json.dumps(
                [d.to_dict() for d in self.memory.list_decisions(session_id)],
                default=str,
            ),
        )

        # [4] The database file, byte for byte.
        self.assertNotIn(
            self.MARKER,
            Path(self.memory.database_path).read_bytes().decode("latin-1"),
        )

        # [5] The public envelope.
        envelope = build_envelope("run", PermissionMode.SESSION, run)
        self.assertIsNone(envelope["local_read"])
        self.assertNotIn(self.MARKER, json.dumps(envelope, default=str))

        # [6] The Claude payload, canonical outbound bytes and argv.
        claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        consumer = Task(
            objective="analyze",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
            ),
        )
        consumer.local_read = stored.local_read
        payload, _ = claude._sanitized_payload(consumer)
        self.assertNotIn("local_read", payload)
        self.assertNotIn(self.MARKER, json.dumps(payload, default=str))
        outbound = claude.canonical_outbound(consumer).decode("utf-8")
        self.assertNotIn(self.MARKER, outbound)
        self.assertNotIn(self.MARKER, json.dumps(claude._command(outbound)))

    def test_the_refusal_precedes_any_byte_being_consumed(self) -> None:
        """No outside content is held in memory even transiently.

        The check sits at the ``fstat`` of the opened handle, before the
        single ``read`` call this connector makes, so the refusal cannot be
        satisfied by discarding bytes that were already loaded.
        """
        victim = self.outside / "secret.txt"
        victim.write_text(f"{self.MARKER}\n", encoding="utf-8")
        link = self.root / "innocent.txt"
        self._hard_link(victim, link)

        reads: list[int] = []
        real_fdopen = local_files.os.fdopen

        class CountingHandle:
            """Passes everything through, but records every ``read``."""

            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *exception):
                return self._inner.__exit__(*exception)

            def fileno(self):
                return self._inner.fileno()

            def read(self, *args, **kwargs):
                reads.append(1)
                return self._inner.read(*args, **kwargs)

        def counting_fdopen(descriptor, *args, **kwargs):
            return CountingHandle(real_fdopen(descriptor, *args, **kwargs))

        with mock.patch.object(local_files.os, "fdopen", counting_fdopen):
            result = self.connector.execute(
                read_task(link), self.core.start_session("hardlink read")
            )
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual([], reads, "bytes were read before the refusal")

        # And the same seam proves an ordinary file DOES reach the read, so
        # the assertion above is about the refusal, not about a broken seam.
        self.write("ordinary.txt", "content\n")
        with mock.patch.object(local_files.os, "fdopen", counting_fdopen):
            allowed = self.connector.execute(
                read_task(self.root / "ordinary.txt"),
                self.core.start_session("ordinary read"),
            )
        self.assertIs(OutcomeStatus.SUCCESS, allowed.outcome)
        self.assertEqual([1], reads)

    def test_the_policy_is_scoped_to_the_opened_regular_file(self) -> None:
        """Directory handling is untouched: a directory never reaches the
        link check, because it is refused as a non-regular file first."""
        (self.root / "adir").mkdir()
        _, run = self.run_read(self.root / "adir")
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertIn("not an existing regular file", run.error.message)
        self.assertNotIn("filesystem name", run.error.message)
# --------------------------------------------------------------------------
# R3-1. A classification that cannot be read invalidates the WHOLE snapshot
# --------------------------------------------------------------------------


class MalformedClassificationTests(LocalReadTestCase):
    """A SECRET fallback looked fail-closed and was not.

    It turned a row whose classification could not be read into a
    structurally VALID, PRESENT snapshot: one that satisfies the whole
    contract, renders a block in the envelope, and answers ``present`` to
    every gate that asks that before it asks anything else. Malformed
    persisted state must not survive as a usable object wearing a safe
    label - the snapshot is discarded instead.
    """

    MARKER = "MALFORMED-CLASS-MARKER-8f31"

    MALFORMED = (
        ("unknown string", "totally-made-up"),
        ("empty string", ""),
        ("wrong case", "PUBLIC"),
        ("none", None),
        ("integer", 7),
        ("float", 1.5),
        ("boolean", True),
        ("list", ["public"]),
        ("mapping", {"value": "public"}),
        ("nested list", [["internal"]]),
        ("bytes", b"internal"),
    )

    #: The subset a real persisted row can actually carry: task state is
    #: stored as JSON, so ``bytes`` can only ever reach ``from_dict`` from
    #: in-process code, never from the database.
    MALFORMED_PERSISTED = tuple(
        (label, value)
        for label, value in MALFORMED
        if not isinstance(value, bytes)
    )

    def document(self, **overrides) -> dict:
        base = {
            "relative_path": "notes.txt",
            "byte_size": 11,
            "sha256": "a" * 64,
            "excerpt": f"{self.MARKER}\n",
            "truncated": False,
            "classification": DataClassification.INTERNAL.value,
            "read_at": "2026-01-01T00:00:00+00:00",
        }
        base.update(overrides)
        return base

    def test_every_malformed_classification_loads_as_nothing(self) -> None:
        for label, value in self.MALFORMED:
            with self.subTest(label=label):
                snapshot = LocalReadSnapshot.from_dict(
                    self.document(classification=value)
                )
                self.assertFalse(snapshot.present)
                self.assertEqual("", snapshot.relative_path)
                self.assertEqual("", snapshot.excerpt)
                self.assertNotIn(self.MARKER, json.dumps(snapshot.to_dict()))

    def test_every_valid_classification_is_preserved(self) -> None:
        for classification in DataClassification:
            with self.subTest(classification=classification):
                snapshot = LocalReadSnapshot.from_dict(
                    self.document(classification=classification.value)
                )
                self.assertTrue(snapshot.present)
                self.assertIs(classification, snapshot.classification)
        # SECRET in particular is a legitimate persisted value, not a
        # marker of failure, and must survive a load unchanged.
        secret = LocalReadSnapshot.from_dict(
            self.document(classification=DataClassification.SECRET.value)
        )
        self.assertTrue(secret.present)
        self.assertIs(DataClassification.SECRET, secret.classification)

    def test_load_resave_reload_remains_empty(self) -> None:
        for label, value in self.MALFORMED:
            with self.subTest(label=label):
                first = LocalReadSnapshot.from_dict(
                    self.document(classification=value)
                )
                self.assertFalse(first.present)
                replayed = LocalReadSnapshot.from_dict(
                    json.loads(json.dumps(first.to_dict()))
                )
                self.assertFalse(replayed.present)
                self.assertNotIn(self.MARKER, json.dumps(replayed.to_dict()))

    def _tamper(self, task_id: str, value) -> None:
        connection = sqlite3.connect(self.memory.database_path)
        try:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            document = json.loads(row[0])
            document["local_read"]["classification"] = value
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task_id),
            )
            connection.commit()
        finally:
            connection.close()

    def _tampered_source(self, value):
        self.write("notes.txt", f"{self.MARKER}\n")
        stored, run = self.completed_snapshot(self.root / "notes.txt")
        self._tamper(stored.id, value)
        return stored, run

    def test_a_tampered_row_produces_no_envelope_block(self) -> None:
        for label, value in self.MALFORMED_PERSISTED:
            with self.subTest(label=label):
                stored, run = self._tampered_source(value)
                reloaded = self.memory.load_task(stored.id)
                self.assertFalse(reloaded.local_read.present)
                envelope = build_envelope(
                    "run",
                    PermissionMode.SESSION,
                    replace_run_task(run, reloaded),
                )
                self.assertIsNone(envelope["local_read"])
                self.assertNotIn(
                    self.MARKER, json.dumps(envelope, default=str)
                )

    def test_a_cross_task_reference_fails_before_any_connector_or_attempt(
        self,
    ) -> None:
        for label, value in self.MALFORMED_PERSISTED:
            with self.subTest(label=label):
                stored, run = self._tampered_source(value)
                consumer = Task(
                    objective="analyze using the earlier read",
                    capability="code_analysis",
                    scope=ActionScope(
                        action="analyze",
                        target=str(self.root),
                        data_classification=DataClassification.INTERNAL,
                    ),
                )
                consumer.context[LOCAL_READ_TASK_REFERENCE_KEY] = stored.id
                hop = self.core.run(consumer, session_id=run.session.id)
                self.assertEqual(RunStatus.FAILED, hop.status)
                self.assertEqual(ErrorCode.INVALID_TASK, hop.error.code)
                carried = self.memory.load_task(consumer.id)
                self.assertFalse(carried.local_read.present)
                self.assertIsNone(carried.selected_connector)
                self.assertIsNone(
                    self.memory.latest_execution_attempt(consumer.id)
                )
                self.assertNotIn(
                    self.MARKER, json.dumps(carried.to_dict(), default=str)
                )

    def test_the_marker_never_reaches_the_canonical_outbound(self) -> None:
        claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        for label, value in self.MALFORMED_PERSISTED:
            with self.subTest(label=label):
                stored, _ = self._tampered_source(value)
                reloaded = self.memory.load_task(stored.id)
                consumer = Task(
                    objective="analyze",
                    capability="code_analysis",
                    scope=ActionScope(
                        action="analyze",
                        target=str(self.root),
                        destination="anthropic/claude-code",
                    ),
                )
                consumer.local_read = reloaded.local_read
                payload, _ = claude._sanitized_payload(consumer)
                self.assertNotIn("local_read", payload)
                outbound = claude.canonical_outbound(consumer).decode("utf-8")
                self.assertNotIn(self.MARKER, outbound)
                self.assertNotIn(
                    self.MARKER, json.dumps(claude._command(outbound))
                )


# --------------------------------------------------------------------------
# R3-2. Windows NTFS alternate data streams
# --------------------------------------------------------------------------


class AlternateDataStreamTests(LocalReadTestCase):
    """A named stream is a second data channel inside one directory entry.

    It defeats every rule the deny policy has: the policy matches an exact
    name and an exact suffix, and ``.env:s`` has neither the name ``.env``
    nor the suffix ``.pem`` - while ``secret.pem:s:$DATA`` names a stream of
    an explicitly denied file and reads its bytes. Streams are simply outside
    M3's single-file contract, so they are refused rather than parsed.
    """

    MARKER = "ADS-STREAM-MARKER-3b7e"

    STREAM_NAMES = (".env:named", ".env:named:$DATA", "id_rsa:named",
                    "secret.pem:named:$DATA", "notes.txt:hidden")

    def _write_stream(self, name: str) -> str:
        base, _, _ = name.partition(":")
        host = self.root / base
        host.parent.mkdir(parents=True, exist_ok=True)
        host.write_text("host file content\n", encoding="utf-8")
        target = str(self.root / name)
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(f"{self.MARKER}\n")
        except OSError:  # pragma: no cover - non-NTFS platform
            self.skipTest("this filesystem has no alternate data streams")
        return target

    def test_the_policy_refuses_every_stream_spelling(self) -> None:
        for name in self.STREAM_NAMES:
            with self.subTest(name=name):
                self.assertEqual(
                    "windows alternate data stream",
                    local_read_denial_reason(Path(self.root / name)),
                )
                self.assertEqual(
                    "path carries an alternate-data-stream separator",
                    local_read_path_refusal(name),
                )

    def test_a_stream_is_refused_before_approval_authority_exists(self) -> None:
        for name in self.STREAM_NAMES:
            with self.subTest(name=name):
                target = self._write_stream(name)
                with self.assertRaises(AttestationError) as caught:
                    self.connector.resource_boundary(read_task(target))
                self.assertIs(ErrorCode.EGRESS_REFUSED, caught.exception.code)
                self.assertNotIn(self.MARKER, caught.exception.args[0])

    def test_a_stream_never_reads_a_byte(self) -> None:
        target = self._write_stream(".env:named")
        session = self.core.start_session("ads")

        reads: list[int] = []
        real_fdopen = local_files.os.fdopen

        def counting_fdopen(descriptor, *args, **kwargs):
            reads.append(1)
            return real_fdopen(descriptor, *args, **kwargs)

        with mock.patch.object(local_files.os, "fdopen", counting_fdopen):
            result = self.connector.execute(read_task(target), session)
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual([], reads, "the stream was opened for reading")

    def test_the_marker_reaches_no_surface_at_all(self) -> None:
        target = self._write_stream("secret.pem:named:$DATA")
        task, run = self.run_read(target)
        self.assertEqual(RunStatus.FAILED, run.status)
        stored = self.memory.load_task(task.id)
        self.assertFalse(stored.local_read.present)

        self.assertNotIn(self.MARKER, json.dumps(stored.to_dict(), default=str))
        self.assertNotIn(self.MARKER, json.dumps(run.error.to_dict(), default=str))
        session_id = self.memory.task_session_id(task.id)
        self.assertNotIn(
            self.MARKER, json.dumps(self.memory.list_events(session_id), default=str)
        )
        self.assertNotIn(
            self.MARKER,
            json.dumps(
                [d.to_dict() for d in self.memory.list_decisions(session_id)],
                default=str,
            ),
        )
        self.assertNotIn(
            self.MARKER,
            Path(self.memory.database_path).read_bytes().decode("latin-1"),
        )

        envelope = build_envelope("run", PermissionMode.SESSION, run)
        self.assertIsNone(envelope["local_read"])
        self.assertNotIn(self.MARKER, json.dumps(envelope, default=str))

        claude = claude_code.ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        consumer = Task(
            objective="analyze",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
            ),
        )
        consumer.local_read = stored.local_read
        payload, _ = claude._sanitized_payload(consumer)
        self.assertNotIn("local_read", payload)
        outbound = claude.canonical_outbound(consumer).decode("utf-8")
        self.assertNotIn(self.MARKER, outbound)
        self.assertNotIn(self.MARKER, json.dumps(claude._command(outbound)))

    def test_ordinary_drive_letter_paths_are_untouched(self) -> None:
        """Normal Windows absolute handling must not be weakened."""
        self.write("docs/notes.txt", "drive letter content\n")
        absolute = self.root / "docs" / "notes.txt"
        self.assertRegex(str(absolute), r"^[A-Za-z]:" if os.name == "nt" else r"^/")
        self.assertIsNone(local_read_denial_reason(absolute))
        stored, _ = self.completed_snapshot(absolute)
        self.assertEqual("docs/notes.txt", stored.local_read.relative_path)
        self.assertEqual("drive letter content\n", stored.local_read.excerpt)
        # The canonical form still carries the drive, and the safe display
        # form still does not.
        self.assertIsNone(local_read_path_refusal("docs/notes.txt"))

    def test_ordinary_names_without_stream_syntax_are_unaffected(self) -> None:
        for name in (
            "notes.txt",
            "docs/read me.md",
            "a.b.c.txt",
            "tokenizer.py",
            "id_rsa.pub",
            "dotted.name.with.many.parts",
        ):
            with self.subTest(name=name):
                self.write(name, "ordinary\n")
                self.assertIsNone(local_read_path_refusal(name))
                stored, _ = self.completed_snapshot(self.root / name)
                self.assertEqual(name, stored.local_read.relative_path)
# --------------------------------------------------------------------------
# R3-3. A local read records EVIDENCE, not a judgement
# --------------------------------------------------------------------------


class NoDecisionForLocalReadTests(LocalReadTestCase):
    """A DecisionRecord is trusted inherited history.

    Later tasks in the session retrieve decisions as context, so a decision
    is a judgement future work is entitled to act on. A local read observed a
    file; it decided nothing. Writing a decision for it would put file-derived
    text into the one channel that IS authority-adjacent, and would do it for
    every read. The row is therefore never written - not blanked, not a
    sentinel, not deleted afterwards - so there is nothing to inherit and
    nothing to clean up.
    """

    MARKER = "DECISION-LEAK-MARKER-4f8d"

    def test_a_completed_local_read_creates_zero_decisions(self) -> None:
        self.write("notes.txt", f"{self.MARKER}\n")
        stored, run = self.completed_snapshot(self.root / "notes.txt")
        self.assertIs(TaskStatus.COMPLETED, stored.status)
        self.assertTrue(stored.local_read.present)

        session_id = self.memory.task_session_id(stored.id)
        self.assertEqual([], self.memory.list_decisions(session_id))

        connection = sqlite3.connect(self.memory.database_path)
        try:
            rows = connection.execute(
                "SELECT COUNT(*) FROM decision_log WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, rows)

    def test_the_rest_of_finalization_is_untouched(self) -> None:
        """Terminal task, result, session cleanup, usage and event all stay."""
        self.write("notes.txt", "finalization\n")
        stored, run = self.completed_snapshot(self.root / "notes.txt")
        session_id = self.memory.task_session_id(stored.id)

        # Terminal task + result persistence.
        self.assertIs(TaskStatus.COMPLETED, stored.status)
        self.assertTrue(stored.result_snapshot)
        self.assertEqual("finalization\n", stored.local_read.excerpt)

        # Session active-task cleanup.
        session = self.memory.load_session(session_id)
        self.assertIsNone(session.active_task_id)
        self.assertEqual(stored.id, session.latest_task_id)

        # Terminal event.
        events = self.memory.list_events(session_id)
        self.assertIn(
            "task_completed", [event.get("event_type") for event in events]
        )

        # Usage bookkeeping, written in the same transaction.
        connection = sqlite3.connect(self.memory.database_path)
        try:
            usage_rows = connection.execute(
                "SELECT COUNT(*) FROM usage_ledger WHERE task_id = ?", (stored.id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreaterEqual(usage_rows, 1)

    def test_a_later_task_inherits_nothing_from_the_read(self) -> None:
        """Zero retrieval items and zero retrieved characters."""
        self.write("notes.txt", f"{self.MARKER}\n")
        stored, run = self.completed_snapshot(self.root / "notes.txt")
        session_id = self.memory.task_session_id(stored.id)

        follow_up = Task(
            objective="analyze the notes file we just read",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                data_classification=DataClassification.INTERNAL,
            ),
        )
        self.core.run(follow_up, session_id=session_id)
        carried = self.memory.load_task(follow_up.id)
        self.assertEqual((), carried.retrieved_context.items)
        self.assertEqual(0, carried.retrieved_context.character_count)
        self.assertNotIn(self.MARKER, json.dumps(carried.to_dict(), default=str))

    def test_a_normal_task_still_writes_its_decision(self) -> None:
        """The suppression is scoped to the capability, not to finalization."""
        from tests.helpers import DummyConnector

        app = create_app(
            settings=Settings(
                memory_database=self.root / "normal.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(DummyConnector(name="dummy"),),
        )
        task = Task(
            objective="do ordinary work",
            capability="echo",
            scope=ActionScope(action="analyze", target=str(self.root)),
        )
        run = app.core.run(task)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))

        decisions = app.memory.list_decisions(run.session.id)
        self.assertEqual(1, len(decisions))
        self.assertEqual(task.id, decisions[0].task_id)
        self.assertTrue(decisions[0].summary)

    def test_both_capabilities_in_one_session_behave_independently(self) -> None:
        """Exactly one decision, from the task that actually decided."""
        from tests.helpers import DummyConnector

        app = create_app(
            settings=Settings(
                memory_database=self.root / "mixed.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(
                self.connector,
                DummyConnector(name="dummy"),
            ),
        )
        self.write("notes.txt", f"{self.MARKER}\n")
        read = read_task(self.root / "notes.txt")
        read_run = app.core.run(read)
        self.assertEqual(RunStatus.COMPLETED, read_run.status, msg=str(read_run.error))

        analyze = Task(
            objective="analyze",
            capability="echo",
            scope=ActionScope(action="analyze", target=str(self.root)),
        )
        app.core.run(analyze, session_id=read_run.session.id)

        decisions = app.memory.list_decisions(read_run.session.id)
        self.assertEqual(1, len(decisions))
        self.assertEqual(analyze.id, decisions[0].task_id)
        self.assertNotIn(
            self.MARKER, json.dumps([d.to_dict() for d in decisions], default=str)
        )

    def test_finalization_stays_atomic_when_a_terminal_writer_already_won(
        self,
    ) -> None:
        """A lost arbitration must still write NOTHING - decision or not.

        The decision row was one of the things the guarded transaction rolled
        back. Making it optional must not turn the rest of the transaction
        into a set of unguarded writes.
        """
        self.write("notes.txt", "atomic\n")
        task = read_task(self.root / "notes.txt")
        session = self.core.start_session("atomic")

        seen: list[object] = []

        def losing_finalize(session_state, finalize_task_arg, decision, *args):
            # Simulate a cancellation winning the terminal transition in the
            # window before this writer's guarded write.
            seen.append(decision)
            return False

        with mock.patch.object(
            self.memory, "finalize_task", side_effect=losing_finalize
        ):
            run = self.core.run(task, session_id=session.id)

        # The local read reached finalization with NO decision...
        self.assertEqual([None], seen)
        # ...and a lost arbitration wrote nothing at all.
        self.assertNotEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual([], self.memory.list_decisions(session.id))

    def test_a_failed_local_read_also_writes_no_decision(self) -> None:
        """Failure semantics are unchanged: they never wrote one either."""
        _, run = self.run_read(self.root / "missing.txt")
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual([], self.memory.list_decisions(run.session.id))

    def test_a_cancelled_local_read_writes_no_decision(self) -> None:
        self.write("notes.txt", "cancel\n")
        task = read_task(self.root / "notes.txt")
        session = self.core.start_session("cancelled")
        self.memory.save_session(session)
        self.memory.save_task(session.id, task)
        self.core.cancel(session.id, task.id)
        self.assertEqual([], self.memory.list_decisions(session.id))
