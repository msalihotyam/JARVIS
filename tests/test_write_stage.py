"""Offline staging tests: every repository lives beneath TEMP."""

import os
import subprocess
import tempfile
import unittest
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
        self.assertEqual(self.manager._git(self.repo, "rev-parse", stage.branch).decode().strip(), stage.base)

    def test_diff_includes_index_working_tree_and_commits(self):
        stage = self.stage()
        (stage.path / "first.txt").write_text("committed\n")
        self.commit(stage.path)
        (stage.path / "second.txt").write_text("indexed\n")
        self.manager._git(stage.path, "add", "second.txt")
        (stage.path / "first.txt").write_text("working\n")
        artifact = self.manager.diff(stage)
        self.assertIn(b"+working", artifact)
        self.assertIn(b"+indexed", artifact)
        self.assertFalse((self.repo / "first.txt").exists())

    def test_untracked_diff_refused(self):
        stage = self.stage()
        (stage.path / "new.txt").write_text("keep")
        with self.assertRaisesRegex(StageError, "untracked"):
            self.manager.diff(stage)

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

    def test_dirty_cleanup_preserves_files(self):
        stage = self.stage()
        (stage.path / "new.txt").write_text("keep")
        with self.assertRaises(StageError):
            self.manager.cleanup(stage)
        self.assertTrue((stage.path / "new.txt").exists())

    def test_unmerged_cleanup_preserves_commits(self):
        stage = self.stage()
        (stage.path / "new.txt").write_text("keep")
        sha = self.commit(stage.path)
        with self.assertRaises(StageError):
            self.manager.cleanup(stage)
        self.assertEqual(self.manager._git(stage.path, "rev-parse", "HEAD").decode().strip(), sha)
        self.manager._git(self.repo, "merge", "--ff-only", stage.branch)
        self.manager.cleanup(stage)
        self.assertFalse(stage.path.exists())

    def test_hidden_index_refuses_cleanup(self):
        (self.repo / "file.txt").write_text("base")
        self.commit(self.repo)
        stage = self.stage()
        self.manager._git(stage.path, "update-index", "--assume-unchanged", "file.txt")
        (stage.path / "file.txt").write_text("hidden edit")
        with self.assertRaisesRegex(StageError, "hidden"):
            self.manager.cleanup(stage)

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
        with self.assertRaisesRegex(StageError, "ignored .gitignore"):
            self.manager.cleanup(stage)
        self.assertTrue((stage.path / "work.py").exists())
        excludes = self.home / "excludes"
        excludes.write_text("*\n")
        self.manager._git(self.repo, "config", "core.excludesFile", excludes.as_posix())
        (self.repo / "untracked.py").write_text("keep")
        with self.assertRaisesRegex(StageError, "untracked"):
            self.manager.assert_apply_ready(stage)

    def test_cleanup_lists_tracked_and_untracked_changes(self):
        (self.repo / "tracked.txt").write_text("base")
        self.commit(self.repo)
        stage = self.stage()
        (stage.path / "tracked.txt").write_text("edit")
        (stage.path / "fresh.txt").write_text("new")
        with self.assertRaises(StageError) as caught:
            self.manager.cleanup(stage)
        self.assertIn("tracked.txt", str(caught.exception))
        self.assertIn("fresh.txt", str(caught.exception))
        self.assertTrue((stage.path / "fresh.txt").exists())

    def test_planted_post_checkout_hook_does_not_run(self):
        marker = self.home / "hook-ran"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f'#!/bin/sh\necho ran > "{marker.as_posix()}"\n', newline="\n")
        hook.chmod(0o755)
        self.stage()
        self.assertFalse(marker.exists())
        unpinned = tuple(arg for pair in zip(GIT_PIN[::2], GIT_PIN[1::2])
                         if pair[1] != "core.hooksPath=" for arg in pair)
        with patch("jarvis.write_stage.GIT_PIN", unpinned):
            self.manager.create(self.repo, "run1", "control")
        self.assertTrue(marker.exists(), "control: the hook runs without the pin")

    def test_git_error_carries_capped_stderr_tail(self):
        failed = subprocess.CompletedProcess([], 1, b"", b"\xff" + b"x" * 1000 + b"END")
        with patch("jarvis.write_stage.subprocess.run", return_value=failed):
            with self.assertRaises(StageError) as caught:
                self.manager._git(self.repo, "status")
        message = str(caught.exception)
        self.assertTrue(message.endswith("END"))
        self.assertLessEqual(len(message), len("git status exited 1: ") + 500)
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

    def change(self, stage, name):
        (stage.path / name).write_text(name + "\n")
        return self.commit(stage.path)

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
