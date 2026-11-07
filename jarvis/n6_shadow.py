# pyright: strict
"""N6-03: the shadow comparator, N6's verdict against the conveyor's for each lane, read-only.

``load`` reads one conveyor snapshot (``build/conveyor/lanes.json``, ``build/conveyor/events.jsonl``,
``build/reports/<lane>.md``) through the REQUIRED injected reader, N6-01 sight's guarded
``N6.read`` (this module stays off the app's import graph, so it never imports the root);
it raises ``TornSnapshot`` when the state moves under it or any part does not parse. ``compare``
then records, per lane in ``lanes.json``, agree / disagree / false_pass / excluded with a reason
(the orchbot agreement pattern: records plus totals). This module writes nothing at all; the
caller persists the result through the N6 notes sink (``N6.note``), never under build/conveyor/.

The conveyor's FINAL verdict (kit: "the conveyor's final accept/block verdict") is chain-aware:
a lane that finished PASS is a block when a chained check it started (ledger ``chain`` event,
src == lane) finished blocked, as CDX-N6-01 overrode N6-01. N6 applies the same chain rule.

B2+: N6 says PASS only when the report's first line is ``STATUS: PASS``, the lane state is
finished PASS, the report has exactly one 40-hex COMMIT and that sha equals independent
evidence (the ledger, else the injected ``head``); report text alone never yields PASS.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

BLOCK = frozenset({"BLOCKED", "DROPPED", "STUCK", "PARTIAL", "MISSING"})
# ponytail: CI-set statuses are excluded as orchbot does; judge them once N6 reads CI results.
EXCLUDED = frozenset({"GREEN", "COMPLETE", "INTEGRATED"})
VERDICTS = ("agree", "disagree", "false_pass", "excluded")
_LANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SHA = re.compile(r"[0-9a-f]{40}")
_SHA_EVENTS = frozenset({"ci_pending", "ci_result"})


class TornSnapshot(ValueError):
    """A snapshot that changed while it was read, or a part that does not parse."""


@dataclass(frozen=True)
class Snapshot:
    lanes: dict[str, dict[str, Any]]
    events: tuple[dict[str, Any], ...]
    reports: dict[str, str | None]


def _json(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise TornSnapshot(f"{what} does not parse: {error}") from error


def load(root: str | os.PathLike[str], *, read: Callable[[Path], str]) -> Snapshot:
    """One consistent snapshot: lanes.json is read first and last and must not change."""
    conveyor = Path(root) / "build" / "conveyor"
    first = read(conveyor / "lanes.json")
    lanes = _json(first, "lanes.json")
    if not isinstance(lanes, dict) or not all(isinstance(v, dict) for v in lanes.values()):  # pyright: ignore[reportUnknownVariableType]
        raise TornSnapshot("lanes.json is not a lane -> state mapping")
    lanes = cast(dict[str, dict[str, Any]], lanes)
    reports: dict[str, str | None] = {}
    for lane in lanes:  # lanes.json names the lanes, never the reports directory
        if not _LANE.fullmatch(lane):
            raise TornSnapshot(f"lanes.json names a malformed lane: {lane!r}")
        try:
            reports[lane] = read(Path(root) / "build" / "reports" / f"{lane}.md")
        except FileNotFoundError:
            reports[lane] = None
    text = read(conveyor / "events.jsonl")
    if text and not text.endswith("\n"):
        raise TornSnapshot("events.jsonl ends in a partial line")
    events = tuple(_json(line, "an events.jsonl line") for line in text.splitlines() if line.strip())
    if not all(isinstance(e, dict) for e in events):
        raise TornSnapshot("an events.jsonl line is not an object")
    # ponytail: lanes.json is the re-read anchor; events.jsonl is append-only, so a late
    # append only adds evidence. Re-read it too if the ledger is ever rewritten in place.
    if read(conveyor / "lanes.json") != first:
        raise TornSnapshot("lanes.json changed while the snapshot was read")
    return Snapshot(lanes, cast(tuple[dict[str, Any], ...], events), reports)


def _blocked_child(snap: Snapshot, lane: str) -> str | None:
    for event in snap.events:
        if event.get("event") == "chain" and event.get("src") == lane:
            child = snap.lanes.get(str(event.get("id")))
            if child and child.get("finished") is True and child.get("status") in BLOCK:
                return f"{event.get('id')} {child.get('status')}"
    return None


def _ledger_sha(snap: Snapshot, lane: str) -> str | None:
    found = None
    for event in snap.events:
        chained = event.get("event") == "chain" and event.get("src") == lane
        ci = event.get("event") in _SHA_EVENTS and event.get("lane") == lane
        if (chained or ci) and isinstance(event.get("sha"), str):
            found = cast(str, event["sha"])
    return found


def conveyor_verdict(snap: Snapshot, lane: str) -> tuple[str, str]:
    """(pass | block | excluded, reason): the conveyor's final, chain-aware verdict."""
    state = snap.lanes[lane]
    status = state.get("status")
    if state.get("finished") is not True:
        return "excluded", "lane has not finished"
    if status in EXCLUDED or (status != "PASS" and status not in BLOCK):
        return "excluded", f"status {status} is not judged in the shadow stage"
    if status in BLOCK:
        return "block", f"lane finished {status}"
    child = _blocked_child(snap, lane)
    return ("block", f"chained check {child}") if child else ("pass", "lane finished PASS")


