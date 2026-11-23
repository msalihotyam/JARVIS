"""REVIEWKPI1: review finding outcomes and pass-rate KPI over CLREV-*/CDX-* reports.

Read-only over build/reports and build/conveyor/events.jsonl; writes only
build/orch/review_kpi.json and prints a short table. Deterministic: no clock,
no network, no model calls.

Outcome rules (first match wins, per finding):
  dismissed  a CHECK line of the same report cites the finding's file:line with a
             dismissal word (not a defect, not real, rejected, downgrad, ...);
  repeated   a later round of the same series raises the same class
             (same path, line within LINE_WINDOW);
  fixed      a later FIX lane report of the series names the finding's file and the
             next review round of the series is clean (all PASS);
  dismissed  the chain proceeded without a fix: the review itself is PASS, or an INT
             of this or a later slice of the series landed (events.jsonl);
  open       anything else (series not finished).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

NAME_RE = re.compile(r"^(CLREV|CDX)-(.+?)(FIX(\d*))?\.md$")
STATUS_RE = re.compile(r"^STATUS: (PASS|BLOCKED)$")
COMMIT_RE = re.compile(r"^COMMIT: [0-9a-f]{40}$")
FINDING_RE = re.compile(r"^F(\d+) ([A-Z]+): (.*)$")
LOC_RE = re.compile(r"([\w.\-/\\]+\.\w+):(\d+)")
DISMISS_RE = re.compile(r"not a defect|not real|rejected|downgrad|deliberate|false positive", re.I)
INT_SUFFIX_RE = re.compile(r"-(R\d*|AFX|S\d*)$")
SEVERITIES = ("CRITICAL", "MAJOR", "MINOR", "NIT")
OUTCOMES = ("fixed", "dismissed", "repeated", "open")
REVIEWER = {"CLREV": "claude", "CDX": "codex"}
# shortcut: a class is path + line within 40; upgrade to symbol matching if line drift misclassifies
LINE_WINDOW = 40


def norm(path):
    return path.replace("\\", "/").removeprefix("./")


def parse_report(path):
    """One report -> (review dict, malformed reason or None)."""
    kind, root, fix, n = NAME_RE.match(path.name).groups()
    lines = [x.strip() for x in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    rev = {"report": path.name, "reviewer": REVIEWER[kind], "slice": root + (fix or ""), "series": root,
           "round": (int(n) if n else 1) if fix else 0, "status": None, "findings": []}
    status = STATUS_RE.match(lines[0]) if lines else None
    if not status:
        return rev, "first line is not STATUS: PASS|BLOCKED"
    rev["status"] = status.group(1)
    if not any(COMMIT_RE.match(x) for x in lines):
        return rev, "no COMMIT: <40 hex> line"
    checks = [x for x in lines if x.startswith("CHECK") and DISMISS_RE.search(x)]
    for line in lines:
        if not re.match(r"F\d+\b", line):
            continue
        f = FINDING_RE.match(line)
        if not f or f.group(2) not in SEVERITIES:
            return rev, "bad finding line: " + line[:60]
        loc = LOC_RE.search(f.group(3))
        if not loc:
            return rev, f"F{f.group(1)} has no file:line"
        file, num = norm(loc.group(1)), int(loc.group(2))
        # matched by location, never by F<n> (Codex numbering collides with the report's)
        cite = re.compile(re.escape(f"{file.rsplit('/', 1)[-1]}:{num}") + r"(?!\d)")
        rev["findings"].append({"id": "F" + f.group(1), "severity": f.group(2), "file": file, "line": num,
                                "check_dismissed": any(cite.search(c) for c in checks)})
    if rev["status"] == "BLOCKED" and not rev["findings"]:
        return rev, "BLOCKED with no F<n> finding"
    return rev, None


def read_events(path):
    """events.jsonl -> (report stem -> day, landed slice names, unparsable line count)."""
    days, landed, bad = {}, set(), 0
    if not path.is_file():
        return days, landed, bad
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                bad += 1
                continue
            if not isinstance(e, dict):
                bad += 1
                continue
            ev, lane, day = e.get("event"), str(e.get("lane") or ""), str(e.get("ts") or "")[:10]
            if ev == "finish" and lane:
                days[lane] = day
            elif ev == "chain" and e.get("id"):
                days.setdefault(str(e["id"]), day)
            if ev == "chain" and e.get("step") == "INT" and e.get("src"):
                landed.add(str(e["src"]))
            if lane.startswith("INT-") and ((ev == "ci_result" and e.get("result") == "success")
                                            or (ev == "finish" and e.get("status") == "PASS")):
                landed.add(INT_SUFFIX_RE.sub("", lane[4:]))
    return days, landed, bad


def fix_text(reports, series, n):
    names = [f"{series}FIX{n}.md"] + ([f"{series}FIX.md"] if n == 1 else [])
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in (reports / x for x in names) if p.is_file())


def same_class(f, g):
    return f["file"] == g["file"] and abs(f["line"] - g["line"]) <= LINE_WINDOW


def clean_round(rs, n):
    return all(r["status"] == "PASS" for r in rs if r["round"] == n)


def assign_outcomes(reviews, landed, reports):
    by_series = defaultdict(list)
    for r in reviews:
        by_series[r["series"]].append(r)
    for series, rs in by_series.items():
        for r in rs:
            later = [x for x in rs if x["round"] > r["round"]]
            nxt = min((x["round"] for x in later), default=None)
            fixes = "" if nxt is None else "\n".join(fix_text(reports, series, k)
                                                     for k in range(r["round"] + 1, nxt + 1))
            for f in r["findings"]:
                if f.pop("check_dismissed"):
                    f["outcome"] = "dismissed"
                elif any(same_class(f, g) for x in later for g in x["findings"]):
                    f["outcome"] = "repeated"
                elif nxt is not None and clean_round(rs, nxt) and f["file"].rsplit("/", 1)[-1] in fixes:
                    f["outcome"] = "fixed"
                elif r["status"] == "PASS" or any(x["slice"] in landed for x in [r, *later]):
                    f["outcome"] = "dismissed"
                else:
                    f["outcome"] = "open"
    return by_series


def mean(xs):
    return round(sum(xs) / len(xs), 2) if xs else None


def stats(reviews, malformed, series=True):
    """Aggregate one group. Series metrics use only this group's reviews of each series."""
    n, first = len(reviews), [r for r in reviews if r["round"] == 0]
    found = [f for r in reviews for f in r["findings"]]
    outcomes = {o: sum(f["outcome"] == o for f in found) for o in OUTCOMES}
    out = {"reviews": n, "malformed": malformed, "first_round": len(first),
           "first_round_pass": mean([r["status"] == "PASS" for r in first]),
           "findings": len(found),
           "findings_per_review": {s: (round(sum(f["severity"] == s for f in found) / n, 2) if n else None)
                                   for s in SEVERITIES},
           "outcomes": outcomes,
           "outcome_share": {o: (round(c / len(found), 2) if found else None) for o, c in outcomes.items()}}
    if series:
        groups = defaultdict(list)
        for r in reviews:
            groups[r["series"]].append(r)
        green = []
        for rs in groups.values():
            rounds = sorted({r["round"] for r in rs})
            ok = [k for k in rounds if clean_round(rs, k)]
            if ok:
                green.append(rounds.index(ok[0]) + 1)
        out.update({"series": len(groups), "series_green": len(green), "rounds_to_green_mean": mean(green),
                    "fix_rounds_mean": mean([max(r["round"] for r in rs) for rs in groups.values()])})
    return out


