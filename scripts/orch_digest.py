"""Read-only digest of conveyor state for the orchestrator.

Reads build/conveyor/{status,lanes,backlog}.json + events.jsonl, build/approvals/
and build/reports/ under --root; writes only build/orch/DIGEST.md and digest.json.
No network, no model call, no git. Unreadable input -> an 'unknown' item, never a crash.
Same inputs + same --now -> byte-identical output.
"""
import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

REASONS = ("key1_green_protected", "key1_green_chain", "chain_refused_finding", "blocked", "held",
           "needs_t2_approval", "queue_low_n6", "queue_low_improve", "unknown")
MAX_CHARS = 8000
LOW = 2  # a track with fewer ready slices than this raises queue_low_*
RUNNING = ("working", "waiting")
FINISHED = ("done", "stopped")
PREFIX = re.compile(r"^((INT|CLREV|CDX)-)+")
SUFFIX = re.compile(r"(-?AFX|-?R\d*|FIX\d*)$")


def root_id(name):
    return SUFFIX.sub("", PREFIX.sub("", name)) or name


def ts(s):
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


def age(now, then):
    t = ts(then)
    if t is None:
        return "?"
    m = max(0, int((now - t).total_seconds() // 60))
    return f"{m // 60}h{m % 60:02d}m"


def load(path, unknown, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        unknown.append((f"input:{Path(path).name}", f"{Path(path).name}: {type(e).__name__}"))
        return default


def events(path, unknown):
    out, bad = [], []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        unknown.append(("input:events.jsonl", f"events.jsonl: {type(e).__name__}"))
        return out
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            if not isinstance(e, dict):
                raise ValueError
            out.append(e)
        except ValueError:
            bad.append(n)
    if bad:
        unknown.append(("input:events.jsonl", f"events.jsonl: {len(bad)} unreadable lines, first at line {bad[0]}"))
    return out


def attention_code(reason):
    r = reason.lower()
    if "chain refused" in r or "finding" in r:
        return "chain_refused_finding"
    if "touches" in r or "protected" in r or "manual key" in r:
        return "key1_green_protected"
    if "key 1" in r:
        return "key1_green_chain"
    if "blocked" in r:
        return "blocked"
    return "unknown"


def build(root, now, since):
    root = Path(root)
    c = root / "build" / "conveyor"
    unk = []
    status = load(c / "status.json", unk, {})
    lanes = load(c / "lanes.json", unk, {})
    bl = load(c / "backlog.json", unk, {})
    backlog = bl.get("slices", []) if isinstance(bl, dict) else []
    evs = events(c / "events.jsonl", unk)
    status = status if isinstance(status, dict) else {}
    lanes = lanes if isinstance(lanes, dict) else {}
    counts = {}
    for name in lanes:
        counts[root_id(name)] = counts.get(root_id(name), 0) + 1
    items = {}  # id -> (code, evidence, since); first writer wins = one reason per item

    def add(i, code, evidence, at):
        items.setdefault(i, (code, evidence, at))

    for a in status.get("attention") or []:
        if isinstance(a, dict) and a.get("id"):
            add(a["id"], attention_code(str(a.get("reason", ""))), f"status.json attention: {a.get('reason', '')}", a.get("since"))
    for e in evs:
        t = ts(e.get("ts"))
        if e.get("event") == "chain_refused" and e.get("src") and t and t >= since:
            add(e["src"], "chain_refused_finding", f"events.jsonl chain_refused: {e.get('reason', '')}", e.get("ts"))
    hold = c / "hold"
    for p in sorted(hold.iterdir()) if hold.is_dir() else []:
        if p.is_file() and ".json" not in p.name:
            add(p.name, "held", f"build/conveyor/hold/{p.name}", None)
    for b in status.get("backlog") or []:
        if isinstance(b, dict) and b.get("state") == "needs_t2_approval":
            add(b.get("id", "?"), "needs_t2_approval", "status.json backlog state needs_t2_approval", None)
    # ready counts per track: approved slices the conveyor has not started
    seen = {b.get("id") for b in status.get("backlog") or [] if isinstance(b, dict)}
    ready = {"n6": 0, "improve": 0, "other": 0}
    for s in backlog:
        if not isinstance(s, dict) or not s.get("approved") or s.get("id") in seen:
            continue
        title = str(s.get("title", ""))
        ready["n6" if s.get("n6") or title.startswith("[N6]") else "improve" if title.startswith("[IYILESTIRME") else "other"] += 1
    for track in ("n6", "improve"):
        if ready[track] < LOW:
            add(f"queue:{track}", f"queue_low_{track}", f"{ready[track]} ready < {LOW}", None)
    for i, why in unk:
        add(i, "unknown", why, None)
    out_items = []
    for i, (code, ev, at) in items.items():
        r = root_id(i)
        rep = root / "build" / "reports" / f"{r}.md"
        out_items.append({"id": i, "reason": code, "evidence": ev + (f" | build/reports/{r}.md" if rep.is_file() else ""),
                          "attempts": counts.get(r, 0), "waiting": age(now, at) if at else "-"})
    out_items.sort(key=lambda x: (REASONS.index(x["reason"]), x["id"]))
    running = sorted((n, v) for n, v in lanes.items() if isinstance(v, dict) and v.get("state") in RUNNING)
    fin = sorted((n, v) for n, v in lanes.items() if isinstance(v, dict) and v.get("state") in FINISHED
                 and (ts(v.get("updated_at")) or datetime.min) >= since)
    appr = root / "build" / "approvals"
    pend = sorted(p.name for p in appr.iterdir() if p.is_file()) if appr.is_dir() else []
    pace = status.get("pace") if isinstance(status.get("pace"), dict) else {}
    return {
        "generated_at": now.isoformat(), "since": since.isoformat(),
        "running": [{"id": n, "model": v.get("model", "?"), "age": age(now, v.get("started_at"))} for n, v in running],
        "finished": [{"id": n, "state": v.get("state"), "at": v.get("updated_at")} for n, v in fin],
        "items": out_items, "ready": ready, "pending_key1": pend,
        "quota": {"usage_pct": status.get("usage_pct"), "pace_line": pace.get("pace_line"), "band": pace.get("band")},
    }


def render(d, n_items):
    its = d["items"]
    L = [f"# ORCH DIGEST {d['generated_at']} (since {d['since']})",
         f"quota: usage {d['quota']['usage_pct']}% pace_line {d['quota']['pace_line']} | {d['quota']['band']}",
         f"ready: n6={d['ready']['n6']} improve={d['ready']['improve']} other={d['ready']['other']}",
         f"running ({len(d['running'])}): " + ", ".join(f"{r['id']}[{r['model']} {r['age']}]" for r in d["running"][:10]),
         f"finished since ({len(d['finished'])}): " + ", ".join(f"{f['id']}:{f['state']}" for f in d["finished"][:15]),
         f"pending key1 candidates ({len(d['pending_key1'])}; ff/patch-id: reader checks): " + ", ".join(d["pending_key1"][:10]),
         f"needs orchestrator ({len(its)}):"]
    L += [f"- {i['reason']} {i['id']} x{i['attempts']} wait {i['waiting']} :: {i['evidence'][:110]}" for i in its[:n_items]]
    if len(its) > n_items:
        L.append(f"... truncated {len(its) - n_items} more items (all in digest.json)")
    return "\n".join(L) + "\n"


def fit(d):
    n = len(d["items"])
    while n and len(render(d, n)) > MAX_CHARS:
        n -= 1
    return render(d, n)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--now", help="ISO time; default = wall clock")
    ap.add_argument("--since", help="ISO time; default = now - 24h")
    a = ap.parse_args(argv)
    now = ts(a.now) if a.now else datetime.now().replace(microsecond=0)
    if now is None:
        ap.error("bad --now")
    since = (ts(a.since) if a.since else None) or now - timedelta(hours=24)
    d = build(a.root, now, since)
    out = Path(a.root) / "build" / "orch"
    out.mkdir(parents=True, exist_ok=True)
    (out / "DIGEST.md").write_bytes(fit(d).encode("utf-8"))
    (out / "digest.json").write_bytes(json.dumps(d, sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
