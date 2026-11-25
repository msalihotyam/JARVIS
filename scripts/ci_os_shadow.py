"""CIOSSHADOW1: evidence for moving OS-independent test modules off the Windows gate.

The gate does not change here (that is Salih's decision on this evidence). The workflow's
non-blocking "os shadow" job runs the modules in dev/ci/os_independent.txt on Linux and uploads
its own report (artifact ci-os-shadow, never matching the gate's ci-shard-*); this tool reads it
beside the Windows shard reports of the same runs.

  scan [--write]       static check (CIDIAG1 D4 markers); --write regenerates the list file
  check                refuse a list with duplicates, path-like/non-module entries or flagged modules
  modules              the checked list, space separated (the shadow job's argv)
  compare RUN_DIR...   per module same / differs / skipped / missing and the time ratio, oldest run first
                       (consecutive = the order given); safe to move = same and passing in each
                       of the last N runs (--runs-needed, default 10); then the modelled gate time

A run dir holds that run's downloaded artifacts: any shard-report.json (+ shard-summary.txt beside
it) is the Windows side, os-shadow-report.json (+ os-shadow-summary.txt) the Linux side. Fail
closed: anything a side cannot account for makes its modules "unknown", which is never "same".
No network, no model calls, no database.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
LIST = ROOT / "dev" / "ci" / "os_independent.txt"
RUNS_NEEDED = 10

MARKERS = {  # CIDIAG1 D4, plus the platform gate (such a module may skip off Windows, not fail)
    # bare names, so `from ctypes import windll` and the like are caught, not only `ctypes.windll`
    "winapi": re.compile(r"(?i:\bwindll\b|\boledll\b)|\bwintypes\b|\bWINFUNCTYPE\b|\bWinError\b|\bGetLastError\b"
                         r"|\bFormatError\b|\bwinreg\b|\bmsvcrt\b|\bwin32\w*"
                         r"|\bpywintypes\b|\bcomtypes\b|\buiautomation\b"
                         r"|(?i:\b(?:kernel|user|advapi|shell|gdi|ole|ws2_)32\b)|\b_winapi\b|\bwinsound\b|\bstartfile\b"
                         r"|\b[gs]et_last_error\b|\bHRESULT\b"),
    "powershell": re.compile(r"powershell|\bpwsh\b|\.ps1\b", re.IGNORECASE),
    "qml_qt": re.compile(r"PySide6|\bQQml|\bQml|\.qml\b|\bjarvis\.ui\b"),
    "platform_gate": re.compile(r"sys\.platform|os\.name\s*[!=]=|platform\.system\(\)"
                                r"|skip(?:If|Unless)\([^)]*win", re.IGNORECASE),
}
_MODULE = re.compile(r"tests\.test_\w+")
_FAILED = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.MULTILINE)
_IN_NAME = re.compile(r"\((tests\.test_\w+)\.")
SIDES = {"shard-report.json": ("windows", "shard-summary.txt"),
         "os-shadow-report.json": ("linux", "os-shadow-summary.txt")}
RUN_ID = "run-id.txt"  # .txt, never .json: the gate's aggregate reads every *.json in the shard artifacts


class Refused(ValueError):
    """Bad input: the tool stops instead of guessing."""


def _ui_modules(names: list[str]) -> set[str]:
    """The runner's own transitive UI set (test_parallel.ui_modules), so both agree on what is UI."""
    spec = importlib.util.spec_from_file_location("ci_os_shadow_tp", ROOT / "scripts" / "test_parallel.py")
    tp = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(tp)  # type: ignore[union-attr]
    return set(tp.ui_modules(names))


def flags(text: str) -> list[str]:
    return [name for name, rx in MARKERS.items() if rx.search(text)]


