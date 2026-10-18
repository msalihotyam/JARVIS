"""Token-usage ledger from Claude Code session transcripts. Stdlib only.

No network, no model calls. Reads
%USERPROFILE%/.claude/projects/<JARVIS dirs>/**/*.jsonl read-only and merges
into build/ledger/usage.json: a session counted once stays even after its
transcript is gone (the conveyor removes finished lane sessions). Only a
60-char title is kept from any text.

Incremental: usage_state.json next to usage.json keeps, per transcript, its
size/mtime/inode, the byte offset parsed up to (the last complete line) and the
parse result so far, so a run reads only the new bytes of a grown transcript; a
shrunk or replaced one is parsed whole, a vanished one is skipped. The output
equals a full parse of the same files.

    python scripts/usage_ledger.py [out]
"""
import json
import os
import re
import sys
import time
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
MARK = 64  # bytes before the saved offset that must be unchanged to resume there
CHECKPOINT_S = 30  # state is saved this often mid-run, so a killed run keeps its progress


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _new():
    return {"msgs": {}, "models": set(), "lane": None, "title": None, "first": None,
            "ts0": None, "ts1": None}


def _span(s, t0, t1):
    if t0 is not None:
        s["ts0"] = t0 if s["ts0"] is None else min(s["ts0"], t0)
        s["ts1"] = t1 if s["ts1"] is None else max(s["ts1"], t1)


def _keep(msgs, mid, row):
    old = msgs.get(mid)
    # repeated entries of one message: keep the fullest (largest output)
    if old is None or row["output_tokens"] >= old["output_tokens"]:
        msgs[mid] = row


def _feed(ses, raw):
    """One transcript line (bytes, no terminator) into ses."""
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return
    if not isinstance(d, dict):
        return
    sid = d.get("sessionId")
    if not sid:
        return
    s = ses.setdefault(sid, _new())
    if d.get("timestamp"):
        _span(s, d["timestamp"], d["timestamp"])
    t, m = d.get("type"), d.get("message")
    if t == "custom-title" and d.get("customTitle"):
        c = d["customTitle"]
        s["title"] = c[:60] if isinstance(c, str) else c
    elif t == "user" and isinstance(m, dict):
        txt = _text(m.get("content")).strip()
        if not txt or txt.startswith("<"):
            return
        lm = LANE_RE.search(txt[:300])
        if lm and not s["lane"]:
            s["lane"] = lm.group(1)
        if s["first"] is None:
            s["first"] = txt.splitlines()[0][:60]
    elif t == "assistant" and isinstance(m, dict):
        if m.get("model") and not m["model"].startswith("<"):
            s["models"].add(m["model"])
        u = m.get("usage")
        if isinstance(u, dict):
            _keep(s["msgs"], m.get("id") or d.get("uuid"), {k: u.get(k) or 0 for k in FIELDS})


def _combine(parts):
    """Per-file results in file order -> the same sessions one parse of all lines gives
    (a session + its subagents share a sessionId)."""
    ses = {}
    for part in parts:
        for sid, p in part.items():
            s = ses.setdefault(sid, _new())
            for mid, row in p["msgs"].items():
                _keep(s["msgs"], mid, row)
            s["models"] |= p["models"]
            s["lane"] = s["lane"] or p["lane"]
            if p["title"]:
                s["title"] = p["title"]
            if s["first"] is None:
                s["first"] = p["first"]
            _span(s, p["ts0"], p["ts1"])
    return ses


def _dump(ses):
    # lists, not dicts: a message id may be null or a number
    return [[sid, {**s, "msgs": [[mid, *(r[k] for k in FIELDS)] for mid, r in s["msgs"].items()],
                   "models": sorted(s["models"])}] for sid, s in ses.items()]


def _load(data):
    ses = {sid: {"msgs": {r[0]: dict(zip(FIELDS, r[1:], strict=True)) for r in s["msgs"]},
                 "models": set(s["models"]),
                 **{k: s[k] for k in ("lane", "title", "first", "ts0", "ts1")}}
           for sid, s in data}
    for s in ses.values():  # values no parse writes would fail later, in every run: refuse them here
        if not (all(s[k] is None or isinstance(s[k], str) for k in ("lane", "title", "first", "ts0", "ts1"))
                and (s["ts0"] is None) == (s["ts1"] is None)
                and all(isinstance(m, str) for m in s["models"])
                and all(isinstance(n, (int, float)) for r in s["msgs"].values() for n in r.values())):
            raise ValueError("unexpected state")
        if s["ts0"] is not None:
            _ts(s["ts0"]), _ts(s["ts1"])
    return ses


