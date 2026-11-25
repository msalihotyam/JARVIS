"""N6PUSHPATHS1: pure push path decision from git plumbing output."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from jarvis import n6_pushpaths
from jarvis.n6_pushpaths import ALLOW, NEEDS_MANUAL_KEY, REFUSE, decide

NEVER = ("uv.lock", ".env*", "data/", "*.db", "*.key", "*.pem")
KEY = (".claude/", ".github/workflows/", "scripts/conveyor.py", "scripts/orch_gen.py",
       "scripts/test_parallel.py", "scripts/n6_push.py")
S1 = b"1" * 40
Z40 = b"0" * 40


def drec(path: bytes, status=b"A", src=None, dst=None) -> bytes:
    src = src or (b"000000" if status == b"A" else b"100644")
    dst = dst or (b"000000" if status == b"D" else b"100644")
    return b":%s %s %s %s %s\0%s\0" % (
        src, dst, Z40 if status == b"A" else S1, Z40 if status == b"D" else S1, status, path)


def trec(path: bytes, mode=b"100644") -> bytes:
    return b"%s %s %s\t%s\0" % (mode, b"commit" if mode == b"160000" else b"blob", S1, path)


def hand(*paths: str):
    raw = [p.encode("utf-8") for p in paths]
    return decide(b"".join(map(drec, raw)), b"".join(map(trec, raw)), NEVER, KEY)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class RealPlumbingTests(unittest.TestCase):
    """Inputs are real `git diff-tree` / `git ls-tree` bytes from a temp repo."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "gitconfig").write_bytes(b"")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(cls.root / "gitconfig"),
                   HOME=str(cls.root))
        cls.env = env
        cls.git("init", "-q", "repo", cwd=cls.root)
        cls.repo = cls.root / "repo"
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def git(cls, *args, stdin=b"", cwd=None, env=None) -> bytes:
        cmd = ["git", "-c", "core.ignorecase=false", "-c", "core.protectNTFS=false", *args]
        return subprocess.run(cmd, cwd=cwd or cls.repo, input=stdin, env=env or cls.env,
                              capture_output=True, check=True).stdout

    def tree(self, files: dict) -> str:
        type(self).count += 1
        env = dict(self.env, GIT_INDEX_FILE=str(self.root / ("index%d" % self.count)))
        lines = b""
        for path, (mode, content) in files.items():
            sha = content if mode == b"160000" else self.git(
                "hash-object", "-w", "--stdin", stdin=content).strip()
            lines += b"%s %s\t%s\0" % (mode, sha, path)
        self.git("update-index", "-z", "--index-info", stdin=lines, env=env)
        return self.git("write-tree", env=env).decode("ascii").strip()

    def run_case(self, base: dict, cand: dict):
        b, c = self.tree(base), self.tree(cand)
        diff = self.git("diff-tree", "-r", "-z", "--raw", "--no-renames", b, c)
        return decide(diff, self.git("ls-tree", "-r", "-z", c), NEVER, KEY)

    def added(self, *paths: bytes, mode=b"100644"):
        base = {b"README.md": (b"100644", b"base\n")}
        content = S1 if mode == b"160000" else b"x\n"
        return self.run_case(base, {**base, **{p: (mode, content) for p in paths}})

    def test_acc_plain_change_allows(self):
        base = {b"README.md": (b"100644", b"a\n"), b"jarvis/old.py": (b"100644", b"o\n")}
        cand = {b"README.md": (b"100644", b"b\n"), b"jarvis/new.py": (b"100644", b"n\n")}
        self.assertEqual(self.run_case(base, cand), (ALLOW, ()))

    def test_acc_never_list_refuses(self):
        for path in (b"uv.lock", b".env.local", b"data/x.json", b"a/b.db", b"k.key", b"c.PEM"):
            with self.subTest(path=path):
                got = self.added(path)
                self.assertEqual(got.verdict, REFUSE)
                self.assertIn("never-list path", got.reasons[0])

    def test_acc_manual_key_paths(self):
        for path in (b".claude/settings.json", b".github/workflows/ci.yml", b"scripts/conveyor.py"):
            with self.subTest(path=path):
                self.assertEqual(self.added(path), (NEEDS_MANUAL_KEY, (path.decode() + ": manual-key path",)))

    def test_neg8_case_variants_need_key_never_allow(self):
        for path in (b".CLAUDE/settings.json", b".Claude/x", b".github/WORKFLOWS/x"):
            with self.subTest(path=path):
                self.assertEqual(self.added(path).verdict, NEEDS_MANUAL_KEY)

    def test_neg4_case_fold_collision(self):
        for pair in ((b"A.txt", b"a.txt"), (b"Docs/x", b"docs/y"), (b"Lib", b"lib/z")):
            with self.subTest(pair=pair):
                got = self.added(*pair)
                self.assertEqual(got.verdict, REFUSE)
                self.assertIn("case-fold collision", " ".join(got.reasons))

    def test_neg5_symlink_submodule_gitmodules(self):
        self.assertEqual(self.added(b"link", mode=b"120000").verdict, REFUSE)
        self.assertEqual(self.added(b"sub", mode=b"160000").verdict, REFUSE)
        changed = self.run_case({b"f": (b"100644", b"t")}, {b"f": (b"120000", b"t")})
        self.assertIn("f: symlink or submodule mode", changed.reasons)
        self.assertEqual(self.added(b".gitmodules").reasons, (".gitmodules: .gitmodules changed",))

    def test_neg6_python_shadowing(self):
        for paths in ((b"pkg/__init__.py", b"pkg.py"), (b"m.py", b"m.pyc"), (b"m.py", b"m.pyd"),
                      (b"m.py", b"m.pyw"), (b"m.py", b"m.cp312-win_amd64.pyd"), (b"x.pth",),
                      (b"lib/sitecustomize.py",), (b"usercustomize.py",)):
            with self.subTest(paths=paths):
                self.assertEqual(self.added(*paths).verdict, REFUSE)

    def test_neg3_non_utf8_and_non_nfc_from_git(self):
        got = self.added(b"bad\xff.txt")
        self.assertEqual(got, (REFUSE, ("bad\\xff.txt: path not valid UTF-8",)))
        got = self.added("cafe\u0301.txt".encode("utf-8"))
        self.assertEqual(got, (REFUSE, ("cafe\\xcc\\x81.txt: path not NFC",)))

    def test_neg11_empty_diff(self):
        same = {b"README.md": (b"100644", b"a\n")}
        self.assertEqual(self.run_case(same, same), (REFUSE, ("nothing to push: empty diff",)))

    def test_neg12_deletions(self):
        base = {b"README.md": (b"100644", b"a"), b".claude/x": (b"100644", b"c"),
                b"uv.lock": (b"100644", b"u")}
        no_key = {k: v for k, v in base.items() if k != b".claude/x"}
        self.assertEqual(self.run_case(base, no_key), (NEEDS_MANUAL_KEY, (".claude/x: manual-key path",)))
        no_lock = {k: v for k, v in base.items() if k != b"uv.lock"}
        self.assertEqual(self.run_case(base, no_lock), (REFUSE, ("uv.lock: never-list path",)))


