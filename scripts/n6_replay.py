"""N6-REPLAY1: replay N6's shadow decisions over the recorded conveyor history, read-only.

Every lane in ``build/conveyor/events.jsonl`` is rebuilt from the ledger (dispatch -> unfinished,
finish -> finished + status, chain -> chained check) and judged by the live shadow's own code,
``jarvis.n6_shadow`` (nothing here re-decides):
  truth: ``conveyor_verdict`` over the full history, i.e. what actually happened;
  N6:    ``n6_verdict`` at the fake clock of its decision point, the last finish among the lane
         and its chained checks, seeing only the chain events recorded by then.
Each pair is agree, different (N6-different) or not_comparable, tagged with the
N6_HANDOVER_MATRIX behaviour that owns the deciding reason. Inputs are only read; output goes
to build/n6/replay/<run>/{decisions.jsonl,report.md,summary.json} and nowhere else (never
kpi.db, SHADOW_ON or LIMITED_ON). No git, network or model call; no wall clock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # CLI: make ``jarvis`` importable
from jarvis.n6_shadow import _LANE, Snapshot, conveyor_verdict, n6_verdict  # noqa: E402

OUTCOMES = ("agree", "different", "not_comparable")
VERDICTS = ("agree", "disagree", "false_pass", "excluded", "unknown")
CI_SET = frozenset({"GREEN", "COMPLETE", "INTEGRATED", "CI_PENDING"})
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")


def _no_head(_lane: str) -> str | None:
    return None


def load_history(root: Path) -> tuple[list[dict[str, Any]], Counter[str], int]:
    """(valid events in ledger order, malformed counts by kind, non-blank lines read).

    A malformed line is counted and skipped, never fatal: not JSON, not an object, no parsable
    ``ts``, or a dispatch/finish/chain whose lane is not a plain lane name (no report path is
    ever built from it)."""
    bad: Counter[str] = Counter()
    events: list[dict[str, Any]] = []
    lines = 0
    with (root / "build" / "conveyor" / "events.jsonl").open("rb") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            lines += 1
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                bad["not_json"] += 1
                continue
            if not isinstance(event, dict):
                bad["not_object"] += 1
                continue
            try:
                datetime.fromisoformat(event["ts"])
            except (KeyError, TypeError, ValueError):
                bad["bad_ts"] += 1
                continue
            kind = event.get("event")
            name = event.get("src") if kind == "chain" else event.get("lane")
            if kind in ("dispatch", "finish", "chain") and not (
                    isinstance(name, str) and _LANE.fullmatch(name)):
                bad["bad_lane"] += 1
                continue
            if kind == "finish" and not isinstance(event.get("status"), (str, type(None))):
                bad["bad_status"] += 1  # n6_shadow tests status against frozensets
                continue
            events.append(event)
    return events, bad, lines


def behaviour(why: str) -> str:
    """The matrix row owning the reason that decided the pair.

    DECISION TAKEN: chained CLREV/CDX checks are independent reviews -> R05; report text, COMMIT
    and head evidence -> R06; lane state (finished/status) -> R07; CI-set statuses -> R17. INT
    chain checks stay "unmapped" (alternative: R04 INT dependents; no row is clearly theirs).
    R33 is the totals line, never a per-row tag (it would collapse the table to one row)."""
    found = re.match(r"chained check (\S+)", why)
    if found:
        return "R05" if found.group(1).startswith(("CLREV-", "CDX-")) else "unmapped"
    found = re.match(r"status (\S+) is not judged", why)
    if found:
        return "R17" if found.group(1) in CI_SET else "R07"
    if why.startswith(("report PASS but lane state", "lane has not finished", "lane finished")):
        return "R07"
    if why.startswith(("no report", "report", "no independent sha")):
        return "R06"
    return "unmapped"


def _class(truth: str, n6: str, why: str) -> str:
    text = _HEX.sub("<sha>", why)
    text = re.sub(r"chained check ([A-Z]+)-\S+", r"chained check \1-*", text)
    return f"conveyor {truth} / n6 {n6}: {text[:80]}"


def _rate(agree: int, different: int) -> float | None:
    return round(agree / (agree + different), 4) if agree + different else None


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.1%}"


def replay(root: Path, *, head: Callable[[str], str | None] = _no_head) -> dict[str, Any]:
    """{"summary", "records"}: one record per lane with a finish event, in decision order."""
    events, bad, lines = load_history(root)
    state: dict[str, dict[str, Any]] = {}
    last: dict[str, int] = {}
    chains: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for i, event in enumerate(events):
        kind = event.get("event")
        if kind == "dispatch":
            state[event["lane"]] = {"finished": False}
        elif kind == "finish":
            state[event["lane"]] = {"finished": True, "status": event.get("status")}
            last[event["lane"]] = i
        elif kind == "chain":
            chains.setdefault(event["src"], []).append((i, event))
    records: list[dict[str, Any]] = []
    found = 0
    for lane, end in last.items():
        # ponytail: each snapshot holds only the lane, its chain children and its chain events
        # (all n6_shadow looks at), so the replay stays O(events); widen it if N6 ever reads more.
        mine = chains.get(lane, [])
        kids = [str(e.get("id")) for _, e in mine]
        at = max([end] + [last[k] for k in kids if k in last])
        lanes = {k: state[k] for k in [lane, *kids] if k in state}
        try:
            text: str | None = (root / "build" / "reports" / f"{lane}.md").read_text(
                encoding="utf-8", errors="replace")
            found += 1
        except OSError:  # missing, a directory or unreadable: no report
            text = None
        truth = conveyor_verdict(Snapshot(lanes, tuple(e for _, e in mine), {lane: text}), lane)
        n6 = n6_verdict(Snapshot(lanes, tuple(e for i, e in mine if i <= at), {lane: text}),
                        lane, head)
        if truth[0] == "excluded":
            outcome, verdict = "not_comparable", "excluded"
        elif n6[0] == "unknown":
            outcome, verdict = "not_comparable", "unknown"
        elif n6[0] == truth[0]:
            outcome, verdict = "agree", "agree"
        else:
            outcome, verdict = "different", "false_pass" if n6[0] == "pass" else "disagree"
        why = truth[1] if truth[0] == "excluded" or verdict == "false_pass" else n6[1]
        records.append({"idx": at, "ts": events[at]["ts"], "lane": lane, "outcome": outcome,
                        "verdict": verdict, "behaviour": behaviour(why),
                        "class": _class(truth[0], n6[0], why),
                        "conveyor": {"verdict": truth[0], "reason": truth[1]},
                        "n6": {"verdict": n6[0], "reason": n6[1]}})
    records.sort(key=lambda r: (r["idx"], r["lane"]))
    totals = Counter(r["outcome"] for r in records)
    verdicts = Counter(r["verdict"] for r in records)
    rows: dict[str, Counter[str]] = {}
    classes: dict[str, Counter[str]] = {o: Counter() for o in OUTCOMES}
    for r in records:
        rows.setdefault(r["behaviour"], Counter())[r["outcome"]] += 1
        classes[r["outcome"]][r["class"]] += 1
    summary = {
        "lines": lines, "malformed": dict(sorted(bad.items())),
        "malformed_total": sum(bad.values()), "lanes": len(records), "reports_found": found,
        "totals": {o: totals[o] for o in OUTCOMES},
        "verdicts": {v: verdicts[v] for v in VERDICTS},
        "agreement": _rate(totals["agree"], totals["different"]),
        "false_pass": verdicts["false_pass"],
        "would_pass_with_head": sum(r["n6"]["reason"] == "no independent sha (git head)"
                                    for r in records),
        "behaviours": {b: {**{o: c[o] for o in OUTCOMES}, "agreement": _rate(c["agree"], c["different"])}
                       for b, c in sorted(rows.items())},
        "classes": {o: dict(sorted(classes[o].items())) for o in OUTCOMES},
        "last_ts": re.sub(r"[^0-9A-Za-z]", "", events[-1]["ts"]) if events else "empty",
    }
    return {"summary": summary, "records": records}


LIMITS = (
    "- No git head is read (historical branch tips are not recorded), so every would-be N6 PASS "
    "is not comparable (`would_pass_with_head`); false PASS (R33/E8) is NOT measured on real "
    "history, only in the tests that inject a head.",
    "- Reports are today's text of build/reports/<lane>.md; a rewritten or deleted report "
    "replays as it is now, not as it was at the decision point.",
    "- Lane state is rebuilt from dispatch/finish events only; status edits made outside a "
    "finish event (lanes.json differs) are invisible, and old finishes without a status are "
    "excluded as not judged.",
    "- ack/, approvals/done/, hold/backlog.json.bak-* and ORCH_NOTEBOOK.md are not read; "
    "orchestrator decisions (integrate, park, re-dispatch) are not replayed.",
    "- INT chain checks are tagged unmapped (no matrix row clearly owns them); R33 is this "
    "report's totals line, never a per-row tag.",
)


def report(result: dict[str, Any]) -> str:
    s = result["summary"]
    t = s["totals"]
    out = ["# N6 replay", "",
           f"- events: {s['lines']} lines, malformed {s['malformed_total']} {s['malformed']}",
           f"- lanes: {s['lanes']}, reports found {s['reports_found']}",
           f"- totals: agree {t['agree']}, different {t['different']}, not comparable "
           f"{t['not_comparable']}; agreement {_pct(s['agreement'])}",
           f"- R33 totals: false PASS {s['false_pass']}, would pass with a git head "
           f"{s['would_pass_with_head']}", "",
           "| behaviour | agree | different | not-comparable | agreement |",
           "|---|---|---|---|---|"]
    for name, row in s["behaviours"].items():
        out.append(f"| {name} | {row['agree']} | {row['different']} | {row['not_comparable']} "
                   f"| {_pct(row['agreement'])} |")
    out += ["", "## top disagreement classes", ""]
    top = sorted(s["classes"]["different"].items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    for cls, count in top:
        out.append(f"- {count} x {cls}")
        out += [f"  - {r['ts']} {r['lane']}" for r in result["records"] if r["class"] == cls][:3]
    if not top:
        out.append("none")
    out += ["", "## limits", "", *LIMITS]
    return "\n".join(out) + "\n"


def write(root: Path, run: str, result: dict[str, Any]) -> Path:
    """Write the three outputs under build/n6/replay/<run>/; any other target is refused."""
    base = (root / "build" / "n6" / "replay").resolve()
    out = (base / run).resolve()
    if out == base or not out.is_relative_to(base):
        raise SystemExit(f"n6_replay: refusing to write outside {base}: {run!r}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(result["summary"], sort_keys=True, indent=1)
                                      + "\n", encoding="utf-8", newline="\n")
    (out / "decisions.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in result["records"]),
        encoding="utf-8", newline="\n")
    (out / "report.md").write_text(report(result), encoding="utf-8", newline="\n")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="N6 replay over the recorded conveyor history")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run", help="folder under build/n6/replay (default: last event ts)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    result = replay(root)
    out = write(root, args.run or result["summary"]["last_ts"], result)
    digest = hashlib.sha256((out / "summary.json").read_bytes()).hexdigest()
    print(f"{out}\nsummary.json sha256 {digest}\n"
          f"totals {json.dumps(result['summary']['totals'], sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
