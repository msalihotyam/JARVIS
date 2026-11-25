"""CISHARDCOST1: offline model of the CI gate's fixed cost per shard, from shard reports.

Input: a directory of the JSON files ``scripts/test_parallel.py --shard I/N --report
shard-report.json`` writes in CI (artifacts ``ci-shard-N``, downloaded by the operator).
Fields used: ``shard`` [i, n] (null: unsharded run), ``wall`` (s), ``durations``
[[seconds, label], ...] (every test, since --report sets --durations 0), ``ran``.

Per shard: tests = sum(durations) / P, fixed = wall - tests. P (--concurrency, default 1)
is the spec's literal formula; a shard runs several workers at once, so with P=1 the test
sum can exceed wall and the record is flagged "inconsistent" (left out, counted).
build/conveyor/localci/*.json are whole-suite local runs without per-test times: not read.
Read-only, deterministic, no network, no clock. Usage:
    python scripts/ci_shard_cost.py DIR [--out PATH] [--max-files N] [--concurrency P]
"""

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

DEFAULT_OUT = "build/reports/CISHARDCOST1-model.md"
MAX_FILES = 5000
MIN_RECORDS = 5
MODEL_SHARDS = (16, 24, 32)
STEP = 8           # the CI matrix grows in steps of 8 shards
GAIN = 0.05        # stop when one more step saves under 5% of the gate
SEARCH_LIMIT = 1024
NM = "not measured"


class Refused(ValueError):
    """A report that cannot be trusted; counted, never raised past main."""


def _pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise Refused("duplicate key")
    return dict(pairs)


def _constant(name):
    raise Refused(f"{name} is not a number")


def _time(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Refused(f"{what} is not numeric")
    if not math.isfinite(value) or value < 0:
        raise Refused(f"{what} is negative or not finite")
    return float(value)


def parse(text: str) -> dict:
    """One report's text -> {shards, wall, tests_sum}; None means not measured. Raises Refused."""
    try:
        doc = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except Refused:
        raise
    except (ValueError, RecursionError) as exc:
        raise Refused(f"malformed JSON ({type(exc).__name__})") from None
    if not isinstance(doc, dict):
        raise Refused("top level is not an object")
    shards = None
    if "shard" in doc:
        shard = doc["shard"]
        if shard is None:
            shards = 1
        elif (isinstance(shard, list) and len(shard) == 2 and all(type(v) is int for v in shard)
              and 1 <= shard[0] <= shard[1]):
            shards = shard[1]
        else:
            raise Refused("shard is not [i, n]")
    wall = None if doc.get("wall") is None else _time(doc["wall"], "wall")
    tests_sum = None
    rows = doc.get("durations")
    if rows is not None:
        if not isinstance(rows, list):
            raise Refused("durations is not a list")
        for row in rows:
            if not (isinstance(row, list) and len(row) == 2 and isinstance(row[1], str)):
                raise Refused("a durations row is not [seconds, name]")
            _time(row[0], "a test duration")
        # an empty list is only a measurement when the shard ran nothing
        if rows or doc.get("ran") == 0:
            tests_sum = math.fsum(row[0] for row in rows)
    return {"shards": shards, "wall": wall, "tests_sum": tests_sum}


def collect(root: Path, max_files: int) -> tuple[list[Path], int, int]:
    """*.json under root, sorted, links and paths leaving root skipped: (files, found, unsafe)."""
    base = os.path.realpath(root)
    found, unsafe = [], 0
    for folder, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = sorted(d for d in dirs if ".." not in d and not os.path.islink(os.path.join(folder, d)))
        for name in names:
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(folder, name)
            real = os.path.realpath(path)
            if ".." in name or os.path.islink(path) or not _inside(base, real):
                unsafe += 1
                continue
            found.append(Path(path))
    found.sort(key=lambda p: p.relative_to(base).as_posix())
    return found[:max_files], len(found), unsafe


def _inside(base: str, real: str) -> bool:
    try:
        return os.path.commonpath([base, real]) == base
    except ValueError:  # another drive on Windows
        return False


def p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]  # nearest rank


def _fmt(value) -> str:
    return NM if value is None else f"{value:.1f}"


def gate(fixed: float, work: float, shards: int) -> float:
    return fixed + work / shards


