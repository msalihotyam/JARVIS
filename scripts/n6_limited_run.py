"""N6-WIRE2: the LIMITED-phase caller of the N6 decision code (N6_KIT Phase 2).

One ``tick`` runs at most one T1 slice end to end on its worktree branch:
n6_deps.pick (read-only; T1, not INT, not known to the conveyor) ->
n6_lease.acquire -> supervise (breaker) -> switch, conveyor and lease re-check ->
n6_next marks it started -> the injected runner -> n6_record_finish +
n6_deps.record -> release -> page.

Behind build/n6/LIMITED_ON, default OFF: with the file absent (or the conveyor's
PAUSE present) a tick does nothing at all. Nothing here creates LIMITED_ON.

Fallback (N6-WIRE2FIX): nothing is marked started before the lease is held, so
a refused lease, a breaker hold or LIMITED_ON going off leaves deps.json as it
was. The conveyor skips a slice under a live N6 lease, holds everything while
lease.json is unreadable and holds the lease's OS lock while it dispatches, so
N6 and the conveyor never both start one slice; N6 also never takes a slice the
conveyor recorded (lanes.json, inbox/started). A tick releases its lease in
every path (finally); a crashed tick's lease expires after ``ttl_s``, which
must be at least the runner's wall cap. T2, INT, push and cleanup stay with the
conveyor: an INT flag or INT name excludes a slice, and an unknown or raising
tier counts as not T1.
"""

from __future__ import annotations

import json
import math
import re
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


INT_RE = re.compile(r"(?i)^int(?![a-z])")  # the conveyor's INT_RE: an INT-named lane


def _expires(now: object, ttl_s: object) -> float | None:
    """now + ttl_s when both are real numbers and the sum is finite and later than now."""
    if isinstance(now, bool) or isinstance(ttl_s, bool):
        return None
    try:
        t, exp = float(now), float(now + ttl_s)  # pyright: ignore[reportOperatorIssue, reportArgumentType]
    except (OverflowError, TypeError, ValueError):
        return None
    return exp if math.isfinite(t) and math.isfinite(exp) and exp > t else None


def _conveyor_has(root: Path | str, sid: str) -> bool:
    """The conveyor recorded or started ``sid`` (lanes.json key or inbox/started/<ts>-<sid>.md).
    An unreadable lanes.json or started dir counts as yes (fail closed)."""
    conv = Path(root) / "build" / "conveyor"
    try:
        lanes = json.loads((conv / "lanes.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        lanes = {}
    except (OSError, ValueError):
        return True
    if not isinstance(lanes, dict) or sid in lanes:
        return True
    try:
        return any(p.name[16:] == f"{sid}.md" for p in (conv / "inbox" / "started").iterdir())
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _takes(ports: Ports, sid: str, ints: set[str]) -> bool:
    if sid in ints or INT_RE.match(sid):
        return False  # INT (deps flag or name) stays with the conveyor whatever its tier says
    try:
        return _is_t1(ports.tier(sid))
    except Exception:
        return False


def _step(root: Path | str, n6: Path, ports: Ports) -> list[str]:
    lease_path, deps = n6 / "lease.json", n6 / "deps.json"
    held = "n6 held: clock or ttl_s not finite and positive"  # a NaN lease poisons all
    t0 = ports.now()
    if _expires(t0, ports.ttl_s) is None:
        return [held]
    if n6_lease.live_slices(lease_path, t0) != frozenset():
        return ["lease held: nothing started"]
    pick = n6_deps.pick(deps, ports.verify)  # read-only: nothing is marked before the lease
    notes = list(pick.attention)
    if not pick.start:
        return notes
    ints = {str(e["id"]) for e in n6_deps._read(deps) if e["int"] is True}  # pyright: ignore[reportPrivateUsage]
    cands = [s for s, _ in pick.start if _takes(ports, s, ints) and not _conveyor_has(root, s)]
    if not cands:
        return notes
    sid = cands[0]
    t1 = ports.now()  # fresh: picking and verify may have taken a while
    if _expires(t1, ports.ttl_s) is None:
        return [*notes, held]
    lease = n6_lease.acquire(lease_path, OWNER, sid, ports.ttl_s, t1)
    if lease is None:
        return [*notes, "lease held: nothing started"]
    ran = False
    try:
        decision = supervise(ports.situation(sid), n6_dir=n6, slice_id=sid)
        if decision.constraint == "none":
            return [*notes, f"{sid}: handed back ({decision.rule_id})"]
        if not switch_on(root):
            return [*notes, f"{sid}: handed back (switch off)"]
        # Under the lease the conveyor cannot start sid (it skips a live lease and holds
        # the lease lock while it dispatches), so this check and the mark are race-free.
        if _conveyor_has(root, sid):
            return [*notes, f"{sid}: handed back (conveyor has it)"]
        if not n6_lease.check(lease_path, lease, ports.now()):
            return [*notes, f"{sid}: handed back (lease lost)"]
        got = n6_next(n6, ports.verify, allow=lambda s: s == sid, limit=1)
        notes += [a for a in got.attention if a not in notes]
        if sid not in {s for s, _ in got.start}:
            return [*notes, f"{sid}: handed back (not startable)"]
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
