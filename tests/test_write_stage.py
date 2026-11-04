"""Offline staging tests: every repository lives beneath TEMP."""

import itertools
import os
import subprocess
import stat
import sys
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.write_stage import GIT_PIN, StageError, StageManager


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ["TEMP"])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.manager = StageManager(self.home / "stages")
        self.repo = self.manager.create_repo(self.home / "project")
        self.clock = itertools.count(1_000_000_000, 1000)

    def dirty(self, file):
        # A new mtime makes the index entry stat-dirty, so status must re-read
        # the file and would run its clean filter.
        moment = next(self.clock)
        os.utime(file, (moment, moment))

    def plain(self, repo, *args):
        """Ordinary Git without the manager's pins: the firing control."""
        return subprocess.run(
            [self.manager.git.argv0(workspace=(repo,)), "-C", str(repo), *args],
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
            capture_output=True, check=True).stdout

    def test_git_refuses_when_trusted_git_is_gone(self):
        fresh = self.home / "fresh"
        fresh.mkdir()
        self.manager.git = None
        # "init" reaches only the command spawn; any other command reaches the config scan first.
        for cwd, args in ((fresh, ("init",)), (self.repo, ("status",))):
            with self.subTest(args), self.assertRaisesRegex(StageError, "trusted git is unavailable"):
                self.manager._git(cwd, *args)
        self.assertEqual(os.listdir(fresh), [])

    def plant(self, repo, name="planted", kinds=("filter", "diff")):
        """Commit a driver probe, then configure the driver; return its marker."""
        marker = self.home / f"{repo.name}-ran"
        attributes = " ".join(f"{kind}={name}" for kind in kinds)
        (repo / ".gitattributes").write_text(f"*.txt {attributes}\n", encoding="utf-8")
        # A native executable with no shell syntax: as a filter it executes
        # base.txt from stdin, as a textconv it executes the file it is given.
        (repo / "base.txt").write_text(
            "from pathlib import Path\n" + f"Path({str(marker)!r}).write_text('ran')\n"
            + "print('filtered')\n", encoding="utf-8")
        # Committed before the driver exists, so unfiltered content equals the
        # blob and a pinned status stays clean.
        self.commit(repo)
        command = Path(sys.executable).as_posix()
        if "filter" in kinds:
            self.plain(repo, "config", f"filter.{name}.clean", command)
            self.plain(repo, "config", f"filter.{name}.smudge", command)
        if "diff" in kinds:
            self.plain(repo, "config", f"diff.{name}.textconv", command)
        return marker

    def control_fires(self, repo, marker, *args):
        self.dirty(repo / "base.txt")
        self.plain(repo, *(args or ("status", "--porcelain")))
        self.assertTrue(marker.exists(), "control: the probe must fire without the guard")
        marker.unlink()

    def tracked_repo(self, name):
        repo = self.manager.create_repo(self.home / name)
        (repo / "base.txt").write_text("base\n")
        self.commit(repo)
        return repo

    def poison(self, repo, data):
        with open(repo / ".git" / "config", "ab") as config:
            config.write(data)

    def argvs(self, call):
        with patch("jarvis.write_stage.subprocess.run", wraps=subprocess.run) as run:
            call()
        return [item.args[0] for item in run.call_args_list]

    def stage(self):
        return self.manager.create(self.repo, "run1", "slice1")

    def commit(self, path):
        self.manager._git(path, "add", "--all")
        tree = self.manager._git(path, "write-tree").decode().strip()
        parent = self.manager._git(path, "rev-parse", "HEAD").decode().strip()
        sha = self.manager._git(path, "commit-tree", tree, "-p", parent,
                                input=b"test change\n").decode().strip()
        self.manager._git(path, "update-ref", "HEAD", sha, parent)
        return sha

    def test_independent_stage_and_run_repository(self):
        (self.repo / "base.txt").write_text("baseline")
        self.commit(self.repo)
        stage = self.stage()
        owned = self.manager.root.parent / (self.manager.root.name + "-repos") / "run1"
        for repo in (stage.path, owned):
            self.assertTrue((repo / ".git" / "objects").is_dir())
            self.assertFalse((repo / ".git" / "objects" / "info" / "alternates").exists())
            for entry in (repo / ".git").rglob("*"):
                if entry.is_file():
                    content = entry.read_bytes()
                    for target in (str(self.repo), self.repo.as_posix()):
                        self.assertFalse(target.encode() in content, str(entry)[:80])
        self.assertEqual((stage.path / "base.txt").read_text(), "baseline")
        self.assertEqual(list(owned.iterdir()), [owned / ".git"])
        self.assertEqual(self.manager._git(owned, "config", "gc.auto"), b"0\n")
        self.assertEqual(self.manager._git(owned, "rev-parse", "jarvis/run1/main").decode().strip(), stage.base)
        self.assertEqual(self.rev("jarvis/run1/main"), stage.base)
        before = {p.relative_to(self.repo): p.read_bytes()
                  for p in (self.repo / ".git").rglob("*") if p.is_file()}
        for entry in (stage.path / ".git" / "objects").rglob("*"):
            if entry.is_file():
                entry.chmod(stat.S_IWRITE | stat.S_IREAD)
                entry.write_bytes(b"worker overwrite")
        (stage.path / ".git" / "objects" / "worker").write_bytes(b"worker new object")
        after = {p.relative_to(self.repo): p.read_bytes()
                 for p in (self.repo / ".git").rglob("*") if p.is_file()}
        self.assertTrue(before == after, "target metadata changed after stage object writes")

    def test_new_run_repository_seeds_existing_integration(self):
        stage = self.stage()
        commit = self.change(stage, "accepted.txt")
        self.manager.apply(stage, commit)
        manager = StageManager(self.home / "other-stages")
        next_stage = manager.create(self.repo, "run1", "slice2")
        owned = self.home / "other-stages-repos" / "run1"
        self.assertEqual(manager._git(owned, "rev-parse", "jarvis/run1/main").decode().strip(), commit)
        self.assertEqual(next_stage.base, stage.base)

    def test_validate_is_python_only_and_refuses_metadata_tampering(self):
        stage = self.stage()
        with patch.object(self.manager, "_git", side_effect=AssertionError("git during validate")):
            self.manager._validate(stage)
            alternates = stage.path / ".git" / "objects" / "info" / "alternates"
            alternates.write_text("")
            with self.assertRaisesRegex(StageError, "alternates"):
                self.manager._validate(stage)
            alternates.unlink()
            head = stage.path / ".git" / "HEAD"
            original = head.read_bytes()
            for value in (b"ref: refs/heads/wrong\n", stage.base.encode() + b"\n",
                          original.rstrip(b"\n")):
                head.write_bytes(value)
                with self.assertRaises(StageError):
                    self.manager._validate(stage)
            head.write_bytes(original)
            metadata = stage.path / ".git"
            saved = stage.path / "saved-git"
            metadata.rename(saved)
            metadata.write_text("gitdir: " + str(self.repo / ".git"))
            with self.assertRaises(StageError):
                self.manager._validate(stage)
            metadata.unlink()
            saved.rename(metadata)
            real_lstat = os.lstat

            def reparse(path, *args, **kwargs):
                result = real_lstat(path, *args, **kwargs)
                if Path(path) == metadata:
                    from types import SimpleNamespace
                    return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
                return result

            with patch("jarvis.write_stage.os.lstat", side_effect=reparse):
                with self.assertRaises(StageError):
                    self.manager._validate(stage)

    def test_validate_refuses_stage_symlink(self):
        stage = self.stage()
        real_lstat = os.lstat
        for linked in (stage.path, stage.path / ".git"):
            def symlink_info(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if Path(path) == linked:
                    from types import SimpleNamespace
                    return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777,
                                           st_file_attributes=0)
                return info

            with self.subTest(linked=linked), patch.object(
                    self.manager, "_git", side_effect=AssertionError("git during validate")):
                with patch("jarvis.write_stage.os.lstat", side_effect=symlink_info):
                    with self.assertRaises(StageError):
                        self.manager._validate(stage)

    def test_tip_claim_refuses_linked_refs_before_reading(self):
        from types import SimpleNamespace
        stage = self.stage()
        refs = stage.path / ".git" / "refs"
        real_lstat = os.lstat
        for mode, attrs in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFDIR | 0o777, 0x400)):
            def linked(path, *args, **kwargs):
                if Path(path) == refs:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attrs)
                return real_lstat(path, *args, **kwargs)

            with patch("jarvis.write_stage.os.lstat", side_effect=linked), patch.object(
                    Path, "open", side_effect=AssertionError("outside ref read")):
                with self.assertRaisesRegex(StageError, "refs traverses"):
                    self.manager._claim(stage)

    def test_new_repo_has_empty_baseline(self):
        self.assertEqual(self.manager._git(self.repo, "ls-tree", "HEAD"), b"")
        with self.assertRaises(StageError):
            self.manager.create_repo(self.repo)

    def test_stage_branch_and_clean_cleanup(self):
        stage = self.stage()
        self.assertEqual(stage.branch, "jarvis/run1/slice1")
        self.manager.assert_apply_ready(stage)
        self.assertEqual(self.manager.diff(stage), b"")
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())

    def test_diff_contains_only_verified_commits(self):
        stage = self.stage()
        (stage.path / "first.txt").write_text("committed\n")
        self.commit(stage.path)
        (stage.path / "second.txt").write_text("indexed\n")
        self.manager._git(stage.path, "add", "second.txt")
        (stage.path / "first.txt").write_text("working\n")
        artifact = self.manager.diff(stage)
        self.assertTrue(b"+committed" in artifact, repr(artifact[:80]))
        self.assertFalse(b"+working" in artifact, repr(artifact[:80]))
        self.assertFalse(b"+indexed" in artifact, repr(artifact[:80]))
        self.assertFalse((self.repo / "first.txt").exists())

    def test_untracked_edits_are_not_part_of_commit(self):
        stage = self.stage()
        (stage.path / "new.txt").write_text("keep")
        self.assertEqual(self.manager.diff(stage), b"")
        self.assertEqual((stage.path / "new.txt").read_text(), "keep")

    def test_no_git_uses_stage_after_worker_start(self):
        stage = self.stage()
        self.change(stage, "committed.txt")
        with patch("subprocess.run", wraps=subprocess.run) as run, patch(
                "subprocess.Popen", wraps=subprocess.Popen) as popen:
            artifact = self.manager.diff(stage)
            self.assertTrue(b"+committed.txt" in artifact, repr(artifact[:80]))
            with self.assertRaisesRegex(StageError, "cleanliness"):
                self.manager._clean(stage.path)
            self.manager.assert_apply_ready(stage)
            commit = self.rev(stage.branch)
            self.manager.apply(stage, commit)
            self.manager.undo(self.repo, "run1")
            self.manager.cleanup(stage)
        for call in [*run.call_args_list, *popen.call_args_list]:
            argv = call.args[0]
            locations = [call.kwargs.get("cwd"), call.kwargs.get("env", {}).get("GIT_DIR")]
            for i, arg in enumerate(argv):
                if arg in ("-C", "--git-dir"):
                    locations.append(argv[i + 1])
                elif arg.startswith("--git-dir="):
                    locations.append(arg.split("=", 1)[1])
            for location in locations:
                if location is not None:
                    self.assertNotEqual(Path(location).absolute(), stage.path)
                    self.assertNotEqual(Path(location).absolute(), stage.path / ".git")

    def test_stage_include_is_never_reached(self):
        stage = self.stage()
        included = self.home / "bad-config"
        included.write_text("this is invalid git configuration")
        config = stage.path / ".git" / "config"
        with config.open("a") as handle:
            handle.write('[include]\n path = "' + included.as_posix() + '"\n')
        control = subprocess.run([self.manager.git.argv0(workspace=(stage.path,)),
                                  "-C", str(stage.path), "rev-parse", "HEAD"], capture_output=True)
        self.assertNotEqual(control.returncode, 0)
        self.assertIn(b"bad config", control.stderr.lower())
        with patch("subprocess.run", wraps=subprocess.run) as run, patch(
                "subprocess.Popen", wraps=subprocess.Popen) as popen:
            with self.assertRaisesRegex(StageError, "includes"):
                self.manager.diff(stage)
            self.manager.assert_apply_ready(stage)
            self.manager.cleanup(stage)
        for call in [*run.call_args_list, *popen.call_args_list]:
            self.assertNotIn(str(stage.path), call.args[0])
        self.assertFalse(stage.path.exists())

    def test_corrupt_or_missing_stage_object_refused(self):
        for kind in ("corrupt", "missing"):
            with self.subTest(kind=kind):
                stage = self.manager.create(self.repo, kind, "slice")
                (stage.path / "file.txt").write_text("original content")
                self.commit(stage.path)
                oid = self.manager._git(stage.path, "rev-parse", "HEAD:file.txt").decode().strip()
                obj = stage.path / ".git" / "objects" / oid[:2] / oid[2:]
                obj.chmod(stat.S_IREAD | stat.S_IWRITE)
                if kind == "corrupt":
                    obj.write_bytes(zlib.compress(b"blob 4\0evil"))
                else:
                    obj.unlink()
                with self.assertRaises(StageError):
                    self.manager.diff(stage)

    def test_wrong_parent_refused(self):
        stage = self.stage()
        tree = self.manager._git(stage.path, "mktree", input=b"").decode().strip()
        commit = self.manager._git(stage.path, "commit-tree", tree, input=b"orphan").decode().strip()
        self.manager._git(stage.path, "update-ref", "HEAD", commit)
        with self.assertRaises(StageError):
            self.manager.diff(stage)

    def test_unexpected_file_mode_change_refused(self):
        (self.repo / "file.txt").write_text("base")
        self.commit(self.repo)
        stage = self.stage()
        self.manager._git(stage.path, "update-index", "--chmod=+x", "file.txt")
        tree = self.manager._git(stage.path, "write-tree").decode().strip()
        commit = self.manager._git(stage.path, "commit-tree", tree, "-p", stage.base,
                                   input=b"mode change").decode().strip()
        self.manager._git(stage.path, "update-ref", "HEAD", commit)
        with self.assertRaisesRegex(StageError, "file-mode"):
            self.manager.diff(stage)

    def test_oversized_import_refused(self):
        stage = self.stage()
        (stage.path / "large.txt").write_bytes(bytes(range(256)) * 32)
        self.commit(stage.path)
        with self.assertRaises(StageError):
            self.manager.diff(stage, max_bytes=1)

    def test_target_filter_never_fires_in_manager_calls(self):
        marker = self.plant(self.repo)
        self.control_fires(self.repo, marker)

        def silent(call):
            # Every call meets a stat-dirty file, so an unpinned status would filter.
            self.dirty(self.repo / "base.txt")
            result = call()
            self.assertFalse(marker.exists(), "a planted driver fired")
            return result

        stage = silent(self.stage)
        (stage.path / "new.txt").write_text("new")
        commit = self.commit(stage.path)
        artifact = silent(lambda: self.manager.diff(stage))
        self.assertTrue(b"+new" in artifact, repr(artifact[:80]))
        silent(lambda: self.manager.assert_apply_ready(stage))
        self.publish(stage)
        silent(lambda: self.manager.apply(stage, commit))
        silent(lambda: self.manager.undo(self.repo, "run1"))
        silent(lambda: self.manager.cleanup(stage))
        silent(lambda: self.manager._git(self.repo, "status"))

    def test_equals_in_filter_driver_name_is_refused_by_every_target_call(self):
        stage = self.stage()
        marker = self.plant(self.repo, "a=b", kinds=("filter",))
        self.control_fires(self.repo, marker)
        for call in (lambda: self.manager._git(self.repo, "status"),
                     lambda: self.manager._clean(self.repo),
                     lambda: self.manager.assert_apply_ready(stage),
                     lambda: self.manager.create(self.repo, "run2", "slice2"),
                     lambda: self.manager._repository(self.repo),
                     lambda: self.manager.undo(self.repo, "run1")):
            self.dirty(self.repo / "base.txt")
            with self.assertRaisesRegex(StageError, "driver name"):
                call()
            self.assertFalse(marker.exists(), "a planted driver fired")

    def test_equals_in_textconv_driver_name_is_refused(self):
        marker = self.plant(self.repo, "a=b", kinds=("diff",))
        with open(self.repo / "base.txt", "a", encoding="utf-8") as file:
            file.write("# changed\n")
        self.control_fires(self.repo, marker, "diff")
        with self.assertRaisesRegex(StageError, "driver name"):
            self.manager._git(self.repo, "diff")
        self.assertFalse(marker.exists(), "a planted driver fired")

    def test_safe_textconv_driver_is_pinned(self):
        marker = self.plant(self.repo, kinds=("diff",))
        with open(self.repo / "base.txt", "a", encoding="utf-8") as file:
            file.write("# changed\n")
        self.commit(self.repo)
        # show, not diff: the diff.external pin already stops plain "git diff"
        # before textconv, so only show proves the textconv pin itself.
        self.control_fires(self.repo, marker, "show", "HEAD")
        try:
            self.manager._git(self.repo, "show", "HEAD")
        except StageError:
            pass  # An emptied textconv may fail the diff; it must not run the driver.
        self.assertFalse(marker.exists(), "a planted driver fired")

    def test_safe_diff_command_driver_is_pinned(self):
        marker = self.plant(self.repo, kinds=("diff",))
        self.plain(self.repo, "config", "diff.planted.command", Path(sys.executable).as_posix())
        with open(self.repo / "base.txt", "a", encoding="utf-8") as file:
            file.write("# changed\n")
        self.control_fires(self.repo, marker, "diff")
        # The empty pin fails this diff closed, like the diff.external pin.
        try:
            self.manager._git(self.repo, "diff")
        except StageError:
            pass
        self.assertFalse(marker.exists(), "a planted driver fired")

    def test_committed_gitlink_with_staged_deletion_is_refused(self):
        outer = self.manager.create_repo(self.home / "outer")
        self.manager.create_repo(outer / "inner")
        self.commit(outer)
        self.plain(outer, "rm", "--cached", "inner")
        # Excluded, so only the staged deletion could reveal the gitlink.
        (outer / ".git" / "info").mkdir(exist_ok=True)
        (outer / ".git" / "info" / "exclude").write_text("inner/\n")
        with self.assertRaisesRegex(StageError, "submodule gitlink"):
            self.manager._clean(outer)

    def test_gitlink_staged_but_never_committed_is_refused(self):
        outer = self.manager.create_repo(self.home / "outer")
        self.manager.create_repo(outer / "inner")
        self.plain(outer, "add", "inner")
        with self.assertRaisesRegex(StageError, "repository contains a submodule gitlink"):
            self.manager._clean(outer)

    def test_pins_travel_whole_through_the_environment(self):
        self.plant(self.repo)
        with patch("jarvis.write_stage.subprocess.run", wraps=subprocess.run) as run:
            self.manager._git(self.repo, "rev-parse", "HEAD")
        call = run.call_args_list[-1]
        env = call.kwargs["env"]
        pins = {env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
                for index in range(int(env["GIT_CONFIG_COUNT"]))}
        for key in ("filter.planted.clean", "filter.planted.smudge",
                    "filter.planted.process", "diff.planted.textconv"):
            self.assertEqual(pins[key], "", key)
        self.assertEqual(pins["filter.planted.required"], "false")
        self.assertFalse(any("planted" in item for item in call.args[0]), "pin left in argv")

    def test_unusual_but_safe_driver_names_are_pinned(self):
        for index, name in enumerate(("a.b", "MiXeD", "ünï")):
            with self.subTest(index=index):
                repo = self.manager.create_repo(self.home / f"names{index}")
                marker = self.plant(repo, name, kinds=("filter",))
                self.control_fires(repo, marker)
                self.dirty(repo / "base.txt")
                self.manager._git(repo, "status")
                self.assertFalse(marker.exists(), "a planted driver fired")

    def test_unsafe_names_in_config_are_refused(self):
        for index, (data, expected) in enumerate((
                (b'[filter "a\x01b"]\n\tclean = x\n', "driver name"),
                (b'[filter "a\tb"]\n\tsmudge = x\n', "driver name"),
                (b'[filter "a\x7fb"]\n\tprocess = x\n', "driver name"),
                (b'[diff "a=b"]\n\tcommand = x\n', "driver name"),
                (b'[filter "a\xffb"]\n\tclean = x\n', "UnicodeDecodeError"))):
            with self.subTest(index=index):
                repo = self.tracked_repo(f"unsafe{index}")
                self.poison(repo, data)
                with self.assertRaisesRegex(StageError, expected):
                    self.manager._git(repo, "status")

    def test_section_only_filter_key_names_no_driver(self):
        repo = self.tracked_repo("section")
        self.poison(repo, b"[filter]\n\tclean = x\n")
        self.manager._git(repo, "status")

    def test_unsafe_names_only_in_attributes_are_refused(self):
        for name, path, line in (
                ("plain", ".gitattributes", b"*.txt filter=a=b\n"),
                ("macro", ".gitattributes", b"[attr]evil filter=a=b\n*.txt evil\n"),
                ("nested", "sub/.gitattributes", b"*.txt filter=a=b\n"),
                ("info", ".git/info/attributes", b"base.txt filter=a=b\n"),
                ("textconv", ".gitattributes", b"*.txt diff=a=b\n"),
                ("control", ".gitattributes", b"*.txt filter=a\x01b\n")):
            with self.subTest(name=name):
                repo = self.tracked_repo(f"attr-{name}")
                (repo / "sub").mkdir()
                (repo / "sub" / "file.txt").write_text("file\n")
                (repo / path).parent.mkdir(exist_ok=True)
                (repo / path).write_bytes(line)
                self.commit(repo)
                with self.assertRaisesRegex(StageError, "driver name"):
                    self.manager._clean(repo)

    def test_submodule_gitlink_is_refused_before_its_driver_can_run(self):
        for index, rel in enumerate(("inner", "sub/inner")):
            with self.subTest(rel=rel):
                outer = self.manager.create_repo(self.home / f"outer{index}")
                stage = self.manager.create(outer, f"run{index}", "slice1")
                (outer / rel).parent.mkdir(exist_ok=True)
                inner = self.manager.create_repo(outer / rel)
                marker = self.plant(inner)
                self.commit(outer)
                self.control_fires(inner, marker)
                self.dirty(inner / "base.txt")
                self.plain(outer, "status", "--porcelain", "--ignore-submodules=none")
                self.assertTrue(marker.exists(), "control: status must descend and fire")
                marker.unlink()
                for call in (lambda: self.manager._clean(outer),
                             lambda: self.manager.assert_apply_ready(stage)):
                    self.dirty(inner / "base.txt")
                    with self.assertRaisesRegex(StageError, "submodule"):
                        call()
                    self.assertFalse(marker.exists(), "a submodule driver fired")

    def test_gitlink_is_read_before_status_and_status_ignores_submodules(self):
        calls = self.argvs(lambda: self.manager._clean(self.repo))
        listed = next(i for i, argv in enumerate(calls) if "ls-files" in argv)
        status = next(i for i, argv in enumerate(calls) if "status" in argv)
        self.assertLess(listed, status)
        self.assertIn("-s", calls[listed])
        self.assertIn("--ignore-submodules=all", calls[status])

    def test_subdirectory_of_target_is_preflighted_and_refused(self):
        sub = self.repo / "sub"
        sub.mkdir()
        with self.assertRaisesRegex(StageError, "repository root"):
            self.manager._clean(sub)
        marker = self.plant(self.repo, "a=b", kinds=("filter",))
        self.control_fires(self.repo, marker)
        for call in (lambda: self.manager._git(sub, "status"),
                     lambda: self.manager._clean(sub)):
            self.dirty(self.repo / "base.txt")
            with self.assertRaisesRegex(StageError, "driver name"):
                call()
            self.assertFalse(marker.exists(), "a planted driver fired")

    def test_git_environment_failure_is_a_stage_error(self):
        with patch("jarvis.write_stage.subprocess.run", side_effect=OSError("gone")):
            with self.assertRaisesRegex(StageError, "OSError"):
                self.manager._git(self.repo, "status")

    def test_delete_wraps_os_and_recursion_errors(self):
        doomed = self.home / "doomed"
        doomed.mkdir()
        for error in (PermissionError, RecursionError):
            with patch("jarvis.write_stage.os.scandir", side_effect=error("stop")):
                with self.assertRaisesRegex(StageError, error.__name__):
                    self.manager._delete(doomed)

    def test_cleanup_does_not_follow_symlink_or_junction(self):
        outside = self.home / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep")
        stage = self.stage()
        link = stage.path / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            # Exercise the symlink branch even where Windows denies link creation.
            from types import SimpleNamespace
            info = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
            with patch("jarvis.write_stage.os.lstat", return_value=info), patch.object(
                    Path, "unlink") as unlink, patch("jarvis.write_stage.os.scandir") as scan:
                self.manager._delete(link)
            unlink.assert_called_once_with()
            scan.assert_not_called()
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())
        self.assertEqual(sentinel.read_text(), "keep")
        stage = self.manager.create(self.repo, "run1", "junction")
        link = stage.path / "escape"
        if os.name == "nt":
            # One shell end to end; verified paths are inside this temp workspace.
            self.assertTrue(link.absolute().is_relative_to(self.home))
            self.assertTrue(outside.absolute().is_relative_to(self.home))
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                    capture_output=True)
            if result.returncode == 0:
                self.manager.cleanup(stage)
                self.assertFalse(stage.path.exists())
                self.assertEqual(sentinel.read_text(), "keep")
                return
            print("junction creation denied; symlink deletion path tested")
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_dirty_target_refuses_apply(self):
        stage = self.stage()
        (self.repo / "new.txt").write_text("keep")
        with self.assertRaisesRegex(StageError, "dirty"):
            self.manager.assert_apply_ready(stage)
        self.assertEqual((self.repo / "new.txt").read_text(), "keep")

    def test_advanced_target_refuses_apply(self):
        stage = self.stage()
        (self.repo / "new.txt").write_text("new")
        self.commit(self.repo)
        with self.assertRaisesRegex(StageError, "advanced"):
            self.manager.assert_apply_ready(stage)

    def test_dirty_cleanup_deletes_disposable_stage(self):
        stage = self.stage()
        (stage.path / "new.txt").write_text("discard")
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())

    def test_unmerged_cleanup_deletes_disposable_stage(self):
        stage = self.stage()
        (stage.path / "new.txt").write_text("discard")
        self.commit(stage.path)
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())
        self.assertEqual(self.rev("HEAD"), stage.base)

    def test_hidden_index_does_not_block_disposable_cleanup(self):
        (self.repo / "file.txt").write_text("base")
        self.commit(self.repo)
        stage = self.stage()
        self.manager._git(stage.path, "update-index", "--assume-unchanged", "file.txt")
        (stage.path / "file.txt").write_text("hidden edit")
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())
        self.assertEqual((self.repo / "file.txt").read_text(), "base")

    def test_ignored_files_do_not_block_cleanup(self):
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        self.commit(self.repo)
        stage = self.stage()
        (stage.path / "__pycache__").mkdir()
        (stage.path / "__pycache__" / "x.pyc").write_bytes(b"\0")
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())

    def test_ignored_target_files_allow_apply(self):
        (self.repo / ".gitignore").write_text("build/\n")
        self.commit(self.repo)
        stage = self.stage()
        (self.repo / "build" / "nested").mkdir(parents=True)
        (self.repo / "build" / "nested" / "out.txt").write_text("keep")
        (self.repo / ".git" / "info" / "exclude").write_text("uv.lock\n")
        (self.repo / "uv.lock").write_text("keep")
        self.manager.assert_apply_ready(stage)
        self.assertEqual((self.repo / "uv.lock").read_text(), "keep")

    def test_self_ignoring_gitignore_or_excludes_file_cannot_hide_work(self):
        stage = self.stage()
        (stage.path / ".gitignore").write_text("*\n")
        (stage.path / "work.py").write_text("keep")
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())
        stage = self.manager.create(self.repo, "run1", "target-check")
        excludes = self.home / "excludes"
        excludes.write_text("*\n")
        self.manager._git(self.repo, "config", "core.excludesFile", excludes.as_posix())
        (self.repo / "untracked.py").write_text("keep")
        with self.assertRaisesRegex(StageError, "untracked"):
            self.manager.assert_apply_ready(stage)

    def test_cleanup_deletes_tracked_and_untracked_changes(self):
        (self.repo / "tracked.txt").write_text("base")
        self.commit(self.repo)
        stage = self.stage()
        (stage.path / "tracked.txt").write_text("edit")
        (stage.path / "fresh.txt").write_text("new")
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())
        self.assertEqual((self.repo / "tracked.txt").read_text(), "base")

    def test_planted_post_checkout_hook_does_not_run(self):
        marker = self.home / "hook-ran"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!{Path(sys.executable).as_posix()}\nfrom pathlib import Path\n"
                        f"Path({str(marker)!r}).write_text('ran')\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        self.stage()
        self.assertFalse(marker.exists())
        unpinned = tuple(arg for pair in zip(GIT_PIN[::2], GIT_PIN[1::2])
                         if pair[1] != "core.hooksPath=" for arg in pair)
        with patch("jarvis.write_stage.GIT_PIN", unpinned):
            self.manager.create(self.repo, "run1", "control")
        self.assertFalse(marker.exists(), "independent stages never inherit target hooks")
        with patch("jarvis.write_stage.GIT_PIN", unpinned):
            self.manager._git(self.repo, "checkout", "HEAD")
        self.assertTrue(marker.exists(), "control: target checkout runs its hook")

    def test_git_error_carries_capped_stderr_tail(self):
        failed = subprocess.CompletedProcess([], 1, b"", b"\xff" + b"x" * 1000 + b"END")
        with patch("jarvis.write_stage.subprocess.run", side_effect=[
                subprocess.CompletedProcess([], 0, b"", b""), failed]):
            with self.assertRaises(StageError) as caught:
                self.manager._git(self.repo, "status")
        message = str(caught.exception)
        self.assertTrue(message.endswith("END"))
        self.assertLessEqual(len(message), len("git status exited 1: ") + 80)
        with self.assertRaisesRegex(StageError, "fatal"):
            self.manager.create(self.repo, "run", "slice", base="missing-ref")

    def test_identifiers_base_and_collisions(self):
        for value in ("../x", "-bad", "a/b", "", "a\\b"):
            with self.subTest(value=value), self.assertRaises(StageError):
                self.manager.create(self.repo, value, "slice")
        with self.assertRaises(StageError):
            self.manager.create(self.repo, "run", "slice", base="--all")
        self.stage()
        with self.assertRaises(StageError):
            self.stage()

    def test_wrong_stage_identity_refused(self):
        stage = self.stage()
        with self.assertRaises(StageError):
            self.manager.cleanup(replace(stage, path=self.repo))
        self.manager._git(stage.path, "checkout", "--detach")
        with self.assertRaises(StageError):
            self.manager.diff(stage)

    def test_git_pins_hooks_and_never_forces(self):
        self.assertIn("core.hooksPath=", GIT_PIN)
        with patch("jarvis.write_stage.subprocess.run", wraps=__import__("subprocess").run) as run:
            stage = self.stage()
            self.manager.cleanup(stage)
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertIn("core.hooksPath=", argv)
            self.assertNotIn("--force", argv)
            self.assertFalse(call.kwargs["shell"])
            self.assertTrue(Path(argv[0]).is_absolute())

    def rev(self, ref):
        return self.manager._git(self.repo, "rev-parse", ref).decode().strip()

    def publish(self, stage):
        # The legacy apply API still expects target-local objects and a slice ref.
        # Verified worker import belongs to the subsequent redesign slices.
        sha = self.manager._git(stage.path, "rev-parse", "HEAD").decode().strip()
        self.manager._seed(stage.path, self.repo, sha)
        self.manager._git(self.repo, "update-ref", f"refs/heads/{stage.branch}", sha)

    def change(self, stage, name):
        (stage.path / name).write_text(name + "\n")
        sha = self.commit(stage.path)
        self.publish(stage)
        return sha

    def test_apply_creates_checkpoint_and_undo_restores_exact_tree(self):
        stage = self.stage()
        before = self.rev(f"{stage.base}^{{tree}}")
        commit = self.change(stage, "a.txt")
        checkpoint = self.manager.apply(stage, commit)
        self.assertEqual(checkpoint, "refs/jarvis/checkpoints/run1/1")
        self.assertEqual(self.rev(checkpoint), stage.base)
        self.assertEqual(self.rev("refs/heads/jarvis/run1/main"), commit)
        self.assertEqual(self.manager.undo(self.repo, "run1"), stage.base)
        self.assertEqual(self.rev("jarvis/run1/main^{tree}"), before)
        self.assertEqual(self.manager._git(self.repo, "for-each-ref", "refs/jarvis/"), b"")
        with self.assertRaisesRegex(StageError, "no checkpoint"):
            self.manager.undo(self.repo, "run1")

    def test_two_applies_and_two_undos(self):
        first = self.stage()
        one = self.change(first, "a.txt")
        self.manager.apply(first, one)
        second = self.manager.create(self.repo, "run1", "slice2", base=one)
        two = self.change(second, "b.txt")
        self.assertEqual(self.manager.apply(second, two), "refs/jarvis/checkpoints/run1/2")
        self.assertEqual(self.rev("refs/jarvis/checkpoints/run1/1"), first.base)
        self.assertEqual(self.rev("refs/jarvis/checkpoints/run1/2"), one)
        trees = [self.rev(f"{sha}^{{tree}}") for sha in (first.base, one)]
        self.assertEqual(self.manager.undo(self.repo, "run1"), one)
        self.assertEqual(self.rev("jarvis/run1/main^{tree}"), trees[1])
        self.assertEqual(self.manager.undo(self.repo, "run1"), first.base)
        self.assertEqual(self.rev("jarvis/run1/main^{tree}"), trees[0])

    def test_non_fast_forward_apply_refused(self):
        first, second = self.stage(), self.manager.create(self.repo, "run1", "slice2")
        one = self.change(first, "a.txt")
        two = self.change(second, "b.txt")
        self.manager.apply(first, one)
        with self.assertRaisesRegex(StageError, "non-fast-forward"):
            self.manager.apply(second, two)
        self.assertEqual(self.rev("refs/heads/jarvis/run1/main"), one)
        self.assertEqual(self.manager._git(self.repo, "for-each-ref", "--format=%(refname)",
                                           "refs/jarvis/").decode().split(), ["refs/jarvis/checkpoints/run1/1"])
        with self.assertRaisesRegex(StageError, "stage branch tip"):
            self.manager.apply(first, first.base)

    def test_integration_branch_name_cannot_clash(self):
        for slice_id in ("main", "MAIN", "Main"):
            with self.subTest(slice_id=slice_id), self.assertRaisesRegex(StageError, "reserved"):
                self.manager.create(self.repo, "run1", slice_id)
        stage = self.manager.create(self.repo, "main", "slice1")
        commit = self.change(stage, "a.txt")
        self.assertEqual(self.manager.apply(stage, commit), "refs/jarvis/checkpoints/main/1")
        self.assertEqual(self.rev("refs/heads/jarvis/main/main"), commit)
        self.assertEqual(self.rev("refs/heads/jarvis/main/slice1"), commit)

    def test_checked_out_integration_branch_refused(self):
        stage = self.stage()
        commit = self.change(stage, "a.txt")
        self.manager.apply(stage, commit)
        self.manager._git(self.repo, "checkout", "jarvis/run1/main")
        with self.assertRaisesRegex(StageError, "checked out"):
            self.manager.undo(self.repo, "run1")
        self.assertEqual(self.rev("refs/heads/jarvis/run1/main"), commit)

    def test_apply_and_undo_never_force(self):
        stage = self.stage()
        commit = self.change(stage, "a.txt")
        with patch("jarvis.write_stage.subprocess.run", wraps=subprocess.run) as run:
            self.manager.apply(stage, commit)
            self.manager.undo(self.repo, "run1")
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertIn("core.hooksPath=", argv)
            self.assertFalse(any("force" in arg for arg in argv))
            self.assertNotIn(b"force", call.kwargs["input"] or b"")
            self.assertNotIn("push", argv)

    def test_git_failure_is_closed(self):
        with self.assertRaises(StageError):
            self.manager.create(self.repo, "run", "slice", base="missing-ref")
        self.assertFalse((self.manager.root / "run" / "slice").exists())


if __name__ == "__main__":
    unittest.main()
