"""DURAUTO1: find a missing or broken test-duration record before INT and print a ready DUR slice spec.

INT plans its units from scripts/test_durations.json united with scripts/test_durations.d/*.json
(scripts/test_parallel.py load_durations); a module with no record at all, or one malformed .d file,
fails the plan. scan() finds both ahead of INT; propose() prints the DUR slice that adds the record,
in the shape of the hand-written DURPM26 / DURSWEEP1 prompts. Read-only apart from --out.

    python -m scripts.dur_autofill --changed SLICE:tests.test_x [--changed ...] [--base SHA] [--out build/DIR]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

ROOT = Path(__file__).resolve().parent.parent
REASONS = ("absent", "unparseable", "module_not_listed", "orphan")
MAX_RECORD_BYTES = 1 << 20          # a record file is a few hundred bytes; bigger is reported, never read
SLICE_RE = re.compile(r"[A-Z][A-Z0-9]{0,39}")
MODULE_RE = re.compile(r"tests\.test_[a-z0-9_]{1,80}")
KEY_RE = re.compile(r"(tests\.test_[a-z0-9_]{1,80})\.[A-Za-z_][A-Za-z0-9_]{0,120}(\.[A-Za-z_][A-Za-z0-9_]{0,120})?")
BASE_PLACEHOLDER = "<BASE>"


class Missing(NamedTuple):
    test_module: str
    slice_id: str
    reason: str


def _shown(value: object) -> str:
    """A value safe to print on one line on any console: ids pass as they are, anything else as a short ascii()."""
    return value if isinstance(value, str) and (SLICE_RE.fullmatch(value) or MODULE_RE.fullmatch(value)) else ascii(value)[:80]


def _no_dupes(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def read_record(file: Path) -> dict[str, dict[str, float]] | None:
    """test_parallel._read_slice_durations's rules (None where it exits), plus a size cap and no
    duplicate keys (json would keep the last one silently)."""
    try:
        if file.stat().st_size > MAX_RECORD_BYTES:
            return None
        data = json.loads(file.read_text(encoding="utf-8-sig"), object_pairs_hook=_no_dupes)
        if not isinstance(data, dict):
            return None
        maps = {}
        for name in ("classes", "tests"):
            records = data.get(name, {})
            if not isinstance(records, dict):
                return None
            for v in records.values():
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= float(v) < float("inf"):
                    return None
            maps[name] = {k: float(v) for k, v in records.items()}
        return maps
    except (OSError, ValueError, OverflowError, RecursionError, UnicodeError, TypeError):
        return None


def module_file(root: Path, module: str) -> Path:
    return root.joinpath(*module.split(".")).with_suffix(".py")


def test_classes(path: Path) -> list[str] | None:
    """TestCase classes with at least one test method, statically (a test module is never imported);
    None when the file cannot be read or parsed.
    shortcut: a base class imported from another module is seen only by its name (ends in TestCase),
    its inherited test methods are not; upgrade to the unittest loader if that ever misses a class."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, SyntaxError, RecursionError, MemoryError):
        return None
    defs = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

    def base_names(node: ast.ClassDef) -> list[str]:
        return [b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else "" for b in node.bases]

    def is_case(name: str, seen: frozenset = frozenset()) -> bool:
        if name.endswith("TestCase"):
            return True
        node = defs.get(name)
        return node is not None and name not in seen and any(is_case(b, seen | {name}) for b in base_names(node))

    def has_tests(name: str, seen: frozenset = frozenset()) -> bool:
        node = defs.get(name)
        if node is None or name in seen:
            return False
        own = any(isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name.startswith("test") for f in node.body)
        return own or any(has_tests(b, seen | {name}) for b in base_names(node))

    return sorted(n for n, node in defs.items() if any(is_case(b, frozenset({n})) for b in base_names(node)) and has_tests(n))


def _main_classes(durations_dir: Path) -> set[str]:
    try:
        data = json.loads(durations_dir.with_name("test_durations.json").read_text(encoding="utf-8"))
        return {k for k in data.get("classes", {}) if isinstance(k, str)}
    except (OSError, ValueError, AttributeError, TypeError, RecursionError):
        return set()


def _pairs(changed_tests: object) -> list:
    if isinstance(changed_tests, dict):
        return list(changed_tests.items())
    return list(changed_tests) if changed_tests is not None and not isinstance(changed_tests, (str, bytes)) else []


