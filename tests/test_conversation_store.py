"""The V2 conversation persistence foundation and its rollback compatibility.

Conversations move into the Core SQLite store as an ADDITIVE compatibility
layer: new isolated tables, created on every open, never a numbered
migration - because releases built for schema 10 are kept as rollback targets,
share the database, and refuse any ``schema_version`` above 10.

What is pinned here:

* the numbered schema is untouched (``SCHEMA_VERSION`` 10, migrations 1..10,
  schema-10 tables byte-identical), and the layer is idempotent;
* one consistent pre-V2 backup of a real database, made once, never
  overwritten;
* conversations reuse ``conversation.py``'s state and integrity semantics and
  fail closed on anything that does not verify;
* ownership (Global = ``project_id`` NULL, or one existing active project) is
  fixed at creation, and sessions link to one conversation permanently;
* the legacy ``conversation.json`` is only ever READ, and is imported
  idempotently by content digest - including a rollback-era rewrite by an old
  release, which becomes one more recovered Global conversation;
* the real 4f5a93d schema-10 storage code, extracted from git into a
  throwaway directory, opens and uses a V2 database without refusing it and
  without disturbing any V2 row.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from jarvis.contracts import ProjectRecord, SessionState
from jarvis.conversation import (
    ConversationState,
    ConversationStore,
    CorruptConversation,
    append_turn,
    encode_state,
    load_state,
)
from jarvis.memory import (
    COMPATIBILITY_SCHEMA_OBJECTS,
    SCHEMA_VERSION,
    SQLiteMemoryStore,
    pre_v2_backup_path,
)
from tests.helpers import safe_cleanup

REPO = Path(__file__).resolve().parents[1]
#: The retained pre-Chrome rollback release's source commit (schema 10).
ROLLBACK_COMMIT = "4f5a93dca5ee63137733d6a20adae5016350eb45"

#: The schema-10 tables exactly as schema-10 code defines them.
SCHEMA_TEN_COLUMNS = {
    "sessions": ["id", "state_json", "updated_at", "project_id"],
    "projects": ["id", "name", "active", "created_at"],
    "tasks": ["id", "session_id", "state_json", "status", "stage", "updated_at"],
}


def say(state: ConversationState, text: str, key: str) -> ConversationState:
    return append_turn(state, role="user", kind="user", text=text, message=text, key=key)[0]


def objects(database: Path) -> dict[str, str]:
    connection = sqlite3.connect(database)
    try:
        return dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchall()
        )
    finally:
        connection.close()


def rows(database: Path, sql: str, params: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.database = self.root / "jarvis.db"
        self.legacy = self.root / "conversation.json"

    def store(self) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(self.database)

    def schema_ten_database(self) -> None:
        """A real schema-10 database WITHOUT the compatibility layer, holding
        data: what an existing installation looks like before V2 opens it."""
        store = self.store()
        store.save_project(ProjectRecord(id="alpha-proj", name="Alpha"))
        store.save_session(SessionState(objective="v1 work", project_id="alpha-proj"))
        connection = sqlite3.connect(self.database)
        try:
            for name in ("project_instructions", "conversation_legacy_imports", "conversation_sessions", "conversations"):
                connection.execute(f"DROP TABLE {name}")
            connection.commit()
        finally:
            connection.close()
        pre_v2_backup_path(self.database).unlink(missing_ok=True)

    def legacy_file(self, *texts: str) -> None:
        store = ConversationStore(self.legacy)
        for number, text in enumerate(texts):
            store.append(role="user", kind="user", text=text, message=text, key=f"legacy-{number}-{text}")


class NumberedSchemaUntouchedTests(StoreCase):
    def test_the_schema_version_stays_ten_with_historical_migrations(self) -> None:
        store = self.store()
        self.store()
        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(10, store.schema_version())
        self.assertEqual(
            list(range(1, 11)),
            [row[0] for row in rows(self.database, "SELECT version FROM schema_migrations ORDER BY version")],
        )
        self.assertEqual([(10,)], rows(self.database, "SELECT version FROM schema_version"))

    def test_the_layer_is_created_and_is_idempotent(self) -> None:
        self.store()
        first = objects(self.database)
        self.assertLessEqual(COMPATIBILITY_SCHEMA_OBJECTS, set(first))
        self.store()
        self.store()
        self.assertEqual(first, objects(self.database))

    def test_schema_ten_tables_keep_their_exact_columns(self) -> None:
        self.store()
        for table, columns in SCHEMA_TEN_COLUMNS.items():
            with self.subTest(table=table):
                self.assertEqual(
                    columns, [row[1] for row in rows(self.database, f"PRAGMA table_info({table})")]
                )

    def test_an_existing_schema_ten_database_gains_only_the_layer(self) -> None:
        self.schema_ten_database()
        before = objects(self.database)
        self.store()
        after = objects(self.database)
        self.assertEqual(COMPATIBILITY_SCHEMA_OBJECTS, set(after) - set(before))
        for name, sql in before.items():
            self.assertEqual(sql, after[name], name)
        self.assertEqual(1, len(rows(self.database, "SELECT id FROM sessions")))

    def test_the_session_link_has_no_foreign_key_to_sessions(self) -> None:
        """Schema-10 code must be able to write sessions knowing nothing of it."""
        self.store()
        targets = {row[2] for row in rows(self.database, "PRAGMA foreign_key_list(conversation_sessions)")}
        self.assertEqual({"conversations"}, targets)


class PreV2BackupTests(StoreCase):
    def test_a_brand_new_database_needs_no_backup(self) -> None:
        self.store()
        self.assertFalse(pre_v2_backup_path(self.database).exists())

    def test_an_existing_database_is_backed_up_once_before_the_layer(self) -> None:
        self.schema_ten_database()
        backup = pre_v2_backup_path(self.database)
        self.assertEqual("jarvis.pre-v2.db", backup.name)
        self.store()
        self.assertTrue(backup.exists())
        # A consistent, standalone copy of the PRE-V2 state.
        self.assertEqual([("ok",)], rows(backup, "PRAGMA integrity_check"))
        tables = {row[0] for row in rows(backup, "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("sessions", tables)
        self.assertFalse(COMPATIBILITY_SCHEMA_OBJECTS & tables)
        self.assertEqual(1, len(rows(backup, "SELECT id FROM sessions")))
        self.assertEqual([("delete",)], rows(backup, "PRAGMA journal_mode"))
        # No temporary copy is left behind.
        self.assertEqual([], [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")])
        # Later opens copy nothing: the layer is established.
        stamp = backup.stat().st_mtime_ns
        self.store()
        self.assertEqual(stamp, backup.stat().st_mtime_ns)

    def test_an_existing_backup_is_never_overwritten(self) -> None:
        self.schema_ten_database()
        backup = pre_v2_backup_path(self.database)
        backup.write_bytes(b"an earlier pre-V2 copy")
        self.store()
        self.assertEqual(b"an earlier pre-V2 copy", backup.read_bytes())
        self.assertLessEqual(COMPATIBILITY_SCHEMA_OBJECTS, set(objects(self.database)))


class ConversationStoreTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.memory = self.store()
        self.memory.save_project(ProjectRecord(id="alpha-proj", name="Alpha"))

    def test_ids_are_opaque_and_ownership_is_global_or_one_project(self) -> None:
        global_one = self.memory.create_conversation()
        owned = self.memory.create_conversation("alpha-proj", title="Plan")
        for record in (global_one, owned):
            self.assertRegex(record.id, r"^c-[0-9a-f]{32}$")
        self.assertNotEqual(global_one.id, owned.id)
        self.assertIsNone(global_one.project_id)
        self.assertEqual("alpha-proj", owned.project_id)
        self.assertEqual("jarvis", owned.origin)
        self.assertEqual(ConversationState(), self.memory.load_conversation_state(owned.id))

    def test_no_project_is_ever_created_implicitly(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.create_conversation("ghost-proj")
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("INSERT INTO projects (id, name, active, created_at) VALUES ('old-proj', 'Old', 0, 'x')")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            self.memory.create_conversation("old-proj")
        with self.assertRaises(ValueError):
            self.memory.create_conversation("Not A Slug")
        self.assertEqual(["alpha-proj", "old-proj"], [p.id for p in self.memory.list_projects()])
        self.assertEqual([], self.memory.list_conversations())

    def test_titles_are_bounded(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.create_conversation(title="x" * 121)

    def test_listing_filters_by_owner_and_hides_archived(self) -> None:
        g = self.memory.create_conversation()
        a = self.memory.create_conversation("alpha-proj")
        self.assertEqual({g.id, a.id}, {r.id for r in self.memory.list_conversations()})
        self.assertEqual([g.id], [r.id for r in self.memory.list_conversations(project_id=None)])
        self.assertEqual([a.id], [r.id for r in self.memory.list_conversations(project_id="alpha-proj")])
        archived = self.memory.archive_conversation(a.id)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual([g.id], [r.id for r in self.memory.list_conversations()])
        self.assertEqual({g.id, a.id}, {r.id for r in self.memory.list_conversations(include_archived=True)})
        # Idempotent: the first archive time stays.
        self.assertEqual(archived.archived_at, self.memory.archive_conversation(a.id).archived_at)

    def test_state_round_trips_through_the_conversation_semantics(self) -> None:
        record = self.memory.create_conversation()
        self.memory.update_conversation_state(record.id, lambda s: say(s, "Karar: Postgres kullan.", "k1"))
        self.memory.update_conversation_state(record.id, lambda s: say(s, "Karar: Postgres kullan.", "k1"))
        state = self.memory.load_conversation_state(record.id)
        self.assertEqual(1, len(state.turns))           # the idempotency key held
        self.assertEqual("Karar: Postgres kullan.", state.turns[0].message)
        self.assertTrue(state.turns[0].id.startswith("t1-"))
        self.assertTrue(state.active_pins)              # pin semantics preserved

    def test_an_archived_conversation_is_read_only(self) -> None:
        record = self.memory.create_conversation()
        self.memory.archive_conversation(record.id)
        with self.assertRaises(ValueError):
            self.memory.update_conversation_state(record.id, lambda s: say(s, "late", "k"))
        self.assertEqual(ConversationState(), self.memory.load_conversation_state(record.id))

    def test_a_row_that_does_not_verify_fails_closed_and_is_left_alone(self) -> None:
        record = self.memory.create_conversation()
        self.memory.update_conversation_state(record.id, lambda s: say(s, "original", "k1"))
        tampered = json.dumps({"forged": True})
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("UPDATE conversations SET state_json = ? WHERE id = ?", (tampered, record.id))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(CorruptConversation):
            self.memory.load_conversation_state(record.id)
        with self.assertRaises(CorruptConversation):
            self.memory.update_conversation_state(record.id, lambda s: say(s, "more", "k2"))
        self.assertEqual(
            [(tampered,)], rows(self.database, "SELECT state_json FROM conversations WHERE id = ?", (record.id,))
        )

    def test_a_row_this_build_could_not_have_written_is_never_listed(self) -> None:
        state_json, integrity = encode_state(ConversationState())
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO conversations (id, project_id, title, state_json, integrity, origin, created_at, updated_at)"
                " VALUES ('not-an-id', NULL, '', ?, ?, 'jarvis', 'a', 'a')",
                (state_json, integrity),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual([], self.memory.list_conversations())
        with self.assertRaises(ValueError):
            self.memory.load_conversation("not-an-id")

    def test_ownership_cannot_be_moved(self) -> None:
        for name in ("move_conversation", "set_conversation_project", "delete_conversation"):
            self.assertFalse(hasattr(self.memory, name), name)

    def test_a_session_links_to_one_conversation_permanently(self) -> None:
        conversation = self.memory.create_conversation()
        other = self.memory.create_conversation()
        session = SessionState(objective="chat")
        self.memory.save_session(session)
        self.assertIsNone(self.memory.conversation_for_session(session.id))
        self.memory.link_session_conversation(session.id, conversation.id)
        self.memory.link_session_conversation(session.id, conversation.id)   # same link: no-op
        with self.assertRaises(ValueError):
            self.memory.link_session_conversation(session.id, other.id)
        self.assertEqual(conversation.id, self.memory.conversation_for_session(session.id))
        with self.assertRaises(KeyError):
            self.memory.link_session_conversation("no-such-session", conversation.id)

    def test_a_project_bound_session_only_joins_its_own_project(self) -> None:
        bound = SessionState(objective="alpha", project_id="alpha-proj")
        self.memory.save_session(bound)
        with self.assertRaises(ValueError):
            self.memory.link_session_conversation(bound.id, self.memory.create_conversation().id)
        owned = self.memory.create_conversation("alpha-proj")
        self.memory.link_session_conversation(bound.id, owned.id)
        # An unbound session may join a project conversation (it binds later).
        free = SessionState(objective="free")
        self.memory.save_session(free)
        self.memory.link_session_conversation(free.id, owned.id)

    def test_an_archived_conversation_takes_no_new_session(self) -> None:
        record = self.memory.create_conversation()
        self.memory.archive_conversation(record.id)
        session = SessionState(objective="late")
        self.memory.save_session(session)
        with self.assertRaises(ValueError):
            self.memory.link_session_conversation(session.id, record.id)


class LegacyImportTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.memory = self.store()

    def test_an_absent_file_imports_nothing(self) -> None:
        self.assertIsNone(self.memory.import_legacy_conversation(self.legacy))
        self.assertEqual([], self.memory.list_conversations())

    def test_a_valid_file_becomes_one_global_conversation_and_is_never_touched(self) -> None:
        self.legacy_file("ilk", "ikinci")
        original = load_state(self.legacy)
        before = (sha(self.legacy), self.legacy.stat().st_mtime_ns)
        listing = sorted(p.name for p in self.root.iterdir() if not p.name.startswith("jarvis.db"))
        record = self.memory.import_legacy_conversation(self.legacy)
        self.assertIsNotNone(record)
        self.assertIsNone(record.project_id)
        self.assertEqual("legacy-import", record.origin)
        self.assertEqual("", record.title)             # storage never depends on UI locale
        imported = self.memory.load_conversation_state(record.id)
        self.assertEqual(original, imported)            # ids, pins, digests, titles preserved
        self.assertIsNone(self.memory.import_legacy_conversation(self.legacy))
        self.assertIsNone(self.store().import_legacy_conversation(self.legacy))
        self.assertEqual(1, len(self.memory.list_conversations()))
        self.assertEqual(before, (sha(self.legacy), self.legacy.stat().st_mtime_ns))
        # Nothing appeared beside it either (no lock, no rename, no copy) -
        # only the database's own files.
        self.assertEqual(
            listing,
            sorted(p.name for p in self.root.iterdir() if not p.name.startswith("jarvis.db")),
        )

    def test_a_corrupt_file_is_refused_and_left_exactly_as_it_was(self) -> None:
        self.legacy_file("ilk")
        document = json.loads(self.legacy.read_text(encoding="utf-8"))
        document["state"]["next_seq"] = 99                # integrity no longer matches
        self.legacy.write_text(json.dumps(document), encoding="utf-8")
        before = sha(self.legacy)
        with self.assertRaises(CorruptConversation):
            self.memory.import_legacy_conversation(self.legacy)
        self.assertEqual(before, sha(self.legacy))
        self.assertEqual([p.name for p in self.root.iterdir() if "corrupt" in p.name], [])
        self.assertEqual([], self.memory.list_conversations(include_archived=True))

    def test_the_same_digest_can_never_produce_two_rows(self) -> None:
        """The primary key is the invariant, not a check in Python."""
        self.legacy_file("ilk")
        record = self.memory.import_legacy_conversation(self.legacy)
        digest = rows(self.database, "SELECT digest FROM conversation_legacy_imports")[0][0]
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO conversation_legacy_imports (digest, conversation_id, imported_at) VALUES (?, ?, 'x')",
                    (digest, record.id),
                )
        finally:
            connection.close()

    def test_rollback_era_legacy_content_is_recovered_never_lost(self) -> None:
        # A-E: the legacy state imported once; reopening adds nothing.
        self.legacy_file("v1 öncesi konuşma")
        first = self.store().import_legacy_conversation(self.legacy)
        self.assertIsNotNone(first)
        self.assertIsNone(self.store().import_legacy_conversation(self.legacy))
        # F: a rollback release keeps writing the legacy file, through the
        # legacy semantics (append under the file lock, new integrity).
        old_release = ConversationStore.open(self.legacy)[0]
        old_release.append(role="user", kind="user", text="geri dönüşte yazıldı",
                           message="geri dönüşte yazıldı", key="rollback-era")
        # G-H: the new, never-seen digest is recovered as a SEPARATE Global conversation.
        recovered = self.store().import_legacy_conversation(self.legacy)
        self.assertIsNotNone(recovered)
        self.assertNotEqual(first.id, recovered.id)
        self.assertIsNone(recovered.project_id)
        texts = [turn.message for turn in self.memory.load_conversation_state(recovered.id).turns]
        self.assertEqual(["v1 öncesi konuşma", "geri dönüşte yazıldı"], texts)
        # The first import is not merged into or altered.
        self.assertEqual(1, len(self.memory.load_conversation_state(first.id).turns))
        # I-J: reopening again duplicates nothing.
        self.assertIsNone(self.store().import_legacy_conversation(self.legacy))
        self.assertEqual(2, len(self.memory.list_conversations()))


@unittest.skipUnless(shutil.which("git"), "git is required to extract the rollback release's code")
class RollbackReleaseCompatibilityTests(StoreCase):
    """The REAL schema-10 storage code of the retained rollback release, run
    from a throwaway extraction of its commit, against disposable databases.
    Nothing here opens the operator's database or touches the worktree."""

    def setUp(self) -> None:
        super().setUp()
        known = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{ROLLBACK_COMMIT}^{{commit}}"],
                               capture_output=True)
        if known.returncode != 0:
            self.skipTest("the rollback release's commit is not in this clone")
        archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", ROLLBACK_COMMIT, "jarvis"],
                                 capture_output=True, check=True).stdout
        self.old_tree = self.root / "old-release"
        self.old_tree.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(self.old_tree, filter="data")

    def old(self, script: str, *args: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        done = subprocess.run([sys.executable, "-c", script, *args], cwd=self.old_tree, env=env,
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    OLD_OPERATIONS = r"""
import json, sys, jarvis.memory as m
from jarvis.contracts import ProjectRecord, SessionState
assert m.__file__.startswith(sys.argv[3]), m.__file__
store = m.SQLiteMemoryStore(sys.argv[1])
out = {"constant": m.SCHEMA_VERSION, "schema": store.schema_version()}
store.save_project(ProjectRecord(id="legacy-proj", name="Legacy"))
store.save_session(SessionState(objective="written by the old release", project_id="legacy-proj"))
touched = store.load_session(sys.argv[2])
touched.next_step = "touched by the old release"
store.save_session(touched)
out["next_step"] = store.load_session(sys.argv[2]).next_step
out["projects"] = sorted(p.id for p in store.list_projects())
out["sessions"] = len(store.list_sessions(50))
print(json.dumps(out))
"""

    def test_the_rollback_release_uses_a_v2_database_and_leaves_v2_rows_intact(self) -> None:
        # 1-3: a V2 database with representative V2 data.
        memory = self.store()
        memory.save_project(ProjectRecord(id="alpha-proj", name="Alpha"))
        session = SessionState(objective="v2 chat", project_id="alpha-proj")
        memory.save_session(session)
        owned = memory.create_conversation("alpha-proj", title="Alpha plan")
        memory.update_conversation_state(owned.id, lambda s: say(s, "proje notu", "k1"))
        memory.link_session_conversation(session.id, owned.id)
        self.legacy_file("eski")
        imported = memory.import_legacy_conversation(self.legacy)
        v2_before = {
            name: rows(self.database, f"SELECT * FROM {name} ORDER BY 1")
            for name in ("conversations", "conversation_sessions", "conversation_legacy_imports")
        }
        # 4-6: the old release opens it, reads and writes schema-10 data.
        out = self.old(self.OLD_OPERATIONS, str(self.database), session.id, str(self.old_tree))
        self.assertEqual({"constant": 10, "schema": 10}, {k: out[k] for k in ("constant", "schema")})
        self.assertEqual("touched by the old release", out["next_step"])
        self.assertEqual(["alpha-proj", "legacy-proj"], out["projects"])
        self.assertEqual(2, out["sessions"])
        self.assertLessEqual(COMPATIBILITY_SCHEMA_OBJECTS, set(objects(self.database)))
        # 7-8: the new code reopens: every V2 row is exactly as it was and verifies.
        reopened = self.store()
        for name, before in v2_before.items():
            self.assertEqual(before, rows(self.database, f"SELECT * FROM {name} ORDER BY 1"), name)
        self.assertEqual(["proje notu"], [t.message for t in reopened.load_conversation_state(owned.id).turns])
        self.assertEqual(1, len(reopened.load_conversation_state(imported.id).turns))
        self.assertEqual(owned.id, reopened.conversation_for_session(session.id))
        self.assertEqual("touched by the old release", reopened.load_session(session.id).next_step)
        self.assertEqual(10, reopened.schema_version())

    def test_a_database_the_rollback_release_created_is_upgraded_additively(self) -> None:
        create = r"""
import json, sys, jarvis.memory as m
from jarvis.contracts import SessionState
store = m.SQLiteMemoryStore(sys.argv[1])
store.save_session(SessionState(objective="pre-V2"))
print(json.dumps({"schema": store.schema_version()}))
"""
        self.assertEqual({"schema": 10}, self.old(create, str(self.database)))
        before = objects(self.database)
        self.assertFalse(COMPATIBILITY_SCHEMA_OBJECTS & set(before))
        self.store()
        after = objects(self.database)
        self.assertEqual(COMPATIBILITY_SCHEMA_OBJECTS, set(after) - set(before))
        for name, sql in before.items():
            self.assertEqual(sql, after[name], name)
        backup = pre_v2_backup_path(self.database)
        self.assertEqual([("ok",)], rows(backup, "PRAGMA integrity_check"))
        self.assertEqual(1, len(rows(backup, "SELECT id FROM sessions")))

    def test_legacy_content_the_rollback_release_writes_is_recovered(self) -> None:
        """Step F with the OLD release's own conversation code."""
        self.legacy_file("V2 öncesi")
        memory = self.store()
        first = memory.import_legacy_conversation(self.legacy)
        write = r"""
import json, sys
from jarvis.conversation import ConversationStore
store, problem = ConversationStore.open(sys.argv[1])
store.append(role="user", kind="user", text="eski sürümde", message="eski sürümde", key="old-release")
print(json.dumps({"problem": problem, "turns": len(store.state.turns)}))
"""
        self.assertEqual({"problem": "", "turns": 2}, self.old(write, str(self.legacy)))
        recovered = self.store().import_legacy_conversation(self.legacy)
        self.assertIsNotNone(recovered)
        self.assertNotEqual(first.id, recovered.id)
        self.assertEqual(
            ["V2 öncesi", "eski sürümde"],
            [t.message for t in memory.load_conversation_state(recovered.id).turns],
        )
        self.assertIsNone(self.store().import_legacy_conversation(self.legacy))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
