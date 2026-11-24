# pyright: strict
"""N6G26CLEANUP: G26 cleanup and preservation dry-run (R21), shadow only, NOT wired.

Pure: inputs only. No git call, file read or write, network, model call or wall clock; the caller
passes plain values and an injected ``clock``. Nothing here removes a branch or a worktree:
``plan`` only says what N6 WOULD do, and ``compare`` sets that against the conveyor's own
``cleanup`` / ``cleanup_refused`` / ``preserved`` events (shadow agree / disagree). A later slice
wires it after >= 5 real shadow events (kit Phase 4); until then the conveyor stays the fallback.

Per lane the verdict is ``remove`` only when ALL hold, else ``preserve`` with the reason, or
``unknown`` for malformed input (never a guess):

* the branch is not ``main`` or ``ci/*`` (R21: never those) and is a well-formed branch name;
* the worktree is exactly ``<root>/.claude/worktrees/<lane>`` for the injected absolute ``root``
  (the checkout that owns the lanes), so a relative, ``..`` or foreign-root path never matches;
* the lane status is a finished one (anything else is "unknown state");
* evidence exists (M3): ``report is True``, a non-empty ``receipt`` and ``merged is True``.
  A lane with no evidence record is preserved, always;
* every commit of the branch is on main: the head sha is in ``main_shas`` (so are its
  ancestors), or the record's ``commits`` list (oldest first) ends at the head and each commit
  is in ``main_shas`` or is the source of an evidence ``picks`` entry whose target is. ``picks``
  are trusted only as the output of ``n6_refcheck.verify_picks`` (patch-ids checked there,
  resolved to full shas); this module cannot check patch-ids. A supplied list that does not
  end at the head means the tip moved: preserved, even when the head is on main;
* the lane finished strictly more than ``timeout`` (the N2 timeout) before ``clock()``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, NamedTuple, cast

# ponytail: STUCK / MISSING / RUNNING and any new status are "unknown state", so preserved.
DONE = frozenset({"PASS", "BLOCKED", "DROPPED", "PARTIAL", "GREEN", "COMPLETE", "INTEGRATED"})
PLANS = ("remove", "preserve", "unknown")
VERDICTS = ("agree", "disagree", "false_remove", "excluded", "unknown")
# no trailing ".": Windows drops it, so lane "L1." would name L1's worktree directory
_LANE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9_-])?")
_SHA = re.compile(r"[0-9a-f]{40}")
# git check-ref-format plus a leading "-" (it would become an option to git branch -D)
_BAD_REF = re.compile(r"^-|\.\.|[\x00-\x20\x7f~^:?*\[\\]|@\{|\.lock(?:/|$)|(?:^|/)\.|//|[/.]$|^@$")
# normalised absolute root: "x:/a/b" or "/a/b", no empty segment (so no "//", UNC or bare "/")
_ROOT = re.compile(r"(?:[a-z]:)?(?:/[^/]+)+")
# the conveyor logs cleanup result "removed" or "error"; anything else is not evidence
_CLEANUP = {"removed": ("remove", "cleanup removed"), "error": ("preserve", "cleanup error")}


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
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        offset = value.utcoffset()
    except Exception:  # noqa: BLE001 - a broken tzinfo is malformed input, never a guess
        return None
    return None if offset is None else value


def _branch(name: str) -> str:
    """Canonical local branch name: "refs/heads/x", "heads/x", nested spellings of them and "X"
    all name x (Windows refs ignore case, so "Main" can still name refs/heads/main)."""
    name = name.strip().casefold()
    while (short := name.removeprefix("refs/heads/").removeprefix("heads/")) != name:
        name = short
    return name


def _protected(branch: str) -> bool:
    """main, HEAD, ci/*, a remote-tracking or any other non-branch ref, in any spelling."""
    name = _branch(branch)
    return name in ("main", "head") or name.startswith(("ci/", "remotes/", "origin/", "refs/"))


def _bad_branch(branch: str) -> bool:
    """The raw name and its canonical form must both pass ("refs/heads/-x" is "-x")."""
    return any(not name or not name.isprintable() or _BAD_REF.search(name) is not None
               for name in (branch, _branch(branch)))


def _path(value: str) -> str:
    # ASCII fold only: casefold / strip would map other NTFS names (Kelvin K, NBSP) onto ours
    return value.replace("\\", "/").rstrip("/").lower()


def _root(value: object) -> str | None:
    """The owning checkout, normalised: ASCII, absolute (drive or leading "/"), no "." / ".."
    part, no surrounding space."""
    if type(value) is not str or not value.isascii() or value != value.strip():
        return None
    norm = _path(value)
    ok = _ROOT.fullmatch(norm) and not {".", ".."} & set(norm.split("/"))
    return norm if ok else None


def _ours(worktree: str, lane: str, root: str) -> bool:
    """Only what this manager started: exactly ``<root>/.claude/worktrees/<lane>``."""
    norm = _path(worktree)
    return (worktree.isascii() and worktree == worktree.strip()
            and not {".", ".."} & set(norm.split("/"))
            and norm == f"{root}/.claude/worktrees/{lane.lower()}")


def _leaf(path: str) -> str:
    """The directory a worktree path names, as Windows resolves it ("." / ".." parts, trailing
    dots and spaces, case), so any spelling of one directory collides in the one-owner check.
    shortcut: a junction or 8.3 alias is not seen; needs a filesystem check when wired."""
    parts: list[str] = []
    for part in _path(path).split("/"):
        if part == "..":
            del parts[-1:]
        elif part := part.rstrip(". "):
            parts.append(part)
    return parts[-1].casefold() if parts else ""


def _targets(record: object) -> list[tuple[str, str]]:
    """(worktree | branch, canonical name) of one record, for the one-owner check."""
    rec = cast("Mapping[str, object]", record) if isinstance(record, Mapping) else {}
    return [(kind, canon(value)) for kind, canon in (("worktree", _leaf), ("branch", _branch))
            if type(value := rec.get(kind)) is str and value.strip()]


def _read(clock: object) -> object:
    """One clock read; a non-callable or raising clock gives None (a shared-input unknown)."""
    if not callable(clock):
        return None
    try:
        return clock()
    except Exception:  # noqa: BLE001 - any clock failure is malformed input, never a guess
        return None


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
              now: datetime, timeout: timedelta, root: str) -> Plan:
    """remove | preserve(reason) | unknown for one lane. ``main_shas`` / ``now`` / ``timeout``
    / ``root`` (normalised) are already checked by ``plan``; ``evidence`` is this lane's
    record or None."""
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
    if _bad_branch(cast(str, branch)):
        return _unknown(f"branch is not a valid branch name: {branch!r}")
    if not _ours(cast(str, worktree), lane, root):
        return _preserve(f"not a lane worktree: {worktree}")
    if status not in DONE:
        return _preserve(f"unknown state: status {status}")
    if found is None:
        return _preserve("no evidence")  # R21 / G26: a target with no evidence is preserved
    gap, picks = found
    if gap:
        return _preserve(gap)
    if commits is not None:  # a supplied list must end at the head, even with the head on main
        listed = cast("list[str]", list(commits))
        if not listed or listed[-1] != head:
            return _preserve("commits not on main: head is not the listed tip (tip moved)")
        off = [c for c in listed if c not in main_shas and picks.get(c) not in main_shas]
        if off:
            return _preserve(f"commits not on main: {off[0][:12]} (+{len(off) - 1} more)")
    elif head not in main_shas:
        return _preserve("commits not on main: head not on main and no commit list")
    finished = _aware(rec.get("finished_ts"))
    if finished is None:
        return _unknown("finished_ts is not an aware ISO time")
    age = now - finished
    if age <= timeout:
        return _preserve(f"too young: {int(age.total_seconds())}s <= {int(timeout.total_seconds())}s")
    return Plan("remove", "every commit on main, evidence present, older than the timeout")


def plan(lanes: object, main_shas: object, evidence: object, *,
         clock: object, timeout: object, root: object) -> dict[str, Plan]:
    """lane id -> Plan. A record with no usable lane id is keyed ``#<index>``; a malformed
    shared input (lanes, main_shas, evidence, clock, timeout, root) makes every verdict
    unknown. ``root`` is the absolute checkout that owns ``.claude/worktrees``. Input that
    raises (a hostile mapping, tzinfo or iterator) gives the one key ``#plan``, unknown."""
    try:
        return _plan(lanes, main_shas, evidence, clock=clock, timeout=timeout, root=root)
    except Exception as exc:  # noqa: BLE001 - last net: malformed input is unknown, never a crash
        return {"#plan": _unknown(f"input raised {type(exc).__name__}")}


def _plan(lanes: object, main_shas: object, evidence: object, *,
          clock: object, timeout: object, root: object) -> dict[str, Plan]:
    if not isinstance(lanes, (list, tuple)):
        return {"#lanes": _unknown("lanes is not a list")}
    records = cast("list[object]", lanes)
    keys: list[str] = []
    for index, record in enumerate(records):
        lane = cast("Mapping[str, object]", record).get("lane") if isinstance(record, Mapping) else None
        keys.append(lane if type(lane) is str and _LANE.fullmatch(lane) else f"#{index}")
    raw = _read(clock)
    now = _aware(raw) if isinstance(raw, datetime) else None
    owner = _root(root)
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
    elif owner is None:
        shared = "root is not an absolute path without . or .. parts"
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
                             timeout=cast(timedelta, timeout), root=cast(str, owner))
    return out


def conveyor_verdicts(events: Iterable[Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
    """lane -> (remove | preserve, reason) from the LAST cleanup / cleanup_refused / preserved
    event in ledger order (the conveyor can log ``cleanup removed`` for one step and then
    ``cleanup_refused`` for the worktree). ``preserved_released`` clears the lane: the next
    cleanup event decides. A cleanup result other than ``removed`` / ``error`` makes the lane
    unknown (no evidence either way). A lane absent from the result is excluded."""
    out: dict[str, tuple[str, str]] = {}
    for event in events:
        name, lane = event.get("event"), event.get("lane")
        if type(lane) is not str:
            continue
        if name == "preserved_released":
            out.pop(lane, None)
        elif name == "cleanup":
            result = event.get("result")
            out[lane] = (_CLEANUP[result] if type(result) is str and result in _CLEANUP
                         else ("unknown", f"malformed: cleanup result {result!r}"))
        elif name == "cleanup_refused":
            out[lane] = ("preserve", f"cleanup_refused {event.get('step')}: {event.get('reason')}")
        elif name == "preserved":
            out[lane] = ("preserve", f"preserved: {event.get('reason')}")
    return out


def compare(lanes: object, main_shas: object, evidence: object, events: object, *,
            clock: object, timeout: object, root: object) -> dict[str, Any]:
    """One shadow record per planned lane, plus totals and the agreement rate over judged lanes
    (n6_shadow's shape). ``false_remove``: N6 would remove what the conveyor kept. Input that
    raises gives no records and ``error``; nothing is judged."""
    try:
        return _compare(lanes, main_shas, evidence, events, clock=clock, timeout=timeout, root=root)
    except Exception as exc:  # noqa: BLE001 - last net, as in plan
        return {"ts": None, "records": [], "totals": dict.fromkeys(VERDICTS, 0), "agreement": None,
                "error": f"malformed: input raised {type(exc).__name__}"}


def _compare(lanes: object, main_shas: object, evidence: object, events: object, *,
             clock: object, timeout: object, root: object) -> dict[str, Any]:
    raw = _read(clock)  # read once: the plan and the records share one instant
    plans = plan(lanes, main_shas, evidence, clock=lambda: raw, timeout=timeout, root=root)
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
        elif n6.verdict == "unknown" or theirs == "unknown":
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
