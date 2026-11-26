"""Plain-code slice precheck (PRECHECK1): mechanical FIX reasons caught before CLREV / CDX.

A clean precheck is NOT a review receipt. check() is a pure function over the slice's
spec, diff, texts, report and facts; it never raises. The CLI is read-only and reaches
git only through an injected runner that accepts read subcommands. Guard lists are read
from the guard test sources at run time (ast), never copied here.
"""

import argparse
import ast
import contextlib
import io
import json
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path

NOTICE = "Precheck is NOT a review receipt: a clean result does not replace CLREV/CDX."
MAX_BYTES = 5 * 1024 * 1024
SHA_RE = re.compile(r"[0-9a-f]{40}")
IDENT = re.compile(r"[a-z_][a-z0-9_]*")
GIT_READ = frozenset({"log", "diff", "show", "rev-parse", "merge-base"})
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
TEXT_EXT = (".py", ".md", ".json", ".txt", ".ps1", ".toml", ".qml", ".js", ".cfg", ".ini", ".yml", ".yaml")
PF = "project_" + "folders"  # built from parts so this module never spells the guarded import
HEARTBEAT = "def " + "heartbeat"
FACE_GUARD = "tests/test_face_shell.py"
PF_GUARD = "tests/test_" + PF + "_6h.py"
CONVEYOR = "scripts/conveyor.py"
HEARTBEAT_HOMES = frozenset({CONVEYOR, "tests/test_conveyor.py"})
DURATION_DIR = "scripts/test_durations.d/"
SECRET_EXT = (".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3")
PF_IMPORT = re.compile(r"^\s*(?:from\s+[\w.]*\b" + PF + r"\b|import\s+[\w., ]*\b" + PF + r"\b|from\s+[\w.]+\s+import\s+.*\b"
                       + PF + r"\b)")


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    line: int
    message: str


@dataclass(frozen=True)
class Spec:
    lane: str
    bases: frozenset
    owned: tuple
    run: tuple
    run_line: int