class SuspiciousFormTests(unittest.TestCase):
    """Hand-built bytes for forms the OS or git refuses to create."""

    def assertRefused(self, *paths, why=""):
        for path in paths:
            with self.subTest(path=path):
                got = hand(path)
                self.assertEqual(got.verdict, REFUSE)
                self.assertIn(why, " ".join(got.reasons))

    def test_neg1_short_names(self):
        self.assertRefused("CLAUDE~1/settings.json", "GITHUB~1/workflows/x", "GIT~1/config",
                           "a/progra~12", why="8.3 short name")

    def test_neg2_trailing_dot_space_and_bad_characters(self):
        self.assertRefused("a./x", "x/b ", " lead", why="space or ends in dot")
        self.assertRefused("a:b", "a\\b", "a\x01b", "a\u202eb", "a\u200bb", "a\ufeffb",
                           "a\u00a0b", "a\uff1ab", "a\uff0fb", why="reserved, control, bidi")

    def test_neg2_default_ignorable_code_points(self):
        # Every Default_Ignorable_Code_Point range, Mn/Lo/Cf/Cn alike, first and last of each.
        points = (0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180D,
                  0x180E, 0x180F, 0x200B, 0x200F, 0x202A, 0x202E, 0x2060, 0x2065, 0x206F,
                  0x3164, 0xFE00, 0xFE0F, 0xFEFF, 0xFFA0, 0xFFF0, 0xFFF8, 0x1BCA0, 0x1BCA3,
                  0x1D173, 0x1D17A, 0xE0000, 0xE0001, 0xE0020, 0xE007F, 0xE0100, 0xE01EF,
                  0xE0FFF,
                  # Blank-rendering, not Default_Ignorable (F2 hits): braille blank, joiner,
                  # object replacement, null notehead.
                  0x2800, 0x2D7F, 0xFFFC, 0x1D159)
        for cp in points:
            ch = chr(cp)
            self.assertRefused(f".e{ch}nv", f".g{ch}it/config", ch + "lead", "trail" + ch,
                               f"dir/{ch}/x", why="zero-width")

    def test_neg2_ignorable_review_probe_paths(self):
        self.assertRefused(".g\u034fit/config", ".e\u034fnv", ".e\u180bnv", ".e\u3164nv",
                           ".e\u115fnv", ".e\ufe00nv", ".e\u17b4nv", ".e\U000e0100nv",
                           ".g\u200bit/config", why="zero-width")

    def test_visible_non_ascii_still_allowed(self):
        for path in ("caf\u00e9.txt", "docs/\u00fc\u011f\u015f\u0131.md", "\u65e5\u672c/a.py",
                     "\ud55c\uae00.txt","\u0915\u094d\u0937.txt"):
            with self.subTest(path=path):
                self.assertEqual(hand(path), (ALLOW, ()))

    def test_neg2_reserved_device_names(self):
        self.assertRefused("CON", "nul.txt", "x/Aux.tar.gz", "COM1.py", "lpt9", "com\u00b9",
                           "PRN ", why="")
        self.assertRefused("CON", "nul.txt", "COM1.py", "lpt9", "com\u00b9", why="device name")

    def test_neg7_dot_git_segment(self):
        self.assertRefused(".git/config", ".GIT/hooks/x", "sub/.Git/x", "\uff0egit/x", why=".git")

    def test_dot_segments_and_empty(self):
        self.assertRefused("a//b", "./a", "a/../b", why="empty or dot segment")

    def test_neg10_changed_path_missing_from_tree(self):
        got = decide(drec(b"ghost.py"), trec(b"other.py"), NEVER, KEY)
        self.assertEqual(got, (REFUSE, ("ghost.py: changed path missing from the candidate tree",)))
        got = decide(drec(b"gone.py", b"D"), trec(b"gone.py"), NEVER, KEY)
        self.assertEqual(got.verdict, REFUSE)

    def test_nfkc_fold_hits_policy(self):
        self.assertEqual(hand("\uff0eclaude/x").verdict, NEEDS_MANUAL_KEY)
        self.assertEqual(hand("UV.LOCK").verdict, REFUSE)
        self.assertEqual(hand("\uff44ata/x").verdict, REFUSE)


