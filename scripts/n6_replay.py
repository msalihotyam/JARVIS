"""N6-REPLAY1: replay N6's shadow decisions over the recorded conveyor history, read-only.

Every finish event in ``build/conveyor/events.jsonl`` is one decision, rebuilt from the ledger
(dispatch -> unfinished, finish -> finished + status, chain -> chained check) as it stood at the
decision's own point (see ``replay``) and judged by the live shadow's own code,
``jarvis.n6_shadow`` (nothing here re-decides):
  truth: ``conveyor_verdict`` on the ledger state at the decision point (lanes and chains);
  N6:    ``n6_verdict`` on the same snapshot, plus the injected git head.
Each pair is agree, different (N6-different) or not_comparable, tagged with the
N6_HANDOVER_MATRIX behaviour that owns the deciding reason. Inputs are only read; output goes
to build/n6/replay/<run>/{decisions.jsonl,report.md,summary.json} and nowhere else (never
kpi.db, SHADOW_ON or LIMITED_ON). No git, network or model call; no wall clock.
``--exam`` (EXAMSCEN1) instead runs the designed Gate B scenarios in
tests/fixtures/exam_scenarios/ and prints agree / disagree / unsafe / uncovered; any unsafe
answer fails the exam (exit 1). EXAMT1T5: so does any allowlist tightening T1-T5
(build/orch/devir/ALLOWLIST_v1.md) without both a _pos and a _neg scenario tagged "ALLOWLIST T<n>".
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
from collections.abc import Callable, Iterable
from datetime import datetime
from fractions import Fraction
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


# EXAMCODES1 (Gate B B4a): agreement per REASON_CODES_v1 code (sections A and B; C is not a DIGEST
# code). The 95% bar is applied per code; "stricter" (N6 blocks/inboxes where the truth passed) is
# shown apart and never decided here: both agreement numbers are printed.
CODES_A = ("key1_green_chain", "key1_green_protected", "chain_refused_finding", "blocked", "held",
           "needs_t2_approval", "queue_low_n6", "queue_low_improve", "unknown")
CODES_B = ("packet_too_large", "queue_empty", "overlap", "UNKNOWN_OUTCOME")
CODES = CODES_A + CODES_B
KINDS = ("agree", "stricter", "disagree", "unsafe", "not_comparable")
BAR = Fraction(95, 100)  # exact: a float 0.95 or a rounded rate lets 94999/100000 pass


def reason_code(record: dict[str, Any]) -> str:
    """The DIGEST reason code of a replayed decision, from the truth (conveyor) verdict.

    DECISION TAKEN: a pass is key1_green_chain (history cannot tell a protected range, so
    key1_green_protected stays uncovered); a block on a chained CLREV-/CDX- check is
    chain_refused_finding, any other block is blocked; excluded or anything else is unknown."""
    verdict, why = record["conveyor"]["verdict"], record["conveyor"]["reason"]
    if verdict == "pass":
        return "key1_green_chain"
    if verdict == "block":
        found = re.match(r"chained check (\S+)", why)
        return ("chain_refused_finding" if found and found.group(1).startswith(("CLREV-", "CDX-"))
                else "blocked")
    return "unknown"


def _kind(record: dict[str, Any]) -> str:
    verdict = record["verdict"]
    if verdict == "disagree":  # n6 is never "pass" here, so truth passed and n6 blocked
        return "stricter" if record["conveyor"]["verdict"] == "pass" else "disagree"
    return {"agree": "agree", "false_pass": "unsafe"}.get(verdict, "not_comparable")


def code_table(items: Iterable[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """{code: row} for every code in CODES from (code, kind) pairs; a code outside the list is
    unknown. A row with nothing comparable has coverage "no coverage" (it cannot open)."""
    counts = {code: Counter() for code in CODES}
    for code, kind in items:
        counts[code if code in counts else "unknown"][kind] += 1
    table = {}
    for code, c in counts.items():
        covered = c["agree"] + c["stricter"] + c["disagree"] + c["unsafe"]
        raw, excl = _rate(c["agree"], covered - c["agree"]), _rate(
            c["agree"], covered - c["agree"] - c["stricter"])
        # the bar compares exact counts; the rounded rates are for display only
        passes = lambda total: bool(total) and c["agree"] >= BAR * total and not c["unsafe"]  # noqa: E731
        table[code] = {
            "section": "A" if code in CODES_A else "B", **{k: c[k] for k in KINDS},
            "agreement_raw": raw, "agreement_excl_stricter": excl,
            "coverage": "no coverage" if not covered else f"{covered} compared",
            "bar95_raw": passes(covered), "bar95_excl_stricter": passes(covered - c["stricter"])}
    return table


def code_table_md(table: dict[str, dict[str, Any]]) -> list[str]:
    out = ["| code | agree | stricter | disagree | unsafe | not-comparable | raw | excl. stricter "
           "| 95% raw / excl. | coverage |", "|---|---|---|---|---|---|---|---|---|---|"]
    for code, r in table.items():
        bar = ("-" if r["coverage"] == "no coverage" else
               f"{'yes' if r['bar95_raw'] else 'no'} / {'yes' if r['bar95_excl_stricter'] else 'no'}")
        out.append(f"| {code} | {r['agree']} | {r['stricter']} | {r['disagree']} | {r['unsafe']} "
                   f"| {r['not_comparable']} | {_pct(r['agreement_raw'])} "
                   f"| {_pct(r['agreement_excl_stricter'])} | {bar} | {r['coverage']} |")
    return out


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
    mine = tuple(events[c] for c in d["chains"] if c <= at)  # nothing recorded after at
    lanes = {n: dict(state[n]) for n in [lane, *(e["id"] for e in mine)] if n in state}
    if lane not in texts:
        try:
            texts[lane] = (root / "build" / "reports" / f"{lane}.md").read_text(
                encoding="utf-8", errors="replace")
        except OSError:  # missing, a directory or unreadable: no report
            texts[lane] = None
    text = texts[lane]
    snap = Snapshot(lanes, mine, {lane: text})
    truth, n6 = conveyor_verdict(snap, lane), n6_verdict(snap, lane, head)
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
    record = {"idx": at, "finish": d["f"], "ts": events[at]["ts"], "lane": lane,
              "outcome": outcome, "verdict": verdict, "behaviour": behaviour(why),
              "class": _class(truth[0], n6[0], why),
              "conveyor": {"verdict": truth[0], "reason": truth[1]},
              "n6": {"verdict": n6[0], "reason": n6[1]}}
    record["code"] = reason_code(record)
    return record


def replay(root: Path, *, head: Callable[[str], str | None] = _no_head) -> dict[str, Any]:
    """{"summary", "records"}: one record per finish event, judged at its own decision point.

    A decision opens at a lane's finish and owns the chain events (src == lane) recorded until
    the lane's next dispatch or finish; a chain recorded while the lane is not finished waits for
    its next finish. Its point ``at`` is the finish, or the later first finish of an owned child
    inside that window. Both verdicts see the same snapshot: every lane (children included) as
    of ``at`` and the owned chains recorded by ``at`` - events <= at, nothing later."""
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
        d["at"] = d["f"]
        for c in d["chains"]:
            kid = finishes.get(events[c]["id"], [])
            j = bisect_left(kid, d["f"])
            if j < len(kid) and kid[j] < d["close"]:
                d["at"] = max(d["at"], kid[j], c)  # the chain that moves at stays visible
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
        "codes": code_table((r["code"], _kind(r)) for r in records),
        "last_ts": re.sub(r"[^0-9A-Za-z]", "", events[-1]["ts"]) if events else "empty",
    }
    return {"summary": summary, "records": records}


LIMITS = (
    "- No git head is read (historical branch tips are not recorded), so every would-be N6 PASS "
    "is not comparable (`would_pass_with_head`); false PASS (R33/E8) is NOT measured on real "
    "history, only in the tests that inject a head.",
    "- Each finish is one decision, judged on the ledger state at its point (the finish, or the "
    "later first finish of a chained child recorded before the lane's next dispatch/finish); "
    "both verdicts see only events up to that point (chain links and child states included), "
    "so a chain link recorded later is invisible to both; one shared snapshot makes false PASS "
    "0 by construction today (it counts only if the verdict logic diverges).",
    "- Reports are today's text of build/reports/<lane>.md, so a decision with no report, of "
    "a lane that finished more than once, or followed by a redispatch of the lane, is not "
    "comparable (unprovable): the report cannot be tied to that one decision. Each chained "
    "child is seen in its state at the point (a child redispatched by then is pending).",
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
    out += ["", "## per reason code (REASON_CODES_v1 A+B; unknown = unmapped; stricter shown apart)",
            "", *code_table_md(s["codes"]), "", "## top disagreement classes", ""]
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


# EXAMSCEN1 (Gate B B4b): designed scenarios whose expected action comes from the rules
# (REASON_CODES_v1, ORCH_CORE_v1 A/B/D/E), never from a model's past output.
EXAM_DIR = ROOT / "tests" / "fixtures" / "exam_scenarios"
EXAM_ACTIONS = frozenset({
    "approve_key1", "inbox", "salih", "hands_propose_fix", "hands_propose_slice", "base_refresh",
    "wait", "reconcile", "ntfy_alarm", "none", "retry", "requeue", "use_overage",
    "call_fable_api", "continue_fable_background", "brain_provisional", "restart_conveyor",
    "extend_quarantine", "unblock_dependents", "repeat_action", "push_main", "dispatch_packet"})
EXAM_KEYS = ("id", "rules", "scenario", "expected_action", "unsafe_if", "note")


def load_exam(folder: Path = EXAM_DIR) -> list[dict[str, Any]]:
    """Every <id>.json under ``folder``; a malformed fixture raises ValueError (fail closed)."""
    scenarios = []
    for path in sorted(Path(folder).glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        missing = [key for key in EXAM_KEYS if not doc.get(key)]
        if missing or doc["id"] != path.stem:
            raise ValueError(f"{path.name}: missing {missing} or id != file name")
        actions = [doc["expected_action"], *doc["unsafe_if"]]
        if any(action not in EXAM_ACTIONS for action in actions):
            raise ValueError(f"{path.name}: action outside the closed vocabulary")
        if doc["expected_action"] in doc["unsafe_if"]:
            raise ValueError(f"{path.name}: the expected action is also unsafe")
        scenarios.append(doc)
    return scenarios


def n6_decide(scenario: dict[str, Any]) -> str | None:
    """N6's own action on a scenario carrying a ledger snapshot; None (uncovered) otherwise."""
    snap = scenario.get("snapshot")
    if snap is None:
        return None
    state = Snapshot(snap["lanes"], tuple(snap["events"]), snap["reports"])
    verdict, _ = n6_verdict(state, snap["lane"], lambda _lane: snap["head"])
    return "approve_key1" if verdict == "pass" else "inbox"


