"""N6-SHADOWDIFF1: the daily shadow diff, every N6/conveyor disagreement classified once.

Read-only over ``build/n6/kpi.db`` (opened ``mode=ro``), ``build/n6/notes.jsonl``,
``build/conveyor/lanes.json``, ``build/conveyor/events.jsonl`` and ``build/reports/``; it
writes only ``build/n6/daily/<date>.json`` and ``.md`` and never touches SHADOW_ON/LIMITED_ON.
A disagreement is the newest ``disagree`` / ``false_pass`` note of a lane (the runner notes a
lane once). Each gets exactly one class from ``CLASSES``, picked by the first rule in
``classify`` that holds (deterministic code, the reasons are data), an era (``sweep`` when the
lane finished before the shadow start, ``live`` after it, ``unknown`` only with no finished_ts,
which is always not_measurable) and a handover-matrix behaviour (dev/n6/N6_HANDOVER_MATRIX.md).
The shadow start is the first N6 note, else the SHADOW_ON mtime. Malformed note or event lines
are counted, never fatal. The rules mirror N6-R06DIAG: report-format blocks are stricter_format.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # CLI: make ``jarvis`` importable
from jarvis.n6_shadow import Snapshot, _no_head, _report_only, n6_verdict  # noqa: E402

CLASSES = ("n6_wrong", "orchestrator_or_conveyor_wrong", "n6_stricter_format",
           "both_reasonable", "not_measurable")
BEHAVIOURS = {"R05": "Independent review routing (chain)", "R06": "Receipts/evidence (report)",
              "R07": "State ledger (lane state)", "R12": "Recovery: crash (heal)"}
# Lane rules v2 (one exact 40-hex COMMIT line, STATUS first) hold from 2026-10-17 (+03:00).
RULE_V2 = datetime(2026, 10, 17, tzinfo=timezone(timedelta(hours=3))).timestamp()
_NOTE = re.compile(r"(\S+) (disagree|false_pass|agree): conveyor (\w+) \((.*?)\); "
                   r"n6 (\w+) \((.*)\)", re.S)
_STATUS = re.compile(r"STATUS:[\s*`]*((?i:CI_PENDING)(?!\w)|[A-Za-z]+)")  # conveyor.STATUS_RE
_HEX = re.compile(r"(?i:COMMIT)\b[^\n]*?\b([0-9a-f]{7,40})\b")
_FORMAT = ("report says ", "report needs exactly one", "COMMIT: none")
_LANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def report_status(text: str) -> str | None:
    """conveyor.report_status on text: the first STATUS word in the first 15 non-empty lines."""
    lines = [s for s in (x.lstrip(" \t>#*`﻿").rstrip() for x in text.splitlines()) if s][:15]
    for s in lines:
        m = _STATUS.match(s)
        if m:
            return "(INTERIM)" if "(interim)" in s.lower() else m.group(1).upper()
    return None


def read_notes(path: Path) -> tuple[dict[str, int], dict[str, dict[str, Any]], datetime | None]:
    """(counts, newest verdict note per lane, first note time); bad lines are counted."""
    counts = {"lines": 0, "malformed": 0, "other": 0, "agree": 0, "disagree": 0, "false_pass": 0}
    newest: dict[str, dict[str, Any]] = {}
    first: datetime | None = None
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        counts["lines"] += 1
        try:
            note = json.loads(line)
            body, at = note["text"], datetime.fromisoformat(note["at"])
            if not isinstance(body, str):
                raise TypeError("text is not a string")
        except (ValueError, TypeError, KeyError, AttributeError):
            counts["malformed"] += 1
            continue
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        first = at if first is None or at < first else first
        m = _NOTE.fullmatch(body)
        if not m:
            counts["other"] += 1
            continue
        counts[m.group(2)] += 1
        newest[m.group(1)] = {"line": number, "verdict": m.group(2),
                              "conveyor": {"verdict": m.group(3), "reason": m.group(4)},
                              "n6": {"verdict": m.group(5), "reason": m.group(6)}}
    return counts, newest, first


def read_events(path: Path) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    """(events, lane -> line of its last heal event, malformed lines)."""
    events: list[dict[str, Any]] = []
    heals: dict[str, int] = {}
    bad = 0
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            bad += 1
            continue
        events.append(event)
        if event.get("event") == "heal" and isinstance(event.get("lane"), str):
            heals[event["lane"]] = number
    return events, heals, bad


def read_kpi(db: Path) -> dict[str, dict[str, bool]]:
    """slice -> source -> passed, opened read-only; {} when the db or table is absent."""
    if not db.is_file():
        return {}
    found: dict[str, dict[str, bool]] = {}
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            for slice_id, source, passed in con.execute(
                    "SELECT slice_id, source, passed FROM n6_kpi_row"):
                found.setdefault(str(slice_id), {})[str(source)] = bool(passed)
    except sqlite3.OperationalError:
        return {}
    return found


def _finished(state: dict[str, Any] | None) -> float | None:
    try:
        value = float(state["finished_ts"]) if state else None
    except (KeyError, TypeError, ValueError):
        return None
    return value if value is not None and math.isfinite(value) else None


def _rule(lane: str, note: dict[str, Any], snap: Snapshot, finished: float | None,
          healed: int | None) -> tuple[str, str, str, str | None]:
    """(class, rule, reason, N6's verdict today): the first rule that holds wins."""
    state = snap.lanes.get(lane) if _LANE.fullmatch(lane) else None
    report = snap.reports.get(lane)
    if state is None:
        return "not_measurable", "no_lane", "lane is malformed or absent from lanes.json", None
    if report is None:
        return "not_measurable", "no_report", f"build/reports/{lane}.md is absent", None
    if finished is None:
        return "not_measurable", "no_finished_ts", "lane state has no finished_ts", None
    status = report_status(report)
    if status is None:
        return "not_measurable", "no_status", "no STATUS line in the report's first 15 lines", None
    if note["n6"]["verdict"] == "pass":
        return "n6_wrong", "false_pass", "N6 passed a lane the conveyor blocked", None
    if status != "PASS":
        heal = f" after a heal (events.jsonl line {healed})" if healed else ""
        return ("orchestrator_or_conveyor_wrong", "report_not_pass",
                f"lane state PASS{heal} but the report says {status}", None)
    today, why = n6_verdict(snap, lane, _no_head)
    if today != "block":
        return "n6_wrong", "rule_accepts", f"N6 today says {today}: {why}", today
    if not note["n6"]["reason"].startswith(_FORMAT):
        return "both_reasonable", "substance", f"N6 blocked on: {note['n6']['reason']}", today
    if finished < RULE_V2:
        return "n6_stricter_format", "pre_rule", "finished before lane rules v2 (2026-10-17)", today
    if _HEX.search(report):
        return "n6_stricter_format", "format_only", "a commit sha is named, not in the exact form", today
    files = state.get("files")
    if not isinstance(files, list):
        return "not_measurable", "no_grant", "no commit and no files grant to require one", today
    code = [f for f in files if not _report_only(f)]
    if code:
        return ("orchestrator_or_conveyor_wrong", "pass_without_commit",
                f"conveyor passed with no commit while the slice grants {code[0]}", today)
    return ("n6_stricter_format", "no_commit_form",
            "report-only slice whose no-commit line is not the exact COMMIT: none", today)


def classify(lane: str, note: dict[str, Any], snap: Snapshot, heals: dict[str, int],
             kpi: dict[str, dict[str, bool]], shadow: float | None) -> dict[str, Any]:
    """One record: class, rule, era, handover behaviour and evidence pointers."""
    state = snap.lanes.get(lane) if _LANE.fullmatch(lane) else None
    finished, healed = _finished(state), heals.get(lane)
    cls, rule, reason, today = _rule(lane, note, snap, finished, healed)
    if healed is not None:
        behaviour = "R12"
    elif "chained check" in note["conveyor"]["reason"] + note["n6"]["reason"]:
        behaviour = "R05"
    elif note["n6"]["reason"].startswith("report PASS but lane state"):
        behaviour = "R07"
    else:
        behaviour = "R06"
    era = ("unknown" if finished is None
           else "sweep" if shadow is not None and finished < shadow else "live")
    evidence = [f"build/n6/notes.jsonl:{note['line']}"]
    if state is not None:
        evidence += [f"build/reports/{lane}.md", f"build/conveyor/lanes.json#{lane}"]
    if healed is not None:
        evidence.append(f"build/conveyor/events.jsonl:{healed}")
    return {"lane": lane, "class": cls, "rule": rule, "reason": reason, "era": era,
            "behaviour": behaviour, "finished_ts": finished, "verdict": note["verdict"],
            "conveyor": note["conveyor"], "n6": {**note["n6"], "today": today},
            "kpi": kpi.get(lane, {}), "evidence": evidence}


def build(root: Path, day: date) -> dict[str, Any]:
    """The whole daily diff as data; reads only, never the wall clock."""
    n6 = root / "build" / "n6"
    counts, newest, first = read_notes(n6 / "notes.jsonl")
    if first is None and (n6 / "SHADOW_ON").is_file():
        first = datetime.fromtimestamp((n6 / "SHADOW_ON").stat().st_mtime, timezone.utc)
    events, heals, bad = read_events(root / "build" / "conveyor" / "events.jsonl")
    lanes_path = root / "build" / "conveyor" / "lanes.json"
    raw = json.loads(lanes_path.read_text(encoding="utf-8")) if lanes_path.is_file() else {}
    lanes = {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    split = sorted(lane for lane, note in newest.items() if note["verdict"] != "agree")
    reports: dict[str, str | None] = {}
    for lane in split:
        path = root / "build" / "reports" / f"{lane}.md"
        if _LANE.fullmatch(lane) and path.is_file():
            reports[lane] = path.read_text(encoding="utf-8", errors="replace")
    snap = Snapshot(lanes, tuple(events), reports)
    kpi = read_kpi(n6 / "kpi.db")
    shadow = first.timestamp() if first else None
    records = [classify(lane, newest[lane], snap, heals, kpi, shadow) for lane in split]

    def tally(era: str, key: str, names: Any) -> dict[str, int]:
        return {n: sum(r["era"] == era and r[key] == n for r in records) for n in names}

    return {"date": day.isoformat(), "shadow_start": first.isoformat() if first else None,
            "notes": counts, "events_malformed": bad, "kpi_slices": len(kpi),
            "live": tally("live", "class", CLASSES), "sweep": tally("sweep", "class", CLASSES),
            "unknown_era": sum(r["era"] == "unknown" for r in records),
            "live_by_behaviour": tally("live", "behaviour", BEHAVIOURS), "records": records}


def render_md(rep: dict[str, Any]) -> str:
    notes = rep["notes"]
    out = [f"# N6 shadow diff {rep['date']}", "",
           f"Shadow start: {rep['shadow_start'] or 'unknown'}. Note lines {notes['lines']} "
           f"(malformed {notes['malformed']}), event lines malformed {rep['events_malformed']}.",
           "", "## Live disagreements by class", ""]
    out += [f"- {c}: {n}" for c, n in rep["live"].items()]
    out += ["", "## Live disagreements by handover behaviour", ""]
    out += [f"- {b} {BEHAVIOURS[b]}: {n}" for b, n in rep["live_by_behaviour"].items()]
    out += ["", "## Sweep (finished before the shadow start)", ""]
    out += [f"- {c}: {n}" for c, n in rep["sweep"].items()]
    out += [f"- era unknown: {rep['unknown_era']}", "", "## n6_wrong: regression-set candidates", ""]
    out += [f"- {r['lane']} ({r['era']}, {r['rule']}): {r['reason']}. Evidence: "
            + ", ".join(r["evidence"]) for r in rep["records"] if r["class"] == "n6_wrong"] or ["- none"]
    return "\n".join(out) + "\n"


def write(root: Path, day: date) -> tuple[Path, Path]:
    """Write build/n6/daily/<date>.json and .md; nothing else is written."""
    rep = build(root, day)
    out = root / "build" / "n6" / "daily"
    out.mkdir(parents=True, exist_ok=True)
    js, md = out / f"{day.isoformat()}.json", out / f"{day.isoformat()}.md"
    js.write_bytes((json.dumps(rep, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
                   .encode("utf-8"))
    md.write_bytes(render_md(rep).encode("utf-8"))
    return js, md


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="N6 daily shadow diff (read-only inputs)")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    for path in write(args.root, args.date or date.today()):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
