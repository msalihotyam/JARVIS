"""Read-only lesson -> rule -> code/test coverage report (N6 section 4.3).

Reads build/lessons/rules_classification.jsonl and candidates.jsonl, prints
class counts, the share of live rules enforced in code, which enforced rules
a test under tests/ names, and the live rules not in code. Writes nothing.
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOT_LIVE = {"historical", "dropped_dora"}
ENFORCED = "enforced_in_code"


def natural_key(text):
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", text)]


def read_jsonl(path, required):
    """Return (rows, unreadable line numbers); a missing file raises FileNotFoundError."""
    rows, bad = [], []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad.append(number)
                continue
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in required):
                bad.append(number)
                continue
            rows.append(row)
    return rows, bad


def named_in_tests(rule, root):
    """True when a file under <root>/tests names the rule id as a whole word."""
    done = subprocess.run(
        ["git", "grep", "--no-index", "-q", "-w", "-F", "-e", rule, "--", "tests"],
        cwd=root, capture_output=True, text=True,
    )
    if done.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed for {rule}: {done.stderr.strip()[:200]}")
    return done.returncode == 0


def build_report(rules_path, candidates_path, root):
    rules, rules_bad = read_jsonl(rules_path, ("rule", "class"))
    candidates, cand_bad = read_jsonl(candidates_path, ())
    classes = Counter(r["class"] for r in rules)
    live = [r for r in rules if r["class"] not in NOT_LIVE]
    enforced = sorted((r["rule"] for r in live if r["class"] == ENFORCED), key=natural_key)
    guarded = [r for r in enforced if named_in_tests(r, root)]
    status = Counter(str(c.get("dogrulama_durumu", "(none)")) for c in candidates)
    return {
        "classes": dict(sorted(classes.items())),
        "live": len(live),
        "enforced_in_code": len(enforced),
        "coverage_pct": round(100 * len(enforced) / len(live), 1) if live else 0.0,
        "guarded": guarded,
        "enforced_no_test": [r for r in enforced if r not in guarded],
        "live_not_in_code": [
            {"rule": r["rule"], "class": r["class"], "kaynak": r.get("kaynak", "")}
            for r in sorted(live, key=lambda r: natural_key(r["rule"]))
            if r["class"] != ENFORCED
        ],
        "candidates": {"total": len(candidates), "dogrulama_durumu": dict(sorted(status.items()))},
        "unreadable": {"rules": rules_bad, "candidates": cand_bad},
    }


def render(rep):
    out = ["classes:"]
    out += [f"  {name}: {count}" for name, count in rep["classes"].items()]
    out.append(f"live (all but historical, dropped_dora): {rep['live']}")
    out.append(f"coverage: {rep['enforced_in_code']}/{rep['live']} = {rep['coverage_pct']:.1f}% enforced_in_code")
    out.append(f"guarded (a test names it): {', '.join(rep['guarded']) or '-'}")
    out.append(f"enforced, no test names it: {', '.join(rep['enforced_no_test']) or '-'}")
    out.append(f"live rules not in code: {len(rep['live_not_in_code'])}")
    out += [f"  {r['rule']} [{r['class']}] {r['kaynak']}".rstrip() for r in rep["live_not_in_code"]]
    out.append(f"candidates: {rep['candidates']['total']}")
    out += [f"  {k}: {v}" for k, v in rep["candidates"]["dogrulama_durumu"].items()]
    for name, lines in rep["unreadable"].items():
        out.append(f"unreadable {name}: {len(lines)}" + (f" (lines {', '.join(map(str, lines))})" if lines else ""))
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rules", default=str(ROOT / "build/lessons/rules_classification.jsonl"))
    parser.add_argument("--candidates", default=str(ROOT / "build/lessons/candidates.jsonl"))
    parser.add_argument("--root", default=str(ROOT), help="checkout whose tests/ is searched")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    for path in (args.rules, args.candidates):
        if not Path(path).is_file():
            print(f"lesson_coverage: missing input file: {path}", file=sys.stderr)
            return 2
    rep = build_report(args.rules, args.candidates, args.root)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
