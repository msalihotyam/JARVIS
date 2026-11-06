# pyright: strict
"""N6-01: the N6 composition root, read-only sight plus a free-text notes sink.

``compose`` wires the PM26A modules through ``work_ports.build_ports``. ``start`` is the one
N6 entry point; the only mode is ``"sight"``: every port outside ``SIGHT_PORTS`` raises, so N6
acts on nothing and writes nothing but its own notes file. The app never imports this module
(Kit Phase 1 shadow); core policy, protected paths and the two-key main push are untouched.
"""

from __future__ import annotations

import json
import os
import types
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from jarvis import stage_git
from jarvis.memory import SQLiteMemoryStore
from jarvis.memory_import import ProtectedPathError, is_protected_path
from jarvis.memory_import import _final_path  # pyright: ignore[reportPrivateUsage]
from jarvis.work_ports import build_ports

# An allowlist, so a port added to build_ports later is refused until it is judged read-only
# by EFFECT: "applied" is out because work_ports._ref runs update-ref when the ref is absent.
SIGHT_PORTS = frozenset({"clock", "apply_intent", "tree_hash"})
# The read-only part of stage_git.ALLOWED (update-ref, worktree, pack/index-pack are out).
SIGHT_GIT = frozenset({"rev-parse", "cat-file", "ls-tree", "diff-tree", "for-each-ref",
                       "rev-list", "merge-base"})
# diff-tree may name changes, never print content (-p, --stat, --textconv ... are refused).
_DIFF_TREE_OPTIONS = frozenset({"-r", "-t", "-z", "--root", "--raw", "--name-only",
                                "--name-status", "--no-renames", "--no-commit-id", "--"})
# cat-file only as "cat-file <mode> <object>"; typed modes are out because they peel a tag.
_CAT_FILE_MODES = frozenset({"-t", "-s", "-e", "-p"})


def compose(**wiring: Any) -> types.SimpleNamespace:
    """The full PM26A ports for one run (the write path; N6 itself only ever holds ``sight``)."""
    return build_ports(**wiring)


def _refused(name: str) -> Callable[..., NoReturn]:
    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise PermissionError(f"N6 sight mode is read-only: {name}")
    return refuse


def sight(ports: types.SimpleNamespace) -> types.SimpleNamespace:
    """``ports`` with every port outside ``SIGHT_PORTS`` replaced by one that raises."""
    wired: dict[str, Any] = vars(ports)
    return types.SimpleNamespace(**{name: port if name in SIGHT_PORTS else _refused(name)
                                    for name, port in wired.items()})


def _guard(path: str | os.PathLike[str]) -> Path:
    resolved = Path(path).resolve()
    if is_protected_path(path) or is_protected_path(resolved):
        raise PermissionError(f"N6 sight refuses a protected path: {path}")
    return resolved


def _check_operands(repo: Path, args: tuple[str, ...]) -> None:
    """Fail closed on any operand that names, or could peel to, a protected path's content."""
    for arg in args:
        pieces = arg.split(":")[1:]
        # A rev suffix after a path (x.pem^{blob}) hides its name; a %(raw) format prints a blob.
        if "%(" in arg or any(c in piece for piece in pieces for c in "^~{}@"):
            raise PermissionError(f"N6 sight git refuses this operand: {arg}")
        if any(is_protected_path(repo / piece) for piece in (pieces or [arg])):
            raise PermissionError(f"N6 sight git refuses a protected object path: {arg}")
    if args[0] == "diff-tree" and any(a.startswith("-") and a not in _DIFF_TREE_OPTIONS
                                      for a in args[1:]):
        raise PermissionError(f"N6 sight diff-tree prints no content: {args}")
    if args[0] == "cat-file" and (len(args) != 3 or args[1] not in _CAT_FILE_MODES
                                  or args[2].startswith("-")):
        raise PermissionError(f"N6 sight cat-file is only 'cat-file <mode> <object>': {args}")