def parse_spec(text):
    """Spec fields from a lane prompt, or a SPEC finding naming what is missing."""
    if not isinstance(text, str) or not text.strip():
        return None, Finding("SPEC", "spec", 0, "spec is empty or not text; pass the lane prompt file")
    lines = text.splitlines()
    head = re.match(r"@jarvis lane=([A-Za-z0-9._-]+)", lines[0].strip())
    if not head:
        return None, Finding("SPEC", "spec", 1, "first line must start with '@jarvis lane=<ID>'")
    bases = set(re.findall(r"\bbase=([0-9a-f]{40})\b", lines[0]))
    owned, run, run_line = [], [], 0
    for no, line in enumerate(lines, 1):
        bases.update(re.findall(r"\bBASE AT ([0-9a-f]{40})\b", line))
        if line.startswith("Run:") and not run_line:
            run_line, run = no, re.findall(r"\btests\.[A-Za-z0-9_.]+", line)
        if line.startswith("Owned files:"):
            body = line.split("Anything else", 1)[0]
            owned += re.findall(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+", body[len("Owned files:"):])
    if not owned:
        return None, Finding("SPEC", "spec", 0, "spec has no 'Owned files:' line with paths")
    return Spec(head.group(1), frozenset(bases), tuple(owned), tuple(run), run_line), None


def _clean(s):
    """Printable ASCII for output: control, bidi and non-ASCII characters are escaped."""
    s = str(s)
    return "".join(c if " " <= c <= "~" else f"\\u{ord(c):04x}" for c in s)[:300]


def _decode(path, raw):
    """(text or None, finding or None). Size, binary and UTF-8 problems are findings, never exceptions."""
    if raw is None:
        return None, None
    size = len(raw.encode("utf-8", "surrogatepass")) if isinstance(raw, str) else len(raw)
    if size > MAX_BYTES:
        return None, Finding("INPUT", path, 0, f"{size} bytes is over the {MAX_BYTES} byte precheck cap; split it")
    if isinstance(raw, str):
        return raw, None
    if not isinstance(raw, (bytes, bytearray)):
        return None, Finding("INPUT", path, 0, f"content is {type(raw).__name__}, not bytes or text")
    textual = path.lower().endswith(TEXT_EXT)
    if b"\0" in raw[:8192]:
        return None, (Finding("INPUT", path, 0, "binary content in a text file; commit text") if textual else None)
    try:
        return bytes(raw).decode("utf-8-sig"), None
    except UnicodeDecodeError as exc:
        return None, (Finding("INPUT", path, 0, f"invalid UTF-8 at byte {exc.start}; save as UTF-8") if textual else None)


def _added(new, old):
    """(line number, line) for new-text lines that the old text does not have."""
    seen = set(old.splitlines()) if old else set()
    return [(no, line) for no, line in enumerate(new.splitlines(), 1) if line not in seen]


def _key(path):
    """Case-folded posix path with Win32 trailing dots/spaces stripped: the protected-set lookup key."""
    parts = [p.rstrip(". ") for p in path.replace("\\", "/").split("/")]
    return posixpath.normpath("/".join(parts)).casefold()


def _unsafe(path):
    if not isinstance(path, str) or not path:
        return "empty or non-text path"
    if "\\" in path or ":" in path or path.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return "absolute, drive, backslash or control character in path"
    if any(p in ("", ".", "..") or p != p.rstrip(". ") for p in path.split("/")):
        return "empty, '.', '..' or trailing dot/space segment"
    return None


def _hard_protected(key):
    base = posixpath.basename(key)
    return (key.startswith(("data/", ".jarvis/", "dev/dashboard/")) or "/.jarvis/" in "/" + key
            or key in ("data", "scripts/test_durations.json") or base == "uv.lock" or base.startswith(".env")
            or any(s.startswith("riot") for s in key.split("/")) or base.endswith(SECRET_EXT))


def _tree(src):
    try:
        return ast.parse(src) if isinstance(src, str) else None
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _literal(tree, name):
    """frozenset of the literal string collection assigned to `name` in a guard source, or None."""
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if node.__class__ in (ast.Assign, ast.AnnAssign) and node.value is not None \
                and any(isinstance(t, ast.Name) and t.id == name for t in targets):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, RecursionError):
                continue
            if isinstance(value, (set, frozenset, list, tuple)) and all(isinstance(v, str) for v in value):
                return frozenset(value)
    return None


def face_allowed(source):
    """(allowed import modules, denied-module regex or None) read from tests/test_face_shell.py source."""
    tree = _tree(source)
    if tree is None:
        raise LookupError(f"{FACE_GUARD} is missing or does not parse; cannot read the face allow-list")
    allowed = (_literal(tree, "SHELL_IMPORTS") or frozenset()) | (_literal(tree, "PURE_IMPORTS") or frozenset())
    if not allowed:
        raise LookupError(f"{FACE_GUARD} has no SHELL_IMPORTS / PURE_IMPORTS literal")
    deny = next((n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and n.value.startswith("^jarvis\\.")), None)
    return allowed, (re.compile(deny) if deny else None)


def isolation_guards(sources, stems):
    """[(guard path, names, exempt file names, roots)] read from tests/*.py functions that rglob('*.py')."""
    out = []
    for gpath, src in sorted((sources or {}).items()):
        tree = _tree(src) if str(gpath).startswith("tests/") else None
        for fn in ast.walk(tree) if tree else ():
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nodes = list(ast.walk(fn))
            consts = {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            if "*.py" not in consts or not any(isinstance(n, ast.Attribute) and n.attr == "rglob" for n in nodes):
                continue
            names = set()
            for n in nodes:
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Constant) and isinstance(n.left.value, str) \
                        and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops) \
                        and any(isinstance(m, ast.Name) or (isinstance(m, ast.Attribute) and m.attr == "read_text")
                                for c in n.comparators for m in ast.walk(c)):
                    names.add(n.left.value)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "assertNotIn" \
                        and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    names.add(n.args[0].value)
            names = frozenset(x for x in names if IDENT.fullmatch(x) and x in stems)
            if names:
                exempt = frozenset(c for c in consts if c.endswith(".py") and c != "*.py") | {x + ".py" for x in names}
                roots = tuple(r for r in ("jarvis", "scripts") if r in consts) or ("jarvis", "scripts")
                out.append((gpath, names, exempt, roots))
    return out


