# pyright: strict
"""N6-07: the E8 side-by-side report over the hash-pinned M9 acceptance set (R28, R33).

``report(rows, acceptance_set)`` pairs the "n6" and "conveyor" KPI rows of each slice listed in
the pinned set (same-class comparison, M10) and prints E8 (a)-(e) plus security incidents side
by side. ADVANCE needs every measure equal or better, false PASS 0 and n6 security 0; anything
else, an empty or incompletely paired set, is DO-NOT-ADVANCE with its reasons. A set whose
bytes do not match ``SHA256SUMS`` beside it, or that is malformed, is refused (exit 2). Rows
for slices outside the set are listed and never counted. No N6 code decides the verdict: this
file imports only ``jarvis.n6_kpi`` (the row type) and the stdlib. Exit: 0 ADVANCE,
1 DO-NOT-ADVANCE, 2 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # CLI: make ``jarvis`` importable
from jarvis.n6_kpi import KpiRow, rows as read_rows  # noqa: E402

SET_NAME = "acceptance_set.json"
SUMS_NAME = "SHA256SUMS"
MEASURES = {
    "pass_rate": "higher",
    "post_accept_defects": "lower",
    "salih_interventions": "lower",
    "quota_tokens_per_accepted": "lower",
    "finish_seconds": "lower",
}
LIMITS = {"false_pass": 0, "security_incidents": 0, "rollback_drill_required": True}
_KEYS = {"version", "limits", "measures", "slice_ids"}
_HEX = re.compile(r"[0-9a-f]{64}")
ADVANCE = "VERDICT: ADVANCE"
NO_ADVANCE = "VERDICT: DO-NOT-ADVANCE"


class SetError(ValueError):
    """The acceptance set does not match its pinned hash or is malformed."""


def _exact(value: object, expected: object) -> bool:
    # bool is an int subclass: True must not pass as 1, nor 0 as False.
    return type(value) is type(expected) and value == expected


def load_set(acceptance_set: Path) -> tuple[str, tuple[str, ...]]:
    """(pinned sha256, slice ids) of a verified set; raises SetError otherwise."""
    folder = acceptance_set.parent
    try:
        data = acceptance_set.read_bytes()
        sums = (folder / SUMS_NAME).read_text(encoding="ascii").split()
    except (OSError, UnicodeDecodeError) as error:
        raise SetError(f"cannot read the acceptance set: {error}") from error
    if len(sums) != 2 or not _HEX.fullmatch(sums[0]) or sums[1] not in (SET_NAME, "*" + SET_NAME):
        raise SetError(f"{SUMS_NAME} must hold exactly one '<sha256>  {SET_NAME}' line")
    pinned, actual = sums[0], hashlib.sha256(data).hexdigest()
    if acceptance_set.name != SET_NAME or actual != pinned:
        raise SetError(f"hash mismatch: pinned {pinned}, actual {actual}")
    try:
        loaded: object = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SetError(f"malformed set: {error}") from error
    if not isinstance(loaded, dict) or set(cast(dict[str, object], loaded)) != _KEYS:
        raise SetError(f"malformed set: top-level keys must be exactly {sorted(_KEYS)}")
    doc = cast(dict[str, object], loaded)
    if not _exact(doc["version"], 1):
        raise SetError("malformed set: version must be the integer 1")
    limits, measures, ids = doc["limits"], doc["measures"], doc["slice_ids"]
    if not isinstance(limits, dict) or len(cast(dict[str, object], limits)) != len(LIMITS) or not all(
            _exact(cast(dict[str, object], limits).get(k), v) for k, v in LIMITS.items()):
        raise SetError(f"malformed set: limits must be exactly {LIMITS}")
    if measures != MEASURES:
        raise SetError(f"malformed set: measures must be exactly {MEASURES}")
    if (not isinstance(ids, list) or not all(isinstance(i, str) and i for i in cast(list[object], ids))
            or len(set(cast(list[str], ids))) != len(cast(list[str], ids))):
        raise SetError("malformed set: slice_ids must be a list of unique non-empty strings")
    return pinned, tuple(cast(list[str], ids))


def _measures(side: list[KpiRow]) -> dict[str, float]:
    accepted = sum(1 for r in side if r.passed)
    quota = sum(r.quota_tokens for r in side)
    return {
        "pass_rate": accepted / len(side),
        "post_accept_defects": float(sum(r.post_accept_defects for r in side)),
        "salih_interventions": float(sum(r.salih_interventions for r in side)),
        # No accepted slice: quota per accepted slice is unbounded, never 0 (R32).
        "quota_tokens_per_accepted": quota / accepted if accepted else math.inf,
        "finish_seconds": float(sum(r.finish_seconds for r in side)),
    }


def report(rows: Iterable[KpiRow], acceptance_set: Path) -> tuple[int, str]:
    """(exit code, report text) for the rows over the pinned set at ``acceptance_set``."""
    try:
        pinned, slice_ids = load_set(acceptance_set)
    except SetError as error:
        return 2, f"REFUSED: {error}\n{NO_ADVANCE}\n"
    by_slice: dict[str, dict[str, KpiRow]] = {}
    outside: list[str] = []
    for row in rows:
        if row.slice_id not in slice_ids:
            outside.append(f"{row.slice_id}/{row.source}")
            continue
        if row.source in by_slice.setdefault(row.slice_id, {}):
            return 2, f"REFUSED: duplicate row {row.slice_id}/{row.source}\n{NO_ADVANCE}\n"
        by_slice[row.slice_id][row.source] = row
    paired = [s for s in slice_ids if len(by_slice.get(s, {})) == 2]
    unpaired = [s for s in slice_ids if s not in paired]
    lines = [f"PINNED SHA256: {pinned}", f"SET SLICES: {len(slice_ids)}",
             f"PAIRED: {len(paired)} {' '.join(paired)}".rstrip(),
             f"UNPAIRED (not counted): {' '.join(unpaired) or 'none'}",
             f"OUTSIDE SET (not counted): {' '.join(outside) or 'none'}"]
    reasons = [f"set slice {s} lacks an n6 or conveyor row" for s in unpaired]
    if not paired:
        reasons.append("no paired slices")
    else:
        n6 = [by_slice[s]["n6"] for s in paired]
        conveyor = [by_slice[s]["conveyor"] for s in paired]
        ours, base = _measures(n6), _measures(conveyor)
        lines.append(f"{'MEASURE':<28}{'N6':>16}{'CONVEYOR':>16}  BETTER")
        for name, better in MEASURES.items():
            ok = ours[name] >= base[name] if better == "higher" else ours[name] <= base[name]
            ok = ok and math.isfinite(ours[name])  # n6 with no accepted slice never ties
            lines.append(f"{name:<28}{ours[name]:>16.4g}{base[name]:>16.4g}  {better}"
                         f"{'' if ok else '  WORSE'}")
            if not ok:
                reasons.append(f"{name} is worse: n6 {ours[name]:.4g} vs conveyor {base[name]:.4g}")
        false_pass = [r.slice_id for r in n6 if r.passed and r.post_accept_defects > 0]
        security = sum(r.security_incidents for r in n6)
        lines.append(f"{'security_incidents':<28}{security:>16}"
                     f"{sum(r.security_incidents for r in conveyor):>16}  must be 0 (n6)")
        lines.append(f"FALSE PASS (n6): {len(false_pass)} {' '.join(false_pass)}".rstrip())
        if false_pass:
            reasons.append(f"false PASS {len(false_pass)}: {' '.join(false_pass)}")
        if security:
            reasons.append(f"n6 security incidents {security}")
    lines.append("ROLLBACK DRILL: required by M9, confirmed by the judges, not by this report")
    lines += [f"REASON: {r}" for r in reasons]
    lines.append(NO_ADVANCE if reasons else ADVANCE)
    return (1 if reasons else 0), "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E8 side-by-side report over the pinned M9 set")
    parser.add_argument("--db", required=True, type=Path, help="database holding n6_kpi_row")
    parser.add_argument("--set", dest="acceptance_set", type=Path,
                        default=Path(__file__).resolve().parents[1] / "dev" / "n6" / "acceptance" / SET_NAME)
    args = parser.parse_args(argv)
    db = cast(Path, args.db)
    if not db.is_file():  # sqlite3.connect would create an empty database
        print(f"REFUSED: no database at {db}\n{NO_ADVANCE}")
        return 2
    code, text = report(read_rows(db), cast(Path, args.acceptance_set))
    print(text, end="")
    return code


if __name__ == "__main__":
    sys.exit(main())
