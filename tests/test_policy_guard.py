"""POLICYGUARD1: the suite-wide guard in ``tests/__init__.py`` refuses the live database.

A5 "tests never open data/jarvis.db": every test module imports the ``tests``
package, which wraps ``sqlite3.connect`` and ``open``. These tests never touch the real
path: each one first asserts the guard is installed (so a missing guard fails here
instead of opening the file), refusals are proved on a temporary folder configured as
the live data folder, and the "would succeed without it" half runs the guard factory
over a recording stub. POLICYGUARD1FIX3: the guard decides by file and folder IDENTITY,
so every spelling Win32 accepts for the live file - and any it cannot decide - is refused.
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


def touch(path) -> None:  # type: ignore[no-untyped-def]
    # os.open is not guarded: it creates the stand-in live file without going through open().
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY))


class PolicyGuardTests(unittest.TestCase):
    def require_installed(self) -> None:
        # Fail BEFORE any call can reach a path when the guard is missing.
        for function in (sqlite3.connect, builtins.open, io.open):
            self.assertTrue(getattr(function, "policy_guard", False), "suite guard not installed")

    def scratch(self) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name).resolve()

    def stand_in_live(self) -> Path:
        # A temporary data/ folder is configured as the live one: never the real file.
        data = self.scratch() / "data"
        data.mkdir()
        live = data / "jarvis.db"
        patcher = mock.patch.object(tests, "LIVE_DATABASE", live)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.require_installed()
        self.assertTrue(tests._is_live(live), "the stand-in path must be configured as live")
        return live

    def assert_refused(self, spelling, uri: bool = False) -> None:  # type: ignore[no-untyped-def]
        with self.subTest(spelling=spelling):
            self.assertTrue(tests._is_live(spelling))
            with self.assertRaises(AssertionError):
                open(spelling, "rb")
            with self.assertRaises(AssertionError):
                sqlite3.connect(spelling, uri=uri)
            if isinstance(spelling, str):
                with self.assertRaises(AssertionError):
                    sqlite3.connect(os.fsencode(spelling), uri=uri)

    def test_real_live_path_is_recognised_without_opening_it(self) -> None:
        self.require_installed()
        live = tests.LIVE_DATABASE
        for spelling in (live, str(live), live.as_uri(), f"{live.as_uri()}?mode=ro", str(live) + "-wal",
                         live.parent / ".." / "data" / "jarvis.db", BytesPath(live), os.fsencode(live)):
            with self.subTest(spelling=spelling):
                self.assertTrue(tests._is_live(spelling))

    def test_live_path_is_refused_by_connect_and_open(self) -> None:
        # Variants 1 and 2: plain forms, keywords, ".." detours, URIs whatever flag carries them.
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
        self.assertEqual(os.listdir(live.parent), [], "a refused call must not create anything")

    def test_unresolvable_uri_is_treated_as_live(self) -> None:
        # Variant 2: SQLite cuts a decoded URI path at an encoded NUL and opens what is left.
        live = self.stand_in_live()
        for uri in (live.as_uri() + "%00x", "file:" + "%00", "file:\x00bad"):
            with self.subTest(uri=uri[:40]):
                self.assertTrue(tests._is_live(uri))
                with self.assertRaises(AssertionError):
                    sqlite3.connect(uri, uri=True)

    def test_win32_spellings_of_the_live_path_are_refused(self) -> None:
        # Variants 3-8: prefixes, case, trailing dots/spaces, streams, companions, lexical tails.
        live = self.stand_in_live()
        text = str(live)
        folder = str(live.parent)
        spellings = (
            "\\\\?\\" + text,  # extended-length prefix
            "\\\\.\\" + text,  # device prefix
            "//?/" + text.replace("\\", "/"),  # forward-slash extended-length prefix
            "\\??\\" + text,  # NT object-manager prefix
            "\\\\?\\" + text.upper(),  # prefix and case
            text.lower(),
            folder + "\\jarv\u0131s.db",  # dotless i and long s: NTFS does not fold them, refused anyway
            folder + "\\jarvi\u017f.db",
            text + ".",  # trailing dot / space: Win32 drops them
            text + " ",
            text + ". .",
            text + "-wal.",
            text.replace(live.name, live.name + " "),
            folder + ". \\" + live.name,  # trailing dot and space on a directory component
            "\\\\?\\" + folder + " \\" + live.name + ".",  # a folder that does not exist: undecidable
            "file:///" + text.replace("\\", "/") + ".",
            text + "::$DATA",  # NTFS stream on a file that does not exist yet (CLREV F1)
            text + ":x",
            text + ":x:$DATA",
            text + "-wal",  # companions (CLREV F1)
            text + "-shm",
            text + "-journal",
            text + "-wal::$DATA",
            text + "-shm. ",
            text + "-wal\\.",  # lexical tails
            text + "-wal\\x\\..",
            text + "\\.",
            folder + "\\jarvis.dbx",  # any jarvis.db* sibling
        )
        for spelling in spellings:
            self.assert_refused(spelling)
        self.assertEqual(os.listdir(live.parent), [], "a refused call must not create anything")
        self.assertFalse(tests._is_live(folder + "\\other.db."))  # not over-blocking
        self.assertFalse(tests._is_live(folder + "\\other.db::$DATA"))

    def test_other_routes_to_the_same_folder_are_refused(self) -> None:
        # Variant 9: admin share, loopback address and a junction reach the live folder. A share that
        # is not reachable on this machine cannot be decided and is refused too: no case is skipped.
        live = self.stand_in_live()
        drive, rest = os.path.splitdrive(str(live))
        share = f"{drive[0]}$" + rest
        import _winapi  # noqa: PLC0415 - Windows only, as the routes are

        junction = self.scratch() / "route"
        _winapi.CreateJunction(str(live.parent), str(junction))
        self.addCleanup(os.rmdir, junction)
        spellings = (
            "\\\\localhost\\" + share,
            "\\\\127.0.0.1\\" + share,
            "//localhost/" + share.replace("\\", "/"),
            "\\\\localhost\\" + share + ".",
            "\\\\?\\UNC\\localhost\\" + share,
            "\\\\?\\UNC\\localhost\\" + share + "-wal::$DATA",
            str(junction / live.name),
            str(junction / (live.name + "-shm")),
        )
        for spelling in spellings:
            self.assert_refused(spelling)
        self.assert_refused("file:////localhost/" + share.replace("\\", "/"), uri=True)
        self.assertEqual(os.listdir(live.parent), [], "a refused call must not create anything")
        self.assertFalse(tests._is_live("\\\\localhost\\" + share.replace(live.name, "other.db")))  # not over-blocking
        self.assertFalse(tests._is_live(str(junction / "other.db")))

    def test_other_routes_to_the_same_file_are_refused(self) -> None:
        # Variant 10: a hard link or a symlink elsewhere is the live file under another name.
        live = self.stand_in_live()
        touch(live)
        touch(str(live) + "-wal")
        elsewhere = self.scratch()
        os.link(live, elsewhere / "copy.db")
        os.link(str(live) + "-wal", elsewhere / "log.bin")
        os.symlink(live, elsewhere / "pointer.db")
        os.symlink(str(live) + "-shm", elsewhere / "dangling.db")  # to a companion that does not exist
        # CLREV F2 B3: a stream on a hard link is a stream on the live file.
        for name in ("copy.db", "log.bin", "pointer.db", "dangling.db", "copy.db:x", "log.bin::$DATA"):
            self.assert_refused(str(elsewhere / name))
        touch(elsewhere / "unrelated.db")
        self.assertFalse(tests._is_live(elsewhere / "unrelated.db"))  # an unrelated existing file opens
        self.assertFalse(tests._is_live(elsewhere / "missing.db"))

    def test_anything_that_cannot_be_decided_is_treated_as_live(self) -> None:
        # Variant 11: a step that raises, or a jarvis.db* name in a folder that is not there, is live.
        live = self.stand_in_live()
        for error in (ValueError, OSError, PermissionError):
            for target in ("file:///anything.db", "anything.db", live):
                with self.subTest(error=error, target=target):
                    # POLICYGUARD1FIX4: the guard calls its import-time refs, so those are what raise.
                    for name in ("_stat", "_realpath", "_lexists"):
                        with mock.patch.object(tests, name, side_effect=error):
                            self.assertTrue(tests._is_live(target), name)
        missing = self.scratch() / "missing"
        self.assertTrue(tests._is_live(missing / "jarvis.db"))
        self.assertFalse(tests._is_live(missing / "other.db"))  # not a live name: no folder to decide

    def test_spies_on_os_helpers_never_see_the_guard(self) -> None:
        # POLICYGUARD1FIX4 (CLREV F1): a test spying on os.* must not log the guard's own calls inside
        # open()/connect. The live file exists so rule (a) stats; a jarvis.db name runs rule (b).
        live = self.stand_in_live()
        touch(live)
        other = self.scratch()
        touch(other / "plain.txt")
        touch(other / "jarvis.db")
        # Plain strings only: pathlib itself calls os.fspath, which is not the guard.
        plain, copy, live_wal, live = str(other / "plain.txt"), str(other / "jarvis.db"), str(live) + "-wal", str(live)
        names = {
            # POLICYGUARD1FIX5 (CDX F2): os.fspath and the os.path helpers os.path's own functions call
            # too: the guard runs on a private os.path copy bound to an import-time os snapshot.
            os: ("stat", "lstat", "fsdecode", "fsencode", "fspath", "getcwd"),
            os.path: ("realpath", "exists", "lexists", "dirname", "samestat", "abspath", "split", "join",
                      "normpath", "normcase", "isabs", "splitroot"),
        }
        spies = []
        for module, attributes in names.items():
            for attribute in attributes:
                spy = mock.Mock(side_effect=getattr(module, attribute))
                patcher = mock.patch.object(module, attribute, spy)
                patcher.start()
                self.addCleanup(patcher.stop)
                spies.append((attribute, spy))
        with open(plain, "rb"):
            pass
        sqlite3.connect(copy).close()
        self.assertTrue(tests._is_live(live))
        self.assertTrue(tests._is_live(live_wal.upper()))
        self.assertFalse(tests._is_live(copy))
        with self.assertRaises(AssertionError):
            open(live, "rb")
        for attribute, spy in spies:
            self.assertEqual(spy.call_count, 0, attribute)

    def test_wrappers_keep_identity(self) -> None:
        self.require_installed()
        self.assertEqual(sqlite3.connect.__name__, "connect")
        self.assertEqual(open.__name__, "open")
        self.assertIs(builtins.open, io.open)

    def test_a_temporary_copy_with_the_same_name_is_not_refused(self) -> None:
        self.stand_in_live()
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
            with open(BytesPath(copy), "rb") as handle:  # a bytes PathLike is pinned to its bytes
                self.assertTrue(handle.read(16).startswith(b"SQLite format 3"))
            sqlite3.connect(str(copy) + "-wal").close()  # a companion name elsewhere is fine

    def test_guard_factory_blocks_before_the_wrapped_function_runs(self) -> None:
        # Without the guard the same call reaches the function (the recorded call).
        live = self.stand_in_live()
        calls: list[object] = []

        def stub(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(args[0] if args else kwargs["database"])

        guarded = tests._guarded(stub, "factory", 5)
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / "jarvis.db"
            guarded(other)
            self.assertEqual(calls, [str(other)])  # POLICYGUARD1FIX5: every PathLike is pinned to its str
            with self.assertRaises(AssertionError):
                guarded(live)
            self.assertEqual(len(calls), 1)
            for memory in (":memory:", "", "file::memory:", "file::memory:?cache=shared", 3):
                self.assertFalse(tests._is_live(memory))  # in-memory, temporary or a file descriptor
            guarded(":memory:")
            guarded(3)
            guarded(database=other)  # keyword spelling is checked too
            self.assertEqual(len(calls), 4)
        with self.assertRaises(AssertionError):
            guarded(database=live)

    def test_a_file_uri_is_also_judged_as_a_plain_relative_path(self) -> None:
        # CLREV F2 B1: open() and a connect without uri=True take "file:/../data/jarvis.db" as a
        # relative path ("file:" is a folder that ".." leaves): from the live data folder's parent.
        live = self.stand_in_live()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(live.parent.parent)
        for spelling in ("file:/../data/x#/../jarvis.db", "FILE:/../data/jarvis.db", "FILE:/../data/jarvis.db-wal"):
            self.assert_refused(spelling)
        self.assertFalse(tests._is_live("file:/../data/other.db"))
        self.assertEqual(os.listdir(live.parent), [], "a refused call must not create anything")

    def test_a_str_subclass_cannot_answer_differently(self) -> None:
        # CLREV F2 B2: the guard judges the exact str the call opens, not the subclass' == / in.
        live = self.stand_in_live()

        class Sly(str):
            __eq__ = lambda self, other: True  # noqa: E731
            __hash__ = str.__hash__

        self.assert_refused(Sly(str(live)))

    def test_a_relative_dangling_link_reached_through_another_folder_is_refused(self) -> None:
        # CLREV F2 B4: Win32 resolves a relative link target from the path that reached the link
        # (B\hop\..\data), not from the link's real folder: a dangling link is refused outright.
        live = self.stand_in_live()
        deep = self.scratch() / "deep"
        deep.mkdir()
        os.symlink("..\\data\\jarvis.db", deep / "rel.db")
        hop = live.parent.parent / "hop"
        os.symlink(deep, hop, target_is_directory=True)
        self.addCleanup(os.rmdir, hop)
        self.assert_refused(str(hop / "rel.db"))
        self.assertEqual(os.listdir(live.parent), [], "a refused call must not create anything")

    def test_a_changing_fspath_is_pinned_to_the_judged_value(self) -> None:
        # CLREV F2 G3: __fspath__ runs once; the wrapped call receives exactly the judged str.
        live = self.stand_in_live()
        safe = str(self.scratch() / "safe.db")
        calls: list[object] = []

        class Flip:
            def __init__(self, *answers: str) -> None:
                self.answers = list(answers)

            def __fspath__(self) -> str:
                return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

        guarded = tests._guarded(lambda *args, **kwargs: calls.append(args[0]), "factory", 5)
        guarded(Flip(safe, str(live)))
        self.assertEqual(calls, [safe])
        self.assertIs(type(calls[0]), str)
        with self.assertRaises(AssertionError):
            guarded(Flip(str(live), safe))
        self.assertEqual(len(calls), 1)

    def test_a_path_subclass_with_a_changing_str_is_pinned_too(self) -> None:
        # POLICYGUARD1FIX5 (CDX F1): PurePath.__fspath__ is str(self), so a subclass overriding __str__
        # changes its fspath per call; no PathLike is trusted as stable: each is pinned once.
        live = self.stand_in_live()
        folder = self.scratch()
        safe, safe_db = folder / "safe.txt", str(folder / "safe.db")
        for path, marker in ((live, b"live"), (safe, b"safe")):
            handle = os.open(path, os.O_CREAT | os.O_WRONLY)  # os.open: the guard is not in the way
            os.write(handle, marker)
            os.close(handle)

        class Alternating(type(Path())):  # type: ignore[misc]
            # str() is called through a counter only: == / hash / repr would use alternations too.
            def __init__(self, *answers: str) -> None:
                super().__init__(answers[0])
                self.answers, self.count = answers, 0

            def __str__(self) -> str:
                self.count += 1
                return self.answers[(self.count - 1) % 2]

        first = Alternating(str(safe), str(live))
        with open(first, "rb") as handle:
            self.assertEqual(handle.read(), b"safe")
        self.assertEqual(first.count, 1)
        connection = sqlite3.connect(Alternating(safe_db, str(live)))
        self.addCleanup(connection.close)
        self.assertEqual(Path(connection.execute("pragma database_list").fetchone()[2]), Path(safe_db))
        for call in (lambda p: open(p, "rb"), sqlite3.connect):
            second = Alternating(str(live), str(safe))
            with self.assertRaises(AssertionError):
                call(second)
            self.assertEqual(second.count, 1)
        calls: list[object] = []
        guarded = tests._guarded(lambda *args, **kwargs: calls.append(args[0]), "factory", 5)
        guarded(Alternating(safe_db, str(live)))
        self.assertEqual(calls, [safe_db])
        self.assertIs(type(calls[0]), str)

        def other_bytes(target: Path) -> type:
            # a bytes subclass whose __bytes__ names another file: bytes(x) would judge that one
            return type("Other", (bytes,), {"__bytes__": lambda self: os.fsencode(target)})

        with self.assertRaises(AssertionError):
            open(other_bytes(safe)(os.fsencode(live)), "rb")
        with open(other_bytes(live)(os.fsencode(safe)), "rb") as handle:
            self.assertEqual(handle.read(), b"safe")

    def test_a_custom_opener_or_factory_is_refused(self) -> None:
        # CLREV F2 G1/G2: an opener or a Connection factory could open any file whatever the path.
        self.stand_in_live()
        safe = self.scratch() / "safe.db"

        class SubConnection(sqlite3.Connection):
            pass

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            calls = (
                lambda: open(safe, "w", opener=os.open),
                lambda: io.open(safe, "w", -1, None, None, None, True, os.open),  # positional opener
                lambda: sqlite3.connect(":memory:", factory=SubConnection),
                lambda: sqlite3.connect(":memory:", 5.0, 0, "", True, SubConnection),  # positional factory
            )
            for call in calls:
                with self.subTest(call=call), self.assertRaises(AssertionError):
                    call()
        self.assertFalse(safe.exists())
        sqlite3.connect(":memory:", factory=sqlite3.Connection).close()  # the default factory is fine


if __name__ == "__main__":
    unittest.main()
