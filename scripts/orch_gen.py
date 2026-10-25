"""Deterministic generator for the orchestrator's routine prompts (ORCHGEN).

    python scripts/orch_gen.py [--root DIR] review <SLICE>... [--write]
    python scripts/orch_gen.py [--root DIR] int <REVIEW>... [--write]

review: reads build/reports/<SLICE>.md (STATUS PASS, one 40-hex COMMIT, FILES, a "Worker: codex"
line) and writes an independent review prompt: CLREVnn (Claude) for work the Codex worker wrote,
CDXINTnn (Codex) for work Claude wrote, plus a backlog entry with the next free number.
int: once the named reviews are PASS, writes INTnn: base = origin/main (git ls-remote), picks in
ancestry order, expected files = union of git show --name-only, their tests modules (+ tests.test_ui
only when a UI file is touched), CI BRANCH ci/<intnn>.
Dry run is the default (prints what it would write). --write writes the prompt and the backlog entry,
keeps a backup of the backlog in build/conveyor/hold/ and runs build/orch/check_backlog.py.
Anything unclear refuses (exit 2) with the reason. Never edits conveyor.py, never pushes, never
writes approvals (the entry carries no "approved" key).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conveyor  # noqa: E402  (read-only: the conveyor's own validators)

SHA40 = re.compile(r"[0-9a-f]{40}")
COMMIT_RE = re.compile(r"(?m)^[\s>#*`-]*COMMIT:[\s`]*([0-9a-fA-F]{7,40})\b")
FILES_RE = re.compile(r"(?m)^[\s>#*`-]*FILES:[ \t]*(.*)$")
WORKER_RE = re.compile(r"(?im)^[\s>#*`-]*worker:[\s`]*codex\b")
SCOPE_RE = re.compile(r"(?m)^.*?\bScope:(.*)$")
PAIR_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9._-]*) `?([0-9a-f]{7,40})\b")
PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
REVIEW_NUM = re.compile(r"(?i)^(?:CDXINT|CLREV)(\d+)")
INT_NUM = re.compile(r"(?i)^INT(\d+)")


class Refuse(Exception):
    pass


def real_git(root, *args):
    r = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout


class Gen:
    def __init__(self, root, git=real_git, out=print):
        self.root = Path(root)
        self.git_fn, self.out = git, out
        self.backlog_path = self.root / "build" / "conveyor" / "backlog.json"
        self.prompts = self.root / "build" / "conveyor" / "prompts"
        self.reports = self.root / "build" / "reports"

    def git(self, *args):
        rc, out = self.git_fn(self.root, *args)
        if rc != 0:
            raise Refuse(f"git {' '.join(args)} failed (exit {rc})")
        return out

    def backlog(self):
        try:
            b = json.loads(self.backlog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise Refuse(f"backlog unreadable: {e}") from None
        if not isinstance(b, dict) or not isinstance(b.get("slices"), list):
            raise Refuse("backlog has no slices list")
        return b

    def report(self, sid):
        if not conveyor.LANE_RE.fullmatch(sid):
            raise Refuse(f"{sid}: not a lane id")
        p = self.reports / f"{sid}.md"
        if not p.is_file():
            raise Refuse(f"{sid}: no report {p.relative_to(self.root).as_posix()}")
        status = conveyor.report_status(p)
        if status != "PASS":
            raise Refuse(f"{sid}: report STATUS is {status or 'missing'}, not PASS")
        return p.read_text(encoding="utf-8", errors="replace")

    def changed(self, sha):
        files = [f for f in self.git("show", "--name-only", "--format=", sha).splitlines() if f]
        if not files:
            raise Refuse(f"{sha[:12]}: commit changes no files")
        return files

    def slice_info(self, sid):
        """(sha40, changed files, codex?) from a PASS slice report; refuses when unclear."""
        text = self.report(sid)
        shas = {m.lower() for m in COMMIT_RE.findall(text)}
        if not shas:
            raise Refuse(f"{sid}: report has no COMMIT line")
        if len(shas) != 1:
            raise Refuse(f"{sid}: report names {len(shas)} COMMITs ({', '.join(sorted(shas))})")
        sha = shas.pop()
        if not SHA40.fullmatch(sha):
            raise Refuse(f"{sid}: COMMIT {sha} is not 40 hex")
        rc, kind = self.git_fn(self.root, "cat-file", "-t", sha)
        if rc != 0 or kind.strip() != "commit":
            raise Refuse(f"{sid}: COMMIT {sha} is not a commit")
        m = FILES_RE.search(text)
        listed = []
        for part in re.split(r"[;,]", m.group(1) if m else ""):
            p = re.sub(r"\s*\(.*?\)", "", part).strip(" \t`'\"")
            if p:
                listed.append(p)
        if not listed:
            raise Refuse(f"{sid}: report has no FILES")
        files = self.changed(sha)
        unknown = [p for p in listed if not PATH_RE.fullmatch(p) or p not in files]
        if unknown:
            raise Refuse(f"{sid}: FILES not changed by {sha[:12]}: {', '.join(unknown)}")
        codex = bool(WORKER_RE.search(text)) or bool(
            WORKER_RE.search(self.git("show", "-s", "--format=%B", sha)))
        return sha, files, codex

    def next_id(self, rx, prefix, ids):
        nums = [int(m.group(1)) for x in ids if (m := rx.match(x))]
        for d in (self.prompts, self.reports):
            if d.is_dir():
                nums += [int(m.group(1)) for f in os.listdir(d) if (m := rx.match(f))]
        return f"{prefix}{max(nums, default=0) + 1}"

    def review(self, sids):
        if not sids or len(set(sids)) != len(sids):
            raise Refuse("name each slice once")
        info = [(sid, *self.slice_info(sid)) for sid in sids]
        writers = {codex for *_, codex in info}
        if len(writers) != 1:
            raise Refuse("mixed writers (Codex and Claude): review them separately")
        b = self.backlog()
        ids = [s.get("id", "") for s in b["slices"]]
        codex = writers.pop()
        rid = self.next_id(REVIEW_NUM, "CLREV" if codex else "CDXINT", ids)
        names = " + ".join(sids)
        scope = "; ".join(f"{sid} {sha} ({', '.join(files)})" for sid, sha, files, _ in info)
        main = self.git("rev-parse", "refs/heads/main").strip()
        if codex:
            title = f"Claude inceleme: Codex işi {names}"
            body = (
                f"@jarvis lane={rid} model=opus effort=medium base=main\n"
                f'Read-only lane. "Claude independent review" of {names} WRITTEN BY THE CODEX WORKER '
                "(Codex must not review them). LANE RULES: build/conveyor/notes/LANE_RULES.md (CORE).\n"
                f"Scope: {scope}.\n"
                "Check: spec match; fail-closed on bad input; test strength (would a wrong implementation "
                "pass?); scope discipline. Short QUALITY NOTE vs a Claude lane. "
                f"OUTPUT: build/reports/{rid}.md. PASS = no REAL BLOCKER/MAJOR; else BLOCKED + one fix "
                "slice per file.\n"
                "Hard rules: READ-ONLY on source; never commit or push; never touch uv.lock; no "
                "reset/restore/stash/clean/checkout; no live calls. GREEN MEANS STOP.\n")
        else:
            title = f"Codex inceleme: {names}"
            body = (
                f"@jarvis lane={rid} model=opus effort=medium base=main\n"
                f"Read-only lane. Independent Codex review of {names}. Codex = the same command line as "
                "CDXINT63C\n(build/reviews/cdxint63/run.ps1: Sol 6.1, effort high, read-only sandbox, tools "
                "off, --ignore-user-config). Label it honestly.\n"
                "LANE RULES: build/conveyor/notes/LANE_RULES.md (CORE).\n"
                f"Scope: {scope}; main {main[:12]}.\n"
                f"1. Review pack: git --no-optional-locks show of {' '.join(sha for _, sha, _, _ in info)}. "
                "2. ONE Codex round (<= 2 calls): spec match, fail-closed on bad input, test strength "
                "(would a wrong implementation pass?), scope discipline. Severity + file:line.\n"
                f"3. Verify each finding yourself. OUTPUT: build/reports/{rid}.md. PASS = no REAL "
                "BLOCKER/MAJOR.\nIf Codex reports a usage limit: STATUS BLOCKED at once with the reset time.\n"
                "Hard rules: READ-ONLY on source; never commit or push; never touch uv.lock; no "
                "reset/restore/stash/clean/checkout; no live calls except the Codex round. GREEN MEANS STOP.\n")
        entry = {"id": rid, "title": title, "model": "opus", "effort": "medium", "tier": "T1",
                 "files": [f"build/reports/{rid}.md"], "depends_on": [],
                 "prompt": f"build/conveyor/prompts/{rid}.md"}
        return b, entry, body

    def picks(self, rid):
        """[(slice id, sha40, files)] named by a review prompt's Scope line."""
        p = self.prompts / f"{rid}.md"
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            raise Refuse(f"{rid}: no prompt build/conveyor/prompts/{rid}.md") from None
        m = SCOPE_RE.search(text)
        pairs = [x for x in PAIR_RE.findall(m.group(1)) if x[0].lower() != "main"] if m else []
        if not pairs:
            raise Refuse(f"{rid}: prompt has no 'Scope: <SLICE> <sha>' pairs")
        out = []
        for sid, short in pairs:
            sha, files, _ = self.slice_info(sid)
            if not conveyor.sha_matches(sha, short):
                raise Refuse(f"{rid}: reviewed {sid} {short}, but its report COMMIT is {sha}")
            out.append((sid, sha, files))
        return out

    def integration(self, rids):
        if not rids or len(set(rids)) != len(rids):
            raise Refuse("name each review once")
        picks = {}
        for rid in rids:
            self.report(rid)
            for sid, sha, files in self.picks(rid):
                if picks.setdefault(sid, (sha, files))[0] != sha:
                    raise Refuse(f"{sid}: two different commits named")
        shas = [sha for sha, _ in picks.values()]
        if len(set(shas)) != len(shas):
            raise Refuse("two slices name the same commit")
        order = []  # ponytail: O(n^2) ancestry checks, fine for a handful of picks
        todo = list(picks)
        while todo:
            for sid in todo:
                if not any(o != sid and self.is_ancestor(picks[o][0], picks[sid][0]) for o in todo):
                    order.append(sid)
                    todo.remove(sid)
                    break
            else:
                raise Refuse("picks have no ancestry order")
        files = sorted({f for _, fs in picks.values() for f in fs})
        groups = self.test_groups(files)
        if not groups:
            raise Refuse(f"no tests module for {', '.join(files)}")
        line = self.git("ls-remote", "origin", "refs/heads/main").split()
        if len(line) != 2 or not SHA40.fullmatch(line[0]):
            raise Refuse("origin/main not found by git ls-remote")
        base = line[0]
        b = self.backlog()
        iid = self.next_id(INT_NUM, "INT", [s.get("id", "") for s in b["slices"]])
        low, names = iid.lower(), " + ".join(order)
        runs = " | ".join(" ".join(g) for g in groups)
        body = (
            f"@jarvis lane={iid} model=sonnet effort=medium base=main\n"
            f"LANE RULES: read build/conveyor/notes/LANE_RULES.md (CORE + INT). Integration: {names}.\n"
            "CANDIDATE FLOW (CONVINT; do NOT move local main yourself):\n"
            f"0. PREFLIGHT (BLOCKED if any fails): local main == origin/main == {base}; "
            f"{', '.join(f'build/reports/{r}.md' for r in rids)} STATUS PASS; "
            f"{', '.join(f'{s}.md' for s in order)} PASS naming the COMMITs below.\n"
            f"1. In your worktree: git switch -c cand/{low} {base}; cherry-pick in order, each its OWN "
            f"commit only: {', '.join(f'{s} {picks[s][0]}' for s in order)}.\n"
            f"   Expected files only: {', '.join(files)}. Anything else or a conflict: STATUS BLOCKED "
            "naming it.\n"
            f"2. Checks: git diff --check {base}..HEAD; ruff E4,E7,E9,F; SCHEMA_VERSION == 10; "
            f"python scripts/test_parallel.py, one group per run: {runs} (0 failures each; NO local "
            "full suite, O28). No live calls.\n"
            f"3. git push origin HEAD:refs/heads/ci/{low} (never --force, never main).\n"
            f"Report build/reports/{iid}.md: STATUS: CI_PENDING (or BLOCKED); exactly ONE "
            '"CANDIDATE SHA: <40 hex>" line and NO "NEW MAIN SHA" line;\n'
            f"CI BRANCH: ci/{low}; picks + test counts. Name NO other CI branch. Do NOT restart the "
            "conveyor.\n"
            "Hard rules: never push main; never touch uv.lock; explicit paths; no "
            "reset/restore/stash/clean; tests never open data/jarvis.db. GREEN MEANS STOP.\n")
        entry = {"id": iid, "title": f"Entegrasyon {iid}: {names}", "model": "sonnet",
                 "effort": "medium", "tier": "T1", "files": ["@main-checkout"], "depends_on": [],
                 "prompt": f"build/conveyor/prompts/{iid}.md"}
        return b, entry, body

    def is_ancestor(self, a, b):
        rc, _ = self.git_fn(self.root, "merge-base", "--is-ancestor", a, b)
        if rc not in (0, 1):
            raise Refuse(f"git merge-base --is-ancestor {a[:12]} {b[:12]} failed (exit {rc})")
        return rc == 0

    def test_groups(self, files):
        mods, ui = [], False
        for f in files:
            if f.startswith("jarvis/ui/") or f.endswith(".qml"):
                ui = True
            name = Path(f).name
            if f.startswith("tests/") and name.startswith("test_") and name.endswith(".py"):
                mod = f[:-3].replace("/", ".")
            elif f.startswith("jarvis/") and name.endswith(".py") and name != "__init__.py" and (
                    f"tests/{'test_' + name}" in files or (self.root / "tests" / f"test_{name}").is_file()):
                mod = f"tests.test_{name[:-3]}"
            else:
                continue
            if mod != "tests.test_ui" and mod not in mods:
                mods.append(mod)
            ui = ui or mod == "tests.test_ui"
        return [g for g in (mods, ["tests.test_ui"] if ui else []) if g]

    def lint(self, b, entry, body):
        ids = {s.get("id") for s in b["slices"]}
        if entry["id"] in ids or (self.root / entry["prompt"]).exists():
            raise Refuse(f"{entry['id']} already exists")
        why = conveyor.validate_slice(entry)
        if why is None:
            why = conveyor.check_header(body)[1]
        if why is None:
            why = conveyor.lint_slice(entry, body, lambda p: (self.root / p).exists(), ids)
        if why:
            raise Refuse(f"{entry['id']}: generated slice fails the conveyor lint: {why}")

    def emit(self, b, entry, body, write):
        self.lint(b, entry, body)
        if not write:
            self.out(f"dry-run: would write {entry['prompt']}:\n{body}\n"
                     f"dry-run: would append to build/conveyor/backlog.json:\n"
                     f"{json.dumps(entry, indent=2, ensure_ascii=False)}\n(use --write)")
            return 0
        hold = self.root / "build" / "conveyor" / "hold"
        hold.mkdir(parents=True, exist_ok=True)
        backup = hold / f"backlog.json.bak-{entry['id']}"
        raw = self.backlog_path.read_bytes()
        with open(backup, "xb") as f:  # never overwrite an earlier backup
            f.write(raw)
        self.prompts.mkdir(parents=True, exist_ok=True)
        with open(self.root / entry["prompt"], "x", encoding="utf-8", newline="\n") as f:
            f.write(body)
        b["slices"].append(entry)
        tmp = self.backlog_path.with_name("backlog.json.orchgen-tmp")
        tmp.write_text(json.dumps(b, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
        os.replace(tmp, self.backlog_path)
        self.out(f"wrote {entry['prompt']}, appended {entry['id']} (backup "
                 f"{backup.relative_to(self.root).as_posix()})")
        r = subprocess.run([sys.executable, str(self.root / "build" / "orch" / "check_backlog.py")],
                           cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.out((r.stdout + r.stderr).strip())
        if r.returncode != 0:
            self.out(f"check_backlog failed (exit {r.returncode}); backup at {backup}")
            return 1
        return 0


def main(argv=None, git=real_git, out=print):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                    help="main checkout (holds build/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("review", "int"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="+")
        m = p.add_mutually_exclusive_group()
        m.add_argument("--write", action="store_true", help="write the prompt and the backlog entry")
        m.add_argument("--dry-run", action="store_true", help="print only (the default)")
    a = ap.parse_args(argv)
    g = Gen(a.root, git=git, out=out)
    try:
        made = g.review(a.ids) if a.cmd == "review" else g.integration(a.ids)
        return g.emit(*made, a.write)
    except (Refuse, OSError) as e:
        out(f"REFUSED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
