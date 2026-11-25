"""TALKHOME1: conversation service home folder and single-instance lock."""

import json
import os
import re
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from jarvis import talk_home as th
from tests.adversarial import HOSTILE_JSON, HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, each

REPO = Path(__file__).resolve().parent.parent
ALIVE = lambda pid: True  # noqa: E731
DEAD = lambda pid: False  # noqa: E731


class _Home(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = os.path.realpath(tmp.name)
        self.root = os.path.join(self.base, "talk")
        self.lock = os.path.join(self.root, "state", th.LOCK_NAME)

    def refused(self, result, code):
        self.assertIsInstance(result, th.Refusal)
        self.assertEqual(result.code, code, result.text)
        self.assertTrue(result.text.isascii())

    def acquire(self, boot="b1", pid=100, now=1000, alive=ALIVE, **kw):
        return th.acquire(self.root, boot, pid, now, alive=alive, **kw)

    def write_lock(self, data):
        self.assertIsInstance(th.ensure(self.root), th.Layout)
        with open(self.lock, "wb") as f:
            f.write(data)


def _recording_stat(calls):
    def port(path):
        calls.append(path)
        return os.lstat(path)
    return port


def _fake_stat(target, **fields):
    def port(path):
        if path == target:
            return types.SimpleNamespace(**fields)
        return os.lstat(path)
    return port


class LayoutTests(_Home):
    def test_layout_names_four_folders_under_root(self):
        home = th.layout(self.root)
        self.assertIsInstance(home, th.Layout)
        for name in ("log", "attachments", "inbox", "state"):
            self.assertEqual(getattr(home, name), os.path.join(self.root, name))
        self.assertFalse(os.path.exists(self.root))  # layout touches nothing

    def test_ensure_creates_folders_and_is_idempotent(self):
        for _ in range(2):
            home = th.ensure(self.root)
            self.assertIsInstance(home, th.Layout)
        self.assertEqual(sorted(os.listdir(self.root)), ["attachments", "inbox", "log", "state"])
        self.assertEqual(os.listdir(self.base), ["talk"])  # nothing outside the root

    def test_missing_parent_is_refused_not_created(self):
        self.refused(th.ensure(os.path.join(self.base, "a", "talk")), "ROOT_PARENT_MISSING")
        self.assertEqual(os.listdir(self.base), [])

    def test_root_that_is_a_file_is_refused(self):
        Path(self.root).write_bytes(b"x")
        self.refused(th.ensure(self.root), "ROOT_NOT_FOLDER")
        self.refused(self.acquire(), "ROOT_NOT_FOLDER")

    def test_layout_folder_that_is_a_file_is_refused(self):
        os.mkdir(self.root)
        Path(self.root, "state").write_bytes(b"x")
        self.refused(th.ensure(self.root), "FOLDER_NOT_FOLDER")
        self.refused(self.acquire(), "FOLDER_NOT_FOLDER")

    def test_link_or_junction_reported_by_stat_port_is_refused(self):
        for name in ("log", "state"):
            path = os.path.join(self.root, name)
            for fields in ({"st_mode": stat.S_IFLNK | 0o777},
                           {"st_mode": stat.S_IFDIR, "st_file_attributes": 0x400}):
                with self.subTest(name=name, fields=fields):
                    self.refused(th.ensure(self.root, stat=_fake_stat(path, **fields)), "LINK_REFUSED")
        self.refused(th.ensure(self.root, stat=_fake_stat(self.root, st_mode=stat.S_IFLNK)),
                     "LINK_REFUSED")


class RootRefusalTests(_Home):
    def assert_refused_untouched(self, root, codes=("ROOT_INVALID", "ROOT_PROTECTED")):
        calls = []
        port = _recording_stat(calls)
        for result in (th.layout(root), th.ensure(root, stat=port),
                       th.acquire(root, "b1", 1, 1, alive=ALIVE, stat=port),
                       th.release(root, "0" * 32, stat=port), th.operator_reset(root, stat=port)):
            self.assertIsInstance(result, th.Refusal)
            self.assertIn(result.code, codes)
        self.assertEqual(calls, [])  # refused before any filesystem call

    def test_protected_roots_are_never_touched(self):
        for root in (os.path.join(self.base, "data", "talk"), os.path.join(self.base, "DATA"),
                     os.path.join(self.base, ".jarvis", "talk"), os.path.join(self.base, ".Jarvis"),
                     os.path.join(os.path.dirname(self.base), "data")):
            with self.subTest(root=root):
                self.assert_refused_untouched(root, ("ROOT_PROTECTED",))

    def test_hostile_path_parts_are_refused(self):
        def check(part):
            root = self.base + os.sep + part
            if part == ".Claude":  # DECISION: only data and .jarvis are protected roots here
                self.assertIsInstance(th.layout(root), th.Layout)
            else:
                self.assert_refused_untouched(root)
        each(self, HOSTILE_PATH_PARTS, check)

    def test_malformed_roots_are_refused(self):
        roots = (None, "", 5, b"C:\\talk", ["C:\\talk"], "talk", "C:talk", "\\\\host\\share\\talk",
                 "\\\\?\\C:\\talk", "\\\\.\\C:\\talk", "//host/share/talk", self.root + os.sep,
                 self.base + os.sep + "a" * 250, self.root + ":stream", self.root + "\x00")
        each(self, roots, self.assert_refused_untouched)
        for stem in ("NUL .txt", "COM1 .log", "nul  .x", "CONIN$ .txt", "Aux .a.b"):  # F3: spaces end the stem
            with self.subTest(stem=stem):
                self.assert_refused_untouched(self.base + os.sep + stem, ("ROOT_INVALID",))
        each(self, HOSTILE_TEXT, lambda text: self.assert_refused_untouched(self.base + os.sep + text))


def _record(boot="b0", pid=7, since=5, token="a" * 32):
    return json.dumps({"version": 1, "boot_id": boot, "pid": pid, "since": since, "token": token})


class LockTests(_Home):
    def read_lock(self):
        return json.loads(Path(self.lock).read_bytes())

    def test_acquire_writes_record_and_second_acquire_names_holder(self):
        lease = self.acquire()
        self.assertIsInstance(lease, th.Lease)
        self.assertEqual((lease.note, lease.lock_path), ("", self.lock))
        self.assertEqual(self.read_lock(), {"version": 1, "boot_id": "b1", "pid": 100,
                                            "since": 1000, "token": lease.token})
        held = self.acquire(pid=200, now=2000)
        self.refused(held, "HELD")
        self.assertIn("pid 100", held.text)
        self.assertIn("boot b1", held.text)
        self.assertEqual(self.read_lock()["token"], lease.token)

    def test_lock_from_earlier_boot_is_taken_over(self):
        self.write_lock(_record(boot="b0").encode())
        lease = self.acquire(boot="b1")
        self.assertEqual(lease.note, th.INTERRUPTED)
        self.assertEqual(self.read_lock()["token"], lease.token)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.lock))), [th.LOCK_NAME, th.GUARD_NAME])

    def test_dead_holder_is_taken_over_odd_liveness_is_alive(self):
        self.write_lock(_record(boot="b1", pid=7).encode())
        seen = []
        lease = self.acquire(alive=lambda pid: seen.append(pid) or False)
        self.assertEqual((lease.note, seen), (th.INTERRUPTED, [7]))

        def boom(pid):
            raise RuntimeError("x")

        def deep(pid):
            raise RecursionError

        for alive in (boom, deep, lambda pid: None, lambda pid: "no", lambda pid: 0):
            with self.subTest(alive=alive):
                self.refused(self.acquire(alive=alive, pid=300), "HELD")
        self.assertEqual(self.read_lock()["token"], lease.token)

    def test_unreadable_lock_is_unknown_holder_until_operator_reset(self):
        valid = _record(boot="b1").encode()
        corrupt = [b"{", b"", valid[:-5], valid[:-1], b"\xff" + valid,
                   _record(boot="b1").replace('"token"', '"pid": 1, "token"').encode(),
                   _record(token="A" * 32).encode(), _record(pid=True).encode(),
                   valid.replace(b'"version": 1', b'"version": 2')]
        corrupt += [text.encode() for text in HOSTILE_JSON]

        def check(data):
            self.write_lock(data)
            calls = []
            for boot in ("b1", "b9"):
                result = self.acquire(boot=boot, alive=lambda pid: calls.append(pid) or False)
                self.refused(result, "UNKNOWN_HOLDER")
            self.refused(th.release(self.root, "a" * 32), "UNKNOWN_HOLDER")
            self.assertEqual((calls, Path(self.lock).read_bytes()), ([], data))
        each(self, corrupt, check)

        padded = _record(boot="b1").encode()  # F4: valid JSON, only whitespace makes it big
        self.write_lock(padded + b" " * (th.MAX_LOCK_BYTES - len(padded)))
        self.assertEqual(self.acquire(alive=DEAD).note, th.INTERRUPTED)  # exactly MAX still parses
        self.write_lock(padded + b" " * (th.MAX_LOCK_BYTES + 1 - len(padded)))
        self.refused(self.acquire(alive=DEAD), "UNKNOWN_HOLDER")
        self.assertIs(th.operator_reset(self.root), True)
        self.assertIs(th.operator_reset(self.root), False)
        self.assertEqual(self.acquire().note, "")

    def test_contenders_during_a_takeover_are_busy_and_one_wins(self):
        self.write_lock(_record(boot="b1", pid=7).encode())
        others = []

        def alive(pid):  # runs inside the takeover's critical section
            if not others:
                others.extend([self.acquire(pid=8, alive=DEAD), self.acquire(boot="b2", pid=9, alive=DEAD),
                               th.release(self.root, "a" * 32), th.operator_reset(self.root)])
            return False

        mine = self.acquire(pid=10, alive=alive)
        for other in others:
            self.refused(other, "LOCK_BUSY")
        self.assertEqual(len(others), 4)
        self.assertEqual(mine.note, th.INTERRUPTED)
        self.assertEqual(self.read_lock()["token"], mine.token)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.lock))), [th.LOCK_NAME, th.GUARD_NAME])

    def test_takeover_during_a_release_is_busy(self):
        lease = self.acquire()
        rival = []

        def port(path):
            if path == self.lock and not rival:
                rival.append(self.acquire(boot="b9", pid=8, alive=DEAD))
            return os.lstat(path)

        self.assertIs(th.release(self.root, lease.token, stat=port), True)
        self.refused(rival[0], "LOCK_BUSY")
        self.assertFalse(os.path.exists(self.lock))

    def test_guard_is_free_after_every_exit(self):
        self.write_lock(_record(boot="b1").encode())
        self.refused(self.acquire(), "HELD")

        def deep(path):
            if path == self.lock:
                raise RecursionError
            return os.lstat(path)

        self.refused(self.acquire(stat=deep), "INPUT_TOO_DEEP")
        self.refused(th.release(self.root, "a" * 32, stat=deep), "INPUT_TOO_DEEP")
        self.write_lock(b"{")
        self.refused(self.acquire(), "UNKNOWN_HOLDER")
        self.assertIs(th.operator_reset(self.root), True)
        self.assertEqual(self.acquire().note, "")

    def test_guard_that_is_a_link_or_folder_is_refused(self):
        self.write_lock(_record().encode())
        guard = os.path.join(self.root, "state", th.GUARD_NAME)
        port = _fake_stat(guard, st_mode=stat.S_IFLNK)
        self.refused(self.acquire(stat=port), "LINK_REFUSED")
        self.refused(th.release(self.root, "a" * 32, stat=port), "LINK_REFUSED")
        self.refused(th.operator_reset(self.root, stat=port), "LINK_REFUSED")
        os.mkdir(guard)
        self.refused(self.acquire(alive=DEAD), "IO_ERROR")
        self.refused(th.release(self.root, "a" * 32), "IO_ERROR")
        self.assertTrue(os.path.exists(self.lock))

    def test_failed_takeover_keeps_the_stale_lock_and_denied_guard_is_io_error(self):
        stale = _record(boot="b0").encode()
        self.write_lock(stale)

        def full(fd, data):
            raise OSError(28, "no space")

        with mock.patch.object(th.os, "write", full):
            self.refused(self.acquire(), "IO_ERROR")
        self.assertEqual(Path(self.lock).read_bytes(), stale)  # the note is not lost with the write
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.lock))), [th.LOCK_NAME, th.GUARD_NAME])
        guard, real_open = os.path.join(self.root, "state", th.GUARD_NAME), os.open

        def denied(path, flags, *args):
            if path == guard:
                raise PermissionError(13, "denied")
            return real_open(path, flags, *args)

        with mock.patch.object(th.os, "open", denied):
            self.refused(self.acquire(), "IO_ERROR")  # not LOCK_BUSY: nobody holds it
            self.refused(th.operator_reset(self.root), "IO_ERROR")
        self.assertEqual(self.acquire().note, th.INTERRUPTED)

    def test_race_on_atomic_create_has_exactly_one_winner(self):
        self.assertIsInstance(th.ensure(self.root), th.Layout)
        real_open, rival = os.open, _record(boot="b1", pid=8, token="c" * 32).encode()
        excl = []

        def opener(path, flags, *args):  # F5: the rival creates at the very open the outer O_EXCL makes
            if path == self.lock and flags & os.O_EXCL and not excl:
                excl.append(path)
                fd = real_open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
                os.write(fd, rival)
                os.close(fd)
            return real_open(path, flags, *args)

        with mock.patch.object(th.os, "open", opener):
            mine = self.acquire(pid=9)
        self.assertEqual(excl, [self.lock])
        self.refused(mine, "HELD")
        self.assertIn("pid 8", mine.text)
        self.assertEqual(Path(self.lock).read_bytes(), rival)

    def test_release_needs_the_holders_token(self):
        lease = self.acquire()
        before = Path(self.lock).read_bytes()
        self.refused(th.release(self.root, "b" * 32), "WRONG_TOKEN")
        for bad in (None, "", lease.token.upper(), lease.token + "0", lease.token[:-1], 5):
            self.refused(th.release(self.root, bad), "TOKEN_INVALID")
        self.assertEqual(Path(self.lock).read_bytes(), before)
        self.assertIs(th.release(self.root, lease.token), True)
        self.assertFalse(os.path.exists(self.lock))
        self.refused(th.release(self.root, lease.token), "NO_LOCK")
        self.refused(th.release(os.path.join(self.base, "none"), lease.token), "NO_LOCK")
        self.assertEqual(self.acquire().note, "")

    def test_lock_reported_as_link_is_refused(self):
        self.write_lock(_record().encode())
        port = _fake_stat(self.lock, st_mode=stat.S_IFLNK)
        self.refused(self.acquire(stat=port), "LINK_REFUSED")
        self.refused(th.operator_reset(self.root, stat=port), "LINK_REFUSED")
        self.assertTrue(os.path.exists(self.lock))

    def test_bad_arguments_are_refused_before_any_folder_is_made(self):
        each(self, (None, "", "a" * 65, "a b", "a/b", 5) + HOSTILE_TEXT,
             lambda boot: self.refused(self.acquire(boot=boot), "BOOT_ID_INVALID"))
        each(self, HOSTILE_NUMBERS + (0, -1, 2**32),
             lambda pid: self.refused(self.acquire(pid=pid), "PID_INVALID"))
        each(self, HOSTILE_NUMBERS + (-1, 2**53),
             lambda now: self.refused(self.acquire(now=now), "NOW_INVALID"))
        self.refused(self.acquire(alive=None), "ALIVE_INVALID")
        self.assertFalse(os.path.exists(self.root))

    def test_deep_recursion_in_a_port_is_a_refusal(self):
        def port(path):
            raise RecursionError

        self.refused(th.ensure(self.root, stat=port), "INPUT_TOO_DEEP")
        self.refused(self.acquire(stat=port), "INPUT_TOO_DEEP")

    def test_refusal_texts_are_ascii(self):
        self.assertTrue(all(text.isascii() for text in th.REFUSALS.values()))


class RepoTests(unittest.TestCase):
    def test_gitignore_has_talk_exactly_once(self):
        lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count("/talk/"), 1)

    def test_module_never_reaches_home_or_data(self):
        src = (REPO / "jarvis" / "talk_home.py").read_text(encoding="utf-8")
        bad = r"expanduser|Path\.home|\.environ|getenv|realpath|\.resolve\(|jarvis\.db|^\s*(from|import) jarvis"
        self.assertIsNone(re.search(bad, src, re.M))

    def test_nothing_in_the_app_imports_the_module(self):
        users = [p for p in (REPO / "jarvis").rglob("*.py")
                 if p.name != "talk_home.py" and "talk_home" in p.read_text(encoding="utf-8")]
        self.assertEqual(users, [])

    def test_durations_cover_every_test_class(self):
        data = json.loads((REPO / "scripts" / "test_durations.d" / "TALKHOME1.json").read_text())
        names = {f"{__name__}.{n}" for n, v in globals().items()
                 if isinstance(v, type) and issubclass(v, unittest.TestCase) and not n.startswith("_")}
        self.assertEqual(set(data["classes"]), names)
