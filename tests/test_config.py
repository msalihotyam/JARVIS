"""M12 S4: persistent operator configuration, and the global kill-switch.

Two things are on trial here and they are related.

The config store holds PREFERENCES - no secret, no credential, no monthly
cap, no lifecycle fact - and it holds them as one complete document that is
either accepted whole or refused whole. A half-understood document is the
failure this suite is mostly about: a typo'd key silently ignored is a
setting the operator believes they made, and a corrupted file that quietly
relaxes STRICT is a security decision nobody took.

The kill-switch is what the store can turn off. It is a barrier, not a gate:
nothing answers it, and it is refused ahead of every authority that could
start work - so a disabled run costs nothing, changes nothing, and never
relabels work whose outcome is already recorded.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis import cli
from jarvis import settings as settings_module
from jarvis.contracts import (
    CHAIN_NO_PREDECESSOR,
    AttemptState,
    ChainState,
    ChainStatus,
    ErrorCode,
    ExecutionAttempt,
    LoopStage,
    PermissionMode,
    RunStatus,
    SessionState,
    SideEffect,
    Task,
    TaskStatus,
    chain_steps_from_declaration,
    credential_bearing,
)
from jarvis.destinations import (
    CLAUDE_CODE_DESTINATION,
    DestinationGrant,
)
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import (
    CONFIG_KEYS,
    CONFIG_MAX_BYTES,
    CONFIG_VERSION,
    LEGACY_CONFIG_KEYS,
    MONTHLY_HARD_LIMIT_USD,
    ConfigError,
    ConfigStatus,
    ConfigStore,
    Settings,
    load_settings,
)
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, make_task, open_app, open_core, safe_cleanup

EXIT_OK = 0
EXIT_INVALID_INPUT = 64
EXIT_INTERNAL = 70
EXIT_POLICY = 77


class ConfigTestCase(unittest.TestCase):
    """Every test gets its own store; the real one is never read or written."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        self.store = ConfigStore(self.path)
        patcher = mock.patch.object(
            settings_module, "default_config_path", lambda: self.path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- fixtures ------------------------------------------------------

    def canonical(self, **overrides: object) -> dict:
        document = {
            "config_version": CONFIG_VERSION,
            "execution_enabled": True,
            "permission_mode": "session",
            "paid_api_enabled": False,
            "paid_api_approval_id": None,
            "claude_call_budget_usd": 0.25,
            "approval_ttl_minutes": 30,
            "memory_database": str(self.root / "jarvis.db"),
            "claude_model": None,
            "claude_effort": None,
        }
        document.update(overrides)
        return document

    def write(self, document: dict | str | bytes) -> None:
        if isinstance(document, dict):
            self.path.write_text(json.dumps(document), encoding="utf-8")
        elif isinstance(document, bytes):
            self.path.write_bytes(document)
        else:
            self.path.write_text(document, encoding="utf-8")

    def load(self):
        return self.store.load()

    def run_cli(self, argv: list[str], app_factory=None):
        def explode(settings):
            raise AssertionError("an application was built")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(
            io.StringIO()
        ):
            code = cli.main(argv, app_factory=app_factory or explode)
        return code, buffer.getvalue()

    def payload(self, argv: list[str]) -> tuple[int, dict]:
        code, output = self.run_cli(argv)
        return code, json.loads(output)


class ParsingTests(ConfigTestCase):
    """The document is accepted whole, or not at all."""

    def test_an_absent_store_yields_defaults_and_creates_nothing(self) -> None:
        result = self.load()
        self.assertIs(result.status, ConfigStatus.ABSENT)
        self.assertTrue(result.usable)
        self.assertIs(result.settings.execution_enabled, True)
        self.assertIs(result.settings.permission_mode, PermissionMode.SESSION)
        self.assertFalse(self.path.exists(), "loading created a file")
        self.assertFalse(self.path.parent.joinpath("config.json.lock").exists())

    def test_a_valid_document_survives_a_fresh_reload(self) -> None:
        self.write(
            self.canonical(
                execution_enabled=False,
                permission_mode="strict",
                approval_ttl_minutes=90,
                claude_call_budget_usd=1.5,
            )
        )
        result = ConfigStore(self.path).load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertIs(result.settings.execution_enabled, False)
        self.assertIs(result.settings.permission_mode, PermissionMode.STRICT)
        self.assertEqual(result.settings.approval_ttl_minutes, 90)
        self.assertEqual(result.settings.claude_call_budget_usd, 1.5)

    def test_the_key_set_is_exactly_closed(self) -> None:
        self.assertEqual(
            set(CONFIG_KEYS),
            {
                "config_version", "execution_enabled", "permission_mode",
                "paid_api_enabled", "paid_api_approval_id",
                "claude_call_budget_usd", "approval_ttl_minutes",
                "memory_database", "claude_model", "claude_effort",
            },
        )

    def test_a_missing_pre_existing_key_invalidates_the_document(self) -> None:
        # Only the PRE-EXISTING keys: dropping exactly the two selection keys
        # is the accepted legacy v1 shape, covered by its own tests below.
        for key in sorted(LEGACY_CONFIG_KEYS):
            with self.subTest(missing=key):
                document = self.canonical()
                del document[key]
                self.write(document)
                self.assertIs(self.load().status, ConfigStatus.INVALID, key)

    def test_an_unknown_key_invalidates_the_document(self) -> None:
        """A typo silently ignored is a setting nobody actually made."""
        for unknown in ("permision_mode", "monthly_hard_limit_usd", "extra"):
            with self.subTest(key=unknown):
                self.write(self.canonical(**{unknown: True}))
                self.assertIs(self.load().status, ConfigStatus.INVALID, unknown)

    def test_a_duplicate_key_invalidates_the_document(self) -> None:
        """json keeps the last one silently; the operator could not know."""
        body = json.dumps(self.canonical())[:-1]
        self.write(body + ', "execution_enabled": true}')
        self.assertIs(self.load().status, ConfigStatus.INVALID)

    def test_every_wrong_version_invalidates_the_document(self) -> None:
        for version in (0, 2, -1, "1", 1.0, True, None):
            with self.subTest(version=version):
                self.write(self.canonical(config_version=version))
                self.assertIs(self.load().status, ConfigStatus.INVALID, repr(version))

    def test_a_non_object_root_invalidates_the_document(self) -> None:
        for raw in ("[]", "null", '"config"', "7", "true"):
            with self.subTest(root=raw):
                self.write(raw)
                self.assertIs(self.load().status, ConfigStatus.INVALID, raw)

    def test_an_oversized_document_is_refused_before_parsing(self) -> None:
        document = self.canonical()
        blob = json.dumps(document).encode("utf-8")
        self.write(blob + b" " * (CONFIG_MAX_BYTES + 1))
        self.assertIs(self.load().status, ConfigStatus.INVALID)

    def test_malformed_json_invalidates_the_document(self) -> None:
        for raw in ("{not json", "{", '{"a": }', ""):
            with self.subTest(raw=raw):
                self.write(raw)
                self.assertIs(self.load().status, ConfigStatus.INVALID, raw)

    def test_invalid_utf8_invalidates_the_document(self) -> None:
        self.write(b"\xff\xfe\x00bad")
        self.assertIs(self.load().status, ConfigStatus.INVALID)

    def test_an_invalid_permission_mode_is_refused(self) -> None:
        for mode in ("STRICT", "auto", "", None, 1, True):
            with self.subTest(mode=repr(mode)):
                self.write(self.canonical(permission_mode=mode))
                self.assertIs(self.load().status, ConfigStatus.INVALID, repr(mode))

    def test_non_exact_booleans_are_refused(self) -> None:
        for key in ("execution_enabled", "paid_api_enabled"):
            for value in (1, 0, "true", "false", None, []):
                with self.subTest(key=key, value=repr(value)):
                    self.write(self.canonical(**{key: value}))
                    self.assertIs(self.load().status, ConfigStatus.INVALID)

    def test_non_finite_numbers_are_refused(self) -> None:
        for raw in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(number=raw):
                body = json.dumps(self.canonical()).replace("0.25", raw)
                self.write(body)
                self.assertIs(self.load().status, ConfigStatus.INVALID, raw)

    def test_call_budget_bounds(self) -> None:
        for value in (0, -1, 0.0, 20.5, 1000, True, "1", None):
            with self.subTest(value=repr(value)):
                self.write(self.canonical(claude_call_budget_usd=value))
                self.assertIs(self.load().status, ConfigStatus.INVALID, repr(value))
        for value in (0.01, 1, 20):
            with self.subTest(ok=value):
                self.write(self.canonical(claude_call_budget_usd=value))
                self.assertIs(self.load().status, ConfigStatus.VALID, repr(value))

    def test_approval_ttl_bounds(self) -> None:
        for value in (0, -1, 1441, 1.5, True, "30", None):
            with self.subTest(value=repr(value)):
                self.write(self.canonical(approval_ttl_minutes=value))
                self.assertIs(self.load().status, ConfigStatus.INVALID, repr(value))
        for value in (1, 30, 1440):
            with self.subTest(ok=value):
                self.write(self.canonical(approval_ttl_minutes=value))
                self.assertIs(self.load().status, ConfigStatus.VALID, repr(value))

    def test_an_invalid_approval_id_is_refused_and_never_echoed(self) -> None:
        bad = {
            "blank": "   ",
            "too long": "a" * 129,
            "spaces": "approval id",
            "path": "../../etc/passwd",
            "credential": "sk-abcdefghijklmnop",
            "bearer": "Bearer abcdefghijklmnop",
            "wrong type": 7,
        }
        for label, value in bad.items():
            with self.subTest(id=label):
                self.write(self.canonical(paid_api_approval_id=value))
                result = self.load()
                self.assertIs(result.status, ConfigStatus.INVALID, label)
                assert result.reason is not None
                self.assertNotIn(str(value), result.reason, "the value leaked")

    def test_paid_api_enabled_requires_an_approval_id(self) -> None:
        self.write(self.canonical(paid_api_enabled=True, paid_api_approval_id=None))
        self.assertIs(self.load().status, ConfigStatus.INVALID)
        self.write(self.canonical(paid_api_enabled=True, paid_api_approval_id="ok-1"))
        result = self.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertIs(result.settings.paid_api_enabled, True)

    def test_an_id_may_persist_while_the_paid_route_is_off(self) -> None:
        self.write(self.canonical(paid_api_enabled=False, paid_api_approval_id="keep-1"))
        result = self.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertEqual(result.settings.paid_api_approval_id, "keep-1")
        self.assertIs(result.settings.paid_api_enabled, False)


class FailClosedTests(ConfigTestCase):
    """A store that cannot be trusted must not relax anything."""

    def broken_documents(self) -> dict[str, object]:
        return {
            "malformed json": "{not json",
            "unknown key": self.canonical(extra=1),
            "missing key": {k: v for k, v in self.canonical().items() if k != "permission_mode"},
            "bad version": self.canonical(config_version=2),
            "non-object": "[]",
        }

    def test_a_refused_document_disables_execution(self) -> None:
        for label, document in self.broken_documents().items():
            with self.subTest(document=label):
                self.write(document)
                result = self.load()
                self.assertIs(result.status, ConfigStatus.INVALID, label)
                self.assertFalse(result.usable, label)
                self.assertIs(result.settings.execution_enabled, False, label)

    def test_a_refused_document_forces_strict_and_paid_off(self) -> None:
        """A corrupted file must never be able to relax a policy choice."""
        for label, document in self.broken_documents().items():
            with self.subTest(document=label):
                self.write(document)
                settings = self.load().settings
                self.assertIs(settings.permission_mode, PermissionMode.STRICT, label)
                self.assertIs(settings.paid_api_enabled, False, label)
                self.assertIsNone(settings.paid_api_approval_id, label)

    def test_no_field_survives_a_refused_document(self) -> None:
        """Not even the valid-looking ones: partial policy is not policy."""
        self.write(
            self.canonical(
                approval_ttl_minutes=1337,
                claude_call_budget_usd=19.0,
                memory_database=str(self.root / "chosen.db"),
                unknown_key=True,
            )
        )
        settings = self.load().settings
        defaults = Settings()
        self.assertEqual(settings.approval_ttl_minutes, defaults.approval_ttl_minutes)
        self.assertEqual(
            settings.claude_call_budget_usd, defaults.claude_call_budget_usd
        )
        self.assertEqual(settings.memory_database, defaults.memory_database)

    def test_an_unreadable_store_takes_the_same_posture(self) -> None:
        self.write(self.canonical())

        def deny(self_path, *args, **kwargs):
            raise PermissionError("denied")

        with mock.patch.object(Path, "read_bytes", deny):
            result = self.load()
        self.assertIs(result.status, ConfigStatus.UNREADABLE)
        self.assertFalse(result.usable)
        self.assertIs(result.settings.execution_enabled, False)
        self.assertIs(result.settings.permission_mode, PermissionMode.STRICT)
        assert result.reason is not None
        self.assertNotIn("denied", result.reason, "a raw OS error leaked")

    def test_settings_carries_no_diagnostic_state(self) -> None:
        self.assertFalse(hasattr(Settings(), "status"))
        self.assertFalse(hasattr(Settings(), "config_status"))
        self.assertFalse(hasattr(Settings(), "reason"))


class RepairTests(ConfigTestCase):
    """Broken stores are inspectable and repairable, never silently edited."""

    def test_show_reports_a_refused_store_without_its_content(self) -> None:
        self.write('{"secret": "sk-abcdefghijklmnop", not json')
        code, payload = self.payload(["config", "show"])
        self.assertEqual(code, EXIT_POLICY)
        self.assertEqual(payload["config_status"], "invalid")
        self.assertIs(payload["execution_enabled"], False)
        self.assertEqual(payload["permission_mode"], "strict")
        rendered = json.dumps(payload)
        self.assertNotIn("sk-abcdefghijklmnop", rendered)
        self.assertNotIn("not json", rendered)

    def test_set_refuses_a_refused_store_and_preserves_the_bytes(self) -> None:
        self.write("{not json")
        before = self.path.read_bytes()
        code, payload = self.payload(["config", "set", "execution_enabled", "true"])
        self.assertNotEqual(code, EXIT_OK)
        self.assertEqual(payload["error"]["code"], "config_invalid")
        self.assertEqual(before, self.path.read_bytes(), "the file was rewritten")

    def test_reset_key_refuses_a_refused_store_and_preserves_the_bytes(
        self,
    ) -> None:
        self.write(self.canonical(unknown_key=1))
        before = self.path.read_bytes()
        code, _payload = self.payload(["config", "reset", "permission_mode"])
        self.assertNotEqual(code, EXIT_OK)
        self.assertEqual(before, self.path.read_bytes())

    def test_reset_all_repairs_any_document(self) -> None:
        for label, document in {
            "malformed": "{not json",
            "unknown key": self.canonical(extra=1),
            "bad version": self.canonical(config_version=99),
        }.items():
            with self.subTest(document=label):
                self.write(document)
                code, _payload = self.payload(["config", "reset", "--all"])
                self.assertEqual(code, EXIT_OK, label)
                result = self.load()
                self.assertIs(result.status, ConfigStatus.VALID, label)
                self.assertIs(result.settings.execution_enabled, True, label)
                self.assertIs(
                    result.settings.permission_mode, PermissionMode.SESSION, label
                )
                self.assertIs(result.settings.paid_api_enabled, False, label)
                self.assertIsNone(result.settings.paid_api_approval_id, label)

    def test_reset_all_says_that_execution_is_back_on(self) -> None:
        self.store.reset_all()
        self.store.mutate(lambda s: replace(s, execution_enabled=False))
        code, payload = self.payload(["config", "reset", "--all"])
        self.assertEqual(code, EXIT_OK)
        self.assertIs(payload["execution_enabled"], True)
        self.assertIn("ENABLED", payload["next_step"])

    def test_an_ordinary_command_refuses_a_refused_store(self) -> None:
        """It must not fall back to the DEFAULT database and carry on."""
        self.write("{not json")
        code, payload = self.payload(["sessions"])
        self.assertEqual(code, EXIT_POLICY)
        self.assertEqual(payload["error"]["code"], "config_invalid")


class DatabasePathTests(ConfigTestCase):
    """The stored path must mean the same database from anywhere."""

    def test_a_relative_path_is_resolved_at_set_time(self) -> None:
        workdir = self.root / "work"
        workdir.mkdir()
        original = Path.cwd()
        os.chdir(workdir)
        try:
            code, _ = self.payload(["config", "set", "memory_database", "local.db"])
        finally:
            os.chdir(original)
        self.assertEqual(code, EXIT_OK)
        stored = json.loads(self.path.read_text(encoding="utf-8"))["memory_database"]
        self.assertTrue(Path(stored).is_absolute())
        self.assertEqual(Path(stored), (workdir / "local.db").resolve())

    def test_the_stored_path_means_the_same_db_from_another_cwd(self) -> None:
        workdir = self.root / "work"
        workdir.mkdir()
        original = Path.cwd()
        os.chdir(workdir)
        try:
            self.payload(["config", "set", "memory_database", "local.db"])
        finally:
            os.chdir(original)
        expected = (workdir / "local.db").resolve()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.chdir(elsewhere)
        try:
            self.assertEqual(load_settings().settings.memory_database, expected)
        finally:
            os.chdir(original)

    def test_a_home_relative_path_is_expanded(self) -> None:
        code, _ = self.payload(["config", "set", "memory_database", "~/jarvis-test.db"])
        self.assertEqual(code, EXIT_OK)
        stored = json.loads(self.path.read_text(encoding="utf-8"))["memory_database"]
        self.assertEqual(Path(stored), (Path.home() / "jarvis-test.db").resolve())

    def test_a_directory_is_refused_as_a_database(self) -> None:
        directory = self.root / "adir"
        directory.mkdir()
        code, payload = self.payload(
            ["config", "set", "memory_database", str(directory)]
        )
        self.assertNotEqual(code, EXIT_OK)
        self.assertEqual(payload["error"]["code"], "config_invalid")

    def test_a_nonexistent_database_is_allowed_and_not_created(self) -> None:
        target = self.root / "missing" / "jarvis.db"
        code, _ = self.payload(["config", "set", "memory_database", str(target)])
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(target.exists(), "config set created a database")
        self.assertFalse(target.parent.exists(), "config set created a directory")

    def test_a_relative_persisted_path_is_refused_not_reinterpreted(self) -> None:
        self.write(self.canonical(memory_database="relative.db"))
        self.assertIs(self.load().status, ConfigStatus.INVALID)

    def test_a_nul_byte_is_refused(self) -> None:
        self.write(self.canonical(memory_database="/tmp/a\x00b.db"))
        self.assertIs(self.load().status, ConfigStatus.INVALID)


class AtomicityTests(ConfigTestCase):
    """A mutation lands whole or not at all."""

    def temp_files(self) -> list[Path]:
        return [p for p in self.root.iterdir() if p.name.startswith(".config-")]

    def test_a_failed_write_preserves_the_previous_document(self) -> None:
        self.store.reset_all()
        before = self.path.read_bytes()

        def boom(*args, **kwargs):
            raise OSError("disk full")

        with mock.patch("os.fsync", boom), self.assertRaises(ConfigError) as caught:
            self.store.mutate(lambda s: replace(s, execution_enabled=False))
        self.assertIs(caught.exception.code, ErrorCode.CONFIG_IO_FAILED)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.temp_files(), [], "a temp file was left behind")

    def test_a_failed_replace_preserves_the_previous_document(self) -> None:
        self.store.reset_all()
        before = self.path.read_bytes()

        def boom(*args, **kwargs):
            raise OSError("replace failed")

        with mock.patch("os.replace", boom), self.assertRaises(ConfigError):
            self.store.mutate(lambda s: replace(s, execution_enabled=False))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.temp_files(), [], "a temp file was left behind")

    def test_a_lock_failure_is_bounded(self) -> None:
        self.store.reset_all()
        before = self.path.read_bytes()

        def boom(*args, **kwargs):
            raise OSError("no lock")

        with mock.patch("builtins.open", boom), self.assertRaises(ConfigError) as caught:
            self.store.mutate(lambda s: replace(s, execution_enabled=False))
        self.assertIs(caught.exception.code, ErrorCode.CONFIG_IO_FAILED)
        self.assertNotIn("no lock", caught.exception.message)
        self.assertEqual(before, self.path.read_bytes())

    def test_the_written_document_is_canonical(self) -> None:
        self.store.reset_all()
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))
        document = json.loads(text)
        self.assertEqual(set(document), set(CONFIG_KEYS))
        self.assertEqual(list(document), sorted(document), "keys are not sorted")

    def test_a_write_creates_the_directory_but_a_read_does_not(self) -> None:
        nested = self.root / "nested" / "config.json"
        store = ConfigStore(nested)
        self.assertIs(store.load().status, ConfigStatus.ABSENT)
        self.assertFalse(nested.parent.exists(), "a read created the directory")
        store.reset_all()
        self.assertTrue(nested.exists())


