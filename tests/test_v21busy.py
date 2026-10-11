"""V21BUSY: store write transactions retry briefly on SQLite busy, then fail
as before. Contention is forced with a second connection holding the write
lock (``BEGIN IMMEDIATE``); the pause between attempts is where the test
releases it, so nothing depends on wall-clock timing."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import memory
from jarvis.memory import SQLiteMemoryStore


class StoreWriteBusyRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Path(self._tmp.name) / "busy.db"
        self.store = SQLiteMemoryStore(self.database)
        # Short SQLite busy waits keep each refused attempt quick.
        for patch in (
            mock.patch.object(memory, "_BUSY_TIMEOUT_S", 0.1),
            mock.patch.object(memory, "_WRITE_BUSY_TOTAL_S", 15.0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.holder = sqlite3.connect(self.database, isolation_level=None)
        self.addCleanup(self.holder.close)
        self.pauses: list[float] = []

    def hold_write_lock(self) -> None:
        self.holder.execute("BEGIN IMMEDIATE")

    def release_write_lock(self) -> None:
        if self.holder.in_transaction:
            self.holder.execute("COMMIT")

    def project_names(self) -> list[str]:
        return [project.name for project in self.store.list_projects()]

    def test_retry_succeeds_once_the_lock_is_released(self) -> None:
        self.hold_write_lock()

        def pause(seconds: float) -> None:
            self.pauses.append(seconds)
            self.release_write_lock()

        bodies = []
        with mock.patch.object(memory.time, "sleep", pause), \
                self.assertLogs("jarvis.memory", "WARNING") as logs:
            with self.store._connection(write=True) as connection:
                bodies.append(1)
                # The retry's busy wait is back to the store-wide value.
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 100)
                connection.execute(
                    "INSERT INTO projects (id, name, active, created_at) VALUES (?, ?, 1, ?)",
                    ("p-" + "a" * 32, "After the wait", "2026-10-11T00:00:00+00:00"),
                )
        self.assertEqual(bodies, [1])                       # the body ran once, after BEGIN won
        self.assertEqual(self.pauses, [0.05])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("busy (attempt 1 of 3)", logs.output[0])
        self.assertIn("database is locked", logs.output[0])
        self.assertEqual(self.project_names(), ["After the wait"])

    def test_gives_up_after_the_attempt_cap_with_the_original_error(self) -> None:
        self.hold_write_lock()
        with mock.patch.object(memory.time, "sleep", self.pauses.append), \
                self.assertLogs("jarvis.memory", "WARNING") as logs, \
                self.assertRaises(sqlite3.OperationalError) as raised:
            self.store.create_project("Never")
        self.assertIn("database is locked", str(raised.exception))
        self.assertEqual(self.pauses, [0.05, 0.2])
        self.assertEqual(len(logs.output), 3)
        self.assertIn("attempt 1 of 3", logs.output[0])
        self.assertIn("attempt 2 of 3", logs.output[1])
        self.assertIn("gave up after 3 busy attempt(s)", logs.output[2])
        self.release_write_lock()
        self.assertEqual(self.project_names(), [])

    def test_the_total_time_cap_stops_retrying_early(self) -> None:
        self.hold_write_lock()
        with mock.patch.object(memory, "_WRITE_BUSY_TOTAL_S", 0.05), \
                mock.patch.object(memory.time, "sleep", self.pauses.append), \
                self.assertLogs("jarvis.memory", "WARNING") as logs, \
                self.assertRaises(sqlite3.OperationalError) as raised:
            self.store.create_project("Never")
        self.assertIn("database is locked", str(raised.exception))
        self.assertEqual(self.pauses, [])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("gave up after 1 busy attempt(s)", logs.output[0])

    def test_a_busy_error_inside_a_transaction_rolls_it_all_back_and_is_not_retried(self) -> None:
        bodies = []
        with mock.patch.object(memory.time, "sleep", self.pauses.append), \
                self.assertNoLogs("jarvis.memory", "WARNING"), \
                self.assertRaises(sqlite3.OperationalError):
            with self.store._connection(write=True) as connection:
                bodies.append(1)
                connection.execute(
                    "INSERT INTO projects (id, name, active, created_at) VALUES (?, ?, 1, ?)",
                    ("p-" + "b" * 32, "Half", "2026-10-11T00:00:00+00:00"),
                )
                raise sqlite3.OperationalError("database is locked")
        self.assertEqual(bodies, [1])
        self.assertEqual(self.pauses, [])
        self.assertEqual(self.project_names(), [])

    def test_reads_do_not_wait_on_a_held_write_lock(self) -> None:
        self.store.create_project("Readable")
        self.hold_write_lock()
        with mock.patch.object(memory.time, "sleep", self.pauses.append), \
                self.assertNoLogs("jarvis.memory", "WARNING"):
            self.assertEqual(self.project_names(), ["Readable"])
        self.assertEqual(self.pauses, [])


if __name__ == "__main__":
    unittest.main()
