"""BRAINFS1: brain read-only path policy and file tools backend.

Every root is a tempfile.TemporaryDirectory; nothing here opens the real data/
or any database. The "jarvis.db" files below are fake bytes written in temp.
"""

import ast
import contextlib
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from jarvis import brain_fs
from jarvis.brain_fs import BrainFiles, Refusal, Resolved
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, each

MODULE = pathlib.Path(brain_fs.__file__)


def frozen_clock():
    return 0.0


class Spy:
    """Recording realpath / is_link port; armed after the constructor's root call."""

    def __init__(self):
        self.calls = []
        self.armed = False

    def realpath(self, path):
        if self.armed:
            self.calls.append(("realpath", path))
        return os.path.realpath(path)

    def is_link(self, path):
        if self.armed:
            self.calls.append(("is_link", path))
        return brain_fs._is_link(path)


class RootCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "repo")
        self.outside = os.path.join(tmp.name, "outside")
        os.makedirs(self.outside)
        self.write("outside/secret.txt", b"outside secret\n", base=tmp.name)
        files = {
            "jarvis/a.py": b"print('hello world')\nx = 1\n",
            "docs/notes.md": b"hello docs\n",
            "talk/notes.md": b"talk notes hello\n",
            "talk/state/turn.json": b'{"hello": 1}\n',
            "data/jarvis.db": b"SQLite format 3\x00fake",
            ".env": b"KEY=hello\n",
            ".git/config": b"hello git\n",
            "keys/x.pem": b"-----BEGIN KEY-----\nhello\n",
            ".claude/worktrees/w1/data/x.txt": b"hello nested data\n",
        }
        for rel, data in files.items():
            self.write(rel, data)
        self.fs = BrainFiles(self.root, clock=frozen_clock)

    def write(self, rel, data, base=None):
        path = os.path.join(base or self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def assertRefused(self, result, code=None):
        self.assertIsInstance(result, Refusal)
        self.assertTrue(result.reason.isascii(), result)
        if code is not None:
            self.assertEqual(result.code, code, result)


class ResolveTests(RootCase):
    def test_allowed_path_returns_real_path_inside_root(self):
        got = self.fs.resolve("jarvis/a.py")
        self.assertIsInstance(got, Resolved)
        self.assertEqual(got.rel, "jarvis/a.py")
        real_root = os.path.realpath(self.root)
        self.assertEqual(os.path.commonpath([real_root, got.path]), real_root)
        self.assertTrue(os.path.isfile(got.path))

    def test_empty_request_is_the_root(self):
        got = self.fs.resolve("")
        self.assertEqual(got, Resolved(os.path.realpath(self.root), ""))

    def test_module_level_resolve(self):
        self.assertEqual(brain_fs.resolve(self.root, "docs/notes.md").rel, "docs/notes.md")
        self.assertRefused(brain_fs.resolve(self.root, "data/jarvis.db"), "refused_name")

    def test_traversal_absolute_drive_unc_home_env_forms(self):
        # Negative 1.
        other_drive = "D:/x" if os.path.splitdrive(self.root)[0].upper() != "D:" else "E:/x"
        forms = ["..", "../outside/secret.txt", "jarvis/../../outside/secret.txt", "jarvis/..",
                 self.outside, self.outside.replace("\\", "/"), "/etc/passwd", other_drive, "C:x",
                 "\\\\host\\share\\x", "//host/share/x", "\\\\?\\C:\\x", "//?/C:/x", "\\\\.\\C:\\x",
                 "~", "~/x", "~user/x", "%USERPROFILE%/x", "$HOME/x", "${HOME}/x", "x/$env:TEMP"]
        each(self, forms, lambda form: self.assertRefused(self.fs.resolve(form)))


def sample(glob):
    return glob.replace("*", "x")


def calls(fs):
    """The four read-only entry points, each taking one path."""
    return (fs.resolve, fs.read_file, fs.list_dir, lambda p: fs.search("hello", p))


class RefusedNameTests(RootCase):
    def test_every_refused_name_at_any_depth_through_every_call(self):
        # Acceptance 2: one constant, matched per segment, at the root and nested.
        for rule in brain_fs.REFUSED_NAMES:
            rel = "/".join(sample(g) for g in rule)
            places = [rel, "docs/" + rel, "docs/" + rel + "/inner.txt", ".claude/worktrees/w1/" + rel]
            for place in places:
                for call in calls(self.fs):
                    with self.subTest(place=place, call=call):
                        self.assertRefused(call(place), "refused_name")

    def test_case_and_look_alike_variants(self):
        # Negative 4: case folding and NFKC (full-width, Kelvin sign) before matching.
        variants = ["Data", "DATA/jarvis.db", "\uff24\uff21\uff34\uff21", ".ENV", ".Env.local",
                    "\uff0eenv", ".GIT", ".Git/config", "x.KEY", "x.Pem", "cert.PFX", "jarvis.DB",
                    "a.sqlite3", "a.SQLITE-wal", "db-wal", "db-SHM", "jarvis.db-journal",
                    "TALK/State", "talk/STATE/turn.json"]
        each(self, variants, lambda v: self.assertRefused(self.fs.resolve(v), "refused_name"))
        # The Kelvin sign is not NFC, so it is refused one step earlier, as a suspicious form.
        self.assertRefused(self.fs.resolve("x.\u212aey"), "form")

    def test_nested_worktree_data_refused_but_worktree_listed(self):
        self.assertRefused(self.fs.read_file(".claude/worktrees/w1/data/x.txt"), "refused_name")
        listing = self.fs.list_dir(".claude/worktrees/w1")
        self.assertIsInstance(listing, brain_fs.Listing)
        self.assertEqual(listing.entries, ())

    def test_constant_is_one_tuple_with_every_required_glob(self):
        names = brain_fs.REFUSED_NAMES
        self.assertIsInstance(names, tuple)
        for need in [("data",), (".git",), (".env*",), ("*.key",), ("*.pem",), ("*.pfx",), ("*.db",),
                     ("*.sqlite*",), ("*-wal",), ("*-shm",), ("talk", "state")]:
            self.assertIn(need, names)


# Negative 2: refused from the raw text alone, before any realpath / stat port call.
NO_RESOLVE_FORMS = ["DATA~1", "ENV~1", "docs/GITHUB~1", "x:stream", "x::$DATA", "a.", "a ", " a",
                    "docs/notes.md.", "CON", "con.txt", "NUL.png", "COM\u00b9", "lpt9", "a/b\\c",
                    "..", ".", "docs/../data", "docs//notes.md", "docs/", "/docs", "C:x", "C:/x",
                    "\\\\?\\C:\\x", "\\\\host\\share", "//host/share", "~", "~/x", "%TEMP%",
                    "$HOME", "a\uff0fb", "e\u0301", "a\u200bb", "a\u202eb", "a\u034fb", "data",
                    ".env"]

# HOSTILE_PATH_PARTS entries that are legitimate repo-relative names, not suspicious forms.
EXPECTED_ALLOWED = {".Claude", ".GITHUB/workflows", ".Jarvis", "a\u2215b", ".cl\u0430ude",
                    "g\u0435t", "d\u043ecs"}


class SuspiciousFormTests(RootCase):
    def spied(self):
        spy = Spy()
        fs = BrainFiles(self.root, realpath=spy.realpath, is_link=spy.is_link, clock=frozen_clock)
        spy.armed = True
        return spy, fs

    def test_forms_refused_without_resolving(self):
        spy, fs = self.spied()
        for form in NO_RESOLVE_FORMS:
            for call in calls(fs):
                with self.subTest(form=form, call=call):
                    got = call(form)
                    self.assertRefused(got)
                    self.assertIn(got.code, ("form", "refused_name"))
        self.assertEqual(spy.calls, [])

    def check_part(self, spy, fs, part):
        before = len(spy.calls)  # counted before resolve, so a port call inside it shows
        got = fs.resolve(part)
        if part in EXPECTED_ALLOWED:
            self.assertIsInstance(got, Resolved)
            real_root = os.path.realpath(self.root)
            self.assertEqual(os.path.commonpath([real_root, got.path]), real_root)
        else:
            self.assertRefused(got)
            self.assertEqual(spy.calls[before:], [])

    def test_hostile_path_parts_corpus(self):
        spy, fs = self.spied()
        each(self, HOSTILE_PATH_PARTS, lambda part: self.check_part(spy, fs, part))

    def test_corpus_check_catches_a_port_call_before_refusal(self):
        # BRAINFS1FIX F1: a resolve that touches the file system and then refuses must fail the check.
        spy, fs = self.spied()

        class Leaky:
            @staticmethod
            def resolve(part):
                spy.realpath(self.root)
                return fs.resolve(part)

        with self.assertRaises(AssertionError):
            self.check_part(spy, Leaky, "..")

    def test_allowed_path_goes_through_the_ports(self):
        spy, fs = self.spied()
        self.assertIsInstance(fs.resolve("docs/notes.md"), Resolved)
        self.assertIn("realpath", [c[0] for c in spy.calls])
        self.assertIn("is_link", [c[0] for c in spy.calls])


class LinkTests(RootCase):
    """Negative 3: links and junctions, through the injectable stat port and for real."""

    def mapped(self, target):
        real_root = os.path.realpath(self.root)
        docs = os.path.join(real_root, "docs")

        def realpath(path):
            real = os.path.realpath(path)
            if os.path.normcase(real).startswith(os.path.normcase(docs)):
                return target + real[len(docs):]
            return real

        return BrainFiles(self.root, realpath=realpath, is_link=lambda p: False, clock=frozen_clock)

    def test_fake_realpath_outside_refused_or_other_place(self):
        real_root = os.path.realpath(self.root)
        cases = [(os.path.realpath(self.outside), "outside"),
                 (os.path.join(real_root, "data"), "refused_name"),
                 (os.path.join(real_root, "jarvis"), "link")]
        for target, code in cases:
            fs = self.mapped(target)
            for call in calls(fs)[:3]:
                with self.subTest(target=target, call=call):
                    self.assertRefused(call("docs/notes.md"), code)

    def test_stat_port_reporting_a_link_refuses(self):
        fs = BrainFiles(self.root, is_link=lambda p: p.endswith("docs"), clock=frozen_clock)
        for call in calls(fs)[:3]:
            self.assertRefused(call("docs/notes.md"), "link")
        names = [e.name for e in fs.list_dir("").entries]
        self.assertNotIn("docs", names)

    def make_link(self, target, name):
        link = os.path.join(self.root, name)
        try:
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(target, link)
            else:
                os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest("cannot create a link here: " + type(exc).__name__)
        self.addCleanup(os.rmdir if os.name == "nt" else os.unlink, link)
        return link

    def test_real_junction_outside_and_to_refused_folder(self):
        self.make_link(self.outside, "jlink")
        self.make_link(os.path.join(self.root, "data"), "dlink")
        for name in ("jlink", "dlink"):
            for call in calls(self.fs)[:3]:
                with self.subTest(name=name, call=call):
                    self.assertRefused(call(name), "link")
            self.assertRefused(self.fs.read_file(name + "/secret.txt"), "link")
        self.assertRefused(self.fs.read_file("dlink/jarvis.db"), "refused_name")
        names = [e.name for e in self.fs.list_dir("").entries]
        self.assertNotIn("jlink", names)
        self.assertNotIn("dlink", names)
        self.assertEqual(self.fs.search("outside secret").matches, ())
        self.assertEqual(self.fs.search("SQLite").matches, ())
        # Even with a blind stat port, the real-path comparison still refuses.
        self.write("data/plain.txt", b"plain\n")
        blind = BrainFiles(self.root, is_link=lambda p: False, clock=frozen_clock)
        self.assertRefused(blind.read_file("jlink/secret.txt"), "outside")
        self.assertRefused(blind.read_file("dlink/plain.txt"), "refused_name")


class ReadFileTests(RootCase):
    def test_text_offset_limit(self):
        got = self.fs.read_file("jarvis/a.py")
        self.assertEqual(got.text, "print('hello world')\nx = 1\n")
        self.assertEqual((got.total_lines, got.lines, got.truncated), (2, 2, False))
        self.assertEqual(self.fs.read_file("jarvis/a.py", offset=1, limit=1).text, "x = 1\n")
        self.assertTrue(self.fs.read_file("jarvis/a.py", limit=1).truncated)

    def test_long_text_is_capped(self):
        self.write("docs/long.txt", b"a" * 300_000)
        got = self.fs.read_file("docs/long.txt")
        self.assertEqual(len(got.text), brain_fs.MAX_TEXT_CHARS)
        self.assertTrue(got.truncated)

    def test_size_cap(self):
        # Acceptance 3.
        path = self.write("docs/big.txt", b"")
        os.truncate(path, brain_fs.MAX_FILE_BYTES + 1)
        self.assertRefused(self.fs.read_file("docs/big.txt"), "too_large")

    def test_binary_refused(self):
        self.write("docs/zero.txt", b"a\x00b")
        self.write("docs/latin.txt", b"\xff\xfeab")
        self.assertRefused(self.fs.read_file("docs/zero.txt"), "binary")
        self.assertRefused(self.fs.read_file("docs/latin.txt"), "binary")

    def test_secret_header_refused_whatever_the_name(self):
        # Negative 5: header sniff catches a renamed database or key.
        self.write("docs/notes.txt", b"SQLite format 3\x00rest")
        self.write("docs/readme.txt", b"\xef\xbb\xbf\n -----BEGIN PRIVATE KEY-----\nabc\n")
        self.assertRefused(self.fs.read_file("docs/notes.txt"), "secret_header")
        self.assertRefused(self.fs.read_file("docs/readme.txt"), "secret_header")

    def test_renamed_secret_without_header_is_out_of_scope(self):
        # Negative 5, stated scope: a renamed text dump with no header is readable.
        self.write("docs/dump.txt", b"INSERT INTO t VALUES (1);\n")
        self.assertIsInstance(self.fs.read_file("docs/dump.txt"), brain_fs.FileText)

    def test_directory_and_missing(self):
        self.assertIn(self.fs.read_file("docs").code, ("io", "not_file"))
        self.assertRefused(self.fs.read_file("docs/missing.txt"), "io")


def names(listing):
    return {entry.name for entry in listing.entries}


class ListDirTests(RootCase):
    def test_root_hides_every_refused_name(self):
        # Acceptance 4.
        got = self.fs.list_dir("")
        self.assertEqual(names(got), {".claude", "docs", "jarvis", "keys", "talk"})
        self.assertFalse(got.truncated)
        self.assertEqual(names(self.fs.list_dir("talk")), {"notes.md"})
        self.assertEqual(self.fs.list_dir("keys").entries, ())

    def test_suspicious_and_refused_entry_names_hidden(self):
        for rel in ("docs/x~1.txt", "docs/foo.DB", "docs/a.SQLITE", "docs/.Env.prod", "docs/k.Pem"):
            self.write(rel, b"hello\n")
        self.assertEqual(names(self.fs.list_dir("docs")), {"notes.md"})

    def test_refused_folder_is_a_refusal_not_an_empty_list(self):
        # Negative 7.
        for rel in ("data", "talk/state", ".git", "DATA", ".claude/worktrees/w1/data"):
            with self.subTest(rel=rel):
                self.assertRefused(self.fs.list_dir(rel))
        self.assertRefused(self.fs.list_dir("data"), "refused_name")

    def test_file_and_missing(self):
        self.assertIn(self.fs.list_dir("docs/notes.md").code, ("not_dir", "io"))
        self.assertRefused(self.fs.list_dir("missing"), "io")


class SearchTests(RootCase):
    def test_allowed_files_only(self):
        # Acceptance 5: "hello" is also in .env, .git, keys, talk/state and nested data.
        got = self.fs.search("hello")
        self.assertEqual({m.path for m in got.matches}, {"docs/notes.md", "jarvis/a.py", "talk/notes.md"})
        self.assertEqual(got.stopped, "")
        self.assertRefused(self.fs.search("hello", "data"), "refused_name")
        self.assertEqual({m.path for m in self.fs.search("hello", "docs").matches}, {"docs/notes.md"})

    def test_literal_matcher(self):
        self.write("docs/re.txt", b"a.*b literal\nab\naxxb\n")
        got = self.fs.search("a.*b", "docs")
        self.assertEqual([(m.path, m.line) for m in got.matches], [("docs/re.txt", 1)])

    def test_catastrophic_pattern_is_plain_text(self):
        # Negative 6: no regex engine, so "(a+)+$" cannot backtrack.
        self.write("docs/cat.txt", b"a" * 5000 + b"!\n")
        got = self.fs.search("(a+)+$")
        self.assertEqual((got.matches, got.stopped), ((), ""))

    def test_match_cap(self):
        self.write("docs/hits.txt", b"hit\n" * 300)
        got = self.fs.search("hit")
        self.assertEqual((len(got.matches), got.stopped), (brain_fs.MAX_MATCHES, "matches"))

    def test_file_cap(self):
        from unittest import mock
        with mock.patch.object(brain_fs, "MAX_SEARCH_FILES", 2):
            got = self.fs.search("hello")
        self.assertEqual((got.files_scanned, got.stopped), (2, "files"))

    def test_time_cap(self):
        # Negative 6: the clock budget stops the walk.
        ticks = [0]

        def clock():
            ticks[0] += 10
            return ticks[0]

        got = BrainFiles(self.root, clock=clock).search("hello")
        self.assertEqual(got.stopped, "time")

    def counted_scandir(self):
        """Patch the handle scanner with a wrapper counting the entries it yields."""
        real, seen = brain_fs._scan_fd, [0]

        def scan(fd):
            for entry in real(fd):
                seen[0] += 1
                yield entry

        patcher = mock.patch.object(brain_fs, "_scan_fd", scan)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_time_checked_while_listing_one_directory(self):
        # BRAINFS1FIX F2: one huge directory is not listed (and sorted) before the deadline check.
        for i in range(50):
            self.write(f"big/f{i:02}.txt", b"hello\n")
        ticks = [0]

        def clock():  # first call sets the deadline, every later call is past it
            ticks[0] += 1
            return 0.0 if ticks[0] == 1 else 100.0

        seen = self.counted_scandir()
        got = BrainFiles(self.root, clock=clock).search("hello", "big")
        self.assertEqual((got.stopped, got.files_scanned, got.matches), ("time", 0, ()))
        self.assertLessEqual(seen[0], 1)

    def test_entries_cap_per_directory(self):
        # BRAINFS1FIX F2: at most MAX_ENTRIES names are taken from one directory.
        for i in range(10):
            self.write(f"big/f{i:02}.txt", b"hello\n")
        seen = self.counted_scandir()
        with mock.patch.object(brain_fs, "MAX_ENTRIES", 3):
            got = self.fs.search("hello", "big")
        self.assertEqual((got.stopped, got.files_scanned, len(got.matches)), ("entries", 3, 3))
        self.assertLessEqual(seen[0], 4)

    def test_pattern_refusals(self):
        bad = ["", "a" * (brain_fs.MAX_PATTERN_CHARS + 1), None, 5, b"hello", "a\0", "a\nb", "a\rb"]
        each(self, bad, lambda p: self.assertRefused(self.fs.search(p), "pattern"))

    def test_match_text_capped(self):
        self.write("docs/wide.txt", b"needle" + b"x" * 1000 + b"\n")
        (match,) = self.fs.search("needle").matches
        self.assertEqual(len(match.text), brain_fs.MAX_LINE_CHARS)


class FinalPathTests(RootCase):
    """BRAINFS1FIX F3: the opened handle's final path is checked again (swap after resolve)."""

    def swapped_ports(self):
        real_root = os.path.realpath(self.root)

        def raises(fd):
            raise OSError("handle gone")

        return [
            ("outside", lambda fd: os.path.join(os.path.realpath(self.outside), "secret.txt"), "outside"),
            ("other drive", lambda fd: "Z:\\elsewhere\\x.txt" if os.name == "nt" else "/elsewhere/x", "outside"),
            ("refused part", lambda fd: os.path.join(real_root, "data", "jarvis.db"), "refused_name"),
            ("nested refused", lambda fd: os.path.join(real_root, ".claude", "worktrees", "w1", "data"),
             "refused_name"),
            ("other place", lambda fd: os.path.join(real_root, "jarvis", "a.py"), "link"),
            ("raises", raises, "link"),
            ("none", lambda fd: None, "link"),
            ("bytes", lambda fd: os.fsencode(real_root), "link"),
            ("empty", lambda fd: "", "link"),
            ("nul", lambda fd: real_root + "\0", "link"),
        ]

    def test_swapped_handle_refused_everywhere(self):
        for label, port, code in self.swapped_ports():
            with self.subTest(label):
                fs = BrainFiles(self.root, clock=frozen_clock, final_path=port)
                self.assertRefused(fs.read_file("docs/notes.md"), code)
                self.assertRefused(fs.list_dir("docs"), code)
                self.assertRefused(fs.list_dir(""), code)
                got = fs.search("hello")
                self.assertEqual((got.matches, got.stopped), ((), ""))

    def test_swapped_file_handle_skipped_in_search(self):
        # directories verify, but every file handle reports another place
        def port(fd):
            final = brain_fs._final_path(fd)
            return final if os.path.isdir(final) else os.path.join(os.path.realpath(self.root), "jarvis")

        got = BrainFiles(self.root, clock=frozen_clock, final_path=port).search("hello")
        self.assertEqual(got.matches, ())
        self.assertGreater(got.files_scanned, 0)

    def test_handles_are_closed_after_refusal(self):
        fs = BrainFiles(self.root, clock=frozen_clock, final_path=lambda fd: None)
        for _ in range(3):
            fs.read_file("docs/notes.md")
            fs.list_dir("docs")
            fs.search("hello")
        os.rename(os.path.join(self.root, "docs"), os.path.join(self.root, "docs2"))  # fails if a handle leaked

    def test_directory_swapped_after_check_not_followed(self):
        # BRAINFS1FIX review B1: entries come from the verified handle, never the path again.
        self.write("swap/inner.txt", b"hello swap\n")
        here, moved = os.path.join(self.root, "swap"), os.path.join(self.root, "swap_moved")
        real_scandir, swapped = os.scandir, []

        def port(fd):  # the attacker moves the checked folder and puts a junction there
            final = brain_fs._final_path(fd)
            if os.path.normcase(final) == os.path.normcase(os.path.realpath(here)) and not swapped:
                with contextlib.suppress(OSError):  # the delete-less handle should block this
                    os.rename(here, moved)
                    LinkTests.make_link(self, self.outside, "swap")
                    swapped.append(True)
            return final

        def scandir(target):
            self.assertNotIsInstance(target, str, "listing walked a path after the handle check")
            return real_scandir(target)

        fs = BrainFiles(self.root, clock=frozen_clock, final_path=port)
        with mock.patch.object(os, "scandir", scandir):
            listed = fs.list_dir("swap")
            found = fs.search("secret", "swap")
        if os.name == "nt" or not isinstance(listed, Refusal):  # POSIX may refuse the moved handle
            self.assertEqual(names(listed), {"inner.txt"})
        self.assertEqual(found.matches, ())

    def test_default_port_matches_resolved_path(self):
        real = os.path.realpath(os.path.join(self.root, "docs", "notes.md"))
        with open(real, "rb") as handle:
            self.assertEqual(os.path.normcase(brain_fs._final_path(handle.fileno())), os.path.normcase(real))
        fd = brain_fs._open_dir(os.path.dirname(real))
        try:
            self.assertEqual(os.path.normcase(brain_fs._final_path(fd)), os.path.normcase(os.path.dirname(real)))
        finally:
            os.close(fd)
        self.assertEqual(self.fs.read_file("docs/notes.md").text, "hello docs\n")
        self.assertIn("notes.md", names(self.fs.list_dir("docs")))
        self.assertTrue(self.fs.search("hello").matches)


class MalformedInputTests(RootCase):
    def test_wrong_types(self):
        # Negative 8.
        for value in (None, 5, b"docs", ["docs"], 1.5, object()):
            for call in calls(self.fs):
                with self.subTest(value=repr(value), call=call):
                    self.assertRefused(call(value), "type")

    def test_long_path_and_nul(self):
        for call in calls(self.fs):
            with self.subTest(call=call):
                self.assertRefused(call("a" * 10_000), "too_long")
                self.assertRefused(call("docs/a\0b"), "nul")

    def test_hostile_text_refused_except_plain_letter(self):
        def check(text):
            got = self.fs.resolve(text)
            if text == "ı":  # a plain letter: allowed, and it does not exist
                self.assertIsInstance(got, Resolved)
            else:
                self.assertRefused(got)

        each(self, HOSTILE_TEXT, check)

    def test_hostile_numbers_are_bad_arguments(self):
        each(self, HOSTILE_NUMBERS + (-1, 10**9 + 1), lambda n: self.assertRefused(
            self.fs.read_file("docs/notes.md", offset=n), "bad_argument"))
        each(self, HOSTILE_NUMBERS + (0, brain_fs.MAX_LINES + 1), lambda n: self.assertRefused(
            self.fs.read_file("docs/notes.md", limit=n), "bad_argument"))

    def test_bad_roots(self):
        file_root = self.write("docs/file_root.txt", b"x")

        def boom(path):
            raise RuntimeError("no")

        roots = [None, "", "relative/dir", "C:" + "a" * 2000, "x\0", file_root, os.path.join(self.root, "missing")]
        fss = [BrainFiles(r) for r in roots] + [BrainFiles(self.root, realpath=boom)]
        for fs in fss:
            for call in calls(fs):
                with self.subTest(call=call):
                    self.assertRefused(call("docs"), "bad_root")
        self.assertRefused(brain_fs.resolve(None, "docs"), "bad_root")

    def test_raising_stat_port_is_internal_not_an_exception(self):
        def boom(path):
            raise RuntimeError("no")

        fs = BrainFiles(self.root, is_link=boom)
        for call in calls(fs):
            with self.subTest(call=call):
                self.assertRefused(call("docs"), "internal")

    def test_ascii_sweep_no_exception_escapes(self):
        inputs = HOSTILE_PATH_PARTS + HOSTILE_TEXT + HOSTILE_NUMBERS + ("docs", "data", ".ENV")
        for call in calls(self.fs):
            for value in inputs:
                with self.subTest(call=call, value=repr(value)[:60]):
                    got = call(value)
                    if isinstance(got, Refusal):
                        self.assertRefused(got)


SOURCE = pathlib.Path(MODULE).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
BANNED_ATTRS = {
    "write", "writelines", "unlink", "rename", "renames", "replace", "remove", "removedirs", "rmdir",
    "mkdir", "makedirs", "chmod", "symlink", "link", "startfile", "system", "popen", "truncate",
}


class ModuleContractTests(unittest.TestCase):
    # Acceptance 6: no write, delete, rename or exec path exists in the module.
    def test_imports_are_read_only_stdlib(self):
        imported = set()
        for node in ast.walk(TREE):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(
            imported, {"__future__", "ctypes", "fnmatch", "msvcrt", "os", "stat", "time", "unicodedata", "typing"}
        )
        self.assertNotIn("subprocess", SOURCE)

    def test_every_open_is_binary_read(self):
        opens = [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "open"]
        self.assertTrue(opens)
        for node in opens:
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            self.assertEqual(modes, ["rb"])

    def test_no_mutating_or_exec_call(self):
        for node in ast.walk(TREE):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, BANNED_ATTRS)
                self.assertFalse(node.attr.startswith(("spawn", "exec")), node.attr)
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, {"exec", "eval", "compile", "__import__", "breakpoint"})

    def test_public_surface(self):
        public = {name for name in vars(BrainFiles) if not name.startswith("_")}
        self.assertEqual(public, {"resolve", "read_file", "list_dir", "search"})

    def test_running_app_does_not_import_module(self):
        package = pathlib.Path(MODULE).parent
        users = [p.name for p in package.rglob("*.py") if p.name != "brain_fs.py" and "brain_fs" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(users, [])
