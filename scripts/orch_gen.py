"""Deterministic generator for the orchestrator's routine prompts (ORCHGEN).

    python scripts/orch_gen.py [--root DIR] review <SLICE>... [--with-partial ID]... [--focus TEXT] [--write]
    python scripts/orch_gen.py [--root DIR] int <REVIEW>... [--write]

review: reads build/reports/<SLICE>.md (STATUS PASS, one 40-hex COMMIT, FILES, a "Worker: codex"
line) and writes an independent review prompt: CLREVnn (Claude) for work the Codex worker wrote,
CDXINTnn (Codex) for work Claude wrote, plus a backlog entry with the next free number.
FILES entries lose "(...)" annotations; the lane's own report is ignored; a "Worker: jules" report
without FILES uses git show --name-only. --with-partial ID adds a BLOCKED "partial" predecessor
only when a reviewed report names it as BASE: <ID> AT <sha> and builds on that commit; a plain
"STATUS: BLOCKED" predecessor needs that BASE sha to be its full 40-hex COMMIT. The prompt carries
"Spec:" (the slices' prompts and reports, + the PM plan/decisions for PM ids) and "Focus: <TEXT>";
--write without --focus refuses.
int: once the named reviews are PASS, writes INTnn: base = origin/main (git ls-remote), picks in
ancestry order (a BLOCKED pick only as a stacked predecessor, same rule), expected files = union of
git show --name-only, their tests modules + tests importing a changed jarvis module, matching
*_replay tests, memory-schema tests for memory.py/contracts.py (+ tests.test_ui only when a UI
file is touched), CI BRANCH ci/<intnn>.
Dry run is the default (prints what it would write). --write writes the prompt and the backlog entry,
keeps a backup of the backlog in build/conveyor/hold/ and runs build/orch/check_backlog.py.
"slice --spec S.json" / "fix --spec S.json" write a SKELETONS 1 feature slice / a stacked
SKELETONS 2 FIX (stack = source stack + source COMMIT, review base = parent(stack[0]),
stack-only grant = every stack file) from a short JSON spec; they also run
build/orch/preflight.py and roll back the backlog and prompt on any error.
"fill [--track n6|improve|auto]" (ALLOWLIST 5): when orch_digest says a track is queue_low, turns
the highest-priority, oldest approved READY spec build/orch/ready/<track>/<seq>-<id>.json (a slice
spec + track, priority, approved_by, decision) into a slice the slice way (preflight, T5 cap);
--write moves the spec to ready/used/<UTC date>/; every fill appends a line to ready/fill.jsonl.
"depot-fill [--class n6|improve|auto] [--propose ID]" (QUEUEFILL2, never run unless invoked):
strict depot specs build/orch/specs/<id>.json, each approved by a build/orch/specs/approvals/<id>.json
bound to its sha256; a low class gets the deterministic head(s) at base = origin/main (40 hex) under
a lock with an intent/receipt journal and state in build/orch/specs/_run/.
Anything unclear refuses (exit 2) with the reason. Never edits conveyor.py, never pushes, never
writes approvals (the entry carries no "approved" key).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conveyor  # noqa: E402  (read-only: the conveyor's own validators)
import orch_decisions  # noqa: E402  (read-only: fill's decision check)
import orch_digest  # noqa: E402  (read-only: fill's queue_low view)

SHA40 = re.compile(r"[0-9a-f]{40}")
COMMIT_RE = re.compile(r"(?m)^[\s>#*`-]*COMMIT:[\s`]*([0-9a-fA-F]{7,40})\b")
# FILES value plus indented or bulleted continuation lines that are not another upper-case
# "LABEL:" line (any other indented line stays FILES content and must be a changed path)
FILES_RE = re.compile(r"(?m)^[\s>#*`-]*FILES:[ \t]*(.*(?:\n(?:[ \t]+|[ \t]*[-*][ \t]+)"
                      r"(?![-*]?[ \t]*[A-Z][A-Z0-9 _-]*:)\S.*)*)")
WORKER_RE = re.compile(r"(?im)^[\s>#*`-]*worker:[\s`]*codex\b")
JULES_RE = re.compile(r"(?im)^[\s>#*`-]*worker:[\s`]*jules\b")
SCOPE_RE = re.compile(r"(?m)^.*?\bScope:(.*)$")
PAIR_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9._-]*) `?([0-9a-f]{7,40})\b")
PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
SIZES = {"SMALL": 35, "MEDIUM": 50}  # tool-call budget per size (SKELETONS 1/2)
REVIEW_NUM = re.compile(r"(?i)^(?:CDXINT|CLREV)(\d+)")
INT_NUM = re.compile(r"(?i)^INT(\d+)")


PARTIAL_RE = re.compile(r"(?i)STATUS:[\s*`]*BLOCKED[\s*`]*[(:,-]?[ \t]*partial\b")
PLAIN_BLOCKED_RE = re.compile(r"(?i)STATUS:[\s*`]*BLOCKED[\s*`.]*")
PM_SPECS = ("build/orch/PLAN_PROJECT_MODE.md", "build/orch/kb/PM_DECISIONS.md")
FOCUS_PLACEHOLDER = "<FOCUS: fill in before --write>"


class Refuse(Exception):
    pass


def status_line(text):
    """The same STATUS line conveyor.report_status reads (first 15 non-blank lines)."""
    lines = [s for s in (x.lstrip(" \t>#*`﻿").rstrip() for x in text.splitlines()) if s][:15]
    return next((s for s in lines if conveyor.STATUS_RE.match(s)), "")


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

    def report(self, sid, partial=False):
        """Report text; PASS, or with partial=True a BLOCKED report whose STATUS line says partial."""
        if not conveyor.LANE_RE.fullmatch(sid):
            raise Refuse(f"{sid}: not a lane id")
        p = self.reports / f"{sid}.md"
        if not p.is_file():
            raise Refuse(f"{sid}: no report {p.relative_to(self.root).as_posix()}")
        status = conveyor.report_status(p)
        text = p.read_text(encoding="utf-8", errors="replace")
        if not partial:
            if status != "PASS":
                raise Refuse(f"{sid}: report STATUS is {status or 'missing'}, not PASS")
            return text
        line = status_line(text)
        if status != "BLOCKED" or not (PARTIAL_RE.match(line) or PLAIN_BLOCKED_RE.fullmatch(line)):
            raise Refuse(f"{sid}: report STATUS is {status or 'missing'}, not BLOCKED (partial ...) "
                         "or a plain BLOCKED")
        return text

    def changed(self, sha):
        files = [f for f in self.git("show", "--name-only", "--format=", sha).splitlines() if f]
        if not files:
            raise Refuse(f"{sha[:12]}: commit changes no files")
        return files

    def commit_of(self, sid, text):
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
        return sha

    def is_codex(self, text, sha):
        return bool(WORKER_RE.search(text)) or bool(
            WORKER_RE.search(self.git("show", "-s", "--format=%B", sha)))

    def slice_info(self, sid):
        """(sha40, changed files, codex?) from a PASS slice report; refuses when unclear."""
        text = self.report(sid)
        sha = self.commit_of(sid, text)
        codex = self.is_codex(text, sha)
        m = FILES_RE.search(text)
        # the conveyor writes Jules reports without FILES, with "BRANCH: jules/<ID>"
        if not m and JULES_RE.search(text) and re.search(rf"(?m)^BRANCH:[ \t]*jules/{re.escape(sid)}[ \t]*$",
                                                          text):
            if codex:
                raise Refuse(f"{sid}: report names both the Jules and the Codex worker")
            return sha, self.changed(sha), codex
        raw = m.group(1) if m else ""
        while (s := re.sub(r"\([^()]*\)", "", raw)) != raw:  # annotations, innermost first
            raw = s
        own = f"build/reports/{sid}.md"
        listed = []
        for part in re.split(r"[,;\n]", raw):
            p = re.sub(r"^[ \t]*[-*][ \t]+", "", part).strip(" \t`'\"")
            if p and p.replace("\\", "/") != own:  # the lane's own report is not in its commit
                listed.append(p)
        if not listed:
            raise Refuse(f"{sid}: report has no FILES")
        files = self.changed(sha)
        # a stack-only file (listed, in the tree at sha, no pick changes it) is no refusal: the INT's
        # expected files come from the commits' real changes, so it never enters the INT
        unknown = [p for p in listed if not PATH_RE.fullmatch(p) or (
            p not in files and self.git_fn(self.root, "cat-file", "-e", f"{sha}:{p}")[0] != 0)]
        if unknown:
            raise Refuse(f"{sid}: FILES not changed by {sha[:12]}: {', '.join(unknown)}")
        return sha, files, codex

    def partial_info(self, pid, info):
        """(pid, sha40, files, codex?, later id) for a BLOCKED partial slice that a later reviewed
        slice names as its BASE (BASE: <pid> AT <sha>) and builds on; refuses otherwise."""
        text = self.report(pid, partial=True)
        sha = self.commit_of(pid, text)
        # a plain BLOCKED (no "partial" marker) needs the full 40-hex COMMIT as the named BASE
        plain = not PARTIAL_RE.match(status_line(text))
        base_re = re.compile(rf"(?m)^[\s>#*`-]*BASE:[ \t`]*{re.escape(pid)}[ \t`]+(?i:AT)[ \t`]+"
                             r"([0-9a-fA-F]{7,40})\b")
        later = []
        for sid, lsha, *_ in info:
            named = base_re.findall((self.reports / f"{sid}.md").read_text(encoding="utf-8", errors="replace"))
            if any(n.lower() != sha if plain else not conveyor.sha_matches(sha, n.lower()) for n in named):
                raise Refuse(f"{sid}: BASE {pid} is not {pid}'s COMMIT {sha[:12]}")
            if named:
                later.append((sid, lsha))
        if not later:
            raise Refuse(f"{pid}: partial, but no reviewed report names BASE: {pid} AT {sha[:12]}")
        for sid, lsha in later:
            if not self.is_ancestor(sha, lsha):
                raise Refuse(f"{sid}: {lsha[:12]} does not build on {pid} {sha[:12]}")
        return pid, sha, self.changed(sha), self.is_codex(text, sha), later[0][0]

    def next_id(self, rx, prefix, ids):
        nums = [int(m.group(1)) for x in ids if (m := rx.match(x))]
        for d in (self.prompts, self.reports):
            if d.is_dir():
                nums += [int(m.group(1)) for f in os.listdir(d) if (m := rx.match(f))]
        return f"{prefix}{max(nums, default=0) + 1}"

    def review(self, sids, partials=(), focus=None):
        if not sids or len({*sids, *partials}) != len(sids) + len(partials):
            raise Refuse("name each slice once")
        if focus is not None and (not focus.strip() or not focus.isprintable()):
            raise Refuse("focus must be one non-empty line")
        info = [(sid, *self.slice_info(sid)) for sid in sids]
        tags = {}
        for pid in partials:
            pid, sha, files, codex, later = self.partial_info(pid, info)
            info.insert(0, (pid, sha, files, codex))
            tags[pid] = f" [partial, finished by {later}]"
        writers = {codex for *_, codex in info}
        if len(writers) != 1:
            raise Refuse("mixed writers (Codex and Claude): review them separately")
        b = self.backlog()
        ids = [s.get("id", "") for s in b["slices"]]
        codex = writers.pop()
        rid = self.next_id(REVIEW_NUM, "CLREV" if codex else "CDXINT", ids)
        names = " + ".join(sids)
        scope = "; ".join(f"{sid} {sha} ({', '.join(files)}){tags.get(sid, '')}" for sid, sha, files, _ in info)
        main = self.git("rev-parse", "refs/heads/main").strip()
        if tags:
            scope += f"; review as one diff main..{info[-1][1][:12]}"
        specs = [p for sid, *_ in info for p in (f"build/conveyor/prompts/{sid}.md", f"build/reports/{sid}.md")]
        if any(sid.upper().startswith("PM") for sid, *_ in info):
            specs += PM_SPECS
        spec = f"Spec: {', '.join(specs)}.\nFocus: {focus or FOCUS_PLACEHOLDER}\n"
        if codex:
            title = f"Claude inceleme: Codex işi {names}"
            body = (
                f"@jarvis lane={rid} model=opus effort=medium base=main\n"
                f'Read-only lane. "Claude independent review" of {names} WRITTEN BY THE CODEX WORKER '
                "(Codex must not review them). LANE RULES: build/conveyor/notes/LANE_RULES.md (CORE).\n"
                f"Scope: {scope}.\n{spec}"
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
                f"Scope: {scope}; main {main[:12]}.\n{spec}"
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
        out, stacked = [], []
        for sid, short in pairs:
            if not conveyor.LANE_RE.fullmatch(sid):
                raise Refuse(f"{sid}: not a lane id")
            if conveyor.report_status(self.reports / f"{sid}.md") == "BLOCKED":
                stacked.append((sid, short))  # only as a stacked predecessor, checked below
                continue
            sha, files, codex = self.slice_info(sid)
            if not conveyor.sha_matches(sha, short):
                raise Refuse(f"{rid}: reviewed {sid} {short}, but its report COMMIT is {sha}")
            out.append((sid, sha, files, None))
        for sid, short in stacked:
            _, sha, files, _, later = self.partial_info(sid, out)
            if not conveyor.sha_matches(sha, short):
                raise Refuse(f"{rid}: reviewed {sid} {short}, but its report COMMIT is {sha}")
            out.insert(0, (sid, sha, files, later))
        return out

    def integration(self, rids):
        if not rids or len(set(rids)) != len(rids):
            raise Refuse("name each review once")
        picks = {}
        for rid in rids:
            self.report(rid)
            for sid, sha, files, later in self.picks(rid):
                if picks.setdefault(sid, (sha, files, later))[0] != sha:
                    raise Refuse(f"{sid}: two different commits named")
        shas = [sha for sha, *_ in picks.values()]
        if len(set(shas)) != len(shas):
            raise Refuse("two slices name the same commit")
        owner = {}  # ALLOWLIST T3: batch INT scopes disjoint; only a slice and its own FIX may share a file
        for sid in sorted(picks):
            for f in picks[sid][1]:
                o = owner.setdefault(f, sid)
                if o != sid and re.sub(r"(?:FIX\d*)+$", "", o) != re.sub(r"(?:FIX\d*)+$", "", sid):
                    raise Refuse(f"scopes not disjoint: {f} in {o} and {sid}")
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
        files = sorted({f for _, fs, _ in picks.values() for f in fs})
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

        def pre(s):
            sha, _, later = picks[s]
            return (f"{s}.md BLOCKED (stacked: {later}.md names BASE: {s} AT {sha})" if later
                    else f"{s}.md PASS")
        body = (
            f"@jarvis lane={iid} model=sonnet effort=medium base=main\n"
            f"LANE RULES: read build/conveyor/notes/LANE_RULES.md (CORE + INT). Integration: {names}.\n"
            "CANDIDATE FLOW (CONVINT; do NOT move local main yourself):\n"
            f"0. PREFLIGHT (BLOCKED if any fails): local main == origin/main == {base}; "
            f"{', '.join(f'build/reports/{r}.md' for r in rids)} STATUS PASS; "
            f"{', '.join(pre(s) for s in order)} naming the COMMITs below.\n"
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
        for mod in self.importer_tests(files):
            if mod == "tests.test_ui":
                ui = True
            elif mod not in mods:
                mods.append(mod)
        return [g for g in (mods, ["tests.test_ui"] if ui else []) if g]

    def importer_tests(self, files):
        """Sorted tests modules that import a changed jarvis module (static scan of
        tests/test_*.py), replay tests named after one, and the memory-schema tests when
        jarvis/memory.py or jarvis/contracts.py changed."""
        changed = {f[:-3].replace("/", ".").removesuffix(".__init__") for f in files
                   if f.startswith("jarvis/") and f.endswith(".py")}
        stems = {Path(f).stem for f in files if f.startswith("jarvis/") and f.endswith(".py")}
        memory = bool({"jarvis/memory.py", "jarvis/contracts.py"} & set(files))
        if memory:
            changed.add("jarvis.memory")
        if not changed:
            return []
        found = []
        for p in sorted((self.root / "tests").glob("test_*.py")):
            stem = p.stem
            try:
                tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
            except (SyntaxError, ValueError):
                # ponytail: an unparsable test may import anything; include it (fail-closed)
                found.append(f"tests.{stem}")
                continue
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names.add(node.module)
                    names.update(f"{node.module}.{a.name}" for a in node.names)
            hit = any(n == m or n.startswith(m + ".") for n in names for m in changed)
            core = stem.removeprefix("test_").removesuffix("_replay")
            if (hit or (stem.endswith("_replay") and core in stems)
                    or (memory and stem.startswith("test_memory"))):
                found.append(f"tests.{stem}")
        return found

    def spec(self, path, fix):
        """A slice/fix spec (JSON object); refuses on any missing or bad field."""
        try:  # a dict is a spec the depot already parsed and vetted (depot_slice)
            sp = dict(path) if isinstance(path, dict) else json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise Refuse(f"spec unreadable: {e}") from None
        if not isinstance(sp, dict):
            raise Refuse("spec is not a JSON object")
        need = ["id", "title", "tier", "model", "effort", "size", "goal", "tests", "run", "owned", "commit"]
        need += ["review", "source"] if fix else []
        for k in need:
            if not sp.get(k):
                raise Refuse(f"spec has no {k}")
        for k in ("run", "owned"):
            if not isinstance(sp[k], list) or not all(isinstance(x, str) and PATH_RE.fullmatch(x) for x in sp[k]):
                raise Refuse(f"spec {k} must be a list of plain names")
        if not conveyor.LANE_RE.fullmatch(sp["id"]) or not conveyor.LANE_RE.fullmatch(sp.get("source", "x")):
            raise Refuse("spec id/source is not a lane id")
        for k in ("max_calls", "calls"):
            if k in sp and (type(sp[k]) is not int or sp[k] <= 0):
                raise Refuse(f"spec {k} must be a positive integer")
        if sp["size"] not in SIZES and not any(isinstance(sp.get(k), int) for k in ("max_calls", "calls")):
            raise Refuse(f"spec size {sp['size']} needs max_calls (known: {', '.join(SIZES)})")
        tests = sp["tests"]
        if isinstance(tests, list) and not all(isinstance(t, str) and t for t in tests):
            raise Refuse("spec tests list holds non-empty strings")
        flat = [sp.get(k, "") for k in ("title", "goal", "decisions", "commit")] + (
            tests if isinstance(tests, list) else [tests])
        if any("\n" in str(x) for x in flat):
            raise Refuse("spec text fields are one line each")
        return sp

    def owned_line(self, paths):
        """Owned paths, "(new)" on each one main does not have."""
        return ", ".join(p if self.git_fn(self.root, "cat-file", "-e", f"main:{p}")[0] == 0 else f"{p} (new)"
                         for p in paths)

    def from_spec(self, path, fix):
        """(backlog, entry, body) for a SKELETONS 1 feature slice or a SKELETONS 2 stacked FIX."""
        sp, b = self.spec(path, fix), self.backlog()
        sid, rep = sp["id"], f"build/reports/{sp['id']}.md"
        owned = [p for p in sp["owned"] if p != rep]
        calls = sp.get("max_calls") or sp.get("calls") or SIZES[sp["size"]]
        chain = fix or sp.get("chain") is True  # a stacked FIX is always chained (validate_slice)
        entry = {"id": sid, "title": sp["title"], "model": sp["model"], "effort": sp["effort"],
                 "tier": sp["tier"], "files": owned + [rep], "depends_on": list(sp.get("depends_on") or []),
                 "prompt": f"build/conveyor/prompts/{sid}.md"}
        base, mid, grant = "main", [], []
        if fix:
            src = next((s for s in b["slices"] if s.get("id") == sp["source"]), None)
            if src is None:
                raise Refuse(f"{sp['source']}: not in the backlog")
            p = self.reports / f"{sp['review']}.md"
            if not conveyor.LANE_RE.fullmatch(sp["review"]) or not p.is_file():
                raise Refuse(f"{sp['review']}: no review report")
            flines = [m.group(0).strip(" \t>*`-") for m in conveyor.AUTOFIX_F_RE.finditer(
                p.read_text(encoding="utf-8", errors="replace"))]
            fids = sorted({x.split()[0] for x in flines}, key=lambda x: int(x[1:]))
            if not fids:
                raise Refuse(f"{sp['review']}: report has no F<n> finding line")
            stack = list(src.get("stack") or []) + [self.commit_of(sp["source"], self.report(sp["source"]))]
            if len(set(stack)) != len(stack):
                raise Refuse(f"{sp['source']}: its COMMIT is already on the stack")
            rb = self.git("rev-parse", f"{stack[0]}^").strip()
            for sha in stack:
                grant += [f for f in self.changed(sha) if f not in owned and f not in grant]
            entry["files"] = owned + grant + [rep]
            entry.update(stack=stack, review_base=rb, series=src.get("series") or sp["source"])
            mid = [f"BASE AT {stack[-1]} (stack {' -> '.join(x[:7] for x in stack)}; review base {rb[:7]})",
                   f"READ FIRST: build/reports/{sp['review']}.md ({', '.join(fids)})"]
            mid += ["Findings (data):"] + [f"    > {x}" for x in flines]
        elif chain:
            base = self.git("rev-parse", "refs/heads/main").strip()
            if not SHA40.fullmatch(base):
                raise Refuse(f"main is {base!r}, not a 40-hex sha")
        if chain:
            entry["chain"] = ["CLREV", "CDX", "INT"]
        head = f"{sid}: {sp['tier']}, {sp['size']} <= {calls}" + (
            "." if fix else " tool calls" + (", SPLIT-OK." if sp.get("split_ok") else "."))
        lines = [f"@jarvis lane={sid} model={sp['model']} effort={sp['effort']} base={base}",
                 f"LANE RULES: build/conveyor/notes/LANE_RULES.md (CORE{' + FIX' if fix else ''})",
                 head, *(["CHAIN: CLREV+CDX"] if chain else []),
                 *([] if fix else [f"Goal: {sp['goal']}"]), *mid,
                 *([f"Goal: {sp['goal']}"] if fix else []),
                 *([f"Decisions: {sp['decisions']}"] if sp.get("decisions") else []),
                 *(["Tests:", *(f"- {t}" for t in sp["tests"])] if isinstance(sp["tests"], list)
                   else [f"Tests: {sp['tests']}"]), f"Run: python scripts/test_parallel.py {' '.join(sp['run'])}",
                 f"Owned: {self.owned_line(owned)}, {rep} (new)",
                 *([f"Stack-only grant (do not edit): {', '.join(grant)}"] if grant else []),
                 f'Commit: "{sp["commit"]}"']
        return b, entry, "\n".join(lines) + "\n"

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

    def emit(self, b, entry, body, write, strict=False):
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
        checks = ["check_backlog.py"] + (["preflight.py"] if strict else [])
        for name in checks:
            r = subprocess.run([sys.executable, str(self.root / "build" / "orch" / name), str(self.root)],
                               cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.out((r.stdout + r.stderr).strip())
            if r.returncode != 0:
                if strict:  # slice/fix: refuse on any error, leave nothing behind but the backup
                    self.backlog_path.write_bytes(raw)
                    (self.root / entry["prompt"]).unlink()
                    self.out(f"REFUSED: {name} failed (exit {r.returncode}); backlog restored, prompt removed")
                else:
                    self.out(f"{name} failed (exit {r.returncode}); backup at {backup}")
                return 1
        return 0


DAILY_CAP = {"slice": 4, "fix": 6}  # ALLOWLIST T5: --write per UTC day (item 5 new slice, item 2 FIX)


def daily_count(root, cmd, at, add=0):
    """Today's --write count for cmd from build/orch/gen_caps.json; a corrupt file fails closed."""
    path = Path(root) / "build" / "orch" / "gen_caps.json"
    try:
        caps = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        caps = {}
    except ValueError as e:
        raise Refuse(f"ALLOW-T5 cap file unreadable ({e}); escalate to the orchestrator")
    if not isinstance(caps, dict) or not all(type(v) is int and v >= 0 and re.fullmatch(r"(?:%s):\d{4}-\d\d-\d\d" % "|".join(DAILY_CAP), k)
                                             for k, v in caps.items()):
        raise Refuse("ALLOW-T5 cap file malformed; escalate to the orchestrator")
    day = max([time.strftime("%Y-%m-%d", time.gmtime(at))] +
              [k[len(cmd) + 1:] for k in caps if k.startswith(cmd + ":")])  # a backward clock keeps the newest day
    key = f"{cmd}:{day}"
    if add:
        caps = {k: v for k, v in caps.items() if k == key or not k.startswith(cmd + ":")}  # drop old days
        caps[key] = caps.get(key, 0) + add
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(caps, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    return caps.get(key, 0)


TRACKS = (("n6", "[N6]"), ("improve", "[IYILESTIRME"))  # (track, title prefix); fill order N6 > improvement
APPROVERS = ("orchestrator", "brain", "salih")
READY_NAME = re.compile(r"(\d{1,9})-([A-Za-z0-9][A-Za-z0-9._-]{0,63})\.json")  # <seq>-<slice id>.json
STATE_INPUTS = {"input:status.json", "input:status.backlog", "input:lanes.json", "input:backlog.json"}


def ready_skip(g, p, sp, ids, taken):
    """Why the READY spec p (parsed: sp) cannot become a slice now, or None."""
    if sp.get("approved_by") not in APPROVERS:
        return "unapproved"
    if not orch_decisions.require("authority", sp.get("decision"), g.root / orch_decisions.REL)[0]:
        return "inactive_decision"
    if sp["id"] in ids or (g.prompts / f"{sp['id']}.md").exists():
        return "id_exists"
    try:
        b, entry, body = g.from_spec(p, False)
        g.lint(b, entry, body)
    except (Refuse, OSError, ValueError, TypeError, KeyError):
        return "malformed"
    if any(conveyor.files_overlap(entry["files"], f) for f in taken):
        return "overlap"
    return None


def fill(g, track, write, t, rec):
    """ALLOWLIST 5: the best usable READY spec of the first low track -> a slice. rec = the fill.jsonl line."""
    now = datetime.fromtimestamp(t).replace(microsecond=0)
    d = orch_digest.build(g.root, now, now - timedelta(hours=24))
    if any(i["reason"] == "unknown" and i["id"] in STATE_INPUTS for i in d["items"]):
        rec["reason"] = "state_unknown"  # a missing status.json reads as "0 ready": never fill on it
        raise Refuse("conveyor state unreadable; cannot tell which track is low")
    low = [n for n, _ in TRACKS if any(i["reason"] == f"queue_low_{n}" for i in d["items"])]
    tr = low[0] if low and track == "auto" else track if track in low else None
    if tr is None:
        rec.update(result="no_track_low", reason="not_low")
        g.out(f"fill: track {track}: not queue_low; nothing to fill")
        return 0
    rec.update(track=tr, reason=f"queue_low_{tr}")
    lanes = orch_digest.load(g.root / "build" / "conveyor" / "lanes.json", [], None)
    if not isinstance(lanes, dict):
        rec["reason"] = "state_unknown"
        raise Refuse("lanes.json is not an object")
    b = g.backlog()
    ids = {s.get("id") for s in b["slices"]}
    # running lanes + slices not started yet (no lane, no report: conveyor slice_hold's own test)
    live = [v for v in lanes.values() if isinstance(v, dict) and not v.get("finished") and v.get("state") != "missing"]
    queued = [s for s in b["slices"] if s.get("id") not in lanes and not (g.reports / f"{s.get('id')}.md").is_file()]
    taken = [x.get("files") or [] for x in live + queued]
    specs, skipped = [], []
    for p in sorted((g.root / "build" / "orch" / "ready" / tr).glob("*.json")):
        m = READY_NAME.fullmatch(p.name)
        sp = orch_digest.load(p, [], None) if m else None
        if (isinstance(sp, dict) and sp.get("id") == m.group(2) and sp.get("track") == tr
                and str(sp.get("title", "")).startswith(dict(TRACKS)[tr]) and type(sp.get("priority")) is int):
            specs.append((-sp["priority"], int(m.group(1)), p.name, p, sp))
        else:
            skipped.append((p.name if m else "<bad-name>", "malformed"))
    pick = None
    for _, _, name, p, sp in sorted(specs, key=lambda x: x[:3]):  # highest priority, then oldest seq
        why = ready_skip(g, p, sp, ids, taken)
        if why is None:
            pick = (name, p)
            break
        skipped.append((name, why))
    rec["skipped"] = [f"{n}:{w}" for n, w in skipped]
    for n, w in skipped:
        g.out(f"fill: skipped {n}: {w}")
    if pick is None:
        rec["result"] = "queue_empty"
        g.out(f"fill: {tr} is queue_low but no READY spec is usable: queue_empty")
        return 1
    name, p = pick
    rec["spec"] = name
    g.out(f"fill: {tr} is queue_low ({d['ready'].get(tr)} ready); picked {tr}/{name}")
    if not write:
        rec["result"] = "dry_run"
        return g.emit(*g.from_spec(p, False), False)
    if daily_count(g.root, "slice", t) >= DAILY_CAP["slice"]:  # shares the slice counter (ALLOWLIST T5 item 5)
        rec["reason"] = "cap_reached"
        raise Refuse(f"ALLOW-T5 daily slice cap {DAILY_CAP['slice']} reached; escalate to the orchestrator")
    dest = g.root / "build" / "orch" / "ready" / "used" / time.strftime("%Y-%m-%d", time.gmtime(t)) / name
    if dest.exists():
        rec["reason"] = "used_exists"
        raise Refuse(f"{dest} already exists")
    if g.emit(*g.from_spec(p, False), True, strict=True):
        rec["reason"] = "preflight_failed"
        return 1
    daily_count(g.root, "slice", t, add=1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(p, dest)  # kept, never deleted
    rec["result"] = "written"
    g.out(f"fill: moved {tr}/{name} to {dest.relative_to(g.root).as_posix()}")
    return 0


def fill_log(root, t, rec):
    """One build/orch/ready/fill.jsonl line: fixed codes and file names only, never free text."""
    path = Path(root) / "build" / "orch" / "ready" / "fill.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    at = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"at": at, **rec}, sort_keys=True) + "\n")


# QUEUEFILL2: the spec depot build/orch/specs/<id>.json. Off by default: only `depot-fill --write` writes,
# and only under build/orch/specs/_run/ plus the one emitted slice (backlog.json + its prompt).
DEPOT = Path("build") / "orch" / "specs"
SPEC_KEYS = {"id", "class", "tier", "order", "objective", "owned_files", "acceptance", "negative_tests",
             "depends_on", "worker", "effort", "contracts"}
SPEC_MAX, APPROVAL_MAX, INBOX_MAX = 16384, 1024, 16  # bytes per spec / approval record, items in the inbox
CLASSES = ("n6", "improve")  # N5: n6 before improve
TITLE = {"n6": "[N6] ", "improve": "[IYILESTIRME] "}  # orch_digest counts ready by these prefixes
LOW, TARGET, PER_ROUND = 2, 3, 2  # P2: trigger below LOW ready, fill toward TARGET, at most PER_ROUND a round
DEPOT_CAP = {"n6": 2, "improve": 2}  # N8: depot slices per class per UTC day (reviewed code, no data file)
AT_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
OWNED_RE = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_./-]*)( \(new\))?")
INBOX_RE = re.compile(r"spec_needed:(n6|improve)|queue_empty|unknown_outcome:[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key {k!r}")
        d[k] = v
    return d


