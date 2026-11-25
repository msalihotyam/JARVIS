"""Read-only batch INT prompt builder (INTBATCH1).

python scripts/int_batch.py --since <ISO ts> [--exclude ID ...]
    [--include ID=SHA ...] [--name INT###] [--out PATH] [--check-only]

Collects TRAIN-READY stacks from build/conveyor/events.jsonl, expands each
into its commits (dropping commits already on origin/main by patch-id),
reports file overlap, missing duration records and importer test modules,
and prints an INT prompt in the INT191.md format plus one backlog.json line.
It never writes backlog.json, never runs tests, never fetches or pushes;
the only file it writes is --out.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MAIN = "origin/main"
HEX = re.compile(r"^[0-9a-fA-F]{7,40}$")
SLICE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
TEST_FILE = re.compile(r"^tests/(test_[A-Za-z0-9_]+)\.py$")
DUR_D = re.compile(r"^scripts/test_durations\.d/[^/]+\.json$")
LANE_RULES = "LANE RULES: build/conveyor/notes/LANE_RULES.md (CORE + INT)"
CONFLICT = (
    "CONFLICT: the EXPECTED files only. Resolve by keeping BOTH sides' "
    "behaviour (never drop a main-side guard, test or duration record; never "
    "drop a pick's change); scripts/test_durations.json conflicts: keep every "
    "record from both sides. List every resolved hunk under RESOLVED. Any "
    "conflict outside these files -> BLOCKED. Never add a commit of your own."
)
REQUIRE_LINE = (
    "Also run python scripts/test_parallel.py --require-durations <MODULES> "
    "and report 0 \"no duration record\" lines; otherwise STATUS BLOCKED naming "
    "the modules. A failing test: name the pick that introduced it (bisect by "
    "module) in the report."
)


def git(root: Path, *args: str, data: bytes | None = None) -> bytes | None:
    """Run a read-only git plumbing command; None on failure."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    r = subprocess.run(["git", *args], cwd=root, input=data,
                       capture_output=True, env=env)
    return r.stdout if r.returncode == 0 else None


