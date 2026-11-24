# pyright: strict
"""N6BASE1: the conveyor baseline for E8 per lane class and writer, recomputed read-only.

Reads the conveyor ledger (``build/conveyor/events.jsonl``) and the conveyor rows of
``build/n6/kpi.db`` (opened ``mode=ro``) and writes ``build/n6/baseline.json`` plus
``baseline.json.sha256`` ("<sha256>  baseline.json", the SHA256SUMS form). One slice is one lane
name. Class is the conveyor's own ``lane_type`` (code / review); int lanes (INT*, DRY*, route
"integration") and fix lanes (*FIX*, -AFX, -S) are not slices and are only counted as excluded.
Writer is codex when the ledger says so (a ``route`` event with worker codex, or a
``codex_commit`` event), jules lanes are excluded, else claude. A measure with no reading is the string "missing", never 0
(R32); a group with n < 10 is ``small_sample``. A torn or corrupt ledger line raises. The output
holds no wall-clock value, so a rerun on the same input is byte-identical. The script refuses to
write under ``build/conveyor/`` or ``dev/n6/acceptance/``, never opens ``data/``/``jarvis.db``/``~/.jarvis``
(nor writes there) and never writes over its own ``--events``/``--kpi-db`` source.
No network, no model call, nothing on the live conveyor. Exit: 0 written, 2 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (("build", "conveyor"), ("dev", "n6", "acceptance"))
OPERATOR_HOME = Path.home() / ".jarvis"  # ~\.jarvis\ is operator data; tests patch it
SMALL_SAMPLE = 10
MISSING = "missing"
CLASSES = ("code", "review")
WRITERS = ("claude", "codex")
GOOD = frozenset({"PASS", "COMPLETE", "DONE", "GREEN", "OK"})  # = scripts/conveyor.py GOOD
_INT_RE = re.compile(r"(?i)^int(?![a-z])")  # = scripts/conveyor.py INT_RE
DEFINITIONS = {
    "slice": "one lane name with a dispatch and a finish event; int, fix and jules lanes excluded",
    "class": "scripts/conveyor.py lane_type: review = CLREV*/CDX*, code = the rest",
    "writer": "codex if a route event has worker codex or a codex_commit event exists, else claude;"
              " a codex lane with neither (non-auto, never committed) is not visible in the ledger",
    "pass_rate": "slices whose last finish status is in conveyor GOOD (PASS COMPLETE DONE GREEN OK) / n",
    "post_accept_failures": "sum of kpi.db conveyor post_accept_defects over accepted slices with a row",
    "interventions": "needs_salih reader events over slices with any reader event (interventions_n)",
    "quota_per_accepted": "mean kpi.db conveyor quota_tokens over accepted slices with a row",
    "finish_seconds_median": "median of last finish ts - first dispatch ts",
    "window": "slices whose first dispatch ts is in [since, until)",
}


class BaselineError(ValueError):
    """Corrupt input or a refused path: nothing is written."""


@dataclass
class _Lane:
    start: datetime | None = None
    end: datetime | None = None
    status: str | None = None
    codex: bool = False
    jules: bool = False
    integration: bool = False
    readers: int = 0
    salih: int = 0


def lane_class(name: str) -> str:
    """scripts/conveyor.py ``lane_type`` (copied: importing conveyor loads the orchestrator),
    with its None (fix-flavoured name) spelled "fix"."""
    if re.search(r"(?i)fix|-afx$|-s$", name):
        return "fix"
    if re.match(r"(?i)clrev|cdx", name):
        return "review"
    if _INT_RE.match(name) or re.match(r"(?i)dry(?![a-z])", name):
        return "int"
    return "code"


def _ts(value: object, where: str) -> datetime:
    if not isinstance(value, str):
        raise BaselineError(f"{where}: ts missing or not a string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise BaselineError(f"{where}: bad ts {value!r}") from error
    if parsed.tzinfo is not None:  # the ledger is naive local time; a mix cannot be ordered
        raise BaselineError(f"{where}: timezone-aware ts {value!r}")
    return parsed


def read_events(path: Path) -> list[dict[str, object]]:
    """Every ledger line as an object; any torn, blank, NUL or non-object line raises."""
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        raise BaselineError(f"{path}: last line is torn (no trailing newline)")
    events: list[dict[str, object]] = []
    for number, raw in enumerate(data.split(b"\n")[:-1] if data else [], 1):
        where = f"{path}:{number}"
        if b"\x00" in raw:
            raise BaselineError(f"{where}: NUL byte (torn write)")
        try:
            loaded: object = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise BaselineError(f"{where}: corrupt line: {error}") from error
        if not isinstance(loaded, dict):
            raise BaselineError(f"{where}: not a JSON object")
        event = cast(dict[str, object], loaded)
        if event.get("event") in ("dispatch", "finish"):
            if not isinstance(event.get("lane"), str) or not event.get("lane"):
                raise BaselineError(f"{where}: {event.get('event')} without a lane")
            _ts(event.get("ts"), where)
        events.append(event)
    return events


def load_kpi(path: Path) -> dict[str, tuple[int, int]]:
    """slice -> (post_accept_defects, quota_tokens) of the conveyor rows, opened read-only."""
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                           " AND name = 'n6_kpi_row'").fetchone() is None:
                return {}
            got = con.execute("SELECT slice_id, post_accept_defects, quota_tokens"
                              " FROM n6_kpi_row WHERE source = 'conveyor'").fetchall()
    except sqlite3.Error as error:
        raise BaselineError(f"{path}: unreadable kpi database: {error}") from error
    return {str(r[0]): (int(r[1]), int(r[2])) for r in got}  # pyright: ignore[reportAny]


def _lanes(events: list[dict[str, object]]) -> dict[str, _Lane]:
    lanes: dict[str, _Lane] = {}
    for event in events:
        name = event.get("lane")
        if not isinstance(name, str) or not name:
            continue
        lane, kind = lanes.setdefault(name, _Lane()), event.get("event")
        if kind == "dispatch":
            ts = _ts(event.get("ts"), name)
            lane.start = ts if lane.start is None else min(lane.start, ts)
            lane.integration |= event.get("route") == "integration"
        elif kind == "finish":
            lane.end, status = _ts(event.get("ts"), name), event.get("status")
            lane.status = status if isinstance(status, str) else None
        elif kind == "route":  # an auto slice routed to a worker
            lane.codex |= event.get("worker") == "codex"
            lane.jules |= event.get("worker") == "jules"
        elif kind == "codex_commit":
            lane.codex = True
        elif isinstance(kind, str) and kind.startswith("jules_"):
            lane.jules = True
        elif kind == "reader":
            lane.readers += 1
            reading = event.get("reading")
            if isinstance(reading, dict) and cast(dict[str, object], reading).get("kind") == "needs_salih":
                lane.salih += 1
    return lanes


def _group(members: list[tuple[str, _Lane]], kpi: dict[str, tuple[int, int]]) -> dict[str, object]:
    n = len(members)
    accepted = [name for name, lane in members if lane.status in GOOD]
    rows = [kpi[name] for name in accepted if name in kpi]
    read = [lane for _, lane in members if lane.readers]  # no reader event = no reading, not 0
    finish = [(lane.end - lane.start).total_seconds() for _, lane in members
              if lane.end is not None and lane.start is not None]
    return {
        "n": n,
        "accepted": len(accepted),
        "small_sample": n < SMALL_SAMPLE,
        "pass_rate": round(len(accepted) / n, 6) if n else MISSING,
        "post_accept_failures": sum(r[0] for r in rows) if rows else MISSING,
        "interventions": sum(lane.salih for lane in read) if read else MISSING,
        "interventions_n": len(read),
        "quota_per_accepted": round(sum(r[1] for r in rows) / len(rows), 3) if rows else MISSING,
        "kpi_rows_measured": len(rows),
        "finish_seconds_median": round(statistics.median(finish), 3) if finish else MISSING,
    }


def compute(events: list[dict[str, object]], kpi: dict[str, tuple[int, int]],
            since: datetime | None = None, until: datetime | None = None) -> dict[str, object]:
    """The baseline document (no sources): groups per class/writer, exclusions and window."""
    excluded: Counter[str] = Counter()
    members: dict[str, list[tuple[str, _Lane]]] = {f"{c}/{w}": [] for c in CLASSES for w in WRITERS}
    for name, lane in sorted(_lanes(events).items()):
        cls = "int" if lane.integration else lane_class(name)
        if lane.start is None:
            excluded["no_dispatch"] += 1
        elif (since and lane.start < since) or (until and lane.start >= until):
            excluded["outside_window"] += 1
        elif cls not in CLASSES:
            excluded[f"{cls}_lane"] += 1
        elif lane.jules:
            excluded["jules_lane"] += 1
        elif lane.end is None or lane.status is None:
            excluded["unfinished"] += 1
        elif lane.end < lane.start:
            raise BaselineError(f"{name}: finish {lane.end} before first dispatch {lane.start}")
        else:
            members[f"{cls}/{'codex' if lane.codex else 'claude'}"].append((name, lane))
    counted = [lane for group in members.values() for _, lane in group]
    first = min((cast(datetime, x.start) for x in counted), default=None)
    last = max((cast(datetime, x.end) for x in counted), default=None)
    return {
        "version": 1,
        "definitions": DEFINITIONS,
        "groups": {key: _group(group, kpi) for key, group in members.items()},
        "excluded": dict(sorted(excluded.items())),
        "window": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "first_dispatch": first.isoformat() if first else None,
            "last_finish": last.isoformat() if last else None,
        },
    }


def _guard(path: Path) -> Path:
    """Refuse a write anywhere under a build/conveyor or dev/n6/acceptance directory, in any checkout."""
    target = path.resolve()
    parts = tuple(p.casefold() for p in target.parts)
    for bad in FORBIDDEN:
        if any(parts[i:i + len(bad)] == bad for i in range(len(parts))):
            raise BaselineError(f"refused: {target} is under {'/'.join(bad)}/")
    return target


def _not_operator(path: Path, root: Path) -> Path:
    """Refuse jarvis.db anywhere, data/ of this checkout and of the main checkout, and ~/.jarvis."""
    target = path.resolve()
    base = root.resolve()
    protected = [base / "data", OPERATOR_HOME.resolve()]
    if base.parent.name.casefold() == "worktrees" and base.parent.parent.name.casefold() == ".claude":
        protected.append(base.parents[2] / "data")  # a worktree: the main checkout's data/
    name = target.name.casefold()
    if name == "jarvis.db" or name.startswith("jarvis.db-") or any(target.is_relative_to(p) for p in protected):
        raise BaselineError(f"refused: {target} is operator data, never opened")
    return target


def _same(a: Path, b: Path) -> bool:
    """Same file by case-normalised resolved path or, when both exist, by identity (hard/symlink)."""
    if os.path.normcase(a) == os.path.normcase(b):
        return True
    return a.exists() and b.exists() and os.path.samefile(a, b)


def _targets(out: Path, root: Path, sources: tuple[Path, ...]) -> tuple[Path, Path]:
    """The checked ``out`` and ``out.sha256``: never conveyor/acceptance, operator data, a source or
    a sqlite sidecar of one (-wal/-shm/-journal). Runs before anything is read or written."""
    target = _guard(out)
    sums = _guard(target.with_name(target.name + ".sha256"))
    resolved = [s.resolve() for s in sources]
    for path in (target, sums):
        # Windows strips a trailing dot/space, \\?\ skips that, ":" names an NTFS stream: all alias a guarded path
        if path.anchor.startswith(("\\\\?\\", "\\\\.\\")) or any(
                ":" in p or p != p.rstrip(". ") for p in path.parts[1:]):
            raise BaselineError(f"refused: {path} is an ambiguous path spelling")
        _not_operator(path, root)
        name = os.path.normcase(path.name)
        for src in resolved:
            sidecar = (os.path.normcase(path.parent) == os.path.normcase(src.parent)
                       and name.startswith(os.path.normcase(src.name) + "-"))
            if _same(path, src) or sidecar:
                raise BaselineError(f"refused: {path} would overwrite the source {src}")
    return target, sums


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(events_path: Path, kpi_path: Path, root: Path, since: datetime | None = None,
          until: datetime | None = None) -> dict[str, object]:
    kpi_target = _not_operator(kpi_path, root)
    events = read_events(_not_operator(events_path, root))
    present = kpi_target.is_file()  # sqlite would otherwise create an empty file
    kpi = load_kpi(kpi_target) if present else {}
    doc = compute(events, kpi, since, until)
    doc["sources"] = {
        "events": {"path": str(events_path.resolve()), "lines": len(events),
                   "sha256": _sha(events_path.read_bytes())},
        "kpi_db": {"path": str(kpi_target), "present": present, "conveyor_rows": len(kpi),
                   "sha256": _sha(kpi_target.read_bytes()) if present else MISSING},
    }
    return doc


def write(doc: dict[str, object], out: Path, root: Path, sources: tuple[Path, ...]) -> str:
    """Write ``out`` and ``out.sha256`` (bytes, LF only) after the path checks; the sha256."""
    target, sums = _targets(out, root, sources)
    data = (json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    digest = _sha(data)
    line = f"{digest}  {target.name}\n".encode("utf-8")  # both byte strings before either write
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        sums.write_bytes(line)
    except OSError:
        target.unlink(missing_ok=True)  # never a baseline.json without its .sha256
        raise
    return digest


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="E8 conveyor baseline per lane class and writer")
    parser.add_argument("--events", type=Path, default=root / "build" / "conveyor" / "events.jsonl")
    parser.add_argument("--kpi-db", type=Path, default=root / "build" / "n6" / "kpi.db")
    parser.add_argument("--out", type=Path, default=root / "build" / "n6" / "baseline.json")
    parser.add_argument("--since", help="ISO ts, inclusive (first dispatch)")
    parser.add_argument("--until", help="ISO ts, exclusive (first dispatch)")
    args = parser.parse_args(argv)
    try:
        since = _ts(args.since, "--since") if args.since else None  # pyright: ignore[reportAny]
        until = _ts(args.until, "--until") if args.until else None  # pyright: ignore[reportAny]
        out, events, kpi_db = cast(Path, args.out), cast(Path, args.events), cast(Path, args.kpi_db)
        _targets(out, root, (events, kpi_db))  # refuse before reading anything
        doc = build(events, kpi_db, root, since, until)
        digest = write(doc, out, root, (events, kpi_db))
    except (BaselineError, OSError, ValueError) as error:  # ValueError: NUL / unencodable path
        print(f"REFUSED: {error}")
        return 2
    print(f"WROTE {out} sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
