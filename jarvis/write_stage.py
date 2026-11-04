# pyright: strict
"""Local staging primitives; callers own leases, scope gates and integration.

No app wiring or network operations. Diffs contain verified committed changes
only; worker index and working-tree edits are not part of the claimed commit.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jarvis import stage_git

from jarvis.trusted_exec import (
    NO_CONSOLE_WINDOW, resolve_trusted_executable, trusted_environment,
)

GIT_PIN = (
    "-c", "log.diffMerges=separate", "-c", "diff.ignoreSubmodules=none",
    "-c", "diff.relative=false", "-c", "status.showUntrackedFiles=all",
    "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
    "-c", "core.hooksPath=", "-c", "core.quotePath=true", "-c", "core.excludesFile=",
    "-c", "core.attributesFile=", "-c", "core.autocrlf=false",
    "-c", "diff.external=", "-c", "diff.trustExitCode=false",
    "-c", "core.useReplaceRefs=false", "-c", "core.commitGraph=false",
    "-c", "core.multiPackIndex=false", "-c", "pack.useBitmaps=false",
    "-c", "gc.auto=0", "-c", "maintenance.auto=false",
)
UNSAFE_DRIVER_NAME = re.compile(r"[=\x00-\x1f\x7f-\x9f]")


class StageError(RuntimeError):
    """A Git failure or an unsafe stage operation."""


@dataclass(frozen=True)
class Stage:
    target: Path
    path: Path
    branch: str
    base: str


class StageManager:
    """Manage independent repositories beneath a caller-selected staging directory.