def text(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def resolve(root: Path, ref: str) -> str | None:
    out = text(git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}")).strip()
    return out or None


def parse_ts(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("ts")
    # shortcut: offsets are dropped (events.jsonl writes naive local ts); compare
    # aware timestamps properly if events ever start carrying offsets.
    return datetime.fromisoformat(value).replace(tzinfo=None)


def read_events(path: Path, since: datetime):
    """-> (candidates [(id, sha)], landed [ids], malformed count)."""
    last: dict[str, tuple[str, datetime, object]] = {}
    ready_since: set[str] = set()
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            if not isinstance(ev, dict):
                raise ValueError("not an object")
            if ev.get("event") != "chain":
                continue
            src, step = ev["src"], ev["step"]
            if not isinstance(src, str) or not isinstance(step, str):
                raise ValueError("src/step")
            ts = parse_ts(ev["ts"])
        except (ValueError, KeyError, TypeError):
            malformed += 1
            continue
        last[src] = (step, ts, ev.get("sha"))
        if step == "TRAIN-READY" and ts >= since:
            ready_since.add(src)
    cands, landed = [], []
    for src, (step, ts, sha) in last.items():
        if step == "TRAIN-READY" and ts >= since:
            cands.append((src, sha))
        elif src in ready_since:
            landed.append(f"{src} (last step {step})")
    return cands, landed, malformed


def patch_ids(root: Path, rng: str) -> dict[str, str]:
    """sha -> stable patch-id for every non-empty commit in rng."""
    log = git(root, "log", "-p", "--pretty=medium", "--no-color", "--no-ext-diff",
              "--no-renames", rng)
    out = text(git(root, "patch-id", "--stable", data=log or b""))
    pairs = (line.split() for line in out.splitlines())
    return {p[1]: p[0] for p in pairs if len(p) == 2}


def commit_files(root: Path, sha: str) -> list[str]:
    # shortcut: merge commits list no files (no -m); pick stacks are linear today.
    raw = text(git(root, "diff-tree", "-r", "--root", "--no-commit-id",
                   "--name-only", "--no-renames", "-z", sha))
    return [f for f in raw.split("\0") if f and not f.startswith("build/")]


def expand_stacks(root: Path, cands: list[tuple[str, object]]):
    """-> (stacks [(id, tip, picks, files, n_on_main)], skipped, unresolved)."""
    stacks, skipped, unresolved = [], [], []
    seen_sha: dict[str, str] = {}
    seen_pid: dict[str, str] = {}
    main_pids: dict[str, set[str]] = {}
    for sid, sha in cands:
        tip = resolve(root, sha) if isinstance(sha, str) and HEX.match(sha) else None
        mb = text(git(root, "merge-base", tip, MAIN)).strip() if tip else ""
        if not tip or not mb:
            unresolved.append(f"{sid} ({sha!s:.40})")
            continue
        if mb not in main_pids:
            main_pids[mb] = set(patch_ids(root, f"{mb}..{MAIN}").values())
        pids = patch_ids(root, f"{mb}..{tip}")
        revs = text(git(root, "rev-list", "--reverse", f"{mb}..{tip}")).split()
        picks, on_main, covered = [], 0, set()
        for c in revs:
            pid = pids.get(c)  # empty/merge commits have none and are kept
            if pid and pid in main_pids[mb]:
                on_main += 1
            elif c in seen_sha or (pid and pid in seen_pid):
                covered.add(seen_sha.get(c) or seen_pid[pid])
            else:
                picks.append(c)
        for c in picks:
            seen_sha[c] = sid
            if pids.get(c):
                seen_pid[pids[c]] = sid
        if not picks:
            why = "covered by " + ", ".join(sorted(covered)) if covered else "all on main"
            skipped.append(f"{sid} ({why})")
            continue
        files = sorted({f for c in picks for f in commit_files(root, c)})
        stacks.append((sid, tip, picks, files, on_main))
    return stacks, skipped, unresolved


def blob_texts(root: Path, specs: list[str]) -> dict[str, str]:
    """'<rev>:<path>' -> text for every spec that exists (one cat-file call)."""
    out = git(root, "cat-file", "--batch", data="".join(s + "\n" for s in specs).encode())
    res, pos, raw = {}, 0, out or b""
    for spec in specs:
        end = raw.find(b"\n", pos)
        if end < 0:
            break
        head = raw[pos:end].split(b" ")
        pos = end + 1
        if len(head) != 3 or not head[2].isdigit():  # "<spec> missing"/"ambiguous"
            continue
        size = int(head[2])
        if head[1] == b"blob":
            res[spec] = raw[pos:pos + size].decode("utf-8-sig", errors="replace")
        pos += size + 1
    return res


def recorded_classes(root: Path, stacks) -> set[str]:
    """Duration class keys on main (json + .d files) plus picks' .d files at their tip."""
    listing = text(git(root, "ls-tree", "-r", "--name-only", MAIN, "scripts/"))
    specs = [f"{MAIN}:{p}" for p in listing.splitlines()
             if p == "scripts/test_durations.json" or DUR_D.match(p)]
    specs += [f"{tip}:{f}" for _, tip, _, files, _ in stacks for f in files if DUR_D.match(f)]
    keys: set[str] = set()
    for spec, body in blob_texts(root, specs).items():
        try:
            classes = json.loads(body).get("classes") or {}
            keys.update(k for k in classes if isinstance(k, str))
        except (ValueError, AttributeError):
            print(f"WARNING: unreadable duration file {spec}", file=sys.stderr)
    return keys


def has_record(mod: str, keys: set[str]) -> bool:
    return any(k == mod or k.startswith(mod + ".") for k in keys)


def source_modules(files: list[str]) -> dict[str, str]:
    """Touched jarvis/ or scripts/ .py file -> dotted module name."""
    mods = {}
    for f in files:
        if f.endswith(".py") and f.split("/", 1)[0] in ("jarvis", "scripts"):
            name = f[:-3].replace("/", ".")
            mods[f] = name[:-len(".__init__")] if name.endswith(".__init__") else name
    return mods


def test_refs(src: str) -> tuple[set[str], set[str]] | None:
    """(imported module names, string constants) of a test file; None if unparsable."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    names, consts = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            consts.add(node.value)
    return names, consts


def importers(root: Path, stacks, mods: dict[str, str]) -> set[str]:
    """tests.test_x modules that import (or name by path) a touched module."""
    if not mods:
        return set()
    specs = {p: f"{MAIN}:{p}" for p in
             text(git(root, "ls-tree", "--name-only", MAIN, "tests/")).splitlines()
             if TEST_FILE.match(p)}
    for _, tip, _, files, _ in stacks:  # later stacks win: the INT result
        specs.update({f: f"{tip}:{f}" for f in files if TEST_FILE.match(f)})
    blobs = blob_texts(root, list(specs.values()))
    found = set()
    for path, spec in specs.items():
        refs = test_refs(blobs.get(spec, ""))
        if refs is None:
            continue
        names, consts = refs
        via_scripts_dir = "scripts" in consts  # ROOT / "scripts" / "x.py" + sys.path
        for f, mod in mods.items():
            base = f.rsplit("/", 1)[-1]
            if (mod in names or any(f in c for c in consts)
                    or (f.startswith("scripts/") and via_scripts_dir
                        and (base in consts or base[:-3] in names))):
                found.add("tests." + TEST_FILE.match(path).group(1))
                break
    return found


def protected_sets() -> tuple[tuple, set] | None:
    """PROTECTED_DIRS / PROTECTED_FILES read from scripts/conveyor.py as text."""
    try:
        src = Path(__file__).with_name("conveyor.py").read_text(encoding="utf-8")
        dirs = re.search(r"^PROTECTED_DIRS\s*=\s*(.+)$", src, re.M)
        files = re.search(r"^PROTECTED_FILES\s*=\s*(.+)$", src, re.M)
        return (tuple(ast.literal_eval(dirs.group(1))),
                set(ast.literal_eval(files.group(1))))
    except (OSError, AttributeError, ValueError, SyntaxError, TypeError):
        return None


def tier_for(paths: list[str]) -> str:
    prot = protected_sets()
    if prot is None:
        print("WARNING: PROTECTED_DIRS/FILES unreadable; tier T2", file=sys.stderr)
        return "T2"
    dirs, files = prot
    hit = any(p.lower().startswith(dirs) or p.lower() in files for p in paths)
    return "T2" if hit else "T1"


def next_name(root: Path) -> str:
    nums = [int(m.group(1)) for p in (root / "build/conveyor/prompts").glob("INT*.md")
            if (m := re.fullmatch(r"INT(\d+)\.md", p.name))]
    return f"INT{max(nums, default=0) + 1}"


def include_pair(value: str) -> tuple[str, str]:
    sid, sep, sha = value.partition("=")
    if not sep or not SLICE_ID.match(sid) or not HEX.match(sha):
        raise argparse.ArgumentTypeError(f"expected ID=<hex sha>, got {value!r}")
    return sid, sha


def iso_ts(value: str) -> datetime:
    try:
        return parse_ts(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}") from None


def slice_name(value: str) -> str:
    if not SLICE_ID.match(value):
        raise argparse.ArgumentTypeError(f"bad name {value!r}")
    return value


def build_prompt(name, since, main_sha, stacks, overlap, not_picked, modules, expected):
    order = "; ".join(f"{sid} ({len(p)} commit{'s' if len(p) != 1 else ''})"
                      for sid, _, p, _, _ in stacks)
    desc = (f"{name}: batch INT of {len(stacks)} TRAIN-READY stacks since {since}. "
            f"Stacks in pick order: {order}.")
    if overlap:
        desc += " Overlap (allowed, review it): " + "; ".join(
            f"{f} ({', '.join(s)})" for f, s in overlap.items()) + "."
    if not_picked:
        desc += " Not picked: " + "; ".join(not_picked) + "."
    desc += (f" MAIN: use origin/main as it is when you start (expected {main_sha[:7]} "
             "or a descendant); if a pick is already on main (same patch), skip it "
             "and say so. Pick in this order.")
    return "\n".join([
        f"@jarvis lane={name} model=sonnet effort=medium base=main", LANE_RULES, desc,
        "PICKS: " + " ".join(c for _, _, p, _, _ in stacks for c in p),
        "EXPECTED: " + ", ".join(expected), CONFLICT,
        "MODULES: " + " ".join(modules), REQUIRE_LINE, f"CI BRANCH: ci/{name.lower()}",
    ]) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", required=True, type=iso_ts)
    ap.add_argument("--exclude", nargs="*", default=[], metavar="ID")
    ap.add_argument("--include", nargs="*", default=[], type=include_pair, metavar="ID=SHA")
    ap.add_argument("--name", type=slice_name)
    ap.add_argument("--out")
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")
    root = Path(text(git(Path.cwd(), "rev-parse", "--show-toplevel")).strip() or ".")
    main_sha = resolve(root, MAIN)
    if not main_sha:
        print(f"ERROR: {MAIN} not found (this tool never fetches)", file=sys.stderr)
        return 2
    cands, landed, malformed = read_events(root / "build/conveyor/events.jsonl", args.since)
    cands = [(s, sha) for s, sha in cands if s not in args.exclude]
    cands += [(s, sha) for s, sha in args.include if s not in args.exclude]
    stacks, skipped, unresolved = expand_stacks(root, cands)
    skipped = landed + skipped
    owners: dict[str, list[str]] = {}
    for sid, _, _, files, _ in stacks:
        for f in files:
            owners.setdefault(f, []).append(sid)
    overlap = {f: s for f, s in sorted(owners.items()) if len(s) > 1}
    touched = sorted(owners)
    tests = {"tests." + m.group(1) for f in touched if (m := TEST_FILE.match(f))}
    # shortcut: a test file deleted by a pick still lands in MODULES; drop it by hand.
    modules = sorted(tests | importers(root, stacks, source_modules(touched)))
    keys = recorded_classes(root, stacks)
    missing = [m for m in modules if not has_record(m, keys)]
    name = args.name or next_name(root)
    expected = sorted(set(touched) | {"scripts/test_durations.json"})
    tier = tier_for(touched)
    err = sys.stderr
    print(f"MAIN: {MAIN} = {main_sha}", file=err)
    print(f"MALFORMED: {malformed}", file=err)
    print("CANDIDATES: " + (" ".join(s for s, _ in cands) or "none"), file=err)
    for label, rows in (("SKIPPED", skipped), ("UNRESOLVED", unresolved)):
        print(f"{label}: " + ("; ".join(rows) or "none"), file=err)
    for sid, _, picks, _, on_main in stacks:
        print(f"STACK: {sid} picks={len(picks)} on_main={on_main}", file=err)
    print("OVERLAP: " + ("; ".join(f"{f} ({', '.join(s)})" for f, s in overlap.items())
                         or "none"), file=err)
    print("MISSING DURATIONS: " + (" ".join(missing) or "none"), file=err)
    print(f"TIER: {tier}", file=err)
    if not stacks:
        print("ERROR: nothing to pick", file=err)
        return 1
    if args.check_only:
        return 3 if missing else 0
    prompt = build_prompt(name, args.since.isoformat(), main_sha, stacks, overlap,
                          skipped + [f"{u} unresolved" for u in unresolved],
                          modules, expected)
    if args.out:
        Path(args.out).write_text(prompt, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(prompt)
    entry = {"id": name, "approved": False,
             "title": f"[INT] Toplu birleştirme: {len(stacks)} yığın ("
                      + ", ".join(s for s, *_ in stacks) + ")",
             "model": "sonnet", "effort": "medium", "tier": tier,
             "files": ["@main-checkout"], "depends_on": [],
             "prompt": f"build/conveyor/prompts/{name}.md"}
    print(json.dumps(entry, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
