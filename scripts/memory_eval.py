"""V2.1b offline retrieval eval for the embeddings decision (KARAR-2).

Loads the fixture into a fresh SQLite store in a temp directory (never
data\\jarvis.db), runs every query through ``SQLiteMemoryStore.search_records``
with the query's project scope and prints recall@5, recall@10, MRR and the
cross-project leak count per mode (an update query's hit counts only above
its ``stale`` records):

- fts / like: the store as shipped (all tokens ANDed), FTS5 or LIKE fallback;
- fts_or / like_or: one search per query token, merged by matched-token count
  then summed rank - shows how much of the gap a lexical OR fixes before
  embeddings.

--extract scores recorded extraction model outputs (gold set) offline through
memory_capture.parse_candidates: precision/recall per kind, false_fact and
instruction_leak counts.  embeddings_gate() encodes the Q3 adoption rule;
--gate BASELINE CANDIDATE feeds it two retrieval modes via gate_input().

No network, no embeddings, stdlib only.  Usage:
    python scripts/memory_eval.py [--fixture PATH] [--json]
    python scripts/memory_eval.py --gate BASELINE_MODE CANDIDATE_MODE
    python scripts/memory_eval.py --extract [--gold PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis import memory, memory_capture  # noqa: E402
from jarvis.contracts import (  # noqa: E402
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
)
from jarvis.memory import SQLiteMemoryStore  # noqa: E402

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "memory_eval_v21.json"
DEFAULT_GOLD = ROOT / "tests" / "fixtures" / "memory_extract_gold_v21.json"
K = 5
K10 = 10
# Fixed insertion clock: fixture order is age order (update pairs list the old
# record first) and created_at ties cannot fall back to random record ids.
_CLOCK = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Q3 (build/v21/SEC_PLAN.md): Recall@10 deltas in percentage points.
GATE_OVERALL_GAIN = 5.0
GATE_SUBSET_GAIN = 10.0
GATE_MAX_LOSS = 2.0


def _load_store(store: SQLiteMemoryStore, fixture: dict) -> tuple[dict, dict]:
    projects = {label: store.create_project(label).id for label in fixture["projects"]}
    labels = {}
    for index, item in enumerate(fixture["records"]):
        record = MemoryRecord(
            kind=MemoryRecordKind[item["kind"].upper()],
            text=item["text"],
            subject_key=item["subject"],
            scope=MemoryRecordScope(projects[item["project"]]),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.MODEL,
            confidence=0.8,
            classification=DataClassification.INTERNAL,
            created_at=(_CLOCK + timedelta(seconds=index)).isoformat(),
        )
        labels[store.insert_record(record).record.record_id] = item["id"]
    return projects, labels


def _search(store, text, scope, strategy, labels):
    """Return [(record, rank)] best first, at most K10; ties break by fixture label."""
    if strategy == "and":
        return [(view.record, rank) for view, rank in store.search_records(text, scope, limit=K10)]
    # ponytail: OR merge in Python over the public API; a real OR mode belongs in memory.py if adopted.
    hits: dict[str, list] = {}
    for token in dict.fromkeys(re.findall(r"[^\W_]+", text.casefold())):
        for view, rank in store.search_records(token, scope, limit=50):
            entry = hits.setdefault(view.record.record_id, [view.record, 0, 0.0])
            entry[1] += 1
            entry[2] += rank
    ordered = sorted(hits.values(), key=lambda e: (-e[1], e[2], labels.get(e[0].record_id, "?")))
    return [(record, matched) for record, matched, _ in ordered[:K10]]


def _run_mode(store, fixture, projects, labels, strategy) -> dict:
    per_query = []
    leaks = 0
    for query in fixture["queries"]:
        project_id = projects[query["project"]]
        results = _search(store, query["text"], MemoryRecordScope(project_id), strategy, labels)
        leaked = [r for r, _ in results if r.scope.project_id != project_id]
        leaks += len(leaked)
        got = [labels.get(r.record_id, "?") for r, _ in results]
        stale = set(query.get("stale", ()))
        # Only hits ranked above the first stale (superseded) record count.
        ranked = got[:next((i for i, label in enumerate(got) if label in stale), len(got))]
        expected = set(query["expected"])
        recall = len(expected & set(ranked[:K])) / len(expected)
        rr = next((1 / (i + 1) for i, label in enumerate(ranked[:K]) if label in expected), 0.0)
        per_query.append({
            "id": query["id"], "type": query["type"], "lang": query["lang"],
            "got": got, "recall": recall,
            "recall_at_10": len(expected & set(ranked)) / len(expected),
            "rr": rr, "leaks": len(leaked),
        })
    n = len(per_query)

    def by_type(key):
        groups: dict[str, list] = {}
        for q in per_query:
            groups.setdefault(q["type"], []).append(q[key])
        return {t: round(sum(v) / len(v), 4) for t, v in sorted(groups.items())}

    return {
        "recall_at_5": round(sum(q["recall"] for q in per_query) / n, 4),
        "recall_at_10": round(sum(q["recall_at_10"] for q in per_query) / n, 4),
        "mrr": round(sum(q["rr"] for q in per_query) / n, 4),
        "leaks": leaks,
        "zero_hit_queries": [q["id"] for q in per_query if not q["got"]],
        "recall_by_type": by_type("recall"),
        "recall_by_type_at_10": by_type("recall_at_10"),
        "queries": per_query,
    }


def run_eval(fixture_path: Path = DEFAULT_FIXTURE) -> dict:
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    result = {
        "records": len(fixture["records"]),
        "queries": len(fixture["queries"]),
        "fts5_available": memory.MEMORY_RECORDS_FTS5_AVAILABLE,
        "modes": {},
    }
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        store = SQLiteMemoryStore(Path(temp) / "memory_eval.db")
        projects, labels = _load_store(store, fixture)
        for strategy in ("and", "or"):
            suffix = "" if strategy == "and" else "_or"
            if memory.MEMORY_RECORDS_FTS5_AVAILABLE:
                result["modes"]["fts" + suffix] = _run_mode(
                    store, fixture, projects, labels, strategy)
            with mock.patch.object(memory, "memory_records_fts_available", return_value=False):
                result["modes"]["like" + suffix] = _run_mode(
                    store, fixture, projects, labels, strategy)
    return result


def run_extract(gold_path: Path = DEFAULT_GOLD) -> dict:
    """Score each case's recorded model output against its gold expectations.

    A gold item matches one unused emitted candidate of the same kind whose
    casefolded text contains ``text_contains``. false_fact counts FACT
    candidates in cases whose gold has no FACT; instruction_leak counts
    candidates containing one of the case's ``instruction_markers``."""
    gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    kinds = [kind.value for kind in MemoryRecordKind]
    tally = {kind: {"tp": 0, "emitted": 0, "expected": 0} for kind in kinds}
    totals = {"false_fact": 0, "instruction_leak": 0, "refused": 0, "dropped": 0, "errors": 0}
    cases = []
    for case in gold["cases"]:
        output = case["recorded_model_output"]
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        turns = [SimpleNamespace(id=turn["id"]) for turn in case["transcript"]]
        parsed = memory_capture.parse_candidates(output, turns)
        if isinstance(parsed, memory_capture.CaptureError):
            totals["errors"] += 1
            emitted = []
        else:
            totals["refused"] += parsed.refused
            totals["dropped"] += len(parsed.dropped)
            emitted = [(c.kind.value, c.text.casefold()) for c in parsed.candidates]
        unused = list(emitted)
        for item in case["expected"]:
            tally[item["kind"]]["expected"] += 1
            needle = item["text_contains"].casefold()
            hit = next((e for e in unused if e[0] == item["kind"] and needle in e[1]), None)
            if hit is not None:
                unused.remove(hit)
                tally[item["kind"]]["tp"] += 1
        for kind, _ in emitted:
            tally[kind]["emitted"] += 1
        gold_kinds = {item["kind"] for item in case["expected"]}
        false_fact = 0 if "fact" in gold_kinds else sum(k == "fact" for k, _ in emitted)
        markers = [m.casefold() for m in case.get("instruction_markers", [])]
        leaks = sum(any(m in text for m in markers) for _, text in emitted)
        totals["false_fact"] += false_fact
        totals["instruction_leak"] += leaks
        cases.append({"id": case["id"], "lang": case["lang"], "emitted": len(emitted),
                      "missed": len(case["expected"]) - (len(emitted) - len(unused)),
                      "false_fact": false_fact, "instruction_leak": leaks})
    by_kind = {
        kind: {
            "precision": round(t["tp"] / t["emitted"], 4) if t["emitted"] else None,
            "recall": round(t["tp"] / t["expected"], 4) if t["expected"] else None,
            **t,
        }
        for kind, t in tally.items()
    }
    return {"cases": len(cases), "by_kind": by_kind, **totals, "per_case": cases}


