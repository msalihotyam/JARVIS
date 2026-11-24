# pyright: strict
"""N6G26CLEANUP: G26 cleanup and preservation dry-run (R21), shadow only, NOT wired.

Pure: inputs only. No git call, file read or write, network, model call or wall clock; the caller
passes plain values and an injected ``clock``. Nothing here removes a branch or a worktree:
``plan`` only says what N6 WOULD do, and ``compare`` sets that against the conveyor's own
``cleanup`` / ``cleanup_refused`` / ``preserved`` events (shadow agree / disagree). A later slice
wires it after >= 5 real shadow events (kit Phase 4); until then the conveyor stays the fallback.

Per lane the verdict is ``remove`` only when ALL hold, else ``preserve`` with the reason, or
``unknown`` for malformed input (never a guess):

* the branch is not ``main`` or ``ci/*`` (R21: never those);
* the lane status is a finished one (anything else is "unknown state");
* evidence exists (M3): ``report is True``, a non-empty ``receipt`` and ``merged is True``.
  A lane with no evidence record is preserved, always;
* every commit of the branch is on main: the head sha is in ``main_shas`` (so are its
  ancestors), or the record's ``commits`` list (oldest first) ends at the head and each commit
  is in ``main_shas`` or is the source of an evidence ``picks`` entry whose target is. ``picks``
  are trusted only as the output of ``n6_refcheck.verify_picks`` (patch-ids checked there,
  resolved to full shas); this module cannot check patch-ids. A head that is not the last
  listed commit means the tip moved: preserved;
* the lane finished strictly more than ``timeout`` (the N2 timeout) before ``clock()``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, NamedTuple, cast

# ponytail: STUCK / MISSING / RUNNING and any new status are "unknown state", so preserved.
DONE = frozenset({"PASS", "BLOCKED", "DROPPED", "PARTIAL", "GREEN", "COMPLETE", "INTEGRATED"})
PLANS = ("remove", "preserve", "unknown")
VERDICTS = ("agree", "disagree", "false_remove", "excluded", "unknown")
_LANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SHA = re.compile(r"[0-9a-f]{40}")


class Plan(NamedTuple):
    verdict: str  # one of PLANS
    reason: str


def _unknown(reason: str) -> Plan:
    return Plan("unknown", f"malformed: {reason}")


def _preserve(reason: str) -> Plan:
    return Plan("preserve", reason)


def _sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _text(value: object) -> bool:
    return type(value) is str and value.strip() != ""


def _aware(value: object) -> datetime | None:
    """An aware datetime, or an ISO string that parses to one; anything else is None."""
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


def _protected(branch: str) -> bool:
    """main, HEAD, ci/* or a remote-tracking ref, in any case or spelling (Windows refs ignore
    case, so "Main" can still name refs/heads/main)."""
    name = branch.strip().casefold().removeprefix("refs/").removeprefix("heads/")
    return name in ("main", "head") or name.startswith(("ci/", "remotes/", "origin/"))


def _ours(worktree: str, lane: str) -> bool:
    """Only what this manager started: a ``.claude/worktrees/<lane>`` path, no ``..``."""
    norm = worktree.strip().replace("\\", "/").rstrip("/").casefold()
    return "/../" not in f"{norm}/" and norm.endswith(f"/.claude/worktrees/{lane.casefold()}")


def _targets(record: object) -> list[tuple[str, str]]:
    """(worktree | branch, normalised name) of one record, for the one-owner check."""
    rec = cast("Mapping[str, object]", record) if isinstance(record, Mapping) else {}
    return [(kind, value.strip().replace("\\", "/").rstrip("/").casefold())
            for kind in ("worktree", "branch")
            if type(value := rec.get(kind)) is str and value.strip()]


def _picks(value: object) -> dict[str, str] | None:
    """source sha -> picked sha, both 40 hex; a missing ``picks`` is an empty mapping."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        return None
    out: dict[str, str] = {}
    for source, new in cast("Mapping[object, object]", value).items():
        if not _sha(source) or not _sha(new):
            return None
        out[cast(str, source)] = cast(str, new)
    return out


def _evidence(value: object) -> tuple[str | None, dict[str, str]] | Plan:
    """(why the evidence is incomplete or None, picks), or an unknown Plan when malformed."""
    if not isinstance(value, Mapping):
        return _unknown("evidence record is not a mapping")
    ev = cast("Mapping[str, object]", value)
    report, receipt, merged = ev.get("report", False), ev.get("receipt"), ev.get("merged", False)
    picks = _picks(ev.get("picks"))
    if type(report) is not bool or type(merged) is not bool or picks is None or not (
            receipt is None or type(receipt) is str):
        return _unknown("evidence needs bool report / merged, str receipt, sha -> sha picks")
    missing = [name for name, ok in (("report", report), ("receipt", _text(receipt)),
                                     ("merged proof", merged)) if ok is not True]
    return ("no evidence: " + ", ".join(missing) if missing else None), picks


def plan_lane(record: object, main_shas: frozenset[str], evidence: object, *,
              now: datetime, timeout: timedelta) -> Plan:
    """remove | preserve(reason) | unknown for one lane. ``main_shas`` / ``now`` / ``timeout``
    are already checked by ``plan``; ``evidence`` is this lane's record or None."""
    if not isinstance(record, Mapping):
        return _unknown("lane record is not a mapping")
    rec = cast("Mapping[str, object]", record)
    lane, worktree, branch = rec.get("lane"), rec.get("worktree"), rec.get("branch")
    head, status = rec.get("head"), rec.get("status")
    if type(lane) is not str or not _LANE.fullmatch(lane):
        return _unknown("lane id")
    if not _text(worktree) or not _text(branch) or type(status) is not str:
        return _unknown("lane record needs worktree, branch and status strings")
    if not _sha(head):
        return _unknown("head is not 40 hex")
    commits = rec.get("commits")
    if commits is not None and (not isinstance(commits, (list, tuple)) or not all(
            _sha(c) for c in cast("list[object]", commits))):
        return _unknown("commits is not a list of 40-hex shas")
    found = None if evidence is None else _evidence(evidence)
    if isinstance(found, Plan):
        return found
    if _protected(cast(str, branch)):
        return _preserve(f"protected branch {branch}")
    if not _ours(cast(str, worktree), lane):
        return _preserve(f"not a lane worktree: {worktree}")
    if status not in DONE:
        return _preserve(f"unknown state: status {status}")
    if found is None:
        return _preserve("no evidence")  # R21 / G26: a target with no evidence is preserved
    gap, picks = found
    if gap:
        return _preserve(gap)
    if head not in main_shas:
        listed = cast("list[str]", commits or [])
        if not listed:
            return _preserve("commits not on main: head not on main and no commit list")
        if listed[-1] != head:
            return _preserve("commits not on main: head is not the listed tip (tip moved)")
        off = [c for c in listed if c not in main_shas and picks.get(c) not in main_shas]
        if off:
            return _preserve(f"commits not on main: {off[0][:12]} (+{len(off) - 1} more)")
    finished = _aware(rec.get("finished_ts"))
    if finished is None:
        return _unknown("finished_ts is not an aware ISO time")
    age = now - finished
    if age <= timeout:
        return _preserve(f"too young: {int(age.total_seconds())}s <= {int(timeout.total_seconds())}s")
    return Plan("remove", "every commit on main, evidence present, older than the timeout")


def plan(lanes: object, main_shas: object, evidence: object, *,
         clock: Callable[[], object], timeout: object) -> dict[str, Plan]:
    """lane id -> Plan. A record with no usable lane id is keyed ``#<index>``; a malformed
    shared input (lanes, main_shas, evidence, clock, timeout) makes every verdict unknown."""
    if not isinstance(lanes, (list, tuple)):
        return {"#lanes": _unknown("lanes is not a list")}
    records = cast("list[object]", lanes)
    keys: list[str] = []
    for index, record in enumerate(records):
        lane = cast("Mapping[str, object]", record).get("lane") if isinstance(record, Mapping) else None
        keys.append(lane if type(lane) is str and _LANE.fullmatch(lane) else f"#{index}")
    raw = clock()
    now = _aware(raw) if isinstance(raw, datetime) else None
    shared = None
    if not isinstance(main_shas, (set, frozenset)) or not all(
            _sha(s) for s in cast("set[object]", main_shas)):
        shared = "main_shas is not a set of 40-hex shas"
    elif not isinstance(evidence, Mapping):
        shared = "evidence is not a lane -> record mapping"
    elif now is None:
        shared = "clock did not return an aware datetime"
    elif type(timeout) is not timedelta or timeout <= timedelta(0):
        shared = "timeout is not a positive timedelta"
    if shared:
        return {key: _unknown(shared) for key in keys}
    shas = frozenset(cast("set[str]", main_shas))
    evs = cast("Mapping[str, object]", evidence)
    targets = [t for record in records for t in _targets(record)]  # one target, one owner
    out: dict[str, Plan] = {}
    for key, record in zip(keys, records):
        if keys.count(key) > 1:  # two records for one lane: neither is trusted
            out[key] = _unknown(f"duplicate lane id {key}")
            continue
        claimed = [kind for kind, name in _targets(record) if targets.count((kind, name)) > 1]
        if claimed:
            out[key] = _unknown(f"{claimed[0]} claimed by more than one lane")
            continue
        out[key] = plan_lane(record, shas, evs.get(key), now=cast(datetime, now),
                             timeout=cast(timedelta, timeout))
    return out


def conveyor_verdicts(events: Iterable[Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
    """lane -> (remove | preserve, reason) from the LAST cleanup / cleanup_refused / preserved
    event in ledger order (the conveyor can log ``cleanup removed`` for one step and then
    ``cleanup_refused`` for the worktree). ``preserved_released`` clears the lane: the next
    cleanup event decides. A lane absent from the result is excluded."""
    out: dict[str, tuple[str, str]] = {}
    for event in events:
        name, lane = event.get("event"), event.get("lane")
        if type(lane) is not str:
            continue
        if name == "preserved_released":
            out.pop(lane, None)
        elif name == "cleanup":
            result = event.get("result")
            out[lane] = (("remove", "cleanup removed") if result == "removed"
                         else ("preserve", f"cleanup {result}"))
        elif name == "cleanup_refused":
            out[lane] = ("preserve", f"cleanup_refused {event.get('step')}: {event.get('reason')}")
        elif name == "preserved":
            out[lane] = ("preserve", f"preserved: {event.get('reason')}")
    return out


def compare(lanes: object, main_shas: object, evidence: object, events: object, *,
            clock: Callable[[], object], timeout: object) -> dict[str, Any]:
    """One shadow record per planned lane, plus totals and the agreement rate over judged lanes
    (n6_shadow's shape). ``false_remove``: N6 would remove what the conveyor kept."""
    raw = clock()  # read once: the plan and the records share one instant
    plans = plan(lanes, main_shas, evidence, clock=lambda: raw, timeout=timeout)
    ts = raw.isoformat() if isinstance(raw, datetime) else None
    ok = isinstance(events, (list, tuple)) and all(
        isinstance(e, Mapping) for e in cast("list[object]", events))
    conveyor = conveyor_verdicts(cast("list[Mapping[str, Any]]", events)) if ok else {}
    records: list[dict[str, Any]] = []
    for lane in sorted(plans):
        n6 = plans[lane]
        theirs, why = conveyor.get(lane, ("excluded", "no conveyor cleanup event"))
        if not ok:
            verdict, why = "unknown", "malformed: events is not a list of objects"
        elif theirs == "excluded":
            verdict = "excluded"
        elif n6.verdict == "unknown":
            verdict = "unknown"
        elif n6.verdict == theirs:
            verdict = "agree"
        else:
            verdict = "false_remove" if n6.verdict == "remove" else "disagree"
        records.append({"ts": ts, "verdict": verdict, "lane": lane,
                        "conveyor": {"verdict": theirs, "reason": why},
                        "n6": {"verdict": n6.verdict, "reason": n6.reason}})
    totals = {v: sum(r["verdict"] == v for r in records) for v in VERDICTS}
    judged = totals["agree"] + totals["disagree"] + totals["false_remove"]
    return {"ts": ts, "records": records, "totals": totals,
            "agreement": totals["agree"] / judged if judged else None}