class ConcurrencyTests(ConfigTestCase):
    """A stale writer must not restore a switch somebody just changed."""

    def test_a_stale_read_modify_write_cannot_restore_the_old_switch(self) -> None:
        """The re-read must happen INSIDE the lock, or the switch comes back.

        The hazard has a direction: a writer that read the document BEFORE
        taking the lock can be overtaken by one that turns execution off, and
        then write its own stale copy back on top - silently re-enabling
        something the operator just disabled.

        Pinned deterministically by pausing the reading writer at its read.
        With the read inside the lock it is holding the lock while paused, so
        the disabling writer cannot even start until it is done, and the
        disable lands last. With the read outside the lock, the disable slips
        in between and is then overwritten.
        """
        self.store.reset_all()
        reading = threading.Event()
        release = threading.Event()
        real_read = ConfigStore._read_document_unlocked

        def paused_read(store_self):
            document = real_read(store_self)
            reading.set()
            release.wait(timeout=10)
            return document

        outcome: list[str] = []

        def slow_writer() -> None:
            with mock.patch.object(
                ConfigStore, "_read_document_unlocked", paused_read
            ):
                ConfigStore(self.path).mutate(
                    lambda s: replace(s, approval_ttl_minutes=45)
                )
            outcome.append("slow")

        def disabling_writer() -> None:
            ConfigStore(self.path).mutate(
                lambda s: replace(s, execution_enabled=False)
            )
            outcome.append("disable")

        slow = threading.Thread(target=slow_writer)
        slow.start()
        self.assertTrue(reading.wait(timeout=10), "the slow writer never read")
        disabler = threading.Thread(target=disabling_writer)
        disabler.start()
        # Give the disabler every chance to get in front of the slow writer.
        disabler.join(timeout=1.0)
        release.set()
        slow.join(timeout=15)
        disabler.join(timeout=15)
        self.assertFalse(slow.is_alive())
        self.assertFalse(disabler.is_alive())

        result = self.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertIs(
            result.settings.execution_enabled,
            False,
            "a stale writer restored the kill-switch",
        )

    def test_rapid_sequential_mutations_always_leave_a_valid_document(self) -> None:
        self.store.reset_all()
        for index in range(12):
            self.store.mutate(
                lambda s, i=index: replace(s, approval_ttl_minutes=(i % 60) + 1)
            )
            self.assertIs(self.load().status, ConfigStatus.VALID, str(index))
        self.assertEqual(self.temp_files_count(), 0)

    def temp_files_count(self) -> int:
        return len([p for p in self.root.iterdir() if p.name.startswith(".config-")])

    def test_concurrent_writers_never_produce_a_broken_document(self) -> None:
        self.store.reset_all()
        errors: list[Exception] = []

        def writer(minutes: int) -> None:
            try:
                ConfigStore(self.path).mutate(
                    lambda s: replace(s, approval_ttl_minutes=minutes)
                )
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(m,)) for m in range(1, 7)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertIs(self.load().status, ConfigStatus.VALID)


