"""V2 Projects Gate 6H H1: the project folder link - context, never authority.

S1/S1r schema and rollback, S2 store, S3 concurrency, P1 path refusals,
P2 known folders, P3 name sanitising, P4 propose, K4 unlink never deletes,
A1-A4 the link widens no capability and reaches no model.

Temp directories only; no test opens the live DB, creates a folder outside
its own temp tree, or makes a live model, auth or network call.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from uuid import uuid4
from pathlib import Path
from unittest import mock

from jarvis import project_folders
from jarvis.app import EXIT_OK, create_app, project_folder_result
from jarvis.connectors import claude_code
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.connectors.local_files import LocalFileReadConnector, LocalFileWriteConnector
from jarvis.contracts import (
    LOCAL_READ_CAPABILITY,
    LOCAL_WRITE_CAPABILITY,
    ActionScope,
    DataClassification,
    ErrorCode,
    PermissionGrant,
    PermissionMode,
    RunStatus,
    SessionState,
    Task,
)
from jarvis.memory import COMPATIBILITY_SCHEMA_OBJECTS, SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import installed_cli, open_core, safe_cleanup
from tests.test_claude_connector import OAUTH_MAX_AUTH_STATUS, FakeProcess, patch_oauth_environment
from tests import test_conversation_store as store_tests
from tests.test_egress_attestation import CLAUDE_TUPLES, SUCCESS_CLI

WINDOWS = os.name == "nt"
BEFORE_6H = {
    "conversations", "idx_conversations_owner", "conversation_sessions", "conversation_legacy_imports",
    "conversation_moves", "conversation_deletions", "project_instructions", "idx_project_instructions_current",
    "project_files", "idx_project_files_active_sha", "project_file_chunks",
}
NEW_6H = {"project_folder_links", "idx_project_folder_links_current"}


def long_temp(case: unittest.TestCase) -> Path:
    """A temp directory under its LONG name (a CI TEMP may be 8.3-spelled)."""
    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    case.addCleanup(safe_cleanup, temp)
    return Path(os.path.realpath(temp.name))


def tree(root: Path) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    return sorted((str(d), tuple(sorted(ds)), tuple(sorted(fs))) for d, ds, fs in os.walk(root))


def junction(target: Path, link: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


class FolderCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = long_temp(self)
        self.database = self.root / "db" / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.settings = Settings(memory_database=self.database)
        self.p = self.memory.create_project("Alpha")
        self.q = self.memory.create_project("Beta")
        self.folders = self.root / "folders"
        self.folders.mkdir()

    def rows(self, sql: str) -> list[tuple]:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return [tuple(row) for row in connection.execute(sql).fetchall()]

    def snapshot(self) -> tuple[list[tuple], list[tuple]]:
        return (self.rows("SELECT * FROM project_folder_links ORDER BY rowid"),
                self.rows("SELECT * FROM projects ORDER BY rowid"))

    def folder(self, name: str) -> str:
        path = self.folders / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def app(self, action: str, project_id: str, **kwargs):
        return project_folder_result(self.memory, self.settings, action, project_id, **kwargs)


# --------------------------------------------------------------------------
# S1 / S1r schema and rollback
# --------------------------------------------------------------------------


class SchemaTests(FolderCase):
    def test_s1_schema_ten_and_exactly_the_two_new_objects(self) -> None:
        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual([(10,)], self.rows("SELECT version FROM schema_version"))
        self.assertEqual(list(range(1, 11)), [r[0] for r in self.rows("SELECT version FROM schema_migrations ORDER BY version")])
        self.assertEqual(NEW_6H, COMPATIBILITY_SCHEMA_OBJECTS - BEFORE_6H)
        self.assertLessEqual(BEFORE_6H, COMPATIBILITY_SCHEMA_OBJECTS)
        self.assertEqual(["id", "name", "active", "created_at"], [r[1] for r in self.rows("PRAGMA table_info(projects)")])
        self.assertEqual(["id", "project_id", "path", "origin", "linked_at", "unlinked_at"],
                         [r[1] for r in self.rows("PRAGMA table_info(project_folder_links)")])
        master = "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        before = self.rows(master)
        self.assertLessEqual(NEW_6H, {r[1] for r in before})
        SQLiteMemoryStore(self.database)                                          # idempotent reopen
        self.assertEqual(before, self.rows(master))
        self.assertEqual([(10,)], self.rows("SELECT version FROM schema_version"))


class RollbackTests(unittest.TestCase):
    """S1r: the REAL schema-10 release at ROLLBACK_COMMIT opens, reads and
    writes a V2 database holding a current and an unlinked folder link."""

    old = store_tests.RollbackReleaseCompatibilityTests.old

    def setUp(self) -> None:
        self.root = long_temp(self)
        self.database = self.root / "jarvis.db"
        known = subprocess.run(["git", "-C", str(store_tests.REPO), "cat-file", "-e",
                                f"{store_tests.ROLLBACK_COMMIT}^{{commit}}"],
                               capture_output=True)
        if known.returncode != 0:
            self.skipTest("the rollback release's commit is not in this clone")
        archive = subprocess.run(["git", "-C", str(store_tests.REPO), "archive", "--format=tar",
                                  store_tests.ROLLBACK_COMMIT, "jarvis"],
                                 capture_output=True, check=True).stdout
        self.old_tree = self.root / "old-release"
        self.old_tree.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(self.old_tree, filter="data")

    def links(self) -> list[tuple]:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT * FROM project_folder_links ORDER BY rowid").fetchall()

    def test_s1r_old_release_round_trip_keeps_links_and_the_index(self) -> None:
        memory = SQLiteMemoryStore(self.database)
        live = memory.create_project("Live")
        session = SessionState(objective="bound", project_id=live.id)
        memory.save_session(session)
        first, second = str(self.root) + "-linked-a", str(self.root) + "-linked-b"
        memory.link_explicit_user_project_folder(live.id, first, "picked")
        memory.unlink_project_folder(live.id)
        memory.link_explicit_user_project_folder(live.id, second, "picked")
        before, shown = self.links(), memory.load_project_folder(live.id)
        self.assertEqual(2, len(before))
        out = self.old(store_tests.RollbackReleaseCompatibilityTests.OLD_READS_V2_PROJECT_DATA, str(self.database), str(self.old_tree))
        self.assertEqual(10, out["schema"])
        self.old(store_tests.RollbackReleaseCompatibilityTests.OLD_OPERATIONS, str(self.database), session.id, str(self.old_tree))
        reopened = SQLiteMemoryStore(self.database)
        self.assertEqual(before, self.links())
        self.assertEqual(shown, reopened.load_project_folder(live.id))
        self.assertEqual(10, reopened.schema_version())
        with self.assertRaisesRegex(ValueError, "already has a linked folder"):
            reopened.link_explicit_user_project_folder(live.id, str(self.root) + "-linked-c", "picked")
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):                      # the partial index enforces
                connection.execute("INSERT INTO project_folder_links VALUES ('pfl-' || hex(randomblob(16)), ?, "
                                   "'D:\\x', 'picked', 'now', NULL)", (live.id,))
        self.assertEqual(before, self.links())


# --------------------------------------------------------------------------
# S2 store / S3 concurrency
# --------------------------------------------------------------------------


class StoreTests(FolderCase):
    def test_s2_link_show_unlink_relink(self) -> None:
        a, b = str(self.folders / "a"), str(self.folders / "b")
        self.assertIsNone(self.memory.load_project_folder(self.p.id))
        linked = self.memory.link_explicit_user_project_folder(self.p.id, a, "picked")
        self.assertEqual(linked, self.memory.load_project_folder(self.p.id))
        self.assertEqual({"path": a, "origin": "picked", "linked_at": linked.linked_at},
                         self.app("show", self.p.id).payload["folder"])
        before = self.rows("SELECT * FROM project_folder_links")
        self.memory.unlink_project_folder(self.p.id)
        after = self.rows("SELECT * FROM project_folder_links")
        self.assertEqual([r[:5] for r in before], [r[:5] for r in after])        # only unlinked_at changed
        self.assertIsNone(before[0][5])
        self.assertIsNotNone(after[0][5])
        self.assertIsNone(self.memory.load_project_folder(self.p.id))
        self.assertIsNone(self.app("show", self.p.id).payload["folder"])
        relinked = self.memory.link_explicit_user_project_folder(self.p.id, b, "picked")
        self.assertEqual(b, self.memory.load_project_folder(self.p.id).path)
        self.memory.unlink_project_folder(self.p.id)
        final = self.rows("SELECT * FROM project_folder_links ORDER BY rowid")
        self.assertEqual(after[0], final[0])                                      # an old row is never touched
        self.assertEqual(relinked.id, final[1][0])

    def test_s2_every_refusal_writes_nothing(self) -> None:
        a = str(self.folders / "a")
        self.memory.link_explicit_user_project_folder(self.q.id, a, "picked")
        archived = self.memory.create_project("Archived")
        self.memory.set_project_active(archived.id, False)
        lone = self.memory.create_project("Lone")
        cases = (
            ("second link while current", "link", self.q.id, str(self.folders / "z"), "picked", "already has a linked"),
            ("same folder as another project", "link", self.p.id, a, "picked", "linked to another project"),
            ("same folder other case", "link", self.p.id, a.upper(), "picked", "linked to another project"),
            ("nested folder", "link", self.p.id, a + "\\inner", "picked", "linked to another project"),
            ("containing folder", "link", self.p.id, str(self.folders), "picked", "linked to another project"),
            ("archived project", "link", archived.id, str(self.folders / "y"), "picked", "archived"),
            ("unknown project", "link", "p-" + "0" * 32, str(self.folders / "y"), "picked", "Unknown project"),
            ("invalid project id", "link", "../x", str(self.folders / "y"), "picked", "project_id"),
            ("reserved origin", "link", self.p.id, str(self.folders / "y"), "created", "origin"),
            ("lexical rule", "link", self.p.id, "relative\\y", "picked", "local drive path"),
            ("under the store's own data folder", "link", self.p.id, str(self.database.parent / "x"), "picked",
             "JARVIS data"),
            ("unlink without link", "unlink", lone.id, None, None, "No folder is linked"),
            ("unlink archived", "unlink", archived.id, None, None, "archived"),
        )
        for name, action, project_id, path, origin, message in cases:
            with self.subTest(name):
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, message):
                    if action == "link":
                        self.memory.link_explicit_user_project_folder(project_id, path, origin)
                    else:
                        self.memory.unlink_project_folder(project_id)
                self.assertEqual(before, self.snapshot())
        # A prefix that is not a parent is no overlap.
        self.memory.link_explicit_user_project_folder(self.p.id, a + "b", "picked")

    def test_s2_a_malformed_row_is_none(self) -> None:
        good = ("pfl-" + uuid4().hex, "D:\\work\\x", "picked", "2026-10-10T00:00:00Z")
        for name, (link_id, path, origin, linked_at) in {
            "bad id": ("pf-1", *good[1:]),
            "bad origin": (good[0], good[1], "evil", good[3]),
            "reserved origin": (good[0], good[1], "created", good[3]),
            "path not text": (good[0], b"D:\\work\\x", *good[2:]),                  # a BLOB keeps its type
            "path fails the lexical rules": (good[0], "D:\\a\\..\\b", *good[2:]),
            "path under the data folder": (good[0], str(self.database.parent / "x"), *good[2:]),
            "linked_at not text": (*good[:3], b"2026"),
        }.items():
            with self.subTest(name):
                link_id = link_id if name == "bad id" else "pfl-" + uuid4().hex
                project = self.memory.create_project(f"Malformed {name}")
                with contextlib.closing(sqlite3.connect(self.database)) as connection:
                    connection.execute("INSERT INTO project_folder_links VALUES (?, ?, ?, ?, ?, NULL)",
                                       (link_id, project.id, path, origin, linked_at))
                    connection.commit()
                self.assertIsNone(self.memory.load_project_folder(project.id))
                self.assertIsNone(self.app("show", project.id).payload["folder"])


class ConcurrencyTests(FolderCase):
    def race(self, first: str, second: str) -> list[str]:
        stores = (SQLiteMemoryStore(self.database), SQLiteMemoryStore(self.database))
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def link(store, project_id, path):
            barrier.wait()
            try:
                store.link_explicit_user_project_folder(project_id, path, "picked")
                outcomes.append("ok")
            except ValueError:
                outcomes.append("refused")

        threads = [threading.Thread(target=link, args=(stores[0], self.p.id, first)),
                   threading.Thread(target=link, args=(stores[1], self.q.id, second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return sorted(outcomes)

    def test_s3_two_stores_link_the_same_folder_exactly_one_wins(self) -> None:
        self.assertEqual(["ok", "refused"], self.race(str(self.folders / "a"), str(self.folders / "a")))
        self.assertEqual(1, len(self.rows("SELECT * FROM project_folder_links WHERE unlinked_at IS NULL")))

    def test_s3_two_stores_link_a_nested_folder_exactly_one_wins(self) -> None:
        self.assertEqual(["ok", "refused"], self.race(str(self.folders / "a"), str(self.folders / "a" / "in")))
        self.assertEqual(1, len(self.rows("SELECT * FROM project_folder_links WHERE unlinked_at IS NULL")))


# --------------------------------------------------------------------------
# P1 path refusals (validate_folder a-e), through the app's link action
# --------------------------------------------------------------------------


@unittest.skipUnless(WINDOWS, "Windows path rules")
class PathRefusalTests(FolderCase):
    def refuse(self, path, message: str, **patches) -> None:
        before_tree, before_db = tree(self.folders), self.snapshot()
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(project_folders, name, **value))
            result = self.app("link", self.p.id, path=path)
        self.assertNotEqual(EXIT_OK, result.exit_code)
        error = result.payload["error"]
        self.assertEqual(ErrorCode.INVALID_TASK.value, error["code"])
        self.assertIn(message, error["message"])
        self.assertEqual(before_tree, tree(self.folders))
        self.assertEqual(before_db, self.snapshot())

    def test_p1_raw_grammar(self) -> None:
        good = self.folder("good")
        home = str(Path.home())
        for name, path, message in (
            ("not a string", None, "non-blank string"),
            ("a number", 5, "non-blank string"),
            ("blank", "   ", "non-blank string"),
            ("longer than 512", "C:\\" + "a" * 600, "longer than 512"),
            ("NUL", good + "\x00", "control or bidi"),
            ("control", good + "\x07", "control or bidi"),
            ("bidi override", good + "\u202e", "control or bidi"),
            ("zero-width format character", good + "\u200b", "control or bidi"),
            ("relative", "folders\\good", "local drive path"),
            ("drive relative", "C:foo", "local drive path"),
            ("rooted without drive", "\\foo", "local drive path"),
            ("UNC", "\\\\server\\share\\x", "local drive path"),
            ("extended prefix", "\\\\?\\" + good, "local drive path"),
            ("device prefix", "\\\\.\\" + good, "local drive path"),
            ("NT prefix", "\\??\\" + good, "local drive path"),
            ("forward slashes", good.replace("\\", "/"), "local drive path"),
            ("one forward slash", good[:3] + good[3:].replace("\\", "/", 1), "local drive path"),
            ("dot-dot that normalizes to an allowed path", good + "\\..\\good", "normal form"),
            ("dot that normalizes to an allowed path", str(self.folders) + "\\.\\good", "normal form"),
            ("doubled separator", str(self.folders) + "\\\\good", "normal form"),
            ("trailing separator", good + "\\", "normal form"),
            ("ADS", "D:\\x\\a:b", "< > :"),
            ("less than", good + "<x", "< > :"),
            ("pipe", good + "|x", "< > :"),
            ("question mark", good + "?", "< > :"),
            ("star", good + "*", "< > :"),
            ("quote", good + '"', "< > :"),
            ("device name", "D:\\x\\CON", "device name"),
            ("device name with extension", "D:\\x\\con.txt", "device name"),
            ("device name com9", "D:\\x\\com9.log", "device name"),
            ("device name superscript", "D:\\x\\LPT\u00b9", "device name"),
            ("device name conin", "D:\\x\\CONIN$", "device name"),
            ("device name nul space", "D:\\x\\nul .txt", "device name"),
            ("trailing dot", good + ".", "dot or a space"),
            ("trailing space", good + " ", "dot or a space"),
            ("8.3 short name", home[:3] + "PROGRA~1", "short (8.3)"),
        ):
            with self.subTest(name):
                self.refuse(path, message)

    def test_p1_lexical_location(self) -> None:
        home = Path.home()
        system_root = os.environ.get("SystemRoot", "C:\\Windows")
        for name, path, message in (
            ("drive root", "C:\\", "drive root"),
            ("profile root", str(home), "profile folder"),
            ("folder containing the profile root", str(home.parent), "profile folder"),
            ("Windows", system_root + "\\System32", "Windows or program"),
            ("Program Files", "C:\\Program Files\\x", "Windows or program"),
            ("Program Files (x86)", "C:\\Program Files (x86)\\x", "Windows or program"),
            ("ProgramData", "C:\\ProgramData\\x", "Windows or program"),
            ("store data folder", str(self.database.parent / "x"), "JARVIS data"),
            ("settings data folder", str(self.database.parent), "JARVIS data"),
            (".jarvis", str(home / ".jarvis" / "x"), "JARVIS data"),
            ("install root", str(home / "Apps" / "JARVIS" / "x"), "JARVIS data"),
            ("backups", str(home / "JARVIS-backups" / "x"), "JARVIS data"),
        ):
            with self.subTest(name):
                self.refuse(path, message)
        # A caller-given protected entry, and the settings DB as its own entry.
        other = self.folder("protected-by-caller")
        with self.assertRaisesRegex(ValueError, "JARVIS data"):
            project_folders.validate_folder(other + "\\x", protected=(other,))
        settings_db = self.root / "settings-data" / "jarvis.db"
        self.settings = Settings(memory_database=settings_db)
        self.refuse(str(settings_db.parent / "x"), "JARVIS data")
        # The data folder configured through a junction still protects the real one.
        real = self.folder("realdata\\x")
        junction(self.folders / "realdata", self.root / "datalink")
        self.settings = Settings(memory_database=self.root / "datalink" / "jarvis.db")
        self.refuse(real, "JARVIS data")

    def test_p1_volume_must_be_fixed(self) -> None:
        good = self.folder("good")
        for name, value in (("removable", {"return_value": 2}), ("network", {"return_value": 4}),
                            ("cd", {"return_value": 5}), ("ram", {"return_value": 6}),
                            ("unknown", {"return_value": 0}), ("unanswered", {"side_effect": OSError("x")})):
            with self.subTest(name):
                self.refuse(good, "local fixed drive", _drive_type=value)

    def test_p1_chain_scan(self) -> None:
        target = self.folder("target")
        Path(target, "sub").mkdir()
        Path(self.folders, "file.txt").write_text("x", encoding="utf-8")
        junction(Path(target), self.folders / "j")
        for name, path, message in (
            ("missing leaf", str(self.folders / "missing"), "does not exist"),
            ("missing component", str(self.folders / "missing" / "leaf"), "does not exist"),
            ("a file", str(self.folders / "file.txt"), "not a folder"),
            ("junction in the chain", str(self.folders / "j" / "sub"), "link or junction"),
            ("junction as the leaf", str(self.folders / "j"), "link or junction"),
        ):
            with self.subTest(name):
                self.refuse(path, message)

    def test_p1_handle_proof(self) -> None:
        good = self.folder("good")
        alias = "Z:" + good[2:]
        for name, value in (("SUBST-style alias", {"return_value": alias}),
                            ("swapped-in junction", {"return_value": str(self.root / "elsewhere")}),
                            ("unanswered", {"return_value": None})):
            with self.subTest(name):
                self.refuse(good, "proven", _final_path=value)

    def test_p1_a_good_folder_links_with_its_proven_path(self) -> None:
        good = self.folder("İyi Klasör")
        result = self.app("link", self.p.id, path=good)
        self.assertEqual(EXIT_OK, result.exit_code, result.payload)
        self.assertEqual(good.casefold(), result.payload["folder"]["path"].casefold())
        self.assertEqual("picked", self.memory.load_project_folder(self.p.id).origin)


# --------------------------------------------------------------------------
# P2 known folders / P3 sanitising / P4 propose
# --------------------------------------------------------------------------


@unittest.skipUnless(WINDOWS, "Windows known folders")
class KnownFolderTests(FolderCase):
    def test_p2_a_onedrive_documents_folder_is_used_as_given(self) -> None:
        documents = self.folder("OneDrive\\Belgeler")
        with mock.patch.object(project_folders, "_shell_known_folder", return_value=documents) as api:
            self.assertEqual(documents, project_folders.known_folder("documents", protected=()))
            self.assertEqual(documents + "\\JARVIS Projeleri\\Alpha",
                             project_folders.propose_folder("Alpha", False, protected=()))
        self.assertEqual(project_folders._KNOWN_FOLDERS["documents"], api.call_args.args[0])

    def test_p2_an_api_failure_refuses_with_no_fallback(self) -> None:
        for name, value in (("no answer", {"return_value": None}), ("empty", {"return_value": ""}),
                            ("raises", {"side_effect": OSError("E_FAIL")})):
            with self.subTest(name):
                with mock.patch.object(project_folders, "_shell_known_folder", **value), \
                        mock.patch.object(Path, "home", side_effect=AssertionError("Path.home fallback")), \
                        mock.patch.dict(os.environ, {"USERPROFILE": "Z:\\no-fallback"}):
                    for kind in ("desktop", "documents"):
                        with self.assertRaisesRegex(ValueError, "bulunamadı"):
                            project_folders.known_folder(kind, protected=())
                with mock.patch.object(project_folders, "_shell_known_folder", **value):
                    result = self.app("propose", self.p.id)
                self.assertEqual(ErrorCode.INVALID_TASK.value, result.payload["error"]["code"])
                self.assertNotIn("path", result.payload)
        with self.assertRaisesRegex(ValueError, "Unknown known folder"):
            project_folders.known_folder("downloads", protected=())

    def test_p2_a_known_folder_must_pass_validation(self) -> None:
        with mock.patch.object(project_folders, "_shell_known_folder", return_value=str(self.database.parent)):
            with self.assertRaisesRegex(ValueError, "JARVIS data"):
                project_folders.known_folder("desktop", protected=(self.database.parent,))

    def test_p2_one_real_local_call(self) -> None:
        found = project_folders.known_folder("desktop", protected=())
        self.assertTrue(os.path.isabs(found))
        self.assertTrue(os.path.isdir(found))


class SanitizeTests(unittest.TestCase):
    def test_p3_table(self) -> None:
        for raw, expected in (
            ("CON", "_CON"),
            ("com1.txt", "_com1.txt"),
            ("LPT\u00b2", "_LPT\u00b2"),
            ("a/b:c", "a-b-c"),
            ('x<y>z"|?*\\', "x-y-z-----"),
            ("x. ", "x"),
            ("  çok   boşluk  ", "çok boşluk"),
            ("   ", "Proje"),
            ("...", "Proje"),
            ("a\u202eb\x07c\u200b\ufeff", "abc"),
            ("x" * 300, "x" * 80),
            ("Şükrü'nün Çalışma Ğöğü ı İ", "Şükrü'nün Çalışma Ğöğü ı İ"),
            ("Ver~1", "Ver-1"),
            (None, "Proje"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(expected, project_folders.sanitize_folder_name(raw))
        self.assertEqual("ü", project_folders.sanitize_folder_name("u\u0308"))             # NFC


@unittest.skipUnless(WINDOWS, "Windows known folders")
class ProposeTests(FolderCase):
    def setUp(self) -> None:
        super().setUp()
        self.desktop = self.folder("Desktop")
        self.documents = self.folder("Documents")
        known = {project_folders._KNOWN_FOLDERS["desktop"]: self.desktop,
                 project_folders._KNOWN_FOLDERS["documents"]: self.documents}
        patcher = mock.patch.object(project_folders, "_shell_known_folder", side_effect=known.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def propose(self, name: str, on_desktop: bool) -> str:
        return project_folders.propose_folder(name, on_desktop, protected=())

    def test_p4_collision_yields_2_and_99_taken_refuses(self) -> None:
        self.assertEqual(self.desktop + "\\Alpha", self.propose("Alpha", True))
        Path(self.desktop, "Alpha").mkdir()
        self.assertEqual(self.desktop + "\\Alpha (2)", self.propose("Alpha", True))
        Path(self.desktop, "Alpha (2)").write_text("a file also takes the name", encoding="utf-8")
        self.assertEqual(self.desktop + "\\Alpha (3)", self.propose("Alpha", True))
        for number in range(3, 100):
            Path(self.desktop, f"Alpha ({number})").mkdir()
        before = tree(self.folders)
        with self.assertRaisesRegex(ValueError, "No free folder name"):
            self.propose("Alpha", True)
        self.assertEqual(before, tree(self.folders))

    def test_p4_propose_creates_nothing(self) -> None:
        Path(self.documents, "JARVIS Projeleri").mkdir()
        for on_desktop in (True, False):
            with self.subTest(on_desktop=on_desktop):
                before = tree(self.folders)
                proposed = self.app("propose", self.p.id, on_desktop=on_desktop)
                self.assertEqual(EXIT_OK, proposed.exit_code, proposed.payload)
                parent = self.desktop if on_desktop else self.documents + "\\JARVIS Projeleri"
                self.assertEqual(parent + "\\Alpha", proposed.payload["path"])
                self.assertEqual(before, tree(self.folders))

    @contextlib.contextmanager
    def only_lstat(self):
        """Spy os.lstat; every other way to look at a path fails the test."""
        with contextlib.ExitStack() as stack:
            for owner, name in ((os, "stat"), (os.path, "exists"), (os.path, "isdir"), (os.path, "lexists"),
                                (Path, "exists"), (Path, "is_dir"), (Path, "stat")):
                stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError(f"{name} probe")))
            yield stack.enter_context(mock.patch.object(project_folders.os, "lstat", side_effect=os.lstat))

    def test_p4_a_junction_jarvis_projeleri_is_refused_with_no_child_lookup(self) -> None:
        elsewhere = self.folder("elsewhere")
        parent = Path(self.documents, "JARVIS Projeleri")
        junction(Path(elsewhere), parent)
        with self.only_lstat() as spy:
            with self.assertRaisesRegex(ValueError, "link or junction"):
                self.propose("Alpha", False)
        beneath = [c.args[0] for c in spy.call_args_list if str(c.args[0]).casefold().startswith(str(parent).casefold() + "\\")]
        self.assertEqual([], beneath)

    def test_p4_an_absent_jarvis_projeleri_yields_its_child_with_no_lookup_beneath(self) -> None:
        parent = self.documents + "\\JARVIS Projeleri"
        with self.only_lstat() as spy:
            self.assertEqual(parent + "\\Alpha", self.propose("Alpha", False))
        beneath = [c.args[0] for c in spy.call_args_list if str(c.args[0]).casefold().startswith(parent.casefold() + "\\")]
        self.assertEqual([], beneath)
        self.assertFalse(os.path.lexists(parent))

    def test_p4_unknown_or_archived_project_and_a_bad_flag_are_refused(self) -> None:
        archived = self.memory.create_project("Archived")
        self.memory.set_project_active(archived.id, False)
        for project_id, kwargs, message in ((archived.id, {}, "archived"), ("p-" + "0" * 32, {}, "Unknown project"),
                                            (self.p.id, {"on_desktop": "yes"}, "on_desktop")):
            with self.subTest(project_id=project_id, **kwargs):
                result = self.app("propose", project_id, **kwargs)
                self.assertEqual(ErrorCode.INVALID_TASK.value, result.payload["error"]["code"])
                self.assertIn(message, result.payload["error"]["message"])


# --------------------------------------------------------------------------
# K4 unlink never deletes
# --------------------------------------------------------------------------


@unittest.skipUnless(WINDOWS, "Windows path rules")
class UnlinkNeverDeletesTests(FolderCase):
    def test_k4_unlink_touches_no_folder(self) -> None:
        folder = Path(self.folder("Kept"))
        (folder / "note.txt").write_text("keep me", encoding="utf-8")
        self.assertEqual(EXIT_OK, self.app("link", self.p.id, path=str(folder)).exit_code)
        before = tree(self.folders)
        with contextlib.ExitStack() as stack:
            for owner, name in ((os, "rmdir"), (os, "remove"), (os, "unlink"), (os, "rename"), (os, "replace"),
                                (shutil, "rmtree"), (shutil, "move"), (Path, "rmdir"), (Path, "unlink")):
                stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError(f"{name} called")))
            result = self.app("unlink", self.p.id)
        self.assertEqual(EXIT_OK, result.exit_code, result.payload)
        self.assertIsNone(result.payload["folder"])
        self.assertEqual("keep me", (folder / "note.txt").read_text(encoding="utf-8"))
        self.assertEqual(before, tree(self.folders))
        self.assertIsNone(self.memory.load_project_folder(self.p.id))


# --------------------------------------------------------------------------
# A1-A4 context, never authority
# --------------------------------------------------------------------------


def outside_folder(case: FolderCase) -> Path:
    """F: a real folder OUTSIDE the startup root R, with x.txt in it."""
    folder = long_temp(case) / "Gizli Klasör 6H"
    folder.mkdir()
    (folder / "x.txt").write_text("outside\n", encoding="utf-8")
    return folder


@unittest.skipUnless(WINDOWS, "Windows path rules")
class LocalFileAuthorityTests(FolderCase):
    def test_a1_read_and_write_into_a_linked_folder_are_refused_exactly_as_unlinked(self) -> None:
        startup = self.folder("startup-root")
        folder = outside_folder(self)
        app = create_app(
            settings=Settings(memory_database=self.database, permission_mode=PermissionMode.SESSION),
            memory=self.memory,
            connectors=(LocalFileReadConnector(allowed_roots=(Path(startup),), default_working_directory=Path(startup)),
                        LocalFileWriteConnector(allowed_roots=(Path(startup),), default_working_directory=Path(startup))),
        )
        self.assertEqual(EXIT_OK, self.app("link", self.q.id, path=str(folder)).exit_code)
        target = str(folder / "x.txt")

        def run(project_id: str, capability: str, action: str):
            task = Task(objective="use the project folder", capability=capability,
                        scope=ActionScope(action=action, target=target, data_classification=DataClassification.INTERNAL))
            if capability == LOCAL_WRITE_CAPABILITY:
                task.write_content, task.write_mode = "changed\n", "create"
            task.project_id = project_id
            outcome = app.core.run(task)
            error = outcome.error
            return outcome.status, error and error.code, error and error.message

        for capability, action in ((LOCAL_READ_CAPABILITY, "read"), (LOCAL_WRITE_CAPABILITY, "write")):
            with self.subTest(capability):
                unlinked, linked = run(self.p.id, capability, action), run(self.q.id, capability, action)
                self.assertEqual(unlinked, linked)
                self.assertNotIn(linked[0], (RunStatus.COMPLETED, RunStatus.AWAITING_APPROVAL))
        self.assertEqual("outside\n", (folder / "x.txt").read_text(encoding="utf-8"))


@unittest.skipUnless(WINDOWS, "Windows path rules")
class ClaudeAuthorityTests(FolderCase):
    def setUp(self) -> None:
        super().setUp()
        self.startup = Path(self.folder("startup-root"))
        self.connector = ClaudeCodeConnector(allowed_roots=(self.startup,), default_working_directory=self.startup,
                                             max_budget_usd=0.15)
        registry = ToolRegistry()
        registry.register(self.connector)
        self.core = open_core(registry, self.memory, self.settings, destination_policy=CLAUDE_TUPLES)
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)
        for patcher in (
            mock.patch.object(ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS),
            mock.patch.object(claude_code, "resolve_trusted_executable", return_value=installed_cli(self)),
            mock.patch.object(claude_code.subprocess, "run", side_effect=AssertionError("subprocess.run")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_and_execute(self, project_id: str) -> dict:
        task = Task(objective="Summarize the module layout", capability="code_planning",
                    scope=ActionScope(action="analyze", target=str(self.startup), destination="anthropic/claude-code",
                                      data_classification=DataClassification.INTERNAL),
                    context={"working_directory": str(self.startup)})
        task.project_id = project_id
        waiting = self.core.run(task)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status, waiting.error)
        request = waiting.permission_request
        with mock.patch.object(claude_code.subprocess, "Popen", return_value=FakeProcess(SUCCESS_CLI)) as popen:
            done = self.core.resume(waiting.session.id, task.id, PermissionGrant.for_request(request))
        self.assertEqual(RunStatus.COMPLETED, done.status, done.error)
        command = popen.call_args.args[0]
        prompt = command[command.index("-p") + 1]
        return {"attestation": request.attestation.to_dict(), "preview": request.outbound_preview.to_dict(),
                "prompt": prompt, "system": command[command.index("--system-prompt") + 1],
                "payload": json.loads(prompt[len(self.connector.prompt_prefix):])}

    def test_a2_a3_attestation_payload_and_preview_identical_and_free_of_the_folder(self) -> None:
        folder = outside_folder(self)
        unlinked = self.run_and_execute(self.p.id)
        self.assertEqual(EXIT_OK, self.app("link", self.q.id, path=str(folder)).exit_code)
        linked = self.run_and_execute(self.q.id)
        for key in ("allowed_roots", "working_directory", "outbound_digest"):           # A2
            self.assertEqual(unlinked["attestation"][key], linked["attestation"][key], key)
        self.assertEqual(unlinked["attestation"], linked["attestation"])
        self.assertEqual(unlinked["payload"], linked["payload"])                           # A3
        self.assertEqual(unlinked["prompt"], linked["prompt"])
        self.assertEqual(unlinked["preview"], linked["preview"])
        self.assertEqual(unlinked["system"], linked["system"])
        rendered = json.dumps(linked, ensure_ascii=False).casefold()
        for secret in (str(folder), str(folder).replace("\\", "\\\\"), str(folder).replace("\\", "/"), folder.name):
            self.assertNotIn(secret.casefold(), rendered)


class SourceAuditTests(unittest.TestCase):
    def test_a4_only_the_store_the_app_and_the_module_name_the_link(self) -> None:
        root = Path(__file__).resolve().parents[1] / "jarvis"
        sources = {str(p.relative_to(root)).replace("\\", "/"): p.read_text(encoding="utf-8", errors="replace")
                   for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".qml", ".js")}
        allowed = {"memory.py", "app.py", "project_folders.py"}
        for needle in ("project_folder_links", "link_explicit_user_project_folder", "load_project_folder",
                       "project_folders"):
            with self.subTest(needle):
                self.assertLessEqual({name for name, text in sources.items() if needle in text}, allowed)
        callers = {name: text.count("link_explicit_user_project_folder(")
                   - text.count("def link_explicit_user_project_folder(") for name, text in sources.items()}
        self.assertEqual({"app.py": 1}, {name: count for name, count in callers.items() if count})
        for writer in ("INSERT INTO project_folder_links", "UPDATE project_folder_links"):
            self.assertEqual(["memory.py"], sorted(name for name, text in sources.items() if writer in text))
        self.assertNotIn("DELETE FROM project_folder_links", sources["memory.py"])


if __name__ == "__main__":
    unittest.main()