def n6_verdict(snap: Snapshot, lane: str,
               head: Callable[[str], str | None]) -> tuple[str, str]:
    """(pass | block, reason): N6's own decision; PASS needs report, state and sha to agree."""
    report = snap.reports[lane]
    if report is None:
        return "block", "no report"
    lines = report.splitlines()
    if not lines or lines[0] != "STATUS: PASS":
        return "block", f"report says {lines[0] if lines else 'nothing'}"
    state = snap.lanes[lane]
    if state.get("finished") is not True or state.get("status") != "PASS":
        return "block", f"report PASS but lane state is {state.get('status')}"
    commits = [line[len("COMMIT:"):].strip() for line in lines if line.startswith("COMMIT:")]
    if len(commits) != 1 or not _SHA.fullmatch(commits[0]):
        return "block", "report needs exactly one 40-hex COMMIT"
    evidence = _ledger_sha(snap, lane) or head(lane)
    if evidence is None:
        return "block", "no sha evidence besides the report"
    if evidence != commits[0]:
        return "block", f"report COMMIT {commits[0][:7]} is not the evidence sha {evidence[:7]}"
    child = _blocked_child(snap, lane)
    if child:
        return "block", f"chained check {child}"
    return "pass", "report, lane state and sha agree"


def _no_head(_lane: str) -> str | None:
    return None


def compare(root: str | os.PathLike[str], *, clock: Callable[[], datetime],
            read: Callable[[Path], str],
            head: Callable[[str], str | None] = _no_head) -> dict[str, Any]:
    """One record per lane in lanes.json, plus totals and the agreement rate over judged lanes."""
    snap = load(root, read=read)
    ts = clock().isoformat()
    records: list[dict[str, Any]] = []
    for lane in sorted(snap.lanes):
        conveyor, conveyor_reason = conveyor_verdict(snap, lane)
        n6, n6_reason = n6_verdict(snap, lane, head)
        if conveyor == "excluded":
            verdict = "excluded"
        elif n6 == conveyor:
            verdict = "agree"
        else:
            verdict = "false_pass" if n6 == "pass" else "disagree"
        records.append({"ts": ts, "verdict": verdict, "lane": lane,
                        "conveyor": {"verdict": conveyor, "reason": conveyor_reason},
                        "n6": {"verdict": n6, "reason": n6_reason}})
    totals = {v: sum(r["verdict"] == v for r in records) for v in VERDICTS}
    judged = totals["agree"] + totals["disagree"] + totals["false_pass"]
    return {"ts": ts, "records": records, "totals": totals,
            "agreement": totals["agree"] / judged if judged else None}
