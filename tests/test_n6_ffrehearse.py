"""N6G25REHEARSE: G25 fast-forward, deploy and previous-version rollback rehearsal (R20).

Every test runs inside its own TemporaryDirectory base; the rehearsal never
touches this repo (the running repo is refused as a target) and the clock is
frozen.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from jarvis import n6_ffrehearse as m

_AT = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
_STEPS = ["sandbox", "seed", "deploy", "fast_forward", "verify_tree", "deploy", "rollback",
          "verify_rollback"]


def _clock() -> datetime:
    return _AT


class _Recorder:
    """Real sandbox git that records every call."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.inner = m.sandbox_git(root)
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def __call__(self, cwd: Path, *args: str) -> tuple[int, str]:
        self.calls.append((cwd, args))
        return self.inner(cwd, *args)


def _junction(target: Path, link: Path) -> None:
    if os.name == "nt":
        import _winapi  # junction: no symlink privilege needed

        _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
    else:
        os.symlink(target, link)


def _no_git(_root: Path) -> m.Git:
    def run(cwd: Path, *args: str) -> tuple[int, str]:
        raise m.GitUnavailable("trusted git is unavailable")
    return run


def _failing_git(_root: Path) -> m.Git:
    return lambda cwd, *args: (128, "")


class FfRehearseTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))

    def _sandbox(self, **kw: object) -> tuple[m.Sandbox, str, str]:
        box = m.open_sandbox(self.base, clock=_clock, **kw)  # type: ignore[arg-type]
        assert isinstance(box, m.Sandbox), box
        seeded = box.seed()
        assert seeded is not None, box.receipts
        return box, *seeded

    def _main(self, box: m.Sandbox) -> str | None:
        return box._head("refs/heads/main")  # pyright: ignore[reportPrivateUsage]

    def _last(self, box: m.Sandbox) -> m.Receipt:
        return box.receipts[-1]

    # -- the whole rehearsal --------------------------------------------------

    def test_full_rehearsal_passes_with_one_receipt_per_step(self) -> None:
        result = m.rehearse(self.base, clock=_clock)
        self.assertEqual("pass", result.status, result.receipts)
        self.assertEqual(_STEPS, [r.step for r in result.receipts])
        self.assertTrue(all(r.status == "pass" for r in result.receipts))
        self.assertTrue(all(r.at == _AT.isoformat() for r in result.receipts))
        self.assertTrue(all(len(r.detail) <= 80 for r in result.receipts))
        (root,) = list(self.base.iterdir())
        self.assertTrue(root.name.startswith("n6-ffrehearse-"))
        install = root / "install"
        self.assertEqual(b"one\n", (install / "app" / "a.txt").read_bytes())
        self.assertEqual(b"same\r\n", (install / "app" / "keep.txt").read_bytes())
        self.assertTrue((install / "app" / "gone.txt").is_file())
        self.assertFalse((install / "app" / "sub").exists())
        failed = [p.name for p in install.iterdir() if p.name.startswith("failed-")]
        self.assertEqual(1, len(failed))  # the rolled-back version is kept, never deleted

    def test_rehearsal_stops_at_first_non_pass_when_git_is_unavailable(self) -> None:
        result = m.rehearse(self.base, clock=_clock, git_factory=_no_git)
        self.assertEqual("unknown", result.status)
        self.assertEqual(["sandbox", "seed"], [r.step for r in result.receipts])

    def test_git_nonzero_exit_is_unknown_not_a_guess(self) -> None:
        result = m.rehearse(self.base, clock=_clock, git_factory=_failing_git)
        self.assertEqual("unknown", result.status)
        self.assertEqual("seed", result.receipts[-1].step)

    # -- fast-forward ---------------------------------------------------------

    def test_fast_forward_moves_main_and_tree_verifies(self) -> None:
        box, v1, v2 = self._sandbox()
        self.assertEqual("pass", box.fast_forward(box.repo, v2))
        self.assertEqual(v2, self._main(box))
        self.assertEqual(v1, box.previous)
        self.assertEqual("pass", box.verify_tree(box.repo, v2))
        self.assertEqual("refused", box.verify_tree(box.repo, v1))

    def test_non_descendant_candidate_is_refused_and_main_not_moved(self) -> None:
        box, _v1, v2 = self._sandbox()
        (box.repo / "a.txt").write_bytes(b"diverged\n")
        self.assertEqual(0, box.git(box.repo, "commit", "-q", "-am", "diverge")[0])
        main = self._main(box)
        self.assertEqual("refused", box.fast_forward(box.repo, v2))
        self.assertIn("does not descend", self._last(box).detail)
        self.assertEqual(main, self._main(box))
        self.assertEqual("0", box.git(box.repo, "rev-list", "--merges", "--all", "--count")[1])

    def test_dirty_checkout_is_refused_and_main_not_moved(self) -> None:
        box, v1, v2 = self._sandbox()
        (box.repo / "a.txt").write_bytes(b"dirty\n")
        self.assertEqual("refused", box.fast_forward(box.repo, v2))
        self.assertIn("dirty", self._last(box).detail)
        self.assertEqual(v1, self._main(box))
        self.assertEqual(b"dirty\n", (box.repo / "a.txt").read_bytes())

    def test_head_off_main_is_refused(self) -> None:
        box, v1, v2 = self._sandbox()
        self.assertEqual(0, box.git(box.repo, "checkout", "-q", "candidate")[0])
        self.assertEqual("refused", box.fast_forward(box.repo, v2))
        self.assertEqual(v1, self._main(box))

    def test_candidate_equal_to_main_is_refused(self) -> None:
        box, v1, _v2 = self._sandbox()
        self.assertEqual("refused", box.fast_forward(box.repo, v1))
        self.assertIn("equals main", self._last(box).detail)

    def test_unknown_or_malformed_sha_is_unknown(self) -> None:
        box, v1, v2 = self._sandbox()
        for bad in (v2.upper(), v2[:12], "", None, 42, "f" * 40):
            for step in (box.fast_forward, box.verify_tree):
                self.assertEqual("unknown", step(box.repo, bad), bad)
            for step in (box.deploy, box.verify_rollback):
                self.assertEqual("unknown", step(box.install, bad), bad)
        self.assertEqual(v1, self._main(box))
        self.assertFalse((box.install / "app").exists())
        self.assertFalse((box.install / "VERSION").exists())

    # -- paths: only what this rehearsal created ------------------------------

    def test_paths_not_created_by_the_rehearsal_are_refused(self) -> None:
        box, v1, v2 = self._sandbox()
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        foreign = (other.name, box.repo / "..", box.root, box.install / "app", str(box.repo) + "x")
        for path in foreign:
            self.assertEqual("refused", box.fast_forward(path, v2), path)
            self.assertEqual("refused", box.verify_tree(path, v2), path)
            self.assertEqual("refused", box.deploy(path, v1), path)
            self.assertEqual("refused", box.rollback(path), path)
            self.assertEqual("refused", box.verify_rollback(path, v1), path)
            self.assertIn("not created", self._last(box).detail)
        self.assertEqual("refused", box.fast_forward(box.install, v2))  # repo step, install dir
        self.assertEqual("refused", box.deploy(box.repo, v1))  # install step, repo dir
        self.assertEqual("unknown", box.deploy(42, v1))
        self.assertEqual(v1, self._main(box))
        self.assertEqual([], os.listdir(other.name))
        self.assertEqual([], os.listdir(box.install))

    def test_own_dir_swapped_for_a_link_elsewhere_is_refused(self) -> None:
        box, v1, _v2 = self._sandbox()
        foreign = self.base / "foreign"
        foreign.mkdir()
        box.install.rename(box.root / "install-moved")
        if os.name == "nt":
            import _winapi  # junction: no symlink privilege needed

            _winapi.CreateJunction(str(foreign), str(box.install))  # type: ignore[attr-defined]
        else:
            os.symlink(foreign, box.install)
        self.assertEqual("refused", box.deploy(box.install, v1))
        self.assertIn("not created", self._last(box).detail)
        self.assertEqual("refused", box.rollback(box.install))
        self.assertEqual([], os.listdir(foreign))

    def test_the_running_repo_is_never_a_target(self) -> None:
        box, v1, v2 = self._sandbox()
        here = m._RUNNING_REPO  # pyright: ignore[reportPrivateUsage]
        for path in (here, here / "jarvis", here.parent):
            self.assertEqual("refused", box.fast_forward(path, v2))
            self.assertIn("runs from", self._last(box).detail)
            self.assertEqual("refused", box.deploy(path, v1))
            self.assertEqual("refused", box.rollback(path))
        self.assertEqual(v1, self._main(box))

    def test_a_sandbox_pointed_at_its_running_repo_refuses_its_own_dirs(self) -> None:
        root = self.base / "r"
        root.mkdir()
        box = m.Sandbox(root, m.sandbox_git(root), _clock, m._real(root))  # pyright: ignore[reportPrivateUsage]
        seeded = box.seed()
        assert seeded is not None, box.receipts
        v1, v2 = seeded
        self.assertEqual("refused", box.fast_forward(box.repo, v2))
        self.assertIn("runs from", self._last(box).detail)
        self.assertEqual("refused", box.deploy(box.install, v1))
        self.assertEqual(v1, self._main(box))

    def test_base_overlapping_the_running_repo_creates_nothing(self) -> None:
        made: list[Path] = []

        def factory(root: Path) -> m.Git:
            made.append(root)
            return _Recorder(root)

        # The default is the real checkout; a temp stand-in keeps a regression from writing into it.
        self.assertTrue((m._RUNNING_REPO / "jarvis" / "n6_ffrehearse.py").is_file())  # pyright: ignore[reportPrivateUsage]
        here = self.base / "running"
        (here / "build").mkdir(parents=True)
        for base in (here, here / "build", self.base):
            result = m.rehearse(base, running_repo=here, clock=_clock, git_factory=factory)
            self.assertEqual("refused", result.status)
            self.assertEqual(["sandbox"], [r.step for r in result.receipts])
        self.assertEqual([], made)
        self.assertEqual(["build"], os.listdir(here))
        self.assertEqual(["running"], os.listdir(self.base))
        inner = self.base / "inner"
        inner.mkdir()
        refused = m.open_sandbox(inner, running_repo=self.base, clock=_clock, git_factory=factory)
        self.assertIsInstance(refused, m.Receipt)
        self.assertEqual([], os.listdir(inner))
        self.assertEqual([], made)

    def test_malformed_or_missing_base_is_unknown(self) -> None:
        for base in (None, 42, "", b"bytes", self.base / "missing"):
            result = m.rehearse(base, clock=_clock)
            self.assertEqual("unknown", result.status, base)
            self.assertEqual(1, len(result.receipts))
        self.assertEqual([], os.listdir(self.base))

    def test_sandbox_git_only_sees_the_sandbox(self) -> None:
        recorder: list[_Recorder] = []

        def factory(root: Path) -> m.Git:
            recorder.append(_Recorder(root))
            return recorder[0]

        result = m.rehearse(self.base, clock=_clock, git_factory=factory)
        self.assertEqual("pass", result.status, result.receipts)
        rec = recorder[0]
        self.assertTrue(rec.calls)
        root = m._real(rec.root)  # pyright: ignore[reportPrivateUsage]
        for cwd, args in rec.calls:
            self.assertTrue(m._inside(m._real(cwd), root), (cwd, args))  # pyright: ignore[reportPrivateUsage]
        verbs = {args[0] for _cwd, args in rec.calls}
        self.assertFalse(verbs & {"push", "fetch", "pull", "reset", "clean", "stash", "remote"})

    def test_ff_failure_after_checks_passed_is_unknown(self) -> None:
        def factory(root: Path) -> m.Git:
            inner = m.sandbox_git(root)
            return lambda cwd, *args: (1, "") if args[0] == "merge" else inner(cwd, *args)

        box, v1, v2 = self._sandbox(git_factory=factory)
        self.assertEqual("unknown", box.fast_forward(box.repo, v2))
        self.assertIn("after checks passed", self._last(box).detail)
        self.assertEqual(v1, self._main(box))
        self.assertIsNone(box.previous)

    # -- links inside the sandbox are never followed (F1) ---------------------

    def _plant(self, link: Path, as_dir: bool) -> tuple[Path, object]:
        """Swap ``link`` (any existing entry moved aside) for a link to a fresh foreign target."""
        if os.path.lexists(link):
            link.rename(link.with_name(link.name + "-aside"))
        foreign = Path(tempfile.mkdtemp(dir=self.base))
        if as_dir:
            _junction(foreign, link)
            return foreign, []
        target = foreign / "foreign.txt"
        target.write_bytes(b"foreign\n")
        os.link(target, link)
        return target, b"foreign\n"

    def _untouched(self, target: Path, before: object) -> None:
        self.assertEqual(before, sorted(os.listdir(target)) if target.is_dir() else target.read_bytes())

    def test_seed_never_follows_a_link(self) -> None:
        box = m.open_sandbox(self.base, clock=_clock)
        assert isinstance(box, m.Sandbox)
        box.repo.rename(box.root / "repo-moved")
        target, before = self._plant(box.repo, as_dir=True)  # the repo dir itself
        self.assertIsNone(box.seed())
        self.assertEqual("refused", self._last(box).status)
        self._untouched(target, before)
        box = m.open_sandbox(self.base, clock=_clock)
        assert isinstance(box, m.Sandbox)
        target, before = self._plant(box.repo / "a.txt", as_dir=False)  # hard link to a foreign file
        self.assertIsNone(box.seed())
        self.assertEqual("refused", self._last(box).status)
        self._untouched(target, before)
        target, before = self._plant(box.repo / "sub", as_dir=True)  # link in an intermediate dir
        with self.assertRaises(m.LinkRefused):
            # a.txt kept in the dict: the planted untracked file is not git rm'd first
            box._write_version({"sub/new.bin": b"x", "a.txt": b"x"})  # pyright: ignore[reportPrivateUsage]
        self._untouched(target, before)

    def test_deploy_rollback_and_verify_never_follow_a_link(self) -> None:
        cases = [("deploy", "VERSION", False), ("deploy", "PREVIOUS_VERSION", False),
                 ("deploy", "incoming", True), ("deploy", "app", True), ("deploy", "previous", True),
                 ("rollback", "VERSION", False), ("rollback", "PREVIOUS_VERSION", False),
                 ("rollback", "app", True), ("rollback", "previous", True),
                 ("verify_rollback", "VERSION", False), ("verify_rollback", "app", True)]
        for step, name, as_dir in cases:
            with self.subTest(step=step, name=name):
                box, v1, v2 = self._sandbox()
                self.assertEqual("pass", box.deploy(box.install, v1))
                self.assertEqual("pass", box.fast_forward(box.repo, v2))
                if step != "deploy":
                    self.assertEqual("pass", box.deploy(box.install, v2))
                if step == "verify_rollback":
                    self.assertEqual("pass", box.rollback(box.install))
                child = f"incoming-{v2}" if name == "incoming" else name
                target, before = self._plant(box.install / child, as_dir)
                calls = {"deploy": lambda: box.deploy(box.install, v2),
                         "rollback": lambda: box.rollback(box.install),
                         "verify_rollback": lambda: box.verify_rollback(box.install, v1)}
                self.assertEqual("refused", calls[step]())
                self.assertIn("link", self._last(box).detail)
                self._untouched(target, before)
                self.assertFalse(any(p.name.startswith("failed-") for p in box.install.iterdir())
                                 and step == "rollback")

    def test_manifest_never_follows_a_link(self) -> None:
        tree = self.base / "tree"
        (tree / "d").mkdir(parents=True)
        (tree / "d" / "f").write_bytes(b"x")
        plain = m.manifest(tree)
        self.assertEqual(["d/f"], list(plain or {}))
        os.link(tree / "d" / "f", self.base / "twin")  # hard link
        self.assertIsNone(m.manifest(tree))
        os.unlink(self.base / "twin")
        self.assertEqual(plain, m.manifest(tree))
        foreign = Path(tempfile.mkdtemp(dir=self.base))
        _junction(foreign, tree / "d" / "j")  # dir link inside
        self.assertIsNone(m.manifest(tree))
        os.rmdir(tree / "d" / "j")
        _junction(tree, self.base / "root-link")  # the tree root is a link
        self.assertIsNone(m.manifest(self.base / "root-link"))
        try:  # a file symlink needs privilege on Windows: checked only where it can be made
            os.symlink(foreign / "x", tree / "s")
        except OSError:
            return
        self.assertIsNone(m.manifest(tree))

    # -- deploy, rollback, byte-equal -----------------------------------------

    def _deployed_twice(self) -> tuple[m.Sandbox, str, str]:
        box, v1, v2 = self._sandbox()
        self.assertEqual("pass", box.deploy(box.install, v1))
        self.assertEqual("pass", box.fast_forward(box.repo, v2))
        self.assertEqual("pass", box.deploy(box.install, v2))
        return box, v1, v2

    def test_deploy_writes_version_and_keeps_previous_runnable(self) -> None:
        box, v1, v2 = self._deployed_twice()
        self.assertEqual(f"{v2}\n", (box.install / "VERSION").read_text(encoding="ascii"))
        self.assertEqual(f"{v1}\n", (box.install / "PREVIOUS_VERSION").read_text(encoding="ascii"))
        self.assertEqual(b"\x00\xff\n", (box.install / "app" / "sub" / "new.bin").read_bytes())
        self.assertEqual(b"one\n", (box.install / "previous" / "a.txt").read_bytes())
        self.assertEqual("refused", box.deploy(box.install, v2))  # previous slot taken
        self.assertIn("nothing is deleted", self._last(box).detail)
        self.assertEqual(b"one\n", (box.install / "previous" / "a.txt").read_bytes())

    def test_rollback_restores_previous_byte_equal(self) -> None:
        box, v1, v2 = self._deployed_twice()
        self.assertEqual("pass", box.rollback(box.install))
        self.assertEqual(f"{v1}\n", (box.install / "VERSION").read_text(encoding="ascii"))
        self.assertTrue((box.install / f"failed-{v2}" / "sub" / "new.bin").is_file())
        self.assertEqual("pass", box.verify_rollback(box.install, v1))
        self.assertIn("3 files byte-equal", self._last(box).detail)

    def test_tampered_restore_is_refused(self) -> None:
        box, v1, _v2 = self._deployed_twice()
        self.assertEqual("pass", box.rollback(box.install))
        (box.install / "app" / "a.txt").write_bytes(b"onE\n")
        self.assertEqual("refused", box.verify_rollback(box.install, v1))
        self.assertIn("not byte-equal", self._last(box).detail)
        (box.install / "app" / "a.txt").write_bytes(b"one\n")
        (box.install / "app" / "extra.txt").write_bytes(b"")
        self.assertEqual("refused", box.verify_rollback(box.install, v1))

    def test_version_marker_mismatch_is_refused(self) -> None:
        box, v1, v2 = self._deployed_twice()
        self.assertEqual("refused", box.verify_rollback(box.install, v1))  # still on v2
        self.assertIn("VERSION", self._last(box).detail)
        self.assertEqual("pass", box.rollback(box.install))
        self.assertEqual("refused", box.verify_rollback(box.install, v2))

    def test_rollback_without_previous_is_refused(self) -> None:
        box, v1, _v2 = self._sandbox()
        self.assertEqual("refused", box.rollback(box.install))
        self.assertEqual("pass", box.deploy(box.install, v1))
        self.assertEqual("refused", box.rollback(box.install))
        self.assertIn("no previous version", self._last(box).detail)
        self.assertEqual(f"{v1}\n", (box.install / "VERSION").read_text(encoding="ascii"))

    def test_malformed_version_marker_is_unknown(self) -> None:
        box, _v1, _v2 = self._deployed_twice()
        (box.install / "PREVIOUS_VERSION").write_text("not-a-sha\n", encoding="ascii")
        self.assertEqual("unknown", box.rollback(box.install))
        self.assertTrue((box.install / "previous").is_dir())
        self.assertFalse(any(p.name.startswith("failed-") for p in box.install.iterdir()))

    def test_result_is_the_first_non_pass(self) -> None:
        box, _v1, _v2 = self._sandbox()
        box.rollback(box.install)
        box.verify_rollback(box.install, "x")
        self.assertEqual("refused", box.result().status)
        self.assertEqual(["sandbox", "seed", "rollback", "verify_rollback"],
                         [r.step for r in box.result().receipts])


if __name__ == "__main__":
    unittest.main()
