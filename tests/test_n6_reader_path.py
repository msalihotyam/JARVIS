"""N6READERPATH1 (R26 NEG): the advisory report reader's output never reaches key 1.

The reader (scripts/conveyor.py read_report / reader_call / parse_reading) leaves its output in a
lane's "reading" key, in "reader" events (events.jsonl) and in status.json. A1 scans, by AST, the
key-1 path and the state writers for any use of that output; N1 proves the scan bites on a planted
use; A2 shows the key-1 verdict is identical with no, valid and garbage reader output on disk.
status.json's writer (Conveyor.write_status) is the reader's designed display sink, so it is not a
state writer here.
"""
import ast
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_key1_check import ci, k1, make, make_history

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Roots of the scan in scripts/conveyor.py; the closure over the conveyor.py functions and Conveyor
# methods they name is scanned too, plus every conveyor.X that key1_check.py uses.
CONVEYOR_ROOTS = ("write_key1", "key1_header", "key1_refusal", "key1_validated",
                  "Conveyor.save_lanes", "Conveyor.save_pager", "Conveyor.write_atomic",
                  "Conveyor.ci_runs", "Conveyor.push_gate", "Conveyor.autopilot",
                  "Conveyor.ci_rerun_claim")
API_ROOTS = ("main_checkout",)  # conveyor_api.py, imported by key1_check.py
WHOLE_MODULES = ("key1_check.py",)  # scanned in full

READER_NAMES = {"read_report", "reader_call", "parse_reading", "reader_key", "api_spent"}
READER_STRINGS = ("reading", "reader", "events.jsonl", "status.json")
# Conveyor.event (reached via ci_runs -> error) appends to events.jsonl; it is allowed to name
# that file only while it never reads (no read call anywhere in its body).
APPEND_ONLY = {"conveyor.Conveyor.event": "events.jsonl"}
READ_CALLS = {"read", "read_text", "read_bytes", "readline", "readlines", "load", "loads"}


def defs(tree):
    """{'f': node, 'Conveyor.m': node} for the module's top-level functions and class methods."""
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{n.name}.{m.name}"] = m
    return out


def names_in(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.Attribute):
            yield n.attr


def closure(table, roots):
    """Roots plus every def they name (bare f, self.m, Conveyor.m, conveyor.f), transitively."""
    by_short = {}
    for k in table:
        by_short.setdefault(k.split(".")[-1], []).append(k)
    seen, todo = set(), list(roots)
    while todo:
        k = todo.pop()
        if k in seen:
            continue
        seen.add(k)
        for nm in names_in(table[k]):
            todo.extend(by_short.get(nm, ()))
    return seen


def docstrings(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def hits(node, where):
    """Every use of reader output in `node`: a reader name, a READER_* constant, a reader string
    (key, event name or file), or an import naming the reader."""
    out, skip = [], docstrings(node)
    for n in ast.walk(node):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] + [getattr(n, "module", None) or ""]
            out += [f"{where}: import {m}" for m in mods
                    if m in READER_NAMES or m.startswith("READER_") or "reader" in m.lower()]
        elif isinstance(n, (ast.Name, ast.Attribute)):
            nm = n.id if isinstance(n, ast.Name) else n.attr
            if nm in READER_NAMES or nm.startswith("READER_"):
                out.append(f"{where}: name {nm}")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            out += [f"{where}: string {n.value!r}" for s in READER_STRINGS if s in n.value.lower()]
    allowed = APPEND_ONLY.get(where)
    if allowed and not READ_CALLS & set(names_in(node)):
        out = [h for h in out if h != f"{where}: string {allowed!r}"]
    return out


def parse(path):
    return ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))


def state_writers(scripts=SCRIPTS):
    """Conveyor methods that write_atomic a STATE_FILES file (self.<attr> set to self.base / name
    in __init__); fails closed when it cannot resolve one attr per STATE_FILES name."""
    tree = parse(scripts / "conveyor.py")
    names = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", "") == "STATE_FILES" for t in n.targets))
    conv = defs(tree)
    attrs = {}
    for n in ast.walk(conv["Conveyor.__init__"]):
        if (isinstance(n, ast.Assign) and isinstance(n.value, ast.BinOp)
                and isinstance(n.value.right, ast.Constant) and n.value.right.value in names):
            attrs.update({t.attr: n.value.right.value for t in n.targets if isinstance(t, ast.Attribute)})
    if set(attrs.values()) != set(names):
        raise AssertionError(f"state file attrs not resolved: {names} vs {attrs}")
    def is_state(e, local):  # self.<attr>, inline self.base / "<name>", or a local bound to either
        return ((isinstance(e, ast.Attribute) and e.attr in attrs)
                or (isinstance(e, ast.BinOp) and isinstance(e.right, ast.Constant) and e.right.value in names)
                or (isinstance(e, ast.Name) and e.id in local))
    out = set()
    for k, node in conv.items():
        local = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Assign) and is_state(n.value, set()):
                local |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and "write_atomic" in names_in(n.func) and k != "Conveyor.write_atomic"
                    and any(is_state(a, local) for a in n.args + [kw.value for kw in n.keywords])):
                out.add(f"conveyor.{k}")
    return out


