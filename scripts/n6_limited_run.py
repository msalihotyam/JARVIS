"""N6-WIRE2: the LIMITED-phase caller of the N6 decision code (N6_KIT Phase 2).

One ``tick`` runs at most one T1 slice end to end on its worktree branch:
n6_next (T1 only, one start) -> n6_lease.acquire -> supervise (breaker) ->
the injected runner -> n6_record_finish + n6_deps.record -> release -> page.

Behind build/n6/LIMITED_ON, default OFF: with the file absent (or the conveyor's
PAUSE present) a tick does nothing at all. Nothing here creates LIMITED_ON.

Fallback: the conveyor already skips a slice under a live N6 lease and holds
everything while lease.json is unreadable. A tick releases its lease in every
path (finally), so switching LIMITED_ON off, a hold or any N6 error hands the
slice back to the conveyor; a crashed tick's lease expires after ``ttl_s``,
which must be at least the runner's wall cap. N6 never re-picks a slice it
marked started, so a handed-back slice is run by the conveyor alone (no double
run). T2, INT, push and cleanup stay with the conveyor: ``tier`` gates the pick
and an unknown or raising tier counts as not T1.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis import n6_deps, n6_lease  # noqa: E402
from jarvis.n6_breaker import BAD, GOOD  # noqa: E402
from jarvis.work_planner import n6_next  # noqa: E402
from jarvis.work_supervisor import Situation, n6_record_finish, supervise  # noqa: E402

OWNER = "n6-limited"


@dataclass(frozen=True)
class Ports:
    now: Callable[[], float]
    verify: n6_deps.Verify
    tier: Callable[[str], object]
    situation: Callable[[str], Situation]
    run: Callable[[str, n6_lease.Lease], object]  # the lane's final status, e.g. "PASS"
    ttl_s: float  # >= the runner's wall cap; the lease is never renewed here
    page: Callable[[Path], object] | None = None  # e.g. n6_notify.flush over notify.json


def switch_on(root: Path | str) -> bool:
    root = Path(root)
    return (root / "build" / "n6" / "LIMITED_ON").is_file() \
        and not (root / "build" / "conveyor" / "PAUSE").exists()


def tick(root: Path | str, ports: Ports) -> list[str]:
    """One LIMITED-phase step, then one page; the notes say what happened. Never raises."""
    if not switch_on(root):
        return []  # OFF: zero N6 actions
    n6 = Path(root) / "build" / "n6"
    try:
        notes = _step(root, n6, ports)
    except Exception as exc:
        notes = [f"n6 held: {type(exc).__name__}"]
    if ports.page is not None:
        try:
            ports.page(n6 / "notify.json")
        except Exception as exc:
            notes.append(f"page failed: {type(exc).__name__}")
    return notes


def _is_t1(tier: object) -> bool:
    return type(tier) is str and tier == "T1"  # exactly the str "T1"; no __eq__ tricks


def _now(ports: Ports, fallback: float) -> float:
    try:
        t = ports.now()
        return t if math.isfinite(t) else fallback
    except Exception:  # a failing clock never keeps the lease from being released
        return fallback


def _step(root: Path | str, n6: Path, ports: Ports) -> list[str]:
    lease_path = n6 / "lease.json"
    t0 = ports.now()
    if not (math.isfinite(t0) and math.isfinite(ports.ttl_s) and ports.ttl_s > 0):
        return ["n6 held: clock or ttl_s not finite and positive"]  # a NaN lease poisons all
    if n6_lease.live_slices(lease_path, t0) != frozenset():
        return ["lease held: nothing started"]
    pick = n6_next(n6, ports.verify, allow=lambda s: _is_t1(ports.tier(s)), limit=1)
    notes = list(pick.attention)
    if not pick.start:
        return notes
    sid = pick.start[0][0]
    lease = n6_lease.acquire(lease_path, OWNER, sid, ports.ttl_s, t0)
    if lease is None:
        return [*notes, f"{sid}: marked started but lease refused; the conveyor keeps it"]
    ran = False
    try:
        decision = supervise(ports.situation(sid), n6_dir=n6, slice_id=sid)
        if decision.constraint == "none":
            return [*notes, f"{sid}: handed back ({decision.rule_id})"]
        if not switch_on(root):
            return [*notes, f"{sid}: handed back (switch off)"]
        status = ports.run(sid, lease)
        ran = True
        verdict = status.strip().upper() if isinstance(status, str) else ""
        if verdict in GOOD | BAD:
            now = _now(ports, t0)
            if not n6_record_finish(n6, sid, verdict, now, now):
                notes.append(f"{sid}: breaker not written")
            n6_deps.record(n6 / "deps.json", sid, "done" if verdict in GOOD else "blocked")
            notes.append(f"{sid}: {verdict}")
        else:
            notes.append(f"{sid}: no verdict ({str(status)[:40]})")
    except Exception as exc:
        what = "ran; bookkeeping failed" if ran else "handed back"
        notes.append(f"{sid}: {what} ({type(exc).__name__})")
    finally:
        if not n6_lease.release(lease_path, lease, _now(ports, t0)):
            notes.append(f"{sid}: lease lost mid-run")
    return notes


def main() -> int:
    if not switch_on(ROOT):
        print("N6 LIMITED is off (build/n6/LIMITED_ON absent): nothing done")
        return 0
    # ponytail: no lane runner or phone send is wired yet; the LIMITED phase supplies them.
    print("N6 LIMITED is on but no lane runner is wired (N6-WIRE2): nothing done")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
