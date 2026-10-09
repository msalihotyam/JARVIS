"""Token-usage ledger from Claude Code session transcripts. Stdlib only.

No network, no model calls. Reads
%USERPROFILE%/.claude/projects/<JARVIS dirs>/**/*.jsonl read-only and merges
into build/ledger/usage.json: a session counted once stays even after its
transcript is gone (the conveyor removes finished lane sessions). Only a
60-char title is kept from any text.

    python scripts/usage_ledger.py
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "ledger" / "usage.json"
PROJECTS = Path(os.environ.get("USERPROFILE") or Path.home()) / ".claude" / "projects"
# worktree dirs share this prefix: ...Masa-st--JARVIS, ...JARVIS-Claude-x, ...JARVIS--claude-worktrees-x
PREFIX = "c--users-realk-masa-st--jarvis"
FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
LANE_RE = re.compile(r"@jarvis\s+lane=([A-Za-z0-9_-]+)")
IST = timezone(timedelta(hours=3))  # Europe/Istanbul: fixed UTC+3 since 2016, no tzdata needed


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_files(files):
    """Merge transcript files (a session + its subagents share a sessionId)."""
    ses = {}
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                sid = d.get("sessionId")
                if not sid:
                    continue
                s = ses.setdefault(sid, {"msgs": {}, "models": set(), "lane": None,
                                         "title": None, "first": None, "ts": []})
                if d.get("timestamp"):
                    s["ts"].append(d["timestamp"])
                t, m = d.get("type"), d.get("message")
                if t == "custom-title" and d.get("customTitle"):
                    s["title"] = d["customTitle"]
                elif t == "user" and isinstance(m, dict):
                    txt = _text(m.get("content")).strip()
                    if not txt or txt.startswith("<"):
                        continue
                    lm = LANE_RE.search(txt[:300])
                    if lm and not s["lane"]:
                        s["lane"] = lm.group(1)
                    if s["first"] is None:
                        s["first"] = txt.splitlines()[0]
                elif t == "assistant" and isinstance(m, dict):
                    if m.get("model") and not m["model"].startswith("<"):
                        s["models"].add(m["model"])
                    u = m.get("usage")
                    if not isinstance(u, dict):
                        continue
                    row = {k: u.get(k) or 0 for k in FIELDS}
                    mid = m.get("id") or d.get("uuid")
                    old = s["msgs"].get(mid)
                    # repeated entries of one message: keep the fullest (largest output)
                    if old is None or row["output_tokens"] >= old["output_tokens"]:
                        s["msgs"][mid] = row
    return ses


def sessions_of(ses):
    sessions = []
    for sid, s in ses.items():
        if not s["ts"]:
            continue
        tot = {k: sum(r[k] for r in s["msgs"].values()) for k in FIELDS}
        tot["total_tokens"] = sum(tot.values())
        title = s["lane"] or s["title"] or s["first"] or sid[:8]
        sessions.append({"session_id": sid, "lane": title[:60],
                         "start": min(s["ts"]), "end": max(s["ts"]),
                         "models": sorted(s["models"]), "messages": len(s["msgs"]), **tot})
    return sessions


def build(sessions):
    sessions = sorted(sessions, key=lambda x: x["start"])
    by_lane, by_day = {}, {}
    for x in sessions:
        day = _ts(x["start"]).astimezone(IST).date().isoformat()
        for agg, key in ((by_lane, x["lane"]), (by_day, day)):
            a = agg.setdefault(key, {"sessions": 0, **dict.fromkeys(FIELDS + ("total_tokens",), 0)})
            a["sessions"] += 1
            for k in FIELDS + ("total_tokens",):
                a[k] += x[k]
    return {"generated_at": datetime.now(IST).isoformat(timespec="seconds"),
            "sessions": sessions, "by_lane": by_lane, "by_day": dict(sorted(by_day.items()))}


def merge(known, fresh):
    """Known sessions by id, updated from fresh transcripts; a known session is never
    dropped, and a smaller fresh count (a partly gone transcript) never replaces it."""
    out = {s["session_id"]: s for s in known}
    for s in fresh:
        old = out.get(s["session_id"])
        if old is None or s["total_tokens"] >= old.get("total_tokens", 0):
            out[s["session_id"]] = s
    return list(out.values())


def load_known(out):
    """Sessions already in usage.json; an unreadable file raises so it is never overwritten."""
    try:
        text = Path(out).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    data = json.loads(text)  # ValueError: keep the file, write nothing
    sessions = data["sessions"]
    if not isinstance(sessions, list) or not all(isinstance(s, dict) and "session_id" in s for s in sessions):
        raise ValueError(f"{out}: unexpected sessions")
    return sessions


def main(projects=PROJECTS, out=OUT):
    known = load_known(out)
    dirs = [p for p in Path(projects).iterdir() if p.is_dir() and p.name.lower().startswith(PREFIX)]
    files = [f for p in dirs for f in p.rglob("*.jsonl")]
    data = build(merge(known, sessions_of(parse_files(files))))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
    return data


if __name__ == "__main__":
    r = main()
    print(f"sessions={len(r['sessions'])} -> {OUT}")
    sys.exit(0)
