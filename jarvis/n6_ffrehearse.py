# pyright: strict
"""N6 G25: fast-forward, deploy and previous-version rollback rehearsal (R20), shadow only.

Everything runs inside one sandbox this module creates under ``base``: a temp
git repo it seeds itself and a temp install dir. It fast-forwards the sandbox
``main`` to a candidate with ``merge --ff-only`` after the same checks the
conveyor's ``fast_forward`` makes (on main, clean, candidate descends from
HEAD), verifies the tree, "deploys" by exporting the commit into
``install/app`` with a ``VERSION`` marker beside it, rolls back to the previous
version by renames, and verifies the restored tree byte for byte against a
fresh export of the previous commit.

Fail closed: every step refuses a path whose realpath is not the sandbox's own
repo or install dir, and a path overlapping the repo JARVIS runs from (JARVIS
never replaces its own running code). A known-bad state is ``refused``; an
unreadable, malformed or failed input is ``unknown``, never a guess. Each step
records one receipt and a rehearsal stops at the first step that does not
pass. Nothing here pushes, deletes, writes conveyor files or uses the network;
it is not wired into the conveyor or orchbot.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from jarvis import stage_git
from jarvis.trusted_exec import (
    NO_CONSOLE_WINDOW,
    TrustedExecutableError,
    resolve_trusted_executable,
    trusted_environment,
)

Status = Literal["pass", "refused", "unknown"]

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_RUNNING_REPO = Path(__file__).resolve().parents[1]
_MAIN = "refs/heads/main"
_V1: dict[str, bytes] = {"a.txt": b"one\n", "keep.txt": b"same\r\n", "gone.txt": b"old\n"}
_V2: dict[str, bytes] = {"a.txt": b"two\n", "keep.txt": b"same\r\n", "sub/new.bin": b"\x00\xff\n"}


@dataclass(frozen=True)
class Receipt:
    """One step's outcome: what ran, how it ended, why, and when (injected clock)."""

    step: str
    status: Status
    detail: str
    at: str


@dataclass(frozen=True)
class Rehearsal:
    """The whole rehearsal: ``pass`` only when every step passed."""

    status: Status
    receipts: tuple[Receipt, ...]


class Git(Protocol):
    """Runs git in ``cwd``; returns (exit code, stdout). Raises ``GitUnavailable``."""

    def __call__(self, cwd: Path, *args: str) -> tuple[int, str]: ...


class GitUnavailable(RuntimeError):
    """Git could not be run at all (missing, untrusted, timed out): the answer is unknown."""


def _utc() -> datetime:
    return datetime.now(UTC)


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(path))


def _inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives
        return False


def _overlaps(a: str, b: str) -> bool:
    return _inside(a, b) or _inside(b, a)


def sandbox_git(root: Path) -> Git:
    """Trusted git confined to ``root``: no system/global config, no hooks, no discovery above it."""
    empty = root / "empty-config"
    empty.write_bytes(b"")
    exe = resolve_trusted_executable("git", workspace=(root,))
    env = trusted_environment(forced={
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty), "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat",
        "GIT_CEILING_DIRECTORIES": str(root), "GIT_NO_REPLACE_OBJECTS": "1",
    })
    pins = ("-c", "core.autocrlf=false", "-c", f"core.hooksPath={root / 'no-hooks'}",
            "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
            "-c", "user.name=n6-rehearsal", "-c", "user.email=n6-rehearsal@invalid")

    def run(cwd: Path, *args: str) -> tuple[int, str]:
        if exe is None:
            raise GitUnavailable("trusted git is unavailable")
        try:
            result = subprocess.run(  # noqa: S603 - list argv, trusted absolute git, shell=False
                [exe.argv0(workspace=(root,)), "--no-pager", *pins, *args],
                cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, env=env, shell=False, creationflags=NO_CONSOLE_WINDOW, check=False,
            )
        except (OSError, subprocess.TimeoutExpired, TrustedExecutableError) as exc:
            raise GitUnavailable(f"git {args[0]} failed: {type(exc).__name__}") from exc
        return result.returncode, result.stdout.strip()

    return run