def _protected_blob(repo: Path, oid: bytes) -> bool:
    """True when HEAD or any ref tip lists ``oid`` under a protected name."""
    # ponytail: ref tips only, not history; walk rev-list --all if a protected blob kept only
    # in an old commit and re-hung under a safe name must also be refused.
    tips = [b"HEAD"] + [line.split()[0] for line in stage_git.run(repo, "for-each-ref").splitlines()]
    for tip in tips:
        try:
            listing = stage_git.run(repo, "ls-tree", "-r", "-z", tip.decode() + "^{tree}")
        except Exception:  # a tip that is not tree-ish (a tag on a blob) lists nothing
            continue
        for entry in listing.split(b"\0"):
            meta, _, name = entry.partition(b"\t")
            if meta.split()[2:3] == [oid] and is_protected_path(repo / name.decode()):
                return True
    return False


class N6:
    """N6 in sight mode: read-only ports, files, reports, events, git and CI files, plus notes."""

    def __init__(self, ports: types.SimpleNamespace, *, store: SQLiteMemoryStore,
                 notes: Path, clock: Callable[[], datetime]) -> None:
        self.ports = ports
        self._store = store
        self._notes = notes
        self._clock = clock

    def read(self, path: str | os.PathLike[str]) -> str:
        """A file, report or local CI result; protected paths are refused."""
        return _guard(path).read_text(encoding="utf-8")

    def events(self, run_id: str) -> tuple[object, ...]:
        return tuple(self._store.work_events(run_id))

    def git(self, repo: str | os.PathLike[str], *args: str) -> bytes:
        """One read-only plumbing command; stage_git.run still applies its own option checks."""
        if not args or args[0] not in SIGHT_GIT:
            raise PermissionError(f"N6 sight git is read-only: {args[:1]}")
        root = _guard(repo)
        _check_operands(root, args)
        if args[0] != "cat-file":
            return stage_git.run(root, *args)
        # Pin the object once, so the checked name and the read object are the same; a blob is
        # read (any mode) only through a checked <rev>:<path>, never by a bare oid or ref name,
        # and never when a ref tree holds the same blob under a protected name (notes, copies).
        oid = stage_git.run(root, "rev-parse", "--verify", args[2]).decode().strip()
        if stage_git.run(root, "cat-file", "-t", oid).strip() == b"blob" and (
                ":" not in args[2] or _protected_blob(root, oid.encode())):
            raise PermissionError(f"N6 sight refuses this blob: {args[2]}")
        return stage_git.run(root, "cat-file", args[1], oid)

    def note(self, text: str) -> None:
        """Append one free-text note (JSON line, injected clock) to N6's own notes file."""
        if not text.strip():
            raise ValueError("a note needs text")
        notes = _guard(self._notes)
        if notes != self._notes:
            raise PermissionError(f"N6 notes path moved: {notes}")
        notes.parent.mkdir(parents=True, exist_ok=True)
        with notes.open("a", encoding="utf-8") as sink:
            # The check above is on the name; a junction swapped in before the open shows only
            # on the OPENED handle (memory_import's post-open check), so check it, then write.
            try:
                final = _final_path(sink.fileno(), notes)
            except ProtectedPathError as exc:
                raise PermissionError("N6 cannot verify the opened notes path") from exc
            if (os.path.normcase(final) != os.path.normcase(str(notes))
                    or is_protected_path(final) or os.fstat(sink.fileno()).st_nlink > 1):
                raise PermissionError(f"N6 notes path changed while it was opened: {final}")
            sink.write(json.dumps({"at": self._clock().isoformat(), "text": text}) + "\n")


def start(*, notes: str | os.PathLike[str], mode: str = "sight", **wiring: Any) -> N6:
    """The N6 entry point. There is no write path: any mode but ``"sight"`` is refused."""
    if mode != "sight":
        raise PermissionError("N6 has no write path; the only mode is sight")
    return N6(sight(compose(**wiring)), store=wiring["store"], notes=_guard(notes),
              clock=wiring["clock"])