class CommandSurfaceTests(ConfigTestCase):
    """The operator surface: closed keys, bounded output, local only."""

    def test_set_and_show_round_trip_every_key(self) -> None:
        self.store.reset_all()
        target = self.root / "chosen.db"
        cases = {
            "execution_enabled": ("false", False),
            "permission_mode": ("strict", "strict"),
            "approval_ttl_minutes": ("90", 90),
            "claude_call_budget_usd": ("1.5", 1.5),
        }
        for key, (raw, expected) in cases.items():
            with self.subTest(key=key):
                code, _ = self.payload(["config", "set", key, raw])
                self.assertEqual(code, EXIT_OK, key)
                _code, shown = self.payload(["config", "show"])
                self.assertEqual(shown[key], expected, key)
        code, _ = self.payload(["config", "set", "memory_database", str(target)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(load_settings().settings.memory_database, target.resolve())

    def test_reset_returns_one_key_to_its_default(self) -> None:
        self.store.reset_all()
        self.payload(["config", "set", "approval_ttl_minutes", "90"])
        code, shown = self.payload(["config", "reset", "approval_ttl_minutes"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(shown["approval_ttl_minutes"], Settings().approval_ttl_minutes)

    def test_an_unknown_key_is_refused_by_the_parser(self) -> None:
        code, _output = self.run_cli(["config", "set", "monthly_hard_limit_usd", "999"])
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertFalse(self.path.exists(), "a refused key wrote a document")

    def test_reset_without_a_key_or_all_is_refused(self) -> None:
        self.store.reset_all()
        before = self.path.read_bytes()
        code, _payload = self.payload(["config", "reset"])
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertEqual(before, self.path.read_bytes())

    def test_an_unknown_sub_command_is_refused(self) -> None:
        code, payload = self.payload(["config"])
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertIn("show", payload["error"]["message"])

    def test_the_approval_id_relationship_is_enforced_at_set_time(self) -> None:
        self.store.reset_all()
        code, _payload = self.payload(["config", "set", "paid_api_enabled", "true"])
        self.assertNotEqual(code, EXIT_OK, "paid was enabled without an id")
        self.payload(["config", "set", "paid_api_approval_id", "approval-1"])
        code, shown = self.payload(["config", "set", "paid_api_enabled", "true"])
        self.assertEqual(code, EXIT_OK)
        self.assertIs(shown["paid_api_enabled"], True)
        # The id cannot be cleared while the route is on.
        code, _ = self.payload(["config", "reset", "paid_api_approval_id"])
        self.assertNotEqual(code, EXIT_OK)
        # Disabling keeps the id, and re-enabling reuses it.
        self.payload(["config", "set", "paid_api_enabled", "false"])
        self.assertEqual(
            load_settings().settings.paid_api_approval_id, "approval-1"
        )
        code, _ = self.payload(["config", "set", "paid_api_enabled", "true"])
        self.assertEqual(code, EXIT_OK)
        # reset --all clears both.
        self.payload(["config", "reset", "--all"])
        settings = load_settings().settings
        self.assertIs(settings.paid_api_enabled, False)
        self.assertIsNone(settings.paid_api_approval_id)

    def test_the_output_is_bounded_and_leaks_nothing(self) -> None:
        self.store.reset_all()
        self.payload(["config", "set", "paid_api_approval_id", "approval-1"])
        _code, shown = self.payload(["config", "show"])
        # The id itself is never echoed; only whether one is present.
        self.assertNotIn("approval-1", json.dumps(shown))
        self.assertIs(shown["paid_api_approval_id_present"], True)
        self.assertNotIn("paid_api_approval_id", shown)
        # The database path is home-relative or bounded, never raw.
        self.assertTrue(
            shown["memory_database"].startswith("~/")
            or shown["memory_database"] == "[PATH]",
            shown["memory_database"],
        )
        self.assertEqual(shown["monthly_hard_limit_usd"], MONTHLY_HARD_LIMIT_USD)
        self.assertIs(shown["monthly_hard_limit_configurable"], False)

    def test_config_commands_build_no_application(self) -> None:
        """run_cli's default factory raises if anything builds an app."""
        self.store.reset_all()
        for argv in (
            ["config", "show"],
            ["config", "set", "approval_ttl_minutes", "45"],
            ["config", "reset", "approval_ttl_minutes"],
            ["config", "reset", "--all"],
        ):
            with self.subTest(argv=" ".join(argv)):
                code, _ = self.run_cli(argv)
                self.assertIn(code, (EXIT_OK, EXIT_POLICY))

    def test_config_commands_open_no_database(self) -> None:
        self.store.reset_all()

        def explode(*args, **kwargs):
            raise AssertionError("a database was opened")

        with mock.patch.object(cli, "SQLiteMemoryStore", explode):
            for argv in (
                ["config", "show"],
                ["config", "set", "approval_ttl_minutes", "45"],
                ["config", "reset", "--all"],
            ):
                with self.subTest(argv=" ".join(argv)):
                    code, _ = self.run_cli(argv)
                    self.assertEqual(code, EXIT_OK)


class PrecedenceTests(ConfigTestCase):
    """Injected, then flag, then file, then defaults."""

    def test_injected_settings_bypass_the_store_entirely(self) -> None:
        self.write("{not json")

        def explode(path=None):
            raise AssertionError("the config store was read")

        injected = Settings(memory_database=self.root / "j.db")
        out = io.StringIO()
        with mock.patch.object(settings_module, "load_settings", explode), \
                mock.patch.object(cli, "load_settings", explode), \
                contextlib.redirect_stdout(out):
            code = cli.main(["status"], settings=injected)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())["permission_mode"], "session")

    def test_injected_settings_beat_a_transient_flag(self) -> None:
        injected = Settings(
            memory_database=self.root / "j.db", permission_mode=PermissionMode.STRICT
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(
                ["--permission-mode", "smart_auto", "status"], settings=injected
            )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())["permission_mode"], "strict")

    def test_a_flag_beats_the_store_but_never_writes_it(self) -> None:
        self.store.reset_all()
        self.store.mutate(
            lambda s: replace(s, permission_mode=PermissionMode.SESSION)
        )
        before = self.path.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--permission-mode", "strict", "status"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())["permission_mode"], "strict")
        self.assertEqual(before, self.path.read_bytes(), "the flag was persisted")

    def test_the_store_beats_the_code_defaults(self) -> None:
        self.store.reset_all()
        self.store.mutate(lambda s: replace(s, permission_mode=PermissionMode.STRICT))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["status"])
        self.assertEqual(json.loads(out.getvalue())["permission_mode"], "strict")


