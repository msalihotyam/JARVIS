"""Changed-file test selector over the Python import graph (stdlib only, parse-only).

Prints the unittest module ids (tests.test_x) that a change can affect, one per line, sorted;
a one-line summary goes to stderr. A test module is selected when it imports, directly or
transitively, a changed module (importing a.b.c also runs a/__init__.py and a/b/__init__.py).
Fails safe to the full suite (every tests/test_*.py) on any doubt: a non-Python, unknown,
deleted or tests/ helper path, scripts/test_parallel.py, scripts/test_durations.json, pyproject,
CI config, any file in the graph that does not parse, any read error, or a failing git diff.
The ALL list is every tests/test_*.py FILE, parsed or not, so a broken test file still runs.
Dynamic loading (design, kept on purpose): a module that loads code dynamically (import_module,
reload, runpy, spec_from_file_location, pkgutil, unittest loaders, exec, ...) cannot show its
edges. A change to such a non-test module selects ALL; whatever else changed, it still seeds the
walk (it may have loaded the changed code), so its importers' tests always run. A TEST module
that loads dynamically, spawns sys.executable or scans repo source (read_text/rglob/glob/os.walk
next to __file__, "jarvis" or "tests") is always selected. A literal "dynamic import anywhere ->
ALL" rule would fire on every run, since the repo always contains such modules.
Reuses scripts/code_map.py parsing; source is only read, never imported. No network.

    python scripts/test_select.py PATH [PATH ...]
    python scripts/test_select.py --diff BASE..HEAD
"""
from __future__ import annotations

import argparse
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import code_map  # noqa: E402

ROOTS = ("jarvis", "scripts", "tests")
ALWAYS_ALL = {"scripts/test_parallel.py", "scripts/test_durations.json", "scripts/code_map.py",
              "scripts/test_select.py"}
# ponytail: textual scan (comments count too, which only over-selects); a test that spawns a plain
# "python" string instead of sys.executable is not seen, add the marker if one appears.
OPAQUE = re.compile(rb"import_module|__import__|\breload\s*\(|runpy|run_path|run_module|spec_from_"
                    rb"|module_from_spec|SourceFileLoader|SourcelessFileLoader|zipimport|pkgutil"
                    rb"|TestLoader|loadTestsFrom|\.discover\s*\(|(?<![.\w])exec\s*\(|builtins\.exec"
                    rb"|exec_module|load_module|(?<![.\w])eval\s*\(")
# A test spawning an interpreter (sys.executable, "from sys import executable") may run any repo
# script, so it is always selected. A non-test spawner keeps its static edges only.
# ponytail: scripts/conveyor.py spawns repo scripts by name; test_conveyor is opaque (spec_from_) today,
# and a spawn hidden in a tests/ helper or a subclassed TestCase is not seen.
SPAWN = re.compile(rb"\bexecutable\b")
# A test reading or listing files next to a repo anchor may scan source it never imports.
# ponytail: co-occurrence, not data flow; over-selects tests that read temp files and use __file__.
# A non-test module that scans source is not seen; mark it opaque if one appears.
SCAN = re.compile(rb"read_text|read_bytes|\bopen\s*\(|glob\s*\(|iterdir|os\.walk|listdir|scandir")
ANCHOR = re.compile(rb"__file__|[\"'](?:jarvis|tests|scripts)\b|\b(?:jarvis|tests|scripts)[/\\]"
                    rb"|\b(?:get)?cwd\(")


def _is_test(rel: str) -> bool:
    return bool(re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", rel))


def _test_id(rel: str) -> str:
    return rel[:-3].replace("/", ".")


def _parents(rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    return [("/".join(parts[:i]) + "/__init__.py") for i in range(1, len(parts) + 1)]


def _all(root: Path) -> list[str]:
    """Every tests/test_*.py file as an id, from the file list, never from the parse."""
    return sorted(_test_id(rel) for p in (root / "tests").glob("test_*.py")
                  if _is_test(rel := p.relative_to(root).as_posix()))


def _opaque(rel: str, src: bytes) -> bool:
    if OPAQUE.search(src):
        return True
    return _is_test(rel) and bool(SPAWN.search(src) or (SCAN.search(src) and ANCHOR.search(src)))


def select(root: Path, changed: list[str]) -> tuple[list[str], str]:
    """Return (sorted test ids, reason). reason starts with "ALL:" when it failed safe."""
    try:
        return _select(root, changed)
    except OSError as exc:  # a file vanished or is unreadable mid-run
        return _all(root), f"ALL: read error {type(exc).__name__}: {exc}"


def _select(root: Path, changed: list[str]) -> tuple[list[str], str]:
    graph = code_map.build(root, ROOTS)
    modules = graph["modules"]
    tests = _all(root)
    if graph["unparsed"]:
        return tests, f"ALL: parse error in {graph['unparsed'][0]['path']}"
    seeds: set[str] = set()
    for raw in changed:
        rel = raw.strip().replace("\\", "/").removeprefix("./")
        if not rel:
            continue
        if rel in ALWAYS_ALL:
            return tests, f"ALL: runner/selector file {rel}"
        if rel not in modules or not (root / rel).is_file():
            return tests, f"ALL: non-Python, unknown or deleted path {rel}"
        if rel.startswith("tests/") and not _is_test(rel):
            return tests, f"ALL: tests helper {rel}"
        seeds.add(rel)
    if not seeds:
        return [], "no changed paths"
    opaque = {r for r in modules if _opaque(r, (root / r).read_bytes())}
    if dyn := sorted(r for r in seeds & opaque if not _is_test(r)):
        return tests, f"ALL: dynamic-load module changed {dyn[0]}"
    importers: dict[str, set[str]] = {r: set() for r in modules}
    for rel, mod in modules.items():
        for dep in set(mod["imports"]) | {p for p in _parents(rel) if p in modules and p != rel}:
            importers[dep].add(rel)
    # An opaque test module is selected itself; its importers reuse its helpers (pump, UiTestCase),
    # not its test methods, so they keep their own edges. An opaque non-test module seeds the walk.
    # ponytail: a test subclassing another test module's spawning TestCase is missed; mark it opaque too.
    hit, todo = set(), list(seeds | {r for r in opaque if not _is_test(r)})
    while todo:
        rel = todo.pop()
        if rel not in hit:
            hit.add(rel)
            todo.extend(importers[rel])
    picked = sorted(_test_id(r) for r in hit | opaque if _is_test(r))
    return picked, f"{len(seeds)} changed module(s), {len(opaque)} dynamic-load module(s) always affected"


def _diff_paths(root: Path, spec: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", "--no-renames", spec],  # nosec B603 B607
                         cwd=root, capture_output=True, encoding="utf-8", errors="replace",
                         check=True).stdout
    return out.splitlines()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="changed repo-relative paths")
    ap.add_argument("--diff", metavar="BASE..HEAD", help="take the paths from git diff --name-only")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        changed = list(args.paths) + (_diff_paths(root, args.diff) if args.diff else [])
        ids, reason = select(root, changed)
    except (subprocess.CalledProcessError, OSError) as exc:  # git missing or diff failed
        ids, reason = _all(root), f"ALL: git diff failed ({type(exc).__name__})"
    for tid in ids:
        print(tid)
    print(f"test_select: {len(ids)} module(s) selected; {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
