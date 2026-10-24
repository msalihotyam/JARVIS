"""SPEEDBASE: a latency baseline of today's text chat path.

Drives the real Core text path headlessly - ``build_default_app``, the app's own
composition root, the Claude CLI on the subscription route with every billing
check on - against an isolated TEMP data directory, never ``data/jarvis.db``.
Each prompt is one series in one conversation, so turns 2+ show the cache.
Stops at the first turn that is not a plain conversation reply (auth, billing,
limit or any other refusal) and writes what it has.

    python scripts/speed_baseline.py            # the authorised 20 + 10 + 3 turns
    python scripts/speed_baseline.py --turns 1  # one turn per prompt

Writes build/speed/baseline-<ts>.json and baseline-<ts>.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROMPTS = (
    ("selam", 20),
    ("bana 3 satırlık bir tablo yap: ad, yaş, şehir", 10),
    ("kısa bir Python fonksiyonu yaz: iki sayıyı topla", 3),
)
METRICS = (("spawn", "t_proc_ready"), ("first token", "t_first_token"), ("total", "t_turn_done"))


def refuse_live(data_dir: Path) -> None:
    """Exit unless ``data_dir`` is outside every checkout's live ``data/``."""
    from jarvis.settings import PROJECT_ROOT, Settings

    target = Path(data_dir).resolve()
    live = {(ROOT / "data").resolve(), (PROJECT_ROOT / "data").resolve(),
            Settings().memory_database.resolve().parent}
    if any(target == d or d in target.parents for d in live):
        raise SystemExit(f"refused: {target} is the live data directory")


def pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def cache_ratio(turns: list[dict]) -> float | None:
    read = sum(t.get("cache_read", 0) for t in turns)
    whole = read + sum(t.get("tokens_in", 0) + t.get("cache_creation", 0) for t in turns)
    return round(read / whole, 3) if whole else None


def run_series(app, prompt: str, count: int) -> tuple[list[dict], str | None]:
    conversation: list[dict[str, str]] = []
    turns = []
    for _ in range(count):
        intent = app.submit_text(prompt, request_id=f"speedbase-{uuid.uuid4().hex[:12]}",
                                 response_language="tr-TR", conversation=list(conversation)).payload["intent"]
        status = intent.get("status")
        if status != "conversation":
            return turns, f"{status}: {intent.get('message') or intent.get('error')}"
        turns.append(dict((intent.get("usage") or {}).get("timing") or {}))
        conversation += [{"role": "user", "text": prompt}, {"role": "assistant", "text": intent.get("reply", "")}]
    return turns, None


def table(series: list[dict]) -> str:
    lines = ["| prompt | n | spawn p50/p95 ms | first token p50/p95 ms | total p50/p95 ms | cache t1 | cache t2+ |",
             "|---|---|---|---|---|---|---|"]
    for s in series:
        cells = [f"{pct(v, .5)}/{pct(v, .95)}" for v in
                 ([t[key] for t in s["turns"] if key in t] for _, key in METRICS)]
        lines.append(f"| {s['prompt']} | {len(s['turns'])} | " + " | ".join(cells)
                     + f" | {cache_ratio(s['turns'][:1])} | {cache_ratio(s['turns'][1:])} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", type=int, default=0, help="turns per prompt (default: the authorised counts)")
    args = parser.parse_args(argv)

    from jarvis.app import build_default_app
    from jarvis.settings import Settings

    data_dir = Path(tempfile.mkdtemp(prefix="jarvis-speedbase-"))
    refuse_live(data_dir)
    app = build_default_app(Settings(memory_database=data_dir / "jarvis.db"))
    series, anomaly = [], None
    for prompt, count in PROMPTS:
        turns, anomaly = run_series(app, prompt, args.turns or count)
        series.append({"prompt": prompt, "turns": turns})
        print(f"{prompt!r}: {len(turns)} turns", flush=True)
        if anomaly:
            print(f"stopped: {anomaly}", file=sys.stderr)
            break
    out = ROOT / "build" / "speed"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    record = {"stamp": stamp, "data_dir": str(data_dir), "anomaly": anomaly, "series": series}
    (out / f"baseline-{stamp}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"baseline-{stamp}.md").write_text(table(series), encoding="utf-8")
    print(table(series))
    print(out / f"baseline-{stamp}.json")
    return 1 if anomaly else 0


if __name__ == "__main__":
    raise SystemExit(main())