def scan(changed_tests, durations_dir, known_dur_slices: Iterable = (), root: Path | None = None) -> list[Missing]:
    """changed_tests: {test_module: slice_id} or (test_module, slice_id) pairs, the slice that adds or
    changes the module. absent = no class of the module recorded anywhere (INT fails); module_not_listed =
    some of its TestCase classes recorded, not all; unparseable = a .d file INT rejects (test_module "")
    or an invalid changed id; orphan = a .d key naming a test module that no longer exists. A module
    whose DUR slice ("DUR" + slice id) is already queued or landed is not reported. Never raises."""
    found: set[Missing] = set()
    try:
        durations_dir = Path(durations_dir)
        root = Path(root) if root is not None else durations_dir.resolve().parent.parent
        known = {k for k in (known_dur_slices or ()) if isinstance(k, str)}
        recorded = _main_classes(durations_dir)
        files = sorted(durations_dir.glob("*.json"), key=lambda p: p.name) if durations_dir.is_dir() else []
        for file in files:
            known.add(file.stem)
            record = read_record(file)
            if record is None:
                found.add(Missing("", _shown(file.stem), "unparseable"))
                continue
            recorded |= set(record["classes"])
            for key in sorted({**record["classes"], **record["tests"]}):
                match = KEY_RE.fullmatch(key)
                if match is None:
                    found.add(Missing(_shown(key), _shown(file.stem), "unparseable"))
                elif not module_file(root, match.group(1)).is_file():
                    found.add(Missing(match.group(1), _shown(file.stem), "orphan"))
        for pair in _pairs(changed_tests):
            try:
                module, slice_id = pair
            except (TypeError, ValueError):
                found.add(Missing(_shown(pair), "", "unparseable"))
                continue
            if not (isinstance(module, str) and MODULE_RE.fullmatch(module)
                    and isinstance(slice_id, str) and SLICE_RE.fullmatch(slice_id)):
                found.add(Missing(_shown(module), _shown(slice_id), "unparseable"))
                continue
            path = module_file(root, module)
            if "DUR" + slice_id in known or not path.is_file():
                continue                    # a DUR slice covers it, or the module is gone: nothing to record
            if not any(k.startswith(module + ".") for k in recorded):
                found.add(Missing(module, slice_id, "absent"))
            elif any(f"{module}.{c}" not in recorded for c in test_classes(path) or []):
                found.add(Missing(module, slice_id, "module_not_listed"))
    except Exception as exc:  # noqa: BLE001 - a scan never breaks the INT it guards; it reports instead
        found.add(Missing(_shown(type(exc).__name__), "", "unparseable"))
    return sorted(found)


def propose(missing: Iterable, base: str = BASE_PLACEHOLDER, root: Path | None = None) -> dict[str, str]:
    """{"DUR" + slice id: prompt text}, one per slice, in the DURPM26 shape: T1, files exactly the one
    record plus its report. Orphans and unparseable entries get none. Same input, same bytes."""
    root = Path(root) if root is not None else ROOT
    base = base if isinstance(base, str) and re.fullmatch(r"[0-9a-f]{40}", base) else BASE_PLACEHOLDER
    by_slice: dict[str, set[str]] = {}
    for m in missing or ():
        if (isinstance(m, Missing) and m.reason in ("absent", "module_not_listed")
                and isinstance(m.slice_id, str) and SLICE_RE.fullmatch(m.slice_id)
                and isinstance(m.test_module, str) and MODULE_RE.fullmatch(m.test_module)):
            by_slice.setdefault(m.slice_id, set()).add(m.test_module)
    return {f"DUR{s}": _spec(s, sorted(mods), base, root) for s, mods in sorted(by_slice.items())}