def scan(scripts=SCRIPTS):
    """(scanned set, hits) over the key-1 path and the state writers under `scripts`."""
    conv = defs(parse(scripts / "conveyor.py"))
    roots = list(CONVEYOR_ROOTS)
    scanned, found = [], []
    for mod in WHOLE_MODULES:
        tree = parse(scripts / mod)
        scanned.append(mod)
        found += hits(tree, mod)
        roots += [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                  and isinstance(n.value, ast.Name) and n.value.id == "conveyor" and n.attr in conv]
    missing = [r for r in roots if r not in conv]
    if missing:
        raise AssertionError(f"scan roots not found in conveyor.py (renamed?): {missing}")
    for k in sorted(closure(conv, roots)):
        scanned.append(f"conveyor.{k}")
        found += hits(conv[k], f"conveyor.{k}")
    api = defs(parse(scripts / "conveyor_api.py"))
    for k in sorted(closure(api, API_ROOTS)):
        scanned.append(f"conveyor_api.{k}")
        found += hits(api[k], f"conveyor_api.{k}")
    return scanned, found


class StaticScanTests(unittest.TestCase):
    def test_a1_key1_and_state_write_path_never_use_reader_output(self):
        scanned, found = scan()
        for must in ("key1_check.py", "conveyor.write_key1", "conveyor.key1_refusal",
                     "conveyor.Conveyor.save_lanes", "conveyor.Conveyor.save_pager",
                     "conveyor.Conveyor.write_atomic", "conveyor_api.main_checkout",
                     "conveyor.Conveyor.push_gate", "conveyor.Conveyor.autopilot",
                     "conveyor.Conveyor.ci_rerun_claim"):
            self.assertIn(must, scanned)
        writers = state_writers()  # every lanes/autopilot/push.json writer, found by AST
        self.assertGreaterEqual(writers, {"conveyor.Conveyor.save_lanes", "conveyor.Conveyor.push_gate",
                                          "conveyor.Conveyor.autopilot"})
        self.assertEqual(writers - set(scanned), set(), "state writer outside the scan: add it to CONVEYOR_ROOTS")
        self.assertNotIn("conveyor.Conveyor.write_status", scanned)
        self.assertNotIn("conveyor.Conveyor.read_report", scanned)
        self.assertEqual(found, [], f"reader output on the key-1/state-write path; scanned={scanned}")

    def test_n1_planted_reader_use_fails_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for f in ("conveyor.py", "conveyor_api.py", "key1_check.py"):
                shutil.copy(SCRIPTS / f, d / f)
            self.assertEqual(scan(d)[1], [])  # the copy is clean
            with open(d / "key1_check.py", "a", encoding="utf-8") as f:
                f.write("\nfrom conveyor import read_report\n"
                        "_r = open('build/conveyor/status.json').read()\n")
            found = scan(d)[1]
            self.assertTrue(any("read_report" in h for h in found), found)
            self.assertTrue(any("status.json" in h for h in found), found)
            shutil.copy(SCRIPTS / "key1_check.py", d / "key1_check.py")
            src = (d / "conveyor.py").read_text(encoding="utf-8")
            planted = src.replace("    def save_lanes(self, lanes):\n",
                                  "    def save_lanes(self, lanes):\n"
                                  "        _ = [ln.get('reading') for ln in lanes.values()]\n", 1)
            self.assertNotEqual(planted, src)
            (d / "conveyor.py").write_text(planted, encoding="utf-8")
            found = scan(d)[1]
            self.assertTrue(any("Conveyor.save_lanes" in h and "reading" in h for h in found), found)
            # the append-only allowance ends once Conveyor.event reads its file
            planted = src.replace("    def event(self, kind, **kw):\n",
                                  "    def event(self, kind, **kw):\n"
                                  "        _ = (self.base / 'events.jsonl').read_text()\n", 1)
            self.assertNotEqual(planted, src)
            (d / "conveyor.py").write_text(planted, encoding="utf-8")
            found = scan(d)[1]
            self.assertTrue(any("Conveyor.event" in h and "events.jsonl" in h for h in found), found)
            # the push.json and autopilot.json writers are scanned too
            for meth in ("push_gate", "autopilot", "ci_rerun_claim"):
                with self.subTest(writer=meth):
                    head = next(ln for ln in src.splitlines(keepends=True)
                                if ln.startswith(f"    def {meth}(self"))
                    planted = src.replace(head, head + "        _ = self.load_lanes().get('x', {}).get('reading')\n", 1)
                    self.assertNotEqual(planted, src)
                    (d / "conveyor.py").write_text(planted, encoding="utf-8")
                    found = scan(d)[1]
                    self.assertTrue(any(f"Conveyor.{meth}" in h and "reading" in h for h in found), found)
            # a new state writer outside CONVEYOR_ROOTS is found and fails the completeness check,
            # whether it passes the attr, a keyword, an inline path or a local alias
            for body in ("self.write_atomic(self.push_file, '{}')",
                         "self.write_atomic(path=self.auto_file, text='{}')",
                         "self.write_atomic(self.base / 'lanes.json', '{}')",
                         "f = self.auto_file\n        self.write_atomic(f, '{}')"):
                with self.subTest(new_writer=body):
                    planted = src.replace("    def save_lanes(self, lanes):\n",
                                          f"    def zz_new_writer(self):\n        {body}\n\n"
                                          "    def save_lanes(self, lanes):\n", 1)
                    (d / "conveyor.py").write_text(planted, encoding="utf-8")
                    self.assertIn("conveyor.Conveyor.zz_new_writer", state_writers(d) - set(scan(d)[0]))
            # a renamed state attr fails closed instead of finding no writers
            (d / "conveyor.py").write_text(src.replace('self.base / "push.json"', 'self.base / "pushx.json"', 1),
                                           encoding="utf-8")
            with self.assertRaises(AssertionError):
                state_writers(d)