Stage and per-run repositories own their objects. Legacy apply and cleanup
remain separate migration steps. This is not a diff-scope or approval gate.
"""

    def __init__(self, root: Path, *, timeout: float = 30):
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.git = resolve_trusted_executable("git", workspace=(self.root,))
        if self.git is None:
            raise StageError("trusted git is unavailable")

    def _git(self, cwd: Path, *args: str, input: bytes | None = None) -> bytes:
        git = self.git
        if git is None:
            raise StageError("trusted git is unavailable")
        env = trusted_environment(forced={
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "JARVIS",
            "GIT_AUTHOR_EMAIL": "jarvis@localhost", "GIT_COMMITTER_NAME": "JARVIS",
            "GIT_COMMITTER_EMAIL": "jarvis@localhost",
            "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
        })
        try:
            pins: dict[str, str] = {}
            # Only init may run where no repository exists yet. A subdirectory
            # of a repository has no .git of its own and still gets the preflight.
            if args[0] != "init" or (cwd / ".git").exists():
                # No includes are read by this query. Refuse them before the real
                # command; pin every locally named filter and textconv driver.
                config = subprocess.run(
                    [git.argv0(workspace=(self.root, cwd)), *GIT_PIN,
                     "-C", str(cwd), "config", "--local", "--no-includes", "--null", "--list"],
                    input=None, capture_output=True, timeout=self.timeout, env=env, shell=False,
                    creationflags=NO_CONSOLE_WINDOW,
                )
                if config.returncode:
                    tail = config.stderr.decode("utf-8", "replace").strip()[-80:]
                    raise StageError(f"git {args[0]} exited {config.returncode}: {tail}")
                for entry in config.stdout.split(b"\0"):
                    key = entry.split(b"\n", 1)[0].decode("utf-8", "strict")
                    if key.lower().startswith(("include.", "includeif.", "extensions.worktreeconfig")):
                        raise StageError("repository configuration includes another file")
                    section = key.split(".", 1)[0].lower()
                    if section not in ("filter", "diff") or key.count(".") < 2:
                        continue  # filter.clean or diff.external names no driver.
                    driver = key[:key.rfind(".")]
                    self._refuse_driver_name(driver.split(".", 1)[1])
                    if section == "filter":
                        pins.update({f"{driver}.clean": "", f"{driver}.smudge": "",
                                     f"{driver}.process": "", f"{driver}.required": "false"})
                    elif key.lower().endswith(".textconv"):
                        pins.update({key: "", f"{driver}.cachetextconv": "false"})
                    elif key.lower().endswith(".command"):
                        # Empty fails a diff closed, as the diff.external pin does.
                        pins[key] = ""
            # Git splits "-c k=v" at the first "=", so a name holding one would
            # pin the wrong key. These variables carry each key whole.
            env = {**env, "GIT_CONFIG_COUNT": str(len(pins))}
            for index, (key, value) in enumerate(pins.items()):
                env[f"GIT_CONFIG_KEY_{index}"] = key
                env[f"GIT_CONFIG_VALUE_{index}"] = value
            result = subprocess.run(
                [git.argv0(workspace=(self.root, cwd)), *GIT_PIN,
                 "-C", str(cwd), *args], input=input, capture_output=True,
                timeout=self.timeout, env=env, shell=False,
                creationflags=NO_CONSOLE_WINDOW,
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise StageError(f"git {args[0]} failed: {type(exc).__name__}") from exc
        if result.returncode:
            tail = result.stderr.decode("utf-8", "replace").strip()[-80:]
            raise StageError(f"git {args[0]} exited {result.returncode}: {tail}")
        return result.stdout

    @staticmethod
    def _refuse_driver_name(name: str) -> None:
        if UNSAFE_DRIVER_NAME.search(name):
            raise StageError("repository names an unsafe filter or diff driver name")

    @staticmethod
    def _identifier(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            raise StageError("invalid run or slice identifier")
        return value

    def _destination(self, run: str, slice_id: str) -> tuple[Path, str]:
        run, slice_id = self._identifier(run), self._identifier(slice_id)
        # jarvis/<run>/main is the run's integration branch (refs are case-insensitive on NTFS).
        if slice_id.casefold() == "main":
            raise StageError("slice identifier is reserved")
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
        if Path(path).absolute().is_relative_to(self.root):
            raise StageError("stage cleanliness is not queried after worker start")
        # ls-files only lists below its cwd, so this must be the root.
        path = self._repository(path)
        # A submodule's own config is never preflighted or pinned, so any Git
        # command that descends into one could run its drivers. Refuse first.
        index = self._git(path, "ls-files", "-s", "-z").split(b"\0")
        if any(entry.startswith(b"160000 ") for entry in index):
            raise StageError("repository contains a submodule gitlink")
        # The status below ignores submodules, so it cannot see a gitlink whose
        # deletion is staged; that one survives only in HEAD.
        committed = self._git(path, "ls-tree", "-r", "-z", "HEAD").split(b"\0")
        if any(entry.startswith(b"160000 ") for entry in committed):
            raise StageError("repository commits a submodule gitlink")
        tracked = b"\0".join(entry.split(b"\t", 1)[1] for entry in index if entry)
        if tracked:
            attributes = self._git(path, "check-attr", "-z", "--stdin", "filter", "diff",
                                   input=tracked + b"\0").split(b"\0")
            for value in attributes[2::3]:
                self._refuse_driver_name(value.decode("utf-8", "replace"))
        changes = self._git(path, "status", "--porcelain=v1", "--untracked-files=all",
                            "--ignore-submodules=all").decode("utf-8", "replace").splitlines()
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
        ref, current, _ = self._integration(target, run)
        owned = self.root.parent / (self.root.name + "-repos") / run
        if owned.resolve() != owned:
            raise StageError("run repository traverses a link")
        if owned.exists():
            for entry in (owned, owned / ".git", owned / ".git" / "objects"):
                try:
                    info = os.lstat(entry)
                except OSError as exc:
                    raise StageError("invalid run repository") from exc
                if (not stat.S_ISDIR(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise StageError("invalid run repository")
        else:
            owned.mkdir(parents=True)
            self._git(owned, "init", "--template=")
            self._git(owned, "config", "gc.auto", "0")
            self._seed(target, owned, sha)
            if current and current != sha:
                self._seed(target, owned, current)
            self._git(owned, "update-ref", ref, current or sha)
        if current is None:
            self._git(target, "update-ref", ref, sha, "0" * len(sha))
        path.mkdir()
        self._git(path, "init", "--template=")
        self._seed(target, path, sha)
        self._git(path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        self._git(path, "update-ref", "HEAD", sha)
        self._git(path, "read-tree", "-u", "--reset", sha)
        return Stage(target, path, branch, sha)

    def _seed(self, source: Path, destination: Path, sha: str) -> None:
        """Copy trusted history without links, remotes or alternates."""
        pack = self._git(source, "pack-objects", "--revs", "--stdout",
                         input=f"{sha}\n".encode())
        self._git(destination, "index-pack", "--stdin", input=pack)

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
        try:
            for entry in (stage.path.parent, stage.path, stage.path / ".git",
                          stage.path / ".git" / "objects",
                          stage.path / ".git" / "objects" / "info"):
                info = os.lstat(entry)
                if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise StageError("stage contains a link or non-directory")
            head = stage.path / ".git" / "HEAD"
            info = os.lstat(head)
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise StageError("stage HEAD is not a regular file")
            if head.read_bytes().decode("utf-8") != f"ref: refs/heads/{stage.branch}\n":
                raise StageError("stage branch mismatch")
            try:
                os.lstat(stage.path / ".git" / "objects" / "info" / "alternates")
            except FileNotFoundError:
                pass
            else:
                raise StageError("stage alternates refused")
        except (OSError, UnicodeError) as exc:
            raise StageError("invalid stage metadata") from exc

    def _claim(self, stage: Stage) -> str:
        """Read a bounded tip claim without invoking Git in the worker repo."""
        path = stage.path / ".git" / "refs" / "heads"
        try:
            info = os.lstat(path.parent)
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise StageError("stage refs traverses a link")
            for part in stage.branch.split("/"):
                info = os.lstat(path)
                if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise StageError("stage ref traverses a link")
                path /= part
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise StageError("stage ref is not a regular file")
            with path.open("rb") as handle:
                claim = handle.read(66)
        except FileNotFoundError:
            # packed-refs is a claim too, never an authority for object contents.
            path = stage.path / ".git" / "packed-refs"
            try:
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise StageError("invalid packed refs")
                with path.open("rb") as handle:
                    data = handle.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise StageError("packed refs too large")
                matches = [line.split(b" ")[0] for line in data.splitlines()
                           if line.endswith(b" refs/heads/" + stage.branch.encode())]
                if len(matches) != 1:
                    raise StageError("stage tip is missing or ambiguous")
                claim = matches[0] + b"\n"
            except OSError as exc:
                raise StageError("stage tip is missing") from exc
        except OSError as exc:
            raise StageError("invalid stage tip") from exc
        if not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\n", claim):
            raise StageError("invalid stage tip claim")
        return claim.rstrip(b"\n").decode("ascii")

    def diff(self, stage: Stage, *, max_bytes: int = 256 * 1024 * 1024) -> bytes:
        """Return base..C after verified import; index/worktree edits are excluded.

        The worker must have stopped. This is not OS confinement of that worker.
        """
        self._validate(stage)
        commit = self._claim(stage)
        owned = self.root.parent / (self.root.name + "-repos") / stage.branch.split("/")[1]
        try:
            stage_git.import_commit(stage.path, owned, commit, stage.base,
                                    max_bytes=max_bytes, timeout=self.timeout)
            raw = stage_git.run(owned, "diff-tree", "-r", "--raw", "--no-commit-id",
                                "--no-renames", stage.base, commit, timeout=self.timeout)
            for line in raw.splitlines():
                modes = line.split()[:2]
                if len(modes) != 2:
                    raise StageError("invalid file mode record")
                old, new = modes[0].lstrip(b":"), modes[1]
                if (any(mode not in (b"000000", b"100644", b"100755") for mode in (old, new))
                        or old != new and b"000000" not in (old, new)):
                    raise StageError("unexpected file-mode change")
            return stage_git.run(owned, "diff-tree", "-r", "-p", "--binary", "--no-renames",
                                 "--no-commit-id", stage.base, commit, timeout=self.timeout)
        except (stage_git.StageGitError, ValueError) as exc:
            raise StageError(f"stage import/diff refused: {str(exc)[:80]}") from exc

    def assert_apply_ready(self, stage: Stage) -> None:
        """Preflight only: integration must recheck atomically when applying."""
        self._validate(stage)
        self._clean(stage.target)
        if self._git(stage.target, "rev-parse", "HEAD").decode().strip() != stage.base:
            raise StageError("target advanced from the stage baseline")

    def _integration(self, target: Path, run: str) -> tuple[str, str | None, list[int]]:
        """Return the run's integration ref, its sha (None if absent) and checkpoint numbers."""
        ref = f"refs/heads/jarvis/{self._identifier(run)}/main"
        # update-ref never touches a working tree, so a checked-out integration branch would desync.
        if f"branch {ref}\n".encode() in self._git(target, "worktree", "list", "--porcelain"):
            raise StageError("integration branch is checked out")
        current = self._git(target, "for-each-ref", "--format=%(objectname)", ref).decode().strip() or None
        names = self._git(target, "for-each-ref", "--format=%(refname)",
                          f"refs/jarvis/checkpoints/{run}/").decode().split()
        numbers = sorted(int(name.rsplit("/", 1)[1]) for name in names)
        return ref, current, numbers

    def apply(self, stage: Stage, commit: str) -> str:
        """Fast-forward jarvis/<run>/main to an accepted stage commit; return the checkpoint ref."""
        self._validate(stage)
        run = stage.branch.split("/")[1]
        commit = self._git(stage.target, "rev-parse", "--verify", "--end-of-options",
                           f"{commit}^{{commit}}").decode().strip()
        if commit != self._git(stage.target, "rev-parse", "--verify", f"refs/heads/{stage.branch}").decode().strip():
            raise StageError("commit is not the stage branch tip")
        ref, current, numbers = self._integration(stage.target, run)
        old = current or stage.base
        try:
            self._git(stage.target, "merge-base", "--is-ancestor", old, commit)
        except StageError as exc:
            raise StageError("non-fast-forward apply refused") from exc
        checkpoint = f"refs/jarvis/checkpoints/{run}/{(numbers[-1] if numbers else 0) + 1}"
        main = f"update {ref} {commit} {current}" if current else f"create {ref} {commit}"
        # One transaction: the checkpoint must not exist and main must not have moved.
        self._git(stage.target, "update-ref", "--stdin",
                  input=f"start\ncreate {checkpoint} {old}\n{main}\nprepare\ncommit\n".encode())
        return checkpoint

    def undo(self, target: Path, run: str) -> str:
        """Move jarvis/<run>/main back to the last checkpoint and consume it; return the sha."""
        target = self._repository(target)
        ref, current, numbers = self._integration(target, run)
        if not numbers or current is None:
            raise StageError("no checkpoint to undo")
        checkpoint = f"refs/jarvis/checkpoints/{run}/{numbers[-1]}"
        sha = self._git(target, "rev-parse", "--verify", checkpoint).decode().strip()
        self._git(target, "update-ref", "--stdin", input=(
            f"start\nupdate {ref} {sha} {current}\ndelete {checkpoint} {sha}\nprepare\ncommit\n").encode())
        return sha

    def cleanup(self, stage: Stage) -> None:
        """Delete a stopped worker's disposable stage without following links."""
        self._validate(stage)
        self._delete(stage.path)

    @classmethod
    def _delete(cls, path: Path) -> None:
        try:
            cls._remove(path)
        except (OSError, RecursionError) as exc:
            raise StageError(f"stage delete failed: {type(exc).__name__}") from exc

    @classmethod
    def _remove(cls, path: Path) -> None:
        info = os.lstat(path)
        linked = stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
        if linked:
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                path.rmdir()  # Windows junction: remove only the reparse point.
            else:
                path.unlink()
        elif stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                children = [Path(entry.path) for entry in entries]
            for child in children:
                cls._remove(child)
            path.rmdir()
        else:
            # Git's object files can be read-only on Windows.
            path.chmod(stat.S_IREAD | stat.S_IWRITE)
            path.unlink()
