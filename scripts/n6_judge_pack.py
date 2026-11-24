# pyright: strict
"""N6JUDGEPACK1: the N6 Phase 3 judge packet over the hash-pinned M9 acceptance set.

Lays out, for judges outside N6 (orchestrator, a second provider, Salih), the verified set hash,
each set case's inputs and observed verdicts (n6 / conveyor KPI rows, N6EVT1 limited runs), the
E8 report (scripts/n6_e8_report.py) verbatim as observed output, the E8 measures beside the
N6BASE1 baseline groups (build/n6/baseline.json), open disagreements with notes and blank
verdict lines. N6 code decides nothing here: no verdict and no better/worse call of its own.
A set whose bytes do not match SHA256SUMS, a baseline whose .sha256 does not match, a missing
KPI database or a deny/secret hit (jarvis/dora_screen.py) refuses and writes nothing. The KPI
database is opened ``mode=ro`` and backed up into a temp copy; ``data/`` and ``jarvis.db`` are
never opened. The packet holds no absolute path and no wall-clock value: same input, same
bytes. It is written only under ``<root>/build/reports/``, never under ``dev/n6/acceptance/``.
No network, no model call. Exit: 0 written, 2 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # CLI: make ``jarvis`` importable
sys.path.insert(0, str(Path(__file__).resolve().parent))  # and the sibling E8 report
import n6_e8_report as e8  # noqa: E402
from jarvis.dora_screen import screen  # noqa: E402
from jarvis.n6_kpi import KpiRow, LimitedEvent, limited_events, rows as read_rows  # noqa: E402

MISSING = "missing"
OUT_NAME = "n6_judge_packet.md"  # not N6JUDGEPACK1.md: that is the lane report
FORBIDDEN = (("dev", "n6", "acceptance"), ("build", "conveyor"))
# E8 measure -> N6BASE1 baseline key (scripts/n6_baseline.py, commit 80dd0013).
BASE_KEYS = {
    "pass_rate": "pass_rate",
    "post_accept_defects": "post_accept_failures",
    "salih_interventions": "interventions",
    "quota_tokens_per_accepted": "quota_per_accepted",
    "finish_seconds": "finish_seconds_median",
}
DEFINITION_NOTE = ("E8 columns are totals over the paired set slices (pass rate and quota per"
                   " accepted are ratios); N6BASE1 columns are per class/writer group (sum of"
                   " failures, mean quota, median finish). They are shown side by side and not"
                   " compared by code.")
VERDICT_LINES = (
    "JUDGE orchestrator: ____  notes: ____",
    "JUDGE second provider (ChatGPT or Codex): ____  notes: ____",
    "ROLLBACK DRILL passed (yes/no, by whom): ____",
    "SALIH: ____  signed: ____",
)


class PackError(ValueError):
    """A refused input or path: nothing is written."""


def _parts(path: Path) -> tuple[str, ...]:
    return tuple(p.casefold() for p in path.resolve().parts)


def _under(path: Path, folder: Path) -> bool:
    inner, outer = _parts(path), _parts(folder)
    return inner[:len(outer)] == outer


def _not_operator(path: Path, root: Path) -> Path:
    """Resolved (symlinks followed); any data/ or .jarvis/ folder, jarvis.db or hardlink refused."""
    resolved = path.resolve()
    parts = _parts(path)
    if ("jarvis.db" in (path.name.casefold(), parts[-1]) or _under(path, root / "data")
            or {"data", ".jarvis"} & set(parts[:-1])
            or (resolved.is_file() and resolved.stat().st_nlink > 1)):
        raise PackError(f"refused: {path.name} is operator data, never opened")
    return resolved


def _guard(out: Path, root: Path) -> Path:
    """Only under <root>/build/reports/, never under dev/n6/acceptance or build/conveyor."""
    parts = _parts(out)
    if {"data", ".jarvis"} & set(parts[:-1]):
        raise PackError("refused: output is under operator data")
    for bad in FORBIDDEN:
        if any(parts[i:i + len(bad)] == bad for i in range(len(parts))):
            raise PackError(f"refused: output is under {'/'.join(bad)}/")
    if not _under(out, root / "build" / "reports") or _parts(out) == _parts(root / "build" / "reports"):
        raise PackError("refused: the packet is written only under build/reports/")
    return out.resolve()


def _kpi(db: Path, root: Path) -> tuple[tuple[KpiRow, ...], tuple[LimitedEvent, ...]]:
    """Rows and limited events from a read-only backup of ``db`` (n6_kpi opens read-write)."""
    source = _not_operator(db, root)
    if not source.is_file():  # sqlite3.connect would create an empty database
        raise PackError(f"refused: no KPI database {db.name}")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "kpi.db"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, \
                closing(sqlite3.connect(copy)) as dst:
            src.backup(dst)  # also takes rows still in a -wal file
        return read_rows(copy), limited_events(copy)


def _baseline(path: Path, root: Path) -> tuple[dict[str, dict[str, object]] | None, str]:
    """(groups, sha256) of a verified baseline.json; (None, MISSING) when it is absent."""
    source = _not_operator(path, root)
    if not source.is_file():
        return None, MISSING
    data = source.read_bytes()
    sums = source.with_name(source.name + ".sha256")
    try:
        pinned = sums.read_text(encoding="ascii").split()
    except (OSError, UnicodeDecodeError) as error:
        raise PackError(f"refused: cannot read {sums.name}: {error}") from error
    actual = hashlib.sha256(data).hexdigest()
    if pinned != [actual, source.name]:
        raise PackError(f"refused: baseline hash mismatch, {sums.name} does not pin {actual}")
    try:
        doc: object = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise PackError(f"refused: malformed baseline: {error}") from error
    groups = cast(dict[str, object], doc).get("groups") if isinstance(doc, dict) else None
    if not isinstance(groups, dict) or not all(
            isinstance(g, dict) for g in cast(dict[str, object], groups).values()):
        raise PackError("refused: malformed baseline: groups must be an object of objects")
    return cast(dict[str, dict[str, object]], groups), actual


def _num(value: object) -> str:
    if value is None or value == MISSING:
        return MISSING
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PackError(f"refused: malformed baseline value {value!r}")
    if math.isfinite(value):
        return f"{value:.4g}"
    if value == math.inf:  # E8's own "no accepted slice" quota
        return "inf"
    raise PackError(f"refused: non-finite value {value!r}")


def _row(row: KpiRow | None) -> str:
    if row is None:
        return MISSING
    return (f"passed={'yes' if row.passed else 'no'} post_accept_defects={row.post_accept_defects}"
            f" salih_interventions={row.salih_interventions} quota_tokens={row.quota_tokens}"
            f" finish_seconds={row.finish_seconds:.4g} security_incidents={row.security_incidents}"
            f" origin={row.origin}")


def build(acceptance_set: Path, kpi_db: Path, baseline: Path, root: Path) -> str:
    """The packet text; raises PackError (nothing written) on any refused input."""
    try:
        pinned, slice_ids = e8.load_set(_not_operator(acceptance_set, root))
    except e8.SetError as error:
        raise PackError(f"refused: acceptance set: {error}") from error
    rows, limited = _kpi(kpi_db, root)
    groups, base_sha = _baseline(baseline, root)
    code, e8_text = e8.report(rows, acceptance_set, limited)
    if code == 2:
        raise PackError(f"refused: the E8 report refused: {e8_text.splitlines()[0]}")
    by = {(r.slice_id, r.source): r for r in rows}
    runs: dict[str, list[LimitedEvent]] = {}
    for event in limited:
        runs.setdefault(event.slice_id, []).append(event)
    notes: list[str] = []
    lines = ["# N6 Phase 3 judge packet", "",
             "Laid out for judges outside N6. N6 code decides nothing in this packet: it shows"
             " inputs and observed outputs only, and every verdict line below is blank.", "",
             "## Acceptance set", f"FILE: {e8.SET_NAME}",
             f"PINNED SHA256: {pinned} (verified against {e8.SUMS_NAME})",
             "LIMITS: " + " ".join(f"{k}={v}" for k, v in e8.LIMITS.items()),
             f"CASES: {len(slice_ids)}", f"KPI DATABASE: {kpi_db.name} (read-only copy)",
             f"BASELINE: {baseline.name} sha256 {base_sha}", "",
             "## Per-case inputs and observed verdicts"]
    if not slice_ids:
        lines.append("none: the pinned set lists no slice")
    for slice_id in slice_ids:
        n6_row = by.get((slice_id, "n6"))
        lines += [f"CASE {slice_id}", f"  n6: {_row(n6_row)}",
                  f"  conveyor: {_row(by.get((slice_id, 'conveyor')))}",
                  "  n6 limited runs: " + ("; ".join(
                      f"{e.run_id} verdict={e.verdict} receipt={e.receipt_id}"
                      for e in runs.get(slice_id, [])) or "none")]
        for event in runs.get(slice_id, []):
            if n6_row is not None and (event.verdict == "PASS") != n6_row.passed:
                notes.append(f"{slice_id}: limited run {event.run_id} verdict {event.verdict} but"
                             f" the n6 KPI row says passed={'yes' if n6_row.passed else 'no'}")
    lines += ["", "## E8 report (output of scripts/n6_e8_report.py, verbatim)",
              f"E8 EXIT CODE: {code} (0 ADVANCE, 1 DO-NOT-ADVANCE, as that report computes it)", ""]
    lines += [f"    {line}" for line in e8_text.splitlines()]
    lines += _table(slice_ids, by, groups, notes)
    reasons = [line[len("REASON: "):] for line in e8_text.splitlines() if line.startswith("REASON: ")]
    disagreements = ([f"E8 reason: {r}" for r in reasons] + notes + [DEFINITION_NOTE])
    lines += ["", "## Open disagreements (for the judges; none is resolved by code)"]
    lines += [f"{i}. {d}" for i, d in enumerate(disagreements, 1)]
    lines += ["", "## Verdicts (blank: filled by the judges and Salih, never by N6)"]
    lines += [f"- {v}" for v in VERDICT_LINES]
    # A line break inside a data value (run id, verdict, group name) could forge a verdict line.
    if any(ch in line for line in lines for ch in "\r\n\v\f\x1c\x1d\x1e\x85  "):
        raise PackError("refused: a line break inside an input value")
    text = "\n".join(lines) + "\n"
    ok, label = screen(text)
    if not ok:
        raise PackError(f"refused: the packet failed the deny/secret screen ({label})")
    return text


def _table(slice_ids: tuple[str, ...], by: dict[tuple[str, str], KpiRow],
           groups: dict[str, dict[str, object]] | None, notes: list[str]) -> list[str]:
    """E8 measures over the paired set beside each N6BASE1 group; appends its notes."""
    paired = [s for s in slice_ids if (s, "n6") in by and (s, "conveyor") in by]
    sides = {src: e8._measures([by[(s, src)] for s in paired]) if paired else None  # pyright: ignore[reportPrivateUsage]
             for src in ("n6", "conveyor")}
    names = sorted(groups or {})
    if groups is None:
        notes.append("baseline.json is absent: every N6BASE1 column reads missing")
        names = ["N6BASE1"]
    if not paired:
        notes.append("no paired set slice: the n6 and conveyor columns read missing")
    # One labeled line per measure, not a grid: a row of bare numbers joined by " | " reads as a
    # phone number to the deny screen, and a column label between values keeps that from happening.
    columns = ["n6 (set)", "conveyor (set)", *names]
    lines = ["", "## E8 measures vs N6BASE1 (shown side by side, not compared by code)", "",
             "COLUMNS: " + "; ".join(columns)]
    for measure, key in BASE_KEYS.items():
        cells = [_num(None if side is None else side[measure]) for side in sides.values()]
        for name in names:
            value = _num(None if groups is None else groups[name].get(key))
            if value == MISSING and groups is not None:
                notes.append(f"N6BASE1 {name} {key} is missing")
            cells.append(value)
        lines.append(f"- {measure}: " + "; ".join(f"{c}={v}" for c, v in zip(columns, cells)))
    for extra in ("n", "small_sample"):
        cells = [str(len(paired)) if extra == "n" else "-"] * 2
        for name in names:
            value = None if groups is None else groups[name].get(extra)
            cells.append(MISSING if value is None else str(value))
            if extra == "small_sample" and value is True:
                notes.append(f"N6BASE1 {name} is a small sample (n < 10)")
        lines.append(f"- {extra}: " + "; ".join(f"{c}={v}" for c, v in zip(columns, cells)))
    return lines


def write(text: str, out: Path, root: Path) -> str:
    """Write the packet only under build/reports/; returns its sha256."""
    target = _guard(out, root)
    data = text.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write beside, then replace the name: a hardlinked old packet's other name is never written.
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    os.replace(part, target)
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="N6 Phase 3 judge packet (decides nothing)")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--set", dest="acceptance_set", type=Path)
    parser.add_argument("--kpi", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    root = cast(Path, args.root).resolve()
    acceptance_set = cast(Path | None, args.acceptance_set) or root / "dev" / "n6" / "acceptance" / e8.SET_NAME
    kpi = cast(Path | None, args.kpi) or root / "build" / "n6" / "kpi.db"
    baseline = cast(Path | None, args.baseline) or root / "build" / "n6" / "baseline.json"
    out = cast(Path | None, args.out) or root / "build" / "reports" / OUT_NAME
    try:
        _guard(out, root)  # refuse a bad target before reading anything
        digest = write(build(acceptance_set, kpi, baseline, root), out, root)
    except (PackError, ValueError, OSError, sqlite3.Error) as error:  # KpiError is a ValueError
        print(f"REFUSED: {error}")
        return 2
    print(f"WROTE {out.name} sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