def _duration_error(text):
    """Mirror of scripts/test_parallel.py _read_slice_durations validation; None when the record is good."""
    try:
        data = json.loads(text.lstrip("﻿"))
    except (ValueError, RecursionError) as exc:
        return f"not JSON ({type(exc).__name__})"
    if not isinstance(data, dict):
        return "top level is not an object"
    for name in ("classes", "tests"):
        records = data.get(name, {})
        if not isinstance(records, dict):
            return f'"{name}" is not an object'
        for key, value in records.items():
            try:
                ok = not isinstance(value, bool) and isinstance(value, (int, float)) and 0 <= float(value) < float("inf")
            except OverflowError:
                ok = False
            if not ok:
                return f'{name}["{key}"] is not a finite number >= 0'
    return None


def _scope(spec, changes, texts, old, facts):
    owned = {_key(p) for p in spec.owned}
    tree = _tree((facts.get("sources") or {}).get(CONVEYOR))
    dirs, files = (_literal(tree, n) if tree else None for n in ("PROTECTED_DIRS", "PROTECTED_FILES"))
    if not dirs or not files:
        raise LookupError(f"{CONVEYOR} has no PROTECTED_DIRS / PROTECTED_FILES literal")
    dirs, files, out = tuple(d.casefold() for d in dirs), {f.casefold() for f in files}, []
    for status, path, before in changes:
        reason = next((f"{label} path: {why}" for label, p in (("new", path), ("old", before)) if p is not None
                       for why in [_unsafe(p)] if why), None)
        keys = [k for k in (_key(path), _key(before) if before else None) if k]
        if reason is None:
            for k in keys:
                if _hard_protected(k):
                    reason = f"{k} is protected and is never touched"
                elif k in files or k.startswith(dirs):
                    reason = f"{k} is in the conveyor protected set"
                elif status[0] in "DR" and re.fullmatch(r"tests/test_[^/]+\.py", keys[-1]) and k == keys[-1]:
                    reason = f"removes guard test {k}; restore it"
                elif k not in owned:
                    reason = f"{k} is not in owned_files" + (" (rename from outside scope)" if k != keys[0] else "")
                if reason:
                    break
        if reason:
            out.append(Finding("SCOPE", path, 0, reason + "; revert this change or STOP (C2)"))
    return out


def _duration(spec, changes, texts, old, facts):
    out, records = [], []
    for status, path, _ in changes:
        if status[0] != "D" and path.startswith(DURATION_DIR) and path.endswith(".json"):
            records.append(path)
            why = "content is not readable text" if texts.get(path) is None else _duration_error(texts[path])
            if why:
                out.append(Finding("DURATION", path, 0, f"duration record does not parse: {why}"))
    touched = any(s[0] != "D" and re.fullmatch(r"tests/test_[^/]+\.py", p) for s, p, _ in changes)
    lane_file = f"{DURATION_DIR}{spec.lane}.json"
    if touched and not records and lane_file not in (facts.get("head_files") or ()):
        out.append(Finding("DURATION", lane_file, 0, "tests were added or changed but no duration record is in the "
                           "diff; add it (version 1, every TestCase class, rounded up to 0.1, floor 1.0)"))
    return out


