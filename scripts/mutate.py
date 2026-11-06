"""MUTATE: run one or more mutations in a throwaway detached worktree (stdlib only).

    python scripts/mutate.py --file <repo-relative> --old <exact text> --new <text>
                             --tests <id> [<id> ...] [--base <sha>] [--label <lane>]
    python scripts/mutate.py --spec specs.json [--base <sha>] [--label <lane>]
        (specs.json: a list of {"name", "file", "old", "new", "tests"})

Per run: ``git worktree add --detach <TEMP>/mut-<label>-<pid> <base>``; each
mutation must match exactly once; the unmutated targeted tests must pass first
(one baseline per distinct test list); the mutated bytes are written, ONLY the
named unittest ids run (never the suite), the bytes are restored and checked
(equal, ``git status --porcelain`` empty); the worktree is removed WITHOUT
``--force`` in a finally (also on Ctrl-C). Per mutation: CAUGHT / MISSED /
NOT APPLIED / ERROR-CAUGHT (an import or syntax error, not an assertion).
Exit: 0 all CAUGHT, 1 any MISSED, 2 any NOT APPLIED, 3 baseline red, restore
or cleanup failed ("SCRATCH LEFT: <path>"). Highest applicable code wins.
No network, no commit or push, never data/ or uv.lock, files over 64 MiB refused.

The git-safety probe set (fake_git, grid, interleave, fake_clock, fake_scandir,
bad_values, sparse_file, held_file_replace) is at the end of this file.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024  # C11
_FORBIDDEN_TOP = {"data"}
_FORBIDDEN_NAMES = {"uv.lock"}
_ID = re.compile(r"^(?:FAIL|ERROR): (.+)$", re.M)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def check_target(root, rel):
    """Return (path, None) for a mutable file inside root, else (None, reason)."""
    root = Path(root).resolve()
    try:
        path = (root / rel).resolve()
        parts = path.relative_to(root).parts
    except (ValueError, OSError):
        return None, "file escapes the worktree"
    if not parts or parts[0] in _FORBIDDEN_TOP or parts[-1] in _FORBIDDEN_NAMES:
        return None, "file is protected (data/ or uv.lock)"
    if not path.is_file():
        return None, "file not found"
    if path.stat().st_size > MAX_BYTES:
        return None, "file over 64 MiB"
    return path, None


def apply_text(data, old, new):
    """Return (mutated bytes, None) or (None, reason); old must match exactly once."""
    if not old or old == new:
        return None, "old is empty or equals new"
    o, n = old.encode(), new.encode()
    if data.count(o) == 0 and b"\r\n" in data:  # CRLF checkout of an LF-written old
        o, n = o.replace(b"\n", b"\r\n"), n.replace(b"\n", b"\r\n")
    count = data.count(o)
    if count != 1:
        return None, f"{count} matches"
    return data.replace(o, n), None


def run_tests(python, scratch, ids, pyc, timeout):
    """Run only the named unittest ids; return (returncode, output). Timeout = (-1, ...)."""
    env = dict(os.environ, PYTHONPYCACHEPREFIX=str(pyc), PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run([python, "-m", "unittest", *ids], cwd=str(scratch), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    return p.returncode, (p.stderr or "") + (p.stdout or "")


def classify(rc, out):
    """('PASS'|'CAUGHT'|'ERROR-CAUGHT', detail) for one mutated run."""
    if rc == 0:
        return "PASS", ""
    if rc == -1:
        return "ERROR-CAUGHT", "timeout"
    ids = [m.group(1)[:80] for m in _ID.finditer(out)]
    broken = ("_FailedTest" in out or "Ran 0 tests" in out
              or (re.search(r"SyntaxError|IndentationError", out) and "AssertionError" not in out))
    if broken or not ids:
        return "ERROR-CAUGHT", "import/syntax error or no tests ran"
    m = re.search(r"FAILED \((.*?)\)", out)
    return "CAUGHT", f"failures={len(ids)} ({m.group(1) if m else '?'}) by " + "; ".join(ids[:5])


def _restore(path, original, scratch):
    path.write_bytes(original)
    st = _git(scratch, "status", "--porcelain", "--untracked-files=all").stdout.strip()
    return path.read_bytes() == original and not st


def _body(repo, specs, base, scratch, python, timeout, out):
    sha = _git(repo, "rev-parse", "--verify", base + "^{commit}")
    if sha.returncode:
        out(f"BASE NOT FOUND: {base}")
        return 3
    code = 0
    baselines = set()
    add = _git(repo, "worktree", "add", "--detach", str(scratch), sha.stdout.strip())
    if add.returncode:
        out("WORKTREE ADD FAILED: " + add.stderr.strip()[:200])
        return 3
    with tempfile.TemporaryDirectory(prefix="mut-pyc-") as pyc:
        for i, spec in enumerate(specs, 1):
            name, ids = spec.get("name") or f"M{i}", list(spec.get("tests") or [])
            path, why = check_target(scratch, spec.get("file", ""))
            mutated = None
            if path and not ids:
                why = "no tests named"
            elif path:
                original = path.read_bytes()
                mutated, why = apply_text(original, spec.get("old", ""), spec.get("new", ""))
            if mutated is None:
                out(f"{name} NOT APPLIED: {why}")
                code = max(code, 2)
                continue
            if tuple(ids) not in baselines:
                rc, text = run_tests(python, scratch, ids, Path(pyc) / f"b{i}", timeout)
                if rc:
                    out(f"{name} BASELINE RED: " + "; ".join(_ID.findall(text)[:5])[:300])
                    return 3
                baselines.add(tuple(ids))
            try:
                path.write_bytes(mutated)
                rc, text = run_tests(python, scratch, ids, Path(pyc) / f"m{i}", timeout)
            finally:
                ok = _restore(path, original, scratch)
            out(f"{name} RESTORED: {ok}")
            if not ok:
                return 3
            verdict, detail = classify(rc, text)
            if verdict == "PASS":
                out(f"{name} MISSED")
                code = max(code, 1)
            else:
                out(f"{name} {verdict} {detail}".rstrip())
    return code


def run_specs(repo, specs, base="HEAD", label="mut", python=None, scratch_root=None,
              timeout=600, out=print):
    """Run the specs in one scratch worktree; return the exit code."""
    scratch = Path(scratch_root or tempfile.gettempdir()) / f"mut-{label}-{os.getpid()}"
    try:
        code = _body(repo, specs, base, scratch, python or sys.executable, timeout, out)
    finally:
        if scratch.exists():
            _git(repo, "worktree", "remove", str(scratch))  # never --force
        left = scratch.exists()
        if left:
            out(f"SCRATCH LEFT: {scratch}")  # the operator decides; no rmtree of unknown paths
    return 3 if left else code


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file")
    ap.add_argument("--old")
    ap.add_argument("--new")
    ap.add_argument("--tests", nargs="+")
    ap.add_argument("--spec", help="JSON list of {name,file,old,new,tests}")
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--label", default="mut")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--scratch-root")
    ap.add_argument("--python")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args(argv)
    if a.spec:
        specs = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    elif a.file and a.old is not None and a.new is not None and a.tests:
        specs = [{"file": a.file, "old": a.old, "new": a.new, "tests": a.tests}]
    else:
        ap.error("give --spec, or --file --old --new --tests")
    top = _git(a.repo, "rev-parse", "--show-toplevel")
    if top.returncode:
        print("NOT A GIT REPO")
        return 3
    try:
        return run_specs(top.stdout.strip(), specs, a.base, a.label, a.python,
                         a.scratch_root, a.timeout)
    except KeyboardInterrupt:
        print("INTERRUPTED (scratch cleaned up)")
        return 3


# --- git-safety probe set: importable helpers for hand-built review probes ---
BAD_VALUES = (0, -1, True, 1.5, "1", None, float("nan"), 2**33)


def bad_values():
    """Values a numeric input must refuse (fail closed): zero, negative, bool, float, str, None, NaN, huge."""
    return list(BAD_VALUES)


def fake_git(rc=0, out="", err=""):
    """A git-runner stand-in returning CompletedProcess(rc, out, err); .calls records every argv."""
    def fake(*args, **kwargs):
        fake.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, rc, out, err)
    fake.calls = []
    return fake


def grid(labels, cases=("keep", "lower", "upper", "title", "swapcase"), prefixes=("",), seps=("",)):
    """Every prefix + sep + case(label) form: feed it to a gate and expect one verdict for all."""
    out = []
    for lab in labels:
        for case in cases:
            text = lab if case == "keep" else getattr(lab, case)()
            out.extend(pre + sep + text for pre in prefixes for sep in seps)
    return out


def interleave(fn, at, inject):
    """Wrap fn: on its at-th call (1-based) run inject() first, then fn (forces a race window)."""
    def wrapper(*args, **kwargs):
        wrapper.calls += 1
        if wrapper.calls == at:
            inject()
        return fn(*args, **kwargs)
    wrapper.calls = 0
    return wrapper


def fake_clock(step=1.0, start=0.0):
    """A now() that advances by step on every call (never reads the wall clock)."""
    t = [start - step]

    def now():
        t[0] += step
        return t[0]
    return now


def fake_scandir(tree):
    """scandir stand-in over a dict tree: {name: {...subdir} | int size}; keys of path parts via '/'."""
    from types import SimpleNamespace

    def scandir(path="."):
        node = tree
        for part in [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]:
            node = node[part]
        for name, val in node.items():
            is_dir = isinstance(val, dict)
            yield SimpleNamespace(
                name=name, path=f"{path}/{name}", is_dir=lambda follow_symlinks=True, d=is_dir: d,
                is_file=lambda follow_symlinks=True, d=is_dir: not d,
                is_symlink=lambda: False,
                stat=lambda follow_symlinks=True, v=val: SimpleNamespace(st_size=0 if is_dir else v))
    return scandir


def sparse_file(path, size):
    """Create a file of `size` bytes with os.truncate (never writes data); C11 caps size at 64 MiB + 1."""
    if size > MAX_BYTES + 1:
        raise ValueError("C11: never create a file past 64 MiB + 1")
    with open(path, "wb"):
        pass
    os.truncate(path, size)
    return path


def held_file_replace(directory):
    """Hold a file open, os.replace another over it; return (replaced, winerror, errno)."""
    d = Path(directory)
    victim, other = d / "held.txt", d / "other.txt"
    victim.write_text("held", encoding="utf-8")
    other.write_text("other", encoding="utf-8")
    with open(victim, "rb"):
        try:
            os.replace(other, victim)
        except OSError as exc:
            return False, getattr(exc, "winerror", None), exc.errno
    return True, None, None


if __name__ == "__main__":
    sys.exit(main())
