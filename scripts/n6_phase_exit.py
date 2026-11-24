# pyright: strict
"""N6P2EXIT1: one read-only check of every Phase 2 exit criterion of dev/n6/N6_KIT.md.

Prints one line per criterion, PASS or NOT_YET with its evidence (FAIL for a false PASS or a
security incident), then OVERALL, which is PASS only when every line is PASS and FAIL when any
line is FAIL. Inputs: the kpi DB (n6_limited_event, N6EVT1, and n6_kpi_row), the N6BASE1
baseline.json pinned by baseline.json.sha256, backlog.json (slice tier), a test_parallel report
JSON as the CI record, and a fallback drill receipt. A missing reading is NOT_YET, never PASS.
A baseline whose hash does not match, a malformed input or an operator database is refused
(exit 2, nothing evaluated, nothing written). The only write is build/reports/n6_phase2_exit.md.
No network, no model, no wall clock. Exit: 0 PASS, 1 NOT_YET or FAIL, 2 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # CLI: make ``jarvis`` importable
from jarvis.n6_kpi import KpiRow, limited_events, rows as read_rows  # noqa: E402

PASS, NOT_YET, FAIL = "PASS", "NOT_YET", "FAIL"
MIN_SLICES = 10
GROUP = "code/claude"
TWO_MANAGER = ("tests.test_n6_lease.", ".test_neg_two_managers_exactly_one_wins")
REPORT = Path("build") / "reports" / "n6_phase2_exit.md"
_HEX = re.compile(r"[0-9a-f]{64}")
E8 = (  # key, line, better, the formula printed as evidence
    ("a", "E8a_PASS_RATE", "higher", "accepted/n vs baseline pass_rate"),
    ("b", "E8b_POST_ACCEPT", "lower", "defects/accepted vs post_accept_failures/kpi_rows_measured"),
    ("c", "E8c_INTERVENTIONS", "lower", "interventions/n vs interventions/interventions_n"),
    ("d", "E8d_QUOTA", "lower", "quota/accepted vs quota_per_accepted"),
    ("e", "E8e_FINISH", "lower", "median finish_seconds vs 1.10 x finish_seconds_median"),
)


class Refused(ValueError):
    """An input that must not be evaluated: hash mismatch, malformed, operator data."""


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise Refused(f"cannot read {path}: {error}") from error


def _num(value: object) -> float | None:
    # bool is an int subclass and "missing" is a reading: neither is a number here.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _ratio(top: object, bottom: object) -> float | None:
    a, b = _num(top), _num(bottom)
    return a / b if a is not None and b else None


def load_baseline(path: Path, group: str) -> dict[str, Any] | None:
    """The group of a hash-verified N6BASE1 baseline; None when no baseline exists yet."""
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
        sums = path.with_name(path.name + ".sha256").read_text(encoding="ascii").split()
    except (OSError, UnicodeDecodeError) as error:
        raise Refused(f"baseline hash file unreadable: {error}") from error
    if len(sums) != 2 or not _HEX.fullmatch(sums[0]) or sums[1] != path.name:
        raise Refused(f"{path.name}.sha256 must hold exactly '<sha256>  {path.name}'")
    actual = hashlib.sha256(data).hexdigest()
    if actual != sums[0]:
        raise Refused(f"baseline hash mismatch: pinned {sums[0]}, actual {actual}")
    try:
        doc: object = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise Refused(f"malformed baseline: {error}") from error
    groups = cast(dict[str, Any], doc).get("groups") if isinstance(doc, dict) else None
    if not isinstance(groups, dict) or not isinstance(cast(dict[str, Any], groups).get(group), dict):
        raise Refused(f"baseline has no group {group!r}")
    return cast(dict[str, Any], groups[group])


def baseline_rates(group: dict[str, Any] | None) -> tuple[dict[str, float | None], str]:
    """Per-slice baseline rates comparable with N6's, and why there are none."""
    if group is None:
        return {}, "no baseline.json"
    if group.get("small_sample") is not False:
        return {}, f"baseline small_sample (n={group.get('n')!r})"
    finish = _num(group.get("finish_seconds_median"))
    return {"a": _num(group.get("pass_rate")),
            "b": _ratio(group.get("post_accept_failures"), group.get("kpi_rows_measured")),
            "c": _ratio(group.get("interventions"), group.get("interventions_n")),
            "d": _num(group.get("quota_per_accepted")),
            "e": None if finish is None else finish * 1.10}, ""