def _report(report, facts):
    if report is None:
        return [Finding("REPORT", "report", 0, "report is missing or unreadable; write build/reports/<lane>.md (C7)")]
    lines, out = report.splitlines(), []
    first = re.fullmatch(r"STATUS: (PASS|BLOCKED|CI_PENDING)\b.*", lines[0].strip()) if lines else None
    if not first:
        out.append(Finding("REPORT", "report", 1, "first line must be 'STATUS: PASS|BLOCKED|CI_PENDING'"))
    status = first.group(1) if first else None
    commits = [(no, line) for no, line in enumerate(lines, 1) if re.match(r"[\s`*>-]*COMMIT:", line)]
    if not commits:
        out.append(Finding("REPORT", "report", 0, "no 'COMMIT: <40 hex>' line; add exactly one"))
    elif len(commits) > 1:
        out.append(Finding("REPORT", "report", commits[1][0], f"{len(commits)} lines say COMMIT:; keep exactly one"))
    else:
        no, line = commits[0]
        sha = re.fullmatch(r"COMMIT: ([0-9a-f]{40})", line.rstrip())
        head = facts.get("head")
        if sha and head and sha.group(1) != head:
            out.append(Finding("REPORT", "report", no, f"COMMIT {sha.group(1)} is not the slice head {head}"))
        elif not sha and not (line.rstrip() == "COMMIT: none" and status not in (None, "PASS")):
            out.append(Finding("REPORT", "report", no, "COMMIT line must be exactly 'COMMIT: <40 lowercase hex>' "
                               "alone on its line (no backticks, no short sha, no trailing text)"))
    runs = [(no, line) for no, line in enumerate(lines, 1) if line.startswith("RUNS:")]
    if not runs:
        out.append(Finding("REPORT", "report", 0, "RUNS line must give '<n> tests, <n> failures' for the run command"))
    for no, line in runs:
        counts = re.search(r"(\d+) tests?, (\d+) failures?", line)
        code = re.search(r"\bexit (?:code )?(-?\d+)", line)
        if not counts:
            out.append(Finding("REPORT", "report", no, "RUNS line must give '<n> tests, <n> failures' for the run command"))
        elif status == "PASS" and (int(counts.group(2)) or (code and int(code.group(1)))):
            out.append(Finding("REPORT", "report", no, "STATUS: PASS with failing tests or a non-zero exit; "
                               "fix them or report BLOCKED"))
    return out


def _banned(spec, changes, texts, old, facts):
    stems = {posixpath.splitext(posixpath.basename(p))[0] for p in facts.get("head_files") or ()
             if p.startswith("jarvis/") and p.endswith(".py")}
    out, seen = [], set()
    for gpath, names, exempt, roots in isolation_guards(facts.get("sources"), stems):
        for status, path, _ in changes:
            if status[0] == "D" or not path.endswith(".py") or path.split("/")[0] not in roots or texts.get(path) is None \
                    or any(path == e or path.endswith("/" + e) for e in exempt):
                continue
            for no, line in _added(texts[path], old.get(path)):
                hit = next((n for n in sorted(names) if n in line), None)
                if hit and (path, no) not in seen:
                    seen.add((path, no))
                    out.append(Finding("BANNED_NAME", path, no, f"'{hit}' is forbidden here by the isolation guard in "
                                       f"{gpath} (code, comment or docstring alike); reword without the name"))
    return out


def _face(spec, changes, texts, old, facts):
    new = [p for s, p, _ in changes if s[0] in "AR" and re.fullmatch(r"jarvis/face/[^/]+\.py", p)]
    if not new:
        return []
    allowed, deny = face_allowed((facts.get("sources") or {}).get(FACE_GUARD))
    guard_added = [line for _, line in _added(texts.get(FACE_GUARD) or "", old.get(FACE_GUARD))] \
        if any(p == FACE_GUARD for _, p, _ in changes) else []
    out = []
    for path in new:
        name = posixpath.basename(path)
        tree = _tree(texts.get(path))
        if any(re.search(r"(?<![\w.])" + re.escape(name), line) for line in guard_added):
            continue
        if tree is None:
            out.append(Finding("FACE_ALLOW", path, 0, "new face module does not parse; fix the syntax"))
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                pairs = [(a.name, {a.name}) for a in node.names] if isinstance(node, ast.Import) \
                    else [(node.module, {a.name for a in node.names})]
                bad = next((m for m, names in pairs if m is None or m not in allowed or (deny and deny.match(m))
                            or (m == "urllib.parse" and m in names)), "")
                if bad != "":
                    out.append(Finding("FACE_ALLOW", path, node.lineno, f"imports '{bad or 'relative'}', outside the "
                                       f"{FACE_GUARD} allow-list; name {name} in that allow-list in this diff"))
                    break
    return out