def manifest(tree: Path) -> dict[str, str] | None:
    """Relative path -> sha256 of every file under ``tree``; ``None`` when unreadable."""
    files: dict[str, str] = {}
    try:
        for folder, _dirs, names in os.walk(tree, onerror=_raise):
            for name in names:
                path = Path(folder, name)
                files[path.relative_to(tree).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return files


def _raise(exc: OSError) -> None:
    raise exc


class Sandbox:
    """The repo and install dirs one rehearsal created; every step records one receipt."""

    def __init__(self, root: Path, git: Git, clock: Callable[[], datetime], running_repo: str) -> None:
        self.root = root
        self.repo = root / "repo"
        self.install = root / "install"
        self.repo.mkdir()
        self.install.mkdir()
        # Pinned at creation: a dir later swapped for a link to elsewhere no longer matches.
        self._pinned = {self.repo: _real(self.repo), self.install: _real(self.install)}
        self.git = git
        self.receipts: list[Receipt] = []
        self.previous: str | None = None
        self._clock = clock
        self._running = running_repo
        self._exports = 0

    def _note(self, step: str, status: Status, detail: str) -> Status:
        self.receipts.append(Receipt(step, status, detail[:80], self._clock().isoformat()))
        return status

    def _target(self, step: str, path: object, own: Path) -> Status | None:
        """``None`` when ``path`` is exactly ``own``; otherwise the refusal, recorded."""
        if not isinstance(path, (str, Path)):
            return self._note(step, "unknown", "target is not a path")
        real = _real(path)
        if _overlaps(real, self._running):
            return self._note(step, "refused", "target overlaps the repo JARVIS runs from")
        if real != self._pinned[own] or os.path.islink(own):
            return self._note(step, "refused", "target was not created by this rehearsal")
        return None

    def _sha(self, step: str, sha: object) -> Status | None:
        if type(sha) is str and _SHA_RE.fullmatch(sha):
            return None
        return self._note(step, "unknown", "sha is not 40 lowercase hex")

    def _head(self, ref: str) -> str | None:
        code, out = self.git(self.repo, "rev-parse", "--verify", "--end-of-options", ref)
        return out if code == 0 and _SHA_RE.fullmatch(out) else None

    def seed(self) -> tuple[str, str] | None:
        """Two commits: v1 on ``main``, v2 (one file changed, one added, one gone) on ``candidate``."""
        try:
            steps: list[tuple[str, ...] | dict[str, bytes]] = [
                ("init", "-q", "--template=", "-b", "main"), _V1, ("add", "-A"),
                ("commit", "-q", "-m", "v1"), ("checkout", "-q", "-b", "candidate"),
                _V2, ("add", "-A"), ("commit", "-q", "-m", "v2"), ("checkout", "-q", "main"),
            ]
            for item in steps:
                if isinstance(item, dict):
                    self._write_version(item)
                elif self.git(self.repo, *item)[0]:
                    return self._seed_failed(f"git {item[0]} failed")
            base, candidate = self._head(_MAIN), self._head("refs/heads/candidate")
        except (GitUnavailable, OSError) as exc:
            return self._seed_failed(str(exc) or type(exc).__name__)
        if base is None or candidate is None:
            return self._seed_failed("seeded refs are unreadable")
        self._note("seed", "pass", f"main {base[:12]} candidate {candidate[:12]}")
        return base, candidate

    def _write_version(self, files: dict[str, bytes]) -> None:
        for old in self.repo.iterdir():
            if old.is_file() and old.name not in files and self.git(self.repo, "rm", "-q", "--", old.name)[0]:
                raise OSError(f"git rm {old.name} failed")
        for name, data in files.items():
            (self.repo / name).parent.mkdir(exist_ok=True)
            (self.repo / name).write_bytes(data)

    def _seed_failed(self, why: str) -> None:
        self._note("seed", "unknown", why)

    def fast_forward(self, repo: object, sha: object) -> Status:
        """Move sandbox ``main`` to ``sha`` with ``merge --ff-only``; never a merge commit or reset."""
        step = "fast_forward"
        refusal = self._target(step, repo, self.repo) or self._sha(step, sha)
        if refusal is not None:
            return refusal
        assert type(sha) is str
        try:
            before = self._head(_MAIN)
            if before is None:
                return self._note(step, "unknown", "main is unreadable")
            code, out = self.git(self.repo, "symbolic-ref", "-q", "HEAD")
            if code or out != _MAIN:
                return self._note(step, "refused", "HEAD is not on main: not moved")
            code, out = self.git(self.repo, "status", "--porcelain", "--untracked-files=no")
            if code:
                return self._note(step, "unknown", "status failed")
            if out:
                return self._note(step, "refused", "dirty checkout: main not moved")
            if self._head(f"{sha}^{{commit}}") != sha:
                return self._note(step, "unknown", "candidate is not a commit in the sandbox")
            if sha == before:
                return self._note(step, "refused", "candidate equals main: nothing to move")
            code, _ = self.git(self.repo, "merge-base", "--is-ancestor", before, sha)
            if code == 1:
                return self._note(step, "refused", "candidate does not descend from main: not moved")
            if code:
                return self._note(step, "unknown", "ancestry check failed")
            if self.git(self.repo, "merge", "--ff-only", "-q", sha)[0]:
                return self._note(step, "refused", "merge --ff-only refused: main not moved")
            if self._head(_MAIN) != sha:
                return self._note(step, "unknown", "main is not at the candidate after merge")
        except GitUnavailable as exc:
            return self._note(step, "unknown", str(exc))
        self.previous = before
        return self._note(step, "pass", f"main {before[:12]} -> {sha[:12]}")

    def verify_tree(self, repo: object, sha: object) -> Status:
        """HEAD's tree is the candidate's tree and the work tree is clean."""
        step = "verify_tree"
        refusal = self._target(step, repo, self.repo) or self._sha(step, sha)
        if refusal is not None:
            return refusal
        try:
            have, want = self._head("HEAD^{tree}"), self._head(f"{sha}^{{tree}}")
            code, out = self.git(self.repo, "status", "--porcelain")
        except GitUnavailable as exc:
            return self._note(step, "unknown", str(exc))
        if have is None or want is None or code:
            return self._note(step, "unknown", "tree or status is unreadable")
        if have != want:
            return self._note(step, "refused", "HEAD tree is not the candidate tree")
        if out:
            return self._note(step, "refused", "work tree is not clean")
        return self._note(step, "pass", f"tree {want[:12]}")

    def deploy(self, install: object, sha: object) -> Status:
        """Export ``sha`` into ``install/app`` with a ``VERSION`` marker; the old app becomes ``previous``."""
        step = "deploy"
        refusal = self._target(step, install, self.install) or self._sha(step, sha)
        if refusal is not None:
            return refusal
        app, previous, marker = self.install / "app", self.install / "previous", self.install / "VERSION"
        if previous.exists():
            return self._note(step, "refused", "previous slot is taken (nothing is deleted)")
        incoming = self.install / f"incoming-{sha}"
        try:
            stage_git.export_commit(self.repo, str(sha), incoming)
            if app.exists():
                (self.install / "PREVIOUS_VERSION").write_text(marker.read_text(encoding="ascii"), encoding="ascii")
                app.rename(previous)
            incoming.rename(app)
            marker.write_text(f"{sha}\n", encoding="ascii")
        except (stage_git.StageGitError, ValueError, OSError) as exc:
            return self._note(step, "unknown", f"deploy failed: {type(exc).__name__}")
        return self._note(step, "pass", f"installed {str(sha)[:12]}")

    def rollback(self, install: object) -> Status:
        """Rename ``app`` to ``failed-<sha>`` and ``previous`` back to ``app``; nothing is deleted."""
        refusal = self._target("rollback", install, self.install)
        if refusal is not None:
            return refusal
        app, previous = self.install / "app", self.install / "previous"
        if not (app.is_dir() and previous.is_dir() and (self.install / "PREVIOUS_VERSION").is_file()):
            return self._note("rollback", "refused", "no previous version to restore")
        try:
            prior = (self.install / "PREVIOUS_VERSION").read_text(encoding="ascii").strip()
            current = (self.install / "VERSION").read_text(encoding="ascii").strip()
            if not (_SHA_RE.fullmatch(prior) and _SHA_RE.fullmatch(current)):
                return self._note("rollback", "unknown", "version marker is malformed")
            app.rename(self.install / f"failed-{current}")
            previous.rename(app)
            (self.install / "VERSION").write_text(f"{prior}\n", encoding="ascii")
        except (OSError, UnicodeError) as exc:
            return self._note("rollback", "unknown", f"rollback failed: {type(exc).__name__}")
        return self._note("rollback", "pass", f"restored {prior[:12]}")

    def verify_rollback(self, install: object, sha: object) -> Status:
        """``install/app`` is byte-equal to a fresh export of ``sha`` and ``VERSION`` names it."""
        step = "verify_rollback"
        refusal = self._target(step, install, self.install) or self._sha(step, sha)
        if refusal is not None:
            return refusal
        self._exports += 1
        check = self.root / f"check-{self._exports}"
        try:
            stage_git.export_commit(self.repo, str(sha), check)
            version = (self.install / "VERSION").read_text(encoding="ascii").strip()
        except (stage_git.StageGitError, ValueError, OSError, UnicodeError) as exc:
            return self._note(step, "unknown", f"reference unreadable: {type(exc).__name__}")
        want, have = manifest(check), manifest(self.install / "app")
        if want is None or have is None:
            return self._note(step, "unknown", "a tree is unreadable")
        if version != sha:
            return self._note(step, "refused", "VERSION is not the previous version")
        if have != want:
            return self._note(step, "refused", "restored tree is not byte-equal")
        return self._note(step, "pass", f"{len(want)} files byte-equal at {sha[:12]}")

    def result(self) -> Rehearsal:
        """``pass`` only when every recorded step passed; else the first non-pass status."""
        status = next((r.status for r in self.receipts if r.status != "pass"), "pass")
        return Rehearsal(status, tuple(self.receipts))


def open_sandbox(base: object, *, running_repo: Path = _RUNNING_REPO,
                 clock: Callable[[], datetime] = _utc,
                 git_factory: Callable[[Path], Git] = sandbox_git) -> Sandbox | Receipt:
    """A fresh sandbox under ``base``, or the refusal; nothing is created when refused."""
    def refuse(status: Status, why: str) -> Receipt:
        return Receipt("sandbox", status, why, clock().isoformat())

    if not isinstance(base, (str, Path)) or not str(base):
        return refuse("unknown", "base is not a path")
    running = _real(running_repo)
    if _overlaps(_real(base), running):
        return refuse("refused", "base overlaps the repo JARVIS runs from")
    if not os.path.isdir(base):
        return refuse("unknown", "base is not a directory")
    try:
        root = Path(tempfile.mkdtemp(prefix="n6-ffrehearse-", dir=os.path.realpath(base)))
        sandbox = Sandbox(root, git_factory(root), clock, running)
    except OSError as exc:
        return refuse("unknown", f"sandbox not created: {type(exc).__name__}")
    sandbox._note("sandbox", "pass", root.name)  # pyright: ignore[reportPrivateUsage]
    return sandbox


def rehearse(base: object, *, running_repo: Path = _RUNNING_REPO,
             clock: Callable[[], datetime] = _utc,
             git_factory: Callable[[Path], Git] = sandbox_git) -> Rehearsal:
    """Seed, deploy v1, fast-forward to v2, verify, deploy v2, roll back, verify byte-equal.

    The sandbox is left under ``base`` (this module never deletes); the caller
    owns ``base`` and its cleanup.
    """
    opened = open_sandbox(base, running_repo=running_repo, clock=clock, git_factory=git_factory)
    if isinstance(opened, Receipt):
        return Rehearsal(opened.status, (opened,))
    seeded = opened.seed()
    if seeded is not None:
        v1, v2 = seeded
        steps: list[Callable[[], Status]] = [
            lambda: opened.deploy(opened.install, v1),
            lambda: opened.fast_forward(opened.repo, v2),
            lambda: opened.verify_tree(opened.repo, v2),
            lambda: opened.deploy(opened.install, v2),
            lambda: opened.rollback(opened.install),
            lambda: opened.verify_rollback(opened.install, v1),
        ]
        for step in steps:
            if step() != "pass":
                break
    return opened.result()