def parse_spec(raw, stem=None):
    """(spec, sha256 of these exact bytes); N13: exact key set, typed fields, one line each, <= SPEC_MAX."""
    if len(raw) > SPEC_MAX:
        raise Refuse(f"N13 spec over {SPEC_MAX} bytes")
    try:
        sp = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes)
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        raise Refuse(f"N13 spec unreadable: {e}") from None
    if not isinstance(sp, dict) or set(sp) != SPEC_KEYS:
        raise Refuse(f"N13 spec fields must be exactly {', '.join(sorted(SPEC_KEYS))}")

    def one(x):
        return isinstance(x, str) and 0 < len(x) <= 300 and x.isprintable()

    def lines(x, need=False):
        return isinstance(x, list) and len(x) <= 20 and (x or not need) and all(one(y) for y in x)

    ok = (isinstance(sp["id"], str) and conveyor.LANE_RE.fullmatch(sp["id"]) and stem in (None, sp["id"])
          and sp["class"] in CLASSES and sp["tier"] in ("T0", "T1", "T2") and type(sp["order"]) is int
          and one(sp["objective"]) and sp["worker"] in ("opus", "codex") and sp["effort"] in conveyor.EFFORTS
          and lines(sp["acceptance"], True) and lines(sp["negative_tests"]) and lines(sp["contracts"])
          and lines(sp["owned_files"], True) and all(OWNED_RE.fullmatch(p) for p in sp["owned_files"])
          and isinstance(sp["depends_on"], list) and len(sp["depends_on"]) <= 20
          and all(isinstance(d, str) and conveyor.LANE_RE.fullmatch(d) for d in sp["depends_on"]))
    if not ok:
        raise Refuse(f"N13 spec {stem or '?'}: a field breaks the schema")
    return sp, hashlib.sha256(raw).hexdigest()