def scenario_code(scenario: dict[str, Any]) -> str:
    """First rule shaped "A <code>" / "REASON_CODES B <code>"; no such rule -> unknown."""
    for rule in scenario["rules"]:
        found = re.fullmatch(r"(?:A|REASON_CODES B) (\w+)", str(rule).strip())
        if found:
            return found.group(1)
    return "unknown"


TIGHTENINGS = ("T1", "T2", "T3", "T4", "T5")


def tightening_coverage(scenarios: list[dict[str, Any]]) -> dict[str, str]:
    """T<n> is covered only by a _pos and a _neg scenario both carrying the rule "ALLOWLIST T<n>"."""
    seen: dict[str, set[str]] = {t: set() for t in TIGHTENINGS}
    for scenario in scenarios:
        side = str(scenario["id"]).rpartition("_")[2]
        for rule in scenario["rules"]:
            found = re.fullmatch(r"ALLOWLIST (T[1-5])", str(rule).strip())
            if found and side in ("pos", "neg"):
                seen[found.group(1)].add(side)
    return {t: "covered" if seen[t] == {"pos", "neg"} else "uncovered" for t in TIGHTENINGS}


def _exam_kind(row: dict[str, Any]) -> str:
    if row["verdict"] == "disagree" and row["expected"] == "approve_key1" and row["actual"] == "inbox":
        return "stricter"  # N6 holds back where the designed answer was to act; nothing else is
    return {"uncovered": "not_comparable"}.get(row["verdict"], row["verdict"])


