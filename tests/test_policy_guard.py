"""POLICYGUARD1: the suite-wide guard in ``tests/__init__.py`` refuses the live database.

A5 "tests never open data/jarvis.db": every test module imports the ``tests``
package, which wraps ``sqlite3.connect`` and ``open``. These tests never touch the real
path: each one first asserts the guard is installed (so a missing guard fails here
instead of opening the file), and the "would succeed without it" half runs the guard
factory over a recording stub.
"""

import builtins
import io
import os
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

import tests


class BytesPath:
    """An os.PathLike whose __fspath__ returns bytes (legal for open)."""

    def __init__(self, path) -> None:  # type: ignore[no-untyped-def]
        self.raw = os.fsencode(path)

    def __fspath__(self) -> bytes:
        return self.raw


class PolicyGuardTests(unittest.TestCase):
    def require_installed(self) -> None:
        # Fail BEFORE any call can reach a path when the guard is missing.
        for function in (sqlite3.connect, builtins.open, io.open):
            self.assertTrue(getattr(function, "policy_guard", False), "suite guard not installed")

    def stand_in_live(self) -> Path:
        # The refusal is proved on a temporary path configured as live: never the real file.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        live = Path(folder.name).resolve() / "stand-in-live.db"
        paths = {os.path.normcase(str(live) + suffix) for suffix in ("", "-wal", "-shm", "-journal")}
        patcher = mock.patch.object(tests, "_LIVE_PATHS", paths)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.require_installed()
        self.assertTrue(tests._is_live(live), "the stand-in path must be configured as live")
        return live

    def test_real_live_path_is_recognised_without_opening_it(self) -> None:
        self.require_installed()
        live = tests.LIVE_DATABASE
        for spelling in (live, str(live), live.as_uri(), f"{live.as_uri()}?mode=ro", str(live) + "-wal",
                         live.parent / ".." / "data" / "jarvis.db", BytesPath(live), os.fsencode(live)):
            with self.subTest(spelling=spelling):
                self.assertTrue(tests._is_live(spelling))

    def test_live_path_is_refused_by_connect_and_open(self) -> None:
        live = self.stand_in_live()
        uri = live.as_uri()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            calls = (
                lambda: sqlite3.connect(live),
                lambda: sqlite3.connect(str(live)),
                lambda: sqlite3.connect(uri, uri=True),
                lambda: sqlite3.connect(f"{uri}?mode=ro", uri=True),
                lambda: sqlite3.connect(str(live) + "-wal"),
                lambda: sqlite3.connect(uri),  # a "file:" string is a URI whatever the flag says
                lambda: sqlite3.connect(uri.upper().replace("FILE:", "file:", 1)),
                lambda: sqlite3.connect(uri, 5.0, 0, "", True, sqlite3.Connection, 128, True),  # positional uri
                lambda: sqlite3.connect(database=uri, uri=True),
                lambda: sqlite3.connect(os.fsencode(live)),
                lambda: open(live, "rb"),
                lambda: open(file=str(live)),
                lambda: io.open(str(live)),
                lambda: open(BytesPath(live)),
                lambda: live.read_bytes(),
                lambda: open(live.parent / ".." / live.parent.name / live.name),
            )
            for call in calls:
                with self.subTest(call=call), self.assertRaises(AssertionError):
                    call()
        self.assertFalse(live.exists(), "a refused call must not create the file")

    def test_unresolvable_uri_is_treated_as_live(self) -> None:
        live = self.stand_in_live()
        for uri in (live.as_uri() + "%00x", "file:" + "%00", "file:\x00bad"):
            with self.subTest(uri=uri[:40]):
                self.assertTrue(tests._is_live(uri))
                with self.assertRaises(AssertionError):
                    sqlite3.connect(uri, uri=True)

    def test_a_uri_that_fails_to_resolve_is_treated_as_live(self) -> None:
        self.require_installed()
        for error in (ValueError, OSError):
            with self.subTest(error=error), mock.patch.object(Path, "resolve", side_effect=error):
                self.assertTrue(tests._is_live("file:///anything.db"))
                self.assertFalse(tests._is_live("anything.db"))  # a plain path: its own call raises

    def test_wrappers_keep_identity(self) -> None:
        self.require_installed()
        self.assertEqual(sqlite3.connect.__name__, "connect")
        self.assertEqual(open.__name__, "open")
        self.assertIs(builtins.open, io.open)

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
            with open(BytesPath(copy), "rb") as handle:  # a bytes PathLike passes through untouched
                self.assertTrue(handle.read(16).startswith(b"SQLite format 3"))

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
