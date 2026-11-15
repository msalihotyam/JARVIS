"""REDFIRST: run a commit's new or changed tests against its PARENT code (stdlib only).

    python scripts/red_first.py [--commit <sha>] [--label <lane>] [--repo .]

Test files (anything under a ``tests/`` folder, or a root-level ``test_*.py``) come from
the commit, every other file from its first parent, in a throwaway
``git worktree add --detach <TEMP>/redfirst-<label>-<pid> <parent>``. Test methods
the commit adds or changes (top-level class, ``test*`` name, AST differs from the
parent) run one by one, in sorted order: RED = fails on the parent (it guards the
change), GREEN = passes on the parent (proves nothing for this change). The test
files are restored and checked (bytes equal, ``git status --porcelain`` empty);
the worktree is removed WITHOUT ``--force`` in a finally (also on Ctrl-C).
Exit: 0 all RED or N/A (no source change), 1 any GREEN or a source change with
no new/changed test, 3 setup, restore or cleanup failed ("SCRATCH LEFT: <path>").
No network, no commit or push, never data/ or uv.lock.
"""

import argparse
import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutate import _git, classify, run_tests  # noqa: E402


def _blob(repo, rev, path):
    """Bytes of path at rev, or None when it does not exist there."""
    p = subprocess.run(["git", "cat-file", "blob", f"{rev}:{path}"], cwd=str(repo),
                       capture_output=True)
    return p.stdout if p.returncode == 0 else None


def is_test_file(path):
    """Under a tests/ folder, or a root-level test_*.py (scripts/test_parallel.py is source)."""
    parts = path.split("/")
    return "tests" in parts[:-1] or len(parts) == 1 and parts[0].startswith("test_") and path.endswith(".py")


def protected(path):
    parts = path.split("/")
    return parts[0] == "data" or parts[-1] == "uv.lock" or ".." in parts or path.startswith("/")


def test_methods(source):
    """{(class, method): ast dump} for test* methods of top-level classes."""
    if source is None:
        return {}
    tree = ast.parse(source)
    return {(c.name, f.name): ast.dump(f)
            for c in tree.body if isinstance(c, ast.ClassDef)
            for f in c.body
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name.startswith("test")}


def changed_ids(path, old, new):
    """Sorted unittest ids of the test methods new adds or changes against old."""
    module = path[:-3].replace("/", ".")
    before, after = test_methods(old), test_methods(new)
    return sorted(f"{module}.{c}.{m}" for (c, m), d in after.items() if before.get((c, m)) != d)


def _body(repo, commit, scratch, python, timeout, out):
    sha = _git(repo, "rev-parse", "--verify", commit + "^{commit}")
    parent = _git(repo, "rev-parse", "--verify", commit + "^1^{commit}")
    if sha.returncode or parent.returncode:
        out(f"COMMIT OR PARENT NOT FOUND: {commit}")
        return 3
    sha, parent = sha.stdout.strip(), parent.stdout.strip()
    out(f"COMMIT {sha}\nPARENT {parent}")
    diff = _git(repo, "diff", "--name-status", "--no-renames", "-z", parent, sha)
    if diff.returncode:
        out("DIFF FAILED: " + diff.stderr.strip()[:200])
        return 3
    fields = diff.stdout.split("\0")[:-1]
    changes = sorted(zip(fields[1::2], fields[0::2]))  # (path, status)
    tests = [p for p, s in changes if is_test_file(p) and s != "D"]
    if not any(not is_test_file(p) for p, _ in changes):
        out("N/A: test-only commit (no source change)")
        return 0
    bad = [p for p in tests if protected(p)]
    if bad:
        out("PROTECTED PATH: " + ", ".join(bad))
        return 3
    overlay, ids = {}, []
    for p in tests:
        new = _blob(repo, sha, p)
        overlay[p] = new
        if p.endswith(".py") and p.split("/")[-1].startswith("test_"):
            try:
                ids += changed_ids(p, _blob(repo, parent, p), new)
            except SyntaxError as e:
                out(f"PARSE FAILED: {p}: {e}")
                return 3
    if not ids:
        out("NO NEW TESTS: source changed but no test method was added or changed")
        return 1
    add = _git(repo, "worktree", "add", "--detach", str(scratch), parent)
    if add.returncode:
        out("WORKTREE ADD FAILED: " + add.stderr.strip()[:200])
        return 3
    originals = {p: (scratch / p).read_bytes() if (scratch / p).is_file() else None for p in overlay}
    results = {}
    try:
        for p, data in overlay.items():
            (scratch / p).parent.mkdir(parents=True, exist_ok=True)
            (scratch / p).write_bytes(data)
        with tempfile.TemporaryDirectory(prefix="redfirst-pyc-") as pyc:
            for i, tid in enumerate(ids):
                results[tid] = run_tests(python, scratch, [tid], Path(pyc) / f"r{i}", timeout)
    finally:
        for p, data in originals.items():
            if data is None:
                (scratch / p).unlink(missing_ok=True)
            else:
                (scratch / p).write_bytes(data)
        st = _git(scratch, "status", "--porcelain", "--untracked-files=all").stdout.strip()
        ok = not st and all(data is None or (scratch / p).read_bytes() == data
                            for p, data in originals.items())
        out(f"RESTORED: {ok}")
    if not ok:
        return 3
    code = 0
    for tid in ids:
        verdict, detail = classify(*results[tid])
        if verdict == "PASS":
            out(f"{tid} GREEN")
            code = 1
        else:
            out(f"{tid} RED {detail}".rstrip())
    return code


def run(repo, commit="HEAD", label="rf", python=None, scratch_root=None, timeout=600, out=print):
    """Check one commit in one scratch worktree; return the exit code."""
    scratch = Path(scratch_root or tempfile.gettempdir()) / f"redfirst-{label}-{os.getpid()}"
    try:
        code = _body(repo, commit, scratch, python or sys.executable, timeout, out)
    finally:
        if scratch.exists():
            _git(repo, "worktree", "remove", str(scratch))  # never --force
        left = scratch.exists()
        if left:
            out(f"SCRATCH LEFT: {scratch}")  # the operator decides; no rmtree of unknown paths
    return 3 if left else code


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--label", default="rf")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--scratch-root")
    ap.add_argument("--python")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args(argv)
    top = _git(a.repo, "rev-parse", "--show-toplevel")
    if top.returncode:
        print("NOT A GIT REPO")
        return 3
    try:
        return run(top.stdout.strip(), a.commit, a.label, a.python, a.scratch_root, a.timeout)
    except KeyboardInterrupt:
        print("INTERRUPTED (scratch cleaned up)")
        return 3


if __name__ == "__main__":
    sys.exit(main())
