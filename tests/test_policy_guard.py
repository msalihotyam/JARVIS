"""POLICYGUARD1: the suite-wide guard in ``tests/__init__.py`` refuses the live database.

A5 "tests never open data/jarvis.db": every test module imports the ``tests``
package, which wraps ``sqlite3.connect`` and ``open``. These tests never touch the real
path: each one first asserts the guard is installed (so a missing guard fails here
instead of opening the file), and the "would succeed without it" half runs the guard
factory over a recording stub.
"""

import builtins
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

import tests


class PolicyGuardTests(unittest.TestCase):
    def require_installed(self) -> None:
        # Fail BEFORE any call can reach the real path when the guard is missing.
        for function in (sqlite3.connect, builtins.open, io.open):
            self.assertTrue(getattr(function, "policy_guard", False), "suite guard not installed")

    def test_live_path_is_refused_by_connect_and_open(self) -> None:
        self.require_installed()
        live = tests.LIVE_DATABASE
        for call in (
            lambda: sqlite3.connect(live),
            lambda: sqlite3.connect(str(live)),
            lambda: sqlite3.connect(live.as_uri(), uri=True),
            lambda: sqlite3.connect(f"{live.as_uri()}?mode=ro", uri=True),
            lambda: sqlite3.connect(str(live) + "-wal"),
            lambda: open(live, "rb"),
            lambda: io.open(str(live)),
            lambda: live.read_bytes(),
            lambda: open(live.parent / ".." / "data" / "jarvis.db"),
        ):
            with self.subTest(call=call), self.assertRaises(AssertionError):
                call()

    def test_a_temporary_copy_with_the_same_name_is_not_refused(self) -> None:
        self.require_installed()
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "jarvis.db"
            connection = sqlite3.connect(copy)
            connection.execute("create table t (x)")
            connection.commit()
            connection.close()
            with open(copy, "rb") as handle:
                self.assertTrue(handle.read(16).startswith(b"SQLite format 3"))
            readonly = sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)
            readonly.close()

    def test_guard_factory_blocks_before_the_wrapped_function_runs(self) -> None:
        # Without the guard the same call reaches the function (the recorded call).
        calls: list[object] = []

        def stub(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(args[0] if args else kwargs["database"])

        guarded = tests._guarded(stub)
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / "jarvis.db"
            guarded(other)
            self.assertEqual(calls, [other])
        with self.assertRaises(AssertionError):
            guarded(tests.LIVE_DATABASE)
        self.assertEqual(len(calls), 1)
        guarded(":memory:")
        guarded(3)  # a file descriptor is never the path
        guarded(database=other)  # keyword spelling is checked too
        self.assertEqual(len(calls), 4)
        with self.assertRaises(AssertionError):
            guarded(database=tests.LIVE_DATABASE)


if __name__ == "__main__":
    unittest.main()