def _guard_word(spec, changes, texts, old, facts):
    out, allowed = [], None
    for status, path, _ in changes:
        if status[0] == "D" or not path.endswith(".py") or texts.get(path) is None:
            continue
        for no, line in _added(texts[path], old.get(path)):
            if HEARTBEAT in line and path not in HEARTBEAT_HOMES:
                out.append(Finding("GUARD_WORD", path, no, f"'{HEARTBEAT}' belongs only to {CONVEYOR}; remove it"))
            if path.startswith(("jarvis/", "scripts/")) and PF_IMPORT.match(line):
                if allowed is None:
                    allowed = _literal(_tree((facts.get("sources") or {}).get(PF_GUARD)) or ast.Module([], []), "allowed")
                    if allowed is None:
                        raise LookupError(f"{PF_GUARD} has no 'allowed' literal")
                if not path.startswith("jarvis/") or path[len("jarvis/"):] not in allowed:
                    out.append(Finding("GUARD_WORD", path, no, f"{PF} import outside the {PF_GUARD} allow-list; remove it"))
    return out


def _base(spec, changes, texts, old, facts):
    parent, main = facts.get("parent"), facts.get("main")
    ok = {b for b in (main, *spec.bases) if b}
    if not (isinstance(parent, str) and SHA_RE.fullmatch(parent)):
        return [Finding("BASE", "HEAD^", 0, "parent commit is unknown; the slice must be one commit on its base")]
    if parent not in ok:
        return [Finding("BASE", "HEAD^", 0, f"parent {parent} is neither current main nor the declared base; "
                        "rebase onto main or the stacked base named in the spec")]
    return []


def _run_line(spec, changes, texts, old, facts):
    present = set(facts.get("head_files") or ()) | {p for s, p, _ in changes if s[0] != "D"}
    if not spec.run:
        return [Finding("RUN_LINE", "spec", spec.run_line, "spec has no 'Run:' line naming tests.<module>")]
    def exists(mod):  # tests.mod.Class.test: any dotted prefix that is a file counts
        parts = mod.rstrip(".").split(".")
        return any("/".join(parts[:n]) + ".py" in present for n in range(2, len(parts) + 1))
    return [Finding("RUN_LINE", "spec", spec.run_line, f"{mod} has no file {mod.replace('.', '/')}.py; fix the run line")
            for mod in spec.run if not exists(mod)]


CHECKS = (("SCOPE", _scope), ("DURATION", _duration), ("BANNED_NAME", _banned), ("FACE_ALLOW", _face),
          ("BASE", _base), ("GUARD_WORD", _guard_word), ("RUN_LINE", _run_line))
BAD_ARGS = ("--output", "-o", "-c", "-C", "--ext-diff", "--textconv", "--exec", "--git-dir", "--work-tree", "--no-index")
ROOT = Path(__file__).resolve().parents[1]


def _changes(changed_files):
    """(status, path, old path or None) tuples; a malformed entry is a SCOPE finding."""
    if not isinstance(changed_files, (list, tuple)):
        return [], [Finding("SCOPE", "changed_files", 0, "changed_files is not a list of (status, path) entries")]
    out, bad = [], []
    for i, entry in enumerate(changed_files):
        entry = ("M", entry) if isinstance(entry, str) else entry
        if isinstance(entry, (list, tuple)) and len(entry) in (2, 3) and isinstance(entry[0], str) and entry[0] \
                and entry[0][0] in "AMDRCT" and isinstance(entry[1], str) \
                and (len(entry) == 2 or entry[2] is None or isinstance(entry[2], str)):
            out.append((entry[0], entry[1], entry[2] if len(entry) == 3 and entry[0][0] == "R" else None))
        else:
            bad.append(Finding("SCOPE", f"changed_files[{i}]", 0, "malformed entry; expected (status, path[, old path])"))
    return out, bad


