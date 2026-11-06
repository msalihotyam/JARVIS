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
from jarvis.memory_import import is_protected_path
from jarvis.work_ports import build_ports

# An allowlist, so a port added to build_ports later is refused until it is judged read-only.
SIGHT_PORTS = frozenset({"clock", "applied", "apply_intent", "tree_hash"})
# The read-only part of stage_git.ALLOWED (update-ref, worktree, pack/index-pack are out).
SIGHT_GIT = frozenset({"rev-parse", "cat-file", "ls-tree", "diff-tree", "for-each-ref",
                       "rev-list", "merge-base"})


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
        return stage_git.run(_guard(repo), *args)

    def note(self, text: str) -> None:
        """Append one free-text note (JSON line, injected clock) to N6's own notes file."""
        if not text.strip():
            raise ValueError("a note needs text")
        notes = _guard(self._notes)
        notes.parent.mkdir(parents=True, exist_ok=True)
        with notes.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps({"at": self._clock().isoformat(), "text": text}) + "\n")


def start(*, notes: str | os.PathLike[str], mode: str = "sight", **wiring: Any) -> N6:
    """The N6 entry point. There is no write path: any mode but ``"sight"`` is refused."""
    if mode != "sight":
        raise PermissionError("N6 has no write path; the only mode is sight")
    _guard(notes)
    return N6(sight(compose(**wiring)), store=wiring["store"], notes=Path(notes),
              clock=wiring["clock"])
