"""Read-only digest of conveyor state for the orchestrator.

Reads build/conveyor/{status,lanes,backlog}.json + events.jsonl, build/approvals/
and build/reports/ under --root; writes only build/orch/DIGEST.md and digest.json.
Held and ready come from the conveyor's status.json backlog view states; build/conveyor/hold/
is an archive of old hold notes and is not read.
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
CLIP = 900  # max chars per DIGEST.md line: 7 header lines + 1 truncation line stay under MAX_CHARS
LOW = 2  # a track with fewer ready slices than this raises queue_low_*
RUNNING = ("working", "waiting")
FINISHED = ("done", "stopped")
PREFIX = re.compile(r"^((INT|CLREV|CDX)-)+")
SUFFIX = re.compile(r"(-?AFX|-?R\d*|FIX\d*)$")


def root_id(name):
    name = str(name)
    return SUFFIX.sub("", PREFIX.sub("", name)) or name


def ts(s):
    """Naive local datetime or None; an aware value is converted to local time so all compare."""
    try:
        t = datetime.fromisoformat(str(s))
        return t.astimezone().replace(tzinfo=None) if t.tzinfo else t
    except (ValueError, TypeError, OverflowError, OSError):
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
    except (OSError, ValueError, RecursionError) as e:
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
        except (ValueError, RecursionError):
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
    backlog = bl.get("slices") if isinstance(bl, dict) else None
    backlog = backlog if isinstance(backlog, list) else []
    evs = events(c / "events.jsonl", unk)
    status = status if isinstance(status, dict) else {}
    lanes = lanes if isinstance(lanes, dict) else {}
    counts = {}
    for name in lanes:
        counts[root_id(name)] = counts.get(root_id(name), 0) + 1
    items = {}  # id -> (code, evidence, since); first writer wins = one reason per item

    def add(i, code, evidence, at):
        items.setdefault(i, (code, evidence, at))

    att = status.get("attention")
    for a in att if isinstance(att, list) else []:
        if isinstance(a, dict) and a.get("id") not in (None, ""):
            add(str(a["id"]), attention_code(str(a.get("reason", ""))), f"status.json attention: {a.get('reason', '')}", a.get("since"))
    for e in evs:
        t = ts(e.get("ts"))
        if e.get("event") == "chain_refused" and e.get("src") not in (None, "") and t and t >= since:
            add(str(e["src"]), "chain_refused_finding", f"events.jsonl chain_refused: {e.get('reason', '')}", e.get("ts"))
    # conveyor view states (slice_hold/pick_slice): "held: <why>", "needs_t2_approval",
    # "ready" (the conveyor dispatches it) and "n6" (only N6's claim starts it); the rest is not ready
    view = status.get("backlog") or []
    if not isinstance(view, list):
        unk.append(("input:status.backlog", "status.json: backlog view is not a list"))
        view = []
    title = {str(s.get("id")): str(s.get("title", "")) for s in backlog if isinstance(s, dict)}
    ready = {"n6": 0, "improve": 0, "other": 0}
    for b in view:
        if not isinstance(b, dict) or b.get("id") in (None, ""):
            continue
        i, st = str(b["id"]), str(b.get("state", ""))
        if st.startswith("held:"):
            add(i, "held", f"status.json backlog {st}", None)
        elif st == "needs_t2_approval":
            add(i, "needs_t2_approval", "status.json backlog state needs_t2_approval", None)
        elif st == "n6":
            ready["n6"] += 1
        elif st == "ready":
            t = title.get(i, "")
            ready["n6" if t.startswith("[N6]") else "improve" if t.startswith("[IYILESTIRME") else "other"] += 1
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
                 and (ts(v.get("updated_at")) or datetime.min) >= since)  # ts() is naive, like since
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
    return "\n".join(x if len(x) <= CLIP else x[:CLIP - 3] + "..." for x in L) + "\n"


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
    since = ts(a.since) if a.since else now - timedelta(hours=24)
    if since is None:
        ap.error("bad --since")
    d = build(a.root, now, since)
    out = Path(a.root) / "build" / "orch"
    out.mkdir(parents=True, exist_ok=True)
    (out / "DIGEST.md").write_bytes(fit(d).encode("utf-8", "replace"))  # replace: lone surrogates from JSON escapes
    (out / "digest.json").write_bytes(json.dumps(d, sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
