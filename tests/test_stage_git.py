import hashlib
import io
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from jarvis.stage_git import (
    ALLOWED, StageGitError, _check, _extract_tar, _pipe, export_commit, import_commit, run, tree_of,
)

FILES = {
    ".gitattributes": b"*.txt export-subst ident\n*.dat text eol=crlf\nskip.txt export-ignore\n",
    "a.txt": b"$Format:%H$ $Id$\n",
    "lf.dat": b"a\nb\n",
    "skip.txt": b"kept\n",
    "sub/b.bin": bytes(range(256)),
}


def with_pack_names(midx, names):
    """``midx`` with its PNAM chunk replaced by ``names``; chunk offsets and checksum rebuilt."""
    count = midx[6]
    table = [struct.unpack(">4sQ", midx[12 + 12 * i:24 + 12 * i]) for i in range(count + 1)]
    chunks = {cid: midx[table[i][1]:table[i + 1][1]] for i, (cid, _) in enumerate(table[:-1])}
    pnam = b"".join(name.encode() + b"\0" for name in names)
    chunks[b"PNAM"] = pnam + b"\0" * (-len(pnam) % 4)
    head, body, start = midx[:12], b"", 12 + 12 * (count + 1)
    for cid, _ in table[:-1]:
        head += struct.pack(">4sQ", cid, start + len(body))
        body += chunks[cid]
    data = head + struct.pack(">4sQ", b"\0" * 4, start + len(body)) + body
    return data + hashlib.sha1(data, usedforsecurity=False).digest()


class StageGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ["TEMP"])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.repo = self.home / "repo"
        self.marker = self.home / "marker"
        self.repo.mkdir()
        self.git("init", "-q")
        for name, data in FILES.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_bytes(data)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "one")
        (self.repo / "skip.txt").write_bytes(b"changed\n")
        self.git("commit", "-q", "-am", "two")
        self.plant()

    def git(self, *args, input=None, repo=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run([shutil.which("git"), "-C", str(repo or self.repo), *args], input=input, env=env,
                              capture_output=True, check=True).stdout

    def plant(self):
        """Everything a worker could write into .git that would run a command; each writes the marker."""
        write = f"echo x >> '{self.marker.as_posix()}'"
        hooks = self.home / "hooks"
        script = f"#!/bin/sh\n{write}\n".encode()
        for directory in (hooks, self.repo / ".git" / "hooks"):
            directory.mkdir(exist_ok=True)
            for hook in ("reference-transaction", "post-checkout", "fsmonitor-watchman"):
                (directory / hook).write_bytes(script)
        for key, value in (
            ("filter.x.clean", write), ("filter.x.smudge", write), ("filter.x.process", write),
            ("core.hooksPath", hooks.as_posix()), ("core.fsmonitor", (hooks / "fsmonitor-watchman").as_posix()),
            ("hook.evil.command", write), ("hook.evil.event", "reference-transaction"),
            ("hook.odd=name.command", write), ("hook.odd=name.event", "reference-transaction"),
            ("diff.external", write), ("diff.x.textconv", write), ("diff.x.command", write),
            ("gpg.program", write), ("tar.tar.command", write), ("core.sshCommand", write),
        ):
            self.git("config", key, value)
        (self.repo / ".git" / "info" / "attributes").write_bytes(b"* filter=x diff=x\n")

    def test_planted_configuration_never_runs(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        run(self.repo, "rev-parse", "HEAD")
        run(self.repo, "cat-file", "-p", "HEAD:a.txt")
        run(self.repo, "cat-file", "--batch", input=b"HEAD:lf.dat\n")
        run(self.repo, "ls-tree", "-r", "HEAD")
        run(self.repo, "diff-tree", "-p", "-r", "HEAD~1", "HEAD")
        run(self.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        run(self.repo, "update-ref", "refs/heads/moved", head)
        run(self.repo, "update-ref", "--stdin", input=f"create refs/heads/tx {head}\n".encode())
        export_commit(self.repo, "HEAD", self.home / "out")
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.git("rev-parse", "refs/heads/moved", "refs/heads/tx").decode().split(), [head] * 2)
        # Control: the same calls without the helper do run the planted commands.
        self.git("update-ref", "refs/heads/control", head)
        fired = self.marker.read_bytes().count(b"x")
        self.assertGreater(fired, 0)
        self.git("archive", "--format=tar", "HEAD")
        self.assertGreater(self.marker.read_bytes().count(b"x"), fired)

    def test_config_defined_hook_alone_never_runs(self):
        # core.hooksPath does not cover hook.<name>.command; the event pin must.
        repo = self.home / "hookonly"
        repo.mkdir()
        self.git("init", "-q", repo=repo)
        self.git("commit", "-q", "--allow-empty", "-m", "a", repo=repo)
        self.git("config", "hook.evil.command", f"echo x >> '{self.marker.as_posix()}'", repo=repo)
        self.git("config", "hook.evil.event", "reference-transaction", repo=repo)
        head = self.git("rev-parse", "HEAD", repo=repo).decode().strip()
        run(repo, "update-ref", "refs/heads/helper", head)
        self.assertFalse(self.marker.exists())
        self.git("update-ref", "refs/heads/control", head, repo=repo)
        self.assertTrue(self.marker.exists())

    def test_redirecting_repositories_are_refused_before_spawn(self):
        git_dir = self.repo / ".git"
        config = (git_dir / "config").read_bytes()
        gitfile = self.home / "gitfile"
        gitfile.mkdir()
        (gitfile / ".git").write_bytes(f"gitdir: {git_dir.as_posix()}\n".encode())

        def planted(path, data):
            """Plant ``data`` at ``path``; return the undo."""
            def plant():
                path.write_bytes(data)
                return (lambda: path.write_bytes(config)) if path.name == "config" else path.unlink
            return plant

        def junction():
            import _winapi
            link = git_dir / "logs" / "linked"
            _winapi.CreateJunction(str(self.home), str(link))
            return lambda: os.rmdir(link)

        def midx_chain():
            chain = git_dir / "objects/pack/multi-pack-index.d"
            chain.mkdir()
            return chain.rmdir

        cases = {
            "multi-pack-index": (self.repo, planted(git_dir / "objects/pack/multi-pack-index", b"")),
            "midx upper": (self.repo, planted(git_dir / "objects/pack/MULTI-PACK-INDEX", b"MIDX")),
            "midx bitmap": (self.repo, planted(git_dir / "objects/pack/multi-pack-index-0a.bitmap", b"")),
            "midx chain": (self.repo, midx_chain),
            "gitfile": (gitfile, lambda: None),
            "walk up": (self.repo / "sub", lambda: None),
            "commondir": (self.repo, planted(git_dir / "commondir", b"../elsewhere\n")),
            "alternates": (self.repo, planted(git_dir / "objects/info/alternates", b"//host/share/objects\n")),
            "include": (self.repo, planted(git_dir / "config", config + b"[include]\n\tpath = //host/share/c\n")),
            "include if": (self.repo, planted(git_dir / "config", config + b'[includeIf "onbranch:*"]\n\tpath = x\n')),
            "ref storage files": (self.repo, planted(git_dir / "config", config.replace(
                b"repositoryformatversion = 0", b"repositoryformatversion = 1")
                + f"[extensions]\n\trefStorage = files://{self.home.as_posix()}/other/.git\n".encode())),
            "ref storage reftable": (self.repo, planted(
                git_dir / "config", config + b"[EXTENSIONS]\n\tREFSTORAGE = reftable\n")),
            "junction": (self.repo, junction),
        }
        with patch("jarvis.stage_git.subprocess.run") as spawn:
            for label, (repo, plant) in cases.items():
                with self.subTest(label):
                    undo = plant()
                    try:
                        with self.assertRaises(StageGitError):
                            run(repo, "rev-parse", "HEAD")
                    finally:
                        if undo:
                            undo()
        spawn.assert_not_called()
        run(self.repo, "rev-parse", "HEAD")

    def test_disallowed_commands_are_refused_before_spawn(self):
        refused = [
            (), ("status",), ("config", "--list"), ("-c", "x=y", "rev-parse"), ("hook", "run", "x"),
            ("diff-tree", "--ext-diff", "HEAD"), ("diff-tree", "--ext", "HEAD"), ("diff-tree", "--text", "HEAD"),
            ("cat-file", "--filters", "HEAD:a.txt"), ("cat-file", "--textconv", "HEAD:a.txt"),
            ("for-each-ref", "--sh", "--format=%(signature)"), ("rev-parse", "--output=x"),
            ("archive", "--format=tar", "HEAD"), ("archive", "--remote=x", "--format=tar", "0" * 40),
            ("archive", "--exec=x", "0" * 40), ("archive", "--format=tar", "0" * 40, "--output=x"),
            ("rev-parse", b"HEAD"),
            ("diff-tree", "-Ofile", "HEAD"), ("diff-tree", "-O", "file", "HEAD"), ("diff-tree", "-pOfile", "HEAD"),
            ("diff-tree", "--orderfile=x", "HEAD"), ("diff-tree", "--order=x", "HEAD"),
            ("rev-parse", "--resolve-git-dir", "x"), ("rev-parse", "--resolve-git", "x"),
            ("rev-parse", "--git-dir"), ("rev-parse", "--git-path", "x"), ("rev-parse", "--work-tree=x"),
            ("diff-tree", "-Sx", "-Ofile", "HEAD"), ("diff-tree", "-pO", "file", "HEAD"),
            # an option's value, not the end of options
            ("diff-tree", "-p", "-I", "--end-of-options", "-Ofile", "HEAD"),
            ("diff-tree", "-p", "--src-prefix", "--end-of-options", "-Ofile", "HEAD"),
        ]
        with patch("jarvis.stage_git.subprocess.run") as spawn:
            for args in refused:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    run(self.repo, *args)
            for commit in ("", "-x", "--output=x", None):
                with self.subTest(commit=commit), self.assertRaises(ValueError):
                    tree_of(self.repo, commit)
        spawn.assert_not_called()

    def test_help_is_refused_before_spawn(self):
        # CDXINT129: `git <sub> --help` runs `git help`, which starts the repository's web browser.
        write = f"echo x >> '{self.marker.as_posix()}'"
        for key, value in (("help.format", "web"), ("web.browser", "x"), ("browser.x.cmd", write),
                           ("man.viewer", "x"), ("man.x.cmd", write)):
            self.git("config", key, value)
        variants = ("--help", "--hel", "--he", "--h", "--HELP", "--help-all", "--help=x", "-h")
        with patch("jarvis.stage_git.subprocess.run") as spawn:
            for sub in sorted(ALLOWED):
                for args in ((sub, "--help"), (sub, "help"), *((sub, "HEAD", v) for v in variants)):
                    with self.subTest(args=args), self.assertRaises(ValueError):
                        run(self.repo, *args)
        spawn.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_option_like_values_and_operands_are_allowed(self):
        for args in (("diff-tree", "-SFOO", "HEAD~1", "HEAD"), ("diff-tree", "-GFOO", "HEAD~1", "HEAD"),
                     ("diff-tree", "-p", "-SOx", "HEAD~1", "HEAD"),
                     ("diff-tree", "HEAD~1", "HEAD", "--end-of-options", "-Object.txt"),
                     ("rev-parse", "--end-of-options", "-Object.txt"), ("ls-tree", "-rO", "HEAD")):
            with self.subTest(args=args):
                _check(args)

    def test_multi_pack_index_never_reads_outside_the_repository(self):
        # CDXINT128: a planted multi-pack-index whose pack name climbs out to another repository's pack.
        outside = self.home / "outside"
        outside.mkdir()
        self.git("init", "-q", repo=outside)
        (outside / "secret.txt").write_bytes(b"outside secret\n")
        self.git("add", "-A", repo=outside)
        self.git("commit", "-q", "-m", "s", repo=outside)
        commit, tree, blob = self.git("rev-parse", "HEAD", "HEAD^{tree}", "HEAD:secret.txt",
                                      repo=outside).decode().split()
        self.git("repack", "-adq", repo=outside)
        self.git("multi-pack-index", "write", repo=outside)
        pack = outside / ".git" / "objects" / "pack"
        idx = next(name for name in os.listdir(pack) if name.endswith(".idx"))
        (self.repo / ".git" / "objects" / "pack" / "multi-pack-index").write_bytes(with_pack_names(
            (pack / "multi-pack-index").read_bytes(), [f"../../../../outside/.git/objects/pack/{idx}"]))
        # Control: plain Git reads the outside blob through it.
        self.assertEqual(self.git("cat-file", "-p", blob), b"outside secret\n")
        with patch("jarvis.stage_git.subprocess.run") as spawn:
            for args in (("cat-file", "-p", blob), ("archive", "--format=tar", tree)):
                with self.subTest(args[0]), self.assertRaises(StageGitError):
                    run(self.repo, *args)
            with self.assertRaises(StageGitError):
                export_commit(self.repo, commit, self.home / "out")
        spawn.assert_not_called()
        self.assertEqual(os.listdir(self.home / "out"), [])
        # The pin alone (refusal bypassed) also keeps Git from reading it, the archive view included.
        with patch("jarvis.stage_git.os.listdir", return_value=[]):
            for args in (("cat-file", "-p", blob), ("archive", "--format=tar", tree)):
                with self.subTest(f"pinned {args[0]}"), self.assertRaises(StageGitError) as caught:
                    run(self.repo, *args)
                self.assertNotIn("outside secret", str(caught.exception))
            with self.assertRaises(StageGitError):
                export_commit(self.repo, commit, self.home / "out")
        self.assertEqual(os.listdir(self.home / "out"), [])

    def test_tree_of_matches_git(self):
        self.assertEqual(tree_of(self.repo, "HEAD~1"), self.git("rev-parse", "HEAD~1^{tree}").decode().strip())
        with self.assertRaises(StageGitError):
            tree_of(self.repo, "missing")

    def test_export_writes_committed_bytes_without_git(self):
        dest = export_commit(self.repo, "HEAD", self.home / "out")
        names = self.git("ls-tree", "-r", "--name-only", "-z", "HEAD").decode().strip("\0").split("\0")
        exported = sorted(path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file())
        self.assertEqual(exported, sorted(names))
        self.assertFalse((dest / ".git").exists())
        for name in names:
            with self.subTest(name=name):
                self.assertEqual((dest / name).read_bytes(), self.git("cat-file", "blob", f"HEAD:{name}"))
        self.assertEqual((dest / "a.txt").read_bytes(), FILES["a.txt"])
        self.assertEqual((dest / "lf.dat").read_bytes(), b"a\nb\n")

    def test_export_refuses_a_used_destination(self):
        (self.home / "full").mkdir()
        (self.home / "full" / "x").write_bytes(b"")
        (self.home / "file").write_bytes(b"")
        for dest in ("full", "file"):
            with self.subTest(dest=dest), self.assertRaises(StageGitError):
                export_commit(self.repo, "HEAD", self.home / dest)
        self.assertEqual(os.listdir(self.home / "full"), ["x"])

    def test_extractor_refuses_unsafe_members(self):
        def member(name, kind=tarfile.REGTYPE, link=""):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, link
            return info

        cases = {
            "parent": [member("../evil")], "nested parent": [member("a/../../evil")],
            "absolute": [member("/evil")], "drive": [member("C:/evil")], "stream": [member("a:evil")],
            "backslash": [member("..\\evil")], "symlink": [member("link", tarfile.SYMTYPE, "../")],
            "hardlink": [member("a"), member("hard", tarfile.LNKTYPE, "a")],
            "device": [member("dev", tarfile.CHRTYPE)], "fifo": [member("pipe", tarfile.FIFOTYPE)],
            "dot git": [member(".git/config")], "upper git": [member(".GIT/hooks/x")],
            "short git": [member("GIT~1/config")], "reserved": [member("sub/con.txt")],
            "trailing dot": [member("evil.")], "duplicate": [member("a"), member("A")],
            "case dir": [member("A/one"), member("a/two")],
            "case dir entry": [member("A/", tarfile.DIRTYPE), member("a/two")],
            "case deep": [member("d/A/one"), member("d/a/two")],
            "case file vs dir": [member("a"), member("A/x")],
            "unicode fold": [member("STRASSE/x"), member("straße/y")],
            "unicode nfc": [member("café/x"), member("café/y")],
            "file then dir": [member("a"), member("a/", tarfile.DIRTYPE)],
            "dir then file": [member("a/", tarfile.DIRTYPE), member("a")],
        }
        for label, members in cases.items():
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for info in members:
                    archive.addfile(info, io.BytesIO(b"") if info.isreg() else None)
            dest = self.home / "x" / label.replace(" ", "-")
            dest.mkdir(parents=True)
            with self.subTest(label), self.assertRaises(StageGitError):
                _extract_tar(buffer.getvalue(), dest)
        self.assertEqual(sorted(os.listdir(self.home)), ["hooks", "repo", "x"])
        self.assertFalse(self.marker.exists())

    def test_extractor_refuses_member_without_readable_content(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            archive.addfile(tarfile.TarInfo("a"), io.BytesIO(b""))
        dest = self.home / "none"
        dest.mkdir()
        with patch.object(tarfile.TarFile, "extractfile", return_value=None), self.assertRaises(StageGitError):
            _extract_tar(buffer.getvalue(), dest)
        self.assertEqual(os.listdir(dest), [])


class ImportCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ["TEMP"])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.stage = self.home / "stage"
        self.own = self.home / "own"
        self.stage.mkdir()
        self.own.mkdir()
        self.git(self.stage, "init", "-q")
        self.write("a.txt", b"a\n")
        self.write("protected.txt", b"p\n")
        self.git(self.stage, "add", "-A")
        self.git(self.stage, "commit", "-q", "-m", "base")
        self.base = self.rev("HEAD")
        self.base_tree = self.rev("HEAD^{tree}")
        self.git(self.own, "init", "-q")
        self.git(self.own, "config", "gc.auto", "0")
        pack = self.git(self.stage, "pack-objects", "--revs", "--stdout", "-q", input=f"{self.base}\n".encode())
        self.git(self.own, "index-pack", "--stdin", input=pack)
        self.git(self.own, "update-ref", "refs/jarvis/base", self.base)
        self.write("a.txt", b"changed\n")
        self.write("protected.txt", b"EVIL\n")
        self.git(self.stage, "commit", "-qam", "work")
        self.tip = self.rev("HEAD")
        self.tip_tree = self.rev("HEAD^{tree}")

    def git(self, repo, *args, input=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run([shutil.which("git"), "-C", str(repo), *args], input=input, env=env,
                              capture_output=True, check=True).stdout

    def rev(self, name):
        return self.git(self.stage, "rev-parse", name).decode().strip()

    def write(self, name, data):
        (self.stage / name).write_bytes(data)

    def blob(self, data):
        return self.git(self.stage, "hash-object", "-w", "--stdin", input=data).decode().strip()

    def loose(self, oid, repo=None):
        return (repo or self.stage) / ".git" / "objects" / oid[:2] / oid[2:]

    def present(self, oid):
        return subprocess.run(
            [shutil.which("git"), "-C", str(self.own), "cat-file", "-e", oid], capture_output=True,
        ).returncode == 0

    def forge(self, oid, kind, body):
        """The stage's loose file for ``oid`` now holds other bytes: the worker can write them."""
        path = self.loose(oid)
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            path.chmod(0o644)
        path.write_bytes(zlib.compress(f"{kind} {len(body)}\0".encode() + body))

    def commit_body(self, tree, message, *parents):
        lines = [f"tree {tree}", *(f"parent {parent}" for parent in parents),
                 "author a <a@a> 1 +0000", "committer a <a@a> 1 +0000", "", message, ""]
        return "\n".join(lines).encode()

    def refused(self, pattern, commit=None, **kwargs):
        commit = commit or self.tip
        with self.assertRaisesRegex(StageGitError, pattern):
            import_commit(self.stage, self.own, commit, self.base, **kwargs)
        self.assertFalse(self.present(commit))

    def test_clean_commit_is_accepted_and_no_git_touches_the_stage(self):
        popen = subprocess.Popen
        calls = []

        def spy(argv, *args, **kwargs):
            calls.append((argv, kwargs.get("cwd")))
            return popen(argv, *args, **kwargs)

        with patch("subprocess.Popen", side_effect=spy):
            import_commit(self.stage, self.own, self.tip, self.base)
        self.assertEqual(self.git(self.own, "diff-tree", "-r", "--name-status", self.base, self.tip),
                         b"M\ta.txt\nM\tprotected.txt\n")
        self.assertEqual(self.git(self.own, "for-each-ref", "--format=%(refname)"), b"refs/jarvis/base\n")
        self.assertEqual(len([argv for argv, _ in calls if "pack-objects" in argv]), 1)
        stage = str(self.stage).casefold()
        for argv, cwd in calls:
            self.assertNotIn(stage, " ".join(argv).casefold())
            self.assertNotIn(stage, str(cwd).casefold())

    def test_forged_blob_is_refused(self):
        self.forge(self.blob(b"changed\n"), "blob", b"something else\n")
        self.refused("did not receive expected object")

    def test_forged_tip_tree_is_refused(self):
        self.loose(self.tip_tree).chmod(0o644)
        self.loose(self.tip_tree).write_bytes(self.loose(self.base_tree).read_bytes())
        self.refused("did not receive expected object")

    def test_forged_commit_passes_index_pack_and_is_still_refused(self):
        self.forge(self.tip, "commit", self.commit_body(self.tip_tree, "forged", self.base))
        self.refused("^git cat-file exited")

    def test_claimed_tag_is_refused(self):
        self.git(self.stage, "tag", "-a", "-m", "x", "t1", self.tip)
        self.git(self.stage, "tag", "-a", "-m", "y", "t2", "t1")
        for tag in ("t1", "t2"):
            with self.subTest(tag), self.assertRaisesRegex(StageGitError, "not a commit"):
                import_commit(self.stage, self.own, self.rev(tag), self.base)

    def test_oversized_config_is_refused_before_reading_it(self):
        os.truncate(self.stage / ".git" / "config", 1024 * 1024 + 1)
        self.refused("too large")
        with self.assertRaisesRegex(StageGitError, "too large"):
            run(self.stage, "rev-parse", "HEAD")

    def test_commit_already_in_own_repository_with_missing_objects_is_refused(self):
        planted = self.commit_body("1" * 40, "planted", self.base)
        oid = self.git(self.own, "hash-object", "-w", "--literally", "-t", "commit", "--stdin",
                       input=planted).decode().strip()
        self.forge(oid, "commit", self.commit_body(self.base_tree, "other", self.base))
        with self.assertRaisesRegex(StageGitError, "^git rev-list exited"):
            import_commit(self.stage, self.own, oid, self.base)

    def test_commit_not_on_base_is_refused(self):
        orphan = self.git(self.stage, "commit-tree", self.tip_tree, "-m", "orphan").decode().strip()
        with self.assertRaisesRegex(StageGitError, "^git merge-base exited 1"):
            import_commit(self.stage, self.own, orphan, self.base)

    def test_forged_base_tree_cannot_hide_a_change(self):
        evil = self.blob(b"EVIL\n")
        forged_tree = self.git(
            self.stage, "mktree",
            input=(f"100644 blob {self.blob(b'a' + bytes([10]))}\ta.txt\n"
                   f"100644 blob {evil}\tprotected.txt\n").encode(),
        ).decode().strip()
        self.loose(self.base_tree).chmod(0o644)
        self.loose(self.base_tree).write_bytes(self.loose(forged_tree).read_bytes())
        # Control: Git in the stage itself now reports only a.txt, so the forgery works.
        self.assertEqual(self.git(self.stage, "-c", "core.commitGraph=false", "diff-tree", "-r", "--name-status",
                                  self.base, self.tip), b"M\ta.txt\n")
        self.refused("did not receive expected object")

    def test_planted_commands_do_not_fire_and_their_controls_do(self):
        markers = {name: self.home / name for name in ("hook", "fsmonitor", "filter", "planted")}
        command = {name: f"echo x >> '{path.as_posix()}'" for name, path in markers.items()}
        git_dir = self.stage / ".git"
        hook = git_dir / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_bytes(f"#!/bin/sh\n{command['hook']}\n".encode())
        hook.chmod(0o755)
        with open(git_dir / "config", "a", encoding="ascii") as config:
            config.write(f"[core]\n\tfsmonitor = \"{command['fsmonitor']}\"\n"
                         f"\tpackedGitLimit = 1\n\tsshCommand = \"{command['planted']}\"\n"
                         f"\tgitProxy = \"{command['planted']}\"\n"
                         f"[filter \"evil\"]\n\tclean = \"{command['filter']}; cat\"\n"
                         f"\tsmudge = \"{command['filter']}; cat\"\n\trequired = true\n"
                         f"[pack]\n\twindow = 0\n")
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "attributes").write_bytes(b"* filter=evil\n")
        import_commit(self.stage, self.own, self.tip, self.base)
        self.assertTrue(self.present(self.tip))
        self.assertEqual([name for name, path in markers.items() if path.exists()], [])
        # Controls: ordinary Git in the stage does run each of them.
        self.git(self.stage, "status")
        self.assertTrue(markers["fsmonitor"].exists())
        self.write("new.txt", b"n\n")
        self.git(self.stage, "add", "new.txt")
        self.assertTrue(markers["filter"].exists())
        self.git(self.stage, "commit", "-qm", "control")
        self.assertTrue(markers["hook"].exists())

    def test_size_cap_refuses_the_pack(self):
        self.refused("exceeds maximum allowed size", max_bytes=64)

    def test_alternates_in_the_stage_are_refused(self):
        (self.stage / ".git" / "objects" / "info" / "alternates").write_bytes(b"C:/elsewhere\n")
        self.refused("redirects")

    def test_dot_git_entry_is_refused_by_strict(self):
        entry = self.git(self.stage, "mktree", "--missing", input=(
            f"040000 tree {self.base_tree}\t.git\n").encode()).decode().strip()
        commit = self.git(self.stage, "commit-tree", entry, "-p", self.base, "-m", "dotgit").decode().strip()
        self.refused("fsck error", commit)

    def test_object_format_comes_from_own_repository(self):
        sha256 = self.home / "sha256"
        sha256.mkdir()
        self.git(sha256, "init", "-q", "--object-format=sha256")
        (sha256 / "a.txt").write_bytes(b"a\n")
        self.git(sha256, "add", "-A")
        self.git(sha256, "commit", "-q", "-m", "work")
        long_tip = self.git(sha256, "rev-parse", "HEAD").decode().strip()
        self.assertEqual(len(long_tip), 64)
        with self.assertRaisesRegex(StageGitError, "object format mismatch"):
            import_commit(sha256, self.own, long_tip, self.base)
        with self.assertRaisesRegex(StageGitError, "object format mismatch"):
            import_commit(self.stage, self.own, long_tip, long_tip)
        # A short claim is read through a sha1 view whatever the stage says it is.
        with self.assertRaisesRegex(StageGitError, "^git pack-objects exited"):
            import_commit(sha256, self.own, self.tip, self.base)

    def test_stage_config_cannot_choose_the_object_format(self):
        with open(self.stage / ".git" / "config", "a", encoding="ascii") as config:
            config.write("[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n")
        import_commit(self.stage, self.own, self.tip, self.base)
        self.assertTrue(self.present(self.tip))

    def test_only_full_object_ids_and_a_positive_cap_reach_git(self):
        with patch("subprocess.Popen", side_effect=AssertionError("spawned")):
            for bad in ("main", "HEAD", "--all", self.tip[:12], self.tip.upper(), self.tip + "\n", None):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    import_commit(self.stage, self.own, bad, self.base)
                with self.subTest(base=bad), self.assertRaises(ValueError):
                    import_commit(self.stage, self.own, self.tip, bad)
            for cap in (0, -1, True, 1.5, "64"):
                with self.subTest(cap=cap), self.assertRaises(ValueError):
                    import_commit(self.stage, self.own, self.tip, self.base, max_bytes=cap)

    def test_other_pack_and_index_forms_are_refused(self):
        for args in (("pack-objects", "--revs", "--stdout"), ("pack-objects", "--all", "--stdout", "-q"),
                     ("pack-objects", "--revs", "--stdout", "-q", "base"),
                     ("index-pack", "--strict", "--stdin"),
                     ("index-pack", "--strict", "--max-input-size=0", "--stdin"),
                     ("index-pack", "--strict", "--max-input-size=64", "--fix-thin"),
                     ("index-pack", "--strict", "--max-input-size=64", "--stdin", "--keep"),
                     ("index-pack", "--max-input-size=64", "--max-input-size=64", "--stdin")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                _check(args)

    def test_timeout_kills_both_sides(self):
        hang = [sys.executable, "-I", "-c", "import time; time.sleep(600)"]
        drain = [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"]
        for producer, consumer in ((hang, drain), (drain, hang), (hang, hang)):
            popen = subprocess.Popen
            started = []

            def spy(argv, *args, **kwargs):
                started.append(popen(argv, *args, **kwargs))
                return started[-1]

            scratch = self.home / f"scratch{len(producer)}"
            shutil.rmtree(scratch, ignore_errors=True)
            scratch.mkdir()
            with patch("subprocess.Popen", side_effect=spy), \
                    self.assertRaisesRegex(StageGitError, "TimeoutExpired"):
                _pipe(producer, consumer, b"", dict(os.environ), scratch, 0.5)
            self.assertEqual(len(started), 2)
            self.assertTrue(all(process.poll() is not None for process in started))
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