def _scan(path, prev):
    """Parse one transcript, resuming at prev's offset when the bytes before it are unchanged.
    Returns (state entry, sessions up to the last complete line, sessions of the rest)."""
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        ses, off = {}, 0
        try:
            if (prev["ino"] == st.st_ino and prev["size"] <= st.st_size
                    and (prev["size"] < st.st_size or prev["mtime_ns"] == st.st_mtime_ns)
                    and 0 <= prev["offset"] <= prev["size"]):
                fh.seek(max(0, prev["offset"] - MARK))
                if fh.read(min(MARK, prev["offset"])).hex() == prev["mark"]:
                    ses, off = _load(prev["ses"]), prev["offset"]
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):  # none or not ours
            ses, off = {}, 0
        fh.seek(off)
        data = fh.read(max(0, st.st_size - off))
        # resume only after a "\n": a line still being written is re-read whole next time
        cut = data.rfind(b"\n") + 1
        fh.seek(max(0, off + cut - MARK))
        mark = fh.read(min(MARK, off + cut)).hex()
    for line in data[:cut].splitlines():  # \n, \r, \r\n: the same lines as text-mode reading
        _feed(ses, line)
    tail = {}
    for line in data[cut:].splitlines():
        _feed(tail, line)
    entry = {"ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
             "offset": off + cut, "mark": mark, "ses": _dump(ses)}
    return entry, ses, tail


def sessions_of(ses):
    sessions = []
    for sid, s in ses.items():
        if s["ts0"] is None:
            continue
        tot = {k: sum(r[k] for r in s["msgs"].values()) for k in FIELDS}
        tot["total_tokens"] = sum(tot.values())
        title = s["lane"] or s["title"] or s["first"] or sid[:8]
        sessions.append({"session_id": sid, "lane": title[:60],
                         "start": s["ts0"], "end": s["ts1"],
                         "models": sorted(s["models"]), "messages": len(s["msgs"]), **tot})
    return sessions


def build(sessions, now=lambda: datetime.now(IST)):
    sessions = sorted(sessions, key=lambda x: x["start"])
    by_lane, by_day = {}, {}
    for x in sessions:
        day = _ts(x["start"]).astimezone(IST).date().isoformat()
        for agg, key in ((by_lane, x["lane"]), (by_day, day)):
            a = agg.setdefault(key, {"sessions": 0, **dict.fromkeys(FIELDS + ("total_tokens",), 0)})
            a["sessions"] += 1
            for k in FIELDS + ("total_tokens",):
                a[k] += x[k]
    return {"generated_at": now().isoformat(timespec="seconds"),
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


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # two writers never share a tmp
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(projects=PROJECTS, out=OUT, now=lambda: datetime.now(IST), clock=time.monotonic):
    out = Path(out)
    stamp = now()  # generated_at is the scan start: a long run never looks fresher than its data
    known = load_known(out)
    state_path = out.with_name(out.stem + "_state.json")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    dirs = [p for p in Path(projects).iterdir() if p.is_dir() and p.name.lower().startswith(PREFIX)]
    files = []
    for p in dirs:
        try:
            files += list(p.rglob("*.jsonl"))
        except OSError:  # removed while listing
            continue
    new, parts, last = {}, [], clock()
    for f in files:
        prev = state.get(str(f))
        try:
            try:
                entry, ses, tail = _scan(f, prev)
            except OSError:
                raise
            except Exception:  # a saved state no parse writes: parse whole, never fail every run
                if prev is None:
                    raise
                entry, ses, tail = _scan(f, None)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):  # vanished: parsed whole if back
            continue  # any other OSError (locked, denied) fails the run: a stale count must not look fresh
        new[str(f)] = entry
        parts += (ses, tail)
        if clock() - last >= CHECKPOINT_S:
            last = clock()
            _write(state_path, json.dumps({**state, **new}))
    data = build(merge(known, sessions_of(_combine(parts))), lambda: stamp)
    _write(out, json.dumps(data, indent=1, ensure_ascii=False))
    _write(state_path, json.dumps(new))  # a cache of whole-file results: order vs usage.json is free
    return data


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    r = main(out=out)
    print(f"sessions={len(r['sessions'])} -> {out}")
    sys.exit(0)
