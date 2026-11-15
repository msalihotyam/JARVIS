"""MUTGEN: generate a deterministic mutation spec list for scripts/mutate.py (stdlib only).

    python scripts/mutgen.py <repo-relative file> [--tests <id> ...] [--out specs.json]
                             [--cap 60] [--repo .]

Operators (one site each): cmp (== != < <= > >= flipped), bool (True <-> False),
not (removed), num (int literal + 1), ret (return X -> return None),
raise (raise ... -> pass), early (a return directly inside an if -> pass).
Sites are found with tokenize/ast, so strings, docstrings and comments are never
touched. ``old`` is the whole physical line; a site whose line does not occur
exactly once in the file is skipped and counted as ambiguous, and a mutant that
no longer compiles is skipped and counted as invalid. At most 60 specs per run:
over the cap the per-operator lists are interleaved round-robin before cutting,
so every operator stays represented. Same input -> byte-identical JSON.
Tests default to tests.test_<stem> when tests/test_<stem>.py exists.
"""

import argparse
import ast
import io
import json
import sys
import tokenize
from pathlib import Path

MAX_MUTATIONS = 60
OPS = ("cmp", "bool", "not", "num", "ret", "raise", "early")
_CMP = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}
_BOOL = {"True": "False", "False": "True"}


def _col(line, byte_col):
    """ast offsets are UTF-8 byte offsets; return the character offset."""
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))


def _token_sites(text, lines):
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        (row, col), (erow, ecol) = tok.start, tok.end
        if row != erow:
            continue
        line, s = lines[row - 1], tok.string
        if tok.type == tokenize.OP and s in _CMP:
            yield row, col, "cmp", line[:col] + _CMP[s] + line[ecol:]
        elif tok.type == tokenize.NAME and s in _BOOL:
            yield row, col, "bool", line[:col] + _BOOL[s] + line[ecol:]
        elif tok.type == tokenize.NAME and s == "not":
            yield row, col, "not", line[:col] + line[ecol:].lstrip()
        elif tok.type == tokenize.NUMBER and s.isdigit():
            yield row, col, "num", line[:col] + str(int(s) + 1) + line[ecol:]


def _ast_sites(tree, lines):
    early = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            early.update(id(s) for s in node.body + node.orelse if isinstance(s, ast.Return))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Return, ast.Raise)) or node.lineno != node.end_lineno:
            continue
        line = lines[node.lineno - 1]
        col, end = _col(line, node.col_offset), _col(line, node.end_col_offset)
        head, tail = line[:col], line[end:]
        if isinstance(node, ast.Raise):
            yield node.lineno, col, "raise", head + "pass" + tail
            continue
        value = node.value
        if value is not None and not (isinstance(value, ast.Constant) and value.value is None):
            yield node.lineno, col, "ret", head + "return None" + tail
        if id(node) in early:
            yield node.lineno, col, "early", head + "pass" + tail


def generate(text, rel_file, tests, cap=MAX_MUTATIONS):
    """Return (specs, stats) for one file's text; deterministic for the same input."""
    cap = max(0, min(cap, MAX_MUTATIONS))
    lines = text.split("\n")
    sites = set(_token_sites(text, lines)) | set(_ast_sites(ast.parse(text), lines))
    stats = {"candidates": len(sites), "ambiguous": 0, "invalid": 0, "capped": 0,
             "generated": 0}
    by_op, seen = {op: [] for op in OPS}, set()
    for row, col, op, new in sorted(sites):
        old = lines[row - 1]
        if text.count(old) != 1:
            stats["ambiguous"] += 1
            continue
        if old == new or (old, new) in seen:
            continue
        try:
            compile(text.replace(old, new, 1), rel_file, "exec")
        except (SyntaxError, ValueError):
            stats["invalid"] += 1
            continue
        seen.add((old, new))
        by_op[op].append((row, col, op, {"name": f"{op}-L{row}C{col}", "file": rel_file,
                                         "old": old, "new": new, "tests": list(tests)}))
    pool, queues = [], [by_op[op] for op in OPS]
    while any(queues):
        for q in queues:
            if q:
                pool.append(q.pop(0))
    kept = sorted(pool[:cap], key=lambda item: item[:3])
    stats["capped"], stats["generated"] = len(pool) - len(kept), len(kept)
    return [item[3] for item in kept], stats


def dump(specs):
    return json.dumps(specs, indent=1, sort_keys=True) + "\n"


def default_tests(repo, rel):
    stem = Path(rel).stem
    return [f"tests.test_{stem}"] if (Path(repo) / "tests" / f"test_{stem}.py").is_file() else []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file", help="repo-relative path of the target")
    ap.add_argument("--tests", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--cap", type=int, default=MAX_MUTATIONS)
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    if Path(a.file).is_absolute():
        ap.error("file must be repo-relative")
    if not 0 < a.cap <= MAX_MUTATIONS:
        ap.error(f"--cap must be 1..{MAX_MUTATIONS}")
    rel = Path(a.file).as_posix()
    tests = a.tests or default_tests(a.repo, rel)
    if not tests:
        ap.error("no tests/test_<stem>.py found; pass --tests")
    text = (Path(a.repo) / rel).read_text(encoding="utf-8")
    specs, stats = generate(text, rel, tests, a.cap)
    data = dump(specs)
    if a.out:
        Path(a.out).write_text(data, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(data)
    print(" ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