def owned_paths(sp):
    return [OWNED_RE.fullmatch(p).group(1) for p in sp["owned_files"]]


def spec_tier(sp):
    """The tier from owned_files: any protected file needs T2; a lower declared tier is refused (N12)."""
    hot = [p for p in owned_paths(sp) if conveyor.protected_path(p) or p in conveyor.PROTECTED_FILES]
    if hot and sp["tier"] != "T2":
        raise Refuse(f"N12 {sp['id']}: declared {sp['tier']} but owns protected {', '.join(hot)}")
    return sp["tier"]


def pick(specs, approvals, state, cls, on_main=(), blocked=(), taken=()):
    """Pure, model-free choice: (eligible ids head first, {id: why skipped}); order N6, order, approval time."""
    ok, why = [], {}
    for sid, (sp, sha) in specs.items():
        if cls in CLASSES and sp["class"] != cls:
            continue
        st, ap = (state.get(sid) or {}).get("state", "READY"), approvals.get(sid)
        if st != "READY" or sid in taken:
            why[sid] = f"N11 {st if st != 'READY' else 'already in the backlog'}"
        elif not isinstance(ap, dict):
            why[sid] = "N2 no approval"
        elif ap.get("sha256") != sha:
            why[sid] = "N1 approval sha256 does not match the spec bytes"
        elif (set(ap) != {"spec_id", "sha256", "approver", "at"} or ap["spec_id"] != sid
              or ap["approver"] not in APPROVERS or not isinstance(ap["at"], str) or not AT_RE.fullmatch(ap["at"])):
            why[sid] = "N2 approval record malformed"
        elif any(d not in on_main or d in blocked for d in sp["depends_on"]) or sid in blocked:
            why[sid] = "N6 depends_on not on main, or blocked"
        else:
            try:
                spec_tier(sp)
                ok.append(sid)
            except Refuse as e:
                why[sid] = str(e)
    ok.sort(key=lambda s: (CLASSES.index(specs[s][0]["class"]), specs[s][0]["order"], approvals[s]["at"], s))
    return ok, why


