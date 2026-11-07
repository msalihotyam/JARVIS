# pyright: strict
"""N6-12 (G14, R12, R14): the boot scan and crash recovery run before the N6 driver acts.

``scan`` reads only the N6 lease and the durable pause file. Everything else comes in as
data (worktrees, commits, receipts, applied shas, outbox rows, events), so tests need no
live git. Each leftover gets exactly one action:

- RESUME: only when every needed fact is present and consistent under this owner's live lease.
- HAND_BACK: the slice goes back to the conveyor. Boot records why, then releases the lease
  if it is ours and live. Recording comes first, so a crash between the two keeps the lease held.
- ATTENTION: the leftover is unknown or not ours. It is left untouched and only recorded.

A paused or unreadable (HELD_ALL) state sets ``hold``. ``act`` then touches nothing and the
driver does not run. Boot never calls ``acquire``, so resuming under a new epoch belongs to
the conveyor. ``record`` must be a single atomic event+outbox write (see ``recorder``).
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jarvis import n6_lease
from jarvis.n6_lease import Lease
from jarvis.work_controls import read_pause
from jarvis.work_evidence import EvidenceReceipt

_SHA = re.compile(r"[0-9a-f]{40,64}")


class Action(enum.Enum):
    RESUME = "resume"
    HAND_BACK = "hand_back"
    ATTENTION = "attention"


@dataclass(frozen=True)
class Item:
    kind: str  # "slice" or "outbox"
    ref: str  # slice id, or "<run_id>#<event_seq>"
    action: Action
    reason: str
    sha: str = ""


@dataclass(frozen=True)
class Facts:
    known: frozenset[str] = frozenset()  # slice ids the conveyor owns
    worktrees: frozenset[str] = frozenset()  # slice ids that left a worktree behind
    commits: Mapping[str, str] = field(default_factory=dict[str, str])  # slice -> branch commit sha
    receipts: Mapping[str, str] = field(default_factory=dict[str, str])  # slice -> receipt JSON
    applied: frozenset[str] = frozenset()  # commit shas already integrated (git, as data)
    outbox: tuple[tuple[str, int], ...] = ()  # undelivered (run_id, event_seq) rows
    events: frozenset[tuple[str, int]] = frozenset()  # persisted (run_id, seq) events


@dataclass(frozen=True)
class Report:
    owner: str
    hold: str  # "" lets act() run; otherwise why nothing may act ("paused", "held_all")
    lease: Lease | None
    items: tuple[Item, ...]


def _receipt_ok(raw: str | None) -> bool:
    try:
        return raw is not None and EvidenceReceipt.from_json(raw).verify()
    except (ValueError, TypeError, RecursionError):
        return False


def _slice(sid: str, owner: str, now: float, lease: Lease | None, facts: Facts) -> Item:
    if sid not in facts.known:
        return Item("slice", sid, Action.ATTENTION, "unknown leftover")
    if lease is None or lease.slice_id != sid:
        return Item("slice", sid, Action.HAND_BACK, "no N6 lease")
    if lease.expires <= now:
        return Item("slice", sid, Action.HAND_BACK, f"expired lease epoch {lease.epoch}")
    if lease.owner != owner:
        return Item("slice", sid, Action.ATTENTION, "live lease of another owner")
    sha = facts.commits.get(sid, "")
    missing = [name for name, ok in (
        ("worktree", sid in facts.worktrees),
        ("commit", _SHA.fullmatch(sha) is not None),
        ("receipt", _receipt_ok(facts.receipts.get(sid))),
    ) if not ok]
    if missing:
        return Item("slice", sid, Action.HAND_BACK, "missing or invalid " + ",".join(missing))
    return Item("slice", sid, Action.RESUME, "consistent", sha)


def scan(lease_path: Path | str, pause_path: Path | str, owner: str, now: float,
         facts: Facts) -> Report:
    """Classify every leftover. Pure apart from reading the lease and pause files."""
    state = n6_lease._read(Path(lease_path))  # pyright: ignore[reportPrivateUsage]
    held_all = state is n6_lease.HELD_ALL
    lease = n6_lease._lease(state) if isinstance(state, dict) else None  # pyright: ignore[reportPrivateUsage]
    sids = facts.worktrees | set(facts.commits) | set(facts.receipts)
    if lease is not None and lease.slice_id in facts.known:
        sids |= {lease.slice_id}
    items: list[Item] = []
    for sid in sorted(sids):
        items.append(Item("slice", sid, Action.ATTENTION, "lease unreadable")
                     if held_all else _slice(sid, owner, now, lease, facts))
    for run_id, seq in facts.outbox:
        ok = (run_id, seq) in facts.events
        items.append(Item("outbox", f"{run_id}#{seq}", Action.RESUME if ok else Action.ATTENTION,
                          "deliver" if ok else "outbox row without its event"))
    hold = "paused" if read_pause(Path(pause_path)) else "held_all" if held_all else ""
    return Report(owner, hold, lease, tuple(items))


def act(report: Report, lease_path: Path | str, now: float,
        record: Callable[[Item], object], resume: Callable[[Item], object]) -> bool:
    """Carry out each item once. A held report does nothing and returns False."""
    if report.hold:
        return False
    lease = report.lease
    for item in report.items:
        if item.action is Action.RESUME:
            _ = resume(item)
            continue
        _ = record(item)  # before the release: a crash here leaves the lease held
        if (item.action is Action.HAND_BACK and lease is not None and lease.slice_id == item.ref
                and lease.owner == report.owner and lease.expires > now):
            _ = n6_lease.release(lease_path, lease, now)
    return True


def apply_once(lease_path: Path | str, lease: Lease, sha: str, now: float,
               applied: frozenset[str], apply: Callable[[], object],
               receipt: Callable[[], object]) -> str:
    """Apply ``sha`` for the lease's slice at most once: "applied", "receipt" or "attention".

    The intent key (slice id + epoch + sha) is written to the lease before the apply, so a
    crash after the commit replays as a receipt-only step (git shows ``sha`` applied) and a
    crash before it becomes attention. Neither path ever applies twice. Fenced propagates."""
    if sha in applied:
        _ = receipt()
        return "receipt"
    if not n6_lease.apply_event(lease_path, lease, f"apply:{lease.slice_id}:{lease.epoch}:{sha}", now):
        return "attention"  # intent recorded earlier but git shows no apply: never guess
    _ = apply()
    _ = receipt()
    return "applied"


def recorder(notify: Callable[..., bool], run_id: str, at: datetime,
             next_seq: Callable[[], int]) -> Callable[[Item], bool]:
    """``record`` over ``NotificationCore(store.append_work_event, ...).notify``: one atomic
    event + outbox write per item, deduplicated across boots by the item's identity."""
    def record(item: Item) -> bool:
        key = f"n6-boot:{item.kind}:{item.ref}:{item.action.value}:{item.reason}"
        return notify(run_id, "interrupted", key, seq=next_seq(), at=at)
    return record
