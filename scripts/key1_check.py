"""Read-only verifier for the orchestrator's key1 decision (one command instead of many).

    python scripts/key1_check.py INT-DOCPATH1 [--operator-tool] [--root <main checkout>]

Prints one PASS / FAIL / WARN line per check and a final VERDICT; exit 0 only when no check
FAILs. It writes NOTHING (no build/approvals, no file), never pushes, calls no model and no
network beyond the read-only `gh run list` the conveyor's own CI gate uses (ci_runs, imported).
"""
import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conveyor  # noqa: E402
from conveyor_api import main_checkout  # noqa: E402


def _run(args, cwd, inp=None):
    kw = {"input": inp} if inp is not None else {"stdin": subprocess.DEVNULL}
    p = subprocess.run([shutil.which(args[0]) or args[0], *args[1:]], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120, **kw)
    return p.returncode, (p.stdout or "") + ("" if p.returncode == 0 else p.stderr or "")


def make_git(root):
    return lambda args, inp=None: _run(["git", *args], root, inp)


def make_gh(root):
    return lambda args: _run(args, root)


def field(text, key):
    m = re.search(rf"^{key}:[ \t]*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def read(root, name):
    try:
        return (Path(root) / "build" / "reports" / f"{name}.md").read_text(encoding="utf-8")
    except OSError:
        return None


def patch_id(git, sha):
    rc, diff = git(["diff-tree", "-p", "-r", "--no-ext-diff", "--no-textconv", sha])
    if rc != 0:
        return None
    rc, out = git(["patch-id", "--stable"], diff)
    return out.split()[0] if rc == 0 and out.split() else None


def verify(lane, root, git, gh, operator_tool=False):
    """-> (output lines, ok). git(args, stdin=None) and gh(args) return (rc, output)."""
    out, ok = [], True

    def put(n, st, msg):
        nonlocal ok
        ok = ok and st != "FAIL"
        out.append(f"{st} {n} {msg}")

    rep = read(root, lane)
    if rep is None:
        return [f"FAIL 1 no report build/reports/{lane}.md"], False
    cand, branch = field(rep, "CANDIDATE SHA"), field(rep, "CI BRANCH")
    picks = re.findall(r"([0-9a-f]{40})\s*->\s*([0-9a-f]{40})", rep)
    pick_shas = [c for _, c in picks]
    try:
        backlog = json.loads((Path(root) / "build" / "conveyor" / "backlog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        backlog = {}
    slices = {s.get("id"): s for s in backlog.get("slices", []) if isinstance(s, dict)}
    deps = slices.get(lane, {}).get("depends_on") or []
    srcs = list(dict.fromkeys(re.sub(r"^(CLREV|CDX)-", "", d) for d in deps)) or \
        ([lane[4:]] if lane.startswith("INT-") else [])
    reviews = list(dict.fromkeys(
        [d for d in deps if d.startswith(("CLREV-", "CDX-"))] + [f"CLREV-{s}" for s in srcs]
        + [f"CDX-{s}" for s in srcs if "CDX" in (slices.get(s, {}).get("chain") or [])]))

    # 1: the integration commit, fast-forward of origin/main
    base = git(["rev-parse", "origin/main"])[1].strip()
    if not (cand and conveyor.SHA_RE.fullmatch(cand)) or git(["cat-file", "-e", f"{cand}^{{commit}}"])[0] != 0:
        put(1, "FAIL", f"candidate sha {cand!r} is not a commit")
        return out, False
    commit = field(rep, "COMMIT")
    if not pick_shas or pick_shas[-1] != cand or commit not in (None, cand):
        put(1, "FAIL", f"candidate {cand[:7]} is not the report's integration commit (last pick / COMMIT)")
    elif git(["merge-base", "--is-ancestor", cand, base])[0] == 0:
        put(1, "PASS", f"candidate {cand[:7]} is already contained in origin/main {base[:7]}")
    elif git(["merge-base", "--is-ancestor", base, cand])[0] != 0:
        put(1, "FAIL", f"candidate {cand[:7]} is not a fast-forward of origin/main {base[:7]}")
    else:
        put(1, "PASS", f"candidate {cand[:7]} fast-forwards origin/main {base[:7]}")

    # 2: every pick's patch-id equals the commit its reviews name
    rev = {n: read(root, n) for n in reviews}
    rcommits = {n: field(t, "COMMIT") for n, t in rev.items() if t}
    pp = {s: patch_id(git, s) for s in pick_shas}
    rp = {n: patch_id(git, c) if c else None for n, c in rcommits.items()}
    extra = [c for c in git(["rev-list", f"{base}..{cand}"])[1].split() if c not in pick_shas]
    bad = [f"pick {s[:7]} {'unreadable' if p is None else 'unmatched'}"
           for s, p in pp.items() if p is None or p not in rp.values()] + \
          [f"{n} commit {(rcommits[n] or '?')[:7]} {'not found in repo' if p is None else 'unmatched'}"
           for n, p in rp.items() if p is None or p not in pp.values()] + \
          [f"unlisted commit {c[:7]}" for c in extra]
    if not rev or bad:
        put(2, "FAIL", "patch-id mismatch: " + (", ".join(bad) or "no review reports"))
    else:
        put(2, "PASS", f"{len(pick_shas)} pick(s) patch-id equal to {len(rev)} review commit(s)")

    # 3: every review report STATUS PASS and FINDINGS none
    probs, warns = [], []
    for n in reviews:
        t = rev[n]
        f = (field(t, "FINDINGS") or "").lower() if t else None
        if t is None:
            probs.append(f"{n} report missing")
        elif t.splitlines()[:1] != ["STATUS: PASS"]:
            probs.append(f"{n} {(t.splitlines() or [''])[0]}")
        elif f.startswith("minor") and operator_tool:
            warns.append(f"{n} FINDINGS {f}")
        elif not f.startswith("none"):
            probs.append(f"{n} FINDINGS {f or 'missing'}")
    if probs:
        put(3, "FAIL", "; ".join(probs))
    elif warns:
        put(3, "WARN", "MINOR allowed by --operator-tool: " + "; ".join(warns))
    else:
        put(3, "PASS", f"{', '.join(reviews)}: STATUS PASS, FINDINGS none")

    # 4: CI success on exactly the candidate (the conveyor's own ci_runs)
    state, urls, why = conveyor.Conveyor.ci_runs(SimpleNamespace(cmd=gh), branch or "", cand)
    put(4, "PASS" if state == "success" else "FAIL",
        f"CI {state} on {branch} for {cand[:7]}" + (f": {urls}" if state == "success" else f": {why}"))

    # 5: touched files only those the source slices list
    allowed = [f.replace("\\", "/") for s in srcs for f in slices.get(s, {}).get("files") or []]
    touched = set(git(["diff", "--name-only", "--no-renames", f"{base}...{cand}"])[1].split())
    for s in pick_shas:
        touched |= set(git(["diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", s])[1].split())
    stray = sorted(f for f in touched if not any(fnmatch.fnmatch(f, a) or (a.endswith("/") and f.startswith(a))
                                                 for a in allowed))
    if not allowed or stray:
        put(5, "FAIL", f"files outside {srcs} backlog files: {stray or 'no backlog files'}")
    else:
        put(5, "PASS", f"{len(touched)} file(s), all in the backlog files of {', '.join(srcs)}")

    # 6: the approval file name the orchestrator would write (never written here)
    name = f"push-{cand}-{branch[3:]}" if branch and branch.startswith("ci/") else None
    if name and conveyor.APPROVAL_RE.fullmatch(name):
        put(6, "PASS", f"approval file would be build/approvals/{name}")
    else:
        put(6, "FAIL", f"no valid approval name from CI BRANCH {branch!r}")
    return out, ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("lane", help="INT lane id, e.g. INT-DOCPATH1")
    ap.add_argument("--operator-tool", action="store_true", help="MINOR review findings are WARN, not FAIL")
    ap.add_argument("--root", default=None, help="main checkout (default: this repo's main checkout)")
    a = ap.parse_args(argv)
    root = Path(a.root) if a.root else main_checkout(Path(__file__).resolve().parents[1])
    out, ok = verify(a.lane, root, make_git(root), make_gh(root), a.operator_tool)
    print("\n".join(out))
    print(f"VERDICT: {'PASS' if ok else 'FAIL'} {a.lane}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