class ReviewBypassTests(unittest.TestCase):
    """Bypasses found in review: each one must be REFUSE."""

    def test_pycache_and_init_with_other_suffix(self):
        for paths in (("j/foo.py", "j/__pycache__/foo.cpython-312.pyc"),
                      ("pkg.py", "pkg/__init__.pyc"), ("pkg.py", "pkg/__init__.pyd"),
                      ("pkg.py", "pkg/__init__.pyw")):
            with self.subTest(paths=paths):
                got = hand(*paths)
                self.assertEqual(got.verdict, REFUSE)
                self.assertTrue(any("python module shadowing" in r for r in got.reasons))

    def test_startup_hook_as_package(self):
        for path in ("sitecustomize/__init__.py", "a/UserCustomize/__init__.py"):
            with self.subTest(path=path):
                self.assertIn("python startup hook", hand(path).reasons[0])

    def test_deep_path_is_linear(self):
        deep = b"a/" * 200_000 + b"b"
        got = decide(drec(deep), trec(deep), NEVER, KEY)
        self.assertEqual(got, (ALLOW, ()))


class MalformedInputTests(unittest.TestCase):
    """neg 9: every malformed input is REFUSE and no exception escapes."""

    GOOD_DIFF = drec(b"a.py", b"M")
    GOOD_TREE = trec(b"a.py")

    def refused(self, diff=GOOD_DIFF, tree=GOOD_TREE, never=NEVER, key=KEY):
        got = decide(diff, tree, never, key)
        self.assertEqual(got.verdict, REFUSE)
        self.assertEqual(len(got.reasons), 1)
        return got.reasons[0]

    def test_good_baseline_allows(self):
        self.assertEqual(decide(self.GOOD_DIFF, self.GOOD_TREE, NEVER, KEY), (ALLOW, ()))

    def test_truncated_and_unterminated(self):
        self.assertIn("truncated", self.refused(diff=self.GOOD_DIFF[:-1]))
        self.assertIn("truncated", self.refused(diff=self.GOOD_DIFF.split(b"\0")[0] + b"\0"))
        self.assertIn("truncated", self.refused(tree=self.GOOD_TREE[:-1]))
        self.assertIn("malformed", self.refused(diff=self.GOOD_DIFF[:30] + b"\0a.py\0"))

    def test_unknown_status_and_bad_modes(self):
        for status in (b"X", b"U", b"R", b"C", b"m"):
            with self.subTest(status=status):
                self.assertIn("unknown status", self.refused(diff=drec(b"a.py", status)))
        for mode in (b"10064x", b"100648", b"10644", b"1006444"):
            with self.subTest(mode=mode):
                self.refused(diff=drec(b"a.py", b"M", dst=mode))
                self.refused(tree=trec(b"a.py", mode))
        self.assertIn("modes do not fit", self.refused(diff=drec(b"a.py", b"A", src=b"100644")))
        self.assertIn("not a tree mode", self.refused(diff=drec(b"a.py", b"M", dst=b"040000")))
        self.assertIn("malformed", self.refused(tree=trec(b"a.py", b"160000").replace(b"commit", b"blob")))

    def test_rename_score_and_garbage_records(self):
        self.refused(diff=drec(b"a.py", b"M").replace(b" M\0", b" R100\0"))
        self.refused(diff=b"\0\0")
        self.refused(tree=b"garbage\0")
        self.refused(diff=drec(b"a.py", b"M") * 2, tree=self.GOOD_TREE)
        self.refused(tree=self.GOOD_TREE * 2)

    def test_100k_records_refused_fast(self):
        many = drec(b"a.py", b"M") * 100_000
        self.assertEqual(self.refused(diff=many), "diff-tree input too large")
        self.assertEqual(self.refused(tree=trec(b"a.py") * 100_000), "ls-tree input too large")

    def test_wrong_types_and_policy(self):
        self.assertIn("not bytes", self.refused(diff="text"))
        self.assertIn("not bytes", self.refused(tree=None))
        self.assertIn("never-list", self.refused(never=()))
        self.assertIn("never-list", self.refused(never="uv.lock"))
        self.assertIn("manual-key", self.refused(key=("",)))
        self.assertIn("manual-key", self.refused(key=(None,)))