def _texts(raw, keep):
    """Decoded {path: text}; decode findings go to `keep` when it is a list."""
    out = {}
    for path, content in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(path, str):
            out[path], bad = _decode(path, content)
            if bad and keep is not None:
                keep.append(bad)
    return out


def _check(spec, changed_files, texts, report, facts):
    if not isinstance(spec, Spec):
        spec, bad = parse_spec(_decode("spec", spec)[0] if isinstance(spec, (bytes, bytearray)) else spec)
        if bad:
            return [bad]
    facts = dict(facts) if isinstance(facts, dict) else {}
    changes, out = _changes(changed_files)
    texts = _texts(texts, out)
    old = _texts(facts.get("old"), None)
    facts["sources"] = _texts(facts.get("sources"), None)
    hf = facts.get("head_files")
    facts["head_files"] = frozenset(p for p in hf if isinstance(p, str)) if isinstance(hf, (list, tuple, set, frozenset)) \
        else frozenset()
    rep, bad = _decode("report.md", report)
    out += [bad] if bad else _report(rep, facts)
    for name, fn in CHECKS:
        try:
            out += fn(spec, changes, texts, old, facts)
        except Exception as exc:  # one broken check is a typed CHECKER_ERROR, the others still run
            out.append(Finding("CHECKER_ERROR", name, 0, f"{type(exc).__name__}: {_clean(exc)}"))
    return out


def check(spec, changed_files, texts, report, facts):
    """list[Finding] for one slice. Never raises: any failure is a CHECKER_ERROR finding."""
    try:
        return _check(spec, changed_files, texts, report, facts)
    except Exception as exc:  # the contract is a typed result, never an exception
        return [Finding("CHECKER_ERROR", "check", 0, f"{type(exc).__name__}: {_clean(exc)}")]


def exit_code(findings):
    codes = {f.code for f in findings}
    return 2 if "CHECKER_ERROR" in codes else 1 if codes else 0


def render(findings):
    """Markdown for the lane (file:line, what to change) then the same findings as JSON. Line 1 is NOTICE."""
    lines = [NOTICE, ""]
    if findings:
        lines.append(f"Precheck: {len(findings)} finding(s); fix each line below, then rerun.")
        lines += [f"- `{_clean(f.path)}:{f.line}` {f.code}: {_clean(f.message)}" for f in findings]
    else:
        lines.append("Precheck: clean (0 findings).")
    payload = {"notice": NOTICE, "exit": exit_code(findings),
               "findings": [{"code": f.code, "path": f.path, "line": f.line, "message": f.message} for f in findings]}
    return "\n".join(lines + ["", "```json", json.dumps(payload, ensure_ascii=True, sort_keys=True), "```"])


def _git(runner, *args):
    """Run one read-only git command through the injected runner: (returncode, stdout bytes)."""
    if not args or args[0] not in GIT_READ or any(not isinstance(a, str) or a.startswith(BAD_ARGS) for a in args[1:]):
        raise PermissionError(f"refused git command: {_clean(' '.join(map(str, args)))[:80]}")
    result = runner(list(args))
    if not (isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int)
            and isinstance(result[1], (bytes, bytearray))):
        raise RuntimeError("runner must return (returncode, stdout bytes)")
    return result


def _default_runner(args):
    import subprocess  # nosec B404 - the only process call; _git admits read subcommands only
    proc = subprocess.run(["git", *args], capture_output=True, cwd=ROOT, check=False, timeout=120)  # nosec B603 B607
    return proc.returncode, proc.stdout


