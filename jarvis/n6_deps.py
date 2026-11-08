# pyright: strict
"""N6-14 (B4): the cross-run backlog and the INT push wait.

One JSON file whose path the caller passes in holds the backlog order and every
slice's dependency state, so both survive a restart. A dependent of an INT
starts only after that INT's push is recorded AND the injected ``verify`` port
says the remote main contains the INT's candidate SHA; the dependent's base is
that verified main SHA (R04). Anything unknown holds: a missing or blocked
dependency, a finished INT not yet pushed (the INT116 flaw), a push the remote
does not show, a verifier that raises or answers anything but a full SHA, and a
corrupt or unreadable state file (hold everything plus attention, never "no
deps"). Writes are temp file + ``os.replace``. No clock, no git, no network.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

VERSION = 1
_SHA = re.compile(r"[0-9a-f]{40}")
_STATES = frozenset({"pending", "started", "done", "blocked"})
_KEYS = frozenset({"id", "deps", "int", "state", "sha", "pushed"})

#: verify(candidate_sha) -> the remote main SHA that contains it, or None when it
#: does not. A raise means the remote is unreadable; both hold the dependent.
Verify = Callable[[str], object]


class Unreadable(ValueError):
    """The state file is corrupt or unreadable: everything holds."""


@dataclass(frozen=True)
class Item:
    slice_id: str
    depends_on: tuple[str, ...] = ()
    is_int: bool = False


@dataclass(frozen=True)
class Pick:
    start: tuple[tuple[str, str | None], ...]  # (slice id, base SHA or None), backlog order
    held: tuple[tuple[str, str], ...]  # (slice id, reason)
    attention: tuple[str, ...]
    held_all: bool = False


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _entry(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    e = cast("dict[str, object]", raw)
    # a list "state" is unhashable: check str first so pick() never crashes
    if frozenset(e) != _KEYS or not isinstance(e["state"], str) or e["state"] not in _STATES:
        return None
    sid, deps, sha = e["id"], e["deps"], e["sha"]
    if not isinstance(sid, str) or not sid.strip() or not isinstance(deps, list):
        return None
    if not all(isinstance(d, str) and d.strip() for d in cast("list[object]", deps)):
        return None
    if type(e["int"]) is not bool or type(e["pushed"]) is not bool:
        return None
    if sha is not None and not _is_sha(sha):
        return None
    if e["int"] and e["state"] == "done" and sha is None:
        return None
    if e["pushed"] and (not e["int"] or e["state"] != "done"):
        return None
    return e


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # json keeps the last duplicate: "deps":["INT1"],"deps":[] would drop a dependency
    out = dict(pairs)
    if len(out) != len(pairs):
        raise Unreadable("state file has a duplicate key")
    return out


def _read(path: Path) -> list[dict[str, object]]:
    """The backlog; [] when there is no file. Raises Unreadable when it cannot be trusted."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise Unreadable(f"state file unreadable ({type(exc).__name__})") from exc
    try:
        data = cast(object, json.loads(raw, object_pairs_hook=_no_duplicate_keys))
    except Unreadable:
        raise
    except (ValueError, RecursionError) as exc:  # RecursionError: absurdly deep nesting
        raise Unreadable("state file is not JSON") from exc
    if not isinstance(data, dict):
        raise Unreadable("state file shape")
    top = cast("dict[str, object]", data)
    version, backlog = top.get("version"), top.get("backlog")
    if type(version) is not int or version != VERSION or not isinstance(backlog, list):
        raise Unreadable("state file shape")
    out: list[dict[str, object]] = []
    for raw_entry in cast("list[object]", backlog):
        e = _entry(raw_entry)
        if e is None or any(o["id"] == e["id"] for o in out):
            raise Unreadable("state file entry shape")
        out.append(e)
    return out


def _write(path: Path, entries: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "backlog": entries}, f)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _find(entries: list[dict[str, object]], slice_id: str) -> dict[str, object]:
    for e in entries:
        if e["id"] == slice_id:
            return e
    raise ValueError(f"unknown slice {slice_id!r}")


# ponytail: read-modify-write without a file lock; the single conveyor process
# (N6-08 lease holder) is the only writer. Add the lease's _locked if that changes.
def enqueue(path: Path | str, items: Iterable[Item]) -> None:
    """Append items to the backlog in order, in one atomic write.

    Raises ValueError on a malformed item, a duplicate id or a self-dependency
    (nothing is written) and Unreadable when the file cannot be trusted."""
    path = Path(path)
    entries = _read(path)
    ids = {e["id"] for e in entries}
    for item in items:
        sid = cast(object, item.slice_id)  # Item is unchecked caller input
        deps = cast(object, item.depends_on)
        if not isinstance(sid, str) or not sid.strip() or sid in ids:
            raise ValueError(f"bad or duplicate slice id {sid!r}")
        if not isinstance(deps, tuple) or sid in deps or not all(
                isinstance(d, str) and d.strip() for d in cast("tuple[object, ...]", deps)):
            raise ValueError(f"bad depends_on for {sid!r}")
        if type(cast(object, item.is_int)) is not bool:
            raise ValueError(f"bad is_int for {sid!r}")
        ids.add(sid)
        entries.append({"id": sid, "deps": list(cast("tuple[str, ...]", deps)),
                        "int": item.is_int, "state": "pending", "sha": None, "pushed": False})
    _write(path, entries)