def embeddings_gate(baseline: dict, candidate: dict) -> tuple[str, list[str]]:
    """Q3: ADOPT iff (overall Recall@10 >= +5 points OR paraphrase or tr_en
    >= +10) AND exact and update each lose <= 2 points; else REJECT.

    Both dicts: {"recall_at_10": float, "recall_by_type": {type: float}} with
    recall as a 0..1 fraction. A missing exact/update value fails closed."""

    def delta(get):
        try:
            # Rounded so float noise cannot flip a threshold edge (0.48 - 0.50).
            return round((float(get(candidate)) - float(get(baseline))) * 100, 6)
        except (KeyError, TypeError, ValueError):
            return None

    reasons = []
    overall = delta(lambda d: d["recall_at_10"])
    gain = overall is not None and overall >= GATE_OVERALL_GAIN
    reasons.append(f"overall {overall:+.1f}" if overall is not None else "overall missing")
    for name in ("paraphrase", "tr_en"):
        d = delta(lambda m, n=name: m["recall_by_type"][n])
        reasons.append(f"{name} {d:+.1f}" if d is not None else f"{name} missing")
        gain = gain or (d is not None and d >= GATE_SUBSET_GAIN)
    safe = True
    for name in ("exact", "update"):
        d = delta(lambda m, n=name: m["recall_by_type"][n])
        if d is None or d < -GATE_MAX_LOSS:
            safe = False
            reasons.append(f"{name} missing" if d is None else f"{name} loses {-d:.1f} > {GATE_MAX_LOSS}")
        else:
            reasons.append(f"{name} {d:+.1f}")
    if not gain:
        reasons.append(f"no gain: overall < +{GATE_OVERALL_GAIN} and paraphrase/tr_en < +{GATE_SUBSET_GAIN}")
    return ("ADOPT" if gain and safe else "REJECT"), reasons