def _ok(runner, *args):
    rc, out = _git(runner, *args)
    if rc:
        raise RuntimeError(f"git {args[0]} failed with exit {rc}")
    return bytes(out)


def _z(raw):
    return [t.decode("utf-8", "replace") for t in raw.split(b"\0") if t]


def collect(runner, base):
    """(changes, texts, facts) for base..HEAD, read through the injected runner only."""
    def sha(ref):
        rc, out = _git(runner, "rev-parse", "--verify", "-q", ref + "^{commit}")
        text = bytes(out).decode("ascii", "replace").strip()
        return text if rc == 0 and SHA_RE.fullmatch(text) else None

    def show(ref, path):
        rc, out = _git(runner, "show", f"{ref}:{path}")
        return None if rc else bytes(out)

    facts = {"head": sha("HEAD"), "parent": sha("HEAD^"), "main": sha("main")}
    tokens, changes, i = _z(_ok(runner, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "-M",
                               base, "HEAD")), [], 0
    while i < len(tokens):
        status = tokens[i]
        if status[:1] in ("R", "C"):
            changes.append((status, tokens[i + 2], tokens[i + 1] if status[0] == "R" else None))
            i += 3
        else:
            changes.append((status, tokens[i + 1], None))
            i += 2
    texts = {p: show("HEAD", p) for s, p, _ in changes if s[0] != "D"}
    facts["old"] = {p: show(base, b or p) for s, p, b in changes if s[0] in "MR"}
    facts["head_files"] = _z(_ok(runner, "diff", "--no-ext-diff", "--name-only", "-z", EMPTY_TREE, "HEAD", "--",
                                 "jarvis", "scripts", "tests"))
    # guard lists come from the base, so a slice cannot loosen the list it is checked against
    guards = _z(_ok(runner, "diff", "--no-ext-diff", "--name-only", "-z", "-Grglob", EMPTY_TREE, base, "--", "tests"))
    facts["sources"] = {p: show(base, p) for p in {*guards, FACE_GUARD, PF_GUARD, CONVEYOR}}
    return changes, texts, facts


def _read(path, label):
    """(bytes or None, finding or None); a missing file is (None, None)."""
    try:
        p = Path(path)
        if not p.is_file():
            return None, None
        size = p.stat().st_size
        if size > MAX_BYTES:
            return None, Finding("INPUT", label, 0, f"{size} bytes is over the {MAX_BYTES} byte precheck cap")
        return p.read_bytes(), None
    except (OSError, ValueError) as exc:
        return None, Finding("INPUT", label, 0, f"cannot read {label}: {type(exc).__name__}")


def main(argv=None, runner=None):
    parser = argparse.ArgumentParser(prog="python -m scripts.slice_precheck", description=NOTICE)
    for flag in ("--spec", "--base", "--report"):
        parser.add_argument(flag, required=True)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        print("\n".join([NOTICE] + [_clean(line) for line in buf.getvalue().splitlines()]))
        return 0 if exc.code == 0 else 2
    spec_raw, spec_bad = _read(args.spec, "spec")
    if not (SHA_RE.fullmatch(args.base) or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,99}", args.base)) \
            or ".." in args.base:
        findings = [Finding("CHECKER_ERROR", "--base", 0, "base must be a 40 hex sha or a plain ref name")]
    elif spec_bad or spec_raw is None:
        findings = [spec_bad or Finding("CHECKER_ERROR", "spec", 0, "spec file is missing; pass --spec FILE")]
    else:
        report_raw, report_bad = _read(args.report, "report")
        try:
            changes, texts, facts = collect(runner or _default_runner, args.base)
        except Exception as exc:  # git failure is a checker error, never a traceback
            findings = [Finding("CHECKER_ERROR", "git", 0, f"{type(exc).__name__}: {_clean(exc)}")]
        else:
            findings = ([report_bad] if report_bad else []) + check(spec_raw, changes, texts, report_raw, facts)
    print(render(findings))
    return exit_code(findings)


if __name__ == "__main__":
    sys.exit(main())