def knee(fixed: float, work: float) -> int | None:
    """Smallest N (8, 16, ...) where going to N+8 shards saves under GAIN of gate(N)."""
    for n in range(STEP, SEARCH_LIMIT + 1, STEP):
        now = gate(fixed, work, n)
        if now <= 0 or (now - gate(fixed, work, n + STEP)) / now < GAIN:
            return n
    return None


def build_report(root: Path, max_files: int = MAX_FILES, concurrency: float = 1.0) -> str:
    files, found, unsafe = collect(root, max_files)
    lines = ["# CISHARDCOST1 shard fixed-cost model", "",
             f"input: {len(files)} of {found} JSON files read (cap --max-files {max_files}"
             + (f"; CAPPED, {found - len(files)} not read)" if found > len(files) else ")"),
             f"skipped (link, '..' or outside the folder): {unsafe}",
             f"formula: tests = sum(durations) / P, P = {concurrency:g}; fixed = wall - tests",
             "localci (build/conveyor/localci/*.json): not read, no per-test times", "",
             "| file | shards | wall s | tests s | fixed s | status |", "|---|---|---|---|---|---|"]
    refused = inconsistent = unmeasured = 0
    usable = []
    for path in files:
        name = path.relative_to(os.path.realpath(root)).as_posix()
        try:
            rec = parse(path.read_text(encoding="utf-8"))
        except (Refused, OSError, UnicodeDecodeError) as exc:
            refused += 1
            lines.append(f"| {name} | | | | | refused: {exc} |")
            continue
        tests = None if rec["tests_sum"] is None else rec["tests_sum"] / concurrency
        fixed = None if rec["wall"] is None or tests is None else rec["wall"] - tests
        status = "ok"
        if fixed is None:
            unmeasured += 1
            status = NM
        elif fixed < 0:
            inconsistent += 1
            status = "inconsistent"
        else:
            usable.append((fixed, tests, rec["shards"]))
        shards = NM if rec["shards"] is None else str(rec["shards"])
        lines.append(f"| {name} | {shards} | {_fmt(rec['wall'])} | {_fmt(tests)} | {_fmt(fixed)} | {status} |")
    lines += ["", f"usable: {len(usable)}; refused: {refused}; inconsistent: {inconsistent}; not measured: {unmeasured}"]
    if inconsistent and concurrency == 1:
        lines.append("note: shards run tests on several workers at once, so the test sum can exceed wall;"
                     " --concurrency P divides it (P = full packing, overstates fixed cost)")
    if len(usable) < MIN_RECORDS:
        lines.append(f"insufficient data: {len(usable)} usable records, {MIN_RECORDS} needed; no model")
        return "\n".join(lines) + "\n"
    fixed_values = [f for f, _, _ in usable]
    med, high = statistics.median(fixed_values), p90(fixed_values)
    lines.append(f"fixed s: median {med:.1f}, p90 {high:.1f}")
    runs = [t * n for _, t, n in usable if n is not None]
    if not runs:
        lines.append(f"model: {NM} (no record names its shard count)")
        return "\n".join(lines) + "\n"
    work = statistics.median(runs)
    lines += [f"test work per run W = median(tests x shard count) = {work:.1f} s",
              "gate(N) = fixed + W / N", "", "| shards | gate s (median fixed) | gate s (p90 fixed) |", "|---|---|---|"]
    lines += [f"| {n} | {gate(med, work, n):.1f} | {gate(high, work, n):.1f} |" for n in MODEL_SHARDS]
    stop = knee(med, work)
    lines += ["", f"stop rule (median fixed, step {STEP}): one more step gains under {GAIN:.0%} from "
              + (f"{stop} shards" if stop else f"no count up to {SEARCH_LIMIT}")]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--max-files", type=int, default=MAX_FILES)
    parser.add_argument("--concurrency", type=float, default=1.0)
    args = parser.parse_args(argv)
    root = Path(args.directory)
    if not root.is_dir() or args.max_files < 1 or not (math.isfinite(args.concurrency) and args.concurrency >= 1):
        print("error: DIR must be a folder, --max-files >= 1, --concurrency >= 1", file=sys.stderr)
        return 2
    text = build_report(root, args.max_files, args.concurrency)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