class KillSwitchTestCase(unittest.TestCase):
    """Direct AgentCore proof, with no CLI in the way."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.database = Path(self.temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.connector = _WatchfulConnector()
        self.registry = _WatchfulRegistry()
        self.registry.register(self.connector)

    def core(self, *, enabled: bool) -> AgentCore:
        return open_core(
            self.registry,
            self.memory,
            Settings(memory_database=self.database, execution_enabled=enabled),
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        self.connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )

    def database_content(self) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return {
                table: list(connection.execute(f"SELECT * FROM {table}"))
                for table in (
                    "tasks", "sessions", "execution_attempts", "attempt_billing",
                    "event_log", "loop_checkpoints", "usage_ledger",
                    "permission_grants", "decision_log",
                )
            }

    def assertNoExecutionState(self, before: dict, label: str = "") -> None:
        """Every table the loop writes, unchanged.

        Deliberately not the whole database: ``resume`` saves the session's
        navigation pointer eagerly, before the loop is entered at all, and
        that predates S4. What must not move is anything the EXECUTION path
        owns - the task itself, the attempt journal, billing, grants and the
        audit trail.
        """
        after = self.database_content()
        for table in (
            "tasks", "execution_attempts", "attempt_billing", "usage_ledger",
            "permission_grants", "decision_log", "loop_checkpoints", "event_log",
        ):
            with self.subTest(table=table):
                self.assertEqual(before[table], after[table], f"{label}: {table}")


class _WatchfulConnector(DummyConnector):
    """Records every question anybody asks it."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.touches: list[str] = []

    def can_handle(self, task):
        self.touches.append("can_handle")
        return super().can_handle(task)

    def suitability(self, task):
        self.touches.append("suitability")
        return super().suitability(task)

    def availability(self):
        self.touches.append("availability")
        return super().availability()

    def estimate_cost(self, task):
        self.touches.append("estimate_cost")
        return super().estimate_cost(task)

    def execute(self, task, session):
        self.touches.append("execute")
        return super().execute(task, session)

    def verify(self, task, result, session):
        self.touches.append("verify")
        return super().verify(task, result, session)


class _WatchfulRegistry(ToolRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.selections = 0

    def get(self, name: str):
        self.selections += 1
        return super().get(name)


class GlobalKillSwitchTests(KillSwitchTestCase):
    """Disabled means nothing starts, and nothing is written."""

    def test_a_disabled_run_reaches_no_authority_at_all(self) -> None:
        before = self.database_content()
        run = self.core(enabled=False).run(make_task())
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_DISABLED)
        # Nothing was asked of the connector, and nothing was selected.
        self.assertEqual(self.connector.touches, [])
        self.assertEqual(self.registry.selections, 0)
        # No attempt, no reservation, no ledger row, no grant, no checkpoint.
        after = self.database_content()
        for table in (
            "execution_attempts", "attempt_billing", "usage_ledger",
            "permission_grants", "decision_log", "loop_checkpoints",
        ):
            with self.subTest(table=table):
                self.assertEqual(before[table], after[table], f"{table} was written")

    def test_a_disabled_run_creates_no_pending_approval(self) -> None:
        run = self.core(enabled=False).run(
            make_task(effect=SideEffect.EXTERNAL_EGRESS, reversible=False)
        )
        self.assertIsNot(run.status, RunStatus.AWAITING_APPROVAL)
        self.assertIsNone(run.permission_request)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM pending_approvals"
            ).fetchone()[0]
        self.assertEqual(pending, 0)

    def test_no_approval_or_grant_can_answer_the_barrier(self) -> None:
        """It is a barrier, not a gate: nothing satisfies it."""
        gated = self.core(enabled=True).run(
            make_task(effect=SideEffect.EXTERNAL_EGRESS, reversible=False)
        )
        self.assertIs(gated.status, RunStatus.AWAITING_APPROVAL)
        assert gated.permission_request is not None
        from jarvis.contracts import PermissionGrant

        grant = PermissionGrant.for_request(gated.permission_request)
        self.connector.touches.clear()
        before = self.database_content()
        run = self.core(enabled=False).resume(
            gated.session.id, gated.task.id, grant=grant
        )
        self.assertIs(run.status, RunStatus.FAILED)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_DISABLED)
        self.assertEqual(self.connector.touches, [])
        after = self.database_content()
        self.assertEqual(
            before["permission_grants"], after["permission_grants"], "a grant was used"
        )
        self.assertEqual(before["execution_attempts"], after["execution_attempts"])

    def test_the_paid_api_switch_is_not_the_kill_switch(self) -> None:
        """Two different things, and the barrier is the stronger one."""
        settings = Settings(
            memory_database=self.database,
            execution_enabled=False,
            paid_api_enabled=True,
            paid_api_approval_id="approval-1",
        )
        core = open_core(
            self.registry,
            self.memory,
            settings,
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        self.connector.manifest.name,
                        DUMMY_CAPABILITY,
                        CLAUDE_CODE_DESTINATION,
                    )
                }
            ),
        )
        run = core.run(make_task())
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_DISABLED)
        self.assertEqual(self.connector.touches, [])

    def test_a_disabled_refusal_rewrites_no_task_state(self) -> None:
        enabled = self.core(enabled=True)
        gated = enabled.run(make_task(effect=SideEffect.EXTERNAL_EGRESS, reversible=False))
        before = self.database_content()
        stored_before = self.memory.load_task(gated.task.id)
        assert stored_before is not None
        self.core(enabled=False).resume(gated.session.id, gated.task.id)
        self.assertNoExecutionState(before, "disabled resume")

    def test_enabling_execution_starts_nothing_by_itself(self) -> None:
        run = self.core(enabled=False).run(make_task())
        self.assertIs(run.status, RunStatus.FAILED)
        before = self.database_content()
        # Constructing an enabled core changes nothing until asked.
        self.core(enabled=True)
        self.assertEqual(before, self.database_content())
        self.assertEqual(self.connector.touches, [])

    def test_the_positive_control_still_executes(self) -> None:
        run = self.core(enabled=True).run(make_task())
        self.assertIs(run.status, RunStatus.COMPLETED)
        self.assertIn("execute", self.connector.touches)


