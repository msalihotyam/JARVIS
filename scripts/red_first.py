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
Only files named test_*.py are parsed for methods (tests/testcalc.py is overlaid but never
yields ids: NO NEW TESTS, exit 1). Only TestCase subclasses count (test* methods inherited from in-file bases included); an
id unittest cannot load as a TestCase test on the commit itself is ERROR, never RED.
Exit: 0 all RED or N/A (no source change), 1 any GREEN or a source change with
no new/changed test, 3 any ERROR id, setup, restore or cleanup failed ("SCRATCH LEFT:
<path>") or any unexpected exception (one line "ERROR: <type>: <message>").
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


def _inside(root, rel):
    """True when rel resolves (symlinks/junctions followed) to a path inside root."""
    try:
        (Path(root) / rel).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def protected(path):
    parts = path.split("/")
    return parts[0] == "data" or parts[-1] == "uv.lock" or ".." in parts or path.startswith("/")


def test_methods(source):
    """{(class, method): ast dump} for test* methods (own + inherited from in-file bases) of
    top-level classes that may be TestCase subclasses. A class whose bases all resolve in the
    file to non-TestCase (a plain mixin) is left out; an external base it cannot see is kept
    and the commit-tree probe (``unloadable``) decides."""
    if source is None:
        return {}
    classes = {c.name: c for c in ast.parse(source).body if isinstance(c, ast.ClassDef)}

    def kind(name, seen):  # True TestCase, None unknown (external base), False not one
        res = set()
        for b in classes[name].bases:
            s = ast.unparse(b)
            if s in classes and s not in seen:
                res.add(kind(s, seen | {s}))
            elif s.split(".")[-1].endswith("TestCase"):
                res.add(True)
            elif s not in ("object", name) and s not in seen:
                res.add(None)
        return True if True in res else None if None in res else False

    def methods(name, seen):
        out = {}
        for b in reversed(classes[name].bases):
            s = ast.unparse(b)
            if s in classes and s not in seen:
                out.update(methods(s, seen | {s}))
        out.update({f.name: ast.dump(f) for f in classes[name].body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name.startswith("test")})
        return out

    return {(n, m): d for n in classes if kind(n, {n}) is not False
            for m, d in methods(n, {n}).items()}


_PROBE = (  # positive OK lines on sys.__stdout__: a hijacked or silent stdout reads as unloadable
    # POLISH1 F4 (doc-noted): a @staticmethod test stays ERROR on purpose; stock unittest cannot run it by name
    "import importlib, sys, types, unittest\n"
    "for tid in sys.argv[1:]:\n"
    "    mod, cls, meth = tid.rsplit('.', 2)\n"
    "    try:\n"
    "        c = getattr(importlib.import_module(mod), cls)\n"
    "        ok = issubclass(c, unittest.TestCase) and isinstance(getattr(c(meth), meth), types.MethodType)\n"
    "    except BaseException:\n"
    "        ok = False\n"
    "    if ok:\n"
    "        sys.__stdout__.write('OK ' + tid + '\\n')\n"
    "sys.__stdout__.flush()\n"
)


def unloadable(python, tree, ids, timeout):
    """Ids unittest cannot load as a TestCase test in tree (the commit), or None if the probe failed."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([python, "-B", "-c", _PROBE, *ids], cwd=str(tree), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if p.returncode:
        return None
    ok = {ln[3:] for ln in p.stdout.splitlines() if ln.startswith("OK ")}
    return [t for t in ids if t not in ok]


def changed_ids(path, old, new):
    """Sorted unittest ids of the test methods new adds or changes against old."""
    module = path[:-3].replace("/", ".")
    before, after = test_methods(old), test_methods(new)
    return sorted(f"{module}.{c}.{m}" for (c, m), d in after.items() if before.get((c, m)) != d)


def _body(repo, commit, scratch, python, timeout, out, state):
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
    gone = [p for p, s in changes if is_test_file(p) and s == "D"]
    if not any(not is_test_file(p) for p, _ in changes):
        out("N/A: test-only commit (no source change)")
        return 0
    bad = [p for p in tests + gone if protected(p)]
    if bad:
        out("PROTECTED PATH: " + ", ".join(bad))
        return 3
    overlay, ids = {}, []
    status = dict(changes)
    for p in tests:
        new = _blob(repo, sha, p)
        old = _blob(repo, parent, p)
        if new is None or old is None and status[p] != "A":  # a failed read is not "absent"
            out(f"BLOB READ FAILED: {p}")
            return 3
        overlay[p] = new
        if p.endswith(".py") and p.split("/")[-1].startswith("test_"):
            try:
                ids += changed_ids(p, old, new)
            except SyntaxError as e:
                out(f"PARSE FAILED: {p}: {e}")
                return 3
    if not ids:
        out("NO NEW TESTS: source changed but no test method was added or changed")
        return 1
    add = _git(repo, "worktree", "add", "--detach", str(scratch), sha)
    if add.returncode:
        out("WORKTREE ADD FAILED: " + add.stderr.strip()[:200])
        return 3
    state["added"] = True
    bad_ids = unloadable(python, scratch, ids, timeout)  # judged on the commit, never the parent
    if bad_ids is None:
        out("LOAD PROBE FAILED")
        return 3
    co = _git(scratch, "checkout", "-q", "--detach", parent)
    if co.returncode:
        out("CHECKOUT PARENT FAILED: " + co.stderr.strip()[:200])
        return 3
    ids = [t for t in ids if t not in bad_ids]
    overlay.update({p: None for p in gone})  # the commit deletes them: absent in the overlay
    if not all(_inside(scratch, p) for p in overlay):
        out("OVERLAY PATH ESCAPES THE WORKTREE")
        return 3
    originals = {p: (scratch / p).read_bytes() if (scratch / p).is_file() else None for p in overlay}
    results = {}
    try:
        for p, data in overlay.items():
            if data is None:
                (scratch / p).unlink(missing_ok=True)
                continue
            (scratch / p).parent.mkdir(parents=True, exist_ok=True)
            (scratch / p).write_bytes(data)
        with tempfile.TemporaryDirectory(prefix="redfirst-pyc-") as pyc:
            for i, tid in enumerate(ids):
                results[tid] = run_tests(python, scratch, [tid], Path(pyc) / f"r{i}", timeout)
    finally:
        escaped = False
        for p, data in originals.items():
            if not _inside(scratch, p):  # POLISH2 F1: a path that now resolves outside is never written
                escaped = True
                continue
            if data is None:
                (scratch / p).unlink(missing_ok=True)
            else:
                (scratch / p).write_bytes(data)
        st = _git(scratch, "status", "--porcelain", "--untracked-files=all")
        ok = not escaped and st.returncode == 0 and not st.stdout.strip() and all(data is None or (scratch / p).read_bytes() == data
                            for p, data in originals.items())
        out(f"RESTORED: {ok}")
    if not ok:
        return 3
    code = 3 if bad_ids else 0
    for tid in bad_ids:
        out(f"{tid} ERROR not loadable as a TestCase test on the commit")
    for tid in ids:
        verdict, detail = classify(*results[tid])
        if results[tid][0] < 0:  # timeout or killed runner: a tool failure, never RED
            out(f"{tid} ERROR runner timed out or was killed")
            code = 3
        elif verdict == "PASS":
            out(f"{tid} GREEN")
            code = max(code, 1)
        else:
            out(f"{tid} RED {detail}".rstrip())
    return code


def run(repo, commit="HEAD", label="rf", python=None, scratch_root=None, timeout=600, out=print):
    """Check one commit in one scratch worktree; return the exit code."""
    scratch = Path(scratch_root or tempfile.gettempdir()) / f"redfirst-{label}-{os.getpid()}"
    state = {"added": False}
    try:
        code = _body(repo, commit, scratch, python or sys.executable, timeout, out, state)
    finally:  # only a worktree THIS run added is ours (a pid-reused dir is not)
        if state["added"] and scratch.exists():
            _git(repo, "worktree", "remove", str(scratch))  # never --force
        left = state["added"] and scratch.exists()
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
    try:
        top = _git(a.repo, "rev-parse", "--show-toplevel")
        if top.returncode:
            print("NOT A GIT REPO")
            return 3
        return run(top.stdout.strip(), a.commit, a.label, a.python, a.scratch_root, a.timeout)
    except KeyboardInterrupt:
        print("INTERRUPTED (scratch cleaned up)")
        return 3
    except Exception as e:  # a tool crash must never read as exit 1 (= GREEN)
        msg = " ".join(f"ERROR: {type(e).__name__}: {e}".split())[:300]
        print(msg.encode("ascii", "backslashreplace").decode())  # never a second crash on a cp125x pipe
        return 3


if __name__ == "__main__":
    sys.exit(main())
