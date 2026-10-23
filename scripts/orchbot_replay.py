"""BOTREPLAY: run orchbot's own Bot decision code over the recorded history with a fake clock.

    python scripts/orchbot_replay.py --root <repo> [--since ISO] [--until ISO]

Read-only on every input; writes only build/bot/replay/<run>/{decisions.jsonl,report.md,summary.json}.
Same inputs give a byte-identical summary.json (no wall clock, sorted keys).

Inputs and what they can prove (the limits are listed in report.md too):
  build/conveyor/events.jsonl        every event, fed to Bot.on_event at its own ts
  build/conveyor/ack/*               orchestrator acks; only the LAST touch of each name survives
  build/approvals/done/*             Key 1 files at their mtime, classified like Bot.observe
  build/conveyor/hold/backlog.json.bak-*  backlog snapshots by mtime: new ids = queue_fix_slice
  build/reports/*.md                 read by the Bot as today's reports (no history of them)
  build/orch/ORCH_NOTEBOOK.md        evidence lines only
Attention is synthesized from events (status.json has no history): a reader needs_orchestrator
event opens '<lane>', a cleanup_refused event opens 'cleanup:<lane>'; see Attention.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conveyor as cv  # noqa: E402
import orchbot as ob  # noqa: E402

PASS_S = 300  # one bot pass per 5 fake minutes (the live loop's cadence order)
ITEM_S = 86400  # DECISION TAKEN: an attention item nothing cleared lives a day (alternative: forever)
NOTE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}):? (.*)$")
KINDS = ("agree", "disagree", "unmatched", "missed")


def files(d):
    try:
        return sorted((p.name, p.stat().st_mtime) for p in Path(d).iterdir() if p.is_file())
    except FileNotFoundError:
        return []


def load_events(conv, since, until):
    out = []
    try:
        with open(conv / "events.jsonl", encoding="utf-8", errors="replace") as f:
            for line in f:  # streamed: the log is never held whole in memory
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                at = ob.parse_ts(ev.get("ts")) if isinstance(ev, dict) else None
                if at is not None and since <= at <= until:
                    out.append((at, ev))
    except OSError:
        return []  # no history: 0 events in the summary, never a crash
    return out  # file order: the conveyor appends in time order


def load_actuals(root, since, until):
    """Timed orchestrator/conveyor actions from files, in the shape Bot.observe returns."""
    conv, out = root / "build" / "conveyor", []
    for name, mt in files(conv / "ack"):
        out.append({"at": mt, "action": "ack", "key": "ack:" + name.lower(), "side": "orchestrator",
                    "what": f"ack/{name}"})
    for d in (root / "build" / "approvals", root / "build" / "approvals" / "done"):
        for raw, mt in files(d):
            name = ob.STAMP_RE.sub("", raw)
            if not cv.APPROVAL_RE.fullmatch(name):
                continue
            with open(Path(d) / raw, encoding="utf-8", errors="replace") as f:
                text = f.read(512)
            key = ob.agree_key("write_key1", name)
            if text.startswith("auto-approved by the conveyor"):
                act, side = "write_key1", "conveyor"
            elif re.search(r"\bby salih\b", text, re.I):
                act, side = "ask_salih_key1", "orchestrator"
            else:
                act, side = "write_key1", "orchestrator"
            out.append({"at": mt, "action": act, "key": key, "side": side, "what": f"{raw}: {text}"[:200]})
    backlogs, known = [], None
    for name, mt in sorted(files(conv / "hold"), key=lambda x: (x[1], x[0])):
        if not name.startswith("backlog.json.bak-"):
            continue
        try:
            data = json.loads((conv / "hold" / name).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue  # a broken snapshot adds no ids; the next good one is compared to the last good one
        if not isinstance(data, dict):
            continue
        backlogs.append((mt, data))
        ids = {str(s.get("id")) for s in data.get("slices") or [] if isinstance(s, dict) and s.get("id")}
        for i in sorted(ids - known) if known is not None else []:  # the first snapshot is a baseline
            out.append({"at": mt, "action": "queue_fix_slice", "key": ob.agree_key("queue_fix_slice", i.split("-")[0]),
                        "side": "orchestrator", "what": f"backlog id {i} ({name})"})
        known = ids if known is None else known | ids  # an id that leaves and comes back is not new
    acts = [a for a in out if since <= a["at"] <= until]
    return sorted(acts, key=lambda a: (a["at"], a["key"], a["action"])), backlogs


class Attention:
    """status.json attention rebuilt from events, in conveyor.py's reason shapes (2324-2373).
    DECISION TAKEN: an item clears on the lane's next dispatch/finish/cleanup or a newer reading, or
    after ITEM_S (alternative: only an ack clears it - the conveyor drops items the lane leaves)."""

    def __init__(self):
        self.items, self.status = {}, {}

    def feed(self, at, ev):
        kind, lane = ev.get("event"), str(ev.get("lane") or "")
        if not lane:
            return
        if kind == "finish":
            self.status[lane] = str(ev.get("status") or "").upper()
        if kind in ("dispatch", "finish", "reader"):
            self.items.pop(lane, None)
        if kind == "cleanup":
            self.items.pop("cleanup:" + lane, None)
        if kind == "reader":
            r = ev.get("reading") if isinstance(ev.get("reading"), dict) else {}
            if r.get("kind") == "needs_orchestrator":
                why, st = [], self.status.get(lane, "")
                if st and st not in cv.GOOD | {"CI_PENDING"}:
                    why.append(f"STATUS {st}")
                why.append(f"reader needs_orchestrator: {r.get('summary') or ''}")
                self.items[lane] = {"id": lane, "reason": "; ".join(why), "since": ev.get("ts"), "at": at}
        if kind == "cleanup_refused" and "cleanup:" + lane not in self.items:
            # DECISION TAKEN: since = the first refusal of the episode (alternative: each retry's ts)
            self.items["cleanup:" + lane] = {"id": "cleanup:" + lane, "since": ev.get("ts"), "at": at,
                                             "reason": f"cleanup refused ({ev.get('reason')}); retried after {ev.get('ts')}"}

    def now(self, t):
        self.items = {k: v for k, v in self.items.items() if t - v["at"] <= ITEM_S}
        return {"attention": [dict(id=v["id"], reason=v["reason"], since=v["since"]) for v in self.items.values()]}


def lanes_at(events, t):
    """lanes.json shape the handlers read, rebuilt from events up to t."""
    lanes = {}
    for at, ev in events:
        if at > t:
            break
        lane = ev.get("lane")
        if not isinstance(lane, str) or not lane:
            continue
        ln = lanes.setdefault(lane, {"ci": {}, "files": []})
        if ev.get("event") in ("ci_result", "ci_pending", "push") and ev.get("sha"):
            ln["ci"]["sha"] = ev["sha"]
        if ev.get("event") == "ci_pending":
            ln["ci"]["cand"] = True
    return lanes


def prune(st, now):  # Bot.save's in-memory pruning, without the write
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= now - ob.SEEN_S}
    st["respawns"] = {k: v for k, v in st["respawns"].items() if v and v[-1] >= now - ob.RESPAWN_S}
    st["history"] = [h for h in st["history"] if h[0] >= now - ob.LOOP_S]


def replay(root, since, until):
    root = Path(root)
    conv = root / "build" / "conveyor"
    events = load_events(conv, since, until)
    acts, backlogs = load_actuals(root, since, until)
    clock = [since]
    bot = ob.Bot(root, now=lambda: clock[0], sleep=lambda s: None)
    bot.new, bot.reports, bot.verdicts, bot.observed = [], {}, [], []
    bot.st = {"cursor": None, "decisions": [], "counters": {}, "seen": {}, "history": [], "fixes": {},
              "respawns": {}, "head": "", "agreement": ob.agree_blank(), "asks": {}, "conv": {}, "conv_push": {}}
    bot.st["agreement"].update(start=since, known={})
    att, window = Attention(), {"last": since}
    real_read = bot.read_json
    bot.read_json = lambda path, default: att.now(clock[0]) if path.name == "status.json" else \
        ({} if path.name == "pager.json" else real_read(path, default))

    def observe(known):
        lo, hi = window["last"], clock[0]
        return [a for a in acts if lo < a["at"] <= hi], known
    bot.observe = observe
    decisions, verdicts, ei, end = [], [], 0, until + ob.MATCH_S + 1
    t = since
    while t <= end:
        t = min(t + PASS_S, end) if t < end else end + 1
        clock[0] = min(t, end)
        bot.lanes = lanes_at(events, clock[0])
        bot.backlog = next((b for mt, b in reversed(backlogs) if mt <= clock[0]), {})
        while ei < len(events) and events[ei][0] <= clock[0]:
            at, ev = events[ei]
            ei += 1
            pass_t, clock[0] = clock[0], at
            att.feed(at, ev)
            try:
                bot.on_event(ev, at)
            except Exception as e:  # the same fail-closed path as Bot.run
                bot.decide(at, f"event {ev.get('event')} {ev.get('lane', '')}".strip(), "escalate_orchestrator",
                           "FAILCLOSED", "orchbot", f"handler error: {e!r}")
            clock[0] = pass_t
        try:
            bot.check_attention()
        except Exception as e:
            bot.decide(clock[0], "check_attention", "escalate_orchestrator", "FAILCLOSED", "orchbot",
                       f"check error: {e!r}")
        bot.agree()
        window["last"] = clock[0]
        decisions += bot.new
        verdicts += bot.verdicts
        bot.new, bot.verdicts, bot.observed = [], [], []
        prune(bot.st, clock[0])
        if t > end:
            break
    return decisions, verdicts, len(events), len(acts)


def notebook(root):
    out = []
    try:
        with open(root / "build" / "orch" / "ORCH_NOTEBOOK.md", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                m = NOTE_RE.match(line)
                if m and ob.parse_ts(m.group(1).replace(" ", "T") + ":00") is not None:
                    out.append((ob.parse_ts(m.group(1).replace(" ", "T") + ":00"), line))
    except FileNotFoundError:
        return []
    return out


def evidence(notes, v):
    """The notebook line nearest the verdict that names its target (+-6 h), or ''."""
    b, a = v.get("bot") or {}, v.get("orchestrator") or {}
    at = ob.parse_ts(b.get("at") or a.get("at"))
    word = (b.get("target") or v["key"].split(":", 1)[-1]).split(":")[-1].split("-")[0]
    hits = [(abs(t - at), line) for t, line in notes if word and len(word) > 2 and t is not None
            and abs(t - at) <= 6 * 3600 and re.search(re.escape(word), line, re.I)]
    return min(hits)[1][:300] if hits else ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--since", default="2026-10-09T00:00:00")
    ap.add_argument("--until", default=None)
    a = ap.parse_args(argv)
    root = Path(a.root)
    since = ob.parse_ts(a.since)
    if since is None:
        ap.error(f"bad --since {a.since!r}")
    if a.until is None:
        last = load_events(root / "build" / "conveyor", since, float("inf"))
        until = last[-1][0] if last else since
    else:
        until = ob.parse_ts(a.until)
        if until is None:
            ap.error(f"bad --until {a.until!r}")
    decisions, verdicts, n_ev, n_act = replay(root, since, until)
    per = {}
    for v in verdicts:
        name = (v.get("bot") or {}).get("would_do") or v["orchestrator"]["did"]
        row = per.setdefault(name, dict({k: 0 for k in KINDS}, n=0))
        row[v["verdict"]] += 1
        row["n"] += 1
    would = {}
    for d in decisions:
        would[d["would_do"]] = would.get(d["would_do"], 0) + 1
    summary = {"since": cv.iso(since), "until": cv.iso(until), "events": n_ev, "actuals": n_act,
               "decisions": len(decisions), "would_do": would, "per_action": per,
               "verdicts": {k: sum(r[k] for r in per.values()) for k in KINDS}}
    run = f"{cv.iso(since)}_{cv.iso(until)}".replace(":", "").replace("-", "")
    out = root / "build" / "bot" / "replay" / run
    out.mkdir(parents=True, exist_ok=True)
    body = json.dumps(summary, sort_keys=True, indent=1) + "\n"
    (out / "summary.json").write_text(body, encoding="utf-8", newline="\n")
    with open(out / "decisions.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for rec in [dict(d, kind="decision") for d in decisions] + [dict(v, kind="verdict") for v in verdicts]:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    notes = notebook(root)
    md = [f"# orchbot replay {summary['since']} .. {summary['until']}", "",
          f"events {n_ev}, actuals {n_act}, decisions {len(decisions)}", "",
          "action | n | agree | disagree | unmatched | missed"]
    md += [f"{k} | {r['n']} | " + " | ".join(str(r[x]) for x in KINDS) for k, r in sorted(per.items())]
    md += ["", "## disagreements and misses", ""]
    for v in verdicts:
        if v["verdict"] not in ("disagree", "missed"):
            continue
        b, o = v.get("bot") or {}, v.get("orchestrator") or {}
        saw = next((d["saw"] for d in decisions if b and d["ts"] == b.get("at") and d["target"] == b.get("target")
                    and d["would_do"] == b.get("would_do")), "")
        md.append(f"- {v['verdict']} {v['key']} at {b.get('at') or o.get('at')}: saw {saw!r}; bot "
                  f"{b.get('would_do', '-')} {b.get('rule') or ''}; actual {o.get('did', '-')} ({o.get('what', '')}); "
                  f"evidence {evidence(notes, v)!r}")
    md += ["", "## limits", "",
           "- reports are today's files, not the ones the orchestrator read; approved() sees today's build/approvals",
           "- only the last touch of each ack name survives, so earlier acks of a name are unmatched, not missed",
           "- backlog snapshots start 2026-10-19; queue_fix_slice actuals before that are invisible",
           "- attention is synthesized from reader/cleanup_refused events; pace.json cuts have no history"]
    (out / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8", newline="\n")
    print(f"{out}\nsummary sha256 {hashlib.sha256(body.encode()).hexdigest()}")
    print(json.dumps(summary["verdicts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