class KillSwitchH2PrecedenceTests(KillSwitchTestCase):
    """Existing verdicts outrank the switch."""

    def staged_task(self, state: AttemptState | None) -> tuple[str, Task]:
        session = SessionState(objective="kill switch")
        self.memory.save_session(session)
        task = make_task()
        task.status = TaskStatus.RUNNING
        task.stage = LoopStage.EXECUTE
        self.memory.save_task(session.id, task)
        if state is not None:
            attempt = self.memory.open_execution_attempt(
                ExecutionAttempt(
                    session_id=session.id,
                    task_id=task.id,
                    connector="dummy",
                    fingerprint="f" * 64,
                )
            )
            if state is not AttemptState.PREPARED:
                with contextlib.closing(sqlite3.connect(self.database)) as db:
                    db.execute(
                        "UPDATE execution_attempts SET state = ? WHERE id = ?",
                        (state.value, attempt.id),
                    )
                    db.commit()
        return session.id, task

    def test_a_terminal_task_is_still_reported(self) -> None:
        session_id, task = self.staged_task(None)
        stored = self.memory.load_task(task.id)
        assert stored is not None
        stored.status = TaskStatus.COMPLETED
        stored.stage = LoopStage.COMPLETED
        self.memory.save_task(session_id, stored)
        run = self.core(enabled=False).resume(session_id, task.id)
        self.assertIs(run.status, RunStatus.COMPLETED)
        assert run.error is None or run.error.code is not ErrorCode.EXECUTION_DISABLED

    def test_in_flight_stays_in_doubt_not_disabled(self) -> None:
        session_id, task = self.staged_task(AttemptState.IN_FLIGHT)
        before = self.database_content()
        run = self.core(enabled=False).resume(session_id, task.id)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        self.assertIsNot(run.error.code, ErrorCode.EXECUTION_DISABLED)
        self.assertNoExecutionState(before, "in-doubt resume")
        self.assertEqual(self.connector.touches, [])

    def test_unknown_outcome_stays_in_doubt_not_disabled(self) -> None:
        session_id, task = self.staged_task(AttemptState.UNKNOWN_OUTCOME)
        before = self.database_content()
        run = self.core(enabled=False).resume(session_id, task.id)
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_UNKNOWN_OUTCOME)
        self.assertNoExecutionState(before, "unknown-outcome resume")

    def test_the_switch_never_replays_an_in_doubt_attempt(self) -> None:
        session_id, task = self.staged_task(AttemptState.IN_FLIGHT)
        for enabled in (False, True, False):
            with self.subTest(enabled=enabled):
                self.core(enabled=enabled).resume(session_id, task.id)
                self.assertEqual(self.connector.touches, [], "a replay happened")
        attempt = self.memory.latest_execution_attempt(task.id)
        assert attempt is not None
        self.assertIs(attempt.state, AttemptState.IN_FLIGHT)


class BillingAuthorityTests(ConfigTestCase):
    """The monthly cap is not configuration, and never becomes it."""

    def test_the_fixed_cap_is_unchanged_and_not_persistable(self) -> None:
        self.assertEqual(MONTHLY_HARD_LIMIT_USD, 20.0)
        self.assertNotIn("monthly_hard_limit_usd", CONFIG_KEYS)
        self.write(self.canonical(monthly_hard_limit_usd=1000.0))
        self.assertIs(self.load().status, ConfigStatus.INVALID)

    def test_settings_still_refuses_any_other_cap(self) -> None:
        with self.assertRaises(ValueError):
            Settings(monthly_hard_limit_usd=1000.0)

    def test_the_call_budget_is_not_a_monthly_authority(self) -> None:
        """It is a per-call ceiling and is bounded well under the cap."""
        self.write(self.canonical(claude_call_budget_usd=20.0))
        result = self.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertEqual(result.settings.monthly_hard_limit_usd, MONTHLY_HARD_LIMIT_USD)
        self.write(self.canonical(claude_call_budget_usd=20.01))
        self.assertIs(self.load().status, ConfigStatus.INVALID)

    def test_no_config_command_touches_a_billing_table(self) -> None:
        database = self.root / "jarvis.db"
        SQLiteMemoryStore(database)
        with contextlib.closing(sqlite3.connect(database)) as connection:
            before = {
                table: list(connection.execute(f"SELECT * FROM {table}"))
                for table in ("attempt_billing", "usage_ledger", "permission_grants")
            }
        self.store.reset_all()
        self.payload(["config", "set", "claude_call_budget_usd", "2"])
        with contextlib.closing(sqlite3.connect(database)) as connection:
            after = {
                table: list(connection.execute(f"SELECT * FROM {table}"))
                for table in ("attempt_billing", "usage_ledger", "permission_grants")
            }
        self.assertEqual(before, after)


class SecretsTests(ConfigTestCase):
    """Nothing secret is persisted, because nothing secret is a setting."""

    def test_no_credential_key_exists_in_the_document(self) -> None:
        for key in CONFIG_KEYS:
            with self.subTest(key=key):
                for banned in ("key", "token", "secret", "password", "credential"):
                    self.assertNotIn(banned, key)

    def test_a_credential_shaped_approval_id_is_refused(self) -> None:
        self.store.reset_all()
        for value in ("sk-abcdefghijklmnop", "Bearer abcdefghijkl", "ghp_abcdefghijkl"):
            with self.subTest(value=value[:6]):
                code, payload = self.payload(
                    ["config", "set", "paid_api_approval_id", value]
                )
                self.assertNotEqual(code, EXIT_OK, value[:6])
                self.assertNotIn(value, json.dumps(payload))

    def test_a_labelled_credential_is_refused_by_the_canonical_policy(self) -> None:
        """The reason this field reuses the established policy.

        A prefix check sees ``api_key:HUNTER2`` as an ordinary identifier and
        waves it through. The shared egress policy is syntax-aware, so it
        reads the label and the delimiter and refuses the whole shape - and
        that is the policy this field defers to, not a second weaker one.
        """
        self.store.reset_all()
        for value in (
            "api_key:HUNTER2",
            "client_secret:HUNTER2",
            "AWS_SECRET_ACCESS_KEY:HUNTER2",
            "password:HUNTER2",
            "token:HUNTER2",
        ):
            with self.subTest(value=value.split(":")[0]):
                self.assertTrue(
                    credential_bearing(value), "the canonical policy changed"
                )
                with self.assertRaises((ConfigError, ValueError)):
                    self.store.mutate(
                        lambda current, v=value: replace(
                            current, paid_api_approval_id=v
                        )
                    )
                self.assertNotIn("HUNTER2", self.path.read_text(encoding="utf-8"))

    def test_this_field_refuses_a_superset_of_the_shared_policy(self) -> None:
        """Never a subset. The supplementary shape only ever adds."""
        for value in (
            "api_key:HUNTER2",
            "sk-abcdefghijklmnop",
            "Bearer abcdefghijkl",
            "AWS_SECRET_ACCESS_KEY:HUNTER2",
            "ghp_abcdefghijkl",
        ):
            with self.subTest(value=value[:8]):
                if credential_bearing(value):
                    self.assertTrue(
                        settings_module._looks_like_a_credential(value),
                        "the field is more permissive than the shared policy",
                    )
                with self.assertRaises(ValueError):
                    Settings(paid_api_approval_id=value)

    def test_a_refused_approval_id_is_never_echoed_back(self) -> None:
        self.store.reset_all()
        code, payload = self.payload(
            ["config", "set", "paid_api_approval_id", "api_key:HUNTER2"]
        )
        self.assertNotEqual(code, EXIT_OK)
        rendered = json.dumps(payload)
        self.assertNotIn("HUNTER2", rendered)
        self.assertNotIn("api_key", rendered)

    def test_an_ordinary_approval_id_is_still_accepted(self) -> None:
        """The control: the policy refuses credentials, not approval ids."""
        self.store.reset_all()
        for value in ("approval-1", "session-approval", "ops-2026-08-31"):
            with self.subTest(value=value):
                code, _ = self.payload(
                    ["config", "set", "paid_api_approval_id", value]
                )
                self.assertEqual(code, EXIT_OK, value)
                self.assertEqual(self.load().settings.paid_api_approval_id, value)


CHAIN_STEP = {
    "objective": "echo this",
    "capability": "echo",
    "action": "read",
    "target": "notes.txt",
    "destination": "",
    "write_mode": "",
    "consumes": CHAIN_NO_PREDECESSOR,
}