def _spec(slice_id: str, modules: list[str], base: str, root: Path) -> str:
    dur, mods = f"DUR{slice_id}", ", ".join(modules)
    expected = [f"{m}.{c}" for m in modules for c in (test_classes(module_file(root, m)) or [])]
    record = f"scripts/test_durations.d/{dur}.json"
    return "\n".join([
        f"@jarvis lane={dur} model=sonnet effort=medium base={base}",
        "LANE RULES: build/conveyor/notes/lanes/code.md (CORE). SMALL (<= 30 tool calls).",
        f"T1. CI needs a duration record for every TestCase class. {slice_id} adds or changes {mods} without a "
        f"complete one (found by scripts/dur_autofill.py). Cherry-pick its commit(s) in the throwaway branch.",
        "Method: in your worktree make a throwaway branch from your base, cherry-pick those commits, run each module "
        "with python scripts/test_parallel.py <module> --durations 50 and note per-class seconds. Then git switch "
        f"back to a fresh branch at your base (git switch -c {dur.lower()} <base>) - never reset/stash/clean - and "
        f'create ONLY {record}: {{"version": 1, "classes": {{"tests.test_x.ClassName": seconds, ...}}}} for every '
        "TestCase class of the module(s) not yet recorded, seconds rounded up to 0.1 with a floor of 1.0. Same "
        'schema as scripts/test_durations.json "classes". Expected classes (verify from the code): '
        f"{', '.join(expected) or 'derive from the code'}.",
        "Acceptance: 1) every TestCase class in the module(s) has a record (list them in the report, derived from "
        "the code, not guessed). 2) the JSON parses; values non-negative numbers. 3) your commit contains only that one file.",
        "Hard limits: no network, no model calls, never open data/jarvis.db.",
        f"Run: python -c \"import json;json.load(open('{record}'))\"",
        f"Owned files: {record} (new), build/reports/{dur}.md (new). Anything else -> STOP (C2). "
        "Do NOT edit scripts/test_durations.json.",
        f'ONE commit "test(ci): {dur} duration records for {mods}".',
        f"Report build/reports/{dur}.md (C7) with the class -> seconds table and exactly one COMMIT: line (full 40 hex).",
    ]) + "\n"


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(folder)]) == os.path.normcase(folder)
    except ValueError:              # another drive, or mixed absolute/relative
        return False


def out_dir(value: str, root: Path) -> str | None:
    """The realpath of --out when it sits inside root/build (relative values resolve against root);
    None for anything else: absolute elsewhere, '..', a symlink or junction leading out."""
    build = os.path.realpath(root / "build")
    try:
        target = os.path.realpath(os.path.join(root, value))
    except (OSError, ValueError, TypeError):
        return None
    return target if target != build and _inside(target, build) else None


def _changed(value: str) -> tuple[str, str]:
    slice_id, sep, module = value.partition(":")
    module = module.replace("\\", "/")
    if module.endswith(".py"):
        module = module[:-3].replace("/", ".")
    return (module, slice_id) if sep else (value, "")


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    root = Path(root) if root is not None else ROOT
    parser = argparse.ArgumentParser(prog="python -m scripts.dur_autofill", description=__doc__.splitlines()[0])
    parser.add_argument("--changed", action="append", default=[], metavar="SLICE:tests.test_x",
                        help="a slice and a test module it adds or changes (repeatable; tests/test_x.py also works)")
    parser.add_argument("--durations-dir", default=str(root / "scripts" / "test_durations.d"))
    parser.add_argument("--base", default=BASE_PLACEHOLDER, help="base SHA written into the proposed specs")
    parser.add_argument("--out", help="write each proposal to DIR/<DURID>.md; DIR must be inside build/")
    args = parser.parse_args(argv)
    target = None
    if args.out is not None:
        target = out_dir(args.out, root)
        if target is None:
            print(f"refused: --out must be a folder inside {root / 'build'}: {_shown(args.out)}", file=sys.stderr)
            return 2
    prompts = root / "build" / "conveyor" / "prompts"
    known = {p.stem for p in prompts.glob("DUR*.md")} if prompts.is_dir() else set()
    missing = scan([_changed(c) for c in args.changed], Path(args.durations_dir), known, root=root)
    proposals = propose(missing, args.base, root)
    for m in missing:
        print(f"MISSING {m.reason} module={m.test_module or '-'} slice={m.slice_id or '-'}")
    for dur, text in proposals.items():
        print(f"--- {dur}\n{text}", end="")
        if target is not None:
            file = os.path.join(target, f"{dur}.md")
            try:
                os.makedirs(target, exist_ok=True)
                if not _inside(os.path.realpath(file), target):
                    raise OSError("leaves the --out folder")
                with open(file, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
            except OSError as exc:
                print(f"refused: {_shown(dur)}: {_shown(str(exc))}", file=sys.stderr)
                return 2
    if not missing:
        print("dur_autofill: every changed test module has its duration record")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