def n6_rates(side: list[KpiRow]) -> dict[str, float]:
    accepted = [r for r in side if r.passed]
    per_accepted = (lambda total: total / len(accepted)) if accepted else (lambda total: math.inf)
    return {"a": len(accepted) / len(side),
            "b": per_accepted(sum(r.post_accept_defects for r in accepted)),
            "c": sum(r.salih_interventions for r in side) / len(side),
            "d": per_accepted(sum(r.quota_tokens for r in accepted)),
            "e": statistics.median(r.finish_seconds for r in side)}


def tiers(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    doc = _json(path)
    slices = cast(dict[str, Any], doc).get("slices") if isinstance(doc, dict) else None
    if not isinstance(slices, list):
        raise Refused(f"{path.name} has no slices list")
    return {s["id"]: s.get("tier") for s in cast(list[Any], slices)
            if isinstance(s, dict) and isinstance(cast(dict[str, Any], s).get("id"), str)}


def two_manager(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return NOT_YET, f"no CI record at {path}"
    rec = _json(path)
    if not isinstance(rec, dict):
        raise Refused(f"CI record {path} is not a test_parallel report object")
    rec = cast(dict[str, Any], rec)
    # "dispatched" may hold class-level units; "durations" ([seconds, test id]) is per test.
    ids = [*(rec.get("dispatched") or []),
           *(d[1] for d in rec.get("durations") or [] if isinstance(d, list) and len(d) == 2)]
    ran = sorted({t for t in ids if isinstance(t, str)
                  and t.startswith(TWO_MANAGER[0]) and t.endswith(TWO_MANAGER[1])})
    skipped = {s[0] for s in rec.get("skips") or [] if isinstance(s, list) and s}
    if not ran:
        return NOT_YET, f"two-manager test not in the CI record {path.name}"
    if any(t in skipped for t in ran):
        return NOT_YET, f"{ran[0]} was skipped in the CI record"
    green = (rec.get("green") is True and rec.get("accounted") is True
             and type(rec.get("failures")) is int and rec.get("failures") == 0
             and type(rec.get("errors")) is int and rec.get("errors") == 0)
    if not green:
        return NOT_YET, (f"CI record not green (green={rec.get('green')!r}, failures="
                         f"{rec.get('failures')!r}, errors={rec.get('errors')!r})")
    return PASS, f"{ran[0]} ran in a green CI record ({path.name})"


def drill(path: Path, root: Path) -> tuple[str, str]:
    """Only a receipt of a live drill on this checkout counts; a temp-copy one is a rehearsal."""
    if not path.is_file():
        return NOT_YET, f"no fallback drill receipt at {path}"
    rec = _json(path)
    if not isinstance(rec, dict):
        raise Refused(f"drill receipt {path} is not an object")
    rec = cast(dict[str, Any], rec)
    where = rec.get("root")
    live = (rec.get("kind") == "live" and isinstance(where, str) and bool(where)
            and Path(where).resolve() == root.resolve())
    if not live:
        return NOT_YET, (f"rehearsal receipt (kind={rec.get('kind')!r}, root={where!r}),"
                         " not counted as the live drill")
    lost, doubled = rec.get("lost"), rec.get("doubled")
    if not (type(lost) is int and lost == 0 and type(doubled) is int and doubled == 0):
        return NOT_YET, f"live drill lost={lost!r} doubled={doubled!r}, need exactly 0 and 0"
    return PASS, f"live drill on {root} (slice {rec.get('slice_id')!r}): lost 0, doubled 0"


def evaluate(root: Path, db: Path, baseline: Path, group: str, backlog: Path,
             ci_record: Path, drill_receipt: Path) -> list[tuple[str, str, str]]:
    """(criterion, status, evidence) per Phase 2 exit line; raises Refused before reading."""
    resolved = db.resolve()
    if resolved.name == "jarvis.db" or (root / "data").resolve() in resolved.parents:
        raise Refused(f"{db} is operator data, never opened")
    base = load_baseline(baseline, group)
    tier = tiers(backlog)
    try:  # absent db: no readings; sqlite3.connect would create it
        events = limited_events(db) if db.is_file() else ()
        n6 = list(read_rows(db, source="n6")) if db.is_file() else []
    except (sqlite3.Error, ValueError) as error:  # KpiError is a ValueError
        raise Refused(f"unreadable kpi db {db}: {error}") from error
    ids = sorted({e.slice_id for e in events})
    counted = [s for s in ids if tier.get(s) == "T1"]  # unknown tier is not counted
    other = [s for s in ids if s not in counted]
    lines = [("SLICES", PASS if len(counted) >= MIN_SLICES else NOT_YET,
              f"{len(counted)} N6-run T1 slices with an n6_limited record (need {MIN_SLICES}):"
              f" {' '.join(counted) or 'none'}; not T1 or tier unknown: {' '.join(other) or 'none'}")]
    live = {r.slice_id: r for r in n6 if r.origin == "live"}
    gaps = [s for s in counted if s not in live]
    missing = (["no counted N6 slice"] if not counted else []) + (
        [f"no live n6 kpi row for {' '.join(gaps)}"] if gaps else [])
    rates, why = baseline_rates(base)
    ours = n6_rates([live[s] for s in counted]) if not missing else {}
    for key, name, better, formula in E8:
        theirs = rates.get(key)
        if missing or why or theirs is None:
            reasons = missing + ([why] if why else []) + (
                [f"baseline {group} reading missing"] if not why and theirs is None else [])
            lines.append((name, NOT_YET, f"{formula}: {'; '.join(reasons)}"))
            continue
        mine = ours[key]
        ok = math.isfinite(mine) and (mine >= theirs if better == "higher" else mine <= theirs)
        lines.append((name, PASS if ok else NOT_YET,
                      f"{formula}: n6 {mine:.4g} vs baseline {group} {theirs:.4g}"
                      f" ({better} is better, n={len(counted)})"))
    # Any n6 row of any origin: one false PASS or one incident fails the phase.
    for name, bad in (("FALSE_PASS", [r.slice_id for r in n6 if r.passed and r.post_accept_defects > 0]),
                      ("SECURITY", [r.slice_id for r in n6 if r.security_incidents > 0])):
        status = FAIL if bad else NOT_YET if missing else PASS
        lines.append((name, status, f"{len(bad)} over {len(n6)} n6 rows: {' '.join(bad) or 'none'}"
                      + (f"; {'; '.join(missing)}" if missing and not bad else "")))
    lines.append(("TWO_MANAGER", *two_manager(ci_record)))
    lines.append(("FALLBACK_DRILL", *drill(drill_receipt, root)))
    return lines


def overall(lines: list[tuple[str, str, str]]) -> str:
    statuses = [status for _, status, _ in lines]
    return FAIL if FAIL in statuses else PASS if statuses and all(s == PASS for s in statuses) else NOT_YET


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    n6 = root / "build" / "n6"
    parser = argparse.ArgumentParser(description="N6 Phase 2 exit check (read-only)")
    parser.add_argument("--db", type=Path, default=n6 / "kpi.db")
    parser.add_argument("--baseline", type=Path, default=n6 / "baseline.json")
    parser.add_argument("--group", default=GROUP, help="N6BASE1 class to compare with")
    parser.add_argument("--backlog", type=Path, default=root / "build" / "conveyor" / "backlog.json")
    parser.add_argument("--ci-record", type=Path, default=n6 / "ci-report.json",
                        help="a test_parallel report JSON")
    parser.add_argument("--drill", type=Path, default=n6 / "fallback_drill.json")
    args = parser.parse_args(argv)
    try:
        lines = evaluate(root, cast(Path, args.db), cast(Path, args.baseline), cast(str, args.group),
                         cast(Path, args.backlog), cast(Path, args.ci_record), cast(Path, args.drill))
    except Refused as error:
        print(f"REFUSED: {error}")
        return 2
    verdict = overall(lines)
    text = "".join(f"{n}: {s} | {e}\n" for n, s, e in lines) + f"OVERALL: {verdict}\n"
    print(text, end="")
    out = root / REPORT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return 0 if verdict == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
