"""Orchestrator bot, SHADOW MODE v0 (conveyor redesign stage 3).

One pass reads conveyor state - the build/conveyor/events.jsonl tail since the last cursor,
lanes.json, backlog.json, pager.json, status.json (attention), the lane reports and build/approvals -
and appends what it WOULD do to build/bot/decisions.jsonl; build/bot/shadow.json (atomic) holds the
cursor, counters, rule memory and the last 50 decisions for the pano. It never acts: no git, no
network, no model calls, never data/jarvis.db. Rules: ORCH_RULES O5 O10 O15 O19 O23 O26 O33 and
EVTAUDIT (R4, R7, #1). A bad input logs one escalate_orchestrator decision; a pass never crashes.

    python scripts/orchbot.py --shadow [--root DIR] [--once] [--quota-ceiling PCT]
"""
import argparse
import hashlib
import json
import os
import posixpath
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conveyor as cv  # noqa: E402  constants, regexes and pure helpers only

ACTIONS = ("none", "ack", "rerun_ci_once", "write_key1", "ask_salih_key1", "queue_fix_slice",
           "hold_dispatch", "escalate_orchestrator", "page_salih")
EPISODE_S = 2 * 3600  # a hold / page / bad-input episode ends after 2 h without its signal
LOOP_S, LOOP_N = 2 * 3600, 3  # the same action on the same target 3x in 2 h -> escalate instead
RESPAWN_S, RESPAWN_N = 30 * 60, 3  # EVTAUDIT #1: >=3 respawns of one lane in 30 min = outage
ACK_AGE_S = 25 * 60  # O33a: routine attention must not age past 30 min
FIX_CAP = 2  # O5/O15: a third fix for one target goes to the orchestrator
KEEP = 50
SEEN_S = 7 * 86400
REPORT_MAX = 256 * 1024
ROUTINE_RE = re.compile(r"^(reader needs_orchestrator|FINDINGS):")
# the only cleanup refusals raised after the work was proven on main (conveyor remove_worktree: worktree
# remove after the clean+integrated checks, branch delete after the off-main check); any other is kept
PROVEN_RE = re.compile(r"^cleanup refused \(git (?:worktree remove|update-ref -d \S+) rc=-?\d+: ")
FILES_RE = re.compile(r"(?m)^[ \t]*(?:[A-Z]+ )*FILES:[ \t]*(.*)$")
TS_FMT = "%Y-%m-%dT%H:%M:%S"
HEAD_N = 256  # events.jsonl rotation fingerprint: a hash of its first bytes


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


SHAPES = {  # shadow.json keys; a present key of another shape resets the state (fail closed)
    "decisions": lambda v: isinstance(v, list) and all(isinstance(d, dict) for d in v),
    "counters": lambda v: isinstance(v, dict) and all(num(x) for x in v.values()),
    "seen": lambda v: isinstance(v, dict) and all(num(x) for x in v.values()),
    "history": lambda v: isinstance(v, list) and all(
        isinstance(h, list) and len(h) == 3 and num(h[0]) and isinstance(h[1], str) and isinstance(h[2], str)
        for h in v),
    "fixes": lambda v: isinstance(v, dict) and all(type(x) is int for x in v.values()),
    "respawns": lambda v: isinstance(v, dict) and all(isinstance(x, list) and all(num(t) for t in x)
                                                     for x in v.values()),
    "head": lambda v: isinstance(v, str),
}


def parse_ts(ts):
    try:
        return time.mktime(time.strptime(str(ts), TS_FMT))
    except (ValueError, OverflowError):
        return None


