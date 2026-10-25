"""Offline staging tests: every repository lives beneath TEMP."""

import os
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

    def test_ignored_files_refuse_cleanup(self):
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        self.commit(self.repo)
        stage = self.stage()
        (stage.path / "ignored.txt").write_text("keep")
        with self.assertRaises(StageError):
            self.manager.cleanup(stage)
        self.assertTrue((stage.path / "ignored.txt").exists())

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

    def test_git_failure_is_closed(self):
        with self.assertRaises(StageError):
            self.manager.create(self.repo, "run", "slice", base="missing-ref")
        self.assertFalse((self.manager.root / "run" / "slice").exists())


if __name__ == "__main__":
    unittest.main()