def build(root):
    root = Path(root)
    reports = root / "build" / "reports"
    days, landed, bad_events = read_events(root / "build" / "conveyor" / "events.jsonl")
    reviews, malformed, skipped = [], [], 0
    for p in sorted(reports.glob("*.md")) if reports.is_dir() else []:
        if not NAME_RE.match(p.name):
            skipped += p.name.startswith(("CLREV", "CDX"))  # legacy names without a hyphen
            continue
        rev, why = parse_report(p)
        rev["day"] = days.get(p.stem) or "unknown"
        if why:
            malformed.append({"report": p.name, "reviewer": rev["reviewer"], "day": rev["day"], "reason": why})
        else:
            reviews.append(rev)
    assign_outcomes(reviews, landed, reports)

    def bad(pred):
        return sum(1 for m in malformed if pred(m))

    groups = {"all": stats(reviews, len(malformed))}
    for who in ("claude", "codex"):
        groups[who] = stats([r for r in reviews if r["reviewer"] == who], bad(lambda m, w=who: m["reviewer"] == w))
    by_day = {}
    for day in sorted({r["day"] for r in reviews} | {m["day"] for m in malformed}):
        by_day[day] = {who: stats([r for r in reviews if r["day"] == day and r["reviewer"] == who],
                                  bad(lambda m, w=who, d=day: m["reviewer"] == w and m["day"] == d), series=False)
                       for who in ("claude", "codex")}
    return {"rules": __doc__.split("Outcome rules", 1)[1].strip(), "groups": groups, "by_day": by_day,
            "malformed": malformed, "legacy_names_skipped": skipped, "events_unparsable": bad_events,
            "reviews": reviews}


def pct(x):
    return "-" if x is None else f"{round(x * 100)}%"


def num(x):
    return "-" if x is None else f"{x:g}"


def table(data):
    head = ("group", "reviews", "1st-pass", "MAJ/rev", "MIN/rev", "NIT/rev", "fixed", "dismissed",
            "repeated", "open", "to-green", "fix-rounds", "malformed")
    rows = [head]
    for name, g in data["groups"].items():
        fpr, sh = g["findings_per_review"], g["outcome_share"]
        rows.append((name, str(g["reviews"]), pct(g["first_round_pass"]), num(fpr["MAJOR"]), num(fpr["MINOR"]),
                     num(fpr["NIT"]), pct(sh["fixed"]), pct(sh["dismissed"]), pct(sh["repeated"]), pct(sh["open"]),
                     num(g["rounds_to_green_mean"]), num(g["fix_rounds_mean"]), str(g["malformed"])))
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="checkout holding build/")
    args = ap.parse_args(argv)
    data = build(args.root)
    out = Path(args.root) / "build" / "orch" / "review_kpi.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(table(data))
    print(f"days: {len(data['by_day'])}  legacy names skipped: {data['legacy_names_skipped']}  "
          f"events unparsable: {data['events_unparsable']}  json: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