def protected(files):
    """Why this file list needs Salih's key 1 (conveyor.protected_change on a file list), else None.
    Fail closed: no list, a quoted path, a glob or directory that may cover a protected path, the
    state doc, or a protected path anywhere in the path (any case, after normalising ..) is a reason.
    DECISION TAKEN: STATE_DOC asks key 1 (alternative: conveyor's git load-sensitive diff)."""
    if not files:
        return "no changed-file list for the range"
    dirs = tuple(d.casefold() for d in cv.PROTECTED_DIRS)
    pfiles = tuple(sorted({f.casefold() for f in cv.PROTECTED_FILES} | {cv.STATE_DOC.casefold()}))
    roots = dirs + pfiles
    for f in files:
        p = str(f).strip().replace("\\", "/")
        if not p:
            continue
        if p.startswith('"'):
            return f"quoted path {p}"
        q = posixpath.normpath(p).casefold()
        cut = re.search(r"[*?\[]", q)
        if cut or p.endswith("/") or q == ".":
            base = q[:cut.start()] if cut else "" if q == "." else q + "/"
            if any("/" + d in "/" + base for d in dirs) or any(r.startswith(base) for r in roots):
                return f"the glob {p} may cover a protected path"
        elif any("/" + d in "/" + q for d in dirs) or any(("/" + q).endswith("/" + r) for r in pfiles) \
                or cv.SKIPLIST_RE.search(q):
            return f"the range touches {p}"
    return None


