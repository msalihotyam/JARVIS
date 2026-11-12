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

N6-WIRE2FIX2 (O77 option B): the conveyor stays the only launcher. In production
the runner is ``write_claim``: the pick is written to build/n6/claim.json and the
lease is kept live; the conveyor's next pass starts it through its own start()
or refuses it, writing build/n6/claim_result.json. The next tick settles the
claim: a refused or expired claim goes back to pending, a started one waits for
the conveyor's lanes.json verdict, then n6_record_finish + n6_deps.record.
A runner that raises after the mark hands the slice back to pending.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
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
CLAIMED = "claimed"  # a runner's answer: the conveyor will start it (keep the lease)


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
    n6 = Path(root) / "build" / "n6"
    claim = (n6 / "claim.json").exists()  # an open claim settles even with the switch off
    if not claim and not switch_on(root):
        return []  # OFF: zero N6 actions
    try:
        notes = _settle(root, n6, ports) if claim else _step(root, n6, ports)
    except Exception as exc:
        notes = [f"n6 {'claim ' if claim else ''}held: {type(exc).__name__}"]
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
    ran = marked = kept = False
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
        marked = True
        status = ports.run(sid, lease)
        ran = True
        if status == CLAIMED:
            kept = True  # the lease stays live until the conveyor starts or refuses it
            return [*notes, f"{sid}: claimed for the conveyor"]
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
        if marked and not ran:  # N6-WIRE2FIX2: a raising runner never strands it started
            try:
                n6_deps.hand_back(deps, sid)
            except Exception as exc2:
                notes.append(f"{sid}: stays started ({type(exc2).__name__})")
    finally:
        if not kept and not n6_lease.release(lease_path, lease, _now(ports, t0)):
            notes.append(f"{sid}: lease lost mid-run")
    return notes


def _put(path: Path, data: object) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_claim(n6: Path, lease: n6_lease.Lease) -> str:
    """The production runner (O77 B): hand the pick to the conveyor as build/n6/claim.json."""
    _put(n6 / "claim.json", dataclasses.asdict(lease))
    return CLAIMED


def _drop(*paths: Path) -> None:
    for p in paths:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


def _settle(root: Path | str, n6: Path, ports: Ports) -> list[str]:
    """The open claim: wait for the conveyor's answer, hand a refused or expired one back
    to pending, record a started one's verdict once the conveyor's lane has finished."""
    claim_path, res_path = n6 / "claim.json", n6 / "claim_result.json"
    lease_path, deps = n6 / "lease.json", n6 / "deps.json"
    lease = n6_lease.Lease(**json.loads(claim_path.read_text(encoding="utf-8")))
    sid = lease.slice_id

    def result() -> dict[str, object] | None:
        try:
            r = json.loads(res_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        ok = isinstance(r, dict) and r.get("epoch") == lease.epoch and r.get("slice_id") == sid
        return r if ok else None  # an older claim's answer is no answer

    res = result()
    if res is None:
        # The conveyor holds this lock from its checks to the inbox/started write, so an
        # expired, unanswered claim it did not start is safe to hand back here.
        with n6_lease._locked(lease_path) as got:  # pyright: ignore[reportPrivateUsage]
            if not got or result() is not None or n6_lease.check(lease_path, lease, ports.now()) \
                    or _conveyor_has(root, sid):
                return [f"{sid}: claim pending"]
            n6_deps.hand_back(deps, sid)
            _drop(claim_path)
        return [f"{sid}: handed back (claim expired)"]
    if res.get("state") != "started":
        n6_deps.hand_back(deps, sid)
        n6_lease.release(lease_path, lease, ports.now())
        _drop(claim_path, res_path)
        return [f"{sid}: handed back ({str(res.get('reason'))[:60]})"]
    n6_lease.release(lease_path, lease, ports.now())  # started: the lane is the conveyor's now
    lanes = json.loads((Path(root) / "build" / "conveyor" / "lanes.json").read_text(encoding="utf-8"))
    ln = lanes.get(sid) if isinstance(lanes, dict) else None
    if not isinstance(ln, dict) or not ln.get("finished"):
        return [f"{sid}: running"]
    status = ln.get("status")
    verdict = status.strip().upper() if isinstance(status, str) else ""
    notes: list[str] = []
    if verdict in GOOD | BAD:
        now = ports.now()
        if not n6_record_finish(n6, sid, verdict, now, now):
            notes.append(f"{sid}: breaker not written")
        try:
            n6_deps.record(deps, sid, "done" if verdict in GOOD else "blocked")
        except n6_deps.Unreadable:
            raise
        except ValueError:
            pass  # already final: a second settle never rewrites it
        notes.append(f"{sid}: {verdict}")
    else:
        notes.append(f"{sid}: no verdict ({str(status)[:40]})")
    _drop(claim_path, res_path)
    return notes


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)


def _verify(sha: str) -> str | None:
    """The local origin/main SHA when it contains ``sha`` (local refs only, no fetch)."""
    rc = _git("merge-base", "--is-ancestor", sha, "origin/main").returncode
    if rc == 1:
        return None
    if rc != 0:
        raise RuntimeError(f"git merge-base rc {rc}")
    return _git("rev-parse", "origin/main").stdout.strip()


def _tier(sid: str) -> object:
    data = json.loads((ROOT / "build" / "conveyor" / "backlog.json").read_text(encoding="utf-8"))
    slices = data.get("slices") if isinstance(data, dict) else data
    return next(s.get("tier") for s in slices if isinstance(s, dict) and s.get("id") == sid)


def _situation(sid: str) -> Situation:
    # ponytail: nothing writes situation.json yet; absent -> _step hands the pick back.
    return Situation(**json.loads((ROOT / "build" / "n6" / "situation.json").read_text(encoding="utf-8")))


def main() -> int:
    n6 = ROOT / "build" / "n6"
    if not switch_on(ROOT) and not (n6 / "claim.json").exists():
        print("N6 LIMITED is off (build/n6/LIMITED_ON absent): nothing done")
        return 0
    # ponytail: no phone send wired (page=None); n6_notify.flush when the LIMITED phase wants it.
    ports = Ports(now=time.time, verify=_verify, tier=_tier, situation=_situation,
                  run=lambda sid, lease: write_claim(n6, lease), ttl_s=900.0)
    for note in tick(ROOT, ports):
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