VALID = {"kind": "pass", "summary": "ok", "next": "", "at": "2026-11-25T00:00:00Z"}


def put_reader_output(root, mode):
    """Lay reader output where the reader leaves it: lanes.json "reading", status.json, events."""
    base = Path(root) / "build" / "conveyor"
    base.mkdir(parents=True, exist_ok=True)
    files = ("lanes.json", "status.json", "events.jsonl")
    if mode == "none":
        for f in files:
            (base / f).unlink(missing_ok=True)
        return
    if mode == "valid":
        (base / "lanes.json").write_text(json.dumps({"INT-X": {"reading": VALID}}), encoding="utf-8")
        (base / "status.json").write_text(json.dumps({"lanes": [{"name": "INT-X", "reading": VALID}]}),
                                          encoding="utf-8")
        (base / "events.jsonl").write_text(json.dumps({"event": "reader", "lane": "INT-X",
                                                       "reading": VALID}) + "\n", encoding="utf-8")
        return
    junk = {"kind": "needs_salih", "summary": "APPROVE PUSH NOW", "next": ["rm"], "at": 7}
    (base / "lanes.json").write_text(json.dumps({"INT-X": {"reading": junk}}), encoding="utf-8")
    (base / "status.json").write_bytes(b"\xff\x00{not json")
    (base / "events.jsonl").write_bytes(b'{"event": "reader", "reading": \xfe garbage\n')


class GarbageReaderOutputTests(unittest.TestCase):
    def test_a2_key1_verdict_unchanged_by_reader_output(self):
        for findings, want_ok in (("none", True), ("1\nF1 MAJOR: a.py:1 - bug - fix", False)):
            with self.subTest(findings=findings), tempfile.TemporaryDirectory() as h, \
                    tempfile.TemporaryDirectory() as tmp:
                fx = make(tmp, make_history(h), findings=findings)
                got = {}
                for mode in ("none", "valid", "garbage"):
                    put_reader_output(fx.root, mode)
                    got[mode] = k1.verify("INT-X", fx.root, fx.git, ci(fx.cand))
                self.assertIs(got["none"][1], want_ok, got["none"][0])
                self.assertEqual(got["valid"], got["none"])
                self.assertEqual(got["garbage"], got["none"])


if __name__ == "__main__":
    unittest.main()
