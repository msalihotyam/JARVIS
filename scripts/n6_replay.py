"""N6-REPLAY1: replay N6's shadow decisions over the recorded conveyor history, read-only.

Every finish event in ``build/conveyor/events.jsonl`` is one decision, rebuilt from the ledger
(dispatch -> unfinished, finish -> finished + status, chain -> chained check) as it stood at the
decision's own point (see ``replay``) and judged by the live shadow's own code,
``jarvis.n6_shadow`` (nothing here re-decides):
  truth: ``conveyor_verdict`` on the lane state at the decision point plus the decision's chains;
  N6:    ``n6_verdict`` on the same state, seeing only the chain events recorded by then.
Each pair is agree, different (N6-different) or not_comparable, tagged with the
N6_HANDOVER_MATRIX behaviour that owns the deciding reason. Inputs are only read; output goes
to build/n6/replay/<run>/{decisions.jsonl,report.md,summary.json} and nowhere else (never
kpi.db, SHADOW_ON or LIMITED_ON). No git, network or model call; no wall clock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # CLI: make ``jarvis`` importable
from jarvis.n6_shadow import _LANE, Snapshot, conveyor_verdict, n6_verdict  # noqa: E402

OUTCOMES = ("agree", "different", "not_comparable")
VERDICTS = ("agree", "disagree", "false_pass", "excluded", "unprovable", "unknown")
CI_SET = frozenset({"GREEN", "COMPLETE", "INTEGRATED", "CI_PENDING"})
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")


def _no_head(_lane: str) -> str | None:
    return None


def load_history(root: Path) -> tuple[list[dict[str, Any]], Counter[str], int]:
    """(valid events in ledger order, malformed counts by kind, non-blank lines read).

    A malformed line is counted and skipped, never fatal: not JSON, not an object, no parsable
    ``ts``, or a dispatch/finish/chain whose lane (a chain's ``src`` and ``id``) is not a plain
    lane name (no report path is ever built from it)."""
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
            names = [event.get("src"), event.get("id")] if kind == "chain" else [event.get("lane")]
            if kind in ("dispatch", "finish", "chain") and not all(
                    isinstance(name, str) and _LANE.fullmatch(name) for name in names):
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


def _judge(root: Path, events: list[dict[str, Any]], d: dict[str, Any],
           state: dict[str, dict[str, Any]], texts: dict[str, str | None],
           finishes: dict[str, list[int]], head: Callable[[str], str | None]) -> dict[str, Any]:
    """One decision on the state as of its point (``state`` holds events <= d["at"])."""
    lane, at = d["lane"], d["at"]
    # ponytail: each snapshot holds only the lane, its chain children and its chain events
    # (all n6_shadow looks at), so the replay stays O(events); widen it if N6 ever reads more.
    mine = [events[c] for c in d["chains"]]
    kids = [e["id"] for e in mine]
    lanes = {n: dict(state[n]) for n in [lane, *kids] if n in state}
    for kid, idx in d["kid_at"].items():  # each child as of its own first finish in the window
        lanes[kid] = {"finished": True, "status": events[idx].get("status")}
    if lane not in texts:
        try:
            texts[lane] = (root / "build" / "reports" / f"{lane}.md").read_text(
                encoding="utf-8", errors="replace")
        except OSError:  # missing, a directory or unreadable: no report
            texts[lane] = None
    text = texts[lane]
    truth = conveyor_verdict(Snapshot(lanes, tuple(mine), {lane: text}), lane)
    n6 = n6_verdict(Snapshot(lanes, tuple(events[c] for c in d["chains"] if c <= at),
                             {lane: text}), lane, head)
    times = len(finishes[lane])
    if truth[0] == "excluded":
        outcome, verdict, why = "not_comparable", "excluded", truth[1]
    elif text is None or times > 1 or d["close"] < len(events):  # report not tied to this decision
        outcome, verdict = "not_comparable", "unprovable"
        why = "report unprovable: " + ("no report" if text is None else
                                       f"lane finished {times} times" if times > 1 else
                                       "lane redispatched after this finish")
    elif n6[0] == "unknown":
        outcome, verdict, why = "not_comparable", "unknown", n6[1]
    elif n6[0] == truth[0]:
        outcome, verdict, why = "agree", "agree", n6[1]
    elif n6[0] == "pass":
        outcome, verdict, why = "different", "false_pass", truth[1]
    else:
        outcome, verdict, why = "different", "disagree", n6[1]
    return {"idx": at, "finish": d["f"], "ts": events[at]["ts"], "lane": lane,
            "outcome": outcome, "verdict": verdict, "behaviour": behaviour(why),
            "class": _class(truth[0], n6[0], why),
            "conveyor": {"verdict": truth[0], "reason": truth[1]},
            "n6": {"verdict": n6[0], "reason": n6[1]}}


def replay(root: Path, *, head: Callable[[str], str | None] = _no_head) -> dict[str, Any]:
    """{"summary", "records"}: one record per finish event, judged at its own decision point.

    A decision opens at a lane's finish and owns the chain events (src == lane) recorded until
    the lane's next dispatch or finish; a chain recorded while the lane is not finished waits for
    its next finish. Its point ``at`` is the finish, or the later first finish of an owned child
    inside that window. Both verdicts see the lane state as of ``at`` (events <= at, nothing
    later); N6 sees only the owned chains recorded by ``at``, the truth all owned chains."""
    events, bad, lines = load_history(root)
    finishes: dict[str, list[int]] = {}
    decisions: list[dict[str, Any]] = []
    current: dict[str, dict[str, Any] | None] = {}
    pending: dict[str, list[int]] = {}
    for i, event in enumerate(events):
        kind = event.get("event")
        if kind in ("dispatch", "finish"):
            lane = event["lane"]
            open_ = current.get(lane)
            if open_:
                open_["close"] = i
            current[lane] = None
            if kind == "finish":
                finishes.setdefault(lane, []).append(i)
                current[lane] = {"lane": lane, "f": i, "close": len(events),
                                 "chains": pending.pop(lane, [])}
                decisions.append(current[lane])
        elif kind == "chain":
            open_ = current.get(event["src"])
            (open_["chains"] if open_ else pending.setdefault(event["src"], [])).append(i)
    for d in decisions:
        d["at"], d["kid_at"] = d["f"], {}
        for c in d["chains"]:
            kid = finishes.get(events[c]["id"], [])
            j = bisect_left(kid, d["f"])
            if j < len(kid) and kid[j] < d["close"]:
                d["kid_at"][events[c]["id"]] = kid[j]
                d["at"] = max(d["at"], kid[j])
    order = sorted(decisions, key=lambda d: (d["at"], d["f"], d["lane"]))
    texts: dict[str, str | None] = {}
    state: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    k = 0
    for i, event in enumerate(events):
        if event.get("event") == "dispatch":
            state[event["lane"]] = {"finished": False}
        elif event.get("event") == "finish":
            state[event["lane"]] = {"finished": True, "status": event.get("status")}
        while k < len(order) and order[k]["at"] == i:
            records.append(_judge(root, events, order[k], state, texts, finishes, head))
            k += 1
    totals = Counter(r["outcome"] for r in records)
    verdicts = Counter(r["verdict"] for r in records)
    rows: dict[str, Counter[str]] = {}
    classes: dict[str, Counter[str]] = {o: Counter() for o in OUTCOMES}
    for r in records:
        rows.setdefault(r["behaviour"], Counter())[r["outcome"]] += 1
        classes[r["outcome"]][r["class"]] += 1
    summary = {
        "lines": lines, "malformed": dict(sorted(bad.items())),
        "malformed_total": sum(bad.values()), "decisions": len(records),
        "lanes": len(finishes), "reports_found": sum(t is not None for t in texts.values()),
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
    "- Each finish is one decision, judged on the ledger state at its point (the finish, or the "
    "later first finish of a chained child recorded before the lane's next dispatch/finish); the "
    "truth also counts that decision's chain links recorded after the point (strict <= point "
    "for both would make false PASS 0 by construction).",
    "- Reports are today's text of build/reports/<lane>.md, so a decision with no report, of "
    "a lane that finished more than once, or followed by a redispatch of the lane, is not "
    "comparable (unprovable): the report cannot be tied to that one decision. Each chained "
    "child is seen as of its own first finish inside the decision's window.",
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
           f"- decisions: {s['decisions']} over {s['lanes']} lanes, reports found "
           f"{s['reports_found']}",
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
    base = root.resolve() / "build" / "n6" / "replay"  # unresolved below root: a linked
    out = (base / run).resolve()  # build/n6 or replay resolves outside and is refused
    if out == base or not out.is_relative_to(base):
        raise SystemExit(f"n6_replay: refusing to write outside {base}: {run!r}")
    out.mkdir(parents=True, exist_ok=True)
    texts = {"summary.json": json.dumps(result["summary"], sort_keys=True, indent=1) + "\n",
             "decisions.jsonl": "".join(json.dumps(r, sort_keys=True) + "\n"
                                        for r in result["records"]),
             "report.md": report(result)}
    for name in texts:
        if (out / name).is_symlink():
            raise SystemExit(f"n6_replay: refusing to write through a symlink: {out / name}")
    for name, text in texts.items():  # a fresh file renamed over the old: never follows a link
        fd, tmp = tempfile.mkstemp(dir=out, prefix=f".{name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp, out / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
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
