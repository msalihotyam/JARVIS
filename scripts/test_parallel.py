"""Parallel runner for the JARVIS test suite (stdlib only).

Runs the SAME tests as ``python -m unittest discover -s tests`` as several
``python -m unittest -v <names>`` processes and prints one combined summary.
The serial run stays the reference when a result is disputed.

* Discovery first, in its own process: every module's TestCase classes and
  their test ids, checked against the serial ``discover -s tests`` set (the
  same ids, the same number of times) before anything runs.
* Work units are modules; a module whose recorded duration is large is split
  at TestCase level into duration-balanced chunks (``scripts/test_durations.json``,
  ``--write-durations``). A module with ``load_tests`` is never split.
* Units that drive a real QML window or launch the app (modules importing
  PySide6 / jarvis.ui, directly or through another test module) run over K UI
  processes (default ``min(4, cpu_count // 3)``, at least 1); the rest over N
  workers (default ``min(cpu_count - 1, 6)``); longest first. Every process
  has its own TEMP / TMP / TMPDIR and, being a process, its own QApplication.
  ``tests/__init__.py`` (the live-database guard) is imported by every
  ``tests.*`` module, so the guard holds in every process.
* Exit code 0 only when every unit passed AND the union of dispatched test ids
  equals the discovered set exactly AND every process ran exactly the tests it
  was given (its ``Ran N`` count and every id in its ``-v`` output). A failure
  that is ONLY on the load-sensitive list in JARVIS_ACTIVE_STATE.md is rerun
  alone once; if all of those pass, the run counts as green and both results
  are printed (rule D).
* CI: ``--shard I/N`` runs shard I of a deterministic plan (the recorded
  durations, longest first onto the least-loaded shard; units without a
  recorded duration last, each onto the least-loaded shard) and ``--report``
  writes what it dispatched and ran; ``--aggregate DIR --shards N`` checks all
  the shard reports against a fresh discovery and the same plan and prints one
  combined summary.

    python scripts/test_parallel.py [--workers N] [--ui-workers K] [--durations N] [--no-rerun]
                                    [--python PATH] [--shard I/N] [--report PATH]
                                    [--write-durations PATH] [modules ...]
    python scripts/test_parallel.py --aggregate DIR --shards N [--durations N]
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
STATE = ROOT / "JARVIS_ACTIVE_STATE.md"
DURATIONS = Path(__file__).resolve().with_name("test_durations.json")

#: A module recorded above this many seconds is split into chunks of about this size.
CHUNK_SECONDS = 60.0

_UI_MARKERS = re.compile(r"^\s*(?:from|import)\s+(?:PySide6|jarvis\.ui)\b", re.MULTILINE)
_TEST_IMPORT = re.compile(r"^\s*(?:from|import)\s+tests\.(test_\w+)", re.MULTILINE)
_RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.MULTILINE)
_COUNTS = re.compile(r"^(?:OK|FAILED)(?: \(([^)]*)\))?\s*$", re.MULTILINE)
_FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.MULTILINE)
_DURATION = re.compile(r"^\s*(\d+\.\d+)s\s+(\S.*)$", re.MULTILINE)
_SEEN = re.compile(r"\b(\w+) \((tests\.[\w.]+)\)")


def discover(selected: list[str]) -> list[str]:
    names = sorted(p.stem for p in TESTS.glob("test_*.py"))
    if selected:
        wanted = {s.removeprefix("tests.") for s in selected}
        names = [n for n in names if n in wanted]
    return names


def ui_modules(names: list[str]) -> set[str]:
    """Modules that import PySide6 / jarvis.ui, directly or via another test module."""
    every = sorted(p.stem for p in TESTS.glob("test_*.py"))
    source = {n: (TESTS / f"{n}.py").read_text(encoding="utf-8", errors="replace") for n in every}
    ui = {n for n, text in source.items() if _UI_MARKERS.search(text)}
    changed = True
    while changed:
        changed = False
        for n, text in source.items():
            if n not in ui and any(m in ui for m in _TEST_IMPORT.findall(text)):
                ui.add(n)
                changed = True
    return ui & set(names)


def load_sensitive() -> set[str]:
    """Test ids (``Class.test_name``) the orchestrator listed as load-sensitive."""
    try:
        text = STATE.read_text(encoding="utf-8")
    except OSError:
        return set()
    flat = re.sub(r"\s+", " ", text)
    return {f"{m.group(1)}.{m.group(2)}" for m in re.finditer(r"test_ui\.(\w+)\.(test_\w+)", flat)}


# -- discovery (in its own process) ---------------------------------------------

def _discover_child(modules: list[str], serial: bool) -> dict:
    """``{module: {"split": bool, "classes": {attr: [ids]}}}`` as each worker loads it, and the
    serial ``discover -s tests`` id multiset when ``serial``. Runs in a child process."""
    import importlib
    import unittest

    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    loader = unittest.TestLoader()
    out: dict = {"modules": {}, "errors": []}

    def ids(suite) -> list[str]:
        found = []
        for item in suite:
            found.extend(ids(item) if isinstance(item, unittest.TestSuite) else [item.id()])
        return found

    for name in modules:
        try:
            module = importlib.import_module(f"tests.{name}")
        except Exception as exc:  # noqa: BLE001 - reported, the run is red
            out["errors"].append(f"tests.{name}: {type(exc).__name__}: {exc}")
            continue
        if hasattr(module, "load_tests"):
            out["modules"][name] = {"split": False, "classes": {"": ids(loader.loadTestsFromModule(module))}}
            continue
        classes = {}
        for attr in dir(module):                     # the loader's own order and filter
            obj = getattr(module, attr)
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase):
                found = ids(loader.loadTestsFromTestCase(obj))
                if found:
                    classes[attr] = found
        out["modules"][name] = {"split": True, "classes": classes}
    out["errors"].extend(str(e) for e in loader.errors)
    if serial:
        reference = unittest.TestLoader().discover("tests")
        out["serial"] = [i if i.startswith("tests.") else "tests." + i for i in ids(reference)]
    return out


def discover_tests(python: str, modules: list[str], serial: bool) -> dict:
    command = [python, str(Path(__file__).resolve()), "--_discover-child", "--_serial" if serial else "--_no-serial", *modules]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    base = Path(tempfile.mkdtemp(prefix="jarvis-test-discover-"))
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(base)
    try:
        done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    marker = done.stdout.rfind("\n@@DISCOVERY@@")
    if done.returncode != 0 or marker < 0:
        raise SystemExit(f"discovery failed (exit {done.returncode}):\n{done.stdout[-3000:]}{done.stderr[-3000:]}")
    return json.loads(done.stdout[marker + len("\n@@DISCOVERY@@"):])


# -- units and the plan -------------------------------------------------------------

class Unit:
    """One ``python -m unittest`` name list run as one process: a whole module or a chunk of its classes."""

    def __init__(self, module: str, attrs: list[str], ids: list[str], seconds: float | None, ui: bool):
        self.module, self.attrs, self.ids, self.seconds, self.ui = module, attrs, ids, seconds, ui

    @property
    def names(self) -> list[str]:
        return [f"tests.{self.module}"] if self.attrs == [""] else [f"tests.{self.module}.{a}" for a in self.attrs]

    @property
    def label(self) -> str:
        return self.names[0] if len(self.names) == 1 else f"tests.{self.module}[{self.attrs[0]}..{self.attrs[-1]}]"

    @property
    def weight(self) -> float:
        return self.seconds if self.seconds is not None else 0.0


def load_durations(path: Path = DURATIONS) -> dict[str, float]:
    try:
        return {k: float(v) for k, v in json.loads(path.read_text(encoding="utf-8"))["classes"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def build_units(discovery: dict, ui: set[str], durations: dict[str, float]) -> list[Unit]:
    """Whole modules, except a module recorded above CHUNK_SECONDS: its classes in chunks of
    about CHUNK_SECONDS each (consecutive, in the loader's order). A unit's seconds are the sum
    of its classes' recorded seconds; None when any class has no record."""
    units = []
    for module, info in sorted(discovery["modules"].items()):
        per: dict[str, float | None] = {}
        for attr, ids in info["classes"].items():
            known = [durations.get(c) for c in sorted({t.rsplit(".", 1)[0] for t in ids})]
            per[attr] = None if any(k is None for k in known) else sum(known)
        total = None if any(v is None for v in per.values()) else sum(per.values())
        if not info["split"] or total is None or total <= CHUNK_SECONDS or len(per) == 1:
            ids = [t for attr_ids in info["classes"].values() for t in attr_ids]
            units.append(Unit(module, [""], ids, total, module in ui))
            continue
        pieces = max(2, round(total / CHUNK_SECONDS))
        bins: list[list[str]] = [[] for _ in range(pieces)]
        loads = [0.0] * pieces
        for attr in sorted(per, key=lambda a: (-per[a], a)):          # longest first onto the least-loaded chunk
            at = loads.index(min(loads))
            bins[at].append(attr)
            loads[at] += per[attr]
        for chunk, load in zip(bins, loads):
            if chunk:
                chunk.sort()
                ids = [t for attr in chunk for t in info["classes"][attr]]
                units.append(Unit(module, chunk, ids, load, module in ui))
    return units


def shard_plan(units: list[Unit], shards: int) -> list[list[Unit]]:
    """Deterministic. UI units and the others are balanced separately (a shard runs them side
    by side): recorded units longest first (ties by name) onto the shard least loaded with that
    kind (ties: the lowest index); then the units without a record, by name, onto the least-loaded
    shard overall."""
    plan: list[list[Unit]] = [[] for _ in range(shards)]
    loads = {kind: [0.0] * shards for kind in (True, False)}
    for kind in (True, False):
        for unit in sorted((u for u in units if u.seconds is not None and u.ui is kind), key=lambda u: (-u.seconds, u.label)):
            at = loads[kind].index(min(loads[kind]))
            plan[at].append(unit)
            loads[kind][at] += unit.seconds
    for unit in sorted((u for u in units if u.seconds is None), key=lambda u: u.label):
        total = [loads[True][i] + loads[False][i] for i in range(shards)]
        at = total.index(min(total))
        plan[at].append(unit)
        loads[unit.ui][at] += CHUNK_SECONDS / 4
    return plan


def discovered_ids(discovery: dict) -> collections.Counter:
    return collections.Counter(t for info in discovery["modules"].values() for ids in info["classes"].values() for t in ids)


# -- running ------------------------------------------------------------------------

class Result:
    def __init__(self, unit, command, returncode, output, wall):
        self.unit, self.command, self.returncode, self.output, self.wall = unit, command, returncode, output, wall
        self.label = unit.label if unit is not None else "rerun"
        ran = _RAN.search(output)
        self.ran = int(ran.group(1)) if ran else 0
        counts = {}
        tail = _COUNTS.findall(output)
        for part in (tail[-1] if tail else "").split(","):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                counts[key] = int(value)
        self.failures, self.errors, self.skipped = (counts.get(k, 0) for k in ("failures", "errors", "skipped"))
        self.parsed = bool(ran) and bool(tail)
        # ("Class.test_name", "module") for every FAIL / ERROR line.
        self.failed = [(".".join(path.split(".")[-2:]), path.split(".")[-3] if path.count(".") >= 2 else "")
                       for path in (m.group(2) for m in _FAILED_TEST.finditer(output))]
        seen = {m.group(2) for m in _SEEN.finditer(output) if m.group(2).endswith("." + m.group(1))}
        # Exactly what it was given: the count, and every id it was given in its -v output.
        self.missing = sorted(set(unit.ids) - seen) if unit is not None else []
        self.accounted = unit is None or (self.parsed and self.ran == len(unit.ids) and not self.missing)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.parsed and not self.failures and not self.errors and self.accounted


def worker_env(index: int, bases: list[Path]) -> dict[str, str]:
    base = Path(tempfile.mkdtemp(prefix=f"jarvis-test-w{index}-"))
    bases.append(base)
    env = dict(os.environ)
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(base)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_unit(python: str, unit: Unit, env: dict[str, str], durations: int | None) -> Result:
    command = [python, "-m", "unittest", "-v", *unit.names]
    if durations is not None:
        command[4:4] = ["--durations", str(durations)]
    started = time.monotonic()
    done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return Result(unit, command, done.returncode, done.stdout + done.stderr, time.monotonic() - started)


def execute(args, units: list[Unit], ui_workers: int, workers: int) -> tuple[list[Result], dict[str, float], float, list[Path]]:
    durations = 0 if args.write_durations else (args.durations or None)
    bases: list[Path] = []
    results: list[Result] = []
    lock = threading.Lock()
    order = lambda u: (-(u.seconds if u.seconds is not None else CHUNK_SECONDS / 4), u.label)  # noqa: E731
    queues = {"ui": sorted((u for u in units if u.ui), key=order), "plain": sorted((u for u in units if not u.ui), key=order)}
    worker_wall: dict[str, float] = {}

    def worker(kind: str, index: int) -> None:
        env, began = worker_env(index, bases), time.monotonic()
        while True:
            with lock:
                if not queues[kind]:
                    break
                unit = queues[kind].pop(0)
            result = run_unit(args.python, unit, env, durations)
            with lock:
                results.append(result)
        worker_wall[f"{kind}{index}"] = time.monotonic() - began

    started = time.monotonic()
    plan = [("ui", i) for i in range(1, ui_workers + 1)] + [("plain", i) for i in range(ui_workers + 1, ui_workers + workers + 1)]
    with futures.ThreadPoolExecutor(len(plan)) as pool:
        for job in [pool.submit(worker, kind, i) for kind, i in plan]:
            job.result()
    return results, worker_wall, time.monotonic() - started, bases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modules", nargs="*", help="test modules (default: all of tests/)")
    parser.add_argument("--workers", type=int, default=0, help="non-UI workers (default min(cpu_count - 1, 6))")
    parser.add_argument("--ui-workers", type=int, default=0, help="UI processes at once (default min(4, cpu_count // 3), >= 1)")
    parser.add_argument("--durations", type=int, default=0, help="report the N slowest tests")
    parser.add_argument("--no-rerun", action="store_true", help="do not rerun load-sensitive failures alone")
    parser.add_argument("--python", default=sys.executable, help="interpreter for the workers")
    parser.add_argument("--shard", default="", help="I/N: run shard I of N of the deterministic plan (CI)")
    parser.add_argument("--report", default="", help="write a JSON report of what was dispatched and ran")
    parser.add_argument("--write-durations", default="", help="record every test class's seconds to this JSON file")
    parser.add_argument("--aggregate", default="", help="check and combine the shard reports in this directory")
    parser.add_argument("--shards", type=int, default=0, help="with --aggregate: the number of shards")
    parser.add_argument("--_discover-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_serial", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_no-serial", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # A test's output may hold any character; a console code page (cp1254) must never end the run.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    if args._discover_child:
        print("\n@@DISCOVERY@@" + json.dumps(_discover_child(args.modules, args._serial)), flush=True)
        return 0
    if args.aggregate:
        return aggregate(args)

    names = discover(args.modules)
    if not names:
        print("no test modules found", file=sys.stderr)
        return 2
    shard = None
    if args.shard:
        match = re.fullmatch(r"(\d+)/(\d+)", args.shard)
        if not match or not 1 <= int(match.group(1)) <= int(match.group(2)) or args.modules:
            print("--shard must be I/N with 1 <= I <= N, over the whole suite", file=sys.stderr)
            return 2
        shard = (int(match.group(1)), int(match.group(2)))

    cpus = os.cpu_count() or 2
    workers = max(1, args.workers or min(cpus - 1, 6))
    ui_workers = max(1, args.ui_workers or min(4, cpus // 3))
    began = time.monotonic()
    discovery = discover_tests(args.python, names, serial=not args.modules)
    if discovery["errors"]:
        print("discovery errors:\n  " + "\n  ".join(discovery["errors"]))
        return 1
    expected = discovered_ids(discovery)
    if not args.modules and collections.Counter(discovery["serial"]) != expected:
        _print_set_difference("the serial discover -s tests set", collections.Counter(discovery["serial"]), expected)
        return 1
    units = build_units(discovery, ui_modules(names), load_durations())
    if shard is not None:
        units = shard_plan(units, shard[1])[shard[0] - 1]
    # The union of dispatched ids equals the discovered set exactly (the whole suite, or this shard's plan).
    dispatched = collections.Counter(t for u in units for t in u.ids)
    if shard is None and dispatched != expected:
        _print_set_difference("the dispatched ids", dispatched, expected)
        return 1
    ui_count = sum(1 for u in units if u.ui)
    print(f"{len(names)} modules, {sum(expected.values())} tests discovered (serial set checked: "
          f"{'yes' if not args.modules else 'no, modules given'}) in {time.monotonic() - began:.0f}s"
          + (f"; shard {shard[0]}/{shard[1]}" if shard else ""), flush=True)
    print(f"{len(units)} units: {ui_count} UI over {ui_workers} processes, {len(units) - ui_count} other over {workers} workers;"
          f" {sum(dispatched.values())} tests dispatched", flush=True)

    results, worker_wall, wall, bases = execute(args, units, ui_workers, workers)
    try:
        ran = sum(r.ran for r in results)
        skipped = sum(r.skipped for r in results)
        failures = sum(r.failures for r in results)
        errors = sum(r.errors for r in results)
        broken = [r for r in results if not r.ok]
        print()
        for label, seconds in sorted(worker_wall.items(), key=lambda kv: (kv[0][0] != "u", int(re.sub(r"\D", "", kv[0])))):
            print(f"worker {label}: {seconds:.0f} s")
        print(f"Ran {ran} tests in {wall:.1f}s wall (skipped={skipped}, failures={failures}, errors={errors})")
        accounted = len(results) == len(units) and all(r.accounted for r in results) and ran == sum(dispatched.values())
        if not accounted:
            for r in results:
                if not r.accounted:
                    print(f"NOT ACCOUNTED: {r.label}: ran {r.ran} of {len(r.unit.ids)}"
                          + (f"; missing {', '.join(r.missing[:5])}{' ...' if len(r.missing) > 5 else ''}" if r.missing else ""))
            print(f"dispatched {sum(dispatched.values())} tests in {len(units)} units; {len(results)} units reported, {ran} ran")
        green = accounted and not broken
        rule_d = []
        if broken:
            green = _report_failures(broken, args, bases, rule_d) and accounted
        if args.durations:
            _print_durations(results, args.durations)
        if args.write_durations:
            _write_durations(results, Path(args.write_durations))
        if args.report:
            _write_report(Path(args.report), shard, units, results, wall, green, rule_d, discovery)
        print("OK" if green else "FAILED", flush=True)
        return 0 if green else 1
    finally:
        for base in bases:
            shutil.rmtree(base, ignore_errors=True)


def _print_set_difference(what: str, got: collections.Counter, expected: collections.Counter) -> None:
    extra, lost = got - expected, expected - got
    print(f"MISMATCH: {what} differ from the discovered set ({sum(got.values())} vs {sum(expected.values())});"
          f" {sum(extra.values())} extra, {sum(lost.values())} missing")
    for test in list(extra)[:10]:
        print(f"  extra:   {test}")
    for test in list(lost)[:10]:
        print(f"  missing: {test}")


def _report_failures(broken: list[Result], args, bases: list[Path], rule_d: list) -> bool:
    """Print every failure; apply rule D. True when the run counts as green."""
    allowed = load_sensitive()
    named = [test for r in broken for test in r.failed]
    only_known = (not args.no_rerun and bool(named) and all(r.parsed and r.accounted for r in broken)
                  and all(key in allowed and module == "test_ui" for key, module in named)
                  and sum(len(r.failed) for r in broken) == sum(r.failures + r.errors for r in broken))
    for result in broken:
        print(f"\n===== FAILED: {result.label} (exit {result.returncode}) {' '.join(result.command[2:])}")
        print(result.output[-6000:] if not result.parsed else _traces(result.output))
    if not only_known:
        return False
    print("\nOnly load-sensitive tests failed; rerunning each alone once (rule D):")
    green = True
    for key, _module in dict.fromkeys(named):
        command = [args.python, "-m", "unittest", f"tests.test_ui.{key}"]
        done = subprocess.run(command, cwd=ROOT, env=worker_env(99, bases), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        passed = done.returncode == 0
        rule_d.append({"test": key, "alone": "PASS" if passed else "FAIL"})
        print(f"  {key}: {'PASS' if passed else 'FAIL'} alone (first run failed)")
        if not passed:
            print(done.stdout + done.stderr)
        green &= passed
    print("rule D: " + ("every rerun passed - the run counts as green" if green else "a rerun failed - the run is red"))
    return green


def _traces(output: str) -> str:
    start = output.find("======")
    return output[start:] if start >= 0 else output[-4000:]


def _durations_of(results: list[Result]) -> list[tuple[float, str]]:
    return sorted(((float(m.group(1)), m.group(2)) for r in results for m in _DURATION.finditer(r.output)), reverse=True)


def _print_durations(results: list[Result], top: int) -> None:
    _print_rows(_durations_of(results), top)


def _print_rows(rows: list[tuple[float, str]], top: int) -> None:
    print(f"\nSlowest {top} tests (all workers):")
    for seconds, name in rows[:top]:
        print(f"  {seconds:8.3f}s  {name}")


def _write_durations(results: list[Result], path: Path) -> None:
    """Seconds per test class (``tests.module.Class``), averaged over the times it ran."""
    total: dict[str, float] = collections.defaultdict(float)
    for seconds, name in _durations_of(results):
        match = _SEEN.search(name)
        if match:
            total[match.group(2).rsplit(".", 1)[0]] += seconds
    runs: dict[str, int] = collections.Counter()
    for r in results:
        for cls in {t.rsplit(".", 1)[0] for t in r.unit.ids}:
            runs[cls] += 1
    classes = {cls: round(seconds / max(1, runs.get(cls, 1)), 3) for cls, seconds in sorted(total.items())}
    for r in results:                                       # classes too fast to be listed still get a record
        for cls in {t.rsplit(".", 1)[0] for t in r.unit.ids}:
            classes.setdefault(cls, 0.0)
    path.write_text(json.dumps({"version": 1, "about": "Seconds per test class from one local parallel run; "
                                "scripts/test_parallel.py plans its units and CI shards from it "
                                "(--write-durations regenerates it).", "classes": dict(sorted(classes.items()))},
                               indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(classes)} class durations to {path}")


def _write_report(path: Path, shard, units, results, wall, green, rule_d, discovery) -> None:
    path.write_text(json.dumps({
        "shard": list(shard) if shard else None,
        "green": green,
        "wall": round(wall, 1),
        "units": [u.label for u in units],
        "dispatched": [t for u in units for t in u.ids],
        "ran": sum(r.ran for r in results),
        "skipped": sum(r.skipped for r in results),
        "failures": sum(r.failures for r in results),
        "errors": sum(r.errors for r in results),
        "accounted": all(r.accounted for r in results) and len(results) == len(units),
        "rule_d": rule_d,
        "durations": _durations_of(results)[:200],
        "discovered": sum(discovered_ids(discovery).values()),
    }, ensure_ascii=False), encoding="utf-8")


def aggregate(args) -> int:
    """CI's final job: every shard's report against a fresh discovery and the same plan; one result."""
    directory = Path(args.aggregate)
    shards = args.shards
    names = discover([])
    discovery = discover_tests(args.python, names, serial=True)
    expected = discovered_ids(discovery)
    units = build_units(discovery, ui_modules(names), load_durations())
    plan = shard_plan(units, shards) if shards > 0 else []
    reports = {}
    for file in sorted(directory.rglob("*.json")):
        try:
            report = json.loads(file.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(report, dict) and isinstance(report.get("shard"), list):
            reports[tuple(report["shard"])] = report
    problems = []
    if discovery["errors"]:
        problems.append("discovery errors: " + "; ".join(discovery["errors"]))
    if collections.Counter(discovery["serial"]) != expected:
        problems.append("the per-module discovery differs from the serial discover -s tests set")
    if shards <= 0:
        problems.append("--shards N is required")
    dispatched: collections.Counter = collections.Counter()
    print(f"{sum(expected.values())} tests discovered; {len(units)} units over {shards} shards")
    for index in range(1, shards + 1):
        report = reports.get((index, shards))
        if report is None:
            problems.append(f"shard {index}/{shards}: no report")
            print(f"shard {index}/{shards}: NO REPORT")
            continue
        planned = [u.label for u in plan[index - 1]]
        if report["units"] != planned:
            problems.append(f"shard {index}/{shards}: its units differ from the plan")
        dispatched.update(report["dispatched"])
        if not report["accounted"] or report["ran"] != len(report["dispatched"]):
            problems.append(f"shard {index}/{shards}: ran {report['ran']} of {len(report['dispatched'])} dispatched")
        if not report["green"]:
            problems.append(f"shard {index}/{shards}: red")
        print(f"shard {index}/{shards}: {'green' if report['green'] else 'RED'}, {report['ran']} tests in {report['wall']:.0f}s"
              f" (skipped={report['skipped']}, failures={report['failures']}, errors={report['errors']})"
              + (f", rule D: {report['rule_d']}" if report["rule_d"] else ""))
    if dispatched != expected:
        _print_set_difference("the shards' dispatched ids", dispatched, expected)
        problems.append("the union of dispatched ids is not the discovered set")
    ran = sum(r["ran"] for r in reports.values())
    print(f"Ran {ran} tests over {len(reports)} shards (discovered {sum(expected.values())};"
          f" skipped={sum(r['skipped'] for r in reports.values())}, failures={sum(r['failures'] for r in reports.values())},"
          f" errors={sum(r['errors'] for r in reports.values())})")
    rows = sorted((tuple(row) for r in reports.values() for row in r["durations"]), reverse=True)
    if rows:
        _print_rows(rows, args.durations or 30)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    green = not problems
    print("OK" if green else "FAILED", flush=True)
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