class Bot:
    def __init__(self, root, now=time.time, sleep=time.sleep, quota_ceiling=90, from_start=False):
        self.root = Path(root)
        self.conv = self.root / "build" / "conveyor"
        self.out = self.root / "build" / "bot"
        self.now, self.sleep, self.ceiling, self.from_start = now, sleep, quota_ceiling, from_start

    # ---- state -------------------------------------------------------------------------------
    def run(self):
        """One pass; returns the decisions it appended."""
        self.out.mkdir(parents=True, exist_ok=True)
        self.new, self.reports = [], {}
        self.st = self.load_state()
        self.lanes = self.read_json(self.conv / "lanes.json", {})
        self.backlog = self.read_json(self.conv / "backlog.json", {})
        try:
            for ev, at in self.tail():
                self.st["counters"]["events"] = self.st["counters"].get("events", 0) + 1
                try:
                    self.on_event(ev, at)
                except Exception as e:  # fail closed: one decision, the pass goes on
                    self.decide(at, f"event {ev.get('event')} {ev.get('lane', '')}".strip(),
                                "escalate_orchestrator", "FAILCLOSED", "orchbot", f"handler error: {e!r}")
        except Exception as e:
            self.bad("events.jsonl", e)
        for check in (self.check_pager, self.check_attention):
            try:
                check()
            except Exception as e:
                self.decide(self.now(), check.__name__, "escalate_orchestrator", "FAILCLOSED", "orchbot",
                            f"check error: {e!r}")
        self.save()
        return self.new

    def load_state(self):
        blank = {"cursor": None, "decisions": [], "counters": {}, "seen": {}, "history": [],
                 "fixes": {}, "respawns": {}, "head": ""}
        path = self.out / "shadow.json"
        if not path.exists():
            return blank
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(st, dict):
                raise ValueError("not an object")
            bad = [k for k, ok in SHAPES.items() if k in st and not ok(st[k])]
            if bad:
                raise ValueError(f"bad shape: {', '.join(bad)}")
            out = {k: st.get(k, v) for k, v in blank.items()}
            cur = st.get("cursor")
            out["cursor"] = cur if type(cur) is int and cur >= 0 else None
            return out
        except Exception as e:
            # DECISION TAKEN: resume at the end of events.jsonl (alternative: replay it all, a storm)
            self.st = blank
            blank["cursor"] = self.events_size()
            self.decide(self.now(), "shadow.json unreadable", "escalate_orchestrator", "FAILCLOSED",
                        "shadow.json", f"{e!r}; state reset, cursor at the end of events.jsonl")
            return blank

    def events_size(self):
        try:
            return (self.conv / "events.jsonl").stat().st_size
        except OSError:
            return 0

    def read_json(self, path, default):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, type(default)):
                raise ValueError(f"expected {type(default).__name__}")
            return data
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as e:
            self.bad(path.name, e)
            return default

    def bad(self, name, err):
        if self.fresh(f"bad:{name}", self.now()):
            self.decide(self.now(), f"{name} unreadable", "escalate_orchestrator", "FAILCLOSED", name,
                        f"{err!r}"[:300])

    def tail(self):
        """(event, at) for each complete new line of events.jsonl; advances the cursor."""
        size = self.events_size()
        cur = self.st["cursor"]
        if cur is None:  # first pass: only what happens from now on, unless replaying (tests)
            cur = 0 if self.from_start else size
        n = self.st["head"].partition(":")[0]
        if cur > size:
            self.decide(self.now(), "events.jsonl shrank", "escalate_orchestrator", "FAILCLOSED",
                        "events.jsonl", f"cursor {cur} > size {size}; resuming at the end")
            cur = size
        elif n.isdigit() and 0 < int(n) <= size and self.head(int(n)) != self.st["head"]:
            # DECISION TAKEN: replay the new file from 0 (alternative: resume at the end, losing it)
            self.decide(self.now(), "events.jsonl replaced", "escalate_orchestrator", "FAILCLOSED",
                        "events.jsonl", f"its first {n} bytes changed (rotated?); replaying it from 0")
            cur = 0
        self.st["cursor"] = cur
        if cur == size:
            return self.mark()
        with open(self.conv / "events.jsonl", "rb") as f:
            f.seek(cur)
            chunk = f.read(size - cur)
        end = chunk.rfind(b"\n") + 1  # a half-written last line waits for the next pass
        for raw in chunk[:end].splitlines():
            if raw.strip():
                try:
                    ev = json.loads(raw.decode("utf-8"))
                    if not isinstance(ev, dict):
                        raise ValueError("not an object")
                except Exception as e:  # RecursionError on deep nesting too
                    self.decide(self.now(), "bad events.jsonl line", "escalate_orchestrator", "FAILCLOSED",
                                "events.jsonl", f"{e!r}"[:300])
                    continue
                yield ev, parse_ts(ev.get("ts")) or self.now()
        self.st["cursor"] = cur + end
        self.mark()

    def head(self, n):
        with open(self.conv / "events.jsonl", "rb") as f:
            b = f.read(n)
        return f"{len(b)}:{hashlib.sha256(b).hexdigest()[:16]}"

    def mark(self):
        cur = self.st["cursor"]
        self.st["head"] = self.head(min(HEAD_N, cur)) if cur else ""

    def fresh(self, key, at):
        """True when key has had no signal for EPISODE_S (a new episode); refreshes it."""
        last = self.st["seen"].get(key)
        self.st["seen"][key] = at
        return last is None or at - last > EPISODE_S

    def once(self, key, at):
        if key in self.st["seen"]:
            return False
        self.st["seen"][key] = at
        return True

    def decide(self, at, saw, action, rule, target, detail=""):
        assert action in ACTIONS, action  # nosec B101 - a programming error, the closed enum
        hist = [h for h in self.st["history"] if at - h[0] < LOOP_S]
        if action not in ("none", "escalate_orchestrator") and \
                sum(h[1] == action and h[2] == target for h in hist) >= LOOP_N - 1:
            detail = f"loop guard: {action} on {target} {LOOP_N}x in 2 h; {detail}"
            action, rule = "escalate_orchestrator", "LOOP"
        hist.append([at, action, target])
        self.st["history"] = hist
        rec = {"ts": cv.iso(self.now()), "saw": saw.replace("\n", " ")[:200], "would_do": action,
               "rule": rule, "target": target, "detail": str(detail).replace("\n", " ")[:500]}
        self.new.append(rec)
        self.st["counters"][action] = self.st["counters"].get(action, 0) + 1

    def save(self):
        now = self.now()
        st = self.st
        st["seen"] = {k: v for k, v in st["seen"].items() if v >= now - SEEN_S}
        st["respawns"] = {n: ts for n, ts in st["respawns"].items() if ts and ts[-1] >= now - RESPAWN_S}
        st["history"] = [h for h in st["history"] if h[0] >= now - LOOP_S]
        st["counters"]["passes"] = st["counters"].get("passes", 0) + 1
        if self.new and not self.retry(self.append, self.new):  # the decisions still land in shadow.json
            self.decide(now, "decisions.jsonl write failed", "escalate_orchestrator", "FAILCLOSED",
                        "decisions.jsonl", "this pass's decisions are only in shadow.json")
        if not self.retry(self.write_shadow):  # the decisions of this pass still land in decisions.jsonl
            self.decide(now, "shadow.json write failed", "escalate_orchestrator", "FAILCLOSED", "shadow.json",
                        f"PermissionError after {cv.REPLACE_TRIES} tries")
            self.retry(self.append, self.new[-1:])

    def retry(self, fn, *a):
        """fn(*a); PermissionError (the pano reading) is retried REPLACE_TRIES x REPLACE_GAP_S like
        CONVSTATUS, then one error (False); any other OSError is False at once."""
        for i in range(cv.REPLACE_TRIES):
            try:
                fn(*a)
                return True
            except PermissionError:
                if i < cv.REPLACE_TRIES - 1:
                    self.sleep(cv.REPLACE_GAP_S)
            except OSError:
                return False
        return False

    def append(self, recs):
        with open(self.out / "decisions.jsonl", "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in recs))

    def write_shadow(self):
        """shadow.json via tmp + os.replace."""
        st = dict(self.st, decisions=(self.st["decisions"] + self.new)[-KEEP:], updated=cv.iso(self.now()))
        path = self.out / "shadow.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # ---- inputs ------------------------------------------------------------------------------
    def report(self, lane, ln):
        """The newest build/reports/<lane>.md (main checkout or the lane worktree), '' if none."""
        if lane not in self.reports:
            text, best = "", -1.0
            for p in (self.root / "build" / "reports" / f"{lane}.md",
                      self.root / ".claude" / "worktrees" / lane / "build" / "reports" / f"{lane}.md"):
                try:
                    mt = p.stat().st_mtime
                    if mt > best:
                        with open(p, encoding="utf-8", errors="replace") as f:
                            text, best = f.read(REPORT_MAX), mt
                except OSError:
                    continue
            self.reports[lane] = text
        return self.reports[lane]

    def changed(self, ln, rep):
        """The range's files as far as the bot can tell without git: the lane's grant plus the
        report's FILES / ADJACENT FILES paths. DECISION TAKEN: no git diff (alternative: git diff
        origin/main..sha, a process the shadow bot must not need)."""
        files = [str(f) for f in ln.get("files") or []]
        for m in FILES_RE.finditer(rep):
            for tok in re.split(r"[,\s]+", m.group(1)):
                tok = tok.lstrip("(`'\";:").rstrip(")`'\";:,.")  # never a leading '.': .claude, .env
                if "/" in tok or "\\" in tok or "." in tok:
                    files.append(tok)
        return files

    def approved(self, sha):
        try:
            names = os.listdir(self.root / "build" / "approvals")
        except OSError:
            return False
        for n in names:
            m = cv.APPROVAL_RE.search(n)
            if m and sha.startswith(m.group(1)):
                return True
        return False

    # ---- event rules -------------------------------------------------------------------------
    def on_event(self, ev, at):
        handler = {"ci_result": self.ci_result, "finish": self.finish, "respawn": self.respawn,
                   "error": self.error, "push_blocked": self.push_blocked, "pace": self.pace}.get(ev.get("event"))
        if handler:
            handler(ev, at, str(ev.get("lane") or ""))

    def ci_result(self, ev, at, lane):
        ln = self.lanes.get(lane) or {}
        sha = str(ev.get("sha") or "")
        saw = f"CI {ev.get('result')} {lane} {sha[:12]}"
        if ev.get("result") == "success":
            return self.key1(at, saw, lane, ln, sha)
        rep = self.report(lane, ln)
        ids = sorted(set(cv.TEST_ID_RE.findall(rep)))
        mods = sorted({"/".join(i.split(".")[:2]) + ".py" for i in ids})
        own = [m for m in mods if any(m.startswith(str(f).rstrip("*")) for f in ln.get("files") or [])]
        if not ids:
            self.decide(at, saw, "escalate_orchestrator", "R4", lane, "CI red, the report names no failing test")
        elif own:
            self.fix(at, saw, lane, f"CI red in the lane's own tests: {', '.join(own)}")
        elif (ln.get("ci") or {}).get("rerun") or not self.once(f"rerun:{lane}:{sha}", at):
            self.decide(at, saw, "escalate_orchestrator", "R4", lane,
                        f"CI red again after one rerun, outside the lane's files: {', '.join(ids)[:300]}")
        else:
            self.decide(at, saw, "rerun_ci_once", "R4", lane,
                        f"failing tests outside the lane's files: {', '.join(ids)[:300]}")

    def key1(self, at, saw, lane, ln, sha):
        if not sha:
            return self.decide(at, saw, "escalate_orchestrator", "O26", lane, "green CI without a sha")
        if not self.once(f"key1:{lane}:{sha}", at):
            return
        rep = self.report(lane, ln)
        if not ((ln.get("ci") or {}).get("cand") or cv.CANDIDATE_RE.search(rep)):
            return self.decide(at, saw, "none", "O26", lane, "not a candidate flow; the conveyor gate decides")
        if self.approved(sha):
            return self.decide(at, saw, "none", "O26", lane, "key 1 already in build/approvals")
        why = protected(self.changed(ln, rep))
        if why:
            self.decide(at, saw, "ask_salih_key1", "CONVSAFE", lane, why)
        else:
            self.decide(at, saw, "write_key1", "O26", lane, f"build/approvals/push-{sha}-{lane}")

    def finish(self, ev, at, lane):
        status = str(ev.get("status") or "").upper()
        ln = self.lanes.get(lane) or {}
        saw = f"finish {lane} {status}"
        if status == "GREEN":  # ci_done after CI_PENDING; deduped with its ci_result on the sha
            return self.key1(at, saw, lane, ln, str((ln.get("ci") or {}).get("sha") or ""))
        if status not in cv.BAD or str(ev.get("reason") or "").startswith("CI:"):
            return  # a CI red was decided on its ci_result
        need = cv.NEEDS_RE.search(self.report(lane, ln))
        deps = [str(s.get("id")) for s in self.backlog.get("slices") or []
                if isinstance(s, dict) and lane in (s.get("depends_on") or [])]
        if need and need.group(1).strip():
            self.fix(at, saw, lane, f"NEEDS FILES: {need.group(1).strip()}")
        elif deps:
            self.decide(at, saw, "escalate_orchestrator", "O10", lane,
                        f"blocked dependency of {', '.join(deps)}; never unblocks them")
        else:
            self.decide(at, saw, "none", "O10", lane, "blocked, no dependents; the conveyor pages it")

    def fix(self, at, saw, lane, detail):
        target = lane.split("-")[0]  # INT14-AFX and INT14-R count against INT14
        n = self.st["fixes"].get(target, 0)
        if n >= FIX_CAP:
            return self.decide(at, saw, "escalate_orchestrator", "O5", target,
                               f"{n} fixes already queued for {target}; {detail}")
        self.st["fixes"][target] = n + 1
        self.decide(at, saw, "queue_fix_slice", "O5", target, detail)

    def push_blocked(self, ev, at, lane):
        self.decide(at, f"push blocked {ev.get('approval', '')}", "ask_salih_key1", "R7/O19",
                    str(ev.get("approval") or "main"), str(ev.get("reason") or ""))

    def pace(self, ev, at, lane):
        pct = max([v for v in (ev.get("usage_pct"), ev.get("projected_pct"))
                   if isinstance(v, (int, float))], default=None)
        if pct is not None and pct > self.ceiling and self.fresh("quota", at):
            self.decide(at, f"quota {pct}% > {self.ceiling}%", "hold_dispatch", "O23", "dispatch",
                        f"mode {ev.get('mode')}, band {ev.get('band')}")

    def respawn(self, ev, at, lane):
        rs = [t for t in self.st["respawns"].get(lane, []) if at - t < RESPAWN_S] + [at]
        self.st["respawns"][lane] = rs
        api = cv.API_ERR_RE.search(str(ev.get("output") or ""))
        if api or len(rs) >= RESPAWN_N:
            self.outage(at, f"respawn {lane} rc={ev.get('rc')}, {len(rs)} in 30 min",
                        f"API error text: {api.group(0)}" if api else f"{lane} respawned {len(rs)}x in 30 min")

    def error(self, ev, at, lane):
        api = cv.API_ERR_RE.search(str(ev.get("message") or ""))
        if api:
            self.outage(at, f"error {lane}".strip(), f"API error text: {api.group(0)}")

    def outage(self, at, saw, why):
        """EVTAUDIT #1: hold dispatch and page Salih once per episode (the page is the episode's
        state-change record, so one event may carry both)."""
        if self.fresh("outage", at):
            self.decide(at, saw, "hold_dispatch", "EVTAUDIT1", "dispatch", why)
            self.decide(at, "outage episode opened", "page_salih", "EVTAUDIT1", "salih", why)

    # ---- state rules -------------------------------------------------------------------------
    def check_pager(self):
        bi = self.read_json(self.conv / "pager.json", {}).get("backlog_invalid")
        if bi and self.once(f"backlog_invalid:{json.dumps(bi, sort_keys=True)}", self.now()):
            self.decide(self.now(), "pager backlog_invalid", "escalate_orchestrator", "PAGER",
                        "backlog.json", str(bi)[:300])

    def check_attention(self):
        now = self.now()
        for it in self.read_json(self.conv / "status.json", {}).get("attention") or []:
            iid, reason, since = str(it.get("id")), str(it.get("reason") or ""), parse_ts(it.get("since"))
            routine = iid.startswith(("cleanup:", "preserved:")) or \
                (reason and all(ROUTINE_RE.match(p) for p in reason.split("; ")))
            if not routine or since is None or now - since <= ACK_AGE_S:
                continue
            ack = self.conv / "ack" / cv.ack_name(iid)
            if ack.exists() and ack.stat().st_mtime >= since:
                continue
            if iid.startswith("cleanup:") and not PROVEN_RE.match(reason):
                # BOTV1: work not proven on main stays in front of a human (fail closed); logged once a day
                if self.once(f"keep:{iid}:{cv.iso(now)[:10]}", now):
                    self.decide(now, f"cleanup refusal {iid}: work not on main", "none", "KEEP-UNMERGED",
                                iid, reason[:300])
                continue
            if self.once(f"ack:{iid}:{it.get('since')}", now):
                self.decide(now, f"routine attention {iid} {int((now - since) // 60)} min old", "ack", "O33",
                            iid, f"touch build/conveyor/ack/{cv.ack_name(iid)}")


def main(argv=None, now=time.time):
    ap = argparse.ArgumentParser(description="Orchestrator bot, shadow mode: logs what it would do.")
    ap.add_argument("--shadow", action="store_true", required=True, help="log only (the only mode in v0)")
    ap.add_argument("--root", default=str(cv.ROOT), help="repo root (default: this checkout)")
    ap.add_argument("--once", action="store_true", help="one pass (v0 always runs one; schedule it)")
    ap.add_argument("--quota-ceiling", type=float, default=90, help="hold dispatch above this usage %%")
    try:
        a = ap.parse_args(argv)
    except SystemExit:  # a bad command line still exits 0; argparse printed why
        return 0
    try:
        Bot(a.root, now=now, quota_ceiling=a.quota_ceiling).run()
    except Exception as e:  # never crash the scheduler; the next pass retries
        print(f"orchbot: {e!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
