"""F7 on file result turns: only the path the file connector proved from its
open handle can make a turn local_trusted_file, never the planner's
destination text (SECCAPWIRE2).

Fake providers only; every store lives in the test's own TEMP database.
"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from jarvis.connectors.local_files import LocalFileReadConnector
from jarvis.conversation import ConversationState, append_turn, state_from_dict
from jarvis.core import _capture_session_taint, _validated_capture_turns
from jarvis.memory import SQLiteMemoryStore
from jarvis.memory_guard import SessionTaint
from jarvis.registry import ToolRegistry
from jarvis.ui.bridge import _proved_path
from tests.helpers import isolated_settings, open_core, safe_cleanup
from tests.test_memory_capture_guard import BUILD, DEPLOY, FACT, SESSION_END, FakeModel


class Roots(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        base = Path(temp.name).resolve()
        self.root, self.outside = base / "project", base / "outside"
        (self.root / "docs").mkdir(parents=True)
        self.outside.mkdir()
        (self.root / "docs" / "notes.txt").write_text("x", encoding="utf-8")
        (self.outside / "secret.txt").write_text("x", encoding="utf-8")
        self.roots = (self.root,)


class ProvedPathTaintTests(Roots):
    def taint(self, capability="file_read", destination="", **path):
        turn = SimpleNamespace(kind="result", capability=capability, destination=destination, **path)
        return _capture_session_taint((turn,), SESSION_END, self.roots)

    def test_connector_proved_path_inside_a_root_is_trusted(self) -> None:
        for capability in ("file_read", "file_write"):
            with self.subTest(capability=capability):
                self.assertIs(self.taint(capability, path="docs/notes.txt"), SessionTaint.LOCAL_TRUSTED_FILE)

    def test_planner_destination_is_never_trusted(self) -> None:
        inside = str(self.root / "docs" / "notes.txt")
        self.assertIs(self.taint(destination=inside), SessionTaint.IMPORT)
        self.assertIs(self.taint(destination=inside, path=""), SessionTaint.IMPORT)
        # An escaping proved path is not rescued by an in-root destination.
        self.assertIs(self.taint(destination=inside, path="../outside/secret.txt"), SessionTaint.IMPORT)

    def test_missing_bare_anchored_or_escaping_path_is_import(self) -> None:
        for path in ("", None, 7, "notes.txt", "../outside/secret.txt", "docs/../../outside/secret.txt",
                     str(self.root / "docs" / "notes.txt"), "/docs/notes.txt", "C:docs/notes.txt",
                     "\\\\host\\share\\notes.txt", "//host/share/notes.txt"):
            with self.subTest(path=path):
                self.assertIs(self.taint(path=path), SessionTaint.IMPORT)
        self.assertIs(self.taint(), SessionTaint.IMPORT)   # an old turn with no path attribute

    def test_failed_file_run_records_no_path(self) -> None:
        block = {"relative_path": "docs/notes.txt"}
        self.assertEqual(_proved_path({"run_status": "completed", "local_read": block}), "docs/notes.txt")
        self.assertEqual(_proved_path({"run_status": "completed", "local_write": block}), "docs/notes.txt")
        for run in ({"run_status": "failed", "local_read": block}, {"run_status": "failed", "local_write": block},
                    {"run_status": "completed", "local_read": None}, {"run_status": "completed",
                     "destination": str(self.root / "docs" / "notes.txt")}, None, "completed"):
            with self.subTest(run=run):
                self.assertEqual(_proved_path(run), "")


class SavedStateTests(unittest.TestCase):
    def test_path_round_trips_and_old_state_without_it_loads(self) -> None:
        state, _ = append_turn(ConversationState(), role="assistant", kind="result", text="done",
                               capability="file_read", path="docs/notes.txt", key="r")
        state, _ = append_turn(state, role="assistant", kind="result", text="done", capability="web_search", key="w")
        saved = state.to_dict()
        self.assertEqual([t.get("path") for t in saved["turns"]], ["docs/notes.txt", None])
        self.assertEqual([t.path for t in state_from_dict(saved).turns], ["docs/notes.txt", ""])
        del saved["turns"][0]["path"]   # what an older build saved
        self.assertEqual([t.path for t in state_from_dict(saved).turns], ["", ""])

    def test_a_compacted_turns_path_is_dropped(self) -> None:
        # A compacted turn's path is bound to nothing stored, so it reads as import.
        batch = []
        state, _ = append_turn(ConversationState(), role="assistant", kind="result", text="done",
                               capability="file_read", path="docs/notes.txt", key="r")
        state, _ = append_turn(state, role="user", kind="user", text="next", message="next", key="u",
                               raw_turns_max=1, on_compact=batch.extend)
        self.assertEqual([t.path for t in batch], ["docs/notes.txt"])
        self.assertEqual([t.path for t in _validated_capture_turns(tuple(batch), state)], [""])


class CaptureEndToEndTests(Roots):
    def setUp(self) -> None:
        super().setUp()
        registry = ToolRegistry()
        registry.register(LocalFileReadConnector(allowed_roots=[self.root]))
        settings = isolated_settings(self)
        self.database = settings.memory_database
        self.store = SQLiteMemoryStore(self.database)
        self.core = open_core(registry, self.store, settings, destination_policy=frozenset())
        self.alpha = self.store.create_project("Alpha").id

    def capture_taint(self, **result):
        conversation = self.store.create_conversation(self.alpha).id
        ids = []

        def change(state):
            for index, text in enumerate((DEPLOY, BUILD)):
                state, turn = append_turn(state, role="user", kind="user", text=text, message=text, key=f"u{index}")
                ids.append(turn.id)
            state, _ = append_turn(state, role="assistant", kind="result", text="done",
                                   capability="file_read", key="r", **result)
            return state

        self.store.update_conversation_state(conversation, change)
        model = FakeModel(json.dumps({"candidates": [{"kind": "fact", "text": FACT, "subject_key": "tooling",
                                                      "confidence": 0.9, "evidence_turn_ids": [ids[1]]}]}))
        self.assertEqual(self.core.capture_memory(conversation, SESSION_END, model).outcome, "ok")
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT session_taint FROM memory_record_guard ORDER BY rowid DESC "
                                      "LIMIT 1").fetchone()[0]

    def test_proved_in_root_read_is_local_trusted_file(self) -> None:
        self.assertEqual(self.capture_taint(path="docs/notes.txt"), "local_trusted_file")

    def test_in_root_destination_alone_is_import(self) -> None:
        self.assertEqual(self.capture_taint(destination=str(self.root / "docs" / "notes.txt")), "import")


if __name__ == "__main__":
    unittest.main()