def scan(tests: Path = TESTS) -> tuple[list[str], dict[str, list[str]]]:
    """(OS-independent modules, {flagged module: reasons}) over tests/test_*.py."""
    names = sorted(p.stem for p in tests.glob("test_*.py"))
    ui = _ui_modules(names) if tests == TESTS else set()
    flagged = {}
    for name in names:
        reasons = flags((tests / f"{name}.py").read_text(encoding="utf-8", errors="replace"))
        reasons += ["ui_transitive"] if name in ui and "qml_qt" not in reasons else []
        if reasons:
            flagged[f"tests.{name}"] = reasons
    return [f"tests.{n}" for n in names if f"tests.{n}" not in flagged], flagged


def read_list(path: Path = LIST, tests: Path = TESTS) -> list[str]:
    """The checked module list; Refused on a duplicate, a path-like or non-module entry, a missing
    module or one the static check flags."""
    if not path.is_file():
        raise Refused(f"{path}: no list file")
    modules, problems = [], []
    ui = _ui_modules(sorted(p.stem for p in tests.glob("test_*.py"))) if tests == TESTS else set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _MODULE.fullmatch(line):
            problems.append(f"line {number}: {line!r} is not a tests.test_* module name")
        elif line in modules:
            problems.append(f"line {number}: {line} is a duplicate")
        elif not (tests / f"{line[len('tests.'):]}.py").is_file():
            problems.append(f"line {number}: {line} does not exist")
        else:
            reasons = flags((tests / f"{line[len('tests.'):]}.py").read_text(encoding="utf-8", errors="replace"))
            reasons += ["ui_transitive"] if line[len("tests."):] in ui else []
            if reasons:
                problems.append(f"line {number}: {line} is flagged ({', '.join(reasons)})")
            modules.append(line)
    if problems:
        raise Refused("; ".join(problems))
    if not modules:
        raise Refused(f"{path}: the list is empty")
    return modules


def _module_of(test_id: str) -> str:
    return ".".join(test_id.split(".")[:2])


