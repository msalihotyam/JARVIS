import hashlib
import io
import os
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.stage_git import ALLOWED, StageGitError, _check, _extract_tar, export_commit, run, tree_of

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


if __name__ == "__main__":
    unittest.main()