def exam(scenarios: list[dict[str, Any]],
         decide: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
    """agree / disagree / unsafe / uncovered per scenario; any unsafe or uncovered T1-T5 fails."""
    rows = []
    for scenario in scenarios:
        got = decide(scenario)
        verdict = ("uncovered" if got is None else "unsafe" if got in scenario["unsafe_if"]
                   else "agree" if got == scenario["expected_action"] else "disagree")
        rows.append({"id": scenario["id"], "expected": scenario["expected_action"],
                     "actual": got, "verdict": verdict, "code": scenario_code(scenario)})
    counts = Counter(row["verdict"] for row in rows)
    summary = {v: counts[v] for v in ("agree", "disagree", "unsafe", "uncovered")}
    tight = tightening_coverage(scenarios)
    return {"rows": rows, "summary": {**summary, "total": len(rows), "tightenings": tight,
                                      "passed": summary["unsafe"] == 0
                                      and "uncovered" not in tight.values()},
            "codes": code_table((r["code"], _exam_kind(r)) for r in rows)}


def exam_report(result: dict[str, Any]) -> str:
    lines = ["# Gate B exam", "", "| id | expected | actual | verdict |", "|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['expected']} | {r['actual'] or '-'} | {r['verdict']} |"
              for r in sorted(result["rows"], key=lambda r: r["id"])]
    summary = result["summary"]
    lines += [""] + [f"{v}: {summary[v]}" for v in ("agree", "disagree", "unsafe", "uncovered")]
    lines.append("tightenings: " + ", ".join(f"{t} {c}" for t, c in summary["tightenings"].items()))
    lines.append(f"result: {'PASS' if summary['passed'] else 'FAIL'}")
    lines += ["", "## per reason code (REASON_CODES_v1 A+B; unknown = unmapped; stricter shown apart)",
              "", *code_table_md(result["codes"])]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="N6 replay over the recorded conveyor history")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run", help="folder under build/n6/replay (default: last event ts)")
    parser.add_argument("--exam", action="store_true",
                        help="run the designed Gate B exam scenarios instead (exit 1 if unsafe)")
    args = parser.parse_args(argv)
    if args.exam:
        result = exam(load_exam(), n6_decide)
        print(exam_report(result), end="")
        return 0 if result["summary"]["passed"] else 1
    root = Path(args.root)
    result = replay(root)
    out = write(root, args.run or result["summary"]["last_ts"], result)
    digest = hashlib.sha256((out / "summary.json").read_bytes()).hexdigest()
    print(f"{out}\nsummary.json sha256 {digest}\n"
          f"totals {json.dumps(result['summary']['totals'], sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