class ModuleContractTests(unittest.TestCase):
    SOURCE = Path(n6_pushpaths.__file__).read_text(encoding="utf-8")

    def test_acc4_no_os_pathlib_subprocess_socket_import(self):
        banned = {"os", "pathlib", "subprocess", "socket", "shutil", "importlib", "ctypes"}
        for node in ast.walk(ast.parse(self.SOURCE)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                self.assertNotIn(name.split(".")[0], banned)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "__import__", "exec", "eval"})

    def test_acc4_never_returns_pushed_approved_or_key(self):
        strings = {n.value.lower() for n in ast.walk(ast.parse(self.SOURCE))
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertFalse({"pushed", "approved", "key"} & strings)
        self.assertEqual(n6_pushpaths.PathDecision._fields, ("verdict", "reasons"))

    def test_acc1_module_holds_no_path_list(self):
        for word in ("uv.lock", ".env", "workflows", "jarvis.db", ".pem", "conveyor"):
            self.assertNotIn(word, self.SOURCE)

    def test_acc5_deterministic_ascii_reasons(self):
        paths = ["uv.lock", ".claude/x", "CON", "a\u202eb", "\u00e9t\u00e9/x.db", "ok.py"]
        diff = b"".join(drec(p.encode()) for p in paths)
        tree = b"".join(trec(p.encode()) for p in reversed(paths))
        first = decide(diff, tree, NEVER, KEY)
        self.assertEqual(first.verdict, REFUSE)
        self.assertEqual(len(first.reasons), 4)
        self.assertEqual(first, decide(diff, tree, list(reversed(NEVER)), set(KEY)))
        self.assertTrue(all(r.isascii() for r in first.reasons))
        self.assertEqual(list(first.reasons), sorted(first.reasons))


if __name__ == "__main__":
    unittest.main()