class ChainKillSwitchTests(KillSwitchTestCase):
    """The chain driver is behind the same barrier as everything else.

    A chain is the one path that can create authority on its own: it derives
    a step task, writes it, and binds its id into the chain before any
    ordinary run begins. So refusing at the ordinary resume would be too
    late - the task row and the binding would already exist, and the chain
    would have moved while the operator believed nothing could run.
    """

    def declared(self, count: int = 2, bound: tuple[str, ...] | None = None) -> str:
        session = SessionState(objective="chain")
        self.memory.save_session(session)
        steps = chain_steps_from_declaration([dict(CHAIN_STEP) for _ in range(count)])
        self.assertEqual(len(steps), count, "the declaration itself was refused")
        chain = ChainState(
            id="chain-1",
            steps=steps,
            step_task_ids=bound or ("",) * count,
            cursor=0,
            status=ChainStatus.ACTIVE,
        )
        self.assertEqual(
            self.memory.declare_chain_if_absent(session.id, chain), "declared"
        )
        return session.id

    def chain_now(self, session_id: str):
        session = self.memory.load_session(session_id)
        assert session is not None and session.chain is not None
        return session.chain

    def test_a_disabled_chain_creates_no_step_task(self) -> None:
        session_id = self.declared()
        run, verdict = self.core(enabled=False).run_chain(session_id)
        self.assertEqual(verdict, "blocked")
        assert run.error is not None
        self.assertIs(run.error.code, ErrorCode.EXECUTION_DISABLED)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            rows = list(db.execute("SELECT id FROM tasks"))
        self.assertEqual(rows, [], "a step task was materialized while disabled")

    def test_a_disabled_chain_binds_no_task_id(self) -> None:
        session_id = self.declared()
        before = self.chain_now(session_id)
        self.core(enabled=False).run_chain(session_id)
        after = self.chain_now(session_id)
        self.assertEqual(after.step_task_ids, ("", ""), "a binding was written")
        self.assertEqual(after.step_task_ids, before.step_task_ids)

    def test_a_disabled_chain_does_not_move_the_cursor(self) -> None:
        session_id = self.declared()
        self.core(enabled=False).run_chain(session_id)
        after = self.chain_now(session_id)
        self.assertEqual(after.cursor, 0)
        self.assertIs(after.status, ChainStatus.ACTIVE, "the chain was settled")

    def test_a_disabled_chain_writes_no_execution_state(self) -> None:
        session_id = self.declared()
        before = self.database_content()
        self.core(enabled=False).run_chain(session_id)
        self.assertNoExecutionState(before, "disabled chain")

    def test_a_disabled_chain_reaches_no_registry_or_connector(self) -> None:
        session_id = self.declared()
        self.core(enabled=False).run_chain(session_id)
        self.assertEqual(self.registry.selections, 0, "the registry was consulted")
        self.assertEqual(self.connector.touches, [], "a connector was consulted")

    def test_an_already_bound_step_reports_its_durable_truth(self) -> None:
        """The switch does not get to relabel work that already happened."""
        session = SessionState(objective="chain")
        self.memory.save_session(session)
        task = make_task()
        task.status = TaskStatus.COMPLETED
        task.stage = LoopStage.COMPLETED
        self.memory.save_task(session.id, task)
        steps = chain_steps_from_declaration([dict(CHAIN_STEP) for _ in range(2)])
        self.memory.declare_chain_if_absent(
            session.id,
            ChainState(
                id="chain-1",
                steps=steps,
                step_task_ids=(task.id, ""),
                cursor=0,
                status=ChainStatus.ACTIVE,
            ),
        )
        run, _ = self.core(enabled=False).run_chain(session.id)
        self.assertIs(run.status, RunStatus.COMPLETED)
        if run.error is not None:
            self.assertIsNot(run.error.code, ErrorCode.EXECUTION_DISABLED)

    def test_an_enabled_chain_still_starts_its_first_step(self) -> None:
        """The positive control: the barrier is a switch, not a wall."""
        session_id = self.declared()
        self.core(enabled=True).run_chain(session_id)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            rows = list(db.execute("SELECT id FROM tasks"))
        self.assertTrue(rows, "nothing ran with the switch on")


