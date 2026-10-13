"""V2.1b offline retrieval eval for the embeddings decision (KARAR-2).

Loads the fixture into a fresh SQLite store in a temp directory (never
data\\jarvis.db), runs every query through ``SQLiteMemoryStore.search_records``
with the query's project scope and prints recall@5, MRR and the cross-project
leak count per mode:

- fts / like: the store as shipped (all tokens ANDed), FTS5 or LIKE fallback;
- fts_or / like_or: one search per query token, merged by matched-token count
  then summed rank - shows how much of the gap a lexical OR fixes before
  embeddings.

No network, no embeddings, stdlib only.  Usage:
    python scripts/memory_eval.py [--fixture PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis import memory  # noqa: E402
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
K = 5


def _load_store(store: SQLiteMemoryStore, fixture: dict) -> tuple[dict, dict]:
    projects = {label: store.create_project(label).id for label in fixture["projects"]}
    labels = {}
    for item in fixture["records"]:
        record = MemoryRecord(
            kind=MemoryRecordKind[item["kind"].upper()],
            text=item["text"],
            subject_key=item["subject"],
            scope=MemoryRecordScope(projects[item["project"]]),
            origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
            actor=MemoryRecordActor.MODEL,
            confidence=0.8,
            classification=DataClassification.INTERNAL,
        )
        labels[store.insert_record(record).record.record_id] = item["id"]
    return projects, labels


def _search(store, text, scope, strategy):
    """Return [(record, rank)] best first, at most K."""
    if strategy == "and":
        return [(view.record, rank) for view, rank in store.search_records(text, scope, limit=K)]
    # ponytail: OR merge in Python over the public API; a real OR mode belongs in memory.py if adopted.
    hits: dict[str, list] = {}
    for token in dict.fromkeys(re.findall(r"[^\W_]+", text.casefold())):
        for view, rank in store.search_records(token, scope, limit=50):
            entry = hits.setdefault(view.record.record_id, [view.record, 0, 0.0])
            entry[1] += 1
            entry[2] += rank
    ordered = sorted(hits.values(), key=lambda e: (-e[1], e[2], e[0].record_id))
    return [(record, matched) for record, matched, _ in ordered[:K]]


def _run_mode(store, fixture, projects, labels, strategy) -> dict:
    per_query = []
    leaks = 0
    for query in fixture["queries"]:
        project_id = projects[query["project"]]
        results = _search(store, query["text"], MemoryRecordScope(project_id), strategy)
        leaked = [r for r, _ in results if r.scope.project_id != project_id]
        leaks += len(leaked)
        got = [labels.get(r.record_id, "?") for r, _ in results]
        expected = set(query["expected"])
        recall = len(expected & set(got)) / len(expected)
        rr = next((1 / (i + 1) for i, label in enumerate(got) if label in expected), 0.0)
        per_query.append({
            "id": query["id"], "type": query["type"], "lang": query["lang"],
            "got": got, "recall": recall, "rr": rr, "leaks": len(leaked),
        })
    n = len(per_query)
    by_type: dict[str, list] = {}
    for q in per_query:
        by_type.setdefault(q["type"], []).append(q["recall"])
    return {
        "recall_at_5": round(sum(q["recall"] for q in per_query) / n, 4),
        "mrr": round(sum(q["rr"] for q in per_query) / n, 4),
        "leaks": leaks,
        "zero_hit_queries": [q["id"] for q in per_query if not q["got"]],
        "recall_by_type": {t: round(sum(v) / len(v), 4) for t, v in sorted(by_type.items())},
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--json", action="store_true", help="print full JSON result")
    args = parser.parse_args(argv)
    result = run_eval(args.fixture)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"records={result['records']} queries={result['queries']} fts5={result['fts5_available']}")
    for name, mode in result["modes"].items():
        print(f"{name:8} recall@5={mode['recall_at_5']:.3f} mrr={mode['mrr']:.3f} "
              f"leaks={mode['leaks']} zero_hit={len(mode['zero_hit_queries'])}")
        print("         by_type: " + " ".join(f"{t}={v:.2f}" for t, v in mode["recall_by_type"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