def load_depot(root):
    """({id: (spec, sha)}, {id: approval}, {id: why unreadable}); a bad record is skipped, never guessed."""
    d = Path(root) / DEPOT
    specs, approvals, bad = {}, {}, {}
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            specs[p.stem] = parse_spec(p.read_bytes(), p.stem)
        except (Refuse, OSError) as e:
            bad[p.stem] = str(e)
    ad = d / "approvals"
    for p in sorted(ad.glob("*.json")) if ad.is_dir() else []:
        try:
            raw = p.read_bytes()
            if len(raw) <= APPROVAL_MAX:
                approvals[p.stem] = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes)
        except (OSError, ValueError, RecursionError):
            bad[f"approvals/{p.stem}"] = "unreadable approval"
    return specs, approvals, bad


def _jload(p, default):
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError, RecursionError) as e:
        raise Refuse(f"{p.name} unreadable ({type(e).__name__}); fail closed") from None
    if not isinstance(v, type(default)):
        raise Refuse(f"{p.name} is not a {type(default).__name__}; fail closed")
    return v


def _jsave(p, v):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(v, indent=1, sort_keys=True), encoding="utf-8", newline="\n")
    os.replace(tmp, p)


def journal(run, rec):
    run.mkdir(parents=True, exist_ok=True)
    with open(run / "journal.jsonl", "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def inbox_set(run, code, on, at, write, out):
    """One schema-bound {code, at} item per outage: raised once while it lasts, cleared when it ends."""
    p = run / "inbox.json"
    items = _jload(p, {"items": []}).get("items")
    if not isinstance(items, list) or not all(isinstance(i, dict) and set(i) == {"code", "at"}
                                              and INBOX_RE.fullmatch(str(i["code"])) for i in items):
        raise Refuse("inbox.json breaks its schema; fail closed")
    if not INBOX_RE.fullmatch(code) or on == any(i["code"] == code for i in items):
        return
    new = items + [{"code": code, "at": at}] if on else [i for i in items if i["code"] != code]
    if len(new) > INBOX_MAX:
        raise Refuse(f"inbox holds {INBOX_MAX} items; not adding {code}")
    out(f"{'' if write else 'dry-run: would '}{'raise' if on else 'clear'} inbox {code}")
    if write:
        _jsave(p, {"items": new})


def as_gen_spec(sp):
    """A vetted depot spec -> the Gen.spec dict; every text field comes from the sha-approved spec bytes."""
    owned = owned_paths(sp)
    run = [f"tests.{Path(p).stem}" for p in owned if p.startswith("tests/test_") and p.endswith(".py")]
    out = {"id": sp["id"], "title": TITLE[sp["class"]] + sp["objective"], "tier": sp["tier"], "model": sp["worker"],
           "effort": sp["effort"], "size": "MEDIUM", "goal": sp["objective"],
           "tests": sp["acceptance"] + sp["negative_tests"], "run": run or ["tests"], "owned": owned,
           "commit": f"feat: {sp['id']} {sp['objective']}", "depends_on": sp["depends_on"]}
    if sp["contracts"]:
        out["decisions"] = "; ".join(sp["contracts"])
    return out


def origin_main(g):
    """N16: the slice base is origin/main as a full 40-hex sha, never a branch name."""
    rc, o = g.git_fn(g.root, "rev-parse", "--verify", "refs/remotes/origin/main")
    if rc != 0 or not SHA40.fullmatch(o.strip()):
        raise Refuse("N16 origin/main is not a 40-hex sha; fail closed")
    return o.strip()


def depot_fill(g, cls, propose, write, t):
    """QUEUEFILL2: approved READY depot specs -> slices for a low class. Dry-run unless write; one fill at a time."""
    run = g.root / DEPOT / "_run"
    lock = run / "fill.lock"
    if write:
        run.mkdir(parents=True, exist_ok=True)
        try:  # N10: O_EXCL makes the second concurrent fill refuse; a stale lock needs a human (fail closed)
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            raise Refuse(f"N10 another depot fill holds {lock.relative_to(g.root).as_posix()}") from None
    try:
        return _depot_fill(g, run, cls, propose, write, t)
    finally:
        if write:
            lock.unlink()


def _depot_fill(g, run, cls, propose, write, t):
    at = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day = at[:10]
    state = _jload(run / "state.json", {})
    caps = _jload(run / "caps.json", {})
    if not all(isinstance(v, dict) and v.get("state") in ("READY", "CONSUMED", "REJECTED") for v in state.values()) \
            or not all(type(v) is int and v >= 0 for v in caps.values()):
        raise Refuse("depot state.json/caps.json breaks its schema; fail closed")
    b = g.backlog()
    ids = {s.get("id") for s in b["slices"]}

    def save():
        if write:
            _jsave(run / "state.json", state)

    # N9: an intent without a receipt is UNKNOWN_OUTCOME: adopt the slice if it landed, else reject; never retry
    recs = [json.loads(x) for x in (run / "journal.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] \
        if (run / "journal.jsonl").is_file() else []
    done = {r.get("id") for r in recs if r.get("kind") == "receipt"}
    for sid in dict.fromkeys(r.get("id") for r in recs if r.get("kind") == "intent" and r.get("id") not in done):
        if not isinstance(sid, str) or not conveyor.LANE_RE.fullmatch(sid):
            raise Refuse("journal.jsonl holds a malformed intent; fail closed")
        landed = sid in ids
        st = state.setdefault(sid, {"state": "READY", "attempts": 0})
        st.update(state="CONSUMED", slice=sid) if landed else st.update(state="REJECTED", reason="UNKNOWN_OUTCOME")
        g.out(f"depot: N9 intent {sid} had no receipt: {'adopted the landed slice' if landed else 'UNKNOWN_OUTCOME'}")
        if write:
            journal(run, {"kind": "receipt", "id": sid, "result": "reconciled", "at": at})
        if not landed:
            inbox_set(run, f"unknown_outcome:{sid}", True, at, write, g.out)
        save()
    now = datetime.fromtimestamp(t).replace(microsecond=0)
    d = orch_digest.build(g.root, now, now - timedelta(hours=24))
    if any(i["reason"] == "unknown" and i["id"] in STATE_INPUTS for i in d["items"]):
        raise Refuse("conveyor state unreadable; cannot count ready slices")
    view = orch_digest.load(g.root / "build" / "conveyor" / "status.json", [], {}).get("backlog") or []
    seen = {str(v.get("id")) for v in view if isinstance(v, dict)}
    blocked = {i["id"] for i in d["items"] if i["reason"] in ("blocked", "held")}
    specs, approvals, bad = load_depot(g.root)
    for k, why in sorted(bad.items()):
        g.out(f"depot: skipped {k}: {why}")
    deps = {x for sp, _ in specs.values() for x in sp["depends_on"]}
    on_main = {x for x in deps if (lambda r: r[0] == 0 and r[1].strip())(g.git_fn(
        g.root, "log", "-1", "--format=%H", "-E", f"--grep=(^|[ :(]){re.escape(x)}([ ):]|$)", "refs/remotes/origin/main"))}
    if propose:  # N4: a proposed id must be this class's head, never just any spec
        if propose not in specs or (cls in CLASSES and specs[propose][0]["class"] != cls):
            raise Refuse(f"N4 proposed {propose} is not a {cls} depot spec")
        cls = specs[propose][0]["class"]
    total_ready = sum(d["ready"].values())
    made = 0
    for c in [cls] if cls in CLASSES else CLASSES:
        pending = [s for s in b["slices"] if str(s.get("title", "")).startswith(TITLE[c].strip())
                   and s.get("id") not in seen and not (g.reports / f"{s.get('id')}.md").is_file()]
        ready = d["ready"][c] + len(pending)  # P4: a slice written but not yet in status.json counts as ready
        if ready >= LOW:  # N3: a stale trigger is a no-op; the outage (if any) is over
            g.out(f"depot: {c} has {ready} ready >= {LOW}; nothing to fill")
            inbox_set(run, f"spec_needed:{c}", False, at, write, g.out)
            continue
        ok, why = pick(specs, approvals, state, c, on_main, blocked, ids)
        for sid, w in sorted(why.items()):
            g.out(f"depot: skipped {sid}: {w}")
            if w.startswith("N12") and write:  # a T1/T0 spec owning protected files is rejected for good
                state.setdefault(sid, {"state": "READY", "attempts": 0}).update(state="REJECTED", reason="N12")
                save()
        if propose and (not ok or ok[0] != propose):
            raise Refuse(f"N4 proposed {propose} is not the {c} head ({ok[0] if ok else 'none eligible'})")
        if not ok:
            empty = not specs and total_ready == 0
            code = "queue_empty" if empty else f"spec_needed:{c}"  # N15 / N6: once per outage
            inbox_set(run, code, True, at, write, g.out)
            continue
        inbox_set(run, f"spec_needed:{c}", False, at, write, g.out)
        inbox_set(run, "queue_empty", False, at, write, g.out)
        want = min(TARGET - ready, PER_ROUND, DEPOT_CAP[c] - caps.get(f"{c}:{day}", 0))
        if want <= 0:
            g.out(f"depot: N8 daily cap {DEPOT_CAP[c]} for {c} reached")
            continue
        base = origin_main(g)
        for sid in ok[:want]:
            sp, sha = specs[sid]
            raw = g.backlog_path.read_bytes()
            bb, entry, body = g.from_spec(as_gen_spec(sp), False)
            head, rest = body.split("\n", 1)
            body = head.replace(" base=main", f" base={base}") + "\n" + rest
            g.lint(bb, entry, body)
            if not write:
                g.emit(bb, entry, body, False)
                continue
            if origin_main(g) != base or g.backlog_path.read_bytes() != raw:  # N16 / N10 CAS right before emit
                raise Refuse("N10/N16 origin/main or backlog.json moved during the fill; nothing written")
            journal(run, {"kind": "intent", "id": sid, "sha256": sha, "base": base, "at": at})
            rc = g.emit(bb, entry, body, True, strict=True)
            st = state.setdefault(sid, {"state": "READY", "attempts": 0})
            if rc:  # N7: emit restored backlog + prompt; drop its backup so a retry can run
                (g.root / "build" / "conveyor" / "hold" / f"backlog.json.bak-{sid}").unlink(missing_ok=True)
                st["attempts"] = st.get("attempts", 0) + 1
                if st["attempts"] >= 2:
                    st.update(state="REJECTED", reason="N7 check failed twice")
            else:
                st.update(state="CONSUMED", slice=sid)
                caps[f"{c}:{day}"] = caps.get(f"{c}:{day}", 0) + 1
                _jsave(run / "caps.json", {k: v for k, v in caps.items() if k.endswith(day)})
                made += 1
            journal(run, {"kind": "receipt", "id": sid, "result": "failed" if rc else "written", "at": at})
            save()
            if rc:
                return 1
            b = g.backlog()
            ids.add(sid)
    g.out(f"depot: {made} slice(s) written" if write else "depot: dry-run; nothing written (use --write)")
    return 0


def main(argv=None, git=real_git, out=print, now=time.time):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                    help="main checkout (holds build/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("review", "int", "slice", "fix", "fill", "depot-fill"):
        p = sub.add_parser(name)
        if name in ("slice", "fix"):
            p.add_argument("--spec", required=True, metavar="JSON", help="the slice/fix spec file")
        elif name == "fill":
            p.add_argument("--track", choices=[n for n, _ in TRACKS] + ["auto"], default="auto")
        elif name == "depot-fill":
            p.add_argument("--class", dest="cls", choices=CLASSES + ("auto",), default="auto")
            p.add_argument("--propose", metavar="ID", help="the spec id the caller expects at the head")
        else:
            p.add_argument("ids", nargs="+")
        if name == "review":
            p.add_argument("--with-partial", action="append", default=[], metavar="ID",
                           help="a BLOCKED predecessor a reviewed slice names as BASE")
            p.add_argument("--focus", metavar="TEXT", help="what the reviewer must look at (one line)")
        m = p.add_mutually_exclusive_group()
        m.add_argument("--write", action="store_true", help="write the prompt and the backlog entry")
        m.add_argument("--dry-run", action="store_true", help="print only (the default)")
    a = ap.parse_args(argv)
    g = Gen(a.root, git=git, out=out)
    focus = getattr(a, "focus", None)
    if a.cmd == "review" and not (focus or "").strip():
        if a.write:
            out('REFUSED: focus required (--focus "<text>")')
            return 2
        out(f"warning: no --focus; the prompt carries the placeholder {FOCUS_PLACEHOLDER}")
        focus = None
    try:
        if a.cmd == "depot-fill":
            return depot_fill(g, a.cls, a.propose, a.write, now())
        if a.cmd == "fill":
            t = now()
            rec = {"track": None, "spec": None, "result": "refused", "reason": "error", "skipped": []}
            try:
                return fill(g, a.track, a.write, t, rec)
            finally:
                fill_log(g.root, t, rec)
        if a.cmd in ("slice", "fix"):
            cap = DAILY_CAP[a.cmd]
            if a.write and daily_count(a.root, a.cmd, now()) >= cap:  # ALLOWLIST T5 items 2 and 5
                raise Refuse(f"ALLOW-T5 daily {a.cmd} cap {cap} reached; escalate to the orchestrator")
            rc = g.emit(*g.from_spec(a.spec, a.cmd == "fix"), a.write, strict=True)
            if a.write and rc == 0:
                daily_count(a.root, a.cmd, now(), add=1)
            return rc
        made = g.review(a.ids, a.with_partial, focus) if a.cmd == "review" else g.integration(a.ids)
        return g.emit(*made, a.write)
    except (Refuse, OSError) as e:
        out(f"REFUSED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