def read_report(path: Path) -> dict | None:
    """One report as {dispatched, failing, skipped, seconds, wall, shard}; None when it cannot be accounted
    for (unreadable, a test count that does not add up, a red run whose failures are not all named, or a
    skip whose id is neither a dispatched id nor a class/module prefix of one)."""
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        dispatched = list(report["dispatched"])
        if not report["accounted"] or report["ran"] != len(dispatched):
            return None
        failing: set[str] = set()
        if not report["green"]:
            summary = path.with_name(SIDES[path.name][1])
            text = summary.read_text(encoding="utf-8-sig", errors="replace") if summary.is_file() else ""
            named = _FAILED.findall(text)
            for name, where in named:
                failing.add(name if name.startswith("tests.") else f"tests.{name}"
                            if where.startswith("unittest.loader._FailedTest") else _module_of(where))
            if not failing or len(named) < int(report["failures"]) + int(report["errors"]):
                return None  # red, but which modules failed is not in the (possibly truncated) summary
        skips = report["skips"]  # [[id, reason]]; a skipped test never ran, so its module is not "pass"
        if not isinstance(skips, list) or len(skips) != int(report["skipped"]):
            return None
        # a skip id must be a dispatched id or a dot-boundary class/module prefix of one (setUpClass names
        # its class); any other id hides which dispatched test did not run, so no module can be passing
        known = {".".join(parts[:i]) for t in dispatched for parts in [str(t).split(".")]
                 for i in range(2, len(parts) + 1)}
        skipped = set()
        for test, _reason in skips:
            if not isinstance(test, str) or not _MODULE.match(test) or test not in known:
                return None
            skipped.add(_module_of(test))
        seconds: dict[str, float] = {}
        for value, name in report["durations"]:
            match = _IN_NAME.search(name)
            if match:
                seconds[match.group(1)] = seconds.get(match.group(1), 0.0) + float(value)
        return {"dispatched": {_module_of(t) for t in dispatched}, "ids": set(dispatched), "failing": failing,
                "skipped": skipped, "seconds": seconds, "wall": float(report["wall"]), "shard": report["shard"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def run_id(run_dir: Path) -> str | None:
    """The one CI run id written beside every report in run_dir (tests.yml: run-id.txt holds
    github.run_id); None with no report. Refused when a report has none or the dir mixes two runs."""
    found = set()
    for report in (p for name in SIDES for p in sorted(run_dir.rglob(name))):
        beside = report.with_name(RUN_ID)
        value = beside.read_text(encoding="utf-8-sig", errors="replace").strip() if beside.is_file() else ""
        if not value:
            raise Refused(f"compare: {report} has no {RUN_ID} beside it (its CI run is unknown)")
        found.add(value)
    if len(found) > 1:
        raise Refused(f"compare: {run_dir} mixes the artifacts of CI runs {', '.join(sorted(found))}")
    return found.pop() if found else None


def read_run(run_dir: Path, modules: list[str]) -> dict:
    """{side: {"results": {module: (pass|fail|skipped|missing|unknown, seconds, test ids)},
    "walls": [(wall, {module: s})]}}.
    A side is unknown when a report is unreadable, a Windows shard is absent or doubled (a module's
    classes can sit in several shards), or there is more than one Linux report."""
    out = {}
    for report_name, (side, _summary) in SIDES.items():
        reports = [read_report(p) for p in sorted(run_dir.rglob(report_name))]
        good = [r for r in reports if r is not None]
        if side == "windows":
            shards = [tuple(r["shard"]) if isinstance(r["shard"], list) and len(r["shard"]) == 2 else (0, 0)
                      for r in good]
            total = shards[0][1] if shards else 0
            complete = not good or sorted(shards) == [(i, total) for i in range(1, total + 1)]
        else:
            complete = len(good) <= 1
        broken = len(good) < len(reports) or not complete
        results = {}
        for module in modules:
            seconds = sum(r["seconds"].get(module, 0.0) for r in good)
            ids = frozenset(t for r in good for t in r["ids"] if _module_of(t) == module)
            if broken:
                status = "unknown"
            elif not any(module in r["dispatched"] for r in good):
                status = "missing"
            else:
                # a skip beats fail too, so a module with skipped tests is never "same" (even fail/fail)
                status = ("skipped" if any(module in r["skipped"] for r in good)
                          else "fail" if any(module in r["failing"] for r in good) else "pass")
            results[module] = (status, seconds, ids)
        out[side] = {"results": results, "walls": [(r["wall"], r["seconds"]) for r in good]}
    return out


def verdict(windows: str, linux: str) -> str:
    if "missing" in (windows, linux):
        return "missing"
    if "unknown" in (windows, linux):
        return "unknown"
    if "skipped" in (windows, linux):
        return "skipped"  # tests that did not run on a side cannot show the same result
    return "same" if windows == linux else "differs"


def compare(runs: list[dict], modules: list[str], runs_needed: int = RUNS_NEEDED) -> dict:
    """Per module: the verdict of each run, the median time ratio linux/windows, and safe or not.
    safe is None (not decided) with fewer than runs_needed runs."""
    rows = {}
    for module in modules:
        verdicts, ratios = [], []
        for run in runs:
            w_status, w_sec, w_ids = run["windows"]["results"][module]
            l_status, l_sec, l_ids = run["linux"]["results"][module]
            v = verdict(w_status, l_status)
            verdicts.append(("differs" if v == "same" and w_ids != l_ids else v, w_status))  # other tests ran
            if w_sec > 0 and l_sec > 0:
                ratios.append(l_sec / w_sec)
        last = verdicts[-runs_needed:]
        safe = (None if len(runs) < runs_needed
                else all(v == "same" and status == "pass" for v, status in last))
        rows[module] = {"verdicts": [v for v, _ in verdicts],
                        "ratio": statistics.median(ratios) if ratios else None, "safe": safe}
    return rows


def modelled_gate(runs: list[dict], safe: set[str]) -> tuple[float, float] | None:
    """(median gate time now, median gate time with the safe set on Linux): the slowest Windows shard
    without the safe modules' seconds, against the Linux job's own wall. None without Windows walls.
    shortcut: the safe seconds are summed over a shard's parallel workers, so the saving is an upper
    bound (optimistic gate time); model per worker if the decision needs a tighter number."""
    now, moved = [], []
    for run in runs:
        walls = run["windows"]["walls"]
        if not walls:
            continue
        now.append(max(wall for wall, _ in walls))
        rest = max(max(wall - sum(s for m, s in secs.items() if m in safe), 0.0) for wall, secs in walls)
        linux = max((wall for wall, _ in run["linux"]["walls"]), default=0.0)
        moved.append(max(rest, linux) if safe else now[-1])
    return (statistics.median(now), statistics.median(moved)) if now else None


def write_list(modules: list[str], path: Path = LIST) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# CIOSSHADOW1: test modules with no Windows marker (CIDIAG1 D4 + platform gate + the runner's\n"
              "# transitive UI set), from `python scripts/ci_os_shadow.py scan --write`. One module per line.\n")
    path.write_text(header + "".join(f"{m}\n" for m in modules), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan").add_argument("--write", action="store_true")
    sub.add_parser("check")
    sub.add_parser("modules")
    cmp = sub.add_parser("compare")
    cmp.add_argument("run_dirs", nargs="+", type=Path)
    cmp.add_argument("--runs-needed", type=int, default=RUNS_NEEDED)
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            modules, flagged = scan()
            print(f"{len(modules)} OS-independent, {len(flagged)} flagged")
            for module, reasons in sorted(flagged.items()):
                print(f"  flagged {module}: {', '.join(reasons)}")
            if args.write:
                write_list(modules)
            return 0
        modules = read_list()
        if args.command == "check":
            print(f"{LIST.relative_to(ROOT).as_posix()}: {len(modules)} modules, OK")
            return 0
        if args.command == "modules":
            print(" ".join(modules))
            return 0
        if args.runs_needed < 1 or any(not d.is_dir() for d in args.run_dirs):
            raise Refused("compare needs existing run dirs and --runs-needed >= 1")
        if len({d.resolve() for d in args.run_dirs}) < len(args.run_dirs):
            raise Refused("compare: a run dir is given twice (one run would count as several)")
        copies = [p.read_bytes() for d in args.run_dirs for name in SIDES for p in d.rglob(name)]
        if len(set(copies)) < len(copies):
            raise Refused("compare: the same report is in two places (one run would count as several)")
        ids = [i for i in (run_id(d) for d in args.run_dirs) if i is not None]
        if len(set(ids)) < len(ids):
            raise Refused("compare: two run dirs hold the same CI run (one run would count as several)")
        runs = [read_run(d, modules) for d in args.run_dirs]
        rows = compare(runs, modules, args.runs_needed)
        for module, row in rows.items():
            ratio = "n/a" if row["ratio"] is None else f"{row['ratio']:.2f}"
            state = {None: "not enough runs", True: "safe", False: "not safe"}[row["safe"]]
            print(f"{module}: last {row['verdicts'][-1]}  linux/windows {ratio}  {state}")
        if len(runs) < args.runs_needed:
            print(f"not enough runs: {len(runs)} of {args.runs_needed}; no safe set")
            return 0
        safe = {m for m, row in rows.items() if row["safe"]}
        print(f"safe to move: {len(safe)} of {len(modules)} (same and passing in the last {args.runs_needed} runs)")
        gate = modelled_gate(runs, safe)
        if gate is None:
            print("modelled gate time: no Windows shard reports")
        else:
            print(f"modelled gate time: {gate[0]:.0f}s now, {gate[1]:.0f}s with the safe set on Linux")
        return 0
    except Refused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
