"""PM32 scorer: compare project mode with a single agent from recorded runs.

It runs no model; it only reads two JSON files and writes build/v24/PM32.md.

Task list (--tasks): a JSON list of task id strings, e.g. ["PM32-01", ...];
an id is 1-80 ASCII letters, digits, ".", "_" or "-", starting with a letter or digit.
Run records (--runs): {"runs": [record, ...]}, one record per task and arm:
    {"task_id": "PM32-01",            # must be in the task list
     "arm": "project" | "single",
     "claimed_done": true | false,
     "hidden_check_result": "pass" | "fail" | null,   # null = missing
     "quota": number >= 0 | null,                     # null = missing
     "duration": number >= 0 | null}                  # seconds, null = missing
A task with no record (or a null hidden_check_result) for an arm is "missing":
never pass, never fail. Accepted = claimed_done and hidden checks pass.
False-done = claimed_done with failing hidden checks.
Exit 0: report written. Exit 2: input refused, nothing written.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path

ARMS = ("project", "single")
ARM_TITLES = {"project": "project mode", "single": "single agent"}
MIN_TASKS = 20
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "build" / "v24" / "PM32.md"
MISSING = "missing"
# Ids are written raw into the report: an allowlist keeps line breaks, separators and look-alikes out.
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


class Refused(ValueError):
    pass


def _reject_constant(name):
    raise Refused(f"non-finite number {name} refused")


def _load_json(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Refused(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except Refused:
        raise
    except (ValueError, RecursionError) as exc:  # ValueError covers JSONDecodeError and int digit limits
        raise Refused(f"{path}: invalid JSON: {str(exc)[:80]}") from exc


def load_tasks(data):
    if not isinstance(data, list) or not data:
        raise Refused("task list must be a non-empty JSON list")
    seen = set()
    for task_id in data:
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise Refused(f"bad task id {task_id!r:.80}")
        if task_id in seen:
            raise Refused(f"duplicate task id {task_id!r}")
        seen.add(task_id)
    return sorted(seen)


def _amount(record, key):
    value = record.get(key)
    if value is None:
        return None
    bad = Refused(f"{record.get('task_id')}/{record.get('arm')}: bad {key} {value!r:.80}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise bad
    try:
        value = float(value)  # same report text: the totals are formatted as floats anyway
    except OverflowError:
        raise bad from None
    if not math.isfinite(value) or value < 0:
        raise bad
    return value


def load_runs(data, tasks):
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise Refused('run records must be {"runs": [...]}')
    known = set(tasks)
    runs = {}
    for record in data["runs"]:
        if not isinstance(record, dict):
            raise Refused(f"run record must be an object, got {record!r:.80}")
        task_id, arm = record.get("task_id"), record.get("arm")
        if not isinstance(task_id, str) or task_id not in known:
            raise Refused(f"unknown task id {task_id!r:.80}")
        if arm not in ARMS:
            raise Refused(f"{task_id}: unknown arm {arm!r:.80}")
        if (task_id, arm) in runs:
            raise Refused(f"{task_id}/{arm}: duplicate run record")
        claimed = record.get("claimed_done")
        if not isinstance(claimed, bool):
            raise Refused(f"{task_id}/{arm}: claimed_done must be true or false")
        hidden = record.get("hidden_check_result")
        if hidden not in ("pass", "fail", None):
            raise Refused(f"{task_id}/{arm}: bad hidden_check_result {hidden!r:.80}")
        runs[(task_id, arm)] = {
            "claimed_done": claimed,
            "hidden": hidden,
            "quota": _amount(record, "quota"),
            "duration": _amount(record, "duration"),
        }
    return runs


def _ratio(num, den):
    if den == 0:
        return "n/a"
    return f"{num}/{den} ({100 * num / den:.1f}%)"


def score_arm(tasks, runs, arm):
    scored, missing, accepted, claimed, false_done = 0, [], 0, 0, 0
    quota, duration = 0, 0
    quota_missing = duration_missing = False
    for task_id in tasks:  # sorted order keeps float sums deterministic
        run = runs.get((task_id, arm))
        if run is None or run["hidden"] is None:
            missing.append(task_id)
        else:
            scored += 1
            passed = run["hidden"] == "pass"
            if run["claimed_done"]:
                claimed += 1
                accepted += passed
                false_done += not passed
        if run is None or run["quota"] is None:
            quota_missing = True
        else:
            quota += run["quota"]
        if run is None or run["duration"] is None:
            duration_missing = True
        else:
            duration += run["duration"]
    if not (math.isfinite(quota) and math.isfinite(duration)):
        raise Refused(f"{arm}: quota or duration total overflows")
    if quota_missing:
        per_accepted = MISSING
    elif accepted == 0:
        per_accepted = "n/a"
    else:
        per_accepted = f"{quota / accepted:.2f}"
    return {
        "scored": scored,
        "missing": missing,
        "accepted": accepted,
        "false_done": false_done,
        "pass_rate": _ratio(accepted, scored),
        "pass_fraction": Fraction(accepted, scored) if scored else None,
        "false_done_rate": _ratio(false_done, claimed),
        "quota_total": MISSING if quota_missing else f"{quota:.2f}",
        "quota_per_accepted": per_accepted,
        "duration_total": MISSING if duration_missing else f"{duration:.1f}s",
    }


def verdict(tasks, scores):
    if len(tasks) < MIN_TASKS:
        return "incomplete"
    if any(s["missing"] or s["scored"] < MIN_TASKS for s in scores.values()):
        return "incomplete"
    return "yes" if scores["project"]["pass_fraction"] >= scores["single"]["pass_fraction"] else "no"


ROWS = (
    ("tasks in set", None),
    ("scored runs", "scored"),
    ("missing runs", None),
    ("accepted (claimed done, hidden checks pass)", "accepted"),
    ("pass rate (accepted / scored)", "pass_rate"),
    ("false-done (claimed done, hidden checks fail)", "false_done"),
    ("false-done rate (false-done / claimed done)", "false_done_rate"),
    ("quota total", "quota_total"),
    ("quota per accepted task", "quota_per_accepted"),
    ("duration total", "duration_total"),
)


def render(tasks, runs):
    scores = {arm: score_arm(tasks, runs, arm) for arm in ARMS}
    for s in scores.values():
        s["tasks in set"], s["missing runs"] = len(tasks), len(s["missing"])
    lines = [
        "# PM32 comparison: project mode vs single agent",
        "",
        "| metric | " + " | ".join(ARM_TITLES[a] for a in ARMS) + " |",
        "|---|" + "---|" * len(ARMS),
    ]
    for name, key in ROWS:
        lines.append(f"| {name} | " + " | ".join(str(scores[a][key or name]) for a in ARMS) + " |")
    lines.append("")
    for arm in ARMS:
        lines.append(f"Missing ({ARM_TITLES[arm]}): " + (", ".join(scores[arm]["missing"]) or "none"))
    lines += ["", f"project mode pass rate >= single agent: {verdict(tasks, scores)}", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="PM32 scorer (reads recorded runs, calls no model)")
    parser.add_argument("--tasks", required=True, help="JSON list of PM32 task ids")
    parser.add_argument("--runs", required=True, help='JSON {"runs": [...]} run records')
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="report path (default build/v24/PM32.md)")
    args = parser.parse_args(argv)
    try:
        tasks = load_tasks(_load_json(args.tasks))
        text = render(tasks, load_runs(_load_json(args.runs), tasks))
    except Refused as exc:
        print(f"pm32_compare: refused: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"pm32_compare: wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