def record(path: Path | str, slice_id: str, state: str, candidate_sha: str | None = None) -> None:
    """A finished slice: "done" (an INT needs its 40-hex candidate SHA) or "blocked".

    Done and blocked are final: a BLOCKED slice never turns done (R04)."""
    if state not in ("done", "blocked"):
        raise ValueError(f"bad state {state!r}")
    if candidate_sha is not None and not _is_sha(candidate_sha):
        raise ValueError("candidate SHA is not 40 lowercase hex")
    path = Path(path)
    entries = _read(path)
    e = _find(entries, slice_id)
    if e["state"] in ("done", "blocked"):
        raise ValueError(f"{slice_id} is already {e['state']}")
    if state == "done" and e["int"] and candidate_sha is None:
        raise ValueError(f"INT {slice_id} done without a candidate SHA")
    e["state"], e["sha"] = state, candidate_sha
    _write(path, entries)


def record_push(path: Path | str, slice_id: str) -> None:
    """The push of a done INT was reported; dependents still wait for ``verify``."""
    path = Path(path)
    entries = _read(path)
    e = _find(entries, slice_id)
    if not e["int"] or e["state"] != "done":
        raise ValueError(f"{slice_id} is not a done INT")
    e["pushed"] = True
    _write(path, entries)


def mark_started(path: Path | str, slice_id: str, verify: Verify) -> bool:
    """pending -> started only when pick() would start it now (N6-14FIX: the
    caller is not trusted). False and no write when it is not pending or is held
    (unpushed, unverified, unreadable remote, ...), so two passes start it once
    and pick() keeps reporting the hold reason."""
    path = Path(path)
    entries = _read(path)
    e = _find(entries, slice_id)
    if e["state"] != "pending":
        return False
    by_id = {cast(str, o["id"]): o for o in entries}
    if _hold_reason(e, by_id, verify, {}, [])[0] is not None:
        return False
    e["state"] = "started"
    _write(path, entries)
    return True


def _verified(verify: Verify, dep: str, sha: str, attention: list[str]) -> str | None:
    try:
        got = verify(sha)
    except Exception as exc:  # unreadable remote: hold, never a crash
        attention.append(f"remote unreadable verifying INT {dep} ({type(exc).__name__})")
        return None
    if got is None:
        return None
    if isinstance(got, str) and _SHA.fullmatch(got):
        return got
    attention.append(f"verifier gave a malformed answer for INT {dep}")
    return None


def pick(path: Path | str, verify: Verify) -> Pick:
    """The pending slices that may start now, with their base SHA, in backlog order.

    Read-only. A corrupt or unreadable file starts nothing (held_all + attention)."""
    try:
        entries = _read(Path(path))
    except Unreadable as exc:
        return Pick((), (), (f"N6-14 backlog held: {exc}",), held_all=True)
    by_id = {cast(str, e["id"]): e for e in entries}
    start: list[tuple[str, str | None]] = []
    held: list[tuple[str, str]] = []
    attention: list[str] = []
    seen: dict[str, str | None] = {}
    for e in entries:
        if e["state"] != "pending":
            continue
        reason, base = _hold_reason(e, by_id, verify, seen, attention)
        if reason is None:
            start.append((cast(str, e["id"]), base))
        else:
            held.append((cast(str, e["id"]), reason))
    return Pick(tuple(start), tuple(held), tuple(attention))


def _hold_reason(e: dict[str, object], by_id: dict[str, dict[str, object]], verify: Verify,
                 seen: dict[str, str | None], attention: list[str]) -> tuple[str | None, str | None]:
    """(hold reason, None) or (None, base SHA or None): the one readiness check."""
    bases: set[str] = set()
    for dep in cast("list[str]", e["deps"]):
        d = by_id.get(dep)
        if d is None or d["state"] != "done":
            return f"dependency {dep} is {'unknown' if d is None else d['state']}", None
        if not d["int"]:
            continue
        if not d["pushed"]:
            return f"INT {dep} is not pushed", None
        if dep not in seen:
            seen[dep] = _verified(verify, dep, cast(str, d["sha"]), attention)
        base = seen[dep]
        if base is None:
            return f"INT {dep} is not verified on the remote main", None
        bases.add(base)
    if len(bases) > 1:
        return "INT dependencies verified at different main SHAs", None
    return None, min(bases) if bases else None
