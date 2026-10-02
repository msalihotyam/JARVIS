"""Parallel runner for the JARVIS test suite (stdlib only).

Runs the SAME tests as ``python -m unittest discover -s tests`` as several
``python -m unittest <module>`` processes and prints one combined summary.
The serial run stays the reference when a result is disputed.

* Modules that drive a real QML window or launch the app (everything that
  imports PySide6 / jarvis.ui, directly or through another test module) run
  serially in ONE dedicated worker: a window is a global resource.
* Every other module is its own process, spread over N workers (default
  ``min(cpu_count - 1, 6)``), biggest first.
* Each worker has its own TEMP / TMP / TMPDIR. ``tests/__init__.py`` (the
  live-database guard) is imported by every ``tests.*`` module, so the guard
  holds in every process.
* Exit code 0 only when every module passed. A failure that is ONLY on the
  load-sensitive list in JARVIS_ACTIVE_STATE.md is rerun alone once; if all of
  those pass, the run counts as green and both results are printed (rule D).

    python scripts/test_parallel.py [--workers N] [--durations N] [--no-rerun]
                                    [--python PATH] [modules ...]
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
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

_UI_MARKERS = re.compile(r"^\s*(?:from|import)\s+(?:PySide6|jarvis\.ui)\b", re.MULTILINE)
_TEST_IMPORT = re.compile(r"^\s*(?:from|import)\s+tests\.(test_\w+)", re.MULTILINE)
_RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.MULTILINE)
_COUNTS = re.compile(r"^(?:OK|FAILED)(?: \(([^)]*)\))?\s*$", re.MULTILINE)
_FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.MULTILINE)
_DURATION = re.compile(r"^\s*(\d+\.\d+)s\s+(\S.*)$", re.MULTILINE)


def discover(selected: list[str]) -> list[str]:
    names = sorted(p.stem for p in TESTS.glob("test_*.py"))
    if selected:
        wanted = {s.removeprefix("tests.") for s in selected}
        names = [n for n in names if n in wanted]
    return names


def ui_modules(names: list[str]) -> set[str]:
    """Modules that import PySide6 / jarvis.ui, directly or via another test module."""
    source = {n: (TESTS / f"{n}.py").read_text(encoding="utf-8", errors="replace") for n in names}
    ui = {n for n, text in source.items() if _UI_MARKERS.search(text)}
    changed = True
    while changed:
        changed = False
        for n, text in source.items():
            if n not in ui and any(m in ui for m in _TEST_IMPORT.findall(text)):
                ui.add(n)
                changed = True
    return ui


def load_sensitive() -> set[str]:
    """Test ids (``Class.test_name``) the orchestrator listed as load-sensitive."""
    try:
        text = STATE.read_text(encoding="utf-8")
    except OSError:
        return set()
    flat = re.sub(r"\s+", " ", text)
    return {f"{m.group(1)}.{m.group(2)}" for m in re.finditer(r"test_ui\.(\w+)\.(test_\w+)", flat)}


class Result:
    def __init__(self, label, command, returncode, output, wall):
        self.label, self.command, self.returncode, self.output, self.wall = label, command, returncode, output, wall
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

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.parsed and not self.failures and not self.errors


def worker_env(index: int, bases: list[Path]) -> dict[str, str]:
    base = Path(tempfile.mkdtemp(prefix=f"jarvis-test-w{index}-"))
    bases.append(base)
    env = dict(os.environ)
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(base)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_modules(python: str, modules: list[str], env: dict[str, str], durations: int, label: str) -> Result:
    command = [python, "-m", "unittest", *[f"tests.{m}" for m in modules]]
    if durations:
        command[3:3] = ["--durations", str(durations)]
    started = time.monotonic()
    done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return Result(label, command, done.returncode, done.stdout + done.stderr, time.monotonic() - started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modules", nargs="*", help="test modules (default: all of tests/)")
    parser.add_argument("--workers", type=int, default=0, help="non-UI workers (default min(cpu_count - 1, 6))")
    parser.add_argument("--durations", type=int, default=0, help="report the N slowest tests")
    parser.add_argument("--no-rerun", action="store_true", help="do not rerun load-sensitive failures alone")
    parser.add_argument("--python", default=sys.executable, help="interpreter for the workers")
    args = parser.parse_args(argv)

    names = discover(args.modules)
    if not names:
        print("no test modules found", file=sys.stderr)
        return 2
    ui = ui_modules(names)
    plain = sorted((n for n in names if n not in ui), key=lambda n: -(TESTS / f"{n}.py").stat().st_size)
    workers = max(1, args.workers or min((os.cpu_count() or 2) - 1, 6))
    print(f"{len(names)} modules: {len(ui)} UI (1 serial worker), {len(plain)} other over {workers} workers", flush=True)

    bases: list[Path] = []
    results: list[Result] = []
    lock = threading.Lock()
    queue = list(plain)
    started = time.monotonic()
    worker_wall: dict[str, float] = {}

    def ui_worker() -> None:
        began = time.monotonic()
        if ui:
            result = run_modules(args.python, sorted(ui), worker_env(0, bases), args.durations, "ui")
            with lock:
                results.append(result)
        worker_wall["ui"] = time.monotonic() - began

    def plain_worker(index: int) -> None:
        env, began = worker_env(index, bases), time.monotonic()
        while True:
            with lock:
                if not queue:
                    break
                module = queue.pop(0)
            result = run_modules(args.python, [module], env, args.durations, f"w{index}")
            with lock:
                results.append(result)
        worker_wall[f"w{index}"] = time.monotonic() - began

    try:
        with futures.ThreadPoolExecutor(workers + 1) as pool:
            jobs = [pool.submit(ui_worker)] + [pool.submit(plain_worker, i) for i in range(1, workers + 1)]
            for job in jobs:
                job.result()
        wall = time.monotonic() - started

        ran = sum(r.ran for r in results)
        skipped = sum(r.skipped for r in results)
        failures = sum(r.failures for r in results)
        errors = sum(r.errors for r in results)
        broken = [r for r in results if not r.ok]
        print()
        for label, seconds in sorted(worker_wall.items()):
            print(f"worker {label}: {seconds:.0f} s")
        print(f"Ran {ran} tests in {wall:.1f}s wall (skipped={skipped}, failures={failures}, errors={errors})")

        green = not broken
        if broken:
            green = _report_failures(broken, args, bases)
        if args.durations:
            _print_durations(results, args.durations)
        print("OK" if green else "FAILED", flush=True)
        return 0 if green else 1
    finally:
        for base in bases:
            shutil.rmtree(base, ignore_errors=True)


def _report_failures(broken: list[Result], args, bases: list[Path]) -> bool:
    """Print every failure; apply rule D. True when the run counts as green."""
    allowed = load_sensitive()
    named = [test for r in broken for test in r.failed]
    only_known = (not args.no_rerun and bool(named) and all(r.parsed for r in broken)
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
        print(f"  {key}: {'PASS' if passed else 'FAIL'} alone (first run failed)")
        if not passed:
            print(done.stdout + done.stderr)
        green &= passed
    print("rule D: " + ("every rerun passed - the run counts as green" if green else "a rerun failed - the run is red"))
    return green


def _traces(output: str) -> str:
    start = output.find("======")
    return output[start:] if start >= 0 else output[-4000:]


def _print_durations(results: list[Result], top: int) -> None:
    rows = sorted(((float(m.group(1)), m.group(2)) for r in results for m in _DURATION.finditer(r.output)), reverse=True)
    print(f"\nSlowest {top} tests (all workers):")
    for seconds, name in rows[:top]:
        print(f"  {seconds:8.3f}s  {name}")


if __name__ == "__main__":
    raise SystemExit(main())
