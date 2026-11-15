"""Changed-file test selector over the Python import graph (stdlib only, parse-only).

Prints the unittest module ids (tests.test_x) that a change can affect, one per line, sorted;
a one-line summary goes to stderr. A test module is selected when it imports, directly or
transitively, a changed module (importing a.b.c also runs a/__init__.py and a/b/__init__.py).
Fails safe to the full suite (every tests/test_*.py) on any doubt: a non-Python, unknown,
deleted or tests/ helper path, scripts/test_parallel.py, scripts/test_durations.json, pyproject,
CI config, or any file in the graph that does not parse. A module that loads code dynamically
(import_module, spec_from_file_location, run_path, exec, ...; for tests also spawning
sys.executable) is treated as depending on every module, so it is always affected.
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
OPAQUE = re.compile(rb"import_module|__import__|spec_from_file_location|run_path|SourceFileLoader"
                    rb"|(?<![.\w])exec\(")
# A test spawning sys.executable may run any repo script, so it is always selected. Outside tests/
# sys.executable runs Qt tools or tests, never repo code by name, and keeps its static edges only.
SPAWN = re.compile(rb"sys\.executable")


def _is_test(rel: str) -> bool:
    return bool(re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", rel))


def _test_id(rel: str) -> str:
    return rel[:-3].replace("/", ".")


def _parents(rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    return [("/".join(parts[:i]) + "/__init__.py") for i in range(1, len(parts) + 1)]


def select(root: Path, changed: list[str]) -> tuple[list[str], str]:
    """Return (sorted test ids, reason). reason starts with "ALL:" when it failed safe."""
    graph = code_map.build(root, ROOTS)
    modules = graph["modules"]
    tests = sorted(_test_id(r) for r in modules if _is_test(r))
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
    importers: dict[str, set[str]] = {r: set() for r in modules}
    opaque = {r for r in modules if (src := (root / r).read_bytes()) and (
        OPAQUE.search(src) or (_is_test(r) and SPAWN.search(src)))}
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
                         cwd=root, capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="changed repo-relative paths")
    ap.add_argument("--diff", metavar="BASE..HEAD", help="take the paths from git diff --name-only")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    root = Path(args.root)
    changed = list(args.paths) + (_diff_paths(root, args.diff) if args.diff else [])
    ids, reason = select(root, changed)
    for tid in ids:
        print(tid)
    print(f"test_select: {len(ids)} module(s) selected; {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
