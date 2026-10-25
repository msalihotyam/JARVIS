"""Local staging primitives; callers own leases, scope gates and integration.

No app wiring, network operations, branch deletion or forced cleanup. Diffs
include committed, indexed and working-tree changes relative to a pinned base;
untracked files must be indexed by the worker before an artifact is extracted.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jarvis.trusted_exec import (
    NO_CONSOLE_WINDOW, resolve_trusted_executable, trusted_environment,
)

GIT_PIN = (
    "-c", "log.diffMerges=separate", "-c", "diff.ignoreSubmodules=none",
    "-c", "diff.relative=false", "-c", "status.showUntrackedFiles=all",
    "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
    "-c", "core.hooksPath=", "-c", "core.quotePath=true", "-c", "core.excludesFile=",
)


class StageError(RuntimeError):
    """A Git failure or an unsafe stage operation (nothing is discarded)."""


@dataclass(frozen=True)
class Stage:
    target: Path
    path: Path
    branch: str
    base: str


class StageManager:
    """Manage linked worktrees beneath a caller-selected staging directory.

New project repositories are retained permanently; cleanup only removes linked
worktrees whose commits are already reachable from the target's HEAD. Branches
are retained as recovery refs. This is not a diff-scope or approval gate.
"""

    def __init__(self, root: Path, *, timeout: float = 30):
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.git = resolve_trusted_executable("git", workspace=(self.root,))
        if self.git is None:
            raise StageError("trusted git is unavailable")

    def _git(self, cwd: Path, *args: str, input: bytes | None = None) -> bytes:
        env = trusted_environment(forced={
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "JARVIS",
            "GIT_AUTHOR_EMAIL": "jarvis@localhost", "GIT_COMMITTER_NAME": "JARVIS",
            "GIT_COMMITTER_EMAIL": "jarvis@localhost",
        })
        try:
            result = subprocess.run(
                [self.git.argv0(workspace=(self.root, cwd)), *GIT_PIN,
                 "-C", str(cwd), *args], input=input, capture_output=True,
                timeout=self.timeout, env=env, shell=False,
                creationflags=NO_CONSOLE_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StageError(f"git {args[0]} failed: {type(exc).__name__}") from exc
        if result.returncode:
            tail = result.stderr.decode("utf-8", "replace").strip()[-500:]
            raise StageError(f"git {args[0]} exited {result.returncode}: {tail}")
        return result.stdout

    @staticmethod
    def _identifier(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            raise StageError("invalid run or slice identifier")
        return value

    def _destination(self, run: str, slice_id: str) -> tuple[Path, str]:
        run, slice_id = self._identifier(run), self._identifier(slice_id)
        path = self.root / run / slice_id
        if path.resolve() != path or path.exists():
            raise StageError("stage destination exists or traverses a link")
        return path, f"jarvis/{run}/{slice_id}"

    def _repository(self, target: Path) -> Path:
        target = Path(target).resolve(strict=True)
        actual = Path(self._git(target, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        if actual != target:
            raise StageError("target must be a repository root")
        return target

    def _clean(self, path: Path) -> None:
        """Refuse tracked or untracked changes; ignored files are tolerated."""
        changes = self._git(path, "status", "--porcelain=v1", "--untracked-files=all",
                            "--ignore-submodules=none").decode("utf-8", "replace").splitlines()
        if changes:
            raise StageError(f"repository contains {len(changes)} dirty or untracked files: "
                             + "; ".join(changes)[:500])
        # An ignore file that ignores itself would hide untracked work from status.
        ignored = self._git(path, "ls-files", "--others", "--ignored", "--exclude-standard",
                            "--directory", "-z").split(b"\0")
        if any(entry.rsplit(b"/", 1)[-1] == b".gitignore" for entry in ignored):
            raise StageError("repository contains an ignored .gitignore")
        entries = self._git(path, "ls-files", "-v").splitlines()
        if any(not line.startswith(b"H ") for line in entries):
            raise StageError("repository contains hidden index entries")

    def create(self, target: Path, run: str, slice_id: str, *, base: str = "HEAD") -> Stage:
        """Pin a local commit and create a fresh branch; never reuse a stage."""
        target = self._repository(target)
        path, branch = self._destination(run, slice_id)
        if not base or base.startswith("-"):
            raise StageError("invalid base")
        sha = self._git(target, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(target, "worktree", "add", "-b", branch, str(path), sha)
        return Stage(target, path, branch, sha)

    def create_repo(self, path: Path) -> Path:
        """Initialize a new project with an empty baseline; never reuse a folder."""
        path = Path(path).absolute()
        if path.exists() or path.resolve() != path:
            raise StageError("new project destination exists or traverses a link")
        path.mkdir(parents=True)
        self._git(path, "init")
        tree = self._git(path, "mktree", input=b"").decode().strip()
        commit = self._git(path, "commit-tree", tree, input=b"Project baseline\n").decode().strip()
        self._git(path, "update-ref", "HEAD", commit)
        return path

    def _validate(self, stage: Stage) -> None:
        expected = self.root / stage.branch.removeprefix("jarvis/")
        if (not re.fullmatch(r"jarvis/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", stage.branch)
                or stage.path != expected or stage.path.resolve() != expected
                or stage.target.resolve() == stage.path):
            raise StageError("stage identity mismatch")
        common = self._git(stage.path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        target_common = self._git(stage.target, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        branch = self._git(stage.path, "symbolic-ref", "--short", "HEAD").decode().strip()
        if common != target_common or branch != stage.branch:
            raise StageError("stage repository or branch mismatch")

    def diff(self, stage: Stage) -> bytes:
        """Return a binary-capable patch; refuse to silently omit new files."""
        self._validate(stage)
        if self._git(stage.path, "ls-files", "--others", "--exclude-standard", "-z"):
            raise StageError("untracked files must be indexed before diff extraction")
        return self._git(stage.path, "diff", "--binary", "--no-ext-diff", "--no-textconv",
                         "--no-renames", "--ignore-submodules=none", stage.base, "--")

    def assert_apply_ready(self, stage: Stage) -> None:
        """Preflight only: integration must recheck atomically when applying."""
        self._validate(stage)
        self._clean(stage.target)
        if self._git(stage.target, "rev-parse", "HEAD").decode().strip() != stage.base:
            raise StageError("target advanced from the stage baseline")

    def cleanup(self, stage: Stage) -> None:
        self._validate(stage)
        self._clean(stage.path)
        self._git(stage.target, "merge-base", "--is-ancestor", stage.branch, "HEAD")
        self._git(stage.target, "worktree", "remove", str(stage.path))