def gate_input(mode_result: dict) -> dict:
    """One run_eval mode as embeddings_gate input (Recall@10, 0..1).

    Missing values stay missing so the gate fails closed (REJECT)."""
    return {
        "recall_at_10": mode_result.get("recall_at_10"),
        "recall_by_type": dict(mode_result.get("recall_by_type_at_10") or {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--extract", action="store_true", help="score the extraction gold set")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--json", action="store_true", help="print full JSON result")
    parser.add_argument("--gate", nargs=2, metavar=("BASELINE_MODE", "CANDIDATE_MODE"),
                        help="run embeddings_gate on two retrieval modes")
    args = parser.parse_args(argv)
    if args.extract:
        result = run_extract(args.gold)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"cases={result['cases']} false_fact={result['false_fact']} "
              f"instruction_leak={result['instruction_leak']} refused={result['refused']} "
              f"dropped={result['dropped']} errors={result['errors']}")
        for kind, row in result["by_kind"].items():
            p = "-" if row["precision"] is None else f"{row['precision']:.3f}"
            r = "-" if row["recall"] is None else f"{row['recall']:.3f}"
            print(f"{kind:10} precision={p} recall={r} tp={row['tp']} "
                  f"emitted={row['emitted']} expected={row['expected']}")
        return 0
    result = run_eval(args.fixture)
    if args.gate:
        missing = [m for m in args.gate if m not in result["modes"]]
        if missing:
            print(f"unknown mode(s): {', '.join(missing)}; have: {', '.join(result['modes'])}")
            return 2
        baseline, candidate = (gate_input(result["modes"][m]) for m in args.gate)
        verdict, reasons = embeddings_gate(baseline, candidate)
        print(f"gate {args.gate[0]} -> {args.gate[1]}: {verdict}")
        for reason in reasons:
            print(f"  {reason}")
        return 0
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"records={result['records']} queries={result['queries']} fts5={result['fts5_available']}")
    for name, mode in result["modes"].items():
        print(f"{name:8} recall@5={mode['recall_at_5']:.3f} recall@10={mode['recall_at_10']:.3f} "
              f"mrr={mode['mrr']:.3f} "
              f"leaks={mode['leaks']} zero_hit={len(mode['zero_hit_queries'])}")
        print("         by_type: " + " ".join(f"{t}={v:.2f}" for t, v in mode["recall_by_type"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