class SettingsTypeTests(unittest.TestCase):
    """Settings is THE typed runtime authority, so it has to be typed.

    Core compares ``permission_mode`` with ``is`` against enum members and
    reads the switches as booleans. A raw string or an ``int`` 1 would match
    nothing and therefore refuse nothing - a policy that fails open by never
    being recognized. Every field is checked for its exact runtime type at
    the single point every construction passes through.
    """

    def absolute(self) -> Path:
        return Path(tempfile.gettempdir()).resolve() / "jarvis-typed.db"

    def test_permission_mode_must_be_a_real_member(self) -> None:
        for value in ("strict", "SESSION", 0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(permission_mode=value)

    def test_the_switches_must_be_exactly_bool(self) -> None:
        for field in ("execution_enabled", "paid_api_enabled"):
            for value in (1, 0, "true", None):
                with self.subTest(field=field, value=value), self.assertRaises(
                    ValueError
                ):
                    Settings(**{field: value})

    def test_the_approval_window_must_be_exactly_int(self) -> None:
        # bool subclasses int, so True would otherwise pass as one minute.
        for value in (True, 30.0, "30", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(approval_ttl_minutes=value)

    def test_the_amounts_must_be_numbers(self) -> None:
        for value in ("0.25", None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(claude_call_budget_usd=value)

    def test_the_approval_id_must_be_exactly_a_string(self) -> None:
        for value in (1, 1.0, True, b"a-1", Path("a-1")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(paid_api_approval_id=value)

    def test_a_string_database_is_converted_not_merely_accepted(self) -> None:
        """Backwards compatible, and still typed: the result is a real Path."""
        wanted = self.absolute()
        try:
            settings = Settings(memory_database=str(wanted))
        except ValueError as exc:
            self.fail(f"a string database was refused instead of converted: {exc}")
        self.assertIsInstance(settings.memory_database, Path)
        self.assertEqual(settings.memory_database, wanted)

    def test_a_relative_database_is_refused(self) -> None:
        # A relative path names a different file from a different working
        # directory. Settings that say "the database" must mean exactly one.
        for value in ("rel.db", "./rel.db", Path("rel.db")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(memory_database=value)

    def test_a_database_that_is_not_a_path_at_all_is_refused(self) -> None:
        for value in (1, None, True, b"/tmp/x.db", ["/tmp/x.db"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(memory_database=value)

    def test_the_ordinary_valid_construction_still_works(self) -> None:
        """The control: nothing above narrowed what production already builds."""
        settings = Settings(
            memory_database=self.absolute(),
            execution_enabled=False,
            permission_mode=PermissionMode.STRICT,
            paid_api_enabled=False,
            approval_ttl_minutes=90,
            claude_call_budget_usd=1.5,
        )
        self.assertIs(settings.permission_mode, PermissionMode.STRICT)
        self.assertIs(settings.execution_enabled, False)


class WriteDefenceTests(ConfigTestCase):
    """The write path is never more permissive than the read path.

    A store that persists a document it will refuse on the next startup has
    corrupted itself, and the operator has no way to tell that from damage
    they did not cause. So ``write`` validates through the SAME grammar
    ``load`` uses, before the destination is touched at all.
    """

    def test_write_refuses_what_its_own_loader_would_refuse(self) -> None:
        """The asymmetry itself, not a value the dataclass already refuses.

        ``Settings`` accepts an approval id that is merely a non-blank,
        non-credential string. The DOCUMENT grammar is narrower and demands
        the ``_APPROVAL_ID_PATTERN`` shape. So a value with spaces is exactly
        the case only the write path's own validation can catch - and if that
        validation is removed, this document is persisted and then refused on
        the very next load.
        """
        self.store.reset_all()
        original = self.path.read_bytes()
        smuggled = replace(
            Settings(memory_database=self.root / "j.db"),
            paid_api_approval_id="approval id with spaces",
        )
        with self.assertRaises((ConfigError, ValueError)):
            self.store.write(smuggled)
        self.assertEqual(self.path.read_bytes(), original, "the old bytes moved")
        self.assertIs(self.load().status, ConfigStatus.VALID)

    def test_a_value_the_dataclass_itself_refuses_never_reaches_write(self) -> None:
        with self.assertRaises(ValueError):
            Settings(memory_database=self.root / "j.db", approval_ttl_minutes=0)

    def test_everything_write_accepts_loads_back_valid(self) -> None:
        for change in (
            {"execution_enabled": False},
            {"permission_mode": PermissionMode.STRICT},
            {"approval_ttl_minutes": 1440},
            {"claude_call_budget_usd": 20.0},
            {"paid_api_approval_id": "approval-1"},
        ):
            with self.subTest(change=next(iter(change))):
                self.store.write(
                    replace(Settings(memory_database=self.root / "j.db"), **change)
                )
                self.assertIs(self.load().status, ConfigStatus.VALID)

    def test_a_refused_write_leaves_no_debris(self) -> None:
        self.store.reset_all()
        with self.assertRaises((ConfigError, ValueError)):
            self.store.write(
                Settings(
                    memory_database=self.root / "j.db",
                    claude_call_budget_usd=1_000.0,
                )
            )
        leftovers = [
            entry.name
            for entry in self.root.iterdir()
            if entry.name not in ("config.json", "config.json.lock")
        ]
        self.assertEqual(leftovers, [], f"temporary files survived: {leftovers}")


#: Representative raw provider tokens, one per anchored family. These are
#: fabricated shapes, not live credentials - the point is the PREFIX.
RAW_TOKEN_FAMILIES = {
    "github_pat": "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz01",
    "ghp": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "gho": "gho_abcdefghijklmnopqrstuvwxyz0123456789",
    "ghu": "ghu_abcdefghijklmnopqrstuvwxyz0123456789",
    "ghs": "ghs_abcdefghijklmnopqrstuvwxyz0123456789",
    "ghr": "ghr_abcdefghijklmnopqrstuvwxyz0123456789",
    "glpat": "glpat-abcdefghijklmnopqrst",
    "xoxb": "xoxb-1234567890-1234567890123-abcdefghijklmnopqrstuvwx",
    "xoxp": "xoxp-1234567890-1234567890123-abcdefghijklmnopqrstuvwx",
    "xoxa": "xoxa-1234567890-1234567890123-abcdefghijklmnopqrstuvwx",
    "xoxr": "xoxr-1234567890-1234567890123-abcdefghijklmnopqrstuvwx",
    "xapp": "xapp-1-A012345678-1234567890123-abcdefghijklmnopqrstuv",
    "ya29": "ya29.a0ARrdaM-abcdefghijklmnopqrstuvwxyz0123456789",
}

#: The one representative per provider used where a full sweep would only
#: repeat itself. Every family is still swept on the Settings path.
ONE_PER_PROVIDER = ("github_pat", "glpat", "xoxb", "ya29")

#: Families the CANONICAL ``credential_bearing`` policy recognizes on its
#: own, so the anchored supplement is no longer what refuses them. GitHub
#: moved here when its token shapes were added to the shared scanner, which
#: is a widening of the shared defence and not a weakening of this one: the
#: refusal below is still asserted for every family either way. The split is
#: explicit so that a family silently CHANGING side - in either direction -
#: is a failure rather than a shrug.
SHARED_POLICY_FAMILIES = frozenset(
    {"github_pat", "ghp", "gho", "ghu", "ghs", "ghr"}
)

#: Ordinary approval identifiers that merely CONTAIN a harmless word, or sit
#: near a token family without being one. Refusing any of these would be a
#: false positive on inert metadata.
SAFE_APPROVAL_IDS = (
    "approval-1",
    "session-approval",
    "approval_tokenizer",
    "github-approval",
    "gitlab-approval",
    "slack-approval",
    "google-approval",
    "pat-approval",
    "token-review",
    "ghost-approval",
    "xoxo-approval",
    "yaml-approval",
)


class RawTokenRefusalTests(ConfigTestCase):
    """No raw provider token reaches this plaintext field, by any path.

    ``paid_api_approval_id`` is inert compatibility metadata stored in a
    world-readable JSON file. The canonical ``credential_bearing`` policy is
    syntax-aware - it reads labels and delimiters - so a RAW token, which
    carries neither, is invisible to it. That gap is closed here by a closed
    list of anchored token families, and only that: there is no substring
    hunt for "token" or "secret", because an approval id is allowed to
    contain an ordinary word.
    """

    def smuggled(self, value: str) -> Settings:
        """A Settings carrying ``value`` past its own constructor.

        Only useful for proving that ``write`` does not TRUST its argument.
        Nothing in production builds a Settings this way.
        """
        settings = Settings(memory_database=self.root / "j.db")
        object.__setattr__(settings, "paid_api_approval_id", value)
        return settings

    # -- path 1: direct construction -----------------------------------

    def test_no_raw_token_family_survives_settings_construction(self) -> None:
        for family, value in RAW_TOKEN_FAMILIES.items():
            with self.subTest(family=family):
                self.assertEqual(
                    family in SHARED_POLICY_FAMILIES,
                    credential_bearing(value),
                    "a family changed which defence refuses it; that may be "
                    "done deliberately, but not by accident",
                )
                with self.assertRaises(ValueError):
                    Settings(
                        memory_database=self.root / "j.db",
                        paid_api_approval_id=value,
                    )

    # -- path 2: the store's mutate/set path ---------------------------

    def test_no_raw_token_survives_a_store_mutation(self) -> None:
        for family in ONE_PER_PROVIDER:
            value = RAW_TOKEN_FAMILIES[family]
            with self.subTest(family=family):
                self.store.reset_all()
                original = self.path.read_bytes()
                with self.assertRaises((ConfigError, ValueError)):
                    self.store.mutate(
                        lambda current, v=value: replace(
                            current, paid_api_approval_id=v
                        )
                    )
                self.assertEqual(self.path.read_bytes(), original)
                self.assertNotIn(value, self.path.read_text(encoding="utf-8"))

    # -- path 3: write, which must not trust its argument --------------

    def test_write_refuses_a_raw_token_smuggled_past_the_constructor(self) -> None:
        for family in ONE_PER_PROVIDER:
            value = RAW_TOKEN_FAMILIES[family]
            with self.subTest(family=family):
                self.store.reset_all()
                original = self.path.read_bytes()
                with self.assertRaises((ConfigError, ValueError)):
                    self.store.write(self.smuggled(value))
                self.assertEqual(
                    self.path.read_bytes(), original, "the old bytes moved"
                )
                self.assertNotIn(value, self.path.read_text(encoding="utf-8"))

    def test_a_refused_raw_token_leaves_no_debris_behind(self) -> None:
        self.store.reset_all()
        value = RAW_TOKEN_FAMILIES["ya29"]
        with self.assertRaises((ConfigError, ValueError)):
            self.store.write(self.smuggled(value))
        for entry in self.root.iterdir():
            with self.subTest(entry=entry.name):
                self.assertIn(
                    entry.name,
                    ("config.json", "config.json.lock"),
                    f"a temporary file survived: {entry.name}",
                )
                if entry.name == "config.json":
                    self.assertNotIn(value, entry.read_text(encoding="utf-8"))

    # -- path 4: the CLI ------------------------------------------------

    def test_the_cli_refuses_every_family_without_echoing_it(self) -> None:
        for family, value in RAW_TOKEN_FAMILIES.items():
            with self.subTest(family=family):
                self.store.reset_all()
                original = self.path.read_bytes()
                code, payload = self.payload(
                    ["config", "set", "paid_api_approval_id", value]
                )
                self.assertEqual(code, EXIT_POLICY, family)
                rendered = json.dumps(payload)
                self.assertNotIn(value, rendered, "the raw token was echoed")
                # Not even a recognizable head of it.
                self.assertNotIn(value[:12], rendered)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertNotIn(value, self.path.read_text(encoding="utf-8"))

    # -- the shared policy is still the primary authority ---------------

    def test_the_canonical_policy_is_still_enforced(self) -> None:
        """The supplement adds; it never replaces."""
        for value in (
            "api_key:HUNTER2",
            "api-key:HUNTER2",
            "client_secret:HUNTER2",
            "client-secret:HUNTER2",
            "password:HUNTER2",
            "token:HUNTER2",
            "Bearer abcdefghijkl",
            "sk-abcdefghijklmnop",
        ):
            with self.subTest(value=value.split(":")[0]):
                self.assertTrue(
                    credential_bearing(value), "the shared policy changed"
                )
                with self.assertRaises(ValueError):
                    Settings(
                        memory_database=self.root / "j.db",
                        paid_api_approval_id=value,
                    )

    # -- and it is still a supplement, not a general engine -------------

    def test_an_ordinary_approval_id_is_never_refused(self) -> None:
        for value in SAFE_APPROVAL_IDS:
            with self.subTest(value=value):
                try:
                    settings = Settings(
                        memory_database=self.root / "j.db",
                        paid_api_approval_id=value,
                    )
                except ValueError as exc:
                    self.fail(f"a safe approval id was refused: {value} ({exc})")
                self.assertEqual(settings.paid_api_approval_id, value)

    def test_a_safe_approval_id_still_round_trips_through_the_cli(self) -> None:
        for value in SAFE_APPROVAL_IDS:
            with self.subTest(value=value):
                self.store.reset_all()
                code, _ = self.payload(
                    ["config", "set", "paid_api_approval_id", value]
                )
                self.assertEqual(code, EXIT_OK, value)
                self.assertEqual(self.load().settings.paid_api_approval_id, value)
                # ``show`` reports PRESENCE, never the value - so the
                # round-trip is proved through the store, not the output.
                code, payload = self.payload(["config", "show"])
                self.assertEqual(code, EXIT_OK)
                self.assertIs(payload["paid_api_approval_id_present"], True)
                self.assertNotIn(value, json.dumps(payload))

    def test_the_supplement_stays_a_closed_anchored_list(self) -> None:
        """Anchored only: a token family named mid-string is not a match.

        This is what keeps the supplement from drifting into a general
        credential engine. If somebody replaces an anchor with a substring
        search, these stop being accepted and this test says so.
        """
        for value in ("approval-ghp_x", "approval-xoxb-1", "approval-ya29.x"):
            with self.subTest(value=value):
                try:
                    Settings(
                        memory_database=self.root / "j.db",
                        paid_api_approval_id=value,
                    )
                except ValueError as exc:
                    self.fail(f"the supplement is no longer anchored: {exc}")
        for shape in settings_module._EXTRA_CREDENTIAL_SHAPES:
            with self.subTest(shape=shape.pattern):
                self.assertTrue(
                    shape.pattern.startswith("\\A"),
                    f"unanchored shape: {shape.pattern}",
                )


class ClaudeSelectionTests(ConfigTestCase):
    """Manual Claude model/effort selection: operator authority, bounded data.

    The selection is provider-scoped on purpose (only Claude is wired), is
    validated at the one shared grammar in ``settings``, and is stored as
    ordinary non-secret preference data. Nothing here may relax any other
    setting.
    """

    def test_defaults_are_unset(self) -> None:
        settings = self.load().settings
        self.assertIsNone(settings.claude_model)
        self.assertIsNone(settings.claude_effort)

    def test_valid_values_round_trip_through_the_store(self) -> None:
        def change(current: Settings) -> Settings:
            return replace(
                current,
                claude_model="claude-sonnet-4-5",
                claude_effort="high",
            )

        updated = self.store.mutate(change)
        self.assertEqual(updated.claude_model, "claude-sonnet-4-5")
        self.assertEqual(updated.claude_effort, "high")
        reloaded = self.store.load()
        self.assertIs(reloaded.status, ConfigStatus.VALID)
        self.assertEqual(reloaded.settings.claude_model, "claude-sonnet-4-5")
        self.assertEqual(reloaded.settings.claude_effort, "high")

    def test_a_document_with_null_selection_is_valid(self) -> None:
        self.write(self.canonical())
        result = self.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertIsNone(result.settings.claude_model)
        self.assertIsNone(result.settings.claude_effort)

    # -- Finding 1: the exact legacy ea1c123-era v1 shape -------------------

    def _legacy_document(self, **overrides: object) -> dict:
        document = self.canonical(**overrides)
        del document["claude_model"]
        del document["claude_effort"]
        return document

    def test_the_exact_legacy_v1_document_loads_valid(self) -> None:
        self.write(self._legacy_document())
        result = self.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertIsNone(result.settings.claude_model)
        self.assertIsNone(result.settings.claude_effort)
        # Every persisted pre-existing setting survives untouched.
        self.assertIs(result.settings.execution_enabled, True)
        self.assertIs(result.settings.permission_mode, PermissionMode.SESSION)
        self.assertIs(result.settings.paid_api_enabled, False)
        self.assertIsNone(result.settings.paid_api_approval_id)
        self.assertEqual(result.settings.claude_call_budget_usd, 0.25)
        self.assertEqual(result.settings.approval_ttl_minutes, 30)
        self.assertEqual(
            result.settings.memory_database,
            Path(self.canonical()["memory_database"]),
        )

    def test_the_legacy_document_preserves_non_default_values(self) -> None:
        self.write(
            self._legacy_document(
                execution_enabled=False,
                permission_mode="strict",
                approval_ttl_minutes=90,
                claude_call_budget_usd=1.5,
                paid_api_enabled=True,
                paid_api_approval_id="approval-1",
            )
        )
        result = self.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertIs(result.settings.execution_enabled, False)
        self.assertIs(result.settings.permission_mode, PermissionMode.STRICT)
        self.assertEqual(result.settings.approval_ttl_minutes, 90)
        self.assertEqual(result.settings.claude_call_budget_usd, 1.5)
        self.assertIs(result.settings.paid_api_enabled, True)
        self.assertEqual(result.settings.paid_api_approval_id, "approval-1")
        self.assertIsNone(result.settings.claude_model)
        self.assertIsNone(result.settings.claude_effort)

    def test_one_new_key_without_the_other_is_still_invalid(self) -> None:
        for dropped in ("claude_model", "claude_effort"):
            with self.subTest(dropped=dropped):
                document = self.canonical()
                del document[dropped]
                self.write(document)
                self.assertIs(self.load().status, ConfigStatus.INVALID)

    def test_set_on_a_legacy_document_upgrades_it_to_the_current_shape(self) -> None:
        self.write(self._legacy_document())
        code, shown = self.payload(["config", "set", "claude_effort", "high"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(shown["claude_effort"], "high")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(stored), set(CONFIG_KEYS))
        self.assertEqual(stored["claude_effort"], "high")
        self.assertIsNone(stored["claude_model"])
        self.assertIs(ConfigStore(self.path).load().status, ConfigStatus.VALID)

    def test_reset_key_on_a_legacy_document_upgrades_it_too(self) -> None:
        self.write(self._legacy_document(approval_ttl_minutes=90))
        code, shown = self.payload(["config", "reset", "approval_ttl_minutes"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(shown["approval_ttl_minutes"], Settings().approval_ttl_minutes)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(stored), set(CONFIG_KEYS))
        self.assertIs(ConfigStore(self.path).load().status, ConfigStatus.VALID)

    def test_a_mutated_legacy_document_keeps_its_old_values(self) -> None:
        self.write(
            self._legacy_document(
                execution_enabled=False,
                permission_mode="strict",
            )
        )
        self.payload(["config", "set", "claude_model", "claude-opus-4-1"])
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(stored["execution_enabled"], False)
        self.assertEqual(stored["permission_mode"], "strict")
        self.assertEqual(stored["claude_model"], "claude-opus-4-1")
        self.assertIsNone(stored["claude_effort"])

    # -- Finding 2: the full published effort vocabulary --------------------

    def test_all_five_efforts_round_trip_through_the_store(self) -> None:
        self.store.reset_all()
        for value in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=value):
                code, _ = self.payload(["config", "set", "claude_effort", value])
                self.assertEqual(code, EXIT_OK)
                _code, shown = self.payload(["config", "show"])
                self.assertEqual(shown["claude_effort"], value)

    def test_set_and_show_expose_the_selected_values(self) -> None:
        self.store.reset_all()
        code, _ = self.payload(["config", "set", "claude_model", "claude-opus-4-1"])
        self.assertEqual(code, EXIT_OK)
        code, _ = self.payload(["config", "set", "claude_effort", "medium"])
        self.assertEqual(code, EXIT_OK)
        code, shown = self.payload(["config", "show"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(shown["claude_model"], "claude-opus-4-1")
        self.assertEqual(shown["claude_effort"], "medium")
        # Unrelated settings semantics are untouched by the new keys.
        self.assertIs(shown["execution_enabled"], True)
        self.assertEqual(shown["permission_mode"], "session")
        self.assertIs(shown["paid_api_enabled"], False)
        self.assertIs(shown["paid_api_approval_id_present"], False)
        self.assertEqual(shown["claude_call_budget_usd"], 0.25)
        self.assertEqual(shown["approval_ttl_minutes"], 30)

    def test_reset_returns_selection_to_unset(self) -> None:
        self.store.reset_all()
        self.payload(["config", "set", "claude_model", "claude-sonnet-4-5"])
        self.payload(["config", "set", "claude_effort", "low"])
        code, shown = self.payload(["config", "reset", "claude_model"])
        self.assertEqual(code, EXIT_OK)
        self.assertIsNone(shown["claude_model"])
        # The untouched key survives the reset of its sibling.
        self.assertEqual(shown["claude_effort"], "low")

    def test_an_invalid_effort_is_refused_and_never_written(self) -> None:
        self.store.reset_all()
        before = self.path.read_bytes()
        for value in ("extreme", "HIGH", "Low", "low medium", "", "ultracode"):
            with self.subTest(value=value):
                code, shown = self.payload(["config", "set", "claude_effort", value])
                self.assertEqual(code, EXIT_POLICY)
                # The refusal is value-free and the store bytes stand.
                if value:
                    self.assertNotIn(value, json.dumps(shown))
                self.assertEqual(before, self.path.read_bytes())

    def test_invalid_model_values_are_refused_at_construction(self) -> None:
        for value in (
            "-p",              # flag-like
            "--model",         # flag-like
            "-",
            "a b",             # whitespace separator
            "a;b",             # shell separator
            "a|b",
            "a&b",
            "a\nb",            # control character
            "a\tb",
            "a\x00b",
            "a/b",             # path separator
            "a:b",
            "a'b",
            'a"b',
            "",                # empty
            "$HOME",
            "a" * 65,          # over the length bound
            7,                 # wrong type
            True,
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    Settings(claude_model=value)

    def test_an_invalid_model_is_refused_at_set_time_and_never_echoed(self) -> None:
        self.store.reset_all()
        before = self.path.read_bytes()
        # A leading dash is refused by the closed parser itself; the other
        # shapes reach the store and are refused by the shared grammar.
        for value in ("-p", "--model"):
            with self.subTest(value=value):
                code, _ = self.payload(["config", "set", "claude_model", value])
                self.assertNotEqual(EXIT_OK, code)
                self.assertEqual(before, self.path.read_bytes())
        for value in ("a;b", "a b"):
            with self.subTest(value=value):
                code, shown = self.payload(["config", "set", "claude_model", value])
                self.assertEqual(code, EXIT_POLICY)
                self.assertNotIn(value, json.dumps(shown))
                self.assertEqual(before, self.path.read_bytes())

    def test_an_invalid_effort_is_refused_at_construction(self) -> None:
        for value in ("extreme", "HIGH", "", "low medium", 7, True):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    Settings(claude_effort=value)

    def test_a_malformed_persisted_selection_invalidates_the_document(self) -> None:
        for overrides in (
            {"claude_model": 7},
            {"claude_model": "-p"},
            {"claude_model": "a b"},
            {"claude_effort": 7},
            {"claude_effort": "extreme"},
        ):
            with self.subTest(overrides=overrides):
                self.write(self.canonical(**overrides))
                self.assertIs(self.load().status, ConfigStatus.INVALID)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
